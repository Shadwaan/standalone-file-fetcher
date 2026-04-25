# Standalone File Fetcher — Product Requirements Document

## Vision
A standalone application that bridges Spotify playlists to Rekordbox and Traktor DJ libraries. Automatically discovers FF-prefixed playlists from Spotify, downloads audio from YouTube, imports tracks to both Rekordbox master.db and Traktor collection.nml, creates/syncs playlists, and optionally exports all FF playlists to USB pen drive for CDJ hardware.

**Target User:** DJ SupahFunk — Professional DJ performing with Rekordbox/CDJ-3000 hardware, managing 4,800+ tracks across Rekordbox and Traktor.

---

## Core Pipeline

### 1. Pre-flight Check
- **Check if Rekordbox is running** — refuse to sync if it is, show error to user
- Rekordbox locks master.db; writing while it's open causes corruption or invisible writes
- Rekordbox open also creates its own WAL which can conflict with our writes

### 2. Discover Playlists
- Connect to Spotify via spotipy (OAuth for private playlists)
- Find all playlists with configurable prefix (default "FF")
- Paginate `current_user_playlists()` to find all playlists
- Use `.get()` for all Spotify API fields — some playlist items may have missing keys

### 3. Duplicate Detection (3-layer, before downloading)
**CRITICAL:** All 3 strategies MUST match BOTH artist AND title — never title alone (title-alone matching caused "Dreamer" by Four Tet to match a different "Dreamer").
- **Layer 1: Rekordbox DB** — `find_content_by_title(artist, title)`:
  - Strategy A: Exact filename match `"Artist - Title"`
  - Strategy B: Title field match + verify artist is in filepath OR ArtistID
  - Strategy C: Partial filename match — both artist AND title in filename
- **Layer 2: File index** — recursively scan `D:/Music Backup` (and music folder + playlist subfolders) for matching filenames at sync start
- **Layer 3: Music folder** — check playlist subfolder path, then root music folder path
- Only download if ALL three layers find no match
- If Rekordbox match found, skip download and just add existing track to playlist

### 4. Download New Tracks
- For each new track, search YouTube via yt-dlp with 5 query strategies:
  - `"Artist - Title"`, `"FirstArtist - Title"`, `"FirstArtist Title"`, `"Title FirstArtist"`, `"Title"`
- Use `extract_flat=True` for search (avoids crashing on unavailable videos in search results)
- Duration matching within 30-second tolerance
- Convert to MP3 320kbps via ffmpeg
- **CRITICAL:** Must pass `ffmpeg_location` directly to yt-dlp options — PATH alone is unreliable
- ffmpeg installed at: `C:\Users\Lenovo\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1-full_build\bin`
- Tag with ID3 metadata from Spotify (artist, title, album, artwork via mutagen)
- **Files organized by playlist**: saved to `D:/Music Backup/Incoming/{PlaylistName}/Artist - Title.mp3`

### 5. Import to Rekordbox (UNANALYZED)
- **Rekordbox handles all audio analysis** — we do NOT use librosa for BPM/key/beatgrid
- Create DjmdContent entries in master.db with `Analysed=0` (tells Rekordbox to analyze)
- Do NOT write ANLZ files — Rekordbox creates its own when it analyzes
- **CRITICAL: Set explicit ID** — pyrekordbox does NOT auto-generate IDs. Generate random 9-digit string, verify uniqueness
- **CRITICAL: Set attributes individually** (NOT kwargs — ArtistName is an association proxy)
- **CRITICAL: Create proper DjmdAlbum and DjmdArtist rows when needed** — see "Drag-import parity" rule below. Without this, Rekordbox batch analysis hangs on the second track.

### 5a. Drag-import parity rule (CRITICAL — updated 2026-04-25 r2)
**Verified by DB row diff between drag-imported (works) and sff-imported (hangs) tracks.** sff was missing many fields that drag-import sets. The CRITICAL ones:

