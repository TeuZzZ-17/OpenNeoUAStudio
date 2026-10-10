from __future__ import annotations

import copy
import math
import time

import numpy as np
from PySide6.QtCore import QObject, QPointF, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import (QColor, QImage, QPainter, QPainterPath,
                           QPainterPathStroker, QPen, QPixmap, QPolygonF)
from PySide6.QtWidgets import QWidget, QApplication

from ..core.factions import load_owner_colors
from ..core.ldf_model import LdfDocument, SECTOR_SIZE
from .camera import ISO_PITCH, ISO_YAW, IsoCamera
from .sector_mesh import SectorMeshLibrary
from .map_scene import render_scene, scene_polygons
from .terrain_mesh import TerrainMesh


class _RenderSignals(QObject):
    finished = Signal(object)


class _RenderJob(QRunnable):
    def __init__(self, polygons, camera, tables, key, generation, terrain, preview_cells=(), preview_codes=()):
        super().__init__()
        self.signals = _RenderSignals()
        self.polygons, self.camera, self.tables = polygons, camera, tables
        self.key, self.generation, self.terrain = key, generation, terrain
        self.preview_codes = preview_codes
        self.preview_cells = preview_cells

    def run(self):
        try:
            frame = render_scene(self.polygons, self.camera, self.tables, fast=self.key[-1],
                                 preview_codes=self.preview_codes)
            if self.preview_cells:
                ids = [r*self.terrain.width+c+1 for c,r in self.preview_cells]
                mask = np.isin(np.abs(frame.cell_ids),ids)
                grey = frame.rgba[mask,:3] @ np.array((.299,.587,.114))
                frame.rgba[mask,:3] = np.round(grey[:,None]*.72+255*.62*.28).astype(np.uint8)
            error = None
        except Exception as exc:
            frame, error = None, str(exc)
        self.signals.finished.emit((frame, self.key, self.generation, self.terrain, error))


