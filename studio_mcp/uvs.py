"""UV generation for the MCP modeling tools.

UVs are one (u, v) byte pair per polygon corner, 0..255, on the block's texture (a 256x256 bitmap).
A UV *spec* is a dict with a "mode":
  planar  - project along the polygon's dominant axis and stretch to fill ``rect`` (default [16,16,240,240])
  rect    - same, but into an explicit ``rect`` [u0, v0, u1, v1]
  world   - one texture pixel = ``1/texels_per_unit`` model units, so neighbours get the same texel size;
            optional ``region`` [u0,v0,u1,v1] keeps the polygons inside that part of the texture
  copy    - take the UVs of ``from_polygon`` (same corner count) else fit into that polygon's UV box
  explicit- ``uvs`` is the list of [u, v] pairs
  adjust  - change the polygon's current UVs: ``offset`` [du, dv], ``scale`` [su, sv] or number,
            ``flip_u``, ``flip_v``, ``rotate_90`` (turns, around the UV box center)
"""

from __future__ import annotations

import math

from studio_mcp import core, meshmath

DEFAULT_RECT = (16, 16, 240, 240)
UV_MODES = ("planar", "rect", "world", "copy", "explicit", "adjust")


def project(points, natural: bool = False) -> list[tuple[float, float]]:
    """Plane coordinates of the corners, dropping the polygon's dominant normal axis.

    natural=False keeps the axis order of Studio's own planar repair (``planar_uvs``);
    natural=True puts v on the vertical y axis for wall-like faces so textures stand upright.
    """
    normal = meshmath.newell(points)
    dominant = max(range(3), key=lambda i: abs(normal[i]))
    axes = [i for i in range(3) if i != dominant]
    if natural and dominant == 0:
        axes = [2, 1]
    return [(p[axes[0]], p[axes[1]]) for p in points]


def fit(points, rect=DEFAULT_RECT, keep_aspect: bool = False, natural: bool = False) -> list[tuple[int, int]]:
    """Stretch the planar projection of ``points`` over ``rect`` = (u0, v0, u1, v1) (natural: see ``project``)."""
    if len(points) < 3:
        raise core.McpToolError("UVs need at least 3 corners")
    u0, v0, u1, v1 = (float(x) for x in rect)
    coords = project(points, natural)
    us, vs = [c[0] for c in coords], [c[1] for c in coords]
    du, dv = (max(us) - min(us)) or 1.0, (max(vs) - min(vs)) or 1.0
    su, sv = (u1 - u0) / du, (v1 - v0) / dv
    if keep_aspect:
        su = sv = min(abs(su), abs(sv))
    return [(round(u0 + (u - min(us)) * su), round(v0 + (v - min(vs)) * sv)) for u, v in coords]


def world(points, texels_per_unit: float = 1.0, offset=(0.0, 0.0), region=None) -> tuple[list[tuple[int, int]], bool]:
    """Constant texel size. Returns (uvs, shrunk): shrunk is True if the polygon was too big for the texture.

    The polygon sits where its world position falls on the texture, wrapping by whole tiles, so neighbouring
    polygons continue the pattern. ``region`` = (u0, v0, u1, v1) is the tile (default: the whole 256x256 texture);
    a polygon that would cross a tile edge slides back inside."""
    if texels_per_unit <= 0:
        raise core.McpToolError("texels_per_unit must be greater than 0")
    u0, v0, u1, v1 = (float(x) for x in (region or (0, 0, 255, 255)))
    tile_u, tile_v = max(u1 - u0, 8.0), max(v1 - v0, 8.0)
    coords = project(points, natural=True)
    us = [c[0] * texels_per_unit + offset[0] for c in coords]
    vs = [c[1] * texels_per_unit + offset[1] for c in coords]
    shrunk = False
    if max(max(us) - min(us), 1e-9) > tile_u or max(max(vs) - min(vs), 1e-9) > tile_v:
        u0, v0, tile_u, tile_v = 0.0, 0.0, 255.0, 255.0       # bigger than the region: use the whole texture
    span = max(max(us) - min(us), max(vs) - min(vs))
    if span > 255:
        factor, shrunk = 255.0 / span, True
        us, vs = [u * factor for u in us], [v * factor for v in vs]

    def fit_axis(values, low, size):
        shift = -math.floor((min(values) - low) / size) * size      # whole tiles: min lands inside [low, low + size)
        overflow = max(values) + shift - (low + size)
        return shift - overflow if overflow > 0 else shift          # crossing the tile edge: slide back inside

    shift_u, shift_v = fit_axis(us, u0, tile_u), fit_axis(vs, v0, tile_v)
    return [(round(u + shift_u), round(v + shift_v)) for u, v in zip(us, vs)], shrunk


