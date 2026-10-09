#!/usr/bin/env python3
"""Extract frames at configured percents using ffprobe + ffmpeg."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import load_config  # noqa: E402
from status import write_status  # noqa: E402


def ffprobe_duration(ffprobe: str, video: Path) -> float:
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(video),
    ]
    out = subprocess.check_output(cmd, text=True)
    data = json.loads(out)
    dur = float(data["format"]["duration"])
    if dur <= 0:
        raise ValueError(f"Non-positive duration for {video}")
    return dur


def extract_frames(
    video: Path,
    out_dir: Path,
    percents: list[int],
    ffmpeg: str,
    ffprobe: str,
    update_status: bool = True,
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    duration = ffprobe_duration(ffprobe, video)
    paths: list[Path] = []
    total = len(percents)
    stem = video.stem

    for i, pct in enumerate(percents, start=1):
        t = max(0.0, min(duration * (pct / 100.0), max(duration - 0.05, 0)))
        out = out_dir / f"{stem}_f{pct:02d}.jpg"
        cmd = [
            ffmpeg,
            "-y",
            "-ss",
            f"{t:.3f}",
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(out),
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        paths.append(out)
        if update_status:
            write_status(
                {
                    "state": "extracting_frames",
                    "current_file": str(video),
                    "step": f"extract_frame_{pct}pct",
                    "frames_done": i,
                    "frames_total": total,
                    "eta": None,
                    "message": f"Extracted {i}/{total} frames",
                }
            )
    return paths


def main() -> int:
    ap = argparse.ArgumentParser(description="Extract percent-based frames from a video")
    ap.add_argument("video", type=Path, help="Input video path")
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Output directory (default: processing/<stem>_frames)",
    )
    ap.add_argument("--no-status", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    video = args.video.expanduser().resolve()
    if not video.is_file():
        print(f"Not a file: {video}", file=sys.stderr)
        return 2

    ffmpeg = cfg.get("ffmpeg_path") or shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
    ffprobe = cfg.get("ffprobe_path") or shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"
    if not Path(ffmpeg).exists() or not Path(ffprobe).exists():
        print("ffmpeg/ffprobe not found", file=sys.stderr)
        return 2

    percents = list(cfg.get("frame_percents") or [10, 20, 30, 40, 50, 60, 70, 80, 90])
    proc = Path(cfg.get("_processing_dir_abs") or (Path(cfg["_root"]) / "processing"))
    out_dir = args.out_dir or (proc / f"{video.stem}_frames")

    print(f"Video: {video}")
    print(f"Out:   {out_dir}")
    print(f"Percents: {percents}")
    paths = extract_frames(
        video, out_dir, percents, ffmpeg, ffprobe, update_status=not args.no_status
    )
    print(f"Wrote {len(paths)} frames:")
    for p in paths:
        print(f"  {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
