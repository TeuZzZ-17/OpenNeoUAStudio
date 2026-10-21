"""Wireframe skeletons (.SKL / .sklt without a BASE): the Wireframe Editor's work, for assistants.

A wireframe is a skeleton whose POO2 points are seen from above (x across, z up/down; y is kept) and whose POL2
entries are lines (2 points) or closed outlines (3+ points). Files with an OTL2 outline only allow moving the
outline points (their count is fixed by the format). Operations are chained like studio_edit_batch and written once.
"""

from __future__ import annotations

import inspect
import math
import os
import shutil
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from studio.formats.sklt import (
    create_minimal_sklt_model,
    parse_sklt_file,
    save_sklt_with_otl2_points,
    save_sklt_with_poo2_points,
    save_sklt_with_poo2_pol2_structure,
)
from studio_mcp import core, history, meshmath
from studio_mcp.select import MeshView, select_points, select_polygons

MAX_POINTS = 0x10000
WIRE_OPS: dict = {}
SHAPES = {"triangle": 3, "square": 4, "pentagon": 5, "circle": 16}


def wire_op(name: str):
    def register(function):
        WIRE_OPS[name] = function
        function.op_name = name
        return function
    return register


def resolve_skeleton(target: str, must_exist: bool = True) -> Path:
    path = Path(target)
    if not path.is_absolute():
        path = core.data_root() / path
    path = path.resolve()
    if must_exist and not path.is_file():
        raise core.McpToolError(f"Skeleton not found: {path}")
    if path.suffix.lower() not in (".skl", ".sklt"):
        raise core.McpToolError(f"Expected a .SKL or .sklt skeleton, got {path.name}")
    return path


class WireModel:
    def __init__(self, path: Path, writable: bool = True):
        self.path = path
        self.model = parse_sklt_file(path)
        if writable:
            core.ensure_writable(path)
        self.mode = "otl2" if self.model.has_outline_data else (
            "poo2" if self.model.poo2_payload_offset is not None and self.model.pol2_payload_offset is not None else "none")
        if self.mode == "none":
            raise core.McpToolError(f"{path.name} has no editable POO2/POL2 or OTL2/OLPL data")
        self.points = [tuple(p) for p in self.model.points]
        self.polygons = [list(p) for p in self.model.polygons]
        self.outline = [tuple(p) for p in self.model.outline_points]
        self._orig = (list(self.points), [list(p) for p in self.polygons], list(self.outline))
        self.notes: list[str] = []

    @property
    def dirty(self) -> bool:
        return (self.points, self.polygons, self.outline) != self._orig

    def view(self, named=None) -> MeshView:
        view = MeshView(self.points, self.polygons, {})
        if named:
            view.named_points = named.get("points", {})
            view.named_polygons = named.get("polygons", {})
        return view

    def summary(self) -> dict:
        return {"skeleton": str(self.path), "mode": self.mode, "points": len(self.points),
                "polygons": len(self.polygons), "bbox": meshmath.bbox(self.points),
                "outline_points": len(self.outline)}

    def commit(self, label: str) -> dict:
        if not self.dirty:
            return {"written": False, "note": "no change", **self.summary()}
        if self.mode == "poo2":
            for ring in self.polygons:
                if len(ring) < 2 or len(set(ring)) != len(ring) or any(not 0 <= i < len(self.points) for i in ring):
                    raise core.McpToolError(f"Edit refused, nothing was written: bad polygon {ring}")
        stage = core.work_dir() / f"stage_{uuid.uuid4().hex[:8]}"
        stage.mkdir(parents=True)
        try:
            staged = stage / self.path.name
            if self.mode == "otl2":
                if len(self.outline) != len(self.model.outline_points):
                    raise core.McpToolError("An OTL2 outline keeps its number of points; only moving them is possible")
                save_sklt_with_otl2_points(self.model, [tuple(int(round(v)) for v in p) for p in self.outline], staged)
            elif len(self.points) != len(self._orig[0]) or self.polygons != self._orig[1]:
                save_sklt_with_poo2_pol2_structure(self.model, self.points, self.polygons, staged)
            else:
                save_sklt_with_poo2_points(self.model, self.points, staged)
            check = parse_sklt_file(staged)
            if self.mode == "poo2" and (len(check.points) != len(self.points) or check.polygons != self.polygons):
                raise core.McpToolError("The staged wireframe does not match the edit; nothing was written")
            history.snapshot(self.path, [self.path], label)
            os.replace(staged, self.path)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        return {"written": True, **self.summary()}

    def delete_points(self, doomed: set[int]) -> dict[int, int]:
        """Remove points (and every polygon that used them); returns the old->new point map."""
        keep = [i for i in range(len(self.points)) if i not in doomed]
        remap = {old: new for new, old in enumerate(keep)}
        self.points = [self.points[i] for i in keep]
        self.polygons = [[remap[i] for i in ring] for ring in self.polygons if not doomed.intersection(ring)]
        return remap


