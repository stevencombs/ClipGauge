// ClipGauge main window (v0.8): two views and a toolbar.
//   Queue    — drop folders or clips: each folder becomes a project (name editable, mode in place / copy to inbox,
//              Transcribe switch). One Start / Pause / Stop for the whole queue; reorder, pause, skip, remove.
//              Held or synced folders show one refused row. The queue lives in logs/queue.json and resumes after a
//              stop, quit or sleep (scripts/project_queue.py).
//   Results  — old → new name by project and status; reveal, undo (row, batch or whole project), review editor,
//              transcript view and search across every transcript.
//   Tools ▾  — transcript bundle (project / queue / everything), move renamed clips into a folder, Sort into
//              Projects, Instructions, Ask the Model, open inbox, clear the queue. ⚙︎ — Settings, Setup, About.
// Every action goes through scripts/clipgauge_cli.py (same locks and guards as the engine); nothing here renames.
import AppKit
import SwiftUI
import UniformTypeIdentifiers

enum MainTab: String, CaseIterable { case queue = "Queue", results = "Results" }
enum ResultFilter: String, CaseIterable { case all = "All", review = "Needs review", renamed = "Renamed", other = "Skipped / dry run" }
enum BundleScope: String, CaseIterable { case project = "One project", queue = "Whole queue", everything = "Everything" }

final class MainModel: ObservableObject {
    weak var renamer: RenamerModel?
    @Published var tab: MainTab = .queue
    @Published var lastMessage: String?
    @Published var rows: [ResultRow] = []
    @Published var filter: ResultFilter = .all
    @Published var project = ""                 // "" = all projects
    @Published var loadingResults = false
    @Published var editing: ResultRow?
    @Published var editName = ""
    @Published var busy: String?
    @Published var search = ""
    @Published var hits: [[String: Any]] = []
    @Published var searching = false
    @Published var transcript: [String: Any]?   // sheet
    @Published var showBundle = false
    @Published var bundleScope: BundleScope = .queue
    @Published var bundleProject = ""
    @Published var includeSilent = false
    @Published var bundleMessage: String?
    @Published var showMove = false
    @Published var moveFolder = ""
    @Published var queueDry = false
    @Published var exampleData = false
    @Published var exampleQueue: QueueState? = nil   // previews only (EXAMPLE DATA)

    init(renamer: RenamerModel) { self.renamer = renamer }

    var queue: QueueState { exampleQueue ?? renamer?.queue ?? QueueState() }

    func add(_ urls: [URL]) {
        guard !urls.isEmpty else { return }
        tab = .queue
        renamer?.queueAdd(urls)
    }

    func choose() {
        let p = NSOpenPanel()
        p.canChooseFiles = true
        p.canChooseDirectories = true
        p.allowsMultipleSelection = true
        p.message = "Choose folders (one project each) or clips."
        p.prompt = "Add to Queue"
        NSApp.activate(ignoringOtherApps: true)
        if p.runModal() == .OK { add(p.urls) }
    }

    // MARK: results

    var projectByBatch: [String: String] { queue.projectByBatch }
    func projectName(_ row: ResultRow) -> String? { row.batch.flatMap { projectByBatch[$0] } }
    var projectNames: [String] {
        var seen = Set<String>(), out: [String] = []
        for it in queue.items where !it.batches.isEmpty && !seen.contains(it.name) { seen.insert(it.name); out.append(it.name) }
        return out
    }

    var filtered: [ResultRow] {
        let byProject = project.isEmpty ? rows : rows.filter { projectName($0) == project }
        switch filter {
        case .all: return byProject
        case .review: return byProject.filter { $0.status == "needs review" }
        case .renamed: return byProject.filter { $0.status == "renamed" }
        case .other: return byProject.filter { $0.status != "renamed" && $0.status != "needs review" }
        }
    }
    var reviewCount: Int { rows.filter { $0.status == "needs review" }.count }

    func loadResults() {
        guard let r = renamer, !exampleData else { return }
        loadingResults = true
        r.engine(["results", "--limit", "600"]) { j in
            self.loadingResults = false
            if intVal(j["code"]) == 200 { self.rows = ((j["items"] as? [[String: Any]]) ?? []).map(ResultRow.init) }
            else { self.lastMessage = RenamerModel.errorText(j) }
        }
    }

