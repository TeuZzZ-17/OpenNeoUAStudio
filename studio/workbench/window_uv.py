"""AssemblyWindow mixin: UV editing and model textures: UV topology edits, UV reset/revert, texture
choice and loading."""

from __future__ import annotations

import copy

from PySide6.QtCore import Qt
from PySide6.QtGui import (
    QImage,
)
from PySide6.QtWidgets import (
    QDialog,
    QInputDialog,
    QMenu,
    QMessageBox,
)

from studio.assets.catalog import build_texture_catalog, classify_texture_assignment
from studio.assets.mapping import (
    MappingEditError,
    rewrite_block_texture_template,
)
from studio.formats.setbas import decode_texture
from studio.ui.uv_editor import (
    apply_uv_vertex_deletion,
    apply_uv_vertex_insertion,
    plan_uv_vertex_deletion,
    plan_uv_vertex_insertion,
    UVLoop,
    UVTopologyError,
)
from studio.ui.widgets import (
    checker_thumbnail as _checker_thumbnail,
    crop_uv_region,
    TexturePickerDialog,
)


class _UvTexturesMixin:
    """Part of AssemblyWindow; `self` is always the AssemblyWindow."""

    @staticmethod
    def _uv_reason_text(exc: Exception) -> str:
        text = str(exc).rstrip(".")
        return text[:1].upper() + text[1:] + "."

    def _uv_topology_reason(
            self, operation: str,
            handle: tuple[tuple[str, int, int], int] | None = None) -> str:
        if not self._editing_allowed():
            return "Enter Edit Mode before changing UV topology."
        if self.viewport.paste_preview_active:
            return "Finish or cancel the active paste preview first."
        if len(self._uv_contexts) != 1 or self.uv_editor.loop_count() != 1:
            return "Select exactly one editable UV loop/polygon first."
        key, context = next(iter(self._uv_contexts.items()))
        if key[0] == "@VANM":
            return (
                "VANM supports coordinate editing only; changing the UV "
                "group length would invalidate its frame layout.")
        fam_obj, block, _block_index, atts_index, poly_id = context
        selected = self.uv_editor.selected_handles()
        if handle is None:
            if len(selected) != 1:
                return "Select exactly one UV handle first."
            handle = next(iter(selected))
        if handle[0] != key or not (0 <= handle[1] < len(
                block.olpl[atts_index]
                if atts_index < len(block.olpl) else [])):
            return "Select exactly one handle in the visible UV loop."
        if not self.uv_editor.loop_editable(key):
            return "The visible UV loop is read-only."
        if (block.class_id or "").lower() != "amesh.class":
            return "Topology editing is available only for amesh OLPL."
        owner = fam_obj.owner_path
        if self._mapping_index is None:
            return "The polygon mapping index is unavailable."
        refs = self._mapping_index.refs.get(poly_id, [])
        if len(refs) != 1 \
                or refs[0].block is not block \
                or refs[0].atts_index != atts_index:
            return "The polygon must have one unambiguous ATTS/OLPL mapping."
        if any(
                element.owner_path == owner and poly_id in element.poly_ids
                for element in self._fx_elements):
            return (
                "Fixed-shape FX polygons do not support vertex topology edits.")
        model = getattr(fam_obj, "skeleton", None)
        if model is None:
            return "The selected model has no editable skeleton."
        try:
            planner = (
                plan_uv_vertex_insertion
                if operation == "add" else plan_uv_vertex_deletion)
            planner(model, block, poly_id, atts_index, handle[1])
        except UVTopologyError as exc:
            return self._uv_reason_text(exc)
        return ""

    def _uv_add_vertex_reason(self) -> str:
        return self._uv_topology_reason("add")

    def _uv_delete_vertex_reason(self) -> str:
        return self._uv_topology_reason("delete")

    def _uv_revert_reason(self) -> str:
        if not self._editing_allowed():
            return "Enter Edit Mode before changing UV coordinates."
        selected = self.uv_editor.selected_handles()
        if not selected:
            return "Select one or more edited UV handles first."
        for key, index in selected:
            tracker, tracked_key = self._uv_original_tracker(key)
            original = tracker.get(tracked_key)
            storage = self._uv_storage(key)
            if original is None or storage is None:
                continue
            current = self._uv_storage_points(storage)
            if index < len(original) and index < len(current) \
                    and tuple(current[index]) != tuple(original[index]):
                return ""
        return "The selected UV handles have no pending changes to revert."

    def _select_uv_phase(self, index):
        key = self.uv_phase_combo.itemData(index)
        self.uv_editor.set_visible_loop_key(key)
        if key is None:
            self.uv_editor.select_all()
        else:
            self.uv_editor.select_handles({
                (key, i)
                for i in range(len(self.uv_editor.loop_uvs().get(key, ())))
            })

    def _sync_uv_add_vertex_button(self, *_args) -> None:
        combo = getattr(self, "uv_phase_combo", None)
        if combo is not None:
            keys = self.uv_editor.loop_keys()
            if tuple(combo.itemData(i) for i in range(1, combo.count())) != keys:
                combo.clear()
                combo.addItem("All UV phases", None)
                for index, key in enumerate(keys):
                    combo.addItem(f"Phase {index + 1} — {'Yellow' if index % 2 == 0 else 'Cyan'}", key)
            combo.setEnabled(bool(keys))
        add_button = getattr(self, "uv_add_vertex_button", None)
        if add_button is not None:
            reason = self._uv_add_vertex_reason()
            add_button.setEnabled(not reason)
            add_button.setToolTip(
                reason or
                "Insert one midpoint in POO2, POL2 and OLPL after the selected "
                "handle. The operation can be undone.")
        delete_button = getattr(self, "uv_delete_vertex_button", None)
        if delete_button is not None:
            reason = self._uv_delete_vertex_reason()
            delete_button.setEnabled(not reason)
            delete_button.setToolTip(
                reason or
                "Remove the selected POL2 winding and OLPL coordinate while "
                "retaining POO2 indices. The operation can be undone.")
        revert_button = getattr(self, "uv_revert_button", None)
        if revert_button is not None:
            reason = self._uv_revert_reason()
            revert_button.setEnabled(not reason)
            revert_button.setToolTip(
                reason or "Revert only the selected edited UV handles.")
        reset_all = getattr(self, "uv_reset_all_button", None)
        if reset_all is not None:
            owner = self._selected_owner
            changed = bool(
                self._editing_allowed() and owner is not None
                and (any(key[0] == owner for key in self._uv_original) or self._owner_vanm_uv_keys(owner)))
            reset_all.setEnabled(changed)
            reset_all.setToolTip(
                "Restore every changed editable UV loop in the active model."
                if changed else
                "No changed editable UV loop is available to reset.")

    @staticmethod
    def _configure_uv_menu_action(action, reason: str,
                                  description: str) -> None:
        action.setEnabled(not reason)
        action.setToolTip(reason or description)
        action.setStatusTip(reason or description)

    def _show_uv_handle_context_menu(
            self, key, index: int, global_position) -> None:
        if key not in self.uv_editor.loop_keys():
            return
        handle = (key, index)
        menu = QMenu(self.uv_editor)
        add = menu.addAction("Add Vertex After")
        add_reason = self._uv_topology_reason("add", handle)
        self._configure_uv_menu_action(
            add, add_reason,
            "Insert one midpoint in POO2, POL2 and OLPL after the selected "
            "handle.")
        add.triggered.connect(
            lambda _checked=False: self._run_uv_topology_from_handle(
                "add", handle))
        delete = menu.addAction("Delete Vertex")
        delete_reason = self._uv_topology_reason("delete", handle)
        self._configure_uv_menu_action(
            delete, delete_reason,
            "Remove this winding/OLPL vertex without compacting POO2.")
        delete.triggered.connect(
            lambda _checked=False: self._run_uv_topology_from_handle(
                "delete", handle))
        menu.addSeparator()
        select_this = menu.addAction("Select This Vertex")
        select_this.triggered.connect(
            lambda _checked=False: self.uv_editor.select_handle(*handle))
        select_all = menu.addAction("Select All UVs")
        self._configure_uv_menu_action(
            select_all,
            "" if self.uv_editor.loop_count() else "No UV loop is visible.",
            "Select every handle in every visible UV loop.")
        select_all.triggered.connect(self.uv_editor.select_all)
        clear = menu.addAction("Clear Selection")
        self._configure_uv_menu_action(
            clear,
            "" if self.uv_editor.selected_handles()
            else "No UV handle is selected.",
            "Clear the current UV handle selection.")
        clear.triggered.connect(self.uv_editor.select_none)
        menu.addSeparator()
        revert = menu.addAction("Revert Selected UV")
        revert_reason = self._uv_revert_reason()
        self._configure_uv_menu_action(
            revert, revert_reason,
            "Restore the selected handles to their original UV coordinates.")
        revert.triggered.connect(self._revert_selected_uv)
        menu.exec(global_position)

    def _run_uv_topology_from_handle(self, operation: str, handle) -> None:
        self.uv_editor.select_handle(*handle)
        if operation == "add":
            self._add_uv_vertex()
        else:
            self._delete_uv_vertex()

    def _add_uv_vertex(self) -> None:
        reason = self._uv_add_vertex_reason()
        if reason:
            self._notify(f"Add Vertex refused: {reason}", 8000)
            return
        self._apply_uv_topology_edit("add")

    def _delete_uv_vertex(self) -> None:
        reason = self._uv_delete_vertex_reason()
        if reason:
            self._notify(f"Delete Vertex refused: {reason}", 8000)
            return
        self._apply_uv_topology_edit("delete")

    def _apply_uv_topology_edit(self, operation: str) -> None:
        key, context = next(iter(self._uv_contexts.items()))
        fam_obj, block, _block_index, atts_index, poly_id = context
        owner = fam_obj.owner_path
        model = fam_obj.skeleton
        selected = next(iter(self.uv_editor.selected_handles()))[1]
        label = "Add Vertex" if operation == "add" else "Delete Vertex"
        before = self._capture_topology_state(owner)
        if before is None:
            self._notify(
                f"{label} refused: topology snapshot unavailable.", 8000)
            return
        had_topology_original = owner in self._topology_original
        previous_geom_original = copy.deepcopy(
            self._geom_original.get(owner))
        previous_dirty = self._geom_dirty.get(owner)
        previous_undo = list(self._edit_undo_stack)
        previous_redo = list(self._edit_redo_stack)
        try:
            if operation == "add":
                plan = plan_uv_vertex_insertion(
                    model, block, poly_id, atts_index, selected)
            else:
                plan = plan_uv_vertex_deletion(
                    model, block, poly_id, atts_index, selected)
            self._topology_original.setdefault(owner, copy.deepcopy(before))
            self._remember_geometry_original(owner)
            if operation == "add":
                apply_uv_vertex_insertion(model, block, plan)
                selected_handle = plan.insert_at
                selected_vertex = plan.new_point_index
            else:
                apply_uv_vertex_deletion(model, block, plan)
                selected_handle = min(
                    plan.vertex_index, len(model.polygons[poly_id]) - 1)
                selected_vertex = model.polygons[poly_id][selected_handle]
            self._uv_history_before = None
            self._refresh_object_material_faces(fam_obj)
            self._selected_owner = owner
            self._selected_polys = {poly_id}
            self._selected_poly = poly_id
            self.viewport.refresh_family_materials()
            self._rebuild_workbench(self._family, owner)
            self._refresh_fx_elements()
            self.viewport.set_selected_owner(owner)
            self.viewport.set_selected_polygon(poly_id)
            self.viewport.set_highlight_polys({poly_id})
            if self.edit_toggle_action.isChecked():
                self.viewport.enter_edit_mode_with_vertices(
                    owner, [selected_vertex], pick_polygons=True)
                self.viewport.configure_edit_interaction(
                    selected_only=False, pick_polygons=True)
                session = self.viewport.edit_session
                if session is not None:
                    session._undo.clear()
                    session._redo.clear()
            self._on_geometry_edited(owner)
            after = self._capture_topology_state(owner)
            if after is None:
                raise UVTopologyError(
                    "the committed topology could not be verified")
            self._record_edit_command({
                "kind": "topology",
                "owner": owner,
                "before": before,
                "after": after,
                "label": label,
            })
            self._fill_polygon_inspector(poly_id)
            self.uv_editor.select_handle(key, selected_handle)
            self._sync_edit_action_states()
            if operation == "add":
                message = (
                    f"Added POO2 #{plan.new_point_index} and connected it "
                    f"between winding vertices {plan.edge_start} and "
                    f"{plan.insert_at}. Undo is available.")
            else:
                message = (
                    f"Removed winding/OLPL vertex {plan.vertex_index}; "
                    f"POO2 #{plan.point_index} was retained to preserve shared "
                    "indices. Undo is available.")
            self._notify(message, 8000)
        except Exception as exc:
            try:
                rolled_back = self._restore_topology_state(owner, before)
            except Exception as rollback_exc:
                rolled_back = (
                    self._topology_content(
                        self._capture_topology_state(owner))
                    == self._topology_content(before))
                self._log(
                    f"{label} rollback UI refresh failed after data restore: "
                    f"{rollback_exc}")
            if not had_topology_original:
                self._topology_original.pop(owner, None)
            if previous_geom_original is None:
                self._geom_original.pop(owner, None)
            else:
                self._geom_original[owner] = previous_geom_original
            if previous_dirty is None:
                self._geom_dirty.pop(owner, None)
            else:
                self._geom_dirty[owner] = previous_dirty
            self._edit_undo_stack[:] = previous_undo
            self._edit_redo_stack[:] = previous_redo
            self._uv_history_before = None
            try:
                self._refresh_object_material_faces(fam_obj)
                self.viewport.refresh_family_materials()
                self._rebuild_workbench(self._family, owner)
                self._refresh_fx_elements()
                self.viewport.set_selected_owner(owner)
                self.viewport.set_selected_polygon(poly_id)
                self.viewport.set_highlight_polys({poly_id})
                self._fill_polygon_inspector(poly_id)
                self._sync_editor_context()
            except Exception as refresh_exc:
                self._log(
                    f"{label} rollback UI refresh failed: {refresh_exc}")
            self._notify(
                f"{label} failed and "
                f"{'was rolled back' if rolled_back else 'rollback failed'}: "
                f"{exc}.", 8000)
        finally:
            self._sync_uv_add_vertex_button()

    def _update_uv_editor(self, poly_id: int | None) -> None:
        self._uv_phase_links = {}
        self._uv_ctx = None
        self._uv_contexts = {}
        if poly_id is None or self._workbench_obj is None \
                or self._mapping_index is None:
            self.uv_editor.set_loops(
                None, [],
                "Select a mapped polygon to edit its texture coordinates.")
            self._sync_uv_add_vertex_button()
            return

        element = self._fx_element_for_polygon(poly_id)
        if element is not None:
            self._update_fx_uv_editor(element)
            return

        requested = self._resolved_geometry_polys()
        if not requested:
            requested = set(self._selected_polys)
        if not requested:
            requested = {poly_id}
        ordered = [poly_id] + sorted(requested - {poly_id})
        loops = []
        skipped: list[tuple[int, str]] = []
        image = None
        message = ""
        model = self._workbench_obj.skeleton
        for selected_poly in ordered:
            if not (0 <= selected_poly < len(model.polygons)):
                skipped.append((selected_poly, "invalid polygon"))
                continue
            refs = self._mapping_index.refs.get(selected_poly, [])
            if len(refs) != 1:
                skipped.append((
                    selected_poly,
                    "unmapped" if not refs else "duplicate mapping"))
                continue
            ref = refs[0]
            block = ref.block
            texture = block.texture
            if texture is not None and texture.kind == "bmpanim":
                if len(requested) != 1:
                    skipped.append((selected_poly, "animated/VANM"))
                    continue
                frame = self.viewport.current_animation_frame_data(
                    self._workbench_obj.owner_path, ref.block_index)
                animation_group = self.viewport.current_animation_group(
                    self._workbench_obj.owner_path, ref.block_index)
                source_uvs = []
                if frame is not None:
                    source_uvs = list(
                        frame[1][:len(model.polygons[selected_poly])])
                    image = frame[0]
                    message = (
                        f"UV source: VANM current {frame[2]}. Coordinate edits "
                        "are exported as a verified ANM file by Export Asset "
                        "Family.")
                elif ref.atts_index < len(block.olpl):
                    source_uvs = list(block.olpl[ref.atts_index])
                    message = "The current VANM frame group is unavailable."
                if source_uvs:
                    key = (
                        ("@VANM", animation_group[0], animation_group[1])
                        if animation_group is not None else
                        (self._workbench_obj.owner_path,
                         ref.block_index, ref.atts_index))
                    editable = bool(
                        animation_group is not None
                        and self._animation_uv_writable(
                            animation_group[0], animation_group[1]))
                    if editable:
                        self._uv_contexts[key] = (
                            self._workbench_obj, None,
                            animation_group[0], animation_group[1],
                            selected_poly)
                    loops.append(UVLoop(
                        key, selected_poly, source_uvs, editable))
                else:
                    skipped.append((selected_poly, "VANM frame unavailable"))
                continue
            block_class = (block.class_id or "").lower()
            if block_class == "area.class":
                # Sky panels and other area.class faces use the texture OTL2
                # outline directly. It is read-only, but belongs in the UV
                # preview and must use the same lazy texture loader as amesh.
                if ref.atts_index >= len(block.olpl):
                    skipped.append((selected_poly, "missing OTL2 outline"))
                    continue
                uvs = list(block.olpl[ref.atts_index])
                if not uvs or len(uvs) != len(model.polygons[selected_poly]):
                    skipped.append((
                        selected_poly, "POL2/OTL2 count mismatch"))
                    continue
                key = (self._workbench_obj.owner_path,
                       ref.block_index, ref.atts_index)
                loops.append(UVLoop(key, selected_poly, uvs, False))
                if image is None:
                    texture_name = texture.name if texture else ""
                    if texture_name:
                        loaded_name = self._ensure_model_texture_loaded(
                            texture_name, show_error=False)
                        image = self._texture_qimage(
                            loaded_name or texture_name)
                message = "area.class OTL2 preview (read-only)."
                continue
            if block_class != "amesh.class":
                skipped.append((selected_poly, "non-amesh mapping"))
                continue
            if ref.atts_index >= len(block.olpl):
                skipped.append((selected_poly, "missing OLPL"))
                continue
            uvs = list(block.olpl[ref.atts_index])
            if not uvs or len(uvs) != len(model.polygons[selected_poly]):
                skipped.append((selected_poly, "POL2/OLPL count mismatch"))
                continue
            key = (
                self._workbench_obj.owner_path,
                ref.block_index, ref.atts_index)
            context = (
                self._workbench_obj, block, ref.block_index,
                ref.atts_index, selected_poly)
            self._uv_contexts[key] = context
            loops.append(UVLoop(
                key, selected_poly, uvs, self._editing_allowed()))
            if image is None:
                texture_name = (
                    texture.name if texture else "")
                if texture_name:
                    # Archive-only textures are decoded lazily when selected
                    # in the resource list.  Polygon selection reaches the
                    # UV editor directly, so request the same lazy load here
                    # or the 3D viewport can be textured while UV stays on
                    # its checkerboard.
                    loaded_name = self._ensure_model_texture_loaded(
                        texture_name, show_error=False)
                    image = self._texture_qimage(
                        loaded_name or texture_name)

        if loops:
            first_key = next(
                (loop.key for loop in loops
                 if loop.key in self._uv_contexts), None)
            if first_key is not None:
                self._uv_ctx = self._uv_contexts[first_key]
        category_counts = {}
        for _skipped_poly, category in skipped:
            category_counts[category] = category_counts.get(category, 0) + 1
        if len(requested) > 1:
            details = ", ".join(
                f"{count} {category}"
                for category, count in sorted(category_counts.items()))
            message = (
                f"{len(loops)} compatible UV loop(s) shown; "
                f"{len(skipped)} incompatible polygon(s) skipped"
                + (f" ({details})." if details else "."))
            self.statusBar().showMessage(message, 7000)
        elif not loops:
            if skipped and skipped[0][1] == "unmapped":
                message = (
                    "This polygon has no UV mapping. Open Tools > "
                    "Mapping Repair first.")
            else:
                reason = skipped[0][1] if skipped else "UV data unavailable"
                message = f"This polygon is not editable: {reason}."
        self.uv_editor.set_loops(image, loops, message)
        self._sync_uv_add_vertex_button()

    def _expand_uv_phase_updates(self, loop_uvs):
        expanded = dict(loop_uvs)
        for primary, links in getattr(self, "_uv_phase_links", {}).items():
            if primary in expanded:
                for key, indices in links:
                    expanded[key] = [expanded[primary][index] for index in indices]
        return expanded

    def _on_uv_changed(self, loop_uvs: dict) -> None:
        if not self._editing_allowed():
            self._update_uv_editor(self._selected_poly)
            return
        if not self._uv_contexts:
            return
        loop_uvs = self._expand_uv_phase_updates(loop_uvs)
        changed = {}
        before = {}
        for key, uvs in loop_uvs.items():
            if key not in self._uv_contexts:
                continue
            storage = self._uv_storage(key)
            if storage is None:
                return
            current = self._uv_storage_points(storage)
            updated = [tuple(uv) for uv in uvs]
            if current != updated:
                before[key] = current
                changed[key] = updated
        if not changed:
            return
        history_before = copy.deepcopy(self._uv_history_before)
        if self._uv_history_before is None:
            self._uv_history_before = {}
        for key, current in before.items():
            self._uv_history_before.setdefault(key, current)
        if not self._apply_uv_histories(changed, refresh=False):
            self._uv_history_before = history_before
            self._notify(
                "UV edit failed; all selected loops were left unchanged.",
                7000)
            self._update_uv_editor(self._selected_poly)
            return
        self._sync_geometry_save_controls()
        self._update_editor_status()
        self._sync_uv_add_vertex_button()

    def _on_uv_edit_finished(self) -> None:
        if not self._editing_allowed():
            return
        if not self._uv_contexts or self._family is None:
            return
        history = self._uv_history_before or {}
        self._uv_history_before = None
        before_snapshot = {}
        after_snapshot = {}
        for key, before in history.items():
            storage = self._uv_storage(key)
            if storage is None:
                continue
            after = self._uv_storage_points(storage)
            if list(before) != after:
                before_snapshot[key] = list(before)
                after_snapshot[key] = after
        if before_snapshot:
            self._record_edit_command({
                "kind": "uv_multi",
                "before": before_snapshot,
                "after": after_snapshot,
                "label": (
                    "UV multi-loop edit"
                    if len(before_snapshot) > 1 else "UV edit"),
            })
        poly_id = self._uv_ctx[4] if self._uv_ctx else self._selected_poly
        self.viewport.refresh_family_materials()
        self.viewport.set_selected_polygon(poly_id)
        self._sync_animation_controls()
        self._sync_editor_context()
        self._sync_uv_add_vertex_button()

    def _restore_uv(self, key: tuple[str, int, int],
                    uvs: list[tuple[int, int]]) -> None:
        storage = self._uv_storage(key)
        if storage is None:
            return
        updated = [tuple(uv) for uv in uvs]
        kind, fam_obj_or_name, container, atts_index = storage
        if kind == "vanm":
            container.texcoord_groups[atts_index] = updated
            return
        fam_obj = fam_obj_or_name
        block = container
        block_index = int(key[1])
        block.olpl[atts_index] = updated
        entry = block.atts[atts_index] if atts_index < len(block.atts) else None
        if block_index < len(fam_obj.materials) and entry is not None:
            group = fam_obj.materials[block_index]
            for index, (pid, _old_uvs, shade) in enumerate(group.faces):
                if pid == entry.poly_id:
                    group.faces[index] = (pid, updated, shade)
                    break

    def _revert_selected_uv(self) -> None:
        reason = self._uv_revert_reason()
        if reason:
            self._notify(f"UV revert refused: {reason}", 6000)
            return
        selected = self.uv_editor.selected_handles()
        targets = {}
        before = {}
        reverted_count = 0
        for key, index in selected:
            tracker, tracked_key = self._uv_original_tracker(key)
            original = tracker.get(tracked_key)
            storage = self._uv_storage(key)
            if original is None or storage is None:
                continue
            current = self._uv_storage_points(storage)
            if not (index < len(original) and index < len(current)) \
                    or current[index] == tuple(original[index]):
                continue
            before.setdefault(key, current)
            target = targets.setdefault(key, list(current))
            target[index] = tuple(original[index])
            reverted_count += 1
        targets = self._expand_uv_phase_updates(targets)
        for key in targets:
            if key not in before:
                storage = self._uv_storage(key)
                if storage is not None:
                    before[key] = self._uv_storage_points(storage)
        if not targets or not self._apply_uv_histories(targets):
            self._notify(
                "UV revert failed; all selected loops were left unchanged.",
                7000)
            return
        self._record_edit_command({
            "kind": "uv_multi",
            "before": before,
            "after": targets,
            "label": "UV revert",
        })
        poly_id = self._uv_ctx[4] if self._uv_ctx else None
        self._update_uv_editor(poly_id)
        self.uv_editor.select_handles(selected)
        self._sync_geometry_save_controls()
        self._update_editor_status()
        self._sync_uv_add_vertex_button()
        self.statusBar().showMessage(
            f"Reverted {reverted_count} selected UV handle(s).", 4000)

    def _reset_all_uvs(self) -> None:
        if not self._require_editing("Reset All UVs"):
            return
        owner = self._selected_owner
        targets = {
            key: list(original)
            for key, original in self._uv_original.items()
            if key[0] == owner and self._uv_storage(key) is not None
        }
        targets.update({
            ("@VANM", name, group_index): list(original)
            for (name, group_index), original
            in self._vanm_uv_original.items()
            if (name, group_index) in self._owner_vanm_uv_keys(owner)
            and self._uv_storage(
                ("@VANM", name, group_index)) is not None
        })
        if not targets:
            return
        before = {}
        poly_ids = set()
        for key in targets:
            storage = self._uv_storage(key)
            before[key] = self._uv_storage_points(storage)
            if storage[0] == "olpl":
                _kind, _fam_obj, block, atts_index = storage
                if atts_index < len(block.atts):
                    poly_ids.add(block.atts[atts_index].poly_id)
        if len(targets) > 1 or len(poly_ids) > 1:
            answer = QMessageBox.warning(
                self, "Reset all UVs?",
                f"Restore {len(targets)} changed UV loops across "
                f"{len(poly_ids)} polygon(s)?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        if not self._apply_uv_histories(targets):
            self._notify(
                "Reset All UVs failed; all loops were left unchanged.", 7000)
            return
        self._record_edit_command({
            "kind": "uv_multi",
            "before": before,
            "after": targets,
            "label": "reset all UVs",
        })
        self._update_uv_editor(self._selected_poly)
        self._sync_geometry_save_controls()
        self._update_editor_status()
        self._sync_uv_add_vertex_button()
        self._notify(
            f"Reset {len(targets)} UV loop(s) to their loaded positions.",
            5000)

    def _reset_model(self) -> None:
        if not self._require_editing("Reset Model"):
            return
        owner = self._selected_owner
        has_geometry = owner in self._geom_dirty
        has_texture = any(key[0] == owner for key in self._texture_original)
        has_uv = (
            any(key[0] == owner for key in self._uv_original)
            or bool(self._owner_vanm_uv_keys(owner)))
        if owner is None or not (has_geometry or has_texture or has_uv):
            return
        answer = QMessageBox.warning(
            self, "Reset model?",
            "Reset restores the selected model to its original state and "
            "discards its current vertex edits and texture previews. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        fam_obj = self._owner_to_obj.get(owner)
        model = getattr(fam_obj, "skeleton", None)
        texture_keys = [key for key in self._texture_original
                        if key[0] == owner]
        uv_keys = [key for key in self._uv_original if key[0] == owner]
        vanm_uv_keys = self._owner_vanm_uv_keys(owner)
        before_state = {
            "topology": self._capture_topology_state(owner)
            if owner in self._topology_original else None,
            "geometry": (
                list(model.points)
                if model is not None and owner not in self._topology_original
                else None),
            "textures": {
                key: getattr(
                    self._owner_to_obj[key[0]].base_object.ades[key[1]],
                    key[2]).name
                for key in texture_keys
            },
            "uvs": {
                key: list(self._owner_to_obj[key[0]].base_object
                          .ades[key[1]].olpl[key[2]])
                for key in uv_keys
            },
            "vanm_uvs": {
                key: self._uv_storage_points(
                    self._uv_storage(("@VANM", key[0], key[1])))
                for key in vanm_uv_keys
            },
        }
        resume_edit = self.edit_toggle_action.isChecked()
        if self.viewport.is_edit_mode:
            self.viewport.exit_edit_mode()
        original = self._geom_original.get(owner)
        topology_original = self._topology_original.get(owner)
        if topology_original is not None:
            self._restore_topology_state(owner, topology_original)
        elif model is not None and original is not None:
            model.points[:] = list(original)
        texture_restore = {
            key: original
            for key, original in self._texture_original.items()
            if key[0] == owner
        }
        if texture_restore:
            self._apply_texture_history(texture_restore)
        for key, uvs in list(self._uv_original.items()):
            if key[0] != owner:
                continue
            self._restore_uv(key, uvs)
            del self._uv_original[key]
        for key in list(vanm_uv_keys):
            self._restore_uv(
                ("@VANM", key[0], key[1]),
                self._vanm_uv_original[key])
            del self._vanm_uv_original[key]
        after_state = {
            "topology": self._capture_topology_state(owner)
            if topology_original is not None else None,
            "geometry": (
                list(model.points)
                if model is not None and topology_original is None
                else None),
            "textures": {
                key: getattr(
                    self._owner_to_obj[key[0]].base_object.ades[key[1]],
                    key[2]).name
                for key in texture_keys
            },
            "uvs": {
                key: list(self._owner_to_obj[key[0]].base_object
                          .ades[key[1]].olpl[key[2]])
                for key in uv_keys
            },
            "vanm_uvs": {
                key: self._uv_storage_points(
                    self._uv_storage(("@VANM", key[0], key[1])))
                for key in vanm_uv_keys
            },
        }
        self._geom_dirty.pop(owner, None)
        if not texture_restore:
            self.viewport.refresh_family_materials()
        self._fill_polygon_inspector(self._selected_poly)
        self._sync_geometry_save_controls()
        self._update_editor_status()
        if resume_edit:
            self.edit_toggle_action.setChecked(True)
        self._sync_editor_context()
        self._record_edit_command({
            "kind": "model_state", "owner": owner,
            "before": before_state, "after": after_state,
            "label": "model reset",
        })
        self.statusBar().showMessage("Model reset to its original state.", 5000)

    def _available_model_textures(self) -> list[str]:
        family = self._family
        if family is None:
            return []
        archive = family.setbas_archive or self._setbas
        loaded = {str(name).casefold() for name in family.textures}
        return [
            entry.name for entry in build_texture_catalog(family, archive)
            if (
                entry.archive_resource is not None and entry.decodable
            ) or entry.name.casefold() in loaded
        ]

    def _available_model_animations(self) -> list[str]:
        family = self._family
        if family is None:
            return []
        return sorted(
            family.animations,
            key=lambda value: value.casefold())

    def _visual_texture_available(self, name: str) -> bool:
        family = self._family
        if family is None:
            return False
        if any(key.casefold() == str(name).casefold()
               for key in family.textures):
            return True
        archive = family.setbas_archive or self._setbas
        return bool(
            archive is not None
            and any(resource.decodable
                    for resource in archive.find(name, "ilbm.class")))

    def _ensure_model_texture_loaded(
            self, name: str, *, show_error: bool = True) -> str | None:
        family = self._family
        if family is None:
            return None
        existing = next((key for key in family.textures
                         if key.lower() == name.lower()), None)
        if existing is not None:
            return existing
        archive = family.setbas_archive or self._setbas
        if archive is None:
            return None
        cache_key = (id(archive), str(name).casefold())
        if cache_key in self._texture_preview_cache:
            return self._texture_preview_cache[cache_key]
        resources = archive.find(name, "ilbm.class")
        resource = next((item for item in resources if item.decodable), None)
        if resource is None:
            self._texture_preview_cache[cache_key] = None
            return None
        try:
            image = decode_texture(archive, resource)
        except Exception as exc:
            self._texture_preview_cache[cache_key] = None
            if show_error:
                QMessageBox.warning(self, "Texture load failed", str(exc))
            return None
        family.textures[resource.resource_name] = image
        self._texture_preview_cache[cache_key] = resource.resource_name
        return resource.resource_name

    def _load_visual_texture_item(self, item) -> None:
        if item is None or self._family is None:
            return
        name = item.data(Qt.ItemDataRole.UserRole)
        if not name:
            return
        loaded_name = self._ensure_model_texture_loaded(
            name, show_error=False)
        if loaded_name is None:
            return
        qimage = self._texture_qimage(loaded_name)
        if qimage is not None and not qimage.isNull():
            item.setIcon(_checker_thumbnail(qimage))
            item.setToolTip(
                f"{name}\nDecoded lazily from the active texture sources.")

    def _texture_thumbnail_for_picker(self, name: str) -> QImage | None:
        image = self._texture_qimage(name)
        if image is not None:
            return image
        family = self._family
        if family is None:
            return None
        archive = family.setbas_archive or self._setbas
        if archive is None:
            return None
        resource = next(
            (item for item in archive.find(name, "ilbm.class")
             if item.decodable), None)
        if resource is None:
            return None
        try:
            decoded = decode_texture(archive, resource)
        except Exception:
            return None
        family.textures[resource.resource_name] = decoded
        return self._texture_qimage(resource.resource_name)

    def _binding_preview_frames(self, name: str, binding_kind: str):
        if binding_kind == "bmpanim":
            entry = self._animation_for_name(name)
            if entry is None:
                return []
            animation = entry[1]
            frames = []
            for frame in animation.frames:
                if not (0 <= frame.frame_id < len(animation.bitmap_names) and
                        0 <= frame.texcoords_id < len(animation.texcoord_groups)):
                    continue
                image = self._texture_thumbnail_for_picker(animation.bitmap_names[frame.frame_id])
                cropped = crop_uv_region(image, [animation.texcoord_groups[frame.texcoords_id]])
                if not cropped.isNull():
                    frames.append((cropped, frame.duration_ms))
            return frames
        image = self._texture_thumbnail_for_picker(name)
        groups = list(self.uv_editor.loop_uvs().values())
        cropped = crop_uv_region(image, groups)
        return [(cropped, 180)] if not cropped.isNull() else []


    def _choose_model_texture(self, names: list[str], current: str,
                              binding_kind: str = "ilbm") -> str | None:
        cache = {}
        def frames(name):
            if name not in cache:
                cache[name] = self._binding_preview_frames(name, binding_kind)
            return cache[name]
        dialog = TexturePickerDialog(
            names, current, lambda name: frames(name)[0][0] if frames(name) else None,
            self, frame_loader=frames)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self._notify("Texture selection cancelled.", 3000)
            return None
        return dialog.selected_name()

    def _choose_texture_binding_group(self, groups):
        if len(groups) == 1:
            return groups[0]
        labels = [
            f"{len(group.bindings)} block(s) {group.binding_slot}: "
            f"{group.binding_name} "
            f"({group.binding_kind}, {len(group.affected_polys)} polygons)"
            for group in groups
        ]
        selected, accepted = QInputDialog.getItem(
            self, "Choose texture binding",
            "Replace which logical material binding?",
            labels, 0, False)
        if not accepted:
            return None
        return groups[labels.index(selected)]

    def _replace_selected_texture_structurally(
            self, owner: str, group, bindings,
            loaded_name: str) -> bool:
        """Split shared AMESH mappings so only selected POL2s are retargeted."""

        fam_obj = self._owner_to_obj.get(owner)
        if fam_obj is None:
            return False
        reason = self._geometry_writer_reason(owner, fam_obj)
        if reason:
            self._notify(f"Load Texture refused: {reason}.", 8000)
            return False
        before = self._capture_topology_state(owner)
        if before is None:
            return False
        had_topology_original = owner in self._topology_original
        had_geom_original = owner in self._geom_original
        previous_geom_original = copy.deepcopy(
            self._geom_original.get(owner))
        had_dirty = owner in self._geom_dirty
        previous_dirty = self._geom_dirty.get(owner)
        previous_undo = list(self._edit_undo_stack)
        previous_redo = list(self._edit_redo_stack)
        try:
            self._topology_original.setdefault(
                owner, copy.deepcopy(before))
            blocks = fam_obj.base_object.ades
            split_count = 0
            for binding in bindings:
                if not (0 <= binding.block_index < len(blocks)):
                    raise MappingEditError(
                        "the selected material block no longer exists")
                block = blocks[binding.block_index]
                selected = set(binding.selected_polys)
                selected_positions = [
                    index for index, entry in enumerate(block.atts)
                    if entry.poly_id in selected
                ]
                if not selected_positions:
                    raise MappingEditError(
                        "the selected segment is no longer in its material")
                new_template = rewrite_block_texture_template(
                    block, loaded_name, binding.binding_slot)
                if len(selected_positions) == len(block.atts):
                    getattr(block, binding.binding_slot).name = loaded_name
                    block.source_objt_bytes = new_template
                    continue
                if (block.class_id or "").casefold() != "amesh.class":
                    raise MappingEditError(
                        "only AMESH material blocks can be split by segment")
                atts_only = bool(
                    block.texture is not None
                    and block.texture.kind == "bmpanim"
                    and not block.olpl)
                if not atts_only and len(block.atts) != len(block.olpl):
                    raise MappingEditError(
                        "the shared material has ambiguous ATTS/OLPL counts")
                selected_set = set(selected_positions)
                clone = copy.deepcopy(block)
                clone.atts = [
                    copy.deepcopy(block.atts[index])
                    for index in selected_positions]
                clone.olpl = (
                    [] if atts_only else [
                        copy.deepcopy(block.olpl[index])
                        for index in selected_positions])
                block.atts = [
                    entry for index, entry in enumerate(block.atts)
                    if index not in selected_set]
                if not atts_only:
                    block.olpl = [
                        uvs for index, uvs in enumerate(block.olpl)
                        if index not in selected_set]
                getattr(clone, binding.binding_slot).name = loaded_name
                clone.source_objt_bytes = new_template
                blocks.append(clone)
                split_count += 1

            self._refresh_object_material_faces(fam_obj)
            self._selected_owner = owner
            self._selected_polys = set(group.selected_polys)
            self._selected_poly = min(
                self._selected_polys,
                default=self._selected_poly)
            self.viewport.refresh_family_materials()
            self._rebuild_workbench(self._family, owner)
            self._refresh_fx_elements()
            self.viewport.set_selected_owner(owner)
            self.viewport.set_selected_polygon(self._selected_poly)
            self.viewport.set_highlight_polys(self._selected_polys)
            self._on_geometry_edited(owner)
            after = self._capture_topology_state(owner)
            if after is None:
                raise MappingEditError(
                    "the segment-scoped texture edit could not be verified")
            self._record_edit_command({
                "kind": "topology",
                "owner": owner,
                "before": before,
                "after": after,
                "label": "segment texture assignment",
            })
            self._fill_polygon_inspector(self._selected_poly)
            self._sync_geometry_save_controls()
            self._update_editor_status()
            self._sync_editor_context()
            message = (
                f"{len(group.selected_polys)} selected polygon(s) changed"
                + (f"; {split_count} shared material block(s) isolated."
                   if split_count else "."))
            self.statusBar().showMessage(message, 9000)
            self._log(f"Load Texture: {message}")
            return True
        except Exception as exc:
            self._restore_topology_state(owner, before)
            if not had_topology_original:
                self._topology_original.pop(owner, None)
            if had_geom_original:
                self._geom_original[owner] = previous_geom_original
            else:
                self._geom_original.pop(owner, None)
            if had_dirty:
                self._geom_dirty[owner] = previous_dirty
            else:
                self._geom_dirty.pop(owner, None)
            self._edit_undo_stack[:] = previous_undo
            self._edit_redo_stack[:] = previous_redo
            self._notify(
                f"Load Texture failed and was rolled back: {exc}.", 9000)
            return False

    def _load_model_texture(self) -> None:
        if not self._require_editing("Load Texture"):
            return
        if self._mapping_index is None or self._workbench_obj is None:
            return
        requested = self._resolved_geometry_polys()
        if not requested:
            requested = {
                poly_id for poly_id in self._selected_polys
                if 0 <= poly_id < self._mapping_index.poly_count
            }
        if not requested and self._selected_poly is not None:
            requested = {self._selected_poly}
        selected_classification = classify_texture_assignment(
            self._mapping_index, requested)
        if selected_classification.skipped:
            reason_labels = {
                "unmapped": (
                    "has no ATTS material mapping in the BASE file"),
                "duplicate-mapped": (
                    "has more than one material mapping"),
                "invalid": "is not a valid polygon",
                "material has no texture": (
                    "uses a color-only material with no texture binding"),
                "material has no valid polygons": (
                    "belongs to an invalid material block"),
                "material overlaps duplicate mapping": (
                    "belongs to an ambiguous shared material"),
            }
            details = [
                f"Polygon #{poly_id}: "
                f"{reason_labels.get(reason, reason)}."
                for poly_id, reason in selected_classification.skipped[:12]
            ]
            remaining = len(selected_classification.skipped) - len(details)
            if remaining:
                details.append(f"...and {remaining} more polygon(s).")
            if selected_classification.groups:
                summary = (
                    "Some selected segments cannot have their texture "
                    "replaced and will be left unchanged.")
            else:
                summary = (
                    "The selected segment(s) cannot have their texture "
                    "replaced. The engine data contains no compatible texture "
                    "binding to change.")
            QMessageBox.warning(
                self, "Texture cannot be replaced",
                summary + "\n\n" + "\n".join(details)
                + "\n\nNo unsupported material data was modified.")
        if not selected_classification.groups:
            return
        group = self._choose_texture_binding_group(
            list(selected_classification.groups))
        if group is None:
            self._notify("Texture binding selection cancelled.", 3000)
            return
        names = (
            self._available_model_animations()
            if group.binding_kind == "bmpanim"
            else self._available_model_textures())
        if not names:
            QMessageBox.information(
                self, "No textures available",
                "No compatible decoded texture or animation binding is "
                "available in this family or SET.BAS.")
            return
        owner = self._workbench_obj.owner_path
        current = group.binding_name
        chosen = self._choose_model_texture(
            names, current, group.binding_kind)
        if not chosen:
            return
        if group.binding_kind == "bmpanim":
            loaded_name = next((
                name for name in self._family.animations
                if name.casefold() == chosen.casefold()
            ), None)
        else:
            loaded_name = self._ensure_model_texture_loaded(chosen)
        if loaded_name is None:
            QMessageBox.warning(
                self, "Texture load failed",
                f"{chosen} could not be decoded from the loaded sources.")
            return
        bindings = {
            (binding.block_index, binding.binding_slot): binding
            for binding in group.bindings
        }
        affected_polys = set(group.affected_polys)
        before = {
            (owner, binding.block_index, binding.binding_slot):
            getattr(binding.block, binding.binding_slot).name
            for binding in bindings.values()
        }
        changes = {
            key: loaded_name for key, old_name in before.items()
            if old_name.casefold() != loaded_name.casefold()
        }
        if not changes:
            self._notify(
                "Every selected compatible section already uses that resource.",
                         3500)
            return
        changed_bindings = [
            binding for binding in bindings.values()
            if (owner, binding.block_index, binding.binding_slot) in changes
        ]
        structural_needed = any(
            len(binding.selected_polys)
            < len(getattr(binding.block, "atts", ()))
            for binding in changed_bindings)
        if structural_needed:
            self._replace_selected_texture_structurally(
                owner, group, changed_bindings, loaded_name)
            return
        if not self._apply_texture_history(changes):
            self._notify(
                "Texture replacement failed; the binding was left unchanged.",
                7000)
            return
        after = {
            key: getattr(
                self._owner_to_obj[key[0]].base_object.ades[key[1]],
                key[2]).name
            for key in before
        }
        self._selected_polys = set(group.selected_polys)
        self.viewport.set_highlight_polys(self._selected_polys)
        self._record_edit_command({
            "kind": "texture",
            "before": before,
            "after": after,
            "label": "texture binding assignment",
        })
        self._fill_polygon_inspector(self._selected_poly)
        self._sync_geometry_save_controls()
        self._update_editor_status()
        self._sync_editor_context()
        changed_blocks = len(changes)
        changed_polygons = len(affected_polys)
        skipped = len(selected_classification.skipped)
        message = (
            f"{changed_blocks} material block(s) changed, "
            f"{changed_polygons} polygon(s) affected, "
            f"{skipped} incompatible polygon(s) skipped.")
        self.statusBar().showMessage(message, 9000)
        self._log(f"Load Texture: {message}")
