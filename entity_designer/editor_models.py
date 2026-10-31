"""CollisionEditorWindow mixin: model browser: BASE/SKLT lists, script units and model states."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import (
    QSignalBlocker,
    Qt,
    QTimer,
)
from PySide6.QtWidgets import (
    QMessageBox,
    QTreeWidgetItem,
)

from entity_designer import destruction_fx as fx
from entity_designer import fx_visuals
from entity_designer.script_model import (
    _load_3ds_family,
    _public_dependency,
    CollisionScriptError,
    CollisionSphere,
    runtime_vp_table,
    VEHICLE,
    VehicleModelReference,
)
from studio.assets.dependencies import filter_tree as filter_asset_tree
from studio.assets.family import AssetFamily, load_manual_family
from studio.assets.visproto import (
    reconstruct_embedded_vps,
    VPEmbeddedError,
)
from entity_designer.editor_common import (
    _MODEL_NAME_ROLE,
    _MODEL_VP_ROLE,
    _SCRIPT_REFERENCE_ROLE,
    _vp_ids_per_model,
)


class _ModelBrowserMixin:
    """Part of CollisionEditorWindow; `self` is always the CollisionEditorWindow."""

    def open_base(self, path: str | Path, setbas: str | Path | None = None):
        """Open a BASE, SET.BAS or 3DS. ``setbas`` is the set that supplies the
        textures/animations of a loose BASE (default: the set already open)."""

        if not self.fx_panel.confirm_text_changes():
            return False
        resource_set = setbas if setbas is not None else self._resource_set_bas_path()
        self._clear_script_link()
        path = Path(path)
        embedded = None
        vp_table = None
        vp_source = ""
        vp_warnings: list[str] = []
        if path.suffix.casefold() == ".3ds":
            try:
                family = _load_3ds_family(path)
            except Exception as exc:
                QMessageBox.critical(
                    self, "3DS load failed",
                    f"No file was modified.\n\n{exc}")
                return False
            self.family = family
            self._vp_embedded = None
            self._vp_table = None
            self._vp_table_source = ""
            self._active_base_path = path
            self._last_directory = path.parent
            self.project.source_base = path.name
            self._fill_models(family)
            self.source_label.setText(
                f"{path.name}\n{len(family.all_objects()) - 1} mesh parts; "
                "geometry-only preview through AssetViewport")
            self._set_model_browser_enabled(True)
            self._sync_close_archive_action()
            self._sync_animation_controls()
            self._sync_all()
            self._set_modified()
            return True
        if path.name.casefold() == "set.bas":
            try:
                embedded = reconstruct_embedded_vps(path)
                vp_table, vp_source, vp_warnings = runtime_vp_table(
                    path, embedded)
            except (VPEmbeddedError, OSError, ValueError,
                    CollisionScriptError) as exc:
                QMessageBox.critical(
                    self, "VP database unavailable",
                    "SET.BAS was found, but its visual-prototype table could "
                    f"not be reconstructed.\n\n{exc}")
                return False
        try:
            if resource_set is not None and path.name.casefold() != "set.bas":
                family = self._load_family(path, setbas=resource_set)
            else:
                family = self._load_family(path)
        except Exception as exc:
            QMessageBox.critical(
                self, "Load failed", f"No file was modified.\n\n{exc}")
            return False
        if not any(obj.skeleton is not None for obj in family.all_objects()):
            QMessageBox.warning(
                self, "No models",
                "The BASE was parsed, but no SKLT model could be resolved.")
        self.family = family
        self._vp_embedded = embedded
        self._vp_table = vp_table
        self._vp_table_source = vp_source
        self._active_base_path = path
        self._last_directory = path.parent
        self.project.source_base = path.name
        self._fill_models(family)
        if path.suffix.casefold() == '.bas':
            from studio.ui.tabs import sync_archive_set_selector
            sync_archive_set_selector(self.game_object_browser.set_combo, path)
            self.game_object_browser.select_object(None)
        self.source_label.setText(
            f"{path.name}\n"
            f"{len([o for o in family.all_objects() if o.skeleton])} models")
        self._set_model_browser_enabled(True)
        if vp_warnings:
            self.statusBar().showMessage(" ".join(vp_warnings), 9000)
        self._set_modified()
        self._sync_close_archive_action()
        return True

    def open_sklt(self, path: str | Path):
        if not self.fx_panel.confirm_text_changes():
            return False
        self._clear_script_link()
        try:
            family = _public_dependency(
                "load_manual_family", load_manual_family)(path, [], [])
        except Exception as exc:
            QMessageBox.critical(
                self, "Load failed", f"No file was modified.\n\n{exc}")
            return
        if not any(obj.skeleton is not None for obj in family.all_objects()):
            QMessageBox.warning(
                self, "SKLT parse failed",
                "\n".join(family.warnings) or "The model could not be loaded.")
            return
        self.family = family
        self._vp_embedded = None
        self._vp_table = None
        self._vp_table_source = ""
        self._active_base_path = Path(path)
        self._last_directory = Path(path).parent
        self.project.source_base = ""
        self._fill_models(family)
        self.source_label.setText(
            f"{Path(path).name}\nExternal SKLT; geometry-only unless its "
            "material mapping is supplied by a BASE.")
        self._set_model_browser_enabled(True)
        self._set_modified()
        self._sync_close_archive_action()

    def _fill_models(self, family: AssetFamily):
        self._set_script_unit_mode(False)
        self.model_tree.clear()
        self._model_info_by_vp = {}
        self._script_unit_references = []
        self._script_unit_visual_reference = None
        model_index = 0
        vp_ids_per_model = _vp_ids_per_model(
            family, self._vp_embedded, self._vp_table)
        for obj in family.all_objects():
            if obj.skeleton is None:
                continue
            vp_ids = vp_ids_per_model[model_index]
            exact_vp = bool(vp_ids)
            if exact_vp:
                # First family object wins a VP, matching the first-match
                # lookup previously used to resolve script imports.
                for vp_id in vp_ids:
                    self._model_info_by_vp.setdefault(
                        vp_id, (obj.owner_path, obj.display_name))
            vp_text = ", ".join(map(str, vp_ids)) if vp_ids else "—"
            item = QTreeWidgetItem([
                obj.base_object.skeleton_name or obj.owner_path,
                vp_text,
            ])
            full_path = obj.base_object.skeleton_name or obj.owner_path
            item.setData(0, Qt.ItemDataRole.UserRole, obj.owner_path)
            item.setData(0, _MODEL_NAME_ROLE, obj.display_name)
            item.setData(
                0, _MODEL_VP_ROLE, tuple(vp_ids) if exact_vp else ())
            item.setToolTip(0, full_path)
            item.setToolTip(
                1, f"VP {vp_text}" if exact_vp
                else "Child model without its own VP slot")
            item.setTextAlignment(
                1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.model_tree.addTopLevelItem(item)
            model_index += 1
        self.model_tree.resizeColumnToContents(1)
        self._filter_models(self.model_search.text())
        for index in range(self.model_tree.topLevelItemCount()):
            item = self.model_tree.topLevelItem(index)
            if not item.isHidden():
                self.model_tree.setCurrentItem(item)
                break

    def _filter_models(self, text: str) -> None:
        self._model_filter_expansion = filter_asset_tree(
            self.model_tree, text, self._model_item_search_metadata,
            self._model_filter_expansion)

    @staticmethod
    def _model_item_search_metadata(item) -> str:
        display_name = item.data(0, _MODEL_NAME_ROLE) or ""
        owner_path = item.data(0, Qt.ItemDataRole.UserRole) or ""
        reference = item.data(0, _SCRIPT_REFERENCE_ROLE)
        unit_label = reference.label if reference is not None else ""
        return f"{display_name} {owner_path} {unit_label}"

    def _fill_script_units(
            self, references: list[VehicleModelReference], *,
            active_visual_reference: VehicleModelReference | None = None
            ) -> None:
        """List the loadable units of the source script in the left panel.

        Rows are rebuilt only when the content really changes: rebuilding the
        same list while Qt is still handling the click that selected a unit
        would reset the scroll position and leave the selection on an
        unrelated row. Signals stay blocked during a rebuild so filling the
        list can never trigger a unit load on its own.
        """

        if (references == self._script_unit_references
                and active_visual_reference
                == self._script_unit_visual_reference):
            return
        self._script_unit_references = list(references)
        self._script_unit_visual_reference = active_visual_reference
        with QSignalBlocker(self.model_tree):
            self.model_tree.clear()
            for reference in references:
                block = reference.block
                row_reference = (
                    active_visual_reference
                    if (active_visual_reference is not None
                        and active_visual_reference.block == block)
                    else reference)
                if (row_reference.three_ds_normal
                        or row_reference.base_normal):
                    owner_path = ""
                    display_name = Path(
                        row_reference.three_ds_normal or
                        row_reference.base_normal).stem
                else:
                    owner_path, display_name = self._model_info_by_vp.get(
                        row_reference.vp_normal, ("", ""))
                if not owner_path and row_reference.vp_normal is None:
                    obj = next((candidate for candidate in self.family.all_objects()
                                if candidate.skeleton is not None), None) \
                        if self.family is not None else None
                    if obj is not None:
                        owner_path, display_name = (
                            obj.owner_path, obj.display_name)
                vp_text = (
                    str(reference.vp_normal)
                    if reference.vp_normal is not None else "—")
                item = QTreeWidgetItem([
                    block.name or f"{block.kind} {block.object_id}",
                    vp_text,
                ])
                item.setData(0, Qt.ItemDataRole.UserRole, owner_path)
                item.setData(0, _MODEL_NAME_ROLE, display_name)
                item.setData(
                    0, _MODEL_VP_ROLE,
                    (reference.vp_normal,)
                    if reference.vp_normal is not None else ())
                item.setData(0, _SCRIPT_REFERENCE_ROLE, reference)
                item.setToolTip(0, reference.label)
                item.setToolTip(
                    1, f"VP {reference.vp_normal}"
                    if reference.vp_normal is not None
                    else "Visual override; no SET VP needed")
                item.setTextAlignment(
                    1,
                    Qt.AlignmentFlag.AlignRight
                    | Qt.AlignmentFlag.AlignVCenter)
                self.model_tree.addTopLevelItem(item)
            self.model_tree.resizeColumnToContents(1)
        self._filter_models(self.model_search.text())

    def _select_script_unit_item(self, reference: VehicleModelReference):
        """Select one unit row without re-entering its load path."""

        for index in range(self.model_tree.topLevelItemCount()):
            item = self.model_tree.topLevelItem(index)
            if item.data(0, _SCRIPT_REFERENCE_ROLE) == reference:
                with QSignalBlocker(self.model_tree):
                    self.model_tree.setCurrentItem(item)
                    item.setSelected(True)
                return item
        return self.model_tree.currentItem()

    def _restore_script_unit_item(self) -> None:
        """Return the selection to the loaded unit after a cancelled switch."""

        if self._active_model_reference is not None:
            self._select_script_unit_item(self._active_model_reference)

    def _confirm_script_unit_switch(self) -> bool:
        """Ask before replacing unexported work with another script unit."""

        if not self._has_unexported_work():
            return True
        answer = QMessageBox.question(
            self, "Unsaved Entity Designer changes",
            self._unsaved_changes_message("Load another unit"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        return answer == QMessageBox.StandardButton.Yes

    def _owner_for_vp(self, vp_id: int) -> str | None:
        """Owner path of the model a VP resolves to, in any list mode."""

        info = self._model_info_by_vp.get(int(vp_id))
        return info[0] if info is not None else None

    def _collision_shape_root_owner(self) -> str | None:
        """Return the owner subtree for the visual source actually loaded."""

        reference = self._active_visual_reference
        if reference is not None and (
                reference.three_ds_normal or reference.base_normal):
            return "root"
        state_vp = self._model_state_vps.get(self._model_state)
        if state_vp is not None and self._owner_for_vp(state_vp) is not None:
            return self._owner_for_vp(state_vp)
        if reference is not None and reference.vp_normal is not None:
            return self._owner_for_vp(reference.vp_normal)
        if (self._active_base_path is not None
                and self._active_base_path.suffix.casefold() == ".3ds"):
            return "root"
        return self._current_owner

    def _model_changed(self, current, _previous):
        if current is None or self.family is None:
            return
        reference = current.data(0, _SCRIPT_REFERENCE_ROLE)
        if reference is not None:
            if reference != self._active_model_reference:
                # Qt is still handling the click or key press that changed
                # this row. Loading here would rebuild the list in the middle
                # of that interaction: the scroll position resets and the
                # selection lands on an unrelated row. Load after it ends.
                QTimer.singleShot(
                    0, lambda: self._load_script_unit(reference))
                return
            # The loaded unit reselected: refresh the preview state only.
            self._show_model_item(current)
            self._sync_all()
            return
        self._show_model_item(current)
        self._set_modified()
        self._sync_all()

    def _load_script_unit(self, reference: VehicleModelReference) -> None:
        """Load one listed unit once the selection interaction has ended."""

        if reference == self._active_model_reference:
            return
        if self._modified and not self._confirm_script_unit_switch():
            self._restore_script_unit_item()
            return
        if not self.open_vehicle_script(
                self._active_script_path,
                object_id=reference.block.object_id,
                object_kind=reference.block.kind):
            self._restore_script_unit_item()

    def _show_model_item(self, item) -> None:
        """Present one left-list model or script unit in the viewport."""

        owner = item.data(0, Qt.ItemDataRole.UserRole)
        owner = owner or None
        self._current_owner = owner
        self._model_state = "normal"
        self._normal_state_owner = owner
        self._fill_model_state_combo()
        # Capture the new unit's orbit view; _sync_all restores the active tab.
        self.viewport.set_cockpit_preview_active(False)
        self.viewport.load_family(
            self.family, {owner} if owner else None,
            primary_owner=owner)
        self._fx_scope = None
        self._current_owner_base_bounds = (
            self.viewport._model_preview_base_owner_bounds.get(owner))
        self._viewport_owner = owner
        # Frame and capture Reset View using this unit's preview settings,
        # before _sync_all updates the remaining controls and overlays.
        self.viewport.set_model_preview_scale(
            self.project.model_scale_x, self.project.model_scale_y,
            self.project.model_scale_z)
        self.viewport.set_ground_alignment(
            self.project.target_category == VEHICLE,
            self.project.overeof_enabled, self.project.overeof)
        if owner:
            self.viewport.frame_owner(owner)
        else:
            self.viewport.frame_all()
        # Same contract as Model Editor: Reset View restores the exact camera
        # first presented for this selected model, not a later re-fit.
        self.viewport.capture_reset_view()
        # A model selected inside FX needs its own normal editing view.
        self._fx_edit_camera = (self.viewport._camera_state()
                                if self._is_fx_tab_active() else None)
        self.project.source_model = item.data(0, _MODEL_NAME_ROLE)
        self._refresh_collision_geometry_widget()
        with QSignalBlocker(self.toolbar_view_preset_combo):
            self.toolbar_view_preset_combo.setCurrentText("Current View")
        self._sync_animation_controls()
        self._sync_gizmo_camera()
        self._update_reset_view_controls()

    def _script_model_state_vps(self, text, block, reference) -> dict[str, int]:
        """State -> VP of every extra model state the unit defines and can show."""

        values = fx_visuals.read_values(text, block)
        normal_vp = reference.vp_normal
        states: dict[str, int] = {}
        for state, _label in fx_visuals.STATES:
            if state == "normal":
                continue
            raw = values.get(f"vp_{state}")
            if raw is None and state == "wait" and reference.vp_wait is not None:
                raw = str(reference.vp_wait)
            try:
                vp = int(str(raw), 0)
            except (TypeError, ValueError):
                continue
            if vp > 0 and vp != normal_vp and self._owner_for_vp(vp) is not None:
                states[state] = vp
        return states

    def _fill_model_state_combo(self) -> None:
        """List Normal plus the states of the loaded unit; Normal is selected."""

        combo = self.toolbar_model_state_combo
        labels = dict(fx_visuals.STATES)
        with QSignalBlocker(combo):
            combo.clear()
            combo.addItem("Normal", "normal")
            for state, vp in self._model_state_vps.items():
                combo.addItem(f"{labels[state]} (VP {vp})", state)
            combo.setCurrentIndex(0)
        combo.setEnabled(combo.count() > 1)

    def _fx_unit_in_model_state(self, unit):
        """The FX unit showing the Model state on screen as its normal model."""

        vp = self._model_state_vps.get(self._model_state)
        return unit if vp is None else replace(unit, normal=fx.VisualRef(vp))

    def _on_model_state_changed(self, _index) -> None:
        """Show another model state of the unit; the script stays untouched."""

        state = self.toolbar_model_state_combo.currentData() or "normal"
        if state == self._model_state or self.family is None:
            return
        if state == "normal":
            owner = self._normal_state_owner
        else:
            owner = self._owner_for_vp(self._model_state_vps.get(state, 0))
        if owner is None:
            return
        self._model_state = state
        self._current_owner = owner
        # Loading another model re-frames the camera on it, which makes the
        # ground seem to move. Only the model changes: keep the exact view.
        previous_camera = self.viewport._camera_state()
        previous_home_camera = self.viewport._home_camera_state
        self.viewport.load_family(
            self.family, {owner}, keep_camera=True, primary_owner=owner)
        self.viewport._set_camera_state(previous_camera)
        self.viewport._home_camera_state = previous_home_camera
        self._viewport_owner = owner
        self.viewport.set_model_preview_scale(
            self.project.model_scale_x, self.project.model_scale_y,
            self.project.model_scale_z)
        self.viewport.set_ground_alignment(
            self.project.target_category == VEHICLE,
            self.project.overeof_enabled, self.project.overeof)
        self._current_owner_base_bounds = (
            self.viewport._model_preview_base_owner_bounds.get(owner))
        self._refresh_collision_geometry_widget()
        self._sync_animation_controls()
        base_unit = getattr(self, "_fx_base_unit", None)
        if base_unit is not None and self._active_script_path is not None:
            # Visuals & FX draws the unit from its own phases: swap the model.
            self.fx_panel.unit = replace(
                self.fx_panel.unit,
                normal=self._fx_unit_in_model_state(base_unit).normal)
            self.fx_panel._rebuild_simulation()
        self._sync_all()
        self._sync_fx_preview()

    def _model_bounds(self):
        # Cockpit View may temporarily render vp_wait while collision data
        # remains authored against the selected vp_normal owner.
        bounds = self._current_owner_base_bounds
        if bounds is None:
            return self.viewport.local_scaled_owner_bounds(self._current_owner)
        x0, y0, z0, x1, y1, z1 = bounds
        scale = (
            self.project.model_scale_x,
            self.project.model_scale_y,
            self.project.model_scale_z,
        )
        a = (x0 * scale[0], y0 * scale[1], z0 * scale[2])
        b = (x1 * scale[0], y1 * scale[1], z1 * scale[2])
        return (
            min(a[0], b[0]), min(a[1], b[1]), min(a[2], b[2]),
            max(a[0], b[0]), max(a[1], b[1]), max(a[2], b[2]),
        )

    def _default_sphere(self, category: str) -> CollisionSphere:
        bounds = self._model_bounds()
        if bounds is None:
            return CollisionSphere(category, radius=1.0)
        x0, y0, z0, x1, y1, z1 = bounds
        radius = max(x1 - x0, y1 - y0, z1 - z0, 1.0) * 0.5
        return CollisionSphere(
            category,
            (x0 + x1) * 0.5,
            (y0 + y1) * 0.5,
            (z0 + z1) * 0.5,
            radius,
        )
