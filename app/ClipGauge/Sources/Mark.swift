import AppKit
import SwiftUI

/// ClipGauge mark: GrokGauge's open 300° gauge ring (same stroke ratio) with a play triangle inside.
enum ClipMarkGeometry {
    /// Stroke-based path in a y-down coordinate space (SwiftUI / flipped NSImage).
    static func path(in rect: CGRect) -> CGPath {
        let s = min(rect.width, rect.height)
        let ox = rect.midX - s / 2, oy = rect.midY - s / 2
        func p(_ x: CGFloat, _ y: CGFloat) -> CGPoint { CGPoint(x: ox + x * s, y: oy + y * s) }
        let path = CGMutablePath()
        let r: CGFloat = 0.36
        var first = true
        for deg in stride(from: -15.0, through: 285.0, by: 2.5) {
            let a = deg * .pi / 180
            let pt = p(0.5 + r * CGFloat(cos(a)), 0.5 + r * CGFloat(sin(a)))
            if first { path.move(to: pt); first = false } else { path.addLine(to: pt) }
        }
        // Play triangle, optically centred.
        path.move(to: p(0.43, 0.36))
        path.addLine(to: p(0.65, 0.50))
        path.addLine(to: p(0.43, 0.64))
        path.closeSubpath()
        return path
    }

    static let lineWidthRatio: CGFloat = 0.105
}

struct ClipMarkShape: Shape {
    func path(in rect: CGRect) -> Path { Path(ClipMarkGeometry.path(in: rect)) }
}

struct ClipMarkView: View {
    var size: CGFloat = 18
    var body: some View {
        ClipMarkShape()
            .stroke(style: StrokeStyle(lineWidth: size * ClipMarkGeometry.lineWidthRatio, lineCap: .round, lineJoin: .round))
            .frame(width: size, height: size)
            .accessibilityHidden(true)
    }
}

enum ClipMarkImage {
    /// Template image for the menu bar: adapts to light/dark/tinted menu bars automatically.
    static func template(size: CGFloat = 18) -> NSImage {
        let image = NSImage(size: NSSize(width: size, height: size), flipped: true) { rect in
            guard let ctx = NSGraphicsContext.current?.cgContext else { return false }
            let inset = rect.insetBy(dx: size * 0.04, dy: size * 0.04)
            ctx.addPath(ClipMarkGeometry.path(in: inset))
            ctx.setLineWidth(size * ClipMarkGeometry.lineWidthRatio * 1.15)
            ctx.setLineCap(.round)
            ctx.setLineJoin(.round)
            ctx.setStrokeColor(NSColor.black.cgColor)
            ctx.strokePath()
            return true
        }
        image.isTemplate = true
        image.accessibilityDescription = "ClipGauge"
        return image
    }

    static func tinted(size: CGFloat = 18, _ color: NSColor) -> NSImage {
        let base = template(size: size)
        let out = NSImage(size: base.size, flipped: false) { rect in
            base.draw(in: rect)
            color.set()
            rect.fill(using: .sourceAtop)
            return true
        }
        out.isTemplate = false
        out.accessibilityDescription = "ClipGauge"
        return out
    }
}
