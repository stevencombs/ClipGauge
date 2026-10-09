// ClipGauge Settings (v0.7) — the same window as GrokGauge's: a tab bar with Layout, Menu Bar, Colors & Alerts,
// Sync, Diagnostics and About, grouped rows (PrefGroup / PrefRow), drag-to-reorder lists (ReorderList), a live
// menu bar preview, a global shortcut recorder, level colors with presets, settings sync through a folder your Macs
// already sync, export/import, and "Reset …" buttons. Saved as JSON in UserDefaults ("settings.v1") on every change.
import AppKit
import Combine
import SwiftUI

// MARK: - Model

/// The popover's sections, in their default order (the order v0.6 shipped with, plus Updates).
enum PopoverSection: String, Codable, CaseIterable, Identifiable {
    case alerts, statusRing, currentRun, inbox, updates, askBox, services, refreshRow, actions, sortButton
    var id: String { rawValue }
    var title: String {
        switch self {
        case .alerts: return "Alerts (last-run errors)"
        case .statusRing: return "Status ring"
        case .currentRun: return "Current run (while processing)"
        case .inbox: return "Inbox"
        case .updates: return "Updates"
        case .askBox: return "Ask the model box"
        case .services: return "Services (Open at login)"
        case .refreshRow: return "Updated & Refresh row"
        case .actions: return "Action buttons"
        case .sortButton: return "Sort into Projects button"
        }
    }
    var symbol: String {
        switch self {
        case .alerts: return "exclamationmark.triangle"
        case .statusRing: return "circle.circle"
        case .currentRun: return "film.stack"
        case .inbox: return "tray"
        case .updates: return "arrow.down.circle"
        case .askBox: return "sparkles"
        case .services: return "power"
        case .refreshRow: return "arrow.clockwise"
        case .actions: return "square.grid.2x2"
        case .sortButton: return "folder.badge.gearshape"
        }
    }
}

enum ActionButtonKind: String, Codable, CaseIterable, Identifiable {
    case start, open, inbox, ask
    var id: String { rawValue }
    var title: String {
        switch self {
        case .start: return "Start / Stop"
        case .open: return "Open (main window)"
        case .inbox: return "Inbox (Finder)"
        case .ask: return "Ask (chat window)"
        }
    }
    var symbol: String {
        switch self {
        case .start: return "play.fill"
        case .open: return "macwindow"
        case .inbox: return "folder"
        case .ask: return "sparkles"
        }
    }
}

/// One entry in a user-orderable list with a show/hide switch (as in GrokGauge).
struct OrderedToggle<ID: Hashable & Codable & CaseIterable>: Codable, Equatable, Hashable, Identifiable {
    var id: ID
    var visible: Bool
    init(_ id: ID, visible: Bool = true) { self.id = id; self.visible = visible }
}

typealias SectionItem = OrderedToggle<PopoverSection>
typealias ActionItem = OrderedToggle<ActionButtonKind>

enum MenuBarMode: String, Codable, CaseIterable, Identifiable {
    case iconAndPercent, iconOnly, percentOnly, iconAndReview
    var id: String { rawValue }
    var title: String {
        switch self {
        case .iconAndPercent: return "Icon + progress % while processing"
        case .iconOnly: return "Icon only, tinted by status"
        case .percentOnly: return "Progress % only while processing (icon when idle)"
        case .iconAndReview: return "Icon + progress %, or the needs-review count"
        }
    }
}

/// The groups that sync independently. When two Macs changed different groups, both changes survive the merge.
enum SettingsGroup: String, CaseIterable, Codable {
    case layout, menuBar, colors, alerts, general
}

/// Everything in Settings (v0.7). Preferences only: never clips, notes, transcripts, models, paths or logins.
/// Same JSON style as GrokGauge's GaugeSettings (lenient decoding, #RRGGBB colors, ISO dates, modifiedAt).
struct ClipGaugeSettings: Codable, Equatable {
    static let schemaVersion = 1

    // Layout
    var sections: [SectionItem]
    var actions: [ActionItem]
    // Menu bar
    var menuBarMode: MenuBarMode
    var showETA: Bool
    var hideLogo: Bool
    var hotKey: HotKey
    // Colors & alerts
    var colors: LevelColors
    var notifyRuns: Bool
    var notifyReview: Bool
    var notifyUpdates: Bool
    // Updates (the app itself)
    var checkForUpdates: Bool

    /// When these settings were last changed on any Mac, and per group (newest change wins, group by group).
    var modifiedAt: Date
    var groupModified: [String: Date]

    static let defaultSections: [SectionItem] = PopoverSection.allCases.map { SectionItem($0) }
    static let defaultActions: [ActionItem] = ActionButtonKind.allCases.map { ActionItem($0, visible: $0 != .ask) }
    static let epoch = Date(timeIntervalSince1970: 0)
    static let defaults = ClipGaugeSettings()

