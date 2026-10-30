"""AssemblyWindow mixin: geometry editing: transform gizmo, mirror selection, copy/paste/move/
delete of geometry, viewport context menu and the edit history (undo/redo)."""

from __future__ import annotations

import copy
import math
from pathlib import Path

from PySide6.QtGui import (
    QCursor,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QMenu,
    QMessageBox,
    QVBoxLayout,
)

from studio.assets.catalog import classify_texture_assignment
from studio.assets.family import (
    _is_setbas_family,
    rebuild_materials,
)
from studio.assets.mapping import (
    export_base_object_bytes,
    MappingEditError,
    MappingIndex,
)
from studio.formats.animation import AnmParseError, export_anm_bytes
from studio.formats.sklt import (
    parse_sklt_bytes,
    SkltParseError,
)
from studio.ui.widgets import (
    LiveRotateDialog,
    LiveScaleDialog,
)
from studio.rendering.geometry import (
    append_fx_element_clipboard,
    append_geometry_clipboard,
    apply_delete_geometry,
    build_fx_elements_clipboard,
    build_geometry_clipboard,
    FxElement,
    FxElementClipboard,
    GeometryClipboardError,
    ImportedFxClipboard,
    match_mirror_points,
    match_mirror_polygons,
    plan_delete_geometry,
    stage_fx_clipboard,
    unsafe_fx_geometry_polys,
    validate_fx_element_clipboard,
)


