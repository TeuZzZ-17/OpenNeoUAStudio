"""Selectors: describe WHICH points or polygons to edit without listing hundreds of numbers.

A selector is either a list of indices or a dict of conditions that must ALL be true:
point keys   : indices, box, x, y, z, sphere, polygons, block, connected_to, nearest, unused, named, mirror_of,
               not, or, invert
polygon keys : ids, block, box, box_all, x, y, z, facing, connected_to, points_any, points_all, vertices,
               min_area, max_area, unmapped, named, mirror_of, not, or, invert
A missing selector (None) means "everything".
"""

from __future__ import annotations

from dataclasses import dataclass, field

from studio_mcp import core, meshmath

POINT_KEYS = {"indices", "box", "x", "y", "z", "sphere", "polygons", "block", "connected_to", "nearest",
              "unused", "named", "not", "or", "invert", "all", "mirror_of"}
POLYGON_KEYS = {"ids", "block", "box", "box_all", "x", "y", "z", "facing", "connected_to", "points_any",
                "points_all", "vertices", "min_area", "max_area", "named", "not", "or", "invert", "all", "mirror_of",
                "unmapped"}

DIRECTIONS = {
    "up": (0.0, -1.0, 0.0), "down": (0.0, 1.0, 0.0), "right": (1.0, 0.0, 0.0), "left": (-1.0, 0.0, 0.0),
    "front": (0.0, 0.0, 1.0), "forward": (0.0, 0.0, 1.0), "back": (0.0, 0.0, -1.0), "rear": (0.0, 0.0, -1.0),
    "+x": (1.0, 0.0, 0.0), "-x": (-1.0, 0.0, 0.0), "+y": (0.0, 1.0, 0.0), "-y": (0.0, -1.0, 0.0),
    "+z": (0.0, 0.0, 1.0), "-z": (0.0, 0.0, -1.0),
}


@dataclass
class MeshView:
    """What the selectors need to know about a model (works for loose and Set.BAS models)."""

    points: list
    polygons: list
    poly_block: dict[int, int] = field(default_factory=dict)
    named_points: dict[str, list[int]] = field(default_factory=dict)
    named_polygons: dict[str, list[int]] = field(default_factory=dict)

    def polygon_centers(self) -> list:
        return [meshmath.centroid([self.points[i] for i in poly]) if poly else (0.0, 0.0, 0.0) for poly in self.polygons]


def view_of_object(fam_obj) -> MeshView:
    """MeshView of one family object (loose or Set.BAS)."""
    poly_block = {}
    for block_index, block in enumerate(fam_obj.base_object.ades):
        for entry in block.atts:
            poly_block[entry.poly_id] = block_index
        if (block.class_id or "").lower() == "area.class" and not block.atts:
            poly_block[block.ade_poly_id] = block_index
    return MeshView(fam_obj.skeleton.points, fam_obj.skeleton.polygons, poly_block)


def view_of(lm) -> MeshView:
    return view_of_object(lm.fam_obj)


def _unknown(spec: dict, allowed: set[str], kind: str) -> None:
    extra = sorted(set(spec) - allowed)
    if extra:
        raise core.McpToolError(f"Unknown {kind} selector key(s) {extra}. Allowed: {sorted(allowed)}")


def _range_ok(value: float, bounds) -> bool:
    low, high = bounds
    return (low is None or value >= low) and (high is None or value <= high)


def _box_bounds(box: dict) -> list:
    low = box.get("min") or [None] * 3
    high = box.get("max") or [None] * 3
    if len(low) != 3 or len(high) != 3:
        raise core.McpToolError("box needs 'min' and 'max' with 3 numbers each (null = unbounded)")
    return [(low[i], high[i]) for i in range(3)]


def _axis_ranges(spec: dict) -> list:
    ranges = [(None, None)] * 3
    for key, axis in (("x", 0), ("y", 1), ("z", 2)):
        if key in spec:
            pair = spec[key]
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise core.McpToolError(f"'{key}' must be [min, max] (null = unbounded)")
            ranges[axis] = (pair[0], pair[1])
    if "box" in spec:
        for axis, bounds in enumerate(_box_bounds(spec["box"])):
            if bounds != (None, None):
                ranges[axis] = bounds
    return ranges


def _in_ranges(point, ranges) -> bool:
    return all(_range_ok(point[i], ranges[i]) for i in range(3))


