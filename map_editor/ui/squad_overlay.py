"""Faction silhouettes and click targets for the real squad meshes."""
import math
import time
import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QImage
from ..core.actor_comments import host_comment, squad_comment
from ..render.squad_scene import squad_members, host_members
from ..render.special_scene import scene_object_styles
from .game_icons import draw_game_badge, MAP_BADGE_SIZE


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

    def _badge_anchor(self, member, camera, terrain):
        squad = self.viewport.doc.squads[member.squad]
        x, y, z = terrain.cell_center(squad['x'], squad['y'])
        screen_x, screen_y = camera.world_to_screen((x, y - 180, z))[0]
        # Elevation alone gives no separation in a top-down view.
        return QPointF(screen_x, screen_y - 22)

    def host_badges(self, camera, terrain):
        view = self.viewport
        placed = [value for value in view.doc.host_stations
                  if view.doc.cell_is_valid(value) and not value.get('_preview')]
        ordered = view.doc.host_stations_for_save(placed)
        player = ordered[0] if ordered else None
        for member in host_members(view.doc, terrain, view.lib):
            index = member.squad - len(view.doc.squads)
            value = view.doc.host_stations[index]
            x,y,z = member.position
            bounds = view.lib.vehicle_mesh(member.vehicle).bounds
            screen_x = camera.world_to_screen((x, y, z))[0][0]
            top = min(camera.world_to_screen((x + xx, y + yy, z + zz))[0][1]
                      for xx in (bounds[0], bounds[3])
                      for yy in (bounds[1], bounds[4])
                      for zz in (bounds[2], bounds[5]))
            anchor = QPointF(screen_x, top - 18)
            code = -(view.doc.mw * view.doc.mh + member.squad + 1)
            grey = value.get('_preview') or code in view.dragged_codes
            color = QColor(160,160,160) if grey else QColor(*view.owner_colors.get(member.owner,(150,150,150)))
            if -60 < anchor.x() < view.width()+60 and -60 < anchor.y() < view.height()+60:
                yield index,anchor,member.owner,color,value is player and not grey

    def _draw_comment(self, painter, center_x, center_y, text):
        """Comment of the selected actor, as wide as its text (shortened only past the view width)."""
        metrics = painter.fontMetrics()
        text = metrics.elidedText(text, Qt.TextElideMode.ElideRight, max(60, self.viewport.width() - 40))
        width = metrics.horizontalAdvance(text) + 16
        left = min(max(6, center_x - width / 2), max(6, self.viewport.width() - width - 6))
        rect = QRectF(left, center_y - 10, width, 20)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(20, 16, 16, 190))
        painter.drawRoundedRect(rect, 4, 4)
        painter.setPen(QColor(255, 250, 210))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

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
                if selected or style[3] == 4:
                    pixels[expand(expand(mask)) & ~mask] = (*((255,255,255) if selected else style[:3]),255)
                    edge = expand(edge) & mask
                color = (255,255,255) if selected else style[:3]
                pixels[edge] = (*color,255)
            image = QImage(pixels.data,ids.shape[1],ids.shape[0],ids.shape[1]*4,QImage.Format.Format_RGBA8888).copy()
            painter.drawImage(0,0,image)
        now = time.monotonic()
        for member, center, radius in self.circles(camera, terrain):
            preview = view.doc.squads[member.squad].get('_preview', False)
            selected = member.squad in self.selected and not preview
            if member.member == 0:
                anchor = self._badge_anchor(member, camera, terrain)
                color = QColor(160,160,160) if preview else QColor(*view.owner_colors.get(member.owner, (150,150,150)))
                draw_game_badge(painter, anchor, MAP_BADGE_SIZE, 'squad', color, now + member.squad)
            if selected and member.member == 0:
                self._draw_comment(painter, center[0], center[1] - radius - 13,
                                   squad_comment(view.doc, member.squad, view.lib.vehicles))
        for index,anchor,owner,color,player in self.host_badges(camera,terrain):
            vehicle = view.doc.host_stations[index].get('veh', 0)
            definition = view.lib.vehicles.get(vehicle)
            icon = definition.type_icon if definition else None
            draw_game_badge(painter, anchor, MAP_BADGE_SIZE, 'host', color,
                            now + index, owner=owner, glyph_key=icon)
            if index == getattr(view, 'selected_host', -1):
                self._draw_comment(painter, anchor.x(), anchor.y() - MAP_BADGE_SIZE * .75 - 10,
                                   host_comment(view.doc, index, view.lib.vehicles))
            if player:
                draw_game_badge(painter,anchor+QPointF(30,0),MAP_BADGE_SIZE,'player',QColor(255,220,100),now+index)
        painter.restore()

    def pick_host_badge(self, x, y):
        for index,anchor,_owner,_color,player in self.host_badges(self.viewport.camera,self.viewport.terrain):
            # The player badge is drawn on top of the host badge, so it wins the overlap.
            if player and math.hypot(x-anchor.x()-30,y-anchor.y())<=19:
                return index
            if math.hypot(x-anchor.x(),y-anchor.y())<=19:
                return index
        return None

    def pick(self, x, y):
        for member in self._members(self.viewport.terrain):
            if member.member == 0:
                anchor = self._badge_anchor(member, self.viewport.camera, self.viewport.terrain)
                if math.hypot(x - anchor.x(), y - anchor.y()) <= 19:
                    return member.squad
        candidates = [(math.hypot(x - p[0], y - p[1]), member.squad)
                      for member, p, radius in self.circles(self.viewport.camera, self.viewport.terrain)
                      if math.hypot(x - p[0], y - p[1]) <= radius]
        return min(candidates)[1] if candidates else None
