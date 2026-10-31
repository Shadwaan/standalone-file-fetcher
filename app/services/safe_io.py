"""Two guards against a full disk, which once left sff's record files empty and its conversions failing.

write_text_atomic: write the new content beside the file, then swap it in. If the disk fills up
halfway, the old file is still there untouched (a plain write_text truncates it first).

wait_for_space: pause (instead of failing track after track) while the drive is nearly full,
and carry on by itself once space has been freed.
"""
import logging
import os
import shutil
import time
from pathlib import Path

logger = logging.getLogger(__name__)

MIN_FREE_GB = 15.0          # below this, downloads and conversions wait
CHECK_EVERY_SECONDS = 60


def write_text_atomic(path, text: str, encoding: str = "utf-8") -> None:
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding=encoding)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def free_gb(path) -> float:
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    return shutil.disk_usage(p).free / 1e9


def wait_for_space(path, min_free_gb: float = MIN_FREE_GB, on_progress=None, sleep=time.sleep,
                   max_wait: float = 12 * 3600) -> bool:
    """Block while `path`'s drive has less than `min_free_gb` free. False if it never freed up."""
    deadline = time.time() + max_wait
    warned = False
    while True:
        try:
            free = free_gb(path)
        except OSError:
            return True                                    # can't tell: don't hold the sync up
        if free >= min_free_gb:
            if warned:
                logger.info("disk space is back (%.0f GB free): carrying on", free)
            return True
        if not warned:
            logger.warning("only %.1f GB free on %s: pausing until at least %.0f GB is free", free, path, min_free_gb)
            warned = True
        if on_progress:
            on_progress(f"Paused: only {free:.0f} GB free on the music drive. Free up space and sff carries on by itself")
        if time.time() >= deadline:
            return False
        sleep(CHECK_EVERY_SECONDS)
