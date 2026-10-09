#!/usr/bin/env python3
"""
Sweep processing/ and remove leftovers (frames dir, audio/transcript dir, wav, per-clip temps) of clips that were
already renamed in place, according to logs/rename-log.jsonl.

  python3 scripts/cleanup_processing.py --dry-run   # preview: what would be removed, and what is kept and why
  python3 scripts/cleanup_processing.py             # remove (takes logs/pipeline.lock; refused while a run is active)

Same rules as the per-clip cleanup in apply_renames.py: entries are matched by the clip's ORIGINAL basename
(exact names only), only direct children of processing/ are touched, never anything outside processing/.
If the .json sidecar doesn't already hold the full transcript, <new stem>.transcript.json/.txt are copied next to
the renamed video first. Kept: needs-review / pending clips, and anything not tied to a renamed clip (e.g. old
'x4 unboxing' test frames for a clip outside inbox/). Each cleanup is appended to logs/rename-log.jsonl
(action "cleanup"; undo ignores these lines).
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import load_config  # noqa: E402
from apply_renames import (  # noqa: E402
    _tree_size,
    append_log,
    cleanup_clip_processing,
    current_path,
    inbox_videos,
    log_index,
    new_batch_id,
    now_iso,
    processing_dir,
)


def sweep(cfg: dict, preview: bool = False) -> dict:
    index = log_index(cfg)
    batch = new_batch_id("cleanup")
    results, seen = [], set()
    removed_names: set[str] = set()
    for r in reversed([x for x in index["active"] if x["action"] == "renamed"]):  # newest first
        orig, new = Path(r["original"]), Path(current_path(index, r))  # follows moves into a batch folder
        key = orig.stem.lower()
        if key in seen:
            continue
        seen.add(key)
        if orig.exists():  # back under its original name (e.g. renamed back by hand) -> may be reprocessed
            results.append({"original": str(orig), "skipped": "original file is back in inbox/ — kept"})
            continue
        c = cleanup_clip_processing(cfg, orig, new, preview=preview)
        if not (c["removed"] or c["transcript_copies"] or c["errors"] or c.get("skipped")):
            continue
        removed_names.update(Path(p).name for p in c["removed"])
        results.append({"original": str(orig), "new": str(new), **c})
        if not preview and (c["removed"] or c["transcript_copies"]):
            append_log(cfg, {"id": uuid.uuid4().hex[:12], "batch": batch, "time": now_iso(), "action": "cleanup",
                             "original": str(orig), "new": str(new), "removed": c["removed"], "bytes": c["bytes"],
                             "transcript_copies": c["transcript_copies"], "errors": c["errors"],
                             "origin": "cleanup_processing"})
    # what stays, and why
    proc = processing_dir(cfg)
    pending = {v.stem.lower() for v in inbox_videos(cfg)}
    review = {Path(x["original"]).stem.lower() for x in index["active"] if x["action"] == "needs-review"}
    kept = []
    if proc.is_dir():
        for p in sorted(proc.iterdir()):
            if p.name.startswith(".") or p.name in removed_names:
                continue
            stem = p.name
            for suf in ("_frames", "_audio", ".wav", ".transcript.json", ".transcript.txt", ".json", ".tmp", "_tmp"):
                if stem.endswith(suf):
                    stem = stem[: -len(suf)]
                    break
            s = stem.lower()
            why = ("needs review — kept for re-check" if s in review else
                   "clip still pending in inbox/" if s in pending else
                   "not a renamed clip (e.g. outside inbox/) — left alone")
            kept.append({"path": str(p), "why": why})
    total = sum(r.get("bytes", 0) for r in results)
    return {"preview": preview, "results": results, "kept": kept, "bytes": total, "batch": batch,
            "removed_count": sum(len(r.get("removed") or []) for r in results)}


def main() -> int:
    ap = argparse.ArgumentParser(description="Remove processing/ leftovers of clips already renamed (rename-log.jsonl)")
    ap.add_argument("--dry-run", action="store_true", help="Preview only")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    cfg = load_config()
    from pipeline_lock import acquire_lock, lock_path, release_lock  # noqa: E402

    proc = processing_dir(cfg)
    before = _tree_size(proc) if proc.is_dir() else 0
    lock = None
    if not args.dry_run:
        lock = lock_path(cfg)
        ok, holder = acquire_lock(lock)
        if not ok:
            print(f"A pipeline run is active (pid {holder.get('pid')}) — not cleaning while it runs "
                  "(use --dry-run to preview).", file=sys.stderr)
            return 3
    try:
        summary = sweep(cfg, preview=args.dry_run)
    finally:
        if lock:
            release_lock(lock)
    after = _tree_size(proc) if proc.is_dir() else 0
    summary.update(processing_bytes_before=before, processing_bytes_after=after)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    verb = "WOULD REMOVE" if args.dry_run else "REMOVED"
    for r in summary["results"]:
        name = Path(r["original"]).name
        if r.get("skipped"):
            print(f"KEPT         {name}: {r['skipped']}")
            continue
        if r.get("removed"):
            print(f"{verb:12} {name} -> {Path(r['new']).name}: {len(r['removed'])} item(s), {r['bytes'] / 1e6:.1f} MB")
            for p in r["removed"]:
                print(f"               {Path(p).name}")
        for c in r.get("transcript_copies") or []:
            print(f"               transcript {'would be ' if args.dry_run else ''}copied -> {Path(c['to']).name}")
        for e in r.get("errors") or []:
            print(f"               note: {e}")
    for k in summary["kept"]:
        print(f"KEPT         {Path(k['path']).name}: {k['why']}")
    print(f"{'Preview: would remove' if args.dry_run else 'Removed'} {summary['removed_count']} item(s), "
          f"{summary['bytes'] / 1e6:.1f} MB. processing/ {before / 1e6:.1f} MB -> {after / 1e6:.1f} MB"
          + (" (unchanged, preview)" if args.dry_run else "") + ".")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
