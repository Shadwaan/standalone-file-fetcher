# Session handoff — yt-dlp staleness fix (2026-08-31)

Written for whoever picks this up next (Cowork or a future Claude session).
Covers **only** the work done in response to the yt-dlp brief. Earlier work in
the same session (Traktor playlist mirror + `SUBNODES` fix) is already committed
as `3344145` and is not repeated here, except where it affects numbering.

**Read `app/CLAUDE.md` first.** Its Safety Rules govern everything below.

---

## 0. Environment mismatch — read this before following the brief

The brief was written for the **Mac** clone (`~/Documents/CODE/Standalone File
Fetcher/repo`, `start.command`, osxkeychain, Safari cookies, origin at `5002f6d`).
All work below happened on the **Windows** machine instead:

| | Brief assumed | Actually here |
|---|---|---|
| Repo path | `~/Documents/CODE/Standalone File Fetcher/repo` | `C:\Users\Shadwaan\Claude\Projects\File Fetcher` |
| HEAD | `5002f6d` | `3344145` (Traktor fix, same day) |
| yt-dlp installed | ~2026.4.x, frozen since April | **2026.7.4** — venv was rebuilt this session |
| PAT in `.git/config` | present | **absent** (see §4) |

Both launchers were still edited, so Mac/Windows parity is preserved.

---

## 1. Diagnosis (Task 1) — classification: **(a) stale extractor**

Not bot-detection. Not rate limiting. This matters because an earlier call in the
same session said "transient throttling, wait 10–15 min" — **that was wrong**, and
the wrong diagnosis is itself recorded in `DEBUG_LOG.md` §16.2 so it isn't repeated.

### Confirmed structural bug
Both launchers run pip **only** when they create the venv:
- `start.command:79` — `if [ ! -x ".venv/bin/python" ]; then` … `pip install -r requirements.txt` … `fi`
- `start.bat` — `if not exist .venv\Scripts\python.exe (` … `pip install -r requirements.txt` … `)`

With `requirements.txt` pinning `yt-dlp>=2024.1.0` (a floor, not a version),
yt-dlp froze at whatever shipped on first run, permanently.

### `ydl_opts` inventory as shipped (`app/services/downloader.py`)
Search (`_search_youtube`): `quiet`, `no_warnings`, `extract_flat=True`,
`default_search=ytsearch5`, `noplaylist`.
Download (`_download_audio`): `format=bestaudio/best`, `outtmpl`, `quiet`,
`no_warnings`, `ffmpeg_location`, MP3-320 postprocessor, `noplaylist`.

**No** cookies, `extractor_args`, player-client selection, user-agent, `retries`,
`sleep_interval`, or PO-token provider. The 5-query search fan-out is unpaced.

### Reproduction (real code path, verbose)
```
WARNING: [youtube] No supported JavaScript runtime could be found...
[youtube] EBtBSdZk0xM: Downloading android vr player API JSON
[info] EBtBSdZk0xM: Downloading 1 format(s): 251
ERROR: unable to download video data: HTTP Error 403: Forbidden
```
yt-dlp could not use the web clients, fell back to `ANDROID_VR` (visible as
`c=ANDROID_VR` in the googlevideo URL), and that client's format URL was 403'd.

Probes on 2026.7.4:

| Attempt | Result |
|---|---|
| default (android_vr) | formats returned, download **403** |
| `js_runtimes={'node':{}}` | warning gone, still android_vr, still **403** |
| `player_client=tv` | `The page needs to be reloaded.` |
| `web` / `web_safari` / `ios` / `mweb` | **zero formats** (`Requested format is not available`) |

**Decisive control:** a video that downloaded successfully at 16:33 the same day
*also* 403'd on retry → systemic and time-dependent, not per-video, not IP quota.

### Fix confirmed
Upgrade to nightly **2026.8.30.232658.dev0** → control video *and* the track that
had failed 3× (`Tour-Maubourg – Dreams`) both downloaded first try, with **no other
changes to `ydl_opts`**.

