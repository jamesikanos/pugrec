// Screen (+ mic) -> screen.mov: ScreenCaptureKit -> HEVC on the media engine, mic as ALAC.
//
// Apple Silicon decodes AV1 but has no AV1 encoder, so the source is HEVC (the Linux
// recorder's AV1 was for the same reason: what the GPU encodes at native resolution).
// The cursor is drawn into the video, as on Linux. VFR: ScreenCaptureKit only delivers a
// frame when something changed; at the stop the last frame is appended again at the stop
// time, so the video runs right up to it (like wf-recorder -D) and a gap can only be a stall.
import AVFoundation
import CoreMedia
import ScreenCaptureKit

final class ScreenRecorder: NSObject, SCStreamOutput, SCStreamDelegate {
    let url: URL
    let displayID: CGDirectDisplayID
    let fps: Int
    let mic: AVCaptureDevice?
    let excludePID: pid_t?
    let queue = DispatchQueue(label: "pugrec.screen")
    private var stream: SCStream?
    private var writer: AVAssetWriter?
    private var video: AVAssetWriterInput?
    private var adaptor: AVAssetWriterInputPixelBufferAdaptor?
    private var audio: AVAssetWriterInput?
    private var lastFrame: CVPixelBuffer?
    private var firstPTS = CMTime.invalid
    private var lastPTS = CMTime.invalid
    private var started = false
    private var stopping = false
    private(set) var pixels = (0, 0)
    var onFirstFrame: ((Int64) -> Void)?
    // mute watch: exact digital zeros for 0.5 s = muted (a live mic is never all-zero)
    private var zeroRun = 0.0, liveRun = 0.0
    private var muted: Bool?

    init(url: URL, displayID: CGDirectDisplayID, fps: Int, mic: AVCaptureDevice?, excludePID: pid_t? = nil) {
        self.url = url
        self.displayID = displayID
        self.fps = fps
        self.mic = mic
        self.excludePID = excludePID
    }

    func start() async throws {
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
        guard let display = content.displays.first(where: { $0.displayID == displayID }) else {
            throw NSError(domain: "pugrec", code: 1, userInfo: [NSLocalizedDescriptionKey: "display \(displayID) not shareable"])
        }
        let mode = CGDisplayCopyDisplayMode(displayID)
        let w = mode?.pixelWidth ?? display.width * 2
        let h = mode?.pixelHeight ?? display.height * 2
        pixels = (w, h)

        let cfg = SCStreamConfiguration()
        cfg.width = w
        cfg.height = h
        cfg.minimumFrameInterval = CMTime(value: 1, timescale: CMTimeScale(fps))
        cfg.pixelFormat = kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange
        cfg.colorMatrix = CGDisplayStream.yCbCrMatrix_ITU_R_709_2
        cfg.colorSpaceName = CGColorSpace.sRGB
        cfg.showsCursor = true
        cfg.queueDepth = 8
        cfg.capturesAudio = false  // system audio: not wanted, only the mic
        if let mic {
            cfg.captureMicrophone = true
            cfg.microphoneCaptureDeviceID = mic.uniqueID
        }

        let w0 = try AVAssetWriter(outputURL: url, fileType: .mov)
        let vin = AVAssetWriterInput(mediaType: .video, outputSettings: [
            AVVideoCodecKey: AVVideoCodecType.hevc,
            AVVideoWidthKey: w,
            AVVideoHeightKey: h,
            AVVideoColorPropertiesKey: [
                AVVideoColorPrimariesKey: AVVideoColorPrimaries_ITU_R_709_2,
                AVVideoTransferFunctionKey: AVVideoTransferFunction_ITU_R_709_2,
                AVVideoYCbCrMatrixKey: AVVideoYCbCrMatrix_ITU_R_709_2,
            ],
            AVVideoCompressionPropertiesKey: [
                AVVideoQualityKey: 0.8,
                AVVideoExpectedSourceFrameRateKey: fps,
                AVVideoMaxKeyFrameIntervalDurationKey: 2.0,
                AVVideoAllowFrameReorderingKey: false,  // no B-frames: cheap seeks in the editor
            ],
        ])
        vin.expectsMediaDataInRealTime = true
        w0.add(vin)
        adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: vin, sourcePixelBufferAttributes: nil)
        if mic != nil {
            // Lossless, so `pugrec audio` starts from the real signal (FLAC on Linux).
            let ain = AVAssetWriterInput(mediaType: .audio, outputSettings: [
                AVFormatIDKey: kAudioFormatAppleLossless,
                AVSampleRateKey: 48000,
                AVNumberOfChannelsKey: 1,
                AVEncoderBitDepthHintKey: 24,
            ])
            ain.expectsMediaDataInRealTime = true
            w0.add(ain)
            audio = ain
        }
        writer = w0
        video = vin

