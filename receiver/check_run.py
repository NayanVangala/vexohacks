"""Summarise what the receiver saved: counts, cadence, gaps, frame sizes, audio length.

Filenames are server-side time_ns(), so arrival times come straight from them.
Usage: .venv/bin/python check_run.py [--since-seconds N]
"""

import argparse
import os
import statistics
import struct
import time
from pathlib import Path

DATA = Path(os.environ.get("VEXO_DATA_DIR", Path(__file__).parent / "data"))


def jpeg_size(b: bytes):
    i = 2
    while i < len(b) - 9:
        if b[i] != 0xFF:
            i += 1
            continue
        marker = b[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            h, w = struct.unpack(">HH", b[i + 5:i + 9])
            return w, h
        i += 2 + struct.unpack(">H", b[i + 2:i + 4])[0]
    return None


def wav_seconds(b: bytes):
    # 44-byte canonical header from AudioChunker: byte rate at 28, data size at 40.
    return struct.unpack("<I", b[40:44])[0] / struct.unpack("<I", b[28:32])[0]


def times(kind, since):
    d = DATA / kind
    if not d.exists():
        return []
    files = sorted((int(p.stem) / 1e9, p) for p in d.iterdir() if p.stem.isdigit())
    return [(t, p) for t, p in files if t >= since]


def summarise(name, files):
    ts = [t for t, _ in files]
    if len(ts) < 2:
        print(f"{name}: {len(ts)} file(s)")
        return
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    print(f"{name}: {len(ts)} files over {ts[-1] - ts[0]:.1f}s | interval median {statistics.median(gaps):.2f}s "
          f"min {min(gaps):.2f}s max {max(gaps):.2f}s | gaps >5s: {sum(g > 5 for g in gaps)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since-seconds", type=float, default=None, help="only files newer than N seconds ago")
    a = ap.parse_args()
    since = 0 if a.since_seconds is None else time.time() - a.since_seconds

    frames = times("frames", since)
    audio = times("audio", since)
    summarise("frames", frames)
    summarise("audio ", audio)

    sizes = {}
    for _, p in frames:
        s = jpeg_size(p.read_bytes())
        sizes[s] = sizes.get(s, 0) + 1
    if sizes:
        print("frame sizes:", ", ".join(f"{s[0]}x{s[1]} x{n}" for s, n in sizes.items() if s))

    secs = [wav_seconds(p.read_bytes()) for _, p in audio]
    if secs:
        print(f"audio: {sum(secs):.1f}s total, per-file {min(secs):.2f}-{max(secs):.2f}s")
    print(f"activation photos: {len(times('activation', since))}")


if __name__ == "__main__":
    main()
