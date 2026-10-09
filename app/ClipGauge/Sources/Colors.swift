// Colors (v0.7) — ported from GrokGauge's Colors.swift / Palette.swift so both apps share the same level colors,
// presets (System, Colorblind-Friendly / Okabe–Ito), CIEDE2000 "too similar" warning and #RRGGBB JSON format.
// ClipGauge's levels are states, not percents: normal = processing / OK, warning = paused or needs review,
// critical = errors. (GrokGauge's percent thresholds don't apply and were skipped.)
import AppKit
import Foundation
import SwiftUI

/// An sRGB color (components 0…1) that round-trips through JSON as "#RRGGBB" / "#RRGGBBAA".
struct RGBAColor: Codable, Equatable, Hashable, Sendable {
    var red: Double
    var green: Double
    var blue: Double
    var alpha: Double

    init(red: Double, green: Double, blue: Double, alpha: Double = 1) {
        func c(_ v: Double) -> Double { v.isFinite ? min(max(v, 0), 1) : 0 }
        self.red = c(red)
        self.green = c(green)
        self.blue = c(blue)
        self.alpha = c(alpha)
    }

    init?(hex: String) {
        var s = hex.trimmingCharacters(in: .whitespaces)
        if s.hasPrefix("#") { s.removeFirst() }
        guard s.count == 6 || s.count == 8, let v = UInt64(s, radix: 16) else { return nil }
        let hasAlpha = s.count == 8
        let r = Double((v >> (hasAlpha ? 24 : 16)) & 0xFF) / 255
        let g = Double((v >> (hasAlpha ? 16 : 8)) & 0xFF) / 255
        let b = Double((v >> (hasAlpha ? 8 : 0)) & 0xFF) / 255
        let a = hasAlpha ? Double(v & 0xFF) / 255 : 1
        self.init(red: r, green: g, blue: b, alpha: a)
    }

    var hex: String {
        func b(_ v: Double) -> Int { Int((v * 255).rounded()) }
        let base = String(format: "#%02X%02X%02X", b(red), b(green), b(blue))
        return alpha < 1 ? base + String(format: "%02X", b(alpha)) : base
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        let s = try c.decode(String.self)
        guard let v = RGBAColor(hex: s) else {
            throw DecodingError.dataCorruptedError(in: c, debugDescription: "Expected #RRGGBB")
        }
        self = v
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        try c.encode(hex)
    }
}

/// The three level colors. `nil` means "the macOS system color" (green / orange / red), which
/// adapts to light, dark, and Increase Contrast automatically.
struct LevelColors: Codable, Equatable, Hashable, Sendable {
    var normal: RGBAColor?
    var warning: RGBAColor?
    var critical: RGBAColor?

    init(normal: RGBAColor? = nil, warning: RGBAColor? = nil, critical: RGBAColor? = nil) {
        self.normal = normal
        self.warning = warning
        self.critical = critical
    }

    static let system = LevelColors()

    /// Okabe–Ito palette: blue / orange / vermillion, distinguishable with the common forms of
    /// color blindness.
    static let colorblindFriendly = LevelColors(
        normal: RGBAColor(hex: "#0072B2"), warning: RGBAColor(hex: "#E69F00"), critical: RGBAColor(hex: "#D55E00"))

    /// Light-appearance sRGB values of systemGreen / systemOrange / systemRed, used for math.
    static let systemFallback = (normal: RGBAColor(hex: "#34C759")!,
                                        warning: RGBAColor(hex: "#FF9500")!,
                                        critical: RGBAColor(hex: "#FF3B30")!)

    var resolved: (normal: RGBAColor, warning: RGBAColor, critical: RGBAColor) {
        (normal ?? Self.systemFallback.normal, warning ?? Self.systemFallback.warning,
         critical ?? Self.systemFallback.critical)
    }

    func color(for level: Level) -> RGBAColor? {
        switch level {
        case .normal: return normal
        case .warning: return warning
        case .critical: return critical
        case .neutral: return nil
        }
    }

    /// Pairs of levels whose colors are too close to tell apart at a glance.
    func similarPairs(threshold: Double = ColorMath.similarityThreshold) -> [(Level, Level)] {
        let r = resolved
        let all: [(Level, RGBAColor)] = [(.normal, r.normal), (.warning, r.warning), (.critical, r.critical)]
        var out: [(Level, Level)] = []
        for i in 0..<all.count {
            for j in (i + 1)..<all.count where ColorMath.tooSimilar(all[i].1, all[j].1, threshold: threshold) {
                out.append((all[i].0, all[j].0))
            }
        }
        return out
    }
}

enum ColorMath {
    /// CIEDE2000 distance below which two colors read as "the same" in a small gauge.
    static let similarityThreshold = 12.0

    static func tooSimilar(_ a: RGBAColor, _ b: RGBAColor, threshold: Double = similarityThreshold) -> Bool {
        deltaE2000(a, b) < threshold
    }

    // MARK: sRGB -> CIELAB (D65)

    struct Lab: Equatable {
        let l, a, b: Double
        init(l: Double, a: Double, b: Double) {
            self.l = l
            self.a = a
            self.b = b
        }
    }

