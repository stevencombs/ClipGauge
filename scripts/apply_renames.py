#!/usr/bin/env python3
"""
Apply proposed names IN PLACE inside inbox/ (live mode). Shared by run_pipeline.py (per clip, right after its
sidecars are written), the web UI ("Apply proposed names") and this CLI.

  python3 scripts/apply_renames.py --dry-run   # preview only: what would be renamed / marked needs-review
  python3 scripts/apply_renames.py             # apply (needs config dry_run false; refused while a pipeline run is active)
  python3 scripts/apply_renames.py --json      # machine-readable summary (used by the web UI)
  python3 scripts/apply_renames.py --folder "Show 2026"        # apply; confident clips go into inbox/Show 2026/
  python3 scripts/apply_renames.py --move-into "Show 2026" [--dry-run]
                                               # move clips ALREADY renamed (still at inbox/ top level, per the
                                               # rename log) + their sidecars into inbox/Show 2026/ (logged, undoable)

Rules
  * Candidates: the latest logs/dry-run/report.jsonl entry per source, plus inbox videos whose next-to-video
    .json sidecar holds a proposal that was never applied. Only files that still exist DIRECTLY in inbox/ are
    touched — anything else (DaVinci Resolve folders, any other path) is refused. Hidden and ._ files are ignored.
  * Confident (needs_review false + a proposed name) -> renamed in place to inbox/<proposed>. The .json/.md
    sidecars move next to it, renamed to match. The new name is the "fully processed" marker.
  * needs_review / no proposal / failed -> original filename kept in inbox/; its sidecars are placed next to it
    (inbox/<stem>.json|.md) and the decision is logged, so later runs skip it.
  * Never overwrites: if inbox/<proposed> (or its .json/.md names) exists, _t02, _t03 … is used (naming spec take suffix).
  * os.rename (same ExFAT volume -> atomic); the source size is compared with the destination size afterwards.
    macOS ._ AppleDouble companions move with their file if macOS didn't already move them; never an error.
  * After a successful rename the clip's processing/ leftovers are deleted (frames dir, audio/transcript dir,
    wav, other per-clip temps — matched by the ORIGINAL basename, direct children of processing/ only; never
    anything outside processing/). The full transcript is already in the .json sidecar (transcript.segments);
    if it isn't, <new stem>.transcript.json/.txt are copied next to the video first. needs-review/failed clips
    keep their processing files. Disable with config "cleanup_processing_after_rename": false.
  * Batch folder (optional, --folder NAME / run_pipeline.py --folder NAME / web UI checkbox): a confident clip is
    renamed straight into inbox/<NAME>/ (one os.rename, same volume) with its sidecars; _t02, _t03 … on a clash
    inside the folder. needs-review / failed clips stay at inbox/ top level with their original names. The folder
    name is validated by sanitize_folder_name (no / \\ or '..', no leading dot, trimmed, max 80 chars) and created
    on the first rename. The log record's "new" is the final path and "folder" the folder name.
  * --move-into NAME: clips already renamed and still at inbox/ top level move into inbox/<NAME>/ with their
    .json/.md (and copied transcript) sidecars; one {"action": "moved", "move_of": <rename id>} record each.
    Files inside inbox/ subfolders are never (re)processed or renamed — only moved by these two paths.
  * Notes: every clip's full record lives in the central store notes/clips/<clip_id>.json (notes_store.py); its
    'applied' note and current_path are updated on rename / move / undo. .json/.md sidecars next to the clip are
    optional (config sidecar.write_next_to_clip) — when present they move with the clip exactly as before; nothing
    here needs them (skip rules, Apply, Move and undo read the store when there is no sidecar).
  * Every action is appended to logs/rename-log.jsonl; scripts/undo_renames.py reverses them (clips go back to
    inbox/ top level under their original names; a batch folder is removed only if it ends up empty).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import sys
import unicodedata
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import abs_path, atomic_write_text, load_config, section  # noqa: E402
from rename_dry_run import apply_allowed, plan_rename  # noqa: E402
import notes_store as ns  # noqa: E402

# Media types come from notes_store (same sets as run_pipeline; selftest checks). Photos are handled like clips.
VIDEO_EXTS = ns.VIDEO_EXTS
PHOTO_EXTS = ns.PHOTO_EXTS
MEDIA_EXTS = ns.MEDIA_EXTS
SIDECAR_EXTS = (".json", ".md")
TRANSCRIPT_SUFFIXES = (".transcript.json", ".transcript.txt")  # optional extra sidecars (copied transcripts)
# Per-clip entries in processing/, by ORIGINAL video stem (exact names only — a prefix match could hit another
# clip, e.g. CAM_X vs CAM_X_2). Directories: <stem>_frames, <stem>_audio. Files: wav / whisper output / temps.
PROCESSING_SUFFIXES = ("_frames", "_audio", ".wav", ".json", ".transcript.json", ".transcript.txt", ".tmp", "_tmp")
LOG_NAME = "rename-log.jsonl"
TOOL = "AI-Video-Renamer"
ACTIVE_ACTIONS = ("renamed", "needs-review", "moved")
FOLDER_MAX_LEN = 80  # characters; batch folder names (Finder shows them in full)
FOLDER_BAD = re.compile(r'[:*?"<>|]')  # ExFAT-illegal (slashes are refused outright, not replaced)
FOLDER_JUNK = (".DS_Store",)  # Finder litter that doesn't keep an otherwise empty batch folder alive


# ---------------------------------------------------------------- paths ----

def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def inbox_dir(cfg: dict) -> Path:
    return abs_path(cfg.get("inbox_dir") or "inbox")


def logs_dir(cfg: dict) -> Path:
    return abs_path(cfg.get("logs_dir") or "logs")


def dry_run_dir(cfg: dict) -> Path:
    return abs_path(section(cfg, "sidecar").get("dry_run_dir") or "logs/dry-run")


def processing_dir(cfg: dict) -> Path:
    return abs_path(cfg.get("processing_dir") or "processing")


def rename_log_path(cfg: dict) -> Path:
    return logs_dir(cfg) / LOG_NAME


def report_path(cfg: dict) -> Path:
    return dry_run_dir(cfg) / "report.jsonl"


def norm(p: str | os.PathLike) -> str:
    """Comparable key: resolved parent + name, NFC, lower-cased (ExFAT/APFS are case-insensitive)."""
    p = Path(p)
    try:
        parent = p.parent.resolve()
    except OSError:
        parent = p.parent
    return unicodedata.normalize("NFC", str(parent / p.name)).lower()


def is_hidden(p: str | os.PathLike) -> bool:
    return Path(p).name.startswith(".")  # dotfiles, ._ AppleDouble, .uploading-* temps


def is_video(p: str | os.PathLike) -> bool:
    """A file the pipeline handles: video OR photo (name kept for compatibility); never dotfiles."""
    p = Path(p)
    return p.suffix.lower() in MEDIA_EXTS and not is_hidden(p)


def in_inbox(p: str | os.PathLike, cfg: dict) -> bool:
    """True only for a file directly inside inbox/ (not a subfolder, not anywhere else)."""
    p = Path(p)
    try:
        return norm(p.parent / "_") == norm(inbox_dir(cfg) / "_")
    except OSError:
        return False


def protected(p: str | os.PathLike, cfg: dict) -> str | None:
    s = str(Path(p).resolve())
    for marker in section(cfg, "sidecar").get("protected_path_markers") or []:
        if marker and marker in s:
            return marker
    return None


def name_regex(cfg: dict) -> re.Pattern:
    """{YYYYMMDD}_{project}_{subject}_{clipType}[_t##].ext as produced by propose_name.py."""
    types = [re.escape(t) for t in dict.fromkeys(list(cfg.get("clip_types") or []) + ["photo"])] \
        if cfg.get("clip_types") else [r"[a-z0-9]+(?:-[a-z0-9]+)*"]
    seg = r"[a-z0-9]+(?:-[a-z0-9]+)*"
    return re.compile(rf"^\d{{8}}_{seg}_{seg}_(?:{'|'.join(types)})(?:_t\d{{2}})?\.[A-Za-z0-9]+$")


def matches_pattern(name: str, cfg: dict) -> bool:
    return bool(name_regex(cfg).match(name))


def inbox_videos(cfg: dict) -> list[Path]:
    d = inbox_dir(cfg)
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if p.is_file() and is_video(p))


