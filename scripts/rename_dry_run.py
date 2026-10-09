#!/usr/bin/env python3
"""Propose (and optionally apply) a rename. Default is dry-run only."""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import load_config  # noqa: E402


def plan_rename(src: Path, proposed_name: str, dest_dir: Path) -> Path:
    # Keep extension from source if proposed name has none
    name = proposed_name
    if not Path(name).suffix and src.suffix:
        name = f"{name}{src.suffix}"
    return dest_dir / name


def apply_allowed(cfg: dict, apply_flag: bool) -> tuple[bool, str]:
    if not apply_flag:
        return False, "dry-run (pass --apply to request write)"
    if cfg.get("dry_run", True):
        return False, "blocked: config dry_run is true (set dry_run=false in config.json AND pass --apply)"
    return True, "apply enabled"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Print proposed rename; never renames unless --apply and dry_run=false"
    )
    ap.add_argument("src", type=Path, help="Source file path")
    ap.add_argument("proposed_name", help="Proposed basename (with or without extension)")
    ap.add_argument(
        "--dest-dir",
        type=Path,
        default=None,
        help="Destination directory (default: done/)",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Actually move/rename ONLY if config dry_run is false",
    )
    args = ap.parse_args()

    cfg = load_config()
    src = args.src.expanduser().resolve()
    if not src.is_file():
        print(f"Not a file: {src}", file=sys.stderr)
        return 2

    dest_dir = args.dest_dir
    if dest_dir is None:
        dest_dir = Path(cfg.get("_done_dir_abs") or (Path(cfg["_root"]) / "done"))
    else:
        dest_dir = dest_dir.expanduser().resolve()

    dest = plan_rename(src, args.proposed_name, dest_dir)
    ok, reason = apply_allowed(cfg, args.apply)

    print("=== Rename plan ===")
    print(f"  from: {src}")
    print(f"  to:   {dest}")
    print(f"  pattern hint: {cfg.get('naming_pattern')}")
    print(f"  config.dry_run: {cfg.get('dry_run', True)}")
    print(f"  --apply: {args.apply}")
    print(f"  action: {'MOVE' if ok else 'WOULD MOVE (no write)'} — {reason}")

    if not ok:
        return 0

    dest_dir.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        print(f"Refusing: destination exists: {dest}", file=sys.stderr)
        return 1
    shutil.move(str(src), str(dest))
    print(f"Moved -> {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
