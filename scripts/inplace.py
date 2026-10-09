#!/usr/bin/env python3
"""
"Process in place" guard (ClipGauge v0.6): may clips at PATH be renamed where they are, without copying to inbox/?

  python3 scripts/inplace.py PATH [PATH ...]   # JSON: per path ok / needs_confirm / refused + reason, and the media found

Rules (checked for the path itself and every parent folder):
  * refused  — a folder on hold (config/project-sort.json hold_sources: Held Project, Archive Footage, …) anywhere in
               the path; DaVinci Resolve's own folders (protected_dirs: BackUps, CacheClip, ProxyMedia, …) or any
               hidden folder; a Blackmagic Cloud-synced project (.syncprojectinfo.json in any parent); iCloud Drive /
               Dropbox / Google Drive / OneDrive folders (renaming there syncs to every device); this project's own
               folders (scripts, logs, notes, models, processing, exports, config, app); apps/packages (*.app …).
  * needs_confirm — under the DaVinci Resolve folder or a sidecar.protected_path_markers path: Resolve links media by
               path, so renaming clips it already imported makes them go offline. The caller must ask and then pass
               --confirm-resolve (run_pipeline.py) / allow_protected=True (apply_renames.apply_clip).
  * ok       — anything else on a local disk.
Inbox top level is not "in place" (that is the normal flow) but is reported ok.
"""
from __future__ import annotations

import json
import os
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import ROOT, load_config, section  # noqa: E402
import notes_store as ns  # noqa: E402

MAX_FILES = 2000
MAX_DEPTH = 6
INTERNAL_DIRS = ("scripts", "logs", "notes", "models", "processing", "exports", "config", "app")
PACKAGE_SUFFIXES = (".app", ".drp", ".dra", ".photoslibrary", ".fcpbundle", ".bundle", ".framework", ".pkg")
CLOUD_MARKERS = ("/Library/Mobile Documents/", "/Library/CloudStorage/", "/Dropbox/", "/Google Drive/",
                 "/OneDrive/", "/iCloud Drive/")


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s).lower()


def sort_cfg(cfg: dict) -> dict:
    try:
        import sort_projects as sp
        return sp.sort_config(cfg)
    except Exception:  # noqa: BLE001  — fall back to built-in defaults (same as config/project-sort.json)
        return {"hold_sources": ["Held Project", "Archive Footage", "Old Card Dump"],
                "protected_dirs": ["BackUps", "CacheClip", ".gallery", ".blackmagicsync-v2", "ProxyMedia",
                                   "OptimizedMedia"],
                "sync_marker": ".syncprojectinfo.json", "resolve_root": None}


def resolve_root(cfg: dict, sc: dict) -> Path:
    r = sc.get("resolve_root")
    return Path(r) if r else ns.volume_root(cfg) / "DaVinci Resolve"


def _inside(p: Path, root: Path) -> bool:
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False


