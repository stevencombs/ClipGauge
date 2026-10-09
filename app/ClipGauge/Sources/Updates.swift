// Setup › Updates (v0.3): checks the renamer's AI models and tools for updates (scripts/check_updates.py — read-only)
// and applies them on request (scripts/apply_updates.py --detach — background, resumable, logged). ClipGauge only
// shows results, asks before every upgrade with the download size, and reads logs/updates-status.json for progress.
import AppKit
import SwiftUI

struct UpdateItem: Identifiable {
    let id: String
    let kind: String        // model | tool | whisper | advisory
    let name: String
    let status: String      // current | outdated | missing | unknown | info
    let detail: String
    let role: String?
    let current: String?
    let latest: String?
    let downloadBytes: Int64?
    let sizeBytes: Int64?
    let upgradable: Bool
    let action: String
    let link: URL?

    init(_ d: [String: Any]) {
        id = d["id"] as? String ?? UUID().uuidString
        kind = d["kind"] as? String ?? "?"
        name = d["name"] as? String ?? id
        status = d["status"] as? String ?? "unknown"
        detail = d["detail"] as? String ?? ""
        role = d["role"] as? String
        current = d["current"] as? String
        latest = d["latest"] as? String
        downloadBytes = (d["download_bytes"] as? NSNumber)?.int64Value
        sizeBytes = (d["size_bytes"] as? NSNumber)?.int64Value
        upgradable = d["upgradable"] as? Bool ?? false
        action = d["action"] as? String ?? "Upgrade"
        link = (d["link"] as? String).flatMap(URL.init(string:))
    }

    var symbol: String {
        switch kind {
        case "model": return "eye"
        case "tool": return "shippingbox"
        case "whisper": return "waveform"
        default: return "sparkles"
        }
    }
    var kindLabel: String {
        switch kind {
        case "model": return role == "tier" ? "Vision model · in use" : "Vision model"
        case "tool": return "Homebrew tool"
        case "whisper": return role == "tier" ? "Speech model · in use" : "Speech model"
        default: return "Newer model available"
        }
    }
}

struct UpdateJob {
    let state: String           // running | done | done_with_errors | cancelled | blocked
    let percent: Int
    let speedBps: Double
    let eta: Date?
    let message: String
    let current: String?
    let index: Int
    let total: Int
    let itemStates: [String: (state: String, message: String)]
    let log: String?
    let updatedAt: Date?

    init(_ d: [String: Any]) {
        state = d["state"] as? String ?? "?"
        percent = intVal(d["percent"]) ?? 0
        speedBps = (d["speed_bps"] as? NSNumber)?.doubleValue ?? 0
        eta = (d["eta"] as? String).flatMap { isoParser.date(from: $0) }
        message = d["message"] as? String ?? ""
        current = d["current"] as? String
        index = intVal(d["item_index"]) ?? 0
        total = intVal(d["item_total"]) ?? 0
        var m: [String: (String, String)] = [:]
        for it in d["items"] as? [[String: Any]] ?? [] {
            if let id = it["id"] as? String { m[id] = (it["state"] as? String ?? "", it["message"] as? String ?? "") }
        }
        itemStates = m
        log = d["log"] as? String
        updatedAt = (d["updated_at"] as? String).flatMap { isoParser.date(from: $0) }
    }

    var hasPending: Bool { itemStates.values.contains { $0.state == "pending" || $0.state == "failed" } }
}

func byteString(_ n: Int64?) -> String {
    guard let n else { return "size unknown" }
    return ByteCountFormatter.string(fromByteCount: n, countStyle: .file)
}

/// "about 50 min on slow hotel Wi-Fi (~2 MB/s), about 4 min on fast broadband (~25 MB/s)"
func downloadEstimate(_ bytes: Int64) -> String {
    let slow = Double(bytes) / 2_000_000, fast = Double(bytes) / 25_000_000
    return "about \(fmtDuration(max(slow, 1))) on slow hotel Wi-Fi (~2 MB/s), about \(fmtDuration(max(fast, 1))) on fast broadband (~25 MB/s)"
}

