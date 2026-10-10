"""Visible-pixel highlights for buildings, without enclosing polygons."""

import numpy as np


def building_outline_pixels(ids, building_grid, *, selected=None, previews=()):
    """Return yellow model edges, grey drag edges and a white selection border."""
    height, width = ids.shape
    rgba = np.zeros((height, width, 4), np.uint8)
    buildings = np.asarray([[int(str(value), 16) for value in row]
                            for row in building_grid], dtype=np.int32)
    rows, cols = buildings.shape
    codes = np.clip(-ids.astype(np.int64) - 1, 0, rows * cols - 1)
    visible = (ids < 0) & (-ids <= rows * cols) & (buildings.reshape(-1)[codes] != 0)
    if not visible.any():
        return rgba

    surrounding = np.pad(ids, 1, constant_values=0)
    inner = visible & ((ids != surrounding[:-2, 1:-1]) |
                       (ids != surrounding[2:, 1:-1]) |
                       (ids != surrounding[1:-1, :-2]) |
                       (ids != surrounding[1:-1, 2:]))
    rgba[inner] = (255, 204, 40, 255)
    previews = set(previews)
    for col, row in previews:
        if 0 <= col < cols and 0 <= row < rows:
            rgba[inner & (ids == -(row * cols + col + 1))] = (160, 160, 160, 255)

    if selected is not None and selected not in previews:
        col, row = selected
        if 0 <= col < cols and 0 <= row < rows:
            mask = visible & (ids == -(row * cols + col + 1))
            padded = np.pad(mask, 1)
            outer = ((padded[:-2, 1:-1] | padded[2:, 1:-1] |
                      padded[1:-1, :-2] | padded[1:-1, 2:]) & ~visible)
            rgba[outer] = (255, 255, 255, 255)
    return rgba
