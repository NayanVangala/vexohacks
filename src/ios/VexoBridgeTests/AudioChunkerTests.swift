//
//  AudioChunkerTests.swift
//  VexoBridgeTests
//
//  WAV header math, off-device. CLAUDE-ios.md §7.7, §13.1 step 3.
//
//  AudioChunker is pure logic with no platform dependency and is the one
//  component that unit-tests off-device on both platforms. These assertions are
//  byte-exact and are the same numbers the Android test checks.
//

import XCTest
@testable import VexoBridge

final class AudioChunkerTests: XCTestCase {

    // MARK: - Window sizing

    func testDefaultWindowIs80000Bytes() {
        // 16000 Hz * 1 ch * 2 bytes * 2500 ms / 1000 = 80_000
        let chunker = AudioChunker { _ in }
        XCTAssertEqual(chunker.bytesPerWindow, 80_000)
    }

    func testFiveSecondQueryWindowIs160000Bytes() {
        let chunker = AudioChunker(windowMillis: 5_000) { _ in }
        XCTAssertEqual(chunker.bytesPerWindow, 160_000)
    }

    // MARK: - feed / flush / discard

    func testFeedEmitsNothingBelowOneWindow() {
        var emitted = 0
        let chunker = AudioChunker { _ in emitted += 1 }
        chunker.feed(Data(count: 79_999))
        XCTAssertEqual(emitted, 0)
        XCTAssertEqual(chunker.bufferedByteCount, 79_999)
    }

    func testFeedEmitsExactlyOneWavPerFullWindow() {
        var sizes: [Int] = []
        let chunker = AudioChunker { sizes.append($0.count) }
        chunker.feed(Data(count: 80_000))
        XCTAssertEqual(sizes, [80_044])          // 44-byte header + payload
        XCTAssertEqual(chunker.bufferedByteCount, 0)
    }

    func testOneLargeFeedEmitsSeveralWindowsAndKeepsTheRemainder() {
        var sizes: [Int] = []
        let chunker = AudioChunker { sizes.append($0.count) }
        chunker.feed(Data(count: 80_000 * 3 + 1_234))
        XCTAssertEqual(sizes, [80_044, 80_044, 80_044])
        XCTAssertEqual(chunker.bufferedByteCount, 1_234)
    }

    func testFlushEmitsThePartialTail() {
        var sizes: [Int] = []
        let chunker = AudioChunker { sizes.append($0.count) }
        chunker.feed(Data(count: 1_000))
        chunker.flush()
        XCTAssertEqual(sizes, [1_044])
        XCTAssertEqual(chunker.bufferedByteCount, 0)
    }

    func testFlushOnEmptyBufferEmitsNothing() {
        var emitted = 0
        let chunker = AudioChunker { _ in emitted += 1 }
        chunker.flush()
        XCTAssertEqual(emitted, 0)
    }

    /// discard() exists so a query recording starts clean and cannot be prefixed
    /// by a fragment of an abandoned one (§7.7).
    func testDiscardDropsWithoutEmitting() {
        var emitted = 0
        let chunker = AudioChunker { _ in emitted += 1 }
        chunker.feed(Data(count: 40_000))
        chunker.discard()
        XCTAssertEqual(emitted, 0)
        XCTAssertEqual(chunker.bufferedByteCount, 0)
    }

    // MARK: - WAV header, byte by byte
    //
    // The byte-offset table in CLAUDE.md §7.7 is authoritative. Little-endian
    // everywhere. Built with explicit writes, not withUnsafeBytes of a struct —
    // struct padding corrupts the header in a way that plays fine in QuickTime
    // and fails in faster-whisper.

    func testWavHeaderIsByteExact() {
        let payload = Data(count: 320)   // 10 ms at 16 kHz mono 16-bit
        let wav = AudioChunker.wav(pcm: payload, sampleRate: 16_000, channels: 1, bitsPerSample: 16)

        XCTAssertEqual(wav.count, 44 + 320)

        XCTAssertEqual(Self.ascii(wav, 0, 4), "RIFF")
        XCTAssertEqual(Self.le32(wav, 4), UInt32(36 + 320))       // ChunkSize
        XCTAssertEqual(Self.ascii(wav, 8, 4), "WAVE")

        XCTAssertEqual(Self.ascii(wav, 12, 4), "fmt ")
        XCTAssertEqual(Self.le32(wav, 16), 16)                    // Subchunk1Size
        XCTAssertEqual(Self.le16(wav, 20), 1)                     // PCM
        XCTAssertEqual(Self.le16(wav, 22), 1)                     // channels
        XCTAssertEqual(Self.le32(wav, 24), 16_000)                // sample rate
        XCTAssertEqual(Self.le32(wav, 28), 32_000)                // byte rate  = 16000*1*2
        XCTAssertEqual(Self.le16(wav, 32), 2)                     // block align = 1*2
        XCTAssertEqual(Self.le16(wav, 34), 16)                    // bits per sample

        XCTAssertEqual(Self.ascii(wav, 36, 4), "data")
        XCTAssertEqual(Self.le32(wav, 40), 320)                   // Subchunk2Size
    }

    func testWavHeaderMathForStereoAnd8Bit() {
        // Not a configuration the app ships, but it pins the byte-rate and
        // block-align formulas rather than the constants.
        let wav = AudioChunker.wav(pcm: Data(count: 8), sampleRate: 8_000, channels: 2, bitsPerSample: 8)
        XCTAssertEqual(Self.le32(wav, 28), 8_000 * 2 * 1)         // byte rate
        XCTAssertEqual(Self.le16(wav, 32), 2)                     // block align
    }

    func testPayloadFollowsHeaderUnmodified() {
        let payload = Data([0x01, 0x02, 0xFD, 0xFE])
        let wav = AudioChunker.wav(pcm: payload, sampleRate: 16_000, channels: 1, bitsPerSample: 16)
        XCTAssertEqual(wav.suffix(4), payload)
    }

    // MARK: - Thread safety
    //
    // Fed from the audio tap thread, flushed from the main actor. This does not
    // prove correctness, but it does catch an unlocked buffer under TSan.

    func testConcurrentFeedsDoNotLoseBytes() {
        let expectation = expectation(description: "feeds complete")
        let lock = NSLock()
        var totalPayloadBytes = 0

        let chunker = AudioChunker { wav in
            lock.lock()
            totalPayloadBytes += wav.count - 44
            lock.unlock()
        }

        let iterations = 100
        DispatchQueue.concurrentPerform(iterations: iterations) { _ in
            chunker.feed(Data(count: 8_000))
        }
        chunker.flush()
        expectation.fulfill()

        wait(for: [expectation], timeout: 5)
        XCTAssertEqual(totalPayloadBytes, iterations * 8_000)
    }

    // MARK: - Helpers

    private static func ascii(_ data: Data, _ offset: Int, _ count: Int) -> String {
        String(decoding: data[offset..<(offset + count)], as: UTF8.self)
    }

    private static func le16(_ data: Data, _ offset: Int) -> UInt16 {
        UInt16(data[offset]) | (UInt16(data[offset + 1]) << 8)
    }

    private static func le32(_ data: Data, _ offset: Int) -> UInt32 {
        UInt32(data[offset])
            | (UInt32(data[offset + 1]) << 8)
            | (UInt32(data[offset + 2]) << 16)
            | (UInt32(data[offset + 3]) << 24)
    }
}
