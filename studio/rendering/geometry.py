"""Geometry editing engine: edit sessions, clipboard, mirror/delete plans, FX authoring and FX atlas/element editing.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
import math
from math import sqrt
from typing import Mapping

from studio.assets.family import FamilyObject
from studio.assets.mapping import (
    build_material_block_clipboard,
    build_polygon_reference_graph,
    diagnose_polygon_references,
    MappingEditError,
    MappingIndex,
    MaterialBlockClipboard,
    paste_material_block,
    plan_polygon_renumber,
    PolygonReferenceError,
    StructuralBlockState,
    transfer_material_resources,
)
from studio.formats.animation import VanmData
from studio.formats.base_format import AmeshBlock, AttsEntry, parse_base_bytes, TextureRef
from studio.formats.sklt import Point3D, SkltModel
from studio.rendering.indexed import UnsupportedIndexedMaterialError, validate_retail_polygon_flags

# ----------------------------------------------------------------------------
# Geometry editing state and safe geometry clipboard helpers.
# (formerly geometry_editor.py)
# ----------------------------------------------------------------------------

Matrix3 = list[list[float]]


MAX_UNDO = 64


class GeometryClipboardError(ValueError):
    pass


@dataclass(frozen=True)
class GeometryClipboardPolygon:
    source_poly_id: int
    local_indices: tuple[int, ...]
    block_index: int
    color_val: int
    shade_val: int
    tracy_val: int
    pad: int
    uvs: tuple[tuple[int, int], ...]
    atts_only_mapping: bool
    block_class_id: str
    block_template: AmeshBlock | None = field(
        default=None, repr=False, compare=False)


@dataclass(frozen=True)
class GeometryClipboard:
    owner: str
    model_identity: int
    source_name: str
    points: tuple[Point3D, ...]
    local_points: tuple[Point3D, ...]
    pivot: Point3D
    original_to_local: tuple[tuple[int, int], ...]
    polygons: tuple[GeometryClipboardPolygon, ...]


@dataclass(frozen=True)
class TopologyAppendResult:
    point_indices: tuple[int, ...]
    polygon_indices: tuple[int, ...]


@dataclass(frozen=True)
class GeometryDeletePlan:
    points: tuple[Point3D, ...]
    polygons: tuple[tuple[int, ...], ...]
    blocks: tuple[AmeshBlock, ...]
    source_block_classes: tuple[str, ...]
    deleted_polygon_indices: tuple[int, ...]
    old_to_new_polygon: tuple[tuple[int, int], ...]
    old_to_new_point: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class TopologyDeleteResult:
    deleted_polygon_indices: tuple[int, ...]
    old_to_new_polygon: tuple[tuple[int, int], ...]
    old_to_new_point: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class MirrorMatchResult:
    """Deterministic point symmetry graph used by automatic Mirror Select."""

    axes: tuple[int, ...]
    tolerance: float
    counterparts: tuple[tuple[int, tuple[int, ...]], ...]
    unmatched: frozenset[int]
    self_mirrored: frozenset[int]
    ambiguous: tuple[tuple[int, tuple[int, ...]], ...]
    ambiguous_vertices: frozenset[int]
    components: tuple[tuple[int, ...], ...]
    axis_pairs: tuple[tuple[int, tuple[tuple[int, int], ...]], ...]
    axis_ambiguities: tuple[
        tuple[int, tuple[tuple[int, tuple[int, ...]], ...]], ...]

    @property
    def recipient_indices(self) -> frozenset[int]:
        return frozenset(
            target
            for _source, targets in self.counterparts
            for target in targets)


@dataclass(frozen=True)
class MirrorPolygonMatchResult:
    counterparts: tuple[tuple[int, tuple[int, ...]], ...]
    unmatched: frozenset[int]
    self_mirrored: frozenset[int]
    ambiguous: tuple[tuple[int, tuple[int, ...]], ...]
    point_match: MirrorMatchResult

    @property
    def recipient_indices(self) -> frozenset[int]:
        return frozenset(
            target
            for _source, targets in self.counterparts
            for target in targets)


def normalize_mirror_axes(axes) -> tuple[int, ...]:
    """Return unique model-space X/Y/Z indices in canonical order."""

    values = set()
    for axis in axes:
        if isinstance(axis, str):
            name = axis.strip().upper()
            if name not in ("X", "Y", "Z"):
                raise ValueError(f"invalid Mirror axis {axis!r}")
            values.add("XYZ".index(name))
        elif isinstance(axis, int) and axis in (0, 1, 2):
            values.add(axis)
        else:
            raise ValueError(f"invalid Mirror axis {axis!r}")
    return tuple(sorted(values))


def mirror_tolerance(points, tolerance: float | None = None) -> float:
    """Use an extent-relative tolerance for automatic Mirror Select."""

    values = [tuple(float(value) for value in point) for point in points]
    if tolerance is not None:
        result = float(tolerance)
        if not math.isfinite(result) or result <= 0.0:
            raise ValueError("Mirror tolerance must be finite and positive")
        return result
    if not values:
        return 1e-5
    extent = max(
        max(point[axis] for point in values)
        - min(point[axis] for point in values)
        for axis in range(3))
    return max(1e-5, extent * 1e-5)


def _reflected_point(point, axes, tolerance: float) -> Point3D:
    values = [float(value) for value in point]
    for axis in axes:
        values[axis] = (
            0.0 if abs(values[axis]) <= tolerance else -values[axis])
    return tuple(values)


def _point_candidates(points, desired, tolerance: float) -> tuple[int, ...]:
    return tuple(
        index for index, candidate in enumerate(points)
        if all(
            abs(candidate[axis] - desired[axis]) <= tolerance
            for axis in range(3)))


def match_mirror_points(
        points, selection, axes, tolerance: float | None = None,
        candidates=None,
        ) -> MirrorMatchResult:
    """Build one-to-one axis edges and symmetry components without guessing."""

    values = [tuple(float(value) for value in point) for point in points]
    active_axes = normalize_mirror_axes(axes)
    selected = frozenset(int(index) for index in selection)
    invalid = sorted(
        index for index in selected if index < 0 or index >= len(values))
    if invalid:
        raise ValueError(
            f"Mirror selection contains invalid point index {invalid[0]}")
    eligible = (
        set(range(len(values))) if candidates is None
        else {int(index) for index in candidates})
    invalid_candidates = sorted(
        index for index in eligible if index < 0 or index >= len(values))
    if invalid_candidates:
        raise ValueError(
            "Mirror candidates contain invalid point index "
            f"{invalid_candidates[0]}")
    eligible.update(selected)
    resolved_tolerance = mirror_tolerance(values, tolerance)
    adjacency = {index: set() for index in range(len(values))}
    axis_maps: dict[int, dict[int, int]] = {}
    ambiguities_by_axis: dict[int, dict[int, tuple[int, ...]]] = {}
    ambiguous_vertices = set()

    for axis in active_axes:
        axis_map: dict[int, int] = {}
        axis_ambiguous: dict[int, tuple[int, ...]] = {}
        for index in sorted(eligible):
            point = values[index]
            if abs(point[axis]) <= resolved_tolerance:
                axis_map[index] = index
                continue
            desired = _reflected_point(
                point, (axis,), resolved_tolerance)
            candidates = tuple(
                candidate for candidate in _point_candidates(
                    values, desired, resolved_tolerance)
                if candidate != index and candidate in eligible)
            if len(candidates) == 1:
                axis_map[index] = candidates[0]
            elif len(candidates) > 1:
                axis_ambiguous[index] = candidates
                ambiguous_vertices.add(index)
                ambiguous_vertices.update(candidates)

        # A valid edge must be a deterministic reciprocal one-to-one pair.
        for source, target in tuple(axis_map.items()):
            if source == target:
                continue
            if axis_map.get(target) != source:
                candidates = tuple(sorted({
                    target,
                    *(axis_ambiguous.get(source, ())),
                    *(axis_ambiguous.get(target, ())),
                }))
                axis_ambiguous[source] = candidates
                ambiguous_vertices.add(source)
                ambiguous_vertices.update(candidates)
                axis_map.pop(source, None)
                if axis_map.get(target) == source:
                    axis_map.pop(target, None)

        for source, target in axis_map.items():
            if source != target:
                adjacency[source].add(target)
                adjacency[target].add(source)
        axis_maps[axis] = axis_map
        ambiguities_by_axis[axis] = axis_ambiguous

    components = []
    component_for = {}
    visited = set()
    for start in range(len(values)):
        if start in visited:
            continue
        pending = [start]
        component = []
        while pending:
            index = pending.pop()
            if index in visited:
                continue
            visited.add(index)
            component.append(index)
            pending.extend(sorted(adjacency[index] - visited, reverse=True))
        normalized = tuple(sorted(component))
        component_index = len(components)
        components.append(normalized)
        for index in normalized:
            component_for[index] = component_index

    counterparts = []
    unmatched = set()
    self_mirrored = set()
    selected_ambiguities = []
    for source in sorted(selected):
        source_ambiguities = sorted({
            candidate
            for axis in active_axes
            for candidate in ambiguities_by_axis[axis].get(source, ())
        })
        if source_ambiguities:
            selected_ambiguities.append(
                (source, tuple(source_ambiguities)))
            continue
        if any(
                axis_maps[axis].get(source) == source
                for axis in active_axes):
            self_mirrored.add(source)
        component = components[component_for[source]]
        targets = tuple(
            index for index in component
            if index not in selected
            and index not in ambiguous_vertices)
        if targets:
            counterparts.append((source, targets))
        elif source not in self_mirrored:
            unmatched.add(source)

    return MirrorMatchResult(
        axes=active_axes,
        tolerance=resolved_tolerance,
        counterparts=tuple(counterparts),
        unmatched=frozenset(unmatched),
        self_mirrored=frozenset(self_mirrored),
        ambiguous=tuple(selected_ambiguities),
        ambiguous_vertices=frozenset(ambiguous_vertices),
        components=tuple(components),
        axis_pairs=tuple(
            (axis, tuple(sorted(axis_maps[axis].items())))
            for axis in active_axes),
        axis_ambiguities=tuple(
            (axis, tuple(sorted(ambiguities_by_axis[axis].items())))
            for axis in active_axes),
    )


def _axis_masks(axes: tuple[int, ...]):
    for mask in range(1, 1 << len(axes)):
        yield tuple(
            axis for bit, axis in enumerate(axes)
            if mask & (1 << bit))


def match_mirror_polygons(
        points, polygons, selection, axes,
        tolerance: float | None = None) -> MirrorPolygonMatchResult:
    """Match polygon counterparts by mirrored vertex sets, not POL2 order."""

    polygon_values = [tuple(int(index) for index in polygon)
                      for polygon in polygons]
    selected = frozenset(int(poly_id) for poly_id in selection)
    invalid = sorted(
        poly_id for poly_id in selected
        if poly_id < 0 or poly_id >= len(polygon_values))
    if invalid:
        raise ValueError(
            f"Mirror selection contains invalid polygon #{invalid[0]}")
    source_vertices = {
        index for poly_id in selected for index in polygon_values[poly_id]}
    point_match = match_mirror_points(
        points, source_vertices, axes, tolerance)
    axis_maps = {
        axis: dict(pairs) for axis, pairs in point_match.axis_pairs}
    axis_ambiguities = {
        axis: dict(entries)
        for axis, entries in point_match.axis_ambiguities}
    by_vertices: dict[tuple[int, frozenset[int]], list[int]] = {}
    for poly_id, polygon in enumerate(polygon_values):
        by_vertices.setdefault(
            (len(polygon), frozenset(polygon)), []).append(poly_id)

    matched_by_source: dict[int, set[int]] = {
        poly_id: set() for poly_id in selected}
    unmatched = set()
    self_mirrored = set()
    ambiguous: dict[int, set[int]] = {}
    for source_poly in sorted(selected):
        source_polygon = polygon_values[source_poly]
        had_missing = False
        for mask_axes in _axis_masks(point_match.axes):
            target_vertices = list(source_polygon)
            mask_ambiguous = set()
            missing = False
            for axis in mask_axes:
                next_vertices = []
                for vertex in target_vertices:
                    candidates = axis_ambiguities[axis].get(vertex)
                    if candidates:
                        mask_ambiguous.update(candidates)
                        missing = True
                        continue
                    target = axis_maps[axis].get(vertex)
                    if target is None:
                        missing = True
                    else:
                        next_vertices.append(target)
                if missing:
                    break
                target_vertices = next_vertices
            if mask_ambiguous:
                ambiguous.setdefault(source_poly, set()).update(
                    mask_ambiguous)
                continue
            if missing:
                had_missing = True
                continue
            target_key = (
                len(target_vertices), frozenset(target_vertices))
            source_key = (
                len(source_polygon), frozenset(source_polygon))
            if target_key == source_key:
                self_mirrored.add(source_poly)
                continue
            candidates = by_vertices.get(target_key, [])
            if len(candidates) == 1:
                matched_by_source[source_poly].add(candidates[0])
            elif len(candidates) > 1:
                ambiguous.setdefault(source_poly, set()).update(candidates)
            else:
                had_missing = True
        if not matched_by_source[source_poly] \
                and source_poly not in self_mirrored \
                and source_poly not in ambiguous:
            unmatched.add(source_poly)
        elif had_missing and not matched_by_source[source_poly]:
            unmatched.add(source_poly)

    return MirrorPolygonMatchResult(
        counterparts=tuple(
            (source, tuple(sorted(targets)))
            for source, targets in sorted(matched_by_source.items())
            if targets),
        unmatched=frozenset(unmatched),
        self_mirrored=frozenset(self_mirrored),
        ambiguous=tuple(
            (source, tuple(sorted(candidates)))
            for source, candidates in sorted(ambiguous.items())),
        point_match=point_match,
    )


def _polygon_area_measure(points) -> float:
    """Squared Newell-normal length; zero means a degenerate polygon."""

    nx = ny = nz = 0.0
    for index, (x0, y0, z0) in enumerate(points):
        x1, y1, z1 = points[(index + 1) % len(points)]
        nx += (y0 - y1) * (z0 + z1)
        ny += (z0 - z1) * (x0 + x1)
        nz += (x0 - x1) * (y0 + y1)
    return nx * nx + ny * ny + nz * nz


def propagate_mirror_transform(
        origin, pending: list[Point3D], sources, match: MirrorMatchResult,
        tolerance: float, axes=(0, 1, 2)) -> None:
    """Copy the move of the ``sources`` points onto their mirror counterparts (in place on ``pending``).

    ``origin`` holds the positions before the move, ``pending`` the positions after it. Points that sit on a
    mirror plane stay on it. Shared by the editor's Mirror Select and the MCP."""

    for component in match.components:
        component_sources = [
            index for index in component if index in sources]
        if not component_sources or any(
                index in match.ambiguous_vertices
                for index in component):
            continue
        source = min(component_sources)
        source_point = pending[source]
        for target in component:
            values = list(source_point)
            for axis in axes:
                source_coordinate = origin[source][axis]
                target_coordinate = origin[target][axis]
                if abs(target_coordinate) <= tolerance:
                    values[axis] = 0.0
                elif source_coordinate * target_coordinate < 0.0:
                    values[axis] = -values[axis]
            pending[target] = tuple(values)
    for index in sources:
        if index >= len(origin):
            continue
        values = list(pending[index])
        for axis in axes:
            if abs(origin[index][axis]) <= tolerance:
                values[axis] = 0.0
        pending[index] = tuple(values)


