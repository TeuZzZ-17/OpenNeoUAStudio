"""A loose model (BASE + skeleton) opened for MCP editing, with a verified atomic commit.

All edits happen in memory on Studio's own data structures (SkltModel and the ADES material blocks)
and nothing touches the disk until ``commit``. The commit reuses the same writers as the Studio
window (``build_topology_states`` + ``save_model_base_copy`` + ``save_sklt_with_poo2_pol2_structure``),
checks the staged files, takes an undo snapshot and only then replaces the real files.
"""

from __future__ import annotations

import copy
import math
import os
import shutil
import uuid
from pathlib import Path

from studio.assets.family import load_asset_family
from studio.assets.mapping import _block_material_metadata, MappingEditError, MappingIndex, rewrite_ade_point_template, save_model_base_copy
from studio.exchange.portable import DirectoryIndex
from studio.formats.base_format import AttsEntry, parse_base_bytes
from studio.formats.sklt import (
    parse_sklt_file,
    save_sklt_with_poo2_points,
    save_sklt_with_poo2_pol2_structure,
)
from studio.rendering.geometry import (
    apply_delete_geometry,
    build_topology_states,
    detect_fx_elements,
    GeometryClipboardError,
    plan_delete_geometry,
    unsafe_fx_geometry_polys,
)
from studio_mcp import core, history, meshmath

UV_MAX = 255
MAX_POINTS = 0x10000
MAX_POLYGONS = 0x8000


def resolve_base(target: str) -> Path:
    """A .BASE file, a .sklt (its folder must hold one .BASE) or a folder with exactly one .BASE (relative paths start at Data)."""
    path = core.resolve_existing(target)
    if path.is_dir():
        found = sorted(p for p in path.iterdir() if p.is_file() and p.suffix.lower() == ".base")
        if len(found) != 1:
            names = ", ".join(p.name for p in found) or "none"
            raise core.McpToolError(f"{path} must contain exactly one .BASE (found: {names}); pass the .BASE path")
        return found[0]
    if path.suffix.lower() == ".sklt":
        return resolve_base(str(path.parent))
    if path.suffix.lower() not in (".base", ".bas"):
        raise core.McpToolError(f"Expected a .BASE file or a folder with one, got {path.name}")
    return path


