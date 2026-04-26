# sff — Contributor / Deep Setup Notes

> **End users:** see [`README.md`](../README.md) at the repo root. Just double-click `start.command` (Mac) or `start.bat` (Windows) — the launchers handle Python venv + dependency install on first run.
>
> **This file** is the deep version: how to run the server manually for development, troubleshooting, the file layout, and notes for new contributors / Claude sessions.

## What sff does (in one sentence)
Watches your Spotify FF-prefixed playlists, downloads new tracks from YouTube, and imports them into Rekordbox (and optionally Traktor) with all their playlists/ordering kept in sync — accessed from a local web UI at `http://localhost:8899`.

## Supported platforms
- **Windows 10/11** — fully supported (original target)
- **macOS** (Intel + Apple Silicon) — supported (Rekordbox 7 verified)
- **Linux** — untested (no Rekordbox/Traktor available, only the download path is meaningful)

## Repo layout (post-restructure)

```
/
├── README.md          ← end-user instructions
├── start.command      ← Mac launcher (auto-bootstraps venv + runs server)
├── start.bat          ← Windows launcher (same)
└── app/               ← all the technical code lives here
    ├── main.py            ← FastAPI server entry
    ├── requirements.txt
    ├── .env.example       ← copy to .env, paste Spotify keys
    ├── services/          ← sync, downloader, rekordbox, traktor, platform_paths, app_config
    ├── frontend/          ← single index.html SPA
    ├── models/            ← dataclasses (TrackInfo, AnalysisResult)
    ├── SETUP.md           ← this file
    ├── CLAUDE.md          ← architecture & critical bugs to avoid
    ├── PRD.md             ← product spec
    └── DEBUG_LOG.md       ← every bug we've hit and fixed
```

The launchers `cd app/` before doing anything, so all Python paths inside the app are unaffected by the restructure.

## TL;DR for a new Claude session

```
The project lives in app/ — all Python code, requirements, docs.
Launchers (start.command, start.bat) are at the repo root.

Read app/PRD.md, app/CLAUDE.md, and app/DEBUG_LOG.md first — they document
every architectural decision and every bug we've fixed. Then:

  cd app/
  .venv/bin/python main.py    # macOS / Linux
  .venv\Scripts\python.exe main.py  # Windows

Or just double-click start.command / start.bat from the repo root.

Open http://localhost:8899 — click Sync. Make sure Rekordbox is CLOSED.
```

That's it. Always hosts on `localhost:8899` (FastAPI/uvicorn). No remote server, no cloud.

---

## First-time setup (only needed once per machine)

### 1. Clone the repo
```bash
git clone https://github.com/Shadwaan/standalone-file-fetcher.git
cd standalone-file-fetcher
```

### 2. Install Python (3.10+)

