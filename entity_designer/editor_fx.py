"""CollisionEditorWindow mixin: Destruction FX preview."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import fields, replace
from pathlib import Path

from PySide6.QtCore import (
    QPointF,
    QSignalBlocker,
    QTimer,
)

from entity_designer import destruction_fx as fx
from entity_designer.script_model import (
    _data_root_for_path,
    _load_3ds_family,
    _resolve_data_asset_path,
    _resolve_patch_target_block,
    CollisionScriptError,
    effective_runtime_radius,
    find_script_blocks,
    import_collision_block,
    import_fire_area_block,
    import_fire_points_block,
    import_gun_points_block,
    import_cockpit_camera_block,
    import_briefing_size_block,
    import_briefing_xyz_block,
    import_type_icon_block,
    import_wireframe_block,
    import_overeof_block,
    OPENNEOUA,
    read_script_file,
    runtime_vp_table,
    script_model_references,
    ScriptBlock,
    VEHICLE,
    VehicleModelReference,
)
from entity_designer.shape import (
    visual_transform_point,
)
from studio.assets.visproto import (
    reconstruct_embedded_vps,
)
from entity_designer.editor_common import (
    _fx_item_state,
)


class _FxPreviewMixin:
    """Part of CollisionEditorWindow; `self` is always the CollisionEditorWindow."""

    # -- Destruction FX preview -------------------------------------------------------

    def _is_fx_tab_active(self) -> bool:
        return (hasattr(self, "fx_tab_index")
                and self.properties_tabs.currentIndex() == self.fx_tab_index)

    def _fx_unit_radius(self) -> float:
        # StartDestFX scales dest_fx offsets by the unit's vanilla radius.
        legacy = self.project.legacy
        return (legacy.radius if legacy is not None
                else effective_runtime_radius(self.project))

    def _fx_unit_scale_rotation(self):
        """Preview scale and Euler rotation (degrees) of the unit."""

        return ((self.project.model_scale_x, self.project.model_scale_y,
                 self.project.model_scale_z),
                (self.project.model_rotation_x, self.project.model_rotation_y,
                 self.project.model_rotation_z))


    def _fx_unit_transform(self, scale_rotation=None):
        """Preview scale/rotation of the unit as a matrix (static preview)."""

        scale, rotation = scale_rotation or self._fx_unit_scale_rotation()
        columns = [visual_transform_point(axis, scale, rotation)
                   for axis in ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))]
        return tuple(tuple(columns[column][row] for column in range(3))
                     for row in range(3))

    def _build_fx_catalog(self, text: str, objects=None):
        """FX vehicles for dest_fx previews: game catalog plus this script."""

        catalog: dict[int, fx.LegacyFxVehicle] = {}
        if objects is None:
            objects = getattr(self.game_object_browser, "objects", None) or []
        for obj in objects:
            if obj.kind == "vehicle":
                catalog[obj.object_id] = fx.legacy_vehicle_from_text(
                    obj.object_id, obj.effective_text, str(obj.source))
        lines = text.splitlines()
        for block in find_script_blocks(text):
            if block.kind == "new_vehicle" and block.complete                     and block.object_id not in catalog:
                body = "\n".join(lines[block.start_line:block.end_line + 1])
                catalog[block.object_id] = fx.legacy_vehicle_from_text(
                    block.object_id, body)
        return catalog

    def _build_fx_weapons(self, text: str, objects=None):
        """Weapons a cluster can spawn: the game catalog plus this script."""

        weapons: dict[int, fx.WeaponDef] = {}
        if objects is None:
            objects = getattr(self.game_object_browser, "objects", None) or []
        for obj in objects:
            if obj.kind == "weapon":
                weapons[obj.object_id] = fx.weapon_def_from_text(
                    obj.object_id, obj.effective_text)
        lines = text.splitlines()
        for block in find_script_blocks(text):
            if block.kind == "new_weapon" and block.complete:
                body = "\n".join(lines[block.start_line:block.end_line + 1])
                weapons[block.object_id] = fx.weapon_def_from_text(
                    block.object_id, body + "\n")
        return weapons

    def _build_fx_debuffs(self, text: str):
        """Debuff profiles of the game scripts plus this script."""

        debuffs: dict[int, fx.DebuffSpec] = {}
        try:
            from map_editor import bootstrap
            from map_editor.core.script_catalog import script_texts
            install = bootstrap.installation()
            if install is not None:
                for body in script_texts(install.folder("scripts")):
                    debuffs.update(fx.debuff_profiles_from_text(body))
        except (OSError, ValueError, RuntimeError, TypeError):
            pass
        debuffs.update(fx.debuff_profiles_from_text(text))
        return debuffs

    def _build_fx_decals(self, text: str):
        """Ground decal profiles (new_decal) of the game scripts plus this script."""

        decals: dict[int, fx.DecalProfile] = {}
        try:
            from map_editor import bootstrap
            from map_editor.core.script_catalog import script_texts
            install = bootstrap.installation()
            if install is not None:
                for body in script_texts(install.folder("scripts")):
                    decals.update(fx.decal_profiles_from_text(body))
        except (OSError, ValueError, RuntimeError, TypeError):
            pass
        decals.update(fx.decal_profiles_from_text(text))
        return decals

    def _fx_unit_phases(self, text: str, block: ScriptBlock,
                        reference: VehicleModelReference, *,
                        carrier: bool = False) -> fx.UnitPhases:
        """Death models of the definition, resolved like the engine."""

        def value(key: str) -> str:
            return fx.top_level_value(
                text, block.start_line, block.end_line, key) or ""

        def state(name: str) -> fx.VisualRef:
            # Engine defaults for visual states that are not in this script.
            vp = (reference.vp_normal or 0 if name == "normal"
                  else fx.script_int(value(f"vp_{name}"),
                                     {"fire": 1, "wait": 3, "genesis": 5,
                                      "dead": 4, "megadeth": 2}.get(name, 0)))
            return fx.VisualRef(vp=vp, mesh3ds=value(f"3ds_{name}"),
                                base_path=value(f"base_{name}"))

        weapon = block.kind.endswith("weapon")
        model = value("model").casefold()
        scale_rotation = (
            ((reference.scale_x, reference.scale_y, reference.scale_z),
             (reference.rotation_x, reference.rotation_y, reference.rotation_z))
            if carrier else self._fx_unit_scale_rotation())
        scale, euler = scale_rotation
        overeof = (self.project.overeof if self.project.overeof_enabled else 0.0)
        if carrier:
            overeof = fx._full_float(value("overeof")) or 0.0
        normal = state("normal")
        fire = state("fire")
        wait = state("wait")
        genesis = state("genesis")
        main_visuals = (normal, fire, wait) if weapon else (normal, fire, wait, genesis)
        look = fx.unit_look(
            fx.top_level_values(text, block.start_line, block.end_line),
            weapon=weapon)
        unit = fx.UnitPhases(
            normal=normal, dead=state("dead"), megadeth=state("megadeth"),
            main_visuals=main_visuals, model=model, weapon=weapon,
            transform=self._fx_unit_transform(scale_rotation), overeof=overeof,
            scale=scale, euler=euler, **look)
        if unit.flies:
            unit = replace(unit, altitude=fx.DEFAULT_FLIGHT_ALTITUDE)
        elif weapon:
            # Artillery shells fall from their arc height; other projectiles use the preview default.
            unit = replace(unit, altitude=unit.altitude or fx.DEFAULT_PROJECTILE_ALTITUDE)
        return unit

    def wireframe_export_name(self) -> str:
        """Name the exported wireframe after the vehicle or weapon being edited."""

        return self.project.name

    def wireframe_exported(self, path: Path) -> str:
        """Point the definition's ``wireframe`` to a newly exported file.

        The engine reads the value as a path inside Data, so a file saved
        elsewhere cannot be referenced and the script value stays as it is.
        """

        from entity_designer.script_model import _data_root_for_path

        data_root = _data_root_for_path(
            self._active_script_path, self._active_base_path, path)
        try:
            value = path.resolve().relative_to(data_root.resolve()).as_posix()
        except (AttributeError, ValueError):
            return (f"Wireframe exported to {path}. It is outside the game Data "
                    "folder, so the script wireframe was not changed.")
        if any(character.isspace() for character in value):
            return (f"Wireframe exported to {path}. Its path has spaces, so the "
                    "script wireframe was not changed.")
        if value != self.project.wireframe:
            self._push_undo()
            self.project.wireframe = value
            self._set_modified()
            self._sync_all()
        return (f"Wireframe exported: wireframe = {value}. Overwrite, Apply to "
                "Script or Copy writes it into the script.")

    def _sync_wireframe_current(self, text: str, block: ScriptBlock) -> None:
        """Give the Wireframe tab the file named by ``wireframe =`` of the loaded definition."""

        from entity_designer.wireframe_panel import resolve_wireframe_path

        value = fx.top_level_value(text, block.start_line, block.end_line, "wireframe")
        self.wireframe_panel.set_current_wireframe(
            resolve_wireframe_path(
                value, self._active_script_path, self._active_base_path))

    def _load_fx_context(self, text: str, block: ScriptBlock,
                         reference: VehicleModelReference, *,
                         keep_view: bool = False, objects=None) -> None:
        """Show one loaded definition in the Destruction FX preview."""

        from entity_designer.fire_area_units import mirrored_single_fire_point
        self._fire_point_mirrored = mirrored_single_fire_point(
            fx.top_level_values(text, block.start_line, block.end_line))
        self._prepare_collision_tracer(
            fx.top_level_values(text, block.start_line, block.end_line))
        self._fx_catalog = self._build_fx_catalog(text, objects)
        self._sync_wireframe_current(text, block)
        if not keep_view:
            self._fx_changed_assets.clear()
        if not keep_view or self._fx_source_from_catalog:
            self._fx_script_sources = set()
            if self._fx_source_from_catalog:
                from map_editor import bootstrap
                from map_editor.core.script_catalog import script_texts
                scripts = bootstrap.installation().folder("scripts")
                self._fx_script_sources.update(str((scripts / source).resolve())
                                               for _body, source in script_texts(scripts, with_source=True))
                self._fx_script_sources.update(str(path.resolve()) for path in
                                               (bootstrap.find_ci(scripts, "Startup.cfg"),
                                                bootstrap.find_ci(scripts, "startup.scr"))
                                               if path is not None and path.is_file())
        self._fx_base_unit = self._fx_unit_phases(text, block, reference)
        unit = self._fx_unit_in_model_state(self._fx_base_unit)
        kamikaze = unit.weapon and unit.model == "kamikaze"
        candidates = objects if objects is not None else self.game_object_browser.objects
        origins = [(self._active_script_path, block.start_line, block.end_line)]
        if self._fx_source_from_catalog:
            source = next((obj for obj in candidates
                           if obj.object_id == block.object_id
                           and f"new_{obj.kind}" == block.kind), None)
            origins = getattr(source, "body_sources", ())
        preview_text, preview_block = text, block
        panel = self.fx_panel
        previous_carrier = getattr(self, "_fx_preview_carrier", None)
        self._fx_preview_carrier = None
        if kamikaze:
            carriers = self._fx_kamikaze_carriers(block.object_id, candidates)
            combo = panel.carrier_combo
            choice = combo.currentData() if keep_view and panel.kamikaze else None
            with QSignalBlocker(combo):
                combo.clear()
                for obj, _body, _ref in carriers:
                    combo.addItem(f"Carrier: {obj.name} ({obj.object_id})", obj.object_id)
                combo.addItem("Payload effects" if carriers else "Payload effects (no carrier found)", None)
                if combo.findData(choice) >= 0:
                    combo.setCurrentIndex(combo.findData(choice))
            if combo.currentIndex() < len(carriers):
                obj, preview_text, carrier_ref = carriers[combo.currentIndex()]
                self._fx_preview_carrier = obj.object_id
                preview_block = carrier_ref.block
                unit = self._fx_unit_phases(preview_text, preview_block, carrier_ref, carrier=True)
                origins = obj.body_sources
        same_preview = (keep_view and panel.kamikaze == kamikaze
                        and previous_carrier == self._fx_preview_carrier)
        if same_preview and (unit.weapon or panel.unit.flies):
            unit = replace(unit, altitude=panel.unit.altitude)
        panel.set_context(
            fx.CONTEXT_WEAPON if unit.weapon else fx.CONTEXT_VEHICLE,
            fx.extract_fx_document(preview_text, preview_block.start_line, preview_block.end_line),
            catalog=self._fx_catalog, unit=unit, keep_view=same_preview,
            weapons=self._build_fx_weapons(text, objects),
            debuffs=self._build_fx_debuffs(text),
            decals=self._build_fx_decals(text),
            settings=self._fx_effect_settings(), kamikaze=kamikaze)
        self.fx_panel.set_script_sources(
            origins if self._active_script_path else (),
            expected=(preview_block.kind, preview_block.object_id))
        if self._fx_preview_carrier is not None:
            self._fx_script_sources.update(str(Path(path).resolve()) for path, _, _ in origins)
        self._fx_update_watcher()
        if not keep_view:
            # A fresh definition: what is on disk now is what the tab shows.
            self._fx_script_snapshot = self._fx_script_values(text, block, reference)
            self._fx_loaded_signatures = self._fx_signatures(self._fx_tracked)
            self._fx_failed_signatures = None
        self._sync_fx_preview()

    @staticmethod
    def _fx_kamikaze_carriers(weapon_id, objects):
        vehicles = [obj for obj in objects if obj.kind == "vehicle"]
        users = {obj.object_id for obj in vehicles
                 if any(fx.script_int(obj.values.get(key), -1) == weapon_id
                        for key in ("weapon", "weapon_2", "weapon_3", "weapon_4")
                        if key == "weapon" or weapon_id > 0)}
        carriers = []
        for obj in vehicles:
            body = obj.effective_text
            try:
                if obj.object_id not in users:
                    if not any(key.endswith("_gun_type") for key in obj.values):
                        continue
                    _, mounts, _ = import_gun_points_block(body, find_script_blocks(body)[0])
                    if not any(point.gun_type > 0 and point.gun_type in users for point in mounts):
                        continue
                reference = next(iter(script_model_references(body)), None)
            except CollisionScriptError:
                continue
            if reference is not None:
                carriers.append((obj, body, reference))
        return carriers

    def _fx_carrier_changed(self, _index) -> None:
        panel = self.fx_panel
        if not panel.kamikaze:
            return
        if not panel.confirm_text_changes():
            with QSignalBlocker(panel.carrier_combo):
                panel.carrier_combo.setCurrentIndex(
                    panel.carrier_combo.findData(self._fx_preview_carrier))
            return
        self.reload_fx_from_file()

    def _fx_script_values(self, text: str, block: ScriptBlock,
                          reference: VehicleModelReference) -> dict:
        """Read the script values shown by the Entity Designer."""

        values = dict(zip(
            ("model_scale_x", "model_scale_y", "model_scale_z",
             "model_rotation_x", "model_rotation_y", "model_rotation_z"),
            (reference.scale_x, reference.scale_y, reference.scale_z,
             reference.rotation_x, reference.rotation_y, reference.rotation_z)))
        try:
            values["legacy"], values["compound"], _warnings = import_collision_block(text, block, OPENNEOUA)
            values["wireframe"] = import_wireframe_block(text, block)
            if not block.kind.endswith("weapon"):
                for names, result in (
                    (("overeof_enabled", "overeof"), import_overeof_block(text, block)),
                    (("fire_points_enabled", "fire_x", "fire_y", "fire_z",
                      "num_weapons", "num_weapons_max"), import_fire_points_block(text, block)),
                    (("gun_points_enabled", "gun_points", "unit_gun_default_icon"),
                     import_gun_points_block(text, block)),
                    (("cockpit_camera_enabled", "cockpit_camera_offset_x",
                      "cockpit_camera_offset_y", "cockpit_camera_offset_z"),
                     import_cockpit_camera_block(text, block)),
                    (("briefing_size_enabled", "briefing_size"),
                     import_briefing_size_block(text, block)),
                    (("briefing_xyz_enabled", "briefing_x", "briefing_y", "briefing_z"),
                     import_briefing_xyz_block(text, block)),
                ):
                    values.update(zip(names, result))
                values["type_icon"] = import_type_icon_block(text, block)
            else:
                values["fire_area"] = import_fire_area_block(text, block)
        except CollisionScriptError:
            pass
        return values

    def _fx_apply_script_changes(self, text: str, block: ScriptBlock,
                                 reference: VehicleModelReference) -> None:
        """Merge script edits without replacing unsaved Designer values."""

        current = self._fx_script_values(text, block, reference)
        previous = self._fx_script_snapshot
        self._fx_script_snapshot = {**(previous or {}), **current}
        if previous is None:
            return
        project = self.project
        baseline = self._baseline_project()
        changed = False
        changed_names = set()
        for name, value in current.items():
            if name not in previous or value == previous[name]:
                continue
            changed_names.add(name)
            old = previous[name]
            local = getattr(project, name)
            if name == "fire_area":
                updates = {field.name: getattr(value, field.name) for field in fields(value)
                           if getattr(local, field.name) == getattr(old, field.name)}
                merged = replace(local, **updates)
            else:
                merged = deepcopy(value) if local == old else local
            if merged != local:
                setattr(project, name, merged)
                if name == "type_icon":
                    project.type_icon_cell = None
                changed = True
            setattr(baseline, name, deepcopy(value))
            if name == "type_icon":
                baseline.type_icon_cell = None
        self._tab_reset_baseline = baseline.snapshot()
        if "fire_area" in changed_names:
            self._fire_area_loaded = current["fire_area"]
        if changed_names.intersection({"cockpit_camera_enabled", "cockpit_camera_offset_x",
                                       "cockpit_camera_offset_y", "cockpit_camera_offset_z"}):
            self._loaded_cockpit_camera_enabled = baseline.cockpit_camera_enabled
            self._loaded_cockpit_camera_offset = (
                baseline.cockpit_camera_offset_x, baseline.cockpit_camera_offset_y,
                baseline.cockpit_camera_offset_z)
        if changed_names.intersection({"gun_points_enabled", "gun_points", "unit_gun_default_icon"}):
            self._loaded_gun_points_enabled = baseline.gun_points_enabled
            self._loaded_gun_points = [point.clone() for point in baseline.gun_points]
            self._loaded_unit_gun_default_icon = baseline.unit_gun_default_icon
        if changed:
            self._sync_all()

    def reload_fx_from_file(self) -> None:
        """Read the definition again from disk and refresh the preview."""

        # A reload refills the model list and can change the autoplay key; it must
        # never start the animation. Only entering the tab or switching unit does.
        self._fx_reloading = True
        try:
            self._reload_fx_from_file()
        finally:
            self._fx_reloading = False

    def _reload_fx_from_file(self) -> None:
        if (self._active_script_path is None or self._active_script_id is None
                or self._active_model_reference is None):
            self.statusBar().showMessage(
                "Load a vehicle or weapon from a script before reloading.", 5000)
            return
        kind = self._active_script_kind
        objects = None
        # Taken before reading: a save during the read is caught by the next poll.
        snapshot = self._fx_signatures(self._fx_tracked)
        try:
            if self._fx_source_from_catalog:
                # Game catalog definitions merge every script of the game.
                from studio.assets.catalog import load_game_objects
                from map_editor import bootstrap
                objects = load_game_objects(bootstrap.installation())
                match = next(
                    (obj for obj in objects
                     if f"new_{obj.kind}" == kind
                     and obj.object_id == self._active_script_id), None)
                if match is None:
                    raise CollisionScriptError(
                        f"{kind} {self._active_script_id} is no longer in the game scripts.")
                text = match.effective_text
            else:
                text, _encoding, _bom = read_script_file(self._active_script_path)
                if self.fx_panel.kamikaze:
                    from map_editor import bootstrap
                    from studio.assets.catalog import load_game_objects
                    installation = bootstrap.installation()
                    if installation is not None:
                        objects = load_game_objects(installation)
            block = _resolve_patch_target_block(
                text, kind, self._active_script_id)
        except (OSError, UnicodeError, CollisionScriptError, RuntimeError) as exc:
            # Not retried until a file changes again (the poll would loop).
            self._fx_failed_signatures = snapshot
            self.statusBar().showMessage(f"Reload failed: {exc}", 8000)
            return
        if self._fx_changed_assets:
            try:
                self._reload_fx_assets()
            except (OSError, ValueError, RuntimeError) as exc:
                self._fx_failed_signatures = snapshot
                self.statusBar().showMessage(f"Asset reload failed: {exc}", 8000)
                return
        reference = next((ref for ref in script_model_references(text)
                          if ref.block.kind == kind and ref.block.object_id == self._active_script_id),
                         self._active_model_reference)
        self._active_model_reference = reference
        self._fx_apply_script_changes(text, block, reference)
        if self.fx_panel.has_unsaved_text:
            self._fx_failed_signatures = snapshot
            self.statusBar().showMessage(
                "Source files changed. Save or revert the effect-code drafts before reloading effects.", 6000)
            return
        self._load_fx_context(text, block, reference,
                              keep_view=True, objects=objects)
        self._fx_failed_signatures = None
        self._fx_loaded_signatures = {
            path: snapshot[path] if path in snapshot else self._fx_file_signature(path)
            for path in self._fx_tracked}
        self.statusBar().showMessage(
            f"Visuals & FX reloaded from {self._active_script_path.name}.", 4000)

    def _fx_dependency_paths(self):
        """Collect loaded model, animation and texture sources plus external FX assets."""
        paths = set()
        families = [self.family]
        families.extend(helper._family_ref for helper in self.viewport._fx_external_cache.values()
                        if helper is not None)
        for family in families:
            if family is None:
                continue
            paths.update(str(path.resolve()) for path in
                         (family.base_path, family.setbas_path, family.external_palette_path) if path)
            paths.update(str(dep.resolved_path.resolve()) for dep in family.dependencies
                         if dep.resolved_path is not None)
            paths.update(str(path.resolve()) for path in family.effect_override_paths.values())
        unit = self.fx_panel.unit
        visuals = [unit.normal, unit.dead, unit.megadeth, unit.genesis]
        values = []
        for item in self.fx_panel.document.items:
            if isinstance(item, fx.FxBlock):
                spec = fx.interpret_block(item, self.fx_panel.context)
                visuals.extend(spec.visuals)
                if spec.physical_model is not None:
                    visuals.append(spec.physical_model)
        visited_legacy: set[int] = set()

        def include_legacy_visuals(vehicle_id: int, depth: int = 0) -> None:
            if (vehicle_id in visited_legacy
                    or depth >= fx.LEGACY_NESTING_LIMIT):
                return
            vehicle = self.fx_panel.catalog.get(vehicle_id)
            if vehicle is None:
                return
            visited_legacy.add(vehicle_id)
            visuals.extend((vehicle.dead_visual, vehicle.megadeth_visual))
            for effect in vehicle.chain_fx:
                visuals.extend(effect.visuals)
            for child in vehicle.dest_fx:
                if child.kind in ("death", "megadeth"):
                    include_legacy_visuals(child.vehicle_id, depth + 1)

        for item in self.fx_panel.document.items:
            if isinstance(item, fx.DestFxLine) and item.parsed is not None:
                if item.parsed.kind in ("death", "megadeth"):
                    include_legacy_visuals(item.parsed.vehicle_id)
        # Ground decal textures come from the linked new_decal profiles.
        for owner in (unit, *(child.unit for child in
                              fx.cluster_chain(unit, self.fx_panel.weapons))):
            profile = self.fx_panel.decals.get(owner.ground_decal_id)
            if profile is not None and profile.spec.texture:
                values.append(profile.spec.texture)
        if unit.decoration is not None:
            visuals.append(unit.decoration.visual)
        if unit.tracer is not None:
            values.append(unit.tracer.mesh)
        samples = [spec.sample for spec in unit.sounds.values() if spec.sample]
        for item in self.fx_panel.document.items:
            if isinstance(item, fx.FxBlock):
                samples.extend(spec.sample for spec in fx.interpret_block(
                    item, self.fx_panel.context).sounds.values() if spec.sample)
            elif isinstance(item, fx.DestFxLine) and item.parsed is not None:
                vehicle = self.fx_panel.catalog.get(item.parsed.vehicle_id)
                if vehicle is not None:
                    samples.extend(spec.sample for spec in vehicle.sounds.values()
                                   if spec.sample)
        if unit.cluster is not None and unit.cluster.sound is not None:
            samples.append(unit.cluster.sound.sample)
        for sample in samples:
            path = self._resolve_fx_sound(sample)
            if path is not None:
                paths.add(str(path.resolve()))
        for child in fx.cluster_chain(unit, self.fx_panel.weapons):
            samples = [spec.sample for spec in child.unit.sounds.values() if spec.sample]
            for sample in samples:
                path = self._resolve_fx_sound(sample)
                if path is not None:
                    paths.add(str(path.resolve()))
            visuals.extend((child.unit.normal, child.unit.dead, child.unit.megadeth))
            if child.unit.tracer is not None:
                values.append(child.unit.tracer.mesh)
            if child.unit.decoration is not None:
                visuals.append(child.unit.decoration.visual)
        for visual in visuals:
            values.extend(value for value in (visual.mesh3ds, visual.base_path) if value)
        for value in values:
            path = _resolve_data_asset_path(value, self._active_script_path, self._active_base_path)
            if path is not None:
                paths.add(str(path.resolve()))
        return paths

    @staticmethod
    def _fx_file_signature(path):
        try:
            stat = Path(path).stat()
            return stat.st_mtime_ns, stat.st_size
        except OSError:
            return None

    def _fx_update_watcher(self) -> None:
        """Watch the active definition and its loaded preview dependencies."""
        watched = self._fx_watcher.files() + self._fx_watcher.directories()
        if watched:
            self._fx_watcher.removePaths(watched)
        self._fx_watch_signatures = {}
        if self._active_script_path is None:
            self._fx_reload_timer.stop()
            self._fx_poll_timer.stop()
            return
        paths = {str(self._active_script_path.resolve())} | self._fx_script_sources
        paths.update(str(Path(vehicle.source).resolve()) for vehicle in self._fx_catalog.values()
                     if vehicle.source)
        self._fx_asset_paths = self._fx_dependency_paths()
        paths.update(self._fx_asset_paths)
        self._fx_tracked = set(paths)
        self._fx_watch_signatures = {path: self._fx_file_signature(path) for path in paths}
        # A file tracked for the first time (an external model or the set read
        # while painting) was loaded just now: it is not an edit to reload for.
        for path, signature in self._fx_watch_signatures.items():
            self._fx_loaded_signatures.setdefault(path, signature)
        if not self._fx_poll_timer.isActive():
            self._fx_poll_timer.start()
        # Files edited while the tab was away are picked up right now.
        QTimer.singleShot(0, self._fx_poll)
        existing = [path for path in paths if Path(path).is_file()]
        directories = {str(Path(path).parent) for path in paths if Path(path).parent.is_dir()}
        if existing or directories:
            self._fx_watcher.addPaths(existing + sorted(directories))

    def _fx_signatures(self, paths) -> dict:
        return {path: self._fx_file_signature(path) for path in paths}

    def _fx_poll(self) -> None:
        """Reload when a tracked file differs from the loaded version."""

        if (self._active_script_path is None
                or self._fx_reload_timer.isActive()):
            return
        current = self._fx_signatures(self._fx_tracked)
        if current == self._fx_loaded_signatures or current == self._fx_failed_signatures:
            return
        for path, signature in current.items():
            if (self._fx_loaded_signatures.get(path) != signature
                    and path in self._fx_asset_paths):
                self._fx_changed_assets.add(path)
        self._fx_reload_timer.start()

    def _fx_watched_directory_changed(self, directory):
        # Atomic saves replace files; directory notifications keep their watches alive.
        for path, signature in tuple(self._fx_watch_signatures.items()):
            if str(Path(path).parent) == directory and self._fx_file_signature(path) != signature:
                self._fx_watched_file_changed(path)

    def _fx_watched_file_changed(self, path: str) -> None:
        if self._active_script_path is None:
            return
        if path in self._fx_asset_paths:
            self._fx_changed_assets.add(path)
        self._fx_watch_signatures[path] = self._fx_file_signature(path)
        self._fx_reload_timer.start()

    def _reload_fx_assets(self):
        """Refresh rendered assets through the existing loaders, preserving authoring data."""
        family = self.family
        if family is not None and family.base_path is not None:
            path = family.base_path
            if path.suffix.casefold() == ".3ds":
                refreshed = _load_3ds_family(path)
            else:
                refreshed = self._load_family(
                    path, extra_roots=family.search_roots, overrides=family.overrides,
                    setbas=family.setbas_path)
            references = list(self._script_unit_references)
            visual_reference = self._script_unit_visual_reference
            unit_mode = self._script_unit_mode
            self.family = refreshed
            if family.setbas_path and str(family.setbas_path.resolve()) in self._fx_changed_assets:
                self._vp_embedded = reconstruct_embedded_vps(family.setbas_path)
                self._vp_table, self._vp_table_source, _warnings = runtime_vp_table(
                    family.setbas_path, self._vp_embedded)
            with QSignalBlocker(self.model_tree):
                self._fill_models(refreshed)
                if unit_mode:
                    self._fill_script_units(references, active_visual_reference=visual_reference)
                    self._select_script_unit_item(self._active_model_reference)
            self._load_viewport_scope(self.viewport.visible_owners())
            self._sync_animation_controls()
        for helper in self.viewport._fx_external_cache.values():
            if helper is not None:
                helper.close()
        self.viewport._fx_external_cache.clear()
        self._fx_set_cache = None
        self.viewport._fx_decal_cache.clear()
        self.viewport._fx_emitter_cache.clear()
        self.viewport._fx_tinted_stage_cache.clear()
        self._fx_changed_assets.clear()

    def _fx_document_edited(self) -> None:
        """Resolve newly referenced VP models through the normal preview path."""
        self._sync_fx_preview()

    def _fx_draft_state_changed(self) -> None:
        if not self.fx_panel.has_unsaved_text:
            self._fx_failed_signatures = None

    def _fx_sources_saved(self, paths) -> None:
        # Record our save before reloading, so the watcher does not repeat it.
        for path in paths:
            key = str(Path(path).resolve())
            signature = self._fx_file_signature(path)
            self._fx_loaded_signatures[key] = signature
            self._fx_watch_signatures[key] = signature
        self._fx_failed_signatures = None

    def _sync_fx_preview(self) -> None:
        """Enter/leave the FX viewport mode and load the effect VP owners."""

        if not hasattr(self, "fx_panel"):
            return
        self._sync_collision_model_preview()
        fx_tab = self._is_fx_tab_active()
        # Visuals & FX and Fire Area share the scene preview (unit, ground, shot).
        active = fx_tab or self._is_fire_area_tab_active()
        if active and self._fx_edit_camera is None:
            self._fx_edit_camera = self.viewport._camera_state()
        self.fx_panel.transport_bar.setVisible(active)
        # A loose-BASE unit has no VP table: its effects use the set's VPs.
        self._fx_owner_map = {} if self._loose_unit_uses_set() else {
            vp: owner for vp in (self.fx_panel.required_vps()
                                 | self._fire_area_extra_vps())
            if (owner := self._owner_for_vp(vp)) is not None}
        if active and self.family is not None:
            owners = set(self._fx_owner_map.values())
            if self._current_owner:
                owners.add(self._current_owner)
            if owners != (self.viewport.visible_owners() or set()):
                self._fx_scope = owners
                self._load_viewport_scope(owners)
        else:
            if self.fx_panel.is_playing():
                self.fx_panel.play_button.setChecked(False)
            if self._fx_scope is not None:
                self._fx_scope = None
                self._load_viewport_scope(
                    {self._current_owner} if self._current_owner else None)
        if not active and self._fx_edit_camera is not None:
            self.viewport._set_camera_state(self._fx_edit_camera)
            self._fx_edit_camera = None
            self._fx_fitted_definition = None
            self.viewport.update()
        carrier_id = getattr(self, "_fx_preview_carrier", None) if self.fx_panel.kamikaze else None
        if carrier_id is not None:
            unit = self.fx_panel.unit
            carrier = self._fx_catalog[carrier_id]
            self.fx_panel.set_scene((0.0, -unit.overeof, 0.0), 0.0, carrier.radius)
        else:
            scale, euler = self._fx_unit_scale_rotation()
            self.fx_panel.set_scene(
                (0.0, self.viewport.overeof_preview_offset, 0.0), 0.0,
                self._fx_unit_radius(), transform=self._fx_unit_transform(),
                overeof=self.project.overeof if self.project.overeof_enabled else 0.,
                scale=scale, euler=euler)
        self._fx_update_watcher()
        definition = (self._active_script_path, self._active_script_kind,
                      self._active_script_id)
        if (fx_tab and self.family is not None
                and definition != self._fx_fitted_definition):
            # Effects are usually far larger than the unit: frame them once
            # on entry or a definition change; later camera moves stay manual.
            self._fx_fitted_definition = definition
            self._fit_fx_view()
        self._push_fx_frame()
        self._sync_animation_controls()

    def _load_viewport_scope(self, owners) -> None:
        """Reload the visible owners without moving the camera."""

        camera = self.viewport._camera_state()
        home = self.viewport._home_camera_state
        self.viewport.load_family(
            self.family, owners, keep_camera=True,
            primary_owner=self._current_owner)
        self.viewport.set_model_preview_scale(
            self.project.model_scale_x, self.project.model_scale_y,
            self.project.model_scale_z)
        self.viewport.set_ground_alignment(
            self.project.target_category == VEHICLE,
            self.project.overeof_enabled, self.project.overeof)
        self.viewport._set_camera_state(camera)
        self.viewport._home_camera_state = home

    def _fit_fx_view(self) -> None:
        simulation = self.fx_panel.simulation
        extent = simulation.extent() if simulation is not None else None
        if extent is None:
            return
        center, radius = extent
        if self.fx_panel.follow_unit.isChecked():
            owner = (self._fx_owner_map.get(self.fx_panel.unit.normal.vp)
                     if self.fx_panel.kamikaze else self._current_owner)
            bounds = self.viewport._owner_bounds.get(owner)
            if bounds is not None:
                center = tuple((bounds[axis] + bounds[axis + 3]) * .5 for axis in range(3))
                # Leave room around the unit while avoiding the full debris envelope.
                radius = max(max(bounds[axis + 3] - bounds[axis] for axis in range(3)) * .75,
                             fx.FOLLOW_MIN_RADIUS, self.fx_panel.unit.look_reach())
        self.viewport._center = center
        self.viewport._scale = 1.0 / radius
        self.viewport._zoom = 1.0
        self.viewport._pan = QPointF(0.0, 0.0)
        if self._is_fx_tab_active():
            self._push_fx_frame()
        self.viewport._fx_home_camera = self.viewport._camera_state()
        self.viewport.update()

    def _fx_palette(self):
        """(normclr, invclr) of the palette flash playing now, or None."""

        palettes = self.fx_panel.palette_effects()
        if not palettes:
            return None
        custom, powers = self._fx_color_settings()
        return fx.palette_colors(palettes, custom, powers)

    def _fx_ini_settings(self):
        """(custom palette, slot powers, effect settings) of the game's OpenNeoUA.ini."""

        from map_editor import bootstrap
        install = bootstrap.installation()
        ini = None
        if install is not None:
            ini = bootstrap.find_ci(Path(install.data), "OpenNeoUA.ini")
        if ini is None:
            root = _data_root_for_path(self._active_script_path, self._active_base_path)
            ini = bootstrap.find_ci(root, "OpenNeoUA.ini") if root is not None else None
        signature = (str(ini), self._fx_file_signature(ini)) if ini is not None else None
        cache = getattr(self, "_fx_color_cache", None)
        if cache is None or cache[0] != signature:
            settings = (({}, {}), fx.EffectSettings())
            if ini is not None:
                try:
                    text = Path(ini).read_text(encoding="utf-8", errors="replace")
                    settings = (fx.parse_color_settings(text),
                                fx.parse_effect_settings(text))
                except OSError:
                    pass
            self._fx_color_cache = (signature, settings)
        return self._fx_color_cache[1]

    def _fx_color_settings(self):
        """Custom palette slots and slot powers of the game's OpenNeoUA.ini."""

        return self._fx_ini_settings()[0]

    def _fx_effect_settings(self):
        """Global fades, palette limits and master volume of OpenNeoUA.ini."""

        return self._fx_ini_settings()[1]

    def _resolve_fx_sound(self, name: str) -> Path | None:
        """Sound sample named by a script, relative to the game Data folder."""

        raw = name.strip().replace("\\", "/")
        if not raw:
            return None
        relative = raw[5:] if raw.casefold().startswith("data/") else raw
        roots = []
        from map_editor import bootstrap
        install = bootstrap.installation()
        if install is not None:
            roots.append(Path(install.data))
        root = _data_root_for_path(self._active_script_path, self._active_base_path)
        if root is not None:
            roots.append(root)
        for folder in roots:
            candidate = folder / relative
            if candidate.is_file():
                return candidate
        return None

    def _load_fx_external_model(self, value: str):
        """Family of an external 3DS/BASE model named by a script path."""

        path = _resolve_data_asset_path(
            value, self._active_script_path, self._active_base_path)
        if path is None:
            return None
        try:
            if path.suffix.casefold() == ".3ds":
                family = _load_3ds_family(path)
            else:
                resource_set = self._resource_set_bas_path()
                family = (self._load_family(path, setbas=resource_set)
                          if resource_set is not None else self._load_family(path))
            QTimer.singleShot(0, self._fx_update_watcher)
            return family
        except (OSError, ValueError, RuntimeError):
            return None

    def _push_fx_frame(self) -> None:
        """Send the current preview time of the FX tab to the viewport."""

        if not hasattr(self, "fx_panel"):
            return
        fire_area_tab = self._is_fire_area_tab_active()
        if not (self._is_fx_tab_active() or fire_area_tab):
            if self.viewport.fx_preview_active:
                self.viewport.set_fx_preview(active=False)
            return
        panel = self.fx_panel
        items = panel.preview_items()
        if fire_area_tab:
            items = items + self._fire_area_unit_items()
        tracked_unit = next((item for item in items if item.kind == "unit"), None)
        # A model state whose row is unchecked in the list is not drawn.
        hidden_states = panel.hidden_states
        show_unit = "normal" not in hidden_states
        if hidden_states:
            items = [item for item in items
                     if _fx_item_state(item) not in hidden_states]
        # The unit is drawn by its phase items (normal -> dead -> megadeth)
        # instead of the static model whenever the preview plays a sequence.
        phased = panel.simulation is not None and panel.simulation.shows_unit
        particles = []
        if panel.simulation is not None:
            emitters = {vp: self.viewport.fx_vp_emitter_specs(vp)
                        for vp in panel.required_vps()}
            particles = panel.simulation.particles_at(panel.time_ms, emitters)
            if not show_unit:
                # Decorations and the tracer belong to the normal model and hide with it.
                particles = [sprite for sprite in particles if sprite.source >= 0]
        self.viewport.set_fx_preview(
            active=True, items=items, owner_by_vp=self._fx_owner_map,
            unit_owners={self._current_owner} if self._current_owner else set(),
            boxes=panel.offset_boxes(), time_ms=panel.time_ms,
            show_unit=show_unit and not phased and not panel.kamikaze,
            weapon_context=panel.context == fx.CONTEXT_WEAPON,
            data_root=_data_root_for_path(
                self._active_script_path, self._active_base_path),
            highlight=panel.highlight, particles=particles,
            external_loader=self._load_fx_external_model,
            palette=self._fx_palette(), shake=panel.shake_angles(),
            vp_source=self._fx_set_vp_source,
            required_vps=panel.required_vps() | self._fire_area_extra_vps(),
            damage=panel.damage_marks())
        # The Fire Area camera stays on the volume instead of following the shot.
        self.viewport.set_fx_follow_unit(
            panel.follow_unit.isChecked() and not fire_area_tab, tracked_unit,
            None if panel.kamikaze else self._current_owner)
