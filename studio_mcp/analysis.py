"""Read-only inspection tools: materials, quality analysis and model comparison (works on loose and Set.BAS models)."""

from __future__ import annotations

from collections import defaultdict

from studio.assets.catalog import build_texture_catalog
from studio.assets.mapping import MappingIndex
from studio.rendering.geometry import detect_fx_elements
from studio_mcp import core, meshmath

LIMIT_POINTS = 0x10000
LIMIT_POLYGONS = 0x8000


def _object(source):
    family = core.load_family(source)
    if family.root_object is None:
        raise core.McpToolError(f"Could not load {source.label}")
    objects = [o for o in family.all_objects() if o.skeleton is not None]
    if not objects:
        raise core.McpToolError("No object with a skeleton found")
    return objects[0]


def materials(source) -> dict:
    """Every material block: texture, how many polygons use it, a sample polygon to copy (like_polygon)."""
    obj = _object(source)
    rows = []
    for index, block in enumerate(obj.base_object.ades):
        polys = sorted({a.poly_id for a in block.atts}) or ([block.ade_poly_id] if block.class_id == "area.class" else [])
        uv = [tuple(uv) for group in block.olpl for uv in group]
        first = block.atts[0] if block.atts else None
        rows.append({
            "block": index, "class": block.class_id,
            "texture": block.texture.name if block.texture else None,
            "tracy_texture": block.tracy_texture.name if block.tracy_texture else None,
            "material": block.describe_polflags(),
            "polygons": len(polys), "sample_polygon": polys[0] if polys else None,
            "atts_sample": {"color": first.color_val, "shade": first.shade_val, "tracy": first.tracy_val,
                            "pad": first.pad} if first else None,
            "uv_box": [min(u for u, _ in uv), min(v for _, v in uv), max(u for u, _ in uv), max(v for _, v in uv)] if uv else None,
            "takes_new_polygons": (block.class_id or "").lower() == "amesh.class" and block.atts_chunk_offset >= 0,
            "has_uvs": not block.uses_atts_only_mapping if (block.class_id or "").lower() == "amesh.class" else None,
        })
    return {"source": source.label, "blocks": rows,
            "hint": "Use material={'like_polygon': sample_polygon} to add polygons that look like an existing one."}


def _edges(polygons):
    directed = defaultdict(list)
    for pid, ring in enumerate(polygons):
        for k, a in enumerate(ring):
            directed[(a, ring[(k + 1) % len(ring)])].append(pid)
    return directed


def _signed_volume(points, polygons, ids) -> float:
    """Volume of a closed surface from its polygons (positive when they face outward in UA's convention)."""
    total = 0.0
    for pid in ids:
        ring = [points[i] for i in polygons[pid]]
        for k in range(1, len(ring) - 1):
            total += meshmath.dot(ring[0], meshmath.cross(ring[k], ring[k + 1])) / 6.0
    return -total   # UA outward = negative Newell, so flip the usual sign


