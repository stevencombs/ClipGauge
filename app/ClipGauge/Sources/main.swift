// ClipGauge — the all-in-one menu bar app for AI-Video-Renamer (v0.6; was "ClipGuage" ≤ v0.5). Styled after GrokGauge.
//   ClipGauge.app                      menu bar app (LSUIElement) + main window + Ask the Model chat
//   .../MacOS/ClipGauge --print-state  one poll, print the derived state (for scripts / debugging)
//   .../MacOS/ClipGauge --render-preview DIR [--chat-prompt TEXT] [--only-v061|--only-v07|--only-v072]   write the preview PNGs offscreen
//   .../MacOS/ClipGauge --sync-selftest                          settings-sync checks in a temporary folder
import AppKit
import Combine
import SwiftUI

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, NSDraggingDestination {
    private var statusItem: NSStatusItem!
    private let popover = NSPopover()
    let model = RenamerModel()
    private var cancellables = Set<AnyCancellable>()
    private var lastLogged = ""
    private lazy var setup = SetupModel(renamer: model)
    private lazy var setupWindow = HostedWindow(title: "ClipGauge Setup", autosave: "ClipGaugeSetup") { [unowned self] in
        AnyView(SetupView(setup: self.setup, model: self.model))
    }
    private lazy var sort = SortModel(renamer: model)
    private lazy var sortWindow = HostedWindow(title: "Sort into Projects", autosave: "ClipGaugeSort") { [unowned self] in
        AnyView(SortView(sort: self.sort, model: self.model))
    }
    private lazy var instructions: InstructionsModel = {
        let i = InstructionsModel(renamer: model)
        i.showPreview = { [weak self] in self?.promptWindow.show() }
        return i
    }()
    private lazy var instructionsWindow = HostedWindow(title: "Custom Instructions", autosave: "ClipGaugeInstructions") { [unowned self] in
        AnyView(InstructionsView(ins: self.instructions, model: self.model))
    }
    private lazy var promptWindow = HostedWindow(title: "Prompt Preview", autosave: "ClipGaugePromptPreview") { [unowned self] in
        AnyView(PromptPreviewView(ins: self.instructions))
    }
    private lazy var main = MainModel(renamer: model)
    private lazy var mainWindow = HostedWindow(title: kAppName, autosave: "ClipGaugeMain", resizable: true,
                                               minSize: NSSize(width: 860, height: 560)) { [unowned self] in
        AnyView(MainView(main: self.main, model: self.model))
    }
    private lazy var chat = ChatModel(renamer: model)
    private lazy var chatWindow = HostedWindow(title: "Ask the Model", autosave: "ClipGaugeChat", resizable: true,
                                               minSize: NSSize(width: 560, height: 480)) { [unowned self] in
        AnyView(ChatView(chat: self.chat, model: self.model))
    }
    private lazy var settingsWindow = HostedWindow(title: "ClipGauge Settings", autosave: "ClipGaugeSettings") { [unowned self] in
        AnyView(SettingsView(store: LayoutStore.shared, model: self.model, openSetup: { [weak self] in self?.openSetup() }))
    }

    func openSetup() {
        if popover.isShown { popover.performClose(nil) }
        setup.recheck()
        setupWindow.show()
    }

    /// Gear › Check for Updates…: open Setup and run an explicit check (brew update + registry/HF lookups; no downloads).
    func openUpdates() {
        openSetup()
        model.updates.check(explicit: true)
    }

    /// Gear › Sort into Projects… / popover button: dry run first, apply is a separate confirmed step.
    func openSort() {
        if popover.isShown { popover.performClose(nil) }
        sort.reload()
        sortWindow.show()
    }

    /// Gear › Instructions… / popover Instructions row: standing + next-run instructions and the glossary.
    func openInstructions() {
        if popover.isShown { popover.performClose(nil) }
        if !instructions.dirty { instructions.load() }
        instructionsWindow.show()
    }

    /// The main window (add clips, results, undo, tools). Dropped items are queued on the Add Clips tab.
    func openMain(adding urls: [URL] = []) {
        if popover.isShown { popover.performClose(nil) }
        mainWindow.show()
        if !urls.isEmpty { main.add(urls) }
    }

    /// Ask the Model; with a prompt it is sent right away (unless a run/update blocks it — then it waits in the box).
    func openChat(_ prompt: String? = nil) {
        if popover.isShown { popover.performClose(nil) }
        chatWindow.show()
        if let p = prompt, !p.isEmpty {
            if chat.blocker == nil && !chat.streaming { chat.send(p) } else { chat.input = p; chat.error = chat.blocker }
        }
    }

    /// Settings (v0.6.1): rearrange the popover and choose what the menu bar shows (GrokGauge's Layout / Menu Bar).
    func openSettings(_ tab: SettingsTab? = nil) {
        if popover.isShown { popover.performClose(nil) }
        if let tab { SettingsNav.shared.tab = tab }
        settingsWindow.show()
    }

    /// About is a Settings tab, as in GrokGauge.
    func openAbout() { openSettings(.about) }

    /// v0.7: apply the parts of Settings that live outside SwiftUI (shortcut, update check).
    private func apply(_ s: ClipGaugeSettings, old: ClipGaugeSettings?) {
        if old?.hotKey != s.hotKey { HotKeyCenter.shared.register(s.hotKey) }
        if old?.checkForUpdates != s.checkForUpdates { AppUpdateMonitor.shared.setEnabled(s.checkForUpdates) }
    }
    private var appliedSettings: ClipGaugeSettings?

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.autosaveName = kAppName
        statusItem.isVisible = true
        if let button = statusItem.button {
            button.imagePosition = .imageLeading
            button.imageHugsTitle = true
            button.target = self
            button.action = #selector(togglePopover(_:))
            button.setAccessibilityLabel(kAppName)
            // Drop clips/folders on the menu bar icon: the status item's window forwards drags to its delegate.
            button.window?.registerForDraggedTypes([.fileURL])
            button.window?.delegate = self
        }
        let host = NSHostingController(rootView: PopoverView(model: model))
        host.sizingOptions = .preferredContentSize
        popover.contentViewController = host
        popover.behavior = .transient
        popover.animates = true

        model.beforeModal = { [weak self] in
            if self?.popover.isShown == true { self?.popover.performClose(nil) }
        }
        model.showSetup = { [weak self] in self?.openSetup() }
        model.showAbout = { [weak self] in self?.openAbout() }
        model.showSettings = { [weak self] in self?.openSettings() }
        model.showUpdates = { [weak self] in self?.openUpdates() }
        model.showSort = { [weak self] in self?.openSort() }
        model.showInstructions = { [weak self] in self?.openInstructions() }
        model.showMain = { [weak self] in self?.openMain() }
        model.showChat = { [weak self] p in self?.openChat(p) }
        model.onRunStarted = { [weak self] in self?.main.tab = .results; self?.main.loadResults() }
        MainMenu.install(target: self)
        model.objectWillChange
            .receive(on: RunLoop.main)
            .sink { [weak self] _ in DispatchQueue.main.async { self?.updateStatusItem() } }
            .store(in: &cancellables)
        AppUpdateMonitor.shared.objectWillChange   // "Update available" in the tooltip, as in GrokGauge
            .receive(on: RunLoop.main)
            .sink { [weak self] _ in DispatchQueue.main.async { self?.updateStatusItem() } }
            .store(in: &cancellables)
        LayoutStore.shared.objectWillChange   // Settings › Menu Bar changes apply right away
            .receive(on: RunLoop.main)
            .sink { [weak self] _ in DispatchQueue.main.async { self?.updateStatusItem() } }
            .store(in: &cancellables)
        // v0.7: GrokGauge's core services — global shortcut, app update check, settings sync.
        HotKeyCenter.shared.action = { [weak self] in self?.togglePopover(nil) }
        AppUpdateMonitor.shared.root = { [weak self] in self?.model.root }
        LayoutStore.shared.$settings
            .receive(on: RunLoop.main)
            .sink { [weak self] s in
                guard let self else { return }
                self.apply(s, old: self.appliedSettings)
                self.appliedSettings = s
            }
            .store(in: &cancellables)
        LayoutStore.shared.start()
        model.start()
        updateStatusItem()
        // First run on a Mac without a project folder: open Setup instead of a blank gauge.
        if model.root == nil && Project.remembered == nil {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in self?.openSetup() }
        }
    }

    @objc func openSetupAction(_ sender: Any?) { openSetup() }
    @objc func openAboutAction(_ sender: Any?) { openAbout() }
    @objc func openSettingsAction(_ sender: Any?) { openSettings() }
    @objc func openUpdatesAction(_ sender: Any?) { openUpdates() }
    @objc func openSortAction(_ sender: Any?) { openSort() }
    @objc func openInstructionsAction(_ sender: Any?) { openInstructions() }
    @objc func openMainAction(_ sender: Any?) { openMain() }
    @objc func openChatAction(_ sender: Any?) { openChat() }
    /// Opens the PayPal tip page in the browser (nothing is sent by ClipGauge).
    @objc func openSupportAction(_ sender: Any?) { NSWorkspace.shared.open(AppInfo.tipURL) }

    // MARK: drop on the menu bar icon
    private func droppedURLs(_ info: NSDraggingInfo) -> [URL] {
        (info.draggingPasteboard.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL]) ?? []
    }
    func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
        guard model.lexarOK, !droppedURLs(sender).isEmpty else { return [] }
        statusItem.button?.highlight(true)
        return .copy
    }
    func draggingExited(_ sender: NSDraggingInfo?) { statusItem.button?.highlight(false) }
    func prepareForDragOperation(_ sender: NSDraggingInfo) -> Bool { true }
    func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        statusItem.button?.highlight(false)
        let urls = droppedURLs(sender)
        guard !urls.isEmpty else { return false }
        DispatchQueue.main.async { self.openMain(adding: urls) }
        return true
    }

    private func updateStatusItem() {
        guard let button = statusItem?.button else { return }
        let r = StatusItemLook(model)
        button.image = r.image
        button.attributedTitle = r.title
        let tip = r.tooltip + (AppUpdateMonitor.shared.available.map { "\nUpdate available: \($0)" } ?? "")
        button.toolTip = tip
        button.setAccessibilityValue(tip)
        let line = "\(r.title.string)|\(r.tooltip)"
        if line != lastLogged {
            lastLogged = line
            let f = button.window?.frame ?? .zero
            NSLog("ClipGauge status item: title='%@' tip='%@' (x=%.0f w=%.0f)", r.title.string, r.tooltip, f.origin.x, f.size.width)
        }
    }

    @objc private func togglePopover(_ sender: Any?) {
        guard let button = statusItem.button else { return }
        if popover.isShown {
            popover.performClose(sender)
        } else {
            model.tick()
            model.refreshInbox(force: true)
            popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
            NSApp.activate(ignoringOtherApps: true)
            popover.contentViewController?.view.window?.makeKey()
        }
    }
}

