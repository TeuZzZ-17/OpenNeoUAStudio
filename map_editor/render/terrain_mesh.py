from __future__ import annotations

import numpy as np

from ..core.ldf_model import DEFAULT_HGT, SECTOR_SIZE

HEIGHT_UNIT = 100.0
HALF = SECTOR_SIZE / 2
PLATEAU_HALF = 450.0


class TerrainMesh:
    """Quote del gioco: piani di 900 unità e raccordi di 300 unità."""

    def __init__(self):
        self.width = 0
        self.height = 0
        self.corners = np.zeros((1, 1))
        self.cells = np.zeros((1, 1))

    def rebuild(self, hgt) -> None:
        self.cells = np.asarray(hgt, dtype=np.float64)
        self.height, self.width = self.cells.shape
        heights = -(self.cells - DEFAULT_HGT) * HEIGHT_UNIT
        padded = np.pad(heights, 1, mode="edge")
        self.corners = (padded[:-1, :-1] + padded[:-1, 1:]
                        + padded[1:, :-1] + padded[1:, 1:]) / 4.0

    def cell_y(self, col: int, row: int) -> float:
        return -(float(self.cells[row, col]) - DEFAULT_HGT) * HEIGHT_UNIT

    def cell_center(self, col: int, row: int):
        # Le posizioni degli oggetti LDF di Sektor2 restano in ldf_model.
        return ((col + 0.5) * SECTOR_SIZE, self.cell_y(col, row),
                -(row + 0.5) * SECTOR_SIZE)

    def filler_heights(self, col: int, row: int, vertical: bool):
        """Ordine POO2 di PrepareFiller: 4 + 4 quote e due medie d'incrocio."""
        if vertical:
            first = self.cell_y(col - 1, row)
            second = self.cell_y(col, row)
            end = self.corners[row + 1, col]
            start = self.corners[row, col]
        else:
            first = self.cell_y(col, row - 1)
            second = self.cell_y(col, row)
            end = self.corners[row, col + 1]
            start = self.corners[row, col]
        return (first,) * 4 + (second,) * 4 + (float(end), float(start))

    def boundary(self, col: int, row: int):
        """Perimetro del settore appoggiato sui raccordi reali."""
        x, y, z = self.cell_center(col, row)
        north = (y + self.cell_y(col, max(0, row - 1))) / 2
        east = (y + self.cell_y(min(self.width - 1, col + 1), row)) / 2
        south = (y + self.cell_y(col, min(self.height - 1, row + 1))) / 2
        west = (y + self.cell_y(max(0, col - 1), row)) / 2
        c = self.corners
        return (
            (x - HALF, float(c[row, col]), z + HALF),
            (x - PLATEAU_HALF, north, z + HALF),
            (x + PLATEAU_HALF, north, z + HALF),
            (x + HALF, float(c[row, col + 1]), z + HALF),
            (x + HALF, east, z + PLATEAU_HALF),
            (x + HALF, east, z - PLATEAU_HALF),
            (x + HALF, float(c[row + 1, col + 1]), z - HALF),
            (x + PLATEAU_HALF, south, z - HALF),
            (x - PLATEAU_HALF, south, z - HALF),
            (x - HALF, float(c[row + 1, col]), z - HALF),
            (x - HALF, west, z - PLATEAU_HALF),
            (x - HALF, west, z + PLATEAU_HALF),
        )
