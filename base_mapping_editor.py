"""Polygon Mapping Workbench core: mapping index, repair plans, safe writer.

Repairs exactly one class of defect: a skeleton POL2 polygon that no amesh
ATTS entry maps (an "ATTS coverage hole" — the polygon is invisible
in-game).  Known base-game cases: ST_FLAK1 #77, ST_FLAK2 #9, ST_NSTR2 #24,
ST_ENDL5 #82.

A repair appends ONE 6-byte ATTS entry and ONE matching OLPL group
(s16 uvCount + uvCount * (u8 u, u8 v), uvCount == polygon vertex count) to a
user-chosen amesh block.  Both formats are CONFIRMED against the OpenNeoUA
runtime (amesh.cpp); the appended deltas are always even (6 bytes, and
2 + 2*n bytes), so IFF chunk padding never changes — the writer only splices
the new bytes at the end of the two chunk payloads and bumps the big-endian
size of every enclosing chunk.

Safety model:
- the original file is never written; callers save to a NEW path;
- the writer re-parses its own output and refuses to return bytes that do
  not verify (one extra ATTS entry, one extra OLPL group, all other blocks
  byte-comparable, chunk tree shape unchanged);
- particle.class ATTS (different payload) and HUD OLPL (different layout)
  are never touched: only amesh blocks with recorded chunk offsets are
  eligible targets.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import struct

from base_parser import AmeshBlock, parse_base_bytes
from iff_reader import read_iff_bytes


class MappingEditError(Exception):
    pass


# --- mapping index ----------------------------------------------------------------


@dataclass
class MappingRef:
    block_index: int
    atts_index: int
    block: AmeshBlock


class MappingIndex:
    """polyID -> mapping refs + status for one family object."""

    def __init__(self, fam_obj):
        self.fam_obj = fam_obj
        self.poly_count = (fam_obj.skeleton.parsed_polygon_count
                           if fam_obj.skeleton else 0)
        self.refs: dict[int, list[MappingRef]] = {}
        self.invalid: list[tuple[int, int, int]] = []  # (block, atts_idx, polyID)

        for block_index, block in enumerate(fam_obj.base_object.ades):
            for atts_index, entry in enumerate(block.atts):
                if 0 <= entry.poly_id < self.poly_count:
                    self.refs.setdefault(entry.poly_id, []).append(
                        MappingRef(block_index, atts_index, block)
                    )
                else:
                    self.invalid.append(
                        (block_index, atts_index, entry.poly_id)
                    )

    @property
    def unmapped(self) -> list[int]:
        return [p for p in range(self.poly_count) if p not in self.refs]

    @property
    def duplicates(self) -> dict[int, int]:
        return {p: len(r) for p, r in self.refs.items() if len(r) > 1}

    def status(self, poly_id: int) -> str:
        if not (0 <= poly_id < self.poly_count):
            return "invalid"
        refs = self.refs.get(poly_id, [])
        if not refs:
            return "unmapped"
        if len(refs) > 1:
            return "duplicate"
        return "mapped"


# --- repair plans -----------------------------------------------------------------


@dataclass
class RepairPlan:
    poly_id: int
    block_index: int
    color_val: int = 0
    shade_val: int = 0
    tracy_val: int = 128
    pad: int = 0
    uvs: list[tuple[int, int]] = field(default_factory=list)
    method: str = ""          # "copy-style" | "planar"
    source_poly: int | None = None
    notes: list[str] = field(default_factory=list)

    def describe(self) -> list[str]:
        lines = [
            f"repair polygon #{self.poly_id} -> material block "
            f"#{self.block_index} ({self.method})",
            f"ATTS entry: polyID={self.poly_id} colorVal={self.color_val} "
            f"shadeVal={self.shade_val} tracyVal={self.tracy_val} "
            f"pad={self.pad}",
            f"OLPL group ({len(self.uvs)} UVs): "
            + " ".join(f"({u},{v})" for u, v in self.uvs),
        ]
        lines.extend(self.notes)
        return lines


@dataclass(frozen=True)
class StructuralBlockState:
    """Complete typed state for one ADES block.

    ``template_objt`` is the exact parsed source OBJT used by the grow/shrink
    writer.  ``None`` retains compatibility with the older amesh-only API.
    """

    block_index: int
    atts: tuple
    # None means that this material uses ATTS-only mapping semantics (for
    # example a demonstrated VANM block) and its OLPL chunk must be preserved.
    olpl: tuple | None
    class_id: str = ""
    payload_form_type: str = ""
    ade_poly_id: int | None = None
    template_objt: bytes | None = field(
        default=None, repr=False, compare=False)


@dataclass(frozen=True)
class MaterialResourceSnapshot:
    """One decoded dependency carried by the structural material clipboard."""

    logical_name: str
    resource_kind: str             # "texture" | "animation"
    semantic_signature: tuple
    value: object = field(repr=False, compare=False)
    reference: object | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class MaterialBlockClipboard:
    """Dependency-aware snapshot of one writable ADES material block.

    AMESH mappings are deliberately not pasted: copying those polyIDs into a
    different model would create duplicate or unrelated references.  AREA has
    one typed polygon reference and can therefore be remapped, but only to an
    explicit compatible, currently-unmapped target polygon.
    """

    source_family_identity: int
    source_owner: str
    source_block_index: int
    class_id: str
    block_template: object = field(repr=False, compare=False)
    area_vertex_count: int | None = None
    resources: tuple[MaterialResourceSnapshot, ...] = ()
    indexed_profile_signature: tuple[tuple[str, str], ...] | None = None


@dataclass(frozen=True)
class MaterialPasteResult:
    block_index: int
    assigned_poly_id: int | None
    imported_resources: tuple[tuple[str, str], ...] = ()


def _unique_casefold_item(mapping: dict, logical_name: str, kind: str):
    matches = [
        (key, value) for key, value in mapping.items()
        if str(key).casefold() == str(logical_name).casefold()
    ]
    if len(matches) > 1:
        raise MappingEditError(
            f"{kind} dependency {logical_name!r} is ambiguous in the "
            "loaded AssetFamily")
    return matches[0] if matches else None


def _texture_semantic_signature(image) -> tuple:
    return (
        str(getattr(image, "kind", "")),
        int(getattr(image, "width", 0)),
        int(getattr(image, "height", 0)),
        int(getattr(image, "n_planes", 0)),
        int(getattr(image, "masking", 0)),
        int(getattr(image, "compression", 0)),
        int(getattr(image, "transparent_color", 0)),
        tuple(tuple(rgb) for rgb in (getattr(image, "palette", None) or ())),
        bytes(getattr(image, "pixels", None) or b""),
    )


def _animation_semantic_signature(animation) -> tuple:
    return (
        bool(getattr(animation, "has_form", False)),
        str(getattr(animation, "bitmap_class", "")),
        tuple(str(name) for name in getattr(animation, "bitmap_names", ())),
        tuple(
            tuple(tuple(uv) for uv in group)
            for group in getattr(animation, "texcoord_groups", ())),
        tuple(
            (
                int(getattr(frame, "frame_time", 0)),
                int(getattr(frame, "frame_id", 0)),
                int(getattr(frame, "texcoords_id", 0)),
            )
            for frame in getattr(animation, "frames", ())),
    )


def _indexed_profile_signature(family) \
        -> tuple[tuple[str, str], ...] | None:
    refs = (
        ("palette", getattr(family, "external_palette_ref", None)),
        ("shader", getattr(family, "indexed_profile_refs", {}).get("shader")),
        ("tracy", getattr(family, "indexed_profile_refs", {}).get("tracy")),
    )
    result = []
    for label, ref in refs:
        path = getattr(ref, "path", None)
        if path is None or not Path(path).is_file():
            return None
        try:
            digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        except OSError:
            return None
        result.append((label, digest))
    return tuple(result)


def _material_resources(family, block) \
        -> tuple[MaterialResourceSnapshot, ...]:
    snapshots: dict[tuple[str, str], MaterialResourceSnapshot] = {}

    def add_texture(logical_name: str) -> None:
        key = ("texture", logical_name.casefold())
        if key in snapshots:
            return
        match = _unique_casefold_item(
            getattr(family, "textures", {}), logical_name, "texture")
        if match is None:
            raise MappingEditError(
                f"texture dependency {logical_name!r} is not decoded")
        canonical, image = match
        ref_match = _unique_casefold_item(
            getattr(family, "texture_refs", {}), logical_name,
            "texture provenance")
        snapshots[key] = MaterialResourceSnapshot(
            logical_name=str(canonical),
            resource_kind="texture",
            semantic_signature=_texture_semantic_signature(image),
            value=copy.deepcopy(image),
            reference=(copy.deepcopy(ref_match[1])
                       if ref_match is not None else None),
        )

    def add_animation(logical_name: str) -> None:
        key = ("animation", logical_name.casefold())
        if key in snapshots:
            return
        match = _unique_casefold_item(
            getattr(family, "animations", {}), logical_name, "animation")
        if match is None:
            raise MappingEditError(
                f"animation dependency {logical_name!r} is not decoded")
        canonical, animation = match
        ref_match = _unique_casefold_item(
            getattr(family, "animation_refs", {}), logical_name,
            "animation provenance")
        snapshots[key] = MaterialResourceSnapshot(
            logical_name=str(canonical),
            resource_kind="animation",
            semantic_signature=_animation_semantic_signature(animation),
            value=copy.deepcopy(animation),
            reference=(copy.deepcopy(ref_match[1])
                       if ref_match is not None else None),
        )
        for bitmap_name in getattr(animation, "bitmap_names", ()):
            add_texture(str(bitmap_name))

    for descriptor in (
            getattr(block, "texture", None),
            getattr(block, "tracy_texture", None)):
        name = str(getattr(descriptor, "name", "") or "")
        if not name:
            continue
        if str(getattr(descriptor, "kind", "")).casefold() == "bmpanim":
            add_animation(name)
        else:
            add_texture(name)
    return tuple(
        snapshots[key] for key in sorted(snapshots))


def build_material_block_clipboard(
        family, fam_obj, block_index: int) -> MaterialBlockClipboard:
    """Validate and snapshot one AMESH/AREA block plus its dependencies."""

    blocks = getattr(getattr(fam_obj, "base_object", None), "ades", ())
    model = getattr(fam_obj, "skeleton", None)
    if not (0 <= int(block_index) < len(blocks)):
        raise MappingEditError("select a valid material block first")
    block = blocks[int(block_index)]
    class_id = (getattr(block, "class_id", "") or "").lower()
    if class_id not in ("amesh.class", "area.class"):
        raise MappingEditError(
            f"class {block.class_id or '<missing class>'!r} has no safe "
            "structural material clipboard handler")
    if not getattr(block, "source_objt_bytes", b""):
        raise MappingEditError(
            f"material block #{block_index} has no exact source OBJT")
    if model is None:
        raise MappingEditError("the selected material owner has no skeleton")

    area_vertex_count = None
    if class_id == "amesh.class":
        atts_only = bool(
            getattr(block, "texture", None) is not None
            and block.texture.kind == "bmpanim" and not block.olpl)
        if not atts_only and len(block.atts) != len(block.olpl):
            raise MappingEditError(
                f"material block #{block_index} has ambiguous ATTS/OLPL "
                "counts")
        if len({entry.poly_id for entry in block.atts}) != len(block.atts):
            raise MappingEditError(
                f"material block #{block_index} contains duplicate polyIDs")
        for entry_index, entry in enumerate(block.atts):
            if not (0 <= entry.poly_id < len(model.polygons)):
                raise MappingEditError(
                    f"material block #{block_index} has invalid polyID "
                    f"{entry.poly_id}")
            if not atts_only and len(block.olpl[entry_index]) \
                    != len(model.polygons[entry.poly_id]):
                raise MappingEditError(
                    f"material block #{block_index} UV count does not match "
                    f"polygon #{entry.poly_id}")
    else:
        poly_id = int(block.ade_poly_id)
        if getattr(block, "ade_strc_chunk_offset", -1) < 0:
            raise MappingEditError(
                f"AREA block #{block_index} has no writable ADE/STRC")
        if not (0 <= poly_id < len(model.polygons)) \
                or len(block.atts) != 1 \
                or block.atts[0].poly_id != poly_id:
            raise MappingEditError(
                f"AREA block #{block_index} has an inconsistent POL2 mapping")
        area_vertex_count = len(model.polygons[poly_id])
        if block.olpl and (
                len(block.olpl) != 1
                or len(block.olpl[0]) != area_vertex_count):
            raise MappingEditError(
                f"AREA block #{block_index} has incompatible OLPL data")

    resources = _material_resources(family, block)
    return MaterialBlockClipboard(
        source_family_identity=id(family),
        source_owner=str(getattr(fam_obj, "owner_path", "root")),
        source_block_index=int(block_index),
        class_id=class_id,
        block_template=copy.deepcopy(block),
        area_vertex_count=area_vertex_count,
        resources=resources,
        indexed_profile_signature=(
            _indexed_profile_signature(family) if resources else None),
    )


def _plan_material_resource_transfer(
        family, clipboard: MaterialBlockClipboard
        ) -> list[tuple[MaterialResourceSnapshot, str]]:
    if clipboard.resources and id(family) != clipboard.source_family_identity:
        source_profile = clipboard.indexed_profile_signature
        target_profile = _indexed_profile_signature(family)
        if source_profile is None or target_profile is None:
            raise MappingEditError(
                "cross-family material paste requires complete palette, "
                "SHADERMP and TRACYRMP provenance on both families")
        if source_profile != target_profile:
            raise MappingEditError(
                "cross-family material paste refused: the SET palette/remap "
                "profiles differ")

    additions = []
    for snapshot in clipboard.resources:
        mapping_name = (
            "textures" if snapshot.resource_kind == "texture"
            else "animations")
        mapping = getattr(family, mapping_name)
        match = _unique_casefold_item(
            mapping, snapshot.logical_name, snapshot.resource_kind)
        if match is not None:
            current_signature = (
                _texture_semantic_signature(match[1])
                if snapshot.resource_kind == "texture"
                else _animation_semantic_signature(match[1]))
            if current_signature != snapshot.semantic_signature:
                raise MappingEditError(
                    f"{snapshot.resource_kind} collision for "
                    f"{snapshot.logical_name!r}: target content differs")
            continue
        additions.append((snapshot, mapping_name))
    return additions


def paste_material_block(
        family, fam_obj, clipboard: MaterialBlockClipboard,
        *, target_poly_id: int | None = None) -> MaterialPasteResult:
    """Append a safe material slot or remapped AREA block atomically."""

    if not isinstance(clipboard, MaterialBlockClipboard):
        raise MappingEditError("the material clipboard is invalid")
    model = getattr(fam_obj, "skeleton", None)
    blocks = getattr(getattr(fam_obj, "base_object", None), "ades", None)
    if model is None or blocks is None:
        raise MappingEditError("the target owner has no editable model")
    block = copy.deepcopy(clipboard.block_template)
    class_id = (getattr(block, "class_id", "") or "").lower()
    if class_id != clipboard.class_id \
            or class_id not in ("amesh.class", "area.class") \
            or not getattr(block, "source_objt_bytes", b""):
        raise MappingEditError("the material clipboard template is invalid")

    assigned_poly_id = None
    if class_id == "amesh.class":
        if target_poly_id is not None:
            raise MappingEditError(
                "AMESH paste creates an empty material slot; map polygons "
                "explicitly with the Mapping Repair tools")
        block.atts = []
        block.olpl = []
    else:
        if target_poly_id is None:
            raise MappingEditError(
                "AREA paste requires one explicit unmapped target polygon")
        target_poly_id = int(target_poly_id)
        if not (0 <= target_poly_id < len(model.polygons)):
            raise MappingEditError("the AREA target polygon is invalid")
        mapping = MappingIndex(fam_obj)
        if mapping.status(target_poly_id) != "unmapped":
            raise MappingEditError(
                f"AREA target polygon #{target_poly_id} is already mapped")
        if len(model.polygons[target_poly_id]) \
                != clipboard.area_vertex_count:
            raise MappingEditError(
                "AREA target polygon vertex count differs from the copied "
                "material")
        if len(block.atts) != 1:
            raise MappingEditError("the copied AREA mapping is inconsistent")
        source_entry = block.atts[0]
        block.ade_poly_id = target_poly_id
        block.atts = [type(source_entry)(
            target_poly_id, source_entry.color_val, source_entry.shade_val,
            source_entry.tracy_val, source_entry.pad)]
        if block.olpl:
            block.olpl = [copy.deepcopy(block.olpl[0])]
        assigned_poly_id = target_poly_id

    additions = _plan_material_resource_transfer(family, clipboard)
    imported = []
    for snapshot, mapping_name in additions:
        getattr(family, mapping_name)[snapshot.logical_name] = copy.deepcopy(
            snapshot.value)
        refs_name = (
            "texture_refs" if snapshot.resource_kind == "texture"
            else "animation_refs")
        if snapshot.reference is not None:
            getattr(family, refs_name)[snapshot.logical_name] = copy.deepcopy(
                snapshot.reference)
        imported.append((snapshot.resource_kind, snapshot.logical_name))
    block_index = len(blocks)
    blocks.append(block)
    return MaterialPasteResult(
        block_index=block_index,
        assigned_poly_id=assigned_poly_id,
        imported_resources=tuple(imported),
    )


def delete_material_block(fam_obj, block_index: int):
    """Delete one typed AMESH/AREA block without touching POL2 geometry."""

    blocks = getattr(getattr(fam_obj, "base_object", None), "ades", None)
    if blocks is None or not (0 <= int(block_index) < len(blocks)):
        raise MappingEditError("select a valid material block first")
    block = blocks[int(block_index)]
    class_id = (getattr(block, "class_id", "") or "").lower()
    if class_id not in ("amesh.class", "area.class"):
        raise MappingEditError(
            f"class {block.class_id or '<missing class>'!r} has no safe "
            "structural delete handler")
    if not getattr(block, "source_objt_bytes", b""):
        raise MappingEditError(
            f"material block #{block_index} has no exact source OBJT")
    return blocks.pop(int(block_index))


def _polygon_vertices(fam_obj, poly_id: int) -> list[tuple[float, float, float]]:
    skeleton = fam_obj.skeleton
    if skeleton is None or not (0 <= poly_id < len(skeleton.polygons)):
        raise MappingEditError(f"polygon #{poly_id} not available")
    return [skeleton.points[i] for i in skeleton.polygons[poly_id]]


def planar_uvs(points: list[tuple[float, float, float]],
               lo: int = 16, hi: int = 240) -> list[tuple[int, int]]:
    """Simple planar projection: drop the dominant normal axis, normalise the
    remaining two coordinates into texture-space bytes [lo, hi]."""

    if len(points) < 3:
        raise MappingEditError("planar UVs need at least 3 vertices")

    # Newell normal
    nx = ny = nz = 0.0
    for i, (x0, y0, z0) in enumerate(points):
        x1, y1, z1 = points[(i + 1) % len(points)]
        nx += (y0 - y1) * (z0 + z1)
        ny += (z0 - z1) * (x0 + x1)
        nz += (x0 - x1) * (y0 + y1)
    dominant = max(range(3), key=lambda i: abs((nx, ny, nz)[i]))
    axes = [i for i in range(3) if i != dominant]

    us = [p[axes[0]] for p in points]
    vs = [p[axes[1]] for p in points]
    du = (max(us) - min(us)) or 1.0
    dv = (max(vs) - min(vs)) or 1.0
    span = hi - lo
    return [
        (lo + round((u - min(us)) / du * span),
         lo + round((v - min(vs)) / dv * span))
        for u, v in zip(us, vs)
    ]


def plan_planar(fam_obj, poly_id: int, block_index: int,
                mapping: MappingIndex) -> RepairPlan:
    """Assign the polygon to a block with planar-projected UVs."""

    block = _eligible_block(fam_obj, block_index)
    points = _polygon_vertices(fam_obj, poly_id)
    plan = RepairPlan(poly_id=poly_id, block_index=block_index,
                      method="planar", uvs=planar_uvs(points))
    _default_atts_values(plan, block)
    plan.notes.append(
        "UVs are a planar projection of the polygon bounds (preview and "
        "adjust in-game if needed)."
    )
    return plan


def plan_copy_style(fam_obj, poly_id: int, source_poly: int,
                    mapping: MappingIndex) -> RepairPlan:
    """Copy material block, ATTS values and UV pattern from a mapped polygon."""

    refs = mapping.refs.get(source_poly)
    if not refs:
        raise MappingEditError(
            f"source polygon #{source_poly} has no mapping to copy"
        )
    ref = refs[0]
    block = _eligible_block(fam_obj, ref.block_index)
    entry = block.atts[ref.atts_index]

    plan = RepairPlan(poly_id=poly_id, block_index=ref.block_index,
                      method="copy-style", source_poly=source_poly,
                      color_val=entry.color_val, shade_val=entry.shade_val,
                      tracy_val=entry.tracy_val)

    target_points = _polygon_vertices(fam_obj, poly_id)
    source_uvs = (block.olpl[ref.atts_index]
                  if ref.atts_index < len(block.olpl) else [])
    if source_uvs and len(source_uvs) == len(target_points):
        plan.uvs = list(source_uvs)
        plan.notes.append(
            f"UV pattern copied from polygon #{source_poly} "
            "(same vertex count)."
        )
    else:
        plan.uvs = planar_uvs(target_points)
        plan.notes.append(
            f"vertex counts differ from #{source_poly} "
            f"({len(source_uvs)} vs {len(target_points)}): "
            "planar UVs generated instead."
        )
    return plan


def _default_atts_values(plan: RepairPlan, block: AmeshBlock) -> None:
    """Sensible ATTS defaults: mimic the block's existing entries."""

    if block.atts:
        entry = block.atts[0]
        plan.color_val = entry.color_val
        plan.shade_val = entry.shade_val
        plan.tracy_val = entry.tracy_val
        plan.notes.append(
            f"colorVal/shadeVal/tracyVal copied from the block's first entry."
        )


