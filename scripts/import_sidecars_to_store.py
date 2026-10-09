#!/usr/bin/env python3
"""
One-time import: copy every existing .json clip sidecar on the Lexar into the central notes store
(notes/clips/<clip_id>.json, see notes_store.py). Safe to re-run (idempotent).

  python3 scripts/import_sidecars_to_store.py --dry-run     # preview counts, write nothing
  python3 scripts/import_sidecars_to_store.py               # import (sidecars are left exactly where they are)
  python3 scripts/import_sidecars_to_store.py --path "DaVinci Resolve/Retro Game Expo"   # limit to a folder
  python3 scripts/import_sidecars_to_store.py --remove-sidecars [--dry-run]
        # after a successful import, MOVE each imported .json/.md (+ copied .transcript.json/.txt and ._ companions)
        # into logs/backups/sidecars-<YYYYMMDD-HHMMSS>/<path relative to the Lexar root>/ — never deletes anything

Scans the whole Lexar (hidden folders, Resolve caches, models/, processing/, logs/, exports/, notes/ skipped).
A sidecar is ours when it's a renamer record (tool "AI-Video-Renamer", source + transcript/proposal). Its clip's
current location = the video next to it with the same name, else a video of the same byte size in that folder
(renamed by hand in Finder), else its last known path ('video_found' false). Merging with a record the store already
has (same clip id): the store's record is kept and only its current_path is refreshed when the stored path no longer
exists and the sidecar's video was found. Takes no pipeline lock (only the store is written, atomically).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import abs_path, load_config  # noqa: E402
import notes_store as ns  # noqa: E402
from build_transcript_bundle import find_video, inside, load_sidecar, rel_to, same_size_beside, walk_sidecars  # noqa: E402

COMPANION_SUFFIXES = (".json", ".md", ".transcript.json", ".transcript.txt")


def sidecar_video(sj: Path, rec: dict) -> tuple[Path | None, str]:
    v = find_video(sj, rec)
    if v:
        return v, "same name"
    v = same_size_beside(sj, rec)
    return (v, "same size (renamed by hand?)") if v else (None, "not found")


def import_all(cfg: dict, paths: list[str] | None = None, preview: bool = False) -> dict:
    vol = ns.volume_root(cfg)
    tops = []
    for raw in paths or []:
        p = Path(raw) if Path(raw).is_absolute() else vol / raw
        if not inside(p, vol) or not p.is_dir():
            raise ValueError(f"{raw}: not a folder on the Lexar ({vol})")
        tops.append(p)
    tops = tops or [vol]
    counts = {"sidecars_scanned": 0, "not_ours": 0, "imported": 0, "updated_path": 0, "unchanged": 0,
              "video_found": 0, "video_by_size": 0, "video_not_found": 0}
    items, seen = [], set()
    idx = ns.build_index(cfg)
    for top in tops:
        for sj in walk_sidecars(top, True, ns.SKIP_DIRS):
            key = os.path.realpath(sj).lower()
            if key in seen or sj.name.lower().endswith(".transcript.json"):
                continue
            seen.add(key)
            counts["sidecars_scanned"] += 1
            rec = load_sidecar(sj)
            if rec is None:
                counts["not_ours"] += 1
                continue
            video, how = sidecar_video(sj, rec)
            counts["video_found" if video else "video_not_found"] += 1
            counts["video_by_size"] += how.startswith("same size")
            cid = rec.get("clip_id") or ns.make_id(rec)
            old = idx["by_id"].get(cid)
            item = {"sidecar": rel_to(sj, vol), "clip_id": cid, "video": rel_to(video, vol) if video else None,
                    "located": how}
            if old is None:
                item["result"] = "imported"
                if not preview:
                    saved = ns.save(cfg, dict(rec, clip_id=cid),
                                    video or (rec.get("applied") or {}).get("new_path") or sj.with_name(
                                        Path(str((rec.get("proposal") or {}).get("new_name") or sj.stem)).name),
                                    imported_from=str(sj))
                    idx["by_id"][cid] = saved
            else:
                stored_ok = old.get("current_path") and Path(old["current_path"]).is_file()
                if video and not stored_ok:
                    item["result"] = "updated_path"
                    if not preview:
                        idx["by_id"][cid] = ns.save(cfg, old, video)
                else:
                    item["result"] = "unchanged"
            counts[item["result"]] += 1
            items.append(item)
    return {"preview": preview, "store": str(ns.clips_dir(cfg)), "counts": counts, "items": items}


def remove_sidecars(cfg: dict, items: list[dict], preview: bool = False) -> dict:
    """Move imported sidecars (+ companions) into logs/backups/sidecars-<ts>/<rel path>. Never deletes."""
    vol = ns.volume_root(cfg)
    dest_root = abs_path(cfg.get("logs_dir") or "logs") / "backups" / f"sidecars-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    moved, kept = [], []
    for it in items:
        sj = vol / it["sidecar"]
        cid = it["clip_id"]
        if ns.load(cfg, cid) is None and not preview:
            kept.append({"sidecar": it["sidecar"], "reason": "not in the store — kept"})
            continue
        stem = sj.name[:-5]
        for suf in COMPANION_SUFFIXES:
            for name in (stem + suf, "._" + stem + suf):
                src = sj.parent / name
                if not src.is_file():
                    continue
                dst = dest_root / rel_to(src, vol)
                if not preview:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    if dst.exists():
                        kept.append({"sidecar": rel_to(src, vol), "reason": "backup target exists — kept"})
                        continue
                    if os.stat(src).st_dev != os.stat(dst.parent).st_dev:  # never copy + delete across volumes
                        kept.append({"sidecar": rel_to(src, vol), "reason": "not on the same volume — kept"})
                        continue
                    os.rename(src, dst)
                moved.append({"from": rel_to(src, vol), "to": str(dst)})
    return {"backup_dir": str(dest_root), "moved": moved, "kept": kept}


def main() -> int:
    ap = argparse.ArgumentParser(description="Import existing .json sidecars into the central notes store")
    ap.add_argument("--path", action="append", help="limit to a folder on the Lexar (relative to the drive root)")
    ap.add_argument("--dry-run", action="store_true", help="preview, write nothing")
    ap.add_argument("--remove-sidecars", action="store_true",
                    help="after importing, MOVE the sidecars into logs/backups/sidecars-<ts>/ (never deletes)")
    ap.add_argument("--json", action="store_true", help="print the full result as JSON")
    ap.add_argument("-v", "--verbose", action="store_true", help="one line per sidecar")
    args = ap.parse_args()
    cfg = load_config()
    try:
        res = import_all(cfg, args.path, args.dry_run)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.remove_sidecars:
        res["remove"] = remove_sidecars(cfg, [i for i in res["items"]], args.dry_run)
    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0
    c = res["counts"]
    print(("Preview — nothing written. " if args.dry_run else "") + f"Store: {res['store']}")
    print(f"  sidecars scanned {c['sidecars_scanned']} (not renamer notes: {c['not_ours']})")
    print(f"  imported {c['imported']} · path updated {c['updated_path']} · already in store {c['unchanged']}")
    print(f"  video found {c['video_found']} (by size: {c['video_by_size']}) · not found {c['video_not_found']}")
    if args.verbose:
        for i in res["items"]:
            print(f"  {i['result']:<13} {i['clip_id']:<40} {i['video'] or '(video not found)'}  [{i['located']}]")
    if "remove" in res:
        r = res["remove"]
        print(f"  sidecars {'that would be ' if args.dry_run else ''}moved to {r['backup_dir']}: {len(r['moved'])} file(s); "
              f"kept {len(r['kept'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
