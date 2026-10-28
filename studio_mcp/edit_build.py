"""Building operations for new designs (registered into edit.OPS): loft, paint, auto_shade.

They answer the three things that make a hand-built model look wrong in Urban Assault:
- shape: `loft` turns a few cross-sections into one closed, outward-facing hull (no loose boxes, no gaps);
- texture: `paint` maps polygons onto a named faction swatch (swatches.json), the way the original models map
  each face onto one panel of the atlas;
- light: the engine has no lighting, every polygon carries a baked `shade`. `auto_shade` sets it from the facing,
  with the values measured on the original Set1 vehicles (top light, darker sides, dark belly).
"""

from __future__ import annotations

import json
from pathlib import Path

from studio_mcp import core, meshmath, uvs as uvtools
from studio_mcp.edit import _need, _reference_for, _uv_spec, op
from studio_mcp.edit_materials import op_add_material_block, op_set_texture

SWATCH_FILE = Path(__file__).with_name("swatches.json")
_SWATCHES: dict | None = None
_DONORS: dict | None = None

# Medians of the original Set1 vehicles (ATTS shade, 0 = full light, higher = darker):
# top 5, front 30, sides 50, back 70, belly 95.  shade = base + vertical * ny - front * nz.
SHADE_BASE, SHADE_VERTICAL, SHADE_FRONT = 50.0, 45.0, 20.0


def swatches() -> dict:
    """Named texture patches: {name: {texture, rect, use}}; names starting with '_' are comments."""
    global _SWATCHES, _DONORS
    if _SWATCHES is None:
        data = json.loads(SWATCH_FILE.read_text(encoding="utf-8"))
        _SWATCHES = {k: v for k, v in data.items() if not k.startswith("_")}
        _DONORS = {k.upper(): v for k, v in data.get("_donors", {}).items()}
    return _SWATCHES


def donor_block(texture: str):
    """(source model, block) of an original opaque block with this texture, or None."""
    swatches()
    return _DONORS.get(texture.upper())


def swatch_entry(name: str) -> dict:
    table = swatches()
    if name not in table:
        close = [k for k in table if name.split(".")[0] in k] or sorted(table)
        raise core.McpToolError(f"Unknown swatch {name!r}. Known: {close[:40]} (studio_swatches lists them all)")
    return table[name]


# --- loft --------------------------------------------------------------------------------------


def _section_ring(section: dict, axis: str, half: bool) -> list[tuple]:
    """3D corners of one section. `points` are 2D: axis z -> [x, y], axis x -> [z, y], axis y -> [x, z]."""
    if not isinstance(section, dict) or "at" not in section or "points" not in section:
        raise core.McpToolError("each section is {'at': position_along_axis, 'points': [[a, b], ...]}")
    at = float(section["at"])
    flat = [(float(p[0]), float(p[1])) for p in section["points"]]
    if not flat:
        raise core.McpToolError("a section needs at least one point")
    if half and len(flat) > 1:
        if axis == "x":
            raise core.McpToolError("half=true mirrors across x, so it works with axis 'z' or 'y' only")
        if flat[0][0] != 0 or flat[-1][0] != 0 or any(a < 0 for a, _ in flat):
            raise core.McpToolError("half=true: list the x >= 0 side only, first and last corner exactly on x = 0")
        flat = flat + [(-a, b) for a, b in reversed(flat[1:-1])]
    if axis == "z":
        return [(a, b, at) for a, b in flat]
    if axis == "x":
        return [(at, b, a) for a, b in flat]
    if axis == "y":
        return [(a, at, b) for a, b in flat]
    raise core.McpToolError("axis must be 'z' (sections across the length), 'x' (wings) or 'y' (towers)")


def _plane_area(ring: list[tuple], axis: str) -> float:
    """Signed area of a section in its own plane (the sign tells the direction the corners go round)."""
    keep = {"z": (0, 1), "x": (2, 1), "y": (0, 2)}[axis]
    pts = [(p[keep[0]], p[keep[1]]) for p in ring]
    return sum(pts[k][0] * pts[(k + 1) % len(pts)][1] - pts[(k + 1) % len(pts)][0] * pts[k][1]
               for k in range(len(pts))) / 2.0


def _is_planar(pts: list[tuple]) -> bool:
    normal = meshmath.unit(meshmath.newell(pts))
    if normal is None:
        return False
    size = max(meshmath.distance(a, b) for a in pts for b in pts) or 1.0
    origin = pts[0]
    return all(abs(meshmath.dot(meshmath.sub(p, origin), normal)) <= 1e-3 * size for p in pts)


