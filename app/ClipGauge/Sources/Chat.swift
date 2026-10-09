// Ask the Model (v0.6): a chat with the local Ollama (POST /api/chat, streamed NDJSON) — loopback only, nothing
// leaves the Mac. History + follow-ups, a model picker (installed models; default = the tier's vision model), an
// image or a clip's notes/transcript as context, copy, save as Markdown into exports/chats/, new chat, stop.
// Blocked while a processing run or an update is active (the Air has 16 GB: one model at a time).
import AppKit
import SwiftUI
import UniformTypeIdentifiers

struct ChatMessage: Identifiable {
    let id = UUID()
    let role: String            // user | assistant | context
    var text: String
    var images: [Data] = []     // JPEG, sent base64
    var label: String? = nil    // "Image: x.jpg" / "Notes: clip.mp4"
    var error = false
}

/// Streams /api/chat lines to the main thread.
final class ChatStreamer: NSObject, URLSessionDataDelegate {
    var onLine: ([String: Any]) -> Void = { _ in }
    var onDone: (String?) -> Void = { _ in }
    private var buffer = Data()
    private var status = 200

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse,
                    completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        status = (response as? HTTPURLResponse)?.statusCode ?? 0
        completionHandler(.allow)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        buffer.append(data)
        while let nl = buffer.firstIndex(of: 0x0A) {
            let line = buffer[buffer.startIndex..<nl]
            buffer.removeSubrange(buffer.startIndex...nl)
            if let j = (try? JSONSerialization.jsonObject(with: Data(line))) as? [String: Any] {
                DispatchQueue.main.async { self.onLine(j) }
            }
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        if !buffer.isEmpty, let j = (try? JSONSerialization.jsonObject(with: buffer)) as? [String: Any] {
            DispatchQueue.main.async { self.onLine(j) }
        }
        var msg: String? = nil
        if let e = error as NSError? {
            msg = e.code == NSURLErrorCancelled ? nil : "Ollama didn't answer: \(e.localizedDescription). Is it running? (Setup shows its state.)"
        } else if status != 200 {
            msg = "Ollama answered HTTP \(status) — the request was refused."
        }
        DispatchQueue.main.async { self.onDone(msg) }
        session.finishTasksAndInvalidate()
    }
}

final class ChatModel: ObservableObject {
    weak var renamer: RenamerModel?
    @Published var messages: [ChatMessage] = []
    @Published var input = ""
    @Published var models: [String] = []
    @Published var model = ""
    @Published var streaming = false
    @Published var error: String?
    @Published var pendingImage: (name: String, data: Data)?
    @Published var pendingNotes: (name: String, text: String)?
    @Published var clips: [(name: String, id: String)] = []
    @Published var savedTo: String?
    private var task: URLSessionDataTask?
    private var started = Date()

    init(renamer: RenamerModel) { self.renamer = renamer }

    /// Loopback Ollama URL from config (describe.ollama_url); anything that isn't this Mac is refused.
    var baseURL: URL? {
        var s = "http://127.0.0.1:11434"
        if let r = renamer?.root, let cfg = readJSONObject(r.appendingPathComponent("config/config.json")),
           let u = (cfg["describe"] as? [String: Any])?["ollama_url"] as? String, !u.isEmpty { s = u }
        guard let u = URL(string: s), let h = u.host, ["127.0.0.1", "localhost", "::1"].contains(h) else { return nil }
        return u
    }

    /// Why sending is blocked right now (nil = OK).
    var blocker: String? {
        guard let r = renamer else { return "Not ready." }
        if r.runActive { return "A processing run is active — Ask the Model is paused so the run has the memory (16 GB Air). It's available again when the run finishes." }
        if r.updates.jobActive { return "Models/tools are being updated — Ask the Model is available again when the update finishes." }
        if baseURL == nil { return "config describe.ollama_url isn't on this Mac — \(kAppName) only talks to a local Ollama." }
        return nil
    }

    /// The configured model (config describe.model), else the tier's vision model, else qwen2.5vl:7b.
    var defaultModel: String {
        if let r = renamer?.root, let cfg = readJSONObject(r.appendingPathComponent("config/config.json")),
           let m = (cfg["describe"] as? [String: Any])?["model"] as? String, ChatModel.isRealName(m) { return m }
        if let v = renamer?.visionModel, ChatModel.isRealName(v) { return v }
        return "qwen2.5vl:7b"
    }

