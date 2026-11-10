"""Line photos up with each other before they are laid on the map.

Each photo is first placed from the aircraft's own record: GPS position, height and
gimbal angles, onto flat ground at take-off level. That is quick but every photo
carries its own error: a few metres of GPS, a degree or two of heading, and a scale
error whenever the visible surface is not at take-off level (a forest canopy can be
tens of metres closer to the camera than the ground, which makes its photos come out
too large).

Here the photos correct one another:

1. **Features.** Distinct points are found in each photo (SIFT) and given a ground
   position from that photo's first placement.
2. **Links.** For photos whose footprints overlap, the same points are matched. A
   robust fit keeps only matches that agree on one consistent shift, turn and scale.
3. **Solve.** One correction per photo (shift, turn, scale about its own centre) is
   solved for all photos at once, so that matched points land on the same spot,
   while each photo is held near its recorded GPS position. The GPS keeps the whole
   map from drifting; the matches make neighbours agree. Scale is not guessed: two
   photos taken a known distance apart only agree at the right scale, which is how
   the height of the visible surface is recovered.

This is a flat, two-dimensional adjustment. It makes a search mosaic line up within
a metre or so in seconds per photo. It does not remove the lean of tall objects or
terrain relief; that needs a full photogrammetric reconstruction from photos flown
with far more overlap.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np

from .config import AlignConfig


@dataclass
class Features:
    ne: np.ndarray          # (N, 2) float32 ground position (north, east) from the first placement
    desc: np.ndarray        # (N, 128) uint8 SIFT descriptors
    lum: np.ndarray         # (N,) float32 local brightness, for matching exposure between photos


@dataclass
class Link:
    a: int
    b: int
    pa: np.ndarray          # (K, 2) points in photo a, first-placement ground coordinates
    pb: np.ndarray          # (K, 2) the same points as seen in photo b
    lum_a: float
    lum_b: float


@dataclass
class Correction:
    """p' = centre + (1 + ds) * R(rot) * (p - centre) + shift, in (north, east) metres."""

    cn: float
    ce: float
    ds: float = 0.0
    b: float = 0.0          # sine-like term of the linearised turn-and-scale
    tn: float = 0.0
    te: float = 0.0
    gain: float = 1.0
    links: int = 0

    def apply(self, pts: np.ndarray) -> np.ndarray:
        pts = np.asarray(pts, dtype=np.float64).reshape(-1, 2)
        q = pts - (self.cn, self.ce)
        a = 1.0 + self.ds
        out = np.empty_like(q)
        out[:, 0] = self.cn + a * q[:, 0] - self.b * q[:, 1] + self.tn
        out[:, 1] = self.ce + self.b * q[:, 0] + a * q[:, 1] + self.te
        return out

    @property
    def scale(self) -> float:
        return math.hypot(1.0 + self.ds, self.b)

    @property
    def turn_deg(self) -> float:
        return math.degrees(math.atan2(self.b, 1.0 + self.ds))

    @property
    def shift_m(self) -> float:
        return math.hypot(self.tn, self.te)

    def to_dict(self) -> dict:
        return {"cn": self.cn, "ce": self.ce, "ds": self.ds, "b": self.b, "tn": self.tn, "te": self.te,
                "gain": self.gain, "links": self.links}

    @classmethod
    def from_dict(cls, d: dict) -> "Correction":
        return cls(**{k: d[k] for k in ("cn", "ce", "ds", "b", "tn", "te", "gain", "links") if k in d})


# ---------------------------------------------------------------------------
# 1. Features
# ---------------------------------------------------------------------------
_sift = None


def _detector(n_features: int):
    global _sift
    if _sift is None or _sift.getNFeatures() != n_features:
        _sift = cv2.SIFT_create(nfeatures=n_features)
    return _sift


