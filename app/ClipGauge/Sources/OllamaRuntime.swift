// Keep the model ready (v0.8.5): while ClipGauge is open, the project's Ollama server is running and the chat model is
// loaded; when ClipGauge quits, the models it loaded are unloaded (keep_alive 0) and a server it started itself is
// stopped. A server run by the project's LaunchAgent (com.retrocombs.ollama-lexar) keeps running, with no model loaded.
//
//  • ensure() — on launch, when a drive mounts, when the project folder changes, every 60 s and from Retry:
//      1. GET /api/version. Not answering → kick the LaunchAgent (launchctl kickstart, or bootstrap if it isn't loaded);
//         without one, spawn `ollama serve` with OLLAMA_MODELS=<project>/models. Wait up to 45 s.
//      2. GET /api/tags. Empty while <project>/models has manifests → the server is stale (e.g. its binary was replaced
//         by `brew upgrade` and macOS now refuses it the drive): restart the LaunchAgent once (at most every 10 min,
//         never during a run or an update).
//      3. Pick the model: exact name, then the same name with another tag variant (prefix), then any installed vision
//         model, then any chat model.
//      4. "Keep model loaded while ClipGauge is open" on (default) and no run/update → POST /api/generate with
//         keep_alive -1 (load, no prompt). Re-warmed when /api/ps no longer lists it.
//  • shutdown() — from applicationShouldTerminate: unload, stop our own server, then let the app quit (≤ 8 s).
//    A clip run keeps going after Quit as before (it's detached), so its model and the server stay up for it.
// Loopback only. models/ is never touched directly — only through Ollama's own API.
import AppKit
import Foundation
import SwiftUI

let kKeepModelLoadedKey = "ClipGaugeKeepModelLoaded"
let kOllamaAgentLabel = "com.retrocombs.ollama-lexar"

final class OllamaRuntime: ObservableObject {
    static let shared = OllamaRuntime()

    enum Phase: Equatable {
        case idle                     // not checked yet
        case starting                 // starting the server / waiting for it
        case loading(String)          // loading the model into memory
        case ready(String)            // loaded (or, with the toggle off, installed and ready to load)
        case paused(String)           // a run or an update has the memory; the model loads after it
        case unreachable(String)      // server not answering: what was tried
        case noModel(String)          // server answers but no usable model
        case failed(String)           // anything else, with Ollama's words
    }

    @Published private(set) var phase: Phase = .idle
    @Published private(set) var resolved: String?            // the model the chat should use
    @Published private(set) var installed: [String] = []     // chat-capable names (/api/tags)
    weak var renamer: RenamerModel?
    /// The model the chat wants (config describe.model / tier vision model).
    var desired: () -> String = { "qwen2.5vl:7b" }

    private let q = DispatchQueue(label: "clipgauge.ollama-runtime")
    private var working = false
    private var again = false
    private var timer: Timer?
    private var observers: [NSObjectProtocol] = []
    private var spawned: Process?
    private let lock = NSLock()
    private var _loadedByUs = Set<String>()       // models ClipGauge loaded (guarded by lock)
    private var _loading: String?                  // a load in flight
    private var lastBase: URL?
    private var loadedByUs: Set<String> { lock.lock(); defer { lock.unlock() }; return _loadedByUs }
    private func mark(_ m: String, loaded: Bool) { lock.lock(); if loaded { _loadedByUs.insert(m) } else { _loadedByUs.remove(m) }; lock.unlock() }
    private func setLoading(_ m: String?) { lock.lock(); _loading = m; lock.unlock() }
    private var lastRestart = Date.distantPast
    private var shuttingDown = false
    private var wasBusy = false

    /// Settings › General › Models on this Mac: "Keep model loaded while ClipGauge is open" (per Mac, default on).
    static func keepLoaded(in d: UserDefaults) -> Bool { d.object(forKey: kKeepModelLoadedKey) as? Bool ?? true }
    static var keepLoaded: Bool { keepLoaded(in: .standard) }

