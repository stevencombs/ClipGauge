#!/usr/bin/env python3
"""
JSON bridge between ClipGauge (the native menu bar app, v0.6) and the engine. No HTTP, no server, nothing leaves the Mac.
Every command prints ONE JSON object on stdout ({"code": <http-like status>, ...}); copy-in prints one JSON object per
line (progress events) and ends with {"event": "done", ...}.

  python3 scripts/clipgauge_cli.py status                 # status.json + run/update locks + last-error card + tier model
  python3 scripts/clipgauge_cli.py inbox                  # inbox files/folders with pending / renamed / needs review
  python3 scripts/clipgauge_cli.py start '{"dry_run": true, "use_folder": true, "folder": "Show", "project_from_folder": true}'
  python3 scripts/clipgauge_cli.py start '{"sources": ["/Volumes/Lexar/Footage/Day 1"], "confirm_resolve": false}'
  python3 scripts/clipgauge_cli.py stop
  python3 scripts/clipgauge_cli.py results [--limit N]
  python3 scripts/clipgauge_cli.py apply '{"use_folder": false}'
  python3 scripts/clipgauge_cli.py move-into NAME
  python3 scripts/clipgauge_cli.py folder-check NAME
  python3 scripts/clipgauge_cli.py check-path PATH [PATH ...]      # in-place verdict + media count + copy plan
  python3 scripts/clipgauge_cli.py copy-in [--folder-hint NAME] PATH [PATH ...]  # copy media into inbox/ (progress)
  python3 scripts/clipgauge_cli.py undo --id LOG_ID | --batch BATCH [--preview]
  python3 scripts/clipgauge_cli.py review-accept --file PATH --name NEW_NAME [--confirm-resolve]
  python3 scripts/clipgauge_cli.py bundle-scopes
  python3 scripts/clipgauge_cli.py bundle '{"scope": "inbox", "include_silent": false}'
  python3 scripts/clipgauge_cli.py note CLIP_ID           # a clip's notes as Markdown (Ask the Model context)

Safety: start/apply/move/undo/review-accept use the same guards as everywhere else (single-run lock, update lock,
Resolve guard, held / Resolve-internal / Cloud-synced folders refused, never overwrite, every rename logged in
logs/rename-log.jsonl and undoable). copy-in never overwrites (same name + same size = duplicate, skipped; otherwise
_2, _3 …), writes to a hidden .uploading- temp first and keeps the original modification date.
"""
from __future__ import annotations

import json
import re
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import renamer_actions as ra  # noqa: E402
from _common import ROOT, load_config  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent
SMALL_FILE = 64 * 1024