| Platform | How |
|---|---|
| Windows | Python 3.13 from [python.org](https://www.python.org/downloads/) (or `winget install Python.Python.3.13`) |
| macOS | `brew install python@3.13 python-tk@3.13` |

> **Mac note:** the bare `python@3.13` brew formula does NOT include tkinter, which sff uses for the native folder-picker dialog. Installing `python-tk@3.13` alongside fixes that. If you skip it, the Browse button in the UI still falls back to the manual path-input field — it just isn't a native dialog.

### 3. Install Python dependencies

Create a venv and install (recommended):
```bash
# Windows
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# macOS
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

This pulls in: fastapi, uvicorn, spotipy, yt-dlp, mutagen, numpy, pyrekordbox, python-dotenv, requests, psutil.

### 4. Install ffmpeg
Required by yt-dlp for MP3 conversion.

| Platform | How |
|---|---|
| Windows | `winget install Gyan.FFmpeg` |
| macOS | `brew install ffmpeg` |

sff auto-detects ffmpeg via PATH and common install locations. The Config panel in the UI shows where it found it (or "Not found" if the install is missing). On Windows, sff also searches the WinGet install path; on Mac, it checks `/opt/homebrew/bin`, `/usr/local/bin`, and `~/.local/homebrew/bin`.

### 5. Spotify auth — PKCE flow with shared dev app
sff uses Spotify's **PKCE (Proof Key for Code Exchange)** flow. There's no Client Secret in the code — security comes from a per-session cryptographic challenge instead. The Client ID is hardcoded in `app/services/spotify.py` (it's safe to ship publicly — Spotify designed Client IDs to be public).

**End users never need to create a Spotify dev app.** They just click Sync and authorize sff in their browser.

**You (the owner of this fork) DO need a Spotify dev app**, since you're publishing it. The current code uses Client ID `ee8d13f0effb403ca47b7fe518b55633` — that's the existing one. Required app settings:
- Redirect URI: `http://127.0.0.1:8888/callback` (already there)
- User allowlist: in Development Mode, only emails you've added in **Settings → User Management** can authorize. To onboard a new user, add their Spotify email there.
- **Hard cap: 5 named users.** Spotify reduced this from 25 in May 2025, and they no longer accept Extended Quota Mode applications from individuals — only registered businesses with 250k+ MAU. For personal/small-group projects this is effectively permanent.
- **App owner needs Spotify Premium** to keep dev-mode apps active.
- **Past 5 users:** rotate the allowlist (drop one to add another), OR have additional users create their own Spotify dev app and override `SPOTIFY_CLIENT_ID` in their `app/.env`. Each user becomes the sole occupant of their own dev-mode bubble. See README.md step 3.

To swap to your own Client ID later: edit `DEFAULT_CLIENT_ID` in `app/services/spotify.py`, or set `SPOTIFY_CLIENT_ID=...` in `app/.env`.

### 6. `.env` is now optional
Most users won't have an `.env` at all. The launchers and UI handle:
- Spotify auth (PKCE, no keys needed)
- Music folder (UI on first run + Config card)
- Traktor opt-in (Config card → Sync to Traktor checkbox)
- Rekordbox/Traktor/ffmpeg paths (auto-detected per platform)

`.env` exists only for power-user overrides. See `app/.env.example` for the available knobs (Client ID swap, custom paths, custom prefix).

### 7. First launch + first-run setup
```bash
# Windows: double-click start.bat — OR — terminal:
python main.py

# macOS: terminal:
.venv/bin/python main.py    # if using venv
# or just: python3 main.py
```

Open `http://localhost:8899`. A modal pops up asking where to download music — pick any parent folder, sff appends `/Incoming` (so `~/Music` → `~/Music/Incoming`). The choice is saved to `app_config.json` (gitignored). Change it any time via the **Change…** link in the Config panel.

### 8. First-run Spotify authorization
The first time you click Sync, Spotify will pop open a browser asking you to authorize the app. Allow it. The token is cached at `.spotify_cache` (also gitignored) and auto-refreshes after that.

---

## Running

### Easiest: double-click the launcher

| Platform | File | Notes |
|---|---|---|
| Windows | `start.bat` | Opens a console window with logs, starts the server, opens your default browser to `http://localhost:8899` after 3 seconds. **Close the browser tab and the server + console close themselves automatically** (~20s after the tab closes). |
| macOS | `start.command` | Same behavior in Terminal. **Close the browser tab and the Terminal window closes itself** (~20s later). If macOS refuses to run it the first time ("cannot verify the developer"), right-click → Open → Open. If it opens in TextEdit instead of running, run `chmod +x start.command` once in Terminal. |

**How auto-shutdown works:** the page sends a heartbeat to the server every 5 seconds. When you close the tab, heartbeats stop, and the server exits cleanly after 20 seconds of silence. This only kicks in when launched via `start.command` / `start.bat` (they set `AUTO_SHUTDOWN_IDLE=20`). Manual `python main.py` runs have no idle timeout — the server stays alive until you Ctrl+C it.

### From a terminal (alternative)
```bash
cd <project_dir>
.venv/bin/python main.py     # macOS / Linux (with venv)
.venv\Scripts\python main.py # Windows (with venv)
# or just: python main.py
```
Then manually open `http://localhost:8899`.

The UI has:
- **Sync** — Spotify → Rekordbox (downloads new tracks, creates playlists, reorders to match Spotify)
- **Tracked Playlists** card — shows what's in `sync_state.json`. Click ✕ next to any playlist to stop syncing it (does NOT touch Rekordbox — re-add the FF prefix in Spotify and sync again to bring it back).
- **Config** card — shows resolved paths and ffmpeg status. **Change…** next to Music Folder opens a native folder picker.

For USB export to CDJs, use Rekordbox's native **File → Export Collection in rekordbox xml format** + **Export to Device** after sff finishes syncing and Rekordbox has analyzed the new tracks. We removed the in-app USB button because writing CDJ-readable Device Library Plus / PDB outside Rekordbox is impractical (proprietary formats), and Rekordbox's native export is what you actually need.

### Pre-flight rules

1. **Close Rekordbox before clicking Sync.** sff refuses to write to master.db while Rekordbox is open (it would corrupt or lose writes).
2. **Don't force-quit Rekordbox during analysis.** Force-quit loses unsaved DB commits — your analysis goes to the ANLZ files but the DB row update stays in memory and gets dropped. Click X and let it close itself.
3. **After Sync finishes, open Rekordbox to analyze new tracks** (right-click → Analyse Track on each new playlist).

### Toggling Traktor sync on/off

Traktor sync is **OFF by default.** Toggle it via the **Sync to Traktor** checkbox in the Config card of the web UI. Persists in `app/app_config.json` (gitignored).

When ON, sff also writes to your `collection.nml` on every track import, playlist creation, ordering change, and removal. The path is auto-detected (Mac: `~/Library/Application Support/Native Instruments/Traktor X.X.X/collection.nml`; Windows: `~/Documents/Native Instruments/Traktor X.X.X/collection.nml`). If it lives elsewhere, override `TRAKTOR_NML_PATH` in `.env`.

The legacy `ENABLE_TRAKTOR=1` env var still works as a fallback for users upgrading from earlier versions, but the checkbox is the primary control now.

---

## What's stored where

| Thing | Location | In git? | Secret? |
|---|---|---|---|
| Spotify Client ID/Secret | `.env` | NO (gitignored) | YES |
| User config (music folder, first-run flag) | `app_config.json` | NO (gitignored) | NO |
| OAuth token cache | `.spotify_cache` | NO (gitignored) | refreshable |
| Sync state (per-playlist track tracking) | `sync_state.json` | NO (gitignored) | NO |
| Downloaded MP3s | `<your music folder>/Incoming/{Playlist}/...` | NO (outside repo) | NO |
| Rekordbox library (Win) | `%APPDATA%/Pioneer/rekordbox/master.db` | NO | NO |
| Rekordbox library (Mac) | `~/Library/Pioneer/rekordbox/master.db` | NO | NO |
| Rekordbox playlist registry (Win) | `%APPDATA%/Pioneer/rekordbox/masterPlaylists6.xml` | NO | NO |
| Rekordbox playlist registry (Mac) | `~/Library/Pioneer/rekordbox/masterPlaylists6.xml` | NO | NO |
| Traktor library (Win) | `~/Documents/Native Instruments/Traktor X.X.X/collection.nml` | NO | NO |
| Traktor library (Mac) | `~/Library/Application Support/Native Instruments/Traktor X.X.X/collection.nml` | NO | NO |

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
ffmpeg isn't installed or sff can't find it. Check the Config card in the UI — `ffmpeg` row shows the resolved path or "Not found". Reinstall via `winget install Gyan.FFmpeg` (Windows) or `brew install ffmpeg` (Mac).

### Reset sync state (re-check all tracks)
Delete `sync_state.json`. Next sync will re-evaluate every track via the 3-layer dedup (Rekordbox DB title search + file index scan + path check) — won't re-download anything that already exists.

### Reset music folder choice
Delete `app_config.json`. Next page load will show the first-run modal again.

### Stop syncing a specific playlist
Click ✕ next to it in the **Tracked Playlists** card. Only removes it from `sync_state.json` — Rekordbox keeps the playlist and tracks. Re-add the FF prefix in Spotify and sync to bring it back.

---

## Architecture summary

```
Spotify (FF playlists)
    ↓ spotipy PKCE
sff (FastAPI, localhost:8899)
    ↓ yt-dlp + ffmpeg + mutagen
Local MP3s (<user music folder>/Incoming/{playlist}/)
    ↓ pyrekordbox + masterPlaylists6.xml registration
Rekordbox (master.db + ANLZ on first analyze)
    ↓ optional: UI checkbox "Sync to Traktor"
Traktor (collection.nml, atomic write)
    ↓ Rekordbox's native "Export to Device" (NOT sff)
USB pen drive (Device Library Plus + ANLZ)
    ↓
CDJ-3000 / XDJ
```

For deep architecture, see PRD.md. For every bug we've hit and fixed, see DEBUG_LOG.md.

---

## Where my secrets are stored locally
At `D:/Code/secrets/standalone-file-fetcher.txt` (outside this repo, machine-local). Never put real Spotify keys in `.env.example` or anywhere committed.
