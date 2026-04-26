# AI Assistant Guidelines for Standalone File Fetcher

## Project Context
Standalone app bridging Spotify playlists to Rekordbox + Traktor DJ libraries, with USB export for CDJ hardware.
Extracted from DJ File Manager (FM) and DJ File Fetcher codebases into a single self-contained application.

**Read PRD.md first** — it contains the full pipeline spec, safety rules, and complete bugs-fixed table.

---

## Architecture Decision: Rekordbox Handles Analysis

**We do NOT use librosa for BPM/key/beatgrid analysis.** Rekordbox's built-in analyzer is far superior for DJ music. Our pipeline:
1. Download + tag MP3 (via yt-dlp + ffmpeg + mutagen)
2. Import to Rekordbox DB with `Analysed=0` (no ANLZ files)
3. Import to Traktor NML (no BPM/key/grid)
4. Flush WAL **TWICE**
5. User opens Rekordbox, analyzes per-playlist manually

The `analyzer.py` file exists but is NOT used in the sync pipeline. It's kept for reference only.

**Auto-analyze GUI automation is REMOVED.** It used to live in `services/rekordbox_auto.py`. The problem: Ctrl+A in Rekordbox's Collection selects all 5000 tracks and forces re-analysis of the whole library. We deleted the file entirely 2026-04-26. Users open Rekordbox after sync; Rekordbox auto-analyzes new tracks on next launch (now that import hygiene is correct — UUID, ArtistID, AlbumID, SR/BR from file). No GUI automation needed.

**USB sync is REMOVED.** It used to write a Rekordbox-XML-on-USB format that CDJs couldn't actually use (CDJs need Device Library Plus or PDB, both proprietary). For real CDJ-ready USB drives, use Rekordbox's native "Export to Device" instead. Files removed: `services/usb_detect.py`, `services/usb_export.py`. See git history (commit `ac51935`) if you ever need to revive.

**Traktor sync is OPT-IN.** Set `ENABLE_TRAKTOR=1` in `.env` to also write to `collection.nml`. Default off (Rekordbox-only). Code in `services/traktor.py` is preserved either way.

---

## File Organization

Downloads are organized into playlist subfolders:
```
D:/Music Backup/Incoming/
├── Opening/
│   ├── Jo Paciello - Fantasy.mp3
│   └── ...
├── Progressive/
│   └── ...
└── Oldies/
    └── ...
```

The playlist subfolder name matches the display name (prefix stripped: "FF Opening" → "Opening").
Rekordbox FolderPath stores the full path including the subfolder.
Duplicate detection scans all subfolders recursively via the file index.

---

## Critical Bugs to Avoid (Hard-Won Lessons)

### 1. pyrekordbox requires EXPLICIT IDs
pyrekordbox does NOT auto-generate primary keys. You MUST set `.ID` before flush/commit:
```python
import random
content = tables.DjmdContent()
content.ID = str(random.randint(100000000, 999999999))  # 9-digit string
```
This applies to: DjmdContent, DjmdPlaylist, DjmdSongPlaylist, DjmdArtist, DjmdAlbum.
**If you forget this, the entire transaction rolls back silently.**

### 2. DjmdContent MUST have FileType/BitRate/SampleRate
Without these, Rekordbox shows red ? icons and cannot load the track:
```python
content.FileType = 1      # 1 = MP3
content.BitRate = 320      # kbps
content.SampleRate = 44100 # Hz
```

### 3. DjmdContent AND DjmdPlaylist BOTH need rb_data_status=0 and rb_local_usn
`rb_data_status=1` means "pending cloud sync" — Rekordbox ignores these rows entirely.
**CRITICAL: This applies to playlists too**, not just content. If the playlist row has `rb_data_status=1`, the playlist will appear empty in Rekordbox even if DjmdSongPlaylist rows exist and all content is correct.

```python
from sqlalchemy import func as sa_func

# For content:
max_usn = db.session.query(sa_func.max(tables.DjmdContent.rb_local_usn)).scalar() or 0
content.rb_data_status = 0
content.rb_local_usn = max_usn + 1

# For playlist (DjmdPlaylist):
max_pl_usn = db.session.query(sa_func.max(tables.DjmdPlaylist.rb_local_usn)).scalar() or 0
playlist.rb_data_status = 0
playlist.rb_local_usn = max_pl_usn + 1
playlist.usn = None  # Important: not 0, None

# For songs in playlist (DjmdSongPlaylist):
# These CAN have rb_data_status=1 (that's how working playlists look)
```

