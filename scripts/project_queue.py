#!/usr/bin/env python3
"""
ClipGauge v0.8 multi-project queue: several folders or clip sets, each its own project, run one after another.

logs/queue.json (plain JSON, written atomically; ExFAT-safe):
  {schema, state: idle|running|pausing|paused|stopped|done, pid, options: {dry_run, auto_bundle, bundle_scope},
   confirm_resolve, message, items: [project, ...]}
  project: {id, name, paths[], mode: in_place|copy, transcribe, name_in_filenames,
            status: waiting|running|paused|done|done_with_errors|skipped|refused|failed|stopped,
            reason, media_count, needs_confirm, copy_ok, batches[], copied[], copy_done,
            progress: {index, total, current, step}, message, log, started_at, finished_at}

Rules (same as everywhere): one pipeline run at a time (logs/pipeline.lock, taken by run_pipeline.py per project),
never while DaVinci Resolve is open (checked before every project; the queue pauses if Resolve opens), held /
Resolve-internal / Cloud-synced folders are refused (one row per dropped folder), clips inside the DaVinci Resolve
folder only after ONE confirmation per queue, never overwrite, every rename logged with a per-project batch id
(undo a whole project with undo_renames.py --batch for each id in "batches"), dates kept (filedates).

Resume: the queue survives Stop, quit, crash and sleep. Start again continues with the first project that isn't
finished; inside a project, clips already renamed are skipped (rename-log / notes store), so nothing is redone.

  python3 scripts/project_queue.py show | run | add PATH... [--mode copy|in_place]
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import ROOT, abs_path, atomic_write_text, load_config  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent
SCHEMA = 1
FINISHED = {"done", "done_with_errors", "skipped", "refused", "failed"}
RUNNABLE = {"waiting", "stopped", "running"}   # "running" here = interrupted mid-project (resumed)
EDITABLE = {"name", "mode", "transcribe", "name_in_filenames"}
PAUSE_FLAG = "queue-pause.flag"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def logs(cfg: dict) -> Path:
    d = abs_path(cfg.get("logs_dir") or "logs")
    d.mkdir(parents=True, exist_ok=True)
    return d


def path(cfg: dict) -> Path:
    return logs(cfg) / "queue.json"


def empty() -> dict:
    return {"schema": SCHEMA, "state": "idle", "pid": None, "confirm_resolve": False, "message": "",
            "options": {"dry_run": False, "auto_bundle": False, "bundle_scope": "queue"}, "items": [],
            "updated_at": _now()}


def read(cfg: dict) -> dict:
    try:
        d = json.loads(path(cfg).read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("items"), list):
            base = empty()
            base.update(d)
            base["options"] = {**empty()["options"], **(d.get("options") or {})}
            return base
    except (OSError, ValueError):
        pass
    return empty()


def write(cfg: dict, d: dict) -> None:
    d["updated_at"] = _now()
    atomic_write_text(path(cfg), json.dumps(d, indent=2, ensure_ascii=False) + "\n")


@contextmanager
def locked(cfg: dict, timeout: float = 10.0):
    """Short edit lock (mkdir is atomic on ExFAT too; flock isn't reliable there). Stale after 30 s."""
    lk = logs(cfg) / "queue.edit-lock"
    t0 = time.monotonic()
    while True:
        try:
            lk.mkdir()
            break
        except FileExistsError:
            try:
                if time.time() - lk.stat().st_mtime > 30:
                    lk.rmdir()
                    continue
            except OSError:
                pass
            if time.monotonic() - t0 > timeout:
                raise TimeoutError("the queue file is busy — try again")
            time.sleep(0.05)
    try:
        d = read(cfg)
        yield d
        write(cfg, d)
    finally:
        try:
            lk.rmdir()
        except OSError:
            pass


def alive(pid) -> bool:
    try:
        if not pid:
            return False
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------- items ----

def _classify(cfg: dict, paths: list[str]) -> dict:
    """In-place verdict for a project's paths: status ok|needs_confirm|refused, reason, counts, copy_ok."""
    import inplace
    import clipgauge_cli as cli
    sc = inplace.sort_cfg(cfg)
    held = {inplace._nfc(h) for h in sc.get("hold_sources") or []}
    worst, reason, media, copy_n, needs = "ok", "", 0, 0, False
    copy_ok = True
    for p in paths:
        c = inplace.classify(p, cfg, sc)
        if any(inplace._nfc(part) in held for part in Path(c["path"]).parts):
            copy_ok = False
        if c["status"] == "refused":
            worst, reason = "refused", c.get("reason") or "refused"
            if Path(p).exists() and copy_ok:
                copy_n += len(cli.copy_media(Path(p))[0])
            continue
        files, _ref = inplace.media_in(p, cfg, sc)
        media += len(files)
        copy_n += len(files)
        if c["status"] == "needs_confirm" or any(inplace.classify(f, cfg, sc)["status"] == "needs_confirm"
                                                 for f in files[:200]):
            needs = True
    return {"status": worst, "reason": reason, "media_count": media, "copy_count": copy_n,
            "needs_confirm": needs, "copy_ok": copy_ok}


def _group(paths: list[str]) -> list[tuple[str, list[str]]]:
    """Dropped paths -> projects: each folder is one project; loose files are grouped by their folder."""
    out: list[tuple[str, list[str]]] = []
    loose: dict[str, list[str]] = {}
    for raw in paths:
        p = Path(raw).expanduser()
        if p.is_dir():
            out.append((p.name, [str(p)]))
        else:
            loose.setdefault(str(p.parent), []).append(str(p))
    for parent, files in loose.items():
        out.append((Path(parent).name or "Clips", files))
    return out


def make_item(cfg: dict, name: str, paths: list[str], mode: str | None = None) -> dict:
    c = _classify(cfg, paths)
    it = {"id": uuid.uuid4().hex[:8], "name": name, "paths": paths, "mode": "in_place", "transcribe": True,
          "name_in_filenames": False, "status": "waiting", "reason": "", "media_count": c["media_count"],
          "copy_count": c["copy_count"], "needs_confirm": c["needs_confirm"], "copy_ok": c["copy_ok"],
          "batches": [], "copied": [], "copy_done": False, "progress": {}, "message": "", "added_at": _now()}
    if c["status"] == "refused":
        if c["copy_ok"] and c["copy_count"] and mode != "in_place":
            it["mode"] = "copy"
            it["reason"] = f"Can't be renamed in place ({c['reason']}) — will be copied into the inbox instead."
        else:
            it["status"], it["reason"] = "refused", c["reason"]
            if "hold" in c["reason"].lower():
                it["reason"] = f"“{name}” is on hold (hold_sources) — remove it from hold in Settings or config to process"
    elif mode == "copy":
        it["mode"] = "copy"
    if it["status"] != "refused" and not (it["media_count"] if it["mode"] == "in_place" else it["copy_count"]):
        it["status"], it["reason"] = "refused", "No video or photo files here."
    return it


def add(cfg: dict, paths: list[str], mode: str | None = None) -> dict:
    groups = _group(paths)
    with locked(cfg) as d:
        known = {tuple(i["paths"]) for i in d["items"] if i["status"] not in FINISHED}
        added = []
        for name, ps in groups:
            if tuple(ps) in known:
                continue
            it = make_item(cfg, name, ps, mode)
            d["items"].append(it)
            added.append(it)
        if d["state"] in ("done",):
            d["state"] = "idle"
    return {"added": added, "queue": summary(cfg)}


def _find(d: dict, iid: str) -> dict:
    for it in d["items"]:
        if it["id"] == iid:
            return it
    raise KeyError(f"no project {iid} in the queue")


def update(cfg: dict, iid: str, fields: dict) -> dict:
    with locked(cfg) as d:
        it = _find(d, iid)
        if it["status"] == "running":
            raise ValueError("That project is running — pause the queue first.")
        for k, v in fields.items():
            if k == "paused":
                if it["status"] in ("waiting", "stopped", "paused"):
                    it["status"] = "paused" if v else "waiting"
                continue
            if k not in EDITABLE:
                continue
            if k == "name":
                v = " ".join(str(v).split())[:120]
                if not v:
                    raise ValueError("The project name can't be empty.")
            if k == "mode":
                if v not in ("in_place", "copy"):
                    raise ValueError("mode is in_place or copy")
                c = _classify(cfg, it["paths"])
                if v == "in_place" and c["status"] == "refused":
                    raise ValueError(f"Can't process in place: {c['reason']}")
                if v == "copy" and not c["copy_ok"]:
                    raise ValueError("This folder is on hold — it isn't copied either.")
            it[k] = v
        return dict(it)


def remove(cfg: dict, iid: str) -> None:
    with locked(cfg) as d:
        it = _find(d, iid)
        if it["status"] == "running" and alive(d.get("pid")):
            raise ValueError("That project is running — skip it or stop the queue first.")
        d["items"] = [i for i in d["items"] if i["id"] != iid]


def move(cfg: dict, iid: str, to: int) -> None:
    with locked(cfg) as d:
        it = _find(d, iid)
        d["items"].remove(it)
        d["items"].insert(max(0, min(int(to), len(d["items"]))), it)


def clear(cfg: dict, finished_only: bool = True) -> int:
    with locked(cfg) as d:
        if not finished_only and alive(d.get("pid")):
            raise ValueError("The queue is running — stop it first.")
        before = len(d["items"])
        d["items"] = [i for i in d["items"] if finished_only and i["status"] not in FINISHED]
        if not d["items"]:
            d["state"], d["message"] = "idle", ""
        return before - len(d["items"])


def skip(cfg: dict, iid: str) -> dict:
    with locked(cfg) as d:
        it = _find(d, iid)
        if it["status"] == "running" and alive(d.get("pid")):
            it["skip_requested"] = True   # the runner stops this project's run and moves on
        elif it["status"] not in FINISHED:
            it["status"], it["message"] = "skipped", "Skipped by you."
        return dict(it)


# ------------------------------------------------------------ start/stop ----

def summary(cfg: dict) -> dict:
    d = read(cfg)
    run_alive = alive(d.get("pid"))
    if d["state"] in ("running", "pausing") and not run_alive:
        d["state"], d["interrupted"] = "stopped", True   # quit / crash / sleep: Start resumes
        d["message"] = d.get("message") or "The queue stopped before it finished — Start resumes it."
    left = [i for i in d["items"] if i["status"] in RUNNABLE]
    d.update({"running": run_alive and d["state"] in ("running", "pausing"), "left": len(left),
              "clips_left": sum(i.get("media_count") if i["mode"] == "in_place" else i.get("copy_count", 0)
                                for i in left),
              "needs_confirm": [i["name"] for i in left if i["mode"] == "in_place" and i.get("needs_confirm")],
              "path": str(path(cfg))})
    return d


def start(cfg: dict, opts: dict) -> tuple[int, dict]:
    import renamer_actions as ra
    from check_resolve import resolve_running
    d = summary(cfg)
    if d["running"]:
        return 409, {"error": "The queue is already running."}
    info = ra.run_info(cfg)
    if info["active"]:
        return 409, {"error": "Another run is active — wait for it to finish or stop it first."}
    if ra.active_updates(cfg):
        return 409, {"error": "Model/tool updates are being installed — the queue can start when they finish."}
    if resolve_running():
        return 409, {"error": "DaVinci Resolve is running — quit Resolve, then start the queue."}
    if not d["left"]:
        return 400, {"error": "Nothing to run — add folders or clips first (finished, skipped and refused projects "
                              "don't run again)."}
    dry = bool(cfg.get("dry_run", True)) or bool(opts.get("dry_run"))
    if d["needs_confirm"] and not dry and not opts.get("confirm_resolve"):
        return 409, {"error": "Some projects are inside the DaVinci Resolve folder — renaming clips Resolve has "
                              "imported breaks their links. Confirm once for this queue.",
                     "needs_confirm": d["needs_confirm"]}
    with locked(cfg) as q:
        q["options"]["dry_run"] = bool(opts.get("dry_run"))
        for k in ("auto_bundle", "bundle_scope"):
            if k in opts:
                q["options"][k] = opts[k]
        q["confirm_resolve"] = bool(opts.get("confirm_resolve")) or q.get("confirm_resolve", False)
        q["state"], q["message"], q["pid"] = "running", "Starting…", None
        for it in q["items"]:
            if it["status"] in ("running", "stopped"):
                it["status"] = "waiting"
    (logs(cfg) / PAUSE_FLAG).unlink(missing_ok=True)
    log = logs(cfg) / f"clipgauge-queue-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    cmd = [sys.executable, "-u", str(SCRIPTS / "project_queue.py"), "run"]
    if Path("/usr/bin/caffeinate").exists():
        cmd = ["/usr/bin/caffeinate", "-i"] + cmd   # the Mac stays awake for the whole queue
    env = dict(os.environ, PYTHONUNBUFFERED="1", AI_VIDEO_RENAMER_QUEUE_LOG=str(log))
    env.setdefault("OLLAMA_MODELS", str(abs_path(cfg.get("models_dir") or "models")))
    with open(log, "ab") as lf:
        lf.write(f"# queue started {_now()}: {' '.join(cmd)}\n".encode())
        proc = subprocess.Popen(cmd, cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT,
                                start_new_session=True, close_fds=True, env=env)
    with locked(cfg) as q:
        q["pid"], q["pgid"], q["log"] = proc.pid, proc.pid, str(log)
    return 202, {"ok": True, "message": f"Queue started: {d['left']} project(s), {d['clips_left']} clip(s)"
                                        + (" (dry run)." if dry else "."), "log": str(log)}


def set_options(cfg: dict, opts: dict) -> dict:
    with locked(cfg) as d:
        for k in ("auto_bundle", "bundle_scope", "dry_run"):
            if k in opts:
                d["options"][k] = opts[k]
    return summary(cfg)


def pause(cfg: dict) -> dict:
    """Pause after the clip in progress (nothing is abandoned)."""
    (logs(cfg) / PAUSE_FLAG).write_text(_now() + "\n", encoding="utf-8")
    with locked(cfg) as d:
        if alive(d.get("pid")):
            d["state"], d["message"] = "pausing", "Pausing after the clip in progress…"
        else:
            d["state"] = "paused" if d["items"] else "idle"
    return summary(cfg)


def stop(cfg: dict) -> dict:
    """Stop now: the clip in progress is abandoned (finished clips stay renamed). Start resumes later."""
    d = read(cfg)
    pg = d.get("pgid") or d.get("pid")
    if pg and alive(d.get("pid")):
        try:
            os.killpg(int(pg), signal.SIGTERM)
        except OSError:
            pass
        for _ in range(50):
            if not alive(d.get("pid")):
                break
            time.sleep(0.1)
    with locked(cfg) as q:
        q["state"], q["message"] = "stopped", "Stopped — Start resumes the queue."
        for it in q["items"]:
            if it["status"] == "running":
                it["status"], it["message"] = "stopped", "Stopped — resumes where it left off."
    return summary(cfg)


# ------------------------------------------------------------------ runner ----

def _status(cfg: dict) -> dict:
    try:
        return json.loads((logs(cfg) / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _copy_in(cfg: dict, it: dict) -> tuple[bool, str]:
    """Copy this project's media into inbox/ (never overwrites; dates kept). Records where each file landed."""
    cmd = [sys.executable, "-u", str(SCRIPTS / "clipgauge_cli.py"), "copy-in", *it["paths"]]
    proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    done = {}
    for line in proc.stdout:
        try:
            j = json.loads(line)
        except ValueError:
            continue
        if j.get("event") in ("progress", "file"):
            with locked(cfg) as d:
                x = _find(d, it["id"])
                x["progress"] = {"step": "copying", "index": j.get("index"), "total": j.get("files"),
                                 "current": j.get("file"), "done_bytes": j.get("done_bytes"),
                                 "total_bytes": j.get("total_bytes")}
        if j.get("event") == "done":
            done = j
    proc.wait()
    copied = [c["to"] for c in done.get("copied") or []]
    with locked(cfg) as d:
        x = _find(d, it["id"])
        x["copied"] = sorted(set(x.get("copied", []) + copied))
        x["copy_done"] = bool(done.get("ok"))
    return bool(done.get("ok")), str(done.get("message") or done.get("error") or "")


def _pipeline_cmd(cfg: dict, d: dict, it: dict, batch: str) -> list[str] | None:
    cmd = [sys.executable, "-u", str(SCRIPTS / "run_pipeline.py"), "--all", "--batch-id", batch]
    if d["options"].get("dry_run"):
        cmd.append("--dry-run")
    if not it.get("transcribe", True):
        cmd.append("--skip-whisper")
    if it["mode"] == "in_place":
        for p in it["paths"]:
            cmd += ["--source", p]
        if d.get("confirm_resolve") and it.get("needs_confirm"):
            cmd.append("--confirm-resolve")
        if it.get("name_in_filenames"):
            cmd += ["--project", it["name"]]
    else:
        left = [p for p in it.get("copied", []) if Path(p).is_file()]
        if not left:
            return None
        for p in left:
            cmd += ["--video", p]
        cmd += ["--folder", it["name"]] + (["--project-from-folder"] if it.get("name_in_filenames") else [])
    return cmd


def _run_project(cfg: dict, it: dict) -> str:
    """Run one project. Returns the queue's next state: 'next' | 'paused' | 'stopped' | 'blocked'."""
    from check_resolve import resolve_running
    if resolve_running():
        with locked(cfg) as d:
            d["state"], d["message"] = "paused", "DaVinci Resolve is open — the queue paused. Quit Resolve, then Start."
        return "blocked"
    with locked(cfg) as d:
        x = _find(d, it["id"])
        x["status"], x["started_at"], x["message"] = "running", x.get("started_at") or _now(), ""
        d["message"] = f"Running “{x['name']}”"
        it = dict(x)
        q = dict(d)
    if it["mode"] == "copy" and not it.get("copy_done"):
        ok, msg = _copy_in(cfg, it)
        if not ok:
            with locked(cfg) as d:
                x = _find(d, it["id"])
                x["status"], x["message"], x["finished_at"] = "failed", f"Copy failed: {msg}", _now()
            return "next"
        it = _find(read(cfg), it["id"])
    batch = f"{datetime.now().strftime('%Y%m%dT%H%M%S')}-queue-{it['id']}"
    cmd = _pipeline_cmd(cfg, q, it, batch)
    if cmd is None:
        with locked(cfg) as d:
            x = _find(d, it["id"])
            x["status"], x["message"], x["finished_at"] = "done", "Nothing left to process.", _now()
        return "next"
    run_log = logs(cfg) / f"clipgauge-run-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    env = dict(os.environ, AI_VIDEO_RENAMER_LAUNCHED_BY="ClipGauge queue", AI_VIDEO_RENAMER_RUN_LOG=str(run_log),
               PYTHONUNBUFFERED="1")
    with locked(cfg) as d:
        x = _find(d, it["id"])
        x["batches"] = x.get("batches", []) + [batch]
        x["log"] = str(run_log)
    with open(run_log, "ab") as lf:
        lf.write(f"# started from the ClipGauge queue ({it['name']}) {_now()}: {' '.join(cmd)}\n".encode())
        lf.flush()
        child = subprocess.Popen(cmd, cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT,
                                 env=env)
    skipped = False
    total = None
    while child.poll() is None:
        time.sleep(2)
        st = _status(cfg)
        m = re.search(r"\((\d+)/(\d+)\)", str(st.get("message") or ""))
        with locked(cfg) as d:
            x = _find(d, it["id"])
            if m:
                total = int(m.group(2))
                x["progress"] = {"step": st.get("step"), "index": int(m.group(1)), "total": total,
                                 "current": Path(str(st.get("current_file") or "")).name or None,
                                 "eta_seconds": st.get("eta_seconds")}
            if x.get("skip_requested") and not skipped:
                skipped = True
                child.send_signal(signal.SIGTERM)
    rc = child.returncode
    st = _status(cfg)
    with locked(cfg) as d:
        x = _find(d, it["id"])
        x.pop("skip_requested", None)
        x["message"] = str(st.get("message") or "")[:600]
        if skipped:
            x["status"], x["message"] = "skipped", "Skipped by you (clips finished before that stay renamed)."
        elif rc == 4:
            x["status"] = "waiting"
            d["state"], d["message"] = "paused", "Paused — Start resumes the queue."
        elif rc == 130:
            x["status"] = "stopped"
        elif rc in (0, 1):
            x["status"] = "done" if rc == 0 else "done_with_errors"
            x["progress"] = {"index": total or 0, "total": total or 0}
        else:
            x["status"] = "failed"
            x["message"] = str(st.get("last_error") or st.get("message") or f"exit code {rc}")[:600]
        if x["status"] in FINISHED:
            x["finished_at"] = _now()
        nxt = "paused" if rc == 4 else ("stopped" if rc == 130 and not skipped else "next")
    return nxt


def _bundle(cfg: dict, d: dict) -> str:
    import notes_store as ns
    scope = d["options"].get("bundle_scope") or "queue"
    cmd = [sys.executable, str(SCRIPTS / "build_transcript_bundle.py"), "--json"]
    if scope == "everything":
        cmd.append("--everywhere")
    else:
        root = ns.volume_root(cfg)
        for it in d["items"]:
            if it["status"] not in ("done", "done_with_errors"):
                continue
            dirs = ({str(Path(p) if Path(p).is_dir() else Path(p).parent) for p in it["paths"]}
                    if it["mode"] == "in_place" else {str(ns.inbox_dir(cfg) / it["name"])})
            for p in dirs:
                try:
                    cmd += ["--path", str(Path(p).relative_to(root))]
                except ValueError:
                    pass
        if "--path" not in cmd:
            return "Transcript bundle: no finished project folders to include."
    r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=1800)
    last = (r.stdout.strip().splitlines() or [""])[-1]
    try:
        return str(json.loads(last).get("message") or "Transcript bundle written to exports/.")
    except ValueError:
        return f"Transcript bundle: exit {r.returncode}"


def run() -> int:
    cfg = load_config()
    flag = logs(cfg) / PAUSE_FLAG

    def _term(signum, frame):  # noqa: ARG001
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGHUP, _term)
    with locked(cfg) as d:
        d["pid"], d["state"] = os.getpid(), "running"
    try:
        while True:
            d = read(cfg)
            if flag.exists() and d["state"] in ("pausing", "paused"):
                with locked(cfg) as q:
                    q["state"], q["message"] = "paused", "Paused — Start resumes the queue."
                break
            nxt = next((i for i in d["items"] if i["status"] in RUNNABLE), None)
            if nxt is None:
                msg = "Queue finished."
                if d["options"].get("auto_bundle"):
                    msg += " " + _bundle(cfg, d)
                done = [i for i in d["items"] if i["status"] in ("done", "done_with_errors")]
                with locked(cfg) as q:
                    q["state"], q["message"] = "done", f"{msg} {len(done)} project(s) processed."
                break
            r = _run_project(cfg, nxt)
            if r in ("paused", "blocked", "stopped"):
                if r == "stopped":
                    with locked(cfg) as q:
                        q["state"], q["message"] = "stopped", "Stopped — Start resumes the queue."
                break
    except KeyboardInterrupt:
        with locked(cfg) as q:
            q["state"], q["message"] = "stopped", "Stopped — Start resumes the queue."
            for it in q["items"]:
                if it["status"] == "running":
                    it["status"] = "stopped"
        return 130
    finally:
        flag.unlink(missing_ok=True)
        try:
            with locked(cfg) as q:
                q["pid"] = None
        except Exception:  # noqa: BLE001
            pass
    return 0


