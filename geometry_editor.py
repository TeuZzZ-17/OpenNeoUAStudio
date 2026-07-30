"""Geometry editing state and safe geometry clipboard helpers.

``GeometryEditSession`` holds vertex selection, modal transform previews and
coordinate-only history for one FamilyObject.  The immutable geometry
clipboard helpers validate and append POO2/POL2 plus matching ATTS/OLPL data;
window-level topology snapshots own undo/redo for those structural changes.

The session works entirely in *model space* (raw POO2 coordinates).  The
viewport converts screen input into model-space deltas and calls the
preview_* methods; committing writes ``model.points`` in place so every
other panel (stats, save, reload of the same family) sees the edit.

Normal transforms still change only vertex positions and retain the fixed-size
``save_sklt_with_poo2_points`` path.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
import math

from asset_family import FamilyObject
from base_parser import AmeshBlock, AttsEntry
from polygon_reference_graph import (
    PolygonReferenceError,
    build_polygon_reference_graph,
    plan_polygon_renumber,
)
from sklt_parser import Point3D, SkltModel

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


def _polygon_area_measure(points) -> float:
    """Squared Newell-normal length; zero means a degenerate polygon."""

    nx = ny = nz = 0.0
    for index, (x0, y0, z0) in enumerate(points):
        x1, y1, z1 = points[(index + 1) % len(points)]
        nx += (y0 - y1) * (z0 + z1)
        ny += (z0 - z1) * (x0 + x1)
        nz += (x0 - x1) * (y0 + y1)
    return nx * nx + ny * ny + nz * nz


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
        unsafe_fx_poly_ids=()) -> GeometryDeletePlan:
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
    if len(selected) == len(model.polygons):
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
    if not any(len(polygon) >= 2 for _old_id, polygon in survivors):
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
            atts_only = bool(
                block.texture is not None
                and block.texture.kind == "bmpanim"
                and not block.olpl)
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
        self._mirror_components: list[tuple[int, ...]] | None = None
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

    def toggle(self, index: int) -> None:
        if index in self.selection:
            self.selection.discard(index)
        else:
            self.selection.add(index)

    def selection_pivot(self) -> tuple[float, float, float] | None:
        """Median-point pivot of the current selection, in model space."""

        if not self.selection:
            return None
        pts = self.model.points
        sel = [pts[i] for i in sorted(self.selection) if i < len(pts)]
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
        self._modal_pivot = self.selection_pivot()
        self._mirror_components = None
        return True

    def set_mirror_enabled(self, enabled: bool) -> None:
        self.mirror_enabled = bool(enabled)
        self._mirror_components = None

    def _enabled_mirror_axes(self) -> tuple[int, ...]:
        return (0, 1, 2) if self.mirror_enabled else ()

    def _build_mirror_components(
            self, origin: list[Point3D],
            axes: tuple[int, ...]) -> list[tuple[int, ...]]:
        """Build one-to-one symmetry orbits for the active coordinate planes."""

        extent = max(
            max(point[axis] for point in origin)
            - min(point[axis] for point in origin)
            for axis in range(3))
        tolerance = max(1e-5, extent * 1e-5)
        self._mirror_tolerance = tolerance
        adjacency = {index: set() for index in range(len(origin))}
        for axis in axes:
            positives = [
                index for index, point in enumerate(origin)
                if point[axis] > tolerance]
            negatives = {
                index for index, point in enumerate(origin)
                if point[axis] < -tolerance}
            for positive in positives:
                point = origin[positive]
                candidates = [
                    negative for negative in negatives
                    if all(
                        abs(
                            origin[negative][coordinate]
                            - (-point[coordinate]
                               if coordinate == axis
                               else point[coordinate]))
                        <= tolerance
                        for coordinate in range(3))
                ]
                if not candidates:
                    continue
                negative = min(candidates, key=lambda index: (
                    sum(
                        abs(
                            origin[index][coordinate]
                            - (-point[coordinate]
                               if coordinate == axis
                               else point[coordinate]))
                        for coordinate in range(3)),
                    index))
                negatives.remove(negative)
                adjacency[positive].add(negative)
                adjacency[negative].add(positive)

        components = []
        visited = set()
        for start in range(len(origin)):
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
                pending.extend(adjacency[index] - visited)
            components.append(tuple(sorted(component)))
        return components

    def _apply_mirrors(self, pending: list[Point3D]) -> None:
        """Propagate transformed vertices across every enabled symmetry plane."""

        origin = self._modal_origin
        axes = self._enabled_mirror_axes()
        if origin is None or not origin or not axes:
            return
        if self._mirror_components is None:
            self._mirror_components = self._build_mirror_components(
                origin, axes)
        tolerance = self._mirror_tolerance
        for component in self._mirror_components:
            selected = [
                index for index in component if index in self.selection]
            if not selected:
                continue
            source = min(selected, key=lambda index: (
                tuple(
                    0 if origin[index][axis] > tolerance
                    else 1 if abs(origin[index][axis]) <= tolerance
                    else 2
                    for axis in axes),
                index,
            ))
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
        for index in self.selection:
            if index >= len(origin):
                continue
            values = list(pending[index])
            for axis in axes:
                if abs(origin[index][axis]) <= tolerance:
                    values[axis] = 0.0
            pending[index] = tuple(values)

    def mirror_recipient_indices(self) -> set[int]:
        """Unselected vertices currently receiving a mirrored modal edit."""

        origin = self._modal_origin
        axes = self._enabled_mirror_axes()
        if origin is None or not origin or not axes:
            return set()
        if self._mirror_components is None:
            self._mirror_components = self._build_mirror_components(
                origin, axes)
        recipients = set()
        for component in self._mirror_components:
            if any(index in self.selection for index in component):
                recipients.update(
                    index for index in component
                    if index not in self.selection)
        return recipients

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
        for i in self.selection:
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
        for i in self.selection:
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
        for i in self.selection:
            if i < len(pending):
                x, y, z = self._modal_origin[i]
                pending[i] = (
                    cx + (x - cx) * fx,
                    cy + (y - cy) * fy,
                    cz + (z - cz) * fz,
                )
        self._apply_mirrors(pending)
        self._pending = pending

    def preview_replace_selected(self, points) -> None:
        """Replace selected coordinates during a normal undoable modal edit."""

        if self._modal_origin is None:
            return
        indices = sorted(self.selection)
        replacements = list(points)
        if len(indices) != len(replacements):
            raise ValueError(
                "replacement point count must match the vertex selection")
        pending = list(self._modal_origin)
        for index, point in zip(indices, replacements):
            if index < len(pending):
                pending[index] = tuple(float(value) for value in point)
        self._pending = pending

    def cancel_modal(self) -> None:
        self._pending = None
        self._modal_origin = None
        self._modal_pivot = None
        self._mirror_components = None

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