    /// Previews (EXAMPLE DATA) only.
    func setPreview(_ p: Phase, resolved r: String?) { phase = p; resolved = r }

    // MARK: lifecycle

    func start(renamer r: RenamerModel) {
        if ChatModel.previewMode { return }
        renamer = r
        let nc = NSWorkspace.shared.notificationCenter
        for n in [NSWorkspace.didMountNotification, NSWorkspace.didWakeNotification] {
            observers.append(nc.addObserver(forName: n, object: nil, queue: .main) { [weak self] _ in
                DispatchQueue.main.asyncAfter(deadline: .now() + 3) { self?.ensure() }
            })
        }
        timer = Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in self?.ensure() }
        ensure()
    }

    /// Toggle changed in Settings: load now, or unload what we loaded.
    func keepLoadedChanged() {
        if OllamaRuntime.keepLoaded { ensure(force: true) }
        else { q.async { self.unloadOurs(except: []); self.ensure() } }
    }

    /// The chat used this model (so Quit unloads it too).
    func noteUsed(_ m: String) { mark(m, loaded: true) }

    /// keep_alive for a chat request: -1 while the toggle is on and this is the warmed model.
    func chatKeepAlive(for m: String, isDefault: Bool) -> Any {
        if OllamaRuntime.keepLoaded && (m == resolved || isDefault) { return -1 }
        return isDefault ? "5m" : "30s"
    }

    /// Check the server and the model, start/restart/warm as needed. Safe to call often; runs one at a time.
    func ensure(force: Bool = false, then: (() -> Void)? = nil) {
        if ChatModel.previewMode || shuttingDown { then?(); return }
        let snap = Snapshot(renamer)
        let want = desired()
        let keep = OllamaRuntime.keepLoaded
        q.async {
            if self.working { self.again = true; DispatchQueue.main.async { then?() }; return }
            self.working = true
            self.step(snap, want: want, keep: keep, force: force)
            self.working = false
            DispatchQueue.main.async {
                then?()
                if self.again { self.again = false; self.ensure() }
            }
        }
    }

    /// What the runtime needs from the app, read on the main thread.
    struct Snapshot {
        var base: URL?
        var root: URL?
        var busy: Bool          // a clip run or an update job
        var runModel: String?   // the model the run uses
        init(_ r: RenamerModel?) {
            base = ChatModel.ollamaBase(root: r?.root)
            root = r?.root
            busy = (r?.runActive ?? false) || (r?.updates.jobActive ?? false)
            runModel = MachineModels.shared.usedVision ?? r?.visionModel
        }
        init(base: URL?, root: URL?, busy: Bool, runModel: String?) {
            self.base = base; self.root = root; self.busy = busy; self.runModel = runModel
        }
    }

    private func set(_ p: Phase, resolved r: String?? = nil, installed i: [String]? = nil) {
        DispatchQueue.main.async {
            if self.phase != p { self.phase = p }
            if let r { self.resolved = r }
            if let i, self.installed != i { self.installed = i }
        }
    }

    private func step(_ s: Snapshot, want: String, keep: Bool, force: Bool) {
        guard let base = s.base else {
            set(.failed("config describe.ollama_url isn't on this Mac — \(kAppName) only talks to a local Ollama.")); return
        }
        lastBase = base
        // 1. server
        if !Self.http(base, "api/version", timeout: 2).ok {
            set(.starting)
            let how = startServer(root: s.root)
            var up = false
            for _ in 0..<45 { if Self.http(base, "api/version", timeout: 2).ok { up = true; break }; Thread.sleep(forTimeInterval: 1) }
            if !up {
                set(.unreachable("Ollama isn't answering on \(base.host ?? "127.0.0.1"):\(base.port ?? 11434). \(how)"), installed: [])
                return
            }
        }
        // 2. models (+ stale-server repair)
        var rows = Self.tagRows(base)
        var names = ChatModel.chatModels(rows)
        let onDisk = s.root.map { Self.modelsOnDisk($0.appendingPathComponent("models")) } ?? false
        if names.isEmpty && onDisk {
            if !s.busy && (force || Date().timeIntervalSince(lastRestart) > 600) && Self.agentPlist != nil {
                lastRestart = Date()
                NSLog("ClipGauge: Ollama lists no models but the project has some — restarting %@", kOllamaAgentLabel)
                set(.starting)
                Self.launchctl(["kickstart", "-k", Self.agentTarget])
                for _ in 0..<45 {
                    Thread.sleep(forTimeInterval: 1)
                    if Self.http(base, "api/version", timeout: 2).ok { rows = Self.tagRows(base); if !rows.isEmpty { break } }
                }
                names = ChatModel.chatModels(rows)
            }
            if names.isEmpty {
                set(.noModel("Ollama is running but can't see this project's models (models/ has them). Another Ollama may be "
                             + "running, or macOS is refusing it the drive after an Ollama update — Retry restarts it."), resolved: .some(nil), installed: [])
                return
            }
        }
        let vision = rows.compactMap { d -> String? in
            guard let n = d["name"] as? String, ChatModel.isRealName(n), (d["capabilities"] as? [String])?.contains("vision") == true else { return nil }
            return n
        }
        guard let pick = Self.resolve(want, among: names, vision: vision) else {
            set(.noModel("No chat model is installed in this project's Ollama (expected \(want)). Setup › Install Missing… pulls it."),
                resolved: .some(nil), installed: names)
            return
        }
        set(phase, resolved: .some(pick), installed: names)
        // 3. memory
        if s.busy {
            if !wasBusy, let rm = s.runModel { unloadOurs(except: [rm]) }   // the run gets the memory
            wasBusy = true
            set(.paused(pick)); return
        }
        wasBusy = false
        guard keep else { set(.ready(pick)); return }
        let ps = Self.http(base, "api/ps", timeout: 3).json
        if Self.names(fromPS: ps).contains(pick) {
            // Loaded. A clip run's own requests (keep_alive 10m) shorten the timer: pin it again, quietly.
            if !Self.pinned(fromPS: ps).contains(pick) {
                _ = Self.http(base, "api/generate", body: ["model": pick, "keep_alive": -1, "prompt": ""], timeout: 30)
            }
            mark(pick, loaded: true); set(.ready(pick)); return
        }
        set(.loading(pick))
        setLoading(pick)
        let r = Self.http(base, "api/generate", body: ["model": pick, "keep_alive": -1, "prompt": ""], timeout: 600)
        setLoading(nil)
        if shuttingDown { return }
        if r.ok {
            mark(pick, loaded: true)
            unloadOurs(except: [pick] + (s.runModel.map { [$0] } ?? []))   // only one model pinned at a time
            set(.ready(pick))
        } else {
            set(.failed("Couldn't load \(pick): \(Self.ollamaError(r) ?? "no answer").\(Self.hint(r))"))
        }
    }

    // MARK: quit

    /// Unload what ClipGauge loaded, stop a server ClipGauge started; `done` on the main thread within ~8 s.
    func shutdown(done: @escaping () -> Void) {
        if ChatModel.previewMode || renamer == nil { DispatchQueue.main.async { done() }; return }
        shuttingDown = true
        timer?.invalidate()
        let s = Snapshot(renamer)
        var finished = false
        let finish = { DispatchQueue.main.async { if !finished { finished = true; done() } } }
        DispatchQueue.main.asyncAfter(deadline: .now() + 8) { finish() }
        DispatchQueue.global(qos: .userInitiated).async {
            guard let base = s.base else { finish(); return }
            self.lock.lock(); let ours = self._loadedByUs; let inFlight = self._loading; self.lock.unlock()
            let ps = Self.loaded(base) + (inFlight.map { [$0] } ?? [])
            let drop = Self.quitPlan(loaded: ps, ours: ours.union(inFlight.map { [$0] } ?? []), runActive: s.busy, runModel: s.runModel)
            for m in drop { _ = Self.http(base, "api/generate", body: ["model": m, "keep_alive": 0], timeout: 3) }
            if let p = self.spawned, p.isRunning {
                if s.busy { NSLog("ClipGauge: a run is active — leaving the Ollama server it uses running") }
                else { p.terminate(); for _ in 0..<20 where p.isRunning { Thread.sleep(forTimeInterval: 0.1) } }
            }
            NSLog("ClipGauge quit: unloaded %@", drop.joined(separator: ", "))
            finish()
        }
    }

    /// Which loaded models to unload at Quit. No run: everything in /api/ps (this project's server serves ClipGauge).
    /// A run still going: only what ClipGauge itself loaded, never the run's model.
    static func quitPlan(loaded: [String], ours: Set<String>, runActive: Bool, runModel: String?) -> [String] {
        var seen = Set<String>()
        let uniq = loaded.filter { seen.insert($0).inserted }
        if !runActive { return uniq }
        return uniq.filter { ours.contains($0) && $0 != runModel }
    }

    private func unloadOurs(except keepThese: [String]) {
        guard let b = lastBase else { return }
        let ours = loadedByUs
        for m in Self.loaded(b) where ours.contains(m) && !keepThese.contains(m) {
            _ = Self.http(b, "api/generate", body: ["model": m, "keep_alive": 0], timeout: 5)
            mark(m, loaded: false)
        }
    }

    // MARK: server start

    static var agentPlist: URL? {
        let u = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/LaunchAgents/\(kOllamaAgentLabel).plist")
        return FileManager.default.fileExists(atPath: u.path) ? u : nil
    }
    static var agentTarget: String { "gui/\(getuid())/\(kOllamaAgentLabel)" }

    /// Start the server the least invasive way; returns what was tried (for the error text).
    private func startServer(root: URL?) -> String {
        if let plist = Self.agentPlist {
            if Self.launchctl(["print", Self.agentTarget]) == 0 {
                Self.launchctl(["kickstart", Self.agentTarget])
                return "Started the project's Ollama service (\(kOllamaAgentLabel)); it waits for the project drive."
            }
            Self.launchctl(["bootstrap", "gui/\(getuid())", plist.path])
            return "Loaded the project's Ollama service (\(kOllamaAgentLabel)); it waits for the project drive."
        }
        if let p = spawned, p.isRunning { return "\(kAppName) started `ollama serve`; it hasn't answered yet." }
        guard let exe = Self.ollamaExe else { return "Ollama isn't installed — Setup › Install Missing… installs it." }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: exe)
        p.arguments = ["serve"]
        var env = ProcessInfo.processInfo.environment
        if let m = root?.appendingPathComponent("models"), FileManager.default.fileExists(atPath: m.path) { env["OLLAMA_MODELS"] = m.path }
        env["OLLAMA_HOST"] = "127.0.0.1:11434"
        p.environment = env
        let logDir = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/ClipGauge")
        try? FileManager.default.createDirectory(at: logDir, withIntermediateDirectories: true)
        let log = logDir.appendingPathComponent("ollama-serve.log")
        if !FileManager.default.fileExists(atPath: log.path) { FileManager.default.createFile(atPath: log.path, contents: nil) }
        if let h = try? FileHandle(forWritingTo: log) { h.seekToEndOfFile(); p.standardOutput = h; p.standardError = h }
        do { try p.run(); spawned = p } catch { return "Couldn't start \(exe) serve: \(error.localizedDescription)" }
        return "\(kAppName) started `ollama serve` (log: ~/Library/Logs/ClipGauge/ollama-serve.log)."
    }

    static var ollamaExe: String? {
        ["/opt/homebrew/bin/ollama", "/usr/local/bin/ollama", "/Applications/Ollama.app/Contents/Resources/ollama"]
            .first { FileManager.default.isExecutableFile(atPath: $0) }
    }

    @discardableResult static func launchctl(_ args: [String]) -> Int32 {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        p.arguments = args
        p.standardOutput = FileHandle.nullDevice; p.standardError = FileHandle.nullDevice
        do { try p.run(); p.waitUntilExit(); return p.terminationStatus } catch { return -1 }
    }

    // MARK: pure helpers (selftested)

    /// The model to use: exact, then the same name with another tag variant ("qwen2.5vl:7b" ↔ "qwen2.5vl:7b-q4_K_M",
    /// "qwen2.5vl" ↔ "qwen2.5vl:latest"), then the first installed vision model, then any chat model.
    static func resolve(_ want: String, among names: [String], vision: [String] = []) -> String? {
        let real = names.filter(ChatModel.isRealName)
        if real.contains(want) { return want }
        let w = want.hasSuffix(":latest") ? String(want.dropLast(7)) : want
        if let m = real.first(where: { $0 == w || $0 == w + ":latest" }) { return m }
        if let m = real.sorted(by: { $0.count < $1.count }).first(where: { $0.hasPrefix(w + "-") || $0.hasPrefix(w + "_") || (!w.contains(":") && $0.hasPrefix(w + ":")) }) { return m }
        let fam = w.split(separator: ":").first.map(String.init) ?? w
        if let m = real.first(where: { $0.hasPrefix(fam + ":") }) { return m }
        if let m = vision.first(where: { real.contains($0) }) { return m }
        return real.first
    }

    /// True when models/ holds at least one manifest (AppleDouble "._*" files on exFAT don't count). Read-only.
    static func modelsOnDisk(_ models: URL) -> Bool {
        let fm = FileManager.default
        for top in ["manifests-v2", "manifests"] {
            let d = models.appendingPathComponent(top)
            guard let e = fm.enumerator(at: d, includingPropertiesForKeys: [.isRegularFileKey], options: [.skipsHiddenFiles]) else { continue }
            var n = 0
            for case let u as URL in e {
                n += 1; if n > 2000 { break }
                if e.level > 5 { e.skipDescendants(); continue }
                if !u.lastPathComponent.hasPrefix("._"), (try? u.resourceValues(forKeys: [.isRegularFileKey]))?.isRegularFile == true { return true }
            }
        }
        return false
    }

    static func names(fromPS o: [String: Any]?) -> [String] {
        ((o?["models"] as? [[String: Any]]) ?? []).compactMap { ($0["name"] as? String) ?? ($0["model"] as? String) }
    }

    /// Loaded models kept forever (keep_alive -1: Ollama reports an expiry centuries away).
    static func pinned(fromPS o: [String: Any]?, now: Date = Date()) -> [String] {
        let year = Calendar(identifier: .gregorian).component(.year, from: now)
        return ((o?["models"] as? [[String: Any]]) ?? []).compactMap { d in
            guard let n = (d["name"] as? String) ?? (d["model"] as? String), let e = d["expires_at"] as? String,
                  let y = Int(e.prefix(4)), y > year + 50 else { return nil }
            return n
        }
    }

    /// The header text and whether Retry makes sense.
    static func describe(_ p: Phase, keep: Bool) -> (text: String, icon: String, retry: Bool) {
        switch p {
        case .idle: return ("Checking Ollama…", "hourglass", false)
        case .starting: return ("Starting Ollama…", "hourglass", false)
        case .loading(let m): return ("Loading \(m)…", "arrow.down.circle", false)
        case .ready(let m): return (keep ? "Ready · \(m)" : "Ready · \(m) (loads on first question)", "checkmark.circle.fill", false)
        case .paused(let m): return ("\(m) · paused for the run", "pause.circle", false)
        case .unreachable(let s), .noModel(let s), .failed(let s): return (s, "exclamationmark.triangle.fill", true)
        }
    }

    // MARK: HTTP (loopback, blocking — runtime queue only)

    struct Reply { var ok: Bool; var status: Int; var json: [String: Any]?; var error: String? }

    static func http(_ base: URL, _ path: String, body: [String: Any]? = nil, timeout: TimeInterval) -> Reply {
        var req = URLRequest(url: base.appendingPathComponent(path), timeoutInterval: timeout)
        req.httpMethod = body == nil ? "GET" : "POST"
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            var b = body; if path.hasSuffix("generate") { b["stream"] = false }
            req.httpBody = try? JSONSerialization.data(withJSONObject: b)
        }
        let cfg = URLSessionConfiguration.ephemeral
        cfg.connectionProxyDictionary = [:]
        let sem = DispatchSemaphore(value: 0)
        var out = Reply(ok: false, status: 0, json: nil, error: nil)
        let s = URLSession(configuration: cfg)
        s.dataTask(with: req) { data, resp, err in
            out.status = (resp as? HTTPURLResponse)?.statusCode ?? 0
            out.json = data.flatMap { try? JSONSerialization.jsonObject(with: $0) } as? [String: Any]
            out.error = err?.localizedDescription ?? (out.json?["error"] as? String)
            out.ok = err == nil && out.status == 200
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + timeout + 2)
        s.finishTasksAndInvalidate()
        return out
    }

    static func tagRows(_ base: URL) -> [[String: Any]] {
        (http(base, "api/tags", timeout: 5).json?["models"] as? [[String: Any]]) ?? []
    }
    static func loaded(_ base: URL) -> [String] { names(fromPS: http(base, "api/ps", timeout: 3).json) }
    static func ollamaError(_ r: Reply) -> String? { r.error ?? (r.status == 0 ? nil : "HTTP \(r.status)") }
    static func hint(_ r: Reply) -> String {
        let l = (r.error ?? "").lowercased()
        if l.contains("operation not permitted") { return " macOS refused Ollama the project drive — Retry restarts it." }
        if l.contains("not found") { return " It isn't installed — Setup › Install Missing… pulls it." }
        return ""
    }

    /// Retry from the chat: force a restart if the server looks stale.
    func retry() { ensure(force: true) }
}