def build_geometry_clipboard(
        fam_obj: FamilyObject, owner: str, poly_ids, mapping,
        unsafe_fx_poly_ids=(), *, require_writable_chunks: bool = False
        ) -> GeometryClipboard:
    """Validate and copy mapped static polygons without retaining live refs."""

    model = getattr(fam_obj, "skeleton", None)
    selected = tuple(sorted(set(poly_ids)))
    if model is None:
        raise GeometryClipboardError("the selected owner has no editable skeleton")
    if not selected:
        raise GeometryClipboardError("select one or more polygons first")

    unsafe_fx = set(unsafe_fx_poly_ids)
    original_to_local: dict[int, int] = {}
    copied_points: list[Point3D] = []
    copied_polygons: list[GeometryClipboardPolygon] = []
    for poly_id in selected:
        if not (0 <= poly_id < len(model.polygons)):
            raise GeometryClipboardError(f"polygon #{poly_id} is invalid")
        polygon = tuple(model.polygons[poly_id])
        if len(polygon) < 3:
            raise GeometryClipboardError(
                f"polygon #{poly_id} has fewer than three vertices")
        if len(set(polygon)) != len(polygon):
            raise GeometryClipboardError(
                f"polygon #{poly_id} repeats a POO2 vertex index")
        if any(index < 0 or index >= len(model.points) for index in polygon):
            raise GeometryClipboardError(
                f"polygon #{poly_id} references an invalid POO2 index")
        polygon_points = [model.points[index] for index in polygon]
        if not all(math.isfinite(value)
                   for point in polygon_points for value in point):
            raise GeometryClipboardError(
                f"polygon #{poly_id} contains non-finite coordinates")
        if _polygon_area_measure(polygon_points) < 1e-18:
            raise GeometryClipboardError(f"polygon #{poly_id} is degenerate")

        refs = mapping.refs.get(poly_id, [])
        if not refs:
            raise GeometryClipboardError(f"polygon #{poly_id} is unmapped")
        if len(refs) != 1:
            raise GeometryClipboardError(
                f"polygon #{poly_id} has ambiguous duplicate mappings")
        ref = refs[0]
        block = ref.block
        block_class = (block.class_id or "").lower()
        if block_class not in ("amesh.class", "area.class"):
            raise GeometryClipboardError(
                f"polygon #{poly_id} uses unsupported class "
                f"{block.class_id or '<missing class>'!r}")
        if block_class == "area.class":
            if ref.atts_index != 0 \
                    or block.ade_strc_chunk_offset < 0 \
                    or not block.source_objt_bytes:
                raise GeometryClipboardError(
                    f"polygon #{poly_id} area.class has no writable "
                    "FORM ADE/STRC source")
        elif require_writable_chunks and (
                    block.atts_chunk_offset < 0
                    or block.olpl_chunk_offset < 0):
                raise GeometryClipboardError(
                    f"polygon #{poly_id} has no writable ATTS/OLPL chunks")
        if ref.atts_index >= len(block.atts):
            raise GeometryClipboardError(
                f"polygon #{poly_id} has no valid ATTS record")
        texture = block.texture
        atts_only_animation = bool(
            block_class == "area.class"
            and texture is not None
            and texture.kind == "bmpanim"
            and not block.olpl)
        if ref.atts_index >= len(block.olpl) \
                and not atts_only_animation:
            raise GeometryClipboardError(
                f"polygon #{poly_id} has no matching OLPL group")
        if block_class == "amesh.class" \
                and not atts_only_animation \
                and len(block.atts) != len(block.olpl):
            raise GeometryClipboardError(
                f"polygon #{poly_id} belongs to a block with ambiguous "
                "ATTS/OLPL counts")
        if block_class == "amesh.class" \
                and texture is not None and texture.kind == "bmpanim":
            raise GeometryClipboardError(
                f"polygon #{poly_id} uses animated BMPANIM/VANM material")
        if poly_id in unsafe_fx:
            raise GeometryClipboardError(
                f"FX polygon #{poly_id} has shared or ambiguous references")
        uvs = (
            tuple(tuple(uv) for uv in block.olpl[ref.atts_index])
            if ref.atts_index < len(block.olpl) else ())
        if uvs and len(uvs) != len(polygon):
            raise GeometryClipboardError(
                f"polygon #{poly_id} has {len(uvs)} UVs for "
                f"{len(polygon)} vertices")

        local_indices = []
        for original_index in polygon:
            local_index = original_to_local.get(original_index)
            if local_index is None:
                local_index = len(copied_points)
                original_to_local[original_index] = local_index
                copied_points.append(tuple(model.points[original_index]))
            local_indices.append(local_index)
        entry = block.atts[ref.atts_index]
        copied_polygons.append(GeometryClipboardPolygon(
            source_poly_id=poly_id,
            local_indices=tuple(local_indices),
            block_index=ref.block_index,
            color_val=entry.color_val,
            shade_val=entry.shade_val,
            tracy_val=entry.tracy_val,
            pad=entry.pad,
            uvs=uvs,
            atts_only_mapping=atts_only_animation,
            block_class_id=block_class,
            block_template=(
                copy.deepcopy(block)
                if block_class == "area.class" else None),
        ))

    count = float(len(copied_points))
    pivot = tuple(
        sum(point[axis] for point in copied_points) / count
        for axis in range(3))
    local_points = tuple(
        tuple(point[axis] - pivot[axis] for axis in range(3))
        for point in copied_points)
    return GeometryClipboard(
        owner=owner,
        model_identity=id(model),
        source_name=model.source_name,
        points=tuple(copied_points),
        local_points=local_points,
        pivot=pivot,
        original_to_local=tuple(sorted(original_to_local.items())),
        polygons=tuple(copied_polygons),
    )


