// App update check (v0.7) — GrokGauge's About › Updates ("Check for updates daily", Check Now, "is up to date" /
// "Version X is available", last checked). GrokGauge asks GitHub's public releases; ClipGauge has no public repo
// (and must not publish one), so it checks the ClipGauge source in the project folder instead — app/ClipGauge/
// Info.plist on the drive — which is where new versions land. Local only: no network. Nothing installs on its own;
// the update row offers the rebuild command (as GrokGauge offers its Homebrew command).
import AppKit
import Foundation

/// A dotted version like "0.9.0" or "v1.2"; pre-release suffixes sort before the release.
struct SemVer: Comparable, Equatable, CustomStringConvertible {
    let parts: [Int]
    let prerelease: String?

    init?(_ string: String) {
        var s = string.trimmingCharacters(in: .whitespaces)
        if s.hasPrefix("v") || s.hasPrefix("V") { s.removeFirst() }
        let main = s.split(separator: "-", maxSplits: 1).map(String.init)
        guard let core = main.first, !core.isEmpty else { return nil }
        let nums = core.split(separator: ".").map { Int($0) }
        guard !nums.isEmpty, nums.allSatisfy({ $0 != nil }) else { return nil }
        parts = nums.map { $0! }
        prerelease = main.count > 1 ? main[1] : nil
    }

    var description: String { parts.map(String.init).joined(separator: ".") + (prerelease.map { "-\($0)" } ?? "") }

    static func < (a: SemVer, b: SemVer) -> Bool {
        let n = max(a.parts.count, b.parts.count)
        for i in 0..<n {
            let x = i < a.parts.count ? a.parts[i] : 0
            let y = i < b.parts.count ? b.parts[i] : 0
            if x != y { return x < y }
        }
        switch (a.prerelease, b.prerelease) {
        case (nil, nil), (nil, _?): return false
        case (_?, nil): return true
        case (let p?, let q?): return p < q
        }
    }

    static func == (a: SemVer, b: SemVer) -> Bool { !(a < b) && !(b < a) }
}


final class AppUpdateMonitor: ObservableObject {
    static let shared = AppUpdateMonitor()
    static let lastCheckKey = "appUpdates.lastCheck"
    static let interval: TimeInterval = 24 * 3600

    @Published private(set) var available: String?
    @Published private(set) var lastChecked: Date?
    @Published private(set) var lastError: String?
    @Published private(set) var isChecking = false

    var root: () -> URL? = { nil }
    private let persist: Bool
    private var timer: Timer?
    private var enabled = false

    init(persist: Bool = true) {
        self.persist = persist
        if persist { lastChecked = UserDefaults.standard.object(forKey: Self.lastCheckKey) as? Date }
    }

    static func preview(available: String?, lastChecked: Date?) -> AppUpdateMonitor {
        let m = AppUpdateMonitor(persist: false)
        m.available = available
        m.lastChecked = lastChecked
        return m
    }

    func setEnabled(_ on: Bool) {
        guard persist else { return }
        enabled = on
        timer?.invalidate()
        timer = nil
        guard on else { available = nil; return }
        timer = Timer.scheduledTimer(withTimeInterval: 3600, repeats: true) { [weak self] _ in self?.checkIfDue() }
        timer?.tolerance = 600
        DispatchQueue.main.asyncAfter(deadline: .now() + 20) { [weak self] in self?.checkIfDue() }
    }

    func checkIfDue() {
        guard enabled else { return }
        if let last = lastChecked, Date().timeIntervalSince(last) < Self.interval { return }
        checkNow()
    }

    func checkNow() {
        guard persist, !isChecking else { return }
        isChecking = true
        let r = root()
        DispatchQueue.global(qos: .utility).async {
            let found = r.flatMap { Self.sourceVersion(root: $0) }
            DispatchQueue.main.async {
                if let r, found == nil {
                    self.lastError = FileManager.default.fileExists(atPath: r.path) ? "No ClipGauge source in the project folder" : "Project folder not connected"
                    self.available = nil
                } else if r == nil {
                    self.lastError = "Project folder not found"
                    self.available = nil
                } else {
                    self.lastError = nil
                    self.available = found.flatMap { Self.isNewer($0, than: AppInfo.version) ? $0 : nil }
                }
                self.lastChecked = Date()
                UserDefaults.standard.set(self.lastChecked, forKey: Self.lastCheckKey)
                self.isChecking = false
            }
        }
    }

    /// CFBundleShortVersionString of <project>/app/ClipGauge/Info.plist.
    static func sourceVersion(root: URL) -> String? {
        let u = root.appendingPathComponent("app/ClipGauge/Info.plist")
        guard let d = NSDictionary(contentsOf: u) else { return nil }
        return d["CFBundleShortVersionString"] as? String
    }

    static func isNewer(_ latest: String, than current: String) -> Bool {
        guard let l = SemVer(latest), let c = SemVer(current) else { return false }
        return l > c
    }

    static func buildCommand(root: URL?) -> String {
        let r = root?.path ?? "<project folder>"
        return "cd '\(r)' && zsh app/ClipGauge/build.sh"
    }

    func copyBuildCommand() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(Self.buildCommand(root: root()), forType: .string)
    }
}