    init(sections: [SectionItem] = ClipGaugeSettings.defaultSections,
         actions: [ActionItem] = ClipGaugeSettings.defaultActions,
         menuBarMode: MenuBarMode = .iconAndPercent, showETA: Bool = false, hideLogo: Bool = false,
         hotKey: HotKey = .standard, colors: LevelColors = .system,
         notifyRuns: Bool = true, notifyReview: Bool = true, notifyUpdates: Bool = true,
         checkForUpdates: Bool = true,
         modifiedAt: Date = ClipGaugeSettings.epoch, groupModified: [String: Date] = [:]) {
        self.sections = Self.normalized(sections, defaults: Self.defaultSections)
        self.actions = Self.normalized(actions, defaults: Self.defaultActions)
        self.menuBarMode = menuBarMode
        self.showETA = showETA
        self.hideLogo = hideLogo
        self.hotKey = hotKey
        self.colors = colors
        self.notifyRuns = notifyRuns
        self.notifyReview = notifyReview
        self.notifyUpdates = notifyUpdates
        self.checkForUpdates = checkForUpdates
        self.modifiedAt = modifiedAt
        self.groupModified = groupModified
    }

    func isVisible(_ s: PopoverSection) -> Bool { sections.first { $0.id == s }?.visible ?? true }
    var visibleSections: [PopoverSection] { sections.filter(\.visible).map(\.id) }
    var visibleActions: [ActionButtonKind] { actions.filter(\.visible).map(\.id) }

    mutating func resetLayout() {
        sections = Self.defaultSections
        actions = Self.defaultActions
    }

    /// Equality that ignores the timestamps (used to detect real changes).
    func sameContent(as o: ClipGaugeSettings) -> Bool {
        var a = self, b = o
        a.modifiedAt = Self.epoch; b.modifiedAt = Self.epoch
        a.groupModified = [:]; b.groupModified = [:]
        return a == b
    }

    /// Which groups differ between two settings.
    func changedGroups(from o: ClipGaugeSettings) -> [SettingsGroup] {
        SettingsGroup.allCases.filter { !sameGroup($0, as: o) }
    }

    func sameGroup(_ g: SettingsGroup, as o: ClipGaugeSettings) -> Bool {
        switch g {
        case .layout: return sections == o.sections && actions == o.actions
        case .menuBar: return menuBarMode == o.menuBarMode && showETA == o.showETA && hideLogo == o.hideLogo && hotKey == o.hotKey
        case .colors: return colors == o.colors
        case .alerts: return notifyRuns == o.notifyRuns && notifyReview == o.notifyReview && notifyUpdates == o.notifyUpdates
        case .general: return checkForUpdates == o.checkForUpdates
        }
    }

    mutating func copyGroup(_ g: SettingsGroup, from o: ClipGaugeSettings) {
        switch g {
        case .layout: sections = o.sections; actions = o.actions
        case .menuBar: menuBarMode = o.menuBarMode; showETA = o.showETA; hideLogo = o.hideLogo; hotKey = o.hotKey
        case .colors: colors = o.colors
        case .alerts: notifyRuns = o.notifyRuns; notifyReview = o.notifyReview; notifyUpdates = o.notifyUpdates
        case .general: checkForUpdates = o.checkForUpdates
        }
    }

    /// When a group was last changed (files without per-group stamps fall back to modifiedAt).
    func stamp(_ g: SettingsGroup) -> Date { groupModified[g.rawValue] ?? modifiedAt }

    /// Writes an explicit stamp for every group (epoch for never-changed ones), so bumping modifiedAt later doesn't
    /// make untouched groups look new.
    mutating func pinGroupStamps() {
        for g in SettingsGroup.allCases where groupModified[g.rawValue] == nil {
            groupModified[g.rawValue] = modifiedAt
        }
    }

    /// Drops unknown/duplicate ids and inserts anything missing after its closest preceding default (GrokGauge's rule).
    static func normalized<ID>(_ items: [OrderedToggle<ID>], defaults: [OrderedToggle<ID>]) -> [OrderedToggle<ID>] {
        var seen = Set<ID>()
        var out: [OrderedToggle<ID>] = []
        for item in items where !seen.contains(item.id) {
            seen.insert(item.id)
            out.append(item)
        }
        for (i, d) in defaults.enumerated() where !seen.contains(d.id) {
            let previous = defaults[..<i].reversed().first { p in out.contains { $0.id == p.id } }
            let at = previous.flatMap { p in out.firstIndex { $0.id == p.id } }.map { $0 + 1 } ?? 0
            out.insert(d, at: at)
            seen.insert(d.id)
        }
        return out
    }

