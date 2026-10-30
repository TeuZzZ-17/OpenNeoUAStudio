"""AssemblyWindow mixin: Polygon Mapping Workbench."""

from __future__ import annotations

import copy

from PySide6.QtCore import Qt
from PySide6.QtGui import (
    QColor,
    QImage,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QListWidgetItem,
    QMessageBox,
)

from studio.assets.catalog import classify_texture_assignment
from studio.assets.family import (
    AssetFamily,
)
from studio.assets.mapping import (
    build_material_block_clipboard,
    delete_material_block,
    eligible_blocks,
    MappingEditError,
    MappingIndex,
    paste_material_block,
)
from studio.ui.uv_editor import (
    UVLoop,
)
from studio.ui.widgets import (
    draw_uv_polygon as _draw_uv_polygon,
)
from studio.rendering.geometry import (
    FxElement,
)


class _PolygonMappingMixin:
    """Part of AssemblyWindow; `self` is always the AssemblyWindow."""

    # -- Polygon Mapping Workbench -------------------------------------------------

    def _set_mapping_diagnostics(self, enabled: bool) -> None:
        self.viewport.set_mapping_diagnostics(enabled)
        self._update_mapping_diagnostics_summary()
        if enabled and self._mapping_index is not None:
            unmapped = self._mapping_index.unmapped
            duplicates = self._mapping_index.duplicates
            self.statusBar().showMessage(
                f"Mapping diagnostics: {len(unmapped)} unmapped "
                f"{unmapped if unmapped else ''}, "
                f"{len(duplicates)} duplicate-mapped, "
                f"{len(self._mapping_index.invalid)} invalid"
            )

    def _update_mapping_diagnostics_summary(self) -> None:
        """Refresh the Mapping Repair summary from the shared index only."""

        label = getattr(self, "mapping_diagnostics_label", None)
        if label is None:
            return
        index = self._mapping_index
        if index is None:
            label.setText("No editable skeleton selected.")
            return
        label.setText(
            f"Unmapped: {len(index.unmapped)}   |   "
            f"Duplicate: {len(index.duplicates)}   |   "
            f"Invalid: {len(index.invalid)}"
        )

    def _sync_poly_id_control(self) -> None:
        """Keep the compact poly selector aligned with the active owner."""

        spin = getattr(self, "poly_id_spin", None)
        if spin is None:
            return
        count = (self._mapping_index.poly_count
                 if self._mapping_index is not None else 0)
        enabled = count > 0
        spin.blockSignals(True)
        spin.setRange(0, max(0, count - 1))
        if self._selected_poly is not None and \
                0 <= self._selected_poly < count:
            spin.setValue(self._selected_poly)
        elif enabled:
            spin.setValue(0)
        spin.blockSignals(False)
        spin.setEnabled(enabled)
        previous = getattr(self, "poly_id_previous_button", None)
        following = getattr(self, "poly_id_next_button", None)
        if previous is not None:
            previous.setEnabled(enabled and spin.value() > 0)
        if following is not None:
            following.setEnabled(enabled and spin.value() + 1 < count)

    def _on_poly_id_changed(self, poly_id: int) -> None:
        if self._mapping_index is None \
                or not (0 <= poly_id < self._mapping_index.poly_count):
            return
        self._on_polygon_picked(poly_id, False)

    def _step_poly_id(self, direction: int) -> None:
        if self._mapping_index is None:
            return
        target = max(
            0, min(self._mapping_index.poly_count - 1,
                   self.poly_id_spin.value() + int(direction)))
        self.poly_id_spin.setValue(target)

    def _rebuild_workbench(self, family: AssetFamily,
                           owner: str | None = None) -> None:
        self._workbench_obj = None
        if owner is not None:
            candidate = self._owner_to_obj.get(owner)
            if candidate is not None and candidate.skeleton is not None:
                self._workbench_obj = candidate
        if self._workbench_obj is None:
            self._workbench_obj = next(
                (o for o in family.all_objects() if o.skeleton is not None),
                None,
            )
        self._mapping_index = (MappingIndex(self._workbench_obj)
                               if self._workbench_obj else None)
        self._update_mapping_diagnostics_summary()
        self._repair_plan = None
        self.repair_preview.clear()
        self._fill_blocks_list(family)
        self.repair_target_combo.clear()
        if self._workbench_obj is not None:
            for index, block in eligible_blocks(self._workbench_obj):
                tex = block.texture.name if block.texture else f"block {index}"
                self.repair_target_combo.addItem(
                    f"#{index} {tex} ({len(block.atts)} entries)", index
                )
        if self._selected_poly is not None and self._mapping_index is not None \
                and self._selected_poly < self._mapping_index.poly_count:
            self.viewport.set_selected_polygon(self._selected_poly)
            self._fill_polygon_inspector(self._selected_poly)
        else:
            self._selected_poly = None
            self._fill_polygon_inspector(None)
        self._sync_poly_id_control()
        self._update_repair_buttons()

    def _fill_blocks_list(self, family: AssetFamily) -> None:
        self.blocks_list.clear()
        if self._workbench_obj is None:
            return
        for index, block in enumerate(self._workbench_obj.base_object.ades):
            tex = block.texture.name if block.texture else block.class_id
            ids = [e.poly_id for e in block.atts]
            rng = f"{min(ids)}..{max(ids)}" if ids else "none"
            ref = (family.texture_refs.get(tex)
                   or family.animation_refs.get(tex))
            source = ref.source if ref and ref.source else "?"
            item = QListWidgetItem(
                f"#{index}  {tex}  —  {len(ids)} polygons")
            item.setData(Qt.ItemDataRole.UserRole, index)
            item.setToolTip(
                f"Source: {source}\npolyID range: {rng}\n"
                f"{block.describe_polflags()}")
            self.blocks_list.addItem(item)

    def _on_block_selected(self, row: int) -> None:
        if row < 0 or self._workbench_obj is None:
            self.viewport.set_highlight_polys(set())
            self._update_repair_buttons()
            return
        item = self.blocks_list.item(row)
        index = item.data(Qt.ItemDataRole.UserRole)
        block = self._workbench_obj.base_object.ades[index]
        ids = {e.poly_id for e in block.atts}
        self.viewport.set_highlight_polys(ids)
        tex = block.texture.name if block.texture else block.class_id
        self.statusBar().showMessage(
            f"Block #{index} {tex}: {len(ids)} polygon(s) highlighted"
        )
        self._draw_block_uv_islands(index)
        self._update_repair_buttons()

    def _current_material_block_index(self) -> int | None:
        item = self.blocks_list.currentItem()
        if item is None:
            return None
        value = item.data(Qt.ItemDataRole.UserRole)
        return int(value) if value is not None else None

    @staticmethod
    def _capture_family_resource_state(family: AssetFamily) -> dict:
        return {
            "textures": dict(family.textures),
            "texture_refs": dict(family.texture_refs),
            "animations": dict(family.animations),
            "animation_refs": dict(family.animation_refs),
        }

    @staticmethod
    def _restore_family_resource_state(
            family: AssetFamily, state: dict) -> None:
        for name in (
                "textures", "texture_refs", "animations", "animation_refs"):
            mapping = getattr(family, name)
            mapping.clear()
            mapping.update(state[name])

    def _material_structure_reason(self) -> str:
        if not self._editing_allowed():
            return "Edit Mode is required"
        if self.viewport.paste_preview_active:
            return "finish or cancel the geometry Paste Preview first"
        owner = self._selected_owner
        if self._workbench_obj is None or self._family is None \
                or owner != self._workbench_obj.owner_path:
            return "select an editable model owner first"
        if self._pending_repairs:
            return "export or revert pending Mapping Repairs first"
        if any(key[0] == owner for key in self._texture_original):
            return "save or reset pending material previews first"
        return ""

    def _apply_material_structure(self, label: str, operation):
        reason = self._material_structure_reason()
        if reason:
            raise MappingEditError(reason)
        owner = self._selected_owner
        family = self._family
        fam_obj = self._workbench_obj
        before = self._capture_topology_state(owner)
        if before is None:
            raise MappingEditError("the topology snapshot could not be created")
        resources_before = self._capture_family_resource_state(family)
        had_topology_original = owner in self._topology_original
        previous_geom_original = self._geom_original.get(owner)
        previous_dirty = self._geom_dirty.get(owner)
        previous_undo = list(self._edit_undo_stack)
        previous_redo = list(self._edit_redo_stack)
        try:
            self._topology_original.setdefault(owner, copy.deepcopy(before))
            self._remember_geometry_original(owner)
            result = operation()
            self._remap_uv_tracking(owner, before)
            self._refresh_object_material_faces(fam_obj)
            self._selected_owner = owner
            self.viewport.refresh_family_materials()
            self._rebuild_workbench(family, owner)
            self._refresh_fx_elements()
            self.viewport.set_selected_owner(owner)
            self.viewport.set_selected_polygon(self._selected_poly)
            self.viewport.set_highlight_polys(self._selected_polys)
            self._on_geometry_edited(owner)
            after = self._capture_topology_state(owner)
            if after is None:
                raise MappingEditError(
                    "the committed ADES topology could not be verified")
            resources_after = self._capture_family_resource_state(family)
            self._record_edit_command({
                "kind": "material_topology",
                "owner": owner,
                "before": before,
                "after": after,
                "resource_before": resources_before,
                "resource_after": resources_after,
                "label": label,
            })
            block_index = getattr(result, "block_index", None)
            if block_index is not None:
                for row in range(self.blocks_list.count()):
                    item = self.blocks_list.item(row)
                    if item.data(Qt.ItemDataRole.UserRole) == block_index:
                        self.blocks_list.setCurrentRow(row)
                        break
            self._fill_polygon_inspector(self._selected_poly)
            self._sync_geometry_save_controls()
            self._sync_editor_context()
            return result
        except Exception as exc:
            self._restore_family_resource_state(family, resources_before)
            self._restore_topology_state(owner, before)
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
            if isinstance(exc, MappingEditError):
                raise
            raise MappingEditError(
                f"{label} failed and was rolled back: {exc}") from exc

    def _copy_material_block(self) -> None:
        block_index = self._current_material_block_index()
        if self._family is None or self._workbench_obj is None \
                or block_index is None:
            self._notify("Copy Material: select a material block first.", 5000)
            return
        try:
            self._material_clipboard = build_material_block_clipboard(
                self._family, self._workbench_obj, block_index)
        except MappingEditError as exc:
            QMessageBox.warning(self, "Cannot copy material", str(exc))
            return
        block = self._workbench_obj.base_object.ades[block_index]
        texture = block.texture.name if block.texture else block.class_id
        self._notify(
            f"Copied material #{block_index} {texture} with "
            f"{len(self._material_clipboard.resources)} dependency snapshot(s).",
            7000)
        self._update_repair_buttons()

    def _paste_material_block(self) -> None:
        if not self._require_editing("Paste Material"):
            return
        clipboard = self._material_clipboard
        if clipboard is None or self._family is None \
                or self._workbench_obj is None:
            self._notify("Paste Material: the material clipboard is empty.", 5000)
            return
        target_poly_id = None
        if clipboard.class_id == "area.class":
            selected = set(self._selected_polys)
            if not selected and self._selected_poly is not None:
                selected = {self._selected_poly}
            if len(selected) != 1:
                self._notify(
                    "AREA paste refused: select exactly one unmapped target "
                    "polygon.", 7000)
                return
            target_poly_id = next(iter(selected))
        try:
            result = self._apply_material_structure(
                "paste material block",
                lambda: paste_material_block(
                    self._family, self._workbench_obj, clipboard,
                    target_poly_id=target_poly_id))
        except MappingEditError as exc:
            QMessageBox.warning(self, "Cannot paste material", str(exc))
            return
        assigned = (
            f" and mapped it to polygon #{result.assigned_poly_id}"
            if result.assigned_poly_id is not None else
            "; AMESH polygon mappings remain intentionally empty")
        imported = (
            f" Imported {len(result.imported_resources)} missing "
            "dependency snapshot(s)."
            if result.imported_resources else "")
        self._notify(
            f"Pasted material block #{result.block_index}{assigned}."
            f"{imported} Undo is available.", 9000)

    def _add_material_slot(self) -> None:
        if not self._require_editing("Add Compatible Material Slot"):
            return
        block_index = self._current_material_block_index()
        if self._family is None or self._workbench_obj is None \
                or block_index is None:
            self._notify("Add Slot: select an AMESH template first.", 5000)
            return
        try:
            clipboard = build_material_block_clipboard(
                self._family, self._workbench_obj, block_index)
            if clipboard.class_id != "amesh.class":
                raise MappingEditError(
                    "AREA is a one-polygon typed block; use Copy/Paste AREA "
                    "with an explicit unmapped target polygon")
            result = self._apply_material_structure(
                "add compatible material slot",
                lambda: paste_material_block(
                    self._family, self._workbench_obj, clipboard))
        except MappingEditError as exc:
            QMessageBox.warning(self, "Cannot add material slot", str(exc))
            return
        self._notify(
            f"Added empty compatible AMESH slot #{result.block_index}. "
            "Use Mapping Repair to assign polygons; Undo is available.", 9000)

    def _delete_material_block(self) -> None:
        if not self._require_editing("Delete Material"):
            return
        block_index = self._current_material_block_index()
        if self._workbench_obj is None or block_index is None:
            self._notify("Delete Material: select a block first.", 5000)
            return
        block = self._workbench_obj.base_object.ades[block_index]
        mapped = len(block.atts)
        answer = QMessageBox.warning(
            self, "Delete material block?",
            f"Delete ADES block #{block_index} ({block.class_id or 'unknown'})?\n\n"
            f"{mapped} polygon mapping(s) will be removed; POL2 geometry is "
            "kept and those polygons become unmapped. SET.BAS and source "
            "files are not modified until an explicit export.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self._apply_material_structure(
                "delete material block",
                lambda: delete_material_block(
                    self._workbench_obj, block_index))
        except MappingEditError as exc:
            QMessageBox.warning(self, "Cannot delete material", str(exc))
            return
        self._notify(
            f"Deleted material block #{block_index}; {mapped} polygon(s) "
            "are now unmapped. Undo is available.", 9000)

    def _on_polygon_structure_picked(
            self, owner: str, poly_id: int, block_index: int) -> None:
        self._picked_structure = (owner, poly_id, block_index)

    def _on_polygon_picked(self, poly_id: int,
                           additive: bool = False) -> None:
        was_selected = poly_id in self._selected_polys
        mirror_active = self._mirror_select_active()
        if mirror_active:
            if additive and was_selected:
                self._mirror_select_source_polys.difference_update(
                    self._mirrored_polygon_selection({poly_id}))
            elif additive:
                self._mirror_select_source_polys.add(poly_id)
            else:
                self._mirror_select_source_polys = {poly_id}
            self._mirror_select_source_polys = \
                self._complete_fx_polygon_selection(
                    self._mirror_select_source_polys)
            self._selected_polys = self._mirrored_polygon_selection(
                self._mirror_select_source_polys)
            if self._editing_allowed():
                mirrored_polys = set(self._selected_polys)
                self._detach_mirrored_shared_fx(self._selected_polys)
                self._mirror_select_source_polys = \
                    self._complete_fx_polygon_selection(
                        self._mirror_select_source_polys)
                self._selected_polys = self._complete_fx_polygon_selection(
                    mirrored_polys)
        else:
            self._mirror_select_source_polys.clear()
            picked_polys = self._complete_fx_polygon_selection({poly_id})
            if additive:
                if was_selected:
                    self._selected_polys.difference_update(picked_polys)
                else:
                    self._selected_polys.update(picked_polys)
            else:
                self._selected_polys = picked_polys
        self._selected_poly = (
            poly_id if poly_id in self._selected_polys else
            (min(self._selected_polys, default=None)))
        self.viewport.set_selected_polygon(self._selected_poly)
        self.viewport.set_highlight_polys(self._selected_polys)
        self._sync_poly_id_control()
        self._repair_plan = None
        self.repair_preview.clear()
        if self._mapping_index is not None:
            self._fill_polygon_inspector(self._selected_poly)
        self._update_repair_buttons()
        if self._mapping_index is not None:
            status = (self._mapping_index.status(self._selected_poly)
                      if self._selected_poly is not None else "none")
            self.statusBar().showMessage(
                f"Selected {len(self._selected_polys)} polygon(s); "
                f"current #{self._selected_poly}: {status}"
            )
        picked = self._picked_structure
        self._picked_structure = None
        block_index = (
            picked[2] if picked is not None
            and picked[0] == self._selected_owner
            and picked[1] == poly_id else None)
        element = self._fx_element_for_polygon(poly_id, block_index)
        if poly_id not in self._selected_polys:
            element = self._fx_element_for_polygon(self._selected_poly)
        self.fx_combo.blockSignals(True)
        self.fx_combo.setCurrentIndex(
            self._fx_combo_index(element.identity) if element else 0)
        self.fx_combo.blockSignals(False)
        if element is not None and not additive and not mirror_active:
            if self._editing_allowed() \
                    and element.shared_state == "shared":
                element = self._detach_fx_for_editing(element) or element
        self._sync_edit_action_states()
        if self._active_editor_panel() is not self._model_editor_panel \
                or not self.edit_toggle_action.isChecked() \
                or not self.viewport.is_edit_mode:
            return
        target = self._selected_polygon_vertices()
        if target is not None:
            owner, vertices = target
            self.viewport.enter_edit_mode_with_vertices(
                owner, vertices, pick_polygons=True)
            if mirror_active:
                self._set_mirror_transform_source(
                    self._polygon_vertices(
                        self.viewport.edit_session.model,
                        self._mirror_select_source_polys))
            self.viewport.configure_edit_interaction(
                selected_only=False, pick_polygons=True)
        elif mirror_active and self.viewport.edit_session is not None:
            self.viewport.edit_session.select_none()
        elif not self._selected_polys and self.viewport.edit_session is not None:
            self.viewport.edit_session.select_none()
        self._sync_edit_action_states()

    def _fx_element_for_polygon(
            self, poly_id: int | None,
            block_index: int | None = None) -> FxElement | None:
        if poly_id is None:
            return None
        def matches(element: FxElement) -> bool:
            return any(
                candidate_poly == poly_id
                and (block_index is None or candidate_block == block_index)
                for candidate_poly, candidate_block in zip(
                    element.poly_ids, element.block_indices))

        selected = self._selected_fx_element()
        if selected is not None and matches(selected):
            return selected
        return next((element for element in self._fx_elements
                     if element.owner_path == self._selected_owner
                     and matches(element)), None)

    def _fx_face_records(self, element: FxElement):
        obj = self._owner_to_obj.get(element.owner_path)
        if obj is None:
            return []
        records = []
        for index, poly_id in enumerate(element.poly_ids):
            if index >= len(element.block_indices) \
                    or index >= len(element.atts_indices):
                continue
            block_index = element.block_indices[index]
            atts_index = element.atts_indices[index]
            if not (0 <= block_index < len(obj.base_object.ades)):
                continue
            block = obj.base_object.ades[block_index]
            material = (
                element.material_names[index]
                if index < len(element.material_names)
                else block.texture.name if block.texture else "-")
            records.append((poly_id, block_index, atts_index, block, material))
        return records

    def _update_fx_uv_editor(self, element: FxElement) -> None:
        self._uv_ctx = None
        self._uv_contexts = {}
        self._uv_phase_links = {}
        obj = self._owner_to_obj.get(element.owner_path)
        model = getattr(obj, "skeleton", None)
        loops, keys = [], set()
        phase_shapes = {}
        image = None
        for poly_id, block_index, atts_index, block, material in self._fx_face_records(element):
            if model is None or not 0 <= poly_id < len(model.polygons):
                continue
            vertex_count = len(model.polygons[poly_id])
            animation_entry = self._animation_for_name(material) if block.texture and block.texture.kind == "bmpanim" else None
            if animation_entry is not None:
                name, animation = animation_entry
                # UV groups are authored phases, not the current playback frame.
                # Repeated frames share one group and must edit it only once.
                for frame in animation.frames:
                    group = frame.texcoords_id
                    if not 0 <= group < len(animation.texcoord_groups):
                        continue
                    key = ("@VANM", name, group)
                    if key in keys:
                        continue
                    keys.add(key)
                    uvs = list(animation.texcoord_groups[group])
                    editable = self._editing_allowed() and len(uvs) == vertex_count and self._animation_uv_writable(name, group)
                    bitmap = animation.bitmap_names[frame.frame_id] if 0 <= frame.frame_id < len(animation.bitmap_names) else ""
                    shape = (name, bitmap.casefold(), tuple(sorted(uvs)))
                    if editable:
                        self._uv_contexts[key] = (obj, None, name, group, poly_id)
                    # Rotated windings of the same footprint are animation
                    # samples of one phase. Keep their corner correspondence.
                    if shape in phase_shapes and len(set(uvs)) == len(uvs):
                        primary, primary_uvs = phase_shapes[shape]
                        self._uv_phase_links.setdefault(primary, []).append(
                            (key, tuple(primary_uvs.index(uv) for uv in uvs)))
                        continue
                    phase_shapes[shape] = (key, uvs)
                    if image is None and 0 <= frame.frame_id < len(animation.bitmap_names):
                        image = self._texture_qimage(animation.bitmap_names[frame.frame_id])
                    loops.append(UVLoop(key, poly_id, uvs, editable))
                    if editable:
                        self._uv_contexts[key] = (obj, None, name, group, poly_id)
                continue
            key = (element.owner_path, block_index, atts_index)
            if key in keys or not 0 <= atts_index < len(block.olpl):
                continue
            keys.add(key)
            uvs = list(block.olpl[atts_index])
            editable = bool(self._editing_allowed() and element.editable
                            and len(uvs) == vertex_count
                            and (block.class_id or "").casefold() in ("amesh.class", "area.class"))
            if image is None:
                image = self._texture_qimage(material)
            loops.append(UVLoop(key, poly_id, uvs, editable))
            if editable:
                context = (obj, block, block_index, atts_index, poly_id)
                self._uv_contexts[key] = context
                if self._uv_ctx is None:
                    self._uv_ctx = context
        self.uv_editor.set_loops(
            image, loops,
            f"{len(loops)} UV phases shown together by default. Use the phase "
            "selector to isolate one phase; drag to move, wheel to zoom, "
            "middle drag to pan. Yellow and cyan identify the phases.")
        self._sync_uv_add_vertex_button()

    def _fill_fx_inspector(self, element: FxElement, poly_id: int) -> None:
        obj = self._owner_to_obj.get(element.owner_path)
        model = getattr(obj, "skeleton", None)
        polygon = model.polygons[poly_id] if model is not None else ()
        lines = [
            f"polyID {poly_id} | {len(polygon)} vertices | POO2 {list(polygon)}",
            f"FX element: {element.fx_name} | source: {element.source_kind}",
            f"FX polyIDs: {list(element.poly_ids)}",
            f"UV source: {element.uv_source} | "
            + (
                "editable VANM group"
                if element.source_kind == "VANM"
                else
                "editable direct OLPL"
                if element.editable and element.source_kind == "direct"
                and element.uv_source == "OLPL"
                else "UV coordinates unavailable"),
        ]
        labels = []
        for face_poly, block_index, atts_index, block, material in \
                self._fx_face_records(element):
            labels.append(material)
            lines.append(
                f"poly #{face_poly} | block #{block_index} "
                f"({block.class_id}) | texture: {material} | "
                f"ATTS #{atts_index}")
            if element.source_kind == "VANM" \
                    or (block.texture is not None
                        and block.texture.kind == "bmpanim"):
                frame = self.viewport.current_animation_frame_data(
                    element.owner_path, block_index)
                if frame is not None:
                    lines.append(f"current VANM: {frame[2]}")
        label = ", ".join(dict.fromkeys(value for value in labels if value))
        self.model_texture_label.setText(
            "Current FX Texture: " + (label or "-"))
        self.load_texture_button.setToolTip(
            "Replace the selected FX segment texture or VANM binding without "
            "changing its geometry, winding or animation data.")
        self._set_polygon_object_info(lines)

    def _fill_polygon_inspector(self, poly_id: int | None) -> None:
        self._update_uv_editor(poly_id)
        self.poly_uv_label.clear()
        self.poly_uv_label.setText("Select a polygon in the viewport.")
        self.model_texture_label.setText("Current FX Texture: -")
        self.load_texture_button.setEnabled(False)
        if poly_id is None or self._workbench_obj is None \
                or self._workbench_obj.skeleton is None:
            self._set_polygon_object_info([])
            return
        obj = self._workbench_obj
        skeleton = obj.skeleton
        if not (0 <= poly_id < len(skeleton.polygons)):
            self._set_polygon_object_info(
                [f"polygon #{poly_id}: out of range"])
            return

        element = self._fx_element_for_polygon(poly_id)
        if element is not None:
            self._fill_fx_inspector(element, poly_id)
            return

        polygon = skeleton.polygons[poly_id]
        lines = [
            f"polyID {poly_id} | {len(polygon)} vertices | "
            f"POO2 {list(polygon)}"]
        requested = self._resolved_geometry_polys() or {poly_id}
        texture_selection = classify_texture_assignment(
            self._mapping_index, requested)
        self.load_texture_button.setEnabled(
            self._editing_allowed() and bool(
                texture_selection.groups or texture_selection.skipped))
        self.load_texture_button.setToolTip(
            "Replace the compatible ILBM or VANM binding only on the selected "
            "segment(s). Clicking the button explains any selected segment "
            "that has no replaceable texture binding.")

        status = self._mapping_index.status(poly_id)
        lines.append(f"mapping: {status.upper()}")
        if status == "unmapped":
            lines.append("No ATTS material: polygon is invisible in-game.")
            self._set_polygon_object_info(lines)
            return

        texture_names = []
        for ref in self._mapping_index.refs.get(poly_id, []):
            block = ref.block
            tex = block.texture.name if block.texture else "-"
            texture_names.append(tex)
            lines.append(
                f"block #{ref.block_index} | texture: {tex} | "
                f"ATTS #{ref.atts_index}")
            uvs = (block.olpl[ref.atts_index]
                   if ref.atts_index < len(block.olpl) else [])
            self._draw_uv_overlay(tex, uvs, poly_id)
        self.model_texture_label.setText(
            "Current FX Texture: " + ", ".join(dict.fromkeys(texture_names)))
        self._set_polygon_object_info(lines)

    def _texture_qimage(self, tex_name: str) -> QImage | None:
        family = self._family
        if family is None:
            return None
        loaded = next(
            (name for name in family.textures
             if name.casefold() == str(tex_name).casefold()), None)
        img = family.textures.get(loaded) if loaded is not None else None
        if img is None:
            anm = family.animations.get(tex_name)
            if anm and anm.bitmap_names:
                img = family.textures.get(anm.bitmap_names[0])
        if img is None or not img.has_body:
            return None
        external_palette = (
            family.external_palette if not img.palette else None)
        cache_key = (
            id(family),
            id(img),
            id(external_palette),
            str(tex_name).casefold(),
        )
        cached = self._texture_qimage_cache.get(cache_key)
        if cached is not None:
            return QImage(cached)
        rgba = img.to_rgba_bytes(
            external_palette, "chroma"
        )
        cached = QImage(
            rgba, img.width, img.height, img.width * 4,
            QImage.Format.Format_RGBA8888).copy()
        self._texture_qimage_cache[cache_key] = cached
        return QImage(cached)

    def _draw_uv_overlay(self, tex_name: str, uvs: list, poly_id: int,
                         extra_groups: list | None = None) -> None:
        qimage = self._texture_qimage(tex_name)
        available = min(
            max(0, self.poly_uv_label.width() - 16),
            max(0, self.poly_uv_label.height() - 16),
        )
        size = max(280, min(520, available if available > 0 else 360))
        pix = QPixmap(size, size)
        pix.fill(QColor(40, 42, 48))
        painter = QPainter(pix)
        if qimage is not None:
            painter.drawImage(
                0, 0,
                qimage.scaled(size, size,
                              Qt.AspectRatioMode.IgnoreAspectRatio,
                              Qt.TransformationMode.FastTransformation),
            )
        for group in (extra_groups or []):
            painter.setPen(QPen(QColor(90, 230, 255, 170), 1.0))
            _draw_uv_polygon(painter, group, size)
        if uvs:
            painter.setPen(QPen(QColor(255, 255, 90), 1.6))
            _draw_uv_polygon(painter, uvs, size)
            painter.setPen(QColor(255, 255, 255))
            for vi, (u, v) in enumerate(uvs):
                painter.drawText(int(u / 256 * size) + 3,
                                 int(v / 256 * size) - 3, str(vi))
        painter.end()
        self.poly_uv_label.setPixmap(pix)

    def _draw_block_uv_islands(self, block_index: int) -> None:
        if self._workbench_obj is None:
            return
        block = self._workbench_obj.base_object.ades[block_index]
        tex = block.texture.name if block.texture else ""
        if not tex or not block.olpl:
            return
        self._draw_uv_overlay(tex, [], -1, extra_groups=block.olpl)
