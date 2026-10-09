#!/usr/bin/env python3
"""Apply updates found by check_updates.py — never run implicitly; ClipGauge asks first and shows the size.

  python3 scripts/apply_updates.py --all                 # everything upgradable (asks to confirm in Terminal)
  python3 scripts/apply_updates.py --items tool:ollama,model:qwen2.5vl:7b --yes
  python3 scripts/apply_updates.py --all --yes --detach  # background (new session + caffeinate) — what ClipGauge uses
  python3 scripts/apply_updates.py --status [--json]     # progress of the current / last job
  python3 scripts/apply_updates.py --cancel              # stop after the current download chunk (resumable)
  python3 scripts/apply_updates.py --resume --yes        # finish an interrupted / cancelled job

  * Models: `ollama pull` through the running Ollama server, which must serve <project>/models (checked first).
    The old version is kept under <name>:<tag>-clipgauge-prev until the new one answers a tiny prompt; then that
    backup tag is removed with Ollama's own delete (Ollama frees the old blobs itself — nothing is deleted by hand).
    If the test fails, the previous version is restored.
  * Tools: `brew upgrade <formula>`; after ollama, its LaunchAgent (com.retrocombs.ollama-lexar / com.clipgauge.ollama /
    homebrew.mxcl.ollama) is restarted if present.
  * Whisper files: download to models/whisper/.<file>.part (resumable), verify sha256, keep the old file as .bak,
    swap in, load-test with whisper-cli, then drop the .bak (or restore it on failure).

Refuses while a processing run holds logs/pipeline.lock or DaVinci Resolve is open; holds logs/updates.lock so
processing can't start meanwhile. Progress: logs/updates-status.json; log: logs/updates-*.log.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import wave
from collections import deque
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_updates as cu  # noqa: E402
from _common import ROOT, atomic_write_text, load_config, section  # noqa: E402
import pipeline_lock as pl  # noqa: E402

MARKER = pl.UPDATES_MARKER
OLLAMA_AGENTS = ("com.retrocombs.ollama-lexar", "com.clipgauge.ollama", "com.clipguage.ollama",  # .clipguage = label made
                 "homebrew.mxcl.ollama")                                       # by ClipGuage ≤ v0.5
EXIT_BLOCKED, EXIT_BUSY = 4, 5


class Cancelled(BaseException):   # not an Exception: per-item error handlers must not swallow it
    pass


class Blocked(Exception):
    pass


_CANCEL = {"requested": False, "interruptible": True}


def _on_term(_sig, _frm):
    _CANCEL["requested"] = True
    if _CANCEL["interruptible"]:
        raise Cancelled()


# ---------------------------------------------------------------------------
# paths / guards
# ---------------------------------------------------------------------------

def updates_lock_path(cfg: dict) -> Path:
    return pl.updates_lock_path(cfg)


def status_file(cfg: dict) -> Path:
    return cu.logs_dir(cfg) / "updates-status.json"


def state_file(cfg: dict) -> Path:
    return cu.logs_dir(cfg) / "updates-state.json"


def active_pipeline(cfg: dict) -> dict | None:
    return pl.active_lock(pl.lock_path(cfg))


def resolve_open() -> list[str]:
    from check_resolve import resolve_running
    return resolve_running()


def active_updates(cfg: dict) -> dict | None:
    return pl.active_updates(cfg)


def blocked_reason(cfg: dict) -> str | None:
    run = active_pipeline(cfg)
    if run:
        return (f"a processing run is active (pid {run.get('pid')}, from {run.get('launched_by') or 'terminal'}) — "
                "updates wait until it finishes")
    if resolve_open():
        return "DaVinci Resolve is open — quit Resolve, then upgrade"
    return None


def read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------------------
# progress
# ---------------------------------------------------------------------------

def eta_text(ts: float) -> str:
    return time.strftime("%-I:%M %p %Z", time.localtime(ts))


class Progress:
    DEFAULT_TOOL_BYTES = 30_000_000   # weight for a brew step whose bottle size is unknown

    def __init__(self, cfg: dict, items: list[dict], log: str | None):
        self.cfg, self.path, self.log = cfg, status_file(cfg), log
        self.items = [{"id": i["id"], "name": i["name"], "kind": i["kind"], "target": i.get("target"),
                       "state": "pending", "message": "",
                       "bytes_total": int(i.get("download_bytes") or (self.DEFAULT_TOOL_BYTES if i["kind"] == "tool" else 0)),
                       "bytes_done": 0} for i in items]
        self.started = time.time()
        self.samples: deque = deque(maxlen=60)
        self.index = 0
        self.state = "running"
        self.message = "Starting…"
        self._last = 0.0
        self.write(force=True)

    def item(self, i: int) -> dict:
        return self.items[i]

    def totals(self) -> tuple[int, int]:
        return (sum(i["bytes_done"] for i in self.items), sum(max(i["bytes_total"], i["bytes_done"]) for i in self.items))

    def speed(self) -> float:
        now = time.monotonic()
        while len(self.samples) > 2 and now - self.samples[0][0] > 20:
            self.samples.popleft()
        if len(self.samples) < 2:
            return 0.0
        (t0, b0), (t1, b1) = self.samples[0], self.samples[-1]
        return max(0.0, (b1 - b0) / (t1 - t0)) if t1 > t0 else 0.0

    def update(self, i: int, done: int | None = None, total: int | None = None, message: str | None = None,
               state: str | None = None, force: bool = False) -> None:
        it = self.items[i]
        if total is not None and total > 0:
            it["bytes_total"] = total
        if done is not None:
            it["bytes_done"] = max(0, done)
        if message is not None:
            it["message"] = message
            self.message = f"{it['name']}: {message}"
        if state is not None:
            it["state"] = state
            if state == "done":
                it["bytes_done"] = max(it["bytes_done"], it["bytes_total"])
        self.index = i
        self.samples.append((time.monotonic(), self.totals()[0]))
        self.write(force=force or state is not None)

    def snapshot(self) -> dict:
        done, total = self.totals()
        spd = self.speed()
        remaining = max(0, total - done)
        eta_s = remaining / spd if spd > 1 and self.state == "running" else None
        return {
            "state": self.state, "pid": os.getpid(), "pgid": os.getpgid(0),
            "started_at": datetime.fromtimestamp(self.started).astimezone().isoformat(timespec="seconds"),
            "updated_at": cu.now_iso(), "log": self.log,
            "item_index": self.index + 1, "item_total": len(self.items),
            "current": self.items[self.index]["name"] if self.items else None,
            "bytes_done": done, "bytes_total": total,
            "percent": int(min(100, done * 100 / total)) if total else (100 if self.state != "running" else 0),
            "speed_bps": int(spd), "eta_seconds": int(eta_s) if eta_s else None,
            "eta": datetime.fromtimestamp(time.time() + eta_s).astimezone().isoformat(timespec="seconds") if eta_s else None,
            "eta_text": eta_text(time.time() + eta_s) if eta_s else None,
            "message": self.message, "items": self.items,
        }

    def write(self, force: bool = False) -> None:
        if not force and time.monotonic() - self._last < 1.0:
            return
        self._last = time.monotonic()
        try:
            atomic_write_text(self.path, json.dumps(self.snapshot(), indent=1) + "\n")
        except OSError:
            pass

    def finish(self, state: str, message: str) -> None:
        self.state, self.message = state, message
        self.write(force=True)


def say(text: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {text}", flush=True)


# ---------------------------------------------------------------------------
# Ollama (HTTP API of the running server — same thing `ollama pull/cp/rm/show` do)
# ---------------------------------------------------------------------------

def ollama_base(cfg: dict) -> str:
    return (section(cfg, "describe").get("ollama_url") or "http://127.0.0.1:11434").rstrip("/")


def ollama_post(base: str, path: str, body: dict | None = None, timeout: float = 60, method: str = "POST") -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read() or b"{}").get("error")
        except ValueError:
            msg = None
        raise RuntimeError(f"Ollama {path}: HTTP {e.code} {msg or ''}".strip()) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Ollama isn't reachable at {base} ({e.reason})") from e
    except (TimeoutError, ConnectionError, http.client.HTTPException, OSError) as e:
        # A server that is still starting (e.g. right after a LaunchAgent restart) can accept the connection and
        # then time out or drop it. Before v0.5 this escaped as a bare "timed out" and failed the whole upgrade.
        raise RuntimeError(f"Ollama {path}: {type(e).__name__}: {e or 'no answer'}") from e
    return json.loads(raw) if raw.strip() else {}


def ollama_stream(base: str, path: str, body: dict, timeout: float = 900):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for line in r:
                if line.strip():
                    yield json.loads(line)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Ollama {path}: HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Ollama isn't reachable at {base} ({e.reason})") from e


def verify_serving_project(base: str, mdir: Path) -> None:
    """Refuse to pull if the running Ollama keeps its models somewhere other than <project>/models.
    Works for both layouts: a served model matches when its /api/tags digest is one of the local copy's ids (Ollama
    0.40 reports the per-runner child manifest digest, not the tag file's)."""
    local = {ref: ident for ref in cu.installed_models(mdir) if (ident := cu.local_identity(mdir, ref))}
    if not local:
        return   # nothing to compare yet — checked after the pull instead
    tags = ollama_post(base, "/api/tags", None, timeout=15, method="GET").get("models") or []
    served: dict[str, set] = {}
    for m in tags:
        served.setdefault(m.get("name") or m.get("model") or "", set()).add(str(m.get("digest") or "").split(":")[-1])
    for ref, ident in local.items():
        for name in (ref, ref.replace(":latest", "")):
            if served.get(name, set()) & set(ident["ids"]):
                return
    raise RuntimeError(f"The running Ollama isn't using {mdir} (none of its models match). "
                       f"Restart it with OLLAMA_MODELS={mdir} (ClipGauge › Setup › Install Missing… can set that up).")


