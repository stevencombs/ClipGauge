# ClipGauge: local AI video renamer (portable)

[![Tip via PayPal](https://img.shields.io/badge/Tip%20via-PayPal-00457C?logo=paypal&logoColor=white)](https://paypal.me/stevencombs) [![YouTube @retroCombs-Tech](https://img.shields.io/badge/YouTube-%40retroCombs--Tech-FF0000?logo=youtube&logoColor=white)](https://www.youtube.com/@retroCombs-Tech)

Local-first video clip renamer + cataloguer that runs on macOS Apple Silicon with **Ollama** VLMs (clip description) and **whisper.cpp** (speech-to-text). The entire tree lives on this ExFAT Lexar drive so you can move it between machines (Air ↔ Pro) without reinstalling models into the system disk.

## Safety defaults

- **`dry_run`** in `config/config.json`:
  - `true` — dry run: proposals, sidecars (in `logs/dry-run/`) and the report only; nothing is renamed.
  - `false` — **live**: each confident clip is **renamed in place inside `inbox/`** right after it is processed (the new name is the "done" marker); low-confidence / failed clips keep their original name and are marked *needs review*. Every action is logged to `logs/rename-log.jsonl` and can be undone (`scripts/undo_renames.py`). Nothing is ever overwritten and nothing outside `inbox/` is ever renamed.
- **`resolve_guard: true`** — pipeline refuses to run while DaVinci Resolve is open (protects Media/project folders).
- Does **not** touch `DaVinci Resolve/`, `DaVinci Resolve Media/`, or other existing Lexar project trees. You copy clips into `inbox/` yourself.

## Folder roles

| Folder | Role |
|--------|------|
| `inbox/` | Drop source clips here (copy, don’t auto-ingest camera dumps). In live mode clips are renamed **in place** here, with their `.json`/`.md` sidecars next to them — or, with the batch-folder option, into `inbox/<folder>/`. Only files **directly** in `inbox/` are ever processed; subfolders are never (re)processed. |
| `processing/` | Working area: extracted frames, audio/transcripts, temp files. A clip's entries are deleted right after it is renamed (needs-review clips keep theirs). |
| `done/`, `needs-review/` | Not used any more (clips stay in `inbox/`; the name shows the state). Kept for compatibility. |
| `models/` | Ollama model store (`OLLAMA_MODELS` must point here). `models/whisper/` holds whisper.cpp ggml models. |
| `logs/` | `status.json`, `timings.json` (ETA averages), `pipeline.lock` (one run at a time), run logs (`clipgauge-run-*.log`, `ui-apply-*.log`, `ui-move-*.log`, `auto-apply-*.log`; `ui-run-*.log` / `web_ui.log` from the legacy web UI), `dry-run/` sidecars + `report.jsonl`, `rename-log.jsonl` (every rename / needs-review decision), `backups/`. |
| `notes/` | Central notes store: `notes/clips/<clip_id>.json`, one full record per processed clip or photo (always written); `notes/dry-run/` holds dry-run previews. See *Central notes store*. |
| `exports/` | Transcript bundles (`transcripts-<scope>-<YYYYMMDD-HHMM>.json` + `.md`) from `build_transcript_bundle.py` / the UI button. Safe to delete old ones. |
| `scripts/` | Pipeline code (Python). |
| `config/` | `config.json`, `ram-tiers.json`. |

## ExFAT / Ollama models

Lexar is **ExFAT → no symlinks**. Do not symlink `~/.ollama/models` into this tree.

Set:

```bash
export OLLAMA_MODELS="/Volumes/Lexar/AI-Video-Renamer/models"
```

Use `scripts/setup-ollama-env.sh` (see `INSTALL.md`). Pull models only after Ollama is installed and this env is set so weights land on Lexar, not the Mac internal SSD.

## Naming pattern

```
{YYYYMMDD}_{project}_{subject}_{clipType}[_t##].ext
```

`clipType` allowlist: `gameplay`, `broll`, `talking-head`, `bench`, `boot`, `menu`, `unboxing`, `fail`, plus `photo` — the generic fallback for still photos only (videos are never offered `photo`).

## Pipeline

`scripts/run_pipeline.py` runs, per clip:

1. **Resolve guard** — aborts if DaVinci Resolve is open (`check_resolve.py` matches process *executables*, so a command that only mentions a `/DaVinci Resolve/…` folder — e.g. `--video` on a file there — is not mistaken for Resolve; its own process chain is ignored).
2. **Frames** — 9 stills at `frame_percents` → `processing/<stem>_frames/` (`extract_frames.py`). Downscaled copies for the VLM go to `vlm_672/`. Note: Ollama 0.35's llama-server bills each image at **≥1024 tokens** (`--image-min-tokens 1024`), so 9 frames at 672 px ≈ 10k prompt tokens (larger images cost more: 896 px ≈ 1.75k tokens/frame and overflowed 16k); `describe.num_ctx` is 16384 and is auto-raised if more frames are configured.
3. **Transcribe (optional)** — `transcribe.py`: ffmpeg → 16 kHz mono wav → `whisper-cli` → `processing/<stem>_audio/<stem>.transcript.{json,txt}` (timestamps). Skips with a clear message if whisper isn't installed, the model is missing, or the clip has no audio.
4. **Describe** — `describe_clip.py`: 9 frames + transcript excerpt → `qwen2.5vl:7b` via `http://127.0.0.1:11434/api/chat` (JSON-schema constrained). Validated strictly; one retry with a corrective message on bad JSON.
   Returns: `summary`, `subjects`, `objects`, `on_screen_text` (OCR), `clip_type` (allowlist), `suggested_project`, `suggested_subject`, `keywords`, `confidence` 0–1.
5. **Propose filename** — `propose_name.py`: `{YYYYMMDD}_{project}_{subject}_{clipType}[_t##].ext`. Date = container `creation_time` (→ local time), else file birth/mtime. `_t01…` is only added when the name is already taken (in `done/`, the source folder, or earlier in the same run).
6. **Review** — `confidence < review_threshold` (default 0.6) or a failed/skipped describe → **needs-review**.
7. **Notes + report** — the full machine-readable record (source, frames, **full transcript**, description, proposal, timing) is **always** written to the central notes store `notes/clips/<clip_id>.json` (`notes_store.py`, see *Central notes store*). `.json`/`.md` sidecars (`sidecar.py`) are written only in dry run (`logs/dry-run/`) or, live, next to the clip when `sidecar.write_next_to_clip` is on (default **off**). One line per clip is appended to `logs/dry-run/report.jsonl` (with `action` = `dry-run (not renamed)` / `renamed` / `needs-review`, and `final_path`).
8. **Live apply** (`dry_run: false` only) — immediately after the sidecars, `apply_renames.apply_clip` renames the clip in place (see below). `status.json` shows *Renamed X -> Y* / *Needs review: X*; the final summary counts renamed vs needs-review.

**Skipping already-handled clips:** `run_pipeline.py` (`--all` or the default first-clip mode) skips any inbox clip that is in `logs/rename-log.jsonl` (renamed or marked needs-review), already has a generated name (`YYYYMMDD_project_subject_clipType[_t##].ext`), or has a tool `.json` sidecar next to it / a live notes-store record at that path. Add **`--force`** to reprocess them anyway. Files inside subfolders of `inbox/` (batch folders included) are never picked up, not even with `--force`; clips moved into a batch folder still count as handled (the log follows them).

`logs/status.json` is updated at every step: `step`, `frames_done/frames_total` (n/9), `eta` (local ISO time) + `eta_seconds`, `clip_index/clip_total`, `needs_review`.

### Photos

Stills in `inbox/` are picked up by the same run (and ClipGauge's Add Clips / drag-drop): **`.jpg .jpeg .png .heic .heif .dng .webp .tif .tiff`**, any case; `._*` and other dotfiles are skipped. Per photo (`photos.py`):

- **Date** — EXIF `DateTimeOriginal` (the camera's own day; `OffsetTimeOriginal` used when present), else the file modified time. Read with a small stdlib EXIF reader (JPEG, TIFF/DNG, PNG, WebP, HEIC/HEIF). `python3 scripts/photos.py FILE…` shows what it finds (read-only).
- **Image for the model** — one JPEG, long side ≤ `describe.max_image_px` (672), rotated upright from the EXIF orientation, written to `processing/<stem>_frames/<stem>_photo.jpg` (cleaned up like video frames after a rename). Made with `sips` (built into macOS); HEIC/HEIF/DNG need it, other formats fall back to ffmpeg. The original file is only read.
- **Describe** — the single image goes to the VLM with a still-photo prompt (no Whisper). `clip_type` from the usual list where it fits (product shot / scenery → `broll`, packaging → `unboxing`, screenshot of a UI → `menu`, …) or `photo`.
- Everything else is the same as for clips: naming template `{YYYYMMDD}_{project}_{subject}_{type}[_t##].ext` (e.g. `20261004_trip_beach-sunset_photo.jpg`), `--folder` / `--project-from-folder`, `review_threshold`, never-overwrite `_t##`, rename log + undo, the Resolve guard, and the notes store (`"media_kind": "photo"`).

`--dry-run` (run_pipeline.py) forces a dry run for one invocation even when `dry_run` is false in config — nothing is renamed, sidecars go to `logs/dry-run/<folder>/`, the notes preview to `notes/dry-run/` (never `notes/clips/`). `--video` can be repeated:

```bash
python3 scripts/run_pipeline.py --dry-run --video "/Volumes/Lexar/DaVinci Resolve/Archive Footage/PXL_20260101_120000000.jpg"
```

### Central notes store (`notes/clips/`)

Every processed clip gets one record **`notes/clips/<clip_id>.json`** in the project folder — the same fields as a `.json` sidecar plus:

- `clip_id` — stable id from the **original camera file**: `<camera stem slug>-<8 hex of sha1(name | size | creation_time)>`, e.g. `cam-20261004125318-0181-d-1a2b3c4d`. It never changes when the clip is renamed or moved; reprocessing the same clip (`--force`) replaces its record.
- `current_path` — where the video is now. Updated on rename (pipeline / Apply), **Move renamed clips into folder** and undo.
- `applied` — the rename / needs-review note (as in sidecars); `store` — created/updated times, original name/path, `imported_from` for imported sidecars.

ExFAT-safe: plain files, no symlinks, written atomically (temp file + replace), so a reader (UI, transcript bundle) never sees half a record; one file per clip, so nothing is ever rewritten wholesale. `python3 scripts/notes_store.py` prints a summary, `--show <clip_id or video path>` one record. The web UI's **Notes** button reads from here when a clip has no `.md` beside it.

Moved a clip yourself in Finder (e.g. into a Resolve project folder)? The store keeps the last path it knows; the transcript bundle finds the file again by searching `inbox/` + `transcripts.search_roots` for its name, then for its exact byte size (catches clips renamed by hand), then for the original camera name.

**Sidecars next to clips are optional:** `sidecar.write_next_to_clip` (default `false`; UI checkbox *Write .json/.md notes next to each clip*, stored in `config.json`, applies from the next run). When on, everything behaves as before (`<stem>.json`/`.md` beside the clip, moved/renamed with it). Skip rules, Apply, Move-into-folder, undo and processing cleanup all work without sidecars (they read the store; cleanup puts a missing full transcript into the store instead of copying `.transcript.*` files beside the clip).

**One-time import of existing sidecars:** `python3 scripts/import_sidecars_to_store.py [--dry-run] [-v]` scans the whole Lexar (hidden folders, Resolve caches, `models/`, `processing/`, `logs/`, `exports/`, `notes/` skipped), copies every renamer sidecar into the store with the clip's current location (video with the same name beside it, else the one video of the same byte size in that folder), and is safe to re-run. Sidecars are left in place. `--remove-sidecars` (optional, not run by default) **moves** each imported `.json`/`.md` (+ `.transcript.*` and `._` companions) into `logs/backups/sidecars-<timestamp>/<path relative to the Lexar root>/` — nothing is deleted.

### Sidecar location (when sidecars are written)

| Situation | Where sidecars go |
|---|---|
| `dry_run: true` | `logs/dry-run/<source folder name>/<stem>.json|.md` |
| Source under `/DaVinci Resolve/` or `/DaVinci Resolve Media/` (`sidecar.protected_path_markers`) | same dry-run folder, even with `dry_run: false` |
| `dry_run: false` and `sidecar.write_next_to_clip: true` (e.g. clips in `inbox/`) | next to the video |
| `dry_run: false` and `sidecar.write_next_to_clip: false` (default) | none — notes store only |

ExFAT on macOS adds `._*` AppleDouble files beside anything written — harmless; scripts ignore them.

## Batch folder ("put this batch in a folder")

Optional, live mode only. Confident clips are renamed **straight into `inbox/<folder>/`** (one `os.rename` on the same volume) together with their `.json`/`.md` sidecars; needs-review / failed clips stay at `inbox/` top level with their original names. Without the option nothing changes (renamed in place).

```bash
python3 scripts/run_pipeline.py --all --folder "Vintage Collectibles Show"                       # rename into the folder
python3 scripts/run_pipeline.py --all --folder "Vintage Collectibles Show" --project-from-folder # + same {project} for all
python3 scripts/apply_renames.py --folder "Vintage Collectibles Show"            # Apply proposed names into the folder
python3 scripts/apply_renames.py --move-into "Vintage Collectibles Show" --dry-run   # preview moving clips already renamed
python3 scripts/apply_renames.py --move-into "Vintage Collectibles Show"             # move them (+ sidecars) into the folder
```

- **Folder name** (`apply_renames.sanitize_folder_name`): trimmed (runs of spaces collapsed), no `/` or `\`, no `..`, no leading dot, max 80 characters; ExFAT-illegal `: * ? " < > |` become `-`, trailing dots/spaces are dropped. Spaces and normal punctuation (`& , ' ( ) - _ ! #`) are kept. The folder is created on the first rename; an existing folder is reused (any upper/lower case spelling), a *file* with that name is refused.
- **Clashes:** nothing is ever overwritten — inside the folder `_t02`, `_t03` … is appended (sidecars follow the new name).
- **`--project-from-folder`** (UI: *Use folder name as the project in filenames*, default off): the `{project}` part of every new filename is the slugified folder name instead of the model's per-clip guess (fixes batches with `vintagecollectiblesshow` / `vintage-collectibles-sho` / `flea-market` mixed). The slug is cut at `naming.project_max_len` (24) on a whole word — *Vintage Collectibles Show* → `vintage-collectibles` — and clip-type words are not stripped from it, so the whole batch shares one project. Pattern unchanged: `{YYYYMMDD}_{project}_{subject}_{clipType}[_t##].ext`. Only affects new runs (Apply keeps names already proposed).
- **Logged:** the rename record's `new` is the final path inside the folder, plus `"folder"` (and `folder_created`); the report line has `final_path`, `folder` and `project_source`; the JSON sidecar's `applied` note carries `folder` and the `.md` shows *Folder: inbox/<folder>/*.
- **Move renamed clips into folder** (`--move-into`, UI button): for clips that were already renamed and still sit at `inbox/` top level *according to the rename log*, moves each clip and its sidecars (incl. copied transcripts) into the folder; one `{"action": "moved", "move_of": <rename id>, "from", "new", "folder"}` record each. Files not in the log (e.g. renamed by hand) and needs-review clips are left alone. Refused while a pipeline run is active (takes `logs/pipeline.lock`) and when `dry_run` is true; idempotent.

## Renaming in place (live mode)

With `"dry_run": false`, a processed clip is handled like this (`scripts/apply_renames.py`, shared by the pipeline, the UI and the CLI):

| Result | What happens |
|---|---|
| confident (`needs_review` false, proposed name present) | `inbox/CAM_…MP4` → `inbox/<proposed name>`; its `.json`/`.md` sidecars move next to it and are renamed to match (the JSON gets an `applied` note, the `.md` says *Renamed in place*). |
| needs review / no proposal / describe failed | original filename kept in `inbox/`; sidecars placed next to it (`inbox/<stem>.json|.md`); logged as `needs-review` so later runs skip it. |
| name already taken | never overwritten — `_t02`, `_t03` … is appended (the naming spec's take suffix) and noted in the log. |
| file outside `inbox/` (e.g. `DaVinci Resolve/…`), hidden or `._*` files | refused / ignored, untouched. |

**Processing cleanup:** right after each successful rename (live run or Apply), the clip's leftovers in `processing/` are deleted — `<original stem>_frames/` (incl. `vlm_672/`), `<original stem>_audio/` (whisper JSON + transcript), `<stem>.wav` and other per-clip temps, plus their `._*` companions. Matching is by the clip's **original** basename and exact names only (so `CAM_X` never touches `CAM_X_2`); only direct children of `processing/` are removed, symlinks are never followed, nothing outside `processing/` is touched. The full transcript (all segments with timestamps) is already in the `.json` sidecar; if it ever isn't, `<new name>.transcript.json/.txt` are copied next to the video first. needs-review / failed clips keep their processing files (for a re-check). What was removed is logged in the rename record (`cleanup.removed`, `cleanup.bytes`). Turn it off with `"cleanup_processing_after_rename": false` in `config.json` (default on).

```bash
python3 scripts/cleanup_processing.py --dry-run   # sweep: leftovers of clips already renamed (per rename-log.jsonl)
python3 scripts/cleanup_processing.py             # remove them (takes the pipeline lock; refused while a run is active)
```
The sweep lists what it keeps and why (needs review, still pending, or not a renamed clip — e.g. old `x4 unboxing` test frames are left alone). Each sweep is logged as an `action: "cleanup"` line (ignored by undo).

Renames use `os.rename` (same ExFAT volume → atomic) and the size is verified afterwards. macOS `._*` companions move with their file if macOS didn't already move them. Every action is appended to **`logs/rename-log.jsonl`** (`time`, `original`, `new`, `sidecars` moved, `confidence`, `reason`, `collision`, `batch`).

```bash
python3 scripts/apply_renames.py --dry-run   # preview: which processed clips would be renamed / marked needs-review
python3 scripts/apply_renames.py             # apply to everything already processed (report.jsonl, latest entry per clip)
```
`apply_renames.py` requires `dry_run: false`, takes `logs/pipeline.lock` (refuses while a pipeline run is active) and is idempotent.

### Undo

```bash
python3 scripts/undo_renames.py --dry-run            # preview undo of the LAST batch
python3 scripts/undo_renames.py                      # undo the last batch
python3 scripts/undo_renames.py --all --dry-run      # everything still applied (then without --dry-run)
python3 scripts/undo_renames.py --since 2026-10-06T13:00
python3 scripts/undo_renames.py --file 20261004_expo_entry-ticket_broll.mp4
```
Undo renames clips back to their original names in `inbox/` (top level — also clips that went into a batch folder), puts the sidecars (and any copied transcripts) back under their original names and removes the `applied` note; an `undo` line is logged. Deleted `processing/` files are not restored (they are re-created if the clip is reprocessed). Undone clips count as unprocessed again — except ones whose sidecar was written next to the video by a live run (they're still skipped; use `run_pipeline.py --force` to redo those).

Batch folders: undoing a `moved` record (the last batch after *Move renamed clips into folder*) moves the clips and sidecars back to `inbox/` top level, still renamed; undoing again (or `--all`) restores the original names. Undoing a rename whose clip was moved later undoes that move first. A batch folder is removed only if it ends up empty (Finder's `.DS_Store` / `._*` litter doesn't count); otherwise it stays.

### Unattended finish (`auto_apply_after_run.py`)

```bash
python3 scripts/auto_apply_after_run.py --launch --wait-pid <pipeline pid>
```
Starts a detached waiter (new session, `caffeinate -i`, log `logs/auto-apply-<timestamp>.log`) that waits until the current run ends (lock gone / pid dead), then (a) runs `apply_renames.py` over everything processed and (b) runs `run_pipeline.py --all` once more in live mode (normal lock) for anything still pending in `inbox/`, and (c) sweeps `processing/` (`cleanup_processing.py`). If `dry_run` is `true` again by then, it renames nothing. `--cleanup-only` waits for the run and only does the sweep (log `logs/auto-cleanup-<timestamp>.log`).

## Quick start (after INSTALL.md)

```bash
cd /Volumes/Lexar/AI-Video-Renamer
export OLLAMA_MODELS="$PWD/models"
python3 scripts/detect_ram.py
python3 scripts/check_resolve.py && echo "Resolve is off"
python3 scripts/selftest.py -v          # offline unit tests, no downloads

# one clip (read-only; sidecars land in logs/dry-run/):
python3 scripts/run_pipeline.py --video "/Volumes/Lexar/DaVinci Resolve/E-Reader Review/x4 unboxing.MP4" --reuse-frames
# or everything in inbox/:
python3 scripts/run_pipeline.py --all
```

Prefer clicking? Use **ClipGauge** (below): drop clips on its window or its menu bar icon.

Useful flags: `--reuse-frames` (reuse complete, newer-than-video frames), `--skip-whisper`, `--skip-describe`, `--model qwen2.5vl:3b`, `--dry-run` (force a dry run this time), `--video PATH` (repeatable; videos or photos).

Individual steps:

```bash
python3 scripts/describe_clip.py CLIP --print-prompt     # show prompt + JSON schema, no model call
python3 scripts/describe_clip.py CLIP --out /tmp/desc.json
python3 scripts/transcribe.py CLIP
python3 scripts/transcribe.py --print-model              # tier's whisper model
python3 scripts/media.py CLIP                            # duration / audio / date
python3 scripts/sidecar.py "logs/dry-run/<folder>/<stem>.json"   # re-render .md
python3 scripts/apply_renames.py --dry-run               # preview in-place renames
python3 scripts/undo_renames.py --dry-run                # preview undo of the last batch
```

Search the catalogue: `grep -ril "unboxing" inbox/*.md logs/dry-run/` (or Spotlight on the `.md` files).

## Legacy web UI (hidden in v0.6, removed in v0.7)

Everything the web page did is now in **ClipGauge** (see below): adding clips (drag and drop, copy or process in
place), Start/Stop, results with Undo and review, Move into folder, the transcript bundle, Sort into Projects and
Instructions. ClipGauge talks to the engine directly (`scripts/clipgauge_cli.py`); nothing needs port 8765 any more.
The web UI no longer starts automatically and has no buttons or menu items. The `Start/Stop Renamer UI.command`
launchers were retired to `logs/backups/2026-10-08_pre-clipgauge-v06/`.

For one more version it still runs from Terminal as a fallback (same engine, same locks and guards):

```bash
python3 scripts/web_ui.py --background   # start detached, then open http://127.0.0.1:8765
python3 scripts/web_ui.py --status
python3 scripts/web_ui.py --stop
```

v0.7 deletes it (`scripts/web_ui.py`, its `ui` config keys and tests, and these notes).

## Transcript bundle (for script writing)

`scripts/build_transcript_bundle.py` gathers the Whisper transcript of every clip (from the central notes store first, then any `.json` sidecars on disk the store doesn't have — deduped by clip id and original camera name + size) into **one file pair** you can hand to a script-writing bot, with every line traceable to its video:

```bash
python3 scripts/build_transcript_bundle.py                         # all of inbox/ (incl. batch subfolders)
python3 scripts/build_transcript_bundle.py --folder "Show 2026"    # just inbox/Show 2026/
python3 scripts/build_transcript_bundle.py --path "DaVinci Resolve/Retro Game Expo"   # any folder on the Lexar
python3 scripts/build_transcript_bundle.py --everywhere            # inbox/ + DaVinci Resolve, DaVinci Resolve Media, Video Assets
python3 scripts/build_transcript_bundle.py --date 2026-10-04       # only clips recorded that day (combines with the above)
#  --include-silent  full entries for clips with no speech (default: brief list at the end)
#  --omit-silent     leave them out entirely
#  --keep-repeats    don't collapse identical consecutive lines
#  --dry-run         scan + print counts, write nothing        --list-folders  folders that contain processed clips
#  --store-only / --sidecars-only   read just one source (default: both)
#  --exclude-photos  leave stills out (default: photos are listed with the silent assets, "media_kind": "photo")
```

Clips you moved into a Resolve project folder are still found (store path, else same name / same size in `inbox/` + search roots); use `--path` / `--everywhere` (or that folder in the UI dropdown). A clip is flagged *video file not found* only when none of that matches. JSON clips carry `note_id` (store id) and `notes_source` (`store`/`sidecar`); counts include `from_store`, `from_sidecars`, `video_not_found`.

Output in **`exports/`** (created on first use; never overwritten — a second export in the same minute gets `-2`):

- `transcripts-<scope>-<YYYYMMDD-HHMM>.json` — schema `ai-video-renamer.transcript-bundle` v1: `generated_at`, `scope`, `counts` (clips scanned / with speech / silent / lines), `total_speech_seconds`, a `how_to_use` note for the AI, `citation_format`, then `clips[]` in **recording-time order**. Each clip: `file`, `path` (relative to the Lexar root), `original_name` (camera file), `recorded_at` (+ `_utc`, and where the time came from), `duration`, `clip_type`, `summary`, `keywords`, `on_screen_text`, `subjects`, `objects`, `has_speech`, `speech_seconds`, `transcript` (full text) and `segments[]` = `{id: "<file>#0003", start, end, start_ts: "HH:MM:SS.mmm", end_ts, text}`. Clips without speech are listed in `silent_clips[]` (file, path, duration, type, summary, keywords, on-screen text) so the bot can still suggest them as b-roll.
- `transcripts-<scope>-<YYYYMMDD-HHMM>.md` — the same grouped by clip (heading = file name, then path · recorded time · length · type · camera file, summary, on-screen text, keywords) with one `- [start → end] #NNNN text` line per segment; compact enough to paste into a chat bot.

Clean-up rules: segments that are only Whisper markers (`[BLANK_AUDIO]`, `[Music]`, `(wind blowing)`, `>> [INAUDIBLE]`, `♪`) are dropped; `[BLANK_AUDIO]` inside a line is removed; other inline markers such as `[INAUDIBLE]` or `(chuckles)` stay. A leading `>>` (speaker change) is stripped and kept as `speaker_change: true`. Identical consecutive lines — Whisper's hallucination loop over wind/water noise — become one segment with `"repeats": n` and a note on the clip. Segment numbers are 1-based over the kept lines, so ids are stable for the same sidecar and options; `whisper_index` points back into the sidecar's own segment list.

Read-only for media: the script only reads sidecars and writes the two export files — it never renames, moves or deletes a video or sidecar, and it takes no pipeline lock.

### One run at a time (`logs/pipeline.lock`)

`run_pipeline.py` (and `apply_renames.py` / `undo_renames.py`) take `logs/pipeline.lock` (pid, process group, host, start time, who launched it) before it touches `status.json`, so a Terminal run and a UI run can't overlap: the second one prints *"Another pipeline run is already active…"* and exits with code **3**. A lock whose pid is dead (crash, reboot, drive moved to the other Mac) is treated as stale and replaced automatically. Ctrl-C, closing the Terminal window, or the UI's Stop button all end the run cleanly (status `stopped`, lock removed). Check it with `python3 scripts/pipeline_lock.py` (`--clear-stale` removes only a stale lock).

## Config keys (config/config.json)

| Key | Default | Meaning |
|---|---|---|
| `review_threshold` | `0.6` | below → needs-review |
| `clip_types` | 8 video types + `photo` | allowed types; `photo` is only offered to the model for stills |
| `whisper.enabled` | `true` | transcription step on/off (auto-skips if not installed) |
| `whisper.model` | `{"air": "base.en", "pro": "large-v3-turbo"}` | ggml model per tier (`models/whisper/ggml-<name>.bin`); env `AI_VIDEO_RENAMER_WHISPER_MODEL` overrides |
| `whisper.cli_path`, `models_dir`, `language`, `threads`, `keep_wav`, `excerpt_chars`, `timeout_s`, `download_base_url` | | whisper.cpp details |
| `describe.enabled` | `true` | VLM step on/off |
| `describe.model` | `null` | `null` = tier `prefer` → `fallback` from `ram-tiers.json` |
| `describe.ollama_url` | `http://127.0.0.1:11434` | Ollama HTTP API |
| `describe.max_image_px`, `num_ctx`, `tokens_per_image_min`, `temperature`, `timeout_s`, `keep_alive`, `json_retries`, `transcript_excerpt_chars` | `672`, `16384`, `1100`, `0.1`, `900`, `10m`, `1`, `1200` | VLM call tuning |
| `describe.max_output_tokens`, `repeat_penalty`, `max_list_items` | `1024`, `1.1`, `12` | stop runaway generation (qwen2.5vl looped keywords until the context filled without these) |
| `sidecar.enabled`, `write_json`, `write_md` | `true` | sidecar output (when sidecars are written) |
| `sidecar.write_next_to_clip` | `false` | live mode: also write `<stem>.json/.md` next to each clip (UI checkbox). The notes store is always written |
| `notes_store.dir` | `notes` | central notes store folder (records in `<dir>/clips/`) |
| `dry_run` | `true` | `false` = live: rename in place in `inbox/` (see *Renaming in place*) |
| `cleanup_processing_after_rename` | `true` (implicit) | delete the clip's `processing/` leftovers after a successful rename |
| `sidecar.dry_run_dir` | `logs/dry-run` | sidecar folder while dry-run (also holds `report.jsonl`) |
| `sidecar.protected_path_markers` | DaVinci Resolve paths | never write sidecars inside these |
| `ui.host`, `ui.port` | `127.0.0.1`, `8765` | legacy web UI only (Terminal fallback in v0.6; removed in v0.7) |
| `ui.poll_ms`, `ui.results_limit`, `ui.min_free_gb` | `2500`, `200`, `2` | status refresh, rows in the results table, free space kept on the Lexar when uploading |
| `transcripts.exports_dir` | `exports` | where transcript bundles are written (optional key) |
| `transcripts.search_roots` | `["DaVinci Resolve", "DaVinci Resolve Media", "Video Assets"]` | Lexar folders (relative to the drive root) searched by `--everywhere` and listed in the UI dropdown (optional key) |
| `naming.*` | | `lowercase_ext`, slug max lengths, fallbacks, `take_suffix_max`, `strip_clip_type_words` (drop e.g. `-unboxing` from project/subject slugs) |

## RAM tiers

See `config/ram-tiers.json`. Air (~16GB) prefers `qwen2.5vl:7b` (fallback `3b`) + whisper `base.en`. Pro (~64GB) prefers `gemma3:12b` or stronger + whisper `large-v3-turbo`. Auto-detect at launch; override with `AI_VIDEO_RENAMER_TIER=air|pro` or `override_tier` in config.

## Status

Live progress is written to `logs/status.json` (queue, current file, step, frames n/9, ETA from rolling averages in `logs/timings.json`). `python3 scripts/status.py show` prints it.

## ClipGauge (the app)

`ClipGauge.app` (in this folder) is the whole app: a menu bar gauge in the style of GrokGauge plus a main window.
Until v0.5 it was spelled **ClipGauge**; v0.6 renamed it (new bundle id `com.retrocombs.ClipGauge`) and carried
your settings over. The menu bar shows the mark, plus a green `NN%` while a run is active. Click it for the popover:

- **Progress ring:** clip X of Y, mode (live or dry run) and who started the run.
- **Now processing card:** current clip, current step, and ETA (HST).
- **Last-run error card** (only when the last run ended with errors): which clip, the reason and what happens next
  (for example: an empty 1.2 KB recording is marked *needs review* on the next run). **Dismiss** hides it for that run;
  `logs/status.json` is never edited.
- **Inbox card:** last renamed file, needs-review count, clips waiting, Instructions on/off.
- **Ask the model…** box (⌘K): opens the chat with your question.
- **Open at login** switch (needs the app in /Applications).
- **Buttons:** **Start / Stop**, **Open** (the main window), **Inbox** (Finder), **Sort into Projects…**.

**Main window** (popover **Open**, gear › **Open ClipGauge**, or ⌘0):

- **Add Clips:** drop files or folders onto the window **or onto the menu bar icon**, or click **Choose…**. Folders
  are searched (hidden folders, packages and folders on hold are skipped). Two modes:
  - **Copy into inbox:** copies to `inbox/` with a progress bar (temp `.uploading-` file, renamed when complete; the
    modified date is kept; never overwrites: same name and size is skipped, otherwise `_2`, `_3`…). Optional folder
    name and *use folder name as the project*, then *Start processing when the copy finishes*.
  - **Process in place:** renames clips where they are, no copy (`run_pipeline.py --source PATH`). Refused: folders on
    hold (Held Project, Archive Footage, Old Card Dump, anywhere in the path), DaVinci Resolve's own
    folders (BackUps, CacheClip, ProxyMedia…), Blackmagic Cloud-synced projects, iCloud/Dropbox/Google Drive/OneDrive
    folders and the project's own folders. Inside **/Volumes/Lexar/DaVinci Resolve** it asks first, because renaming
    media Resolve already imported makes it go offline (`--confirm-resolve`). Logged and undoable like inbox renames.
- **Start inbox / Stop** and **Dry run (preview only)** in the top bar. Runs start detached (new session +
  `caffeinate -i`) and respect the run lock, the update lock and the Resolve guard.
- **Results:** original → new name, type, confidence and status (renamed / needs review / skipped / dry run).
  Double-click reveals in Finder. The ⋯ menu has **Undo this rename**, **Undo whole batch…** and **Show in Finder**.
  **Needs review** filter + **Review…** editor: accept the proposed name or type your own (extension kept; logged in
  `rename-log.jsonl`, undoable).
- **Tools:** **Move renamed clips into a folder**, **Transcript bundle** (scope picker, builds into `exports/`, Open /
  Reveal), Sort into Projects, Instructions, Setup & Updates, About.

**Ask the Model** (⌘K): chat with the local Ollama (`/api/chat`, streamed; 127.0.0.1 only — nothing leaves the Mac).
Follow-ups keep the history; pick any installed model (default: the tier's vision model, `qwen2.5vl:7b` on the Air);
attach an image (drop it on the window) or a clip's notes and transcript; copy any message; **Save** writes Markdown
to `exports/chats/`; **New chat**; **Stop** ends a reply. It's paused while a processing run or an update is active,
so the Air's 16 GB stay with the run.

It never renames anything itself: every action runs `scripts/clipgauge_cli.py` (JSON bridge to the engine), so
`logs/pipeline.lock`, `logs/updates.lock`, the Resolve guard and `logs/rename-log.jsonl` all still apply.

Guards and notices:

- **Lexar not connected:** the menu bar shows `–` and the controls are greyed out.
- **DaVinci Resolve is open:** the mark turns orange, the popover shows "Paused, Resolve is open" and Start is disabled. Resolve is detected by bundle id `com.blackmagic-design.DaVinciResolve*`.
- **Notifications:** you get one when a run ends and when a clip needs review. If macOS notifications aren't allowed, it falls back to a basic AppleScript notification. (New bundle id in v0.6: macOS asks once again.)

- **Launch:** double-click `ClipGauge.app`. It's ad-hoc signed and built on this Mac, so it normally opens directly. If Gatekeeper complains (for example after copying it from another Mac), right-click it, choose **Open**, then **Open** again.
- **Finding it:** if the menu bar is full, macOS may tuck the icon behind the `»` overflow, like GrokGauge. ⌘-drag it into the visible area.
- **Project folder:** the app finds `AI-Video-Renamer/` next to itself, so it moves with the drive to the Pro. If you copy it to /Applications (needed for **Open at login**), it finds the project on any mounted drive (`/Volumes/*/AI-Video-Renamer`). Set `CLIPGAUGE_ROOT` to override (the old `CLIPGUAGE_ROOT` still works).
- **Ejecting the Lexar:** quit ClipGauge first while it runs from the Lexar. A running app keeps the drive busy. Running it from /Applications avoids this.
- **Rebuild:** run `app/ClipGauge/build.sh`. It needs only the Xcode Command Line Tools (swiftc). Sources are in `app/ClipGauge/Sources/`, and the build is ad-hoc signed. `UNIVERSAL=1` adds x86_64.
- **Debug:**
  - `ClipGauge.app/Contents/MacOS/ClipGauge --print-state` prints what the app sees.
  - `--render-preview DIR [--chat-prompt TEXT]` writes popover, main window, chat and menu bar PNGs without Screen Recording permission.
  - `python3 scripts/clipgauge_cli.py status` (or `inbox`, `results`, `check-path PATH`…) shows what the app gets from the engine.

### What's new in ClipGauge v0.2

- **Clearer Start errors.** When a run stops right away, the alert gives the real reason, the last lines of the run log and the log path, instead of "Pipeline exited immediately (code 1)". If the run had nothing to do, it says so.
- **Unreadable clips no longer crash a run.** An empty or interrupted recording (for example a 1 KB MP4 with no video stream) is marked **needs review** with the reason. Its name is kept, it's logged in `logs/rename-log.jsonl`, and the run carries on.
- **Clickable Inbox rows:**
  - **Last renamed** shows the file in Finder.
  - **Needs review** shows those clips.
  - **Waiting to process** opens `inbox/`.
- **Gear menu (top right):** **Setup…**, **About ClipGauge** and Quit.
  - About shows the version, the retroCombs credits, a privacy note and the third-party licenses.
- **Setup window:** choose or create a project folder and check every requirement (Homebrew, Python, ffmpeg, Ollama, whisper.cpp, RAM tier, vision and speech models).
  - **Install Missing…** opens `scripts/setup-mac.sh` in Terminal, which asks before every install or download. `scripts/setup-mac.sh --check` only reports.
  - Setup opens by itself on a Mac with no project folder.
- **Portable engine.** The scripts are bundled inside the app at `Contents/Resources/engine`, so **Setup › Choose or Create…** can make a new `AI-Video-Renamer/` project on any drive. New projects start in dry-run mode. Existing files are never overwritten.
  - The scripts now find the project from their own location instead of a hard-coded `/Volumes/Lexar/...`. Override it with `AI_VIDEO_RENAMER_ROOT`.
- **Docs:**
  - `SETUP.md`: setup guide for new users.
  - `docs/DISTRIBUTION.md`: draft plan for a public release. Nothing has been published.
- **Debug:**
  - `--print-state --checks` adds the Setup checks.
  - `--create-project DIR` runs the same code as Create Project.
  - `--render-preview DIR` now also writes About and Setup PNGs.

### What's new in ClipGauge v0.3: updates for models and tools

**Setup › Updates** (also gear › **Check for Updates…**) checks everything the renamer depends on:

| What | How it's checked (read-only) | Upgrade |
|---|---|---|
| Ollama models in use: the tier's preferred model plus every model in `models/` | Local manifest sha256 compared with `registry.ollama.ai/v2/library/<name>/manifests/<tag>`. The size shown is only the layers that aren't already in `models/blobs` | Pulled through the running Ollama, which must be serving `<project>/models` (checked first) |
| Homebrew `ollama`, `whisper.cpp`, `ffmpeg` | **Check Now** runs `brew update --quiet`, then `brew outdated --json=v2` for just those formulae. The size shown is the bottle size | `brew upgrade <formula>`. After ollama, the `com.retrocombs.ollama-lexar` LaunchAgent is restarted if present |
| Whisper `models/whisper/ggml-*.bin` | sha256 compared with Hugging Face's `X-Linked-ETag` for ggerganov/whisper.cpp (HEAD request, no download) | Downloaded to `.ggml-*.bin.part` (resumable) and sha256-verified. The old file is kept as `.bak` until whisper-cli loads the new one |
| Newer model families (`config/updates.json`) | Checked against the registry for existence and size | **Info only.** Shows a link; never downloads or switches |

**Safety rules for upgrades:**

- **Nothing is upgraded without a click.** **Upgrade** and **Upgrade All…** first show the download size and an estimated time (slow hotel Wi-Fi vs fast broadband).
- **Model upgrades are reversible until verified.**
  - The current model is first copied to `<tag>-clipgauge-prev` (ClipGuage ≤ v0.5 used `-clipguage-prev`; both are ignored as models).
  - The new one must answer a tiny prompt.
  - On success, the backup tag is removed with Ollama's own delete, which frees the old blobs. No blob is deleted by hand.
  - On failure, the previous version is restored.
- **Upgrades run in the background** in a new session with caffeinate, so closing the popover or Setup doesn't stop them.
  - Progress (percent, MB/s, ETA in HST) shows in Setup, and as a blue `↓NN%` in the menu bar.
  - **Cancel** stops after the current chunk; **Resume** continues where it stopped.
- **Updates and processing never overlap.**
  - Upgrades are refused while a processing run holds `logs/pipeline.lock` or DaVinci Resolve is open.
  - While an upgrade holds `logs/updates.lock`, Start is disabled in ClipGauge (and the legacy web UI returns 409), and `run_pipeline.py` exits with code 5.
- **Weekly automatic check** is on by default. It only checks (no `brew update`, no downloads) and sends a notification if something is new.
- **Files:**
  - Last check: `logs/updates-state.json`
  - Current or last job: `logs/updates-status.json`
  - Logs: `logs/updates-YYYYMMDD.log` (one line per check and upgrade) and `logs/updates-YYYYMMDD-HHMMSS.log` (job output)

**Same thing from Terminal:**

```bash
python3 scripts/check_updates.py                  # summary (add --json; --no-brew-update skips brew update)
python3 scripts/apply_updates.py --all            # asks before upgrading; --items tool:ollama,model:qwen2.5vl:7b
python3 scripts/apply_updates.py --all --yes --detach && python3 scripts/apply_updates.py --status
python3 scripts/apply_updates.py --cancel         # then --resume --yes to continue
python3 scripts/clipgauge_cli.py status            # run/update locks as ClipGauge sees them
```


### What's new in ClipGauge v0.4: Sort into Projects

**Gear › Sort into Projects…** (or the **Sort into Projects…** button in the popover) files clips into DaVinci Resolve projects on the Lexar.

**Layout.** Projects live directly in `DaVinci Resolve/<Project>/` with these folders:

- `A-Roll/`, `B-Roll/` and `Images/`
- optional `_Notes/` (.json/.md sidecars), `_Review/` (low-confidence clips) and `Exports/` (finished renders, never touched)

There are no channel folders and no separate Projects folder. Never touched:

- Resolve's own folders (`BackUps`, `CacheClip`, `.gallery`, `.blackmagicsync-v2`, `ProxyMedia`, `OptimizedMedia`)
- any Blackmagic Cloud-synced project (a folder containing `.syncprojectinfo.json`)
- folders listed in `hold_sources`, which are refused even for a dry run

**How it decides.** All settings are in `config/project-sort.json`:

- **Project name.** It's the source folder's name. A folder with an uninformative name (`CAM_…`, a date, `New Folder`, `DCIM`…) is split into shoots instead.
- **Shoots.** A new shoot starts after a gap in recording time (`gap_minutes`, default 60) or on a new day. Times come from the `CAM_YYYYMMDDHHMMSS` filename, the creation time or ffprobe. Neighbouring shoots with similar notes are joined again.
- **Names for shoots,** in this order:
  - text read from a short slate shot (product box or name card)
  - the clips' suggested project and keywords
  - "Shoot YYYY-MM-DD HHMM"

  Every proposed name is flagged so you can rename it before applying.
- **Matching existing projects.** Each shoot is compared with existing projects by name and content (keywords in their notes and file names). A good match is offered as **Merge into "…"**.
- **Two shoots in one folder.** A folder that looks like two shoots (a big change in content or time) gets a **Split into A + B** option. Nothing is split unless you tick it.
- **Folder by clip type** (`type_map`):
  - A-Roll: talking-head, unboxing, menu and screen recordings.
  - B-Roll: broll, pans, cutaways, bench, boot, fail and gameplay.
  - Images: photos.
  - `_Review`: clips with low confidence (< `review_threshold`), marked needs-review, or with no notes. Check those by hand.
- Each clip's `.json/.md` sidecars go to `_Notes/`.

**Safety:**

- **A dry run always comes first.**
  - It's grouped by project: clip/photo count, time range, a one-line description, the destination, and whether the project is new or existing.
  - The plan is saved to `logs/sort-plans/`. Nothing moves until you click **Apply…** and confirm.
- **Apply is refused** while DaVinci Resolve is open, a processing run holds `logs/pipeline.lock`, or an update holds `logs/updates.lock`. The sort itself takes `logs/pipeline.lock`, so processing waits for it.
- **Never overwrites.** A same-name file gets a `_2` suffix (or is skipped with `"on_conflict": "skip"`). Files that changed since the dry run are skipped and reported. Cross-drive moves aren't done.
- **Undo map for every apply**, in `logs/sort-undo-<plan>.jsonl`.
  - **Undo Last Sort…** moves everything back.
  - It also removes the folders the sort created (only if they're empty) and restores the clip notes' paths.
- **Resolve relinking.** Clips already imported into a Resolve project go offline when they move; the dry run warns about this. Relink them in Resolve (Media Pool › Relink Selected Clips…), or exclude those groups.
- **Clip notes follow the move.** `notes/clips/<id>.json` gets the new `current_path` and a `sorted` record.

**Already-sorted projects.** Running the dry run on a project already in this layout is a check. Files in a bucket folder stay where they are (`respect_existing_buckets`). Only loose files at the project's top level would move.

**Same thing from Terminal:**

```bash
python3 scripts/sort_projects.py --list-sources
python3 scripts/sort_projects.py --source "CAM_20261009" --dry-run          # bare name = folder under DaVinci Resolve/
python3 scripts/sort_projects.py --source inbox --dry-run --gap-minutes 90
python3 scripts/sort_projects.py --apply --plan logs/sort-plans/sort-plan-<id>.json [--edits edits.json]   # asks first
python3 scripts/sort_projects.py --undo            # last sort (asks first); --dry-run to preview, --plan-id <id>
python3 scripts/sort_projects.py --history
# edits.json: {"g1": {"project": "Router Review"}, "g2": {"include": false}, "g3": {"split": true},
#              "g4": {"merge_into": "Acme R3000"}}   (merge_into: null = new project)
```

(Legacy, until v0.7: the web UI also has a **Sort into projects** card; its API is `GET /api/sort/sources`, `POST /api/sort/plan`, `POST /api/sort/apply` (needs `"confirm": true`), `POST /api/sort/undo` and `GET /api/sort/history`.)

### What's new in ClipGauge v0.5: custom instructions + a fixed update check

**Custom instructions** (ClipGauge › gear › **Instructions…**, ⌘I in its windows):

- **Standing instructions** (≤ 2000 characters): added to the vision describe/naming prompt for every clip and photo,
  e.g. "Call the router Acme R3000".
- **Next run only** (≤ 1000 characters): used by the next run, then cleared after a *live* run unless **Keep after the
  run** is ticked (dry runs never clear it). A line like `Project: Retro Game Expo` also names the shoot in
  **Sort into Projects** (used directly when the source is one shoot, offered as a suggestion otherwise).
  From Terminal: `run_pipeline.py --instructions-file FILE` replaces it for one run; `--no-instructions` turns everything off.
  `sort_projects.py` takes the same two flags.
- **Glossary** (≤ 80 terms, ≤ 40 characters each, ≤ 600 characters in all): given to whisper-cli as its initial prompt
  (`--prompt`, improves spelling such as "Commodore 64", "Raspberry Pi", "retroCombs") and to the vision model. Sort into
  Projects also uses it to fix spellings in proposed project names ("Acme Router" → "Acme Router").
- **Safety:** your text goes into the user message inside a delimited `<<<USER_GUIDANCE … USER_GUIDANCE>>>` block,
  *before* the output format, rules and Ollama's JSON schema, which stay last and authoritative. Control characters, code
  fences and look-alike delimiters are removed. With no instructions the prompt is exactly as before.
- **Traceability:** every clip's notes record (`notes/clips/<id>.json`, and dry-run records) gets
  `"instructions": {hash, standing, batch, batch_source, glossary, used_for}`. The popover's Inbox card shows
  **Instructions: Active/Off**.
- **Preview Prompt…** shows the full system + user prompt for a sample clip (or photo) plus the Whisper `--prompt`, built
  from the text in the window (saved or not). Terminal: `python3 scripts/instructions.py --preview [--photo]`.
- Stored in `config/custom-instructions.json` (`scripts/instructions.py --show | --set-standing F | --set-glossary F |
  --set-next F [--keep] | --clear-next`). Whisper: `transcribe.py VIDEO --out-dir /tmp/x --no-status [--prompt TEXT | --no-glossary]`;
  `whisper.use_glossary: false` in config.json turns the glossary off for Whisper only.

**Update check fix:** after `brew upgrade ollama` the restart check now polls `/api/version` for up to 90 s
(`updates.ollama_restart_wait_s`) with retries and waits for the new version; a slow restart is reported as a note,
never as a failure. `python3 scripts/apply_updates.py --reverify` re-checks a finished job's failed items read-only
(no brew update, no downloads) and clears the ones that are actually fine.

### What's new in ClipGauge v0.6: the all-in-one app (and the spelling)

- **Renamed ClipGuage → ClipGauge** (matches GrokGauge): app, bundle id `com.retrocombs.ClipGauge`, sources in
  `app/ClipGauge/`, notifications, docs. Settings (project folder, update settings, window positions, menu bar icon
  position) were copied from the old app once. The old `ClipGuage.app` was removed after the new one launched (a zip
  is in `logs/backups/2026-10-08_pre-clipgauge-v06/`).
- **Main window** with Add Clips (copy with progress, or process in place), run controls with a dry-run toggle,
  Results with per-row/per-batch Undo and a review editor, Move into folder and the transcript bundle.
- **Drop on the menu bar icon** opens the window with the clips queued.
- **Ask the Model** chat (local Ollama only).
- **Last-run error card** in the popover with the reason and a Dismiss button.
- **Engine:** `scripts/clipgauge_cli.py` (JSON bridge), `scripts/inplace.py` (in-place guard),
  `run_pipeline.py --source PATH [--confirm-resolve]`, `undo_renames.py --id LOG_ID [--json]`,
  `scripts/renamer_actions.py` (the shared non-HTTP code, moved out of `web_ui.py`). Unreadable clips now show
  ffprobe's own reason (e.g. *moov atom not found*).
- **Web UI retired:** hidden (no auto-start, no buttons); Terminal fallback for one version, see *Legacy web UI*.

### What's new in ClipGauge v0.6.1: fixes + a Settings window

- **Model updates on Ollama 0.40+.** Ollama 0.40 keeps tags under `models/manifests-v2/ollama.com/…` as a *manifest
  list*, and its "local compat GGUF migration" re-packs llama.cpp models under a new digest (the weights are kept, so a
  migrated model uses roughly twice the disk). The old checker looked only at `models/manifests/registry.ollama.ai/…`
  and compared the registry digest with the local one, so every migrated model looked outdated and every "upgrade"
  failed verification and was rolled back. The checker now reads both layouts, recognises the re-packed copy (child
  digests, the pre-migration digest, or the registry manifest + every layer already on disk) and pins the pairing in
  `logs/model-versions.json`. An upgrade is skipped when the model is already current, and a leftover
  `<model>-clipgauge-prev` tag that is an exact duplicate is removed through Ollama (`apply_updates.py --cleanup-prev
  [--preview]`). Never delete files in `models/` by hand.
- **Ask the Model** only offers real chat models (Ollama's internal `llamacpp:<hash>` rollback entries are hidden),
  defaults to the configured vision model, never sends an empty model, and explains errors in plain words.
- **Error card**: clears itself when every file it mentions has left the inbox or when the error came from an older
  engine that has since been fixed; Dismiss is remembered across restarts.
- **Settings… (⌘,)** — like GrokGauge: *Layout* (drag, ↑/↓ or checkbox to reorder/show/hide the popover sections and
  the action buttons; Reset Layout to Default) and *Menu Bar* (icon + percent, icon only, percent only, icon + review
  count; optional time left). Saved in the app's preferences. Setup… moved to ⌥⌘,.
- Popover header has a little more room at the top.

### What's new in ClipGauge v0.7: GrokGauge's core features

ClipGauge's Settings window (⌘,) now matches GrokGauge's, tab for tab:

- **Layout** — reorder and show/hide the popover sections and action buttons (drag, ↑/↓, checkbox); Reset Layout to Default.
- **Menu Bar** — icon + percent / icon only / percent only / icon + review count, optional time left, *Hide the
  ClipGauge logo*, live preview; **global keyboard shortcut** (default ⌃⌥C, GrokGauge uses ⌃⌥G) with a recorder;
  a list of the in-app shortcuts.
- **Colors & Alerts** — level colors (Normal = processing/OK, Warning = paused/needs review, Critical = errors) with
  the macOS color picker, the *Colorblind-Friendly* (Okabe–Ito) preset and *Reset Colors*, the same "too similar"
  warning as GrokGauge; notification switches (runs, review, model/tool updates); Launch at login.
- **Sync** — choose a folder your Macs already sync (if GrokGauge already syncs settings, its folder is offered). ClipGauge keeps one file there, `ClipGauge/settings.json`, with
  preferences only — never clips, notes, transcripts, models or paths. Changes merge group by group (newest wins),
  unknown keys from newer builds are kept, another app's file is never touched, and the previous file is backed up
  in `~/Library/Application Support/ClipGauge/sync-backups` before it's replaced. Export…/Import…, Reset All Settings….
  Sync is **off** until you choose a folder.
- **Diagnostics** — engine, Ollama, models and app status plus *Copy Report* (no clip names or paths).
- **About** — GrokGauge's About tab (maker card, Updates, Links) plus ClipGauge's This Mac / Privacy / Built with.
  *About ClipGauge* opens this tab. The app's own update check compares the running app with the source in
  `app/ClipGauge` (local, no network) and shows the rebuild command when a newer version is there.

`ClipGauge.app/Contents/MacOS/ClipGauge --sync-selftest` runs the sync checks in a temporary folder.

## Credits & contact

Created by **Steven Combs** (**retroCombs**) and built together with **Grok Bot**, Steven's AI assistant.

- **YouTube:** [@retroCombs-Tech](https://www.youtube.com/@retroCombs-Tech)
- **Support the project:** if ClipGauge saves you an evening of renaming clips, you can
  [tip via PayPal](https://paypal.me/stevencombs). Thank you!
- **Support ClipGauge…** (dropdown footer, app menu, Settings › About) just opens
  [paypal.me/stevencombs](https://paypal.me/stevencombs) in your browser. ClipGauge sends nothing.

The same links are in ClipGauge under **Settings › About**.