    // Lenient decoding (as in GrokGauge): unknown ids/values from a newer build are skipped, missing keys use defaults.
    enum CodingKeys: String, CodingKey {
        case sections, actions, menuBarMode, showETA, hideLogo, hotKey, colors, notifyRuns, notifyReview, notifyUpdates,
             checkForUpdates, modifiedAt, groupModified
    }
    private struct RawToggle: Codable { let id: String; let visible: Bool? }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let d = ClipGaugeSettings.defaults
        func list<ID: RawRepresentable & Hashable & Codable & CaseIterable>(_ key: CodingKeys) -> [OrderedToggle<ID>]? where ID.RawValue == String {
            guard let raw = try? c.decode([RawToggle].self, forKey: key) else { return nil }
            return raw.compactMap { r in ID(rawValue: r.id).map { OrderedToggle($0, visible: r.visible ?? true) } }
        }
        func value<T: Decodable>(_ key: CodingKeys, _ fallback: T) -> T { (try? c.decodeIfPresent(T.self, forKey: key)) ?? fallback }
        self.init(sections: list(.sections) ?? d.sections,
                  actions: list(.actions) ?? d.actions,
                  menuBarMode: (try? c.decode(String.self, forKey: .menuBarMode)).flatMap(MenuBarMode.init(rawValue:)) ?? d.menuBarMode,
                  showETA: value(.showETA, d.showETA),
                  hideLogo: value(.hideLogo, d.hideLogo),
                  hotKey: value(.hotKey, d.hotKey),
                  colors: value(.colors, d.colors),
                  notifyRuns: value(.notifyRuns, d.notifyRuns),
                  notifyReview: value(.notifyReview, d.notifyReview),
                  notifyUpdates: value(.notifyUpdates, d.notifyUpdates),
                  checkForUpdates: value(.checkForUpdates, d.checkForUpdates),
                  modifiedAt: value(.modifiedAt, d.modifiedAt),
                  groupModified: value(.groupModified, [String: Date]()))
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(sections.map { RawToggle(id: $0.id.rawValue, visible: $0.visible) }, forKey: .sections)
        try c.encode(actions.map { RawToggle(id: $0.id.rawValue, visible: $0.visible) }, forKey: .actions)
        try c.encode(menuBarMode.rawValue, forKey: .menuBarMode)
        try c.encode(showETA, forKey: .showETA)
        try c.encode(hideLogo, forKey: .hideLogo)
        try c.encode(hotKey, forKey: .hotKey)
        try c.encode(colors, forKey: .colors)
        try c.encode(notifyRuns, forKey: .notifyRuns)
        try c.encode(notifyReview, forKey: .notifyReview)
        try c.encode(notifyUpdates, forKey: .notifyUpdates)
        try c.encode(checkForUpdates, forKey: .checkForUpdates)
        try c.encode(modifiedAt, forKey: .modifiedAt)
        try c.encode(groupModified, forKey: .groupModified)
    }
}

typealias LayoutSettings = ClipGaugeSettings

/// Owns the preferences (UserDefaults "settings.v1", saved on every change — no Save button, as in GrokGauge) and,
/// when a folder is chosen in Settings › Sync, keeps them in sync with `<folder>/ClipGauge/settings.json`.
final class LayoutStore: ObservableObject {
    enum SyncState: Equatable {
        case off
        case synced(Date)
        case failed(String)
    }

    static let shared = LayoutStore()
    static let key = "settings.v1"
    static let legacyKey = "layoutSettings"         // v0.6.1
    static let syncFolderKey = "sync.folderPath"    // per Mac, never synced (same key as GrokGauge)

    @Published var settings: ClipGaugeSettings {
        didSet { settingsDidChange(from: oldValue) }
    }
    @Published private(set) var syncFolder: URL?
    @Published private(set) var syncState: SyncState = .off
    @Published private(set) var suggestedFolder: URL?
    @Published private(set) var lastSyncNote: String?

    private let defaults: UserDefaults
    private let persist: Bool
    private let isShared: Bool
    private var applyingRemote = false
    private var bumping = false
    private var watcher: FolderWatcher?
    private var pendingWrite: DispatchWorkItem?

    init(defaults: UserDefaults = .standard, persist: Bool = true, shared: Bool = true) {
        self.defaults = defaults
        self.persist = persist
        self.isShared = shared
        if persist, let data = defaults.data(forKey: Self.key), let saved = try? SettingsSync.decodeSettings(data) {
            settings = saved
        } else if persist, let data = defaults.data(forKey: Self.legacyKey),
                  let old = try? JSONDecoder().decode(ClipGaugeSettings.self, from: data) {
            // v0.6.1 kept only the layout + menu bar choice under "layoutSettings": carry it over unchanged.
            var s = old
            s.modifiedAt = Self.epochIfUnchanged(s)
            settings = s
        } else {
            settings = ClipGaugeSettings()
        }
        if persist, let path = defaults.string(forKey: Self.syncFolderKey) {
            syncFolder = URL(fileURLWithPath: path, isDirectory: true)
        }
        if shared { Palette.current = Palette(colors: settings.colors) }
        if persist { save() }
    }

    /// A migrated-but-default settings object keeps the epoch stamp so it never beats a real synced change.
    private static func epochIfUnchanged(_ s: ClipGaugeSettings) -> Date {
        s.sameContent(as: ClipGaugeSettings()) ? ClipGaugeSettings.epoch : SettingsSync.roundedNow()
    }

    /// Inert store for previews: never touches UserDefaults, the sync folder or the shared palette.
    static func preview(_ s: ClipGaugeSettings = ClipGaugeSettings(), syncFolder: URL? = nil, state: SyncState = .off,
                        suggested: URL? = nil, note: String? = nil) -> LayoutStore {
        let store = LayoutStore(persist: false, shared: false)
        store.settings = s
        store.syncFolder = syncFolder
        store.syncState = state
        store.suggestedFolder = suggested
        store.lastSyncNote = note
        return store
    }

    func start() {
        guard persist else { return }
        startWatching()
    }

    // MARK: local changes

    private func settingsDidChange(from old: ClipGaugeSettings) {
        if isShared { Palette.current = Palette(colors: settings.colors) }
        guard !bumping else { return }
        if !applyingRemote, !settings.sameContent(as: old) {
            bumping = true
            let now = SettingsSync.roundedNow()
            settings.pinGroupStamps()
            for g in settings.changedGroups(from: old) { settings.groupModified[g.rawValue] = now }
            settings.modifiedAt = now
            bumping = false
            scheduleSyncWrite()
        }
        save()
    }

    func save() {
        guard persist, let data = try? SettingsSync.encodeSettings(settings) else { return }
        defaults.set(data, forKey: Self.key)
    }