def _eligible_block(fam_obj, block_index: int) -> AmeshBlock:
    ades = fam_obj.base_object.ades
    if not (0 <= block_index < len(ades)):
        raise MappingEditError(f"material block #{block_index} does not exist")
    block = ades[block_index]
    if (block.class_id or "").lower() != "amesh.class":
        raise MappingEditError(
            f"block #{block_index} is {block.class_id!r}: only amesh.class "
            "blocks can receive new ATTS/OLPL entries"
        )
    if block.atts_chunk_offset < 0 or block.olpl_chunk_offset < 0:
        raise MappingEditError(
            f"block #{block_index} has no recorded ATTS/OLPL chunk offsets"
        )
    return block


def eligible_blocks(fam_obj) -> list[tuple[int, AmeshBlock]]:
    result = []
    for index, block in enumerate(fam_obj.base_object.ades):
        if (block.class_id or "").lower() == "amesh.class" \
                and block.atts_chunk_offset >= 0 \
                and block.olpl_chunk_offset >= 0:
            result.append((index, block))
    return result


# --- safe writer -------------------------------------------------------------------


def _pack_atts_entry(plan: RepairPlan) -> bytes:
    return struct.pack(">hBBBB", plan.poly_id, plan.color_val & 0xFF,
                       plan.shade_val & 0xFF, plan.tracy_val & 0xFF,
                       plan.pad & 0xFF)