    func runSearch() {
        let q = search.trimmingCharacters(in: .whitespaces)
        guard let r = renamer, !q.isEmpty, !exampleData else { hits = []; return }
        searching = true
        r.engine(["transcripts-search"] + q.split(separator: " ").map(String.init)) { j in
            self.searching = false
            self.hits = (j["hits"] as? [[String: Any]]) ?? []
            if let e = j["error"] as? String { self.lastMessage = e }
        }
    }

    func showTranscript(key: String) {
        guard let r = renamer else { return }
        r.engine(["transcript", key]) { j in
            if intVal(j["code"]) == 200 { self.transcript = j } else { r.alert("No transcript", RenamerModel.errorText(j)) }
        }
    }

    func reveal(_ row: ResultRow) { revealPath(row.current) }
    func revealPath(_ p: String) {
        let u = URL(fileURLWithPath: p)
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
        if let name = projectName(row), let it = queue.items.first(where: { $0.name == name && $0.batches.contains(b) }) {
            r.undoProject(it) { self.loadResults() }
            return
        }
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
        showMove = false
        r.engine(["move-into", f]) { j in self.finish(j, title: "Move") }
    }

    func buildBundle() {
        guard let r = renamer else { return }
        busy = "Building transcript bundle…"
        bundleMessage = nil
        var o: [String: Any] = ["scope": bundleScope == .project ? "project" : (bundleScope == .queue ? "queue" : "everything")]
        if bundleScope == .project, let it = queue.items.first(where: { $0.name == bundleProject }) { o["id"] = it.id }
        let s = String(decoding: (try? JSONSerialization.data(withJSONObject: o)) ?? Data("{}".utf8), as: UTF8.self)
        r.engine(["queue-bundle", s]) { j in
            self.busy = nil
            if intVal(j["code"]) == 200 { self.bundleMessage = (j["message"] as? String) ?? "Bundle written to exports/." }
            else { r.alert("Couldn't build the bundle", RenamerModel.errorText(j)) }
        }
    }

    func openExports() {
        guard let root = renamer?.root else { return }
        let u = root.appendingPathComponent("exports")
        NSWorkspace.shared.open(fileExists(u) ? u : root)
    }
}

// MARK: - window

struct MainView: View {
    @ObservedObject var main: MainModel
    @ObservedObject var model: RenamerModel
    var scrolls = true
    @ViewState private var dropTarget = false

