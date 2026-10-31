"""Sector interaction on the existing Map Editor GPU/software viewport."""

import os
from dataclasses import replace

from PySide6.QtCore import QEvent, QRectF, QPointF, Qt, Signal
from PySide6.QtGui import QColor, QPen, QPolygonF
from PySide6.QtWidgets import QApplication, QToolTip

from map_editor.render.gpu_viewport import GpuMapViewport, available
from map_editor.render.sector_mesh import SectorMesh
from map_editor.core.ldf_model import DEFAULT_HGT, LdfDocument
from studio.rendering.asset_viewport import PickShape, resolve_pick_shape
from studio.ui.cursor_themes import studio_cursor
from studio.rendering.indexed import retail_source_face_front_facing


class PreviewLibrary:
    """Select a damage state without changing the shared renderer or authored data."""
    def __init__(self, library, state=0, model=None, mesh_override=None):
        self.library = library
        self.state = state
        self.model = model
        self.mesh_override = mesh_override
        self.preview_face_codes = {}
        self.preview_styles = []

    def __getattr__(self, name):
        return getattr(self.library, name)

    def mesh(self, typ, state=0):
        if self.mesh_override is not None:
            return self.mesh_override
        if self.model is None:
            return self.library.mesh(typ, self.state)
        name = self.library.assets.sdf.models[self.model].base
        faces = self.library._base_faces(name)
        return SectorMesh(typ, [(face, 0, 0) for face in faces or ()],
                          missing=[] if faces is not None else [name])


