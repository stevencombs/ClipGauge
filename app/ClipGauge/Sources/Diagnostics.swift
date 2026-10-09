// Diagnostics (v0.7) — GrokGauge's Diagnostics tab: one group per source with Status / Last success / Last error,
// a "This Mac" group (version, macOS) and Copy Report for bug reports. ClipGauge's sources are the renamer engine,
// the local Ollama and the update checks. The report never includes clip names, file paths, notes or transcripts.
import AppKit
import SwiftUI

struct DiagnosticsReport {
    struct Row { let label: String; let value: String }
    struct Section { let name: String; let rows: [Row] }
    let appVersion: String
    let macOSVersion: String
    let architecture: String
    let sections: [Section]
    let settingsSummary: [String]

    var text: String {
        var lines = ["ClipGauge \(appVersion) · macOS \(macOSVersion) (\(architecture))", ""]
        for s in sections {
            lines.append("[\(s.name)]")
            for r in s.rows { lines.append("\(r.label): \(r.value)") }
            lines.append("")
        }
        lines.append("[Settings]")
        lines += settingsSummary
        return lines.joined(separator: "\n")
    }
}

enum Diagnostics {
    static func report(model m: RenamerModel, store: LayoutStore, updates: AppUpdateMonitor, ollama: String) -> DiagnosticsReport {
        let os = ProcessInfo.processInfo.operatingSystemVersion
        #if arch(arm64)
        let arch = "Apple silicon"
        #else
        let arch = "Intel"
        #endif
        let s = store.settings
        let engineOK = m.root.map { fileExists($0.appendingPathComponent("scripts/clipgauge_cli.py")) } ?? false
        let lastRun: String = {
            let st = m.state
            guard !st.isEmpty, st != "idle" else { return "None yet" }
            return st.replacingOccurrences(of: "_", with: " ")
        }()
        let problem: String = {
            guard let p = m.problem else { return "None" }
            if (p["resolved"] as? Bool) == true || !m.problemVisible { return "None (last error handled)" }
            return (p["reason"] as? String).map { redact(String($0.prefix(160))) } ?? "Last run had errors"
        }()
        let engine = DiagnosticsReport.Section(name: "Renamer engine", rows: [
            .init(label: "Project folder", value: m.root == nil ? "Not found" : (m.lexarOK ? "Connected" : "Drive not connected")),
            .init(label: "Engine", value: engineOK ? "OK (scripts/clipgauge_cli.py)" : "Missing"),
            .init(label: "Mode", value: m.dryRun ? "Dry run" : "Live"),
            .init(label: "Last run", value: lastRun),
            .init(label: "Last error", value: problem),
            .init(label: "DaVinci Resolve", value: m.resolveRunning ? "Open (processing paused)" : "Not running"),
        ])
        let models = DiagnosticsReport.Section(name: "Local models", rows: [
            .init(label: "Ollama", value: ollama),
            .init(label: "Vision model", value: m.visionModel),
            .init(label: "Models & tools", value: m.updates.checkedAt.map { "Checked \(stamp($0)) · \(m.updates.upgradable.count) update(s)" } ?? "Not checked yet"),
        ])
        let app = DiagnosticsReport.Section(name: "ClipGauge app", rows: [
            .init(label: "Update check", value: updates.available.map { "Version \($0) available" }
                  ?? (updates.lastChecked.map { "Up to date · checked \(stamp($0))" } ?? "Not checked yet")),
            .init(label: "Settings sync", value: store.syncFolder == nil ? "Off" : syncText(store.syncState)),
            .init(label: "Shortcut", value: s.hotKey.enabled ? (HotKeyCenter.shared.registrationFailed ? "\(s.hotKey.display) (refused by macOS)" : s.hotKey.display) : "Off"),
        ])
        let summary = [
            "Menu bar: \(s.menuBarMode.rawValue)\(s.showETA ? " + time left" : "")\(s.hideLogo ? ", no logo" : "")",
            "Popover: \(s.visibleSections.count)/\(PopoverSection.allCases.count) sections, buttons \(s.visibleActions.map(\.rawValue).joined(separator: ","))",
            "Colors: \(store.colorsName)",
            "Notifications: runs \(s.notifyRuns ? "on" : "off"), review \(s.notifyReview ? "on" : "off"), updates \(s.notifyUpdates ? "on" : "off")",
            "Update check: \(s.checkForUpdates ? "on" : "off")",
        ]
        return DiagnosticsReport(appVersion: AppInfo.version, macOSVersion: "\(os.majorVersion).\(os.minorVersion).\(os.patchVersion)",
                                 architecture: arch, sections: [engine, models, app], settingsSummary: summary)
    }

