"""CollisionEditorWindow mixin: loaded-state baselines, per-tab change detection and tab reset."""

from __future__ import annotations


from PySide6.QtWidgets import (
    QMessageBox,
)

from entity_designer.fire_area import SHAPE_KEYS
from entity_designer.script_model import (
    _number,
    _project_num_weapons_range,
    _resolve_patch_target_block,
    _SCRIPT_TAB_BRIEFING,
    _SCRIPT_TAB_COCKPIT,
    _SCRIPT_TAB_COLLISION,
    _SCRIPT_TAB_FIRE,
    _SCRIPT_TAB_FIRE_AREA,
    _SCRIPT_TAB_GUN,
    _SCRIPT_TAB_ORDER,
    CollisionProject,
    CollisionScriptError,
    gun_point_families_in_block,
    import_briefing_size_block,
    import_briefing_xyz_block,
    import_cockpit_camera_block,
    import_collision_block,
    import_collision_shape_reference,
    import_fire_area_block,
    import_fire_points_block,
    import_gun_points_block,
    import_overeof_block,
    import_turret_limits,
    OPENNEOUA,
    read_script_file,
    VEHICLE,
    WEAPON,
)


class _TabBaselineMixin:
    """Part of CollisionEditorWindow; `self` is always the CollisionEditorWindow."""

    def _capture_loaded_cockpit_camera(self) -> None:
        """Snapshot source-authored cockpit coordinates for later restore."""

        self._loaded_cockpit_camera_enabled = bool(
            self.project.cockpit_camera_enabled)
        self._loaded_cockpit_camera_offset = (
            float(self.project.cockpit_camera_offset_x),
            float(self.project.cockpit_camera_offset_y),
            float(self.project.cockpit_camera_offset_z),
        )

    def _restore_loaded_cockpit_camera(self) -> None:
        """Restore cockpit X/Y/Z exactly as read from the source script."""

        if (self.project.target_category != VEHICLE
                or not self._loaded_cockpit_camera_enabled):
            return
        baseline = self._loaded_cockpit_camera_offset
        current = (
            float(self.project.cockpit_camera_offset_x),
            float(self.project.cockpit_camera_offset_y),
            float(self.project.cockpit_camera_offset_z),
        )
        if (self.project.cockpit_camera_enabled
                and all(abs(a - b) < 1e-9
                        for a, b in zip(current, baseline))):
            return
        self._vehicle_preview_active_edits.clear()
        self._push_undo()
        self.project.cockpit_camera_enabled = True
        (self.project.cockpit_camera_offset_x,
         self.project.cockpit_camera_offset_y,
         self.project.cockpit_camera_offset_z) = baseline
        self._set_modified()
        self._sync_all()

    def _capture_loaded_gun_points(self) -> None:
        """Snapshot source-authored mounts and turret limits for Reset All."""

        self._loaded_gun_points_enabled = bool(
            self.project.gun_points_enabled)
        self._loaded_gun_points = [
            point.clone() for point in self.project.gun_points]
        self._loaded_unit_gun_default_icon = (
            self.project.unit_gun_default_icon)
        self._loaded_gun_point_scheme = self._new_gun_point_scheme
        self._loaded_turret_limits = {
            vehicle_id: limits.clone()
            for vehicle_id, limits in self.project.turret_limits.items()
        }

    def _capture_tab_reset_baseline(self) -> None:
        """Remember the last explicit source/import state for tab Reset."""

        self._tab_reset_baseline = self.project.snapshot()
        self._tab_reset_gun_point_scheme = self._new_gun_point_scheme

    def _baseline_project(self) -> CollisionProject:
        baseline = CollisionProject()
        baseline.restore(self._tab_reset_baseline)
        return baseline

    @staticmethod
    def _script_tab_state(project: CollisionProject, group: str) -> tuple:
        """Compare authored values using the precision written to scripts.

        Disabled controls and the source block used for a turret are editor
        metadata, not distinct game parameters. Ignore them when checking
        whether the exported definition still differs from this project.
        """

        def authored_number(value: float) -> float:
            return float(_number(float(value)))

        if group == _SCRIPT_TAB_COLLISION:
            legacy = (
                None if project.legacy is None
                else int(round(project.legacy.radius)))
            compound = tuple(
                (authored_number(sphere.x), authored_number(sphere.y),
                 authored_number(sphere.z), authored_number(sphere.radius))
                for sphere in project.compound)
            shape_path = (
                project.collision_shape_path.replace("\\", "/").strip())
            return (
                bool(project.overeof_enabled),
                authored_number(project.overeof)
                if project.overeof_enabled else None,
                legacy, compound, shape_path,
            )
        if group == _SCRIPT_TAB_FIRE:
            if not project.fire_points_enabled:
                return (False,)
            return (
                True,
                authored_number(project.fire_x),
                authored_number(project.fire_y),
                authored_number(project.fire_z),
                *_project_num_weapons_range(project),
            )
        if group == _SCRIPT_TAB_GUN:
            authored_points = (
                project.gun_points if project.gun_points_enabled else [])
            points = tuple((
                point.scheme, authored_number(point.x),
                authored_number(point.y), authored_number(point.z),
                authored_number(point.dir_x), authored_number(point.dir_y),
                authored_number(point.dir_z), int(point.gun_type),
                point.name, point.icon,
            ) for point in authored_points)
            default_icon = (
                project.unit_gun_default_icon
                if project.gun_points_enabled else "")
            # The script catalog provides limits for every vehicle in the
            # file; only guns mounted here (or explicitly edited limits)
            # belong to this entity's Gun Points export status.
            referenced_types = {point.gun_type for point in authored_points}
            limits = tuple((
                vehicle_id, int(limit.side), int(limit.up),
                int(limit.down),
            ) for vehicle_id, limit in sorted(project.turret_limits.items())
                if limit.enabled and
                (vehicle_id in referenced_types or limit.dirty))
            return (points, default_icon, limits)
        if group == _SCRIPT_TAB_BRIEFING:
            return (
                bool(project.briefing_size_enabled),
                int(round(project.briefing_size))
                if project.briefing_size_enabled else None,
                bool(project.briefing_xyz_enabled),
                # briefing_xyz is written with ``:g`` (6 digits): compare at that precision.
                float(f"{project.briefing_x:g}"), float(f"{project.briefing_y:g}"),
                float(f"{project.briefing_z:g}"),
            )
        if group == _SCRIPT_TAB_COCKPIT:
            if not project.cockpit_camera_enabled:
                return (False,)
            return (
                True,
                authored_number(project.cockpit_camera_offset_x),
                authored_number(project.cockpit_camera_offset_y),
                authored_number(project.cockpit_camera_offset_z),
            )
        if group == _SCRIPT_TAB_FIRE_AREA:
            area = project.fire_area
            return (
                tuple(None if area.size(shape) is None else tuple(
                    authored_number(value) for value in area.size(shape))
                    for shape in SHAPE_KEYS),
                authored_number(area.dead_zone),
                authored_number(area.max_inclination),
            )
        raise ValueError(group)

    def _changed_script_tabs(self) -> set[str]:
        """Return tabs whose script-authored state differs from the source."""

        baseline = self._baseline_project()
        groups = {_SCRIPT_TAB_COLLISION}
        if self.project.target_category == VEHICLE:
            groups.update((
                _SCRIPT_TAB_FIRE, _SCRIPT_TAB_GUN, _SCRIPT_TAB_COCKPIT, _SCRIPT_TAB_BRIEFING))
        else:
            groups.add(_SCRIPT_TAB_FIRE_AREA)
        return {
            group for group in groups
            if self._script_tab_state(self.project, group)
            != self._script_tab_state(baseline, group)
        }

    def _refresh_linked_script_baseline(
            self, applied_groups: set[str] | None = None) -> bool:
        """Re-read exported tabs without replacing untouched import state.

        The game catalog may have loaded an effective/merged definition while
        the actual saved file contains only its own blocks. Refreshing every
        tab from that file made unrelated Gun Points appear dirty after a
        Cockpit or Collision export. Only explicitly applied groups change
        their source baseline; skipped preview sections remain pending.
        """

        if (self._active_script_path is None
                or self._active_script_id is None
                or not self._active_script_kind):
            return False
        groups = (
            set(_SCRIPT_TAB_ORDER) if applied_groups is None
            else set(applied_groups))
        if not groups:
            return True
        try:
            text, _encoding, _bom = read_script_file(self._active_script_path)
            block = _resolve_patch_target_block(
                text, self._active_script_kind, self._active_script_id)
            baseline = self._baseline_project()
            if _SCRIPT_TAB_COLLISION in groups:
                baseline.legacy, baseline.compound, _warnings = (
                    import_collision_block(text, block, OPENNEOUA))
                baseline.collision_shape_path = import_collision_shape_reference(
                    text, block)
                if baseline.target_category == VEHICLE:
                    baseline.overeof_enabled, baseline.overeof = (
                        import_overeof_block(text, block))
            if baseline.target_category == VEHICLE:
                if _SCRIPT_TAB_FIRE in groups:
                    (baseline.fire_points_enabled, baseline.fire_x,
                     baseline.fire_y, baseline.fire_z, baseline.num_weapons,
                     baseline.num_weapons_max) = import_fire_points_block(
                        text, block)
                if _SCRIPT_TAB_GUN in groups:
                    (baseline.gun_points_enabled, baseline.gun_points,
                     baseline.unit_gun_default_icon) = import_gun_points_block(
                        text, block)
                    # Turret limits belong to their own vehicle definitions.
                    # A merged game-catalog import can contain angles sourced
                    # from other files: replace only limits actually edited
                    # and written by this workspace, not every catalog entry.
                    source_limits = import_turret_limits(text)
                    for vehicle_id, current in self.project.turret_limits.items():
                        if current.dirty:
                            if vehicle_id in source_limits:
                                baseline.turret_limits[vehicle_id] = (
                                    source_limits[vehicle_id])
                            else:
                                baseline.turret_limits.pop(vehicle_id, None)
                if _SCRIPT_TAB_BRIEFING in groups:
                    (baseline.briefing_size_enabled,
                     baseline.briefing_size) = import_briefing_size_block(
                        text, block)
                    (baseline.briefing_xyz_enabled, baseline.briefing_x,
                     baseline.briefing_y, baseline.briefing_z) = import_briefing_xyz_block(text, block)
                if _SCRIPT_TAB_COCKPIT in groups:
                    (baseline.cockpit_camera_enabled,
                     baseline.cockpit_camera_offset_x,
                     baseline.cockpit_camera_offset_y,
                     baseline.cockpit_camera_offset_z) = (
                        import_cockpit_camera_block(text, block))

            if _SCRIPT_TAB_GUN in groups:
                imported_schemes = gun_point_families_in_block(text, block)
                if len(imported_schemes) == 1:
                    baseline_scheme = next(iter(imported_schemes))
                elif "unit" in imported_schemes:
                    baseline_scheme = "unit"
                else:
                    baseline_scheme = self._tab_reset_gun_point_scheme
                self._tab_reset_gun_point_scheme = baseline_scheme
                self._loaded_gun_points_enabled = baseline.gun_points_enabled
                self._loaded_gun_points = [
                    point.clone() for point in baseline.gun_points]
                self._loaded_unit_gun_default_icon = (
                    baseline.unit_gun_default_icon)
                self._loaded_gun_point_scheme = baseline_scheme
                self._loaded_turret_limits = {
                    vehicle_id: limits.clone()
                    for vehicle_id, limits in baseline.turret_limits.items()
                }
                for vehicle_id, limits in self.project.turret_limits.items():
                    source = baseline.turret_limits.get(vehicle_id)
                    if source is not None:
                        limits.source_kind = source.source_kind
                        limits.source_name = source.source_name
                    limits.dirty = (
                        source is None or
                        (limits.enabled, limits.side if limits.enabled else 0,
                         limits.up if limits.enabled else 0,
                         limits.down if limits.enabled else 0)
                        != (source.enabled,
                            source.side if source.enabled else 0,
                            source.up if source.enabled else 0,
                            source.down if source.enabled else 0))
            if _SCRIPT_TAB_COCKPIT in groups:
                self._loaded_cockpit_camera_enabled = (
                    baseline.cockpit_camera_enabled)
                self._loaded_cockpit_camera_offset = (
                    baseline.cockpit_camera_offset_x,
                    baseline.cockpit_camera_offset_y,
                    baseline.cockpit_camera_offset_z,
                )
            if (baseline.target_category == WEAPON
                    and _SCRIPT_TAB_FIRE_AREA in groups):
                baseline.fire_area = import_fire_area_block(text, block)
            self._tab_reset_baseline = baseline.snapshot()
            self._set_modified(bool(self._changed_script_tabs()))
            return True
        except (OSError, UnicodeError, CollisionScriptError):
            return False

    def _workspace_state(self, project: CollisionProject,
                         tab_index: int) -> tuple:
        def sphere_state(sphere):
            return None if sphere is None else (
                sphere.category, sphere.x, sphere.y, sphere.z,
                sphere.radius, sphere.visible,
            )

        if tab_index == self.ground_tab_index:
            return (project.overeof_enabled, project.overeof)
        if tab_index == self.collision_tab_index:
            return (
                sphere_state(project.legacy),
                tuple(sphere_state(sphere) for sphere in project.compound),
                project.sphere_isolation_active,
            )
        if tab_index == self.collision_shape_tab_index:
            return (
                project.collision_shape,
                project.collision_shape_path,
                project.collision_shape_file_path,
                project.collision_shape_owners,
                project.collision_shape_components,
                project.collision_shape_warnings,
            )
        if tab_index == self.fire_points_tab_index:
            return (
                project.fire_points_enabled,
                project.fire_x, project.fire_y, project.fire_z,
                project.num_weapons, project.num_weapons_max,
            )
        if tab_index == self.gun_points_tab_index:
            return (
                project.gun_points_enabled,
                project.unit_gun_default_icon,
                tuple((
                    point.scheme, point.x, point.y, point.z,
                    point.dir_x, point.dir_y, point.dir_z,
                    point.gun_type, point.name, point.icon,
                ) for point in project.gun_points),
                tuple((
                    vehicle_id, limits.enabled, limits.side, limits.up,
                    limits.down, limits.source_kind, limits.source_name,
                    limits.dirty,
                ) for vehicle_id, limits in sorted(
                    project.turret_limits.items())),
            )
        if tab_index == self.briefing_tab_index:
            return (project.briefing_size_enabled, project.briefing_size,
                    project.briefing_xyz_enabled, project.briefing_x,
                    project.briefing_y, project.briefing_z)
        if tab_index == self.cockpit_tab_index:
            return (
                project.cockpit_camera_enabled,
                project.cockpit_camera_offset_x,
                project.cockpit_camera_offset_y,
                project.cockpit_camera_offset_z,
            )
        if tab_index == self.fire_area_tab_index:
            return (project.fire_area,)
        return ()

    def _current_tab_has_changes(self) -> bool:
        if not hasattr(self, "properties_tabs"):
            return False
        tab_index = self.properties_tabs.currentIndex()
        baseline = self._baseline_project()
        return (
            self._workspace_state(self.project, tab_index)
            != self._workspace_state(baseline, tab_index)
        )

    def _reset_current_tab(self) -> None:
        """Revert only the active authoring workspace after confirmation."""

        if not hasattr(self, "properties_tabs"):
            return
        tab_index = self.properties_tabs.currentIndex()
        if not self._current_tab_has_changes():
            return
        tab_name = self.properties_tabs.tabText(tab_index)
        answer = QMessageBox.question(
            self, "Reset Current Tab",
            f"Reset all changes in the {tab_name} tab to the last "
            "loaded/imported state?\n\nChanges in the other tabs are kept.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return

        baseline = self._baseline_project()
        self._vehicle_preview_active_edits.clear()
        self._push_undo()

        if tab_index == self.ground_tab_index:
            self.project.overeof_enabled = baseline.overeof_enabled
            self.project.overeof = baseline.overeof
        elif tab_index == self.collision_tab_index:
            self.project.legacy = (
                baseline.legacy.clone() if baseline.legacy is not None
                else None)
            self.project.compound = [
                sphere.clone() for sphere in baseline.compound]
            self.project.sphere_isolation_active = (
                baseline.sphere_isolation_active)
            self._selected = min(
                self._selected, len(self.project.spheres()) - 1)
        elif tab_index == self.collision_shape_tab_index:
            self.project.collision_shape = baseline.collision_shape
            self.project.collision_shape_path = baseline.collision_shape_path
            self.project.collision_shape_file_path = (
                baseline.collision_shape_file_path)
            self.project.collision_shape_owners = (
                baseline.collision_shape_owners)
            self.project.collision_shape_components = (
                baseline.collision_shape_components)
            self.project.collision_shape_warnings = (
                baseline.collision_shape_warnings)
        elif tab_index == self.fire_points_tab_index:
            self.project.fire_points_enabled = baseline.fire_points_enabled
            self.project.fire_x = baseline.fire_x
            self.project.fire_y = baseline.fire_y
            self.project.fire_z = baseline.fire_z
            self.project.num_weapons = baseline.num_weapons
            self.project.num_weapons_max = baseline.num_weapons_max
            self._selected_fire_point = -1
        elif tab_index == self.gun_points_tab_index:
            self.project.gun_points_enabled = baseline.gun_points_enabled
            self.project.gun_points = [
                point.clone() for point in baseline.gun_points]
            self.project.unit_gun_default_icon = (
                baseline.unit_gun_default_icon)
            self.project.turret_limits = {
                vehicle_id: limits.clone()
                for vehicle_id, limits in baseline.turret_limits.items()
            }
            self._new_gun_point_scheme = self._tab_reset_gun_point_scheme
            self._selected_gun_point = min(
                self._selected_gun_point,
                len(self.project.gun_points) - 1)
        elif tab_index == self.briefing_tab_index:
            self.project.briefing_size_enabled = baseline.briefing_size_enabled
            self.project.briefing_size = baseline.briefing_size
            self.project.briefing_xyz_enabled = baseline.briefing_xyz_enabled
            self.project.briefing_x = baseline.briefing_x
            self.project.briefing_y = baseline.briefing_y
            self.project.briefing_z = baseline.briefing_z
        elif tab_index == self.cockpit_tab_index:
            self.project.cockpit_camera_enabled = (
                baseline.cockpit_camera_enabled)
            self.project.cockpit_camera_offset_x = (
                baseline.cockpit_camera_offset_x)
            self.project.cockpit_camera_offset_y = (
                baseline.cockpit_camera_offset_y)
            self.project.cockpit_camera_offset_z = (
                baseline.cockpit_camera_offset_z)
        elif tab_index == self.fire_area_tab_index:
            self.project.fire_area = baseline.fire_area

        self._set_modified(
            self.project.snapshot() != self._tab_reset_baseline)
        self._sync_all()
        self.statusBar().showMessage(
            f"{tab_name} restored to the last loaded/imported state.", 5000)