# ------------------------------------------------------- batch folders ----

def sanitize_folder_name(name: object) -> str:
    """User-typed batch folder name -> safe single folder name inside inbox/ (raises ValueError with a readable reason).

    Trims and collapses whitespace, drops control characters, refuses / \\ '..' and a leading dot (hidden folder),
    replaces ExFAT-illegal : * ? " < > | with '-', strips trailing dots/spaces, max FOLDER_MAX_LEN characters.
    Spaces and normal punctuation (& , ' ( ) - _ ! #) are kept — Finder shows the name as typed."""
    s = unicodedata.normalize("NFC", str(name if name is not None else ""))
    s = re.sub(r"\s+", " ", s)
    s = "".join(ch for ch in s if ch >= " " and ch != "\x7f").strip()
    if not s:
        raise ValueError("folder name is empty")
    if "/" in s or "\\" in s:
        raise ValueError("folder name can't contain / or \\ (one folder, directly in inbox/)")
    if ".." in s:
        raise ValueError("folder name can't contain '..'")
    if s.startswith("."):
        raise ValueError("folder name can't start with a dot (it would be a hidden folder)")
    s = FOLDER_BAD.sub("-", s).rstrip(". ").strip()
    if not s:
        raise ValueError("folder name has no usable characters")
    if len(s) > FOLDER_MAX_LEN:
        raise ValueError(f"folder name is too long ({len(s)} characters, max {FOLDER_MAX_LEN})")
    return s


def folder_path(cfg: dict, name: object) -> Path:
    """inbox/<sanitized name> (may not exist yet). An existing folder whose name differs only in case is reused
    under its real name (ExFAT is case-insensitive). Raises ValueError if the name is taken by a file / symlink."""
    clean = sanitize_folder_name(name)
    inbox = inbox_dir(cfg)
    p = inbox / clean
    if p.exists():  # on ExFAT/APFS this also matches 'show 2026' vs 'Show 2026' -> use the real spelling
        with os.scandir(inbox) as it:
            for e in it:
                if unicodedata.normalize("NFC", e.name).lower() == clean.lower():
                    p = inbox / e.name
                    break
    if p.is_symlink():
        raise ValueError(f"inbox/{p.name} is a symlink — choose another folder name")
    if p.exists() and not p.is_dir():
        raise ValueError(f"inbox/{p.name} already exists and is a file — choose another folder name")
    return p


def folder_project_slug(name: object, cfg: dict) -> str:
    """Folder name -> {project} slug for filenames ('Vintage Collectibles Show' -> 'vintage-collectibles').
    Cut at naming.project_max_len on a whole word when possible. '' if nothing usable (e.g. only symbols)."""
    from _common import slugify  # local: keeps the import list above unchanged

    max_len = int(section(cfg, "naming").get("project_max_len") or 24)
    full = slugify(name, 0)
    if len(full) <= max_len:
        return full
    cut = full[:max_len].rstrip("-")
    if full[len(cut)] != "-" and "-" in cut:  # cut fell inside a word -> drop the partial word
        cut = cut.rsplit("-", 1)[0]
    return cut


def companion_paths(video: Path) -> list[Path]:
    """Existing sidecars that travel with a clip: <stem>.json/.md and copied <stem>.transcript.json/.txt."""
    out = []
    for ext in SIDECAR_EXTS + TRANSCRIPT_SUFFIXES:
        p = video.parent / f"{video.stem}{ext}"
        if p.is_file() and not p.is_symlink():
            out.append(p)
    return out


def remove_folder_if_empty(cfg: dict, folder: str | os.PathLike, notes: list[str] | None = None) -> bool:
    """Undo helper: remove a batch folder (a direct child of inbox/) only if nothing but Finder litter
    (.DS_Store, ._* AppleDouble) is left in it. Never touches anything else."""
    d = Path(folder)
    notes = notes if notes is not None else []
    try:
        if d.is_symlink() or not d.is_dir() or not in_inbox(d, cfg):
            return False
        names = os.listdir(d)
        if any(not (n in FOLDER_JUNK or n.startswith("._")) for n in names):
            return False
        for n in names:
            p = d / n
            if p.is_file() and not p.is_symlink():
                p.unlink()
        d.rmdir()
        ad = d.parent / f"._{d.name}"
        if ad.is_file():
            ad.unlink(missing_ok=True)
        notes.append(f"removed empty folder inbox/{d.name}/")
        return True
    except OSError as e:
        notes.append(f"folder inbox/{d.name}/ not removed: {e}")
        return False


# ------------------------------------------------------------ sidecars ----