def _pack_olpl_group(plan: RepairPlan) -> bytes:
    blob = struct.pack(">h", len(plan.uvs))
    for u, v in plan.uvs:
        blob += struct.pack(">BB", u & 0xFF, v & 0xFF)
    return blob


def apply_repair_to_bytes(data: bytes, block: AmeshBlock,
                          plan: RepairPlan) -> bytes:
    """Splice one ATTS entry + one OLPL group into a .base byte image."""

    if not plan.uvs:
        raise MappingEditError("the repair plan has no UVs")
    atts_blob = _pack_atts_entry(plan)
    olpl_blob = _pack_olpl_group(plan)

    tree = read_iff_bytes(data)
    atts_chunk = None
    olpl_chunk = None
    for chunk in tree.iter_all():
        if chunk.offset == block.atts_chunk_offset and chunk.tag == "ATTS":
            atts_chunk = chunk
        elif chunk.offset == block.olpl_chunk_offset and chunk.tag == "OLPL":
            olpl_chunk = chunk
    if atts_chunk is None or olpl_chunk is None:
        raise MappingEditError(
            "target ATTS/OLPL chunks not found at the recorded offsets "
            "(file changed since parsing?)"
        )

    insertions = [
        (atts_chunk.payload_offset + atts_chunk.size, atts_blob),
        (olpl_chunk.payload_offset + olpl_chunk.size, olpl_blob),
    ]

    new_data = bytearray(data)
    for pos, blob in sorted(insertions, key=lambda x: x[0], reverse=True):
        new_data[pos:pos] = blob

    # Bump the size field of every chunk whose payload received an insertion
    # (the chunk itself and all enclosing FORMs).  Deltas are even, so IFF
    # padding never changes.
    for chunk in tree.iter_all():
        payload_start = chunk.payload_offset
        payload_end = chunk.payload_offset + chunk.size
        delta = sum(len(blob) for pos, blob in insertions
                    if payload_start <= pos <= payload_end)
        if not delta:
            continue
        shift = sum(len(blob) for pos, blob in insertions
                    if pos <= chunk.offset)
        struct.pack_into(">I", new_data, chunk.offset + 4 + shift,
                         chunk.size + delta)

    return bytes(new_data)


