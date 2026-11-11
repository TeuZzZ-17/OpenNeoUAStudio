"""Native particle emitter selection, preview and undoable editing."""

from __future__ import annotations

from dataclasses import replace
from bisect import bisect_right
from itertools import accumulate
import copy

from PySide6.QtCore import QEvent, QObject, QPoint, QSignalBlocker, QTimer, Qt
from PySide6.QtGui import QCursor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QLabel, QMenu, QSpinBox,
)

from studio.assets.mapping import (
    MappingEditError, copy_particle_emitter, update_particle_emitter,
)
from studio.rendering.playback import PreviewClock
from studio.workbench.effect_catalog import (
    EffectCatalogDialog, FrameSequence, emitter_blocks, emitter_preview,
    emitter_stage_preview, emitter_texture_names, scan_catalog,
)


class _EmitterAnchorPick(QObject):
    def __init__(self, window, callback, preview=None):
        viewport = window.viewport
        super().__init__(viewport)
        self.viewport = viewport
        self.owner = viewport.edit_owner
        self.callback = callback
        self.tracking = viewport.hasMouseTracking()
        self.cursor = viewport.cursor()
        self.ghost = None
        if preview is not None and preview.images:
            self.ghost = QLabel(viewport)
            self.ghost.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            self.ghost.resize(preview.images[0].size())
            self.ghost_frames = FrameSequence(
                [self._gray_preview(image) for image in preview.images], preview.ends)
            self.ghost_clock = PreviewClock(lambda: viewport.animation_speed)
            self.ghost_timer = QTimer(self, interval=45)
            self.ghost_timer.timeout.connect(self._animate_ghost)
            self.ghost_timer.start()
            self._animate_ghost()
        viewport.setMouseTracking(True)
        viewport.setCursor(Qt.CursorShape.CrossCursor)
        viewport.installEventFilter(self)
        viewport.setFocus()
        position = viewport.mapFromGlobal(QCursor.pos())
        self._aim(position if viewport.rect().contains(position) else viewport.rect().center())

    @staticmethod
    def _gray_preview(image):
        gray = image.convertToFormat(QImage.Format.Format_Grayscale8).convertToFormat(
            QImage.Format.Format_ARGB32)
        gray.setAlphaChannel(image.convertToFormat(QImage.Format.Format_Alpha8))
        ghost = QImage(gray.size(), gray.format())
        ghost.fill(Qt.GlobalColor.transparent)
        painter = QPainter(ghost)
        painter.setOpacity(0.65)
        painter.drawImage(0, 0, gray)
        painter.end()
        return ghost

    def _animate_ghost(self):
        image = self.ghost_frames.at(self.ghost_clock.elapsed())
        self.ghost.setPixmap(QPixmap.fromImage(image))

    def _aim(self, position):
        viewport = self.viewport
        index = viewport._nearest_edit_vertex(position, radius=22)
        if index is not None and index > 32767:
            index = None
        viewport._emitter_anchor_hover = index
        if self.ghost is not None:
            if viewport.rect().contains(position):
                if index is not None:
                    position = viewport._edit_screen_points()[index].toPoint()
                self.ghost.move(position - QPoint(self.ghost.width() // 2,
                                                   self.ghost.height() // 2))
                self.ghost.show()
                self.ghost.raise_()
            else:
                self.ghost.hide()
        viewport.update()
        return index

    def finish(self, index=None):
        viewport = self.viewport
        viewport.removeEventFilter(self)
        viewport.setMouseTracking(self.tracking)
        viewport.setCursor(self.cursor)
        viewport._emitter_anchor_hover = None
        if self.ghost is not None:
            self.ghost_timer.stop()
            self.ghost.hide()
            self.ghost.deleteLater()
        viewport.update()
        self.deleteLater()
        if index is not None and viewport.edit_owner == self.owner:
            self.callback(index)

    def eventFilter(self, _obj, event):  # noqa: N802
        kind = event.type()
        if kind == QEvent.Type.FocusOut:
            self.finish()
            return False
        if kind in (QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress) \
                and event.key() == Qt.Key.Key_Escape:
            event.accept()
            if kind == QEvent.Type.KeyPress:
                self.finish()
            return True
        if kind == QEvent.Type.Leave:
            self.viewport._emitter_anchor_hover = None
            if self.ghost is not None:
                self.ghost.hide()
            self.viewport.update()
        if kind == QEvent.Type.MouseMove:
            self._aim(event.position().toPoint())
            return True
        if kind == QEvent.Type.MouseButtonPress and event.button() in (
                Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
            return True
        if kind == QEvent.Type.MouseButtonRelease:
            if event.button() == Qt.MouseButton.RightButton:
                self.viewport._suppress_next_context_menu = True
                self.finish()
                return True
            if event.button() == Qt.MouseButton.LeftButton:
                index = self._aim(event.position().toPoint())
                if index is not None:
                    self.finish(index)
                return True
        return False


class _EmittersMixin:
    def _setup_emitter_preview(self):
        self._emitter_clipboard = None
        self._emitter_sequence = None
        self._emitter_clock = PreviewClock(lambda: self.viewport.animation_speed)
        self._emitter_timer = QTimer(self, interval=65)
        self._emitter_timer.timeout.connect(self._draw_emitter_preview)

    def _selected_emitter(self):
        combo = getattr(self, "emitter_combo", None)
        if combo is None or self._workbench_obj is None:
            return None
        owner, index, identity = combo.currentData() or (None, -1, None)
        if owner != self._selected_owner or owner != self._workbench_obj.owner_path:
            return None
        blocks = self._workbench_obj.base_object.ades
        if 0 <= index < len(blocks) and id(blocks[index]) == identity:
            return index, blocks[index]
        return None

    def _refresh_emitters(self):
        if not hasattr(self, "emitter_combo"):
            return
        combo = self.emitter_combo
        previous = self._selected_emitter()
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("No emitter selected")
        obj = self._workbench_obj
        if obj is not None and obj.owner_path == self._selected_owner:
            for index, block in emitter_blocks(obj):
                names = ", ".join(emitter_texture_names(block)) or "No texture"
                combo.addItem(
                    f"Emitter {index} · Point {block.ade_point_id} | {names}",
                    (obj.owner_path, index, id(block)))
                if previous is not None and block is previous[1]:
                    combo.setCurrentIndex(combo.count() - 1)
        combo.setEnabled(combo.count() > 1)
        combo.blockSignals(False)
        selection = self._selected_emitter()
        if selection is None:
            self._clear_emitter_preview()
        else:
            self.viewport.set_selected_emitter((obj.owner_path, selection[0]))

    def _clear_emitter_selection(self):
        combo = getattr(self, "emitter_combo", None)
        if combo is None:
            return
        combo.blockSignals(True)
        combo.setCurrentIndex(0)
        combo.blockSignals(False)
        self._clear_emitter_preview()

    def _clear_emitter_preview(self):
        self.viewport.set_selected_emitter(None)
        self._emitter_timer.stop()
        self._emitter_sequence = None
        if getattr(self, "emitter_stage_combo", None) is not None \
                and not self.emitter_stage_combo.isHidden():
            self._uv_timeline = None
            self._uv_timeline_token = None
        stage_combo = getattr(self, "emitter_stage_combo", None)
        if stage_combo is not None:
            stage_combo.hide()
            self.uv_phase_combo.show()

    def _select_emitter_block(self, index):
        for row in range(1, self.emitter_combo.count()):
            if self.emitter_combo.itemData(row)[1] == index:
                self.emitter_combo.setCurrentIndex(row)
                return

    def _on_emitter_picked(self, owner, block_index):
        if self._selected_owner != owner:
            self._on_object_picked(owner)
        self._select_emitter_block(block_index)

    def _select_emitter_from_vertex(self, vertices):
        selected = set(vertices)
        if len(selected) == 1:
            anchor, = selected
            matches = [index for index, block in emitter_blocks(self._workbench_obj)
                       if block.ade_point_id == anchor]
            if matches:
                current = self._selected_emitter()
                self._select_emitter_block(current[0] if current and current[0] in matches else matches[0])
                return
        self._clear_emitter_selection()

    def _on_emitter_selected(self, _row):
        selection = self._selected_emitter()
        if selection is None:
            self._clear_emitter_selection()
            self._fill_polygon_inspector(self._selected_poly)
            self._sync_edit_action_states()
            return
        index, block = selection
        self.viewport.set_selected_emitter((self._selected_owner, index))
        # Mark the existing POO2 anchor without generating another pick.
        self._selected_poly = None
        self._selected_polys.clear()
        self._mirror_select_source_polys.clear()
        self.viewport.set_selected_polygon(None)
        self.viewport.set_highlight_polys(set())
        session = self.viewport.edit_session
        if session is not None and self.viewport.edit_owner == self._selected_owner:
            point = block.ade_point_id
            session.set_selection({point} if 0 <= point < len(session.model.points) else set())
            self.viewport.update()
        self._sync_poly_id_control()
        self.fx_combo.blockSignals(True)
        self.fx_combo.setCurrentIndex(0)
        self.fx_combo.blockSignals(False)
        self._update_uv_editor(None)
        self.load_texture_button.setEnabled(False)
        combo = self.emitter_stage_combo
        with QSignalBlocker(combo):
            combo.clear()
            combo.addItem("Particle emission", None)
            for index, stage in enumerate(block.particle_stages[:8]):
                name = stage.texture.name if stage.texture else "No texture"
                combo.addItem(f"Life stage {index + 1} · {name}", index)
            if combo.count() > 1:
                first_animated = next(
                    (index + 1 for index, stage in enumerate(block.particle_stages[:8])
                     if stage.texture and stage.texture.kind == "bmpanim"), 1)
                combo.setCurrentIndex(first_animated)
        combo.show()
        self.uv_phase_combo.show()
        self._show_emitter_stage()
        self._sync_edit_action_states()

    def _show_emitter_stage(self, _row=0):
        selection = self._selected_emitter()
        if selection is None:
            return
        _, block = selection
        stage_index = self.emitter_stage_combo.currentData()
        names = emitter_texture_names(block)
        self._uv_timeline = None
        self._emitter_sequence = None
        self._emitter_clock.start()
        try:
            if stage_index is None:
                sequence = emitter_preview(self._family, block)
            else:
                stage = block.particle_stages[stage_index]
                names = (stage.texture.name,) if stage.texture else ()
                entry = (self._animation_for_name(stage.texture.name)
                         if stage.texture and stage.texture.kind == "bmpanim" else None)
                if entry is not None and entry[1].frames:
                    ends = tuple(accumulate(max(1, frame.duration_ms)
                                            for frame in entry[1].frames))
                    self._show_uv_timeline(
                        *entry, self._workbench_obj, None,
                        lambda: bisect_right(ends, self._emitter_clock.elapsed() % ends[-1]))
                    self.model_texture_label.setText("Current Emitter Textures: " + ", ".join(names))
                    self._emitter_timer.start()
                    return
                sequence = emitter_stage_preview(self._family, stage)
        except (ValueError, IndexError, OverflowError, RuntimeError):
            sequence = FrameSequence([], ())
        self.model_texture_label.setText(
            "Current Emitter Textures: " + (", ".join(names) or "-"))
        self.poly_uv_label.setText(f"Emitter anchor: point {block.ade_point_id}")
        self._emitter_sequence = sequence
        self._uv_timeline_token = None
        self._emitter_clock.start()
        self._draw_emitter_preview()
        self._sync_uv_add_vertex_button()
        if len(self._emitter_sequence.images) > 1:
            self._emitter_timer.start()
        else:
            self._emitter_timer.stop()

    def _draw_emitter_preview(self):
        if getattr(self, "_uv_timeline", None) is not None:
            self._refresh_uv_timeline()
            return
        sequence = self._emitter_sequence
        if sequence is not None:
            image = sequence.at(self._emitter_clock.elapsed())
            self.uv_editor.set_loops(image, [], "Particle preview · native life stages"
                                     if image is not None else "No drawable particle stages")

    def _sync_emitter_actions(self):
        if not hasattr(self, "add_emitter_action") or not hasattr(self, "emitter_combo"):
            return
        can_add = not self._material_structure_reason()
        selection = self._selected_emitter()
        can_edit = can_add and selection is not None
        self.add_emitter_action.setEnabled(can_add)
        self.delete_emitter_action.setEnabled(can_edit)
        can_copy = can_edit and bool(selection[1].source_objt_bytes)
        self.clone_emitter_action.setEnabled(can_copy)
        self.copy_emitter_action.setEnabled(can_copy)
        self.cut_emitter_action.setEnabled(can_copy)
        clipboard = self._emitter_clipboard
        self.paste_emitter_action.setEnabled(bool(
            can_add and clipboard and clipboard[0] is self._family
            and clipboard[1] is self._workbench_obj
            and (not clipboard[3] or any(
                block is clipboard[2] for block in self._workbench_obj.base_object.ades))))
        self.reanchor_emitter_action.setEnabled(can_copy and bool(
            self._workbench_obj.skeleton.points))
        attrs = selection[1].particle_attributes if selection is not None else None
        self.edit_emitter_action.setEnabled(
            can_copy and attrs is not None and attrs.version == 1)

    def _show_emitter_context_menu(self, position):
        menu = QMenu(self)
        for action in (self.add_emitter_action, self.clone_emitter_action,
                       self.copy_emitter_action, self.cut_emitter_action, self.paste_emitter_action,
                       self.reanchor_emitter_action, self.edit_emitter_action,
                       self.delete_emitter_action):
            menu.addAction(action)
        menu.exec(self.emitter_combo.mapToGlobal(position))

    def _pick_emitter_anchor(self, callback, *, preview=None):
        points = self._workbench_obj.skeleton.points
        if not points:
            callback(0)
            return
        owner = self._workbench_obj

        def attach(index):
            if self._workbench_obj is owner and not self._material_structure_reason():
                callback(index)

        _EmitterAnchorPick(self, attach, preview)
        self._notify("Click a highlighted model point to attach. Esc or right click cancels.", 10000)

    def _edit_emitter_blocks(self, label, operation):
        try:
            index = self._apply_material_structure(label, operation)
        except MappingEditError as exc:
            self._notify(str(exc), 9000)
            return False
        if index is not None:
            self._select_emitter_block(index)
        self._notify(label, 4500)
        return True

    def _add_emitter(self):
        if self._material_structure_reason():
            return
        owner = self._workbench_obj
        dialog = EffectCatalogDialog(
            scan_catalog(self._family, "emitter", [owner]), self, kind="emitter",
            playback_speed=lambda: self.viewport.animation_speed,
            active_source=lambda: self._catalog_source("emitter"),
            external_source=lambda: self._catalog_source("emitter", external=True))
        if dialog.exec() != QDialog.DialogCode.Accepted or owner is not self._workbench_obj:
            return
        entry = dialog.selected_entry()
        if entry is None:
            return
        block = entry.owner.base_object.ades[entry.value]
        self._pick_emitter_anchor(lambda anchor: self._insert_emitter(
            entry.family, entry.owner, block, anchor, "Add emitter"),
            preview=emitter_preview(entry.family, block, sample_limit=48))

    def _insert_emitter(self, family, source, block, anchor, label):
        def operation():
            obj = self._workbench_obj
            if not obj.skeleton.points:
                obj.skeleton.points.append((0.0, 0.0, 0.0))
            copied = copy_particle_emitter(family, source, self._family, block, anchor)
            obj.base_object.ades.append(copied)
            return len(obj.base_object.ades) - 1
        self._edit_emitter_blocks(label, operation)

    def _clone_emitter(self):
        selection = self._selected_emitter()
        if selection is None or self._material_structure_reason():
            return
        _, block = selection
        self._pick_emitter_anchor(lambda anchor: self._insert_emitter(
            self._family, self._workbench_obj, block, anchor, "Clone emitter"),
            preview=emitter_preview(self._family, block, sample_limit=48))

    def _copy_emitter(self, cut=False):
        selection = self._selected_emitter()
        if selection is None or self._material_structure_reason():
            return
        block = selection[1] if cut else copy.deepcopy(selection[1])
        self._emitter_clipboard = (self._family, self._workbench_obj, block, cut)
        self._sync_emitter_actions()
        self._notify('Emitter ready to move' if cut else 'Emitter copied', 4500)

    def _cut_emitter(self):
        self._copy_emitter(cut=True)

    def _paste_emitter(self):
        clipboard = self._emitter_clipboard
        if not clipboard or not self.paste_emitter_action.isEnabled():
            return
        family, owner, block, cut = clipboard

        def paste(anchor):
            if self._emitter_clipboard is not clipboard or owner is not self._workbench_obj:
                return
            if cut and not any(item is block for item in owner.base_object.ades):
                return
            if cut:
                index = next(i for i, item in enumerate(owner.base_object.ades) if item is block)
                if anchor == block.ade_point_id:
                    return
                def operation():
                    owner.base_object.ades[index] = copy_particle_emitter(
                        family, owner, family, block, anchor)
                    return index
                if self._edit_emitter_blocks('Move emitter', operation):
                    self._emitter_clipboard = None
                    self._sync_emitter_actions()
            else:
                self._insert_emitter(family, owner, block, anchor, 'Paste emitter')

        self._pick_emitter_anchor(paste, preview=emitter_preview(family, block, sample_limit=48))

    def _reanchor_emitter(self):
        selection = self._selected_emitter()
        if selection is None or self._material_structure_reason():
            return
        index, block = selection
        def reanchor(anchor):
            if anchor == block.ade_point_id:
                return

            def operation():
                obj = self._workbench_obj
                obj.base_object.ades[index] = copy_particle_emitter(
                    self._family, obj, self._family, block, anchor)
                return index
            self._edit_emitter_blocks("Change emitter anchor", operation)

        self._pick_emitter_anchor(reanchor, preview=emitter_preview(
            self._family, block, sample_limit=48))

    def _edit_emitter_settings(self):
        selection = self._selected_emitter()
        if selection is None or self._material_structure_reason():
            return
        index, block = selection
        attrs = block.particle_attributes
        if attrs is None or attrs.version != 1:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Emitter Settings")
        form = QFormLayout(dialog)
        fields = {}
        for key, label in (
                ("start_speed", "Start speed"),
                ("context_lifetime", "Context lifetime (ticks)"),
                ("context_start_generation", "Emission start (ticks)"),
                ("context_stop_generation", "Emission stop (ticks)"),
                ("birth_rate", "Birth rate"),
                ("lifetime", "Particle lifetime (ticks)"),
                ("start_size", "Start size"),
                ("end_size", "End size"),
                ("noise", "Noise")):
            spin = QSpinBox()
            spin.setRange(min(0, getattr(attrs, key)), 2147483647)
            spin.setValue(getattr(attrs, key))
            form.addRow(label, spin)
            fields[key] = spin
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        changed = replace(attrs, **{key: spin.value() for key, spin in fields.items()})
        if changed == attrs:
            return
        if changed.context_stop_generation < changed.context_start_generation:
            self._notify("Emission stop must be at or after emission start.", 7000)
            return

        def operation():
            self._workbench_obj.base_object.ades[index] = update_particle_emitter(
                block, changed)
            return index
        self._edit_emitter_blocks("Edit emitter settings", operation)

    def _delete_emitter(self):
        selection = self._selected_emitter()
        if selection is None or self._material_structure_reason():
            return
        index, _block = selection

        def operation():
            self._clear_emitter_selection()
            self._workbench_obj.base_object.ades.pop(index)
        self._edit_emitter_blocks("Delete emitter", operation)
