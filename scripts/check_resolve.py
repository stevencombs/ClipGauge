#!/usr/bin/env python3
"""Exit non-zero if DaVinci Resolve appears to be running.

Matches on each process's EXECUTABLE path (ps -o comm), not its arguments, so a command that merely mentions a
"/DaVinci Resolve/..." folder (e.g. run_pipeline.py --video '/Volumes/Lexar/DaVinci Resolve/x.JPG', caffeinate,
an editor) is not mistaken for Resolve. This process and its parents are never counted.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys


RESOLVE_NAMES = (
    "DaVinci Resolve",
    "Resolve",
    "com.blackmagic-design.DaVinciResolve",
)


def _own_chain() -> set[int]:
    """This process and its ancestors (caffeinate, the shell, the web UI that launched us…)."""
    own, pid = set(), os.getpid()
    for _ in range(32):
        if pid <= 1 or pid in own:
            break
        own.add(pid)
        try:
            pid = int(subprocess.check_output(["ps", "-o", "ppid=", "-p", str(pid)], text=True,
                                              stderr=subprocess.DEVNULL).strip() or 0)
        except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
            break
    return own


def is_resolve_exe(exe: str) -> bool:
    low = exe.lower()
    return ("davinci resolve" in low or "com.blackmagic-design.davinciresolve" in low
            or os.path.basename(exe) == "Resolve")


def _legacy_scan(own: set[int]) -> list[str]:
    """Fallback when ps can't list executables: the old pgrep scan (minus our own process chain)."""
    try:
        out = subprocess.check_output(["pgrep", "-fl", "Resolve"], text=True, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    hits = []
    for line in out.splitlines():
        parts = line.split(None, 1)
        if not parts or (parts[0].isdigit() and int(parts[0]) in own):
            continue
        low = line.lower()
        if "davinci" in low or ("Resolve" in line and "check_resolve" not in line and "AI-Video-Renamer" not in line
                                and not any(x in low for x in ("spotlight", "mds_stores", "chrome", "firefox"))):
            hits.append(line.strip())
    return hits


def resolve_running(ps_output: str | None = None, own: set[int] | None = None) -> list[str]:
    """Return matching process lines ("pid executable"); empty if Resolve is not running (best-effort, macOS).
    ps_output / own are injectable for tests."""
    own = _own_chain() if own is None else own
    if ps_output is None:
        try:
            ps_output = subprocess.check_output(["ps", "ax", "-o", "pid=,comm="], text=True, stderr=subprocess.DEVNULL)
        except (subprocess.CalledProcessError, FileNotFoundError):
            return _legacy_scan(own)
    hits: list[str] = []
    for line in ps_output.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        pid, exe = int(parts[0]), parts[1].strip()
        if pid in own:
            continue
        if is_resolve_exe(exe):
            hits.append(f"{pid} {exe}")
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description="Guard: fail if DaVinci Resolve is running")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    hits = resolve_running()
    if hits:
        if not args.quiet:
            print("DaVinci Resolve appears to be RUNNING — refusing to proceed.", file=sys.stderr)
            for h in hits[:10]:
                print(f"  {h}", file=sys.stderr)
            print("Quit Resolve, then re-run. (resolve_guard)", file=sys.stderr)
        return 1
    if not args.quiet:
        print("DaVinci Resolve: not detected (OK)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
