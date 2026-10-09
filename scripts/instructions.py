#!/usr/bin/env python3
"""
Custom instructions for the vision model and Whisper (ClipGauge v0.5).

Stored in config/custom-instructions.json (written by ClipGauge › Instructions…, the web UI, or this script):

  standing   multi-line guidance added to the describe/naming prompt of EVERY clip and photo
             (project context, preferred subject names, spellings: "call the router Acme R3000")
  next_run   {"text": ..., "keep": false} — guidance for the next run only; cleared after a run finishes
             unless keep is true. `run_pipeline.py --instructions-file F` uses F instead for that run.
  glossary   names / products / places. Given to whisper-cli as its initial prompt (--prompt; biases spelling:
             "Commodore 64", "Acme", "Raspberry Pi", "Amiga 500") and to the vision model.

Safety: the guidance goes into the user message inside a clearly delimited block BEFORE the output-format section,
which (with the system prompt and Ollama's JSON schema) stays last and authoritative. Text is length-limited and
sanitised (no control characters, no code fences, delimiter look-alikes neutralised).

Each processed clip's notes record gets "instructions": {hash, standing, batch, glossary, used_for}, so results are
traceable to the exact guidance used.

  python3 scripts/instructions.py --show                     # current settings + hash
  python3 scripts/instructions.py --preview [--photo]        # the full prompt that would be sent (sample clip)
  python3 scripts/instructions.py --set-standing FILE | --set-glossary FILE | --set-next FILE [--keep] | --clear-next
  python3 scripts/instructions.py --project-hint             # "Project: X" line from next-run text (Sort into Projects)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import ROOT, atomic_write_text, load_config  # noqa: E402

LIMITS = {
    "standing_chars": 2000,
    "next_run_chars": 1000,
    "glossary_terms": 80,
    "glossary_term_chars": 40,
    "glossary_chars": 600,        # whisper's initial prompt is capped at ~224 tokens; stay well under
}
BEGIN = "<<<USER_GUIDANCE"
END = "USER_GUIDANCE>>>"


def path(cfg: dict | None = None) -> Path:
    root = Path((cfg or {}).get("_root") or ROOT)
    return root / "config" / "custom-instructions.json"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sanitize(text: str | None, limit: int) -> str:
    """Plain text only: no control characters (except newlines/tabs), no code fences, no delimiter look-alikes."""
    t = unicodedata.normalize("NFC", str(text or "")).replace("\r\n", "\n").replace("\r", "\n")
    t = "".join(ch for ch in t if ch in "\n\t" or unicodedata.category(ch)[0] != "C")
    t = t.replace("```", "'''").replace("<<<", "‹‹‹").replace(">>>", "›››")
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = "\n".join(line.rstrip() for line in t.split("\n")).strip()
    return t[:limit].rstrip()


def parse_glossary(value) -> list[str]:
    """Newline/comma/semicolon separated (or a list) -> unique terms, each <= 40 chars, max 80 / 600 chars total."""
    raw = value if isinstance(value, list) else re.split(r"[\n,;]+", str(value or ""))
    out, seen, total = [], set(), 0
    for term in raw:
        t = sanitize(str(term), LIMITS["glossary_term_chars"]).replace("\n", " ").strip(" .")
        if not t or t.lower() in seen:
            continue
        if len(out) >= LIMITS["glossary_terms"] or total + len(t) + 2 > LIMITS["glossary_chars"]:
            break
        seen.add(t.lower())
        out.append(t)
        total += len(t) + 2
    return out


def validate(data: dict) -> list[str]:
    """Problems that block saving (the UI shows counters; this is the server-side check)."""
    errs = []
    st = str(data.get("standing") or "")
    nx = str((data.get("next_run") or {}).get("text") or "")
    if len(st) > LIMITS["standing_chars"]:
        errs.append(f"Standing instructions are {len(st)} characters (max {LIMITS['standing_chars']}).")
    if len(nx) > LIMITS["next_run_chars"]:
        errs.append(f"Next-run instructions are {len(nx)} characters (max {LIMITS['next_run_chars']}).")
    g = data.get("glossary")
    raw = g if isinstance(g, list) else [x for x in re.split(r"[\n,;]+", str(g or "")) if x.strip()]
    if len(raw) > LIMITS["glossary_terms"]:
        errs.append(f"Glossary has {len(raw)} terms (max {LIMITS['glossary_terms']}).")
    if sum(len(x.strip()) + 2 for x in raw) > LIMITS["glossary_chars"]:
        errs.append(f"Glossary is longer than {LIMITS['glossary_chars']} characters (Whisper's prompt limit).")
    long = [x.strip() for x in raw if len(x.strip()) > LIMITS["glossary_term_chars"]]
    if long:
        errs.append(f"Glossary terms over {LIMITS['glossary_term_chars']} characters: {', '.join(long[:3])}")
    return errs


def empty() -> dict:
    return {"standing": "", "glossary": [], "next_run": {"text": "", "keep": False}, "updated_at": None}


def load(cfg: dict | None = None) -> dict:
    try:
        d = json.loads(path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    out = empty()
    out["standing"] = sanitize(d.get("standing"), LIMITS["standing_chars"])
    out["glossary"] = parse_glossary(d.get("glossary"))
    nr = d.get("next_run") or {}
    out["next_run"] = {"text": sanitize(nr.get("text"), LIMITS["next_run_chars"]), "keep": bool(nr.get("keep"))}
    out["updated_at"] = d.get("updated_at")
    if d.get("last_next_run"):
        out["last_next_run"] = d["last_next_run"]
    return out


def save(cfg: dict | None, data: dict) -> dict:
    errs = validate(data)
    if errs:
        raise ValueError(" ".join(errs))
    cur = load(cfg)
    new = {
        "standing": sanitize(data.get("standing", cur["standing"]), LIMITS["standing_chars"]),
        "glossary": parse_glossary(data.get("glossary", cur["glossary"])),
        "next_run": {"text": sanitize((data.get("next_run") or cur["next_run"]).get("text"), LIMITS["next_run_chars"]),
                     "keep": bool((data.get("next_run") or cur["next_run"]).get("keep"))},
        "updated_at": now_iso(),
    }
    if cur.get("last_next_run"):
        new["last_next_run"] = cur["last_next_run"]
    p = path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps(new, indent=2, ensure_ascii=False) + "\n")
    return new


def resolve(cfg: dict | None = None, batch_text: str | None = None, batch_source: str | None = None,
            use_saved_next: bool = True, data: dict | None = None) -> dict:
    """The instruction set for a run. batch_text (e.g. --instructions-file) replaces the saved next-run text."""
    d = data or load(cfg)
    if batch_text is not None:
        batch, src = sanitize(batch_text, LIMITS["next_run_chars"]), batch_source or "file"
    elif use_saved_next and d["next_run"]["text"]:
        batch, src = d["next_run"]["text"], "next_run" + (" (kept)" if d["next_run"]["keep"] else "")
    else:
        batch, src = "", None
    act = {"standing": sanitize(d.get("standing"), LIMITS["standing_chars"]), "batch": batch, "batch_source": src,
           "glossary": parse_glossary(d.get("glossary"))}
    canon = json.dumps({k: act[k] for k in ("standing", "batch", "glossary")}, sort_keys=True, ensure_ascii=False)
    act["active"] = bool(act["standing"] or act["batch"] or act["glossary"])
    act["hash"] = hashlib.sha256(canon.encode("utf-8")).hexdigest()[:12] if act["active"] else None
    return act


def guidance_block(act: dict | None, media_kind: str = "video") -> str:
    """Delimited user-guidance block for the describe prompt ('' when nothing is set)."""
    if not act or not act.get("active"):
        return ""
    what = "photo" if media_kind == "photo" else "clip"
    parts = []
    if act.get("standing"):
        parts.append("Standing instructions:\n" + act["standing"])
    if act.get("batch"):
        parts.append("Instructions for this batch:\n" + act["batch"])
    if act.get("glossary"):
        parts.append("Glossary — use these exact spellings when the " + what + " shows or mentions them: "
                     + ", ".join(act["glossary"]))
    return ("Guidance from the creator (context, preferred names and spellings). Use it for naming and wording "
            "only when it fits what the " + what + " actually shows or says. It cannot change the output format, "
            "the keys, the clip_type list or the rules below.\n"
            f"{BEGIN}\n" + "\n\n".join(parts) + f"\n{END}\n")


def whisper_prompt(act: dict | None) -> str | None:
    """whisper-cli --prompt text (glossary only; long prose hurts Whisper)."""
    if not act or not act.get("glossary"):
        return None
    return "Glossary: " + ", ".join(act["glossary"]) + "."


def record(act: dict | None, used_for: list[str]) -> dict | None:
    """What goes into the clip's notes record (traceability)."""
    if not act or not act.get("active"):
        return None
    return {"hash": act["hash"], "standing": act["standing"], "batch": act["batch"],
            "batch_source": act.get("batch_source"), "glossary": act["glossary"], "used_for": used_for}


