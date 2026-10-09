// Custom instructions (v0.5): front end for scripts/instructions.py (config/custom-instructions.json).
//   Standing instructions  -> every clip & photo (vision describe/naming prompt, inside a delimited "user guidance" block
//                              that sits BEFORE the output format; the format + Ollama's JSON schema stay authoritative)
//   Next run only          -> the next run (cleared after a live run unless "Keep" is on); Sort into Projects reads a
//                              "Project: …" line from it
//   Glossary               -> whisper-cli --prompt (spelling) + the vision prompt
// Each clip's notes record the instructions used (hash + text). "Preview Prompt" shows exactly what will be sent.
import AppKit
import SwiftUI

/// Same limits as instructions.py LIMITS (the script re-checks on save).
enum InstructionLimits {
    static let standing = 2000
    static let nextRun = 1000
    static let glossaryTerms = 80
    static let glossaryTermChars = 40
    static let glossaryChars = 600
}

/// Popover indicator: read straight from config/custom-instructions.json on every poll (no Python).
struct InstructionsSummary: Equatable {
    var standing = false
    var nextRun = false
    var keep = false
    var glossaryTerms = 0
    var active: Bool { standing || nextRun || glossaryTerms > 0 }
    var detail: String {
        var p: [String] = []
        if standing { p.append("standing") }
        if nextRun { p.append(keep ? "next run (kept)" : "next run") }
        if glossaryTerms > 0 { p.append("\(glossaryTerms) glossary term\(glossaryTerms == 1 ? "" : "s")") }
        return p.isEmpty ? "None — Setup › Instructions…" : p.joined(separator: " · ")
    }

