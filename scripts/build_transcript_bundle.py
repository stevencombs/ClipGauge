#!/usr/bin/env python3
"""
Consolidated transcript export: every clip's Whisper transcript (from its .json sidecar) in ONE file pair that a
script-writing bot can read and cite back to the exact video.

  python3 scripts/build_transcript_bundle.py                          # all of inbox/ (incl. batch subfolders)
  python3 scripts/build_transcript_bundle.py --folder "Show 2026"     # just inbox/Show 2026/
  python3 scripts/build_transcript_bundle.py --path "DaVinci Resolve/Retro Game Expo"   # any folder on the Lexar
  python3 scripts/build_transcript_bundle.py --everywhere             # inbox/ + every Lexar folder with clip notes
  python3 scripts/build_transcript_bundle.py --date 2026-09-20        # only clips recorded that day (combines with the above)
  options: --include-silent (full entries for clips with no speech) · --omit-silent (don't list them at all)
           --exclude-photos (leave stills out; by default photos are listed with the silent assets)
           --keep-repeats (don't collapse Whisper's repeated-line loops) · --dry-run (scan + print, write nothing)
           --json (last stdout line = machine-readable summary, used by the web UI) · --out-dir DIR

Output (default exports/ in the project folder, created on first use):
  exports/transcripts-<scope>-<YYYYMMDD-HHMM>.json   schema "ai-video-renamer.transcript-bundle" v1:
      metadata + how_to_use note + clips[] in recording-time order; each clip: file, path (relative to the Lexar
      root), original camera name, recorded_at, duration, clip_type, summary, keywords, on-screen text, has_speech,
      transcript text and segments[] {id "<file>#0003", start/end seconds, HH:MM:SS.mmm, text}.
  exports/transcripts-<scope>-<YYYYMMDD-HHMM>.md     the same, grouped by clip, compact enough to paste into a chat bot.

Read-only for media: it only READS .json sidecars (written atomically by the pipeline, so never half-written) and
writes the two export files. It never renames, moves or deletes a video or sidecar, and takes no pipeline lock —
a clip still being processed during a live run is simply picked up by the next export.
Non-speech: segments that are only Whisper markers ([BLANK_AUDIO], [Music], (wind blowing), >> [INAUDIBLE], ♪ …)
are dropped; a clip with nothing left has has_speech false. Consecutive identical lines (a known Whisper
hallucination loop on wind/water noise) are collapsed into one segment with "repeats": n unless --keep-repeats.
Segment ids are 1-based over the kept segments, so they are stable for the same sidecar + options.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import ROOT, abs_path, atomic_write_text, load_config, section, slugify  # noqa: E402
import notes_store as ns  # noqa: E402

SCHEMA = "ai-video-renamer.transcript-bundle"
SCHEMA_VERSION = 1
TOOL = "AI-Video-Renamer"
# Shared with notes_store (video extensions, Lexar search roots, folders never searched)
VIDEO_EXTS = ns.VIDEO_EXTS
MEDIA_EXTS = ns.MEDIA_EXTS  # videos + photos: what a sidecar / store record can describe
DEFAULT_SEARCH_ROOTS = ns.DEFAULT_SEARCH_ROOTS
SKIP_DIRS = ns.SKIP_DIRS
MAX_SIDECAR_BYTES = 5 * 1024 * 1024
MAX_WALK_ENTRIES = 50000

# A segment that is nothing but markers: [BLANK_AUDIO], [Music], (upbeat music), *applause*, ♪ ♪ ...
NOISE_RE = re.compile(r"^\s*(?:[\[\(\*♪][^\]\)\*♪]*[\]\)\*♪]\s*)+$")
# Markers that carry no information even inline
BLANK_INLINE_RE = re.compile(r"\[\s*(?:BLANK_AUDIO|BLANK AUDIO|SILENCE|NO SPEECH|NO_SPEECH)\s*\]|\(\s*(?:silence|no speech)\s*\)",
                             re.IGNORECASE)
SPEAKER_RE = re.compile(r"^\s*(?:>>+|-\s+)\s*")  # whisper speaker-turn marker ">> " / "- "
CAM_TS_RE = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})[_-]?(\d{2})(\d{2})(\d{2})(?!\d)")
HOW_TO_USE = [
    "This file is a transcript bundle of b-roll / A-roll video clips, exported by AI-Video-Renamer.",
    "clips[] is in recording-time order. Each clip is one video file; 'file' is its current file name and 'path' "
    "its location relative to the Lexar drive root.",
    "Every spoken line is a segment with an id '<file>#NNNN' plus start/end time inside that clip. When you quote "
    "or adapt speech in a script, cite the segment id(s) (e.g. [20260920_show_vendor_broll.mp4#0003]) so the editor "
    "can find the exact video and timecode.",
    "summary / keywords / on_screen_text describe the visuals (from a local vision model) — use them to suggest "
    "b-roll for a script beat even when a clip has no speech (silent_clips / has_speech=false).",
    "Transcripts are automatic (Whisper) and can mishear names and brands; segments with 'repeats' > 1 were "
    "identical consecutive lines and are often a transcription error over wind/water noise.",
]


# ------------------------------------------------------------ helpers ----

def volume_root(cfg: dict) -> Path:
    """The Lexar drive root (parent of the project folder) — clip paths are reported relative to this."""
    return ns.volume_root(cfg)


def inbox_dir(cfg: dict) -> Path:
    return abs_path(cfg.get("inbox_dir") or "inbox")


def exports_dir(cfg: dict) -> Path:
    return abs_path(section(cfg, "transcripts").get("exports_dir") or cfg.get("exports_dir") or "exports")


def search_roots(cfg: dict) -> list[str]:
    return ns.search_roots(cfg)


def fmt_ts(seconds: float | None) -> str:
    ms = int(round(max(0.0, float(seconds or 0)) * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def fmt_dur(s: float | None) -> str:
    if not s:
        return "0s"
    s = int(round(s))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h{m:02d}m{sec:02d}s" if h else (f"{m}m{sec:02d}s" if m else f"{sec}s")


def rel_to(p: Path, base: Path) -> str:
    try:
        return Path(os.path.relpath(p, base)).as_posix() if os.path.commonpath([str(p), str(base)]) == str(base) else str(p)
    except ValueError:
        return str(p)


def inside(p: Path, base: Path) -> bool:
    try:
        rp, rb = os.path.realpath(p), os.path.realpath(base)
        return os.path.commonpath([rp, rb]) == rb
    except ValueError:
        return False


def norm_key(text: str) -> str:
    """Comparison key for repeat detection: lower-case letters/digits only."""
    return re.sub(r"[^0-9a-z]+", " ", unicodedata.normalize("NFKD", text).lower()).strip()


def clean_text(raw: object) -> tuple[str, bool]:
    """Whisper segment text -> (clean text or '' if pure non-speech, speaker_change)."""
    t = " ".join(str(raw or "").split())
    speaker = bool(SPEAKER_RE.match(t)) and t.lstrip().startswith(">>")
    t = SPEAKER_RE.sub("", t)
    t = BLANK_INLINE_RE.sub(" ", t)
    t = " ".join(t.split())
    residual = re.sub(r"[\[\(\*♪][^\]\)\*♪]*[\]\)\*♪]", " ", t).replace("♪", " ")
    if not t or NOISE_RE.match(t) or not re.search(r"[^\W_]", residual):
        return "", speaker
    return t, speaker


def clean_segments(file_name: str, segs: list, collapse_repeats: bool = True) -> list[dict]:
    """Sidecar transcript.segments -> cleaned, (optionally) repeat-collapsed segments with stable ids."""
    out: list[dict] = []
    for i, s in enumerate(segs or []):
        if not isinstance(s, dict):
            continue
        text, speaker = clean_text(s.get("text"))
        if not text:
            continue
        try:
            start, end = round(float(s.get("start") or 0), 3), round(float(s.get("end") or 0), 3)
        except (TypeError, ValueError):
            continue
        end = max(end, start)
        if collapse_repeats and out and norm_key(out[-1]["text"]) == norm_key(text):
            prev = out[-1]
            prev["end"] = max(prev["end"], end)
            prev["repeats"] = prev.get("repeats", 1) + 1
            prev["whisper_index_last"] = i
            continue
        seg = {"start": start, "end": end, "text": text, "whisper_index": i}
        if speaker:
            seg["speaker_change"] = True
        out.append(seg)
    for n, seg in enumerate(out, 1):
        seg["id"] = f"{file_name}#{n:04d}"
        seg["start_ts"], seg["end_ts"] = fmt_ts(seg["start"]), fmt_ts(seg["end"])
    # stable key order for readers
    keys = ("id", "start", "end", "start_ts", "end_ts", "text", "repeats", "speaker_change", "whisper_index",
            "whisper_index_last")
    return [{k: seg[k] for k in keys if k in seg} for seg in out]


def parse_iso(s: object) -> datetime | None:
    if not s or not isinstance(s, str):
        return None
    t = s.strip().replace("Z", "+00:00")
    t = re.sub(r"\.(\d{6})\d+", r".\1", t)
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def recorded_time(rec: dict, video: Path | None, sidecar: Path) -> tuple[datetime | None, str]:
    """(aware datetime, source) — container creation_time, else CAM_YYYYMMDDHHMMSS camera clock, else source.date, else mtime."""
    src = rec.get("source") or {}
    dt = parse_iso(src.get("creation_time"))
    if dt:
        return dt, "creation_time"
    for name in (src.get("name"), video.name if video else None):
        m = CAM_TS_RE.search(str(name or ""))
        if m:
            try:
                return datetime(*map(int, m.groups())).astimezone(), "camera filename"
            except ValueError:
                pass
    d = str(src.get("date") or "")
    if re.fullmatch(r"\d{8}", d):
        try:
            return datetime.strptime(d, "%Y%m%d").astimezone(), "date only"
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp((video or sidecar).stat().st_mtime).astimezone(), "file modified time"
    except OSError:
        return None, "unknown"


def norm_date(s: str | None) -> str | None:
    """'2026-09-20' / '20260920' / '2026/09/20' -> '20260920' (ValueError if unusable)."""
    if not s:
        return None
    d = re.sub(r"[^0-9]", "", str(s))
    if len(d) != 8:
        raise ValueError(f"--date must be YYYY-MM-DD or YYYYMMDD, got {s!r}")
    datetime.strptime(d, "%Y%m%d")
    return d


# ------------------------------------------------------------ discovery ----

def is_clip_sidecar(rec: object) -> bool:
    return isinstance(rec, dict) and rec.get("tool") == TOOL and isinstance(rec.get("source"), dict) and "transcript" in rec


def find_video(sidecar: Path, rec: dict) -> Path | None:
    """The video/photo next to this sidecar (same stem, any media extension, case-insensitive; the original
    file's extension wins if both X.mp4 and X.jpg exist)."""
    stem = sidecar.stem.lower()
    want = os.path.splitext(str((rec.get("source") or {}).get("name") or ""))[1].lower()
    hits = []
    try:
        with os.scandir(sidecar.parent) as it:
            for e in it:
                if e.name.startswith("."):
                    continue
                st, ext = os.path.splitext(e.name)
                if st.lower() == stem and ext.lower() in MEDIA_EXTS and e.is_file():
                    hits.append(sidecar.parent / e.name)
    except OSError:
        pass
    hits.sort(key=lambda p: p.suffix.lower() != want)
    return hits[0] if hits else None


def same_size_beside(sidecar: Path, rec: dict) -> Path | None:
    """The one video in the sidecar's folder with the clip's exact byte size (a clip renamed by hand in Finder)."""
    size = (rec.get("source") or {}).get("size_bytes")
    if not isinstance(size, int) or size <= 0:
        return None
    try:
        with os.scandir(sidecar.parent) as it:
            hits = [Path(e.path) for e in it if not e.name.startswith(".")
                    and os.path.splitext(e.name)[1].lower() in MEDIA_EXTS and e.is_file() and e.stat().st_size == size]
    except OSError:
        return None
    return hits[0] if len(hits) == 1 else None


def walk_sidecars(top: Path, recursive: bool = True, skip: set[str] | None = None) -> list[Path]:
    """Candidate *.json sidecars under top (hidden, ._ AppleDouble, .tmp and Resolve cache folders skipped)."""
    skip = SKIP_DIRS if skip is None else skip
    found: list[Path] = []
    seen = 0
    stack = [top]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                entries = sorted(it, key=lambda e: e.name.lower())
        except OSError:
            continue
        for e in entries:
            seen += 1
            if seen > MAX_WALK_ENTRIES:
                return found
            if e.name.startswith("."):
                continue
            if e.is_dir(follow_symlinks=False):
                if recursive and e.name not in skip:
                    stack.append(Path(e.path))
            elif e.is_file(follow_symlinks=False) and e.name.lower().endswith(".json"):
                found.append(Path(e.path))
    return found


def load_sidecar(p: Path) -> dict | None:
    try:
        if p.stat().st_size > MAX_SIDECAR_BYTES:
            return None
        rec = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return rec if is_clip_sidecar(rec) else None


def resolve_scope(cfg: dict, folder: str | None = None, paths: list[str] | None = None,
                  everywhere: bool = False) -> dict:
    """-> {kind, label, slug, dirs: [(Path, recursive)]}. ValueError with a readable message."""
    vol, inbox = volume_root(cfg), inbox_dir(cfg)
    dirs: list[tuple[Path, bool]] = []
    if sum(bool(x) for x in (folder, paths, everywhere)) > 1:
        raise ValueError("use only one of --folder, --path, --everywhere")
    if folder:
        import apply_renames as ar  # local: only needed for the folder-name rules
        name = ar.sanitize_folder_name(folder)
        d = inbox / name
        if inbox.is_dir():  # use the real spelling (ExFAT/APFS are case-insensitive: 'show 2026' finds 'Show 2026')
            with os.scandir(inbox) as it:
                for e in it:
                    if e.is_dir() and unicodedata.normalize("NFC", e.name).lower() == unicodedata.normalize("NFC", name).lower():
                        d = inbox / e.name
                        break
        if not d.is_dir():
            raise ValueError(f"inbox/{name}/ does not exist")
        return {"kind": "folder", "label": f"inbox/{d.name}", "slug": "inbox-" + (slugify(d.name, 40) or "folder"),
                "dirs": [(d, True)], "folder": d.name}
    if paths:
        labels = []
        for raw in paths:
            p = Path(raw)
            p = p if p.is_absolute() else vol / p
            if not inside(p, vol):
                raise ValueError(f"{raw}: must be a folder on the Lexar drive ({vol})")
            if not p.is_dir():
                raise ValueError(f"{raw}: not a folder")
            dirs.append((p, True))
            labels.append(rel_to(Path(os.path.realpath(p)), Path(os.path.realpath(vol))) or ".")
        slug = slugify("-".join(Path(lbl).name or lbl for lbl in labels), 48) or "lexar"
        return {"kind": "path", "label": ", ".join(labels), "slug": slug, "dirs": dirs, "paths": labels}
    if everywhere:
        dirs.append((inbox, True))
        for r in search_roots(cfg):
            p = vol / r
            if p.is_dir():
                dirs.append((p, True))
        return {"kind": "everywhere", "label": "inbox/ + " + ", ".join(search_roots(cfg)), "slug": "all-lexar", "dirs": dirs}
    return {"kind": "inbox", "label": "inbox/ (incl. subfolders)", "slug": "inbox", "dirs": [(inbox, True)]}


def discover_folders(cfg: dict) -> list[dict]:
    """Folders that hold processed clips (from the notes store, located on disk, plus sidecars in inbox/ and the
    search roots) — for the UI scope list. [{dir, rel, kind: inbox|lexar|other, clips}]"""
    vol, inbox = volume_root(cfg), inbox_dir(cfg)
    got = collect(cfg, resolve_scope(cfg, everywhere=True))
    out: dict[str, dict] = {}
    for c in got["clips"]:
        d = Path(c["_dir"])
        kind = "inbox" if inside(d, inbox) else ("lexar" if inside(d, vol) else "other")
        e = out.setdefault(str(d), {"dir": str(d), "rel": rel_to(d, vol), "kind": kind, "clips": 0, "found": 0})
        e["clips"] += 1
        e["found"] += bool(c["video_found"])
    return sorted(out.values(), key=lambda x: (x["kind"] != "inbox", x["rel"].lower()))


# ------------------------------------------------------------ building ----

def clip_entry(rec: dict, cfg: dict, video: Path | None, where: Path, sidecar: Path | None = None,
               located: str = "", collapse_repeats: bool = True) -> dict:
    """One clip for the bundle. video = its file if found (where = video or the last known path)."""
    vol = volume_root(cfg)
    src = rec.get("source") or {}
    desc = (rec.get("describe") or {}).get("description") or {}
    tx = rec.get("transcript") or {}
    file_name = where.name
    dt, dt_src = recorded_time(rec, video, sidecar or where)
    segs = clean_segments(file_name, tx.get("segments") or [], collapse_repeats)
    speech_s = round(sum(s["end"] - s["start"] for s in segs), 3)
    notes = []
    loops = [s for s in segs if s.get("repeats", 1) >= 3]
    if loops:
        notes.append(f"{len(loops)} line(s) repeated 3+ times in a row — likely a Whisper error over background noise")
    if not video:
        notes.append("video file not found (last known location shown; searched inbox/ and "
                     + ", ".join(search_roots(cfg)) + ")")
    elif located.startswith("same size"):
        notes.append(f"found by file size — {located}")
    dur = src.get("duration_s")
    kind = ns.record_kind(rec)
    if kind == "photo":
        dur = None  # a still: no duration
    return {
        "file": file_name,
        "media_kind": kind,
        "path": rel_to(where, vol),
        "folder": rel_to(where.parent, vol),
        "video_found": bool(video),
        "original_name": src.get("name"),
        "recorded_at": dt.astimezone().isoformat(timespec="seconds") if dt else None,
        "recorded_at_utc": dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z") if dt else None,
        "recorded_time_source": dt_src,
        "duration_s": round(float(dur), 3) if isinstance(dur, (int, float)) else None,
        "duration": fmt_ts(dur) if isinstance(dur, (int, float)) else None,
        "clip_type": desc.get("clip_type"),
        "summary": desc.get("summary"),
        "keywords": list(desc.get("keywords") or []),
        "on_screen_text": list(desc.get("on_screen_text") or []),
        "subjects": list(desc.get("subjects") or []),
        "objects": list(desc.get("objects") or []),
        "has_audio": src.get("has_audio"),
        "has_speech": bool(segs),
        "speech_seconds": speech_s,
        "transcript_status": tx.get("status"),
        "transcript_model": tx.get("model"),
        "transcript": " ".join(s["text"] for s in segs),
        "segments": segs,
        "note_id": rec.get("clip_id") or ns.make_id(rec),
        "notes_source": "sidecar" if sidecar else "store",
        "sidecar": rel_to(sidecar, vol) if sidecar else None,
        "notes": notes,
        "_sort": (dt.timestamp() if dt else float("inf"), file_name.lower()),
        "_date": dt.astimezone().strftime("%Y%m%d") if dt else str(src.get("date") or ""),
        "_src_date": str(src.get("date") or ""),
        "_dir": str(where.parent),
    }


def _orig_key(rec: dict) -> str:
    src = rec.get("source") or {}
    return f"{str(src.get('name') or '').lower()}|{src.get('size_bytes') or ''}"


def collect(cfg: dict, scope: dict, date: str | None = None, collapse_repeats: bool = True,
            sources: str = "both", include_photos: bool = True) -> dict:
    """Clips in scope: notes-store records first (located on disk — stored path, else same file name / same size in
    inbox/ + search roots), then .json sidecars found on disk that the store doesn't already have (deduped by clip id
    and by original camera name + size). sources: both | store | sidecars."""
    clips, skipped = [], []
    inbox, vol = inbox_dir(cfg), volume_root(cfg)
    seen_ids: set[str] = set()
    seen_orig: set[str] = set()
    claimed: set[str] = set()
    fmap = None
    everywhere = scope["kind"] == "everywhere"

    def in_scope(p: Path) -> bool:
        return everywhere or any(inside(p, d) for d, _ in scope["dirs"])

    def keep(c: dict) -> bool:
        if not include_photos and c["media_kind"] == "photo":
            return False
        return not date or date in (c["_date"], c["_src_date"])

    if sources in ("both", "store"):
        fmap = ns.build_file_map(cfg, [d for d, _ in scope["dirs"]])
        recs = ns.load_all(cfg)
        # records whose stored path still exists claim their file first; the rest are located by name / size
        recs.sort(key=lambda r: not (r.get("current_path") and Path(r["current_path"]).is_file()))
        for rec in recs:
            video, how = ns.locate(rec, fmap, claimed)
            where = video or Path(rec.get("current_path") or (rec.get("source") or {}).get("path") or rec["clip_id"])
            if video:
                claimed.add(ns.norm(video))
            seen_ids.add(rec.get("clip_id") or ns.make_id(rec))
            seen_orig.add(_orig_key(rec))
            if not in_scope(where):
                continue
            c = clip_entry(rec, cfg, video, where, None, how, collapse_repeats)
            if keep(c):
                clips.append(c)
    if sources in ("both", "sidecars"):
        seen_files: set[str] = set()
        for d, recursive in scope["dirs"]:
            # inside inbox/ any batch-folder name is fine; elsewhere skip Resolve caches and project internals
            for sj in walk_sidecars(d, recursive, skip=set() if inside(d, inbox) else SKIP_DIRS):
                key = os.path.realpath(sj).lower()
                if key in seen_files:
                    continue
                seen_files.add(key)
                rec = load_sidecar(sj)
                if rec is None:
                    if not sj.name.lower().endswith(".transcript.json") and find_video(sj, {}):
                        skipped.append({"sidecar": rel_to(sj, vol), "reason": "unreadable or not a renamer sidecar"})
                    continue
                cid = rec.get("clip_id") or ns.make_id(rec)
                if cid in seen_ids or _orig_key(rec) in seen_orig:
                    continue  # the store already has this clip
                seen_ids.add(cid)
                seen_orig.add(_orig_key(rec))
                video, how = find_video(sj, rec), "next to sidecar"
                applied = rec.get("applied") or {}
                guess = (Path(str(applied.get("new_path"))).name if applied.get("new_path") else None) or \
                    (rec.get("source") or {}).get("name") or sj.stem
                if not video:
                    video = same_size_beside(sj, rec)
                    how = "same size beside its sidecar (renamed by hand?)" if video else how
                if not video:
                    if fmap is None:
                        fmap = ns.build_file_map(cfg, [d for d, _ in scope["dirs"]])
                    video, how = ns.locate(dict(rec, current_path=str(sj.with_name(guess))), fmap, claimed)
                    if video and not inside(video, sj.parent):
                        video, how = None, "not found"  # a sidecar describes the clip beside it
                if video:
                    claimed.add(ns.norm(video))
                c = clip_entry(rec, cfg, video, video or sj.with_name(guess), sj, how, collapse_repeats)
                if keep(c):
                    clips.append(c)
    clips.sort(key=lambda c: c["_sort"])
    return {"clips": clips, "skipped": skipped}



def build_bundle(cfg: dict, scope: dict, date: str | None = None, include_silent: bool = False,
                 omit_silent: bool = False, collapse_repeats: bool = True, now: datetime | None = None,
                 sources: str = "both", include_photos: bool = True) -> dict:
    now = now or datetime.now().astimezone()
    got = collect(cfg, scope, date, collapse_repeats, sources, include_photos)
    speech = [c for c in got["clips"] if c["has_speech"]]
    silent = [c for c in got["clips"] if not c["has_speech"]]
    public = lambda c: {k: v for k, v in c.items() if not k.startswith("_")}  # noqa: E731
    in_bundle = got["clips"] if include_silent else speech
    total_speech = round(sum(c["speech_seconds"] for c in speech), 3)
    bundle = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.isoformat(timespec="seconds"),
        "generator": f"{TOOL} scripts/build_transcript_bundle.py",
        "scope": {"kind": scope["kind"], "label": scope["label"], "date": date, "include_silent": include_silent,
                  "omit_silent": omit_silent, "collapse_repeats": collapse_repeats, "notes_sources": sources,
                  "include_photos": include_photos,
                  "lexar_root": str(volume_root(cfg))},
        "how_to_use": HOW_TO_USE,
        "citation_format": "<file>#NNNN (segment id; NNNN = 1-based line number within that clip)",
        "counts": {"clips_scanned": len(got["clips"]), "clips_in_bundle": len(in_bundle), "clips_with_speech": len(speech),
                   "clips_silent": len(silent), "segments": sum(len(c["segments"]) for c in speech),
                   "photos": sum(c["media_kind"] == "photo" for c in got["clips"]),
                   "skipped_sidecars": len(got["skipped"]),
                   "from_store": sum(c["notes_source"] == "store" for c in got["clips"]),
                   "from_sidecars": sum(c["notes_source"] == "sidecar" for c in got["clips"]),
                   "video_not_found": sum(not c["video_found"] for c in got["clips"])},
        "total_speech_seconds": total_speech,
        "total_speech": fmt_ts(total_speech),
        "total_clip_seconds": round(sum(c["duration_s"] or 0 for c in got["clips"]), 3),
        "clips": [public(c) for c in in_bundle],
    }
    if not include_silent and not omit_silent:
        bundle["silent_clips"] = [{k: c[k] for k in ("file", "media_kind", "path", "original_name", "recorded_at", "duration_s", "duration",
                                                      "clip_type", "summary", "keywords", "on_screen_text")} for c in silent]
    if got["skipped"]:
        bundle["skipped"] = got["skipped"]
    return bundle


