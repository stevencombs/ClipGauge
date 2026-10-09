// ClipGauge model: reads logs/status.json, logs/pipeline.lock, logs/rename-log.jsonl and the dry-run report, and
// drives the engine directly through scripts/clipgauge_cli.py (JSON, no HTTP since v0.6), so the pipeline lock, the
// update lock, the Resolve guard and the rename log stay in charge. ClipGauge never renames anything itself.
import AppKit
import Combine
import Darwin
import ServiceManagement
import UserNotifications

let kVersion = (Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String) ?? "0.1"
let kAppName = "ClipGauge"
let kLegacyBundleID = "com.retrocombs.ClipGuage"   // ClipGuage ≤ v0.5 (old spelling): settings are migrated once
let kResolveBundlePrefix = "com.blackmagic-design.DaVinciResolve"
let kPythonCandidates = ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"]
let kPollSeconds: TimeInterval = 2.0
let kInboxPollSeconds: TimeInterval = 30.0

// MARK: - helpers

func fileExists(_ u: URL) -> Bool { FileManager.default.fileExists(atPath: u.path) }

func readJSONObject(_ u: URL) -> [String: Any]? {
    guard fileExists(u) else { return nil }
    for _ in 0..<3 {  // tolerate status.py's tmp -> replace
        if let d = try? Data(contentsOf: u),
           let o = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any] { return o }
        usleep(40_000)
    }
    return nil
}

func intVal(_ v: Any?) -> Int? {
    if let n = v as? NSNumber { return n.intValue }
    if let s = v as? String { return Int(s) }
    return nil
}

func hostName() -> String {
    var buf = [CChar](repeating: 0, count: 256)
    gethostname(&buf, 255)
    return String(cString: buf)
}

func pidAlive(_ pid: Int32) -> Bool {
    if pid <= 0 { return false }
    if kill(pid, 0) == 0 { return true }
    return errno == EPERM
}

func shortName(_ path: String?) -> String? {
    guard let p = path, !p.isEmpty else { return nil }
    return (p as NSString).lastPathComponent
}

func fmtDuration(_ secs: Double) -> String {
    let s = max(0, Int(secs.rounded()))
    if s < 60 { return "\(s)s" }
    let m = s / 60
    if m < 60 { return "\(m) min" }
    return "\(m / 60)h \(m % 60)m"
}

let isoParser: ISO8601DateFormatter = {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime]
    return f
}()

/// "9:42 AM HST"
func clockString(_ d: Date, zone: Bool = true) -> String {
    let f = DateFormatter()
    f.dateFormat = zone ? "h:mm a zzz" : "h:mm a"
    return f.string(from: d)
}

/// "Oct 7, 5:47 PM"
func shortStamp(_ d: Date) -> String {
    let f = DateFormatter()
    f.dateFormat = Calendar.current.isDateInToday(d) ? "'today' h:mm a" : "MMM d, h:mm a"
    return f.string(from: d)
}

// MARK: - project root (portable: the app sits in AI-Video-Renamer/)

enum Project {
    static func isProject(_ u: URL) -> Bool { fileExists(u.appendingPathComponent("scripts/run_pipeline.py")) }

    /// 1) $CLIPGAUGE_ROOT (or the old $CLIPGUAGE_ROOT)  2) the folder the app sits in  3) folder chosen/created in Setup or last good root
    /// 4) /Volumes/Lexar/AI-Video-Renamer  5) /Volumes/*/AI-Video-Renamer
    static func locate() -> URL? {
        var cands: [URL] = []
        let env = ProcessInfo.processInfo.environment
        if let e = env["CLIPGAUGE_ROOT"] ?? env["CLIPGUAGE_ROOT"], !e.isEmpty { cands.append(URL(fileURLWithPath: e)) }
        cands.append(Bundle.main.bundleURL.deletingLastPathComponent())
        if let s = UserDefaults.standard.string(forKey: "projectRoot") { cands.append(URL(fileURLWithPath: s)) }
        cands.append(URL(fileURLWithPath: "/Volumes/Lexar/AI-Video-Renamer"))
        if let vols = try? FileManager.default.contentsOfDirectory(atPath: "/Volumes") {
            for v in vols.sorted() where !v.hasPrefix(".") { cands.append(URL(fileURLWithPath: "/Volumes/\(v)/AI-Video-Renamer")) }
        }
        for c in cands where isProject(c) {
            let u = c.standardizedFileURL
            if UserDefaults.standard.string(forKey: "projectRoot") != u.path { UserDefaults.standard.set(u.path, forKey: "projectRoot") }
            return u
        }
        return nil
    }

    static func python() -> String? { kPythonCandidates.first { FileManager.default.isExecutableFile(atPath: $0) } }