def analyze(source) -> dict:
    obj = _object(source)
    points, polygons = [tuple(p) for p in obj.skeleton.points], [list(p) for p in obj.skeleton.polygons]
    mapping = MappingIndex(obj)
    box = meshmath.bbox(points)
    diagonal = meshmath.length(box["size"]) if box else 0.0
    problems, warnings = [], []
    info = {"points": len(points), "polygons": len(polygons), "bbox": box,
            "by_corner_count": {str(n): sum(1 for r in polygons if len(r) == n) for n in sorted({len(r) for r in polygons})}}
    if len(points) > LIMIT_POINTS or len(polygons) > LIMIT_POLYGONS:
        problems.append("point/polygon count above the engine's file limits (65536 points / 32768 polygons)")
    if mapping.unmapped:
        problems.append(f"polygons without material: {mapping.unmapped[:12]}")
    if mapping.duplicates:
        problems.append(f"polygons mapped to more than one material: {sorted(mapping.duplicates)[:12]}")
    if mapping.invalid:
        problems.append(f"material entries pointing at missing polygons: {mapping.invalid[:6]}")

    degenerate = [pid for pid, ring in enumerate(polygons)
                  if len(set(ring)) < 3 or meshmath.area([points[i] for i in ring]) < 1e-9]
    if degenerate:
        problems.append(f"polygons with no area: {degenerate[:12]}")

    used = {i for ring in polygons for i in ring}
    unused = [i for i in range(len(points)) if i not in used]
    if unused:
        warnings.append(f"points no polygon uses (emitters/effects may use them): {unused[:12]}")

    cell = max(diagonal * 1e-5, 1e-4)
    spots = defaultdict(list)
    for i, p in enumerate(points):
        spots[tuple(round(c / cell) for c in p)].append(i)
    doubled = [group for group in spots.values() if len(group) > 1]
    if doubled:
        warnings.append(f"{len(doubled)} spot(s) hold more than one point (weld them?): {doubled[:6]}")

    seen, twins = {}, []
    for pid, ring in enumerate(polygons):
        key = frozenset(ring)
        if key in seen:
            twins.append((seen[key], pid))
        else:
            seen[key] = pid
    if twins:
        warnings.append(f"polygons sharing the same corners (double-sided or duplicates): {twins[:8]}")

    tolerance = max(diagonal * 0.002, 0.05)
    bent = [(pid, round(meshmath.planarity_error([points[i] for i in ring]), 3)) for pid, ring in enumerate(polygons)
            if len(ring) > 3 and meshmath.planarity_error([points[i] for i in ring]) > tolerance]
    if bent:
        warnings.append(f"{len(bent)} non-flat quad(s) (corner off its plane; texture may skew): {bent[:8]}")
    concave = [pid for pid, ring in enumerate(polygons) if len(ring) > 3 and not meshmath.is_convex([points[i] for i in ring])]
    if concave:
        warnings.append(f"{len(concave)} non-convex polygon(s) (the renderer expects convex): {concave[:8]}")

    directed = _edges(polygons)
    clash = sorted({tuple(sorted(set(pids))[:2]) for pids in directed.values() if len(set(pids)) > 1})
    if clash:
        warnings.append("neighbour polygons wound in opposite directions (one of each pair may be inside-out): "
                        f"{clash[:12]}")
    sharing = defaultdict(list)
    for (a, b), pids in directed.items():
        sharing[frozenset((a, b))].extend(pids)
    info["open_edges"] = sum(1 for pids in sharing.values() if len(pids) == 1)

    parent = list(range(len(polygons)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for pids in sharing.values():
        for other in pids[1:]:
            parent[find(other)] = find(pids[0])
    components = defaultdict(list)
    for pid in range(len(polygons)):
        components[find(pid)].append(pid)
    inside_out = []
    for ids in components.values():
        edges = {frozenset((polygons[p][k], polygons[p][(k + 1) % len(polygons[p])])) for p in ids
                 for k in range(len(polygons[p]))}
        if len(ids) >= 4 and all(len(sharing[e]) == 2 for e in edges) and _signed_volume(points, polygons, ids) < 0:
            inside_out.append(sorted(ids)[:6])
    if inside_out:
        problems.append(f"closed solid(s) turned inside out (use flip on them): first polygons {inside_out[:4]}")

    flat_uv = []
    for index, block in enumerate(obj.base_object.ades):
        if block.uses_atts_only_mapping:
            continue
        for k, group in enumerate(block.olpl):
            if group and len({tuple(uv) for uv in group}) < 3 and k < len(block.atts):
                flat_uv.append(block.atts[k].poly_id)
    if flat_uv:
        warnings.append(f"polygons whose UVs collapse to a line/point: {flat_uv[:12]}")

    if box and points:
        for axis, name in ((0, "x"), (2, "z")):
            lookup = {tuple(int(round(c / tolerance)) for c in p) for p in points}
            missing = []
            for i, p in enumerate(points):
                q = list(p)
                q[axis] = -q[axis]
                base = tuple(int(round(c / tolerance)) for c in q)
                if not any((base[0] + dx, base[1] + dy, base[2] + dz) in lookup
                           for dx in (-1, 0, 1) for dy in (-1, 0, 1) for dz in (-1, 0, 1)):
                    missing.append(i)
            share = 1 - len(missing) / len(points)
            info[f"symmetry_{name}"] = round(share, 3)
            if axis == 0 and missing and share > 0.6:
                warnings.append(f"almost mirror-symmetric on x, but points {missing[:10]} have no partner")
    return {"source": source.label, "ok": not problems, "problems": problems, "warnings": warnings, "info": info}


def compare(first, second) -> dict:
    """Difference between two models: size, counts, moved points (same topology) and added/removed polygons."""
    a, b = _object(first), _object(second)
    pa, pb = [tuple(p) for p in a.skeleton.points], [tuple(p) for p in b.skeleton.points]
    qa, qb = [list(r) for r in a.skeleton.polygons], [list(r) for r in b.skeleton.polygons]
    result = {"a": {"source": first.label, "points": len(pa), "polygons": len(qa), "bbox": meshmath.bbox(pa)},
              "b": {"source": second.label, "points": len(pb), "polygons": len(qb), "bbox": meshmath.bbox(pb)}}
    if len(pa) == len(pb) and qa == qb:
        moved = [(i, round(meshmath.distance(p, q), 3)) for i, (p, q) in enumerate(zip(pa, pb)) if meshmath.distance(p, q) > 1e-4]
        result["same_topology"] = True
        result["moved_points"] = len(moved)
        result["largest_moves"] = sorted(moved, key=lambda m: -m[1])[:10]
    else:
        def signature(points, ring):
            return tuple(sorted(tuple(round(c, 2) for c in points[i]) for i in ring))
        sa, sb = {signature(pa, r) for r in qa}, {signature(pb, r) for r in qb}
        only_a = [i for i, r in enumerate(qa) if signature(pa, r) not in sb]
        only_b = [i for i, r in enumerate(qb) if signature(pb, r) not in sa]
        result["same_topology"] = False
        result["polygons_only_in_a"] = {"count": len(only_a), "first": only_a[:12]}
        result["polygons_only_in_b"] = {"count": len(only_b), "first": only_b[:12]}
    ta = {(x["texture"]): x["polygons"] for x in materials(first)["blocks"]}
    tb = {(x["texture"]): x["polygons"] for x in materials(second)["blocks"]}
    result["textures_changed"] = ta != tb
    return result


def effects(source) -> dict:
    """FX elements (flames, smoke planes), particle emitters and animated polygons of a model."""
    family = core.load_family(source)
    objects = [o for o in family.all_objects() if o.skeleton is not None]
    if not objects:
        raise core.McpToolError("No object with a skeleton found")
    obj = objects[0]
    fx = [{"fx": i, "name": e.fx_name, "kind": e.source_kind, "polygons": list(e.poly_ids),
           "state": e.shared_state, "textures": list(e.material_names), "warnings": list(e.warnings)}
          for i, e in enumerate(detect_fx_elements(obj, family.animations))]
    emitters, animated = [], []
    for index, block in enumerate(obj.base_object.ades):
        cls = (block.class_id or "").lower()
        if cls == "particle.class":
            attrs = block.particle_attributes
            emitters.append({"block": index, "point": block.ade_point_id,
                             "attributes": {k: v for k, v in vars(attrs).items() if not k.startswith("_")} if attrs else None,
                             "stages": len(block.particle_stages)})
        elif cls == "area.class":
            animated.append({"block": index, "polygon": block.ade_poly_id,
                             "texture": block.texture.name if block.texture else None,
                             "kind": block.texture.kind if block.texture else None})
    return {"source": source.label, "fx_elements": fx, "emitters": emitters, "animated_polygons": animated,
            "hint": "studio_add_fx / studio_delete_fx use the fx index; studio_add_emitter copies an emitter block "
                    "of one model onto a point of another."}


def textures(source, name_filter: str = "") -> dict:
    """Textures a model can use: the ones it already loads plus every decodable bitmap of its Set.BAS."""
    family = core.load_family(source)
    archive = getattr(family, "setbas_archive", None)
    rows = []
    for entry in build_texture_catalog(family, archive):
        if name_filter and name_filter.lower() not in entry.name.lower():
            continue
        rows.append({"name": entry.name, "in_model": entry.name.casefold() in {k.casefold() for k in family.textures},
                     "decodable": bool(entry.decodable or entry.name.casefold() in {k.casefold() for k in family.textures})})
    return {"source": source.label, "textures": rows[:400], "count": len(rows)}
