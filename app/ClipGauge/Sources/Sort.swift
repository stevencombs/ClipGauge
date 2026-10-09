// Sort into Projects (v0.4): front end for scripts/sort_projects.py. Dry run first (read-only, plan saved in
// logs/sort-plans/), edit names / include / merge / split per group, then Apply (confirm; refused while DaVinci Resolve
// is open or a run/update holds a lock). Every apply writes an undo map (logs/sort-undo-*.jsonl); "Undo Last Sort…" reverses it.
import AppKit
import SwiftUI

struct SortSource: Identifiable, Hashable {
    let id: String          // value passed to --source ("inbox" or an absolute path)
    let name: String
    let kind: String        // inbox | project | raw | held | synced | resolve | custom
    let note: String
    let sortable: Bool

    init(_ d: [String: Any]) {
        kind = d["kind"] as? String ?? "raw"
        name = d["name"] as? String ?? "?"
        id = kind == "inbox" ? "inbox" : (d["path"] as? String ?? name)
        note = d["note"] as? String ?? ""
        sortable = d["sortable"] as? Bool ?? false
    }
    init(custom url: URL) {
        id = url.path; name = url.lastPathComponent; kind = "custom"; note = "chosen folder"; sortable = true
    }
    var symbol: String {
        switch kind {
        case "inbox": return "tray"
        case "project": return "film.stack"
        case "held": return "hand.raised"
        case "synced": return "icloud"
        case "resolve": return "lock"
        default: return "folder"
        }
    }
}

struct SortItem: Identifiable {
    let id: String
    let name: String
    let bucket: String
    let action: String      // move | keep
    let reason: String
    let kind: String
    init(_ d: [String: Any]) {
        id = d["src"] as? String ?? UUID().uuidString
        name = d["name"] as? String ?? "?"
        bucket = d["bucket"] as? String ?? "?"
        action = d["action"] as? String ?? "move"
        reason = d["reason"] as? String ?? ""
        kind = d["kind"] as? String ?? "video"
    }
}

struct SortGroup: Identifiable {
    let id: String
    let project: String
    let nameSource: String
    let existing: Bool
    let mergeInto: String?
    let clipCount: Int
    let photoCount: Int
    let start: String?
    let end: String?
    let description: String
    let dest: String
    let flags: [String]
    let buckets: [(String, Int)]
    let items: [SortItem]
    let splitParts: [(String, Int)]

    init(_ d: [String: Any]) {
        id = d["id"] as? String ?? UUID().uuidString
        project = d["project"] as? String ?? "?"
        nameSource = d["name_source"] as? String ?? ""
        existing = d["existing"] as? Bool ?? false
        mergeInto = d["merge_into"] as? String
        clipCount = intVal(d["clip_count"]) ?? 0
        photoCount = intVal(d["photo_count"]) ?? 0
        start = d["time_start"] as? String
        end = d["time_end"] as? String
        description = d["description"] as? String ?? ""
        dest = d["dest"] as? String ?? ""
        flags = d["flags"] as? [String] ?? []
        // bucket_counts keeps the engine's order (A-Roll, B-Roll, Images, _Notes, _Review)
        let items = (d["items"] as? [[String: Any]] ?? []).map(SortItem.init)
        self.items = items
        var order: [String] = []
        for i in items where !order.contains(i.bucket) { order.append(i.bucket) }
        let bc = d["bucket_counts"] as? [String: Any] ?? [:]
        let pref = ["A-Roll", "B-Roll", "Images", "_Notes", "_Review"]
        let keys = Array(bc.keys).sorted { (pref.firstIndex(of: $0) ?? 99, $0) < (pref.firstIndex(of: $1) ?? 99, $1) }
        buckets = keys.map { ($0, intVal(bc[$0]) ?? 0) }
        let parts = ((d["split_suggestion"] as? [String: Any])?["parts"] as? [[String: Any]]) ?? []
        splitParts = parts.map { ($0["project"] as? String ?? "?", ($0["names"] as? [Any])?.count ?? 0) }
    }

    var moves: Int { items.filter { $0.action == "move" }.count }
    var timeRange: String {
        guard let s = start else { return "time unknown" }
        let day = String(s.prefix(10)), t0 = String(s.dropFirst(11).prefix(5)), t1 = String((end ?? s).dropFirst(11).prefix(5))
        let d1 = String((end ?? s).prefix(10))
        return d1 == day ? "\(day) \(t0)–\(t1)" : "\(day) \(t0) – \(d1) \(t1)"
    }
}

