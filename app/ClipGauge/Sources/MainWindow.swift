// ClipGauge main window (v0.6): the all-in-one app that replaced the web UI.
//   Add Clips — drop files/folders (here or on the menu bar icon) or Choose…; copy into the inbox (progress, never
//               overwrites) or process in place (guards: held / Resolve-internal / Cloud-synced folders refused,
//               DaVinci Resolve folder only after confirmation).
//   Results  — old → new name, type, confidence, status; reveal in Finder; per-row / per-batch Undo; needs-review
//               filter and a review editor (accept or edit the proposed name — logged and undoable).
//   Tools    — move renamed clips into a folder, transcript bundle (exports/), Sort into Projects, Instructions.
// Every action goes through scripts/clipgauge_cli.py (same locks and guards as the engine); nothing here renames.
import AppKit
import SwiftUI
import UniformTypeIdentifiers

enum MainTab: String, CaseIterable { case add = "Add Clips", results = "Results", tools = "Tools" }
enum AddMode: String, CaseIterable { case copy = "Copy into inbox", inPlace = "Process in place" }
enum ResultFilter: String, CaseIterable { case all = "All", review = "Needs review", renamed = "Renamed", other = "Skipped / dry run" }

struct QueuedItem: Identifiable {
    let id = UUID()
    let url: URL
    var check: [String: Any]? = nil
    var status: String { (check?["status"] as? String) ?? "checking" }
    var reason: String { (check?["reason"] as? String) ?? "" }
    var mediaCount: Int { intVal(check?["media_count"]) ?? 0 }
    var copyCount: Int { intVal(check?["copy_count"]) ?? 0 }
    var bytes: Int64 { (check?["bytes"] as? NSNumber)?.int64Value ?? 0 }
    var needsConfirm: Bool { (check?["needs_confirm"] as? Bool) ?? false }
    var copyOK: Bool { (check?["copy_ok"] as? Bool) ?? true }
    var isDir: Bool { (try? url.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) == true }
}

struct ResultRow: Identifiable {
    let id: String
    let d: [String: Any]
    init(_ d: [String: Any]) {
        self.d = d
        id = (d["source"] as? String ?? UUID().uuidString) + "|" + (d["time"] as? String ?? "")
    }
    var name: String { d["name"] as? String ?? "?" }
    var source: String { d["source"] as? String ?? "" }
    var current: String { d["current"] as? String ?? source }
    var proposed: String? { d["proposed"] as? String }
    var clipType: String { d["clip_type"] as? String ?? "—" }
    var confidence: Double? { (d["confidence"] as? NSNumber)?.doubleValue }
    var status: String { d["status"] as? String ?? "skipped" }
    var logID: String? { d["log_id"] as? String }
    var batch: String? { d["batch"] as? String }
    var reasons: [String] { d["review_reasons"] as? [String] ?? [] }
    var newName: String {
        if status == "renamed" { return (current as NSString).lastPathComponent }
        return proposed ?? "—"
    }
    var time: Date? { (d["time"] as? String).flatMap { isoParser.date(from: $0) } }
    var inResolve: Bool { current.contains("/DaVinci Resolve/") || current.contains("/DaVinci Resolve Media/") }
}

final class MainModel: ObservableObject {
    weak var renamer: RenamerModel?
    @Published var tab: MainTab = .add
    @Published var queue: [QueuedItem] = []
    @Published var mode: AddMode = .copy
    @Published var folderName = ""
    @Published var folderAsProject = false
    @Published var startAfterCopy = true
    @Published var dryRunOnly = false
    @Published var copying = false
    @Published var copyFraction = 0.0
    @Published var copyLine = ""
    @Published var lastMessage: String?
    @Published var rows: [ResultRow] = []
    @Published var filter: ResultFilter = .all
    @Published var loadingResults = false
    @Published var editing: ResultRow?
    @Published var editName = ""
    @Published var busy: String?
    @Published var moveFolder = ""
    @Published var scopes: [[String: Any]] = []
    @Published var scope = "inbox"
    @Published var includeSilent = false
    @Published var excludePhotos = false
    @Published var bundle: [String: Any]?
    @Published var exports: [[String: Any]] = []
    @Published var exportsDir: String?
    @Published var exampleData = false
    private var copyProc: Process?

    init(renamer: RenamerModel) { self.renamer = renamer }

    // MARK: add clips

    func add(_ urls: [URL]) {
        let fresh = urls.map { $0.standardizedFileURL }.filter { u in !queue.contains { $0.url == u } }
        guard !fresh.isEmpty else { return }
        queue += fresh.map { QueuedItem(url: $0) }
        tab = .add
        if folderName.isEmpty, fresh.count == 1, let f = fresh.first,
           (try? f.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) == true {
            folderName = f.lastPathComponent
        }
        recheck()
    }

