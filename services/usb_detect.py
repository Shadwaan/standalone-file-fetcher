"""
USB drive detection.

Scans for removable USB drives on Windows, skipping system drives.
"""

import logging
import os
import shutil
import subprocess

logger = logging.getLogger(__name__)

# Drives to always skip (system + music storage + partitions)
SKIP_DRIVES = {"C:", "D:", "E:"}


def detect_usb_drives() -> list[dict]:
    """Detect USB/removable drives. Returns list of {letter, name, size_gb, free_gb, has_rekordbox}."""
    drives = []

    for letter in "EFGHIJKLMNOPQRSTUVWXYZ":
        drive = f"{letter}:"
        drive_path = f"{drive}/"

        if not os.path.exists(drive_path):
            continue

        try:
            usage = shutil.disk_usage(drive_path)

            # Get volume name
            name = _get_volume_name(drive)

            # Check for existing Rekordbox data
            has_pioneer = os.path.exists(os.path.join(drive_path, "PIONEER"))
            has_contents = os.path.exists(os.path.join(drive_path, "Contents"))

            drives.append({
                "letter": drive,
                "name": name,
                "size_gb": round(usage.total / (1024 ** 3), 1),
                "free_gb": round(usage.free / (1024 ** 3), 1),
                "has_rekordbox": has_pioneer or has_contents,
            })
        except (OSError, PermissionError):
            pass

    logger.info("Detected %d USB drives", len(drives))
    return drives


def _get_volume_name(drive: str) -> str:
    """Get the volume label of a drive via wmic."""
    try:
        result = subprocess.run(
            ["wmic", "logicaldisk", "where", f"Caption='{drive}'", "get", "VolumeName", "/value"],
            capture_output=True, text=True, timeout=3,
        )
        for line in result.stdout.strip().split("\n"):
            if line.startswith("VolumeName="):
                name = line.split("=", 1)[1].strip()
                if name:
                    return name
    except Exception:
        pass
    return "USB Drive"
