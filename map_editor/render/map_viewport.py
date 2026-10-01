from __future__ import annotations

import copy
import math

import numpy as np
from PySide6.QtCore import QObject, QPointF, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import (QColor, QImage, QPainter, QPainterPath,
                           QPainterPathStroker, QPen, QPixmap, QPolygonF)
from PySide6.QtWidgets import QWidget

from ..core.factions import load_owner_colors
from ..core.ldf_model import LdfDocument, SECTOR_SIZE
from .camera import ISO_PITCH, ISO_YAW, IsoCamera
from .sector_mesh import SectorMeshLibrary
from .map_scene import render_scene, scene_polygons
from .terrain_mesh import TerrainMesh


class _RenderSignals(QObject):
    finished = Signal(object)


class _RenderJob(QRunnable):
    def __init__(self, polygons, camera, tables, key, generation, terrain):
        super().__init__()
        self.signals = _RenderSignals()
        self.polygons, self.camera, self.tables = polygons, camera, tables
        self.key, self.generation, self.terrain = key, generation, terrain

    def run(self):
        try:
            frame = render_scene(self.polygons, self.camera, self.tables, fast=self.key[-1])
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
    cameraChanged = Signal()
    statusMessage = Signal(str)
    backendChanged = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(480, 360)
        self.setToolTip("Click: edit · right drag: rotate · middle drag: pan · wheel: zoom · Home: recenter")
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
        self.recenter()
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

    def _camera_key(self):
        cam = self.camera
        return (cam.yaw, cam.pitch, cam.zoom, cam.center, cam.pan, self.width(), self.height())

    def _render_key(self):
        return (self._scene_revision, self._camera_key(), self._interacting or self._editing)

    def _launch_render(self):
        if self._job is not None or self.lib is None or self.doc is None:
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
        self._job = _RenderJob(polygons, cam, self.lib.tables, key, self._generation, terrain)
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
            overlay = getattr(self, 'building_overlay', None)
            if overlay is not None:
                cam = self.camera.copy()
                cam.yaw, cam.pitch, cam.zoom, cam.center, cam.pan, cam.width, cam.height = self._frame_key[1]
                overlay.draw(painter, cam, self._display_terrain)
            if self._frame_key[1] == self._camera_key():
                painter.drawImage(0, 0, self._annotations())
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
            if (col, row) in self.brush_cells:
                painter.setPen(QPen(QColor(245, 245, 245, 190), 1.2, Qt.PenStyle.DashLine))
                for side, neighbour in enumerate(((col, row - 1), (col + 1, row),
                                                  (col, row + 1), (col - 1, row))):
                    if neighbour not in self.brush_cells:
                        for edge in range(side * 3, side * 3 + 3):
                            painter.drawLine(points[edge], points[(edge + 1) % 12])
            if (col, row) in self.selection or (col, row) == self.hover:
                self._draw_brackets(painter, points, (col, row) in self.selection)
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
    def _draw_brackets(painter, points, selected):
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
                painter.setPen(QPen(QColor(255, 255, 255, 255 if selected else 180),
                                    1.7 if selected else 1.1))
                painter.drawLine(corner, end)

    def pick_cell(self, x: float, y: float):
        if self._frame is None or self._frame_key[1] != self._camera_key():
            return None
        return self._frame.pick(x, y, self.doc.mw)

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
        if event.button() == Qt.MouseButton.LeftButton:
            cell = self.pick_cell(event.position().x(), event.position().y())
            if cell:
                self.cellPressed.emit(*cell, event.button(), event.modifiers())

    def mouseMoveEvent(self, event):
        pos = event.position()
        delta = pos - self._last
        self._last = pos
        if self._drag_button == Qt.MouseButton.RightButton:
            self.camera.yaw = (self.camera.yaw - delta.x() * 0.4) % 360
            self.camera.pitch = max(8.0, min(90.0, self.camera.pitch + delta.y() * 0.3))
            self._begin_interaction()
            self.cameraChanged.emit()
            self.update()
            return
        if self._drag_button == Qt.MouseButton.MiddleButton:
            self._pan_pixels(delta.x(), delta.y())
            return
        cell = self.pick_cell(pos.x(), pos.y())
        if cell != self.hover:
            self.hover = cell
            self.cellHovered.emit(*(cell if cell else (-1, -1)))
            self.update()
        if self._drag_button == Qt.MouseButton.LeftButton and cell:
            self.cellDragged.emit(*cell, event.button(), event.modifiers())

    def mouseReleaseEvent(self, event):
        cell = self.pick_cell(event.position().x(), event.position().y())
        if event.button() == Qt.MouseButton.LeftButton:
            self.cellReleased.emit(*(cell if cell else (-1, -1)), event.modifiers())
        self._drag_button = None

    def mouseDoubleClickEvent(self, event):
        cell = self.pick_cell(event.position().x(), event.position().y())
        if cell and event.button() == Qt.MouseButton.LeftButton:
            self.cellDoubleClicked.emit(*cell, event.modifiers())

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
        anchor = cam.screen_to_ground(event.position().x(), event.position().y())
        cam.zoom = max(0.012, min(1.2, cam.zoom * 1.15 ** (event.angleDelta().y() / 120.0)))
        after = cam.screen_to_ground(event.position().x(), event.position().y())
        if anchor and after:
            cam.center = (cam.center[0] + anchor[0] - after[0], cam.center[1],
                          cam.center[2] + anchor[2] - after[2])
        self._begin_interaction()
        self.cameraChanged.emit()
        self.update()

    def keyPressEvent(self, event):
        step = 60
        key = event.key()
        moves = {Qt.Key.Key_W: (0, step), Qt.Key.Key_Up: (0, step),
                 Qt.Key.Key_S: (0, -step), Qt.Key.Key_Down: (0, -step),
                 Qt.Key.Key_A: (step, 0), Qt.Key.Key_Left: (step, 0),
                 Qt.Key.Key_D: (-step, 0), Qt.Key.Key_Right: (-step, 0)}
        if key in moves:
            self._pan_pixels(*moves[key])
        elif key == Qt.Key.Key_Home:
            self.camera.yaw, self.camera.pitch = ISO_YAW, ISO_PITCH
            self.recenter()
        elif key in (Qt.Key.Key_Q, Qt.Key.Key_E):
            self.camera.yaw = (self.camera.yaw + (15 if key == Qt.Key.Key_Q else -15)) % 360
            self._begin_interaction()
            self.cameraChanged.emit()
            self.update()
        else:
            super().keyPressEvent(event)


def create_viewport():
    try:
        from .gpu_viewport import create_viewport as create_gpu_viewport
    except ImportError:
        view = MapViewport()
        view.renderer_name = 'Software (OpenGL package unavailable)'
        return view
    return create_gpu_viewport()
