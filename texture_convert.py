"""Texture conversions: ILBM/VBMP -> PNG, PNG -> ILBM, VBMP -> ILBM.

Capabilities merged from the BASet project (same author, GPL v3), rebuilt on
top of OpenNeoUA Studio's own parsers so the format knowledge lives in one place:

  - decode side: ``ilbm_parser`` (engine-confirmed ILBM/VBMP reader)
  - IFF structure: ``iff_reader``
  - PNG I/O: Qt (QImage) - no Pillow dependency

All conversions are read-only for their inputs and write new files only.

PNG export preserves palette indices (indexed PNG with the CMAP as color
table); masking mode 2 becomes palette-index transparency (PNG tRNS).
PNG import (``convert_png_to_ilbm``) needs a template ILBM: it reuses the
template's BMHD/CMAP/extra chunks and replaces only the BODY, mapping any
out-of-palette color to the nearest palette entry (with a warning), then
re-parses the written file to verify the pixels survive a round-trip.

CLI:
    python texture_convert.py to-png INPUT [INPUT...] --out-dir DIR
                              [--pal FILE.PAL | --palette-ilbm FILE]
    python texture_convert.py png-to-ilbm PNG --template FILE.ILBM --out OUT
    python texture_convert.py vbmp-to-ilbm INPUT [INPUT...] --out-dir DIR
                              [--pal FILE.PAL | --palette-ilbm FILE]
"""

from __future__ import annotations

import argparse
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

from ilbm_parser import (
    IlbmImage,
    parse_ilbm_bytes,
    parse_ilbm_file,
    parse_pal_file,
)
from iff_reader import read_iff_file

Palette = list[tuple[int, int, int]]

# Fallback CMAP for VBMPs converted without any palette source (from BASet:
# the AIR1TXT.ILBM palette, the shipped vehicle-texture palette).
BUILTIN_PALETTE_SOURCE = "built-in AIR1TXT.ILBM CMAP"
BUILTIN_AIR1TXT_CMAP = bytes.fromhex(
    "ff ff 00 ff ff ff da da da 9b 9b 9b 6d 6d 6d 49 49 49 00 00 00 9b c5 d0 00 82 ff 00 00 ff ff 00 00 ff ab 1c 00 d9 51 c8 37 b2 ff ff 88 00 89 aa"
    "00 00 00 ff ff ff a5 e8 ff 6d bd b7 60 c2 a4 60 c7 92 9b c7 ac 62 b9 c9 63 b4 dc 64 b0 ef ce ba d8 dc b9 ab f7 c5 8d ff ef 65 ff 8d 47 ff 61 61"
    "00 00 00 ee ee ee 9a d8 ee 65 b0 aa 59 b5 99 59 b9 88 90 b9 a0 5b ac bb 5c a8 cd 5d a4 df c0 ad c9 cd ac 9f e6 b7 83 ee df 5e ee 83 42 ee 5a 5a"
    "00 00 00 dd dd dd 8f c9 dd 5e a3 9e 53 a8 8e 53 ac 7e 86 ac 95 54 a0 ae 55 9c be 56 98 cf b2 a1 bb be a0 94 d6 aa 7a dd cf 57 dd 7a 3d dd 54 54"
    "00 00 00 cc cc cc 84 b9 cc 57 97 92 4c 9b 83 4c 9f 74 7c 9f 89 4e 94 a0 4f 90 af 4f 8c bf a4 94 ac af 94 88 c5 9d 70 cc bf 50 cc 70 38 cc 4d 4d"
    "00 00 00 bb bb bb 79 aa bb 4f 8a 86 46 8e 78 46 91 6b 71 91 7e 47 87 93 48 84 a1 49 81 af 97 88 9e a1 87 7d b5 90 67 bb af 4a bb 67 34 bb 47 47"
    "00 00 00 aa aa aa 6e 9a aa 48 7e 7a 40 81 6d 40 84 61 67 84 72 41 7b 86 42 78 92 42 75 9f 89 7c 90 92 7b 72 a4 83 5e aa 9f 43 aa 5e 2f aa 40 40"
    "00 00 00 99 99 99 63 8b 99 41 71 6d 39 74 62 39 77 57 5d 77 67 3a 6f 78 3b 6c 83 3b 69 8f 7b 6f 81 83 6f 66 94 76 54 99 8f 3c 99 54 2a 99 3a 3a"
    "00 00 00 88 88 88 58 7b 88 3a 64 61 33 67 57 33 6a 4d 52 6a 5b 34 62 6b 34 60 75 35 5d 7f 6d 63 73 75 62 5b 83 69 4b 88 7f 35 88 4b 25 88 33 33"
    "00 00 00 77 77 77 4d 6c 77 32 58 55 2c 5a 4c 2c 5c 44 48 5c 50 2d 56 5d 2e 54 66 2e 52 6f 60 56 64 66 56 4f 73 5b 41 77 6f 2f 77 41 21 77 2d 2d"
    "00 00 00 66 66 66 42 5c 66 2b 4b 49 26 4d 41 26 4f 3a 3e 4f 44 27 4a 50 27 48 57 27 46 5f 52 4a 56 57 4a 44 62 4e 38 66 5f 28 66 38 1c 66 26 26"
    "00 00 00 55 55 55 37 4d 55 24 3f 3d 20 40 36 20 42 30 33 42 39 20 3d 43 21 3c 49 21 3a 4f 44 3e 48 49 3d 39 52 41 2f 55 4f 21 55 2f 17 55 20 20"
    "00 00 00 44 44 44 2c 3d 44 1d 32 30 19 33 2b 19 35 26 29 35 2d 1a 31 35 1a 30 3a 1a 2e 3f 36 31 39 3a 31 2d 41 34 25 44 3f 1a 44 25 12 44 19 19"
    "00 00 00 33 33 33 21 2e 33 15 25 24 13 26 20 13 27 1d 1f 27 22 13 25 28 13 24 2b 13 23 2f 29 25 2b 2b 25 22 31 27 1c 33 2f 14 33 1c 0e 33 13 13"
    "00 00 00 22 22 22 16 1e 22 0e 19 18 0c 19 15 0c 1a 13 14 1a 16 0d 18 1a 0d 18 1d 0d 17 1f 1b 18 1c 1d 18 16 20 1a 12 22 1f 0d 22 12 09 22 0c 0c"
    "00 00 00 11 11 11 0b 0f 11 07 0c 0c 06 0c 0a 06 0d 09 0a 0d 0b 06 0c 0d 06 0c 0e 06 0b 0f 0d 0c 0e 0e 0c 0b 10 0d 09 11 0f 06 11 09 04 11 06 06"
)


