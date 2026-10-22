// swift-tools-version:6.0
// pugrec-capture: the macOS side of `pugrec record` (screen, mic, camera, input, windows)
// and the countdown overlay. Built by `pugrec setup` into macos/.build/release/.
import PackageDescription

let package = Package(
    name: "pugrec-capture",
    platforms: [.macOS(.v15)],

    targets: [
        .executableTarget(
            name: "pugrec-capture",
            path: "Sources/pugrec-capture",
            linkerSettings: [
                .linkedFramework("ScreenCaptureKit"),
                .linkedFramework("AVFoundation"),
                .linkedFramework("CoreMedia"),
                .linkedFramework("AppKit"),
            ]
        )
    ],
    swiftLanguageModes: [.v5]
)
