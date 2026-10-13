"""BASE / BAS prefab files: read-only parser and BASE/KIDS scene export.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import shutil
import struct
from typing import Dict, Iterable, List, Optional, Tuple

from studio.formats.animation import parse_particle_attributes, ParticleAttributes
from studio.formats.images import IffChunk, IffTree, read_cstring, read_iff_bytes, read_iff_file

# ----------------------------------------------------------------------------
# Read-only parser for Urban Assault .base/.bas prefab files.
# (formerly base_parser.py)
# ----------------------------------------------------------------------------

CONFIRMED = "CONFIRMED"


STRONG_HYPOTHESIS = "STRONG HYPOTHESIS"


WEAK_HYPOTHESIS = "WEAK HYPOTHESIS"


UNKNOWN = "UNKNOWN"


class BaseParseError(Exception):
    pass


# --- decoded structures -----------------------------------------------------


@dataclass
class BaseTransform:
    """FORM BASE > STRC payload (62 bytes, CONFIRMED, base.cpp ReadIFFTagSTRC)."""

    version: int = 0
    position: tuple[float, float, float] = (0.0, 0.0, 0.0)
    saved_move: tuple[float, float, float] = (0.0, 0.0, 0.0)
    scale: tuple[float, float, float] = (1.0, 1.0, 1.0)
    euler: tuple[int, int, int] = (0, 0, 0)
    saved_rotation: tuple[int, int, int] = (0, 0, 0)
    flags: int = 0
    vis_limit: int = 4096
    ambient_light: int = 255


@dataclass
class AttsEntry:
    """One amesh ATTS record (6 bytes, CONFIRMED, amesh.cpp LoadingFromIFF)."""

    poly_id: int
    color_val: int
    shade_val: int
    tracy_val: int
    pad: int


@dataclass
class TextureRef:
    """Texture object found inside FORM AREA (ilbm.class or bmpanim.class)."""

    class_id: str = ""
    kind: str = ""          # "ilbm" | "bmpanim" | class_id fallback
    name: str = ""          # resource file name (NAM2 / BANI STRC name)
    outline_uvs: list[tuple[int, int]] = field(default_factory=list)  # CIBO OTL2
    anim_type: int | None = None  # BANI only: 0 loop, 1 ping-pong
    # Absolute c-string span in the source BASE.  The conservative texture
    # writer only replaces names that fit in this existing allocation.
    name_payload_offset: int = -1
    name_capacity: int = 0


@dataclass
class AmeshBlock:
    """One ADES entry: amesh.class material block (or area.class single poly)."""

    class_id: str = ""
    ade_flags: int = 0
    ade_point_id: int = 0
    ade_poly_id: int = 0
    area_flags: int = 0
    polflags: int = 0
    color_val: int = 0
    tracy_val: int = 0
    shade_val: int = 0
    texture: TextureRef | None = None
    tracy_texture: TextureRef | None = None
    atts: list[AttsEntry] = field(default_factory=list)
    olpl: list[list[tuple[int, int]]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # File offsets of this block's ATTS/OLPL chunks (headers), -1 when absent.
    # Used by the safe mapping editor to append entries in place.
    atts_chunk_offset: int = -1
    atts_chunk_size: int = 0
    olpl_chunk_offset: int = -1
    olpl_chunk_size: int = 0
    # Structural source metadata for typed ADES grow/shrink serialization.
    payload_form_type: str = ""
    source_ades_index: int = -1
    source_objt_offset: int = -1
    source_objt_size: int = 0
    source_objt_bytes: bytes = field(default=b"", repr=False, compare=False)
    ade_strc_chunk_offset: int = -1
    ade_strc_chunk_size: int = 0
    # particle.class PTCL payload.  Particle life stages are nested ADE OBJTs,
    # not standalone external dependencies.
    particle_attributes: ParticleAttributes | None = None
    particle_stages: list["AmeshBlock"] = field(default_factory=list)

    @property
    def uses_atts_only_mapping(self) -> bool:
        """Untextured and animated AMESH may legitimately omit fixed UVs."""
        return bool(
            (self.class_id or "").casefold() == "amesh.class" and not self.olpl
            and (self.texture is None or self.texture.kind == "bmpanim"))

    # AREA_POL_FLAG_* bits (CONFIRMED, area.h)
    @property
    def map_mode(self) -> str:
        bits = self.polflags & 0x6
        return {0x0: "none", 0x2: "linear", 0x6: "depth"}.get(bits, "linear?")

    @property
    def textured(self) -> bool:
        return bool(self.polflags & 0x8)

    @property
    def shade_mode(self) -> str:
        bits = self.polflags & 0x30
        return {0x0: "none", 0x10: "flat", 0x20: "line", 0x30: "gradient"}[bits]

    @property
    def tracy_mode(self) -> str:
        bits = self.polflags & 0xC0
        return {0x0: "none", 0x40: "clear", 0x80: "flat", 0xC0: "mapped"}[bits]

    @property
    def tracy_light(self) -> bool:
        return bool(self.polflags & 0x100)

    @property
    def depth_fade(self) -> bool:
        return bool(self.area_flags & 0x1)  # AREA_FLAG_DPTHFADE

    def describe_polflags(self) -> str:
        parts = [f"map={self.map_mode}"]
        parts.append("textured" if self.textured else "untextured")
        parts.append(f"shade={self.shade_mode}")
        parts.append(f"tracy={self.tracy_mode}")
        if self.tracy_light:
            parts.append("tracy-light")
        if self.depth_fade:
            parts.append("depth-fade")
        return ", ".join(parts)

    def iter_visual_blocks(self):
        """Yield this ADE and recursively decoded particle life stages."""

        yield self
        for stage in self.particle_stages:
            yield from stage.iter_visual_blocks()


@dataclass
class EmbeddedResource:
    """One EMRS record inside FORM EMBD (CONFIRMED, embed.cpp)."""

    class_id: str = ""
    resource_name: str = ""
    payload_tag: str = ""
    payload_form_type: str = ""
    payload_offset: int = 0
    payload_size: int = 0  # full payload chunk bytes, including header/pad


@dataclass
class BaseObject:
    """One base.class object (root or KIDS child)."""

    name: str = ""
    transform: BaseTransform | None = None
    skeleton_class: str = ""
    skeleton_name: str = ""
    ades: list[AmeshBlock] = field(default_factory=list)
    kids: list["BaseObject"] = field(default_factory=list)
    embedded: list[EmbeddedResource] = field(default_factory=list)
    unknown_chunks: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Source FORM OBJT location, used to export one KIDS object as a standalone
    # loose BASE without serializing or rewriting unrelated archive objects.
    source_objt_offset: int = -1
    source_objt_size: int = 0
    ades_form_offset: int = -1
    ades_form_size: int = 0

    def iter_tree(self):
        yield self
        for kid in self.kids:
            yield from kid.iter_tree()


@dataclass
class BaseAsset:
    """Parsed .base file."""

    source_path: str = ""
    tree: IffTree | None = None
    root: BaseObject | None = None
    warnings: list[str] = field(default_factory=list)

    def all_objects(self) -> list[BaseObject]:
        return list(self.root.iter_tree()) if self.root else []


    def referenced_textures(self) -> list[str]:
        names = []
        for obj in self.all_objects():
            for root_block in obj.ades:
                for block in root_block.iter_visual_blocks():
                    for tex in (block.texture, block.tracy_texture):
                        if tex and tex.name and tex.name not in names:
                            names.append(tex.name)
        return names

    def referenced_animations(self) -> list[str]:
        names = []
        for obj in self.all_objects():
            for root_block in obj.ades:
                for block in root_block.iter_visual_blocks():
                    for tex in (block.texture, block.tracy_texture):
                        if tex and tex.kind == "bmpanim" and tex.name \
                                and tex.name not in names:
                            names.append(tex.name)
        return names


# --- low-level chunk decoding ------------------------------------------------


def _read_clid(data: bytes, objt: IffChunk) -> str:
    for child in objt.children:
        if child.tag == "CLID":
            text, _ = read_cstring(
                data, child.payload_offset, child.available_payload_end
            )
            return text
    return ""


def _payload_form(objt: IffChunk) -> IffChunk | None:
    for child in objt.children:
        if child.tag == "FORM":
            return child
    return None


def _read_root_name(data: bytes, form: IffChunk) -> str:
    root = None
    for child in form.children:
        if child.is_form("ROOT"):
            root = child
            break
    if root is None:
        return ""
    for child in root.children:
        if child.tag == "NAME":
            text, _ = read_cstring(
                data, child.payload_offset, child.available_payload_end
            )
            return text
    return ""


def _parse_base_strc(data: bytes, chunk: IffChunk, warnings: list[str]) -> BaseTransform:
    if chunk.available_size < 62:
        warnings.append(
            f"BASE STRC at 0x{chunk.offset:X} has {chunk.available_size} "
            "available bytes; expected 62."
        )
        return BaseTransform()
    p = chunk.payload_offset
    tf = BaseTransform()
    tf.version = struct.unpack_from(">h", data, p)[0]
    tf.position = struct.unpack_from(">fff", data, p + 2)
    tf.saved_move = struct.unpack_from(">fff", data, p + 14)
    tf.scale = struct.unpack_from(">fff", data, p + 26)
    tf.euler = struct.unpack_from(">hhh", data, p + 38)
    tf.saved_rotation = struct.unpack_from(">hhh", data, p + 44)
    tf.flags = struct.unpack_from(">h", data, p + 50)[0]
    tf.vis_limit, tf.ambient_light = struct.unpack_from(">ii", data, p + 54)
    return tf


def _parse_cibo(data: bytes, form: IffChunk, class_id: str) -> TextureRef:
    tex = TextureRef(class_id=class_id, kind="ilbm")
    for child in form.children:
        if child.tag == "NAM2":
            tex.name, _ = read_cstring(
                data, child.payload_offset, child.available_payload_end
            )
            tex.name_payload_offset = child.payload_offset
            tex.name_capacity = child.available_size
        elif child.tag == "OTL2":
            payload = child.payload(data)
            usable = len(payload) - (len(payload) % 2)
            tex.outline_uvs = [
                (payload[i], payload[i + 1]) for i in range(0, usable, 2)
            ]
    return tex


def _parse_bani(data: bytes, form: IffChunk, class_id: str,
                warnings: list[str]) -> TextureRef:
    tex = TextureRef(class_id=class_id, kind="bmpanim")
    for child in form.children:
        if child.tag == "STRC":
            p = child.payload_offset
            if child.available_size < 7:
                warnings.append(
                    f"BANI STRC at 0x{child.offset:X} is too small "
                    f"({child.available_size} available bytes)."
                )
                continue
            version, name_offset, anim_type = struct.unpack_from(">hhh", data, p)
            tex.anim_type = anim_type
            # Runtime: name starts at buf[nameOffset - 6] where buf follows the
            # three int16 fields (bmpAnm.cpp LoadingFromIFF, CONFIRMED).
            name_start = p + 6 + max(0, name_offset - 6)
            if version >= 1 and name_start < child.available_payload_end:
                tex.name, _ = read_cstring(
                    data, name_start, child.available_payload_end
                )
                tex.name_payload_offset = name_start
                tex.name_capacity = child.available_payload_end - name_start
    return tex


def _parse_texture_objt(data: bytes, objt: IffChunk,
                        warnings: list[str]) -> TextureRef | None:
    class_id = _read_clid(data, objt)
    form = _payload_form(objt)
    if form is None:
        warnings.append(f"Texture OBJT at 0x{objt.offset:X} has no payload FORM.")
        return None
    if form.form_type == "CIBO":
        return _parse_cibo(data, form, class_id)
    if form.form_type == "BANI":
        return _parse_bani(data, form, class_id, warnings)
    warnings.append(
        f"Texture OBJT class {class_id!r} uses unhandled FORM "
        f"{form.form_type!r} at 0x{form.offset:X}."
    )
    return TextureRef(class_id=class_id, kind=class_id or "unknown")


def _parse_ade(data: bytes, form: IffChunk, block: AmeshBlock) -> None:
    """Decode the common ADE/STRC reference embedded by AREA and PTCL."""

    for child in form.children:
        if child.tag != "STRC":
            continue
        if child.available_size < 10:
            block.warnings.append(
                f"ADE STRC at 0x{child.offset:X} has "
                f"{child.available_size} available bytes; expected 10.")
            continue
        block.ade_strc_chunk_offset = child.offset
        block.ade_strc_chunk_size = child.size
        p = child.payload_offset
        version, _nu, flags, point, poly, _nu2 = struct.unpack_from(
            ">hbbhhh", data, p)
        if version >= 1:
            block.ade_flags = flags
            block.ade_point_id = point
            block.ade_poly_id = poly


def _parse_area(data: bytes, area: IffChunk, block: AmeshBlock) -> None:
    for child in area.children:
        if child.is_form("ADE"):
            _parse_ade(data, child, block)
        elif child.tag == "STRC":
            if child.available_size < 10:
                block.warnings.append(
                    f"AREA STRC at 0x{child.offset:X} has "
                    f"{child.available_size} available bytes; expected 10."
                )
                continue
            p = child.payload_offset
            version, flags, polflags, _un, clr, trc, shd = struct.unpack_from(
                ">hHHBBBB", data, p
            )
            if version >= 1:
                block.area_flags = flags
                block.polflags = polflags
                block.color_val = clr
                block.tracy_val = trc
                block.shade_val = shd
        elif child.is_form("OBJT"):
            tex = _parse_texture_objt(data, child, block.warnings)
            if tex is None:
                continue
            # Runtime assignment (area.cpp area_func5__sub1, CONFIRMED):
            # TEXBIT -> diffuse texture; TRACYMAPPED -> transparency map;
            # both -> first OBJT is diffuse, second is the tracy map.
            textured = bool(block.polflags & 0x8)
            tracy_mapped = (block.polflags & 0xC0) == 0xC0
            if textured and (not tracy_mapped or block.texture is None):
                block.texture = tex
            elif tracy_mapped:
                block.tracy_texture = tex
            else:
                block.texture = tex


def _parse_atts(data: bytes, chunk: IffChunk, block: AmeshBlock) -> None:
    block.atts_chunk_offset = chunk.offset
    block.atts_chunk_size = chunk.size
    count = chunk.available_size // 6
    if chunk.available_size % 6:
        block.warnings.append(
            f"ATTS available size {chunk.available_size} is not a multiple of 6; "
            "trailing bytes ignored."
        )
    p = chunk.payload_offset
    for i in range(count):
        poly_id, color, shade, tracy, pad = struct.unpack_from(">hBBBB", data, p + i * 6)
        block.atts.append(AttsEntry(poly_id, color, shade, tracy, pad))


def _parse_olpl(data: bytes, chunk: IffChunk, block: AmeshBlock) -> None:
    block.olpl_chunk_offset = chunk.offset
    block.olpl_chunk_size = chunk.size
    p = chunk.payload_offset
    end = chunk.available_payload_end
    expected = len(block.atts)
    while p + 2 <= end and (expected == 0 or len(block.olpl) < expected):
        count = struct.unpack_from(">h", data, p)[0]
        p += 2
        if count < 0 or p + count * 2 > end:
            block.warnings.append(
                f"OLPL group {len(block.olpl)} is truncated or has an invalid "
                f"count ({count}); remaining data skipped."
            )
            return
        block.olpl.append(
            [(data[p + 2 * i], data[p + 2 * i + 1]) for i in range(count)]
        )
        p += count * 2
    leftover = end - p
    if leftover > 1:
        block.warnings.append(f"OLPL has {leftover} unused trailing byte(s).")


def _parse_amesh(data: bytes, form: IffChunk, class_id: str) -> AmeshBlock:
    block = AmeshBlock(class_id=class_id)
    for child in form.children:
        if child.is_form("AREA"):
            _parse_area(data, child, block)
        elif child.tag == "ATTS":
            _parse_atts(data, child, block)
        elif child.tag == "OLPL":
            _parse_olpl(data, child, block)
        else:
            block.warnings.append(
                f"Unhandled chunk {child.display_name} inside AMSH at 0x{child.offset:X}."
            )
    if block.olpl and len(block.olpl) != len(block.atts):
        block.warnings.append(
            f"OLPL group count ({len(block.olpl)}) differs from ATTS entry "
            f"count ({len(block.atts)})."
        )
    return block


def _parse_area_only(data: bytes, form: IffChunk, class_id: str) -> AmeshBlock:
    """area.class ADES entry: FORM AREA without AMSH wrapper (one polygon)."""

    block = AmeshBlock(class_id=class_id)
    _parse_area(data, form, block)
    # area.class renders exactly one polygon: _polyID from the ADE STRC, with
    # UVs taken from the texture's OTL2 outline (area.cpp GenMesh, CONFIRMED).
    block.atts.append(
        AttsEntry(block.ade_poly_id, block.color_val, block.shade_val,
                  block.tracy_val, 0)
    )
    if block.texture and block.texture.outline_uvs:
        block.olpl.append(list(block.texture.outline_uvs))
    return block


def _parse_particle(data: bytes, form: IffChunk,
                    class_id: str) -> AmeshBlock:
    """Decode a particle emitter and all nested visual life-stage ADEs."""

    block = AmeshBlock(class_id=class_id)
    for child in form.children:
        if child.is_form("ADE"):
            _parse_ade(data, child, block)
        elif child.tag == "ATTS":
            block.atts_chunk_offset = child.offset
            block.atts_chunk_size = child.size
            block.particle_attributes = parse_particle_attributes(data, child)
            if block.particle_attributes is None:
                block.warnings.append(
                    f"PTCL ATTS at 0x{child.offset:X} has "
                    f"{child.available_size} available bytes; expected at "
                    "least 94.")
            else:
                block.warnings.extend(block.particle_attributes.warnings)
        elif child.is_form("OBJT"):
            block.particle_stages.append(_parse_ades_objt(
                data, child, len(block.particle_stages)))
        else:
            block.warnings.append(
                f"Unhandled chunk {child.display_name} inside PTCL at "
                f"0x{child.offset:X}.")
    if len(block.particle_stages) > 8:
        block.warnings.append(
            f"PTCL contains {len(block.particle_stages)} life stages; the "
            "confirmed runtime limit is 8.")
    return block


def _parse_ades_objt(data: bytes, child: IffChunk,
                     source_index: int) -> AmeshBlock:
    """Parse one ADES-compatible OBJT, including nested particle stages."""

    class_id = _read_clid(data, child)
    payload = _payload_form(child)
    if payload is None:
        block = AmeshBlock(class_id=class_id)
        block.warnings.append(
            f"ADES OBJT at 0x{child.offset:X} ({class_id!r}) has no "
            "payload FORM.")
    elif payload.form_type == "AMSH":
        block = _parse_amesh(data, payload, class_id)
    elif payload.form_type == "AREA":
        block = _parse_area_only(data, payload, class_id)
    elif payload.form_type == "PTCL":
        block = _parse_particle(data, payload, class_id)
    else:
        block = AmeshBlock(class_id=class_id)
        block.warnings.append(
            f"ADES entry with unhandled payload FORM "
            f"{payload.form_type!r} (class {class_id!r}) at "
            f"0x{payload.offset:X}.")
    block.payload_form_type = payload.form_type if payload is not None else ""
    block.source_ades_index = source_index
    block.source_objt_offset = child.offset
    block.source_objt_size = child.size
    end = child.offset + 8 + child.size + (child.size & 1)
    block.source_objt_bytes = bytes(data[child.offset:end])
    return block


def _parse_embed(data: bytes, form: IffChunk, obj: BaseObject) -> None:
    for child_index, child in enumerate(form.children):
        if child.tag != "EMRS":
            continue
        class_id, pos = read_cstring(
            data, child.payload_offset, child.available_payload_end
        )
        res_name, pos = read_cstring(data, pos, child.available_payload_end)
        record = EmbeddedResource(class_id=class_id, resource_name=res_name)
        # Payload is either inline after the strings or the next sibling chunk.
        while pos < child.available_payload_end and data[pos] == 0:
            pos += 1
        if pos + 8 <= child.available_payload_end:
            record.payload_tag = data[pos:pos + 4].decode("latin-1", "replace")
            declared = struct.unpack_from(">I", data, pos + 4)[0]
            record.payload_size = 8 + declared + (declared & 1)
            record.payload_offset = pos
            if record.payload_tag == "FORM":
                record.payload_form_type = data[pos + 8:pos + 12].decode(
                    "latin-1", "replace"
                )
        elif child_index + 1 < len(form.children):
            # embed.cpp stores the normal payload as the EMRS record's next
            # sibling.  It is not part of the EMRS chunk itself.
            payload = form.children[child_index + 1]
            if payload.tag != "EMRS":
                record.payload_tag = payload.tag
                record.payload_form_type = payload.form_type
                record.payload_offset = payload.offset
                record.payload_size = 8 + payload.size + (payload.size & 1)
        obj.embedded.append(record)


def _parse_ades(data: bytes, form: IffChunk, obj: BaseObject) -> None:
    obj.ades_form_offset = form.offset
    obj.ades_form_size = form.size
    for child in form.children:
        if not child.is_form("OBJT"):
            continue
        obj.ades.append(_parse_ades_objt(data, child, len(obj.ades)))


def _parse_base_object(data: bytes, objt: IffChunk, warnings: list[str]) -> BaseObject:
    obj = BaseObject()
    obj.source_objt_offset = objt.offset
    obj.source_objt_size = objt.size
    class_id = _read_clid(data, objt)
    if class_id and class_id.lower() != "base.class":
        obj.warnings.append(f"OBJT declares class {class_id!r}; expected base.class.")

    base_form = _payload_form(objt)
    if base_form is None or base_form.form_type != "BASE":
        obj.warnings.append("OBJT has no FORM BASE payload.")
        return obj

    obj.name = _read_root_name(data, base_form)
    strc_seen = False

    for child in base_form.children:
        if child.is_form("ROOT"):
            continue  # name already read
        if child.tag == "STRC":
            obj.transform = _parse_base_strc(data, child, obj.warnings)
            strc_seen = True
        elif child.is_form("OBJT"):
            # Runtime rule (base.cpp LoadingFromIFF, CONFIRMED): an OBJT before
            # STRC is the embedded-resources object, after STRC it is the
            # skeleton object.
            inner_class = _read_clid(data, child)
            inner_form = _payload_form(child)
            if not strc_seen:
                if inner_form is not None and inner_form.form_type == "EMBD":
                    _parse_embed(data, inner_form, obj)
                else:
                    obj.warnings.append(
                        f"Pre-STRC OBJT (class {inner_class!r}) with unexpected "
                        f"payload at 0x{child.offset:X}."
                    )
            else:
                obj.skeleton_class = inner_class
                if inner_form is not None and inner_form.form_type == "SKLC":
                    for sub in inner_form.children:
                        if sub.tag == "NAME":
                            obj.skeleton_name, _ = read_cstring(
                                data, sub.payload_offset,
                                sub.available_payload_end
                            )
                else:
                    obj.warnings.append(
                        f"Skeleton OBJT (class {inner_class!r}) without FORM SKLC "
                        f"at 0x{child.offset:X}."
                    )
        elif child.is_form("ADES"):
            _parse_ades(data, child, obj)
        elif child.is_form("KIDS"):
            for kid_objt in child.children:
                if kid_objt.is_form("OBJT"):
                    obj.kids.append(_parse_base_object(data, kid_objt, warnings))
        else:
            obj.unknown_chunks.append(
                f"{child.display_name} at 0x{child.offset:X} ({child.size} bytes)"
            )
    return obj


# --- entry points -------------------------------------------------------------


def parse_base_bytes(data: bytes, source_name: str = "<memory>") -> BaseAsset:
    asset = BaseAsset(source_path=source_name)
    asset.tree = read_iff_bytes(data, source_name)
    asset.warnings.extend(asset.tree.warnings)

    mc2 = asset.tree.find_first("FORM", "MC2")
    if mc2 is None:
        asset.warnings.append("No FORM MC2 container found; not a .base file?")
        return asset

    root_objt = None
    for child in mc2.children:
        if child.is_form("OBJT"):
            root_objt = child
            break
    if root_objt is None:
        asset.warnings.append("FORM MC2 contains no FORM OBJT.")
        return asset

    asset.root = _parse_base_object(data, root_objt, asset.warnings)
    for obj in asset.root.iter_tree():
        asset.warnings.extend(obj.warnings)
        for block in obj.ades:
            asset.warnings.extend(block.warnings)
    return asset


def parse_base_file(path: str | Path) -> BaseAsset:
    file_path = Path(path)
    tree = read_iff_file(file_path)
    asset = parse_base_bytes(tree.data, file_path.name)
    asset.source_path = str(file_path)
    return asset


# ----------------------------------------------------------------------------
# Export BASE/KIDS scene metadata from a local Urban Assault/OpenNeoUA SET.BAS.
# (formerly base_kids_export.py)
# ----------------------------------------------------------------------------

ASCII_TAG = re.compile(rb"^[\x20-\x7e]{4}$")


TEXT_TAGS = {"CLID", "NAME", "NAM2"}


STRUCTURAL_FORM_TYPES = {
    "OBJT", "ROOT", "ADE ", "AREA", "BANI", "CIBO", "AMSH",
    "BASE", "SKLC", "ADES", "PTCL", "KIDS",
}


STRUCTURAL_LEAF_TAGS = {"STRC", "ATTS", "OLPL", "OTL2", "POL2", "POO2", "SEN2"}


RAW_LEAF_TAGS = {"CLID", "NAME", "NAM2", "STRC", "ATTS", "OLPL", "OTL2"}


ASSET_REF_RE = re.compile(r"\.(ilbm|ilb|sklt|skl|anm|vanm)$", re.IGNORECASE)


class ExportError(Exception):
    pass


@dataclass
class Chunk:
    tag: str
    offset: int
    size: int
    data_start: int
    data_end: int
    padded_end: int
    depth: int
    path: str
    form_type: Optional[str] = None


@dataclass
class ObjectNode:
    node_id: str
    parent_id: Optional[str]
    chunk_path: str
    offset: int
    size: int
    form_type: str
    class_id: str = ""
    name: str = ""
    nam2: str = ""
    names: List[str] = field(default_factory=list)
    nam2_values: List[str] = field(default_factory=list)
    child_ids: List[str] = field(default_factory=list)
    structural_chunks: List[Dict[str, object]] = field(default_factory=list)


def read_u32be(data: bytes, offset: int) -> int:
    return struct.unpack_from(">I", data, offset)[0]


def decode_tag(raw: bytes) -> str:
    if len(raw) != 4:
        return "????"
    if ASCII_TAG.match(raw):
        return raw.decode("ascii")
    return "".join(chr(b) if 32 <= b <= 126 else "." for b in raw)


def hex_preview(data: bytes, start: int, end: int, limit: int = 16) -> str:
    return " ".join(f"{b:02X}" for b in data[start:min(end, start + limit)])


def chunk_bytes(data: bytes, chunk: Chunk) -> bytes:
    return data[chunk.offset:chunk.data_end]


def read_c_string(data: bytes, start: int, end: int, limit: int = 256) -> str:
    capped_end = min(end, start + limit)
    nul = data.find(b"\0", start, capped_end)
    if nul >= 0:
        capped_end = nul
    raw = data[start:capped_end]
    return raw.decode("latin-1", errors="replace").strip("\0")


def parse_chunk_at(data: bytes, offset: int, container_end: int, depth: int, path: str) -> Chunk:
    if offset + 8 > container_end:
        raise ExportError(f"truncated chunk header at 0x{offset:X}")

    tag = decode_tag(data[offset:offset + 4])
    size = read_u32be(data, offset + 4)
    data_start = offset + 8
    data_end = data_start + size
    padded_end = data_end + (size & 1)
    if data_end > container_end:
        raise ExportError(f"chunk {tag} at 0x{offset:X} extends past container end")
    if padded_end > len(data):
        raise ExportError(f"chunk {tag} at 0x{offset:X} padding extends past file end")

    form_type = None
    if tag == "FORM":
        if size < 4:
            raise ExportError(f"FORM at 0x{offset:X} is too small")
        form_type = decode_tag(data[data_start:data_start + 4])

    return Chunk(tag, offset, size, data_start, data_end, padded_end, depth, path, form_type)


def iter_chunks(data: bytes, start: int, end: int, depth: int, path: str) -> Iterable[Chunk]:
    offset = start
    index = 0
    while offset < end:
        if offset + 8 > end:
            raise ExportError(f"trailing bytes before container end at 0x{offset:X}")
        chunk = parse_chunk_at(data, offset, end, depth, f"{path}/{index}")
        yield chunk
        offset = chunk.padded_end
        index += 1


def load_setbas(path: Path) -> Tuple[bytes, Chunk]:
    data = path.read_bytes()
    if len(data) < 12:
        raise ExportError("file is too small to be an IFF FORM")
    root = parse_chunk_at(data, 0, len(data), 0, "root")
    if root.tag != "FORM":
        raise ExportError("SET.BAS root is not an IFF FORM")
    return data, root


def read_emrs_names(data: bytes, root: Chunk) -> Dict[str, List[str]]:
    names_by_class: Dict[str, List[str]] = defaultdict(list)

    def walk(start: int, end: int, depth: int, path: str) -> None:
        for chunk in iter_chunks(data, start, end, depth, path):
            if chunk.tag == "EMRS":
                class_name = read_c_string(data, chunk.data_start, chunk.data_end, 256)
                pos = chunk.data_start + len(class_name.encode("latin-1", errors="replace")) + 1
                resource = read_c_string(data, pos, chunk.data_end, 256) if pos < chunk.data_end else ""
                if class_name and resource:
                    names_by_class[class_name].append(resource)
            if chunk.tag == "FORM":
                walk(chunk.data_start + 4, chunk.data_end, depth + 1, chunk.path)

    walk(root.data_start + 4, root.data_end, 1, "root")
    return dict(names_by_class)


def classify_reference(text: str) -> Optional[str]:
    lowered = text.lower()
    if lowered.endswith((".ilbm", ".ilb")):
        return "texture"
    if lowered.endswith((".sklt", ".skl")) or "skeleton/" in lowered:
        return "skeleton"
    if lowered.endswith((".anm", ".vanm")):
        return "animation"
    return None


def asset_like(text: str) -> bool:
    return bool(ASSET_REF_RE.search(text)) or "Skeleton/" in text or "skeleton/" in text


def write_binary(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")


class BaseKidsExporter:
    def __init__(self, data: bytes, root: Chunk) -> None:
        self.data = data
        self.root = root
        self.nodes: List[ObjectNode] = []
        self.kids_sections: List[Dict[str, object]] = []
        self.class_counts: Counter = Counter()
        self.name_counts: Counter = Counter()
        self.nam2_counts: Counter = Counter()
        self.texture_refs: Counter = Counter()
        self.skeleton_refs: Counter = Counter()
        self.animation_refs: Counter = Counter()
        self.references_by_node: Dict[str, Dict[str, List[str]]] = {}
        self.node_counter = 0

    def next_node_id(self) -> str:
        self.node_counter += 1
        return f"node_{self.node_counter:05d}"

    def export(self) -> Dict[str, object]:
        self.walk(self.root.data_start + 4, self.root.data_end, 1, "root", None, None)
        return {
            "root_form_type": self.root.form_type or "",
            "kids_sections": self.kids_sections,
            "nodes": [self.node_to_dict(node) for node in self.nodes],
        }

    def walk(
        self,
        start: int,
        end: int,
        depth: int,
        path: str,
        current_node: Optional[ObjectNode],
        parent_node_id: Optional[str],
    ) -> None:
        for chunk in iter_chunks(self.data, start, end, depth, path):
            if chunk.tag == "FORM" and chunk.form_type == "KIDS":
                self.kids_sections.append({
                    "offset": chunk.offset,
                    "size": chunk.size,
                    "path": chunk.path,
                    "parent_node_id": current_node.node_id if current_node else None,
                })
                self.walk(chunk.data_start + 4, chunk.data_end, depth + 1, chunk.path, current_node, current_node.node_id if current_node else parent_node_id)
                continue

            if chunk.tag == "FORM" and chunk.form_type == "OBJT":
                node = ObjectNode(
                    node_id=self.next_node_id(),
                    parent_id=parent_node_id,
                    chunk_path=chunk.path,
                    offset=chunk.offset,
                    size=chunk.size,
                    form_type=chunk.form_type or "",
                )
                self.nodes.append(node)
                if parent_node_id:
                    parent = self.find_node(parent_node_id)
                    if parent is not None:
                        parent.child_ids.append(node.node_id)
                self.walk(chunk.data_start + 4, chunk.data_end, depth + 1, chunk.path, node, node.node_id)
                self.finalize_node(node)
                continue

            if current_node is not None:
                self.capture_chunk(current_node, chunk)

            if chunk.tag == "FORM":
                self.walk(chunk.data_start + 4, chunk.data_end, depth + 1, chunk.path, current_node, parent_node_id)

    def find_node(self, node_id: str) -> Optional[ObjectNode]:
        for node in self.nodes:
            if node.node_id == node_id:
                return node
        return None

    def capture_chunk(self, node: ObjectNode, chunk: Chunk) -> None:
        if chunk.tag in TEXT_TAGS:
            text = read_c_string(self.data, chunk.data_start, chunk.data_end)
            if chunk.tag == "CLID":
                node.class_id = node.class_id or text
                self.class_counts[text] += 1
            elif chunk.tag == "NAME":
                node.names.append(text)
                self.name_counts[text] += 1
            elif chunk.tag == "NAM2":
                node.nam2_values.append(text)
                self.nam2_counts[text] += 1
            self.capture_reference(node, text)
            return

        if chunk.tag == "FORM":
            label = f"FORM {chunk.form_type}" if chunk.form_type else "FORM"
            if chunk.form_type in STRUCTURAL_FORM_TYPES:
                node.structural_chunks.append(self.chunk_info(chunk, label))
            return

        if chunk.tag in STRUCTURAL_LEAF_TAGS or chunk.size:
            node.structural_chunks.append(self.chunk_info(chunk, chunk.tag))

    def capture_reference(self, node: ObjectNode, text: str) -> None:
        ref_type = classify_reference(text)
        if ref_type is None:
            return

        by_node = self.references_by_node.setdefault(node.node_id, {"texture": [], "skeleton": [], "animation": []})
        if text not in by_node[ref_type]:
            by_node[ref_type].append(text)

        if ref_type == "texture":
            self.texture_refs[text] += 1
        elif ref_type == "skeleton":
            self.skeleton_refs[text] += 1
        elif ref_type == "animation":
            self.animation_refs[text] += 1

    def chunk_info(self, chunk: Chunk, label: str) -> Dict[str, object]:
        return {
            "label": label,
            "tag": chunk.tag,
            "form_type": chunk.form_type or "",
            "offset": chunk.offset,
            "size": chunk.size,
            "parent_path": chunk.path.rsplit("/", 1)[0] if "/" in chunk.path else "",
            "hex_preview": hex_preview(self.data, chunk.data_start, chunk.data_end),
        }

    def finalize_node(self, node: ObjectNode) -> None:
        if node.names:
            node.name = node.names[0]
        if node.nam2_values:
            node.nam2 = node.nam2_values[0]

    def node_to_dict(self, node: ObjectNode) -> Dict[str, object]:
        return {
            "id": node.node_id,
            "parent_id": node.parent_id,
            "chunk_path": node.chunk_path,
            "offset": node.offset,
            "size": node.size,
            "form_type": node.form_type,
            "class_id": node.class_id,
            "NAME": node.name,
            "NAM2": node.nam2,
            "names": node.names,
            "nam2_values": node.nam2_values,
            "child_ids": node.child_ids,
            "structural_chunks": node.structural_chunks,
        }

    def references(self) -> Dict[str, object]:
        return {
            "textures": dict(sorted(self.texture_refs.items())),
            "skeletons": dict(sorted(self.skeleton_refs.items())),
            "animations": dict(sorted(self.animation_refs.items())),
            "class_counts": dict(sorted(self.class_counts.items())),
            "name_counts": dict(sorted(self.name_counts.items())),
            "nam2_counts": dict(sorted(self.nam2_counts.items())),
            "references_by_node_id": self.references_by_node,
        }


class RawBaseKidsExporter:
    def __init__(
        self,
        data: bytes,
        root: Chunk,
        out_dir: Path,
        export_leaf_chunks: bool = True,
        write_manifest: bool = True,
    ) -> None:
        self.data = data
        self.root = root
        self.out_dir = out_dir
        self.export_leaf_chunks = export_leaf_chunks
        self.write_manifest = write_manifest
        self.entries: List[Dict[str, object]] = []
        self.kids_count = 0
        self.objt_count = 0
        self.node_count = 0
        self.leaf_counts: Counter = Counter()
        self.text_leaf_count = 0
        self.binary_leaf_count = 0
        self.warnings: List[str] = []

    def export(self) -> Dict[str, object]:
        self.prepare_output_dir()
        self.walk(self.root.data_start + 4, self.root.data_end, 1, "root", None, None, False)

        manifest = {
            "description": "Raw BASE/KIDS structural data extracted from SET.BAS",
            "leaf_chunk_files_enabled": self.export_leaf_chunks,
            "kids_forms_exported": self.kids_count,
            "objt_forms_exported": self.objt_count,
            "leaf_chunks_exported": dict(sorted(self.leaf_counts.items())),
            "text_leaf_chunks_exported": self.text_leaf_count,
            "binary_leaf_chunks_exported": self.binary_leaf_count,
            "warnings": self.warnings,
            "entries": self.entries,
        }
        manifest_path = self.out_dir / "base_kids_raw_manifest.json"
        if self.write_manifest:
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        return {
            "raw_manifest_path": manifest_path if self.write_manifest else None,
            "kids_forms_exported": self.kids_count,
            "objt_forms_exported": self.objt_count,
            "leaf_chunks_exported": dict(sorted(self.leaf_counts.items())),
            "text_leaf_chunks_exported": self.text_leaf_count,
            "binary_leaf_chunks_exported": self.binary_leaf_count,
            "warning_count": len(self.warnings),
            "leaf_chunk_files_enabled": self.export_leaf_chunks,
        }

    def prepare_output_dir(self) -> None:
        """Clear BASet-generated BASE_KIDS raw output before writing a fresh export."""
        self.out_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = self.out_dir / "base_kids_raw_manifest.json"
        if manifest_path.exists():
            manifest_path.unlink()
        chunks_dir = self.out_dir / "chunks"
        if chunks_dir.exists():
            shutil.rmtree(chunks_dir)
        for child in self.out_dir.glob("kids_*"):
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()

    def walk(
        self,
        start: int,
        end: int,
        depth: int,
        path: str,
        current_kids_dir: Optional[Path],
        current_node_id: Optional[str],
        parent_is_kids: bool,
    ) -> None:
        for chunk in iter_chunks(self.data, start, end, depth, path):
            if chunk.tag == "FORM" and chunk.form_type == "KIDS":
                kids_index = self.kids_count
                kids_dir = self.out_dir / f"kids_{kids_index:04d}"
                out_file = kids_dir / f"kids_{kids_index:04d}_0x{chunk.offset:08X}.form"
                write_binary(out_file, chunk_bytes(self.data, chunk))
                self.kids_count += 1
                self.add_entry("kids_form", chunk, out_file, None, "")
                self.walk(chunk.data_start + 4, chunk.data_end, depth + 1, chunk.path, kids_dir, current_node_id, True)
                continue

            next_node_id = current_node_id
            if chunk.tag == "FORM" and chunk.form_type == "OBJT":
                self.node_count += 1
                next_node_id = f"node_{self.node_count:05d}"
                if parent_is_kids and current_kids_dir is not None:
                    out_file = current_kids_dir / f"objt_{self.objt_count:06d}_0x{chunk.offset:08X}.form"
                    write_binary(out_file, chunk_bytes(self.data, chunk))
                    self.objt_count += 1
                    self.add_entry("objt_form", chunk, out_file, next_node_id, "")

            if self.export_leaf_chunks and current_kids_dir is not None and chunk.tag in RAW_LEAF_TAGS:
                self.export_leaf(chunk, next_node_id)

            if chunk.tag == "FORM":
                self.walk(chunk.data_start + 4, chunk.data_end, depth + 1, chunk.path, current_kids_dir, next_node_id, False)

    def export_leaf(self, chunk: Chunk, node_id: Optional[str]) -> None:
        tag_dir = self.out_dir / "chunks" / chunk.tag
        index = self.leaf_counts[chunk.tag]
        stem = f"{chunk.tag}_{index:06d}_0x{chunk.offset:08X}"
        bin_file = tag_dir / f"{stem}.bin"
        write_binary(bin_file, chunk_bytes(self.data, chunk))
        self.leaf_counts[chunk.tag] += 1
        self.binary_leaf_count += 1

        text_value = ""
        text_file: Optional[Path] = None
        if chunk.tag in TEXT_TAGS:
            text_value = read_c_string(self.data, chunk.data_start, chunk.data_end)
            text_file = tag_dir / f"{stem}.txt"
            write_text(text_file, text_value)
            self.text_leaf_count += 1

        self.add_entry("leaf_chunk", chunk, bin_file, node_id, text_value)
        if text_file is not None:
            self.entries[-1]["text_output_file"] = self.relative_output(text_file)

    def add_entry(self, kind: str, chunk: Chunk, out_file: Path, node_id: Optional[str], text_value: str) -> None:
        self.entries.append({
            "kind": kind,
            "tag": chunk.tag,
            "form_type": chunk.form_type or "",
            "offset": chunk.offset,
            "size": chunk.size,
            "parent_path": chunk.path.rsplit("/", 1)[0] if "/" in chunk.path else "",
            "chunk_path": chunk.path,
            "node_id": node_id or "",
            "text_value": text_value,
            "output_file": self.relative_output(out_file),
        })

    def relative_output(self, path: Path) -> str:
        return str(path.relative_to(self.out_dir)).replace("\\", "/")


def build_unresolved_lines(references: Dict[str, object], emrs_names_by_class: Dict[str, List[str]]) -> List[str]:
    emrs_full = set()
    emrs_base = set()
    for names in emrs_names_by_class.values():
        for name in names:
            normalized = name.replace("\\", "/").lower()
            emrs_full.add(normalized)
            emrs_base.add(normalized.rsplit("/", 1)[-1])

    lines: List[str] = []
    for group in ("textures", "skeletons", "animations"):
        refs = references[group]
        for ref, count in sorted(refs.items()):
            normalized = ref.replace("\\", "/").lower()
            base = normalized.rsplit("/", 1)[-1]
            if asset_like(ref) and normalized not in emrs_full and base not in emrs_base:
                lines.append(f"[{group}] {ref} (count {count})")
    return lines


def write_raw_base_kids(
    setbas: Path,
    out_dir: Path,
    export_leaf_chunks: bool = True,
    write_manifest: bool = True,
) -> Dict[str, object]:
    data, root = load_setbas(setbas)
    exporter = RawBaseKidsExporter(
        data,
        root,
        out_dir,
        export_leaf_chunks=export_leaf_chunks,
        write_manifest=write_manifest,
    )
    return exporter.export()


def write_outputs(setbas: Path, out_dir: Path) -> Dict[str, object]:
    data, root = load_setbas(setbas)
    exporter = BaseKidsExporter(data, root)
    scenegraph = exporter.export()
    references = exporter.references()
    emrs_names = read_emrs_names(data, root)
    unresolved = build_unresolved_lines(references, emrs_names)

    out_dir.mkdir(parents=True, exist_ok=True)
    scenegraph_path = out_dir / "scenegraph.json"
    references_path = out_dir / "references.json"
    unresolved_path = out_dir / "unresolved_refs.txt"

    scenegraph_path.write_text(json.dumps(scenegraph, indent=2), encoding="utf-8")
    references_path.write_text(json.dumps(references, indent=2), encoding="utf-8")
    if unresolved:
        unresolved_path.write_text("\n".join(unresolved) + "\n", encoding="utf-8")
    else:
        unresolved_path.write_text("<none>\n", encoding="utf-8")

    return {
        "scenegraph_path": scenegraph_path,
        "references_path": references_path,
        "unresolved_path": unresolved_path,
        "kids_count": len(scenegraph["kids_sections"]),
        "node_count": len(scenegraph["nodes"]),
        "top_class_counts": Counter(references["class_counts"]).most_common(10),
        "texture_ref_count": len(references["textures"]),
        "skeleton_ref_count": len(references["skeletons"]),
        "animation_ref_count": len(references["animations"]),
        "unresolved_count": len(unresolved),
    }


def print_summary(summary: Dict[str, object]) -> None:
    print(f"wrote {summary['scenegraph_path']}")
    print(f"wrote {summary['references_path']}")
    print(f"wrote {summary['unresolved_path']}")
    print("")
    print("Summary:")
    print(f"  KIDS sections found: {summary['kids_count']}")
    print(f"  OBJT nodes exported: {summary['node_count']}")
    print("  top CLID counts:")
    for class_name, count in summary["top_class_counts"]:
        print(f"    {class_name}: {count}")
    print(f"  texture references found: {summary['texture_ref_count']}")
    print(f"  skeleton references found: {summary['skeleton_ref_count']}")
    print(f"  animation references found: {summary['animation_ref_count']}")
    print(f"  unresolved references: {summary['unresolved_count']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export BASE/KIDS metadata from a local OpenNeoUA/Urban Assault SET.BAS.")
    parser.add_argument("setbas", metavar="path_to_SET.BAS", help="local SET.BAS file to parse read-only")
    parser.add_argument("--out", required=True, help="metadata output directory")
    return parser