struct SortPlan {
    let id: String
    let file: String
    let source: String
    let sourceKind: String
    let warnings: [String]
    let groups: [SortGroup]
    let existingProjects: [String]
    let moves: Int
    let keeps: Int

    init(_ d: [String: Any]) {
        id = d["plan_id"] as? String ?? "?"
        file = d["plan_file"] as? String ?? ""
        source = d["source"] as? String ?? ""
        sourceKind = d["source_kind"] as? String ?? ""
        warnings = d["warnings"] as? [String] ?? []
        groups = (d["groups"] as? [[String: Any]] ?? []).map(SortGroup.init)
        existingProjects = d["existing_projects"] as? [String] ?? []
        let t = d["totals"] as? [String: Any] ?? [:]
        moves = intVal(t["moves"]) ?? groups.reduce(0) { $0 + $1.moves }
        keeps = intVal(t["keeps"]) ?? 0
    }
}

struct GroupEdit {
    var include = true
    var name: String
    var mergeInto: String       // "" = new project / its own name
    var split = false
}

final class SortModel: ObservableObject {
    @Published var sources: [SortSource] = []
    @Published var resolveRoot: String = ""
    @Published var selected: String = "inbox"
    @Published var gapMinutes = 60
    @Published var plan: SortPlan?
    @Published var edits: [String: GroupEdit] = [:]
    @Published var planning = false
    @Published var working = false
    @Published var error: String?
    @Published var result: String?
    @Published var lastSort: (id: String, time: String, moved: Int, undone: Bool)?

    weak var renamer: RenamerModel?
    var root: URL? { renamer?.root }

    init(renamer: RenamerModel?) { self.renamer = renamer }

    var sortable: [SortSource] { sources.filter { $0.sortable } }
    var untouchable: [SortSource] { sources.filter { !$0.sortable && $0.kind != "resolve" } }

    /// Why Apply is disabled right now (nil = allowed). The script re-checks all of this itself.
    var applyBlockedReason: String? {
        guard let r = renamer else { return nil }
        if !r.lexarOK { return "The Lexar isn't connected." }
        if r.resolveRunning { return "DaVinci Resolve is open — quit it to apply (moving clips under an open project makes them offline)." }
        if r.runActive { return "A processing run (or another sort) is active — apply when it finishes." }
        if r.updates.jobActive { return "Models/tools are being updated — apply when that finishes." }
        return nil
    }

    var includedGroups: [SortGroup] { plan?.groups.filter { edits[$0.id]?.include ?? true } ?? [] }