### 4. WAL checkpoint must run TWICE
pyrekordbox uses SQLite WAL mode. Writes go to a WAL file, NOT master.db. Rekordbox reads master.db directly and NEVER sees WAL contents. A single checkpoint is insufficient because pyrekordbox opens a new connection per operation, and even the checkpoint itself may leave WAL entries.

```python
from sqlalchemy import text

# First flush
db = Rekordbox6Database()
db.session.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
db.session.commit()
db.session.close()
db.engine.dispose()

# Second flush — catches WAL entries from the first checkpoint
db2 = Rekordbox6Database()
db2.session.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
db2.session.commit()
db2.session.close()
db2.engine.dispose()

# Verify
import os
wal_path = os.path.join(os.environ["APPDATA"], "Pioneer", "rekordbox", "master.db-wal")
wal_size = os.path.getsize(wal_path) if os.path.exists(wal_path) else 0
# wal_size should be 0 — if not, Rekordbox won't see the changes
```

**Without this, all your imports are invisible to Rekordbox.** This was the cause of playlists showing 0 tracks.

### 5. Rekordbox being open breaks WAL flush
When Rekordbox is running, it:
- Loads master.db into memory on startup
- Creates its own WAL for its internal operations
- Doesn't see our writes to its WAL even after checkpoint
- May overwrite our flushed data with stale in-memory state

If user reports "changes not visible in Rekordbox after sync":
1. Check WAL file size — should be 0 bytes
2. Tell user to close Rekordbox COMPLETELY and reopen
3. Rekordbox reads master.db on startup, picks up the fresh data

### 6. Duplicate detection MUST match both artist AND title
**Title-only matching causes false positives.** E.g. "Dreamer" by Four Tet matched a different "Dreamer" by another artist. All three strategies in `find_content_by_title()` must verify the artist:

```python
# Strategy A: Exact filename pattern "Artist - Title"
target_name = f"{artist} - {title}".lower()
# Matches fname.stem.lower() == target_name

# Strategy B: Title match + verify artist in path OR ArtistID
if ct == title_lower and (first_artist in fp_lower or artist in ArtistRecord.Name):
    return match

# Strategy C: Partial filename — BOTH artist AND title in filename
if title_lower in fname and first_artist in fname:
    return match
```
Never match by title alone.

### 7. DO create DjmdArtist + DjmdAlbum rows (with explicit IDs) — UPDATED 2026-04-25
**Old guidance was wrong.** Earlier this said "don't create new artists, embed in Title". That workaround leaves `AlbumID=None`/`ArtistID=None` on DjmdContent, which makes Rekordbox's batch analysis hang on the second sequential track (the "2nd-track-hang" pattern).

**Verified by drag-import test** (2026-04-25): files dragged into Rekordbox via Explorer get proper Album + Artist rows created automatically and analyze instantly with no hangs. Same files inserted by sff with Album/Artist=None hang reproducibly.

**Correct approach: create the rows with explicit IDs, just like DjmdContent.** The old "crashes the session" symptom was caused by missing the explicit ID, not by creating new rows in general.

```python
import random
from sqlalchemy import func as sa_func
from datetime import datetime, timezone

# Helper for any DjmdAlbum/DjmdArtist creation
def _get_or_create_artist(db, name):
    if not name:
        return None
    existing = db.session.query(tables.DjmdArtist).filter_by(Name=name).first()
    if existing:
        return existing.ID
    new_id = str(random.randint(1000000000, 9999999999))
    while db.session.query(tables.DjmdArtist).filter_by(ID=new_id).first():
        new_id = str(random.randint(1000000000, 9999999999))
    artist = tables.DjmdArtist()
    artist.ID = new_id
    artist.Name = name
    artist.rb_data_status = 0
    artist.rb_local_data_status = 0
    artist.rb_local_deleted = 0
    artist.rb_local_synced = 0
    artist.usn = None
    max_usn = db.session.query(sa_func.max(tables.DjmdArtist.rb_local_usn)).scalar() or 0
    artist.rb_local_usn = max_usn + 1
    artist.created_at = datetime.now(timezone.utc)
    artist.updated_at = datetime.now(timezone.utc)
    db.session.add(artist)
    db.session.flush()
    return artist.ID

# Same pattern for DjmdAlbum — set Name, AlbumArtistID (optional), and same rb_* fields

content.ArtistID = _get_or_create_artist(db, track.artist)
content.AlbumID = _get_or_create_album(db, track.album, artist_id=content.ArtistID)
```

