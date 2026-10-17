// pugrec-capture: what `pugrec record` runs on macOS, in place of wf-recorder, the camera
// ffmpeg, the evdev root helper and Hyprland IPC. Started and steered by recorder.py.
//
//   pugrec-capture devices          JSON: cameras, mics, defaults
//   pugrec-capture state            JSON: monitors, windows, focus, cursor, permissions
//   pugrec-capture record ...       JSON lines on stdout; steered by lines on stdin:
//       camera   start the camera        -> {"kind":"camera_start", "mono": first frame, ...}
//       screen   start screen (+ mic)    -> {"kind":"screen_start", "mono": first frame, ...}
//                                           then cursor / windows / button / wheel / key / mic_mute
//       stop     (or stdin closed, SIGINT, SIGTERM): finalise both files -> {"kind":"stopped"}, exit 0
//   pugrec-capture overlay ...      the countdown overlay; exit 0 = go, 3 = cancel
//
// Every line has "mono": host-clock ns, the clock Python's time.monotonic_ns() reads.
import AppKit
import AVFoundation
import Foundation

func arg(_ name: String, _ args: [String]) -> String? {
    guard let i = args.firstIndex(of: name), i + 1 < args.count else { return nil }
    return args[i + 1]
}

func printJSON(_ obj: Any) {
    let data = try! JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys, .prettyPrinted])
    print(String(data: data, encoding: .utf8)!)
}

final class Recording {
    let args: [String]
    var screen: ScreenRecorder?
    var camera: CameraRecorder?
    var input: InputTap?
    var timers: [Timer] = []
    var stopping = false

    init(args: [String]) {
        self.args = args
    }

    func begin() {
        let mode = arg("--input", args) ?? "mouse"
        var info: [String: Any] = ["input": false, "keys": false, "devices": []]
        if mode != "none" {
            let tap = InputTap(keys: mode == "keys")
            let r = tap.start()
            input = tap
            info = ["input": r.ok, "keys": r.keys, "devices": r.ok ? (r.keys ? ["mouse", "keyboard"] : ["mouse"]) : []]
        }
        if !CGPreflightScreenCaptureAccess() {
            CGRequestScreenCaptureAccess()
            info["screen_permission"] = false
        }
        Out.emit("hello", info)
        // Commands: one per line. EOF = stop (the recorder died or closed the pipe).
        Thread.detachNewThread { [self] in
            while let line = readLine() {
                let cmd = line.trimmingCharacters(in: .whitespaces)
                DispatchQueue.main.async { self.command(cmd) }
            }
            DispatchQueue.main.async { self.stop() }
        }
        for sig in [SIGINT, SIGTERM] {
            signal(sig, SIG_IGN)
            let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            src.setEventHandler { [self] in stop() }
            src.resume()
            signalSources.append(src)
        }
    }

    var signalSources: [DispatchSourceSignal] = []

    func command(_ cmd: String) {
        switch cmd {
        case "camera": startCamera()
        case "screen": startScreen()
        case "stop": stop()
        case "": break
        default: Out.log("unknown command \(cmd)")
        }
    }

    func startCamera() {
        let want = arg("--camera", args)
        guard let dev = want.flatMap({ findDevice($0, .video) }) ?? AVCaptureDevice.default(for: .video) else {
            Out.emit("camera_failed", ["error": "no camera found"])
            return
        }
        let size = (arg("--camera-size", args) ?? "1920x1080").lowercased().split(separator: "x").compactMap { Int($0) }
        let cam = CameraRecorder(url: URL(fileURLWithPath: arg("--camera-out", args)!), device: dev,
                                 size: size.count == 2 ? (size[0], size[1]) : (1920, 1080))
        cam.onFirstFrame = { mono, w, h in
            Out.emit("camera_start", mono: mono, ["device": dev.localizedName, "pixels": [w, h]])
        }
        do {
            try cam.start()
            camera = cam
        } catch {
            Out.emit("camera_failed", ["error": error.localizedDescription])
        }
    }

    func startScreen() {
        let id = CGDirectDisplayID(arg("--display", args).flatMap { UInt32($0) } ?? CGMainDisplayID())
        let fps = arg("--fps", args).flatMap { Int($0) } ?? 60
        var mic: AVCaptureDevice?
        if let m = arg("--mic", args), m != "none" {
            mic = m == "default" ? AVCaptureDevice.default(for: .audio) : findDevice(m, .audio)
            if mic == nil { Out.notice("mic \(m) not found; recording without sound") }
        }
        let rec = ScreenRecorder(url: URL(fileURLWithPath: arg("--screen-out", args)!), displayID: id, fps: fps, mic: mic,
                                 excludePID: arg("--exclude-pid", args).flatMap { pid_t($0) })
        rec.onFirstFrame = { [self] mono in
            Out.emit("screen_start", mono: mono, ["pixels": [rec.pixels.0, rec.pixels.1],
                                                  "mic": mic?.localizedName as Any])
            DispatchQueue.main.async { self.startPollers() }
        }
        screen = rec
        Task {
            do {
                try await rec.start()
            } catch {
                Out.emit("screen_failed", ["error": error.localizedDescription])
            }
        }
    }

    /// Cursor at 120 Hz and windows/monitors at 10 Hz, each written only when changed.
    func startPollers() {
        var lastCursor = CGPoint(x: -1e9, y: -1e9)
        timers.append(Timer.scheduledTimer(withTimeInterval: 1.0 / 120, repeats: true) { _ in
            let p = cursorPoint()
            if p != lastCursor {
                lastCursor = p
                Out.emit("cursor", ["x": (p.x * 10).rounded() / 10, "y": (p.y * 10).rounded() / 10])
            }
        })
        let exclude = arg("--exclude-pid", args).flatMap { pid_t($0) }
        var lastState = Data()
        timers.append(Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { _ in
            let mono = nowNs()
            let wl = windowList(exclude: exclude)
            let rec: [String: Any] = ["windows": wl.windows, "focus": wl.focus as Any, "monitors": monitors()]
            let data = (try? JSONSerialization.data(withJSONObject: rec, options: [.sortedKeys])) ?? Data()
            if data != lastState {
                lastState = data
                Out.emit("windows", mono: mono, rec)
            }
        })
        for t in timers { RunLoop.main.add(t, forMode: .common) }
    }

    func stop() {
        if stopping { return }
        stopping = true
        let at = nowNs()
        timers.forEach { $0.invalidate() }
        Task {
            async let s: Void = screen?.stop(at: at) ?? ()
            async let c: Void = camera?.stop(at: at) ?? ()
            _ = await (s, c)
            Out.emit("stopped", ["at": at])
            Out.flush()
            exit(0)
        }
    }
}

let argv = Array(CommandLine.arguments.dropFirst())
switch argv.first {
case "devices":
    printJSON(devices())
case "state":
    _ = NSApplication.shared
    let wl = windowList(exclude: arg("--exclude-pid", argv).flatMap { pid_t($0) })
    printJSON(["monitors": monitors(), "windows": wl.windows, "focus": wl.focus as Any,
               "cursor": [cursorPoint().x, cursorPoint().y],
               "permissions": ["screen": CGPreflightScreenCaptureAccess(), "keys": CGPreflightListenEventAccess()]])
case "record":
    let app = NSApplication.shared
    app.setActivationPolicy(.prohibited)
    let rec = Recording(args: argv)
    rec.begin()
    app.run()
case "overlay":
    runOverlay(argv)
default:
    FileHandle.standardError.write(Data("usage: pugrec-capture devices|state|record|overlay ...\n".utf8))
    exit(2)
}
