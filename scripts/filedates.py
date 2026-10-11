"""Keep every clip's original dates (modified + created) across copy, move, rename and undo.

ExFAT (the Lexar) cannot store dates before 1980-01-01: a source with no date (epoch 0, e.g. some phone-to-Mac
transfers) would land as "Jan 1, 1980 12:00 AM". So:
  * snapshot() the source before a copy / move / rename, keep() on the result afterwards: both dates are put back
    and checked; a date that still differs is reported (never silently).
  * A missing date (<= 1980-01-02) is never copied as-is: the capture time is used instead, from the camera /
    Pixel / screenshot name, the video's creation_time (ffprobe) or the photo's EXIF DateTimeOriginal.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import struct
import subprocess
from pathlib import Path

DOS_EPOCH = dt.datetime(1980, 1, 2).timestamp()   # anything at or before this is "no date"
SETFILE = "/usr/bin/SetFile"
TOLERANCE_S = 2.0                                  # ExFAT keeps modified times in 2 s steps
UTC = dt.timezone.utc


def is_missing(ts: float | None) -> bool:
    return ts is None or ts <= DOS_EPOCH


def snapshot(p: str | os.PathLike) -> dict:
    st = os.stat(p)
    return {"mtime": st.st_mtime, "birth": getattr(st, "st_birthtime", st.st_mtime)}


def _local_tz():
    return dt.datetime.now().astimezone().tzinfo


def time_from_name(name: str) -> tuple[float | None, str | None]:
    """Capture time encoded in a camera / phone file name."""
    n = os.path.basename(name)
    m = re.match(r"CAM_(\d{14})_", n)
    if m:
        return dt.datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=_local_tz()).timestamp(), "camera name"
    m = re.match(r"PXL_(\d{8})_(\d{6})", n)           # Google Pixel: UTC
    if m:
        return dt.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=UTC).timestamp(), "Pixel name (UTC)"
    m = re.match(r"Screenshot_(\d{8})-(\d{6})", n)
    if m:
        return dt.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=_local_tz()).timestamp(), "screenshot name"
    m = re.match(r"(?:IMG|VID)_(\d{8})_(\d{6})", n)
    if m:
        return dt.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=_local_tz()).timestamp(), "phone name"
    return None, None


def _ffprobe() -> str | None:
    return shutil.which("ffprobe") or ("/opt/homebrew/bin/ffprobe" if os.path.exists("/opt/homebrew/bin/ffprobe") else None)


def video_creation_time(p: str | os.PathLike) -> float | None:
    exe = _ffprobe()
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "-v", "quiet", "-print_format", "json", "-show_entries", "format_tags=creation_time", str(p)],
                             capture_output=True, text=True, timeout=30).stdout
        t = json.loads(out or "{}").get("format", {}).get("tags", {}).get("creation_time")
        if t:
            ts = dt.datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
            return None if is_missing(ts) else ts
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def exif_time(p: str | os.PathLike) -> float | None:
    """EXIF DateTimeOriginal (+ OffsetTimeOriginal) from a JPEG/HEIC-style TIFF block; no external tools."""
    try:
        with open(p, "rb") as f:
            d = f.read(512 * 1024)
        i = d.find(b"Exif\x00\x00")
        if i < 0:
            return None
        t = d[i + 6:]
        e = "<" if t[:2] == b"II" else ">"

        def ifd(off):
            n = struct.unpack(e + "H", t[off:off + 2])[0]
            return {struct.unpack(e + "H", t[off + 2 + 12 * k: off + 4 + 12 * k])[0]:
                    struct.unpack(e + "HI4s", t[off + 4 + 12 * k: off + 14 + 12 * k]) for k in range(n)}

        def text(v):
            _typ, cnt, val = v
            raw = t[struct.unpack(e + "I", val)[0]:][:cnt] if cnt > 4 else val[:cnt]
            return raw.split(b"\x00")[0].decode(errors="ignore")

        i0 = ifd(struct.unpack(e + "I", t[4:8])[0])
        if 0x8769 not in i0:
            return None
        ex = ifd(struct.unpack(e + "I", i0[0x8769][2])[0])
        if 0x9003 not in ex:
            return None
        when = dt.datetime.strptime(text(ex[0x9003]), "%Y:%m:%d %H:%M:%S")
        tz = _local_tz()
        if 0x9011 in ex:
            m = re.match(r"([+-])(\d\d):(\d\d)", text(ex[0x9011]))
            if m:
                sign = 1 if m.group(1) == "+" else -1
                tz = dt.timezone(sign * dt.timedelta(hours=int(m.group(2)), minutes=int(m.group(3))))
        ts = when.replace(tzinfo=tz).timestamp()
        return None if is_missing(ts) else ts
    except (OSError, ValueError, struct.error, IndexError):
        return None


def capture_time(p: str | os.PathLike, names: list[str] | tuple = ()) -> tuple[float | None, str | None]:
    """Best capture time: name (given names first, then the file's own) -> video creation_time -> EXIF."""
    for n in list(names) + [os.fspath(p)]:
        ts, how = time_from_name(n)
        if ts is not None and not is_missing(ts):
            return ts, how
    ts = video_creation_time(p)
    if ts is not None:
        return ts, "creation_time"
    ts = exif_time(p)
    if ts is not None:
        return ts, "EXIF DateTimeOriginal"
    return None, None


def _setfile_stamp(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%m/%d/%Y %H:%M:%S")


def set_dates(p: str | os.PathLike, mtime: float, birth: float) -> None:
    """Created date via SetFile -d, then modified via os.utime. If SetFile is missing or didn't take, a utime to the
    (earlier) created date first makes macOS lower the created date to it, then the modified date is set."""
    p = os.fspath(p)
    birth = min(birth, mtime)
    if os.path.exists(SETFILE):
        subprocess.run([SETFILE, "-d", _setfile_stamp(birth), p], capture_output=True, timeout=30)
    atime = os.stat(p).st_atime
    os.utime(p, (atime, mtime))
    if abs(getattr(os.stat(p), "st_birthtime", birth) - birth) > TOLERANCE_S:
        os.utime(p, (atime, birth))
        os.utime(p, (atime, mtime))


def wanted(snap: dict, p: str | os.PathLike, names=()) -> tuple[dict, str]:
    """The dates a copy/move result must have: the source's, or the capture time where the source has none."""
    m, b = snap.get("mtime"), snap.get("birth")
    if not is_missing(m) and not is_missing(b):
        return {"mtime": m, "birth": min(b, m)}, "original"
    ts, how = capture_time(p, names)
    if ts is None:
        keep_m = m if not is_missing(m) else (b if not is_missing(b) else None)
        if keep_m is None:
            return {}, "none"
        return {"mtime": keep_m, "birth": min(keep_m, b if not is_missing(b) else keep_m)}, "original (partial)"
    return {"mtime": m if not is_missing(m) else ts, "birth": min(ts, m if not is_missing(m) else ts)}, how


def check(p: str | os.PathLike, want: dict) -> list[str]:
    """What differs from the wanted dates ([] = fine)."""
    if not want:
        return []
    now = snapshot(p)
    bad = []
    for k, label in (("mtime", "modified"), ("birth", "created")):
        if k in want and abs(now[k] - want[k]) > TOLERANCE_S:
            bad.append(f"{label} {dt.datetime.fromtimestamp(want[k]):%Y-%m-%d %H:%M:%S} -> "
                       f"{dt.datetime.fromtimestamp(now[k]):%Y-%m-%d %H:%M:%S}")
    return bad


def keep(p: str | os.PathLike, snap: dict, names=()) -> list[str]:
    """After a copy / move / rename: give `p` the source's dates (or the capture time where the source had none),
    then verify. Returns warnings ([] = dates kept)."""
    want, how = wanted(snap, p, names)
    name = os.path.basename(os.fspath(p))
    if not want:
        return [f"{name}: no original date and no capture time found — dates left as they are (needs review)"]
    if check(p, want):
        try:
            set_dates(p, want["mtime"], want["birth"])
        except (OSError, subprocess.SubprocessError) as e:
            return [f"{name}: couldn't set dates ({e})"]
    bad = check(p, want)
    notes = [f"{name}: date changed and couldn't be restored: " + "; ".join(bad)] if bad else []
    if how not in ("original", "original (partial)") and not bad:
        notes.append(f"{name}: source had no date — set from the {how}")
    return notes
