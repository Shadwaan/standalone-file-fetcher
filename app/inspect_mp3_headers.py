"""
READ-ONLY deep inspection of an MP3's technical headers.

Compares: the suspect track + a known-good track that Rekordbox analyzes fine.
Reports encoder delay/padding, VBR tag, LAME header, frame sync integrity,
bitrate/samplerate variability, and ID3 padding size.
"""

import struct
import sys
from pathlib import Path

from mutagen.mp3 import MP3, HeaderNotFoundError
from mutagen.id3 import ID3


def read_id3_size(fp: Path) -> int:
    """Return size of the leading ID3v2 tag in bytes (0 if none)."""
    with open(fp, "rb") as f:
        head = f.read(10)
    if head[:3] != b"ID3":
        return 0
    # Syncsafe integer
    size = (head[6] << 21) | (head[7] << 14) | (head[8] << 7) | head[9]
    return size + 10


def find_xing_lame(fp: Path) -> dict:
    """Parse the first MPEG frame and any Xing/Info + LAME tag in it."""
    id3_len = read_id3_size(fp)
    info = {"id3_size": id3_len}
    with open(fp, "rb") as f:
        f.seek(id3_len)
        # Scan up to 4KB for frame sync 0xFFE
        blob = f.read(8192)

    # Find frame sync
    sync_idx = -1
    for i in range(len(blob) - 4):
        if blob[i] == 0xFF and (blob[i + 1] & 0xE0) == 0xE0:
            sync_idx = i
            break
    info["first_frame_offset_from_id3"] = sync_idx
    if sync_idx < 0:
        return info

    header = blob[sync_idx:sync_idx + 4]
    b1, b2, b3, b4 = header
    version_bits = (b2 >> 3) & 0x3
    layer_bits = (b2 >> 1) & 0x3
    bitrate_idx = (b3 >> 4) & 0xF
    sr_idx = (b3 >> 2) & 0x3

    # MPEG 1 Layer III bitrates (kbps)
    BR_TABLE = [
        None, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, None,
    ]
    SR_TABLE = {0: 44100, 1: 48000, 2: 32000, 3: None}

    info["mpeg_version_bits"] = version_bits
    info["layer_bits"] = layer_bits
    info["bitrate_kbps"] = BR_TABLE[bitrate_idx] if version_bits == 3 and layer_bits == 1 else "unknown"
    info["sample_rate"] = SR_TABLE.get(sr_idx)

    # Look for Xing / Info tag: side info is 32 bytes for stereo MPEG1-LIII
    # Then look for "Xing" or "Info"
    xing_start = sync_idx + 4 + 32
    if xing_start + 4 > len(blob):
        return info
    tag = blob[xing_start:xing_start + 4]
    info["vbr_tag"] = tag.decode("ascii", errors="replace") if tag in (b"Xing", b"Info") else None

    if tag in (b"Xing", b"Info"):
        flags = struct.unpack(">I", blob[xing_start + 4:xing_start + 8])[0]
        info["xing_flags"] = flags
        cur = xing_start + 8
        if flags & 0x1:
            info["xing_frames"] = struct.unpack(">I", blob[cur:cur + 4])[0]
            cur += 4
        if flags & 0x2:
            info["xing_bytes"] = struct.unpack(">I", blob[cur:cur + 4])[0]
            cur += 4
        if flags & 0x4:
            cur += 100  # TOC
        if flags & 0x8:
            info["xing_quality"] = struct.unpack(">I", blob[cur:cur + 4])[0]
            cur += 4

        # LAME tag is 36 bytes starting here, begins with 4-char encoder string
        lame = blob[cur:cur + 36]
        if len(lame) >= 36 and lame[:4] in (b"LAME", b"Lavf", b"Lame"):
            info["lame_encoder"] = lame[:9].decode("ascii", errors="replace").strip()
            # Delay + padding is at offset 21-23 (3 bytes = 2 x 12-bit values)
            enc_delay = (lame[21] << 4) | (lame[22] >> 4)
            enc_padding = ((lame[22] & 0x0F) << 8) | lame[23]
            info["encoder_delay_samples"] = enc_delay
            info["encoder_padding_samples"] = enc_padding
        else:
            # Lavf often writes Xing without a proper LAME tag
            info["lame_encoder"] = lame[:4].decode("ascii", errors="replace")
            info["encoder_delay_samples"] = None
            info["encoder_padding_samples"] = None

    return info


def inspect(fp: Path) -> None:
    print(f"\n=== {fp.name}")
    print(f"  size: {fp.stat().st_size:,} bytes")

    try:
        audio = MP3(str(fp))
    except HeaderNotFoundError as e:
        print(f"  ERROR: {e}")
        return

    print(f"  mutagen:  length={audio.info.length:.3f}s  "
          f"bitrate={audio.info.bitrate}  "
          f"sample_rate={audio.info.sample_rate}  "
          f"channels={audio.info.channels}  "
          f"mode={audio.info.mode}  "
          f"version={audio.info.version}  "
          f"layer={audio.info.layer}")
    print(f"  bitrate_mode: {getattr(audio.info, 'bitrate_mode', '?')}")
    print(f"  encoder_info: {getattr(audio.info, 'encoder_info', '?')!r}")
    print(f"  encoder_settings: {getattr(audio.info, 'encoder_settings', '?')!r}")
    print(f"  track_gain: {getattr(audio.info, 'track_gain', None)}  "
          f"track_peak: {getattr(audio.info, 'track_peak', None)}")

    hdr = find_xing_lame(fp)
    for k, v in hdr.items():
        if isinstance(v, int) and v > 1000 and k not in ("id3_size", "first_frame_offset_from_id3"):
            print(f"  hdr.{k}: {v:,}")
        else:
            print(f"  hdr.{k}: {v}")

    # Calculate offset the way Rekordbox might:
    # First audio sample position = encoder_delay_samples / sample_rate (s)
    delay = hdr.get("encoder_delay_samples")
    sr = hdr.get("sample_rate") or 44100
    if delay is not None:
        print(f"  >>> encoder delay = {delay} samples = {delay / sr * 1000:.2f} ms")
    else:
        print(f"  >>> encoder delay: UNKNOWN (no LAME tag) — Rekordbox will guess")


def main():
    if len(sys.argv) < 2:
        print("Usage: python inspect_mp3_headers.py <file1.mp3> [file2.mp3 ...]")
        sys.exit(1)
    for arg in sys.argv[1:]:
        inspect(Path(arg))


if __name__ == "__main__":
    main()