def prev_refs(ref: str) -> list[str]:
    ns, name, tag = cu.parse_ref(ref)
    return [cu.ref_of(ns, name, tag + suf) for suf in (cu.PREV_SUFFIX, *cu.LEGACY_PREV_SUFFIXES)]


def stale_prev_tags(mdir: Path) -> list[tuple[str, str]]:
    """Backup tags left behind by an interrupted/failed upgrade: [(prev_ref, model_ref)] where the backup is an
    exact duplicate of the model's current tag (same manifest bytes) — removing it frees nothing and loses nothing."""
    out = []
    for base_ref in cu.installed_models(mdir):
        cur = cu.local_identity(mdir, base_ref)
        for pr in prev_refs(base_ref):
            pi = cu.local_identity(mdir, pr)
            if pi and cur and pi["digest"] == cur["digest"]:
                out.append((pr, base_ref))
    return out


def cleanup_stale_prev(base: str, mdir: Path, dry: bool = False) -> list[str]:
    """Delete duplicate backup tags through Ollama's own API (never touches models/ files directly)."""
    done = []
    for pr, ref in stale_prev_tags(mdir):
        if dry:
            done.append(f"would remove {pr} (identical to {ref})")
            continue
        try:
            ollama_post(base, "/api/delete", {"model": pr}, timeout=60, method="DELETE")
            done.append(f"removed {pr} (identical to {ref})")
        except RuntimeError as e:
            done.append(f"couldn't remove {pr}: {e}")
    return done