1. **`UUID`** — must be set with `str(uuid.uuid4())`. Rekordbox uses this to build the ANLZ analysis file path: `/PIONEER/USBANLZ/{uuid[:3]}/{uuid[3:]}/ANLZ0000.DAT`. Without UUID, Rekordbox falls back to the broken sentinel path `/PIONEER/USBANLZ///ANLZ0018.DAT` (triple slashes — empty UUID). Multiple tracks collide on this path, causing batch analysis hangs and wrong artwork.
2. **`ArtistID`** must point to a real DjmdArtist row (use `_get_or_create_artist`, which now also sets UUID on the artist row)
3. **`AlbumID`** must point to a real DjmdAlbum row (use `_get_or_create_album`, which now also sets UUID on the album row)
4. **`HotCueAutoLoad='on'`**, **`DeliveryControl='on'`**, **`StockDate`**, **`DateCreated`**, **`ColorID='0'`**, **`DJPlayCount=0`**, **`DiscNo=0`**, **`Rating=0`**, **`TrackNo=0`** — drag-import sets these; sff was leaving them None. Some are functional (HotCueAutoLoad), some are display defaults.

Without these, sff-imported tracks:
- Analyze SLOWLY on first attempt (Rekordbox struggles to write the broken ANLZ path)
- HANG on second sequential analysis (path collision or broken state from first analysis)

Playlist position information lives in `masterPlaylists6.xml`, NOT just master.db. Don't touch that file. When a deleted track is re-added (e.g. via drag-import), Rekordbox cross-references this XML and restores the original TrackNo automatically.
- **CRITICAL: Must set FileType=1, BitRate=320, SampleRate=44100** — without these Rekordbox shows red ? icons
- **CRITICAL: Set `rb_data_status=0`** and assign sequential `rb_local_usn` — `rb_data_status=1` makes tracks invisible to Rekordbox
- **CRITICAL: PLAYLISTS ALSO need `rb_data_status=0` + `rb_local_usn`** — if only content has this, playlist shows empty even though rows exist in DjmdSongPlaylist
- Artist/Album: lookup existing only, do NOT create new DjmdArtist/DjmdAlbum
- `updated_at` must be `datetime` objects, NOT `.isoformat()` strings

### 6. Import to Traktor (UNANALYZED)
- Create ENTRY elements in collection.nml — just file path, artist, title, bitrate
- No BPM, key, or grid anchor — Traktor will analyze on first load
- LOCATION format: VOLUME + DIR (/:separated/:) + FILE
- ATOMIC writes: temp file → parse to verify → backup original → replace

### 7. WAL Checkpoint (CRITICAL — DOUBLE FLUSH REQUIRED)
- After all DB writes, run `PRAGMA wal_checkpoint(TRUNCATE)` **TWICE**
- pyrekordbox uses SQLite WAL mode — writes go to a separate WAL file
- **Rekordbox reads master.db directly and does NOT see WAL contents**
- Without this checkpoint, all imports are invisible to Rekordbox (playlists show 0 tracks)
- pyrekordbox opens a new DB connection per operation, each creating WAL entries
- First checkpoint flushes existing writes; second catches any WAL entries created by the first
- Must `session.close()` and `engine.dispose()` between the two flushes
- Verify WAL file size is 0 bytes after — if not, Rekordbox won't see changes
- If Rekordbox is open during/after flush, it creates its OWN WAL which can mask our writes — close Rekordbox and reopen to force it to reload master.db

### 8. Auto-Analysis (CURRENTLY DISABLED)
- **Auto-analyze via GUI automation is DISABLED** because Rekordbox Ctrl+A selects ALL tracks in Collection, causing it to re-analyze already-analyzed tracks
- No way to filter/select only unanalyzed tracks via GUI automation
- After sync, UI shows: "Sync complete. N tracks need analysis. Open Rekordbox → select new FF playlists → Analyse Track"
- User manually analyzes per-playlist (in Rekordbox: click playlist → Ctrl+A → right-click → Analyse Track)
- This way only that playlist's tracks are touched, not the entire library
- Rekordbox has NO auto-analyze on startup and NO CLI — manual per-playlist is the only safe approach
- `rekordbox_auto.py` and `_count_unanalyzed()` still exist for detecting how many tracks need analysis

