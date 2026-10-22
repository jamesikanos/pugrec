// Monitors, windows and focus in the shapes FORMAT.md defines (Hyprland's, mapped).
//
// Coordinates: CoreGraphics global points, origin top-left of the main display - the same
// shape as Hyprland's logical coordinates. Monitor width/height are pixels, scale = pixels
// per point (2 on a Retina panel).
//
// macOS doesn't expose Spaces, so a workspace is just "on screen" (1) or not (-1): every
// monitor shows workspace 1, special 0. A minimised window, one on another Space or one of
// a hidden app is hidden=true, workspace -1, and its title is blank (not recorded).
import AppKit
import AVFoundation
import CoreGraphics

private let ownName = ProcessInfo.processInfo.processName

func displayID(_ s: NSScreen) -> CGDirectDisplayID {
    (s.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.uint32Value ?? 0
}

func cursorPoint() -> CGPoint {
    CGEvent(source: nil)?.location ?? .zero
}

func screenLocked() -> Bool {
    guard let d = CGSessionCopyCurrentDictionary() as? [String: Any] else { return false }
    return (d["CGSSessionScreenIsLocked"] as? Bool) ?? false
}

/// [monitor state]. Names are NSScreen.localizedName, made unique with " #2" etc.
func monitors() -> [[String: Any]] {
    let cursor = cursorPoint()
    let locked = screenLocked()
    var seen: [String: Int] = [:]
    return NSScreen.screens.map { s in
        let id = displayID(s)
        let b = CGDisplayBounds(id)
        let mode = CGDisplayCopyDisplayMode(id)
        let pw = mode?.pixelWidth ?? Int(b.width)
        let ph = mode?.pixelHeight ?? Int(b.height)
        var name = s.localizedName
        seen[name, default: 0] += 1
        if seen[name]! > 1 { name += " #\(seen[name]!)" }
        return [
            "name": name, "id": Int(id),
            "x": Int(b.origin.x), "y": Int(b.origin.y),
            "width": pw, "height": ph,
            "scale": b.width > 0 ? Double(pw) / Double(b.width) : 1.0,
            "transform": 0, "workspace": 1, "special": 0,
            "focused": b.contains(cursor),
            "locked": locked,
        ]
    }
}

struct WindowList {
    var windows: [[String: Any]]
    var focus: String?
}

/// Every normal (layer 0) window, front to back, and the focused one: the frontmost
/// app's frontmost on-screen window. (Accessibility would say which exactly, but needs one
/// more permission; the topmost one is right nearly always.)
/// `exclude`: an app kept out of the video (the control panel): its windows aren't listed, and
/// while it's frontmost (you just pressed one of its keys), focus is the topmost window that
/// *is* in the picture - so `focused` never points at something the viewer can't see.
func windowList(exclude: pid_t? = nil) -> WindowList {
    let opts: CGWindowListOption = [.optionAll, .excludeDesktopElements]
    guard let info = CGWindowListCopyWindowInfo(opts, kCGNullWindowID) as? [[String: Any]] else {
        return WindowList(windows: [], focus: nil)
    }
    let screens = NSScreen.screens.map { CGDisplayBounds(displayID($0)) }
    var front = NSWorkspace.shared.frontmostApplication?.processIdentifier
    if front == exclude { front = nil }
    var topmost: String?
    var bundles: [pid_t: String] = [:]
    var out: [[String: Any]] = []
    var focus: String?
    for w in info {
        guard (w[kCGWindowLayer as String] as? Int) == 0,
              let num = w[kCGWindowNumber as String] as? Int,
              let pid = w[kCGWindowOwnerPID as String] as? pid_t,
              let bd = w[kCGWindowBounds as String] as? NSDictionary,
              let r = CGRect(dictionaryRepresentation: bd),
              r.width >= 40, r.height >= 40,
              ((w[kCGWindowAlpha as String] as? Double) ?? 1) > 0
        else { continue }
        let owner = w[kCGWindowOwnerName as String] as? String ?? ""
        if owner == ownName || pid == exclude { continue }  // our countdown overlay, the control panel
        if bundles[pid] == nil {
            bundles[pid] = NSRunningApplication(processIdentifier: pid)?.bundleIdentifier ?? owner
        }
        let onscreen = (w[kCGWindowIsOnscreen as String] as? Bool) ?? false
        var title = w[kCGWindowName as String] as? String ?? ""
        // Off-screen windows with no title are mostly helper/utility surfaces: noise.
        if !onscreen && title.isEmpty { continue }
        // Privacy: an off-screen window's title isn't recorded (other Spaces, minimised, hidden
        // apps: client names, tokens in titles). It's kept as a window, so a shot can follow it
        // back on screen; its title comes back with it, when it's in the picture anyway.
        if !onscreen { title = "" }
        let center = CGPoint(x: r.midX, y: r.midY)
        let address = String(format: "0x%x", num)
        if focus == nil && onscreen && pid == front { focus = address }
        if topmost == nil && onscreen { topmost = address }
        out.append([
            "address": address,
            "class": bundles[pid]!,
            "title": title,
            "at": [Int(r.origin.x), Int(r.origin.y)],
            "size": [Int(r.width), Int(r.height)],
            "workspace": onscreen ? 1 : -1,
            "floating": true,
            "fullscreen": 0,
            "monitor": screens.firstIndex { $0.contains(center) } ?? 0,
            "mapped": true,
            "hidden": !onscreen,
            "pid": Int(pid),
            "app": owner,
        ])
    }
    return WindowList(windows: out, focus: front == nil ? topmost : focus)
}

func devices() -> [String: Any] {
    let cams = AVCaptureDevice.DiscoverySession(
        deviceTypes: [.builtInWideAngleCamera, .external, .continuityCamera, .deskViewCamera],
        mediaType: .video, position: .unspecified).devices
    let mics = AVCaptureDevice.DiscoverySession(
        deviceTypes: [.microphone, .external], mediaType: .audio, position: .unspecified).devices
    func row(_ d: AVCaptureDevice) -> [String: Any] {
        ["id": d.uniqueID, "name": d.localizedName, "model": d.modelID]
    }
    return [
        "cameras": cams.map(row),
        "mics": mics.map(row),
        "default_camera": AVCaptureDevice.default(for: .video).map(row) as Any,
        "default_mic": AVCaptureDevice.default(for: .audio).map(row) as Any,
    ]
}

/// A device by unique ID, or by (case-insensitive substring of) its name.
func findDevice(_ want: String, _ type: AVMediaType) -> AVCaptureDevice? {
    let all = AVCaptureDevice.DiscoverySession(
        deviceTypes: type == .video ? [.builtInWideAngleCamera, .external, .continuityCamera, .deskViewCamera]
                                    : [.microphone, .external],
        mediaType: type, position: .unspecified).devices
    return all.first { $0.uniqueID == want }
        ?? all.first { $0.localizedName.lowercased() == want.lowercased() }
        ?? all.first { $0.localizedName.lowercased().contains(want.lowercased()) }
}