class SectorViewport(GpuMapViewport):
    cellsPicked = Signal(int, bool)
    cellsRangePicked = Signal(int)
    cellContextRequested = Signal(int, object)
    modelDropped = Signal(int, int)
    placementRequested = Signal(int)
    placementHovered = Signal(object)
    previewFailed = Signal(str)
    cellsBoxPicked = Signal(object, bool)
    selectionCleared = Signal()
    cellDragStarted = Signal(int)
    navigationRequested = Signal(int, object)
    applyRequested = Signal()

    def __init__(self, parent=None, *, top_down=False):
        enabled = os.environ.get("NME_RENDERER", "").casefold() != "software" and available()
        super().__init__(parent, enable_gpu=enabled)
        self.selected_cells = {4}
        self.occupied_cells = set()
        self.top_down = top_down
        self.model_source = None
        self.placement_active = False
        self.cell_details = {}
        self.target_cells = set()
        self._ghost_mesh = None
        self._ghost_spec = None
        self._source_library = None
        self._number, self._state = 0, 0
        self._cell_press = None
        self._gesture = None
        self._selection_box = None
        self.hover_cell = None
        self.setAcceptDrops(True)
        self.setMouseTracking(True)
        self.contextRequested.connect(self._request_context)
        self.single = False
        self.model_preview = False
        self._home_camera = None
        self.set_grid(False)
        self.set_sky_visible(False)
        self.setToolTip("Left click: select cell · Ctrl+click: add cell · Right drag: orbit · Middle drag: pan · Wheel: zoom")

    def show_sector(self, library, number, *, state=0, model=None, reset=False):
        camera = self.camera.copy()
        self._source_library, self._number, self._state = library, number, state
        self._ghost_spec = self._ghost_mesh = None
        self.target_cells.clear()
        self.model_preview = model is not None
        sector = library.assets.sdf.sectors.get(number)
        self.single = sector.single if sector else False
        self.set_library(PreviewLibrary(library, state, model))
        if self.doc is None:
            doc = LdfDocument(mw=1, mh=1)
            doc.grids["hgt"][0][0] = DEFAULT_HGT
            doc.grids["blg"][0][0] = "00"
            doc.grids["own"][0][0] = 0
            self.set_document(doc)
        self.doc.grids["type"][0][0] = f"{number:02x}"
        self.scene_changed()
        if reset:
            super().reset_camera()
            mesh = self.lib.mesh(number)
            if mesh.faces:
                bounds = mesh.bounds
                self.camera.center = (600 + (bounds[0] + bounds[3]) / 2,
                                      (bounds[1] + bounds[4]) / 2,
                                      -600 + (bounds[2] + bounds[5]) / 2)
                extent = max(1000, bounds[3] - bounds[0], bounds[4] - bounds[1], bounds[5] - bounds[2])
                self.camera.zoom = min(self.width(), self.height()) / (extent * 1.8)
            self._home_camera = self.camera.copy()
        else:
            self.camera = camera
        if self.top_down:
            self._fit_top()
        self.update()

    def _fit_top(self):
        self.camera.yaw, self.camera.pitch = 180, 90
        self.camera.perspective = False
        self.camera.center, self.camera.pan = (600, 0, -600), (0, 0)
        self.camera.zoom = max(1, min(self.width(), self.height()) - 24) / 900

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.top_down:
            self._fit_top()

    def reset_camera(self):
        if self.top_down:
            self._fit_top()
            self.update()
            return
        if self._home_camera is None:
            return super().reset_camera()
        self.camera = self._home_camera.copy()
        self.camera.width, self.camera.height = self.width(), self.height()
        self.cameraChanged.emit()
        self.update()

    def cell_index(self, ox, oz):
        return 4 if self.single else (1 - round(oz / 300)) * 3 + round(ox / 300) + 1

    def pick_subcell(self, point):
        """Use the Model Editor's depth picker before falling back to the ground grid."""
        if self.lib is None or self.doc is None or self.model_preview:
            return None
        number = int(self.doc.grids["type"][0][0], 16)
        shapes = []
        for order, (face, ox, oz) in enumerate(self.lib.mesh(number).faces):
            camera = tuple(self.camera.to_camera((x + ox + 600, y, z + oz - 600))
                           for x, y, z in face.vertices)
            if not retail_source_face_front_facing(camera):
                continue
            polygon = QPolygonF([QPointF(*self.camera.to_screen(vertex)) for vertex in camera])
            cell = self.cell_index(ox, oz)
            shapes.append(PickShape(polygon, replace(face, owner=str(cell)), camera, order))
        picked = resolve_pick_shape(shapes, point)
        if picked is not None:
            return int(picked.face.owner)
        ground = self.camera.screen_to_ground(point.x(), point.y(), 0)
        if ground is None:
            return None
        x, _, z = ground
        col, row = int((x - 150) // 300), int((-z - 150) // 300)
        return (4 if self.single else row * 3 + col) if 0 <= col < 3 and 0 <= row < 3 else None

    def frame_selection(self):
        if self.doc is None or self.lib is None:
            return
        mesh = self.lib.mesh(int(self.doc.grids["type"][0][0], 16))
        points = [(x + ox + 600, y, z + oz - 600) for face, ox, oz in mesh.faces
                  if self.model_preview or self.cell_index(ox, oz) in self.selected_cells
                  for x, y, z in face.vertices]
        if not points:
            return
        lows = tuple(min(p[axis] for p in points) for axis in range(3))
        highs = tuple(max(p[axis] for p in points) for axis in range(3))
        self.camera.center = tuple((low + high) / 2 for low, high in zip(lows, highs))
        self.camera.pan = (0, 0)
        extent = max(300, *(high - low for low, high in zip(lows, highs)))
        self.camera.zoom = min(self.width(), self.height()) / (extent * 1.8)
        self.cameraChanged.emit()
        self.update()

    def capture_preview(self, width=1024):
        current = self.lib
        if self._ghost_mesh is None:
            return self._capture_preview(width)
        self.set_library(PreviewLibrary(self._source_library, self._state))
        try:
            return self._capture_preview(width)
        finally:
            self.set_library(current)

    def _capture_preview(self, width=1024):
        """Render the current view through the shared renderer, without editor overlays."""
        if self.lib is None or self.doc is None:
            raise ValueError("Open a sector or model first.")
        camera = self.camera.copy()
        ratio = width / max(1, self.width())
        camera.width, camera.height = width, max(1, round(self.height() * ratio))
        camera.zoom *= ratio
        camera.pan = tuple(value * ratio for value in camera.pan)
        if self.renderer is not None and not self._software_fallback:
            from OpenGL import GL
            from PySide6.QtOpenGL import QOpenGLFramebufferObject
            self._canvas.makeCurrent()
            target = None
            try:
                self._paint_gpu()
                target = QOpenGLFramebufferObject(camera.width, camera.height)
                self.renderer.render(camera, camera.width, camera.height, target.handle(), owner_colors={},
                                     grid=False, sky=False, overlays=False, transparent_background=True)
                return target.toImage()
            finally:
                del target
                GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self._canvas.defaultFramebufferObject())
                self._canvas.doneCurrent()
                self._gpu_camera_key = None
                self.update()
        from map_editor.render.map_scene import render_scene, scene_polygons
        frame = render_scene(scene_polygons(self.lib, self.doc, self.terrain, camera), camera, self.lib.tables)
        frame.rgba[frame.cell_ids == 0, 3] = 0
        return frame.image()

    def set_ghost(self, model=None, cells=(), *, placements=None, hidden=()):
        """Replace target geometry in the shared scene; native tables stay untouched."""
        targets = frozenset(cells)
        placements = tuple((model, cell) for cell in sorted(targets)) if placements is None and model is not None else tuple(placements or ())
        spec = (placements, targets, frozenset(hidden))
        if spec == self._ghost_spec or self._source_library is None or self.model_preview:
            return
        self._ghost_spec = spec
        self.target_cells = set(targets)
        library = self._source_library
        preview = PreviewLibrary(library, self._state)
        self._ghost_mesh = None
        if placements:
            original = preview.mesh(self._number)
            removed = targets | frozenset(hidden)
            faces = [(face, ox, oz) for face, ox, oz in original.faces
                     if self.cell_index(ox, oz) not in removed]
            ghosts = []
            for model, cell in placements:
                if isinstance(model, str):
                    name = model
                elif model is not None and 0 <= model < len(library.assets.sdf.models):
                    name = library.assets.sdf.models[model].base
                else:
                    continue
                source = library._base_faces(name)
                if source is None:
                    self.previewFailed.emit(f"Preview unavailable: {name}")
                    continue
                ox, oz = (0, 0) if self.single else ((cell % 3 - 1) * 300, (1 - cell // 3) * 300)
                # Faces need separate identities when a BASE is also used in unchanged cells.
                for face in source:
                    ghost = replace(face)
                    ghosts.append((ghost, ox, oz))
                    preview.preview_face_codes[id(ghost)] = -2
            if ghosts:
                self._ghost_mesh = SectorMesh(self._number, ghosts)
                preview.mesh_override = SectorMesh(self._number, faces + ghosts, original.resolved, original.missing)
                preview.preview_styles = [(160, 160, 160, 3)]
        self.set_library(preview)
        self.update()

    def cell_polygon(self, index):
        col, row = index % 3, index // 3
        half = 450 if self.single else 150
        x, z = 600 + (col - 1) * 300, -600 + (1 - row) * 300
        return QPolygonF([QPointF(*self.camera.world_to_screen((px, -2, pz))[0])
                          for px, pz in ((x-half, z-half), (x+half, z-half),
                                         (x+half, z+half), (x-half, z+half))])

    def _request_context(self, point, global_point):
        cell = self.pick_subcell(point)
        if cell is not None:
            self.cellContextRequested.emit(cell, global_point)

    def _drag_model(self, event):
        if self.model_preview or event.source() is not self.model_source or self.model_source is None:
            return None
        mime = event.mimeData()
        if not mime.hasFormat("application/x-openneoua-sector-model"):
            return None
        try:
            model = int(bytes(mime.data("application/x-openneoua-sector-model")))
            if self.lib is not None and 0 <= model < len(self.lib.assets.sdf.models):
                return model
        except ValueError:
            pass
        return None

    def dragEnterEvent(self, event):
        if self._drag_model(event) is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        model = self._drag_model(event)
        cell = self.pick_subcell(event.position())
        if model is not None and cell is not None:
            cells = self.selected_cells if cell in self.selected_cells else {cell}
            if (model, frozenset(cells)) != getattr(self, '_drop_target', None):
                self._drop_target = model, frozenset(cells)
                self.set_ghost(model, cells)
            event.acceptProposedAction()
        else:
            self._drop_target = None
            self.set_ghost()
            event.ignore()

    def dragLeaveEvent(self, event):
        self._drop_target = None
        self.set_ghost()
        event.accept()

    def dropEvent(self, event):
        model = self._drag_model(event)
        cell = self.pick_subcell(event.position())
        self._drop_target = None
        self.set_ghost()
        if model is not None and cell is not None:
            self.modelDropped.emit(model, cell)
            event.acceptProposedAction()
        else:
            event.ignore()

    def event(self, event):
        if event.type() == QEvent.Type.ToolTip and not self.model_preview:
            cell = self.pick_subcell(QPointF(event.pos()))
            text = self.cell_details.get(cell, "")
            if text:
                QToolTip.showText(event.globalPos(), text, self)
            else:
                QToolTip.hideText()
            return True
        return super().event(event)

    def cancel_gesture(self):
        self._cell_press = self._gesture = self._selection_box = None
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and not self.model_preview:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self._cell_press = event.position()
            self._press_cell = self.pick_subcell(event.position())
            self._cell_modifiers = event.modifiers()
            self._gesture = "confirm" if self.placement_active else "pending"
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        self.hover_cell = cell = self.pick_subcell(event.position())
        if self._cell_press is not None:
            distance = (event.position() - self._cell_press).manhattanLength()
            if self._gesture == "pending" and distance >= QApplication.startDragDistance():
                if self._press_cell in (self.selected_cells | self.occupied_cells) and not self._cell_modifiers:
                    self._gesture = "move"
                    self.cellDragStarted.emit(self._press_cell)
                    if not self.placement_active:
                        self._gesture = "cancelled"
                else:
                    self._gesture = "marquee"
            if self._gesture == "marquee":
                self._selection_box = QRectF(self._cell_press, event.position()).normalized()
                self.setCursor(studio_cursor("place"))
                self.update()
            elif self._gesture in ("move", "confirm"):
                self.placementHovered.emit(cell)
            event.accept()
            return
        if self.placement_active and not event.buttons():
            self.placementHovered.emit(cell)
        elif self._drag_button is None:
            self.setCursor(studio_cursor(
                "open" if cell in (self.selected_cells | self.occupied_cells) and self._ghost_mesh is None
                else "point"))
        if self._drag_button is not None and not self.top_down:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and not self.model_preview:
            gesture, box = self._gesture, self._selection_box
            self._cell_press = self._gesture = self._selection_box = None
            cell = self.pick_subcell(event.position())
            if gesture == "marquee" and box is not None:
                rectangle = QPolygonF(box)
                cells = {index for index in ({4} if self.single else range(9))
                         if self.cell_polygon(index).intersects(rectangle)}
                self.cellsBoxPicked.emit(cells, bool(self._cell_modifiers & Qt.KeyboardModifier.ControlModifier))
            elif gesture in ("move", "confirm"):
                if cell is None:
                    self.operationCancelled.emit()
                else:
                    self.placementRequested.emit(cell)
            elif gesture == "pending" and cell is not None:
                if self._cell_modifiers & Qt.KeyboardModifier.ShiftModifier:
                    self.cellsRangePicked.emit(cell)
                else:
                    self.cellsPicked.emit(cell, bool(self._cell_modifiers & Qt.KeyboardModifier.ControlModifier))
            self.update()
            event.accept()
            return
        super().mouseReleaseEvent(event)
        self.setCursor(studio_cursor("point"))

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and not self.model_preview:
            if self.pick_subcell(event.position()) is None:
                self._cell_press = self._gesture = self._selection_box = None
                self.selectionCleared.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def leaveEvent(self, event):
        self.hover_cell = None
        if self.placement_active and self._cell_press is None:
            self.placementHovered.emit(None)
        super().leaveEvent(event)

    def wheelEvent(self, event):
        if self.top_down:
            event.ignore()
        else:
            super().wheelEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.cancel_gesture()
            self.operationCancelled.emit()
            event.accept()
        elif event.key() in (Qt.Key.Key_W, Qt.Key.Key_A, Qt.Key.Key_S, Qt.Key.Key_D):
            self.navigationRequested.emit(event.key(), event.modifiers())
            event.accept()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.applyRequested.emit()
            event.accept()
        elif self.top_down and event.key() != Qt.Key.Key_Escape:
            event.ignore()
        else:
            super().keyPressEvent(event)

    def draw_interaction_overlay(self, painter):
        window = self.window()
        if self.model_preview or self.doc is None:
            return
        painter.save()
        if not self.top_down and hasattr(window, "grid_action") and not window.grid_action.isChecked() and not self.target_cells:
            painter.restore()
            return
        for index in range(9):
            if self.single and index != 4:
                continue
            polygon = self.cell_polygon(index)
            chosen, target = index in self.selected_cells, index in self.target_cells
            color = QColor(230, 215, 135, 245) if target else QColor(75, 195, 220, 230) if chosen else QColor(180, 185, 195, 100)
            painter.setPen(QPen(color, 3 if target else 2 if chosen else 1))
            painter.setBrush(QColor(75, 195, 220, 30) if chosen else Qt.BrushStyle.NoBrush)
            painter.drawPolygon(polygon)
            if self.top_down:
                rect = polygon.boundingRect()
                painter.fillRect(QRectF(rect.left() + 3, rect.top() + 3, 18, 18), QColor(18, 22, 28, 195))
                painter.setPen(QColor(215, 225, 235))
                painter.drawText(rect.adjusted(6, 3, -3, -3), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, str(index + 1))
        if self._selection_box is not None:
            painter.setPen(QPen(QColor(75, 195, 220), 1, Qt.PenStyle.DashLine))
            painter.setBrush(QColor(75, 195, 220, 35))
            painter.drawRect(self._selection_box)
        painter.restore()