    func resetLayout() { settings.resetLayout() }
    func resetColors() { settings.colors = .system }

    /// Everything back to defaults; the sync folder choice is kept (as in GrokGauge).
    func resetAll() {
        var fresh = ClipGaugeSettings()
        fresh.modifiedAt = settings.modifiedAt
        fresh.groupModified = settings.groupModified
        settings = fresh
    }

    /// "status · inbox · …" — for --print-state.
    var summary: String {
        settings.visibleSections.map(\.rawValue).joined(separator: ",") + " | actions " +
            settings.visibleActions.map(\.rawValue).joined(separator: ",") + " | menu bar \(settings.menuBarMode.rawValue)" +
            (settings.showETA ? "+eta" : "") + (settings.hideLogo ? " no-logo" : "") +
            " | colors \(colorsName) | shortcut \(settings.hotKey.enabled ? settings.hotKey.display : "off")" +
            " | sync \(syncFolder == nil ? "off" : "on")"
    }

    var colorsName: String {
        settings.colors == .system ? "system" : settings.colors == .colorblindFriendly ? "colorblind-friendly" : "custom"
    }

    // MARK: sync

    func chooseSyncFolder() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        panel.allowsMultipleSelection = false
        panel.prompt = "Use Folder"
        panel.message = "Choose a folder your Macs already sync (Google Drive, Insync, iCloud Drive, Dropbox…). "
            + "\(kAppName) keeps one small settings.json in a \(kAppName) subfolder."
        panel.directoryURL = syncFolder ?? suggestedFolder ?? FileManager.default.homeDirectoryForCurrentUser
        NSApp.activate(ignoringOtherApps: true)
        guard panel.runModal() == .OK, let url = panel.url else { return }
        setSyncFolder(url)
    }

    func setSyncFolder(_ url: URL?) {
        stopWatching()
        syncFolder = url
        if persist {
            if let url { defaults.set(url.path, forKey: Self.syncFolderKey) } else { defaults.removeObject(forKey: Self.syncFolderKey) }
        }
        syncState = .off
        lastSyncNote = nil
        startWatching()
    }

    private func startWatching() {
        guard persist, let folder = syncFolder else { return }
        let file = SettingsSync.fileURL(inSyncFolder: folder)
        reconcile()
        let w = FolderWatcher(directory: file.deletingLastPathComponent(), file: file) { [weak self] in self?.reconcile() }
        w.start()
        watcher = w
    }

    private func stopWatching() {
        watcher?.stop()
        watcher = nil
        pendingWrite?.cancel()
    }

    /// Merges this Mac's settings with the shared file, group by group (newest change wins). An existing file is
    /// never overwritten without merging: unknown keys from newer builds are kept, and the previous file is
    /// backed up locally (~/Library/Application Support/ClipGauge/sync-backups) before it's replaced.
    func reconcile() {
        guard persist, let folder = syncFolder else { return }
        let file = SettingsSync.fileURL(inSyncFolder: folder)
        do {
            let remote = try SettingsSync.read(from: file)
            let r = SettingsSync.merge(local: settings, remote: remote)
            if !r.merged.sameContent(as: settings) || r.merged.groupModified != settings.groupModified || r.merged.modifiedAt != settings.modifiedAt {
                applyingRemote = true
                settings = r.merged
                applyingRemote = false
            }
            if r.writeFile {
                try SettingsSync.write(r.merged, to: file, backupDir: persist ? Self.backupDir : nil)
            }
            lastSyncNote = r.note
            syncState = .synced(Date())
        } catch {
            syncState = .failed(SettingsSync.describe(error))
        }
    }

    static var backupDir: URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("ClipGauge/sync-backups", isDirectory: true)
    }

    /// Debounced so a burst of changes writes once.
    private func scheduleSyncWrite() {
        guard persist, syncFolder != nil else { return }
        pendingWrite?.cancel()
        let work = DispatchWorkItem { [weak self] in self?.reconcile() }
        pendingWrite = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0, execute: work)
    }

    // MARK: export / import

    func exportSettings() {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = "ClipGauge Settings.json"
        panel.allowedContentTypes = [.json]
        panel.canCreateDirectories = true
        NSApp.activate(ignoringOtherApps: true)
        guard panel.runModal() == .OK, let url = panel.url else { return }
        do { try SettingsSync.encodeEnvelope(settings).write(to: url, options: .atomic) } catch {
            alert("Couldn't export settings", SettingsSync.describe(error))
        }
    }

    func importSettings() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.json]
        panel.allowsMultipleSelection = false
        NSApp.activate(ignoringOtherApps: true)
        guard panel.runModal() == .OK, let url = panel.url else { return }
        do {
            guard let imported = try SettingsSync.read(from: url) else { return }
            // An import is a change made on this Mac now, so it wins the next sync.
            var s = imported
            s.modifiedAt = settings.modifiedAt
            s.groupModified = settings.groupModified
            settings = s
        } catch {
            alert("Couldn't import settings", SettingsSync.describe(error))
        }
    }

    private func alert(_ title: String, _ text: String) {
        let a = NSAlert()
        a.messageText = title
        a.informativeText = text
        a.runModal()
    }

    // MARK: suggested folder

    /// GrokGauge's sync folder (read from its preferences, read-only) is the first suggestion, so both apps sit
    /// side by side; otherwise a shallow, read-only search for "MacSyncing" like GrokGauge's.
    func findSuggestedFolder() {
        guard persist, suggestedFolder == nil else { return }
        if let g = Self.grokGaugeSyncFolder() { suggestedFolder = g; return }
        DispatchQueue.global(qos: .utility).async {
            let hit = Self.searchSuggestedFolder()
            DispatchQueue.main.async { self.suggestedFolder = hit }
        }
    }

    static func grokGaugeSyncFolder() -> URL? {
        guard let path = UserDefaults(suiteName: "com.retrocombs.GrokGauge")?.string(forKey: "sync.folderPath"),
              FileManager.default.fileExists(atPath: path) else { return nil }
        return URL(fileURLWithPath: path, isDirectory: true)
    }

    static func searchSuggestedFolder() -> URL? {
        let fm = FileManager.default
        let home = fm.homeDirectoryForCurrentUser
        let names: Set<String> = ["macsyncing", "mac syncing", "mac sync"]
        let cloudHints = ["drive", "dropbox", "icloud", "onedrive", "sync", "box"]
        let roots = ((try? fm.contentsOfDirectory(at: home, includingPropertiesForKeys: [.isDirectoryKey],
                                                  options: [.skipsHiddenFiles])) ?? [])
            .filter { url in cloudHints.contains { url.lastPathComponent.lowercased().contains($0) } }
        // iCloud Drive and ~/Library/CloudStorage are deliberately not scanned: touching them can
        // trigger a macOS privacy prompt. Users can still pick folders there with "Choose…".
        if names.contains(home.lastPathComponent.lowercased()) { return home }

        var visited = 0
        var queue: [(URL, Int)] = roots.map { ($0, 0) }
        while !queue.isEmpty, visited < 4_000 {
            let (dir, depth) = queue.removeFirst()
            visited += 1
            if names.contains(dir.lastPathComponent.lowercased()) { return dir }
            guard depth < 4 else { continue }
            let kids = (try? fm.contentsOfDirectory(at: dir, includingPropertiesForKeys: [.isDirectoryKey, .isPackageKey],
                                                    options: [.skipsHiddenFiles, .skipsPackageDescendants])) ?? []
            for k in kids {
                let v = try? k.resourceValues(forKeys: [.isDirectoryKey, .isPackageKey])
                if v?.isDirectory == true, v?.isPackage != true { queue.append((k, depth + 1)) }
            }
        }
        return nil
    }
}

