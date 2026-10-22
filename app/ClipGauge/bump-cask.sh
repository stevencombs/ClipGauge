#!/bin/zsh
# Bump the ClipGauge Homebrew cask (stevencombs/homebrew-tap, Casks/clipgauge.rb) to a published release.
# Run it by hand AFTER the GitHub release and its ClipGauge-<version>.zip asset are live. Nothing runs it
# automatically. It downloads the release zip, checks it against the release's .sha256 file, rewrites
# `version` and `sha256` in the cask, then runs `brew style` and `brew audit`.
#   app/ClipGauge/bump-cask.sh                 # version from ./VERSION; edit + check only, no commit
#   app/ClipGauge/bump-cask.sh 0.8.4 --commit  # also commit as retroCombs <retroCombs@icloud.com>
#   app/ClipGauge/bump-cask.sh 0.8.4 --push    # commit and push to the tap
# The tap checkout defaults to `brew --repo stevencombs/tap` (run `brew tap stevencombs/tap` once); override
# with TAP_DIR=/path/to/homebrew-tap. Only Casks/clipgauge.rb is ever changed.
set -euo pipefail
HERE="${0:A:h}"
ROOT="${HERE:h:h}"
VER=""; COMMIT=0; PUSH=0
for a in "$@"; do
  case "$a" in
    --commit) COMMIT=1 ;;
    --push) COMMIT=1; PUSH=1 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) VER="${a#v}" ;;
  esac
done
[[ -n "$VER" ]] || VER="$(tr -d '[:space:]' < "$ROOT/VERSION")"
[[ "$VER" =~ '^[0-9]+(\.[0-9]+)+$' ]] || { echo "Not a version: $VER"; exit 1; }
command -v brew >/dev/null || { echo "Homebrew not found"; exit 1; }
TAP="${TAP_DIR:-$(brew --repo stevencombs/tap)}"
CASK="$TAP/Casks/clipgauge.rb"
[[ -f "$CASK" ]] || { echo "Cask not found: $CASK (run: brew tap stevencombs/tap)"; exit 1; }

BASE="https://github.com/stevencombs/ClipGauge/releases/download/v$VER"
TMP="$(mktemp -d /tmp/clipgauge-cask.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT
echo "Downloading ClipGauge-$VER.zip…"
curl -fsSL -o "$TMP/ClipGauge-$VER.zip" "$BASE/ClipGauge-$VER.zip"
SHA="$(shasum -a 256 "$TMP/ClipGauge-$VER.zip" | awk '{print $1}')"
if curl -fsSL -o "$TMP/ClipGauge-$VER.zip.sha256" "$BASE/ClipGauge-$VER.zip.sha256"; then
  PUB="$(awk '{print $1; exit}' "$TMP/ClipGauge-$VER.zip.sha256")"
  [[ "$PUB" == "$SHA" ]] || { echo "SHA-256 mismatch: zip $SHA, release file $PUB"; exit 2; }
  echo "SHA-256 matches the release's .sha256 file."
else
  echo "Note: no .sha256 asset on the release; using the computed hash."
fi
echo "sha256 $SHA"

/usr/bin/sed -i '' -E \
  -e "s/^(  version )\"[^\"]*\"/\1\"$VER\"/" \
  -e "s/^(  sha256 )\"[0-9a-f]*\"/\1\"$SHA\"/" "$CASK"
grep -q "version \"$VER\"" "$CASK" && grep -q "sha256 \"$SHA\"" "$CASK" || { echo "Cask edit failed"; exit 3; }

HOMEBREW_NO_AUTO_UPDATE=1 brew style "$CASK"
HOMEBREW_NO_AUTO_UPDATE=1 brew audit --cask --tap stevencombs/tap
git -C "$TAP" --no-pager diff -- Casks/clipgauge.rb

if (( COMMIT )); then
  git -C "$TAP" add Casks/clipgauge.rb
  git -C "$TAP" -c user.name=retroCombs -c user.email=retroCombs@icloud.com commit -q -m "clipgauge $VER"
  git -C "$TAP" --no-pager log -1 --format='%h %an <%ae> %s'
  (( PUSH )) && git -C "$TAP" push -q origin HEAD:main && echo "Pushed. Users get it with: brew upgrade --cask clipgauge"
else
  echo "Cask updated (not committed). Re-run with --commit or --push."
fi
