"""Read-only reader for Urban Assault SET.BAS embedded resource archives.

SET.BAS is one big IFF file whose FORM tree contains EMRS records
(CONFIRMED, UA_source/src/embed.cpp and the BASet extractor):

    EMRS  "class.name\\0resource_name\\0" [+ optional inline payload chunk]
    <payload chunk>   when not inline: the next sibling chunk in the stream

Known payload classes (SET1 census: 1119 EMRS = 28 VBMP for ilbm.class,
798 SKLT for sklt.class, 293 VANM for bmpanim.class):

    ilbm.class    -> FORM VBMP (raw 8bpp) or FORM ILBM
    sklt.class    -> FORM SKLT
    bmpanim.class -> FORM VANM

Everything stays in memory: payloads are decoded straight from the loaded
byte buffer with the existing OpenNeoUAStudio parsers.  This module never writes,
moves or repacks anything.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
import struct

from asset_resolver import EXTENSION_ALIASES, normalize_logical_name

MAX_SETBAS_SIZE = 512 * 1024 * 1024

DECODABLE_CLASSES = {"ilbm.class", "sklt.class", "bmpanim.class"}


class SetBasError(Exception):
    pass


@dataclass
class SetBasResource:
    index: int
    class_id: str
    resource_name: str
    emrs_offset: int
    payload_tag: str = ""
    payload_form_type: str = ""
    payload_offset: int = 0
    payload_size: int = 0          # full chunk bytes (header included)
    payload_source: str = "none"   # "inline" | "next_sibling" | "none"
    error: str = ""

    @property
    def decodable(self) -> bool:
        return (not self.error and self.payload_source != "none"
                and self.class_id.lower() in DECODABLE_CLASSES)

    @property
    def display_payload(self) -> str:
        if self.payload_tag == "FORM" and self.payload_form_type:
            return self.payload_form_type.strip()
        return self.payload_tag or "?"


@dataclass
class SetBasArchive:
    path: Path
    data: bytes = b""
    resources: list[SetBasResource] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # normalized bare filename (lower) -> resources with that name
    _by_name: dict[str, list[SetBasResource]] = field(default_factory=dict)

    def census(self) -> dict[str, int]:
        counts = Counter(r.class_id for r in self.resources)
        return dict(sorted(counts.items()))

    def payload_census(self) -> dict[str, int]:
        counts = Counter(r.display_payload for r in self.resources
                         if r.payload_source != "none")
        return dict(sorted(counts.items()))

    def payload_bytes(self, resource: SetBasResource) -> bytes:
        return self.data[resource.payload_offset:
                         resource.payload_offset + resource.payload_size]

    def find(self, logical_name: str,
             class_id: str | None = None) -> list[SetBasResource]:
        """Find resources matching a logical name (case-insensitive,
        extension aliases and Amiga separators honored, subfolders like
        Skeleton/ compared on the bare filename as the engine does)."""

        normalized = normalize_logical_name(logical_name)
        bare = normalized.rsplit("/", 1)[-1].lower()
        names = {bare}
        stem, dot, ext = bare.rpartition(".")
        if dot:
            for alias in EXTENSION_ALIASES.get("." + ext, ()):
                names.add(stem + alias)

        found: list[SetBasResource] = []
        for name in names:
            for resource in self._by_name.get(name, []):
                if class_id and resource.class_id.lower() != class_id.lower():
                    continue
                if resource not in found:
                    found.append(resource)
        return found


def _read_cstring(data: bytes, pos: int, limit: int) -> tuple[str, int]:
    end = data.find(b"\0", pos, limit)
    if end < 0:
        raise SetBasError(f"unterminated string at 0x{pos:X}")
    return data[pos:end].decode("latin-1", errors="replace"), end + 1


def _chunk_at(data: bytes, offset: int, end: int):
    if offset + 8 > end:
        return None
    tag = data[offset:offset + 4].decode("latin-1", errors="replace")
    size = struct.unpack_from(">I", data, offset + 4)[0]
    payload_end = offset + 8 + size
    if payload_end > end:
        return None
    form_type = ""
    if tag == "FORM" and size >= 4:
        form_type = data[offset + 8:offset + 12].decode("latin-1", "replace")
    return tag, size, payload_end + (size & 1), form_type


def _walk_emrs(data: bytes, start: int, end: int,
               archive: SetBasArchive) -> None:
    offset = start
    pending: SetBasResource | None = None

    while offset < end:
        chunk = _chunk_at(data, offset, end)
        if chunk is None:
            if end - offset > 1:
                archive.warnings.append(
                    f"Truncated chunk at 0x{offset:X}; scan stopped early."
                )
            return
        tag, size, next_offset, form_type = chunk

        if pending is not None:
            # embed.cpp: the payload of the previous EMRS is the next sibling.
            pending.payload_tag = tag
            pending.payload_form_type = form_type
            pending.payload_offset = offset
            pending.payload_size = next_offset - offset
            pending.payload_source = "next_sibling"
            pending = None
        elif tag == "EMRS":
            resource = SetBasResource(
                index=len(archive.resources), class_id="",
                resource_name="", emrs_offset=offset,
            )
            archive.resources.append(resource)
            try:
                payload_limit = offset + 8 + size
                class_id, pos = _read_cstring(data, offset + 8, payload_limit)
                name, pos = _read_cstring(data, pos, payload_limit)
                resource.class_id = class_id
                resource.resource_name = name
                while pos < payload_limit and data[pos] == 0:
                    pos += 1
                inline = _chunk_at(data, pos, payload_limit)
                if inline is not None:
                    in_tag, _in_size, in_next, in_form = inline
                    resource.payload_tag = in_tag
                    resource.payload_form_type = in_form
                    resource.payload_offset = pos
                    resource.payload_size = in_next - pos
                    resource.payload_source = "inline"
                else:
                    pending = resource
            except SetBasError as exc:
                resource.error = str(exc)
        elif tag == "FORM":
            _walk_emrs(data, offset + 12, offset + 8 + size, archive)

        offset = next_offset

    if pending is not None:
        pending.error = "missing payload (EMRS at end of container)"
        archive.warnings.append(
            f"EMRS {pending.resource_name!r} at 0x{pending.emrs_offset:X} "
            "has no payload chunk."
        )


def read_setbas(path: str | Path) -> SetBasArchive:
    """Load and index a SET.BAS archive, strictly read-only."""

    file_path = Path(path)
    try:
        size = file_path.stat().st_size
        if size > MAX_SETBAS_SIZE:
            raise SetBasError(f"file too large ({size} bytes)")
        data = file_path.read_bytes()
    except OSError as exc:
        raise SetBasError(f"could not open SET.BAS: {exc}") from exc

    archive = SetBasArchive(path=file_path, data=data)
    if len(data) < 12 or data[:4] != b"FORM":
        raise SetBasError("not an IFF FORM file")

    root = _chunk_at(data, 0, len(data))
    if root is None:
        raise SetBasError("truncated root FORM")
    _tag, root_size, _next, _form = root
    _walk_emrs(data, 12, 8 + root_size, archive)

    for resource in archive.resources:
        bare = normalize_logical_name(resource.resource_name)
        bare = bare.rsplit("/", 1)[-1].lower()
        archive._by_name.setdefault(bare, []).append(resource)

    duplicates = {name: len(items) for name, items in archive._by_name.items()
                  if len(items) > 1}
    if duplicates:
        archive.warnings.append(
            f"{len(duplicates)} resource name(s) appear more than once in the "
            f"archive (first few: "
            f"{', '.join(list(duplicates)[:4])}); callers must disambiguate "
            "them before decoding."
        )
    return archive


# -- in-memory decoding helpers -------------------------------------------------


def decode_skeleton(archive: SetBasArchive, resource: SetBasResource):
    """Decode a sklt.class payload with the existing SKLT parser."""

    from sklt_parser import parse_sklt_bytes

    return parse_sklt_bytes(archive.payload_bytes(resource),
                            f"SET.BAS:{resource.resource_name}")


def decode_texture(archive: SetBasArchive, resource: SetBasResource):
    """Decode an ilbm.class payload (FORM VBMP or FORM ILBM)."""

    from ilbm_parser import parse_ilbm_bytes

    img = parse_ilbm_bytes(archive.payload_bytes(resource),
                           f"SET.BAS:{resource.resource_name}")
    return img


def decode_animation(archive: SetBasArchive, resource: SetBasResource):
    """Decode a bmpanim.class payload (FORM VANM)."""

    from anm_parser import parse_anm_bytes

    return parse_anm_bytes(archive.payload_bytes(resource),
                           f"SET.BAS:{resource.resource_name}")


if __name__ == "__main__":
    import argparse

    cli = argparse.ArgumentParser(
        description="Inspect a SET.BAS archive (strictly read-only)."
    )
    cli.add_argument("setbas")
    cli.add_argument("--find", help="look up a logical resource name")
    cli.add_argument("--list", action="store_true", help="list all resources")
    args = cli.parse_args()

    parsed = read_setbas(args.setbas)
    print(f"{parsed.path}: {len(parsed.resources)} EMRS resources")
    print("classes:", parsed.census())
    print("payloads:", parsed.payload_census())
    for warning in parsed.warnings:
        print(f"WARNING: {warning}")

    if args.find:
        for resource in parsed.find(args.find):
            print(f"  {resource.class_id} {resource.resource_name} "
                  f"({resource.display_payload}, {resource.payload_size} bytes "
                  f"@ 0x{resource.payload_offset:X}, {resource.payload_source})")
    if args.list:
        for resource in parsed.resources:
            print(f"  [{resource.index:4}] {resource.class_id:14} "
                  f"{resource.resource_name:32} {resource.display_payload:5} "
                  f"{resource.payload_size:8} bytes  {resource.error}")