### 9. Create Playlists
- Create matching playlists in BOTH Rekordbox DB and Traktor NML
- Strip prefix: "FF Opening" → "Opening"
- New playlists at TOP of stack (Seq=0 in Rekordbox, insert at index 0 in NML)
- **CRITICAL: Playlist row needs rb_data_status=0 AND rb_local_usn assigned**
- Songs within playlist (DjmdSongPlaylist) can have rb_data_status=1 — that's how working playlists look

### 10. Sync Ordering
- On each sync, read Spotify track order
- Reorder tracks in both Rekordbox and Traktor playlists to match
- Reordering only touches playlist position (TrackNo) — never touches analysis data

### 11. Handle Additions
- New tracks added to a Spotify playlist get downloaded, imported, added to playlists
- Tracks already in Rekordbox library are detected and just added to the playlist (no re-download)

### 12. Handle Removals (SAFELY)
- Track removed from Spotify playlist → remove from RB/TK playlists only
- NEVER delete the track from the library
- Playlist no longer prefixed → stop syncing, NEVER delete RB/TK playlist

### 13. USB Export (BUILT)
- "Sync (USB)" button in the UI next to main Sync button
- Greyed out with "Drive not connected" when no USB detected
- Polls every 5 seconds for USB drive presence
- When USB detected, shows drive name + free space
- On click:
  - Reads FF playlists from Rekordbox master.db (identified by tracks in music folder path)
  - Creates `USB:/PIONEER/USBANLZ/`, `USB:/PIONEER/rekordbox/`, `USB:/Contents/` directories
  - Copies audio files to `USB:/Contents/{PlaylistName}/`
  - Skips copy if destination file exists with same size
  - Copies ANLZ files from local `%APPDATA%/Pioneer/rekordbox/share/PIONEER/USBANLZ/` (if track is analyzed)
  - **Rewrites PPTH tags inside ANLZ to use USB-relative paths** (`Contents/{Playlist}/{file}`)
  - Writes `USB:/PIONEER/rekordbox/rekordbox.xml` with all playlists + tracks for CDJ import
- Uses direct USB write approach (does NOT use Rekordbox GUI automation for export)
- Limitation: does NOT create Device Library Plus format (`exportLibrary.db`) — only Rekordbox's native export does that

---

## USB Drive Detection

### ⚠️ KNOWN UNRESOLVED ISSUE
**USB detection is NOT robust.** It currently uses a hardcoded skip list of `{"C:", "D:", "E:"}` on this machine because there's no reliable way with stdlib to distinguish a removable USB drive from an SSD partition. The code does NOT actually check if a drive is removable — it just lists all drive letters that exist and filters by letter.

**What goes wrong:**
- E: is an SSD partition on this machine, not a removable drive
- The code still detects E: as "connected USB" because it just checks `os.path.exists("E:/")`
- Current workaround: `SKIP_DRIVES = {"C:", "D:", "E:"}` in `services/usb_detect.py`
- This only works on THIS machine — breaks if user has a real USB at E:, or has different partition layout

**Proper fix (not yet implemented):**
Use Windows API via `ctypes` to check drive type:
```python
import ctypes
drive_type = ctypes.windll.kernel32.GetDriveTypeW(f"{letter}:\\")
# 2 = DRIVE_REMOVABLE (USB stick, SD card)
# 3 = DRIVE_FIXED (hard drive, SSD)
# 4 = DRIVE_REMOTE (network)
# 5 = DRIVE_CDROM
# Only accept drive_type == 2
```
This would let us auto-detect actual USB drives regardless of letter, and not need a hardcoded skip list.

