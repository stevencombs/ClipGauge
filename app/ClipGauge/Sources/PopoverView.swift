// ClipGauge popover — same visual language as GrokGauge: rounded headline + capsule badge header,
// a gauge ring hero, rounded "card" sections with small-caps headers, an "Updated … / Refresh now" row,
// capsule action buttons, and a muted version/Quit footer.
import AppKit
import SwiftUI

/// `@State` is a macro in recent SDKs and the Command Line Tools lack SwiftUI's macro plugin (as in GrokGauge).
typealias ViewState<Value> = SwiftUI.State<Value>


struct PopoverView: View {
    @ObservedObject var model: RenamerModel
    @ObservedObject var layout = LayoutStore.shared
    @ObservedObject var appUpdates = AppUpdateMonitor.shared

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            header
                .padding(.top, 8)   // v0.6.1: the title row sat too close to the popover's top edge
            VStack(alignment: .leading, spacing: 12) {
                // Drive / Resolve warnings always lead, whatever the layout says.
                if !layout.settings.isVisible(.alerts) || layout.settings.visibleSections.first != .alerts { criticalAlerts }
                ForEach(layout.settings.visibleSections) { section($0) }
            }
            Divider()
            footer
        }
        .padding(16)
        .frame(width: 320)
    }

    /// One popover section (Settings › Layout decides order and visibility).
    @ViewBuilder private func section(_ s: PopoverSection) -> some View {
        switch s {
        case .alerts: if layout.settings.visibleSections.first == .alerts { problem } else { lastRunProblem }
        case .statusRing: HeroRing(model: model)
        case .currentRun: if model.runActive && model.isPipelineRun { NowCard(model: model) }
        case .inbox: if model.lexarOK { LibraryCard(model: model) }
        case .updates: if model.lexarOK { UpdatesCard(model: model, updates: model.updates) }
        case .askBox: if model.lexarOK { AskBox(model: model) }
        case .services: ServicesCard(model: model)
        case .refreshRow: RefreshRow(model: model)
        case .actions: ActionsRow(model: model, kinds: layout.settings.visibleActions)
        case .sortButton: SortButton(model: model)
        }
    }

    private var header: some View {
        HStack(spacing: 8) {
            ClipMarkView(size: 18).foregroundStyle(.primary)
            Text(kAppName).font(.system(.headline, design: .rounded))
            Spacer()
            Text(badge)
                .font(.caption2.weight(.semibold))
                .textCase(.uppercase)
                .foregroundStyle(.secondary)
                .padding(.horizontal, 8)
                .padding(.vertical, 3)
                .background(.fill.tertiary, in: Capsule())
            Menu {
                Button("Open \(kAppName)…") { model.showMain() }
                Button("Ask the Model…") { model.showChat(nil) }
                Divider()
                Button("Settings…") { model.showSettings() }
                Button("Setup…") { model.showSetup() }
                Button("Model & Tool Updates…") { model.showUpdates() }
                Button("Sort into Projects…") { model.showSort() }
                Button("Instructions…") { model.showInstructions() }
                Button("About \(kAppName)") { model.showAbout() }
                Divider()
                Button("Quit \(kAppName)") { NSApp.terminate(nil) }
            } label: {
                Image(systemName: "gearshape").font(.system(size: 13, weight: .medium))
            }
            .menuStyle(.borderlessButton)
            .menuIndicator(.hidden)
            .fixedSize()
            .help("Settings (⌘,), Setup and About")
            .accessibilityLabel("Settings, Setup and About")
        }
    }

    private var badge: String {
        guard model.lexarOK else { return "Offline" }
        return model.dryRun ? "Renamer · Dry run" : "Renamer · Live"
    }

    /// Every alert (used when Alerts is the first section, as in v0.6).
    @ViewBuilder private var problem: some View {
        if !model.lexarOK || model.resolveRunning { criticalAlerts } else { lastRunProblem }
    }

    @ViewBuilder private var lastRunProblem: some View {
        if model.lexarOK && !model.resolveRunning && model.problemVisible, let p = model.problem {
            ProblemCard(icon: "exclamationmark.triangle.fill",
                        title: model.state == "error" ? "Last run ended with an error" : "Last run finished with errors",
                        detail: problemText(p),
                        action: ((p["exists"] as? Bool) == true ? "Show File" : "Open Inbox", { model.revealProblemFile() }),
                        secondary: ("Dismiss", { model.dismissProblem() }))
        }
    }

    @ViewBuilder private var criticalAlerts: some View {
        if !model.lexarOK {
            ProblemCard(icon: Project.remembered == nil ? "folder.badge.questionmark" : "externaldrive.badge.xmark",
                        title: Project.missingTitle,
                        detail: Project.remembered == nil
                            ? "\(kAppName) needs a project folder (scripts, inbox, models). Open Setup to choose an existing one or create a new one."
                            : "Connect the drive that holds \(Project.remembered ?? "the project"). Controls are greyed out until then.",
                        action: ("Open Setup…", { model.showSetup() }))
        } else if model.resolveRunning {
            ProblemCard(icon: "pause.circle.fill", title: "Paused, Resolve is open",
                        detail: model.runActive
                            ? "DaVinci Resolve is running. The current run keeps going; new runs can't start until Resolve quits."
                            : "Processing can't start while DaVinci Resolve is running. Quit Resolve to continue.")
        }
    }

    private func problemText(_ p: [String: Any]) -> String {
        var t = (p["reason"] as? String) ?? "Unknown error"
        if let at = (p["updated_at"] as? String).flatMap({ isoParser.date(from: $0) }) { t = "\(shortStamp(at)): " + t }
        if let h = p["hint"] as? String, !h.isEmpty { t += "\n\n" + h }
        return t
    }

    private var footer: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let v = appUpdates.available {
                UpdateBanner(version: v, updates: appUpdates, open: { model.showSettings() })
            }
            if model.notifier.mode == .fallback {
                Label {
                    Text("Notifications are limited. Allow \(kAppName) in System Settings › Notifications.")
                } icon: {
                    Image(systemName: "bell.slash.fill").foregroundStyle(.orange)
                }
                .font(.caption)
                .foregroundStyle(.secondary)
            }
            HStack {
                Text("v\(kVersion) · local")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
                    .help("\(kAppName) v\(kVersion) · runs locally")
                Spacer()
                Button("Support \(kAppName)…") { NSWorkspace.shared.open(AppInfo.tipURL) }
                    .buttonStyle(.borderless)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .help("Tip via PayPal (paypal.me/stevencombs)")
                    .lineLimit(1)
                    .fixedSize()
                Button("Quit \(kAppName)") { NSApp.terminate(nil) }
                    .buttonStyle(.borderless)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .fixedSize()
                    .keyboardShortcut("q")
            }
        }
    }
}

