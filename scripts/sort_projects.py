#!/usr/bin/env python3
"""Sort clips into Resolve project folders: <resolve_root>/<Project>/{A-Roll,B-Roll,Images,_Notes,_Review,Exports}.

  python3 scripts/sort_projects.py --list-sources [--json]
  python3 scripts/sort_projects.py --source "/Volumes/Lexar/DaVinci Resolve/Some Folder" --dry-run [--json]
  python3 scripts/sort_projects.py --source inbox --dry-run            # the renamer's inbox/
  python3 scripts/sort_projects.py --apply --plan logs/sort-plans/sort-plan-….json [--edits edits.json] [--yes]
  python3 scripts/sort_projects.py --undo [--plan-id ID] [--dry-run] [--yes]   # default: the last applied sort

Always a dry run first: the plan (logs/sort-plans/*.json) groups clips by project with clip count, time range, a
one-line description, the destination and whether the project is new or existing. Nothing moves until --apply is run
on that plan (ClipGauge › Sort into Projects… › Apply asks first). Edits (rename / exclude / merge into an existing
project / accept a suggested split) are applied on top of the plan.

Rules: project = the source folder's name unless it's uninformative (CAM_ dump, date-only, "New Folder"...), then
the clips are split by time (gap_minutes / day change), checked against their content (notes store / describe
results) and matched to existing projects. Type sort per config/project-sort.json (type_map). Low confidence ->
_Review. Sidecars (.json/.md) -> _Notes. Never overwrites (suffix _2, _3… or skip). Every apply writes an undo map
(logs/sort-undo-<plan_id>.jsonl). Refuses to apply while DaVinci Resolve is open, a processing run or an update is
active. Never touches Resolve's own folders, Blackmagic Cloud-synced projects or folders on hold (hold_sources).
"""
from __future__ import annotations

import argparse
import errno
import json
import math
import os
import re
import sys
import time
import unicodedata
from collections import Counter
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import ROOT, abs_path, atomic_write_text, load_config, section, slugify  # noqa: E402
import instructions as ins  # noqa: E402
import notes_store as ns  # noqa: E402
import pipeline_lock as pl  # noqa: E402

MARKER = "sort_projects"
EXIT_BLOCKED = 4
VIDEO_EXTS, PHOTO_EXTS = ns.VIDEO_EXTS, ns.PHOTO_EXTS
SIDECAR_EXTS = {".json", ".md"}
CAM_RE = re.compile(r"(?:^|[_-])(20\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(?:[_-]|$)")
DATE_PREFIX_RE = re.compile(r"^(20\d{2})(\d{2})(\d{2})_")
TAKE_RE = re.compile(r"^t\d{1,3}$")
STOP = {"the", "a", "an", "and", "of", "in", "on", "at", "to", "with", "for", "is", "are", "person", "people", "video",
        "clip", "camera", "shot", "view", "scene", "footage", "broll", "b", "roll", "talking", "head", "close", "up",
        "table", "hand", "hands", "showing", "man", "woman", "device", "setup", "mp4", "mov", "jpg"}
DEFAULTS = {
    "buckets": {"a_roll": "A-Roll", "b_roll": "B-Roll", "images": "Images", "notes": "_Notes", "review": "_Review",
                "exports": "Exports"},
    "type_map": {"talking-head": "a_roll", "unboxing": "a_roll", "menu": "a_roll", "screen-recording": "a_roll",
                 "screen": "a_roll", "broll": "b_roll", "pan": "b_roll", "cutaway": "b_roll", "bench": "b_roll",
                 "boot": "b_roll", "fail": "b_roll", "gameplay": "b_roll", "photo": "images"},
    "default_bucket": "b_roll", "review_threshold": 0.6, "gap_minutes": 60, "split_on_day_change": True,
    "merge_similar_groups": 0.5, "two_shoot_similarity": 0.12, "match_existing_min_score": 0.34,
    "slate": {"enabled": True, "max_seconds": 8, "keywords": ["box", "packaging", "slate", "name card", "title card"]},
    "on_conflict": "suffix", "respect_existing_buckets": True,
    "protected_dirs": ["BackUps", "CacheClip", ".gallery", ".blackmagicsync-v2", "ProxyMedia", "OptimizedMedia"],
    "sync_marker": ".syncprojectinfo.json", "hold_sources": [],
    "uninformative_name_patterns": [r"^cam([_ -]|\d|$)", r"^dcim$", r"^\d{4}-\d{2}-\d{2}$", r"^\d{8}$", r"^new folder",
                                    r"^untitled", r"^inbox$", r"^footage$"],
}
RELINK_WARNING = ("Clips already imported into a DaVinci Resolve project go offline when they move. After applying, "
                  "relink them in Resolve (Media Pool › select › right-click › Relink Selected Clips…), or exclude "
                  "those groups.")


class Refused(Exception):
    def __init__(self, msg: str, code: int = EXIT_BLOCKED):
        super().__init__(msg)
        self.code = code


# ---------------------------------------------------------------------------
# config / places
# ---------------------------------------------------------------------------

