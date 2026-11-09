"""SQLite store for images, overlays and candidates.

One database per mission working directory. WAL mode lets the ingest process
write while the review server reads.
"""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from typing import Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    mtime REAL NOT NULL,
    source TEXT,
    lat REAL, lon REAL, rel_alt REAL,
    gimbal_yaw REAL, gimbal_pitch REAL, gimbal_roll REAL,
    width INTEGER, height INTEGER,
    gsd_m REAL,
    captured_at TEXT,
    footprint TEXT,          -- JSON [[lat,lon],...] TL,TR,BR,BL
    overlay_png TEXT,
    overlay_bounds TEXT,     -- JSON [[south,west],[north,east]]
    status TEXT DEFAULT 'ok',
    error TEXT,
    processed_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY,
    image_id INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    detector TEXT NOT NULL,
    score REAL NOT NULL,
    px REAL, py REAL,
    bbox TEXT,               -- JSON [x,y,w,h]
    area_m2 REAL,
    lat REAL, lon REAL,
    thumb TEXT,
    extra TEXT,
    status TEXT DEFAULT 'pending',   -- pending | accepted | rejected
    note TEXT,
    decided_at TEXT,
    range_m REAL,            -- horizontal distance from the aircraft to the sighting
    group_id INTEGER         -- object id shared by sightings of the same thing across frames
);
CREATE INDEX IF NOT EXISTS idx_candidates_status ON candidates(status);
CREATE INDEX IF NOT EXISTS idx_candidates_image ON candidates(image_id);
"""


class Store:
    def __init__(self, workdir: str | Path):
        self.workdir = Path(workdir)
        self.path = self.workdir / "pointersearch.db"
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self):
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(candidates)")}
        for col, typ in (("range_m", "REAL"), ("group_id", "INTEGER")):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE candidates ADD COLUMN {col} {typ}")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_candidates_group ON candidates(group_id)")
        self.conn.commit()

    def close(self):
        self.conn.close()

    # ---- images -------------------------------------------------------
    def image_is_current(self, path: Path) -> bool:
        row = self.conn.execute("SELECT mtime, status FROM images WHERE path=?", (str(path),)).fetchone()
        return bool(row) and abs(row["mtime"] - path.stat().st_mtime) < 1e-6 and row["status"] == "ok"

    def upsert_image(self, record: dict) -> int:
        cols = ", ".join(record.keys())
        placeholders = ", ".join("?" for _ in record)
        updates = ", ".join(f"{k}=excluded.{k}" for k in record if k != "path")
        cur = self.conn.execute(
            f"INSERT INTO images ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(path) DO UPDATE SET {updates}, processed_at=CURRENT_TIMESTAMP",
            tuple(record.values()),
        )
        self.conn.commit()
        if cur.lastrowid:
            return int(cur.lastrowid)
        return int(self.conn.execute("SELECT id FROM images WHERE path=?", (record["path"],)).fetchone()["id"])

    def replace_candidates(self, image_id: int, candidates: Iterable[dict]):
        with self.conn:
            self.conn.execute("DELETE FROM candidates WHERE image_id=? AND status='pending'", (image_id,))
            self.conn.executemany(
                "INSERT INTO candidates (image_id, detector, score, px, py, bbox, area_m2, lat, lon, thumb, extra, range_m) "
                "VALUES (:image_id, :detector, :score, :px, :py, :bbox, :area_m2, :lat, :lon, :thumb, :extra, :range_m)",
                [{"range_m": None, **c, "image_id": image_id, "bbox": json.dumps(c["bbox"]),
                  "extra": json.dumps(c.get("extra", {}))} for c in candidates],
            )

    def list_images(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM images ORDER BY captured_at, id").fetchall()
        return [_image_row(r) for r in rows]

    def overlays(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, overlay_png, overlay_bounds FROM images WHERE overlay_png IS NOT NULL ORDER BY captured_at, id"
        ).fetchall()
        return [{"id": r["id"], "png": r["overlay_png"], "bounds": json.loads(r["overlay_bounds"])} for r in rows]

    # ---- candidates ----------------------------------------------------
    def list_candidates(self, status: str | None = None, detector: str | None = None,
                        since_id: int = 0) -> list[dict]:
        q = ("SELECT c.*, i.path AS image_path, i.source AS source, i.captured_at AS captured_at "
             "FROM candidates c JOIN images i ON i.id=c.image_id WHERE c.id>?")
        args: list = [since_id]
        if status:
            q += " AND c.status=?"
            args.append(status)
        if detector:
            q += " AND c.detector=?"
            args.append(detector)
        q += " ORDER BY c.score DESC"
        return [_cand_row(r) for r in self.conn.execute(q, args).fetchall()]

    def get_candidate(self, cid: int) -> dict | None:
        r = self.conn.execute(
            "SELECT c.*, i.path AS image_path, i.source AS source, i.captured_at AS captured_at "
            "FROM candidates c JOIN images i ON i.id=c.image_id WHERE c.id=?", (cid,)).fetchone()
        return _cand_row(r) if r else None

    def set_decision(self, cid: int, status: str, note: str | None = None) -> bool:
        if status not in ("pending", "accepted", "rejected"):
            raise ValueError(status)
        with self.conn:
            cur = self.conn.execute(
                "UPDATE candidates SET status=?, note=COALESCE(?, note), "
                "decided_at=CASE WHEN ?='pending' THEN NULL ELSE CURRENT_TIMESTAMP END WHERE id=?",
                (status, note, status, cid))
        return cur.rowcount > 0

    # ---- groups (one object seen in many frames) -------------------------
    def regroup(self, radius_m: float) -> int:
        """Cluster all candidates whose positions fall within ``radius_m`` of each
        other (single linkage via a grid hash + union-find). Returns group count."""
        rows = self.conn.execute("SELECT id, lat, lon FROM candidates").fetchall()
        if not rows:
            return 0
        lat0 = rows[0]["lat"]
        k_lat = 111_320.0
        k_lon = 111_320.0 * math.cos(math.radians(lat0))
        pts = {r["id"]: ((r["lat"] - lat0) * k_lat, (r["lon"] - rows[0]["lon"]) * k_lon) for r in rows}
        parent = {i: i for i in pts}

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        cell = radius_m
        grid: dict[tuple[int, int], list[int]] = {}
        for i, (n, e) in pts.items():
            grid.setdefault((int(n // cell), int(e // cell)), []).append(i)
        r2 = radius_m * radius_m
        for (cn, ce), ids in grid.items():
            neighbours = []
            for dn in (-1, 0, 1):
                for de in (-1, 0, 1):
                    neighbours += grid.get((cn + dn, ce + de), [])
            for i in ids:
                ni, ei = pts[i]
                for j in neighbours:
                    if j <= i:
                        continue
                    nj, ej = pts[j]
                    if (ni - nj) ** 2 + (ei - ej) ** 2 <= r2:
                        ri, rj = find(i), find(j)
                        if ri != rj:
                            parent[max(ri, rj)] = min(ri, rj)
        with self.conn:
            self.conn.executemany("UPDATE candidates SET group_id=? WHERE id=?",
                                  [(find(i), i) for i in pts])
        return len({find(i) for i in pts})

    def list_groups(self, status: str | None = None) -> list[dict]:
        """One record per object: best sighting, member ids, frames seen, combined status."""
        cands = self.list_candidates()
        groups: dict[int, list[dict]] = {}
        for c in cands:
            groups.setdefault(c["group_id"] if c["group_id"] is not None else c["id"], []).append(c)
        out = []
        for gid, members in groups.items():
            members.sort(key=lambda c: c["score"], reverse=True)
            best = members[0]
            # Position from the sighting with the best geometry (closest to the aircraft).
            nearest = min(members, key=lambda c: c["range_m"] if c["range_m"] is not None else 1e9)
            statuses = {c["status"] for c in members}
            if "accepted" in statuses:
                g_status = "accepted"
            elif statuses == {"rejected"}:
                g_status = "rejected"
            else:
                g_status = "pending"
            if status and g_status != status:
                continue
            out.append({
                "id": gid, "status": g_status, "lat": nearest["lat"], "lon": nearest["lon"],
                "score": best["score"], "detector": best["detector"],
                "detectors": sorted({c["detector"] for c in members}),
                "n_sightings": len(members), "n_frames": len({c["image_id"] for c in members}),
                "min_range_m": nearest["range_m"], "area_m2": best["area_m2"],
                "best": best, "members": [c["id"] for c in members],
                "thumbs": [c["thumb"] for c in members[:8]],
                "note": next((c["note"] for c in members if c["note"]), None),
            })
        out.sort(key=lambda g: g["score"], reverse=True)
        return out

    def set_group_decision(self, gid: int, status: str, note: str | None = None) -> int:
        if status not in ("pending", "accepted", "rejected"):
            raise ValueError(status)
        with self.conn:
            cur = self.conn.execute(
                "UPDATE candidates SET status=?, note=COALESCE(?, note), "
                "decided_at=CASE WHEN ?='pending' THEN NULL ELSE CURRENT_TIMESTAMP END "
                "WHERE group_id=? OR (group_id IS NULL AND id=?)",
                (status, note, status, gid, gid))
        return cur.rowcount

    def summary(self) -> dict:
        imgs = self.conn.execute("SELECT COUNT(*) AS n, SUM(status='ok') AS ok FROM images").fetchone()
        by_status = {r["status"]: r["n"] for r in
                     self.conn.execute("SELECT status, COUNT(*) AS n FROM candidates GROUP BY status")}
        by_detector = {r["detector"]: r["n"] for r in
                       self.conn.execute("SELECT detector, COUNT(*) AS n FROM candidates GROUP BY detector")}
        last = self.conn.execute("SELECT MAX(id) AS m FROM candidates").fetchone()["m"] or 0
        groups = self.list_groups()
        g_status = {}
        for g in groups:
            g_status[g["status"]] = g_status.get(g["status"], 0) + 1
        return {"images": imgs["n"], "images_ok": imgs["ok"] or 0, "candidates": by_status,
                "detectors": by_detector, "last_candidate_id": last,
                "groups": g_status, "n_groups": len(groups)}


def _image_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["footprint"] = json.loads(d["footprint"]) if d.get("footprint") else None
    d["overlay_bounds"] = json.loads(d["overlay_bounds"]) if d.get("overlay_bounds") else None
    return d


def _cand_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["bbox"] = json.loads(d["bbox"]) if d.get("bbox") else None
    d["extra"] = json.loads(d["extra"]) if d.get("extra") else {}
    return d
