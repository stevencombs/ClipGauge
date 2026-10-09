// ClipGauge Setup: pick/create a project folder and check the prerequisites (Homebrew, ffmpeg, Ollama, whisper.cpp,
// Python, model files, RAM tier). "Install missing…" opens scripts/setup-mac.sh in Terminal, where every install is
// shown and asked for first; ClipGauge itself never installs anything.
import AppKit
import SwiftUI

struct CheckItem: Identifiable {
    enum State { case ok, missing, warning, checking }
    let id: String
    let icon: String
    let title: String
    var state: State
    var detail: String
}

final class SetupModel: ObservableObject {
    @Published var items: [CheckItem] = []
    @Published var checking = false
    @Published var lastChecked: Date?
    @Published var createdMessage: String?
    let renamer: RenamerModel

    init(renamer: RenamerModel) { self.renamer = renamer }

    var root: URL? { renamer.root }
    var missingCount: Int { items.filter { $0.state == .missing }.count }
    var allGood: Bool { !items.isEmpty && missingCount == 0 }

    // MARK: checks

    func recheck(completion: (() -> Void)? = nil) {
        guard !checking else { return }
        checking = true
        renamer.tick()
        let root = renamer.root
        DispatchQueue.global(qos: .userInitiated).async {
            let items = SetupModel.runChecks(root: root)
            DispatchQueue.main.async {
                self.items = items
                self.checking = false
                self.lastChecked = Date()
                completion?()
            }
        }
    }

