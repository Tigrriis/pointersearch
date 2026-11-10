"""Poll a directory for new images and process them as they arrive.

Polling (rather than inotify) is deliberate: it works on SD-card mounts, SMB
shares, and the upload folder of a Cloud API media server alike. A file is only
processed once its size has been stable for one polling interval, so partially
written uploads are not read.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from .pipeline import IMAGE_SUFFIXES, Pipeline

log = logging.getLogger("pointersearch")


class Watcher:
    def __init__(self, pipeline: Pipeline, folder: str | Path):
        self.pipeline = pipeline
        self.folder = Path(folder)
        self._seen_sizes: dict[Path, int] = {}

    def poll(self) -> int:
        """One pass over the folder. Returns the number of images processed."""
        processed = 0
        for p in sorted(self.folder.rglob("*")):
            if p.suffix.lower() not in IMAGE_SUFFIXES or not p.is_file():
                continue
            size = p.stat().st_size
            if self._seen_sizes.get(p) != size:
                self._seen_sizes[p] = size      # wait one more cycle for it to settle
                continue
            try:
                if self.pipeline.process_image(p):
                    processed += 1
            except Exception as exc:  # keep the watcher alive through bad files
                log.exception("failed on %s: %s", p.name, exc)
        return processed

    def run(self, interval_s: float = 2.0):
        log.info("watching %s every %.1fs (Ctrl-C to stop)", self.folder, interval_s)
        while True:
            if self.poll():
                self.pipeline.align_mission()
            time.sleep(interval_s)


def watch(pipeline: Pipeline, folder: Path, interval_s: float = 2.0, once: bool = False) -> int:
    w = Watcher(pipeline, folder)
    if once:
        w.poll()                      # record sizes
        return w.poll()               # process files whose size has not changed
    w.run(interval_s)
    return 0
