# Standalone File Fetcher — Product Requirements Document

## Vision
A standalone application that bridges Spotify playlists to Rekordbox and Traktor DJ libraries. Automatically discovers FF-prefixed playlists from Spotify, downloads audio from YouTube, imports tracks to both Rekordbox master.db and Traktor collection.nml, creates/syncs playlists, and auto-launches Rekordbox to analyze new tracks.

**Target User:** DJ SupahFunk — Professional DJ performing with Rekordbox/CDJ-3000 hardware, managing 4,800+ tracks across Rekordbox and Traktor.

---

## Core Pipeline

### 1. Pre-flight Check
- **Check if Rekordbox is running** — refuse to sync if it is, show error to user
- Rekordbox locks master.db; writing while it's open causes corruption or invisible writes

### 2. Discover Playlists
- Connect to Spotify via spotipy (OAuth for private playlists)
- Find all playlists with configurable prefix (default "FF")
- Paginate `current_user_playlists()` to find all playlists
- Use `.get()` for all Spotify API fields — some playlist items may have missing keys

### 3. Duplicate Detection (3-layer, before downloading)
- **Layer 1: Rekordbox DB** — search by Title field across entire master.db, fallback to filename match, then partial artist+title match
- **Layer 2: File index** — recursively scan `D:/Music Backup` (and music folder) for matching filenames at sync start
- **Layer 3: Music folder** — check exact destination path
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
- Files saved as: `"Artist - Title.mp3"` in `D:/Music Backup/Incoming/`

### 5. Import to Rekordbox (UNANALYZED)
- **Rekordbox handles all audio analysis** — we do NOT use librosa for BPM/key/beatgrid
- Create DjmdContent entries in master.db with `Analysed=0` (tells Rekordbox to analyze)
- Do NOT write ANLZ files — Rekordbox creates its own when it analyzes
- **CRITICAL: Set explicit ID** — pyrekordbox does NOT auto-generate IDs. Generate random 9-digit string, verify uniqueness
- **CRITICAL: Set attributes individually** (NOT kwargs — ArtistName is an association proxy)
- **CRITICAL: Must set FileType=1, BitRate=320, SampleRate=44100** — without these Rekordbox shows red ? icons
- **CRITICAL: Set `rb_data_status=0`** and assign sequential `rb_local_usn` — `rb_data_status=1` makes tracks invisible to Rekordbox
- Artist/Album: lookup existing only, do NOT create new DjmdArtist/DjmdAlbum
- `updated_at` must be `datetime` objects, NOT `.isoformat()` strings

### 6. Import to Traktor (UNANALYZED)
- Create ENTRY elements in collection.nml — just file path, artist, title, bitrate
- No BPM, key, or grid anchor — Traktor will analyze on first load
- LOCATION format: VOLUME + DIR (/:separated/:) + FILE
- ATOMIC writes: temp file → parse to verify → backup original → replace

### 7. WAL Checkpoint (CRITICAL)
- After all DB writes, run `PRAGMA wal_checkpoint(TRUNCATE)`
- pyrekordbox uses SQLite WAL mode — writes go to a separate WAL file
- **Rekordbox reads master.db directly and does NOT see WAL contents**
- Without this checkpoint, all imports are invisible to Rekordbox
- Must also `session.close()` and `engine.dispose()` after checkpoint

### 8. Auto-Analysis via Rekordbox GUI
- Only triggers if `tracks_downloaded > 0` (not on reorder-only syncs)
- First checks DB for `Analysed=0` count — if zero, skips entirely
- Launches Rekordbox, waits for window (90s timeout + 15s UI load)
- Clicks Collection → Ctrl+A → right-click → Analyse Track
- Uses pywinauto (UI Automation backend) to find elements, falls back to keyboard shortcuts
- **Does NOT overwrite existing analysis** — only tracks with `Analysed=0` get analyzed
- Tracks already analyzed by user (Analysed=105) are untouched

### 9. Create Playlists
- Create matching playlists in BOTH Rekordbox DB and Traktor NML
- Strip prefix: "FF Opening" → "Opening"
- New playlists at TOP of stack (Seq=0 in Rekordbox, insert at index 0 in NML)

### 10. Sync Ordering
- On each sync, read Spotify track order
- Reorder tracks in both Rekordbox and Traktor playlists to match
- Reordering only touches playlist position (TrackNo) — never touches analysis data

### 11. Handle Additions
- New tracks added to a Spotify playlist get downloaded, imported, added to playlists

