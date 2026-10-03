"""Checks on the audio of a finished download, for mistakes a filename can hide.

A file can carry the right name and still be the wrong thing: another song entirely, or just the
vocal stem. Both have turned up in real syncs ("Pleasure Love" was a different song; "Time After
Time" was an acapella whose filename had the word spelled "Accapella"). Names are checked when a
source is chosen (soulseek.passes_version_guard); these look at the sound once it has arrived.
"""
import logging
import subprocess

import numpy as np

logger = logging.getLogger(__name__)

# Calibrated on 30 real downloads compared with the YouTube MP3 already in the library for the
# same Spotify track: 24 matched at 0.98 or better; the wrong songs scored 0.49, 0.51 and 0.59; a
# vocals-only stem scored 0.81 (it keeps the tune, loses the rest) and is caught by looks_like_stem.
SAME_SONG_REJECT_BELOW = 0.70
SAME_SONG_WARN_BELOW = 0.93
# A vocals-only track has next to no energy at 40-100 Hz (kick and bass): measured as that
# band's share of the signal it is ~0.00-0.01, against a median of several tenths for real tracks.
STEM_BASS_SHARE_BELOW = 0.02


def _duration(path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def _decode(path, seconds: float, af: str = "") -> np.ndarray:
    """Mono 22.05 kHz samples from a window in the middle of the file."""
    start = max(0.0, _duration(path) / 2 - seconds / 2)
    cmd = ["ffmpeg", "-v", "error", "-ss", str(start), "-t", str(seconds), "-i", str(path), "-map", "0:a:0", "-ac", "1",
           "-ar", "22050"] + (["-af", af] if af else []) + ["-f", "f32le", "-"]
    return np.frombuffer(subprocess.run(cmd, capture_output=True, timeout=300).stdout, dtype=np.float32)


def chroma_profile(path, seconds: float = 150):
    """The track's average pitch-class profile (12 numbers: how much of each musical note it contains)."""
    x = _decode(path, seconds)
    n = 8192
    if len(x) < n * 4:
        return None
    starts = np.arange(0, len(x) - n, n // 2)
    spec = np.abs(np.fft.rfft(np.stack([x[s:s + n] * np.hanning(n) for s in starts]), axis=1))
    freqs = np.fft.rfftfreq(n, 1 / 22050)
    band = (freqs > 65) & (freqs < 2100)
    note = np.round(12 * np.log2(freqs[band] / 440.0)).astype(int) % 12
    power = spec[:, band] ** 2
    chroma = np.array([power[:, note == k].sum() for k in range(12)])
    return chroma / (chroma.sum() + 1e-12)


def chroma_similarity(a, b) -> float:
    """1.0 = the same music (an edit or longer mix of one song stays near 1.0); a different song is far lower."""
    a, b = a - a.mean(), b - b.mean()
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-12))


def bass_share(path):
    """Share of the signal that is 40-100 Hz, or None if the file is silent/too short."""
    bass = "highpass=f=40:poles=2,lowpass=f=100:poles=2,highpass=f=40:poles=2,lowpass=f=100:poles=2"
    full, low = _decode(path, 100), _decode(path, 100, bass)
    if len(full) < 22050 * 10:
        return None
    full_rms = float(np.sqrt((full ** 2).mean()))
    return None if full_rms < 1e-5 else float(np.sqrt((low ** 2).mean())) / full_rms


def looks_like_stem(path) -> bool:
    """True for a vocals-only (or other bass-less) file."""
    share = bass_share(path)
    return share is not None and share < STEM_BASS_SHARE_BELOW


def compare(path, reference):
    """Similarity of `path` to a file known to be the right song, or None if either can't be read."""
    a, b = chroma_profile(path), chroma_profile(reference)
    return None if a is None or b is None else chroma_similarity(a, b)