// MARK: - Controls (ported from GrokGauge's PrefsControls)

// PrefGroup / PrefRow live in Setup.swift (same metrics as GrokGauge's).

/// Drag-to-reorder list with show/hide checkboxes and ↑/↓ buttons (keyboard / VoiceOver friendly).
struct ReorderList<ID: Hashable & Codable & CaseIterable & RawRepresentable>: View where ID.RawValue == String {
    @Binding var items: [OrderedToggle<ID>]
    let title: (ID) -> String
    let icon: (ID) -> String
    var note: (ID) -> String? = { _ in nil }
    @ViewState private var targeted: ID? = nil

    var body: some View {
        VStack(spacing: 0) {
            ForEach(Array(items.enumerated()), id: \.element.id) { index, item in
                row(index: index, item: item)
                if index < items.count - 1 { Divider() }
            }
        }
    }

    private func row(index: Int, item: OrderedToggle<ID>) -> some View {
        HStack(spacing: 10) {
            Image(systemName: "line.3.horizontal").foregroundStyle(.tertiary).help("Drag to reorder").accessibilityHidden(true)
            Toggle(isOn: Binding(get: { items.indices.contains(index) ? items[index].visible : false },
                                 set: { v in if items.indices.contains(index) { items[index].visible = v } })) {
                HStack(spacing: 6) {
                    Image(systemName: icon(item.id)).foregroundStyle(.secondary).frame(width: 18)
                    Text(title(item.id))
                    if let n = note(item.id) { Text(n).font(.caption).foregroundStyle(.secondary) }
                }
            }
            .toggleStyle(.checkbox)
            Spacer()
            HStack(spacing: 2) {
                Button { move(item.id, to: index - 1) } label: { Image(systemName: "chevron.up") }
                    .disabled(index == 0).accessibilityLabel("Move \(title(item.id)) up")
                Button { move(item.id, to: index + 1) } label: { Image(systemName: "chevron.down") }
                    .disabled(index == items.count - 1).accessibilityLabel("Move \(title(item.id)) down")
            }
            .buttonStyle(.borderless).font(.caption).foregroundStyle(.secondary)
        }
        .padding(.vertical, 6)
        .contentShape(Rectangle())
        .background(targeted == item.id ? Color.accentColor.opacity(0.12) : .clear, in: RoundedRectangle(cornerRadius: 6))
        .draggable(item.id.rawValue) {
            Text(title(item.id)).padding(.horizontal, 10).padding(.vertical, 5).background(.regularMaterial, in: Capsule())
        }
        .dropDestination(for: String.self) { dropped, _ in
            guard let raw = dropped.first, let id = ID(rawValue: raw) else { return false }
            move(id, to: index)
            return true
        } isTargeted: { on in
            if on { targeted = item.id } else if targeted == item.id { targeted = nil }
        }
        .accessibilityElement(children: .contain)
        .accessibilityAction(named: "Move up") { move(item.id, to: index - 1) }
        .accessibilityAction(named: "Move down") { move(item.id, to: index + 1) }
    }