    static func read(root: URL) -> InstructionsSummary {
        guard let o = readJSONObject(root.appendingPathComponent("config/custom-instructions.json")) else { return .init() }
        let st = (o["standing"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let nr = o["next_run"] as? [String: Any] ?? [:]
        let nt = (nr["text"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let g = (o["glossary"] as? [Any])?.compactMap { ($0 as? String)?.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty } ?? []
        return InstructionsSummary(standing: !st.isEmpty, nextRun: !nt.isEmpty, keep: nr["keep"] as? Bool ?? false, glossaryTerms: g.count)
    }
}

final class InstructionsModel: ObservableObject {
    @Published var standing = ""
    @Published var nextRun = ""
    @Published var keep = false
    @Published var glossary = ""          // one term per line in the editor
    @Published var savedHash: String?
    @Published var updatedAt: String?
    @Published var lastNextRun: String?
    @Published var working = false
    @Published var error: String?
    @Published var note: String?
    @Published var previewPhoto = false
    @Published var preview: (system: String, user: String, whisper: String?, hash: String?, chars: Int, note: String)?
    private var saved: (String, String, Bool, String) = ("", "", false, "")

    weak var renamer: RenamerModel?
    var showPreview: () -> Void = {}
    init(renamer: RenamerModel?) { self.renamer = renamer }

    var dirty: Bool { (standing, nextRun, keep, glossary) != saved }
    var glossaryTerms: [String] {
        glossary.split(whereSeparator: { "\n,;".contains($0) }).map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
    }
    var glossaryChars: Int { glossaryTerms.reduce(0) { $0 + $1.count + 2 } }
    var longTerms: [String] { glossaryTerms.filter { $0.count > InstructionLimits.glossaryTermChars } }
    var overLimit: String? {
        if standing.count > InstructionLimits.standing { return "Standing instructions are over \(InstructionLimits.standing) characters." }
        if nextRun.count > InstructionLimits.nextRun { return "Next-run instructions are over \(InstructionLimits.nextRun) characters." }
        if glossaryTerms.count > InstructionLimits.glossaryTerms { return "The glossary has more than \(InstructionLimits.glossaryTerms) terms." }
        if glossaryChars > InstructionLimits.glossaryChars { return "The glossary is over \(InstructionLimits.glossaryChars) characters (Whisper's prompt limit)." }
        if let t = longTerms.first { return "“\(t.prefix(24))…” is longer than \(InstructionLimits.glossaryTermChars) characters." }
        return nil
    }
    var active: Bool {
        !standing.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !nextRun.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            || !glossaryTerms.isEmpty
    }
    var hasProjectLine: Bool {
        nextRun.split(separator: "\n").contains { $0.trimmingCharacters(in: .whitespaces).lowercased().range(of: #"^project\s*[:=]"#, options: .regularExpression) != nil }
    }

    func fill(_ o: [String: Any]) {
        standing = o["standing"] as? String ?? ""
        let nr = o["next_run"] as? [String: Any] ?? [:]
        nextRun = nr["text"] as? String ?? ""
        keep = nr["keep"] as? Bool ?? false
        glossary = (o["glossary"] as? [String] ?? []).joined(separator: "\n")
        savedHash = o["hash"] as? String
        updatedAt = o["updated_at"] as? String
        lastNextRun = (o["last_next_run"] as? [String: Any])?["text"] as? String
        saved = (standing, nextRun, keep, glossary)
    }

    private var draftJSON: [String: Any] {
        ["standing": standing, "glossary": glossaryTerms, "next_run": ["text": nextRun, "keep": keep]]
    }

    private func run(_ args: [String], draft: Bool, done: @escaping (Int32, [String: Any], String) -> Void) {
        guard let py = Project.python(), let r = renamer?.root else { done(127, [:], "python3 not found or no project folder"); return }
        let script = r.appendingPathComponent("scripts/instructions.py")
        guard fileExists(script) else {
            done(127, [:], "scripts/instructions.py is missing — update the project's scripts (ClipGauge v0.5 bundles it).")
            return
        }
        var extra: [String] = []
        var tmp: URL?
        if draft {
            let u = FileManager.default.temporaryDirectory.appendingPathComponent("clipgauge-instructions-\(UUID().uuidString).json")
            guard let d = try? JSONSerialization.data(withJSONObject: draftJSON), (try? d.write(to: u)) != nil else {
                done(126, [:], "Couldn't write a temporary file"); return
            }
            tmp = u
            extra = [u.path]
        }
        DispatchQueue.global(qos: .userInitiated).async {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: py)
            p.arguments = [script.path] + args + extra + ["--json"]
            p.currentDirectoryURL = r
            var env = ProcessInfo.processInfo.environment
            env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
            p.environment = env
            let out = Pipe(), err = Pipe()
            p.standardOutput = out
            p.standardError = err
            p.standardInput = FileHandle.nullDevice
            do { try p.run() } catch {
                if let t = tmp { try? FileManager.default.removeItem(at: t) }
                DispatchQueue.main.async { done(126, [:], error.localizedDescription) }
                return
            }
            let o = out.fileHandleForReading.readDataToEndOfFile()
            let e = err.fileHandleForReading.readDataToEndOfFile()
            p.waitUntilExit()
            if let t = tmp { try? FileManager.default.removeItem(at: t) }
            let last = String(decoding: o, as: UTF8.self).split(separator: "\n").last.map(String.init) ?? ""
            let obj = (try? JSONSerialization.jsonObject(with: Data(last.utf8))) as? [String: Any] ?? [:]
            DispatchQueue.main.async { done(p.terminationStatus, obj, String(decoding: e, as: UTF8.self)) }
        }
    }

    private func failText(_ code: Int32, _ o: [String: Any], _ err: String) -> String {
        (o["error"] as? String) ?? "instructions.py exit \(code): " + String(err.split(separator: "\n").suffix(3).joined(separator: " ").prefix(400))
    }

    func load() {
        error = nil
        run(["--show"], draft: false) { code, o, err in
            guard code == 0 else { self.error = self.failText(code, o, err); return }
            self.fill(o)
            self.renamer?.tick()
        }
    }

    func save() {
        guard !working, overLimit == nil else { return }
        working = true; error = nil; note = nil
        run(["--save-json"], draft: true) { code, o, err in
            self.working = false
            guard code == 0 else { self.error = self.failText(code, o, err); return }
            self.fill(o)
            self.note = (o["active"] as? Bool ?? false) ? "Saved — instructions active (hash \(self.savedHash ?? "—"))." : "Saved — no instructions active."
            self.renamer?.tick()
        }
    }

    func revert() { (standing, nextRun, keep, glossary) = saved; error = nil; note = nil }

    /// Builds the full prompt from the current (possibly unsaved) text and opens the Prompt Preview window.
    func runPreview(open: Bool = true, done: (() -> Void)? = nil) {
        guard overLimit == nil else { error = overLimit; return }
        working = true; error = nil
        run(["--preview"] + (previewPhoto ? ["--photo"] : []) + ["--draft"], draft: true) { code, o, err in
            self.working = false
            defer { done?() }
            guard code == 0 else { self.error = self.failText(code, o, err); return }
            let ch = o["chars"] as? [String: Any] ?? [:]
            self.preview = (o["system"] as? String ?? "", o["user"] as? String ?? "", o["whisper_prompt"] as? String,
                            o["hash"] as? String, (intVal(ch["system"]) ?? 0) + (intVal(ch["user"]) ?? 0), o["note"] as? String ?? "")
            if open { self.showPreview() }
        }
    }
}

struct InstructionsView: View {
    @ObservedObject var ins: InstructionsModel
    @ObservedObject var model: RenamerModel
    var scrolls = true

    var body: some View {
        if scrolls {
            ScrollView { content }.frame(width: 640).frame(minHeight: 420, idealHeight: 760, maxHeight: 960)
        } else {
            content.frame(width: 640)
        }
    }

    private var content: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(spacing: 12) {
                Image(systemName: "text.bubble").font(.system(size: 28)).foregroundStyle(.secondary)
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 8) {
                        Text("Custom Instructions").font(.system(.title3, design: .rounded).weight(.semibold))
                        badge
                    }
                    Text("Guidance for the vision model and Whisper — names, spellings, project context. It goes into a clearly marked block before the output format, so it can't change the JSON the renamer needs.")
                        .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
            }
            editor(title: "Standing instructions", hint: "Every clip and photo.", text: $ins.standing,
                   count: "\(ins.standing.count) / \(InstructionLimits.standing) characters", over: ins.standing.count > InstructionLimits.standing,
                   height: 110)
            VStack(alignment: .leading, spacing: 6) {
                editor(title: "Next run only", hint: "This batch. Also used by Sort into Projects (a “Project: …” line names the shoot).",
                       text: $ins.nextRun,
                       count: "\(ins.nextRun.count) / \(InstructionLimits.nextRun) characters" + (ins.hasProjectLine ? " · has a “Project:” line" : ""),
                       over: ins.nextRun.count > InstructionLimits.nextRun, height: 70)
                Toggle("Keep after the run", isOn: $ins.keep)
                    .toggleStyle(.checkbox).font(.callout)
                    .help("Off: cleared automatically after the next live run (dry runs never clear it). On: used for every run until you change it.")
                if let l = ins.lastNextRun, ins.nextRun.isEmpty {
                    Text("Last used: “\(l.prefix(80))\(l.count > 80 ? "…" : "")”").font(.caption).foregroundStyle(.secondary).lineLimit(1)
                }
            }
            editor(title: "Glossary", hint: "Names, products, places — one per line. Whisper's initial prompt (better spelling) and the vision prompt.",
                   text: $ins.glossary,
                   count: "\(ins.glossaryTerms.count) / \(InstructionLimits.glossaryTerms) terms · \(ins.glossaryChars) / \(InstructionLimits.glossaryChars) characters",
                   over: ins.glossaryTerms.count > InstructionLimits.glossaryTerms || ins.glossaryChars > InstructionLimits.glossaryChars || !ins.longTerms.isEmpty,
                   height: 96, mono: true)
            if let e = ins.error ?? ins.overLimit {
                Label(e, systemImage: "exclamationmark.triangle.fill").font(.callout).foregroundStyle(Level.critical.color)
                    .fixedSize(horizontal: false, vertical: true)
            } else if let n = ins.note {
                Label(n, systemImage: "checkmark.circle.fill").font(.callout).foregroundStyle(Level.normal.color)
            }
            HStack(spacing: 10) {
                Text(status).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                Spacer()
                if ins.working { ProgressView().controlSize(.small) }
                Toggle("As a photo", isOn: $ins.previewPhoto).toggleStyle(.checkbox).font(.caption)
                Button("Preview Prompt…") { ins.runPreview() }
                    .disabled(ins.working || ins.overLimit != nil || !model.lexarOK)
                    .help("Shows the full prompt (system + user + Whisper --prompt) for a sample clip, using the text above (saved or not)")
                Button("Revert") { ins.revert() }.disabled(!ins.dirty || ins.working)
                Button("Save") { ins.save() }
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut("s")
                    .disabled(!ins.dirty || ins.working || ins.overLimit != nil || !model.lexarOK)
            }
            Text("Saved in config/custom-instructions.json. Each clip's notes record the instructions used (hash + text). Pipeline flags: --instructions-file FILE, --no-instructions.")
                .font(.caption2).foregroundStyle(.tertiary).fixedSize(horizontal: false, vertical: true)
        }
        .padding(20)
    }

    private var badge: some View {
        let on = ins.active
        return Text(on ? "Instructions active" : "Off")
            .font(.caption2.weight(.semibold))
            .foregroundStyle(on ? Color.purple : .secondary)
            .padding(.horizontal, 8).padding(.vertical, 2)
            .background(on ? AnyShapeStyle(Color.purple.opacity(0.16)) : AnyShapeStyle(.fill.tertiary), in: Capsule())
    }

    private var status: String {
        if ins.dirty { return "Unsaved changes" }
        guard let u = ins.updatedAt else { return "Nothing saved yet" }
        let t = u.prefix(16).replacingOccurrences(of: "T", with: " ")
        return "Saved \(t)" + (ins.savedHash.map { " · hash \($0)" } ?? "")
    }

    private func editor(title: String, hint: String, text: Binding<String>, count: String, over: Bool, height: CGFloat,
                        mono: Bool = false) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text(title).font(.system(size: 13, weight: .semibold))
                Text(hint).font(.caption).foregroundStyle(.secondary).lineLimit(1)
            }
            TextEditor(text: text)
                .font(mono ? .system(.callout, design: .monospaced) : .callout)
                .scrollContentBackground(.hidden)
                .padding(6)
                .frame(height: height)
                .background(.fill.quinary, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .strokeBorder(over ? AnyShapeStyle(Level.critical.color) : AnyShapeStyle(.separator.opacity(0.6)), lineWidth: over ? 1 : 0.5))
            Text(count).font(.caption2).monospacedDigit().foregroundStyle(over ? Level.critical.color : .secondary)
        }
    }
}

