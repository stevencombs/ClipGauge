# Setting up ClipGauge + AI Video Renamer

ClipGauge is a menu bar app that runs a local AI video renamer. The renamer:

- Samples 9 frames from each clip.
- Transcribes any speech.
- Asks a local vision model what the clip shows.
- Names it `{YYYYMMDD}_{project}_{subject}_{clipType}[_t##].ext`.

Low-confidence clips keep their original name and are flagged **needs review**.

**Everything runs on your Mac.** No clip, frame, audio or transcript is uploaded anywhere. The network is used only to download the tools and models during setup.

---

## What you need

| | Minimum | Notes |
|---|---|---|
| Mac | Apple silicon (M1 or later) recommended, macOS 14 Sonoma or newer | Intel works, but the vision model is very slow |
| Memory | 16 GB | 16 GB uses the **Air tier** (qwen2.5vl:7b + whisper base.en). 48 GB or more uses the **Pro tier** (larger models) |
| Disk | ~10 GB free for models, plus room for your clips | The project folder can live on an external drive (APFS, exFAT or HFS+) |
| Software | Homebrew, Python 3.10+, ffmpeg, Ollama, whisper.cpp | **Setup › Install Missing…** installs these with your approval |

## 1. Open ClipGauge

Double-click **ClipGauge.app**. The first time, macOS may say it can't verify the developer, because builds aren't notarized yet:

1. Right-click (or Control-click) **ClipGauge.app** and choose **Open**.
2. Click **Open** again.
3. If there's no **Open** button: go to System Settings › Privacy & Security, scroll down, and click **Open Anyway**.

ClipGauge appears in the menu bar as a small gauge with a ▶︎ inside. On a crowded menu bar it may sit behind the `»` overflow. ⌘-drag it to where you want it.

## 2. Choose or create a project folder

On a Mac with no project folder yet, ClipGauge opens **Setup** automatically. Later you can reach it from the gear in the popover, then **Setup…**.

- **Existing project:** click **Choose or Create…** and pick the `AI-Video-Renamer` folder, for example on an external drive.
- **New project:** pick any folder or drive. ClipGauge creates `AI-Video-Renamer/` there, containing:
  - `scripts/`: the renamer, copied from inside the app.
  - `config/`: settings. New projects start in **dry-run** mode, so nothing is renamed.
  - `inbox/`: drop clips here.
  - `models/`: the AI models, so they travel with the drive.
  - `logs/`, `notes/`, `exports/`, `processing/`: working files.

ClipGauge never overwrites a file that already exists.

## 3. Check and install requirements

Setup lists each requirement with a ✓, ✗ or ⚠︎:

- **Homebrew:** the package manager. Installs via the official script from brew.sh, which asks for your Mac password itself.
- **Python 3.10+, ffmpeg, Ollama, whisper.cpp:** installed with `brew install …`.
- **Ollama server:** must be running with `OLLAMA_MODELS` pointing at `<project>/models`, so models are stored in the project. The setup script can create a login item (`~/Library/LaunchAgents/com.clipguage.ollama.plist`) that does this.
- **Vision model:** `qwen2.5vl:7b`, about 6 GB, on the Air tier. The Pro tier prefers `gemma3:12b`.
- **Speech model:** `ggml-base.en.bin`, about 140 MB, or `large-v3-turbo` (about 1.6 GB) on the Pro tier.
- **RAM tier:** picked automatically from your Mac's memory. Override it with `AI_VIDEO_RENAMER_TIER=air|pro` or `override_tier` in `config/ram-tiers.json`.

Click **Install Missing…**. Terminal opens `scripts/setup-mac.sh`, which:

- Shows what's missing.
- **Asks before every install or download.** Answer `y` to go ahead, or press Return to skip.
- Can be re-run any time. Use `scripts/setup-mac.sh --check` to only look, without changing anything.

On a slow connection, skip the model downloads and re-run later. Downloads resume where they stopped. When it finishes, go back to Setup and click **Re-check**.

## 4. First run

1. Drop a few clips on the ClipGauge window (or its menu bar icon), or copy them into `inbox/` in Finder.
2. Click **Start**. In dry-run mode you get proposed names only.
3. Review the names on the Renamer page (**Open Renamer page**).
4. When you're happy, set `"dry_run": false` in `config/config.json`. From then on, confident clips are renamed in place as each one finishes.

