// Settings › General › Models (v0.8.3): which vision model (Ollama) and speech model (Whisper) THIS Mac processes
// clips with. Saved per Mac in ~/Library/Application Support/ClipGauge/models.json — never on the project drive and
// never synced (Macs differ in memory). "" = Automatic (RAM tier, config/ram-tiers.json). The engine reads the same
// file (scripts/machine_prefs.py); a chosen model that isn't installed falls back to the tier.
import AppKit
import SwiftUI

final class MachineModels: ObservableObject {
    static let shared = MachineModels()
    @Published var vision = ""                 // "" = Automatic (RAM tier)
    @Published var whisper = ""
    @Published var installedVision: [String] = []
    @Published var installedWhisper: [String] = []
    @Published var usedVision: String?
    @Published var usedVisionSource: String?
    @Published var usedWhisper: String?
    @Published var usedWhisperSource: String?
    @Published var tier: String?
    @Published var loading = false
    @Published var error: String?

    static var file: URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSHomeDirectory() + "/Library/Application Support")
        return base.appendingPathComponent("ClipGauge/models.json")
    }

    init() { read() }

    func read() {
        let o = readJSONObject(MachineModels.file) ?? [:]
        vision = (o["vision_model"] as? String) ?? ""
        whisper = (o["whisper_model"] as? String) ?? ""
    }

    /// Writes the choice for this Mac (atomically), then asks the engine what it will actually use.
    func save(_ r: RenamerModel?) {
        var o: [String: Any] = ["vision_model": vision.isEmpty ? NSNull() : vision,
                                "whisper_model": whisper.isEmpty ? NSNull() : whisper,
                                "note": "ClipGauge Settings › General › Models — this Mac only, not synced. null = Automatic (RAM tier)."]
        o["updated_at"] = ISO8601DateFormatter().string(from: Date())
        do {
            try FileManager.default.createDirectory(at: MachineModels.file.deletingLastPathComponent(), withIntermediateDirectories: true)
            let d = try JSONSerialization.data(withJSONObject: o, options: [.prettyPrinted, .sortedKeys])
            try d.write(to: MachineModels.file, options: .atomic)
            error = nil
        } catch {
            self.error = "Couldn't save the model choice: \(error.localizedDescription)"
        }
        refresh(r)
    }

    func refresh(_ r: RenamerModel?) {
        guard let r, !ChatModel.previewMode else { return }
        loading = true
        r.engine(["models"]) { j in
            self.loading = false
            guard intVal(j["code"]) == 200 else { self.error = RenamerModel.errorText(j); return }
            self.installedVision = ((j["installed_models"] as? [String]) ?? []).filter { ChatModel.isRealName($0) }.sorted()
            self.installedWhisper = (j["whisper_models"] as? [String]) ?? []
            self.usedVision = j["vision_model"] as? String
            self.usedVisionSource = j["vision_source"] as? String
            self.usedWhisper = j["whisper_model"] as? String
            self.usedWhisperSource = j["whisper_source"] as? String
            self.tier = j["tier"] as? String
            if let v = self.usedVision, ChatModel.isRealName(v) { r.visionModel = v }
            if j["installed_models"] is NSNull { self.error = "Ollama isn't running, so installed vision models can't be listed." }
        }
    }

    /// Picker entries: the installed ones, plus the saved choice even if it's missing right now.
    var visionChoices: [String] { (installedVision + (vision.isEmpty || installedVision.contains(vision) ? [] : [vision])) }
    var whisperChoices: [String] { (installedWhisper + (whisper.isEmpty || installedWhisper.contains(whisper) ? [] : [whisper])) }

    func usedLine(_ name: String?, _ src: String?) -> String {
        guard let name else { return loading ? "Checking…" : "In use: —" }
        return "In use: \(name)" + (src.map { " · \($0)" } ?? "")
    }
}

/// The Models group shown in Settings › General.
struct ModelsPrefGroup: View {
    @ObservedObject var models: MachineModels = .shared
    @ObservedObject var model: RenamerModel
    @AppStorage(kKeepModelLoadedKey) private var keepLoaded = true   // v0.8.5, per Mac

    var body: some View {
        PrefGroup(title: "Models on this Mac",
                  footer: "Saved on this Mac only and never synced, since each Mac's memory differs. Automatic uses the "
                    + "RAM tier\(models.tier.map { " (\($0))" } ?? ""). A choice that isn't installed falls back to Automatic. "
                    + "Applies from the next clip; Setup shows what is in use." + (models.error.map { "\n⚠︎ \($0)" } ?? "")) {
            PrefRow(label: "Vision model (describes and names clips)",
                    detail: models.usedLine(models.usedVision, models.usedVisionSource)) {
                Picker("", selection: Binding(get: { models.vision }, set: { models.vision = $0; models.save(model) })) {
                    Text("Automatic (RAM tier)").tag("")
                    ForEach(models.visionChoices, id: \.self) { m in
                        Text(models.installedVision.contains(m) ? m : "\(m) (not installed)").tag(m)
                    }
                }
                .labelsHidden().frame(width: 230)
                .disabled(model.runActive)
                .help(model.runActive ? "Change it after the current run" : "Installed Ollama models")
            }
            Divider()
            PrefRow(label: "Speech model (Whisper transcripts)",
                    detail: models.usedLine(models.usedWhisper, models.usedWhisperSource)) {
                Picker("", selection: Binding(get: { models.whisper }, set: { models.whisper = $0; models.save(model) })) {
                    Text("Automatic (RAM tier)").tag("")
                    ForEach(models.whisperChoices, id: \.self) { m in
                        Text(models.installedWhisper.contains(m) ? m : "\(m) (not in models/whisper)").tag(m)
                    }
                }
                .labelsHidden().frame(width: 230)
                .disabled(model.runActive)
                .help(model.runActive ? "Change it after the current run" : "Models found in models/whisper")
            }
            Divider()
            PrefRow(label: "Keep model loaded while \(kAppName) is open",
                    detail: "Starts Ollama and keeps the chat model in memory, so answers start right away. Unloaded when "
                        + "\(kAppName) quits; paused while clips process.") {
                Toggle("", isOn: Binding(get: { keepLoaded }, set: { keepLoaded = $0; OllamaRuntime.shared.keepLoadedChanged() }))
                    .labelsHidden().toggleStyle(.switch)
            }
        }
        .onAppear { if !ChatModel.previewMode { models.read(); models.refresh(model) } }
    }
}