    private func runPython(_ args: [String], done: @escaping (Int32, [String: Any], String) -> Void) {
        guard let py = Project.python(), let r = root else { done(127, [:], "python3 not found or no project folder"); return }
        let script = r.appendingPathComponent("scripts/sort_projects.py")
        guard fileExists(script) else {
            done(127, [:], "scripts/sort_projects.py is missing — update the project's scripts (ClipGauge v0.4 bundles it).")
            return
        }
        DispatchQueue.global(qos: .userInitiated).async {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: py)
            p.arguments = [script.path] + args + ["--json"]
            p.currentDirectoryURL = r
            var env = ProcessInfo.processInfo.environment
            env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
            env["AI_VIDEO_RENAMER_LAUNCHED_BY"] = "clipgauge-sort"
            p.environment = env
            let out = Pipe(), err = Pipe()
            p.standardOutput = out
            p.standardError = err
            p.standardInput = FileHandle.nullDevice
            do { try p.run() } catch {
                DispatchQueue.main.async { done(126, [:], error.localizedDescription) }
                return
            }
            let o = out.fileHandleForReading.readDataToEndOfFile()
            let e = err.fileHandleForReading.readDataToEndOfFile()
            p.waitUntilExit()
            let last = String(decoding: o, as: UTF8.self).split(separator: "\n").last.map(String.init) ?? ""
            let obj = (try? JSONSerialization.jsonObject(with: Data(last.utf8))) as? [String: Any] ?? [:]
            let se = String(decoding: e, as: UTF8.self)
            DispatchQueue.main.async { done(p.terminationStatus, obj, se) }
        }
    }

    private func fail(_ code: Int32, _ o: [String: Any], _ err: String) -> String {
        (o["error"] as? String) ?? "sort_projects.py exit \(code): " + String(err.split(separator: "\n").suffix(3).joined(separator: " ").prefix(400))
    }

    func reload() {
        runPython(["--list-sources"]) { code, o, err in
            guard code == 0 else { self.error = self.fail(code, o, err); return }
            self.sources = (o["sources"] as? [[String: Any]] ?? []).map(SortSource.init)
            self.resolveRoot = o["resolve_root"] as? String ?? ""
            if !self.sources.contains(where: { $0.id == self.selected && $0.sortable }) && !self.selected.hasPrefix("/") {
                self.selected = "inbox"
            }
        }
        runPython(["--history"]) { code, o, _ in
            guard code == 0, let h = (o["history"] as? [[String: Any]])?.first else { self.lastSort = nil; return }
            self.lastSort = (h["plan_id"] as? String ?? "?", h["time"] as? String ?? "", intVal(h["moved"]) ?? 0, h["undone"] as? Bool ?? false)
        }
    }

    func chooseFolder() {
        let panel = NSOpenPanel()
        panel.title = "Choose a folder of clips to sort"
        panel.message = "Nothing moves now — ClipGauge makes a dry run first."
        panel.prompt = "Choose"
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        if !resolveRoot.isEmpty { panel.directoryURL = URL(fileURLWithPath: resolveRoot) }
        guard panel.runModal() == .OK, let u = panel.url else { return }
        if !sources.contains(where: { $0.id == u.path }) { sources.append(SortSource(custom: u)) }
        selected = u.path
        plan = nil
    }

    func dryRun() {
        guard !planning else { return }
        planning = true
        error = nil
        result = nil
        runPython(["--source", selected, "--dry-run", "--gap-minutes", String(gapMinutes)]) { code, o, err in
            self.planning = false
            guard code == 0, o["plan_id"] != nil else { self.plan = nil; self.error = self.fail(code, o, err); return }
            self.load(plan: SortPlan(o))
        }
    }

    func load(plan p: SortPlan) {
        plan = p
        var e: [String: GroupEdit] = [:]
        for g in p.groups { e[g.id] = GroupEdit(name: g.project, mergeInto: g.mergeInto ?? "") }
        edits = e
    }

    func binding(_ id: String) -> Binding<GroupEdit> {
        Binding(get: { self.edits[id] ?? GroupEdit(name: "", mergeInto: "") }, set: { self.edits[id] = $0 })
    }

    /// The edits document passed to --edits (see sort_projects.apply_edits).
    func editsDocument() -> [String: Any] {
        var d: [String: Any] = [:]
        for g in plan?.groups ?? [] {
            let e = edits[g.id] ?? GroupEdit(name: g.project, mergeInto: g.mergeInto ?? "")
            var x: [String: Any] = ["include": e.include]
            if e.mergeInto.isEmpty {
                x["merge_into"] = NSNull()
                x["project"] = e.name.trimmingCharacters(in: .whitespaces)
            } else {
                x["merge_into"] = e.mergeInto
            }
            if e.split { x["split"] = true }
            d[g.id] = x
        }
        return d
    }

    func destination(for g: SortGroup) -> String {
        let e = edits[g.id]
        if g.splitParts.count > 1 && e?.split == true { return g.splitParts.map { $0.0 }.joined(separator: " + ") }
        if let m = e?.mergeInto, !m.isEmpty { return m }
        return e?.name ?? g.project
    }

    func apply() {
        guard let p = plan, let r = renamer else { return }
        if let why = applyBlockedReason { r.alert("Can't apply right now", why); return }
        let groups = includedGroups
        guard !groups.isEmpty else { r.alert("Nothing to apply", "All groups are excluded."); return }
        for g in groups where (edits[g.id]?.mergeInto ?? "").isEmpty && (edits[g.id]?.name ?? "").trimmingCharacters(in: .whitespaces).isEmpty {
            r.alert("Project name missing", "Group \(g.id) needs a project name (or choose an existing project to merge into).")
            return
        }
        let n = groups.reduce(0) { $0 + $1.moves }
        let lines = groups.map { g -> String in
            let e = edits[g.id]
            let tag = (e?.mergeInto.isEmpty == false || g.existing) ? "existing" : "new"
            return "• \(destination(for: g)) (\(tag)) — \(g.moves) file(s)"
        }.joined(separator: "\n")
        let text = lines + "\n\n\(n) file(s) move inside the drive (no copying; same-name files are never overwritten — a suffix is added). " +
            "Clips already imported into a DaVinci Resolve project go offline and need relinking.\n\nAn undo map is written; Undo Last Sort puts everything back."
        guard r.confirm("Move \(n) file(s) into \(groups.count) project folder(s)?", text, ok: "Move Files") else { return }
        let tmp = FileManager.default.temporaryDirectory.appendingPathComponent("clipgauge-sort-edits-\(p.id).json")
        do {
            let data = try JSONSerialization.data(withJSONObject: editsDocument(), options: [.prettyPrinted])
            try data.write(to: tmp)
        } catch { r.alert("Couldn't write the edits", error.localizedDescription); return }
        working = true
        runPython(["--apply", "--plan", p.file, "--edits", tmp.path, "--yes"]) { code, o, err in
            self.working = false
            try? FileManager.default.removeItem(at: tmp)
            if code != 0 || (o["ok"] as? Bool) != true {
                r.alert("Sort not applied", self.fail(code, o, err))
                return
            }
            let moved = intVal(o["moved"]) ?? 0
            let skipped = (o["skipped"] as? [Any])?.count ?? 0
            let renamed = (o["renamed"] as? [Any])?.count ?? 0
            self.result = "Moved \(moved) file(s)" + (renamed > 0 ? ", \(renamed) renamed with a suffix (same name already there)" : "") +
                (skipped > 0 ? ", \(skipped) skipped" : "") + ". Undo map: \((o["undo"] as? String).map { URL(fileURLWithPath: $0).lastPathComponent } ?? "—")"
            self.plan = nil
            self.renamer?.notifier.post("ClipGauge: sorted \(moved) file(s)", "Undo Last Sort in the Sort window puts them back.")
            self.reload()
            self.renamer?.tick()
        }
    }

    func undoLast() {
        guard let r = renamer else { return }
        if let why = applyBlockedReason { r.alert("Can't undo right now", why); return }
        working = true
        runPython(["--undo", "--dry-run"]) { code, o, err in
            self.working = false
            guard code == 0 else { r.alert("Nothing to undo", self.fail(code, o, err)); return }
            let n = (o["would_restore"] as? [Any])?.count ?? 0
            let id = o["plan_id"] as? String ?? "?"
            guard r.confirm("Undo the last sort?", "Moves \(n) file(s) from sort \(id) back where they came from and removes the folders that sort created (only if empty). Clip notes get their old paths back.", ok: "Undo Sort") else { return }
            self.working = true
            self.runPython(["--undo", "--plan-id", id, "--yes"]) { code, o, err in
                self.working = false
                if code != 0 || (o["ok"] as? Bool) != true { r.alert("Undo failed", self.fail(code, o, err)); return }
                let skipped = (o["skipped"] as? [Any])?.count ?? 0
                self.result = "Undo: \(intVal(o["restored"]) ?? 0) file(s) moved back" + (skipped > 0 ? ", \(skipped) skipped (see log)" : "") + "."
                self.reload()
            }
        }
    }
}