    private static let shadow = try! NSRegularExpression(pattern: "^(llamacpp|ggml|mlx|ollama):[0-9a-f]{64}$")

    /// A name a person would pick: not empty, not Ollama 0.40's internal rollback shadow ("llamacpp:<64-hex digest>",
    /// ollama/ollama#18830), not a raw digest tag, not an update backup tag, not an embedding model.
    static func isRealName(_ n: String) -> Bool {
        let t = n.trimmingCharacters(in: .whitespaces)
        guard !t.isEmpty, t == n else { return false }
        if shadow.firstMatch(in: n, range: NSRange(n.startIndex..., in: n)) != nil { return false }
        let tag = n.split(separator: ":", maxSplits: 1).dropFirst().first.map(String.init) ?? ""
        if tag.count == 64 && tag.allSatisfy({ $0.isHexDigit }) { return false }
        for suf in ["-clipgauge-prev", "-clipguage-prev"] where n.hasSuffix(suf) { return false }
        return !n.lowercased().contains("embed")
    }

    /// /api/tags rows → unique, chat-capable model names (Ollama 0.40 lists every runner variant as its own row).
    static func chatModels(_ rows: [[String: Any]]) -> [String] {
        var out: [String] = []
        for d in rows {
            guard let n = (d["name"] as? String) ?? (d["model"] as? String), isRealName(n), !out.contains(n) else { continue }
            if let caps = d["capabilities"] as? [String], !caps.isEmpty, !caps.contains("completion") { continue }
            out.append(n)
        }
        return out.sorted()
    }

    private(set) var modelsLoaded = false

    func loadModels(then: (() -> Void)? = nil) {
        guard let b = baseURL else { then?(); return }
        var req = URLRequest(url: b.appendingPathComponent("api/tags"), timeoutInterval: 5)
        req.httpMethod = "GET"
        URLSession(configuration: .ephemeral).dataTask(with: req) { data, _, err in
            let o = data.flatMap { try? JSONSerialization.jsonObject(with: $0) } as? [String: Any]
            let names = ChatModel.chatModels((o?["models"] as? [[String: Any]]) ?? [])
            DispatchQueue.main.async {
                let def = self.defaultModel
                // default first, the rest alphabetical
                self.models = names.contains(def) ? [def] + names.filter { $0 != def } : names
                if !names.isEmpty { self.modelsLoaded = true }
                if !ChatModel.isRealName(self.model) || (!names.isEmpty && !names.contains(self.model)) {
                    self.model = names.contains(def) || names.isEmpty ? def : names[0]
                }
                if err != nil && names.isEmpty { self.error = "Ollama isn't answering on \(self.baseURL?.absoluteString ?? "127.0.0.1:11434"). Start it (Setup shows how), then try again." }
                then?()
            }
        }.resume()
    }

    /// Friendly text for Ollama's chat errors.
    func friendly(_ raw: String) -> String {
        let l = raw.lowercased()
        if l.contains("model is required") {
            return "No model was selected — pick one in the Model menu (default: \(defaultModel)) and send again."
        }
        if l.contains("not found") && (l.contains("model") || l.contains("pull")) {
            return "The model “\(model)” isn't installed in this project's Ollama — pick another in the Model menu (default: \(defaultModel))."
        }
        return raw
    }

    func loadClips() {
        renamer?.engine(["results", "--limit", "40"]) { j in
            let items = (j["items"] as? [[String: Any]]) ?? []
            self.clips = items.compactMap { d in
                guard let id = d["note_id"] as? String, !id.isEmpty else { return nil }
                let cur = (d["current"] as? String).map { ($0 as NSString).lastPathComponent } ?? (d["name"] as? String ?? id)
                return (cur, id)
            }
        }
    }

    func attachNotes(_ clip: (name: String, id: String)) {
        renamer?.engine(["note", clip.id]) { j in
            if let md = j["markdown"] as? String { self.pendingNotes = (clip.name, String(md.prefix(24_000))) }
            else { self.error = (j["error"] as? String) ?? "No notes for that clip." }
        }
    }