    private func move(_ id: ID, to target: Int) {
        guard let from = items.firstIndex(where: { $0.id == id }) else { return }
        let to = min(max(target, 0), items.count - 1)
        guard from != to else { return }
        var copy = items
        let item = copy.remove(at: from)
        copy.insert(item, at: to)
        withAnimation(.easeInOut(duration: 0.18)) { items = copy }
    }
}

/// What the menu bar would show for a sample state (dark bar, like GrokGauge's preview).
struct MenuBarPreview: View {
    let look: StatusItemLook

    private var textColor: Color {
        let c = look.title.length > 0 ? look.title.attribute(.foregroundColor, at: 0, effectiveRange: nil) as? NSColor : nil
        return c.map { Color(nsColor: $0) } ?? Color.white.opacity(0.6)
    }

    var body: some View {
        HStack(spacing: 0) {
            if let img = look.image {
                Image(nsImage: img).renderingMode(img.isTemplate ? .template : .original)
                    .foregroundStyle(Color.white.opacity(0.92))
            }
            if !look.title.string.isEmpty {
                Text(look.title.string).font(.system(size: 13, weight: .semibold).monospacedDigit()).foregroundStyle(textColor)
            }
        }
        .padding(.horizontal, 10).padding(.vertical, 4)
        .frame(minHeight: 24)
        .background(Color(white: 0.13), in: RoundedRectangle(cornerRadius: 6, style: .continuous))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Menu bar preview")
        .accessibilityValue(look.title.string)
    }
}

// MARK: - Settings window

enum SettingsTab: String, CaseIterable, Identifiable {
    case layout, menuBar, colors, sync, diagnostics, about
    var id: String { rawValue }
    var title: String {
        switch self {
        case .layout: return "Layout"
        case .menuBar: return "Menu Bar"
        case .colors: return "Colors & Alerts"
        case .sync: return "Sync"
        case .diagnostics: return "Diagnostics"
        case .about: return "About"
        }
    }
    var symbol: String {
        switch self {
        case .layout: return "rectangle.3.group"
        case .menuBar: return "menubar.rectangle"
        case .colors: return "paintpalette"
        case .sync: return "arrow.triangle.2.circlepath"
        case .diagnostics: return "stethoscope"
        case .about: return "info.circle"
        }
    }
}

/// Which tab the Settings window shows (so About… and the popover can open a specific tab).
final class SettingsNav: ObservableObject {
    static let shared = SettingsNav()
    @Published var tab: SettingsTab
    init(_ tab: SettingsTab = .layout) { self.tab = tab }
}

struct SettingsView: View {
    @ObservedObject var store: LayoutStore
    @ObservedObject var model: RenamerModel
    @ObservedObject var nav: SettingsNav = .shared
    @ObservedObject var updates: AppUpdateMonitor = .shared
    @ObservedObject var hotKeys: HotKeyCenter = .shared
    /// false when rendering previews: lays everything out at full height instead of scrolling.
    var scrollable = true
    var ollamaOverride: String? = nil
    var openSetup: () -> Void = {}

    private var settings: Binding<ClipGaugeSettings> { $store.settings }
    private var palette: Palette { Palette(colors: store.settings.colors) }

    var body: some View {
        VStack(spacing: 0) {
            tabBar
            Divider()
            if scrollable { ScrollView { page.padding(20) } } else { page.padding(20) }
        }
        .frame(width: 600)
        .frame(minHeight: scrollable ? 640 : nil, alignment: .top)
    }