### 8. yt-dlp needs ffmpeg_location (not just PATH)
Setting `os.environ["PATH"]` is unreliable. Pass the path directly:
```python
ydl_opts = {
    "ffmpeg_location": r"C:\Users\Lenovo\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1-full_build\bin",
    ...
}
```

### 9. yt-dlp search must use extract_flat=True
Using `extract_flat=False` for search causes yt-dlp to fully extract each result. If ANY result is an unavailable video, the entire search crashes:
```python
ydl_opts = {
    "extract_flat": True,
    "default_search": "ytsearch5",
    ...
}
```

### 10. Spotify API fields can be missing
Always use `.get()` with defaults:
```python
for item in results.get("items", []):
    if not item or not item.get("name"):
        continue
    tracks_info = item.get("tracks") or {}
    track_count = tracks_info.get("total", 0)
```

### 11. DjmdContent attributes must be set individually
ArtistName is an association proxy — passing it as a kwarg to the constructor crashes:
```python
# WRONG: tables.DjmdContent(Title="...", ArtistName="...")  # CRASHES
# RIGHT:
content = tables.DjmdContent()
content.Title = "..."
content.ArtistID = artist_obj.ID
```

### 12. NEVER write to master.db while Rekordbox is running
Check process list before sync:
```python
import psutil
for proc in psutil.process_iter(['name']):
    if 'rekordbox' in proc.info['name'].lower():
        raise RuntimeError("Close Rekordbox before syncing")
```

### 13. Import with Analysed=0 — NEVER use librosa
Rekordbox analysis is superior. Import tracks as unanalyzed and let Rekordbox handle BPM/key/beatgrid:
```python
content.Analysed = 0
# Do NOT write ANLZ files
# Do NOT set BPM or KeyID
# Do NOT set AnalysisDataPath
```

### 14. Auto-analyze would re-analyze everything — DISABLED
Rekordbox GUI's Ctrl+A selects ALL tracks in Collection (5000+), causing re-analysis of the entire library. There's no GUI way to filter only unanalyzed tracks. **Auto-analyze is disabled.** User analyzes per-playlist manually.

### 15. USB detection is NOT PROPERLY FIXED (hardcoded skip list)
**This is an unresolved issue.** The code doesn't actually detect whether a drive is removable — it just lists all drive letters and skips a hardcoded set. E: is an SSD partition on this machine but looks identical to a USB drive via `os.path.exists()`.

Current workaround:
```python
SKIP_DRIVES = {"C:", "D:", "E:"}
```

**This only works on THIS machine.** If the user has a different partition layout, or plugs a real USB into E:, this breaks. The fragile hardcoded list gets stale fast.

**Proper fix (do this if user keeps hitting this):**
```python
import ctypes
drive_type = ctypes.windll.kernel32.GetDriveTypeW(f"{letter}:\\")
# Only accept drive_type == 2 (DRIVE_REMOVABLE)
# DRIVE_FIXED=3 (SSDs, partitions) would be correctly skipped
```
This uses the Windows API to check actual drive type, making the detection robust regardless of drive letter.

---

## Rekordbox Integration Rules

### DjmdContent Creation (Unanalyzed Import)
- Set `.ID` = random 9-digit string (verify unique)
- **Set `.UUID` = `str(uuid.uuid4())`** — CRITICAL. Rekordbox uses this to build the ANLZ path `/PIONEER/USBANLZ/{uuid[:3]}/{uuid[3:]}/ANLZ0000.DAT`. Without it, Rekordbox writes to broken sentinel path `/PIONEER/USBANLZ///ANLZ0018.DAT` and batch analysis hangs.
- Set `.FolderPath` = forward-slash normalized path (including playlist subfolder)
- Set `.Title`, `.FileNameL`, `.FileSize`
- Set `.FileType` = 1
- **Set `.BitRate` and `.SampleRate` from the actual MP3 file** via `mutagen.mp3.MP3.info.sample_rate` and `info.bitrate // 1000`. Hardcoding 44100 caused analysis hangs because yt-dlp outputs 48000Hz MP3s; Rekordbox sees the DB/file SR mismatch and struggles. Verified 2026-04-25.
- **Set `.ArtistID`** to a real DjmdArtist row (use `_get_or_create_artist` which sets UUID on artist too)
- **Set `.AlbumID`** to a real DjmdAlbum row (use `_get_or_create_album` which sets UUID on album too)
- **Drag-import parity fields** (without these, batch analysis hangs):
  - `.HotCueAutoLoad = 'on'`
  - `.DeliveryControl = 'on'`
  - `.StockDate = today_yyyy_mm_dd`
  - `.DateCreated = today_yyyy_mm_dd`
  - `.ColorID = '0'`, `.DJPlayCount = 0`, `.DiscNo = 0`, `.Rating = 0`, `.TrackNo = 0`