    func recheck() {
        guard let r = renamer, !queue.isEmpty else { return }
        r.engine(["check-path"] + queue.map { $0.url.path }) { j in
            let items = (j["items"] as? [[String: Any]]) ?? []
            for (i, it) in items.enumerated() where i < self.queue.count { self.queue[i].check = it }
            if let e = j["error"] as? String { self.lastMessage = e }
        }
    }

    func remove(_ item: QueuedItem) { queue.removeAll { $0.id == item.id } }
    func clear() { queue.removeAll(); lastMessage = nil }

    func choose() {
        let p = NSOpenPanel()
        p.canChooseFiles = true
        p.canChooseDirectories = true
        p.allowsMultipleSelection = true
        p.message = "Choose clips or folders of clips (videos and photos)."
        p.prompt = "Add"
        NSApp.activate(ignoringOtherApps: true)
        if p.runModal() == .OK { add(p.urls) }
    }

    var usable: [QueuedItem] {
        mode == .copy ? queue.filter { $0.copyOK && $0.copyCount > 0 }
                      : queue.filter { $0.status != "refused" && $0.status != "checking" && $0.mediaCount > 0 }
    }
    var clipCount: Int { usable.reduce(0) { $0 + (mode == .copy ? $1.copyCount : $1.mediaCount) } }
    var needsConfirm: Bool { mode == .inPlace && usable.contains { $0.needsConfirm } }

    func go() {
        guard let r = renamer, !usable.isEmpty else { return }
        if mode == .copy { copyIn(r) } else { processInPlace(r) }
    }

    private func copyIn(_ r: RenamerModel) {
        copying = true; copyFraction = 0; copyLine = "Starting copy…"; lastMessage = nil
        let paths = usable.map { $0.url.path }
        copyProc = r.engineStream(["copy-in"] + paths, line: { j in
            switch j["event"] as? String {
            case "start":
                self.copyLine = "Copying \(intVal(j["files"]) ?? 0) file(s), \(byteString(Int64(intVal(j["total_bytes"]) ?? 0)))…"
            case "progress", "file":
                let done = Double(intVal(j["done_bytes"]) ?? 0), tot = Double(max(1, intVal(j["total_bytes"]) ?? 1))
                self.copyFraction = done / tot
                let saved = (j["saved_as"] as? String).map { " → \($0)" } ?? ""
                self.copyLine = "\(intVal(j["index"]) ?? 0) of \(intVal(j["files"]) ?? 0): \(j["file"] as? String ?? "")\(saved)"
            case "done":
                let copied = (j["copied"] as? [[String: Any]])?.count ?? 0
                let skipped = (j["skipped"] as? [String]) ?? []
                var msg = (j["message"] as? String) ?? (j["error"] as? String) ?? "Copy finished."
                if let e = j["error"] as? String { msg = e }
                if !skipped.isEmpty { msg += "\n" + skipped.prefix(6).joined(separator: "\n") + (skipped.count > 6 ? "\n…" : "") }
                self.lastMessage = msg
                self.copyFraction = 1
                if (j["ok"] as? Bool) == true {
                    self.queue.removeAll { item in self.usable.contains { $0.id == item.id } }
                    r.refreshInbox(force: true)
                    if self.startAfterCopy && copied > 0 { self.startInbox(r) }
                }
            default: break
            }
        }, done: { _ in
            self.copying = false
            self.copyProc = nil
        })
    }

    func cancelCopy() { copyProc?.terminate() }

    private func startInbox(_ r: RenamerModel) {
        r.engine(["inbox"]) { inbox in
            let pending = intVal(inbox["pending_count"]) ?? 0
            guard pending > 0 else { return }
            let f = self.folderName.trimmingCharacters(in: .whitespaces)
            r.startRun(RunOptions(dry: self.dryRunOnly, folder: f.isEmpty ? nil : f, projectFromFolder: self.folderAsProject),
                       pending: pending)
        }
    }

    private func processInPlace(_ r: RenamerModel) {
        var o = RunOptions(dry: dryRunOnly, sources: usable.map { $0.url.path })
        if needsConfirm && !(dryRunOnly || r.dryRun) {
            guard r.confirmResolveRename() else { return }
            o.confirmResolve = true
        }
        r.startRun(o, pending: clipCount, confirmed: false)
    }

    // MARK: results

