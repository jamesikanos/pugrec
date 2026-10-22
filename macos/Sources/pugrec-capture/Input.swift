// Clicks, wheel and (opt-in) keys: a listen-only CGEventTap. It never alters or blocks an
// event. Mouse needs no permission; keys need Input Monitoring for the app that started
// this (the terminal, or the control app), granted once in System Settings - no password
// per recording, unlike the Linux root helper.
//
// Codes use the evdev names FORMAT.md defines (KEY_A, BTN_LEFT, ...) so sessions read the
// same whichever OS recorded them. Command is KEY_LEFTMETA/RIGHTMETA ("super" in mods).
import CoreGraphics
import Foundation

private let keyNames: [Int64: String] = [
    0: "KEY_A", 11: "KEY_B", 8: "KEY_C", 2: "KEY_D", 14: "KEY_E", 3: "KEY_F", 5: "KEY_G", 4: "KEY_H",
    34: "KEY_I", 38: "KEY_J", 40: "KEY_K", 37: "KEY_L", 46: "KEY_M", 45: "KEY_N", 31: "KEY_O", 35: "KEY_P",
    12: "KEY_Q", 15: "KEY_R", 1: "KEY_S", 17: "KEY_T", 32: "KEY_U", 9: "KEY_V", 13: "KEY_W", 7: "KEY_X",
    16: "KEY_Y", 6: "KEY_Z",
    29: "KEY_0", 18: "KEY_1", 19: "KEY_2", 20: "KEY_3", 21: "KEY_4", 23: "KEY_5", 22: "KEY_6", 26: "KEY_7",
    28: "KEY_8", 25: "KEY_9",
    36: "KEY_ENTER", 76: "KEY_KPENTER", 48: "KEY_TAB", 49: "KEY_SPACE", 51: "KEY_BACKSPACE", 117: "KEY_DELETE",
    53: "KEY_ESC", 27: "KEY_MINUS", 24: "KEY_EQUAL", 33: "KEY_LEFTBRACE", 30: "KEY_RIGHTBRACE",
    42: "KEY_BACKSLASH", 41: "KEY_SEMICOLON", 39: "KEY_APOSTROPHE", 50: "KEY_GRAVE", 43: "KEY_COMMA",
    47: "KEY_DOT", 44: "KEY_SLASH", 10: "KEY_102ND",
    123: "KEY_LEFT", 124: "KEY_RIGHT", 125: "KEY_DOWN", 126: "KEY_UP",
    115: "KEY_HOME", 119: "KEY_END", 116: "KEY_PAGEUP", 121: "KEY_PAGEDOWN",
    122: "KEY_F1", 120: "KEY_F2", 99: "KEY_F3", 118: "KEY_F4", 96: "KEY_F5", 97: "KEY_F6", 98: "KEY_F7",
    100: "KEY_F8", 101: "KEY_F9", 109: "KEY_F10", 103: "KEY_F11", 111: "KEY_F12",
    // modifiers (flagsChanged)
    56: "KEY_LEFTSHIFT", 60: "KEY_RIGHTSHIFT", 59: "KEY_LEFTCTRL", 62: "KEY_RIGHTCTRL",
    58: "KEY_LEFTALT", 61: "KEY_RIGHTALT", 55: "KEY_LEFTMETA", 54: "KEY_RIGHTMETA",
    57: "KEY_CAPSLOCK", 63: "KEY_FN",
]

private let modifierFlag: [Int64: CGEventFlags] = [
    56: .maskShift, 60: .maskShift, 59: .maskControl, 62: .maskControl,
    58: .maskAlternate, 61: .maskAlternate, 55: .maskCommand, 54: .maskCommand,
    57: .maskAlphaShift, 63: .maskSecondaryFn,
]

final class InputTap {
    static var current: InputTap?
    let keys: Bool
    private var tap: CFMachPort?

    init(keys: Bool) {
        self.keys = keys
    }