def sanity_check_model(base: str, ref: str) -> str:
    ollama_post(base, "/api/show", {"model": ref}, timeout=60)
    r = ollama_post(base, "/api/generate", {"model": ref, "prompt": "Reply with the single word OK.", "stream": False,
                                            "think": False, "keep_alive": 0,
                                            "options": {"num_predict": 8, "temperature": 0}}, timeout=600)
    text = (r.get("response") or r.get("thinking") or "").strip()
    if not text:
        raise RuntimeError("the model loaded but returned an empty answer")
    return text[:40]


def upgrade_model(cfg: dict, it: dict, prog: Progress, i: int) -> str:
    base, mdir, ref = ollama_base(cfg), cu.models_dir(cfg), it["target"]
    prog.update(i, message="checking Ollama…", state="running")
    ver = ollama_post(base, "/api/version", None, timeout=10, method="GET").get("version")
    verify_serving_project(base, mdir)
    vpath = cu.versions_path(mdir, cfg)
    try:
        remote, man = cu.remote_manifest(ref)
    except (OSError, ValueError) as e:
        raise RuntimeError(f"couldn't read {ref} from the Ollama registry ({e})") from e
    layers = cu.manifest_layers(man)
    old = cu.local_identity(mdir, ref)
    ok, how = cu.is_current(mdir, ref, remote, layers, old, cu.read_versions(vpath))
    if ok:   # nothing newer exists — never pull/swap for a false positive
        cu.record_version(vpath, ref, remote, old, f"upgrade-skip:{how}")
        return f"already current ({remote[:12]}) — nothing downloaded"
    prev = prev_refs(ref)[0]
    had_old = old is not None
    if had_old:
        ollama_post(base, "/api/copy", {"source": ref, "destination": prev}, timeout=60)
        say(f"{ref}: kept the current version as {prev} until the new one is verified")
    seen: dict[str, list[int]] = {}
    swapped = False

    def drop_prev() -> None:
        try:
            ollama_post(base, "/api/delete", {"model": prev}, timeout=60, method="DELETE")
        except RuntimeError as e:
            say(f"note: couldn't remove backup tag {prev}: {e}")

    try:
        prog.update(i, message=f"downloading via Ollama {ver}…")
        for ev in ollama_stream(base, "/api/pull", {"model": ref, "stream": True}):
            if ev.get("error"):
                raise RuntimeError(f"ollama pull: {ev['error']}")
            dg, total, done = ev.get("digest"), ev.get("total"), ev.get("completed")
            if dg and total:
                if dg not in seen:   # [done, total, already present locally -> not counted]
                    seen[dg] = [0, int(total), 1 if (done or 0) >= total else 0]
                seen[dg][0] = int(done or 0)
            counted = [v for v in seen.values() if not v[2]]
            prog.update(i, done=sum(v[0] for v in counted), total=sum(v[1] for v in counted) or None,
                        message=ev.get("status") or "")
        swapped = True
        now = cu.local_identity(mdir, ref)
        got, how = cu.is_current(mdir, ref, remote, layers, now)
        if now is None:
            raise RuntimeError(f"Ollama reported success, but {ref} isn't in {mdir} (looked in manifests-v2/ and "
                               "manifests/) — is Ollama using this project's models folder?")
        if not got:
            raise RuntimeError(f"Ollama reported success, but {mdir} has no copy of registry version {remote[:12]} "
                               f"(local {now['digest'][:12]}) — the pull didn't take")
        prog.update(i, message="testing the new version…")
        answer = sanity_check_model(base, ref)
    except Exception as e:  # noqa: BLE001 - restore and report any failure
        if swapped and had_old:
            try:
                ollama_post(base, "/api/copy", {"source": prev, "destination": ref}, timeout=60)
            except RuntimeError as e2:
                raise RuntimeError(f"{e} — restoring failed ({e2}); the previous version is still saved as {prev}") from e
            restored = cu.local_identity(mdir, ref)
            if restored and old and restored["digest"] == old["digest"]:
                drop_prev()   # the tag is back to the exact previous manifest: the backup is a pure duplicate now
                raise RuntimeError(f"{e} — restored the previous version") from e
            raise RuntimeError(f"{e} — restored the previous version; backup kept as {prev} to be safe") from e
        if had_old:   # pull never replaced it: Ollama kept the old manifest, drop the duplicate tag
            drop_prev()
        raise
    cu.record_version(vpath, ref, remote, cu.local_identity(mdir, ref), f"upgrade:{how}")
    if had_old:
        # New version verified: remove the backup tag with Ollama's own delete (it frees the old blobs itself).
        drop_prev()
    return f"updated to {remote[:12]} · test answer “{answer}”"