    /// The remembered project folder (even if it's not reachable right now).
    static var remembered: String? { UserDefaults.standard.string(forKey: "projectRoot") }

    static func remember(_ u: URL) { UserDefaults.standard.set(u.standardizedFileURL.path, forKey: "projectRoot") }

    /// "Lexar not connected" / "Drive “X” not connected" / "No project folder yet".
    static var missingTitle: String {
        guard let r = remembered else { return "No project folder yet" }
        let parts = r.split(separator: "/")
        if r.hasPrefix("/Volumes/"), parts.count >= 2 {
            let vol = String(parts[1])
            return vol == "Lexar" ? "Lexar not connected" : "Drive “\(vol)” not connected"
        }
        return "Project folder not found"
    }

    /// Scripts bundled inside the app (Contents/Resources/engine) used to create a new project folder.
    static var bundledEngine: URL? {
        let u = Bundle.main.resourceURL?.appendingPathComponent("engine")
        return u.flatMap { fileExists($0.appendingPathComponent("scripts/run_pipeline.py")) ? $0 : nil }
    }
}

/// Mutable byte buffer shared with a pipe's readability handler (a class, so the @Sendable closure can append).
final class LineBuffer: @unchecked Sendable { var data = Data() }

/// Options for one run (popover Start uses the defaults; the main window sets the rest).
struct RunOptions {
    var dry: Bool
    var folder: String? = nil
    var projectFromFolder = false
    var sources: [String] = []          // non-empty = process in place (no copy into inbox/)
    var confirmResolve = false          // the user confirmed renaming under DaVinci Resolve
    var json: String {
        var d: [String: Any] = ["dry_run": dry]
        if let f = folder, !f.trimmingCharacters(in: .whitespaces).isEmpty, sources.isEmpty {
            d["use_folder"] = true; d["folder"] = f; d["project_from_folder"] = projectFromFolder
        }
        if !sources.isEmpty { d["sources"] = sources; d["confirm_resolve"] = confirmResolve }
        let data = (try? JSONSerialization.data(withJSONObject: d)) ?? Data("{}".utf8)
        return String(decoding: data, as: UTF8.self)
    }
}

/// ClipGuage (old spelling, ≤ v0.5) kept its settings under com.retrocombs.ClipGuage. Copy them once so the project
/// folder, update settings, window frames and the menu bar icon position carry over to ClipGauge.
enum LegacyDefaults {
    static func migrate() {
        let d = UserDefaults.standard
        guard !d.bool(forKey: "migratedFromClipGuage") else { return }
        defer { d.set(true, forKey: "migratedFromClipGuage") }
        guard let old = d.persistentDomain(forName: kLegacyBundleID), !old.isEmpty else { return }
        for (k, v) in old {
            let nk = k.replacingOccurrences(of: "ClipGuage", with: "ClipGauge")
            if d.object(forKey: nk) == nil { d.set(v, forKey: nk) }
        }
    }
}

// MARK: - notifications (UNUserNotificationCenter, falls back to osascript if not allowed)

final class Notifier: NSObject, UNUserNotificationCenterDelegate, ObservableObject {
    enum Mode { case checking, native, fallback }
    @Published var mode: Mode = .checking

    func setup() {
        let c = UNUserNotificationCenter.current()
        c.delegate = self
        c.requestAuthorization(options: [.alert, .sound]) { granted, _ in
            DispatchQueue.main.async { self.mode = granted ? .native : .fallback }
        }
    }

    enum Kind { case run, review, updates }

    /// v0.7: each kind can be switched off in Settings › Colors & Alerts › Notifications (as GrokGauge's alerts).
    func post(_ title: String, _ body: String, kind: Kind = .run) {
        let s = LayoutStore.shared.settings
        switch kind {
        case .run: guard s.notifyRuns else { return }
        case .review: guard s.notifyReview else { return }
        case .updates: guard s.notifyUpdates else { return }
        }
        if mode == .native {
            let content = UNMutableNotificationContent()
            content.title = title
            content.body = body
            content.sound = .default
            let req = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
            UNUserNotificationCenter.current().add(req) { err in
                if err != nil { DispatchQueue.main.async { self.mode = .fallback; self.osascript(title, body) } }
            }
        } else {
            osascript(title, body)
        }
    }

    private func osascript(_ title: String, _ body: String) {
        func esc(_ s: String) -> String { s.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"") }
        let b = body.count > 230 ? String(body.prefix(229)) + "…" : body
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        p.arguments = ["-e", "display notification \"\(esc(b))\" with title \"\(esc(title))\""]
        try? p.run()
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .list, .sound])
    }
}

// MARK: - model

enum Level { case normal, warning, critical, neutral }