def verify_repair(original: bytes, repaired: bytes, block_index: int,
                  plan: RepairPlan) -> list[str]:
    """Re-parse the output and prove the edit is exactly the intended one.
    Returns a list of verification notes; raises on any mismatch."""

    notes: list[str] = []
    before = parse_base_bytes(original, "<original>")
    after = parse_base_bytes(repaired, "<repaired>")

    blocks_before = before.root.ades if before.root else []
    blocks_after = after.root.ades if after.root else []
    if len(blocks_before) != len(blocks_after):
        raise MappingEditError("material block count changed")

    for index, (a, b) in enumerate(zip(blocks_before, blocks_after)):
        if index == block_index:
            if len(b.atts) != len(a.atts) + 1:
                raise MappingEditError(
                    f"block #{index}: expected +1 ATTS entry "
                    f"({len(a.atts)} -> {len(b.atts)})"
                )
            if len(b.olpl) != len(a.olpl) + 1:
                raise MappingEditError(
                    f"block #{index}: expected +1 OLPL group "
                    f"({len(a.olpl)} -> {len(b.olpl)})"
                )
            new_entry = b.atts[-1]
            if (new_entry.poly_id, new_entry.color_val, new_entry.shade_val,
                    new_entry.tracy_val, new_entry.pad) != (
                        plan.poly_id, plan.color_val, plan.shade_val,
                        plan.tracy_val, plan.pad):
                raise MappingEditError("appended ATTS entry does not match plan")
            if b.olpl[-1] != plan.uvs:
                raise MappingEditError("appended OLPL group does not match plan")
            if b.atts[:-1] != a.atts or b.olpl[:-1] != a.olpl:
                raise MappingEditError(
                    f"block #{index}: existing entries changed"
                )
            notes.append(
                f"block #{index}: ATTS {len(a.atts)} -> {len(b.atts)}, "
                f"OLPL {len(a.olpl)} -> {len(b.olpl)} (appended entry verified)"
            )
        else:
            if a.atts != b.atts or a.olpl != b.olpl:
                raise MappingEditError(f"unrelated block #{index} changed")

    tags_before = [c.tag for c in before.tree.iter_all()]
    tags_after = [c.tag for c in after.tree.iter_all()]
    if tags_before != tags_after:
        raise MappingEditError("chunk tree structure changed")
    notes.append(f"chunk tree shape unchanged ({len(tags_after)} chunks)")
    notes.append(
        f"file size {len(original)} -> {len(repaired)} "
        f"(+{len(repaired) - len(original)} bytes)"
    )
    return notes


def _encode_atts_payload(entries) -> bytes:
    payload = bytearray()
    for index, entry in enumerate(entries):
        if not (-0x8000 <= entry.poly_id <= 0x7FFF):
            raise MappingEditError(
                f"ATTS entry #{index} polyID is outside signed 16-bit range")
        values = (
            entry.color_val, entry.shade_val, entry.tracy_val, entry.pad)
        if any(value < 0 or value > 0xFF for value in values):
            raise MappingEditError(
                f"ATTS entry #{index} contains a value outside byte range")
        payload.extend(struct.pack(
            ">hBBBB", entry.poly_id, entry.color_val, entry.shade_val,
            entry.tracy_val, entry.pad))
    return bytes(payload)


def _encode_olpl_payload(groups) -> bytes:
    payload = bytearray()
    for index, group in enumerate(groups):
        if len(group) > 0x7FFF:
            raise MappingEditError(
                f"OLPL group #{index} exceeds the signed 16-bit count limit")
        payload.extend(struct.pack(">h", len(group)))
        for u, v in group:
            if not (0 <= u <= 0xFF and 0 <= v <= 0xFF):
                raise MappingEditError(
                    f"OLPL group #{index} contains a UV outside byte range")
            payload.extend(struct.pack(">BB", u, v))
    return bytes(payload)


def _rebuild_iff_chunk(data: bytes, chunk, replacements: dict[int, bytes]) -> bytes:
    replacement = replacements.get(chunk.offset)
    if replacement is not None:
        payload = replacement
    elif chunk.tag == "FORM" and chunk.children:
        payload = bytearray(data[chunk.payload_offset:chunk.payload_offset + 4])
        cursor = chunk.payload_offset + 4
        declared_end = chunk.payload_offset + chunk.size
        for child in chunk.children:
            payload.extend(data[cursor:child.offset])
            payload.extend(_rebuild_iff_chunk(data, child, replacements))
            cursor = child.offset + 8 + child.size + (child.size & 1)
        payload.extend(data[cursor:declared_end])
        payload = bytes(payload)
    else:
        end = chunk.offset + 8 + chunk.size + (chunk.size & 1)
        return data[chunk.offset:end]

    result = bytearray(data[chunk.offset:chunk.offset + 4])
    result.extend(struct.pack(">I", len(payload)))
    result.extend(payload)
    if len(payload) & 1:
        old_pad = chunk.payload_offset + chunk.size
        result.append(data[old_pad] if old_pad < len(data) else 0)
    return bytes(result)


def _wrap_ades_objt(objt: bytes) -> bytes:
    if len(objt) < 12 or objt[:4] != b"FORM" or objt[8:12] != b"OBJT":
        raise MappingEditError("structural ADES template is not FORM OBJT")

    def chunk(tag: bytes, payload: bytes) -> bytes:
        encoded = tag + struct.pack(">I", len(payload)) + payload
        return encoded + (b"\0" if len(payload) & 1 else b"")

    def form(form_type: bytes, children: bytes) -> bytes:
        return chunk(b"FORM", form_type + children)

    ades = form(b"ADES", objt)
    base = form(b"BASE", ades)
    root_objt = form(
        b"OBJT", chunk(b"CLID", b"base.class\0") + base)
    return form(b"MC2 ", root_objt)


def _rewrite_structural_objt(state: StructuralBlockState) -> bytes:
    template = state.template_objt
    if template is None:
        raise MappingEditError(
            f"block #{state.block_index} has no typed source OBJT")
    wrapped = _wrap_ades_objt(template)
    parsed = parse_base_bytes(wrapped, "<ades-template>")
    if parsed.root is None or parsed.tree is None \
            or len(parsed.root.ades) != 1:
        raise MappingEditError(
            f"block #{state.block_index} source OBJT failed to parse")
    block = parsed.root.ades[0]
    expected_class = (state.class_id or block.class_id or "").lower()
    if (block.class_id or "").lower() != expected_class:
        raise MappingEditError(
            f"block #{state.block_index} source class changed from "
            f"{state.class_id!r} to {block.class_id!r}")
    replacements: dict[int, bytes] = {}
    if expected_class == "amesh.class":
        if block.atts_chunk_offset < 0:
            raise MappingEditError(
                f"amesh.class block #{state.block_index} has no ATTS chunk")
        if state.olpl is not None and block.olpl_chunk_offset < 0:
            raise MappingEditError(
                f"amesh.class block #{state.block_index} has no OLPL chunk")
        if state.olpl is not None and len(state.atts) != len(state.olpl):
            raise MappingEditError(
                f"amesh.class block #{state.block_index} has ambiguous "
                "ATTS/OLPL counts")
        replacements[block.atts_chunk_offset] = _encode_atts_payload(
            state.atts)
        if state.olpl is not None:
            replacements[block.olpl_chunk_offset] = _encode_olpl_payload(
                state.olpl)
    elif expected_class == "area.class":
        if state.ade_poly_id is None:
            raise MappingEditError(
                f"area.class block #{state.block_index} has no POL2 reference")
        if len(state.atts) != 1 \
                or state.atts[0].poly_id != state.ade_poly_id:
            raise MappingEditError(
                f"area.class block #{state.block_index} has an inconsistent "
                "derived mapping")
        if block.ade_strc_chunk_offset < 0 \
                or block.ade_strc_chunk_size < 10:
            raise MappingEditError(
                f"area.class block #{state.block_index} has no writable "
                "FORM ADE/STRC")
        ade_chunk = next(
            (chunk for chunk in parsed.tree.iter_all()
             if chunk.offset == block.ade_strc_chunk_offset), None)
        if ade_chunk is None or ade_chunk.available_size < 10:
            raise MappingEditError(
                f"area.class block #{state.block_index} ADE/STRC is truncated")
        payload = bytearray(ade_chunk.payload(wrapped))
        struct.pack_into(">h", payload, 6, state.ade_poly_id)
        replacements[ade_chunk.offset] = bytes(payload)
    elif expected_class == "particle.class":
        # PTCL/ATTS stores emitter parameters; there are no decoded POL2 refs.
        pass
    else:
        # Unknown classes are preserved byte-for-byte.  Topology renumbering
        # reaches this writer only after the reference graph has rejected any
        # operation that could require rewriting one.
        if template != block.source_objt_bytes:
            raise MappingEditError(
                f"unknown class {state.class_id!r} template is inconsistent")

    root = parsed.tree.roots[0]
    rebuilt = _rebuild_iff_chunk(wrapped, root, replacements)
    verified = parse_base_bytes(rebuilt, "<ades-template-rewritten>")
    if verified.root is None or len(verified.root.ades) != 1:
        raise MappingEditError(
            f"block #{state.block_index} rewrite failed to re-parse")
    return verified.root.ades[0].source_objt_bytes