def local_time(iso: str | None) -> str:
    if not iso:
        return "time unknown"
    dt = parse_iso(iso)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z") if dt else iso


def build_markdown(b: dict) -> str:
    c = b["counts"]
    L = [f"# Transcript bundle — {b['scope']['label']}" + (f" · recorded {b['scope']['date']}" if b["scope"]["date"] else ""), ""]
    L.append(f"Generated {local_time(b['generated_at'])} · {c['clips_with_speech']} clip(s) with speech, "
             f"{c['clips_silent']} without · {round(b['total_speech_seconds'] / 60, 1)} min of speech · "
             f"{c['segments']} lines" + (f" · {c['photos']} photo(s) listed with the silent assets" if c.get("photos") else ""))
    L += ["", "**For the AI:** each clip below is one video file (heading = file name, then its path on the Lexar drive). "
          "Lines are `[start → end] #NNNN text`, times inside that clip. Cite a line as `<file>#NNNN` "
          "(e.g. `" + next((c["segments"][0]["id"] for c in b["clips"] if c["segments"]), "clip.mp4#0001")
          + "`) so the editor can find the exact video and timecode. Transcripts are automatic and can mishear names; "
          "“×N” marks a line repeated N times in a row (often a transcription error over noise).", ""]
    for n, clip in enumerate(b["clips"], 1):
        L.append(f"## {n}. {clip['file']}")
        meta = [f"`{clip['path']}`", local_time(clip["recorded_at"]),
                "photo" if clip.get("media_kind") == "photo" else fmt_dur(clip["duration_s"])]
        if clip.get("clip_type"):
            meta.append(clip["clip_type"])
        if clip.get("original_name") and clip["original_name"] != clip["file"]:
            meta.append(f"camera file {clip['original_name']}")
        L.append(" · ".join(meta))
        if clip.get("summary"):
            L.append(f"> {clip['summary']}")
        if clip.get("on_screen_text"):
            L.append("On-screen text: " + "; ".join(clip["on_screen_text"]))
        if clip.get("keywords"):
            L.append("Keywords: " + ", ".join(clip["keywords"]))
        for note in clip.get("notes") or []:
            L.append(f"_Note: {note}_")
        L.append("")
        if clip["segments"]:
            for s in clip["segments"]:
                rep = f" (×{s['repeats']})" if s.get("repeats", 1) > 1 else ""
                L.append(f"- [{s['start_ts']} → {s['end_ts']}] #{s['id'].rsplit('#', 1)[1]} {s['text']}{rep}")
        else:
            L.append("_(no speech)_")
        L.append("")
    silent = b.get("silent_clips") or []
    if silent:
        n_ph = sum(s.get("media_kind") == "photo" for s in silent)
        L += [f"## Clips with no speech ({len(silent)})", "",
              "_Visual-only clips (possible b-roll)" + (f"; {n_ph} of them are still photos (marked photo)" if n_ph else "")
              + ":_", ""]
        for s in silent:
            summ = (s.get("summary") or "").strip()
            if len(summ) > 160:
                summ = summ[:157].rsplit(" ", 1)[0] + "…"
            what = "photo" if s.get("media_kind") == "photo" else fmt_dur(s.get("duration_s"))
            L.append(f"- `{s['file']}` · {what} · {s.get('clip_type') or '?'}"
                     + (f" — {summ}" if summ else ""))
        L.append("")
    if b.get("skipped"):
        L += [f"_Skipped {len(b['skipped'])} unreadable sidecar(s)._", ""]
    return "\n".join(L).rstrip() + "\n"