// MARK: - Hero ring

private struct HeroRing: View {
    @ObservedObject var model: RenamerModel
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        // While models/tools are being upgraded (and no run is active), the ring shows the update job in blue.
        let upd = model.progress == nil ? model.updates.progressPercent : nil
        let p = model.progress ?? upd.map { (pct: $0, index: 0, total: 0) }
        let color = upd != nil ? Color.blue : model.level.color
        VStack(spacing: 6) {
            ZStack {
                RingGauge(fraction: Double(p?.pct ?? 0) / 100, color: color, lineWidth: 11)
                if model.actionBusy != nil {
                    ProgressView().controlSize(.small)
                } else {
                    VStack(spacing: 0) {
                        Text(p.map { "\($0.pct)%" } ?? (model.runActive ? "…" : "—"))
                            .font(.system(size: 29, weight: .bold, design: .rounded))
                            .monospacedDigit()
                            .foregroundStyle(p == nil ? Color.secondary : color)
                            .contentTransition(reduceMotion ? .identity : .numericText())
                        Text(p == nil ? (model.lexarOK ? "idle" : "offline") : (upd != nil ? "updating" : "done"))
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .frame(width: 116, height: 116)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(upd != nil ? "Update progress" : "Batch progress")
            .accessibilityValue(p.map { "\($0.pct) percent" } ?? model.headline)

            Text(model.headline)
                .font(.system(.callout, design: .rounded).weight(.semibold))
            Text(model.subline)
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(1)
        }
        .frame(maxWidth: .infinity)
    }
}

struct RingGauge: View {
    let fraction: Double
    let color: Color
    var lineWidth: CGFloat = 13
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.colorSchemeContrast) private var contrast

    var body: some View {
        let f = min(1, max(0, fraction))
        let high = contrast == .increased
        ZStack {
            Circle()
                .stroke(high ? Color.secondary.opacity(0.45) : color.opacity(0.16), lineWidth: lineWidth)
            if f > 0 {
                Circle()
                    .trim(from: 0, to: f)
                    .stroke(
                        high ? AnyShapeStyle(color) : AnyShapeStyle(
                            AngularGradient(colors: [color.opacity(0.6), color], center: .center,
                                            startAngle: .degrees(0), endAngle: .degrees(360 * f))),
                        style: StrokeStyle(lineWidth: lineWidth, lineCap: .round)
                    )
                    .rotationEffect(.degrees(-90))
                    .shadow(color: high ? .clear : color.opacity(0.35), radius: 4)
            }
        }
        .padding(lineWidth / 2)
        .animation(reduceMotion ? nil : .spring(duration: 0.6), value: f)
    }
}

// MARK: - Cards

private struct CardBackground: ViewModifier {
    @Environment(\.colorSchemeContrast) private var contrast
    func body(content: Content) -> some View {
        content
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(.fill.quinary, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .strokeBorder(contrast == .increased ? AnyShapeStyle(Color.primary.opacity(0.5))
                                                         : AnyShapeStyle(.separator.opacity(0.5)),
                                  lineWidth: contrast == .increased ? 1 : 0.5)
            )
    }
}

extension View {
    func card() -> some View { modifier(CardBackground()) }
}

private struct CardTitle: View {
    let text: String
    var body: some View {
        Text(text.uppercased())
            .font(.caption2.weight(.semibold))
            .foregroundStyle(.secondary)
    }
}

private struct NowCard: View {
    @ObservedObject var model: RenamerModel

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            CardTitle(text: "Now processing")
            Text(model.currentClip ?? "—")
                .font(.callout.weight(.semibold))
                .lineLimit(1)
                .truncationMode(.middle)
            if let m = model.stepMessage {
                Text(m).font(.caption).foregroundStyle(.secondary).lineLimit(2)
            }
            Divider()
            TimelineView(.periodic(from: .now, by: 15)) { ctx in
                etaLine(now: ctx.date)
            }
        }
        .card()
    }

    @ViewBuilder private func etaLine(now: Date) -> some View {
        if let eta = model.eta {
            let left = max(0, eta.timeIntervalSince(now))
            let mins = Int((left / 60).rounded(.up))
            HStack(alignment: .center, spacing: 8) {
                Text("ETA").font(.callout.weight(.semibold)).frame(width: 40, alignment: .leading)
                HStack(alignment: .firstTextBaseline, spacing: 3) {
                    Text(mins >= 90 ? "\(mins / 60)h \(mins % 60)" : "\(mins)")
                        .font(.system(size: 24, weight: .bold, design: .rounded))
                        .monospacedDigit()
                    Text(mins >= 90 ? "min" : (mins == 1 ? "min" : "mins"))
                        .font(.system(.callout, design: .rounded).weight(.semibold))
                        .foregroundStyle(.secondary)
                }
                Spacer(minLength: 4)
                VStack(alignment: .trailing, spacing: 1) {
                    Text("done ≈ \(clockString(eta, zone: false))").font(.caption).monospacedDigit()
                    Text(TimeZone.current.abbreviation() ?? "").font(.caption2).foregroundStyle(.secondary)
                }
            }
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("About \(mins) minutes left, finishing around \(clockString(eta))")
        } else {
            HStack {
                Text("ETA").font(.callout.weight(.semibold))
                Spacer()
                Text("estimating…").font(.caption).foregroundStyle(.secondary)
            }
        }
    }
}

