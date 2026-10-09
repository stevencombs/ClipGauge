#!/usr/bin/env python3
"""
AI Video Renamer pipeline (dry-run safe):
  1) Resolve guard
  2) Pick one inbox file (or --video; --all for the whole inbox)
  3) Extract 9 frames            -> processing/<stem>_frames/
  4) Transcribe (optional)       -> processing/<stem>_audio/   (whisper.cpp; skips if not installed)
  5) Describe with local VLM     -> strict JSON via Ollama HTTP API (qwen2.5vl:7b on Air)
  6) Propose filename            -> {YYYYMMDD}_{project}_{subject}_{clipType}[_t##].ext
  7) Notes + report              -> ALWAYS the central notes store notes/clips/<clip_id>.json (notes_store.py);
                                    .json/.md sidecars only in dry run (logs/dry-run/<folder>/) or, live, next to the
                                    video when config sidecar.write_next_to_clip is true (default false)
                                    + logs/dry-run/report.jsonl
  8) Live apply (dry_run false)  -> right after the sidecars: rename IN PLACE in inbox/ via apply_renames.apply_clip
                                    (confident -> proposed name + matching sidecars; needs-review -> name kept), logged
                                    to logs/rename-log.jsonl (undo: undo_renames.py)
     --folder NAME                -> confident clips are renamed straight into inbox/NAME/ (with sidecars; _t02… on a
                                    clash there); needs-review/failed clips stay at inbox/ top level, name kept
     --project-from-folder        -> {project} in every new filename = slug of NAME instead of the model's guess
  Photos (jpg/jpeg/png/heic/heif/dng/webp/tif, any case) in inbox/ go through the same run: date from EXIF
  DateTimeOriginal (else file mtime), ONE resized JPEG (photos.prepare_photo; sips for HEIC/HEIF/DNG, original
  untouched) -> VLM with a photo prompt, no Whisper; same naming/review/apply/notes (media_kind "photo").
  --dry-run forces a dry run for this invocation (nothing renamed, notes preview in notes/dry-run/) even when
  config dry_run is false; --video may be repeated.
  status.json is updated at every step (step, frames n/9, ETA).
  Only one run at a time: logs/pipeline.lock (pipeline_lock.py) — exit code 3 if another run holds it.
  Inbox runs skip clips already handled (rename-log, generated-name pattern, a sidecar next to the video, or a live
  notes-store record at that path);
  --force reprocesses them. Only files DIRECTLY in inbox/ are processed — never anything inside subfolders
  (batch folders included).
With config dry_run true nothing is renamed or moved (unchanged dry-run behaviour).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import ROOT, abs_path, load_config, section, tool_path  # noqa: E402
from check_resolve import resolve_running  # noqa: E402
from describe_clip import available_models, choose_model, describe, find_frames  # noqa: E402
from photos import is_photo, photo_date, photo_info, prepare_photo  # noqa: E402
from detect_ram import current_tier  # noqa: E402
from extract_frames import extract_frames  # noqa: E402
from media import clip_date, probe_media  # noqa: E402
from pipeline_lock import acquire_lock, active_updates, lock_path, release_lock  # noqa: E402
from propose_name import propose_filename, taken_names  # noqa: E402
from apply_renames import apply_clip, filter_unhandled, folder_path, folder_project_slug, new_batch_id  # noqa: E402
from rename_dry_run import apply_allowed, plan_rename  # noqa: E402
from sidecar import write_sidecars  # noqa: E402
import instructions as ins  # noqa: E402
import notes_store as ns  # noqa: E402
from status import write_status  # noqa: E402
from transcribe import transcribe  # noqa: E402


VIDEO_EXTS = ns.VIDEO_EXTS  # compared case-insensitively
PHOTO_EXTS = ns.PHOTO_EXTS
MEDIA_EXTS = ns.MEDIA_EXTS  # what the pipeline picks up from inbox/
STEPS = ("frames", "transcribe", "describe", "sidecar")


def pick_inbox(inbox: Path) -> Path | None:
    files = inbox_videos(inbox)
    return files[0] if files else None


def select_inbox_videos(cfg: dict, force: bool = False) -> tuple[list[Path], list[tuple[Path, str]]]:
    """Inbox videos still to process + (skipped, reason) for ones already handled (unless force)."""
    inbox = abs_path(cfg.get("inbox_dir") or "inbox")
    return filter_unhandled(inbox_videos(inbox), cfg, force=force)


def is_inbox_video(p: Path) -> bool:
    """Video or photo by extension (any case); skips dotfiles: ._ AppleDouble and web-UI .uploading-* temps."""
    return p.is_file() and p.suffix.lower() in MEDIA_EXTS and not p.name.startswith(".")


def inbox_videos(inbox: Path) -> list[Path]:
    return sorted(
        p for p in inbox.iterdir() if is_inbox_video(p)
    )


def ollama_available() -> tuple[bool, str]:
    exe = shutil.which("ollama") or ("/opt/homebrew/bin/ollama" if Path("/opt/homebrew/bin/ollama").exists() else None)
    if not exe:
        return False, "ollama CLI not found on PATH"
    try:
        subprocess.check_output([exe, "--version"], stderr=subprocess.STDOUT, text=True)
    except Exception as e:  # noqa: BLE001
        return False, f"ollama not runnable: {e}"
    return True, exe


# ------------------------------------------------------------------ ETA ----

class EtaModel:
    """Rolling per-step timing averages persisted in logs/timings.json."""

    DEFAULTS = {
        "frames_s": 15.0,
        "transcribe_fixed_s": 5.0,
        "transcribe_rtf": 0.15,  # seconds of compute per second of audio
        "describe_s": 150.0,
        "sidecar_s": 1.0,
        "photo_prep_s": 1.0,
        "photo_describe_s": 40.0,
    }

    def __init__(self, path: Path):
        self.path = path
        self.data = dict(self.DEFAULTS)
        try:
            self.data.update(json.loads(path.read_text()))
        except Exception:  # noqa: BLE001
            pass

    def estimate(self, duration_s: float, do_tx: bool, do_desc: bool, photo: bool = False) -> dict:
        d = self.data
        if photo:
            return {"frames": d.get("photo_prep_s", 1.0), "transcribe": 0.0,
                    "describe": d.get("photo_describe_s", 40.0) if do_desc else 0.0, "sidecar": d["sidecar_s"]}
        return {
            "frames": d["frames_s"],
            "transcribe": (d["transcribe_fixed_s"] + d["transcribe_rtf"] * (duration_s or 0)) if do_tx else 0.0,
            "describe": d["describe_s"] if do_desc else 0.0,
            "sidecar": d["sidecar_s"],
        }

    def update(self, key: str, value: float, alpha: float = 0.4) -> None:
        old = self.data.get(key, value)
        self.data[key] = round((1 - alpha) * old + alpha * value, 3)

    def save(self) -> None:
        try:
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.data, indent=2) + "\n")
            tmp.replace(self.path)
        except OSError:
            pass


def eta_fields(seconds: float) -> dict:
    seconds = max(0.0, seconds)
    return {
        "eta_seconds": int(round(seconds)),
        "eta": (datetime.now().astimezone() + timedelta(seconds=seconds)).isoformat(timespec="seconds"),
    }


# -------------------------------------------------------------- helpers ----

def frames_reusable(frames_dir: Path, video: Path, percents: list[int]) -> list[Path] | None:
    paths = [frames_dir / f"{video.stem}_f{p:02d}.jpg" for p in percents]
    if not all(p.is_file() and p.stat().st_size > 0 for p in paths):
        return None
    vmt = video.stat().st_mtime
    if any(p.stat().st_mtime < vmt for p in paths):
        return None
    return paths


def review_decision(desc_res: dict, threshold: float) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if desc_res.get("status") != "ok":
        reasons.append(f"describe {desc_res.get('status')}: {desc_res.get('reason')}")
    else:
        conf = float(desc_res["description"]["confidence"])
        if conf < threshold:
            reasons.append(f"confidence {conf:.2f} < review_threshold {threshold:.2f}")
    return bool(reasons), reasons


# --------------------------------------------------------- per-clip run ----

def unreadable_reason(video: Path, info: dict) -> str | None:
    """Why a video can't be processed at all (no picture / zero length), else None.
    Typical cause: an empty or interrupted camera recording (only a ~1 KB MP4 header) or a partial copy."""
    has_video = info.get("has_video", True)  # probe_media always sets it; absent = unknown (treated as present)
    if has_video and float(info.get("duration_s") or 0) > 0 and not info.get("probe_error"):
        return None
    try:
        size = video.stat().st_size
    except OSError:
        size = 0
    human = f"{size} bytes" if size < 1024 else (f"{size / 1024:.1f} KB" if size < 1024 ** 2 else f"{size / 1024 ** 2:.1f} MB")
    what = "no video stream" if not has_video else "zero duration"
    hint = ("looks like an empty or interrupted recording" if size < 64 * 1024
            else "the file may be damaged or only partly copied")
    if info.get("probe_error"):
        what = f"ffprobe could not read it ({info['probe_error'].strip().splitlines()[-1][:120]})"
    return f"unreadable video: {what}, {human} — {hint}"


def unreadable_report(video: Path, reason: str, ctx: dict) -> dict:
    """Record an unreadable clip as needs review (name kept, never renamed) instead of crashing the run.
    Live mode logs it in rename-log.jsonl, so later runs skip it until it's replaced or --force is used."""
    cfg, dry = ctx["cfg"], ctx["dry"]
    print(f"SKIP {video.name}: {reason}")
    applied, action, final_path = None, "dry-run (not renamed)", str(video)
    if not dry:
        applied = apply_clip(video, None, cfg, needs_review=True, reasons=[reason], failed=True,
                             batch=ctx.get("batch"), origin="pipeline",
                             in_place=bool(ctx.get("in_place")), allow_protected=bool(ctx.get("allow_protected")))
        action = applied.get("action") or "needs-review"
        final_path = applied.get("new") or str(video)
        print(f"  action:     {action} ({applied.get('reason')})")
    report_line = {
        "time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": str(video),
        "media_kind": "video",
        "proposed": None,
        "clip_type": None,
        "confidence": None,
        "needs_review": True,
        "review_reasons": [reason],
        "sidecar_json": None,
        "note_id": None,
        "timing_s": {"total": 0.0},
        "dry_run": dry,
        "action": action,
        "final_path": final_path,
        "unreadable": True,
    }
    if applied and action not in ("renamed", "needs-review"):
        report_line["action_reason"] = applied.get("reason")
    rdir = abs_path(section(cfg, "sidecar").get("dry_run_dir") or "logs/dry-run")
    rdir.mkdir(parents=True, exist_ok=True)
    with open(rdir / "report.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(report_line, ensure_ascii=False) + "\n")
    write_status({"message": f"Needs review: {video.name} — {reason}"})
    return report_line


def process_video(video: Path, idx: int, total: int, queue_after: list, ctx: dict) -> dict:
    """queue_after: [(duration_s, is_photo)] of the clips after this one (plain floats = videos)."""
    cfg, args, eta, dry = ctx["cfg"], ctx["args"], ctx["eta"], ctx["dry"]
    t_clip = time.monotonic()
    timing: dict = {}
    ffmpeg = tool_path(cfg, "ffmpeg_path", "ffmpeg")
    ffprobe = tool_path(cfg, "ffprobe_path", "ffprobe")
    percents = list(cfg.get("frame_percents") or list(range(10, 100, 10)))
    processing = abs_path(cfg.get("processing_dir") or "processing")
    frames_dir = processing / f"{video.stem}_frames"

    photo = is_photo(video)
    if photo:
        info = photo_info(video)
        date_str, date_src = photo_date(info, video)
    else:
        try:
            info = probe_media(ffprobe, video)
        except (subprocess.CalledProcessError, OSError, ValueError) as e:
            why = (getattr(e, "stderr", None) or "").strip()  # ffprobe's own words, e.g. "moov atom not found"
            why = why.splitlines()[-1] if why else str(e)
            info = {"duration_s": 0.0, "has_video": False, "has_audio": False, "probe_error": why[:300]}
        bad = unreadable_reason(video, info)
        if bad:
            return unreadable_report(video, bad, ctx)
        date_str, date_src = clip_date(info, video)
    tx_on = bool(section(cfg, "whisper").get("enabled", True)) and not args.skip_whisper
    do_tx = tx_on and not photo  # photos: no Whisper
    do_desc = bool(section(cfg, "describe").get("enabled", True)) and not args.skip_describe
    est = eta.estimate(info["duration_s"], do_tx, do_desc, photo)
    queue = [q if isinstance(q, tuple) else (q, False) for q in queue_after]
    queue_est = sum(sum(eta.estimate(d, tx_on and not ph, do_desc, ph).values()) for d, ph in queue)

    def status(step: str, message: str, remaining: tuple[str, ...], **extra) -> None:
        rem = sum(est[s] for s in remaining) + queue_est
        write_status(
            {
                "state": "running",
                "current_file": str(video),
                "step": step,
                "message": message,
                "clip_index": idx,
                "clip_total": total,
                **eta_fields(rem),
                **extra,
            }
        )

    # 3) frames (photo: one resized JPEG) -----------------------------------
    t = time.monotonic()
    photo_prep = None
    paths = frames_reusable(frames_dir, video, percents) if args.reuse_frames and not photo else None
    if photo:
        status("extract_frames", "Preparing photo for the model", STEPS, frames_done=0, frames_total=1)
        max_px = int(section(cfg, "describe").get("max_image_px") or 672)
        try:
            jpg, how = prepare_photo(video, frames_dir, max_px, cfg, (info.get("exif") or {}).get("Orientation"))
            photo_prep = {"status": "ok", "file": str(jpg), "method": how, "max_px": max_px}
            print(f"Photo: {how} -> {jpg}")
        except RuntimeError as e:
            photo_prep = {"status": "error", "reason": str(e), "max_px": max_px}
            print(f"Photo [ERROR]: {e}")
        eta.update("photo_prep_s", time.monotonic() - t)
    elif paths:
        status("extract_frames", f"Reusing {len(paths)} existing frames", STEPS[1:], frames_done=len(paths), frames_total=len(percents))
        print(f"Frames: reusing {len(paths)} in {frames_dir}")
    else:
        status("extract_frames", "Extracting frames", STEPS, frames_done=0, frames_total=len(percents))
        paths = extract_frames(video, frames_dir, percents, ffmpeg, ffprobe, update_status=True)
        eta.update("frames_s", time.monotonic() - t)
        print(f"Frames: {len(paths)} -> {frames_dir}")
    timing["frames"] = round(time.monotonic() - t, 2)
    if photo:
        frames = [(0, Path(photo_prep["file"]))] if photo_prep.get("status") == "ok" else []
    else:
        frames = find_frames(frames_dir, video.stem)
        frames = [(pct, p) for pct, p in frames if pct in percents]

    # 4) transcribe (optional) --------------------------------------------
    t = time.monotonic()
    if do_tx:
        status("transcribe", "Transcribing audio (whisper.cpp)", STEPS[1:], frames_done=len(frames))
        tx = transcribe(video, cfg, info=info, update_status=True)
        if tx["status"] in ("ok", "no_speech") and info["duration_s"]:
            eta.update("transcribe_rtf", max(0.0, tx["elapsed_s"] - eta.data["transcribe_fixed_s"]) / info["duration_s"])
    else:
        why = "photo (no audio)" if photo else ("--skip-whisper" if args.skip_whisper else "whisper.enabled is false")
        tx = {"status": "skipped", "reason": why, "excerpt": "", "segments": [], "model": None, "elapsed_s": 0.0}
        print(f"Transcribe [SKIP]: {why}")
    timing["transcribe"] = round(time.monotonic() - t, 2)

    # 5) describe ---------------------------------------------------------
    t = time.monotonic()
    model = None
    if not do_desc or (photo and not frames):
        why = ("--skip-describe" if args.skip_describe else "describe.enabled is false") if not do_desc else \
            f"photo could not be prepared: {photo_prep.get('reason')}"
        dres = {"status": "skipped", "reason": why, "model": None, "attempts": 0, "description": None, "elapsed_s": 0.0}
    else:
        url = section(cfg, "describe").get("ollama_url") or "http://127.0.0.1:11434"
        try:
            avail = available_models(url)
            model, cands = choose_model(cfg, ctx["tier_info"], avail, args.model)
            why = None if model else (
                f"model not pulled (tried {cands}; installed: {avail or 'none'}). "
                f"With OLLAMA_MODELS={ROOT / 'models'}: ollama pull {cands[0] if cands else 'qwen2.5vl:7b'}"
            )
        except Exception as e:  # noqa: BLE001
            why = f"Ollama server not reachable at {url} ({e}) — start `ollama serve` with OLLAMA_MODELS set"
        if model:
            status("describe", f"Describing {'photo' if photo else 'clip'} with {model}", STEPS[2:], frames_done=len(frames))
            if photo:
                ex = info.get("exif") or {}
                dres = describe(video, frames, cfg, model, duration_s=None, transcript=None, update_status=True,
                                media_kind="photo",
                                photo_meta={"width": info.get("width"), "height": info.get("height"),
                                            "taken": info.get("creation_time"),
                                            "camera": " ".join(str(ex[k]) for k in ("Make", "Model") if ex.get(k)) or None})
            else:
                dres = describe(video, frames, cfg, model, duration_s=info["duration_s"], transcript=tx, update_status=True)
            if dres["status"] == "ok":
                eta.update("photo_describe_s" if photo else "describe_s", dres["elapsed_s"])
        else:
            dres = {"status": "skipped", "reason": why, "model": None, "attempts": 0, "description": None, "elapsed_s": 0.0}
    print(f"Describe [{dres['status'].upper()}]: " + (dres.get("reason") or f"{dres.get('model')} in {dres.get('elapsed_s')}s"))
    timing["describe"] = round(time.monotonic() - t, 2)

    # 6) propose + review -------------------------------------------------
    threshold = float(cfg.get("review_threshold", 0.6))
    needs_review, reasons = review_decision(dres, threshold)
    new_name = None
    if dres.get("status") == "ok":
        d = dres["description"]
        done_dir = abs_path(cfg.get("done_dir") or "done")
        taken = taken_names([done_dir, video.parent], exclude=video) | ctx["proposed"]
        fixed = ctx.get("project_slug")  # --project-from-folder: same {project} for the whole batch
        new_name = propose_filename(date_str, fixed or d["suggested_project"], d["suggested_subject"], d["clip_type"],
                                    video.suffix, cfg, taken, keep_project=bool(fixed))
        ctx["proposed"].add(new_name.lower())

    # 7) sidecar + report -------------------------------------------------
    t = time.monotonic()
    record = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "tool": "AI-Video-Renamer",
        "media_kind": "photo" if photo else "video",
        "dry_run": dry,
        "tier": ctx["tier"],
        "source": {
            "path": str(video),
            "name": video.name,
            "folder": video.parent.name,
            "size_bytes": video.stat().st_size,
            "media_kind": "photo" if photo else "video",
            **info,
            "date": date_str,
            "date_source": date_src,
        },
        "frames": ({"dir": str(frames_dir), "count": len(frames), "percents": [], "files": [str(p) for _, p in frames],
                    "photo": photo_prep} if photo else
                   {"dir": str(frames_dir), "count": len(frames), "percents": [p for p, _ in frames], "files": [str(p) for _, p in frames]}),
        "transcript": {k: v for k, v in tx.items() if k != "text"},
        "describe": {k: v for k, v in dres.items()},
        "proposal": {
            "new_name": new_name,
            "pattern": cfg.get("naming_pattern"),
            "needs_review": needs_review,
            "review_reasons": reasons,
            "review_threshold": threshold,
            "action": "none (dry-run)" if dry else (
                f"rename into inbox/{ctx['folder']}/ (live; applied right after sidecars)" if ctx.get("folder")
                else "rename in place in inbox/ (live; applied right after sidecars)"),
            "folder": ctx.get("folder"),
            "project_source": "folder" if ctx.get("project_slug") else "model",
        },
        "timing_s": timing,
    }
    ins_rec = ins.record(cfg.get("_instructions"),
                         (["describe"] if dres.get("instructions") else [])
                         + (["whisper"] if tx.get("prompt") else []))
    if ins_rec:
        record["instructions"] = ins_rec
    side = None
    want_side = section(cfg, "sidecar").get("enabled", True) and (dry or ns.next_to_clip(cfg))
    status("sidecar", "Writing notes" + (" + sidecars" if want_side else ""), STEPS[3:])
    if want_side:
        side = write_sidecars(video, record, cfg, dry_run=dry)
    timing["sidecar"] = round(time.monotonic() - t, 2)
    timing["total"] = round(time.monotonic() - t_clip, 2)
    record["timing_s"] = timing
    if side and side.get("json"):  # refresh timings in the JSON sidecar
        side = write_sidecars(video, record, cfg, dry_run=dry)
    if side:
        record["sidecar"] = {k: side.get(k) for k in ("dir", "location", "json", "md")}
    note_id = ns.save(cfg, record, current_path=video)["clip_id"]  # central store: always, full record (dry -> notes/dry-run/)

    conf_val = (dres.get("description") or {}).get("confidence")
    applied = None
    action = "dry-run (not renamed)"
    final_path = str(video)
    sidecar_json = side and side.get("json")
    if not dry:  # 8) live: apply this clip now (done/needs-review decided per clip)
        status("apply", f"Applying result for {video.name}", ())
        applied = apply_clip(
            video, new_name, cfg,
            needs_review=needs_review, reasons=reasons, confidence=conf_val,
            failed=dres.get("status") != "ok",
            sidecars=[side.get("json"), side.get("md")] if side else [],
            batch=ctx.get("batch"), origin="pipeline", folder=ctx.get("folder"),
            in_place=bool(ctx.get("in_place")), allow_protected=bool(ctx.get("allow_protected")),
        )
        action = applied["action"]
        if applied.get("new") and action in ("renamed", "needs-review"):
            final_path = applied["new"]
            js = Path(final_path).with_suffix(".json")
            if js.exists():
                sidecar_json = str(js)
        if action == "renamed":
            shown = (f"{applied['folder']}/" if applied.get("folder") else "") + Path(final_path).name
            status("apply", f"Renamed {video.name} -> {shown}", ())
        elif action == "needs-review":
            status("apply", f"Needs review: {video.name} (name kept)", ())
        else:
            status("apply", f"{video.name}: not renamed ({action}: {applied.get('reason')})", ())

    report_line = {
        "time": record["generated_at"],
        "source": str(video),
        "media_kind": "photo" if photo else "video",
        "proposed": new_name,
        "clip_type": (dres.get("description") or {}).get("clip_type"),
        "confidence": conf_val,
        "needs_review": needs_review,
        "review_reasons": reasons,
        "sidecar_json": sidecar_json,
        "note_id": note_id,
        "timing_s": timing,
        "dry_run": dry,
        "action": action,
        "final_path": final_path,
    }
    if applied and applied.get("collision"):
        report_line["collision"] = applied["collision"]
    if applied and applied.get("folder"):
        report_line["folder"] = applied["folder"]
    if ctx.get("project_slug"):
        report_line["project_source"] = "folder"
    if applied and action not in ("renamed", "needs-review"):
        report_line["action_reason"] = applied.get("reason")
    rdir = abs_path(section(cfg, "sidecar").get("dry_run_dir") or "logs/dry-run")
    rdir.mkdir(parents=True, exist_ok=True)
    with open(rdir / "report.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(report_line, ensure_ascii=False) + "\n")

    print("=== Dry-run report ===" if dry else "=== Report (live) ===")
    print(f"  source:     {video}")
    if new_name:
        print(f"  proposed:   {new_name}")
        if dry:
            dest = plan_rename(video, new_name, video.parent)
            where = f"into inbox/{ctx['folder']}/" if ctx.get("folder") else "in place"
            print(f"  would rename {where}: {dest.name}  [dry-run: config dry_run is true]")
    else:
        print("  proposed:   (none — no description)")
    if not dry:
        print(f"  action:     {action}" + (f" -> {(applied.get('folder') + '/') if applied.get('folder') else ''}"
                                           f"{Path(final_path).name}" if action == "renamed" else "")
              + (f"  ({applied.get('reason')})" if applied and action != "renamed" else ""))
        for n in (applied or {}).get("notes") or []:
            print(f"  note:       {n}")
    conf = report_line["confidence"]
    print(f"  confidence: {conf if conf is not None else 'n/a'}  threshold: {threshold}")
    print(f"  review:     {'NEEDS REVIEW — ' + '; '.join(reasons) if needs_review else 'ok'}")
    print(f"  notes:      {ns.record_path(cfg, note_id, dry=dry)}")
    if side:
        print(f"  sidecars:   {side['json']}\n              {side['md']}  ({side['location']})")
    print(f"  timing:     {timing}")
    eta.save()
    return report_line


def main() -> int:
    ap = argparse.ArgumentParser(description="AI Video Renamer pipeline (renames in place in inbox/ only when config dry_run is false)")
    ap.add_argument("--video", type=Path, action="append", default=None,
                    help="Explicit video/photo path (repeatable; else first inbox/ file)")
    ap.add_argument("--all", action="store_true", help="Process every video and photo in inbox/")
    ap.add_argument("--dry-run", action="store_true",
                    help="Force a dry run for this invocation (nothing renamed/moved; notes preview in notes/dry-run/), "
                         "even when config dry_run is false")
    ap.add_argument("--reuse-frames", action="store_true", help="Reuse processing/<stem>_frames if complete and newer than the video")
    ap.add_argument("--skip-whisper", action="store_true", help="Skip transcription")
    ap.add_argument("--skip-describe", action="store_true", help="Skip the VLM step")
    ap.add_argument("--model", default=None, help="Override VLM model (default: tier prefer/fallback)")
    ap.add_argument("--skip-resolve-check", action="store_true", help="Dangerous; for debugging only")
    ap.add_argument("--force", action="store_true",
                    help="Reprocess clips already handled (in rename-log, generated name, sidecar next to the video or notes store)")
    ap.add_argument("--folder", default=None, metavar="NAME",
                    help="Live mode: rename confident clips into inbox/NAME/ (created on the first rename); "
                         "needs-review clips stay in inbox/")
    ap.add_argument("--project-from-folder", action="store_true",
                    help="Use the slugified --folder name as {project} in every new filename (instead of the model's guess)")
    ap.add_argument("--source", action="append", default=None, metavar="PATH",
                    help="Process in place (ClipGauge v0.6): media at PATH (a file, or a folder walked recursively; "
                         "repeatable) is renamed where it is, no copy into inbox/. Held / Resolve-internal / "
                         "Cloud-synced / cloud-drive folders are refused (scripts/inplace.py)")
    ap.add_argument("--confirm-resolve", action="store_true",
                    help="With --source: allow renaming under the DaVinci Resolve folder (the user confirmed that the "
                         "clips are not imported in a Resolve project — renaming imported media breaks its links)")
    ap.add_argument("--instructions-file", type=Path, default=None, metavar="FILE",
                    help="Per-batch instructions for this run (replaces the saved next-run text; standing instructions "
                         "and the glossary still apply). See scripts/instructions.py")
    ap.add_argument("--no-instructions", action="store_true",
                    help="Ignore custom instructions and the glossary for this run")
    args = ap.parse_args()

    # Model/tool upgrades (apply_updates.py) swap Ollama/whisper underneath a run: never overlap them.
    upd = active_updates()
    if upd:
        print(f"Model/tool updates are being installed (pid {upd.get('pid')}, started {upd.get('started_at')}) — "
              "processing can start when they finish (ClipGauge › Setup › Updates).", file=sys.stderr)
        return 5
    # Single-run lock (Terminal vs web UI share status.json): refuse before touching status.json.
    lock = lock_path()
    ok, holder = acquire_lock(lock)
    if not ok:
        print(
            f"Another pipeline run is already active (pid {holder.get('pid')}, started {holder.get('started_at')}, "
            f"from {holder.get('launched_by')}). Lock: {lock} — not starting.",
            file=sys.stderr,
        )
        return 3
    prev = {sig: signal.signal(sig, _stop_signal) for sig in (signal.SIGTERM, signal.SIGHUP)}
    try:
        return run(args)
    except KeyboardInterrupt:
        msg = ("Stopped (Ctrl-C / Stop button) — finished clips are in report.jsonl "
               "(live mode: finished clips were already renamed, see logs/rename-log.jsonl).")
        try:
            write_status({"state": "stopped", "step": "stopped", "message": msg, "eta": None, "eta_seconds": None})
        except OSError:
            pass
        print("\n" + msg, file=sys.stderr)
        return 130
    finally:
        for sig, handler in prev.items():
            signal.signal(sig, handler)
        release_lock(lock)


def _stop_signal(signum, frame) -> None:  # noqa: ARG001
    """SIGTERM (UI Stop button) / SIGHUP (Terminal closed) -> same clean path as Ctrl-C."""
    raise KeyboardInterrupt(f"signal {signum}")


def run(args: argparse.Namespace) -> int:
    cfg = load_config()
    dry = bool(cfg.get("dry_run", True)) or bool(getattr(args, "dry_run", False))
    frames_total = len(cfg.get("frame_percents") or [10, 20, 30, 40, 50, 60, 70, 80, 90])
    act, ins_err = None, None
    if not getattr(args, "no_instructions", False):
        try:
            f = getattr(args, "instructions_file", None)
            bt = Path(f).expanduser().read_text(encoding="utf-8") if f else None
            if bt is not None and len(bt) > ins.LIMITS["next_run_chars"]:
                raise ValueError(f"{f} has {len(bt)} characters (max {ins.LIMITS['next_run_chars']})")
            act = ins.resolve(cfg, batch_text=bt, batch_source=(f"file:{Path(f).name}" if f else None))
        except (OSError, UnicodeDecodeError, ValueError) as e:
            ins_err = f"Instructions file: {e} — not starting."
    cfg["_instructions"] = act if act and act.get("active") else None

    write_status(
        {
            "state": "starting",
            "queue": [],
            "current_file": None,
            "step": "init",
            "frames_done": 0,
            "frames_total": frames_total,
            "eta": None,
            "eta_seconds": None,
            "message": "Pipeline starting",
            "last_error": None,
            "dry_run": dry,
            "needs_review": [],
            "instructions": ({"active": True, "hash": act["hash"], "batch": bool(act["batch"]),
                              "glossary_terms": len(act["glossary"])} if cfg["_instructions"] else {"active": False}),
        },
        merge=False,
    )
    if ins_err:
        write_status({"state": "error", "step": "init", "message": ins_err, "last_error": ins_err})
        print(ins_err, file=sys.stderr)
        return 2
    if cfg["_instructions"]:
        a = cfg["_instructions"]
        print(f"Instructions: active (hash {a['hash']}; standing {len(a['standing'])} chars, "
              f"batch {len(a['batch'])} chars{' from ' + a['batch_source'] if a['batch'] else ''}, "
              f"glossary {len(a['glossary'])} terms)")

    # batch folder options (validated before anything is processed)
    folder, project_slug = None, None
    try:
        if getattr(args, "folder", None) is not None:
            folder = folder_path(cfg, args.folder).name
        if getattr(args, "project_from_folder", False):
            if not folder:
                raise ValueError("--project-from-folder needs --folder NAME")
            project_slug = folder_project_slug(folder, cfg)
            if not project_slug:
                raise ValueError(f"{folder!r} gives no usable project slug (needs letters or digits)")
    except ValueError as e:
        msg = f"Folder option: {e} — not starting."
        write_status({"state": "error", "step": "init", "message": msg, "last_error": msg})
        print(msg, file=sys.stderr)
        return 2
    sources = [str(x) for x in (getattr(args, "source", None) or [])]
    if sources and (folder or getattr(args, "video", None)):
        msg = "--source (process in place) can't be combined with --folder or --video — not starting."
        write_status({"state": "error", "step": "init", "message": msg, "last_error": msg})
        print(msg, file=sys.stderr)
        return 2
    if folder:
        print(f"Batch folder: inbox/{folder}/ — confident clips are renamed into it"
              + (" (dry run: nothing is moved)" if dry else "; needs-review clips stay in inbox/")
              + (f"; project in filenames: {project_slug}" if project_slug else ""))

    # 1) Resolve guard
    if cfg.get("resolve_guard", True) and not args.skip_resolve_check:
        hits = resolve_running()
        if hits:
            msg = "DaVinci Resolve is running — aborting (resolve_guard)"
            write_status({"state": "blocked", "step": "resolve_guard", "message": msg, "last_error": msg})
            print(msg, file=sys.stderr)
            for h in hits[:5]:
                print(f"  {h}", file=sys.stderr)
            return 1
        print("Resolve guard: OK")

    # 2) Pick file(s)
    inbox = abs_path(cfg.get("inbox_dir") or "inbox")
    force = bool(getattr(args, "force", False))
    skipped: list = []
    in_place, confirm = bool(sources), bool(getattr(args, "confirm_resolve", False))
    if sources:
        import inplace
        sc = inplace.sort_cfg(cfg)
        found: list[Path] = []
        needs_confirm: list[str] = []
        for src in sources:
            files, refused = inplace.media_in(src, cfg, sc)
            for r in refused:
                print(f"  refused {r['path']}: {r['reason']}")
            for f in files:
                c = inplace.classify(f, cfg, sc)
                if c["status"] == "refused":
                    print(f"  refused {f}: {c['reason']}")
                    continue
                if c["status"] == "needs_confirm":
                    needs_confirm.append(str(f))
                if all(str(f) != str(x) for x in found):
                    found.append(f)
        if needs_confirm and not dry and not confirm:
            msg = (f"{len(needs_confirm)} clip(s) are inside the DaVinci Resolve folder — renaming media Resolve has "
                   "imported breaks its links. Confirm in ClipGauge (or pass --confirm-resolve) — not starting.")
            write_status({"state": "error", "step": "init", "message": msg, "last_error": msg})
            print(msg, file=sys.stderr)
            return 2
        print(f"Process in place: {len(found)} media file(s) from {len(sources)} source(s)"
              + (" (dry run: nothing is renamed)" if dry else " — renamed where they are"))
        videos, skipped = filter_unhandled(found, cfg, force=force)  # every chosen clip (no --all needed)
    elif args.video:
        given = args.video if isinstance(args.video, list) else [args.video]
        videos, skipped = filter_unhandled([Path(v).expanduser().resolve() for v in given], cfg, force=force)
    else:
        videos, skipped = select_inbox_videos(cfg, force=force)
        if not args.all:
            videos = videos[:1]
    if skipped:
        print(f"Skipping {len(skipped)} already-handled clip(s) (use --force to reprocess):")
        for p, why in skipped[:200]:
            print(f"  skip {p.name}: {why}")

    if not videos:
        queue = [str(p) for p in inbox_videos(inbox)]
        msg = (f"Nothing new to process: {len(skipped)} clip(s) already handled (renamed / needs review) — "
               "use --force to reprocess" if skipped else
               ("No video or photo found in the chosen file(s)/folder(s)" if sources else
                f"No video or photo in inbox: {inbox} — copy one there, or pass --video PATH"))
        write_status({"state": "idle", "step": "await_inbox", "queue": queue, "message": msg})
        print(msg)
        print("Example (read-only, sidecars go to logs/dry-run/):")
        print("  python3 scripts/run_pipeline.py --video '/Volumes/Lexar/DaVinci Resolve/E-Reader Review/x4 unboxing.MP4' --reuse-frames")
        return 0
    for v in videos:
        if not v.is_file():
            msg = f"Video not found: {v}"
            write_status({"state": "error", "message": msg, "last_error": msg})
            print(msg, file=sys.stderr)
            return 2

    tier, tier_info, gb = current_tier()
    models_env = os.environ.get("OLLAMA_MODELS", "")
    print(f"Tier: {tier} ({gb:.1f} GiB) prefer={tier_info.get('prefer')}  dry_run={dry}")
    if models_env and Path(models_env).resolve() != (ROOT / "models").resolve():
        print(f"NOTE: OLLAMA_MODELS should be {ROOT / 'models'} (ExFAT Lexar tree).", file=sys.stderr)

    ffprobe = tool_path(cfg, "ffprobe_path", "ffprobe")
    durations = []
    for v in videos:
        if is_photo(v):
            durations.append((0.0, True))
            continue
        try:
            durations.append((probe_media(ffprobe, v)["duration_s"], False))
        except Exception:  # noqa: BLE001
            durations.append((0.0, False))

    ctx = {
        "cfg": cfg,
        "args": args,
        "dry": dry,
        "tier": tier,
        "tier_info": tier_info,
        "eta": EtaModel(abs_path(cfg.get("logs_dir") or "logs") / "timings.json"),
        "proposed": set(),
        "batch": new_batch_id("pipeline"),
        "folder": folder,
        "project_slug": project_slug,
        "in_place": in_place,
        "allow_protected": confirm,
    }
    reports, review = [], []
    rc = 0
    for i, v in enumerate(videos, start=1):
        write_status({"state": "running", "queue": [str(x) for x in videos[i:]], "current_file": str(v), "step": "selected", "message": f"Selected {v.name} ({i}/{len(videos)})"})
        print(f"\n[{i}/{len(videos)}] Selected: {v}")
        try:
            rep = process_video(v, i, len(videos), durations[i:], ctx)
        except Exception as e:  # noqa: BLE001
            msg = f"{v.name}: {type(e).__name__}: {e}"
            write_status({"state": "error", "message": msg, "last_error": msg})
            print(f"ERROR {msg}", file=sys.stderr)
            rc = 1
            continue
        reports.append(rep)
        if rep["needs_review"]:
            review.append(str(v))

    n_ok = sum(1 for r in reports if r["proposed"])
    skip_note = f" {len(skipped)} already-handled clip(s) skipped." if skipped else ""
    if dry:
        msg = f"Done: {n_ok}/{len(videos)} proposals, {len(review)} need review. dry_run={dry} — no renames performed.{skip_note}"
    else:
        n_ren = sum(1 for r in reports if r.get("action") == "renamed")
        n_rev = sum(1 for r in reports if r.get("action") == "needs-review")
        n_other = len(reports) - n_ren - n_rev
        n_err = len(videos) - len(reports)
        where = f"into inbox/{folder}/" if folder else ("where they are" if in_place else "in place")
        msg = (f"Done (live): {n_ren} renamed {where}, {n_rev} need review (name kept)"
               + (f", {n_other} not applied" if n_other else "") + (f", {n_err} failed" if n_err else "")
               + f" — of {len(videos)} clip(s). Undo: python3 scripts/undo_renames.py --dry-run.{skip_note}")
    write_status(
        {
            "state": "done" if rc == 0 else "done_with_errors",
            "step": "report",
            "queue": [],
            "message": msg,
            "needs_review": review,
            "eta": None,
            "eta_seconds": 0,
        }
    )
    # next-run instructions are used up by a live run that processed something (dry runs are previews: kept)
    if cfg.get("_instructions") and not dry and reports and ins.consume_next_run(cfg, cfg["_instructions"]):
        print("Next-run instructions used and cleared (tick 'keep' in Instructions to reuse them).")
    print("\n" + msg)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