    var body: some View {
        VStack(spacing: 0) {
            Toolbar(main: main, model: model)
            Divider()
            Group {
                switch main.tab {
                case .queue: QueueView(main: main, model: model, scrolls: scrolls)
                case .results: ResultsView(main: main, model: model, scrolls: scrolls)
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
        .frame(minWidth: 860, minHeight: scrolls ? 560 : nil)
        .overlay(dropTarget ? RoundedRectangle(cornerRadius: 12).stroke(Color.accentColor, lineWidth: 3).padding(4) : nil)
        .onDrop(of: [UTType.fileURL], isTargeted: $dropTarget) { providers in
            loadURLs(providers) { main.add($0) }
            return true
        }
        .onAppear { if main.tab == .results { main.loadResults() } }
        .onChange(of: main.tab) { t in if t == .results { main.loadResults() } }
        .sheet(isPresented: $main.showBundle) { BundleSheet(main: main) }
        .sheet(isPresented: $main.showMove) { MoveSheet(main: main, model: model) }
        .sheet(isPresented: Binding(get: { main.transcript != nil }, set: { if !$0 { main.transcript = nil } })) {
            if let t = main.transcript { TranscriptSheet(main: main, t: t) }
        }
    }
}

/// Queue | Results, a status pill, Tools ▾ and ⚙︎ (GrokGauge-style header).
private struct Toolbar: View {
    @ObservedObject var main: MainModel
    @ObservedObject var model: RenamerModel

    var body: some View {
        HStack(spacing: 12) {
            Picker("", selection: $main.tab) {
                Text("Queue").tag(MainTab.queue)
                Text(main.reviewCount > 0 ? "Results (\(main.reviewCount) to review)" : "Results").tag(MainTab.results)
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .frame(maxWidth: 300)
            if main.exampleData {
                Text("EXAMPLE DATA").font(.caption2.weight(.bold)).padding(.horizontal, 6).padding(.vertical, 2)
                    .background(Color.orange.opacity(0.25), in: Capsule())
            }
            Spacer()
            statusPill
            Menu {
                Button("Transcript Bundle…") { main.showBundle = true }
                Button("Move Renamed Clips into a Folder…") { main.showMove = true }
                    .disabled(main.movableCount == 0 || model.runActive || model.dryRun)
                Button("Sort into Projects…") { model.showSort() }
                Button("Instructions…") { model.showInstructions() }
                Button("Ask the Model…") { model.showChat(nil) }.keyboardShortcut("k")
                Divider()
                Button("Open Inbox") { model.openInbox() }
                Button("Open Exports") { main.openExports() }
                Divider()
                Button("Clear Finished Projects") { model.queueClear(all: false) }
                Button("Clear Queue…") { model.queueClear(all: true) }.disabled(main.queue.running)
            } label: { Label("Tools", systemImage: "wrench.and.screwdriver") }
            .fixedSize()
            Menu {
                Button("Settings…") { model.showSettings() }
                Button("Setup & Updates…") { model.showSetup() }
                Button("About \(kAppName)") { model.showAbout() }
            } label: { Image(systemName: "gearshape") }
            .menuIndicator(.hidden)
            .fixedSize()
            .help("Settings, Setup and About")
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 10)
    }

    private var statusPill: some View {
        let (t, c): (String, Color) = {
            if !model.lexarOK { return ("Drive not connected", .red) }
            if model.resolveRunning { return ("Resolve is open", .orange) }
            if main.queue.running { return (main.queue.state == "pausing" ? "Pausing…" : "Running", .blue) }
            if model.runActive { return ("Busy", .blue) }
            return ("Ready · " + (model.dryRun ? "dry run" : "live"), .green)
        }()
        return Label(t, systemImage: "circle.fill")
            .labelStyle(PillLabel())
            .font(.caption.weight(.semibold)).foregroundStyle(c)
            .padding(.horizontal, 9).padding(.vertical, 3)
            .background(c.opacity(0.14), in: Capsule())
    }
}

private struct PillLabel: LabelStyle {
    func makeBody(configuration: Configuration) -> some View {
        HStack(spacing: 5) { configuration.icon.font(.system(size: 6)); configuration.title }
    }
}

// MARK: - Queue

private struct QueueView: View {
    @ObservedObject var main: MainModel
    @ObservedObject var model: RenamerModel
    var scrolls: Bool

    var body: some View {
        let q = main.queue
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 10) {
                DropZone(main: main, compact: !q.items.isEmpty)
                if q.items.isEmpty {
                    Text("Each folder becomes a project (its name is editable). In place renames clips where they are; Copy to inbox copies them first (originals untouched). Held, Resolve-internal and Cloud-synced folders are refused.")
                        .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(.horizontal, 20).padding(.top, 14).padding(.bottom, 10)
            if !q.items.isEmpty {
                if scrolls {
                    ScrollView { list(q) }.frame(maxHeight: .infinity)
                } else {
                    list(q)
                }
            } else {
                Spacer(minLength: 0)
            }
            Divider()
            footer(q)
        }
    }

    private func list(_ q: QueueState) -> some View {
        LazyVStack(spacing: 7) {
            ForEach(Array(q.items.enumerated()), id: \.element.id) { i, it in
                ProjectRow(main: main, model: model, item: it, position: i, count: q.items.count, queueRunning: q.running)
            }
        }
        .padding(.horizontal, 20).padding(.bottom, 12)
    }

    @ViewBuilder private func footer(_ q: QueueState) -> some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text(footerLine(q)).font(.caption).foregroundStyle(.secondary).lineLimit(2)
                if !q.message.isEmpty && !q.running { Text(q.message).font(.caption2).foregroundStyle(.tertiary).lineLimit(1) }
            }
            Spacer()
            if q.running {
                Button { model.queuePause() } label: { Label(q.state == "pausing" ? "Pausing…" : "Pause after this clip", systemImage: "pause.fill") }
                    .disabled(q.state == "pausing")
                Button { model.queueStop() } label: { Label("Stop", systemImage: "stop.fill") }
            } else {
                Toggle("Dry run", isOn: $main.queueDry).toggleStyle(.checkbox)
                    .disabled(model.dryRun)
                    .help(model.dryRun ? "config dry_run is on — every run is a dry run" : "Process without renaming anything")
                Toggle("Build transcript bundle when done", isOn: Binding(
                    get: { q.autoBundle }, set: { v in model.queueOptions(["auto_bundle": v]) }))
                    .toggleStyle(.checkbox)
                Button {
                    model.queueStart(dryRun: main.queueDry, autoBundle: q.autoBundle)
                } label: {
                    Label(q.canResume ? "Resume queue" : "Start queue", systemImage: "play.fill").frame(minWidth: 110)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                .keyboardShortcut(.defaultAction)
                .disabled(q.left.isEmpty || model.startBlocker != nil || model.actionBusy != nil)
                .help(q.left.isEmpty ? "Add folders or clips first" : (model.startBlocker ?? "Run every waiting project in order"))
            }
        }
        .padding(.horizontal, 20).padding(.vertical, 12)
    }

    private func footerLine(_ q: QueueState) -> String {
        if !model.lexarOK { return Project.missingTitle }
        if model.resolveRunning { return "DaVinci Resolve is open — the queue can't start until it quits." }
        if q.items.isEmpty { return "Nothing queued yet." }
        if q.running, let c = q.current {
            return "Project \((q.items.firstIndex(of: c) ?? 0) + 1) of \(q.items.count) · \(q.summaryLine) · resumes after a stop, quit or sleep"
        }
        var s = "\(q.left.count) project(s) · \(q.clipsLeft) clip(s)"
        let est = queueEstimate(clips: q.clipsLeft)
        if !est.isEmpty { s += " · \(est)" }
        s += (main.queueDry || model.dryRun) ? " · Dry run" : " · Live (renames files)"
        let inResolve = q.left.filter { $0.mode == "in_place" && $0.needsConfirm }.count
        if inResolve > 0 && !(main.queueDry || model.dryRun) { s += " · \(inResolve) in the DaVinci Resolve folder: you'll confirm once" }
        if let b = model.startBlocker, !q.left.isEmpty { s += " — " + b }
        return s
    }
}

private struct DropZone: View {
    @ObservedObject var main: MainModel
    var compact: Bool
    var body: some View {
        Group {
            if compact {
                HStack(spacing: 8) {
                    Image(systemName: "plus.rectangle.on.folder").foregroundStyle(.secondary)
                    Text("Drop more folders or clips — they join the end of the queue").foregroundStyle(.secondary)
                    Button("Choose…") { main.choose() }.controlSize(.small)
                }
                .font(.callout)
                .frame(maxWidth: .infinity, minHeight: 40)
            } else {
                VStack(spacing: 10) {
                    Image(systemName: "square.and.arrow.down.on.square").font(.system(size: 34)).foregroundStyle(.secondary)
                    Text("Drop folders or clips here").font(.title3.weight(.semibold))
                    Text("…or on the \(kAppName) menu bar icon. Each folder becomes a project.")
                        .font(.caption).foregroundStyle(.secondary).multilineTextAlignment(.center)
                    Button("Choose…") { main.choose() }.controlSize(.large)
                }
                .frame(maxWidth: .infinity, minHeight: 170)
            }
        }
        .background(RoundedRectangle(cornerRadius: 12).strokeBorder(style: StrokeStyle(lineWidth: 1.5, dash: [6, 5])).foregroundStyle(.tertiary))
    }
}

private struct ProjectRow: View {
    @ObservedObject var main: MainModel
    @ObservedObject var model: RenamerModel
    let item: QueueItem
    let position: Int
    let count: Int
    let queueRunning: Bool
    @ViewState private var name = ""
    @ViewState private var editingName = false

    var body: some View {
        HStack(spacing: 10) {
            statusIcon.frame(width: 22)
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    if editingName {
                        TextField("Project name", text: $name, onCommit: commitName)
                            .textFieldStyle(.roundedBorder).frame(maxWidth: 280)
                    } else {
                        Text(item.name).font(.callout.weight(.semibold)).lineLimit(1)
                        if editable {
                            Button { name = item.name; editingName = true } label: { Image(systemName: "pencil") }
                                .buttonStyle(.borderless).foregroundStyle(.secondary).help("Rename the project")
                        }
                    }
                }
                Text(subline).font(.caption).foregroundStyle(item.isRefused || item.status == "failed" ? Color.red : Color.secondary)
                    .lineLimit(2).fixedSize(horizontal: false, vertical: true)
                if item.isRunning {
                    ProgressView(value: item.fraction).progressViewStyle(.linear).frame(maxWidth: 360)
                }
            }
            Spacer(minLength: 8)
            if !item.isRefused {
                Picker("", selection: Binding(get: { item.mode }, set: { model.queueUpdate(item.id, ["mode": $0]) })) {
                    Text("In place").tag("in_place")
                    Text("Copy to inbox").tag("copy")
                }
                .labelsHidden().frame(width: 130).disabled(!editable)
                .help(item.mode == "copy" ? "Copied into inbox/\(item.name)/ first; originals untouched" : "Renamed where they are")
                Toggle("Transcribe", isOn: Binding(get: { item.transcribe }, set: { model.queueUpdate(item.id, ["transcribe": $0]) }))
                    .toggleStyle(.switch).controlSize(.mini).disabled(!editable)
                    .help("Speech to text (whisper) for this project's clips")
            } else {
                Text("Refused").font(.caption.weight(.semibold)).foregroundStyle(.red)
                    .padding(.horizontal, 7).padding(.vertical, 2).background(Color.red.opacity(0.14), in: Capsule())
            }
            Menu { menu } label: { Image(systemName: "ellipsis.circle") }
                .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
        }
        .padding(.horizontal, 12).padding(.vertical, 9)
        .background(RoundedRectangle(cornerRadius: 10).fill(item.isRefused ? Color.red.opacity(0.07) : Color.primary.opacity(0.04)))
        .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(item.isRunning ? Color.accentColor : Color.primary.opacity(0.08),
                                                                lineWidth: item.isRunning ? 1.5 : 0.5))
        .opacity(item.status == "paused" || item.status == "skipped" ? 0.6 : 1)
    }

    private var editable: Bool { !item.isRunning && !item.isFinished && !main.exampleData }

    private func commitName() {
        editingName = false
        let n = name.trimmingCharacters(in: .whitespaces)
        if !n.isEmpty && n != item.name { model.queueUpdate(item.id, ["name": n]) }
    }

    @ViewBuilder private var menu: some View {
        if item.status == "paused" { Button("Resume this project") { model.queueUpdate(item.id, ["paused": false]) } }
        else if ["waiting", "stopped"].contains(item.status) { Button("Pause this project") { model.queueUpdate(item.id, ["paused": true]) } }
        if !item.isFinished { Button(item.isRunning ? "Skip (stop this project, go on)" : "Skip") { model.queueSkip(item.id) } }
        if !item.isRefused {
            Toggle("Use the name in filenames", isOn: Binding(get: { item.nameInFilenames },
                                                               set: { model.queueUpdate(item.id, ["name_in_filenames": $0]) }))
                .disabled(!editable)
        }
        Divider()
        Button("Move Up") { model.queueMove(item.id, to: position - 1) }.disabled(position == 0)
        Button("Move Down") { model.queueMove(item.id, to: position + 1) }.disabled(position >= count - 1)
        Divider()
        Button("Show in Finder") { model.revealProject(item) }
        if !item.batches.isEmpty {
            Button("Show Results") { main.project = item.name; main.tab = .results }
            Button("Transcript Bundle for this Project…") { main.bundleScope = .project; main.bundleProject = item.name; main.showBundle = true }
            Button("Undo this Project…") { model.undoProject(item) }
        }
        Divider()
        Button("Remove from Queue") { model.queueRemove(item.id) }.disabled(item.isRunning)
    }

    @ViewBuilder private var statusIcon: some View {
        switch item.status {
        case "running": ProgressView().controlSize(.small)
        case "done": Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
        case "done_with_errors": Image(systemName: "checkmark.circle.trianglebadge.exclamationmark").foregroundStyle(.orange)
        case "refused": Image(systemName: "hand.raised.fill").foregroundStyle(.red)
        case "failed": Image(systemName: "xmark.octagon.fill").foregroundStyle(.red)
        case "paused": Image(systemName: "pause.circle").foregroundStyle(.orange)
        case "skipped": Image(systemName: "forward.end.circle").foregroundStyle(.secondary)
        case "stopped": Image(systemName: "stop.circle").foregroundStyle(.orange)
        default: Image(systemName: item.paths.count == 1 && FileManager.default.isDirectory(URL(fileURLWithPath: item.paths[0])) ? "folder.fill" : "film.stack")
            .foregroundStyle(.secondary)
        }
    }

    private var subline: String {
        if item.isRefused { return item.reason }
        if item.isRunning {
            let n = max(item.total, item.clips)
            var s = item.step == "copying" ? "Copying \(item.index) of \(n)" : "Clip \(max(1, item.index)) of \(n)"
            if let c = item.current { s += " · \(c)" }
            if let st = item.step, st != "copying" { s += " · \(st)" }
            if let e = item.etaSeconds, e > 0 { s += " · \(e / 60) min left" }
            return s
        }
        if item.isFinished || item.status == "stopped" { return item.message.isEmpty ? item.status : item.message }
        var s = "\(item.clips) clip(s)"
        if !item.location.isEmpty { s += " · \(item.location)" }
        if item.mode == "copy" { s += " · into inbox/\(item.name)/" }
        if item.needsConfirm && item.mode == "in_place" { s += " · DaVinci Resolve folder (asked once)" }
        if !item.transcribe { s += " · no transcription" }
        if item.nameInFilenames { s += " · name in filenames" }
        if !item.reason.isEmpty { s += " · " + item.reason }
        return s
    }
}