    /// Removes clip names and paths from an error line (the report must not carry them).
    static func redact(_ s: String) -> String {
        var t = s
        for pattern in [#"(/[^\s:]+)+"#, #"[^\s/:]+\.(?i:mp4|mov|m4v|mxf|mts|avi|mkv|heic|jpe?g|png|wav|mp3|braw|r3d)"#] {
            t = t.replacingOccurrences(of: pattern, with: "‹clip›", options: .regularExpression)
        }
        return t
    }

    static func syncText(_ s: LayoutStore.SyncState) -> String {
        switch s {
        case .off: return "Waiting…"
        case .synced(let d): return "In sync · checked \(d.formatted(date: .omitted, time: .shortened))"
        case .failed(let m): return "⚠︎ \(m)"
        }
    }

    static func stamp(_ d: Date) -> String {
        d.formatted(.dateTime.weekday(.abbreviated).month(.abbreviated).day().hour().minute())
    }

    /// Loopback only: asks the local Ollama for its version.
    static func probeOllama(_ done: @escaping (String) -> Void) {
        guard let url = URL(string: "http://127.0.0.1:11434/api/version") else { return done("Unknown") }
        var req = URLRequest(url: url)
        req.timeoutInterval = 3
        URLSession(configuration: .ephemeral).dataTask(with: req) { data, resp, _ in
            var text = "Not running (127.0.0.1:11434)"
            if (resp as? HTTPURLResponse)?.statusCode == 200, let data,
               let o = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                text = "Running · v\(o["version"] as? String ?? "?")"
            }
            DispatchQueue.main.async { done(text) }
        }.resume()
    }
}

struct DiagnosticsTab: View {
    @ObservedObject var model: RenamerModel
    @ObservedObject var store: LayoutStore
    @ObservedObject var updates: AppUpdateMonitor
    var ollamaOverride: String? = nil
    @ViewState var ollama = "Checking…"
    @ViewState var copied = false

    var body: some View {
        let report = Diagnostics.report(model: model, store: store, updates: updates, ollama: ollamaOverride ?? ollama)
        Group {
            ForEach(report.sections, id: \.name) { s in
                PrefGroup(title: s.name) {
                    ForEach(Array(s.rows.enumerated()), id: \.offset) { i, r in
                        if i > 0 { Divider() }
                        PrefRow(label: r.label) {
                            Text(r.value).foregroundStyle(.secondary).multilineTextAlignment(.trailing).lineLimit(2)
                        }
                    }
                }
            }
            PrefGroup(title: "This Mac", footer: "The report never includes clip names, file paths, notes, transcripts or chats.") {
                PrefRow(label: kAppName) { Text(report.appVersion).foregroundStyle(.secondary) }
                Divider()
                PrefRow(label: "macOS") { Text("\(report.macOSVersion) (\(report.architecture))").foregroundStyle(.secondary) }
                Divider()
                PrefRow(label: "Bug report") {
                    Button(copied ? "Copied" : "Copy Report") {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(report.text, forType: .string)
                        copied = true
                    }
                }
            }
        }
        .onAppear { if ollamaOverride == nil { Diagnostics.probeOllama { ollama = $0 } } }
    }
}