final class UpdatesModel: ObservableObject {
    @Published var items: [UpdateItem] = []
    @Published var checkedAt: Date?
    @Published var checkReason: String?
    @Published var checking = false
    @Published var checkError: String?
    @Published var brewNote: String?
    @Published var job: UpdateJob?
    @Published var jobActive = false
    @Published var launching = false
    @Published var autoCheck: Bool {
        didSet { UserDefaults.standard.set(autoCheck, forKey: "autoCheckUpdates") }
    }

    weak var renamer: RenamerModel?
    private var root: URL?
    private var stateStamp: Date?
    private var prevActive: Bool?
    private var lastAutoAttempt = Date.distantPast
    static let weekly: TimeInterval = 7 * 24 * 3600

    init() {
        autoCheck = UserDefaults.standard.object(forKey: "autoCheckUpdates") as? Bool ?? true
    }

    var upgradable: [UpdateItem] { items.filter { $0.upgradable } }
    var advisories: [UpdateItem] { items.filter { $0.kind == "advisory" } }
    var totalDownload: Int64 { upgradable.reduce(0) { $0 + ($1.downloadBytes ?? 0) } }
    var progressPercent: Int? { jobActive ? job?.percent ?? 0 : nil }

    var blockedReason: String? {
        guard let r = renamer else { return nil }
        if r.runActive { return "A processing run is active — upgrades wait until it finishes." }
        if r.resolveRunning { return "DaVinci Resolve is open — quit it to upgrade." }
        return nil
    }

    // MARK: polling (from RenamerModel.tick)

    func poll(root r: URL?) {
        root = r
        guard let r else { if jobActive { jobActive = false }; return }
        let logs = r.appendingPathComponent("logs")
        let stateURL = logs.appendingPathComponent("updates-state.json")
        let m = (try? FileManager.default.attributesOfItem(atPath: stateURL.path))?[.modificationDate] as? Date
        if m != stateStamp, let o = readJSONObject(stateURL) {
            stateStamp = m
            apply(state: o)
        }
        let active = UpdatesModel.lockActive(logs.appendingPathComponent("updates.lock"))
        if active != jobActive { jobActive = active }
        if let o = readJSONObject(logs.appendingPathComponent("updates-status.json")) {
            job = UpdateJob(o)
        }
        detectJobEnd()
    }

    func apply(state o: [String: Any]) {
        items = (o["items"] as? [[String: Any]] ?? []).map(UpdateItem.init)
        checkedAt = (o["checked_at"] as? String).flatMap { isoParser.date(from: $0) }
        checkReason = o["reason"] as? String
        if let e = o["brew_update_error"] as? String { brewNote = "Homebrew index not refreshed: \(e)" }
        else if (o["brew_updated"] as? Bool) == true { brewNote = "Homebrew index refreshed (brew update)" }
        else { brewNote = "Homebrew index not refreshed on this check" }
    }

    static func lockActive(_ u: URL) -> Bool {
        guard fileExists(u) else { return false }
        guard let o = readJSONObject(u) else {
            let m = (try? FileManager.default.attributesOfItem(atPath: u.path))?[.modificationDate] as? Date
            return m.map { Date().timeIntervalSince($0) < 30 } ?? false
        }
        if let h = o["host"] as? String, !h.isEmpty, h != hostName() { return false }
        guard let pid = intVal(o["pid"]) else { return false }
        return pidAlive(Int32(pid))
    }

    private func detectJobEnd() {
        defer { prevActive = jobActive }
        guard let prev = prevActive, prev, !jobActive, let j = job, renamer?.preview == false else { return }
        let title: String
        switch j.state {
        case "done": title = "ClipGauge: updates installed"
        case "cancelled": title = "ClipGauge: updates paused"
        case "blocked": title = "ClipGauge: updates stopped"
        default: title = "ClipGauge: some updates failed"
        }
        renamer?.notifier.post(title, j.message, kind: .updates)
    }

    // MARK: actions

    private func script(_ name: String) -> URL? { root?.appendingPathComponent("scripts/\(name)") }