    /// Synchronous; call off the main thread.
    static func runChecks(root: URL?) -> [CheckItem] {
        var out: [CheckItem] = []
        let fm = FileManager.default
        func exe(_ cands: [String]) -> String? { cands.first { fm.isExecutableFile(atPath: $0) } }
        func run(_ path: String, _ args: [String], cwd: URL? = nil, timeout: TimeInterval = 20) -> (Int32, String) {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: path)
            p.arguments = args
            if let c = cwd { p.currentDirectoryURL = c }
            var env = ProcessInfo.processInfo.environment
            env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
            p.environment = env
            let pipe = Pipe()
            p.standardOutput = pipe
            p.standardError = pipe
            p.standardInput = FileHandle.nullDevice
            do { try p.run() } catch { return (-1, error.localizedDescription) }
            let deadline = Date().addingTimeInterval(timeout)
            while p.isRunning && Date() < deadline { usleep(50_000) }
            if p.isRunning { p.terminate(); return (-2, "timed out") }
            let s = String(decoding: pipe.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
            return (p.terminationStatus, s.trimmingCharacters(in: .whitespacesAndNewlines))
        }
        func http(_ url: String) -> Data? {
            guard let u = URL(string: url) else { return nil }
            var result: Data?
            let sem = DispatchSemaphore(value: 0)
            let cfg = URLSessionConfiguration.ephemeral
            cfg.connectionProxyDictionary = [:]
            URLSession(configuration: cfg).dataTask(with: URLRequest(url: u, timeoutInterval: 3)) { d, r, _ in
                if (r as? HTTPURLResponse)?.statusCode == 200 { result = d }
                sem.signal()
            }.resume()
            _ = sem.wait(timeout: .now() + 4)
            return result
        }

        // Project folder
        if let r = root {
            var detail = r.path
            if let v = try? r.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey, .volumeAvailableCapacityKey, .volumeLocalizedFormatDescriptionKey]) {
                // "Important usage" reads 0 on exFAT/external volumes; fall back to the plain available capacity.
                let important = v.volumeAvailableCapacityForImportantUsage ?? 0
                if let free = important > 0 ? important : v.volumeAvailableCapacity.map(Int64.init) {
                    detail += " · \(ByteCountFormatter.string(fromByteCount: free, countStyle: .file)) free"
                }
                if let f = v.volumeLocalizedFormatDescription { detail += " · \(f)" }
            }
            let writable = fm.isWritableFile(atPath: r.appendingPathComponent("logs").path)
            out.append(CheckItem(id: "root", icon: "folder", title: "Project folder", state: writable ? .ok : .warning,
                                 detail: writable ? detail : detail + " · logs/ is not writable"))
        } else {
            out.append(CheckItem(id: "root", icon: "folder", title: "Project folder", state: .missing,
                                 detail: Project.remembered.map { "\(Project.missingTitle): \($0)" } ?? "Choose an existing AI-Video-Renamer folder, or create a new one"))
        }

        // Homebrew
        let brew = exe(["/opt/homebrew/bin/brew", "/usr/local/bin/brew"])
        out.append(CheckItem(id: "brew", icon: "shippingbox", title: "Homebrew", state: brew != nil ? .ok : .missing,
                             detail: brew ?? "Installs the tools below — brew.sh"))

        // Python
        if let py = Project.python() {
            let (rc, v) = run(py, ["-c", "import sys;print(sys.version.split()[0]);sys.exit(0 if sys.version_info>=(3,10) else 1)"])
            out.append(CheckItem(id: "python", icon: "chevron.left.forwardslash.chevron.right", title: "Python 3.10+",
                                 state: rc == 0 ? .ok : .missing, detail: rc == 0 ? "\(v) · \(py)" : "Found \(v) at \(py); need 3.10 or newer"))
        } else {
            out.append(CheckItem(id: "python", icon: "chevron.left.forwardslash.chevron.right", title: "Python 3.10+", state: .missing, detail: "brew install python"))
        }

        // ffmpeg
        let cfg = root.flatMap { readJSONObject($0.appendingPathComponent("config/config.json")) } ?? [:]
        let ffmpeg = exe([(cfg["ffmpeg_path"] as? String) ?? "", "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"])
        let ffprobe = exe([(cfg["ffprobe_path"] as? String) ?? "", "/opt/homebrew/bin/ffprobe", "/usr/local/bin/ffprobe"])
        out.append(CheckItem(id: "ffmpeg", icon: "film", title: "ffmpeg + ffprobe", state: (ffmpeg != nil && ffprobe != nil) ? .ok : .missing,
                             detail: ffmpeg.map { "\($0)" } ?? "brew install ffmpeg"))

        // Ollama
        let ollama = exe(["/opt/homebrew/bin/ollama", "/usr/local/bin/ollama", "/Applications/Ollama.app/Contents/Resources/ollama"])
        let ver = http("http://127.0.0.1:11434/api/version").flatMap { (try? JSONSerialization.jsonObject(with: $0)) as? [String: Any] }?["version"] as? String
        var ollamaState: CheckItem.State = .missing
        var ollamaDetail = "brew install ollama"
        if let o = ollama {
            ollamaState = ver != nil ? .ok : .warning
            ollamaDetail = ver.map { "v\($0) running · \(o)" } ?? "Installed (\(o)) but not running"
        }
        out.append(CheckItem(id: "ollama", icon: "cpu", title: "Ollama", state: ollamaState, detail: ollamaDetail))

        // whisper.cpp
        let wcfg = cfg["whisper"] as? [String: Any]
        let whisper = exe([(wcfg?["cli_path"] as? String) ?? "", "/opt/homebrew/bin/whisper-cli", "/usr/local/bin/whisper-cli"])
        out.append(CheckItem(id: "whisper", icon: "waveform", title: "whisper.cpp", state: whisper != nil ? .ok : .missing,
                             detail: whisper ?? "brew install whisper-cpp"))

        // RAM tier + models
        var tier: [String: Any] = [:]
        if let r = root, let py = Project.python() {
            let (rc, js) = run(py, [r.appendingPathComponent("scripts/detect_ram.py").path, "--json"], cwd: r)
            if rc == 0, let d = js.data(using: .utf8), let o = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any] { tier = o }
        }
        if !tier.isEmpty {
            let gb = (tier["ram_gb_binary"] as? NSNumber)?.doubleValue ?? 0
            out.append(CheckItem(id: "tier", icon: "memorychip", title: "RAM tier", state: .ok,
                                 detail: "\(Int(gb.rounded())) GB → \((tier["label"] as? String) ?? (tier["tier"] as? String) ?? "?") · \((tier["prefer_model"] as? String) ?? "?") + whisper \((tier["whisper_model"] as? String) ?? "?")"))
        } else {
            let gb = Double(ProcessInfo.processInfo.physicalMemory) / 1_073_741_824
            out.append(CheckItem(id: "tier", icon: "memorychip", title: "RAM tier", state: .warning,
                                 detail: "\(Int(gb.rounded())) GB RAM · tier is chosen once a project folder and Python are ready"))
        }
        if let r = root {
            let models = r.appendingPathComponent("models")
            if let vlm = tier["prefer_model"] as? String {
                let parts = vlm.split(separator: ":", maxSplits: 1).map(String.init)
                let name = parts.first ?? vlm, tag = parts.count > 1 ? parts[1] : "latest"
                // Ollama ≤ 0.39 kept manifests/registry.ollama.ai/…; 0.40 moved tags to manifests-v2/ollama.com/… (symlinks)
                let onDisk = ["manifests-v2/ollama.com", "manifests-v2/registry.ollama.ai", "manifests/registry.ollama.ai"]
                    .contains { fileExists(models.appendingPathComponent("\($0)/library/\(name)/\(tag)")) }
                let tags = http("http://127.0.0.1:11434/api/tags").flatMap { (try? JSONSerialization.jsonObject(with: $0)) as? [String: Any] }
                let served = ((tags?["models"] as? [[String: Any]]) ?? []).contains { ($0["name"] as? String) == vlm }
                let state: CheckItem.State = onDisk && (served || ver == nil) ? (served ? .ok : .warning) : (served ? .warning : .missing)
                var detail = onDisk ? "In \(models.path)" : "Not in \(models.path) (several GB)"
                if onDisk && !served && ver != nil { detail += " · Ollama isn't serving this folder (set OLLAMA_MODELS)" }
                if !onDisk && served { detail = "Ollama has it, but stored outside the project folder" }
                if onDisk && served { detail += " · served by Ollama" }
                out.append(CheckItem(id: "vlm", icon: "eye", title: "Vision model \(vlm)", state: state, detail: detail))
            }
            if let wm = tier["whisper_model"] as? String {
                let f = models.appendingPathComponent("whisper/ggml-\(wm).bin")
                let size = ((try? fm.attributesOfItem(atPath: f.path))?[.size] as? NSNumber)?.int64Value ?? 0
                out.append(CheckItem(id: "wmodel", icon: "text.bubble", title: "Speech model \(wm)", state: size > 0 ? .ok : .missing,
                                     detail: size > 0 ? "\(f.lastPathComponent) · \(ByteCountFormatter.string(fromByteCount: size, countStyle: .file))"
                                                      : "Not in \(models.appendingPathComponent("whisper").path)"))
            }
        }
        return out
    }

    // MARK: project folder

    func chooseFolder() {
        NSApp.activate(ignoringOtherApps: true)
        let panel = NSOpenPanel()
        panel.title = "Choose or create a project folder"
        panel.message = "Pick an existing AI-Video-Renamer folder, or any folder (a drive works too) to create a new project in."
        panel.prompt = "Use Folder"
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        panel.allowsMultipleSelection = false
        guard panel.runModal() == .OK, let u = panel.url else { return }
        if Project.isProject(u) {
            useProject(u, message: "Using \(u.path)")
            return
        }
        let nested = u.appendingPathComponent("AI-Video-Renamer")
        if Project.isProject(nested) {
            useProject(nested, message: "Using \(nested.path)")
            return
        }
        let target = u.lastPathComponent == "AI-Video-Renamer" ? u : nested
        let a = NSAlert()
        a.messageText = "Create a new project?"
        a.informativeText = "ClipGauge will create \(target.path) with the renamer scripts, an inbox/ folder and a starter config (dry-run on, so nothing is renamed until you turn it off).\n\nNothing is installed and no models are downloaded — Setup checks those next."
        a.addButton(withTitle: "Create Project")
        a.addButton(withTitle: "Cancel")
        guard a.runModal() == .alertFirstButtonReturn else { return }
        do {
            let n = try SetupModel.createProject(at: target)
            useProject(target, message: "Created \(target.path) (\(n) files copied)")
        } catch {
            renamer.alert("Couldn't create the project", error.localizedDescription)
        }
    }

    private func useProject(_ u: URL, message: String) {
        Project.remember(u)
        createdMessage = message
        renamer.tick()
        recheck()
    }

    /// Copy the bundled engine into `target` without overwriting anything; create the working folders.
    static func createProject(at target: URL) throws -> Int {
        guard let engine = Project.bundledEngine else {
            throw NSError(domain: "ClipGauge", code: 1, userInfo: [NSLocalizedDescriptionKey: "This copy of ClipGauge has no bundled scripts (Contents/Resources/engine). Rebuild it with app/ClipGauge/build.sh."])
        }
        let fm = FileManager.default
        try fm.createDirectory(at: target, withIntermediateDirectories: true)
        var copied = 0
        let en = fm.enumerator(at: engine, includingPropertiesForKeys: [.isDirectoryKey], options: [.skipsHiddenFiles])
        while let src = en?.nextObject() as? URL {
            let rel = String(src.path.dropFirst(engine.path.count + 1))
            let dst = target.appendingPathComponent(rel)
            let isDir = (try? src.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) ?? false
            if isDir {
                try fm.createDirectory(at: dst, withIntermediateDirectories: true)
            } else if !fm.fileExists(atPath: dst.path) {  // never overwrite
                try fm.copyItem(at: src, to: dst)
                copied += 1
            }
        }
        for d in ["inbox", "logs", "processing", "notes", "exports", "models/whisper", "needs-review", "done"] {
            try fm.createDirectory(at: target.appendingPathComponent(d), withIntermediateDirectories: true)
        }
        for f in ["scripts/setup-mac.sh", "scripts/setup-whisper.sh", "scripts/setup-ollama-env.sh",
                  "Start Renamer UI.command", "Stop Renamer UI.command"] {
            try? fm.setAttributes([.posixPermissions: 0o755], ofItemAtPath: target.appendingPathComponent(f).path)
        }
        return copied
    }

    // MARK: install / guide

    /// Opens scripts/setup-mac.sh in Terminal — you see and answer every step there.
    func installMissing() {
        guard let base = root ?? Project.bundledEngine else {
            renamer.alert("Choose a project folder first", "Setup needs a project folder before it can install anything.")
            return
        }
        let script = base.appendingPathComponent("scripts/setup-mac.sh")
        guard fileExists(script) else {
            renamer.alert("Setup script missing", "\(script.path) wasn't found. Create the project from ClipGauge › Setup, or copy scripts/setup-mac.sh from the app bundle.")
            return
        }
        let dir = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0].appendingPathComponent("ClipGauge")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let cmd = dir.appendingPathComponent("ClipGauge Setup.command")
        func q(_ s: String) -> String { "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'" }
        let body = "#!/bin/zsh\n# Opened by ClipGauge › Setup › Install missing. Every step asks before it installs anything.\nclear\nexec /bin/zsh \(q(script.path)) --root \(q((root ?? base).path))\n"
        do {
            try body.write(to: cmd, atomically: true, encoding: .utf8)
            try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: cmd.path)
        } catch {
            renamer.alert("Couldn't prepare the setup script", error.localizedDescription)
            return
        }
        let term = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.apple.Terminal")
        if let t = term {
            NSWorkspace.shared.open([cmd], withApplicationAt: t, configuration: NSWorkspace.OpenConfiguration()) { _, err in
                if let e = err { DispatchQueue.main.async { self.renamer.alert("Couldn't open Terminal", e.localizedDescription) } }
            }
        } else {
            NSWorkspace.shared.open(cmd)
        }
    }

    func openGuide() {
        let cands = [root?.appendingPathComponent("SETUP.md"), Bundle.main.resourceURL?.appendingPathComponent("SETUP.md")].compactMap { $0 }
        if let g = cands.first(where: fileExists) { NSWorkspace.shared.open(g) }
        else { renamer.alert("Setup guide not found", "SETUP.md isn't in the project folder or the app bundle.") }
    }
}

