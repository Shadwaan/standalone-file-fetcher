"""The shopping list: tracks Soulseek could not supply, so they can be bought (Bandcamp, Beatport) and replace
whatever stands in for them.

Built from sff's own records: the persistent failure log (what Soulseek could not find, and in how many runs) and the
sync state (which playlists hold a YouTube stand-in for a track). Nothing here is written back anywhere.
"""
import csv
import io
from urllib.parse import quote_plus

# how sure we are that Soulseek will never have it
NOT_ON_SOULSEEK = "Not on Soulseek"
BAD_COPIES_ONLY = "Only bad copies found"
UNSURE = "Tried only once or twice"

_ORDER = {NOT_ON_SOULSEEK: 0, BAD_COPIES_ONLY: 1, UNSURE: 2}


def _group(rec: dict) -> str:
    category, runs = rec.get("category", ""), int(rec.get("times_failed") or 1)
    if category in ("no results", "no match"):
        return NOT_ON_SOULSEEK if runs >= 3 else UNSURE
    if "rejected: a fake" in (rec.get("detail") or "") or category == "wrong quality / dead source":
        return BAD_COPIES_ONLY if runs >= 3 else UNSURE
    return UNSURE


def links(artist: str, title: str) -> dict:
    q = quote_plus(f"{artist.split(',')[0].strip()} {title}")
    return {"bandcamp": f"https://bandcamp.com/search?q={q}&item_type=t",
            "beatport": f"https://www.beatport.com/search?q={q}"}


def youtube_stand_ins(state: dict) -> dict[str, dict]:
    """{spotify_id: {"playlists": [...], "label": ...}} for every track that currently has a YouTube copy standing in."""
    out: dict[str, dict] = {}
    for pl in (state.get("playlists") or {}).values():
        for v in (pl.get("variants") or {}).values():
            for sid, rec in (v.get("tracks") or {}).items():
                if rec.get("stand_in") and (rec.get("source") or {}).get("how") == "youtube":
                    entry = out.setdefault(sid, {"playlists": [], "label": rec.get("stand_in_label", "")})
                    name = v.get("display_name") or ""
                    if name and name not in entry["playlists"]:
                        entry["playlists"].append(name)
    return out


def build_list(state: dict, failures: list[dict]) -> list[dict]:
    """One row per track and playlist, most certain first."""
    yt = youtube_stand_ins(state)
    rows = []
    for rec in failures:
        sid = rec.get("spotify_id", "")
        have = yt.get(sid)
        rows.append({
            "spotify_id": sid, "playlist": rec.get("playlist", ""), "artist": rec.get("artist", ""), "title": rec.get("title", ""),
            "group": _group(rec), "runs_failed": int(rec.get("times_failed") or 1), "last_tried": (rec.get("last_failed") or "")[:10],
            "what_happened": rec.get("detail", ""),
            "youtube_copy": have["label"] if have else "",
            **links(rec.get("artist", ""), rec.get("title", "")),
        })
    rows.sort(key=lambda r: (_ORDER[r["group"]], r["playlist"].lower(), r["artist"].lower(), r["title"].lower()))
    return rows


def as_csv(rows: list[dict]) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["playlist", "artist", "title", "group", "runs failed", "last tried", "youtube copy in playlist", "bandcamp", "beatport"])
    for r in rows:
        w.writerow([r["playlist"], r["artist"], r["title"], r["group"], r["runs_failed"], r["last_tried"],
                    r["youtube_copy"], r["bandcamp"], r["beatport"]])
    return out.getvalue()
