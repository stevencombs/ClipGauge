#!/usr/bin/env python3
"""
Central clip-notes store: one JSON file per clip in notes/clips/<clip_id>.json (project folder on the Lexar).

The pipeline ALWAYS writes the full per-clip record here (same fields as a .json sidecar: source, frames,
transcript with every segment, describe, proposal, timing, applied) plus:
  clip_id       stable id: <original camera stem slug>-<8 hex of sha1(original name | size | creation_time)>
                (same clip -> same id, whatever it is renamed to or wherever it is moved)
  current_path  where the video is now (updated on rename, move-into-folder and undo)
  store         {schema, created_at, updated_at, original_name, original_path, imported_from?}
.json/.md sidecars next to each clip are optional (config sidecar.write_next_to_clip, default false).

ExFAT-safe: plain files only (no symlinks / hard links), atomic temp + replace writes (a reader never sees a
half-written record), macOS ._ AppleDouble companions are ignored. One file per clip, so concurrent writers of
different clips never collide, and a pipeline run plus a reader (UI, transcript bundle) are always safe.

  python3 scripts/notes_store.py                 # summary: how many records, where
  python3 scripts/notes_store.py --show ID|PATH  # print one record
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import ROOT, abs_path, atomic_write_text, load_config, section, slugify  # noqa: E402

STORE_SCHEMA = 1
TOOL = "AI-Video-Renamer"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
# Single source of truth for media types (run_pipeline / apply_renames / web_ui / bundle import these; selftest checks)
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".m4v", ".avi", ".webm", ".mts"}
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".dng", ".webp", ".tif", ".tiff"}
MEDIA_EXTS = VIDEO_EXTS | PHOTO_EXTS
# Lexar folders searched for moved clips (relative to the drive root); config transcripts.search_roots
DEFAULT_SEARCH_ROOTS = ["DaVinci Resolve", "DaVinci Resolve Media", "Video Assets"]
SKIP_DIRS = {"CacheClip", "ProxyMedia", "OptimizedMedia", ".gallery", "models", "processing", "logs", "exports",
             "notes", "needs-review", "done", "config", "scripts", "System Volume Information"}
MAX_WALK_ENTRIES = 200000


def media_kind(p: str | os.PathLike) -> str | None:
    """'video' | 'photo' by extension (any case), None for anything else."""
    ext = os.path.splitext(str(p))[1].lower()
    return "video" if ext in VIDEO_EXTS else ("photo" if ext in PHOTO_EXTS else None)


def record_kind(rec: dict) -> str:
    """media_kind of a stored record (older records have none -> from the file extension, default video)."""
    k = rec.get("media_kind") or (rec.get("source") or {}).get("media_kind")
    return k or media_kind((rec.get("source") or {}).get("name") or "") or "video"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def norm(p: str | os.PathLike) -> str:
    """Comparable key (same rule as apply_renames.norm): resolved parent + name, NFC, lower-cased."""
    p = Path(p)
    try:
        parent = p.parent.resolve()
    except OSError:
        parent = p.parent
    return unicodedata.normalize("NFC", str(parent / p.name)).lower()


# ------------------------------------------------------------ locations ----

def notes_dir(cfg: dict) -> Path:
    return abs_path(section(cfg, "notes_store").get("dir") or "notes")


def clips_dir(cfg: dict) -> Path:
    return notes_dir(cfg) / "clips"


def dry_run_dir(cfg: dict) -> Path:
    """Dry-run previews go here, never into clips/ (a preview must not replace or pose as a processed clip)."""
    return notes_dir(cfg) / "dry-run"


def next_to_clip(cfg: dict) -> bool:
    """config sidecar.write_next_to_clip (default false): also write <video>.json/.md next to each clip."""
    return bool(section(cfg, "sidecar").get("write_next_to_clip", False))


def volume_root(cfg: dict) -> Path:
    """The Lexar drive root (parent of the project folder)."""
    v = section(cfg, "transcripts").get("volume_root")
    return Path(v) if v else ROOT.parent


def inbox_dir(cfg: dict) -> Path:
    return abs_path(cfg.get("inbox_dir") or "inbox")


def search_roots(cfg: dict) -> list[str]:
    r = section(cfg, "transcripts").get("search_roots")
    return [str(x) for x in r] if isinstance(r, list) else list(DEFAULT_SEARCH_ROOTS)


# ------------------------------------------------------------------ ids ----

def make_id(rec: dict) -> str:
    """Stable clip id from the ORIGINAL camera file: name + size + container creation time."""
    src = rec.get("source") or {}
    name = str(src.get("name") or Path(str(src.get("path") or "clip")).name)
    base = slugify(Path(name).stem, 40) or "clip"
    key = f"{unicodedata.normalize('NFC', name).lower()}|{src.get('size_bytes') or ''}|{src.get('creation_time') or ''}"
    return f"{base}-{hashlib.sha1(key.encode('utf-8')).hexdigest()[:8]}"


def record_path(cfg: dict, cid: str, dry: bool = False) -> Path:
    if not ID_RE.match(cid or ""):
        raise ValueError(f"bad clip id {cid!r}")
    return (dry_run_dir(cfg) if dry else clips_dir(cfg)) / f"{cid}.json"


def is_clip_record(rec: object) -> bool:
    return isinstance(rec, dict) and isinstance(rec.get("source"), dict) and (
        rec.get("tool") == TOOL or "proposal" in rec or "transcript" in rec)


# ------------------------------------------------------------- read/write ----

def load(cfg: dict, cid: str) -> dict | None:
    try:
        rec = json.loads(record_path(cfg, cid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return rec if is_clip_record(rec) else None


def load_all(cfg: dict) -> list[dict]:
    d = clips_dir(cfg)
    out: list[dict] = []
    if not d.is_dir():
        return out
    with os.scandir(d) as it:
        names = sorted(e.name for e in it if e.is_file(follow_symlinks=False) and e.name.endswith(".json")
                       and not e.name.startswith("."))
    for n in names:
        try:
            rec = json.loads((d / n).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if is_clip_record(rec):
            rec.setdefault("clip_id", n[:-5])
            out.append(rec)
    return out


def save(cfg: dict, record: dict, current_path: str | os.PathLike | None = None,
         imported_from: str | None = None) -> dict:
    """Write (create or replace) one clip's record atomically. Returns the stored record.
    Dry-run previews (record["dry_run"] true, never applied, not an imported sidecar) go to notes/dry-run/ instead of
    notes/clips/, so a preview never replaces or poses as a processed clip."""
    rec = dict(record)
    cid = rec.get("clip_id") or make_id(rec)
    dry = bool(rec.get("dry_run")) and not rec.get("applied") and not imported_from
    old = {} if dry else (load(cfg, cid) or {})
    src = rec.get("source") or {}
    rec["clip_id"] = cid
    cur = str(current_path) if current_path else (rec.get("current_path") or old.get("current_path") or src.get("path"))
    rec["current_path"] = cur
    meta = dict(old.get("store") or {})
    meta.update({"schema": STORE_SCHEMA, "updated_at": now_iso(), "original_name": src.get("name"),
                 "original_path": src.get("path")})
    meta.setdefault("created_at", meta["updated_at"])
    if imported_from:
        meta["imported_from"] = imported_from
    if rec.get("media_kind") is None:
        rec["media_kind"] = record_kind(rec)
    rec["store"] = meta
    atomic_write_text(record_path(cfg, cid, dry), json.dumps(rec, indent=2, ensure_ascii=False) + "\n")
    return rec


def build_index(cfg: dict, records: list[dict] | None = None) -> dict:
    recs = load_all(cfg) if records is None else records
    idx = {"records": recs, "by_id": {}, "by_current": {}, "by_source": {}}
    for r in recs:
        idx["by_id"][r.get("clip_id")] = r
        if r.get("current_path"):
            k = norm(r["current_path"])
            prev = idx["by_current"].get(k)  # two records claim one path -> the most recently written wins
            if prev is None or str((r.get("store") or {}).get("updated_at") or "") >= str((prev.get("store") or {}).get("updated_at") or ""):
                idx["by_current"][k] = r
        sp = (r.get("source") or {}).get("path")
        if sp:
            idx["by_source"].setdefault(norm(sp), r)
    return idx


def find(cfg: dict, video: str | os.PathLike, idx: dict | None = None) -> dict | None:
    """The record whose CURRENT path is this video (None if the store doesn't know it there)."""
    idx = idx if idx is not None else build_index(cfg)
    return idx["by_current"].get(norm(video))


def find_dry(cfg: dict, video: str | os.PathLike) -> dict | None:
    """A dry-run preview record (notes/dry-run/) whose current path is this video, newest first."""
    d, key, best = dry_run_dir(cfg), norm(video), None
    if not d.is_dir():
        return None
    with os.scandir(d) as it:
        names = [e.name for e in it if e.name.endswith(".json") and not e.name.startswith(".")]
    for n in names:
        try:
            r = json.loads((d / n).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if is_clip_record(r) and r.get("current_path") and norm(r["current_path"]) == key:
            if best is None or str((r.get("store") or {}).get("updated_at")) > str((best.get("store") or {}).get("updated_at")):
                best = r
    return best


def ingest_sidecar(cfg: dict, json_path: str | os.PathLike, current_path: str | os.PathLike | None = None) -> dict | None:
    """Copy a .json sidecar into the store (used when a clip processed before the store existed is applied)."""
    try:
        rec = json.loads(Path(json_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not is_clip_record(rec):
        return None
    return save(cfg, rec, current_path or (rec.get("applied") or {}).get("new_path"), imported_from=str(json_path))


# --------------------------------------------------------------- updates ----

def annotate_applied(cfg: dict, video: str | os.PathLike, applied: dict, new_path: str | os.PathLike,
                     fallback_json: str | os.PathLike | None = None) -> str | None:
    """After apply_clip: store the 'applied' note and the new location. Returns the clip id (None if unknown).
    Never raises."""
    try:
        rec = find(cfg, video)
        if rec is None and fallback_json and Path(fallback_json).is_file():
            rec = ingest_sidecar(cfg, fallback_json, video)
        if rec is None:
            rec = find_dry(cfg, video)  # applied after a dry run (config dry_run true, then Apply)
        if rec is None:
            return None
        rec = dict(rec)
        rec["applied"] = dict(applied, previous_current_path=rec.get("current_path"))
        return save(cfg, rec, new_path)["clip_id"]
    except Exception:  # noqa: BLE001
        return None


def annotate_move(cfg: dict, old_path: str | os.PathLike, new_path: str | os.PathLike, folder: str | None,
                  moved_from: str | None = None, log_id: str | None = None) -> str | None:
    """A renamed clip moved into (folder set) or back out of (folder None) a batch folder. Never raises."""
    try:
        rec = find(cfg, old_path)
        if rec is None:
            return None
        rec = dict(rec)
        ap = dict(rec.get("applied") or {})
        ap["new_path"] = str(new_path)
        if folder:
            ap.update(folder=folder, moved_from=moved_from, moved_log_id=log_id)
        else:
            for k in ("folder", "moved_from", "moved_log_id"):
                ap.pop(k, None)
        if rec.get("applied") is not None:
            rec["applied"] = ap
        return save(cfg, rec, new_path)["clip_id"]
    except Exception:  # noqa: BLE001
        return None


def strip_applied(cfg: dict, current: str | os.PathLike, back_to: str | os.PathLike) -> str | None:
    """Undo: drop the 'applied' note, the clip is back at back_to (its original path). Never raises."""
    try:
        rec = find(cfg, current) or find(cfg, back_to)
        if rec is None:
            return None
        rec = dict(rec)
        rec.pop("applied", None)
        return save(cfg, rec, back_to)["clip_id"]
    except Exception:  # noqa: BLE001
        return None


def update_transcript(cfg: dict, video: str | os.PathLike, segments: list[dict]) -> bool:
    """Put the full transcript segments into the record (processing cleanup, when the record lacked them)."""
    try:
        rec = find(cfg, video)
        if rec is None:
            return False
        rec = dict(rec)
        tx = dict(rec.get("transcript") or {})
        tx["segments"] = segments
        rec["transcript"] = tx
        save(cfg, rec)
        return True
    except Exception:  # noqa: BLE001
        return False


# -------------------------------------------------- locating moved clips ----

def build_file_map(cfg: dict, extra_dirs: list[Path] | None = None) -> dict:
    """Video and photo files under inbox/ + the search roots: {"by_name": lower name -> [paths], "by_size": size -> [paths]}."""
    vol = volume_root(cfg)
    tops = [inbox_dir(cfg)] + [vol / r for r in search_roots(cfg)] + list(extra_dirs or [])
    by_name: dict[str, list[Path]] = {}
    by_size: dict[int, list[Path]] = {}
    seen: set[str] = set()
    budget = [MAX_WALK_ENTRIES]
    stack = [t for t in tops if t.is_dir()]
    while stack:
        d = stack.pop()
        key = os.path.realpath(d)
        if key in seen:
            continue
        seen.add(key)
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except OSError:
            continue
        for e in entries:
            budget[0] -= 1
            if budget[0] <= 0:
                return {"by_name": by_name, "by_size": by_size}
            if e.name.startswith("."):
                continue
            if e.is_dir(follow_symlinks=False):
                if e.name not in SKIP_DIRS:
                    stack.append(Path(e.path))
            elif os.path.splitext(e.name)[1].lower() in MEDIA_EXTS and e.is_file(follow_symlinks=False):
                p = Path(e.path)
                by_name.setdefault(unicodedata.normalize("NFC", e.name).lower(), []).append(p)
                try:
                    by_size.setdefault(e.stat().st_size, []).append(p)
                except OSError:
                    pass
    return {"by_name": by_name, "by_size": by_size}


def locate(rec: dict, fmap: dict, claimed: set[str] | None = None) -> tuple[Path | None, str]:
    """Where the clip's video is now: (path, how) — how = stored path | same name in <folder> | same size in <folder>
    | not found. Size matching (exact bytes, a video nobody else claims, near the stored location) catches clips
    renamed by hand in Finder."""
    claimed = claimed if claimed is not None else set()
    cur = rec.get("current_path")
    if cur and Path(cur).is_file():
        return Path(cur), "stored path"
    def by_name(n: str | None) -> Path | None:
        if not n:
            return None
        hits = [p for p in fmap["by_name"].get(unicodedata.normalize("NFC", n).lower(), []) if norm(p) not in claimed]
        return _closest(hits, cur) if hits else None

    # 1) the name it was given (renamed / proposed) anywhere in inbox/ + search roots
    for n in (cur and Path(cur).name, (rec.get("applied") or {}).get("new_path") and Path(rec["applied"]["new_path"]).name,
              (rec.get("proposal") or {}).get("new_name")):
        hit = by_name(n)
        if hit:
            return hit, f"same name in {hit.parent.name}/"
    # 2) exact byte size: in the stored folder first, else unique across all searched folders (renamed by hand)
    size = (rec.get("source") or {}).get("size_bytes")
    hits = [p for p in fmap["by_size"].get(size, []) if norm(p) not in claimed] if isinstance(size, int) and size > 0 else []
    near = [p for p in hits if cur and p.parent.name.lower() == Path(cur).parent.name.lower()]
    if len(near) == 1 or len(hits) == 1:
        h = near[0] if len(near) == 1 else hits[0]
        return h, f"same size in {h.parent.name}/ (renamed by hand?)"
    # 3) the original camera name (an un-renamed copy)
    hit = by_name((rec.get("source") or {}).get("name"))
    if hit:
        return hit, f"original camera name in {hit.parent.name}/"
    return None, "not found"


def _closest(hits: list[Path], cur: str | None) -> Path:
    if len(hits) == 1 or not cur:
        return hits[0]
    pname = Path(cur).parent.name.lower()
    return next((h for h in hits if h.parent.name.lower() == pname), hits[0])


# ------------------------------------------------------------------ CLI ----

def main() -> int:
    ap = argparse.ArgumentParser(description="Central clip-notes store (notes/clips/<clip_id>.json)")
    ap.add_argument("--show", help="print one record (clip id or current video path)")
    args = ap.parse_args()
    cfg = load_config()
    if args.show:
        rec = (load(cfg, args.show) if ID_RE.match(args.show) else None) or find(cfg, args.show)
        if not rec and ID_RE.match(args.show):  # a dry-run preview
            try:
                rec = json.loads(record_path(cfg, args.show, dry=True).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                rec = None
        if not rec:
            print("not found", file=sys.stderr)
            return 1
        print(json.dumps(rec, indent=2, ensure_ascii=False))
        return 0
    recs = load_all(cfg)
    live = sum(1 for r in recs if not r.get("dry_run"))
    applied = sum(1 for r in recs if (r.get("applied") or {}).get("action") == "renamed")
    photos = sum(1 for r in recs if record_kind(r) == "photo")
    print(f"{clips_dir(cfg)}: {len(recs)} record(s) ({photos} photo) · live {live} · renamed {applied} · "
          f"next-to-clip sidecars {'ON' if next_to_clip(cfg) else 'off'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