---

## 2. Changes made (Task 2) — 4 files, all uncommitted

```
 M start.bat            # + section "2b. Keep yt-dlp current"
 M start.command        # + same block, bash form
 M app/requirements.txt # yt-dlp>=2024.1.0  ->  yt-dlp>=2026.8.19
 M app/DEBUG_LOG.md     # + Section 16
```

Inserted in both launchers **after** venv resolution, **before** `main.py`:

```bash
.venv/bin/python -m pip install -U --pre -q \
    --disable-pip-version-check --timeout 10 --retries 1 \
    "yt-dlp[default]" \
  || echo "  (skipped — offline or PyPI unreachable; using installed version)"
```

Design constraints — do not "simplify" these away:
- **Must be in the launcher, not `main.py`.** Upgrading after Python has imported
  `yt_dlp` means the current run still executes the old code.
- **`yt-dlp -U` does not work.** Self-update only applies to the standalone binary;
  a pip install can only be updated by pip.
- **`--pre` (nightly)** — YouTube extractor fixes land in nightly first. That is
  the entire point.
- **Never block launch** — `--timeout 10 --retries 1` caps the offline penalty,
  `||` degrades failure to a warning.
- **Scoped to yt-dlp only.** Deliberately NOT `pip install -U -r requirements.txt`.
  `pyrekordbox` must not move silently — all the DB-write hygiene in `CLAUDE.md`
  (UUID, ArtistID/AlbumID, drag-import parity) is verified against the current one.

Chose the every-launch form over the optional daily stamp file: measured cost is
~1.4 s, which doesn't justify the extra state.

**Section numbering:** the brief said "new Section 15", but §15 was already used
earlier the same day by the Traktor `SUBNODES` fix. This went in as **Section 16**,
and says so in its own header.

---

## 3. Hardening (Task 3) — NOT DONE, awaiting Shadwaan's decision

Recommendation given: **do nothing further.** Every observed failure traced to
version staleness, now automated away. `cookiesfrombrowser` adds a browser-profile
dependency and a locked-cookie-DB failure mode; a PO-token provider adds a Node
service dependency — real costs for a problem that no longer reproduces.

Only cheap option still on the table: modest pacing (`retries`,
`sleep_interval`/`max_sleep_interval`) so the 5-query fan-out doesn't hammer
YouTube. Pure insurance, no new dependencies. **Not implemented — ask first.**

If failures ever recur *on an up-to-date yt-dlp*, that is the signal to revisit,
and only then in the order: pacing → cookies → PO-token provider.

Preserve regardless: `extract_flat=True` for search, 30 s duration tolerance,
explicit `ffmpeg_location`, MP3 320 kbps, mutagen tagging.

---

## 4. Security (Task 4) — nothing to do on Windows; **action still open on Mac**

This clone is clean:
- remote is plain `https://github.com/Shadwaan/standalone-file-fetcher.git`
- no `gh[pousr]_…` token anywhere under `.git/`
- `credential.helper = manager` (Git Credential Manager — correct Windows equivalent of osxkeychain)

**The PAT is in the Mac clone.** Not reachable from this machine. On the Mac:

```bash
git remote set-url origin https://github.com/Shadwaan/standalone-file-fetcher.git
git config --global credential.helper osxkeychain
```

Shadwaan rotates the token himself — **do not attempt to rotate it.**

---

## 5. Verification status

| # | Check | Result |
|---|---|---|
| 1 | `pip show yt-dlp` reports 2026.8.x | **PASS** — `2026.8.30.232658.dev0` |
| 2 | Launch via launcher; update step prints; app on :8899; watchdog exits | **PASS** — update printed, server up in 1 s, watchdog fired at 20 s idle and exited |
| 3 | Offline path warns and starts anyway within ~10 s | **PASS** — exit 0 in ~1.2 s (DNS failure) and ~5.4 s (connection refused). Simulated via unreachable `--index-url`, not by killing wifi |
| 4 | Real sync, Rekordbox CLOSED | **IN PROGRESS at handoff** — Rekordbox confirmed closed, `master.db` backed up first. Last read: **23 downloaded / 23 imported / 3 skipped / 0 failed**, still climbing. Zero failures at every sample so far (prior run on stale yt-dlp: 16 failures in 183) |
| 5 | Previously-failing downloads succeed + drag-import parity | **PARTIAL** — the 3×-failing track now downloads; **parity check not yet run** |