Every rename is logged in `logs/rename-log.jsonl` and can be undone:

```bash
python3 scripts/undo_renames.py --all --dry-run          # preview undoing everything
python3 scripts/undo_renames.py --file NEWNAME.mp4       # undo one clip
python3 scripts/undo_renames.py --since 2026-10-08T09:00 # undo a session
```

## Adding clips and reviewing results

Open the **ClipGauge** window (popover › **Open**) and drop clips or folders on it, or drop them on the menu bar icon.
Choose **Copy into inbox** (the usual way) or **Process in place** (renames the files where they are; folders on hold,
Resolve's own folders and Cloud-synced projects are refused, and inside DaVinci Resolve it asks first). The
**Results** tab lists every clip with its new name; **Undo** and **Review…** are there too. Everything is logged and
undoable.

## Asking the model

Type in the popover's **Ask the model…** box or press ⌘K. The chat runs on your Mac with Ollama; you can attach an
image or a clip's notes and transcript, and **Save** it to `exports/chats/`. It waits while a run or an update is
active.

## Keeping models and tools up to date

Open **Setup**, or use the gear menu › **Check for Updates…**, then click **Check Now**. ClipGauge compares:

- your Ollama models with ollama.com
- your Homebrew tools with Homebrew
- your Whisper files with Hugging Face

Nothing is downloaded during a check. Each update shows its download size. **Upgrade All…** shows the total and an estimated time before anything starts.

Upgrades run in the background, so you can close the window, and pick up where they left off if interrupted. They wait while a processing run is active or Resolve is open, and processing waits for them in turn. A weekly check (checks only) is on by default.

From Terminal:

```bash
python3 scripts/check_updates.py
python3 scripts/apply_updates.py --all
```

## Sorting clips into Resolve projects

Use the gear menu › **Sort into Projects…**, or the button in the popover.

1. Pick a source: the inbox, a folder under **DaVinci Resolve/**, or **Choose Folder…**.
2. Click **Dry Run**. Nothing moves. ClipGauge groups the clips by shoot and shows where each group would go:
   - an existing project ("Merge into …") or a new one
   - the A-Roll, B-Roll, Images and _Review counts
   - anything it wants you to check, such as a proposed name or "looks like two shoots"
3. Adjust the plan: rename projects, untick groups to leave them, or tick **Split** if a folder holds two shoots.
4. Click **Apply…**. It's greyed out while DaVinci Resolve is open, because clips that are already in a Resolve project go offline when they move and need relinking.

**Undo Last Sort…** puts everything back. Folders on hold (`hold_sources` in `config/project-sort.json`) and Blackmagic Cloud-synced projects are never touched.

## Custom instructions

ClipGauge › gear › **Instructions…** holds standing instructions (every clip), next-run instructions (this batch; a
`Project: …` line also names the shoot in Sort into Projects) and a glossary of names for better Whisper spelling.
**Preview Prompt…** shows exactly what the model gets. Your text can steer names and spellings but can't change the
output format. The popover shows **Instructions: Active** while any of it is set.

## Good to know

- **DaVinci Resolve:** processing won't start while Resolve is open. ClipGauge shows *Paused, Resolve is open*.
- **Unreadable clips:** an empty or interrupted recording (for example a 1 KB MP4) is marked **needs review** with the reason, and the run carries on.
- **External drives:** quit ClipGauge before ejecting the drive it runs from, or copy ClipGauge to /Applications. From there it finds the project on any connected drive, and **Open at login** works.
- **exFAT drives:** don't create symlinks inside the project. exFAT can't store them.
- **Status from Terminal:** `python3 scripts/status.py show`.

## Troubleshooting

| Symptom | What to do |
|---|---|
| "Couldn't start processing" | The alert shows the real reason and the run log path (`logs/clipgauge-run-*.log`) |
| Vision model ⚠︎ "Ollama isn't serving this folder" | Ollama was started without `OLLAMA_MODELS=<project>/models`. Re-run `scripts/setup-mac.sh` and let it create the login item, or quit Ollama and start `OLLAMA_MODELS=<project>/models ollama serve` |
| Nothing happens on Start | Every clip in `inbox/` is already renamed or needs review. Add new clips |
| Clips stay **needs review** | Open them from the Inbox card. Rename by hand, or re-run one clip with `python3 scripts/run_pipeline.py --video inbox/CLIP.MP4 --force` |