def append_geometry_clipboard(model: SkltModel, blocks,
                              clipboard: GeometryClipboard,
                              delta: Point3D) -> TopologyAppendResult:
    """Build then atomically assign one append-only geometry copy."""

    if id(model) != clipboard.model_identity:
        raise GeometryClipboardError(
            "geometry can only be pasted into its source SKLT")
    if not all(math.isfinite(value) for value in delta):
        raise GeometryClipboardError("paste delta contains non-finite values")
    if len(model.points) + len(clipboard.points) > 0x10000:
        raise GeometryClipboardError(
            "paste would exceed the uint16 POO2 index limit")
    if len(model.polygons) + len(clipboard.polygons) > 0x8000:
        raise GeometryClipboardError(
            "paste would exceed the signed ATTS polyID limit")

    first_point = len(model.points)
    new_points = list(model.points)
    for point in clipboard.points:
        new_points.append(tuple(point[axis] + delta[axis]
                                for axis in range(3)))
    new_polygons = list(model.polygons)
    block_values = {
        index: (list(block.atts), [list(group) for group in block.olpl])
        for index, block in enumerate(blocks)
    }
    new_area_blocks: list[AmeshBlock] = []
    polygon_indices = []
    for copied in clipboard.polygons:
        if copied.block_class_id == "area.class":
            block = copy.deepcopy(copied.block_template)
            if block is None \
                    or (block.class_id or "").lower() != "area.class" \
                    or block.ade_strc_chunk_offset < 0 \
                    or not block.source_objt_bytes:
                raise GeometryClipboardError(
                    "clipboard area.class template is no longer writable")
        elif copied.block_class_id == "amesh.class":
            if not (0 <= copied.block_index < len(blocks)):
                raise GeometryClipboardError(
                    f"material block #{copied.block_index} no longer exists")
            block = blocks[copied.block_index]
            if (block.class_id or "").lower() != "amesh.class":
                raise GeometryClipboardError(
                    f"material block #{copied.block_index} is no longer editable")
            atts_only = bool(
                copied.atts_only_mapping
                and block.texture is not None
                and block.texture.kind == "bmpanim"
                and not block.olpl)
            if copied.atts_only_mapping and not atts_only:
                raise GeometryClipboardError(
                    f"material block #{copied.block_index} lost its "
                    "ATTS-only animation semantics")
            if not atts_only and len(block.atts) != len(block.olpl):
                raise GeometryClipboardError(
                    f"material block #{copied.block_index} now has ambiguous "
                    "ATTS/OLPL counts")
        else:
            raise GeometryClipboardError(
                f"clipboard uses unsupported class "
                f"{copied.block_class_id!r}")
        poly_id = len(new_polygons)
        polygon = [first_point + index for index in copied.local_indices]
        if any(index < first_point
               or index >= first_point + len(clipboard.points)
               for index in polygon):
            raise GeometryClipboardError("clipboard polygon index is invalid")
        new_polygons.append(polygon)
        entry = AttsEntry(
            poly_id, copied.color_val, copied.shade_val,
            copied.tracy_val, copied.pad)
        if copied.block_class_id == "area.class":
            block.ade_poly_id = poly_id
            block.atts = [entry]
            block.olpl = (
                [list(copied.uvs)] if copied.uvs else [])
            new_area_blocks.append(block)
        else:
            atts, olpl = block_values[copied.block_index]
            atts.append(entry)
            if not copied.atts_only_mapping:
                olpl.append(list(copied.uvs))
        polygon_indices.append(poly_id)

    model.points[:] = new_points
    model.polygons[:] = new_polygons
    model.parsed_polygon_count = len(new_polygons)
    for index, block in enumerate(blocks):
        atts, olpl = block_values[index]
        block.atts[:] = atts
        block.olpl[:] = olpl
    blocks.extend(new_area_blocks)
    return TopologyAppendResult(
        point_indices=tuple(range(first_point, len(new_points))),
        polygon_indices=tuple(polygon_indices),
    )


def plan_delete_geometry(
        fam_obj: FamilyObject, poly_ids, mapping,
        unsafe_fx_poly_ids=(), *, allow_empty=False) -> GeometryDeletePlan:
    """Validate and build an atomic polygon-delete topology replacement."""

    model = getattr(fam_obj, "skeleton", None)
    blocks = getattr(getattr(fam_obj, "base_object", None), "ades", [])
    selected = tuple(sorted(set(poly_ids)))
    if model is None:
        raise GeometryClipboardError("the selected owner has no editable skeleton")
    if not selected:
        raise GeometryClipboardError("select one or more polygons first")
    if model.poo2_payload_offset is None or model.pol2_payload_offset is None:
        raise GeometryClipboardError(
            "the model has no writable POO2/POL2 structure")
    if model.parsed_polygon_count != len(model.polygons):
        raise GeometryClipboardError(
            "the parsed POL2 count does not match the in-memory polygon list")
    if len(model.points) > 0x10000:
        raise GeometryClipboardError(
            "the POO2 point count exceeds the uint16 index limit")
    if not all(math.isfinite(value)
               for point in model.points for value in point):
        raise GeometryClipboardError(
            "the model contains non-finite POO2 coordinates")
    for poly_id, polygon in enumerate(model.polygons):
        if len(polygon) == 1:
            raise GeometryClipboardError(
                f"polygon #{poly_id} has exactly one vertex")
        if any(index < 0 or index >= len(model.points)
               for index in polygon):
            raise GeometryClipboardError(
                f"polygon #{poly_id} references an invalid POO2 index")
    if any(poly_id < 0 or poly_id >= len(model.polygons)
           for poly_id in selected):
        raise GeometryClipboardError("the polygon selection contains an invalid ID")
    if not allow_empty and len(selected) == len(model.polygons):
        raise GeometryClipboardError(
            "deleting the entire model is not supported")
    if mapping.invalid:
        raise GeometryClipboardError(
            "the model contains invalid ATTS polygon references")
    if mapping.duplicates:
        raise GeometryClipboardError(
            "the model contains duplicate polygon mappings")
    unsafe = set(unsafe_fx_poly_ids)
    if unsafe.intersection(selected):
        raise GeometryClipboardError(
            "the selection contains shared or partially selected FX geometry")

    selected_set = set(selected)
    survivors = [
        (old_id, tuple(polygon))
        for old_id, polygon in enumerate(model.polygons)
        if old_id not in selected_set
    ]
    if not allow_empty and not any(len(polygon) >= 2 for _old_id, polygon in survivors):
        raise GeometryClipboardError(
            "deleting all renderable model geometry is not supported")
    old_to_new_polygon = {
        old_id: new_id
        for new_id, (old_id, _polygon) in enumerate(survivors)
    }
    used_points: set[int] = set()
    for old_id, polygon in survivors:
        for point_index in polygon:
            used_points.add(point_index)

    kept_point_indices = [
        index for index in range(len(model.points)) if index in used_points
    ]
    old_to_new_point = {
        old_id: new_id for new_id, old_id in enumerate(kept_point_indices)
    }
    new_points = tuple(tuple(model.points[index])
                       for index in kept_point_indices)
    new_polygons = tuple(
        tuple(old_to_new_point[index] for index in polygon)
        for _old_id, polygon in survivors
    )

    # Confirm that every writable class also has the decoded source structure
    # required by the symmetric BASE serializer.  Synthetic amesh fixtures may
    # provide chunk offsets without retaining the full source OBJT.
    for block_index, block in enumerate(blocks):
        class_id = (block.class_id or "").lower()
        if class_id == "area.class":
            if block.ade_strc_chunk_offset < 0 or not block.source_objt_bytes:
                raise GeometryClipboardError(
                    f"area.class at {getattr(fam_obj, 'owner_path', 'root')}/"
                    f"ADES[{block_index}] has no writable FORM ADE/STRC source")
            continue
        if class_id == "amesh.class":
            atts_only = block.uses_atts_only_mapping
            if not atts_only and len(block.atts) != len(block.olpl):
                raise GeometryClipboardError(
                    f"material block #{block_index} has ambiguous ATTS/OLPL counts")
            if block.atts_chunk_offset < 0 \
                    or (not atts_only and block.olpl_chunk_offset < 0):
                raise GeometryClipboardError(
                    f"material block #{block_index} has no compatible "
                    "structural mapping chunks")
            for entry_index, entry in enumerate(block.atts):
                if atts_only:
                    continue
                group = block.olpl[entry_index]
                expected_uvs = len(model.polygons[entry.poly_id])
                if len(group) != expected_uvs:
                    raise GeometryClipboardError(
                        f"material block #{block_index} mapping #{entry_index} "
                        f"has {len(group)} UVs for {expected_uvs} vertices")

    try:
        graph = build_polygon_reference_graph(fam_obj)
        reference_plan = plan_polygon_renumber(
            graph, old_to_new_polygon, selected_set)
    except PolygonReferenceError as exc:
        raise GeometryClipboardError(str(exc)) from exc

    return GeometryDeletePlan(
        points=new_points,
        polygons=new_polygons,
        blocks=reference_plan.blocks,
        source_block_classes=tuple(
            (block.class_id or "").lower() for block in blocks),
        deleted_polygon_indices=selected,
        old_to_new_polygon=tuple(sorted(old_to_new_polygon.items())),
        old_to_new_point=tuple(sorted(old_to_new_point.items())),
    )


def apply_delete_geometry(model: SkltModel, blocks,
                          plan: GeometryDeletePlan) -> TopologyDeleteResult:
    """Assign a fully validated delete plan as one in-memory operation."""

    if tuple((block.class_id or "").lower() for block in blocks) \
            != plan.source_block_classes:
        raise GeometryClipboardError(
            "the material block structure changed before Delete")
    model.points[:] = [tuple(point) for point in plan.points]
    model.polygons[:] = [list(polygon) for polygon in plan.polygons]
    model.parsed_polygon_count = len(plan.polygons)
    if len(blocks) == len(plan.blocks) and all(
            (current.class_id or "").lower()
            == (updated.class_id or "").lower()
            for current, updated in zip(blocks, plan.blocks)):
        # Preserve existing MaterialGroup block identities when no ADES OBJT
        # is inserted/removed.
        for current, updated in zip(blocks, plan.blocks):
            current.__dict__.clear()
            current.__dict__.update(copy.deepcopy(updated.__dict__))
    else:
        blocks[:] = [copy.deepcopy(block) for block in plan.blocks]
    return TopologyDeleteResult(
        deleted_polygon_indices=plan.deleted_polygon_indices,
        old_to_new_polygon=plan.old_to_new_polygon,
        old_to_new_point=plan.old_to_new_point,
    )


def unsafe_fx_geometry_polys(fx_elements, owner: str, selected_polys) -> set[int]:
    """Selected polygons that belong to a shared, invalid or partly selected FX element."""

    selected = set(selected_polys)
    unsafe = set()
    for element in fx_elements:
        if element.owner_path != owner:
            continue
        element_polys = set(element.poly_ids)
        if not (selected & element_polys):
            continue
        if element.shared_state == "invalid"                 or (element.bilateral
                    and not element_polys.issubset(selected)):
            unsafe.update(selected & element_polys)
    return unsafe


