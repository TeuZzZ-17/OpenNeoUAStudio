"""Generic, defensive IFF-like chunk reader for Urban Assault assets.

Urban Assault files (.base/.bas, .sklt/.skl, .ilbm/.ilb, .anm/.vanm, SET.BAS)
share an IFF-style layout: four ASCII bytes of chunk ID, a big-endian uint32
payload size, the payload, and one padding byte when the payload size is odd.
A FORM chunk's payload starts with a four-byte form type followed by child
chunks.

This module is strictly read-only.  It never raises on malformed data past the
first header: problems are recorded as warnings and the tree is truncated at
the damaged point, so unknown or broken chunks can still be inspected.

Confidence: the chunk framing itself is CONFIRMED (it matches both the OpenUA
runtime ``IFFile`` parser and the original 1996-1998 asset files byte for
byte).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import struct
from typing import Iterator


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


if __name__ == "__main__":
    import argparse
    import json

    cli = argparse.ArgumentParser(description="Dump the IFF chunk tree of a file.")
    cli.add_argument("file", help="asset file to inspect (read-only)")
    cli.add_argument("--json", action="store_true", help="print JSON instead of text")
    args = cli.parse_args()

    parsed = read_iff_file(args.file)
    if args.json:
        print(json.dumps(parsed.to_dict(), indent=2))
    else:
        print(parsed.dump_text())
        for warning in parsed.warnings:
            print(f"WARNING: {warning}")