    /// Start on the main run loop. Returns whether keys are really being recorded
    /// (false when Input Monitoring isn't granted: clicks only, with a notice).
    func start() -> (ok: Bool, keys: Bool) {
        var wantKeys = keys
        if wantKeys && !CGPreflightListenEventAccess() {
            CGRequestListenEventAccess()  // shows the system prompt / adds the app to the list
            Out.notice("keys unavailable: allow Input Monitoring for this terminal in System Settings > " +
                       "Privacy & Security, then record again; recording clicks only")
            wantKeys = false
        }
        var mask: CGEventMask = 0
        for t: CGEventType in [.leftMouseDown, .leftMouseUp, .rightMouseDown, .rightMouseUp,
                               .otherMouseDown, .otherMouseUp, .scrollWheel] {
            mask |= 1 << t.rawValue
        }
        if wantKeys {
            for t: CGEventType in [.keyDown, .keyUp, .flagsChanged] { mask |= 1 << t.rawValue }
        }
        InputTap.current = self
        guard let tap = CGEvent.tapCreate(tap: .cgSessionEventTap, place: .tailAppendEventTap, options: .listenOnly,
                                          eventsOfInterest: mask, callback: inputCallback, userInfo: nil) else {
            Out.notice("clicks unavailable: couldn't create an event tap")
            return (false, false)
        }
        self.tap = tap
        let src = CFMachPortCreateRunLoopSource(nil, tap, 0)
        CFRunLoopAddSource(CFRunLoopGetMain(), src, .commonModes)
        CGEvent.tapEnable(tap: tap, enable: true)
        return (true, wantKeys)
    }

    func reenable() {
        if let tap { CGEvent.tapEnable(tap: tap, enable: true) }
    }

    func handle(_ type: CGEventType, _ e: CGEvent) {
        let mono = nowNs()
        switch type {
        case .leftMouseDown, .rightMouseDown, .otherMouseDown, .leftMouseUp, .rightMouseUp, .otherMouseUp:
            let n = e.getIntegerValueField(.mouseEventButtonNumber)
            let code = [0: "BTN_LEFT", 1: "BTN_RIGHT", 2: "BTN_MIDDLE", 3: "BTN_SIDE", 4: "BTN_EXTRA"][n] ?? "BTN_\(n)"
            let down = [.leftMouseDown, .rightMouseDown, .otherMouseDown].contains(type)
            Out.emit("button", mono: mono, ["code": code, "value": down ? 1 : 0])
        case .scrollWheel:
            // Line steps, sign = direction (as REL_WHEEL). Trackpads send many small pixel
            // scrolls with 0 lines: those are skipped, the cursor and video still show them.
            let v = e.getIntegerValueField(.scrollWheelEventDeltaAxis1)
            let h = e.getIntegerValueField(.scrollWheelEventDeltaAxis2)
            if v != 0 { Out.emit("wheel", mono: mono, ["code": "vertical", "value": v]) }
            if h != 0 { Out.emit("wheel", mono: mono, ["code": "horizontal", "value": h]) }
        case .keyDown, .keyUp:
            let kc = e.getIntegerValueField(.keyboardEventKeycode)
            let rep = e.getIntegerValueField(.keyboardEventAutorepeat) != 0
            Out.emit("key", mono: mono, ["code": keyNames[kc] ?? "KEY_MAC_\(kc)",
                                         "value": type == .keyUp ? 0 : (rep ? 2 : 1)])
        case .flagsChanged:
            let kc = e.getIntegerValueField(.keyboardEventKeycode)
            guard let name = keyNames[kc], let flag = modifierFlag[kc] else { return }
            Out.emit("key", mono: mono, ["code": name, "value": e.flags.contains(flag) ? 1 : 0])
        default:
            break
        }
    }
}

private func inputCallback(proxy: CGEventTapProxy, type: CGEventType, event: CGEvent,
                           refcon: UnsafeMutableRawPointer?) -> Unmanaged<CGEvent>? {
    if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
        InputTap.current?.reenable()
    } else {
        InputTap.current?.handle(type, event)
    }
    return Unmanaged.passUnretained(event)
}