- Set `.Analysed` = 0 (Rekordbox will analyze)
- Do NOT set BPM, KeyID, or AnalysisDataPath — Rekordbox handles these
- Do NOT write ANLZ files — Rekordbox creates its own
- Set `.rb_data_status` = 0, assign sequential `.rb_local_usn`
- Set `.updated_at` = `datetime.now(timezone.utc)` (NOT isoformat string)
- **Without UUID + Artist/Album rows + drag-import parity fields, Rekordbox batch analysis hangs on the 2nd track.** Verified 2026-04-25 by row-diff between drag-imported (works) and sff-imported (hangs) track.

### DjmdPlaylist Creation (CRITICAL — three things must all be done)
- **`.ID` = `str(random.randint(1, (2**32) - 1))`** (32-bit unsigned). NOT modulo `10^10`. Rekordbox stores playlist IDs as 32-bit ints in `masterPlaylists6.xml`. IDs > 2^32-1 (9+ hex chars) are unparseable by Rekordbox and the playlist appears empty.
- **`.UUID` = `str(uuid.uuid4())`** — without UUID, playlist appears empty.
- Set `.Name`, `.Seq` = 0 (top), bump existing Seq values up
- Set `.Attribute` = 0, `.ParentID` = 'root'
- Set `.rb_data_status` = 0 (not 1)
- Set `.rb_local_usn` = next sequential, `.usn` = None
- **After commit, register the playlist in `%APPDATA%/Pioneer/rekordbox/masterPlaylists6.xml`** by adding a `<NODE Id="{HEX}" ParentId="0" Attribute="0" Timestamp="{UNIX_MS}" Lib_Type="0" CheckType="0"/>` entry under the `<PLAYLISTS>` element. Without this, Rekordbox doesn't know the playlist exists. Use `_register_playlist_in_xml(playlist_id)` helper in services/rekordbox.py.

All three must be done. Missing any one results in an empty-looking playlist in the UI. Verified 2026-04-25 r5 via NuJungle saga (DEBUG_LOG section 12).
- Set `.rb_local_usn` = max+1 from DjmdPlaylist USNs
- Set `.usn` = None (not 0, None)
- Set `.rb_local_data_status` = 0, `.rb_local_deleted` = 0, `.rb_local_synced` = 0

### DjmdSongPlaylist (songs within playlist)
- Set `.ID` = random 10-digit string
- **Set `.UUID` = `str(uuid.uuid4())`** — CRITICAL. Rekordbox filters out song rows without UUIDs (the entire playlist appears empty in UI even though playlist row + content rows are correct). Verified 2026-04-25 r4 via NuJungle empty-playlist bug.
- Set `.PlaylistID`, `.ContentID`, `.TrackNo` (1-based)
- Set `.rb_data_status` = 0 (NOT 1 — earlier guidance was wrong)
- Set `.usn = None` and `.rb_local_usn` = next sequential from `func.max(DjmdSongPlaylist.rb_local_usn)` + 1

### Files in `%APPDATA%\Pioneer\rekordbox\` — what to touch and what NOT to touch

**DO NOT TOUCH:**
- `master.db` — write only via pyrekordbox transactions, never edit raw
- `master.db-wal`, `master.db-shm` — SQLite WAL/shared memory; pyrekordbox manages these
- `share/USBANLZ/` — Rekordbox's analysis output (BPM, key, beat grid, waveforms). Deleting these throws away analysis work.
- `share/Artwork/` — artwork cache; safe to delete contents but Rekordbox will re-extract
- `masterPlaylists6.xml` — **Rekordbox stores playlist position memory here.** When a track is removed from master.db and re-added (e.g. via drag-import), Rekordbox cross-references this XML and restores its original TrackNo automatically. Do not delete or modify this file from sff. Verified 2026-04-25 by drag-import test.
- `automixPlaylist6.xml` — Auto-mix queue state
- `master.backup*.db` and `master.db.bak.*` — backups; preserve these

