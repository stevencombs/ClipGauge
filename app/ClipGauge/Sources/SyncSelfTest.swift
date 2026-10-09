// `ClipGauge --sync-selftest`: exercises settings sync end to end in a temporary folder (never the real sync folder,
// never the app's own preferences). Prints one line per check; exit code 0 when everything passes.
import Foundation

enum SyncSelfTest {
    static func run() -> Int32 {
        var failures = 0
        func check(_ name: String, _ ok: Bool, _ extra: String = "") {
            print("\(ok ? "ok  " : "FAIL") \(name)\(extra.isEmpty ? "" : " — " + extra)")
            if !ok { failures += 1 }
        }
        let fm = FileManager.default
        let tmp = fm.temporaryDirectory.appendingPathComponent("clipgauge-sync-selftest-\(UUID().uuidString)", isDirectory: true)
        defer { try? fm.removeItem(at: tmp) }
        try? fm.createDirectory(at: tmp, withIntermediateDirectories: true)
        let file = SettingsSync.fileURL(inSyncFolder: tmp)
        let backups = tmp.appendingPathComponent("backups", isDirectory: true)

        // 1. JSON round trip (envelope, dates, colors, hot key)
        var a = ClipGaugeSettings()
        a.colors = .colorblindFriendly
        a.hotKey = HotKey(keyCode: 8, control: true, option: true, shift: true)
        a.sections.swapAt(0, 1)
        a.modifiedAt = SettingsSync.roundedNow()
        a.groupModified = ["colors": a.modifiedAt, "layout": a.modifiedAt, "menuBar": a.modifiedAt]
        let rt = (try? SettingsSync.encodeEnvelope(a)).flatMap { try? SettingsSync.decode($0) }
        check("round trip keeps every value", rt == a)

        // 2. no file yet -> write
        let m0 = SettingsSync.merge(local: a, remote: nil)
        check("missing file is created", m0.writeFile)
        do { try SettingsSync.write(a, to: file, backupDir: backups) } catch { check("write", false, "\(error)") }
        check("file written under ClipGauge/settings.json", fm.fileExists(atPath: file.path) && file.path.hasSuffix("/ClipGauge/settings.json"))

        // 3. a fresh Mac (never changed anything) takes everything from the file and doesn't rewrite it
        let fresh = ClipGaugeSettings()
        let remote = try? SettingsSync.read(from: file)
        let m1 = SettingsSync.merge(local: fresh, remote: remote)
        check("fresh Mac adopts the file", m1.merged.sameContent(as: a), SettingsSync.names(m1.adopted))
        check("fresh Mac doesn't overwrite the file", !m1.writeFile)

        // 4. different groups changed on two Macs -> both survive
        var macA = a, macB = a
        let t1 = a.modifiedAt.addingTimeInterval(10), t2 = a.modifiedAt.addingTimeInterval(20)
        macA.menuBarMode = .iconOnly; macA.groupModified["menuBar"] = t1; macA.modifiedAt = t1
        macB.colors = .system; macB.groupModified["colors"] = t2; macB.modifiedAt = t2
        let m2 = SettingsSync.merge(local: macA, remote: macB)
        check("merge keeps this Mac's newer menu bar", m2.merged.menuBarMode == .iconOnly)
        check("merge takes the file's newer colors", m2.merged.colors == .system)
        check("merged result is written back", m2.writeFile, m2.note ?? "")

        // 5. same group, newer remote wins; tie keeps local
        var older = a; older.colors = .system; older.groupModified["colors"] = a.modifiedAt.addingTimeInterval(-5)
        check("newer file wins a group", SettingsSync.merge(local: older, remote: a).merged.colors == .colorblindFriendly)
        var tie = a; tie.colors = .system
        check("a tie keeps this Mac's value", SettingsSync.merge(local: tie, remote: a).merged.colors == .system)

        // 6. unknown keys from a newer build are kept, and the old file is backed up first
        if var obj = (try? Data(contentsOf: file)).flatMap({ try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }) {
            var inner = obj["settings"] as? [String: Any] ?? [:]
            inner["futureOption"] = "keep me"
            obj["settings"] = inner
            obj["extraTopLevel"] = 42
            try? JSONSerialization.data(withJSONObject: obj, options: [.prettyPrinted]).write(to: file)
        }
        do { try SettingsSync.write(m2.merged, to: file, backupDir: backups) } catch { check("write merged", false, "\(error)") }
        let after = (try? Data(contentsOf: file)).flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] } ?? [:]
        check("unknown settings key kept", (after["settings"] as? [String: Any])?["futureOption"] as? String == "keep me")
        check("unknown top-level key kept", after["extraTopLevel"] as? Int == 42)
        check("app name is ClipGauge", after["app"] as? String == "ClipGauge")
        let nBackups = ((try? fm.contentsOfDirectory(atPath: backups.path)) ?? []).filter { $0.hasPrefix("settings-") }.count
        check("previous file backed up before replacing", nBackups >= 1, "\(nBackups) backup(s)")
        check("merged file reads back", (try? SettingsSync.read(from: file))?.sameContent(as: m2.merged) == true)

        // 7. a file that isn't ClipGauge's (e.g. GrokGauge's) is never touched
        let foreign = tmp.appendingPathComponent("Foreign/ClipGauge/settings.json")
        try? fm.createDirectory(at: foreign.deletingLastPathComponent(), withIntermediateDirectories: true)
        let gg = Data("{\"app\":\"GrokGauge\",\"schema\":1,\"settings\":{}}".utf8)
        try? gg.write(to: foreign)
        var refused = false
        do { try SettingsSync.write(a, to: foreign, backupDir: nil) } catch { refused = true }
        check("refuses to overwrite another app's file", refused && (try? Data(contentsOf: foreign)) == gg)
        var readFailed = false
        do { _ = try SettingsSync.read(from: foreign) } catch { readFailed = true }
        check("won't read another app's file as settings", readFailed)

        // 8. v0.6.1 layout preferences still decode
        let legacy = Data("{\"sections\":[{\"id\":\"inbox\",\"visible\":true},{\"id\":\"bogus\",\"visible\":true}],\"actions\":[],\"menuBarMode\":\"iconOnly\",\"showETA\":true}".utf8)
        let old = try? JSONDecoder().decode(ClipGaugeSettings.self, from: legacy)
        check("v0.6.1 settings migrate", old?.menuBarMode == .iconOnly && old?.showETA == true && old?.isVisible(.inbox) == true
              && old?.sections.count == PopoverSection.allCases.count)

        // 9. two stores (two "Macs") syncing through the same folder, each with its own throwaway preferences
        let folder2 = tmp.appendingPathComponent("Shared", isDirectory: true)
        try? fm.createDirectory(at: folder2, withIntermediateDirectories: true)
        let suiteA = "clipgauge.selftest.A.\(UUID().uuidString)", suiteB = "clipgauge.selftest.B.\(UUID().uuidString)"
        if let dA = UserDefaults(suiteName: suiteA), let dB = UserDefaults(suiteName: suiteB) {
            defer { dA.removePersistentDomain(forName: suiteA); dB.removePersistentDomain(forName: suiteB) }
            let A = LayoutStore(defaults: dA, persist: true, shared: false)
            let B = LayoutStore(defaults: dB, persist: true, shared: false)
            A.settings.colors = .colorblindFriendly
            A.setSyncFolder(folder2)
            B.setSyncFolder(folder2)
            check("second Mac picks up the first Mac's colors", B.settings.colors == .colorblindFriendly)
            Thread.sleep(forTimeInterval: 0.01)
            B.settings.menuBarMode = .percentOnly
            A.settings.notifyUpdates = false
            B.reconcile(); A.reconcile(); B.reconcile()
            check("changes made on both Macs merge", A.settings.menuBarMode == .percentOnly && !A.settings.notifyUpdates
                  && !B.settings.notifyUpdates && B.settings.menuBarMode == .percentOnly)
            check("sync folder choice stays per Mac", dA.string(forKey: LayoutStore.syncFolderKey) != nil
                  && !(String(data: (try? Data(contentsOf: SettingsSync.fileURL(inSyncFolder: folder2))) ?? Data(), encoding: .utf8) ?? "").contains(folder2.path))
            A.setSyncFolder(nil); B.setSyncFolder(nil)
        } else {
            check("temporary preferences", false)
        }

        // 9b. a Mac that only changed its colors must not push its default layout over a customised shared file
        var shared = ClipGaugeSettings()
        shared.sections[0].visible = false
        shared.modifiedAt = SettingsSync.roundedNow().addingTimeInterval(-100)
        shared.groupModified = ["layout": shared.modifiedAt]
        var newMac = ClipGaugeSettings()
        newMac.pinGroupStamps()
        newMac.colors = .colorblindFriendly
        newMac.groupModified["colors"] = SettingsSync.roundedNow()
        newMac.modifiedAt = newMac.groupModified["colors"]!
        let m9 = SettingsSync.merge(local: newMac, remote: shared)
        check("untouched groups never beat the shared file", m9.merged.sections[0].visible == false && m9.merged.colors == .colorblindFriendly)

        // 10. nothing but preferences in the file
        let text = String(data: (try? Data(contentsOf: file)) ?? Data(), encoding: .utf8) ?? ""
        check("file holds preferences only", !text.contains("/Volumes") && !text.contains("/Users") && !text.contains(".MP4") && !text.contains("models"))

        print(failures == 0 ? "sync selftest: all passed" : "sync selftest: \(failures) failed")
        return failures == 0 ? 0 : 1
    }
}
