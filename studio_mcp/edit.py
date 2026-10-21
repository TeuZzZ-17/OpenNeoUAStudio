"""Modeling operations of the Studio MCP.

Every operation is a function ``op_<name>(ctx, **args)`` registered in OPS. The server exposes each one as its
own tool AND lets an agent chain several in one atomic call (``studio_edit_batch``): the model is edited in
memory and written once at the end, after verification. If any step fails nothing is written.

Conventions: x right, y DOWN (negative = up), z forward. The visible side of a polygon is given by
``meshmath.outward_normal``. Selectors are described in ``select.py``.
"""

from __future__ import annotations

import inspect
import math
import random
from typing import NamedTuple
from collections import defaultdict
from dataclasses import dataclass, field, replace

from studio.assets.mapping import _block_material_metadata, MappingIndex
from studio.rendering.geometry import (
    append_geometry_clipboard,
    build_geometry_clipboard,
    detect_fx_elements,
    GeometryClipboardError,
    unsafe_fx_geometry_polys,
)
from studio_mcp import core, meshmath, shapes, uvs as uvtools
from studio_mcp.loose import LooseModel
from studio_mcp.select import DIRECTIONS, MeshView, select_points, select_polygons, view_of

OPS: dict = {}
MAX_SHOWN = 150

# MCP tool name of each operation (the default is studio_<op>)
TOOL_NAMES = {
    "transform": "studio_transform_points", "set_points": "studio_set_points", "snap": "studio_snap_points",
    "smooth": "studio_smooth_points", "symmetrize": "studio_symmetrize_model", "fit": "studio_fit_model",
    "flip": "studio_flip_polygons", "mirror": "studio_mirror_polygons", "duplicate": "studio_duplicate_polygons",
    "extrude": "studio_extrude_polygons", "subdivide": "studio_subdivide_polygons", "weld": "studio_weld_points",
    "clear": "studio_clear_model", "cleanup": "studio_cleanup_model",
}


def tool_name(op_name: str) -> str:
    return TOOL_NAMES.get(op_name, f"studio_{op_name}")


def op(name: str):
    def register(function):
        OPS[name] = function
        function.op_name = name
        return function
    return register


@dataclass
class Context:
    lm: LooseModel
    named_points: dict = field(default_factory=dict)
    named_polygons: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)

    def view(self) -> MeshView:
        view = view_of(self.lm)
        view.named_points, view.named_polygons = self.named_points, self.named_polygons
        return view

    def points(self, selector) -> list[int]:
        return select_points(selector, self.view())

    def polygons(self, selector) -> list[int]:
        return select_polygons(selector, self.view())

    def apply_remap(self, point_remap: dict | None, polygon_remap: dict | None) -> None:
        """Keep saved selections valid after points/polygons were renumbered."""
        if point_remap is not None:
            self.named_points = {n: sorted(point_remap[i] for i in ids if i in point_remap)
                                 for n, ids in self.named_points.items()}
        if polygon_remap is not None:
            self.named_polygons = {n: sorted(polygon_remap[i] for i in ids if i in polygon_remap)
                                   for n, ids in self.named_polygons.items()}


def _vec(value, name: str, default=None) -> tuple:
    if value is None:
        if default is None:
            raise core.McpToolError(f"{name} is required")
        return default
    if isinstance(value, (int, float)):
        return (float(value),) * 3
    if len(value) != 3:
        raise core.McpToolError(f"{name} must be [x, y, z]")
    return tuple(float(v) for v in value)


def _need(selector, kind: str):
    if selector is None:
        raise core.McpToolError(f"This operation needs a '{kind}' selector (use 'all' to mean every {kind})")
    return selector


# --- points: moving things around -------------------------------------------------------


def _symmetric_followers(lm, chosen: list[int], before: list, mirror, tolerance) -> tuple[list, int]:
    """Apply the editor's Mirror Select: counterparts of the moved points follow, mirrored.

    ``before`` are all point positions before the move; the moved ones are already updated in lm.points."""
    if not mirror:
        return [], 0
    from studio.rendering.geometry import match_mirror_points, propagate_mirror_transform
    from studio_mcp.select import mirror_axes
    axes = mirror_axes(mirror)
    match = match_mirror_points(before, chosen, axes, tolerance)
    pending = [tuple(p) for p in lm.points]
    propagate_mirror_transform(before, pending, set(chosen), match, match.tolerance, axes)
    followers = [i for i in range(len(pending)) if i not in set(chosen) and pending[i] != tuple(lm.points[i])]
    for i in followers:
        lm.points[i] = meshmath.rounded(pending[i], 4)
    return followers, len(match.ambiguous_vertices)


@op("transform")
def op_transform(ctx, select=None, translate=None, rotate_deg=None, scale=None, pivot="center",
                 jitter: float = 0.0, seed: int = 0, mirror=None, mirror_tolerance=None, save_as=None):
    """Move / rotate / scale chosen points. pivot: [x,y,z] | 'center' (of the selection) | 'origin' | 'min' | 'max'.
    mirror: 'x', 'z', 'xz', 'xyz'... = the editor's Mirror Select: the mirror counterparts of the moved points
    follow symmetrically (mirror planes pass through the origin)."""
    lm = ctx.lm
    chosen = ctx.points(select)
    if not chosen:
        raise core.McpToolError("The selector matched no points")
    translate, rotate, factor = _vec(translate, "translate", (0, 0, 0)), _vec(rotate_deg, "rotate_deg", (0, 0, 0)), _vec(scale, "scale", (1, 1, 1))
    selected = [lm.points[i] for i in chosen]
    if isinstance(pivot, str):
        box = meshmath.bbox(selected)
        origin = {"origin": (0.0, 0.0, 0.0), "center": tuple((a + b) / 2 for a, b in zip(box["min"], box["max"])),
                  "min": tuple(box["min"]), "max": tuple(box["max"])}.get(pivot)
        if origin is None:
            raise core.McpToolError("pivot must be [x,y,z], 'center', 'origin', 'min' or 'max'")
    else:
        origin = _vec(pivot, "pivot")
    before = [tuple(p) for p in lm.points]
    rx, ry, rz = (math.radians(a) for a in rotate)
    rng = random.Random(seed)
    for i in chosen:
        p = meshmath.sub(lm.points[i], origin)
        p = tuple(p[k] * factor[k] for k in range(3))
        p = meshmath.rotate_xyz(p, rx, ry, rz)
        p = tuple(p[k] + origin[k] + translate[k] + (rng.uniform(-jitter, jitter) if jitter else 0.0) for k in range(3))
        lm.points[i] = meshmath.rounded(p, 4)
    followers, ambiguous = _symmetric_followers(lm, chosen, before, mirror, mirror_tolerance)
    if ambiguous:
        ctx.notes.append(f"{ambiguous} point(s) have more than one mirror candidate and did not follow")
    result = {"moved": len(chosen), "points": chosen}
    if mirror:
        result["followed"] = followers
    return result


