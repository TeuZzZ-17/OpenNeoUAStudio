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


def brush_footprint(doc: LdfDocument, col: int, row: int, radius: float,
                    radius_z: float | None = None, shape: BrushShape = BrushShape.SQUARE):
    """Celle interne (bordo escluso); il peso serve a Flatten e Smooth."""
    cells = []
    radius_z = radius if radius_z is None else radius_z
    for r in range(row - math.ceil(radius_z), row + math.ceil(radius_z) + 1):
        for c in range(col - math.ceil(radius), col + math.ceil(radius) + 1):
            if not (1 <= c < doc.mw - 1 and 1 <= r < doc.mh - 1):
                continue
            dx = abs(c - col) / max(radius, 1e-9)
            dz = abs(r - row) / max(radius_z, 1e-9)
            dist = math.hypot(dx, dz) if shape == BrushShape.ROUND else max(dx, dz)
            if dist >= 1:
                continue
            weight = 0.5 * (1 + math.cos(math.pi * dist))
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
        self._target = doc.grids['hgt'][row][col] if inside else None

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
        for c, r, weight in self.footprint(doc, col, row):
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