class LooseModel:
    """In-memory working copy of one object of a loose BASE (the root object unless owner_path says otherwise)."""

    def __init__(self, base_path: Path, owner_path: str = "", set_hint: str = "", writable: bool = True):
        self.base_path = base_path
        self.writable = writable
        self.set_hint = set_hint
        setbas = Path(core._set_info(set_hint or "set1")["set_bas"])
        DirectoryIndex.clear_cache()   # see files written since the last call (see core.load_family)
        self.family = load_asset_family(base_path, setbas=setbas)
        if self.family.root_object is None:
            raise core.McpToolError(f"Could not load {base_path.name}: {'; '.join(self.family.warnings[:3])}")
        candidates = [o for o in self.family.all_objects() if o.skeleton is not None]
        if owner_path:
            candidates = [o for o in candidates if o.owner_path == owner_path]
        if not candidates:
            raise core.McpToolError("No object with a skeleton found" + (f" at {owner_path!r}" if owner_path else ""))
        self.fam_obj = candidates[0]
        self.owner_path = self.fam_obj.owner_path
        self.model = self.fam_obj.skeleton
        self.blocks = self.fam_obj.base_object.ades
        ref = self.fam_obj.skeleton_ref
        if ref is None or ref.path is None or ref.source != "loose":
            raise core.McpToolError("The skeleton does not come from a loose file: use studio_export_model first")
        self.sklt_path = Path(str(ref.path)).resolve()
        if writable:
            core.ensure_writable(self.base_path)
            core.ensure_writable(self.sklt_path)
        self.base_bytes = base_path.read_bytes()
        self._orig_points = [tuple(p) for p in self.model.points]
        self._orig_polygons = [list(p) for p in self.model.polygons]
        self._orig_blocks = copy.deepcopy([(b.atts, b.olpl) for b in self.blocks])
        self._orig_unmapped = len(self.mapping().unmapped)
        self.block_memory = self._remember_blocks()
        self.log: list[str] = []
        self.warnings: list[str] = []
        self.extra_files: dict[str, bytes] = {}   # new resource files to write beside the BASE (e.g. .ANM)

    @classmethod
    def open(cls, target: str, owner_path: str = "", set_hint: str = "", writable: bool = True) -> "LooseModel":
        return cls(resolve_base(target), owner_path, set_hint, writable)

    def _remember_blocks(self) -> dict:
        """First ATTS values and UV box of every amesh block as opened, keyed by the block's material (texture and
        flags, which survive renumbering): after `clear` empties the blocks, new polygons can still get the usual
        shading and texture patch."""
        memory = {}
        for block in self.blocks:
            if (block.class_id or "").lower() != "amesh.class":
                continue
            uv = [tuple(p) for group in block.olpl for p in group]
            memory[_block_material_metadata(block)] = {
                "density": self._texel_density(block),
                "atts": (block.atts[0].color_val, block.atts[0].shade_val, block.atts[0].tracy_val, block.atts[0].pad)
                if block.atts else None,
                "uv_box": (min(u for u, _ in uv), min(v for _, v in uv), max(u for u, _ in uv), max(v for _, v in uv))
                if uv else None}
        return memory

    def _texel_density(self, block) -> float | None:
        """Texture pixels per model unit that the block's polygons use (square root of UV area over surface area)."""
        surface = texture = 0.0
        for entry, group in zip(block.atts, block.olpl):
            if not 0 <= entry.poly_id < len(self.polygons) or len(group) < 3:
                continue
            area = meshmath.area([self.points[i] for i in self.polygons[entry.poly_id]])
            twice = sum(group[k][0] * group[(k + 1) % len(group)][1] - group[(k + 1) % len(group)][0] * group[k][1]
                        for k in range(len(group)))
            if area > 1e-9 and abs(twice) > 1:
                surface += area
                texture += abs(twice) / 2.0
        return math.sqrt(texture / surface) if surface > 0 and texture > 0 else None

    # --- state ----------------------------------------------------------------------

    @property
    def points(self) -> list:
        return self.model.points

    @property
    def polygons(self) -> list:
        return self.model.polygons

    @property
    def is_root(self) -> bool:
        return self.fam_obj is self.family.root_object

    def require_topology_edit(self) -> None:
        if not self.is_root:
            raise core.McpToolError("Adding or removing points/polygons works on the root object only "
                                    "(this model's other objects can only have their points moved)")

    def mapping(self) -> MappingIndex:
        return MappingIndex(self.fam_obj)

    def block_class(self, index: int) -> str:
        return (self.blocks[index].class_id or "").lower()

    def polygon_slot(self, poly_id: int) -> tuple[int, int]:
        """(block index, ATTS index) that maps this polygon to a material."""
        refs = self.mapping().refs.get(poly_id, [])
        if len(refs) != 1:
            raise core.McpToolError(f"Polygon #{poly_id} has {len(refs)} material mappings (expected 1)")
        return refs[0].block_index, refs[0].atts_index

    def slot_table(self) -> dict[int, tuple[int, int]]:
        table = {}
        for block_index, block in enumerate(self.blocks):
            for atts_index, entry in enumerate(block.atts):
                table[entry.poly_id] = (block_index, atts_index)
        return table

    def check_polygon_ids(self, ids) -> list[int]:
        count = len(self.polygons)
        clean = sorted({int(i) for i in ids})
        for pid in clean:
            if not 0 <= pid < count:
                raise core.McpToolError(f"Polygon #{pid} does not exist (model has {count} polygons: 0..{count - 1})")
        return clean

    def check_point_ids(self, ids) -> list[int]:
        count = len(self.points)
        clean = sorted({int(i) for i in ids})
        for index in clean:
            if not 0 <= index < count:
                raise core.McpToolError(f"Point #{index} does not exist (model has {count} points: 0..{count - 1})")
        return clean

    def polygon_points(self, poly_id: int) -> list:
        return [self.points[i] for i in self.polygons[poly_id]]

    def uv_group(self, poly_id: int, table=None):
        """The UV list of a polygon, or None when its block maps polygons without UVs."""
        block_index, atts_index = (table or self.slot_table())[poly_id]
        block = self.blocks[block_index]
        if block.uses_atts_only_mapping or atts_index >= len(block.olpl):
            return None
        return block.olpl[atts_index]

    def editable_amesh_blocks(self) -> list[int]:
        return [i for i, b in enumerate(self.blocks)
                if (b.class_id or "").lower() == "amesh.class" and b.atts_chunk_offset >= 0]

    def require_amesh_block(self, index: int):
        if not 0 <= index < len(self.blocks):
            raise core.McpToolError(f"Material block #{index} does not exist (0..{len(self.blocks) - 1})")
        block = self.blocks[index]
        if (block.class_id or "").lower() != "amesh.class" or block.atts_chunk_offset < 0:
            raise core.McpToolError(f"Block #{index} is {block.class_id!r}: only amesh.class blocks take new polygons")
        if not block.uses_atts_only_mapping and (block.olpl_chunk_offset < 0 or len(block.atts) != len(block.olpl)):
            raise core.McpToolError(f"Block #{index} has inconsistent ATTS/OLPL data and cannot be edited")
        return block

    # --- low level polygon edits (every method keeps ATTS/OLPL and POL2 in step) --------

    def append_point(self, point) -> int:
        if len(self.points) >= MAX_POINTS:
            raise core.McpToolError("Point limit reached (65536 points per skeleton)")
        x, y, z = (float(v) for v in point)
        if not all(math.isfinite(v) for v in (x, y, z)):
            raise core.McpToolError("Points must be finite numbers")
        self.points.append((x, y, z))
        return len(self.points) - 1

    def append_polygon(self, vertices, block_index: int, atts: tuple[int, int, int, int], uvs) -> int:
        """Add one polygon and map it to an amesh block. ``atts`` is (color, shade, tracy, pad)."""
        if len(self.polygons) >= MAX_POLYGONS:
            raise core.McpToolError("Polygon limit reached (32768 polygons per skeleton)")
        vertices = [int(v) for v in vertices]
        if len(vertices) < 3 or len(set(vertices)) != len(vertices):
            raise core.McpToolError(f"A polygon needs 3 or more different points, got {vertices}")
        for index in vertices:
            if not 0 <= index < len(self.points):
                raise core.McpToolError(f"Point #{index} does not exist")
        block = self.require_amesh_block(block_index)
        poly_id = len(self.polygons)
        self.polygons.append(vertices)
        self.model.parsed_polygon_count = len(self.polygons)
        block.atts.append(AttsEntry(poly_id, *(int(v) for v in atts)))
        if not block.uses_atts_only_mapping:
            block.olpl.append(self.clean_uvs(uvs, len(vertices)))
        return poly_id

    def attach_polygon(self, poly_id: int, block_index: int, atts: tuple[int, int, int, int], uvs) -> None:
        """Map an existing but unmapped polygon to an amesh block."""
        if poly_id in self.slot_table():
            raise core.McpToolError(f"Polygon #{poly_id} already has a material")
        block = self.require_amesh_block(block_index)
        block.atts.append(AttsEntry(poly_id, *(int(v) for v in atts)))
        if not block.uses_atts_only_mapping:
            block.olpl.append(self.clean_uvs(uvs, len(self.polygons[poly_id])))

    def clean_uvs(self, uvs, count: int) -> list[tuple[int, int]]:
        if uvs is None or len(uvs) != count:
            raise core.McpToolError(f"Expected {count} UV pairs, got {None if uvs is None else len(uvs)}")
        result = []
        for pair in uvs:
            u, v = int(round(pair[0])), int(round(pair[1]))
            if not (0 <= u <= UV_MAX and 0 <= v <= UV_MAX):
                self.warnings.append(f"UV ({u}, {v}) clamped into 0..{UV_MAX}")
                u, v = min(max(u, 0), UV_MAX), min(max(v, 0), UV_MAX)
            result.append((u, v))
        return result

    def set_uvs(self, poly_id: int, uvs, table=None) -> bool:
        """Replace the UVs of a polygon; returns False when its block has no UVs to set."""
        block_index, atts_index = (table or self.slot_table())[poly_id]
        block = self.blocks[block_index]
        if self.block_class(block_index) != "amesh.class":
            raise core.McpToolError(f"Polygon #{poly_id} belongs to a {block.class_id} block: its UVs are not editable here")
        if block.uses_atts_only_mapping:
            return False
        block.olpl[atts_index] = self.clean_uvs(uvs, len(self.polygons[poly_id]))
        return True

    def reverse_polygon(self, poly_id: int, table=None) -> None:
        block_index, atts_index = (table or self.slot_table())[poly_id]
        if self.block_class(block_index) != "amesh.class":
            raise core.McpToolError(f"Polygon #{poly_id} belongs to a {self.blocks[block_index].class_id} block and cannot be flipped")
        self.polygons[poly_id] = list(reversed(self.polygons[poly_id]))
        block = self.blocks[block_index]
        if not block.uses_atts_only_mapping:
            block.olpl[atts_index] = list(reversed(block.olpl[atts_index]))

    def set_polygon_vertices(self, poly_id: int, vertices, keep_positions=None, table=None) -> None:
        """Replace a polygon's point list. If it gets shorter, ``keep_positions`` names the old corners that
        stay (their UVs are kept, the others are dropped)."""
        old = self.polygons[poly_id]
        vertices = [int(v) for v in vertices]
        block_index, atts_index = (table or self.slot_table())[poly_id]
        block = self.blocks[block_index]
        if len(vertices) != len(old):
            if self.block_class(block_index) != "amesh.class":
                raise core.McpToolError(f"Polygon #{poly_id} belongs to a {block.class_id} block: its vertex count cannot change")
            if keep_positions is None or len(keep_positions) != len(vertices):
                raise core.McpToolError(f"Polygon #{poly_id}: which corners keep their UVs is not defined")
            if not block.uses_atts_only_mapping:
                block.olpl[atts_index] = [block.olpl[atts_index][k] for k in keep_positions]
        self.polygons[poly_id] = vertices

    def move_polygon_to_block(self, poly_id: int, target_block: int, atts=None, uvs=None, table=None) -> None:
        """Re-map a polygon to another amesh block (its UVs and ATTS values are kept unless given)."""
        table = table or self.slot_table()
        block_index, atts_index = table[poly_id]
        if self.block_class(block_index) != "amesh.class":
            raise core.McpToolError(f"Polygon #{poly_id} belongs to a {self.blocks[block_index].class_id} block and cannot change material")
        target = self.require_amesh_block(target_block)
        if block_index == target_block:
            if atts is not None:
                entry = target.atts[atts_index]
                target.atts[atts_index] = AttsEntry(entry.poly_id, *atts)
            if uvs is not None:
                self.set_uvs(poly_id, uvs, table)
            return
        source = self.blocks[block_index]
        entry = source.atts.pop(atts_index)
        old_uvs = None if source.uses_atts_only_mapping or atts_index >= len(source.olpl) else source.olpl.pop(atts_index)
        # the source block may have lost its last UV group: it is then a plain ATTS-only block
        new_atts = AttsEntry(entry.poly_id, *(atts if atts is not None else
                                              (entry.color_val, entry.shade_val, entry.tracy_val, entry.pad)))
        target.atts.append(new_atts)
        if not target.uses_atts_only_mapping:
            group = uvs if uvs is not None else old_uvs
            if group is None:
                group = [(0, 0)] * len(self.polygons[poly_id])
                self.warnings.append(f"Polygon #{poly_id} got placeholder UVs: set them with studio_set_uvs")
            target.olpl.append(self.clean_uvs(group, len(self.polygons[poly_id])))

    def pinned_points(self) -> set[int]:
        """Points that other data points at by index (emitters and other non-mesh blocks)."""
        return {b.ade_point_id for b in self.blocks
                if (b.class_id or "").lower() not in ("amesh.class", "area.class") and b.ade_point_id >= 0}

    def _emitter_blocks(self) -> list:
        return [b for b in self.blocks
                if (b.class_id or "").lower() not in ("amesh.class", "area.class") and b.ade_point_id >= 0]

    def _retarget_emitters(self, remap: dict[int, int], old_positions: list) -> None:
        """After points were renumbered: emitters keep their place. A block follows its point to the new index;
        if the point disappeared (no polygon uses it) it is re-created at the same position."""
        for block in self._emitter_blocks():
            old = block.ade_point_id
            if old in remap:
                new = remap[old]
            elif 0 <= old < len(old_positions):
                new = self.append_point(old_positions[old])
            else:
                continue
            if new != old:
                block.source_objt_bytes = rewrite_ade_point_template(block, new)
                block.ade_point_id = new

    def delete_polygons(self, poly_ids) -> dict:
        """Delete polygons with Studio's own delete plan (renumbers polygons, drops unused points)."""
        ids = self.check_polygon_ids(poly_ids)
        if not ids:
            return {"deleted": 0, "polygon_remap": {}, "point_remap": {}}
        fx = detect_fx_elements(self.fam_obj, self.family.animations)
        unsafe = unsafe_fx_geometry_polys(fx, self.owner_path, ids)
        old_positions = [tuple(p) for p in self.points]
        try:
            plan = plan_delete_geometry(self.fam_obj, ids, self.mapping(), unsafe, allow_empty=True)
            result = apply_delete_geometry(self.model, self.blocks, plan)
        except (GeometryClipboardError, MappingEditError) as exc:
            raise core.McpToolError(f"Cannot delete: {exc}") from exc
        point_remap = dict(result.old_to_new_point)
        self._retarget_emitters(point_remap, old_positions)
        return {"deleted": len(ids), "polygon_remap": dict(result.old_to_new_polygon), "point_remap": point_remap}

    def compact_points(self) -> dict[int, int]:
        """Drop points no polygon uses and renumber the rest (emitter points stay attached); old->new map."""
        used = sorted({i for poly in self.polygons for i in poly} | self.pinned_points())
        used = [i for i in used if i < len(self.points)]
        if len(used) == len(self.points):
            return {i: i for i in used}
        remap = {old: new for new, old in enumerate(used)}
        old_positions = [tuple(p) for p in self.points]
        self.points[:] = [self.points[i] for i in used]
        self.polygons[:] = [[remap[i] for i in poly] for poly in self.polygons]
        self._retarget_emitters(remap, old_positions)
        return remap

    # --- commit -------------------------------------------------------------------

    @property
    def points_changed(self) -> bool:
        return [tuple(p) for p in self.points] != self._orig_points

    @property
    def topology_changed(self) -> bool:
        return [list(p) for p in self.polygons] != self._orig_polygons or len(self.points) != len(self._orig_points)

    @property
    def blocks_changed(self) -> bool:
        return copy.deepcopy([(b.atts, b.olpl) for b in self.blocks]) != self._orig_blocks

    @property
    def dirty(self) -> bool:
        return self.points_changed or self.topology_changed or self.blocks_changed

    def _final_checks(self) -> None:
        mapping = self.mapping()
        problems = []
        if mapping.invalid:
            problems.append(f"ATTS entries pointing at missing polygons: {mapping.invalid[:4]}")
        if mapping.duplicates:
            problems.append(f"polygons mapped twice: {sorted(mapping.duplicates)[:8]}")
        missing = [i for i, poly in enumerate(self.polygons) if len(poly) >= 3 and i not in mapping.refs]
        if len(missing) > self._orig_unmapped:
            problems.append(f"polygons without material: {missing[:8]} (studio_map_polygons gives them one)")
        elif missing:
            self.warnings.append(f"{len(missing)} polygon(s) still have no material (they were like that before)")
        for index, poly in enumerate(self.polygons):
            if len(set(poly)) != len(poly) or any(not 0 <= v < len(self.points) for v in poly):
                problems.append(f"polygon #{index} has repeated or missing points: {poly}")
                break
        if problems:
            raise core.McpToolError("Edit refused, nothing was written: " + "; ".join(problems))

    def commit(self, label: str) -> dict:
        """Write the edited model. Verifies the staged files, snapshots the old ones, swaps atomically."""
        if not self.writable:
            raise core.McpToolError("This model was opened read-only")
        if not self.dirty:
            return {"written": False, "note": "no change", **self.summary()}
        self._final_checks()
        structural_sklt = self.topology_changed
        stage = core.work_dir() / f"stage_{uuid.uuid4().hex[:8]}"
        stage.mkdir(parents=True)
        try:
            staged_sklt = stage / self.sklt_path.name
            staged_base = stage / self.base_path.name
            if structural_sklt:
                save_sklt_with_poo2_pol2_structure(self.model, self.points, self.polygons, staged_sklt)
            elif self.points_changed:
                save_sklt_with_poo2_points(self.model, self.points, staged_sklt, update_sensors=False)
            write_base = self.topology_changed or self.blocks_changed
            if write_base:
                states = build_topology_states(self.base_bytes, self.fam_obj, self.family.animations)
                save_model_base_copy(self.base_bytes, [], [], staged_base, structural_blocks=states)
            self._verify_staged(staged_sklt if staged_sklt.exists() else None,
                                staged_base if write_base else None)
            touched = [self.sklt_path] + ([self.base_path] if write_base else [])
            for name, data in self.extra_files.items():
                (stage / name).write_bytes(data)
            history.snapshot(self.base_path, [self.base_path, self.sklt_path], label)
            for source, target in ((staged_sklt, self.sklt_path), (staged_base, self.base_path)):
                if source.exists():
                    os.replace(source, target)
            for name in self.extra_files:
                target = core.ensure_writable(self.base_path.parent / name)
                os.replace(stage / name, target)
                touched.append(target)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        return {"written": True, "files": [str(p) for p in touched], **self.summary()}

    def _verify_staged(self, sklt: Path | None, base: Path | None) -> None:
        if sklt is not None:
            parsed = parse_sklt_file(sklt)
            if len(parsed.points) != len(self.points) or parsed.polygons != [list(p) for p in self.polygons]:
                raise core.McpToolError("Staged skeleton does not match the edit; nothing was written")
            for a, b in zip(parsed.points, self.points):
                if any(abs(a[i] - b[i]) > 1e-3 + abs(b[i]) * 1e-5 for i in range(3)):
                    raise core.McpToolError("Staged skeleton points differ from the edit; nothing was written")
        if base is not None:
            asset = parse_base_bytes(base.read_bytes(), "<staged-base>")
            if asset.root is None or len(asset.root.ades) != len(self.blocks):
                raise core.McpToolError("Staged BASE failed to re-parse; nothing was written")
            for saved, current in zip(asset.root.ades, self.blocks):
                if [tuple(vars(e).values()) for e in saved.atts] != [tuple(vars(e).values()) for e in current.atts]:
                    raise core.McpToolError("Staged BASE material mapping differs from the edit; nothing was written")
                if not current.uses_atts_only_mapping and \
                        [[tuple(uv) for uv in g] for g in saved.olpl] != [[tuple(uv) for uv in g] for g in current.olpl]:
                    raise core.McpToolError("Staged BASE UVs differ from the edit; nothing was written")

    def summary(self) -> dict:
        return {"model": str(self.base_path), "skeleton": str(self.sklt_path), "points": len(self.points),
                "polygons": len(self.polygons), "bbox": meshmath.bbox(self.points)}

