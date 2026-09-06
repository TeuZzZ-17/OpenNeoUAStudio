"""Small screen-space label placement helpers used by editor overlays.

The helpers deliberately know nothing about Qt.  They place rectangular text
labels around an anchor while treating editor geometry as hard obstacles.
Background grids are intentionally not represented here, so callers can let
labels overlap the grid while still avoiding actual editable content.
"""

from __future__ import annotations

from math import inf


ScreenPoint = tuple[float, float]
ScreenRect = tuple[float, float, float, float]  # left, top, right, bottom
ScreenSegment = tuple[ScreenPoint, ScreenPoint]


def _expanded(rect: ScreenRect, padding: float) -> ScreenRect:
    left, top, right, bottom = rect
    return (
        left - padding,
        top - padding,
        right + padding,
        bottom + padding,
    )


def _rects_intersect(first: ScreenRect, second: ScreenRect) -> bool:
    a_left, a_top, a_right, a_bottom = first
    b_left, b_top, b_right, b_bottom = second
    return not (
        a_right <= b_left
        or a_left >= b_right
        or a_bottom <= b_top
        or a_top >= b_bottom
    )


def _segment_intersects_rect(
    segment: ScreenSegment,
    rect: ScreenRect,
    padding: float = 0.0,
) -> bool:
    """Return whether a line segment intersects an axis-aligned rectangle."""

    left, top, right, bottom = _expanded(rect, padding)
    (x0, y0), (x1, y1) = segment

    # Liang-Barsky clipping.  It is compact, stable for vertical/horizontal
    # links, and avoids a Qt dependency in the layout helper itself.
    dx = x1 - x0
    dy = y1 - y0
    t_min = 0.0
    t_max = 1.0

    for p, q in (
        (-dx, x0 - left),
        (dx, right - x0),
        (-dy, y0 - top),
        (dy, bottom - y0),
    ):
        if abs(p) <= 1.0e-12:
            if q < 0.0:
                return False
            continue
        t = q / p
        if p < 0.0:
            if t > t_max:
                return False
            if t > t_min:
                t_min = t
        else:
            if t < t_min:
                return False
            if t < t_max:
                t_max = t

    return t_min <= t_max


def _inside(rect: ScreenRect, bounds: ScreenRect) -> bool:
    left, top, right, bottom = rect
    b_left, b_top, b_right, b_bottom = bounds
    return (
        left >= b_left
        and top >= b_top
        and right <= b_right
        and bottom <= b_bottom
    )


def _clear(
    rect: ScreenRect,
    point_obstacles: tuple[ScreenRect, ...],
    segments: tuple[ScreenSegment, ...],
    occupied_labels: tuple[ScreenRect, ...],
) -> bool:
    padded_label = _expanded(rect, 2.5)
    if any(_rects_intersect(padded_label, obstacle) for obstacle in point_obstacles):
        return False
    if any(_rects_intersect(padded_label, other) for other in occupied_labels):
        return False
    if any(_segment_intersects_rect(segment, rect, 3.0) for segment in segments):
        return False
    return True


def _candidate_rects(
    anchor: ScreenPoint,
    width: float,
    height: float,
):
    x, y = anchor
    # Near positions come first.  Extra rings let dense wireframes push a
    # number farther away rather than drawing it on top of editable geometry.
    for gap in (8.0, 12.0, 18.0, 26.0, 36.0, 48.0, 64.0, 84.0, 108.0, 136.0):
        offsets = (
            (gap, -height - gap),          # north-east
            (gap, gap),                    # south-east
            (-width - gap, -height - gap), # north-west
            (-width - gap, gap),           # south-west
            (-width * 0.5, -height - gap), # north
            (-width * 0.5, gap),           # south
            (gap, -height * 0.5),          # east
            (-width - gap, -height * 0.5), # west
        )
        for dx, dy in offsets:
            left = x + dx
            top = y + dy
            yield (left, top, left + width, top + height)


def choose_screen_label_rect(
    anchor: ScreenPoint,
    size: tuple[float, float],
    bounds: ScreenRect,
    *,
    point_obstacles: tuple[ScreenRect, ...] = (),
    segments: tuple[ScreenSegment, ...] = (),
    occupied_labels: tuple[ScreenRect, ...] = (),
) -> ScreenRect | None:
    """Choose the nearest collision-free label rectangle.

    Editable vertices, links and already-placed labels are hard obstacles.
    When the local rings are full, a deterministic viewport scan finds the
    nearest remaining free slot.  The grid is absent by design and therefore
    never influences placement.
    """

    width = max(float(size[0]), 1.0)
    height = max(float(size[1]), 1.0)

    for rect in _candidate_rects(anchor, width, height):
        if _inside(rect, bounds) and _clear(
            rect, point_obstacles, segments, occupied_labels
        ):
            return rect

    # Dense fallback: search the entire visible workspace and choose the free
    # rectangle closest to the owning vertex.  This is intentionally a final
    # fallback so normal labels remain visually attached to their vertex.
    left, top, right, bottom = bounds
    max_left = right - width
    max_top = bottom - height
    if max_left < left or max_top < top:
        return None

    best_rect: ScreenRect | None = None
    best_distance = inf
    step = max(4.0, min(width, height) * 0.45)
    y = top
    while y <= max_top + 1.0e-9:
        x = left
        while x <= max_left + 1.0e-9:
            rect = (x, y, x + width, y + height)
            if _clear(rect, point_obstacles, segments, occupied_labels):
                center_x = x + width * 0.5
                center_y = y + height * 0.5
                distance = (center_x - anchor[0]) ** 2 + (center_y - anchor[1]) ** 2
                if distance < best_distance:
                    best_distance = distance
                    best_rect = rect
            x += step
        y += step

    return best_rect
