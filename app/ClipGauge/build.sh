#!/bin/zsh
# Rebuild "ClipGauge.app" (the all-in-one menu bar app for AI-Video-Renamer) from app/ClipGauge/Sources.
# Needs Xcode Command Line Tools (swiftc). Builds in /tmp (ExFAT can't hold codesign-clean bundles while
# building), ad-hoc signs it, then copies it to <AI-Video-Renamer>/ClipGauge.app (or the path given as $1).
#   app/ClipGauge/build.sh                      # -> AI-Video-Renamer/ClipGauge.app
#   app/ClipGauge/build.sh ~/Applications/ClipGauge.app
#   UNIVERSAL=1 app/ClipGauge/build.sh          # arm64 + x86_64
set -euo pipefail
HERE="${0:A:h}"
ROOT="${HERE:h:h}"
NAME="ClipGauge"
OUT="${1:-$ROOT/$NAME.app}"
EXE=ClipGauge
MINOS=14.0

command -v swiftc >/dev/null || { echo "swiftc not found — install Xcode Command Line Tools: xcode-select --install"; exit 1; }

BUILD="$(mktemp -d /tmp/clipgauge-build.XXXXXX)"
trap 'rm -rf "$BUILD"' EXIT
APP="$BUILD/$NAME.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

archs=(arm64)
[[ "${UNIVERSAL:-0}" == 1 ]] && archs=(arm64 x86_64)
bins=()
for a in $archs; do
  echo "swiftc ($a)…"
  swiftc -O -swift-version 5 -target "$a-apple-macos$MINOS" \
    -framework AppKit -framework ServiceManagement -framework UserNotifications -framework SwiftUI -framework Combine -framework Carbon \
    "$HERE"/Sources/*.swift -o "$BUILD/$EXE-$a"
  bins+=("$BUILD/$EXE-$a")
done
if (( ${#bins} > 1 )); then lipo -create $bins -output "$APP/Contents/MacOS/$EXE"; else cp "${bins[1]}" "$APP/Contents/MacOS/$EXE"; fi
cp "$HERE/Info.plist" "$APP/Contents/Info.plist"
printf 'APPL????' > "$APP/Contents/PkgInfo"
# App icon drawn by the app itself (same mark as the menu bar), then packed with iconutil.
if "$APP/Contents/MacOS/$EXE" --render-iconset "$BUILD/AppIcon.iconset" && iconutil -c icns "$BUILD/AppIcon.iconset" -o "$APP/Contents/Resources/AppIcon.icns"; then
  /usr/libexec/PlistBuddy -c "Add :CFBundleIconFile string AppIcon" "$APP/Contents/Info.plist" 2>/dev/null || true
else
  echo "Note: icon generation failed — using the generic app icon."
fi
# Bundle the renamer engine so Setup › Create Project can make a project folder anywhere (no models, no personal data).
SRC="${ENGINE_SRC:-$ROOT}"
ENG="$APP/Contents/Resources/engine"
mkdir -p "$ENG/scripts" "$ENG/config"
for f in "$SRC"/scripts/*.py(N) "$SRC"/scripts/*.sh(N); do cp "$f" "$ENG/scripts/"; done
[[ -f "$ENG/scripts/run_pipeline.py" && -f "$ENG/scripts/setup-mac.sh" && -f "$ENG/scripts/check_updates.py" && -f "$ENG/scripts/sort_projects.py" && -f "$ENG/scripts/instructions.py" && -f "$ENG/scripts/clipgauge_cli.py" && -f "$ENG/scripts/inplace.py" && -f "$ENG/scripts/renamer_actions.py" ]] || { echo "Engine scripts missing in $SRC/scripts"; exit 4; }
chmod +x "$ENG"/scripts/*.sh "$ENG"/scripts/*.py
cp "$SRC/config/ram-tiers.json" "$ENG/config/"
if [[ -f "$SRC/config/updates.json" ]]; then cp "$SRC/config/updates.json" "$ENG/config/"; fi
if [[ -f "$SRC/config/project-sort.json" ]]; then cp "$SRC/config/project-sort.json" "$ENG/config/"; fi
# Template config: dry-run on, no machine-specific root / absolute tool paths (PATH + Homebrew defaults are used).
/usr/bin/python3 - "$SRC/config/config.json" "$ENG/config/config.json" <<'PY'
import json, sys
c = json.load(open(sys.argv[1]))
c["dry_run"] = True
c.pop("root", None)
c["notes"] = "Portable project folder (any drive). On exFAT do not use symlinks. OLLAMA_MODELS must point at models/."
for k in ("ffmpeg_path", "ffprobe_path"):
    if k in c: c[k] = None
w = c.get("whisper")
if isinstance(w, dict):
    w.pop("cli_path", None)
    if "model_path" in w: w["model_path"] = None
json.dump(c, open(sys.argv[2], "w"), indent=2)
open(sys.argv[2], "a").write("\n")
PY
for f in README.md INSTALL.md SETUP.md DISTRIBUTION.md; do
  if [[ -f "$SRC/$f" ]]; then cp "$SRC/$f" "$ENG/"; fi
done
if [[ -f "$SRC/SETUP.md" ]]; then cp "$SRC/SETUP.md" "$APP/Contents/Resources/SETUP.md"; fi
if [[ -f "$HERE/Resources/retrocombs-logo.png" ]]; then cp "$HERE/Resources/retrocombs-logo.png" "$APP/Contents/Resources/"; fi
echo "Engine bundled: $(find "$ENG" -type f | wc -l | tr -d ' ') files"
xattr -cr "$APP"
codesign --force --sign - --identifier com.retrocombs.ClipGauge "$APP"
codesign --verify --strict "$APP"

if pgrep -f "$OUT/Contents/MacOS/$EXE" >/dev/null 2>&1; then
  echo "ClipGauge is running from $OUT — quit it from its menu first, then rebuild."; exit 2
fi
if [[ -d "$OUT" ]]; then
  [[ -f "$OUT/Contents/Info.plist" && "$OUT" == *.app ]] || { echo "Refusing to replace $OUT (not an app bundle)"; exit 3; }
  rm -rf "$OUT"
fi
mkdir -p "${OUT:h}"
ditto --norsrc --noextattr --noacl "$APP" "$OUT"
# ExFAT: drop any AppleDouble (._*) files macOS created inside the bundle — they would break the signature seal.
find "$OUT" -name '._*' -type f -delete 2>/dev/null || true
if codesign --verify --strict "$OUT" 2>/dev/null; then echo "Signature OK"; else echo "Note: signature check on the destination volume failed (ExFAT metadata) — the app still runs locally."; fi
echo "Built: $OUT"
