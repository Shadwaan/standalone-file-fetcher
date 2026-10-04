"""Sources whose file turned out to be wrong.

When a downloaded file is rejected (the review page: "New file is wrong"), the Soulseek source it came
from is remembered, so a later sync cannot pick the same file from the same person again even after
Nicotine+'s own transfer history is cleared. Only sources sff recorded can be remembered; for older
downloads the sound check against the copy already in the library has to catch a repeat.
"""
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

FILE = Path(__file__).resolve().parent.parent / "rejected_sources.json"


def _load() -> list[list[str]]:
    try:
        return json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def add(user: str, remote_path: str) -> None:
    if not user or not remote_path:
        return
    items = _load()
    if [user, remote_path] not in items:
        items.append([user, remote_path])
        try:
            FILE.write_text(json.dumps(items, indent=1, ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            logger.warning("Could not save the rejected-sources record: %s", e)


def all_sources() -> set[tuple[str, str]]:
    return {(u, p) for u, p in _load()}
