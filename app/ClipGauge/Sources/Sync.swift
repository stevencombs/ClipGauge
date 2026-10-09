// Settings sync (v0.7) — GrokGauge's design: one small JSON file, `<sync folder>/ClipGauge/settings.json`, in a folder
// the user's Macs already sync (Google Drive via Insync, iCloud Drive, Dropbox…), watched for changes. Preferences
// only — never clips, notes, transcripts, models, paths, or anything in the project folder.
// ClipGauge goes one step further than GrokGauge's "last write wins": it merges group by group (layout, menu bar,
// colors, alerts, general), keeps unknown keys written by newer builds, and backs up the previous file locally
// before replacing it, so an existing sync file is never simply overwritten.
import Foundation

struct SettingsEnvelope: Codable {
    var app: String
    var schema: Int
    var settings: ClipGaugeSettings
}

enum SettingsSync {
    static let folderName = "ClipGauge"
    static let fileName = "settings.json"
    static let appName = "ClipGauge"
    /// Timestamps closer than this are treated as equal (JSON keeps milliseconds).
    static let tolerance: TimeInterval = 0.002

    enum SyncError: Error { case notClipGauge, tooLarge }

    static func fileURL(inSyncFolder folder: URL) -> URL {
        folder.appendingPathComponent(folderName, isDirectory: true).appendingPathComponent(fileName)
    }

    // MARK: merge

    struct MergeResult {
        var merged: ClipGaugeSettings
        /// The shared file is missing or behind: write the merged settings.
        var writeFile: Bool
        /// The groups taken from the file (for the status line).
        var adopted: [SettingsGroup]
        var note: String?
    }

    /// Group by group, the newer change wins; a tie keeps this Mac's values. A Mac that never changed a group
    /// (epoch stamp) always takes the file's version of it, so linking a new Mac never clobbers the shared file.
    static func merge(local: ClipGaugeSettings, remote: ClipGaugeSettings?) -> MergeResult {
        guard let remote else {
            return MergeResult(merged: local, writeFile: true, adopted: [], note: "Created \(folderName)/\(fileName)")
        }
        var m = local
        var adopted: [SettingsGroup] = []
        var localNewer: [SettingsGroup] = []
        for g in SettingsGroup.allCases {
            let lt = local.stamp(g), rt = remote.stamp(g)
            let same = local.sameGroup(g, as: remote)
            if rt.timeIntervalSince(lt) > tolerance {
                if !same { m.copyGroup(g, from: remote); adopted.append(g) }
            } else if lt.timeIntervalSince(rt) > tolerance, !same {
                localNewer.append(g)
            } else if !same {
                localNewer.append(g)   // tie with different values: keep this Mac's (as GrokGauge does)
            }
            let newest = max(lt, rt)
            if newest > ClipGaugeSettings.epoch { m.groupModified[g.rawValue] = newest }
        }
        m.modifiedAt = max(local.modifiedAt, remote.modifiedAt)
        m.pinGroupStamps()
        let write = !m.sameContent(as: remote) || !stampsEqual(m, remote)
        var note: String?
        if !adopted.isEmpty && !localNewer.isEmpty {
            note = "Merged: took \(names(adopted)) from the file, kept this Mac's \(names(localNewer))"
        } else if !adopted.isEmpty {
            note = "Took \(names(adopted)) from the file"
        } else if !localNewer.isEmpty {
            note = "Wrote this Mac's \(names(localNewer))"
        }
        return MergeResult(merged: m, writeFile: write, adopted: adopted, note: note)
    }

    static func names(_ gs: [SettingsGroup]) -> String {
        gs.map { g -> String in
            switch g {
            case .layout: return "layout"
            case .menuBar: return "menu bar"
            case .colors: return "colors"
            case .alerts: return "notifications"
            case .general: return "update check"
            }
        }.joined(separator: ", ")
    }

    static func stampsEqual(_ a: ClipGaugeSettings, _ b: ClipGaugeSettings) -> Bool {
        if abs(a.modifiedAt.timeIntervalSince(b.modifiedAt)) > tolerance { return false }
        for g in SettingsGroup.allCases where abs(a.stamp(g).timeIntervalSince(b.stamp(g))) > tolerance { return false }
        return true
    }

