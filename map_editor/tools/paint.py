from __future__ import annotations

from ..core.ldf_model import LdfDocument


def paint_cells(doc: LdfDocument, cells, layer: str, value) -> bool:
    """Scrive typ/blg/own sulle celle interne; i bordi restano f8-fd."""
    changed = False
    grid = doc.grids[{'type': 'type', 'own': 'own', 'blg': 'blg'}[layer]]
    for c, r in cells:
        if not (0 <= c < doc.mw and 0 <= r < doc.mh):
            continue
        if layer == 'type' and not (1 <= c < doc.mw - 1 and 1 <= r < doc.mh - 1):
            continue
        if grid[r][c] != value:
            grid[r][c] = value
            changed = True
    return changed


def cells_in_radius(doc: LdfDocument, col: int, row: int, size: int):
    half = size // 2
    return [(c, r) for r in range(row - half, row - half + size)
            for c in range(col - half, col - half + size)
            if 0 <= c < doc.mw and 0 <= r < doc.mh]