# ---------------------------------------------------------------------------
# Homebrew
# ---------------------------------------------------------------------------

def restart_ollama_agents() -> str:
    uid = os.getuid()
    restarted = []
    for label in OLLAMA_AGENTS:
        plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        if not plist.is_file():
            continue
        rc, _o, _e = cu.run_cmd(["launchctl", "print", f"gui/{uid}/{label}"], timeout=15)
        if rc != 0:
            continue
        rc, _o, err = cu.run_cmd(["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"], timeout=30)
        restarted.append(label if rc == 0 else f"{label} (failed: {err.strip()[:80]})")
    return ", ".join(restarted)


OLLAMA_RESTART_WAIT_S = 90


def plain_version(v: str | None) -> str:
    """'0.40.1_1' (Homebrew revision) -> '0.40.1'."""
    import re
    return re.sub(r"_\d+$", "", str(v or "").strip())


def wait_ollama(base: str, seconds: float = OLLAMA_RESTART_WAIT_S, expect: str | None = None, interval: float = 2.0,
                fetch=None, sleep=time.sleep, clock=time.monotonic) -> dict:
    """Health check after a restart: poll GET /api/version until it answers (and, with expect, reports that version —
    the old process can still answer for a moment), retrying through refused/reset/timed-out requests.
    Returns {"version": v|None, "ok": bool, "attempts": n, "waited_s": s, "last_error": str|None}."""
    fetch = fetch or (lambda: ollama_post(base, "/api/version", None, timeout=5, method="GET").get("version"))
    t0 = clock()
    end = t0 + seconds
    res = {"version": None, "ok": False, "attempts": 0, "waited_s": 0.0, "last_error": None}
    want = plain_version(expect) if expect else None
    while True:
        res["attempts"] += 1
        try:
            v = fetch()
            res["version"] = v
            if v and (not want or plain_version(v) == want):
                res["ok"] = True
                break
            res["last_error"] = f"answering as {v}, waiting for {want}" if v else "empty version"
        except Exception as e:  # noqa: BLE001 - any failure while it's starting means "try again"
            res["last_error"] = str(e)[:200]
        if clock() + interval > end:
            break
        sleep(interval)
    res["waited_s"] = round(clock() - t0, 1)
    return res


def ollama_restart_note(health: dict, agents: str, seconds: float) -> str:
    """Message for the job item after `brew upgrade ollama` + LaunchAgent restart. Never an error: brew succeeded."""
    if health["ok"]:
        return f" · restarted {agents} · Ollama {health['version']} answering (after {health['waited_s']:.0f} s)"
    if health["version"]:
        return (f" · restarted {agents} · Ollama answers as {health['version']} (not the new version yet after "
                f"{seconds:.0f} s — it may need another restart)")
    return (f" · restarted {agents} · Ollama not answering yet after {seconds:.0f} s ({health['last_error']}); "
            "it may still be starting — ClipGauge › Setup shows when it's back")


