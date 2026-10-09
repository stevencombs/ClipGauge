#!/usr/bin/env python3
"""
Describe one clip with a local VLM (Ollama HTTP API, default qwen2.5vl:7b).

Sends the 9 sampled frames (downscaled copies) + optional transcript excerpt and
asks for STRICT JSON:
  summary, subjects, objects, on_screen_text, clip_type (allowlist),
  suggested_project, suggested_subject, keywords, confidence (0-1)
The reply is validated; one retry with a corrective message on bad JSON.
Photos (media_kind="photo"): ONE image (already resized by photos.prepare_photo), a still-photo prompt, no
transcript; clip_type from the same allowlist plus the generic fallback "photo" (videos never get "photo").
Nothing here renames anything.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import abs_path, load_config, section, slugify, tool_path  # noqa: E402
from status import write_status  # noqa: E402

DEFAULT_URL = "http://127.0.0.1:11434"

CLIP_TYPE_HINTS = {
    "gameplay": "video-game footage or screen capture of a game being played",
    "broll": "supplementary/cutaway footage: product pans, close-ups, hands, scenery; nobody addressing the camera",
    "talking-head": "a person speaking to the camera (presenter / review commentary)",
    "bench": "benchmarks, tests or measurements: charts, scores, meters, performance runs",
    "boot": "a device powering on or booting: logos, startup / setup screens",
    "menu": "navigating software UI: menus, settings, library or system screens",
    "unboxing": "opening packaging and revealing the product and its accessories",
    "fail": "mistakes, bloopers, broken or failed attempts",
    "photo": "a still photo that fits none of the other types (generic fallback)",
}
PHOTO_TYPE = "photo"
# what each existing type means for a STILL photo (types without an entry keep CLIP_TYPE_HINTS)
PHOTO_TYPE_HINTS = {
    "gameplay": "a screenshot / photo of a video game being played",
    "broll": "a product shot, close-up, detail, scenery or place: usable as a cutaway still",
    "talking-head": "a portrait of the presenter / a person posing for the camera",
    "bench": "benchmark results, test readings, charts, scores or a measuring setup",
    "boot": "a device's boot / logo / startup / setup screen",
    "menu": "a screenshot or photo of software UI: menus, settings, library screens",
    "unboxing": "packaging, an opened box, the product with its accessories laid out",
    "fail": "something broken, a mistake or a failed attempt",
    "photo": "generic still photo — use ONLY when none of the types above fits",
}

CLIP_TYPE_ALIASES = {
    "b-roll": "broll",
    "b-rolls": "broll",
    "brolls": "broll",
    "cutaway": "broll",
    "talkinghead": "talking-head",
    "talking-heads": "talking-head",
    "a-roll": "talking-head",
    "presenter": "talking-head",
    "benchmark": "bench",
    "benchmarks": "bench",
    "benchmarking": "bench",
    "booting": "boot",
    "boot-up": "boot",
    "bootup": "boot",
    "startup": "boot",
    "menus": "menu",
    "unbox": "unboxing",
    "unboxings": "unboxing",
    "fails": "fail",
    "blooper": "fail",
    "bloopers": "fail",
    "game-play": "gameplay",
    "gaming": "gameplay",
    "still": "photo",
    "picture": "photo",
    "image": "photo",
    "photograph": "photo",
}


def types_for(cfg: dict, media_kind: str = "video") -> list[str]:
    """Allowed clip_type values: config clip_types; 'photo' is only offered for photos (always, as the fallback)."""
    types = [t for t in (cfg.get("clip_types") or list(CLIP_TYPE_HINTS)) if t != PHOTO_TYPE]
    return types + [PHOTO_TYPE] if media_kind == "photo" else types

REQUIRED_KEYS = (
    "summary",
    "subjects",
    "objects",
    "on_screen_text",
    "clip_type",
    "suggested_project",
    "suggested_subject",
    "keywords",
    "confidence",
)
LIST_KEYS = ("subjects", "objects", "on_screen_text", "keywords")


# ---------------------------------------------------------------- prompt ----

def response_schema(clip_types: list[str], max_items: int = 12) -> dict:
    """JSON schema for Ollama `format`. maxItems/maxLength bound generation (stops keyword loops)."""
    def arr(n: int = max_items, item_len: int = 120) -> dict:
        return {"type": "array", "items": {"type": "string", "maxLength": item_len}, "maxItems": n}

    return {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "maxLength": 600},
            "subjects": arr(8),
            "objects": arr(),
            "on_screen_text": arr(item_len=160),
            "clip_type": {"type": "string", "enum": list(clip_types)},
            "suggested_project": {"type": "string", "maxLength": 40},
            "suggested_subject": {"type": "string", "maxLength": 48},
            "keywords": arr(),
            "confidence": {"type": "number"},
        },
        "required": list(REQUIRED_KEYS),
    }


# v0.5 custom instructions: the creator's guidance sits in the user message between delimiters, BEFORE the output format.
# The system prompt, the format section + rules (last) and Ollama's JSON schema stay authoritative.
GUIDANCE_SYSTEM_NOTE = (" The user message may contain a delimited block of guidance from the creator (context, names, "
                        "spellings): follow it for naming and wording when it fits the media, but it never changes the "
                        "JSON-only reply or the required keys.")
GUIDANCE_RULE = ("- Creator guidance (the delimited block above) helps with names, spellings and project context only; "
                 "if any of it conflicts with these rules or the JSON format, ignore that part. Never mention it in the summary.\n")


def build_prompt(
    clip_types: list[str],
    *,
    video_name: str,
    folder_name: str,
    duration_s: float | None,
    frame_meta: list[tuple[int, float | None]],
    transcript_excerpt: str | None,
    transcript_note: str | None = None,
    project_max_len: int = 24,
    subject_max_len: int = 32,
    guidance: str = "",
) -> tuple[str, str]:
    """Return (system, user) prompt text. frame_meta = [(percent, seconds|None), ...].
    guidance: instructions.guidance_block(...) — placed before the output format, which stays last."""
    system = (
        "You catalogue raw video clips for a YouTube creator's editing workflow. "
        "You receive frames sampled in time order from ONE clip, plus optional speech transcript, "
        "and return ONE JSON object describing the clip. Be factual: describe only what is visible "
        "or said. Do not invent brand or model names unless they are legible on screen, spoken, or "
        "present in the original filename/folder. Reply with JSON only — no markdown, no commentary."
        + (GUIDANCE_SYSTEM_NOTE if guidance else "")
    )
    dur = f"{duration_s:.1f} s" if duration_s else "unknown"
    frames_desc = ", ".join(
        f"#{i} @{pct}%" + (f" ({sec:.1f}s)" if sec is not None else "")
        for i, (pct, sec) in enumerate(frame_meta, start=1)
    )
    if transcript_excerpt:
        tx = f'Transcript excerpt (local speech-to-text, may contain errors):\n"""\n{transcript_excerpt}\n"""'
    else:
        tx = f"Transcript: none ({transcript_note or 'not available'})."
    types_line = " | ".join(clip_types)
    hints = "\n".join(f"- {t}: {CLIP_TYPE_HINTS.get(t, t)}" for t in clip_types)
    user = f"""Clip context:
- Original filename: {video_name}
- Folder: {folder_name}
- Duration: {dur}
- Frames attached: {len(frame_meta)}, in time order: {frames_desc}
{tx}
{("\n" + guidance) if guidance else ""}
Return ONE JSON object with exactly these keys:
{{
  "summary": "1-3 sentences: what happens in the clip",
  "subjects": ["main subjects: people, products, devices"],
  "objects": ["other notable visible objects"],
  "on_screen_text": ["legible text exactly as shown (OCR); [] if none"],
  "clip_type": "exactly one of: {types_line}",
  "suggested_project": "kebab-case slug for the project / series / product family",
  "suggested_subject": "kebab-case slug for the specific subject of this clip",
  "keywords": ["5-12 lowercase search keywords"],
  "confidence": <number 0-1>
}}

clip_type definitions:
{hints}

Rules:
- Slugs: lowercase ASCII words joined by '-', no dates, no clip type words; project <= {project_max_len} chars, subject <= {subject_max_len} chars.
- The original filename and folder are strong hints for project/subject naming.
- confidence = how sure you are about clip_type AND the subject naming; use < 0.5 when frames are ambiguous.
- If transcript and frames disagree, trust the frames for clip_type.
{GUIDANCE_RULE if guidance else ""}"""
    return system, user


def build_photo_prompt(
    clip_types: list[str],
    *,
    photo_name: str,
    folder_name: str,
    width: int | None = None,
    height: int | None = None,
    taken: str | None = None,
    camera: str | None = None,
    project_max_len: int = 24,
    subject_max_len: int = 32,
    guidance: str = "",
) -> tuple[str, str]:
    """(system, user) prompt for ONE still photo. guidance: see build_prompt."""
    system = (
        "You catalogue still photos for a YouTube creator's editing workflow (stills used as b-roll, thumbnails "
        "and reference shots). You receive ONE photo and return ONE JSON object describing it. Be factual: describe "
        "only what is visible. Do not invent brand or model names unless they are legible in the photo or present "
        "in the original filename/folder. Reply with JSON only — no markdown, no commentary."
        + (GUIDANCE_SYSTEM_NOTE if guidance else "")
    )
    types_line = " | ".join(clip_types)
    hints = "\n".join(f"- {t}: {PHOTO_TYPE_HINTS.get(t, CLIP_TYPE_HINTS.get(t, t))}" for t in clip_types)
    size = f"{width}x{height}" if width and height else "unknown"
    user = f"""Photo context:
- Original filename: {photo_name}
- Folder: {folder_name}
- Size: {size} px (attached image is downscaled)
- Taken: {taken or 'unknown'}{f"  · camera: {camera}" if camera else ""}
- This is a single still photo (no video, no audio).
{("\n" + guidance) if guidance else ""}
Return ONE JSON object with exactly these keys:
{{
  "summary": "1-2 sentences: what the photo shows",
  "subjects": ["main subjects: people, products, devices, places"],
  "objects": ["other notable visible objects"],
  "on_screen_text": ["legible text in the photo exactly as shown (signs, labels, screens); [] if none"],
  "clip_type": "exactly one of: {types_line}",
  "suggested_project": "kebab-case slug for the project / series / product family / place",
  "suggested_subject": "kebab-case slug for the specific subject of this photo",
  "keywords": ["5-12 lowercase search keywords"],
  "confidence": <number 0-1>
}}

clip_type definitions (for a still photo):
{hints}

Rules:
- Slugs: lowercase ASCII words joined by '-', no dates, no type words (no 'photo', 'picture', 'image'); project <= {project_max_len} chars, subject <= {subject_max_len} chars.
- The original filename and folder are strong hints for project/subject naming.
- Prefer a specific type when it fits; "photo" is the fallback.
- confidence = how sure you are about clip_type AND the subject naming; use < 0.5 when the photo is ambiguous, blurry or dark.
{GUIDANCE_RULE if guidance else ""}"""
    return system, user


def retry_message(errors: list[str], clip_types: list[str]) -> str:
    return (
        "Your previous reply could not be used: "
        + "; ".join(errors)
        + ". Reply again with ONLY one valid JSON object containing exactly these keys: "
        + ", ".join(REQUIRED_KEYS)
        + f". clip_type must be one of: {', '.join(clip_types)}. confidence must be a number between 0 and 1."
    )


# ------------------------------------------------------------ validation ----

def extract_json(text: str) -> dict:
    """Parse a JSON object from model output (tolerates ``` fences / leading chatter)."""
    t = (text or "").strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, re.S | re.I)
    if fence:
        t = fence.group(1)
    try:
        obj = json.loads(t)
    except json.JSONDecodeError:
        a, b = t.find("{"), t.rfind("}")
        if a < 0 or b <= a:
            raise ValueError("no JSON object found in reply") from None
        try:
            obj = json.loads(t[a : b + 1])
        except json.JSONDecodeError as e:
            raise ValueError(f"invalid JSON: {e.msg} at char {e.pos}") from None
    if not isinstance(obj, dict):
        raise ValueError(f"top-level JSON is {type(obj).__name__}, expected object")
    return obj


def normalize_clip_type(value: object, clip_types: list[str]) -> str | None:
    v = re.sub(r"[\s_]+", "-", str(value or "").strip().lower())
    v = CLIP_TYPE_ALIASES.get(v, v)
    return v if v in clip_types else None


def _str_list(val: object, lower: bool = False, cap: int = 30) -> list[str] | None:
    if val is None:
        return []
    if isinstance(val, str):
        val = [val] if val.strip() else []
    if not isinstance(val, list):
        return None
    out: list[str] = []
    seen: set[str] = set()
    for item in val:
        if not isinstance(item, (str, int, float)):
            continue
        s = " ".join(str(item).split())[:200]
        if lower:
            s = s.lower()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out[:cap]


def validate_description(
    obj: object,
    clip_types: list[str],
    project_max_len: int = 24,
    subject_max_len: int = 32,
    max_items: int = 30,
) -> tuple[dict | None, list[str], list[str]]:
    """Return (clean, errors, warnings). clean is None when errors is non-empty."""
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(obj, dict):
        return None, ["reply is not a JSON object"], warnings
    missing = [k for k in REQUIRED_KEYS if k not in obj]
    if missing:
        errors.append(f"missing keys: {', '.join(missing)}")
    clean: dict = {}

    summary = obj.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        errors.append("summary must be a non-empty string")
    else:
        summary = " ".join(summary.split())
        n_sent = len([s for s in re.split(r"(?<=[.!?])\s+", summary) if s])
        if n_sent > 3:
            warnings.append(f"summary has {n_sent} sentences (asked for 1-3)")
        if len(summary) > 700:
            summary = summary[:700].rsplit(" ", 1)[0] + " …"
            warnings.append("summary truncated to 700 chars")
        clean["summary"] = summary

    for key in LIST_KEYS:
        lst = _str_list(obj.get(key), lower=(key == "keywords"), cap=max_items)
        if lst is None:
            errors.append(f"{key} must be an array of strings")
        else:
            clean[key] = lst
    if clean.get("keywords") == []:
        warnings.append("no keywords returned")

    if "clip_type" in obj:
        ct = normalize_clip_type(obj.get("clip_type"), clip_types)
        if ct is None:
            errors.append(f"clip_type {obj.get('clip_type')!r} not in allowlist {clip_types}")
        else:
            if ct != obj.get("clip_type"):
                warnings.append(f"clip_type {obj.get('clip_type')!r} normalized to {ct!r}")
            clean["clip_type"] = ct

    for key, max_len in (("suggested_project", project_max_len), ("suggested_subject", subject_max_len)):
        if key in obj:
            raw = obj.get(key)
            slug = slugify(raw, max_len) if isinstance(raw, (str, int, float)) else ""
            if not slug:
                errors.append(f"{key} is empty after slugify")
            else:
                if slug != raw:
                    warnings.append(f"{key} {raw!r} slugified to {slug!r}")
                clean[key] = slug

    if "confidence" in obj:
        conf = obj.get("confidence")
        try:
            if isinstance(conf, bool):
                raise TypeError
            conf = float(conf)
        except (TypeError, ValueError):
            errors.append(f"confidence {obj.get('confidence')!r} is not a number")
        else:
            if 1.0 < conf <= 100.0:
                warnings.append(f"confidence {conf} looked like a percent; divided by 100")
                conf /= 100.0
            if not 0.0 <= conf <= 1.0:
                errors.append(f"confidence {conf} outside 0-1")
            else:
                clean["confidence"] = round(conf, 3)

    extra = sorted(set(obj) - set(REQUIRED_KEYS))
    if extra:
        warnings.append(f"ignored extra keys: {', '.join(extra)}")
    if errors:
        return None, errors, warnings
    return clean, errors, warnings


# ----------------------------------------------------------------- frames ----

FRAME_RE = re.compile(r"_f(\d{1,3})\.jpe?g$", re.I)


def find_frames(frames_dir: Path, stem: str | None = None) -> list[tuple[int, Path]]:
    """[(percent, path)] sorted by percent; ignores ExFAT ._ files."""
    out = []
    for p in frames_dir.iterdir() if frames_dir.is_dir() else []:
        if p.name.startswith("._") or not p.is_file():
            continue
        if stem and not p.name.startswith(f"{stem}_f"):
            continue
        m = FRAME_RE.search(p.name)
        if m:
            out.append((int(m.group(1)), p))
    return sorted(out)


def prepare_vlm_images(frames: list[Path], max_px: int, ffmpeg: str, out_dir: Path) -> list[Path]:
    """Downscale frames (long side <= max_px) for the VLM; 4K stills would blow up token count."""
    if not max_px:
        return list(frames)
    out_dir.mkdir(parents=True, exist_ok=True)
    scaled = []
    vf = f"scale='if(gt(iw,ih),min({max_px},iw),-2)':'if(gt(iw,ih),-2,min({max_px},ih))'"
    for f in frames:
        dst = out_dir / f.name
        if not (dst.is_file() and dst.stat().st_mtime >= f.stat().st_mtime and dst.stat().st_size > 0):
            subprocess.run(
                [ffmpeg, "-y", "-v", "error", "-i", str(f), "-vf", vf, "-q:v", "3", str(dst)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        scaled.append(dst)
    return scaled


# ----------------------------------------------------------------- ollama ----

def ollama_request(url: str, path: str, payload: dict | None = None, timeout: float = 30) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url.rstrip("/") + path, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def available_models(url: str = DEFAULT_URL, timeout: float = 5) -> list[str]:
    data = ollama_request(url, "/api/tags", None, timeout)
    out: list[str] = []
    for m in data.get("models", []):
        n = m.get("name") or m.get("model")
        # Ollama 0.40 lists each runner variant as its own row (same name) plus an internal rollback shadow
        # 'llamacpp:<64-hex digest>' (ollama/ollama#18830): de-duplicate and never treat the shadow as a model.
        if not n or n in out or re.fullmatch(r"(llamacpp|ggml|mlx|ollama):[0-9a-f]{64}", n):
            continue
        out.append(n)
    return out


def choose_model(cfg: dict, tier_info: dict, available: list[str], override: str | None = None) -> tuple[str | None, list[str]]:
    dcfg = section(cfg, "describe")
    if override:
        cands = [override]
    elif dcfg.get("model"):
        cands = [dcfg["model"]]
    else:
        cands = [tier_info.get("prefer"), tier_info.get("fallback"), *(tier_info.get("alternates") or [])]
    cands = [c for c in cands if c]
    have = set(available)
    for c in cands:
        if c in have or (":" not in c and f"{c}:latest" in have):
            return c, cands
    return None, cands


def _http_error_text(e: urllib.error.HTTPError) -> str:
    try:
        body = e.read().decode("utf-8", "replace")
        try:
            err = json.loads(body).get("error") or body
        except json.JSONDecodeError:
            err = body
        if isinstance(err, dict):
            err = err.get("message") or json.dumps(err)
        return str(err)[:400]
    except Exception:  # noqa: BLE001
        return str(e)


def ollama_chat(url: str, payload: dict, timeout: float) -> dict:
    """POST /api/chat; if the server rejects a JSON-schema `format`, fall back to format=json."""
    try:
        return ollama_request(url, "/api/chat", payload, timeout)
    except urllib.error.HTTPError as e:
        msg = _http_error_text(e)
        if e.code == 400 and isinstance(payload.get("format"), dict) and "format" in msg.lower():
            try:
                return ollama_request(url, "/api/chat", dict(payload, format="json"), timeout)
            except urllib.error.HTTPError as e2:
                msg = _http_error_text(e2)
        raise RuntimeError(f"Ollama HTTP {e.code}: {msg}") from None


def needed_ctx(n_images: int, configured: int, per_image: int = 1100, text_budget: int = 3072) -> int:
    """Ollama/llama-server bills >= ~1024 tokens per image (--image-min-tokens); grow num_ctx to fit."""
    need = n_images * per_image + text_budget
    if configured >= need:
        return configured
    return ((need + 4095) // 4096) * 4096


def describe(
    video: Path,
    frames: list[tuple[int, Path]],
    cfg: dict,
    model: str,
    *,
    duration_s: float | None = None,
    transcript: dict | None = None,
    update_status: bool = True,
    chat_fn=None,
    media_kind: str = "video",
    photo_meta: dict | None = None,
    instructions: dict | None = None,
) -> dict:
    """Run the VLM; returns {status: ok|error, description, attempts, ...}. chat_fn is injectable for tests.
    media_kind="photo": frames = [(0, prepared_jpeg)] (sent as is), photo prompt, photo type list."""
    dcfg = section(cfg, "describe")
    ncfg = section(cfg, "naming")
    photo = media_kind == "photo"
    clip_types = types_for(cfg, media_kind)
    url = dcfg.get("ollama_url") or DEFAULT_URL
    pmax, smax = int(ncfg.get("project_max_len") or 24), int(ncfg.get("subject_max_len") or 32)
    chat_fn = chat_fn or ollama_chat
    t0 = time.monotonic()
    res: dict = {
        "status": "error",
        "reason": None,
        "model": model,
        "attempts": 0,
        "description": None,
        "errors": [],
        "warnings": [],
        "raw_replies": [],
        "frames_sent": 0,
        "image_max_px": int(dcfg.get("max_image_px") or 0),
        "elapsed_s": 0.0,
        "ollama_stats": [],
        "media_kind": media_kind,
    }

    excerpt = None
    note = None
    if transcript:
        if transcript.get("status") == "ok" and transcript.get("excerpt"):
            n = int(dcfg.get("transcript_excerpt_chars") or 1200)
            excerpt = transcript["excerpt"][:n]
        else:
            note = f"{transcript.get('status')}: {transcript.get('reason')}"
    meta = [(pct, (duration_s * pct / 100.0) if duration_s else None) for pct, _ in frames]
    pm = photo_meta or {}
    import instructions as ins
    act = instructions if instructions is not None else cfg.get("_instructions")
    guidance = ins.guidance_block(act, media_kind)
    if guidance:
        res["instructions"] = {"hash": act.get("hash"), "guidance_chars": len(guidance)}
    system, user = build_photo_prompt(
        clip_types,
        photo_name=video.name,
        folder_name=video.parent.name,
        width=pm.get("width"),
        height=pm.get("height"),
        taken=pm.get("taken"),
        camera=pm.get("camera"),
        project_max_len=pmax,
        subject_max_len=smax,
        guidance=guidance,
    ) if photo else build_prompt(
        clip_types,
        video_name=video.name,
        folder_name=video.parent.name,
        duration_s=duration_s,
        frame_meta=meta,
        transcript_excerpt=excerpt,
        transcript_note=note,
        project_max_len=pmax,
        subject_max_len=smax,
        guidance=guidance,
    )

    ffmpeg = tool_path(cfg, "ffmpeg_path", "ffmpeg")
    frame_paths = [p for _, p in frames]
    if frame_paths and photo:
        imgs = frame_paths  # photos.prepare_photo already resized it to describe.max_image_px
    elif frame_paths:
        vlm_dir = frame_paths[0].parent / f"vlm_{res['image_max_px'] or 'orig'}"
        imgs = prepare_vlm_images(frame_paths, res["image_max_px"], ffmpeg, vlm_dir)
    else:
        imgs = []
    images_b64 = [base64.b64encode(p.read_bytes()).decode("ascii") for p in imgs]
    res["frames_sent"] = len(images_b64)

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user, "images": images_b64},
    ]
    num_ctx = needed_ctx(len(images_b64), int(dcfg.get("num_ctx") or 16384), int(dcfg.get("tokens_per_image_min") or 1100))
    if num_ctx != int(dcfg.get("num_ctx") or 16384):
        res["warnings"].append(f"num_ctx raised to {num_ctx} to fit {len(images_b64)} images")
    res["num_ctx"] = num_ctx
    max_attempts = 1 + max(0, int(dcfg.get("json_retries", 1)))
    for attempt in range(1, max_attempts + 1):
        res["attempts"] = attempt
        if update_status:
            write_status(
                {
                    "step": "describe",
                    "message": (f"{model}: describing photo" if photo else f"{model}: describing {len(images_b64)} frames")
                               + f" (attempt {attempt}/{max_attempts})",
                }
            )
        payload = {
            "model": model,
            "stream": False,
            "format": response_schema(clip_types, int(dcfg.get("max_list_items") or 12)),
            "keep_alive": dcfg.get("keep_alive", "10m"),
            "options": {
                "temperature": float(dcfg.get("temperature", 0.1)),
                "num_ctx": num_ctx,
                "num_predict": int(dcfg.get("max_output_tokens") or 1024),
                "repeat_penalty": float(dcfg.get("repeat_penalty") or 1.1),
            },
            "messages": messages,
        }
        try:
            reply = chat_fn(url, payload, float(dcfg.get("timeout_s") or 900))
        except (urllib.error.URLError, ConnectionError, TimeoutError, RuntimeError, OSError) as e:
            res["reason"] = f"Ollama request failed ({url}): {e}"
            break
        content = ((reply or {}).get("message") or {}).get("content", "")
        res["raw_replies"].append(content[:4000])
        res["ollama_stats"].append(
            {
                k: reply.get(k)
                for k in ("total_duration", "load_duration", "prompt_eval_count", "prompt_eval_duration", "eval_count", "eval_duration")
                if isinstance(reply, dict) and k in reply
            }
        )
        try:
            obj = extract_json(content)
            clean, errs, warns = validate_description(obj, clip_types, pmax, smax)
        except ValueError as e:
            clean, errs, warns = None, [str(e)], []
        if clean is None and (reply or {}).get("done_reason") == "length":
            errs.append("reply was cut off at the output token limit — keep lists short (<= 12 items, no repeats)")
        res["warnings"].extend(warns)
        if clean is not None:
            res["status"], res["description"], res["reason"] = "ok", clean, None
            break
        res["errors"].extend(f"attempt {attempt}: {e}" for e in errs)
        res["reason"] = f"invalid JSON after {attempt} attempt(s): {'; '.join(errs)}"
        messages = messages + [
            {"role": "assistant", "content": content[:1500]},  # never feed a runaway reply back in full
            {"role": "user", "content": retry_message(errs, clip_types)},
        ]
    res["elapsed_s"] = round(time.monotonic() - t0, 2)
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description="Describe one clip with the local VLM (dry-run safe)")
    ap.add_argument("video", type=Path)
    ap.add_argument("--frames-dir", type=Path, default=None, help="Default: processing/<stem>_frames")
    ap.add_argument("--transcript", type=Path, default=None, help="transcribe.py <stem>.transcript.json")
    ap.add_argument("--model", default=None, help="Override model (default: tier prefer/fallback)")
    ap.add_argument("--print-prompt", action="store_true", help="Print prompt + schema; no Ollama call")
    ap.add_argument("--no-status", action="store_true")
    ap.add_argument("--out", type=Path, default=None, help="Write result JSON here")
    args = ap.parse_args()

    from detect_ram import current_tier
    from media import probe_media

    cfg = load_config()
    video = args.video.expanduser().resolve()
    proc = abs_path(cfg.get("processing_dir") or "processing")
    frames_dir = args.frames_dir or proc / f"{video.stem}_frames"
    frames = find_frames(frames_dir, video.stem)
    if not frames:
        print(f"No frames in {frames_dir} — run extract_frames.py first", file=sys.stderr)
        return 2
    duration = None
    if video.is_file():
        duration = probe_media(tool_path(cfg, "ffprobe_path", "ffprobe"), video)["duration_s"]
    transcript = json.loads(args.transcript.read_text()) if args.transcript else None

    import instructions as ins
    act = ins.resolve(cfg, use_saved_next=False)   # standing + glossary (next-run text is for pipeline runs)
    if args.print_prompt:
        ncfg = section(cfg, "naming")
        clip_types = types_for(cfg)
        meta = [(pct, duration * pct / 100 if duration else None) for pct, _ in frames]
        s, u = build_prompt(
            clip_types,
            video_name=video.name,
            folder_name=video.parent.name,
            duration_s=duration,
            frame_meta=meta,
            transcript_excerpt=(transcript or {}).get("excerpt"),
            transcript_note=None if transcript else "not run",
            project_max_len=int(ncfg.get("project_max_len") or 24),
            subject_max_len=int(ncfg.get("subject_max_len") or 32),
            guidance=ins.guidance_block(act),
        )
        print("=== SYSTEM ===\n" + s + "\n\n=== USER ===\n" + u)
        print("=== SCHEMA ===\n" + json.dumps(response_schema(clip_types), indent=2))
        return 0

    tier, tinfo, _ = current_tier()
    url = section(cfg, "describe").get("ollama_url") or DEFAULT_URL
    try:
        avail = available_models(url)
    except Exception as e:  # noqa: BLE001
        print(f"Ollama not reachable at {url}: {e}", file=sys.stderr)
        return 2
    model, cands = choose_model(cfg, tinfo, avail, args.model)
    if not model:
        print(f"No candidate model pulled (tried {cands}; have {avail})", file=sys.stderr)
        return 2
    res = describe(video, frames, cfg, model, duration_s=duration, transcript=transcript, update_status=not args.no_status,
                   instructions=act)
    out = json.dumps(res, indent=2, ensure_ascii=False)
    if args.out:
        args.out.write_text(out + "\n", encoding="utf-8")
    print(out)
    return 0 if res["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
