#!/usr/bin/env python3
"""Check (read-only) whether the renamer's AI models and tools have updates.

  python3 scripts/check_updates.py            # human summary
  python3 scripts/check_updates.py --json     # machine-readable (ClipGauge, web UI)
  python3 scripts/check_updates.py --no-brew-update   # don't refresh Homebrew's index first (weekly auto-check)

What it checks — nothing is downloaded or installed here (see apply_updates.py for that):
  * Ollama models in use (active RAM tier's preferred model + every model in <project>/models): the registry's
    manifest digest (registry.ollama.ai/v2/library/<name>/manifests/<tag>) vs this Mac's copy — Ollama ≤ 0.39
    manifests/ and Ollama 0.40 manifests-v2/ (manifest lists, locally migrated llama.cpp variants); see is_current().
    Download size = remote layers that aren't already in models/blobs.
  * Homebrew tools: ollama, whisper.cpp, ffmpeg (`brew update --quiet` first unless --no-brew-update, then
    `brew outdated --json=v2 <those formulae>`); size = the bottle for this Mac.
  * Whisper ggml files in models/whisper: sha256 vs Hugging Face's X-Linked-ETag (ggerganov/whisper.cpp).
  * Advisories (config/updates.json): newer model families suited to this RAM tier — information only.

Writes logs/updates-state.json (last result + time) and appends to logs/updates-YYYYMMDD.log.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import ROOT, abs_path, atomic_write_text, load_config, section  # noqa: E402

REGISTRY = "https://registry.ollama.ai"
MANIFEST_ACCEPT = "application/vnd.docker.distribution.manifest.v2+json"
HF_BASE = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main"
BREW_FORMULAE = (("ollama", "Ollama"), ("whisper.cpp", "whisper.cpp"), ("ffmpeg", "FFmpeg"))
FORMULA_ALIASES = {"whisper.cpp": ("whisper.cpp", "whisper-cpp")}
PREV_SUFFIX = "-clipgauge-prev"   # backup tag apply_updates.py keeps while a new model is verified
LEGACY_PREV_SUFFIXES = ("-clipguage-prev",)  # written by ClipGuage (≤ v0.5, old spelling) — still never listed as installed models
UA = "ClipGauge-update-check/0.3"
MACOS_TAGS = {14: "sonoma", 15: "sequoia", 26: "tahoe", 27: "golden_gate"}


# ---------------------------------------------------------------------------
# I/O seams (selftest replaces these)
# ---------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # noqa: D401 - keep the 302 (its headers carry the HF etag)
        return None


def fetch(url: str, headers: dict | None = None, method: str = "GET", timeout: float = 20,
          follow: bool = True) -> tuple[int, dict, bytes]:
    """(status, lower-cased headers, body). HTTP errors are returned, not raised; network errors raise OSError."""
    req = urllib.request.Request(url, method=method, headers={"User-Agent": UA, **(headers or {})})
    opener = urllib.request.build_opener() if follow else urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, (r.read() if method != "HEAD" else b"")
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, b""
    except urllib.error.URLError as e:
        raise OSError(str(e.reason)) from e


def run_cmd(argv: list[str], timeout: float = 120, env: dict | None = None) -> tuple[int, str, str]:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, **(env or {})}, stdin=subprocess.DEVNULL)
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return 127, "", f"{argv[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{' '.join(argv[:3])} timed out after {timeout:.0f}s"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def human_bytes(n: int | None) -> str:
    if n is None:
        return "size unknown"
    x = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if x < 1000 or unit == "TB":
            return f"{x:.0f} {unit}" if unit in ("B", "KB") or x >= 100 else f"{x:.1f} {unit}"
        x /= 1000
    return f"{n} B"


def sha256_file(path: Path, chunk: int = 4 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def logs_dir(cfg: dict) -> Path:
    return abs_path(cfg.get("logs_dir") or "logs")


def models_dir(cfg: dict) -> Path:
    return abs_path(cfg.get("models_dir") or "models")


def updates_config(root: Path = ROOT) -> dict:
    try:
        return json.loads((root / "config" / "updates.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def log_line(cfg: dict, text: str) -> None:
    try:
        d = logs_dir(cfg)
        d.mkdir(parents=True, exist_ok=True)
        with open(d / f"updates-{datetime.now():%Y%m%d}.log", "a", encoding="utf-8") as f:
            f.write(f"[{now_iso()}] {text}\n")
    except OSError:
        pass


def parse_ref(ref: str) -> tuple[str, str, str]:
    """'qwen2.5vl:7b' -> ('library', 'qwen2.5vl', '7b'); 'ns/name' -> ('ns', 'name', 'latest')."""
    name, _, tag = ref.partition(":")
    ns, _, base = name.rpartition("/")
    return (ns or "library"), base, (tag or "latest")


def ref_of(ns: str, name: str, tag: str) -> str:
    return f"{name}:{tag}" if ns == "library" else f"{ns}/{name}:{tag}"


# ---------------------------------------------------------------------------
# Ollama models
# ---------------------------------------------------------------------------

# Ollama ≤ 0.39 keeps one manifest file per tag under manifests/registry.ollama.ai/<ns>/<name>/<tag>.
# Ollama 0.40 moved tags to manifests-v2/ollama.com/<ns>/<name>/<tag> — a symlink to a blob holding a *manifest list*
# (application/vnd.ollama.manifest.list.v2+json) whose children are per-runner manifests stored as blobs. Its "local
# compat GGUF migration" also re-packs llama.cpp-runner models locally (new child manifest, new digest, the original
# weights kept beside the converted ones) and leaves a rollback shadow tag  manifests/registry.ollama.ai/library/
# llamacpp/<64-hex digest>  that /api/tags lists as a model (ollama/ollama#18830). So:
#   * a tag can live in either layout (both are checked, v2 first);
#   * the shadow tags are not models (never listed, never checked, never offered in the chat picker);
#   * the local digest after migration never equals the registry's, so "current" is decided from evidence that the
#     registry's manifest was pulled here (see local_identity / is_current), not from the tag file's sha256.
MANIFEST_LIST = "application/vnd.ollama.manifest.list.v2+json"
V2_HOSTS = ("ollama.com", "registry.ollama.ai")
SHADOW_RUNNERS = ("llamacpp", "ggml", "mlx", "ollama")
VERSIONS_FILE = "model-versions.json"   # logs/: registry digest recorded for each verified local tag


def _hex64(s: str) -> bool:
    return len(s) == 64 and all(c in "0123456789abcdef" for c in s)


def is_shadow_ref(ref: str) -> bool:
    """Ollama 0.40 migration rollback shadows ('llamacpp:<64-hex>') — internal, not user models."""
    ns, name, tag = parse_ref(ref)
    return ns == "library" and name in SHADOW_RUNNERS and _hex64(tag)


def manifest_paths(mdir: Path, ref: str) -> list[Path]:
    ns, name, tag = parse_ref(ref)
    return [mdir / "manifests-v2" / h / ns / name / tag for h in V2_HOSTS] + \
           [mdir / "manifests" / "registry.ollama.ai" / ns / name / tag]


def local_manifest_path(mdir: Path, ref: str) -> Path:
    """The file that holds the tag (following Ollama 0.40's symlinks); the legacy path if none exists."""
    paths = manifest_paths(mdir, ref)
    for p in paths:
        try:
            if p.is_file():
                return p
        except OSError:
            continue
    return paths[-1]


def installed_models(mdir: Path) -> list[str]:
    out: list[str] = []
    roots = [mdir / "manifests-v2" / h for h in V2_HOSTS] + [mdir / "manifests" / "registry.ollama.ai"]
    for base in roots:
        if not base.is_dir():
            continue
        for ns_dir in sorted(p for p in base.iterdir() if p.is_dir() and not p.name.startswith(".")):
            for name_dir in sorted(p for p in ns_dir.iterdir() if p.is_dir() and not p.name.startswith(".")):
                for tag in sorted(p for p in name_dir.iterdir() if not p.name.startswith(".")):
                    try:
                        if not tag.is_file():
                            continue
                    except OSError:
                        continue
                    if tag.name.endswith((PREV_SUFFIX, *LEGACY_PREV_SUFFIXES)):
                        continue
                    ref = ref_of(ns_dir.name, name_dir.name, tag.name)
                    if is_shadow_ref(ref) or ref in out:
                        continue
                    out.append(ref)
    return out


def go_json(obj) -> bytes:
    """Compact JSON the way Go's encoding/json writes it (HTML-escaped <, >, &)."""
    s = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
    return s.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026").encode()


def pre_migration_digest(m: dict) -> str | None:
    """Undo what Ollama 0.40 adds to a migrated child manifest (runner/format fields, appended manifest layers) and
    hash it again: equals the registry digest when the child is the original (non-converted) variant."""
    if not isinstance(m, dict) or "layers" not in m:
        return None
    c = {k: v for k, v in m.items() if k not in ("runner", "format")}
    c["layers"] = [l for l in (m.get("layers") or []) if not (isinstance(l, dict) and str(l.get("mediaType", ""))
                   .endswith(("manifest.list.v2+json", "manifest.v2+json")))]
    try:
        return hashlib.sha256(go_json(c)).hexdigest()
    except (TypeError, ValueError):
        return None


def _blob_json(mdir: Path, digest: str) -> tuple[bytes | None, dict | None]:
    try:
        raw = blob_path(mdir, digest if ":" in digest else f"sha256:{digest}").read_bytes()
        return raw, json.loads(raw)
    except (OSError, ValueError):
        return None, None


def local_identity(mdir: Path, ref: str) -> dict | None:
    """What this Mac has for `ref`: {path, layout, digest (sha256 of the tag file), ids (every digest this local copy
    can be identified by: the tag file, manifest-list children, their pre-migration digests), runners, mtime}."""
    p = local_manifest_path(mdir, ref)
    try:
        raw = p.read_bytes()
        st = p.stat()
    except OSError:
        return None
    d = hashlib.sha256(raw).hexdigest()
    ids, runners = {d}, []
    try:
        m = json.loads(raw)
    except ValueError:
        m = None
    if isinstance(m, dict) and m.get("mediaType") == MANIFEST_LIST:
        for ch in m.get("manifests") or []:
            cd = str((ch or {}).get("digest") or "").split(":")[-1]
            if not cd:
                continue
            ids.add(cd)
            if ch.get("runner"):
                runners.append(ch["runner"])
            _raw, cm = _blob_json(mdir, cd)
            pm = pre_migration_digest(cm) if cm else None
            if pm:
                ids.add(pm)
    elif isinstance(m, dict):
        pm = pre_migration_digest(m)
        if pm:
            ids.add(pm)
        if m.get("runner"):
            runners.append(m["runner"])
    return {"path": str(p), "layout": "v2" if "manifests-v2" in p.parts else "legacy", "digest": d,
            "ids": sorted(ids), "runners": runners,
            "mtime": datetime.fromtimestamp(st.st_mtime).astimezone().isoformat(timespec="seconds")}


def versions_path(mdir: Path, cfg: dict | None = None) -> Path:
    return (logs_dir(cfg) if cfg else (mdir.parent / "logs")) / VERSIONS_FILE


def read_versions(path: Path) -> dict:
    try:
        v = json.loads(path.read_text(encoding="utf-8"))
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def record_version(path: Path, ref: str, registry_digest: str, local: dict | None, how: str) -> None:
    """Remember which registry version a verified local tag corresponds to (survives Ollama pruning the original
    registry manifest blob after a migration)."""
    if not local or not registry_digest:
        return
    v = read_versions(path)
    v[ref] = {"registry_digest": registry_digest, "local_digest": local["digest"], "how": how,
              "recorded_at": now_iso()}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(v, indent=2, sort_keys=True) + "\n")
    except OSError:
        pass