/// What the menu bar shows (v0.6.1: depends on Settings › Menu Bar). Default: the ClipGauge mark (template) + a
/// semibold monospaced-digit percent while a run is active, coloured like GrokGauge's levels. Orange-tinted mark =
/// paused for Resolve; "–" = Lexar not connected.
struct StatusItemLook {
    let image: NSImage?
    let title: NSAttributedString
    let tooltip: String

    /// Everything the look depends on (so Settings can preview sample states without a live model).
    struct Inputs {
        var lexarOK = true
        var progress: (pct: Int, index: Int, total: Int)? = nil
        var eta: Date? = nil
        var runActive = false
        var update: (pct: Int, current: String?, eta: Date?)? = nil
        var resolveRunning = false
        var needsReview = 0
        var problem = false
    }

    enum Sample { case idleReview, processing, paused }

    static func sample(_ k: Sample, settings: LayoutSettings) -> StatusItemLook {
        var i = Inputs()
        switch k {
        case .idleReview: i.needsReview = 3
        case .processing: i.progress = (42, 3, 7); i.runActive = true; i.eta = Date().addingTimeInterval(185)
        case .paused: i.resolveRunning = true
        }
        return StatusItemLook(i, settings: settings, forceWhite: true)
    }

    init(_ m: RenamerModel, forceWhite: Bool = false, settings: LayoutSettings = LayoutStore.shared.settings) {
        var i = Inputs()
        i.lexarOK = m.lexarOK
        if let p = m.progress { i.progress = (p.pct, p.index, p.total) }
        i.eta = m.eta
        i.runActive = m.runActive
        if m.updates.jobActive, let j = m.updates.job { i.update = (j.percent, j.current, j.eta) }
        i.resolveRunning = m.resolveRunning
        i.needsReview = m.needsReviewCount
        i.problem = m.problemVisible
        self.init(i, settings: settings, forceWhite: forceWhite)
    }

