"""Punch the leftover fill out of web/static/logo.png.

The remove.bg export still carries a halo: semi-transparent white around the
shield (which reads as gray on the dark admin panel) and a strip of leftover
black along the top. The shield itself is two solid colours plus a thin black
outline. Keep those, drop everything else.

    python scripts/cutout_logo.py
"""

from __future__ import annotations

import array
import struct
import zlib
from pathlib import Path

LOGO = Path(__file__).resolve().parent.parent / "web" / "static" / "logo.png"


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def read_rgba(path: Path) -> tuple[int, int, bytes]:
    """Decode a PNG into raw RGBA bytes. Enough of the format for this file."""
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    pos = 8
    width = height = None
    idat = bytearray()
    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        ctype = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            width, height, bit, color, *_ = struct.unpack(">IIBBBBB", chunk)
            if bit != 8 or color != 6:
                raise SystemExit(f"need 8-bit RGBA PNG, got bit={bit} color={color}")
        elif ctype == b"IDAT":
            idat.extend(chunk)
        elif ctype == b"IEND":
            break
    raw = zlib.decompress(bytes(idat))
    stride = width * 4
    out = bytearray(height * stride)
    prev = bytearray(stride)
    i = 0
    for y in range(height):
        filt = raw[i]
        i += 1
        row = bytearray(raw[i:i + stride])
        i += stride
        if filt == 1:  # Sub
            for x in range(stride):
                left = row[x - 4] if x >= 4 else 0
                row[x] = (row[x] + left) & 255
        elif filt == 2:  # Up
            for x in range(stride):
                row[x] = (row[x] + prev[x]) & 255
        elif filt == 3:  # Average
            for x in range(stride):
                left = row[x - 4] if x >= 4 else 0
                row[x] = (row[x] + ((left + prev[x]) // 2)) & 255
        elif filt == 4:  # Paeth
            for x in range(stride):
                left = row[x - 4] if x >= 4 else 0
                up = prev[x]
                ul = prev[x - 4] if x >= 4 else 0
                row[x] = (row[x] + _paeth(left, up, ul)) & 255
        elif filt != 0:
            raise SystemExit(f"unsupported PNG filter {filt}")
        out[y * stride:(y + 1) * stride] = row
        prev = row
    return width, height, bytes(out)


def write_rgba(path: Path, width: int, height: int, pixels: bytes) -> None:
    stride = width * 4
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter None
        raw.extend(pixels[y * stride:(y + 1) * stride])

    def chunk(tag: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(tag)
        crc = zlib.crc32(payload, crc) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    idat = zlib.compress(bytes(raw), 9)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", idat)
        + chunk(b"IEND", b"")
    )


def is_blue(r: int, g: int, b: int) -> bool:
    # Shield field is solid (0, 130, 190). Allow a little anti-alias mix.
    return b >= 150 and g >= 80 and g <= 180 and r <= 80 and (b - r) >= 80


def is_white(r: int, g: int, b: int, a: int) -> bool:
    return a >= 250 and r >= 220 and g >= 220 and b >= 220


def is_black(r: int, g: int, b: int, a: int) -> bool:
    return a >= 250 and r <= 40 and g <= 40 and b <= 40


def cutout(width: int, height: int, src: bytes) -> bytes:
    n = width * height
    keep = array.array("B", bytes(n))

    def pix(i: int) -> tuple[int, int, int, int]:
        o = i * 4
        return src[o], src[o + 1], src[o + 2], src[o + 3]

    for i in range(n):
        r, g, b, a = pix(i)
        if is_blue(r, g, b) or is_white(r, g, b, a):
            keep[i] = 1

    # Dilate so the thin black outline (and anti-aliased edge) stays attached.
    dilated = array.array("B", keep)
    radius = 3
    for y in range(height):
        for x in range(width):
            i = y * width + x
            if keep[i]:
                continue
            found = False
            y0, y1 = max(0, y - radius), min(height, y + radius + 1)
            x0, x1 = max(0, x - radius), min(width, x + radius + 1)
            for yy in range(y0, y1):
                row = yy * width
                for xx in range(x0, x1):
                    if keep[row + xx]:
                        found = True
                        break
                if found:
                    break
            if found:
                dilated[i] = 1

    out = bytearray(src)
    kept = dropped = 0
    for i in range(n):
        r, g, b, a = pix(i)
        o = i * 4
        shield = (
            is_blue(r, g, b)
            or is_white(r, g, b, a)
            or (dilated[i] and is_black(r, g, b, a))
        )
        # Anti-aliased outline: dark or blue-tinted, sitting on the ring.
        edge = dilated[i] and a >= 200 and (max(r, g, b) <= 80 or is_blue(r, g, b))
        if shield or edge:
            kept += 1
            continue
        out[o] = 0
        out[o + 1] = 0
        out[o + 2] = 0
        out[o + 3] = 0
        dropped += 1
    print(f"kept {kept} pixels, cleared {dropped}")
    return bytes(out)


def main() -> None:
    width, height, src = read_rgba(LOGO)
    print(f"loaded {LOGO} {width}x{height}")
    out = cutout(width, height, src)
    write_rgba(LOGO, width, height, out)
    print(f"wrote {LOGO} ({LOGO.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