    var filtered: [ResultRow] {
        switch filter {
        case .all: return rows
        case .review: return rows.filter { $0.status == "needs review" }
        case .renamed: return rows.filter { $0.status == "renamed" }
        case .other: return rows.filter { $0.status != "renamed" && $0.status != "needs review" }
        }
    }
    var reviewCount: Int { rows.filter { $0.status == "needs review" }.count }

    func loadResults() {
        guard let r = renamer, !exampleData else { return }
        loadingResults = true
        r.engine(["results", "--limit", "400"]) { j in
            self.loadingResults = false
            if intVal(j["code"]) == 200 { self.rows = ((j["items"] as? [[String: Any]]) ?? []).map(ResultRow.init) }
            else { self.lastMessage = RenamerModel.errorText(j) }
        }
    }

    func reveal(_ row: ResultRow) {
        let u = URL(fileURLWithPath: row.current)
        if fileExists(u) { NSWorkspace.shared.activateFileViewerSelecting([u]) }
        else if fileExists(u.deletingLastPathComponent()) { NSWorkspace.shared.open(u.deletingLastPathComponent()) }
    }

    func undo(_ row: ResultRow) {
        guard let r = renamer, let id = row.logID else { return }
        let what = row.status == "renamed" ? "Rename \(row.newName) back to \(row.name)?" : "Clear the needs-review mark on \(row.name)? (It will be processed again on the next run.)"
        guard r.confirm("Undo", what + "\n\nThis is logged too.", ok: "Undo") else { return }
        busy = "Undoing…"
        r.engine(["undo", "--id", id]) { j in self.finish(j, title: "Undo") }
    }

    func undoBatch(_ row: ResultRow) {
        guard let r = renamer, let b = row.batch else { return }
        busy = "Checking batch…"
        r.engine(["undo", "--batch", b, "--preview"]) { p in
            self.busy = nil
            let n = intVal(p["total"]) ?? 0
            guard n > 0 else { r.alert("Nothing to undo", (p["message"] as? String) ?? "That batch has nothing left to undo."); return }
            guard r.confirm("Undo the whole batch?", "\(n) rename(s) / review mark(s) from batch \(b) will be reversed (clips get their original names back).", ok: "Undo \(n)") else { return }
            self.busy = "Undoing batch…"
            r.engine(["undo", "--batch", b]) { j in self.finish(j, title: "Undo batch") }
        }
    }

    func startEdit(_ row: ResultRow) {
        editing = row
        editName = row.proposed ?? row.name
    }

    func acceptEdit() {
        guard let r = renamer, let row = editing else { return }
        var args = ["review-accept", "--file", row.current, "--name", editName]
        if row.inResolve {
            guard r.confirmResolveRename() else { return }
            args.append("--confirm-resolve")
        }
        busy = "Renaming…"
        r.engine(args) { j in
            if intVal(j["code"]) == 200 { self.editing = nil }
            self.finish(j, title: "Rename")
        }
    }

    private func finish(_ j: [String: Any], title: String) {
        busy = nil
        let code = intVal(j["code"]) ?? 0
        if code >= 200 && code < 300 && (j["errors"] as? [Any] ?? []).isEmpty {
            lastMessage = j["message"] as? String
        } else {
            renamer?.alert("\(title) didn't finish", RenamerModel.errorText(j) + ((j["errors"] as? [String]).map { "\n" + $0.joined(separator: "\n") } ?? ""))
        }
        renamer?.refreshInbox(force: true)
        loadResults()
    }

    // MARK: tools

    var movableCount: Int { intVal(renamer?.inbox["movable_count"]) ?? 0 }

    func moveInto() {
        guard let r = renamer else { return }
        let f = moveFolder.trimmingCharacters(in: .whitespaces)
        guard !f.isEmpty else { return }
        guard r.confirm("Move renamed clips into “\(f)”?", "\(movableCount) renamed clip(s) at the top of inbox/ (and their notes) move into inbox/\(f)/. Logged and undoable.", ok: "Move") else { return }
        busy = "Moving…"
        r.engine(["move-into", f]) { j in self.finish(j, title: "Move") }
    }

    func loadScopes() {
        renamer?.engine(["bundle-scopes"]) { j in
            self.scopes = (j["scopes"] as? [[String: Any]]) ?? []
            self.exports = (j["exports"] as? [[String: Any]]) ?? []
            self.exportsDir = j["exports_dir"] as? String
            if !self.scopes.contains(where: { ($0["value"] as? String) == self.scope }) {
                self.scope = (self.scopes.first?["value"] as? String) ?? "inbox"
            }
        }
    }