def upgrade_tool(cfg: dict, it: dict, prog: Progress, i: int) -> str:
    brew = cu.brew_path()
    if not brew:
        raise RuntimeError("Homebrew isn't installed")
    formula = it["target"]
    prog.update(i, message=f"brew upgrade {formula}…", state="running")
    _CANCEL["interruptible"] = False   # never kill brew halfway; a cancel stops after this step
    try:
        proc = subprocess.Popen([brew, "upgrade", formula], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, text=True,
                                env={**os.environ, "HOMEBREW_NO_AUTO_UPDATE": "1", "HOMEBREW_NO_ENV_HINTS": "1",
                                     "NONINTERACTIVE": "1"})
        tail: deque = deque(maxlen=8)
        for line in proc.stdout:
            line = line.rstrip()
            print(line, flush=True)
            tail.append(line)
            if line.startswith("==>"):
                prog.update(i, message=line[4:].strip()[:120])
        rc = proc.wait()
    finally:
        _CANCEL["interruptible"] = True
    if rc != 0:
        raise RuntimeError(f"brew upgrade {formula} failed (exit {rc}): " + " | ".join(list(tail)[-3:]))
    _rc, out, _ = cu.run_cmd([brew, "list", "--versions", formula], timeout=60, env={"HOMEBREW_NO_AUTO_UPDATE": "1"})
    ver = (out.strip().split() or ["?"])[-1]
    msg = f"now {ver}"
    if formula == "ollama":
        prog.update(i, message="restarting Ollama…")
        agents = restart_ollama_agents()
        if agents:
            wait_s = float(section(cfg, "updates").get("ollama_restart_wait_s") or OLLAMA_RESTART_WAIT_S)
            prog.update(i, message=f"waiting for Ollama {plain_version(ver)} to answer (up to {wait_s:.0f} s)…")
            health = wait_ollama(ollama_base(cfg), seconds=wait_s, expect=ver)
            say(f"   Ollama health check: {health}")
            msg += ollama_restart_note(health, agents, wait_s)
        else:
            msg += " · restart Ollama (or log out and in) to use the new version"
    if _CANCEL["requested"]:
        raise Cancelled()
    return msg


# ---------------------------------------------------------------------------
# Whisper ggml files
# ---------------------------------------------------------------------------