    init(_ m: Inputs, settings s: LayoutSettings, forceWhite: Bool = false) {
        let font = NSFont.monospacedDigitSystemFont(ofSize: NSFont.systemFontSize, weight: .semibold)
        let mode = s.menuBarMode
        var text = ""
        var color: NSColor = forceWhite ? NSColor.white.withAlphaComponent(0.6) : .secondaryLabelColor
        var tip: String
        var tint: NSColor? = nil
        var busy = false
        if !m.lexarOK {
            text = " –"; tip = "ClipGauge — Lexar not connected"
        } else if let p = m.progress {
            text = " \(p.pct)%"; color = Level.normal.nsColor; tint = Level.normal.nsColor; busy = true
            tip = "ClipGauge — processing clip \(max(1, p.index)) of \(p.total) (\(p.pct)%)"
            if let e = m.eta {
                tip += ", ETA \(clockString(e))"
                if s.showETA { text += " · \(Self.minutesLeft(e))" }
            }
        } else if m.runActive {
            text = " …"; tip = "ClipGauge — busy"; tint = Level.normal.nsColor; busy = true
        } else if let u = m.update {
            text = " ↓\(u.pct)%"; color = .systemBlue; tint = .systemBlue; busy = true
            tip = "ClipGauge — updating \(u.current ?? "models & tools") (\(u.pct)%)"
            if let e = u.eta {
                tip += ", done ≈ \(clockString(e))"
                if s.showETA { text += " · \(Self.minutesLeft(e))" }
            }
        } else if m.resolveRunning {
            tint = Level.warning.nsColor; tip = "ClipGauge — paused, Resolve is open"
        } else {
            tip = "ClipGauge — idle"
            if m.problem { tint = Level.critical.nsColor; tip += " (last run had errors)" }
        }
        if m.needsReview > 0 { tip += "\n\(m.needsReview) clip(s) need review" }
        var img: NSImage? = forceWhite ? ClipMarkImage.tinted(.white) : ClipMarkImage.template(size: 18)
        switch mode {
        case .iconAndPercent:
            if m.resolveRunning && !busy { img = ClipMarkImage.tinted(Level.warning.nsColor) }
        case .iconOnly:
            text = m.lexarOK ? "" : text
            if let t = tint { img = ClipMarkImage.tinted(t) }
        case .percentOnly:
            if busy && !text.isEmpty { img = nil; text = text.trimmingCharacters(in: .whitespaces) }
            if m.resolveRunning && !busy { img = ClipMarkImage.tinted(Level.warning.nsColor) }
        case .iconAndReview:
            if m.resolveRunning && !busy { img = ClipMarkImage.tinted(Level.warning.nsColor) }
            if !busy && m.lexarOK && m.needsReview > 0 { text = " \(m.needsReview)"; color = Level.warning.nsColor }
        }
        // "Hide the ClipGauge logo" (as in GrokGauge): only when there's text to show, so the item never vanishes.
        if s.hideLogo && mode != .iconOnly && !text.trimmingCharacters(in: .whitespaces).isEmpty {
            img = nil
            text = text.trimmingCharacters(in: .whitespaces)
        }
        image = img
        title = NSAttributedString(string: text, attributes: [.font: font, .foregroundColor: color])
        tooltip = tip
    }

    static func minutesLeft(_ e: Date) -> String {
        let s = max(0, e.timeIntervalSinceNow)
        return s < 90 ? "<2m" : s < 3600 ? "\(Int((s / 60).rounded()))m" : String(format: "%.1fh", s / 3600)
    }
}

/// Mocked update states for --render-preview (nothing is checked or downloaded).
enum PreviewData {
    static func updates(_ m: RenamerModel, running: Bool) -> UpdatesModel {
        let u = UpdatesModel()
        u.renamer = m
        func item(_ id: String, _ kind: String, _ name: String, _ status: String, _ detail: String, role: String? = nil,
                  dl: Int64? = nil, action: String = "Upgrade", link: String? = nil) -> [String: Any] {
            var d: [String: Any] = ["id": id, "kind": kind, "name": name, "status": status, "detail": detail,
                                    "upgradable": status == "outdated" || status == "missing", "action": action]
            if let role { d["role"] = role }
            if let dl { d["download_bytes"] = NSNumber(value: dl) }
            if let link { d["link"] = link }
            return d
        }
        u.apply(state: [
            "checked_at": isoParser.string(from: Date()), "reason": "manual", "brew_updated": true,
            "items": [
                item("model:qwen2.5vl:7b", "model", "qwen2.5vl:7b", "outdated",
                     "New version on ollama.com — 412 MB to download", role: "tier", dl: 412_000_000),
                item("tool:ollama", "tool", "Ollama", "outdated", "0.35.1_1 → 0.40.0 · ~17 MB (plus any outdated dependencies)", dl: 17_124_651),
                item("tool:whisper.cpp", "tool", "whisper.cpp", "outdated", "1.9.4 → 1.9.5 · ~2.1 MB (plus any outdated dependencies)", dl: 2_100_000),
                item("tool:ffmpeg", "tool", "FFmpeg", "current", "9.0.2 · up to date"),
                item("whisper:ggml-base.en.bin", "whisper", "ggml-base.en.bin", "outdated",
                     "Changed on Hugging Face — 148 MB to download (old file kept as .bak until verified)", role: "tier", dl: 147_964_211),
                item("advisory:qwen3.5:9b", "advisory", "Qwen3.5 9B (vision)", "info",
                     "qwen3.5:9b (6.6 GB) — newer Qwen family with image input. Info only: try it on a few clips before switching.",
                     link: "https://ollama.com/library/qwen3.5"),
            ],
        ])
        if running {
            let eta = isoParser.string(from: Date().addingTimeInterval(7 * 60))
            u.job = UpdateJob([
                "state": "running", "percent": 42, "speed_bps": 3_400_000, "eta": eta, "current": "qwen2.5vl:7b",
                "item_index": 3, "item_total": 4, "message": "qwen2.5vl:7b: pulling a99b7f834d75",
                "items": [["id": "tool:ollama", "state": "done", "message": "now 0.40.0 · restarted com.retrocombs.ollama-lexar (Ollama 0.40.0)"],
                          ["id": "tool:whisper.cpp", "state": "done", "message": "now 1.9.5"],
                          ["id": "model:qwen2.5vl:7b", "state": "running", "message": "pulling a99b7f834d75 — 173 of 412 MB"],
                          ["id": "whisper:ggml-base.en.bin", "state": "pending", "message": ""]],
            ])
            u.jobActive = true
        }
        return u
    }
}

extension PreviewData {
    /// Mocked "mixed SD card" dry run (existing-project match, content-named new group, two-shoots split option, _Review).
    static func sortMock(_ m: RenamerModel) -> SortModel {
        let s = SortModel(renamer: m)
        let o = (try? JSONSerialization.jsonObject(with: Data(kMockSortPlanJSON.utf8))) as? [String: Any] ?? [:]
        s.sources = [SortSource(["kind": "inbox", "name": "inbox", "note": "the renamer's inbox/", "sortable": true]),
                     SortSource(["kind": "raw", "name": "CAM_20261009", "path": o["source"] as? String ?? "", "note": "folder of clips (not sorted yet)", "sortable": true]),
                     SortSource(["kind": "held", "name": "Held Project", "note": "on hold", "sortable": false]),
                     SortSource(["kind": "synced", "name": "AOHi 280W", "note": "Cloud-synced", "sortable": false]),
                     SortSource(["kind": "synced", "name": "WiiM Ultra", "note": "Cloud-synced", "sortable": false])]
        s.resolveRoot = "/Volumes/Lexar/DaVinci Resolve"
        s.selected = o["source"] as? String ?? "inbox"
        s.load(plan: SortPlan(o))
        s.lastSort = ("20261007-181502-gl-inet-be3600", "2026-10-07T18:15:02-10:00", 12, false)
        return s
    }
}