// MARK: - view

struct SetupView: View {
    @ObservedObject var setup: SetupModel
    @ObservedObject var model: RenamerModel
    /// false for offscreen previews (renders the whole content without a scroll view)
    var scrolls = true

    var body: some View {
        if scrolls {
            ScrollView { content }
                .frame(width: 580)
                .frame(minHeight: 480, idealHeight: 800, maxHeight: 900)
        } else {
            content
        }
    }

    private var content: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(spacing: 14) {
                Image(nsImage: NSApp.applicationIconImage ?? NSImage())
                    .resizable().frame(width: 56, height: 56).accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 3) {
                    Text("Set up ClipGauge").font(.title2.weight(.semibold))
                    Text("Everything runs on this Mac. ClipGauge only checks; installs happen in Terminal, where you approve each step.")
                        .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
            }

            PrefGroup(title: "Project folder",
                      footer: "Holds the scripts, inbox/, logs/ and the AI models. It can live on an external drive (e.g. the Lexar) and moves between Macs with it.") {
                PrefRow(label: model.root?.lastPathComponent ?? Project.missingTitle,
                        detail: model.root?.path ?? Project.remembered ?? "None chosen yet") {
                    if let r = model.root {
                        Button("Reveal") { NSWorkspace.shared.activateFileViewerSelecting([r]) }
                    }
                    Button(model.root == nil ? "Choose or Create…" : "Change…") { setup.chooseFolder() }
                }
                if let m = setup.createdMessage {
                    Divider()
                    Text(m).font(.caption).foregroundStyle(.secondary).padding(.vertical, 6)
                }
            }