**SAFE TO RENAME/DELETE for cache reset (Rekordbox closed first):**
- `networkAnalyze6.db` — internal analysis cache; regenerated on next launch
- `ExtData.edb`, `ExtData.backup.edb` — extended data cache
- `datafile.edb`, `datafile.backup.edb` — datafile cache
- `Crashes/`, `Corrupt/` — diagnostic dumps

### WAL Flush (DOUBLE — after ALL DB operations)
```python
from pyrekordbox import Rekordbox6Database
from sqlalchemy import text

for _ in range(2):
    db = Rekordbox6Database()
    db.session.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
    db.session.commit()
    db.session.close()
    db.engine.dispose()
```

---

## USB Export Rules

### Drive Detection (usb_detect.py)
- Scan E: through Z: (not C: or D:)
- Skip E: specifically (it's an SSD partition on this user's machine)
- Use `shutil.disk_usage()` for size, `wmic` for volume name

### Export Flow (usb_export.py)
1. Find FF playlists: iterate all DjmdPlaylist, check if any track in it is from `D:/Music Backup/Incoming/`
2. Create `USB:/Contents/{PlaylistName}/` directories
3. Copy audio files (skip if dest exists with same size)
4. For analyzed tracks: copy ANLZ files from `%APPDATA%/Pioneer/rekordbox/share/PIONEER/USBANLZ/{uuid}/` to `USB:/PIONEER/USBANLZ/{new_uuid}/`
5. **Rewrite PPTH tag** inside each ANLZ file to use USB-relative path (`Contents/{Playlist}/{file}`)
6. Write `USB:/PIONEER/rekordbox/rekordbox.xml` with all playlists + tracks

### PPTH Rewrite in ANLZ
PPTH tag structure: `"PPTH"` (4 bytes) + header_len (4) + tag_len (4) + path_len (4) + path_bytes (UTF-16BE).
Must also update the file header's total length field.

### Limitation
This approach does NOT create Device Library Plus format (`exportLibrary.db`). Only Rekordbox's native "Export to Device" creates that. For newer CDJs that require DLP, user must use Rekordbox's built-in export.

---

## Traktor NML Writer Rules
- ATOMIC writes: write to temp → parse to verify → backup .bak → shutil.move to replace
- LOCATION: `VOLUME` + `DIR` (/:separated/:) + `FILE`
- Import tracks unanalyzed — just file entry with artist, title, bitrate
- Traktor will analyze on first load
- New playlists: insert NODE at index 0 of root PLAYLISTS node

---

## Duplicate Detection (3-layer, ALL require artist+title match)
Before downloading any track:
1. **Rekordbox DB**: `find_content_by_title(artist, title)` — three strategies, ALL requiring artist match
2. **File index**: `_build_file_index()` — scans D:/Music Backup recursively at sync start
3. **Music folder**: checks `Incoming/{PlaylistName}/track.filename` then `Incoming/track.filename`

If any layer matches, skip download. If Rekordbox match found, just add existing track to playlist.
**NEVER match by title alone.**

---

## Spotify API Rules
- spotipy with SpotifyOAuth (scope: playlist-read-private, playlist-read-collaborative)
- Cache token at `.spotify_cache` in project root
- `current_user_playlists(limit=50)` — MUST paginate, user has 382+ playlists
- `playlist_items()` — do NOT use `fields` parameter (filters out the `item` field)
- Track data is at `item.get("track") or item.get("item")` (API inconsistency)

---

## YouTube Download Rules
- Try 5 search query variants: full artist, first artist only, title only
- Use `extract_flat=True` for search to avoid unavailable video crashes
- Duration tolerance: 30 seconds
- Always pass `ffmpeg_location` in yt-dlp options
- Post-process to MP3 320kbps
- Tag with mutagen (TPE1, TIT2, TALB, TDRC, APIC for artwork)
- Save to `D:/Music Backup/Incoming/{PlaylistName}/` subfolder

---