### 12. Handle Removals (SAFELY)
- Track removed from Spotify playlist → remove from RB/TK playlists only
- NEVER delete the track from the library
- Playlist no longer prefixed → stop syncing, NEVER delete RB/TK playlist

---

## Safety Rules
1. NEVER delete tracks from Rekordbox/Traktor library — only remove from playlists
2. NEVER delete playlists from Rekordbox/Traktor — only stop syncing
3. NEVER sync while Rekordbox is running — check process list and refuse
4. Always use atomic writes for Traktor NML (temp → verify → backup → replace)
5. Always set DjmdContent attributes individually (not kwargs)
6. Always generate explicit IDs for new Rekordbox DB rows
7. Always set FileType=1, BitRate=320, SampleRate=44100 on DjmdContent
8. Always set rb_data_status=0 and assign rb_local_usn on new content
9. Always flush WAL after DB writes (`PRAGMA wal_checkpoint(TRUNCATE)`)
10. Always strip prefix from playlist names ("FF Opening" → "Opening")
11. New playlists go to Seq=0 (top) in Rekordbox
12. Always pass ffmpeg_location to yt-dlp (PATH alone is unreliable)
13. Auto-analyze only triggers on new downloads, never on reorder-only syncs
14. Auto-analyze never overwrites existing analysis (only Analysed=0 tracks)

---

## Bugs Fixed (Lessons Learned)

| Bug | Root Cause | Fix |
|-----|-----------|-----|
| Spotify `'tracks'` KeyError | Some playlist items have missing fields | Use `.get()` with defaults for all Spotify API fields |
| yt-dlp "Encoder not found" | ffmpeg not found via PATH | Pass `ffmpeg_location` directly in yt-dlp options |
| yt-dlp search crashes on unavailable video | `extract_flat=False` tries to fully extract each search result | Use `extract_flat=True` for search, only extract on download |
| YouTube search fails for long titles | Multi-artist + subtitle makes query too long | Try 5 query variants: full, first artist only, title only |
| Rekordbox import crashes (NULL identity key) | pyrekordbox requires explicit IDs on all tables | Generate random 9-digit string ID, verify uniqueness |
| Artist creation crashes transaction | DjmdArtist needs explicit ID, flush fails and rolls back everything | Don't create new artists — lookup existing only |
| Tracks in Rekordbox show red ? icons | Missing FileType, BitRate, SampleRate fields | Set FileType=1, BitRate=320, SampleRate=44100 |
| Tracks invisible in Rekordbox | `rb_data_status=1` means "pending sync" — RB ignores these | Set `rb_data_status=0` and assign sequential `rb_local_usn` |
| Playlists show 0 tracks in Rekordbox | Writes stuck in WAL file, not checkpointed to master.db | Run `PRAGMA wal_checkpoint(TRUNCATE)` after all writes |
| Duplicate downloads on first run | sync_state.json starts empty, no library check | 3-layer duplicate detection: RB title search + file index + path check |
| Bad BPM/beatgrid from librosa | librosa analysis inferior to Rekordbox for DJ music | Let Rekordbox handle all analysis — import with Analysed=0 |

---

## UI
- Single HTML page served by FastAPI on localhost:8899
- "Sync" button to trigger full pipeline (runs in background thread)
- Live progress polling (1-second interval)
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
│   └── index.html          # Sync UI
├── services/
│   ├── spotify.py          # Spotify OAuth + playlist discovery + track listing
│   ├── downloader.py       # YouTube search + yt-dlp download + ffmpeg convert + ID3 tagging
│   ├── analyzer.py         # librosa analysis (UNUSED — kept for reference, Rekordbox handles analysis)
│   ├── rekordbox.py        # Rekordbox DB import (unanalyzed) + ANLZ writer + WAL flush + playlist CRUD
│   ├── rekordbox_auto.py   # GUI automation: launch Rekordbox + trigger analysis on unanalyzed tracks
│   ├── traktor.py          # Traktor NML import (unanalyzed) + atomic writes + playlist CRUD
│   └── sync.py             # Orchestrator: preflight → discover → dedupe → download → import → playlist → WAL flush → auto-analyze
└── models/
    └── track.py            # TrackInfo + AnalysisResult dataclasses
```

## Dependencies
```
fastapi, uvicorn, spotipy, yt-dlp, mutagen, librosa, numpy,
pyrekordbox, python-dotenv, requests, pyautogui, pygetwindow, pywinauto, psutil
```

## Running
```bash
cd D:/Code/standalone-file-fetcher
pip install -r requirements.txt
python main.py
# Open http://localhost:8899, click Sync
```