def read_sidecar(path: Path) -> dict | None:
    """Parsed tool sidecar JSON (None if absent / not ours)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and (data.get("tool") == TOOL or "proposal" in data):
        return data
    return None


def sidecar_paths(video: Path) -> list[Path]:
    return [video.parent / f"{video.stem}{ext}" for ext in SIDECAR_EXTS]


def clip_record(video: Path, cfg: dict, index: dict | None = None) -> dict | None:
    """The clip's notes: its .json sidecar next to the video, else a LIVE record in the central store whose current
    path is this video (dry-run records don't count — a dry run never marks a clip as handled)."""
    rec = read_sidecar(video.parent / f"{video.stem}.json")
    if rec is not None:
        return rec
    sidx = (index or {}).get("store")
    rec = ns.find(cfg, video, sidx)
    if rec is not None and not rec.get("dry_run"):
        return rec
    return None


def is_sidecar_of_inbox_video(name: str, video_stems: set[str]) -> bool:
    low = name.lower()
    for suf in TRANSCRIPT_SUFFIXES:
        if low.endswith(suf):
            return low[: -len(suf)] in video_stems
    stem, ext = os.path.splitext(name)
    return ext.lower() in SIDECAR_EXTS and stem.lower() in video_stems


# ------------------------------------------------------ processing cleanup ----

def clip_processing_paths(cfg: dict, original_video: str | os.PathLike) -> list[Path]:
    """Existing processing/ entries that belong to this clip (by original stem), plus their ._ companions.
    Only direct children of processing/; symlinks are never included."""
    proc = processing_dir(cfg)
    stem = Path(original_video).stem
    if not stem or stem.startswith(".") or "/" in stem or not proc.is_dir():
        return []
    proc_r = proc.resolve()
    out: list[Path] = []
    for suf in PROCESSING_SUFFIXES:
        for name in (f"{stem}{suf}", f"._{stem}{suf}"):
            p = proc / name
            if not (p.exists() or p.is_symlink()) or p.is_symlink():
                continue
            try:
                if p.resolve().parent != proc_r:
                    continue
            except OSError:
                continue
            out.append(p)
    return out


def _tree_size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    total = 0
    for root, _dirs, files in os.walk(p):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def _transcript_captured(sidecar: dict | None, transcript_json: Path) -> bool:
    """Is the full transcript (every segment's text) already inside the .json sidecar?"""
    try:
        full = json.loads(transcript_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True  # nothing readable to preserve
    segs = full.get("segments") or []
    if not segs:
        return True  # no speech: nothing to keep
    side = ((sidecar or {}).get("transcript") or {}).get("segments")
    if not isinstance(side, list) or len(side) != len(segs):
        return False
    return [str(s.get("text", "")).strip() for s in side] == [str(s.get("text", "")).strip() for s in segs]


def cleanup_clip_processing(cfg: dict, original_video: str | os.PathLike, final_video: str | os.PathLike,
                            preview: bool = False) -> dict:
    """Remove a renamed clip's processing/ leftovers. Returns {removed, bytes, transcript_copies, skipped, errors}."""
    original_video, final_video = Path(original_video), Path(final_video)
    res: dict = {"removed": [], "bytes": 0, "transcript_copies": [], "errors": [], "skipped": None}
    proc = processing_dir(cfg)
    stem = original_video.stem
    # another video in inbox/ still uses this stem (e.g. CAM_X.MP4 + CAM_X.MOV) -> its frames dir is shared
    others = [v for v in inbox_videos(cfg) if v.stem.lower() == stem.lower() and norm(v) != norm(final_video)]
    if others:
        res["skipped"] = f"{others[0].name} in inbox/ shares the stem {stem} — processing files kept"
        return res
    paths = clip_processing_paths(cfg, original_video)
    if not paths:
        return res
    # keep the full transcript with the sidecars if the .json sidecar doesn't already hold it
    audio = proc / f"{stem}_audio"
    tj, tt = audio / f"{stem}.transcript.json", audio / f"{stem}.transcript.txt"
    side = read_sidecar(final_video.parent / f"{final_video.stem}.json")
    stored = ns.find(cfg, final_video)
    if tj.is_file() and stored is not None and side is None and not _transcript_captured(stored, tj) and not preview:
        # no sidecar next to the clip: keep the full transcript in the notes store instead of copying files beside it
        try:
            segs = json.loads(tj.read_text(encoding="utf-8")).get("segments") or []
            if ns.update_transcript(cfg, final_video, segs):
                res["transcript_to_store"] = stored.get("clip_id")
        except (OSError, ValueError):
            pass
        stored = ns.find(cfg, final_video)
    if tj.is_file() and not _transcript_captured(side or stored, tj):
        for src, suf in ((tj, ".transcript.json"), (tt, ".transcript.txt")):
            if not src.is_file():
                continue
            dst = final_video.parent / f"{final_video.stem}{suf}"
            item = {"from": str(src), "to": str(dst), "undo_name": f"{stem}{suf}"}
            if dst.exists():
                res["errors"].append(f"{dst.name} exists — transcript not copied; audio dir kept")
                paths = [p for p in paths if p.name not in (audio.name, f"._{audio.name}")]
                continue
            if not preview:
                try:
                    shutil.copy2(src, dst)
                except OSError as e:
                    res["errors"].append(f"copy {src.name}: {e} — audio dir kept")
                    paths = [p for p in paths if p.name not in (audio.name, f"._{audio.name}")]
                    continue
            res["transcript_copies"].append(item)
    proc_r = proc.resolve()
    for p in paths:
        try:
            if p.is_symlink() or p.resolve().parent != proc_r:  # belt and braces: never outside processing/
                res["errors"].append(f"not removing {p} (outside processing/ or symlink)")
                continue
            size = _tree_size(p)
            if not preview:
                if p.is_dir():
                    shutil.rmtree(p)
                else:
                    p.unlink()
            res["removed"].append(str(p))
            res["bytes"] += size
        except OSError as e:
            res["errors"].append(f"{p.name}: {e}")
    return res


def _render_md(json_path: Path, record: dict) -> None:
    md = json_path.with_suffix(".md")
    if md.exists():
        from sidecar import build_markdown  # local import: sidecar imports _common only

        atomic_write_text(md, build_markdown(record))


def annotate_sidecar(json_path: Path, applied: dict) -> bool:
    """Record the applied action inside the JSON sidecar (+ re-render .md). Never raises."""
    try:
        rec = read_sidecar(json_path)
        if rec is None:
            return False
        applied = dict(applied, previous_sidecar=rec.get("sidecar"))
        rec["applied"] = applied
        rec["sidecar"] = {"dir": str(json_path.parent), "location": "next_to_video",
                          "json": str(json_path), "md": str(json_path.with_suffix(".md"))}
        atomic_write_text(json_path, json.dumps(rec, indent=2, ensure_ascii=False) + "\n")
        _render_md(json_path, rec)
        return True
    except Exception:  # noqa: BLE001
        return False


def annotate_move(json_path: Path, video: Path, folder: str | None, moved_from: str | None = None,
                  log_id: str | None = None) -> bool:
    """Update the 'applied' note after a clip moved into (folder set) or back out of (folder None) a batch folder."""
    try:
        rec = read_sidecar(json_path)
        if rec is None or not isinstance(rec.get("applied"), dict):
            return False
        ap = rec["applied"]
        ap["new_path"] = str(video)
        if folder:
            ap.update(folder=folder, moved_from=moved_from, moved_log_id=log_id)
        else:
            for k in ("folder", "moved_from", "moved_log_id"):
                ap.pop(k, None)
        rec["sidecar"] = {"dir": str(json_path.parent), "location": "next_to_video",
                          "json": str(json_path), "md": str(json_path.with_suffix(".md"))}
        atomic_write_text(json_path, json.dumps(rec, indent=2, ensure_ascii=False) + "\n")
        _render_md(json_path, rec)
        return True
    except Exception:  # noqa: BLE001
        return False


def strip_annotation(json_path: Path) -> bool:
    """Undo helper: drop the 'applied' block, restore the previous sidecar location info, re-render .md."""
    try:
        rec = read_sidecar(json_path)
        if rec is None or "applied" not in rec:
            return False
        applied = rec.pop("applied") or {}
        if applied.get("previous_sidecar") is not None:
            rec["sidecar"] = applied["previous_sidecar"]
        atomic_write_text(json_path, json.dumps(rec, indent=2, ensure_ascii=False) + "\n")
        _render_md(json_path, rec)
        return True
    except Exception:  # noqa: BLE001
        return False


# ----------------------------------------------------------------- log ----

def read_log(cfg: dict) -> list[dict]:
    path = rename_log_path(cfg)
    out: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except FileNotFoundError:
        pass
    return out


def append_log(cfg: dict, rec: dict) -> None:
    path = rename_log_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def active_records(records: list[dict]) -> list[dict]:
    undone = {r.get("undo_of") for r in records if r.get("action") == "undo"}
    return [r for r in records if r.get("action") in ACTIVE_ACTIONS and r.get("id") not in undone]


def current_paths(act: list[dict]) -> dict[str, str]:
    """rename record id -> where that clip is now (its 'new' path, or the 'new' of the latest active 'moved' record)."""
    cur = {r.get("id"): r["new"] for r in act if r["action"] == "renamed" and r.get("new")}
    for r in act:  # log order: later moves win
        if r["action"] == "moved" and r.get("move_of") in cur and r.get("new"):
            cur[r["move_of"]] = r["new"]
    return cur


def current_path(index: dict, rec: dict) -> str | None:
    """Current location of a renamed clip (follows 'moved' records)."""
    return index.get("current", {}).get(rec.get("id")) or rec.get("new")


def log_index(cfg: dict, records: list[dict] | None = None) -> dict:
    recs = read_log(cfg) if records is None else records
    act = active_records(recs)
    cur = current_paths(act)
    return {
        "records": recs,
        "active": act,
        "current": cur,
        "renamed_new": {norm(cur.get(r.get("id")) or r["new"]): r for r in act if r["action"] == "renamed" and r.get("new")},
        "renamed_orig": {norm(r["original"]): r for r in act if r["action"] == "renamed" and r.get("original")},
        "review": {norm(r["original"]): r for r in act if r["action"] == "needs-review" and r.get("original")},
        "store": ns.build_index(cfg),
    }


# ------------------------------------------------------------ skip rules ----

def handled_reason(video: Path, cfg: dict, index: dict | None = None) -> tuple[str, str] | None:
    """Why run_pipeline should skip this clip: (code, text) or None if it still needs processing.

    codes: renamed (rename-log) · needs-review (rename-log) · pattern (already has a generated name) ·
           sidecar (a tool .json sidecar sits next to it, or the notes store has a live record at this path)."""
    index = index if index is not None else log_index(cfg)
    k = norm(video)
    if k in index["renamed_new"]:
        r = index["renamed_new"][k]
        return "renamed", f"renamed from {Path(r['original']).name} ({r.get('time')})"
    if k in index["review"]:
        return "needs-review", f"marked needs-review ({index['review'][k].get('time')})"
    if matches_pattern(video.name, cfg):
        return "pattern", "already has a generated name"
    if read_sidecar(video.parent / f"{video.stem}.json") is not None:
        return "sidecar", f"has sidecar {video.stem}.json"
    if clip_record(video, cfg, index) is not None:
        return "sidecar", "processed (notes store)"
    return None


def filter_unhandled(videos: list[Path], cfg: dict, force: bool = False) -> tuple[list[Path], list[tuple[Path, str]]]:
    """(to_process, skipped[(path, reason)]). force=True processes everything."""
    if force:
        return list(videos), []
    index = log_index(cfg)
    todo, skipped = [], []
    for v in videos:
        why = handled_reason(v, cfg, index)
        if why:
            skipped.append((v, why[1]))
        else:
            todo.append(v)
    return todo, skipped


def latest_report(cfg: dict) -> dict[str, dict]:
    """norm(source) -> latest report.jsonl entry (insertion order = order of latest appearance)."""
    out: dict[str, dict] = {}
    try:
        with open(report_path(cfg), encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(e, dict) or not e.get("source"):
                    continue
                k = norm(e["source"])
                out.pop(k, None)
                out[k] = e
    except FileNotFoundError:
        pass
    return out


def video_state(video: Path, cfg: dict, index: dict, report: dict) -> dict:
    """UI state of one inbox video: pending | renamed | needs review."""
    why = handled_reason(video, cfg, index)
    k = norm(video)
    if why and why[0] in ("renamed", "pattern"):
        r = index["renamed_new"].get(k)
        return {"state": "renamed", "original": Path(r["original"]).name if r else None,
                "confidence": r.get("confidence") if r else None, "detail": why[1]}
    if why and why[0] == "needs-review":
        r = index["review"][k]
        return {"state": "needs review", "detail": r.get("reason"), "confidence": r.get("confidence")}
    if why and why[0] == "sidecar":
        sc = clip_record(video, cfg, index) or {}
        prop = sc.get("proposal") or {}
        conf = ((sc.get("describe") or {}).get("description") or {}).get("confidence")
        if prop.get("needs_review") or not prop.get("new_name"):
            return {"state": "needs review", "detail": "; ".join(prop.get("review_reasons") or []) or "no proposal",
                    "confidence": conf}
        return {"state": "pending", "proposed": prop.get("new_name"), "confidence": conf,
                "detail": "processed, rename not applied yet"}
    e = report.get(k)
    if e:
        return {"state": "pending", "proposed": e.get("proposed"), "confidence": e.get("confidence"),
                "proposed_needs_review": bool(e.get("needs_review")),
                "detail": "processed (dry run) — not applied yet"}
    return {"state": "pending", "detail": "not processed yet"}


# --------------------------------------------------------------- moves ----

def _same(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _companion(p: Path) -> Path:
    return p.parent / f"._{p.name}"


def move_file(src: Path, dst: Path, notes: list[str]) -> dict:
    """os.rename src -> dst; never overwrites; verifies size; carries the ._ companion if still behind."""
    src, dst = Path(src), Path(dst)
    if dst.exists() and not _same(src, dst):
        raise FileExistsError(f"refusing to overwrite {dst}")
    size = src.stat().st_size
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.rename(src, dst)
    got = dst.stat().st_size
    info = {"from": str(src), "to": str(dst), "size": size}
    if got != size:  # cannot happen for a same-volume rename; recorded loudly, never silently
        info["size_mismatch"] = [size, got]
        notes.append(f"WARNING size mismatch after rename: {src.name} {size} B -> {dst.name} {got} B")
    try:
        ad_src, ad_dst = _companion(src), _companion(dst)
        if ad_src.exists() and not ad_dst.exists():
            os.rename(ad_src, ad_dst)
            info["appledouble"] = True
    except OSError as e:
        notes.append(f"._ companion of {src.name} left in place ({e})")
    return info


def pick_target(directory: Path, name: str, src: Path, take_max: int = 99) -> tuple[Path | None, str | None]:
    """directory/name, or the first free _tNN take (starting at _t02, or after an existing _tNN)."""
    p = Path(name)
    stem, ext = p.stem, p.suffix

    def free(s: str) -> bool:
        for cand in (directory / f"{s}{ext}", directory / f"{s}.json", directory / f"{s}.md"):
            if cand.exists() and not _same(cand, src):
                return False
        return True

    if free(stem):
        return directory / name, None
    m = re.match(r"^(.*)_t(\d{2})$", stem)
    base, start = (m.group(1), max(2, int(m.group(2)) + 1)) if m else (stem, 2)
    for n in range(start, take_max + 1):
        s = f"{base}_t{n:02d}"
        if free(s):
            return directory / f"{s}{ext}", f"_t{n:02d}"
    return None, None


def new_batch_id(origin: str) -> str:
    return f"{datetime.now().strftime('%Y%m%dT%H%M%S')}-{origin}-{uuid.uuid4().hex[:4]}"


def apply_clip(
    video: str | os.PathLike,
    proposed: str | None,
    cfg: dict,
    *,
    needs_review: bool = False,
    reasons: list[str] | tuple = (),
    confidence: float | None = None,
    failed: bool = False,
    sidecars: list | tuple = (),
    preview: bool = False,
    batch: str | None = None,
    origin: str = "apply_renames",
    folder: str | None = None,
    in_place: bool = False,
    allow_protected: bool = False,
) -> dict:
    """Apply ONE clip's result in place (or, with folder, rename it straight into inbox/<folder>/). Returns a result
    dict; action is one of renamed | needs-review | would-rename | would-mark-review | skipped | refused | error.
    needs-review / failed clips always stay at inbox/ top level under their original name.
    in_place (ClipGauge "Process in place", v0.6): the clip may live outside inbox/ — it is renamed in its own folder
    if inplace.classify allows it (held / Resolve-internal / Cloud-synced / cloud-drive / project folders refused;
    under DaVinci Resolve or a protected marker only with allow_protected, after the user confirmed). No folder."""
    video = Path(video)
    notes: list[str] = []
    res = {"original": str(video), "new": None, "proposed": proposed, "confidence": confidence,
           "sidecars": [], "notes": notes, "origin": origin}
    if not preview:
        ok, why = apply_allowed(cfg, True)
        if not ok:
            return {**res, "action": "refused", "reason": why}
    if is_hidden(video):
        return {**res, "action": "skipped", "reason": "hidden / ._ file ignored"}
    if in_place and not in_inbox(video, cfg):
        if folder:
            return {**res, "action": "refused", "reason": "a batch folder can't be used when processing in place"}
        import inplace
        where = inplace.classify(video, cfg)
        if where["status"] == "refused":
            return {**res, "action": "refused", "reason": f"in place: {where['reason']}"}
        if where["status"] == "needs_confirm" and not allow_protected:
            return {**res, "action": "refused", "reason": "in place: under DaVinci Resolve — not confirmed (renaming "
                                                          "imported media breaks Resolve links)"}
    else:
        if not in_inbox(video, cfg):
            return {**res, "action": "refused", "reason": "outside inbox/ — only clips directly in inbox/ are renamed"}
        mk = protected(video, cfg)
        if mk:
            return {**res, "action": "refused", "reason": f"protected path ({mk.strip('/')})"}
    if not video.is_file():
        return {**res, "action": "skipped", "reason": "source file no longer exists"}

    inbox = video.parent
    dest_dir = inbox
    rs = [str(r) for r in (reasons or [])]
    review = bool(needs_review) or bool(failed) or not proposed
    if failed and not rs:
        rs.append("processing failed")
    if not proposed and not rs:
        rs.append("no proposed name")
    suffix = None
    dst = video
    if not review:
        pname = str(proposed)
        if Path(pname).name != pname or pname.startswith(".") or not pname.strip():
            review = True
            rs.append(f"invalid proposed name {pname!r}")
        else:
            take_max = int(section(cfg, "naming").get("take_suffix_max") or 99)
            if folder:
                try:
                    dest_dir = folder_path(cfg, folder)
                except ValueError as e:
                    return {**res, "action": "refused", "reason": f"batch folder: {e}"}
            target, suffix = pick_target(dest_dir, plan_rename(video, pname, dest_dir).name, video, take_max)
            if target is None:
                review = True
                rs.append(f"no free take number for {pname}")
            else:
                dst = target
    action = "needs-review" if review else "renamed"
    into = dst.parent.name if action == "renamed" and norm(dst.parent / "_") != norm(inbox / "_") else None
    if review:
        reason = "needs review: " + "; ".join(rs)
    else:
        reason = f"confidence {confidence} — " + (f"proposed name applied, moved into {into}/" if into
                                                  else "proposed name applied in place")
        if suffix:
            reason += f"; {proposed} already existed, used take suffix {suffix}"
            notes.append(f"collision: {proposed} exists -> {dst.name}")

    # sidecars -> next to the (renamed) video, named to match
    plan: list[tuple[Path, Path]] = []
    seen: set[str] = set()
    for s in sidecars or ():
        if not s:
            continue
        s = Path(s)
        if s.suffix.lower() not in SIDECAR_EXTS or is_hidden(s) or s.suffix.lower() in seen or not s.is_file():
            continue
        seen.add(s.suffix.lower())
        t = dst.parent / f"{dst.stem}{s.suffix}"
        if norm(s) == norm(t):
            continue  # already in place (needs-review clip with live sidecars)
        if t.exists():
            notes.append(f"sidecar target {t.name} exists — {s.name} left where it is")
            continue
        plan.append((s, t))

    res.update(new=str(dst), reason=reason, collision=suffix, folder=into)
    if preview:
        res["action"] = "would-rename" if action == "renamed" else "would-mark-review"
        res["sidecars"] = [{"from": str(s), "to": str(t)} for s, t in plan]
        return res

    rec = {
        "id": uuid.uuid4().hex[:12],
        "batch": batch or new_batch_id(origin),
        "time": now_iso(),
        "action": action,
        "original": str(video),
        "new": str(dst),
        "proposed": proposed,
        "confidence": confidence,
        "reason": reason,
        "collision": suffix,
        "origin": origin,
    }
    if into:
        rec["folder"] = into
    if in_place and not in_inbox(video, cfg):
        rec["in_place"] = True
    try:
        if action == "renamed":
            if into:
                rec["folder_created"] = not dst.parent.is_dir()
                dst.parent.mkdir(exist_ok=True)
            mv = move_file(video, dst, notes)
            rec["size"] = mv["size"]
            rec["appledouble"] = bool(mv.get("appledouble"))
            if mv.get("size_mismatch"):
                rec["size_mismatch"] = mv["size_mismatch"]
        else:
            rec["size"] = video.stat().st_size
    except OSError as e:
        rec.update(action="error", reason=f"{type(e).__name__}: {e}", new=None)
        append_log(cfg, rec)
        return {**res, **rec, "notes": notes}
    moves = []
    for s, t in plan:
        try:
            moves.append(move_file(s, t, notes))
        except OSError as e:
            notes.append(f"sidecar {s.name} not moved: {e}")
    rec["sidecars"] = moves
    js = dst.parent / f"{dst.stem}.json"
    note = {"action": action, "original_path": str(video), "new_path": str(dst), "time": rec["time"],
            "log_id": rec["id"], "reason": reason}
    if into:
        note["folder"] = into
    rec["annotated"] = [str(js)] if annotate_sidecar(js, note) else []
    first_json = next((str(m["to"]) for m in moves if str(m["to"]).lower().endswith(".json")), None) or next(
        (str(s) for s in (sidecars or ()) if s and str(s).lower().endswith(".json") and Path(s).is_file()), None)
    sid = ns.annotate_applied(cfg, video, note, dst, fallback_json=js if js.is_file() else first_json)
    if sid:
        rec["note_id"] = sid
    if action == "renamed" and cfg.get("cleanup_processing_after_rename", True):
        try:
            rec["cleanup"] = cleanup_clip_processing(cfg, video, dst)
            c = rec["cleanup"]
            if c["removed"] or c["transcript_copies"]:
                notes.append(f"processing cleanup: removed {len(c['removed'])} item(s), {c['bytes'] / 1e6:.1f} MB"
                             + (f"; transcript copied next to the video" if c["transcript_copies"] else ""))
            for e in c["errors"] + ([c["skipped"]] if c.get("skipped") else []):
                notes.append(f"processing cleanup: {e}")
        except Exception as e:  # noqa: BLE001  cleanup must never undo/abort a finished rename
            rec["cleanup"] = {"removed": [], "bytes": 0, "errors": [f"{type(e).__name__}: {e}"]}
    rec["notes"] = notes
    append_log(cfg, rec)
    return {**res, **rec}


# --------------------------------------------------------------- batch ----

def _find_sidecars(entry: dict, src: Path, cfg: dict) -> list[Path]:
    sj = entry.get("sidecar_json")
    cands: list[Path] = []
    if sj:
        cands.append(Path(sj))
    cands.append(dry_run_dir(cfg) / (src.parent.name or "_root") / f"{src.stem}.json")
    cands.append(src.parent / f"{src.stem}.json")
    for c in cands:
        if c.is_file():
            return [c, c.with_suffix(".md")]
    return []


def collect_candidates(cfg: dict, index: dict | None = None) -> list[dict]:
    """Report entries (latest per source) + unapplied next-to-video sidecars, each as kwargs for apply_clip
    (or {'skip': reason})."""
    index = index if index is not None else log_index(cfg)
    out: list[dict] = []
    seen: set[str] = set()
    for k, e in latest_report(cfg).items():
        src = Path(e["source"])
        base = {"video": src, "proposed": e.get("proposed"), "confidence": e.get("confidence")}
        if is_hidden(src):
            continue
        if not in_inbox(src, cfg):
            out.append({**base, "skip": "outside inbox/ (left untouched)"})
            continue
        seen.add(k)
        if not src.exists():
            out.append({**base, "skip": "source no longer exists (already renamed?)"})
            continue
        why = handled_reason(src, cfg, index)
        if why and why[0] != "sidecar":
            out.append({**base, "skip": f"already handled: {why[1]}"})
            continue
        out.append({**base, "needs_review": bool(e.get("needs_review")), "reasons": e.get("review_reasons") or [],
                    "failed": bool(e.get("error")) or not e.get("proposed"), "sidecars": _find_sidecars(e, src, cfg)})
    for v in inbox_videos(cfg):
        k = norm(v)
        if k in seen:
            continue
        why = handled_reason(v, cfg, index)
        if not why or why[0] != "sidecar":
            continue
        sj = v.parent / f"{v.stem}.json"
        sc = clip_record(v, cfg, index) or {}
        prop = sc.get("proposal") or {}
        out.append({"video": v, "proposed": prop.get("new_name"),
                    "confidence": ((sc.get("describe") or {}).get("description") or {}).get("confidence"),
                    "needs_review": bool(prop.get("needs_review")), "reasons": prop.get("review_reasons") or [],
                    "failed": not prop.get("new_name"),
                    "sidecars": [sj, sj.with_suffix(".md")] if sj.is_file() else []})
    return out


def apply_report(cfg: dict, preview: bool = False, origin: str = "apply_renames", stop=None,
                 folder: str | None = None) -> dict:
    """Apply every processed-but-unapplied clip. Raises PermissionError when config dry_run is true (unless preview).
    folder: confident clips are renamed into inbox/<folder>/ (ValueError if the name is unusable)."""
    if not preview:
        ok, why = apply_allowed(cfg, True)
        if not ok:
            raise PermissionError(why)
    if folder:
        folder = folder_path(cfg, folder).name
    batch = new_batch_id(origin)
    index = log_index(cfg)
    results = []
    for c in collect_candidates(cfg, index):
        if stop and stop():
            results.append({"original": str(c["video"]), "action": "skipped", "reason": "stopped (signal)"})
            continue
        if "skip" in c:
            results.append({"original": str(c["video"]), "proposed": c.get("proposed"), "action": "skipped", "reason": c["skip"]})
            continue
        kw = {k: v for k, v in c.items() if k != "video"}
        results.append(apply_clip(c["video"], cfg=cfg, preview=preview, batch=batch, origin=origin, folder=folder, **kw))
    counts = Counter(r["action"] for r in results)
    return {"batch": batch, "preview": preview, "counts": dict(counts), "results": results,
            "log": str(rename_log_path(cfg)), "folder": folder}


# ------------------------------------------------- move existing into folder ----

def movable_records(cfg: dict, index: dict | None = None) -> list[tuple[dict, Path]]:
    """(rename record, current path) for clips already renamed that still sit at inbox/ top level (per the log)."""
    index = index if index is not None else log_index(cfg)
    out = []
    for r in index["active"]:
        if r["action"] != "renamed":
            continue
        cur = Path(current_path(index, r) or "")
        if cur.name and in_inbox(cur, cfg) and not is_hidden(cur) and cur.is_file():
            out.append((r, cur))
    return out


def move_clip_into_folder(cfg: dict, rec: dict, cur: Path, fdir: Path, preview: bool = False,
                          batch: str | None = None, origin: str = "apply_renames") -> dict:
    """Move one renamed clip (+ sidecars) from inbox/ top level into fdir. Logs a 'moved' record."""
    notes: list[str] = []
    res = {"original": rec.get("original"), "from": str(cur), "new": None, "folder": fdir.name, "notes": notes,
           "origin": origin, "sidecars": []}
    take_max = int(section(cfg, "naming").get("take_suffix_max") or 99)
    target, suffix = pick_target(fdir, cur.name, cur, take_max)
    if target is None:
        return {**res, "action": "error", "reason": f"no free take number for {cur.name} in {fdir.name}/"}
    plan = []
    for s in companion_paths(cur):
        t = fdir / f"{target.stem}{s.name[len(cur.stem):]}"
        if t.exists():
            notes.append(f"sidecar target {fdir.name}/{t.name} exists — {s.name} left where it is")
            continue
        plan.append((s, t))
    reason = f"moved into {fdir.name}/" + (f"; {cur.name} already existed there, used take suffix {suffix}" if suffix else "")
    res.update(new=str(target), collision=suffix, reason=reason)
    if preview:
        return {**res, "action": "would-move", "sidecars": [{"from": str(s), "to": str(t)} for s, t in plan]}
    log_rec = {"id": uuid.uuid4().hex[:12], "batch": batch or new_batch_id(origin), "time": now_iso(), "action": "moved",
               "move_of": rec.get("id"), "original": rec.get("original"), "from": str(cur), "new": str(target),
               "folder": fdir.name, "collision": suffix, "reason": reason, "origin": origin}
    try:
        log_rec["folder_created"] = not fdir.is_dir()
        fdir.mkdir(exist_ok=True)
        mv = move_file(cur, target, notes)
        log_rec["size"] = mv["size"]
        log_rec["appledouble"] = bool(mv.get("appledouble"))
    except OSError as e:
        notes.append(f"{type(e).__name__}: {e}")
        return {**res, "action": "error", "reason": f"{type(e).__name__}: {e}"}
    moves = []
    for s, t in plan:
        try:
            moves.append(move_file(s, t, notes))
        except OSError as e:
            notes.append(f"sidecar {s.name} not moved: {e}")
    log_rec["sidecars"] = moves
    js = target.parent / f"{target.stem}.json"
    log_rec["annotated"] = [str(js)] if annotate_move(js, target, fdir.name, str(cur), log_rec["id"]) else []
    sid = ns.annotate_move(cfg, cur, target, fdir.name, str(cur), log_rec["id"])
    if sid:
        log_rec["note_id"] = sid
    log_rec["notes"] = notes
    append_log(cfg, log_rec)
    return {**res, **log_rec, "notes": notes}


def move_renamed_into_folder(cfg: dict, folder: str, preview: bool = False, origin: str = "apply_renames",
                             stop=None) -> dict:
    """Move every renamed clip still at inbox/ top level (per the rename log) into inbox/<folder>/.
    Raises PermissionError when config dry_run is true (unless preview), ValueError for an unusable folder name."""
    if not preview:
        ok, why = apply_allowed(cfg, True)
        if not ok:
            raise PermissionError(why)
    fdir = folder_path(cfg, folder)
    batch = new_batch_id(origin)
    results = []
    for rec, cur in movable_records(cfg):
        if stop and stop():
            results.append({"original": rec.get("original"), "from": str(cur), "action": "skipped", "reason": "stopped (signal)"})
            continue
        results.append(move_clip_into_folder(cfg, rec, cur, fdir, preview=preview, batch=batch, origin=origin))
    counts = Counter(r["action"] for r in results)
    return {"batch": batch, "preview": preview, "counts": dict(counts), "results": results, "folder": fdir.name,
            "log": str(rename_log_path(cfg))}


def move_summary_line(summary: dict) -> str:
    c, f = summary["counts"], summary.get("folder")
    if summary["preview"]:
        return f"Preview: {c.get('would-move', 0)} renamed clip(s) would move into inbox/{f}/ — nothing changed."
    if not summary["results"]:
        return f"Nothing to move: no renamed clips at inbox/ top level (folder inbox/{f}/ not created)."
    return (f"Moved {c.get('moved', 0)} renamed clip(s) (with sidecars) into inbox/{f}/, {c.get('error', 0)} errors. "
            f"Undo: python3 scripts/undo_renames.py --dry-run. Log: {summary['log']}")


def run_move_into_folder(cfg: dict, folder: str, preview: bool = False, origin: str = "apply_renames",
                         stop=None) -> tuple[int, dict]:
    """Locked wrapper used by the CLI / web UI: (exit code, summary or {'error'}). 3 = a pipeline run is active."""
    from pipeline_lock import acquire_lock, lock_path, release_lock  # noqa: E402

    lock = None
    if not preview:
        ok, why = apply_allowed(cfg, True)
        if not ok:
            return 2, {"error": f"Refusing to move: {why}. Use --dry-run to preview."}
        lock = lock_path(cfg)
        ok, holder = acquire_lock(lock)
        if not ok:
            return 3, {"error": f"A pipeline run is active (pid {holder.get('pid')}, from {holder.get('launched_by')}) — "
                                "not moving clips while it runs."}
    try:
        summary = move_renamed_into_folder(cfg, folder, preview=preview, origin=origin, stop=stop)
    except (ValueError, PermissionError) as e:
        return 2, {"error": f"Folder name: {e}" if isinstance(e, ValueError) else str(e)}
    finally:
        if lock:
            release_lock(lock)
    return (1 if summary["counts"].get("error") else 0), summary


def summary_line(summary: dict) -> str:
    c = summary["counts"]
    where = f"into inbox/{summary['folder']}/" if summary.get("folder") else "in place"
    if summary["preview"]:
        return (f"Preview: {c.get('would-rename', 0)} would be renamed {where}, {c.get('would-mark-review', 0)} would stay "
                f"as needs-review, {c.get('skipped', 0)} skipped, {c.get('refused', 0)} refused — nothing changed.")
    return (f"Applied: {c.get('renamed', 0)} renamed {where}, {c.get('needs-review', 0)} marked needs-review (name kept), "
            f"{c.get('skipped', 0)} skipped, {c.get('refused', 0)} refused, {c.get('error', 0)} errors. Log: {summary['log']}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Rename processed clips in place in inbox/ (needs config dry_run false)")
    ap.add_argument("--dry-run", action="store_true", help="Preview only; change nothing")
    ap.add_argument("--json", action="store_true", help="Print the summary as JSON")
    ap.add_argument("-v", "--verbose", action="store_true", help="Also list skipped entries")
    ap.add_argument("--folder", default=None, help="Rename confident clips into inbox/FOLDER/ (created if needed)")
    ap.add_argument("--move-into", default=None, metavar="FOLDER",
                    help="Instead of applying: move clips already renamed (inbox/ top level) into inbox/FOLDER/")
    args = ap.parse_args()
    cfg = load_config()
    from pipeline_lock import acquire_lock, lock_path, release_lock  # noqa: E402

    if args.move_into is not None:
        return move_main(cfg, args)
    if args.folder is not None:
        try:
            args.folder = folder_path(cfg, args.folder).name
        except ValueError as e:
            msg = f"Folder name: {e}"
            print(json.dumps({"error": msg}) if args.json else msg, file=sys.stdout if args.json else sys.stderr)
            return 2

    lock = None
    if not args.dry_run:
        ok, why = apply_allowed(cfg, True)
        if not ok:
            msg = f"Refusing to rename: {why}. Use --dry-run to preview."
            print(json.dumps({"error": msg}) if args.json else msg, file=sys.stdout if args.json else sys.stderr)
            return 2
        lock = lock_path(cfg)
        ok, holder = acquire_lock(lock)
        if not ok:
            msg = (f"A pipeline run is active (pid {holder.get('pid')}, from {holder.get('launched_by')}) — "
                   "not renaming while it runs.")
            print(json.dumps({"error": msg}) if args.json else msg, file=sys.stdout if args.json else sys.stderr)
            return 3
    stop = {"flag": False}

    def _sig(signum, frame):  # noqa: ARG001  finish the current clip, then stop cleanly
        stop["flag"] = True

    for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(s, _sig)
    try:
        summary = apply_report(cfg, preview=args.dry_run,
                               origin=os.environ.get("AI_VIDEO_RENAMER_LAUNCHED_BY", "apply_renames"),
                               stop=lambda: stop["flag"], folder=args.folder)
    finally:
        if lock:
            release_lock(lock)
    if args.json:
        print(json.dumps({**summary, "message": summary_line(summary)}, ensure_ascii=False, default=str))
        return 0
    for r in summary["results"]:
        a = r["action"]
        name = Path(r["original"]).name
        if a in ("renamed", "would-rename"):
            extra = f"  [{r['collision']} take suffix]" if r.get("collision") else ""
            print(f"{'RENAMED ' if a == 'renamed' else 'WOULD   '} {name} -> "
                  f"{(r['folder'] + '/') if r.get('folder') else ''}{Path(r['new']).name}  "
                  f"(conf {r.get('confidence')}, {len(r.get('sidecars') or [])} sidecar(s)){extra}")
        elif a in ("needs-review", "would-mark-review"):
            print(f"REVIEW   {name} (name kept) — {r.get('reason')}")
        elif a in ("error", "refused"):
            print(f"{a.upper():8} {name} — {r.get('reason')}")
        elif args.verbose:
            print(f"SKIP     {name} — {r.get('reason')}")
        for n in r.get("notes") or []:
            print(f"         note: {n}")
    print(summary_line(summary))
    return 1 if summary["counts"].get("error") else 0


def move_main(cfg: dict, args: argparse.Namespace) -> int:
    """--move-into FOLDER: move already-renamed clips from inbox/ top level into inbox/FOLDER/."""
    stop = {"flag": False}

    def _sig(signum, frame):  # noqa: ARG001
        stop["flag"] = True

    for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(s, _sig)
    code, summary = run_move_into_folder(cfg, args.move_into, preview=args.dry_run,
                                         origin=os.environ.get("AI_VIDEO_RENAMER_LAUNCHED_BY", "apply_renames"),
                                         stop=lambda: stop["flag"])
    if "error" in summary and "results" not in summary:
        print(json.dumps(summary) if args.json else summary["error"], file=sys.stdout if args.json else sys.stderr)
        return code
    if args.json:
        print(json.dumps({**summary, "message": move_summary_line(summary)}, ensure_ascii=False, default=str))
        return code
    for r in summary["results"]:
        a = r["action"]
        name = Path(r.get("from") or r.get("original") or "").name
        if a in ("moved", "would-move"):
            extra = f"  [{r['collision']} take suffix]" if r.get("collision") else ""
            print(f"{'MOVED  ' if a == 'moved' else 'WOULD  '} {name} -> {summary['folder']}/{Path(r['new']).name}"
                  f"  ({len(r.get('sidecars') or [])} sidecar(s)){extra}")
        else:
            print(f"{a.upper():8} {name} — {r.get('reason')}")
        for n in r.get("notes") or []:
            print(f"         note: {n}")
    print(move_summary_line(summary))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