    func attachImage() {
        let p = NSOpenPanel()
        p.allowedContentTypes = [.image]
        p.allowsMultipleSelection = false
        p.message = "Choose an image to ask about (it stays on this Mac)."
        NSApp.activate(ignoringOtherApps: true)
        guard p.runModal() == .OK, let u = p.url else { return }
        attachImage(url: u)
    }

    func attachImage(url u: URL) {
        guard let img = NSImage(contentsOf: u), let jpg = Self.jpeg(img, maxSide: 1280) else { error = "Couldn't read that image."; return }
        pendingImage = (u.lastPathComponent, jpg)
    }

    static func jpeg(_ img: NSImage, maxSide: CGFloat) -> Data? {
        guard let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else { return nil }
        let w = CGFloat(cg.width), h = CGFloat(cg.height)
        let s = min(1, maxSide / max(w, h))
        let size = NSSize(width: Int(w * s), height: Int(h * s))
        guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size.width), pixelsHigh: Int(size.height),
                                         bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                         colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0) else { return nil }
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
        NSGraphicsContext.current?.cgContext.draw(cg, in: CGRect(origin: .zero, size: size))
        NSGraphicsContext.restoreGraphicsState()
        return rep.representation(using: .jpeg, properties: [.compressionFactor: 0.85])
    }

    func newChat() {
        stop()
        messages.removeAll(); input = ""; error = nil; pendingImage = nil; pendingNotes = nil; savedTo = nil
        started = Date()
    }

    func send(_ textIn: String? = nil, done: (() -> Void)? = nil) {
        let text = (textIn ?? input).trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !streaming else { return }
        if let b = blocker { error = b; return }
        guard let base = baseURL else { return }
        // v0.6.1: the popover's Ask box could send before the model list had loaded → "model is required" (HTTP 400).
        if !modelsLoaded || !ChatModel.isRealName(model) || !models.contains(model) {
            let pending = textIn ?? input
            loadModels { [weak self] in
                guard let self else { return }
                if self.modelsLoaded && ChatModel.isRealName(self.model) && self.models.contains(self.model) {
                    self.send(pending, done: done)
                } else {
                    self.input = pending
                    self.error = self.models.isEmpty
                        ? "No chat model is available from Ollama yet (expected \(self.defaultModel)). Check Setup, then try again."
                        : "Pick a model in the Model menu (default: \(self.defaultModel)), then send again."
                    done?()
                }
            }
            return
        }
        guard ChatModel.isRealName(model) else { error = "Pick a model in the Model menu (default: \(defaultModel))."; done?(); return }
        error = nil
        if let n = pendingNotes {
            messages.append(ChatMessage(role: "context", text: "Notes and transcript for the clip \(n.name):\n\n\(n.text)", label: "Notes: \(n.name)"))
            pendingNotes = nil
        }
        var m = ChatMessage(role: "user", text: text)
        if let i = pendingImage { m.images = [i.data]; m.label = "Image: \(i.name)"; pendingImage = nil }
        messages.append(m)
        input = ""
        messages.append(ChatMessage(role: "assistant", text: ""))
        let idx = messages.count - 1
        var payload: [[String: Any]] = [["role": "system", "content":
            "You are the assistant inside \(kAppName), a local video-renaming app for video creators. Answer concisely. When notes or a transcript are given, base answers on them."]]
        for msg in messages.dropLast() {
            var d: [String: Any] = ["role": msg.role == "context" ? "user" : msg.role, "content": msg.text]
            if !msg.images.isEmpty { d["images"] = msg.images.map { $0.base64EncodedString() } }
            if !msg.error { payload.append(d) }
        }
        let isDefault = model == defaultModel
        let body: [String: Any] = ["model": model, "messages": payload, "stream": true,
                                   "keep_alive": isDefault ? "5m" : "30s", "options": ["num_ctx": 8192]]
        var req = URLRequest(url: base.appendingPathComponent("api/chat"), timeoutInterval: 300)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        let streamer = ChatStreamer()
        streamer.onLine = { [weak self] j in
            guard let self, idx < self.messages.count else { return }
            if let e = j["error"] as? String { self.messages[idx].text += (self.messages[idx].text.isEmpty ? "" : "\n") + "⚠️ " + self.friendly(e); self.messages[idx].error = true }
            if let c = (j["message"] as? [String: Any])?["content"] as? String { self.messages[idx].text += c }
        }
        streamer.onDone = { [weak self] err in
            guard let self else { return }
            self.streaming = false
            self.task = nil
            if let err, idx < self.messages.count {
                // Ollama already explained an HTTP error in an {"error": …} line: don't add a bare "HTTP 400" under it
                if !(self.messages[idx].error && err.hasPrefix("Ollama answered HTTP")) {
                    self.messages[idx].text += (self.messages[idx].text.isEmpty ? "" : "\n\n") + "⚠️ " + err
                }
                self.messages[idx].error = true
            }
            done?()
        }
        let cfg = URLSessionConfiguration.ephemeral
        cfg.connectionProxyDictionary = [:]   // never via a proxy
        let session = URLSession(configuration: cfg, delegate: streamer, delegateQueue: nil)
        streaming = true
        task = session.dataTask(with: req)
        task?.resume()
    }

    func stop() {
        task?.cancel()
        task = nil
        if streaming, let i = messages.indices.last, messages[i].role == "assistant" { messages[i].text += " … (stopped)" }
        streaming = false
    }

    func copy(_ m: ChatMessage) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(m.text, forType: .string)
    }

    var markdown: String {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd h:mm a zzz"
        var s = "# \(kAppName) chat — \(f.string(from: started))\n\nModel: `\(model)` (local Ollama, nothing sent off this Mac)\n\n"
        for m in messages {
            switch m.role {
            case "user": s += "**You:**" + (m.label.map { " _(\($0))_" } ?? "") + "\n\n\(m.text)\n\n"
            case "context": s += "<details><summary>\(m.label ?? "Context")</summary>\n\n\(m.text)\n\n</details>\n\n"
            default: s += "**\(model):**\n\n\(m.text)\n\n"
            }
        }
        return s
    }

    func save() {
        guard let r = renamer?.root else { return }
        let dir = r.appendingPathComponent("exports/chats")
        do {
            try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            let f = DateFormatter()
            f.dateFormat = "yyyyMMdd-HHmmss"
            var u = dir.appendingPathComponent("chat-\(f.string(from: started)).md")
            var n = 2
            while fileExists(u) && n < 100 {   // never overwrite
                u = dir.appendingPathComponent("chat-\(f.string(from: started))-\(n).md"); n += 1
            }
            try markdown.write(to: u, atomically: true, encoding: .utf8)
            savedTo = u.path
        } catch {
            self.error = "Couldn't save: \(error.localizedDescription)"
        }
    }
}