struct PromptPreviewView: View {
    @ObservedObject var ins: InstructionsModel
    var scrolls = true

    var body: some View {
        if scrolls {
            ScrollView { content }.frame(width: 720).frame(minHeight: 400, idealHeight: 760, maxHeight: 980)
        } else {
            content.frame(width: 720)
        }
    }

    private var content: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 10) {
                Image(systemName: "doc.text.magnifyingglass").font(.system(size: 24)).foregroundStyle(.secondary)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Prompt Preview").font(.system(.title3, design: .rounded).weight(.semibold))
                    if let p = ins.preview {
                        Text("\(p.chars) characters\(p.hash.map { " · instructions \($0)" } ?? " · no instructions")\(ins.dirty ? " · unsaved draft" : "")")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
                Spacer()
                if let p = ins.preview {
                    Button("Copy") {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(fullText(p), forType: .string)
                    }
                }
            }
            if let p = ins.preview {
                Text(p.note).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                block("System", p.system)
                block("User (sent with the frames)", p.user, highlight: true)
                block("Whisper --prompt", p.whisper ?? "(none — the glossary is empty)")
            } else {
                Text("Click Preview Prompt… in the Instructions window.").foregroundStyle(.secondary)
            }
        }
        .padding(20)
    }

    private func fullText(_ p: (system: String, user: String, whisper: String?, hash: String?, chars: Int, note: String)) -> String {
        "=== SYSTEM ===\n\(p.system)\n\n=== USER ===\n\(p.user)\n=== WHISPER --prompt ===\n\(p.whisper ?? "(none)")"
    }

    private func block(_ title: String, _ text: String, highlight: Bool = false) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title.uppercased()).font(.caption2.weight(.semibold)).foregroundStyle(.secondary)
            Text(highlight ? highlighted(text) : AttributedString(text))
                .font(.system(size: 11, design: .monospaced))
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(10)
                .background(.fill.quinary, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
        }
    }

    /// Tints the delimited user-guidance block so it's easy to see where your text lands.
    private func highlighted(_ text: String) -> AttributedString {
        var a = AttributedString(text)
        if let s = a.range(of: "<<<USER_GUIDANCE"), let e = a.range(of: "USER_GUIDANCE>>>"), s.lowerBound < e.upperBound {
            a[s.lowerBound..<e.upperBound].foregroundColor = .purple
        }
        return a
    }
}