def is_current(mdir: Path, ref: str, remote_digest: str, layers: list[dict] | None = None,
               local: dict | None = None, versions: dict | None = None) -> tuple[bool, str]:
    """(current?, how). Evidence, strongest first: the local copy's ids include the registry digest; a recorded
    verified pairing (registry digest ↔ this exact local tag file); Ollama kept the registry's manifest as a blob and
    every layer it lists is on disk (what an Ollama ≥ 0.40 pull leaves behind before/after migrating)."""
    local = local if local is not None else local_identity(mdir, ref)
    if not local or not remote_digest:
        return False, "not installed" if not local else "no registry digest"
    if remote_digest in local["ids"]:
        return True, "digest"
    rec = (versions or {}).get(ref) or {}
    if rec.get("registry_digest") == remote_digest and rec.get("local_digest") == local["digest"]:
        return True, "recorded"
    if blob_path(mdir, f"sha256:{remote_digest}").is_file() and layers is not None and \
            all(blob_path(mdir, l["digest"]).is_file() for l in layers):
        return True, "pulled"
    return False, "differs"


def remote_manifest(ref: str) -> tuple[str, dict]:
    """(sha256 hex of the manifest bytes, parsed manifest). Raises OSError on any failure."""
    ns, name, tag = parse_ref(ref)
    code, _h, body = fetch(f"{REGISTRY}/v2/{ns}/{name}/manifests/{tag}", {"Accept": MANIFEST_ACCEPT})
    if code == 404:
        raise FileNotFoundError(f"{ref} is not in the Ollama library")
    if code != 200 or not body:
        raise OSError(f"registry answered HTTP {code}")
    return hashlib.sha256(body).hexdigest(), json.loads(body)