class TextureConvertError(Exception):
    pass


@dataclass
class ConvertResult:
    source: Path
    output: Path
    width: int
    height: int
    warning: str = ""


def _same_path(first: Path, second: Path) -> bool:
    """Compare existing files safely, with a fallback for new outputs."""

    try:
        if first.exists() and second.exists():
            return first.samefile(second)
    except OSError:
        pass
    try:
        return first.resolve() == second.resolve()
    except OSError:
        return first.absolute() == second.absolute()


def _require_separate_output(out_path: Path, *input_paths) -> None:
    for raw_path in input_paths:
        if raw_path is None:
            continue
        input_path = Path(raw_path)
        if _same_path(out_path, input_path):
            raise TextureConvertError(
                f"Output must not overwrite input file: {input_path}")


def _require_qimage():
    try:
        from PySide6.QtGui import QImage
    except ImportError as exc:  # pragma: no cover - PySide6 is a hard dep
        raise TextureConvertError(
            "PySide6 (QImage) is required for PNG conversion."
        ) from exc
    return QImage


def cmap_to_palette(cmap: bytes) -> Palette:
    entries = min(len(cmap) // 3, 256)
    return [(cmap[i * 3], cmap[i * 3 + 1], cmap[i * 3 + 2])
            for i in range(entries)]


def palette_to_cmap(palette: Palette) -> bytes:
    cmap = bytearray(768)
    for i, (r, g, b) in enumerate(palette[:256]):
        cmap[i * 3] = r
        cmap[i * 3 + 1] = g
        cmap[i * 3 + 2] = b
    return bytes(cmap)


def resolve_palette(pal_path: str | Path | None = None,
                    palette_ilbm_path: str | Path | None = None
                    ) -> tuple[Palette, str] | tuple[None, str]:
    """Palette from a .PAL file or a reference ILBM's CMAP, else None."""

    if pal_path:
        palette = parse_pal_file(pal_path)
        if palette is None:
            raise TextureConvertError(f"No CMAP palette found in {pal_path}")
        return palette, str(pal_path)
    if palette_ilbm_path:
        image = parse_ilbm_file(palette_ilbm_path)
        if image.palette is None:
            raise TextureConvertError(
                f"Reference ILBM has no CMAP: {palette_ilbm_path}")
        return image.palette, str(palette_ilbm_path)
    return None, ""


# -- ILBM / VBMP -> PNG -----------------------------------------------------------


def image_to_qimage(image: IlbmImage, palette: Palette | None = None):
    """Indexed-8 QImage preserving palette indices; masking 2 -> index alpha."""

    QImage = _require_qimage()
    if image.pixels is None:
        raise TextureConvertError(
            f"{image.source_name}: palette-only file, nothing to convert.")
    palette = palette or image.palette
    if palette is None:
        raise TextureConvertError(
            f"{image.source_name}: no palette (VBMP has no CMAP) - pass a "
            ".PAL file or a reference ILBM, or use the built-in fallback.")

    qimage = QImage(image.width, image.height, QImage.Format.Format_Indexed8)
    table = []
    transparent = (image.transparent_color & 0xFF
                   if image.masking == 2 else None)
    for index in range(256):
        r, g, b = palette[index] if index < len(palette) else (0, 0, 0)
        alpha = 0 if transparent is not None and index == transparent else 255
        table.append((alpha << 24) | (r << 16) | (g << 8) | b)
    qimage.setColorCount(256)
    qimage.setColorTable(table)

    stride = qimage.bytesPerLine()
    buffer = qimage.bits()
    for y in range(image.height):
        row = image.pixels[y * image.width:(y + 1) * image.width]
        buffer[y * stride:y * stride + image.width] = row
    return qimage


def convert_to_png(source: str | Path, out_path: str | Path,
                   pal_path: str | Path | None = None,
                   palette_ilbm_path: str | Path | None = None,
                   allow_builtin: bool = True) -> ConvertResult:
    """Convert one ILBM or VBMP file to an indexed PNG."""

    source = Path(source)
    out_path = Path(out_path)
    _require_separate_output(out_path, source, pal_path, palette_ilbm_path)
    image = parse_ilbm_file(source)
    palette, palette_source = resolve_palette(pal_path, palette_ilbm_path)
    warning = ""
    if palette is None and image.palette is None:
        if not allow_builtin:
            raise TextureConvertError(
                f"{source.name}: no palette available (VBMP without CMAP).")
        palette = cmap_to_palette(BUILTIN_AIR1TXT_CMAP)
        warning = f"no palette source; used {BUILTIN_PALETTE_SOURCE}"
    if image.masking == 1:
        warning = (warning + "; " if warning else "") + \
            "masking mode 1 (mask plane) is ignored, engine parity"

    qimage = image_to_qimage(image, palette)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not qimage.save(str(out_path), "PNG"):
        raise TextureConvertError(f"Could not write PNG: {out_path}")
    if palette_source and image.palette is None:
        warning = (warning + "; " if warning else "") + \
            f"palette from {palette_source}"
    return ConvertResult(source, out_path, image.width, image.height, warning)


def ilbm_image_to_png(image: IlbmImage, out_path: str | Path,
                      palette_override: Palette | None = None) -> None:
    """Write an already parsed IlbmImage (e.g. a loaded family texture)."""

    out_path = Path(out_path)
    qimage = image_to_qimage(image, palette_override)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not qimage.save(str(out_path), "PNG"):
        raise TextureConvertError(f"Could not write PNG: {out_path}")


# -- IFF building helpers -----------------------------------------------------------


def iff_chunk(tag: bytes, payload: bytes) -> bytes:
    if len(tag) != 4:
        raise TextureConvertError("IFF chunk tag must be 4 bytes")
    data = bytearray(tag)
    data.extend(len(payload).to_bytes(4, "big"))
    data.extend(payload)
    if len(payload) & 1:
        data.append(0)
    return bytes(data)


def row_bytes_per_plane(width: int) -> int:
    return ((width + 15) // 16) * 2


def pack_ilbm_body(indexed_pixels: bytes, width: int, height: int,
                   planes: int) -> bytes:
    """Chunky-to-planar conversion (inverse of the engine's BODY reader)."""

    if len(indexed_pixels) != width * height:
        raise TextureConvertError(
            f"pixel data size {len(indexed_pixels)} does not match "
            f"{width}x{height}")
    row_bytes = row_bytes_per_plane(width)
    body = bytearray(height * planes * row_bytes)
    pos = 0
    for y in range(height):
        row = indexed_pixels[y * width:(y + 1) * width]
        for plane in range(planes):
            for byte_x in range(row_bytes):
                value = 0
                base_x = byte_x * 8
                for bit_pos in range(8):
                    x = base_x + bit_pos
                    if x < width and ((row[x] >> plane) & 1):
                        value |= 0x80 >> bit_pos
                body[pos] = value
                pos += 1
    return bytes(body)


def byterun1_pack_row(row: bytes) -> bytes:
    out = bytearray()
    i = 0
    n = len(row)
    while i < n:
        run_len = 1
        while i + run_len < n and run_len < 128 and row[i + run_len] == row[i]:
            run_len += 1
        if run_len >= 3:
            out.append((257 - run_len) & 0xFF)
            out.append(row[i])
            i += run_len
            continue
        literal_start = i
        i += 1
        while i < n:
            run_len = 1
            while i + run_len < n and run_len < 128 \
                    and row[i + run_len] == row[i]:
                run_len += 1
            if run_len >= 3 or i - literal_start >= 128:
                break
            i += 1
        literal = row[literal_start:i]
        out.append(len(literal) - 1)
        out.extend(literal)
    return bytes(out)


def compress_body_byterun1(raw_body: bytes, width: int, height: int,
                           planes: int) -> bytes:
    row_bytes = row_bytes_per_plane(width)
    if len(raw_body) != height * planes * row_bytes:
        raise TextureConvertError("raw BODY size mismatch during compression")
    out = bytearray()
    pos = 0
    for _ in range(height * planes):
        out.extend(byterun1_pack_row(raw_body[pos:pos + row_bytes]))
        pos += row_bytes
    return bytes(out)


# -- VBMP -> standalone ILBM -----------------------------------------------------


def write_image_as_ilbm(image: IlbmImage, out_path: str | Path,
                        palette_override: Palette | None = None,
                        source: str | Path | None = None,
                        warning: str = "") -> ConvertResult:
    """Write a decoded VBMP/ILBM image as a standalone FORM ILBM.

    This is the in-memory path used by the SET.BAS browser.  It avoids the
    pointless detour of first dumping an embedded ``FORM VBMP`` to disk only
    to read it again.  The output is always verified by the shared parser.
    """

    out_path = Path(out_path)
    if image.pixels is None:
        raise TextureConvertError(
            f"{image.source_name or 'texture'}: no BODY pixels found.")
    palette = palette_override or image.palette
    if palette is None:
        palette = cmap_to_palette(BUILTIN_AIR1TXT_CMAP)
        warning = warning or (
            f"no palette source; used {BUILTIN_PALETTE_SOURCE}")

    bmhd = struct.pack(
        ">HHhhBBBBHBBHH",
        image.width, image.height, 0, 0,
        8,              # nPlanes
        0,              # masking: none
        0,              # compression: none
        0,              # pad1
        0,              # transparentColor
        10, 10,         # aspect
        image.width, image.height,
    )
    body = pack_ilbm_body(image.pixels, image.width, image.height, 8)
    payload = (b"ILBM" + iff_chunk(b"BMHD", bmhd)
               + iff_chunk(b"CMAP", palette_to_cmap(palette))
               + iff_chunk(b"BODY", body))
    out_bytes = iff_chunk(b"FORM", payload)

    verify = parse_ilbm_bytes(out_bytes, str(out_path))
    if (verify.kind != "ILBM"
            or (verify.width, verify.height) != (image.width, image.height)
            or verify.pixels != image.pixels):
        raise TextureConvertError(
            f"verification failed: {out_path} does not re-parse to the "
            "source pixels")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(out_bytes)
    source_path = Path(source) if source is not None else Path(
        image.source_name or "embedded_texture.VBMP")
    return ConvertResult(source_path, out_path, image.width, image.height,
                         warning)


def convert_vbmp_to_ilbm(source: str | Path, out_path: str | Path,
                         pal_path: str | Path | None = None,
                         palette_ilbm_path: str | Path | None = None,
                         allow_builtin: bool = True) -> ConvertResult:
    """Wrap a raw VBMP into a standard FORM ILBM (8 planes, uncompressed)."""

    source = Path(source)
    out_path = Path(out_path)
    _require_separate_output(out_path, source, pal_path, palette_ilbm_path)
    image = parse_ilbm_file(source)
    palette, palette_source = resolve_palette(pal_path, palette_ilbm_path)
    warning = ""
    if palette is None:
        palette = image.palette
    if palette is None and not allow_builtin:
        raise TextureConvertError(f"{source.name}: no palette available.")
    if palette is None:
        palette = cmap_to_palette(BUILTIN_AIR1TXT_CMAP)
        warning = f"no palette source; used {BUILTIN_PALETTE_SOURCE}"
    elif palette_source:
        warning = f"palette from {palette_source}"
    return write_image_as_ilbm(
        image, out_path, palette, source=source, warning=warning)


# -- PNG -> ILBM (template-based) ---------------------------------------------------


def _qimage_to_indices(qimage, palette: Palette) -> tuple[bytes, bool]:
    """Map a QImage to palette indices; True when nearest-color was needed."""

    QImage = _require_qimage()
    width = qimage.width()
    height = qimage.height()

    # Fast path: indexed PNG whose color table already matches the palette.
    if qimage.format() == QImage.Format.Format_Indexed8:
        table = qimage.colorTable()
        same = len(table) >= len(palette) and all(
            ((table[i] >> 16) & 0xFF, (table[i] >> 8) & 0xFF, table[i] & 0xFF)
            == palette[i]
            for i in range(len(palette))
        )
        if same:
            stride = qimage.bytesPerLine()
            raw = bytes(qimage.bits())
            out = bytearray(width * height)
            for y in range(height):
                out[y * width:(y + 1) * width] = \
                    raw[y * stride:y * stride + width]
            return bytes(out), False

    rgb = qimage.convertToFormat(QImage.Format.Format_RGB32)
    stride = rgb.bytesPerLine()
    raw = bytes(rgb.bits())
    exact = {color: index for index, color in enumerate(palette)}
    cache: dict[tuple[int, int, int], int] = {}
    out = bytearray(width * height)
    warned = False
    for y in range(height):
        base = y * stride
        for x in range(width):
            offset = base + x * 4          # BGRA little-endian layout
            color = (raw[offset + 2], raw[offset + 1], raw[offset])
            index = exact.get(color)
            if index is None:
                index = cache.get(color)
                if index is None:
                    r, g, b = color
                    index = min(
                        range(len(palette)),
                        key=lambda i: (r - palette[i][0]) ** 2
                        + (g - palette[i][1]) ** 2
                        + (b - palette[i][2]) ** 2,
                    )
                    cache[color] = index
                warned = True
            out[y * width + x] = index
    return bytes(out), warned


def convert_png_to_ilbm(png_path: str | Path, template_path: str | Path,
                        out_path: str | Path) -> ConvertResult:
    """Rebuild an ILBM from an edited PNG, using a template ILBM.

    The template provides BMHD, CMAP and every non-BODY chunk verbatim;
    only the BODY is regenerated from the PNG (ByteRun1-compressed when the
    template is).  The written file is re-parsed and its pixels compared
    for an exact round-trip."""

    png_path = Path(png_path)
    template_path = Path(template_path)
    out_path = Path(out_path)
    _require_separate_output(out_path, png_path, template_path)

    template = parse_ilbm_file(template_path)
    if template.palette is None:
        raise TextureConvertError(
            f"template {template_path.name} has no CMAP palette")
    tree = read_iff_file(template_path)
    root = tree.roots[0] if tree.roots else None
    if root is None or root.tag != "FORM" or root.form_type != "ILBM":
        raise TextureConvertError(
            f"template {template_path.name} is not a FORM ILBM")
    body = tree.find_first("BODY")
    if body is None:
        raise TextureConvertError(
            f"template {template_path.name} has no BODY chunk")
    cmap = tree.find_first("CMAP")
    if not 1 <= template.n_planes <= 8:
        raise TextureConvertError(
            f"template {template_path.name} has unsupported bit depth "
            f"{template.n_planes}; expected 1..8 planes")
    if template.compression not in (0, 1):
        raise TextureConvertError(
            f"template {template_path.name} has unsupported compression "
            f"mode {template.compression}; expected 0 or ByteRun1 (1)")
    required_colors = 1 << template.n_planes
    required_cmap = required_colors * 3
    if cmap is None or cmap.size < required_cmap:
        raise TextureConvertError(
            f"template {template_path.name} CMAP is too small for "
            f"{template.n_planes} planes")

    QImage = _require_qimage()
    qimage = QImage(str(png_path))
    if qimage.isNull():
        raise TextureConvertError(f"could not read PNG: {png_path}")
    if (qimage.width(), qimage.height()) != (template.width, template.height):
        raise TextureConvertError(
            f"PNG size {qimage.width()}x{qimage.height()} does not match "
            f"template {template.width}x{template.height}")

    indices, palette_warning = _qimage_to_indices(
        qimage, template.palette[:required_colors])
    raw_body = pack_ilbm_body(indices, template.width, template.height,
                              template.n_planes)
    new_body = (compress_body_byterun1(raw_body, template.width,
                                       template.height, template.n_planes)
                if template.compression else raw_body)

    data = template_path.read_bytes()
    body_end = body.payload_end + (body.size & 1)
    payload = (b"ILBM"
               + data[root.payload_offset + 4:body.offset]
               + iff_chunk(b"BODY", new_body)
               + data[body_end:root.payload_end])
    out_bytes = iff_chunk(b"FORM", payload)

    verify = parse_ilbm_bytes(out_bytes, str(out_path))
    if (verify.kind != "ILBM"
            or (verify.width, verify.height)
            != (template.width, template.height)
            or verify.pixels != indices):
        raise TextureConvertError(
            f"verification failed: {out_path} does not re-parse to the "
            "converted pixels")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(out_bytes)
    warning = ("PNG had colors outside the template palette; mapped to "
               "nearest palette colors" if palette_warning else "")
    return ConvertResult(png_path, out_path, template.width, template.height,
                         warning)


# -- CLI ---------------------------------------------------------------------------


def _iter_inputs(paths: list[str], suffixes: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            files.extend(p for p in sorted(path.rglob("*"))
                         if p.is_file() and p.suffix.lower() in suffixes)
        elif path.is_file():
            files.append(path)
        else:
            raise TextureConvertError(f"input not found: {path}")
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="OpenNeoUA Studio texture conversions (merged from BASet).")
    sub = parser.add_subparsers(dest="command", required=True)

    to_png = sub.add_parser("to-png", help="ILBM/VBMP -> indexed PNG")
    to_png.add_argument("inputs", nargs="+", help="files or folders")
    to_png.add_argument("--out", help="output PNG (single input file only)")
    to_png.add_argument("--out-dir", help="output folder")
    to_png.add_argument("--pal", help=".PAL palette for VBMP inputs")
    to_png.add_argument("--palette-ilbm", help="reference ILBM with CMAP")

    p2i = sub.add_parser("png-to-ilbm", help="edited PNG -> ILBM (template)")
    p2i.add_argument("png", help="edited PNG file")
    p2i.add_argument("--template", required=True, help="template ILBM")
    p2i.add_argument("--out", required=True, help="output ILBM path")

    v2i = sub.add_parser("vbmp-to-ilbm", help="raw VBMP -> standalone ILBM")
    v2i.add_argument("inputs", nargs="+", help="files or folders")
    v2i.add_argument("--out", help="output ILBM (single input file only)")
    v2i.add_argument("--out-dir", help="output folder")
    v2i.add_argument("--pal", help=".PAL palette file")
    v2i.add_argument("--palette-ilbm", help="reference ILBM with CMAP")

    args = parser.parse_args(argv)
    try:
        if args.command == "png-to-ilbm":
            result = convert_png_to_ilbm(args.png, args.template, args.out)
            note = f" [{result.warning}]" if result.warning else ""
            print(f"[OK] {result.source} -> {result.output} "
                  f"({result.width}x{result.height}){note}")
            return 0

        suffixes = ((".ilbm", ".ilb", ".iff", ".lbm", ".vbmp")
                    if args.command == "to-png" else (".vbmp", ".ilbm", ".ilb"))
        files = _iter_inputs(args.inputs, suffixes)
        if not files:
            print("no input files found", file=sys.stderr)
            return 1
        if args.out and len(files) != 1:
            parser.error("--out needs exactly one input file; use --out-dir")
        if not args.out and not args.out_dir:
            parser.error("--out or --out-dir is required")

        converter = (convert_to_png if args.command == "to-png"
                     else convert_vbmp_to_ilbm)
        new_suffix = ".png" if args.command == "to-png" else ".ILBM"
        converted = 0
        errors = 0
        for path in files:
            out = (Path(args.out) if args.out
                   else Path(args.out_dir) / (path.stem + new_suffix))
            try:
                result = converter(path, out, args.pal, args.palette_ilbm)
            except TextureConvertError as exc:
                errors += 1
                print(f"[ERROR] {path.name}: {exc}")
                continue
            converted += 1
            note = f" [{result.warning}]" if result.warning else ""
            print(f"[OK] {path} -> {result.output} "
                  f"({result.width}x{result.height}){note}")
        print(f"\nSummary: {converted} converted, {errors} error(s)")
        return 0 if converted and not errors else 1
    except TextureConvertError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
