# Cue Bridge — spec (draft)

Own-your-own Lexicon: sync cue points, loops, and beat grids between Rekordbox
and Traktor for a shared local file library. Companion to sff — reuses its
venv, its atomic NML writer, and its Rekordbox DB access.

Status: **spec only, nothing built.** Drafted 2026-07-30.

---

## Scope

- **Phase 1 (RB → Traktor)** — read-only on Rekordbox, writes only to Traktor
  `collection.nml`. Ship this first; it's the low-risk direction and covers the
  "grid + cue in Rekordbox, practice in Traktor" workflow.
- **Phase 2 (Traktor → RB)** — requires writing `djmdCue` rows into
  `master.db`. Higher risk; only build once Phase 1 is trusted. Same
  discipline as sff: backup first, Rekordbox closed, double WAL checkpoint.
- **Non-goals:** waveform/analysis transfer (each app analyzes its own),
  playlist sync (already handled), streaming-service anything.

## Where the data lives

| Thing | Rekordbox | Traktor |
|---|---|---|
| Hot cues + memory cues | `djmdCue` table in `master.db` (`ContentID`, `InMsec`, `Kind`, `Color`, comment) | `CUE_V2` elements in each `ENTRY` of `collection.nml` |
| Loops | `djmdCue` rows with `OutMsec` set | `CUE_V2` with `TYPE=5`, `LEN` > 0 |
| Beat grid | ANLZ files (`share/PIONEER/USBANLZ/<uuid-path>/ANLZ0000.DAT`, `PQTZ` tag) + `BPM` on `DjmdContent` (×100) | `CUE_V2` with `TYPE=4` (grid anchor) + `TEMPO` element (`BPM` attr) |

- pyrekordbox reads all of the RB side, including an `anlz` module for the
  ANLZ/PQTZ beat grid. No new parsing work needed.
- Traktor side is plain XML on top of sff's existing `_safe_write_nml`.

## Mapping rules

- RB hot cue A–H → `CUE_V2 HOTCUE=0–7`. RB memory cue → `CUE_V2 HOTCUE=-1`.
- RB loop → `TYPE=5`, `START=InMsec`, `LEN=OutMsec−InMsec`.
- Cue names/comments and colors carry over where the target supports them
  (Traktor has no per-cue color; drop silently).
- Grid: take RB's first beat anchor + BPM → one `TYPE=4` grid cue + `TEMPO`.
  **Dynamic RB grids flatten to a static grid** (Traktor can't represent
  drifting grids well). If RB grid has >1 tempo region, warn and use the
  dominant region; list these tracks in the run report.
- Track matching across libraries: **absolute file path** (both libraries
  point at the same files on this machine — exact match, no fuzzy logic).

## Safety rules (inherit sff's non-negotiables)

1. Timestamped backup of `collection.nml` (and `master.db` in Phase 2) before
   every apply.
2. Dry-run by default; `--apply` to write. Dry-run prints a per-track diff
   (cues added / updated / skipped).
3. Upsert, never wipe: existing cues in the target are updated by slot, never
   deleted. A `--replace` flag is the only way to overwrite a track's cues
   wholesale.
4. Both apps closed during writes (process check, refuse otherwise).
5. Atomic NML writes via sff's writer (temp → parse-verify → backup → move).
6. Phase 2 only: double WAL checkpoint after `master.db` writes, verify 0
   bytes, same as sff sync.

## Shape

`app/cue_bridge.py` (CLI, runs in sff's venv):

```
python cue_bridge.py rb-to-traktor [--playlist NAME ...] [--apply] [--replace]
python cue_bridge.py traktor-to-rb [--playlist NAME ...] [--apply]   # Phase 2
```

Per-run report: tracks matched, cues written, grids flattened (with warnings),
tracks skipped (no match / no cues). Nothing touches files on disk — metadata
only.

## Open questions

- Conflict policy when both sides have cues for the same track (Phase 2):
  newest-wins vs. RB-wins vs. per-track prompt. Default proposal: RB wins,
  because RB is the analysis/gridding home base.
- Whether to bridge Traktor's 4 extra cue slots (Traktor allows 8 hot cues
  total, same as RB — fine; but Traktor "fade in/out" cue types have no RB
  equivalent — proposal: ignore them).
