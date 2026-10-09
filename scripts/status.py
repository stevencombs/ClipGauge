#!/usr/bin/env python3
"""Read/write logs/status.json for live pipeline progress."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import ENGINE_VERSION, load_config, status_path  # noqa: E402


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def default_status() -> dict:
    return {
        "updated_at": _now_iso(),
        "state": "idle",
        "queue": [],
        "current_file": None,
        "step": None,
        "frames_done": 0,
        "frames_total": 9,
        "eta": None,
        "message": "",
        "last_error": None,
        "dry_run": True,
    }


def read_status(path: Path | None = None) -> dict:
    path = path or status_path()
    if not path.exists():
        return default_status()
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_status(update: dict, path: Path | None = None, merge: bool = True) -> dict:
    path = path or status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    current = read_status(path) if merge and path.exists() else default_status()
    current.update(update)
    current["updated_at"] = _now_iso()
    current["engine"] = ENGINE_VERSION
    # Atomic-ish write for ExFAT
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(current, f, indent=2)
        f.write("\n")
    tmp.replace(path)
    return current


def main() -> int:
    ap = argparse.ArgumentParser(description="status.json helper")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_show = sub.add_parser("show", help="Print current status")
    p_show.add_argument("--path", default=None)

    p_init = sub.add_parser("init", help="Write idle status.json")
    p_init.add_argument("--path", default=None)

    p_set = sub.add_parser("set", help="Merge key=value (JSON values allowed)")
    p_set.add_argument("pairs", nargs="+", help="key=value")
    p_set.add_argument("--path", default=None)

    args = ap.parse_args()
    cfg = load_config()
    path = Path(args.path) if getattr(args, "path", None) else status_path(cfg)

    if args.cmd == "show":
        print(json.dumps(read_status(path), indent=2))
        return 0
    if args.cmd == "init":
        st = default_status()
        st["dry_run"] = bool(cfg.get("dry_run", True))
        write_status(st, path=path, merge=False)
        print(f"Initialized {path}")
        return 0
    if args.cmd == "set":
        update: dict = {}
        for pair in args.pairs:
            if "=" not in pair:
                print(f"Bad pair: {pair}", file=sys.stderr)
                return 2
            k, v = pair.split("=", 1)
            try:
                update[k] = json.loads(v)
            except json.JSONDecodeError:
                update[k] = v
        write_status(update, path=path, merge=True)
        print(json.dumps(read_status(path), indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