class _GeometryEditMixin:
    """Part of AssemblyWindow; `self` is always the AssemblyWindow."""

    # -- Models and Textures ------------------------------------------------------

    def _apply_transform_mode_colors(self) -> None:
        colors = {
            "move": ("#8f2530", "#ef3f50"),
            "rotate": ("#244f99", "#4b86ff"),
            "scale": ("#28783f", "#43c96c"),
        }
        for mode, button in self.transform_mode_buttons.items():
            base, border = colors[mode]
            button.setStyleSheet(
                "QPushButton:checked {"
                f"background: {base}; border: 2px solid {border};"
                "color: white; font-weight: bold; }")

    def _set_transform_mode(self, mode: str) -> None:
        mode = str(mode).lower()
        if mode not in ("move", "rotate", "scale"):
            return
        if self.viewport.paste_preview_active and mode != "move":
            mode = "move"
        spin = getattr(self, "gizmo_intensity_spin", None)
        if spin is not None:
            self._transform_steps[self._transform_mode] = float(spin.value())
        self._transform_mode = mode
        for name, button in self.transform_mode_buttons.items():
            button.blockSignals(True)
            button.setChecked(name == mode)
            button.blockSignals(False)
        self.model_gizmo.set_mode(mode)
        self._apply_transform_mode_colors()
        settings = {
            "move": {
                "label": "Move step",
                "help": (
                    "UA axes: -Y = Up, +Y = Down. Diagonal handles apply "
                    "the full step on every active axis."),
                "range": (0.001, 1000.0),
                "step": 0.1,
                "suffix": " model units",
                "tip": (
                    "Type the exact movement distance applied by each gizmo "
                    "nudge."),
            },
            "rotate": {
                "label": "Rotate step",
                "help": (
                    "Positive and negative rotation use the right-hand rule "
                    "around the selected model-space X, Y or Z axis."),
                "range": (0.1, 360.0),
                "step": 1.0,
                "suffix": "°",
                "tip": "Set the angle applied by each rotation action.",
            },
            "scale": {
                "label": "Scale step",
                "help": (
                    "Scale X, Y, Z or all axes. Minus handles apply the safe "
                    "inverse and never use a zero or negative factor."),
                "range": (0.1, 95.0),
                "step": 1.0,
                "suffix": "%",
                "tip": "Set the percentage applied by each scale action.",
            },
        }[mode]
        self.transform_step_label.setText(settings["label"])
        self.model_gizmo.setToolTip(settings["help"])
        self.auto_align_check.setVisible(mode == "move")
        self._transform_options_layout.removeWidget(
            self.mirror_select_check)
        self._transform_options_layout.addWidget(
            self.mirror_select_check, 0, 0)
        spin.blockSignals(True)
        spin.display_mode = mode
        spin.setRange(*settings["range"])
        spin.setDecimals(4)
        spin.setSingleStep(settings["step"])
        spin.setSuffix(settings["suffix"])
        spin.setValue(self._transform_steps[mode])
        spin.setToolTip(settings["tip"])
        spin.blockSignals(False)
        slider = self.gizmo_intensity_slider
        slider.blockSignals(True)
        if mode == "move":
            slider.setRange(0, 1000)
            slider.setValue(self._gizmo_intensity_to_slider(spin.value()))
        elif mode == "rotate":
            slider.setRange(1, 3600)
            slider.setValue(round(spin.value() * 10.0))
        else:
            slider.setRange(1, 950)
            slider.setValue(round(spin.value() * 10.0))
        slider.setToolTip(settings["tip"])
        slider.blockSignals(False)
        self.viewport.set_direct_transform(mode, spin.value())

    def _mirror_axes(self) -> tuple[int, ...]:
        return (0, 1, 2)

    def _on_mirror_select_toggled(self, enabled: bool) -> None:
        session = self.viewport.edit_session
        if enabled:
            if session is not None:
                session.set_mirror_enabled(True)
            self._expand_current_mirror_selection()
        else:
            source_vertices = (
                set(session.mirror_source_selection)
                if session is not None else set())
            context = self._mirror_selection_context()
            if context is not None and self._mirror_select_source_polys:
                _session, owner, fam_obj = context
                self._set_mirror_polygon_selection(
                    owner, fam_obj,
                    self._mirror_select_source_polys)
            elif session is not None and source_vertices:
                session.set_selection(source_vertices)
                self._selected_polys.clear()
                self._selected_poly = None
                self._on_edit_selection_details_changed(
                    source_vertices, "programmatic")
            self._mirror_select_source_polys.clear()
            if session is not None:
                session.set_mirror_enabled(False)
                session.set_mirror_source_selection(())
        self._sync_edit_action_states()

    def _begin_gizmo_nudge(self, direction) -> None:
        if not self._editing_allowed():
            return
        if self.viewport.paste_preview_active:
            return
        if self._transform_mode == "move":
            self.viewport.begin_model_nudge()
        else:
            self.viewport.begin_model_transform(
                self._transform_mode, direction)

    @staticmethod
    def _slider_to_gizmo_intensity(value: int) -> float:
        normalized = max(0.0, min(1.0, int(value) / 1000.0))
        return 10.0 ** (-3.0 + normalized * 6.0)

    @staticmethod
    def _gizmo_intensity_to_slider(value: float) -> int:
        safe = max(0.001, min(1000.0, float(value)))
        normalized = (math.log10(safe) + 3.0) / 6.0
        return max(0, min(1000, round(normalized * 1000.0)))

    def _gizmo_intensity(self) -> float:
        spin = getattr(self, "gizmo_intensity_spin", None)
        return float(spin.value()) if spin is not None else 1.0

    def _set_gizmo_intensity_from_slider(self, value: int) -> None:
        spin = getattr(self, "gizmo_intensity_spin", None)
        if spin is None:
            return
        if self._transform_mode == "move":
            step = self._slider_to_gizmo_intensity(value)
        else:
            step = float(value) / 10.0
        spin.blockSignals(True)
        spin.setValue(step)
        spin.blockSignals(False)
        self._transform_steps[self._transform_mode] = float(spin.value())
        self.viewport.set_direct_transform(
            self._transform_mode, spin.value())

    def _set_gizmo_slider_from_intensity(self, value: float) -> None:
        slider = getattr(self, "gizmo_intensity_slider", None)
        if slider is None:
            return
        slider.blockSignals(True)
        if self._transform_mode == "move":
            slider.setValue(self._gizmo_intensity_to_slider(value))
        else:
            slider.setValue(round(float(value) * 10.0))
        slider.blockSignals(False)
        self._transform_steps[self._transform_mode] = float(value)
        self.viewport.set_direct_transform(
            self._transform_mode, value)

    def _apply_gizmo_nudge(self, direction) -> None:
        # ModelSpaceGizmo is normally disabled in View Mode, but guard the
        # callback too so programmatic signals/shortcuts cannot mutate data.
        if not self._editing_allowed():
            return
        step = self._gizmo_intensity()
        if self.viewport.paste_preview_active:
            dx, dy, dz = (component * step for component in direction)
            self.viewport.nudge_paste_preview_model(dx, dy, dz)
        elif self._transform_mode == "move":
            dx, dy, dz = (component * step for component in direction)
            self.viewport.nudge_edit_selection_model(dx, dy, dz)
        elif self._transform_mode == "rotate":
            self.viewport.rotate_edit_selection_model(math.radians(step))
        elif self._transform_mode == "scale":
            self.viewport.scale_edit_selection_model(step / 100.0)

    def _finish_gizmo_nudge(self) -> None:
        if not self.viewport.paste_preview_active \
                and self._transform_mode == "move":
            self.viewport.end_model_nudge()
        elif not self.viewport.paste_preview_active:
            self.viewport.end_model_transform()
        self.viewport.setFocus()

    def _mirror_selection_context(self):
        session, owner, fam_obj = self._geometry_context()
        if not self._editing_allowed() or session is None or owner is None:
            return None
        if fam_obj is None or fam_obj is not self._workbench_obj:
            return None
        if self.viewport.paste_preview_active:
            return None
        return session, owner, fam_obj

    def _mirror_select_active(self) -> bool:
        check = getattr(self, "mirror_select_check", None)
        return bool(
            check is not None and check.isChecked()
            and self._mirror_selection_context() is not None)

    def _complete_fx_polygon_selection(self, polygon_ids) -> set[int]:
        """Keep every detected FX1/FX2 element structurally atomic."""

        selected = {int(poly_id) for poly_id in polygon_ids}
        for element in self._fx_elements:
            if element.owner_path == self.viewport.edit_owner \
                    and selected.intersection(element.poly_ids):
                selected.update(element.poly_ids)
        return selected

    def _mirrored_fx_element_polygons(
            self, selected_polys, point_match) -> set[int]:
        """Resolve atomic FX counterparts hidden by bilateral POL2 ambiguity.

        A bilateral FX1/FX2 element is stored as two opposite-winding POL2
        faces that share one vertex set.  The generic polygon matcher correctly
        treats the two mirrored target faces as ambiguous, but at FX-element
        level they are one unique editable object.  Match those complete
        elements by their reflected POO2 vertex sets without guessing between
        genuinely distinct overlapping FX elements.
        """

        owner = self.viewport.edit_owner
        selected = {int(poly_id) for poly_id in selected_polys}
        if owner is None or not selected:
            return set()
        elements = [
            element for element in self._fx_elements
            if element.owner_path == owner
            and element.shared_state != "invalid"
            and element.vertex_indices
        ]
        sources = [
            element for element in elements
            if set(element.poly_ids).issubset(selected)
        ]
        if not sources:
            return set()

        by_vertices: dict[tuple[str, str, frozenset[int]], list[FxElement]] = {}
        for element in elements:
            key = (
                element.fx_name,
                element.source_kind,
                frozenset(element.vertex_indices),
            )
            by_vertices.setdefault(key, []).append(element)

        axis_maps = {
            axis: dict(pairs) for axis, pairs in point_match.axis_pairs
        }
        axes = tuple(point_match.axes)
        recipients: set[int] = set()
        for source in sources:
            source_vertices = tuple(source.vertex_indices)
            source_key = frozenset(source_vertices)
            for mask in range(1, 1 << len(axes)):
                target_vertices = list(source_vertices)
                valid = True
                for bit, axis in enumerate(axes):
                    if not (mask & (1 << bit)):
                        continue
                    axis_map = axis_maps.get(axis, {})
                    remapped = []
                    for vertex in target_vertices:
                        target = axis_map.get(vertex)
                        if target is None:
                            valid = False
                            break
                        remapped.append(target)
                    if not valid:
                        break
                    target_vertices = remapped
                if not valid:
                    continue
                target_key = frozenset(target_vertices)
                if target_key == source_key:
                    continue
                matches = by_vertices.get((
                    source.fx_name, source.source_kind, target_key), ())
                # One logical FX element may contain two bilateral faces, but
                # two separate elements at the same coordinates remain truly
                # ambiguous and must not be selected automatically.
                if len(matches) == 1:
                    recipients.update(matches[0].poly_ids)
        return recipients

    def _mirrored_polygon_selection(self, polygon_ids) -> set[int]:
        context = self._mirror_selection_context()
        if context is None:
            return {int(poly_id) for poly_id in polygon_ids}
        session, _owner, _fam_obj = context
        selected = self._complete_fx_polygon_selection({
            int(poly_id) for poly_id in polygon_ids
            if 0 <= int(poly_id) < len(session.model.polygons)})
        if not selected:
            return set()
        result = match_mirror_polygons(
            session.model.points, session.model.polygons,
            selected, self._mirror_axes())
        recipients = set(result.recipient_indices)
        recipients.update(self._mirrored_fx_element_polygons(
            selected, result.point_match))
        return self._complete_fx_polygon_selection(selected | recipients)

    def _mirrored_vertex_selection(self, vertex_ids) -> set[int]:
        context = self._mirror_selection_context()
        if context is None:
            return {int(vertex_id) for vertex_id in vertex_ids}
        session, _owner, _fam_obj = context
        selected = {
            int(vertex_id) for vertex_id in vertex_ids
            if 0 <= int(vertex_id) < len(session.model.points)}
        if not selected:
            return set()
        result = match_mirror_points(
            session.model.points, selected, self._mirror_axes())
        return selected | set(result.recipient_indices)

    @staticmethod
    def _polygon_vertices(model, polygon_ids) -> set[int]:
        return {
            vertex
            for poly_id in polygon_ids
            if 0 <= poly_id < len(model.polygons)
            for vertex in model.polygons[poly_id]}

    def _set_mirror_transform_source(self, vertex_ids) -> None:
        session = self.viewport.edit_session
        if session is None:
            return
        session.set_mirror_enabled(
            bool(self.mirror_select_check.isChecked()))
        session.set_mirror_source_selection(vertex_ids)

    def _set_mirror_polygon_selection(
            self, owner: str, fam_obj, polygon_ids,
            source_polygon_ids=()) -> None:
        selected = {
            int(poly_id) for poly_id in polygon_ids
            if 0 <= int(poly_id) < len(fam_obj.skeleton.polygons)}
        vertices = {
            index for poly_id in selected
            for index in fam_obj.skeleton.polygons[poly_id]}
        self._selected_owner = owner
        self._selected_polys = selected
        self._selected_poly = min(selected, default=None)
        session = self.viewport.edit_session
        if session is not None and session.model is fam_obj.skeleton:
            session.set_selection(vertices)
            self._set_mirror_transform_source(
                self._polygon_vertices(
                    fam_obj.skeleton, source_polygon_ids))
        self.viewport.set_selected_owner(owner)
        self.viewport.set_selected_polygon(self._selected_poly)
        self.viewport.set_highlight_polys(selected)
        element = next((
            candidate for candidate in self._fx_elements
            if candidate.owner_path == owner
            and set(candidate.poly_ids).issubset(selected)
        ), None)
        self.fx_combo.blockSignals(True)
        self.fx_combo.setCurrentIndex(
            self._fx_combo_index(element.identity) if element else 0)
        self.fx_combo.blockSignals(False)
        self._sync_poly_id_control()
        self._fill_polygon_inspector(self._selected_poly)
        self._update_repair_buttons()
        self._sync_edit_action_states()
        self.viewport.setFocus()

    def _expand_current_mirror_selection(self) -> None:
        context = self._mirror_selection_context()
        if context is None:
            return
        session, owner, fam_obj = context
        explicit_polys = {
            poly_id for poly_id in self._selected_polys
            if 0 <= poly_id < len(session.model.polygons)}
        if explicit_polys:
            sources = (
                set(self._mirror_select_source_polys)
                if self._mirror_select_source_polys
                and self._mirror_select_source_polys.issubset(explicit_polys)
                else set(explicit_polys))
            sources = self._complete_fx_polygon_selection(sources)
            self._mirror_select_source_polys = sources
            expanded = self._mirrored_polygon_selection(sources)
            if self._editing_allowed():
                mirrored_polys = set(expanded)
                self._detach_mirrored_shared_fx(expanded)
                sources = self._complete_fx_polygon_selection(sources)
                self._mirror_select_source_polys = sources
                expanded = self._complete_fx_polygon_selection(
                    mirrored_polys)
            self._set_mirror_polygon_selection(
                owner, fam_obj, expanded, sources)
            return
        selected_vertices = set(session.selection)
        expanded = self._mirrored_vertex_selection(selected_vertices)
        session.set_selection(expanded)
        self._set_mirror_transform_source(selected_vertices)
        self._selected_polys.clear()
        self._on_edit_selection_details_changed(
            expanded, "programmatic")

    def _unsafe_fx_geometry_polys(
            self, owner: str, selected_polys) -> set[int]:
        return unsafe_fx_geometry_polys(
            self._fx_elements, owner, selected_polys)

    def _build_geometry_placement_clipboard(self, elements=None):
        """Build the one immutable source used by both Copy and Move."""

        explicit_elements = tuple(elements) if elements is not None else None
        fx_elements = (
            explicit_elements if explicit_elements is not None
            else tuple(self._active_fx_elements()))
        if fx_elements:
            element = fx_elements[0]
            family = self._family
            fam_obj = self._owner_to_obj.get(element.owner_path)
            if family is None or fam_obj is None:
                raise GeometryClipboardError(
                    "the source FX model is unavailable")
            clipboard = build_fx_elements_clipboard(
                fam_obj, fx_elements, family.animations)
            self._selected_polys = {
                poly_id for selected in fx_elements
                for poly_id in selected.poly_ids}
            self.viewport.set_highlight_polys(self._selected_polys)
            descriptor = (
                f"{len(fx_elements)} mirrored {element.fx_name} "
                f"{'VANM ' if element.source_kind == 'VANM' else ''}elements"
                if len(fx_elements) > 1 else
                f"{element.fx_name} "
                f"{'VANM ' if element.source_kind == 'VANM' else ''}Element")
            return clipboard, descriptor

        owner = self.viewport.edit_owner
        fam_obj = self._owner_to_obj.get(owner) if owner else None
        if fam_obj is None:
            raise GeometryClipboardError(
                "the editable source model is unavailable")
        selected = self._resolved_geometry_polys()
        mapping = MappingIndex(fam_obj)
        clipboard = build_geometry_clipboard(
            fam_obj, owner, selected, mapping,
            self._unsafe_fx_geometry_polys(owner, selected),
            require_writable_chunks=False)
        return clipboard, f"{len(clipboard.polygons)} polygon(s)"

    def _start_geometry_placement(
            self, operation: str, clipboard, descriptor: str,
            *, position=None, store_copy: bool = False) -> bool:
        """Start the shared placement controller used by Move and Copy."""

        operation = str(operation).casefold()
        if operation not in ("copy", "move"):
            operation = "copy"
        if store_copy:
            self._geometry_clipboard = clipboard
        if position is None or not hasattr(position, "x"):
            position = self._current_viewport_position()
        # Placement is a translation operation in both modes.  This also makes
        # keyboard arrows and gizmo handles use the visible Move-step value.
        self._set_transform_mode("move")
        started = self.viewport.begin_paste_preview(
            clipboard, position, operation=operation)
        self._sync_edit_action_states()
        self.viewport.setFocus()
        if not started:
            self._notify(
                f"{operation.title()} {descriptor} could not start.", 9000)
            return False
        action = "Copied" if operation == "copy" else "Moving"
        self._notify(
            f"{action} {descriptor}. Move the transparent preview; "
            "LMB/Enter confirms, RMB/Esc cancels.", 12000)
        return True

    def _copy_geometry(self) -> None:
        if self.viewport.paste_preview_active:
            self.viewport.cancel_paste_preview()
        reason = self._copy_geometry_reason()
        if reason:
            self._notify(f"Copy refused: {reason}.", 7000)
            return
        try:
            clipboard, descriptor = self._build_geometry_placement_clipboard()
        except GeometryClipboardError as exc:
            self._notify(f"Copy refused: {exc}.", 9000)
            return
        self._start_geometry_placement(
            "copy", clipboard, descriptor, store_copy=True)


    def _geometry_context(self):
        owner = self.viewport.edit_owner
        fam_obj = self._owner_to_obj.get(owner) if owner else None
        return self.viewport.edit_session, owner, fam_obj

    def _resolved_geometry_polys(self) -> set[int]:
        """Return the one canonical geometry selection for edit actions.

        An explicit polygon pick is authoritative.  Some UA assets contain
        overlapping or duplicate POL2 faces which reuse the exact same POO2
        vertices; deriving polygons again from the vertex selection would make
        a normal click appear to select several areas.  Vertex-derived polygon
        selection remains available for genuine vertex/box gestures, because
        those gestures clear ``_selected_polys`` first.
        """

        session, owner, fam_obj = self._geometry_context()
        model = getattr(fam_obj, "skeleton", None)
        if session is None or owner is None or model is None \
                or session.model is not model \
                or getattr(fam_obj, "owner_path", None) != owner:
            return set()
        resolved = {
            poly_id for poly_id in self._selected_polys
            if 0 <= poly_id < len(model.polygons)
        }
        if resolved:
            return resolved
        selected_vertices = set(session.selection)
        if selected_vertices:
            resolved.update(
                poly_id
                for poly_id, polygon in enumerate(model.polygons)
                if polygon and set(polygon).issubset(selected_vertices)
            )
        return resolved

    def _on_edit_selection_details_changed(
            self, _vertices, source: str) -> None:
        if source == "vertex":
            # A vertex gesture replaces the older direct polygon-pick intent.
            # Complete polygons are then re-derived from the POO2 selection.
            self._selected_polys.clear()
            self._mirror_select_source_polys.clear()
            session = self.viewport.edit_session
            if session is not None and self._mirror_select_active():
                source_vertices = set(_vertices)
                session.set_selection(
                    self._mirrored_vertex_selection(source_vertices))
                self._set_mirror_transform_source(source_vertices)
        resolved = self._resolved_geometry_polys()
        if self._selected_poly not in resolved:
            self._selected_poly = min(resolved, default=None)
        self.viewport.set_selected_polygon(self._selected_poly)
        self.viewport.set_highlight_polys(resolved)
        if source == "vertex":
            active_elements = self._active_fx_elements()
            if len(active_elements) > 1:
                # More than one complete FX is selected: the list cannot name a
                # single one, so it stays on the neutral multi entry.
                self.fx_combo.blockSignals(True)
                self.fx_combo.setCurrentIndex(0)
                self.fx_combo.blockSignals(False)
            else:
                element = active_elements[0] if active_elements else next(
                    (candidate for candidate in self._fx_elements
                     if candidate.owner_path == self.viewport.edit_owner
                     and set(candidate.poly_ids).issubset(resolved)),
                    None)
                self.fx_combo.blockSignals(True)
                self.fx_combo.setCurrentIndex(
                    self._fx_combo_index(element.identity) if element else 0)
                self.fx_combo.blockSignals(False)
        if self._mapping_index is not None:
            self._fill_polygon_inspector(self._selected_poly)
        self._update_repair_buttons()
        self._sync_fx_combo_label()
        self._sync_edit_action_states()

    def _copy_geometry_reason(self) -> str:
        session, owner, fam_obj = self._geometry_context()
        if self.viewport.paste_preview_active:
            return "finish or cancel the active placement preview first"
        if session is None or owner is None:
            return "enable Edit Mode on one model"
        if fam_obj is None or fam_obj is not self._workbench_obj:
            return "the active owner is not the editable model"
        element = self._active_fx_selection()
        if element is not None:
            mixed_reason = self._mixed_fx_selection_reason(element)
            if mixed_reason:
                return mixed_reason
            if element.shared_state == "invalid":
                return (
                    f"{element.fx_name} is structurally invalid")
            try:
                build_fx_elements_clipboard(
                    fam_obj, self._active_fx_elements(),
                    self._family.animations if self._family else {})
            except GeometryClipboardError as exc:
                return str(exc)
            return ""
        if not self._resolved_geometry_polys():
            return "select one or more complete polygons first"
        return ""


    def _preview_geometry_reason(self) -> str:
        clipboard = self._geometry_clipboard
        session, owner, fam_obj = self._geometry_context()
        if clipboard is None:
            return "no copied geometry is available"
        if session is None or owner is None or fam_obj is None:
            return "activate the source model in Edit Mode"
        if owner != clipboard.owner:
            return "the clipboard belongs to a different owner"
        if id(session.model) != clipboard.model_identity:
            return "the clipboard belongs to a different in-memory model"
        if session.modal_active:
            return "finish the current modal transform first"
        return ""

    def can_preview_geometry(self) -> bool:
        return not self._preview_geometry_reason()

    def _commit_geometry_reason(self) -> str:
        reason = self._preview_geometry_reason()
        if reason:
            return reason
        clipboard = self._geometry_clipboard
        session, _owner, fam_obj = self._geometry_context()
        model = getattr(fam_obj, "skeleton", None)
        if model is None or session is None or clipboard is None:
            return "the editable model is no longer available"
        if model.poo2_payload_offset is None \
                or model.pol2_payload_offset is None:
            return "decoded data has no structural POO2/POL2 writer"
        writer_reason = self._geometry_writer_reason(
            self.viewport.edit_owner, fam_obj)
        if writer_reason:
            return writer_reason
        if isinstance(clipboard, FxElementClipboard):
            mapping = MappingIndex(fam_obj)
            if mapping.invalid:
                return "the model contains invalid ATTS polygon references"
            if mapping.duplicates:
                return "the model contains duplicate polygon mappings"
            try:
                if isinstance(clipboard, ImportedFxClipboard):
                    stage_fx_clipboard(self._family, fam_obj, clipboard)
                else:
                    validate_fx_element_clipboard(
                        fam_obj, clipboard,
                        self._family.animations if self._family else {})
            except (GeometryClipboardError, MappingEditError) as exc:
                return str(exc)
            return ""
        if len(model.points) + len(clipboard.points) > 0x10000:
            return "the paste would exceed the uint16 POO2 index limit"
        if len(model.polygons) + len(clipboard.polygons) > 0x8000:
            return "the paste would exceed the signed ATTS polyID limit"
        mapping = MappingIndex(fam_obj)
        if mapping.invalid:
            return "the model contains invalid ATTS polygon references"
        if mapping.duplicates:
            return "the model contains duplicate polygon mappings"
        blocks = fam_obj.base_object.ades
        for copied in clipboard.polygons:
            if copied.block_class_id == "area.class":
                template = copied.block_template
                if template is None \
                        or (template.class_id or "").lower() != "area.class" \
                        or template.ade_strc_chunk_offset < 0 \
                        or not template.source_objt_bytes:
                    return (
                        f"polygon #{copied.source_poly_id} area.class has no "
                        "typed FORM ADE/STRC writer")
                continue
            if not (0 <= copied.block_index < len(blocks)):
                return f"material block #{copied.block_index} no longer exists"
            block = blocks[copied.block_index]
            if (block.class_id or "").lower() != "amesh.class":
                return f"material block #{copied.block_index} is not amesh"
            if len(block.atts) != len(block.olpl):
                return (
                    f"material block #{copied.block_index} has ambiguous "
                    "ATTS/OLPL counts")
            if block.atts_chunk_offset < 0 or block.olpl_chunk_offset < 0:
                return (
                    f"material block #{copied.block_index} has no structural "
                    "ATTS/OLPL writer")
        return ""

    def can_commit_geometry(self) -> bool:
        return not self._commit_geometry_reason()

    def _geometry_writer_reason(self, owner: str | None, fam_obj) -> str:
        family = self._family
        model = getattr(fam_obj, "skeleton", None)
        tree = getattr(getattr(family, "base_asset", None), "tree", None)
        tree_data = getattr(tree, "data", None)
        standalone = (
            self._bundle_base_snapshots.get(owner) if owner is not None
            else None)
        source_data = standalone if standalone is not None else tree_data
        cache_key = (
            owner, id(model), id(getattr(model, "original_data", None)),
            id(source_data),
            len(getattr(model, "points", ())),
            len(getattr(model, "polygons", ())),
            tuple(
                (
                    block.class_id, block.ade_poly_id,
                    tuple(entry.poly_id for entry in block.atts),
                    len(block.olpl),
                )
                for block in getattr(
                    getattr(fam_obj, "base_object", None), "ades", ())),
        )
        cached = self._geometry_writer_capability_cache.get(cache_key)
        if cached is not None:
            return cached
        reason = ""
        try:
            if model is None or not model.original_data:
                raise MappingEditError(
                    "decoded SKLT source bytes are unavailable")
            parsed_sklt = parse_sklt_bytes(
                model.original_data, "<geometry-writer-preflight>")
            if parsed_sklt.poo2_payload_offset is None \
                    or parsed_sklt.pol2_payload_offset is None:
                raise MappingEditError(
                    "decoded SKLT bytes have no structural POO2/POL2 chunks")
            if standalone is None:
                if not isinstance(tree_data, bytes):
                    raise MappingEditError(
                        "decoded BASE source bytes are unavailable")
                standalone = export_base_object_bytes(
                    tree_data, fam_obj.base_object)
            self._bundle_topology_states(standalone, fam_obj)
        except (MappingEditError, SkltParseError, OSError) as exc:
            reason = str(exc)
        self._geometry_writer_capability_cache[cache_key] = reason
        return reason

    def can_save_as_geometry(self) -> bool:
        """Whether a complete BASE + SKLT asset family can be exported."""

        owner = self.viewport.edit_owner or self._selected_owner
        fam_obj = self._owner_to_obj.get(owner) if owner else None
        family = self._family
        basic = bool(
            owner is not None and fam_obj is not None
            and getattr(fam_obj, "skeleton", None) is not None
            and getattr(getattr(family, "base_asset", None), "tree", None)
            is not None
            and not self.viewport.paste_preview_active)
        return bool(
            basic and not self._geometry_writer_reason(owner, fam_obj))

    def _sklt_save_context(self, *, notify: bool = False):
        """Return the selected writable skeleton without requiring BASE data."""

        owner = self.viewport.edit_owner or self._selected_owner
        family = self._family
        fam_obj = self._owner_to_obj.get(owner) if owner else None
        model = getattr(fam_obj, "skeleton", None)
        if owner is None or family is None or fam_obj is None or model is None:
            if notify:
                QMessageBox.information(
                    self, "Export unavailable",
                    "Select a decoded SKLT model first.")
            return None
        if not getattr(model, "original_data", None):
            if notify:
                QMessageBox.information(
                    self, "Export unavailable",
                    "The selected model has no source SKLT bytes to rewrite.")
            return None
        return owner, family, fam_obj, model

    def can_export_sklt(self) -> bool:
        context = self._sklt_save_context()
        return bool(
            context is not None and not self.viewport.paste_preview_active)

    def _standalone_sklt_source(self, fam_obj) -> Path | None:
        """Loose source used only by a manual SKLT-only editing session."""

        family = self._family
        if family is None or getattr(family, "base_asset", None) is not None:
            return None
        ref = getattr(fam_obj, "skeleton_ref", None)
        path = getattr(ref, "path", None)
        if path is None:
            return None
        source = Path(path)
        return source if (
            source.suffix.casefold() in (".sklt", ".skl")
            and source.is_file()
        ) else None

    def _loose_base_targets(self, fam_obj) -> tuple[Path, Path] | None:
        """Skeleton and BASE files of a loose BASE opened from disk (not SET.BAS).

        Overwrite replaces exactly these two files (with .bak copies when the Backup
        option is enabled), so a model opened from the game folder can be saved
        without exporting a new package.
        """

        family = self._family
        if family is None or family.base_path is None or fam_obj is None:
            return None
        base = Path(family.base_path)
        if (base.suffix.casefold() not in (".base", ".bas") or not base.is_file()
                or _is_setbas_family(family)):
            return None
        ref = getattr(fam_obj, "skeleton_ref", None)
        skeleton = Path(ref.path) if getattr(ref, "path", None) else None
        if skeleton is None or not skeleton.is_file():
            return None
        return skeleton, base

    def can_overwrite_geometry(self) -> bool:
        owner = self.viewport.edit_owner or self._selected_owner
        fam_obj = self._owner_to_obj.get(owner) if owner else None
        if self.can_save_as_geometry() and owner in self._bundle_targets:
            return True
        if self.can_save_as_geometry() and self._loose_base_targets(fam_obj):
            return True
        return bool(
            self.can_export_sklt() and fam_obj is not None
            and self._standalone_sklt_source(fam_obj) is not None)

    def _is_embedded_geometry_source(self, fam_obj) -> bool:
        # A skeleton may legitimately have no loose path because it was
        # decoded from a standalone BASE/self-provider. That model is still
        # editable through the existing verified BASE + SKLT export path.
        # Read-only follows the BASE entry point, never a stale ResolvedFile.
        return bool(fam_obj is not None and
                    self._family_entry_is_setbas_archive())

    def _current_viewport_position(self):
        cursor = self.viewport.mapFromGlobal(QCursor.pos())
        return (cursor if self.viewport.rect().contains(cursor)
                else self.viewport.rect().center())

    def _paste_geometry(self, position=None) -> None:
        if not self._require_editing("Paste Geometry"):
            return
        if self.viewport.paste_preview_active:
            self._confirm_paste_geometry()
            return
        reason = self._preview_geometry_reason()
        if reason:
            self._notify(f"Paste refused: {reason}.", 8000)
            return
        if position is None or not hasattr(position, "x"):
            position = self._current_viewport_position()
        if self.viewport.begin_paste_preview(
                self._geometry_clipboard, position, operation="copy"):
            self.viewport.setFocus()
            label = (
                "Paste FX Preview"
                if isinstance(self._geometry_clipboard, FxElementClipboard)
                else "Copy Preview")
            self._notify(
                f"{label}: move the transparent copy; Paste/LMB/Enter "
                "confirms; RMB/Esc cancels.", 12000)

    def _remap_uv_tracking(self, owner, before, polygon_remap=None):
        mapping = MappingIndex(self._owner_to_obj[owner])
        self._uv_original = {key: value for key, value in self._uv_original.items() if key[0] != owner}
        for key, original in before.get("uv_original", {}).items():
            block = before["blocks"][key[1]]
            poly = block.atts[key[2]].poly_id
            if polygon_remap is not None:
                poly = polygon_remap.get(poly)
            refs = mapping.refs.get(poly, [])
            if len(refs) == 1:
                ref = refs[0]
                # A new animated material takes its UVs from VANM, so an old
                # fixed OLPL edit no longer belongs to the current mapping.
                if ref.atts_index >= len(ref.block.olpl):
                    continue
                self._uv_original[(owner, ref.block_index, ref.atts_index)] = copy.deepcopy(original)

    def _capture_topology_state(self, owner: str) -> dict | None:
        fam_obj = self._owner_to_obj.get(owner)
        model = getattr(fam_obj, "skeleton", None)
        if model is None:
            return None
        session = self.viewport.edit_session
        selected_vertices = (
            tuple(sorted(session.selection))
            if session is not None and self.viewport.edit_owner == owner
            else ())
        return {
            "uv_original": {key: copy.deepcopy(value) for key, value in self._uv_original.items() if key[0] == owner},
            "fx_aliases": {key: value for key, value in self._fx_aliases.items() if key[0] == owner},
            "points": [tuple(point) for point in model.points],
            "polygons": [list(polygon) for polygon in model.polygons],
            "parsed_polygon_count": model.parsed_polygon_count,
            "blocks": copy.deepcopy(fam_obj.base_object.ades),
            "selected_polys": set(self._selected_polys),
            "selected_poly": self._selected_poly,
            "selected_vertices": selected_vertices,
            "edit_active": session is not None,
        }

    def _refresh_object_material_faces(self, fam_obj) -> None:
        model = getattr(fam_obj, "skeleton", None)
        if model is None:
            return
        blocks = fam_obj.base_object.ades
        if self._family is not None and (
                len(fam_obj.materials) != len(blocks)
                or any(
                    group.block is not block
                    for group, block in zip(fam_obj.materials, blocks))):
            rebuild_materials(fam_obj, self._family)
        for group in fam_obj.materials:
            block = group.block
            if block is None:
                continue
            group.faces[:] = [
                (
                    entry.poly_id,
                    list(block.olpl[index])
                    if index < len(block.olpl) else [],
                    entry.shade_val,
                )
                for index, entry in enumerate(block.atts)
                if 0 <= entry.poly_id < len(model.polygons)
            ]

    @staticmethod
    def _topology_content(state: dict | None):
        if state is None:
            return None
        return (
            state["points"],
            state["polygons"],
            state["blocks"],
        )

    def _restore_topology_state(self, owner: str, state: dict) -> bool:
        fam_obj = self._owner_to_obj.get(owner)
        model = getattr(fam_obj, "skeleton", None)
        blocks = getattr(getattr(fam_obj, "base_object", None), "ades", [])
        if model is None:
            return False
        if self.viewport.paste_preview_active:
            self.viewport.cancel_paste_preview()
        keep_global_edit = self.edit_toggle_action.isChecked()
        if self.viewport.is_edit_mode:
            self.viewport.blockSignals(True)
            self.viewport.exit_edit_mode()
            self.viewport.blockSignals(False)
        model.points[:] = [tuple(point) for point in state["points"]]
        model.polygons[:] = [list(polygon) for polygon in state["polygons"]]
        model.parsed_polygon_count = state["parsed_polygon_count"]
        self._fx_aliases = {key: value for key, value in self._fx_aliases.items() if key[0] != owner}
        self._fx_aliases.update(state.get("fx_aliases", {}))
        self._uv_original = {key: value for key, value in self._uv_original.items() if key[0] != owner}
        self._uv_original.update(copy.deepcopy(state.get("uv_original", {})))
        saved_blocks = copy.deepcopy(state["blocks"])
        if len(blocks) == len(saved_blocks) and all(
                (current.class_id or "").lower()
                == (saved.class_id or "").lower()
                for current, saved in zip(blocks, saved_blocks)):
            for current, saved in zip(blocks, saved_blocks):
                current.__dict__.clear()
                current.__dict__.update(copy.deepcopy(saved.__dict__))
        else:
            blocks[:] = saved_blocks
        self._refresh_object_material_faces(fam_obj)
        self._selected_owner = owner
        self._selected_polys = set(state["selected_polys"])
        self._selected_poly = state["selected_poly"]
        self.viewport.refresh_family_materials()
        self._rebuild_workbench(self._family, owner)
        self._refresh_fx_elements()
        self.viewport.set_selected_owner(owner)
        self.viewport.set_selected_polygon(self._selected_poly)
        self.viewport.set_highlight_polys(self._selected_polys)
        self._sync_poly_id_control()
        if keep_global_edit and not self.edit_toggle_action.isChecked():
            self.edit_toggle_action.blockSignals(True)
            self.edit_toggle_action.setChecked(True)
            self.edit_toggle_action.blockSignals(False)
        if state["edit_active"] and keep_global_edit:
            selected_vertices = state["selected_vertices"]
            if selected_vertices:
                self.viewport.enter_edit_mode_with_vertices(
                    owner, selected_vertices, pick_polygons=True)
            else:
                self.viewport.enter_edit_mode(owner)
                self.viewport.configure_edit_interaction(
                    selected_only=False, pick_polygons=True)
            session = self.viewport.edit_session
            if session is not None:
                session._undo.clear()
                session._redo.clear()
        return True

    @staticmethod
    def _clipboard_source_polygon_ids(clipboard) -> set[int]:
        source = getattr(clipboard, "source_poly_ids", None)
        if source is not None:
            return {int(poly_id) for poly_id in source}
        return {
            int(copied.source_poly_id)
            for copied in getattr(clipboard, "polygons", ())}

    @staticmethod
    def _clipboard_source_vertex_ids(clipboard) -> tuple[int, ...]:
        return tuple(sorted(
            int(original)
            for original, _local in getattr(
                clipboard, "original_to_local", ())))

    def _confirm_move_placement(self, clipboard, delta) -> None:
        """Commit Move using the exact same preview/controller as Copy."""

        owner = self.viewport.edit_owner
        fam_obj = self._owner_to_obj.get(owner) if owner else None
        model = getattr(fam_obj, "skeleton", None)
        session = self.viewport.edit_session
        if clipboard is None or delta is None or fam_obj is None \
                or model is None or session is None:
            self._notify("Move refused: the target context changed.", 8000)
            return
        if owner != getattr(clipboard, "owner", None) \
                or id(model) != getattr(clipboard, "model_identity", None):
            self._notify("Move refused: the source model changed.", 8000)
            return
        if not all(math.isfinite(float(value)) for value in delta):
            self._notify("Move refused: invalid movement delta.", 8000)
            return

        pairs = tuple(getattr(clipboard, "original_to_local", ()))
        points = tuple(getattr(clipboard, "points", ()))
        if not pairs:
            self._notify("Move refused: no source vertices are available.", 8000)
            return
        before = [tuple(point) for point in model.points]
        previous_selected_owner = self._selected_owner
        previous_selected_polys = set(self._selected_polys)
        previous_selected_poly = self._selected_poly
        try:
            self._remember_geometry_original(owner)
            for original, local in pairs:
                if not (0 <= original < len(model.points)
                        and 0 <= local < len(points)):
                    raise GeometryClipboardError(
                        "the Move source vertex mapping is no longer valid")
                source = tuple(points[local])
                # A placement preview never edits live geometry.  If another
                # path changed the source underneath it, fail closed instead
                # of moving unrelated data.
                if tuple(model.points[original]) != source:
                    raise GeometryClipboardError(
                        "the Move source geometry changed during preview")
                model.points[original] = tuple(
                    source[axis] + delta[axis] for axis in range(3))

            moved_polys = self._clipboard_source_polygon_ids(clipboard)
            moved_vertices = self._clipboard_source_vertex_ids(clipboard)
            self.viewport.finish_paste_preview()
            self._refresh_object_material_faces(fam_obj)
            self._selected_owner = owner
            self._selected_polys = moved_polys
            self._selected_poly = min(moved_polys, default=None)
            # Same-owner geometry refresh must preserve the exact viewport
            # framing.  Only the selected vertices change coordinates.
            self.viewport.refresh_family_materials()
            self._rebuild_workbench(self._family, owner)
            self._refresh_fx_elements()
            self.viewport.set_selected_owner(owner)
            self.viewport.set_selected_polygon(self._selected_poly)
            self.viewport.set_highlight_polys(moved_polys)
            if moved_vertices:
                self.viewport.enter_edit_mode_with_vertices(
                    owner, moved_vertices, pick_polygons=True)
            self._on_geometry_edited(owner)
            after = [tuple(point) for point in model.points]
            self._record_edit_command({
                "kind": "geometry",
                "owner": owner,
                "before": before,
                "after": after,
                "label": "Move Geometry",
            })
            self._fill_polygon_inspector(self._selected_poly)
            self._sync_editor_context()
            self._notify(
                f"Moved {len(moved_polys)} polygon(s) with "
                f"{len(moved_vertices)} source vertex/vertices.", 8000)
        except Exception as exc:
            model.points[:] = before
            self._selected_owner = previous_selected_owner
            self._selected_polys = previous_selected_polys
            self._selected_poly = previous_selected_poly
            self.viewport.cancel_paste_preview()
            try:
                self._refresh_object_material_faces(fam_obj)
                self.viewport.refresh_family_materials()
                self._rebuild_workbench(self._family, owner)
                self._refresh_fx_elements()
                self.viewport.set_selected_owner(previous_selected_owner)
                self.viewport.set_selected_polygon(previous_selected_poly)
                self.viewport.set_highlight_polys(previous_selected_polys)
                self._sync_editor_context()
            except Exception as refresh_exc:
                self._log(f"Move rollback UI refresh failed: {refresh_exc}")
            self._notify(f"Move failed and was rolled back: {exc}.", 10000)

    def _confirm_paste_geometry(self) -> None:
        operation = self.viewport.paste_preview_operation or "copy"
        action_label = "Move Geometry" if operation == "move" else "Paste Geometry"
        if not self._require_editing(action_label):
            if self.viewport.paste_preview_active:
                self.viewport.cancel_paste_preview()
            return
        clipboard = (
            self.viewport.paste_preview_clipboard
            if self.viewport.paste_preview_active
            else self._geometry_clipboard)
        delta = self.viewport.paste_preview_delta
        if operation == "move":
            self._confirm_move_placement(clipboard, delta)
            return
        owner = self.viewport.edit_owner
        fam_obj = self._owner_to_obj.get(owner) if owner else None
        model = getattr(fam_obj, "skeleton", None)
        reason = self._commit_geometry_reason()
        if clipboard is None or delta is None or fam_obj is None \
                or model is None or reason:
            self._notify(
                f"Paste refused: {reason or 'the target context changed'}.",
                8000)
            return
        before = self._capture_topology_state(owner)
        if before is None:
            return
        had_topology_original = owner in self._topology_original
        had_geom_original = owner in self._geom_original
        previous_geom_original = copy.deepcopy(
            self._geom_original.get(owner))
        had_dirty = owner in self._geom_dirty
        previous_dirty = self._geom_dirty.get(owner)
        previous_undo = list(self._edit_undo_stack)
        previous_redo = list(self._edit_redo_stack)
        previous_selected_owner = self._selected_owner
        previous_selected_polys = set(self._selected_polys)
        previous_selected_poly = self._selected_poly
        try:
            self._topology_original.setdefault(owner, copy.deepcopy(before))
            self._remember_geometry_original(owner)
            resources_before = self._capture_family_resource_state(self._family)
            if isinstance(clipboard, ImportedFxClipboard):
                staged_family, staged, result = stage_fx_clipboard(
                    self._family, fam_obj, clipboard, delta)
                model.points[:] = staged.skeleton.points
                model.polygons[:] = staged.skeleton.polygons
                model.parsed_polygon_count = staged.skeleton.parsed_polygon_count
                fam_obj.base_object.ades[:] = staged.base_object.ades
                self._restore_family_resource_state(
                    self._family, self._capture_family_resource_state(staged_family))
            elif isinstance(clipboard, FxElementClipboard):
                result = append_fx_element_clipboard(
                    fam_obj, clipboard, delta,
                    self._family.animations if self._family else {})
            else:
                result = append_geometry_clipboard(
                    model, fam_obj.base_object.ades, clipboard, delta)

            self._refresh_object_material_faces(fam_obj)
            self.viewport.finish_paste_preview()
            self._selected_owner = owner
            self._selected_polys = set(result.polygon_indices)
            self._selected_poly = (
                result.polygon_indices[0]
                if result.polygon_indices else None)
            self.viewport.refresh_family_materials()
            self._rebuild_workbench(self._family, owner)
            self._refresh_fx_elements()
            if isinstance(clipboard, FxElementClipboard):
                pasted_ids = set(result.polygon_indices)
                pasted_fx = next(
                    (element for element in self._fx_elements
                     if set(element.poly_ids) == pasted_ids
                     and element.fx_name == clipboard.fx_name
                     and element.source_kind == clipboard.source_kind),
                    None)
                if pasted_fx is not None:
                    self.fx_combo.blockSignals(True)
                    self.fx_combo.setCurrentIndex(
                        self._fx_combo_index(pasted_fx.identity))
                    self.fx_combo.blockSignals(False)
            self.viewport.set_selected_owner(owner)
            self.viewport.set_selected_polygon(self._selected_poly)
            self.viewport.set_highlight_polys(self._selected_polys)
            self.viewport.enter_edit_mode_with_vertices(
                owner, result.point_indices, pick_polygons=True)
            session = self.viewport.edit_session
            if session is not None:
                session._undo.clear()
                session._redo.clear()
            self._on_geometry_edited(owner)
            after = self._capture_topology_state(owner)
            if after is None:
                raise GeometryClipboardError(
                    "the committed topology could not be verified")
            self._record_edit_command({
                "kind": "material_topology" if isinstance(clipboard, ImportedFxClipboard) else "topology",
                "owner": owner,
                "before": before,
                "after": after,
                "resource_before": resources_before,
                "resource_after": self._capture_family_resource_state(self._family),
                "label": (
                    f"Paste {clipboard.fx_name} FX"
                    if isinstance(clipboard, FxElementClipboard)
                    else "Paste Geometry"),
            })
            self._fill_polygon_inspector(self._selected_poly)
            self._sync_editor_context()
            pasted_label = (
                f"Pasted {clipboard.fx_name} "
                f"{'VANM ' if clipboard.source_kind == 'VANM' else ''}"
                "FX Element"
                if isinstance(clipboard, FxElementClipboard)
                else f"Pasted {len(result.polygon_indices)} polygon(s)")
            self._notify(
                f"{pasted_label} with "
                f"{len(result.point_indices)} independent vertex/vertices."
                + (
                    " Embedded source: use Export Asset Family to export the "
                    "edited model."
                    if self._is_embedded_geometry_source(fam_obj) else ""
                ),
                10000)
        except Exception as exc:
            if isinstance(clipboard, ImportedFxClipboard) and 'resources_before' in locals():
                self._restore_family_resource_state(self._family, resources_before)
            try:
                rolled_back = self._restore_topology_state(owner, before)
            except Exception as rollback_exc:
                rolled_back = (
                    self._topology_content(
                        self._capture_topology_state(owner))
                    == self._topology_content(before))
                self._log(
                    "Paste rollback UI refresh failed after data restore: "
                    f"{rollback_exc}")
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
            self._selected_owner = previous_selected_owner
            self._selected_polys = previous_selected_polys
            self._selected_poly = previous_selected_poly
            self.viewport.cancel_paste_preview()
            try:
                self._refresh_object_material_faces(fam_obj)
                self.viewport.refresh_family_materials()
                self._rebuild_workbench(self._family, owner)
                self._refresh_fx_elements()
                self.viewport.set_selected_owner(previous_selected_owner)
                self.viewport.set_selected_polygon(previous_selected_poly)
                self.viewport.set_highlight_polys(previous_selected_polys)
                self._sync_editor_context()
            except Exception as refresh_exc:
                self._log(
                    "Paste rollback UI refresh failed: "
                    f"{refresh_exc}")
            self._notify(
                "Paste Geometry failed and "
                f"{'was rolled back' if rolled_back else 'rollback failed'}: "
                f"{exc}.", 9000)

    def _delete_geometry_plan(self):
        reason = self._delete_geometry_reason()
        if reason:
            raise GeometryClipboardError(reason)
        owner = self.viewport.edit_owner
        fam_obj = self._owner_to_obj[owner]
        element = self._active_fx_selection()
        selected = self._resolved_geometry_polys()
        mapping = MappingIndex(fam_obj)
        return plan_delete_geometry(
            fam_obj, selected, mapping,
            self._unsafe_fx_geometry_polys(owner, selected), allow_empty=bool(element))

    def _delete_geometry_reason(self) -> str:
        if not self._editing_allowed():
            return "enable Edit Mode on one model"
        session, owner, fam_obj = self._geometry_context()
        if self.viewport.paste_preview_active:
            return "cancel the active placement preview first"
        if session is None or owner is None:
            return "enable Edit Mode on one model"
        if fam_obj is None or fam_obj is not self._workbench_obj:
            return "the active owner is not the editable model"
        element = self._active_fx_selection()
        mixed_reason = self._mixed_fx_selection_reason(element)
        if mixed_reason:
            return mixed_reason
        if element is not None and element.shared_state == "invalid":
            return (
                f"{element.fx_name} is structurally invalid")
        selected = self._resolved_geometry_polys()
        if not selected:
            return "select one or more complete polygons first"
        if any(key[0] == owner for key in self._texture_original):
            return (
                "save or reset pending material previews before changing "
                "topology")
        try:
            mapping = MappingIndex(fam_obj)
            plan_delete_geometry(
                fam_obj, selected, mapping,
                self._unsafe_fx_geometry_polys(
                    owner, selected), allow_empty=bool(element))
        except GeometryClipboardError as exc:
            return str(exc)
        return ""

    def can_delete_geometry(self) -> bool:
        return not self._delete_geometry_reason()

    def _apply_delete_plan(self, owner: str, fam_obj, plan,
                           *, label: str,
                           preserve_polygons=(),
                           preserve_vertices=()) -> int:
        model = fam_obj.skeleton
        before = self._capture_topology_state(owner)
        if before is None:
            raise GeometryClipboardError(
                "the topology snapshot could not be created")
        had_topology_original = owner in self._topology_original
        previous_geom_original = self._geom_original.get(owner)
        previous_dirty = self._geom_dirty.get(owner)
        try:
            self._topology_original.setdefault(owner, copy.deepcopy(before))
            self._remember_geometry_original(owner)
            result = apply_delete_geometry(
                model, fam_obj.base_object.ades, plan)
            polygon_remap = dict(result.old_to_new_polygon)
            self._remap_uv_tracking(owner, before, polygon_remap)
            self._fx_aliases = {
                ((key[0], tuple(polygon_remap[p] for p in key[1])) if key[0] == owner else key): value
                for key, value in self._fx_aliases.items()
                if key[0] != owner or all(p in polygon_remap for p in key[1])}
            point_remap = dict(result.old_to_new_point)
            remapped_polygons = {
                polygon_remap[poly_id]
                for poly_id in preserve_polygons
                if poly_id in polygon_remap}
            remapped_vertices = {
                point_remap[point_id]
                for point_id in preserve_vertices
                if point_id in point_remap}
            active_session = self.viewport.edit_session
            if active_session is not None:
                active_session.select_none()
            self._refresh_object_material_faces(fam_obj)
            self._selected_owner = owner
            self._selected_polys = remapped_polygons
            self._selected_poly = min(
                remapped_polygons, default=None)
            self.viewport.refresh_family_materials()
            self._rebuild_workbench(self._family, owner)
            self._refresh_fx_elements()
            self.viewport.set_selected_owner(owner)
            self.viewport.set_selected_polygon(self._selected_poly)
            self.viewport.set_highlight_polys(remapped_polygons)
            if remapped_vertices:
                self.viewport.enter_edit_mode_with_vertices(
                    owner, remapped_vertices, pick_polygons=True)
            else:
                self.viewport.enter_edit_mode(owner)
                self.viewport.configure_edit_interaction(
                    selected_only=True, pick_polygons=True)
            session = self.viewport.edit_session
            if session is not None:
                if remapped_vertices:
                    session.set_selection(remapped_vertices)
                else:
                    session.select_none()
                session._undo.clear()
                session._redo.clear()
            self._on_geometry_edited(owner)
            after = self._capture_topology_state(owner)
            if after is None:
                raise GeometryClipboardError(
                    "the committed topology could not be verified")
            self._fill_polygon_inspector(self._selected_poly)
            self._sync_editor_context()
            self._record_edit_command({
                "kind": "topology",
                "owner": owner,
                "before": before,
                "after": after,
                "label": label,
            })
            return len(result.deleted_polygon_indices)
        except Exception as exc:
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
            if isinstance(exc, GeometryClipboardError):
                raise
            raise GeometryClipboardError(
                f"the topology commit failed and was rolled back: {exc}") \
                from exc

    def _delete_geometry(self) -> None:
        if not self._require_editing("Delete Geometry"):
            return
        element = self._active_fx_selection()
        fx_elements = self._active_fx_elements()
        try:
            plan = self._delete_geometry_plan()
        except GeometryClipboardError as exc:
            self._notify(f"Delete refused: {exc}.", 8000)
            return
        count = len(plan.deleted_polygon_indices)
        question = (
            f"Delete {len(fx_elements)} complete FX elements "
            f"({count} polygon(s))?\n"
            if len(fx_elements) > 1 else
            f"Delete the complete {element.fx_name} "
            f"{'VANM ' if element.source_kind == 'VANM' else ''}"
            f"FX element ({count} polygon(s))?\n"
            if element is not None else
            f"Delete {count} selected polygon(s)?\n"
        ) + "This operation can be undone."
        answer = QMessageBox.question(
            self,
            f"Delete {element.fx_name} FX?"
            if element is not None else "Delete geometry?",
            question,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            self._notify("Delete cancelled.", 3000)
            return
        owner = self.viewport.edit_owner
        fam_obj = self._owner_to_obj[owner]
        try:
            deleted = self._apply_delete_plan(
                owner, fam_obj, plan,
                label=(
                    "Delete FX elements"
                    if len(fx_elements) > 1 else
                    f"Delete {element.fx_name} FX"
                    if element is not None else "Delete Geometry"))
        except GeometryClipboardError as exc:
            self._notify(f"Delete refused: {exc}.", 8000)
            return
        deleted_message = (
            f"Deleted {len(fx_elements)} complete FX elements. "
            if len(fx_elements) > 1 else
            f"Deleted complete {element.fx_name} FX element. "
            if element is not None else
            f"Deleted {deleted} selected polygon(s). "
        ) + "This operation can be undone."
        self._notify(deleted_message, 8000)

    def _cut_geometry(self) -> None:
        """Cut the selection: clipboard copy plus one undoable removal."""

        if not self._require_editing("Cut Geometry"):
            return
        if self.viewport.paste_preview_active:
            self.viewport.cancel_paste_preview()
        reason = self._copy_geometry_reason()
        if reason:
            self._notify(f"Cut refused: {reason}.", 7000)
            return
        try:
            plan = self._delete_geometry_plan()
        except GeometryClipboardError as exc:
            self._notify(f"Cut refused: {exc}.", 9000)
            return
        try:
            clipboard, descriptor = self._build_geometry_placement_clipboard()
        except GeometryClipboardError as exc:
            self._notify(f"Cut refused: {exc}.", 9000)
            return
        owner = self.viewport.edit_owner
        fam_obj = self._owner_to_obj.get(owner) if owner else None
        if fam_obj is None:
            self._notify(
                "Cut refused: the editable model is unavailable.", 7000)
            return
        try:
            self._apply_delete_plan(owner, fam_obj, plan, label="Cut Geometry")
        except GeometryClipboardError as exc:
            self._notify(f"Cut refused: {exc}.", 9000)
            return
        # Store the clipboard only after the removal succeeded: a failed cut
        # keeps the previous clipboard and leaves the geometry untouched.
        self._geometry_clipboard = clipboard
        self._sync_edit_action_states()
        self._notify(
            f"Cut {descriptor}. The copy is ready for Paste.", 8000)

    def _center_model_at_origin(self) -> None:
        if not self._require_editing("Center Model at Origin"):
            return
        if self.viewport.paste_preview_active:
            self._notify(
                "Center refused: finish or cancel the active placement first.",
                7000)
            return
        owner = self.viewport.edit_owner
        fam_obj = self._owner_to_obj.get(owner) if owner else None
        if not self.viewport.center_edit_model_at_origin():
            self._notify("The model is already centered at the origin.", 4500)
            return
        children = bool(fam_obj is not None and fam_obj.kids)
        self._notify(
            "Model centered at the origin. Undo restores it."
            + (" Child BASE objects keep their own offset." if children
               else ""), 7000)

    def _move_selected_geometry(self) -> None:
        if not self._require_editing("Move"):
            return
        if self.viewport.paste_preview_active:
            self._notify(
                "Move refused: finish or cancel the active placement first.",
                7000)
            return
        session = self.viewport.edit_session
        if session is None or session.modal_active:
            return
        reason = self._copy_geometry_reason()
        if reason:
            self._notify(f"Move refused: {reason}.", 7000)
            return
        try:
            clipboard, descriptor = self._build_geometry_placement_clipboard()
        except GeometryClipboardError as exc:
            self._notify(f"Move refused: {exc}.", 9000)
            return
        self._start_geometry_placement(
            "move", clipboard, descriptor, store_copy=False)

    def _scale_selected_geometry(self) -> None:
        if not self._require_editing("Scale"):
            return
        if self._family is None:
            self._notify("Scale: load a model first.")
            return
        if self._live_rotate_dialog is not None \
                and self._live_rotate_dialog.isVisible():
            self._live_rotate_dialog.raise_()
            self._live_rotate_dialog.activateWindow()
            self._notify("Finish the active Rotate preview first.")
            return
        if self._live_scale_dialog is not None \
                and self._live_scale_dialog.isVisible():
            self._live_scale_dialog.raise_()
            self._live_scale_dialog.activateWindow()
            return
        if not self.viewport.is_edit_mode:
            self._notify("Scale: the current model is not in Edit Mode.")
            return
        if not self.viewport.begin_scale_preview(select_all_if_empty=True):
            self._notify("Scale: no editable vertices are available.")
            return
        dialog = LiveScaleDialog(self)
        self._live_scale_dialog = dialog
        dialog.factorChanged.connect(self.viewport.update_scale_preview)
        def finish(accepted: bool) -> None:
            if self._live_scale_dialog is not dialog:
                return
            if accepted:
                self.viewport.update_scale_preview(dialog.factor())
            changed = self.viewport.finish_scale_preview(accepted)
            self._live_scale_dialog = None
            self._notify(
                f"Scale applied at {dialog.factor():.2f}x."
                if changed else
                "Scale cancelled." if not accepted else
                "Scale unchanged.", 4500)
            self.viewport.setFocus()

        dialog.accepted.connect(lambda: finish(True))
        dialog.rejected.connect(lambda: finish(False))
        dialog.show()

    def _rotate_selected_geometry(self) -> None:
        if not self._require_editing("Rotate"):
            return
        if self._family is None:
            self._notify("Rotate: load a model first.")
            return
        if self._live_scale_dialog is not None \
                and self._live_scale_dialog.isVisible():
            self._live_scale_dialog.raise_()
            self._live_scale_dialog.activateWindow()
            self._notify("Finish the active Scale preview first.")
            return
        if self._live_rotate_dialog is not None \
                and self._live_rotate_dialog.isVisible():
            self._live_rotate_dialog.raise_()
            self._live_rotate_dialog.activateWindow()
            return
        if not self.viewport.is_edit_mode:
            self._notify("Rotate: the current model is not in Edit Mode.")
            return
        if not self.viewport.begin_rotate_preview(select_all_if_empty=True):
            self._notify("Rotate: no editable vertices are available.")
            return
        dialog = LiveRotateDialog(self)
        self._live_rotate_dialog = dialog
        dialog.rotationChanged.connect(
            self.viewport.update_rotate_preview)

        def finish(accepted: bool) -> None:
            if self._live_rotate_dialog is not dialog:
                return
            if accepted:
                self.viewport.update_rotate_preview(
                    dialog.axis(), dialog.angle_degrees())
            changed = self.viewport.finish_rotate_preview(accepted)
            self._live_rotate_dialog = None
            self._notify(
                f"Rotate applied: {dialog.angle_degrees():+.2f}° around "
                f"{dialog.axis()}."
                if changed else
                "Rotate cancelled." if not accepted else
                "Rotate unchanged.", 4500)
            self.viewport.setFocus()

        dialog.accepted.connect(lambda: finish(True))
        dialog.rejected.connect(lambda: finish(False))
        dialog.show()

    def _cancel_live_scale(self) -> None:
        """Cancel a non-modal scale preview before its model disappears."""

        dialog = self._live_scale_dialog
        if dialog is None:
            return
        if dialog.isVisible():
            dialog.reject()
        else:
            self.viewport.finish_scale_preview(False)
            self._live_scale_dialog = None

    def _cancel_live_rotate(self) -> None:
        """Cancel a non-modal rotation preview before its model disappears."""

        dialog = self._live_rotate_dialog
        if dialog is None:
            return
        if dialog.isVisible():
            dialog.reject()
        else:
            self.viewport.finish_rotate_preview(False)
            self._live_rotate_dialog = None

    def _cancel_live_transforms(self) -> None:
        self._cancel_live_scale()
        self._cancel_live_rotate()

    def _create_viewport_context_menu(self, position=None) -> QMenu:
        menu = QMenu(self.viewport)
        session = self.viewport.edit_session
        active = session is not None
        mode_text = "View Mode" if active else "Edit Mode"
        menu.addAction(
            mode_text,
            lambda: self.edit_toggle_action.setChecked(
                not self.edit_toggle_action.isChecked()))
        menu.addSeparator()
        editing = self._editing_allowed()
        undo = menu.addAction("Undo", self._undo_edit)
        undo.setEnabled(editing and bool(self._edit_undo_stack))
        redo = menu.addAction("Redo", self._redo_edit)
        redo.setEnabled(editing and bool(self._edit_redo_stack))
        reset = menu.addAction("Reset Model", self._reset_model)
        reset.setEnabled(getattr(self, "edit_reset_action", None) is not None
                         and self.edit_reset_action.isEnabled())
        menu.addSeparator()
        select_all = menu.addAction(
            "Select All Geometry", self.viewport.select_all_edit_vertices)
        select_all.setEnabled(active)
        menu.addAction(self.select_all_fx_action)
        select_none = menu.addAction(
            "Deselect All Geometry", self.viewport.select_no_edit_vertices)
        select_none.setEnabled(active)
        deselect = menu.addAction(
            "Deselect",
            lambda: self.viewport.deselect_at(position)
            if position is not None else
            self.viewport.select_no_edit_vertices())
        has_selection = bool(self._selected_polys)
        if session is not None:
            has_selection = has_selection or bool(session.selection)
        has_selection = (
            has_selection or self._active_fx_selection() is not None)
        deselect.setEnabled(has_selection)
        menu.addSeparator()
        active_fx = self._active_fx_selection()
        if active_fx is not None:
            menu.addAction(self.clone_fx_action)
        else:
            menu.addAction(self.copy_geometry_action)
            if not isinstance(self._geometry_clipboard, FxElementClipboard):
                menu.addAction(self.paste_geometry_action)
            menu.addAction(self.cut_geometry_action)
        if active_fx is not None:
            active_fx_elements = self._active_fx_elements()
            delete_geometry = menu.addAction(
                "Delete FX Elements"
                if len(active_fx_elements) > 1 else
                "Delete FX Element",
                self._delete_geometry
                if len(active_fx_elements) > 1 else
                self._delete_selected_fx_element)
            delete_geometry.setEnabled(self.can_delete_geometry())
        else:
            delete_geometry = menu.addAction("Delete", self._delete_geometry)
            delete_geometry.setEnabled(self.can_delete_geometry())
        menu.addAction(self.add_fx_action)
        if active_fx is not None:
            menu.addAction(self.rename_fx_action)
        menu.addAction(self.edit_move_action)
        scale = menu.addAction(
            "Scale", self._scale_selected_geometry)
        scale.setEnabled(editing and self._family is not None)
        rotate = menu.addAction(
            "Rotate", self._rotate_selected_geometry)
        rotate.setEnabled(editing and self._family is not None)
        menu.addSeparator()
        reset_camera = menu.addAction(
            "Reset Camera", self._reset_view_and_gizmo)
        reset_camera.setEnabled(self.viewport.can_reset_camera)
        return menu

    def _show_viewport_context_menu(self, position) -> None:
        if self.viewport.consume_context_menu_suppression():
            return
        if self.viewport.paste_preview_active:
            self.viewport.cancel_paste_preview()
            return
        menu = self._create_viewport_context_menu(position)
        menu.exec(self.viewport.mapToGlobal(position))

    def _remember_geometry_original(self, owner: str | None) -> None:
        if owner is None or owner in self._geom_original:
            return
        fam_obj = self._owner_to_obj.get(owner)
        model = getattr(fam_obj, "skeleton", None)
        if model is not None:
            self._geom_original[owner] = list(model.points)

    def _show_model_editor(self) -> None:
        if self._selected_model_is_archive_read_only():
            self._notify(
                self._archive_read_only_message(), 9000)
        if hasattr(self, "_right_tabs") and hasattr(self, "_editor_tabs"):
            self._right_tabs.setCurrentWidget(self._editor_tabs)
            self._editor_tabs.setCurrentWidget(self._model_editor_panel)

    def _show_mapping_repair(self) -> None:
        """Open the existing repair panel as one shared-state tool window."""

        self._show_model_editor()
        if self._mapping_dialog is None:
            dialog = QDialog(self)
            dialog.setWindowTitle("Mapping Repair")
            dialog.resize(520, 680)
            layout = QVBoxLayout(dialog)
            layout.setContentsMargins(4, 4, 4, 4)
            layout.addWidget(self._mapping_panel)
            self._mapping_dialog = dialog
        self._update_mapping_diagnostics_summary()
        self._update_repair_buttons()
        self._mapping_dialog.show()
        self._mapping_dialog.raise_()
        self._mapping_dialog.activateWindow()

    def _set_global_edit_mode(self, enabled: bool) -> None:
        if enabled:
            if not self._has_editable_model():
                self.edit_toggle_action.blockSignals(True)
                self.edit_toggle_action.setChecked(False)
                self.edit_toggle_action.blockSignals(False)
                if self.viewport.is_edit_mode:
                    self.viewport.exit_edit_mode()
                self._notify(
                    "Edit Mode requires a decoded skeleton-bearing model.",
                    7000)
                return
            self._remember_geometry_original(self._selected_owner)
            self._sync_editor_context()
            if not self.viewport.is_edit_mode:
                self.edit_toggle_action.blockSignals(True)
                self.edit_toggle_action.setChecked(False)
                self.edit_toggle_action.blockSignals(False)
                return
            self.viewport.setFocus()
        else:
            self._cancel_live_transforms()
            if self.viewport.paste_preview_active:
                self.viewport.cancel_paste_preview()
            if self.viewport.is_edit_mode:
                self.viewport.exit_edit_mode()
            self._clear_model_selection()

    def _on_edit_mode_toggled(self, active: bool) -> None:
        button = getattr(self, "edit_toggle_action", None)
        if button is not None and button.isChecked() != active:
            button.blockSignals(True)
            button.setChecked(active)
            button.blockSignals(False)
        if active:
            if self._active_editor_panel() is not self._model_editor_panel:
                self._show_model_editor()
            # The viewport's central Edit Mode badge and the Tab shortcut can
            # enter Edit Mode directly. Reapply the same polygon-picking
            # interaction configured by the main switch so all entry paths
            # behave identically.
            self._sync_editor_context()
            if self.mirror_select_check.isChecked():
                session = self.viewport.edit_session
                if session is not None:
                    session.set_mirror_enabled(True)
                self._expand_current_mirror_selection()
        self._sync_gizmo_camera()
        self._sync_edit_action_states()
        self._notify("Edit Mode enabled." if active else
                     "View Mode enabled.", 4000)

    def _on_paste_preview_active_changed(self, active: bool) -> None:
        if active and hasattr(self, "model_gizmo"):
            self._set_transform_mode("move")
        for mode, button in getattr(
                self, "transform_mode_buttons", {}).items():
            button.setEnabled(not active or mode == "move")
        self.file_menu.setEnabled(not active)
        self.asset_tree.setEnabled(not active)
        self.edit_toggle_action.setEnabled(not active)
        self.toolbar_view_preset_combo.setEnabled(not active)
        self.edit_reset_action.setEnabled(
            not active and self.edit_reset_action.isEnabled())
        self._sync_geometry_save_controls()
        self._sync_edit_action_states()

    def _sync_edit_action_states(self) -> None:
        session = self.viewport.edit_session
        active = session is not None
        editing = self._editing_allowed()
        paste_preview = self.viewport.paste_preview_active
        preview_operation = self.viewport.paste_preview_operation
        archive_read_only = self._selected_model_is_archive_read_only()
        copy_reason = self._copy_geometry_reason()
        delete_reason = self._delete_geometry_reason()
        can_copy = not copy_reason
        can_delete = not delete_reason
        if hasattr(self, "edit_toggle_action"):
            self.edit_toggle_action.setText(
                "View Mode" if active else "Edit Mode")
            self.edit_toggle_action.setEnabled(not paste_preview)
        if hasattr(self, "edit_menu"):
            self.edit_menu.setEnabled(not paste_preview)
        if hasattr(self, "mapping_repair_action"):
            self.mapping_repair_action.setEnabled(not paste_preview)
        if hasattr(self, "_right_tabs") and hasattr(self, "_editor_tabs"):
            editor_index = self._right_tabs.indexOf(self._editor_tabs)
            if editor_index >= 0:
                # Archive-backed and loose models share the same in-memory
                # Edit Mode. Persistence remains constrained by the verified
                # export/overwrite paths, so SET.BAS itself stays read-only.
                self._right_tabs.setTabEnabled(editor_index, True)
                self._right_tabs.setTabToolTip(
                    editor_index,
                    self._archive_read_only_message()
                    if archive_read_only else "Edit the selected model.")
        for name in ("edit_select_all_action", "edit_select_none_action"):
            action = getattr(self, name, None)
            if action is not None:
                action.setEnabled(active and not paste_preview)
        if hasattr(self, "edit_undo_action"):
            self.edit_undo_action.setEnabled(
                editing and bool(self._edit_undo_stack)
                and not paste_preview)
        if hasattr(self, "edit_redo_action"):
            self.edit_redo_action.setEnabled(
                editing and bool(self._edit_redo_stack)
                and not paste_preview)
        if hasattr(self, "global_undo_button"):
            self.global_undo_button.setEnabled(
                editing and bool(self._edit_undo_stack)
                and not paste_preview)
        if hasattr(self, "global_redo_button"):
            self.global_redo_button.setEnabled(
                editing and bool(self._edit_redo_stack)
                and not paste_preview)
        active_fx = self._active_fx_selection()
        fx_descriptor = (
            f"{active_fx.fx_name} "
            f"{'VANM ' if active_fx.source_kind == 'VANM' else ''}Element"
            if active_fx is not None else "")
        if hasattr(self, "mirror_select_check"):
            base_enabled = bool(
                editing and session is not None and not paste_preview)
            self.mirror_select_check.setEnabled(base_enabled)
            self.mirror_select_check.setToolTip(
                "Keep selected geometry and automatically add every matching "
                "X, Y and Z counterpart."
                if base_enabled else
                "Enable Edit Mode to use automatic mirrored selection.")
        if hasattr(self, "copy_geometry_action"):
            self.copy_geometry_action.setEnabled(can_copy)
            copy_tip = (
                f"Copy {fx_descriptor} and start Paste FX Preview."
                if active_fx is not None else
                "Copy the selected geometry and start Copy Preview.")
            self.copy_geometry_action.setToolTip(copy_tip)
            self.copy_geometry_action.setStatusTip(copy_tip)
        if hasattr(self, "cut_geometry_action"):
            can_cut = editing and can_copy and can_delete
            self.cut_geometry_action.setEnabled(can_cut)
            cut_tip = (
                "Cut the selected geometry: copy it and remove it in one "
                "undoable step."
                if can_cut else
                f"Cut unavailable: {copy_reason or delete_reason}.")
            self.cut_geometry_action.setToolTip(cut_tip)
            self.cut_geometry_action.setStatusTip(cut_tip)
        if hasattr(self, "paste_geometry_action"):
            has_clipboard = self._geometry_clipboard is not None
            self.paste_geometry_action.setEnabled(
                editing and (has_clipboard or paste_preview))
            fx_clipboard = isinstance(
                self._geometry_clipboard, FxElementClipboard)
            paste_reason = (
                f"Confirm the current {(preview_operation or 'copy').title()} "
                f"{'FX ' if fx_clipboard else ''}Preview."
                if paste_preview else self._preview_geometry_reason())
            if not paste_reason and not self.can_commit_geometry():
                paste_reason = (
                    "Preview is available; confirmation will be refused: "
                    f"{self._commit_geometry_reason()}.")
            if not paste_reason:
                paste_reason = (
                    "Start a Paste FX Preview."
                    if fx_clipboard else "Start a Copy Preview.")
            self.paste_geometry_action.setToolTip(paste_reason)
            self.paste_geometry_action.setStatusTip(paste_reason)
        if hasattr(self, "delete_geometry_action"):
            self.delete_geometry_action.setEnabled(
                editing and can_delete)
            delete_tip = (
                f"Delete the complete {fx_descriptor}."
                if active_fx is not None else
                "Delete selected geometry and compact unused vertices.")
            self.delete_geometry_action.setToolTip(delete_tip)
            self.delete_geometry_action.setStatusTip(delete_tip)
        can_select_all_fx = bool(self._fx_elements) and not paste_preview
        if hasattr(self, "select_all_fx_action"):
            self.select_all_fx_action.setEnabled(can_select_all_fx)
        can_add_fx = editing and self._can_add_fx_element()
        if hasattr(self, "add_fx_action"):
            self.add_fx_action.setEnabled(can_add_fx)
        if hasattr(self, "add_fx_button"):
            self.add_fx_button.setEnabled(can_add_fx)
        if hasattr(self, "clone_fx_action"):
            can_clone = editing and can_copy and bool(self._active_fx_elements())
            self.clone_fx_action.setEnabled(can_clone)
            self.clone_fx_button.setEnabled(can_clone)
            can_rename = editing and self._selected_fx_element() is not None and not paste_preview
            self.rename_fx_action.setEnabled(can_rename)
        if hasattr(self, "delete_fx_button"):
            element = self._selected_fx_element()
            multi_fx = self._active_fx_elements()
            can_delete_fx = (
                editing
                and can_delete
                and ((element is not None
                      and element.shared_state != "invalid")
                     or bool(multi_fx)))
            self.delete_fx_button.setEnabled(can_delete_fx)
            if element is None:
                delete_fx_tip = (
                    f"Delete the {len(multi_fx)} selected FX elements. "
                    "The operation can be undone."
                    if multi_fx else
                    "Select an FX element from the list first.")
            elif element.shared_state == "invalid":
                delete_fx_tip = (
                    f"{element.fx_name} is structurally invalid and "
                    "cannot be deleted safely.")
            else:
                delete_fx_tip = (
                    f"Delete unavailable: {delete_reason}."
                    if delete_reason else
                    f"Delete the complete {element.fx_name} FX element. "
                    "The operation can be undone.")
            self.delete_fx_button.setToolTip(delete_fx_tip)
        if hasattr(self, "model_gizmo"):
            can_transform = bool(
                editing and session is not None and session.selection)
            can_use_gizmo = bool(
                paste_preview if self._transform_mode == "move"
                else can_transform)
            if self._transform_mode == "move":
                can_use_gizmo = paste_preview or can_transform
            self.model_gizmo.setEnabled(can_use_gizmo)
            self.gizmo_intensity_slider.setEnabled(can_use_gizmo)
            self.gizmo_intensity_spin.setEnabled(can_use_gizmo)
        if hasattr(self, "edit_move_action"):
            self.edit_move_action.setEnabled(
                editing and can_copy and session is not None
                and not session.modal_active and not paste_preview)
        if hasattr(self, "edit_scale_action"):
            self.edit_scale_action.setEnabled(
                editing and self._family is not None and not paste_preview)
        if hasattr(self, "edit_center_action"):
            can_center = bool(
                editing and session is not None
                and not session.modal_active and not paste_preview)
            self.edit_center_action.setEnabled(can_center)
            if hasattr(self, "center_origin_button"):
                self.center_origin_button.setEnabled(can_center)
        if hasattr(self, "edit_rotate_action"):
            self.edit_rotate_action.setEnabled(
                editing and self._family is not None and not paste_preview)
        if hasattr(self, "load_texture_button"):
            classification = (
                classify_texture_assignment(
                    self._mapping_index, self._selected_polys)
                if self._mapping_index is not None else None)
            self.load_texture_button.setEnabled(bool(
                editing and classification is not None
                and (classification.groups or classification.skipped)))
        # This synchronizer is also used while the window is being built.
        # Keep it safe before the later editor widgets exist.
        if hasattr(self, "repair_copy_button"):
            self._update_repair_buttons()
        if hasattr(self, "uv_add_vertex_button"):
            self._sync_uv_add_vertex_button()

    def _sync_geometry_save_controls(self) -> None:
        family_export_enabled = self.can_save_as_geometry()
        overwrite_enabled = self.can_overwrite_geometry()
        self.model_save_button.setEnabled(overwrite_enabled)
        self.model_save_as_button.setEnabled(family_export_enabled)
        self.save_asset_family_action.setEnabled(family_export_enabled)
        onua3d_enabled = bool(
            self._blender_export_object() is not None
            and not self.viewport.paste_preview_active)
        self.export_blender_action.setEnabled(onua3d_enabled)
        self.overwrite_action.setEnabled(overwrite_enabled)
        owner = self._selected_owner
        can_reset = bool(
            self._editing_allowed()
            and not self.viewport.paste_preview_active
            and (
                owner in self._geom_dirty
                or any(key[0] == owner for key in self._texture_original)
                or any(key[0] == owner for key in self._uv_original)
                or bool(self._owner_vanm_uv_keys(owner))))
        reset_action = getattr(self, "edit_reset_action", None)
        if reset_action is not None:
            reset_action.setEnabled(can_reset)
        reset_button = getattr(self, "toolbar_reset_model_button", None)
        if reset_button is not None:
            reset_button.setEnabled(can_reset)
        self._sync_edit_action_states()

    def _on_geometry_edited(self, owner: str) -> None:
        fam_obj = self._owner_to_obj.get(owner)
        if fam_obj is not None and getattr(fam_obj, "skeleton", None) \
                is not None:
            self._remember_geometry_original(owner)
            self._geom_dirty[owner] = fam_obj
        self._sync_geometry_save_controls()
        self._update_editor_status()
        self._sync_uv_add_vertex_button()

    def _on_geometry_command_committed(self, owner: str, before,
                                       after) -> None:
        self._record_edit_command({
            "kind": "geometry",
            "owner": owner,
            "before": [tuple(point) for point in before],
            "after": [tuple(point) for point in after],
            "label": "geometry edit",
        })

    def _record_edit_command(self, command: dict) -> None:
        if self._history_replaying or command.get("before") == command.get("after"):
            return
        self._edit_undo_stack.append(command)
        del self._edit_undo_stack[:-100]
        self._edit_redo_stack.clear()
        self._sync_edit_action_states()

    def _apply_geometry_history(self, owner: str, points) -> bool:
        values = [tuple(point) for point in points]
        if self.viewport.apply_geometry_snapshot(owner, values):
            applied = True
        else:
            fam_obj = self._owner_to_obj.get(owner)
            model = getattr(fam_obj, "skeleton", None)
            if model is None or len(model.points) != len(values):
                return False
            model.points[:] = values
            self.viewport.refresh_family_materials()
            applied = True
        original = self._geom_original.get(owner)
        fam_obj = self._owner_to_obj.get(owner)
        if original is not None and values == original:
            self._geom_dirty.pop(owner, None)
        elif fam_obj is not None:
            self._geom_dirty[owner] = fam_obj
        return applied

    def _apply_texture_history(self, snapshot: dict) -> bool:
        resolved = []
        affected = set()
        for key, target in snapshot.items():
            owner, block_index, binding_slot = key
            fam_obj = self._owner_to_obj.get(owner)
            if fam_obj is None or not (
                    0 <= block_index < len(fam_obj.base_object.ades)):
                return False
            block = fam_obj.base_object.ades[block_index]
            if binding_slot not in ("texture", "tracy_texture"):
                return False
            texture = getattr(block, binding_slot)
            if texture is None:
                return False
            current = texture.name
            resolved.append((key, texture, current, str(target)))
            affected.add(owner)
        tracker_before = dict(self._texture_original)
        try:
            for key, texture, current, target in resolved:
                if key not in self._texture_original \
                        and current.casefold() != target.casefold():
                    self._texture_original[key] = current
                texture.name = target
                original = self._texture_original.get(key)
                if original is not None \
                        and target.casefold() == original.casefold():
                    self._texture_original.pop(key, None)
            for owner in affected:
                fam_obj = self._owner_to_obj.get(owner)
                if fam_obj is not None and self._family is not None:
                    rebuild_materials(fam_obj, self._family)
        except Exception:
            for _key, texture, current, _target in resolved:
                texture.name = current
            self._texture_original = tracker_before
            for owner in affected:
                fam_obj = self._owner_to_obj.get(owner)
                if fam_obj is not None and self._family is not None:
                    try:
                        rebuild_materials(fam_obj, self._family)
                    except Exception:
                        pass
            return False
        self.viewport.refresh_family_materials()
        self._refresh_fx_elements()
        return True

    def _animation_for_name(self, name: str):
        if self._family is None:
            return None
        return next((
            (canonical, animation)
            for canonical, animation in self._family.animations.items()
            if canonical.casefold() == str(name).casefold()
        ), None)

    def _animation_uv_writable(self, name: str, group_index: int) -> bool:
        entry = self._animation_for_name(name)
        if entry is None:
            return False
        animation = entry[1]
        if not (0 <= int(group_index) < len(animation.texcoord_groups)):
            return False
        try:
            export_anm_bytes(animation)
        except AnmParseError:
            return False
        return True

    def _owner_animation_names(self, owner: str | None) -> set[str]:
        fam_obj = self._owner_to_obj.get(owner) if owner else None
        if fam_obj is None:
            return set()
        names = set()
        for block in fam_obj.base_object.ades:
            texture = block.texture
            if texture is None or texture.kind != "bmpanim":
                continue
            entry = self._animation_for_name(texture.name)
            if entry is not None:
                names.add(entry[0].casefold())
        return names

    def _owner_vanm_uv_keys(self, owner: str | None):
        names = self._owner_animation_names(owner)
        return {
            key for key in self._vanm_uv_original
            if key[0].casefold() in names}

    def _uv_storage(self, key):
        owner, block_index, atts_index = key
        if owner == "@VANM":
            entry = self._animation_for_name(block_index)
            if entry is None:
                return None
            canonical, animation = entry
            group_index = int(atts_index)
            if not (0 <= group_index < len(animation.texcoord_groups)):
                return None
            return "vanm", canonical, animation, group_index
        fam_obj = self._owner_to_obj.get(owner)
        if fam_obj is None or block_index >= len(fam_obj.base_object.ades):
            return None
        block = fam_obj.base_object.ades[block_index]
        if atts_index >= len(block.olpl):
            return None
        return "olpl", fam_obj, block, atts_index

    @staticmethod
    def _uv_storage_points(storage):
        kind, _owner, container, index = storage
        groups = (
            container.texcoord_groups if kind == "vanm"
            else container.olpl)
        return [tuple(uv) for uv in groups[index]]

    def _uv_original_tracker(self, key):
        if key[0] == "@VANM":
            entry = self._animation_for_name(key[1])
            canonical = entry[0] if entry is not None else str(key[1])
            return self._vanm_uv_original, (canonical, int(key[2]))
        return self._uv_original, key

    def _apply_uv_histories(self, snapshot: dict,
                            *, refresh: bool = True) -> bool:
        """Apply several OLPL groups as one rollback-safe history step."""

        resolved = {}
        before = {}
        for key, target in snapshot.items():
            storage = self._uv_storage(key)
            if storage is None:
                return False
            current = self._uv_storage_points(storage)
            updated = [tuple(uv) for uv in target]
            if len(updated) != len(current) or any(
                    len(uv) != 2
                    or any(value < 0 or value > 255 for value in uv)
                    for uv in updated):
                return False
            resolved[key] = updated
            before[key] = current

        dirty_before = {}
        for key in resolved:
            tracker, tracked_key = self._uv_original_tracker(key)
            dirty_before[key] = (
                tracker, tracked_key, tracked_key in tracker,
                copy.deepcopy(tracker.get(tracked_key)))
        try:
            for key, updated in resolved.items():
                current = before[key]
                tracker, tracked_key = self._uv_original_tracker(key)
                if tracked_key not in tracker and current != updated:
                    tracker[tracked_key] = current
                self._restore_uv(key, updated)
                if tracker.get(tracked_key) == updated:
                    tracker.pop(tracked_key, None)
        except Exception:
            for key, current in before.items():
                self._restore_uv(key, current)
            for _key, (tracker, tracked_key, existed, value) \
                    in dirty_before.items():
                if existed:
                    tracker[tracked_key] = value
                else:
                    tracker.pop(tracked_key, None)
            return False
        if refresh:
            self.viewport.refresh_family_materials()
        return True

    def _apply_uv_history(self, key, target) -> bool:
        return self._apply_uv_histories({key: target})

    def _apply_topology_history(self, owner: str, target: dict) -> bool:
        if not self._restore_topology_state(owner, target):
            return False
        original = self._topology_original.get(owner)
        fam_obj = self._owner_to_obj.get(owner)
        if self._topology_content(target) == self._topology_content(original):
            self._geom_dirty.pop(owner, None)
        elif fam_obj is not None:
            self._geom_dirty[owner] = fam_obj
        return True

    def _apply_material_topology_history(
            self, command: dict, target: dict, *, redo: bool) -> bool:
        family = self._family
        if family is None:
            return False
        previous = self._capture_family_resource_state(family)
        resources = command[
            "resource_after" if redo else "resource_before"]
        try:
            # Resources must be present before rebuilding material groups for
            # the restored ADES state.
            self._restore_family_resource_state(family, resources)
            if not self._apply_topology_history(command["owner"], target):
                self._restore_family_resource_state(family, previous)
                return False
        except Exception:
            self._restore_family_resource_state(family, previous)
            return False
        return True

    def _apply_edit_command(self, command: dict, redo: bool) -> bool:
        target = command["after" if redo else "before"]
        kind = command.get("kind")
        self._history_replaying = True
        try:
            if kind == "geometry":
                applied = self._apply_geometry_history(command["owner"], target)
            elif kind == "topology":
                applied = self._apply_topology_history(
                    command["owner"], target)
            elif kind == "material_topology":
                applied = self._apply_material_topology_history(
                    command, target, redo=redo)
            elif kind == "texture":
                applied = self._apply_texture_history(target)
            elif kind == "uv":
                applied = self._apply_uv_history(command["key"], target)
            elif kind == "uv_multi":
                applied = self._apply_uv_histories(target)
            elif kind == "model_state":
                applied = True
                topology = target.get("topology")
                geometry = target.get("geometry")
                if topology is not None:
                    applied = self._apply_topology_history(
                        command["owner"], topology) and applied
                elif geometry is not None:
                    applied = self._apply_geometry_history(
                        command["owner"], geometry) and applied
                if target.get("textures"):
                    applied = self._apply_texture_history(
                        target["textures"]) and applied
                for key, uvs in target.get("uvs", {}).items():
                    applied = self._apply_uv_history(key, uvs) and applied
                for key, uvs in target.get("vanm_uvs", {}).items():
                    applied = self._apply_uv_history(
                        ("@VANM", key[0], key[1]), uvs) and applied
            else:
                applied = False
        finally:
            self._history_replaying = False
        if not applied:
            return False
        self._fill_polygon_inspector(self._selected_poly)
        self._sync_geometry_save_controls()
        self._update_editor_status()
        self._sync_uv_add_vertex_button()
        self._sync_editor_context()
        return True

    def _undo_edit(self) -> None:
        if not self._require_editing("Undo"):
            return
        if not self._edit_undo_stack:
            self._notify("Undo: no edit is available.", 3000)
            return
        command = self._edit_undo_stack.pop()
        if self._apply_edit_command(command, False):
            self._edit_redo_stack.append(command)
            self._notify(f"Undo: {command.get('label', 'edit')}.", 4500)
        else:
            self._edit_undo_stack.append(command)
            self._notify("Undo failed: the edited model is no longer available.",
                         6000)
        self._sync_edit_action_states()

    def _redo_edit(self) -> None:
        if not self._require_editing("Redo"):
            return
        if not self._edit_redo_stack:
            self._notify("Redo: no edit is available.", 3000)
            return
        command = self._edit_redo_stack.pop()
        if self._apply_edit_command(command, True):
            self._edit_undo_stack.append(command)
            self._notify(f"Redo: {command.get('label', 'edit')}.", 4500)
        else:
            self._edit_redo_stack.append(command)
            self._notify("Redo failed: the edited model is no longer available.",
                         6000)
        self._sync_edit_action_states()

    def _confirm_discard_geometry(self) -> bool:
        """Confirm switching away from unsaved in-memory model edits."""

        model_dirty = bool(
            self._geom_dirty or self._texture_original
            or self._uv_original or self._vanm_uv_original
            or self._pending_repairs)
        if not model_dirty:
            return True
        if self._skip_model_switch_warning:
            return True
        if self._selected_model_is_archive_read_only():
            message = (
                "This SET.BAS model has unsaved in-memory changes.\n"
                "SET.BAS has not been modified. Export Asset Family first if "
                "you want to keep the changes.\n\n"
                "Switching models will discard them. Continue?")
        else:
            message = (
                "This model has unsaved changes.\n"
                "Switching models will discard them. Continue?")
        box = QMessageBox(QMessageBox.Icon.Question, "Unsaved changes",
                          message,
                          QMessageBox.StandardButton.Yes
                          | QMessageBox.StandardButton.No, self)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        skip = QCheckBox("Don't show this again during this session")
        box.setCheckBox(skip)
        accepted = box.exec() == QMessageBox.StandardButton.Yes
        if accepted and skip.isChecked():
            self._skip_model_switch_warning = True
        return accepted