def _rewrite_complete_ades(
        data: bytes, asset, states: list[StructuralBlockState]) -> bytes:
    if asset.root.ades_form_offset < 0:
        raise MappingEditError("the standalone BASE has no FORM ADES")
    expected_indices = list(range(len(states)))
    if [state.block_index for state in states] != expected_indices:
        raise MappingEditError(
            "complete ADES states must be ordered and consecutively indexed")
    children = b"".join(_rewrite_structural_objt(state) for state in states)
    replacements = {
        asset.root.ades_form_offset: b"ADES" + children,
    }
    output = bytearray()
    cursor = 0
    for root in asset.tree.roots:
        output.extend(data[cursor:root.offset])
        output.extend(_rebuild_iff_chunk(data, root, replacements))
        cursor = root.offset + 8 + root.size + (root.size & 1)
    output.extend(data[cursor:])
    return bytes(output)


def rewrite_model_base_structure(
        data: bytes, states: list[StructuralBlockState]) -> bytes:
    """Rewrite typed ADES state, including OBJT grow/shrink."""

    asset = parse_base_bytes(data, "<model-base-structure>")
    if asset.root is None or asset.tree is None:
        raise MappingEditError("not a parseable standalone model BASE")
    if any(chunk.truncated for chunk in asset.tree.iter_all()):
        raise MappingEditError(
            "the BASE contains truncated chunks and cannot be rewritten")
    complete = any(state.template_objt is not None for state in states)
    if complete:
        if not all(state.template_objt is not None for state in states):
            raise MappingEditError(
                "typed and legacy structural block states cannot be mixed")
        return _rewrite_complete_ades(data, asset, states)

    # Backward-compatible amesh-only writer used by existing API callers.
    blocks = asset.root.ades
    replacements: dict[int, bytes] = {}
    seen: set[int] = set()
    for state in states:
        block_index = state.block_index
        if block_index in seen:
            raise MappingEditError(
                f"duplicate structural state for block #{block_index}")
        seen.add(block_index)
        if not (0 <= block_index < len(blocks)):
            raise MappingEditError(
                f"material block #{block_index} no longer exists")
        block = blocks[block_index]
        if (block.class_id or "").lower() != "amesh.class":
            raise MappingEditError(
                f"block #{block_index} is not an editable amesh block")
        if block.atts_chunk_offset < 0:
            raise MappingEditError(
                f"block #{block_index} has no structural ATTS chunk")
        if state.olpl is not None and block.olpl_chunk_offset < 0:
            raise MappingEditError(
                f"block #{block_index} has no structural OLPL chunk")
        if state.olpl is not None and len(state.atts) != len(state.olpl):
            raise MappingEditError(
                f"block #{block_index} has ambiguous ATTS/OLPL counts")
        replacements[block.atts_chunk_offset] = _encode_atts_payload(
            state.atts)
        if state.olpl is not None:
            replacements[block.olpl_chunk_offset] = _encode_olpl_payload(
                state.olpl)

    output = bytearray()
    cursor = 0
    for root in asset.tree.roots:
        output.extend(data[cursor:root.offset])
        output.extend(_rebuild_iff_chunk(data, root, replacements))
        cursor = root.offset + 8 + root.size + (root.size & 1)
    output.extend(data[cursor:])
    return bytes(output)


def _block_metadata(block) -> tuple:
    texture = block.texture
    tracy = block.tracy_texture
    return (
        block.class_id, block.ade_flags, block.ade_point_id,
        block.ade_poly_id, block.area_flags, block.polflags,
        block.color_val, block.tracy_val, block.shade_val,
        (
            texture.class_id, texture.kind, texture.name,
            tuple(texture.outline_uvs), texture.anim_type
        ) if texture is not None else None,
        (
            tracy.class_id, tracy.kind, tracy.name,
            tuple(tracy.outline_uvs), tracy.anim_type
        ) if tracy is not None else None,
    )


def _block_material_metadata(block) -> tuple:
    metadata = _block_metadata(block)
    # ADE polyID is the only AREA material metadata field rewritten by the
    # typed handler.  ATTS/OLPL are intentionally not part of this tuple.
    return metadata[:3] + metadata[4:]


def verify_model_base_structure(
        original: bytes, edited: bytes,
        states: list[StructuralBlockState]) -> list[str]:
    """Prove structural output matches requested mappings only."""

    before = parse_base_bytes(original, "<model-base-before>")
    after = parse_base_bytes(edited, "<model-base-after>")
    if before.root is None or after.root is None \
            or before.tree is None or after.tree is None:
        raise MappingEditError("structurally edited BASE failed to re-parse")
    complete = any(state.template_objt is not None for state in states)
    before_objects = _walk_with_owner_paths(before.root)
    after_objects = _walk_with_owner_paths(after.root)
    if before_objects.keys() != after_objects.keys():
        raise MappingEditError("BASE object tree changed")
    requested = {state.block_index: state for state in states}
    for owner, object_before in before_objects.items():
        object_after = after_objects[owner]
        if (
                object_before.name != object_after.name
                or object_before.transform != object_after.transform
                or object_before.skeleton_class != object_after.skeleton_class
                or object_before.skeleton_name != object_after.skeleton_name):
            raise MappingEditError(
                f"{owner}: non-mapping BASE object data changed")
        if owner != "root" or not complete:
            if len(object_before.ades) != len(object_after.ades):
                raise MappingEditError(
                    f"{owner}: unrelated material block count changed")
        if owner == "root" and complete:
            if len(object_after.ades) != len(states):
                raise MappingEditError(
                    "typed ADES block count did not round-trip")
            for state, block_after in zip(states, object_after.ades):
                if (block_after.class_id or "").lower() \
                        != (state.class_id or "").lower():
                    raise MappingEditError(
                        f"block #{state.block_index}: class did not round-trip")
                if list(state.atts) != block_after.atts:
                    raise MappingEditError(
                        f"block #{state.block_index}: POL2 mapping did not "
                        "round-trip")
                expected_olpl = (
                    [list(group) for group in state.olpl]
                    if state.olpl is not None else None)
                if expected_olpl is not None \
                        and expected_olpl != block_after.olpl:
                    raise MappingEditError(
                        f"block #{state.block_index}: OLPL did not round-trip")
                if (state.class_id or "").lower() == "area.class" \
                        and block_after.ade_poly_id != state.ade_poly_id:
                    raise MappingEditError(
                        f"block #{state.block_index}: AREA polyID did not "
                        "round-trip")
                template = parse_base_bytes(
                    _wrap_ades_objt(state.template_objt),
                    "<verify-ades-template>")
                template_block = template.root.ades[0]
                if _block_material_metadata(template_block) \
                        != _block_material_metadata(block_after):
                    raise MappingEditError(
                        f"block #{state.block_index}: material, texture or "
                        "animation data changed")
            continue
        for block_index, (block_before, block_after) in enumerate(
                zip(object_before.ades, object_after.ades)):
            if _block_metadata(block_before) != _block_metadata(block_after):
                raise MappingEditError(
                    f"{owner} block #{block_index}: material data changed")
            state = requested.get(block_index) if owner == "root" else None
            if state is None:
                if block_before.atts != block_after.atts \
                        or block_before.olpl != block_after.olpl:
                    raise MappingEditError(
                        f"{owner} block #{block_index}: unrelated mapping changed")
            else:
                expected_atts = list(state.atts)
                expected_olpl = (
                    [
                        [tuple(uv) for uv in group]
                        for group in state.olpl
                    ]
                    if state.olpl is not None else block_before.olpl)
                if block_after.atts != expected_atts \
                        or block_after.olpl != expected_olpl:
                    raise MappingEditError(
                        f"block #{block_index}: structural mapping did not "
                        "round-trip exactly")

    if complete:
        return [
            f"verified {len(states)} typed ADES block state(s)",
            f"BASE size {len(original)} -> {len(edited)}",
            "object tree, materials, textures, animations and non-ADES data "
            "preserved",
        ]

    chunks_before = list(before.tree.iter_all())
    chunks_after = list(after.tree.iter_all())
    if len(chunks_before) != len(chunks_after):
        raise MappingEditError("BASE chunk tree length changed")
    target_offsets = set()
    for state in states:
        block = before.root.ades[state.block_index]
        target_offsets.add(block.atts_chunk_offset)
        if state.olpl is not None:
            target_offsets.add(block.olpl_chunk_offset)
    for old, new in zip(chunks_before, chunks_after):
        if old.tag != new.tag or old.form_type != new.form_type:
            raise MappingEditError("BASE chunk tree shape changed")
        if old.children or old.offset in target_offsets:
            continue
        if old.payload(original) != new.payload(edited):
            raise MappingEditError(
                f"unrelated chunk {old.display_name} payload changed")
    return [
        f"verified {len(states)} complete amesh ATTS/OLPL block state(s)",
        f"BASE size {len(original)} -> {len(edited)}",
        "object tree, materials, textures and unrelated chunk payloads preserved",
    ]