struct ChatView: View {
    @ObservedObject var chat: ChatModel
    @ObservedObject var model: RenamerModel
    var scrolls = true
    @ViewState private var dropTarget = false

    var body: some View {
        VStack(spacing: 0) {
            toolbar
            Divider()
            if let b = chat.blocker {
                Label(b, systemImage: "pause.circle.fill").font(.callout).foregroundStyle(.orange)
                    .padding(10).frame(maxWidth: .infinity, alignment: .leading).background(Color.orange.opacity(0.1))
            }
            if scrolls {
                ScrollViewReader { proxy in
                    ScrollView { thread }
                        .onChange(of: chat.messages.last?.text ?? "") {
                            if let id = chat.messages.last?.id { proxy.scrollTo(id, anchor: .bottom) }
                        }
                }
            } else {
                thread
            }
            Divider()
            composer
        }
        .frame(minWidth: 560, minHeight: scrolls ? 480 : nil)
        .onDrop(of: [UTType.fileURL], isTargeted: $dropTarget) { providers in
            loadURLs(providers) { urls in if let u = urls.first { chat.attachImage(url: u) } }
            return true
        }
        .onAppear { chat.loadModels(); chat.loadClips() }
    }

    private var toolbar: some View {
        HStack(spacing: 10) {
            Image(systemName: "sparkles").foregroundStyle(.purple)
            Picker("Model", selection: $chat.model) {
                ForEach(chat.models.isEmpty ? [chat.model.isEmpty ? chat.defaultModel : chat.model] : chat.models, id: \.self) { m in
                    Text(m == chat.defaultModel ? "\(m) (default)" : m).tag(m)
                }
            }
            .frame(maxWidth: 300)
            Spacer()
            Button { chat.save() } label: { Label("Save", systemImage: "square.and.arrow.down") }
                .disabled(chat.messages.isEmpty).help("Save as Markdown in exports/chats/")
            Button { chat.newChat() } label: { Label("New chat", systemImage: "plus.bubble") }
        }
        .padding(.horizontal, 14).padding(.top, 12).padding(.bottom, 10)
    }

