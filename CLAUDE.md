# AI Assistant Guidelines for Standalone File Fetcher

## Project Context
Standalone app bridging Spotify playlists to Rekordbox + Traktor DJ libraries.
Extracted from DJ File Manager (FM) and DJ File Fetcher codebases into a single self-contained application.

**Read PRD.md first** — it contains the full pipeline spec, safety rules, and bugs-fixed table.

---

## Architecture Decision: Rekordbox Handles Analysis

**We do NOT use librosa for BPM/key/beatgrid analysis.** Rekordbox's built-in analyzer is far superior for DJ music. Our pipeline:
1. Download + tag MP3
2. Import to Rekordbox DB with `Analysed=0` (no ANLZ files)
3. Import to Traktor NML (no BPM/key/grid)
4. Flush WAL
5. Auto-launch Rekordbox → trigger Analyse Track on unanalyzed tracks only

The `analyzer.py` file exists but is NOT used in the sync pipeline. It's kept for reference only.

**Rekordbox has NO auto-analyze on startup and NO CLI for analysis.** The only way to trigger analysis is through the GUI (select tracks → right-click → Analyse Track). We automate this with pywinauto + pyautogui in `rekordbox_auto.py`.

---

## File Organization

Downloads are organized into playlist subfolders:
```
D:/Music Backup/Incoming/
├── Opening/
│   ├── Jo Paciello - Fantasy.mp3
│   └── ...
├── Progressive/
│   ├── Chris Luno - Yes Baby.mp3
│   └── ...
├── Oldies/
│   ├── Paul Johnson - Get Get Down.mp3
│   └── ...
└── Half moon/
    ├── Supernova - Phantascope.mp3
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

### 3. DjmdContent MUST have rb_data_status=0 and rb_local_usn
`rb_data_status=1` means "pending cloud sync" — Rekordbox ignores these tracks entirely:
```python
from sqlalchemy import func as sa_func
max_usn = db.session.query(sa_func.max(tables.DjmdContent.rb_local_usn)).scalar() or 0
content.rb_data_status = 0       # 0 = local data, ready to use
content.rb_local_usn = max_usn + 1  # Sequential USN
```

### 4. WAL checkpoint is MANDATORY after writes
pyrekordbox uses SQLite WAL mode. Writes go to a WAL file, NOT master.db. Rekordbox reads master.db directly and NEVER sees WAL contents:
```python
from sqlalchemy import text
db.session.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
db.session.commit()
db.session.close()
db.engine.dispose()
```
**Without this, all your imports are invisible to Rekordbox.** This was the cause of playlists showing 0 tracks.

### 5. Do NOT create new DjmdArtist or DjmdAlbum rows
Creating new artist/album rows requires explicit IDs and crashes the session on flush.
Instead: lookup existing artist by name, and if not found, embed artist in Title field:
```python
artist_obj = db.session.query(tables.DjmdArtist).filter_by(Name=track.artist).first()
if artist_obj:
    content.ArtistID = artist_obj.ID
else:
    content.Title = f"{track.artist} - {track.title}"
```

### 6. yt-dlp needs ffmpeg_location (not just PATH)
Setting `os.environ["PATH"]` is unreliable. Pass the path directly:
```python
ydl_opts = {
    "ffmpeg_location": r"C:\Users\Lenovo\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1-full_build\bin",
    ...
}
```

### 7. yt-dlp search must use extract_flat=True
Using `extract_flat=False` for search causes yt-dlp to fully extract each result. If ANY result is an unavailable video, the entire search crashes:
```python
ydl_opts = {
    "extract_flat": True,  # Only get metadata, not full extraction
    "default_search": "ytsearch5",
    ...
}
```

### 8. Spotify API fields can be missing
Always use `.get()` with defaults:
```python
for item in results.get("items", []):
    if not item or not item.get("name"):
        continue
    tracks_info = item.get("tracks") or {}
    track_count = tracks_info.get("total", 0)
```

### 9. DjmdContent attributes must be set individually
ArtistName is an association proxy — passing it as a kwarg to the constructor crashes:
```python
# WRONG: tables.DjmdContent(Title="...", ArtistName="...")  # CRASHES
# RIGHT:
content = tables.DjmdContent()
content.Title = "..."
content.ArtistID = artist_obj.ID
```

### 10. NEVER write to master.db while Rekordbox is running
Check process list before sync:
```python
import psutil
for proc in psutil.process_iter(['name']):
    if 'rekordbox' in proc.info['name'].lower():
        raise RuntimeError("Close Rekordbox before syncing")