@op("set_points")
def op_set_points(ctx, updates=None, all_points=None):
    """Set exact coordinates: updates = [[index, [x,y,z]], ...] or {index: [x,y,z]}; or all_points = full list."""
    lm = ctx.lm
    if all_points is not None:
        if len(all_points) != len(lm.points):
            raise core.McpToolError(f"all_points needs {len(lm.points)} points, got {len(all_points)}")
        pairs = list(enumerate(all_points))
    elif updates is not None:
        pairs = list(updates.items()) if isinstance(updates, dict) else list(updates)
    else:
        raise core.McpToolError("give 'updates' or 'all_points'")
    done = []
    for index, position in pairs:
        index = lm.check_point_ids([int(index)])[0]
        lm.points[index] = meshmath.rounded(_vec(position, f"position of point {index}"), 4)
        done.append(index)
    return {"set": len(done), "points": done}


@op("snap")
def op_snap(ctx, select=None, mode: str = "grid", step: float = 1.0, axis="y", value: float = 0.0):
    """mode 'grid': round coordinates to multiples of step.  mode 'plane': flatten onto axis = value."""
    lm = ctx.lm
    chosen = ctx.points(select)
    if mode == "grid":
        if step <= 0:
            raise core.McpToolError("step must be greater than 0")
        for i in chosen:
            lm.points[i] = meshmath.rounded(tuple(round(c / step) * step for c in lm.points[i]), 4)
    elif mode == "plane":
        a = meshmath.axis_index(axis)
        for i in chosen:
            p = list(lm.points[i])
            p[a] = float(value)
            lm.points[i] = tuple(p)
    else:
        raise core.McpToolError("mode must be 'grid' or 'plane'")
    return {"snapped": len(chosen), "points": chosen}


@op("smooth")
def op_smooth(ctx, select=None, iterations: int = 1, strength: float = 0.5, keep_boundary: bool = True):
    """Relax points toward the average of their neighbours (rounds off lumpy shapes)."""
    lm = ctx.lm
    chosen = set(ctx.points(select))
    neighbours, edge_count = defaultdict(set), defaultdict(int)
    for poly in lm.polygons:
        for k, a in enumerate(poly):
            b = poly[(k + 1) % len(poly)]
            neighbours[a].add(b)
            neighbours[b].add(a)
            edge_count[frozenset((a, b))] += 1
    boundary = {i for edge, n in edge_count.items() if n == 1 for i in edge} if keep_boundary else set()
    strength = min(max(float(strength), 0.0), 1.0)
    for _ in range(max(1, int(iterations))):
        updated = {}
        for i in chosen:
            if i in boundary or not neighbours[i]:
                continue
            mean = meshmath.centroid([lm.points[j] for j in neighbours[i]])
            updated[i] = meshmath.lerp(lm.points[i], mean, strength)
        for i, position in updated.items():
            lm.points[i] = meshmath.rounded(position, 4)
    return {"smoothed": len(chosen - boundary), "points": sorted(chosen - boundary)}


@op("symmetrize")
def op_symmetrize(ctx, axis="x", source: str = "positive", tolerance: float = 0.5, plane: float = 0.0):
    """Make the model mirror-symmetric across axis = plane. source: 'positive' side wins, 'negative' side wins,
    or 'average'. Points on the plane are put exactly on it. Unpaired points are reported, not changed."""
    lm = ctx.lm
    a = meshmath.axis_index(axis)
    if source not in ("positive", "negative", "average"):
        raise core.McpToolError("source must be 'positive', 'negative' or 'average'")

    def reflected(p):
        q = list(p)
        q[a] = 2 * plane - p[a]
        return tuple(q)

    on_plane, pairs, unpaired, used = [], [], [], set()
    order = sorted(range(len(lm.points)), key=lambda i: lm.points[i][a], reverse=(source != "negative"))
    for i in order:
        p = lm.points[i]
        if abs(p[a] - plane) <= tolerance:
            on_plane.append(i)
            continue
        if i in used or (source != "average" and ((p[a] > plane) != (source == "positive"))):
            continue
        target = reflected(p)
        best = min((j for j in range(len(lm.points)) if j != i and j not in used),
                   key=lambda j: meshmath.distance(lm.points[j], target), default=None)
        if best is not None and meshmath.distance(lm.points[best], target) <= tolerance * 2:
            pairs.append((i, best))
            used.update((i, best))
        else:
            unpaired.append(i)
    for i in on_plane:
        p = list(lm.points[i])
        p[a] = plane
        lm.points[i] = tuple(p)
    for keep, other in pairs:
        if source == "average":
            mid = meshmath.lerp(lm.points[keep], reflected(lm.points[other]), 0.5)
            lm.points[keep], lm.points[other] = meshmath.rounded(mid, 4), meshmath.rounded(reflected(mid), 4)
        else:
            lm.points[other] = meshmath.rounded(reflected(lm.points[keep]), 4)
    return {"pairs": len(pairs), "on_plane": len(on_plane), "unpaired": unpaired[:MAX_SHOWN]}


@op("center_at_origin")
def op_center_at_origin(ctx, axes: str = "xyz"):
    """Move the whole model so the center of its bounding box is at the origin (only points used by polygons
    count, like the editor's Center Model at Origin). axes limits it, e.g. 'xz' keeps the height."""
    lm = ctx.lm
    used = sorted({i for ring in lm.polygons for i in ring}) or list(range(len(lm.points)))
    if not used:
        raise core.McpToolError("The model has no points")
    box = meshmath.bbox([lm.points[i] for i in used])
    shift = [0.0, 0.0, 0.0]
    for ch in str(axes).lower():
        k = meshmath.axis_index(ch)
        shift[k] = -(box["min"][k] + box["max"][k]) / 2
    for i, p in enumerate(lm.points):
        lm.points[i] = meshmath.rounded(meshmath.add(p, shift), 4)
    return {"offset": [round(v, 4) for v in shift], "bbox": meshmath.bbox(lm.points)}