## Safety Rules (NON-NEGOTIABLE)
1. NEVER delete tracks from Rekordbox/Traktor library
2. NEVER delete playlists from Rekordbox/Traktor
3. NEVER sync while Rekordbox is running — check and refuse
4. NEVER match duplicates by title alone
5. Only REMOVE tracks from playlists, or STOP syncing playlists
6. Always use atomic writes for Traktor NML
7. Always flush WAL TWICE after DB writes, verify WAL is 0 bytes
8. Set rb_data_status=0 on BOTH content AND playlists
9. Never use librosa for analysis — Rekordbox handles it
10. Never write ANLZ during import — only copy/rewrite during USB export
11. Rewrite PPTH in ANLZ when copying to USB

---

## Diagnostic Commands

### Check if a playlist is missing tracks
```python
from pyrekordbox import Rekordbox6Database
from pyrekordbox.db6 import tables
db = Rekordbox6Database()
for pl in db.session.query(tables.DjmdPlaylist).all():
    if pl.Name == "MyPlaylist":
        print(f"rb_data_status={pl.rb_data_status}")  # Must be 0
        print(f"rb_local_usn={pl.rb_local_usn}")  # Must be non-zero
        songs = db.session.query(tables.DjmdSongPlaylist).filter_by(PlaylistID=pl.ID).all()
        print(f"Songs: {len(songs)}")
```

### Check WAL size
```python
import os
wal = os.path.join(os.environ["APPDATA"], "Pioneer", "rekordbox", "master.db-wal")
print(os.path.getsize(wal) if os.path.exists(wal) else 0)
# Should be 0. If not, run WAL checkpoint.
```

### Force WAL flush
```python
from pyrekordbox import Rekordbox6Database
from sqlalchemy import text
for _ in range(2):
    db = Rekordbox6Database()
    db.session.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
    db.session.commit()
    db.session.close()
    db.engine.dispose()
```

### Fix broken playlist (rb_data_status=1)
```python
from datetime import datetime, timezone
from sqlalchemy import func
max_usn = db.session.query(func.max(tables.DjmdPlaylist.rb_local_usn)).scalar() or 0
for pl in db.session.query(tables.DjmdPlaylist).filter_by(rb_data_status=1).all():
    max_usn += 1
    pl.rb_data_status = 0
    pl.rb_local_usn = max_usn
    pl.updated_at = datetime.now(timezone.utc)
db.session.commit()
# Then flush WAL twice
```

---

## Repo layout (post-restructure 2026-04-26)

All technical code lives in `app/`. Launchers + README live at the repo root.

```
/
├── README.md           ← end-user doc (zero-config flow)
├── start.command       ← Mac launcher (auto-bootstraps venv, auto-shutdown)
├── start.bat           ← Windows launcher (same)
└── app/                ← everything technical
    ├── main.py
    ├── requirements.txt
    ├── .env.example    ← optional power-user overrides only
    ├── services/
    │   ├── platform_paths.py   ← cross-platform path/process resolution
    │   ├── app_config.py       ← user prefs (music_folder, sync_to_traktor)
    │   ├── spotify.py          ← uses SpotifyPKCE (no Client Secret)
    │   ├── sync.py             ← orchestrator, has stop_syncing_playlist()
    │   ├── downloader.py, rekordbox.py, traktor.py
    │   └── analyzer.py         ← unused (legacy librosa code)
    ├── frontend/index.html
    ├── models/
    ├── SETUP.md, CLAUDE.md, PRD.md, DEBUG_LOG.md
```

**Files no longer present** (deleted in commit 67e59f0): `services/usb_detect.py`, `services/usb_export.py`, `services/rekordbox_auto.py`. Don't try to reference them.

## Running

```bash
# Easiest: from the repo root, double-click start.command (Mac) or
# start.bat (Windows). They auto-detect Python (>= 3.10), create app/.venv,
# pip-install deps, run app/main.py with AUTO_SHUTDOWN_IDLE=20.

# Manual (developer mode):
cd app
.venv/bin/python main.py             # Mac/Linux
.venv\Scripts\python.exe main.py     # Windows
# Or with bootstrap if you blew away .venv:
#   python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Server hosts on `http://localhost:8899`. Click Sync. Heartbeat from the page keeps it alive; close tab → server exits ~20s later.

## Cross-platform notes

- **Path resolution**: `app/services/platform_paths.py` is the single source of truth. It auto-detects Rekordbox base dir (`%APPDATA%/Pioneer/rekordbox` on Win, `~/Library/Pioneer/rekordbox` on Mac), Traktor NML path, ffmpeg location, etc. Never hardcode platform paths in other modules — import from `platform_paths`.
- **Rekordbox 7 verified working** with pyrekordbox 0.4.4 on macOS (read + write).
- **Windows-only deps**: `pywinauto`, `pygetwindow`, `pyautogui` were removed from requirements when `rekordbox_auto.py` was deleted. Don't reintroduce them.