def manifest_layers(m: dict) -> list[dict]:
    layers = list(m.get("layers") or [])
    if isinstance(m.get("config"), dict):
        layers.append(m["config"])
    return [l for l in layers if isinstance(l, dict) and l.get("digest")]


def blob_path(mdir: Path, digest: str) -> Path:
    return mdir / "blobs" / digest.replace(":", "-")


def check_model(ref: str, mdir: Path, role: str, versions: dict | None = None) -> dict:
    item = {"id": f"model:{ref}", "kind": "model", "name": ref, "target": ref, "role": role,
            "link": f"https://ollama.com/library/{parse_ref(ref)[1]}"}
    local = local_identity(mdir, ref)
    if local:
        item["current"] = local["digest"][:12]
        item["installed_at"] = local["mtime"]
        item["layout"] = local["layout"]
        if local["runners"]:
            item["runners"] = local["runners"]
    try:
        remote, man = remote_manifest(ref)
    except FileNotFoundError as e:
        return {**item, "status": "unknown", "upgradable": False, "detail": str(e)}
    except (OSError, ValueError) as e:
        return {**item, "status": "unknown", "upgradable": False, "error": str(e),
                "detail": f"Couldn't reach the Ollama registry ({e})"}
    layers = manifest_layers(man)
    size = sum(int(l.get("size") or 0) for l in layers)
    need = sum(int(l.get("size") or 0) for l in layers if not blob_path(mdir, l["digest"]).is_file())
    item.update({"latest": remote[:12], "latest_digest": remote, "local_digest": local["digest"] if local else None,
                 "size_bytes": size, "download_bytes": need})
    if local is None:
        item.update(status="missing", upgradable=True, action="Install",
                    detail=f"Not installed in {mdir.name}/ — needed for this Mac's tier ({human_bytes(need)})")
        return item
    ok, how = is_current(mdir, ref, remote, layers, local, versions)
    item["match"] = how
    if ok:
        detail = "Up to date" + (" (Ollama re-packed it locally for its llama.cpp runner — same version)"
                                 if how != "digest" or local["runners"] else "")
        item.update(status="current", upgradable=False, download_bytes=0, detail=detail)
    else:
        item.update(status="outdated", upgradable=True, action="Upgrade",
                    detail=f"New version on ollama.com — {human_bytes(need)} to download")
    return item