@op("fit")
def op_fit(ctx, size=None, uniform: bool = True, recenter=None):
    """Scale the whole model to a target size [sx, sy, sz] (use null for 'free' axes). uniform=true applies the
    factor of the first given axis to all axes. recenter: axes to center on 0, e.g. 'xz'."""
    lm = ctx.lm
    box = meshmath.bbox(lm.points)
    if not box:
        raise core.McpToolError("The model has no points")
    current = box["size"]
    if size is not None:
        if len(size) != 3:
            raise core.McpToolError("size must be [sx, sy, sz] (null = leave that axis)")
        factors = [1.0, 1.0, 1.0]
        given = [k for k in range(3) if size[k] is not None]
        if not given:
            raise core.McpToolError("give at least one axis in size")
        for k in given:
            if current[k] <= 1e-9:
                raise core.McpToolError(f"The model has no extent on axis {'xyz'[k]}")
            factors[k] = float(size[k]) / current[k]
        if uniform:
            factors = [factors[given[0]]] * 3
        center = [(box["min"][k] + box["max"][k]) / 2 for k in range(3)]
        for i, p in enumerate(lm.points):
            lm.points[i] = meshmath.rounded(tuple(center[k] + (p[k] - center[k]) * factors[k] for k in range(3)), 4)
    if recenter:
        after = meshmath.bbox(lm.points)
        shift = [0.0, 0.0, 0.0]
        for ch in str(recenter).lower():
            k = meshmath.axis_index(ch)
            shift[k] = -(after["min"][k] + after["max"][k]) / 2
        for i, p in enumerate(lm.points):
            lm.points[i] = meshmath.rounded(meshmath.add(p, shift), 4)
    return {"bbox": meshmath.bbox(lm.points)}


# --- adding and removing geometry ----------------------------------------------------------


@op("add_points")
def op_add_points(ctx, points=None, save_as=None):
    """Append free points (they belong to no polygon until add_polygons uses them). Returns their indices."""
    if not points:
        raise core.McpToolError("give 'points': [[x,y,z], ...]")
    created = [ctx.lm.append_point(_vec(p, "point")) for p in points]
    return {"points": created}


class MaterialChoice(NamedTuple):
    """Where new polygons get their material from."""

    block: int
    atts: tuple
    reference_uvs: list | None
    reference_poly: int | None
    density: float | None        # texture pixels per model unit used by the block's existing polygons
    region: tuple | None = None  # the part of the texture those polygons use (u0, v0, u1, v1)


def _reference_for(lm, material: dict | None) -> MaterialChoice:
    """Block, ATTS values, reference UVs and texel density for new polygons."""
    material = dict(material or {})
    table = lm.slot_table()
    reference_poly = material.get("like_polygon")
    if reference_poly is not None:
        if not lm.polygons:
            raise core.McpToolError("The model has no polygons to copy a material from: use material={'block': N} "
                                    f"(amesh blocks: {lm.editable_amesh_blocks()}, see studio_materials)")
        reference_poly = lm.check_polygon_ids([reference_poly])[0]
        block_index, atts_index = table[reference_poly]
        entry = lm.blocks[block_index].atts[atts_index]
        values = (entry.color_val, entry.shade_val, entry.tracy_val, entry.pad)
        reference_uvs = lm.uv_group(reference_poly, table)
        known = lm.block_memory.get(_block_material_metadata(lm.blocks[block_index]), {})
        density, region = known.get("density"), known.get("uv_box")
    else:
        if "block" in material:
            block_index = int(material["block"])
        else:
            candidates = [i for i in lm.editable_amesh_blocks() if not lm.blocks[i].uses_atts_only_mapping]
            if len(candidates) != 1:
                listing = [f"#{i}: {lm.blocks[i].texture.name if lm.blocks[i].texture else 'no texture'}"
                           for i in lm.editable_amesh_blocks()]
                raise core.McpToolError("Say which material to use: material={'block': N} or {'like_polygon': ID}. "
                                        f"Editable blocks: {listing}")
            block_index = candidates[0]
        block = lm.require_amesh_block(block_index)
        first = block.atts[0] if block.atts else None
        remembered = lm.block_memory.get(_block_material_metadata(block), {})
        values = (first.color_val, first.shade_val, first.tracy_val, first.pad) if first else (
            remembered.get("atts") or (0, 0, 128, 0))
        box = remembered.get("uv_box")
        reference_uvs = [(box[0], box[1]), (box[2], box[3])] if box else None
        density, region = remembered.get("density"), remembered.get("uv_box")
    if "atts" in material:
        given = material["atts"]
        values = (int(given.get("color", values[0])), int(given.get("shade", values[1])),
                  int(given.get("tracy", values[2])), int(given.get("pad", values[3])))
    lm.require_amesh_block(block_index)
    return MaterialChoice(block_index, values, reference_uvs, reference_poly, density, region)


def _uv_spec(lm, spec: dict | None, reference_uvs, density: float | None = None, region=None):
    """Resolve 'from_polygon' inside a UV spec into reference UVs. Without a mode, a known texel density of the
    material gives 'world' UVs (same pixel size as the original surfaces)."""
    spec = dict(spec or {})
    if "mode" not in spec and "from_polygon" not in spec and density:
        spec = {"mode": "world", "texels_per_unit": round(density, 3), **({"region": list(region)} if region else {}), **spec}
    if "from_polygon" in spec:
        source = lm.check_polygon_ids([spec["from_polygon"]])[0]
        reference_uvs = lm.uv_group(source)
        if reference_uvs is None:
            raise core.McpToolError(f"Polygon #{source} has no UVs to copy")
        if "mode" not in spec:
            spec["mode"] = "copy"
    return spec, reference_uvs


def _find_or_add_point(lm, position, tolerance: float) -> int:
    best, best_distance = None, tolerance
    for i, p in enumerate(lm.points):
        d = meshmath.distance(p, position)
        if d <= best_distance:
            best, best_distance = i, d
    return best if best is not None else lm.append_point(position)


