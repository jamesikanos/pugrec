// The countdown overlay (macOS twin of overlay/countdown.qml): dims the screen, a spinner
// while the recorder starts, then 3-2-1 in a circle.
//   click the circle / Pause / Space: pause or resume      Cancel / Esc: discard
// Spins until the recorder writes "go" to --ready-file, then counts. Exit 0 = go, 3 = cancel.
// --mode stopping: a small "Saving…" spinner, click-through, no keyboard; the recorder closes it.
//
// Same escape hatches as the Linux one: the dim layer never takes the mouse (only the
// controls do), Esc always cancels, it starts by itself after 2 minutes, and the recorder
// kills it at 2:10 whatever state it's in.
import AppKit

private let ink = NSColor(srgbRed: 0x16 / 255.0, green: 0x17 / 255.0, blue: 0x1a / 255.0, alpha: 1)
private let cream = NSColor(srgbRed: 0xf1 / 255.0, green: 0xe4 / 255.0, blue: 0xd9 / 255.0, alpha: 1)
private let orange = NSColor(srgbRed: 1, green: 0x6b / 255.0, blue: 0x2c / 255.0, alpha: 1)

private final class KeyPanel: NSPanel {
    override var canBecomeKey: Bool { true }
}

private final class Controls: NSView {
    let total: Double
    let stopping: Bool
    let waiting: String
    var left: Double
    var ready: Bool
    var paused = false
    var spin = 0.0
    var finish: (Bool) -> Void = { _ in }

    let circle = NSRect(x: 30, y: 150, width: 300, height: 300)
    var pauseButton: NSRect { NSRect(x: 22, y: 20, width: 150, height: 48) }
    var cancelButton: NSRect { ready ? NSRect(x: 188, y: 20, width: 150, height: 48) : NSRect(x: 105, y: 20, width: 150, height: 48) }

    init(total: Double, stopping: Bool, waiting: String, ready: Bool) {
        self.total = total
        self.left = total
        self.stopping = stopping
        self.waiting = waiting
        self.ready = ready
        super.init(frame: NSRect(x: 0, y: 0, width: 360, height: 480))
    }

    required init?(coder: NSCoder) { fatalError() }

    override var acceptsFirstResponder: Bool { true }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    private func text(_ s: String, size: CGFloat, weight: NSFont.Weight, color: NSColor, center: NSPoint) {
        let attrs: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: size, weight: weight), .foregroundColor: color]
        let a = NSAttributedString(string: s, attributes: attrs)
        let sz = a.size()
        a.draw(at: NSPoint(x: center.x - sz.width / 2, y: center.y - sz.height / 2))
    }

    private func button(_ r: NSRect, _ label: String, filled: Bool) {
        let p = NSBezierPath(roundedRect: r.insetBy(dx: 1, dy: 1), xRadius: 6, yRadius: 6)
        if filled { ink.setFill(); p.fill() }
        (filled ? orange : cream).setStroke()
        p.lineWidth = 2
        p.stroke()
        text(label, size: 19, weight: .semibold, color: cream, center: NSPoint(x: r.midX, y: r.midY))
    }

    override func draw(_ dirty: NSRect) {
        let c = NSPoint(x: circle.midX, y: circle.midY)
        let disc = NSBezierPath(ovalIn: circle.insetBy(dx: 1, dy: 1))
        ink.withAlphaComponent(0.92).setFill()
        disc.fill()
        NSColor(white: 1, alpha: 0.08).setStroke()
        disc.lineWidth = 2
        disc.stroke()
        let ring = NSBezierPath()
        let r = circle.width / 2 - 14
        if !ready {  // spinner: a quarter arc going round (AppKit angles: degrees, counter-clockwise)
            let a = -spin * 180 / .pi
            ring.appendArc(withCenter: c, radius: r, startAngle: a, endAngle: a - 90, clockwise: true)
        } else {
            let frac = left - floor(left - 1e-6)  // 1 -> 0 within a second
            ring.appendArc(withCenter: c, radius: r, startAngle: 90, endAngle: 90 - 360 * frac, clockwise: true)
        }
        ring.lineWidth = 12
        ring.lineCapStyle = .round
        orange.setStroke()
        ring.stroke()
        if ready {
            text(paused ? "❚❚" : "\(max(1, Int(ceil(left - 1e-6))))", size: paused ? 96 : 150, weight: .bold, color: cream, center: c)
        }
        let label = !ready ? waiting : paused ? "Paused - click the circle to continue" : "Recording starts"
        text(label, size: 20, weight: .regular, color: cream.withAlphaComponent(0.85), center: NSPoint(x: 180, y: 112))
        if !stopping {
            if ready { button(pauseButton, paused ? "Resume" : "Pause", filled: true) }
            button(cancelButton, "Cancel", filled: false)
        }
    }

    override func mouseDown(with e: NSEvent) {
        if stopping { return }
        let p = convert(e.locationInWindow, from: nil)
        if cancelButton.contains(p) {
            finish(false)
        } else if ready && (pauseButton.contains(p) || NSBezierPath(ovalIn: circle).contains(p)) {
            paused.toggle()
            needsDisplay = true
        }
    }

    override func keyDown(with e: NSEvent) {
        if stopping { return }
        if e.keyCode == 53 { finish(false) }                          // Esc
        else if e.keyCode == 49 && ready { paused.toggle(); needsDisplay = true }  // Space
    }

    override func resetCursorRects() {
        if !stopping { addCursorRect(bounds, cursor: .pointingHand) }
    }
}

