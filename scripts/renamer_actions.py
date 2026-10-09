#!/usr/bin/env python3
"""
Shared actions behind ClipGauge (via clipgauge_cli.py) and the legacy web UI (web_ui.py): inbox listing, upload/copy
naming, run start/stop, results, apply / move-into-folder, transcript bundles, sort and instruction helpers.
No HTTP here. Moved out of web_ui.py in v0.6 so the web UI can be deleted in v0.7 without touching ClipGauge.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import CONFIG_PATH, ROOT, abs_path, atomic_write_text, load_config, section, status_path  # noqa: E402
from pipeline_lock import active_updates, lock_path, lock_state, pid_alive, pid_command, updates_lock_path  # noqa: E402
import apply_renames as ar  # noqa: E402
import build_transcript_bundle as tb  # noqa: E402
import instructions as ins  # noqa: E402
import notes_store as ns  # noqa: E402

APP_ID = "ai-video-renamer-ui"
SCRIPTS = Path(__file__).resolve().parent
DEFAULT_PORT = 8765
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
TEMP_PREFIX = ".uploading-"
# Media types from notes_store (same as run_pipeline; selftest checks) — anything else would never be processed.
VIDEO_EXTS = ns.VIDEO_EXTS
PHOTO_EXTS = ns.PHOTO_EXTS
MEDIA_EXTS = ns.MEDIA_EXTS  # accepted by upload / drag-drop and listed as processable in the inbox
CHUNK = 1024 * 1024  # 1 MiB streaming chunks: memory stays flat for multi-GB uploads
MAX_NAME_BYTES = 200
EXFAT_BAD = re.compile(r'[\\/:*?"<>|]')
QUIET_PATHS = {"/api/status", "/api/inbox", "/api/updates", "/api/results", "/api/ping", "/api/upload-check", "/api/folder-check",
               "/api/transcript-scopes", "/favicon.ico"}
EXPORT_NAME_RE = re.compile(r"^transcripts-[A-Za-z0-9._-]{1,180}\.(json|md)$")
EXPORT_TYPES = {".json": "application/json; charset=utf-8", ".md": "text/markdown; charset=utf-8"}
SUBFOLDER_DETAIL = "inside a subfolder — the pipeline only processes files directly in inbox/ (move it out to process it)"

UPLOAD_LOCK = threading.Lock()  # name reservation + final rename
START_LOCK = threading.Lock()  # one start attempt at a time
BUNDLE_LOCK = threading.Lock()  # one transcript bundle build at a time
RESERVED: set[str] = set()  # lowercase final names of in-flight uploads (ExFAT is case-insensitive)


def log(msg: str) -> None:
    print(f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] {msg}", file=sys.stderr, flush=True)


def ui_cfg(cfg: dict) -> dict:
    u = section(cfg, "ui")
    host = str(u.get("host") or "127.0.0.1")
    if host not in LOOPBACK:
        log(f"ui.host {host!r} is not loopback — using 127.0.0.1 (this UI is local-only)")
        host = "127.0.0.1"
    return {
        "host": "127.0.0.1" if host == "localhost" else host,
        "port": int(u.get("port") or DEFAULT_PORT),
        "poll_ms": int(u.get("poll_ms") or 2500),
        "results_limit": int(u.get("results_limit") or 200),
        "min_free_gb": float(u.get("min_free_gb") if u.get("min_free_gb") is not None else 2),
    }


def inbox_dir(cfg: dict) -> Path:
    return abs_path(cfg.get("inbox_dir") or "inbox")


def logs_dir(cfg: dict) -> Path:
    return abs_path(cfg.get("logs_dir") or "logs")


def dry_run_dir(cfg: dict) -> Path:
    return abs_path(section(cfg, "sidecar").get("dry_run_dir") or "logs/dry-run")


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


# ------------------------------------------------------------ filenames ----

def sanitize_filename(name: str) -> str:
    """Browser-supplied name -> safe single path component for inbox/ (raises ValueError).

    Drops any directory part (no traversal), control chars, ExFAT-illegal chars (\\ / : * ? " < > |),
    leading dots (no hidden / ._ / .uploading- names, no '.' or '..'), and caps the length."""
    s = unicodedata.normalize("NFC", str(name or ""))
    s = s.replace("\\", "/").split("/")[-1]
    s = "".join(ch for ch in s if ch >= " " and ch != "\x7f")
    s = EXFAT_BAD.sub("-", s).strip()
    s = s.lstrip(". ").rstrip(". ")
    if not s:
        raise ValueError(f"unusable filename: {name!r}")
    stem, ext = os.path.splitext(s)
    if len(ext) > 16:
        stem, ext = s, ""
    while len((stem + ext).encode("utf-8")) > MAX_NAME_BYTES and stem:
        stem = stem[:-1]
    stem = stem.rstrip(". ")
    if not stem:
        raise ValueError(f"unusable filename: {name!r}")
    return stem + ext


def is_video_name(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in MEDIA_EXTS  # video or photo


def name_taken(directory: Path, name: str, reserved: set[str] | None = None) -> bool:
    reserved = RESERVED if reserved is None else reserved
    return (
        name.lower() in reserved
        or (directory / name).exists()
        or (directory / (TEMP_PREFIX + name)).exists()
    )


def unique_name(directory: Path, name: str, reserved: set[str] | None = None) -> str:
    """name, or name_2.ext, name_3.ext … — never an existing file (case-insensitive like ExFAT)."""
    if not name_taken(directory, name, reserved):
        return name
    stem, ext = os.path.splitext(name)
    for n in range(2, 10000):
        cand = f"{stem}_{n}{ext}"
        if not name_taken(directory, cand, reserved):
            return cand
    raise ValueError(f"no free name for {name}")


def upload_plan(directory: Path, raw_name: str, size: int | None) -> dict:
    """Decide where an upload goes: action ok | rename | duplicate (same name + same size already in inbox)."""
    name = sanitize_filename(raw_name)
    existing = directory / name
    if existing.is_file():
        if size is not None and existing.stat().st_size == size:
            return {"action": "duplicate", "name": name, "requested": raw_name,
                    "message": f"{name} is already in inbox/ (same size) — skipped, nothing overwritten"}
    final = unique_name(directory, name)
    return {"action": "ok" if final == name else "rename", "name": final, "requested": raw_name,
            "sanitized": name != raw_name, "video": is_video_name(final),
            "message": "" if final == name else f"{name} already exists — will save as {final} (nothing overwritten)"}


def cleanup_temp_uploads(directory: Path) -> list[str]:
    """Remove leftover .uploading-* temps (only ever created by this UI) from an earlier crash."""
    removed = []
    for prefix in (TEMP_PREFIX, "._" + TEMP_PREFIX):
        for p in directory.glob(prefix + "*"):
            try:
                p.unlink()
                removed.append(p.name)
            except OSError:
                pass
    return removed


# ---------------------------------------------------------------- inbox ----

def scan_dir(d: Path, cfg: dict, index: dict, report: dict) -> dict:
    """One directory level: visible files with per-video state (sidecars/dotfiles hidden), uploads, subfolder names."""
    entries, uploading, dirs = [], [], []
    with os.scandir(d) as it:
        for e in it:
            if e.is_dir(follow_symlinks=False):
                if not e.name.startswith("."):
                    dirs.append(e.name)
                continue
            if not e.is_file(follow_symlinks=False):
                continue
            if e.name.startswith(TEMP_PREFIX):
                uploading.append({"name": e.name[len(TEMP_PREFIX):], "size": e.stat().st_size})
                continue
            if e.name.startswith("."):
                continue  # dotfiles + ._ AppleDouble
            entries.append((e.name, e.stat()))
    video_stems = {os.path.splitext(n)[0].lower() for n, _ in entries if is_video_name(n)}
    files, hidden = [], 0
    for name, st in entries:
        bare = name[:-4] if name.lower().endswith(".tmp") else name
        if ar.is_sidecar_of_inbox_video(bare, video_stems):
            hidden += 1  # the clip's .json/.md sidecar (or its atomic-write temp)
            continue
        f = {"name": name, "size": st.st_size, "mtime": iso(st.st_mtime), "mtime_epoch": st.st_mtime,
             "video": is_video_name(name)}
        if f["video"]:
            f.update(ar.video_state(d / name, cfg, index, report))
        files.append(f)
    files.sort(key=lambda f: f["name"].lower())
    return {"files": files, "uploading": uploading, "hidden": hidden, "dirs": sorted(dirs, key=str.lower)}


def list_folder(d: Path, cfg: dict, index: dict, report: dict) -> dict:
    """A subfolder of inbox/ (e.g. a batch folder) as a group: files + counts. Never processed by the pipeline."""
    s = scan_dir(d, cfg, index, report)
    states: dict[str, int] = {}
    for f in s["files"]:
        if not f["video"]:
            continue
        if f.get("state") == "pending":  # not in the log / no generated name: the pipeline won't touch it here
            f["state"], f["detail"] = "not processed", SUBFOLDER_DETAIL
            f.pop("proposed", None)
        states[f["state"]] = states.get(f["state"], 0) + 1
    st = d.stat()
    return {"name": d.name, "count": len(s["files"]), "video_count": sum(f["video"] for f in s["files"]),
            "total_bytes": sum(f["size"] for f in s["files"]), "states": states, "files": s["files"],
            "subfolders": len(s["dirs"]), "sidecars_hidden": s["hidden"], "mtime": iso(st.st_mtime)}


def list_inbox(cfg: dict) -> dict:
    d = inbox_dir(cfg)
    index, report = ar.log_index(cfg), ar.latest_report(cfg)
    s = scan_dir(d, cfg, index, report)
    files, uploading, hidden = s["files"], s["uploading"], s["hidden"]
    folders = []
    for name in s["dirs"]:
        try:
            folders.append(list_folder(d / name, cfg, index, report))
        except OSError as e:
            folders.append({"name": name, "error": str(e), "count": 0, "video_count": 0, "total_bytes": 0,
                            "states": {}, "files": [], "subfolders": 0})
    total = sum(f["size"] for f in files)
    usage = shutil.disk_usage(d)
    states = {"pending": 0, "renamed": 0, "needs review": 0}
    for f in files:
        if f["video"]:
            states[f.get("state", "pending")] = states.get(f.get("state", "pending"), 0) + 1
    # processed (report entry or sidecar) but not applied yet -> what "Apply proposed names" would act on
    applicable = sum(1 for f in files if f["video"] and f.get("state") == "pending" and "proposed" in f)
    return {"dir": str(d), "count": len(files), "video_count": sum(f["video"] for f in files), "total_bytes": total,
            "files": files, "uploading": uploading, "free_bytes": usage.free, "states": states,
            "pending_count": states["pending"], "applicable_count": applicable, "sidecars_hidden": hidden,
            "folders": folders, "movable_count": len(ar.movable_records(cfg, index))}


# -------------------------------------------------------- batch folder ----

def folder_opts(cfg: dict, opts: dict | None) -> tuple[str | None, bool]:
    """(folder name or None, project_from_folder) from a Start/Apply request body. ValueError = message for the UI."""
    opts = opts if isinstance(opts, dict) else {}
    proj = bool(opts.get("project_from_folder"))
    if not opts.get("use_folder"):
        if proj:
            raise ValueError("“Use folder name as the project” needs “Put renamed clips in a folder” ticked.")
        return None, False
    raw = str(opts.get("folder") or "")
    if not raw.strip():
        raise ValueError("enter a folder name (or untick “Put renamed clips in a folder”)")
    name = ar.folder_path(cfg, raw).name
    if proj and not ar.folder_project_slug(name, cfg):
        raise ValueError(f"“{name}” has no letters or digits to use as the project in filenames")
    return name, proj


def folder_check(cfg: dict, raw: str) -> dict:
    """What a typed folder name becomes: sanitized name, exists?, clips inside, project slug (raises ValueError)."""
    p = ar.folder_path(cfg, raw)
    videos = files = 0
    if p.is_dir():
        with os.scandir(p) as it:
            for e in it:
                if e.is_file(follow_symlinks=False) and not e.name.startswith("."):
                    files += 1
                    videos += is_video_name(e.name)
    return {"ok": True, "name": p.name, "requested": raw, "changed": p.name != str(raw).strip(), "exists": p.is_dir(),
            "video_count": videos, "file_count": files, "project_slug": ar.folder_project_slug(p.name, cfg),
            "path": str(p)}


# ------------------------------------------------------------- pipeline ----

def pipeline_processes() -> list[dict]:
    """Python processes running run_pipeline.py (catches runs that hold no lock, e.g. started before the lock existed)."""
    try:
        out = subprocess.check_output(["ps", "-axo", "pid=,pgid=,command="], text=True, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, OSError):
        return []
    procs = []
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3 or "run_pipeline.py" not in parts[2]:
            continue
        exe = os.path.basename(parts[2].split()[0]).lower()
        if not exe.startswith("python"):
            continue  # skip caffeinate wrapper, shells, editors
        pid = int(parts[0])
        if pid != os.getpid():
            procs.append({"pid": pid, "pgid": int(parts[1]), "command": parts[2][:300]})
    return procs


def run_info(cfg: dict) -> dict:
    state, info, why = lock_state(lock_path(cfg))
    procs = pipeline_processes()
    locked = info if state == "active" else None
    unlocked = [] if locked else procs
    source = None
    if locked:
        source = locked.get("launched_by") or "terminal"
    elif unlocked:
        source = "terminal (no lock)"
    return {
        "active": bool(locked) or bool(unlocked),
        "source": source,
        "lock_state": state,
        "lock_reason": why,
        "lock": locked,
        "unlocked_processes": unlocked,
        "can_stop": bool(locked and locked.get("pgid")),
    }


def read_status(cfg: dict) -> dict:
    p = status_path(cfg)
    for _ in range(3):  # tolerate a concurrent tmp->replace
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"state": "idle", "message": "no status.json yet"}
        except (json.JSONDecodeError, OSError):
            time.sleep(0.05)
    return {"state": "unknown", "message": "status.json unreadable"}


def read_json_file(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def updates_info(cfg: dict) -> dict:
    """GET /api/updates: last check (check_updates.py) + current/last upgrade job (apply_updates.py)."""
    ldir = logs_dir(cfg)
    return {"check": read_json_file(ldir / "updates-state.json"), "job": read_json_file(ldir / "updates-status.json"),
            "active": bool(active_updates(cfg)), "lock": str(updates_lock_path(cfg))}


def check_resolve() -> tuple[bool, str]:
    """(running, detail) via scripts/check_resolve.py (exit code != 0 -> Resolve is running)."""
    r = subprocess.run([sys.executable, str(SCRIPTS / "check_resolve.py")], capture_output=True, text=True, timeout=30)
    return r.returncode != 0, (r.stderr or r.stdout).strip()


def tail_text(path: Path, max_bytes: int = 4000) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(max(0, path.stat().st_size - max_bytes))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def run_log_reason(text: str) -> str:
    """The most useful line of a short run log: the last ERROR/SKIP/Refusing/Another… line, else the last line."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    for ln in reversed(lines):
        if ln.startswith(("ERROR", "SKIP", "Refusing", "Another pipeline run", "DaVinci Resolve", "Traceback")) or "Error" in ln:
            return ln[:400]
    return lines[-1][:400] if lines else ""


def finished_early(cfg: dict, code: int | None, run_log: Path) -> tuple[int, dict]:
    """The pipeline ended before we saw its lock (seconds). Say why in plain words instead of a bare exit code:
    a clean finish (e.g. the only pending clip was unreadable -> needs review) is not an error."""
    log_text = tail_text(run_log)
    st = read_status(cfg)
    msg = str(st.get("message") or "").strip()
    last_err = str(st.get("last_error") or "").strip()
    if code == 0:
        return 200, {"ok": True, "finished": True, "message": "Run finished right away. " + (msg or run_log_reason(log_text)),
                     "log": str(run_log)}
    reason = last_err or run_log_reason(log_text) or msg or "no details in the run log"
    hint = "Another run holds logs/pipeline.lock." if code == 3 and "lock" not in reason.lower() else ""
    return 500, {"error": f"The run stopped right away (exit code {code}): {reason}" + (f" {hint}" if hint else ""),
                 "reason": reason, "status_message": msg, "log": str(run_log), "detail": log_text}


def start_pipeline(cfg: dict, opts: dict | None = None) -> tuple[int, dict]:
    with START_LOCK:
        info = run_info(cfg)
        if info["active"]:
            who = info["lock"] or (info["unlocked_processes"] or [{}])[0]
            return 409, {"error": f"A pipeline run is already active (pid {who.get('pid')}, from {info['source']}). Not starting.",
                         "run": info}
        upd = active_updates(cfg)
        if upd:
            job = read_json_file(logs_dir(cfg) / "updates-status.json")
            pct = f" ({job.get('percent')}% done)" if isinstance(job.get("percent"), int) else ""
            return 409, {"error": f"Model/tool updates are being installed{pct} — Start is available again when they finish.",
                         "updates": job}
        opts = opts if isinstance(opts, dict) else {}
        sources = [str(x) for x in (opts.get("sources") or []) if str(x).strip()]
        origin = "clipgauge" if opts.get("origin") == "clipgauge" else "ui"
        force_dry = bool(opts.get("dry_run"))
        dry = bool(cfg.get("dry_run", True)) or force_dry
        try:
            folder, proj = folder_opts(cfg, opts)
        except ValueError as e:
            return 400, {"error": f"Folder: {e}"}
        if sources and folder:
            return 400, {"error": "Process in place can't be combined with a batch folder."}
        running, detail = check_resolve()
        if running:
            return 409, {"error": "DaVinci Resolve is running — quit Resolve, then start the review.", "detail": detail}
        n_clips = 0
        if sources:
            import inplace
            sc = inplace.sort_cfg(cfg)
            confirm_needed = []
            for src in sources:
                c = inplace.classify(src, cfg, sc)
                if c["status"] == "refused":
                    return 400, {"error": f"{Path(src).name}: {c['reason']}"}
                files, _refused = inplace.media_in(src, cfg, sc)
                n_clips += len(files)
                if c["status"] == "needs_confirm" or any(inplace.classify(f, cfg, sc)["status"] == "needs_confirm"
                                                         for f in files[:200]):
                    confirm_needed.append(src)
            if not n_clips:
                return 400, {"error": "No video or photo files in the chosen file(s)/folder(s)."}
            if confirm_needed and not dry and not opts.get("confirm_resolve"):
                return 409, {"error": "Inside the DaVinci Resolve folder — renaming clips Resolve has imported breaks "
                                      "their links. Confirm first.", "needs_confirm": confirm_needed}
        else:
            inbox = list_inbox(cfg)
            if not inbox["video_count"]:
                return 400, {"error": "No video or photo files in inbox/ — nothing to review."}
            if not inbox["pending_count"]:
                return 400, {"error": "Every clip in inbox/ is already handled (renamed or needs review) — nothing to process."}
            n_clips = inbox["pending_count"]
        ldir = logs_dir(cfg)
        run_log = ldir / f"{'clipgauge' if origin == 'clipgauge' else 'ui'}-run-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
        env = dict(os.environ)
        env.update({"AI_VIDEO_RENAMER_LAUNCHED_BY": "ClipGauge" if origin == "clipgauge" else "ui",
                    "AI_VIDEO_RENAMER_RUN_LOG": str(run_log), "PYTHONUNBUFFERED": "1"})
        env.setdefault("OLLAMA_MODELS", str(abs_path(cfg.get("models_dir") or "models")))
        cmd = [sys.executable, "-u", str(SCRIPTS / "run_pipeline.py"), "--all"]
        if force_dry:
            cmd += ["--dry-run"]
        if folder:
            cmd += ["--folder", folder] + (["--project-from-folder"] if proj else [])
        for src in sources:
            cmd += ["--source", src]
        if sources and opts.get("confirm_resolve"):
            cmd += ["--confirm-resolve"]
        if Path("/usr/bin/caffeinate").exists():
            cmd = ["/usr/bin/caffeinate", "-i"] + cmd  # keep the Mac awake for the whole batch
        with open(run_log, "ab") as lf:
            lf.write(f"# started from {'ClipGauge' if origin == 'clipgauge' else 'web UI'} "
                     f"{datetime.now().astimezone().isoformat(timespec='seconds')}: {' '.join(cmd)}\n".encode())
            lf.flush()
            proc = subprocess.Popen(cmd, cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT,
                                    start_new_session=True, close_fds=True, env=env)
        threading.Thread(target=proc.wait, daemon=True).start()  # reap when it ends (if we're still up)
        log(f"start: pid {proc.pid} {' '.join(cmd)} -> {run_log}")
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            state, linfo, _ = lock_state(lock_path(cfg))
            if state == "active" and linfo and linfo.get("pgid") == proc.pid:
                mode = "dry run" if dry else (
                    f"live: confident clips are renamed into inbox/{folder}/" if folder
                    else "live: confident clips are renamed where they are" if sources
                    else "live: confident clips are renamed in place")
                if folder and proj:
                    mode += f", project in filenames: {ar.folder_project_slug(folder, cfg)}"
                tail = "" if origin == "clipgauge" else " Safe to close the browser."
                return 202, {"ok": True, "message": f"Run started ({n_clips} clip(s), {mode}).{tail}",
                             "pid": linfo.get("pid"), "pgid": proc.pid, "log": str(run_log)}
            if proc.poll() is not None:
                return finished_early(cfg, proc.returncode, run_log)
            time.sleep(0.2)
        return 202, {"ok": True, "message": "Started; waiting for the pipeline to report in.", "pgid": proc.pid, "log": str(run_log)}


def apply_proposed(cfg: dict, opts: dict | None = None) -> tuple[int, dict]:
    """Run apply_renames.py (renames in place, or into a batch folder) for clips already processed.
    Refused while any run is active."""
    if cfg.get("dry_run", True):
        return 409, {"error": "config dry_run is true — set \"dry_run\": false in config/config.json to rename. Nothing changed."}
    try:
        folder, _proj = folder_opts(cfg, dict(opts or {}, project_from_folder=False))  # Apply keeps proposed names
    except ValueError as e:
        return 400, {"error": f"Folder: {e}"}
    with START_LOCK:
        info = run_info(cfg)
        if info["active"]:
            who = info["lock"] or (info["unlocked_processes"] or [{}])[0]
            return 409, {"error": f"A pipeline run is active (pid {who.get('pid')}, from {info['source']}) — "
                                  "apply after it finishes (live runs rename as they go)."}
        env = dict(os.environ, AI_VIDEO_RENAMER_LAUNCHED_BY="ui-apply", PYTHONUNBUFFERED="1")
        cmd = [sys.executable, str(SCRIPTS / "apply_renames.py"), "--json"] + (["--folder", folder] if folder else [])
        r = subprocess.run(cmd, cwd=str(ROOT), env=env,
                           capture_output=True, text=True, timeout=1800, stdin=subprocess.DEVNULL)
        out = (r.stdout or "").strip().splitlines()
        try:
            j = json.loads(out[-1]) if out else {}
        except json.JSONDecodeError:
            j = {}
        alog = logs_dir(cfg) / f"ui-apply-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
        try:
            alog.write_text((r.stdout or "") + (r.stderr or ""), encoding="utf-8")
        except OSError:
            pass
        log(f"apply: exit {r.returncode} {j.get('message') or j.get('error') or (r.stderr or '')[-300:]} -> {alog}")
        if r.returncode in (2, 3) or j.get("error"):
            return 409, {"error": j.get("error") or (r.stderr or "refused").strip()[-600:]}
        if not j:
            return 500, {"error": f"apply_renames.py exit {r.returncode}", "detail": (r.stderr or "")[-1500:]}
        return 200, {"ok": r.returncode == 0, "message": j.get("message"), "counts": j.get("counts"), "log": str(alog)}


def move_into_folder(cfg: dict, opts: dict | None = None) -> tuple[int, dict]:
    """Run apply_renames.py --move-into NAME: clips already renamed and still at inbox/ top level (per the rename log)
    move into inbox/NAME/ with their sidecars. Refused while any run is active or when dry_run is true."""
    if cfg.get("dry_run", True):
        return 409, {"error": "config dry_run is true — moving files is off in dry-run mode. Nothing changed."}
    raw = str((opts or {}).get("folder") or "")
    try:
        folder = ar.folder_path(cfg, raw).name
    except ValueError as e:
        return 400, {"error": f"Folder: {e}"}
    with START_LOCK:
        info = run_info(cfg)
        if info["active"]:
            who = info["lock"] or (info["unlocked_processes"] or [{}])[0]
            return 409, {"error": f"A pipeline run is active (pid {who.get('pid')}, from {info['source']}) — "
                                  "move clips after it finishes."}
        env = dict(os.environ, AI_VIDEO_RENAMER_LAUNCHED_BY="ui-move", PYTHONUNBUFFERED="1")
        r = subprocess.run([sys.executable, str(SCRIPTS / "apply_renames.py"), "--move-into", folder, "--json"],
                           cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=1800, stdin=subprocess.DEVNULL)
        out = (r.stdout or "").strip().splitlines()
        try:
            j = json.loads(out[-1]) if out else {}
        except json.JSONDecodeError:
            j = {}
        mlog = logs_dir(cfg) / f"ui-move-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
        try:
            mlog.write_text((r.stdout or "") + (r.stderr or ""), encoding="utf-8")
        except OSError:
            pass
        log(f"move-into-folder {folder!r}: exit {r.returncode} {j.get('message') or j.get('error') or (r.stderr or '')[-300:]} -> {mlog}")
        if r.returncode in (2, 3) or j.get("error"):
            return 409, {"error": j.get("error") or (r.stderr or "refused").strip()[-600:]}
        if not j:
            return 500, {"error": f"apply_renames.py --move-into exit {r.returncode}", "detail": (r.stderr or "")[-1500:]}
        return 200, {"ok": r.returncode == 0, "message": j.get("message"), "counts": j.get("counts"), "folder": folder,
                     "log": str(mlog)}


# ---------------------------------------------------------------- Sort into Projects (v0.4)
def sort_cli(cfg: dict, args: list[str], timeout: int = 1800) -> tuple[int, dict]:
    """Run scripts/sort_projects.py ... --json in a subprocess (so its lock carries the sort_projects marker)."""
    env = dict(os.environ, AI_VIDEO_RENAMER_LAUNCHED_BY="ui-sort", PYTHONUNBUFFERED="1")
    r = subprocess.run([sys.executable, str(SCRIPTS / "sort_projects.py"), *args, "--json"], cwd=str(ROOT), env=env,
                       capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    lines = (r.stdout or "").strip().splitlines()
    try:
        j = json.loads(lines[-1]) if lines else {}
    except json.JSONDecodeError:
        j = {}
    if not isinstance(j, dict) or not j:
        return 500, {"error": f"sort_projects.py exit {r.returncode}", "detail": ((r.stderr or "") + (r.stdout or ""))[-1500:]}
    if r.returncode != 0 or j.get("ok") is False:
        return (409 if r.returncode in (1, 3, 4) else 400), {"error": j.get("error") or f"exit {r.returncode}"}
    return 200, j


# ---------------------------------------------------- custom instructions ----

def instructions_info(cfg: dict) -> dict:
    d = ins.load(cfg)
    act = ins.resolve(cfg, data=d)
    return {**d, "hash": act["hash"], "active": act["active"], "limits": ins.LIMITS, "path": str(ins.path(cfg)),
            "whisper_prompt": ins.whisper_prompt(act)}


def instructions_summary(cfg: dict) -> dict:
    """Small 'Instructions active' block for /api/status (polled)."""
    try:
        d = ins.load(cfg)
        act = ins.resolve(cfg, data=d)
    except Exception as e:  # noqa: BLE001
        return {"active": False, "error": str(e)}
    return {"active": act["active"], "hash": act["hash"], "standing": bool(d["standing"]),
            "next_run": bool(d["next_run"]["text"]), "keep": d["next_run"]["keep"], "glossary_terms": len(d["glossary"])}


def instructions_draft(body: dict) -> dict:
    nr = body.get("next_run") if isinstance(body.get("next_run"), dict) else {}
    return {"standing": str(body.get("standing") or ""), "glossary": body.get("glossary") or "",
            "next_run": {"text": str(nr.get("text") or ""), "keep": bool(nr.get("keep"))}}


def save_instructions(cfg: dict, body: dict) -> tuple[int, dict]:
    try:
        ins.save(cfg, instructions_draft(body))
    except ValueError as e:
        return 400, {"error": str(e)}
    log("instructions saved")
    return 200, {"ok": True, **instructions_info(cfg)}


def preview_instructions(cfg: dict, body: dict) -> tuple[int, dict]:
    d = instructions_draft(body)
    errs = ins.validate(d)
    if errs:
        return 400, {"error": " ".join(errs)}
    data = {**ins.empty(), "standing": ins.sanitize(d["standing"], ins.LIMITS["standing_chars"]),
            "glossary": ins.parse_glossary(d["glossary"]),
            "next_run": {"text": ins.sanitize(d["next_run"]["text"], ins.LIMITS["next_run_chars"]), "keep": d["next_run"]["keep"]}}
    act = ins.resolve(cfg, data=data)
    return 200, {"ok": True, **ins.preview(cfg, act, "photo" if body.get("photo") else "video")}


def sort_plan(cfg: dict, body: dict) -> tuple[int, dict]:
    src = str(body.get("source") or "").strip()
    if not src:
        return 400, {"error": "Pick a source folder"}
    args = ["--source", src, "--dry-run"]
    gap = body.get("gap_minutes")
    if isinstance(gap, (int, float)) and 1 <= gap <= 24 * 60:
        args += ["--gap-minutes", str(int(gap))]
    return sort_cli(cfg, args, timeout=900)


def sort_apply(cfg: dict, body: dict) -> tuple[int, dict]:
    if body.get("confirm") is not True:
        return 400, {"error": "Apply needs \"confirm\": true (review the dry run first)"}
    plans = (logs_dir(cfg) / "sort-plans").resolve()
    pf = Path(str(body.get("plan_file") or "")).expanduser()
    try:
        pf = pf.resolve()
        pf.relative_to(plans)
    except (OSError, ValueError):
        return 400, {"error": "plan_file must be a dry-run plan in logs/sort-plans/"}
    if not pf.is_file():
        return 404, {"error": "Plan not found — run the dry run again"}
    args = ["--apply", "--plan", str(pf), "--yes"]
    edits = body.get("edits")
    tmp = None
    try:
        if isinstance(edits, dict) and edits:
            tmp = plans / f".edits-{os.getpid()}-{int(time.time() * 1000)}.json"
            tmp.write_text(json.dumps(edits, ensure_ascii=False), encoding="utf-8")
            args += ["--edits", str(tmp)]
        with START_LOCK:
            code, j = sort_cli(cfg, args)
    finally:
        if tmp:
            tmp.unlink(missing_ok=True)
    log(f"sort apply {pf.name}: {code} {j.get('error') or ('moved ' + str(j.get('moved')))}")
    return code, j


def sort_undo(cfg: dict, body: dict) -> tuple[int, dict]:
    if body.get("confirm") is not True:
        return 400, {"error": "Undo needs \"confirm\": true"}
    args = ["--undo", "--yes"]
    pid = str(body.get("plan_id") or "").strip()
    if pid:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", pid):
            return 400, {"error": "bad plan_id"}
        args += ["--plan-id", pid]
    with START_LOCK:
        code, j = sort_cli(cfg, args)
    log(f"sort undo {pid or '(latest)'}: {code} {j.get('error') or ('restored ' + str(j.get('restored')))}")
    return code, j


def stop_pipeline(cfg: dict) -> tuple[int, dict]:
    state, info, why = lock_state(lock_path(cfg))
    if state != "active" or not info:
        procs = pipeline_processes()
        if procs:
            return 409, {"error": f"A run without a lock is active (pid {procs[0]['pid']}). Stop it with Ctrl-C in its Terminal."}
        return 409, {"error": f"No pipeline run to stop ({why})."}
    pid, pgid = info.get("pid"), info.get("pgid")
    if not isinstance(pid, int) or not isinstance(pgid, int) or pgid <= 1:
        return 409, {"error": "Lock has no usable process group — stop it from its Terminal (Ctrl-C)."}
    try:
        actual = os.getpgid(pid)
    except ProcessLookupError:
        return 409, {"error": f"pid {pid} already exited."}
    if actual != pgid:
        return 409, {"error": f"pid {pid} is no longer in process group {pgid} — not signalling."}
    if pgid == os.getpgid(0):
        return 409, {"error": "Refusing: that process group includes this web server."}
    os.killpg(pgid, signal.SIGTERM)
    log(f"stop: SIGTERM -> process group {pgid} (pipeline pid {pid}, from {info.get('launched_by')})")
    return 200, {"ok": True, "message": f"Stop requested (SIGTERM to pipeline process group {pgid}). Finished clips stay in the report."}


# ---------------------------------------------------- transcript bundle ----

def list_exports(cfg: dict, limit: int = 12) -> list[dict]:
    d = tb.exports_dir(cfg)
    if not d.is_dir():
        return []
    out = []
    with os.scandir(d) as it:
        for e in it:
            if e.is_file(follow_symlinks=False) and EXPORT_NAME_RE.match(e.name):
                st = e.stat()
                out.append({"name": e.name, "size": st.st_size, "mtime": iso(st.st_mtime), "mtime_epoch": st.st_mtime,
                            "url": "/exports/" + urllib.parse.quote(e.name)})
    out.sort(key=lambda x: x["mtime_epoch"], reverse=True)
    return out[:limit]


def transcript_scopes(cfg: dict) -> dict:
    """Dropdown entries for the bundle: all of inbox/, each inbox subfolder, other Lexar folders with clip sidecars."""
    inbox = inbox_dir(cfg)
    found = tb.discover_folders(cfg)
    n_inbox = sum(f["clips"] for f in found if f["kind"] == "inbox")
    scopes = [{"value": "inbox", "label": f"All of inbox/ (incl. subfolders) — {n_inbox} clip(s)", "count": n_inbox}]
    for f in found:
        d = Path(f["dir"])
        if f["kind"] == "inbox" and d.parent == inbox:
            scopes.append({"value": "folder:" + d.name, "label": f"inbox/{d.name}/ — {f['clips']} clip(s)",
                           "count": f["clips"]})
    others = [f for f in found if f["kind"] == "lexar" and Path(f["dir"]).is_dir()]
    for f in others:
        scopes.append({"value": "path:" + f["rel"], "label": f"{f['rel']}/ — {f['clips']} clip(s)", "count": f["clips"]})
    if others or any(f["kind"] == "other" for f in found):
        total = sum(f["clips"] for f in found)
        scopes.append({"value": "everywhere", "label": f"Everything (every clip in the notes store + sidecars in inbox/, "
                                                       f"{', '.join(tb.search_roots(cfg))}) — {total} clip(s)", "count": total})
    return {"scopes": scopes, "exports": list_exports(cfg), "exports_dir": str(tb.exports_dir(cfg)),
            "run_active": run_info(cfg)["active"]}


def build_transcript_bundle(cfg: dict, opts: dict | None = None) -> tuple[int, dict]:
    """Run build_transcript_bundle.py --json for the chosen scope. Read-only for media, so allowed during a run:
    the pipeline writes sidecars atomically (temp + replace), a half-written sidecar is never visible."""
    opts = opts if isinstance(opts, dict) else {}
    scope = str(opts.get("scope") or "inbox").strip()
    cmd = [sys.executable, str(SCRIPTS / "build_transcript_bundle.py"), "--json"]
    if scope.startswith("folder:"):
        cmd += ["--folder", scope[len("folder:"):]]
    elif scope.startswith("path:"):
        cmd += ["--path", scope[len("path:"):]]
    elif scope == "everywhere":
        cmd += ["--everywhere"]
    elif scope != "inbox":
        return 400, {"error": f"unknown scope {scope!r}"}
    date = str(opts.get("date") or "").strip()
    if date:
        cmd += ["--date", date]
    if opts.get("include_silent"):
        cmd += ["--include-silent"]
    if opts.get("exclude_photos"):
        cmd += ["--exclude-photos"]
    if not BUNDLE_LOCK.acquire(blocking=False):
        return 409, {"error": "A transcript bundle is already being built — try again in a moment."}
    try:
        r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL)
    finally:
        BUNDLE_LOCK.release()
    out = (r.stdout or "").strip().splitlines()
    try:
        j = json.loads(out[-1]) if out else {}
    except json.JSONDecodeError:
        j = {}
    log(f"transcript bundle {scope!r}: exit {r.returncode} {j.get('message') or j.get('error') or (r.stderr or '')[-300:]}")
    if r.returncode == 2 or j.get("error"):
        return 400, {"error": j.get("error") or (r.stderr or "refused").strip()[-600:]}
    if r.returncode != 0 or not j:
        return 500, {"error": f"build_transcript_bundle.py exit {r.returncode}", "detail": (r.stderr or "")[-1500:]}
    for k in ("json", "md"):
        if j.get(k):
            j[k + "_url"] = "/exports/" + urllib.parse.quote(Path(j[k]).name)
            j[k + "_name"] = Path(j[k]).name
    if run_info(cfg)["active"]:
        j["note"] = "A pipeline run is active — clips still being processed are not in this bundle yet."
    return 200, j


def export_file(cfg: dict, name: str) -> tuple[int, Path | str]:
    """(200, path) for a transcripts-*.json/.md directly inside exports/, else (code, message). No other file is served."""
    if not EXPORT_NAME_RE.match(name or ""):
        return 403, "Only transcripts-*.json / .md files in exports/ can be downloaded."
    d = os.path.realpath(tb.exports_dir(cfg))
    p = os.path.realpath(os.path.join(d, name))
    if os.path.dirname(p) != d:
        return 403, "Only files directly inside exports/ can be downloaded."
    if not os.path.isfile(p):
        return 404, "Export not found."
    return 200, Path(p)


# ------------------------------------------------------------ settings ----

def set_write_next_to_clip(value: bool, config_path: Path | None = None) -> tuple[int, dict]:
    """Persist config.json sidecar.write_next_to_clip (atomic rewrite; every other key kept as is)."""
    path = Path(config_path or CONFIG_PATH)
    raw = json.loads(path.read_text(encoding="utf-8"))
    side = raw.get("sidecar") if isinstance(raw.get("sidecar"), dict) else {}
    side["write_next_to_clip"] = bool(value)
    raw["sidecar"] = side
    atomic_write_text(path, json.dumps(raw, indent=2) + "\n")
    log(f"settings: sidecar.write_next_to_clip = {bool(value)}")
    return 200, {"ok": True, "write_next_to_clip": bool(value),
                 "message": ("New clips also get .json/.md notes next to them" if value else
                             "No .json/.md files next to clips — notes go to the central store only")
                 + " (from the next run; the notes store is always written)."}


def note_markdown(cfg: dict, clip_id: str) -> tuple[int, str]:
    """A clip's notes from the central store, rendered like a .md sidecar."""
    from sidecar import build_markdown  # local: sidecar imports _common only

    if not ns.ID_RE.match(clip_id or ""):
        return 400, "Bad clip id."
    rec = ns.load(cfg, clip_id)
    if not rec:
        return 404, "No notes for that clip in the store."
    return 200, build_markdown(rec) + f"\n_Notes store: {ns.record_path(cfg, clip_id)} · now at `{rec.get('current_path')}`_\n"


# -------------------------------------------------------------- results ----

def read_results(cfg: dict, limit: int = 200, dedupe: bool = True) -> dict:
    path = dry_run_dir(cfg) / "report.jsonl"
    if not path.exists():
        return {"path": str(path), "count": 0, "items": []}
    size = path.stat().st_size
    max_bytes = 8 * 1024 * 1024
    with open(path, "rb") as f:
        f.seek(max(0, size - max_bytes))
        raw = f.read().decode("utf-8", "replace").splitlines()
    if size > max_bytes and raw:
        raw = raw[1:]  # partial first line
    index = ar.log_index(cfg)
    by_source = index["store"]["by_source"]
    items, seen = [], set()
    for line in reversed(raw):
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        src = str(r.get("source") or "")
        if dedupe:
            if src in seen:
                continue
            seen.add(src)
        sj = r.get("sidecar_json")
        k = ar.norm(src) if src else ""
        ren, rev = index["renamed_orig"].get(k), index["review"].get(k)
        if ren:
            current = ar.current_path(index, ren)
            cp = Path(current)
            shown = cp.name if ar.in_inbox(cp, cfg) else f"{cp.parent.name}/{cp.name}"  # batch folder: Folder/name
            state, status = f"renamed → {shown}", "renamed"
        elif rev:
            state, current, status = "needs review (name kept)", src, "needs review"
        elif r.get("action") and r.get("action") != "dry-run (not renamed)":
            state, current, status = str(r["action"]), r.get("final_path") or src, "skipped"
        else:
            state, current = ("dry run — not applied" if r.get("dry_run") else "not applied"), src
            status = "dry run" if r.get("dry_run") else "skipped"
        lrec = ren or rev or {}
        if sj and not os.path.isfile(sj) and current:  # sidecar moved next to the (renamed) clip
            cand = str(Path(current).with_suffix(".json"))
            sj = cand if os.path.isfile(cand) else sj
        md = str(Path(sj).with_suffix(".md")) if sj else None
        items.append({
            "state": state,
            "current": current,
            "time": r.get("time"),
            "source": src,
            "name": os.path.basename(src),
            "folder": os.path.basename(os.path.dirname(src)),
            "proposed": r.get("proposed"),
            "clip_type": r.get("clip_type"),
            "confidence": r.get("confidence"),
            "needs_review": bool(r.get("needs_review")),
            "review_reasons": r.get("review_reasons") or [],
            "sidecar_json": sj if sj and os.path.isfile(sj) else None,
            "note_id": r.get("note_id") or (by_source.get(ar.norm(src)) or {}).get("clip_id") if src else r.get("note_id"),
            "has_md": bool(md and os.path.isfile(md)),
            "seconds": (r.get("timing_s") or {}).get("total"),
            "dry_run": r.get("dry_run"),
            "status": status,  # renamed | needs review | skipped | dry run (ClipGauge results table)
            "log_id": lrec.get("id"),
            "batch": lrec.get("batch"),
            "in_place": bool(lrec.get("in_place")),
            "unreadable": bool(r.get("unreadable")),
            "action_reason": r.get("action_reason"),
        })
        if len(items) >= limit:
            break
    return {"path": str(path), "count": len(items), "items": items}


def sidecar_md(cfg: dict, json_path: str) -> tuple[int, str]:
    bases = [os.path.realpath(dry_run_dir(cfg)), os.path.realpath(inbox_dir(cfg))]
    md = os.path.realpath(str(Path(json_path).with_suffix(".md")))
    if not md.endswith(".md") or not any(os.path.commonpath([b, md]) == b for b in bases):
        return 403, "Only sidecars inside logs/dry-run/ or inbox/ can be shown."
    try:
        with open(md, encoding="utf-8", errors="replace") as f:
            return 200, f.read(1024 * 1024)
    except FileNotFoundError:
        return 404, "Sidecar .md not found."
