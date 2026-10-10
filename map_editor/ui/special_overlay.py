"""Special-object markers use the same displayed camera as the map."""
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPen

from ..core.special_objects import SPECIAL_KINDS, special_store, special_slots, special_cell

from ..render.special_scene import SPECIAL_COLORS as COLORS


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

    def draw(self, painter, camera=None, terrain=None):
        win, view = self.window, self.window.view
        doc = win.doc
        camera, terrain = camera or view.camera, terrain or view.terrain
        if doc is None or camera.perspective or terrain.width != doc.mw or terrain.height != doc.mh:
            return
        painter.save()
        for kind in SPECIAL_KINDS:
            color = QColor(*COLORS[kind])
            for slot in special_slots(doc, kind):
                value = special_store(doc, kind)[slot]
                cell = special_cell(value)
                if cell is None or not (0 <= cell[0] < doc.mw and 0 <= cell[1] < doc.mh):
                    continue
                selected = win.palette_tabs.currentIndex() == win.special_tab_indices[kind] and win.special_panels[kind].slot == slot
                point = QPointF(*camera.world_to_screen(terrain.cell_center(*cell))[0])
                # Geometry and silhouettes are shared with the actor renderer.
                painter.setBrush(Qt.BrushStyle.NoBrush)
                if selected:
                    for index, key in enumerate(value.get('keys', []), 1):
                        if not (0 <= key[0] < doc.mw and 0 <= key[1] < doc.mh):
                            continue
                        kp = QPointF(*camera.world_to_screen(terrain.cell_center(*key))[0])
                        painter.setPen(QPen(QColor(235, 200, 95, 180), 1.5, Qt.PenStyle.DashLine))
                        painter.drawLine(point, kp)
                        painter.setPen(QPen(QColor(235, 200, 95), 2))
                        painter.drawText(kp + QPointF(12, -4), f'Key {index}')
        painter.restore()