# --- UV edits (fixed-size in-place patch of existing OLPL groups) ---------------


@dataclass
class UVEdit:
    """One edited OLPL group: same UV count, new (u, v) byte values."""

    owner_path: str          # FamilyObject.owner_path ("root", "root/kid[3]")
    block_index: int         # index into that object's ades list
    atts_index: int          # OLPL group index (== ATTS entry index)
    uvs: list[tuple[int, int]] = field(default_factory=list)

    def key(self) -> tuple[str, int, int]:
        return (self.owner_path, self.block_index, self.atts_index)


@dataclass
class AttsValueEdit:
    """One edited ATTS entry: new color/shade/tracy byte values.

    poly_id and pad are NEVER touched (fixed-size in-place patch of
    bytes 2..4 of the 6-byte record)."""

    owner_path: str
    block_index: int
    atts_index: int
    color_val: int = 0
    shade_val: int = 0
    tracy_val: int = 0

    def key(self) -> tuple[str, int, int]:
        return (self.owner_path, self.block_index, self.atts_index)


@dataclass
class TextureNameEdit:
    """Replace one logical ILBM or bmpanim reference in a material block."""

    owner_path: str
    block_index: int
    name: str
    binding_slot: str = "texture"

    def key(self) -> tuple[str, int, str]:
        return (self.owner_path, self.block_index, self.binding_slot)


def _walk_with_owner_paths(root) -> dict[str, object]:
    """BaseObject tree -> {owner_path: BaseObject}, same labelling as
    asset_family (parse order is deterministic)."""

    result: dict[str, object] = {}

    def walk(obj, path: str) -> None:
        result[path] = obj
        for index, kid in enumerate(obj.kids):
            walk(kid, f"{path}/kid[{index}]")

    walk(root, "root")
    return result


def _olpl_group_offset(data: bytes, block, atts_index: int) -> tuple[int, int]:
    """(payload offset of the group's first UV byte, uv count) inside the
    block's OLPL chunk."""

    if block.olpl_chunk_offset < 0:
        raise MappingEditError("block has no OLPL chunk on disk")
    pos = block.olpl_chunk_offset + 8
    end = block.olpl_chunk_offset + 8 + block.olpl_chunk_size
    for index in range(atts_index + 1):
        if pos + 2 > end:
            raise MappingEditError(
                f"OLPL group #{atts_index} not found (chunk too short)"
            )
        count = struct.unpack_from(">h", data, pos)[0]
        pos += 2
        if index == atts_index:
            if pos + count * 2 > end:
                raise MappingEditError("OLPL group is truncated")
            return pos, count
        pos += count * 2
    raise MappingEditError("unreachable")


def _atts_entry_offset(data: bytes, block, atts_index: int) -> int:
    """Absolute offset of the 6-byte ATTS record #atts_index."""

    if block.atts_chunk_offset < 0:
        raise MappingEditError("block has no ATTS chunk on disk")
    offset = block.atts_chunk_offset + 8 + 6 * atts_index
    end = block.atts_chunk_offset + 8 + block.atts_chunk_size
    if offset + 6 > end:
        raise MappingEditError(
            f"ATTS entry #{atts_index} not found (chunk too short)"
        )
    return offset


def apply_atts_edits_to_bytes(data: bytes,
                              edits: list[AttsValueEdit]) -> bytes:
    """Overwrite color/shade/tracy of existing ATTS entries.  poly_id, pad,
    the file size and every chunk size stay identical."""

    asset = parse_base_bytes(data, "<atts-edit>")
    if asset.root is None:
        raise MappingEditError("not a parseable BASE file")
    objects = _walk_with_owner_paths(asset.root)

    new_data = bytearray(data)
    for edit in edits:
        obj = objects.get(edit.owner_path)
        if obj is None:
            raise MappingEditError(f"object {edit.owner_path!r} not found")
        if not (0 <= edit.block_index < len(obj.ades)):
            raise MappingEditError(
                f"{edit.owner_path}: block #{edit.block_index} not found"
            )
        block = obj.ades[edit.block_index]
        offset = _atts_entry_offset(data, block, edit.atts_index)
        new_data[offset + 2] = edit.color_val & 0xFF
        new_data[offset + 3] = edit.shade_val & 0xFF
        new_data[offset + 4] = edit.tracy_val & 0xFF
    return bytes(new_data)


def apply_uv_edits_to_bytes(data: bytes, edits: list[UVEdit]) -> bytes:
    """Overwrite the UV bytes of existing OLPL groups.  The file size and
    every chunk size stay identical: this is a fixed-size in-place patch."""

    asset = parse_base_bytes(data, "<uv-edit>")
    if asset.root is None:
        raise MappingEditError("not a parseable BASE file")
    objects = _walk_with_owner_paths(asset.root)

    new_data = bytearray(data)
    for edit in edits:
        obj = objects.get(edit.owner_path)
        if obj is None:
            raise MappingEditError(f"object {edit.owner_path!r} not found")
        if not (0 <= edit.block_index < len(obj.ades)):
            raise MappingEditError(
                f"{edit.owner_path}: block #{edit.block_index} not found"
            )
        block = obj.ades[edit.block_index]
        offset, count = _olpl_group_offset(data, block, edit.atts_index)
        if count != len(edit.uvs):
            raise MappingEditError(
                f"{edit.owner_path} block #{edit.block_index} group "
                f"#{edit.atts_index}: UV count mismatch "
                f"({count} on disk vs {len(edit.uvs)} edited)"
            )
        for i, (u, v) in enumerate(edit.uvs):
            new_data[offset + 2 * i] = u & 0xFF
            new_data[offset + 2 * i + 1] = v & 0xFF
    return bytes(new_data)


def _expected_edit_offsets(data: bytes, uv_edits: list[UVEdit],
                           atts_edits: list[AttsValueEdit]) -> set[int]:
    """Absolute byte offsets that are allowed to change."""

    asset = parse_base_bytes(data, "<edit-offsets>")
    if asset.root is None:
        raise MappingEditError("not a parseable BASE file")
    objects = _walk_with_owner_paths(asset.root)
    offsets: set[int] = set()

    for edit in uv_edits:
        obj = objects.get(edit.owner_path)
        if obj is None:
            raise MappingEditError(f"object {edit.owner_path!r} not found")
        if not (0 <= edit.block_index < len(obj.ades)):
            raise MappingEditError(
                f"{edit.owner_path}: block #{edit.block_index} not found"
            )
        block = obj.ades[edit.block_index]
        offset, count = _olpl_group_offset(data, block, edit.atts_index)
        if count != len(edit.uvs):
            raise MappingEditError(
                f"{edit.owner_path} block #{edit.block_index} group "
                f"#{edit.atts_index}: UV count mismatch "
                f"({count} on disk vs {len(edit.uvs)} edited)"
            )
        for i in range(count):
            offsets.add(offset + 2 * i)
            offsets.add(offset + 2 * i + 1)

    for edit in atts_edits:
        obj = objects.get(edit.owner_path)
        if obj is None:
            raise MappingEditError(f"object {edit.owner_path!r} not found")
        if not (0 <= edit.block_index < len(obj.ades)):
            raise MappingEditError(
                f"{edit.owner_path}: block #{edit.block_index} not found"
            )
        block = obj.ades[edit.block_index]
        offset = _atts_entry_offset(data, block, edit.atts_index)
        offsets.update((offset + 2, offset + 3, offset + 4))

    return offsets