@dataclass
class WireContext:
    wm: WireModel
    named_points: dict = field(default_factory=dict)
    named_polygons: dict = field(default_factory=dict)

    def points(self, selector) -> list[int]:
        return select_points(selector, self.wm.view({"points": self.named_points, "polygons": self.named_polygons}))

    def polygons(self, selector) -> list[int]:
        return select_polygons(selector, self.wm.view({"points": self.named_points, "polygons": self.named_polygons}))


def _xz(value, name: str) -> tuple[float, float, float | None]:
    """[x, z] or [x, y, z] -> (x, z, y or None)."""
    if value is None or len(value) not in (2, 3):
        raise core.McpToolError(f"{name} must be [x, z] or [x, y, z]")
    if len(value) == 2:
        return float(value[0]), float(value[1]), None
    return float(value[0]), float(value[2]), float(value[1])


def _default_y(wm: WireModel) -> float:
    return sum(p[1] for p in wm.points) / len(wm.points) if wm.points else 0.0


def _need_poo2(wm: WireModel) -> None:
    if wm.mode != "poo2":
        raise core.McpToolError("This file only has an OTL2 outline: use `outline_move` (point count is fixed)")


@wire_op("add_points")
def w_add_points(ctx, points=None, save_as=None):
    """Add free points: [[x, z], ...] (or [x, y, z]). Returns the new indices."""
    wm = ctx.wm
    _need_poo2(wm)
    if not points:
        raise core.McpToolError("give `points`: [[x, z], ...]")
    created = []
    for p in points:
        x, z, y = _xz(p, "point")
        if len(wm.points) >= MAX_POINTS:
            raise core.McpToolError("Point limit reached")
        wm.points.append((x, _default_y(wm) if y is None else y, z))
        created.append(len(wm.points) - 1)
    return {"points": created}


@wire_op("add_polygons")
def w_add_polygons(ctx, polygons=None, close_loops: bool = False, save_as=None):
    """Link existing points: each entry is a list of point indices. 2 points = a line, 3+ = an outline.
    close_loops=true turns every list of 3+ points into a closed outline (the usual form for shapes)."""
    wm = ctx.wm
    _need_poo2(wm)
    created = []
    for ring in polygons or []:
        ring = [int(i) for i in ring]
        if len(ring) < 2 or len(set(ring)) != len(ring) or any(not 0 <= i < len(wm.points) for i in ring):
            raise core.McpToolError(f"Polygon {ring} needs 2+ different, existing points")
        if len(wm.polygons) >= 0x7FFF:
            raise core.McpToolError("Polygon limit reached")
        wm.polygons.append(ring)
        created.append(len(wm.polygons) - 1)
    if not created:
        raise core.McpToolError("give `polygons`: [[a, b], [a, b, c], ...]")
    return {"polygons": created}


@wire_op("add_shape")
def w_add_shape(ctx, shape: str = "square", center=None, radius: float = 80.0, rotate_deg: float = 0.0, save_as=None):
    """Add a closed shape (triangle, square, pentagon, circle) around center [x, z] with the given radius."""
    wm = ctx.wm
    _need_poo2(wm)
    if shape not in SHAPES:
        raise core.McpToolError(f"shape must be one of {sorted(SHAPES)}")
    cx, cz, _ = _xz(center or [0, 0], "center")
    count, y = SHAPES[shape], _default_y(wm)
    start = -math.pi / 2 + math.radians(rotate_deg)
    first = len(wm.points)
    for step in range(count):
        angle = start + math.tau * step / count
        wm.points.append((round(cx + math.cos(angle) * radius, 3), y, round(cz + math.sin(angle) * radius, 3)))
    wm.polygons.append(list(range(first, first + count)))
    return {"points": list(range(first, first + count)), "polygons": [len(wm.polygons) - 1]}


