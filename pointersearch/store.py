"""SQLite store for images, overlays and candidates.

One database per mission working directory. WAL mode lets the ingest process
write while the review server reads.
"""

from __future__ import annotations

import json
import math
import sqlite3

import numpy as np
from pathlib import Path
from typing import Iterable

STATUSES = ("pending", "accepted", "followup", "rejected")

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
    processed_at TEXT DEFAULT CURRENT_TIMESTAMP,
    capture_key TEXT,        -- shared by the wide / zoom / thermal files of one shutter press
    proc_version INTEGER     -- detector version that searched this image (NULL = 1)
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
    status TEXT DEFAULT 'pending',   -- pending | accepted | followup | rejected
    note TEXT,
    decided_at TEXT,
    range_m REAL,            -- horizontal distance from the aircraft to the sighting
    group_id INTEGER         -- object id shared by sightings of the same thing across frames
);
CREATE TABLE IF NOT EXISTS links (            -- matched points between two overlapping photos
    a INTEGER NOT NULL, b INTEGER NOT NULL,
    n INTEGER NOT NULL,
    pts BLOB NOT NULL,                        -- float32 (n, 4): north, east in a; north, east in b
    lum_a REAL, lum_b REAL,
    PRIMARY KEY (a, b)
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
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
        if "capture_key" not in {r["name"] for r in self.conn.execute("PRAGMA table_info(images)")}:
            self.conn.execute("ALTER TABLE images ADD COLUMN capture_key TEXT")
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(images)")}
        for col, typ in (("proc_version", "INTEGER"), ("align", "TEXT"), ("aligned_png", "TEXT"),
                         ("aligned_bounds", "TEXT")):
            if col not in have:
                self.conn.execute(f"ALTER TABLE images ADD COLUMN {col} {typ}")
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(candidates)")}
        for col in ("lat_raw", "lon_raw"):                      # position before photo alignment
            if col not in have:
                self.conn.execute(f"ALTER TABLE candidates ADD COLUMN {col} REAL")
        self.conn.execute("UPDATE candidates SET lat_raw=lat, lon_raw=lon WHERE lat_raw IS NULL")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_images_capture ON images(capture_key)")
        # missions processed before lens switching existed: derive the key from the file name
        from .capture import capture_key
        todo = self.conn.execute("SELECT id, path FROM images WHERE capture_key IS NULL").fetchall()
        self.conn.executemany("UPDATE images SET capture_key=? WHERE id=?",
                              [(capture_key(r["path"]) or f"single|{r['id']}", r["id"]) for r in todo])
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
                "INSERT INTO candidates (image_id, detector, score, px, py, bbox, area_m2, lat, lon, thumb, extra, "
                "range_m, lat_raw, lon_raw) VALUES (:image_id, :detector, :score, :px, :py, :bbox, :area_m2, :lat, "
                ":lon, :thumb, :extra, :range_m, :lat, :lon)",
                [{"range_m": None, **c, "image_id": image_id, "bbox": json.dumps(c["bbox"]),
                  "extra": json.dumps(c.get("extra", {}))} for c in candidates],
            )

    def clean_split_frames(self) -> dict:
        """Missions processed before version 0.2 searched "_S" split-screen pictures as if
        they were colour photos. Remove what that produced: undecided detections found in them
        and their patches on the mosaic. Decisions a reviewer already made are kept."""
        rows = self.conn.execute("SELECT id, path FROM images").fetchall()
        ids = [r["id"] for r in rows if Path(r["path"]).stem.upper().endswith("_S")]
        if not ids:
            return {"frames": 0, "detections_removed": 0}
        removed = 0
        with self.conn:
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                marks = ",".join("?" * len(chunk))
                removed += self.conn.execute(
                    f"DELETE FROM candidates WHERE status='pending' AND image_id IN ({marks})", chunk).rowcount
                self.conn.execute(f"UPDATE images SET source='split', overlay_png=NULL, overlay_bounds=NULL, "
                                  f"footprint=NULL WHERE id IN ({marks})", chunk)
        return {"frames": len(ids), "detections_removed": removed}

    def stale_images(self, versions: dict[str, int]) -> list[dict]:
        """Images searched by an older detector than the current one for their lens."""
        out = []
        for source, version in versions.items():
            rows = self.conn.execute(
                "SELECT id, path, source FROM images WHERE status='ok' AND source=? AND COALESCE(proc_version, 1) < ?",
                (source, version)).fetchall()
            out += [dict(r) for r in rows]
        return out

    def get_image(self, image_id: int) -> dict | None:
        r = self.conn.execute("SELECT * FROM images WHERE id=?", (image_id,)).fetchone()
        return _image_row(r) if r else None

    def siblings(self, image: dict) -> list[dict]:
        """Every processed frame from the same shutter press, the given one included."""
        key = image.get("capture_key")
        if not key or key.startswith("single|"):
            return [image]
        rows = self.conn.execute("SELECT * FROM images WHERE capture_key=? AND status='ok' ORDER BY id",
                                 (key,)).fetchall()
        out = [_image_row(r) for r in rows]
        return out if any(i["id"] == image["id"] for i in out) else [image] + out

    def list_images(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM images ORDER BY captured_at, id").fetchall()
        return [_image_row(r) for r in rows]

    def overlays(self) -> list[dict]:
        """Map patches, one per photo: the aligned version where there is one."""
        rows = self.conn.execute(
            "SELECT id, source, overlay_png, overlay_bounds, aligned_png, aligned_bounds FROM images "
            "WHERE overlay_png IS NOT NULL ORDER BY captured_at, id").fetchall()
        return [{"id": r["id"], "source": r["source"] or "unknown", "aligned": bool(r["aligned_png"]),
                 "png": r["aligned_png"] or r["overlay_png"],
                 "bounds": json.loads(r["aligned_bounds"] or r["overlay_bounds"])} for r in rows]

    # ---- photo alignment -----------------------------------------------
    def origin(self, lat: float | None = None, lon: float | None = None) -> tuple[float, float] | None:
        """The mission's local map origin. Set once, from the first photo."""
        r = self.conn.execute("SELECT value FROM kv WHERE key='origin'").fetchone()
        if r:
            return tuple(json.loads(r["value"]))
        if lat is None or lon is None:
            return None
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO kv (key, value) VALUES ('origin', ?)", (json.dumps([lat, lon]),))
        return self.origin()

    def kv(self, key: str):
        r = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else None

    def save_links(self, image_id: int, links: list) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM links WHERE a=? OR b=?", (image_id, image_id))
            self.conn.executemany(
                "INSERT OR REPLACE INTO links (a, b, n, pts, lum_a, lum_b) VALUES (?,?,?,?,?,?)",
                [(l.a, l.b, len(l.pa), np.hstack([l.pa, l.pb]).astype(np.float32).tobytes(), l.lum_a, l.lum_b)
                 for l in links])

    def all_links(self) -> list:
        from .align import Link
        out = []
        for r in self.conn.execute("SELECT * FROM links"):
            pts = np.frombuffer(r["pts"], dtype=np.float32).reshape(-1, 4)
            out.append(Link(r["a"], r["b"], pts[:, :2], pts[:, 2:], r["lum_a"] or 0.0, r["lum_b"] or 0.0))
        return out

    def set_alignment(self, image_id: int, align: dict | None, png: str | None, bounds: list | None) -> None:
        self.conn.execute("UPDATE images SET align=?, aligned_png=?, aligned_bounds=? WHERE id=?",
                          (json.dumps(align) if align else None, png, json.dumps(bounds) if bounds else None, image_id))

    def move_candidates(self, image_id: int, to_latlon) -> None:
        """Re-place an image's detections. ``to_latlon(lat_raw, lon_raw)`` -> (lat, lon)."""
        rows = self.conn.execute("SELECT id, lat_raw, lon_raw FROM candidates WHERE image_id=?", (image_id,)).fetchall()
        self.conn.executemany("UPDATE candidates SET lat=?, lon=? WHERE id=?",
                              [(*to_latlon(r["lat_raw"], r["lon_raw"]), r["id"]) for r in rows if r["lat_raw"] is not None])

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
        if status not in STATUSES:
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
        """One record per object: best sighting, member ids, frames seen, combined status.

        ``status`` may be one status or a comma-separated list ("accepted,followup")."""
        wanted = {x.strip() for x in status.split(",") if x.strip()} if status else set()
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
            elif "followup" in statuses:
                g_status = "followup"
            elif statuses == {"rejected"}:
                g_status = "rejected"
            else:
                g_status = "pending"
            if wanted and g_status not in wanted:
                continue
            out.append({
                "id": gid, "status": g_status, "lat": nearest["lat"], "lon": nearest["lon"],
                "score": best["score"], "detector": best["detector"],
                "detectors": sorted({c["detector"] for c in members}),
                "n_sightings": len(members), "n_frames": len({c["image_id"] for c in members}),
                "min_range_m": nearest["range_m"], "area_m2": best["area_m2"],
                "rx_score": max((c["score"] for c in members if c["detector"] == "rx"), default=None),
                "red_hot": any((c.get("extra") or {}).get("red_hot") for c in members),
                # each detector scores on its own scale, so keep them apart
                "scores": {d: max(c["score"] for c in members if c["detector"] == d)
                           for d in sorted({c["detector"] for c in members})},
                "best": best, "members": [c["id"] for c in members],
                "thumbs": [c["thumb"] for c in members[:8]],
                "note": next((c["note"] for c in members if c["note"]), None),
            })
        out.sort(key=lambda g: g["score"], reverse=True)
        return out

    def set_group_decision(self, gid: int, status: str, note: str | None = None) -> int:
        if status not in STATUSES:
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
    d["aligned_bounds"] = json.loads(d["aligned_bounds"]) if d.get("aligned_bounds") else None
    d["align"] = json.loads(d["align"]) if d.get("align") else None
    return d


def _cand_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["bbox"] = json.loads(d["bbox"]) if d.get("bbox") else None
    d["extra"] = json.loads(d["extra"]) if d.get("extra") else {}
    return d