def extract_features(image_bgr: np.ndarray, pixel_to_ne, cfg: AlignConfig) -> Features | None:
    """Distinct points of a photo with their ground positions.

    ``pixel_to_ne(px, py)`` gives the ground position (north, east) of a pixel under
    the photo's first placement, or None above the horizon.
    """
    h, w = image_bgr.shape[:2]
    scale = min(1.0, cfg.max_dim / max(h, w))
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY) if image_bgr.ndim == 3 else image_bgr
    if scale < 1.0:
        gray = cv2.resize(gray, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    # even out harsh light and shade so both sides of a seam yield the same points
    gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
    kps, desc = _detector(cfg.features).detectAndCompute(gray, None)
    if desc is None or len(kps) < cfg.min_inliers:
        return None
    soft = cv2.GaussianBlur(gray, (0, 0), 3)
    ne, keep, lum = [], [], []
    for i, kp in enumerate(kps):
        p = pixel_to_ne(kp.pt[0] / scale, kp.pt[1] / scale)
        if p is None:
            continue
        ne.append(p)
        keep.append(i)
        lum.append(float(soft[min(soft.shape[0] - 1, int(kp.pt[1])), min(soft.shape[1] - 1, int(kp.pt[0]))]))
    if len(keep) < cfg.min_inliers:
        return None
    return Features(np.asarray(ne, np.float32), np.clip(desc[keep], 0, 255).astype(np.uint8),
                    np.asarray(lum, np.float32))


# ---------------------------------------------------------------------------
# 2. Links between overlapping photos
# ---------------------------------------------------------------------------
def match_pair(a: int, fa: Features, b: int, fb: Features, baseline_m: float, cfg: AlignConfig,
               rng: np.random.Generator | None = None) -> Link | None:
    """Matched points between two photos, or None if they do not convincingly overlap."""
    knn = cv2.BFMatcher(cv2.NORM_L2).knnMatch(fa.desc.astype(np.float32), fb.desc.astype(np.float32), k=2)
    good = [m for m, n in (p for p in knn if len(p) == 2) if m.distance < cfg.ratio * n.distance]
    if len(good) < cfg.min_inliers:
        return None
    ia = np.array([m.queryIdx for m in good]); ib = np.array([m.trainIdx for m in good])
    pa, pb = fa.ne[ia].astype(np.float64), fb.ne[ib].astype(np.float64)
    M, inl = cv2.estimateAffinePartial2D(pa, pb, method=cv2.RANSAC, ransacReprojThreshold=cfg.ransac_m,
                                         maxIters=2000, confidence=0.995)
    if M is None or inl is None:
        return None
    inl = inl.ravel().astype(bool)
    n_in = int(inl.sum())
    if n_in < cfg.min_inliers or n_in < cfg.min_inlier_ratio * len(good):
        return None
    # Between two photos of the same ground the relation is close to a pure shift. A wrong
    # surface height shows as a shift along the line between the two cameras, never as a
    # large turn or a change of size; anything else is a false match.
    scale = math.hypot(M[0, 0], M[1, 0])
    turn = abs(math.degrees(math.atan2(M[1, 0], M[0, 0])))
    shift = math.hypot(*(M @ np.append(pa[inl].mean(axis=0), 1.0) - pa[inl].mean(axis=0)))
    if not (0.85 <= scale <= 1.18) or turn > cfg.max_pair_turn_deg or shift > cfg.max_height_ratio * baseline_m + 15.0:
        return None
    idx = np.flatnonzero(inl)
    if len(idx) > cfg.keep_per_link:
        idx = (rng or np.random.default_rng(a * 100003 + b)).choice(idx, cfg.keep_per_link, replace=False)
    return Link(a, b, pa[idx].astype(np.float32), pb[idx].astype(np.float32),
                float(fa.lum[ia[inl]].mean()), float(fb.lum[ib[inl]].mean()))


# ---------------------------------------------------------------------------
# 3. One correction per photo, solved together
# ---------------------------------------------------------------------------
def plausible_links(centres: dict, links: list[Link], cfg: AlignConfig) -> list[Link]:
    """Drop links that contradict what the GPS positions allow.

    If the visible surface is not at take-off level, every pair of overlapping photos
    disagrees by a shift along the line between the two cameras, and by the same
    fraction of that distance for every pair (it only depends on the surface height).
    That fraction is estimated as the median over all links. A link whose disagreement
    is far from it, or points somewhere else entirely, is a false match between photos
    that look alike, and one such link could otherwise drag the whole map.
    """
    if len(links) < 4:
        return list(links)
    shift, base = [], []
    for l in links:
        shift.append((l.pa.astype(np.float64) - l.pb.astype(np.float64)).mean(axis=0))
        base.append(np.subtract(centres[l.a], centres[l.b]))
    shift, base = np.array(shift), np.array(base)
    length = np.hypot(*base.T)
    ok = length > 5.0
    if ok.sum() < 3:
        return list(links)
    kappa = float(np.median((shift[ok] * base[ok]).sum(axis=1) / length[ok] ** 2))
    off = np.hypot(*(shift - kappa * base).T)
    return [l for l, d, n in zip(links, off, length) if d <= cfg.link_tolerance_m + cfg.link_tolerance_frac * n]


def solve(centres: dict[int, tuple[float, float]], links: list[Link], cfg: AlignConfig) -> dict[int, Correction]:
    """Corrections for every photo that has at least one trustworthy link.

    Guarded three ways, because a least-squares fit will happily satisfy bad matches by
    shrinking the whole map towards a point: links are first checked against the GPS
    (:func:`plausible_links`); links the solution cannot satisfy are dropped and the
    solve repeated; and a solution that moves photos much further than GPS error allows
    is thrown away altogether, leaving the photos as recorded.
    """
    links = plausible_links(centres, links, cfg)
    out = _solve(centres, links, cfg)
    if out:
        rms = np.array([_link_rms(l, out) for l in links])
        limit = max(cfg.max_link_residual_m, 4.0 * float(np.median(rms)))
        if (rms > limit).any():
            links = [l for l, e in zip(links, rms) if e <= limit]
            out = _solve(centres, links, cfg)
    if out and float(np.median([c.shift_m for c in out.values()])) > 2.5 * cfg.prior_shift_m:
        return {}
    if cfg.exposure and out:
        for i, gain in solve_gains(links, set(out)).items():
            out[i].gain = gain
    return out


def _link_rms(l: Link, corr: dict[int, Correction]) -> float:
    pa = corr[l.a].apply(l.pa) if l.a in corr else l.pa.astype(np.float64)
    pb = corr[l.b].apply(l.pb) if l.b in corr else l.pb.astype(np.float64)
    return float(np.sqrt(((pa - pb) ** 2).sum(axis=1).mean()))


def _solve(centres: dict[int, tuple[float, float]], links: list[Link], cfg: AlignConfig) -> dict[int, Correction]:
    """Least squares for one correction per photo.

    Unknowns per photo: (ds, b, tn, te). For a matched point seen at p_a in photo a and
    p_b in photo b the two corrected positions should coincide, which is linear in the
    unknowns. Each photo is also pulled gently towards "no correction": firmly for the
    shift (GPS is trustworthy to a few metres), loosely for scale (surface height is
    unknown). Two passes: the second down-weights matches the first left far apart.
    """
    ids = sorted({l.a for l in links} | {l.b for l in links})
    if not ids:
        return {}
    col = {i: 4 * k for k, i in enumerate(ids)}
    n = 4 * len(ids)
    w_match = 1.0 / cfg.match_sigma_m
    prior = np.tile([1.0 / cfg.prior_scale, 1.0 / math.radians(cfg.prior_turn_deg) ,
                     1.0 / cfg.prior_shift_m, 1.0 / cfg.prior_shift_m], len(ids)) ** 2
    x = np.zeros(n)
    count = {i: 0 for i in ids}
    for l in links:
        count[l.a] += 1
        count[l.b] += 1

    def rows(l: Link):
        """Jacobian (2K x 8) and constant (2K,) of the residual for one link."""
        qa = l.pa.astype(np.float64) - centres[l.a]
        qb = l.pb.astype(np.float64) - centres[l.b]
        k = len(qa)
        J = np.zeros((2 * k, 8))
        # north residual                       east residual
        J[0::2, 0], J[1::2, 0] = qa[:, 0], qa[:, 1]            # ds_a
        J[0::2, 1], J[1::2, 1] = -qa[:, 1], qa[:, 0]           # b_a
        J[0::2, 2] = 1.0; J[1::2, 3] = 1.0                     # shift_a
        J[0::2, 4], J[1::2, 4] = -qb[:, 0], -qb[:, 1]          # ds_b
        J[0::2, 5], J[1::2, 5] = qb[:, 1], -qb[:, 0]           # b_b
        J[0::2, 6] = -1.0; J[1::2, 7] = -1.0                   # shift_b
        r0 = (l.pa.astype(np.float64) - l.pb.astype(np.float64)).reshape(-1)
        return J, r0

    weights = [np.ones(2 * len(l.pa)) for l in links]
    for _ in range(2):
        A = np.diag(prior).astype(np.float64)
        g = np.zeros(n)
        cache = []
        for l, w in zip(links, weights):
            J, r0 = rows(l)
            cache.append((J, r0))
            Jw = J * (w * w_match)[:, None]
            rw = r0 * (w * w_match)
            sl = np.r_[col[l.a]:col[l.a] + 4, col[l.b]:col[l.b] + 4]
            A[np.ix_(sl, sl)] += Jw.T @ Jw
            g[sl] -= Jw.T @ rw
        x = np.linalg.solve(A, g)
        # Huber-style: matches still more than a couple of sigma apart count for less
        for i, (l, (J, r0)) in enumerate(zip(links, cache)):
            sl = np.r_[col[l.a]:col[l.a] + 4, col[l.b]:col[l.b] + 4]
            res = (J @ x[sl] + r0).reshape(-1, 2)
            d = np.hypot(res[:, 0], res[:, 1])
            w = np.where(d <= 2 * cfg.match_sigma_m, 1.0, np.sqrt(2 * cfg.match_sigma_m / np.maximum(d, 1e-9)))
            weights[i] = np.repeat(w, 2)

    out = {}
    for i in ids:
        ds, b, tn, te = x[col[i]:col[i] + 4]
        c = Correction(centres[i][0], centres[i][1], float(ds), float(b), float(tn), float(te), 1.0, count[i])
        # a correction this large means the matches were wrong, not the aircraft's record
        if not (cfg.min_scale <= c.scale <= cfg.max_scale) or abs(c.turn_deg) > cfg.max_turn_deg \
                or c.shift_m > cfg.max_shift_m:
            continue
        out[i] = c
    return out


def solve_gains(links: list[Link], ids: set[int], strength: float = 0.1) -> dict[int, float]:
    """A brightness multiplier per photo so that overlapping photos match."""
    ids = sorted(ids)
    if not ids:
        return {}
    col = {i: k for k, i in enumerate(ids)}
    A = np.eye(len(ids)) * strength          # pull towards "no change" so the average stays put
    g = np.zeros(len(ids))
    for l in links:
        if l.a not in col or l.b not in col or l.lum_a <= 1 or l.lum_b <= 1:
            continue
        d = math.log(l.lum_b / l.lum_a)       # want gain_a - gain_b = log(lum_b / lum_a)
        i, j = col[l.a], col[l.b]
        A[i, i] += 1; A[j, j] += 1; A[i, j] -= 1; A[j, i] -= 1
        g[i] += d; g[j] -= d
    logs = np.linalg.solve(A, g)
    return {i: float(np.clip(math.exp(logs[col[i]]), 0.7, 1.4)) for i in ids}


def residual_m(links: list[Link], corrections: dict[int, Correction]) -> float | None:
    """Median distance between matched points after correction (uncorrected photos as recorded)."""
    d = []
    for l in links:
        pa = corrections[l.a].apply(l.pa) if l.a in corrections else l.pa.astype(np.float64)
        pb = corrections[l.b].apply(l.pb) if l.b in corrections else l.pb.astype(np.float64)
        d.append(np.hypot(*(pa - pb).T))
    return float(np.median(np.concatenate(d))) if d else None
