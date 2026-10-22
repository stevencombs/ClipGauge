# ClipGauge: draft plan for public distribution

*Plan, 2026-10-08. The source is public at github.com/stevencombs/ClipGauge since v0.7.2. The app is ad-hoc signed, not notarized.*

## 1. What gets shipped

| Piece | Ship? | How |
|---|---|---|
| ClipGauge.app (Swift: menu bar + main window + Ask the Model chat) | Yes | Signed and notarized `.dmg` or `.zip` on GitHub Releases, plus a Homebrew cask (same pattern as GrokGauge) |
| Renamer engine (`scripts/*.py`, `*.sh`, config templates, docs) | Yes, **inside the app** at `Contents/Resources/engine` | Setup › *Create Project* copies it into the user's chosen folder |
| Homebrew, Python, ffmpeg, Ollama, whisper.cpp | **No** | The user installs them via `scripts/setup-mac.sh` with a prompt for each step. Not bundling them avoids the GPL/LGPL redistribution duties of FFmpeg and keeps the app small |
| Model weights (qwen2.5vl, gemma3, Whisper ggml) | **No** | Downloaded on the user's Mac with `ollama pull` and from the whisper.cpp Hugging Face repo, after the user approves. The app only *names* the models |

## 2. GitHub repository (when the owner says go)

- **Name:** `stevencombs/ClipGauge` (public since v0.7.2).
- **Layout:**

  ```
  app/ClipGauge/   (Sources, Info.plist, build.sh, Resources/retrocombs-logo.png)
  engine/          (scripts/, config/*.json templates)
  docs/            (SETUP.md, DISTRIBUTION.md, screenshots)
  ```

  `build.sh` takes the engine from `engine/` instead of the project root.
- **Must NOT be committed:**
  - `models/`, `inbox/`, `logs/`, `notes/`, `exports/`, `processing/`
  - Any clip, transcript or rename log
  - The personal `config/config.json`, which holds absolute paths. Commit a `config.example.json` with `"dry_run": true`.
- **Personal data:** remove personal defaults before going public.
  - A hard-coded drive path is now only a *fallback* in ClipGauge's project lookup. `_common.py` and the setup scripts already derive the root from their own location.
  - Check the project-name / subject vocabulary in config and prompts.
- **CI (optional):** a GitHub Actions macOS runner can build with `swiftc` and run `python3 scripts/selftest.py`. It needs no models; the tests use mocks.

## 3. License

- **Code:** MIT, matching GrokGauge (`Copyright (c) 2026 Steven Combs (retroCombs)`).
- **Third parties.** Add a `THIRD_PARTY_NOTICES.md` that names each component, its license and where it comes from. None of them is redistributed by us.

| Component | License | Note |
|---|---|---|
| Ollama | MIT | Installed by the user via Homebrew |
| whisper.cpp | MIT | Homebrew formula `whisper-cpp` (binary `whisper-cli`) |
| Whisper model weights (OpenAI; ggml conversions) | MIT | `ggml-base.en.bin` / `ggml-large-v3-turbo.bin` from huggingface.co/ggerganov/whisper.cpp |
| **Qwen2.5-VL-7B** (default on 16 GB Macs) | **Apache 2.0** | Commercial use is OK. Keep the attribution/notice |
| Qwen2.5-VL-3B (fallback) | **Qwen Research License (non-commercial)** | Fine for personal use. Flag it in the docs, or swap the fallback to a permissive model if ClipGauge is ever sold or bundled commercially |
| Qwen2.5-VL-32B / 72B (Pro alternates) | Apache 2.0 (32B) / Qwen license (72B) | Verify at release time |
| Gemma 3 (Pro tier default `gemma3:12b`) | Gemma Terms of Use + Prohibited Use Policy | Not OSI open source. Users accept the terms by pulling it. Mention it in docs |
| FFmpeg | LGPL 2.1+ / GPL (Homebrew build is GPL) | Not bundled, only invoked as an external tool |
| Python 3 | PSF | Not bundled |

Re-verify every model license on the model card at release time, since licenses on Hugging Face and the Ollama library can change.

## 4. Signing and notarization (Developer ID)

Today's builds are **ad-hoc signed**. Downloaded copies show the Gatekeeper warning, and *Open at login* (SMAppService) is unreliable for ad-hoc apps outside /Applications.

**Needs:**

