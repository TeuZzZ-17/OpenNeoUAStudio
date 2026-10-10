"""Faction silhouettes and click targets for the real squad meshes."""
import math
import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPen, QPainter, QImage
from ..render.squad_scene import squad_members
from ..render.special_scene import scene_object_styles


class SquadOverlay:
    def __init__(self, viewport):
        self.viewport = viewport
        self.selected = set()
        self._key = None
        self.members = []
        viewport.squad_overlay = self

    def invalidate(self):
        self._key = None
        self.viewport.update()

    def _members(self, terrain):
        view = self.viewport
        key = (id(view.doc), view._scene_revision, id(view.lib))
        if self._key != key:
            self.members = list(squad_members(view.doc, terrain, view.lib))
            self._key = key
        return self.members

    def circles(self, camera, terrain):
        view = self.viewport
        if view.doc is None or view.lib is None:
            return
        for member in self._members(terrain):
            mesh = view.lib.vehicle_mesh(member.vehicle)
            x, y, z = member.position
            b = mesh.bounds
            points = [camera.world_to_screen((x + xx, y + yy, z + zz))[0]
                      for xx in (b[0], b[3]) for yy in (b[1], b[4]) for zz in (b[2], b[5])]
            left, right = min(p[0] for p in points), max(p[0] for p in points)
            top, bottom = min(p[1] for p in points), max(p[1] for p in points)
            center = ((left + right) / 2, (top + bottom) / 2)
            radius = max(6, math.hypot(right - left, bottom - top) / 2 + 3)
            if -radius < center[0] < view.width() + radius and -radius < center[1] < view.height() + radius:
                yield member, center, radius

    def draw(self, painter, camera=None, terrain=None):
        view = self.viewport
        camera, terrain = camera or view.camera, terrain or view.terrain
        if camera.perspective:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # GPU contours use visible object IDs directly in the screen shader.
        # The software path has the same IDs in its completed frame.
        if getattr(view,'_software_fallback',True) and view._frame is not None:
            ids = view._frame.cell_ids
            pixels = np.zeros((*ids.shape,4),np.uint8)
            def expand(mask):
                padded = np.pad(mask,1)
                return (mask | padded[:-2,1:-1] | padded[2:,1:-1]
                        | padded[1:-1,:-2] | padded[1:-1,2:])
            for index, style in enumerate(scene_object_styles(view)):
                mask = ids == -(view.doc.mw*view.doc.mh+index+1)
                if not mask.any():
                    continue
                selected = style[3] == 2
                edge = mask & expand(~mask)
                if selected:
                    pixels[expand(expand(mask)) & ~mask] = (255,255,255,255)
                    edge = expand(edge) & mask
                color = style[:3]
                pixels[edge] = (*color,255)
            image = QImage(pixels.data,ids.shape[1],ids.shape[0],ids.shape[1]*4,QImage.Format.Format_RGBA8888).copy()
            painter.drawImage(0,0,image)
        for member, center, radius in self.circles(camera, terrain):
            preview = view.doc.squads[member.squad].get('_preview', False)
            selected = member.squad in self.selected and not preview
            if selected and member.member == 0:
                painter.setPen(QColor(255, 250, 210))
                painter.drawText(QRectF(center[0] - 70, center[1] - radius - 23, 140, 20),
                                 Qt.AlignmentFlag.AlignCenter, f"{view.doc.squads[member.squad].get('custom_name') or 'Squad '+str(member.squad+1)} · Selected")
        painter.restore()

    def pick(self, x, y):
        candidates = [(math.hypot(x - p[0], y - p[1]), member.squad)
                      for member, p, radius in self.circles(self.viewport.camera, self.viewport.terrain)
                      if math.hypot(x - p[0], y - p[1]) <= radius]
        return min(candidates)[1] if candidates else None