def build_topology_states(
        data: bytes, fam_obj: FamilyObject,
        animations: Mapping[str, VanmData] | None = None
        ) -> list[StructuralBlockState]:
    """Complete ADES state of an edited model, ready for ``save_model_base_copy``.

    ``data`` is the standalone BASE the edit started from. Each block keeps its exact
    source OBJT, so a grown or shrunk ATTS/OLPL can be written back byte-safely.
    """

    parsed = parse_base_bytes(data, "<saved-model-base>")
    if parsed.root is None:
        raise MappingEditError(
            "the saved BASE snapshot has no parseable root object")
    saved_blocks = parsed.root.ades
    current_blocks = fam_obj.base_object.ades
    model = fam_obj.skeleton
    mapping = MappingIndex(fam_obj)
    if mapping.invalid:
        raise MappingEditError(
            "the in-memory BASE has invalid ATTS polygon references")
    if mapping.duplicates:
        raise MappingEditError(
            "the in-memory BASE has duplicate polygon mappings")
    diagnostics = [
        issue for issue in diagnose_polygon_references(
            fam_obj, detect_fx_elements(fam_obj, animations or {}))
        if "missing typed handler" not in issue
    ]
    if diagnostics:
        raise MappingEditError(
            "the in-memory reference graph is invalid: "
            + "; ".join(diagnostics[:4]))

    def template_signature(block) -> tuple:
        texture = block.texture
        tracy = block.tracy_texture
        return (
            (block.class_id or "").lower(),
            block.payload_form_type,
            block.ade_flags,
            block.ade_point_id,
            block.area_flags,
            block.polflags,
            block.color_val,
            block.tracy_val,
            block.shade_val,
            (
                texture.class_id, texture.kind, texture.name,
                texture.anim_type, tuple(texture.outline_uvs)
            ) if texture is not None else None,
            (
                tracy.class_id, tracy.kind, tracy.name,
                tracy.anim_type, tuple(tracy.outline_uvs)
            ) if tracy is not None else None,
        )

    def template_for(block, block_index: int) -> bytes:
        source = block.source_objt_bytes
        exact = next(
            (saved.source_objt_bytes for saved in saved_blocks
             if source and saved.source_objt_bytes == source), None)
        if exact is not None:
            return exact
        signature = template_signature(block)
        compatible = [
            saved for saved in saved_blocks
            if template_signature(saved) == signature
        ]
        if compatible:
            candidate = (
                saved_blocks[block_index]
                if block_index < len(saved_blocks)
                and saved_blocks[block_index] in compatible
                else compatible[0])
            return candidate.source_objt_bytes
        if source:
            return source
        raise MappingEditError(
            f"block #{block_index} ({block.class_id or 'unknown class'}) "
            "has no parsed source OBJT")

    states = []
    for block_index, current in enumerate(current_blocks):
        class_id = (current.class_id or "").lower()
        atts_only = current.uses_atts_only_mapping
        if class_id == "amesh.class"                 and not atts_only                 and len(current.atts) != len(current.olpl):
            raise MappingEditError(
                f"material block #{block_index} has ambiguous "
                "ATTS/OLPL counts")
        for index, entry in enumerate(current.atts):
            if not (0 <= entry.poly_id < len(model.polygons)):
                raise MappingEditError(
                    f"block #{block_index} mapping #{index} has invalid "
                    f"polyID {entry.poly_id}")
            if atts_only:
                continue
            if index >= len(current.olpl):
                if class_id != "area.class":
                    raise MappingEditError(
                        f"block #{block_index} mapping #{index} has no "
                        "matching OLPL group")
                continue
            uvs = current.olpl[index]
            expected_uvs = len(model.polygons[entry.poly_id])
            if len(uvs) != expected_uvs:
                raise MappingEditError(
                    f"block #{block_index} mapping #{index} has "
                    f"{len(uvs)} UVs for {expected_uvs} vertices")
        states.append(StructuralBlockState(
            block_index=block_index,
            atts=tuple(copy.deepcopy(current.atts)),
            olpl=(
                None if atts_only else tuple(
                    tuple(tuple(uv) for uv in group)
                    for group in current.olpl)),
            class_id=current.class_id,
            payload_form_type=current.payload_form_type,
            ade_poly_id=(
                current.ade_poly_id
                if class_id == "area.class" else None),
            template_objt=template_for(current, block_index),
        ))
    return states


def invert_3x3(matrix: Matrix3) -> Matrix3 | None:
    """Inverse of a 3x3 matrix, or None when singular."""

    (a, b, c), (d, e, f), (g, h, i) = matrix
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if abs(det) < 1e-12:
        return None
    inv_det = 1.0 / det
    return [
        [(e * i - f * h) * inv_det, (c * h - b * i) * inv_det,
         (b * f - c * e) * inv_det],
        [(f * g - d * i) * inv_det, (a * i - c * g) * inv_det,
         (c * d - a * f) * inv_det],
        [(d * h - e * g) * inv_det, (b * g - a * h) * inv_det,
         (a * e - b * d) * inv_det],
    ]


def mat_apply(matrix: Matrix3, vector) -> tuple[float, float, float]:
    x, y, z = vector
    return (
        matrix[0][0] * x + matrix[0][1] * y + matrix[0][2] * z,
        matrix[1][0] * x + matrix[1][1] * y + matrix[1][2] * z,
        matrix[2][0] * x + matrix[2][1] * y + matrix[2][2] * z,
    )


def normalize(vector) -> tuple[float, float, float] | None:
    x, y, z = vector
    length = math.sqrt(x * x + y * y + z * z)
    if length < 1e-12:
        return None
    return (x / length, y / length, z / length)


def rotate_around_axis(point, axis, angle: float, pivot):
    """Rodrigues rotation of ``point`` around ``axis`` through ``pivot``."""

    px = point[0] - pivot[0]
    py = point[1] - pivot[1]
    pz = point[2] - pivot[2]
    ux, uy, uz = axis
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)
    dot = ux * px + uy * py + uz * pz
    cx = uy * pz - uz * py
    cy = uz * px - ux * pz
    cz = ux * py - uy * px
    return (
        pivot[0] + px * cos_a + cx * sin_a + ux * dot * (1.0 - cos_a),
        pivot[1] + py * cos_a + cy * sin_a + uy * dot * (1.0 - cos_a),
        pivot[2] + pz * cos_a + cz * sin_a + uz * dot * (1.0 - cos_a),
    )


AXIS_VECTORS = {
    "X": (1.0, 0.0, 0.0),
    "Y": (0.0, 1.0, 0.0),
    "Z": (0.0, 0.0, 1.0),
}