def download(url: str, part: Path, on_progress, expected_size: int | None) -> str:
    """Resumable download into part; returns sha256 of the whole file."""
    h = hashlib.sha256()
    offset = part.stat().st_size if part.exists() else 0
    if expected_size and offset > expected_size:
        part.unlink()
        offset = 0
    if offset:
        with open(part, "rb") as f:
            for b in iter(lambda: f.read(4 << 20), b""):
                h.update(b)
    req = urllib.request.Request(url, headers={"User-Agent": cu.UA, **({"Range": f"bytes={offset}-"} if offset else {})})
    try:
        r = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 416 and expected_size and offset == expected_size:
            on_progress(offset, expected_size)
            return h.hexdigest()
        raise RuntimeError(f"download failed: HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"download failed: {e.reason}") from e
    with r:
        if offset and r.status != 206:      # server ignored Range -> start over
            offset = 0
            h = hashlib.sha256()
            mode = "wb"
        else:
            mode = "ab"
        total = expected_size or ((int(r.headers.get("content-length") or 0) + offset) or None)
        done = offset
        with open(part, mode) as f:
            for b in iter(lambda: r.read(1 << 20), b""):
                f.write(b)
                h.update(b)
                done += len(b)
                on_progress(done, total)
    return h.hexdigest()


def _silence_wav(path: Path, seconds: float = 1.0) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(struct.pack("<h", 0) * int(16000 * seconds))


def whisper_load_test(cfg: dict, model: Path) -> str:
    from transcribe import whisper_cli_path
    cli = whisper_cli_path(section(cfg, "whisper"))
    if not cli:
        return "load test skipped (whisper-cli not installed)"
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "silence.wav"
        _silence_wav(wav)
        rc, _o, err = cu.run_cmd([cli, "-m", str(model), "-f", str(wav), "-nt", "-np"], timeout=300)
    if rc != 0:
        raise RuntimeError(f"whisper-cli couldn't load it (exit {rc}): {err.strip()[-160:]}")
    return "loads in whisper-cli"


def upgrade_whisper(cfg: dict, it: dict, prog: Progress, i: int, fetcher=download, tester=whisper_load_test) -> str:
    wdir = cu.whisper_dir(cfg)
    wdir.mkdir(parents=True, exist_ok=True)
    fname = it["target"]
    if "/" in fname or not fname.startswith("ggml-") or not fname.endswith(".bin"):
        raise RuntimeError(f"unexpected whisper file name {fname!r}")
    dest, part, bak = wdir / fname, wdir / f".{fname}.part", wdir / f"{fname}.bak"
    base = section(cfg, "whisper").get("download_base_url") or cu.HF_BASE
    expected_sha, expected_size = it.get("latest_sha"), it.get("size_bytes")
    prog.update(i, message="downloading…", state="running", total=expected_size)
    sha = fetcher(f"{base.rstrip('/')}/{fname}", part, lambda d, t: prog.update(i, done=d, total=t), expected_size)
    size = part.stat().st_size
    if expected_size and size != expected_size:
        raise RuntimeError(f"size mismatch ({size} vs {expected_size} bytes) — partial file kept, Resume continues it")
    if expected_sha and len(expected_sha) == 64 and sha != expected_sha:
        part.unlink(missing_ok=True)
        raise RuntimeError("checksum mismatch — download discarded, current file untouched")
    prog.update(i, message="verifying…")
    had_old = dest.exists()
    if had_old:
        os.replace(dest, bak)
    os.replace(part, dest)
    try:
        note = tester(cfg, dest)
    except Exception as e:  # noqa: BLE001
        if had_old:
            os.replace(bak, dest)
            raise RuntimeError(f"{e} — restored the previous file") from e
        raise
    if had_old:
        bak.unlink(missing_ok=True)
    return f"replaced · sha256 verified · {note}"


# ---------------------------------------------------------------------------
# job
# ---------------------------------------------------------------------------

HANDLERS = {"model": upgrade_model, "tool": upgrade_tool, "whisper": upgrade_whisper}
ORDER = {"tool": 0, "whisper": 1, "model": 2}   # tools first (a newer Ollama may be needed for newer manifests)


def plan(cfg: dict, ids: list[str] | None, resume: bool = False) -> list[dict]:
    if resume:
        st = read_json(status_file(cfg))
        ids = [i["id"] for i in st.get("items", []) if i.get("state") != "done"]
        if not ids:
            raise SystemExit("Nothing to resume.")
    state = read_json(state_file(cfg))
    if not state.get("items"):
        say("No recent check — checking first (no brew update)…")
        state = cu.run_check(cfg, brew_update=False, reason="before-upgrade")
    by_id = {i["id"]: i for i in state["items"]}
    if ids is None:
        chosen = [i for i in state["items"] if i.get("upgradable")]
    else:
        missing = [x for x in ids if x not in by_id]
        if missing:
            raise SystemExit(f"Unknown update id(s): {', '.join(missing)} — run check_updates.py first.")
        chosen = [by_id[x] for x in ids if by_id[x].get("upgradable") or resume]
        skipped = [x for x in ids if not (by_id[x].get("upgradable") or resume)]
        if skipped:
            say(f"Skipping (nothing to upgrade): {', '.join(skipped)}")
    return sorted(chosen, key=lambda i: ORDER.get(i["kind"], 9))


def run_job(cfg: dict, items: list[dict]) -> int:
    ok, holder = pl.acquire_lock(updates_lock_path(cfg), marker=MARKER)
    if not ok:
        say(f"Another update job is running (pid {holder.get('pid')}).")
        return EXIT_BUSY
    prog = Progress(cfg, items, os.environ.get("CLIPGAUGE_UPDATE_LOG") or os.environ.get("CLIPGUAGE_UPDATE_LOG"))
    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)   # survive the parent / Terminal going away
    failures = 0
    final = "done"
    try:
        for i, it in enumerate(items):
            why = blocked_reason(cfg)
            if why:
                for j in range(i, len(items)):
                    prog.update(j, state="skipped", message=f"not started: {why}")
                final = "blocked"
                say(f"Stopped before {it['name']}: {why}")
                break
            say(f"[{i + 1}/{len(items)}] {it['name']} ({it['kind']}) …")
            cu.log_line(cfg, f"upgrade start: {it['id']}")
            try:
                msg = HANDLERS[it["kind"]](cfg, it, prog, i)
                prog.update(i, state="done", message=msg)
                cu.log_line(cfg, f"upgrade ok: {it['id']} — {msg}")
                say(f"   ✓ {msg}")
            except Cancelled:
                raise
            except Exception as e:  # noqa: BLE001 - report per item, keep going
                failures += 1
                prog.update(i, state="failed", message=str(e)[:300])
                cu.log_line(cfg, f"upgrade FAILED: {it['id']} — {e}")
                say(f"   ✗ {e}")
    except Cancelled:
        final = "cancelled"
        for it in prog.items:
            if it["state"] in ("pending", "running"):
                it["state"] = "pending"
                it["message"] = "cancelled — Resume continues (downloads pick up where they stopped)"
        cu.log_line(cfg, "upgrade job cancelled")
        say("Cancelled — run with --resume to continue.")
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
    if final == "done" and failures:
        final = "done_with_errors"
    done = sum(1 for it in prog.items if it["state"] == "done")
    summary = {"done": f"Updated {done} item(s).", "done_with_errors": f"Updated {done}, {failures} failed — see the log.",
               "cancelled": "Cancelled — Resume continues where it stopped.",
               "blocked": prog.message}[final]
    if any(it.get("kind") == "model" for it in items):
        for line in cleanup_stale_prev(ollama_base(cfg), cu.models_dir(cfg)):   # exact-duplicate backups only
            say(f"note: {line}")
            cu.log_line(cfg, f"cleanup-prev: {line}")
    prog.finish(final, summary)
    pl.release_lock(updates_lock_path(cfg))
    if done:
        try:
            cu.run_check(cfg, brew_update=False, reason="after-upgrade")
        except Exception as e:  # noqa: BLE001
            say(f"note: re-check failed: {e}")
    say(summary)
    return 0 if final == "done" else (EXIT_BLOCKED if final == "blocked" else 1)