class MapViewport(QWidget):
    cellHovered = Signal(int, int)
    cellPressed = Signal(int, int, object, object)
    cellDragged = Signal(int, int, object, object)
    cellReleased = Signal(int, int, object)
    cellDoubleClicked = Signal(int, int, object)
    squadPressed = Signal(int, object)
    hostPressed = Signal(int)
    actorDragStarted = Signal(str, object)
    actorDragged = Signal(object)
    actorDragFinished = Signal()
    cellSelected = Signal(object, object)
    cellSwept = Signal(object)
    selectionCleared = Signal()
    contextRequested = Signal(object, object)
    placementConfirmed = Signal(object)
    operationCancelled = Signal()
    cameraChanged = Signal()
    statusMessage = Signal(str)
    backendChanged = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(480, 360)
        self.setToolTip('Left drag: paint / move squad · Ctrl+click: add/remove · Right: menu · Right drag: rotate · Middle: pan')
        self.active_tool = 'sector'
        self.cursor_color = (75, 195, 220)
        self.sample_active = False
        self.sample_flash_until = 0.0
        self.draft_active = False
        self.dragged_codes = set()
        self.selected_special = None
        self._press_pos = None
        self._drag_actor = False
        self._moved = False
        self._paint_gesture = False
        self._paint_started = False
        self._paint_last = None
        self._sweep_gesture = False
        self._sweep_last = None
        self._paint_last = None
        self._closed = False
        self.preview_cells = set()
        self.doc: LdfDocument | None = None
        self.lib: SectorMeshLibrary | None = None
        self.terrain = TerrainMesh()
        self.camera = IsoCamera(yaw=ISO_YAW, pitch=ISO_PITCH, zoom=0.1)
        self.owner_colors = load_owner_colors()
        self.show_grid = True
        self.show_sky = True
        self._sky_image = QImage()
        self._sky_scaled = QPixmap()
        self.hover: tuple[int, int] | None = None
        self.brush_cells: set[tuple[int, int]] = set()
        self.selection: set[tuple[int, int]] = set()
        self._drag_button = None
        self._last = QPointF()
        self._interacting = False
        self._editing = False
        self._scene_revision = self._generation = 0
        self._frame = self._frame_key = self._image = None
        self._owner_marks = None
        self._annotation_base = None
        self._visible_codes = set()
        self._display_terrain = None
        self._job = None
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._render_timer = QTimer(self, singleShot=True, interval=16)
        self._render_timer.timeout.connect(self._launch_render)
        self._settle = QTimer(self, singleShot=True, interval=140)
        self._settle.timeout.connect(self._end_interaction)
        self._nav_keys = set()
        self._nav_last = time.monotonic()
        self._nav_timer = QTimer(self, interval=16)
        self._nav_timer.timeout.connect(self._tick_navigation)

    def set_library(self, lib: SectorMeshLibrary | None) -> None:
        self.lib = lib
        self._generation += 1
        self._frame = self._image = self._frame_key = None
        self._owner_marks = None
        self._annotation_base = None
        self.scene_changed()

    def set_document(self, doc: LdfDocument) -> None:
        self.doc = doc
        self._generation += 1
        self._frame = self._image = self._frame_key = None
        self._owner_marks = None
        self._annotation_base = None
        self.hover = None
        self.selection.clear()
        self.brush_cells.clear()
        self.terrain.rebuild(doc.grids['hgt'])
        self.reset_camera()
        self.scene_changed()

    def scene_changed(self, cells=None):
        self._scene_revision += 1
        self.update()

    def owners_changed(self):
        self._owner_marks = None
        self._annotation_base = None
        self.update()

    def terrain_changed(self, cells=None) -> None:
        if self.doc is not None:
            self.terrain.rebuild(self.doc.grids['hgt'])
        self.scene_changed()

    def set_grid(self, visible):
        self.show_grid = visible
        self._annotation_base = None
        self.update()

    def set_editing(self, editing: bool):
        self._editing = editing
        self.update()

    def set_sky_image(self, image: QImage | None):
        self._sky_image = image if image is not None else QImage()
        self._sky_scaled = QPixmap()
        self.update()

    def set_sky_visible(self, visible: bool):
        self.show_sky = visible
        self.update()

    def set_camera_angles(self, yaw, pitch):
        self.camera.perspective = False
        self.camera.yaw, self.camera.pitch = yaw, pitch
        self._settle.stop()
        self._interacting = False
        self.cameraChanged.emit()
        self.update()

    def recenter(self) -> None:
        if self.doc is None:
            return
        self.camera.center = (self.doc.mw * SECTOR_SIZE / 2, 0.0,
                              -self.doc.mh * SECTOR_SIZE / 2)
        self.camera.pan = (0.0, 0.0)
        extent = max(self.doc.mw, self.doc.mh) * SECTOR_SIZE * 0.75
        self.camera.zoom = max(0.012, min(0.6, min(self.width(), self.height()) / extent))
        self.cameraChanged.emit()
        self.update()

    def reset_camera(self):
        self.unsetCursor()
        self.camera.perspective = False
        self.camera.yaw, self.camera.pitch = ISO_YAW, ISO_PITCH
        self._nav_keys.clear()
        self._nav_timer.stop()
        self.recenter()

    def enter_pov(self, member):
        bounds = self.lib.vehicle_mesh(member.vehicle).bounds
        x,y,z = member.position
        self.enter_pov_at((x,y+bounds[1]-30,z),(0,0,1))

    def enter_pov_at(self, eye, direction):
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.camera.center = tuple(eye)
        self.camera.perspective = True
        # La camera guarda verso -Z a yaw 0; il fronte del modello è il suo asse +Z.
        x,y,z = direction
        self.camera.yaw = math.degrees(math.atan2(x,-z))
        self.camera.pitch = math.degrees(math.atan2(y,math.hypot(x,z)))
        self.camera.pan = (0,0)
        self.camera.fov = 70.0
        self.hover = None
        self.cameraChanged.emit()
        self.statusMessage.emit('POV · Right click: exit menu · Right drag: look · WASD: move · R/F: up/down · Esc: exit')
        self.update()

    def _camera_key(self):
        cam = self.camera
        return (cam.yaw, cam.pitch, cam.zoom, cam.center, cam.pan, self.width(), self.height(), cam.perspective, cam.fov)

    def _render_key(self):
        return (self._scene_revision, self._camera_key(), self._interacting or self._editing)

    def _launch_render(self):
        if self._closed or self._job is not None or self.lib is None or self.doc is None:
            return
        key = self._render_key()
        if key == self._frame_key:
            return
        cam = self.camera.copy()
        cam.width, cam.height = self.width(), self.height()
        terrain = copy.deepcopy(self.terrain)
        try:
            # Il caricatore Qt resta nel thread della finestra; il raster lavora a parte.
            polygons = scene_polygons(self.lib, self.doc, terrain, cam)
        except Exception as exc:
            self._frame_key = key
            self.statusMessage.emit(f"Unable to update the view: {exc}")
            return
        from .special_scene import scene_object_styles
        previews = tuple(-(self.doc.mw * self.doc.mh + i + 1) for i, style in
                         enumerate(scene_object_styles(self)) if style[3] == 3)
        self._job = _RenderJob(polygons, cam, self.lib.tables, key, self._generation, terrain,
                              tuple(self.preview_cells), previews)
        self._job.signals.finished.connect(self._render_finished)
        self._pool.start(self._job)
        self.update()

    def _render_finished(self, result):
        frame, key, generation, terrain, error = result
        self._job = None
        if generation == self._generation and key[0] == self._scene_revision:
            self._frame_key = key
            if error:
                self.statusMessage.emit(f"Unable to update the view: {error}")
            elif frame is not None:
                self._frame, self._display_terrain = frame, terrain
                self._image = None
                self._owner_marks = None
                self._annotation_base = None
        self.update()

    def paintEvent(self, event):
        self.camera.width, self.camera.height = self.width(), self.height()
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(18, 22, 28))
        if self.show_sky and not self._sky_image.isNull():
            if self._sky_scaled.size() != self.size():
                self._sky_scaled = QPixmap.fromImage(self._sky_image).scaled(
                    self.size(), Qt.AspectRatioMode.IgnoreAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)
            painter.drawPixmap(0, 0, self._sky_scaled)
        if self.doc is None:
            return
        if self.lib is None:
            painter.setPen(QColor(160, 170, 180))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Set unavailable")
            return
        if self._render_key() != self._frame_key and self._job is None and not self._render_timer.isActive():
            self._render_timer.start()
        if self._frame is not None:
            if self._image is None:
                self._image = self._frame.image()
            painter.drawImage(0, 0, self._image)
            cam = self.camera.copy()
            cam.yaw, cam.pitch, cam.zoom, cam.center, cam.pan, cam.width, cam.height = self._frame_key[1][:7]
            cam.perspective, cam.fov = self._frame_key[1][7:]
            overlay = getattr(self, 'building_overlay', None)
            if overlay is not None and not self.camera.perspective:
                overlay.draw(painter, cam, self._display_terrain)
            if self._frame_key[1] == self._camera_key() and not self.camera.perspective:
                painter.drawImage(0, 0, self._annotations())
                overlay = getattr(self, 'squad_overlay', None)
                if overlay is not None:
                    overlay.draw(painter, cam, self._display_terrain)
        overlay = getattr(self, 'special_overlay', None)
        if overlay is not None and self._frame is not None:
            overlay.draw(painter, cam, self._display_terrain)
        self.draw_interaction_overlay(painter)
        if self._job is not None or self._render_timer.isActive():
            painter.setPen(QColor(160, 170, 180))
            painter.drawText(12, self.height() - 12, "Updating view…")

    def _annotations(self):
        if self._annotation_base is None:
            self._annotation_base = self._make_annotation_base()
        dynamic_cells = self.brush_cells | self.selection
        if self.hover is not None:
            dynamic_cells = dynamic_cells | {self.hover}
        if not dynamic_cells:
            return self._annotation_base
        image = QImage(self.width(), self.height(), QImage.Format.Format_RGBA8888)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        terrain = self._display_terrain
        for col, row in dynamic_cells:
            if row * self.doc.mw + col + 1 not in self._visible_codes:
                continue
            points = QPolygonF([QPointF(*self.camera.world_to_screen(v)[0])
                                for v in terrain.boundary(col, row)])
            if not points.boundingRect().intersects(self.rect().toRectF()):
                continue
            if (col, row) in self.brush_cells and self.active_tool != 'terrain':
                painter.setPen(QPen(QColor(245, 245, 245, 190), 1.2, Qt.PenStyle.DashLine))
                for side, neighbour in enumerate(((col, row - 1), (col + 1, row),
                                                  (col, row + 1), (col - 1, row))):
                    if neighbour not in self.brush_cells:
                        for edge in range(side * 3, side * 3 + 3):
                            painter.drawLine(points[edge], points[(edge + 1) % 12])
            if (col, row) in self.selection or (col, row) == self.hover:
                self._draw_brackets(painter, points, (col, row) in self.selection, self.current_cursor_color())
        painter.end()
        pixels = np.frombuffer(image.bits(), np.uint8).reshape(self.height(), self.width(), 4)
        pixels[:, :, 3] *= (self._frame.cell_ids > 0).astype(np.uint8)
        _, owner_pixels = self._owner_marks
        pixels[:, :, 3][owner_pixels] = 0
        result = self._annotation_base.copy()
        painter = QPainter(result)
        painter.drawImage(0, 0, image)
        painter.end()
        result_pixels = np.frombuffer(result.bits(), np.uint8).reshape(self.height(), self.width(), 4)
        base_pixels = np.frombuffer(self._annotation_base.bits(), np.uint8).reshape(
            self.height(), self.width(), 4)
        result_pixels[owner_pixels] = base_pixels[owner_pixels]
        return result

    def _make_annotation_base(self):
        image = QImage(self.width(), self.height(), QImage.Format.Format_RGBA8888)
        image.fill(Qt.GlobalColor.transparent)
        visible = np.unique(np.abs(self._frame.cell_ids))
        visible = visible[visible <= self.doc.mw*self.doc.mh]
        self._visible_codes = {int(code) for code in visible if code > 0}
        if self.show_grid:
            painter = QPainter(image)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(0, 0, 0, 85), 0.8))
            for code in self._visible_codes:
                col, row = (code - 1) % self.doc.mw, (code - 1) // self.doc.mw
                points = QPolygonF([QPointF(*self.camera.world_to_screen(v)[0])
                                    for v in self._display_terrain.boundary(col, row)])
                if points.boundingRect().intersects(self.rect().toRectF()):
                    painter.drawPolygon(points)
            painter.end()
            pixels = np.frombuffer(image.bits(), np.uint8).reshape(self.height(), self.width(), 4)
            pixels[:, :, 3] *= (self._frame.cell_ids > 0).astype(np.uint8)
        if self._owner_marks is None:
            self._owner_marks = self._make_owner_marks(visible)
        painter = QPainter(image)
        painter.drawImage(0, 0, self._owner_marks[0])
        painter.end()
        return image

    def _make_owner_marks(self, visible):
        image = QImage(self.width(), self.height(), QImage.Format.Format_RGBA8888)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        for code in visible[visible > 0]:
            col, row = (int(code) - 1) % self.doc.mw, (int(code) - 1) // self.doc.mw
            owner = self.doc.grids['own'][row][col]
            if not owner:
                continue
            points = QPolygonF([QPointF(*self.camera.world_to_screen(v)[0])
                                for v in self._display_terrain.boundary(col, row)])
            rect = points.boundingRect().adjusted(-1, -1, 1, 1).toAlignedRect().intersected(self.rect())
            if rect.isEmpty():
                continue
            mark = QImage(rect.size(), QImage.Format.Format_RGBA8888)
            mark.fill(Qt.GlobalColor.transparent)
            path = QPainterPath()
            path.addPolygon(points)
            path.closeSubpath()
            stroke = QPainterPathStroker()
            stroke.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            mark_painter = QPainter(mark)
            mark_painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            mark_painter.translate(-rect.left(), -rect.top())
            mark_painter.setPen(Qt.PenStyle.NoPen)
            # Ogni colore resta sul proprio lato del confine, anche tra due owner diversi.
            stroke.setWidth(6.0)
            mark_painter.setBrush(QColor(185, 195, 205, 120))
            mark_painter.drawPath(stroke.createStroke(path).intersected(path))
            stroke.setWidth(4.0)
            mark_painter.setBrush(QColor(*self.owner_colors.get(owner, self.owner_colors[0])))
            mark_painter.drawPath(stroke.createStroke(path).intersected(path))
            mark_painter.end()
            pixels = np.frombuffer(mark.bits(), np.uint8).reshape(rect.height(), rect.width(), 4)
            cells = self._frame.cell_ids[rect.top():rect.top() + rect.height(),
                                         rect.left():rect.left() + rect.width()]
            pixels[:, :, 3] *= (cells == code).astype(np.uint8)
            painter.drawImage(rect.topLeft(), mark)
        painter.end()
        pixels = np.frombuffer(image.bits(), np.uint8).reshape(self.height(), self.width(), 4)
        return image, pixels[:, :, 3].copy() > 0

    @staticmethod
    def _draw_brackets(painter, points, selected, color=(255, 255, 255)):
        center = QPointF(sum(p.x() for p in points) / len(points),
                         sum(p.y() for p in points) / len(points))
        for index in (0, 3, 6, 9):
            direction = points[index] - center
            distance = math.hypot(direction.x(), direction.y())
            offset = direction * (8.0 / max(distance, 1e-6))
            corner = points[index] + offset
            for target in (points[(index - 1) % 12] + offset, points[(index + 1) % 12] + offset):
                delta = target - corner
                length = math.hypot(delta.x(), delta.y())
                if length < 1e-6:
                    continue
                end = corner + delta * min(0.8, 14.0 / length)
                painter.setPen(QPen(QColor(0, 0, 0, 210), 3.5))
                painter.drawLine(corner, end)
                painter.setPen(QPen(QColor(*color, 255 if selected else 200),
                                    1.7 if selected else 1.1))
                painter.drawLine(corner, end)

    def _draw_cell_footprint(self, painter, cells, color):
        """Disegna solo il bordo esterno di un footprint a celle, senza linee interne."""
        cells = set(cells)
        if not cells or self.doc is None:
            return
        terrain = self.terrain
        sides = (((0, -1), 0), ((1, 0), 3), ((0, 1), 6), ((-1, 0), 9))
        for col, row in cells:
            if not (0 <= col < self.doc.mw and 0 <= row < self.doc.mh):
                continue
            points = QPolygonF([QPointF(*self.camera.world_to_screen(v)[0])
                                for v in terrain.boundary(col, row)])
            for (dc, dr), start in sides:
                if (col + dc, row + dr) in cells:
                    continue
                for edge in range(start, start + 3):
                    a, b = points[edge], points[(edge + 1) % 12]
                    painter.setPen(QPen(QColor(0, 0, 0, 185), 5.0))
                    painter.drawLine(a, b)
                    painter.setPen(QPen(color, 2.2))
                    painter.drawLine(a, b)

    def pick_cell(self, x: float, y: float):
        if self._frame is None or self._frame_key[1] != self._camera_key():
            return None
        cell = self._frame.pick(x, y, self.doc.mw)
        return cell if cell is None or cell[1] < self.doc.mh else None

    def pick_squad(self, x, y):
        if (self._frame is None or self._frame_key[1] != self._camera_key()
                or self._frame_key[0] != self._scene_revision):
            return None
        ix, iy = int(x), int(y)
        if 0 <= ix < self.width() and 0 <= iy < self.height():
            code = abs(int(self._frame.cell_ids[iy, ix])) - self.doc.mw * self.doc.mh - 1
            if 0 <= code < len(self.doc.squads):
                return code
        overlay = getattr(self, 'squad_overlay', None)
        return overlay.pick(x, y) if overlay is not None else None

    def _pick_code(self, x, y):
        if (self._frame is None or self._frame_key[1] != self._camera_key()
                or self._frame_key[0] != self._scene_revision):
            return 0
        ix, iy = int(x), int(y)
        if 0 <= ix < self.width() and 0 <= iy < self.height():
            return int(self._frame.cell_ids[iy, ix])
        return 0

    def pick_scene_object(self, x, y):
        if self.doc is None:
            return None
        from .special_scene import special_members
        code = self._pick_code(x, y)
        index = -code - self.doc.mw * self.doc.mh - 1
        if 0 <= index < len(self.doc.squads):
            return 'squad', index
        index -= len(self.doc.squads)
        if 0 <= index < len(self.doc.host_stations):
            return 'host', index
        index -= len(self.doc.host_stations)
        members = list(special_members(self.doc, self.lib))
        if 0 <= index < len(members):
            value = members[index]
            return 'special', (value.kind, value.slot, value.key)
        if 0 < abs(code) <= self.doc.mw * self.doc.mh:
            cell_id = abs(code) - 1
            cell = cell_id % self.doc.mw, cell_id // self.doc.mw
            matches = [member for member in members if member.cell == cell]
            if matches:
                value = next((member for member in matches if member.key < 0), matches[0])
                return 'special', (value.kind, value.slot, value.key)
        return None

    def pick_host(self, x, y):
        if (self._frame is None or self._frame_key[1] != self._camera_key()
                or self._frame_key[0] != self._scene_revision):
            return None
        ix, iy = int(x), int(y)
        if 0 <= ix < self.width() and 0 <= iy < self.height():
            code = abs(int(self._frame.cell_ids[iy, ix])) - self.doc.mw * self.doc.mh - len(self.doc.squads) - 1
            if 0 <= code < len(self.doc.host_stations):
                return code
        return None

    def _begin_interaction(self):
        self._interacting = True
        self._settle.start()

    def _end_interaction(self):
        self._interacting = False
        self.update()

    def mousePressEvent(self, event):
        self.setFocus()
        self._drag_button = event.button()
        self._last = event.position()
        self._press_pos = event.position()
        self._moved = False
        self._drag_actor = False
        self._paint_gesture = False
        self._paint_started = False
        self._paint_last = None
        self._sweep_gesture = False
        self._sweep_last = None
        if self.camera.perspective:
            return
        if self.draft_active and event.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
            self.placementConfirmed.emit(self.ground_cell(event.position().x(), event.position().y()))
            self._drag_button = None
            return
        if event.button() == Qt.MouseButton.LeftButton:
            cell = self.ground_cell(event.position().x(), event.position().y())
            overlay = getattr(self, 'special_overlay', None)
            hit = self.pick_scene_object(event.position().x(), event.position().y())
            if hit is not None and hit[0] == 'special' and overlay is not None and overlay.window._special_placement is None:
                self._drag_actor = True
                self.actorDragStarted.emit('special', {'cell': cell, 'hit': hit[1]})
                return
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier and self.active_tool != 'terrain':
                self._sweep_gesture = True
                self._sweep_over(self.ground_cell(event.position().x(), event.position().y()))
                return
            host = self.pick_host(event.position().x(), event.position().y())
            if host is not None:
                self.hostPressed.emit(host)
                self._drag_actor = True
                self.actorDragStarted.emit('host', self.ground_cell(event.position().x(), event.position().y()))
                return
            squad = self.pick_squad(event.position().x(), event.position().y())
            if squad is not None:
                self.squadPressed.emit(squad, event.modifiers())
                self._drag_actor = not bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
                if self._drag_actor:
                    self.actorDragStarted.emit('squad', self.ground_cell(event.position().x(), event.position().y()))
                else:
                    self._drag_button = None
                return
            if self.active_tool == 'special':
                return
            cell = self.pick_cell(event.position().x(), event.position().y())
            if cell and self.sample_active and self.active_tool == 'terrain':
                self.cellPressed.emit(*cell, event.button(), event.modifiers())
                self._drag_button = None
                return
            self._paint_gesture = self.active_tool in ('sector','building','owner','terrain') and not bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
            if cell and self._paint_gesture:
                self._paint_last = cell
                self._paint_started = True
                self.cellPressed.emit(*cell, event.button(), event.modifiers())

    def mouseMoveEvent(self, event):
        pos = event.position()
        delta = pos - self._last
        self._last = pos
        if self._press_pos is not None and (pos - self._press_pos).manhattanLength() >= 4:
            self._moved = True
        if self._drag_button == Qt.MouseButton.RightButton:
            if not self._moved:
                return
            self.camera.yaw = (self.camera.yaw - delta.x() * 0.4) % 360
            self.camera.pitch = max(-85.0 if self.camera.perspective else 8.0,
                                    min(85.0 if self.camera.perspective else 90.0, self.camera.pitch + delta.y() * 0.3))
            self.setCursor(Qt.CursorShape.SizeAllCursor)
            self._begin_interaction()
            self.cameraChanged.emit()
            self.update()
            return
        if self._drag_button == Qt.MouseButton.MiddleButton:
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            self._pan_pixels(delta.x(), delta.y())
            return
        if self.camera.perspective:
            self.setCursor(Qt.CursorShape.CrossCursor)
            return
        cell = self.ground_cell(pos.x(), pos.y())
        if cell != self.hover:
            self.hover = cell
            self.cellHovered.emit(*(cell if cell else (-1, -1)))
            self.update()
        if (event.modifiers() & Qt.KeyboardModifier.ShiftModifier or self._sweep_gesture) and not self.draft_active and self.active_tool != 'terrain':
            self._sweep_gesture = True
            self.setCursor(Qt.CursorShape.CrossCursor)
            self._sweep_over(cell)
            return
        self._sweep_last = None
        if self.sample_active:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        elif self.draft_active and not self.preview_cells and not any(a.get('_preview') for a in self.doc.squads + self.doc.host_stations):
            self.setCursor(Qt.CursorShape.ForbiddenCursor)
        elif self.draft_active or self.active_tool == 'terrain' or event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.setCursor(Qt.CursorShape.CrossCursor)
        elif self.pick_squad(pos.x(), pos.y()) is not None or self.pick_host(pos.x(), pos.y()) is not None:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        if self._drag_button == Qt.MouseButton.LeftButton:
            if self._drag_actor:
                if self._moved:
                    self.setCursor(Qt.CursorShape.ClosedHandCursor)
                    self.actorDragged.emit(cell)
            elif self._paint_gesture:
                if cell and not self._paint_started:
                    self._paint_started = True
                    self.cellPressed.emit(*cell, Qt.MouseButton.LeftButton, event.modifiers())
                elif cell:
                    from ..tools.paint import cells_between
                    cells = [cell] if self.active_tool == 'terrain' or self._paint_last is None else cells_between(self._paint_last, cell)
                    for crossed in cells:
                        self.cellDragged.emit(*crossed, Qt.MouseButton.LeftButton, event.modifiers())
                self._paint_last = cell

    def mouseReleaseEvent(self, event):
        cell = self.ground_cell(event.position().x(), event.position().y())
        if self.camera.perspective or self._sweep_gesture:
            if self.camera.perspective and event.button() == Qt.MouseButton.RightButton and not self._moved:
                self.contextRequested.emit(event.position(),event.globalPosition().toPoint())
            if self._paint_started:
                self.cellReleased.emit(*(cell if cell else (-1,-1)),event.modifiers())
                self._paint_started = False
            self._drag_button = None
            self._press_pos = None
            self._sweep_gesture = False
            self._sweep_last = None
            self.unsetCursor()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            if self._drag_actor:
                self.actorDragFinished.emit()
                self.unsetCursor()
            elif self._paint_gesture:
                self.cellReleased.emit(*(cell if cell else (-1, -1)), event.modifiers())
            elif self._drag_button is not None:
                self.cellSelected.emit(cell, event.modifiers())
        elif event.button() == Qt.MouseButton.RightButton and self._drag_button is not None and not self._moved:
            self.contextRequested.emit(event.position(), event.globalPosition().toPoint())
        self._drag_button = None
        self._press_pos = None
        self._drag_actor = False
        self._paint_started = False
        self.unsetCursor()
        self.update()

    def mouseDoubleClickEvent(self, event):
        if self.camera.perspective:
            return
        cell = self.pick_cell(event.position().x(), event.position().y())
        if self.active_tool == 'squad' and event.button() == Qt.MouseButton.LeftButton and self.pick_squad(event.position().x(), event.position().y()) is None:
            self.selectionCleared.emit()
        elif cell and event.button() == Qt.MouseButton.LeftButton:
            self.cellDoubleClicked.emit(*cell, event.modifiers())

    def ground_cell(self, x, y):
        cell = self.pick_cell(x, y)
        if cell is not None or self.doc is None:
            return cell
        point = self.camera.screen_to_ground(x, y)
        if point is not None:
            col, row = math.floor(point[0] / SECTOR_SIZE), math.floor(-point[2] / SECTOR_SIZE)
            if 0 <= col < self.doc.mw and 0 <= row < self.doc.mh:
                point = self.camera.screen_to_ground(x, y, self.terrain.cell_y(col, row))
                col, row = math.floor(point[0] / SECTOR_SIZE), math.floor(-point[2] / SECTOR_SIZE)
                if 0 <= col < self.doc.mw and 0 <= row < self.doc.mh:
                    return col, row
        return None

    def current_cursor_color(self):
        return (255, 220, 60) if self.sample_active or time.monotonic() < self.sample_flash_until else self.cursor_color

    def draw_interaction_overlay(self, painter):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.camera.perspective:
            painter.setPen(QPen(QColor(240,240,240,180),1))
            cx,cy = self.width()/2,self.height()/2
            painter.drawLine(QPointF(cx-7,cy),QPointF(cx+7,cy))
            painter.drawLine(QPointF(cx,cy-7),QPointF(cx,cy+7))
            painter.restore()
            return
        if self.preview_cells:
            self._draw_cell_footprint(painter,self.preview_cells,QColor(190,190,190))
        if self.doc is not None and self.active_tool == 'terrain' and self.hover is not None:
            from ..tools.brush import brush_outlines
            col, row = self.hover
            sample = self.sample_active or time.monotonic() < self.sample_flash_until
            pulse = .7 + .3 * math.sin(time.monotonic() * 8) if sample else 1
            color = QColor(*( (255, 220, 60) if sample else self.cursor_color))
            color.setAlpha(round(180 + 75 * pulse))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            cx, cy, cz = self.terrain.cell_center(col, row)
            loops = ([[( -.5, -.5), (.5, -.5), (.5, .5), (-.5, .5)]] if sample else
                     brush_outlines(self.terrain_brush.shape, self.terrain_brush.radius_x, self.terrain_brush.radius_z))
            for loop in loops:
                points = []
                for dx, dz in loop:
                    x, z = cx + dx * SECTOR_SIZE, cz - dz * SECTOR_SIZE
                    c, r = math.floor(x / SECTOR_SIZE), math.floor(-z / SECTOR_SIZE)
                    y = self.terrain.cell_y(c, r) if 0 <= c < self.doc.mw and 0 <= r < self.doc.mh else cy
                    points.append(QPointF(*self.camera.world_to_screen((x, y - 3, z))[0]))
                polygon = QPolygonF(points)
                painter.setPen(QPen(QColor(color.red(), color.green(), color.blue(), 55), 9 if sample else 6))
                painter.drawPolygon(polygon)
                painter.setPen(QPen(color, 3 if sample else 2))
                painter.drawPolygon(polygon)
        painter.restore()

    def leaveEvent(self, event):
        self.hover = None
        self.cellHovered.emit(-1, -1)
        self.update()

    def _pan_pixels(self, dx, dy):
        cam = self.camera
        a = cam.screen_to_ground(cam.width / 2, cam.height / 2)
        b = cam.screen_to_ground(cam.width / 2 - dx, cam.height / 2 - dy)
        if a and b:
            cam.center = (cam.center[0] + b[0] - a[0], cam.center[1],
                          cam.center[2] + b[2] - a[2])
        self._begin_interaction()
        self.cameraChanged.emit()
        self.update()

    def wheelEvent(self, event):
        cam = self.camera
        if cam.perspective:
            cam.fov = max(35,min(110,cam.fov-event.angleDelta().y()/120*5))
            self.cameraChanged.emit()
            self.update()
            return
        anchor = cam.screen_to_ground(event.position().x(), event.position().y())
        cam.zoom = max(0.012, min(1.2, cam.zoom * 1.15 ** (event.angleDelta().y() / 120.0)))
        after = cam.screen_to_ground(event.position().x(), event.position().y())
        if anchor and after:
            cam.center = (cam.center[0] + anchor[0] - after[0], cam.center[1],
                          cam.center[2] + anchor[2] - after[2])
        self._begin_interaction()
        self.cameraChanged.emit()
        self.update()

    def _tick_navigation(self):
        if not self._nav_keys:
            self._nav_timer.stop()
            return
        now = time.monotonic()
        dt = min(0.05, max(0.0, now - self._nav_last))
        self._nav_last = now
        if self.camera.perspective:
            k = self._nav_keys
            forward = int(Qt.Key.Key_W in k or Qt.Key.Key_Up in k)-int(Qt.Key.Key_S in k or Qt.Key.Key_Down in k)
            side = int(Qt.Key.Key_D in k or Qt.Key.Key_Right in k)-int(Qt.Key.Key_A in k or Qt.Key.Key_Left in k)
            up = int(Qt.Key.Key_R in k)-int(Qt.Key.Key_F in k)
            yaw = math.radians(self.camera.yaw)
            length = max(1,math.sqrt(forward**2+side**2+up**2))
            speed = (2200 if QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier else 700)*dt/length
            x,y,z = self.camera.center
            self.camera.center = (x+(forward*math.sin(yaw)+side*math.cos(yaw))*speed,
                                  y-up*speed,z+(-forward*math.cos(yaw)+side*math.sin(yaw))*speed)
            self.cameraChanged.emit()
            self.update()
            return
        x = int(Qt.Key.Key_A in self._nav_keys or Qt.Key.Key_Left in self._nav_keys)
        x -= int(Qt.Key.Key_D in self._nav_keys or Qt.Key.Key_Right in self._nav_keys)
        y = int(Qt.Key.Key_W in self._nav_keys or Qt.Key.Key_Up in self._nav_keys)
        y -= int(Qt.Key.Key_S in self._nav_keys or Qt.Key.Key_Down in self._nav_keys)
        if not x and not y:
            return
        length = math.hypot(x, y)
        speed = 900.0
        self._pan_pixels(x / length * speed * dt, y / length * speed * dt)

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Escape:
            if self.camera.perspective:
                self.reset_camera()
                return
            self.operationCancelled.emit()
            self.unsetCursor()
            self._drag_button = None
            self._drag_actor = False
            self.update()
            return
        nav_keys = {Qt.Key.Key_W, Qt.Key.Key_A, Qt.Key.Key_S, Qt.Key.Key_D,
                    Qt.Key.Key_Up, Qt.Key.Key_Left, Qt.Key.Key_Down, Qt.Key.Key_Right}
        if self.camera.perspective:
            nav_keys |= {Qt.Key.Key_R,Qt.Key.Key_F}
        if key in nav_keys:
            if not event.isAutoRepeat():
                was_idle = not self._nav_keys
                self._nav_keys.add(key)
                if was_idle:
                    self._nav_last = time.monotonic() - 1.0 / 60.0
                    self._tick_navigation()
                self._nav_timer.start()
            event.accept()
        elif key == Qt.Key.Key_Home:
            self.reset_camera()
        elif key in (Qt.Key.Key_Q, Qt.Key.Key_E):
            self.camera.yaw = (self.camera.yaw + (15 if key == Qt.Key.Key_Q else -15)) % 360
            self._begin_interaction()
            self.cameraChanged.emit()
            self.update()
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        key = event.key()
        if key in self._nav_keys:
            if not event.isAutoRepeat():
                self._nav_keys.discard(key)
                if not self._nav_keys:
                    self._nav_timer.stop()
            event.accept()
            return
        super().keyReleaseEvent(event)

    def focusOutEvent(self, event):
        if self._paint_started:
            self.cellReleased.emit(-1,-1,Qt.KeyboardModifier.NoModifier)
            self._paint_started = False
            self._drag_button = None
        self._sweep_last = None
        self._sweep_gesture = False
        self._nav_keys.clear()
        self._nav_timer.stop()
        super().focusOutEvent(event)

    def _sweep_over(self, cell):
        if cell is None:
            self._sweep_last = None
            return
        from ..tools.paint import cells_between
        for crossed in cells_between(self._sweep_last or cell,cell):
            self.cellSwept.emit(crossed)
        self._sweep_last = cell

    def stop_rendering(self):
        self._closed = True
        self._render_timer.stop()
        self._settle.stop()
        self._nav_timer.stop()
        self._pool.waitForDone()


def create_viewport():
    try:
        from .gpu_viewport import create_viewport as create_gpu_viewport
    except ImportError:
        view = MapViewport()
        view.renderer_name = 'Software (OpenGL package unavailable)'
        return view
    return create_gpu_viewport()
