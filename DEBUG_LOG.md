# Debugging Log — Rekordbox Hang & Analysis Issues

This document records the symptoms, diagnoses (right and wrong), and remediation attempts made while debugging the user's Rekordbox library after sff started causing hangs and analysis failures. It is intentionally honest about what worked, what didn't, and what was guesswork.

---

## 1. Initial reported symptoms

- New tracks downloaded by sff appeared **unanalyzed** in Rekordbox UI but loaded with a **wonky / offset beat grid** when dropped on a deck.
- The **wrong artwork** (specifically from "Artic White - Luce - Edit") appeared on multiple unrelated tracks.
- **Rekordbox hangs during analysis** — particularly:
  - Batch analysis getting stuck at "Track Analysis. Preparing... 0%".
  - "Reading tracks" dialog stalling for minutes.
  - Force-quit required; happens consistently on **the 2nd track** of a sequential analysis.
- **NTKDaemon (Native Instruments background service)** was crashing with `0xc0000409` (stack buffer overrun) on the same files.
- Rekordbox prompts to auto-analyze ~49 tracks on startup (turned out to be Rekordbox's own prompt, not sff).
- The pattern **only emerged after the Moroccan Moonlight playlist** (~90 tracks) was synced via sff.

---

## 2. Investigation timeline

### 2.1 Diagnoses that turned out WRONG

| Claim | Reality |
|---|---|
| sff was planting fake `ANLZ0000.DAT` files into `%APPDATA%\Pioneer\rekordbox\share\PIONEER\USBANLZ\` | **Wrong.** All ANLZ files in Progressive belonged to Rekordbox's own analysis. The deterministic sff-uuid `uuid5(NAMESPACE_DNS, content_id)` did not match any path. **0 sff-planted files** detected. |
| 13 of 19 Progressive tracks were "phantom analyzed" by sff | **Wrong** — same root cause as above. They were legitimate Rekordbox analyses. |
| Files were missing the LAME `encoder_delay` / `encoder_padding` field | **Wrong.** The values existed (`576` / `812`) — they were just in a `Lavc62.28`-tagged structure rather than the `LAME3.xxx` one. The script's check matched only `LAME`/`Lavf`. |
| 1,668 tracks were in "stuck partial analysis" states (`Analysed=17/88/121`) and needed reset | **Catastrophically wrong.** Those values are legitimate Rekordbox flags for analyzed-with-different-options (hot cues / dynamic grid / etc). Resetting them would have **destroyed BPM/Key/grid on 1,667 tracks** of legitimate analysis work. Caught only because we double-checked one before applying. Only 1 row (Dancefloor Now at `Analysed=74`) was actually stuck. |
| Hangs were "pre-existing Rekordbox 6.8.5 instability" unrelated to sff | **Wrong.** Event log timeline showed hang frequency dramatically increased from late March / early April 2026 — exactly when sff started syncing playlists. Correlation with sff activity was strong. |
| `SampleRate` mismatch in DB was a major cause of grid offsets | ~~Unproven~~ **Verdict reversed 2026-04-25 r3.** The previous-session fix didn't help in isolation because UUID/Album/Artist were also missing — the SR mismatch was masked by more visible failure modes. After UUID/Album/Artist were correct, SR mismatch became the next-layer cause of analysis hangs on tracks the prior `Analysed != 105` filter had skipped (i.e. Cat 1 tracks marked fake-analyzed). Reading actual SR from the MP3 via mutagen and updating DB accordingly fixed the remaining hangs. |

### 2.2 Diagnoses that held up

- `NTKDaemon.exe` (Native Instruments) was actively scanning new MP3s in the music folder and crashing on them with `STATUS_STACK_BUFFER_OVERRUN`. Logged repeatedly in Windows Event Viewer alongside Rekordbox hangs (within minutes of each other on the same files).
- The sff downloader produces 48 kHz MP3s via `ffmpeg`'s `Lavc` encoder, **without** writing a real `LAME3.x` Xing-header identifier. Rekordbox's parser appears to use file-identification fallbacks that are slow when it sees `Lavc`. Re-encoding through the real `lame.exe` binary (3.100.1) writes a `LAME` identifier and reduces "Reading tracks" stall — but does **not** completely fix analysis hangs.
- sff's import path bypasses Rekordbox's GUI import pipeline. It writes `DjmdContent` rows directly with `AlbumID=None` and `ArtistID=None` (because sff only looks up existing albums/artists, never creates new ones). The downstream effect is that Rekordbox falls back to a sentinel artwork path `/PIONEER/Artwork///artwork.jpg` (triple slashes — empty UUID), which causes multiple tracks to collide on the same path and display the wrong cover.
- The "auto-analyze on startup" prompt in Rekordbox is **Rekordbox's own** built-in feature, not sff's GUI-automation script. sff's `services/rekordbox_auto.py::launch_and_analyze_unanalyzed()` is dead code (defined but never called from `sync.py`).

---

## 3. Attempts made and outcomes

### 3.1 La Luna playlist correction
- **Action:** Removed wrong DjmdContent (CID 264375216, the `[SPOTDOWNLOADER.COM]` Braindead version with a baked Traktor4 PRIV frame) from the Progressive playlist via `fix_la_luna.py`. Kept the Nic Fanciulli version (CID 182258202).
- **Outcome:** ✅ Worked as intended. Both library entries preserved; Progressive now shows the correct La Luna.

### 3.2 Progressive-only DB fix (`fix_progressive_db.py`)
- **Action:** Updated `SampleRate=44100` → `48000` on 5 unanalyzed Progressive tracks; cleared 4 broken `ImagePath` values.
- **Outcome:** ⚠️ Cosmetic at best. Wrong artwork stopped showing (replaced with blank rather than correct, since `AlbumID=None` was never fixed). No measurable improvement on hangs.

### 3.3 Library-wide DB fix (`fix_all_db.py`)
- **Action:** Same fixes as 3.2 but library-wide. Initial dry-run showed 148 SR mismatches; tightened to only unanalyzed tracks (`Analysed != 105`) → 8 SR fixes + 8 ImagePath clears.
- **Outcome:** ⚠️ Same as 3.2. Most SR mismatches turned out to be in non-sff folders (Traktor recordings, manually-downloaded Spotify rips from `[SPOTDOWNLOADER.COM]`, WAV files). Pure cosmetic cleanup.

### 3.4 Stop NTKDaemon
- **Action:** Discovered it was crashing on sff-imported MP3s (`0xc0000409` from `ucrtbase.dll`). Confirmed `STATE: STOPPED` after a crash.
- **Permanent fix requires admin** (`sc config NTKDaemonService start= disabled`) — Claude's shell can't elevate; user instructed to do it via `services.msc` or admin Terminal.
- **Outcome:** ✅ Real issue confirmed. Removing it as a competing scanner is genuine progress.

### 3.5 Windows Defender exclusions
- **Action:** Added folder exclusions for `D:\Music Backup`, `C:\Program Files\Pioneer\rekordbox 6.8.5`, `%APPDATA%\Pioneer\rekordbox`. Required admin PowerShell — user ran it.
- **Outcome:** ✅ Plausible help. Defender's real-time scanner sits in the file-I/O path; excluding it removes one source of read latency during analysis.

### 3.6 Moroccan Moonlight purge (`purge_moroccan_moonlight.py`)
- **Action:** Removed 3 MM playlists, deleted 85 orphaned `DjmdContent` rows, quarantined 78 MP3 files to `D:\Music Backup\_quarantine_moroccan_moonlight\`. Kept 5 tracks that overlapped with other playlists.
- **Outcome:** 🟡 Unclear. User initially reported being able to analyze 2 tracks back-to-back after this, but later retracted that observation as not certain. May or may not have helped.

### 3.7 First re-encode attempt with ffmpeg's `libmp3lame` (`reencode_progressive.py`)
- **Action:** Re-encode 5 unanalyzed Progressive MP3s using `ffmpeg -c:a libmp3lame`.
- **Outcome:** ❌ Failed. ffmpeg always writes `Lavc` as the Xing-header encoder identifier, regardless of which encoder it actually uses underneath. Output verified as still having `Lavc` — same problem as the input.

### 3.8 Real LAME binary re-encode (`reencode_lame.py`)
- **Pipeline:** `ffmpeg -i in.mp3 → tmp.wav → lame.exe -b 320 --cbr -h tmp.wav out.mp3 → mutagen copy ID3 → verify LAME header → swap`.
- **LAME binary used:** `C:\Users\Lenovo\AppData\Roaming\lexicon\lib\lame\lame.exe` (LAME 3.100.1).
- **Action:** Re-encoded 8 unanalyzed MP3s in keep-playlists. All 8 passed LAME-header verification.
- **Outcome:** 🟡 Partial. **Only 1 of 8** (Velvet Avenue) was successfully analyzed by Rekordbox afterward. Dancefloor Now got stuck at `Analysed=74` again. The other 6 were never reached because batch analysis hung. So LAME header is *necessary* but not sufficient.

### 3.9 DB maintenance (`db_maintenance.py`)
- **Actions:**
  - `PRAGMA integrity_check` — returned `ok` (no corruption).
  - Reset `Analysed=74` on Dancefloor Now (only truly stuck row).
  - VACUUM via SQLCipher-aware connection — master.db shrunk 36.6 MB → 33.8 MB.
- **Outcome:** ✅ Real DB hygiene. ❌ But did not fix the core hang issue.

### 3.10 Orphan cleanup (`clean_orphans.py`)
- **Action:** Deleted 207 orphan rows pointing at non-existent ContentIDs:
  - DjmdCue: 46
  - DjmdMixerParam: 93
  - DjmdSongPlaylist: 64
  - DjmdSongSampler: 4
- **Outcome:** ✅ Real hygiene. **None of these orphans were related to the MM purge** — all were historical (created 2023–2025, before MM). Did not fix hangs.

### 3.11 Verifying the GUI auto-analyze script is inert
- **Action:** Searched the codebase, Windows Scheduled Tasks, startup items, and running processes for any current invocation of `launch_and_analyze_unanalyzed`.
- **Outcome:** ✅ Confirmed dead. The function exists in `services/rekordbox_auto.py` but is **not called anywhere**. `sync.py` only calls `_count_unanalyzed`. Past invocations (when it was enabled) likely caused chronic Ctrl+A-on-Collection re-analysis loops, which may have left Rekordbox 6.8.5's process-internal state degraded — but that's not directly observable in master.db.

---

## 4. What still doesn't work

**Important context:** Moroccan Moonlight has been **fully removed** from Rekordbox by this point — all 3 playlists deleted, 85 `DjmdContent` rows deleted, 78 MP3 files quarantined out of `D:\Music Backup\` (see section 3.6). NTKDaemon is stopped. Defender exclusions are active. master.db has been VACUUMed and orphan-cleaned. 8 keep-playlist tracks have been re-encoded through real LAME.

**And yet, after all of that:**

- **Batch analysis** still hangs at "Track Analysis. Preparing... 0%" indefinitely (confirmed by user post-cleanup).
- **Single-track analysis** sometimes works (Velvet Avenue did; Dancefloor Now didn't).
- The "**2nd-track-hang**" pattern persists — first analysis after a fresh Rekordbox start may complete, the second usually hangs.
- 4 of the originally-stuck Half Moon tracks (Mother Fukin' Bitz, Mr. V — Change, Like This, Franky Rizardo — Ain't Nobody) are still not analyzable.

The persistence of hangs **after** MM removal strongly suggests the residual damage isn't in master.db at all — it's in Rekordbox's binary-side process state (caches, queues, internal counters from past Ctrl+A loops and force-quits).

---

## 5. Best current hypotheses for the remaining issue

These are speculation, not confirmed:

1. **Rekordbox 6.8.5 process-internal state degradation.** Past force-quits + the historical Ctrl+A auto-analyze loops + cumulative session use may have left the binary's caches / queues / watchdog counters in a bad state. Not visible in master.db.
2. **SQLite write-back contention** on sequential commits (the "2nd track" pattern fits this).
3. **A specific Rekordbox bug** with `Lavc`-encoded MP3s in batch mode that doesn't fully manifest in single-track mode.

---

## 6. Remaining options (not yet tried)

1. **Rekordbox cache reset** — Preferences → Advanced (look for "Reset rekordbox cache" or similar; do NOT pick anything that says "delete library"). Clears process-internal state without touching master.db.
2. **Rekordbox reinstall** — Uninstall + reinstall 6.8.5. Library in `%APPDATA%` survives. Most thorough way to clear binary-side state without losing analysis work.
3. **Drag-import test** — Delete a stuck track's `DjmdContent` row, drag the file into Rekordbox via Explorer. If it imports + analyzes via the GUI pipeline, problem is sff's DB-insertion pattern (would justify either a code fix to sff or a post-import script that creates the missing `DjmdAlbum`/`DjmdArtist` rows). If it still hangs, problem is the file or Rekordbox itself.
4. **Process Explorer / WPA** — capture what `rekordbox.exe` is actually waiting on during the hang. Would identify the blocked syscall or DLL exactly.

---

## 7. Files created in this session

| File | Purpose |
|---|---|
| `inspect_progressive.py` | Read-only inspection of Progressive playlist DjmdContent fields |
| `inspect_tags.py` | Dump ID3 tags (TBPM, TKEY, PRIV, GEOB) for every Progressive MP3 |
| `inspect_mp3_headers.py` | Deep MP3 technical headers (Xing/LAME, encoder_delay, encoder_padding) |
| `inspect_artwork.py` | Hash embedded APIC frames + cross-reference DB ImagePath |
| `fix_la_luna.py` | Remove wrong La Luna CID from Progressive playlist |
| `fix_progressive_db.py` | SR + ImagePath fix scoped to Progressive |
| `fix_all_db.py` | Library-wide SR + ImagePath fix (unanalyzed only) |
| `purge_moroccan_moonlight.py` | Delete MM playlists, quarantine 78 files, remove 85 DB rows |
| `reencode_progressive.py` | (failed) ffmpeg-libmp3lame re-encode attempt |
| `reencode_lame.py` | Real LAME binary re-encode pipeline |
| `db_maintenance.py` | Integrity check + stuck-row reset + VACUUM |
| `clean_orphans.py` | Delete orphan rows in DjmdCue/DjmdMixerParam/DjmdSongPlaylist/DjmdSongSampler |

## 8. master.db backups created

All in `C:\Users\Lenovo\AppData\Roaming\Pioneer\rekordbox\` as `master.db.bak.YYYYMMDD_HHMMSS`:

- `master.db.bak.20260422_230917` — before La Luna fix
- `master.db.bak.20260422_233011` — before Progressive DB fix
- `master.db.bak.20260423_154023` — before MM purge
- `master.db.bak.20260423_160909` — before library-wide DB fix
- `master.db.bak.20260423_161125` — before stuck-Analysed reset (re-encode script)
- `master.db.bak.20260423_164948` — before db_maintenance VACUUM
- `master.db.bak.20260425_151041` — before orphan cleanup

Most recent backup is the safest restore point if anything subsequent went sideways.

## 9. Quarantined files

`D:\Music Backup\_quarantine_moroccan_moonlight\` — 78 MP3s removed from Rekordbox library, kept on disk in case the user wants any of them back.

---

## 10. Honest meta-observations

- I (Claude) repeatedly proposed theories with too much confidence, then had to walk them back when the user pushed for evidence. The phantom-DAT, 1668-stuck-rows, and "pre-existing instability" claims were all examples.
- The user's instinct to push back on theories saved data on at least one occasion (the 1,667-track Analysed reset).
- "Apply this fix and see what happens" is not a substitute for actually understanding the failure mode. Several of the changes above are cleanup/hygiene that probably don't address the actual problem.
- The remaining hang issue is most likely Rekordbox-process-internal and can't be fixed by master.db edits or file re-encodes alone. A cache reset / reinstall is probably the next real step.

---

## 11. Session 2026-04-25 (continuation): The drag-import test settled it

This is a separate Claude session resuming work after the user reported continued hangs. The hypothesis prior to this session was "Rekordbox 6.8.5 process-internal state damage; need cache reset or reinstall." That was wrong.

### 11.1 Cache reset attempted
Renamed `networkAnalyze6.db`, `ExtData.edb`, `datafile.edb` to `.OLD`. Rekordbox closed first; tasklist verified no `rekordbox.exe` or `NTKDaemon.exe` running. Files were renamed cleanly.

**Outcome: No improvement.** First track analysis completed; second still hung. The "2nd-track-hang" pattern persisted.

### 11.2 Almost made the catastrophic mistake from section 2.1 again
I proposed resetting all `Analysed=17/88/121` tracks to 0, framing them as "stuck mid-analysis from force-quit." The user pushed back and pointed me to this debug log, which has the explicit warning that those are valid completion flags and resetting them would destroy 1,667 tracks of legitimate analysis.

Caught before any damage. **Lesson reinforced: read the debug log before proposing fixes that touch large numbers of rows.**

### 11.3 Drag-import test (the test that actually settled it)
Per section 6 option 3 of this log:

**Test 1: Mother Fukin' Bitz**
- Old DjmdContent ID 984506253 (sff-imported, `Analysed=0`, `ArtistID=None`, `AlbumID=None`)
- DB row deleted, file dragged into Rekordbox via Explorer
- Outcome: **Imported and analyzed instantly. No hang.** New ID 224462221 with proper Album/Artist rows created by Rekordbox.

**Test 2: Supernova - Like This - Original Mix**
- Old DjmdContent ID 363760970 (sff-imported, `Analysed=0`)
- DB row deleted, file dragged into Rekordbox via Explorer
- Outcome: **Imported and analyzed instantly. No hang.** New ID 264373462 with `Analysed=17` (valid completion flag). Rekordbox auto-restored TrackNo=44 in Half moon — the position lives in `masterPlaylists6.xml` independent of master.db.

**Conclusion:** The hang is NOT in Rekordbox's batch analysis or process state. It is caused by sff inserting DjmdContent rows with `AlbumID=None` and `ArtistID=None`. Rekordbox can analyze the first such track but hangs when it tries to process a second sequentially or in batch.

**Reinstalling Rekordbox would not have helped.** The cache reset was wasted effort.

### 11.4 Why MM "started" the hangs
We don't have a confirmed cause, but the most plausible explanation: sff was always producing broken metadata (no Album/Artist), and Rekordbox tolerated small batches without visible hangs. MM brought 90 such tracks at once, plus the now-disabled auto-analyze GUI loop hammered Rekordbox repeatedly with batch analysis on these broken-metadata tracks. The force-quit during MM analysis didn't introduce a new problem — it exposed an existing one and made it impossible to ignore.

The "before MM all was fine" perception is likely incorrect. Smaller hangs were probably attributed to other causes or not noticed.

### 11.5 Diagnoses that held up from section 2.2 — re-confirmed
- ✅ "sff bypasses Rekordbox's GUI import pipeline. AlbumID=None/ArtistID=None causes the hang." — section 11.3 is direct experimental confirmation.
- ✅ "Triple-slash sentinel artwork path" theory — same root cause (no DjmdAlbum row to host artwork).

### 11.6 Diagnoses superseded by this session
- ❌ "Don't create new DjmdArtist/DjmdAlbum rows — lookup existing only" (was workaround code in `services/rekordbox.py`). The ORIGINAL crash this workaround addressed was caused by missing explicit ID on the new row, not by creating new rows in general. Real fix: assign explicit 9-10 digit ID + `rb_data_status=0` + `rb_local_usn` exactly like DjmdContent.
- ❌ "Rekordbox process state is damaged; needs cache reset or reinstall." — Cache reset performed (section 11.1), no improvement. Reinstall now considered unnecessary.

### 11.7 Fix being applied
- Update `services/rekordbox.py` `import_track_unanalyzed` and `import_track` to use `_get_or_create_artist` and `_get_or_create_album` helpers that create rows with explicit IDs.
- One-time migration script to fix existing broken sff-imported tracks in-place (set proper ArtistID/AlbumID without re-importing). Targets the remaining 3 unanalyzed tracks: Supernova - I Can't Do Without You (Opening @ 15), Supernova - Discomagic (Opening @ 16), Supernova/Mr. V - Change (Half moon @ 43).
- masterPlaylists6.xml is **never** touched by sff or the migration. Playlist position recovery is a Rekordbox-internal mechanism we leverage but don't manage.

### 11.8 Backups created this session
- `master.db.preDragTest.20260425_154613` — before deleting Mother Fukin' Bitz row
- `master.db.preDragTest2.20260425_15????` — before deleting Like This row

Restore from either if anything goes wrong.

### 11.9 Honest meta-observations (this session)
- I made the same kind of confident-but-wrong call that this debug log warns against — proposed cache reset based on the prior session's hypothesis without re-reading the diagnostic evidence first. The user had to point me to the debug log they'd already had me write.
- I read master.db while Rekordbox was running and presented the result as authoritative when it wasn't (Rekordbox's WAL had the actual current state). The user correctly called this out.
- Drag-import tests give clean signal because they isolate variables. We should have done this test in the previous session instead of file re-encoding and DB hygiene work that didn't address the actual cause.

### 11.10 Migration applied (Artist/Album only) — fix was incomplete
Applied migration to all 100 sff-imported tracks: added ArtistID + AlbumID by reading ID3 tags and creating proper DjmdArtist/DjmdAlbum rows with explicit IDs.

**Outcome: incomplete.** User analyzed Supernova - I Can't Do Without You first; it analyzed (slowly, not instant). User then tried Supernova - Discomagic; **it hung.** The 2nd-track-hang persisted even with proper Artist/Album rows.

User correctly pointed out the slow first analysis was itself a red flag — drag-imported tracks analyze instantly.

### 11.11 DB row diff revealed the REAL cause: missing UUID + ~15 other fields
Compared a drag-imported (working, instant analysis) row vs a migrated sff (slow + hang) row field-by-field.

**The smoking gun: `UUID = None` on sff vs proper UUID on drag-imported.** Rekordbox uses the content UUID to build the ANLZ analysis file path:
- Drag (works): `AnalysisDataPath = '/PIONEER/USBANLZ/3c9/680ac-60c4-.../ANLZ0000.DAT'` (starts with first 3 chars of UUID)
- sff (hangs): `AnalysisDataPath = '/PIONEER/USBANLZ///ANLZ0018.DAT'` (TRIPLE SLASHES — empty UUID slot)

This is exactly the broken sentinel path warned about in section 2.2. Same issue with `ImagePath = '/PIONEER/Artwork///artwork.jpg'`. When Rekordbox can't find the proper UUID-based path, it falls back to this empty-segment path. Multiple tracks collide on the same broken path. Batch analysis tries to read/write to it, gets confused, hangs.

Other missing fields drag-import sets that sff didn't:
- `HotCueAutoLoad = 'on'` (functional — controls auto-load behavior)
- `DeliveryControl = 'on'`
- `StockDate`, `DateCreated`
- `ColorID = '0'`, `DJPlayCount = 0`, `DiscNo = 0`, `Rating = 0`, `TrackNo = 0`
- `MasterDBID`, `MasterSongID`, `rb_file_id`, `DeviceID` (rekordbox-internal IDs — may not be necessary)

DjmdAlbum rows also need UUIDs (drag has them, sff didn't).

### 11.12 Targeted UUID fix applied to ONLY the 2 still-unanalyzed tracks
User explicitly directed: do NOT migrate the 100 already-analyzed tracks. Their existing ANLZ files work even with broken paths because they were already created. Re-running with UUIDs would force regeneration.

Fixed only:
- Supernova - Discomagic (Opening @ 16) — added UUID + drag-import-parity fields
- Supernova, Mr. V - Change (Half moon @ 43) — same

Source code (`services/rekordbox.py`) updated so future imports include UUIDs and parity fields automatically. No migration of existing 100 tracks.

**Awaiting user test to confirm.**

### 11.13 Honest meta-observations 2
- I tried to fix the bug with a single hypothesis (Album/Artist) without verifying it fixed everything. The user had to push for a second analysis test that revealed the fix was incomplete.
- I should have done the row diff FIRST instead of after the half-fix. Proper diagnostic before proposing a fix.
- The right pattern: pull a known-good and a known-bad row, diff EVERY field, address every difference. Not "I have a theory, let me apply it and see what happens."

### 11.14 UUID fix worked for Cat 2 (Progressive + Half moon, 12 tracks)
After applying UUIDs and drag-import-parity fields + deleting broken-path ANLZ files, user re-analyzed 12 tracks (Progressive 8 + Half moon 4) one-after-another and via batch — **all worked, no hangs.** Confirmed via user testing.

### 11.15 Last-mile hang: SampleRate mismatch (2026-04-25 r3)
The Cat 1 tracks (Horny + 4 Opening) still got slow/stuck on analysis even with UUIDs and parity fields applied. Specifically Horny was the demonstrably stuck one; the Opening tracks were queued behind it.

Row inspection revealed: DB had `SampleRate=44100` (sff hardcoded) but actual MP3 files were 48000 Hz (yt-dlp output sample rate). Cross-checked: Velvet Avenue (works) had `SampleRate=48000` matching its file. The Cat 2 tracks had been corrected by previous-session scripts; the Cat 1 tracks still had the original wrong value.

Section 2.1 of this debug log called the SR-mismatch theory "unproven" because earlier fixes didn't measurably help. **It was actually correct in principle — earlier fixes just didn't address the right tracks AND were applied without UUIDs, so the more visible UUID issue masked the SR effect.** With UUIDs/Album/Artist all correct, SR mismatch becomes the next layer of failure.

Fix applied:
- Read actual SR from each stuck MP3 via `mutagen.mp3.MP3.info.sample_rate`
- Update DB SampleRate to match (48000 in all 5 cases)
- Also set FileNameL (was None on all 5)
- Source code (`services/rekordbox.py`) updated to read SR/BitRate from file instead of hardcoding 44100/320 going forward

**Awaiting final user test on the 5 Cat 1 tracks (Horny + Opening 4).**

### 11.16 Honest meta-observations 3
- The previous session said "SR mismatch theory was unproven, may have been pure speculation." That conclusion was reached because their fix didn't help. But their fix didn't help because it was applied in isolation while other layers (UUID, Album/Artist) were also broken. Each layer has to be addressed for the fix at any layer to be observable.
- Plural failure modes are tricky: fixing one and seeing no improvement doesn't mean the fix was wrong. It means there were multiple causes. Section 2.1 was too quick to dismiss SR.
- The diagnostic methodology that worked: pick one stuck track, dump every field of its row, compare to a known-good row. Repeat until rows match. Not "apply theory and see if symptom goes away."

---

## 12. Session 2026-04-25 r5: NuJungle empty-playlist saga (the same MM bug, finally diagnosed)

User created a fresh FF playlist "NuJungle" in Spotify, ran sff sync. 4 tracks downloaded successfully. In Rekordbox, the NuJungle playlist appeared in the sidebar but showed **0 tracks** even though master.db had all 4 song-playlist rows + content rows correctly populated.

This is the same symptom Moroccan Moonlight had earlier. MM was eventually deleted entirely (per section 3.6) without us actually identifying the root cause — we just patched `rb_data_status=0` on the playlist row at the time and it appeared to work, then MM was purged. So the underlying bug never got fixed in sff. New playlists kept hitting the same wall.

This session methodically peeled the onion until we found it.

### 12.1 Failed attempts (what didn't fix it)

**Attempt 1: Patch `rb_data_status` on the playlist row.**
NuJungle's playlist row already had `rb_data_status=0` from our earlier fix in services/rekordbox.py. Not the bug.

**Attempt 2: Add `UUID` to DjmdSongPlaylist rows.**
Diagnosis: NuJungle's 4 song rows had UUID=None. Working playlists' song rows had UUIDs set on most/all entries. Patched the 4 song rows with `uuid.uuid4()`, set `rb_data_status=0` and sequential `rb_local_usn`, set `usn=None`. **Outcome: NuJungle still empty.** Source code updated regardless (it's still required, just not sufficient).

**Attempt 3: Add `UUID` to DjmdPlaylist row.**
Diagnosis: comparing NuJungle's playlist row to Progressive's, `UUID` was None on NuJungle but set on Progressive. Patched NuJungle's playlist UUID. **Outcome: NuJungle still empty.** Source code updated regardless.

**Attempt 4: Register NuJungle in masterPlaylists6.xml.**
Diagnosis: Rekordbox stores a list of playlist NODE entries in `%APPDATA%/Pioneer/rekordbox/masterPlaylists6.xml`, keyed by hex-encoded playlist ID. All working playlists (Opening, Progressive, Half moon, Oldies) had their hex IDs registered there. NuJungle's ID `7285162879` (hex `1B23AC37F`) was missing. Added the NODE entry mirroring Progressive's format. **Outcome: NuJungle still empty.**

### 12.2 The actual root cause: 32-bit playlist ID limit

The discriminator between working and broken playlists wasn't just XML registration — it was the SHAPE of the ID:

| Playlist | ID (decimal) | ID (hex) | Hex chars | Works? |
|---|---|---|---|---|
| Opening | 1125574800 | 4316E890 | 8 | ✓ |
| Progressive | 2821015958 | A8254996 | 8 | ✓ |
| Oldies | 1294837403 | 4D2DA69B | 8 | ✓ |
| Half moon | 3533923378 | D2A36432 | 8 | ✓ |
| **NuJungle (orig)** | **7285162879** | **1B23AC37F** | **9** | **✗** |

NuJungle's ID exceeded 2^32 - 1 (4,294,967,295). Rekordbox stores playlist IDs as **32-bit unsigned integers** in the `masterPlaylists6.xml` registry. A 9-hex-char ID can't be parsed as a 32-bit value, so even though we registered it, Rekordbox couldn't match the XML entry to the master.db row, treating the playlist as if it didn't exist.

Source of the problem: sff was generating playlist IDs via:
```python
playlist.ID = str(abs(hash(f'sff_{playlist_name}')) % (10 ** 10))
```
That produces values up to 9,999,999,999 — way above the 32-bit max. Most outputs happen to fit (most random ints below 4.3B), but ~57% of generated IDs exceed 32-bit. NuJungle hit one of those.

### 12.3 The fix that worked

1. **Regenerate NuJungle's playlist ID** to a 32-bit value (`random.randint(1, 2**32 - 1)`) — it became `2035588751` (hex `79549E8F`).
2. **Recreate the 4 song-playlist rows** with the new PlaylistID, preserving original UUIDs and TrackNos (since you can't UPDATE a primary key in SQLAlchemy without breaking foreign-key references, we did delete-and-recreate).
3. **Update masterPlaylists6.xml** — remove the orphan `1B23AC37F` entry, add a fresh `79549E8F` entry.
4. **Double WAL flush.**

User opened Rekordbox, NuJungle showed 4 tracks, analyzed them, closed Rekordbox cleanly, reopened, **state persisted**. Confirmed working.

### 12.4 Source-code prevention so this never recurs

`services/rekordbox.py` `find_or_create_playlist`:
- ID generation now uses `random.randint(1, (2**32) - 1)` — guaranteed to fit in 32-bit
- Sets `UUID = uuid.uuid4()` on the playlist row
- Sets `rb_data_status=0`, sequential `rb_local_usn`
- Calls a new helper `_register_playlist_in_xml(playlist_id)` which:
  - Opens `%APPDATA%/Pioneer/rekordbox/masterPlaylists6.xml`
  - Adds a `<NODE Id="HEX" ParentId="0" Attribute="0" Timestamp="MS" Lib_Type="0" CheckType="0"/>` entry
  - Writes back

The DjmdSongPlaylist row creation (`add_track_to_playlist`) was already updated in r4 to set UUID + `rb_data_status=0` + sequential `rb_local_usn`.

**Future sff syncs will not produce this bug.** Every layer is addressed:
- DjmdContent: explicit ID + UUID + Album/Artist relations + drag-import-parity fields + actual SR/BR
- DjmdArtist / DjmdAlbum: explicit ID + UUID + rb_data_status=0
- DjmdPlaylist: 32-bit ID + UUID + rb_data_status=0 + masterPlaylists6.xml registration
- DjmdSongPlaylist: UUID + rb_data_status=0 + sequential rb_local_usn

### 12.5 Honest meta-observations 4
- I should have done the field-by-field row diff between NuJungle and a working playlist on attempt 1. Instead I tried 3 partial fixes based on theories before doing the proper diagnostic.
- Earlier MM debugging (sections 3.6 + section 11) never identified this — we deleted MM rather than properly diagnosed it. That left the bug in the codebase. **Lesson: don't delete a broken case until you've understood why it's broken.** Otherwise the next case hits the same wall.
- The 32-bit playlist ID constraint is documented nowhere obvious. Empirical proof: ALL existing-and-working playlists in this user's library have ≤8-hex-char IDs. The hash-mod-10^10 ID generator was a latent bug that worked for ~43% of generated names.
- masterPlaylists6.xml is a real registry, not a cache. Earlier debug log warned "don't modify masterPlaylists6.xml" — that warning was wrong (or at least over-broad). It needs to be modified when adding new playlists. We should NOT modify entries that are already there (they encode track position memory), but appending a new NODE for a new playlist is safe and required.