# ------------------------------------------------------------ transcripts ----

def search_transcripts(cfg: dict, query: str, limit: int = 200) -> dict:
    """Case-insensitive search across every clip's transcript in the notes store."""
    import notes_store as ns
    q = " ".join(query.lower().split())
    hits = []
    if not q:
        return {"query": query, "hits": []}
    for rec in ns.load_all(cfg):
        segs = (rec.get("transcript") or {}).get("segments") or []
        for s in segs:
            t = str(s.get("text") or "")
            if q in " ".join(t.lower().split()):
                cur = rec.get("current_path") or ""
                hits.append({"clip_id": rec.get("clip_id"), "file": Path(cur).name or (rec.get("source") or {}).get("name"),
                             "path": cur, "start": s.get("start"), "end": s.get("end"), "text": t.strip()})
                if len(hits) >= limit:
                    return {"query": query, "hits": hits, "truncated": True}
    return {"query": query, "hits": hits}


def transcript(cfg: dict, key: str) -> dict:
    import notes_store as ns
    rec = ns.load(cfg, key) if ns.ID_RE.match(key or "") else None
    if rec is None:
        rec = ns.find(cfg, key)
    if rec is None:
        return {"error": "No notes for that clip (it may not have been processed yet)."}
    tx = rec.get("transcript") or {}
    return {"clip_id": rec.get("clip_id"), "file": Path(rec.get("current_path") or "").name,
            "path": rec.get("current_path"), "language": tx.get("language"),
            "segments": [{"start": s.get("start"), "end": s.get("end"), "text": str(s.get("text") or "").strip()}
                         for s in tx.get("segments") or []],
            "has_speech": bool(tx.get("segments"))}


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "show"
    if cmd == "run":
        return run()
    cfg = load_config()
    if cmd == "add":
        mode = None
        if "--mode" in argv:
            mode = argv[argv.index("--mode") + 1]
            argv = [a for a in argv if a not in ("--mode", mode)]
        print(json.dumps(add(cfg, argv[1:], mode), indent=2, ensure_ascii=False))
        return 0
    print(json.dumps(summary(cfg), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
