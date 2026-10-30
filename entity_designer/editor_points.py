"""CollisionEditorWindow mixin: cockpit camera and fire/gun point add/remove."""

from __future__ import annotations

import math


from entity_designer.script_model import (
    _fire_point_preview_count,
    _project_num_weapons_range,
    GunPoint,
    VEHICLE,
)


class _WeaponPointsMixin:
    """Part of CollisionEditorWindow; `self` is always the CollisionEditorWindow."""

    def _toggle_cockpit_camera(self) -> None:
        if self._syncing or self.project.target_category != VEHICLE:
            return
        self._vehicle_preview_active_edits.clear()
        self._push_undo()
        self.project.cockpit_camera_enabled = (
            not self.project.cockpit_camera_enabled)
        self._set_modified()
        self._sync_all()

    def _cockpit_value_changed(self, axis: str, value: float) -> None:
        if self._syncing or self.project.target_category != VEHICLE:
            return
        value = float(value)
        if not math.isfinite(value) or axis not in {"x", "y", "z"}:
            return
        field = f"cockpit_camera_offset_{axis}"
        if abs(float(getattr(self.project, field)) - value) < 1e-9:
            return
        self._begin_vehicle_preview_edit(f"cockpit_{axis}")
        setattr(self.project, field, value)
        self._set_modified()
        self._sync_all()

    def _reset_cockpit_camera(self) -> None:
        if self.project.target_category != VEHICLE:
            return
        values = (
            self.project.cockpit_camera_enabled,
            self.project.cockpit_camera_offset_x,
            self.project.cockpit_camera_offset_y,
            self.project.cockpit_camera_offset_z,
        )
        if values == (False, 0.0, 0.0, 0.0):
            return
        self._vehicle_preview_active_edits.clear()
        self._push_undo()
        self.project.cockpit_camera_offset_x = 0.0
        self.project.cockpit_camera_offset_y = 0.0
        self.project.cockpit_camera_offset_z = 0.0
        self._set_modified()
        self._sync_all()

    def _cockpit_nudge(self, direction) -> None:
        if not self._is_cockpit_tab_active():
            return
        step = float(self.move_strength_spin.value())
        self._vehicle_preview_active_edits.clear()
        self._push_undo()
        # Moving the cockpit camera is the explicit request for the offset:
        # enable its output so the edit is not silently discarded on export.
        self.project.cockpit_camera_enabled = True
        self.project.cockpit_camera_offset_x += float(direction[0]) * step
        self.project.cockpit_camera_offset_y += float(direction[1]) * step
        self.project.cockpit_camera_offset_z += float(direction[2]) * step
        self._set_modified()
        self._sync_all()

    def _begin_vehicle_preview_edit(self, field: str) -> None:
        if field not in self._vehicle_preview_active_edits:
            self._push_undo()
            self._vehicle_preview_active_edits.add(field)

    def _finish_vehicle_preview_edit(self, field: str) -> None:
        self._vehicle_preview_active_edits.discard(field)

    def add_fire_point(self) -> None:
        if self.project.target_category != VEHICLE:
            return
        if self.project.fire_points_enabled:
            minimum, maximum = _project_num_weapons_range(self.project)
            count = max(1, maximum)
            if count >= 255:
                self.statusBar().showMessage(
                    "Vanilla num_weapons is already at its maximum (255).",
                    4500)
                return
        self._vehicle_preview_active_edits.clear()
        self._push_undo()
        if not self.project.fire_points_enabled:
            # fire_x/y/z + num_weapons are one vanilla rack. Add means one
            # authored muzzle, not resurrection of a stale disabled count.
            self.project.fire_points_enabled = True
            self.project.num_weapons = 1
            self.project.num_weapons_max = 1
            self._selected_fire_point = 0
        else:
            if minimum == maximum:
                self.project.num_weapons = count + 1
                self.project.num_weapons_max = count + 1
            else:
                self.project.num_weapons_max = count + 1
            self._selected_fire_point = count
        self._selected = -1
        self._selected_gun_point = -1
        self._set_modified()
        self._sync_all()

    def remove_fire_point(self) -> None:
        if (self.project.target_category != VEHICLE
                or not self.project.fire_points_enabled):
            return
        minimum, maximum = _project_num_weapons_range(self.project)
        count = max(1, maximum)
        index = self._selected_fire_point
        if not (0 <= index < count):
            return
        self._vehicle_preview_active_edits.clear()
        self._push_undo()
        if count <= 1:
            # Keep authored fire_x/y/z values in memory so re-adding a point
            # is non-destructive, but omit the whole Fire Point block from
            # export/overwrite until the user adds it again.
            self.project.fire_points_enabled = False
            self.project.num_weapons = 1
            self.project.num_weapons_max = 1
            self._selected_fire_point = -1
        else:
            if minimum == maximum:
                self.project.num_weapons = count - 1
                self.project.num_weapons_max = count - 1
            else:
                self.project.num_weapons_max = max(minimum, count - 1)
            self._selected_fire_point = min(
                index, _fire_point_preview_count(self.project) - 1)
            self.statusBar().showMessage(
                "Vanilla Fire Points share one symmetric rack; removing a "
                "point re-spaced the remaining muzzles.", 5000)
        self._set_modified()
        self._sync_all()

    def _reset_fire_point_position(self) -> None:
        """Reset the authored vanilla Fire Point rack to the model origin."""

        if self.project.target_category != VEHICLE:
            return
        was_enabled = self.project.fire_points_enabled
        if (was_enabled
                and self.project.fire_x == 0.0
                and self.project.fire_y == 0.0
                and self.project.fire_z == 0.0):
            return
        self._vehicle_preview_active_edits.clear()
        self._push_undo()
        self.project.fire_points_enabled = True
        self.project.fire_x = 0.0
        self.project.fire_y = 0.0
        self.project.fire_z = 0.0
        if not was_enabled:
            self.project.num_weapons = 1
            self.project.num_weapons_max = 1
        if self._selected_fire_point < 0:
            self._selected_fire_point = 0
        self._selected = -1
        self._selected_gun_point = -1
        self._set_modified()
        self._sync_all()

    def _fire_point_value_changed(self, field: str, value: float) -> None:
        if self._syncing or self.project.target_category != VEHICLE:
            return
        value = float(value)
        if not math.isfinite(value):
            return
        current = float(getattr(self.project, field))
        if abs(current - value) < 1e-9:
            return
        self._begin_vehicle_preview_edit(field)
        setattr(self.project, field, value)
        self.project.fire_points_enabled = True
        self._set_modified()
        self._sync_all()

    def _num_weapons_changed(self, value: int) -> None:
        if self._syncing or self.project.target_category != VEHICLE:
            return
        value = max(0, min(255, int(value)))
        old_min, old_max = _project_num_weapons_range(self.project)
        if value == old_min:
            return
        self._begin_vehicle_preview_edit("num_weapons")
        self.project.num_weapons = value
        if value == 0:
            self.project.num_weapons_max = 0
        elif old_min == old_max:
            self.project.num_weapons_max = value
        else:
            self.project.num_weapons_max = max(value, old_max)
        self.project.fire_points_enabled = True
        point_count = _fire_point_preview_count(self.project)
        if self._selected_fire_point >= point_count:
            self._selected_fire_point = point_count - 1
        self._set_modified()
        self._sync_all()

    def _num_weapons_max_changed(self, value: int) -> None:
        if self._syncing or self.project.target_category != VEHICLE:
            return
        value = max(0, min(255, int(value)))
        old_min, old_max = _project_num_weapons_range(self.project)
        if value == old_max:
            return
        self._begin_vehicle_preview_edit("num_weapons_max")
        if value == 0:
            self.project.num_weapons = 0
            self.project.num_weapons_max = 0
        else:
            if old_min == 0:
                self.project.num_weapons = 1
                old_min = 1
            self.project.num_weapons_max = max(old_min, value)
        self.project.fire_points_enabled = True
        point_count = _fire_point_preview_count(self.project)
        if self._selected_fire_point >= point_count:
            self._selected_fire_point = point_count - 1
        self._set_modified()
        self._sync_all()


    def add_gun_point(self):
        if self.project.target_category != VEHICLE:
            return
        scheme = (
            self._new_gun_point_scheme
            if self._new_gun_point_scheme in {"robo", "unit"}
            else "unit")
        family_count = sum(
            point.scheme == scheme for point in self.project.gun_points)
        if family_count >= 20:
            family_name = (
                "Vanilla robo_*" if scheme == "robo"
                else "OpenNeoUA unit_*")
            self.statusBar().showMessage(
                f"{family_name} supports at most 20 gun points.", 5000)
            return
        self._vehicle_preview_active_edits.clear()
        self._push_undo()
        point = GunPoint(scheme=scheme)
        self.project.gun_points.append(point)
        self.project.gun_points_enabled = True
        self._selected_gun_point = len(self.project.gun_points) - 1
        self._selected_fire_point = -1
        self._selected = -1
        self._set_modified()
        self._sync_all()

    def remove_gun_point(self):
        point = self._selected_gun()
        if point is None:
            return
        index = self._selected_gun_point
        self._vehicle_preview_active_edits.clear()
        self._push_undo()
        del self.project.gun_points[index]
        # Keep management enabled so Apply/Overwrite removes the old script
        # rows when the user intentionally deletes the final mount.
        self.project.gun_points_enabled = True
        self._selected_gun_point = min(
            index, len(self.project.gun_points) - 1)
        self._set_modified()
        self._sync_all()
