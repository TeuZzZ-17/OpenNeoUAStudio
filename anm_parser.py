"""Read-only parser for Urban Assault .ANM / VANM texture animations.

Layout (CONFIRMED, mirrors OpenUA UA_source/src/bmpAnm.cpp and verified byte
for byte against original FLAK1.ANM):

    FORM VANM
      DATA  sequential, non-chunked stream:
        1. s16 size, then size bytes: bitmap class name ("ilbm.class\\0")
        2. s16 size, then size bytes: NUL-separated bitmap resource names
        3. s16 cnt, then UV groups until the runtime loop ends:
           each group is  u16 numUV, numUV * (u8 u, u8 v).
           Quirk (CONFIRMED): cnt is the sum over groups of (numUV + 1);
           the runtime loop advances i += numUV + 1 per group.
        4. s16 frameCount, then per frame:
           s32 FrameTime (game ticks, 1024 Hz clock),
           s16 FrameID (index into bitmap list),
           s16 TexCoordsID (index into UV group list)

The same stream may exist without the FORM wrapper (bmpanim.cpp reads raw
streams too); ``parse_anm_bytes`` handles both.

Semantics (CONFIRMED from runtime use): VANM is *texture/material* animation.
Each frame selects a bitmap and a UV outline group for a duration.  It never
contains skeletal or vertex animation.  The play mode (0 = loop,
1 = ping-pong) is NOT stored here: it comes from the referencing .base file
(bmpanim.class FORM BANI STRC animType).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import struct

from iff_reader import read_iff_bytes


class AnmParseError(Exception):
    pass


@dataclass
class VanmFrame:
    frame_time: int      # duration in 1024 Hz game ticks
    frame_id: int        # bitmap index
    texcoords_id: int    # UV group index

    @property
    def duration_ms(self) -> float:
        return self.frame_time * 1000.0 / 1024.0


@dataclass
class VanmData:
    source_name: str = ""
    has_form: bool = False
    bitmap_class: str = ""
    bitmap_names: list[str] = field(default_factory=list)
    texcoord_groups: list[list[tuple[int, int]]] = field(default_factory=list)
    frames: list[VanmFrame] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def total_duration_ms(self) -> float:
        return sum(f.duration_ms for f in self.frames)

    def summary(self) -> str:
        return (
            f"class={self.bitmap_class or '?'} bitmaps={self.bitmap_names} "
            f"uv_groups={[len(g) for g in self.texcoord_groups]} "
            f"frames={len(self.frames)} (~{self.total_duration_ms:.0f} ms/cycle)"
        )


def _read_s16(data: bytes, pos: int) -> tuple[int, int]:
    if pos + 2 > len(data):
        raise AnmParseError(f"stream truncated at offset {pos}")
    return struct.unpack_from(">h", data, pos)[0], pos + 2


def _parse_stream(data: bytes, start: int, end: int, anm: VanmData) -> None:
    view = data[:end]
    pos = start

    size, pos = _read_s16(view, pos)
    if size < 0 or pos + size > end:
        raise AnmParseError("invalid class-name block size")
    anm.bitmap_class = view[pos:pos + size].split(b"\0")[0].decode("latin-1")
    pos += size

    size, pos = _read_s16(view, pos)
    if size < 0 or pos + size > end:
        raise AnmParseError("invalid bitmap-names block size")
    anm.bitmap_names = [
        n.decode("latin-1") for n in view[pos:pos + size].split(b"\0") if n
    ]
    pos += size

    cnt, pos = _read_s16(view, pos)
    consumed = 0
    while consumed < cnt:
        num_uv, pos = _read_s16(view, pos)
        if num_uv < 0 or pos + num_uv * 2 > end:
            raise AnmParseError("invalid UV group")
        anm.texcoord_groups.append(
            [(view[pos + 2 * j], view[pos + 2 * j + 1]) for j in range(num_uv)]
        )
        pos += num_uv * 2
        consumed += num_uv + 1  # runtime loop quirk (bmpAnm.cpp ReadTexCoords)

    frame_count, pos = _read_s16(view, pos)
    for _ in range(frame_count):
        if pos + 8 > end:
            raise AnmParseError("frame table truncated")
        frame_time, frame_id, texcoords_id = struct.unpack_from(">ihh", view, pos)
        pos += 8
        anm.frames.append(VanmFrame(frame_time, frame_id, texcoords_id))

    leftover = end - pos
    if leftover > 1:
        anm.warnings.append(f"{leftover} unused trailing byte(s) after frame table.")

    for frame in anm.frames:
        if not (0 <= frame.frame_id < max(1, len(anm.bitmap_names))):
            anm.warnings.append(
                f"Frame references bitmap #{frame.frame_id} but only "
                f"{len(anm.bitmap_names)} bitmap name(s) exist."
            )
        if not (0 <= frame.texcoords_id < max(1, len(anm.texcoord_groups))):
            anm.warnings.append(
                f"Frame references UV group #{frame.texcoords_id} but only "
                f"{len(anm.texcoord_groups)} group(s) exist."
            )


def parse_anm_bytes(data: bytes, source_name: str = "<memory>") -> VanmData:
    anm = VanmData(source_name=source_name)
    if len(data) < 4:
        anm.warnings.append("File too small.")
        return anm

    if data[:4] == b"FORM":
        tree = read_iff_bytes(data, source_name)
        anm.warnings.extend(tree.warnings)
        root = tree.roots[0] if tree.roots else None
        if root is None or not root.is_form("VANM"):
            anm.warnings.append(
                f"FORM type {(root.form_type if root else '?')!r} is not VANM."
            )
            return anm
        anm.has_form = True
        data_chunk = root.find_first("DATA")
        if data_chunk is None:
            anm.warnings.append("FORM VANM contains no DATA chunk.")
            return anm
        try:
            _parse_stream(data, data_chunk.payload_offset,
                          data_chunk.available_payload_end, anm)
        except AnmParseError as exc:
            anm.warnings.append(f"DATA stream parse failed: {exc}")
    else:
        # Raw stream without IFF wrapper (also accepted by the runtime).
        try:
            _parse_stream(data, 0, len(data), anm)
        except AnmParseError as exc:
            anm.warnings.append(f"Raw ANM stream parse failed: {exc}")
    return anm


def parse_anm_file(path: str | Path) -> VanmData:
    file_path = Path(path)
    try:
        data = file_path.read_bytes()
    except OSError as exc:
        raise AnmParseError(f"Could not open file: {exc}") from exc
    anm = parse_anm_bytes(data, file_path.name)
    return anm


if __name__ == "__main__":
    import argparse

    cli = argparse.ArgumentParser(description="Inspect a VANM/ANM file (read-only).")
    cli.add_argument("file")
    args = cli.parse_args()

    parsed = parse_anm_file(args.file)
    print(f"{parsed.source_name}: {parsed.summary()}")
    for i, frame in enumerate(parsed.frames):
        print(f"  frame {i}: bitmap#{frame.frame_id} uv#{frame.texcoords_id} "
              f"{frame.frame_time} ticks (~{frame.duration_ms:.1f} ms)")
    for i, group in enumerate(parsed.texcoord_groups):
        print(f"  uv group {i}: {group}")
    for warning in parsed.warnings:
        print(f"WARNING: {warning}")