def uv_box(uvs) -> tuple[int, int, int, int]:
    return (min(u for u, _ in uvs), min(v for _, v in uvs), max(u for u, _ in uvs), max(v for _, v in uvs))


def adjust(uvs, offset=(0, 0), scale=(1.0, 1.0), flip_u=False, flip_v=False, rotate_90=0):
    """Offset / scale / flip / quarter-turn existing UVs around their box center."""
    if isinstance(scale, (int, float)):
        scale = (float(scale), float(scale))
    u0, v0, u1, v1 = uv_box(uvs)
    cu, cv = (u0 + u1) / 2.0, (v0 + v1) / 2.0
    result = []
    for u, v in uvs:
        x, y = u - cu, v - cv
        for _ in range(int(rotate_90) % 4):
            x, y = -y, x
        if flip_u:
            x = -x
        if flip_v:
            y = -y
        result.append((round(cu + x * scale[0] + offset[0]), round(cv + y * scale[1] + offset[1])))
    return result


def compute(spec, points, current=None, reference=None, notes: list | None = None):
    """UVs for one polygon from a UV spec.

    ``points``     corner coordinates;  ``current`` its present UVs (for adjust);
    ``reference``  UVs of the polygon the new one is modeled on (for copy and the default patch).
    """
    spec = dict(spec or {})
    mode = spec.get("mode") or ("copy" if reference is not None and len(reference) == len(points) else
                                ("planar" if reference is None else "rect"))
    if mode not in UV_MODES:
        raise core.McpToolError(f"Unknown uv mode {mode!r}. Use one of {list(UV_MODES)}")
    keep_aspect = bool(spec.get("keep_aspect", False))
    if mode == "planar":
        return fit(points, spec.get("rect") or (uv_box(reference) if reference else DEFAULT_RECT), keep_aspect)
    if mode == "rect":
        rect = spec.get("rect") or (uv_box(reference) if reference else None)
        if rect is None or len(rect) != 4:
            raise core.McpToolError("uv mode 'rect' needs rect [u0, v0, u1, v1]")
        return fit(points, rect, keep_aspect)
    if mode == "world":
        uvs, shrunk = world(points, float(spec.get("texels_per_unit", 1.0)), spec.get("offset", (0.0, 0.0)),
                            spec.get("region"))
        if shrunk and notes is not None:
            notes.append("a polygon was larger than the 256-pixel texture: its UVs were scaled down")
        return uvs
    if mode == "copy":
        if reference is None:
            raise core.McpToolError("uv mode 'copy' needs from_polygon")
        if len(reference) == len(points):
            return [tuple(uv) for uv in reference]
        return fit(points, uv_box(reference), keep_aspect)
    if mode == "explicit":
        uvs = spec.get("uvs")
        if not uvs or len(uvs) != len(points):
            raise core.McpToolError(f"uv mode 'explicit' needs {len(points)} [u, v] pairs in 'uvs'")
        return [tuple(uv) for uv in uvs]
    if current is None:
        raise core.McpToolError("uv mode 'adjust' needs a polygon that already has UVs")
    return adjust(current, spec.get("offset", (0, 0)), spec.get("scale", (1.0, 1.0)), bool(spec.get("flip_u")),
                  bool(spec.get("flip_v")), int(spec.get("rotate_90", 0)))