        // The control app (--exclude-pid) is left out of the picture: it's on screen to be clicked.
        let hidden = content.applications.filter { $0.processID == excludePID }
        let filter = SCContentFilter(display: display, excludingApplications: hidden, exceptingWindows: [])
        let s = SCStream(filter: filter, configuration: cfg, delegate: self)
        try s.addStreamOutput(self, type: .screen, sampleHandlerQueue: queue)
        if mic != nil {
            try s.addStreamOutput(self, type: .microphone, sampleHandlerQueue: queue)
        }
        try await s.startCapture()
        stream = s
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
        guard !stopping, let writer else { return }
        switch type {
        case .screen:
            guard let atts = CMSampleBufferGetSampleAttachmentsArray(sb, createIfNecessary: false) as? [[SCStreamFrameInfo: Any]],
                  let raw = atts.first?[.status] as? Int, SCFrameStatus(rawValue: raw) == .complete,
                  let pb = CMSampleBufferGetImageBuffer(sb) else { return }
            let pts = sb.presentationTimeStamp
            if !started {
                guard writer.startWriting() else {
                    Out.notice("screen writer failed to start: \(writer.error?.localizedDescription ?? "?")")
                    return
                }
                writer.startSession(atSourceTime: pts)
                firstPTS = pts
                started = true
                onFirstFrame?(hostNs(pts))
            }
            if video?.isReadyForMoreMediaData == true {
                adaptor?.append(pb, withPresentationTime: pts)
                lastFrame = pb
                lastPTS = pts
            }
        case .microphone:
            watchMute(sb)
            // Before the first video frame there's no file yet: that audio is the warm-up's anyway.
            if started, let audio, audio.isReadyForMoreMediaData,
               CMTimeCompare(sb.presentationTimeStamp, firstPTS) >= 0 {
                audio.append(sb)
            }
        default:
            break
        }
    }

    private func watchMute(_ sb: CMSampleBuffer) {
        guard let mic else { return }
        var allZero = true
        try? sb.withAudioBufferList { abl, _ in
            for buf in abl {
                guard let p = buf.mData else { continue }
                let bytes = UnsafeRawBufferPointer(start: p, count: Int(buf.mDataByteSize))
                if bytes.contains(where: { $0 != 0 }) { allZero = false; break }
            }
        }
        let dur = sb.duration.seconds.isFinite ? sb.duration.seconds : 0.01
        if allZero { zeroRun += dur; liveRun = 0 } else { liveRun += dur; zeroRun = 0 }
        let now: Bool? = zeroRun >= 0.5 ? true : (liveRun >= 0.1 ? false : nil)
        if let now, now != muted {
            muted = now
            Out.emit("mic_mute", ["source": mic.localizedName, "muted": now])
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        Out.notice("screen capture stopped: \(error.localizedDescription)")
        Out.emit("screen_stopped", ["error": error.localizedDescription])
    }

    /// Stop capturing at host time `at` (ns) and finalise the file.
    func stop(at: Int64) async {
        try? await stream?.stopCapture()
        await withCheckedContinuation { (k: CheckedContinuation<Void, Never>) in
            queue.async { [self] in
                stopping = true
                guard let writer, started else {
                    writer?.cancelWriting()
                    k.resume()
                    return
                }
                let end = hostTime(at)
                if let lastFrame, lastPTS.isValid, CMTimeCompare(end, lastPTS) > 0 {
                    while video?.isReadyForMoreMediaData == false { usleep(1000) }
                    adaptor?.append(lastFrame, withPresentationTime: end)  // video runs to the stop
                }
                writer.endSession(atSourceTime: lastPTS.isValid ? CMTimeMaximum(end, lastPTS) : end)
                video?.markAsFinished()
                audio?.markAsFinished()
                writer.finishWriting {
                    if writer.status != .completed {
                        Out.notice("screen.mov didn't finalise: \(writer.error?.localizedDescription ?? "?")")
                    }
                    k.resume()
                }
            }
        }
    }
}
