"""Typed decoder for ``particle.class`` PTCL/ATTS payloads.

The particle emitter itself does not reference an external file.  Its nested
``FORM OBJT`` life stages can, however, contain normal AREA/AMSH texture or
animation objects.  ``base_parser`` owns that recursive object parsing; this
module keeps the fixed-size emitter attributes separate from the BASE parser.
"""

from __future__ import annotations

from dataclasses import dataclass
import struct


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
