"""
Output formats (FLAC / AIFF / WAV) and the conversion that produces them.

Every file sff hands to Rekordbox from Soulseek ends up as 16-bit PCM at 44.1 or
48 kHz, in whichever container(s) the user picked -- the range CDJs and standalone
players actually play. Sample rates are never resampled unless they have to be:
44.1k/48k are kept as-is, and hi-res rates go to the nearest rate at an exact
integer ratio (88.2k/176.4k -> 44.1k, 96k/192k -> 48k), which resamples cleanly.

One downloaded file can feed several outputs; a format added to a playlist later
can be derived from a file that's already there, without downloading again.
Originals are always archived, never deleted.
"""

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

DJ_SAMPLE_RATES = (44100, 48000)
LOSSLESS_EXTS = {".flac", ".wav", ".aif", ".aiff"}


@dataclass(frozen=True)
class OutputFormat:
    key: str        # what's stored in config/state
    label: str      # what's shown in the UI and used as the playlist suffix
    ext: str        # extension without the dot
    codec: str      # ffmpeg audio codec that produces a 16-bit file
    muxer: str      # ffmpeg container


# AIFF is big-endian PCM on purpose: Pioneer decks only play plain uncompressed
# AIFF, not AIFF-C (which is where little-endian "sowt" PCM lives).
FORMATS: dict[str, OutputFormat] = {
    "flac": OutputFormat("flac", "FLAC", "flac", "flac", "flac"),
    "aiff": OutputFormat("aiff", "AIFF", "aiff", "pcm_s16be", "aiff"),
    "wav": OutputFormat("wav", "WAV", "wav", "pcm_s16le", "wav"),
}
FORMAT_ORDER = list(FORMATS)


def target_sample_rate(rate: int | None) -> int:
    if rate in DJ_SAMPLE_RATES:
        return rate
    if rate in (88200, 176400):
        return 44100
    if rate in (96000, 192000):
        return 48000
    return 44100


def probe(path: Path) -> tuple[str | None, int | None, int | None]:
    """(codec, bits per sample, sample rate) read from the file itself."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=codec_name,bits_per_raw_sample,bits_per_sample,sample_rate",
         "-of", "default=noprint_wrappers=1", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    codec = raw = per = rate = None
    for line in out.stdout.splitlines():
        key, _, v = line.partition("=")
        if key == "codec_name":
            codec = v
        elif key == "bits_per_raw_sample":
            raw = int(v) if v.isdigit() else None
        elif key == "bits_per_sample":
            per = int(v) if v.isdigit() else None
        elif key == "sample_rate":
            rate = int(v) if v.isdigit() else None
    # FLAC reports its depth as "raw", PCM as plain bits-per-sample (raw is N/A)
    return codec, (raw or per or None), rate


def is_valid_lossless(path: Path) -> bool:
    """Really lossless audio: FLAC or uncompressed PCM. Rules out a WAV that's
    secretly ADPCM/MP3-in-WAV, or a file that isn't audio at all."""
    codec, _, _ = probe(path)
    return bool(codec) and (codec == "flac" or codec.startswith("pcm_"))


def is_ready(path: Path, fmt: str) -> bool:
    """Already exactly what `fmt` means: right container+codec, 16-bit, 44.1/48k."""
    f = FORMATS[fmt]
    if path.suffix.lower().lstrip(".") not in {f.ext, "aif" if fmt == "aiff" else f.ext}:
        return False
    codec, bits, rate = probe(path)
    return codec == f.codec and bits == 16 and rate in DJ_SAMPLE_RATES


def convert(src: Path, dst: Path, fmt: str) -> bool:
    """Convert src -> dst as 16-bit `fmt` at 44.1/48k. Atomic: writes a temp file,
    renames on success, leaves nothing behind on failure."""
    f = FORMATS[fmt]
    codec, bits, rate = probe(src)
    already_16bit_pcm = bits == 16 and rate in DJ_SAMPLE_RATES and codec is not None and (
        codec == "flac" or codec.startswith("pcm_s16"))

    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-map", "0:a:0", "-map_metadata", "-1"]
    if not already_16bit_pcm:
        # soxr resampling + triangular dither: dropping to 16-bit without dither adds
        # audible quantisation distortion on quiet passages. Skipped when the source
        # is already 16-bit at a good rate, so those conversions stay bit-exact.
        cmd += ["-af", f"aresample=resampler=soxr:osf=s16:osr={target_sample_rate(rate)}:dither_method=triangular"]
    tmp = dst.with_name(dst.name + ".converting")
    cmd += ["-c:a", f.codec, "-f", f.muxer, str(tmp)]

    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        tmp.unlink(missing_ok=True)
        logger.warning("Conversion of %s to %s failed: %s", src.name, fmt, result.stderr[-300:])
        return False
    tmp.replace(dst)
    return True


def unique_path(folder: Path, stem: str, ext: str) -> Path:
    """folder/stem.ext, or stem_1.ext, stem_2.ext... if that name is taken."""
    dest = folder / f"{stem}.{ext}"
    n = 1
    while dest.exists():
        dest = folder / f"{stem}_{n}.{ext}"
        n += 1
    return dest


def _unique(folder: Path, stem: str, fmt: str) -> Path:
    return unique_path(folder, stem, FORMATS[fmt].ext)


def build_outputs(
    src: Path,
    formats: list[str],
    dest_dir_for: Callable[[str], Path],
    originals_dir: Path,
    keep_source: bool = False,
) -> dict[str, Path]:
    """Produce one output file per requested format from `src`.

    - A source that already IS one of the requested formats (16-bit, 44.1/48k) is
      moved into place rather than copied, so it isn't stored twice.
    - The others are converted from it.
    - Anything not used as an output is archived in `originals_dir`, never deleted.
    - keep_source=True is for a file that must stay where it is (a track already in
      another format's playlist): it's only ever converted from, never moved.

    Returns {format: path} for every output that was produced; a format missing
    from the result means its conversion failed.
    """
    outputs: dict[str, Path] = {}
    base = src

    if not keep_source:
        for fmt in formats:
            if is_ready(src, fmt):
                dest_dir = dest_dir_for(fmt)
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = _unique(dest_dir, src.stem, fmt)
                shutil.move(str(src), str(dest))
                outputs[fmt], base = dest, dest
                break

    for fmt in formats:
        if fmt in outputs:
            continue
        dest_dir = dest_dir_for(fmt)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = _unique(dest_dir, src.stem, fmt)
        if convert(base, dest, fmt):
            outputs[fmt] = dest

    if not keep_source and base == src and outputs and src.exists():
        originals_dir.mkdir(parents=True, exist_ok=True)
        archived = originals_dir / src.name
        n = 1
        while archived.exists():
            archived = originals_dir / f"{src.stem}_{n}{src.suffix}"
            n += 1
        shutil.move(str(src), str(archived))
        logger.info("Archived original %s -> %s", src.name, archived)

    return outputs
