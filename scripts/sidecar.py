#!/usr/bin/env python3
"""
Searchable sidecars: <video stem>.json (full record) + <video stem>.md (human summary).

Location:
  dry_run true                     -> sidecar.dry_run_dir/<source folder name>/  (default logs/dry-run/)
  source under a protected marker  -> same dry-run dir (never write into DaVinci Resolve trees)
  otherwise                        -> next to the video
ExFAT on macOS will add ._ AppleDouble files beside them; harmless.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import abs_path, atomic_write_text, load_config, section  # noqa: E402

SCHEMA_VERSION = 1


def sidecar_dir(video: Path, cfg: dict, dry_run: bool) -> tuple[Path, str]:
    scfg = section(cfg, "sidecar")
    dry_dir = abs_path(scfg.get("dry_run_dir") or "logs/dry-run") / (video.parent.name or "_root")
    if dry_run:
        return dry_dir, "dry_run"
    src = str(video.resolve())
    for marker in scfg.get("protected_path_markers") or []:
        if marker and marker in src:
            return dry_dir, f"protected path ({marker.strip('/')})"
    return video.parent, "next_to_video"


def _fmt_dur(s: float | None) -> str:
    if not s:
        return "?"
    m, sec = divmod(int(round(s)), 60)
    return f"{m}m{sec:02d}s" if m else f"{sec}s"


def build_markdown(rec: dict) -> str:
    src = rec.get("source") or {}
    desc = (rec.get("describe") or {}).get("description") or {}
    dres = rec.get("describe") or {}
    tx = rec.get("transcript") or {}
    prop = rec.get("proposal") or {}
    conf = desc.get("confidence")
    lines = [f"# {src.get('name', 'clip')}", ""]
    lines.append(f"**Proposed filename:** `{prop.get('new_name') or '(none)'}`  ")
    lines.append(
        f"**Confidence:** {conf if conf is not None else 'n/a'} · "
        f"**Clip type:** {desc.get('clip_type', 'n/a')} · "
        f"**Needs review:** {'YES' if prop.get('needs_review') else 'no'}  "
    )
    if prop.get("review_reasons"):
        lines.append(f"**Review reasons:** {'; '.join(prop['review_reasons'])}  ")
    applied = rec.get("applied") or {}
    if applied.get("action") == "renamed":
        lines.append(f"**Renamed in place:** `{os.path.basename(str(applied.get('original_path')))}` → "
                     f"`{os.path.basename(str(applied.get('new_path')))}` ({applied.get('time')})"
                     + (f"  \n**Folder:** `inbox/{applied['folder']}/`" if applied.get("folder") else ""))
    elif applied.get("action") == "needs-review":
        lines.append(f"**Needs review:** original filename kept in inbox/ ({applied.get('time')})")
    else:
        lines.append(f"**Dry run:** {'yes — nothing renamed' if rec.get('dry_run') else 'no (live: renamed in place when confident)'}")
    lines += ["", "## Summary", "", desc.get("summary") or f"_No description ({dres.get('status')}: {dres.get('reason')})_", ""]
    kws = desc.get("keywords") or []
    lines += ["## Keywords", "", ", ".join(kws) if kws else "_none_"]
    if kws:
        lines += ["", " ".join("#" + k.replace(" ", "-") for k in kws)]
    lines.append("")
    if desc.get("subjects") or desc.get("objects"):
        lines += ["## Subjects / objects", ""]
        lines += [f"- {s}" for s in desc.get("subjects", [])]
        if desc.get("objects"):
            lines.append(f"- Also visible: {', '.join(desc['objects'])}")
        lines.append("")
    lines += ["## On-screen text", ""]
    lines += [f"- {t}" for t in desc.get("on_screen_text", [])] or ["_none detected_"]
    lines += ["", "## Transcript excerpt", ""]
    if tx.get("excerpt"):
        lines += ["> " + tx["excerpt"].replace("\n", "\n> ")]
    else:
        lines.append(f"_Transcript {tx.get('status', 'not run')}: {tx.get('reason') or ''}_")
    lines += ["", "## Details", ""]
    lines.append(f"- Source: `{src.get('path')}`")
    lines.append(
        f"- Duration: {_fmt_dur(src.get('duration_s'))} · Audio: {'yes' if src.get('has_audio') else 'no'} · "
        f"Resolution: {src.get('width')}x{src.get('height')} · Date: {src.get('date')} ({src.get('date_source')})"
    )
    fr = rec.get("frames") or {}
    lines.append(f"- Frames: `{fr.get('dir')}` ({fr.get('count', 0)})")
    lines.append(
        f"- Model: {dres.get('model') or 'n/a'} · describe {dres.get('elapsed_s', 0)} s · attempts {dres.get('attempts', 0)}"
    )
    if tx.get("model"):
        lines.append(f"- Whisper: {tx.get('model')} · {tx.get('elapsed_s', 0)} s")
    tm = rec.get("timing_s") or {}
    if tm:
        lines.append("- Timing: " + ", ".join(f"{k} {v}s" for k, v in tm.items()))
    lines.append(f"- Generated: {rec.get('generated_at')}")
    return "\n".join(lines) + "\n"


def write_sidecars(video: Path, record: dict, cfg: dict, dry_run: bool, out_dir: Path | None = None) -> dict:
    """Write .json/.md sidecars; returns {dir, json, md, location}. Never touches the video itself."""
    scfg = section(cfg, "sidecar")
    if out_dir is not None:
        d, why = out_dir, "explicit"
    else:
        d, why = sidecar_dir(video, cfg, dry_run)
    stem = video.stem
    out = {"dir": str(d), "location": why, "json": None, "md": None}
    record = dict(record, schema_version=SCHEMA_VERSION)
    record["sidecar"] = {k: v for k, v in out.items()}
    if scfg.get("write_json", True):
        p = atomic_write_text(d / f"{stem}.json", json.dumps(record, indent=2, ensure_ascii=False) + "\n")
        out["json"] = str(p)
    if scfg.get("write_md", True):
        p = atomic_write_text(d / f"{stem}.md", build_markdown(record))
        out["md"] = str(p)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Re-render the .md sidecar from an existing .json sidecar")
    ap.add_argument("json_sidecar", type=Path)
    args = ap.parse_args()
    load_config()
    rec = json.loads(args.json_sidecar.read_text(encoding="utf-8"))
    md = args.json_sidecar.with_suffix(".md")
    atomic_write_text(md, build_markdown(rec))
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
