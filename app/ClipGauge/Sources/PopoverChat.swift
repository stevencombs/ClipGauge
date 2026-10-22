// Ask the Model in the popover (v0.8.4). The popover is the first thing ClipGauge shows (menu bar click, ⌃⌥C), so
// the chat lives right at its top: the conversation, a message box and the model in use. It is the same
// conversation as the main window's chat panel; the expand button (or ⇧⌘A) continues there with attachments.
// Open by default; the chevron collapses it to one line and the choice is remembered.
import AppKit
import SwiftUI

/// UserDefaults key: is the chat card expanded in the popover (default true; reset to true on upgrade to 0.8.4).
let kPopoverChatKey = "ClipGaugePopoverChatOpen"
/// Popover width when the chat card is shown (320 without it).
let kPopoverWidthWithChat: CGFloat = 380

struct PopoverChatCard: View {
    @ObservedObject var chat: ChatModel
    @ObservedObject var model: RenamerModel
    var scrolls = true
    @AppStorage(kPopoverChatKey) private var open = true
    @FocusState private var focused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            header
            if open {
                if let b = chat.blocker {
                    Label(b, systemImage: "pause.circle.fill").font(.caption).foregroundStyle(.orange)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if !chat.messages.isEmpty {
                    if scrolls {
                        ScrollViewReader { proxy in
                            ScrollView { thread }
                                .frame(maxHeight: 230)
                                .onAppear { if let id = chat.messages.last?.id { proxy.scrollTo(id, anchor: .bottom) } }
                                .onChange(of: chat.messages.last?.text ?? "") {
                                    if let id = chat.messages.last?.id { proxy.scrollTo(id, anchor: .bottom) }
                                }
                        }
                    } else {
                        thread
                    }
                } else {
                    Text("Runs on this Mac with Ollama, nothing is sent anywhere. Ask about your clips, names or transcripts.")
                        .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
                if let e = chat.error {
                    Text(e).font(.caption).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true)
                }
                composer
            }
        }
        .padding(10)
        .background(.fill.quinary, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(.separator.opacity(0.5), lineWidth: 0.5))
        .onAppear {
            chat.loadModels()
            if open && chat.blocker == nil { DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { focused = true } }
        }
        .onChange(of: chat.focusRequest) { if open { focused = true } }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Ask the Model")
    }

    private var header: some View {
        HStack(spacing: 6) {
            Image(systemName: "sparkles").foregroundStyle(.purple)
            Text("Ask the Model").font(.callout.weight(.semibold))
            if open {
                Text(chat.model.isEmpty ? chat.defaultModel : chat.model)
                    .font(.caption2).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
            }
            Spacer()
            if open {
                Button { chat.newChat() } label: { Image(systemName: "plus.bubble") }
                    .buttonStyle(.borderless).disabled(chat.messages.isEmpty || chat.streaming).help("New chat")
                Button { model.showChat(nil) } label: { Image(systemName: "arrow.up.left.and.arrow.down.right") }
                    .buttonStyle(.borderless).help("Continue in the \(kAppName) window: model picker, attachments, save (⇧⌘A)")
            }
            Button { withAnimation(.easeInOut(duration: 0.15)) { open.toggle() } } label: {
                Image(systemName: open ? "chevron.up" : "chevron.down")
            }
            .buttonStyle(.borderless)
            .help(open ? "Collapse the chat" : "Show the chat")
            .accessibilityLabel(open ? "Collapse the chat" : "Show the chat")
        }
        .font(.system(size: 12))
    }

    private var thread: some View {
        LazyVStack(alignment: .leading, spacing: 8) {
            ForEach(chat.messages.filter { $0.role != "context" }) { m in
                VStack(alignment: .leading, spacing: 2) {
                    Text(m.role == "user" ? "You" : (chat.model.isEmpty ? "Model" : chat.model))
                        .font(.caption2.weight(.semibold)).foregroundStyle(.secondary)
                    if m.text.isEmpty {
                        ProgressView().controlSize(.small)
                    } else {
                        Text(m.text).font(.callout).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                            .foregroundStyle(m.error ? Color.red : Color.primary)
                    }
                }
                .padding(8)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(m.role == "user" ? Color.accentColor.opacity(0.12) : Color.primary.opacity(0.05),
                            in: RoundedRectangle(cornerRadius: 8))
                .id(m.id)
            }
        }
    }

    private var composer: some View {
        HStack(alignment: .bottom, spacing: 6) {
            TextField(chat.blocker != nil ? "Paused while busy" : "Ask the model…", text: $chat.input, axis: .vertical)
                .textFieldStyle(.roundedBorder)
                .lineLimit(1...4)
                .focused($focused)
                .disabled(chat.blocker != nil)
                .onSubmit { send() }
            if chat.streaming {
                Button { chat.stop() } label: { Image(systemName: "stop.circle.fill").font(.system(size: 18)) }
                    .buttonStyle(.borderless).help("Stop")
            } else {
                Button { send() } label: { Image(systemName: "arrow.up.circle.fill").font(.system(size: 18)) }
                    .buttonStyle(.borderless)
                    .disabled(!canSend)
                    .help(chat.blocker ?? "Send (Return)")
                    .accessibilityLabel("Send")
            }
        }
    }

    private var canSend: Bool {
        !chat.input.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && chat.blocker == nil
            && ChatModel.isRealName(chat.model.isEmpty ? chat.defaultModel : chat.model)
    }

    private func send() {
        guard canSend, !chat.streaming else { return }
        if !ChatModel.isRealName(chat.model) { chat.model = chat.defaultModel }
        chat.send()
    }
}