@op("add_polygons")
def op_add_polygons(ctx, polygons=None, material=None, uv=None, weld_tolerance: float = 0.001,
                    double_sided: bool = False, flip: bool = False, save_as=None):
    """Add polygons. Each polygon is a list of corners; a corner is a point index or a new [x,y,z] position.
    material: {'block': N} or {'like_polygon': ID} (plus optional 'atts': {color, shade, tracy}).
    uv: a UV spec (see studio_help('uv')); default copies the UVs of like_polygon, else planar."""
    lm = ctx.lm
    lm.require_topology_edit()
    if not polygons:
        raise core.McpToolError("give 'polygons': [[corner, corner, corner, ...], ...]")
    choice = _reference_for(lm, material)
    block_index, atts = choice.block, choice.atts
    spec, reference_uvs = _uv_spec(lm, uv, choice.reference_uvs, choice.density, choice.region)
    first_new_point = len(lm.points)
    created_polys = []
    for number, corners in enumerate(polygons):
        ring = []
        for corner in corners:
            index = int(corner) if isinstance(corner, int) and not isinstance(corner, bool) else \
                _find_or_add_point(lm, _vec(corner, f"corner of polygon {number}"), weld_tolerance)
            ring.append(index)
        if flip:
            ring.reverse()
        pts = [lm.points[i] for i in ring]
        if len(set(ring)) != len(ring) or len(ring) < 3:
            raise core.McpToolError(f"Polygon {number} {corners}: needs 3+ different corners")
        if meshmath.area(pts) < 1e-9:
            ctx.notes.append(f"polygon {number} has no area (corners on a line)")
        for ring_try in ([ring, list(reversed(ring))] if double_sided else [ring]):
            pts = [lm.points[i] for i in ring_try]
            group = uvtools.compute(spec, pts, reference=reference_uvs, notes=ctx.notes)
            created_polys.append(lm.append_polygon(ring_try, block_index, atts, group))
    return {"polygons": created_polys, "points": list(range(first_new_point, len(lm.points))), "block": block_index}


@op("create_primitive")
def op_create_primitive(ctx, shape: str = "box", center=None, size=None, segments: int = 8, rings: int = 4,
                        top_scale=1.0, taper=1.0, caps: str = "both", rotate_deg=None, phase_deg=None, cells=(1, 1),
                        material=None, uv=None, flip: bool = False, save_as=None):
    """Add a ready-made solid: box (taper for pyramids/trapezoids), wedge, prism (cylinder, cone via top_scale=0),
    sphere, plane. size is the full extent [sx, sy, sz]; y down, so the 'top' is negative y."""
    lm = ctx.lm
    lm.require_topology_edit()
    points, polygons = shapes.make(shape, _vec(center, "center", (0, 0, 0)), _vec(size, "size", (2, 2, 2)), segments, rings,
                                   top_scale, taper, caps, _vec(rotate_deg, "rotate_deg", (0, 0, 0)), phase_deg, cells)
    choice = _reference_for(lm, material)
    block_index, atts = choice.block, choice.atts
    spec, reference_uvs = _uv_spec(lm, uv, choice.reference_uvs, choice.density, choice.region)
    first = len(lm.points)
    for p in points:
        lm.append_point(meshmath.rounded(p, 4))
    created = []
    for ring in polygons:
        ring = [first + i for i in (reversed(ring) if flip else ring)]
        group = uvtools.compute(spec, [lm.points[i] for i in ring], reference=reference_uvs, notes=ctx.notes)
        created.append(lm.append_polygon(ring, block_index, atts, group))
    return {"polygons": created, "points": list(range(first, len(lm.points))), "block": block_index}


@op("delete_polygons")
def op_delete_polygons(ctx, select=None):
    """Delete polygons (points only they used disappear too; indices after them shift down)."""
    lm = ctx.lm
    lm.require_topology_edit()
    ids = ctx.polygons(_need(select, "polygon"))
    result = lm.delete_polygons(ids)
    ctx.apply_remap(result["point_remap"], result["polygon_remap"])
    return {"deleted": result["deleted"], "polygons_left": len(lm.polygons), "points_left": len(lm.points),
            "renumbered": True}


@op("clear")
def op_clear(ctx):
    """Delete every polygon (an empty shell keeps its materials: build a new model on top of it)."""
    lm = ctx.lm
    lm.require_topology_edit()
    result = lm.delete_polygons(range(len(lm.polygons)))
    ctx.apply_remap(result["point_remap"], result["polygon_remap"])
    blocks = [{"block": i, "texture": b.texture.name if b.texture else None}
              for i, b in enumerate(lm.blocks) if (b.class_id or "").lower() == "amesh.class"]
    return {"deleted": result["deleted"], "amesh_blocks_left": blocks,
            "note": "animated (area) blocks went with their polygons, so block numbers may have changed"}


@dataclass
class PolyRecord:
    """One polygon of a model, detached from it: corner point indices, material block, ATTS values, UVs."""

    ring: list
    block: int
    atts: tuple
    uvs: list | None


def records_of(obj, ids) -> tuple[list[PolyRecord], list[int]]:
    """Detach amesh polygons of a family object; returns (records, ids that are not plain amesh polygons)."""
    refs = MappingIndex(obj).refs
    records, others = [], []
    for pid in ids:
        found = refs.get(pid, [])
        if len(found) != 1 or (found[0].block.class_id or "").lower() != "amesh.class":
            others.append(pid)
            continue
        ref = found[0]
        block, entry = ref.block, ref.block.atts[ref.atts_index]
        uvs = None if block.uses_atts_only_mapping else [tuple(uv) for uv in block.olpl[ref.atts_index]]
        records.append(PolyRecord(list(obj.skeleton.polygons[pid]), ref.block_index,
                                  (entry.color_val, entry.shade_val, entry.tracy_val, entry.pad), uvs))
    return records, others