## Spotify auth (PKCE flow)

`app/services/spotify.py` uses `spotipy.oauth2.SpotifyPKCE`, not `SpotifyOAuth`. No Client Secret anywhere — security comes from a per-session crypto challenge instead. Hardcoded `DEFAULT_CLIENT_ID = "ee8d13f0effb403ca47b7fe518b55633"` (sff's shared dev app — Client IDs are public by design, sent in plaintext OAuth URLs); env override via `SPOTIFY_CLIENT_ID` in `app/.env` for power users / BYO-app workaround.

Cache lives at `app/.spotify_cache` (gitignored). PKCE-issued tokens cannot be refreshed by code that uses the old `SpotifyOAuth` flow and vice-versa — if you switch flows, delete the cache.

### Spotify Developer Program limits (current — May 2025 policy)

- **Development Mode is capped at 5 named users.** Reduced from 25 in May 2025. The owner is one of those 5 — so 5 users total, not 5 + owner.
- **Each user must be allowlisted manually** in `dashboard → app → Settings → User Management` using their *Spotify-account email* (which can differ from their primary email).
- **Non-allowlisted users get 403** during OAuth. They can't authorize, period.
- **App owner must have a Spotify Premium account** to keep dev-mode apps active.
- **Higher API rate limits only kick in at Extended Quota Mode.** Standard rate limits are fine for hundreds of tracks per playlist; consider this if syncing huge libraries.

### Extended Quota Mode is effectively closed to individuals

As of May 15, 2025, the apply path moved out of the dashboard into a Google Form requiring:
- Company email (no `@gmail.com`)
- Registered business entity
- Active, launched service
- **At least 250,000 monthly active users**
- Review takes up to 6 weeks

This is a chicken-and-egg gate by design — you can't reach 250k MAU with a 5-user cap. Don't waste time applying as an individual. Don't suggest fronting through a sponsor company; reviews catch this and damage that company's developer account.

### How to scale past 5 users

The only realistic path: the user creates their own Spotify dev app (free, takes 2 min) and overrides `SPOTIFY_CLIENT_ID` in `app/.env`. They become the sole occupant of their own dev-mode bubble. README step 3 documents this. **Scales infinitely** because each user is in their own quota.

### Kill-switch if a Client ID gets abused

`dashboard → File Fetcher → Settings → Reset Client ID` (or create a new app entirely). Update `DEFAULT_CLIENT_ID` in `services/spotify.py`, push. Old users on that ID need to re-authorize after pulling.

## Settings architecture

Three places where settings live, in order of precedence:

1. **`app/app_config.json`** (gitignored, written via UI): `music_folder`, `first_run_complete`, `sync_to_traktor`. Read via `services.app_config.get_*()` helpers, never raw JSON access.
2. **`app/.env`** (gitignored, optional): power-user overrides. Most users have no `.env`.
3. **`platform_paths.py` defaults**: platform-detected fallbacks.

Traktor opt-in is now a UI checkbox (`app_config.sync_to_traktor`), NOT the legacy `ENABLE_TRAKTOR=1` env var (still works as fallback for migration).

## Auto-shutdown

Frontend pings `POST /api/heartbeat` every 5s. `app/main.py` watchdog exits the process after `AUTO_SHUTDOWN_IDLE` seconds without a heartbeat (default off; launchers set it to 20). Closing the browser tab → server dies → start.command/.bat exits → Terminal/cmd window auto-closes (osascript on Mac, just exits on Windows).

## State files (all gitignored, all live in `app/`)

- `app/sync_state.json` — per-playlist track tracking. Now also stores `name` and `display_name` per playlist (added for stop_syncing lookup).
- `app/.spotify_cache` — PKCE token cache. Delete = forces re-auth (used by the Sign-out UI button → `POST /api/spotify/sign-out`).
- `app/app_config.json` — user prefs (above).

Clearing `sync_state.json` forces a full re-check; dedup still prevents re-downloads.

## Dependencies

```
fastapi, uvicorn, spotipy, yt-dlp, mutagen, numpy, pyrekordbox,
python-dotenv, requests, psutil
```

`librosa` was removed (unused — see CLAUDE.md item 13). Windows GUI deps (`pywinauto`/`pygetwindow`/`pyautogui`) were removed with `rekordbox_auto.py`.

## GUI features — non-obvious behaviors and gotchas

### First-run music folder modal
Triggered automatically when `app_config.first_run_complete == False`. User picks a parent folder; `app_config.set_music_folder()` appends `/Incoming` if not already present (so `~/Music` → `~/Music/Incoming`). The "Incoming" suffix is sff's convention — code in `sync.py` and `usb_export.py` (legacy) assume it. Don't bypass the helper.

### Folder picker (Browse... button)
Implemented as a **subprocess** that runs tkinter, NOT as `loop.run_in_executor`. **Reason:** tkinter on macOS REQUIRES the main thread of its process. uvicorn workers are not the main thread, so calling `tk.Tk()` from a worker thread crashes the entire uvicorn worker (the user sees "Failed to fetch" and the server is dead). The subprocess gets its own main thread, sidesteps the macOS restriction, and works on Windows too.

If tkinter isn't available (brew bare `python@3.13` ships without it; needs `python-tk@3.13` or python.org's installer), the subprocess exits with stderr; the frontend falls back gracefully to "paste the path manually" and shows that hint to the user.