// MARK: - window

struct SortView: View {
    @ObservedObject var sort: SortModel
    @ObservedObject var model: RenamerModel
    var scrolls = true

    var body: some View {
        if scrolls {
            ScrollView { content }
                .frame(width: 700)
                .frame(minHeight: 460, idealHeight: 760, maxHeight: 980)
        } else {
            content.frame(width: 700)
        }
    }

    private var content: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(spacing: 12) {
                Image(systemName: "folder.badge.gearshape").font(.system(size: 28)).foregroundStyle(.secondary)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Sort into Projects").font(.system(.title3, design: .rounded).weight(.semibold))
                    Text("Groups clips by shoot, then files them into \(rootLabel)/<Project>/ A-Roll · B-Roll · Images · _Notes · _Review. Dry run first — nothing moves until you apply.")
                        .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
            }
            sourceGroup
            if let e = sort.error {
                Label(e, systemImage: "exclamationmark.triangle.fill").font(.callout).foregroundStyle(Level.critical.color)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let r = sort.result {
                Label(r, systemImage: "checkmark.circle.fill").font(.callout).foregroundStyle(Level.normal.color)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let p = sort.plan { planView(p) }
            footer
        }
        .padding(20)
    }

    private var rootLabel: String {
        sort.resolveRoot.isEmpty ? "DaVinci Resolve" : URL(fileURLWithPath: sort.resolveRoot).lastPathComponent
    }