            PrefGroup(title: "Requirements",
                      footer: setup.lastChecked.map { "Checked \(clockString($0, zone: false)). " + (setup.allGood ? "All set." : "\(setup.missingCount) missing.") }) {
                if setup.items.isEmpty {
                    HStack { ProgressView().controlSize(.small); Text("Checking…").foregroundStyle(.secondary) }.padding(.vertical, 8)
                }
                ForEach(Array(setup.items.enumerated()), id: \.element.id) { i, item in
                    if i > 0 { Divider() }
                    CheckRow(item: item)
                }
            }

            HStack(spacing: 8) {
                Button { setup.recheck() } label: {
                    HStack(spacing: 4) {
                        if setup.checking { ProgressView().controlSize(.mini) } else { Image(systemName: "arrow.clockwise") }
                        Text("Re-check")
                    }
                }
                .disabled(setup.checking)
                Button("Open Setup Guide") { setup.openGuide() }
                Spacer()
                Button("Install Missing…") { setup.installMissing() }
                    .buttonStyle(.borderedProminent)
                    .disabled(setup.items.isEmpty || setup.allGood)
                    .help("Opens scripts/setup-mac.sh in Terminal. It asks before every install or download.")
            }
            .controlSize(.large)

            UpdatesSection(updates: model.updates, model: model)
                .id("updates")
        }
        .padding(20)
        .frame(width: 560)
    }
}

