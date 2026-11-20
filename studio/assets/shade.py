"""Baked polygon shade (ATTS shade byte): 0 = full light, higher = darker.

The engine has no lighting: every polygon of an original model carries a hand-made shade. These helpers
set it on the selected polygons of an editable model, either to a fixed value or from the face's own
direction with the values measured on the original Set1 vehicles (top 5, front 30, sides 50, back 70,
belly 95).  Shared by the Model Editor panel and the Studio MCP `auto_shade` operation.
"""

from __future__ import annotations

from typing import Iterable

SHADE_BASE, SHADE_VERTICAL, SHADE_FRONT = 50.0, 45.0, 20.0
SHADE_MAX = 250


def outward_normal(points) -> tuple[float, float, float] | None:
    """Unit normal of the visible side of a UA polygon (x right, y down, z forward); None if degenerate."""

    nx = ny = nz = 0.0
    count = len(points)
    for index in range(count):
        ax, ay, az = points[index]
        bx, by, bz = points[(index + 1) % count]
        nx += (ay - by) * (az + bz)
        ny += (az - bz) * (ax + bx)
        nz += (ax - bx) * (ay + by)
    length = (nx * nx + ny * ny + nz * nz) ** 0.5
    if length < 1e-12:
        return None
    return (-nx / length, -ny / length, -nz / length)


def auto_shade_value(points, *, base: float = SHADE_BASE, vertical: float = SHADE_VERTICAL,
                     front: float = SHADE_FRONT, offset: float = 0.0) -> int | None:
    """shade = base + vertical*ny - front*nz + offset, clamped to 0..250; None for a degenerate polygon."""

    normal = outward_normal(points)
    if normal is None:
        return None
    value = base + vertical * normal[1] - front * normal[2] + offset
    return int(round(min(max(value, 0), SHADE_MAX)))


def shadable_entries(fam_obj, poly_ids: Iterable[int] | None = None):
    """(poly_id, ATTS entry) of the plain textured, opaque AMESH polygons (flames, smoke and ANM keep their own)."""

    wanted = None if poly_ids is None else set(poly_ids)
    for block in fam_obj.base_object.ades:
        if (block.class_id or "").lower() != "amesh.class" or not block.textured \
                or block.tracy_mode != "none":
            continue
        for entry in block.atts:
            if wanted is None or entry.poly_id in wanted:
                yield entry.poly_id, entry


def apply_polygon_shade(fam_obj, poly_ids: Iterable[int] | None = None, *, value: int | None = None,
                        offset: float = 0.0, delta: int | None = None) -> dict:
    """Shade ``poly_ids`` (all when None): a fixed ``value``, a ``delta`` added to each current shade (+ darker),
    or, with neither, auto from each face's direction.

    Returns {"shaded": n, "skipped": n, "range": (min, max) | None}; polygons outside the eligible AMESH blocks
    are skipped.
    """

    model = fam_obj.skeleton
    if model is None:
        raise ValueError("the model has no skeleton to shade")
    if value is not None and not 0 <= int(value) <= SHADE_MAX:
        raise ValueError(f"shade must be 0..{SHADE_MAX}")
    ids = list(range(len(model.polygons))) if poly_ids is None else list(poly_ids)
    eligible = dict(shadable_entries(fam_obj, ids))
    values = []
    for poly_id in ids:
        entry = eligible.get(poly_id)
        if entry is None:
            continue
        if value is not None:
            new_value = int(value)
        elif delta is not None:
            new_value = min(max(entry.shade_val + int(delta), 0), SHADE_MAX)
        else:
            points = [model.points[index] for index in model.polygons[poly_id]]
            new_value = auto_shade_value(points, offset=offset)
            if new_value is None:
                continue
        entry.shade_val = new_value
        values.append(new_value)
    return {"shaded": len(values), "skipped": len(ids) - len(values),
            "range": (min(values), max(values)) if values else None}


def shade_offset(fam_obj, poly_ids: Iterable[int] | None = None, *, limit: int = 100) -> int:
    """The darker(+)/lighter(-) level the polygons currently carry, relative to the automatic shade.

    It is read back from the saved ATTS bytes (nothing extra is stored): the offset ``d`` whose automatic shade
    ``clamp(auto + d)`` matches the actual values best, so an Auto Shade model reads 0, one darkened by 37 reads
    37 again after saving and reopening, and a hand-shaded original reads how far it sits from the automatic look.
    Ties (everything clamped) resolve to the smallest |d|. 0 when no polygon is eligible.
    """

    model = fam_obj.skeleton
    if model is None:
        return 0
    pairs = []
    for poly_id, entry in shadable_entries(fam_obj, poly_ids):
        if not 0 <= poly_id < len(model.polygons):
            continue
        auto = auto_shade_value([model.points[i] for i in model.polygons[poly_id]])
        if auto is not None:
            pairs.append((auto, entry.shade_val))
    if not pairs:
        return 0
    best, best_error = 0, None
    for d in sorted(range(-limit, limit + 1), key=abs):
        error = sum(abs(min(max(auto + d, 0), SHADE_MAX) - actual) for auto, actual in pairs)
        if best_error is None or error < best_error:
            best, best_error = d, error
    return best
