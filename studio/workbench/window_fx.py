"""AssemblyWindow mixin: FX Elements: add, edit and remove FX element geometry."""

from __future__ import annotations

import copy
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import (
    QImage,
)
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QInputDialog,
    QMenu,
    QMessageBox,
)

from studio.assets.family import (
    load_asset_family,
)
from studio.assets.mapping import (
    MappingEditError,
    MappingIndex,
)
from studio.assets.visproto import (
    DUMMY_BASE_NAME,
    reconstruct_embedded_vps,
    VPEmbeddedError,
)
from studio.formats.setbas import read_setbas
from studio.ui.widgets import (
    choose_bas_archive,
    crop_uv_region,
)
from studio.rendering.geometry import (
    build_fx_element_clipboard,
    detach_shared_fx_vertices,
    detect_fx_elements,
    FxElement,
    FxElementCapabilities,
    GeometryClipboardError,
    plan_delete_geometry,
    prepare_fx_clipboard,
    stage_fx_clipboard,
)
from studio.workbench.common import (
    AddFxElementDialog,
)


class _FxElementsMixin:
    """Part of AssemblyWindow; `self` is always the AssemblyWindow."""

    # -- FX Elements ---------------------------------------------------------------

    def _selected_fx_element(self) -> FxElement | None:
        value = self.fx_combo.currentData()
        return value if isinstance(value, FxElement) else None

    def _active_fx_selection(self) -> FxElement | None:
        """Resolve an explicit combo/polygon/complete-vertex FX selection."""

        owner = self.viewport.edit_owner
        resolved = self._resolved_geometry_polys()
        selected = self._selected_fx_element()
        if selected is not None and selected.owner_path == owner \
                and resolved.intersection(selected.poly_ids):
            return selected
        return next(
            (element for element in self._fx_elements
             if element.owner_path == owner
             and resolved.intersection(element.poly_ids)),
            None)

    def _active_fx_elements(self) -> tuple[FxElement, ...]:
        """Resolve an exact selection made only of complete FX elements."""

        owner = self.viewport.edit_owner
        resolved = self._resolved_geometry_polys()
        if not owner or not resolved:
            return ()
        touched = tuple(
            element for element in self._fx_elements
            if element.owner_path == owner
            and resolved.intersection(element.poly_ids))
        if not touched or any(
                not set(element.poly_ids).issubset(resolved)
                for element in touched):
            return ()
        covered = {
            poly_id for element in touched for poly_id in element.poly_ids}
        return touched if covered == resolved else ()

    def _mixed_fx_selection_reason(
            self, element: FxElement | None) -> str:
        if element is None:
            return ""
        resolved = self._resolved_geometry_polys()
        if resolved and not self._active_fx_elements():
            return (
                "FX elements cannot be combined with other polygons in one "
                "Copy, Move or Delete operation")
        return ""

    def _refresh_fx_elements(self) -> None:
        self._fx_preview_cache.clear()
        previous = self._selected_fx_element()
        previous_identity = previous.identity if previous is not None else None
        self._fx_elements = []
        self.viewport.set_highlight_polys(set())

        family = self._family
        obj = self._owner_to_obj.get(self._selected_owner) \
            if self._selected_owner else None
        if family is not None and obj is not None:
            self._fx_elements = detect_fx_elements(obj, family.animations)

        self.fx_combo.blockSignals(True)
        self.fx_combo.clear()
        self.fx_combo.addItem("No FX element selected", None)
        selected_index = 0
        for element in self._fx_elements:
            capabilities = self._fx_capabilities(element)
            delete_reason = capabilities.delete_reason
            if element.editable and capabilities.can_delete:
                status = (
                    "Bilateral | Editable | Delete supported"
                    if element.bilateral else
                    "Editable | Delete supported")
            elif element.editable:
                status = (
                    "Bilateral | Move/Scale only"
                    if element.bilateral else "Move/Scale only")
            else:
                status = (
                    "Invalid"
                    if element.shared_state == "invalid" else
                    "Shared vertices | Auto-detach on edit")
            label = (
                f"{element.fx_name} - polyIDs "
                f"{','.join(str(poly_id) for poly_id in element.poly_ids)} "
                f"({status})"
            )
            label = self._fx_aliases.get((element.owner_path, element.poly_ids), label)
            self.fx_combo.addItem(label, element)
            index = self.fx_combo.count() - 1
            details = [
                f"owner: {element.owner_path}",
                f"materials: {list(element.material_names)}",
                f"ATTS entries: {list(element.atts_indices)}",
                f"POL2: {list(element.poly_ids)}",
                f"POO2 indices: {list(element.vertex_indices)}",
            ]
            if element.shared_vertices:
                details.append(
                    f"shared POO2: {list(element.shared_vertices)}; "
                    f"other POL2: {list(element.shared_with_polys)}"
                )
                details.append(
                    "Editing duplicates only these shared vertices first, so "
                    "the other geometry is not deformed."
                )
            if delete_reason:
                details.append(f"Delete unavailable: {delete_reason}.")
            else:
                classes = sorted({
                    self._owner_to_obj[element.owner_path]
                    .base_object.ades[index].class_id
                    for index in element.block_indices
                })
                details.append(
                    "Typed POL2 handlers: " + ", ".join(classes))
            details.append(
                "Capabilities: "
                f"move={capabilities.can_move}, "
                f"scale={capabilities.can_scale}, "
                f"copy={capabilities.can_copy}, "
                f"paste={capabilities.can_paste}, "
                f"delete={capabilities.can_delete}, "
                f"add similar={capabilities.can_add_similar}, "
                f"save={capabilities.can_save}, "
                f"overwrite={capabilities.can_overwrite}, "
                f"save as={capabilities.can_save_as}")
            details.extend(element.warnings)
            self.fx_combo.setItemData(
                index, "\n".join(details), Qt.ItemDataRole.ToolTipRole)
            if element.identity == previous_identity:
                selected_index = index
        self.fx_combo.setCurrentIndex(selected_index)
        self.fx_combo.setEnabled(bool(self._fx_elements))
        self.fx_combo.blockSignals(False)
        edit_owner = self.viewport.edit_owner or self._selected_owner
        read_only_vertices = {
            vertex
            for element in self._fx_elements
            if element.owner_path == edit_owner and not element.editable
            for vertex in element.vertex_indices
        }
        self.viewport.set_edit_read_only_vertices(read_only_vertices)
        # The FX list drives the FX commands, so realign labels and enabled
        # states every time the list is rebuilt.
        self._sync_fx_combo_label()
        self._sync_edit_action_states()

    def _sync_fx_combo_label(self) -> None:
        """Name the neutral FX entry after the real multi-element selection."""

        label = ("Multiple FX selected"
                 if len(self._active_fx_elements()) > 1
                 else "No FX element selected")
        self.fx_combo.setItemText(0, label)

    def _fx_combo_index(self, identity) -> int:
        for index in range(1, self.fx_combo.count()):
            candidate = self.fx_combo.itemData(index)
            if isinstance(candidate, FxElement) \
                    and candidate.identity == identity:
                return index
        return 0

    def _on_fx_selected(self, _index: int) -> None:
        element = self._selected_fx_element()
        if not isinstance(element, FxElement):
            self._clear_model_selection()
            return
        identity = element.identity
        self._select_highlight_fx()
        if self._editing_allowed() and self._mirror_select_active():
            source_polys = set(self._mirror_select_source_polys)
            mirrored_polys = set(self._selected_polys)
            self._detach_mirrored_shared_fx(self._selected_polys)
            index = self._fx_combo_index(identity)
            self.fx_combo.blockSignals(True)
            self.fx_combo.setCurrentIndex(index)
            self.fx_combo.blockSignals(False)
            element = self._selected_fx_element() or element
            self._mirror_select_source_polys = \
                self._complete_fx_polygon_selection(source_polys)
            self._set_mirror_polygon_selection(
                element.owner_path, self._workbench_obj,
                self._complete_fx_polygon_selection(mirrored_polys),
                self._mirror_select_source_polys)
        elif self._editing_allowed() and element.shared_state == "shared":
            element = self._detach_fx_for_editing(element) or element
        if element.editable and self._editing_allowed():
            self._edit_selected_fx()
        self._sync_edit_action_states()

    def _detach_fx_for_editing(
            self, element: FxElement) -> FxElement | None:
        """Make one shared FX independently editable as one undoable edit."""

        owner = element.owner_path
        fam_obj = self._owner_to_obj.get(owner)
        if fam_obj is None or fam_obj is not self._workbench_obj:
            return None
        reason = self._geometry_writer_reason(owner, fam_obj)
        if reason:
            self._notify(f"FX edit unavailable: {reason}.", 8000)
            return None
        before = self._capture_topology_state(owner)
        if before is None:
            return None
        had_topology_original = owner in self._topology_original
        had_geom_original = owner in self._geom_original
        previous_geom_original = copy.deepcopy(
            self._geom_original.get(owner))
        had_dirty = owner in self._geom_dirty
        previous_dirty = self._geom_dirty.get(owner)
        previous_undo = list(self._edit_undo_stack)
        previous_redo = list(self._edit_redo_stack)
        try:
            detached = detach_shared_fx_vertices(fam_obj, element)
            if not detached:
                return None
            self._topology_original.setdefault(owner, copy.deepcopy(before))
            self._refresh_object_material_faces(fam_obj)
            self.viewport.refresh_family_materials()
            self._rebuild_workbench(self._family, owner)
            self._refresh_fx_elements()
            updated = next((
                candidate for candidate in self._fx_elements
                if candidate.identity == element.identity), None)
            if updated is None or not updated.editable:
                raise GeometryClipboardError(
                    "the detached FX did not become independently editable")
            self.fx_combo.blockSignals(True)
            self.fx_combo.setCurrentIndex(
                self._fx_combo_index(updated.identity))
            self.fx_combo.blockSignals(False)
            self._on_geometry_edited(owner)
            after = self._capture_topology_state(owner)
            if after is None:
                raise GeometryClipboardError(
                    "the detached FX could not be verified")
            self._record_edit_command({
                "kind": "topology",
                "owner": owner,
                "before": before,
                "after": after,
                "label": f"Detach {element.fx_name} for editing",
            })
            self._notify(
                f"{element.fx_name} detached from shared model vertices; "
                f"{detached} mirrored/source vertex reference(s) duplicated. "
                "The operation can be undone.",
                9000)
            return updated
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
            self._notify(f"FX detach failed and was rolled back: {exc}.", 9000)
            return None

    def _detach_mirrored_shared_fx(self, polygon_ids) -> bool:
        """Detach every shared FX touched by Mirror Select before transforms."""

        pending = {int(poly_id) for poly_id in polygon_ids}
        while True:
            shared = next((
                element for element in self._fx_elements
                if element.owner_path == self.viewport.edit_owner
                and element.shared_state == "shared"
                and pending.intersection(element.poly_ids)
            ), None)
            if shared is None:
                return True
            if self._detach_fx_for_editing(shared) is None:
                return False

    def _can_delete_selected_fx_element(self) -> bool:
        element = self._selected_fx_element()
        return bool(
            self._editing_allowed()
            and element is not None
            and element.shared_state != "invalid"
            and not self._delete_geometry_reason())

    def _fx_structural_delete_reason(self, element: FxElement) -> str:
        if element.shared_state == "invalid":
            return (
                f"{element.fx_name} is structurally invalid")
        fam_obj = self._owner_to_obj.get(element.owner_path)
        if fam_obj is None or fam_obj is not self._workbench_obj:
            return "the owner is not the editable model"
        selected = set(element.poly_ids)
        try:
            plan_delete_geometry(
                fam_obj, selected, MappingIndex(fam_obj),
                self._unsafe_fx_geometry_polys(
                    element.owner_path, selected), allow_empty=True)
        except GeometryClipboardError as exc:
            return str(exc)
        return ""

    def _fx_capabilities(
            self, element: FxElement) -> FxElementCapabilities:
        fam_obj = self._owner_to_obj.get(element.owner_path)
        family = self._family
        can_move = element.shared_state != "invalid"
        can_copy = False
        can_add_similar = False
        if fam_obj is not None and family is not None \
                and element.shared_state != "invalid":
            try:
                build_fx_element_clipboard(
                    fam_obj, element, family.animations)
                can_copy = True
                can_add_similar = True
            except GeometryClipboardError:
                pass
        delete_reason = self._fx_structural_delete_reason(element)
        can_delete = not delete_reason
        save_reason = (
            self._geometry_writer_reason(element.owner_path, fam_obj)
            if fam_obj is not None else "owner unavailable")
        can_save = not save_reason
        return FxElementCapabilities(
            can_move=can_move,
            can_scale=can_move,
            can_copy=can_copy,
            can_paste=can_copy,
            can_delete=can_delete,
            can_add_similar=can_add_similar,
            can_save=can_save,
            can_overwrite=(
                can_save and element.owner_path in self._bundle_targets),
            can_save_as=can_save,
            delete_reason=delete_reason,
        )

    def _delete_selected_fx_element(self) -> None:
        if not self._require_editing("Delete FX Element"):
            return
        element = self._selected_fx_element()
        if element is None:
            # A multi-element selection lives in the geometry, not in the list:
            # Delete Selected FX then removes every selected FX at once.
            if self._active_fx_elements():
                self._delete_geometry()
                return
            self._notify("Select an FX element from the list first.", 5000)
            return
        if element.shared_state == "invalid":
            self._notify(
                f"Delete refused: {element.fx_name} is "
                "structurally invalid.", 8000)
            return
        identity = element.identity
        if self._selected_owner != element.owner_path:
            self._select_owner(element.owner_path)
            index = self._fx_combo_index(identity)
            self.fx_combo.blockSignals(True)
            self.fx_combo.setCurrentIndex(index)
            self.fx_combo.blockSignals(False)
        index = self._fx_combo_index(identity)
        if index <= 0:
            self._notify(
                "Delete refused: the selected FX element is no longer "
                "available in the active owner.", 7000)
            return
        self.fx_combo.blockSignals(True)
        self.fx_combo.setCurrentIndex(index)
        self.fx_combo.blockSignals(False)
        self._select_highlight_fx()
        self._sync_editor_context()
        self._delete_geometry()

    def _rename_selected_fx(self) -> None:
        element = self._selected_fx_element()
        if element is None:
            return
        key = (element.owner_path, element.poly_ids)
        value, accepted = QInputDialog.getText(
            self, "Rename FX", "Name in this editing session (empty resets):",
            text=self._fx_aliases.get(key, ""))
        if not accepted:
            return
        if value.strip():
            self._fx_aliases[key] = value.strip()
        else:
            self._fx_aliases.pop(key, None)
        self._refresh_fx_elements()
        self._select_highlight_fx()
        self._sync_edit_action_states()

    def _show_fx_combo_context_menu(self, position) -> None:
        menu = QMenu(self.fx_combo)
        menu.addAction(self.select_all_fx_action)
        add_action = menu.addAction(
            "Add FX", self._add_fx_element)
        add_action.setEnabled(self._can_add_fx_element())
        menu.addAction(self.clone_fx_action)
        menu.addAction(self.rename_fx_action)
        delete_action = menu.addAction(
            "Delete Selected FX Element",
            self._delete_selected_fx_element)
        delete_action.setEnabled(self._can_delete_selected_fx_element())
        menu.exec(self.fx_combo.mapToGlobal(position))

    def _fx_add_templates(self) -> list[FxElement]:
        family = self._family
        fam_obj = self._owner_to_obj.get(self.viewport.edit_owner) \
            if self.viewport.edit_owner else None
        if family is None or fam_obj is None:
            return []
        templates = []
        seen = set()
        for element in self._fx_elements:
            if element.shared_state == "invalid" \
                    or element.owner_path != fam_obj.owner_path:
                continue
            key = element.identity
            if key in seen:
                continue
            try:
                build_fx_element_clipboard(
                    fam_obj, element, family.animations)
            except GeometryClipboardError:
                continue
            seen.add(key)
            templates.append(element)
        return templates

    def _fx_template_preview(
            self, element: FxElement) -> tuple[list[QImage], list[str]]:
        cached = self._fx_preview_cache.get(element.identity)
        if cached is not None:
            return cached
        frames: list[QImage] = []
        phase_count = 1
        if element.source_kind == "VANM" and self._family is not None:
            animation_names = []
            for _poly_id, _block_index, _atts_index, block, _material in \
                    self._fx_face_records(element):
                texture = block.texture
                if texture is not None and texture.kind == "bmpanim":
                    animation_names.append(texture.name)
            animations = []
            for name in dict.fromkeys(animation_names):
                normalized = name.replace("\\", "/").casefold()
                animation = next((
                    value for key, value in self._family.animations.items()
                    if key.replace("\\", "/").casefold() == normalized
                ), None)
                if animation is not None:
                    animations.append(animation)
            for animation in animations:
                phase_count = max(phase_count, len(animation.frames))
                for frame in animation.frames:
                    if not (
                            0 <= frame.frame_id
                            < len(animation.bitmap_names)
                            and 0 <= frame.texcoords_id
                            < len(animation.texcoord_groups)):
                        continue
                    image = self._texture_qimage(
                        animation.bitmap_names[frame.frame_id])
                    if image is None or image.isNull():
                        continue
                    frames.append(self._fx_uv_preview_image(
                        image,
                        [animation.texcoord_groups[
                            frame.texcoords_id]]))
        else:
            groups_by_material: dict[str, list[list[tuple[int, int]]]] = {}
            for _poly_id, _block_index, atts_index, block, material in \
                    self._fx_face_records(element):
                if 0 <= atts_index < len(block.olpl):
                    groups_by_material.setdefault(
                        material.casefold(), []).append(
                            list(block.olpl[atts_index]))
            for material in dict.fromkeys(element.material_names):
                image = self._texture_qimage(material)
                if image is not None and not image.isNull():
                    frames.append(self._fx_uv_preview_image(
                        image, groups_by_material.get(
                            material.casefold(), [])))
        frame_label = "frame" if phase_count == 1 else "frames"
        state = "bilateral" if element.bilateral else element.shared_state
        uv_state = (
            "UV VANM"
            if element.source_kind == "VANM"
            else "UV OLPL editable"
            if element.editable and element.uv_source == "OLPL"
            else f"UV {element.uv_source} unavailable")
        info = [
            f"{element.fx_name} | {element.source_kind} | "
            f"{len(element.poly_ids)} polygon(s) | "
            f"{phase_count} {frame_label} | {state} | {uv_state}",
        ]
        if not frames:
            info.append(
                "preview diagnostic: referenced asset is missing or unreadable")
        result = (frames, info)
        self._fx_preview_cache[element.identity] = result
        return result

    @staticmethod
    def _fx_uv_preview_image(image, groups):
        return crop_uv_region(image, groups)

    def _can_add_fx_element(self) -> bool:
        return bool(
            self._editing_allowed()
            and not self.viewport.paste_preview_active
            and self._workbench_obj is not None
            and self._workbench_obj.skeleton is not None)

    def _load_fx_source(self, kind):
        family = self._family
        if family is None:
            return []
        if kind == "model":
            filename, _ = QFileDialog.getOpenFileName(
                self, "Choose a model containing FX", str(self._last_directory),
                "BASE models (*.base *.BASE)")
            if not filename:
                return []
            source_family = load_asset_family(
                filename, self._extra_roots, setbas=self._setbas)
            sources = self._fx_sources_for_objects(
                source_family, source_family.all_objects())
            if not sources:
                raise GeometryClipboardError(
                    "This model has no readable FX geometry/material pair.")
            return Path(filename).name, sources
        if kind == "bas":
            archive = self._setbas
            if archive is None:
                path = choose_bas_archive(self, self._last_directory)
                if not path:
                    return []
                archive = read_setbas(path)
            embedded = (
                self._vp_embedded
                if archive is self._setbas and self._vp_embedded is not None
                else reconstruct_embedded_vps(archive.path))
            entries = [
                entry for entry in embedded.entries
                if entry.normalized_base != DUMMY_BASE_NAME
            ]
            if not entries:
                raise VPEmbeddedError(
                    "This BAS archive has no usable VP-defined models.")
            labels = [
                f"VP {entry.index}: {entry.base_name}"
                + (f" | {entry.skeleton_name}" if entry.skeleton_name else "")
                for entry in entries
            ]
            selected, accepted = QInputDialog.getItem(
                self, "Choose BAS model", "VP-defined model:",
                labels, 0, False)
            if not accepted:
                return []
            entry = entries[labels.index(selected)]
            source_family = load_asset_family(
                Path(archive.path), self._extra_roots, {}, archive)
            source_obj = self._resolve_family_base_object(
                source_family, entry.base_name, entry.source_objt_offset)
            if source_obj is None:
                raise VPEmbeddedError(
                    f"VP {entry.index} ({entry.base_name}) could not be "
                    "resolved to its embedded BASE object.")
            sources = self._fx_sources_for_objects(
                source_family, list(source_obj.iter_tree()))
            if not sources:
                raise GeometryClipboardError(
                    f"VP {entry.index} ({entry.base_name}) has no readable "
                    "FX geometry/material pair.")
            return f"VP {entry.index}: {entry.base_name}", sources
        return []

    @staticmethod
    def _fx_sources_for_objects(source_family, objects):
        """Return only FX elements proven copyable from the given objects."""

        sources = []
        for obj in objects:
            for element in detect_fx_elements(
                    obj, source_family.animations):
                try:
                    build_fx_element_clipboard(
                        obj, element, source_family.animations)
                except GeometryClipboardError:
                    continue
                sources.append((source_family, obj, element))
        return sources

    def _add_fx_element(
            self, *, preferred: FxElement | None = None) -> None:
        if not self._require_editing("Add FX Element"):
            return
        templates = self._fx_add_templates()
        dialog = AddFxElementDialog(
            templates, self, preferred=preferred,
            preview_provider=self._fx_template_preview,
            source_loader=self._load_fx_source)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self._notify("Add FX Element cancelled.", 3000)
            return
        template = dialog.selected_template()
        family = self._family
        fam_obj = self._owner_to_obj.get(self.viewport.edit_owner)
        if template is None or family is None or fam_obj is None:
            self._notify("Add FX Element refused: the target changed.", 7000)
            return
        try:
            context = dialog.source_context(template)
            if context is not None:
                source_family, source = context
                clipboard = prepare_fx_clipboard(
                    fam_obj, source_family, source, template)
                stage_fx_clipboard(family, fam_obj, clipboard)
            else:
                clipboard = build_fx_element_clipboard(
                    fam_obj, template, family.animations)
        except (GeometryClipboardError, MappingEditError) as exc:
            self._notify(f"Add FX Element refused: {exc}.", 9000)
            return
        self._geometry_clipboard = clipboard
        position = self._current_viewport_position()
        if not self.viewport.begin_paste_preview(clipboard, position):
            self._notify(
                "Add FX Element prepared, but its preview could not start: "
                f"{self._preview_geometry_reason()}.", 9000)
            return
        self.viewport.setFocus()
        self._sync_edit_action_states()
        self._notify(
            f"Clone {clipboard.fx_name} "
            f"{'VANM ' if clipboard.source_kind == 'VANM' else ''}"
            "FX Preview. Paste/LMB/Enter confirms; RMB/Esc cancels. "
            "Scale and orientation are unchanged.",
            12000)

    def _select_all_fx_elements(self) -> None:
        """Select every FX element of the current model and nothing else."""

        if self.viewport.paste_preview_active:
            self._notify(
                "Selection unchanged: finish or cancel the active placement "
                "preview first.", 6000)
            return
        elements = tuple(self._fx_elements)
        if not elements:
            self.statusBar().showMessage(
                "Select All FX: the current model has no FX element.", 6000)
            return
        owner = elements[0].owner_path
        if self._selected_owner != owner:
            self._select_owner(owner)
            elements = tuple(
                element for element in self._fx_elements
                if element.owner_path == owner)
            if not elements:
                return
        fx_polys = self._complete_fx_polygon_selection(
            poly_id for element in elements for poly_id in element.poly_ids)
        # Mirror expansion is intentionally skipped: every FX is already in the
        # selection and mirroring could pull plain model polygons into it.
        self._mirror_select_source_polys = set(fx_polys)
        self._selected_poly = min(fx_polys, default=None)
        self._selected_polys = set(fx_polys)
        self.viewport.set_selected_polygon(self._selected_poly)
        self.viewport.set_highlight_polys(self._selected_polys)
        # In Edit Mode select exactly the FX POO2 vertices, so the transforms
        # never touch the regular model geometry.
        session = self.viewport.edit_session
        if session is not None and self.viewport.edit_owner == owner:
            self.viewport.select_edit_vertices(sorted(
                {index for element in elements
                 for index in element.vertex_indices}))
        self._sync_poly_id_control()
        self._fill_polygon_inspector(self._selected_poly)
        self._update_repair_buttons()
        if len(elements) > 1:
            # The list cannot name a single element for this selection.
            self.fx_combo.blockSignals(True)
            self.fx_combo.setCurrentIndex(0)
            self.fx_combo.blockSignals(False)
        self._sync_fx_combo_label()
        self._sync_edit_action_states()
        self.statusBar().showMessage(
            f"Selected {len(elements)} FX element(s): "
            + ", ".join(element.fx_name for element in elements)
            + " | polyIDs "
            + ",".join(str(poly_id) for poly_id in sorted(fx_polys)))

    def _select_highlight_fx(self) -> None:
        element = self._selected_fx_element()
        if element is None:
            return
        if self._selected_owner != element.owner_path:
            identity = element.identity
            self._select_owner(element.owner_path)
            index = self._fx_combo_index(identity)
            self.fx_combo.blockSignals(True)
            self.fx_combo.setCurrentIndex(index)
            self.fx_combo.blockSignals(False)
            element = self._selected_fx_element()
            if element is None:
                return
        primary_poly = element.poly_ids[0]
        source_polys = self._complete_fx_polygon_selection(element.poly_ids)
        selected_polys = source_polys
        if self._mirror_select_active():
            self._mirror_select_source_polys = set(source_polys)
            selected_polys = self._mirrored_polygon_selection(source_polys)
        self._selected_poly = primary_poly
        self._selected_polys = set(selected_polys)
        self.viewport.set_selected_polygon(primary_poly)
        self.viewport.set_highlight_polys(self._selected_polys)
        self._sync_poly_id_control()
        self._fill_polygon_inspector(primary_poly)
        self._update_repair_buttons()
        self.statusBar().showMessage(
            f"Selected {element.fx_name} polyIDs "
            f"{','.join(str(poly_id) for poly_id in element.poly_ids)} "
            f"(material blocks "
            f"{','.join(str(index) for index in element.block_indices)})"
            + (
                f" | Invalid: {element.shared_state} geometry/mapping."
                if element.shared_state == "invalid" else "")
        )

    def _edit_selected_fx(self) -> None:
        element = self._selected_fx_element()
        if element is None:
            return
        if element.shared_state == "invalid":
            QMessageBox.information(
                self, "Invalid FX element",
                "This FX element has invalid or ambiguous geometry/mapping "
                "references and cannot be edited safely."
            )
            return
        if not (
                self._mirror_select_active()
                and self._selected_polys
                and self._mirror_select_source_polys):
            self._select_highlight_fx()
        self._remember_geometry_original(element.owner_path)
        vertices = (
            self._polygon_vertices(
                self.viewport.edit_session.model, self._selected_polys)
            if self._mirror_select_active()
            and self.viewport.edit_session is not None
            else set(element.vertex_indices))
        if self.viewport.enter_edit_mode_with_vertices(
                element.owner_path, vertices):
            if self._mirror_select_active():
                self._set_mirror_transform_source(
                    self._polygon_vertices(
                        self.viewport.edit_session.model,
                        self._mirror_select_source_polys))
            self.viewport.configure_edit_interaction(
                selected_only=True, pick_polygons=True)
            self.viewport.setFocus()
            self.statusBar().showMessage(
                f"Editing {element.fx_name} polyIDs "
                f"{','.join(str(poly_id) for poly_id in element.poly_ids)}: "
                "drag a red selected vertex to move; G/R/S transform; "
                "X/Y/Z constrain.",
                10000,
            )
