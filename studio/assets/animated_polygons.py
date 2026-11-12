"""Static textured polygon -> vanilla-style animated polygon (area.class + bmpanim.class + a new ANM).

Shared by the Studio window (Add UV Phase / Paste UV Position on a still face), the MCP ``animate_polygons`` tool
and the Genesis generator, so the animated block template and the ANM writer exist only once.
"""

from __future__ import annotations

import copy
import struct

from studio.assets.mapping import (
    MappingEditError,
    MappingIndex,
    MaterialPasteResult,
    _wrap_ades_objt,
    rewrite_block_texture_template,
)
from studio.formats.animation import (
    VanmData,
    VanmFrame,
    edit_anm_phase,
    parse_anm_bytes,
    place_new_phase,
)
from studio.formats.base_format import AttsEntry, parse_base_bytes

# Opaque animated-polygon material (area.class + bmpanim.class "SIMPEL.ANM"), taken from the original Weasel.
# The name space of this template is 10 characters, so every ANM name must fit "NAME.ANM" in 10 characters.
ANIMATED_POLYGON_TEMPLATE = bytes.fromhex(
    "464f524d000000a84f424a54434c49440000000b617265612e636c6173730000"
    "464f524d0000008841524541464f524d0000002241444520464f524d00000004"
    "524f4f54535452430000000a00010006000000000000535452430000000a0001"
    "0001003a00008000464f524d000000404f424a54434c49440000000e626d7061"
    "6e696d2e636c61737300464f524d0000001e42414e4953545243000000110001"
    "0006000053494d50454c2e414e4d0000")

ANIMATION_NAME_LIMIT = 10
DEFAULT_FRAME_TIME = 256   # 1024 Hz ticks = 250 ms per phase


def build_vanm(source, groups, name):
    """Build native VANM data from ``source`` (class, bitmaps, frames) + UV ``groups`` and verify it with the parser."""
    bitmap_class = source.bitmap_class.encode("latin-1") + b"\0"
    bitmaps = b"".join(n.encode("latin-1") + b"\0" for n in source.bitmap_names)
    count = sum(len(group) + 1 for group in groups)
    if count > 32767:
        raise MappingEditError("The animation exceeds the VANM UV limit.")
    data = struct.pack(">h", len(bitmap_class)) + bitmap_class
    data += struct.pack(">h", len(bitmaps)) + bitmaps + struct.pack(">h", count)
    for group in groups:
        data += struct.pack(">h", len(group)) + bytes(c for uv in group for c in uv)
    data += struct.pack(">h", len(source.frames))
    data += b"".join(struct.pack(">ihh", f.frame_time, f.frame_id, f.texcoords_id)
                     for f in source.frames)
    chunk = b"DATA" + struct.pack(">I", len(data)) + data
    chunk += b"\0" if len(data) % 2 else b""
    encoded = b"FORM" + struct.pack(">I", len(chunk) + 4) + b"VANM" + chunk
    parsed = parse_anm_bytes(encoded, name)
    if parsed.warnings or parsed.texcoord_groups != groups or parsed.frames != source.frames:
        raise MappingEditError("The generated animation failed verification.")
    return parsed


def animated_polygon_block(anm_name, poly_id):
    """New animated area.class block that plays ``anm_name`` on polygon ``poly_id``.

    An area.class block has no ATTS chunk: its mapping is derived from the block itself (polygon, color, shade and
    tracy of its own STRC, drawn unshaded like the originals), exactly as the BASE parser rebuilds it.
    """
    base = parse_base_bytes(_wrap_ades_objt(ANIMATED_POLYGON_TEMPLATE), "<animated polygon>").root.ades[0]
    template = rewrite_block_texture_template(base, anm_name)
    block = copy.deepcopy(
        parse_base_bytes(_wrap_ades_objt(template), "<animated polygon>").root.ades[0])
    block.ade_poly_id = poly_id
    block.atts = [AttsEntry(poly_id, block.color_val, block.shade_val, block.tracy_val, 0)]
    return block


