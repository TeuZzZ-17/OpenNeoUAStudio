"""Original strategic-map glyphs projected onto special sectors."""
import math
import time

import numpy as np
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPen, QPolygonF, QTransform, QImage

from ..core.special_objects import SPECIAL_KINDS, special_store, special_slots, special_cell
from ..render.special_scene import SPECIAL_COLORS as COLORS, special_members, actor_code
from .game_icons import game_icon, draw_game_badge


class SpecialOverlay:
    def __init__(self, window):
        self.window = window
        window.view.special_overlay = self

    def pick(self, cell):
        if cell is None:
            return None
        win = self.window
        kinds = sorted(SPECIAL_KINDS, key=lambda kind: win.special_tab_indices[kind] != win.palette_tabs.currentIndex())
        for kind in kinds:
            for slot in special_slots(win.doc, kind):
                value = special_store(win.doc, kind)[slot]
                if special_cell(value) == cell:
                    return kind, slot, -1
                if cell in value.get('keys', []):
                    return kind, slot, value['keys'].index(cell)
        return None

    @staticmethod
    def _grey(image):
        image = image.convertToFormat(QImage.Format.Format_RGBA8888)
        pixels = np.frombuffer(image.bits(), np.uint8).reshape(
            image.height(), image.bytesPerLine())[:, :image.width() * 4].reshape(
            image.height(), image.width(), 4)
        grey = np.rint(pixels[:, :, :3] @ np.array((.299, .587, .114))).astype(np.uint8)
        pixels[:, :, :3] = grey[:, :, None]
        return image

    def draw(self, painter, camera=None, terrain=None):
        win, view = self.window, self.window.view
        doc = view.doc
        camera, terrain = camera or view.camera, terrain or view.terrain
        if doc is None or camera.perspective or terrain.width != doc.mw or terrain.height != doc.mh:
            return
        painter.save()
        now = time.monotonic()
        for index, member in enumerate(special_members(doc, view.lib)):
            value = special_store(doc, member.kind)[member.slot]
            grey = member.preview or actor_code(doc, index) in view.dragged_codes
            color = QColor(160, 160, 160) if grey else QColor(*COLORS[member.kind])
            selected = view.selected_special == (member.kind, member.slot, member.key)
            x, y, z = terrain.cell_center(*member.cell)
            icon = 'wave' if member.kind == 'item' and value.get('type') == 2 else member.kind
            if member.key >= 0:
                icon += '_key'
                image = game_icon(icon, tight=False)
                if image.isNull():
                    continue
                if grey:
                    image = self._grey(image)
                points = QPolygonF([QPointF(*camera.world_to_screen((x + dx, y - 8, z + dz))[0])
                                    for dx, dz in ((-450, 450), (450, 450), (450, -450), (-450, -450))])
                source = QPolygonF([QPointF(0, 0), QPointF(image.width(), 0),
                                    QPointF(image.width(), image.height()), QPointF(0, image.height())])
                transform = QTransform()
                if QTransform.quadToQuad(source, points, transform):
                    painter.save()
                    painter.setWorldTransform(transform, True)
                    painter.setOpacity(.80 + .20 * math.sin(now * 3 + index))
                    painter.drawImage(0, 0, image)
                    painter.restore()
                if selected:
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.setPen(QPen(QColor('white'), 2.5))
                    painter.drawPolygon(points)
                parent = special_cell(value)
                if view.selected_special and view.selected_special[:2] == (member.kind, member.slot) and parent:
                    point = QPointF(*camera.world_to_screen(terrain.cell_center(*parent))[0])
                    target = QPointF(*camera.world_to_screen((x, y - 8, z))[0])
                    painter.setPen(QPen(color, 1.2, Qt.PenStyle.DashLine))
                    painter.drawLine(point, target)
                continue
            bounds = view.lib.mesh(member.typ).bounds
            anchor = QPointF(*camera.world_to_screen((x, y + bounds[1] - 150, z))[0])
            if -80 < anchor.x() < view.width() + 80 and -80 < anchor.y() < view.height() + 80:
                draw_game_badge(painter, anchor, 25 * (1 + .08 * math.sin(now * 3 + index)), icon, color, now + index)
        painter.restore()