final class RenamerModel: ObservableObject {
    @Published var root: URL?
    @Published var dryRun = true
    @Published var resolveRunning = false
    @Published var actionBusy: String?      // "Checking inbox…" / "Starting run…" / "Stopping run…"
    @Published var status: [String: Any] = [:]
    @Published var lock: [String: Any]?     // active lock (nil = nothing running)
    @Published var lastRenamed: String?
    @Published var lastRenamedAt: Date?
    @Published var lastRenamedPath: String?
    @Published var reviewPaths: [String] = []     // needs-review clips (inbox + batch folders) from /api/inbox
    @Published var inboxReview: Int?
    @Published var inboxPending: Int?
    @Published var updatedAt: Date?
    @Published var loginEnabled = false
    @Published var instructions = InstructionsSummary()   // config/custom-instructions.json (v0.5)
    @Published var inbox: [String: Any] = [:]             // last clipgauge_cli.py inbox answer (v0.6)
    @Published var problem: [String: Any]?                // last run's error card: file, reason, hint (v0.6)
    @Published var visionModel = "qwen2.5vl:7b"           // the tier's vision model (Ask the Model default)

    let notifier = Notifier()
    /// Setup › Updates (model/tool upgrades; v0.3).
    let updates = UpdatesModel()
    let preview: Bool
    /// Called before an alert so the popover can close first.
    var beforeModal: () -> Void = {}
    /// Open the Setup / About windows (set by the app delegate).
    var showSetup: () -> Void = {}
    var showAbout: () -> Void = {}
    var showSettings: () -> Void = {}
    var showUpdates: () -> Void = {}
    var showSort: () -> Void = {}
    var showInstructions: () -> Void = {}
    var showMain: () -> Void = {}
    var showChat: (String?) -> Void = { _ in }

    private var timer: Timer?
    private var lastInboxPoll = Date.distantPast
    private var renameLogStamp: (UInt64, Date)?
    private var reportOffset: UInt64?
    private var reportURL: URL?
    private var prevRunActive: Bool?
    private var inboxInFlight = false
    private var cancellables = Set<AnyCancellable>()

    init(preview: Bool = false) {
        self.preview = preview
        notifier.objectWillChange.sink { [weak self] _ in self?.objectWillChange.send() }.store(in: &cancellables)
        updates.objectWillChange.sink { [weak self] _ in self?.objectWillChange.send() }.store(in: &cancellables)
        updates.renamer = self
    }