def verify_family_edits(original: bytes, edited: bytes,
                        uv_edits: list[UVEdit],
                        atts_edits: list[AttsValueEdit]) -> list[str]:
    """Re-parse and prove only the intended UV/ATTS bytes changed."""

    if len(original) != len(edited):
        raise MappingEditError("file size changed (must be identical)")
    allowed_offsets = _expected_edit_offsets(original, uv_edits, atts_edits)
    changed_offsets = {
        i for i, (before, after) in enumerate(zip(original, edited))
        if before != after
    }
    unexpected = changed_offsets - allowed_offsets
    if unexpected:
        first = min(unexpected)
        raise MappingEditError(
            f"unexpected byte changed at 0x{first:X} "
            "(outside edited UV/ATTS records)"
        )
    before = parse_base_bytes(original, "<original>")
    after = parse_base_bytes(edited, "<edited>")
    objs_before = _walk_with_owner_paths(before.root)
    objs_after = _walk_with_owner_paths(after.root)
    if objs_before.keys() != objs_after.keys():
        raise MappingEditError("object tree changed")

    uv_by_key = {e.key(): e for e in uv_edits}
    atts_by_key = {e.key(): e for e in atts_edits}
    notes: list[str] = []
    for path, obj_a in objs_before.items():
        obj_b = objs_after[path]
        if len(obj_a.ades) != len(obj_b.ades):
            raise MappingEditError(f"{path}: block count changed")
        for bi, (a, b) in enumerate(zip(obj_a.ades, obj_b.ades)):
            if len(a.atts) != len(b.atts):
                raise MappingEditError(
                    f"{path} block #{bi}: ATTS entry count changed"
                )
            for gi, (ea, eb) in enumerate(zip(a.atts, b.atts)):
                key = (path, bi, gi)
                edit = atts_by_key.get(key)
                if edit is not None:
                    if (eb.poly_id, eb.pad) != (ea.poly_id, ea.pad):
                        raise MappingEditError(
                            f"{path} block #{bi} ATTS #{gi}: "
                            "poly_id/pad changed (must never happen)"
                        )
                    if (eb.color_val, eb.shade_val, eb.tracy_val) != (
                            edit.color_val, edit.shade_val, edit.tracy_val):
                        raise MappingEditError(
                            f"{path} block #{bi} ATTS #{gi}: edit not applied"
                        )
                    notes.append(f"{path} block #{bi} ATTS #{gi}: "
                                 f"color={eb.color_val} shade={eb.shade_val} "
                                 f"tracy={eb.tracy_val}")
                elif ea != eb:
                    raise MappingEditError(
                        f"{path} block #{bi} ATTS #{gi}: unintended change"
                    )
            if len(a.olpl) != len(b.olpl):
                raise MappingEditError(
                    f"{path} block #{bi}: OLPL group count changed"
                )
            for gi, (ga, gb) in enumerate(zip(a.olpl, b.olpl)):
                key = (path, bi, gi)
                if key in uv_by_key:
                    if gb != uv_by_key[key].uvs:
                        raise MappingEditError(
                            f"{path} block #{bi} group #{gi}: edit not applied"
                        )
                    notes.append(f"{path} block #{bi} group #{gi}: "
                                 f"UVs updated ({len(gb)} points)")
                elif ga != gb:
                    raise MappingEditError(
                        f"{path} block #{bi} group #{gi}: unintended change"
                    )
    notes.append(f"file size unchanged ({len(edited)} bytes); "
                 "only edited UV/ATTS bytes differ")
    return notes


def verify_uv_edits(original: bytes, edited: bytes,
                    edits: list[UVEdit]) -> list[str]:
    """Re-parse and prove only the intended UV bytes changed."""

    return verify_family_edits(original, edited, edits, [])


def save_family_edits(family, uv_edits: list[UVEdit],
                      atts_edits: list[AttsValueEdit],
                      out_path: str | Path) -> list[str]:
    """Apply UV + ATTS edits to the ORIGINAL file bytes and save to a NEW
    path.  Both patches are fixed-size and in-place, so they compose."""

    if not uv_edits and not atts_edits:
        raise MappingEditError("no edits to save")
    source = family.base_path
    if source is None or not Path(source).is_file():
        raise MappingEditError("the family was not loaded from a file")
    out_path = Path(out_path)
    if out_path.resolve() == Path(source).resolve():
        raise MappingEditError(
            "refusing to overwrite the original file; choose a new path"
        )

    data = Path(source).read_bytes()
    edited = data
    if uv_edits:
        edited = apply_uv_edits_to_bytes(edited, uv_edits)
    if atts_edits:
        edited = apply_atts_edits_to_bytes(edited, atts_edits)
    notes = verify_family_edits(data, edited, uv_edits, atts_edits)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(edited)
    notes.append(f"saved to {out_path}")
    return notes


def save_uv_edited_base(family, edits: list[UVEdit],
                        out_path: str | Path) -> list[str]:
    """Apply UV edits to the ORIGINAL file bytes and save to a NEW path."""

    return save_family_edits(family, edits, [], out_path)


# --- standalone model BASE export and texture references -----------------------


def export_base_object_bytes(source: bytes, base_object) -> bytes:
    """Wrap one source FORM OBJT (including its KIDS) in a standalone MC2.

    No object payload is reconstructed: the exact original OBJT bytes are
    copied, while archive siblings stay out of the loose BASE export.
    """

    offset = getattr(base_object, "source_objt_offset", -1)
    size = getattr(base_object, "source_objt_size", 0)
    end = offset + 8 + size + (size & 1)
    if offset < 0 or size < 4 or end > len(source):
        raise MappingEditError("selected BASE object has no valid source OBJT span")
    if source[offset:offset + 4] != b"FORM" \
            or source[offset + 8:offset + 12] != b"OBJT":
        raise MappingEditError("selected source span is not FORM OBJT")
    objt = source[offset:end]
    payload = b"MC2 " + objt
    exported = b"FORM" + struct.pack(">I", len(payload)) + payload
    if len(payload) & 1:
        exported += b"\0"
    parsed = parse_base_bytes(exported, "<standalone-model-base>")
    if parsed.root is None:
        raise MappingEditError("standalone BASE verification could not parse its root")
    return exported


def _texture_name_span(data: bytes, block, edit: TextureNameEdit):
    if edit.binding_slot not in ("texture", "tracy_texture"):
        raise MappingEditError(
            f"unsupported texture binding slot {edit.binding_slot!r}")
    texture = getattr(block, edit.binding_slot)
    if texture is None:
        raise MappingEditError(
            f"{edit.owner_path} block #{edit.block_index} has no texture binding")
    if texture.kind not in ("ilbm", "bmpanim"):
        raise MappingEditError(
            f"{edit.owner_path} block #{edit.block_index} uses "
            f"{texture.kind or 'an unsupported texture class'}; only ILBM and "
            "bmpanim references can be exported safely")
    offset = texture.name_payload_offset
    capacity = texture.name_capacity
    if offset < 0 or capacity <= 0 or offset + capacity > len(data):
        raise MappingEditError(
            f"{edit.owner_path} block #{edit.block_index} has no writable "
            "texture-name span")
    try:
        encoded = edit.name.encode("latin-1")
    except UnicodeEncodeError as exc:
        raise MappingEditError("texture names must use Latin-1 characters") from exc
    if not encoded or b"\0" in encoded:
        raise MappingEditError("texture name must be a non-empty c-string")
    if len(encoded) + 1 > capacity:
        raise MappingEditError(
            f"texture name {edit.name!r} needs {len(encoded) + 1} bytes, "
            f"but the original name allocation has {capacity}; choose a "
            f"name of at most {capacity - 1} bytes")
    return offset, capacity, encoded


def apply_texture_name_edits_to_bytes(
        data: bytes, edits: list[TextureNameEdit]) -> bytes:
    asset = parse_base_bytes(data, "<texture-name-edit>")
    if asset.root is None:
        raise MappingEditError("not a parseable BASE file")
    objects = _walk_with_owner_paths(asset.root)
    output = bytearray(data)
    seen: set[tuple[str, int, str]] = set()
    for edit in edits:
        if edit.key() in seen:
            raise MappingEditError(f"duplicate texture edit for {edit.key()}")
        seen.add(edit.key())
        obj = objects.get(edit.owner_path)
        if obj is None or not (0 <= edit.block_index < len(obj.ades)):
            raise MappingEditError(
                f"{edit.owner_path}: block #{edit.block_index} not found")
        offset, capacity, encoded = _texture_name_span(
            data, obj.ades[edit.block_index], edit)
        output[offset:offset + capacity] = (
            encoded + b"\0" + bytes(capacity - len(encoded) - 1))
    return bytes(output)


