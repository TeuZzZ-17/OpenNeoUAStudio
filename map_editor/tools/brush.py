from __future__ import annotations

import math
from enum import Enum

from ..core.ldf_model import HGT_MAX, HGT_MIN, LdfDocument


class BrushMode(str, Enum):
    RAISE = "raise"
    LOWER = "lower"
    FLATTEN = "flatten"
    SMOOTH = "smooth"


class BrushShape(str, Enum):
    SQUARE = "square"
    ROUND = "round"
    DIAMOND = "diamond"
    CROSS = "cross"
    RING = "ring"


_ROUND_OUTLINE_SEGMENTS = 64
_CROSS_ARM_FRACTION = 0.35
_RING_INNER_RADIUS = 0.45


def _brush_shape(shape: BrushShape | str) -> BrushShape:
    try:
        return BrushShape(shape)
    except (TypeError, ValueError):
        return BrushShape.SQUARE


def _ellipse_loop(radius_x: float, radius_z: float,
                  scale: float = 1.0) -> list[tuple[float, float]]:
    return [
        (radius_x * scale * math.cos(2 * math.pi * index / _ROUND_OUTLINE_SEGMENTS),
         radius_z * scale * math.sin(2 * math.pi * index / _ROUND_OUTLINE_SEGMENTS))
        for index in range(_ROUND_OUTLINE_SEGMENTS)
    ]


def brush_outlines(shape: BrushShape | str, radius_x: float,
                   radius_z: float) -> list[list[tuple[float, float]]]:
    """Restituisce i contorni del pennello in unità di settore, centrati in (0, 0)."""
    shape = _brush_shape(shape)
    radius_x = max(0.0, float(radius_x))
    radius_z = max(0.0, float(radius_z))

    if shape == BrushShape.SQUARE:
        return [[(-radius_x, -radius_z), (radius_x, -radius_z),
                 (radius_x, radius_z), (-radius_x, radius_z)]]
    if shape == BrushShape.ROUND:
        return [_ellipse_loop(radius_x, radius_z)]
    if shape == BrushShape.DIAMOND:
        return [[(-radius_x, 0.0), (0.0, -radius_z),
                 (radius_x, 0.0), (0.0, radius_z)]]
    if shape == BrushShape.CROSS:
        half_x = radius_x * _CROSS_ARM_FRACTION
        half_z = radius_z * _CROSS_ARM_FRACTION
        return [[(-half_x, -radius_z), (half_x, -radius_z),
                 (half_x, -half_z), (radius_x, -half_z),
                 (radius_x, half_z), (half_x, half_z),
                 (half_x, radius_z), (-half_x, radius_z),
                 (-half_x, half_z), (-radius_x, half_z),
                 (-radius_x, -half_z), (-half_x, -half_z)]]
    return [_ellipse_loop(radius_x, radius_z),
            _ellipse_loop(radius_x, radius_z, _RING_INNER_RADIUS)]