### Stop-syncing button (per playlist)
`POST /api/playlists/{id}/stop-syncing` removes the playlist from `sync_state.json` ONLY. It does NOT:
- Delete the playlist in Rekordbox
- Delete the playlist in Traktor
- Touch the downloaded MP3s on disk

This is intentional and a NON-NEGOTIABLE safety rule (see Safety Rules section). The user can re-add the FF prefix in Spotify and the playlist gets rediscovered + re-tracked on the next sync. State lookup supports both playlist ID and display_name.

### Tracked Playlists card
Reads `sync_state.json` directly (not the last sync's progress). Shows what sff *thinks it's syncing*, persistent across restarts. This is distinct from the "Playlists" card which only shows the most recent sync run.

### Sign out of Spotify (button in Config card)
Click → confirmation dialog → `POST /api/spotify/sign-out` → deletes `app/.spotify_cache`. Use case: switching to a different Spotify account. Does NOT revoke the OAuth grant on Spotify's side; the user has to visit `spotify.com/account/apps` for a full revoke (the dialog text mentions this).

The `/api/config` response includes `spotify_signed_in: bool` derived from `.spotify_cache` file presence — purely a "does the cache exist" check, no API call to Spotify.

### Traktor opt-in checkbox
Persists in `app_config.json` via `app_config.set_sync_to_traktor()`. The legacy `ENABLE_TRAKTOR=1` env var still works as a fallback (in `app_config.get_sync_to_traktor()`) for users upgrading from older versions, but the checkbox is the primary control. The Config card shows a green checkmark + path when on, or grey "Off" when off, with a ⚠️ warning if the resolved Traktor NML path doesn't exist on disk.

### Auto-shutdown (heartbeat)
Frontend's `setInterval(..., 5000)` ping + server's idle watchdog (see Auto-shutdown section above). The launchers set `AUTO_SHUTDOWN_IDLE=20`; manual `python main.py` runs leave it off (env var unset = 0 = disabled). Don't enable this by default — developers running the server for testing don't want it dying when they switch tabs.

## API endpoints (current)

| Method | Path | Purpose |
|---|---|---|
| GET  | `/` | Frontend HTML (Cache-Control: no-cache) |
| POST | `/api/sync` | Start sync (background) |
| GET  | `/api/status` | Sync progress |
| GET  | `/api/playlists` | Playlists from last sync |
| GET  | `/api/playlists/tracked` | All playlists in sync_state.json |
| POST | `/api/playlists/{id}/stop-syncing` | Remove from sync state (does NOT touch RB/Traktor) |
| GET  | `/api/config` | Current config (paths, signed-in flags, platform) |
| POST | `/api/config/music-folder` | Set music folder (appends `/Incoming`) |
| POST | `/api/config/pick-folder` | Native folder dialog (tkinter, subprocess) |
| POST | `/api/config/traktor` | Toggle sync_to_traktor |
| POST | `/api/spotify/sign-out` | Delete .spotify_cache (force re-auth) |
| POST | `/api/heartbeat` | Liveness ping (idle watchdog) |
| GET  | `/api/health` | Health check |
