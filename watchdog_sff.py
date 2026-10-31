"""Keeps the sff sync going while nobody is watching.

Every couple of minutes it looks at sff. If the server is gone it starts it again, and if a sync stopped with an
error (or never got going) it presses Sync again. Anything already downloaded is picked up from the Nicotine+
folder, so a restart loses at most the playlist being worked on.

It stops by itself when a sync completes properly. Run with the app's own Python:
    app\\.venv\\Scripts\\python.exe watchdog_sff.py
Log: watchdog_sff.log
"""
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP = ROOT / "app"
PY = APP / ".venv" / "Scripts" / "python.exe"
BASE = "http://127.0.0.1:8899"
LOG = ROOT / "watchdog_sff.log"
CHECK_EVERY = 120
MIN_BETWEEN_RESTARTS = 600
LOW_DISK_GB = 15


def log(msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def status():
    try:
        with urllib.request.urlopen(f"{BASE}/api/status", timeout=15) as r:
            return json.loads(r.read())
    except Exception:
        return None


def start_server() -> bool:
    log("sff is not answering: starting it")
    subprocess.Popen([str(PY), "main.py"], cwd=str(APP), creationflags=0x08000000,       # no console window
                     stdout=(APP / "server_out.txt").open("ab"), stderr=(APP / "server_err.txt").open("ab"))
    for _ in range(60):
        time.sleep(2)
        if status() is not None:
            time.sleep(3)
            return True
    log("sff did not come up")
    return False


def press_sync() -> None:
    try:
        req = urllib.request.Request(f"{BASE}/api/sync", method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            log(f"pressed Sync: {r.read().decode()[:120]}")
    except Exception as e:
        log(f"could not press Sync: {e}")


def main() -> None:
    last_restart = 0.0
    low_disk_noted = False
    log("watchdog started")
    while True:
        st = status()
        now = time.time()
        free = shutil.disk_usage("D:\\").free / 1e9
        if free < LOW_DISK_GB and not low_disk_noted:
            log(f"LOW DISK: only {free:.0f} GB free on D: (sff pauses by itself until space is freed)")
            low_disk_noted = True
        elif free >= LOW_DISK_GB:
            low_disk_noted = False

        if st is None:
            if now - last_restart > MIN_BETWEEN_RESTARTS and start_server():
                last_restart = time.time()
                press_sync()
        elif st.get("status") == "done" and st.get("phase") == "complete":
            log(f"sync completed: {st.get('message', '')[:160]}. Watchdog stopping.")
            return
        elif st.get("status") in ("error", "idle", "cancelled") and now - last_restart > MIN_BETWEEN_RESTARTS:
            log(f"sync is not running (status={st.get('status')}: {str(st.get('message'))[:120]}): pressing Sync")
            last_restart = now
            press_sync()
        time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
