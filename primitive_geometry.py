"""Session-only primitive generators for the normal geometry clipboard.

The generated shapes are ordinary POO2/POL2 polygons associated with one
existing static ``amesh.class`` ATTS/OLPL material.  No procedural primitive
type is retained after :func:`append_geometry_clipboard` consumes the result.
"""

from __future__ import annotations

import math
from numbers import Integral, Real

from geometry_editor import (
    GeometryClipboard,
    GeometryClipboardError,
    GeometryClipboardPolygon,
)
from sklt_parser import Point3D


DEFAULT_SIZE = 100.0
DEFAULT_RADIUS = 50.0
DEFAULT_SEGMENTS = 12
MIN_SEGMENTS = 3
MAX_SEGMENTS = 64
MAX_DIMENSION = 1_000_000.0

PRIMITIVE_KINDS = (
    "triangle",
    "square",
    "rectangle",
    "circle",
    "plane",
    "cube",
    "pyramid",
    "cylinder",
)

_KIND_ALIASES = {
    "disc": "circle",
    "box": "cube",
}
_QUAD_UVS = ((0, 0), (255, 0), (255, 255), (0, 255))
_TRIANGLE_UVS = ((0, 255), (255, 255), (128, 0))


def _positive_dimension(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise GeometryClipboardError(f"{name} must be a positive number")
    result = float(value)
    if not math.isfinite(result) or result <= 0.0:
        raise GeometryClipboardError(f"{name} must be a positive finite number")
    if result > MAX_DIMENSION:
        raise GeometryClipboardError(
            f"{name} exceeds the safe maximum of {MAX_DIMENSION:g}")
    return result


def _segment_count(value) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise GeometryClipboardError("segments must be an integer")
    result = int(value)
    if not MIN_SEGMENTS <= result <= MAX_SEGMENTS:
        raise GeometryClipboardError(
            f"segments must be between {MIN_SEGMENTS} and {MAX_SEGMENTS}")
    return result


def _take(params: dict, name: str, default):
    return params.pop(name) if name in params else default


def _reject_unknown_params(kind: str, params: dict) -> None:
    if params:
        names = ", ".join(sorted(str(name) for name in params))
        raise GeometryClipboardError(
            f"unsupported {kind} parameter(s): {names}")


def _rectangle_points(width: float, height: float, *, plane: str = "xy"):
    half_width = width * 0.5
    half_height = height * 0.5
    if plane == "xz":
        return (
            (-half_width, 0.0, -half_height),
            (-half_width, 0.0, half_height),
            (half_width, 0.0, half_height),
            (half_width, 0.0, -half_height),
        )
    return (
        (-half_width, -half_height, 0.0),
        (half_width, -half_height, 0.0),
        (half_width, half_height, 0.0),
        (-half_width, half_height, 0.0),
    )


def _radial_uv(angle: float) -> tuple[int, int]:
    return (
        int(round(127.5 + math.cos(angle) * 127.5)),
        int(round(127.5 - math.sin(angle) * 127.5)),
    )


def _build_shape(kind: str, params: dict):
    if kind == "triangle":
        size = _positive_dimension(
            _take(params, "size", DEFAULT_SIZE), "size")
        _reject_unknown_params(kind, params)
        half = size * 0.5
        third_height = math.sqrt(3.0) * size / 6.0
        points = (
            (-half, -third_height, 0.0),
            (half, -third_height, 0.0),
            (0.0, third_height * 2.0, 0.0),
        )
        return points, (((0, 1, 2), _TRIANGLE_UVS),)

    if kind == "square":
        size = _positive_dimension(
            _take(params, "size", DEFAULT_SIZE), "size")
        _reject_unknown_params(kind, params)
        points = _rectangle_points(size, size)
        return points, (((0, 1, 2, 3), _QUAD_UVS),)

    if kind == "rectangle":
        width = _positive_dimension(
            _take(params, "width", DEFAULT_SIZE), "width")
        height = _positive_dimension(
            _take(params, "height", DEFAULT_SIZE), "height")
        _reject_unknown_params(kind, params)
        points = _rectangle_points(width, height)
        return points, (((0, 1, 2, 3), _QUAD_UVS),)

    if kind == "circle":
        radius = _positive_dimension(
            _take(params, "radius", DEFAULT_RADIUS), "radius")
        segments = _segment_count(
            _take(params, "segments", DEFAULT_SEGMENTS))
        _reject_unknown_params(kind, params)
        angles = tuple(
            -math.pi * 0.5 + math.tau * index / segments
            for index in range(segments))
        points = tuple(
            (math.cos(angle) * radius, math.sin(angle) * radius, 0.0)
            for angle in angles)
        uvs = tuple(_radial_uv(angle) for angle in angles)
        return points, ((tuple(range(segments)), uvs),)

    if kind == "plane":
        size = _positive_dimension(
            _take(params, "size", DEFAULT_SIZE), "size")
        _reject_unknown_params(kind, params)
        points = _rectangle_points(size, size, plane="xz")
        return points, (((0, 1, 2, 3), _QUAD_UVS),)

    if kind == "cube":
        size = _positive_dimension(
            _take(params, "size", DEFAULT_SIZE), "size")
        _reject_unknown_params(kind, params)
        half = size * 0.5
        points = (
            (-half, -half, -half),
            (half, -half, -half),
            (half, half, -half),
            (-half, half, -half),
            (-half, -half, half),
            (half, -half, half),
            (half, half, half),
            (-half, half, half),
        )
        faces = (
            (4, 5, 6, 7),  # +Z
            (1, 0, 3, 2),  # -Z
            (0, 4, 7, 3),  # -X
            (5, 1, 2, 6),  # +X
            (3, 7, 6, 2),  # +Y
            (0, 1, 5, 4),  # -Y
        )
        return points, tuple((face, _QUAD_UVS) for face in faces)

    if kind == "pyramid":
        if "base" in params and "base_size" in params:
            raise GeometryClipboardError(
                "use either base or base_size, not both")
        base_value = (
            params.pop("base_size") if "base_size" in params
            else _take(params, "base", DEFAULT_SIZE))
        base_size = _positive_dimension(base_value, "base_size")
        height = _positive_dimension(
            _take(params, "height", DEFAULT_SIZE), "height")
        _reject_unknown_params(kind, params)
        half = base_size * 0.5
        # Keep the generated vertex centroid at the clipboard origin.
        base_y = -height / 5.0
        apex_y = height * 4.0 / 5.0
        points = (
            (-half, base_y, -half),
            (half, base_y, -half),
            (half, base_y, half),
            (-half, base_y, half),
            (0.0, apex_y, 0.0),
        )
        return points, (
            ((0, 1, 2, 3), _QUAD_UVS),
            ((1, 0, 4), _TRIANGLE_UVS),
            ((2, 1, 4), _TRIANGLE_UVS),
            ((3, 2, 4), _TRIANGLE_UVS),
            ((0, 3, 4), _TRIANGLE_UVS),
        )

    if kind == "cylinder":
        radius = _positive_dimension(
            _take(params, "radius", DEFAULT_RADIUS), "radius")
        height = _positive_dimension(
            _take(params, "height", DEFAULT_SIZE), "height")
        segments = _segment_count(
            _take(params, "segments", DEFAULT_SEGMENTS))
        _reject_unknown_params(kind, params)
        half_height = height * 0.5
        angles = tuple(
            math.tau * index / segments for index in range(segments))
        bottom = tuple(
            (math.cos(angle) * radius, -half_height,
             math.sin(angle) * radius)
            for angle in angles)
        top = tuple((x, half_height, z) for x, _y, z in bottom)
        points = bottom + top
        cap_uvs = tuple(_radial_uv(angle) for angle in angles)
        polygons = [
            (tuple(range(segments)), cap_uvs),
            (
                tuple(range(segments * 2 - 1, segments - 1, -1)),
                tuple(reversed(cap_uvs)),
            ),
        ]
        for index in range(segments):
            next_index = (index + 1) % segments
            u0 = int(round(255.0 * index / segments))
            u1 = int(round(255.0 * (index + 1) / segments))
            polygons.append((
                (
                    index,
                    segments + index,
                    segments + next_index,
                    next_index,
                ),
                ((u0, 255), (u0, 0), (u1, 0), (u1, 255)),
            ))
        return points, tuple(polygons)

    raise GeometryClipboardError(f"unsupported primitive kind {kind!r}")


def _area_measure(points, indices) -> float:
    nx = ny = nz = 0.0
    polygon = [points[index] for index in indices]
    for index, (x0, y0, z0) in enumerate(polygon):
        x1, y1, z1 = polygon[(index + 1) % len(polygon)]
        nx += (y0 - y1) * (z0 + z1)
        ny += (z0 - z1) * (x0 + x1)
        nz += (x0 - x1) * (y0 + y1)
    return nx * nx + ny * ny + nz * nz


def _validate_generated_geometry(points, polygons) -> None:
    if not points or not polygons:
        raise GeometryClipboardError("the generated primitive is empty")
    for point in points:
        if len(point) != 3 or not all(math.isfinite(value) for value in point):
            raise GeometryClipboardError(
                "the generated primitive contains invalid coordinates")
    for indices, uvs in polygons:
        if len(indices) < 3 or len(set(indices)) != len(indices):
            raise GeometryClipboardError(
                "the generated primitive contains an invalid polygon")
        if any(index < 0 or index >= len(points) for index in indices):
            raise GeometryClipboardError(
                "the generated primitive contains an invalid point index")
        if _area_measure(points, indices) < 1e-18:
            raise GeometryClipboardError(
                "the generated primitive contains a degenerate polygon")
        if len(uvs) != len(indices) or any(
                not isinstance(u, int) or not isinstance(v, int)
                or not (0 <= u <= 255 and 0 <= v <= 255)
                for u, v in uvs):
            raise GeometryClipboardError(
                "the generated primitive contains invalid UV coordinates")


def _valid_uv_group(group, expected_count: int) -> bool:
    if not isinstance(group, (list, tuple)) or len(group) != expected_count:
        return False
    for uv in group:
        if not isinstance(uv, (list, tuple)) or len(uv) != 2:
            return False
        u, v = uv
        if isinstance(u, bool) or isinstance(v, bool) \
                or not isinstance(u, Integral) \
                or not isinstance(v, Integral) \
                or not (0 <= int(u) <= 255 and 0 <= int(v) <= 255):
            return False
    return True


def _material_context(fam_obj, owner: str, block_index, atts_index):
    model = getattr(fam_obj, "skeleton", None)
    if model is None:
        raise GeometryClipboardError(
            "the selected owner has no editable skeleton")
    if not isinstance(owner, str) or not owner:
        raise GeometryClipboardError("the primitive owner is invalid")
    current_owner = getattr(fam_obj, "owner_path", None)
    if current_owner and current_owner != owner:
        raise GeometryClipboardError(
            "the primitive owner does not match the editable model")
    if isinstance(block_index, bool) or not isinstance(block_index, Integral):
        raise GeometryClipboardError("the material block index is invalid")
    if isinstance(atts_index, bool) or not isinstance(atts_index, Integral):
        raise GeometryClipboardError("the ATTS index is invalid")

    base_object = getattr(fam_obj, "base_object", None)
    blocks = getattr(base_object, "ades", None)
    block_index = int(block_index)
    atts_index = int(atts_index)
    if blocks is None or not 0 <= block_index < len(blocks):
        raise GeometryClipboardError(
            f"material block #{block_index} does not exist")
    block = blocks[block_index]
    if (block.class_id or "").lower() != "amesh.class":
        raise GeometryClipboardError(
            f"material block #{block_index} is not amesh.class")
    for texture in (block.texture, block.tracy_texture):
        if texture is not None and (texture.kind or "").lower() != "ilbm":
            raise GeometryClipboardError(
                f"material block #{block_index} is not a static ILBM material")
    if len(block.atts) != len(block.olpl):
        raise GeometryClipboardError(
            f"material block #{block_index} has ambiguous ATTS/OLPL counts")
    if not 0 <= atts_index < len(block.atts):
        raise GeometryClipboardError(
            f"ATTS entry #{atts_index} does not exist in block #{block_index}")

    entry = block.atts[atts_index]
    atts_values = (
        entry.color_val, entry.shade_val, entry.tracy_val, entry.pad)
    if any(isinstance(value, bool) or not isinstance(value, Integral)
           or not 0 <= int(value) <= 255 for value in atts_values):
        raise GeometryClipboardError(
            f"ATTS entry #{atts_index} contains a value outside byte range")
    if isinstance(entry.poly_id, bool) \
            or not isinstance(entry.poly_id, Integral) \
            or not 0 <= int(entry.poly_id) < len(model.polygons):
        raise GeometryClipboardError(
            f"ATTS entry #{atts_index} references an invalid polygon")
    source_poly_id = int(entry.poly_id)
    source_polygon = tuple(model.polygons[source_poly_id])
    source_uvs = block.olpl[atts_index]
    if len(source_polygon) < 3 \
            or len(set(source_polygon)) != len(source_polygon) \
            or any(index < 0 or index >= len(model.points)
                   for index in source_polygon) \
            or any(not all(math.isfinite(value)
                           for value in model.points[index])
                   for index in source_polygon) \
            or not _valid_uv_group(source_uvs, len(source_polygon)):
        raise GeometryClipboardError(
            "the selected ATTS/OLPL material mapping is invalid")

    matching_refs = [
        (candidate_block_index, candidate_atts_index)
        for candidate_block_index, candidate in enumerate(blocks)
        for candidate_atts_index, candidate_entry in enumerate(candidate.atts)
        if candidate_entry.poly_id == source_poly_id
    ]
    if matching_refs != [(block_index, atts_index)]:
        raise GeometryClipboardError(
            "the selected polygon has ambiguous duplicate material mappings")
    return model, block_index, entry, source_poly_id


def build_primitive_clipboard(
        fam_obj, owner: str, block_index: int, atts_index: int,
        kind: str, **params) -> GeometryClipboard:
    """Build one normal-geometry clipboard from a current static material.

    Canonical ``kind`` values are listed in :data:`PRIMITIVE_KINDS`.
    ``disc`` aliases ``circle`` and ``box`` aliases ``cube``.  Circle and
    cylinder segment counts default to 12 and are limited to 3..64.
    """

    if not isinstance(kind, str):
        raise GeometryClipboardError("primitive kind must be text")
    normalized_kind = kind.strip().lower()
    normalized_kind = _KIND_ALIASES.get(normalized_kind, normalized_kind)
    if normalized_kind not in PRIMITIVE_KINDS:
        raise GeometryClipboardError(
            f"unsupported primitive kind {kind!r}")

    model, block_index, entry, source_poly_id = _material_context(
        fam_obj, owner, block_index, atts_index)
    points, topology = _build_shape(normalized_kind, dict(params))
    points = tuple(
        tuple(float(value) for value in point) for point in points)
    topology = tuple(
        (tuple(indices), tuple(tuple(uv) for uv in uvs))
        for indices, uvs in topology)
    _validate_generated_geometry(points, topology)

    count = float(len(points))
    raw_pivot = tuple(
        sum(point[axis] for point in points) / count
        for axis in range(3))
    scale = max(
        1.0, *(abs(value) for point in points for value in point))
    pivot: Point3D = tuple(
        0.0 if abs(value) <= scale * 1e-12 else value
        for value in raw_pivot)
    local_points = tuple(
        tuple(point[axis] - pivot[axis] for axis in range(3))
        for point in points)
    polygons = tuple(
        GeometryClipboardPolygon(
            source_poly_id=source_poly_id,
            local_indices=indices,
            block_index=block_index,
            color_val=int(entry.color_val),
            shade_val=int(entry.shade_val),
            tracy_val=int(entry.tracy_val),
            pad=int(entry.pad),
            uvs=uvs,
            atts_only_mapping=False,
            block_class_id="amesh.class",
        )
        for indices, uvs in topology)
    return GeometryClipboard(
        owner=owner,
        model_identity=id(model),
        source_name=model.source_name,
        points=points,
        local_points=local_points,
        pivot=pivot,
        original_to_local=(),
        polygons=polygons,
    )