// MARK: - Results

private struct ResultsView: View {
    @ObservedObject var main: MainModel
    @ObservedObject var model: RenamerModel
    var scrolls: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 10) {
                Picker("Project", selection: $main.project) {
                    Text("All projects").tag("")
                    ForEach(main.projectNames, id: \.self) { Text($0).tag($0) }
                }
                .frame(maxWidth: 240)
                Picker("Show", selection: $main.filter) {
                    ForEach(ResultFilter.allCases, id: \.self) { f in
                        Text(f == .review ? "Needs review (\(main.reviewCount))" : f.rawValue).tag(f)
                    }
                }
                .frame(maxWidth: 230)
                Spacer()
                TextField("Search transcripts", text: $main.search, onCommit: { main.runSearch() })
                    .textFieldStyle(.roundedBorder).frame(maxWidth: 220)
                if !main.search.isEmpty {
                    Button { main.search = ""; main.hits = [] } label: { Image(systemName: "xmark.circle.fill") }.buttonStyle(.borderless)
                }
                if main.loadingResults || main.busy != nil || main.searching { ProgressView().controlSize(.small) }
                Button { main.loadResults() } label: { Image(systemName: "arrow.clockwise") }.help("Refresh")
            }
            if let b = main.busy { Text(b).font(.caption).foregroundStyle(.secondary) }
            if !main.search.isEmpty && (!main.hits.isEmpty || !main.searching) {
                hitsList
            } else {
                header
                if scrolls { ScrollView { rowsList } } else { rowsList }
            }
            if let e = main.editing { ReviewEditor(main: main, row: e) }
            if !main.project.isEmpty, let it = main.queue.items.first(where: { $0.name == main.project }) {
                HStack {
                    Text("Project “\(it.name)” · every rename is in logs/rename-log.jsonl").font(.caption).foregroundStyle(.secondary)
                    Spacer()
                    Button("Transcript bundle…") { main.bundleScope = .project; main.bundleProject = it.name; main.showBundle = true }
                    Button("Undo project…") { model.undoProject(it) { main.loadResults() } }.disabled(it.batches.isEmpty)
                }
            }
        }
        .padding(20)
    }

    private var header: some View {
        HStack(spacing: 8) {
            Text("Project").frame(width: 130, alignment: .leading)
            Text("Original").frame(width: 190, alignment: .leading)
            Text("New name").frame(maxWidth: .infinity, alignment: .leading)
            Text("Conf.").frame(width: 44, alignment: .trailing)
            Text("Status").frame(width: 100, alignment: .leading)
            Text("").frame(width: 110)
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

    private var hitsList: some View {
        let content = LazyVStack(alignment: .leading, spacing: 0) {
            Text("\(main.hits.count) match(es) for “\(main.search)” across every transcript")
                .font(.caption).foregroundStyle(.secondary).padding(10)
            ForEach(main.hits.indices, id: \.self) { i in
                let h = main.hits[i]
                HStack(alignment: .top, spacing: 10) {
                    Text(timeString(h["start"])).font(.caption.monospacedDigit()).foregroundStyle(.secondary).frame(width: 60, alignment: .leading)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(h["file"] as? String ?? "?").font(.callout.weight(.medium)).lineLimit(1).truncationMode(.middle)
                        Text(h["text"] as? String ?? "").font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer()
                    Button("Transcript") { if let k = (h["clip_id"] as? String) ?? (h["path"] as? String) { main.showTranscript(key: k) } }
                        .controlSize(.small)
                    Button("Show") { main.revealPath(h["path"] as? String ?? "") }.controlSize(.small)
                }
                .padding(.horizontal, 10).padding(.vertical, 7)
                Divider()
            }
        }
        .background(.fill.quinary, in: RoundedRectangle(cornerRadius: 10))
        return Group { if scrolls { ScrollView { content } } else { content } }
    }
}

func timeString(_ v: Any?) -> String {
    let s = Int((v as? NSNumber)?.doubleValue ?? 0)
    return s >= 3600 ? String(format: "%d:%02d:%02d", s / 3600, (s % 3600) / 60, s % 60) : String(format: "%d:%02d", s / 60, s % 60)
}

private struct TranscriptSheet: View {
    @ObservedObject var main: MainModel
    let t: [String: Any]
    var body: some View {
        let segs = (t["segments"] as? [[String: Any]]) ?? []
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text(t["file"] as? String ?? "Transcript").font(.headline).lineLimit(1).truncationMode(.middle)
                Spacer()
                Button("Copy") {
                    let text = segs.map { "[\(timeString($0["start"]))] \($0["text"] as? String ?? "")" }.joined(separator: "\n")
                    NSPasteboard.general.clearContents(); NSPasteboard.general.setString(text, forType: .string)
                }
                Button("Show in Finder") { main.revealPath(t["path"] as? String ?? "") }
            }
            if segs.isEmpty {
                Text("No speech in this clip (or it wasn't transcribed).").foregroundStyle(.secondary).padding(.vertical, 20)
            } else {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 6) {
                        ForEach(segs.indices, id: \.self) { i in
                            HStack(alignment: .top, spacing: 10) {
                                Text(timeString(segs[i]["start"])).font(.caption.monospacedDigit()).foregroundStyle(.secondary).frame(width: 60, alignment: .leading)
                                Text(segs[i]["text"] as? String ?? "").textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    }
                }
            }
            HStack { Spacer(); Button("Done") { main.transcript = nil }.keyboardShortcut(.defaultAction) }
        }
        .padding(20)
        .frame(width: 620, height: 480)
    }
}