    func buildBundle() {
        guard let r = renamer else { return }
        busy = "Building transcript bundle…"
        let opts: [String: Any] = ["scope": scope, "include_silent": includeSilent, "exclude_photos": excludePhotos]
        let s = String(decoding: (try? JSONSerialization.data(withJSONObject: opts)) ?? Data("{}".utf8), as: UTF8.self)
        r.engine(["bundle", s]) { j in
            self.busy = nil
            if intVal(j["code"]) == 200 { self.bundle = j; self.loadScopes() }
            else { r.alert("Couldn't build the bundle", RenamerModel.errorText(j)) }
        }
    }

    func exportURL(_ name: String) -> URL? { exportsDir.map { URL(fileURLWithPath: $0).appendingPathComponent(name) } }
}

// MARK: - views

struct MainView: View {
    @ObservedObject var main: MainModel
    @ObservedObject var model: RenamerModel
    var scrolls = true
    @ViewState private var dropTarget = false

    var body: some View {
        VStack(spacing: 0) {
            RunBar(main: main, model: model)
            Divider()
            Picker("", selection: $main.tab) {
                ForEach(MainTab.allCases, id: \.self) { t in
                    Text(t == .results && main.reviewCount > 0 ? "Results (\(main.reviewCount) to review)" : t.rawValue).tag(t)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .padding(.horizontal, 20)
            .padding(.top, 12)
            .frame(maxWidth: 560)
            Group {
                switch main.tab {
                case .add: AddClipsView(main: main, model: model)
                case .results: ResultsView(main: main, model: model, scrolls: scrolls)
                case .tools: ToolsView(main: main, model: model)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: scrolls ? .infinity : nil, alignment: .top)
            if let m = main.lastMessage {
                Divider()
                HStack(alignment: .top) {
                    Image(systemName: "info.circle").foregroundStyle(.secondary)
                    Text(m).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer()
                    Button { main.lastMessage = nil } label: { Image(systemName: "xmark") }.buttonStyle(.borderless)
                }
                .padding(.horizontal, 20).padding(.vertical, 8)
            }
        }
        .frame(minWidth: 860, minHeight: scrolls ? 600 : nil)
        .overlay(dropTarget ? RoundedRectangle(cornerRadius: 12).stroke(Color.accentColor, lineWidth: 3).padding(4) : nil)
        .onDrop(of: [UTType.fileURL], isTargeted: $dropTarget) { providers in
            loadURLs(providers) { main.add($0) }
            return true
        }
        .onAppear { if main.tab == .results { main.loadResults() } }
        .onChange(of: main.tab) { t in
            if t == .results { main.loadResults() }
            if t == .tools { main.loadScopes(); model.refreshInbox(force: true) }
        }
    }
}

/// File URLs from drag-and-drop providers (Finder files and folders).
func loadURLs(_ providers: [NSItemProvider], _ done: @escaping ([URL]) -> Void) {
    final class Box: @unchecked Sendable { var urls: [URL] = []; let lock = NSLock() }
    let box = Box()
    let g = DispatchGroup()
    for p in providers where p.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier) {
        g.enter()
        _ = p.loadObject(ofClass: URL.self) { u, _ in
            if let u { box.lock.lock(); box.urls.append(u); box.lock.unlock() }
            g.leave()
        }
    }
    g.notify(queue: .main) { done(box.urls) }
}

/// Status + Start / Stop + dry-run toggle (calls the engine directly; same locks and guards as everywhere).
private struct RunBar: View {
    @ObservedObject var main: MainModel
    @ObservedObject var model: RenamerModel

    var body: some View {
        let running = model.runActive && model.isPipelineRun
        HStack(spacing: 14) {
            ZStack {
                RingGauge(fraction: Double(model.progress?.pct ?? 0) / 100, color: model.level.color, lineWidth: 6)
                Text(model.progress.map { "\($0.pct)%" } ?? "—").font(.system(size: 12, weight: .bold, design: .rounded)).monospacedDigit()
            }
            .frame(width: 52, height: 52)
            VStack(alignment: .leading, spacing: 2) {
                Text(model.headline).font(.headline)
                Text(running ? (model.currentClip.map { "\(model.subline) · \($0)" } ?? model.subline) : model.subline)
                    .font(.caption).foregroundStyle(.secondary).lineLimit(1)
                if let e = model.eta, running {
                    Text("done ≈ \(clockString(e))").font(.caption2).foregroundStyle(.secondary)
                }
            }
            Spacer()
            Toggle("Dry run (preview only)", isOn: $main.dryRunOnly)
                .toggleStyle(.checkbox)
                .disabled(model.dryRun)
                .help(model.dryRun ? "config dry_run is on — every run is a dry run" : "Process without renaming anything (notes go to notes/dry-run/)")
            Button {
                if running { model.stopRun() }
                else {
                    model.engine(["inbox"]) { j in
                        let pending = intVal(j["pending_count"]) ?? 0
                        if pending == 0 { model.alert("Nothing to process", "No unprocessed clips in inbox/. Add clips on the Add Clips tab first."); return }
                        let f = main.folderName.trimmingCharacters(in: .whitespaces)
                        model.startRun(RunOptions(dry: main.dryRunOnly, folder: main.mode == .copy && !f.isEmpty ? f : nil,
                                                  projectFromFolder: main.folderAsProject), pending: pending)
                    }
                }
            } label: {
                Label(running ? "Stop" : "Start inbox", systemImage: running ? "stop.fill" : "play.fill").frame(minWidth: 90)
            }
            .buttonStyle(.borderedProminent)
            .tint(running ? .red : .accentColor)
            .controlSize(.large)
            .disabled(!model.lexarOK || model.actionBusy != nil || (!running && model.startBlocker != nil))
            .help(running ? "Stop processing" : (model.startBlocker ?? "Process every unprocessed clip in inbox/"))
            Button { model.showChat(nil) } label: { Label("Ask", systemImage: "sparkles") }
                .controlSize(.large)
                .help("Ask the local model (⌘K)")
                .keyboardShortcut("k")
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 12)
    }
}

private struct AddClipsView: View {
    @ObservedObject var main: MainModel
    @ObservedObject var model: RenamerModel

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            if main.queue.isEmpty {
                DropZone(main: main)
            } else {
                HStack {
                    Text("\(main.queue.count) item(s) · \(main.clipCount) clip(s) to \(main.mode == .copy ? "copy" : "process")").font(.headline)
                    if main.exampleData {
                        Text("EXAMPLE DATA").font(.caption2.weight(.bold)).padding(.horizontal, 6).padding(.vertical, 2)
                            .background(Color.orange.opacity(0.25), in: Capsule())
                    }
                    Spacer()
                    Button("Choose…") { main.choose() }
                    Button("Clear") { main.clear() }.disabled(main.copying)
                }
                VStack(spacing: 0) {
                    ForEach(main.queue) { item in
                        QueueRow(item: item, mode: main.mode) { main.remove(item) }
                        Divider()
                    }
                }
                .background(.fill.quinary, in: RoundedRectangle(cornerRadius: 10))
            }
            Picker("Mode", selection: $main.mode) {
                ForEach(AddMode.allCases, id: \.self) { Text($0.rawValue).tag($0) }
            }
            .pickerStyle(.radioGroup)
            .horizontalRadioGroupLayout()
            if main.mode == .copy {
                VStack(alignment: .leading, spacing: 8) {
                    Text("Clips are copied to inbox/ (originals untouched, nothing overwritten — same name and size is skipped, otherwise _2, _3 …). Renamed clips can go into a folder:")
                        .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    HStack {
                        TextField("Folder name (optional), e.g. Retro Game Expo", text: $main.folderName).frame(maxWidth: 360)
                        Toggle("Use folder name as the project in filenames", isOn: $main.folderAsProject)
                            .disabled(main.folderName.trimmingCharacters(in: .whitespaces).isEmpty)
                    }
                    Toggle("Start processing when the copy finishes", isOn: $main.startAfterCopy)
                }
            } else {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Clips are renamed where they are — no copy. Folders on hold (Held Project, Archive Footage, Old Card Dump), DaVinci Resolve's own folders and Cloud-synced projects are refused. Inside the DaVinci Resolve folder you'll be asked first: renaming media Resolve already imported breaks its links.")
                        .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    if main.needsConfirm {
                        Label("Some clips are inside the DaVinci Resolve folder — you'll be asked to confirm.", systemImage: "exclamationmark.triangle.fill")
                            .font(.caption).foregroundStyle(.orange)
                    }
                }
            }
            if main.copying {
                VStack(alignment: .leading, spacing: 4) {
                    ProgressView(value: main.copyFraction)
                    HStack {
                        Text(main.copyLine).font(.caption).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
                        Spacer()
                        Button("Cancel") { main.cancelCopy() }.controlSize(.small)
                    }
                }
            }
            HStack {
                Spacer()
                Button {
                    main.go()
                } label: {
                    Label(main.mode == .copy ? (main.startAfterCopy ? "Copy & Process" : "Copy into Inbox") : "Process in Place",
                          systemImage: main.mode == .copy ? "square.and.arrow.down.on.square" : "wand.and.stars")
                        .frame(minWidth: 150)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                .disabled(main.usable.isEmpty || main.copying || !model.lexarOK
                          || (main.mode == .inPlace && model.startBlocker != nil))
            }
        }
        .padding(20)
    }
}

