// About ClipGauge — GrokGauge's About tab: icon + name + version + one-line description, the retroCombs maker card
// (logo, YouTube, PayPal tip, contact), Updates, Links, then ClipGauge's grouped rows for the project, privacy and credits.
import AppKit
import SwiftUI

enum AppInfo {
    static var version: String { kVersion }
    static let youTubeHandle = "@retroCombs-Tech"
    static let youTubeURL = URL(string: "https://www.youtube.com/@retroCombs-Tech")!
    static let contactEmail = "retroCombs@icloud.com"
    static let contactURL = URL(string: "mailto:retroCombs@icloud.com")!
    /// Tips (PayPal.Me). Opens in the browser; ClipGauge sends nothing.
    static let tipURL = URL(string: "https://paypal.me/stevencombs")!
    static var makerLogo: NSImage? {
        Bundle.main.url(forResource: "retrocombs-logo", withExtension: "png").flatMap { NSImage(contentsOf: $0) }
    }
}

/// Settings › About (v0.7, GrokGauge's About tab): icon + name + version + one line, the maker card, Updates
/// (the app itself), Links, then ClipGauge's own This Mac / Privacy / Built with groups.
struct AboutTab: View {
    @ObservedObject var model: RenamerModel
    @ObservedObject var store: LayoutStore
    @ObservedObject var updates: AppUpdateMonitor
    var openSetup: () -> Void = {}
    var showDiagnostics: () -> Void = {}

    var body: some View {
        Group {
            HStack(spacing: 14) {
                Image(nsImage: NSApp.applicationIconImage ?? NSImage())
                    .resizable().frame(width: 64, height: 64).accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 3) {
                    Text(kAppName).font(.title2.weight(.semibold))
                    Text("Version \(AppInfo.version)").foregroundStyle(.secondary)
                    Text(verbatim: "The local AI video renamer in your menu bar: add clips, watch progress and ETA, review and undo renames, and ask the model about your footage. Clips are named {date}_{project}_{subject}_{type} on your own Mac.")
                        .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
            }
            MakerCard()
            PrefGroup(title: "Updates",
                      footer: "Compares this app with the ClipGauge source in your project folder (app/ClipGauge) — no network, no account. Nothing installs automatically. Models and tools have their own check in Setup › Updates.") {
                Toggle("Check for updates daily", isOn: $store.settings.checkForUpdates)
                    .padding(.vertical, 6)
                Divider()
                PrefRow(label: updateHeadline, detail: updateDetail) {
                    if updates.isChecking { ProgressView().controlSize(.small) }
                    Button("Check Now") { updates.checkNow() }.disabled(updates.isChecking)
                }
                if updates.available != nil {
                    Divider()
                    PrefRow(label: "Rebuild from the project", detail: "zsh app/ClipGauge/build.sh — quit ClipGauge first") {
                        Button("Copy Command") { updates.copyBuildCommand() }
                        if let r = model.root {
                            Button("Show Source") { NSWorkspace.shared.activateFileViewerSelecting([r.appendingPathComponent("app/ClipGauge")]) }
                        }
                    }
                }
            }
            PrefGroup(title: "Links") {
                PrefRow(label: "README & setup guide", detail: "In the project folder") {
                    Button("Open README") { if let r = model.root { NSWorkspace.shared.open(r.appendingPathComponent("README.md")) } }
                        .disabled(model.root == nil)
                }
                Divider()
                PrefRow(label: "Report a bug", detail: "Copy the report from the Diagnostics tab") {
                    Button("Diagnostics…", action: showDiagnostics)
                }
            }
            PrefGroup(title: "This Mac") {
                PrefRow(label: "Project folder", detail: model.root?.path ?? Project.missingTitle) {
                    if let r = model.root { Button("Reveal") { NSWorkspace.shared.activateFileViewerSelecting([r]) } }
                    Button("Setup…", action: openSetup)
                }
                Divider()
                PrefRow(label: "Engine", detail: "Runs the renamer scripts directly (scripts/clipgauge_cli.py) — no web server") {
                    Button("Open \(kAppName)") { model.showMain() }.disabled(!model.lexarOK)
                }
            }
            PrefGroup(title: "Privacy",
                      footer: "\(kAppName) and the renamer run entirely on this Mac. Frames, audio, transcripts and Ask the Model chats are processed by local models (Ollama, whisper.cpp) and never uploaded. Network use: checking for and downloading tools and models, which you approve. Settings sync (off until you choose a folder) writes only a small preferences file into that folder.") {
                PrefRow(label: "Network access", detail: "127.0.0.1 only (local Ollama)") {
                    Image(systemName: "lock.shield").foregroundStyle(Level.normal.color)
                }
            }
            PrefGroup(title: "Built with") {
                credit("Ollama", "Local model runner · MIT", "https://ollama.com")
                Divider()
                credit("Qwen2.5-VL", "Vision model by Alibaba Qwen · 7B: Apache 2.0 (3B: Qwen Research License)", "https://ollama.com/library/qwen2.5vl")
                Divider()
                credit("whisper.cpp + Whisper models", "Speech to text · MIT", "https://github.com/ggml-org/whisper.cpp")
                Divider()
                credit("FFmpeg", "Frame and audio extraction · LGPL/GPL", "https://ffmpeg.org")
            }
            HStack {
                Text("© 2026 Steven Combs (retroCombs). Unofficial; not affiliated with the model makers.")
                    .font(.caption2).foregroundStyle(.tertiary)
                Spacer()
            }
        }
    }

    private var updateHeadline: String {
        if let v = updates.available { return "Version \(v) is available" }
        if updates.lastError != nil { return "Couldn't check for updates" }
        return updates.lastChecked == nil ? "Not checked yet" : "\(kAppName) is up to date"
    }