def _is_convex(pts: list[tuple]) -> bool:
    normal = meshmath.newell(pts)
    sign = 0
    for k in range(len(pts)):
        a, b, c = pts[k], pts[(k + 1) % len(pts)], pts[(k + 2) % len(pts)]
        turn = meshmath.dot(meshmath.cross(meshmath.sub(b, a), meshmath.sub(c, b)), normal)
        if abs(turn) < 1e-9:
            continue
        if sign == 0:
            sign = 1 if turn > 0 else -1
        elif (turn > 0) != (sign > 0):
            return False
    return True


def loft_mesh(sections: list, axis: str = "z", half: bool = False, caps: str = "both") -> tuple[list, list]:
    """Points and outward-wound polygons of a closed hull through the given sections."""
    if not sections or len(sections) < 2:
        raise core.McpToolError("loft needs at least 2 sections")
    if caps not in ("both", "start", "end", "none"):
        raise core.McpToolError("caps must be 'both', 'start', 'end' or 'none'")
    rings = [_section_ring(s, axis, half) for s in sections]
    count = max(len(r) for r in rings)
    if count < 3:
        raise core.McpToolError("sections need at least 3 corners (a single point is allowed for a tip)")
    for number, ring in enumerate(rings):
        if len(ring) == 1:
            rings[number] = ring * count                      # a tip: every corner collapses on one point
        elif len(ring) != count:
            raise core.McpToolError(f"section {number} has {len(ring)} corners, the others {count}: "
                                    "every section needs the same number (or 1 for a tip)")
    # every ring must go round in the same direction, or the side walls cross over
    direction = next((_plane_area(r, axis) for r in rings if abs(_plane_area(r, axis)) > 1e-9), None)
    if direction is None:
        raise core.McpToolError("all sections are flat lines or points: give them some width and height")
    rings = [list(reversed(r)) if _plane_area(r, axis) * direction < 0 else r for r in rings]

    points, index_of = [], {}

    def index(p):
        key = tuple(round(v, 4) for v in p)
        if key not in index_of:
            index_of[key] = len(points)
            points.append(key)
        return index_of[key]

    ids = [[index(p) for p in ring] for ring in rings]
    faces = []

    def add(face):
        ring = []
        for i in face:
            if not ring or ring[-1] != i:
                ring.append(i)
        if len(ring) > 1 and ring[0] == ring[-1]:
            ring.pop()
        if len(set(ring)) < 3 or meshmath.area([points[i] for i in ring]) < 1e-9:
            return
        pts = [points[i] for i in ring]
        if len(ring) == 4 and not (_is_planar(pts) and _is_convex(pts)):
            a, b, c, d = ring          # split on the shorter diagonal: two flat triangles
            if meshmath.distance(points[a], points[c]) <= meshmath.distance(points[b], points[d]):
                faces.extend([[a, b, c], [a, c, d]])
            else:
                faces.extend([[a, b, d], [b, c, d]])
            return
        faces.append(ring)

    for s in range(len(ids) - 1):
        a, b = ids[s], ids[s + 1]
        for k in range(count):
            add([a[k], a[(k + 1) % count], b[(k + 1) % count], b[k]])

    def cap(ring, reverse):
        if len(set(ring)) < 3:
            return
        ring = list(reversed(ring)) if reverse else list(ring)
        pts = [points[i] for i in ring]
        if _is_planar(pts) and _is_convex(pts):
            faces.append(ring)
            return
        center = index(meshmath.centroid(pts))      # fan around the middle: valid for any star-shaped outline
        for k in range(len(ring)):
            add([ring[k], ring[(k + 1) % len(ring)], center])

    if caps in ("both", "start"):
        cap(ids[0], True)
    if caps in ("both", "end"):
        cap(ids[-1], False)
    # the walls and caps above share one winding; make it the outward one (UA: outward = -Newell)
    volume = 0.0
    for ring in faces:
        pts = [points[i] for i in ring]
        for k in range(1, len(pts) - 1):
            volume += meshmath.dot(pts[0], meshmath.cross(pts[k], pts[k + 1]))
    if volume > 0:
        faces = [list(reversed(f)) for f in faces]
    return points, faces


def _placeholder_block(lm, material) -> dict:
    """With a swatch the starting block does not matter (paint moves the polygons): take any plain one."""
    if material:
        return material
    for index in lm.editable_amesh_blocks():
        block = lm.blocks[index]
        if not block.uses_atts_only_mapping and block.textured:
            return {"block": index}
    return material


