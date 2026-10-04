"""Guesses taken from a downloaded file's own name: the version it carries, or what a different song is called.

Plain text rules (no model involved). Used by the review page to prefill its boxes and by the sync to
notice that a file may be a different mix than the Spotify title says.
"""
import re

_TRACK_NUMBER = re.compile(r"^\s*(?:[\(\[]?[a-zA-Z]?\d{1,4}[\)\]]?[\.\-_\s]+){1,2}")
_VERSION_WORD = re.compile(r"\b(mix|remix|edit|version|dub|rework|vip|instrumental|extended|radio|bootleg|club|remaster(?:ed)?"
                           r"|live|acoustic)\b", re.IGNORECASE)
_PLAIN_TAGS = {"original mix", "original version", "album version", "album mix"}


def _clean_stem(stem: str) -> str:
    """A file name without its track number, underscores or extension."""
    text = _TRACK_NUMBER.sub("", stem.replace("_", " "))
    return re.sub(r"\s+", " ", text).strip(" -")


def suggest_label(stem: str, spotify_title: str) -> str:
    """The version tag in the file's name that the Spotify title doesn't have, e.g.
    'Bob Sinclar - I Feel For You (CZR's Peak Hour Mix)' -> "CZR's Peak Hour Mix". Empty if there is none."""
    text, title = _clean_stem(stem), spotify_title.lower()
    for group in reversed(re.findall(r"[\(\[]([^\)\]]+)[\)\]]", text)):
        low = group.strip().lower()
        if _VERSION_WORD.search(group) and low not in _PLAIN_TAGS and low not in title:
            return group.strip()
    match = re.search(r"\s-\s([^-]*\b(?:mix|remix|edit|version|dub)\b[^-]*)$", text, re.IGNORECASE)
    if match and match.group(1).strip().lower() not in title:
        return match.group(1).strip()
    return ""


def suggest_other(stem: str) -> tuple[str, str]:
    """(artist, title) guessed from a file name: the number is dropped and 'Artist - Title' split.
    The artist is empty when the name has only a title."""
    text = _clean_stem(stem)
    parts = re.split(r"\s+-\s+", text, maxsplit=1)
    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else ("", text)