def out(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def coded(code: int, body: dict) -> dict:
    return {"code": code, "ok": 200 <= code < 300 and not body.get("error"), **body}


def parse_opts(args: list[str]) -> dict:
    if not args:
        return {}
    try:
        v = json.loads(args[0])
    except json.JSONDecodeError as e:
        raise SystemExit(out(coded(400, {"error": f"options must be JSON: {e}"})) or 2)
    return v if isinstance(v, dict) else {}


# ----------------------------------------------------------------- status ----

def human(n: int) -> str:
    return f"{n} bytes" if n < 1024 else f"{n / 1024:.1f} KB" if n < 1024 ** 2 else f"{n / 1024 ** 2:.1f} MB"


# Errors a newer engine no longer produces (signature, fixed in). A card with one of these from a run that predates
# the version stamp in status.json ("engine", v0.6.1+) is shown as handled: re-running can't hit it again.
FIXED_ERRORS = (("KeyError: 'duration'", "0.6"),)
MEDIA_NAME = re.compile(r"[^\s:;,/'\"()]+\.(?:mp4|mov|m4v|avi|mkv|mts|m2ts|mxf|insv|360|lrv|heic|heif|jpe?g|png|dng|arw|"
                        r"cr2|cr3|nef|raf|orf|rw2|tiff?|webp|gif|mpg|mpeg|3gp|wmv|webm)\b", re.I)


def _mentioned_files(st: dict, err: str) -> list[str]:
    names = []
    for m in MEDIA_NAME.finditer(err):
        if m.group(0) not in names:
            names.append(m.group(0))
    first = err.split(":", 1)[0].strip() if ":" in err else ""
    if first and "/" not in first and first not in names and "." in first:
        names.insert(0, first)
    return names


def problem_card(cfg: dict, st: dict) -> dict | None:
    """Last run ended with errors -> what failed, why, and what happens next (shown in the popover).
    v0.6.1: the card resolves itself (resolved=True, auto=True) when every file the error mentions is gone from the
    inbox (and from where an in-place run found it), or when the error is one the current engine can't produce any
    more and the run predates the engine version stamp. status.json itself is never edited."""
    if st.get("state") not in ("done_with_errors", "error") or not st.get("last_error"):
        return None
    err = str(st["last_error"])
    names = _mentioned_files(st, err)
    name = names[0] if names else ""
    card = {"reason": err, "updated_at": st.get("updated_at"), "file": None, "exists": False, "size": None,
            "state": st.get("state"), "hint": "", "files": names, "engine": st.get("engine")}
    cur = Path(str(st["current_file"])) if st.get("current_file") else None
    found: list[Path] = []
    for n in names:
        cands = [ra.inbox_dir(cfg) / n]
        if cur and cur.name == n:
            cands.insert(0, cur)
        hit = next((p for p in cands if p.is_file()), None)
        if hit:
            found.append(hit)
    fixed_in = next((v for sig, v in FIXED_ERRORS if sig in err), None)
    if fixed_in and not st.get("engine"):
        card.update(resolved=True, auto=True,
                    hint=f"This error came from an older engine (before v{fixed_in}); the current one handles it — "
                         "nothing to do." + (f" {found[0].name} is still in the inbox and will be marked Needs review "
                                             "(name kept) on the next run if it can't be read." if found else ""))
        if found:
            card.update(file=str(found[0]), exists=True, size=found[0].stat().st_size)
        return card
    if not found:
        if names:
            card.update(resolved=True, auto=True,
                        hint=(f"{name} is" if len(names) == 1 else f"All {len(names)} files it mentions are")
                        + " no longer in the inbox — nothing left to fix.")
        return card
    hit = found[0]
    size = hit.stat().st_size
    card.update(file=str(hit), exists=True, size=size)
    idx = ra.ar.log_index(cfg)
    if all(idx["review"].get(ra.ar.norm(f)) for f in found):
        card["hint"] = "Since then it was marked Needs review (name kept) — nothing else to do."
        card["resolved"] = True
    elif size < SMALL_FILE:
        card["hint"] = (f"{hit.name} is only {human(size)} — an empty or interrupted camera recording (no video in "
                        "it). The current engine marks it Needs review (name kept) on the next run instead of "
                        "failing. You can also delete it in Finder.")
    else:
        card["hint"] = "The next run retries it; if it fails again it is marked Needs review (name kept)."
    return card


def cmd_status(cfg: dict) -> dict:
    st = ra.read_status(cfg)
    run = ra.run_info(cfg)
    upd = ra.active_updates(cfg)
    vision = None
    try:
        import detect_ram
        _tier, info, _gb = detect_ram.current_tier()
        vision = info.get("prefer") or info.get("fallback")
    except Exception:  # noqa: BLE001
        pass
    return coded(200, {"status": st, "run": run, "updates_active": bool(upd), "updates": upd or None,
                       "config_dry_run": bool(cfg.get("dry_run", True)), "problem": problem_card(cfg, st),
                       "vision_model": vision, "root": str(ROOT),
                       "resolve_running": ra.check_resolve()[0]})


# ---------------------------------------------------------------- in place ----

def cmd_check_path(cfg: dict, paths: list[str]) -> dict:
    import inplace
    sc = inplace.sort_cfg(cfg)
    items = []
    for p in paths:
        c = inplace.classify(p, cfg, sc)
        files, refused = inplace.media_in(p, cfg, sc) if c["status"] != "refused" else ([], [])
        nc = c["status"] == "needs_confirm" or any(inplace.classify(f, cfg, sc)["status"] == "needs_confirm"
                                                   for f in files[:200])
        size = 0
        for f in files:
            try:
                size += f.stat().st_size
            except OSError:
                pass
        # copying only reads the source: allowed from Resolve folders, never from held folders
        copy_ok = not any(inplace._nfc(part) in {inplace._nfc(h) for h in sc.get("hold_sources") or []}
                          for part in Path(c["path"]).parts)
        if not files and c["status"] != "refused" and Path(c["path"]).exists():
            cf, _ = copy_media(Path(c["path"]))
            files_for_copy = cf
        else:
            files_for_copy = files
        items.append({**c, "needs_confirm": nc, "media_count": len(files), "bytes": size,
                      "media": [str(f) for f in files[:500]], "skipped": refused[:50], "copy_ok": copy_ok,
                      "copy_count": len(files_for_copy)})
    return coded(200, {"items": items, "inbox": str(ra.inbox_dir(cfg))})


# ---------------------------------------------------------------- copy in ----

def copy_media(p: Path) -> tuple[list[Path], list[str]]:
    """Media to copy from a dropped file/folder (folders walked, hidden/package folders skipped, max 2000)."""
    import inplace
    if p.is_file():
        ok = p.suffix.lower() in ra.MEDIA_EXTS and not p.name.startswith(".")
        return ([p], []) if ok else ([], [f"{p.name}: not a video or photo"])
    found = []
    for dirpath, dirnames, filenames in os.walk(p):
        dirnames[:] = sorted(n for n in dirnames if not n.startswith(".")
                             and not n.lower().endswith(inplace.PACKAGE_SUFFIXES))
        for f in sorted(filenames):
            if not f.startswith(".") and Path(f).suffix.lower() in ra.MEDIA_EXTS:
                found.append(Path(dirpath) / f)
                if len(found) >= inplace.MAX_FILES:
                    return found, [f"more than {inplace.MAX_FILES} files — only the first {inplace.MAX_FILES} copied"]
    return found, []


class Cancelled(Exception):
    pass


def _cancel(signum, frame):  # noqa: ARG001
    raise Cancelled()


def cmd_copy_in(cfg: dict, paths: list[str]) -> int:
    """Copy media into inbox/ top level (the pipeline only processes the top level; a batch folder name is applied
    by the run with --folder). Streams progress lines."""
    import inplace
    sc = inplace.sort_cfg(cfg)
    held = {inplace._nfc(h) for h in sc.get("hold_sources") or []}
    inbox = ra.inbox_dir(cfg)
    inbox.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    notes: list[str] = []
    for raw in paths:
        p = Path(raw).expanduser()
        try:
            p = p.resolve()
        except OSError:
            pass
        if not p.exists():
            notes.append(f"{p.name}: not found")
            continue
        if any(inplace._nfc(part) in held for part in p.parts):
            notes.append(f"{p.name}: inside a folder on hold — not copied")
            continue
        if ra.ar.in_inbox(p, cfg):
            notes.append(f"{p.name}: already in the inbox")
            continue
        f, n = copy_media(p)
        files += f
        notes += n
    total = 0
    for f in files:
        try:
            total += f.stat().st_size
        except OSError:
            pass
    free = ra.shutil.disk_usage(inbox).free
    if total > free - 512 * 1024 * 1024:
        out({"event": "done", "ok": False, "error": f"Not enough free space on the drive: need {human(total)}, "
                                                    f"{human(free)} free (keeping 512 MB spare).", "copied": [],
             "skipped": notes})
        return 1
    out({"event": "start", "files": len(files), "total_bytes": total, "inbox": str(inbox), "notes": notes})
    signal.signal(signal.SIGTERM, _cancel)
    signal.signal(signal.SIGINT, _cancel)
    copied, skipped, done_bytes = [], list(notes), 0
    last = 0.0
    tmp: Path | None = None
    try:
        for i, src in enumerate(files, 1):
            size = src.stat().st_size
            with ra.UPLOAD_LOCK:
                plan = ra.upload_plan(inbox, src.name, size)
            if plan["action"] == "duplicate":
                skipped.append(plan["message"])
                done_bytes += size
                continue
            final = plan["name"]
            tmp = inbox / f"{ra.TEMP_PREFIX}{final}"
            if tmp.exists():
                tmp.unlink()
            with open(src, "rb") as fi, open(tmp, "xb") as fo:
                while True:
                    chunk = fi.read(ra.CHUNK * 4)
                    if not chunk:
                        break
                    fo.write(chunk)
                    done_bytes += len(chunk)
                    now = time.monotonic()
                    if now - last > 0.25:
                        last = now
                        out({"event": "progress", "file": src.name, "index": i, "files": len(files),
                             "done_bytes": done_bytes, "total_bytes": total})
            if tmp.stat().st_size != size:
                tmp.unlink()
                tmp = None
                skipped.append(f"{src.name}: size changed while copying — not saved")
                continue
            st = src.stat()
            os.utime(tmp, (st.st_atime, st.st_mtime))  # keep the recording date (used when metadata has none)
            dest = inbox / final
            if dest.exists():  # appeared meanwhile: pick again, never overwrite
                final = ra.unique_name(inbox, final)
                dest = inbox / final
            os.rename(tmp, dest)
            tmp = None
            copied.append({"from": str(src), "to": str(dest), "name": final, "renamed": final != src.name})
            out({"event": "file", "file": src.name, "saved_as": final, "index": i, "files": len(files),
                 "done_bytes": done_bytes, "total_bytes": total})
    except Cancelled:
        if tmp and tmp.exists():
            tmp.unlink()
        out({"event": "done", "ok": False, "cancelled": True, "copied": copied, "skipped": skipped,
             "message": f"Cancelled — {len(copied)} file(s) copied before stopping (nothing partial left behind)."})
        return 130
    except OSError as e:
        if tmp and tmp.exists():
            tmp.unlink()
        out({"event": "done", "ok": False, "error": f"{type(e).__name__}: {e}", "copied": copied, "skipped": skipped})
        return 1
    msg = f"Copied {len(copied)} file(s) into the inbox" + (f"; {len(skipped)} skipped" if skipped else "") + "."
    out({"event": "done", "ok": True, "copied": copied, "skipped": skipped, "message": msg})
    return 0


# ------------------------------------------------------------ undo / review ----

def run_script(args: list[str], timeout: int = 1800) -> tuple[int, dict, str]:
    env = dict(os.environ, AI_VIDEO_RENAMER_LAUNCHED_BY="ClipGauge", PYTHONUNBUFFERED="1")
    r = subprocess.run([sys.executable, *args], cwd=str(ROOT), env=env, capture_output=True, text=True,
                       timeout=timeout, stdin=subprocess.DEVNULL)
    lines = (r.stdout or "").strip().splitlines()
    try:
        j = json.loads(lines[-1]) if lines else {}
    except json.JSONDecodeError:
        j = {}
    return r.returncode, j, (r.stdout or "") + (r.stderr or "")


def cmd_undo(cfg: dict, args: list[str]) -> dict:
    sel = []
    if "--id" in args:
        sel = ["--id", args[args.index("--id") + 1]]
    elif "--batch" in args:
        sel = ["--batch", args[args.index("--batch") + 1]]
    else:
        return coded(400, {"error": "undo needs --id LOG_ID or --batch BATCH"})
    rc, j, text = run_script([str(SCRIPTS / "undo_renames.py"), *sel, "--json"]
                             + (["--dry-run"] if "--preview" in args else []))
    if not j:
        return coded(500, {"error": f"undo_renames.py exit {rc}", "detail": text[-1500:]})
    return coded(409 if rc == 3 else 200, {**j, "output": text[-4000:]})


def cmd_review_accept(cfg: dict, args: list[str]) -> dict:
    """Rename a needs-review clip to the proposed (or edited) name — logged and undoable like any rename."""
    try:
        video = Path(args[args.index("--file") + 1]).expanduser()
        raw = args[args.index("--name") + 1]
    except (ValueError, IndexError):
        return coded(400, {"error": "review-accept needs --file PATH --name NEW_NAME"})
    if cfg.get("dry_run", True):
        return coded(409, {"error": "config dry_run is true — renaming is switched off. Nothing changed."})
    if not video.is_file():
        return coded(404, {"error": f"{video.name} is no longer there."})
    name = ra.sanitize_filename(raw.strip())
    if not name or name.startswith("."):
        return coded(400, {"error": "Enter a file name."})
    if Path(name).suffix.lower() != video.suffix.lower():
        name = f"{name}{video.suffix}" if Path(name).suffix.lower() not in ra.MEDIA_EXTS else \
            f"{Path(name).stem}{video.suffix}"
    if name == video.name:
        return coded(400, {"error": "That's the current name — nothing to change."})
    from pipeline_lock import acquire_lock, lock_path, release_lock
    if ra.active_updates(cfg):
        return coded(409, {"error": "Updates are being installed — try again when they finish."})
    lock = lock_path(cfg)
    ok, holder = acquire_lock(lock)
    if not ok:
        return coded(409, {"error": f"A run is active (pid {holder.get('pid')}) — try again when it finishes."})
    try:
        in_place = not ra.ar.in_inbox(video, cfg)
        sidecars = [p for p in (video.with_suffix(".json"), video.with_suffix(".md")) if p.is_file()]
        res = ra.ar.apply_clip(video, name, cfg, needs_review=False, confidence=None, sidecars=sidecars,
                               origin="clipgauge-review", in_place=in_place,
                               allow_protected="--confirm-resolve" in args)
    finally:
        release_lock(lock)
    if res.get("action") == "renamed":
        return coded(200, {"message": f"Renamed to {Path(res['new']).name} (Undo is in the results table).",
                           "new": res["new"], "log_id": res.get("id"), "batch": res.get("batch")})
    return coded(409, {"error": res.get("reason") or res.get("action"), "result": res})


# ------------------------------------------------------------------- main ----

def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    cfg = load_config()
    if cmd == "copy-in":
        return cmd_copy_in(cfg, [a for a in rest if not a.startswith("--")])
    try:
        if cmd == "status":
            res = cmd_status(cfg)
        elif cmd == "inbox":
            res = coded(200, ra.list_inbox(cfg))
        elif cmd == "start":
            res = coded(*ra.start_pipeline(cfg, {**parse_opts(rest), "origin": "clipgauge"}))
        elif cmd == "stop":
            res = coded(*ra.stop_pipeline(cfg))
        elif cmd == "results":
            limit = int(rest[rest.index("--limit") + 1]) if "--limit" in rest else 300
            res = coded(200, ra.read_results(cfg, limit=limit))
        elif cmd == "apply":
            res = coded(*ra.apply_proposed(cfg, parse_opts(rest)))
        elif cmd == "move-into":
            res = coded(*ra.move_into_folder(cfg, {"folder": rest[0] if rest else ""}))
        elif cmd == "folder-check":
            try:
                res = coded(200, ra.folder_check(cfg, rest[0] if rest else ""))
            except ValueError as e:
                res = coded(400, {"error": str(e)})
        elif cmd == "check-path":
            res = cmd_check_path(cfg, rest)
        elif cmd == "undo":
            res = cmd_undo(cfg, rest)
        elif cmd == "review-accept":
            res = cmd_review_accept(cfg, rest)
        elif cmd == "bundle-scopes":
            res = coded(200, ra.transcript_scopes(cfg))
        elif cmd == "bundle":
            res = coded(*ra.build_transcript_bundle(cfg, parse_opts(rest)))
        elif cmd == "note":
            code, md = ra.note_markdown(cfg, rest[0] if rest else "")
            res = coded(code, {"markdown": md} if code == 200 else {"error": md})
        else:
            res = coded(400, {"error": f"unknown command {cmd!r}"})
    except Exception as e:  # noqa: BLE001  — the app shows this instead of a crash
        res = coded(500, {"error": f"{type(e).__name__}: {e}"})
    out(res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