    func start() {
        notifier.setup()
        let ws = NSWorkspace.shared.notificationCenter
        for n in [NSWorkspace.didLaunchApplicationNotification, NSWorkspace.didTerminateApplicationNotification,
                  NSWorkspace.didMountNotification, NSWorkspace.didUnmountNotification] {
            ws.addObserver(self, selector: #selector(tick), name: n, object: nil)
        }
        tick()
        let t = Timer(timeInterval: kPollSeconds, target: self, selector: #selector(tick), userInfo: nil, repeats: true)
        RunLoop.main.add(t, forMode: .common)
        timer = t
    }

    // MARK: derived state

    var lexarOK: Bool { root != nil }
    var runActive: Bool { lock != nil }
    var isPipelineRun: Bool {
        guard let l = lock else { return false }
        let argv = (l["argv"] as? [String])?.joined(separator: " ") ?? ""
        return argv.isEmpty || argv.contains("run_pipeline")
    }
    var lockArgv: String { (lock?["argv"] as? [String])?.joined(separator: " ") ?? "" }
    var state: String { (status["state"] as? String) ?? "idle" }
    var runIsDry: Bool { (status["dry_run"] as? Bool) ?? dryRun }
    var launchedBy: String? { lock?["launched_by"] as? String }
    var currentClip: String? { shortName(status["current_file"] as? String) }
    var stepMessage: String? { (status["message"] as? String).flatMap { $0.isEmpty ? nil : $0 } }
    var inApplications: Bool {
        let p = Bundle.main.bundlePath
        return p.hasPrefix("/Applications/") || p.hasPrefix(NSHomeDirectory() + "/Applications/")
    }

    /// (percent, clip index, clip total) while a pipeline run is active.
    var progress: (pct: Int, index: Int, total: Int)? {
        guard runActive, isPipelineRun else { return nil }
        if state == "starting" { return (0, 0, intVal(status["clip_total"]) ?? 0) }
        guard state == "running", let total = intVal(status["clip_total"]), total > 0 else { return nil }
        let queueLeft = (status["queue"] as? [Any])?.count
        let idx = intVal(status["clip_index"]) ?? (queueLeft.map { total - $0 } ?? 1)
        let step = (status["step"] as? String) ?? ""
        let fd = Double(intVal(status["frames_done"]) ?? 0), ft = Double(max(1, intVal(status["frames_total"]) ?? 9))
        let frac: Double
        switch step {
        case "selected": frac = 0.0
        case "transcribe_extract_audio": frac = 0.22
        case "transcribe_whisper": frac = 0.3
        case "describe": frac = 0.5
        case "report": frac = 0.98
        default: frac = step.contains("frame") ? 0.2 * fd / ft : 0.85
        }
        let pct = (Double(max(0, idx - 1)) + frac) / Double(total) * 100
        return (min(99, max(0, Int(pct))), idx, total)
    }

    var eta: Date? {
        guard runActive, isPipelineRun else { return nil }
        if let e = status["eta"] as? String, let d = isoParser.date(from: e) { return d }
        if let s = intVal(status["eta_seconds"]), s > 0 { return Date().addingTimeInterval(Double(s)) }
        return nil
    }

    var needsReviewCount: Int { inboxReview ?? (status["needs_review"] as? [Any])?.count ?? 0 }

    /// Short headline for the hero ring.
    var headline: String {
        if !lexarOK { return Project.missingTitle }
        if let b = actionBusy { return b }
        if runActive && isPipelineRun {
            if let p = progress, p.total > 0 { return "Clip \(max(1, p.index)) of \(p.total)" }
            return "Starting…"
        }
        if runActive { return lockArgv.contains("sort_projects") ? "Sorting into projects…" : "Applying renames…" }
        if updates.jobActive { return "Updating models & tools" }
        if resolveRunning { return "Paused, Resolve is open" }
        return "Idle"
    }

    var subline: String {
        if !lexarOK { return Project.remembered == nil ? "Open Setup to choose or create one" : "Plug in the drive to continue" }
        if runActive && isPipelineRun {
            var s = runIsDry ? "Dry run" : "Live"
            if let l = launchedBy { s += " · from \(l == "ui" ? "web UI (legacy)" : l)" }
            return s
        }
        if updates.jobActive, let j = updates.job {
            var t = "\(j.percent)% · \(j.current ?? "starting")"
            if let e = j.eta { t += " · done ≈ \(clockString(e))" }
            return t
        }
        switch state {
        case "done": return "Last run finished"
        case "done_with_errors", "error":
            if problemVisible { return state == "error" ? "Last run ended with an error" : "Last run finished with errors" }
            if (problem?["resolved"] as? Bool) == true { return "Last run finished · error since handled" }
            if problem != nil { return dryRun ? "Dry-run mode" : "Live mode · renames in place" }
            return state == "error" ? "Last run ended with an error" : "Last run finished with errors"
        case "stopped": return "Last run was stopped"
        case "blocked": return "Last run ended: blocked"
        case "running", "starting": return "Last run was interrupted"
        default: return dryRun ? "Dry-run mode" : "Live mode · renames in place"
        }
    }

    var level: Level {
        if !lexarOK { return .neutral }
        if runActive { return .normal }
        if resolveRunning { return .warning }
        if (state == "done_with_errors" || state == "error") && (problem == nil || problemVisible) { return .critical }
        return .neutral
    }

    // MARK: polling

    @objc func tick() {
        let r = Project.locate()
        if r != root { root = r }
        let resolve = NSWorkspace.shared.runningApplications.contains {
            ($0.bundleIdentifier ?? "").hasPrefix(kResolveBundlePrefix) && !$0.isTerminated
        }
        if resolve != resolveRunning { resolveRunning = resolve }
        if let r = root {
            let cfg = readJSONObject(r.appendingPathComponent("config/config.json")) ?? [:]
            dryRun = (cfg["dry_run"] as? Bool) ?? true
            let dr = ((cfg["sidecar"] as? [String: Any])?["dry_run_dir"] as? String) ?? "logs/dry-run"
            reportURL = (dr.hasPrefix("/") ? URL(fileURLWithPath: dr) : r.appendingPathComponent(dr)).appendingPathComponent("report.jsonl")
            status = readJSONObject(r.appendingPathComponent("logs/status.json")) ?? [:]
            let ins = InstructionsSummary.read(root: r)
            if ins != instructions { instructions = ins }
            lock = activeLock(r)
            readLastRenamed(r)
            if !preview { checkNewReviewItems() }
            refreshInbox(force: false)
            updates.poll(root: r)
            if !preview { updates.maybeAutoCheck() }
        } else {
            updates.poll(root: nil)
            status = [:]; lock = nil; inboxReview = nil; inboxPending = nil
        }
        loginEnabled = inApplications && SMAppService.mainApp.status == .enabled
        updatedAt = Date()
        if !preview { detectRunEnd() }
    }

    private func activeLock(_ r: URL) -> [String: Any]? {
        let u = r.appendingPathComponent("logs/pipeline.lock")
        guard fileExists(u) else { return nil }
        guard let o = readJSONObject(u) else {
            // being written: treat as active for 30 s (same rule as pipeline_lock.py)
            if let m = (try? FileManager.default.attributesOfItem(atPath: u.path))?[.modificationDate] as? Date,
               Date().timeIntervalSince(m) < 30 { return [:] }
            return nil
        }
        if let h = o["host"] as? String, !h.isEmpty, h != hostName() { return nil }
        guard let pid = intVal(o["pid"]), pidAlive(Int32(pid)) else { return nil }
        return o
    }

    private func readLastRenamed(_ r: URL) {
        let u = r.appendingPathComponent("logs/rename-log.jsonl")
        guard let a = try? FileManager.default.attributesOfItem(atPath: u.path),
              let size = (a[.size] as? NSNumber)?.uint64Value, let m = a[.modificationDate] as? Date else { return }
        if let s = renameLogStamp, s.0 == size, s.1 == m { return }
        renameLogStamp = (size, m)
        guard let fh = try? FileHandle(forReadingFrom: u) else { return }
        defer { try? fh.close() }
        try? fh.seek(toOffset: size > 131_072 ? size - 131_072 : 0)
        guard let d = try? fh.readToEnd() else { return }
        for line in String(decoding: d, as: UTF8.self).split(separator: "\n").reversed() {
            guard let ld = line.data(using: .utf8),
                  let o = (try? JSONSerialization.jsonObject(with: ld)) as? [String: Any],
                  (o["action"] as? String) == "renamed" else { continue }
            lastRenamed = shortName(o["new"] as? String)
            lastRenamedPath = o["new"] as? String
            lastRenamedAt = (o["time"] as? String).flatMap { isoParser.date(from: $0) }
            return
        }
    }

    /// Notify for each new report.jsonl line with needs_review = true (written per clip, dry run and live).
    private func checkNewReviewItems() {
        guard let u = reportURL,
              let size = ((try? FileManager.default.attributesOfItem(atPath: u.path))?[.size] as? NSNumber)?.uint64Value else { return }
        guard let off = reportOffset, size >= off else { reportOffset = size; return }  // first look / truncated: no backlog
        guard size > off, let fh = try? FileHandle(forReadingFrom: u) else { return }
        defer { try? fh.close() }
        try? fh.seek(toOffset: off)
        guard let d = try? fh.readToEnd(), let lastNL = d.lastIndex(of: 0x0A) else { return }
        let chunk = d[d.startIndex...lastNL]  // complete lines only
        reportOffset = off + UInt64(chunk.count)
        var names: [String] = []
        for line in String(decoding: chunk, as: UTF8.self).split(separator: "\n") {
            guard let ld = line.data(using: .utf8),
                  let o = (try? JSONSerialization.jsonObject(with: ld)) as? [String: Any],
                  (o["needs_review"] as? Bool) == true else { continue }
            names.append(shortName(o["source"] as? String) ?? "a clip")
        }
        if names.count == 1 {
            notifier.post("Clip needs review", "\(names[0]) kept its name (low confidence).", kind: .review)
        } else if names.count > 1 {
            notifier.post("\(names.count) clips need review", names.prefix(4).joined(separator: ", ") + (names.count > 4 ? "…" : ""), kind: .review)
        }
    }

    private func detectRunEnd() {
        let active = runActive
        defer { prevRunActive = active }
        guard let prev = prevRunActive, prev, !active else { return }
        let msg = stepMessage ?? ""
        switch state {
        case "done": notifier.post("\(kAppName): run finished", msg)
        case "done_with_errors": notifier.post("\(kAppName): finished with errors", msg)
        case "stopped": notifier.post("\(kAppName): run stopped", msg)
        case "error", "blocked": notifier.post("\(kAppName): run ended (\(state))", msg)
        default: notifier.post("\(kAppName): run ended", msg.isEmpty ? "Status: \(state)" : msg)
        }
        refreshInbox(force: true)
    }

    // MARK: engine bridge (scripts/clipgauge_cli.py — no HTTP, nothing leaves the Mac)

    /// Environment for engine scripts: Homebrew on PATH, unbuffered output, models on the project drive.
    func engineEnv() -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        env["PYTHONUNBUFFERED"] = "1"
        env["AI_VIDEO_RENAMER_LAUNCHED_BY"] = kAppName
        return env
    }

    /// `python3 scripts/clipgauge_cli.py <args>` off the main thread; `done` gets the JSON object (with "code") on main.
    func engine(_ args: [String], done: @escaping ([String: Any]) -> Void) {
        guard let r = root, let py = Project.python() else { done(["code": 0, "error": "python3 not found"]); return }
        let cli = r.appendingPathComponent("scripts/clipgauge_cli.py")
        guard fileExists(cli) else {
            done(["code": 0, "error": "scripts/clipgauge_cli.py is missing — update the project's scripts (ClipGauge v0.6 bundles it)."])
            return
        }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: py)
        p.arguments = [cli.path] + args
        p.currentDirectoryURL = r
        p.environment = engineEnv()
        let out = Pipe(), err = Pipe()
        p.standardOutput = out
        p.standardError = err
        p.standardInput = FileHandle.nullDevice
        DispatchQueue.global(qos: .userInitiated).async {
            do { try p.run() } catch {
                DispatchQueue.main.async { done(["code": 0, "error": error.localizedDescription]) }
                return
            }
            let o = out.fileHandleForReading.readDataToEndOfFile()
            let e = err.fileHandleForReading.readDataToEndOfFile()
            p.waitUntilExit()
            let text = String(decoding: o, as: UTF8.self)
            var obj: [String: Any] = [:]
            if let last = text.split(separator: "\n").last, let d = last.data(using: .utf8),
               let j = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any] { obj = j }
            if obj.isEmpty {
                let tail = String(decoding: e, as: UTF8.self).split(separator: "\n").suffix(6).joined(separator: "\n")
                obj = ["code": 0, "error": "The engine didn't answer (exit \(p.terminationStatus)).", "detail": tail]
            }
            DispatchQueue.main.async { done(obj) }
        }
    }

    /// Streaming variant (copy-in progress): one JSON object per line. Returns the process so it can be cancelled.
    @discardableResult
    func engineStream(_ args: [String], line: @escaping ([String: Any]) -> Void, done: @escaping (Int32) -> Void) -> Process? {
        guard let r = root, let py = Project.python() else { done(-1); return nil }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: py)
        p.arguments = [r.appendingPathComponent("scripts/clipgauge_cli.py").path] + args
        p.currentDirectoryURL = r
        p.environment = engineEnv()
        let out = Pipe()
        p.standardOutput = out
        p.standardError = FileHandle.nullDevice
        p.standardInput = FileHandle.nullDevice
        let buffer = LineBuffer()
        out.fileHandleForReading.readabilityHandler = { h in
            let chunk = h.availableData
            if chunk.isEmpty { return }
            buffer.data.append(chunk)
            while let nl = buffer.data.firstIndex(of: 0x0A) {
                let lineData = buffer.data[buffer.data.startIndex..<nl]
                buffer.data.removeSubrange(buffer.data.startIndex...nl)
                if let j = (try? JSONSerialization.jsonObject(with: Data(lineData))) as? [String: Any] {
                    DispatchQueue.main.async { line(j) }
                }
            }
        }
        p.terminationHandler = { proc in
            out.fileHandleForReading.readabilityHandler = nil
            let rest = out.fileHandleForReading.readDataToEndOfFile()
            for l in rest.split(separator: 0x0A) {
                if let j = (try? JSONSerialization.jsonObject(with: Data(l))) as? [String: Any] { DispatchQueue.main.async { line(j) } }
            }
            DispatchQueue.main.async { done(proc.terminationStatus) }
        }
        do { try p.run() } catch { done(-1); return nil }
        return p
    }

    func refreshInbox(force: Bool) {
        guard root != nil else { inboxReview = nil; inboxPending = nil; return }
        if !force && Date().timeIntervalSince(lastInboxPoll) < kInboxPollSeconds { return }
        if inboxInFlight { return }
        lastInboxPoll = Date()
        inboxInFlight = true
        engine(["inbox"]) { j in
            self.inboxInFlight = false
            guard intVal(j["code"]) == 200 else { return }
            // "states" covers the top of inbox/; each batch folder in "folders" carries its own "states".
            var review = intVal((j["states"] as? [String: Any])?["needs review"]) ?? 0
            let inbox = (j["dir"] as? String) ?? self.root?.appendingPathComponent("inbox").path ?? ""
            var paths: [String] = []
            for f in (j["files"] as? [[String: Any]]) ?? [] where (f["state"] as? String) == "needs review" {
                if let n = f["name"] as? String { paths.append(inbox + "/" + n) }
            }
            for f in (j["folders"] as? [[String: Any]]) ?? [] {
                review += intVal((f["states"] as? [String: Any])?["needs review"]) ?? 0
                let folder = (f["name"] as? String) ?? ""
                for c in (f["files"] as? [[String: Any]]) ?? [] where (c["state"] as? String) == "needs review" {
                    if let n = c["name"] as? String { paths.append(inbox + "/" + folder + "/" + n) }
                }
            }
            self.inboxReview = review
            self.reviewPaths = paths
            self.inboxPending = intVal(j["pending_count"])
            self.inbox = j
        }
        engine(["status"]) { j in
            guard intVal(j["code"]) == 200 else { return }
            self.problem = j["problem"] as? [String: Any]
            if let v = j["vision_model"] as? String { self.visionModel = v }
        }
    }

    // MARK: last-run problem card (never edits status.json; Dismiss is remembered per run)

    /// Identifies one run's error: the status time stamp + the error text.
    var problemKey: String? {
        guard let p = problem, let t = p["updated_at"] as? String else { return nil }
        return t + "|" + ((p["reason"] as? String) ?? "")
    }
    /// v0.6.1: dismissals are kept as a list (last 20) and written through immediately, so Dismiss survives quitting,
    /// relaunching and new builds; v0.6's single "dismissedProblem" (time stamp only) still counts.
    static func dismissedProblems() -> [String] { UserDefaults.standard.stringArray(forKey: "dismissedProblems") ?? [] }
    var problemDismissed: Bool {
        guard let k = problemKey else { return false }
        if Self.dismissedProblems().contains(k) { return true }
        if let old = UserDefaults.standard.string(forKey: "dismissedProblem"), old == (problem?["updated_at"] as? String) { return true }
        return false
    }
    var problemVisible: Bool {
        guard let p = problem, ["done_with_errors", "error"].contains(state), (p["resolved"] as? Bool) != true else { return false }
        return !problemDismissed
    }
    func dismissProblem() {
        guard let k = problemKey else { return }
        var list = Self.dismissedProblems().filter { $0 != k }
        list.append(k)
        UserDefaults.standard.set(Array(list.suffix(20)), forKey: "dismissedProblems")
        UserDefaults.standard.synchronize()   // a quit right after the click must not lose it
        objectWillChange.send()
    }
    func revealProblemFile() {
        if let f = problem?["file"] as? String, fileExists(URL(fileURLWithPath: f)) {
            NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: f)])
        } else { openInbox() }
    }

    // MARK: actions

    func toggleRun() {
        guard lexarOK, actionBusy == nil else { return }
        if runActive && isPipelineRun { stopRun() } else if !runActive { startRun() }
    }

    /// Why a run can't start right now (nil = it can).
    var startBlocker: String? {
        if !lexarOK { return Project.missingTitle }
        if runActive { return "A run is already active." }
        if resolveRunning { return "Paused, Resolve is open — quit DaVinci Resolve, then start processing." }
        if updates.jobActive { return "Updates are being installed (\(updates.job?.percent ?? 0)% done) — processing can start when they finish." }
        return nil
    }

    /// Popover Start: process the inbox with the current mode (confirm first). The main window has the full options.
    func startRun() {
        if let b = startBlocker { alert("Can't start yet", b); return }
        actionBusy = "Checking inbox…"
        engine(["inbox"]) { inbox in
            self.actionBusy = nil
            guard intVal(inbox["code"]) == 200 else { self.alert("Couldn't read the inbox", Self.errorText(inbox)); return }
            let pending = intVal(inbox["pending_count"]) ?? 0
            if pending == 0 {
                self.alert("Nothing to process", "Every clip in inbox/ is already renamed or waiting for review. Add clips in the \(kAppName) window (drop them on it or on the menu bar icon) first.")
                return
            }
            self.startRun(RunOptions(dry: self.dryRun), pending: pending)
        }
    }

    /// Confirm and start a run with options (from the popover or the main window).
    func startRun(_ o: RunOptions, pending: Int, confirmed: Bool = false) {
        if let b = startBlocker { alert("Can't start yet", b); return }
        let live = !(o.dry || dryRun)
        let est = fmtDuration(Double(max(1, pending)) * 120)
        let what = o.sources.isEmpty ? "\(pending) unprocessed clip(s) in inbox/" : "\(pending) clip(s) where they are (in place)"
        var text = live
            ? "Start a LIVE run over \(what)?\n\nConfident clips are renamed as each one finishes; low-confidence ones keep their name (needs review). About 2 min per clip on the Air (≈ \(est)).\n\nEvery rename is logged — Undo is in the Results tab."
            : "Start a dry-run review of \(what)?\n\nNothing is renamed. About 2 min per clip (≈ \(est))."
        if let f = o.folder, !f.isEmpty, o.sources.isEmpty { text += "\n\nRenamed clips go into inbox/\(f)/" + (o.projectFromFolder ? " and use “\(f)” as the project in their names." : ".") }
        if !confirmed {
            guard confirm(live ? "Start processing (live)" : "Start processing (dry run)", text, ok: "Start") else { return }
        }
        if let b = startBlocker { alert("Can't start yet", b); return }
        actionBusy = "Starting run…"
        engine(["start", o.json]) { j in
            self.actionBusy = nil
            self.tick()
            self.refreshInbox(force: true)
            let code = intVal(j["code"]) ?? 0
            if code >= 200 && code < 300 {
                let msg = (j["message"] as? String) ?? "Processing \(pending) clip(s)."
                if (j["finished"] as? Bool) == true { self.alert("Run finished right away", msg) }
                else { self.notifier.post("\(kAppName): run started", msg) }
                self.onRunStarted()
            } else if code == 409, j["needs_confirm"] != nil, !o.confirmResolve {
                var o2 = o
                o2.confirmResolve = self.confirmResolveRename()
                if o2.confirmResolve { self.startRun(o2, pending: pending, confirmed: true) }
            } else {
                self.alert("Couldn't start processing", Self.errorText(j))
            }
        }
    }

    /// Renaming media under DaVinci Resolve breaks links in projects that already imported it — ask every time.
    func confirmResolveRename() -> Bool {
        confirm("Rename clips inside the DaVinci Resolve folder?",
                "DaVinci Resolve links media by file path. If these clips are already imported in a Resolve project, renaming them makes them go offline (you'd have to relink).\n\nOnly continue if these clips are NOT in a Resolve project yet. Every rename is logged and can be undone.",
                ok: "Rename in place")
    }

    var onRunStarted: () -> Void = {}

    func stopRun() {
        let cur = currentClip ?? "the current clip"
        guard confirm("Stop processing?", "The clip in progress (\(cur)) is abandoned; clips already finished stay renamed and in the report.", ok: "Stop") else { return }
        actionBusy = "Stopping run…"
        engine(["stop"]) { j in
            self.actionBusy = nil
            self.tick()
            let code = intVal(j["code"]) ?? 0
            if !(code >= 200 && code < 300) { self.alert("Couldn't stop processing", Self.errorText(j)) }
        }
    }

    /// The real reason from the engine (error + the most useful log line + where the log is), never just a code.
    static func errorText(_ j: [String: Any]) -> String {
        var parts: [String] = []
        if let e = j["error"] as? String, !e.isEmpty { parts.append(e) }
        else { parts.append("The engine answered \(intVal(j["code"]) ?? 0) without a message.") }
        if let d = j["detail"] as? String {
            let lines = d.split(separator: "\n").map(String.init).filter { !$0.hasPrefix("#") && !$0.trimmingCharacters(in: .whitespaces).isEmpty }
            let tail = lines.suffix(6).joined(separator: "\n")
            if !tail.isEmpty && !(parts.first ?? "").contains(tail) { parts.append("Last lines of the run log:\n" + tail) }
        }
        if let l = j["log"] as? String { parts.append("Log: " + l) }
        return parts.joined(separator: "\n\n")
    }

    func openLastRenamed() {
        if let p = lastRenamedPath, FileManager.default.fileExists(atPath: p) {
            NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: p)])
        } else if let p = lastRenamedPath {
            let dir = URL(fileURLWithPath: p).deletingLastPathComponent()
            NSWorkspace.shared.open(fileExists(dir) ? dir : (root?.appendingPathComponent("inbox") ?? dir))
        } else {
            openInbox()
        }
    }

    /// Reveal the needs-review clips (they keep their original names in inbox/ or a batch folder).
    func openNeedsReview() {
        var urls = reviewPaths.map { URL(fileURLWithPath: $0) }.filter { fileExists($0) }
        if urls.isEmpty {
            urls = ((status["needs_review"] as? [String]) ?? []).map { URL(fileURLWithPath: $0) }.filter { fileExists($0) }
        }
        if urls.isEmpty { openInbox() } else { NSWorkspace.shared.activateFileViewerSelecting(urls) }
    }

    func openInbox() {
        guard let r = root else { return }
        NSWorkspace.shared.open(r.appendingPathComponent("inbox"))
    }

    func toggleLogin() {
        guard inApplications else { return }
        do {
            if SMAppService.mainApp.status == .enabled { try SMAppService.mainApp.unregister() }
            else { try SMAppService.mainApp.register() }
        } catch {
            alert("Open at login", error.localizedDescription + "\n\nYou can also add \(kAppName) in System Settings › General › Login Items.")
        }
        loginEnabled = SMAppService.mainApp.status == .enabled
    }

    func alert(_ title: String, _ text: String) {
        beforeModal()
        NSApp.activate(ignoringOtherApps: true)
        let a = NSAlert()
        a.messageText = title
        a.informativeText = text
        a.runModal()
    }

    func confirm(_ title: String, _ text: String, ok: String) -> Bool {
        beforeModal()
        NSApp.activate(ignoringOtherApps: true)
        let a = NSAlert()
        a.messageText = title
        a.informativeText = text
        a.addButton(withTitle: ok)
        a.addButton(withTitle: "Cancel")
        return a.runModal() == .alertFirstButtonReturn
    }
}