def new_animation_name(is_taken):
    """First free ``UVAnnn.ANM`` (``is_taken(name)`` -> bool), 10 characters like the block template allows."""
    for number in range(1, 1000):
        name = f"UVA{number:03d}.ANM"
        if not is_taken(name):
            return name
    raise MappingEditError("No free UVAnnn.ANM name is left.")


def suggest_second_phase(uvs, bitmap_name="", frame_time=DEFAULT_FRAME_TIME):
    """UVs of a copy of ``uvs`` on the nearest free area of the texture (the first spot offered to the user)."""
    template = VanmData(source_name="", bitmap_class="ilbm.class", bitmap_names=[bitmap_name],
                        frames=[VanmFrame(frame_time, 0, 0)])
    animation = build_vanm(template, [[tuple(uv) for uv in uvs]], "<phase>")
    return place_new_phase(animation, animation.frames[0])


def convert_polygon_to_animation(family, fam_obj, poly_id, anm_name, *, second_uvs=None,
                                 frame_time=DEFAULT_FRAME_TIME):
    """Turn one still textured polygon into a 2-phase animation.

    Phase 1 = its current UVs. Phase 2 = ``second_uvs`` or, when None, a copy placed on a free area of the same
    texture. The polygon leaves its amesh block (an emptied block is removed) and gets an animated area.class
    block; the new ANM is stored in ``family.animations`` (written beside the BASE on export).
    """
    model = getattr(fam_obj, "skeleton", None)
    blocks = getattr(getattr(fam_obj, "base_object", None), "ades", None)
    if model is None or blocks is None or not 0 <= poly_id < len(model.polygons):
        raise MappingEditError("select a valid polygon of an editable model first")
    if len(anm_name) > ANIMATION_NAME_LIMIT or any(
            key.casefold() == anm_name.casefold() for key in family.animations):
        raise MappingEditError(f"{anm_name!r} is not a free animation name of at most {ANIMATION_NAME_LIMIT} characters")
    refs = MappingIndex(fam_obj).refs.get(poly_id, [])
    if len(refs) != 1:
        raise MappingEditError("the polygon needs exactly one material mapping")
    ref = refs[0]
    block = ref.block
    texture = block.texture
    if (block.class_id or "").lower() != "amesh.class" or block.uses_atts_only_mapping \
            or texture is None or texture.kind != "ilbm" or ref.atts_index >= len(block.olpl):
        raise MappingEditError("only a still textured polygon can become an animation")
    uvs = [tuple(uv) for uv in block.olpl[ref.atts_index]]
    if len(uvs) != len(model.polygons[poly_id]):
        raise MappingEditError("the polygon UV count differs from its vertex count")
    template = VanmData(source_name=anm_name, bitmap_class="ilbm.class", bitmap_names=[texture.name])
    if second_uvs is None:
        template.frames = [VanmFrame(frame_time, 0, 0)]
        animation = edit_anm_phase(build_vanm(template, [uvs], anm_name), 0, duplicate=True)
    else:
        second = [tuple(uv) for uv in second_uvs]
        if len(second) != len(uvs) or any(not 0 <= c <= 255 for uv in second for c in uv):
            raise MappingEditError("the second phase needs one UV (0..255) for every vertex")
        template.frames = [VanmFrame(frame_time, 0, 0), VanmFrame(frame_time, 0, 1)]
        animation = build_vanm(template, [uvs, second], anm_name)
        animation.timeline_dirty = True
    animation.new_file = True
    new_block = animated_polygon_block(anm_name, poly_id)
    block.atts.pop(ref.atts_index)
    block.olpl.pop(ref.atts_index)
    if not block.atts:
        blocks.pop(ref.block_index)
    blocks.append(new_block)
    family.animations[anm_name] = animation
    return MaterialPasteResult(
        block_index=len(blocks) - 1, assigned_poly_id=poly_id,
        imported_resources=(("animation", anm_name),))