private struct LibraryCard: View {
    @ObservedObject var model: RenamerModel

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            CardTitle(text: "Inbox")
            row(icon: "checkmark.circle", label: "Last renamed",
                value: model.lastRenamedAt.map(shortStamp) ?? "—", detail: model.lastRenamed,
                help: "Show the last renamed file in Finder") { model.openLastRenamed() }
            row(icon: "flag", label: "Needs review", value: "\(model.needsReviewCount)",
                valueColor: model.needsReviewCount > 0 ? Level.warning.color : nil,
                detail: model.inboxReview == nil ? "last run only · counting the inbox…" : nil,
                help: "Show the clips that need review in Finder") { model.openNeedsReview() }
            row(icon: "tray", label: "Waiting to process", value: model.inboxPending.map { "\($0)" } ?? "—",
                help: "Open inbox/ in Finder") { model.openInbox() }
            row(icon: "text.bubble", label: "Instructions", value: model.instructions.active ? "Active" : "Off",
                valueColor: model.instructions.active ? .purple : .secondary,
                detail: model.instructions.active ? model.instructions.detail : nil,
                help: "Custom instructions and glossary for the vision model and Whisper") { model.showInstructions() }
        }
        .card()
    }

    private func row(icon: String, label: String, value: String, valueColor: Color? = nil, detail: String? = nil,
                     help: String, _ action: @escaping () -> Void) -> some View {
        Button(action: action) { rowBody(icon: icon, label: label, value: value, valueColor: valueColor, detail: detail) }
            .buttonStyle(RowButtonStyle())
            .help(help)
            .accessibilityHint(help)
    }

    private func rowBody(icon: String, label: String, value: String, valueColor: Color?, detail: String?) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 10) {
                Image(systemName: icon).frame(width: 18).foregroundStyle(.secondary)
                Text(label).font(.callout)
                Spacer()
                Text(value)
                    .font(.callout.weight(.semibold))
                    .monospacedDigit()
                    .foregroundStyle(valueColor ?? .primary)
                Image(systemName: "chevron.right")
                    .font(.system(size: 9, weight: .semibold))
                    .foregroundStyle(.tertiary)
            }
            if let d = detail {
                Text(d)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .padding(.leading, 28)
            }
        }
    }
}

