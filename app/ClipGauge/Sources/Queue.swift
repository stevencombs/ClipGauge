// ClipGauge v0.8 — the multi-project queue (logs/queue.json, owned by scripts/project_queue.py).
// The app reads the file on every tick (cheap, no engine call) and changes it only through clipgauge_cli.py
// queue-* commands, which take the queue's edit lock. Same rules as everywhere: one run at a time, never while
// DaVinci Resolve is open, held folders refused, one Resolve confirmation per queue, never overwrite.
import AppKit
import SwiftUI

struct QueueItem: Identifiable, Equatable {
    var id: String
    var name: String
    var paths: [String]
    var mode: String                 // in_place | copy
    var transcribe: Bool
    var nameInFilenames: Bool
    var status: String               // waiting|running|paused|done|done_with_errors|skipped|refused|failed|stopped
    var reason: String
    var message: String
    var mediaCount: Int
    var copyCount: Int
    var needsConfirm: Bool
    var index: Int
    var total: Int
    var current: String?
    var step: String?
    var etaSeconds: Int?
    var batches: [String]

    init(_ d: [String: Any]) {
        id = d["id"] as? String ?? UUID().uuidString
        name = d["name"] as? String ?? "?"
        paths = d["paths"] as? [String] ?? []
        mode = d["mode"] as? String ?? "in_place"
        transcribe = (d["transcribe"] as? Bool) ?? true
        nameInFilenames = (d["name_in_filenames"] as? Bool) ?? false
        status = d["status"] as? String ?? "waiting"
        reason = d["reason"] as? String ?? ""
        message = d["message"] as? String ?? ""
        mediaCount = intVal(d["media_count"]) ?? 0
        copyCount = intVal(d["copy_count"]) ?? 0
        needsConfirm = (d["needs_confirm"] as? Bool) ?? false
        let p = d["progress"] as? [String: Any] ?? [:]
        index = intVal(p["index"]) ?? 0
        total = intVal(p["total"]) ?? 0
        current = p["current"] as? String
        step = p["step"] as? String
        etaSeconds = intVal(p["eta_seconds"])
        batches = d["batches"] as? [String] ?? []
    }

    var clips: Int { mode == "copy" ? copyCount : mediaCount }
    var isRefused: Bool { status == "refused" }
    var isFinished: Bool { ["done", "done_with_errors", "skipped", "refused", "failed"].contains(status) }
    var isRunning: Bool { status == "running" }
    var fraction: Double { total > 0 ? min(1, Double(max(0, index - 1)) / Double(total)) : 0 }
    var folderURL: URL? {
        guard let p = paths.first else { return nil }
        let u = URL(fileURLWithPath: p)
        return FileManager.default.isDirectory(u) ? u : u.deletingLastPathComponent()
    }
    var location: String {
        guard let u = folderURL else { return "" }
        return u.deletingLastPathComponent().path.replacingOccurrences(of: "/Volumes/", with: "")
    }
}

struct QueueState: Equatable {
    var state = "idle"               // idle|running|pausing|paused|stopped|done
    var message = ""
    var alive = false
    var dryRun = false
    var autoBundle = false
    var saveNextToClips = true
    var bundleScope = "queue"
    var items: [QueueItem] = []

    static func read(_ root: URL) -> QueueState {
        var q = QueueState()
        guard let d = readJSONObject(root.appendingPathComponent("logs/queue.json")) else { return q }
        q.state = d["state"] as? String ?? "idle"
        q.message = d["message"] as? String ?? ""
        if let pid = intVal(d["pid"]) { q.alive = pidAlive(Int32(pid)) }
        let o = d["options"] as? [String: Any] ?? [:]
        q.dryRun = (o["dry_run"] as? Bool) ?? false
        q.autoBundle = (o["auto_bundle"] as? Bool) ?? false
        q.saveNextToClips = (o["save_next_to_clips"] as? Bool) ?? true
        q.bundleScope = o["bundle_scope"] as? String ?? "queue"
        q.items = ((d["items"] as? [[String: Any]]) ?? []).map(QueueItem.init)
        if (q.state == "running" || q.state == "pausing") && !q.alive {
            q.state = "stopped"
            if q.message.isEmpty || q.message.hasPrefix("Running") { q.message = "The queue stopped before it finished (quit, crash or sleep) — Start resumes it." }
        }
        return q
    }