class GeometryEditSession:
    """Vertex editing state for one skeleton-bearing family object."""

    def __init__(self, fam_obj: FamilyObject, matrix: Matrix3,
                 position: tuple[float, float, float]) -> None:
        if fam_obj.skeleton is None:
            raise ValueError("object has no skeleton to edit")
        self.fam_obj = fam_obj
        self.model: SkltModel = fam_obj.skeleton
        self.matrix = matrix
        self.position = position
        inverse = invert_3x3(matrix)
        self.inv_matrix = inverse if inverse is not None else [
            [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]
        ]
        self.degenerate_transform = inverse is None
        self.selection: set[int] = set()
        self.dirty = False
        self._undo: list[list[Point3D]] = []
        self._redo: list[list[Point3D]] = []
        # Modal preview state
        self._pending: list[Point3D] | None = None
        self._modal_origin: list[Point3D] | None = None
        self._modal_pivot: tuple[float, float, float] | None = None
        self.mirror_enabled = False
        self.mirror_source_selection: set[int] = set()
        self._modal_transform_selection: set[int] | None = None
        self._mirror_match: MirrorMatchResult | None = None
        self._mirror_tolerance = 1e-5

    # -- coordinate helpers ---------------------------------------------------

    def points(self) -> list[Point3D]:
        """Current coordinates: modal preview if active, else committed."""

        return self._pending if self._pending is not None else self.model.points

    def world_points(self) -> list[tuple[float, float, float]]:
        m, p = self.matrix, self.position
        return [
            (
                m[0][0] * x + m[0][1] * y + m[0][2] * z + p[0],
                m[1][0] * x + m[1][1] * y + m[1][2] * z + p[1],
                m[2][0] * x + m[2][1] * y + m[2][2] * z + p[2],
            )
            for x, y, z in self.points()
        ]

    def world_delta_to_model(self, delta) -> tuple[float, float, float]:
        return mat_apply(self.inv_matrix, delta)

    def world_dir_to_model(self, direction) -> tuple[float, float, float] | None:
        return normalize(mat_apply(self.inv_matrix, direction))

    def model_axis_world(self, axis: str) -> tuple[float, float, float] | None:
        """Model axis expressed in world space (for the constraint guide)."""

        return normalize(mat_apply(self.matrix, AXIS_VECTORS[axis]))

    # -- selection --------------------------------------------------------------

    def select_all(self) -> None:
        self.selection = set(range(len(self.model.points)))

    def select_none(self) -> None:
        self.selection = set()
        self.mirror_source_selection.clear()

    def set_selection(self, indices) -> None:
        """Replace the selection with validated POO2 vertex indices."""

        selected = set(indices)
        if any(not isinstance(index, int) for index in selected):
            raise ValueError("vertex selection indices must be integers")
        invalid = sorted(index for index in selected
                         if index < 0 or index >= len(self.model.points))
        if invalid:
            raise ValueError(
                f"vertex selection contains invalid POO2 index {invalid[0]}"
            )
        self.selection = selected
        self.mirror_source_selection.intersection_update(selected)

    def toggle(self, index: int) -> None:
        if index in self.selection:
            self.selection.discard(index)
        else:
            self.selection.add(index)

    def selection_pivot(self) -> tuple[float, float, float] | None:
        """Median-point pivot of the current selection, in model space."""

        return self._selection_pivot(self.selection)

    def _selection_pivot(
            self, indices) -> tuple[float, float, float] | None:
        selected = {
            index for index in indices
            if 0 <= index < len(self.model.points)}
        if not selected:
            return None
        pts = self.model.points
        sel = [pts[i] for i in sorted(selected)]
        if not sel:
            return None
        n = float(len(sel))
        return (sum(p[0] for p in sel) / n,
                sum(p[1] for p in sel) / n,
                sum(p[2] for p in sel) / n)

    # -- modal transforms ---------------------------------------------------------

    @property
    def modal_active(self) -> bool:
        return self._modal_origin is not None

    def modal_origin_points(self) -> list[Point3D] | None:
        return (list(self._modal_origin)
                if self._modal_origin is not None else None)

    def begin_modal(self) -> bool:
        if not self.selection or self.modal_active:
            return False
        self._modal_origin = list(self.model.points)
        self._pending = list(self.model.points)
        sources = (
            self.mirror_source_selection & self.selection
            if self.mirror_enabled else set())
        self._modal_transform_selection = (
            sources if sources else set(self.selection))
        self._modal_pivot = self._selection_pivot(
            self._modal_transform_selection)
        self._mirror_match = None
        return True

    def set_mirror_enabled(self, enabled: bool) -> None:
        self.mirror_enabled = bool(enabled)
        self._mirror_match = None

    def set_mirror_source_selection(self, indices) -> None:
        sources = {
            int(index) for index in indices
            if 0 <= int(index) < len(self.model.points)}
        self.mirror_source_selection = sources & self.selection
        self._mirror_match = None

    def _current_mirror_match(
            self, origin: list[Point3D]) -> MirrorMatchResult:
        if self._mirror_match is None:
            self._mirror_match = match_mirror_points(
                origin, self._modal_transform_selection or (), (0, 1, 2),
                candidates=self.selection)
            self._mirror_tolerance = self._mirror_match.tolerance
        return self._mirror_match

    def _apply_mirrors(self, pending: list[Point3D]) -> None:
        """Propagate the source transform across matching X/Y/Z counterparts."""

        origin = self._modal_origin
        sources = self._modal_transform_selection or set()
        if not self.mirror_enabled or origin is None or not sources:
            return
        propagate_mirror_transform(
            origin, pending, sources, self._current_mirror_match(origin),
            self._mirror_tolerance)

    def _transform_selection(self) -> set[int]:
        return (
            set(self._modal_transform_selection)
            if self._modal_transform_selection is not None
            else set(self.selection))

    def preview_grab(self, delta_model, axis: str | None = None) -> None:
        if self._modal_origin is None:
            return
        dx, dy, dz = delta_model
        if axis == "X":
            dy = dz = 0.0
        elif axis == "Y":
            dx = dz = 0.0
        elif axis == "Z":
            dx = dy = 0.0
        pending = list(self._modal_origin)
        for i in self._transform_selection():
            if i < len(pending):
                x, y, z = self._modal_origin[i]
                pending[i] = (x + dx, y + dy, z + dz)
        self._apply_mirrors(pending)
        self._pending = pending

    def preview_rotate(self, axis_vector, angle: float) -> None:
        if self._modal_origin is None or self._modal_pivot is None:
            return
        axis = normalize(axis_vector)
        if axis is None:
            return
        pending = list(self._modal_origin)
        for i in self._transform_selection():
            if i < len(pending):
                pending[i] = rotate_around_axis(
                    self._modal_origin[i], axis, angle, self._modal_pivot
                )
        self._apply_mirrors(pending)
        self._pending = pending

    def preview_scale(self, factor: float, axis: str | None = None) -> None:
        fx = fy = fz = factor
        if axis == "X":
            fy = fz = 1.0
        elif axis == "Y":
            fx = fz = 1.0
        elif axis == "Z":
            fx = fy = 1.0
        self.preview_scale_axes((fx, fy, fz))

    def preview_scale_axes(self, factors) -> None:
        """Preview a positive model-space scale from the modal origin."""

        if self._modal_origin is None or self._modal_pivot is None:
            return
        try:
            fx, fy, fz = (float(value) for value in factors)
        except (TypeError, ValueError):
            return
        if not all(math.isfinite(value) and value > 0.0
                   for value in (fx, fy, fz)):
            return
        cx, cy, cz = self._modal_pivot
        pending = list(self._modal_origin)
        for i in self._transform_selection():
            if i < len(pending):
                x, y, z = self._modal_origin[i]
                pending[i] = (
                    cx + (x - cx) * fx,
                    cy + (y - cy) * fy,
                    cz + (z - cz) * fz,
                )
        self._apply_mirrors(pending)
        self._pending = pending


    def cancel_modal(self) -> None:
        self._pending = None
        self._modal_origin = None
        self._modal_pivot = None
        self._modal_transform_selection = None
        self._mirror_match = None

    def commit_modal(self) -> bool:
        """Apply the preview; returns True when geometry actually changed."""

        if self._modal_origin is None or self._pending is None:
            self.cancel_modal()
            return False
        changed = self._pending != self._modal_origin
        if changed:
            self._undo.append(list(self._modal_origin))
            del self._undo[:-MAX_UNDO]
            self._redo.clear()
            self.model.points[:] = self._pending
            self.dirty = True
        self.cancel_modal()
        return changed

    # -- history ---------------------------------------------------------------

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo(self) -> bool:
        if self.modal_active or not self._undo:
            return False
        self._redo.append(list(self.model.points))
        self.model.points[:] = self._undo.pop()
        self.dirty = True
        return True

    def redo(self) -> bool:
        if self.modal_active or not self._redo:
            return False
        self._undo.append(list(self.model.points))
        self.model.points[:] = self._redo.pop()
        self.dirty = True
        return True


# ----------------------------------------------------------------------------
# Discovery and structural editing of FX atlas and animated elements.
# (formerly fx_element_editor.py)
# ----------------------------------------------------------------------------

UVBounds = tuple[int, int, int, int]


Normal3 = tuple[float, float, float]