private struct ServicesCard: View {
    @ObservedObject var model: RenamerModel

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            CardTitle(text: "Services")
            toggleRow(icon: "power", label: "Open at login",
                      detail: model.inApplications ? (model.loginEnabled ? "Starts when you log in" : "Off")
                                                   : "Needs the app in Applications",
                      isOn: Binding(get: { model.loginEnabled }, set: { _ in model.toggleLogin() }),
                      enabled: model.inApplications)
        }
        .card()
    }

    private func toggleRow(icon: String, label: String, detail: String, isOn: Binding<Bool>, enabled: Bool) -> some View {
        HStack(spacing: 10) {
            Image(systemName: icon).frame(width: 18).foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 1) {
                Text(label).font(.callout)
                Text(detail).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer()
            Toggle("", isOn: isOn)
                .labelsHidden()
                .toggleStyle(.switch)
                .controlSize(.small)
                .disabled(!enabled)
        }
    }
}

private struct ProblemCard: View {
    let icon: String
    let title: String
    let detail: String
    var action: (String, () -> Void)? = nil
    var secondary: (String, () -> Void)? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Image(systemName: icon).font(.title2).foregroundStyle(.orange).accessibilityHidden(true)
                Text(title).font(.headline)
            }
            Text(detail)
                .font(.callout)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if action != nil || secondary != nil {
                HStack {
                    Spacer()
                    if let b = secondary { Button(b.0, action: b.1) }
                    if let a = action { Button(a.0, action: a.1) }
                }.controlSize(.small)
            }
        }
        .card()
    }
}

