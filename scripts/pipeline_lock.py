#!/usr/bin/env python3
"""
Single-run lock for run_pipeline.py: logs/pipeline.lock (JSON: pid, pgid, host, argv, started_at, launched_by, log).

A Terminal run and a web-UI run share one status.json, so only one may run at a time.
apply_renames.py / undo_renames.py / cleanup_processing.py take the same lock, so renames never overlap a pipeline run.
The lock is created atomically (O_CREAT|O_EXCL). It is STALE (and silently replaced) when:
  - the pid is no longer alive, or
  - the pid is alive but is not a run_pipeline process (pid reuse after a crash/reboot), or
  - it was written on another host (the Lexar can only be mounted on one Mac at a time), or
  - it is empty/unparseable and older than 30 s.
ExFAT: no symlinks, no hard links — plain file only.
  python3 scripts/pipeline_lock.py               # show lock state (exit 1 if a run is active)
  python3 scripts/pipeline_lock.py --clear-stale # remove a stale lock (never an active one)
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import abs_path, load_config  # noqa: E402

PIPELINE_MARKER = "run_pipeline"
# Scripts that may hold the lock (rename/undo also take it so they never overlap a pipeline run).
LOCK_MARKERS = (PIPELINE_MARKER, "apply_renames", "undo_renames", "cleanup_processing", "sort_projects")
EMPTY_LOCK_GRACE_S = 30.0


def lock_path(cfg: dict | None = None) -> Path:
    cfg = cfg or load_config()
    return abs_path(cfg.get("logs_dir") or "logs") / "pipeline.lock"


def pid_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


def pid_command(pid: int) -> str:
    """Full command line of pid ('' if gone / ps unavailable)."""
    try:
        return subprocess.check_output(["ps", "-p", str(pid), "-o", "command="], text=True, stderr=subprocess.DEVNULL).strip()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return ""


def read_lock(path: Path) -> dict | None:
    """Parsed lock dict, {} if present but empty/unparseable, None if absent."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return {}
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def lock_state(path: Path, marker: str | tuple | None = None) -> tuple[str, dict | None, str]:
    """('absent'|'active'|'stale', info, reason). marker: str/tuple of command substrings (default LOCK_MARKERS)."""
    markers = LOCK_MARKERS if marker is None else ((marker,) if isinstance(marker, str) else tuple(marker))
    info = read_lock(path)
    if info is None:
        return "absent", None, "no lock file"
    if not info:
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            return "absent", None, "lock vanished"
        if age < EMPTY_LOCK_GRACE_S:
            return "active", info, "lock being written"
        return "stale", info, f"unparseable lock ({age:.0f}s old)"
    host = info.get("host")
    if host and host != socket.gethostname():
        return "stale", info, f"written on another host ({host})"
    pid = info.get("pid")
    if not isinstance(pid, int) or not pid_alive(pid):
        return "stale", info, f"pid {pid} is not running"
    cmd = pid_command(pid)
    if cmd and not any(m in cmd for m in markers):
        return "stale", info, f"pid {pid} is not a pipeline process ({cmd[:80]})"
    return "active", info, f"pid {pid} running"


UPDATES_MARKER = "apply_updates"


def updates_lock_path(cfg: dict | None = None) -> Path:
    """logs/updates.lock — held by apply_updates.py while models/tools are being upgraded."""
    return lock_path(cfg).parent / "updates.lock"


def active_updates(cfg: dict | None = None) -> dict | None:
    """The running update job's lock info, or None (processing must not start while one is active)."""
    state, info, _ = lock_state(updates_lock_path(cfg), UPDATES_MARKER)
    return info if state == "active" else None


def active_lock(path: Path | None = None) -> dict | None:
    path = path or lock_path()
    state, info, _ = lock_state(path)
    return info if state == "active" else None


def acquire_lock(path: Path | None = None, argv: list[str] | None = None, marker: str | tuple | None = None) -> tuple[bool, dict]:
    """Try to take the lock for this process. Returns (ok, info) — info is the holder's lock on failure."""
    path = path or lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    info = {
        "pid": os.getpid(),
        "pgid": os.getpgid(0),
        "host": socket.gethostname(),
        "argv": list(argv if argv is not None else sys.argv),
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "launched_by": os.environ.get("AI_VIDEO_RENAMER_LAUNCHED_BY", "terminal"),
        "log": os.environ.get("AI_VIDEO_RENAMER_RUN_LOG"),
    }
    for _ in range(3):
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            state, holder, _why = lock_state(path, marker)
            if state == "active":
                return False, holder or {}
            try:
                path.unlink()  # stale -> replace
            except FileNotFoundError:
                pass
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(info, indent=2) + "\n")
        return True, info
    return False, read_lock(path) or {}


def release_lock(path: Path | None = None, pid: int | None = None) -> bool:
    """Remove the lock only if it belongs to pid (default: this process)."""
    path = path or lock_path()
    pid = os.getpid() if pid is None else pid
    info = read_lock(path)
    if info and info.get("pid") == pid:
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            pass
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Show/clear the pipeline lock (logs/pipeline.lock)")
    ap.add_argument("--clear-stale", action="store_true", help="Delete the lock if it is stale (never touches an active one)")
    args = ap.parse_args()
    path = lock_path()
    state, info, why = lock_state(path)
    print(f"{path}: {state} — {why}")
    if info:
        print(json.dumps(info, indent=2))
    if args.clear_stale and state == "stale":
        path.unlink(missing_ok=True)
        print("Removed stale lock.")
    return 1 if state == "active" else 0


if __name__ == "__main__":
    raise SystemExit(main())
