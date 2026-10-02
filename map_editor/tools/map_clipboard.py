"""Internal multi-cell clipboard helpers for the map editor."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GridClipboard:
    tool: str
    layers: tuple[str, ...]
    cells: tuple[tuple[int, int, tuple], ...]


def copy_grid_cells(doc, cells, tool: str, layers) -> GridClipboard | None:
    """Copy selected cells as offsets around the centre of their bounding box."""
    valid = sorted({(int(c), int(r)) for c, r in cells
                    if 0 <= int(c) < doc.mw and 0 <= int(r) < doc.mh})
    if not valid:
        return None
    min_c = min(c for c, _ in valid)
    max_c = max(c for c, _ in valid)
    min_r = min(r for _, r in valid)
    max_r = max(r for _, r in valid)
    anchor_c = (min_c + max_c) // 2
    anchor_r = (min_r + max_r) // 2
    layer_names = tuple(layers)
    copied = []
    for c, r in valid:
        values = tuple(doc.grids[layer][r][c] for layer in layer_names)
        copied.append((c - anchor_c, r - anchor_r, values))
    return GridClipboard(tool, layer_names, tuple(copied))


def grid_paste_targets(doc, clipboard: GridClipboard, anchor, interior=False):
    """Translate a copied footprint to an anchor; return None if it cannot fit."""
    if clipboard is None or anchor is None:
        return None
    ac, ar = anchor
    result = []
    for dc, dr, values in clipboard.cells:
        c, r = ac + dc, ar + dr
        if not (0 <= c < doc.mw and 0 <= r < doc.mh):
            return None
        if interior and not (1 <= c < doc.mw - 1 and 1 <= r < doc.mh - 1):
            return None
        result.append((c, r, values))
    return result
