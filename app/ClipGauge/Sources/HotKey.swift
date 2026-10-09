// Global keyboard shortcut (v0.7) — ported from GrokGauge (HotKey, HotKeyCenter, HotKeyRecorder): one Carbon
// RegisterEventHotKey shortcut that opens/closes the popover from anywhere. No Accessibility permission needed.
import AppKit
import Carbon
import SwiftUI

/// Global shortcut that opens the popover. `keyCode` is a macOS virtual key code.
struct HotKey: Codable, Equatable, Hashable {
    var enabled: Bool
    var keyCode: UInt32
    var control: Bool
    var option: Bool
    var command: Bool
    var shift: Bool

    init(enabled: Bool = true, keyCode: UInt32, control: Bool = false, option: Bool = false,
                command: Bool = false, shift: Bool = false) {
        self.enabled = enabled
        self.keyCode = keyCode
        self.control = control
        self.option = option
        self.command = command
        self.shift = shift
    }

    /// ⌃⌥C (GrokGauge uses ⌃⌥G, so the two never collide)
    static let standard = HotKey(keyCode: 8, control: true, option: true)

    /// A shortcut needs at least one of ⌃ ⌥ ⌘ so plain typing never triggers it.
    var isValid: Bool { control || option || command }

    /// Carbon modifier mask (cmdKey 0x100, shiftKey 0x200, optionKey 0x800, controlKey 0x1000).
    var carbonModifiers: UInt32 {
        (command ? 0x100 : 0) | (shift ? 0x200 : 0) | (option ? 0x800 : 0) | (control ? 0x1000 : 0)
    }

    var display: String {
        (control ? "⌃" : "") + (option ? "⌥" : "") + (shift ? "⇧" : "") + (command ? "⌘" : "")
            + HotKey.keyName(keyCode)
    }

    static func keyName(_ code: UInt32) -> String {
        let names: [UInt32: String] = [
            0: "A", 1: "S", 2: "D", 3: "F", 4: "H", 5: "G", 6: "Z", 7: "X", 8: "C", 9: "V", 11: "B",
            12: "Q", 13: "W", 14: "E", 15: "R", 16: "Y", 17: "T", 18: "1", 19: "2", 20: "3", 21: "4",
            22: "6", 23: "5", 24: "=", 25: "9", 26: "7", 27: "-", 28: "8", 29: "0", 30: "]", 31: "O",
            32: "U", 33: "[", 34: "I", 35: "P", 37: "L", 38: "J", 39: "'", 40: "K", 41: ";", 42: "\\",
            43: ",", 44: "/", 45: "N", 46: "M", 47: ".", 50: "`", 36: "↩", 48: "⇥", 49: "Space",
            51: "⌫", 53: "⎋", 122: "F1", 120: "F2", 99: "F3", 118: "F4", 96: "F5", 97: "F6",
            98: "F7", 100: "F8", 101: "F9", 109: "F10", 103: "F11", 111: "F12",
            123: "←", 124: "→", 125: "↓", 126: "↑",
        ]
        return names[code] ?? "Key \(code)"
    }
}



/// One global shortcut via Carbon's RegisterEventHotKey (works without Accessibility permission).
final class HotKeyCenter: ObservableObject {
    static let shared = HotKeyCenter()

    /// True when macOS refused the last shortcut (another app probably owns it).
    @Published private(set) var registrationFailed = false

    var action: (() -> Void)?
    private(set) var registered: HotKey?
    private var hotKeyRef: EventHotKeyRef?
    private var handlerRef: EventHandlerRef?
    private static let signature: OSType = 0x434C_5047   // 'CLPG'

    /// Returns false when macOS refuses the combination (usually: another app already owns it).
    @discardableResult
    func register(_ key: HotKey) -> Bool {
        unregister()
        registrationFailed = false
        guard key.enabled, key.isValid else { return true }
        installHandlerIfNeeded()
        var ref: EventHotKeyRef?
        let id = EventHotKeyID(signature: Self.signature, id: 1)
        let status = RegisterEventHotKey(key.keyCode, key.carbonModifiers, id, GetApplicationEventTarget(), 0, &ref)
        guard status == noErr, let ref else {
            registrationFailed = true
            return false
        }
        hotKeyRef = ref
        registered = key
        return true
    }

    func unregister() {
        if let hotKeyRef { UnregisterEventHotKey(hotKeyRef) }
        hotKeyRef = nil
        registered = nil
    }

    private func installHandlerIfNeeded() {
        guard handlerRef == nil else { return }
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, event, _ in
            var id = EventHotKeyID()
            let err = GetEventParameter(event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID),
                                        nil, MemoryLayout<EventHotKeyID>.size, nil, &id)
            guard err == noErr, id.signature == HotKeyCenter.signature else { return OSStatus(eventNotHandledErr) }
            DispatchQueue.main.async {
                HotKeyCenter.shared.action?()
            }
            return noErr
        }, 1, &spec, nil, &handlerRef)
    }
}

// MARK: - Shortcut recorder

struct HotKeyRecorder: View {
    @Binding var hotKey: HotKey
    var onRecording: (Bool) -> Void = { _ in }
    @ViewState var recording = false
    @ViewState var monitor: Any? = nil

    var body: some View {
        Button {
            recording ? stop() : start()
        } label: {
            Text(recording ? "Type a shortcut…" : hotKey.display)
                .font(.system(.body, design: .rounded).weight(.medium))
                .frame(minWidth: 120)
        }
        .controlSize(.large)
        .help(recording ? "Press a key with ⌃, ⌥ or ⌘. Esc cancels." : "Click to record a new shortcut")
        .accessibilityLabel("Keyboard shortcut")
        .accessibilityValue(recording ? "recording" : hotKey.display)
        .onDisappear { stop() }
    }

    private func start() {
        recording = true
        onRecording(true)
        monitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { e in
            if e.keyCode == 53 { stop(); return nil }   // Esc
            let f = e.modifierFlags.intersection(.deviceIndependentFlagsMask)
            let key = HotKey(enabled: true, keyCode: UInt32(e.keyCode), control: f.contains(.control),
                             option: f.contains(.option), command: f.contains(.command), shift: f.contains(.shift))
            if key.isValid {
                hotKey = key
                stop()
            } else {
                NSSound.beep()
            }
            return nil
        }
    }

    private func stop() {
        if let monitor { NSEvent.removeMonitor(monitor) }
        monitor = nil
        if recording {
            recording = false
            onRecording(false)
        }
    }
}