@wire_op("delete_points")
def w_delete_points(ctx, select=None):
    """Delete points together with every line/outline that uses them (indices after them shift down)."""
    wm = ctx.wm
    _need_poo2(wm)
    doomed = set(ctx.points(select))
    if not doomed:
        raise core.McpToolError("The selector matched no points")
    remap = wm.delete_points(doomed)
    ctx.named_points = {n: sorted(remap[i] for i in ids if i in remap) for n, ids in ctx.named_points.items()}
    ctx.named_polygons = {}
    return {"deleted": len(doomed), "points_left": len(wm.points), "polygons_left": len(wm.polygons)}


@wire_op("delete_polygons")
def w_delete_polygons(ctx, select=None, drop_orphan_points: bool = True):
    """Delete lines/outlines (unlink). Points left without any polygon are removed when drop_orphan_points."""
    wm = ctx.wm
    _need_poo2(wm)
    doomed = set(ctx.polygons(select))
    wm.polygons = [ring for i, ring in enumerate(wm.polygons) if i not in doomed]
    removed = 0
    if drop_orphan_points:
        used = {i for ring in wm.polygons for i in ring}
        orphans = {i for i in range(len(wm.points)) if i not in used}
        if orphans:
            wm.delete_points(orphans)
            removed = len(orphans)
    ctx.named_points, ctx.named_polygons = {}, {}
    return {"deleted": len(doomed), "orphan_points_removed": removed}


@wire_op("transform")
def w_transform(ctx, select=None, translate=None, rotate_deg: float = 0.0, scale=1.0, pivot="center",
                mirror: bool = False):
    """Move / rotate (degrees, in the x-z plane) / scale chosen points. translate = [dx, dz]; pivot = [x, z] or
    'center'|'origin'. mirror=true keeps the shape symmetric left/right across the x=0 axis like the editor's Mirror Symmetry."""
    wm = ctx.wm
    _need_poo2(wm)
    chosen = ctx.points(select)
    if not chosen:
        raise core.McpToolError("The selector matched no points")
    dx, dz, _ = _xz(translate or [0, 0], "translate")
    sx, sz = (float(scale), float(scale)) if isinstance(scale, (int, float)) else (float(scale[0]), float(scale[1]))
    if pivot == "origin":
        px, pz = 0.0, 0.0
    elif pivot == "center":
        px = sum(wm.points[i][0] for i in chosen) / len(chosen)
        pz = sum(wm.points[i][2] for i in chosen) / len(chosen)
    else:
        px, pz, _ = _xz(pivot, "pivot")
    angle = math.radians(rotate_deg)
    start = {i: (wm.points[i][0], wm.points[i][2]) for i in chosen}
    moved = {}
    for i in chosen:
        x, z = (start[i][0] - px) * sx, (start[i][1] - pz) * sz
        x, z = x * math.cos(angle) - z * math.sin(angle), x * math.sin(angle) + z * math.cos(angle)
        moved[i] = (px + x + dx, pz + z + dz)
    if mirror:
        from wireframe_editor.window import _apply_mirror_positions, _match_mirror_vertices
        pairs, centre = _match_mirror_vertices([(p[0], p[2]) for p in wm.points], 1.0)
        moved, _guides = _apply_mirror_positions(moved, pairs, centre, start)
    for i, (x, z) in moved.items():
        wm.points[i] = (round(x, 3), wm.points[i][1], round(z, 3))
    return {"moved": sorted(moved), "points": sorted(moved)}


@wire_op("set_points")
def w_set_points(ctx, updates=None):
    """Exact positions: updates = [[index, [x, z]], ...] or {index: [x, z]}."""
    wm = ctx.wm
    _need_poo2(wm)
    pairs = list(updates.items()) if isinstance(updates, dict) else list(updates or [])
    if not pairs:
        raise core.McpToolError("give `updates`")
    for index, position in pairs:
        index = int(index)
        if not 0 <= index < len(wm.points):
            raise core.McpToolError(f"Point #{index} does not exist")
        x, z, y = _xz(position, "position")
        wm.points[index] = (x, wm.points[index][1] if y is None else y, z)
    return {"set": len(pairs)}


