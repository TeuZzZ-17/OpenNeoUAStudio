"""CollisionEditorWindow mixin: opening archives and BASE/SKLT files, loading and overwriting the vehicle
script, game set selection and the cockpit preview model."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import (
    QSignalBlocker,
    Qt,
)
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QMessageBox,
)

from entity_designer import destruction_fx as fx
from entity_designer.dialogs import (
    ApplyScriptDialog,
    OpenScriptObjectDialog,
)
from entity_designer.fire_area import FireArea
from entity_designer.script_model import (
    _number,
    _public_dependency,
    _reference_for_visual_source,
    _resolve_data_asset_path,
    _resolve_script_visual_source,
    _SCRIPT_TAB_LABELS,
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
    import_type_icon_block,
    import_wireframe_block,
    import_fire_points_block,
    import_gun_points_block,
    import_overeof_block,
    import_turret_limits,
    OPENNEOUA,
    OPENNEOUA_SCRIPT_FILE_FILTER,
    read_script_file,
    runtime_vp_table,
    script_model_references,
    VEHICLE,
    WEAPON,
)
from entity_designer.shape import (
    read_collision_shape,
)
from studio.assets.family import AssetFamily, load_asset_family
from studio.exchange.portable import DirectoryIndex
from studio.assets.visproto import (
    reconstruct_embedded_vps,
    VPEmbeddedError,
)
from entity_designer.editor_common import (
    _vp_ids_per_model,
)


class _ScriptFilesMixin:
    """Part of CollisionEditorWindow; `self` is always the CollisionEditorWindow."""

    def open_base_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import BASE / SET.BAS / 3DS", str(self._last_directory),
            "Urban Assault assets (*.base *.bas *.BASE *.BAS *.3ds *.3DS);;"
            "All files (*)")
        if path:
            from studio.ui.widgets import loading_resources
            selected = Path(path)
            label = ("Loading BAS archive. Please wait…" if selected.suffix.casefold() == ".bas"
                     else "Importing model. Please wait…")
            with loading_resources(self, label):
                self.open_base(selected)

    def _set_model_browser_enabled(self, enabled: bool) -> None:
        """Enable model browsing only for the normal archive/SKLT workflow."""

        self._model_browser_enabled = bool(enabled)
        self._apply_model_browser_state()

    def _set_script_unit_mode(self, active: bool) -> None:
        """Switch the left list between archive models and script units.

        Script mode starts only after a script import and lists every loadable
        definition of the source script, so moving to another vehicle or
        weapon never needs a second import.
        """

        self._script_unit_mode = bool(active)
        if hasattr(self, "model_tree"):
            self.model_tree.setHeaderLabels(
                ["Unit", "VP"] if self._script_unit_mode
                else ["Internal path", "VP"])
            self.model_tree.headerItem().setTextAlignment(
                1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if hasattr(self, "model_search"):
            self.model_search.setPlaceholderText(
                "Search vehicle / weapon names" if self._script_unit_mode
                else "Search model names")
            self.model_search.setToolTip(
                "Filter the source script units by vehicle/weapon name."
                if self._script_unit_mode else
                "Filter models by name, internal path or owner path.")
        self._apply_model_browser_state()

    def _apply_model_browser_state(self) -> None:
        """One place for the left list's enabled state and its help text."""

        if self._script_unit_mode:
            enabled = True
            tree_tooltip = (
                "Select a vehicle or weapon to load its collision data from "
                "the source script.")
        elif self._model_browser_enabled:
            enabled = True
            tree_tooltip = "Browse models from the imported BAS/SKLT resource."
        else:
            enabled = False
            tree_tooltip = (
                "Model selection is locked while a script-imported vehicle "
                "or weapon is active. Import a BAS archive or SKLT normally "
                "to browse this list.")
        if hasattr(self, "model_tree"):
            self.model_tree.setEnabled(enabled)
            self.model_tree.setToolTip(tree_tooltip)
        if hasattr(self, "model_search"):
            self.model_search.setEnabled(enabled)

    def _current_visual_resource_is_open(self) -> bool:
        return bool(self.family is not None or self._active_base_path is not None)

    def _sync_close_archive_action(self) -> None:
        action = getattr(self, "close_bas_archive_action", None)
        if action is not None:
            action.setEnabled(self._current_visual_resource_is_open())

    def close_current_archive(self) -> None:
        """Close the current resource and clear its whole authoring session.

        Collision data belongs to the unit/model currently being edited.
        Closing that resource must therefore clear the linked script, spheres,
        shape, Fire Points, Gun Points, cockpit data and undo/redo history too.
        """

        if not self.fx_panel.confirm_text_changes():
            return
        if not self._current_visual_resource_is_open():
            self._sync_close_archive_action()
            return

        process = self._collision_bake_process
        if process is not None:
            self._collision_bake_cancelled = True
            process.kill()
            process.waitForFinished(3000)
            if self._collision_bake_process is process:
                self._collision_bake_process = None
            process.deleteLater()
        self._cleanup_collision_bake_tempdir()
        self._collision_bake_context = {}
        self._collision_bake_cancelled = False

        self.viewport.clear()
        self.family = None
        self._vp_embedded = None
        self._vp_table = None
        self._vp_table_source = ""
        self._active_base_path = None
        self._current_owner = None
        self._current_owner_base_bounds = None
        self._viewport_owner = None
        self._clear_script_link()

        self.project = CollisionProject()
        self._new_gun_point_scheme = "unit"
        self._capture_loaded_gun_points()
        self._capture_loaded_cockpit_camera()
        self._capture_tab_reset_baseline()
        self._selected = -1
        self._selected_spheres.clear()
        self._selected_fire_point = -1
        self._selected_gun_point = -1
        self._undo.clear()
        self._redo.clear()
        self._vehicle_preview_active_edits.clear()
        self._turret_slider_active_edits.clear()
        self._gun_direction_slider_active = False

        self._collision_geometry_cache_key = None
        self._collision_geometry_components = ()
        self._collision_geometry_triangles_by_owner = {}
        self._collision_geometry_owners = ()
        self._collision_geometry_fingerprints.clear()
        widget = getattr(self, "collision_geometry_widget", None)
        if widget is not None:
            widget.set_geometry(None, None, (), {}, None)
        self._set_collision_geometry_preview(None)

        self.model_tree.clear()
        self._model_info_by_vp = {}
        self._script_unit_references = []
        self._script_unit_visual_reference = None
        self._set_script_unit_mode(False)
        if hasattr(self, "model_search"):
            self.model_search.clear()
        self.source_label.setText("No source loaded.")
        self._set_model_browser_enabled(False)
        self._set_modified(False)
        self._sync_animation_controls()
        self._sync_all()
        self._update_window_title()
        self._sync_close_archive_action()
        self.statusBar().showMessage(
            "Current resource closed. Related Entity Designer data cleared.",
            5000)

    def _current_set_bas_path(self) -> Path | None:
        if self._vp_embedded is None:
            return None
        return Path(self._vp_embedded.source_path)

    @staticmethod
    def _load_family(path: Path, **kwargs) -> AssetFamily:
        """Load a BASE family after rereading the folders it searches.

        Folder listings are cached for the session, so a texture or animation
        added beside the BASE after its first load would stay unseen (and the
        set's copy would be used instead of the external file).
        """

        if path.name.casefold() != "set.bas":
            for root in (path.parent, path.parent.parent):
                DirectoryIndex.get(root, refresh=True)
        return _public_dependency("load_asset_family", load_asset_family)(path, **kwargs)

    def _resource_set_bas_path(self) -> Path | None:
        """SET.BAS that holds the textures/animations of a loose BASE.

        A loose BASE (base_normal, base_* of FX) names resources such as
        FAERROBO.ILBM or 29119601.ANM that only exist inside the SET.BAS; the
        engine finds them through the current set, which is the one selected
        in the game browser (Change Set only switches it, it is always there).
        """

        from studio.assets.catalog import set_assets
        try:
            path = set_assets(self.game_object_browser.set_number).setbas_path
        except (ValueError, OSError, RuntimeError):
            path = None
        if path is not None:
            return Path(path)
        current = self._current_set_bas_path()
        if current is not None:
            return current
        family = self.family
        return getattr(family, "setbas_path", None) if family is not None else None

    def _loose_unit_uses_set(self) -> bool:
        """True when the unit is a loose BASE of a script and a set is active.

        Its effects name SET VPs, which the loose BASE family does not hold.
        """

        return (self._vp_embedded is None and self._active_script_path is not None
                and self._resource_set_bas_path() is not None)

    def _fx_set_vp_source(self, vp: int):
        """(family, owner) of a SET VP for the effects of a loose-BASE unit."""

        # Called for every item and particle of every frame: the set is looked
        # up once and kept until the script link or the set changes.
        if self._vp_embedded is not None or self._active_script_path is None:
            return None
        if self._fx_set_cache is None:
            path = self._resource_set_bas_path()
            self._fx_set_cache = (path, self._load_fx_set_vps(path)
                                  if path is not None else (None, {}))
        family, owners = self._fx_set_cache[1]
        owner = owners.get(int(vp))
        return (family, owner) if owner is not None else None

    @staticmethod
    def _load_fx_set_vps(path: Path):
        """The SET.BAS family and the owner of each VP, like opening the set."""

        try:
            embedded = reconstruct_embedded_vps(path)
            table, _source, _warnings = runtime_vp_table(path, embedded)
            family = _public_dependency("load_asset_family", load_asset_family)(path)
        except (VPEmbeddedError, OSError, ValueError, CollisionScriptError):
            return None, {}
        owners: dict[int, str] = {}
        models = [obj for obj in family.all_objects() if obj.skeleton is not None]
        for obj, vp_ids in zip(models, _vp_ids_per_model(family, embedded, table)):
            for vp_id in vp_ids:
                owners.setdefault(vp_id, obj.owner_path)
        return family, owners

    def _clear_script_link(self) -> None:
        self._active_script_path = None
        self._fx_set_cache = None
        if hasattr(self, "fx_panel"):
            self.fx_panel.set_context(fx.CONTEXT_VEHICLE, fx.FxDocument())
        self._active_script_kind = ""
        self._active_script_id = None
        self._active_model_reference = None
        self._active_visual_reference = None
        self._model_state_vps = {}
        self._loaded_gun_points_enabled = False
        self._loaded_gun_points = []
        self._loaded_unit_gun_default_icon = ""
        self._loaded_gun_point_scheme = "unit"
        self._loaded_turret_limits = {}
        self._loaded_cockpit_camera_enabled = False
        self._loaded_cockpit_camera_offset = (0.0, 0.0, 0.0)

    def reload_game_resources(self):
        from studio.assets.catalog import clear_visual_cache
        clear_visual_cache()
        self.game_object_browser.reload()

    def _game_set_changed(self, number):
        from studio.ui.widgets import loading_resources
        self._fx_set_cache = None
        with loading_resources(self, f"Loading Set {number}. Please wait…"):
            if self._load_game_set(number):
                from studio.ui.shell import remember_set
                remember_set(number)

    def _load_game_set(self, number):
        current = self.game_object_browser.tree.currentItem()
        if current is not None:
            from studio.ui.browsers import GAME_OBJECT_ROLE
            obj = current.data(0, GAME_OBJECT_ROLE)
            if obj is not None:
                return self._open_game_object(obj)
        return True

    def _open_game_object(self, obj):
        if self._modified and not self._confirm_script_unit_switch():
            return False
        try:
            from studio.assets.catalog import set_assets
            assets = set_assets(self.game_object_browser.set_number)
            result = self.open_vehicle_script(obj.source, object_id=obj.object_id,
                object_kind=f"new_{obj.kind}", set_bas_path=assets.setbas_path,
                resolved_text=obj.effective_text)
            return result is not False
        except (OSError, ValueError, RuntimeError) as exc:
            QMessageBox.warning(self, "Game object unavailable", str(exc))
            return False

    def open_vehicle_script_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import vehicle / weapon definition",
            str(self._last_directory),
            OPENNEOUA_SCRIPT_FILE_FILTER)
        if path:
            self.open_vehicle_script(path)

    def open_vehicle_script(
            self, path: str | Path, *, vehicle_id: int | None = None,
            object_id: int | None = None, object_kind: str | None = None,
            set_bas_path: str | Path | None = None,
            resolved_text: str | None = None) -> bool:
        """Load one ``new_vehicle`` or ``new_weapon`` through ``vp_normal``.

        ``vehicle_id`` remains as a compatibility argument and implies
        ``new_vehicle``. New callers may use ``object_id`` plus ``object_kind``.
        The linked asset and script are read-only until an explicit Apply or
        Overwrite action is confirmed.
        """

        script_path = Path(path)
        if not self.fx_panel.confirm_text_changes():
            return False
        try:
            text, _encoding, _bom = read_script_file(script_path)
            if resolved_text is not None:
                text = resolved_text
            self._fx_source_from_catalog = resolved_text is not None
            references = script_model_references(text)
            script_turret_limits = import_turret_limits(text)
        except (OSError, UnicodeError, CollisionScriptError) as exc:
            QMessageBox.critical(
                self, "Script object load failed", str(exc))
            return False
        if not references:
            QMessageBox.warning(
                self, "No resolvable definition",
                "No complete new_vehicle or new_weapon block with an active "
                "vp_normal was found. modify_* inheritance is not guessed.")
            return False

        reference = None
        requested_id = object_id
        requested_kind = object_kind.lower() if object_kind else None
        if vehicle_id is not None:
            requested_id = int(vehicle_id)
            requested_kind = "new_vehicle"
        if requested_id is not None:
            matches = [
                candidate for candidate in references
                if candidate.block.object_id == int(requested_id)
                and (requested_kind is None
                     or candidate.block.kind == requested_kind)
            ]
            if len(matches) != 1:
                kind_text = requested_kind or "new_vehicle/new_weapon"
                QMessageBox.warning(
                    self, "Definition not found",
                    f"Expected exactly one {kind_text} {requested_id} with "
                    "vp_normal.")
                return False
            reference = matches[0]

        current_set = self._current_set_bas_path()
        chosen_set = Path(set_bas_path) if set_bas_path is not None else current_set
        if reference is None or (
                reference.vp_normal is not None and chosen_set is None
                and not (reference.three_ds_normal or reference.base_normal)):
            dialog = OpenScriptObjectDialog(
                self, script_path, references, current_set)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return False
            reference, chosen_set = dialog.selected()
        visual_source, override, visual_override, visual_warnings = (
            _resolve_script_visual_source(
                reference, script_path, chosen_set, current_set))
        active_visual_reference = _reference_for_visual_source(
            reference, visual_source)
        chosen_asset: Path
        if visual_source in {"3ds_normal", "base_normal"}:
            if override is None:
                QMessageBox.critical(
                    self, "Script visual unavailable",
                    "The selected script visual could not be resolved.")
                return False
            chosen_asset = override
            if (self._active_base_path is None
                    or self._active_base_path.resolve(strict=False)
                    != chosen_asset.resolve(strict=False)):
                if not self.open_base(chosen_asset, setbas=chosen_set):
                    return False
            source_model = chosen_asset.stem
        else:
            if visual_source == "unavailable":
                QMessageBox.critical(
                    self, "Visual source unavailable",
                    "No usable 3DS, BASE, or SET VP visual source was found. "
                    + "\n".join(visual_warnings))
                return False
            if chosen_set is None:
                fallback_set, _ = QFileDialog.getOpenFileName(
                    self, "Select SET.BAS for vp_normal fallback",
                    str(script_path.parent),
                    "SET.BAS archive (SET.BAS *.bas *.BAS);;All files (*)")
                if not fallback_set:
                    return False
                chosen_set = Path(fallback_set)
            chosen_set = Path(chosen_set)
            if (current_set is None
                    or current_set.resolve(strict=False)
                    != chosen_set.resolve(strict=False)):
                if not self.open_base(chosen_set):
                    return False
            if self._vp_embedded is None or self._vp_table is None:
                QMessageBox.critical(
                    self, "VP database unavailable",
                    "The selected archive did not expose a SET.BAS VP table.")
                return False
            if not 0 <= reference.vp_normal < len(self._vp_table.entries):
                QMessageBox.critical(
                    self, "VP out of range",
                    f"vp_normal {reference.vp_normal} is outside the SET.BAS "
                    f"database (0..{len(self._vp_table.entries) - 1}).")
                return False
            model_info = self._model_info_by_vp.get(reference.vp_normal)
            if model_info is None:
                entry = self._vp_table.entry(reference.vp_normal)
                QMessageBox.critical(
                    self, "VP model unavailable",
                    f"VP {reference.vp_normal} resolves to "
                    f"{entry.base_name}, but its SKLT could not be selected "
                    "from the loaded SET.BAS family. The active VP table "
                    "may reference a loose BASE that is not embedded in this SET.")
                return False
            _owner_path, source_model = model_info
            chosen_asset = chosen_set

        block = reference.block
        target_category = (
            WEAPON if block.kind == "new_weapon" else VEHICLE)
        try:
            legacy, compounds, warnings = import_collision_block(
                text, block, OPENNEOUA)
            warnings.extend(visual_warnings)
            shape_path = import_collision_shape_reference(text, block)
            if target_category == VEHICLE:
                overeof_enabled, overeof = import_overeof_block(text, block)
                (fire_enabled, fire_x, fire_y, fire_z,
                 num_weapons, num_weapons_max) = import_fire_points_block(
                    text, block)
                (cockpit_enabled, cockpit_x, cockpit_y,
                 cockpit_z) = import_cockpit_camera_block(text, block)
                (gun_enabled, gun_points,
                 unit_gun_default_icon) = import_gun_points_block(text, block)
                fire_area = FireArea()
            else:
                overeof_enabled, overeof = False, 0.0
                fire_enabled, fire_x, fire_y, fire_z = False, 0.0, 0.0, 0.0
                num_weapons, num_weapons_max = 1, 1
                cockpit_enabled, cockpit_x, cockpit_y, cockpit_z = (
                    False, 0.0, 0.0, 0.0)
                gun_enabled, gun_points, unit_gun_default_icon = False, [], ""
                fire_area = import_fire_area_block(text, block)
        except CollisionScriptError as exc:
            QMessageBox.critical(
                self, "Script parameter import failed", str(exc))
            return False

        fallback_name = (
            "Weapon" if target_category == WEAPON else "Vehicle")
        shape = None
        shape_file_path = ""
        if shape_path:
            shape_file = _resolve_data_asset_path(
                shape_path, script_path, chosen_asset, chosen_set)
            if shape_file is None:
                warnings.append(
                    f"collision_shape file was not found: {shape_path}")
            else:
                try:
                    shape = read_collision_shape(shape_file)
                    shape_file_path = str(shape_file)
                except (OSError, ValueError) as exc:
                    warnings.append(
                        f"collision_shape could not be read: {exc}")

        self.project = CollisionProject(
            name=block.name or f"{fallback_name}_{block.object_id}",
            source_model=source_model,
            source_base=chosen_asset.name,
            target_category=target_category,
            model_scale_x=reference.scale_x,
            model_scale_y=reference.scale_y,
            model_scale_z=reference.scale_z,
            model_rotation_x=reference.rotation_x,
            model_rotation_y=reference.rotation_y,
            model_rotation_z=reference.rotation_z,
            overeof_enabled=overeof_enabled,
            overeof=overeof,
            fire_points_enabled=fire_enabled,
            fire_x=fire_x,
            fire_y=fire_y,
            fire_z=fire_z,
            num_weapons=num_weapons,
            num_weapons_max=num_weapons_max,
            gun_points_enabled=gun_enabled,
            gun_points=gun_points,
            unit_gun_default_icon=unit_gun_default_icon,
            turret_limits=script_turret_limits if target_category == VEHICLE else {},
            cockpit_camera_enabled=cockpit_enabled,
            cockpit_camera_offset_x=cockpit_x,
            cockpit_camera_offset_y=cockpit_y,
            cockpit_camera_offset_z=cockpit_z,
            briefing_size_enabled=import_briefing_size_block(text, block)[0] if target_category == VEHICLE else False,
            briefing_size=import_briefing_size_block(text, block)[1] if target_category == VEHICLE else 0.0,
            briefing_xyz_enabled=import_briefing_xyz_block(text, block)[0] if target_category == VEHICLE else False,
            briefing_x=import_briefing_xyz_block(text, block)[1] if target_category == VEHICLE else 0.0,
            briefing_y=import_briefing_xyz_block(text, block)[2] if target_category == VEHICLE else 0.0,
            briefing_z=import_briefing_xyz_block(text, block)[3] if target_category == VEHICLE else 0.0,
            fire_area=fire_area,
            type_icon=(import_type_icon_block(text, block)
                       if target_category == VEHICLE else 0),
            wireframe=import_wireframe_block(text, block),
            legacy=legacy,
            compound=compounds,
            collision_shape=shape,
            collision_shape_path=shape_path,
            collision_shape_file_path=shape_file_path,
        )
        imported_gun_schemes = gun_point_families_in_block(text, block)
        if len(imported_gun_schemes) == 1:
            # A script that already uses one family supplies the missing
            # semantic context that a bare SET.BAS model cannot provide.
            self._new_gun_point_scheme = next(iter(imported_gun_schemes))
        elif "unit" in imported_gun_schemes:
            # Mixed scripts remain mixed. Prefer the generic OpenNeoUA family for
            # a brand-new point until the user selects a specific existing one.
            self._new_gun_point_scheme = "unit"
        self._capture_loaded_gun_points()
        self._capture_loaded_cockpit_camera()
        self._capture_tab_reset_baseline()
        self._active_script_path = script_path
        self._active_script_kind = block.kind
        self._active_script_id = block.object_id
        self._active_model_reference = reference
        self._active_visual_reference = active_visual_reference
        self._active_base_path = chosen_asset
        if hasattr(self, "cockpit_model_state_combo"):
            with QSignalBlocker(self.cockpit_model_state_combo):
                self.cockpit_model_state_combo.setCurrentIndex(0)
        self._selected = 0 if self.project.spheres() else -1
        self._selected_spheres = (
            {self._selected} if self._selected >= 0 else set())
        self._selected_fire_point = -1
        self._selected_gun_point = -1
        self._undo.clear()
        self._redo.clear()
        self._last_directory = script_path.parent
        # The left list becomes the unit browser of this script: every other
        # loadable definition is one click away, without importing again.
        self._set_script_unit_mode(True)
        resolved_unit_reference = (
            active_visual_reference
            if active_visual_reference != reference else None)
        self._fill_script_units(
            references, active_visual_reference=resolved_unit_reference)
        self._model_state_vps = self._script_model_state_vps(text, block, reference)
        self._show_model_item(self._select_script_unit_item(reference))
        self._load_fx_context(text, block, reference)
        self._fire_area_load_defaults(text, block)
        self._set_modified(False)
        self.source_label.setText(
            (f"Visual override: {visual_override}" if visual_override
             else f"VP {reference.vp_normal}: {source_model or '<model>'}")
            + "\n"
            f"{script_path.name} — {block.kind} {block.object_id}")
        self._sync_all()
        message = (
            f"Loaded {block.kind} {block.object_id} using "
            f"{visual_override if visual_override else 'VP ' + str(reference.vp_normal)} "
            f"with preview scale {_number(reference.scale_x)}/"
            f"{_number(reference.scale_y)}/{_number(reference.scale_z)}.")
        if warnings:
            message += " " + " ".join(warnings)
        self.statusBar().showMessage(message, 7000)
        return True

    def _cockpit_requested_vp(self) -> tuple[int | None, str]:
        reference = self._active_model_reference
        if reference is None or self.project.target_category != VEHICLE:
            return None, ""
        if reference.three_ds_normal or reference.base_normal:
            return None, ""
        # The toolbar Model state wins: Cockpit View shows the state on screen.
        toolbar_vp = self._model_state_vps.get(self._model_state)
        if toolbar_vp is not None:
            return toolbar_vp, f"vp_{self._model_state}"
        state = (
            self.cockpit_model_state_combo.currentData()
            if hasattr(self, "cockpit_model_state_combo") else "normal")
        if state == "idle" and reference.vp_wait is not None:
            return reference.vp_wait, "vp_wait"
        return reference.vp_normal, "vp_normal"

    def _sync_cockpit_preview_model(self) -> None:
        """Load the runtime VP state without changing authored project data."""

        if self.family is None:
            if hasattr(self, "cockpit_preview_info"):
                self.cockpit_preview_info.setText("")
            return

        cockpit_active = self._is_cockpit_tab_active()
        desired_owner = self._current_owner
        info = ""
        if cockpit_active and self._active_model_reference is not None:
            vp_id, state_name = self._cockpit_requested_vp()
            owner = self._owner_for_vp(vp_id) if vp_id is not None else None
            if owner is None and state_name == "vp_wait":
                vp_id = self._active_model_reference.vp_normal
                state_name = "vp_normal fallback"
                owner = self._owner_for_vp(vp_id)
            if owner is not None:
                desired_owner = owner
                base_name = ""
                if (self._vp_table is not None and vp_id is not None
                        and 0 <= vp_id < len(self._vp_table.entries)):
                    base_name = self._vp_table.entry(vp_id).base_name
                info = f"Preview: {state_name} {vp_id}"
                if base_name:
                    info += f" — {base_name}"
            else:
                info = (
                    "Preview: script VP unavailable in the loaded archive; "
                    "using the selected model.")
        elif cockpit_active:
            info = (
                "Preview: selected model. Import a vehicle script to switch "
                "between its vp_wait and vp_normal states automatically.")

        if desired_owner is not None and desired_owner != self._viewport_owner:
            self.viewport.load_family(
                self.family, {desired_owner}, keep_camera=True,
                primary_owner=desired_owner)
            self.viewport.set_model_preview_scale(
                self.project.model_scale_x,
                self.project.model_scale_y,
                self.project.model_scale_z)
            self.viewport.set_ground_alignment(
                self.project.target_category == VEHICLE,
                self.project.overeof_enabled,
                self.project.overeof)
            self._viewport_owner = desired_owner
            self._sync_animation_controls()

        if hasattr(self, "cockpit_preview_info"):
            self.cockpit_preview_info.setText(info)

    def _cockpit_preview_setting_changed(self, *_args) -> None:
        if hasattr(self, "cockpit_runtime_aspect_combo"):
            self.viewport.set_cockpit_runtime_aspect(
                self.cockpit_runtime_aspect_combo.currentData())
        self._sync_cockpit_preview_model()


    def overwrite_loaded_script(self):
        if (self._active_script_path is None
                or self._active_script_id is None
                or not self._active_script_kind):
            QMessageBox.information(
                self, "No linked script definition",
                "Import a vehicle or weapon from a script first. For an "
                "arbitrary target, use Apply to Script.")
            return
        expected_category = (
            WEAPON if "weapon" in self._active_script_kind else VEHICLE)
        if self.project.target_category != expected_category:
            QMessageBox.warning(
                self, "Target category mismatch",
                "The linked script definition and current project category "
                "do not match.")
            return
        if (self.project.collision_shape is not None
                and not self.project.collision_shape_file_path):
            QMessageBox.warning(
                self, "Export Collision Shape first",
                "The current Collision Shape was generated but has not been "
                "exported yet. It cannot be applied to the linked vehicle or "
                "weapon until it has been exported.\n\nExport the Collision "
                "Shape first and then use Overwrite to Existing Script, or "
                "delete the Collision Shape if you do not want to use it.")
            return
        if not self._validate_for_output():
            return
        dialog = ApplyScriptDialog(
            self, self.project,
            replace_all_managed=True,
            initial_path=self._active_script_path,
            initial_kind=self._active_script_kind,
            initial_id=self._active_script_id,
            changed_groups=self._changed_script_tabs(),
        )
        dialog.path_edit.setReadOnly(True)
        dialog.search_edit.setEnabled(False)
        dialog.block_combo.setEnabled(False)
        dialog.kind_combo.setEnabled(False)
        dialog.id_spin.setEnabled(False)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            baseline_refreshed = self._refresh_linked_script_baseline(
                dialog.applied_groups)
            if not baseline_refreshed:
                self.statusBar().showMessage(
                    "Definition overwritten, but the source baseline could "
                    "not be refreshed. Reload the script before the next "
                    "overwrite.", 9000)
                return
            remaining = self._changed_script_tabs()
            if remaining:
                names = ", ".join(
                    _SCRIPT_TAB_LABELS[group]
                    for group in _SCRIPT_TAB_ORDER if group in remaining)
                self.statusBar().showMessage(
                    "Definition overwritten. Still different from the script: "
                    f"{names}.", 9000)
            else:
                self.statusBar().showMessage(
                    "Loaded definition overwritten.", 7000)