def _components(view: MeshView) -> list[int]:
    """Union-find over the points: points sharing a polygon end up in the same group."""
    parent = list(range(len(view.points)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for poly in view.polygons:
        for other in poly[1:]:
            a, b = find(poly[0]), find(other)
            if a != b:
                parent[b] = a
    return [find(i) for i in range(len(view.points))]


def _check_ids(ids, count: int, kind: str) -> list[int]:
    clean = []
    for value in ids:
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value < count:
            raise core.McpToolError(f"{kind} index {value!r} is out of range 0..{count - 1}")
        clean.append(value)
    return clean


def mirror_axes(value) -> tuple[int, ...]:
    from studio.rendering.geometry import normalize_mirror_axes
    try:
        return normalize_mirror_axes(list(value) if not isinstance(value, str) else list(value.replace(",", "")))
    except ValueError as exc:
        raise core.McpToolError(str(exc)) from exc


def _mirror_points(spec: dict, view: MeshView) -> set[int]:
    """Points that mirror the given ones across the model's x/y/z planes (the editor's Mirror Select)."""
    from studio.rendering.geometry import match_mirror_points
    base = select_points(spec.get("points"), view)
    match = match_mirror_points(view.points, base, mirror_axes(spec.get("axes", "x")), spec.get("tolerance"))
    found = {target for _, targets in match.counterparts for target in targets}
    return found | set(base) if spec.get("include_source", False) else found


def _mirror_polygons(spec: dict, view: MeshView) -> set[int]:
    from studio.rendering.geometry import match_mirror_polygons
    base = select_polygons(spec.get("polygons"), view)
    match = match_mirror_polygons(view.points, view.polygons, base, mirror_axes(spec.get("axes", "x")),
                                  spec.get("tolerance"))
    found = {target for _, targets in match.counterparts for target in targets}
    return found | set(base) if spec.get("include_source", False) else found


def select_points(spec, view: MeshView) -> list[int]:
    count = len(view.points)
    if spec is None or spec == "all":
        return list(range(count))
    if isinstance(spec, (list, tuple)):
        return sorted(set(_check_ids(spec, count, "Point")))
    if not isinstance(spec, dict):
        raise core.McpToolError("A point selector is a list of indices or a dict of conditions")
    _unknown(spec, POINT_KEYS, "point")
    chosen = set(range(count))
    if "or" in spec:
        chosen = set()
        for sub in spec["or"]:
            chosen |= set(select_points(sub, view))
    if "indices" in spec:
        chosen &= set(_check_ids(spec["indices"], count, "Point"))
    ranges = _axis_ranges(spec)
    if any(r != (None, None) for r in ranges):
        chosen = {i for i in chosen if _in_ranges(view.points[i], ranges)}
    if "sphere" in spec:
        sphere = spec["sphere"]
        center, radius = sphere.get("center"), sphere.get("radius")
        if not center or len(center) != 3 or radius is None or radius < 0:
            raise core.McpToolError("sphere needs 'center' [x,y,z] and 'radius' >= 0")
        chosen = {i for i in chosen if meshmath.distance(view.points[i], center) <= radius}
    if "polygons" in spec:
        used = set()
        for pid in _check_ids(spec["polygons"], len(view.polygons), "Polygon"):
            used.update(view.polygons[pid])
        chosen &= used
    if "block" in spec:
        wanted = {spec["block"]} if isinstance(spec["block"], int) else set(spec["block"])
        used = set()
        for pid, block in view.poly_block.items():
            if block in wanted:
                used.update(view.polygons[pid])
        chosen &= used
    if "connected_to" in spec:
        root = _check_ids([spec["connected_to"]], count, "Point")[0]
        groups = _components(view)
        chosen = {i for i in chosen if groups[i] == groups[root]}
    if spec.get("unused"):
        used = {i for poly in view.polygons for i in poly}
        chosen = {i for i in chosen if i not in used}
    if "named" in spec:
        name = spec["named"]
        if name not in view.named_points:
            raise core.McpToolError(f"No point group named {name!r} (known: {sorted(view.named_points)})")
        chosen &= set(view.named_points[name])
    if "mirror_of" in spec:
        chosen &= _mirror_points(spec["mirror_of"], view)
    if "not" in spec:
        chosen -= set(select_points(spec["not"], view))
    if "nearest" in spec:
        target, wanted = spec["nearest"].get("to"), int(spec["nearest"].get("count", 1))
        if not target or len(target) != 3:
            raise core.McpToolError("nearest needs 'to' [x,y,z] and 'count'")
        ranked = sorted(chosen, key=lambda i: meshmath.distance(view.points[i], target))
        chosen = set(ranked[:max(wanted, 0)])
    if spec.get("invert"):
        chosen = set(range(count)) - chosen
    return sorted(chosen)


def _direction(value) -> tuple:
    if isinstance(value, str):
        key = value.strip().lower()
        if key not in DIRECTIONS:
            raise core.McpToolError(f"Unknown direction {value!r}. Use one of {sorted(DIRECTIONS)} or [x,y,z]")
        return DIRECTIONS[key]
    if isinstance(value, (list, tuple)) and len(value) == 3:
        unit = meshmath.unit(tuple(float(v) for v in value))
        if unit is None:
            raise core.McpToolError("Direction vector cannot be zero")
        return unit
    raise core.McpToolError("A direction is a name (up, down, left, right, front, back, +x ...) or [x,y,z]")


def select_polygons(spec, view: MeshView) -> list[int]:
    count = len(view.polygons)
    if spec is None or spec == "all":
        return list(range(count))
    if isinstance(spec, (list, tuple)):
        return sorted(set(_check_ids(spec, count, "Polygon")))
    if not isinstance(spec, dict):
        raise core.McpToolError("A polygon selector is a list of ids or a dict of conditions")
    _unknown(spec, POLYGON_KEYS, "polygon")
    chosen = set(range(count))
    if "or" in spec:
        chosen = set()
        for sub in spec["or"]:
            chosen |= set(select_polygons(sub, view))
    if "ids" in spec:
        chosen &= set(_check_ids(spec["ids"], count, "Polygon"))
    if "block" in spec:
        wanted = {spec["block"]} if isinstance(spec["block"], int) else set(spec["block"])
        chosen = {i for i in chosen if view.poly_block.get(i) in wanted}
    ranges = _axis_ranges({k: v for k, v in spec.items() if k in ("x", "y", "z", "box")})
    if any(r != (None, None) for r in ranges):
        centers = view.polygon_centers()
        chosen = {i for i in chosen if _in_ranges(centers[i], ranges)}
    if "box_all" in spec:
        bounds = _box_bounds(spec["box_all"])
        chosen = {i for i in chosen if all(_in_ranges(view.points[v], bounds) for v in view.polygons[i])}
    if "facing" in spec:
        facing = spec["facing"]
        direction = _direction(facing["dir"] if isinstance(facing, dict) else facing)
        min_dot = float(facing.get("min_dot", 0.7)) if isinstance(facing, dict) else 0.7
        keep = set()
        for i in chosen:
            normal = meshmath.outward_normal([view.points[v] for v in view.polygons[i]])
            if normal is not None and meshmath.dot(normal, direction) >= min_dot:
                keep.add(i)
        chosen = keep
    if "connected_to" in spec:
        root = _check_ids([spec["connected_to"]], count, "Polygon")[0]
        groups = _components(view)
        target = groups[view.polygons[root][0]]
        chosen = {i for i in chosen if view.polygons[i] and groups[view.polygons[i][0]] == target}
    if "points_any" in spec:
        points = set(select_points(spec["points_any"], view))
        chosen = {i for i in chosen if points.intersection(view.polygons[i])}
    if "points_all" in spec:
        points = set(select_points(spec["points_all"], view))
        chosen = {i for i in chosen if view.polygons[i] and points.issuperset(view.polygons[i])}
    if "unmapped" in spec:
        chosen = {i for i in chosen if (i not in view.poly_block) == bool(spec["unmapped"])}
    if "vertices" in spec:
        wanted = {spec["vertices"]} if isinstance(spec["vertices"], int) else set(spec["vertices"])
        chosen = {i for i in chosen if len(view.polygons[i]) in wanted}
    if "min_area" in spec or "max_area" in spec:
        low, high = spec.get("min_area"), spec.get("max_area")
        keep = set()
        for i in chosen:
            size = meshmath.area([view.points[v] for v in view.polygons[i]])
            if (low is None or size >= low) and (high is None or size <= high):
                keep.add(i)
        chosen = keep
    if "named" in spec:
        name = spec["named"]
        if name not in view.named_polygons:
            raise core.McpToolError(f"No polygon group named {name!r} (known: {sorted(view.named_polygons)})")
        chosen &= set(view.named_polygons[name])
    if "mirror_of" in spec:
        chosen &= _mirror_polygons(spec["mirror_of"], view)
    if "not" in spec:
        chosen -= set(select_polygons(spec["not"], view))
    if spec.get("invert"):
        chosen = set(range(count)) - chosen
    return sorted(chosen)