def paste_records(lm, records, source_points, block_map=None, notes=None) -> tuple[list[int], list[int]]:
    """Add detached polygons to a model with their own new points; returns (new polygon ids, new point ids)."""
    block_map = block_map or {}
    point_map: dict[int, int] = {}
    new_polys = []
    for record in records:
        ring = []
        for index in record.ring:
            if index not in point_map:
                point_map[index] = lm.append_point(source_points[index])
            ring.append(point_map[index])
        target = block_map.get(record.block, record.block)
        uvs = record.uvs
        if uvs is None and not lm.require_amesh_block(target).uses_atts_only_mapping:
            uvs = uvtools.fit([lm.points[i] for i in ring])
            if notes is not None:
                notes.append("polygons from an untextured material got planar UVs")
        new_polys.append(lm.append_polygon(ring, target, record.atts, uvs))
    return new_polys, sorted(point_map.values())


def clipboard_copy(lm, obj, ids, notes) -> tuple[list[int], list[int]]:
    """Copy animated/area polygons with the editor's own clipboard (it also carries their typed material).

    The clipboard refuses some polygons (shared FX, animations without UVs); those are skipped one by one
    with a note, so one refusal does not stop the rest."""
    fx = detect_fx_elements(obj, lm.family.animations if obj is lm.fam_obj else {})
    polys, points, skipped = [], [], []
    for pid in ids:
        try:
            clipboard = build_geometry_clipboard(obj, obj.owner_path, [pid], MappingIndex(obj),
                                                 unsafe_fx_geometry_polys(fx, obj.owner_path, [pid]))
            clipboard = replace(clipboard, model_identity=id(lm.model), owner=lm.owner_path)
            result = append_geometry_clipboard(lm.model, lm.blocks, clipboard, (0.0, 0.0, 0.0))
        except GeometryClipboardError as exc:
            skipped.append(f"#{pid} ({exc})")
            continue
        polys += list(result.polygon_indices)
        points += list(result.point_indices)
    if skipped:
        notes.append("not copied: " + "; ".join(skipped[:6]) + (f" ... {len(skipped)} in total" if len(skipped) > 6 else "")
                     + ". FX polygons travel with studio_add_fx.")
    return polys, points


@op("duplicate")
def op_duplicate(ctx, select=None, translate=None, rotate_deg=None, scale=None, save_as=None):
    """Copy polygons (with their material and UVs) as free-standing geometry, optionally moved/rotated/scaled
    around the copy's center. Returns the new polygon and point indices."""
    lm = ctx.lm
    lm.require_topology_edit()
    ids = ctx.polygons(_need(select, "polygon"))
    if not ids:
        raise core.McpToolError("The selector matched no polygons")
    records, others = records_of(lm.fam_obj, ids)
    new_polys, new_points = paste_records(lm, records, lm.points, notes=ctx.notes)
    if others:
        more_polys, more_points = clipboard_copy(lm, lm.fam_obj, others, ctx.notes)
        new_polys += more_polys
        new_points += more_points
    if not new_polys:
        raise core.McpToolError("Nothing could be copied: " + "; ".join(ctx.notes))
    if translate is not None or rotate_deg is not None or scale is not None:
        op_transform(ctx, select=new_points, translate=translate, rotate_deg=rotate_deg, scale=scale)
    return {"polygons": new_polys, "points": new_points}


@op("cut_paste")
def op_cut_paste(ctx, select=None, translate=None, rotate_deg=None, scale=None, save_as=None):
    """Cut the chosen polygons out of the surface and paste them back, optionally moved/rotated/scaled around
    their own center. Unlike transform, the pasted part gets its OWN points, so neighbours are not dragged along."""
    lm = ctx.lm
    lm.require_topology_edit()
    ids = ctx.polygons(_need(select, "polygon"))
    copy = op_duplicate(ctx, select=ids, translate=translate, rotate_deg=rotate_deg, scale=scale)
    result = lm.delete_polygons(ids)
    ctx.apply_remap(result["point_remap"], result["polygon_remap"])
    return {"polygons": [result["polygon_remap"][p] for p in copy["polygons"]],
            "points": [result["point_remap"][p] for p in copy["points"]]}


@op("mirror")
def op_mirror(ctx, select=None, axis="x", plane: float = 0.0, tolerance: float = 0.001, save_as=None):
    """Add a mirrored copy of the chosen polygons across axis = plane (corners on the plane are shared, so the
    halves join). Winding and UV order are reversed so the copy faces outward. Returns the new indices."""
    lm = ctx.lm
    lm.require_topology_edit()
    a = meshmath.axis_index(axis)
    ids = ctx.polygons(_need(select, "polygon"))
    table = lm.slot_table()
    point_map: dict[int, int] = {}

    def mirrored(i: int) -> int:
        if i not in point_map:
            p = list(lm.points[i])
            p[a] = 2 * plane - p[a]
            point_map[i] = _find_or_add_point(lm, tuple(p), tolerance)
        return point_map[i]

    new_polys, skipped = [], 0
    first_point = len(lm.points)
    for pid in ids:
        block_index, atts_index = table[pid]
        if lm.block_class(block_index) != "amesh.class":
            skipped += 1
            continue
        ring = [mirrored(i) for i in lm.polygons[pid]]
        if set(ring) == set(lm.polygons[pid]) or len(set(ring)) != len(ring):
            continue  # lies in the plane: mirroring it adds nothing
        block = lm.blocks[block_index]
        entry = block.atts[atts_index]
        group = None if block.uses_atts_only_mapping else list(reversed(block.olpl[atts_index]))
        new_polys.append(lm.append_polygon(list(reversed(ring)), block_index,
                                           (entry.color_val, entry.shade_val, entry.tracy_val, entry.pad), group))
    if skipped:
        ctx.notes.append(f"{skipped} animated/area polygon(s) were not mirrored")
    return {"polygons": new_polys, "points": list(range(first_point, len(lm.points)))}


@op("flip")
def op_flip(ctx, select=None):
    """Reverse polygons (turn inside-out ones around). UV order is reversed with the corners."""
    lm = ctx.lm
    ids = ctx.polygons(_need(select, "polygon"))
    table = lm.slot_table()
    for pid in ids:
        lm.reverse_polygon(pid, table)
    return {"flipped": len(ids), "polygons": ids}


