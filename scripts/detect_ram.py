#!/usr/bin/env python3
"""Print system RAM (GB) and recommended VLM tier (air | pro)."""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import load_ram_tiers  # noqa: E402


def physical_ram_bytes() -> int:
    system = platform.system()
    if system == "Darwin":
        out = subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True).strip()
        return int(out)
    # Linux fallback
    pages = os.sysconf("SC_PHYS_PAGES")
    page = os.sysconf("SC_PAGE_SIZE")
    return int(pages * page)


def recommend_tier(gb: float, tiers_cfg: dict) -> str:
    override = os.environ.get(
        tiers_cfg.get("detection", {}).get("override_env", "AI_VIDEO_RENAMER_TIER")
    )
    if override:
        return override.strip().lower()
    cfg_override = tiers_cfg.get("override_tier")
    if cfg_override:
        return str(cfg_override).strip().lower()
    det = tiers_cfg.get("detection", {})
    if gb >= float(det.get("pro_if_gb_gte", 48)):
        return "pro"
    if gb < float(det.get("air_if_gb_lt", 32)):
        return "air"
    # Mid band — prefer air for safety
    return "air"


def current_tier() -> tuple[str, dict, float]:
    """Return (tier, tier_info, ram_gib) using the same rules as the CLI."""
    tiers_cfg = load_ram_tiers()
    gb = physical_ram_bytes() / (1024**3)
    tier = recommend_tier(gb, tiers_cfg)
    return tier, tiers_cfg["tiers"].get(tier, {}), gb


def main() -> int:
    ap = argparse.ArgumentParser(description="Detect RAM and recommend model tier")
    ap.add_argument("--json", action="store_true", help="Machine-readable output")
    args = ap.parse_args()

    tiers_cfg = load_ram_tiers()
    raw = physical_ram_bytes()
    gb = raw / (1024**3)
    gb_si = raw / 1e9
    tier = recommend_tier(gb, tiers_cfg)
    info = tiers_cfg["tiers"].get(tier, {})
    payload = {
        "ram_bytes": raw,
        "ram_gb_binary": round(gb, 2),
        "ram_gb_si": round(gb_si, 2),
        "tier": tier,
        "label": info.get("label"),
        "prefer_model": info.get("prefer"),
        "fallback_model": info.get("fallback"),
        "alternates": info.get("alternates"),
        "whisper_model": info.get("whisper_model"),
        "override_env": os.environ.get("AI_VIDEO_RENAMER_TIER"),
        "override_tier_config": tiers_cfg.get("override_tier"),
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"RAM: {payload['ram_gb_binary']:.1f} GiB ({payload['ram_gb_si']:.1f} GB SI)")
        print(f"Tier: {tier} — {info.get('label', '')}")
        print(f"Prefer: {info.get('prefer')}")
        if info.get("fallback"):
            print(f"Fallback: {info.get('fallback')}")
        if info.get("alternates"):
            print(f"Alternates: {', '.join(info['alternates'])}")
        if info.get("whisper_model"):
            print(f"Whisper: {info.get('whisper_model')} (config.json whisper.model overrides per tier)")
        if payload["override_env"] or payload["override_tier_config"]:
            print("(override active)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
