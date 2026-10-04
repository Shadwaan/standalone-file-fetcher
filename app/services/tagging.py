"""
Tags and cover art for files that come from Soulseek (FLAC, AIFF, WAV).

Files from random Soulseek peers arrive with inconsistent tags, often no cover
(or the wrong one), and leftovers from other people's DJ software (Serato/Traktor
cue-point blobs, MusicBrainz IDs, ad URLs...). Rekordbox reads artist, album and
cover art from the tags when it analyses a track, so every file gets the same
clean set the YouTube/MP3 path produces:

  from Spotify:  title, artist, album, year, cover art
  from the file: genre, label, ISRC, album artist, track and disc number

Everything else the file happens to carry is left alone in FLACs and dropped in
AIFFs (which are built fresh from a converted copy).
"""

import logging
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

_ART_CACHE: dict[str, bytes | None] = {}

# normalised name -> tag keys it may appear under in the wild (first hit wins)
_CARRY_KEYS = {
    "genre": ("genre",),
    "label": ("label", "publisher", "organization"),
    "isrc": ("isrc",),
    "albumartist": ("albumartist", "album artist", "album_artist"),
    "tracknumber": ("tracknumber", "track"),
    "discnumber": ("discnumber", "disc"),
}
_ID3_CARRY = {"genre": "TCON", "label": "TPUB", "isrc": "TSRC",
              "albumartist": "TPE2", "tracknumber": "TRCK", "discnumber": "TPOS"}


def fetch_artwork(url: str | None) -> bytes | None:
    """Download a cover image (cached per URL). Returns None on any failure --
    a missing cover must never fail a sync."""
    if not url:
        return None
    if url in _ART_CACHE:
        return _ART_CACHE[url]
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.content
    except Exception as e:
        logger.warning("Could not fetch artwork %s: %s", url, e)
        data = None
    _ART_CACHE[url] = data
    return data


def _mime(data: bytes) -> str:
    return "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"


def embedded_picture(path: Path) -> bytes | None:
    """A cover already embedded in the file, if any (fallback when Spotify has none)."""
    try:
        if path.suffix.lower() == ".flac":
            from mutagen.flac import FLAC
            pics = FLAC(str(path)).pictures
            return pics[0].data if pics else None
        import mutagen
        tags = mutagen.File(str(path)).tags
        if tags:
            for key in tags.keys():
                if key.startswith("APIC"):
                    return tags[key].data
    except Exception:
        pass
    return None


def read_carry_over_tags(path: Path) -> dict[str, str]:
    """The small set of useful fields worth keeping from a downloaded file."""
    out: dict[str, str] = {}
    try:
        if path.suffix.lower() == ".flac":
            from mutagen.flac import FLAC
            raw = {k.lower(): v for k, v in FLAC(str(path)).items()}
            for name, keys in _CARRY_KEYS.items():
                for k in keys:
                    v = raw.get(k)
                    v = v[0] if isinstance(v, list) and v else v
                    if v and str(v).strip():
                        out[name] = str(v).strip()
                        break
        else:
            import mutagen
            tags = mutagen.File(str(path)).tags
            if tags:
                for name, frame in _ID3_CARRY.items():
                    if frame in tags and tags[frame].text:
                        v = str(tags[frame].text[0]).strip()
                        if v:
                            out[name] = v
    except Exception as e:
        logger.warning("Could not read tags from %s: %s", path.name, e)
    return out


def _year(track) -> str:
    y = str(getattr(track, "year", "") or "").strip()
    return y[:4] if y[:4].isdigit() else ""


def _write_flac(path: Path, track, artwork: bytes | None, carry: dict) -> None:
    from mutagen.flac import FLAC, Picture
    f = FLAC(str(path))
    f["title"] = [track.title]
    f["artist"] = [track.artist]
    if track.album:
        f["album"] = [track.album]
    if _year(track):
        f["date"] = [_year(track)]
    for name, value in carry.items():
        f[name] = [value]
    if artwork:
        f.clear_pictures()
        pic = Picture()
        pic.type, pic.mime, pic.desc, pic.data = 3, _mime(artwork), "Cover", artwork
        f.add_picture(pic)
    f.save()