@dataclass(frozen=True)
class FxElement:
    owner_path: str
    fx_name: str
    block_indices: tuple[int, ...]
    atts_indices: tuple[int, ...]
    poly_ids: tuple[int, ...]
    vertex_indices: tuple[int, ...]
    olpl_uvs: tuple[tuple[int, int], ...]
    uv_bounds: UVBounds | None
    uv_source: str
    source_kind: str
    material_names: tuple[str, ...]
    shared_state: str = "exclusive"  # exclusive | bilateral | shared | invalid
    shared_vertices: tuple[int, ...] = ()
    shared_with_polys: tuple[int, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def editable(self) -> bool:
        return self.shared_state in ("exclusive", "bilateral")

    @property
    def bilateral(self) -> bool:
        return self.shared_state == "bilateral"

    @property
    def identity(self) -> tuple:
        return (self.owner_path, self.block_indices,
                self.atts_indices, self.poly_ids)


@dataclass(frozen=True)
class FxElementCapabilities:
    can_move: bool
    can_scale: bool
    can_copy: bool
    can_paste: bool
    can_delete: bool
    can_add_similar: bool
    can_save: bool
    can_overwrite: bool
    can_save_as: bool
    delete_reason: str = ""


@dataclass(frozen=True)
class FxTextureSnapshot:
    class_id: str
    kind: str
    name: str
    anim_type: int | None
    outline_uvs: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class FxAnimationSnapshot:
    source_name: str
    bitmap_class: str
    bitmap_names: tuple[str, ...]
    texcoord_groups: tuple[tuple[tuple[int, int], ...], ...]
    frames: tuple[tuple[int, int, int], ...]


@dataclass(frozen=True)
class FxClipboardPolygon:
    source_poly_id: int
    local_indices: tuple[int, ...]
    block_index: int
    color_val: int
    shade_val: int
    tracy_val: int
    pad: int
    uvs: tuple[tuple[int, int], ...]
    olpl: tuple[tuple[int, int], ...] | None
    texture: FxTextureSnapshot
    block_class_id: str
    block_template: AmeshBlock | None = field(
        default=None, repr=False, compare=False)


@dataclass(frozen=True)
class FxElementClipboard:
    owner: str
    model_identity: int
    source_name: str
    fx_name: str
    source_kind: str
    shared_state: str
    bilateral: bool
    block_indices: tuple[int, ...]
    atts_indices: tuple[int, ...]
    source_poly_ids: tuple[int, ...]
    points: tuple[tuple[float, float, float], ...]
    local_points: tuple[tuple[float, float, float], ...]
    pivot: tuple[float, float, float]
    original_to_local: tuple[tuple[int, int], ...]
    polygons: tuple[FxClipboardPolygon, ...]
    uv_source: str
    material_signature: tuple
    animation: FxAnimationSnapshot | None


def _texture_snapshot(texture: TextureRef | None) -> FxTextureSnapshot:
    if texture is None:
        raise GeometryClipboardError("the FX material has no texture reference")
    return FxTextureSnapshot(
        class_id=texture.class_id,
        kind=texture.kind,
        name=texture.name,
        anim_type=texture.anim_type,
        outline_uvs=tuple(tuple(uv) for uv in texture.outline_uvs),
    )


def _animation_snapshot(
        animations: Mapping[str, VanmData], name: str
        ) -> FxAnimationSnapshot | None:
    animation = _animation_for_name(animations, name)
    if animation is None:
        return None
    return FxAnimationSnapshot(
        source_name=animation.source_name,
        bitmap_class=animation.bitmap_class,
        bitmap_names=tuple(animation.bitmap_names),
        texcoord_groups=tuple(
            tuple(tuple(uv) for uv in group)
            for group in animation.texcoord_groups),
        frames=tuple(
            (frame.frame_time, frame.frame_id, frame.texcoords_id)
            for frame in animation.frames),
    )


def _validate_animation_snapshot(
        snapshot: FxAnimationSnapshot | None, polygon_size: int) -> None:
    if snapshot is None:
        raise GeometryClipboardError(
            "the referenced VANM animation is not decoded")
    if not snapshot.bitmap_names or not snapshot.texcoord_groups \
            or not snapshot.frames:
        raise GeometryClipboardError(
            "the referenced VANM has no complete bitmap/UV/frame data")
    for frame_time, frame_id, texcoords_id in snapshot.frames:
        if frame_time <= 0:
            raise GeometryClipboardError(
                "the referenced VANM contains a non-positive frame duration")
        if not (0 <= frame_id < len(snapshot.bitmap_names)):
            raise GeometryClipboardError(
                "the referenced VANM contains an invalid bitmap index")
        if not (0 <= texcoords_id < len(snapshot.texcoord_groups)):
            raise GeometryClipboardError(
                "the referenced VANM contains an invalid UV-group index")
        if len(snapshot.texcoord_groups[texcoords_id]) < polygon_size:
            raise GeometryClipboardError(
                "the referenced VANM UV group is shorter than the FX polygon")


def build_fx_element_clipboard(
        fam_obj: FamilyObject, element: FxElement,
        animations: Mapping[str, VanmData] | None = None,
        ) -> FxElementClipboard:
    """Copy one complete valid FX element without static conversion."""

    model = fam_obj.skeleton
    if model is None or element.owner_path != fam_obj.owner_path:
        raise GeometryClipboardError("the selected FX owner is not editable")
    if element.shared_state == "invalid":
        raise GeometryClipboardError(
            f"{element.fx_name} is structurally invalid")
    count = len(element.poly_ids)
    if not count or not (
            len(element.block_indices) == count
            == len(element.atts_indices)):
        raise GeometryClipboardError(
            "the FX element has an incomplete material mapping")

    animation_map = animations or {}
    original_to_local: dict[int, int] = {}
    copied_points: list[tuple[float, float, float]] = []
    copied_polygons: list[FxClipboardPolygon] = []
    source_kind = None
    uv_source = None
    animation_snapshot = None
    material_signature = []
    for block_index, atts_index, poly_id in zip(
            element.block_indices, element.atts_indices, element.poly_ids):
        if not (0 <= block_index < len(fam_obj.base_object.ades)):
            raise GeometryClipboardError("the FX material block is invalid")
        block = fam_obj.base_object.ades[block_index]
        block_class = (block.class_id or "").lower()
        if block_class not in ("amesh.class", "area.class") \
                or not (0 <= atts_index < len(block.atts)):
            raise GeometryClipboardError(
                f"the FX uses unsupported class "
                f"{block.class_id or '<missing class>'!r}")
        if block_class == "area.class" and (
                atts_index != 0
                or block.ade_strc_chunk_offset < 0
                or not block.source_objt_bytes):
            raise GeometryClipboardError(
                "the FX area.class has no writable FORM ADE/STRC source")
        entry = block.atts[atts_index]
        if entry.poly_id != poly_id \
                or not (0 <= poly_id < len(model.polygons)):
            raise GeometryClipboardError(
                "the FX ATTS/POL2 association changed")
        polygon = tuple(model.polygons[poly_id])
        if len(polygon) < 3 or len(set(polygon)) != len(polygon) \
                or any(index < 0 or index >= len(model.points)
                       for index in polygon):
            raise GeometryClipboardError("the FX polygon is structurally invalid")
        points = [model.points[index] for index in polygon]
        if not all(math.isfinite(value) for point in points for value in point):
            raise GeometryClipboardError(
                "the FX polygon contains non-finite coordinates")

        source = _rendered_fx(block.texture, animation_map)
        if source is None or source.fx_name != element.fx_name:
            raise GeometryClipboardError(
                "the FX material no longer resolves to its source")
        if source_kind is None:
            source_kind = source.source_kind
        elif source_kind != source.source_kind:
            raise GeometryClipboardError(
                "one FX element mixes direct and VANM materials")
        texture = _texture_snapshot(block.texture)
        current_animation = (
            _animation_snapshot(animation_map, texture.name)
            if source.source_kind == "VANM" else None)
        if source.source_kind == "VANM":
            _validate_animation_snapshot(current_animation, len(polygon))
            if animation_snapshot is None:
                animation_snapshot = current_animation
            elif animation_snapshot != current_animation:
                raise GeometryClipboardError(
                    "one FX element references different VANM timelines")

        if atts_index < len(block.olpl):
            olpl = tuple(tuple(uv) for uv in block.olpl[atts_index])
            if len(olpl) != len(polygon):
                raise GeometryClipboardError(
                    "the FX OLPL does not match its polygon winding")
        elif source.source_kind == "VANM" and not block.olpl:
            olpl = None
        else:
            raise GeometryClipboardError(
                "the FX has no demonstrated OLPL/VANM mapping semantics")
        face_uv_source = (
            "VANM" if source.source_kind == "VANM"
            else "OLPL" if olpl is not None else "none")
        if uv_source is None:
            uv_source = face_uv_source
        elif uv_source != face_uv_source:
            raise GeometryClipboardError(
                "one FX element mixes incompatible UV sources")

        local_indices = []
        for original_index in polygon:
            local_index = original_to_local.get(original_index)
            if local_index is None:
                local_index = len(copied_points)
                original_to_local[original_index] = local_index
                copied_points.append(tuple(model.points[original_index]))
            local_indices.append(local_index)
        copied_polygons.append(FxClipboardPolygon(
            source_poly_id=poly_id,
            local_indices=tuple(local_indices),
            block_index=block_index,
            color_val=entry.color_val,
            shade_val=entry.shade_val,
            tracy_val=entry.tracy_val,
            pad=entry.pad,
            uvs=olpl or (),
            olpl=olpl,
            texture=texture,
            block_class_id=block_class,
            block_template=(
                copy.deepcopy(block)
                if block_class == "area.class" else None),
        ))
        material_signature.append((block_index, texture))

    point_count = float(len(copied_points))
    pivot = tuple(
        sum(point[axis] for point in copied_points) / point_count
        for axis in range(3))
    local_points = tuple(
        tuple(point[axis] - pivot[axis] for axis in range(3))
        for point in copied_points)
    return FxElementClipboard(
        owner=fam_obj.owner_path,
        model_identity=id(model),
        source_name=model.source_name,
        fx_name=element.fx_name,
        source_kind=source_kind or element.source_kind,
        shared_state=(
            "bilateral" if element.bilateral
            else "selection" if element.shared_state == "selection"
            else "exclusive"),
        bilateral=element.bilateral,
        block_indices=tuple(element.block_indices),
        atts_indices=tuple(element.atts_indices),
        source_poly_ids=tuple(element.poly_ids),
        points=tuple(copied_points),
        local_points=local_points,
        pivot=pivot,
        original_to_local=tuple(sorted(original_to_local.items())),
        polygons=tuple(copied_polygons),
        uv_source=uv_source or element.uv_source,
        material_signature=tuple(material_signature),
        animation=animation_snapshot,
    )


def build_fx_elements_clipboard(
        fam_obj: FamilyObject, elements,
        animations: Mapping[str, VanmData] | None = None,
        ) -> FxElementClipboard:
    """Copy one or more complete FX elements as one mirrored selection."""

    selected = tuple(elements)
    if not selected:
        raise GeometryClipboardError("select one or more complete FX elements")
    if len(selected) == 1:
        return build_fx_element_clipboard(
            fam_obj, selected[0], animations)
    owner = fam_obj.owner_path
    if any(element.owner_path != owner for element in selected):
        raise GeometryClipboardError(
            "the selected FX elements do not share one editable owner")
    if any(element.shared_state == "invalid" for element in selected):
        raise GeometryClipboardError(
            "the mirrored FX selection contains invalid geometry")
    poly_ids = [
        poly_id for element in selected for poly_id in element.poly_ids]
    if len(set(poly_ids)) != len(poly_ids):
        raise GeometryClipboardError(
            "the mirrored FX selection contains overlapping elements")
    fx_names = {element.fx_name for element in selected}
    if len(fx_names) != 1:
        raise GeometryClipboardError(
            "FX1 and FX2 cannot be combined in one Paste FX Preview")

    bounds = [
        element.uv_bounds for element in selected
        if element.uv_bounds is not None]
    combined = FxElement(
        owner_path=owner,
        fx_name=selected[0].fx_name,
        block_indices=tuple(
            index for element in selected for index in element.block_indices),
        atts_indices=tuple(
            index for element in selected for index in element.atts_indices),
        poly_ids=tuple(poly_ids),
        vertex_indices=tuple(sorted({
            index for element in selected for index in element.vertex_indices
        })),
        olpl_uvs=selected[0].olpl_uvs,
        uv_bounds=(
            (min(bound[0] for bound in bounds),
             min(bound[1] for bound in bounds),
             max(bound[2] for bound in bounds),
             max(bound[3] for bound in bounds))
            if bounds else None),
        uv_source=selected[0].uv_source,
        source_kind=selected[0].source_kind,
        material_names=tuple(
            name for element in selected for name in element.material_names),
        shared_state="selection",
        warnings=tuple(dict.fromkeys(
            warning for element in selected for warning in element.warnings)),
    )
    return build_fx_element_clipboard(fam_obj, combined, animations)


def detach_shared_fx_vertices(
        fam_obj: FamilyObject, element: FxElement) -> int:
    """Duplicate only FX vertices also used outside the selected FX element."""

    model = fam_obj.skeleton
    if model is None or element.owner_path != fam_obj.owner_path:
        raise GeometryClipboardError("the selected FX owner is not editable")
    if element.shared_state != "shared":
        return 0
    selected = set(element.poly_ids)
    if not selected or any(
            poly_id < 0 or poly_id >= len(model.polygons)
            for poly_id in selected):
        raise GeometryClipboardError("the shared FX polygon set is invalid")
    users: dict[int, set[int]] = {}
    for poly_id, polygon in enumerate(model.polygons):
        for vertex in set(polygon):
            users.setdefault(vertex, set()).add(poly_id)
    shared = {
        vertex for poly_id in selected for vertex in model.polygons[poly_id]
        if users.get(vertex, set()) - selected
    }
    if not shared:
        return 0
    if len(model.points) + len(shared) > 0x10000:
        raise GeometryClipboardError(
            "detaching the shared FX exceeds the POO2 index limit")
    replacements = {}
    for vertex in sorted(shared):
        if not (0 <= vertex < len(model.points)):
            raise GeometryClipboardError(
                "the shared FX references an invalid POO2 vertex")
        replacements[vertex] = len(model.points)
        model.points.append(tuple(model.points[vertex]))
    for poly_id in selected:
        model.polygons[poly_id] = [
            replacements.get(vertex, vertex)
            for vertex in model.polygons[poly_id]]
    return len(replacements)


def validate_fx_element_clipboard(
        fam_obj: FamilyObject, clipboard: FxElementClipboard,
        animations: Mapping[str, VanmData] | None = None,
        ) -> None:
    """Validate the typed FX target without changing any live list."""

    model = fam_obj.skeleton
    blocks = fam_obj.base_object.ades
    if clipboard.fx_name not in ("FX1", "FX2", "FX3", "VANM") \
            or clipboard.source_kind not in ("direct", "VANM"):
        raise GeometryClipboardError("the FX clipboard type is invalid")
    if clipboard.shared_state not in (
            "exclusive", "bilateral", "selection") \
            or clipboard.bilateral != (
                clipboard.shared_state == "bilateral"):
        raise GeometryClipboardError("the FX clipboard grouping is invalid")
    if not clipboard.points or not clipboard.polygons:
        raise GeometryClipboardError("the FX clipboard is empty")
    if (clipboard.shared_state == "exclusive"
            and len(clipboard.polygons) != 1) \
            or (clipboard.shared_state == "bilateral"
                and len(clipboard.polygons) != 2) \
            or (clipboard.shared_state == "selection"
                and len(clipboard.polygons) < 2):
        raise GeometryClipboardError(
            "the FX clipboard polygon grouping is invalid")
    if tuple(
            copied.block_index for copied in clipboard.polygons
            ) != clipboard.block_indices \
            or tuple(
                copied.source_poly_id for copied in clipboard.polygons
            ) != clipboard.source_poly_ids \
            or len(clipboard.atts_indices) != len(clipboard.polygons):
        raise GeometryClipboardError(
            "the FX clipboard source mapping is inconsistent")
    if len(clipboard.local_points) != len(clipboard.points) \
            or any(
                abs(
                    clipboard.local_points[index][axis]
                    + clipboard.pivot[axis]
                    - point[axis]) > 1e-9
                for index, point in enumerate(clipboard.points)
                for axis in range(3)):
        raise GeometryClipboardError(
            "the FX clipboard pivot coordinates are inconsistent")
    if any(
            original < 0
            or local < 0 or local >= len(clipboard.points)
            for original, local in clipboard.original_to_local):
        raise GeometryClipboardError(
            "the FX clipboard vertex mapping is invalid")
    expected_signature = tuple(
        (copied.block_index, copied.texture)
        for copied in clipboard.polygons)
    if clipboard.material_signature != expected_signature:
        raise GeometryClipboardError(
            "the FX clipboard material signature is invalid")
    if model is None or id(model) != clipboard.model_identity \
            or fam_obj.owner_path != clipboard.owner:
        raise GeometryClipboardError(
            "FX can only be pasted into its source model")
    if len(model.points) + len(clipboard.points) > 0x10000 \
            or len(model.polygons) + len(clipboard.polygons) > 0x8000:
        raise GeometryClipboardError("FX paste exceeds the model index limits")
    animation_map = animations or {}
    for copied in clipboard.polygons:
        if len(copied.local_indices) < 3 \
                or len(set(copied.local_indices)) \
                != len(copied.local_indices):
            raise GeometryClipboardError(
                "the FX clipboard polygon is structurally invalid")
        if copied.uvs != (copied.olpl or ()):
            raise GeometryClipboardError(
                "the FX clipboard UV snapshot is inconsistent")
        if copied.block_class_id == "area.class":
            block = copied.block_template
            if block is None \
                    or (block.class_id or "").lower() != "area.class" \
                    or block.ade_strc_chunk_offset < 0 \
                    or not block.source_objt_bytes \
                    or _texture_snapshot(block.texture) != copied.texture:
                raise GeometryClipboardError(
                    "the FX area.class template changed after Copy")
        elif copied.block_class_id == "amesh.class":
            if not (0 <= copied.block_index < len(blocks)):
                raise GeometryClipboardError("the FX material block was removed")
            block = blocks[copied.block_index]
            if (block.class_id or "").lower() != "amesh.class" \
                    or _texture_snapshot(block.texture) != copied.texture:
                raise GeometryClipboardError(
                    "the FX material changed after Copy")
        else:
            raise GeometryClipboardError(
                f"the FX clipboard uses unsupported class "
                f"{copied.block_class_id!r}")
        if clipboard.source_kind == "VANM":
            if copied.texture.kind != "bmpanim":
                raise GeometryClipboardError(
                    "the VANM FX clipboard lost its bmpanim material")
            current = _animation_snapshot(
                animation_map, copied.texture.name)
            if current != clipboard.animation:
                raise GeometryClipboardError(
                    "the FX VANM timeline changed after Copy")
            _validate_animation_snapshot(current, len(copied.local_indices))
        elif copied.texture.kind == "bmpanim" \
                or normalize_fx_name(copied.texture.name) \
                != clipboard.fx_name:
            raise GeometryClipboardError(
                "the direct FX clipboard material is inconsistent")
        if copied.olpl is None:
            if copied.texture.kind != "bmpanim" or block.olpl:
                raise GeometryClipboardError(
                    "the target no longer has ATTS-only VANM semantics")
        elif copied.block_class_id == "amesh.class" \
                and len(block.atts) != len(block.olpl):
            raise GeometryClipboardError(
                "the target FX ATTS/OLPL mapping is ambiguous")
        if any(local_index < 0 or local_index >= len(clipboard.points)
               for local_index in copied.local_indices):
            raise GeometryClipboardError(
                "the FX clipboard is structurally invalid")


def append_fx_element_clipboard(
        fam_obj: FamilyObject, clipboard: FxElementClipboard,
        delta: tuple[float, float, float],
        animations: Mapping[str, VanmData] | None = None,
        ) -> TopologyAppendResult:
    """Append one FX clipboard while retaining its typed material semantics."""

    validate_fx_element_clipboard(fam_obj, clipboard, animations)
    model = fam_obj.skeleton
    blocks = fam_obj.base_object.ades
    if not all(math.isfinite(value) for value in delta):
        raise GeometryClipboardError("FX paste delta is not finite")
    animation_map = animations or {}
    first_point = len(model.points)
    new_points = list(model.points)
    new_points.extend(
        tuple(point[axis] + delta[axis] for axis in range(3))
        for point in clipboard.points)
    new_polygons = [list(polygon) for polygon in model.polygons]
    block_values = {
        index: (list(block.atts), [list(group) for group in block.olpl])
        for index, block in enumerate(blocks)
    }
    new_area_blocks: list[AmeshBlock] = []
    polygon_indices = []
    for copied in clipboard.polygons:
        if copied.block_class_id == "area.class":
            block = copy.deepcopy(copied.block_template)
            if block is None:
                raise GeometryClipboardError(
                    "the FX area.class template is unavailable")
        else:
            if not (0 <= copied.block_index < len(blocks)):
                raise GeometryClipboardError("the FX material block was removed")
            block = blocks[copied.block_index]
            if (block.class_id or "").lower() != "amesh.class" \
                    or _texture_snapshot(block.texture) != copied.texture:
                raise GeometryClipboardError(
                    "the FX material changed after Copy")
        if clipboard.source_kind == "VANM":
            current = _animation_snapshot(
                animation_map, copied.texture.name)
            if current != clipboard.animation:
                raise GeometryClipboardError(
                    "the FX VANM timeline changed after Copy")
            _validate_animation_snapshot(current, len(copied.local_indices))
        polygon = [
            first_point + local_index
            for local_index in copied.local_indices
        ]
        if any(index < first_point
               or index >= first_point + len(clipboard.points)
               for index in polygon):
            raise GeometryClipboardError("the FX clipboard is structurally invalid")
        poly_id = len(new_polygons)
        new_polygons.append(polygon)
        entry = AttsEntry(
            poly_id, copied.color_val, copied.shade_val,
            copied.tracy_val, copied.pad)
        if copied.block_class_id == "area.class":
            block.ade_poly_id = poly_id
            block.atts = [entry]
            block.olpl = (
                [list(copied.olpl)] if copied.olpl is not None else [])
            new_area_blocks.append(block)
        else:
            atts, olpl = block_values[copied.block_index]
            if copied.olpl is None:
                if copied.texture.kind != "bmpanim" or olpl:
                    raise GeometryClipboardError(
                        "the target no longer has ATTS-only VANM semantics")
            elif len(atts) != len(olpl):
                raise GeometryClipboardError(
                    "the target FX ATTS/OLPL mapping is ambiguous")
            atts.append(entry)
            if copied.olpl is not None:
                olpl.append(list(copied.olpl))
        polygon_indices.append(poly_id)

    model.points[:] = new_points
    model.polygons[:] = new_polygons
    model.parsed_polygon_count = len(new_polygons)
    for index, block in enumerate(blocks):
        atts, olpl = block_values[index]
        block.atts[:] = atts
        block.olpl[:] = olpl
    blocks.extend(new_area_blocks)
    return TopologyAppendResult(
        point_indices=tuple(range(first_point, len(new_points))),
        polygon_indices=tuple(polygon_indices),
    )


@dataclass(frozen=True)
class _FxSource:
    fx_name: str
    animation: VanmData | None
    source_kind: str
    source_key: str


@dataclass(frozen=True)
class _FxFace:
    fx_name: str
    block_index: int
    atts_index: int
    poly_id: int
    vertex_indices: tuple[int, ...]
    olpl_uvs: tuple[tuple[int, int], ...]
    uv_bounds: UVBounds | None
    uv_source: str
    source_kind: str
    source_signature: tuple
    material_name: str
    normal: Normal3 | None
    warnings: tuple[str, ...]
    valid: bool = True


def normalize_fx_name(name: str | None) -> str | None:
    """Return the FX atlas identifier for a matching logical resource name."""

    if not name:
        return None
    basename = str(name).strip().replace("\\", "/").rsplit("/", 1)[-1]
    stem = basename.rsplit(".", 1)[0] if "." in basename else basename
    logical = stem.upper()
    return logical if logical in ("FX1", "FX2", "FX3") else None


def _logical_key(name: str) -> str:
    return name.replace("\\", "/").rsplit("/", 1)[-1].lower()


def _animation_for_name(animations: Mapping[str, VanmData],
                        name: str) -> VanmData | None:
    animation = animations.get(name)
    if animation is not None:
        return animation
    normalized = name.replace("\\", "/").lower()
    return next(
        (value for key, value in animations.items()
         if key.replace("\\", "/").lower() == normalized),
        None,
    )


def _rendered_fx(ref: TextureRef | None,
                 animations: Mapping[str, VanmData]) -> _FxSource | None:
    """Resolve FX used by the diffuse material rendered by the viewport."""

    if ref is None or not ref.name:
        return None
    if ref.kind == "bmpanim":
        animation = _animation_for_name(animations, ref.name)
        if animation is None:
            return _FxSource("VANM", None, "VANM", _logical_key(ref.name))
        for bitmap_name in animation.bitmap_names:
            fx_name = normalize_fx_name(bitmap_name)
            if fx_name is not None:
                return _FxSource(
                    fx_name, animation, "VANM", _logical_key(ref.name))
        return _FxSource("VANM", animation, "VANM", _logical_key(ref.name))
    direct = normalize_fx_name(ref.name)
    return (
        _FxSource(direct, None, "direct", direct)
        if direct is not None else None)


def _uv_bounds(groups) -> UVBounds | None:
    points = [point for group in groups for point in group]
    if not points:
        return None
    us = [u for u, _v in points]
    vs = [v for _u, v in points]
    return min(us), min(vs), max(us), max(vs)


def _polygon_normal(points, polygon) -> Normal3 | None:
    if len(polygon) < 3:
        return None
    coords = [points[index] for index in polygon]
    nx = ny = nz = 0.0
    for index, (x0, y0, z0) in enumerate(coords):
        x1, y1, z1 = coords[(index + 1) % len(coords)]
        nx += (y0 - y1) * (z0 + z1)
        ny += (z0 - z1) * (x0 + x1)
        nz += (x0 - x1) * (y0 + y1)
    length = sqrt(nx * nx + ny * ny + nz * nz)
    if length < 1e-9:
        return None
    return nx / length, ny / length, nz / length


def _normals_confirm_bilateral(faces: list[_FxFace]) -> bool:
    """Use reliable normals as confirmation without making them mandatory."""

    normals = [face.normal for face in faces if face.normal is not None]
    if len(normals) < 2:
        return True
    reference = normals[0]
    dots = [sum(a * b for a, b in zip(reference, normal))
            for normal in normals[1:]]
    return (all(abs(dot) >= 0.95 for dot in dots)
            and any(dot <= -0.95 for dot in dots))


def _uv_data(block: AmeshBlock, atts_index: int,
             source: _FxSource) -> tuple[
                 tuple[tuple[int, int], ...], UVBounds | None, str, tuple,
                 tuple[str, ...]]:
    olpl = (tuple(block.olpl[atts_index])
            if atts_index < len(block.olpl) else ())
    warnings: list[str] = []

    animation = source.animation
    if animation is not None and animation.frames \
            and animation.texcoord_groups:
        groups = animation.texcoord_groups
        uv_source = "VANM"
        warnings.append("OLPL absent or unused; preview UVs come from VANM.")
        signature = (
            "VANM", source.source_key,
            tuple(_logical_key(name) for name in animation.bitmap_names),
            tuple((frame.frame_time, frame.frame_id, frame.texcoords_id)
                  for frame in animation.frames),
            tuple(tuple(group) for group in animation.texcoord_groups),
        )
    elif olpl:
        groups = [list(olpl)]
        uv_source = "OLPL"
        # Vertex winding may reverse on a legitimate back face.
        signature = (source.source_kind, source.source_key,
                     tuple(sorted(olpl)))
    else:
        groups = []
        uv_source = "none"
        signature = (source.source_kind, source.source_key, ())
        warnings.append("No OLPL or VANM UV coordinates are available.")
    return olpl, _uv_bounds(groups), uv_source, signature, tuple(warnings)


def _merge_warnings(faces: list[_FxFace]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        warning for face in faces for warning in face.warnings))