@op("weld")
def op_weld(ctx, select=None, tolerance: float = 0.01, position: str = "first"):
    """Merge points closer than tolerance (selected points only). Polygons that collapse are deleted.
    position 'first' keeps the lowest-index point's place, 'average' moves it to the cluster center."""
    lm = ctx.lm
    lm.require_topology_edit()
    chosen = ctx.points(select)
    cell = max(tolerance, 1e-9)
    grid: dict[tuple, list[int]] = defaultdict(list)
    rep: dict[int, int] = {}
    members: dict[int, list[int]] = defaultdict(list)
    for i in chosen:
        p = lm.points[i]
        key = tuple(int(math.floor(c / cell)) for c in p)
        found = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for j in grid.get((key[0] + dx, key[1] + dy, key[2] + dz), ()):
                        if found is None and meshmath.distance(lm.points[j], p) <= tolerance:
                            found = j
        if found is None:
            grid[key].append(i)
            rep[i] = i
            members[i].append(i)
        else:
            rep[i] = found
            members[found].append(i)
    merged = sum(len(m) - 1 for m in members.values())
    if not merged:
        return {"merged": 0}
    if position == "average":
        for root, group in members.items():
            if len(group) > 1:
                lm.points[root] = meshmath.rounded(meshmath.centroid([lm.points[i] for i in group]), 4)
    table = lm.slot_table()
    doomed = []
    for pid, ring in enumerate(lm.polygons):
        if not any(i in rep and rep[i] != i for i in ring):
            continue
        new_ring, keep = [], []
        for position_in_ring, i in enumerate(ring):
            target = rep.get(i, i)
            if target not in new_ring:
                new_ring.append(target)
                keep.append(position_in_ring)
        if len(new_ring) < 3:
            doomed.append(pid)
            continue
        lm.set_polygon_vertices(pid, new_ring, keep, table)
    point_remap = polygon_remap = None
    if doomed:
        result = lm.delete_polygons(doomed)
        point_remap, polygon_remap = result["point_remap"], result["polygon_remap"]
    else:
        point_remap = lm.compact_points()
    ctx.apply_remap(point_remap, polygon_remap)
    return {"merged": merged, "polygons_removed": len(doomed), "points_left": len(lm.points)}


def _islands(lm, ids: list[int]) -> list[list[int]]:
    """Groups of the given polygons that touch each other through shared points."""
    owner: dict[int, list[int]] = defaultdict(list)
    for pid in ids:
        for i in lm.polygons[pid]:
            owner[i].append(pid)
    seen, groups = set(), []
    for pid in ids:
        if pid in seen:
            continue
        stack, group = [pid], []
        seen.add(pid)
        while stack:
            current = stack.pop()
            group.append(current)
            for i in lm.polygons[current]:
                for other in owner[i]:
                    if other not in seen:
                        seen.add(other)
                        stack.append(other)
        groups.append(sorted(group))
    return groups


@op("extrude")
def op_extrude(ctx, select=None, distance: float = 1.0, direction=None, scale: float = 1.0, side_uv=None, save_as=None):
    """Push the chosen polygons out (or in, negative distance) along their average normal or `direction`,
    and build the wall polygons around them. scale<1 narrows the moved face (taper), scale=1.0 keeps its size.
    Walls copy the material of the face they grow from; side_uv defaults to the same texture patch."""
    lm = ctx.lm
    lm.require_topology_edit()
    ids = ctx.polygons(_need(select, "polygon"))
    if not ids:
        raise core.McpToolError("The selector matched no polygons")
    if abs(distance) < 1e-9 and abs(scale - 1.0) < 1e-9:
        raise core.McpToolError("Nothing to do: distance is 0 and scale is 1")
    table = lm.slot_table()
    for pid in ids:
        if lm.block_class(table[pid][0]) != "amesh.class":
            raise core.McpToolError(f"Polygon #{pid} is animated/area geometry and cannot be extruded")
    cap_polys, walls, created_points = [], [], []
    for island in _islands(lm, ids):
        normals = [meshmath.scale(meshmath.outward_normal(lm.polygon_points(pid)) or (0, 0, 0),
                                  meshmath.area(lm.polygon_points(pid))) for pid in island]
        total = (sum(n[0] for n in normals), sum(n[1] for n in normals), sum(n[2] for n in normals))
        if direction is None:
            axis = meshmath.unit(total)
            if axis is None:
                raise core.McpToolError("These polygons have no clear facing direction: pass `direction`")
        else:
            axis = DIRECTIONS.get(direction.lower()) if isinstance(direction, str) else meshmath.unit(_vec(direction, "direction"))
            if axis is None:
                raise core.McpToolError("Unknown direction")
        used = sorted({i for pid in island for i in lm.polygons[pid]})
        center = meshmath.centroid([lm.points[i] for i in used])
        cap_of: dict[int, int] = {}
        for i in used:
            rel = meshmath.sub(lm.points[i], center)
            along = meshmath.dot(rel, axis)
            flat = meshmath.sub(rel, meshmath.scale(axis, along))
            moved = meshmath.add(center, meshmath.add(meshmath.add(meshmath.scale(flat, scale), meshmath.scale(axis, along)),
                                                      meshmath.scale(axis, distance)))
            cap_of[i] = lm.append_point(meshmath.rounded(moved, 4))
            created_points.append(cap_of[i])
        directed = {(ring[k], ring[(k + 1) % len(ring)]): pid for pid in island
                    for ring in [lm.polygons[pid]] for k in range(len(ring))}
        faces_out = meshmath.dot(axis, total) * (1 if distance >= 0 else -1) >= 0
        wall_jobs = [(a, b, pid) for (a, b), pid in directed.items() if (b, a) not in directed]
        for pid in island:
            lm.polygons[pid] = [cap_of[i] for i in lm.polygons[pid]]
            cap_polys.append(pid)
        for a, b, pid in wall_jobs:
            block_index, atts_index = table[pid]
            block = lm.blocks[block_index]
            entry = block.atts[atts_index]
            ring = [a, b, cap_of[b], cap_of[a]]
            corners = [lm.points[i] for i in ring]
            normal = meshmath.outward_normal(corners)
            away = meshmath.sub(meshmath.lerp(lm.points[a], lm.points[b], 0.5), center)
            away = meshmath.sub(away, meshmath.scale(axis, meshmath.dot(away, axis)))
            if normal is not None and (meshmath.dot(normal, away) < 0) == faces_out:
                ring.reverse()
            patch = None if block.uses_atts_only_mapping else block.olpl[atts_index]
            known = lm.block_memory.get(_block_material_metadata(block), {})
            density = known.get("density")
            spec, patch = _uv_spec(lm, side_uv if side_uv else ({} if density else {"mode": "rect" if patch else "planar"}),
                                   patch, density, known.get("uv_box"))
            group = None if block.uses_atts_only_mapping else uvtools.compute(
                spec, [lm.points[i] for i in ring], reference=patch, notes=ctx.notes)
            walls.append(lm.append_polygon(ring, block_index, (entry.color_val, entry.shade_val, entry.tracy_val, entry.pad), group))
    return {"cap_polygons": sorted(cap_polys), "wall_polygons": walls, "points": created_points}


