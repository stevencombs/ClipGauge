#!/usr/bin/env python3
"""
Still-photo helpers for the pipeline (stdlib only; macOS `sips` for conversion).

  * PHOTO_EXTS: .jpg .jpeg .png .heic .heif .dng .webp .tif .tiff (compared case-insensitively; ._ files skipped)
  * read_exif(path)    -> {"DateTimeOriginal", "OffsetTimeOriginal", "Orientation", "Make", "Model", ...}
                          minimal EXIF/TIFF reader: JPEG APP1, TIFF/DNG, PNG eXIf, WebP EXIF, HEIC/HEIF Exif item
                          (found by its "Exif\\0\\0" + TIFF header signature)
  * photo_info(path)   -> probe_media()-style dict (duration 0, no audio) + creation_time from EXIF DateTimeOriginal
  * photo_date(info)   -> (YYYYMMDD, source): EXIF DateTimeOriginal (camera wall clock), else file mtime
  * prepare_photo(...) -> one JPEG, long side <= describe.max_image_px, upright, for the VLM (temp copy in
                          processing/<stem>_frames/). HEIC/HEIF/DNG are converted with sips (built into macOS);
                          the original file is never modified.

  python3 scripts/photos.py PHOTO [PHOTO ...]   # print EXIF date / info (read-only)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import struct
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import load_config, section, tool_path  # noqa: E402

PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".dng", ".webp", ".tif", ".tiff"}
NEEDS_SIPS = {".heic", ".heif", ".dng"}  # ffmpeg can't be relied on to decode these
MAX_SCAN_BYTES = 64 * 1024 * 1024
SIPS = "/usr/bin/sips"

_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}
IFD0_TAGS = {0x010F: "Make", 0x0110: "Model", 0x0112: "Orientation", 0x0132: "DateTime",
             0x0100: "ImageWidth", 0x0101: "ImageLength"}
EXIF_TAGS = {0x9003: "DateTimeOriginal", 0x9004: "DateTimeDigitized", 0x9011: "OffsetTimeOriginal",
             0x9291: "SubSecTimeOriginal", 0xA002: "PixelXDimension", 0xA003: "PixelYDimension"}
ROTATE = {3: 180, 6: 90, 8: 270}  # EXIF orientation -> clockwise degrees to make the image upright


def is_photo(p: str | os.PathLike) -> bool:
    p = Path(p)
    return p.suffix.lower() in PHOTO_EXTS and not p.name.startswith(".")


# --------------------------------------------------------------- EXIF ----

def _parse_tiff(buf: bytes) -> dict:
    """Tags from a TIFF block (buf starts at the II*\\0 / MM\\0* header). Never raises."""
    out: dict = {}
    try:
        if buf[:2] == b"II":
            e = "<"
        elif buf[:2] == b"MM":
            e = ">"
        else:
            return out
        if struct.unpack(e + "H", buf[2:4])[0] != 42:
            return out

        def ifd(off: int, names: dict) -> dict:
            got: dict = {}
            if off <= 0 or off + 2 > len(buf):
                return got
            n = struct.unpack(e + "H", buf[off:off + 2])[0]
            for i in range(min(n, 512)):
                p = off + 2 + 12 * i
                if p + 12 > len(buf):
                    break
                tag, typ, cnt = struct.unpack(e + "HHI", buf[p:p + 8])
                if tag not in names and tag != 0x8769:
                    continue
                size = _TYPE_SIZE.get(typ, 1) * cnt
                data = buf[p + 8:p + 12] if size <= 4 else buf[struct.unpack(e + "I", buf[p + 8:p + 12])[0]:][:size]
                if typ == 2:
                    val = data[:cnt].split(b"\0", 1)[0].decode("ascii", "replace").strip()
                elif typ == 3:
                    val = struct.unpack(e + "H", data[:2])[0] if len(data) >= 2 else None
                elif typ in (4, 9):
                    val = struct.unpack(e + ("I" if typ == 4 else "i"), data[:4])[0] if len(data) >= 4 else None
                elif typ == 7:
                    val = data[:cnt].decode("ascii", "replace").strip("\0 ")
                else:
                    continue
                got[names.get(tag, "_exif_ifd")] = val
            return got

        ifd0_off = struct.unpack(e + "I", buf[4:8])[0]
        out.update(ifd(ifd0_off, IFD0_TAGS))
        exif_off = out.pop("_exif_ifd", None)
        if isinstance(exif_off, int):
            out.update(ifd(exif_off, EXIF_TAGS))
    except (struct.error, IndexError, ValueError):
        pass
    return out


def _tiff_from_jpeg(data: bytes) -> bytes | None:
    if data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker in (0xD9, 0xDA):  # EOI / start of scan: no more metadata
            return None
        seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker == 0xE1 and data[i + 4:i + 10] == b"Exif\0\0":
            return data[i + 10:i + 2 + seg_len]
        i += 2 + seg_len
    return None


def _tiff_from_png(data: bytes) -> bytes | None:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    i = 8
    while i + 8 <= len(data):
        n, typ = struct.unpack(">I4s", data[i:i + 8])
        if typ == b"eXIf":
            return data[i + 8:i + 8 + n]
        if typ == b"IEND":
            return None
        i += 12 + n
    return None


def _tiff_from_webp(data: bytes) -> bytes | None:
    if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    i = 12
    while i + 8 <= len(data):
        typ, n = struct.unpack("<4sI", data[i:i + 8])
        if typ == b"EXIF":
            blk = data[i + 8:i + 8 + n]
            return blk[6:] if blk[:6] == b"Exif\0\0" else blk
        i += 8 + n + (n & 1)
    return None


def _tiff_by_signature(data: bytes) -> bytes | None:
    """HEIC/HEIF (and anything else): the Exif item is 'Exif\\0\\0' followed by a TIFF header."""
    for m in re.finditer(rb"Exif\x00\x00(?=II\*\x00|MM\x00\*)", data):
        blk = data[m.end():]
        if _parse_tiff(blk):
            return blk
    return None


def read_exif(path: str | os.PathLike) -> dict:
    """Selected EXIF tags ({} if none / unreadable). Read-only."""
    p = Path(path)
    try:
        with open(p, "rb") as f:
            data = f.read(MAX_SCAN_BYTES)
    except OSError:
        return {}
    blk = None
    if data[:4] in (b"II*\x00", b"MM\x00*"):  # TIFF / DNG
        blk = data
    for fn in (_tiff_from_jpeg, _tiff_from_png, _tiff_from_webp):
        if blk is None:
            blk = fn(data)
    tags = _parse_tiff(blk) if blk else {}
    if not tags.get("DateTimeOriginal"):
        alt = _tiff_by_signature(data)
        if alt:
            tags = {**_parse_tiff(alt), **{k: v for k, v in tags.items() if v not in (None, "")}}
    return tags


def exif_datetime(tags: dict) -> datetime | None:
    """EXIF DateTimeOriginal as an aware datetime: with OffsetTimeOriginal if present, else this machine's
    local zone (camera clocks are set to local time). None if absent/invalid."""
    raw = str(tags.get("DateTimeOriginal") or "").strip()
    m = re.match(r"^(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})", raw)
    if not m:
        return None
    try:
        dt = datetime(*map(int, m.groups()))
    except ValueError:
        return None
    if dt.year < 1991:
        return None
    off = re.match(r"^([+-])(\d{2}):?(\d{2})$", str(tags.get("OffsetTimeOriginal") or "").strip())
    if off:
        sign = 1 if off.group(1) == "+" else -1
        return dt.replace(tzinfo=timezone(sign * timedelta(hours=int(off.group(2)), minutes=int(off.group(3)))))
    return dt.astimezone()  # naive -> local zone of this Mac


# --------------------------------------------------------------- info ----

def sips_available() -> bool:
    return Path(SIPS).is_file() or bool(shutil.which("sips"))


def _sips() -> str:
    return SIPS if Path(SIPS).is_file() else (shutil.which("sips") or SIPS)


def sips_size(path: Path) -> tuple[int | None, int | None]:
    if not sips_available():
        return None, None
    try:
        out = subprocess.run([_sips(), "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
                             capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return None, None
    w = re.search(r"pixelWidth:\s*(\d+)", out)
    h = re.search(r"pixelHeight:\s*(\d+)", out)
    return (int(w.group(1)) if w else None), (int(h.group(1)) if h else None)


def photo_info(path: str | os.PathLike, exif: dict | None = None) -> dict:
    """probe_media()-shaped info for a still: duration 0, no audio, EXIF date as creation_time."""
    p = Path(path)
    tags = read_exif(p) if exif is None else exif
    dt = exif_datetime(tags)
    w = tags.get("PixelXDimension") or tags.get("ImageWidth")
    h = tags.get("PixelYDimension") or tags.get("ImageLength")
    if not (isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0):
        w, h = sips_size(p)
    if tags.get("Orientation") in (5, 6, 7, 8) and w and h:
        w, h = h, w  # shown rotated
    return {
        "media_kind": "photo",
        "duration_s": 0.0,
        "has_audio": False,
        "has_video": False,
        "width": w,
        "height": h,
        "creation_time": dt.isoformat(timespec="seconds") if dt else None,
        "exif": {k: tags[k] for k in ("DateTimeOriginal", "OffsetTimeOriginal", "Orientation", "Make", "Model")
                 if tags.get(k) not in (None, "")},
    }


def photo_date(info: dict, path: str | os.PathLike) -> tuple[str, str]:
    """(YYYYMMDD, source): the EXIF DateTimeOriginal day as shown on the camera clock, else the file mtime."""
    raw = str((info.get("exif") or {}).get("DateTimeOriginal") or "")
    m = re.match(r"^(\d{4})[:\-](\d{2})[:\-](\d{2})", raw)
    if m and info.get("creation_time"):
        return "".join(m.groups()), "exif_datetime_original"
    return datetime.fromtimestamp(Path(path).stat().st_mtime).strftime("%Y%m%d"), "file_mtime"


# ------------------------------------------------------------ prepare ----

def prepare_photo(path: str | os.PathLike, out_dir: Path, max_px: int, cfg: dict | None = None,
                  orientation: int | None = None) -> tuple[Path, str]:
    """Write <out_dir>/<stem>_photo.jpg (long side <= max_px, rotated upright) for the VLM. Returns (jpeg, method).
    sips (macOS) first; ffmpeg fallback for formats it can decode. The original is only read. Raises RuntimeError."""
    src = Path(path)
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / f"{src.stem}_photo.jpg"
    tmp = out_dir / f".{src.stem}_photo.tmp.jpg"
    errors = []
    if sips_available():
        w, h = sips_size(src)
        cmd = [_sips(), "-s", "format", "jpeg", "-s", "formatOptions", "85"]
        if max_px and (not (w and h) or max(w, h) > max_px):
            cmd += ["-Z", str(int(max_px))]
        if orientation in ROTATE:
            cmd += ["-r", str(ROTATE[orientation])]
        try:
            r = subprocess.run(cmd + [str(src), "--out", str(tmp)], capture_output=True, text=True, timeout=180)
            if r.returncode == 0 and tmp.is_file() and tmp.stat().st_size > 0:
                os.replace(tmp, dst)
                return dst, "sips"
            errors.append(f"sips exit {r.returncode}: {(r.stderr or r.stdout).strip()[:200]}")
        except (OSError, subprocess.SubprocessError) as e:
            errors.append(f"sips: {e}")
    elif src.suffix.lower() in NEEDS_SIPS:
        raise RuntimeError(f"{src.suffix} needs sips (macOS) to convert — not found")
    if src.suffix.lower() not in NEEDS_SIPS:
        ffmpeg = tool_path(cfg or {}, "ffmpeg_path", "ffmpeg")
        vf = [f"scale='if(gt(iw,ih),min({max_px},iw),-2)':'if(gt(iw,ih),-2,min({max_px},ih))'"] if max_px else []
        vf += {90: ["transpose=1"], 180: ["hflip,vflip"], 270: ["transpose=2"]}.get(ROTATE.get(orientation or 1, 0), [])
        try:
            r = subprocess.run([ffmpeg, "-y", "-v", "error", "-i", str(src)] + (["-vf", ",".join(vf)] if vf else [])
                               + ["-frames:v", "1", "-q:v", "3", str(tmp)], capture_output=True, text=True, timeout=180)
            if r.returncode == 0 and tmp.is_file() and tmp.stat().st_size > 0:
                os.replace(tmp, dst)
                return dst, "ffmpeg"
            errors.append(f"ffmpeg exit {r.returncode}: {r.stderr.strip()[:200]}")
        except (OSError, subprocess.SubprocessError) as e:
            errors.append(f"ffmpeg: {e}")
    tmp.unlink(missing_ok=True)
    raise RuntimeError("could not convert photo for the model: " + "; ".join(errors))


def main() -> int:
    ap = argparse.ArgumentParser(description="Show EXIF date / info for photos (read-only)")
    ap.add_argument("photos", nargs="+", type=Path)
    args = ap.parse_args()
    load_config()
    for p in args.photos:
        info = photo_info(p)
        info["date"], info["date_source"] = photo_date(info, p)
        print(json.dumps({"file": str(p), **info}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
