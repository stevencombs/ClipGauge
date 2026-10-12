"""In-place run queue: progress, resume after an interruption, and clips moved during a run (ClipGauge v0.7.6).

logs/inplace-run.json records the sources of the last in-place run, which clips are done and which are left.
  * The pipeline updates it after every clip, so a Stop, crash, sleep or quit leaves an exact "left" list.
  * Resume = run the same sources again. Already-renamed clips are skipped (rename-log / notes store), so nothing is
    processed twice and nothing is overwritten.
  * If a folder is moved or renamed during a run (or between runs), a clip that is no longer at its path is looked
    up by name + size under the same DaVinci Resolve folder (or the source's parent) and processed at its new place.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

NAME = "inplace-run.json"
MAX_DEPTH = 6
SKIP_DIRS = {"CacheClip", "ProxyMedia", "OptimizedMedia", "BackUps", ".gallery"}


def path(cfg: dict) -> Path:
    from _common import abs_path
    return abs_path(cfg.get("logs_dir") or "logs") / NAME


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read(cfg: dict) -> dict | None:
    try:
        return json.loads(path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write(cfg: dict, d: dict) -> None:
    from _common import atomic_write_text
    p = path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps(d, indent=2, ensure_ascii=False) + "\n")


def start(cfg: dict, sources: list[str], confirm: bool, dry: bool, videos: list[Path], skipped: int) -> dict:
    d = {"schema": 1, "state": "running", "pid": os.getpid(), "started_at": _now(), "updated_at": _now(),
         "sources": [str(s) for s in sources], "confirm_resolve": bool(confirm), "dry_run": bool(dry),
         "total": len(videos) + skipped, "already_done": skipped,
         "remaining": [str(v) for v in videos], "done": [], "failed": [], "moved": {}}
    _write(cfg, d)
    return d


def mark(cfg: dict, d: dict, original: Path, now_at: Path, ok: bool, why: str = "") -> None:
    key = str(original)
    if key in d["remaining"]:
        d["remaining"].remove(key)
    (d["done"] if ok else d["failed"]).append({"path": str(now_at), **({"why": why} if why else {})})
    d["updated_at"] = _now()
    _write(cfg, d)


def note_moved(cfg: dict, d: dict, old: Path, new: Path) -> None:
    d.setdefault("moved", {})[str(old)] = str(new)
    _write(cfg, d)


def finish(cfg: dict, d: dict, state: str = "complete") -> None:
    d["state"] = state
    d["finished_at"] = _now()
    d["updated_at"] = d["finished_at"]
    _write(cfg, d)


def summary(cfg: dict) -> dict:
    """For ClipGauge: {interrupted, left, total, sources (relocated if they moved), confirm_resolve, dry_run, ...}."""
    d = read(cfg)
    if not d:
        return {"interrupted": False}
    alive = False
    try:
        if d.get("state") == "running" and d.get("pid"):
            os.kill(int(d["pid"]), 0)
            alive = True
    except (OSError, ValueError):
        alive = False
    left = list(d.get("remaining", [])) + [f["path"] for f in d.get("failed", [])]
    interrupted = d.get("state") == "running" and not alive and bool(left)
    srcs = []
    for s in d.get("sources", []):
        p = Path(s)
        if not p.exists():
            q = relocate(p, None, cfg, is_dir=None)
            srcs.append(str(q) if q else s)
        else:
            srcs.append(s)
    return {"interrupted": interrupted, "state": d.get("state"), "running": alive, "left": len(left),
            "total": d.get("total", 0), "done": len(d.get("done", [])), "failed": len(d.get("failed", [])),
            "sources": srcs, "confirm_resolve": d.get("confirm_resolve", False), "dry_run": d.get("dry_run", False),
            "updated_at": d.get("updated_at"), "path": str(path(cfg))}


def dismiss(cfg: dict) -> bool:
    d = read(cfg)
    if not d or d.get("state") != "running":
        return False
    finish(cfg, d, "dismissed")
    return True


def _search_roots(p: Path) -> list[Path]:
    parts = p.parts
    for i in range(len(parts) - 1, 0, -1):
        if parts[i] == "DaVinci Resolve":
            return [Path(*parts[: i + 1])]
    anc = [a for a in p.parents if a.exists()]
    return anc[:1] + ([anc[0].parent] if anc and anc[0].parent != anc[0] else [])


def relocate(p: Path, size: int | None, cfg: dict | None = None, is_dir: bool | None = False,
             cache: dict | None = None) -> Path | None:
    """Where did `p` go? Unique match by name (+ size for files) under the same Resolve folder / nearby. The new
    place must still be allowed for in-place renaming (inplace.classify). None if not found or ambiguous."""
    p = Path(p)
    if cache is not None:
        for old_dir, new_dir in cache.items():
            if str(p).startswith(old_dir + os.sep):
                q = Path(new_dir + str(p)[len(old_dir):])
                if q.exists():
                    return q
    hits: list[Path] = []
    for root in _search_roots(p):
        if not root.is_dir():
            continue
        base = len(root.parts)
        for dp, dn, fn in os.walk(root):
            dn[:] = [d for d in dn if not d.startswith(".") and d not in SKIP_DIRS
                     and not d.lower().endswith((".app", ".bundle", ".photoslibrary", ".fcpbundle"))]
            if len(Path(dp).parts) - base >= MAX_DEPTH:
                dn[:] = []
            names = dn if is_dir else fn
            if is_dir is None:
                names = list(dn) + list(fn)
            if p.name in names:
                q = Path(dp) / p.name
                if q == p:
                    continue
                if size is not None and q.is_file() and q.stat().st_size != size:
                    continue
                hits.append(q)
        if hits:
            break
    if len(hits) != 1:
        return None
    q = hits[0]
    if cfg is not None:
        try:
            import inplace
            if inplace.classify(q, cfg, inplace.sort_cfg(cfg))["status"] == "refused":
                return None
        except Exception:  # noqa: BLE001
            return None
    if cache is not None and q.parent != p.parent:
        cache[str(p.parent)] = str(q.parent)
    return q