def sort_config(cfg: dict) -> dict:
    root = Path(cfg.get("_root") or ROOT)
    try:
        user = json.loads((root / "config" / "project-sort.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        user = {}
    sc = {**DEFAULTS, **{k: v for k, v in user.items() if not k.startswith("_") and v is not None}}
    sc["buckets"] = {**DEFAULTS["buckets"], **(user.get("buckets") or {})}
    sc["type_map"] = {**DEFAULTS["type_map"], **(user.get("type_map") or {})}
    sc["slate"] = {**DEFAULTS["slate"], **(user.get("slate") or {})}
    if cfg.get("_sort_overrides"):
        sc.update(cfg["_sort_overrides"])
    return sc


def resolve_root(cfg: dict, sc: dict) -> Path:
    r = sc.get("resolve_root")
    return Path(r) if r else ns.volume_root(cfg) / "DaVinci Resolve"


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def is_synced(d: Path, sc: dict) -> bool:
    return (d / sc["sync_marker"]).exists()


def held_name(name: str, sc: dict) -> bool:
    return nfc(name).lower() in {nfc(h).lower() for h in sc.get("hold_sources") or []}


def guard_path(p: Path, cfg: dict, sc: dict, what: str) -> None:
    """Refuse Resolve's own folders, Cloud-synced projects and folders on hold (p itself or any parent below root)."""
    rr = resolve_root(cfg, sc)
    try:
        rel = p.resolve().relative_to(rr.resolve())
    except ValueError:
        rel = None
    parts = list(rel.parts) if rel else []
    if rel is not None and parts:
        top = rr / parts[0]
        if held_name(parts[0], sc):
            raise Refused(f"{what} “{parts[0]}” is on hold (hold_sources in config/project-sort.json) — not touching it.")
        if parts[0] in sc["protected_dirs"] or parts[0].startswith("."):
            raise Refused(f"{what} “{parts[0]}” is one of DaVinci Resolve's own folders — never touched.")
        if is_synced(top, sc):
            raise Refused(f"{what} “{parts[0]}” is a Blackmagic Cloud-synced project ({sc['sync_marker']}) — never touched.")
    elif held_name(p.name, sc):
        raise Refused(f"{what} “{p.name}” is on hold (hold_sources in config/project-sort.json) — not touching it.")


def bucket_names(sc: dict) -> set[str]:
    return set(sc["buckets"].values())


def is_project_dir(d: Path, sc: dict) -> bool:
    b = sc["buckets"]
    return any((d / b[k]).is_dir() for k in ("a_roll", "b_roll", "images"))


def list_sources(cfg: dict, sc: dict | None = None) -> list[dict]:
    sc = sc or sort_config(cfg)
    rr = resolve_root(cfg, sc)
    out = [{"name": "inbox", "path": str(ns.inbox_dir(cfg)), "kind": "inbox", "sortable": True, "note": "the renamer's inbox/"}]
    if rr.is_dir():
        for e in sorted(rr.iterdir(), key=lambda x: x.name.lower()):
            if not e.is_dir() or e.name.startswith("._"):
                continue
            item = {"name": e.name, "path": str(e)}
            if e.name.startswith(".") or e.name in sc["protected_dirs"]:
                item.update(kind="resolve", sortable=False, note="DaVinci Resolve's own folder")
            elif held_name(e.name, sc):
                item.update(kind="held", sortable=False, note="on hold (hold_sources in config/project-sort.json)")
            elif is_synced(e, sc):
                item.update(kind="synced", sortable=False, note="Blackmagic Cloud-synced project — never touched")
            elif is_project_dir(e, sc):
                item.update(kind="project", sortable=True, note="project in the A-Roll/B-Roll/Images layout")
            else:
                item.update(kind="raw", sortable=True, note="folder of clips (not sorted yet)")
            out.append(item)
    return out


def existing_projects(cfg: dict, sc: dict) -> list[Path]:
    rr = resolve_root(cfg, sc)
    if not rr.is_dir():
        return []
    out = []
    for e in sorted(rr.iterdir(), key=lambda x: x.name.lower()):
        if (e.is_dir() and not e.name.startswith(".") and e.name not in sc["protected_dirs"]
                and not held_name(e.name, sc) and not is_synced(e, sc) and is_project_dir(e, sc)):
            out.append(e)
    return out


def uninformative(name: str, sc: dict) -> bool:
    n = nfc(name).strip().lower()
    return not n or any(re.search(p, n) for p in sc["uninformative_name_patterns"])


# ---------------------------------------------------------------------------
# clip facts (notes store / sidecars / names)
# ---------------------------------------------------------------------------

class Store:
    """Notes-store lookups: by current path, else by any known name of the clip with the same byte size."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        try:
            self.idx = ns.build_index(cfg)
        except Exception:  # noqa: BLE001
            self.idx = {"records": [], "by_current": {}}
        self.by_name: dict[str, list[dict]] = {}
        for r in self.idx["records"]:
            names = {(r.get("source") or {}).get("name"), (r.get("proposal") or {}).get("new_name")}
            for p in (r.get("current_path"), (r.get("applied") or {}).get("new_path")):
                if p:
                    names.add(Path(p).name)
            for n in names:
                if n:
                    self.by_name.setdefault(nfc(n).lower(), []).append(r)

    def find(self, p: Path, size: int | None) -> dict | None:
        r = self.idx["by_current"].get(ns.norm(p))
        if r:
            return r
        for r in self.by_name.get(nfc(p.name).lower(), []):
            s = (r.get("source") or {}).get("size_bytes")
            if size is None or not isinstance(s, int) or s == size:
                return r
        return None


def local_dt(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d.astimezone().replace(tzinfo=None) if d.tzinfo else d


def name_type(stem: str, sc: dict) -> str | None:
    """Clip type from the renamer's pattern {date}_{project}_{subject}_{type}[_t##]."""
    parts = stem.split("_")
    if len(parts) >= 4 and DATE_PREFIX_RE.match(stem + "_"):
        last = parts[-1].lower()
        if TAKE_RE.match(last) and len(parts) >= 5:
            last = parts[-2].lower()
        if last in sc["type_map"]:
            return last
    return None


def tokens(*texts) -> Counter:
    c: Counter = Counter()
    for t in texts:
        if not t:
            continue
        if isinstance(t, (list, tuple)):
            for x in t:
                c.update(tokens(x))
            continue
        for w in re.split(r"[^a-z0-9]+", nfc(str(t)).lower()):
            if len(w) > 2 and w not in STOP and not w.isdigit():
                c[w] += 1
    return c


def cosine(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    dot = sum(v * b.get(k, 0) for k, v in a.items())
    na, nb = math.sqrt(sum(v * v for v in a.values())), math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def probe_seconds(p: Path, cfg: dict) -> tuple[float | None, datetime | None]:
    try:
        from media import probe_media
        from _common import tool_path
        info = probe_media(tool_path(cfg, "ffprobe_path", "ffprobe"), p)
        return info.get("duration"), local_dt(info.get("creation_time"))
    except Exception:  # noqa: BLE001
        return None, None


def clip_facts(p: Path, cfg: dict, sc: dict, store: Store, probe: bool = True) -> dict:
    ext = p.suffix.lower()
    st = p.stat()
    f = {"src": str(p), "name": p.name, "size": st.st_size, "mtime": st.st_mtime,
         "kind": "photo" if ext in PHOTO_EXTS else "video"}
    rec = store.find(p, st.st_size)
    side = None
    for cand in (p.with_suffix(".json"), p.parent.parent / sc["buckets"]["notes"] / (p.stem + ".json")):
        if cand.is_file():
            try:
                side = json.loads(cand.read_text(encoding="utf-8"))
                break
            except (OSError, ValueError):
                pass
    meta = rec or side or {}
    desc = ((meta.get("describe") or {}).get("description")) or {}
    prop = meta.get("proposal") or {}
    src = meta.get("source") or {}
    f["clip_id"] = rec.get("clip_id") if rec else None
    f["processed"] = bool(meta)
    f["summary"] = desc.get("summary")
    f["keywords"] = list(desc.get("keywords") or [])[:12]
    f["subjects"] = list(desc.get("subjects") or [])[:8]
    f["on_screen_text"] = list(desc.get("on_screen_text") or [])[:8]
    f["suggested_project"] = desc.get("suggested_project")
    f["duration"] = src.get("duration_s")
    conf = desc.get("confidence")
    f["confidence"] = float(conf) if isinstance(conf, (int, float)) else None
    f["needs_review"] = bool(prop.get("needs_review")) or (meta.get("applied") or {}).get("action") == "needs-review"
    ctype = (desc.get("clip_type") or "").lower() or None
    f["type_source"] = "notes" if ctype else None
    if not ctype:
        ctype = name_type(p.stem, sc)
        f["type_source"] = "name" if ctype else None
    if f["kind"] == "photo":
        ctype, f["type_source"] = "photo", f["type_source"] or "extension"
    f["clip_type"] = ctype
    # time: camera name > creation_time (store) > ffprobe > mtime
    m = CAM_RE.search(p.stem) or CAM_RE.search(str(src.get("name") or ""))
    t = datetime(*map(int, m.groups())) if m else None
    f["time_source"] = "camera name" if t else None
    if t is None:
        t = local_dt(src.get("creation_time"))
        f["time_source"] = "creation time" if t else None
    if (t is None or f["duration"] is None) and probe and f["kind"] == "video":
        dur, ct = probe_seconds(p, cfg)
        f["duration"] = f["duration"] or dur
        if t is None and ct:
            t, f["time_source"] = ct, "media metadata"
    if t is None:
        t, f["time_source"] = datetime.fromtimestamp(st.st_mtime), "file date"
    f["time"] = t.isoformat(timespec="seconds")
    f["sig"] = dict(tokens(f["suggested_project"], f["suggested_project"], f["keywords"], f["subjects"], f["on_screen_text"]))
    return f


def bucket_for(f: dict, sc: dict) -> tuple[str, str]:
    """(bucket key, reason)."""
    if f["kind"] == "photo":
        return "images", "photo"
    if not f["processed"] and not f["clip_type"]:
        return "review", "no notes for this clip (not processed by the renamer) — check it by hand"
    if f["needs_review"]:
        return "review", "the renamer flagged it for review"
    th = float(sc["review_threshold"])
    if f["confidence"] is not None and f["confidence"] < th:
        return "review", f"low confidence ({f['confidence']:.2f} < {th})"
    if not f["clip_type"]:
        return "review", "clip type unknown"
    key = sc["type_map"].get(f["clip_type"], sc["default_bucket"])
    return key, f"{f['clip_type']} ({f['type_source']})"


def is_slate(f: dict, sc: dict) -> bool:
    s = sc["slate"]
    if not s.get("enabled") or f["kind"] != "video" or not f.get("on_screen_text"):
        return False
    if f.get("duration") is None or f["duration"] > float(s.get("max_seconds", 8)):
        return False
    text = " ".join([f.get("summary") or ""] + f["keywords"] + f["subjects"]).lower()
    return any(k in text for k in s.get("keywords", [])) or len(f["on_screen_text"]) >= 1


ACRONYMS = {"tv", "pc", "ui", "usb", "vr", "ai", "diy", "gps", "hdmi", "led", "nas", "ssd", "vpn", "wifi", "4k", "8k"}


def pretty(slug: str) -> str:
    """'gl-inet-router-setup' -> 'Gl Inet Router Setup' (known acronyms upper-cased)."""
    words = [w for w in re.split(r"[-_\s]+", slug or "") if w]
    return " ".join(w.upper() if w.lower() in ACRONYMS else w[:1].upper() + w[1:] for w in words)


def clean_project_name(name: str) -> str:
    n = nfc(name).replace("/", "-").replace(":", "-").strip().strip(".")
    n = re.sub(r"\s+", " ", n)
    return n[:80]


# ---------------------------------------------------------------------------
# grouping
# ---------------------------------------------------------------------------

def time_groups(facts: list[dict], sc: dict, slates: bool) -> list[list[dict]]:
    gap = timedelta(minutes=float(sc["gap_minutes"]))
    out: list[list[dict]] = []
    prev_t = None
    for f in sorted(facts, key=lambda x: (x["time"], x["name"])):
        t = datetime.fromisoformat(f["time"])
        new = prev_t is None or (t - prev_t) > gap or (sc["split_on_day_change"] and t.date() != prev_t.date())
        if not new and slates and f.get("slate") and out and len(out[-1]) > 0:
            new = True
        if new:
            out.append([])
        out[-1].append(f)
        prev_t = t
    return out


def sig_of(fs: list[dict]) -> Counter:
    c: Counter = Counter()
    for f in fs:
        c.update(f.get("sig") or {})
    return c


def merge_similar(groups: list[list[dict]], sc: dict) -> tuple[list[list[dict]], list[str]]:
    """Adjacent same-day groups whose content matches are one shoot with a long break."""
    notes = []
    out: list[list[dict]] = []
    for g in groups:
        if out:
            a, b = out[-1], g
            same_day = a[-1]["time"][:10] == b[0]["time"][:10]
            sim = cosine(sig_of(a), sig_of(b))
            if same_day and sim >= float(sc["merge_similar_groups"]) and not b[0].get("slate"):
                notes.append(f"joined clips from {a[-1]['time'][11:16]} and {b[0]['time'][11:16]} — same content after a break (similarity {sim:.2f})")
                out[-1] = a + b
                continue
        out.append(g)
    return out, notes


def two_shoots(fs: list[dict], sc: dict) -> dict | None:
    """Best time-ordered split point where both halves look different. None if it looks like one shoot."""
    if len(fs) < 4:
        return None
    best = None
    for i in range(2, len(fs) - 1):
        left, right = fs[:i], fs[i:]
        if not sig_of(left) or not sig_of(right):
            continue
        sim = cosine(sig_of(left), sig_of(right))
        t0, t1 = datetime.fromisoformat(left[-1]["time"]), datetime.fromisoformat(right[0]["time"])
        gap_min = (t1 - t0).total_seconds() / 60
        score = (1 - sim) + min(gap_min, 240) / 480
        if sim <= float(sc["two_shoot_similarity"]) and (best is None or score > best["score"]):
            best = {"index": i, "similarity": round(sim, 2), "gap_minutes": round(gap_min), "score": score,
                    "at": right[0]["time"]}
    return best


def describe_group(fs: list[dict]) -> str:
    sums = [f["summary"] for f in fs if f.get("summary")]
    sig = sig_of(fs)
    top = [w for w, _ in sig.most_common(5)]
    vids = sum(1 for f in fs if f["kind"] == "video")
    if sums:
        first = sums[0].rstrip(".")
        return (first[:110] + ("…" if len(first) > 110 else "")) + (f" · themes: {', '.join(top[:4])}" if top else "")
    if top:
        return "Themes: " + ", ".join(top)
    names = {re.sub(r"_t\d+$", "", Path(f["name"]).stem).split("_")[1] for f in fs
             if DATE_PREFIX_RE.match(f["name"]) and len(Path(f["name"]).stem.split("_")) >= 3}
    if names:
        return "From file names: " + ", ".join(sorted(names)[:4])
    return f"{vids} video(s) without notes — run them through the renamer for a description"


_GLOSSARY: list[str] = []   # set by build_plan from Instructions: fixes spellings in proposed names


def content_names(fs: list[dict]) -> list[str]:
    """Candidate names: slate text, most common suggested project, top keywords, date (glossary spellings applied)."""
    out = []
    sl = next((f for f in fs if f.get("slate")), None)
    if sl:
        txt = " ".join(t for t in sl["on_screen_text"][:3] if len(t) <= 40).strip()
        if txt:
            out.append(clean_project_name(txt.title() if txt.isupper() else txt))
    sp = Counter(f["suggested_project"] for f in fs if f.get("suggested_project"))
    for s, _ in sp.most_common(2):
        out.append(pretty(s))
    top = [w for w, _ in sig_of(fs).most_common(3)]
    if top:
        out.append(pretty("-".join(top[:2])))
    t = datetime.fromisoformat(fs[0]["time"])
    out.append(f"Shoot {t:%Y-%m-%d %H%M}")
    seen, uniq = set(), []
    for n in out:
        n = ins.apply_spelling(n, _GLOSSARY) if _GLOSSARY else n
        if n and n.lower() not in seen:
            seen.add(n.lower())
            uniq.append(n)
    return uniq


def project_signature(d: Path, sc: dict, limit: int = 300) -> Counter:
    c = tokens(d.name, d.name, d.name)
    n = 0
    for b in ("a_roll", "b_roll", "images", "notes"):
        bd = d / sc["buckets"][b]
        if not bd.is_dir():
            continue
        for e in bd.iterdir():
            if e.name.startswith(".") or n >= limit:
                continue
            n += 1
            stem = e.stem
            parts = stem.split("_")
            c.update(tokens(parts[1:3] if DATE_PREFIX_RE.match(stem + "_") else stem))
            if b == "notes" and e.suffix == ".json" and n <= 120:
                try:
                    dsc = ((json.loads(e.read_text(encoding="utf-8")).get("describe") or {}).get("description")) or {}
                    c.update(tokens(dsc.get("keywords"), dsc.get("suggested_project")))
                except (OSError, ValueError):
                    pass
    return c


def match_existing(fs: list[dict], names: list[str], projects: dict[str, Counter], sc: dict) -> list[dict]:
    g = sig_of(fs)
    out = []
    for pname, psig in projects.items():
        name_sim = max((SequenceMatcher(None, slugify(n, 60), slugify(pname, 60)).ratio() for n in names), default=0)
        content = cosine(g, psig)
        score = max(name_sim if name_sim >= 0.75 else name_sim * 0.6, content)
        if score >= float(sc["match_existing_min_score"]):
            why = f"name {name_sim:.0%} similar" if name_sim >= content else f"content {content:.0%} similar"
            out.append({"project": pname, "score": round(score, 2), "why": why})
    return sorted(out, key=lambda x: -x["score"])[:3]


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------

def walk_media(src: Path, sc: dict, skip_dirs: set[str], left_out: list[str] | None = None) -> list[Path]:
    out = []
    stack = [src]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(d.iterdir(), key=lambda x: x.name)
        except OSError:
            continue
        for e in entries:
            if e.name.startswith("."):
                continue
            if e.is_dir():
                if held_name(e.name, sc):          # checked first: a held folder is not even looked into
                    if left_out is not None:
                        left_out.append(f"Left out “{e.name}” — on hold (hold_sources).")
                    continue
                if e.name in skip_dirs or e.name in sc["protected_dirs"] or is_synced(e, sc):
                    continue
                stack.append(e)
            elif e.suffix.lower() in VIDEO_EXTS | PHOTO_EXTS:
                out.append(e)
    return out


def sidecars_of(p: Path) -> list[Path]:
    return [q for q in (p.with_suffix(".json"), p.with_suffix(".md")) if q.is_file()]


def plan_items(fs: list[dict], dest_project: Path, sc: dict, verify_root: Path | None) -> list[dict]:
    items = []
    bnames = bucket_names(sc)
    for f in fs:
        p = Path(f["src"])
        key, why = bucket_for(f, sc)
        bucket = sc["buckets"][key]
        action = "move"
        if verify_root is not None and p.parent.parent == verify_root and p.parent.name in bnames:
            if p.parent.name == bucket:
                action, why = "keep", f"already in {bucket}/"
            elif sc.get("respect_existing_buckets", True):
                action, why = "keep", f"left in {p.parent.name}/ (sorter would say {bucket}/: {why})"
                bucket = p.parent.name
        it = {"src": f["src"], "name": f["name"], "kind": f["kind"], "clip_type": f["clip_type"],
              "confidence": f["confidence"], "bucket": bucket, "action": action, "reason": why,
              "clip_id": f.get("clip_id"), "size": f["size"], "mtime": f["mtime"], "time": f["time"],
              "time_source": f["time_source"], "slate": bool(f.get("slate")),
              "dest": str(dest_project / bucket / f["name"])}
        items.append(it)
        if action == "move":
            for s in sidecars_of(p):
                items.append({"src": str(s), "name": s.name, "kind": "sidecar", "bucket": sc["buckets"]["notes"],
                              "action": "move", "reason": f"notes for {p.name}", "size": s.stat().st_size,
                              "mtime": s.stat().st_mtime, "dest": str(dest_project / sc["buckets"]["notes"] / s.name),
                              "time": f["time"], "of": f["name"]})
    return items


def make_group(gid: str, fs: list[dict], project: str, name_source: str, suggestions: list[str],
               projects: dict[str, Counter], cfg: dict, sc: dict, verify_root: Path | None = None) -> dict:
    rr = resolve_root(cfg, sc)
    matches = [] if verify_root is not None else match_existing(fs, [project] + suggestions, projects, sc)
    existing = {p.lower(): p for p in projects}
    proj = existing.get(project.lower(), project)
    is_existing = proj.lower() in existing or verify_root is not None
    merge_into = None
    if not is_existing and matches and name_source != "folder":
        merge_into = matches[0]["project"]    # suggestion only: shown in the dry run, applied if left selected
    dest = rr / (merge_into or proj)
    times = [f["time"] for f in fs]
    g = {"id": gid, "include": True, "project": proj, "name_source": name_source, "suggested_names": suggestions,
         "existing": is_existing or bool(merge_into), "merge_into": merge_into, "merge_candidates": matches,
         "dest": str(dest), "clip_count": sum(1 for f in fs if f["kind"] == "video"),
         "photo_count": sum(1 for f in fs if f["kind"] == "photo"),
         "time_start": min(times) if times else None, "time_end": max(times) if times else None,
         "description": describe_group(fs), "flags": [], "split_suggestion": None,
         "items": plan_items(fs, dest, sc, verify_root)}
    bc = Counter(i["bucket"] for i in g["items"])
    g["bucket_counts"] = {b: bc[b] for b in sorted(bc, key=lambda b: list(sc["buckets"].values()).index(b)
                                                    if b in sc["buckets"].values() else 99)}
    return g


def suggest_split(g: dict, fs: list[dict], idx: int, why: str, keep_first_name: bool) -> None:
    """Flag a group that looks like two shoots and attach a split suggestion (applied only if the user ticks it)."""
    left, right = fs[:idx], fs[idx:]
    first = g["project"] if keep_first_name else (content_names(left) or [g["project"]])[0]
    second = (content_names(right) or [g["project"] + " 2"])[0]
    if second.lower() == first.lower():
        second = first + " 2"
    g["flags"].append(f"Looks like two shoots — {why}. Suggest splitting; nothing is split unless you accept it.")
    g["split_suggestion"] = {"at": right[0]["time"], "reason": why, "parts": [
        {"project": first, "names": [f["name"] for f in left], "description": describe_group(left)},
        {"project": second, "names": [f["name"] for f in right], "description": describe_group(right)}]}


def build_plan(cfg: dict, source: str, probe: bool = True, instructions: dict | None = None) -> dict:
    """instructions: instructions.resolve(...) — glossary fixes name spellings; a 'Project: X' line in the batch text
    names the shoot when the source yields exactly one content-named group (otherwise it's offered as a suggestion)."""
    global _GLOSSARY
    _GLOSSARY = list((instructions or {}).get("glossary") or [])
    try:
        return _build_plan(cfg, source, probe, instructions or {})
    finally:
        _GLOSSARY = []


def _build_plan(cfg: dict, source: str, probe: bool, act: dict) -> dict:
    sc = sort_config(cfg)
    rr = resolve_root(cfg, sc)
    src = ns.inbox_dir(cfg) if source in ("inbox", "") else Path(source).expanduser()
    if not src.is_absolute():                      # a bare name means a folder under the Resolve root
        src = rr / src
    guard_path(src, cfg, sc, "Source")         # held / protected / synced: refused before anything is read
    if not src.is_dir():
        raise Refused(f"Source folder not found: {src}", 2)
    guard_path(src, cfg, sc, "Source")
    if src.resolve() == rr.resolve():
        raise Refused("Pick one folder inside DaVinci Resolve, not the whole DaVinci Resolve folder.", 2)
    store = Store(cfg)
    is_inbox = src.resolve() == ns.inbox_dir(cfg).resolve()
    verify_root = src if (src.parent.resolve() == rr.resolve() and is_project_dir(src, sc)) else None
    skip = {sc["buckets"]["notes"], sc["buckets"]["exports"], "processing", "needs-review", "done"}
    left_out: list[str] = []
    files = walk_media(src, sc, skip, left_out)
    facts, skipped = [], []
    for p in files:
        try:
            f = clip_facts(p, cfg, sc, store, probe=probe)
        except OSError as e:
            skipped.append({"src": str(p), "reason": f"unreadable: {e}"})
            continue
        if is_inbox and not f["processed"] and f["kind"] == "video" and not name_type(p.stem, sc):
            skipped.append({"src": str(p), "reason": "not processed yet — run Start in ClipGauge first"})
            continue
        f["slate"] = is_slate(f, sc)
        facts.append(f)
    projects = {} if verify_root is not None else {p.name: project_signature(p, sc) for p in existing_projects(cfg, sc)}
    groups: list[dict] = []
    notes: list[str] = list(left_out)
    warnings: list[str] = []
    if verify_root is not None:
        g = make_group("g1", facts, src.name, "folder", [], projects, cfg, sc, verify_root=verify_root)
        groups.append(g)
        notes.append(f"“{src.name}” is already a project in the A-Roll/B-Roll/Images layout — files in bucket folders stay; "
                     "loose files are sorted into it.")
    else:
        # inbox: each batch folder is its own source; loose top-level clips are a camera dump
        units: list[tuple[str, list[dict]]] = []
        if is_inbox:
            by_folder: dict[str, list[dict]] = {}
            for f in facts:
                rel = Path(f["src"]).relative_to(src)
                by_folder.setdefault(rel.parts[0] if len(rel.parts) > 1 else "", []).append(f)
            units = sorted(by_folder.items(), key=lambda kv: kv[0])
        else:
            units = [(src.name, facts)]
        hint = ins.project_hint(act.get("batch"))
        hint = clean_project_name(ins.apply_spelling(hint, _GLOSSARY)) if hint else None
        n_content = sum(len(merge_similar(time_groups(uf, sc, slates=True), sc)[0])
                        for un, uf in units if uf and not (un and not uninformative(un, sc))) if hint else 0
        if hint:
            notes.append(f"Batch instructions name the project “{hint}”"
                         + (" — used for the shoot below." if n_content == 1 else
                            " — offered as a suggested name for each shoot." if n_content else
                            " — not used here (no shoot in this source is named from its content)."))
        n = 0
        for uname, ufacts in units:
            if not ufacts:
                continue
            if uname and not uninformative(uname, sc):
                n += 1
                g = make_group(f"g{n}", ufacts, clean_project_name(uname), "folder", content_names(ufacts), projects, cfg, sc)
                tgroups = time_groups(ufacts, sc, slates=False)
                tgroups, _ = merge_similar(tgroups, sc)
                split = two_shoots(sorted(ufacts, key=lambda x: x["time"]), sc)
                if len(tgroups) > 1 or split:
                    fs = sorted(ufacts, key=lambda x: x["time"])
                    idx = split["index"] if split else len(tgroups[0])
                    why = (f"content changes at {fs[idx]['time'][11:16]} (similarity {split['similarity']})" if split
                           else f"{len(tgroups)} time blocks (gap > {sc['gap_minutes']} min or day change)")
                    suggest_split(g, fs, idx, why, keep_first_name=True)
                groups.append(g)
                continue
            tg = time_groups(ufacts, sc, slates=True)
            tg, mnotes = merge_similar(tg, sc)
            notes += mnotes
            for fs in tg:
                n += 1
                cands = content_names(fs)
                name_source = ("slate" if any(f.get("slate") for f in fs) and cands and not cands[0].startswith("Shoot ")
                               else ("time" if cands[0].startswith("Shoot ") else "content"))
                if hint:
                    cands = [hint] + [c for c in cands if c.lower() != hint.lower()]
                    if n_content == 1:
                        name_source = "instructions"
                    else:
                        cands = cands[1:2] + cands[:1] + cands[2:]   # keep the content name first, hint second
                g = make_group(f"g{n}", fs, cands[0], name_source, cands, projects, cfg, sc)
                g["flags"].append({"content": "Name proposed from the clips' content — rename it before applying.",
                                   "slate": "Name read from a slate shot — check the spelling.",
                                   "instructions": "Name from your batch instructions (“Project: …”) — check it fits.",
                                   "time": "No notes to name this shoot from — it's named by date/time; rename it before applying."}[name_source])
                split = two_shoots(fs, sc)
                if split and len(fs) >= 4:
                    suggest_split(g, fs, split["index"], f"content changes at {fs[split['index']]['time'][11:16]} "
                                                         f"(similarity {split['similarity']})", keep_first_name=False)
                groups.append(g)
    if str(src.resolve()).startswith(str(rr.resolve()) + os.sep) and any(
            i["action"] == "move" for g in groups for i in g["items"]):
        warnings.append(RELINK_WARNING)
    from check_resolve import resolve_running
    resolve_open = bool(resolve_running())
    if resolve_open:
        warnings.append("DaVinci Resolve is open — the dry run is fine, but Apply is disabled until you quit Resolve.")
    moves = sum(1 for g in groups for i in g["items"] if i["action"] == "move")
    now = datetime.now()
    plan = {
        "schema": 1, "plan_id": f"{now:%Y%m%d-%H%M%S}-{slugify(src.name, 24) or 'source'}",
        "created_at": now.astimezone().isoformat(timespec="seconds"), "source": str(src),
        "source_kind": "inbox" if is_inbox else ("project" if verify_root is not None else "folder"),
        "resolve_root": str(rr), "resolve_open": resolve_open, "settings": {"gap_minutes": sc["gap_minutes"],
                                                                           "review_threshold": sc["review_threshold"]},
        "existing_projects": sorted(projects.keys(), key=str.lower) if verify_root is None else [src.name],
        "warnings": warnings, "notes": notes, "skipped": skipped, "groups": groups,
        "instructions": ({"hash": act.get("hash"), "project_hint": hint, "glossary_terms": len(_GLOSSARY)}
                         if act.get("active") else None),
        "totals": {"groups": len(groups), "moves": moves,
                   "keeps": sum(1 for g in groups for i in g["items"] if i["action"] == "keep"),
                   "videos": sum(g["clip_count"] for g in groups), "photos": sum(g["photo_count"] for g in groups)},
    }
    return plan


def plans_dir(cfg: dict) -> Path:
    return abs_path(cfg.get("logs_dir") or "logs") / "sort-plans"


def save_plan(cfg: dict, plan: dict) -> Path:
    d = plans_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"sort-plan-{plan['plan_id']}.json"
    atomic_write_text(p, json.dumps(plan, indent=1, ensure_ascii=False) + "\n")
    return p


# ---------------------------------------------------------------------------
# apply / undo
# ---------------------------------------------------------------------------

def guards(cfg: dict) -> None:
    from check_resolve import resolve_running
    if resolve_running():
        raise Refused("DaVinci Resolve is open — quit it first (moving files under an open project breaks its media links).")
    run = pl.active_lock(pl.lock_path(cfg))
    if run:
        raise Refused(f"A processing run (or another sort) is active (pid {run.get('pid')}) — try again when it finishes.")
    upd = pl.active_updates(cfg)
    if upd:
        raise Refused(f"Model/tool updates are being installed (pid {upd.get('pid')}) — try again when they finish.")


def apply_edits(plan: dict, edits: dict, cfg: dict, sc: dict) -> list[dict]:
    """Groups to apply after edits: {gid: {include, project, merge_into, split, split_names}}."""
    rr = resolve_root(cfg, sc)
    existing = {p.lower(): p for p in plan.get("existing_projects", [])}
    out = []
    for g in plan["groups"]:
        e = (edits.get("groups") or edits).get(g["id"], {}) if isinstance(edits, dict) else {}
        if not e.get("include", g.get("include", True)):
            continue
        parts = [g]
        if e.get("split") and g.get("split_suggestion"):
            names = e.get("split_names") or [p["project"] for p in g["split_suggestion"]["parts"]]
            parts = []
            for k, part in enumerate(g["split_suggestion"]["parts"]):
                wanted = set(part["names"])
                sub = dict(g, id=f"{g['id']}.{k + 1}", project=names[k] if k < len(names) else part["project"], merge_into=None)
                sub["items"] = [i for i in g["items"] if i["name"] in wanted or i.get("of") in wanted]
                parts.append(sub)
        for part in parts:
            proj = clean_project_name(e.get("project") or part["project"]) if part is g else clean_project_name(part["project"])
            merge = e.get("merge_into", part.get("merge_into")) if "merge_into" in e or part is g else None
            target = clean_project_name(merge) if merge else proj
            if not target or target.startswith(".") or target in sc["protected_dirs"] or held_name(target, sc):
                raise Refused(f"“{target}” can't be used as a project name.", 2)
            target = existing.get(target.lower(), target)
            tdir = rr / target
            if tdir.exists() and is_synced(tdir, sc):
                raise Refused(f"“{target}” is a Blackmagic Cloud-synced project — never touched.", 2)
            items = []
            for i in part["items"]:
                i = dict(i)
                if i["action"] == "move":
                    i["dest"] = str(tdir / i["bucket"] / Path(i["dest"]).name)
                items.append(i)
            out.append({**part, "project": target, "dest": str(tdir), "items": items})
    return out


def free_name(dest: Path, taken: set[str]) -> Path:
    stem, ext = dest.stem, dest.suffix
    k = 2
    cand = dest
    while cand.exists() or ns.norm(cand) in taken:
        cand = dest.with_name(f"{stem}_{k}{ext}")
        k += 1
    return cand


def undo_path(cfg: dict, plan_id: str) -> Path:
    return abs_path(cfg.get("logs_dir") or "logs") / f"sort-undo-{plan_id}.jsonl"


def _append(path: Path, obj: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def update_store(cfg: dict, store: Store, old: Path, new: Path, item: dict, plan_id: str, project: str) -> str | None:
    try:
        rec = store.find(old, item.get("size"))
        if rec is None:
            return None
        rec = dict(rec)
        rec["sorted"] = {"project": project, "bucket": item["bucket"], "from": str(old), "to": str(new),
                         "plan_id": plan_id, "time": ns.now_iso(), "previous_current_path": rec.get("current_path")}
        ap = rec.get("applied")
        if isinstance(ap, dict) and ap.get("new_path"):
            rec["applied"] = dict(ap, new_path=str(new))
        return ns.save(cfg, rec, new)["clip_id"]
    except Exception:  # noqa: BLE001
        return None


def apply_plan(cfg: dict, plan: dict, edits: dict | None = None, log=print) -> dict:
    sc = sort_config(cfg)
    guard_path(Path(plan["source"]), cfg, sc, "Source")
    guards(cfg)
    groups = apply_edits(plan, edits or {}, cfg, sc)
    lockp = pl.lock_path(cfg)
    ok, holder = pl.acquire_lock(lockp, marker=pl.LOCK_MARKERS)
    if not ok:
        raise Refused(f"Another run holds the lock (pid {holder.get('pid')}).")
    up = undo_path(cfg, plan["plan_id"])
    if up.exists():
        pl.release_lock(lockp)
        raise Refused(f"This plan was already applied ({up.name}). Run a new dry run first.", 2)
    store = Store(cfg)
    res = {"plan_id": plan["plan_id"], "moved": 0, "renamed": [], "skipped": [], "created_dirs": [], "undo": str(up)}
    taken: set[str] = set()
    try:
        _append(up, {"type": "header", "plan_id": plan["plan_id"], "source": plan["source"], "time": ns.now_iso(),
                     "groups": [{"id": g["id"], "project": g["project"], "dest": g["dest"]} for g in groups]})
        for g in groups:
            for it in g["items"]:
                if it["action"] != "move":
                    continue
                src, dest = Path(it["src"]), Path(it["dest"])
                guard_path(dest.parent, cfg, sc, "Destination")
                try:
                    st = src.stat()
                except FileNotFoundError:
                    res["skipped"].append({"src": str(src), "reason": "no longer there"})
                    continue
                if st.st_size != it.get("size"):
                    res["skipped"].append({"src": str(src), "reason": "changed since the dry run"})
                    continue
                if dest.exists() or ns.norm(dest) in taken:
                    if sc["on_conflict"] == "skip":
                        res["skipped"].append({"src": str(src), "reason": f"{dest.name} already exists in {dest.parent.name}/"})
                        continue
                    nd = free_name(dest, taken)
                    res["renamed"].append({"src": str(src), "from": dest.name, "to": nd.name})
                    dest = nd
                for d in reversed([dest.parent, *dest.parent.parents]):
                    if not d.exists():
                        d.mkdir()
                        res["created_dirs"].append(str(d))
                        _append(up, {"type": "mkdir", "path": str(d)})
                    if d == dest.parent:
                        break
                if dest.exists():   # last-moment check — never overwrite
                    res["skipped"].append({"src": str(src), "reason": f"{dest.name} appeared in {dest.parent.name}/"})
                    continue
                try:
                    os.rename(src, dest)
                except OSError as e:
                    if e.errno == errno.EXDEV:
                        res["skipped"].append({"src": str(src), "reason": "different drive — not supported (copy by hand)"})
                        continue
                    raise
                taken.add(ns.norm(dest))
                cid = update_store(cfg, store, src, dest, it, plan["plan_id"], g["project"]) if it["kind"] != "sidecar" else None
                _append(up, {"type": "move", "src": str(src), "dest": str(dest), "clip_id": cid, "group": g["id"],
                             "project": g["project"], "bucket": it["bucket"], "time": ns.now_iso()})
                res["moved"] += 1
        _append(up, {"type": "done", "time": ns.now_iso(), "moved": res["moved"], "skipped": len(res["skipped"])})
    finally:
        pl.release_lock(lockp)
    log(f"Moved {res['moved']} file(s); {len(res['skipped'])} skipped; undo map {up}")
    return res


def undo_logs(cfg: dict) -> list[Path]:
    d = abs_path(cfg.get("logs_dir") or "logs")
    return sorted((p for p in d.glob("sort-undo-*.jsonl") if not p.name.startswith(".")), key=lambda p: p.stat().st_mtime)


def read_undo(p: Path) -> list[dict]:
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def last_undoable(cfg: dict) -> Path | None:
    for p in reversed(undo_logs(cfg)):
        if not any(r.get("type") == "undone" for r in read_undo(p)):
            return p
    return None


def undo(cfg: dict, plan_id: str | None = None, dry: bool = False, log=print) -> dict:
    p = undo_path(cfg, plan_id) if plan_id else last_undoable(cfg)
    if not p or not p.exists():
        raise Refused("Nothing to undo.", 2)
    recs = read_undo(p)
    if any(r.get("type") == "undone" for r in recs):
        raise Refused(f"{p.name} was already undone.", 2)
    if not dry:
        guards(cfg)
    moves = [r for r in recs if r.get("type") == "move"]
    res = {"plan_id": recs[0].get("plan_id") if recs else plan_id, "restored": 0, "skipped": [], "removed_dirs": [],
           "would_restore": [] if dry else None}
    store = Store(cfg) if not dry else None
    lockp = pl.lock_path(cfg)
    if not dry:
        ok, holder = pl.acquire_lock(lockp, marker=pl.LOCK_MARKERS)
        if not ok:
            raise Refused(f"Another run holds the lock (pid {holder.get('pid')}).")
    try:
        for r in reversed(moves):
            src, dest = Path(r["src"]), Path(r["dest"])
            if not dest.exists():
                res["skipped"].append({"file": str(dest), "reason": "not there any more"})
                continue
            if src.exists():
                res["skipped"].append({"file": str(dest), "reason": f"{src.name} already exists back in {src.parent.name}/"})
                continue
            if dry:
                res["would_restore"].append({"from": str(dest), "to": str(src)})
                continue
            src.parent.mkdir(parents=True, exist_ok=True)
            os.rename(dest, src)
            res["restored"] += 1
            if r.get("clip_id"):
                rec = ns.load(cfg, r["clip_id"])
                if rec:
                    rec = dict(rec)
                    rec.pop("sorted", None)
                    ap = rec.get("applied")
                    if isinstance(ap, dict) and ap.get("new_path") == str(dest):
                        rec["applied"] = dict(ap, new_path=str(src))
                    ns.save(cfg, rec, src)
        if not dry:
            for r in reversed([r for r in recs if r.get("type") == "mkdir"]):
                d = Path(r["path"])
                try:
                    if d.is_dir() and not [e for e in d.iterdir() if not e.name.startswith("._") and e.name != ".DS_Store"]:
                        for junk in d.iterdir():
                            junk.unlink()
                        d.rmdir()
                        res["removed_dirs"].append(str(d))
                except OSError:
                    pass
            _append(p, {"type": "undone", "time": ns.now_iso(), "restored": res["restored"], "skipped": len(res["skipped"])})
    finally:
        if not dry:
            pl.release_lock(lockp)
    log(("Would restore " + str(len(res["would_restore"] or []))) if dry else f"Restored {res['restored']} file(s)")
    return res


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def summary(plan: dict) -> str:
    lines = [f"Dry run {plan['plan_id']} — source {plan['source']} ({plan['source_kind']})"]
    for w in plan["warnings"]:
        lines.append(f"  ⚠ {w}")
    for n in plan["notes"]:
        lines.append(f"  · {n}")
    for g in plan["groups"]:
        t0, t1 = (g["time_start"] or "")[:16].replace("T", " "), (g["time_end"] or "")[11:16]
        tgt = f"add to existing “{g['merge_into']}”" if g.get("merge_into") else ("existing project" if g["existing"] else "NEW project")
        lines.append(f"\n[{g['id']}] {g['project']} — {g['clip_count']} clip(s), {g['photo_count']} photo(s), {t0}–{t1} · {tgt}")
        lines.append(f"     {g['description']}")
        lines.append(f"     → {g['dest']}")
        if g["merge_candidates"] and not g.get("merge_into"):
            lines.append("     similar existing: " + ", ".join(f"{m['project']} ({m['why']})" for m in g["merge_candidates"]))
        for fl in g["flags"]:
            lines.append(f"     ⚑ {fl}")
        if g.get("split_suggestion"):
            lines.append("     split option (edits: {\"%s\": {\"split\": true}}): " % g["id"]
                         + " + ".join(f"{x['project']} ({len(x['names'])})" for x in g["split_suggestion"]["parts"]))
        counts = Counter((i["bucket"], i["action"]) for i in g["items"])
        lines.append("     " + ", ".join(f"{b}: {n}{' kept' if a == 'keep' else ''}" for (b, a), n in sorted(counts.items())))
        differ = [i for i in g["items"] if i["action"] == "keep" and "sorter would say" in i.get("reason", "")]
        no_notes = [i for i in differ if "no notes" in i["reason"]]
        other = [i for i in differ if i not in no_notes]
        if no_notes:
            lines.append(f"     {len(no_notes)} file(s) have no renamer notes (hand-named?) — kept where they are")
        if other:
            lines.append(f"     {len(other)} file(s) sit in a different bucket than the type map would choose (kept where they are):")
            for i in other[:8]:
                lines.append(f"       {i['name']}: {i['reason']}")
        for i in g["items"]:
            if i["action"] == "move" and i["kind"] != "sidecar" and i["bucket"] == "_Review":
                lines.append(f"       _Review ← {i['name']} ({i['reason']})")
    if plan["skipped"]:
        lines.append(f"\nSkipped {len(plan['skipped'])}: " + "; ".join(f"{Path(s['src']).name} ({s['reason']})" for s in plan["skipped"][:6]))
    t = plan["totals"]
    lines.append(f"\n{t['moves']} file(s) would move, {t['keeps']} already in place. Nothing has been moved.")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Sort clips into DaVinci Resolve project folders (dry run first)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--list-sources", action="store_true")
    g.add_argument("--source", help="Folder to sort, or 'inbox'")
    g.add_argument("--apply", action="store_true", help="Apply a saved plan (needs --plan)")
    g.add_argument("--undo", action="store_true", help="Undo the last applied sort (or --plan-id)")
    g.add_argument("--history", action="store_true", help="List applied sorts")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--plan", help="Plan file from a dry run")
    ap.add_argument("--edits", help="JSON file with per-group edits (include / project / merge_into / split)")
    ap.add_argument("--plan-id")
    ap.add_argument("--gap-minutes", type=float)
    ap.add_argument("--no-probe", action="store_true", help="Don't ffprobe clips without notes (faster)")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--instructions-file", type=Path, help="Batch instructions for naming (default: saved next-run text)")
    ap.add_argument("--no-instructions", action="store_true", help="Ignore glossary / batch instructions when naming")
    args = ap.parse_args()
    cfg = load_config()
    if args.gap_minutes:
        cfg["_sort_overrides"] = {"gap_minutes": args.gap_minutes}

    def out(obj, text: str, code: int = 0) -> int:
        print(json.dumps(obj, ensure_ascii=False) if args.json else text)
        return code

    try:
        if args.list_sources:
            s = list_sources(cfg)
            return out({"sources": s, "resolve_root": str(resolve_root(cfg, sort_config(cfg)))},
                       "\n".join(f"{x['kind']:<8} {'✓' if x['sortable'] else '✗'} {x['name']} — {x['note']}" for x in s))
        if args.history:
            h = []
            for p in reversed(undo_logs(cfg)):          # newest first
                r = read_undo(p)
                hd = next((x for x in r if x.get("type") == "header"), {})
                h.append({"plan_id": hd.get("plan_id"), "time": hd.get("time"), "source": hd.get("source"),
                          "moved": sum(1 for x in r if x.get("type") == "move"),
                          "undone": any(x.get("type") == "undone" for x in r), "file": str(p)})
            return out({"history": h}, "\n".join(f"{x['time']} {x['plan_id']} moved {x['moved']}{' (undone)' if x['undone'] else ''}" for x in h) or "No sorts yet.")
        if args.source is not None:
            if not args.dry_run:
                return out({"ok": False, "error": "Use --dry-run first; apply a saved plan with --apply --plan FILE."},
                           "Use --dry-run first; apply a saved plan with --apply --plan FILE.", 2)
            act = None if args.no_instructions else ins.resolve(
                cfg, batch_text=args.instructions_file.read_text(encoding="utf-8") if args.instructions_file else None)
            plan = build_plan(cfg, args.source, probe=not args.no_probe, instructions=act)
            path = save_plan(cfg, plan)
            plan["plan_file"] = str(path)
            plan["summary_text"] = summary(plan)
            nxt = (f"Apply: python3 scripts/sort_projects.py --apply --plan '{path}'" if plan["totals"]["moves"]
                   else "Nothing to apply.")
            return out(plan, summary(plan) + f"\nPlan saved: {path}\n{nxt}")
        if args.apply:
            if not args.plan:
                return out({"ok": False, "error": "--apply needs --plan FILE"}, "--apply needs --plan FILE", 2)
            plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
            edits = json.loads(Path(args.edits).read_text(encoding="utf-8")) if args.edits else {}
            if not args.yes:
                print(summary(plan))
                if not sys.stdin.isatty() or input("Move these files now? [y/N] ").strip().lower() not in ("y", "yes"):
                    return out({"ok": False, "error": "Not confirmed — nothing moved."}, "Not confirmed — nothing moved.", 1)
            res = apply_plan(cfg, plan, edits, log=(lambda *_: None) if args.json else print)
            return out({"ok": True, **res}, f"Undo: python3 scripts/sort_projects.py --undo --plan-id {plan['plan_id']}")
        if args.undo:
            if not args.dry_run and not args.yes:
                pre = undo(cfg, args.plan_id, dry=True, log=lambda *_: None)
                print(f"Would move {len(pre['would_restore'])} file(s) back.")
                if not sys.stdin.isatty() or input("Undo now? [y/N] ").strip().lower() not in ("y", "yes"):
                    return out({"ok": False, "error": "Not confirmed."}, "Not confirmed — nothing moved.", 1)
            res = undo(cfg, args.plan_id, dry=args.dry_run, log=(lambda *_: None) if args.json else print)
            if args.dry_run:
                return out({"ok": True, **res}, f"Would move {len(res.get('would_restore', []))} file(s) back (dry run — nothing moved).")
            return out({"ok": True, **res}, f"Undone: {res['restored']} file(s) moved back, {len(res['skipped'])} skipped.")
    except Refused as e:
        return out({"ok": False, "error": str(e)}, f"Refused: {e}", e.code)
    return 0


if __name__ == "__main__":
    sys.exit(main())