private struct CheckRow: View {
    let item: CheckItem
    var body: some View {
        HStack(alignment: .center, spacing: 10) {
            Image(systemName: item.icon).frame(width: 20).foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 1) {
                Text(item.title)
                Text(item.detail).font(.caption).foregroundStyle(.secondary).lineLimit(2).truncationMode(.middle)
            }
            Spacer(minLength: 8)
            badge
        }
        .padding(.vertical, 6)
        .accessibilityElement(children: .combine)
    }

    @ViewBuilder private var badge: some View {
        switch item.state {
        case .ok: Image(systemName: "checkmark.circle.fill").foregroundStyle(Level.normal.color).font(.title3)
        case .missing: Image(systemName: "xmark.circle.fill").foregroundStyle(Level.critical.color).font(.title3)
        case .warning: Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(Level.warning.color).font(.title3)
        case .checking: ProgressView().controlSize(.small)
        }
    }
}

// MARK: - GrokGauge-style grouped rows (same metrics as GrokGauge's PrefGroup / PrefRow)

struct PrefGroup<Content: View>: View {
    var title: String?
    var footer: String?
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let title {
                Text(title).font(.system(size: 13, weight: .semibold)).padding(.leading, 2).accessibilityAddTraits(.isHeader)
            }
            VStack(alignment: .leading, spacing: 0) { content }
                .padding(.horizontal, 12)
                .padding(.vertical, 6)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(.fill.quinary, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(.separator.opacity(0.6), lineWidth: 0.5))
            if let footer {
                Text(.init(footer)).font(.caption).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true).padding(.horizontal, 2)
            }
        }
    }
}

struct PrefRow<Trailing: View>: View {
    let label: String
    var detail: String? = nil
    @ViewBuilder var trailing: Trailing

    var body: some View {
        HStack(alignment: .center, spacing: 10) {
            VStack(alignment: .leading, spacing: 1) {
                Text(label)
                if let detail { Text(detail).font(.caption).foregroundStyle(.secondary).lineLimit(2).truncationMode(.middle) }
            }
            Spacer(minLength: 8)
            trailing
        }
        .padding(.vertical, 7)
    }
}