private struct RefreshRow: View {
    @ObservedObject var model: RenamerModel

    var body: some View {
        HStack {
            Text(model.updatedAt.map { "Updated \(clockString($0, zone: false))" } ?? " ")
                .font(.caption)
                .foregroundStyle(.secondary)
                .monospacedDigit()
            Spacer()
            Button {
                model.tick()
                model.refreshInbox(force: true)
            } label: {
                HStack(spacing: 4) {
                    Image(systemName: "arrow.clockwise")
                    Text("Refresh now")
                }
                .font(.caption.weight(.medium))
            }
            .buttonStyle(.borderless)
            .keyboardShortcut("r")
        }
    }
}

private struct ActionsRow: View {
    @ObservedObject var model: RenamerModel
    var kinds: [ActionButtonKind] = LayoutSettings.defaults.visibleActions

    var body: some View {
        HStack(spacing: 8) {
            ForEach(kinds.isEmpty ? [.start] : kinds) { button($0) }
        }
    }

    @ViewBuilder private func button(_ k: ActionButtonKind) -> some View {
        let running = model.runActive && model.isPipelineRun
        switch k {
        case .start:
            action(running ? "Stop" : "Start", symbol: running ? "stop.fill" : "play.fill",
                   help: running ? "Stop processing" : (model.resolveRunning ? "Paused, Resolve is open" : (model.updates.jobActive ? "Updates are being installed" : "Start processing")),
                   enabled: model.lexarOK && model.actionBusy == nil && (running || (!model.runActive && !model.resolveRunning && !model.updates.jobActive))) {
                model.toggleRun()
            }
        case .open:
            action("Open", symbol: "macwindow", help: "Open the \(kAppName) window (add clips, results, undo, tools)", enabled: model.lexarOK) { model.showMain() }
        case .inbox:
            action("Inbox", symbol: "folder", help: "Open inbox in Finder", enabled: model.lexarOK) { model.openInbox() }
        case .ask:
            action("Ask", symbol: "sparkles", help: "Ask the local model (chat window)", enabled: model.lexarOK) { model.showChat(nil) }
        }
    }

    // Standard bordered capsule buttons (Liquid Glass on macOS 26+, classic bezel on 14–15), as in GrokGauge.
    private func action(_ title: String, symbol: String, help: String, enabled: Bool, _ perform: @escaping () -> Void) -> some View {
        Button(action: perform) {
            Label(title, systemImage: symbol)
                .font(.callout.weight(.medium))
                .lineLimit(1)
                .frame(maxWidth: .infinity)
        }
        .buttonStyle(.bordered)
        .buttonBorderShape(.capsule)
        .controlSize(.large)
        .disabled(!enabled)
        .help(help)
        .accessibilityLabel(help)
    }
}


/// Updates (v0.6.1 layout section): job progress, or what the last check found. Click opens Setup › Updates.
private struct UpdatesCard: View {
    @ObservedObject var model: RenamerModel
    @ObservedObject var updates: UpdatesModel

