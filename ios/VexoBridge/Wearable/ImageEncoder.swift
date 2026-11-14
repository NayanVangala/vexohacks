//
//  ImageEncoder.swift
//  VexoBridge
//
//  VideoFrame -> JPEG. CLAUDE-ios.md §7.5.
//
//  The Android version's entire reason for existing (I420 -> NV21 repack,
//  V-before-U interleave) does not apply here: the SDK hands you a decoded
//  image. There is no chroma-swap bug to reproduce.
//
//  Two paths, in order of preference:
//    1. CIContext + jpegRepresentation on the frame's CMSampleBuffer.
//    2. frame.makeUIImage() -> UIImage.jpegData().
//
//  Exactly one CIContext is created and reused. Constructing one per frame is
//  the single real performance trap in this path (§10).
//
//  Never called on the main thread. The frame callback arrives on an SDK
//  background thread — encoding stays there, and only the BridgeState update
//  hops to @MainActor (§7.5, §11.7).
//

import CoreImage
import CoreMedia
import CoreVideo
import ImageIO
import UIKit

import MWDATCamera

final class ImageEncoder {

    /// Appendix A said never change, on the reasoning that Phase 2 re-encodes
    /// anyway so bigger frames only cost LAN bandwidth. The first half is true
    /// and the second half is why this moved up rather than down.
    ///
    /// Measured 2026-08-02: Haiku bills images on pixel dimensions, not on file
    /// size — the same 360x640 frame cost 556 input tokens at 20 KB and at
    /// 24 KB. So device-side quality is free at the API and costs only WiFi
    /// between the phone and the server, which is not a constrained link. What
    /// it buys is one less lossy generation: this JPEG is decoded and re-encoded
    /// by `describe.prepare_image`, and q80-then-q85 compounds artifacts that
    /// q90-then-q85 does not.
    static let jpegQuality: CGFloat = 0.9

    /// Instance property of a non-Sendable object owned by the controller, per
    /// §11.7 — a global `let` trips Swift 6 strict concurrency, and CIContext is
    /// thread-safe by contract.
    private let ciContext: CIContext
    private let colorSpace = CGColorSpaceCreateDeviceRGB()

    /// Set if frames come back rotated. Verify at hour 1 with a page of text:
    /// a sideways image degrades Haiku's descriptions in a way that reads as
    /// "the model is bad" rather than "the image is rotated" (§7.5, §14.4).
    /// TODO(glasses): confirm upright output on real hardware and set this if not.
    var frameOrientation: CGImagePropertyOrientation = .up

    init() {
        // Software rendering off, cache off: we encode one frame every 2.5 s and
        // never reuse intermediates, so the cache is pure memory growth.
        ciContext = CIContext(options: [.cacheIntermediates: false])
    }

    // MARK: - Public

    /// Returns nil rather than throwing on any frame that will not decode; the
    /// caller drops it (§7.5).
    func jpeg(from frame: VideoFrame) -> Data? {
        if let data = jpegViaCoreImage(frame) { return data }
        if let data = jpegViaUIImage(frame) { return data }
        BridgeState.log("encode: frame produced no JPEG, dropped")
        return nil
    }

    // MARK: - Path 1 — CIContext (recommended, §7.5)

    private func jpegViaCoreImage(_ frame: VideoFrame) -> Data? {
        guard let pixelBuffer = Self.imageBuffer(from: frame) else { return nil }

        var image = CIImage(cvImageBuffer: pixelBuffer)
        if frameOrientation != .up {
            image = image.oriented(frameOrientation)
        }

        // §14.5: the exact CVPixelFormatType is undocumented. CIImage handles
        // any of them — which is why this path is preferred. If this returns
        // nil, fall straight back to makeUIImage() rather than debugging pixel
        // formats.
        return ciContext.jpegRepresentation(
            of: image,
            colorSpace: colorSpace,
            options: [
                CIImageRepresentationOption(
                    rawValue: kCGImageDestinationLossyCompressionQuality as String
                ): Self.jpegQuality
            ]
        )
    }

    /// Verified against MWDATCamera 0.8.0: `VideoFrame.sampleBuffer` is a
    /// non-optional `CMSampleBuffer`. The image buffer inside it can still be
    /// absent, so that part stays optional.
    ///
    /// §14.5: the exact CVPixelFormatType is undocumented, and deliberately not
    /// inspected here — CIImage handles any of them.
    private static func imageBuffer(from frame: VideoFrame) -> CVImageBuffer? {
        CMSampleBufferGetImageBuffer(frame.sampleBuffer)
    }

    /// Delivered pixel dimensions, read straight off the buffer.
    ///
    /// Requested resolution is not delivered resolution — the SDK degrades under
    /// Bluetooth pressure — and this is the cheapest way to see which one we
    /// actually got. Nil for any frame whose buffer will not resolve, the same
    /// frames `jpeg(from:)` drops.
    static func dimensions(of frame: VideoFrame) -> (Int, Int)? {
        guard let buffer = imageBuffer(from: frame) else { return nil }
        return (CVPixelBufferGetWidth(buffer), CVPixelBufferGetHeight(buffer))
    }

    // MARK: - Path 2 — makeUIImage fallback

    private func jpegViaUIImage(_ frame: VideoFrame) -> Data? {
        guard var image = frame.makeUIImage() else { return nil }
        if frameOrientation != .up, let cg = image.cgImage {
            image = UIImage(
                cgImage: cg,
                scale: image.scale,
                orientation: Self.uiOrientation(frameOrientation)
            )
        }
        return image.jpegData(compressionQuality: Self.jpegQuality)
    }

    private static func uiOrientation(_ o: CGImagePropertyOrientation) -> UIImage.Orientation {
        switch o {
        case .up:             return .up
        case .upMirrored:     return .upMirrored
        case .down:           return .down
        case .downMirrored:   return .downMirrored
        case .left:           return .left
        case .leftMirrored:   return .leftMirrored
        case .right:          return .right
        case .rightMirrored:  return .rightMirrored
        @unknown default:     return .up
        }
    }
}

// Activation photos need no encoding at all: PhotoData.data from a `.jpeg`
// capture is already JPEG bytes. Post them as-is (§7.5).