    var running: Bool { alive && (state == "running" || state == "pausing") }
    var left: [QueueItem] { items.filter { ["waiting", "stopped", "running"].contains($0.status) } }
    var clipsLeft: Int { left.reduce(0) { $0 + $1.clips } }
    var current: QueueItem? { items.first { $0.isRunning } }
    var doneCount: Int { items.filter { $0.status == "done" || $0.status == "done_with_errors" }.count }
    var canResume: Bool { !running && (state == "stopped" || state == "paused") && !left.isEmpty }
    /// batch id -> project name (Results groups rows by project).
    var projectByBatch: [String: String] {
        var m: [String: String] = [:]
        for it in items { for b in it.batches { m[b] = it.name } }
        return m
    }
    var summaryLine: String {
        if items.isEmpty { return "Queue empty" }
        let waiting = left.filter { !$0.isRunning }.count
        var parts: [String] = []
        if doneCount > 0 { parts.append("\(doneCount) done") }
        if current != nil { parts.append("1 running") }
        if waiting > 0 { parts.append("\(waiting) waiting") }
        let paused = items.filter { $0.status == "paused" }.count
        if paused > 0 { parts.append("\(paused) paused") }
        return parts.isEmpty ? "\(items.count) project(s)" : parts.joined(separator: " · ")
    }
}

/// Estimated time for N clips from the engine's average (logs/timings.json), else ~3 min a clip (vision model on a
/// 16 GB Mac). Only a hint in the footer.
func queueEstimate(clips: Int) -> String {
    let secs = clips * 180
    if secs < 90 { return "" }
    let h = secs / 3600, m = (secs % 3600) / 60
    return h > 0 ? "≈ \(h) h \(m) min" : "≈ \(m) min"
}

// MARK: - popover card (v0.8 minimal popover)

struct QueueCard: View {
    @ObservedObject var model: RenamerModel

    var body: some View {
        let q = model.queue
        VStack(alignment: .leading, spacing: 8) {
            if let c = q.current {
                Text("\(c.name) · clip \(max(1, c.index)) of \(max(c.total, c.clips))").font(.callout.weight(.semibold)).lineLimit(1)
                ProgressView(value: c.fraction).progressViewStyle(.linear)
                Text(stepLine(c)).font(.caption).foregroundStyle(.secondary).lineLimit(1)
            } else if q.items.isEmpty {
                Text("Nothing queued").font(.callout.weight(.semibold))
                Text("Drop folders or clips on the menu bar icon or the \(kAppName) window.").font(.caption).foregroundStyle(.secondary)
            } else {
                Text(q.canResume ? "Queue \(q.state == "paused" ? "paused" : "stopped")" : (q.state == "done" ? "Queue finished" : "Queue ready"))
                    .font(.callout.weight(.semibold))
                if !q.message.isEmpty { Text(q.message).font(.caption).foregroundStyle(.secondary).lineLimit(2) }
            }
            Text("Queue: \(q.summaryLine)" + (q.clipsLeft > 0 ? " · \(q.clipsLeft) clip(s) left" : ""))
                .font(.caption).foregroundStyle(.secondary)
            if let n = model.lastRenamed { Text("Last: \(n)").font(.caption2).foregroundStyle(.tertiary).lineLimit(1).truncationMode(.middle) }
            HStack {
                if q.running {
                    Button(q.state == "pausing" ? "Pausing…" : "Pause") { model.queuePause() }.disabled(q.state == "pausing")
                } else if q.canResume || (!q.left.isEmpty && q.state != "running") {
                    Button(q.canResume ? "Resume" : "Start") { model.queueStart() }
                        .disabled(model.startBlocker != nil)
                        .help(model.startBlocker ?? "Run the queue")
                }
                Spacer()
                Button("Open \(kAppName)") { model.showMain() }.buttonStyle(.borderedProminent)
            }
            .controlSize(.small)
        }
        .padding(12)
        .background(.fill.quinary, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
    }

    private func stepLine(_ c: QueueItem) -> String {
        var s = (c.step == "copying") ? "Copying into the inbox" : (c.step.map { $0.capitalized } ?? "Working")
        if let e = c.etaSeconds, e > 0 { s += " · \(e / 60) min left in this project" }
        return s
    }
}

// MARK: - queue actions (RenamerModel)

extension RenamerModel {
    private func json(_ o: [String: Any]) -> String {
        String(decoding: (try? JSONSerialization.data(withJSONObject: o)) ?? Data("{}".utf8), as: UTF8.self)
    }

    private func queueDone(_ j: [String: Any], _ title: String) {
        tick()
        let code = intVal(j["code"]) ?? 0
        if !(code >= 200 && code < 300) { alert(title, Self.errorText(j)) }
    }

    func queueAdd(_ urls: [URL], mode: String? = nil) {
        var o: [String: Any] = ["paths": urls.map { $0.standardizedFileURL.path }]
        if let m = mode { o["mode"] = m }
        actionBusy = "Checking \(urls.count) item(s)…"
        engine(["queue-add", json(o)]) { j in
            self.actionBusy = nil
            self.queueDone(j, "Couldn't add to the queue")
        }
    }

