"""Primitive shapes for studio_create_primitive: pure geometry, polygons already wound to face outward.

Sizes are full extents along x, y, z (not radii). y grows DOWN in Urban Assault, so "top" means negative y.
"""

from __future__ import annotations

import math

from studio_mcp import core, meshmath

SHAPES = ("box", "wedge", "prism", "sphere", "plane")


def _finalize(points: list, polygons: list, facing_up: bool = False):
    """Merge coincident points, drop collapsed polygons and wind the rest to face outward.

    "Outward" means away from the mean of the corners, which lies inside every solid made here (convex)."""
    index_of: dict[tuple, int] = {}
    merged, remap = [], []
    for point in points:
        key = tuple(round(v, 7) for v in point)
        if key not in index_of:
            index_of[key] = len(merged)
            merged.append(point)
        remap.append(index_of[key])
    center = meshmath.centroid(merged)
    result = []
    for polygon in polygons:
        ring = []
        for index in (remap[i] for i in polygon):
            if not ring or ring[-1] != index:
                ring.append(index)
        if len(ring) > 1 and ring[0] == ring[-1]:
            ring.pop()
        if len(set(ring)) < 3:
            continue
        pts = [merged[i] for i in ring]
        normal = meshmath.outward_normal(pts)
        if normal is None:
            continue
        reference = (0.0, -1.0, 0.0) if facing_up else meshmath.sub(meshmath.centroid(pts), center)
        if meshmath.dot(normal, reference) < 0:
            ring.reverse()
        result.append(ring)
    return merged, result


def _pose(points: list, center, rotate_deg):
    rx, ry, rz = (math.radians(a) for a in rotate_deg)
    return [meshmath.add(meshmath.rotate_xyz(p, rx, ry, rz), center) for p in points]


def make(shape: str, center=(0.0, 0.0, 0.0), size=(2.0, 2.0, 2.0), segments: int = 8, rings: int = 4,
         top_scale=1.0, taper=1.0, caps: str = "both", rotate_deg=(0.0, 0.0, 0.0), phase_deg: float | None = None,
         cells=(1, 1)) -> tuple[list, list]:
    """Build a shape; returns (points, polygons) in model space."""
    if shape not in SHAPES:
        raise core.McpToolError(f"Unknown shape {shape!r}. Use one of {list(SHAPES)}")
    if len(size) != 3 or any(float(v) < 0 for v in size):
        raise core.McpToolError("size must be [sx, sy, sz] with values >= 0")
    hx, hy, hz = (float(v) / 2.0 for v in size)
    facing_up = False

    if shape == "box":
        tx, tz = (taper, taper) if isinstance(taper, (int, float)) else taper
        ring = [(-1, -1), (1, -1), (1, 1), (-1, 1)]
        points = [(sx * hx * tx, -hy, sz * hz * tz) for sx, sz in ring] + [(sx * hx, hy, sz * hz) for sx, sz in ring]
        polygons = [[0, 1, 2, 3], [4, 5, 6, 7]] + [[i, (i + 1) % 4, 4 + (i + 1) % 4, 4 + i] for i in range(4)]
    elif shape == "wedge":
        # ramp: flat bottom, tall back wall (z = -hz), slope falling to the front edge (z = +hz)
        points = [(-hx, hy, -hz), (hx, hy, -hz), (hx, hy, hz), (-hx, hy, hz), (-hx, -hy, -hz), (hx, -hy, -hz)]
        polygons = [[0, 1, 2, 3], [0, 1, 5, 4], [4, 5, 2, 3], [0, 3, 4], [1, 2, 5]]
    elif shape == "prism":
        count = int(segments)
        if count < 3 or count > 64:
            raise core.McpToolError("segments must be between 3 and 64")
        phase = math.radians(45.0 if (phase_deg is None and count == 4) else (phase_deg or 0.0))
        scale_top = float(top_scale)
        if scale_top < 0:
            raise core.McpToolError("top_scale must be >= 0 (0 gives a cone)")
        bottom = [(hx * math.cos(phase + 2 * math.pi * i / count), hy, hz * math.sin(phase + 2 * math.pi * i / count))
                  for i in range(count)]
        top = [(hx * scale_top * math.cos(phase + 2 * math.pi * i / count), -hy,
                hz * scale_top * math.sin(phase + 2 * math.pi * i / count)) for i in range(count)]
        points = top + bottom
        polygons = [[i, (i + 1) % count, count + (i + 1) % count, count + i] for i in range(count)]
        if caps in ("both", "top"):
            polygons.append(list(range(count)))
        if caps in ("both", "bottom"):
            polygons.append(list(range(count, 2 * count)))
        if caps not in ("both", "top", "bottom", "none"):
            raise core.McpToolError("caps must be 'both', 'top', 'bottom' or 'none'")
    elif shape == "sphere":
        count, bands = int(segments), int(rings)
        if count < 3 or count > 64 or bands < 2 or bands > 64:
            raise core.McpToolError("segments must be 3..64 and rings 2..64")
        points, polygons = [], []
        for r in range(bands + 1):
            angle = math.pi * r / bands
            for i in range(count):
                theta = 2 * math.pi * i / count
                points.append((hx * math.sin(angle) * math.cos(theta), -hy * math.cos(angle),
                               hz * math.sin(angle) * math.sin(theta)))
        for r in range(bands):
            for i in range(count):
                a, b = r * count + i, r * count + (i + 1) % count
                polygons.append([a, b, b + count, a + count])
    else:  # plane
        nx, nz = int(cells[0]), int(cells[1])
        if not (1 <= nx <= 64 and 1 <= nz <= 64):
            raise core.McpToolError("cells must be 1..64 on each side")
        points = [(-hx + 2 * hx * i / nx, 0.0, -hz + 2 * hz * j / nz) for j in range(nz + 1) for i in range(nx + 1)]
        polygons = [[j * (nx + 1) + i, j * (nx + 1) + i + 1, (j + 1) * (nx + 1) + i + 1, (j + 1) * (nx + 1) + i]
                    for j in range(nz) for i in range(nx)]
        facing_up = True
    points, polygons = _finalize(points, polygons, facing_up)
    if not polygons:
        raise core.McpToolError("The shape collapsed to nothing: check size and parameters")
    return _pose(points, center, rotate_deg), polygons