@wire_op("align")
def w_align(ctx, select=None, axis: str = "z", value=None):
    """Put the chosen points on one line: axis 'z' = same z (a horizontal line), 'x' = same x. value = the
    coordinate to use (default: their average)."""
    wm = ctx.wm
    _need_poo2(wm)
    chosen = ctx.points(select)
    if len(chosen) < 2 and value is None:
        raise core.McpToolError("Select 2 or more points, or give `value`")
    index = {"x": 0, "z": 2}.get(str(axis).lower())
    if index is None:
        raise core.McpToolError("axis must be 'x' or 'z'")
    target = float(value) if value is not None else sum(wm.points[i][index] for i in chosen) / len(chosen)
    for i in chosen:
        p = list(wm.points[i])
        p[index] = round(target, 3)
        wm.points[i] = tuple(p)
    return {"aligned": len(chosen), "value": round(target, 3)}


@wire_op("mirror_copy")
def w_mirror_copy(ctx, select=None, axis: str = "z", plane: float = 0.0, tolerance: float = 0.5, save_as=None):
    """Copy the chosen points and the lines between them mirrored across axis = plane (z=0 is the usual centre
    line). Points on the plane are shared, so the halves join."""
    wm = ctx.wm
    _need_poo2(wm)
    index = {"x": 0, "z": 2}.get(str(axis).lower())
    if index is None:
        raise core.McpToolError("axis must be 'x' or 'z'")
    chosen = set(ctx.points(select))
    mapping: dict[int, int] = {}
    created = []
    for i in sorted(chosen):
        p = list(wm.points[i])
        p[index] = 2 * plane - p[index]
        found = next((j for j, q in enumerate(wm.points) if meshmath.distance(q, p) <= tolerance), None)
        if found is None:
            wm.points.append(tuple(round(v, 3) for v in p))
            found = len(wm.points) - 1
            created.append(found)
        mapping[i] = found
    new_polys = []
    for ring in list(wm.polygons):
        if chosen.issuperset(ring):
            mirrored = [mapping[i] for i in ring]
            if len(set(mirrored)) == len(mirrored) and set(mirrored) != set(ring):
                wm.polygons.append(mirrored)
                new_polys.append(len(wm.polygons) - 1)
    return {"points": created, "polygons": new_polys}


@wire_op("weld")
def w_weld(ctx, select=None, tolerance: float = 0.5):
    """Merge points closer than tolerance; lines that collapse disappear."""
    wm = ctx.wm
    _need_poo2(wm)
    chosen = ctx.points(select)
    rep: dict[int, int] = {}
    roots: list[int] = []
    for i in chosen:
        hit = next((r for r in roots if meshmath.distance(wm.points[r], wm.points[i]) <= tolerance), None)
        if hit is None:
            roots.append(i)
            rep[i] = i
        else:
            rep[i] = hit
    merged = sum(1 for i, r in rep.items() if i != r)
    new_polys = []
    for ring in wm.polygons:
        out = []
        for i in ring:
            target = rep.get(i, i)
            if target not in out:
                out.append(target)
        if len(out) >= 2:
            new_polys.append(out)
    wm.polygons = new_polys
    used = {i for ring in wm.polygons for i in ring}
    wm.delete_points({i for i, r in rep.items() if i != r and i not in used})
    ctx.named_points, ctx.named_polygons = {}, {}
    return {"merged": merged}


@wire_op("outline_move")
def w_outline_move(ctx, updates=None):
    """OTL2 files: set outline points (0..255 each): updates = [[index, [u, v]], ...]. The count cannot change."""
    wm = ctx.wm
    if wm.mode != "otl2":
        raise core.McpToolError("This file has no OTL2 outline")
    pairs = list(updates.items()) if isinstance(updates, dict) else list(updates or [])
    for index, position in pairs:
        index = int(index)
        if not 0 <= index < len(wm.outline) or len(position) != 2:
            raise core.McpToolError(f"Outline point #{index} does not exist or is not [u, v]")
        wm.outline[index] = (min(max(int(round(position[0])), 0), 255), min(max(int(round(position[1])), 0), 255))
    return {"moved": len(pairs)}


