"""Which files belong to the same shutter press. No heavy dependencies."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

MAX_SIBLING_GAP_S = 3.0
_DJI_NAME = re.compile(r"^DJI_(\d{14})_(\d{3,6})_([A-Z])$", re.IGNORECASE)
_SUFFIXED = re.compile(r"^(?P<base>.+)_(?P<lens>[WVZTSD])$", re.IGNORECASE)


def capture_key(path: str | Path) -> str | None:
    """Key shared by the files of one shutter press, or None for a lone file.

    ``DJI_20250101120000_0007_W.JPG`` and ``..._0007_T.JPG`` in the same folder
    share the folder and sequence number. The timestamp is compared separately
    (see :func:`same_moment`) because it can differ by a second between lenses.
    """
    p = Path(path)
    m = _DJI_NAME.match(p.stem)
    if m:
        return f"{p.parent}|seq{m.group(2)}"
    m = _SUFFIXED.match(p.stem)
    if m:
        return f"{p.parent}|{m.group('base').lower()}"
    return None


def _name_time(path: str | Path) -> datetime | None:
    m = _DJI_NAME.match(Path(path).stem)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def same_moment(a: str | Path, b: str | Path) -> bool:
    """Guard against sequence numbers repeating after a reset or on another day."""
    ta, tb = _name_time(a), _name_time(b)
    if ta is None or tb is None:
        return True
    return abs((ta - tb).total_seconds()) <= MAX_SIBLING_GAP_S