### Current Implementation
- Scans drive letters E: through Z: on Windows
- **Hardcoded skip: C:, D:, E:** — C: is system, D: is music storage, E: is SSD partition (fragile)
- Uses `shutil.disk_usage()` for size info
- Uses `wmic logicaldisk` to get volume name
- Checks for existing PIONEER/ folder to flag as "has_rekordbox"

---

## File Organization
- Downloads go to `D:/Music Backup/Incoming/{PlaylistName}/Artist - Title.mp3`
- Each FF playlist gets its own subfolder
- Duplicate detection scans all subfolders recursively
- Rekordbox FolderPath stores the full path including playlist subfolder

---

## Safety Rules
1. NEVER delete tracks from Rekordbox/Traktor library — only remove from playlists
2. NEVER delete playlists from Rekordbox/Traktor — only stop syncing
3. NEVER sync while Rekordbox is running — check process list and refuse
4. NEVER match duplicates by title alone — always require artist+title match
5. Always use atomic writes for Traktor NML (temp → verify → backup → replace)
6. Always set DjmdContent attributes individually (not kwargs)
7. Always generate explicit IDs for new Rekordbox DB rows (content, playlist, song_playlist)
8. Always set FileType=1, BitRate=320, SampleRate=44100 on DjmdContent
9. Always set rb_data_status=0 and assign rb_local_usn on new content AND new playlists
10. Always flush WAL TWICE after DB writes (`PRAGMA wal_checkpoint(TRUNCATE)`)
11. Always verify WAL file is 0 bytes after flush — if not, Rekordbox won't see changes
12. Always strip prefix from playlist names ("FF Opening" → "Opening")
13. New playlists go to Seq=0 (top) in Rekordbox, bump existing Seq values up
14. Always pass ffmpeg_location to yt-dlp (PATH alone is unreliable)
15. Never use librosa for analysis — Rekordbox handles it
16. Never write ANLZ files during import — Rekordbox creates them during its analysis
17. Rewrite PPTH paths in ANLZ files when copying to USB (must be USB-relative)
18. Download into playlist subfolders, not flat in Incoming root
19. Skip C:, D:, E: in USB detection (system + music + partition)
20. **NEVER set AlbumID=None or ArtistID=None on DjmdContent** — create proper Album/Artist rows. Verified by drag-import test: missing these makes Rekordbox batch analysis hang.
21. **Never modify masterPlaylists6.xml** — Rekordbox uses this file for playlist position recovery. Touching it could destroy playlist ordering across the entire library.

---

## Bugs Fixed (Lessons Learned)