    // MARK: JSON

    private static func encoder() -> JSONEncoder {
        let e = JSONEncoder()
        e.outputFormatting = [.prettyPrinted, .sortedKeys]
        e.dateEncodingStrategy = .custom { date, enc in
            var c = enc.singleValueContainer()
            try c.encode(timestamp(date))
        }
        return e
    }

    private static func decoder() -> JSONDecoder {
        let d = JSONDecoder()
        d.dateDecodingStrategy = .custom { dec in
            let c = try dec.singleValueContainer()
            if let s = try? c.decode(String.self), let date = parseDate(s) { return date }
            if let t = try? c.decode(Double.self) { return Date(timeIntervalSinceReferenceDate: t) }   // v0.6.1 default encoding
            throw DecodingError.dataCorruptedError(in: c, debugDescription: "Bad date")
        }
        return d
    }

    static func timestamp(_ date: Date) -> String {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        f.timeZone = TimeZone(identifier: "UTC")
        return f.string(from: date)
    }

    static func parseDate(_ s: String) -> Date? {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let d = f.date(from: s) { return d }
        f.formatOptions = [.withInternetDateTime]
        return f.date(from: s)
    }

    /// Dates are kept to the millisecond, the precision the JSON file keeps.
    static func roundedNow() -> Date {
        Date(timeIntervalSince1970: (Date().timeIntervalSince1970 * 1000).rounded() / 1000)
    }

    static func encodeSettings(_ s: ClipGaugeSettings) throws -> Data { try encoder().encode(s) }
    static func decodeSettings(_ d: Data) throws -> ClipGaugeSettings { try decoder().decode(ClipGaugeSettings.self, from: d) }

    static func encodeEnvelope(_ s: ClipGaugeSettings) throws -> Data {
        try encoder().encode(SettingsEnvelope(app: appName, schema: ClipGaugeSettings.schemaVersion, settings: s))
    }

    static func decode(_ data: Data) throws -> ClipGaugeSettings {
        let env = try decoder().decode(SettingsEnvelope.self, from: data)
        guard env.app == appName else { throw SyncError.notClipGauge }
        return env.settings
    }

    /// Reads the shared file. Missing file -> nil. Small files only.
    static func read(from url: URL) throws -> ClipGaugeSettings? {
        guard FileManager.default.fileExists(atPath: url.path) else { return nil }
        let data = try Data(contentsOf: url, options: [.uncached])
        guard data.count < 512 * 1024 else { throw SyncError.tooLarge }
        return try decode(data)
    }

    /// Writes atomically (temp file + rename) so sync clients never see half a file. If a file is already there,
    /// its unknown keys are kept and a copy goes to `backupDir` first (the last 10 are kept).
    static func write(_ s: ClipGaugeSettings, to url: URL, backupDir: URL?) throws {
        let fm = FileManager.default
        try fm.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        var top: [String: Any] = [:]
        var inner: [String: Any] = [:]
        if fm.fileExists(atPath: url.path) {
            let old = try Data(contentsOf: url, options: [.uncached])
            guard old.count < 512 * 1024 else { throw SyncError.tooLarge }
            guard let obj = try JSONSerialization.jsonObject(with: old) as? [String: Any] else { throw SyncError.notClipGauge }
            if let app = obj["app"] as? String, app != appName { throw SyncError.notClipGauge }
            top = obj
            inner = obj["settings"] as? [String: Any] ?? [:]
            if let dir = backupDir { backup(old, to: dir) }
        }
        let ours = try JSONSerialization.jsonObject(with: encodeSettings(s)) as? [String: Any] ?? [:]
        for (k, v) in ours { inner[k] = v }
        top["app"] = appName
        top["schema"] = ClipGaugeSettings.schemaVersion
        top["settings"] = inner
        let data = try JSONSerialization.data(withJSONObject: top, options: [.prettyPrinted, .sortedKeys])
        try data.write(to: url, options: [.atomic])
    }