    private var tabBar: some View {
        HStack(spacing: 4) {
            ForEach(SettingsTab.allCases) { t in
                Button { nav.tab = t } label: {
                    VStack(spacing: 3) {
                        Image(systemName: t.symbol).font(.system(size: 17, weight: .regular)).frame(height: 20)
                        Text(t.title).font(.system(size: 11))
                    }
                    .frame(width: 88, height: 46)
                    .foregroundStyle(nav.tab == t ? Color.accentColor : Color.secondary)
                    .background(nav.tab == t ? AnyShapeStyle(.fill.secondary) : AnyShapeStyle(Color.clear),
                                in: RoundedRectangle(cornerRadius: 7, style: .continuous))
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(t.title)
                .accessibilityAddTraits(nav.tab == t ? [.isSelected, .isButton] : .isButton)
            }
        }
        .padding(.vertical, 8)
        .frame(maxWidth: .infinity)
    }

    @ViewBuilder private var page: some View {
        VStack(alignment: .leading, spacing: 18) {
            switch nav.tab {
            case .layout: layoutTab
            case .menuBar: menuBarTab
            case .colors: colorsTab
            case .sync: syncTab
            case .diagnostics: DiagnosticsTab(model: model, store: store, updates: updates, ollamaOverride: ollamaOverride)
            case .about: AboutTab(model: model, store: store, updates: updates, openSetup: openSetup,
                                  showDiagnostics: { nav.tab = .diagnostics })
            }
        }
        .frame(maxWidth: .infinity, alignment: .topLeading)
    }

    // MARK: Layout

    private var layoutTab: some View {
        Group {
            PrefGroup(title: "Popover sections",
                      footer: "Drag rows (or use the arrows) to reorder. Unchecked sections are hidden. Changes apply right away. "
                        + "“Drive not connected” and “Resolve is open” warnings always show at the top, even with Alerts hidden.") {
                ReorderList(items: settings.sections, title: \.title, icon: \.symbol, note: { s in
                    switch s {
                    case .currentRun: return model.runActive ? nil : "(only during a run)"
                    case .alerts: return model.problemVisible ? "(showing now)" : nil
                    default: return nil
                    }
                })
            }
            PrefGroup(title: "Action buttons", footer: "The row of capsule buttons. At least one stays visible while the section is shown.") {
                ReorderList(items: settings.actions, title: \.title, icon: \.symbol)
            }
            HStack {
                Spacer()
                Button("Reset Layout to Default") { store.resetLayout() }
            }
        }
    }

    // MARK: Menu bar

    private var menuBarTab: some View {
        let s = store.settings
        return Group {
            PrefGroup(title: "Show in the menu bar") {
                Picker("Menu bar shows", selection: settings.menuBarMode) {
                    ForEach(MenuBarMode.allCases) { Text($0.title).tag($0) }
                }
                .pickerStyle(.radioGroup)
                .labelsHidden()
                .padding(.vertical, 6)
                Divider()
                Toggle("Add the time left while processing (42% · 3m)", isOn: settings.showETA)
                    .disabled(s.menuBarMode == .iconOnly)
                    .padding(.vertical, 6)
                Divider()
                Toggle("Hide the \(kAppName) logo", isOn: settings.hideLogo)
                    .disabled(s.menuBarMode == .iconOnly)
                    .padding(.vertical, 6)
                Divider()
                PrefRow(label: "Preview") {
                    HStack(spacing: 10) {
                        MenuBarPreview(look: StatusItemLook.sample(.idleReview, settings: s))
                        MenuBarPreview(look: StatusItemLook.sample(.processing, settings: s))
                        MenuBarPreview(look: StatusItemLook.sample(.paused, settings: s))
                    }
                }
            }
            Text("Preview: idle with 3 clips to review · processing · paused because Resolve is open. With the logo hidden, it still shows when there's nothing else to show. ⌘-drag the icon in the menu bar to move it.")
                .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 2).padding(.top, -12)
            PrefGroup(title: "Keyboard shortcut",
                      footer: hotKeys.registrationFailed && s.hotKey.enabled
                        ? "⚠︎ macOS refused \(s.hotKey.display). Another app probably uses it; record a different one."
                        : "Opens or closes the \(kAppName) popover from anywhere. Needs ⌃, ⌥ or ⌘. (GrokGauge uses ⌃⌥G.)") {
                Toggle("Open the popover with a shortcut", isOn: settings.hotKey.enabled)
                    .padding(.vertical, 6)
                Divider()
                PrefRow(label: "Shortcut") {
                    HotKeyRecorder(hotKey: settings.hotKey, onRecording: { recording in
                        if recording { hotKeys.unregister() } else { hotKeys.register(store.settings.hotKey) }
                    })
                    .disabled(!s.hotKey.enabled)
                    Button("Reset") { store.settings.hotKey = .standard }
                        .disabled(s.hotKey == .standard)
                }
            }
            PrefGroup(title: "In-app shortcuts") {
                shortcutRow("Settings…", "⌘,")
                Divider()
                shortcutRow("Setup…", "⌥⌘,")
                Divider()
                shortcutRow("Open \(kAppName)", "⌘0")
                Divider()
                shortcutRow("Ask the Model…", "⌘K")
                Divider()
                shortcutRow("Instructions…", "⌘I")
            }
        }
    }

    private func shortcutRow(_ label: String, _ keys: String) -> some View {
        PrefRow(label: label) { Text(keys).font(.system(.body, design: .rounded)).foregroundStyle(.secondary) }
    }

    // MARK: Colors & alerts

    private var colorsTab: some View {
        let s = store.settings
        return Group {
            PrefGroup(title: "Colors", footer: similarityNote(s.colors)) {
                colorRow("Normal", "Processing · OK", .normal, s.colors.normal == nil)
                Divider()
                colorRow("Warning", "Paused for Resolve · clips to review", .warning, s.colors.warning == nil)
                Divider()
                colorRow("Critical", "Errors", .critical, s.colors.critical == nil)
                Divider()
                HStack {
                    Text("Presets").foregroundStyle(.secondary)
                    Spacer()
                    Button("Colorblind-Friendly") { store.settings.colors = .colorblindFriendly }
                        .help("Okabe–Ito blue / orange / vermilion")
                    Button("Reset Colors") { store.resetColors() }
                        .disabled(s.colors == .system)
                }
                .padding(.vertical, 7)
            }
            PrefGroup(title: "Preview") {
                HStack(spacing: 18) {
                    swatch("Processing", .normal)
                    swatch("Paused", .warning)
                    swatch("Errors", .critical)
                    Spacer()
                }
                .padding(.vertical, 8)
            }
            PrefGroup(title: "Notifications",
                      footer: "macOS asks once for permission. If notifications are off for \(kAppName) in System Settings › Notifications, it falls back to a plain banner.") {
                Toggle("Runs: started, finished, stopped, sorted", isOn: settings.notifyRuns).padding(.vertical, 6)
                Divider()
                Toggle("Clips that need review", isOn: settings.notifyReview).padding(.vertical, 6)
                Divider()
                Toggle("Model and tool updates", isOn: settings.notifyUpdates).padding(.vertical, 6)
            }
            PrefGroup(title: "General") {
                PrefRow(label: "Launch at login",
                        detail: model.inApplications ? (model.loginEnabled ? "Starts when you log in" : nil)
                                                     : "Needs \(kAppName) in Applications (it runs from the project folder now)") {
                    Toggle("Launch at login", isOn: Binding(get: { model.loginEnabled }, set: { _ in model.toggleLogin() }))
                        .toggleStyle(.switch)
                        .labelsHidden()
                        .disabled(!model.inApplications)
                }
            }
        }
    }