private struct DropZone: View {
    @ObservedObject var main: MainModel
    var body: some View {
        VStack(spacing: 10) {
            Image(systemName: "square.and.arrow.down.on.square").font(.system(size: 34)).foregroundStyle(.secondary)
            Text("Drop clips or folders here").font(.title3.weight(.semibold))
            Text("…or on the \(kAppName) menu bar icon. Videos and photos; folders are searched (hidden and held folders skipped).")
                .font(.caption).foregroundStyle(.secondary).multilineTextAlignment(.center)
            Button("Choose…") { main.choose() }.controlSize(.large)
        }
        .frame(maxWidth: .infinity, minHeight: 180)
        .background(RoundedRectangle(cornerRadius: 14).strokeBorder(style: StrokeStyle(lineWidth: 1.5, dash: [6, 5])).foregroundStyle(.tertiary))
    }
}

private struct QueueRow: View {
    let item: QueuedItem
    let mode: AddMode
    let remove: () -> Void
    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: item.isDir ? "folder.fill" : "film").foregroundStyle(.secondary).frame(width: 20)
            VStack(alignment: .leading, spacing: 2) {
                Text(item.url.lastPathComponent).font(.callout.weight(.medium)).lineLimit(1)
                Text(item.url.deletingLastPathComponent().path).font(.caption2).foregroundStyle(.tertiary).lineLimit(1).truncationMode(.middle)
            }
            Spacer()
            verdict
            Button(action: remove) { Image(systemName: "xmark.circle.fill").foregroundStyle(.tertiary) }.buttonStyle(.borderless)
        }
        .padding(.horizontal, 12).padding(.vertical, 8)
    }

    @ViewBuilder private var verdict: some View {
        if item.status == "checking" {
            ProgressView().controlSize(.small)
        } else if mode == .copy {
            if !item.copyOK { tag("on hold — not copied", .orange) }
            else { tag("\(item.copyCount) clip(s)" + (item.bytes > 0 ? " · \(byteString(item.bytes))" : ""), .secondary) }
        } else if item.status == "refused" {
            tag(item.reason, .red).help(item.reason)
        } else if item.needsConfirm {
            tag("\(item.mediaCount) clip(s) · DaVinci Resolve — asks first", .orange).help(item.reason)
        } else {
            tag("\(item.mediaCount) clip(s) · OK in place", .green)
        }
    }

    private func tag(_ s: String, _ c: Color) -> some View {
        Text(s).font(.caption).foregroundStyle(c).lineLimit(1).truncationMode(.tail).frame(maxWidth: 320, alignment: .trailing)
    }
}

