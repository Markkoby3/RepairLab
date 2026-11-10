// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "DiagKit",
    platforms: [.macOS(.v13), .iOS(.v16)],
    products: [
        .library(name: "DiagKit", targets: ["DiagKit"]),
    ],
    targets: [
        .target(name: "DiagKit"),
        .testTarget(name: "DiagKitTests", dependencies: ["DiagKit"]),
    ]
)
