"""CollisionEditorWindow mixin: edit controls: gun point values, turret limits, gizmo nudge, sphere
visibility/isolation, radius slider and tree refresh."""

from __future__ import annotations

import math

from PySide6.QtCore import (
    QSignalBlocker,
    Qt,
)
from PySide6.QtGui import (
    QBrush,
)
from PySide6.QtWidgets import (
    QTreeWidgetItem,
)

from entity_designer.script_model import (
    _clamp_gun_angle,
    _fire_point_preview_count,
    _num_weapons_text,
    _number,
    _radius_number,
    _sphere_number,
    CollisionSphere,
    effective_runtime_radius,
    fire_point_positions,
    gun_direction_index,
    GUN_DIRECTION_SEQUENCE,
    LEGACY,
    OPENNEOUA,
    TYPE_LABELS,
    VEHICLE,
    WEAPON,
)
from entity_designer.widgets import (
    FIRE_POINT_COLOR,
    GUN_POINT_COLOR,
    TYPE_COLORS,
)
from entity_designer.editor_common import (
    _SPHERE_INDEX_ROLE,
    _FIRE_POINT_INDEX_ROLE,
    _GUN_POINT_INDEX_ROLE,
)


class _EditControlsMixin:
    """Part of CollisionEditorWindow; `self` is always the CollisionEditorWindow."""

    def _reset_all_gun_points(self) -> None:
        """Restore all mounts and turret limits captured from the script."""

        if self.project.target_category != VEHICLE:
            return
        selected_index = self._selected_gun_point
        baseline = [point.clone() for point in self._loaded_gun_points]
        current = [point.clone() for point in self.project.gun_points]
        same_points = (current == baseline)
        baseline_limits = {
            vehicle_id: limits.clone()
            for vehicle_id, limits in self._loaded_turret_limits.items()
        }
        if (same_points
                and self.project.turret_limits == baseline_limits
                and self.project.gun_points_enabled
                    == self._loaded_gun_points_enabled
                and self.project.unit_gun_default_icon
                    == self._loaded_unit_gun_default_icon):
            return
        self._vehicle_preview_active_edits.clear()
        self._push_undo()
        self.project.gun_points_enabled = self._loaded_gun_points_enabled
        self.project.gun_points = baseline
        self.project.unit_gun_default_icon = (
            self._loaded_unit_gun_default_icon)
        self.project.turret_limits = baseline_limits
        self._new_gun_point_scheme = self._loaded_gun_point_scheme
        self._selected_gun_point = (
            selected_index
            if 0 <= selected_index < len(baseline)
            else -1)
        self._set_modified()
        self._sync_all()

    def _reset_gun_point_position(self) -> None:
        """Reset only the selected Gun Point translation to the origin."""

        point = self._selected_gun()
        if point is None or point.position == (0.0, 0.0, 0.0):
            return
        self._vehicle_preview_active_edits.clear()
        self._push_undo()
        point.x = 0.0
        point.y = 0.0
        point.z = 0.0
        self.project.gun_points_enabled = True
        self._set_modified()
        self._sync_all()

    def _gun_point_value_changed(self, field: str, value: float) -> None:
        if self._syncing or self.project.target_category != VEHICLE:
            return
        point = self._selected_gun()
        if point is None or not hasattr(point, field):
            return
        value = float(value)
        if not math.isfinite(value):
            return
        if abs(float(getattr(point, field)) - value) < 1e-9:
            return
        self._begin_vehicle_preview_edit(f"gun_{field}")
        setattr(point, field, value)
        self.project.gun_points_enabled = True
        self._set_modified()
        self._sync_all()

    def _begin_gun_direction_slider(self) -> None:
        point = self._selected_gun()
        if point is None:
            return
        self._vehicle_preview_active_edits.clear()
        self._push_undo()
        self._gun_direction_slider_active = True
        if gun_direction_index(point.dir_x, point.dir_z) is None:
            point.dir_x, point.dir_z = GUN_DIRECTION_SEQUENCE[
                self.gun_direction_slider.value()]
            self.project.gun_points_enabled = True
            self._set_modified()
            self._sync_all()

    def _finish_gun_direction_slider(self) -> None:
        self._gun_direction_slider_active = False

    def _gun_direction_slider_changed(self, index: int) -> None:
        if self._syncing or self.project.target_category != VEHICLE:
            return
        point = self._selected_gun()
        if point is None:
            return
        index = max(0, min(len(GUN_DIRECTION_SEQUENCE) - 1, int(index)))
        dir_x, dir_z = GUN_DIRECTION_SEQUENCE[index]
        if (abs(float(point.dir_x) - dir_x) < 1e-9
                and abs(float(point.dir_z) - dir_z) < 1e-9):
            return
        if not self._gun_direction_slider_active:
            self._vehicle_preview_active_edits.clear()
            self._push_undo()
        point.dir_x = dir_x
        point.dir_z = dir_z
        self.project.gun_points_enabled = True
        self._set_modified()
        self._sync_all()

    def _begin_turret_limit_slider(self, field: str) -> None:
        limits = self._selected_turret_limits()
        if limits is None:
            return
        self._vehicle_preview_active_edits.discard(f"turret_{field}")
        self._push_undo()
        self._turret_slider_active_edits.add(field)

    def _finish_turret_limit_slider(self, field: str) -> None:
        self._turret_slider_active_edits.discard(field)

    def _turret_limit_value_changed(
            self, field: str, value: int, *, from_slider: bool = False) -> None:
        if self._syncing or self.project.target_category != VEHICLE:
            return
        limits = self._selected_turret_limits()
        if limits is None or field not in {"side", "up", "down"}:
            return
        value = _clamp_gun_angle(field, value)
        if int(getattr(limits, field)) == value:
            return
        if from_slider:
            if field not in self._turret_slider_active_edits:
                self._push_undo()
        else:
            self._begin_vehicle_preview_edit(f"turret_{field}")
        setattr(limits, field, value)
        limits.enabled = True
        limits.dirty = True
        self._set_modified()
        self._sync_all()

    def _gun_type_changed(self, value: int) -> None:
        if self._syncing or self.project.target_category != VEHICLE:
            return
        point = self._selected_gun()
        if point is None:
            return
        value = max(0, min(255, int(value)))
        if point.gun_type == value:
            return
        self._begin_vehicle_preview_edit("gun_type")
        point.gun_type = value
        self.project.gun_points_enabled = True
        self._set_modified()
        self._sync_all()

    def _gun_name_changed(self) -> None:
        if self._syncing or self.project.target_category != VEHICLE:
            return
        point = self._selected_gun()
        if point is None:
            return
        value = self.gun_name_edit.text().strip()
        if point.name == value:
            return
        self._push_undo()
        point.name = value
        self.project.gun_points_enabled = True
        self._set_modified()
        self._sync_all()


    def _gizmo_nudge(self, direction):
        if self._is_cockpit_tab_active():
            self._cockpit_nudge(direction)
            return
        step = float(self.move_strength_spin.value())
        active_tab = self.properties_tabs.currentIndex()

        if active_tab == self.fire_area_tab_index:
            self._fire_area_nudge(direction, step)
            return

        if active_tab == self.collision_tab_index:
            spheres = self.project.spheres()
            selected_indices = self._selected_sphere_indices()
            selected_spheres = [
                spheres[index] for index in sorted(selected_indices)
                if 0 <= index < len(spheres)
            ]
            if not selected_spheres:
                return
            movable = [
                sphere for sphere in selected_spheres
                if sphere.category != LEGACY
            ]
            if not movable:
                self.statusBar().showMessage(
                    "Legacy Radius has no script offset and remains at origin.",
                    5000)
                return
            self._push_undo()
            for sphere in movable:
                sphere.x += direction[0] * step
                sphere.y += direction[1] * step
                sphere.z += direction[2] * step
            self._set_modified()
            self._sync_all()
            if len(movable) > 1:
                self.statusBar().showMessage(
                    f"Moved {len(movable)} selected collision spheres.", 3000)
            return

        if active_tab == self.gun_points_tab_index:
            gun = self._selected_gun()
            if gun is None:
                return
            self._push_undo()
            gun.x += float(direction[0]) * step
            gun.y += float(direction[1]) * step
            gun.z += float(direction[2]) * step
            self.project.gun_points_enabled = True
            self._set_modified()
            self._sync_all()
            return

        if active_tab != self.fire_points_tab_index:
            return

        index = self._selected_fire_point
        points = fire_point_positions(self.project)
        if not (self.project.fire_points_enabled
                and 0 <= index < len(points)):
            return

        dx = float(direction[0]) * step
        dy = float(direction[1]) * step
        dz = float(direction[2]) * step
        count = _fire_point_preview_count(self.project)
        factor = None
        if dx and count > 1:
            factor = -1.0 + (2.0 * index / (count - 1))
            if abs(factor) <= 1e-9 and not (dy or dz):
                self.statusBar().showMessage(
                    "The center Fire Point of an odd vanilla rack is "
                    "locked to X = 0. Select another point to change "
                    "the symmetric spacing.", 6500)
                return

        self._push_undo()
        changed = False
        if dy:
            self.project.fire_y += dy
            changed = True
        if dz:
            self.project.fire_z += dz
            changed = True
        if dx:
            if count == 1:
                self.project.fire_x += dx
                changed = True
            elif factor is not None and abs(factor) <= 1e-9:
                self.statusBar().showMessage(
                    "The center Fire Point of an odd vanilla rack is "
                    "locked to X = 0. Y/Z movement still moved the full "
                    "rack.", 6500)
            else:
                current_x = factor * abs(float(self.project.fire_x))
                new_extent = max(0.0, (current_x + dx) / factor)
                sign = -1.0 if self.project.fire_x < 0.0 else 1.0
                self.project.fire_x = sign * new_extent
                changed = True
                self.statusBar().showMessage(
                    "Vanilla multi-weapon Fire Points stay symmetric; "
                    "X movement changed the spacing of the full rack.",
                    5000)
        if not changed:
            return
        self.project.fire_points_enabled = True
        self._set_modified()
        self._sync_all()


    def _new_gun_point_scheme_changed(self, _index: int) -> None:
        if self._syncing:
            return
        scheme = self.gun_point_type_combo.currentData()
        if scheme not in {"robo", "unit"}:
            return

        selected = self._selected_gun()
        if selected is None:
            self._new_gun_point_scheme = scheme
            return

        if selected.scheme == scheme:
            self._new_gun_point_scheme = scheme
            return

        # Each runtime family has its own 20-slot array. Converting an
        # existing mount must therefore respect the same limit as Add Gun
        # Point instead of generating an invalid script on export.
        target_count = sum(
            point.scheme == scheme for point in self.project.gun_points)
        if target_count >= 20:
            self.statusBar().showMessage(
                "Cannot convert Gun Point: target family already has 20 "
                "mounts.", 5000)
            self._sync_all()
            return

        self._push_undo()
        old_scheme = selected.scheme
        selected.scheme = scheme
        if (old_scheme == "unit" and scheme == "robo"
                and not any(
                    point is not selected and point.scheme == "unit"
                    for point in self.project.gun_points)):
            # unit_gun_icon is meaningful only for the generic unit_* family.
            # Converting the final generic mount to vanilla must not leave an
            # otherwise-empty unit_* family behind in canonical script output.
            self.project.unit_gun_default_icon = ""
        self._new_gun_point_scheme = scheme
        self._set_modified()
        self._sync_all()

    def _toggle_selected_sphere_visibility(self):
        entries = self._selected_sphere_entries()
        if not entries:
            return
        # Mixed selections collapse to hidden first; once every selected row
        # is hidden, the same command becomes Show and restores them all.
        target_visible = all(
            not sphere.visible for _index, sphere in entries)
        changing = [
            sphere for _index, sphere in entries
            if sphere.visible != target_visible
        ]
        if not changing and not self.project.sphere_isolation_active:
            return
        self._push_undo()
        for sphere in changing:
            sphere.visible = target_visible
        # Manual visibility editing supersedes Isolate mode. This prevents an
        # old isolation session from changing the meaning of the buttons after
        # the user starts explicitly hiding/showing individual rows.
        self.project.sphere_isolation_active = False
        self._set_modified()
        self._sync_all()

    def _set_all_sphere_visibility(self, visible: bool) -> None:
        spheres = self.project.spheres()
        if not spheres:
            return
        target_visible = bool(visible)
        changing = [
            sphere for sphere in spheres
            if sphere.visible != target_visible
        ]
        if not changing and not self.project.sphere_isolation_active:
            return
        self._push_undo()
        for sphere in changing:
            sphere.visible = target_visible
        self.project.sphere_isolation_active = False
        self._set_modified()
        self._sync_all()

    def _toggle_all_sphere_visibility(self) -> None:
        spheres = self.project.spheres()
        if not spheres:
            return
        all_hidden = all(not sphere.visible for sphere in spheres)
        self._set_all_sphere_visibility(all_hidden)


    def _toggle_selected_sphere_isolation(self):
        if self.project.sphere_isolation_active:
            self._unisolate_sphere_visibility()
            return
        selected = self._selected_sphere_indices()
        if not selected:
            return
        self._isolate_sphere_visibility(selected)

    def _isolate_sphere_visibility(self, selected: set[int]):
        spheres = self.project.spheres()
        if not selected or any(index < 0 or index >= len(spheres)
                               for index in selected):
            return
        changing = [
            sphere for sphere_index, sphere in enumerate(spheres)
            if sphere.visible != (sphere_index in selected)
        ]
        if not changing and self.project.sphere_isolation_active:
            return
        self._push_undo()
        for sphere_index, sphere in enumerate(spheres):
            sphere.visible = sphere_index in selected
        self.project.sphere_isolation_active = True
        self._set_modified()
        self._sync_all()

    def _unisolate_sphere_visibility(self, _index: int | None = None):
        spheres = self.project.spheres()
        if not spheres:
            return
        changing = [sphere for sphere in spheres if not sphere.visible]
        if not changing and not self.project.sphere_isolation_active:
            return
        self._push_undo()
        for sphere in changing:
            sphere.visible = True
        self.project.sphere_isolation_active = False
        self._set_modified()
        self._sync_all()

    def _visibility_changed(self, visible: bool):
        if self._syncing:
            return
        entries = self._selected_sphere_entries()
        if not entries:
            return
        value = bool(visible)
        changing = [
            sphere for _index, sphere in entries
            if sphere.visible != value
        ]
        if not changing and not self.project.sphere_isolation_active:
            return
        self._push_undo()
        for sphere in changing:
            sphere.visible = value
        self.project.sphere_isolation_active = False
        self._set_modified()
        self._sync_all()

    def _begin_radius_slider(self):
        if not self._selected_spheres_with_common_radius():
            return
        self._radius_spin_active = False
        self._push_undo()
        self._radius_slider_active = True

    def _radius_slider_changed(self, value: int):
        if self._syncing:
            return
        spheres = self._selected_spheres_with_common_radius()
        if not spheres:
            return
        if not self._radius_slider_active:
            self._push_undo()
        radius = max(1.0, round(self._slider_to_radius(value)))
        for sphere in spheres:
            sphere.radius = radius
        self._set_modified()
        self._sync_all()

    def _finish_radius_slider(self):
        self._radius_slider_active = False

    def _radius_spin_changed(self, value: float):
        if self._syncing:
            return
        spheres = self._selected_spheres_with_common_radius()
        if not spheres:
            return
        if abs(value - spheres[0].radius) < 1e-9:
            return
        if not self._radius_spin_active:
            self._push_undo()
            self._radius_spin_active = True
        radius = round(value, 0 if any(
            sphere.category == LEGACY for sphere in spheres) else 3)
        for sphere in spheres:
            sphere.radius = radius
        self._set_modified()
        self._sync_all()

    def _finish_radius_spin_edit(self):
        self._radius_spin_active = False

    def _sphere_display_name(
            self, sphere: CollisionSphere,
            compound_index: int | None = None) -> str:
        if sphere.category == LEGACY:
            return "Legacy Radius"
        if compound_index is None:
            for index, candidate in enumerate(self.project.compound):
                if candidate is sphere:
                    compound_index = index
                    break
        if compound_index is None:
            compound_index = -1
        return f"{TYPE_LABELS[sphere.category]} {compound_index}"

    def _refresh_sphere_tree(self):
        with QSignalBlocker(self.sphere_tree):
            self.sphere_tree.clear()
            selected_items = []
            primary_item = None
            selected_indices = self._selected_sphere_indices()
            compound_index = 0
            for flat_index, sphere in enumerate(self.project.spheres()):
                current_compound = None
                if sphere.category != LEGACY:
                    current_compound = compound_index
                    compound_index += 1
                index_text = (
                    "Legacy" if sphere.category == LEGACY
                    else str(current_compound)
                )
                radius_text = (
                    _radius_number(sphere.radius)
                    if sphere.category == LEGACY
                    else _sphere_number(sphere.radius))
                item = QTreeWidgetItem([
                    TYPE_LABELS[sphere.category],
                    radius_text,
                    "Visible" if sphere.visible else "Hidden",
                    _sphere_number(sphere.x),
                    _sphere_number(sphere.y),
                    _sphere_number(sphere.z),
                    index_text,
                ])
                item.setData(0, _SPHERE_INDEX_ROLE, flat_index)
                item.setForeground(0, QBrush(TYPE_COLORS[sphere.category]))
                item.setTextAlignment(
                    0, Qt.AlignmentFlag.AlignLeft
                    | Qt.AlignmentFlag.AlignVCenter)
                for column in range(1, 7):
                    item.setTextAlignment(
                        column, Qt.AlignmentFlag.AlignRight
                        | Qt.AlignmentFlag.AlignVCenter)
                self.sphere_tree.addTopLevelItem(item)
                if flat_index in selected_indices:
                    selected_items.append(item)
                if flat_index == self._selected:
                    primary_item = item
            if primary_item is not None:
                self.sphere_tree.setCurrentItem(primary_item)
                for item in selected_items:
                    item.setSelected(True)
            else:
                self.sphere_tree.setCurrentItem(None)
                self.sphere_tree.clearSelection()

    def _refresh_fire_point_tree(self, points):
        with QSignalBlocker(self.fire_point_tree):
            self.fire_point_tree.clear()
            selected_item = None
            for index, point in enumerate(points):
                item = QTreeWidgetItem([
                    f"F{index + 1}",
                    _number(point[0]), _number(point[1]), _number(point[2]),
                ])
                item.setData(0, _FIRE_POINT_INDEX_ROLE, index)
                item.setForeground(0, QBrush(FIRE_POINT_COLOR))
                for column in (1, 2, 3):
                    item.setTextAlignment(
                        column, Qt.AlignmentFlag.AlignRight
                        | Qt.AlignmentFlag.AlignVCenter)
                self.fire_point_tree.addTopLevelItem(item)
                if index == self._selected_fire_point:
                    selected_item = item
            if selected_item is not None:
                self.fire_point_tree.setCurrentItem(selected_item)
            else:
                self.fire_point_tree.setCurrentItem(None)
                self.fire_point_tree.clearSelection()


    def _refresh_gun_point_tree(self):
        with QSignalBlocker(self.gun_point_tree):
            self.gun_point_tree.clear()
            selected_item = None
            if (self.project.target_category == VEHICLE
                    and self.project.gun_points_enabled):
                for index, point in enumerate(self.project.gun_points):
                    label = f"G{index + 1}"
                    if point.name:
                        label += f" — {point.name}"
                    full_label = label
                    label = f"G{index + 1}"
                    family_label = (
                        "Vanilla" if point.scheme == "robo" else "OpenNeoUA")
                    limits = self.project.turret_limits.get(
                        int(point.gun_type))
                    angle_values = (
                        [str(_clamp_gun_angle(field, value)) for field, value in
                         (("side", limits.side), ("up", limits.up),
                          ("down", limits.down))]
                        if limits is not None
                        else ["-", "-", "-"]
                    )
                    item = QTreeWidgetItem([
                        label, family_label, str(int(point.gun_type)),
                        *angle_values,
                        _number(point.x), _number(point.y), _number(point.z),
                    ])
                    item.setData(0, _GUN_POINT_INDEX_ROLE, index)
                    item.setForeground(0, QBrush(GUN_POINT_COLOR))
                    item.setTextAlignment(
                        0, Qt.AlignmentFlag.AlignCenter
                        | Qt.AlignmentFlag.AlignVCenter)
                    family = (
                        "Legacy Host Station robo_* gun mount"
                        if point.scheme == "robo"
                        else "Generic OpenNeoUA unit_* vehicle gun mount")
                    item.setToolTip(0, f"{full_label}\n{family}")
                    item.setToolTip(1, family)
                    item.setToolTip(
                        2, "Referenced gun vehicle prototype ID.")
                    if limits is None:
                        angle_tip = (
                            f"Vehicle {int(point.gun_type)} was not found in "
                            "the loaded/imported script.")
                    else:
                        angle_tip = (
                            "Shared vanilla turret limits for referenced "
                            f"Vehicle {int(point.gun_type)} (radians x 1000).")
                    for column in (3, 4, 5):
                        item.setToolTip(column, angle_tip)
                    for column in range(2, 9):
                        item.setTextAlignment(
                            column, Qt.AlignmentFlag.AlignRight
                            | Qt.AlignmentFlag.AlignVCenter)
                    self.gun_point_tree.addTopLevelItem(item)
                    if index == self._selected_gun_point:
                        selected_item = item
            if selected_item is not None:
                self.gun_point_tree.setCurrentItem(selected_item)
            else:
                self.gun_point_tree.setCurrentItem(None)
                self.gun_point_tree.clearSelection()


    def _refresh_project_summary_menu(self):
        menu = self.project_summary_menu
        menu.clear()
        openneoua_count = sum(
            sphere.category == OPENNEOUA for sphere in self.project.compound)
        sphere = self._selected_sphere()
        selected_sphere_count = len(self._selected_sphere_indices())
        compound_index = self._selected_compound_index()
        if selected_sphere_count > 1:
            selected = f"{selected_sphere_count} Collision Spheres"
        elif sphere is not None:
            selected = self._sphere_display_name(sphere, compound_index)
        elif (self.project.target_category == VEHICLE
              and self.project.gun_points_enabled
              and 0 <= self._selected_gun_point
              < len(self.project.gun_points)):
            selected = f"Gun Point G{self._selected_gun_point + 1}"
        elif (self.project.target_category == VEHICLE
              and self.project.fire_points_enabled
              and 0 <= self._selected_fire_point
              < len(fire_point_positions(self.project))):
            selected = f"Fire Point F{self._selected_fire_point + 1}"
        else:
            selected = "None"
        compound_mode = bool(self.project.compound)
        if self.project.legacy is None:
            legacy_status = "Missing"
        elif compound_mode:
            legacy_status = "Disabled by compound collisions"
        else:
            legacy_status = "Active"
        collision_mode = (
            "OpenNeoUA compound (Legacy Radius disabled)"
            if compound_mode else "Vanilla Legacy Radius"
        )
        lines = [
            f"Project: {self.project.name or '<unnamed>'}",
            f"Source model: {self.project.source_model or '<none>'}",
            "Linked script object: "
            + (f"{self._active_script_path.name} — "
               f"{self._active_script_kind} {self._active_script_id}"
               if (self._active_script_path is not None
                   and self._active_script_id is not None)
               else "Not linked"),
            "Model preview scale: "
            f"X {_number(self.project.model_scale_x)}  "
            f"Y {_number(self.project.model_scale_y)}  "
            f"Z {_number(self.project.model_scale_z)}",
            "Ground alignment: "
            + ("Not shown for weapon target"
               if self.project.target_category == WEAPON
               else (f"Overeof {_number(self.project.overeof)}"
                     if self.project.overeof_enabled
                     else "Overeof not included")),
            "Fire points: "
            + (f"{_num_weapons_text(self.project)} "
               f"(preview {_fire_point_preview_count(self.project)}) at "
               f"({_number(self.project.fire_x)}, "
               f"{_number(self.project.fire_y)}, "
               f"{_number(self.project.fire_z)})"
               if (self.project.target_category == VEHICLE
                   and self.project.fire_points_enabled)
               else "Not included"),
            "Gun points: "
            + (str(len(self.project.gun_points))
               if (self.project.target_category == VEHICLE
                   and self.project.gun_points_enabled)
               else "Not included"),
            "Cockpit camera: "
            + (f"X {_number(self.project.cockpit_camera_offset_x)}  "
               f"Y {_number(self.project.cockpit_camera_offset_y)}  "
               f"Z {_number(self.project.cockpit_camera_offset_z)}"
               if (self.project.target_category == VEHICLE
                   and self.project.cockpit_camera_enabled)
               else "Not included"),
            f"Collision mode: {collision_mode}",
            f"Legacy Radius: {legacy_status}",
            f"Internal broad-phase extent: "
            f"{_radius_number(effective_runtime_radius(self.project))}",
            f"OpenNeoUA Collision Spheres: {openneoua_count}",
            f"Total Compound Spheres: {len(self.project.compound)}",
            f"Selected Element: {selected}",
        ]
        for line in lines:
            action = menu.addAction(line)
            action.setEnabled(False)

    def _preview_spheres(self) -> list[CollisionSphere]:
        """Return collision preview matching the shared runtime rule.

        ``coll_*`` without an authored ``radius`` is compound-only. If radius
        is explicitly present, Legacy Radius and OpenNeoUA Collision coexist.
        """

        offset_y = (
            -self.project.overeof
            if (self.project.target_category == VEHICLE
                and self.project.overeof_enabled)
            else 0.0
        )
        previews = []
        compound_active = bool(self.project.compound)
        for sphere in self.project.spheres():
            preview = sphere.clone()
            preview.y += offset_y
            if compound_active and sphere.category == LEGACY:
                # Compound spheres drive the runtime extent, so the legacy ring
                # would only add a misleading outline around the model.
                preview.visible = False
            previews.append(preview)
        return previews