func runOverlay(_ args: [String]) -> Never {
    let app = NSApplication.shared
    let stopping = arg("--mode", args) == "stopping"
    app.setActivationPolicy(stopping ? .prohibited : .accessory)
    let readyFile = arg("--ready-file", args)
    let total = Double(arg("--count", args) ?? "3") ?? 3
    let want = arg("--display", args).flatMap { UInt32($0) }
    let screen = NSScreen.screens.first { displayID($0) == want } ?? NSScreen.main ?? NSScreen.screens[0]
    var done = false
    func finish(_ go: Bool) -> Never {
        done = true
        exit(go ? 0 : 3)
    }

    // The dim: full screen, never takes the mouse or the keyboard.
    let dim = NSWindow(contentRect: screen.frame, styleMask: .borderless, backing: .buffered, defer: false)
    dim.level = .screenSaver
    dim.isOpaque = false
    dim.backgroundColor = ink.withAlphaComponent(stopping ? 0.25 : 0.6)
    dim.ignoresMouseEvents = true
    dim.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
    dim.orderFrontRegardless()

    let view = Controls(total: total, stopping: stopping,
                        waiting: arg("--waiting", args) ?? "Getting ready…",
                        ready: !stopping && readyFile == nil)
    view.finish = { go in finish(go) }
    let f = screen.frame
    let panel = KeyPanel(contentRect: NSRect(x: f.midX - 180, y: f.midY - 240, width: 360, height: 480),
                         styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
    panel.level = .screenSaver + 1
    panel.isOpaque = false
    panel.backgroundColor = .clear
    panel.hasShadow = false
    panel.contentView = view
    panel.ignoresMouseEvents = stopping
    panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
    if stopping {
        panel.orderFrontRegardless()
    } else {
        app.activate(ignoringOtherApps: true)
        panel.makeKeyAndOrderFront(nil)
        panel.makeFirstResponder(view)
    }

    Timer.scheduledTimer(withTimeInterval: 1.0 / 60, repeats: true) { _ in
        if !view.ready {
            view.spin = (view.spin + 0.12).truncatingRemainder(dividingBy: 2 * .pi)
            view.needsDisplay = true
        }
    }
    if let readyFile, !stopping {
        Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { t in
            if (try? String(contentsOfFile: readyFile, encoding: .utf8))?.trimmingCharacters(in: .whitespacesAndNewlines) == "go" {
                view.ready = true
                view.needsDisplay = true
                t.invalidate()
            }
        }
    }
    if !stopping {
        Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { _ in
            guard view.ready, !view.paused, !done else { return }
            view.left = max(0, view.left - 0.05)
            view.needsDisplay = true
            if view.left <= 0.001 { finish(true) }
        }
    }
    Timer.scheduledTimer(withTimeInterval: 120, repeats: false) { _ in finish(true) }  // never hold the keyboard long
    signal(SIGTERM, SIG_DFL)
    app.run()
    exit(0)
}