def spawn_detached(cfg: dict, ids: list[str]) -> dict:
    ldir = cu.logs_dir(cfg)
    ldir.mkdir(parents=True, exist_ok=True)
    log = ldir / f"updates-{datetime.now():%Y%m%d-%H%M%S}.log"
    cmd = [sys.executable, "-u", str(Path(__file__).resolve()), "--worker", "--yes", "--items", ",".join(ids)]
    if Path("/usr/bin/caffeinate").exists():
        cmd = ["/usr/bin/caffeinate", "-i", "-m"] + cmd   # keep the Mac (and the drive) awake until done
    with open(log, "ab") as lf:
        lf.write(f"# started {cu.now_iso()}: {' '.join(cmd)}\n".encode())
        lf.flush()
        proc = subprocess.Popen(cmd, cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT,
                                start_new_session=True, close_fds=True,
                                env={**os.environ, "CLIPGAUGE_UPDATE_LOG": str(log), "PYTHONUNBUFFERED": "1"})
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        info = active_updates(cfg)
        if info:
            return {"ok": True, "pid": info.get("pid"), "log": str(log), "items": ids}
        if proc.poll() is not None:
            tail = log.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-3:]
            return {"ok": False, "error": f"The update job stopped right away (exit {proc.returncode}): " + " | ".join(tail),
                    "log": str(log)}
        time.sleep(0.2)
    return {"ok": True, "pid": proc.pid, "log": str(log), "items": ids}


def reverify(cfg: dict, check=None, brew_version=None, ollama_version=None) -> dict:
    """Re-check items a finished job marked "failed" (read-only: a fresh check, no brew update, no downloads).
    A tool that the fresh check reports current — and for Ollama, a server answering with the installed version —
    is corrected to "done" in logs/updates-status.json; the job state is recomputed. Used for v0.3's false
    'timed out' failure after `brew upgrade ollama`."""
    if active_updates(cfg):
        raise SystemExit("An update job is running — re-verify after it finishes.")
    path = status_file(cfg)
    st = read_json(path)
    if not st.get("items"):
        return {"ok": True, "changed": 0, "message": "No update job to re-verify."}
    failed = [it for it in st["items"] if it.get("state") == "failed"]
    if not failed:
        return {"ok": True, "changed": 0, "message": f"Last job is {st.get('state')} — nothing failed, nothing to correct."}
    check = check or (lambda: cu.run_check(cfg, brew_update=False, reason="reverify"))
    state = check()
    by_id = {i["id"]: i for i in state.get("items", [])}
    changed = []
    for it in failed:
        cur = by_id.get(it["id"]) or {}
        if cur.get("status") != "current":
            continue
        note = f"verified {cu.now_iso()}: {cur.get('detail') or 'up to date'}"
        if it["id"] == "tool:ollama":
            bv = brew_version() if brew_version else None
            if bv is None:
                brew = cu.brew_path()
                _rc, out, _ = cu.run_cmd([brew, "list", "--versions", "ollama"], timeout=60,
                                         env={"HOMEBREW_NO_AUTO_UPDATE": "1"}) if brew else (1, "", "")
                bv = (out.strip().split() or [None])[-1]
            health = ollama_version() if ollama_version else wait_ollama(ollama_base(cfg), seconds=15, expect=bv)
            if not health.get("ok"):
                continue
            note = (f"now {plain_version(bv)} · Ollama {health['version']} answering — verified {cu.now_iso()} "
                    f"(the job's '{it.get('message')}' was a restart check that gave up too early)")
        it["state"], it["message"] = "done", note
        it["bytes_done"] = max(it.get("bytes_done") or 0, it.get("bytes_total") or 0)
        changed.append(it["id"])
    if changed:
        left = sum(1 for it in st["items"] if it.get("state") == "failed")
        done = sum(1 for it in st["items"] if it.get("state") == "done")
        if st.get("state") == "done_with_errors" and not left:
            st["state"] = "done"
            st["message"] = f"Updated {done} item(s) (re-verified {cu.now_iso()})."
        elif left:
            st["message"] = f"Updated {done}, {left} failed — see the log (re-verified {cu.now_iso()})."
        st["percent"] = 100 if not left else st.get("percent", 100)
        st["reverified_at"] = cu.now_iso()
        st.setdefault("corrections", []).append({"at": cu.now_iso(), "items": changed, "by": "apply_updates.py --reverify"})
        atomic_write_text(path, json.dumps(st, indent=1) + "\n")
        cu.log_line(cfg, f"re-verify: corrected {', '.join(changed)} to done (job now {st['state']})")
    return {"ok": True, "changed": len(changed), "items": changed, "state": st.get("state"),
            "message": (f"Corrected {', '.join(changed)} → done; job is now {st.get('state')}." if changed
                        else "Nothing corrected — the failed item(s) still don't check out.")}


