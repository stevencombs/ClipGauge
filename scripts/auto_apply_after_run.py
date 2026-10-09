#!/usr/bin/env python3
"""
Unattended finish: wait until the current pipeline run ends (logs/pipeline.lock gone / its pid dead), then
  (a) apply_renames.py — rename in place everything already processed (needs config dry_run false), and
  (b) run_pipeline.py --all once more in live mode (takes the lock normally) to pick up clips still pending in
      inbox/ (ones that errored, or files dropped in later). Already renamed / needs-review clips are skipped.
  (c) cleanup_processing.py — remove processing/ leftovers of every renamed clip.
  With --cleanup-only it skips (a) and (b): wait, then sweep processing/ only.

  python3 scripts/auto_apply_after_run.py --launch [--wait-pid PID]   # detach: new session + caffeinate -i
      log: logs/auto-apply-<timestamp>.log ; prints the waiter pid
  python3 scripts/auto_apply_after_run.py --wait-pid PID              # foreground (what --launch runs)
Safety: if config dry_run is true again when the wait ends, it renames nothing and exits.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import ROOT, abs_path, load_config, status_path  # noqa: E402
from pipeline_lock import lock_path, lock_state, pid_alive  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent


def log(msg: str) -> None:
    print(f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] {msg}", flush=True)


def proc_running(pid: int | None) -> bool:
    """Alive and not a zombie."""
    if not pid or not pid_alive(pid):
        return False
    try:
        st = subprocess.check_output(["ps", "-o", "stat=", "-p", str(pid)], text=True, stderr=subprocess.DEVNULL).strip()
    except (subprocess.CalledProcessError, OSError):
        return False
    return bool(st) and not st.startswith("Z")


def status_line() -> str:
    try:
        s = json.loads(status_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "status.json unreadable"
    return (f"state={s.get('state')} clip {s.get('clip_index')}/{s.get('clip_total')} step={s.get('step')} "
            f"eta={s.get('eta')} file={Path(str(s.get('current_file') or '-')).name}")


def wait_for_idle(lock: Path, wait_pid: int | None, poll: float, deadline: float, label: str) -> bool:
    last_note = 0.0
    while True:
        state, info, why = lock_state(lock)
        busy_pid = proc_running(wait_pid)
        if state != "active" and not busy_pid:
            log(f"{label}: no active run (lock {state}: {why}; pid {wait_pid} running={busy_pid})")
            return True
        if time.time() > deadline:
            log(f"{label}: gave up waiting (max wait reached); lock={state} ({why})")
            return False
        if time.time() - last_note > 600:
            holder = (info or {}).get("pid")
            log(f"{label}: waiting — lock {state} (pid {holder}); {status_line()}")
            last_note = time.time()
        time.sleep(poll)


def run_step(cmd: list[str], env: dict, lock: Path, poll: float, deadline: float, name: str) -> int:
    for attempt in range(1, 4):
        log(f"{name}: running {' '.join(cmd)} (attempt {attempt})")
        rc = subprocess.call(cmd, cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL)
        log(f"{name}: exit code {rc}")
        if rc != 3:  # 3 = lock held by another run -> wait and retry
            return rc
        if not wait_for_idle(lock, None, poll, deadline, f"{name} retry"):
            return rc
    return 3


def worker(args: argparse.Namespace) -> int:
    log(f"{'auto-cleanup' if args.cleanup_only else 'auto-apply'} waiter started (pid {os.getpid()}, pgid {os.getpgid(0)}, sid {os.getsid(0)}); "
        f"waiting for pid {args.wait_pid} / {lock_path()}")
    log(f"now: {status_line()}")
    deadline = time.time() + args.max_wait_h * 3600
    lock = lock_path()
    if not wait_for_idle(lock, args.wait_pid, args.poll, deadline, "wait"):
        return 4
    log(f"pipeline finished: {status_line()}")
    time.sleep(3)
    cfg = load_config()
    py = sys.executable
    if args.cleanup_only:
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        rc = run_step([py, "-u", str(SCRIPTS / "cleanup_processing.py")], env, lock, args.poll, deadline, "cleanup")
        log(f"finished (cleanup only): rc={rc}")
        return rc
    if cfg.get("dry_run", True):
        log("config dry_run is true — renaming nothing, not starting a live run. Exiting.")
        return 0
    env = dict(os.environ, PYTHONUNBUFFERED="1", AI_VIDEO_RENAMER_LAUNCHED_BY="auto-apply",
               AI_VIDEO_RENAMER_RUN_LOG=str(args.log or ""))
    env.setdefault("OLLAMA_MODELS", str(abs_path(cfg.get("models_dir") or "models")))
    env["PATH"] = env.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
    rc_a = run_step([py, "-u", str(SCRIPTS / "apply_renames.py")], env, lock, args.poll, deadline, "apply")
    if args.no_pipeline:
        log("--no-pipeline: done.")
        return rc_a
    rc_p = run_step([py, "-u", str(SCRIPTS / "run_pipeline.py"), "--all"], env, lock, args.poll, deadline, "pipeline (live)")
    rc_c = run_step([py, "-u", str(SCRIPTS / "cleanup_processing.py")], env, lock, args.poll, deadline, "cleanup")
    log(f"finished: apply rc={rc_a}, live pipeline rc={rc_p}, cleanup rc={rc_c}. {status_line()}")
    log("Undo if needed: python3 scripts/undo_renames.py --all --dry-run")
    return 0 if rc_a in (0, 1) and rc_p in (0,) else 1


def launch(args: argparse.Namespace) -> int:
    logs = abs_path(load_config().get("logs_dir") or "logs")
    kind = "auto-cleanup" if args.cleanup_only else "auto-apply"
    logf = Path(args.log) if args.log else logs / f"{kind}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    cmd = [sys.executable, "-u", str(Path(__file__).resolve()), "--log", str(logf), "--poll", str(args.poll),
           "--max-wait-h", str(args.max_wait_h)]
    if args.wait_pid:
        cmd += ["--wait-pid", str(args.wait_pid)]
    if args.no_pipeline:
        cmd.append("--no-pipeline")
    if args.cleanup_only:
        cmd.append("--cleanup-only")
    if Path("/usr/bin/caffeinate").exists():
        cmd = ["/usr/bin/caffeinate", "-i"] + cmd
    with open(logf, "ab") as lf:
        lf.write(f"# launched {datetime.now().astimezone().isoformat(timespec='seconds')}: {' '.join(cmd)}\n".encode())
        lf.flush()
        p = subprocess.Popen(cmd, cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT,
                             start_new_session=True, close_fds=True)
    print(json.dumps({"pid": p.pid, "log": str(logf), "cmd": cmd}))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Wait for the current pipeline run, then apply renames and run once more (live)")
    ap.add_argument("--launch", action="store_true", help="Start detached (new session, caffeinate -i) and return")
    ap.add_argument("--wait-pid", type=int, default=None, help="Also wait for this pid to exit")
    ap.add_argument("--poll", type=float, default=20.0)
    ap.add_argument("--max-wait-h", type=float, default=12.0)
    ap.add_argument("--log", default=None)
    ap.add_argument("--no-pipeline", action="store_true", help="Only apply; skip the second live run")
    ap.add_argument("--cleanup-only", action="store_true", help="After the run ends, only sweep processing/ (cleanup_processing.py)")
    args = ap.parse_args()
    return launch(args) if args.launch else worker(args)


if __name__ == "__main__":
    raise SystemExit(main())
