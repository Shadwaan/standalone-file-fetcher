# Standalone File Fetcher (sff)

Watches your Spotify "FF"-prefixed playlists, downloads new tracks from YouTube as 320 kbps MP3, and imports them into your **Rekordbox** library — playlists, ordering, tags, and all. Runs locally as a tiny web app at `http://localhost:8899`.

---

## How to use it

### 1. Install Python (one time)
Required: Python **3.10 or newer**.

| Platform | How |
|---|---|
| **Windows** | Download from [python.org/downloads](https://www.python.org/downloads/). **Tick "Add Python to PATH" during install.** |
| **macOS**   | Download from [python.org/downloads](https://www.python.org/downloads/) (recommended — includes the GUI bits sff uses for the folder picker). Or `brew install python@3.13 python-tk@3.13` if you prefer Homebrew. |

### 2. Install ffmpeg (one time)
Required for converting YouTube audio to MP3.

| Platform | How |
|---|---|
| **Windows** | `winget install Gyan.FFmpeg` in PowerShell or Terminal |
| **macOS**   | `brew install ffmpeg` |

### 3. Get Spotify access — pick one
sff comes with its own Spotify app, but Spotify caps each app at **5 named users in development mode** (and as of May 2025, Spotify only grants extensions to companies with 250k+ users — so 5 is effectively a permanent cap for personal apps). Two ways to get in:

- **Easy path (≤5 users):** ask the sff owner to add your Spotify email to their app's allowlist. Takes them ~10 seconds in the Spotify dashboard. Send them the email associated with your Spotify account.
- **Self-hosted path (anyone, no allowlist needed):** create your own Spotify dev app — it's free and takes 2 minutes:
  1. Go to [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard) → log in with your Spotify account → **Create app**
  2. Name: anything (e.g. "my sff"). Description: anything.
  3. Redirect URI: `http://127.0.0.1:8888/callback` (exactly that — must match)
  4. Save. Open the app, copy the **Client ID**.
  5. In `app/.env` add: `SPOTIFY_CLIENT_ID=<paste your client id here>`. (If `app/.env` doesn't exist, copy `app/.env.example` to `app/.env`.)
  6. You're now the only user of your own dev app — no allowlist needed.

### 4. Start it

| Platform | What to do |
|---|---|
| **Windows** | Double-click **`start.bat`** |
| **macOS**   | Double-click **`start.command`**. First time only: macOS may say "cannot verify the developer" — right-click → **Open** → **Open**. After that it's trusted. |

That's it. A console/Terminal window opens, the server starts, and your browser opens to the sff page automatically. **First launch takes ~2 minutes** while it sets up its Python environment; every launch after that is instant.

### 5. First-time inside the app
1. A modal pops up asking where to download music. Pick any folder you want — sff creates an `Incoming/` subfolder inside it organized by playlist name.
2. Click **Sync**. Your browser opens a Spotify authorization page once — accept it. (No Client ID, no Secret, no copy-paste.)
3. Sff downloads new tracks, imports them into Rekordbox, then says *"X tracks need analysis in Rekordbox."*
4. Open Rekordbox — it auto-analyzes the new tracks on launch. Done.

### 6. Stop it
Just close the browser tab. The server shuts down automatically about 20 seconds later, and the console window closes itself.

---

## Pre-flight rules

1. **Close Rekordbox before clicking Sync.** sff refuses to write to the library while Rekordbox is open (would corrupt the database).
2. **Don't force-quit Rekordbox during analysis.** Click X and let it close itself, otherwise it loses analysis work.
3. After sync, **open Rekordbox to analyze new tracks** (it auto-analyzes on launch — just open it).

---

## Optional: Sync to Traktor too
If you also use Traktor (Native Instruments DJ software), open the **Config** card in sff and tick **Sync to Traktor**. Sff will also write each new track + playlist to your `collection.nml`. No file editing required.

---

## Common issues

| Problem | Fix |
|---|---|
| "Python is not recognized as an internal or external command" | Reinstall Python and tick **Add Python to PATH** during install. Restart the launcher. |
| Spotify auth says "User not registered in the Developer Dashboard" | Ask the sff owner to add your Spotify email to the app's user allowlist (see step 3 above). |
| "Rekordbox is running. Please close it before syncing." | Close Rekordbox completely (Activity Monitor on Mac, Task Manager on Windows — kill any leftover process). |
| Browse button in folder picker says "tkinter unavailable" | Either install Python from python.org (instead of brew), or just paste the path manually in the text field below. |
| ffmpeg "Encoder not found" during download | Install ffmpeg (see step 2 above). The Config card in sff's UI shows where it found ffmpeg, or "Not found". |
| Want to stop syncing one playlist | Click ✕ next to it in **Tracked Playlists**. Doesn't touch Rekordbox — just removes it from sync state. Re-add the FF prefix in Spotify and sync to bring it back. |
| Want to switch the music download folder | Click **Change…** next to Music Folder in the Config card. |

---

## What's where in this repo

```
/
├── README.md          ← you are here
├── start.command      ← Mac launcher (double-click)
├── start.bat          ← Windows launcher (double-click)
└── app/               ← all the technical guts
    ├── main.py             ← FastAPI server entry point
    ├── requirements.txt    ← Python dependencies
    ├── .env.example        ← optional power-user overrides (most users skip this)
    ├── services/           ← sync logic, downloader, Rekordbox/Traktor writers
    ├── frontend/           ← the local web UI (single index.html)
    ├── models/             ← data classes
    ├── SETUP.md            ← deeper setup + troubleshooting (for contributors)
    ├── CLAUDE.md           ← AI-assistant guidelines / architecture notes
    ├── PRD.md              ← product spec
    └── DEBUG_LOG.md        ← every bug we've hit and fixed
```

You should never need to open the `app/` folder. Everything you do is in the browser.

---

## Privacy
Everything runs locally on your machine. No remote server, no cloud, no telemetry. Your Spotify token lives in `app/.spotify_cache` (gitignored — never pushed). Your sync state lives in `app/sync_state.json` (also gitignored). Music files go wherever you told sff to put them.
