"""Animation formats: ANM / VANM UV animations and ``particle.class`` PTCL/ATTS payloads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import struct

from studio.formats.images import read_iff_bytes

# ----------------------------------------------------------------------------
# Parser and fixed-layout UV writer for Urban Assault .ANM / VANM animations.
# (formerly anm_parser.py)
# ----------------------------------------------------------------------------

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
    original_data: bytes = b""
    texcoord_offsets: list[int] = field(default_factory=list)

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
        anm.texcoord_offsets.append(pos)
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
    anm = VanmData(source_name=source_name, original_data=bytes(data))
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


def export_anm_bytes(anm: VanmData) -> bytes:
    """Patch same-length VANM UV groups and verify a complete round trip."""

    if not anm.original_data:
        raise AnmParseError("VANM source bytes are unavailable")
    if len(anm.texcoord_offsets) != len(anm.texcoord_groups):
        raise AnmParseError("VANM UV offsets are incomplete")
    output = bytearray(anm.original_data)
    for group_index, (offset, group) in enumerate(zip(
            anm.texcoord_offsets, anm.texcoord_groups)):
        if offset < 0 or offset + len(group) * 2 > len(output):
            raise AnmParseError(
                f"VANM UV group #{group_index} exceeds its source span")
        for point_index, uv in enumerate(group):
            if len(uv) != 2 or any(
                    not isinstance(value, int) or value < 0 or value > 255
                    for value in uv):
                raise AnmParseError(
                    f"VANM UV group #{group_index} has an invalid coordinate")
            output[offset + point_index * 2] = uv[0]
            output[offset + point_index * 2 + 1] = uv[1]
    verified = parse_anm_bytes(bytes(output), anm.source_name)
    if verified.bitmap_class != anm.bitmap_class \
            or verified.bitmap_names != anm.bitmap_names \
            or verified.texcoord_groups != anm.texcoord_groups \
            or [(frame.frame_time, frame.frame_id, frame.texcoords_id)
                for frame in verified.frames] != [
                    (frame.frame_time, frame.frame_id, frame.texcoords_id)
                    for frame in anm.frames]:
        raise AnmParseError("exported VANM failed round-trip verification")
    return bytes(output)


def parse_anm_file(path: str | Path) -> VanmData:
    file_path = Path(path)
    try:
        data = file_path.read_bytes()
    except OSError as exc:
        raise AnmParseError(f"Could not open file: {exc}") from exc
    anm = parse_anm_bytes(data, file_path.name)
    return anm


# ----------------------------------------------------------------------------
# Typed decoder for ``particle.class`` PTCL/ATTS payloads.
# (formerly particle_parser.py)
# ----------------------------------------------------------------------------

PARTICLE_ATTS_V1_SIZE = 94


@dataclass(frozen=True)
class ParticleAttributes:
    version: int
    accel_start: tuple[float, float, float]
    accel_end: tuple[float, float, float]
    magnify_start: tuple[float, float, float]
    magnify_end: tuple[float, float, float]
    collide: int
    start_speed: int
    num_contexts: int
    context_lifetime: int
    context_start_generation: int
    context_stop_generation: int
    birth_rate: int
    lifetime: int
    start_size: int
    end_size: int
    noise: int
    warnings: tuple[str, ...] = ()


def parse_particle_attributes(data: bytes, chunk) -> ParticleAttributes | None:
    """Decode the confirmed version-1 PTCL/ATTS record.

    Returns ``None`` for a truncated record.  The caller still preserves the
    original particle OBJT bytes, so an undecodable emitter remains lossless.
    """

    available = chunk.available_size
    if available < PARTICLE_ATTS_V1_SIZE:
        return None
    values = struct.unpack_from(
        ">h12f11i", data, chunk.payload_offset)
    version = values[0]
    vectors = values[1:13]
    integers = values[13:]
    warnings = []
    if version != 1:
        warnings.append(
            f"PTCL ATTS version {version} uses the version-1 layout "
            "provisionally.")
    if available > PARTICLE_ATTS_V1_SIZE:
        warnings.append(
            f"PTCL ATTS has {available - PARTICLE_ATTS_V1_SIZE} trailing "
            "byte(s), preserved unchanged.")
    return ParticleAttributes(
        version=version,
        accel_start=tuple(vectors[0:3]),
        accel_end=tuple(vectors[3:6]),
        magnify_start=tuple(vectors[6:9]),
        magnify_end=tuple(vectors[9:12]),
        collide=integers[0],
        start_speed=integers[1],
        num_contexts=integers[2],
        context_lifetime=integers[3],
        context_start_generation=integers[4],
        context_stop_generation=integers[5],
        birth_rate=integers[6],
        lifetime=integers[7],
        start_size=integers[8],
        end_size=integers[9],
        noise=integers[10],
        warnings=tuple(warnings),
    )