    private func runPython(_ args: [String], done: @escaping (Int32, String, String) -> Void) {
        guard let py = Project.python(), let r = root else { done(127, "", "python3 not found"); return }
        DispatchQueue.global(qos: .userInitiated).async {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: py)
            p.arguments = args
            p.currentDirectoryURL = r
            var env = ProcessInfo.processInfo.environment
            env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
            p.environment = env
            let out = Pipe(), err = Pipe()
            p.standardOutput = out
            p.standardError = err
            p.standardInput = FileHandle.nullDevice
            do { try p.run() } catch {
                DispatchQueue.main.async { done(126, "", error.localizedDescription) }
                return
            }
            let o = out.fileHandleForReading.readDataToEndOfFile()
            let e = err.fileHandleForReading.readDataToEndOfFile()
            p.waitUntilExit()
            let so = String(decoding: o, as: UTF8.self), se = String(decoding: e, as: UTF8.self)
            DispatchQueue.main.async { done(p.terminationStatus, so, se) }
        }
    }

    /// Explicit check (button / gear menu): refreshes Homebrew's index first (`brew update --quiet`).
    func check(explicit: Bool = true) {
        guard !checking, let s = script("check_updates.py"), fileExists(s) else {
            if root != nil, !checking { checkError = "scripts/check_updates.py is missing — update the project's scripts (Setup › Change… › Create Project copies them)." }
            return
        }
        checking = true
        checkError = nil
        var args = [s.path, "--json", "--reason", explicit ? "manual" : "weekly"]
        if !explicit { args.append("--no-brew-update") }
        runPython(args) { code, out, err in
            self.checking = false
            if code == 0, let d = out.data(using: .utf8),
               let o = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any] {
                self.apply(state: o)
                self.stateStamp = nil
                if !explicit, !self.upgradable.isEmpty {
                    self.renamer?.notifier.post("ClipGauge: \(self.upgradable.count) update(s) available",
                                                "\(byteString(self.totalDownload)) to download. Open Setup › Updates to review — nothing is downloaded automatically.", kind: .updates)
                }
            } else {
                self.checkError = "Check failed (exit \(code)): " + (err.split(separator: "\n").suffix(2).joined(separator: " ")).prefix(300)
            }
        }
    }

    /// Weekly automatic check: check only (no brew update, never downloads).
    func maybeAutoCheck() {
        guard autoCheck, root != nil, !checking, !jobActive, Date().timeIntervalSince(lastAutoAttempt) > 3600 else { return }
        if let c = checkedAt, Date().timeIntervalSince(c) < UpdatesModel.weekly { return }
        lastAutoAttempt = Date()
        check(explicit: false)
    }

    func upgrade(_ chosen: [UpdateItem]) {
        guard !chosen.isEmpty, let r = renamer else { return }
        if let why = blockedReason { r.alert("Can't upgrade right now", why); return }
        if jobActive { r.alert("Updates already running", "Wait for the current update job to finish (or cancel it)."); return }
        let total = chosen.reduce(Int64(0)) { $0 + ($1.downloadBytes ?? 0) }
        let unknown = chosen.contains { $0.downloadBytes == nil }
        let lines = chosen.map { "• \($0.name) — \($0.action.lowercased()) · \(byteString($0.downloadBytes))" }.joined(separator: "\n")
        let title = chosen.count == 1 ? "\(chosen[0].action) \(chosen[0].name)?" : "Upgrade all \(chosen.count) items?"
        let text = lines + "\n\nTotal download ≈ \(byteString(total))\(unknown ? " (+ Homebrew dependencies)" : "")." +
            (total > 0 ? "\nEstimated time: \(downloadEstimate(total)).\nHotel Wi-Fi is often slow — the job is resumable if it's interrupted." : "") +
            "\n\nRuns in the background (you can close this window). Processing can't start until it's done. The previous model is kept until the new one passes a quick test."
        guard r.confirm(title, text, ok: chosen.count == 1 ? chosen[0].action : "Upgrade All") else { return }
        start(["--items", chosen.map(\.id).joined(separator: ",")])
    }

    func upgradeAll() { upgrade(upgradable) }

    func resume() {
        guard let r = renamer else { return }
        if let why = blockedReason { r.alert("Can't resume right now", why); return }
        start(["--resume"])
    }

    private func start(_ what: [String]) {
        guard let s = script("apply_updates.py"), fileExists(s) else {
            renamer?.alert("Update script missing", "scripts/apply_updates.py wasn't found in the project folder.")
            return
        }
        launching = true
        runPython([s.path] + what + ["--yes", "--detach", "--json"]) { code, out, err in
            self.launching = false
            let o = out.data(using: .utf8).flatMap { try? JSONSerialization.jsonObject(with: $0) } as? [String: Any] ?? [:]
            if code != 0 || (o["ok"] as? Bool) == false {
                self.renamer?.alert("Couldn't start the upgrade", (o["error"] as? String) ?? (err.isEmpty ? out : err))
            }
            self.renamer?.tick()
        }
    }

    func cancel() {
        guard let s = script("apply_updates.py") else { return }
        runPython([s.path, "--cancel"]) { _, _, _ in self.renamer?.tick() }
    }

    func openLog() {
        if let l = job?.log { NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: l)]) }
        else if let r = root { NSWorkspace.shared.open(r.appendingPathComponent("logs")) }
    }
}