    private var updateDetail: String? {
        if let e = updates.lastError { return e }
        guard let d = updates.lastChecked else { return nil }
        return "Last checked \(d.formatted(date: .abbreviated, time: .shortened))"
    }

    private func credit(_ name: String, _ detail: String, _ url: String) -> some View {
        PrefRow(label: name, detail: detail) {
            if let u = URL(string: url) { Link(u.host ?? url, destination: u).font(.caption) }
        }
    }
}

/// The About content on its own (used by --render-preview); the app opens Settings › About instead.
struct AboutView: View {
    @ObservedObject var model: RenamerModel
    var openSetup: () -> Void = {}

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            AboutTab(model: model, store: LayoutStore.shared, updates: AppUpdateMonitor.shared, openSetup: openSetup)
        }
        .padding(20)
        .frame(width: 560)
    }
}

/// The retroCombs logo, credit line, YouTube channel and contact address (same as GrokGauge's About › maker).
struct MakerCard: View {
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        HStack(alignment: .center, spacing: 18) {
            if let logo = AppInfo.makerLogo {
                Image(nsImage: logo)
                    .resizable()
                    .interpolation(.high)
                    .aspectRatio(1, contentMode: .fit)
                    .frame(width: 112, height: 112)
                    .clipShape(RoundedRectangle(cornerRadius: 22, style: .continuous))
                    .overlay(RoundedRectangle(cornerRadius: 22, style: .continuous)
                        .strokeBorder(Color.primary.opacity(scheme == .dark ? 0.16 : 0.08), lineWidth: 0.5))
                    .shadow(color: .black.opacity(scheme == .dark ? 0.45 : 0.18), radius: 8, x: 0, y: 4)
                    .accessibilityLabel("retroCombs logo")
            }
            VStack(alignment: .leading, spacing: 10) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Made by Steven Combs (retroCombs)").font(.headline)
                    Text("Videos, projects and updates on YouTube.").font(.caption).foregroundStyle(.secondary)
                }
                Button {
                    NSWorkspace.shared.open(AppInfo.youTubeURL)
                } label: {
                    Label("YouTube: \(AppInfo.youTubeHandle)", systemImage: "play.rectangle.fill")
                        .font(.system(size: 13, weight: .semibold))
                }
                .buttonStyle(YouTubeButtonStyle())
                .help(AppInfo.youTubeURL.absoluteString)
                .accessibilityLabel("Open the retroCombs-Tech YouTube channel")
                Button {
                    NSWorkspace.shared.open(AppInfo.tipURL)
                } label: {
                    Label("Support \(kAppName) · Tip via PayPal", systemImage: "heart.fill")
                        .font(.system(size: 12, weight: .semibold))
                }
                .buttonStyle(TipButtonStyle())
                .help(AppInfo.tipURL.absoluteString)
                .accessibilityLabel("Support \(kAppName): tip via PayPal")
                Link(destination: AppInfo.contactURL) {
                    Label("Contact: \(AppInfo.contactEmail)", systemImage: "envelope").font(.system(size: 12))
                }
            }
            Spacer(minLength: 0)
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.fill.quinary, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(.separator.opacity(0.6), lineWidth: 0.5))
    }
}

/// PayPal-blue capsule, same shape as the YouTube button but quieter (as in GrokGauge).
private struct TipButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .foregroundStyle(.white)
            .padding(.horizontal, 12)
            .padding(.vertical, 5)
            .background(Capsule().fill(Color(red: 0.0, green: 0.19, blue: 0.53).opacity(configuration.isPressed ? 0.75 : 1)))
            .shadow(color: .black.opacity(0.15), radius: 2, y: 1)
            .contentShape(Capsule())
    }
}

/// Always-red, always-prominent button (system prominent buttons turn gray in inactive windows).
private struct YouTubeButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .foregroundStyle(.white)
            .padding(.horizontal, 14)
            .padding(.vertical, 7)
            .background(Capsule().fill(Color(red: 0.85, green: 0.11, blue: 0.10).opacity(configuration.isPressed ? 0.75 : 1)))
            .shadow(color: .black.opacity(0.18), radius: 2, y: 1)
            .contentShape(Capsule())
    }
}

/// A plain titled window hosting a SwiftUI view (About / Setup). Released when closed.
final class HostedWindow: NSObject, NSWindowDelegate {
    private var window: NSWindow?
    private let title: String
    private let autosave: String
    private let make: () -> AnyView
    private let resizable: Bool
    private let minSize: NSSize

    init(title: String, autosave: String, resizable: Bool = false, minSize: NSSize = NSSize(width: 400, height: 300),
         make: @escaping () -> AnyView) {
        self.title = title
        self.autosave = autosave
        self.make = make
        self.resizable = resizable
        self.minSize = minSize
    }

    var isVisible: Bool { window?.isVisible == true }

    func show() {
        if window == nil {
            let host = NSHostingController(rootView: make())
            let w: NSWindow
            if resizable {
                host.sizingOptions = []
                w = NSWindow(contentViewController: host)
                w.styleMask = [.titled, .closable, .miniaturizable, .resizable]
                w.contentMinSize = minSize
                w.setContentSize(NSSize(width: max(minSize.width, 900), height: max(minSize.height, 640)))
            } else {
                host.sizingOptions = .preferredContentSize
                w = NSWindow(contentViewController: host)
                w.styleMask = [.titled, .closable, .miniaturizable]
            }
            w.title = title
            w.isReleasedWhenClosed = false
            w.delegate = self
            w.center()
            w.setFrameAutosaveName(autosave)
            window = w
        }
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }

    func windowWillClose(_ notification: Notification) {
        window?.contentViewController = nil
        window = nil
    }
}
