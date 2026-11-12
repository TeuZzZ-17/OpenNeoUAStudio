"""Convert a model's materials using the original Gharghoil III Genesis."""

from __future__ import annotations

import copy
import math

from studio.formats.animation import parse_anm_bytes
from studio.assets.animated_polygons import build_vanm
from studio.assets.mapping import MappingEditError, _wrap_ades_objt, rewrite_block_texture_template
from studio.formats.base_format import AttsEntry, parse_base_bytes
from studio.formats.images import parse_ilbm_bytes
from . import genesis_reference as reference


def _material(data):
    return parse_base_bytes(_wrap_ades_objt(data), "<Genesis reference>").root.ades[0]


def _adapt_animation(source, vertices, name):
    """Map larger polygons into the same five animated atlas regions."""
    normal = [0.0, 0.0, 0.0]
    for a, b in zip(vertices, vertices[1:] + vertices[:1]):
        for axis in range(3):
            j, k = (axis + 1) % 3, (axis + 2) % 3
            normal[axis] += (a[j] - b[j]) * (a[k] + b[k])
    omit = max(range(3), key=lambda axis: abs(normal[axis]))
    axes = [axis for axis in range(3) if axis != omit]
    low = [min(v[axis] for v in vertices) for axis in axes]
    span = [max(v[axis] for v in vertices) - minimum
            for axis, minimum in zip(axes, low)]
    if any(value <= 1e-9 for value in span):
        raise MappingEditError("A Genesis polygon has no usable surface area.")
    coordinates = [tuple((v[axis] - minimum) / width
                         for axis, minimum, width in zip(axes, low, span))
                   for v in vertices]
    groups = []
    for quad in source.texcoord_groups:
        # The reference quad is ordered bottom-right, bottom-left,
        # top-left, top-right. Keep each frame inside that exact atlas region.
        groups.append([tuple(round(
            (1-x)*(1-y)*quad[2][axis] + x*(1-y)*quad[3][axis]
            + (1-x)*y*quad[1][axis] + x*y*quad[0][axis])
            for axis in range(2)) for x, y in coordinates])
    return build_vanm(source, groups, name)


def _animation_signature(animation):
    return (animation.bitmap_class, animation.bitmap_names,
            animation.texcoord_groups, animation.frames)


def generate_genesis(family, obj):
    """Replace the selected model's materials without changing its geometry."""
    model = obj.skeleton
    if model is None or not model.polygons:
        raise MappingEditError("Load a model with polygon surfaces first.")
    if len(model.polygons) > 32768:
        raise MappingEditError("Genesis polygon IDs exceed the native BASE limit.")
    triangle = _material(reference.TRIANGLE_AREA)
    quad = _material(reference.QUAD_AREA)
    tri_animation = parse_anm_bytes(reference.TRIANGLE_ANIMATION, "GENES3-1.ANM")
    # Build rectangular atlas regions from the existing triangle animation.
    quad_groups = []
    for group in tri_animation.texcoord_groups:
        left, right = min(u for u, _ in group), max(u for u, _ in group)
        top, bottom = min(v for _, v in group), max(v for _, v in group)
        quad_groups.append([(right, bottom), (left, bottom),
                            (left, top), (right, top)])
    quad_animation = build_vanm(tri_animation, quad_groups, "GENQ4-1.ANM")
    bitmap_name = next((name for name in family.textures
                        if name.casefold() == "fx2.ilbm"), "FX2.ILBM")
    for animation in (tri_animation, quad_animation):
        if animation.bitmap_names != [bitmap_name]:
            animation.bitmap_names = [bitmap_name]
            updated = build_vanm(animation, animation.texcoord_groups,
                                       animation.source_name)
            animation.original_data = updated.original_data
            animation.texcoord_offsets = updated.texcoord_offsets
    animations = dict(family.animations)
    blocks = []
    for poly_id, polygon in enumerate(model.polygons):
        if len(polygon) < 3:
            continue
        if any(i < 0 or i >= len(model.points) for i in polygon):
            raise MappingEditError(f"Polygon #{poly_id} contains an invalid vertex.")
        vertices = [model.points[i] for i in polygon]
        if any(not math.isfinite(value) for vertex in vertices for value in vertex):
            raise MappingEditError(f"Polygon #{poly_id} contains invalid coordinates.")
        block = copy.deepcopy(triangle if len(polygon) == 3 else quad)
        animation = copy.deepcopy(tri_animation if len(polygon) == 3 else quad_animation)
        name = animation.source_name
        if len(polygon) > 4:
            name = f"GNS{poly_id:05}.ANM"
            animation = _adapt_animation(quad_animation, vertices, name)
        # Reuse identical resources; never replace another model's animation.
        suffix = 0
        while True:
            match = next(((key, a) for key, a in animations.items()
                          if key.casefold() == name.casefold()), None)
            if match is None:
                break
            if _animation_signature(match[1]) == _animation_signature(animation):
                name = match[0]
                break
            suffix += 1
            if suffix > 4095:
                raise MappingEditError("No free Genesis animation name is available.")
            name = f"G{poly_id:04X}{suffix:03X}.ANM"
        if name != block.texture.name:
            template = rewrite_block_texture_template(block, name)
            block = _material(template)
        animation.source_name = name
        animations.setdefault(name, animation)
        block.ade_poly_id = poly_id
        block.atts = [AttsEntry(poly_id, 0, 0, 128, 0)]
        blocks.append(block)
    if not blocks:
        raise MappingEditError("This model contains no polygon surfaces.")
    # FX2 belongs to the active SET palette. Prefer that SET's existing atlas.
    if not any(name.casefold() == "fx2.ilbm" for name in family.textures):
        from studio.assets.family import _load_texture, SETBAS_OVERRIDE_PREFIX
        from studio.exchange.portable import AssetResolver
        staged = copy.copy(family)
        staged.textures = dict(family.textures)
        staged.texture_refs = {}
        staged.warnings = []
        staged.load_errors = {}
        overrides = {key: value for key, value in family.overrides.items()
                     if not str(value).startswith(SETBAS_OVERRIDE_PREFIX)}
        resolver = AssetResolver(family.search_roots, overrides,
                                 prefer_earliest_root=True)
        _load_texture(staged, resolver, "FX2.ILBM")
        if "FX2.ILBM" in staged.textures:
            family.textures["FX2.ILBM"] = staged.textures["FX2.ILBM"]
            family.texture_refs["FX2.ILBM"] = staged.texture_refs["FX2.ILBM"]
        elif staged.texture_refs["FX2.ILBM"].status == "missing":
            family.textures["FX2.ILBM"] = parse_ilbm_bytes(reference.FX_TEXTURE, "FX2.ILBM")
        else:
            raise MappingEditError("FX2.ILBM could not be resolved: "
                                   + "; ".join(staged.warnings))
    family.animations.clear()
    family.animations.update(animations)
    # Only surface materials are converted. Other typed ADE objects retain
    # their existing emitters and references to the unchanged skeleton.
    blocks.extend(copy.deepcopy(block) for block in obj.base_object.ades
                  if block.class_id.casefold() not in ("amesh.class", "area.class"))
    obj.base_object.ades[:] = blocks
