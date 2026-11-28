"""Draw the app icon (a gold Kinah-style coin) and write it as a multi-size .ico."""
import math
import struct
import sys
import zlib


def png(size: int) -> bytes:
    rows = []
    c = (size - 1) / 2
    r_out, r_in = size * 0.47, size * 0.33
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            d = math.hypot(x - c, y - c)
            # soft edge
            a = max(0.0, min(1.0, r_out - d + 0.5))
            # radial gold gradient, lit from top-left
            t = max(0.0, min(1.0, math.hypot(x - c * 0.6, y - c * 0.6) / (size * 0.9)))
            r, g, b = (255 - 110 * t, 228 - 140 * t, 140 - 120 * t)
            if abs(d - r_in) < size * 0.035:      # inner ring
                r, g, b = r * 0.72, g * 0.66, b * 0.5
            if d < r_in * 0.62 and abs(x - c) < size * 0.06 + (y - c) * 0 and abs(y - c) < r_in * 0.55:
                r, g, b = r * 0.7, g * 0.62, b * 0.45   # vertical bar of the "vault" mark
            if d < r_in * 0.62 and abs(y - c) < size * 0.06 and abs(x - c) < r_in * 0.55:
                r, g, b = r * 0.7, g * 0.62, b * 0.45
            row += bytes([int(r), int(g), int(b), int(a * 255)])
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def ico(path: str, sizes=(16, 24, 32, 48, 64, 128, 256)):
    imgs = [png(s) for s in sizes]
    head = struct.pack("<HHH", 0, 1, len(sizes))
    off = 6 + 16 * len(sizes)
    dirs = b""
    for s, data in zip(sizes, imgs):
        dirs += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), off)
        off += len(data)
    with open(path, "wb") as f:
        f.write(head + dirs + b"".join(imgs))


if __name__ == "__main__":
    ico(sys.argv[1] if len(sys.argv) > 1 else "icon.ico")