def classify(path: str | os.PathLike, cfg: dict | None = None, sc: dict | None = None) -> dict:
    """{"path", "status": ok|needs_confirm|refused, "reason", "in_inbox": bool}. Never touches the file system
    beyond stat/exists checks."""
    cfg = cfg or load_config()
    sc = sc or sort_cfg(cfg)
    p = Path(path).expanduser()
    try:
        p = p.resolve()
    except OSError:
        pass
    out = {"path": str(p), "status": "ok", "reason": "", "in_inbox": False}

    def refuse(why: str) -> dict:
        return {**out, "status": "refused", "reason": why}

    if not p.exists():
        return refuse("not found")
    s = str(p) + ("/" if p.is_dir() else "")
    held = {_nfc(h) for h in sc.get("hold_sources") or []}
    prot = set(sc.get("protected_dirs") or [])
    parts = p.parts[1:]
    for part in parts:
        if _nfc(part) in held:
            return refuse(f"“{part}” is on hold (hold_sources in config/project-sort.json) — not touching it")
        if part in prot:
            return refuse(f"“{part}” is one of DaVinci Resolve's own folders — never touched")
        if part.startswith("."):
            return refuse(f"hidden item “{part}” — skipped")
        if part.lower().endswith(PACKAGE_SUFFIXES):
            return refuse(f"“{part}” is an app or package — not touching what's inside it")
    for m in CLOUD_MARKERS:
        if m in s:
            return refuse("cloud-synced folder (iCloud / Dropbox / Google Drive / OneDrive) — renaming there syncs to "
                          "every device; copy the clips into the inbox instead")
    root = ROOT.resolve()
    if _inside(p, root):
        inbox = (root / (cfg.get("inbox_dir") or "inbox")).resolve()
        if _inside(p, inbox):
            out["in_inbox"] = True
            if p.is_file() and p.parent == inbox:
                return {**out, "reason": "already in the inbox (normal processing)"}
        elif p == root:
            return refuse("this is the renamer's own project folder — choose a footage folder")
        else:
            rel = p.relative_to(root).parts
            if rel and (rel[0] in INTERNAL_DIRS or rel[0].endswith(".app")):
                return refuse(f"the renamer's own “{rel[0]}” folder — never processed")
    else:
        # Any other copy of the renamer (e.g. the real project while ROOT points at a test copy, or a second
        # project made by Setup › Create Project): its own folders are never processed in place either.
        for anc in [p, *p.parents]:
            try:
                is_proj = (anc / "scripts" / "run_pipeline.py").is_file()
            except OSError:
                is_proj = False
            if is_proj:
                if p == anc:
                    return refuse("this is a renamer project folder — choose a footage folder")
                rel0 = p.relative_to(anc).parts[0]
                if rel0 in INTERNAL_DIRS or rel0.endswith(".app"):
                    return refuse(f"a renamer project's own “{rel0}” folder — never processed")
                if rel0 in ("inbox", Path(cfg.get("inbox_dir") or "inbox").name):
                    return refuse("another renamer project's inbox — open that project in ClipGauge instead")
                break
            if anc == Path(anc.anchor):
                break
    marker = sc.get("sync_marker") or ".syncprojectinfo.json"
    d = p if p.is_dir() else p.parent
    for anc in [d, *d.parents]:
        try:
            if (anc / marker).exists():
                return refuse(f"“{anc.name}” is a Blackmagic Cloud-synced project ({marker}) — never touched")
        except OSError:
            break
        if anc == Path(anc.anchor):
            break
    rr = resolve_root(cfg, sc)
    try:
        rr = rr.resolve()
    except OSError:
        pass
    markers = [m for m in section(cfg, "sidecar").get("protected_path_markers") or [] if m]
    if _inside(p, rr) or any(m in s for m in markers):
        return {**out, "status": "needs_confirm",
                "reason": "inside the DaVinci Resolve folder — renaming clips Resolve has already imported makes them "
                          "go offline in your projects (Resolve links media by file path). Only continue if these "
                          "clips are not in a Resolve project yet."}
    return out


def media_in(path: str | os.PathLike, cfg: dict | None = None, sc: dict | None = None,
             limit: int = MAX_FILES) -> tuple[list[Path], list[dict]]:
    """Media files at PATH (a file, or a folder walked up to MAX_DEPTH) that may be processed in place, and the
    sub-items refused on the way (held / protected / synced / hidden folders are not descended into)."""
    cfg = cfg or load_config()
    sc = sc or sort_cfg(cfg)
    p = Path(path).expanduser().resolve()
    refused: list[dict] = []
    found: list[Path] = []
    top = classify(p, cfg, sc)
    if top["status"] == "refused":
        return [], [top]
    if p.is_file():
        if p.suffix.lower() in ns.MEDIA_EXTS and not p.name.startswith("."):
            return [p], []
        return [], [{**top, "status": "refused", "reason": "not a video or photo"}]
    marker = sc.get("sync_marker") or ".syncprojectinfo.json"
    held = {_nfc(h) for h in sc.get("hold_sources") or []}
    prot = set(sc.get("protected_dirs") or [])
    base_depth = len(p.parts)
    for dirpath, dirnames, filenames in os.walk(p):
        d = Path(dirpath)
        keep = []
        for n in sorted(dirnames):
            sub = d / n
            if n.startswith(".") or n in prot or n.lower().endswith(PACKAGE_SUFFIXES):
                continue
            if _nfc(n) in held:
                refused.append({"path": str(sub), "status": "refused", "reason": f"“{n}” is on hold — skipped"})
                continue
            if (sub / marker).exists():
                refused.append({"path": str(sub), "status": "refused", "reason": f"“{n}” is Cloud-synced — skipped"})
                continue
            if len(sub.parts) - base_depth >= MAX_DEPTH:
                continue
            keep.append(n)
        dirnames[:] = keep
        for f in sorted(filenames):
            if f.startswith(".") or Path(f).suffix.lower() not in ns.MEDIA_EXTS:
                continue
            found.append(d / f)
            if len(found) >= limit:
                refused.append({"path": str(p), "status": "refused",
                                "reason": f"more than {limit} media files — only the first {limit} are used"})
                return found, refused
    return found, refused


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    cfg = load_config()
    sc = sort_cfg(cfg)
    out = []
    for a in argv:
        c = classify(a, cfg, sc)
        files, skipped = media_in(a, cfg, sc) if c["status"] != "refused" else ([], [])
        out.append({**c, "media": [str(f) for f in files], "skipped": skipped})
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
