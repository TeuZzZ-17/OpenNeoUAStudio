"""Building capability icons drawn with the displayed map frame."""
from __future__ import annotations

import math
import time

import numpy as np
from PySide6.QtCore import QObject, QPointF, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QPolygonF, QImage
from PySide6.QtWidgets import QWidget

from ..core.ldf_model import SECTOR_SIZE
from .game_icons import draw_game_badge
from ..render.building_outline import building_outline_pixels


class BuildingOverlay(QObject):
    """Capability drawing shared by the map and palette previews.

    It projects script-building cells (``blg`` grid -> ``new_building``) to
    screen space and draws energy/flak/radar icons. Plain sectors with an identical look (same
    ``typ`` but ``blg == 00``) intentionally show nothing, which is exactly
    how buildings are told apart from ordinary sectors.
    """

    def __init__(self, viewport, state_provider, parent=None):
        super().__init__(parent or viewport)
        self.viewport = viewport
        self.state_provider = state_provider
        self._outline_cache = {}
        self._pulse = QTimer(self, interval=120)
        self._pulse.timeout.connect(viewport.update)
        self._pulse.start()
        viewport.building_overlay = self
        viewport.cameraChanged.connect(viewport.update)

    def update(self, *args):
        self.viewport.update()

    def draw(self, painter, camera=None, terrain=None):
        try:
            state = self.state_provider()
        except Exception:
            return
        doc = state.get("doc")
        lib = state.get("lib")
        buildings = state.get("buildings") or {}
        if doc is None or lib is None or not buildings:
            return
        view = self.viewport
        camera = camera or view.camera
        camera.width, camera.height = view.width(), view.height()
        terrain = terrain or view.terrain
        if terrain.width != doc.mw or terrain.height != doc.mh:
            return
        now = time.monotonic()
        c0, c1, r0, r1 = self._visible_range(doc, camera)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for row in range(r0, r1):
            blg_row = doc.grids['blg'][row]
            for col in range(c0, c1):
                try:
                    bid = int(str(blg_row[col]), 16)
                except (ValueError, TypeError):
                    continue
                definition = buildings.get(bid)
                if definition is None:
                    continue
                try:
                    self._draw_building(painter, doc, lib, terrain, camera,
                                        col, row, definition, now)
                except Exception:
                    continue
        if getattr(view, '_software_fallback', True):
            self._draw_software_outlines(painter, doc)

    def _draw_software_outlines(self, painter, doc):
        """Outline actual visible building pixels, leaving their textures unchanged."""
        frame = getattr(self.viewport, '_frame', None)
        if frame is None:
            return
        ids = frame.cell_ids
        height, width = ids.shape
        if height != self.viewport.height() or width != self.viewport.width():
            return
        pixels = building_outline_pixels(ids, doc.grids['blg'],
                                         selected=self.viewport.selected_building,
                                         previews=self.viewport.preview_cells)
        image = QImage(pixels.data, width, height, width * 4,
                       QImage.Format.Format_RGBA8888).copy()
        painter.drawImage(0, 0, image)

    def _visible_range(self, doc, camera):
        heights = (0.0,)
        projected = [camera.screen_to_ground(x, y, h)
                     for h in heights
                     for x in (0, max(1, self.viewport.width()))
                     for y in (0, max(1, self.viewport.height()))]
        projected = [p for p in projected if p is not None]
        if not projected:
            return 0, doc.mw, 0, doc.mh
        padding = SECTOR_SIZE * 1.5
        c0 = max(0, math.floor((min(p[0] for p in projected) - padding) / SECTOR_SIZE))
        c1 = min(doc.mw, math.ceil((max(p[0] for p in projected) + padding) / SECTOR_SIZE))
        r0 = max(0, math.floor((-max(p[2] for p in projected) - padding) / SECTOR_SIZE))
        r1 = min(doc.mh, math.ceil((-min(p[2] for p in projected) + padding) / SECTOR_SIZE))
        return c0, max(c0 + 1, c1), r0, max(r0 + 1, r1)

    def _draw_building(self, painter, doc, lib, terrain, camera, col, row, definition, now):
        try:
            mesh = lib.mesh(definition.sec_type)
            top = float(mesh.bounds[1]) if mesh.faces else -500.0
        except Exception:
            top = -500.0
        cx, cy, cz = terrain.cell_center(col, row)
        anchor, depth = camera.world_to_screen((cx, cy + top - 160.0, cz))
        if depth < -1e9:
            return
        if not (-80 <= anchor[0] <= self.viewport.width() + 80 and -80 <= anchor[1] <= self.viewport.height() + 80):
            return
        owner = doc.grids["own"][row][col]
        color = (QColor(160, 160, 160) if (col, row) in self.viewport.preview_cells else
                 QColor(*self.viewport.owner_colors.get(owner, (145, 145, 145))))
        self._draw_icons(painter, anchor, definition, color, now, col, row)

    def _draw_icons(self, painter, anchor, definition, color, now, col, row):
        icons: list[str] = []
        for _ in range(definition.energy_levels):
            icons.append("power")
        if definition.has_guns:
            icons.append("flak")
        if definition.is_radar:
            icons.append("radar")
        if not icons:
            return
        phase = (col * 0.7 + row * 1.3)
        pulse = 1.0 + 0.10 * math.sin(now * 3.2 + phase)
        size = 17.0 * pulse
        gap = 5.0
        total = len(icons) * size + (len(icons) - 1) * gap
        x = anchor[0] - total / 2
        y = anchor[1] - size
        for kind in icons:
            center = QPointF(x + size / 2, y + size / 2)
            if draw_game_badge(painter, center, size*1.35, kind, color, now+phase):
                x += size + gap
                continue
            if kind == "power":
                self._energy_icon(painter, center, size, now + phase, color)
            elif kind == "flak":
                self._flak_icon(painter, center, size, now + phase, color)
            else:
                self._radar_icon(painter, center, size, now + phase, color)
            x += size + gap

    @staticmethod
    def _glow(painter, center, radius, color, alpha=90):
        glow = QColor(color)
        glow.setAlpha(alpha)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(glow))
        painter.drawEllipse(center, radius, radius)

    def _energy_icon(self, painter, center, size, moment, color):
        flicker = 0.75 + 0.25 * math.sin(moment * 5.0)
        self._glow(painter, center, size * 0.95, color, int(80 * flicker) + 30)
        s = size / 2
        bolt = QPolygonF([
            QPointF(center.x() + s * 0.25, center.y() - s),
            QPointF(center.x() - s * 0.45, center.y() + s * 0.15),
            QPointF(center.x() - s * 0.02, center.y() + s * 0.15),
            QPointF(center.x() - s * 0.25, center.y() + s),
            QPointF(center.x() + s * 0.45, center.y() - s * 0.15),
            QPointF(center.x() + s * 0.02, center.y() - s * 0.15),
        ])
        painter.setPen(QPen(QColor(90, 50, 0, 230), 1.4, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(QBrush(QColor(255, 214, 60, 245)))
        painter.drawPolygon(bolt)
        painter.setPen(QPen(QColor(255, 255, 230, 220), 1.0))
        painter.drawLine(QPointF(center.x() + s * 0.12, center.y() - s * 0.72),
                         QPointF(center.x() - s * 0.18, center.y() + s * 0.05))

    def _flak_icon(self, painter, center, size, moment, color):
        flicker = 0.7 + 0.3 * math.sin(moment * 4.2 + 1.0)
        self._glow(painter, center, size * 0.95, color, int(75 * flicker) + 30)
        s = size / 2
        painter.setPen(QPen(QColor(255, 235, 235, 245), 1.6))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(center, s * 0.82, s * 0.82)
        painter.setPen(QPen(QColor(255, 80, 80, 245), 1.7, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap))
        painter.drawLine(QPointF(center.x() - s * 0.82, center.y()),
                         QPointF(center.x() + s * 0.82, center.y()))
        painter.drawLine(QPointF(center.x(), center.y() - s * 0.82),
                         QPointF(center.x(), center.y() + s * 0.82))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(20, 8, 8, 250)))
        painter.drawEllipse(center, s * 0.30, s * 0.30)
        painter.setBrush(QBrush(QColor(255, 150, 120, 250)))
        painter.drawEllipse(center, s * 0.13, s * 0.13)

    def _radar_icon(self, painter, center, size, moment, color):
        flicker = 0.7 + 0.3 * math.sin(moment * 3.4 + 2.0)
        self._glow(painter, center, size * 0.95, color, int(75 * flicker) + 30)
        s = size / 2
        painter.setPen(QPen(QColor(220, 245, 255, 245), 1.6, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for radius, alpha in ((0.82, 200), (0.55, 230), (0.30, 250)):
            pen = QPen(QColor(125, 215, 255, alpha), 1.5, Qt.PenStyle.SolidLine,
                       Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            path = QPainterPath()
            path.arcMoveTo(center.x() - s * radius, center.y() - s * radius,
                           s * radius * 2, s * radius * 2, 35.0)
            path.arcTo(center.x() - s * radius, center.y() - s * radius,
                       s * radius * 2, s * radius * 2, 35.0, 110.0)
            painter.drawPath(path)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(225, 248, 255, 250)))
        painter.drawEllipse(center, s * 0.16, s * 0.16)
        angle = (moment * 90.0) % 360.0
        radians = math.radians(angle)
        end = QPointF(center.x() + math.cos(radians) * s * 0.82,
                      center.y() - math.sin(radians) * s * 0.82)
        painter.setPen(QPen(QColor(190, 240, 255, 235), 1.6,
                            Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(center, end)


class CapabilityPreview(QWidget):
    """Reuse the map icons with a neutral background in the palette."""
    def __init__(self, definition, parent=None):
        super().__init__(parent)
        self.definition = definition
        self.setFixedHeight(38)
        count = definition.energy_levels + int(definition.has_guns) + int(definition.is_radar)
        self.setMinimumWidth(max(1, count*22 + 14))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        BuildingOverlay._draw_icons(self, painter, (self.width()/2, 29),
                                    self.definition, QColor(145,145,145), 0, 0, 0)
        painter.end()

    _energy_icon = BuildingOverlay._energy_icon
    _flak_icon = BuildingOverlay._flak_icon
    _radar_icon = BuildingOverlay._radar_icon
    _glow = staticmethod(BuildingOverlay._glow)