def verify_texture_name_edits(original: bytes, edited: bytes,
                              edits: list[TextureNameEdit]) -> list[str]:
    if len(original) != len(edited):
        raise MappingEditError("texture edit changed the BASE file size")
    before = parse_base_bytes(original, "<texture-original>")
    after = parse_base_bytes(edited, "<texture-edited>")
    if before.root is None or after.root is None:
        raise MappingEditError("texture-edited BASE did not re-parse")
    objects_before = _walk_with_owner_paths(before.root)
    objects_after = _walk_with_owner_paths(after.root)
    if objects_before.keys() != objects_after.keys():
        raise MappingEditError("texture edit changed the BASE object tree")
    edits_by_key = {edit.key(): edit for edit in edits}
    allowed: set[int] = set()
    notes: list[str] = []
    for owner, obj_before in objects_before.items():
        obj_after = objects_after[owner]
        if len(obj_before.ades) != len(obj_after.ades):
            raise MappingEditError(f"{owner}: material block count changed")
        for block_index, (block_before, block_after) in enumerate(
                zip(obj_before.ades, obj_after.ades)):
            for binding_slot in ("texture", "tracy_texture"):
                edit = edits_by_key.get(
                    (owner, block_index, binding_slot))
                before_texture = getattr(block_before, binding_slot)
                after_texture = getattr(block_after, binding_slot)
                before_name = (
                    before_texture.name if before_texture else None)
                after_name = after_texture.name if after_texture else None
                if edit is None:
                    if before_name != after_name:
                        raise MappingEditError(
                            f"{owner} block #{block_index} {binding_slot}: "
                            "unintended texture change")
                elif after_name != edit.name:
                    raise MappingEditError(
                        f"{owner} block #{block_index} {binding_slot}: "
                        "texture edit did not re-parse")
                else:
                    offset, capacity, _encoded = _texture_name_span(
                        original, block_before, edit)
                    allowed.update(range(offset, offset + capacity))
                    notes.append(
                        f"{owner} block #{block_index} {binding_slot}: "
                        f"{before_name} -> {after_name}")
    changed = {index for index, pair in enumerate(zip(original, edited))
               if pair[0] != pair[1]}
    unexpected = changed - allowed
    if unexpected:
        raise MappingEditError(
            f"unexpected byte changed at 0x{min(unexpected):X}")
    notes.append("BASE size and chunk structure unchanged")
    return notes


def rewrite_block_texture_template(
        block, name: str, binding_slot: str = "texture") -> bytes:
    """Return one ADES OBJT template with a verified texture-name change."""

    template = getattr(block, "source_objt_bytes", b"")
    if not template:
        raise MappingEditError("material block has no parsed source OBJT")
    wrapped = _wrap_ades_objt(template)
    edit = TextureNameEdit("root", 0, name, binding_slot)
    edited = apply_texture_name_edits_to_bytes(wrapped, [edit])
    parsed = parse_base_bytes(edited, "<retargeted-ades-template>")
    if parsed.root is None or len(parsed.root.ades) != 1:
        raise MappingEditError(
            "retargeted material template failed to re-parse")
    updated = getattr(parsed.root.ades[0], binding_slot, None)
    if updated is None or updated.name != name:
        raise MappingEditError(
            "retargeted material template did not preserve the new texture")
    return parsed.root.ades[0].source_objt_bytes


def save_model_base_copy(data: bytes, uv_edits: list[UVEdit],
                         texture_edits: list[TextureNameEdit],
                         out_path: str | Path,
                         mapping_plans: list[RepairPlan] | None = None,
                         *,
                         structural_blocks: list[StructuralBlockState] | None = None
                         ) -> list[str]:
    """Save and verify a standalone model BASE copy.

    UV and texture-name edits are fixed-size patches.  An unchanged BASE copy
    is also allowed so a SKLT export can always have its companion BASE.
    """

    edited = data
    notes: list[str] = []
    if structural_blocks is not None:
        if mapping_plans:
            raise MappingEditError(
                "append repair plans and complete structural states cannot "
                "be combined")
        structured = rewrite_model_base_structure(
            edited, structural_blocks)
        notes.extend(verify_model_base_structure(
            edited, structured, structural_blocks))
        edited = structured
    for plan in mapping_plans or []:
        parsed = parse_base_bytes(edited, "<model-base-append>")
        blocks = parsed.root.ades if parsed.root else []
        if not (0 <= plan.block_index < len(blocks)):
            raise MappingEditError(
                f"material block #{plan.block_index} no longer exists")
        repaired = apply_repair_to_bytes(
            edited, blocks[plan.block_index], plan)
        notes.extend(verify_repair(
            edited, repaired, plan.block_index, plan))
        edited = repaired
    if uv_edits:
        uv_edited = apply_uv_edits_to_bytes(edited, uv_edits)
        notes.extend(verify_uv_edits(edited, uv_edited, uv_edits))
        edited = uv_edited
    if texture_edits:
        texture_edited = apply_texture_name_edits_to_bytes(
            edited, texture_edits)
        notes.extend(verify_texture_name_edits(
            edited, texture_edited, texture_edits))
        edited = texture_edited
    verified = parse_base_bytes(edited, "<model-base-export>")
    if verified.root is None:
        raise MappingEditError("exported model BASE failed final verification")
    destination = Path(out_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(edited)
    notes.append(f"saved to {destination}")
    return notes


def save_repaired_base(family, fam_obj, plans: list[RepairPlan],
                       out_path: str | Path) -> list[str]:
    """Apply all plans to the ORIGINAL file bytes and save to a new path."""

    if not plans:
        raise MappingEditError("no repair plans to save")
    source = family.base_path
    if source is None or not Path(source).is_file():
        raise MappingEditError("the family was not loaded from a loose .base file")
    out_path = Path(out_path)
    if out_path.resolve() == Path(source).resolve():
        raise MappingEditError(
            "refusing to overwrite the original file; choose a new path"
        )

    data = Path(source).read_bytes()
    notes: list[str] = []
    for plan in plans:
        # Re-parse each round so chunk offsets are fresh after the previous
        # insertion.
        asset = parse_base_bytes(data, Path(source).name)
        blocks = asset.root.ades if asset.root else []
        if plan.block_index >= len(blocks):
            raise MappingEditError(f"block #{plan.block_index} not found")
        block = blocks[plan.block_index]
        repaired = apply_repair_to_bytes(data, block, plan)
        notes.extend(verify_repair(data, repaired, plan.block_index, plan))
        data = repaired

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)
    notes.append(f"saved to {out_path}")
    return notes


if __name__ == "__main__":
    import argparse

    from asset_family import load_asset_family

    cli = argparse.ArgumentParser(
        description="Inspect/repair BASE ATTS coverage holes (writes only "
                    "to a new file via --repair/--out)."
    )
    cli.add_argument("base_file")
    cli.add_argument("--repair", type=int, metavar="POLYID",
                     help="polygon to repair")
    cli.add_argument("--block", type=int, default=0,
                     help="target material block index (planar method)")
    cli.add_argument("--copy-from", type=int, metavar="POLYID",
                     help="copy mapping style from this polygon instead")
    cli.add_argument("--out", help="output .base path (never the original)")
    cli.add_argument("--deps", action="store_true",
                     help="print the dependency report and exit")
    args = cli.parse_args()

    family = load_asset_family(args.base_file)
    if args.deps:
        from base_dependency_resolver import print_report

        print_report(family)
        raise SystemExit(0)
    fam_obj = next((o for o in family.all_objects() if o.skeleton), None)
    if fam_obj is None:
        raise SystemExit("no skeleton-bearing object in this family")
    mapping = MappingIndex(fam_obj)
    print(f"{args.base_file}: {mapping.poly_count} polygons, "
          f"unmapped={mapping.unmapped}, duplicates={mapping.duplicates}, "
          f"invalid={mapping.invalid}")
    for index, block in eligible_blocks(fam_obj):
        tex = block.texture.name if block.texture else "-"
        print(f"  block #{index}: {tex} ({len(block.atts)} entries)")

    if args.repair is not None:
        if mapping.status(args.repair) != "unmapped":
            raise SystemExit(f"polygon #{args.repair} is not unmapped "
                             f"({mapping.status(args.repair)})")
        if args.copy_from is not None:
            plan = plan_copy_style(fam_obj, args.repair, args.copy_from,
                                   mapping)
        else:
            plan = plan_planar(fam_obj, args.repair, args.block, mapping)
        for line in plan.describe():
            print(line)
        if args.out:
            for note in save_repaired_base(family, fam_obj, [plan], args.out):
                print(note)