private struct ResultsView: View {
    @ObservedObject var main: MainModel
    @ObservedObject var model: RenamerModel
    var scrolls: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Picker("Show", selection: $main.filter) {
                    ForEach(ResultFilter.allCases, id: \.self) { f in
                        Text(f == .review ? "Needs review (\(main.reviewCount))" : f.rawValue).tag(f)
                    }
                }
                .frame(maxWidth: 300)
                if main.exampleData {
                    Text("EXAMPLE DATA").font(.caption2.weight(.bold)).padding(.horizontal, 6).padding(.vertical, 2)
                        .background(Color.orange.opacity(0.25), in: Capsule())
                }
                Spacer()
                if main.loadingResults || main.busy != nil { ProgressView().controlSize(.small) }
                if let b = main.busy { Text(b).font(.caption).foregroundStyle(.secondary) }
                Button { main.loadResults() } label: { Label("Refresh", systemImage: "arrow.clockwise") }
            }
            header
            if scrolls {
                ScrollView { rowsList }
            } else {
                rowsList
            }
            if let e = main.editing { ReviewEditor(main: main, row: e) }
        }
        .padding(20)
    }

    private var header: some View {
        HStack(spacing: 8) {
            Text("Original").frame(width: 220, alignment: .leading)
            Text("New name").frame(maxWidth: .infinity, alignment: .leading)
            Text("Type").frame(width: 90, alignment: .leading)
            Text("Conf.").frame(width: 44, alignment: .trailing)
            Text("Status").frame(width: 100, alignment: .leading)
            Text("").frame(width: 120)
        }
        .font(.caption.weight(.semibold)).foregroundStyle(.secondary)
        .padding(.horizontal, 10)
    }

    private var rowsList: some View {
        LazyVStack(spacing: 0) {
            if main.filtered.isEmpty {
                Text(main.rows.isEmpty ? "No results yet — finished clips appear here as each one is processed." : "Nothing in this filter.")
                    .font(.callout).foregroundStyle(.secondary).padding(30)
            }
            ForEach(main.filtered) { row in
                ResultLine(main: main, row: row)
                Divider()
            }
        }
        .background(.fill.quinary, in: RoundedRectangle(cornerRadius: 10))
    }
}

