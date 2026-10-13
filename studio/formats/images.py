"""IFF chunk reader, ILBM/VBMP indexed textures and texture conversions
(ILBM/VBMP <-> PNG).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import struct
from typing import Iterator
from studio.exchange.portable import same_path

# ----------------------------------------------------------------------------
# Generic, defensive IFF-like chunk reader for Urban Assault assets.
# (formerly iff_reader.py)
# ----------------------------------------------------------------------------

MAX_DEPTH = 64


MAX_FILE_SIZE = 512 * 1024 * 1024


class IffReadError(Exception):
    """Raised only for unusable input (missing file, empty data)."""


@dataclass
class IffChunk:
    """A single chunk in the tree.  ``children`` is non-empty only for FORMs."""

    tag: str
    offset: int
    size: int
    payload_offset: int
    depth: int
    form_type: str = ""
    children: list["IffChunk"] = field(default_factory=list)
    truncated: bool = False
    actual_size: int | None = None

    @property
    def payload_end(self) -> int:
        """Declared payload end (kept for compatibility with existing users)."""

        return self.payload_offset + self.size

    @property
    def available_size(self) -> int:
        """Payload bytes that are actually present in the input buffer."""

        return self.size if self.actual_size is None else self.actual_size

    @property
    def available_payload_end(self) -> int:
        return self.payload_offset + self.available_size

    @property
    def display_name(self) -> str:
        return f"{self.tag} ({self.form_type})" if self.form_type else self.tag

    def payload(self, data: bytes) -> bytes:
        return data[
            self.payload_offset : min(self.available_payload_end, len(data))
        ]

    def is_form(self, form_type: str | None = None) -> bool:
        # UA form types are four bytes and may be space-padded ("MC2 ", "ADE ").
        if self.tag != "FORM":
            return False
        return form_type is None or self.form_type.strip() == form_type.strip()

    def iter_all(self) -> Iterator["IffChunk"]:
        yield self
        for child in self.children:
            yield from child.iter_all()

    def find_all(self, tag: str, form_type: str | None = None) -> list["IffChunk"]:
        found = []
        for chunk in self.iter_all():
            if chunk.tag == tag and (
                form_type is None or chunk.form_type.strip() == form_type.strip()
            ):
                found.append(chunk)
        return found

    def find_first(self, tag: str, form_type: str | None = None) -> "IffChunk | None":
        found = self.find_all(tag, form_type)
        return found[0] if found else None

    def to_dict(self, data: bytes | None = None, preview_bytes: int = 16) -> dict:
        entry: dict = {
            "tag": self.tag,
            "form_type": self.form_type or None,
            "offset": self.offset,
            "size": self.size,
            "actual_size": self.available_size,
            "payload_offset": self.payload_offset,
            "truncated": self.truncated,
        }
        if data is not None and not self.children and self.size > 0:
            raw = self.payload(data)[:preview_bytes]
            entry["payload_preview_hex"] = raw.hex(" ")
        if self.children:
            entry["children"] = [c.to_dict(data, preview_bytes) for c in self.children]
        return entry


@dataclass
class IffTree:
    """Result of parsing one file: top-level chunks plus warnings."""

    source_name: str
    data: bytes
    roots: list[IffChunk] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def iter_all(self) -> Iterator[IffChunk]:
        for root in self.roots:
            yield from root.iter_all()

    def find_all(self, tag: str, form_type: str | None = None) -> list[IffChunk]:
        found = []
        for root in self.roots:
            found.extend(root.find_all(tag, form_type))
        return found

    def find_first(self, tag: str, form_type: str | None = None) -> IffChunk | None:
        found = self.find_all(tag, form_type)
        return found[0] if found else None

    def to_dict(self, preview_bytes: int = 16) -> dict:
        return {
            "source": self.source_name,
            "file_size": len(self.data),
            "warnings": list(self.warnings),
            "chunks": [r.to_dict(self.data, preview_bytes) for r in self.roots],
        }

    def dump_text(self) -> str:
        lines = []
        for chunk in self.iter_all():
            marker = "  !TRUNCATED" if chunk.truncated else ""
            lines.append(
                f"{'  ' * chunk.depth}{chunk.display_name}  size={chunk.size}  "
                f"off=0x{chunk.offset:X}{marker}"
            )
        return "\n".join(lines)


def _decode_tag(raw: bytes) -> str:
    return "".join(chr(b) if 32 <= b <= 126 else "." for b in raw)


def _parse_stream(
    data: bytes, start: int, end: int, depth: int, tree: IffTree
) -> list[IffChunk]:
    chunks: list[IffChunk] = []
    if depth > MAX_DEPTH:
        tree.warnings.append(
            f"FORM nesting deeper than {MAX_DEPTH} at offset 0x{start:X}; ignored."
        )
        return chunks

    offset = start
    while offset < end:
        remaining = end - offset
        if remaining < 8:
            # Trailing pad bytes are normal at container ends; only warn for
            # leftovers that cannot be padding.
            if remaining > 1 or (remaining == 1 and data[offset] != 0):
                tree.warnings.append(
                    f"Truncated chunk header at offset 0x{offset:X} "
                    f"({remaining} byte(s) remain)."
                )
            return chunks

        tag = _decode_tag(data[offset : offset + 4])
        size = struct.unpack_from(">I", data, offset + 4)[0]
        payload_offset = offset + 8
        declared_end = payload_offset + size
        truncated = declared_end > end

        form_type = ""
        if tag == "FORM" and min(declared_end, end) - payload_offset >= 4:
            form_type = _decode_tag(data[payload_offset : payload_offset + 4])

        chunk = IffChunk(
            tag=tag,
            offset=offset,
            size=size,
            payload_offset=payload_offset,
            depth=depth,
            actual_size=max(0, min(declared_end, end) - payload_offset),
            form_type=form_type,
            truncated=truncated,
        )
        chunks.append(chunk)

        if truncated:
            tree.warnings.append(
                f"Chunk {chunk.display_name} at 0x{offset:X} declares {size} bytes "
                f"but only {max(0, end - payload_offset)} are available."
            )

        if tag == "FORM" and form_type:
            chunk.children = _parse_stream(
                data, payload_offset + 4, min(declared_end, end), depth + 1, tree
            )

        next_offset = declared_end + (size & 1)
        if next_offset <= offset:
            tree.warnings.append(f"Invalid chunk size at offset 0x{offset:X}.")
            return chunks
        offset = min(next_offset, end)

    return chunks


def read_iff_bytes(data: bytes, source_name: str = "<memory>") -> IffTree:
    """Parse an in-memory IFF byte stream into a chunk tree."""

    tree = IffTree(source_name=source_name, data=data)
    if not data:
        tree.warnings.append("The file is empty.")
        return tree
    if len(data) < 8:
        tree.warnings.append("The file is too small to contain an IFF chunk.")
        return tree
    if data[:4] != b"FORM":
        tree.warnings.append("The file does not begin with a FORM chunk.")
    tree.roots = _parse_stream(data, 0, len(data), 0, tree)
    return tree


def read_iff_file(path: str | Path) -> IffTree:
    """Read a file from disk (read-only) and parse it into a chunk tree."""

    file_path = Path(path)
    try:
        size = file_path.stat().st_size
        if size > MAX_FILE_SIZE:
            raise IffReadError(
                f"File is too large ({size} bytes; limit is {MAX_FILE_SIZE})."
            )
        data = file_path.read_bytes()
    except OSError as exc:
        raise IffReadError(f"Could not open file: {exc}") from exc
    return read_iff_bytes(data, file_path.name)


def read_cstring(data: bytes, start: int, limit: int) -> tuple[str, int]:
    """Read a NUL-terminated latin-1 string; returns (text, position after NUL)."""

    end = data.find(b"\0", start, limit)
    if end < 0:
        return data[start:limit].decode("latin-1", errors="replace"), limit
    return data[start:end].decode("latin-1", errors="replace"), end + 1


# ----------------------------------------------------------------------------
# Read-only decoder for Urban Assault ILBM / VBMP indexed textures.
# (formerly ilbm_parser.py)
# ----------------------------------------------------------------------------

Palette = list[tuple[int, int, int]]




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


# ----------------------------------------------------------------------------
# Texture conversions: ILBM/VBMP -> PNG, PNG -> ILBM, VBMP -> ILBM.
# (formerly texture_convert.py)
# ----------------------------------------------------------------------------

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




def _require_separate_output(out_path: Path, *input_paths) -> None:
    for raw_path in input_paths:
        if raw_path is None:
            continue
        input_path = Path(raw_path)
        if same_path(out_path, input_path):
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