def brush_footprint(doc: LdfDocument, col: int, row: int, radius: float,
                    radius_z: float | None = None,
                    shape: BrushShape | str = BrushShape.SQUARE):
    """Celle interne (bordo escluso); il peso serve a Flatten e Smooth."""
    cells = []
    radius_x = max(0.0, float(radius))
    radius_z = radius_x if radius_z is None else max(0.0, float(radius_z))
    shape = _brush_shape(shape)
    small_ring = shape == BrushShape.RING and max(radius_x, radius_z) <= 1.0
    for r in range(row - math.ceil(radius_z), row + math.ceil(radius_z) + 1):
        for c in range(col - math.ceil(radius_x), col + math.ceil(radius_x) + 1):
            if not (1 <= c < doc.mw - 1 and 1 <= r < doc.mh - 1):
                continue
            offset_x, offset_z = abs(c - col), abs(r - row)
            dx = offset_x / max(radius_x, 1e-9)
            dz = offset_z / max(radius_z, 1e-9)

            if small_ring:
                if offset_x or offset_z:
                    continue
                useful_distance = 0.0
            elif shape == BrushShape.ROUND:
                if offset_x == 0 and offset_z == 0:
                    useful_distance = 0.0
                else:
                    # Il test è conservativo: la cella intera deve ricadere nell'ellisse.
                    useful_distance = math.hypot(
                        (offset_x + 0.5) / max(radius_x, 1e-9),
                        (offset_z + 0.5) / max(radius_z, 1e-9),
                    )
                    if useful_distance > 1.0:
                        continue
            elif shape == BrushShape.DIAMOND:
                useful_distance = dx + dz
                if useful_distance >= 1.0:
                    continue
            elif shape == BrushShape.CROSS:
                useful_distance = max(dx, dz)
                if (useful_distance >= 1.0 or
                        (dx > _CROSS_ARM_FRACTION and dz > _CROSS_ARM_FRACTION)):
                    continue
            elif shape == BrushShape.RING:
                radial_distance = math.hypot(dx, dz)
                if radial_distance < _RING_INNER_RADIUS or radial_distance >= 1.0:
                    continue
                useful_distance = ((radial_distance - _RING_INNER_RADIUS) /
                                   (1.0 - _RING_INNER_RADIUS))
            else:
                useful_distance = max(dx, dz)
                if useful_distance >= 1.0:
                    continue

            weight = 0.5 * (1 + math.cos(math.pi * useful_distance))
            if weight > 1e-9:
                cells.append((c, r, weight))
    return cells


class TerrainBrush:
    """Pennello terreno: passo 1, limiti 0x61-0x9D, bordi ricalcolati."""

    def __init__(self, radius: float = 2.0, strength: float = 0.6):
        self.radius_x = self.radius_z = radius
        self.shape = BrushShape.SQUARE
        self.strength = strength
        self._acc: dict[tuple[int, int], float] = {}
        self._target: int | None = None
        self._mode: BrushMode | None = None
        self.target_height: int | None = None

    @property
    def radius(self):
        return self.radius_x

    @radius.setter
    def radius(self, value):
        self.radius_x = self.radius_z = value

    def footprint(self, doc, col, row):
        return brush_footprint(doc, col, row, self.radius_x, self.radius_z, self.shape)

    def begin_stroke(self, doc: LdfDocument, col: int, row: int) -> None:
        self._acc.clear()
        self._mode = None
        inside = 0 <= col < doc.mw and 0 <= row < doc.mh
        self._target = (self.target_height if self.target_height is not None
                        else doc.grids['hgt'][row][col] if inside else None)

    def apply(self, doc: LdfDocument, col: int, row: int,
              mode: BrushMode, dt: float = 1.0) -> list[tuple[int, int]]:
        hgt = doc.grids['hgt']
        if mode != self._mode:
            self._acc.clear()
            self._mode = mode
        strength = self.strength * dt
        if self._target is None:
            self._target = hgt[row][col]
        touched: list[tuple[int, int]] = []
        snapshot = [line[:] for line in hgt] if mode == BrushMode.SMOOTH else None
        cells = self.footprint(doc, col, row)
        for c, r, weight in cells:
            current = hgt[r][c]
            if mode == BrushMode.RAISE:
                delta = strength
            elif mode == BrushMode.LOWER:
                delta = -strength
            elif mode == BrushMode.FLATTEN:
                delta = (self._target - current) * min(1.0, weight * strength)
            else:
                total = count = 0
                for rr in range(r - 1, r + 2):
                    for cc in range(c - 1, c + 2):
                        if 0 <= cc < doc.mw and 0 <= rr < doc.mh:
                            total += snapshot[rr][cc]
                            count += 1
                delta = (total / count - current) * min(1.0, weight * strength)
            acc = self._acc.get((c, r), 0.0) + delta
            step = int(acc)
            self._acc[(c, r)] = acc - step
            if step:
                value = max(HGT_MIN, min(HGT_MAX, current + step))
                if value in (HGT_MIN, HGT_MAX):
                    self._acc[(c, r)] = 0.0
                if value != current:
                    hgt[r][c] = value
                    touched.append((c, r))
        if touched:
            touched.extend(doc.normalize_border_heights())
        return touched
