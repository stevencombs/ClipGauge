#!/usr/bin/env python3
"""
Local speech-to-text with whisper.cpp (`whisper-cli`, Homebrew formula `whisper-cpp`).

  ffmpeg -> processing/<stem>_audio/<stem>.wav (16 kHz mono PCM)
  whisper-cli -oj -> parse -> <stem>.transcript.json + <stem>.transcript.txt (timestamps)

Optional step: skips gracefully (status "skipped") when whisper is disabled,
whisper-cli / the ggml model is not installed yet, or the clip has no audio.
Install later with scripts/setup-whisper.sh.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import abs_path, atomic_write_text, load_config, section, tool_path  # noqa: E402
from detect_ram import current_tier  # noqa: E402
from media import probe_media  # noqa: E402
from status import write_status  # noqa: E402

# Whisper non-speech markers: [BLANK_AUDIO], [Music], (upbeat music), *applause* ...
NOISE_RE = re.compile(r"^\s*(?:[\[\(\*♪][^\]\)\*]*[\]\)\*♪]\s*)+$")


def whisper_cli_path(wcfg: dict) -> str | None:
    for cand in (
        wcfg.get("cli_path"),
        shutil.which("whisper-cli"),
        "/opt/homebrew/bin/whisper-cli",
        "/usr/local/bin/whisper-cli",
    ):
        if cand and Path(cand).is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def whisper_model_name(cfg: dict, tier: str | None = None, tier_info: dict | None = None) -> str:
    """env AI_VIDEO_RENAMER_WHISPER_MODEL > config whisper.model[tier] > ram-tiers whisper_model > base.en"""
    env = os.environ.get("AI_VIDEO_RENAMER_WHISPER_MODEL")
    if env:
        return env.strip()
    if tier is None:
        tier, tier_info, _ = current_tier()
    model = section(cfg, "whisper").get("model")
    if isinstance(model, dict) and model.get(tier):
        return str(model[tier])
    if isinstance(model, str) and model:
        return model
    if tier_info and tier_info.get("whisper_model"):
        return str(tier_info["whisper_model"])
    return "base.en"


def whisper_model_file(cfg: dict, name: str) -> Path:
    d = abs_path(section(cfg, "whisper").get("models_dir") or "models/whisper")
    fname = name if name.endswith(".bin") else f"ggml-{name}.bin"
    return d / fname


def fmt_ts(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def parse_whisper_json(data: dict) -> list[dict]:
    """whisper-cli -oj output -> [{start, end, text}] with non-speech markers dropped."""
    segs: list[dict] = []
    for item in data.get("transcription") or []:
        text = " ".join(str(item.get("text") or "").split())
        if not text or NOISE_RE.match(text):
            continue
        off = item.get("offsets") or {}
        segs.append(
            {
                "start": round(float(off.get("from", 0)) / 1000.0, 3),
                "end": round(float(off.get("to", 0)) / 1000.0, 3),
                "text": text,
            }
        )
    return segs


def make_excerpt(segments: list[dict], max_chars: int = 1200) -> str:
    text = " ".join(s["text"] for s in segments).strip()
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars].rsplit(" ", 1)[0]
    return cut + " …"


def transcript_txt(segments: list[dict]) -> str:
    return "".join(f"[{fmt_ts(s['start'])} --> {fmt_ts(s['end'])}] {s['text']}\n" for s in segments)


def transcribe(
    video: Path,
    cfg: dict,
    out_dir: Path | None = None,
    info: dict | None = None,
    update_status: bool = True,
    prompt: str | None = None,
) -> dict:
    """Returns a result dict; status in {ok, no_speech, skipped, error}. Never raises for expected cases.
    prompt: whisper-cli --prompt (initial prompt). None = the glossary from cfg["_instructions"] (set by run_pipeline);
    "" = none. Disable the glossary for Whisper with whisper.use_glossary=false."""
    wcfg = section(cfg, "whisper")
    t0 = time.monotonic()
    res: dict = {
        "status": "skipped",
        "reason": None,
        "engine": "whisper.cpp",
        "model": None,
        "language": wcfg.get("language", "en"),
        "text": "",
        "excerpt": "",
        "segments": [],
        "transcript_json": None,
        "transcript_txt": None,
        "elapsed_s": 0.0,
    }

    def finish(status: str, reason: str | None) -> dict:
        res["status"], res["reason"] = status, reason
        res["elapsed_s"] = round(time.monotonic() - t0, 2)
        label = {"skipped": "SKIP", "error": "ERROR", "no_speech": "NO SPEECH", "ok": "OK"}[status]
        print(f"Transcribe [{label}]: {reason or ''}".rstrip())
        return res

    if not wcfg.get("enabled", True):
        return finish("skipped", "whisper.enabled is false in config.json")

    cli = whisper_cli_path(wcfg)
    if not cli:
        return finish(
            "skipped",
            "whisper-cli not installed yet — run scripts/setup-whisper.sh (brew install whisper-cpp + ggml model)",
        )
    name = whisper_model_name(cfg)
    res["model"] = name
    mfile = whisper_model_file(cfg, name)
    if not mfile.is_file():
        return finish("skipped", f"whisper model missing: {mfile} — run scripts/setup-whisper.sh")

    ffmpeg = tool_path(cfg, "ffmpeg_path", "ffmpeg")
    ffprobe = tool_path(cfg, "ffprobe_path", "ffprobe")
    try:
        info = info or probe_media(ffprobe, video)
    except Exception as e:  # noqa: BLE001
        return finish("error", f"ffprobe failed: {e}")
    if not info.get("has_audio"):
        return finish("skipped", "clip has no audio stream")

    proc_dir = abs_path(cfg.get("processing_dir") or "processing")
    out_dir = out_dir or (proc_dir / f"{video.stem}_audio")
    out_dir.mkdir(parents=True, exist_ok=True)
    wav = out_dir / f"{video.stem}.wav"
    of = out_dir / video.stem  # whisper-cli appends .json

    if update_status:
        write_status({"step": "transcribe_extract_audio", "message": f"Extracting 16 kHz mono audio from {video.name}"})
    try:
        subprocess.run(
            [ffmpeg, "-y", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        return finish("error", f"ffmpeg audio extract failed: {(e.stderr or '').strip()[:300]}")

    lang = str(wcfg.get("language") or "en")
    if name.endswith(".en"):
        lang = "en"  # English-only models
    res["language"] = lang
    cmd = [cli, "-m", str(mfile), "-f", str(wav), "-l", lang, "-t", str(int(wcfg.get("threads") or 4)), "-oj", "-of", str(of), "-np"]
    if prompt is None and wcfg.get("use_glossary", True):
        import instructions as ins
        prompt = ins.whisper_prompt(cfg.get("_instructions"))
    if prompt:
        cmd += ["--prompt", prompt]
        res["prompt"] = prompt
    if update_status:
        write_status({"step": "transcribe_whisper", "message": f"whisper.cpp ({name}) on {info.get('duration_s', 0):.0f}s of audio"})
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=float(wcfg.get("timeout_s") or 1800))
    except subprocess.TimeoutExpired:
        return finish("error", "whisper-cli timed out")
    finally:
        if not wcfg.get("keep_wav", False):
            wav.unlink(missing_ok=True)
    jpath = Path(str(of) + ".json")
    if p.returncode != 0 or not jpath.is_file():
        tail = (p.stderr or p.stdout or "").strip().splitlines()[-3:]
        return finish("error", f"whisper-cli exit {p.returncode}: {' | '.join(tail)[:400]}")

    data = json.loads(jpath.read_text(encoding="utf-8", errors="replace"))
    segs = parse_whisper_json(data)
    res["segments"] = segs
    res["text"] = " ".join(s["text"] for s in segs)
    res["excerpt"] = make_excerpt(segs, int(wcfg.get("excerpt_chars") or 1200))
    res["raw_json"] = str(jpath)

    tj = out_dir / f"{video.stem}.transcript.json"
    tt = out_dir / f"{video.stem}.transcript.txt"
    atomic_write_text(tj, json.dumps({k: v for k, v in res.items() if k != "elapsed_s"}, indent=2, ensure_ascii=False) + "\n")
    atomic_write_text(tt, transcript_txt(segs))
    res["transcript_json"], res["transcript_txt"] = str(tj), str(tt)
    if not segs:
        return finish("no_speech", "audio present but no speech detected")
    return finish("ok", f"{len(segs)} segments, {len(res['text'])} chars -> {tt}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Transcribe a clip with whisper.cpp (optional step)")
    ap.add_argument("video", type=Path, nargs="?", help="Input video path")
    ap.add_argument("--out-dir", type=Path, default=None, help="Default: processing/<stem>_audio")
    ap.add_argument("--no-status", action="store_true")
    ap.add_argument("--print-model", action="store_true", help="Print tier's whisper model name and exit")
    ap.add_argument("--print-model-path", action="store_true", help="Print tier's ggml model path and exit")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--prompt", help="whisper-cli initial prompt (default: the saved glossary from Instructions)")
    g.add_argument("--no-glossary", action="store_true", help="No initial prompt")
    args = ap.parse_args()

    cfg = load_config()
    if args.print_model or args.print_model_path:
        name = whisper_model_name(cfg)
        print(whisper_model_file(cfg, name) if args.print_model_path else name)
        return 0
    if not args.video:
        ap.error("video is required")
    video = args.video.expanduser().resolve()
    if not video.is_file():
        print(f"Not a file: {video}", file=sys.stderr)
        return 2
    if args.no_glossary:
        prompt = ""
    elif args.prompt is not None:
        prompt = args.prompt
    else:
        import instructions as ins
        prompt = ins.whisper_prompt(ins.resolve(cfg, use_saved_next=False)) or ""
    res = transcribe(video, cfg, out_dir=args.out_dir, update_status=not args.no_status, prompt=prompt)
    summary = {k: v for k, v in res.items() if k not in ("segments", "text")}
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 1 if res["status"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
