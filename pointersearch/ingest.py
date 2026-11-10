"""Background image import for the web app.

One job per mission: process every JPEG under a folder, then optionally keep
watching that folder for new files. The job runs in its own thread with its own
database connection, so the review map stays responsive and updates as images
land.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from .pipeline import IMAGE_SUFFIXES, Pipeline
from .watch import Watcher


class IngestError(ValueError):
    pass


class IngestJob:
    def __init__(self, workdir: str | Path):
        self.workdir = Path(workdir)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._s = {"state": "idle", "folder": None, "total": 0, "done": 0, "added": 0,
                   "skipped": 0, "current": None, "message": None, "watch": False}

    # -- public ---------------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            return dict(self._s)

    def start(self, folder: str, watch: bool = False, force: bool = False) -> dict:
        path = Path(folder.strip().strip('"')).expanduser()
        if not folder.strip():
            raise IngestError("enter the folder that holds the images")
        if not path.exists():
            raise IngestError(f"folder not found: {path}")
        if not path.is_dir():
            raise IngestError(f"not a folder: {path}")
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise IngestError("an import is already running for this mission")
            self._stop.clear()
            self._s.update(state="running", folder=str(path), total=0, done=0, added=0, skipped=0,
                           current=None, message=None, watch=bool(watch))
            self._thread = threading.Thread(target=self._run, args=(path, bool(watch), bool(force)), daemon=True,
                                            name=f"ingest-{self.workdir.name}")
            self._thread.start()
        return self.status()

    def start_files(self, files: list[str], label: str = "images already in the mission") -> dict:
        """Search again a given list of images that are already in the mission."""
        paths = [Path(f) for f in files]
        if not paths:
            raise IngestError("there is nothing to search again")
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise IngestError("an import is already running for this mission")
            self._stop.clear()
            self._s.update(state="running", folder=label, total=len(paths), done=0, added=0, skipped=0,
                           current=None, message=None, watch=False)
            self._thread = threading.Thread(target=self._run, args=(None, False, True, paths), daemon=True,
                                            name=f"ingest-{self.workdir.name}")
            self._thread.start()
        return self.status()

    def stop(self) -> dict:
        self._stop.set()
        return self.status()

    def join(self, timeout: float | None = None):
        t = self._thread
        if t is not None:
            t.join(timeout)

    # -- worker ---------------------------------------------------------------
    def _set(self, **kw):
        with self._lock:
            self._s.update(kw)

    def _run(self, folder: Path | None, watch: bool, force: bool = False, files: list[Path] | None = None):
        pipeline = None
        try:
            pipeline = Pipeline(self.workdir)
            missing = 0
            if files is None:
                files = sorted(p for p in folder.rglob("*") if p.suffix.lower() in IMAGE_SUFFIXES and p.is_file())
            else:
                missing = sum(1 for p in files if not p.is_file())
                files = [p for p in files if p.is_file()]
            self._set(total=len(files))
            added = skipped = 0
            for i, f in enumerate(files):
                if self._stop.is_set():
                    self._set(state="stopped", current=None, message=f"stopped after {i} of {len(files)} images")
                    return
                self._set(current=f.name)
                try:
                    if pipeline.process_image(f, force=force):
                        added += 1
                    else:
                        skipped += 1
                except Exception as exc:                 # one bad file must not end the import
                    skipped += 1
                    self._set(message=f"{f.name}: {exc}")
                self._set(done=i + 1, added=added, skipped=skipped)
            if added:
                self._set(state="aligning", current=None, message="lining the photos up on the map")
                pipeline.align_mission()
            if not watch:
                msg = (f"{added} image(s) {'processed again' if force else 'added'}"
                       + (f", {skipped} already imported or unusable" if skipped else ""))
                if missing:
                    msg += (f"; {missing} photo file(s) were not found, is the card or folder connected "
                            "at the same place as before?")
                if not files and not missing:
                    msg = "no JPEG images found in that folder"
                self._set(state="done", current=None, message=msg)
                return
            self._set(state="watching", current=None,
                      message=f"{added} image(s) added; watching for new images")
            watcher = Watcher(pipeline, folder)
            while not self._stop.wait(2.0):
                n = watcher.poll()
                if n:
                    pipeline.align_mission()
                    added += n
                    self._set(added=added, message=f"{added} image(s) added; watching for new images")
            self._set(state="stopped", message=f"stopped watching; {added} image(s) added")
        except Exception as exc:
            self._set(state="error", current=None, message=str(exc) or exc.__class__.__name__)
        finally:
            if pipeline is not None:
                pipeline.store.close()


# -- native folder picker -----------------------------------------------------
_PICKER = (
    "import tkinter as tk\n"
    "from tkinter import filedialog\n"
    "r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True)\n"
    "p = filedialog.askdirectory(title='Choose the folder of drone images')\n"
    "print(p or '')\n"
)


def pick_folder(timeout_s: float = 600.0) -> str | None:
    """Open the operating system's folder dialog on the machine running the app.

    Returns the chosen path, "" if cancelled, or None when no dialog is available
    (no display, or Python without tkinter). Runs in a subprocess so the dialog
    has its own main thread.
    """
    import subprocess
    import sys
    try:
        r = subprocess.run([sys.executable, "-c", _PICKER], capture_output=True, text=True, timeout=timeout_s)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip()
