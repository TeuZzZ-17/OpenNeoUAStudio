"""CollisionEditorWindow mixin: collision shapes: status, preview, generation (bake), import/export."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import re
import sys
import tempfile

from PySide6.QtCore import (
    QProcess,
    QProcessEnvironment,
)
from PySide6.QtWidgets import (
    QFileDialog,
    QMessageBox,
)

from entity_designer.script_model import (
    _collision_shape_export_filename,
    _data_root_for_path,
    CollisionProject,
    VEHICLE,
)
from entity_designer.shape import (
    geometry_fingerprint,
    parse_collision_shape,
    read_collision_shape,
    write_collision_shape,
)
from entity_designer.widgets import (
    _geometry_component_key,
)


class _CollisionShapeMixin:
    """Part of CollisionEditorWindow; `self` is always the CollisionEditorWindow."""

    def _active_asset_set_id(self) -> int:
        path = self._active_base_path
        if path is None:
            return 0
        for part in reversed(path.resolve(strict=False).parts):
            match = re.fullmatch(r"set(\d+)", part, re.IGNORECASE)
            if match:
                return int(match.group(1))
        return 0

    def _collision_shape_warnings(self) -> list[str]:
        shape = self.project.collision_shape
        if shape is None:
            return []
        warnings = []
        scale = (self.project.model_scale_x, self.project.model_scale_y,
                 self.project.model_scale_z)
        rotation = (self.project.model_rotation_x,
                    self.project.model_rotation_y,
                    self.project.model_rotation_z)
        if any(abs(a - b) > 1e-5 for a, b in zip(scale, shape.scale)):
            warnings.append("visual scale differs from generation metadata")
        if any(abs(a - b) > 1e-5
               for a, b in zip(rotation, shape.rotation)):
            warnings.append("visual rotation differs from generation metadata")
        owners = self.project.collision_shape_owners
        selected_keys = self.project.collision_shape_components
        if self.family is not None and selected_keys is None:
            warnings.append(
                "source selection not stored; geometry revision unverified")
        if (owners and selected_keys is not None and self.family is not None
                and self._collision_geometry_cache_key
                == self._current_collision_geometry_source_key()):
            by_key = {
                _geometry_component_key(component): component
                for component in self._collision_geometry_components
            }
            keys = tuple(selected_keys)
            missing = [key for key in keys if key not in by_key]
            parts = [
                (key, by_key[key].triangles)
                for key in keys if key in by_key
            ]
            fingerprint = self._collision_geometry_fingerprints.get(keys)
            if fingerprint is None and parts:
                fingerprint = geometry_fingerprint(parts)
                self._collision_geometry_fingerprints[keys] = fingerprint
            if (missing or (fingerprint is not None
                            and fingerprint != shape.source_hash)):
                warnings.append(
                    "source fingerprint differs from the current geometry "
                    "selection or one or more selected components disappeared")
        return warnings

    def _sync_collision_shape_status(self) -> None:
        shape = self.project.collision_shape
        self.viewport.set_collision_shape(shape)
        self.collision_geometry_widget.set_shape(shape)
        if shape is None:
            if self.project.collision_shape_path:
                self.collision_shape_status.setText(
                    "Script references a shape file that is not loaded: "
                    f"{self.project.collision_shape_path}")
            else:
                self.collision_shape_status.setText("No collision shape loaded.")
            self.export_collision_shape_button.setEnabled(False)
            self._sync_collision_overwrite_button()
            return
        enabled_count = sum(hull.enabled for hull in shape.hulls)
        quality = f"{shape.quality.capitalize()} quality" if shape.quality else "Quality unknown"
        details = f"Shape loaded: {enabled_count}/{len(shape.hulls)} active parts — {quality}"
        if self.project.collision_shape_path:
            details += f" — {Path(self.project.collision_shape_path).name}"
        warnings = self._collision_shape_warnings()
        scale_warning = (
            "visual scale differs from generation metadata" in warnings)
        rotation_warning = (
            "visual rotation differs from generation metadata" in warnings)
        if scale_warning or rotation_warning:
            details += "\nVisual transform warning: " + (
                "scale and rotation differ from the generated shape."
                if scale_warning and rotation_warning else
                "scale differs from the generated shape."
                if scale_warning else
                "rotation differs from the generated shape.")
        self.collision_shape_status.setText(details)
        self.export_collision_shape_button.setEnabled(enabled_count > 0 and self._collision_bake_process is None)
        self._sync_collision_overwrite_button()

    def _sync_collision_overwrite_button(self) -> None:
        """Overwrite needs a shape and the .collision file it belongs to."""

        shape = self.project.collision_shape
        self.overwrite_collision_shape_button.setEnabled(
            shape is not None and any(h.enabled for h in shape.hulls)
            and bool(self.project.collision_shape_file_path)
            and self._collision_bake_process is None)

    def _collision_shape_components_changed(self) -> None:
        shape = self.project.collision_shape
        if shape is None or self.collision_geometry_widget._shape is not shape:
            return
        keys = self.collision_geometry_widget.enabled_shape_keys()
        hulls = tuple(replace(hull, enabled=(
            hull.source_component or f"@hull:{index}") in keys)
            for index, hull in enumerate(shape.hulls))
        if hulls == shape.hulls:
            return
        self._push_undo()
        self.project.collision_shape = replace(shape, hulls=hulls)
        baseline = CollisionProject()
        baseline.restore(self._export_baseline)
        if self.project.collision_shape == baseline.collision_shape:
            # Returning to the saved hull state also restores its existing file reference.
            self.project.collision_shape_file_path = baseline.collision_shape_file_path
            self.project.collision_shape_path = baseline.collision_shape_path
        else:
            self.project.collision_shape_file_path = ""
            self.project.collision_shape_path = ""
        self._set_modified(self.project.snapshot() != self._export_baseline)
        self._sync_all()

    def _shape_preview_settings(self) -> None:
        self.viewport.set_model_preview_scale(
            self.project.model_scale_x, self.project.model_scale_y,
            self.project.model_scale_z)
        self.viewport.set_model_preview_rotation(
            self.project.model_rotation_x, self.project.model_rotation_y,
            self.project.model_rotation_z)
        self.viewport.set_ground_alignment(
            self.project.target_category == VEHICLE,
            self.project.overeof_enabled, self.project.overeof)
        self.viewport.set_collision_shape(self.project.collision_shape)

    def _current_collision_geometry_source_key(self):
        if self.family is None:
            return None
        reference = self._active_visual_reference
        reference_key = None if reference is None else (
            reference.block.kind, reference.block.object_id,
            reference.vp_normal, reference.base_normal,
            reference.three_ds_normal)
        source_path = (
            str(self._active_base_path.resolve(strict=False))
            if self._active_base_path else "")
        return (
            source_path, id(self.family), reference_key,
            self._collision_shape_root_owner(), self._current_owner,
            self.project.source_model)

    def _set_collision_geometry_preview(self, triangles) -> None:
        self._collision_geometry_preview = (
            tuple(triangles) if triangles else None)
        self.viewport.set_geometry_component_preview(
            self._collision_geometry_preview)

    def _refresh_collision_geometry_widget(self) -> None:
        widget = getattr(self, "collision_geometry_widget", None)
        if widget is None:
            return
        source_key = self._current_collision_geometry_source_key()
        if source_key == self._collision_geometry_cache_key:
            return
        self._collision_geometry_fingerprints.clear()
        if self.family is None:
            self._collision_geometry_cache_key = None
            self._collision_geometry_components = ()
            self._collision_geometry_triangles_by_owner = {}
            self._collision_geometry_owners = ()
            widget.set_geometry(None, None, (), {}, None)
            self._set_collision_geometry_preview(None)
            self._sync_collision_generation_controls()
            return

        root_owner = self._collision_shape_root_owner()
        eligible = [
            obj for obj in self.family.all_objects()
            if obj.skeleton is not None and (
                root_owner is None
                or obj.owner_path == root_owner
                or obj.owner_path.startswith(root_owner.rstrip("/") + "/"))
        ]
        if not eligible:
            self._collision_geometry_cache_key = source_key
            self._collision_geometry_components = ()
            self._collision_geometry_triangles_by_owner = {}
            self._collision_geometry_owners = ()
            widget.set_geometry(self.family, root_owner, (), {}, source_key)
            self._sync_collision_generation_controls()
            return

        from entity_designer.mesh_parts import split_triangle_components

        previous_scope = self.viewport.visible_owners()
        previous_camera = self.viewport._camera_state()
        previous_home_camera = self.viewport._home_camera_state
        owner_triangles = {}
        components = ()
        error = None
        try:
            self.viewport.load_family(
                self.family, {obj.owner_path for obj in eligible},
                keep_camera=True, primary_owner=root_owner)
            self._shape_preview_settings()
            owner_triangles = {
                obj.owner_path: tuple(
                    self.viewport.local_owner_triangles(obj.owner_path))
                for obj in eligible
            }
            components = split_triangle_components([
                (owner, triangles)
                for owner, triangles in owner_triangles.items()
            ])
        except (TypeError, ValueError, RuntimeError) as exc:
            error = str(exc)
        finally:
            self.viewport.load_family(
                self.family, previous_scope, keep_camera=True,
                primary_owner=self._current_owner)
            self._shape_preview_settings()
            self.viewport._set_camera_state(previous_camera)
            self.viewport._home_camera_state = previous_home_camera

        self._collision_geometry_cache_key = source_key
        self._collision_geometry_components = tuple(components)
        self._collision_geometry_triangles_by_owner = owner_triangles
        self._collision_geometry_owners = tuple(sorted(owner_triangles))
        widget.set_geometry(
            self.family, root_owner, self._collision_geometry_components,
            owner_triangles, source_key)
        if error:
            widget.summary_label.setText(f"Geometry unavailable: {error}")
        self._sync_collision_generation_controls()

    def _sync_collision_generation_controls(self) -> None:
        process_running = self._collision_bake_process is not None
        widget = getattr(self, "collision_geometry_widget", None)
        has_selection = bool(
            widget is not None and widget.selected_components())
        if hasattr(self, "generate_collision_shape_button"):
            self.generate_collision_shape_button.setEnabled(
                self.family is not None and has_selection and not process_running)
            self.collision_shape_quality.setEnabled(not process_running)
            self.import_collision_shape_button.setEnabled(
                self._current_set_bas_path() is not None and not process_running)
            self.export_collision_shape_button.setEnabled(
                self.project.collision_shape is not None
                and any(h.enabled for h in self.project.collision_shape.hulls)
                and not process_running)
            self.export_collision_shape_action.setEnabled(
                self.export_collision_shape_button.isEnabled())
            self.delete_collision_shape_button.setEnabled(
                self.project.collision_shape is not None and not process_running)
            self._sync_collision_overwrite_button()
            self.collision_shape_progress.setVisible(
                process_running and not self._collision_bake_cancelled)
            if widget is not None:
                widget.setEnabled(not process_running)

    def generate_collision_shape(self) -> None:
        if self._collision_bake_process is not None:
            return
        self._refresh_collision_geometry_widget()
        if self.family is None:
            return
        if (self._active_visual_reference is not None
                and not (self._active_visual_reference.three_ds_normal
                         or self._active_visual_reference.base_normal)
                and self._active_visual_reference.vp_normal is not None
                and self._collision_shape_root_owner() is None):
            self.collision_shape_status.setText(
                "Generation unavailable: active VP has no model geometry.")
            return
        selected_components = (
            self.collision_geometry_widget.selected_components())
        if not selected_components:
            self.collision_shape_status.setText(
                "Select at least one geometry component.")
            return
        try:
            parts = [
                {
                    "owner": _geometry_component_key(component),
                    "model_owner": component.owner,
                    "component_id": component.component_id,
                    "triangles": component.triangles,
                }
                for component in selected_components
            ]
            request = {
                "source": self.project.source_model
                or self.project.source_base or "Model",
                "parts": parts,
                "scale": [
                    self.project.model_scale_x,
                    self.project.model_scale_y,
                    self.project.model_scale_z,
                ],
                "rotation": [
                    self.project.model_rotation_x,
                    self.project.model_rotation_y,
                    self.project.model_rotation_z,
                ],
                "asset_set": self._active_asset_set_id(),
                "quality": self.collision_shape_quality.currentData(),
            }
            request_bytes = json.dumps(
                request, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, ValueError, RuntimeError) as exc:
            self.collision_shape_status.setText(
                f"Generation unavailable: {exc}")
            return

        spheres = self.project.spheres()
        if spheres:
            answer = QMessageBox.question(
                self, "Generate Collision Shape",
                "All Legacy and OpenNeoUA collision spheres will be removed "
                "after generation succeeds. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return

        generation_tempdir = tempfile.TemporaryDirectory(
            prefix="OpenNeoUAStudio-collision-")
        input_path = Path(generation_tempdir.name) / "input.json"
        output_path = Path(generation_tempdir.name) / "output.json"
        try:
            input_path.write_bytes(request_bytes)
        except OSError as exc:
            generation_tempdir.cleanup()
            self.collision_shape_status.setText(
                f"Generation could not start: {exc}")
            return

        process = QProcess(self)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        process.setWorkingDirectory(str(Path(__file__).resolve().parents[1]))
        process.setProcessEnvironment(QProcessEnvironment.systemEnvironment())
        if getattr(sys, "frozen", False):
            program = sys.executable
            arguments = [
                "--collision-bake-worker", str(input_path), str(output_path)]
        else:
            program = sys.executable
            arguments = [
                str(Path(__file__).resolve().parents[1] / "main.py"),
                "--collision-bake-worker",
                str(input_path), str(output_path),
            ]
        process.setProgram(program)
        process.setArguments(arguments)
        process.finished.connect(
            lambda code, status, p=process:
            self._collision_shape_bake_finished(p, code, status))
        process.errorOccurred.connect(
            lambda error, p=process:
            self._collision_shape_bake_error(p, error))
        self._collision_bake_process = process
        self._collision_bake_cancelled = False
        self._collision_bake_tempdir = generation_tempdir
        self._collision_bake_context = {
            "source_path": (str(self._active_base_path.resolve(strict=False))
                            if self._active_base_path else ""),
            "source_model": self.project.source_model,
            "source_key": self._collision_geometry_cache_key,
            "project_snapshot": self.project.snapshot(),
            "scale": tuple(request["scale"]),
            "rotation": tuple(request["rotation"]),
            "owners": request["parts"],
            "output_path": str(output_path),
        }
        self.collision_shape_status.setText(
            f"Generating {len(parts)} selected parts at "
            f"{request['quality']} quality…")
        self._sync_collision_generation_controls()
        process.start()

    def _collision_shape_bake_error(self, process, error) -> None:
        if process is not self._collision_bake_process:
            return
        if self._collision_bake_cancelled:
            return
        if process.state() == QProcess.ProcessState.NotRunning:
            detail = process.errorString()
            self._collision_bake_process = None
            self._sync_collision_generation_controls()
            self._cleanup_collision_bake_tempdir()
            self.collision_shape_status.setText("Generation failed.")
            QMessageBox.warning(
                self, "Collision shape generation failed", detail)

    def _cleanup_collision_bake_tempdir(self) -> None:
        temporary = self._collision_bake_tempdir
        self._collision_bake_tempdir = None
        if temporary is not None:
            try:
                temporary.cleanup()
            except OSError:
                pass

    def _collision_shape_bake_finished(self, process, exit_code, _status):
        if process is not self._collision_bake_process:
            return
        stderr = bytes(process.readAllStandardError()).decode(
            "utf-8", errors="replace").strip()
        context = getattr(self, "_collision_bake_context", {})
        cancelled = self._collision_bake_cancelled
        self._collision_bake_process = None
        self._sync_collision_generation_controls()
        process.deleteLater()
        response = None
        response_error = ""
        try:
            response = json.loads(
                Path(context["output_path"]).read_text(encoding="utf-8"))
        except (KeyError, OSError, ValueError) as exc:
            response_error = str(exc)
        self._cleanup_collision_bake_tempdir()
        if cancelled:
            self.collision_shape_status.setText("Generation cancelled.")
            return
        current_source = (
            str(self._active_base_path.resolve(strict=False))
            if self._active_base_path else "")
        if (current_source != context.get("source_path")
                or self.project.source_model != context.get("source_model")
                or self._current_collision_geometry_source_key()
                != context.get("source_key")
                or tuple((self.project.model_scale_x,
                          self.project.model_scale_y,
                          self.project.model_scale_z)) != context.get("scale")
                or tuple((self.project.model_rotation_x,
                          self.project.model_rotation_y,
                          self.project.model_rotation_z))
                != context.get("rotation")):
            self.collision_shape_status.setText(
                "Generation discarded because the model changed.")
            return
        if self.project.snapshot() != context.get("project_snapshot"):
            self.collision_shape_status.setText(
                "Generation discarded because project data changed.")
            return
        if exit_code != 0 or (response and response.get("error")):
            detail = ((str(response.get("error")) if response else "")
                      or stderr or response_error or "worker error")
            self.collision_shape_status.setText(
                "Generation failed.")
            QMessageBox.warning(
                self, "Collision shape generation failed",
                detail if exit_code != 0
                else f"Worker returned an error: {detail}")
            return
        try:
            if response is None:
                raise ValueError(response_error or "missing worker output")
            shape = parse_collision_shape(response["shape"])
            bake_warnings = tuple(str(value) for value in response.get(
                "warnings", ()))
        except (ValueError, KeyError, TypeError) as exc:
            self.collision_shape_status.setText(
                "Generation returned invalid data.")
            QMessageBox.warning(
                self, "Collision shape generation failed",
                f"Worker response could not be validated: {exc}")
            return
        self._push_undo()
        self.project.collision_shape = shape
        self.project.collision_shape_path = ""
        self.project.collision_shape_file_path = ""
        selected_parts = tuple(context.get("owners", ()))
        self.project.collision_shape_owners = tuple(sorted({
            part["model_owner"] for part in selected_parts
        }))
        self.project.collision_shape_components = tuple(
            part["owner"] for part in selected_parts)
        self.project.collision_shape_warnings = bake_warnings
        self.project.legacy = None
        self.project.compound.clear()
        self.project.sphere_isolation_active = False
        self._selected = -1
        self._selected_spheres.clear()
        self._shape_preview_settings()
        self._set_modified()
        self._sync_all()
        self._sync_collision_shape_status()

    def delete_collision_shape(self) -> None:
        if (self._collision_bake_process is not None
                or self.project.collision_shape is None):
            return
        self._push_undo()
        self.project.collision_shape = None
        self.project.collision_shape_path = ""
        self.project.collision_shape_file_path = ""
        self.project.collision_shape_owners = None
        self.project.collision_shape_components = None
        self.project.collision_shape_warnings = ()
        self._shape_preview_settings()
        self._sync_all()
        self._set_modified()
        self.statusBar().showMessage("Collision shape deleted.", 4500)

    def import_collision_shape(self) -> None:
        if self._current_set_bas_path() is None:
            QMessageBox.information(
                self, "SET required",
                "Open a SET.BAS archive in the Entity Designer before "
                "importing a collision shape.")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Import collision shape", str(self._last_directory),
            "Collision shape (*.collision);;All files (*)")
        if not path:
            return
        source = Path(path)
        try:
            shape = read_collision_shape(source)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Invalid collision shape", str(exc))
            return
        data_root = _data_root_for_path(source)
        script_path = ""
        if data_root is not None:
            try:
                script_path = source.resolve(strict=False).relative_to(
                    data_root.parent).as_posix()
            except ValueError:
                pass
        self._push_undo()
        self.project.collision_shape = shape
        self.project.collision_shape_path = script_path
        self.project.collision_shape_file_path = str(
            source.resolve(strict=False))
        self.project.collision_shape_owners = None
        self.project.collision_shape_components = None
        self.project.collision_shape_warnings = ()
        self._shape_preview_settings()
        self._sync_all()
        self._mark_collision_shape_exported()
        self._last_directory = source.parent
        if shape.scale != (
                self.project.model_scale_x, self.project.model_scale_y,
                self.project.model_scale_z) or shape.rotation != (
                self.project.model_rotation_x, self.project.model_rotation_y,
                self.project.model_rotation_z):
            self.statusBar().showMessage(
                "Imported shape transform metadata differs from this model.",
                9000)

    def export_collision_shape(self) -> None:
        if self.collision_geometry_widget._selection_timer.isActive():
            self.collision_geometry_widget._flush_selection_changed()
        shape = self.project.collision_shape
        if shape is None:
            return
        default_name = _collision_shape_export_filename(self.project)
        data_root = _data_root_for_path(
            self._active_base_path, self._active_script_path)
        if data_root is not None:
            initial = data_root / "Models" / "Collision" / default_name
        elif self.project.collision_shape_file_path:
            initial = Path(self.project.collision_shape_file_path)
        else:
            initial = self._last_directory / default_name
        path, _ = QFileDialog.getSaveFileName(
            self, "Export collision shape", str(initial),
            "Collision shape (*.collision)")
        if not path:
            return
        target = Path(path)
        if target.suffix.casefold() != ".collision":
            target = target.with_suffix(".collision")
        self._write_collision_shape(shape, target)

    def overwrite_collision_shape(self) -> None:
        """Rewrite the .collision file the shape came from, without a file dialog."""

        if self.collision_geometry_widget._selection_timer.isActive():
            self.collision_geometry_widget._flush_selection_changed()
        shape = self.project.collision_shape
        file_path = self.project.collision_shape_file_path
        if shape is None or not file_path:
            return
        answer = QMessageBox.question(
            self, "Overwrite collision shape",
            f"Overwrite the collision shape file?\n\n{file_path}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._write_collision_shape(shape, Path(file_path))

    def _write_collision_shape(self, shape, target: Path) -> None:
        """Write ``shape`` to ``target`` and make it the unit's shape file."""

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            write_collision_shape(target, shape)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Shape export failed", str(exc))
            return
        root = _data_root_for_path(target)
        canonical = ""
        if root is not None:
            try:
                canonical = target.resolve(strict=False).relative_to(
                    root.parent).as_posix()
            except ValueError:
                pass
        self._push_undo()
        self.project.collision_shape_file_path = str(
            target.resolve(strict=False))
        self.project.collision_shape_path = canonical
        self._sync_all()
        self._mark_collision_shape_exported()
        self._last_directory = target.parent
        self._sync_collision_shape_status()
        self.statusBar().showMessage(
            f"Shape exported: {canonical or target.name}.", 5000)
