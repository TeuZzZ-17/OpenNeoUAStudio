"""Small vector and polygon helpers shared by the Studio MCP modeling tools (pure Python, no Qt).

Winding convention of Urban Assault models (measured on the original sets): the OUTWARD normal of a
polygon is the NEGATIVE of the standard Newell normal, because the engine uses x right, y down, z forward.
Every function that needs "the front of a polygon" goes through ``outward_normal`` so the rule lives here.
"""

from __future__ import annotations

import math

Vec = tuple[float, float, float]


def sub(a, b) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def add(a, b) -> Vec:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def scale(a, k: float) -> Vec:
    return (a[0] * k, a[1] * k, a[2] * k)


def dot(a, b) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a, b) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def length(a) -> float:
    return math.sqrt(dot(a, a))


def unit(a) -> Vec | None:
    size = length(a)
    return None if size < 1e-12 else (a[0] / size, a[1] / size, a[2] / size)


def distance(a, b) -> float:
    return length(sub(a, b))


def lerp(a, b, t: float) -> Vec:
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t)


def centroid(points) -> Vec:
    count = float(len(points))
    return (sum(p[0] for p in points) / count, sum(p[1] for p in points) / count,
            sum(p[2] for p in points) / count)


def bbox(points) -> dict:
    if not points:
        return {}
    cols = list(zip(*points))
    return {"min": [round(min(c), 4) for c in cols], "max": [round(max(c), 4) for c in cols],
            "size": [round(max(c) - min(c), 4) for c in cols]}


def newell(points) -> Vec:
    """Standard Newell normal (its length is twice the polygon area)."""
    nx = ny = nz = 0.0
    for index, (x0, y0, z0) in enumerate(points):
        x1, y1, z1 = points[(index + 1) % len(points)]
        nx += (y0 - y1) * (z0 + z1)
        ny += (z0 - z1) * (x0 + x1)
        nz += (x0 - x1) * (y0 + y1)
    return (nx, ny, nz)


def outward_normal(points) -> Vec | None:
    """Unit normal of the visible side of a UA polygon, None for a degenerate polygon."""
    return unit(scale(newell(points), -1.0))


def area(points) -> float:
    return 0.5 * length(newell(points))


def planarity_error(points) -> float:
    """Largest distance of a vertex from the polygon's best plane (0 for triangles)."""
    if len(points) <= 3:
        return 0.0
    normal = unit(newell(points))
    if normal is None:
        return 0.0
    origin = centroid(points)
    return max(abs(dot(sub(p, origin), normal)) for p in points)


def is_convex(points) -> bool:
    """True when the polygon turns the same way at every corner (planar polygons)."""
    if len(points) <= 3:
        return True
    normal = unit(newell(points))
    if normal is None:
        return False
    sign = 0
    for index in range(len(points)):
        a, b, c = points[index], points[(index + 1) % len(points)], points[(index + 2) % len(points)]
        turn = dot(cross(sub(b, a), sub(c, b)), normal)
        if abs(turn) < 1e-9:
            continue
        current = 1 if turn > 0 else -1
        if sign and current != sign:
            return False
        sign = current
    return True


def rotate_xyz(point, rx: float, ry: float, rz: float) -> Vec:
    """Rotate by radians around X, then Y, then Z (same order as studio_transform_points)."""
    x, y, z = point
    c, s = math.cos(rx), math.sin(rx)
    y, z = y * c - z * s, y * s + z * c
    c, s = math.cos(ry), math.sin(ry)
    x, z = x * c + z * s, -x * s + z * c
    c, s = math.cos(rz), math.sin(rz)
    x, y = x * c - y * s, x * s + y * c
    return (x, y, z)


AXES = {"x": 0, "y": 1, "z": 2}


def axis_index(axis) -> int:
    if isinstance(axis, int) and axis in (0, 1, 2):
        return axis
    key = str(axis).strip().lower()
    if key in AXES:
        return AXES[key]
    raise ValueError(f"axis must be 'x', 'y' or 'z', got {axis!r}")


def rounded(point, digits: int = 3) -> Vec:
    return (round(float(point[0]), digits), round(float(point[1]), digits), round(float(point[2]), digits))