def cancel(cfg: dict) -> int:
    info = active_updates(cfg)
    if not info:
        print("No update job is running.")
        return 0
    os.kill(int(info["pid"]), signal.SIGTERM)
    print(f"Asked the update job (pid {info['pid']}) to stop; it finishes the current step safely.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Apply model/tool updates found by check_updates.py")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--all", action="store_true", help="Everything the last check marked upgradable")
    g.add_argument("--items", help="Comma-separated ids from check_updates.py --json (e.g. tool:ollama)")
    g.add_argument("--resume", action="store_true", help="Continue an interrupted/cancelled job")
    g.add_argument("--status", action="store_true", help="Show progress of the current/last job")
    g.add_argument("--cancel", action="store_true", help="Stop the running job (resumable)")
    g.add_argument("--reverify", action="store_true",
                   help="Read-only: re-check items the last job marked failed and correct the job status if they're fine")
    g.add_argument("--cleanup-prev", action="store_true",
                   help="Remove leftover '-clipgauge-prev' backup tags that are exact duplicates of the model's current "
                        "tag (via Ollama's API). Add --preview to only list them")
    ap.add_argument("--preview", action="store_true", help="With --cleanup-prev: list, change nothing")
    ap.add_argument("--yes", action="store_true", help="Don't ask for confirmation")
    ap.add_argument("--detach", action="store_true", help="Run in the background (new session + caffeinate)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    cfg = load_config()

    if args.status:
        st = read_json(status_file(cfg))
        st["active"] = bool(active_updates(cfg))
        if args.json:
            print(json.dumps(st, indent=2))
        elif not st.get("state"):
            print("No update job has run yet.")
        else:
            print(f"{st['state']}{' (running)' if st['active'] else ''} {st.get('percent', 0)}% — {st.get('message', '')}")
        return 0
    if args.cancel:
        return cancel(cfg)
    if args.reverify:
        r = reverify(cfg)
        print(json.dumps(r) if args.json else r["message"])
        return 0
    if args.cleanup_prev:
        if not args.preview and active_updates(cfg):
            print("An update job is running — try again after it finishes.")
            return EXIT_BUSY
        lines = cleanup_stale_prev(ollama_base(cfg), cu.models_dir(cfg), dry=args.preview)
        msg = "\n".join(lines) or "No leftover backup tags."
        if lines and not args.preview:
            cu.log_line(cfg, "cleanup-prev: " + "; ".join(lines))
        print(json.dumps({"ok": True, "items": lines, "message": msg}) if args.json else msg)
        return 0

    def out(obj: dict, code: int) -> int:
        print(json.dumps(obj) if args.json else (obj.get("message") or obj.get("error") or json.dumps(obj)))
        return code

    if not args.worker:
        busy = active_updates(cfg)
        if busy:
            return out({"ok": False, "error": f"An update job is already running (pid {busy.get('pid')})."}, EXIT_BUSY)
        why = blocked_reason(cfg)
        if why:
            return out({"ok": False, "error": f"Can't upgrade now: {why}.", "blocked": True}, EXIT_BLOCKED)
    ids = [x.strip() for x in args.items.split(",") if x.strip()] if args.items else None
    items = plan(cfg, ids, resume=args.resume)
    if not items:
        return out({"ok": True, "message": "Everything is up to date — nothing to upgrade."}, 0)
    total = sum(int(i.get("download_bytes") or 0) for i in items)
    if not args.yes and not args.worker:
        print("Will upgrade:\n" + "\n".join(f"  • {i['name']} — {i.get('detail', '')}" for i in items))
        print(f"Total download ≈ {cu.human_bytes(total)} (≈ {total / 2e6 / 60:.0f} min on slow hotel Wi-Fi at 2 MB/s).")
        if not sys.stdin.isatty() or input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
            return out({"ok": False, "error": "Not confirmed — nothing changed."}, 1)
    if args.detach:
        r = spawn_detached(cfg, [i["id"] for i in items])
        r.setdefault("message", f"Updating {len(items)} item(s) in the background — log: {r.get('log')}")
        return out(r, 0 if r.get("ok") else 1)
    return run_job(cfg, items)


if __name__ == "__main__":
    sys.exit(main())
