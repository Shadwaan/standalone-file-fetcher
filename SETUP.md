# sff — Setup & Run Instructions

> Quick instructions to get `Standalone File Fetcher` running on a fresh machine, or for handing to a new Claude session.

## What sff does (in one sentence)
Watches your Spotify FF-prefixed playlists, downloads new tracks from YouTube, and imports them into Rekordbox + Traktor with all their playlists/ordering kept in sync — accessed from a local web UI at `http://localhost:8899`.

## TL;DR for a new Claude session

```
The project is at D:/Code/standalone-file-fetcher/. Read PRD.md, CLAUDE.md, and DEBUG_LOG.md
first — they document every architectural decision and every bug we've fixed. Then:

  cd D:/Code/standalone-file-fetcher
  python main.py

Open http://localhost:8899 — click Sync (Spotify → RB/Traktor) or Sync (USB) (RB → USB).
Make sure Rekordbox is CLOSED before running sync. Server logs to the terminal you started
it in.
```

That's it. Always hosts on `localhost:8899` (FastAPI/uvicorn). No remote server, no cloud.

---

## First-time setup (only needed once per machine)

### 1. Clone the repo
```bash
git clone https://github.com/Shadwaan/standalone-file-fetcher.git
cd standalone-file-fetcher
```

### 2. Install Python dependencies
Requires Python 3.13+ on Windows.
```bash
pip install -r requirements.txt
```

This pulls in: fastapi, uvicorn, spotipy, yt-dlp, mutagen, librosa (unused but kept), numpy, pyrekordbox, python-dotenv, requests, psutil, pyautogui, pygetwindow, pywinauto.

### 3. Install ffmpeg
Required by yt-dlp for MP3 conversion.
```bash
winget install Gyan.FFmpeg
```
sff will look for it at:
`C:\Users\<YOU>\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1-full_build\bin`
(if it ends up somewhere else, update the `FFMPEG_DIR` constant in `services/downloader.py`).

### 4. Create a Spotify Developer App and get credentials
- Go to https://developer.spotify.com/dashboard → Create app
- Name it whatever (e.g. "sff")
- Add Redirect URI: `http://127.0.0.1:8888/callback`
- Copy the Client ID and Client Secret

### 5. Set up `.env`
```bash
cp .env.example .env
```
Edit `.env`:
- Paste your Spotify Client ID and Secret
- Update the path constants (`MUSIC_FOLDER`, `REKORDBOX_DB_PATH`, `TRAKTOR_NML_PATH`, `ANLZ_ROOT`) to match your machine.

`.env` is gitignored — it never gets pushed.

### 6. First-run authorization
The first time you click Sync, Spotify will pop open a browser asking you to authorize the app. Allow it. The token is cached at `.spotify_cache` (also gitignored) and auto-refreshes after that.

---

## Running

Always:
```bash
cd D:/Code/standalone-file-fetcher
python main.py
```

Then open `http://localhost:8899` in your browser.

The UI has two buttons:
- **Sync** — Spotify → Rekordbox + Traktor (downloads new tracks, creates playlists)
- **Sync (USB)** — Rekordbox → USB pen drive (greyed out when no USB connected)

### Pre-flight rules

1. **Close Rekordbox before clicking Sync.** sff refuses to write to master.db while Rekordbox is open (it would corrupt or lose writes).
2. **Don't force-quit Rekordbox during analysis.** Force-quit loses unsaved DB commits — your analysis goes to the ANLZ files but the DB row update stays in memory and gets dropped. Click X and let it close itself.
3. **After Sync finishes, open Rekordbox to analyze new tracks** (right-click → Analyse Track on each new playlist).

---

## What's stored where

| Thing | Location | In git? | Secret? |
|---|---|---|---|
| Spotify Client ID/Secret | `.env` | NO (gitignored) | YES |
| OAuth token cache | `.spotify_cache` | NO (gitignored) | refreshable |
| Sync state (per-playlist track tracking) | `sync_state.json` | NO (gitignored) | NO |
| Downloaded MP3s | `D:/Music Backup/Incoming/{Playlist}/...` | NO (outside repo) | NO |
| Rekordbox library | `%APPDATA%/Pioneer/rekordbox/master.db` | NO (Rekordbox-managed) | NO |
| Rekordbox playlist registry | `%APPDATA%/Pioneer/rekordbox/masterPlaylists6.xml` | NO (Rekordbox-managed) | NO |
| Traktor library | `~/Documents/Native Instruments/Traktor X.X.X/collection.nml` | NO | NO |

The only secrets are the two Spotify keys. Everything else is either user paths or runtime state.

---

## Troubleshooting

### "Rekordbox is running. Please close it before syncing."
Close Rekordbox (system tray too — check Task Manager for `rekordbox.exe`). Then retry.

### A sync ran but a new playlist appears empty in Rekordbox
This was the bug fixed in DEBUG_LOG.md section 12. Should not happen on fresh installs anymore (sff now generates 32-bit playlist IDs, sets UUIDs everywhere, registers playlists in `masterPlaylists6.xml`). If it does happen, see DEBUG_LOG.md for the diagnostic procedure.

### A track hangs Rekordbox during analysis
Should not happen with current sff. If it does:
- Verify the track row has UUID set, ArtistID + AlbumID set, BitRate/SampleRate matching the actual MP3 file
- See DEBUG_LOG.md sections 11 and 12

### YouTube download fails with "Encoder not found"
ffmpeg isn't on PATH or sff can't find it. Verify with:
```bash
"C:\Users\<YOU>\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1-full_build\bin\ffmpeg.exe" -version
```
If that doesn't work, reinstall via `winget install Gyan.FFmpeg`.

### Reset sync state (re-check all tracks)
Delete `sync_state.json`. Next sync will re-evaluate every track via the 3-layer dedup (Rekordbox DB title search + file index scan + path check) — won't re-download anything that already exists.

---

## Architecture summary

```
Spotify (FF playlists)
    ↓ spotipy OAuth
sff (FastAPI, localhost:8899)
    ↓ yt-dlp + ffmpeg + mutagen
Local MP3s (D:/Music Backup/Incoming/{playlist}/)
    ↓ pyrekordbox + xml.etree
Rekordbox (master.db + ANLZ + masterPlaylists6.xml)
    ↓ atomic XML write
Traktor (collection.nml)
    ↓ Sync (USB) button
USB pen drive (audio + ANLZ + rekordbox.xml + Traktor NML)
    ↓
CDJ-3000 / XDJ
```

For deep architecture, see PRD.md. For every bug we've hit and fixed, see DEBUG_LOG.md.

---

## Where my secrets are stored locally
At `D:/Code/secrets/standalone-file-fetcher.txt` (outside this repo, machine-local). Never put real Spotify keys in `.env.example` or anywhere committed.