def run(target: str, operations: list[dict], label: str = "", dry_run: bool = False) -> dict:
    if not operations:
        raise core.McpToolError("give at least one operation")
    wm = WireModel(resolve_skeleton(target), writable=True)
    ctx = WireContext(wm)
    outputs = []
    for number, raw in enumerate(operations, start=1):
        arguments = dict(raw)
        name = arguments.pop("op", None)
        save_as = arguments.pop("save_as", None)
        function = WIRE_OPS.get(name)
        if function is None:
            raise core.McpToolError(f"operation #{number}: unknown op {name!r}. Known: {sorted(WIRE_OPS)}")
        allowed = [n for n in inspect.signature(function).parameters if n != "ctx"]
        unknown = sorted(set(arguments) - set(allowed))
        if unknown:
            raise core.McpToolError(f"operation #{number} '{name}': unknown argument(s) {unknown}. Allowed: {allowed}")
        if save_as is not None and "save_as" in allowed:
            arguments["save_as"] = save_as
        try:
            result = function(ctx, **arguments)
        except core.McpToolError as exc:
            raise core.McpToolError(f"operation #{number} ({name}) failed, nothing was written: {exc}") from exc
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            raise core.McpToolError(f"operation #{number} ({name}) got bad arguments, nothing was written: "
                                    f"{type(exc).__name__}: {exc}") from exc
        if save_as:
            if result.get("points") is not None:
                ctx.named_points[save_as] = list(result["points"])
            if result.get("polygons") is not None:
                ctx.named_polygons[save_as] = list(result["polygons"])
        outputs.append({"op": name, **result})
    summary = wm.commit(label or "+".join(o["op"] for o in outputs)[:60]) if not dry_run else \
        {"written": False, "dry_run": True, **wm.summary()}
    return {"results": outputs, **summary}


def get(target: str) -> dict:
    wm = WireModel(resolve_skeleton(target), writable=False)
    degree = defaultdict(int)
    for ring in wm.polygons:
        for i in ring:
            degree[i] += 1
    return {**wm.summary(), "points_xz": [[round(p[0], 3), round(p[2], 3)] for p in wm.points], "y": sorted({round(p[1], 3) for p in wm.points})[:5],
            "polygons_list": wm.polygons, "outline": [list(p) for p in wm.outline],
            "outline_groups": [list(g) for g in wm.model.outline_groups], "sensors": len(wm.model.sensors),
            "note": "points_xz are [x, z] pairs (y is kept separately); polygons are point-index lists (2 = line)"}


def new(target: str) -> dict:
    """Create an empty wireframe file (Data/Models or Data/Wireless); then fill it with studio_wire_edit."""
    path = resolve_skeleton(target, must_exist=False)
    core.ensure_writable(path)
    if path.exists():
        raise core.McpToolError(f"{path} already exists")
    model = create_minimal_sklt_model(path.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    save_sklt_with_poo2_pol2_structure(model, [], [], path)
    history.clear(path)
    return {"created": str(path), "next": "studio_wire_edit with add_points / add_shape / add_polygons"}


def render_png(target: str, size: int = 520, labels: bool = True, highlight=None) -> bytes:
    """Draw a wireframe (x across, z down) with point numbers; highlight = point selector to colour."""
    from PIL import Image, ImageDraw
    wm = WireModel(resolve_skeleton(target), writable=False)
    pts = [(p[0], p[2]) for p in wm.points] if wm.mode == "poo2" else [(float(u), float(v)) for u, v in wm.outline]
    groups = wm.polygons if wm.mode == "poo2" else [list(g) for g in wm.model.outline_groups]
    image = Image.new("RGB", (size, size), (31, 34, 40))
    draw = ImageDraw.Draw(image)
    if not pts:
        return _png(image)
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    span = max(max(xs) - min(xs), max(ys) - min(ys), 1e-6)
    margin = 30
    scale = (size - 2 * margin) / span
    cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2

    def to_screen(p):
        return (size / 2 + (p[0] - cx) * scale, size / 2 + (p[1] - cy) * scale)

    for ring in groups:
        if len(ring) >= 2:
            line = [to_screen(pts[i]) for i in ring if i < len(pts)]
            draw.line(line + ([line[0]] if len(line) > 2 else []), fill=(120, 200, 255), width=1)
    marked = set(select_points(highlight, wm.view())) if highlight is not None else set()
    for i, p in enumerate(pts):
        x, y = to_screen(p)
        colour = (255, 80, 200) if i in marked else (255, 220, 60)
        draw.ellipse((x - 3, y - 3, x + 3, y + 3), fill=colour)
        if labels:
            draw.text((x + 5, y - 11), str(i), fill=(255, 235, 120))
    return _png(image)


def _png(image) -> bytes:
    import io
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