```

### 11. Import with Analysed=0 — NEVER use librosa
Rekordbox analysis is superior. Import tracks as unanalyzed and let Rekordbox handle BPM/key/beatgrid:
```python
content.Analysed = 0  # Rekordbox will analyze
# Do NOT write ANLZ files
# Do NOT set BPM or KeyID
```

---

## Rekordbox Integration Rules

### DjmdContent Creation (Unanalyzed Import)
- Set `.ID` = random 9-digit string (verify unique)
- Set `.FolderPath` = forward-slash normalized path (including playlist subfolder)
- Set `.Title`, `.FileNameL`, `.FileSize`
- Set `.FileType` = 1, `.BitRate` = 320, `.SampleRate` = 44100
- Set `.Analysed` = 0 (Rekordbox will analyze)
- Do NOT set BPM, KeyID, or AnalysisDataPath — Rekordbox handles these
- Do NOT write ANLZ files — Rekordbox creates its own
- Set `.rb_data_status` = 0, assign sequential `.rb_local_usn`
- Set `.updated_at` = `datetime.now(timezone.utc)` (NOT isoformat string)

### WAL Flush (after ALL DB operations)
```python
from sqlalchemy import text
db.session.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
db.session.commit()
db.session.close()
db.engine.dispose()
```

### Playlist Management
- DjmdPlaylist: Seq=0 for top, bump existing Seq values up by 1
- DjmdSongPlaylist: TrackNo = 1-based position
- All playlist/song rows need explicit `.ID` (random 9-10 digit string)
- Playlists also need `rb_data_status=0` and `rb_local_usn` assigned

### Auto-Analysis via GUI Automation (rekordbox_auto.py)
- Only triggers when `tracks_imported > 0` or `tracks_downloaded > 0`
- Checks `Analysed=0` count first — if zero, doesn't launch Rekordbox
- Uses pywinauto (UIA backend) to find UI elements, falls back to pyautogui keyboard shortcuts
- Clicks Collection → Ctrl+A → right-click → Analyse Track
- **Never overwrites existing analysis** — only Analysed=0 tracks get processed
- **Never triggers on reorder-only syncs** (no new imports = no Rekordbox launch)
- Rekordbox exe: `C:\Program Files\Pioneer\rekordbox 6.8.5\rekordbox.exe`

---

## Traktor NML Writer Rules
- ATOMIC writes: write to temp → parse to verify → backup .bak → shutil.move to replace
- LOCATION: `VOLUME` + `DIR` (/:separated/:) + `FILE`
- Import tracks unanalyzed — just file entry with artist, title, bitrate
- Traktor will analyze on first load
- New playlists: insert NODE at index 0 of root PLAYLISTS node

---

## Duplicate Detection (3-layer)
Before downloading any track:
1. **Rekordbox DB**: `find_content_by_title(artist, title)` — searches Title field, filename pattern, partial match
2. **File index**: `_build_file_index()` — scans D:/Music Backup recursively at sync start, builds lowercase filename → path map (includes all playlist subfolders)
3. **Music folder**: checks `Incoming/{PlaylistName}/track.filename` then `Incoming/track.filename`

If any layer matches, skip download. If Rekordbox match found, just add existing track to playlist.

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
4. Only REMOVE tracks from playlists, or STOP syncing playlists
5. Always use atomic writes for Traktor NML
6. Always flush WAL after DB writes
7. Auto-analyze only when new tracks exist, never on reorder-only syncs
8. Auto-analyze never overwrites existing analysis (only Analysed=0)
9. Never use librosa for analysis — Rekordbox handles it

---

## Running
```bash
cd D:/Code/standalone-file-fetcher
pip install -r requirements.txt
python main.py
# Open http://localhost:8899, click Sync
```

## State
- `sync_state.json` — tracks which Spotify IDs have been processed per playlist, stores file paths
- `.spotify_cache` — Spotify OAuth token (auto-refreshes)
- Clearing `sync_state.json` forces re-check of all tracks (but duplicate detection prevents re-downloads)

## Dependencies
```
pip install fastapi uvicorn spotipy yt-dlp mutagen librosa numpy pyrekordbox python-dotenv requests psutil pyautogui pygetwindow pywinauto
```

---

## Next Phase: USB Export (NOT YET BUILT)

Automate Rekordbox's "Export to Device" via GUI automation to export FF playlists to USB with Device Library Plus + legacy PDB support. See PRD.md "Next Phase" section for full spec.

Reference code: `D:/Code/DJ/DJ File Manager/backend/services/usb_exporter.py` has USB drive detection, ANLZ writing, Rekordbox XML export, and Traktor NML export. But prefer Rekordbox's native export for Device Library Plus format — automate the GUI rather than writing the DB format ourselves.

Key: must poll `Analysed=0` count to wait for Rekordbox analysis to finish before triggering export.