@op("loft")
def op_loft(ctx, sections=None, axis: str = "z", half: bool = False, caps: str = "both", material=None, uv=None,
            swatch=None, save_as=None):
    """Build ONE closed hull through cross-sections: the clean way to make a fuselage, tank hull, bomb, missile or
    wing. sections = [{'at': z, 'points': [[x, y], ...]}, ...] in order along `axis` ('z': points are [x, y];
    'x': [z, y] for wings; 'y': [x, z] for towers). Same corner count in every section, listed round the outline;
    a section with ONE point makes a nose/tail tip. half=true: give only the x >= 0 side from the top centre
    (x = 0) round to the bottom centre (x = 0); the other side is mirrored. Non-flat quads become two triangles,
    caps close the ends. swatch='ghorkov.hull' paints it at once (see studio_swatches), else material/uv as in
    create_primitive."""
    lm = ctx.lm
    lm.require_topology_edit()
    points, faces = loft_mesh(list(sections or []), axis, bool(half), caps)
    choice = _reference_for(lm, _placeholder_block(lm, material) if swatch else material)
    spec, reference_uvs = _uv_spec(lm, uv, choice.reference_uvs, choice.density, choice.region)
    first = len(lm.points)
    for p in points:
        lm.append_point(meshmath.rounded(p, 4))
    created = []
    for ring in faces:
        ring = [first + i for i in ring]
        group = uvtools.compute(spec, [lm.points[i] for i in ring], reference=reference_uvs, notes=ctx.notes)
        created.append(lm.append_polygon(ring, choice.block, choice.atts, group))
    result = {"polygons": created, "points": list(range(first, len(lm.points))), "block": choice.block,
              "bbox": meshmath.bbox(points)}
    if swatch:
        result["paint"] = paint_polygons(ctx, created, swatch=swatch)
    return result


# --- paint -----------------------------------------------------------------------------------


def _target_block(lm, texture: str, exclude=()) -> int | None:
    """An amesh block with this texture, textured, opaque and with UVs: new paint goes there."""
    for index in lm.editable_amesh_blocks():
        block = lm.blocks[index]
        if index in exclude or block.uses_atts_only_mapping or not block.textured or block.tracy_mode != "none":
            continue
        if block.texture and block.texture.name.casefold() == texture.casefold():
            return index
    return None


def _template_block(lm) -> int | None:
    """A plain block (textured, opaque, with UVs) to clone when the model has no block with the wanted texture."""
    plain = [i for i in lm.editable_amesh_blocks() if not lm.blocks[i].uses_atts_only_mapping
             and lm.blocks[i].textured and lm.blocks[i].tracy_mode == "none"]
    return max(plain, key=lambda i: len(lm.blocks[i].atts)) if plain else None


def _paint_uvs(pts, rect, mapping: str, texels_per_unit: float, rotate_90: int, flip_u: bool, flip_v: bool):
    if mapping == "world":
        group, _ = uvtools.world(pts, texels_per_unit, region=rect)
    elif mapping in ("stretch", "aspect"):
        group = uvtools.fit(pts, rect, keep_aspect=(mapping == "aspect"), natural=True)
    else:
        raise core.McpToolError("mapping must be 'stretch' (one face = the whole patch), 'aspect' or 'world'")
    if rotate_90 or flip_u or flip_v:
        group = uvtools.adjust(group, rotate_90=rotate_90, flip_u=flip_u, flip_v=flip_v)
    return group


