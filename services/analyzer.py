"""
Audio analysis engine.

Detects BPM, musical key, generates beat grid and RGB frequency waveform.
Uses librosa at 44100Hz for beat precision.
"""

import logging
from pathlib import Path

import numpy as np

from models.track import AnalysisResult

logger = logging.getLogger(__name__)

# Lazy import librosa (heavy dependency)
_librosa = None


def _get_librosa():
    global _librosa
    if _librosa is None:
        import librosa
        _librosa = librosa
    return _librosa


# Krumhansl-Kessler key profiles
MAJOR_PROFILE = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
MINOR_PROFILE = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]

# Pitch class names (librosa chroma order)
PITCH_CLASS_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# Camelot key table: (camelot, open_key, musical_key)
KEY_TABLE = [
    ("1A", "1m", "Ab minor"), ("2A", "2m", "Eb minor"), ("3A", "3m", "Bb minor"),
    ("4A", "4m", "F minor"), ("5A", "5m", "C minor"), ("6A", "6m", "G minor"),
    ("7A", "7m", "D minor"), ("8A", "8m", "A minor"), ("9A", "9m", "E minor"),
    ("10A", "10m", "B minor"), ("11A", "11m", "F# minor"), ("12A", "12m", "Db minor"),
    ("1B", "1d", "B major"), ("2B", "2d", "F# major"), ("3B", "3d", "Db major"),
    ("4B", "4d", "Ab major"), ("5B", "5d", "Eb major"), ("6B", "6d", "Bb major"),
    ("7B", "7d", "F major"), ("8B", "8d", "C major"), ("9B", "9d", "G major"),
    ("10B", "10d", "D major"), ("11B", "11d", "A major"), ("12B", "12d", "E major"),
]

# Build lookup dicts
_by_camelot = {}
_by_musical_short = {}

for _cam, _ok, _mus in KEY_TABLE:
    _by_camelot[_cam.upper()] = {"camelot": _cam, "open_key": _ok, "musical": _mus}
    parts = _mus.split()
    short = parts[0] + "m" if parts[1] == "minor" else parts[0]
    _by_musical_short[short.lower()] = _cam


def to_camelot(key_str: str) -> str:
    """Convert musical key notation to Camelot (e.g. 'C#m' -> '12A')."""
    if not key_str:
        return ""
    kl = key_str.strip().lower()
    ku = key_str.strip().upper()
    if ku in _by_camelot:
        return ku
    if kl in _by_musical_short:
        return _by_musical_short[kl]
    return ""


def to_open_key(key_str: str) -> str:
    """Convert any key format to Open Key notation (e.g. '5A' -> '5m')."""
    camelot = to_camelot(key_str)
    if camelot and camelot in _by_camelot:
        return _by_camelot[camelot]["open_key"]
    return ""


def analyze_track(file_path: str, target_waveform_width: int = 1600) -> AnalysisResult:
    """
    Full analysis: BPM, key, beat grid, waveform.
    CPU-intensive — takes 5-30 seconds depending on track length.
    """
    librosa = _get_librosa()
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Audio file not found: {file_path}")

    logger.info("Analyzing: %s", path.name)

    # Load at 44100Hz for beat precision
    y, sr = librosa.load(str(path), sr=44100, mono=True)
    duration_s = len(y) / sr

    result = AnalysisResult(duration_s=duration_s, sample_rate=sr)

    # --- BPM + Beat Grid ---
    logger.info("  Detecting beats...")

    hop_length = 256  # ~5.8ms resolution at 44100Hz
    onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop_length)

    tempo_estimate = librosa.feature.tempo(onset_envelope=onset_env, sr=sr, hop_length=hop_length)
    init_tempo = float(tempo_estimate[0]) if hasattr(tempo_estimate, '__len__') else float(tempo_estimate)

    tempo, beat_frames = librosa.beat.beat_track(
        onset_envelope=onset_env, sr=sr, hop_length=hop_length,
        start_bpm=init_tempo, units="frames",
        tightness=200,
    )

    beat_times = librosa.frames_to_time(beat_frames, sr=sr, hop_length=hop_length)

    # Onset snapping: snap beats to nearest onset within 50ms
    onset_frames = librosa.onset.onset_detect(y=y, sr=sr, hop_length=hop_length, backtrack=True)
    onset_times = librosa.frames_to_time(onset_frames, sr=sr, hop_length=hop_length)

    if len(onset_times) > 0 and len(beat_times) > 0:
        snapped = []
        for bt in beat_times:
            dists = np.abs(onset_times - bt)
            nearest_idx = np.argmin(dists)
            if dists[nearest_idx] < 0.05:
                snapped.append(onset_times[nearest_idx])
            else:
                snapped.append(bt)
        beat_times = np.array(snapped)

    # BPM refinement via linear regression
    if len(beat_times) >= 8:
        indices = np.arange(len(beat_times))
        coeffs = np.polyfit(indices, beat_times, 1)
        interval = coeffs[0]
        result.bpm = round(60.0 / interval, 2)

        # Find the first STRONG kick/onset to anchor the grid
        # Use low-frequency onset strength (kick detection)
        kick_anchor = _find_kick_anchor(y, sr, librosa, beat_times, onset_times, interval)

        # Rebuild clean grid anchored to the first real kick
        clean_times = []
        t = kick_anchor
        while t < duration_s:
            clean_times.append(round(t, 4))
            t += interval
        # Also extend backwards if kick_anchor is after the first beat
        t = kick_anchor - interval
        while t >= 0:
            clean_times.insert(0, round(t, 4))
            t -= interval
        beat_times = np.array(clean_times)
    else:
        result.bpm = float(tempo) if np.isscalar(tempo) else float(tempo[0])

    result.beat_grid = beat_times.tolist()

    # --- Downbeat Detection ---
    # Use onset strength at beat positions to find the strongest beat in each 4-beat group
    # The downbeat (beat 1) should be the loudest in a bar
    result.beat_positions = _detect_downbeats(y, sr, librosa, beat_times)

    # --- Key Detection (Krumhansl-Schmuckler) ---
    logger.info("  Detecting key...")
    result.key_camelot, result.key_musical = _detect_key(y, sr, librosa)

    # --- Waveform Generation ---
    logger.info("  Generating waveform...")
    result.waveform_mono, result.waveform_rgb = _generate_waveform(y, sr, target_waveform_width, librosa)

    logger.info(
        "  Done: %.1f BPM, %s (%s), %d beats, %.1fs",
        result.bpm, result.key_camelot, result.key_musical,
        len(result.beat_grid), result.duration_s,
    )

    return result


