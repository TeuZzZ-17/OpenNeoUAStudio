"""Original strategic-map glyphs projected onto special sectors."""
import math
import time

import numpy as np
from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPolygonF, QTransform, QImage

from ..core.special_objects import SPECIAL_KINDS, special_store, special_slots, special_cell
from ..render.special_scene import special_members, special_cells, actor_code, cell_owner_color
from .game_icons import game_icon, draw_game_badge

# Geometry hides a key glyph only when it is at least this many world units nearer
# to the camera than the glyph plane, so small height differences never hide it.
GLYPH_DEPTH_TOLERANCE = 2.0


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
        view = self.window.view
        doc = view.doc
        camera, terrain = camera or view.camera, terrain or view.terrain
        if doc is None or camera.perspective or terrain.width != doc.mw or terrain.height != doc.mh:
            return
        painter.save()
        now = time.monotonic()
        # Only the GPU frame keeps the depth that hides glyphs behind terrain and buildings.
        renderer = view.renderer if getattr(view, 'renderer_mode', None) == 'gpu' else None
        for index, member in enumerate(special_members(doc, view.lib)):
            value = special_store(doc, member.kind)[member.slot]
            grey = member.preview or actor_code(doc, index) in view.dragged_codes
            color = QColor(160, 160, 160) if grey else QColor(*cell_owner_color(view, doc, member.cell))
            if grey and view.placement_error:
                color = QColor(225, 115, 120)
            x, y, z = terrain.cell_center(*member.cell)
            icon = 'wave' if member.kind == 'item' and value.get('type') == 2 else member.kind
            if member.key >= 0:
                icon += '_key'
                linked = bool(view.selected_special and
                              view.selected_special[:2] == (member.kind, member.slot))
                image = game_icon(icon, tight=False, outlined=linked and not grey)
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
                    painter.setOpacity(.80 + .20 * math.sin(now * 3 + index))
                    self._draw_ground_glyph(painter, camera, renderer, image, transform, y - 8,
                                            self._sector_ids(doc, view.lib, member.cell))
                    painter.restore()
                continue
            bounds = view.lib.mesh(member.typ).bounds
            anchor = QPointF(*camera.world_to_screen((x, y + bounds[1] - 150, z))[0])
            if -80 < anchor.x() < view.width() + 80 and -80 < anchor.y() < view.height() + 80:
                draw_game_badge(painter, anchor, 25 * (1 + .08 * math.sin(now * 3 + index)), icon, color, now + index)
                if member.kind == 'gate':
                    self._draw_key_badges(painter, anchor, doc, value, icon, color, now + index)
        painter.restore()

    @staticmethod
    def _sector_ids(doc, lib, cell):
        """Polygon IDs of the sector's own ground, structure and building geometry."""
        base = cell[1] * doc.mw + cell[0] + 1
        ids = {base, -base}
        owner = special_cells(doc, lib).get(cell)
        if owner is not None:
            ids.add(actor_code(doc, owner[0]))
        return ids

    @staticmethod
    def _draw_ground_glyph(painter, camera, renderer, image, transform, height, own_ids=()):
        """Draw a glyph lying on the ground, leaving out what terrain or buildings of other sectors cover.

        Geometry of the glyph's own sector (``own_ids``) never hides it.
        """
        if renderer is None:
            painter.setWorldTransform(transform, True)
            painter.drawImage(0, 0, image)
            return
        area = transform.mapRect(QRectF(image.rect())).toAlignedRect()
        area = area.intersected(QRect(0, 0, int(camera.width), int(camera.height)))
        if area.isEmpty():
            return
        left, top = area.left(), area.top()
        right, bottom = area.right() + 1, area.bottom() + 1
        painter.beginNativePainting()
        try:
            mask = renderer.glyph_visibility(camera, height, (left, top, right, bottom), GLYPH_DEPTH_TOLERANCE, own_ids)
        finally:
            painter.endNativePainting()
        if not mask.any():
            return
        if mask.all():
            painter.setWorldTransform(transform, True)
            painter.drawImage(0, 0, image)
            return
        # Partly covered: draw the glyph into a layer and clear the hidden pixels.
        layer = QImage(right - left, bottom - top, QImage.Format.Format_ARGB32_Premultiplied)
        layer.fill(Qt.GlobalColor.transparent)
        layer_painter = QPainter(layer)
        layer_painter.setWorldTransform(transform * QTransform.fromTranslate(-left, -top))
        layer_painter.drawImage(0, 0, image)
        layer_painter.end()
        pixels = np.frombuffer(layer.bits(), np.uint8).reshape(
            layer.height(), layer.bytesPerLine())[:, :layer.width() * 4].reshape(layer.height(), layer.width(), 4)
        pixels *= mask[:, :, None]
        painter.drawImage(left, top, layer)

    @staticmethod
    def _draw_key_badges(painter, anchor, doc, value, icon, color, moment):
        """One key icon beside the badge, with the number of key sectors placed for this object."""
        placed = [key for key in value.get('keys', [])
                  if 1 <= key[0] < doc.mw - 1 and 1 <= key[1] < doc.mh - 1]
        if not placed:
            return
        center = QPointF(anchor.x() + 31, anchor.y())
        draw_game_badge(painter, center, 24, icon + '_key', color, moment)
        painter.save()
        font = painter.font()
        font.setBold(True)
        font.setPixelSize(15)
        painter.setFont(font)
        text = str(len(placed))
        width = painter.fontMetrics().horizontalAdvance(text) + 10
        pill = QRectF(center.x() + 15, center.y() - 10, max(width, 20), 20)
        painter.setPen(QColor(color))
        painter.setBrush(QColor(15, 18, 24, 215))
        painter.drawRoundedRect(pill, 10, 10)
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, text)
        painter.restore()