private struct BundleSheet: View {
    @ObservedObject var main: MainModel
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Transcript Bundle").font(.headline)
            Text("One JSON + Markdown file pair in exports/ with every transcript in the scope — ready to paste into a script-writing chat. Read-only for your clips.")
                .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            Picker("Scope", selection: $main.bundleScope) {
                ForEach(BundleScope.allCases, id: \.self) { Text($0.rawValue).tag($0) }
            }
            .pickerStyle(.segmented)
            if main.bundleScope == .project {
                Picker("Project", selection: $main.bundleProject) {
                    ForEach(main.projectNames, id: \.self) { Text($0).tag($0) }
                }
            }
            Toggle("Include clips with no speech", isOn: $main.includeSilent)
            if let m = main.bundleMessage { Text(m).font(.caption).foregroundStyle(.secondary) }
            HStack {
                Button("Open Exports") { main.openExports() }
                Spacer()
                Button("Close") { main.showBundle = false }
                Button("Build") { main.buildBundle() }.buttonStyle(.borderedProminent)
                    .disabled(main.busy != nil || (main.bundleScope == .project && main.bundleProject.isEmpty))
            }
        }
        .padding(20)
        .frame(width: 520)
    }
}

private struct MoveSheet: View {
    @ObservedObject var main: MainModel
    @ObservedObject var model: RenamerModel
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Move renamed clips into a folder").font(.headline)
            Text("\(main.movableCount) renamed clip(s) are at the top of inbox/. Move them (with their notes) into inbox/<folder>/ — logged and undoable.")
                .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            TextField("Folder name", text: $main.moveFolder)
            HStack {
                Spacer()
                Button("Cancel") { main.showMove = false }
                Button("Move…") { main.moveInto() }.buttonStyle(.borderedProminent)
                    .disabled(main.moveFolder.trimmingCharacters(in: .whitespaces).isEmpty || main.movableCount == 0 || model.runActive || model.dryRun)
            }
        }
        .padding(20)
        .frame(width: 460)
    }
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
    var noteID: String? { d["note_id"] as? String }
    var reasons: [String] { d["review_reasons"] as? [String] ?? [] }
    var newName: String {
        if status == "renamed" { return (current as NSString).lastPathComponent }
        return proposed ?? "—"
    }
    var time: Date? { (d["time"] as? String).flatMap { isoParser.date(from: $0) } }
    var inResolve: Bool { current.contains("/DaVinci Resolve/") || current.contains("/DaVinci Resolve Media/") }
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