def _find_kick_anchor(y, sr, librosa, beat_times, onset_times, interval) -> float:
    """
    Find the first strong kick drum to anchor the beat grid.
    Uses low-frequency energy (<200Hz) to isolate kick drums,
    then finds the strongest onset near a beat position in the first 10 seconds.
    """
    from scipy.signal import butter, sosfilt

    # Low-pass filter to isolate kick drum frequencies (<200Hz)
    sos = butter(4, 200, btype='low', fs=sr, output='sos')
    y_kick = sosfilt(sos, y)

    # Get onset strength of kick-filtered signal
    hop = 256
    kick_onset = librosa.onset.onset_strength(y=y_kick, sr=sr, hop_length=hop)

    # Look at the first 15 seconds of the track for a clean anchor
    max_time = min(15.0, len(y) / sr)

    # Find all onsets in the kick signal within the first 15 seconds
    kick_onset_frames = librosa.onset.onset_detect(
        y=y_kick, sr=sr, hop_length=hop, backtrack=False,
    )
    kick_onset_times = librosa.frames_to_time(kick_onset_frames, sr=sr, hop_length=hop)
    kick_onset_times = kick_onset_times[kick_onset_times < max_time]

    if len(kick_onset_times) == 0:
        # Fallback: use first beat from librosa
        return float(beat_times[0]) if len(beat_times) > 0 else 0.0

    # Score each kick onset by its energy
    kick_scores = []
    for t in kick_onset_times:
        # Get the amplitude of the kick signal at this point
        sample_idx = int(t * sr)
        window = 512  # ~12ms window
        start = max(0, sample_idx - window)
        end = min(len(y_kick), sample_idx + window)
        energy = np.sqrt(np.mean(y_kick[start:end] ** 2))
        kick_scores.append((t, energy))

    # Sort by energy (strongest first)
    kick_scores.sort(key=lambda x: -x[1])

    # Find the strongest kick that aligns with the beat grid
    # (i.e., falls on a beat position within 20ms)
    for t, energy in kick_scores:
        # Check if this onset is near any expected beat position
        # Expected beats from this anchor: t, t+interval, t+2*interval, ...
        # We want the anchor where subsequent beats also have strong onsets
        support = 0
        for i in range(1, min(8, int((max_time - t) / interval))):
            expected = t + i * interval
            if len(kick_onset_times) > 0:
                dists = np.abs(kick_onset_times - expected)
                if np.min(dists) < 0.03:  # Within 30ms
                    support += 1

        if support >= 3:  # At least 3 of the next 7 beats confirmed
            logger.info("  Kick anchor: %.3fs (energy=%.4f, support=%d/7)", t, energy, support)
            return float(t)

    # Fallback: strongest kick onset
    best_t = kick_scores[0][0]
    logger.info("  Kick anchor (fallback): %.3fs", best_t)
    return float(best_t)