### Finishing verification 4 & 5
Sync ran under a monitor; re-check before trusting the numbers:
```bash
curl -s http://localhost:8899/api/status
```
Then run the read-only parity checker (queries only, no commit — checks UUID,
ArtistID, AlbumID, HotCueAutoLoad, FileType/BitRate/SampleRate, `Analysed=0`,
`rb_data_status=0`, and that WAL is 0 bytes):
```
<scratchpad>/verify_import_parity.py
```
It lives outside the repo at
`C:\Users\Shadwaan\AppData\Local\Temp\claude\C--Users-Shadwaan-Claude-Projects-File-Fetcher\f60a3f75-ff8c-4c05-8759-bfeabad61c42\scratchpad\`
along with `repro_download_failure.py` (the Task 1 repro) and
`mirror_rb_to_traktor.py` (earlier Traktor work). **Temp dir — copy anything worth
keeping into the repo.**

---

## 6. Git state at handoff — NOTHING COMMITTED, NOTHING PUSHED

```
 M app/DEBUG_LOG.md
 M app/requirements.txt
 M start.bat
 M start.command
?? .claude/              # local launch config — should probably be gitignored
?? CUE_BRIDGE_SPEC.md    # Rekordbox<->Traktor cue sync spec, awaiting review
```

Shadwaan asked to see `git diff` and walk through every change **before** any
commit, and explicitly said **do not push without asking**. Both still stand.

---

## 7. Found but deliberately NOT fixed

1. **Windows launcher window doesn't auto-close.** The watchdog raises SIGINT,
   which exits Python with code **2** on Windows; `start.bat` only treats `0`,
   `-1073741510`, `3221225786` as clean, so it prints "Server exited with error 2.
   Press any key to close." Server exits correctly and frees the port — cosmetic
   only. Blanket-treating 2 as clean would mask genuine errors; the better fix is
   an explicit `sys.exit(0)` on the watchdog path in `main.py`.
2. **`pyrekordbox>=0.3.0` is a floor, not a pin**, despite 0.4.4 being the
   verified version. Worth pinning exactly given how much DB-write behaviour
   depends on it.
3. **No JS runtime installed.** node v24 is present but yt-dlp enables only deno
   by default. Enabling node did *not* fix the 403 and is unnecessary on current
   nightly. Installing deno is the more future-proof option if web-client
   extraction ever becomes mandatory.
4. Running `start.bat` from Git Bash prints `timeout: invalid time interval '/t'`
   (GNU `timeout` shadows Windows' `timeout.exe`). Test artifact, harmless.

---

## 8. Machine facts worth carrying forward

- Music folder: `D:\Music\Incoming`, per-playlist subfolders.
- Rekordbox DB: `%APPDATA%\Pioneer\rekordbox\master.db`. **Treat as read-only
  unless Shadwaan explicitly agrees to a write** — his standing rule.
- Traktor 3.5.1 collection is OneDrive-redirected:
  `C:\Users\Shadwaan\OneDrive\Documents\Native Instruments\Traktor 3.5.1\collection.nml`
  (sff's auto-detect misses this; set `TRAKTOR_NML_PATH`).
- ffmpeg: WinGet Gyan full build, auto-detected correctly.
- 7 FF playlists: Dub, NuJungle, Oldies, Opening, Supahfunk Ref, Progressive, Half moon.
- Backups created this session, all timestamped and safe to delete once happy:
  `master.db.bak.presync.*`, `collection.nml.bak.*`, `C:\Users\Shadwaan\Music\_sff_undo_backup\`.