private struct ResultLine: View {
    @ObservedObject var main: MainModel
    let row: ResultRow
    @ViewState private var hover = false

    var body: some View {
        HStack(spacing: 8) {
            Text(main.projectName(row) ?? row.d["folder"] as? String ?? "—").foregroundStyle(.secondary).lineLimit(1)
                .truncationMode(.tail).frame(width: 130, alignment: .leading)
            Text(row.name).lineLimit(1).truncationMode(.middle).frame(width: 190, alignment: .leading)
            VStack(alignment: .leading, spacing: 1) {
                Text(row.newName).lineLimit(1).truncationMode(.middle)
                    .foregroundStyle(row.status == "renamed" ? Color.primary : Color.secondary)
                if row.status == "needs review", let r = row.reasons.first {
                    Text(r).font(.caption2).foregroundStyle(.orange).lineLimit(1).truncationMode(.tail)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
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
                    if row.batch != nil { Button(main.projectName(row) != nil ? "Undo whole project…" : "Undo whole batch…") { main.undoBatch(row) } }
                    if let n = row.noteID { Button("Transcript…") { main.showTranscript(key: n) } }
                    if row.status != "needs review", row.proposed != nil, row.status != "renamed" {
                        Button("Rename to proposed name…") { main.startEdit(row) }
                    }
                } label: { Image(systemName: "ellipsis.circle") }
                .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
            }
            .frame(width: 110, alignment: .trailing)
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

    /// Example queue for --render-preview (EXAMPLE DATA; never read from or written to disk).
    static func queueMock(running: Bool) -> QueueState {
        func it(_ id: String, _ name: String, _ path: String, _ mode: String, _ status: String, clips: Int,
                extra: [String: Any] = [:]) -> QueueItem {
            QueueItem(["id": id, "name": name, "paths": [path], "mode": mode, "status": status, "media_count": clips,
                       "copy_count": clips, "transcribe": true].merging(extra) { $1 })
        }
        var q = QueueState()
        q.state = running ? "running" : "idle"
        q.alive = running
        q.items = [
            it("a1", "Collectibles Review", "/Volumes/Lexar/DaVinci Resolve/Videos/Collectibles Review", "in_place",
               running ? "done" : "waiting", clips: 18, extra: ["needs_confirm": true, "batches": ["example-a1"],
               "message": "Done (live): 18 renamed where they are, 0 need review — of 18 clip(s)."]),
            it("b2", "Video Project", "/Volumes/Lexar/DaVinci Resolve/Videos/Video Project", "in_place",
               running ? "running" : "waiting", clips: 52, extra: ["needs_confirm": true,
               "progress": ["index": 21, "total": 52, "current": "CAM_20261010090412_0034_D.MP4", "step": "describe", "eta_seconds": 5520]]),
            it("c3", "Retro Console Unboxing", "/Users/you/Desktop/Retro Console Unboxing", "copy", "waiting", clips: 7),
            it("d4", "Vintage Show", "/Volumes/Lexar/DaVinci Resolve/Vintage Show", "in_place", running ? "paused" : "waiting",
               clips: 12, extra: ["needs_confirm": true, "transcribe": false]),
            it("e5", "Held Project", "/Volumes/Lexar/DaVinci Resolve/Held Project", "in_place", "refused", clips: 0,
               extra: ["reason": "“Held Project” is on hold (hold_sources) — remove it from hold in Settings or config to process"]),
        ]
        return q
    }
}