// MARK: - Setup › Updates

struct UpdatesSection: View {
    @ObservedObject var updates: UpdatesModel
    @ObservedObject var model: RenamerModel

    var body: some View {
        PrefGroup(title: "Updates",
                  footer: "Checks the AI models and tools this project uses. Upgrades download in the background (you can close this window), resume if interrupted, and keep the previous model until the new one passes a quick test. Log: logs/updates-*.log.") {
            header
            if let why = updates.blockedReason, !updates.upgradable.isEmpty || updates.jobActive {
                Divider()
                Label(why, systemImage: "pause.circle.fill").font(.caption).foregroundStyle(Level.warning.color).padding(.vertical, 6)
            }
            if let e = updates.checkError {
                Divider()
                Label(e, systemImage: "exclamationmark.triangle.fill").font(.caption).foregroundStyle(Level.critical.color)
                    .padding(.vertical, 6).fixedSize(horizontal: false, vertical: true)
            }
            if let j = updates.job, updates.jobActive || ["cancelled", "done_with_errors", "blocked"].contains(j.state) {
                Divider()
                JobRow(job: j, active: updates.jobActive, updates: updates)
            }
            ForEach(updates.items.filter { $0.kind != "advisory" }) { item in
                Divider()
                ItemRow(item: item, updates: updates, jobState: updates.job?.itemStates[item.id], jobActive: updates.jobActive)
            }
            ForEach(updates.advisories) { item in
                Divider()
                ItemRow(item: item, updates: updates, jobState: nil, jobActive: updates.jobActive)
            }
            Divider()
            HStack(spacing: 10) {
                Image(systemName: "calendar.badge.clock").frame(width: 20).foregroundStyle(.secondary)
                VStack(alignment: .leading, spacing: 1) {
                    Text("Check automatically every week")
                    Text("Checks only — never downloads. You'll get a notification if something's new.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                Toggle("", isOn: $updates.autoCheck).toggleStyle(.switch).labelsHidden().controlSize(.small)
            }
            .padding(.vertical, 6)
        }
    }

    private var header: some View {
        HStack(spacing: 10) {
            Image(systemName: "arrow.triangle.2.circlepath").frame(width: 20).foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 1) {
                Text(summary).fontWeight(.medium)
                Text(subtitle).font(.caption).foregroundStyle(.secondary).lineLimit(2)
            }
            Spacer(minLength: 8)
            Button { updates.check() } label: {
                HStack(spacing: 4) {
                    if updates.checking { ProgressView().controlSize(.mini) } else { Image(systemName: "arrow.clockwise") }
                    Text(updates.checking ? "Checking…" : "Check Now")
                }
            }
            .disabled(updates.checking || updates.jobActive || !model.lexarOK)
            .help("Refreshes Homebrew's index (brew update) and asks ollama.com / Hugging Face for the latest versions. Downloads nothing.")
            if updates.upgradable.count > 1 && !updates.jobActive {
                Button("Upgrade All…") { updates.upgradeAll() }
                    .buttonStyle(.borderedProminent)
                    .disabled(updates.blockedReason != nil || updates.launching)
                    .help("Upgrade all \(updates.upgradable.count) items — \(byteString(updates.totalDownload)) to download")
            }
        }
        .padding(.vertical, 7)
    }

    private var summary: String {
        if updates.checking && updates.items.isEmpty { return "Checking for updates…" }
        if updates.checkedAt == nil { return "Not checked yet" }
        let n = updates.upgradable.count
        if n == 0 { return "Everything is up to date" }
        return "\(n) update\(n == 1 ? "" : "s") available · \(byteString(updates.totalDownload))"
    }

    private var subtitle: String {
        guard let c = updates.checkedAt else { return "Check Now asks ollama.com, Homebrew and Hugging Face. Nothing is downloaded." }
        var s = "Last checked \(shortStamp(c))\(updates.checkReason == "weekly" ? " (weekly)" : "")"
        if let b = updates.brewNote { s += " · \(b)" }
        return s
    }
}

private struct JobRow: View {
    let job: UpdateJob
    let active: Bool
    @ObservedObject var updates: UpdatesModel

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 10) {
                Image(systemName: active ? "arrow.down.circle.fill" : (job.state == "cancelled" ? "pause.circle.fill" : "exclamationmark.circle.fill"))
                    .frame(width: 20).foregroundStyle(active ? Color.accentColor : Level.warning.color)
                VStack(alignment: .leading, spacing: 1) {
                    Text(active ? "Updating \(job.current ?? "…") (\(max(1, job.index)) of \(job.total))" : job.message)
                        .fontWeight(.medium).lineLimit(2)
                    Text(detail).font(.caption).foregroundStyle(.secondary).monospacedDigit().lineLimit(2)
                }
                Spacer(minLength: 8)
                if active {
                    Button("Cancel") { updates.cancel() }.help("Stops after the current chunk; Resume continues where it left off.")
                } else if job.hasPending {
                    Button("Resume") { updates.resume() }.disabled(updates.blockedReason != nil)
                }
                Button { updates.openLog() } label: { Image(systemName: "doc.text.magnifyingglass") }.help("Show the update log")
            }
            if active {
                ProgressView(value: Double(job.percent), total: 100).progressViewStyle(.linear)
            }
        }
        .padding(.vertical, 7)
    }

    private var detail: String {
        if !active { return job.updatedAt.map { "Last job \(shortStamp($0))" } ?? "" }
        var parts = ["\(job.percent)%"]
        if job.speedBps > 0 { parts.append(String(format: "%.1f MB/s", job.speedBps / 1_000_000)) }
        if let e = job.eta { parts.append("done ≈ \(clockString(e))") }
        if !job.message.isEmpty { parts.append(job.message) }
        return parts.joined(separator: " · ")
    }
}

