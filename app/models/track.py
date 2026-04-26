"""Track data model for the sync pipeline."""

from dataclasses import dataclass, field


@dataclass
class TrackInfo:
    """Spotify track metadata used throughout the pipeline."""
    spotify_id: str
    title: str
    artist: str
    album: str
    year: str
    duration_ms: int
    artwork_url: str | None
    playlist_name: str
    position: int = 0

    @property
    def safe_filename(self) -> str:
        """Filesystem-safe filename: 'Artist - Title'."""
        safe = f"{self.artist} - {self.title}"
        for ch in '<>:"/\\|?*':
            safe = safe.replace(ch, "_")
        return safe

    @property
    def filename(self) -> str:
        """Full filename with extension."""
        return f"{self.safe_filename}.mp3"


@dataclass
class AnalysisResult:
    """Audio analysis output: BPM, key, beat grid, waveforms."""
    bpm: float = 0.0
    key_camelot: str = ""
    key_musical: str = ""
    beat_grid: list[float] = field(default_factory=list)    # beat times in seconds
    beat_positions: list[int] = field(default_factory=list)  # 1,2,3,4 pattern
    waveform_mono: list[int] = field(default_factory=list)   # amplitude 0-255 per column
    waveform_rgb: list[dict] = field(default_factory=list)   # {"r", "g", "b"} per column
    duration_s: float = 0.0
    sample_rate: int = 0