    var body: some View {
        Button { model.showUpdates() } label: {
            HStack(spacing: 10) {
                Image(systemName: updates.jobActive ? "arrow.down.circle.fill" : (updates.upgradable.isEmpty ? "checkmark.circle" : "arrow.down.circle"))
                    .frame(width: 18)
                    .foregroundStyle(updates.jobActive ? Color.blue : (updates.upgradable.isEmpty ? Color.secondary : Color.orange))
                VStack(alignment: .leading, spacing: 1) {
                    Text("Updates").font(.callout)
                    Text(detail).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                }
                Spacer()
                Image(systemName: "chevron.right").font(.caption).foregroundStyle(.tertiary)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help("Models and tools — opens Setup › Updates")
        .card()
    }

    private var detail: String {
        if updates.jobActive, let j = updates.job { return "Installing \(j.current ?? "…") · \(j.percent)%" }
        if updates.checking { return "Checking…" }
        let n = updates.upgradable.count
        let when = updates.checkedAt.map { "checked \(shortStamp($0))" } ?? "not checked yet"
        if n == 0 { return "Up to date · \(when)" }
        let size = updates.totalDownload > 0 ? " · \(ByteCountFormatter.string(fromByteCount: updates.totalDownload, countStyle: .file))" : ""
        return "\(n) available\(size) · \(when)"
    }
}

/// ⌘K-style prompt: opens the Ask the Model chat window with this question (local Ollama only).
private struct AskBox: View {
    @ObservedObject var model: RenamerModel
    @ViewState private var text = ""

    var body: some View {
        let blocked = model.runActive || model.updates.jobActive
        HStack(spacing: 8) {
            Image(systemName: "sparkles").foregroundStyle(.purple)
            TextField(blocked ? "Ask the Model — paused while busy" : "Ask the model…", text: $text)
                .textFieldStyle(.plain)
                .disabled(blocked)
                .onSubmit { submit() }
            Button { submit() } label: { Image(systemName: "arrow.up.circle.fill").font(.system(size: 16)) }
                .buttonStyle(.borderless)
                .keyboardShortcut("k")
                .help(blocked ? "Paused while a run or an update is active (memory)" : "Ask the local model (⌘K)")
        }
        .padding(.horizontal, 10).padding(.vertical, 8)
        .background(.fill.quinary, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(.separator.opacity(0.5), lineWidth: 0.5))
    }

    private func submit() {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        text = ""
        model.showChat(t.isEmpty ? nil : t)
    }
}

/// Full-width secondary button under the actions: opens the Sort into Projects window (dry run first).
private struct SortButton: View {
    @ObservedObject var model: RenamerModel
    var body: some View {
        Button { model.showSort() } label: {
            Label("Sort into Projects…", systemImage: "folder.badge.gearshape")
                .font(.callout)
                .frame(maxWidth: .infinity)
        }
        .buttonStyle(.bordered)
        .buttonBorderShape(.capsule)
        .controlSize(.regular)
        .disabled(!model.lexarOK)
        .help("Group clips by shoot and file them into DaVinci Resolve/<Project>/A-Roll, B-Roll… — dry run first")
    }
}

/// Whole-row button: no bezel, subtle highlight on hover/press (Finder-style reveal rows in the Inbox card).
private struct RowButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View { HoverRow(configuration: configuration) }

    private struct HoverRow: View {
        let configuration: ButtonStyleConfiguration
        @ViewState private var hover = false
        var body: some View {
            configuration.label
                .contentShape(Rectangle())
                .padding(.vertical, 3)
                .padding(.horizontal, 6)
                .background(RoundedRectangle(cornerRadius: 7, style: .continuous)
                    .fill(Color.primary.opacity(configuration.isPressed ? 0.12 : (hover ? 0.06 : 0))))
                .padding(.horizontal, -6)
                .onHover { hover = $0 }
        }
    }
}

/// GrokGauge's update banner: a newer ClipGauge is in the project folder; copy the rebuild command.
private struct UpdateBanner: View {
    let version: String
    @ObservedObject var updates: AppUpdateMonitor
    var open: () -> Void
    @ViewState var copied = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Image(systemName: "arrow.down.circle.fill").foregroundStyle(Color.accentColor).accessibilityHidden(true)
                Text("Update available: \(version)").font(.caption.weight(.semibold))
                Spacer()
                Button("Details", action: open).buttonStyle(.borderless).font(.caption)
            }
            HStack(spacing: 6) {
                Text("zsh app/ClipGauge/build.sh")
                    .font(.system(.caption2, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
                Spacer()
                Button(copied ? "Copied" : "Copy") { updates.copyBuildCommand(); copied = true }
                    .buttonStyle(.borderless)
                    .font(.caption)
                    .accessibilityLabel("Copy the rebuild command")
            }
        }
        .padding(10)
        .background(Color.accentColor.opacity(0.10), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
    }
}
