#!/usr/bin/env python3
"""
Reverse in-place renames recorded in logs/rename-log.jsonl (written by apply_renames.py / run_pipeline.py).

  python3 scripts/undo_renames.py --dry-run              # preview undo of the LAST batch (default selection)
  python3 scripts/undo_renames.py                        # undo the last batch
  python3 scripts/undo_renames.py --all [--dry-run]      # undo everything still applied
  python3 scripts/undo_renames.py --since 2026-10-06T13:00 [--dry-run]
  python3 scripts/undo_renames.py --batch <batch id>     # one batch (ids are in the log)
  python3 scripts/undo_renames.py --file NAME            # one clip (current or original file name)
  python3 scripts/undo_renames.py --id LOG_ID [--json]   # one log record (ClipGauge's per-row Undo); --json = summary

Per record (newest first): the video is renamed back to its original name in inbox/ (top level — also for clips
that were renamed into a batch folder inbox/<folder>/), its .json/.md sidecars (and any copied .transcript.json/.txt)
go back to their original names, the 'applied' note is removed from the sidecar, and an
{"action": "undo"} line is appended to the log (so the clip counts as unprocessed again — unless its sidecar
still sits next to it, in which case run_pipeline.py --all keeps skipping it; use --force there to redo it).
The central notes store (notes/clips/<clip_id>.json) is updated too: 'applied' removed, current_path back to the
original (or, for a move, back to inbox/ top level) — so undo works the same with or without sidecars.
processing/ files deleted after a rename are not restored (they are re-created if the clip is reprocessed).
A "moved" record (apply_renames.py --move-into / the UI's "Move renamed clips into folder") is undone by moving the
clip and its sidecars back to inbox/ top level (still renamed); undoing the original rename after that restores the
original name. Undoing a rename whose clip was moved later undoes that move first. A batch folder is removed only if
it ends up empty (Finder's .DS_Store / ._* litter doesn't count).
Never overwrites anything; works whatever config dry_run says; refused while a pipeline run is active.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import load_config  # noqa: E402
from apply_renames import (  # noqa: E402
    _same,
    active_records,
    annotate_move,
    append_log,
    in_inbox,
    move_file,
    new_batch_id,
    norm,
    now_iso,
    read_log,
    remove_folder_if_empty,
    strip_annotation,
)
import notes_store as ns  # noqa: E402


def _parse_time(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.astimezone()


def select_records(records: list[dict], mode: str = "last-batch", since: str | None = None,
                   batch: str | None = None, name: str | None = None, rid: str | None = None) -> list[dict]:
    """Active records to undo, newest first."""
    act = active_records(records)
    if mode == "all":
        sel = act
    elif mode == "since":
        t0 = _parse_time(since)
        sel = [r for r in act if r.get("time") and _parse_time(r["time"]) >= t0]
    elif mode == "batch":
        sel = [r for r in act if r.get("batch") == batch]
    elif mode == "id":
        sel = [r for r in act if rid and r.get("id") == rid]
    elif mode == "file":
        n = (name or "").lower()
        sel = [r for r in act if Path(r.get("new") or "").name.lower() == n or Path(r.get("original") or "").name.lower() == n]
    else:  # last-batch
        sel = [r for r in act if act and r.get("batch") == act[-1].get("batch")]
    return list(reversed(sel))


def _later_moves(cfg: dict, rec: dict, handled: set | None = None) -> list[dict]:
    """Active 'moved' records that moved this renamed clip later (newest first), minus ones already handled."""
    if rec.get("action") != "renamed" or not rec.get("id"):
        return []
    moves = [r for r in active_records(read_log(cfg)) if r.get("action") == "moved" and r.get("move_of") == rec["id"]]
    return [r for r in reversed(moves) if r.get("id") not in (handled or set())]


def undo_move(cfg: dict, rec: dict, preview: bool = False, batch: str | None = None) -> dict:
    """Reverse one 'moved' record: clip + sidecars from inbox/<folder>/ back to inbox/ top level (name it had there)."""
    notes: list[str] = []
    src, back = Path(rec["new"]), Path(rec["from"])
    res = {"id": rec.get("id"), "action": "moved", "original": str(back), "new": str(src), "notes": notes,
           "sidecars": [], "folder": rec.get("folder")}
    if not src.exists():
        return {**res, "result": "error", "reason": f"{src.parent.name}/{src.name} is no longer there (moved by hand?)"}
    if back.exists() and not _same(back, src):
        return {**res, "result": "error", "reason": f"inbox/{back.name} already exists — not overwriting"}
    side_back = []
    for m in reversed(rec.get("sidecars") or []):
        frm, to = Path(m["from"]), Path(m["to"])
        if not to.exists():
            notes.append(f"sidecar {to.name} missing — skipped")
        elif frm.exists():
            notes.append(f"{frm} exists — sidecar {to.name} left in place")
        else:
            side_back.append((to, frm))
    if preview:
        return {**res, "result": "would-undo", "sidecars": [{"from": str(a), "to": str(b)} for a, b in side_back]}
    moves = []
    try:
        moves.append(move_file(src, back, notes))
    except OSError as e:
        return {**res, "result": "error", "reason": f"{type(e).__name__}: {e}"}
    for a, b in side_back:
        try:
            moves.append(move_file(a, b, notes))
        except OSError as e:
            notes.append(f"sidecar {a.name} not moved back: {e}")
    js = back.parent / f"{back.stem}.json"
    if js.exists():
        annotate_move(js, back, None)
    ns.annotate_move(cfg, src, back, None)
    folder_removed = remove_folder_if_empty(cfg, src.parent, notes) if not in_inbox(src, cfg) else False
    undo = {"id": uuid.uuid4().hex[:12], "batch": batch or new_batch_id("undo"), "time": now_iso(), "action": "undo",
            "undo_of": rec.get("id"), "undone_action": "moved", "original": str(back), "new": str(src),
            "moves": moves, "folder_removed": folder_removed, "notes": notes}
    append_log(cfg, undo)
    return {**res, "result": "undone", "moves": moves, "folder_removed": folder_removed}


def undo_record(cfg: dict, rec: dict, preview: bool = False, batch: str | None = None,
                handled: set | None = None) -> dict:
    if rec.get("action") == "moved":
        return undo_move(cfg, rec, preview=preview, batch=batch)
    notes: list[str] = []
    orig, new = Path(rec["original"]), Path(rec["new"]) if rec.get("new") else None
    res = {"id": rec.get("id"), "action": rec.get("action"), "original": str(orig), "new": str(new) if new else None,
           "notes": notes, "sidecars": [], "chained": []}
    # moved into a folder later (move-existing action)? undo that move first so the clip is back at rec["new"]
    for m in _later_moves(cfg, rec, handled):
        sub = undo_move(cfg, m, preview=preview, batch=batch)
        res["chained"].append(sub)
        if sub["result"] == "error":
            return {**res, "result": "error", "reason": f"could not undo the later move first: {sub.get('reason')}"}
    pending_moves = preview and bool(_later_moves(cfg, rec))  # preview: the moves above were not really undone
    renamed = rec.get("action") == "renamed" and new is not None and norm(new) != norm(orig)
    if renamed and not pending_moves and not new.exists():
        return {**res, "result": "error", "reason": f"{new.name} is no longer in place (moved/renamed by hand?)"}
    if renamed and orig.exists() and not (new.exists() and _same(orig, new)):
        return {**res, "result": "error", "reason": f"{orig.name} already exists — not overwriting"}
    side_back = []
    for m in reversed(rec.get("sidecars") or []):
        frm, to = Path(m["from"]), Path(m["to"])
        if not to.exists():
            notes.append(f"sidecar {to.name} missing — skipped")
        elif frm.exists():
            notes.append(f"{frm} exists — sidecar {to.name} left in place")
        else:
            side_back.append((to, frm))
    if preview:
        return {**res, "result": "would-undo", "sidecars": [{"from": str(a), "to": str(b)} for a, b in side_back]}
    moves = []
    try:
        if renamed:
            moves.append(move_file(new, orig, notes))
    except OSError as e:
        return {**res, "result": "error", "reason": f"{type(e).__name__}: {e}"}
    for a, b in side_back:
        try:
            moves.append(move_file(a, b, notes))
        except OSError as e:
            notes.append(f"sidecar {a.name} not restored: {e}")
    # transcripts copied next to the video during processing cleanup -> back to the original stem
    for c in (rec.get("cleanup") or {}).get("transcript_copies") or []:
        to = Path(c["to"])
        back_to = orig.parent / c.get("undo_name", to.name)  # inbox/ top level (the copy may sit in a batch folder)
        if not to.exists() or norm(to) == norm(back_to):
            continue
        if back_to.exists():
            notes.append(f"{back_to.name} exists — {to.name} left in place")
            continue
        try:
            moves.append(move_file(to, back_to, notes))
        except OSError as e:
            notes.append(f"transcript {to.name} not renamed back: {e}")
    if (rec.get("cleanup") or {}).get("removed"):
        notes.append("processing/ files removed after the rename are not restored (re-created if the clip is reprocessed)")
    # annotated JSON sidecar: strip the 'applied' note wherever it is now
    back = {norm(m["to"]): m["from"] for m in rec.get("sidecars") or []}
    for a in rec.get("annotated") or []:
        cur = Path(back.get(norm(a), a))
        if not cur.exists():
            cur = Path(a)
        if cur.exists():
            strip_annotation(cur)
    ns.strip_applied(cfg, new if new is not None else orig, orig)
    folder_removed = False
    if renamed and rec.get("folder") and not in_inbox(new, cfg):  # renamed straight into a batch folder
        folder_removed = remove_folder_if_empty(cfg, new.parent, notes)
    undo = {"id": uuid.uuid4().hex[:12], "batch": batch or new_batch_id("undo"), "time": now_iso(), "action": "undo",
            "undo_of": rec.get("id"), "undone_action": rec.get("action"), "original": str(orig),
            "new": str(new) if new else None, "moves": moves, "folder_removed": folder_removed, "notes": notes}
    append_log(cfg, undo)
    return {**res, "result": "undone", "moves": moves, "folder_removed": folder_removed}


def undo(cfg: dict, records: list[dict], preview: bool = False) -> list[dict]:
    batch = new_batch_id("undo")
    handled: set = set()
    out = []
    for r in records:
        out.append(undo_record(cfg, r, preview=preview, batch=batch, handled=handled))
        handled.add(r.get("id"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Undo in-place renames from logs/rename-log.jsonl")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--all", action="store_true", help="Undo every applied rename / needs-review mark")
    g.add_argument("--since", help="Undo records at/after this local time (ISO, e.g. 2026-10-06T13:00)")
    g.add_argument("--batch", help="Undo one batch id")
    g.add_argument("--file", help="Undo one clip by current or original file name")
    g.add_argument("--id", help="Undo one rename-log record by its id (ClipGauge per-row Undo)")
    ap.add_argument("--dry-run", action="store_true", help="Preview only")
    ap.add_argument("--json", action="store_true", help="Print one JSON summary line at the end (ClipGauge)")
    args = ap.parse_args()
    cfg = load_config()
    mode = ("all" if args.all else "since" if args.since else "batch" if args.batch else "file" if args.file
            else "id" if args.id else "last-batch")
    recs = select_records(read_log(cfg), mode, since=args.since, batch=args.batch, name=args.file, rid=args.id)
    if not recs:
        print("Nothing to undo for that selection.")
        if args.json:
            print(json.dumps({"ok": True, "undone": 0, "total": 0, "message": "Nothing to undo for that selection."}))
        return 0
    from pipeline_lock import acquire_lock, lock_path, release_lock  # noqa: E402

    lock = None
    if not args.dry_run:
        lock = lock_path(cfg)
        ok, holder = acquire_lock(lock)
        if not ok:
            print(f"A pipeline run is active (pid {holder.get('pid')}) — stop it first, then undo.", file=sys.stderr)
            if args.json:
                print(json.dumps({"ok": False, "error": "A run is active — stop it first, then undo."}))
            return 3
    try:
        results = undo(cfg, recs, preview=args.dry_run)
    finally:
        if lock:
            release_lock(lock)
    n_ok = 0
    for r in results:
        name = Path(r["new"] or r["original"]).name
        if r["result"] in ("undone", "would-undo"):
            n_ok += 1
            verb = "UNDONE " if r["result"] == "undone" else "WOULD  "
            for c in r.get("chained") or []:
                print(f"{verb} (first) {Path(c['new']).parent.name}/{Path(c['new']).name} -> back to inbox/")
            if r["action"] == "moved":
                print(f"{verb} {Path(r['new']).parent.name}/{name} -> inbox/{Path(r['original']).name}"
                      f"  (+{len(r.get('sidecars') or []) if r['result'] == 'would-undo' else max(0, len(r.get('moves') or []) - 1)} sidecar(s))")
            elif r["action"] == "renamed":
                n_side = len(r["sidecars"]) if r["result"] == "would-undo" else max(0, len(r.get("moves") or []) - 1)
                print(f"{'UNDONE ' if r['result'] == 'undone' else 'WOULD  '} {name} -> {Path(r['original']).name}"
                      f"  (+{n_side} sidecar(s))")
            else:
                print(f"{'UNDONE ' if r['result'] == 'undone' else 'WOULD  '} needs-review mark on {name} (sidecars restored)")
        else:
            print(f"ERROR   {name}: {r.get('reason')}")
        for n in r["notes"]:
            print(f"        note: {n}")
    verb = "would be undone (preview — nothing changed)" if args.dry_run else "undone"
    print(f"{n_ok}/{len(results)} record(s) {verb}. Selection: {mode}.")
    if args.json:
        print(json.dumps({"ok": n_ok == len(results), "undone": n_ok, "total": len(results), "preview": args.dry_run,
                          "message": f"{n_ok}/{len(results)} record(s) {verb}.",
                          "errors": [r.get("reason") for r in results if r["result"] not in ("undone", "would-undo")]},
                         ensure_ascii=False))
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