    private var sourceGroup: some View {
        PrefGroup(title: "Source",
                  footer: sort.untouchable.isEmpty ? nil : "Never touched: " + sort.untouchable.map { "\($0.name) (\($0.kind == "held" ? "on hold" : "Cloud-synced"))" }.joined(separator: ", ") + " · plus Resolve's own folders.") {
            HStack(spacing: 10) {
                Picker("", selection: $sort.selected) {
                    ForEach(sort.sortable) { s in
                        Label("\(s.name) — \(s.note)", systemImage: s.symbol).tag(s.id)
                    }
                }
                .labelsHidden()
                .frame(maxWidth: 360)
                Button("Choose Folder…") { sort.chooseFolder() }
                Spacer(minLength: 8)
                Stepper(value: $sort.gapMinutes, in: 10...720, step: 10) {
                    Text("Gap \(sort.gapMinutes) min").monospacedDigit().font(.caption)
                }
                .help("A new shoot starts after this much time between clips (or on a new day)")
                Button {
                    sort.dryRun()
                } label: {
                    HStack(spacing: 4) {
                        if sort.planning { ProgressView().controlSize(.mini) } else { Image(systemName: "eye") }
                        Text(sort.planning ? "Reading…" : "Dry Run")
                    }
                }
                .buttonStyle(.borderedProminent)
                .disabled(sort.planning || sort.working || !model.lexarOK)
            }
            .padding(.vertical, 8)
        }
    }

    @ViewBuilder private func planView(_ p: SortPlan) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Dry run · \(p.groups.count) group\(p.groups.count == 1 ? "" : "s")").font(.system(size: 13, weight: .semibold))
                Text(URL(fileURLWithPath: p.source).lastPathComponent).font(.caption).foregroundStyle(.secondary)
                Spacer()
                Text("\(p.moves) to move · \(p.keeps) already in place").font(.caption).foregroundStyle(.secondary).monospacedDigit()
            }
            ForEach(Array(p.warnings.enumerated()), id: \.offset) { _, w in
                Label(w, systemImage: "exclamationmark.triangle.fill").font(.caption).foregroundStyle(Level.warning.color)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if p.groups.isEmpty {
                Text("No clips or photos found in this folder.").foregroundStyle(.secondary)
            }
            ForEach(p.groups) { g in
                GroupCard(group: g, edit: sort.binding(g.id), existing: p.existingProjects, verify: p.sourceKind == "project")
            }
        }
    }

    private var footer: some View {
        HStack(spacing: 10) {
            Button("Undo Last Sort…") { sort.undoLast() }
                .disabled(sort.working || sort.lastSort == nil || sort.lastSort?.undone == true)
                .help(sort.lastSort.map { "Last sort \($0.id): \($0.moved) file(s)\($0.undone ? " — already undone" : "")" } ?? "No sorts yet")
            if let l = sort.lastSort {
                Text("Last sort \(l.time.prefix(16).replacingOccurrences(of: "T", with: " ")) · \(l.moved) file(s)\(l.undone ? " · undone" : "")")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            if let why = sort.applyBlockedReason, sort.plan != nil {
                Label(why, systemImage: "pause.circle.fill").font(.caption).foregroundStyle(Level.warning.color).lineLimit(2)
            }
            if sort.working { ProgressView().controlSize(.small) }
            Button("Apply…") { sort.apply() }
                .buttonStyle(.borderedProminent)
                .disabled(sort.plan == nil || sort.applyBlockedReason != nil || sort.working || sort.includedGroups.reduce(0) { $0 + $1.moves } == 0)
                .help("Moves the files of the included groups (asks first)")
        }
    }
}