def paint_polygons(ctx, ids, swatch=None, texture=None, rect=None, mapping: str = "stretch",
                   texels_per_unit: float = 1.0, rotate_90: int = 0, flip_u: bool = False, flip_v: bool = False,
                   shade=None) -> dict:
    lm = ctx.lm
    if swatch:
        entry = swatch_entry(swatch)
        texture, rect = texture or entry["texture"], rect or entry["rect"]
    if not texture or not rect or len(rect) != 4:
        raise core.McpToolError("give swatch='faction.part' (studio_swatches) or texture + rect [u0, v0, u1, v1]")
    ids = sorted(set(ids))
    if not ids:
        raise core.McpToolError("The selector matched no polygons")
    table = lm.slot_table()
    for pid in ids:
        if pid not in table:
            raise core.McpToolError(f"Polygon #{pid} has no material yet: map it first (studio_map_polygons)")
        if lm.block_class(table[pid][0]) != "amesh.class":
            raise core.McpToolError(f"Polygon #{pid} is animated (area.class): paint works on normal polygons only")
    target = _target_block(lm, texture)
    created_block = retexture = None
    if target is None:
        donor = donor_block(texture)
        if donor is not None:            # copy an original block that already uses this texture
            target = created_block = op_add_material_block(ctx, from_block=donor[1], source=donor[0])["block"]
        else:                            # clone a plain block and rename its texture (name must not be longer)
            template = _template_block(lm)
            if template is None:
                raise core.McpToolError("The model has no plain textured block to copy: add one with "
                                        "studio_add_material_block (source = a vehicle using that texture)")
            target = created_block = retexture = op_add_material_block(ctx, from_block=template)["block"]
    for pid in ids:
        entry = lm.blocks[table[pid][0]].atts[table[pid][1]]
        values = (entry.color_val, entry.shade_val if shade is None else int(shade), entry.tracy_val, entry.pad)
        lm.move_polygon_to_block(pid, target, atts=values, uvs=[(0, 0)] * len(lm.polygons[pid]), table=table)
        table = lm.slot_table()
    if retexture is not None:
        op_set_texture(ctx, select=ids, texture=texture)          # the new block holds only these: retextured in place
    table = lm.slot_table()
    for pid in ids:
        group = _paint_uvs(lm.polygon_points(pid), rect, mapping, texels_per_unit, rotate_90, flip_u, flip_v)
        lm.set_uvs(pid, group, table)
    return {"painted": len(ids), "texture": texture, "rect": list(rect), "block": target,
            "new_block": created_block is not None}


@op("paint")
def op_paint(ctx, select=None, swatch=None, texture=None, rect=None, mapping: str = "stretch",
             texels_per_unit: float = 1.0, rotate_90: int = 0, flip_u: bool = False, flip_v: bool = False, shade=None):
    """Texture polygons with a named faction swatch, e.g. swatch='taerkasten.plate' (list: studio_swatches), or an
    explicit texture + rect [u0,v0,u1,v1]. Polygons go to (or get) an opaque block with that texture.
    mapping 'stretch' (default) lays the whole patch on every face, like the original models; 'aspect' keeps its
    proportions; 'world' keeps one texel size (texels_per_unit) and tiles inside the patch. rotate_90 / flip_u /
    flip_v turn the patch. shade = fixed shade for these faces (normally use auto_shade at the end)."""
    return paint_polygons(ctx, ctx.polygons(_need(select, "polygon")), swatch, texture, rect, mapping,
                          texels_per_unit, rotate_90, flip_u, flip_v, shade)


# --- baked light ---------------------------------------------------------------------------------


@op("auto_shade")
def op_auto_shade(ctx, select=None, base: float = SHADE_BASE, vertical: float = SHADE_VERTICAL,
                  front: float = SHADE_FRONT, offset: float = 0.0):
    """Bake the light the way the original models do (the engine has no lighting; 0 = bright, higher = darker):
    shade = base + vertical*ny - front*nz + offset from each face's outward normal, which gives about top 5,
    front 30, sides 50, back 70, belly 95. Run it as the LAST step on new or repainted parts; animated,
    transparent (FX) and untextured polygons are left alone. offset darkens (+) or lightens (-) a whole selection."""
    lm = ctx.lm
    ids = ctx.polygons(select) if select is not None else list(range(len(lm.polygons)))
    table = lm.slot_table()
    changed, skipped, values = [], 0, []
    for pid in ids:
        block = lm.blocks[table[pid][0]] if pid in table else None
        if block is None or lm.block_class(table[pid][0]) != "amesh.class" or not block.textured                 or block.tracy_mode != "none":          # flames, smoke and tendrils keep their own values
            skipped += 1
            continue
        normal = meshmath.outward_normal(lm.polygon_points(pid))
        if normal is None:
            skipped += 1
            continue
        value = int(round(min(max(base + vertical * normal[1] - front * normal[2] + offset, 0), 250)))
        entry = lm.blocks[table[pid][0]].atts[table[pid][1]]
        lm.move_polygon_to_block(pid, table[pid][0], atts=(entry.color_val, value, entry.tracy_val, entry.pad),
                                 table=table)
        changed.append(pid)
        values.append(value)
    result = {"shaded": len(changed), "skipped": skipped}
    if values:
        result["shade_range"] = [min(values), max(values)]
    return result


def describe_swatches(faction: str = "") -> dict:
    rows = {k: v for k, v in swatches().items() if not faction or k.startswith(faction.lower())}
    return {"swatches": rows, "factions": sorted({k.split(".")[0] for k in swatches()}),
            "use": "studio_paint(select, swatch=name) or loft/create_primitive with swatch=name; finish with auto_shade",
            "board": str(SWATCH_FILE.with_suffix(".png"))}

