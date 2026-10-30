"""CollisionEditorWindow mixin: collision spheres: add/generate/duplicate/mirror/delete, context menus,
selection and workspace overlays."""

from __future__ import annotations

import math

from PySide6.QtCore import (
    QItemSelectionModel,
    QPoint,
    QPointF,
    QSignalBlocker,
    Qt,
    QTimer,
)
from PySide6.QtWidgets import (
    QApplication,
    QMenu,
    QMessageBox,
)

from entity_designer.script_model import (
    CollisionProject,
    CollisionScriptError,
    CollisionSphere,
    fire_point_positions,
    GunPoint,
    import_turret_limits,
    LEGACY,
    OPENNEOUA,
    read_script_file,
    TurretLimits,
    TYPE_LABELS,
    VEHICLE,
)
from entity_designer.sphere_generator import (
    ACCURACY_PRESETS,
    generate_collision_spheres,
    UNIT_COLL_MAX_COUNT,
)
from entity_designer.widgets import (
    _GEOMETRY_COMPONENT_ROLE,
)
from studio.rendering.asset_viewport import (
    VIEW_PRESETS,
)
from entity_designer.editor_common import (
    _NO_HEIGHT_LIMIT,
    _SPHERE_INDEX_ROLE,
    _FIRE_POINT_INDEX_ROLE,
    _GUN_POINT_INDEX_ROLE,
)