private struct GroupCard: View {
    let group: SortGroup
    @Binding var edit: GroupEdit
    let existing: [String]
    let verify: Bool
    @ViewState private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Toggle("", isOn: $edit.include).toggleStyle(.checkbox).labelsHidden().help("Include this group")
                TextField("Project name", text: $edit.name)
                    .textFieldStyle(.roundedBorder)
                    .frame(maxWidth: 240)
                    .disabled(!edit.mergeInto.isEmpty || edit.split || verify)
                badge
                Spacer(minLength: 6)
                if !verify {
                    Picker("", selection: $edit.mergeInto) {
                        Text("New project").tag("")
                        ForEach(existing, id: \.self) { e in Text("Merge into “\(e)”").tag(e) }
                    }
                    .labelsHidden()
                    .frame(maxWidth: 230)
                    .disabled(edit.split)
                }
            }
            Text("\(group.clipCount) clip\(group.clipCount == 1 ? "" : "s")\(group.photoCount > 0 ? " · \(group.photoCount) photo\(group.photoCount == 1 ? "" : "s")" : "") · \(group.timeRange)")
                .font(.caption.weight(.medium)).monospacedDigit()
            if !group.description.isEmpty {
                Text(group.description).font(.caption).foregroundStyle(.secondary).lineLimit(2)
            }
            HStack(spacing: 6) {
                Image(systemName: "arrow.turn.down.right").font(.caption2).foregroundStyle(.secondary)
                Text(destText).font(.caption.monospaced()).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
                Spacer(minLength: 4)
                ForEach(group.buckets, id: \.0) { b in
                    Text("\(b.0) \(b.1)").font(.caption2.weight(.semibold)).monospacedDigit()
                        .padding(.horizontal, 6).padding(.vertical, 2)
                        .background((b.0 == "_Review" ? Level.warning.color : Color.secondary).opacity(0.16), in: Capsule())
                }
            }
            ForEach(group.flags, id: \.self) { f in
                Label(f, systemImage: "flag.fill").font(.caption).foregroundStyle(Level.warning.color)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if group.splitParts.count > 1 {
                Toggle(isOn: $edit.split) {
                    Text("Split into " + group.splitParts.map { "\($0.0) (\($0.1))" }.joined(separator: " + ")).font(.caption)
                }
                .toggleStyle(.checkbox)
            }
            DisclosureGroup(isExpanded: $expanded) {
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(group.items) { i in
                        HStack(spacing: 6) {
                            Image(systemName: i.action == "keep" ? "checkmark" : (i.kind == "sidecar" ? "doc.text" : (i.kind == "photo" ? "photo" : "film")))
                                .font(.caption2).foregroundStyle(.secondary).frame(width: 14)
                            Text(i.name).font(.caption.monospaced()).lineLimit(1).truncationMode(.middle)
                            Text("→ \(i.bucket)").font(.caption).foregroundStyle(i.bucket == "_Review" ? Level.warning.color : .secondary)
                            Spacer(minLength: 4)
                            Text(i.reason).font(.caption2).foregroundStyle(.tertiary).lineLimit(1)
                        }
                    }
                }
                .padding(.top, 4)
            } label: {
                Text("Files (\(group.items.count))").font(.caption).foregroundStyle(.secondary)
            }
        }
        .padding(12)
        .opacity(edit.include ? 1 : 0.5)
        .background(.fill.quinary, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(.separator.opacity(0.6), lineWidth: 0.5))
    }

    private var isExisting: Bool { verify || !edit.mergeInto.isEmpty || existing.contains { $0.lowercased() == edit.name.lowercased() } }

    private var badge: some View {
        let label = edit.split ? "SPLIT" : (isExisting ? "EXISTING" : "NEW")
        let color: Color = edit.split ? .purple : (isExisting ? .blue : Level.normal.color)
        return Text(label).font(.caption2.weight(.bold))
            .foregroundStyle(color)
            .padding(.horizontal, 7).padding(.vertical, 2)
            .background(color.opacity(0.15), in: Capsule())
    }

    private var destText: String {
        if edit.split && group.splitParts.count > 1 { return group.splitParts.map { $0.0 + "/" }.joined(separator: " + ") }
        let base = URL(fileURLWithPath: group.dest).deletingLastPathComponent()
        let name = edit.mergeInto.isEmpty ? (verify ? group.project : edit.name) : edit.mergeInto
        return base.lastPathComponent + "/" + name + "/"
    }
}
