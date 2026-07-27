"""Conservative discovery and grouping of FX1/FX2 geometry elements.

Discovery is read-only.  It follows material/VANM -> ATTS -> POL2 -> POO2
and groups coincident FX faces only when every use of their vertices belongs
to one deterministic, compatible FX group.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
import math
from math import sqrt
from typing import Mapping

from anm_parser import VanmData
from asset_family import FamilyObject
from base_parser import AmeshBlock, AttsEntry, TextureRef
from geometry_editor import GeometryClipboardError, TopologyAppendResult


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
    can_cut: bool
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
    """Copy one complete editable FX element without static conversion."""

    model = fam_obj.skeleton
    if model is None or element.owner_path != fam_obj.owner_path:
        raise GeometryClipboardError("the selected FX owner is not editable")
    if element.shared_state not in ("exclusive", "bilateral"):
        raise GeometryClipboardError(
            f"{element.fx_name} is {element.shared_state} and is read-only")
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
                "the FX material no longer resolves to FX1/FX2")
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
        shared_state=element.shared_state,
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


def build_new_fx_clipboard(
        fam_obj: FamilyObject, template: FxElement,
        animations: Mapping[str, VanmData] | None,
        size: float, bilateral: bool,
        orientation: str = "XY") -> FxElementClipboard:
    """Create a quad preview from one existing, verified FX material."""

    source = build_fx_element_clipboard(fam_obj, template, animations)
    if not math.isfinite(size) or size <= 0.0:
        raise GeometryClipboardError("FX size must be a positive finite value")
    template_polygon = source.polygons[0]
    if template_polygon.olpl is not None \
            and len(template_polygon.olpl) != 4:
        raise GeometryClipboardError(
            "the chosen direct FX material has no demonstrated quad UV group")
    if source.animation is not None:
        _validate_animation_snapshot(source.animation, 4)
    half = size * 0.5
    xy_points = (
        (-half, -half, 0.0),
        (half, -half, 0.0),
        (half, half, 0.0),
        (-half, half, 0.0),
    )
    orientation = orientation.upper()
    if orientation == "XY":
        points = xy_points
    elif orientation == "XZ":
        points = tuple((x, 0.0, y) for x, y, _z in xy_points)
    elif orientation == "YZ":
        points = tuple((0.0, x, y) for x, y, _z in xy_points)
    else:
        raise GeometryClipboardError(
            f"unsupported FX orientation {orientation!r}")
    first = replace(
        template_polygon,
        source_poly_id=-1,
        local_indices=(0, 1, 2, 3))
    polygons = [first]
    if bilateral:
        reversed_olpl = (
            tuple(reversed(first.olpl)) if first.olpl is not None else None)
        polygons.append(replace(
            first,
            local_indices=(3, 2, 1, 0),
            uvs=reversed_olpl or (),
            olpl=reversed_olpl))
    return replace(
        source,
        shared_state="bilateral" if bilateral else "exclusive",
        bilateral=bilateral,
        block_indices=tuple(
            polygon.block_index for polygon in polygons),
        atts_indices=tuple(-1 for _polygon in polygons),
        source_poly_ids=tuple(-1 for _polygon in polygons),
        points=points,
        local_points=points,
        pivot=(0.0, 0.0, 0.0),
        original_to_local=(),
        polygons=tuple(polygons),
        material_signature=tuple(
            (polygon.block_index, polygon.texture)
            for polygon in polygons),
    )


def validate_fx_element_clipboard(
        fam_obj: FamilyObject, clipboard: FxElementClipboard,
        animations: Mapping[str, VanmData] | None = None,
        ) -> None:
    """Validate the typed FX target without changing any live list."""

    model = fam_obj.skeleton
    blocks = fam_obj.base_object.ades
    if clipboard.fx_name not in ("FX1", "FX2") \
            or clipboard.source_kind not in ("direct", "VANM"):
        raise GeometryClipboardError("the FX clipboard type is invalid")
    if clipboard.shared_state not in ("exclusive", "bilateral") \
            or clipboard.bilateral != (
                clipboard.shared_state == "bilateral"):
        raise GeometryClipboardError("the FX clipboard grouping is invalid")
    if not clipboard.points or not clipboard.polygons:
        raise GeometryClipboardError("the FX clipboard is empty")
    if clipboard.bilateral != (len(clipboard.polygons) == 2) \
            or (not clipboard.bilateral and len(clipboard.polygons) != 1):
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
    """Return ``FX1``/``FX2`` for a matching logical resource name."""

    if not name:
        return None
    basename = str(name).strip().replace("\\", "/").rsplit("/", 1)[-1]
    stem = basename.rsplit(".", 1)[0] if "." in basename else basename
    logical = stem.upper()
    return logical if logical in ("FX1", "FX2") else None


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
            return None
        for bitmap_name in animation.bitmap_names:
            fx_name = normalize_fx_name(bitmap_name)
            if fx_name is not None:
                return _FxSource(
                    fx_name, animation, "VANM", _logical_key(ref.name))
        return None
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
        key = (face.fx_name, frozenset(face.vertex_indices),
               face.source_signature)
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