def _group_element(owner_path: str, faces: list[_FxFace]) -> FxElement:
    ordered = sorted(faces, key=lambda face:
                     (face.poly_id, face.block_index, face.atts_index))
    bounds = [face.uv_bounds for face in ordered if face.uv_bounds is not None]
    if bounds:
        uv_bounds = (
            min(bound[0] for bound in bounds), min(bound[1] for bound in bounds),
            max(bound[2] for bound in bounds), max(bound[3] for bound in bounds),
        )
    else:
        uv_bounds = None
    return FxElement(
        owner_path=owner_path,
        fx_name=ordered[0].fx_name,
        block_indices=tuple(face.block_index for face in ordered),
        atts_indices=tuple(face.atts_index for face in ordered),
        poly_ids=tuple(face.poly_id for face in ordered),
        vertex_indices=tuple(sorted(set(ordered[0].vertex_indices))),
        olpl_uvs=ordered[0].olpl_uvs,
        uv_bounds=uv_bounds,
        uv_source=ordered[0].uv_source,
        source_kind=ordered[0].source_kind,
        material_names=tuple(face.material_name for face in ordered),
        shared_state="bilateral",
        warnings=_merge_warnings(ordered),
    )


def _single_element(owner_path: str, face: _FxFace,
                    vertex_users: dict[int, set[int]],
                    mapping_counts: dict[int, int]) -> FxElement:
    if not face.valid:
        state = "invalid"
        shared_vertices = ()
        shared_with = ()
    else:
        shared_vertices = tuple(sorted(
            index for index in set(face.vertex_indices)
            if vertex_users.get(index, set()) - {face.poly_id}
        ))
        shared_with = tuple(sorted({
            other_poly
            for index in shared_vertices
            for other_poly in vertex_users.get(index, set())
            if other_poly != face.poly_id
        }))
        ambiguous_mapping = mapping_counts.get(face.poly_id, 0) != 1
        state = "shared" if shared_vertices or ambiguous_mapping else "exclusive"
    warnings = list(face.warnings)
    if face.valid and mapping_counts.get(face.poly_id, 0) != 1:
        warnings.append("POL2 has multiple ATTS material mappings.")
    return FxElement(
        owner_path=owner_path,
        fx_name=face.fx_name,
        block_indices=(face.block_index,),
        atts_indices=(face.atts_index,),
        poly_ids=(face.poly_id,),
        vertex_indices=face.vertex_indices,
        olpl_uvs=face.olpl_uvs,
        uv_bounds=face.uv_bounds,
        uv_source=face.uv_source,
        source_kind=face.source_kind,
        material_names=(face.material_name,),
        shared_state=state,
        shared_vertices=shared_vertices,
        shared_with_polys=shared_with,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def detect_fx_elements(
        fam_obj: FamilyObject,
        animations: Mapping[str, VanmData] | None = None,
) -> list[FxElement]:
    """Return single or safely grouped FX1/FX2 elements for one owner.

    ``tracy_texture`` is intentionally not considered: the current viewport
    does not render mapped-tracy second textures, so treating one as a visible
    FX element would disagree with the preview.
    """

    skeleton = fam_obj.skeleton
    if skeleton is None:
        return []
    animation_map = animations or {}

    vertex_users: dict[int, set[int]] = {}
    for poly_id, polygon in enumerate(skeleton.polygons):
        for vertex_index in set(polygon):
            vertex_users.setdefault(vertex_index, set()).add(poly_id)

    mapping_counts: dict[int, int] = {}
    for block in fam_obj.base_object.ades:
        for entry in block.atts:
            if 0 <= entry.poly_id < len(skeleton.polygons):
                mapping_counts[entry.poly_id] = \
                    mapping_counts.get(entry.poly_id, 0) + 1

    faces: list[_FxFace] = []
    for block_index, block in enumerate(fam_obj.base_object.ades):
        source = _rendered_fx(block.texture, animation_map)
        if source is None:
            continue
        for atts_index, entry in enumerate(block.atts):
            olpl, bounds, uv_source, signature, warnings = _uv_data(
                block, atts_index, source)
            warning_list = list(warnings)
            valid = 0 <= entry.poly_id < len(skeleton.polygons)
            if valid:
                vertex_indices = tuple(skeleton.polygons[entry.poly_id])
                valid = bool(vertex_indices) and all(
                    0 <= index < len(skeleton.points)
                    for index in vertex_indices)
            else:
                vertex_indices = ()
            if not valid:
                warning_list.append(
                    f"ATTS entry references invalid polyID {entry.poly_id}.")
                vertex_indices = ()
            elif olpl and len(olpl) != len(vertex_indices):
                warning_list.append(
                    f"OLPL has {len(olpl)} UVs for "
                    f"{len(vertex_indices)} polygon vertices.")
            faces.append(_FxFace(
                fx_name=source.fx_name,
                block_index=block_index,
                atts_index=atts_index,
                poly_id=entry.poly_id,
                vertex_indices=vertex_indices,
                olpl_uvs=olpl,
                uv_bounds=bounds,
                uv_source=uv_source,
                source_kind=source.source_kind,
                source_signature=signature,
                material_name=block.texture.name if block.texture else "",
                normal=(_polygon_normal(skeleton.points, vertex_indices)
                        if valid else None),
                warnings=tuple(warning_list),
                valid=valid,
            ))

    candidates: dict[tuple, list[_FxFace]] = {}
    for face in faces:
        if not face.valid or len(set(face.vertex_indices)) != len(face.vertex_indices):
            continue
        # Two sides remain one geometric FX when their UVs are edited
        # independently. UV coordinates are mutable, not element identity.
        key = (face.fx_name, frozenset(face.vertex_indices),
               (face.source_kind, _logical_key(face.material_name)))
        candidates.setdefault(key, []).append(face)

    grouped: set[tuple[int, int, int]] = set()
    elements: list[FxElement] = []
    for (_fx_name, vertex_set, _signature), group in candidates.items():
        poly_ids = {face.poly_id for face in group}
        if len(poly_ids) != 2 or len(group) != 2:
            continue
        external_users = set().union(
            *(vertex_users.get(index, set()) for index in vertex_set)
        ) - poly_ids
        unambiguous_mappings = all(
            mapping_counts.get(poly_id, 0) == 1 for poly_id in poly_ids)
        if external_users or not unambiguous_mappings \
                or not _normals_confirm_bilateral(group):
            continue
        elements.append(_group_element(fam_obj.owner_path, group))
        grouped.update((face.block_index, face.atts_index, face.poly_id)
                       for face in group)

    for face in faces:
        key = (face.block_index, face.atts_index, face.poly_id)
        if key not in grouped:
            elements.append(_single_element(
                fam_obj.owner_path, face, vertex_users, mapping_counts))

    return sorted(
        elements,
        key=lambda element: (
            min(element.poly_ids) if element.poly_ids else 1 << 30,
            element.block_indices, element.atts_indices,
        ),
    )


# ----------------------------------------------------------------------------
# FX authoring through the existing geometry and material clipboard writers.
# (formerly fx_authoring.py)
# ----------------------------------------------------------------------------

@dataclass(frozen=True)
class ImportedFxClipboard(FxElementClipboard):
    materials: tuple[MaterialBlockClipboard, ...] = ()


def prepare_fx_clipboard(target, source_family, source, element):
    """Capture all source data now; opening/cancelling a preview changes nothing."""
    clipboard = build_fx_element_clipboard(source, element, source_family.animations)
    materials = tuple(
        build_material_block_clipboard(source_family, source, index)
        for index in dict.fromkeys(clipboard.block_indices))
    values = dict(clipboard.__dict__)
    values.update(owner=target.owner_path, model_identity=id(target.skeleton),
                  source_poly_ids=tuple(-1 for _ in clipboard.polygons),
                  original_to_local=(),
                  polygons=tuple(replace(face, source_poly_id=-1)
                                 for face in clipboard.polygons))
    return ImportedFxClipboard(**values, materials=materials)


def stage_fx_clipboard(family, target, clipboard, delta=(0.0, 0.0, 0.0)):
    """Build and validate a complete result without touching the target family."""
    if clipboard.owner != target.owner_path \
            or clipboard.model_identity != id(target.skeleton):
        raise GeometryClipboardError("the FX target changed during preview")
    staged_family = copy.copy(family)
    for name in ("textures", "texture_refs", "animations", "animation_refs"):
        setattr(staged_family, name, dict(getattr(family, name)))
    staged = copy.copy(target)
    staged.skeleton = copy.deepcopy(target.skeleton)
    staged.base_object = copy.copy(target.base_object)
    staged.base_object.ades = copy.deepcopy(target.base_object.ades)
    block_indices = {}
    for material in clipboard.materials:
        try:
            validate_retail_polygon_flags(material.block_template.polflags)
        except UnsupportedIndexedMaterialError as exc:
            raise GeometryClipboardError(str(exc)) from exc
        if material.source_family_identity == id(family):
            material = replace(material, source_family_identity=id(staged_family))
        if material.class_id == "area.class":
            transfer_material_resources(staged_family, material)
            # AREA carries its own source OBJT and is appended per polygon by
            # the typed FX writer, rather than creating an empty AREA slot.
            block_indices[material.source_block_index] = material.source_block_index
        else:
            result = paste_material_block(staged_family, staged, material)
            block_indices[material.source_block_index] = result.block_index
    faces = tuple(replace(face, block_index=block_indices[face.block_index])
                  for face in clipboard.polygons)
    values = {name: getattr(clipboard, name)
              for name in FxElementClipboard.__dataclass_fields__}
    values.update(model_identity=id(staged.skeleton), polygons=faces,
                  block_indices=tuple(face.block_index for face in faces),
                  material_signature=tuple((face.block_index, face.texture)
                                           for face in faces))
    local = FxElementClipboard(**values)
    result = append_fx_element_clipboard(
        staged, local, delta, staged_family.animations)
    return staged_family, staged, result