# ---------------------------------------------------------------------------
# Homebrew tools
# ---------------------------------------------------------------------------

def brew_path() -> str | None:
    for c in (shutil.which("brew"), "/opt/homebrew/bin/brew", "/usr/local/bin/brew"):
        if c and os.access(c, os.X_OK):
            return c
    return None


def bottle_tag() -> str:
    arch = "arm64_" if platform.machine() == "arm64" else ""
    try:
        major = int(platform.mac_ver()[0].split(".")[0])
    except (ValueError, IndexError):
        major = 0
    return arch + MACOS_TAGS.get(major, "")


def bottle_size(brew: str, formula: str) -> int | None:
    rc, out, _ = run_cmd([brew, "info", "--json=v2", formula], timeout=60, env={"HOMEBREW_NO_AUTO_UPDATE": "1"})
    if rc != 0:
        return None
    try:
        files = json.loads(out)["formulae"][0]["bottle"]["stable"]["files"]
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    tag = bottle_tag()
    want_arm = tag.startswith("arm64_")
    pick = files.get(tag) or next((v for k, v in files.items()
                                   if "linux" not in k and k.startswith("arm64_") == want_arm), None)
    if not pick or not pick.get("url"):
        return None
    try:
        code, h, _ = fetch(pick["url"], {"Authorization": "Bearer QQ=="}, method="HEAD")
        if code != 200:
            return None
        return int(h.get("content-length", 0)) or None
    except (OSError, ValueError):
        return None


