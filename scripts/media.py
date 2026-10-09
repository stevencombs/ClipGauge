#!/usr/bin/env python3
"""ffprobe helpers: duration, audio presence, creation date for naming."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import load_config, tool_path  # noqa: E402


def probe_media(ffprobe: str, video: Path) -> dict:
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration:format_tags=creation_time:stream=codec_type,width,height:stream_tags=creation_time",
        "-of",
        "json",
        str(video),
    ]
    data = json.loads(subprocess.check_output(cmd, text=True, stderr=subprocess.PIPE))  # stderr kept for the reason
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    vstream = next((s for s in streams if s.get("codec_type") == "video"), {})
    creation = (fmt.get("tags") or {}).get("creation_time") or (vstream.get("tags") or {}).get(
        "creation_time"
    )
    try:
        duration = float(fmt.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    return {
        "duration_s": round(duration, 3),
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
        "has_video": bool(vstream),
        "width": vstream.get("width"),
        "height": vstream.get("height"),
        "creation_time": creation,
    }


def clip_date(info: dict, video: Path) -> tuple[str, str]:
    """Return (YYYYMMDD, source). Order: container creation_time -> birthtime -> mtime."""
    ct = info.get("creation_time")
    if ct:
        try:
            dt = datetime.fromisoformat(str(ct).replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                dt = dt.astimezone()  # local (machine) time zone
            if dt.year > 1990:
                return dt.strftime("%Y%m%d"), "creation_time"
        except ValueError:
            pass
    st = video.stat()
    birth = getattr(st, "st_birthtime", None)
    if birth:
        return datetime.fromtimestamp(birth).strftime("%Y%m%d"), "file_birthtime"
    return datetime.fromtimestamp(st.st_mtime).strftime("%Y%m%d"), "file_mtime"


def main() -> int:
    ap = argparse.ArgumentParser(description="Probe a video (duration, audio, date)")
    ap.add_argument("video", type=Path)
    args = ap.parse_args()
    cfg = load_config()
    video = args.video.expanduser().resolve()
    info = probe_media(tool_path(cfg, "ffprobe_path", "ffprobe"), video)
    info["date"], info["date_source"] = clip_date(info, video)
    print(json.dumps(info, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