    private var thread: some View {
        LazyVStack(alignment: .leading, spacing: 12) {
            if chat.messages.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Ask the local model").font(.title3.weight(.semibold))
                    Text("Runs on this Mac with Ollama — nothing is sent anywhere. Attach an image or a clip's notes and transcript to ask about it.")
                        .font(.callout).foregroundStyle(.secondary)
                }
                .padding(.top, 20)
            }
            ForEach(chat.messages) { m in Bubble(m: m, modelName: chat.model, copy: { chat.copy(m) }).id(m.id) }
            if let s = chat.savedTo {
                HStack {
                    Text("Saved to \(s)").font(.caption).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
                    Button("Reveal") { NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: s)]) }.controlSize(.small)
                }
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var composer: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let e = chat.error {
                Text(e).font(.caption).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: 8) {
                if let i = chat.pendingImage { chip("photo", i.name) { chat.pendingImage = nil } }
                if let n = chat.pendingNotes { chip("doc.text", "Notes: \(n.name)") { chat.pendingNotes = nil } }
            }
            HStack(alignment: .bottom, spacing: 8) {
                Menu {
                    Button("Image…") { chat.attachImage() }
                    Menu("Clip notes & transcript") {
                        if chat.clips.isEmpty { Text("No processed clips yet") }
                        ForEach(chat.clips.indices, id: \.self) { i in
                            Button(chat.clips[i].name) { chat.attachNotes(chat.clips[i]) }
                        }
                    }
                } label: { Image(systemName: "paperclip") }
                .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
                .help("Attach an image or a clip's notes/transcript")
                TextField("Ask about your clips, names, transcripts…", text: $chat.input, axis: .vertical)
                    .textFieldStyle(.roundedBorder)
                    .lineLimit(1...6)
                    .onSubmit { chat.send() }
                if chat.streaming {
                    Button { chat.stop() } label: { Label("Stop", systemImage: "stop.fill") }.keyboardShortcut(".")
                } else {
                    Button { chat.send() } label: { Label("Send", systemImage: "arrow.up.circle.fill") }
                        .buttonStyle(.borderedProminent)
                        .keyboardShortcut(.return, modifiers: .command)
                        .disabled(chat.input.trimmingCharacters(in: .whitespaces).isEmpty || chat.blocker != nil
                                  || !ChatModel.isRealName(chat.model))
                }
            }
        }
        .padding(12)
    }

    private func chip(_ icon: String, _ text: String, remove: @escaping () -> Void) -> some View {
        HStack(spacing: 4) {
            Image(systemName: icon)
            Text(text).lineLimit(1)
            Button(action: remove) { Image(systemName: "xmark.circle.fill") }.buttonStyle(.borderless)
        }
        .font(.caption).padding(.horizontal, 8).padding(.vertical, 3).background(.fill.tertiary, in: Capsule())
    }
}

private struct Bubble: View {
    let m: ChatMessage
    let modelName: String
    let copy: () -> Void

    var body: some View {
        if m.role == "context" {
            Label(m.label ?? "Context", systemImage: "doc.text").font(.caption).foregroundStyle(.secondary)
        } else {
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text(m.role == "user" ? "You" : modelName).font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                    if let l = m.label { Text("· \(l)").font(.caption).foregroundStyle(.secondary) }
                    Spacer()
                    if !m.text.isEmpty {
                        Button(action: copy) { Image(systemName: "doc.on.doc") }.buttonStyle(.borderless).help("Copy")
                    }
                }
                if m.text.isEmpty {
                    ProgressView().controlSize(.small)
                } else {
                    Text(m.text).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                        .foregroundStyle(m.error ? Color.red : Color.primary)
                }
            }
            .padding(10)
            .background(m.role == "user" ? Color.accentColor.opacity(0.12) : Color.primary.opacity(0.05),
                        in: RoundedRectangle(cornerRadius: 10))
        }
    }
}
