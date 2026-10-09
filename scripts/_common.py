"""Shared paths/config helpers for AI Video Renamer."""
from __future__ import annotations

import json
import os
from pathlib import Path

# Project root = the folder that holds scripts/ (portable: Lexar, another drive, or a folder ClipGauge created).
# AI_VIDEO_RENAMER_ROOT overrides (tests / throwaway copies).
ENGINE_VERSION = "0.6.1"   # written into logs/status.json so stale error cards can be dated (ClipGauge)
ROOT = Path(os.environ.get("AI_VIDEO_RENAMER_ROOT") or Path(__file__).resolve().parent.parent)
CONFIG_PATH = ROOT / "config" / "config.json"
RAM_TIERS_PATH = ROOT / "config" / "ram-tiers.json"
DEFAULT_STATUS = ROOT / "logs" / "status.json"


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    # Resolve relative dirs against ROOT
    for key in (
        "models_dir",
        "inbox_dir",
        "processing_dir",
        "done_dir",
        "needs_review_dir",
        "logs_dir",
        "status_path",
    ):
        if key in cfg and not os.path.isabs(str(cfg[key])):
            cfg[f"_{key}_abs"] = str(ROOT / cfg[key])
    cfg["_root"] = str(ROOT)
    return cfg


def load_ram_tiers() -> dict:
    with open(RAM_TIERS_PATH, encoding="utf-8") as f:
        return json.load(f)


def status_path(cfg: dict | None = None) -> Path:
    cfg = cfg or load_config()
    p = cfg.get("status_path", "logs/status.json")
    return Path(p) if os.path.isabs(p) else ROOT / p


# ---------------------------------------------------------------------------
# Helpers added for transcribe / describe / sidecar steps
# ---------------------------------------------------------------------------

def abs_path(p: str | os.PathLike) -> Path:
    """Resolve a config path relative to ROOT (absolute paths pass through)."""
    pp = Path(p)
    return pp if pp.is_absolute() else ROOT / pp


def section(cfg: dict, name: str) -> dict:
    """Return a config sub-dict (empty dict if missing/null)."""
    val = cfg.get(name)
    return dict(val) if isinstance(val, dict) else {}


def tool_path(cfg: dict, key: str, name: str) -> str:
    """ffmpeg/ffprobe resolution: config -> PATH -> Homebrew default."""
    import shutil

    return cfg.get(key) or shutil.which(name) or f"/opt/homebrew/bin/{name}"


def slugify(text: object, max_len: int = 32) -> str:
    """Lowercase ASCII kebab-case slug: 'Xteink X4 Reader!' -> 'xteink-x4-reader'."""
    import re
    import unicodedata

    s = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii")
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    if max_len and len(s) > max_len:
        s = s[:max_len].rstrip("-")
    return s


def atomic_write_text(path: Path, text: str) -> Path:
    """Write via temp file + replace (ExFAT-friendly; ._ AppleDouble files are expected)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    tmp.replace(path)
    return path
