"""Read-only decoder for Urban Assault ILBM / VBMP indexed textures.

Layout (CONFIRMED, mirrors OpenUA UA_source/src/ilbm.cpp READ_ILBM and
ILBM_BODY_READ):

    FORM ILBM
      BMHD  20 bytes: u16 width, u16 height, u16 x, u16 y, s8 nPlanes,
            s8 masking, s8 compression, s8 flags, u16 transparentColor,
            s8 xAspect, s8 yAspect, u16 pageWidth, u16 pageHeight
      CMAP  256 * 3 bytes r,g,b (may be absent: external palette needed)
      BODY  planar bitplane rows; per row and per plane, optionally
            ByteRun1-compressed.  The runtime uses a fixed 128-byte plane
            buffer stride (plane_offset = plane << 7), i.e. textures wider
            than 1024 pixels are not supported by the engine either.

    FORM VBMP
      HEAD  6 bytes: u16 width, u16 height, u16 flags
      CMAP  optional
      BODY  raw 8-bit indexed pixels, width*height bytes

Palette-only files (NORMAL.PAL, STANDARD.PAL) are FORM ILBM with a 0x0 BMHD
and a CMAP but no BODY; ``parse_ilbm_file`` returns them with ``pixels=None``
so they can be used as external palettes.

Pixels stay as palette indices; conversion to RGB happens only for UI preview
so the index meaning is never destroyed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import struct

from iff_reader import IffTree, read_iff_bytes, read_iff_file

Palette = list[tuple[int, int, int]]


class IlbmParseError(Exception):
    pass


@dataclass
class IlbmImage:
    source_name: str = ""
    kind: str = ""             # "ILBM" | "VBMP"
    width: int = 0
    height: int = 0
    n_planes: int = 8
    masking: int = 0
    compression: int = 0
    transparent_color: int = 0
    palette: Palette | None = None
    pixels: bytes | None = None   # width*height palette indices, row-major
    warnings: list[str] = field(default_factory=list)

    @property
    def has_body(self) -> bool:
        return self.pixels is not None

    @property
    def is_palette_only(self) -> bool:
        return self.pixels is None and self.palette is not None

    def to_rgb_bytes(self, palette_override: Palette | None = None) -> bytes | None:
        """Flatten to RGB888 for preview only.  Indices are preserved elsewhere."""

        if self.pixels is None:
            return None
        palette = palette_override or self.palette or _grayscale_palette()
        table = bytearray(768)
        for i, (r, g, b) in enumerate(palette[:256]):
            table[i * 3] = r
            table[i * 3 + 1] = g
            table[i * 3 + 2] = b
        out = bytearray(len(self.pixels) * 3)
        for i, idx in enumerate(self.pixels):
            base = idx * 3
            out[i * 3] = table[base]
            out[i * 3 + 1] = table[base + 1]
            out[i * 3 + 2] = table[base + 2]
        return bytes(out)

    def to_rgba_bytes(self, palette_override: Palette | None = None,
                      alpha_mode: str = "chroma") -> bytes | None:
        """Flatten to RGBA8888 for preview, with engine-style alpha.

        alpha_mode:
          "opaque" — alpha 255 everywhere.
          "chroma" — exact port of GFXEngine::ConvAlphaPalette default path
                     (CONFIRMED): palette color RGB(255,255,0) becomes fully
                     transparent black.  Release vehicle atlases paint palette
                     index 0 pure yellow for exactly this purpose.
          "luma"   — ConvAlphaPalette source-blend path (CONFIRMED formula):
                     chroma removed as above, then per remaining color
                     mx = max(r,g,b); mx <= 8 -> transparent, else RGB
                     normalised by mx and alpha = mx.  Preview aid for
                     flat-tracy faces on renderers without additive blending.
        """

        if self.pixels is None:
            return None
        palette = palette_override or self.palette or _grayscale_palette()
        table = bytearray(1024)
        for i, (r, g, b) in enumerate(palette[:256]):
            a = 255
            if alpha_mode in ("chroma", "luma") and (r, g, b) == (255, 255, 0):
                r = g = b = a = 0
            elif alpha_mode == "luma":
                mx = max(r, g, b)
                if mx <= 8:
                    r = g = b = a = 0
                else:
                    r = min(255, int(255.0 * r / mx))
                    g = min(255, int(255.0 * g / mx))
                    b = min(255, int(255.0 * b / mx))
                    a = mx
            table[i * 4] = r
            table[i * 4 + 1] = g
            table[i * 4 + 2] = b
            table[i * 4 + 3] = a
        out = bytearray(len(self.pixels) * 4)
        for i, idx in enumerate(self.pixels):
            base = idx * 4
            out[i * 4] = table[base]
            out[i * 4 + 1] = table[base + 1]
            out[i * 4 + 2] = table[base + 2]
            out[i * 4 + 3] = table[base + 3]
        return bytes(out)

    def chroma_transparent_count(self,
                                 palette_override: Palette | None = None) -> int:
        """How many pixels use a pure-yellow (transparent) palette entry."""

        if self.pixels is None:
            return 0
        palette = palette_override or self.palette
        if not palette:
            return 0
        yellow = {i for i, c in enumerate(palette[:256]) if c == (255, 255, 0)}
        if not yellow:
            return 0
        return sum(1 for idx in self.pixels if idx in yellow)


def _grayscale_palette() -> Palette:
    return [(i, i, i) for i in range(256)]


def _decode_ilbm_body(payload: bytes, width: int, height: int, n_planes: int,
                      compression: int, warnings: list[str]) -> bytes:
    """Planar-to-chunky conversion, port of ILBM_BODY_READ__sub0 (CONFIRMED)."""

    plane_row_bytes = (width + 7) // 8
    plane_row_bytes += plane_row_bytes & 1  # rows are word-aligned
    if plane_row_bytes > 128:
        warnings.append(
            f"Row plane size {plane_row_bytes} exceeds the engine's 128-byte "
            "plane buffer; decoding anyway with a wider buffer."
        )
    stride = max(plane_row_bytes, 128)

    pixels = bytearray(width * height)
    row_planes = bytearray(stride * max(n_planes, 1))
    pos = 0

    for y in range(height):
        for plane in range(n_planes):
            plane_off = plane * stride
            if compression:
                x = 0
                while x < plane_row_bytes:
                    if pos >= len(payload):
                        warnings.append(
                            f"BODY ran out of data at row {y}, plane {plane}."
                        )
                        return bytes(pixels)
                    n = payload[pos]
                    n = n - 256 if n > 127 else n
                    pos += 1
                    if n == -128:
                        continue
                    if n < 0:
                        count = -n + 1
                        if pos >= len(payload):
                            warnings.append(f"BODY truncated run at row {y}.")
                            return bytes(pixels)
                        value = payload[pos]
                        pos += 1
                        row_planes[plane_off + x : plane_off + x + count] = (
                            bytes([value]) * count
                        )
                        x += count
                    else:
                        count = n + 1
                        chunk = payload[pos : pos + count]
                        if len(chunk) < count:
                            warnings.append(f"BODY truncated literal at row {y}.")
                            return bytes(pixels)
                        row_planes[plane_off + x : plane_off + x + count] = chunk
                        pos += count
                        x += count
            else:
                chunk = payload[pos : pos + plane_row_bytes]
                if len(chunk) < plane_row_bytes:
                    warnings.append(f"Uncompressed BODY truncated at row {y}.")
                    return bytes(pixels)
                row_planes[plane_off : plane_off + plane_row_bytes] = chunk
                pos += plane_row_bytes

        row_base = y * width
        for x in range(width):
            byte_index = x >> 3
            bit_mask = 0x80 >> (x & 7)
            value = 0
            for plane in range(n_planes):
                if row_planes[plane * stride + byte_index] & bit_mask:
                    value |= 1 << plane
            pixels[row_base + x] = value

    if pos < len(payload) - 1:
        warnings.append(f"BODY has {len(payload) - pos} unused trailing byte(s).")
    return bytes(pixels)


def parse_ilbm_bytes(data: bytes, source_name: str = "<memory>") -> IlbmImage:
    tree = read_iff_bytes(data, source_name)
    return parse_ilbm_tree(tree)


def parse_ilbm_tree(tree: IffTree) -> IlbmImage:
    data = tree.data
    img = IlbmImage(source_name=tree.source_name)
    img.warnings.extend(tree.warnings)

    root = tree.roots[0] if tree.roots else None
    if root is None or not root.is_form():
        img.warnings.append("Not an IFF FORM file.")
        return img
    if root.is_form("ILBM"):
        img.kind = "ILBM"
    elif root.is_form("VBMP"):
        img.kind = "VBMP"
    else:
        img.warnings.append(
            f"FORM type {root.form_type!r} is not ILBM or VBMP."
        )
        return img

    body_chunk = None
    for chunk in root.children:
        if chunk.tag == "BMHD" and chunk.available_size >= 20:
            p = chunk.payload_offset
            (img.width, img.height, _x, _y) = struct.unpack_from(">HHHH", data, p)
            img.n_planes, img.masking, img.compression, _flags = struct.unpack_from(
                ">bbbb", data, p + 8
            )
            img.transparent_color = struct.unpack_from(">H", data, p + 12)[0]
        elif chunk.tag == "HEAD" and chunk.available_size >= 6:
            p = chunk.payload_offset
            img.width, img.height, _flags = struct.unpack_from(">HHH", data, p)
            img.n_planes = 8
            img.compression = 0
        elif chunk.tag == "CMAP":
            count = min(256, chunk.available_size // 3)
            p = chunk.payload_offset
            img.palette = [
                (data[p + i * 3], data[p + i * 3 + 1], data[p + i * 3 + 2])
                for i in range(count)
            ]
            if count < 256:
                img.palette.extend([(0, 0, 0)] * (256 - count))
                img.warnings.append(
                    f"CMAP holds {count} colors; padded to 256 with black."
                )
        elif chunk.tag == "BODY":
            body_chunk = chunk

    if body_chunk is not None and img.width > 0 and img.height > 0:
        payload = body_chunk.payload(data)
        if img.kind == "VBMP":
            expected = img.width * img.height
            if len(payload) < expected:
                img.warnings.append(
                    f"VBMP BODY has {len(payload)} bytes; expected {expected}."
                )
            img.pixels = bytes(payload[:expected].ljust(expected, b"\0"))
        else:
            img.pixels = _decode_ilbm_body(
                payload, img.width, img.height, img.n_planes,
                img.compression, img.warnings,
            )
    elif body_chunk is None and (img.width > 0 or img.height > 0):
        img.warnings.append("No BODY chunk found; metadata only.")

    if img.palette is None and img.pixels is not None:
        img.warnings.append(
            "No CMAP palette in file; an external palette (e.g. NORMAL.PAL) "
            "is required for correct colors."
        )
    return img


def parse_ilbm_file(path: str | Path) -> IlbmImage:
    file_path = Path(path)
    tree = read_iff_file(file_path)
    img = parse_ilbm_tree(tree)
    img.source_name = file_path.name
    return img


def parse_pal_file(path: str | Path) -> Palette | None:
    """Read the CMAP palette of a palette-only ILBM (.PAL) file."""

    try:
        img = parse_ilbm_file(path)
    except Exception:
        return None
    return img.palette


if __name__ == "__main__":
    import argparse

    cli = argparse.ArgumentParser(description="Inspect an ILBM/VBMP file (read-only).")
    cli.add_argument("file")
    args = cli.parse_args()

    parsed = parse_ilbm_file(args.file)
    print(f"{parsed.source_name}: {parsed.kind} {parsed.width}x{parsed.height} "
          f"planes={parsed.n_planes} compression={parsed.compression} "
          f"palette={'yes' if parsed.palette else 'no'} "
          f"body={'decoded' if parsed.has_body else 'absent'}")
    if parsed.pixels:
        used = sorted(set(parsed.pixels))
        print(f"indices used: {len(used)} (min {used[0]}, max {used[-1]})")
    for warning in parsed.warnings:
        print(f"WARNING: {warning}")