def _detect_downbeats(y, sr, librosa, beat_times) -> list[int]:
    """
    Detect downbeats (beat 1 of each bar) using low-frequency energy.
    Returns list of beat positions (1-4) for each beat.
    """
    if len(beat_times) < 4:
        return [(i % 4) + 1 for i in range(len(beat_times))]

    from scipy.signal import butter, sosfilt

    # Low-pass for kick energy
    sos = butter(4, 200, btype='low', fs=sr, output='sos')
    y_kick = sosfilt(sos, y)

    # Get energy at each beat position
    beat_energies = []
    window_samples = int(0.05 * sr)  # 50ms window around each beat
    for t in beat_times:
        center = int(t * sr)
        start = max(0, center - window_samples)
        end = min(len(y_kick), center + window_samples)
        if start < end:
            energy = np.sqrt(np.mean(y_kick[start:end] ** 2))
        else:
            energy = 0.0
        beat_energies.append(energy)

    beat_energies = np.array(beat_energies)

    # Find the best phase offset (0-3) for downbeats
    # Try each offset and score by total energy on downbeat positions
    best_offset = 0
    best_score = -1
    for offset in range(4):
        score = 0
        count = 0
        for i in range(offset, len(beat_energies), 4):
            score += beat_energies[i]
            count += 1
        if count > 0:
            avg_score = score / count
            if avg_score > best_score:
                best_score = avg_score
                best_offset = offset

    # Assign beat positions based on best offset
    positions = []
    for i in range(len(beat_times)):
        pos = ((i - best_offset) % 4) + 1
        positions.append(pos)

    logger.info("  Downbeat offset: %d (beat 1 starts at beat index %d)", best_offset, best_offset)
    return positions


def _detect_key(y, sr, librosa) -> tuple[str, str]:
    """Detect musical key using Krumhansl-Schmuckler on chroma features."""
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
    chroma_avg = np.mean(chroma, axis=1)

    best_corr = -2
    best_key = ""
    best_mode = ""

    major = np.array(MAJOR_PROFILE)
    minor = np.array(MINOR_PROFILE)

    for shift in range(12):
        rotated = np.roll(chroma_avg, -shift)
        corr_major = np.corrcoef(rotated, major)[0, 1]
        corr_minor = np.corrcoef(rotated, minor)[0, 1]

        if corr_major > best_corr:
            best_corr = corr_major
            best_key = PITCH_CLASS_NAMES[shift]
            best_mode = "major"
        if corr_minor > best_corr:
            best_corr = corr_minor
            best_key = PITCH_CLASS_NAMES[shift]
            best_mode = "minor"

    musical = f"{best_key} {best_mode}"
    camelot = to_camelot(f"{best_key}{'m' if best_mode == 'minor' else ''}")

    return camelot, musical


def _generate_waveform(y, sr, target_width: int, librosa) -> tuple[list[int], list[dict]]:
    """Generate mono amplitude waveform and RGB frequency-band waveform."""
    # Mono amplitude
    chunk_size = max(1, len(y) // target_width)
    mono = []
    for i in range(target_width):
        start = i * chunk_size
        end = min(start + chunk_size, len(y))
        if start >= len(y):
            mono.append(0)
        else:
            rms = np.sqrt(np.mean(y[start:end] ** 2))
            mono.append(rms)

    max_rms = max(mono) if mono else 1
    if max_rms > 0:
        mono = [int(min(255, (v / max_rms) * 255)) for v in mono]
    else:
        mono = [0] * target_width

    # RGB frequency-band waveform
    n_fft = 2048
    hop_length = max(1, len(y) // target_width)
    D = np.abs(librosa.stft(y, n_fft=n_fft, hop_length=hop_length))
    freqs = librosa.fft_frequencies(sr=sr, n_fft=n_fft)

    bass_mask = freqs < 250
    mid_mask = (freqs >= 250) & (freqs < 4000)
    high_mask = freqs >= 4000

    bass = D[bass_mask].sum(axis=0)
    mids = D[mid_mask].sum(axis=0)
    highs = D[high_mask].sum(axis=0)

    def normalize_band(band, target_max=248):
        mx = band.max() if band.max() > 0 else 1
        return np.clip((band / mx) * target_max, 0, target_max).astype(int)

    bass_n = normalize_band(bass)
    mids_n = normalize_band(mids)
    highs_n = normalize_band(highs)

    if len(bass_n) != target_width:
        indices = np.linspace(0, len(bass_n) - 1, target_width).astype(int)
        bass_n = bass_n[indices]
        mids_n = mids_n[indices]
        highs_n = highs_n[indices]

    rgb = []
    for i in range(target_width):
        rgb.append({"r": int(highs_n[i]), "g": int(mids_n[i]), "b": int(bass_n[i])})

    return mono, rgb