def _write_id3_audio(path: Path, track, artwork: bytes | None, carry: dict, opener) -> None:
    """AIFF and WAV both keep an ID3 tag in a chunk of the file; only the mutagen
    class that opens them differs."""
    from mutagen import id3

    a = opener(str(path))
    if a.tags is not None:
        a.delete()                     # start from a clean ID3 chunk
        a = opener(str(path))
    a.add_tags()
    t = a.tags
    utf16 = 1                          # ID3v2.3 allows Latin-1 or UTF-16 only
    t.add(id3.TIT2(encoding=utf16, text=track.title))
    t.add(id3.TPE1(encoding=utf16, text=track.artist))
    if track.album:
        t.add(id3.TALB(encoding=utf16, text=track.album))
    if _year(track):
        t.add(id3.TDRC(encoding=utf16, text=_year(track)))
    frames = {"albumartist": id3.TPE2, "tracknumber": id3.TRCK, "discnumber": id3.TPOS,
              "genre": id3.TCON, "label": id3.TPUB, "isrc": id3.TSRC}
    for name, cls in frames.items():
        if name in carry:
            t.add(cls(encoding=utf16, text=carry[name]))
    if artwork:
        t.add(id3.APIC(encoding=utf16, mime=_mime(artwork), type=3, desc="Cover", data=artwork))
    a.save(v2_version=3)               # v2.3: what Rekordbox and Pioneer players read most reliably


def _write_aiff(path: Path, track, artwork: bytes | None, carry: dict) -> None:
    from mutagen.aiff import AIFF
    _write_id3_audio(path, track, artwork, carry, AIFF)


def _write_wav(path: Path, track, artwork: bytes | None, carry: dict) -> None:
    from mutagen.wave import WAVE
    _write_id3_audio(path, track, artwork, carry, WAVE)


def _write_mp3(path: Path, track, artwork: bytes | None, carry: dict) -> None:
    from mutagen.mp3 import MP3
    _write_id3_audio(path, track, artwork, carry, MP3)


def set_title(path: Path, title: str) -> bool:
    """Change only the title tag of a file (FLAC, AIFF, WAV or MP3). False if it could not be done."""
    try:
        ext = path.suffix.lower()
        if ext == ".flac":
            from mutagen.flac import FLAC
            f = FLAC(str(path))
            f["title"] = [title]
            f.save()
        elif ext in (".aiff", ".aif", ".wav", ".mp3"):
            from mutagen import id3
            if ext == ".mp3":
                from mutagen.mp3 import MP3 as opener
            elif ext == ".wav":
                from mutagen.wave import WAVE as opener
            else:
                from mutagen.aiff import AIFF as opener
            f = opener(str(path))
            if f.tags is None:
                f.add_tags()
            f.tags.setall("TIT2", [id3.TIT2(encoding=1, text=title)])
            f.save(v2_version=3)
        else:
            return False
        return True
    except Exception as e:
        logger.warning("Could not retitle %s: %s", path.name, e)
        return False


def write_tags(path: Path, track, artwork: bytes | None = None, carry: dict | None = None) -> bool:
    """Write the clean tag set + cover into a FLAC, AIFF, WAV or MP3. Returns False (and
    logs) instead of raising: bad tags shouldn't fail a download."""
    carry = carry if carry is not None else read_carry_over_tags(path)
    if artwork is None:
        artwork = fetch_artwork(getattr(track, "artwork_url", None)) or embedded_picture(path)
    try:
        ext = path.suffix.lower()
        if ext == ".flac":
            _write_flac(path, track, artwork, carry)
        elif ext in (".aiff", ".aif"):
            _write_aiff(path, track, artwork, carry)
        elif ext == ".wav":
            _write_wav(path, track, artwork, carry)
        elif ext == ".mp3":
            _write_mp3(path, track, artwork, carry)
        else:
            logger.info("No tag writer for %s files, skipping %s", ext, path.name)
            return False
        return True
    except Exception as e:
        logger.warning("Could not write tags to %s: %s", path.name, e)
        return False
