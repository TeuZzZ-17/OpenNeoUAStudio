"""Conservative discovery and grouping of FX1/FX2 geometry elements.

Discovery is read-only.  It follows material/VANM -> ATTS -> POL2 -> POO2
and groups coincident FX faces only when every use of their vertices belongs
to one deterministic, compatible FX group.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import Mapping

from anm_parser import VanmData
from asset_family import FamilyObject
from base_parser import AmeshBlock, TextureRef


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
    direct = normalize_fx_name(ref.name)
    if direct is not None:
        return _FxSource(direct, None, "direct", direct)
    if ref.kind != "bmpanim":
        return None
    animation = _animation_for_name(animations, ref.name)
    if animation is None:
        return None
    for bitmap_name in animation.bitmap_names:
        fx_name = normalize_fx_name(bitmap_name)
        if fx_name is not None:
            return _FxSource(
                fx_name, animation, "VANM", _logical_key(ref.name))
    return None


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
        if len(poly_ids) < 2 or len(group) != len(poly_ids):
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