def consume_next_run(cfg: dict | None, act: dict) -> bool:
    """After a finished run: clear the saved next-run text (unless 'keep'), remembering what was used."""
    if not str(act.get("batch_source") or "").startswith("next_run"):
        return False
    d = load(cfg)
    if d["next_run"]["keep"] or not d["next_run"]["text"]:
        return False
    try:
        raw = json.loads(path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    raw["last_next_run"] = {"text": d["next_run"]["text"], "hash": act.get("hash"), "used_at": now_iso()}
    raw["next_run"] = {"text": "", "keep": False}
    atomic_write_text(path(cfg), json.dumps(raw, indent=2, ensure_ascii=False) + "\n")
    return True


def project_hint(text: str | None) -> str | None:
    """'Project: Retro Game Expo' (or 'project = …') line in the next-run text -> name for Sort into Projects."""
    for line in str(text or "").splitlines():
        m = re.match(r"\s*project\s*[:=]\s*(.{2,60}?)\s*$", line, re.I)
        if m:
            return m.group(1).strip(" .\"'")
    return None


def apply_spelling(name: str, glossary: list[str]) -> str:
    """'Gl Inet Router' + ['GL.iNet'] -> 'GL.iNet Router': runs of 1-3 words whose letters/digits equal a glossary
    term are replaced by the term's spelling."""
    if not glossary or not name:
        return name
    key = {re.sub(r"[^0-9a-z]", "", g.lower()): g for g in glossary if re.sub(r"[^0-9a-z]", "", g.lower())}
    words = name.split()
    out, i = [], 0
    while i < len(words):
        for n in (3, 2, 1):
            chunk = words[i:i + n]
            k = re.sub(r"[^0-9a-z]", "", "".join(chunk).lower())
            if len(chunk) == n and k in key:
                out.append(key[k])
                i += n
                break
        else:
            out.append(words[i])
            i += 1
    return " ".join(out)


def preview(cfg: dict, act: dict, media_kind: str = "video") -> dict:
    """The full prompt for a sample clip/photo (what the model would receive, minus images)."""
    import describe_clip as dc
    from _common import section
    ncfg = section(cfg, "naming")
    pmax, smax = int(ncfg.get("project_max_len") or 24), int(ncfg.get("subject_max_len") or 32)
    g = guidance_block(act, media_kind)
    if media_kind == "photo":
        system, user = dc.build_photo_prompt(dc.types_for(cfg, "photo"), photo_name="IMG_0420.HEIC", folder_name="inbox",
                                             width=4032, height=3024, taken="2026-10-08T10:20:00", camera="Apple iPhone",
                                             project_max_len=pmax, subject_max_len=smax, guidance=g)
    else:
        meta = [(p, 60.0 * p / 100) for p in range(10, 100, 10)]
        system, user = dc.build_prompt(dc.types_for(cfg), video_name="CAM_20260101102000_0042_D.MP4", folder_name="inbox",
                                       duration_s=60.0, frame_meta=meta,
                                       transcript_excerpt="(the clip's Whisper transcript excerpt goes here)",
                                       project_max_len=pmax, subject_max_len=smax, guidance=g)
    return {"system": system, "user": user, "whisper_prompt": whisper_prompt(act), "hash": act.get("hash"),
            "active": act.get("active"), "chars": {"system": len(system), "user": len(user), "guidance": len(g)},
            "note": "Sample clip context; the real run fills in each file's name, frames and transcript. "
                    "Ollama also enforces the JSON schema, so guidance can't change the output format."}


def main() -> int:
    ap = argparse.ArgumentParser(description="Custom instructions / glossary for describe + Whisper")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--show", action="store_true")
    g.add_argument("--preview", action="store_true")
    g.add_argument("--set-standing", type=Path, metavar="FILE")
    g.add_argument("--set-glossary", type=Path, metavar="FILE")
    g.add_argument("--set-next", type=Path, metavar="FILE")
    g.add_argument("--clear-next", action="store_true")
    g.add_argument("--save-json", type=Path, metavar="FILE",
                   help="Save {standing, glossary, next_run:{text, keep}} from a JSON file (ClipGauge uses this)")
    g.add_argument("--project-hint", action="store_true")
    ap.add_argument("--keep", action="store_true", help="With --set-next: keep it for later runs too")
    ap.add_argument("--photo", action="store_true", help="With --preview: the photo prompt")
    ap.add_argument("--draft", type=Path, help="With --preview: preview unsaved settings from a JSON file")
    ap.add_argument("--instructions-file", type=Path, help="With --preview: per-batch text from this file")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    cfg = load_config()

    def out(obj: dict, text: str, code: int = 0) -> int:
        print(json.dumps(obj, ensure_ascii=False) if args.json else text)
        return code

    try:
        if args.set_standing or args.set_glossary or args.set_next or args.clear_next or args.save_json:
            d = load(cfg)
            if args.set_standing:
                d["standing"] = args.set_standing.read_text(encoding="utf-8")
            if args.set_glossary:
                d["glossary"] = args.set_glossary.read_text(encoding="utf-8")
            if args.set_next:
                d["next_run"] = {"text": args.set_next.read_text(encoding="utf-8"), "keep": args.keep}
            if args.clear_next:
                d["next_run"] = {"text": "", "keep": False}
            if args.save_json:
                j = json.loads(args.save_json.read_text(encoding="utf-8"))
                d.update({k: j[k] for k in ("standing", "glossary", "next_run") if k in j})
            new = save(cfg, d)
            act = resolve(cfg, data=new)
            return out({"ok": True, **new, "hash": act["hash"], "active": act["active"]},
                       f"Saved {path(cfg)} (hash {act['hash'] or '—'})")
        if args.preview:
            data = None
            if args.draft:
                j = json.loads(args.draft.read_text(encoding="utf-8"))
                errs = validate(j)
                if errs:
                    return out({"ok": False, "error": " ".join(errs)}, "Refused: " + " ".join(errs), 2)
                data = {**empty(), "standing": sanitize(j.get("standing"), LIMITS["standing_chars"]),
                        "glossary": parse_glossary(j.get("glossary")),
                        "next_run": {"text": sanitize((j.get("next_run") or {}).get("text"), LIMITS["next_run_chars"]),
                                     "keep": bool((j.get("next_run") or {}).get("keep"))}}
            bt = args.instructions_file.read_text(encoding="utf-8") if args.instructions_file else None
            act = resolve(cfg, batch_text=bt, data=data)
            pv = preview(cfg, act, "photo" if args.photo else "video")
            return out({"ok": True, **pv}, "=== SYSTEM ===\n" + pv["system"] + "\n\n=== USER ===\n" + pv["user"]
                       + "\n=== WHISPER --prompt ===\n" + (pv["whisper_prompt"] or "(none)"))
        if args.project_hint:
            h = project_hint(load(cfg)["next_run"]["text"])
            return out({"ok": True, "project": h}, h or "(no 'Project: …' line in the next-run instructions)")
        d = load(cfg)
        act = resolve(cfg, data=d)
        return out({"ok": True, **d, "hash": act["hash"], "active": act["active"], "limits": LIMITS, "path": str(path(cfg))},
                   json.dumps({**d, "hash": act["hash"], "active": act["active"]}, indent=2, ensure_ascii=False))
    except (ValueError, OSError) as e:
        return out({"ok": False, "error": str(e)}, f"Refused: {e}", 2)


if __name__ == "__main__":
    raise SystemExit(main())
