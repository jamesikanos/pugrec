// Webcam -> camera.mov: AVCaptureSession -> HEVC, PTS from 0 = its first frame.
// The first frame's host time is reported (camera_start), so the recorder can put
// camera PTS 0 on the session clock, exactly as the Linux recorder does with ffmpeg's
// "start:" line - only here it comes straight from the frame.
import AVFoundation
import CoreMedia

final class CameraRecorder: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    let url: URL
    let device: AVCaptureDevice
    let size: (Int, Int)
    let queue = DispatchQueue(label: "pugrec.camera")
    private let session = AVCaptureSession()
    private var writer: AVAssetWriter?
    private var input: AVAssetWriterInput?
    private var started = false
    private var stopping = false
    var onFirstFrame: ((Int64, Int, Int) -> Void)?

    init(url: URL, device: AVCaptureDevice, size: (Int, Int)) {
        self.url = url
        self.device = device
        self.size = size
    }

    func start() throws {
        session.beginConfiguration()
        let preset: AVCaptureSession.Preset = size.0 >= 3840 ? .hd4K3840x2160 : size.0 >= 1920 ? .hd1920x1080 : .hd1280x720
        session.sessionPreset = session.canSetSessionPreset(preset) ? preset : .high
        let input = try AVCaptureDeviceInput(device: device)
        guard session.canAddInput(input) else {
            throw NSError(domain: "pugrec", code: 2, userInfo: [NSLocalizedDescriptionKey: "can't use camera \(device.localizedName)"])
        }
        session.addInput(input)
        let out = AVCaptureVideoDataOutput()
        out.videoSettings = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange]
        out.alwaysDiscardsLateVideoFrames = false
        out.setSampleBufferDelegate(self, queue: queue)
        session.addOutput(out)
        session.commitConfiguration()
        session.startRunning()
    }

    func captureOutput(_ output: AVCaptureOutput, didOutput sb: CMSampleBuffer, from connection: AVCaptureConnection) {
        guard !stopping, let pb = CMSampleBufferGetImageBuffer(sb) else { return }
        // The session's clock is the host clock on macOS, but convert to be sure.
        let pts = CMSyncConvertTime(sb.presentationTimeStamp, from: session.synchronizationClock ?? CMClockGetHostTimeClock(),
                                    to: CMClockGetHostTimeClock())
        if !started {
            let w = CVPixelBufferGetWidth(pb), h = CVPixelBufferGetHeight(pb)
            do {
                let wr = try AVAssetWriter(outputURL: url, fileType: .mov)
                let vin = AVAssetWriterInput(mediaType: .video, outputSettings: [
                    AVVideoCodecKey: AVVideoCodecType.hevc,
                    AVVideoWidthKey: w,
                    AVVideoHeightKey: h,
                    AVVideoCompressionPropertiesKey: [
                        AVVideoQualityKey: 0.75,
                        AVVideoMaxKeyFrameIntervalDurationKey: 2.0,
                        AVVideoAllowFrameReorderingKey: false,
                    ],
                ])
                vin.expectsMediaDataInRealTime = true
                wr.add(vin)
                guard wr.startWriting() else { throw wr.error ?? NSError(domain: "pugrec", code: 3) }
                wr.startSession(atSourceTime: pts)
                writer = wr
                input = vin
            } catch {
                Out.notice("camera writer failed: \(error.localizedDescription)")
                stopping = true
                return
            }
            started = true
            onFirstFrame?(hostNs(pts), w, h)
        }
        if input?.isReadyForMoreMediaData == true,
           let copy = try? CMSampleBuffer(copying: sb, withNewTiming: [CMSampleTimingInfo(
               duration: sb.duration, presentationTimeStamp: pts, decodeTimeStamp: .invalid)]) {
            input?.append(copy)
        }
    }

    /// Stop at host time `at` (ns): no frames after it, file finalised.
    func stop(at: Int64) async {
        session.stopRunning()
        await withCheckedContinuation { (k: CheckedContinuation<Void, Never>) in
            queue.async { [self] in
                stopping = true
                guard let writer, started else { k.resume(); return }
                input?.markAsFinished()
                writer.endSession(atSourceTime: hostTime(at))
                writer.finishWriting {
                    if writer.status != .completed {
                        Out.notice("camera.mov didn't finalise: \(writer.error?.localizedDescription ?? "?")")
                    }
                    k.resume()
                }
            }
        }
    }
}