    private func swatch(_ label: String, _ level: Level) -> some View {
        VStack(spacing: 4) {
            RingGauge(fraction: 0.66, color: palette.color(level), lineWidth: 6).frame(width: 40, height: 40)
            Text(label).font(.caption2).foregroundStyle(.secondary)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(label) color")
    }

    private func colorRow(_ name: String, _ meaning: String, _ level: Level, _ isSystem: Bool) -> some View {
        PrefRow(label: name, detail: meaning + " · " + (isSystem ? "System color" : (store.settings.colors.color(for: level)?.hex ?? ""))) {
            ColorPicker(name, selection: colorBinding(level), supportsOpacity: false)
                .labelsHidden()
        }
    }

    private func colorBinding(_ level: Level) -> Binding<Color> {
        Binding(
            get: { palette.color(level) },
            set: { new in
                guard let rgba = RGBAColor(nsColor: NSColor(new)) else { return }
                switch level {
                case .normal: store.settings.colors.normal = rgba
                case .warning: store.settings.colors.warning = rgba
                case .critical: store.settings.colors.critical = rgba
                case .neutral: break
                }
            })
    }

    private func similarityNote(_ colors: LevelColors) -> String {
        let pairs = colors.similarPairs()
        guard let (a, b) = pairs.first else {
            return "Used for the status ring, the menu bar and status icons everywhere. Click a color to open the macOS color picker."
        }
        let r = colors.resolved
        func rgba(_ l: Level) -> RGBAColor { l == .normal ? r.normal : l == .warning ? r.warning : r.critical }
        let dE = Int(ColorMath.deltaE2000(rgba(a), rgba(b)).rounded())
        return "⚠︎ \(a.name.capitalized) and \(b.name.capitalized) are hard to tell apart (ΔE \(dE)). Pick more distinct colors, or try the colorblind-friendly preset."
    }

    // MARK: Sync

    private var syncTab: some View {
        Group {
            PrefGroup(title: "Sync settings between Macs",
                      footer: "\(kAppName) keeps one small file, **ClipGauge/settings.json**, in this folder and watches it for changes. Changes merge group by group and the newest wins; an existing file is never replaced without merging (the previous copy is backed up on this Mac). Only preferences sync — never clips, notes, transcripts, models or the project folder. The folder choice stays on this Mac.") {
                PrefRow(label: "Folder", detail: store.syncFolder.map(Self.abbreviate) ?? "Not syncing") {
                    Button(store.syncFolder == nil ? "Choose…" : "Change…") { store.chooseSyncFolder() }
                    if store.syncFolder != nil {
                        Button("Stop Syncing") { store.setSyncFolder(nil) }
                    }
                }
                if store.syncFolder == nil, let hint = store.suggestedFolder {
                    Divider()
                    PrefRow(label: "Suggested folder",
                            detail: Self.abbreviate(hint) + (hint == LayoutStore.grokGaugeSyncFolder() ? " · shared with GrokGauge" : "")) {
                        Button("Use It") { store.setSyncFolder(hint) }
                    }
                }
                if store.syncFolder != nil {
                    Divider()
                    PrefRow(label: "Status", detail: syncStatusText) {
                        Button("Sync Now") { store.reconcile() }
                    }
                }
            }
            PrefGroup(title: "What syncs", footer: "Per-Mac items stay local: the sync folder, the project folder, launch at login, dismissed alerts and window positions.") {
                PrefRow(label: "Layout", detail: "Popover sections and action buttons") { Image(systemName: "checkmark").foregroundStyle(.secondary) }
                Divider()
                PrefRow(label: "Menu bar", detail: "Display mode, time left, logo, keyboard shortcut") { Image(systemName: "checkmark").foregroundStyle(.secondary) }
                Divider()
                PrefRow(label: "Colors & alerts", detail: "Level colors, notification choices, update check") { Image(systemName: "checkmark").foregroundStyle(.secondary) }
            }
            PrefGroup(title: "Back up or move settings", footer: "Exports the same JSON that sync uses. Importing counts as a change on this Mac.") {
                PrefRow(label: "Settings file") {
                    Button("Export…") { store.exportSettings() }
                    Button("Import…") { store.importSettings() }
                }
            }
            HStack {
                Spacer()
                Button("Reset All Settings…") {
                    let a = NSAlert()
                    a.messageText = "Reset all \(kAppName) settings?"
                    a.informativeText = "Layout, menu bar, colors, notifications and the shortcut go back to their defaults. The sync folder choice is kept."
                    a.addButton(withTitle: "Reset")
                    a.addButton(withTitle: "Cancel")
                    if a.runModal() == .alertFirstButtonReturn { store.resetAll() }
                }
            }
        }
        .onAppear { store.findSuggestedFolder() }
    }

    private var syncStatusText: String {
        var t = Diagnostics.syncText(store.syncState)
        if case .synced = store.syncState, let n = store.lastSyncNote { t += " · " + n }
        return t
    }

    static func abbreviate(_ url: URL) -> String {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let p = url.path
        return p.hasPrefix(home) ? "~" + p.dropFirst(home.count) : p
    }
}