@op("subdivide")
def op_subdivide(ctx, select=None, mode: str = "midpoints", save_as=None):
    """Cut polygons into smaller ones so there is more to bend. mode 'midpoints': every n-gon becomes n quads
    (new points on the edge middles and the center); 'center': a fan of triangles around a new center point."""
    lm = ctx.lm
    lm.require_topology_edit()
    if mode not in ("midpoints", "center"):
        raise core.McpToolError("mode must be 'midpoints' or 'center'")
    ids = ctx.polygons(_need(select, "polygon"))
    table = lm.slot_table()
    middle: dict[frozenset, tuple[int, tuple]] = {}
    new_polys, new_points = [], []

    def edge_point(a: int, b: int):
        key = frozenset((a, b))
        if key not in middle:
            index = lm.append_point(meshmath.rounded(meshmath.lerp(lm.points[a], lm.points[b], 0.5), 4))
            new_points.append(index)
            middle[key] = index
        return middle[key]

    def mid_uv(p, q):
        return None if p is None else ((p[0] + q[0]) // 2, (p[1] + q[1]) // 2)

    for pid in ids:
        block_index, atts_index = table[pid]
        if lm.block_class(block_index) != "amesh.class":
            raise core.McpToolError(f"Polygon #{pid} is animated/area geometry and cannot be subdivided")
        block = lm.blocks[block_index]
        entry = block.atts[atts_index]
        values = (entry.color_val, entry.shade_val, entry.tracy_val, entry.pad)
        ring = list(lm.polygons[pid])
        group = None if block.uses_atts_only_mapping else [tuple(uv) for uv in block.olpl[atts_index]]
        count = len(ring)
        center_index = lm.append_point(meshmath.rounded(meshmath.centroid([lm.points[i] for i in ring]), 4))
        new_points.append(center_index)
        center_uv = None if group is None else (sum(u for u, _ in group) // count, sum(v for _, v in group) // count)

        def uv_of(k):
            return None if group is None else group[k % count]

        pieces = []
        if mode == "center":
            for k in range(count):
                pieces.append(([ring[k], ring[(k + 1) % count], center_index],
                               None if group is None else [uv_of(k), uv_of(k + 1), center_uv]))
        else:
            mids = [edge_point(ring[k], ring[(k + 1) % count]) for k in range(count)]
            for k in range(count):
                prev_mid, next_mid = mids[(k - 1) % count], mids[k]
                pieces.append(([ring[k], next_mid, center_index, prev_mid],
                               None if group is None else [uv_of(k), mid_uv(uv_of(k), uv_of(k + 1)), center_uv,
                                                           mid_uv(uv_of(k - 1), uv_of(k))]))
        for vertices, piece_uv in pieces:
            new_polys.append(lm.append_polygon(vertices, block_index, values, piece_uv))
    result = lm.delete_polygons(ids)
    polygon_remap = result["polygon_remap"]
    ctx.apply_remap(result["point_remap"], polygon_remap)
    final = [polygon_remap[p] for p in new_polys]
    return {"polygons": final, "new_points": len(new_points)}


# --- materials and UVs -----------------------------------------------------------------------


@op("set_material")
def op_set_material(ctx, select=None, block=None, like_polygon=None, uv=None, atts=None):
    """Re-map polygons to another material block and/or copy a polygon's material values (atts =
    {color, shade, tracy, pad}). UVs are kept unless a uv spec is given."""
    lm = ctx.lm
    ids = ctx.polygons(_need(select, "polygon"))
    if block is None and like_polygon is None and atts is None:
        raise core.McpToolError("give block, like_polygon or atts")
    reference_values = reference_uvs = None
    target_block = block
    if like_polygon is not None:
        choice = _reference_for(lm, {"like_polygon": like_polygon})
        like_block, reference_values, reference_uvs = choice.block, choice.atts, choice.reference_uvs
        target_block = like_block if block is None else block
    spec, reference_uvs = _uv_spec(lm, uv, reference_uvs) if uv else (None, reference_uvs)
    table = lm.slot_table()
    for pid in ids:
        block_index, atts_index = table[pid]
        entry = lm.blocks[block_index].atts[atts_index]
        values = list(reference_values or (entry.color_val, entry.shade_val, entry.tracy_val, entry.pad))
        for slot, key in enumerate(("color", "shade", "tracy", "pad")):
            if atts and key in atts:
                values[slot] = int(atts[key])
        group = None
        if spec:
            group = uvtools.compute(spec, lm.polygon_points(pid), current=lm.uv_group(pid, table),
                                    reference=reference_uvs, notes=ctx.notes)
        lm.move_polygon_to_block(pid, block_index if target_block is None else target_block,
                                 atts=tuple(values), uvs=group, table=table)
        table = lm.slot_table()
    return {"changed": len(ids), "block": target_block}


@op("map_polygons")
def op_map_polygons(ctx, select=None, material=None, uv=None):
    """Give polygons that have NO material (analyze / validate report them) a material block: select defaults to
    {'unmapped': true}; material = {'block': N} or {'like_polygon': ID}; uv = a UV spec (default planar / copied)."""
    lm = ctx.lm
    lm.require_topology_edit()
    ids = ctx.polygons(select if select is not None else {"unmapped": True})
    table = lm.slot_table()
    ids = [pid for pid in ids if pid not in table]
    if not ids:
        raise core.McpToolError("No unmapped polygons in that selection")
    choice = _reference_for(lm, material)
    block_index, atts = choice.block, choice.atts
    spec, reference_uvs = _uv_spec(lm, uv, choice.reference_uvs, choice.density, choice.region)
    for pid in ids:
        group = uvtools.compute(spec, lm.polygon_points(pid), reference=reference_uvs, notes=ctx.notes)
        lm.attach_polygon(pid, block_index, atts, group)
    return {"mapped": len(ids), "polygons": ids, "block": block_index}


@op("set_uvs")
def op_set_uvs(ctx, select=None, mode: str = "planar", rect=None, texels_per_unit: float = 1.0, from_polygon=None,
               uvs=None, offset=None, scale=None, flip_u: bool = False, flip_v: bool = False, rotate_90: int = 0,
               keep_aspect: bool = False):
    """Recompute or tweak UVs. modes: planar | rect | world | copy | explicit | adjust (see studio_help('uv'))."""
    lm = ctx.lm
    ids = ctx.polygons(_need(select, "polygon"))
    spec = {"mode": mode, "keep_aspect": keep_aspect}
    for key, value in (("rect", rect), ("texels_per_unit", texels_per_unit), ("offset", offset), ("scale", scale),
                       ("flip_u", flip_u), ("flip_v", flip_v), ("rotate_90", rotate_90)):
        if value is not None and value is not False and value != 0:
            spec[key] = value
    spec, reference = _uv_spec(lm, {**spec, **({"from_polygon": from_polygon} if from_polygon is not None else {})}, None)
    table = lm.slot_table()
    done, skipped = [], 0
    for position, pid in enumerate(ids):
        block_index, _ = table[pid]
        if lm.block_class(block_index) != "amesh.class" or lm.blocks[block_index].uses_atts_only_mapping:
            skipped += 1
            continue
        per_poly = dict(spec)
        if mode == "explicit":
            source = uvs.get(str(pid)) if isinstance(uvs, dict) else (uvs[position] if uvs and position < len(uvs) else None)
            per_poly["uvs"] = source
        group = uvtools.compute(per_poly, lm.polygon_points(pid), current=lm.uv_group(pid, table), reference=reference, notes=ctx.notes)
        lm.set_uvs(pid, group, table)
        done.append(pid)
    if skipped:
        ctx.notes.append(f"{skipped} polygon(s) skipped: animated, area or untextured material")
    return {"updated": len(done), "polygons": done}


@op("cleanup")
def op_cleanup(ctx, merge_tolerance: float = 0.001, remove_degenerate: bool = True, remove_duplicates: bool = True,
               remove_unused_points: bool = True):
    """Tidy up: merge doubled points, delete zero-area and duplicate polygons, drop unused points."""
    lm = ctx.lm
    lm.require_topology_edit()
    report = {}
    if merge_tolerance and merge_tolerance > 0:
        report["merge"] = op_weld(ctx, None, merge_tolerance)
    doomed = set()
    if remove_degenerate:
        doomed |= {pid for pid, ring in enumerate(lm.polygons)
                   if meshmath.area([lm.points[i] for i in ring]) < 1e-9}
    if remove_duplicates:
        seen = {}
        for pid, ring in enumerate(lm.polygons):
            key = frozenset(ring)
            if key in seen:
                doomed.add(pid)
            else:
                seen[key] = pid
    if doomed:
        result = lm.delete_polygons(sorted(doomed))
        ctx.apply_remap(result["point_remap"], result["polygon_remap"])
        report["polygons_removed"] = len(doomed)
    if remove_unused_points:
        remap = lm.compact_points()
        ctx.apply_remap(remap, None)
        report["unused_points_removed"] = len(remap)
    return report


# --- runner ---------------------------------------------------------------------------


def _trim(value):
    if isinstance(value, list) and len(value) > MAX_SHOWN:
        return value[:MAX_SHOWN] + [f"... {len(value)} in total"]
    if isinstance(value, dict):
        return {k: _trim(v) for k, v in value.items()}
    return value


def _call(function, ctx, arguments: dict, number: int):
    allowed = [n for n in inspect.signature(function).parameters if n != "ctx"]
    unknown = sorted(set(arguments) - set(allowed))
    if unknown:
        raise core.McpToolError(f"operation #{number} '{function.op_name}': unknown argument(s) {unknown}. "
                                f"Allowed: {allowed}")
    return function(ctx, **arguments)


def run(target: str, operations: list[dict], label: str = "", owner_path: str = "", set_hint: str = "",
        dry_run: bool = False) -> dict:
    """Apply operations in order on one in-memory copy, then verify and write once."""
    if not operations:
        raise core.McpToolError("give at least one operation")
    lm = LooseModel.open(target, owner_path, set_hint, writable=True)
    ctx = Context(lm)
    outputs = []
    for number, raw in enumerate(operations, start=1):
        if "op" not in raw:
            raise core.McpToolError(f"operation #{number} has no 'op' name. Known: {sorted(OPS)}")
        arguments = dict(raw)
        name = arguments.pop("op")
        save_as = arguments.pop("save_as", None)
        if name not in OPS:
            raise core.McpToolError(f"operation #{number}: unknown op {name!r}. Known: {sorted(OPS)}")
        function = OPS[name]
        if save_as is not None and "save_as" in inspect.signature(function).parameters:
            arguments["save_as"] = save_as
        try:
            result = _call(function, ctx, arguments, number)
        except core.McpToolError as exc:
            raise core.McpToolError(f"operation #{number} ({name}) failed, nothing was written: {exc}") from exc
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            raise core.McpToolError(f"operation #{number} ({name}) got bad arguments, nothing was written: "
                                    f"{type(exc).__name__}: {exc}") from exc
        if save_as:
            if result.get("polygons") is not None:
                ctx.named_polygons[save_as] = list(result["polygons"])
            if result.get("points") is not None:
                ctx.named_points[save_as] = list(result["points"])
            if name == "extrude":
                ctx.named_polygons[save_as] = result["cap_polygons"] + result["wall_polygons"]
        outputs.append({"op": name, **_trim(result)})
    summary = lm.commit(label or "+".join(o["op"] for o in outputs)[:60]) if not dry_run else \
        {"written": False, "dry_run": True, **lm.summary()}
    notes = list(dict.fromkeys(ctx.notes + lm.warnings))
    response = {"results": outputs, **summary}
    if notes:
        response["notes"] = notes
    if summary.get("written"):
        response["undo"] = "studio_undo restores the previous state"
    return response


from studio_mcp import edit_materials  # noqa: E402,F401  (registers the material / effect operations)
