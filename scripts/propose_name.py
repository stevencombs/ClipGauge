#!/usr/bin/env python3
"""Build {YYYYMMDD}_{project}_{subject}_{clipType}[_t##].ext from a validated description."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import load_config, section, slugify  # noqa: E402


def taken_names(dirs: list[Path], exclude: Path | None = None) -> set[str]:
    """Lower-cased filenames already present in dirs (ignores ExFAT ._ files)."""
    names: set[str] = set()
    for d in dirs:
        if d and d.is_dir():
            for p in d.iterdir():
                if p.name.startswith("._") or (exclude and p.resolve() == exclude.resolve()):
                    continue
                names.add(p.name.lower())
    return names


def strip_words(slug: str, words: set[str]) -> str:
    """Drop slug tokens that just repeat the clip type ('eink-readers-unboxing' -> 'eink-readers')."""
    kept = [w for w in slug.split("-") if w and w not in words]
    return "-".join(kept) if kept else slug


def propose_filename(
    date_str: str,
    project: str,
    subject: str,
    clip_type: str,
    ext: str,
    cfg: dict | None = None,
    taken: set[str] | None = None,
    keep_project: bool = False,
) -> str:
    """Return a new basename. Appends _t01.._t99 (take #) only when the plain name is taken.
    keep_project=True (project taken from the batch folder name): the project slug is used as given — no clip-type
    word stripping, so every clip of the batch gets the same {project}."""
    ncfg = section(cfg or {}, "naming")
    project = slugify(project, int(ncfg.get("project_max_len") or 24)) or ncfg.get("fallback_project", "misc")
    subject = slugify(subject, int(ncfg.get("subject_max_len") or 32)) or ncfg.get("fallback_subject", "clip")
    clip_type = slugify(clip_type, 24) or "broll"
    if ncfg.get("strip_clip_type_words", True):
        words = {clip_type, *clip_type.split("-")} if clip_type != "talking-head" else {"talking", "head", "talkinghead"}
        project, subject = (project if keep_project else strip_words(project, words)), strip_words(subject, words)
    if ext and not ext.startswith("."):
        ext = "." + ext
    if ncfg.get("lowercase_ext", True):
        ext = ext.lower()
    base = f"{date_str}_{project}_{subject}_{clip_type}"
    taken = taken or set()
    name = f"{base}{ext}"
    if name.lower() not in taken:
        return name
    for n in range(1, int(ncfg.get("take_suffix_max") or 99) + 1):
        name = f"{base}_t{n:02d}{ext}"
        if name.lower() not in taken:
            return name
    raise RuntimeError(f"No free take number for {base}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Preview a filename from parts (no file operations)")
    ap.add_argument("date"), ap.add_argument("project"), ap.add_argument("subject")
    ap.add_argument("clip_type"), ap.add_argument("ext")
    a = ap.parse_args()
    print(propose_filename(a.date, a.project, a.subject, a.clip_type, a.ext, load_config()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