def check_brew(do_update: bool, sizes: bool = True) -> tuple[list[dict], dict]:
    meta: dict = {"brew": None, "brew_updated": False}
    brew = brew_path()
    items = []
    if not brew:
        for f, label in BREW_FORMULAE:
            items.append({"id": f"tool:{f}", "kind": "tool", "name": label, "target": f, "status": "unknown",
                          "upgradable": False, "detail": "Homebrew isn't installed — run Setup › Install Missing…"})
        return items, meta
    meta["brew"] = brew
    if do_update:
        rc, _o, err = run_cmd([brew, "update", "--quiet"], timeout=600)
        meta["brew_updated"] = rc == 0
        if rc != 0:
            meta["brew_update_error"] = (err.strip().splitlines() or ["failed"])[-1][:200]
    installed: dict[str, tuple[str, str]] = {}   # canonical -> (brew name, version)
    for f, _label in BREW_FORMULAE:
        for name in FORMULA_ALIASES.get(f, (f,)):
            rc, out, _ = run_cmd([brew, "list", "--versions", name], timeout=60, env={"HOMEBREW_NO_AUTO_UPDATE": "1"})
            line = out.strip().splitlines()[-1] if rc == 0 and out.strip() else ""
            if line:
                parts = line.split()
                installed[f] = (parts[0], parts[-1])
                break
    outdated: dict[str, dict] = {}
    if installed:
        rc, out, err = run_cmd([brew, "outdated", "--json=v2", *[v[0] for v in installed.values()]], timeout=120,
                               env={"HOMEBREW_NO_AUTO_UPDATE": "1", "HOMEBREW_NO_ENV_HINTS": "1"})
        try:
            for f in json.loads(out or "{}").get("formulae", []):
                outdated[f.get("name")] = f
        except ValueError:
            meta["brew_outdated_error"] = (err.strip() or out.strip())[:200]
    for f, label in BREW_FORMULAE:
        item = {"id": f"tool:{f}", "kind": "tool", "name": label, "target": f,
                "link": f"https://formulae.brew.sh/formula/{f}"}
        if f not in installed:
            exe = shutil.which({"whisper.cpp": "whisper-cli"}.get(f, f))
            item.update(status="unknown", upgradable=False,
                        detail=f"Not installed with Homebrew{f' (found {exe})' if exe else ''} — can't upgrade it here")
        else:
            name, ver = installed[f]
            item["current"] = ver
            item["target"] = name
            o = outdated.get(name)
            if o and not o.get("pinned"):
                item.update(status="outdated", upgradable=True, action="Upgrade", latest=o.get("current_version"),
                            download_bytes=bottle_size(brew, name) if sizes else None)
                item["detail"] = (f"{ver} → {item['latest']} · ~{human_bytes(item['download_bytes'])} "
                                  "(plus any outdated dependencies)")
            elif o and o.get("pinned"):
                item.update(status="current", upgradable=False, latest=o.get("current_version"),
                            detail=f"{ver} (pinned in Homebrew; {o.get('current_version')} available)")
            else:
                item.update(status="current", upgradable=False, latest=ver, detail=f"{ver} · up to date")
        items.append(item)
    return items, meta


# ---------------------------------------------------------------------------
# Whisper ggml files
# ---------------------------------------------------------------------------

def _hash_cache(cfg: dict) -> tuple[Path, dict]:
    p = logs_dir(cfg) / "updates-hash-cache.json"
    try:
        return p, json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return p, {}


def cached_sha(path: Path, cache: dict) -> str:
    st = path.stat()
    key = str(path)
    hit = cache.get(key)
    if hit and hit.get("size") == st.st_size and hit.get("mtime") == int(st.st_mtime):
        return hit["sha256"]
    sha = sha256_file(path)
    cache[key] = {"size": st.st_size, "mtime": int(st.st_mtime), "sha256": sha}
    return sha


