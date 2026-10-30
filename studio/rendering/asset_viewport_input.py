"""AssetViewport mixin: mouse and keyboard interaction."""

from __future__ import annotations

import math
import time

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, Qt
from PySide6.QtGui import (
    QMouseEvent,
    QWheelEvent,
)

from studio.rendering.asset_viewport_common import (
    VertexPressState,
    resolve_pick_shape,
)


class _InteractionMixin:
    """Part of AssetViewport; `self` is always the AssetViewport."""

    # -- interaction ------------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._snapshot_active and event.button() == Qt.MouseButton.LeftButton:
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        self._camera_interacting = False
        pos = event.position().toPoint()
        if event.button() == Qt.MouseButton.RightButton:
            # A fresh RMB press always starts as a normal contextual click.
            # If the pointer actually moves while RMB is held, mouseMoveEvent
            # marks the generated context-menu event for suppression instead.
            self._suppress_next_context_menu = False
        if event.button() == Qt.MouseButton.MiddleButton:
            # Middle-click is a viewport shortcut for the existing Reset View
            # command.  Keep a single camera-reset implementation and let the
            # normal camera-changed signal resync the toolbar/gizmo state.
            self.reset_view()
            self.manualCameraChanged.emit()
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_additive = bool(
                event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        if self._paste_preview is not None:
            if event.button() == Qt.MouseButton.LeftButton \
                    and event.modifiers() & Qt.KeyboardModifier.AltModifier:
                self._begin_paste_camera_inspect()
                self._last_mouse = pos
                event.accept()
                return
            if event.button() == Qt.MouseButton.MiddleButton:
                self._begin_paste_camera_inspect()
                self._last_mouse = pos
                event.accept()
                return
            if event.button() == Qt.MouseButton.LeftButton:
                self._paste_left_pressed_at = time.monotonic()
                self._last_mouse = pos
            elif event.button() == Qt.MouseButton.RightButton:
                self._suppress_next_context_menu = True
                self.cancel_paste_preview()
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton \
                and self._mode_label_rect is not None \
                and self._mode_label_rect.contains(pos) \
                and self._family_ref is not None \
                and not self._snapshot_active:
            self.toggle_edit_mode()
            event.accept()
            return
        if self._edit_session is not None and not self._snapshot_active:
            if self._modal_op is not None:
                if event.button() == Qt.MouseButton.LeftButton:
                    self._commit_modal()
                elif event.button() == Qt.MouseButton.RightButton:
                    self._cancel_modal()
                event.accept()
                return
            if self._edit_session.modal_active:
                # A live scale preview owns the geometry, but the viewport
                # remains free for camera inspection until the dialog closes.
                self._last_mouse = pos
                self._press_pos = pos
                event.accept()
                return
            if event.button() == Qt.MouseButton.LeftButton:
                additive = bool(event.modifiers()
                                & Qt.KeyboardModifier.ControlModifier)
                axis = (None if additive
                        else self._gizmo_axis_at(pos))
                if axis is not None:
                    self._begin_gizmo_drag(axis, pos)
                    self._last_mouse = pos
                    event.accept()
                    return
                vertex = self._nearest_edit_vertex(pos)
                if additive:
                    self._marquee_candidate = True
                    self._last_mouse = pos
                    self._press_pos = pos
                    event.accept()
                    return
                if vertex is not None and not (
                        event.modifiers() & Qt.KeyboardModifier.AltModifier):
                    selection = set(self._edit_session.selection)
                    self._vertex_press = VertexPressState(
                        vertex=vertex,
                        position=QPoint(pos),
                        selection=selection,
                        was_selected=vertex in selection,
                    )
                    self._active_edit_vertex = vertex
                    self._last_mouse = pos
                    self._press_pos = pos
                    event.accept()
                    return
        self._last_mouse = pos
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = self._last_mouse
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._snapshot_active and event.button() == Qt.MouseButton.LeftButton:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        was_camera_interacting = self._camera_interacting
        self._camera_interacting = False
        if was_camera_interacting:
            self.update()
        if self._paste_preview is not None:
            pressed_at = self._paste_left_pressed_at
            confirm = (
                event.button() == Qt.MouseButton.LeftButton
                and pressed_at is not None
                and time.monotonic() - pressed_at < 0.350
                and not self._paste_preview.camera_inspect)
            self._paste_left_pressed_at = None
            self._paste_preview.camera_inspect = False
            self._paste_preview.camera_inspect_anchor_screen = None
            self._reanchor_paste_mouse(event.position().toPoint())
            if confirm:
                self.pastePreviewConfirmRequested.emit()
            event.accept()
            return
        if self._edit_session is not None and not self._snapshot_active:
            if self._edit_session.modal_active and self._modal_op is None:
                self._press_pos = None
                self._press_additive = False
                event.accept()
                return
            if self._direct_grab_active \
                    and event.button() == Qt.MouseButton.LeftButton:
                self._commit_modal()
                self._vertex_press = None
                self._press_pos = None
                self._press_additive = False
                event.accept()
                return
            if self._vertex_press is not None \
                    and event.button() == Qt.MouseButton.LeftButton:
                pressed = self._vertex_press
                self._vertex_press = None
                self._edit_session.selection = {pressed.vertex}
                self._active_edit_vertex = pressed.vertex
                self._emit_selection_hint("vertex")
                self._press_pos = None
                self._press_additive = False
                self.update()
                event.accept()
                return
            additive = self._press_additive
            if self._box_start is not None \
                    and event.button() == Qt.MouseButton.LeftButton:
                self._apply_box_select(additive)
                self._marquee_candidate = False
                self._press_additive = False
                event.accept()
                return
            if event.button() == Qt.MouseButton.LeftButton \
                    and self._press_pos is not None:
                delta = event.position().toPoint() - self._press_pos
                if abs(delta.x()) <= 4 and abs(delta.y()) <= 4:
                    point = event.position().toPoint()
                    nearest = self._nearest_edit_vertex(point)
                    if self._edit_pick_polygons \
                            and (nearest is None
                                 or (self._direct_grab_selected_only
                                     and nearest not in
                                     self._edit_session.selection)):
                        self.pick_at(point, additive)
                    else:
                        self._pick_edit_vertex(point, additive)
            self._press_pos = None
            self._marquee_candidate = False
            self._press_additive = False
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton \
                and self._press_pos is not None and not self._snapshot_active:
            delta = event.position().toPoint() - self._press_pos
            if abs(delta.x()) <= 4 and abs(delta.y()) <= 4:
                additive = self._press_additive
                self.pick_at(event.position().toPoint(), additive)
        self._press_pos = None
        self._press_additive = False
        event.accept()

    def pick_at(self, point: QPoint, additive: bool = False) -> int | None:
        """Select the geometrically nearest rendered polygon under the cursor."""

        if self._edit_session is None or self._snapshot_active:
            return None
        hit = resolve_pick_shape(self._pick_shapes, QPointF(point))
        if hit is not None:
            face = hit.face
            self._selected_owner = face.owner
            self.objectPicked.emit(face.owner)
            if face.primary:
                self._selected_poly = face.poly_id
                self.polygonStructurePicked.emit(
                    face.owner, face.poly_id, face.block_index)
                self.polygonPicked.emit(face.poly_id)
                self.polygonPickedDetailed.emit(face.poly_id, additive)
            self.update()
            return face.poly_id if face.primary else None
        return None

    def event(self, ev) -> bool:  # noqa: N802 - Qt override
        # Tab toggles Edit Mode; it must be caught before Qt's focus chain.
        if ev.type() == QEvent.Type.KeyPress \
                and ev.key() == Qt.Key.Key_Tab \
                and self._family_ref is not None and not self._snapshot_active:
            if self._paste_preview is not None:
                self.statusMessage.emit(
                    "Finish or cancel the active placement preview before leaving Edit Mode.")
                return True
            self.toggle_edit_mode()
            return True
        return super().event(ev)

    def _edit_key_press(self, event) -> bool:
        session = self._edit_session
        if session is None:
            return False
        key = event.key()
        mods = event.modifiers()

        if self._modal_op is not None:
            if self._modal_op == "grab" and key in (
                    Qt.Key.Key_Left, Qt.Key.Key_Right,
                    Qt.Key.Key_Up, Qt.Key.Key_Down):
                amount = self._move_nudge_step
                if mods & Qt.KeyboardModifier.ShiftModifier:
                    amount *= 5.0
                dx_sign, dy_sign = {
                    Qt.Key.Key_Left: (-1.0, 0.0),
                    Qt.Key.Key_Right: (1.0, 0.0),
                    Qt.Key.Key_Up: (0.0, -1.0),
                    Qt.Key.Key_Down: (0.0, 1.0),
                }[key]
                step = self._screen_direction_model_delta(
                    dx_sign, dy_sign, amount, self._modal_pivot_world)
                if self._modal_axis is not None:
                    axis = "XYZ".index(self._modal_axis)
                    sign = 1.0 if step[axis] >= 0.0 else -1.0
                    step = tuple(
                        sign * amount if index == axis else 0.0
                        for index in range(3))
                self._modal_keyboard_delta = tuple(
                    self._modal_keyboard_delta[index] + step[index]
                    for index in range(3))
                self._modal_numeric = ""
                self._update_modal(self._modal_last_mouse)
            elif key == Qt.Key.Key_Escape:
                self._cancel_modal()
            elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._commit_modal()
            elif key in (Qt.Key.Key_X, Qt.Key.Key_Y, Qt.Key.Key_Z):
                name = {Qt.Key.Key_X: "X", Qt.Key.Key_Y: "Y",
                        Qt.Key.Key_Z: "Z"}[key]
                self._modal_axis = None if self._modal_axis == name else name
                self._update_modal(self._modal_last_mouse)
            elif key == Qt.Key.Key_Backspace:
                self._modal_numeric = self._modal_numeric[:-1]
                self._update_modal(self._modal_last_mouse)
            else:
                text = event.text()
                if text and (text.isdigit() or text in ".-"):
                    self._modal_numeric += text
                    self._update_modal(self._modal_last_mouse)
            return True

        if key == Qt.Key.Key_A:
            if mods & Qt.KeyboardModifier.AltModifier:
                session.select_none()
            else:
                session.select_all()
            self._emit_selection_hint("vertex")
            self.update()
            return True
        if key == Qt.Key.Key_G:
            self.movePlacementRequested.emit()
            return True
        if key == Qt.Key.Key_R:
            self._begin_modal("rotate")
            return True
        if key == Qt.Key.Key_S:
            self._begin_modal("scale")
            return True
        if key == Qt.Key.Key_Z \
                and mods & Qt.KeyboardModifier.ControlModifier:
            if mods & Qt.KeyboardModifier.ShiftModifier:
                self.redoRequested.emit()
            else:
                self.undoRequested.emit()
            return True
        if key == Qt.Key.Key_Y \
                and mods & Qt.KeyboardModifier.ControlModifier:
            self.redoRequested.emit()
            return True
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right,
                   Qt.Key.Key_Up, Qt.Key.Key_Down):
            amount = self._move_nudge_step
            if mods & Qt.KeyboardModifier.ShiftModifier:
                amount *= 5.0
            dx_sign, dy_sign = {
                Qt.Key.Key_Left: (-1.0, 0.0),
                Qt.Key.Key_Right: (1.0, 0.0),
                Qt.Key.Key_Up: (0.0, -1.0),
                Qt.Key.Key_Down: (0.0, 1.0),
            }[key]
            return self.nudge_edit_selection_step(
                dx_sign, dy_sign, amount)
        return False

    def _paste_key_press(self, event) -> bool:
        state = self._paste_preview
        if state is None:
            return False
        key = event.key()
        mods = event.modifiers()
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right,
                   Qt.Key.Key_Up, Qt.Key.Key_Down):
            amount = self._move_nudge_step
            if mods & Qt.KeyboardModifier.ShiftModifier:
                amount *= 5.0
            dx_sign, dy_sign = {
                Qt.Key.Key_Left: (-1.0, 0.0),
                Qt.Key.Key_Right: (1.0, 0.0),
                Qt.Key.Key_Up: (0.0, -1.0),
                Qt.Key.Key_Down: (0.0, 1.0),
            }[key]
            self.nudge_paste_preview_step(dx_sign, dy_sign, amount)
        elif key == Qt.Key.Key_Escape:
            self.cancel_paste_preview()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.pastePreviewConfirmRequested.emit()
        elif key in (Qt.Key.Key_X, Qt.Key.Key_Y, Qt.Key.Key_Z):
            name = {Qt.Key.Key_X: "X", Qt.Key.Key_Y: "Y",
                    Qt.Key.Key_Z: "Z"}[key]
            state.axis = None if state.axis == name else name
            state.numeric = ""
            self._reanchor_paste_mouse(state.last_mouse)
            self._emit_paste_hint()
            self.update()
        elif key == Qt.Key.Key_G:
            state.axis = None
            state.numeric = ""
            self._reanchor_paste_mouse(state.last_mouse)
            self._emit_paste_hint()
            self.update()
        elif key == Qt.Key.Key_Backspace:
            state.numeric = state.numeric[:-1]
            self._update_paste_preview(state.last_mouse)
        else:
            text = event.text()
            if text and (text.isdigit() or text in ".-"):
                state.numeric += text
                self._update_paste_preview(state.last_mouse)
            else:
                return False
        return True

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._paste_key_press(event):
            return
        if not self._snapshot_active and self._edit_key_press(event):
            return
        super().keyPressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        current = event.position().toPoint()
        if self._paste_preview is not None:
            state = self._paste_preview
            if self._paste_left_pressed_at is not None \
                    and event.buttons() & Qt.MouseButton.LeftButton \
                    and time.monotonic() - self._paste_left_pressed_at >= 0.350:
                if not state.camera_inspect:
                    self._begin_paste_camera_inspect()
            if state.camera_inspect:
                delta = current - self._last_mouse
                self._last_mouse = current
                if event.buttons() & Qt.MouseButton.LeftButton:
                    self._yaw += delta.x() * 0.6
                    self._pitch = max(
                        -89.0, min(89.0, self._pitch + delta.y() * 0.6))
                    self._stabilize_paste_orbit_pivot()
                elif event.buttons() & Qt.MouseButton.MiddleButton:
                    self._pan += QPointF(delta.x(), delta.y())
                self._reanchor_paste_mouse(current)
                self.manualCameraChanged.emit()
                self.update()
            elif not event.buttons():
                self._update_paste_preview(current)
            else:
                self._last_mouse = current
            event.accept()
            return
        if self._edit_session is not None and not self._snapshot_active:
            if self._modal_op is not None:
                self._modal_last_mouse = current
                self._last_mouse = current
                self._update_modal(current)
                event.accept()
                return
            if not event.buttons():
                hovered = self._gizmo_axis_at(current)
                if hovered != self._gizmo_hover_axis:
                    self._gizmo_hover_axis = hovered
                    if hovered:
                        self.setCursor(Qt.CursorShape.PointingHandCursor)
                    else:
                        self.unsetCursor()
                    self.update()
            if self._box_start is not None:
                self._box_rect = QRect(self._box_start, current).normalized()
                self._last_mouse = current
                self.update()
                event.accept()
                return
            if self._vertex_press is not None \
                    and event.buttons() & Qt.MouseButton.LeftButton:
                pressed = self._vertex_press
                delta = current - pressed.position
                if math.hypot(delta.x(), delta.y()) >= 5.0:
                    if pressed.was_selected:
                        self._edit_session.selection = set(pressed.selection)
                    else:
                        self._edit_session.selection = {pressed.vertex}
                    self._emit_selection_hint("vertex")
                    self._begin_modal(
                        "grab" if self._direct_transform_mode == "move"
                        else self._direct_transform_mode,
                        pressed.position, direct_grab=True)
                    if self._modal_op is not None:
                        self._modal_last_mouse = current
                        self._update_modal(current)
                    self._vertex_press = None
                self._last_mouse = current
                event.accept()
                return
            if self._marquee_candidate \
                    and event.buttons() & Qt.MouseButton.LeftButton \
                    and self._press_pos is not None:
                delta = current - self._press_pos
                if abs(delta.x()) > 4 or abs(delta.y()) > 4:
                    self._box_start = self._press_pos
                    self._box_rect = QRect(
                        self._box_start, current).normalized()
                    self._marquee_candidate = False
                    self._last_mouse = current
                    self.update()
                event.accept()
                return
        delta = current - self._last_mouse
        self._last_mouse = current
        if event.buttons() & Qt.MouseButton.LeftButton:
            old_yaw = self._yaw
            old_pitch = self._pitch
            self._yaw += delta.x() * 0.6
            self._pitch = max(-89.0, min(89.0, self._pitch + delta.y() * 0.6))
            if self._yaw != old_yaw or self._pitch != old_pitch:
                self._camera_interacting = True
                self.manualCameraChanged.emit()
            self.update()
        elif event.buttons() & Qt.MouseButton.RightButton:
            if not delta.isNull():
                self._camera_interacting = True
                # Qt emits the CustomContextMenu request after the RMB gesture
                # on Windows.  Camera panning is a drag gesture, not a request
                # for the contextual menu, so consume that one generated menu
                # event.  A later plain RMB press clears this flag above.
                self._suppress_next_context_menu = True
                self._pan += QPointF(delta.x(), delta.y())
                self.manualCameraChanged.emit()
            self.update()
        event.accept()

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        factor = math.pow(1.0015, event.angleDelta().y())
        old_zoom = self._zoom
        self._zoom = max(0.08, min(30.0, self._zoom * factor))
        if self._zoom != old_zoom:
            self.manualCameraChanged.emit()
            self._reanchor_paste_mouse(
                event.position().toPoint()
                if self._paste_preview is not None else None)
        self.update()
        event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self.deselect_at(event.position().toPoint())
        event.accept()