private struct ResultLine: View {
    @ObservedObject var main: MainModel
    let row: ResultRow
    @ViewState private var hover = false

    var body: some View {
        HStack(spacing: 8) {
            Text(row.name).lineLimit(1).truncationMode(.middle).frame(width: 220, alignment: .leading)
            VStack(alignment: .leading, spacing: 1) {
                Text(row.newName).lineLimit(1).truncationMode(.middle)
                    .foregroundStyle(row.status == "renamed" ? Color.primary : Color.secondary)
                if row.status == "needs review", let r = row.reasons.first {
                    Text(r).font(.caption2).foregroundStyle(.orange).lineLimit(1).truncationMode(.tail)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            Text(row.clipType).foregroundStyle(.secondary).lineLimit(1).frame(width: 90, alignment: .leading)
            Text(row.confidence.map { String(format: "%.2f", $0) } ?? "—").monospacedDigit().foregroundStyle(.secondary)
                .frame(width: 44, alignment: .trailing)
            statusTag.frame(width: 100, alignment: .leading)
            HStack(spacing: 6) {
                if row.status == "needs review" {
                    Button("Review…") { main.startEdit(row) }.controlSize(.small)
                }
                Menu {
                    Button("Show in Finder") { main.reveal(row) }
                    if row.logID != nil {
                        Button(row.status == "renamed" ? "Undo this rename" : "Clear needs-review mark") { main.undo(row) }
                    }
                    if row.batch != nil { Button("Undo whole batch…") { main.undoBatch(row) } }
                    if row.status != "needs review", row.proposed != nil, row.status != "renamed" {
                        Button("Rename to proposed name…") { main.startEdit(row) }
                    }
                } label: { Image(systemName: "ellipsis.circle") }
                .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
            }
            .frame(width: 120, alignment: .trailing)
        }
        .font(.callout)
        .padding(.horizontal, 10).padding(.vertical, 7)
        .background(hover ? Color.primary.opacity(0.05) : .clear)
        .contentShape(Rectangle())
        .onHover { hover = $0 }
        .onTapGesture(count: 2) { main.reveal(row) }
        .help(row.current)
    }

    private var statusTag: some View {
        let (txt, c): (String, Color) = {
            switch row.status {
            case "renamed": return ("renamed", .green)
            case "needs review": return ("needs review", .orange)
            case "dry run": return ("dry run", .blue)
            default: return ("skipped", .secondary)
            }
        }()
        return Text(txt).font(.caption.weight(.semibold)).foregroundStyle(c)
            .padding(.horizontal, 6).padding(.vertical, 2).background(c.opacity(0.14), in: Capsule())
    }
}

private struct ReviewEditor: View {
    @ObservedObject var main: MainModel
    let row: ResultRow
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Review \(row.name)").font(.headline)
            if !row.reasons.isEmpty {
                Text("Why: " + row.reasons.joined(separator: "; ")).font(.caption).foregroundStyle(.secondary)
            }
            HStack {
                TextField("New file name", text: $main.editName).textFieldStyle(.roundedBorder)
                Button("Use proposed") { main.editName = row.proposed ?? main.editName }.disabled(row.proposed == nil)
            }
            Text("The extension is kept. Renaming is logged in rename-log.jsonl and can be undone from this table.")
                .font(.caption2).foregroundStyle(.secondary)
            HStack {
                Button("Show in Finder") { main.reveal(row) }
                Spacer()
                Button("Cancel") { main.editing = nil }
                Button("Rename") { main.acceptEdit() }.buttonStyle(.borderedProminent)
                    .disabled(main.editName.trimmingCharacters(in: .whitespaces).isEmpty || main.busy != nil)
            }
        }
        .padding(12)
        .background(.fill.tertiary, in: RoundedRectangle(cornerRadius: 10))
    }
}