def output_paths(cfg: dict, scope: dict, date: str | None, now: datetime, out_dir: Path | None = None) -> tuple[Path, Path]:
    d = out_dir or exports_dir(cfg)
    base = f"transcripts-{scope['slug']}" + (f"-{date}" if date else "") + f"-{now.strftime('%Y%m%d-%H%M')}"
    n, stem = 1, base
    while (d / f"{stem}.json").exists() or (d / f"{stem}.md").exists():
        n += 1
        stem = f"{base}-{n}"
    return d / f"{stem}.json", d / f"{stem}.md"


def run(cfg: dict, folder: str | None = None, paths: list[str] | None = None, everywhere: bool = False,
        date: str | None = None, include_silent: bool = False, omit_silent: bool = False, collapse_repeats: bool = True,
        dry_run: bool = False, out_dir: Path | None = None, now: datetime | None = None, sources: str = "both",
        include_photos: bool = True) -> dict:
    """Build (and unless dry_run, write) one bundle. Returns a summary dict. ValueError for bad options."""
    if include_silent and omit_silent:
        raise ValueError("--include-silent and --omit-silent are mutually exclusive")
    now = now or datetime.now().astimezone()
    date = norm_date(date)
    scope = resolve_scope(cfg, folder, paths, everywhere)
    if sources not in ("both", "store", "sidecars"):
        raise ValueError(f"sources must be both, store or sidecars (got {sources!r})")
    b = build_bundle(cfg, scope, date, include_silent, omit_silent, collapse_repeats, now, sources, include_photos)
    md = build_markdown(b)
    js = json.dumps(b, indent=2, ensure_ascii=False) + "\n"
    summary = {"ok": True, "scope": scope["label"], "date": date, "counts": b["counts"],
               "speech_minutes": round(b["total_speech_seconds"] / 60, 1), "total_speech": b["total_speech"],
               "dry_run": dry_run, "json": None, "md": None}
    if dry_run:
        summary.update(json_bytes=len(js.encode()), md_bytes=len(md.encode()))
        summary["message"] = (f"Dry run: {b['counts']['clips_with_speech']} clip(s) with speech of "
                              f"{b['counts']['clips_scanned']} found · {summary['speech_minutes']} min — nothing written")
        return summary
    jp, mp = output_paths(cfg, scope, date, now, out_dir)
    atomic_write_text(jp, js)
    atomic_write_text(mp, md)
    summary.update(json=str(jp), md=str(mp), json_bytes=jp.stat().st_size, md_bytes=mp.stat().st_size)
    summary["message"] = (f"{b['counts']['clips_with_speech']} clip(s) with speech of {b['counts']['clips_scanned']} · "
                          f"{summary['speech_minutes']} min of speech → {jp.name} + {mp.name}")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="Consolidate clip transcripts (from .json sidecars) into one JSON + Markdown bundle")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--folder", help="only inbox/<FOLDER>/ (a batch folder)")
    g.add_argument("--path", action="append", help="a folder on the Lexar, relative to the drive root (repeatable)")
    g.add_argument("--everywhere", action="store_true", help="inbox/ + every Lexar search root (transcripts.search_roots)")
    ap.add_argument("--date", help="only clips recorded on this day (YYYY-MM-DD or YYYYMMDD)")
    s = ap.add_mutually_exclusive_group()
    s.add_argument("--include-silent", action="store_true", help="full entries for clips with no speech (default: brief list)")
    s.add_argument("--omit-silent", action="store_true", help="leave clips with no speech out entirely")
    ap.add_argument("--keep-repeats", action="store_true", help="don't collapse identical consecutive lines")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--store-only", action="store_true", help="read only the central notes store (ignore sidecars)")
    src.add_argument("--sidecars-only", action="store_true", help="read only .json sidecars found on disk")
    ap.add_argument("--exclude-photos", action="store_true", help="leave still photos out (default: listed as silent assets)")
    ap.add_argument("--dry-run", action="store_true", help="scan and report, write nothing")
    ap.add_argument("--out-dir", type=Path, help="output folder (default exports/)")
    ap.add_argument("--json", action="store_true", help="print a JSON summary as the last line")
    ap.add_argument("--list-folders", action="store_true", help="print folders that contain clip sidecars and exit")
    args = ap.parse_args()
    cfg = load_config()
    if args.list_folders:
        print(json.dumps(discover_folders(cfg), indent=2, ensure_ascii=False))
        return 0
    try:
        res = run(cfg, args.folder, args.path, args.everywhere, args.date, args.include_silent, args.omit_silent,
                  not args.keep_repeats, args.dry_run, args.out_dir,
                  sources="store" if args.store_only else ("sidecars" if args.sidecars_only else "both"),
                  include_photos=not args.exclude_photos)
    except ValueError as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}))
        else:
            print(f"error: {e}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(res, ensure_ascii=False))
    else:
        c = res["counts"]
        print(res["message"])
        print(f"  scope: {res['scope']}" + (f" · date {res['date']}" if res["date"] else ""))
        print(f"  clips found {c['clips_scanned']} · with speech {c['clips_with_speech']} · silent {c['clips_silent']} · "
              f"lines {c['segments']} · speech {res['total_speech']}" + (f" · skipped {c['skipped_sidecars']}" if c["skipped_sidecars"] else ""))
        print(f"  notes from store {c['from_store']} · from sidecars {c['from_sidecars']} · video not found {c['video_not_found']}"
              + f" · photos {c.get('photos', 0)}")
        if res["json"]:
            print(f"  {res['json']} ({res['json_bytes']:,} bytes)\n  {res['md']} ({res['md_bytes']:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
