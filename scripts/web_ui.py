#!/usr/bin/env python3
"""
Local web UI for AI Video Renamer (Python 3 stdlib, no CDN, binds 127.0.0.1).
Banner shows the real mode: "Dry run" (config dry_run true) or "Live" (clips renamed in place in inbox/).

  python3 scripts/web_ui.py                # serve in the foreground (Ctrl-C to stop)
  python3 scripts/web_ui.py --background   # start detached (new session -> survives closing Terminal), log: logs/web_ui.log
  python3 scripts/web_ui.py --stop         # stop the detached server
  python3 scripts/web_ui.py --status       # is it running?
Port/host: config.json "ui" section (default 127.0.0.1:8765); --port overrides.

Endpoints
  GET  /                      single-page UI
  GET  /api/ping              {"app": "ai-video-renamer-ui", pid, port}
  GET  /api/inbox             inbox files with per-video state pending / renamed / needs review + counts
                              (hides dotfiles, ._ AppleDouble, .uploading-* temps and the clips' .json/.md sidecars);
                              "folders": subfolders of inbox/ (batch folders) with their files, counts and states;
                              "movable_count": renamed clips still at inbox/ top level (for "Move into folder")
  GET  /api/folder-check?name=   sanitized batch folder name, whether inbox/<name>/ exists (+ clip count), project slug
  GET  /api/status            logs/status.json + run info (lock / Terminal-started run detection)
  GET  /api/results?limit=N   recent logs/dry-run/report.jsonl entries (newest first, one per source; &all=1 for every line)
  GET  /api/sidecar?json=P    the .md sidecar next to sidecar JSON P (must be inside logs/dry-run/ or inbox/)
  GET  /api/upload-check?name=&size=   what an upload would be saved as (or duplicate)
  PUT  /api/upload?name=&size=&mtime=  raw request body streamed to inbox/.uploading-<name>, renamed into place when complete
  POST /api/start             detached `caffeinate -i run_pipeline.py --all` (refused if Resolve running / run active);
                              live mode renames each confident clip in place right after it is processed.
                              JSON body (optional): {"use_folder": true, "folder": "Show 2026", "project_from_folder": false}
                              -> run_pipeline.py --all --folder "Show 2026" [--project-from-folder]
  POST /api/apply             run apply_renames.py for clips already processed (refused while a run is active or dry_run true);
                              same body: {"use_folder": true, "folder": NAME} -> apply_renames.py --folder NAME
  POST /api/move-into-folder  {"folder": NAME} -> apply_renames.py --move-into NAME: clips already renamed and still at
                              inbox/ top level (per the rename log) + sidecars move into inbox/NAME/ (refused while a run
                              is active or dry_run true; logged, undo_renames.py reverses it)
  GET  /api/sort/sources      Sort into Projects: candidate source folders (inbox + DaVinci Resolve/ folders, held/synced marked)
  POST /api/sort/plan         {"source": NAME|PATH|"inbox", "gap_minutes": N} -> sort_projects.py --dry-run (plan saved in logs/sort-plans/)
  POST /api/sort/apply        {"plan_file": P, "edits": {...}, "confirm": true} -> sort_projects.py --apply (refused while Resolve is open)
  POST /api/sort/undo         {"plan_id": ID?, "confirm": true} -> sort_projects.py --undo (latest sort by default)
  GET  /api/sort/history      applied sorts (undo maps in logs/sort-undo-*.jsonl)
  GET  /api/instructions      custom instructions (config/custom-instructions.json) + hash, limits, active flag
  POST /api/instructions      {"standing", "glossary", "next_run": {"text", "keep"}} -> saved (400 when over a limit)
  POST /api/instructions/preview  same body (unsaved draft) + "photo": bool -> the full prompt that would be sent
  POST /api/stop              SIGTERM to the process group recorded in logs/pipeline.lock
  GET  /api/transcript-scopes scopes for the transcript bundle (inbox/, inbox subfolders, Lexar folders with clip notes)
                              + the most recent exports/ files
  POST /api/transcript-bundle {"scope": "inbox" | "folder:NAME" | "path:REL" | "everywhere", "include_silent": false,
                              "exclude_photos": false, "date": "YYYY-MM-DD"} -> build_transcript_bundle.py --json: one .json + one .md in exports/
                              (read-only for media; allowed during a run — sidecars are written atomically)
  GET  /exports/NAME          download a transcripts-*.json/.md from exports/ (nothing outside exports/ is served)
  GET  /api/note?id=CLIP_ID   a clip's notes rendered as Markdown from the central store (notes/clips/<id>.json)
  POST /api/settings          {"write_next_to_clip": bool} -> config.json sidecar.write_next_to_clip (applies to the
                              next run; the central notes store is always written)
Caching: every response is Cache-Control: no-store; the page itself also no-cache/must-revalidate (+ Pragma/Expires),
so a refresh always shows the current UI after an update.
Mutating requests need header "X-Renamer-UI: 1" and a 127.0.0.1/localhost Host header (blocks cross-site requests
and DNS rebinding). The UI itself never renames, moves or deletes files: renames/moves happen only in run_pipeline.py (live mode) and
apply_renames.py (Apply / Move buttons), all logged to logs/rename-log.jsonl and reversible with undo_renames.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# v0.6: everything that is not HTTP lives in renamer_actions.py (shared with ClipGauge); web_ui.py is the legacy fallback.
from renamer_actions import *  # noqa: E402,F401,F403
import renamer_actions as _ra  # noqa: E402


# --------------------------------------------------------------- server ----

class Handler(BaseHTTPRequestHandler):
    server_version = "AIVideoRenamerUI/1"
    protocol_version = "HTTP/1.1"
    timeout = 300  # drop dead upload connections instead of holding a thread forever

    # -- plumbing
    def log_message(self, fmt, *args):  # noqa: A003
        pass  # requests are logged selectively in log_request

    def log_request(self, code="-", size="-"):
        path = urlsplit(self.path).path
        if path in QUIET_PATHS and str(code).startswith("2"):
            return
        log(f"{self.command} {path} -> {code}")

    def no_cache(self, html: bool = False) -> None:
        """Never cache: refreshes always get the current page/API state (the page also revalidates on every load)."""
        if html:
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        else:
            self.send_header("Cache-Control", "no-store")

    def send_json(self, code: int, obj, close: bool = False) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.no_cache()
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, code: int, text: str, ctype: str = "text/plain; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.no_cache(html=ctype.startswith("text/html"))
        self.send_header("X-Content-Type-Options", "nosniff")
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(body)

    def send_export(self, path: Path) -> None:
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", EXPORT_TYPES.get(path.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.no_cache()
        self.send_header("X-Content-Type-Options", "nosniff")
        disp = "inline" if self.query().get("view") == "1" else "attachment"
        self.send_header("Content-Disposition", f'{disp}; filename="{path.name}"')
        self.end_headers()
        self.wfile.write(data)

    def host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        port = self.server.server_address[1]
        return host in {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

    def guard(self, mutating: bool) -> bool:
        if not self.host_ok():
            self.send_json(403, {"error": "Forbidden host"}, close=True)
            return False
        if mutating and self.headers.get("X-Renamer-UI") != "1":
            self.send_json(403, {"error": "Missing X-Renamer-UI header"}, close=True)
            return False
        return True

    def query(self) -> dict:
        return {k: v[-1] for k, v in parse_qs(urlsplit(self.path).query).items()}

    # -- routes
    def do_GET(self):  # noqa: N802
        if not self.guard(mutating=False):
            return
        path, q, cfg = urlsplit(self.path).path, self.query(), load_config()
        try:
            if path in ("/", "/index.html"):
                u = ui_cfg(cfg)
                self.send_text(200, PAGE.replace("__POLL_MS__", str(u["poll_ms"])).replace("__EXTS__", json.dumps(sorted(MEDIA_EXTS))),
                               "text/html; charset=utf-8")
            elif path == "/api/ping":
                self.send_json(200, {"app": APP_ID, "pid": os.getpid(), "port": self.server.server_address[1], "root": str(ROOT)})
            elif path == "/api/inbox":
                self.send_json(200, list_inbox(cfg))
            elif path == "/api/updates":
                self.send_json(200, updates_info(cfg))
            elif path == "/api/status":
                self.send_json(200, {"status": read_status(cfg), "run": run_info(cfg), "dry_run": cfg.get("dry_run", True) is True,
                                     "write_next_to_clip": ns.next_to_clip(cfg), "instructions": instructions_summary(cfg),
                                     "server_time": datetime.now().astimezone().isoformat(timespec="seconds")})
            elif path == "/api/results":
                limit = max(1, min(5000, int(q.get("limit") or ui_cfg(cfg)["results_limit"])))
                self.send_json(200, read_results(cfg, limit, dedupe=q.get("all") != "1"))
            elif path == "/api/sidecar":
                code, text = sidecar_md(cfg, q.get("json") or "")
                self.send_text(code, text)
            elif path == "/api/folder-check":
                self.send_json(200, folder_check(cfg, q.get("name") or ""))
            elif path == "/api/upload-check":
                size = int(q["size"]) if q.get("size", "").isdigit() else None
                self.send_json(200, upload_plan(inbox_dir(cfg), q.get("name") or "", size))
            elif path == "/api/note":
                code, text = note_markdown(cfg, q.get("id") or "")
                self.send_text(code, text)
            elif path == "/api/transcript-scopes":
                self.send_json(200, transcript_scopes(cfg))
            elif path == "/api/instructions":
                self.send_json(200, instructions_info(cfg))
            elif path == "/api/sort/sources":
                self.send_json(*sort_cli(cfg, ["--list-sources"], timeout=60))
            elif path == "/api/sort/history":
                self.send_json(*sort_cli(cfg, ["--history"], timeout=60))
            elif path.startswith("/exports/"):
                code, res = export_file(cfg, urllib.parse.unquote(path[len("/exports/"):]))
                if code == 200:
                    self.send_export(res)
                else:
                    self.send_text(code, res)
            elif path == "/favicon.ico":
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self.send_json(404, {"error": "not found"})
        except ValueError as e:
            self.send_json(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            log(f"ERROR GET {path}: {type(e).__name__}: {e}")
            self.send_json(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):  # noqa: N802
        if not self.guard(mutating=True):
            return
        path, cfg = urlsplit(self.path).path, load_config()
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(n, 65536)) if n else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw.strip() else {}
        except (ValueError, UnicodeDecodeError):
            body = {}
        body = body if isinstance(body, dict) else {}
        try:
            if path == "/api/start":
                self.send_json(*start_pipeline(cfg, body))
            elif path == "/api/stop":
                self.send_json(*stop_pipeline(cfg))
            elif path == "/api/apply":
                self.send_json(*apply_proposed(cfg, body))
            elif path == "/api/move-into-folder":
                self.send_json(*move_into_folder(cfg, body))
            elif path == "/api/transcript-bundle":
                self.send_json(*build_transcript_bundle(cfg, body))
            elif path == "/api/sort/plan":
                self.send_json(*sort_plan(cfg, body))
            elif path == "/api/instructions":
                self.send_json(*save_instructions(cfg, body))
            elif path == "/api/instructions/preview":
                self.send_json(*preview_instructions(cfg, body))
            elif path == "/api/sort/apply":
                self.send_json(*sort_apply(cfg, body))
            elif path == "/api/sort/undo":
                self.send_json(*sort_undo(cfg, body))
            elif path == "/api/settings":
                if "write_next_to_clip" not in body:
                    self.send_json(400, {"error": "nothing to change"})
                else:
                    self.send_json(*set_write_next_to_clip(bool(body.get("write_next_to_clip"))))
            else:
                self.send_json(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001
            log(f"ERROR POST {path}: {type(e).__name__}: {e}")
            self.send_json(500, {"error": f"{type(e).__name__}: {e}"})

    def do_PUT(self):  # noqa: N802
        if not self.guard(mutating=True):
            return
        if urlsplit(self.path).path != "/api/upload":
            self.send_json(404, {"error": "not found"}, close=True)
            return
        try:
            self.handle_upload(load_config())
        except Exception as e:  # noqa: BLE001
            log(f"ERROR upload: {type(e).__name__}: {e}")
            try:
                self.send_json(500, {"error": f"{type(e).__name__}: {e}"}, close=True)
            except OSError:
                pass

    def handle_upload(self, cfg: dict) -> None:
        q = self.query()
        cl = self.headers.get("Content-Length")
        if cl is None or not cl.isdigit():
            self.send_json(411, {"error": "Content-Length required (chunked uploads not supported)"}, close=True)
            return
        length = int(cl)
        inbox = inbox_dir(cfg)
        try:
            raw = q.get("name") or ""
            safe = sanitize_filename(raw)
        except ValueError as e:
            self.send_json(400, {"error": str(e)}, close=True)
            return
        if not is_video_name(safe) and q.get("force") != "1":
            self.send_json(415, {"error": f"{safe}: not a video or photo extension ({', '.join(sorted(MEDIA_EXTS))}); "
                                          "the pipeline would skip it. Re-send with force=1 to upload anyway."}, close=True)
            return
        need = length + int(ui_cfg(cfg)["min_free_gb"] * 1024**3)
        if shutil.disk_usage(inbox).free < need:
            self.send_json(507, {"error": f"Not enough free space on the Lexar for {safe} ({length} bytes + safety margin)."}, close=True)
            return
        with UPLOAD_LOCK:
            plan = upload_plan(inbox, raw, length)
            if plan["action"] == "duplicate":
                self.send_json(409, {"error": plan["message"], "duplicate": True, "name": plan["name"]}, close=True)
                return
            final = plan["name"]
            tmp = inbox / (TEMP_PREFIX + final)
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            RESERVED.add(final.lower())
        log(f"upload start: {raw!r} -> {final} ({length} bytes)")
        t0, remaining, ok = time.monotonic(), length, False
        try:
            with os.fdopen(fd, "wb") as f:
                while remaining > 0:
                    chunk = self.rfile.read(min(CHUNK, remaining))
                    if not chunk:
                        break  # client went away
                    f.write(chunk)
                    remaining -= len(chunk)
                if remaining == 0:
                    f.flush()
                    os.fsync(f.fileno())
                    ok = True
        except (OSError, ConnectionError) as e:
            log(f"upload error {final}: {e}")
        finally:
            if not ok:
                tmp.unlink(missing_ok=True)
                (inbox / ("._" + tmp.name)).unlink(missing_ok=True)
                with UPLOAD_LOCK:
                    RESERVED.discard(final.lower())
        if not ok:
            log(f"upload incomplete: {final} ({length - remaining}/{length} bytes) — temp removed")
            try:
                self.send_json(400, {"error": f"Upload incomplete ({length - remaining} of {length} bytes) — nothing saved."}, close=True)
            except OSError:
                pass
            return
        with UPLOAD_LOCK:
            RESERVED.discard(final.lower())
            if (inbox / final).exists():  # appeared meanwhile (e.g. Finder copy) — never overwrite
                final = unique_name(inbox, final)
            os.rename(tmp, inbox / final)
        (inbox / ("._" + tmp.name)).unlink(missing_ok=True)
        mtime = q.get("mtime", "")
        if mtime.isdigit():  # keep the original modified time (pipeline falls back to it for the clip date)
            try:
                os.utime(inbox / final, (time.time(), int(mtime) / 1000))
            except (OSError, ValueError, OverflowError):
                pass
        dt = max(time.monotonic() - t0, 1e-6)
        log(f"upload done: {final} {length} bytes in {dt:.1f}s ({length / dt / 1e6:.1f} MB/s)")
        self.send_json(201, {"ok": True, "name": final, "size": length, "renamed": final != plan["requested"],
                             "message": plan["message"] or f"Saved to inbox/{final}"})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def pid_file(cfg: dict) -> Path:
    return logs_dir(cfg) / "web_ui.pid"


def serve(cfg: dict, port: int) -> int:
    u = ui_cfg(cfg)
    try:
        httpd = Server((u["host"], port), Handler)
    except OSError as e:
        log(f"cannot bind {u['host']}:{port}: {e}")
        return 1
    removed = cleanup_temp_uploads(inbox_dir(cfg))
    if removed:
        log(f"removed leftover partial uploads: {removed}")
    pf = pid_file(cfg)
    pf.write_text(f"{os.getpid()}\n")

    def _term(signum, frame):  # noqa: ARG001
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _term)
    log(f"AI Video Renamer UI on http://{u['host']}:{port}/ (pid {os.getpid()}, dry_run={cfg.get('dry_run')})")
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        try:
            if pf.read_text().strip() == str(os.getpid()):
                pf.unlink()
        except OSError:
            pass
        log("server stopped")
    return 0


def ping(port: int, timeout: float = 1.0) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=timeout) as r:
            data = json.loads(r.read().decode())
            return data if data.get("app") == APP_ID else None
    except Exception:  # noqa: BLE001
        return None


def start_background(cfg: dict, port: int) -> int:
    url = f"http://127.0.0.1:{port}/"
    info = ping(port)
    if info:
        print(f"Already running (pid {info['pid']}): {url}")
        return 0
    logf = logs_dir(cfg) / "web_ui.log"
    with open(logf, "ab") as lf:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--port", str(port)], cwd=str(ROOT),
                         stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
    for _ in range(50):
        time.sleep(0.2)
        info = ping(port)
        if info:
            print(f"Started (pid {info['pid']}): {url}  log: {logf}")
            return 0
    print(f"Server did not come up — see {logf}", file=sys.stderr)
    return 1


def stop_background(cfg: dict, port: int) -> int:
    info = ping(port)
    pid = info["pid"] if info else None
    if pid is None:
        try:
            pid = int(pid_file(cfg).read_text().strip())
        except (OSError, ValueError):
            print("Renamer UI is not running.")
            return 0
    if not pid_alive(pid) or "web_ui.py" not in pid_command(pid):
        print("Renamer UI is not running (stale pid file removed).")
        pid_file(cfg).unlink(missing_ok=True)
        return 0
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        if not pid_alive(pid):
            print(f"Stopped Renamer UI (pid {pid}). A running review (if any) keeps going.")
            return 0
        time.sleep(0.1)
    print(f"pid {pid} did not exit after SIGTERM", file=sys.stderr)
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="AI Video Renamer local web UI")
    ap.add_argument("--port", type=int, default=None, help=f"Port (default config ui.port or {DEFAULT_PORT})")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--background", action="store_true", help="Start detached unless already running")
    g.add_argument("--stop", action="store_true", help="Stop the detached server")
    g.add_argument("--status", action="store_true", help="Print whether the server is running")
    args = ap.parse_args()
    cfg = load_config()
    port = args.port or ui_cfg(cfg)["port"]
    if args.background:
        return start_background(cfg, port)
    if args.stop:
        return stop_background(cfg, port)
    if args.status:
        info = ping(port)
        print(f"Running (pid {info['pid']}): http://127.0.0.1:{port}/" if info else "Not running.")
        return 0 if info else 1
    return serve(cfg, port)


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>AI Video Renamer</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{--bg:#f5f5f7;--card:#fff;--ink:#1d1d1f;--mute:#6e6e73;--line:#e3e3e8;--acc:#0a66d8;--ok:#1f8f4e;--warn:#b26a00;--bad:#c62828}
@media (prefers-color-scheme:dark){:root{--bg:#161618;--card:#222226;--ink:#f2f2f5;--mute:#a1a1a8;--line:#36363c;--acc:#4c9bff;--ok:#43c47a;--warn:#f0a63a;--bad:#ff6b6b}}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 -apple-system,BlinkMacSystemFont,"Helvetica Neue",sans-serif;background:var(--bg);color:var(--ink)}
header{padding:14px 22px;display:flex;align-items:center;gap:14px;flex-wrap:wrap}h1{font-size:19px;margin:0}
.banner{background:#fff3cd;color:#5c4400;border:1px solid #f0d78c;border-radius:8px;padding:8px 14px;font-weight:600}
@media (prefers-color-scheme:dark){.banner{background:#3a3010;color:#ffe08a;border-color:#6b5a1e}}
.banner.live{background:#d8f3e3;color:#145c33;border-color:#8fd3a9}
@media (prefers-color-scheme:dark){.banner.live{background:#12351f;color:#8ff0b4;border-color:#2c6b44}}
.st-renamed{color:var(--ok);font-weight:600}.st-review{color:var(--warn);font-weight:700}.st-pending{color:var(--mute)}.st-proc{color:var(--acc);font-weight:600}
main{padding:0 22px 40px;display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(420px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}.wide{grid-column:1/-1}
h2{font-size:15px;margin:0 0 10px;display:flex;align-items:center;gap:8px}h2 .sp{flex:1}
button{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:7px;padding:5px 12px;cursor:pointer}
button.primary{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:600}button.danger{color:var(--bad);border-color:var(--bad)}
button:disabled{opacity:.45;cursor:not-allowed}
.kv{display:grid;grid-template-columns:120px 1fr;gap:4px 10px}.kv div:nth-child(odd){color:var(--mute)}
.badge{display:inline-block;border-radius:20px;padding:1px 10px;font-weight:600;font-size:12px;background:var(--line)}
.b-running,.b-starting{background:#d9ecff;color:#0a4a9c}.b-done{background:#d8f3e3;color:#145c33}.b-error,.b-done_with_errors,.b-blocked{background:#fde0e0;color:#8e1b1b}.b-stopped{background:#fff0d0;color:#6a4500}
bar,.bar{display:block;height:8px;border-radius:5px;background:var(--line);overflow:hidden}.bar>i{display:block;height:100%;background:var(--acc);width:0;transition:width .3s}
.bar.ok>i{background:var(--ok)}.bar.bad>i{background:var(--bad)}
#drop{border:2px dashed var(--line);border-radius:12px;padding:26px;text-align:center;color:var(--mute);cursor:pointer}
#drop.over{border-color:var(--acc);background:rgba(10,102,216,.07);color:var(--ink)}
.up{display:grid;grid-template-columns:1fr 210px;gap:2px 10px;padding:6px 0;border-bottom:1px solid var(--line)}.up .m{grid-column:1/-1;font-size:12px;color:var(--mute)}
.up .m.bad{color:var(--bad)}.up .m.ok{color:var(--ok)}.up .m.warn{color:var(--warn)}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mute);font-weight:600;font-size:12px;position:sticky;top:0;background:var(--card)}td.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.scroll{max-height:420px;overflow:auto}.mute{color:var(--mute)}.mono{font-family:ui-monospace,Menlo,monospace;font-size:12.5px;word-break:break-all}
.flag{color:var(--warn);font-weight:700}pre.md{white-space:pre-wrap;background:var(--bg);border-radius:8px;padding:10px;margin:4px 0;max-height:380px;overflow:auto;font-size:12.5px}
#toast{position:fixed;right:18px;bottom:18px;max-width:460px;padding:10px 14px;border-radius:9px;background:#333;color:#fff;display:none;white-space:pre-wrap;z-index:9}
#toast.bad{background:#a32020}#toast.ok{background:#1d7a44}
.folderbox{display:flex;flex-wrap:wrap;gap:6px 14px;align-items:center;margin:0 0 12px;padding:8px 10px;border:1px solid var(--line);border-radius:8px}
.folderbox label{display:inline-flex;align-items:center;gap:5px}.folderbox .hint{flex-basis:100%;font-size:12.5px}
input[type=text]{font:inherit;padding:4px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink);min-width:260px}
input[type=text]:disabled{opacity:.5}.err{color:var(--bad);font-weight:600}
tr.grp{cursor:pointer;background:rgba(127,127,127,.07)}tr.grp:hover{background:rgba(127,127,127,.14)}tr.grp td{font-weight:600}
td.sub{padding-left:28px}
.tbrow{display:flex;flex-wrap:wrap;gap:6px 12px;align-items:center;margin-bottom:8px}.tbrow select{font:inherit;padding:4px 6px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink);max-width:100%}
input[type=date]{font:inherit;padding:3px 6px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink)}
#tbResult{margin:6px 0}#tbResult a,#tbRecent a{color:var(--acc)}#tbRecent{font-size:12.5px}
.sg{border:1px solid var(--line);border-radius:9px;padding:8px 10px;margin:8px 0}.sg.off{opacity:.5}.sg h3{font-size:14px;margin:0 0 4px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.sg .tag{font-size:11px;border-radius:10px;padding:1px 8px;background:#d9ecff;color:#0a4a9c}.sg .tag.new{background:#d8f3e3;color:#145c33}.sg .fl{color:var(--warn);font-size:12.5px}
.sw{color:var(--warn);font-weight:600;margin:6px 0}
.insg{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:10px 16px}.insg label.t{display:block;font-weight:600;margin:0 0 3px}
.insg textarea{width:100%;box-sizing:border-box;font:inherit;font-size:13px;padding:6px 8px;border:1px solid var(--line);border-radius:7px;background:var(--bg);color:var(--ink);resize:vertical}
.cnt{font-size:11.5px;color:var(--mute);display:flex;gap:8px;align-items:center;margin-top:2px}.cnt.over{color:var(--bad);font-weight:600}.cnt .sp{flex:1}
.b-ins{background:#efe3ff;color:#5b2a9c}.b-off{background:var(--line);color:var(--mute)}
@media (prefers-color-scheme:dark){.b-ins{background:#3a2560;color:#d9c2ff}}
#insPrev{display:none}#insPrev pre{max-height:460px}.insbar{display:flex;flex-wrap:wrap;gap:8px 12px;align-items:center;margin-top:10px}
#upTotal{display:none;margin:10px 0 2px}#upTotal .m{font-size:12px;color:var(--mute);margin-bottom:3px}
</style></head><body>
<header><h1>🎬 AI Video Renamer</h1><div class="banner" id="banner">…</div><span class="mute" id="srv"></span></header>
<main>
<section class="card" id="statusCard"><h2>Review status <span class="sp"></span>
<button class="primary" id="startBtn" disabled>Start review</button> <button id="applyBtn" disabled>Apply proposed names</button> <button class="danger" id="stopBtn" style="display:none">Stop</button></h2>
<div class="folderbox" id="folderBox">
<label title="Confident clips are renamed straight into inbox/&lt;folder&gt;/ with their .json/.md sidecars; needs-review clips stay in inbox/"><input type="checkbox" id="fOn"> Put renamed clips in a folder</label>
<input type="text" id="fName" placeholder="Folder name, e.g. Vintage Collectibles Show" maxlength="80" disabled autocomplete="off" spellcheck="false">
<label class="mute" title="Off: the model guesses {project} per clip. On: every new filename uses the folder name as {project}"><input type="checkbox" id="fProj" disabled> Use folder name as the project in filenames</label>
<button id="moveBtn" disabled title="Move clips that were already renamed (and are still at the top of inbox/) into the folder">Move renamed clips into folder</button>
<div class="hint mute" id="fHint"></div>
</div>
<div class="folderbox" id="sideBox">
<label title="Off (default): each clip's notes (transcript, summary, keywords, proposed name) are kept only in the central store notes/clips/ on the Lexar — use Transcript bundle to export them. On: also write <clip>.json/.md beside every new clip, like before."><input type="checkbox" id="sideOn"> Write .json/.md notes next to each clip</label>
<span class="hint mute" id="sideHint"></span>
</div>
<div class="kv">
<div>State</div><div><span class="badge" id="sState">…</span> <span class="mute" id="sSource"></span></div>
<div>Current file</div><div class="mono" id="sFile">—</div>
<div>Step</div><div id="sStep">—</div>
<div>Frames</div><div><span id="sFrames">—</span><span class="bar" style="margin-top:4px"><i id="sFramesBar"></i></span></div>
<div>Clip</div><div id="sClip">—</div>
<div>Queue</div><div id="sQueue">—</div>
<div>ETA</div><div id="sEta">—</div>
<div>Message</div><div id="sMsg">—</div>
<div>Instructions</div><div><span class="badge b-off" id="sIns">off</span> <span class="mute" id="sInsTxt"></span></div>
<div>Run log</div><div class="mono mute" id="sLog">—</div>
</div></section>

<section class="card"><h2>Add clips &amp; photos to inbox</h2>
<div id="drop"><b>Drop videos, photos or a whole folder here</b><br>or click to choose files · mp4 mov mkv m4v avi mts webm · jpg png heic heif dng webp tif · copied into <span class="mono">inbox/</span><br>
<span style="font-size:12px">A dropped folder is searched (subfolders too); its videos and photos are copied <b>flat</b> into inbox/ — hidden and other files are skipped.</span>
<input type="file" id="pick" multiple accept="video/*,image/*,.mts,.MTS,.mkv,.heic,.HEIC,.heif,.HEIF,.dng,.DNG,.webp,.tif,.tiff" style="display:none"></div>
<div style="margin-top:6px"><button id="pickDirBtn">Choose a folder…</button><input type="file" id="pickDir" webkitdirectory multiple style="display:none"></div>
<div id="upTotal"><div class="m" id="upTotalTxt"></div><span class="bar"><i id="upTotalBar"></i></span></div>
<div id="uploads"></div></section>

<section class="card wide" id="insCard"><h2>Custom instructions <span class="mute">— guidance for the vision model &amp; Whisper</span><span class="sp"></span><span class="badge b-off" id="insBadge">off</span></h2>
<div class="insg">
<div><label class="t" for="insStanding">Standing instructions <span class="mute" style="font-weight:400">— every clip &amp; photo</span></label>
<textarea id="insStanding" rows="6" maxlength="2000" spellcheck="true" placeholder="e.g. I make retro-tech videos. Call the router &quot;Acme R3000&quot;. Prefer product names over generic words."></textarea>
<div class="cnt" id="insStandingCnt"></div></div>
<div><label class="t" for="insNext">Next run only <span class="mute" style="font-weight:400">— this batch (also used by Sort into projects)</span></label>
<textarea id="insNext" rows="4" maxlength="1000" spellcheck="true" placeholder="e.g. Project: Retro Game Expo&#10;This batch is the Retro Game Expo at the Convention Center."></textarea>
<div class="cnt" id="insNextCnt"></div>
<label class="mute" style="font-size:12.5px" title="Off: cleared automatically after the next live run. On: kept for later runs too (dry runs never clear it)"><input type="checkbox" id="insKeep"> Keep after the run</label></div>
<div><label class="t" for="insGloss">Glossary <span class="mute" style="font-weight:400">— names, products, places (one per line or comma-separated)</span></label>
<textarea id="insGloss" rows="6" spellcheck="false" placeholder="Commodore 64&#10;Acme&#10;Raspberry Pi&#10;Amiga 500"></textarea>
<div class="cnt" id="insGlossCnt"></div></div>
</div>
<div class="insbar"><button class="primary" id="insSave">Save</button> <button id="insRevert">Revert</button> <button id="insPrevBtn">Preview prompt</button>
<label class="mute"><input type="checkbox" id="insPhoto"> as a photo</label><span class="mute" id="insState"></span></div>
<div class="mute" style="font-size:12.5px;margin-top:6px">Your text goes into the vision prompt inside a clearly marked “user guidance” block, <b>before</b> the output format, which stays last — so it can steer names and spellings but can't change the JSON the renamer needs. The glossary is also Whisper's initial prompt (<span class="mono">--prompt</span>) for better spelling. Each clip's notes record which instructions were used (hash + text). Saved in <span class="mono">config/custom-instructions.json</span>.</div>
<div id="insPrev"><h3 style="font-size:13.5px;margin:12px 0 4px">Prompt preview <span class="mute" id="insPrevMeta" style="font-weight:400"></span></h3><pre class="md" id="insPrevTxt"></pre></div>
</section>

<section class="card wide" id="tbCard"><h2>Transcript bundle <span class="mute">— all spoken audio in one file, for a script-writing bot</span><span class="sp"></span><button id="tbReload">Refresh</button></h2>
<div class="tbrow"><label>Clips from <select id="tbScope"><option value="inbox">All of inbox/</option></select></label>
<label title="Optional: only clips recorded on this day">Recorded on <input type="date" id="tbDate"></label>
<label title="Off: clips without speech are only listed briefly at the end. On: full entries (summary, keywords, on-screen text) for them too"><input type="checkbox" id="tbSilent"> Full entries for clips with no speech</label>
<label title="Photos are listed with the silent assets (they have no speech). Tick to leave them out"><input type="checkbox" id="tbNoPhotos"> Leave out photos</label>
<button class="primary" id="tbBtn">Build transcript bundle</button></div>
<div class="mute" style="font-size:12.5px">Reads each clip's notes from the central store (plus any .json sidecars on disk; Whisper transcript + summary) and writes two files to <span class="mono">exports/</span>: <b>.json</b> for bots (every line has an id like <span class="mono">file.mp4#0003</span> plus timecodes, so a script line can cite its video) and <b>.md</b> to paste into a chat. Nothing is renamed or moved.</div>
<div id="tbResult"></div><div id="tbRecent" class="mute"></div></section>

<section class="card wide" id="sortCard"><h2>Sort into projects <span class="mute">— dry run first; nothing moves until you apply</span><span class="sp"></span><button id="sortHist">History</button> <button id="sortUndo">Undo last sort…</button></h2>
<div class="tbrow"><label>Source <select id="sortSrc"><option value="inbox">inbox/</option></select></label>
<label title="A new group starts after a gap this long between clips (or a day change)">Gap <input type="number" id="sortGap" min="5" max="1440" value="60" style="width:70px"> min</label>
<button class="primary" id="sortDry">Dry run</button> <button id="sortApply" disabled>Apply…</button></div>
<div class="mute" style="font-size:12.5px">Projects go to <span class="mono" id="sortRoot">DaVinci Resolve/</span>&lt;Project&gt;/ A-Roll, B-Roll, Images, _Notes, _Review. Resolve's own folders, Blackmagic Cloud-synced projects and folders on hold are never touched. Every apply writes an undo map (logs/sort-undo-*.jsonl).</div>
<div id="sortOut"></div></section>

<section class="card wide"><h2>Inbox <span class="mute" id="inboxSum"></span><span class="sp"></span><button id="inboxRefresh">Refresh</button></h2>
<div class="mute" id="inboxStates" style="margin:-4px 0 8px"></div>
<div class="scroll"><table><thead><tr><th>#</th><th>File</th><th>State</th><th style="text-align:right">Size</th><th>Modified</th></tr></thead><tbody id="inboxBody"></tbody></table></div></section>

<section class="card wide"><h2>Results (report) <span class="mute" id="resSum"></span><span class="sp"></span>
<label class="mute"><input type="checkbox" id="resAll"> every run</label> <button id="resRefresh">Refresh</button></h2>
<div class="scroll" style="max-height:560px"><table><thead><tr><th>Original</th><th>Proposed name</th><th>Type</th><th style="text-align:right">Conf.</th><th>Review</th><th>Action</th><th>When</th><th></th></tr></thead>
<tbody id="resBody"></tbody></table></div></section>
</main><div id="toast"></div>
<script>
"use strict";
const POLL_MS = __POLL_MS__, EXTS = __EXTS__;
const $ = id => document.getElementById(id);
const H = {"X-Renamer-UI": "1"}, JH = {"X-Renamer-UI": "1", "Content-Type": "application/json"};
const LS_FOLDER = "aiVideoRenamer.folderName", FOLDER_MAX = 80;
let lastStatus = null, lastClipKey = "", uploading = 0, queue = [], busy = false;

function el(tag, props, ...kids){const e=document.createElement(tag);if(props)for(const[k,v]of Object.entries(props)){if(k==="class")e.className=v;else if(k==="text")e.textContent=v;else e.setAttribute(k,v);}for(const k of kids)if(k!=null)e.append(k);return e;}
function fmtBytes(n){if(n==null)return"—";const u=["B","KB","MB","GB","TB"];let i=0;while(n>=1000&&i<u.length-1){n/=1000;i++;}return n.toFixed(i<2?0:(n<10?2:1))+" "+u[i];}
function fmtDur(s){if(s==null||isNaN(s))return"—";s=Math.max(0,Math.round(s));const h=Math.floor(s/3600),m=Math.floor(s%3600/60);return h?`${h} h ${m} m`:(m?`${m} m ${s%60} s`:`${s} s`);}
function fmtTime(iso){if(!iso)return"—";const d=new Date(iso);if(isNaN(d))return iso;const t=d.toLocaleTimeString([], {hour:"numeric",minute:"2-digit"});return d.toDateString()===new Date().toDateString()?t:d.toLocaleDateString([], {month:"short",day:"numeric"})+" "+t;}
function base(p){return p?String(p).split("/").pop():"—";}
function toast(msg, kind){const t=$("toast");t.textContent=msg;t.className=kind||"";t.style.display="block";clearTimeout(toast.h);toast.h=setTimeout(()=>t.style.display="none",kind==="bad"?9000:5000);}
async function api(path, opts){const r=await fetch(path,opts);let j;try{j=await r.json();}catch(e){j={error:`HTTP ${r.status}`};}return [r,j];}

// ---------------- status
async function pollStatus(){
  try{
    const [r,j]=await api("/api/status");if(!r.ok)throw new Error(j.error);
    renderStatus(j);
  }catch(e){$("sState").textContent="server unreachable";$("sState").className="badge b-error";}
}
function renderStatus(j){
  const s=j.status||{}, run=j.run||{};lastStatus=j;
  let state=s.state||"idle";
  if(!run.active&&(state==="running"||state==="starting"))state="interrupted";
  $("sState").textContent=run.active&&state!=="running"&&state!=="starting"?"running":state;
  $("sState").className="badge b-"+(run.active?"running":state);
  const lk=run.lock;
  $("sSource").textContent=run.active?(lk?`started from ${lk.launched_by==="ui"?"this UI":"Terminal"} · pid ${lk.pid} · ${fmtTime(lk.started_at)}`:`Terminal run without lock (pid ${(run.unlocked_processes[0]||{}).pid})`):"no run active";
  $("sFile").textContent=s.current_file?base(s.current_file):"—";$("sFile").title=s.current_file||"";
  $("sStep").textContent=s.step||"—";
  const fd=s.frames_done||0, ft=s.frames_total||9;
  $("sFrames").textContent=`${fd}/${ft}`;$("sFramesBar").style.width=(ft?100*fd/ft:0)+"%";
  $("sClip").textContent=s.clip_index?`${s.clip_index} of ${s.clip_total}`:"—";
  const ql=(s.queue||[]).length;$("sQueue").textContent=run.active||ql?`${ql} waiting after this clip`:"—";
  if(run.active&&s.eta_seconds!=null){const age=(Date.now()-new Date(s.updated_at))/1000;$("sEta").textContent=`≈ ${fmtDur(s.eta_seconds-Math.max(0,age))} left · done ≈ ${fmtTime(s.eta)}`;}
  else $("sEta").textContent="—";
  $("sMsg").textContent=(s.message||"—")+(s.last_error&&s.last_error!==s.message?`  ⚠ ${s.last_error}`:"");
  $("sLog").textContent=(lk&&lk.log)||"—";
  renderInsBadge(j.instructions||{});
  const live=!j.dry_run;window._live=live;
  $("banner").textContent=live?"Live — clips are renamed in place in inbox/ (new name = processed)":"Dry run — nothing will be renamed.";
  $("banner").className="banner"+(live?" live":"");
  $("startBtn").textContent=live?"Start (live)":"Start review";
  $("startBtn").disabled=run.active||busy;
  $("startBtn").title=run.active?"A run is already active":(live?"Runs run_pipeline.py --all in the background; confident clips are renamed in place as they finish":"Runs run_pipeline.py --all in the background (dry run)");
  $("applyBtn").disabled=run.active||busy||!live;
  $("applyBtn").title=!live?"Dry run: set \"dry_run\": false in config/config.json to enable renaming":(run.active?"Refused while a pipeline run is active":"Rename clips already processed to their proposed names (in place, inbox/)");
  $("stopBtn").style.display=run.can_stop?"":"none";
  updateMoveBtn();
  if(!sideBusy){$("sideOn").checked=!!j.write_next_to_clip;
    $("sideHint").textContent=j.write_next_to_clip?"On — new clips get .json/.md files beside them (plus the central notes store).":"Off — notes only in the central store (notes/clips/); the Lexar folders stay video-only.";}
  $("srv").textContent=`updated ${fmtTime(s.updated_at)}`;
  const key=`${s.state}|${s.clip_index}|${s.step==="report"}`;
  if(key!==lastClipKey){lastClipKey=key;loadResults();if(!run.active)loadInbox();}
}
$("startBtn").onclick=async()=>{
  const n=(window._inbox||{}).video_count||0;
  const ib=window._inbox||{}, p=ib.pending_count!=null?ib.pending_count:n, ap=ib.applicable_count||0;
  const fo=folderOpts();if(fo.error){folderHint(fo.error,true);toast(fo.error,"bad");return;}
  const msg=window._live
    ?`Start a LIVE run over ${p} unprocessed clip(s) in inbox/?\n\nConfident clips are renamed in place to their proposed names as soon as each finishes; low-confidence ones keep their name (needs review). Already renamed / needs-review clips are skipped.`+(ap?`\n\n${ap} clip(s) already have dry-run proposals — click "Apply proposed names" first to use them instead of reprocessing.`:"")+`\n\nAbout 2 min per clip (≈ ${fmtDur(p*120)}). Undo: python3 scripts/undo_renames.py`+folderMsg(fo)
    :`Start a dry-run review of ${p} clip(s) in inbox/?\n\nAbout 2 min per clip on this Air (≈ ${fmtDur(p*120)}). It keeps running if you close the browser. Nothing is renamed.`+(fo.use_folder?"\n\n(Dry run: the folder option has no effect — nothing is moved.)":"");
  if(!confirm(msg))return;
  busy=true;$("startBtn").disabled=true;
  try{const [r,j]=await api("/api/start",{method:"POST",headers:JH,body:JSON.stringify(fo.body)});
    if(r.ok){toast(j.message||"Started","ok");rememberFolder(fo);}else toast((j.error||"Refused")+(j.detail?"\n\n"+j.detail.slice(-600):""),"bad");
  }catch(e){toast("Start failed: "+e,"bad");}
  busy=false;pollStatus();
};
$("applyBtn").onclick=async()=>{
  const ap=(window._inbox||{}).applicable_count||0;
  const fo=folderOpts();if(fo.error){folderHint(fo.error,true);toast(fo.error,"bad");return;}
  const where=fo.use_folder?`renamed into inbox/${fo.name}/`:"renamed in place in inbox/";
  if(!confirm(`Apply proposed names now?\n\n${ap} processed clip(s) are waiting. Confident ones are ${where} (with their .json/.md sidecars); needs-review ones keep their original name in inbox/. Nothing is overwritten (_t02… on a clash).`+(fo.project_from_folder?"\n\n(“Use folder name as the project” only affects new runs — Apply keeps the names already proposed.)":"")+`\n\nUndo: python3 scripts/undo_renames.py --dry-run`))return;
  busy=true;$("applyBtn").disabled=true;
  try{const [r,j]=await api("/api/apply",{method:"POST",headers:JH,body:JSON.stringify(fo.body)});toast(r.ok?(j.message||"Applied"):(j.error||"Refused"),r.ok?"ok":"bad");if(r.ok)rememberFolder(fo);}
  catch(e){toast("Apply failed: "+e,"bad");}
  busy=false;loadInbox();loadResults();pollStatus();
};
let sideBusy=false;
$("sideOn").onchange=async()=>{
  const v=$("sideOn").checked;sideBusy=true;
  try{const [r,j]=await api("/api/settings",{method:"POST",headers:JH,body:JSON.stringify({write_next_to_clip:v})});
    toast(r.ok?j.message:(j.error||"Could not save"),r.ok?"ok":"bad");if(!r.ok)$("sideOn").checked=!v;}
  catch(e){toast("Could not save: "+e,"bad");$("sideOn").checked=!v;}
  sideBusy=false;pollStatus();
};
$("stopBtn").onclick=async()=>{
  if(!confirm("Stop the running review? The clip in progress is abandoned; finished clips stay in the report."))return;
  const [r,j]=await api("/api/stop",{method:"POST",headers:H});toast(r.ok?j.message:j.error,r.ok?"ok":"bad");setTimeout(pollStatus,800);
};

// ---------------- batch folder
let fTimer=null, fState={};
const openFolders=new Set();
function folderErr(v){
  if(!v)return "Enter a folder name (or untick “Put renamed clips in a folder”).";
  if(/[\/\\]/.test(v))return "No slashes in the folder name — it is one folder directly in inbox/.";
  if(v.includes(".."))return "No “..” in the folder name.";
  if(v.startsWith("."))return "The folder name can't start with a dot (it would be hidden in Finder).";
  if(v.length>FOLDER_MAX)return `Folder name is too long (${v.length} characters, max ${FOLDER_MAX}).`;
  return "";
}
function folderHint(t,bad){const h=$("fHint");h.textContent=t||"";h.className="hint "+(bad?"err":"mute");}
function folderOpts(){
  const on=$("fOn").checked, v=$("fName").value.trim(), proj=on&&$("fProj").checked;
  if(!on)return {use_folder:false,body:{use_folder:false}};
  const fresh=fState.requested===v;
  const err=folderErr(v)||(fresh&&fState.error)||"";
  if(err)return {error:err};
  if(proj&&fresh&&fState.ok&&!fState.project_slug)return {error:"That folder name has no letters or digits to use as the project in filenames."};
  return {use_folder:true,name:fresh&&fState.ok?fState.name:v,project_from_folder:proj,body:{use_folder:true,folder:v,project_from_folder:proj}};
}
function folderMsg(fo){
  if(!fo.use_folder)return "";
  return `\n\nFOLDER: renamed clips go into inbox/${fo.name}/ with their .json/.md sidecars`+(fState.ok&&fState.exists?` (folder exists, ${fState.video_count} clip(s) inside; nothing is overwritten — _t02… on a clash)`:" (created on the first rename)")+
    ". Needs-review / failed clips stay in inbox/ with their original names."+(fo.project_from_folder&&fState.project_slug?`\nProject in every new filename: ${fState.project_slug}`:"");
}
function rememberFolder(fo){if(fo&&fo.use_folder){try{localStorage.setItem(LS_FOLDER,fo.name);}catch(e){}}}
function renderFolderHint(){
  if(!$("fOn").checked){folderHint("");return;}
  const v=$("fName").value.trim(),e=folderErr(v);
  if(e){folderHint(e,true);return;}
  if(fState.requested!==v)return;
  if(fState.error){folderHint(fState.error,true);return;}
  let t=`→ inbox/${fState.name}/`+(fState.exists?` (exists · ${fState.video_count} clip(s) inside — new clips are added, nothing is overwritten)`:" (created on the first rename)");
  if(fState.changed)t+=` · will be saved as “${fState.name}”`;
  const proj=$("fProj").checked;
  if(proj)t+=fState.project_slug?` · project in filenames: ${fState.project_slug}`:" · no letters/digits to use as the project";
  const mv=(window._inbox||{}).movable_count||0;
  if(mv)t+=` · ${mv} renamed clip(s) at the top of inbox/ can be moved in`;
  folderHint(t,proj&&!fState.project_slug);
}
function checkFolder(){
  clearTimeout(fTimer);updateMoveBtn();
  if(!$("fOn").checked){folderHint("");return;}
  const v=$("fName").value.trim(),e=folderErr(v);
  if(e){fState={requested:v,error:e};folderHint(e,true);return;}
  fTimer=setTimeout(async()=>{
    let st;
    try{const [r,j]=await api("/api/folder-check?name="+encodeURIComponent(v));st=r.ok?j:{error:j.error||"invalid folder name"};}
    catch(err){st={error:"could not check the folder name: "+err};}
    st.requested=v;
    if($("fName").value.trim()!==v)return;  // typed on meanwhile
    fState=st;renderFolderHint();updateMoveBtn();
  },250);
}
function updateMoveBtn(){
  const run=(lastStatus||{}).run||{}, mv=(window._inbox||{}).movable_count||0, on=$("fOn").checked, b=$("moveBtn");
  b.textContent=mv?`Move ${mv} renamed clip(s) into folder`:"Move renamed clips into folder";
  b.disabled=!!run.active||busy||!window._live||!on||!mv;
  b.title=!window._live?"Dry run: moving files is off":(run.active?"Refused while a pipeline run is active":(!on?"Tick “Put renamed clips in a folder” and enter the folder name first":
    (!mv?"No renamed clips at the top of inbox/ (per the rename log)":`Move ${mv} renamed clip(s) and their sidecars from the top of inbox/ into the folder (logged; undo_renames.py reverses it)`)));
}
try{$("fName").value=localStorage.getItem(LS_FOLDER)||"";}catch(e){}
$("fOn").onchange=()=>{const on=$("fOn").checked;$("fName").disabled=!on;$("fProj").disabled=!on;if(on&&!$("fName").value.trim())$("fName").focus();checkFolder();};
$("fName").oninput=checkFolder;$("fProj").onchange=renderFolderHint;
$("moveBtn").onclick=async()=>{
  const fo=folderOpts();
  if(fo.error||!fo.use_folder){const e=fo.error||"Tick “Put renamed clips in a folder” and enter the folder name.";folderHint(e,true);toast(e,"bad");return;}
  const mv=(window._inbox||{}).movable_count||0;
  if(!confirm(`Move ${mv} renamed clip(s) from the top of inbox/ into inbox/${fo.name}/?\n\nTheir .json/.md sidecars move with them. Needs-review clips and files that aren't in the rename log stay where they are. Nothing is overwritten (_t02… on a clash).\n\nUndo: python3 scripts/undo_renames.py --dry-run (then without --dry-run)`))return;
  busy=true;updateMoveBtn();
  try{const [r,j]=await api("/api/move-into-folder",{method:"POST",headers:JH,body:JSON.stringify({folder:fo.body.folder})});
    toast(r.ok?(j.message||"Moved"):(j.error||"Refused"),r.ok?"ok":"bad");if(r.ok){rememberFolder(fo);openFolders.add(j.folder||fo.name);}}
  catch(e){toast("Move failed: "+e,"bad");}
  busy=false;await loadInbox();loadResults();pollStatus();checkFolder();
};

// ---------------- inbox
function stateOf(f,cur){
  let t="—",c="mute",ti="";
  if(f.video){
    if(cur&&f.name===cur){t="⏳ processing";c="st-proc";}
    else if(f.state==="renamed"){t="✓ renamed"+(f.original?` (was ${f.original})`:"");c="st-renamed";ti=f.detail||"";}
    else if(f.state==="needs review"){t="⚑ needs review";c="st-review";ti=f.detail||"";}
    else if(f.state==="not processed"){t="not processed (inside a folder)";c="st-pending";ti=f.detail||"";}
    else{t="pending"+(f.proposed?` → ${f.proposed}`+(f.proposed_needs_review?" (⚑ review)":""):"");c="st-pending";ti=f.detail||"";}
  }
  return [t,c,ti];
}
function fileRow(f,num,cur,sub){
  const [t,c,ti]=stateOf(f,cur);
  return el("tr",null,el("td",{class:"n mute",text:num}),el("td",{class:"mono"+(f.video?"":" mute")+(sub?" sub":""),text:f.name+(f.video||sub?"":"  (not a video or photo — will be skipped)")}),
    el("td",{class:c,title:ti,text:t}),el("td",{class:"n",text:fmtBytes(f.size)}),el("td",{class:"mute",text:fmtTime(f.mtime)}));
}
function renderInbox(j){
  const tb=$("inboxBody");tb.replaceChildren();
  const st=(lastStatus||{}).status||{}, act=((lastStatus||{}).run||{}).active, cur=act&&st.current_file?base(st.current_file):null;
  const folders=j.folders||[];
  for(const g of folders){
    const open=openFolders.has(g.name), s=g.states||{}, parts=[`${g.video_count} clip(s)`];
    for(const [k,lab] of [["renamed","renamed"],["needs review","need review"],["not processed","not processed"]])if(s[k])parts.push(`${s[k]} ${lab}`);
    if(g.count>g.video_count)parts.push(`${g.count-g.video_count} other file(s)`);
    if(g.subfolders)parts.push(`${g.subfolders} subfolder(s) not listed`);
    if(g.error)parts.push("⚠ "+g.error);
    const cls=s["needs review"]?"st-review":(g.video_count&&s.renamed===g.video_count?"st-renamed":"mute");
    const hdr=el("tr",{class:"grp",title:open?"Click to collapse":"Click to show the files in this folder"},el("td",{class:"n mute",text:open?"▾":"▸"}),
      el("td",{class:"mono",text:`📁 ${g.name}/`}),el("td",{class:cls,text:parts.join(" · ")}),el("td",{class:"n",text:fmtBytes(g.total_bytes)}),el("td",{class:"mute",text:fmtTime(g.mtime)}));
    hdr.onclick=()=>{if(openFolders.has(g.name))openFolders.delete(g.name);else openFolders.add(g.name);renderInbox(window._inbox);};
    tb.append(hdr);
    if(open){g.files.forEach((f,i)=>tb.append(fileRow(f,i+1,null,true)));if(!g.files.length)tb.append(el("tr",null,el("td"),el("td",{colspan:4,class:"mute sub",text:"(empty)"})));}
  }
  j.files.forEach((f,i)=>tb.append(fileRow(f,i+1,cur,false)));
  if(!j.files.length&&!folders.length)tb.append(el("tr",null,el("td",{colspan:5,class:"mute",text:"inbox/ is empty"})));
  const s=j.states||{};
  const fclips=folders.reduce((a,g)=>a+(g.video_count||0),0);
  $("inboxStates").textContent=`${j.video_count} video(s)/photo(s) at the top of inbox/: ${s.pending||0} pending · ${s.renamed||0} renamed · ${s["needs review"]||0} needs review`+(j.applicable_count?` · ${j.applicable_count} processed, waiting for Apply`:"")+
    (folders.length?` · ${folders.length} folder(s) with ${fclips} clip(s) (click to expand; never processed by a run)`:"");
  let sum=`${j.count} file(s) · ${fmtBytes(j.total_bytes)} · ≈ ${fmtDur((j.pending_count!=null?j.pending_count:j.video_count)*120)} for pending at ~2 min/clip · ${fmtBytes(j.free_bytes)} free`;
  if(j.uploading.length)sum+=` · ${j.uploading.length} upload(s) in progress`;$("inboxSum").textContent=sum;
}
async function loadInbox(){
  try{const [r,j]=await api("/api/inbox");if(!r.ok)throw new Error(j.error);window._inbox=j;renderInbox(j);}
  catch(e){$("inboxSum").textContent="could not list inbox: "+e;}
  updateMoveBtn();renderFolderHint();
}
$("inboxRefresh").onclick=loadInbox;

// ---------------- results
async function loadResults(){
  try{const [r,j]=await api("/api/results?limit=300"+($("resAll").checked?"&all=1":""));if(!r.ok)throw new Error(j.error);
    const tb=$("resBody");tb.replaceChildren();
    for(const it of j.items){
      const conf=it.confidence==null?"—":Number(it.confidence).toFixed(2);
      const btn=(it.sidecar_json||it.note_id)?el("button",{text:"Notes"}):null;
      const tr=el("tr",null,el("td",{class:"mono",title:it.source,text:it.name}),el("td",{class:"mono",text:it.proposed||"(none)"}),el("td",{text:it.clip_type||"—"}),
        el("td",{class:"n",text:conf}),el("td",{class:it.needs_review?"flag":"mute",title:(it.review_reasons||[]).join("; "),text:it.needs_review?"⚑ needs review":"ok"}),
        el("td",{class:/^renamed/.test(it.state||"")?"st-renamed":(/review/.test(it.state||"")?"st-review":"mute"),title:it.current||"",text:it.state||"—"}),
        el("td",{class:"mute",text:fmtTime(it.time)}),el("td",null,btn));
      tb.append(tr);
      if(btn)btn.onclick=async()=>{
        if(tr.nextSibling&&tr.nextSibling.classList&&tr.nextSibling.classList.contains("mdrow")){tr.nextSibling.remove();btn.textContent="Notes";return;}
        const r=await fetch(it.sidecar_json?"/api/sidecar?json="+encodeURIComponent(it.sidecar_json):"/api/note?id="+encodeURIComponent(it.note_id));const t=await r.text();
        const row=el("tr",{class:"mdrow"},el("td",{colspan:8},el("pre",{class:"md",text:t})));tr.after(row);btn.textContent="Hide";
      };
    }
    if(!j.items.length)tb.append(el("tr",null,el("td",{colspan:8,class:"mute",text:"No results yet."})));
    $("resSum").textContent=`${j.count} shown · newest first`+($("resAll").checked?"":" · latest per clip");
  }catch(e){$("resSum").textContent="could not load results: "+e;}
}
$("resRefresh").onclick=loadResults;$("resAll").onchange=loadResults;

// ---------------- transcript bundle
async function loadScopes(){
  try{const [r,j]=await api("/api/transcript-scopes");if(!r.ok)throw new Error(j.error);
    const sel=$("tbScope"),keep=sel.value;sel.replaceChildren();
    for(const s of j.scopes)sel.append(el("option",{value:s.value,text:s.label}));
    if([...sel.options].some(o=>o.value===keep))sel.value=keep;
    else{const best=j.scopes.find(s=>s.count>0);if(best)sel.value=best.value;}
    renderRecent(j.exports||[]);
  }catch(e){$("tbRecent").textContent="could not load scopes: "+e;}
}
function dl(url,name,label){return el("a",{href:url,download:name,text:label||name});}
function renderRecent(list){
  const box=$("tbRecent");box.replaceChildren();if(!list.length)return;
  box.append("Recent exports: ");
  list.slice(0,8).forEach((x,i)=>{if(i)box.append(" · ");box.append(dl(x.url,x.name),` (${fmtBytes(x.size)}, ${fmtTime(x.mtime)})`);});
}
$("tbBtn").onclick=async()=>{
  const body={scope:$("tbScope").value,include_silent:$("tbSilent").checked,exclude_photos:$("tbNoPhotos").checked,date:$("tbDate").value||null};
  const btn=$("tbBtn"),res=$("tbResult");btn.disabled=true;res.className="mute";res.textContent="Building…";
  try{const [r,j]=await api("/api/transcript-bundle",{method:"POST",headers:JH,body:JSON.stringify(body)});
    if(!r.ok){res.className="err";res.textContent=j.error||"Failed";toast(j.error||"Failed","bad");}
    else{const c=j.counts||{};res.className="";res.replaceChildren(
      el("div",null,`✓ ${c.clips_with_speech||0} clip(s) with speech of ${c.clips_scanned||0} · ${j.speech_minutes} min of speech · ${c.segments||0} lines`+(c.photos?` · ${c.photos} photo(s)`:"")+(c.skipped_sidecars?` · ${c.skipped_sidecars} unreadable note(s) skipped`:"")),
      el("div",null,"Download: ",j.json_url?dl(j.json_url,j.json_name,`${j.json_name} (${fmtBytes(j.json_bytes)})`):"—"," · ",
        j.md_url?dl(j.md_url,j.md_name,`${j.md_name} (${fmtBytes(j.md_bytes)})`):"—"," · ",
        j.md_url?el("a",{href:j.md_url+"?view=1",target:"_blank",rel:"noopener",text:"view .md"}):""),
      j.note?el("div",{class:"mute",text:j.note}):null);
      toast("Transcript bundle ready","ok");loadScopes();}
  }catch(e){res.className="err";res.textContent="Failed: "+e;}
  btn.disabled=false;
};
$("tbReload").onclick=loadScopes;

// ---------------- uploads
const drop=$("drop"),pick=$("pick");
drop.onclick=()=>pick.click();
pick.onchange=()=>{addFiles([...pick.files]);pick.value="";};
["dragenter","dragover"].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.add("over");}));
["dragleave","drop"].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.remove("over");}));
window.addEventListener("dragover",e=>e.preventDefault());window.addEventListener("drop",e=>e.preventDefault());
drop.addEventListener("drop",e=>{
  // entries must be taken synchronously inside the event (the DataTransfer is emptied afterwards)
  const files=[],dirs=[],dt=e.dataTransfer,items=dt.items;
  if(items&&items.length&&typeof items[0].webkitGetAsEntry==="function"){
    for(let i=0;i<items.length;i++){const it=items[i];if(it.kind!=="file")continue;const ent=it.webkitGetAsEntry();
      if(ent&&ent.isDirectory)dirs.push(ent);else{const f=it.getAsFile();if(f)files.push(f);}}
  }else files.push(...dt.files);
  addFiles(files);
  if(dirs.length)addFolders(dirs);
});
$("pickDirBtn").onclick=()=>$("pickDir").click();
$("pickDir").onchange=()=>{
  const all=[...$("pickDir").files];$("pickDir").value="";
  const acc={files:[],hidden:0,other:0},top=all.length?String(all[0].webkitRelativePath||"").split("/")[0]:"";
  for(const f of all){const rel=f.webkitRelativePath||f.name;
    if(rel.split("/").some(p=>p.startsWith("."))){acc.hidden++;continue;}
    if(!isVideo(f.name)){acc.other++;continue;}
    f._rel=rel;acc.files.push(f);}
  queueFolder(top||"folder",acc);
};
function readAllEntries(dir){return new Promise((resolve,reject)=>{const rd=dir.createReader(),out=[];
  const next=()=>rd.readEntries(b=>{if(!b.length)resolve(out);else{out.push(...b);next();}},reject);next();});}
function entryFile(ent){return new Promise((resolve,reject)=>ent.file(resolve,reject));}
async function walkDir(dir,path,acc){
  for(const ent of await readAllEntries(dir)){
    if(ent.name.startsWith(".")){acc.hidden++;continue;}  // .DS_Store, ._ AppleDouble, hidden folders
    const rel=path+"/"+ent.name;
    if(ent.isDirectory){await walkDir(ent,rel,acc);continue;}
    if(!isVideo(ent.name)){acc.other++;continue;}
    const f=await entryFile(ent);f._rel=rel;acc.files.push(f);
  }
}
async function addFolders(dirs){
  for(const d of dirs){
    if(d.name.startsWith(".")){toast(`Skipped hidden folder ${d.name}`,"bad");continue;}
    const acc={files:[],hidden:0,other:0};
    try{await walkDir(d,d.name,acc);}catch(err){toast(`Could not read folder “${d.name}”: ${(err&&err.message)||err}`,"bad");continue;}
    queueFolder(d.name,acc);
  }
}
function queueFolder(name,acc){
  acc.files.sort((a,b)=>a._rel.localeCompare(b._rel));
  const skip=[acc.other?`${acc.other} other`:"",acc.hidden?`${acc.hidden} hidden`:""].filter(Boolean).join(" + ");
  toast(`Folder “${name}”: ${acc.files.length} video/photo file(s) → copied flat into inbox/`+(skip?` · skipped ${skip} item(s)`:""),acc.files.length?"ok":"bad");
  addFiles(acc.files,{folder:name});
}
function isVideo(name){const m=/\.[^.]+$/.exec(name);return !!m&&EXTS.includes(m[0].toLowerCase());}
const tot={files:0,done:0,bytes:0,doneBytes:0,cur:0,failed:0,dups:0};
function renderTotal(){
  const box=$("upTotal");if(!tot.files){box.style.display="none";return;}
  box.style.display="block";
  const active=uploading||queue.length, b=tot.doneBytes+tot.cur, p=tot.bytes?b/tot.bytes:(tot.done/tot.files);
  $("upTotalTxt").textContent=(active?`Total: ${tot.done} of ${tot.files} file(s) done`:`Finished: ${tot.files} file(s)`)+` · ${fmtBytes(b)} of ${fmtBytes(tot.bytes)} (${(100*p).toFixed(1)}%)`+
    (tot.dups?` · ${tot.dups} duplicate(s) skipped`:"")+(tot.failed?` · ${tot.failed} failed`:"");
  $("upTotalBar").style.width=(100*p).toFixed(1)+"%";
  $("upTotalBar").parentNode.className="bar"+(active?"":(tot.failed?" bad":" ok"));
}
function addFiles(files,opts){
  opts=opts||{};
  if(!files.length)return;
  let force=false;
  if(!opts.folder){  // loose files: ask about non-videos (folder drops are already filtered)
    const odd=files.filter(f=>!isVideo(f.name));
    if(odd.length){force=confirm(`These don't look like video or photo files (${EXTS.join(" ")}) and the pipeline will skip them:\n\n${odd.map(f=>f.name).join("\n")}\n\nOK = upload them anyway · Cancel = skip them`);}
  }
  if(!uploading&&!queue.length)Object.assign(tot,{files:0,done:0,bytes:0,doneBytes:0,cur:0,failed:0,dups:0});  // new session
  for(const f of files){
    if(!isVideo(f.name)&&!force)continue;
    const bar=el("i"),msg=el("div",{class:"m",text:"queued"}),pct=el("span",{class:"bar"},bar);
    const label=(f._rel&&f._rel!==f.name?`${f._rel} → inbox/${f.name}`:f.name)+` · ${fmtBytes(f.size)}`;
    $("uploads").prepend(el("div",{class:"up"},el("div",{class:"mono",text:label}),pct,msg));
    queue.push({f,bar,pct,msg,force:!isVideo(f.name)});
    tot.files++;tot.bytes+=f.size;
  }
  renderTotal();pump();
}
async function pump(){
  if(uploading||!queue.length)return;
  const job=queue.shift();uploading++;
  try{if(await uploadOne(job)==="dup")tot.dups++;}
  catch(e){job.msg.textContent="failed: "+e;job.msg.className="m bad";job.pct.className="bar bad";tot.failed++;}
  tot.done++;tot.doneBytes+=job.f.size;tot.cur=0;
  uploading--;renderTotal();loadInbox();pump();
}
async function uploadOne({f,bar,pct,msg,force}){
  const [cr,chk]=await api(`/api/upload-check?name=${encodeURIComponent(f.name)}&size=${f.size}`);
  if(!cr.ok)throw new Error(chk.error);
  if(chk.action==="duplicate"){msg.textContent=chk.message;msg.className="m warn";bar.style.width="100%";pct.className="bar";return "dup";}
  if(chk.message){msg.textContent=chk.message;msg.className="m warn";}
  return await new Promise((resolve,reject)=>{
    const x=new XMLHttpRequest();const t0=Date.now();
    x.open("PUT",`/api/upload?name=${encodeURIComponent(f.name)}&size=${f.size}&mtime=${f.lastModified||""}`+(force?"&force=1":""));
    x.setRequestHeader("X-Renamer-UI","1");x.setRequestHeader("Content-Type","application/octet-stream");
    x.upload.onprogress=e=>{if(!e.lengthComputable)return;const p=e.loaded/e.total;bar.style.width=(100*p).toFixed(1)+"%";
      const sp=e.loaded/Math.max(.001,(Date.now()-t0)/1000);msg.textContent=`${(100*p).toFixed(1)}% · ${fmtBytes(e.loaded)} of ${fmtBytes(e.total)} · ${fmtBytes(sp)}/s · ${fmtDur((e.total-e.loaded)/Math.max(1,sp))} left`;msg.className="m";
      tot.cur=Math.min(e.loaded,f.size);renderTotal();};
    x.upload.onload=()=>{msg.textContent="finishing (flushing to disk)…";};
    x.onload=()=>{let j={};try{j=JSON.parse(x.responseText);}catch(e){}
      if(x.status===201){bar.style.width="100%";pct.className="bar ok";msg.textContent="✓ "+(j.message||"saved")+(j.renamed?` (saved as ${j.name})`:"");msg.className="m ok";resolve();}
      else if(x.status===409){pct.className="bar";msg.textContent=j.error||"duplicate";msg.className="m warn";resolve("dup");}
      else reject(new Error(j.error||`HTTP ${x.status}`));};
    x.onerror=()=>reject(new Error("network error (server stopped?) — partial file discarded"));
    x.send(f);
  });
}
window.addEventListener("beforeunload",e=>{if(uploading||queue.length){e.preventDefault();e.returnValue="";}});

// ---- Sort into projects (v0.4)
let sortPlan=null;
async function loadSortSources(){
  try{const [r,j]=await api("/api/sort/sources");if(!r.ok)throw new Error(j.error);const s=$("sortSrc"),cur=s.value;s.innerHTML="";
    $("sortRoot").textContent=(j.resolve_root||"DaVinci Resolve")+"/";
    for(const x of j.sources){const o=el("option",{value:x.kind==="inbox"?"inbox":x.path,text:(x.sortable?"":"⛔ ")+x.name+" — "+x.note});if(!x.sortable)o.disabled=true;s.append(o);}
    if(cur)s.value=cur;}catch(e){$("sortOut").textContent="could not list sources: "+e;}
}
function renderSortPlan(p){
  const o=$("sortOut");o.innerHTML="";sortPlan=p;
  for(const w of p.warnings||[])o.append(el("div",{class:"sw",text:"⚠ "+w}));
  for(const g of p.groups){
    const box=el("div",{class:"sg"}),inc=el("input",{type:"checkbox"});inc.checked=true;inc.dataset.g=g.id;inc.className="sInc";
    const nm=el("input",{type:"text",value:g.project,maxlength:"80"});nm.dataset.g=g.id;nm.className="sName";
    const mg=el("select",{});mg.dataset.g=g.id;mg.className="sMerge";mg.append(el("option",{value:"",text:"new project"}));
    for(const e of p.existing_projects||[]){const op=el("option",{value:e,text:"merge into “"+e+"”"});if(e===g.merge_into)op.selected=true;mg.append(op);}
    inc.onchange=()=>box.classList.toggle("off",!inc.checked);
    const tag=el("span",{class:"tag"+(g.existing?"":" new"),text:g.existing?"EXISTING":"NEW"});
    const ex=new Set((p.existing_projects||[]).map(x=>x.toLowerCase()));
    const upd=()=>{const e=!!mg.value||ex.has(nm.value.trim().toLowerCase())||p.source_kind==="project";nm.disabled=!!mg.value;tag.textContent=e?"EXISTING":"NEW";tag.className="tag"+(e?"":" new");};
    mg.onchange=upd;nm.oninput=upd;upd();
    box.append(el("h3",{},inc,nm,mg,tag,
      el("span",{class:"mute",text:`${g.clip_count} clip(s), ${g.photo_count} photo(s) · ${(g.time_start||"").replace("T"," ").slice(0,16)}–${(g.time_end||"").slice(11,16)}`})));
    box.append(el("div",{text:g.description||""}),el("div",{class:"mono mute",text:"→ "+g.dest}),
      el("div",{class:"mute",text:Object.entries(g.bucket_counts||{}).map(([k,v])=>k+": "+v).join(", ")}));
    for(const f of g.flags||[])box.append(el("div",{class:"fl",text:"⚑ "+f}));
    if(g.split_suggestion){const sp=el("input",{type:"checkbox"});sp.dataset.g=g.id;sp.className="sSplit";
      box.append(el("label",{class:"fl"},sp," Split into: "+g.split_suggestion.parts.map(x=>x.project+" ("+x.names.length+")").join(" + ")));}
    o.append(box);
  }
  o.append(el("div",{class:"mute",text:`${p.totals.moves} file(s) would move, ${p.totals.keeps} already in place. Plan ${p.plan_id} — nothing has been moved.`}));
  $("sortApply").disabled=!p.totals.moves;
}
function sortEdits(){
  const e={};
  for(const g of sortPlan.groups){const q=s=>document.querySelector(`${s}[data-g="${g.id}"]`);
    const x={include:q(".sInc").checked};const n=q(".sName").value.trim(),m=q(".sMerge").value;
    x.merge_into=m||null;if(!m)x.project=n;const sp=q(".sSplit");if(sp&&sp.checked)x.split=true;e[g.id]=x;}
  return e;
}
// ---------------- custom instructions
let insSaved=null, insDirty=false;
function insLimits(){return (insSaved&&insSaved.limits)||{standing_chars:2000,next_run_chars:1000,glossary_terms:80,glossary_term_chars:40,glossary_chars:600};}
function glossTerms(t){return String(t||"").split(/[\n,;]+/).map(x=>x.trim()).filter(Boolean);}
function insBody(){return {standing:$("insStanding").value,glossary:$("insGloss").value,next_run:{text:$("insNext").value,keep:$("insKeep").checked}};}
function renderInsBadge(a){
  const on=!!a.active, parts=[];
  if(a.standing)parts.push("standing");if(a.next_run)parts.push("next run"+(a.keep?" (kept)":""));if(a.glossary_terms)parts.push(`glossary ${a.glossary_terms}`);
  for(const id of ["sIns","insBadge"]){$(id).textContent=on?"Instructions active":"off";$(id).className="badge "+(on?"b-ins":"b-off");$(id).title=on?`hash ${a.hash}`:"No custom instructions";}
  $("sInsTxt").textContent=on?`${parts.join(" · ")} · ${a.hash}`:"";
}
function insCount(){
  const L=insLimits(), b=insBody(), terms=glossTerms(b.glossary), gchars=terms.reduce((n,t)=>n+t.length+2,0), long=terms.filter(t=>t.length>L.glossary_term_chars);
  const set=(id,txt,over)=>{$(id).innerHTML="";$(id).className="cnt"+(over?" over":"");$(id).append(el("span",{text:txt}));};
  set("insStandingCnt",`${b.standing.length} / ${L.standing_chars} characters`,b.standing.length>L.standing_chars);
  set("insNextCnt",`${b.next_run.text.length} / ${L.next_run_chars} characters`+(/^\s*project\s*[:=]/im.test(b.next_run.text)?" · has a “Project:” line for Sort into projects":""),b.next_run.text.length>L.next_run_chars);
  set("insGlossCnt",`${terms.length} / ${L.glossary_terms} terms · ${gchars} / ${L.glossary_chars} characters`+(long.length?` · too long: ${long[0].slice(0,20)}…`:""),terms.length>L.glossary_terms||gchars>L.glossary_chars||long.length>0);
  const over=document.querySelectorAll("#insCard .cnt.over").length>0;
  $("insSave").disabled=over||!insDirty;$("insPrevBtn").disabled=over;
  $("insState").textContent=over?"Over a limit — shorten the red field.":(insDirty?"Unsaved changes":(insSaved&&insSaved.updated_at?`Saved ${fmtTime(insSaved.updated_at)}`+(insSaved.hash?` · hash ${insSaved.hash}`:""):"Nothing saved yet"));
}
function fillIns(j){insSaved=j;insDirty=false;$("insStanding").value=j.standing||"";$("insNext").value=(j.next_run||{}).text||"";$("insKeep").checked=!!(j.next_run||{}).keep;
  $("insGloss").value=(j.glossary||[]).join("\n");insCount();}
async function loadIns(){try{const [r,j]=await api("/api/instructions");if(r.ok)fillIns(j);}catch(e){}}
for(const id of ["insStanding","insNext","insGloss"])$(id).addEventListener("input",()=>{insDirty=true;insCount();});
$("insKeep").onchange=()=>{insDirty=true;insCount();};
$("insRevert").onclick=()=>{if(insSaved)fillIns(insSaved);};
$("insSave").onclick=async()=>{
  $("insSave").disabled=true;
  const [r,j]=await api("/api/instructions",{method:"POST",headers:JH,body:JSON.stringify(insBody())});
  if(r.ok){fillIns(j);toast(j.active?`Saved — instructions active (hash ${j.hash})`:"Saved — no instructions active","ok");pollStatus();}
  else{toast(j.error||"Not saved","bad");insCount();}
};
$("insPrevBtn").onclick=async()=>{
  const [r,j]=await api("/api/instructions/preview",{method:"POST",headers:JH,body:JSON.stringify({...insBody(),photo:$("insPhoto").checked})});
  if(!r.ok){toast(j.error||"Preview failed","bad");return;}
  $("insPrev").style.display="block";
  $("insPrevMeta").textContent=`— ${j.chars.system+j.chars.user} characters (guidance ${j.chars.guidance})${j.hash?" · hash "+j.hash:""}${insDirty?" · unsaved draft":""}. ${j.note}`;
  $("insPrevTxt").textContent="=== SYSTEM ===\n"+j.system+"\n\n=== USER (sent with the frames) ===\n"+j.user+"\n=== WHISPER --prompt ===\n"+(j.whisper_prompt||"(none — glossary is empty)");
  $("insPrev").scrollIntoView({block:"nearest"});
};
loadIns();

$("sortDry").onclick=async()=>{
  const b=$("sortDry");b.disabled=true;$("sortApply").disabled=true;$("sortOut").textContent="Reading clips and notes…";
  try{const [r,j]=await api("/api/sort/plan",{method:"POST",headers:JH,body:JSON.stringify({source:$("sortSrc").value,gap_minutes:+$("sortGap").value||60})});
    if(!r.ok)throw new Error(j.error||"refused");renderSortPlan(j);}catch(e){$("sortOut").textContent="";toast(""+e.message,"bad");}finally{b.disabled=false;}
};
$("sortApply").onclick=async()=>{
  if(!sortPlan)return;const ed=sortEdits(),n=Object.values(ed).filter(x=>x.include).length;
  if(!confirm(`Move the files of ${n} group(s) into their project folders now?\n\nClips already imported into a Resolve project will need relinking. An undo map is written.`))return;
  $("sortApply").disabled=true;
  try{const [r,j]=await api("/api/sort/apply",{method:"POST",headers:JH,body:JSON.stringify({plan_file:sortPlan.plan_file,edits:ed,confirm:true})});
    if(!r.ok)throw new Error(j.error||"refused");toast(`Moved ${j.moved} file(s)`+(j.skipped&&j.skipped.length?`, skipped ${j.skipped.length}`:"")+". Undo is available.","ok");sortPlan=null;$("sortOut").textContent="";loadSortSources();}
  catch(e){toast(""+e.message,"bad");$("sortApply").disabled=false;}
};
$("sortUndo").onclick=async()=>{
  if(!confirm("Move the files of the last sort back where they came from?"))return;
  const [r,j]=await api("/api/sort/undo",{method:"POST",headers:JH,body:JSON.stringify({confirm:true})});
  toast(r.ok?`Restored ${j.restored} file(s)`:(j.error||"refused"),r.ok?"ok":"bad");loadSortSources();
};
$("sortHist").onclick=async()=>{
  const [r,j]=await api("/api/sort/history");if(!r.ok){toast(j.error,"bad");return;}
  const o=$("sortOut");o.innerHTML="";if(!j.history.length){o.textContent="No sorts yet.";return;}
  for(const h of j.history)o.append(el("div",{class:"mono",text:`${h.time} ${h.plan_id} — moved ${h.moved}${h.undone?" (undone)":""}`}));
};
loadInbox();loadResults();pollStatus();loadScopes();loadSortSources();setInterval(pollStatus,POLL_MS);setInterval(()=>{if(!uploading)loadInbox();},30000);
</script></body></html>
"""

if __name__ == "__main__":
    raise SystemExit(main())