    static func lab(_ c: RGBAColor) -> Lab {
        func lin(_ v: Double) -> Double { v <= 0.04045 ? v / 12.92 : pow((v + 0.055) / 1.055, 2.4) }
        let r = lin(c.red), g = lin(c.green), b = lin(c.blue)
        let x = (0.4124564 * r + 0.3575761 * g + 0.1804375 * b) / 0.95047
        let y = (0.2126729 * r + 0.7151522 * g + 0.0721750 * b) / 1.0
        let z = (0.0193339 * r + 0.1191920 * g + 0.9503041 * b) / 1.08883
        func f(_ t: Double) -> Double { t > 216.0 / 24389.0 ? cbrt(t) : (24389.0 / 27.0 * t + 16) / 116 }
        let fx = f(x), fy = f(y), fz = f(z)
        return Lab(l: 116 * fy - 16, a: 500 * (fx - fy), b: 200 * (fy - fz))
    }

    /// CIEDE2000 color difference.
    static func deltaE2000(_ c1: RGBAColor, _ c2: RGBAColor) -> Double {
        deltaE2000(lab(c1), lab(c2))
    }

    static func deltaE2000(_ p: Lab, _ q: Lab) -> Double {
        let rad = Double.pi / 180
        let c1s = (p.a * p.a + p.b * p.b).squareRoot(), c2s = (q.a * q.a + q.b * q.b).squareRoot()
        let cBar = (c1s + c2s) / 2
        let g = 0.5 * (1 - (pow(cBar, 7) / (pow(cBar, 7) + pow(25, 7))).squareRoot())
        let a1 = (1 + g) * p.a, a2 = (1 + g) * q.a
        let cp1 = (a1 * a1 + p.b * p.b).squareRoot(), cp2 = (a2 * a2 + q.b * q.b).squareRoot()
        func hue(_ b: Double, _ a: Double) -> Double {
            if a == 0 && b == 0 { return 0 }
            let h = atan2(b, a) / rad
            return h < 0 ? h + 360 : h
        }
        let h1 = hue(p.b, a1), h2 = hue(q.b, a2)
        let dL = q.l - p.l, dC = cp2 - cp1
        var dh = 0.0
        if cp1 * cp2 != 0 {
            dh = h2 - h1
            if dh > 180 { dh -= 360 } else if dh < -180 { dh += 360 }
        }
        let dH = 2 * (cp1 * cp2).squareRoot() * sin(dh / 2 * rad)
        let lBar = (p.l + q.l) / 2, cpBar = (cp1 + cp2) / 2
        var hBar = h1 + h2
        if cp1 * cp2 != 0 {
            if abs(h1 - h2) <= 180 { hBar = (h1 + h2) / 2 }
            else { hBar = (h1 + h2 < 360) ? (h1 + h2 + 360) / 2 : (h1 + h2 - 360) / 2 }
        }
        let t = 1 - 0.17 * cos((hBar - 30) * rad) + 0.24 * cos(2 * hBar * rad)
            + 0.32 * cos((3 * hBar + 6) * rad) - 0.20 * cos((4 * hBar - 63) * rad)
        let dTheta = 30 * exp(-pow((hBar - 275) / 25, 2))
        let rc = 2 * (pow(cpBar, 7) / (pow(cpBar, 7) + pow(25, 7))).squareRoot()
        let sl = 1 + 0.015 * pow(lBar - 50, 2) / (20 + pow(lBar - 50, 2)).squareRoot()
        let sc = 1 + 0.045 * cpBar
        let sh = 1 + 0.015 * cpBar * t
        let rt = -sin(2 * dTheta * rad) * rc
        let l = dL / sl, c = dC / sc, h = dH / sh
        return (l * l + c * c + h * h + rt * c * h).squareRoot()
    }

    /// WCAG relative-luminance contrast ratio (1…21).
    static func contrastRatio(_ a: RGBAColor, _ b: RGBAColor) -> Double {
        func lum(_ c: RGBAColor) -> Double {
            func lin(_ v: Double) -> Double { v <= 0.03928 ? v / 12.92 : pow((v + 0.055) / 1.055, 2.4) }
            return 0.2126 * lin(c.red) + 0.7152 * lin(c.green) + 0.0722 * lin(c.blue)
        }
        let la = lum(a), lb = lum(b)
        return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)
    }
}


extension RGBAColor {
    var nsColor: NSColor { NSColor(srgbRed: red, green: green, blue: blue, alpha: alpha) }

    init?(nsColor: NSColor) {
        guard let c = nsColor.usingColorSpace(.sRGB) else { return nil }
        self.init(red: Double(c.redComponent), green: Double(c.greenComponent), blue: Double(c.blueComponent), alpha: 1)
    }
}

/// Level colors in effect. `nil` colors mean the system color (adapts to light/dark and Increase Contrast).
struct Palette: Equatable {
    var colors: LevelColors

    static let standard = Palette(colors: .system)
    /// What Level.color / Level.nsColor use everywhere (popover, windows, menu bar). Set by LayoutStore.shared.
    static var current = Palette.standard

    func nsColor(_ level: Level) -> NSColor {
        if let c = colors.color(for: level) { return c.nsColor }
        switch level {
        case .normal: return .systemGreen
        case .warning: return .systemOrange
        case .critical: return .systemRed
        case .neutral: return .systemGray
        }
    }

    func color(_ level: Level) -> Color { Color(nsColor: nsColor(level)) }
}

extension Level {
    var nsColor: NSColor { Palette.current.nsColor(self) }
    var color: Color { self == .neutral ? .gray : Palette.current.color(self) }
    var name: String {
        switch self {
        case .normal: return "normal"
        case .warning: return "warning"
        case .critical: return "critical"
        case .neutral: return "neutral"
        }
    }
}