private struct ToolsView: View {
    @ObservedObject var main: MainModel
    @ObservedObject var model: RenamerModel

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            PrefGroup(title: "Move renamed clips into a folder") {
                VStack(alignment: .leading, spacing: 8) {
                    Text("\(main.movableCount) renamed clip(s) are at the top of inbox/. Move them (with their notes) into inbox/<folder>/ — logged and undoable.")
                        .font(.caption).foregroundStyle(.secondary)
                    HStack {
                        TextField("Folder name", text: $main.moveFolder).frame(maxWidth: 320)
                        Button("Move…") { main.moveInto() }
                            .disabled(main.moveFolder.trimmingCharacters(in: .whitespaces).isEmpty || main.movableCount == 0 || model.runActive || model.dryRun)
                    }
                }
            }
            PrefGroup(title: "Transcript bundle (exports/)") {
                VStack(alignment: .leading, spacing: 8) {
                    Picker("Scope", selection: $main.scope) {
                        ForEach(main.scopes.indices, id: \.self) { i in
                            Text(main.scopes[i]["label"] as? String ?? "?").tag(main.scopes[i]["value"] as? String ?? "inbox")
                        }
                        if main.scopes.isEmpty { Text("All of inbox/").tag("inbox") }
                    }
                    .frame(maxWidth: 560)
                    HStack {
                        Toggle("Include silent clips", isOn: $main.includeSilent)
                        Toggle("Leave out photos", isOn: $main.excludePhotos)
                        Spacer()
                        Button("Build bundle") { main.buildBundle() }.disabled(main.busy != nil)
                    }
                    if let b = main.bundle {
                        Text((b["message"] as? String) ?? "Bundle written.").font(.caption).foregroundStyle(.secondary)
                        if let n = b["note"] as? String { Text(n).font(.caption).foregroundStyle(.orange) }
                    }
                    if !main.exports.isEmpty {
                        Divider()
                        ForEach(main.exports.prefix(4).indices, id: \.self) { i in
                            let e = main.exports[i]
                            let name = e["name"] as? String ?? "?"
                            HStack {
                                Image(systemName: name.hasSuffix(".md") ? "doc.text" : "curlybraces").foregroundStyle(.secondary)
                                Text(name).font(.caption).lineLimit(1)
                                Spacer()
                                Button("Open") { if let u = main.exportURL(name) { NSWorkspace.shared.open(u) } }.controlSize(.small)
                                Button("Reveal") { if let u = main.exportURL(name) { NSWorkspace.shared.activateFileViewerSelecting([u]) } }.controlSize(.small)
                            }
                        }
                    }
                }
            }
            PrefGroup(title: "More") {
                HStack(spacing: 10) {
                    Button { model.showSort() } label: { Label("Sort into Projects…", systemImage: "folder.badge.gearshape") }
                    Button { model.showInstructions() } label: { Label("Instructions…", systemImage: "text.bubble") }
                    Button { model.showSetup() } label: { Label("Setup & Updates…", systemImage: "wrench.and.screwdriver") }
                    Button { model.showAbout() } label: { Label("About", systemImage: "info.circle") }
                    Spacer()
                    Button { model.openInbox() } label: { Label("Open inbox", systemImage: "folder") }
                }
            }
        }
        .padding(20)
    }
}

extension PreviewData {
    /// Example rows for --render-preview (labelled EXAMPLE DATA in the window; never read from or written to disk).
    static func resultsMock(_ m: MainModel) {
        func row(_ name: String, _ new: String?, _ type: String, _ conf: Double, _ status: String, reasons: [String] = []) -> ResultRow {
            ResultRow(["name": name, "source": "/Volumes/Lexar/AI-Video-Renamer/inbox/\(name)",
                       "current": "/Volumes/Lexar/AI-Video-Renamer/inbox/" + (status == "renamed" ? (new ?? name) : name),
                       "proposed": new as Any, "clip_type": type, "confidence": NSNumber(value: conf), "status": status,
                       "log_id": "example", "batch": "example", "review_reasons": reasons, "time": "2026-10-08T10:0\(Int(conf * 9)):00-10:00"])
        }
        m.exampleData = true
        m.tab = .results
        m.rows = [
            row("CAM_20260101101502_0011_D.MP4", "20261004_retro-game-expo_vendor-tables_broll.mp4", "broll", 0.91, "renamed"),
            row("CAM_20260101102233_0014_D.MP4", "20261004_retro-game-expo_host-intro_talking-head.mp4", "talking-head", 0.88, "renamed"),
            row("CAM_20260101103010_0015_D.MP4", "20261004_retro-game-expo_famicom-box_unboxing.mp4", "unboxing", 0.52, "needs review",
                reasons: ["confidence 0.52 < review_threshold 0.60"]),
            row("IMG_0001.HEIC", "20261004_retro-game-expo_poster_photo.heic", "photo", 0.84, "renamed"),
            row("CAM_20260101110354_0027_D.MP4", nil, "—", 0, "needs review",
                reasons: ["unreadable video: ffprobe could not read it (moov atom not found), 1.2 KB — looks like an empty or interrupted recording"]),
            row("CAM_20260101104541_0019_D.MP4", "20261004_retro-game-expo_crowd-pan_broll.mp4", "broll", 0.79, "dry run"),
        ]
    }
}
