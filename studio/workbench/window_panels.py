"""AssemblyWindow mixin: repair actions and the family -> panels refresh."""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QListWidgetItem,
    QMessageBox,
    QTreeWidgetItem,
)

from studio.assets.catalog import build_texture_catalog
from studio.assets.dependencies import filter_tree as filter_asset_tree
from studio.assets.family import (
    AssetFamily,
)
from studio.assets.mapping import (
    MappingEditError,
    MappingIndex,
    plan_copy_style,
    plan_planar,
    RepairPlan,
    save_repaired_base,
)
from studio.ui.style import theme_list_color
from studio.ui.widgets import (
    checker_thumbnail as _checker_thumbnail,
    PROBLEM_STATUSES as _PROBLEM_STATUSES,
    mark_problem_item as _mark_problem_item,
    PROBLEM_ROLE as _PROBLEM_ROLE,
    STATUS_COLORS as _STATUS_COLORS,
    status_icon as _status_icon,
)
from studio.rendering.geometry import (
    detect_fx_elements,
)
from studio.workbench.common import (
    WINDOW_TITLE,
    _display_path,
)


class _FamilyPanelsMixin:
    """Part of AssemblyWindow; `self` is always the AssemblyWindow."""

    # -- repair -----------------------------------------------------------------

    def _update_repair_buttons(self) -> None:
        editing = (
            self._editing_allowed()
            and not self.viewport.paste_preview_active)
        can_plan = (
            editing
            and self._selected_poly is not None
            and self._mapping_index is not None
            and self._mapping_index.status(self._selected_poly) == "unmapped"
            and self._family is not None
            and self._family.base_path is not None
            and self.repair_target_combo.count() > 0
        )
        self.repair_copy_button.setEnabled(can_plan)
        self.repair_planar_button.setEnabled(can_plan)
        self.repair_apply_button.setEnabled(
            editing and self._repair_plan is not None)
        self.repair_revert_button.setEnabled(
            editing and bool(self._pending_repairs))
        self.repair_save_button.setEnabled(bool(self._pending_repairs))
        block_index = self._current_material_block_index()
        block = None
        if self._workbench_obj is not None and block_index is not None \
                and 0 <= block_index < len(
                    self._workbench_obj.base_object.ades):
            block = self._workbench_obj.base_object.ades[block_index]
        supported = bool(
            block is not None
            and (block.class_id or "").lower()
            in ("amesh.class", "area.class"))
        self.material_copy_button.setEnabled(supported)
        self.material_paste_button.setEnabled(
            editing and self._material_clipboard is not None)
        self.material_add_button.setEnabled(
            editing and supported
            and (block.class_id or "").lower() == "amesh.class")
        self.material_delete_button.setEnabled(editing and supported)

    def _show_plan(self, plan: RepairPlan) -> None:
        self._repair_plan = plan
        self.repair_preview.clear()
        for line in plan.describe():
            self.repair_preview.addItem(line)
        block = self._workbench_obj.base_object.ades[plan.block_index]
        tex = block.texture.name if block.texture else "-"
        self._draw_uv_overlay(tex, plan.uvs, plan.poly_id,
                              extra_groups=block.olpl)
        self._update_repair_buttons()

    def _plan_copy_style(self) -> None:
        try:
            plan = plan_copy_style(self._workbench_obj, self._selected_poly,
                                   self.repair_source_spin.value(),
                                   self._mapping_index)
        except MappingEditError as exc:
            QMessageBox.warning(self, "Cannot plan repair", str(exc))
            return
        self._show_plan(plan)

    def _plan_planar(self) -> None:
        target = self.repair_target_combo.currentData()
        if target is None:
            return
        try:
            plan = plan_planar(self._workbench_obj, self._selected_poly,
                               target, self._mapping_index)
        except MappingEditError as exc:
            QMessageBox.warning(self, "Cannot plan repair", str(exc))
            return
        self._show_plan(plan)

    def _apply_repair_in_memory(self) -> None:
        if not self._require_editing("Apply Mapping Repair"):
            return
        if self.viewport.paste_preview_active:
            self._notify(
                "Finish or cancel Paste Geometry before applying a mapping "
                "repair.", 6000)
            return
        plan = self._repair_plan
        if plan is None or self._workbench_obj is None:
            return
        from studio.formats.base_format import AttsEntry

        block = self._workbench_obj.base_object.ades[plan.block_index]
        block.atts.append(AttsEntry(plan.poly_id, plan.color_val,
                                    plan.shade_val, plan.tracy_val, 0))
        block.olpl.append(list(plan.uvs))
        if plan.block_index < len(self._workbench_obj.materials):
            self._workbench_obj.materials[plan.block_index].faces.append(
                (plan.poly_id, list(plan.uvs), plan.shade_val)
            )
        self._pending_repairs.append(plan)
        self._repair_plan = None
        self._mapping_index = MappingIndex(self._workbench_obj)
        self._update_mapping_diagnostics_summary()
        visible = self._family_descendants(
            self._family, self._selected_owner)
        self.viewport.load_family(
            self._family, visible_owners=visible,
            primary_owner=self._selected_owner)
        self._sync_animation_controls()
        self.viewport.set_mapping_diagnostics(
            self.mapping_diag_check.isChecked())
        self.viewport.set_selected_polygon(plan.poly_id)
        self._fill_polygon_inspector(plan.poly_id)
        self._fill_blocks_list(self._family)
        self._update_repair_buttons()
        self.statusBar().showMessage(
            f"Mapping repair ready for polygon #{plan.poly_id}. "
            "Use Export Asset Family to keep it."
        )

    def _revert_repairs(self) -> None:
        if not self._require_editing("Revert Mapping Repairs"):
            return
        if self._family is None or self._family.base_path is None:
            return
        self._pending_repairs = []
        self._repair_plan = None
        self.open_base(self._family.base_path)
        self.statusBar().showMessage("In-memory repairs reverted (reloaded "
                                     "from disk).")

    def _save_repaired_as(self) -> None:
        if not self._pending_repairs or self._family is None \
                or self._family.base_path is None:
            return
        source = self._family.base_path
        suggested = source.with_name(f"{source.stem}.fixed{source.suffix}")
        path, _ = QFileDialog.getSaveFileName(
            self, "Export mapping as",
            str(suggested),
            "Urban Assault BASE (*.base *.bas);;All files (*)",
        )
        if not path:
            return
        try:
            notes = save_repaired_base(self._family, self._workbench_obj,
                                       self._pending_repairs, path)
        except MappingEditError as exc:
            QMessageBox.critical(self, "Export failed - nothing written",
                                 str(exc))
            return
        self._saved_repair_path = path
        self._pending_repairs = []
        self._log("; ".join(notes))
        self._notify(f"Mapping exported: {Path(path).name}.", 8000)
        # Keep resolving against the original family's directories so the
        # reloaded copy finds its skeleton/textures even from a new folder.
        for root in (source.parent, source.parent.parent):
            if root not in self._extra_roots:
                self._extra_roots.append(root)
        self.open_base(path)

    # -- family -> panels --------------------------------------------------------

    def _set_document_title(self, path: str | Path | None) -> None:
        if path is None:
            self.setWindowTitle(WINDOW_TITLE)
            return
        full_path = _display_path(path)
        self.setWindowTitle(
            f"{WINDOW_TITLE} - {full_path.name} - {full_path}"
        )

    def _set_family(self, family: AssetFamily) -> None:
        self._cancel_live_scale()
        self._texture_preview_cache.clear()
        self._texture_qimage_cache.clear()
        if family is not self._family:
            self._base_entry_names.clear()
            self._last_component_selection = None
            self._bundle_targets.clear()
            self._bundle_base_snapshots.clear()
            self._geometry_writer_capability_cache.clear()
        self._family = family
        self._sync_close_archive_action()
        # The viewport always renders the selected object plus its children.
        # This is predictable for normal assets and safe for huge SET.BAS
        # families without a second render-scope system.
        objects = family.all_objects()
        faces = self._family_face_count(family)
        self._large_mode = (len(objects) > self.LARGE_OBJECT_THRESHOLD
                            or faces > self.LARGE_FACE_THRESHOLD)
        self._owner_to_obj = {o.owner_path: o for o in objects}
        default_owner = self._selected_owner \
            if self._selected_owner in self._owner_to_obj \
            else self._default_owner(family)
        self._selected_owner = default_owner
        initial_visible = self._family_descendants(family, default_owner)
        if self._large_mode:
            self._log(
                f"Large asset family detected ({len(objects)} objects, "
                f"{faces} faces) - rendering selected object + children."
            )

        self.viewport.load_family(
            family,
            visible_owners=initial_visible,
            primary_owner=self._selected_owner,
        )
        if self._selected_owner:
            self.viewport.set_selected_owner(self._selected_owner)
            self.viewport.frame_owner(self._selected_owner)
            # Reset View means the exact camera the user receives when this
            # model opens, not a later bbox fit after geometry changes.
            self.viewport.capture_reset_view()
            self._update_reset_camera_action()
        self._fill_asset_tree(family)
        if self._selected_owner:
            selected_item = self._owner_to_item.get(self._selected_owner)
            if selected_item is not None:
                self.asset_tree.setCurrentItem(selected_item)
        self._fill_textures(family)
        self._fill_chunks(family)
        self._fill_checks(family)
        self._pending_repairs = []
        self._geom_dirty = {}
        self._geom_original = {}
        self._topology_original = {}
        self._fx_aliases.clear()
        self._geometry_clipboard = None
        self._uv_ctx = None
        self._uv_contexts = {}
        self._uv_original = {}
        self._vanm_uv_original = {}
        self._texture_original = {}
        self._tab_edit_selection = {}
        self._fx_preview_cache.clear()
        self._edit_undo_stack.clear()
        self._edit_redo_stack.clear()
        self._uv_history_before = None
        self._selected_poly = None
        self._selected_polys.clear()
        self._object_info_polygon_lines = []
        self._sync_geometry_save_controls()
        self._rebuild_workbench(family, self._selected_owner)
        self._refresh_fx_elements()
        self._focus_assets_for_owner(self._selected_owner)
        self.viewport.set_mapping_diagnostics(
            self.mapping_diag_check.isChecked())
        self._apply_diagnostics_filter()

        self._sync_animation_controls()

        self._update_editor_status()
        # Reapply the active main-tab contract after the viewport rebuild.
        self._sync_tab_edit_mode()
        # Family replacement is the final authority for every stale
        # snapshot/paste/archive enable state.
        self._sync_edit_action_states()
        self._update_reset_camera_action()
        status, _details = self._completeness(family)
        self._log(f"loaded {family.base_path}: {status}")

        title_path = family.base_path or family.setbas_path
        self._set_document_title(title_path)
        name = title_path.name if title_path else "manual family"
        diag = (f", textured preview incomplete "
                f"({len(family.textured_diagnostics)} issue(s), see Asset Dependencies)"
                if family.textured_diagnostics else "")
        self.statusBar().showMessage(
            f"Loaded {name}: {len(family.all_objects())} object(s), "
            f"{len(family.textures)} texture(s), "
            f"{len(family.animations)} animation(s), "
            f"{len(family.warnings)} warning(s)"
            f"{diag}"
        )

    def _tree_item(self, label: str, status: str, node_data,
                   source: str = "") -> QTreeWidgetItem:
        item = QTreeWidgetItem([label, status, source])
        if node_data and node_data[0] == "group":
            # Pure category rows remain clickable for expand/collapse but are
            # not selectable, avoiding the accent stripe over their arrow.
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
        status_key = {
            "auto_loaded": "found", "loaded": "found", "resolved": "found",
            "kept_for_session": "manual", "trial_loaded": "manual",
            "ambiguous": "ambiguous", "missing": "missing",
            "failed_load": "decode failed", "skipped": "missing",
            "unsupported_loader": "ambiguous",
        }.get(status, status)
        item.setIcon(0, _status_icon(status_key))
        if status:
            color = _STATUS_COLORS.get(status_key)
            if color is not None:
                item.setForeground(1, color)
        _mark_problem_item(
            item, status in _PROBLEM_STATUSES or status_key in _PROBLEM_STATUSES,
            columns=3)
        item.setData(0, Qt.ItemDataRole.UserRole, node_data)
        return item

    def _dependency_for(self, family: AssetFamily, name: str,
                        owner: str | None = None, kind: str | None = None):
        folded = str(name).casefold()
        matches = [
            dep for dep in family.dependencies
            if str(dep.raw_ref).casefold() == folded
            and (kind is None or dep.kind == kind)]
        if owner is not None:
            exact = [dep for dep in matches if dep.owner_node == owner]
            if exact:
                return exact[0]
        return matches[0] if matches else None

    def _dependency_reference(self, family: AssetFamily, name: str):
        ref = (self._matching_ref(
            getattr(family, "texture_refs", {}), name)
            or self._matching_ref(
                getattr(family, "animation_refs", {}), name))
        if ref is not None:
            return ref
        for fam_obj in getattr(family, "all_objects", lambda: [])():
            skeleton_ref = getattr(fam_obj, "skeleton_ref", None)
            if skeleton_ref is not None and str(
                    skeleton_ref.logical_name).casefold() \
                    == str(name).casefold():
                return skeleton_ref
        return None

    def _decorate_dependency_node(self, item: QTreeWidgetItem, dep,
                                  family: AssetFamily) -> None:
        if dep is None:
            return
        status = self._effective_status(dep.raw_ref, dep.status)
        status_key = {
            "auto_loaded": "found", "resolved": "found",
            "trial_loaded": "manual", "kept_for_session": "manual",
            "ambiguous": "ambiguous", "missing": "missing",
            "failed_load": "decode failed",
            "unsupported_loader": "ambiguous", "skipped": "missing",
        }.get(status, status)
        item.setText(1, status)
        item.setIcon(0, _status_icon(status_key))
        color = _STATUS_COLORS.get(status_key)
        if color is not None:
            item.setForeground(1, color)
        _mark_problem_item(
            item, status in _PROBLEM_STATUSES or status_key in _PROBLEM_STATUSES,
            columns=3)
        location = (dep.display_path() if dep.resolved_path is not None else
                    dep.error or dep.source or "-")
        if dep.resolution_source:
            location = f"{dep.resolution_source}: {location}"
        item.setText(2, location)
        provenance = (
            f"{location}\n"
            f"source={dep.resolution_source or '-'}\n"
            f"rule={dep.resolution_rule or '-'}")
        for column in range(3):
            item.setToolTip(column, provenance)

        # Alternative sources are listed only when there is something to
        # choose from; a clean single match is described by the tooltip.
        ref = self._dependency_reference(family, dep.raw_ref)
        embedded_candidates = list(getattr(ref, "embedded_candidates", ()))
        needs_choice = (
            status_key in ("ambiguous", "missing", "decode failed")
            or len(dep.candidates) + len(embedded_candidates) > 1)
        if needs_choice:
            for candidate in dep.candidates:
                child = self._tree_item(
                    "Loose candidate",
                    "current" if dep.resolved_path == candidate
                    else "available",
                    ("dependency_candidate", (dep.raw_ref, str(candidate))),
                    str(candidate))
                item.addChild(child)
            for embedded in embedded_candidates:
                child = self._tree_item(
                    "SET.BAS fallback",
                    "current" if getattr(ref, "status", "") in (
                        "setbas", "manual (SET.BAS)") else "available",
                    ("dependency_candidate", (dep.raw_ref, embedded)),
                    embedded)
                item.addChild(child)

        bare = dep.raw_ref.replace("\\", "/").split("/")[-1]
        override = (family.overrides.get(dep.raw_ref)
                    or family.overrides.get(bare))
        if override:
            item.addChild(self._tree_item(
                "Session override", "active",
                ("dependency_info", (dep.raw_ref, None)), override))
        saved = self._saved_choice_for(dep.raw_ref)
        if saved is not None:
            saved_row = self._tree_item(
                "Saved profile choice",
                "STALE" if saved.stale else "available",
                ("dependency_candidate", (
                    dep.raw_ref,
                    None if saved.stale else saved.chosen_path)),
                saved.chosen_path)
            if not saved.stale:
                saved_row.setForeground(0, QColor(110, 170, 255))
            item.addChild(saved_row)

    def _base_entry_name(self, family: AssetFamily, fam_obj,
                         owner: str) -> str:
        exact = self._base_entry_names.get(owner)
        if exact:
            return exact
        if owner == "root" and family.base_path is not None \
                and not self._family_entry_is_setbas_archive():
            return family.base_path.name
        parsed = str(getattr(fam_obj.base_object, "name", "") or "").strip()
        if parsed:
            return parsed
        offset = getattr(fam_obj.base_object, "source_objt_offset", -1)
        return (f"BASE object at 0x{offset:X}" if offset >= 0 else
                f"BASE object [{owner}]")

    def _fill_asset_tree(self, family: AssetFamily) -> None:
        self._asset_filter_expansion = None
        # Rebuild atomically with respect to the persistent-focus guard: Qt
        # emits selection changes while clear() destroys the old items.
        self.asset_tree.blockSignals(True)
        try:
            self.asset_tree.clear()
        finally:
            self.asset_tree.blockSignals(False)
        self._owner_to_item = {}
        if family.root_object is None:
            root_item = self._tree_item(
                "Manual family (no BASE)", "loaded", ("base", None),
                str(family.base_path or "manual selection"))
            self.asset_tree.addTopLevelItem(root_item)
            return
        # Asset Dependencies has one stable meaning: show the active model
        # entry point and its own dependency subtree.  Bas Manager remains the
        # archive-wide browser.  Do not switch between whole-archive and
        # model-scoped trees depending on whether a VP name happened to be
        # resolved in this session.
        display_root = family.root_object
        display_owner = display_root.owner_path
        if self._selected_owner:
            scoped = next((
                obj for obj in family.all_objects()
                if obj.owner_path == self._selected_owner), None)
            if scoped is not None:
                display_root = scoped
                display_owner = scoped.owner_path
        root_name = self._base_entry_name(
            family, display_root, display_owner)
        root_source = str(family.base_path or "manual family")
        if self._family_entry_is_setbas_archive():
            offset = getattr(display_root.base_object, "source_objt_offset", -1)
            root_source = (
                f"SET.BAS read-only provider; embedded OBJT @ 0x{offset:X}"
                if offset >= 0 else "SET.BAS read-only provider")
        root_item = self._tree_item(
            f"Root BASE: {root_name}", "loaded",
            ("base", display_root), root_source)
        self.asset_tree.addTopLevelItem(root_item)
        self._owner_to_item[display_owner] = root_item

        def texture_node(name: str, owner: str,
                         dep_kind: str | None = None) -> QTreeWidgetItem:
            dep = self._dependency_for(family, name, owner, dep_kind)
            item = self._tree_item(name, dep.status if dep else "?",
                                   ("texture", name))
            self._decorate_dependency_node(item, dep, family)
            return item

        def animation_node(name: str, owner: str) -> QTreeWidgetItem:
            dep = self._dependency_for(family, name, owner, "animation")
            item = self._tree_item(name, dep.status if dep else "?",
                                   ("animation", name))
            self._decorate_dependency_node(item, dep, family)
            animation = next((value for key, value in family.animations.items()
                              if str(key).casefold() == str(name).casefold()),
                             None)
            bitmaps = list(dict.fromkeys(
                getattr(animation, "bitmap_names", ()) if animation else ()))
            if bitmaps:
                frames = self._tree_item(
                    f"Frame bitmaps ({len(bitmaps)})", "", ("group", None))
                item.addChild(frames)
                for bitmap in bitmaps:
                    frames.addChild(texture_node(
                        bitmap, "family", "anm_bitmap"))
            return item

        def add_object(fam_obj, parent_item, owner: str):
            skel_name = fam_obj.base_object.skeleton_name
            if skel_name:
                dep = self._dependency_for(
                    family, skel_name, owner, "skeleton")
                skeleton_item = self._tree_item(
                    f"Skeleton: {skel_name}",
                    dep.status if dep else (
                        "auto_loaded" if fam_obj.skeleton else "missing"),
                    ("skeleton", fam_obj))
                self._decorate_dependency_node(skeleton_item, dep, family)
                parent_item.addChild(skeleton_item)

            textures = []
            animations = []
            for block in fam_obj.base_object.ades:
                for tex in (block.texture, block.tracy_texture):
                    if tex is None or not tex.name:
                        continue
                    target = (animations if tex.kind == "bmpanim"
                              else textures)
                    if tex.name not in target:
                        target.append(tex.name)
            if textures:
                group = self._tree_item(f"Textures ({len(textures)})",
                                        "", ("group", None))
                parent_item.addChild(group)
                for name in textures:
                    group.addChild(texture_node(name, owner))
            if animations:
                group = self._tree_item(f"Animations ({len(animations)})",
                                        "", ("group", None))
                parent_item.addChild(group)
                for name in animations:
                    group.addChild(animation_node(name, owner))

            extra_deps = [
                dep for dep in family.dependencies
                if dep.owner_node == owner
                and dep.kind in ("embedded", "unknown_chunk")]
            if extra_deps:
                extras = self._tree_item(
                    f"Embedded / other ({len(extra_deps)})", "",
                    ("group", None))
                parent_item.addChild(extras)
                for dep in extra_deps:
                    node = self._tree_item(
                        f"{dep.kind}: {dep.raw_ref}", dep.status,
                        ("dependency_info", (dep.raw_ref, None)))
                    self._decorate_dependency_node(node, dep, family)
                    extras.addChild(node)

            if fam_obj.kids:
                kids_group = self._tree_item(
                    f"Children / KIDS ({len(fam_obj.kids)})", "",
                    ("group", None))
                parent_item.addChild(kids_group)
                for index, kid in enumerate(fam_obj.kids):
                    polys = (kid.skeleton.parsed_polygon_count
                             if kid.skeleton else 0)
                    kid_owner = kid.owner_path
                    kid_name = self._base_entry_name(
                        family, kid, kid_owner)
                    selected = " [selected entry point]" \
                        if kid_owner in self._base_entry_names else ""
                    kid_item = self._tree_item(
                        f"BASE entry: {kid_name}{selected} "
                        f"(KIDS #{index}, {polys} polys)",
                        "resolved" if kid.skeleton else "missing",
                        ("child", kid),
                        f"inline BASE OBJT @ 0x{getattr(kid.base_object, 'source_objt_offset', -1):X}")
                    kid_item.setToolTip(0, kid.owner_path)
                    kids_group.addChild(kid_item)
                    self._owner_to_item[kid.owner_path] = kid_item
                    add_object(kid, kid_item, kid_owner)

        add_object(display_root, root_item, display_owner)

        family_deps = [
            dep for dep in family.dependencies
            if dep.owner_node == "family"
            and dep.kind in ("palette", "shader_remap", "tracy_remap")]
        if family_deps:
            group = self._tree_item(
                f"Palette / remap ({len(family_deps)})", "",
                ("group", None))
            root_item.addChild(group)
            for dep in family_deps:
                node = self._tree_item(
                    f"{dep.kind}: {dep.raw_ref}", dep.status,
                    ("dependency_info", (dep.raw_ref, None)))
                self._decorate_dependency_node(node, dep, family)
                group.addChild(node)

        self.asset_tree.expandAll()
        if self.tree_search.text().strip():
            self._filter_asset_tree(self.tree_search.text())

    def _asset_dependency_focus_items(
            self, owner: str | None) -> list[QTreeWidgetItem]:
        """Return the BASE/dependency rows belonging to one model owner."""

        if owner is None or owner not in self._owner_to_item:
            return []
        owner_item = self._owner_to_item[owner]
        focused = [owner_item]

        def collect_components(item) -> None:
            for index in range(item.childCount()):
                child = item.child(index)
                data = child.data(0, Qt.ItemDataRole.UserRole)
                kind = data[0] if data else ""
                # Every native component in the active BASE subtree is part
                # of that model dependency family.  Keep KIDS and animation
                # frame bitmaps highlighted too; provenance/candidate rows are
                # diagnostic choices, not model dependencies themselves.
                if kind in ("child", "skeleton", "texture", "animation"):
                    focused.append(child)
                    collect_components(child)
                elif kind == "group":
                    child.setExpanded(True)
                    collect_components(child)

        collect_components(owner_item)
        return focused

    def _restore_asset_dependency_focus(self) -> None:
        """Keep the active model dependency highlight locked and persistent."""

        if self._asset_focus_restoring:
            return
        focused = self._asset_dependency_focus_items(self._asset_focus_owner)
        if not focused:
            return
        self._asset_focus_restoring = True
        try:
            self.asset_tree.blockSignals(True)
            self.asset_tree.clearSelection()
            for item in focused:
                item.setSelected(True)
        finally:
            self.asset_tree.blockSignals(False)
            self._asset_focus_restoring = False

    def _focus_assets_for_owner(self, owner: str | None,
                                switch_tabs: bool = True) -> None:
        """Show and persistently highlight the active model dependencies."""

        if owner is None or owner not in self._owner_to_item:
            return
        if switch_tabs:
            self._right_tabs.setCurrentWidget(self._resources_tabs)
            self._resources_tabs.setCurrentWidget(self._assets_panel)
        self._asset_focus_owner = owner
        owner_item = self._owner_to_item[owner]
        owner_item.setExpanded(True)
        self.asset_tree.blockSignals(True)
        try:
            # Current row is only keyboard/context position.  The green rows are
            # the persistent model-dependency focus and are restored separately.
            self.asset_tree.setCurrentItem(owner_item)
        finally:
            self.asset_tree.blockSignals(False)
        self._restore_asset_dependency_focus()
        self.asset_tree.scrollToItem(
            owner_item, QAbstractItemView.ScrollHint.PositionAtCenter)

    def _tree_owner_for_item(self, item) -> str | None:
        current = item
        while current is not None:
            data = current.data(0, Qt.ItemDataRole.UserRole)
            if data:
                kind, payload = data
                if kind in ("skeleton", "child") and payload is not None:
                    return getattr(payload, "owner_path", None)
                if kind == "base" and self._family \
                        and self._family.root_object:
                    return (getattr(payload, "owner_path", None)
                            or self._family.root_object.owner_path)
            current = current.parent()
        return None

    def _on_tree_double_clicked(self, item, _column=0) -> None:
        """Open dependency previews without turning the tree into a model list."""

        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data:
            kind, payload = data
            if kind == "texture":
                self._open_visual_texture(
                    payload, show_preview=True, switch_tabs=False)
                return
            if kind == "animation":
                self._play_asset_animation(item)

    def _filter_asset_tree(self, text: str) -> None:
        self._asset_filter_expansion = filter_asset_tree(
            self.asset_tree, text, self._asset_item_search_metadata,
            self._asset_filter_expansion)

    def _asset_item_search_metadata(self, item) -> str:
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data:
            return ""
        kind, payload = data
        owner = self._tree_owner_for_item(item) or ""
        class_id = {
            "base": "base.class",
            "texture": "ilbm.class",
            "animation": "bmpanim.class VANM",
        }.get(kind, "")
        if kind in ("skeleton", "child") and payload is not None:
            base_object = getattr(payload, "base_object", None)
            skeleton_class = (
                getattr(base_object, "skeleton_class", "") or "sklt.class")
            class_id = (
                f"base.class {skeleton_class}"
                if kind == "child" else skeleton_class)
        return f"{kind} {class_id} {owner}"

    def _open_visual_texture(
            self, name: str, *, show_preview: bool = False,
            switch_tabs: bool = True) -> None:
        """Resolve a texture and optionally show it without forced tab jumps."""

        if not name or not hasattr(self, "_visuals_tabs"):
            return
        if switch_tabs:
            self._right_tabs.setCurrentWidget(self._visuals_tabs)
            self._visuals_tabs.setCurrentIndex(0)
        match = None
        for row in range(self.texture_list.count()):
            item = self.texture_list.item(row)
            value = item.data(Qt.ItemDataRole.UserRole)
            if value and str(value).casefold() == str(name).casefold():
                match = item
                break
        if match is not None:
            self.texture_list.setCurrentItem(match)
            self.texture_list.scrollToItem(
                match, QAbstractItemView.ScrollHint.PositionAtCenter)
            self._load_visual_texture_item(match)
        if show_preview:
            self._preview_family_texture(name)

    def _preview_visual_texture_item(self, item) -> None:
        if item is not None:
            self._preview_family_texture(
                item.data(Qt.ItemDataRole.UserRole))

    def _on_polygon_deselected(self, poly_id: int) -> None:
        mirror_active = self._mirror_select_active()
        if mirror_active:
            self._mirror_select_source_polys.difference_update(
                self._mirrored_polygon_selection({poly_id}))
            self._selected_polys = self._mirrored_polygon_selection(
                self._mirror_select_source_polys)
        else:
            self._selected_polys.difference_update(
                self._complete_fx_polygon_selection({poly_id}))
        context = self._mirror_selection_context()
        if context is not None:
            session, _owner, fam_obj = context
            vertices = self._polygon_vertices(
                fam_obj.skeleton, self._selected_polys)
            session.set_selection(vertices)
            if mirror_active:
                self._set_mirror_transform_source(
                    self._polygon_vertices(
                        fam_obj.skeleton,
                        self._mirror_select_source_polys))
        self._selected_poly = min(self._selected_polys, default=None)
        element = self._fx_element_for_polygon(self._selected_poly)
        self.fx_combo.blockSignals(True)
        self.fx_combo.setCurrentIndex(self._fx_combo_index(element.identity) if element else 0)
        self.fx_combo.blockSignals(False)
        self.viewport.set_selected_polygon(self._selected_poly)
        self.viewport.set_highlight_polys(self._selected_polys)
        self._sync_poly_id_control()
        self._fill_polygon_inspector(self._selected_poly)
        self._update_repair_buttons()
        self._sync_edit_action_states()
        self._notify(
            f"Deselected polygon #{poly_id}."
            if self._selected_poly is not None else
            "Selection cleared.", 3500)

    def _on_selection_cleared(self) -> None:
        self._clear_model_selection(notify=True)

    def _clear_model_selection(self, notify: bool = False) -> None:
        """Clear every polygon/FX selection without changing the owner."""

        self._selected_polys.clear()
        self._mirror_select_source_polys.clear()
        self._selected_poly = None
        session = self.viewport.edit_session
        if session is not None:
            session.select_none()
        self.viewport.set_selected_polygon(None)
        self.viewport.set_highlight_polys(set())
        self._sync_poly_id_control()
        if self.fx_combo.currentIndex() > 0:
            self.fx_combo.blockSignals(True)
            self.fx_combo.setCurrentIndex(0)
            self.fx_combo.blockSignals(False)
        self._fill_polygon_inspector(None)
        self._update_repair_buttons()
        self._sync_edit_action_states()
        if notify:
            self._notify("Selection cleared.", 3500)

    def _on_tree_node_selected(self, current, _previous=None) -> None:
        if current is None or self._family is None:
            self._set_object_info(["No asset selected."])
            return
        data = current.data(0, Qt.ItemDataRole.UserRole)
        if not data:
            self._set_object_info(["No asset information available."])
            return
        kind, payload = data
        # Asset Dependencies is intentionally a viewer/resolver.  Moving the
        # current/hovered row must never switch the active model or replace its
        # persistent dependency highlight.  Explicit model changes come from
        # the viewport/Bas Manager (or the dedicated context action).
        family = self._family
        lines = []

        if kind == "base":
            lines.extend(["BASE prefab", f"path: {family.base_path}",
                          f"search root: {family.search_root}"])
            objects = family.all_objects()
            lines.append(f"objects: {len(objects)} "
                         f"(children: {max(0, len(objects) - 1)})")
            from studio.assets.dependencies import summarize
            lines.append("dependencies: " + ", ".join(
                f"{v} {k}" for k, v in summarize(family.dependencies).items()))
            if self._mapping_index:
                lines.append(f"mapping: {self._mapping_index.poly_count} "
                             f"polygons, unmapped "
                             f"{self._mapping_index.unmapped or 'none'}")
            lines.append(f"warnings: {len(family.warnings)}")
        elif kind in ("skeleton", "child"):
            fam_obj = payload
            skeleton = fam_obj.skeleton
            lines.append("skeleton" if kind == "skeleton"
                         else "child BASE object (KIDS)")
            lines.append(f"owner: {fam_obj.owner_path}")
            if fam_obj.base_object.name:
                lines.append(f"BASE name: {fam_obj.base_object.name}")
            lines.append(f"reference: {fam_obj.base_object.skeleton_name}")
            if fam_obj.base_object.skeleton_class:
                lines.append(
                    f"class: {fam_obj.base_object.skeleton_class}")
            if fam_obj.skeleton_ref:
                lines.append(f"source: {fam_obj.skeleton_ref.source or '?'} "
                             f"-> {fam_obj.skeleton_ref.display_path}")
                source_path = getattr(fam_obj.skeleton_ref, "path", None)
                if source_path and Path(source_path).is_file():
                    path = Path(source_path)
                    lines.append(
                        f"file: {path.stat().st_size:,} bytes | "
                        f"{'writable' if os.access(path, os.W_OK) else 'read-only'}")
            if skeleton:
                lines.append(f"vertices: {len(skeleton.points)}")
                lines.append(
                    f"polygons: {skeleton.parsed_polygon_count} parsed | "
                    f"{skeleton.rendered_polygon_count} rendered")
                lines.append(
                    f"outline: {len(skeleton.outline_points)} points | "
                    f"{skeleton.parsed_outline_group_count} groups")
                lines.append(
                    f"SEN2 points: {len(skeleton.sensors)} | "
                    f"chunks: {len(skeleton.chunks)}")
                mapped = {p for g in fam_obj.materials for p, _u, _s in g.faces}
                lines.append(f"mapped polygons: {len(mapped)}/"
                             f"{skeleton.parsed_polygon_count}")
                mapping = (self._mapping_index
                           if fam_obj is self._workbench_obj
                           else MappingIndex(fam_obj))
                lines.append(
                    f"mapping issues: {len(mapping.unmapped)} unmapped | "
                    f"{len(mapping.duplicates)} duplicate | "
                    f"{len(mapping.invalid)} invalid")
                if skeleton.points:
                    axes = list(zip(*skeleton.points))
                    minimum = tuple(min(axis) for axis in axes)
                    maximum = tuple(max(axis) for axis in axes)
                    size = tuple(maximum[i] - minimum[i] for i in range(3))
                    lines.append(
                        "bounds size: "
                        f"({size[0]:.2f}, {size[1]:.2f}, {size[2]:.2f})")
                    lines.append(
                        "bounds min/max: "
                        f"{tuple(round(v, 2) for v in minimum)} -> "
                        f"{tuple(round(v, 2) for v in maximum)}")
                textures = list(dict.fromkeys(
                    group.texture_name for group in fam_obj.materials
                    if group.texture_name))
                lines.append(
                    f"materials: {len(fam_obj.materials)} | "
                    f"textures: {len(textures)}")
                if textures:
                    lines.append("texture refs: " + ", ".join(textures))
                fx_count = len(detect_fx_elements(
                    fam_obj, family.animations))
                lines.append(f"editable FX elements: {fx_count}")
                session = self.viewport.edit_session
                if session is not None \
                        and self.viewport.edit_owner == fam_obj.owner_path:
                    lines.append(
                        f"selected vertices: {len(session.selection)} | "
                        f"edit state: {'modified' if session.dirty else 'clean'}")
                lines.append(
                    f"unsaved geometry: "
                    f"{'yes' if fam_obj.owner_path in self._geom_dirty else 'no'}")
                total_warnings = len(fam_obj.warnings) + len(skeleton.warnings)
                lines.append(f"model warnings: {total_warnings}")
            else:
                lines.append("skeleton not loaded")
            lines.append(
                f"ADES blocks: {len(fam_obj.base_object.ades)} | "
                f"KIDS: {len(fam_obj.kids)} | "
                f"embedded resources: {len(fam_obj.base_object.embedded)}")
            if fam_obj.base_object.unknown_chunks:
                lines.append(
                    "unknown BASE chunks: "
                    + ", ".join(fam_obj.base_object.unknown_chunks))
            transform = fam_obj.base_object.transform
            if transform:
                lines.append(f"STRC: pos={transform.position} "
                             f"scale={transform.scale} "
                             f"euler(deg)={transform.euler}")
                lines.append(
                    f"STRC flags: {transform.flags} | "
                    f"visibility: {transform.vis_limit} | "
                    f"ambient: {transform.ambient_light}")
        elif kind == "texture":
            name = payload
            ref = family.texture_refs.get(name)
            img = family.textures.get(name)
            lines.extend(["texture", f"reference: {name}"])
            status = self._effective_status(
                name, next((d.status for d in family.dependencies
                            if d.raw_ref == name), "?"))
            lines.append(f"status: {status}")
            if ref:
                lines.append(f"source: {ref.source or '-'} "
                             f"-> {ref.display_path}")
                if ref.candidates:
                    lines.append(f"candidates: {len(ref.candidates)}")
            saved = self._saved_choice_for(name)
            if saved:
                lines.append(f"saved choice: {saved.chosen_path}"
                             + (" [STALE]" if saved.stale else ""))
            if img:
                lines.append(f"format: {img.kind} {img.width}x{img.height}, "
                             f"{img.n_planes} planes, "
                             f"{'ByteRun1' if img.compression else 'raw'}")
                lines.append("palette: "
                             + ("own CMAP" if img.palette else
                                ("external PAL" if family.external_palette
                                 else "grayscale fallback")))
        elif kind == "animation":
            name = payload
            anm = family.animations.get(name)
            lines.extend(["animation (bmpanim/VANM)", f"reference: {name}"])
            if anm:
                lines.append(f"bitmaps: {anm.bitmap_names}")
                lines.append(f"frames: {len(anm.frames)} "
                             f"(~{anm.total_duration_ms:.0f} ms/cycle)")
                lines.append("playback: Play/Step/Speed toolbar")
            else:
                lines.append("not loaded / unsupported")
        else:
            lines.append("group node")
        self._set_object_info(lines)

    def _active_model_texture_names(self) -> set[str]:
        """Return every ILBM/frame bitmap used by the active model subtree."""

        family = self._family
        owner = self._selected_owner
        fam_obj = self._owner_to_obj.get(owner) if owner else None
        if family is None or fam_obj is None:
            return set()
        objects = list(fam_obj.iter_tree())
        animation_names, texture_names = self._visual_reference_names(objects)
        for animation_name in animation_names:
            animation = next((
                value for key, value in family.animations.items()
                if str(key).casefold() == animation_name.casefold()), None)
            if animation is not None:
                texture_names.update(
                    str(name) for name in animation.bitmap_names if name)
        return {name.casefold() for name in texture_names}

    def _refresh_texture_usage_highlight(self) -> None:
        """Prioritize and softly tint textures used by the active model.

        The highlight is informational, not QListWidget selection state. Used
        textures are stable-partitioned to the top whenever the active owner
        changes, so the tab always reflects the current model immediately.
        """

        used = self._active_model_texture_names()
        # Same green as the active model rows of Asset Dependencies.
        active_brush = QBrush(theme_list_color("DependencySelected"))
        clear_brush = QBrush()
        text_brush = QBrush()

        current = self.texture_list.currentItem()
        current_name = (
            str(current.data(Qt.ItemDataRole.UserRole)).casefold()
            if current is not None
            and current.data(Qt.ItemDataRole.UserRole) else None)
        selected_names = {
            str(item.data(Qt.ItemDataRole.UserRole)).casefold()
            for item in self.texture_list.selectedItems()
            if item.data(Qt.ItemDataRole.UserRole)
        }

        # Reorder the existing items too. _fill_textures() already prioritizes
        # its initial build, but changing the selected model must reprioritize
        # the list without rebuilding the whole texture catalog.
        previous_block = self.texture_list.blockSignals(True)
        try:
            items = [
                self.texture_list.takeItem(0)
                for _ in range(self.texture_list.count())
            ]
            used_items = []
            other_items = []
            for item in items:
                name = item.data(Qt.ItemDataRole.UserRole)
                folded = str(name).casefold() if name else ""
                item.setForeground(text_brush)
                if not item.data(_PROBLEM_ROLE):
                    item.setBackground(
                        active_brush if folded in used else clear_brush)
                (used_items if folded in used else other_items).append(item)

            for item in used_items + other_items:
                self.texture_list.addItem(item)

            restored_current = None
            for row in range(self.texture_list.count()):
                item = self.texture_list.item(row)
                name = item.data(Qt.ItemDataRole.UserRole)
                folded = str(name).casefold() if name else ""
                item.setSelected(folded in selected_names)
                if current_name is not None and folded == current_name:
                    restored_current = item
            if restored_current is not None:
                self.texture_list.setCurrentItem(restored_current)
        finally:
            self.texture_list.blockSignals(previous_block)

    def _fill_textures(self, family: AssetFamily | None) -> None:
        self.texture_list.clear()
        archive = getattr(family, "setbas_archive", None) or self._setbas
        family_textures = getattr(family, "textures", {})
        tracy_usage = getattr(family, "texture_tracy_usage", {})
        external_palette = getattr(family, "external_palette", None)
        # Used textures are always the first group.  Python's sort is stable,
        # so both the used group and the remaining catalog keep their existing
        # relative order.  Rebuilding after a model switch updates the priority
        # dynamically without maintaining a second texture list.
        used = (self._active_model_texture_names()
                if family is not None and family is self._family else set())
        catalog = list(build_texture_catalog(family, archive))
        catalog.sort(
            key=lambda entry: 0
            if str(entry.name).casefold() in used else 1)
        for entry in catalog:
            name = entry.name
            ref = entry.reference
            img_name = next(
                (key for key in family_textures
                 if key.casefold() == name.casefold()), None)
            img = family_textures.get(img_name) if img_name else None
            status = entry.status
            if ref is not None and ref.path is not None and img is None:
                status = "decode failed"

            source = getattr(ref, "source", "") if ref is not None else ""
            if not source and entry.archive_resource is not None:
                source = "SET.BAS"
            lines = [f"[{status.upper()}] {name}"
                     + (f" (source: {source})" if source else "")]
            if ref is not None:
                if ref.found:
                    lines.append(ref.display_path)
                elif entry.archive_resource is None:
                    lines.append(
                        "NOT FOUND - use Asset Dependencies to assign a file")
                if len(ref.candidates) > 1:
                    lines.append(
                        f"{len(ref.candidates)} candidates "
                        "(see Asset Dependencies)")
            resource = entry.archive_resource
            if resource is not None:
                if resource.error:
                    lines.append(f"archive error: {resource.error}")
                elif not resource.decodable:
                    lines.append(
                        f"{resource.display_payload}: preview unavailable")
                elif img is None:
                    lines.append(
                        "archive texture indexed; decoded on selection/preview")
            tracy_modes = sorted(
                tracy_usage.get(name, set()) - {"none"})
            if tracy_modes:
                lines.append(
                    f"tracy transparency used: {', '.join(tracy_modes)}")
            if img:
                compression = "ByteRun1" if img.compression else "none"
                palette = ("own CMAP" if img.palette else
                           ("external PAL" if external_palette
                            else "MISSING (grayscale preview)"))
                body = "decoded" if img.has_body else "no BODY"
                lines.append(
                    f"{img.kind} {img.width}x{img.height}, "
                    f"{img.n_planes} planes, {compression}, {palette}, {body}")
                # Missing CMAP is normal for many UA textures resolved with
                # the external family palette. Keep that implementation detail
                # out of the compact Asset Textures list. Other real warnings
                # remain visible.
                for warning in img.warnings:
                    if "no cmap palette in file" in str(warning).casefold():
                        continue
                    lines.append(f"warning: {warning}")
            item = QListWidgetItem("\n".join(lines))
            item.setData(Qt.ItemDataRole.UserRole, name)
            _mark_problem_item(
                item,
                status in _PROBLEM_STATUSES
                or (ref is not None and not ref.found
                    and entry.archive_resource is None))
            # Status remains visible in the label/icon; valid/error rows do not
            # compete with the model-usage background through colored text.
            item.setForeground(QBrush())
            qimage = self._texture_qimage(img_name) if img_name else None
            if qimage is not None and not qimage.isNull():
                item.setIcon(_checker_thumbnail(qimage))
            else:
                item.setIcon(_status_icon(status))
            self.texture_list.addItem(item)
        self._refresh_texture_usage_highlight()

    def _fill_chunks(self, family: AssetFamily) -> None:
        self.chunk_tree.clear()
        if not family.base_asset or not family.base_asset.tree:
            return

        def add_chunk(chunk, parent):
            item = QTreeWidgetItem(
                [chunk.display_name, str(chunk.size), f"0x{chunk.offset:X}"]
            )
            if parent is None:
                self.chunk_tree.addTopLevelItem(item)
            else:
                parent.addChild(item)
            for child in chunk.children:
                add_chunk(child, item)

        for root in family.base_asset.tree.roots:
            add_chunk(root, None)
        self.chunk_tree.expandAll()
        self.chunk_tree.resizeColumnToContents(0)

    def _fill_checks(self, family: AssetFamily) -> None:
        self.checks_list.clear()
        for status, text in family.checks:
            self.checks_list.addItem(f"[{status}] {text}")
        self.warning_list.clear()
        for warning in family.warnings:
            self.warning_list.addItem(warning)
