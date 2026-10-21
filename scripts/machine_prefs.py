#!/usr/bin/env python3
"""Per-Mac model choice (ClipGauge v0.8.3): Settings › Models writes
~/Library/Application Support/ClipGauge/models.json on THIS Mac (never on the project drive, never synced, because
Macs differ in memory):  {"vision_model": "qwen2.5vl:32b" | null, "whisper_model": "large-v3-turbo" | null}
null / missing = "Automatic (RAM tier)". AI_VIDEO_RENAMER_MACHINE_PREFS overrides the file path (tests).

  python3 scripts/machine_prefs.py            # JSON: the choice + what the engine will actually use on this Mac
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def prefs_path() -> Path:
    env = os.environ.get("AI_VIDEO_RENAMER_MACHINE_PREFS")
    if env:
        return Path(env).expanduser()
    return Path.home() / "Library" / "Application Support" / "ClipGauge" / "models.json"


def load() -> dict:
    try:
        d = json.loads(prefs_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(d, dict):
        return {}
    out = {}
    for k in ("vision_model", "whisper_model"):
        v = d.get(k)
        if isinstance(v, str) and v.strip() and v.strip().lower() not in ("auto", "automatic"):
            out[k] = v.strip()
    return out


def vision_choice() -> str | None:
    return load().get("vision_model")


def whisper_choice() -> str | None:
    return load().get("whisper_model")


def whisper_models(cfg: dict) -> list[str]:
    """Model names found in models/whisper (ggml-<name>.bin)."""
    from _common import abs_path, section
    d = abs_path(section(cfg, "whisper").get("models_dir") or "models/whisper")
    try:
        names = [p.name[5:-4] for p in d.iterdir() if p.name.startswith("ggml-") and p.name.endswith(".bin")
                 and p.is_file() and not p.name.startswith(".")]
    except OSError:
        return []
    return sorted(set(names))


def effective(cfg: dict, available: list[str] | None = None) -> dict:
    """What the engine will use on this Mac: {vision_model, vision_source, whisper_model, whisper_source, choice, tier}."""
    from detect_ram import current_tier
    import transcribe as tr
    import describe_clip as dc
    tier, info, gb = current_tier()
    ch = load()
    if available is None:
        try:
            url = (cfg.get("describe") or {}).get("ollama_url") or "http://127.0.0.1:11434"
            available = dc.available_models(url)
        except Exception:  # noqa: BLE001 — Ollama not running: report the choice without checking it
            available = None
    if available is not None:
        vm, _ = dc.choose_model(cfg, info, available, None)
        vsrc = dc.last_choice_source
    else:  # Ollama not running: report the choice without checking it is installed
        vm = ch.get("vision_model") or (cfg.get("describe") or {}).get("model") or info.get("prefer")
        vsrc = "this Mac's choice" if ch.get("vision_model") else "RAM tier"
    wm, wsrc = tr.whisper_model_choice(cfg, tier, info)
    wfile = tr.whisper_model_file(cfg, wm)
    return {"tier": tier, "ram_gb": round(gb, 1), "choice": ch, "prefs_file": str(prefs_path()),
            "vision_model": vm, "vision_source": vsrc, "installed_models": available,
            "whisper_model": wm, "whisper_source": wsrc, "whisper_installed": wfile.is_file(),
            "whisper_models": whisper_models(cfg)}


if __name__ == "__main__":
    from _common import load_config
    print(json.dumps(effective(load_config()), indent=2))