| Bug | Root Cause | Fix |
|-----|-----------|-----|
| Spotify `'tracks'` KeyError | Some playlist items have missing fields | Use `.get()` with defaults for all Spotify API fields |
| yt-dlp "Encoder not found" | ffmpeg not found via PATH | Pass `ffmpeg_location` directly in yt-dlp options |
| yt-dlp search crashes on unavailable video | `extract_flat=False` fully extracts each search result | Use `extract_flat=True` for search, only extract on download |
| YouTube search fails for long titles | Multi-artist + subtitle makes query too long | Try 5 query variants: full, first artist only, title only |
| Rekordbox import crashes (NULL identity key) | pyrekordbox requires explicit IDs on all tables | Generate random 9-digit string ID, verify uniqueness |
| ~~Artist creation crashes transaction~~ (SUPERSEDED) | DjmdArtist needs explicit ID, flush rolls back everything | ~~Don't create new artists — lookup existing only~~ Old fix caused worse bug below. New fix: assign explicit 9-10 digit ID + rb_data_status=0 + rb_local_usn, then create the row. |
| Tracks in Rekordbox show red ? icons | Missing FileType, BitRate, SampleRate fields | Set FileType=1, BitRate=320, SampleRate=44100 |
| Content invisible in Rekordbox | `rb_data_status=1` means "pending sync" — RB ignores these | Set `rb_data_status=0` and assign sequential `rb_local_usn` |
| **Playlist shows empty even with songs in DjmdSongPlaylist** | Playlist row had `rb_data_status=1` but content was fine | Set `rb_data_status=0` and `rb_local_usn` on DjmdPlaylist too, not just DjmdContent |
| Playlists show 0 tracks in Rekordbox | Writes stuck in WAL file, not checkpointed to master.db | Run `PRAGMA wal_checkpoint(TRUNCATE)` after all writes |
| **Single WAL flush insufficient** | pyrekordbox opens new connection per call, each creating WAL | Double flush: checkpoint, close, dispose, open new session, checkpoint again |
| **Rekordbox open during/after sync masks changes** | Rekordbox creates its own WAL and loads master.db into memory on start | Close Rekordbox completely, reopen after sync completes |
| Duplicate downloads on first run | sync_state.json starts empty, no library check | 3-layer duplicate detection: RB title search + file index + path check |
| **Wrong tracks matched as duplicates** | Strategy 1 matched by title alone ("Dreamer" by Four Tet matched different Dreamer) | All 3 strategies now require BOTH artist AND title match |
| Bad BPM/beatgrid from librosa | librosa analysis inferior to Rekordbox for DJ music | Let Rekordbox handle all analysis — import with Analysed=0 |
| Rekordbox won't re-analyze imported tracks | Old librosa ANLZ files + Analysed=105 make RB think they're done | Import with Analysed=0, never write ANLZ files |
| **Auto-analyze re-analyzed ENTIRE library** | Ctrl+A in Collection selects all 5000+ tracks, RB re-analyzes all | DISABLED auto-analyze; user analyzes per-playlist manually |
| Auto-analyze didn't trigger | Checked `tracks_downloaded > 0` but files existed (skipped download) | (Moot — auto-analyze now disabled) |
| All files dumped in one folder | No organization by playlist | Download into `Incoming/{PlaylistName}/` subfolders |
| **E: drive detected as USB** | E: is SSD partition, was matched by drive letter scan | Added E: to SKIP_DRIVES along with C: and D: |
| **Rekordbox batch analysis hangs on 2nd track for sff-imported tracks** (2026-04-25) | First diagnosis (incomplete): sff was creating rows with `AlbumID=None`/`ArtistID=None`. **Real cause (after row diff with drag-imported track): sff was ALSO missing `UUID` on DjmdContent and DjmdAlbum.** Rekordbox uses UUID to build the ANLZ analysis path. Without UUID, Rekordbox falls back to broken sentinel path `/PIONEER/USBANLZ///ANLZ0018.DAT`, multiple tracks collide on it, batch analysis hangs. | (1) Create proper DjmdAlbum + DjmdArtist rows. (2) Generate `UUID = uuid.uuid4()` on every new DjmdContent, DjmdArtist, DjmdAlbum row. (3) Set drag-import-parity fields: HotCueAutoLoad='on', DeliveryControl='on', StockDate, DateCreated, ColorID='0', etc. |
| **Last-mile hang: SampleRate mismatch** (2026-04-25 r3) | After fixing UUIDs/Album/Artist, 5 tracks (Horny + 4 Opening) still got slow/stuck on analysis. Diagnosis via row-inspect: DB had `SampleRate=44100` (sff hardcoded), actual MP3 files were `48000 Hz` (yt-dlp output). Rekordbox reads file SR, sees mismatch with DB, struggles. Velvet Avenue (works) had matching SR=48000. | Read actual SampleRate from file via `mutagen.mp3.MP3.info.sample_rate` and store that in DB instead of hardcoded 44100. Same for BitRate. Also set FileNameL from path (was None). |
| **"Why only after MM force-quit?"** (unresolved theory) | sff was always producing broken metadata, but Rekordbox tolerated small batches. MM (90 tracks) + the now-disabled auto-analyze GUI loop pushed it past tolerance, force-quit corrupted RB process state, hangs became persistent. | Fix the source (sff's missing Album/Artist creation). |

---

## UI
- Single HTML page served by FastAPI on localhost:8899
- **Two buttons side by side**:
  - "Sync" (green) — triggers Spotify → Rekordbox/Traktor sync
  - "Sync (USB)" (blue) — exports FF playlists to USB. Greyed out with "Drive not connected" when no USB. Shows "{Volume Name} ({Letter}:) — {N} GB free" when detected.
- Live progress polling (1-second interval during operations)
- USB drive polling every 5 seconds
- Shows: sync status, download/import/skip/fail counts, playlist list with track counts, errors, last sync time
- Config display: prefix, music folder, Rekordbox/Traktor connection status

---

## Architecture

```
standalone-file-fetcher/
├── PRD.md                  # This file
├── CLAUDE.md               # AI assistant guidelines
├── .env                    # Spotify creds + paths (DO NOT COMMIT)
├── .spotify_cache           # Spotify OAuth token cache
├── sync_state.json          # Tracks what's been synced (persists between runs)
├── requirements.txt
├── main.py                 # FastAPI server on port 8899
├── frontend/
│   └── index.html          # Sync UI (dual buttons: Sync + Sync USB)
├── services/
│   ├── spotify.py          # Spotify OAuth + playlist discovery + track listing
│   ├── downloader.py       # YouTube search + yt-dlp download + ffmpeg convert + ID3 tagging
│   ├── analyzer.py         # librosa analysis (UNUSED — kept for reference, Rekordbox handles analysis)
│   ├── rekordbox.py        # Rekordbox DB import (unanalyzed) + ANLZ writer (legacy) + WAL flush + playlist CRUD + artist+title duplicate detection
│   ├── rekordbox_auto.py   # GUI automation helpers (auto-analyze DISABLED; _count_unanalyzed still used)
│   ├── traktor.py          # Traktor NML import (unanalyzed) + atomic writes + playlist CRUD
│   ├── usb_detect.py       # USB drive detection (skips C:, D:, E:)
│   ├── usb_export.py       # USB export: audio copy, ANLZ copy with PPTH rewrite, Rekordbox XML write
│   └── sync.py             # Orchestrator: preflight → discover → dedupe → download → import → playlist → WAL flush (double)
└── models/
    └── track.py            # TrackInfo + AnalysisResult dataclasses
```

## Dependencies
```
fastapi, uvicorn, spotipy, yt-dlp, mutagen, librosa, numpy,
pyrekordbox, python-dotenv, requests, psutil,
pyautogui, pygetwindow, pywinauto
```

## Running
```bash
cd D:/Code/standalone-file-fetcher
pip install -r requirements.txt
python main.py
# Open http://localhost:8899, click Sync or Sync (USB)
```

---

## Recovery Commands (when things go wrong)

### Flush WAL manually (if Rekordbox shows empty playlists)
```python
cd "D:/Code/standalone-file-fetcher" && python -c "
from dotenv import load_dotenv; load_dotenv()
from pyrekordbox import Rekordbox6Database
from sqlalchemy import text
for _ in range(2):
    db = Rekordbox6Database()
    db.session.execute(text('PRAGMA wal_checkpoint(TRUNCATE)'))
    db.session.commit()
    db.session.close()
    db.engine.dispose()
"
```

### Fix playlists with rb_data_status=1
```python
# See services/rekordbox.py find_or_create_playlist — ensures rb_data_status=0
# To manually fix: iterate DjmdPlaylist, set rb_data_status=0 and assign rb_local_usn
```

### Reset tracks to unanalyzed (if librosa ANLZ files are polluting)
```python
# Set Analysed=0, BPM=0, KeyID=None, AnalysisDataPath=None
# Delete corresponding ANLZ directory under %APPDATA%/Pioneer/rekordbox/share/PIONEER/USBANLZ/
```