/// v0.8.5: the chat header's runtime state (starting / loading model / ready), with Retry when something is wrong.
struct OllamaStatusLine: View {
    @ObservedObject var rt: OllamaRuntime = .shared
    @ObservedObject var chat: ChatModel
    var compact = false
    var onlyProblems = false

    var body: some View {
        let d = OllamaRuntime.describe(rt.phase, keep: OllamaRuntime.keepLoaded)
        if !onlyProblems || d.retry { line(d) }
    }

    private func line(_ d: (text: String, icon: String, retry: Bool)) -> some View {
        HStack(spacing: 4) {
            switch rt.phase {
            case .idle, .starting, .loading: ProgressView().controlSize(.mini)
            default: Image(systemName: d.icon).foregroundStyle(d.retry ? Color.red : (rt.phase == .ready(rt.resolved ?? "") ? Color.green : Color.secondary))
            }
            Text(compact && d.retry ? "Not ready" : d.text)
                .foregroundStyle(d.retry ? Color.red : Color.secondary)
                .lineLimit(compact ? 1 : 4).truncationMode(.middle).fixedSize(horizontal: false, vertical: !compact)
            if d.retry && !compact {   // compact headers say "Not ready"; the full reason + Retry sit below
                Button("Retry") { chat.error = nil; rt.ensure(force: true) { chat.loadModels() } }
                    .controlSize(.mini).help("Start or restart the project's Ollama and check the model again")
            }
        }
        .font(.caption2)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Model status: \(d.text)")
    }
}