    private static func backup(_ data: Data, to dir: URL) {
        let fm = FileManager.default
        try? fm.createDirectory(at: dir, withIntermediateDirectories: true)
        let f = DateFormatter()
        f.dateFormat = "yyyyMMdd-HHmmss-SSS"
        try? data.write(to: dir.appendingPathComponent("settings-\(f.string(from: Date())).json"), options: .atomic)
        let files = ((try? fm.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil)) ?? [])
            .filter { $0.lastPathComponent.hasPrefix("settings-") }.sorted { $0.lastPathComponent < $1.lastPathComponent }
        for old in files.dropLast(10) { try? fm.removeItem(at: old) }
    }

    static func describe(_ error: Error) -> String {
        if let e = error as? SyncError {
            switch e {
            case .notClipGauge: return "settings.json isn't a ClipGauge settings file — left untouched"
            case .tooLarge: return "settings.json is too large"
            }
        }
        if error is DecodingError { return "settings.json isn't a ClipGauge settings file — left untouched" }
        let ns = error as NSError
        if ns.domain == NSCocoaErrorDomain {
            switch ns.code {
            case NSFileReadNoPermissionError, NSFileWriteNoPermissionError: return "No permission to use that folder"
            case NSFileWriteOutOfSpaceError: return "Disk is full"
            case NSFileNoSuchFileError, NSFileReadNoSuchFileError: return "Folder not found"
            default: break
            }
        }
        return "Couldn't read or write settings.json"
    }
}


/// Watches the shared settings folder and file. Sync clients (Google Drive, Insync, Dropbox, iCloud)
/// often replace a file rather than write it in place, so this watches the folder (renames/creates),
/// the file itself (in-place writes), and also polls as a fallback.
final class FolderWatcher {
    private let directory: URL
    private let file: URL
    private let onChange: () -> Void
    private var dirSource: DispatchSourceFileSystemObject?
    private var fileSource: DispatchSourceFileSystemObject?
    private var pollTimer: DispatchSourceTimer?
    private var lastSeen: (Date?, Int?) = (nil, nil)
    private var pending: DispatchWorkItem?

    init(directory: URL, file: URL, pollInterval: TimeInterval = 30, onChange: @escaping () -> Void) {
        self.directory = directory
        self.file = file
        self.onChange = onChange
        let timer = DispatchSource.makeTimerSource(queue: .main)
        timer.schedule(deadline: .now() + pollInterval, repeating: pollInterval, leeway: .seconds(5))
        timer.setEventHandler { [weak self] in self?.poll() }
        pollTimer = timer
    }

    func start() {
        lastSeen = stamp()
        arm()
        pollTimer?.resume()
    }

    func stop() {
        dirSource?.cancel(); dirSource = nil
        fileSource?.cancel(); fileSource = nil
        pollTimer?.cancel(); pollTimer = nil
        pending?.cancel()
    }

    deinit { stop() }

    private func arm() {
        if dirSource == nil { dirSource = source(for: directory) }
        if fileSource == nil { fileSource = source(for: file) }
    }

    private func source(for url: URL) -> DispatchSourceFileSystemObject? {
        let fd = open(url.path, O_EVTONLY)
        guard fd >= 0 else { return nil }
        let src = DispatchSource.makeFileSystemObjectSource(
            fileDescriptor: fd, eventMask: [.write, .extend, .delete, .rename, .attrib], queue: .main)
        src.setEventHandler { [weak self, weak src] in
            guard let self, let src else { return }
            if !src.data.intersection([.delete, .rename]).isEmpty {
                // The watched node went away (atomic replace); re-open on the next event or poll.
                src.cancel()
                if src === self.dirSource { self.dirSource = nil }
                if src === self.fileSource { self.fileSource = nil }
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in self?.arm() }
            }
            self.changed()
        }
        src.setCancelHandler { close(fd) }
        src.resume()
        return src
    }

    private func stamp() -> (Date?, Int?) {
        let values = try? file.resourceValues(forKeys: [.contentModificationDateKey, .fileSizeKey])
        return (values?.contentModificationDate, values?.fileSize)
    }

    private func poll() {
        arm()
        let now = stamp()
        if now.0 != lastSeen.0 || now.1 != lastSeen.1 { changed() }
    }

    /// Coalesces bursts of events (a sync client may touch the file several times).
    private func changed() {
        lastSeen = stamp()
        pending?.cancel()
        let work = DispatchWorkItem { [weak self] in self?.onChange() }
        pending = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4, execute: work)
    }
}
