// swift-tools-version: 5.9
import PackageDescription

// A local package whose tests exist only so a scheme can name them. Xcode has been
// reported to ignore such a reference silently while still reporting success; the
// acceptance check's test-count comparison is what catches that.
let package = Package(
    name: "LeaseProbeKit",
    platforms: [.iOS(.v17)],
    products: [
        .library(name: "LeaseProbeKit", targets: ["LeaseProbeKit"]),
    ],
    targets: [
        .target(name: "LeaseProbeKit"),
        .testTarget(name: "LeaseProbeKitTests", dependencies: ["LeaseProbeKit"]),
    ]
)