def remote_whisper(fname: str, base: str = HF_BASE) -> tuple[str | None, int | None]:
    """(sha256 or etag, size) from Hugging Face without downloading (HEAD, no redirect)."""
    code, h, _ = fetch(f"{base.rstrip('/')}/{fname}", method="HEAD", follow=False)
    if code == 404:
        raise FileNotFoundError(f"{fname} isn't in ggerganov/whisper.cpp on Hugging Face")
    if code not in (200, 301, 302, 303, 307, 308):
        raise OSError(f"Hugging Face answered HTTP {code}")
    etag = (h.get("x-linked-etag") or h.get("etag") or "").replace("W/", "").strip('"') or None
    size = h.get("x-linked-size") or (h.get("content-length") if code == 200 else None)
    return etag, (int(size) if size and str(size).isdigit() else None)


def whisper_dir(cfg: dict) -> Path:
    return abs_path(section(cfg, "whisper").get("models_dir") or "models/whisper")


def check_whisper(cfg: dict, tier_model: str | None) -> list[dict]:
    wdir = whisper_dir(cfg)
    base = section(cfg, "whisper").get("download_base_url") or HF_BASE
    files = sorted(p for p in wdir.glob("ggml-*.bin") if not p.name.startswith(".")) if wdir.is_dir() else []
    wanted = f"ggml-{tier_model}.bin" if tier_model else None
    names = [p.name for p in files]
    if wanted and wanted not in names:
        names.append(wanted)
    cache_path, cache = _hash_cache(cfg)
    items = []
    for fname in names:
        path = wdir / fname
        item = {"id": f"whisper:{fname}", "kind": "whisper", "name": fname, "target": fname,
                "role": "tier" if fname == wanted else "installed",
                "link": f"https://huggingface.co/ggerganov/whisper.cpp/blob/main/{fname}"}
        try:
            etag, size = remote_whisper(fname, base)
        except FileNotFoundError as e:
            items.append({**item, "status": "unknown", "upgradable": False, "detail": str(e)})
            continue
        except OSError as e:
            items.append({**item, "status": "unknown", "upgradable": False, "error": str(e),
                          "detail": f"Couldn't reach Hugging Face ({e})"})
            continue
        item.update(latest_sha=etag, size_bytes=size, latest=(etag or "")[:12] or None)
        if not path.is_file():
            item.update(status="missing", upgradable=True, action="Install", download_bytes=size,
                        detail=f"Not downloaded yet — this Mac's tier uses it ({human_bytes(size)})")
        else:
            local_size = path.stat().st_size
            is_sha = bool(etag and len(etag) == 64 and all(c in "0123456789abcdef" for c in etag))
            if is_sha:
                local = cached_sha(path, cache)
                same = local == etag
                item["current"] = local[:12]
            else:
                same = size is not None and size == local_size
                item["current"] = human_bytes(local_size)
            if same:
                item.update(status="current", upgradable=False, download_bytes=0,
                            detail=f"Up to date · {human_bytes(local_size)}")
            else:
                item.update(status="outdated", upgradable=True, action="Upgrade", download_bytes=size,
                            detail=f"Changed on Hugging Face — {human_bytes(size)} to download (old file kept as .bak until verified)")
        items.append(item)
    try:
        atomic_write_text(cache_path, json.dumps(cache, indent=1) + "\n")
    except OSError:
        pass
    return items


# ---------------------------------------------------------------------------
# Advisories (information only)
# ---------------------------------------------------------------------------

def check_advisories(tier: str, in_use: set[str], ucfg: dict) -> list[dict]:
    items = []
    for adv in (ucfg.get("advisories") or {}).get(tier, []):
        ref = adv.get("model")
        if not ref or ref in in_use:
            continue
        item = {"id": f"advisory:{ref}", "kind": "advisory", "name": adv.get("family") or ref, "target": ref,
                "status": "info", "upgradable": False, "link": adv.get("link") or f"https://ollama.com/library/{parse_ref(ref)[1]}"}
        try:
            _d, man = remote_manifest(ref)
            item["size_bytes"] = sum(int(l.get("size") or 0) for l in manifest_layers(man))
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            item["size_bytes"] = None
        item["detail"] = (f"{ref} ({human_bytes(item['size_bytes'])}) — {adv.get('note') or 'newer model family'}. "
                          "Info only: try it on a few clips before switching (config/ram-tiers.json).")
        items.append(item)
    return items


# ---------------------------------------------------------------------------
# main check
# ---------------------------------------------------------------------------

def active_tier() -> tuple[str, dict]:
    from detect_ram import current_tier
    tier, info, _gb = current_tier()
    return tier, info