private struct ItemRow: View {
    let item: UpdateItem
    @ObservedObject var updates: UpdatesModel
    let jobState: (state: String, message: String)?
    let jobActive: Bool

    var body: some View {
        HStack(alignment: .center, spacing: 10) {
            Image(systemName: item.symbol).frame(width: 20).foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 1) {
                HStack(spacing: 6) {
                    Text(item.name)
                    Text(item.kindLabel.uppercased()).font(.caption2.weight(.semibold)).foregroundStyle(.secondary)
                }
                Text(detailText).font(.caption).foregroundStyle(.secondary).lineLimit(3).fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 8)
            trailing
        }
        .padding(.vertical, 6)
        .accessibilityElement(children: .combine)
    }

    private var detailText: String {
        if let j = jobState, !j.message.isEmpty, j.state != "pending" || jobActive { return j.message }
        return item.detail
    }

    @ViewBuilder private var trailing: some View {
        if let j = jobState, jobActive || j.state == "failed" {
            switch j.state {
            case "running": ProgressView().controlSize(.small)
            case "done": Image(systemName: "checkmark.circle.fill").foregroundStyle(Level.normal.color)
            case "failed": Image(systemName: "xmark.octagon.fill").foregroundStyle(Level.critical.color)
            default: Image(systemName: "clock").foregroundStyle(.secondary)
            }
        } else if item.kind == "advisory" {
            if let l = item.link { Link("Learn more", destination: l).font(.caption) }
        } else if item.upgradable {
            Button(item.action) { updates.upgrade([item]) }
                .disabled(updates.blockedReason != nil || jobActive || updates.launching)
                .help("\(item.action) \(item.name) — \(byteString(item.downloadBytes))")
        } else if item.status == "current" {
            Label("Up to date", systemImage: "checkmark.circle.fill").labelStyle(.iconOnly)
                .foregroundStyle(Level.normal.color).help("Up to date")
        } else {
            Image(systemName: "questionmark.circle").foregroundStyle(.secondary).help(item.detail)
        }
    }
}