    func queueUpdate(_ id: String, _ fields: [String: Any]) {
        engine(["queue-update", json(["id": id, "fields": fields])]) { self.queueDone($0, "Couldn't change the project") }
    }
    func queueRemove(_ id: String) { engine(["queue-remove", id]) { self.queueDone($0, "Couldn't remove the project") } }
    func queueMove(_ id: String, to: Int) { engine(["queue-move", id, String(to)]) { self.queueDone($0, "Couldn't reorder") } }
    func queueSkip(_ id: String) { engine(["queue-skip", id]) { self.queueDone($0, "Couldn't skip") } }
    func queueClear(all: Bool) {
        if all && !confirm("Clear the whole queue?", "Every project is removed from the list. Nothing on disk changes; renamed clips stay renamed.", ok: "Clear") { return }
        engine(["queue-clear"] + (all ? ["--all"] : [])) { self.queueDone($0, "Couldn't clear the queue") }
    }
    func queueOptions(_ o: [String: Any]) { engine(["queue-options", json(o)]) { self.queueDone($0, "Couldn't save the queue option") } }
    func queuePause() { engine(["queue-pause"]) { self.queueDone($0, "Couldn't pause") } }
    func queueStop() {
        let cur = queue.current?.current ?? "the clip in progress"
        guard confirm("Stop the queue now?", "\(cur) is abandoned (clips already finished stay renamed). Start resumes the queue later. To finish the current clip first, use Pause.", ok: "Stop") else { return }
        engine(["queue-stop"]) { self.queueDone($0, "Couldn't stop") }
    }

    /// Start or resume the queue: one confirmation, plus ONE Resolve confirmation for the whole queue if needed.
    func queueStart(dryRun: Bool? = nil, autoBundle: Bool? = nil, confirmed: Bool = false, confirmResolve: Bool = false) {
        if let b = startBlocker { alert("Can't start yet", b); return }
        let q = queue
        let dry = dryRun ?? q.dryRun
        let live = !(dry || self.dryRun)
        if !confirmed {
            let what = "\(q.left.count) project(s), \(q.clipsLeft) clip(s)" + (queueEstimate(clips: q.clipsLeft).isEmpty ? "" : ", \(queueEstimate(clips: q.clipsLeft))")
            let mode = live ? "Live: confident clips are renamed (logged, undoable per project)." : "Dry run: nothing is renamed."
            guard confirm(q.canResume ? "Resume the queue?" : "Start the queue?", "\(what).\n\n\(mode)\nFinished clips are skipped, so a resumed project picks up where it stopped.", ok: q.canResume ? "Resume" : "Start") else { return }
        }
        var o: [String: Any] = ["dry_run": dry, "confirm_resolve": confirmResolve]
        if let a = autoBundle { o["auto_bundle"] = a }
        actionBusy = "Starting queue…"
        engine(["queue-start", json(o)]) { j in
            self.actionBusy = nil
            if intVal(j["code"]) == 409, let names = j["needs_confirm"] as? [String], !confirmResolve {
                let list = names.prefix(8).map { "• \($0)" }.joined(separator: "\n") + (names.count > 8 ? "\n…" : "")
                if self.confirm("Rename clips inside the DaVinci Resolve folder?",
                                "These projects are inside the DaVinci Resolve folder:\n\(list)\n\nRenaming clips Resolve has already imported makes them go offline. Only continue if they're NOT in a Resolve project yet. Asked once for this queue; every rename is logged and can be undone.",
                                ok: "Rename in place") {
                    self.queueStart(dryRun: dry, autoBundle: autoBundle, confirmed: true, confirmResolve: true)
                }
                return
            }
            self.queueDone(j, "Couldn't start the queue")
            if (intVal(j["code"]) ?? 0) / 100 == 2 { self.onRunStarted() }
        }
    }

    func revealProject(_ it: QueueItem) {
        if let u = it.folderURL { NSWorkspace.shared.activateFileViewerSelecting([u]) }
    }

    /// Undo every rename of one project (each queue run of it is one batch in rename-log.jsonl).
    func undoProject(_ it: QueueItem, after: @escaping () -> Void = {}) {
        guard !it.batches.isEmpty else { alert("Nothing to undo", "\(it.name) hasn't renamed anything yet."); return }
        guard confirm("Undo “\(it.name)”?", "Every rename from this project (\(it.batches.count) run(s)) goes back to its original name. Logged too.", ok: "Undo project") else { return }
        actionBusy = "Undoing \(it.name)…"
        var left = it.batches
        var errors: [String] = []
        func next() {
            guard let b = left.first else {
                self.actionBusy = nil
                if !errors.isEmpty { self.alert("Undo didn't finish", errors.joined(separator: "\n")) }
                self.tick(); after(); return
            }
            left.removeFirst()
            engine(["undo", "--batch", b]) { j in
                let code = intVal(j["code"]) ?? 0
                if !(code >= 200 && code < 300) { errors.append(Self.errorText(j)) }
                next()
            }
        }
        next()
    }
}