def run_check(cfg: dict | None = None, brew_update: bool = True, tier: tuple[str, dict] | None = None,
              sizes: bool = True, save: bool = True, reason: str = "manual") -> dict:
    cfg = cfg or load_config()
    t0 = time.monotonic()
    tier_name, tier_info = tier or active_tier()
    mdir = models_dir(cfg)
    ucfg = updates_config(Path(cfg.get("_root") or ROOT))
    wanted: list[tuple[str, str]] = []
    prefer = section(cfg, "describe").get("model") or tier_info.get("prefer")
    if prefer:
        wanted.append((prefer, "tier"))
    for ref in installed_models(mdir):
        if ref not in [w[0] for w in wanted]:
            wanted.append((ref, "installed"))
    vpath = versions_path(mdir, cfg)
    versions = read_versions(vpath)
    items = [check_model(ref, mdir, role, versions) for ref, role in wanted]
    if save:   # pin the verified pairing so a later prune of the registry-manifest blob can't flip it to "outdated"
        for it in items:
            if it.get("status") == "current" and it.get("match") in ("digest", "pulled") and it.get("latest_digest"):
                rec = versions.get(it["name"]) or {}
                if rec.get("registry_digest") != it["latest_digest"] or rec.get("local_digest") != it.get("local_digest"):
                    record_version(vpath, it["name"], it["latest_digest"], local_identity(mdir, it["name"]),
                                   f"check:{it['match']}")
    tools, meta = check_brew(brew_update, sizes=sizes)
    items += tools
    items += check_whisper(cfg, tier_info.get("whisper_model"))
    items += check_advisories(tier_name, {w[0] for w in wanted}, ucfg)
    upgr = [i for i in items if i.get("upgradable")]
    result = {
        "checked_at": now_iso(),
        "reason": reason,
        "root": str(cfg.get("_root") or ROOT),
        "models_dir": str(mdir),
        "tier": tier_name,
        "tier_label": tier_info.get("label"),
        **meta,
        "items": items,
        "summary": {
            "upgradable": len(upgr),
            "download_bytes": sum(int(i.get("download_bytes") or 0) for i in upgr),
            "size_unknown": sum(1 for i in upgr if i.get("download_bytes") is None),
            "errors": sum(1 for i in items if i.get("error")),
        },
        "elapsed_s": round(time.monotonic() - t0, 1),
    }
    if save:
        try:
            atomic_write_text(logs_dir(cfg) / "updates-state.json", json.dumps(result, indent=2) + "\n")
        except OSError:
            pass
        log_line(cfg, f"check ({reason}, brew update {'ok' if meta.get('brew_updated') else 'skipped' if not brew_update else 'failed'}): "
                      + (", ".join(f"{i['name']} {i['status']}" for i in items if i["kind"] != "advisory") or "nothing to check")
                      + f" — {len(upgr)} upgradable, {human_bytes(result['summary']['download_bytes'])}")
    return result


def summary_text(r: dict) -> str:
    mark = {"current": "✓", "outdated": "↑", "missing": "+", "unknown": "?", "info": "i"}
    lines = [f"Update check {r['checked_at']} · tier {r['tier']} · models in {r['models_dir']}"]
    if r.get("brew_update_error"):
        lines.append(f"  (brew update failed: {r['brew_update_error']})")
    for i in r["items"]:
        lines.append(f"  {mark.get(i['status'], '-')} {i['name']:<24} {i.get('detail', '')}")
    s = r["summary"]
    lines.append(f"{s['upgradable']} update(s) available"
                 + (f", {human_bytes(s['download_bytes'])} to download" if s["upgradable"] else "")
                 + (" — run: python3 scripts/apply_updates.py --all" if s["upgradable"] else ""))
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Check (read-only) for model and tool updates")
    ap.add_argument("--json", action="store_true", help="JSON output")
    ap.add_argument("--no-brew-update", action="store_true", help="Don't run `brew update --quiet` first")
    ap.add_argument("--no-save", action="store_true", help="Don't write logs/updates-state.json")
    ap.add_argument("--reason", default="manual", help="Recorded in the state/log (manual|weekly|after-upgrade)")
    args = ap.parse_args()
    r = run_check(brew_update=not args.no_brew_update, save=not args.no_save, reason=args.reason)
    print(json.dumps(r, indent=2) if args.json else summary_text(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