class _SpheresMixin:
    """Part of CollisionEditorWindow; `self` is always the CollisionEditorWindow."""

    def add_legacy(self):
        self._push_undo()
        if self.project.legacy is None:
            sphere = self._default_sphere(LEGACY)
            sphere.x = sphere.y = sphere.z = 0.0
            self.project.legacy = sphere
        self._selected = 0
        self._selected_spheres = {0}
        self._selected_fire_point = -1
        self._selected_gun_point = -1
        self._set_modified()
        self.properties_tabs.setCurrentIndex(self.collision_tab_index)
        self._sync_all()

    def _ensure_compound_capacity(
            self, additional: int, operation: str) -> bool:
        """Keep authored compound collisions within the runtime safety cap."""

        available = max(0, UNIT_COLL_MAX_COUNT - len(self.project.compound))
        if additional <= available:
            return True
        self.statusBar().showMessage(
            f"{operation} would exceed the OpenNeoUA collision sphere limit "
            f"({len(self.project.compound)} / {UNIT_COLL_MAX_COUNT}; "
            f"{available} slot{'s' if available != 1 else ''} available).",
            6500)
        return False

    def add_compound(self, category: str):
        if not self._ensure_compound_capacity(1, "Add sphere"):
            return
        self._push_undo()
        self.project.compound.append(self._default_sphere(category))
        self._selected = len(self.project.spheres()) - 1
        self._selected_spheres = {self._selected}
        self._selected_fire_point = -1
        self._selected_gun_point = -1
        self._set_modified()
        self.properties_tabs.setCurrentIndex(self.collision_tab_index)
        self._sync_all()

    def generate_collision_spheres(self, preset_key: str):
        """Replace compound spheres with a classified physical-core approximation."""

        preset = next((
            candidate for candidate in ACCURACY_PRESETS
            if candidate.key == preset_key
        ), None)
        if preset is None:
            raise ValueError(
                f"Unsupported collision-sphere accuracy: {preset_key}")
        animated_indices = []
        triangles = self.viewport.local_owner_triangles(
            self._current_owner, animated_indices=animated_indices)
        if not triangles:
            QMessageBox.warning(
                self, "No model geometry",
                "Load and select a model with usable surface geometry first.")
            return

        if self.project.compound and not self._confirm_generated_replacement():
            return

        self.statusBar().showMessage(
            f"Generating collision spheres — {preset.label}")
        QApplication.processEvents()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = generate_collision_spheres(
                triangles, preset.key, max_spheres=UNIT_COLL_MAX_COUNT,
                animated_triangles=animated_indices)
        except ValueError as exc:
            QMessageBox.warning(
                self, "Collision sphere generation failed", str(exc))
            return
        finally:
            QApplication.restoreOverrideCursor()

        self._push_undo()
        self.project.compound = [
            CollisionSphere(
                OPENNEOUA, sphere.x, sphere.y, sphere.z, sphere.radius)
            for sphere in result.spheres
        ]
        self.project.sphere_isolation_active = False
        self._selected = 1 if self.project.legacy is not None else 0
        self._selected_spheres = {self._selected}
        self._selected_fire_point = -1
        self._selected_gun_point = -1
        self._set_modified()
        self.properties_tabs.setCurrentIndex(self.collision_tab_index)
        self._sync_all()
        cap_note = " — safety cap reached" if result.hit_safety_cap else ""
        symmetry_note = " — mirrored pairs" if result.symmetry_detected else ""
        coverage_note = (
            " — coverage target not reached"
            if result.measured_error > result.tolerance + 1e-6
            and not result.hit_safety_cap else "")
        self.statusBar().showMessage(
            f"Generated {len(result.spheres)} collision spheres — "
            f"{preset.label}{symmetry_note}{cap_note}{coverage_note}", 8000)

    def _confirm_generated_replacement(self) -> bool:
        confirmation = QMessageBox(self)
        confirmation.setIcon(QMessageBox.Icon.Warning)
        confirmation.setWindowTitle("Replace OpenNeoUA Collision Spheres")
        confirmation.setText(
            "Existing OpenNeoUA collision spheres will be replaced.\n"
            "The Legacy Radius will be preserved.\n\nContinue?")
        generate_button = confirmation.addButton(
            "Generate", QMessageBox.ButtonRole.AcceptRole)
        cancel_button = confirmation.addButton(
            "Cancel", QMessageBox.ButtonRole.RejectRole)
        confirmation.setDefaultButton(cancel_button)
        confirmation.exec()
        return confirmation.clickedButton() is generate_button

    def duplicate_sphere(self):
        entries = self._selected_sphere_entries()
        selected_compound = [
            sphere for _index, sphere in entries
            if sphere.category != LEGACY
        ]
        if not entries:
            return
        if not selected_compound:
            QMessageBox.information(
                self, "Single legacy radius",
                "A project can contain only one Legacy Radius.")
            return
        if not self._ensure_compound_capacity(
                len(selected_compound), "Duplicate selection"):
            return

        self._push_undo()
        selected_ids = {id(sphere) for sphere in selected_compound}
        duplicates = []
        rebuilt = []
        for sphere in self.project.compound:
            rebuilt.append(sphere)
            if id(sphere) in selected_ids:
                duplicate = sphere.clone()
                rebuilt.append(duplicate)
                duplicates.append(duplicate)
        self.project.compound[:] = rebuilt
        self._select_sphere_objects(duplicates)
        self._selected_fire_point = -1
        self._selected_gun_point = -1
        self._set_modified()
        self._sync_all()
        skipped = len(entries) - len(selected_compound)
        detail = " Legacy Radius was skipped." if skipped else ""
        self.statusBar().showMessage(
            f"Duplicated {len(duplicates)} selected collision sphere"
            f"{'s' if len(duplicates) != 1 else ''}.{detail}", 4500)

    def _populate_change_type_menu(self, menu: QMenu) -> None:
        menu.addAction(self.change_to_legacy_action)
        menu.addAction(self.change_to_openneoua_action)

    def _populate_generate_collision_spheres_menu(self, menu: QMenu) -> None:
        for preset in ACCURACY_PRESETS:
            menu.addAction(self.generate_collision_sphere_actions[preset.key])

    def change_sphere_type(self, target_category: str):
        """Convert the complete current sphere selection when valid."""

        if target_category not in (LEGACY, OPENNEOUA):
            raise ValueError(
                f"Unsupported collision category: {target_category}")
        entries = self._selected_sphere_entries()
        if not entries:
            return

        selected_spheres = [sphere for _index, sphere in entries]
        if target_category == LEGACY:
            if len(selected_spheres) != 1:
                self.statusBar().showMessage(
                    "Legacy Radius is singular: select exactly one sphere "
                    "before converting to Legacy Radius.", 5500)
                return
            sphere = selected_spheres[0]
            if sphere.category == LEGACY:
                return
            if self.project.legacy is not None:
                self.statusBar().showMessage(
                    "A project can contain only one Legacy Radius.", 5000)
                return
            compound_index = next((
                index for index, candidate in enumerate(self.project.compound)
                if candidate is sphere
            ), None)
            if compound_index is None:
                return
            self._push_undo()
            del self.project.compound[compound_index]
            sphere.category = LEGACY
            sphere.x = sphere.y = sphere.z = 0.0
            self.project.legacy = sphere
            self._select_sphere_objects([sphere])
            detail = (
                " Positional offsets were reset because vanilla radius "
                "has no offset.")
            changed_count = 1
        else:
            changing = [
                sphere for sphere in selected_spheres
                if sphere.category != target_category
            ]
            if not changing:
                return
            legacy = self.project.legacy
            converted_legacy = (
                legacy is not None
                and any(candidate is legacy for candidate in changing))
            if converted_legacy and not self._ensure_compound_capacity(
                    1, "Convert Legacy Radius"):
                return
            self._push_undo()
            if converted_legacy:
                self.project.legacy = None
                legacy.category = target_category
                # Legacy has no authored offset, so its existing origin is the
                # correct compound position when it becomes movable.
                self.project.compound.insert(0, legacy)
            for sphere in changing:
                if sphere is legacy:
                    continue
                sphere.category = target_category
            self._select_sphere_objects(selected_spheres)
            detail = ""
            changed_count = len(changing)

        self._selected_fire_point = -1
        self._selected_gun_point = -1
        self._set_modified()
        self._sync_all()
        label = TYPE_LABELS[target_category]
        self.statusBar().showMessage(
            f"Changed {changed_count} selected sphere"
            f"{'s' if changed_count != 1 else ''} to {label}.{detail}", 5000)

    def mirror_selected_sphere(self, axis: str):
        """Mirror every selected compound sphere across one model axis."""

        if axis not in ("x", "y", "z"):
            raise ValueError(f"Unsupported mirror axis: {axis}")
        entries = self._selected_sphere_entries()
        if not entries:
            return
        selected_compound = [
            sphere for _index, sphere in entries
            if sphere.category != LEGACY
        ]
        if not selected_compound:
            self.statusBar().showMessage(
                "Legacy Radius is fixed at the origin and cannot be mirrored.",
                5000)
            return
        if not self._ensure_compound_capacity(
                len(selected_compound), "Mirror selection"):
            return

        self._push_undo()
        selected_ids = {id(sphere) for sphere in selected_compound}
        mirrored_spheres = []
        rebuilt = []
        for sphere in self.project.compound:
            rebuilt.append(sphere)
            if id(sphere) in selected_ids:
                mirrored = sphere.clone()
                setattr(mirrored, axis, -getattr(mirrored, axis))
                rebuilt.append(mirrored)
                mirrored_spheres.append(mirrored)
        self.project.compound[:] = rebuilt
        self._select_sphere_objects(mirrored_spheres)
        self._selected_fire_point = -1
        self._selected_gun_point = -1
        self._set_modified()
        self._sync_all()
        skipped = len(entries) - len(selected_compound)
        detail = " Legacy Radius was skipped." if skipped else ""
        self.statusBar().showMessage(
            f"Mirrored {len(mirrored_spheres)} selected collision sphere"
            f"{'s' if len(mirrored_spheres) != 1 else ''} across the "
            f"{axis.upper()} axis.{detail}", 4500)

    def delete_sphere(self):
        entries = self._selected_sphere_entries()
        if not entries:
            return
        selected_indices = {index for index, _sphere in entries}
        selected_ids = {id(sphere) for _index, sphere in entries}
        anchor = min(selected_indices)

        self._push_undo()
        if self.project.legacy is not None \
                and id(self.project.legacy) in selected_ids:
            self.project.legacy = None
        self.project.compound[:] = [
            sphere for sphere in self.project.compound
            if id(sphere) not in selected_ids
        ]
        remaining = self.project.spheres()
        if self.project.sphere_isolation_active and (
                not remaining
                or not any(sphere.visible for sphere in remaining)
                or not any(not sphere.visible for sphere in remaining)):
            self.project.sphere_isolation_active = False
        count = len(self.project.spheres())
        self._selected = min(anchor, count - 1) if count else -1
        self._selected_spheres = (
            {self._selected} if self._selected >= 0 else set())
        self._selected_fire_point = -1
        self._selected_gun_point = -1
        self._set_modified()
        self._sync_all()
        self.statusBar().showMessage(
            f"Deleted {len(entries)} selected collision sphere"
            f"{'s' if len(entries) != 1 else ''}.", 4000)

    def delete_all_collisions(self):
        if not self.project.spheres():
            return
        result = QMessageBox.question(
            self, "Delete All Collisions",
            "Delete every Legacy, Vehicle and Weapon collision sphere?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if result != QMessageBox.StandardButton.Yes:
            return
        self._push_undo()
        self.project.legacy = None
        self.project.compound.clear()
        self.project.sphere_isolation_active = False
        self._selected = -1
        self._selected_spheres.clear()
        self._selected_fire_point = -1
        self._selected_gun_point = -1
        self._set_modified()
        self._sync_all()


    def _create_sphere_context_menu(self, index: int) -> QMenu:
        menu = QMenu(self)
        menu.addAction(self.undo_action)
        menu.addAction(self.redo_action)
        menu.addSeparator()
        menu.addAction(self.add_legacy_action)
        menu.addAction(self.add_openneoua_action)
        menu.addSeparator()
        self._context_action(
            menu, "Select All Spheres",
            lambda _checked=False: self.select_all_spheres(),
            bool(self.project.spheres()))
        menu.addAction(self.duplicate_action)
        menu.addAction(self.delete_action)
        entries = self._selected_sphere_entries()
        all_hidden = bool(entries) and all(
            not sphere.visible for _index, sphere in entries)
        visibility_text = (
            "Show Spheres" if all_hidden and len(entries) > 1
            else "Show Sphere" if all_hidden
            else "Hide Spheres" if len(entries) > 1
            else "Hide Sphere")
        self._context_action(
            menu, visibility_text, self._toggle_selected_sphere_visibility,
            bool(entries))
        spheres = self.project.spheres()
        self._context_action(
            menu, "Hide All Spheres",
            lambda _checked=False: self._set_all_sphere_visibility(False),
            any(sphere.visible for sphere in spheres))
        self._context_action(
            menu, "Show All Spheres",
            lambda _checked=False: self._set_all_sphere_visibility(True),
            any(not sphere.visible for sphere in spheres))
        if self.project.sphere_isolation_active:
            self._context_action(
                menu, "Unisolate Spheres" if len(
                    self._selected_sphere_indices()) > 1
                else "Unisolate Sphere",
                self._unisolate_sphere_visibility, bool(spheres))
        elif 0 <= index < len(spheres):
            selected = self._selected_sphere_indices()
            self._context_action(
                menu, "Isolate Spheres" if len(selected) > 1
                else "Isolate Sphere",
                self._toggle_selected_sphere_isolation)
        change_type_menu = menu.addMenu(
            "Change Sphere Types" if len(entries) > 1
            else "Change Sphere Type")
        self._populate_change_type_menu(change_type_menu)
        mirror_menu = menu.addMenu(
            "Mirror Selected Spheres" if len(entries) > 1
            else "Mirror Selected Sphere")
        mirror_menu.addAction(self.mirror_x_action)
        mirror_menu.addAction(self.mirror_y_action)
        mirror_menu.addAction(self.mirror_z_action)
        menu.addSeparator()
        menu.addAction(self.reset_view_action)
        menu.addSeparator()
        menu.addAction(self.open_base_action)
        preset_menu = menu.addMenu("View Preset")
        self._populate_view_preset_context_menu(preset_menu)
        return menu

    def _populate_view_preset_context_menu(self, menu: QMenu) -> None:
        """Populate the same read-only camera presets in every workspace menu."""

        for preset in VIEW_PRESETS:
            action = menu.addAction(preset)
            action.setCheckable(True)
            action.setChecked(
                preset == self.toolbar_view_preset_combo.currentText())
            action.triggered.connect(
                lambda _checked=False, value=preset:
                self.toolbar_view_preset_combo.setCurrentText(value))

    @staticmethod
    def _context_action(menu: QMenu, text: str, callback, enabled: bool = True):
        action = menu.addAction(text)
        action.setEnabled(enabled)
        action.triggered.connect(callback)
        return action

    def _add_workspace_context_tail(self, menu: QMenu, visibility_action=None) -> None:
        if visibility_action is not None:
            menu.addSeparator()
            menu.addAction(visibility_action)
        menu.addAction(self.reset_view_action)
        menu.addSeparator()
        menu.addAction(self.open_base_action)
        preset_menu = menu.addMenu("View Preset")
        self._populate_view_preset_context_menu(preset_menu)

    def _create_collision_shape_context_menu(self, component_key=None) -> QMenu:
        menu = QMenu(self)
        if component_key is not None:
            widget = self.collision_geometry_widget
            item = next((row for row in widget._items()
                         if row.data(0, _GEOMETRY_COMPONENT_ROLE) == component_key), None)
            if item is not None:
                if not item.isSelected():
                    widget.select_shape_component(component_key)
                else:
                    widget.tree.setCurrentItem(item, 0, QItemSelectionModel.SelectionFlag.NoUpdate)
                widget._add_component_toggle_action(menu, item)
                menu.addSeparator()
        self.collision_geometry_widget._add_component_deselect_action(menu)
        menu.addSeparator()
        menu.addAction(self.undo_action)
        menu.addAction(self.redo_action)
        menu.addSeparator()
        self._add_workspace_context_tail(menu)
        return menu

    def _create_fire_point_context_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.addAction(self.undo_action)
        menu.addAction(self.redo_action)
        menu.addSeparator()
        self._context_action(menu, "Add Fire Point", self.add_fire_point,
                             self.add_fire_point_button.isEnabled())
        self._context_action(
            menu, "Remove Selected Fire Point", self.remove_fire_point,
            self.remove_fire_point_button.isEnabled())
        self._context_action(
            menu, "Reset Position to 0 / 0 / 0",
            self._reset_fire_point_position,
            self.reset_fire_point_button.isEnabled())
        self._add_workspace_context_tail(
            menu, self.viewpoint_actions.get("Show Fire Points"))
        return menu

    def _create_gun_point_context_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.addAction(self.undo_action)
        menu.addAction(self.redo_action)
        menu.addSeparator()
        self._context_action(menu, "Add Gun Point", self.add_gun_point,
                             self.add_gun_point_button.isEnabled())
        self._context_action(
            menu, "Remove Selected Gun Point", self.remove_gun_point,
            self.remove_gun_point_button.isEnabled())
        self._context_action(
            menu, "Reset Selected Position to 0 / 0 / 0",
            self._reset_gun_point_position,
            self.reset_gun_point_button.isEnabled())
        self._context_action(
            menu, "Reset All", self._reset_all_gun_points,
            self.reset_all_gun_points_button.isEnabled())

        type_menu = menu.addMenu("Gun Point Type")
        for index in range(self.gun_point_type_combo.count()):
            text = self.gun_point_type_combo.itemText(index)
            data = self.gun_point_type_combo.itemData(index)
            action = type_menu.addAction(text)
            action.setCheckable(True)
            action.setChecked(data == self._new_gun_point_scheme)
            action.triggered.connect(
                lambda _checked=False, value=data:
                self.gun_point_type_combo.setCurrentIndex(
                    self.gun_point_type_combo.findData(value)))

        self._add_workspace_context_tail(
            menu, self.viewpoint_actions.get("Show Gun Points"))
        return menu

    def _create_cockpit_context_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.addAction(self.undo_action)
        menu.addAction(self.redo_action)
        menu.addSeparator()

        cockpit_enabled = (self.project.target_category == VEHICLE
                           and self.project.cockpit_camera_enabled)
        toggle_text = (
            "Disable Cockpit Camera Offset" if cockpit_enabled
            else "Enable Cockpit Camera Offset")
        self._context_action(
            menu, toggle_text, self._toggle_cockpit_camera,
            self.project.target_category == VEHICLE)
        self._context_action(
            menu, "Reset Position to 0 / 0 / 0", self._reset_cockpit_camera,
            cockpit_enabled)
        self._context_action(
            menu, "Restore Script Position",
            self._restore_loaded_cockpit_camera,
            self.restore_script_cockpit_button.isEnabled())

        model_menu = menu.addMenu("Runtime Model")
        for index in range(self.cockpit_model_state_combo.count()):
            text = self.cockpit_model_state_combo.itemText(index)
            data = self.cockpit_model_state_combo.itemData(index)
            action = model_menu.addAction(text)
            action.setCheckable(True)
            action.setChecked(
                data == self.cockpit_model_state_combo.currentData())
            action.triggered.connect(
                lambda _checked=False, value=data:
                self.cockpit_model_state_combo.setCurrentIndex(
                    self.cockpit_model_state_combo.findData(value)))

        aspect_menu = menu.addMenu("Runtime Aspect")
        for index in range(self.cockpit_runtime_aspect_combo.count()):
            text = self.cockpit_runtime_aspect_combo.itemText(index)
            data = self.cockpit_runtime_aspect_combo.itemData(index)
            action = aspect_menu.addAction(text)
            action.setCheckable(True)
            action.setChecked(
                data == self.cockpit_runtime_aspect_combo.currentData())
            action.triggered.connect(
                lambda _checked=False, value=data:
                self.cockpit_runtime_aspect_combo.setCurrentIndex(
                    self.cockpit_runtime_aspect_combo.findData(value)))

        menu.addSeparator()
        menu.addAction(self.open_base_action)
        return menu

    def _show_collision_shape_context_menu(self, local_pos: QPoint) -> None:
        self._create_collision_shape_context_menu().exec(
            self.collision_shape_box.mapToGlobal(local_pos))

    def _show_fire_points_context_menu(self, local_pos: QPoint) -> None:
        self._create_fire_point_context_menu().exec(
            self.fire_points_box.mapToGlobal(local_pos))

    def _show_gun_points_context_menu(self, local_pos: QPoint) -> None:
        self._create_gun_point_context_menu().exec(
            self.gun_points_box.mapToGlobal(local_pos))

    def _show_cockpit_context_menu(self, local_pos: QPoint) -> None:
        self._create_cockpit_context_menu().exec(
            self.cockpit_box.mapToGlobal(local_pos))

    def _show_fire_point_tree_context_menu(self, local_pos: QPoint) -> None:
        item = self.fire_point_tree.itemAt(local_pos)
        if item is not None:
            self.fire_point_tree.setCurrentItem(item)
        self._create_fire_point_context_menu().exec(
            self.fire_point_tree.viewport().mapToGlobal(local_pos))

    def _show_gun_point_tree_context_menu(self, local_pos: QPoint) -> None:
        item = self.gun_point_tree.itemAt(local_pos)
        if item is not None:
            self.gun_point_tree.setCurrentItem(item)
        self._create_gun_point_context_menu().exec(
            self.gun_point_tree.viewport().mapToGlobal(local_pos))

    def _create_fx_context_menu(self) -> QMenu:
        """Expose only existing destruction-preview controls in the FX viewport."""
        menu = QMenu(self)
        panel = self.fx_panel
        menu.addAction("Pause" if panel.is_playing() else "Play", panel.play_button.click)
        menu.addAction("Restart", panel.restart)
        menu.addAction("Stop", panel.stop)
        menu.addSeparator()
        menu.addAction(self.reset_view_action)
        return menu

    def _show_sphere_context_menu(self, index: int, global_pos: QPoint):
        if self._is_fx_tab_active():
            self._create_fx_context_menu().exec(global_pos)
            return
        if (self.properties_tabs.currentIndex()
                == self.collision_shape_tab_index):
            key = self.viewport._hit_shape_component(
                QPointF(self.viewport.mapFromGlobal(global_pos)))
            self._create_collision_shape_context_menu(key).exec(global_pos)
            return
        self._create_sphere_context_menu(index).exec(global_pos)

    def _show_sphere_tree_context_menu(self, local_pos: QPoint):
        item = self.sphere_tree.itemAt(local_pos)
        index = -1
        if item is not None:
            candidate = item.data(0, _SPHERE_INDEX_ROLE)
            if isinstance(candidate, int):
                index = candidate
                if index not in self._selected_sphere_indices():
                    self._select_sphere(index)
        self._show_sphere_context_menu(
            index, self.sphere_tree.viewport().mapToGlobal(local_pos))

    def _selected_sphere(self):
        spheres = self.project.spheres()
        return spheres[self._selected] if 0 <= self._selected < len(
            spheres) else None

    def _selected_sphere_indices(self) -> set[int]:
        count = len(self.project.spheres())
        selected = {
            index for index in self._selected_spheres
            if 0 <= index < count
        }
        if selected:
            if self._selected not in selected:
                self._selected = min(selected)
        elif 0 <= self._selected < count:
            selected = {self._selected}
        else:
            self._selected = -1
        self._selected_spheres = selected
        return set(selected)

    def _selected_sphere_entries(self) -> list[tuple[int, CollisionSphere]]:
        spheres = self.project.spheres()
        return [
            (index, spheres[index])
            for index in sorted(self._selected_sphere_indices())
            if 0 <= index < len(spheres)
        ]

    def _selected_spheres_with_common_radius(self) -> list[CollisionSphere]:
        """Return the selection only when every authored radius matches."""

        spheres = [sphere for _index, sphere in self._selected_sphere_entries()]
        if spheres and all(sphere.radius == spheres[0].radius
                           for sphere in spheres):
            return spheres
        return []

    def _select_sphere_objects(self, spheres) -> None:
        """Restore a selection by object identity after list mutations."""

        wanted = {id(sphere) for sphere in spheres}
        selected = {
            index for index, sphere in enumerate(self.project.spheres())
            if id(sphere) in wanted
        }
        self._selected_spheres = selected
        self._selected = min(selected, default=-1)

    def _selected_compound_index(self) -> int | None:
        if self._selected < 0:
            return None
        offset = 1 if self.project.legacy is not None else 0
        compound_index = self._selected - offset
        if 0 <= compound_index < len(self.project.compound):
            return compound_index
        return None

    def _select_sphere(self, index: int):
        self._radius_spin_active = False
        self._vehicle_preview_active_edits.clear()
        count = len(self.project.spheres())
        self._selected = index if 0 <= index < count else -1
        self._selected_spheres = (
            {self._selected} if self._selected >= 0 else set())
        self._selected_fire_point = -1
        self._selected_gun_point = -1
        if (0 <= index < len(self.project.spheres())
                and self.properties_tabs.currentIndex()
                != self.collision_tab_index):
            with QSignalBlocker(self.properties_tabs):
                self.properties_tabs.setCurrentIndex(self.collision_tab_index)
        self._sync_all()

    def _toggle_sphere(self, index: int):
        self._radius_spin_active = False
        self._vehicle_preview_active_edits.clear()
        count = len(self.project.spheres())
        if not (0 <= index < count):
            return
        selected = self._selected_sphere_indices()
        if index in selected:
            selected.remove(index)
            self._selected = min(selected, default=-1)
        else:
            selected.add(index)
            self._selected = index
        self._selected_spheres = selected
        self._selected_fire_point = -1
        self._selected_gun_point = -1
        if (selected and self.properties_tabs.currentIndex()
                != self.collision_tab_index):
            with QSignalBlocker(self.properties_tabs):
                self.properties_tabs.setCurrentIndex(self.collision_tab_index)
        self._sync_all()

    def _select_sphere_box(self, indices, additive: bool = True):
        self._radius_spin_active = False
        self._vehicle_preview_active_edits.clear()
        count = len(self.project.spheres())
        hits = {
            int(index) for index in indices
            if isinstance(index, int) and 0 <= index < count
        }
        selected = self._selected_sphere_indices() if additive else set()
        selected |= hits
        self._selected_spheres = selected
        if hits:
            self._selected = min(hits)
        elif self._selected not in selected:
            self._selected = min(selected, default=-1)
        self._selected_fire_point = -1
        self._selected_gun_point = -1
        if (selected and self.properties_tabs.currentIndex()
                != self.collision_tab_index):
            with QSignalBlocker(self.properties_tabs):
                self.properties_tabs.setCurrentIndex(self.collision_tab_index)
        self._sync_all()

    def select_all_spheres(self):
        count = len(self.project.spheres())
        if count <= 0:
            return
        selected = set(range(count))
        self._selected_spheres = selected
        if self._selected not in selected:
            self._selected = 0
        self._selected_fire_point = -1
        self._selected_gun_point = -1
        if self.properties_tabs.currentIndex() != self.collision_tab_index:
            with QSignalBlocker(self.properties_tabs):
                self.properties_tabs.setCurrentIndex(self.collision_tab_index)
        self._sync_all()

    def _select_fire_point(self, index: int):
        self._radius_spin_active = False
        self._vehicle_preview_active_edits.clear()
        point_count = len(fire_point_positions(self.project))
        self._selected_fire_point = (
            index if (self.project.fire_points_enabled
                      and 0 <= index < point_count) else -1)
        self._selected_gun_point = -1
        self._selected = -1
        self._selected_spheres.clear()
        if (self._selected_fire_point >= 0
                and self.properties_tabs.currentIndex()
                != self.fire_points_tab_index):
            # Viewport picks are domain navigation too: jump directly to the
            # controls for the selected muzzle without triggering a redundant
            # currentChanged sync before the normal _sync_all below.
            with QSignalBlocker(self.properties_tabs):
                self.properties_tabs.setCurrentIndex(
                    self.fire_points_tab_index)
        self._sync_all()

    def _selected_gun(self) -> GunPoint | None:
        index = self._selected_gun_point
        if (self.project.gun_points_enabled
                and 0 <= index < len(self.project.gun_points)):
            return self.project.gun_points[index]
        return None

    def _selected_turret_limits(self) -> TurretLimits | None:
        gun = self._selected_gun()
        if gun is None:
            return None
        return self.project.turret_limits.get(int(gun.gun_type))

    def _resolve_referenced_turret_limits(self, vehicle_id: int) -> TurretLimits | None:
        """Resolve a mount's Vehicle through the same catalog as the browser."""
        existing = self.project.turret_limits.get(vehicle_id)
        if existing is not None:
            return existing
        obj = next((item for item in self.game_object_browser.objects
                    if item.kind == "vehicle" and item.object_id == vehicle_id), None)
        if obj is None:
            return None
        try:
            limits = import_turret_limits(obj.effective_text).get(vehicle_id)
            if limits is None:
                return None
            # Effective catalog text is a synthetic new_* block. Saving must
            # retain the actual new/modify block kind in the source file.
            source_text, _encoding, _bom = read_script_file(obj.source)
            source_limits = import_turret_limits(source_text).get(vehicle_id)
            if source_limits is not None:
                limits.source_kind = source_limits.source_kind
        except (OSError, UnicodeError, CollisionScriptError):
            return None
        self.project.turret_limits[vehicle_id] = limits
        self._loaded_turret_limits[vehicle_id] = limits.clone()
        # Reading a referenced definition is not an authored change.
        if self._tab_reset_baseline is not None:
            baseline = CollisionProject()
            baseline.restore(self._tab_reset_baseline)
            baseline.turret_limits[vehicle_id] = limits.clone()
            self._tab_reset_baseline = baseline.snapshot()
        return limits

    def _select_gun_point(self, index: int):
        self._radius_spin_active = False
        self._vehicle_preview_active_edits.clear()
        self._selected_gun_point = (
            index if (self.project.gun_points_enabled
                      and 0 <= index < len(self.project.gun_points)) else -1)
        selected = self._selected_gun()
        if selected is not None and selected.scheme in {"robo", "unit"}:
            # Selection only changes the default family for the *next* mount.
            # The selected point itself is never converted implicitly.
            self._new_gun_point_scheme = selected.scheme
        self._selected_fire_point = -1
        self._selected = -1
        self._selected_spheres.clear()
        if (self._selected_gun_point >= 0
                and self.properties_tabs.currentIndex()
                != self.gun_points_tab_index):
            with QSignalBlocker(self.properties_tabs):
                self.properties_tabs.setCurrentIndex(
                    self.gun_points_tab_index)
        self._sync_all()


    def _fire_point_tree_selection_changed(self, current, _previous):
        if self._syncing or current is None:
            return
        index = current.data(0, _FIRE_POINT_INDEX_ROLE)
        if isinstance(index, int):
            self._select_fire_point(index)


    def _gun_point_tree_selection_changed(self, current, _previous):
        if self._syncing or current is None:
            return
        index = current.data(0, _GUN_POINT_INDEX_ROLE)
        if isinstance(index, int):
            self._select_gun_point(index)


    def _sphere_tree_selection_changed(self):
        if self._syncing:
            return
        selected: set[int] = set()
        for item in self.sphere_tree.selectedItems():
            index = item.data(0, _SPHERE_INDEX_ROLE)
            if isinstance(index, int):
                selected.add(index)
        current = self.sphere_tree.currentItem()
        primary = -1
        if current is not None:
            index = current.data(0, _SPHERE_INDEX_ROLE)
            if isinstance(index, int) and index in selected:
                primary = index
        if primary < 0:
            primary = min(selected, default=-1)
        self._radius_spin_active = False
        self._vehicle_preview_active_edits.clear()
        self._selected_spheres = selected
        self._selected = primary
        self._selected_fire_point = -1
        self._selected_gun_point = -1
        self._sync_all()

    def _move_strength_slider_changed(self, value: int):
        with QSignalBlocker(self.move_strength_spin):
            self.move_strength_spin.setValue(value)

    def _move_strength_spin_changed(self, value: int):
        # A typed value may exceed the normal quick-slider range. Expand only
        # while necessary, then restore the useful fine-control range.
        with QSignalBlocker(self.move_strength_slider):
            self.move_strength_slider.setMaximum(max(500, value))
            self.move_strength_slider.setValue(value)

    def _model_preview_scale_changed(self, *_args):
        if self._syncing:
            return
        values = (
            self.model_scale_x_spin.value(),
            self.model_scale_y_spin.value(),
            self.model_scale_z_spin.value(),
        )
        current = (
            self.project.model_scale_x,
            self.project.model_scale_y,
            self.project.model_scale_z,
        )
        if all(abs(a - b) < 1e-9 for a, b in zip(values, current)):
            return
        self._push_undo()
        (self.project.model_scale_x,
         self.project.model_scale_y,
         self.project.model_scale_z) = values
        self.viewport.set_model_preview_scale(*values)
        self._set_modified()
        self._sync_all()

    def _reset_model_preview_scale(self):
        current = (
            self.project.model_scale_x,
            self.project.model_scale_y,
            self.project.model_scale_z,
        )
        if all(abs(value - 1.0) < 1e-9 for value in current):
            return
        self._push_undo()
        self.project.model_scale_x = 1.0
        self.project.model_scale_y = 1.0
        self.project.model_scale_z = 1.0
        self.viewport.set_model_preview_scale(1.0, 1.0, 1.0)
        self._set_modified()
        self._sync_all()

    def _overeof_enabled_changed(self, enabled: bool):
        if self._syncing or self.project.target_category != VEHICLE:
            return
        enabled = bool(enabled)
        if enabled == self.project.overeof_enabled:
            return
        self._overeof_spin_active = False
        self._push_undo()
        self.project.overeof_enabled = enabled
        self._set_modified()
        self._sync_all()

    def _overeof_value_changed(self, value: float):
        if self._syncing or self.project.target_category != VEHICLE:
            return
        value = float(value)
        if not math.isfinite(value) or abs(value - self.project.overeof) < 1e-9:
            return
        if not self._overeof_spin_active:
            self._push_undo()
            self._overeof_spin_active = True
        self.project.overeof = value
        self.project.overeof_enabled = True
        self._set_modified()
        self._sync_all()

    def _finish_overeof_edit(self):
        self._overeof_spin_active = False

    def _suggest_overeof_from_bounds(self):
        bounds = self._model_bounds()
        if bounds is None:
            QMessageBox.warning(
                self, "No model", "Load and select a model first.")
            return
        # OpenNeoUA places the actor origin at ground_y - overeof.  In the
        # model convention used by UA the lowest point is the maximum local Y.
        suggested = float(bounds[4])
        if (self.project.overeof_enabled
                and abs(self.project.overeof - suggested) < 1e-9):
            return
        self._overeof_spin_active = False
        self._push_undo()
        self.project.overeof = suggested
        self.project.overeof_enabled = True
        self._set_modified()
        self._sync_all()
        self.statusBar().showMessage(
            "Suggested Overeof uses the lowest scaled model point; verify "
            "the final terrain contact in-game.", 7000)

    def _reset_overeof(self):
        if (not self.project.overeof_enabled
                and abs(self.project.overeof) < 1e-9):
            return
        self._overeof_spin_active = False
        self._push_undo()
        self.project.overeof_enabled = False
        self.project.overeof = 0.0
        self._set_modified()
        self._sync_all()

    def _sync_workspace_overlay_visibility(self, *_args) -> None:
        """Show only overlays owned by the active properties workspace.

        Viewpoint menu actions remain per-overlay user preferences, but they
        cannot make an overlay leak into another tab. Model/textures stay
        global because they are the common editing reference for every tab.
        """

        if not (hasattr(self, "properties_tabs")
                and hasattr(self, "viewpoint_actions")):
            return

        active = self.properties_tabs.currentIndex()
        collision_tab = active == self.collision_tab_index
        ground_tab = active == self.ground_tab_index
        shape_tab = active == self.collision_shape_tab_index
        fire_tab = active == self.fire_points_tab_index
        gun_tab = active == self.gun_points_tab_index
        fire_area_tab = self._is_fire_area_tab_active()

        def wanted(label: str) -> bool:
            action = self.viewpoint_actions.get(label)
            return action is None or action.isChecked()

        self.viewport.set_collision_category_visible(
            LEGACY, collision_tab and wanted("Show Legacy Radius"))
        self.viewport.set_collision_category_visible(
            OPENNEOUA, collision_tab and wanted("Show OpenNeoUA Collisions"))
        # The ground plane is also the reference surface of Destruction FX:
        # wrecks, fragments and decals land on it.
        # The fire volume is judged against the floor, so it shows the ground too.
        self.viewport.set_ground_simulation_visible(
            (collision_tab or ground_tab or fire_area_tab
             or self._is_fx_tab_active())
            and wanted("Show Ground Simulation"))
        self.viewport.set_fire_area_visible(fire_area_tab)
        self.viewport.set_overeof_visible(
            ground_tab and wanted("Show Overeof"))
        self.viewport.set_collision_shape_overlay_visible(shape_tab)

        self.viewport.set_fire_points_visible(
            fire_tab and wanted("Show Fire Points"))

        self.viewport.set_gun_points_visible(
            gun_tab and wanted("Show Gun Points"))
        self.viewport.set_turret_limits_visible(
            gun_tab and wanted("Show Turret Limits"))

    def _is_cockpit_tab_selected(self) -> bool:
        return (hasattr(self, "properties_tabs")
                and self.properties_tabs.currentIndex()
                == self.cockpit_tab_index
                and self.project.target_category == VEHICLE)

    def _is_cockpit_tab_active(self) -> bool:
        # Entering the Cockpit tab is a pure viewport switch: the runtime-style
        # preview shows right away and the orbit camera state is never touched,
        # so leaving the tab restores the exact previous editor view.
        return self._is_cockpit_tab_selected()

    def _fit_cockpit_tab_height(self) -> None:
        """Keep the shared gizmo right under the Cockpit View rows.

        That tab has only a few rows, so letting it fill the column would leave a
        large empty gap above the gizmo. Its height is capped to its content and
        the free space moves below the gizmo. Other tabs keep the full height.
        """

        tabs = self.properties_tabs
        # The button strip and the tabs share one container that holds their slot in
        # the column: the container, not the tabs, is what must be capped.
        strip = getattr(tabs, "_palette_strip", None)
        slot = strip.container if strip is not None and strip.container is not None else tabs
        on_cockpit = tabs.currentIndex() == self.cockpit_tab_index
        if on_cockpit and not slot.isVisible():
            return  # Not laid out yet: the sizes below would be meaningless.
        if on_cockpit:
            # Everything around the page (button strip, spacing, frame) plus the page rows.
            chrome = slot.height() - self.cockpit_tab.height()
            wanted = chrome + self.cockpit_tab_layout.sizeHint().height()
        slot.setMaximumHeight(wanted if on_cockpit else _NO_HEIGHT_LIMIT)
        self._properties_layout.setStretch(self._properties_tail_index, int(on_cockpit))

    def _properties_tab_changed(self, _index: int) -> None:
        # Changing workspace must never modify authored data. It only switches
        # the viewport presentation and which object the shared gizmo moves.
        QTimer.singleShot(0, self._fit_cockpit_tab_height)
        briefing_active = self.properties_tabs.currentIndex() == self.briefing_tab_index
        self.renderer_badge.set_viewport(self.briefing_preview if briefing_active else self.viewport)
        self.briefing_preview.setVisible(briefing_active)
        self.viewport.setVisible(not briefing_active)
        if briefing_active:
            self.briefing_preview.refresh()
        cockpit_selected = self._is_cockpit_tab_selected()
        cockpit_active = self._is_cockpit_tab_active()
        shape_tab_active = (
            hasattr(self, "collision_shape_tab_index")
            and self.properties_tabs.currentIndex()
            == self.collision_shape_tab_index)
        self._sync_workspace_overlay_visibility()
        self._fire_area_enter_leave()
        self.viewport.set_cockpit_preview_active(cockpit_active)
        if hasattr(self, "toolbar_view_preset_combo"):
            self.toolbar_view_preset_combo.setEnabled(not cockpit_active)
        self._update_reset_view_controls()
        if hasattr(self, "transform_box"):
            self.transform_box.setVisible(
                not shape_tab_active and not briefing_active
                and self.properties_tabs.currentIndex() != self.ground_tab_index
                and self.properties_tabs.currentIndex() != self.wireframe_tab_index
                and not self._is_fx_tab_active()
                and self.properties_tabs.currentIndex() != self.fire_area_tab_index)
            self.transform_box.setTitle(
                "Move Cockpit Camera" if cockpit_selected
                else "Move Fire Area Grip" if self._is_fire_area_tab_active()
                else "Move Element")
        if hasattr(self, "cockpit_runtime_aspect_combo"):
            self.viewport.set_cockpit_runtime_aspect(
                self.cockpit_runtime_aspect_combo.currentData())
        # Restore the unit-only viewport scope before Cockpit View loads its
        # own preview model.
        self._sync_fx_preview()
        self._sync_cockpit_preview_model()
        self._sync_gizmo_camera()
        if cockpit_active:
            self.viewport.setFocus(Qt.FocusReason.OtherFocusReason)
        if not self._syncing:
            self._sync_all()
        tree = {
            self.fire_points_tab_index: self.fire_point_tree,
            self.gun_points_tab_index: self.gun_point_tree,
        }.get(_index)
        if tree is not None and tree.topLevelItemCount():
            tree.clearSelection()
            tree.setCurrentItem(tree.topLevelItem(0))
