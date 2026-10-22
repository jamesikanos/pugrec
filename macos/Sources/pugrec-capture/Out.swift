// JSON lines on stdout (the recorder's pipe) and the clock every line is stamped with.
import CoreMedia
import Foundation

/// Host time in ns: mach_absolute_time, the clock Python's time.monotonic_ns() reads on macOS
/// and the one ScreenCaptureKit and AVFoundation stamp frames with.
func nowNs() -> Int64 {
    Int64(clock_gettime_nsec_np(CLOCK_UPTIME_RAW))
}

private let timebase: mach_timebase_info_data_t = {
    var tb = mach_timebase_info_data_t()
    mach_timebase_info(&tb)
    return tb
}()

/// A CMTime on the host clock -> ns on nowNs()'s clock.
func hostNs(_ t: CMTime) -> Int64 {
    let ticks = CMClockConvertHostTimeToSystemUnits(t)
    return Int64(ticks) * Int64(timebase.numer) / Int64(timebase.denom)
}

/// ns on nowNs()'s clock -> a CMTime on the host clock (the inverse of hostNs).
func hostTime(_ ns: Int64) -> CMTime {
    CMClockMakeHostTimeFromSystemUnits(UInt64(ns) * UInt64(timebase.denom) / UInt64(timebase.numer))
}

enum Out {
    private static let queue = DispatchQueue(label: "pugrec.out")

    /// One record, one line. Serialised, so lines from different threads never interleave.
    static func emit(_ kind: String, mono: Int64? = nil, _ fields: [String: Any] = [:]) {
        var rec = fields
        rec["kind"] = kind
        rec["mono"] = mono ?? nowNs()
        queue.async {
            guard let data = try? JSONSerialization.data(withJSONObject: rec, options: [.sortedKeys]) else { return }
            FileHandle.standardOutput.write(data)
            FileHandle.standardOutput.write(Data([0x0A]))
        }
    }

    static func notice(_ msg: String) {
        emit("notice", ["message": msg])
        log(msg)
    }

    /// Diagnostics: stderr, which the recorder keeps in capture.log.
    static func log(_ msg: String) {
        FileHandle.standardError.write(Data("pugrec-capture: \(msg)\n".utf8))
    }

    /// Wait until every queued line is written (before exiting).
    static func flush() {
        queue.sync {}
    }
}