extension PreviewData {
    /// Example instructions (sample data) for --render-preview; never saved.
    static func instructionsMock(_ m: RenamerModel) -> InstructionsModel {
        let i = InstructionsModel(renamer: m)
        i.fill(["standing": "I make retro-tech videos. Call the router “Acme R3000” (not Acme-R3000 or acme r3000).\nPrefer real product names over generic words when they're visible or spoken.",
                "next_run": ["text": "Project: Retro Game Expo\nThis batch is the Retro Game Expo at the Convention Center.", "keep": false],
                "glossary": ["Commodore 64", "Acme", "Raspberry Pi", "Amiga 500"],
                "updated_at": "2026-10-08T11:20:00-10:00"])
        return i
    }
}

/// An accessory app has no visible menu bar, but a main menu still gives windows ⌘W / ⌘Q / copy-paste (as in GrokGauge).
enum MainMenu {
    static func install(target: AnyObject) {
        let main = NSMenu()
        let appItem = NSMenuItem()
        let app = NSMenu()
        let about = NSMenuItem(title: "About \(kAppName)", action: #selector(AppDelegate.openAboutAction(_:)), keyEquivalent: "")
        about.target = target
        app.addItem(about)
        app.addItem(.separator())
        let mainItem = NSMenuItem(title: "Open \(kAppName)", action: #selector(AppDelegate.openMainAction(_:)), keyEquivalent: "0")
        mainItem.target = target
        app.addItem(mainItem)
        let ask = NSMenuItem(title: "Ask the Model…", action: #selector(AppDelegate.openChatAction(_:)), keyEquivalent: "k")
        ask.target = target
        app.addItem(ask)
        app.addItem(.separator())
        let settings = NSMenuItem(title: "Settings…", action: #selector(AppDelegate.openSettingsAction(_:)), keyEquivalent: ",")
        settings.target = target
        app.addItem(settings)
        let setup = NSMenuItem(title: "Setup…", action: #selector(AppDelegate.openSetupAction(_:)), keyEquivalent: ",")
        setup.keyEquivalentModifierMask = [.command, .option]   // ⌘, is Settings since v0.6.1 (as in GrokGauge)
        setup.target = target
        app.addItem(setup)
        let upd = NSMenuItem(title: "Check for Updates…", action: #selector(AppDelegate.openUpdatesAction(_:)), keyEquivalent: "")
        upd.target = target
        app.addItem(upd)
        let srt = NSMenuItem(title: "Sort into Projects…", action: #selector(AppDelegate.openSortAction(_:)), keyEquivalent: "")
        srt.target = target
        app.addItem(srt)
        let ins = NSMenuItem(title: "Instructions…", action: #selector(AppDelegate.openInstructionsAction(_:)), keyEquivalent: "i")
        ins.target = target
        app.addItem(ins)
        let support = NSMenuItem(title: "Support \(kAppName)…", action: #selector(AppDelegate.openSupportAction(_:)), keyEquivalent: "")
        support.target = target
        app.addItem(support)
        app.addItem(.separator())
        app.addItem(NSMenuItem(title: "Quit \(kAppName)", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
        appItem.submenu = app
        main.addItem(appItem)
        let editItem = NSMenuItem()
        let edit = NSMenu(title: "Edit")
        edit.addItem(NSMenuItem(title: "Undo", action: Selector(("undo:")), keyEquivalent: "z"))
        edit.addItem(NSMenuItem(title: "Redo", action: Selector(("redo:")), keyEquivalent: "Z"))
        edit.addItem(.separator())
        edit.addItem(NSMenuItem(title: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x"))
        edit.addItem(NSMenuItem(title: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c"))
        edit.addItem(NSMenuItem(title: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v"))
        edit.addItem(NSMenuItem(title: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a"))
        editItem.submenu = edit
        main.addItem(editItem)
        let winItem = NSMenuItem()
        let win = NSMenu(title: "Window")
        win.addItem(NSMenuItem(title: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w"))
        winItem.submenu = win
        main.addItem(winItem)
        NSApp.mainMenu = main
    }
}

// MARK: - CLI helpers (no status item)

enum CLI {
    static func settle(_ m: RenamerModel) {
        m.tick()
        m.refreshInbox(force: true)
        RunLoop.main.run(until: Date().addingTimeInterval(4.0))  // engine: inbox counts + status/problem card
        m.tick()
    }

    static func printState() -> Int32 {
        let m = RenamerModel(preview: true)
        settle(m)
        let look = StatusItemLook(m)
        let p = m.progress
        let lines = [
            "root: \(m.root?.path ?? "(not found)")",
            "lexar_ok: \(m.lexarOK)",
            "resolve_running: \(m.resolveRunning)",
            "engine_cli: \(m.root.map { fileExists($0.appendingPathComponent("scripts/clipgauge_cli.py")) } ?? false)",
            "problem: \(m.problem.map { "\($0["reason"] ?? "?") resolved=\($0["resolved"] ?? false) auto=\($0["auto"] ?? false) dismissed=\(m.problemDismissed) visible=\(m.problemVisible) hint=\($0["hint"] ?? "")" } ?? "-")",
            "vision_model: \(m.visionModel)",
            "layout: \(LayoutStore.shared.summary) · menubar_title=\"\(look.title.string)\"",
            "sync: \(LayoutStore.shared.syncFolder.map { SettingsView.abbreviate($0) } ?? "off") · grokgauge_folder=\(LayoutStore.grokGaugeSyncFolder().map { SettingsView.abbreviate($0) } ?? "-")",
            "app_update: running=\(AppInfo.version) source=\(m.root.flatMap { AppUpdateMonitor.sourceVersion(root: $0) } ?? "-") check_daily=\(LayoutStore.shared.settings.checkForUpdates)",
            "hotkey: \(LayoutStore.shared.settings.hotKey.enabled ? LayoutStore.shared.settings.hotKey.display : "off")",
            "run_active: \(m.runActive) pipeline=\(m.isPipelineRun) launched_by=\(m.launchedBy ?? "-")",
            "status_state: \(m.state) dry_run=\(m.runIsDry)",
            "progress: \(p.map { "\($0.pct)% clip \($0.index)/\($0.total)" } ?? "-")",
            "current_clip: \(m.currentClip ?? "-")",
            "eta: \(m.eta.map { clockString($0) } ?? "-")",
            "headline: \(m.headline) | \(m.subline)",
            "last_renamed: \(m.lastRenamed ?? "-") @ \(m.lastRenamedAt.map(shortStamp) ?? "-")",
            "needs_review: \(m.needsReviewCount) (inbox=\(m.inboxReview.map(String.init) ?? "-"), pending=\(m.inboxPending.map(String.init) ?? "-"))",
            "menu_bar_title: '\(look.title.string)'",
            "tooltip: \(look.tooltip.replacingOccurrences(of: "\n", with: " / "))",
            "in_applications: \(m.inApplications)",
            "bundled_engine: \(Project.bundledEngine?.path ?? "-")",
            "updates: checked=\(m.updates.checkedAt.map { shortStamp($0) } ?? "never") upgradable=\(m.updates.upgradable.count) (\(byteString(m.updates.totalDownload))) job=\(m.updates.job?.state ?? "-") active=\(m.updates.jobActive) auto_weekly=\(m.updates.autoCheck)",
        ]
        print(lines.joined(separator: "\n"))
        if CommandLine.arguments.contains("--checks") {
            for c in SetupModel.runChecks(root: m.root) { print("check \(c.id): \(c.state) — \(c.title) — \(c.detail)") }
        }
        return 0
    }

    /// --create-project DIR: same code path as Setup › Create Project (for testing / scripting).
    static func createProject(_ dir: URL) -> Int32 {
        do {
            let n = try SetupModel.createProject(at: dir)
            print("Created project at \(dir.path) (\(n) files copied)")
            return 0
        } catch {
            FileHandle.standardError.write(Data("error: \(error.localizedDescription)\n".utf8))
            return 1
        }
    }

    static func renderPreview(to dir: URL) -> Int32 {
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let m = RenamerModel(preview: true)
        settle(m)
        if CommandLine.arguments.contains("--only-v072") {
            let ok = renderV072(m, dir: dir)
            print(ok ? "Wrote v0.7.2 previews to \(dir.path)" : "Some previews failed")
            return ok ? 0 : 1
        }
        if CommandLine.arguments.contains("--only-v07") {
            let ok = renderV07(m, dir: dir)
            print(ok ? "Wrote v0.7 previews to \(dir.path)" : "Some previews failed")
            return ok ? 0 : 1
        }
        if CommandLine.arguments.contains("--only-v061") {
            let ok = renderV061(m, dir: dir)
            print(ok ? "Wrote v0.6.1 previews to \(dir.path)" : "Some previews failed")
            return ok ? 0 : 1
        }
        var ok = true
        for scheme in [ColorScheme.dark, .light] {
            let name = scheme == .dark ? "dark" : "light"
            ok = renderView(PopoverView(model: m), scheme: scheme, to: dir.appendingPathComponent("popover-\(name).png")) && ok
        }
        ok = renderMenuBar(m, to: dir.appendingPathComponent("menubar.png")) && ok
        ok = renderView(AboutView(model: m), scheme: .dark, to: dir.appendingPathComponent("about-dark.png")) && ok
        ok = renderView(AboutView(model: m), scheme: .light, to: dir.appendingPathComponent("about-light.png")) && ok
        let sm = SetupModel(renamer: m)
        sm.items = SetupModel.runChecks(root: m.root)
        sm.lastChecked = Date()
        ok = renderView(SetupView(setup: sm, model: m, scrolls: false), scheme: .dark, to: dir.appendingPathComponent("setup-dark.png")) && ok
        ok = renderView(SetupView(setup: sm, model: m, scrolls: false), scheme: .light, to: dir.appendingPathComponent("setup-light.png")) && ok
        // Setup › Updates on its own: real results (logs/updates-state.json), then two mocked states.
        for scheme in [ColorScheme.dark, .light] {
            let name = scheme == .dark ? "dark" : "light"
            ok = renderView(UpdatesSection(updates: m.updates, model: m).padding(20).frame(width: 560), scheme: scheme,
                            to: dir.appendingPathComponent("updates-real-\(name).png")) && ok
            ok = renderView(UpdatesSection(updates: PreviewData.updates(m, running: false), model: m).padding(20).frame(width: 560),
                            scheme: scheme, to: dir.appendingPathComponent("updates-mock-\(name).png")) && ok
        }
        let running = PreviewData.updates(m, running: true)
        ok = renderView(UpdatesSection(updates: running, model: m).padding(20).frame(width: 560), scheme: .dark,
                        to: dir.appendingPathComponent("updates-mock-running-dark.png")) && ok
        // menu bar + popover while an upgrade runs (mocked)
        m.updates.job = running.job
        m.updates.jobActive = true
        ok = renderMenuBar(m, to: dir.appendingPathComponent("menubar-updating.png")) && ok
        ok = renderView(PopoverView(model: m), scheme: .dark, to: dir.appendingPathComponent("popover-updating-dark.png")) && ok
        m.updates.jobActive = false
        m.updates.job = nil
        // Sort into Projects: mocked mixed-card dry run, plus real dry-run plans passed with --sort-plan FILE (read-only).
        let mock = PreviewData.sortMock(m)
        for scheme in [ColorScheme.dark, .light] {
            let name = scheme == .dark ? "dark" : "light"
            ok = renderView(SortView(sort: mock, model: m, scrolls: false), scheme: scheme, to: dir.appendingPathComponent("sort-mock-\(name).png")) && ok
        }
        if let e = mock.plan?.groups.first(where: { !$0.splitParts.isEmpty }) {
            mock.edits[e.id]?.split = true
            mock.edits["g2"]?.name = "Waikiki Sunset"
            mock.edits["g4"]?.include = false
            ok = renderView(SortView(sort: mock, model: m, scrolls: false), scheme: .dark, to: dir.appendingPathComponent("sort-mock-edited-dark.png")) && ok
        }
        let a = CommandLine.arguments
        var k = 0
        for (i, x) in a.enumerated() where x == "--sort-plan" && i + 1 < a.count {
            guard let o = readJSONObject(URL(fileURLWithPath: a[i + 1])) else { ok = false; continue }
            k += 1
            let s = SortModel(renamer: m)
            s.sources = mock.sources
            s.resolveRoot = o["resolve_root"] as? String ?? ""
            s.sources.append(SortSource(["kind": "project", "name": URL(fileURLWithPath: o["source"] as? String ?? "?").lastPathComponent,
                                         "path": o["source"] as? String ?? "", "note": "project in the A-Roll/B-Roll/Images layout", "sortable": true]))
            s.selected = o["source"] as? String ?? "inbox"
            s.load(plan: SortPlan(o))
            ok = renderView(SortView(sort: s, model: m, scrolls: false), scheme: .dark, to: dir.appendingPathComponent("sort-real-\(k)-dark.png")) && ok
        }
        ok = renderView(PopoverView(model: m), scheme: .dark, to: dir.appendingPathComponent("popover-sort-dark.png")) && ok
        // Custom instructions (v0.5): mocked text (nothing is saved); the prompt preview runs instructions.py --preview
        // --draft on that text (read-only). The popover indicator is shown as active.
        let ins = PreviewData.instructionsMock(m)
        for scheme in [ColorScheme.dark, .light] {
            let name = scheme == .dark ? "dark" : "light"
            ok = renderView(InstructionsView(ins: ins, model: m, scrolls: false), scheme: scheme,
                            to: dir.appendingPathComponent("instructions-\(name).png")) && ok
        }
        var previewDone = false
        ins.runPreview(open: false) { previewDone = true }
        let deadline = Date().addingTimeInterval(20)
        while !previewDone && Date() < deadline { RunLoop.main.run(until: Date().addingTimeInterval(0.1)) }
        if ins.preview == nil { FileHandle.standardError.write(Data("prompt preview failed: \(ins.error ?? "timeout")\n".utf8)); ok = false }
        ok = renderView(PromptPreviewView(ins: ins, scrolls: false), scheme: .dark, to: dir.appendingPathComponent("prompt-preview-dark.png")) && ok
        ok = renderView(PromptPreviewView(ins: ins, scrolls: false), scheme: .light, to: dir.appendingPathComponent("prompt-preview-light.png")) && ok
        m.instructions = InstructionsSummary(standing: true, nextRun: true, keep: false, glossaryTerms: 4)
        ok = renderView(PopoverView(model: m), scheme: .dark, to: dir.appendingPathComponent("popover-instructions-dark.png")) && ok
        ok = renderView(PopoverView(model: m), scheme: .light, to: dir.appendingPathComponent("popover-instructions-light.png")) && ok
        m.instructions = InstructionsSummary.read(root: m.root ?? URL(fileURLWithPath: "/nonexistent"))
        ok = renderV06(m, dir: dir) && ok
        print(ok ? "Wrote previews to \(dir.path)" : "Some previews failed")
        return ok ? 0 : 1
    }

    /// v0.6: main window (Add Clips empty + example queue, Results with example rows), Ask the Model (a real short
    /// reply from the local model when --chat-prompt is given and nothing is busy), popover and About.
    static func renderV06(_ m: RenamerModel, dir: URL) -> Bool {
        var ok = true
        let mm = MainModel(renamer: m)
        for scheme in [ColorScheme.dark, .light] {
            let name = scheme == .dark ? "dark" : "light"
            ok = renderView(MainView(main: mm, model: m, scrolls: false).frame(width: 980), scheme: scheme,
                            to: dir.appendingPathComponent("v06-main-add-\(name).png")) && ok
        }
        let ex = MainModel(renamer: m)
        ex.exampleData = true
        func q(_ path: String, _ c: [String: Any]) -> QueuedItem { var i = QueuedItem(url: URL(fileURLWithPath: path)); i.check = c; return i }
        ex.queue = [
            q("/Volumes/SD-CARD/DCIM/100MEDIA", ["status": "ok", "media_count": 14, "copy_count": 14, "bytes": NSNumber(value: 9_800_000_000), "copy_ok": true]),
            q("/Users/you/Desktop/IMG_0001.HEIC", ["status": "ok", "media_count": 1, "copy_count": 1, "bytes": NSNumber(value: 2_400_000), "copy_ok": true]),
            q("/Volumes/Lexar/DaVinci Resolve/Held Project", ["status": "refused", "reason": "“Held Project” is on hold — not touching it", "media_count": 0, "copy_count": 0, "copy_ok": false]),
        ]
        ex.folderName = "Retro Game Expo"
        ex.folderAsProject = true
        ok = renderView(MainView(main: ex, model: m, scrolls: false).frame(width: 980), scheme: .dark,
                        to: dir.appendingPathComponent("v06-main-add-example-dark.png")) && ok
        ex.mode = .inPlace
        ex.queue[0].check?["needs_confirm"] = false
        ex.queue.append(q("/Volumes/Lexar/DaVinci Resolve/Retro Game Expo/B-Roll", ["status": "needs_confirm", "needs_confirm": true, "media_count": 6, "copy_count": 6, "copy_ok": true, "reason": "inside the DaVinci Resolve folder"]))
        ok = renderView(MainView(main: ex, model: m, scrolls: false).frame(width: 980), scheme: .light,
                        to: dir.appendingPathComponent("v06-main-inplace-example-light.png")) && ok
        let res = MainModel(renamer: m)
        PreviewData.resultsMock(res)
        for scheme in [ColorScheme.dark, .light] {
            let name = scheme == .dark ? "dark" : "light"
            ok = renderView(MainView(main: res, model: m, scrolls: false).frame(width: 980), scheme: scheme,
                            to: dir.appendingPathComponent("v06-main-results-example-\(name).png")) && ok
        }
        res.startEdit(res.rows[2])
        res.filter = .review
        ok = renderView(MainView(main: res, model: m, scrolls: false).frame(width: 980), scheme: .dark,
                        to: dir.appendingPathComponent("v06-main-review-example-dark.png")) && ok
        // Ask the Model: optional real reply
        let chat = ChatModel(renamer: m)
        chat.loadModels()
        RunLoop.main.run(until: Date().addingTimeInterval(1.5))
        let a = CommandLine.arguments
        if let i = a.firstIndex(of: "--chat-prompt"), i + 1 < a.count {
            if let b = chat.blocker {
                FileHandle.standardError.write(Data("chat skipped: \(b)\n".utf8))
            } else {
                var finished = false
                chat.send(a[i + 1]) { finished = true }
                let deadline = Date().addingTimeInterval(240)
                while !finished && Date() < deadline { RunLoop.main.run(until: Date().addingTimeInterval(0.2)) }
                if !finished { chat.stop() }
                print("chat reply: \(chat.messages.last?.text.prefix(300) ?? "-")")
            }
        }
        for scheme in [ColorScheme.dark, .light] {
            let name = scheme == .dark ? "dark" : "light"
            ok = renderView(ChatView(chat: chat, model: m, scrolls: false).frame(width: 640), scheme: scheme,
                            to: dir.appendingPathComponent("v06-chat-\(name).png")) && ok
        }
        for scheme in [ColorScheme.dark, .light] {
            let name = scheme == .dark ? "dark" : "light"
            ok = renderView(PopoverView(model: m), scheme: scheme, to: dir.appendingPathComponent("v06-popover-\(name).png")) && ok
            ok = renderView(AboutView(model: m), scheme: scheme, to: dir.appendingPathComponent("v06-about-\(name).png")) && ok
        }
        return ok
    }

    /// v0.6.1: popover (header padding + layout order), Settings › Layout / Menu Bar, and Ask the Model with the
    /// picker on the configured model (a real reply when --chat-prompt is given and nothing is busy).
    static func renderV061(_ m: RenamerModel, dir: URL) -> Bool {
        var ok = true
        let store = LayoutStore.shared
        for scheme in [ColorScheme.dark, .light] {
            let name = scheme == .dark ? "dark" : "light"
            ok = renderView(PopoverView(model: m), scheme: scheme, to: dir.appendingPathComponent("v061-popover-\(name).png")) && ok
            ok = renderView(SettingsView(store: store, model: m, nav: SettingsNav(.layout), scrollable: false), scheme: scheme,
                            to: dir.appendingPathComponent("v061-settings-layout-\(name).png")) && ok
        }
        ok = renderView(SettingsView(store: store, model: m, nav: SettingsNav(.menuBar), scrollable: false), scheme: .dark,
                        to: dir.appendingPathComponent("v061-settings-menubar-dark.png")) && ok
        ok = renderMenuBar(m, to: dir.appendingPathComponent("v061-menubar.png")) && ok
        let chat = ChatModel(renamer: m)
        var loaded = false
        chat.loadModels { loaded = true }
        let t0 = Date().addingTimeInterval(10)
        while !loaded && Date() < t0 { RunLoop.main.run(until: Date().addingTimeInterval(0.1)) }
        print("chat models: \(chat.models.joined(separator: ", ")) · selected=\(chat.model) default=\(chat.defaultModel)")
        let a = CommandLine.arguments
        if let i = a.firstIndex(of: "--chat-prompt"), i + 1 < a.count {
            if let b = chat.blocker {
                FileHandle.standardError.write(Data("chat skipped: \(b)\n".utf8))
            } else {
                var finished = false
                chat.send(a[i + 1]) { finished = true }
                let deadline = Date().addingTimeInterval(240)
                while !finished && Date() < deadline { RunLoop.main.run(until: Date().addingTimeInterval(0.2)) }
                if !finished { chat.stop() }
                print("chat reply: \(chat.messages.last?.text.prefix(300) ?? "-")")
            }
        }
        for scheme in [ColorScheme.dark, .light] {
            let name = scheme == .dark ? "dark" : "light"
            ok = renderView(ChatView(chat: chat, model: m, scrolls: false).frame(width: 640), scheme: scheme,
                            to: dir.appendingPathComponent("v061-chat-\(name).png")) && ok
        }
        return ok
    }

    /// v0.7: every Settings tab, About, the popover in a non-default (colorblind-friendly) palette during an example
    /// run, and the Sync tab both as it really is on this Mac and as it looks once a folder is chosen (example).
    static func renderV07(_ m: RenamerModel, dir: URL) -> Bool {
        var ok = true
        let real = LayoutStore.shared
        for tab in SettingsTab.allCases {
            let schemes: [ColorScheme] = [.layout, .about].contains(tab) ? [.dark, .light] : [.dark]
            for scheme in schemes {
                let name = scheme == .dark ? "dark" : "light"
                var view = SettingsView(store: real, model: m, nav: SettingsNav(tab), scrollable: false)
                if tab == .diagnostics {
                    var text = "Not running (127.0.0.1:11434)"
                    if let u = URL(string: "http://127.0.0.1:11434/api/version"),
                       let d = try? Data(contentsOf: u), let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any] {
                        text = "Running · v\(o["version"] as? String ?? "?")"
                    }
                    view.ollamaOverride = text
                }
                ok = renderView(view, scheme: scheme, to: dir.appendingPathComponent("v07-settings-\(tab.rawValue)-\(name).png")) && ok
            }
        }
        // Sync: real state on this Mac (off, with GrokGauge's folder suggested), and an example once a folder is chosen.
        let gg = LayoutStore.grokGaugeSyncFolder()
        let off = LayoutStore.preview(real.settings, suggested: gg)
        ok = renderView(SettingsView(store: off, model: m, nav: SettingsNav(.sync), scrollable: false), scheme: .dark,
                        to: dir.appendingPathComponent("v07-sync-status-off-dark.png")) && ok
        let on = LayoutStore.preview(real.settings, syncFolder: gg ?? URL(fileURLWithPath: NSHomeDirectory() + "/Google Drive/MacSyncing"),
                                     state: .synced(Date()), note: "Created ClipGauge/settings.json")
        ok = renderView(SettingsView(store: on, model: m, nav: SettingsNav(.sync), scrollable: false), scheme: .dark,
                        to: dir.appendingPathComponent("v07-sync-status-example-dark.png")) && ok
        // About on its own
        ok = renderView(AboutView(model: m), scheme: .dark, to: dir.appendingPathComponent("v07-about-dark.png")) && ok
        // Popover with the colorblind-friendly palette during an example run (mocked state; nothing runs)
        let ex = RenamerModel(preview: true)
        settle(ex)
        ex.lock = ["argv": ["python3", "scripts/run_pipeline.py"], "launched_by": "preview"]
        ex.status = ["state": "running", "clip_total": 7, "clip_index": 3, "step": "describe", "dry_run": true,
                     "current_file": "/example/CAM_EXAMPLE_0003.MP4", "message": "Describing frames", "eta_seconds": 420]
        let saved = Palette.current
        Palette.current = Palette(colors: .colorblindFriendly)
        var cb = real.settings
        cb.colors = .colorblindFriendly
        let cbStore = LayoutStore.preview(cb)
        for scheme in [ColorScheme.dark, .light] {
            let name = scheme == .dark ? "dark" : "light"
            ok = renderView(PopoverView(model: ex, layout: cbStore), scheme: scheme,
                            to: dir.appendingPathComponent("v07-popover-colorblind-\(name).png")) && ok
        }
        ok = renderView(SettingsView(store: cbStore, model: m, nav: SettingsNav(.colors), scrollable: false), scheme: .dark,
                        to: dir.appendingPathComponent("v07-settings-colors-colorblind-dark.png")) && ok
        ok = renderMenuBar(ex, to: dir.appendingPathComponent("v07-menubar-colorblind.png")) && ok
        Palette.current = saved
        ok = renderView(PopoverView(model: m), scheme: .dark, to: dir.appendingPathComponent("v07-popover-default-dark.png")) && ok
        return ok
    }

    /// v0.7.2: About (PayPal button), the popover footer and the app menu, both with "Support ClipGauge…".
    static func renderV072(_ m: RenamerModel, dir: URL) -> Bool {
        var ok = renderView(AboutView(model: m), scheme: .dark, to: dir.appendingPathComponent("v072-about-dark.png"))
        ok = renderView(PopoverView(model: m), scheme: .dark, to: dir.appendingPathComponent("v072-popover-dark.png")) && ok
        // The real app menu, read back from the NSMenu MainMenu installs, drawn as a menu-style list.
        let menuTarget = NSObject()
        MainMenu.install(target: menuTarget)
        defer { withExtendedLifetime(menuTarget) {} }
        let items: [(String, String)] = (NSApp.mainMenu?.items.first?.submenu?.items ?? []).map { item in
            if item.isSeparatorItem { return ("-", "") }
            var key = ""
            if !item.keyEquivalent.isEmpty {
                let mods = item.keyEquivalentModifierMask
                key = (mods.contains(.control) ? "⌃" : "") + (mods.contains(.option) ? "⌥" : "")
                    + (mods.contains(.shift) ? "⇧" : "") + (mods.contains(.command) ? "⌘" : "") + item.keyEquivalent.uppercased()
            }
            return (item.title, key)
        }
        let menu = VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(items.enumerated()), id: \.offset) { _, it in
                if it.0 == "-" {
                    Divider().padding(.vertical, 4).padding(.horizontal, 10)
                } else {
                    let hi = it.0.hasPrefix("Support")
                    HStack {
                        Text(it.0)
                        Spacer(minLength: 24)
                        Text(it.1).foregroundStyle(hi ? Color.white.opacity(0.85) : Color.secondary)
                    }
                    .font(.system(size: 13))
                    .foregroundStyle(hi ? Color.white : Color.primary)
                    .padding(.horizontal, 10).padding(.vertical, 3)
                    .background(hi ? RoundedRectangle(cornerRadius: 4).fill(Color.accentColor) : nil)
                    .padding(.horizontal, 5)
                }
            }
        }
        .padding(.vertical, 5)
        .frame(width: 260)
        let menuPreview = VStack(alignment: .leading, spacing: 6) {
            Text("\(kAppName) app menu").font(.caption).foregroundStyle(.secondary)
            menu.background(RoundedRectangle(cornerRadius: 8).fill(Color(white: 0.2)))
                .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(.separator, lineWidth: 0.5))
        }.padding(14)
        ok = renderView(menuPreview, scheme: .dark, to: dir.appendingPathComponent("v072-appmenu-dark.png")) && ok
        return ok
    }

    private static func renderView<V: View>(_ view: V, scheme: ColorScheme, to url: URL) -> Bool {
        let root = view
            .background(scheme == .dark ? Color(white: 0.15) : Color(white: 0.965))
            .environment(\.colorScheme, scheme)
        let host = NSHostingView(rootView: root)
        host.appearance = NSAppearance(named: scheme == .dark ? .darkAqua : .aqua)
        host.setFrameSize(host.fittingSize)
        let window = NSWindow(contentRect: NSRect(origin: NSPoint(x: -10_000, y: -10_000), size: host.fittingSize),
                              styleMask: [.borderless], backing: .buffered, defer: false)
        window.appearance = host.appearance
        window.contentView = host
        window.orderFrontRegardless()
        RunLoop.main.run(until: Date().addingTimeInterval(0.6))
        host.setFrameSize(host.fittingSize)
        window.setContentSize(host.fittingSize)
        RunLoop.main.run(until: Date().addingTimeInterval(0.2))
        host.layoutSubtreeIfNeeded()
        host.display()
        guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { return false }
        host.cacheDisplay(in: host.bounds, to: rep)
        window.orderOut(nil)
        return write(rep, to: url)
    }

    private static func renderMenuBar(_ m: RenamerModel, to url: URL) -> Bool {
        let look = StatusItemLook(m, forceWhite: true)
        let pad: CGFloat = 10, h: CGFloat = 30, scale: CGFloat = 2
        let tsize = look.title.size()
        let iw: CGFloat = look.image == nil ? 0 : 18
        let w = pad * 2 + iw + tsize.width
        guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(w * scale), pixelsHigh: Int(h * scale),
                                         bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                         colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0) else { return false }
        rep.size = NSSize(width: w, height: h)
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
        NSColor(white: 0.13, alpha: 1).setFill()
        NSBezierPath(roundedRect: NSRect(x: 0, y: 0, width: w, height: h), xRadius: 8, yRadius: 8).fill()
        look.image?.draw(in: NSRect(x: pad, y: (h - 18) / 2, width: 18, height: 18))
        look.title.draw(at: NSPoint(x: pad + iw, y: (h - tsize.height) / 2))
        NSGraphicsContext.restoreGraphicsState()
        return write(rep, to: url)
    }

    /// App icon: GrokGauge-style dark squircle with the mark in green. Writes an .iconset folder for iconutil.
    static func renderIconset(to dir: URL) -> Int32 {
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let specs: [(String, Int)] = [("16x16", 16), ("16x16@2x", 32), ("32x32", 32), ("32x32@2x", 64), ("128x128", 128),
                                      ("128x128@2x", 256), ("256x256", 256), ("256x256@2x", 512), ("512x512", 512), ("512x512@2x", 1024)]
        for (name, px) in specs {
            guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px, bitsPerSample: 8,
                                             samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                                             bytesPerRow: 0, bitsPerPixel: 0) else { return 1 }
            let s = CGFloat(px)
            rep.size = NSSize(width: s, height: s)
            NSGraphicsContext.saveGraphicsState()
            NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
            let inset = s * 0.1
            let rect = NSRect(x: inset, y: inset, width: s - 2 * inset, height: s - 2 * inset)
            let bg = NSBezierPath(roundedRect: rect, xRadius: rect.width * 0.225, yRadius: rect.width * 0.225)
            NSGradient(starting: NSColor(white: 0.22, alpha: 1), ending: NSColor(white: 0.08, alpha: 1))?.draw(in: bg, angle: -90)
            let mark = ClipMarkImage.tinted(size: rect.width * 0.62, .systemGreen)
            let ms = rect.width * 0.62
            mark.draw(in: NSRect(x: rect.midX - ms / 2, y: rect.midY - ms / 2, width: ms, height: ms))
            NSGraphicsContext.restoreGraphicsState()
            if !write(rep, to: dir.appendingPathComponent("icon_\(name).png")) { return 1 }
        }
        return 0
    }

    private static func write(_ rep: NSBitmapImageRep, to url: URL) -> Bool {
        guard let data = rep.representation(using: .png, properties: [:]) else { return false }
        do { try data.write(to: url); return true } catch { return false }
    }
}

// MARK: - entry

let app = NSApplication.shared
let args = CommandLine.arguments
LegacyDefaults.migrate()
if args.contains("--sync-selftest") {
    app.setActivationPolicy(.prohibited)
    exit(SyncSelfTest.run())
}
if args.contains("--print-state") {
    app.setActivationPolicy(.prohibited)
    exit(CLI.printState())
}
if let i = args.firstIndex(of: "--render-preview") {
    app.setActivationPolicy(.accessory)
    let dir = i + 1 < args.count ? URL(fileURLWithPath: args[i + 1]) : URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
    exit(CLI.renderPreview(to: dir))
}
if let i = args.firstIndex(of: "--create-project"), i + 1 < args.count {
    app.setActivationPolicy(.prohibited)
    exit(CLI.createProject(URL(fileURLWithPath: args[i + 1])))
}
if let i = args.firstIndex(of: "--render-iconset"), i + 1 < args.count {
    app.setActivationPolicy(.prohibited)
    exit(CLI.renderIconset(to: URL(fileURLWithPath: args[i + 1])))
}
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