1. **Apple Developer Program** membership ($99/yr) under the owner's Apple ID or a business entity.
2. A **Developer ID Application** certificate in the login keychain. A *Developer ID Installer* certificate is only needed for a `.pkg`, and a `.dmg`/`.zip` doesn't need one.
3. **Hardened runtime.** Add `--options runtime` to codesign. Expected entitlements: none.
   - Not sandboxed: the app runs the Python engine directly (`scripts/clipgauge_cli.py`), Terminal and Finder actions, and reads any user-chosen folder (drop/Choose…, *Process in place*).
   - Localhost HTTP (only the local Ollama server at 127.0.0.1:11434) needs only `NSAllowsLocalNetworking`, which is already in Info.plist.
   - If sandboxing were ever wanted, it would break spawning `python3` and opening Terminal. Not recommended.
4. **Sign everything inside the bundle.**
   - The engine `.sh` and `.py` files are not Mach-O, so they are sealed as resources.
   - With `UNIVERSAL=1`, build arm64 + x86_64.
5. **Notarize:**

   ```bash
   xcrun notarytool store-credentials clipgauge --apple-id … --team-id … --password <app-specific>
   ditto -c -k --keepParent ClipGauge.app ClipGauge.zip
   xcrun notarytool submit ClipGauge.zip --keychain-profile clipgauge --wait
   xcrun stapler staple ClipGauge.app
   ```

   Then make the `.dmg`. Sign, notarize and staple the dmg as well.
6. **Build location.** Build and sign on APFS (`/tmp` or ~). exFAT adds `._*` AppleDouble files that break the signature. `build.sh` already builds in /tmp and copies with `ditto --norsrc`.
7. **Distribution.**
   - GitHub Release assets: `ClipGauge-0.x.dmg` plus a SHA-256.
   - A Homebrew tap cask (live since v0.8.3): `brew install --cask stevencombs/tap/clipgauge`. See *Homebrew cask* below.
   - Sparkle auto-update is optional later. It needs an EdDSA key and an appcast.

## Homebrew cask

The cask is `Casks/clipgauge.rb` in [stevencombs/homebrew-tap](https://github.com/stevencombs/homebrew-tap), the same
tap as GrokGauge. It installs `ClipGauge.app` into /Applications, depends on the `ffmpeg`, `ollama` and `whisper.cpp`
formulae and macOS 14+, clears the quarantine flag on the app only (ad-hoc signed, not notarized), and never downloads
models. `brew uninstall --zap` removes only ClipGauge's per-Mac files in ~/Library, never a project folder or models.

**After each release** (once `ClipGauge-<version>.zip` and its `.sha256` are on the GitHub release), bump the cask:

```sh
app/ClipGauge/bump-cask.sh            # version from VERSION: downloads the zip, checks the .sha256, edits the cask, brew style + audit
app/ClipGauge/bump-cask.sh 0.8.4 --push   # same, then commits "clipgauge 0.8.4" as retroCombs and pushes the tap
```

The script only runs when you invoke it. By hand it's two lines in the cask: `version "<new>"` and
`sha256 "<shasum -a 256 of the zip>"`, then `brew style` / `brew audit --cask --tap stevencombs/tap` and a commit.
The cask's `livecheck` follows the latest GitHub release, so `brew livecheck --cask stevencombs/tap/clipgauge` shows
when it's behind.

## 5. Privacy statement (for README / About / release notes)

> ClipGauge and the AI Video Renamer run entirely on your Mac.
>
> - Video frames, audio, transcripts and file names are processed by local models (Ollama + whisper.cpp) and never leave your computer.
> - There are no accounts, no analytics and no telemetry.
> - The app talks only to `127.0.0.1` (the local Ollama server, for renaming and *Ask the Model*). Nothing is sent off the Mac.
> - Internet access is used only when *you* run setup, to install tools from Homebrew and download models from Ollama/Hugging Face.
> - Renames are logged in your project's `logs/` folder and can be undone.

## 6. Before a public release (checklist)

- [ ] Owner approves the repo name, visibility and license
- [ ] Move the engine to `engine/` in the repo. `build.sh` reads from there
- [ ] Replace the personal `config.json` with `config.example.json`. Starter vocabulary for project and subject names
- [ ] Remove the hard-coded drive fallback, or keep it harmless (it's only tried if it exists)
- [ ] `THIRD_PARTY_NOTICES.md` with the licenses above
- [ ] Screenshots: main window (Add Clips, Results), Ask the Model, popover, Setup, About
- [x] v0.8: removed the legacy web UI (`scripts/web_ui.py`, config `ui`)
- [ ] Developer ID signing + notarization + stapling, universal build
- [ ] Test on a clean macOS user account: Setup → Create Project → Install Missing → Re-check → first dry run
- [ ] Release notes plus the privacy statement

> **Name note:** the app was called *ClipGuage* through v0.5 (bundle id `com.retrocombs.ClipGuage`). From v0.6 it is **ClipGauge** (`com.retrocombs.ClipGauge`). The app migrates its old preferences on first launch.
