"""AssemblyWindow mixin: BASet extraction and conversion (capability merge), session texture
overrides, object selection (Large Family Mode) and completeness checks."""

from __future__ import annotations

import hashlib
from pathlib import Path
import re
import tempfile

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import (
    QAction,
    QColor,
    QImage,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QColorDialog,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from studio.assets.family import (
    AssetFamily,
    load_asset_family,
    SETBAS_OVERRIDE_PREFIX,
)
from studio.assets.mapping import (
    export_base_object_bytes,
    MappingEditError,
    save_model_base_copy,
    TextureNameEdit,
)
from studio.assets.visproto import (
    normalize_base_name,
)
from studio.exchange.portable import (
    backup_enabled,
    commit_verified_files as _commit_verified_files,
    next_backup_path as _next_backup_path,
    VerifiedCommitError as _BundleCommitError,
)
from studio.formats.base_format import parse_base_bytes
from studio.rendering.asset_viewport import (
    VIEW_PRESET_ANGLES,
)
from studio.rendering.snapshot_export import (
    CONTACT_SHEET_BACKGROUND,
    CONTACT_SHEET_VIEWS,
    AnimationExportCancelled,
    animation_frame_count,
    compose_contact_sheet,
    export_animation_atomic,
    is_animated_format,
    output_extension,
    write_static_image_atomic,
)
from studio.ui.widgets import (
    qimage_from_ilbm as _qimage_from_ilbm,
)
from studio.workbench.common import (
    _BAS_NAME_ROLE,
)


class _ExtractionMixin:
    """Part of AssemblyWindow; `self` is always the AssemblyWindow."""

    # -- extraction / conversion (BASet capability merge) -------------------------

    def _setbas_selected_resources(self) -> list:
        if self._setbas is None:
            return []
        resources = []
        seen: set[int] = set()
        context_item = self._setbas_context_item
        items = self.setbas_tree.selectedItems()
        if context_item is not None and not context_item.isSelected():
            items = [context_item]
        if not items and self.setbas_tree.currentItem() is not None:
            items = [self.setbas_tree.currentItem()]
        for item in items:
            index = item.data(0, Qt.ItemDataRole.UserRole)
            if index is None or index in seen:
                continue
            seen.add(index)
            resources.append(self._setbas.resources[index])
        return resources

    @staticmethod
    def _available_output_path(path: Path,
                               reserved: set[str] | None = None) -> Path:
        """Avoid overwriting files or colliding within one pending batch."""

        reserved = reserved or set()

        def unavailable(candidate: Path) -> bool:
            return (candidate.exists()
                    or str(candidate).casefold() in reserved)

        if not unavailable(path):
            return path
        for index in range(1, 10000):
            candidate = path.with_name(
                f"{path.stem}__dup{index:03d}{path.suffix}")
            if not unavailable(candidate):
                return candidate
        raise OSError(f"Could not find a free output name for {path.name}")

    def _extract_one_setbas_resource(self, resource, target: Path) -> str:
        """Extract one resource; embedded textures become usable ILBM."""

        class_id = resource.class_id.lower()
        if class_id == "ilbm.class":
            from studio.formats.setbas import decode_texture
            from studio.formats.images import write_image_as_ilbm

            image = decode_texture(self._setbas, resource)
            palette = image.palette
            palette_source = "embedded CMAP" if palette is not None else ""
            if palette is None:
                palette, palette_source = self._setbas_palette()
            result = write_image_as_ilbm(
                image, target, palette, source=resource.resource_name,
                warning=(f"palette from {palette_source}"
                         if palette_source else ""),
            )
            return (f"converted embedded VBMP to ILBM: "
                    f"{resource.resource_name} -> {result.output}")

        from studio.assets.setbas_export import extract_resource
        extract_resource(self._setbas, resource, target)
        return f"extracted {resource.resource_name} -> {target}"

    def _extract_setbas_selected(self) -> None:
        resources = self._setbas_selected_resources()
        if not resources:
            QMessageBox.information(
                self, "No resource selected",
                "Select one or more embedded resources in the SET.BAS tree "
                "first. Ctrl and Shift selection are supported.")
            return

        from studio.assets.setbas_export import flattened_resource_name

        targets: list[tuple[object, Path]] = []
        if len(resources) == 1:
            resource = resources[0]
            name = flattened_resource_name(resource.resource_name)
            if resource.class_id.lower() == "ilbm.class":
                name = Path(name).with_suffix(".ILBM").name
                caption = (f"Extract and convert {resource.resource_name} "
                           "to ILBM")
                file_filter = "ILBM texture (*.ILBM *.ilbm);;All files (*)"
            else:
                caption = f"Extract {resource.resource_name}"
                file_filter = "All files (*)"
            suggested = Path(self._last_directory) / name
            path, _ = QFileDialog.getSaveFileName(
                self, caption, str(suggested), file_filter)
            if not path:
                return
            targets.append((resource, Path(path)))
        else:
            out_dir = QFileDialog.getExistingDirectory(
                self, f"Output folder for {len(resources)} resources",
                str(self._last_directory))
            if not out_dir:
                return
            root = Path(out_dir)
            reserved: set[str] = set()
            for resource in resources:
                name = flattened_resource_name(resource.resource_name)
                if resource.class_id.lower() == "ilbm.class":
                    name = Path(name).with_suffix(".ILBM").name
                candidate = root / name
                suffix_index = 0
                while (candidate.exists()
                       or str(candidate).lower() in reserved):
                    suffix_index += 1
                    candidate = (root /
                                 f"{Path(name).stem}__dup{suffix_index:03d}"
                                 f"{Path(name).suffix}")
                reserved.add(str(candidate).lower())
                targets.append((resource, candidate))

        converted = 0
        raw = 0
        errors: list[str] = []
        for resource, target in targets:
            try:
                note = self._extract_one_setbas_resource(resource, target)
            except Exception as exc:
                errors.append(f"{resource.resource_name}: {exc}")
                self._log(f"extract FAILED: {resource.resource_name}: {exc}")
                continue
            if resource.class_id.lower() == "ilbm.class":
                converted += 1
            else:
                raw += 1
            self._log(note)

        output_folder = targets[0][1].parent
        self._last_directory = output_folder
        self._remember_output_folder(output_folder)
        message = (f"Selected extraction complete: {raw} raw resource(s), "
                   f"{converted} texture(s) converted to ILBM")
        if errors:
            message += f", {len(errors)} error(s)"
            QMessageBox.warning(
                self, "Extraction completed with errors",
                message + "\n\n" + "\n".join(errors[:12]))
        else:
            self.statusBar().showMessage(message, 10000)

    def _extract_setbas_archive(self) -> None:
        if self._setbas is None:
            return
        from PySide6.QtWidgets import QDialog, QDialogButtonBox

        dialog = QDialog(self)
        dialog.setWindowTitle("Extract SET.BAS archive")
        layout = QVBoxLayout(dialog)
        info = QLabel(
            f"Archive: {self._setbas.path}\n"
            f"{len(self._setbas.resources)} EMRS resources. Extraction is "
            "read-only for the archive;\nfiles go into raw/VBMP, raw/SKLT, "
            "raw/ANM plus manifest.json. Textures can also be converted "
            "automatically into textures_ilbm/ and textures_png/.")
        info.setWordWrap(True)
        layout.addWidget(info)
        all_check = QCheckBox("All classes (unchecked: textures only)")
        all_check.setChecked(True)
        ilbm_check = QCheckBox(
            "Convert embedded VBMP textures to usable ILBM "
            "(textures_ilbm/)")
        ilbm_check.setChecked(True)
        png_check = QCheckBox(
            "Also convert textures to indexed PNG (textures_png/)")
        png_check.setChecked(True)
        kids_check = QCheckBox(
            "Export raw BASE/KIDS chunks (slow, developer mode)")
        meta_check = QCheckBox("Export BASE/KIDS Scene Metadata JSON")
        csv_check = QCheckBox("Write manifest.csv next to manifest.json")
        for check in (all_check, ilbm_check, png_check, kids_check, meta_check,
                      csv_check):
            layout.addWidget(check)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        out_dir = QFileDialog.getExistingDirectory(
            self, "Output folder for the extraction",
            str(self._last_directory))
        if not out_dir:
            return
        from studio.assets.setbas_export import extract_archive
        self._notify("Extracting archive", 0)
        try:
            summary = extract_archive(
                self._setbas, out_dir,
                all_classes=all_check.isChecked(),
                convert_ilbm=ilbm_check.isChecked(),
                convert_png=png_check.isChecked(),
                export_base_kids=kids_check.isChecked(),
                export_metadata=meta_check.isChecked(),
                manifest_csv="manifest.csv" if csv_check.isChecked() else "",
                log=self._log,
            )
        except Exception as exc:
            QMessageBox.critical(self, "Extraction failed",
                                 f"The archive was not modified.\n\n{exc}")
            return
        self._last_directory = Path(out_dir)
        self._remember_output_folder(out_dir)
        message = (f"{summary['extracted']}/{summary['total']} resources "
                   f"extracted, {summary['duplicates']} duplicate name(s), "
                   f"{summary['errors']} error(s)")
        if ilbm_check.isChecked():
            message += (f"; ILBM: {summary['ilbm_converted']} converted, "
                        f"{summary['ilbm_errors']} error(s)")
        if png_check.isChecked():
            message += (f"; PNG: {summary['png_converted']} converted, "
                        f"{summary['png_errors']} error(s)")
        self._log(f"SET.BAS extraction: {message}")
        self._notify(f"{message} | Output: {out_dir}", 15000)

    def _export_runtime_loose_set(self) -> None:
        if self._setbas is None:
            QMessageBox.information(
                self, "No SET.BAS loaded",
                "Open a SET.BAS provider before exporting Runtime Loose data.")
            return
        from studio.assets.setbas_export import SetBasExportError, export_runtime_loose, infer_runtime_loose_root, resolve_runtime_loose_root

        suggested = infer_runtime_loose_root(self._setbas.path)
        initial_target = (suggested.parent if suggested is not None
                          else self._last_directory)
        dialog = QDialog(self)
        dialog.setWindowTitle("Export Runtime Loose SET")
        dialog.setMinimumWidth(680)
        layout = QVBoxLayout(dialog)

        target_hint = QLabel(
            "Select the destination SetN folder. Loose/ is created "
            "automatically.")
        target_hint.setWordWrap(True)
        layout.addWidget(target_hint)

        target_row = QHBoxLayout()
        target_edit = QLineEdit(str(initial_target))
        target_edit.setPlaceholderText("Data/SetN or Data/Sets/SetN")
        target_edit.setMinimumWidth(500)
        browse_button = QPushButton("Browse")
        browse_button.setMinimumWidth(100)

        def browse_target() -> None:
            selected = QFileDialog.getExistingDirectory(
                self, "Select the target Data/SetN folder",
                target_edit.text() or str(self._last_directory))
            if selected:
                target_edit.setText(selected)

        browse_button.clicked.connect(browse_target)
        target_row.addWidget(target_edit, 1)
        target_row.addWidget(browse_button)
        layout.addLayout(target_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        try:
            loose_root, _set_id = resolve_runtime_loose_root(
                target_edit.text().strip())
            self._notify("Exporting Runtime Loose SET", 0)
            QApplication.processEvents()
            summary = export_runtime_loose(
                self._setbas, loose_root,
                dry_run=False,
                overwrite=False,
                log=self._log)
        except (SetBasExportError, OSError, ValueError) as exc:
            QMessageBox.critical(
                self, "Runtime Loose export failed",
                f"SET.BAS was not modified.\n\n{exc}")
            return

        validation = summary["validation"]
        message = (
            f"Export: {summary['exported']} exported, "
            f"{summary['already_current']} already current, "
            f"{summary['planned']} planned, {summary['skipped']} left to "
            f"fallback, {summary['errors']} error(s). Layout validation: "
            f"{'PASS' if validation['valid'] else 'FAIL'} "
            f"({validation['checked']} checked).")
        self._last_directory = loose_root
        self._remember_output_folder(loose_root)
        issue_text = ""
        if validation["issues"]:
            issue_text = "\n\n" + "\n".join(validation["issues"][:12])
        self._log(f"Runtime Loose SET: {message}")
        self._notify(f"{message} | Target: {loose_root}", 18000)
        if summary["errors"] or not validation["valid"]:
            QMessageBox.warning(
                self, "Runtime Loose export needs attention",
                f"{message}\n\nTarget: {loose_root}{issue_text}")

    def _export_setbas_metadata(self) -> None:
        if self._setbas is None:
            QMessageBox.information(
                self, "No SET.BAS loaded",
                "Open a SET.BAS provider before exporting scene metadata.")
            return
        out_dir = QFileDialog.getExistingDirectory(
            self, "Output folder for SET.BAS scene metadata",
            str(self._last_directory))
        if not out_dir:
            return
        metadata_dir = Path(out_dir) / "metadata"
        try:
            import studio.formats.base_format as base_kids_export
            summary = base_kids_export.write_outputs(
                Path(self._setbas.path), metadata_dir)
        except Exception as exc:
            QMessageBox.critical(
                self, "Metadata export failed",
                f"The source archive was not modified.\n\n{exc}")
            return
        self._last_directory = Path(out_dir)
        self._remember_output_folder(out_dir)
        message = (
            f"Metadata exported: {summary['node_count']} object node(s), "
            f"{summary['texture_ref_count']} texture reference(s), "
            f"{summary['skeleton_ref_count']} skeleton reference(s), "
            f"{summary['animation_ref_count']} animation reference(s), "
            f"{summary['unresolved_count']} unresolved reference(s)."
        )
        self._log(message)
        self._notify(f"{message} | Output: {metadata_dir}", 15000)

    def _preview_family_texture(self, name: str) -> None:
        if self._family is None:
            return
        loaded_name = self._ensure_model_texture_loaded(name)
        image = (self._family.textures.get(loaded_name)
                 if loaded_name is not None else None)
        if image is None or not image.has_body:
            self.statusBar().showMessage("The selected texture is not decoded.")
            return
        qimage = _qimage_from_ilbm(
            image, self._family.external_palette if image.palette is None
            else None)
        if qimage is None or qimage.isNull():
            QMessageBox.warning(self, "Preview failed",
                                f"{name} decoded to an empty image.")
            return
        self._show_image_preview(
            f"Texture preview - {name}",
            f"{name}  |  {image.width} x {image.height}  |  {image.kind}",
            qimage)

    def _export_family_textures_png(self, names: list[str]) -> None:
        if self._family is None:
            self.statusBar().showMessage("Load an asset family first.")
            return
        valid = []
        for name in names:
            loaded_name = self._ensure_model_texture_loaded(name)
            image = (self._family.textures.get(loaded_name)
                     if loaded_name is not None else None)
            valid.append((name, image))
        valid = [(name, image) for name, image in valid
                 if image is not None and image.has_body]
        if not valid:
            self.statusBar().showMessage(
                "Select one or more decoded textures first.")
            return
        from studio.formats.images import TextureConvertError, ilbm_image_to_png

        targets: list[tuple[str, object, Path]] = []
        if len(valid) == 1:
            name, image = valid[0]
            safe = Path(name.replace("\\", "/")).name
            suggested = (Path(self._last_directory)
                         / (Path(safe).stem + ".png"))
            path, _ = QFileDialog.getSaveFileName(
                self, f"Export {name} as indexed PNG", str(suggested),
                "PNG image (*.png)")
            if not path:
                return
            targets.append((name, image, Path(path)))
        else:
            directory = QFileDialog.getExistingDirectory(
                self, f"Output folder for {len(valid)} PNG textures",
                str(self._last_directory))
            if not directory:
                return
            root = Path(directory)
            reserved: set[str] = set()
            for name, image in valid:
                safe = Path(name.replace("\\", "/")).stem + ".png"
                target = self._available_output_path(root / safe, reserved)
                reserved.add(str(target).casefold())
                targets.append((name, image, target))

        errors = []
        written = 0
        for name, image, target in targets:
            try:
                ilbm_image_to_png(
                    image, target,
                    self._family.external_palette
                    if image.palette is None else None)
            except (TextureConvertError, OSError) as exc:
                errors.append(f"{name}: {exc}")
                continue
            written += 1
            self._log(f"texture exported: {name} -> {target}")
        if not written:
            QMessageBox.critical(self, "Export failed",
                                 "\n".join(errors[:12]))
            return
        output_folder = targets[0][2].parent
        self._last_directory = output_folder
        self._remember_output_folder(output_folder)
        message = f"{written} PNG texture(s) written to {output_folder}"
        if errors:
            QMessageBox.warning(
                self, "Export completed with errors",
                message + "\n\n" + "\n".join(errors[:12]))
        self.statusBar().showMessage(message, 8000)

    def _export_texture_png(self) -> None:
        names = self._selected_texture_names()
        if not names:
            item = self.texture_list.currentItem()
            name = item.data(Qt.ItemDataRole.UserRole) if item else None
            names = [name] if name else []
        self._export_family_textures_png(names)

    def _convert_ilbm_to_png_dialog(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "Convert ILBM/VBMP textures to PNG",
            str(self._last_directory),
            "ILBM/VBMP (*.ilbm *.ilb *.iff *.lbm *.vbmp);;All files (*)")
        if not files:
            return
        out_dir = QFileDialog.getExistingDirectory(
            self, "Output folder for the PNG files",
            str(Path(files[0]).parent))
        if not out_dir:
            return
        from studio.formats.images import TextureConvertError, convert_to_png
        converted = 0
        failed = 0
        for source in files:
            try:
                result = convert_to_png(
                    source, Path(out_dir) / (Path(source).stem + ".png"))
            except (TextureConvertError, OSError) as exc:
                failed += 1
                self._log(f"to-png FAILED: {source}: {exc}")
                continue
            converted += 1
            note = f" [{result.warning}]" if result.warning else ""
            self._log(f"to-png: {source} -> {result.output}{note}")
        self._last_directory = Path(out_dir)
        self._remember_output_folder(out_dir)
        self.statusBar().showMessage(
            f"PNG conversion: {converted} ok, {failed} failed (see Log)",
            8000)

    def _convert_vbmp_to_ilbm_dialog(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "Convert VBMP textures to standalone ILBM",
            str(self._last_directory),
            "VBMP textures (*.vbmp *.ilbm *.ilb *.iff *.lbm);;All files (*)")
        if not files:
            return
        out_dir = QFileDialog.getExistingDirectory(
            self, "Output folder for standalone ILBM textures",
            str(Path(files[0]).parent))
        if not out_dir:
            return
        from studio.formats.images import TextureConvertError, convert_vbmp_to_ilbm

        converted = 0
        failed = 0
        for source in files:
            target = Path(out_dir) / (Path(source).stem + ".ILBM")
            target = self._available_output_path(target)
            try:
                result = convert_vbmp_to_ilbm(source, target)
            except (TextureConvertError, OSError) as exc:
                failed += 1
                self._log(f"vbmp-to-ilbm FAILED: {source}: {exc}")
                continue
            converted += 1
            note = f" [{result.warning}]" if result.warning else ""
            self._log(f"vbmp-to-ilbm: {source} -> {result.output}{note}")
        self._last_directory = Path(out_dir)
        self._remember_output_folder(out_dir)
        self.statusBar().showMessage(
            f"VBMP to ILBM: {converted} ok, {failed} failed (see Log)",
            10000)

    def _convert_png_to_ilbm_dialog(self) -> None:
        png_files, _ = QFileDialog.getOpenFileNames(
            self, "Select edited PNG textures", str(self._last_directory),
            "PNG images (*.png *.PNG);;All files (*)")
        if not png_files:
            return

        template_dir = QFileDialog.getExistingDirectory(
            self, "Select the folder containing matching original ILBM "
            "templates", str(Path(png_files[0]).parent))
        if not template_dir:
            return
        template_root = Path(template_dir)
        suffixes = {".ilbm", ".ilb", ".iff", ".lbm"}
        templates = {
            path.stem.lower(): path
            for path in sorted(template_root.rglob("*"))
            if path.is_file() and path.suffix.lower() in suffixes
        }
        if not templates:
            QMessageBox.warning(
                self, "No templates found",
                f"No ILBM template files were found in:\n{template_root}")
            return

        output_pairs: list[tuple[Path, Path | None, Path]] = []
        png_paths = [Path(path) for path in png_files]
        if len(png_paths) == 1:
            png = png_paths[0]
            template = templates.get(png.stem.lower())
            if template is None:
                QMessageBox.warning(
                    self, "Matching template missing",
                    f"No ILBM named {png.stem} was found in {template_root}.")
                return
            suggested = png.with_suffix(template.suffix or ".ILBM")
            out, _ = QFileDialog.getSaveFileName(
                self, "Output ILBM path (never the template)",
                str(suggested),
                "ILBM (*.ilbm *.ILBM *.ilb *.ILB);;All files (*)")
            if not out:
                return
            output_pairs.append((png, template, Path(out)))
        else:
            out_dir = QFileDialog.getExistingDirectory(
                self, "Output folder for converted ILBM textures",
                str(png_paths[0].parent))
            if not out_dir:
                return
            root = Path(out_dir)
            reserved: set[str] = set()
            for png in png_paths:
                template = templates.get(png.stem.lower())
                suffix = (template.suffix if template is not None
                          and template.suffix else ".ILBM")
                candidate = root / png.with_suffix(suffix).name
                target = self._available_output_path(candidate, reserved)
                reserved.add(str(target).casefold())
                output_pairs.append((png, template, target))

        from studio.formats.images import TextureConvertError, convert_png_to_ilbm

        converted = 0
        skipped = 0
        failed = 0
        warnings = 0
        for png, template, out in output_pairs:
            if template is None:
                skipped += 1
                self._log(
                    f"png-to-ilbm SKIPPED: no matching template for {png}")
                continue
            try:
                if out.resolve() == template.resolve():
                    raise TextureConvertError(
                        "output must not overwrite the template ILBM")
                result = convert_png_to_ilbm(png, template, out)
            except (TextureConvertError, OSError) as exc:
                failed += 1
                self._log(f"png-to-ilbm FAILED: {png}: {exc}")
                continue
            converted += 1
            if result.warning:
                warnings += 1
            note = f" [{result.warning}]" if result.warning else ""
            self._log(f"png-to-ilbm: {png} -> {out}{note}")

        output_folder = output_pairs[0][2].parent
        self._last_directory = output_folder
        self._remember_output_folder(output_folder)
        message = (f"PNG to ILBM: {converted} converted, {skipped} skipped, "
                   f"{failed} failed, {warnings} warning(s)")
        self._notify(f"{message} | Output: {output_folder}", 15000)
        if failed or skipped:
            QMessageBox.warning(
                self, "Conversion completed with issues",
                message + "\n\nSee Diagnostics > Log.")

    def _toggle_play(self, playing: bool) -> None:
        self.viewport.play_animation(playing)
        self.play_button.setText("Pause" if playing else "Play")
        self._notify("Animation playing." if playing else
                     "Animation paused.", 3500)

    def _on_animation_speed_changed(self, speed: float) -> None:
        self.viewport.set_animation_speed(speed)
        self._notify(f"Animation speed set to {speed:.2f}x.", 3000)

    def _on_view_mode_changed(self, _index: int) -> None:
        mode = self.mode_combo.currentData()
        self.viewport.set_mode(mode)
        self._notify(
            f"Viewport mode changed to {self.mode_combo.currentText()}.",
            3500)

    def _sync_animation_controls(self) -> None:
        """Match controls and playback to the rendered selected subtree.

        Rebuilding the selected subtree recreates viewport materials and stops
        its timer.  When the toolbar still says Pause, resume the timer instead
        of leaving a false playing state in the UI.
        """

        has_anim = self.viewport.has_animation
        self.play_button.setEnabled(has_anim)
        self.step_button.setEnabled(has_anim)
        self.speed_spin.setEnabled(has_anim)
        self._update_snapshot_frame_text()
        if not has_anim:
            if self.play_button.isChecked():
                self.play_button.setChecked(False)
            self.play_button.setText("Play")
            self.viewport.play_animation(False)
            return
        self.play_button.setText(
            "Pause" if self.play_button.isChecked() else "Play")
        self.viewport.play_animation(self.play_button.isChecked())

    def _on_nested_tab_changed(self, _index: int) -> None:
        self._on_right_tab_changed(self._right_tabs.currentIndex())

    def _active_editor_panel(self):
        if self._right_tabs.currentWidget() is not self._editor_tabs:
            return None
        return self._editor_tabs.currentWidget()

    def _has_editable_model(self) -> bool:
        """Return whether the selected model can enter in-memory Edit Mode.

        SET.BAS ownership controls where data may be written, not whether the
        decoded model may be edited in memory.  Archive-backed models therefore
        use the same GeometryEditSession as loose models; verified export paths
        remain the only way to persist those edits.
        """

        if self._family is None:
            return False
        owner = self._selected_owner
        obj = self._owner_to_obj.get(owner) if owner else None
        return bool(
            obj is not None and getattr(obj, "skeleton", None) is not None)

    def _family_entry_is_setbas_archive(self) -> bool:
        """True only for a model whose BASE entry still lives in SET.BAS."""

        family = self._family
        archive = self._setbas
        if family is None or archive is None or family.base_path is None:
            return False
        try:
            return (Path(family.base_path).resolve()
                    == Path(archive.path).resolve())
        except OSError:
            return False

    @staticmethod
    def _archive_read_only_message(action: str = "edited") -> str:
        return (
            "SET.BAS is a read-only archive provider. The selected model may "
            f"be {action} in memory, but SET.BAS is never overwritten. Use "
            "Export Asset Family (or an explicit BASE/SKLT export) to keep "
            "the changes.")

    def _selected_model_is_archive_read_only(self) -> bool:
        """Return True only when the selected model comes from SET.BAS."""

        if self._family is None:
            return False
        owner = self._selected_owner
        obj = self._owner_to_obj.get(owner) if owner else None
        return bool(
            obj is not None and getattr(obj, "skeleton", None) is not None
            and self._is_embedded_geometry_source(obj))

    def _editing_allowed(self) -> bool:
        """Single source of truth for every model-mutating operation."""

        button = getattr(self, "edit_toggle_action", None)
        return bool(
            button is not None and button.isChecked()
            and self.viewport.is_edit_mode
            and not self._snapshot_mode_active
            and self._has_editable_model())

    def _require_editing(self, action: str) -> bool:
        if self._editing_allowed():
            return True
        self._notify(
            f"{action} is available only in Edit Mode. View Mode is "
            "strictly read-only.", 6000)
        return False

    def _selected_polygon_vertices(self) -> tuple[str, list[int]] | None:
        obj = self._workbench_obj
        poly_id = self._selected_poly
        if obj is None or obj.skeleton is None or poly_id is None \
                or not (0 <= poly_id < len(obj.skeleton.polygons)):
            return None
        element = next(
            (candidate for candidate in self._fx_elements
             if candidate.owner_path == obj.owner_path
             and poly_id in candidate.poly_ids),
            None)
        if element is not None and not element.editable:
            return None
        selected = self._resolved_geometry_polys() or {poly_id}
        vertices = {
            vertex
            for selected_poly in selected
            if 0 <= selected_poly < len(obj.skeleton.polygons)
            for vertex in obj.skeleton.polygons[selected_poly]
        }
        return obj.owner_path, sorted(vertices)

    def _sync_editor_context(self) -> None:
        edit_button = getattr(self, "edit_toggle_action", None)
        if edit_button is None or not edit_button.isChecked():
            if self.viewport.is_edit_mode:
                self.viewport.exit_edit_mode()
            return
        panel = self._active_editor_panel()
        if panel is self._model_editor_panel:
            self.viewport.set_highlight_polys(set())
            owner = self._selected_owner
            saved_selection = self._tab_edit_selection.pop(
                owner, None) if owner else None
            if saved_selection is not None:
                self._remember_geometry_original(owner)
                entered = (
                    self.viewport.enter_edit_mode_with_vertices(
                        owner, saved_selection, pick_polygons=True)
                    if saved_selection
                    else self.viewport.enter_edit_mode(owner))
                if entered:
                    self.viewport.configure_edit_interaction(
                        selected_only=False, pick_polygons=True)
                    return
            target = self._selected_polygon_vertices()
            if target is not None:
                owner, vertices = target
                self._remember_geometry_original(owner)
                self.viewport.enter_edit_mode_with_vertices(
                    owner, vertices, pick_polygons=True)
                self.viewport.configure_edit_interaction(
                    selected_only=False, pick_polygons=True)
                return

        owner = self._selected_owner
        self._remember_geometry_original(owner)
        if self.viewport.is_edit_mode and self.viewport.edit_owner != owner:
            self.viewport.exit_edit_mode()
        if not self.viewport.is_edit_mode:
            self.viewport.enter_edit_mode(owner)
        self.viewport.configure_edit_interaction(
            selected_only=False, pick_polygons=True)

    def _sync_tab_edit_mode(self) -> None:
        """Apply the Editor=Edit / other tabs=View contract without data loss."""

        if self.viewport.paste_preview_active:
            return
        active = (
            self._active_editor_panel() is self._model_editor_panel
            and self._has_editable_model()
            and not self._snapshot_mode_active)
        button = self.edit_toggle_action
        if active:
            if not button.isChecked():
                button.blockSignals(True)
                button.setChecked(True)
                button.blockSignals(False)
            self._sync_editor_context()
            return
        session = self.viewport.edit_session
        if session is not None and self.viewport.edit_owner is not None:
            self._tab_edit_selection[self.viewport.edit_owner] = set(
                session.selection)
        if button.isChecked():
            button.blockSignals(True)
            button.setChecked(False)
            button.blockSignals(False)
        self._cancel_live_transforms()
        if self.viewport.is_edit_mode:
            self.viewport.exit_edit_mode()

    def _snapshot_panel_is_active(self) -> bool:
        """Return whether the shared Snapshot panel is currently visible.

        The standard workbench keeps Snapshot inside the nested Visuals tabs.
        Focused workspaces can override this small routing hook while reusing
        the same Snapshot-mode lifecycle and renderer.
        """

        tabs = getattr(self, "_right_tabs", None)
        snapshot_panel = getattr(self, "_snapshot_panel", None)
        visuals_tabs = getattr(self, "_visuals_tabs", None)
        return bool(
            tabs is not None and visuals_tabs is not None
            and snapshot_panel is not None
            and tabs.currentWidget() is visuals_tabs
            and visuals_tabs.currentWidget() is snapshot_panel)

    def _on_right_tab_changed(self, index: int) -> None:
        if self.viewport.paste_preview_active:
            self._right_tabs.blockSignals(True)
            self._editor_tabs.blockSignals(True)
            self._right_tabs.setCurrentWidget(self._editor_tabs)
            self._editor_tabs.setCurrentWidget(self._model_editor_panel)
            self._editor_tabs.blockSignals(False)
            self._right_tabs.blockSignals(False)
            self._notify(
                "Finish or cancel Paste Geometry before changing panels.",
                5000)
            return
        self._sync_animation_controls()
        tabs = getattr(self, "_right_tabs", None)
        entering = self._snapshot_panel_is_active()
        if entering and not self._snapshot_mode_active:
            self._snapshot_mode_active = True
            self.viewport.begin_snapshot_mode(self._snapshot_background())
            self._snapshot_zoom_percent = 100
            for widget in (self.snapshot_zoom_spin,
                           self.snapshot_zoom_slider):
                widget.blockSignals(True)
                widget.setValue(100)
                widget.blockSignals(False)
            self._snapshot_zoom_reference = self.viewport.camera_zoom
            self._enter_snapshot_view_profile()
            snapshot_mode = "textured"
            self.viewport.set_mode(snapshot_mode)
            self.mode_combo.blockSignals(True)
            self.mode_combo.setCurrentIndex(
                self.mode_combo.findData(snapshot_mode))
            self.mode_combo.blockSignals(False)
            self.mode_combo.setEnabled(False)
            self.edit_toggle_action.setEnabled(False)
            self.toolbar_view_preset_combo.setEnabled(False)
            self._on_snapshot_preset_changed(
                self.snapshot_view_combo.currentText())
        elif not entering and self._snapshot_mode_active:
            self._snapshot_mode_active = False
            restored_mode = self.viewport.end_snapshot_mode()
            restored_index = self.mode_combo.findData(restored_mode)
            if restored_index >= 0:
                self.mode_combo.blockSignals(True)
                self.mode_combo.setCurrentIndex(restored_index)
                self.mode_combo.blockSignals(False)
            self.mode_combo.setEnabled(True)
            self.edit_toggle_action.setEnabled(True)
            self.toolbar_view_preset_combo.setEnabled(True)
            self._leave_snapshot_view_profile()
            self._snapshot_zoom_reference = None
        self._sync_tab_edit_mode()
        if self._right_tabs.currentWidget() is self._editor_tabs \
                and self._selected_model_is_archive_read_only():
            self._notify(self._archive_read_only_message(), 9000)
        if tabs is not None:
            outer_index = tabs.currentIndex()
            outer = tabs.tabText(outer_index)
            nested = tabs.currentWidget()
            if isinstance(nested, QTabWidget):
                inner = nested.tabText(nested.currentIndex())
                self._notify(f"Opened {outer} > {inner}.", 3500)
            else:
                self._notify(f"Opened {outer}.", 3500)

    def _set_snapshot_custom_size_visible(self, visible: bool) -> None:
        for widget in (
                self.snapshot_width_label, self.snapshot_width_spin,
                self.snapshot_height_label, self.snapshot_height_spin):
            widget.setVisible(visible)

    def _snapshot_output_size(self) -> QSize:
        choice = self.snapshot_size_combo.currentText()
        if choice == "Current Viewport":
            return QSize(max(1, self.viewport.width()),
                         max(1, self.viewport.height()))
        if choice == "Custom":
            return QSize(self.snapshot_width_spin.value(),
                         self.snapshot_height_spin.value())
        width, height = choice.lower().split(" x ", 1)
        return QSize(int(width), int(height))

    def _on_snapshot_size_changed(self, _value=None) -> None:
        custom = self.snapshot_size_combo.currentText() == "Custom"
        self._set_snapshot_custom_size_visible(custom)

    def _on_snapshot_zoom_changed(self, value: int) -> None:
        value = max(25, min(300, int(value)))
        for widget in (self.snapshot_zoom_spin, self.snapshot_zoom_slider):
            if widget.value() != value:
                widget.blockSignals(True)
                widget.setValue(value)
                widget.blockSignals(False)
        previous = self._snapshot_zoom_percent
        self._snapshot_zoom_percent = value
        if self._snapshot_mode_active and previous > 0:
            self.viewport.adjust_snapshot_zoom(value / previous)

    def _sync_snapshot_zoom_from_viewport(self) -> None:
        """Reflect wheel/gesture zoom in the shared Snapshot controls."""

        reference = self._snapshot_zoom_reference
        if not self._snapshot_mode_active or not reference or reference <= 0:
            return
        value = round(self.viewport.camera_zoom / reference * 100.0)
        value = max(25, min(300, value))
        self._snapshot_zoom_percent = value
        for widget in (self.snapshot_zoom_spin, self.snapshot_zoom_slider):
            if widget.value() != value:
                widget.blockSignals(True)
                widget.setValue(value)
                widget.blockSignals(False)

    def _view_option_actions(self) -> tuple[QAction, ...]:
        """Return the one shared View-option set used by every workspace."""

        return (
            self.sen_check, self.wire_check, self.cull_check, self.axes_check,
            self.grid_check, self.overlay_check, self.mapping_diag_check,
            self.retail_distance_fade_check,
        )

    def _enter_snapshot_view_profile(self) -> None:
        """Use shared View actions with Snapshot-specific clean defaults."""

        actions = self._view_option_actions()
        if self._snapshot_view_action_state is None:
            self._snapshot_view_action_state = {
                action: action.isChecked() for action in actions}
        defaults = {
            self.sen_check: False,
            self.wire_check: False,
            self.cull_check: True,
            self.axes_check: False,
            self.grid_check: False,
            self.overlay_check: False,
            self.mapping_diag_check: False,
            self.retail_distance_fade_check: False,
        }
        for action in actions:
            action.setVisible(True)
            action.setEnabled(True)
            action.setChecked(defaults[action])
        self._sync_snapshot_view_render_mode()

    def _leave_snapshot_view_profile(self) -> None:
        state = self._snapshot_view_action_state
        if not state:
            return
        self._snapshot_view_action_state = None
        for action, checked in state.items():
            action.setEnabled(True)
            action.setChecked(checked)

    def _snapshot_include_guides(self) -> bool:
        return any(action.isChecked() for action in (
            self.sen_check, self.wire_check, self.axes_check, self.grid_check,
            self.overlay_check, self.mapping_diag_check))

    def _sync_snapshot_view_render_mode(self, _checked: bool = False) -> None:
        """Make the Snapshot preview honor the shared View QAction state."""

        if not self._snapshot_mode_active:
            return
        self.viewport.set_snapshot_shared_overlays_enabled(
            self._snapshot_include_guides())

    def _on_snapshot_preset_changed(self, preset: str) -> None:
        if not self._snapshot_mode_active:
            return
        if preset == "Current View" and self._snapshot_zoom_percent != 100:
            self._snapshot_zoom_percent = 100
            for widget in (self.snapshot_zoom_spin,
                           self.snapshot_zoom_slider):
                widget.blockSignals(True)
                widget.setValue(100)
                widget.blockSignals(False)
        self.viewport.apply_snapshot_preset(
            preset, self._snapshot_output_size(),
            self._snapshot_zoom_percent)
        if self._snapshot_zoom_percent > 0:
            self._snapshot_zoom_reference = (
                self.viewport.camera_zoom
                / (self._snapshot_zoom_percent / 100.0))

    def _on_toolbar_view_preset_changed(self, preset: str) -> None:
        if self._snapshot_mode_active:
            return
        self.viewport.apply_view_preset(
            preset,
            QSize(max(1, self.viewport.width()),
                  max(1, self.viewport.height())))
        self._sync_gizmo_camera()
        self._update_reset_camera_action()
        self._notify(f"View preset changed to {preset}.", 3500)

    def _on_manual_camera_changed(self) -> None:
        """Mark the active preset as Current View after user navigation."""

        self._sync_gizmo_camera()
        self._update_reset_camera_action()
        if self._snapshot_mode_active:
            self._sync_snapshot_zoom_from_viewport()
        combo = (self.snapshot_view_combo if self._snapshot_mode_active
                 else self.toolbar_view_preset_combo)
        if combo.currentText() == "Current View":
            return
        combo.blockSignals(True)
        combo.setCurrentText("Current View")
        combo.blockSignals(False)

    def _sync_gizmo_camera(self) -> None:
        gizmo = getattr(self, "model_gizmo", None)
        if gizmo is not None:
            yaw, pitch = self.viewport.camera_orientation
            gizmo.set_camera_orientation(yaw, pitch)
            gizmo.set_model_matrix(self.viewport.edit_model_matrix)

    def _update_reset_camera_action(self) -> None:
        action = getattr(self, "reset_camera_action", None)
        enabled = self.viewport.can_reset_camera
        if action is not None:
            action.setEnabled(enabled)
        button = getattr(self, "toolbar_reset_view_button", None)
        if button is not None:
            button.setEnabled(enabled)

    def _reset_view_and_gizmo(self) -> None:
        if not self.viewport.can_reset_camera:
            self._update_reset_camera_action()
            return
        self.viewport.reset_view()
        combo = (self.snapshot_view_combo if self._snapshot_mode_active
                 else self.toolbar_view_preset_combo)
        combo.blockSignals(True)
        combo.setCurrentText("Current View")
        combo.blockSignals(False)
        if self._snapshot_mode_active:
            self._sync_snapshot_zoom_from_viewport()
        self._sync_gizmo_camera()
        self._update_reset_camera_action()

    def _snapshot_background(self) -> QColor | None:
        if self._snapshot_custom_color is None:
            return None
        color = QColor(self._snapshot_custom_color)
        color.setAlpha(255)
        return color

    def _update_snapshot_color_button(self) -> None:
        if not hasattr(self, "snapshot_color_button"):
            return
        color = self._snapshot_custom_color
        if color is None:
            self.snapshot_color_button.setText("None")
            self.snapshot_color_button.setStyleSheet("")
            self.snapshot_clear_color_button.setEnabled(False)
            return
        self.snapshot_color_button.setText("")
        self.snapshot_color_button.setStyleSheet(
            f"background-color: {color.name()}; border: 1px solid #b0b0b0;")
        self.snapshot_clear_color_button.setEnabled(True)

    def _choose_snapshot_color(self) -> None:
        initial = (self._snapshot_custom_color
                   if self._snapshot_custom_color is not None
                   else QColor(96, 96, 96))
        color = QColorDialog.getColor(
            initial, self, "Snapshot background",
            QColorDialog.ColorDialogOption.DontUseNativeDialog)
        if not color.isValid():
            return
        self._snapshot_custom_color = color
        self._update_snapshot_color_button()
        self.viewport.set_snapshot_background(self._snapshot_background())

    def _clear_snapshot_color(self) -> None:
        self._snapshot_custom_color = None
        self._update_snapshot_color_button()
        self.viewport.set_snapshot_background(None)

    def _update_snapshot_frame_text(self, _text: str = "") -> None:
        if not hasattr(self, "snapshot_frame_label"):
            return
        has_animation = self.viewport.has_animation
        if has_animation:
            # The viewport may animate several materials at once. Snapshot
            # Studio only needs one compact visual reference; keep the full
            # multi-animation state in the viewport and display its first
            # active animation instead of duplicating every material instance.
            full_text = self.viewport.current_frame_text()
            text = full_text.split("; ", 1)[0] if full_text else "No animation"
        else:
            text = "No animation"
        self.snapshot_frame_label.setText(
            f"Current Animation Frame: {text}")
        self.snapshot_next_frame_button.setEnabled(has_animation)
        self.snapshot_reset_frame_button.setEnabled(has_animation)
        self.snapshot_previous_frame_button.setEnabled(has_animation)

    def _on_animation_frame_changed(self, text: str = "") -> None:
        self._update_snapshot_frame_text(text)
        element = self._fx_element_for_polygon(self._selected_poly)
        if element is not None and element.source_kind == "VANM":
            self._fill_fx_inspector(element, self._selected_poly)
            return
        if self._mapping_index is not None and self._selected_poly is not None:
            refs = self._mapping_index.refs.get(self._selected_poly, [])
            if len(refs) == 1 and refs[0].block.texture is not None \
                    and refs[0].block.texture.kind == "bmpanim":
                self._update_uv_editor(self._selected_poly)
                return
        context = getattr(self, "_uv_ctx", None)
        if context is None:
            return
        _fam_obj, block, _block_index, _atts_index, poly_id = context
        if block.texture is not None \
                and block.texture.kind == "bmpanim" and not block.olpl:
            self._update_uv_editor(poly_id)

    def _on_snapshot_format_changed(self, _index: int) -> None:
        format_id = self.snapshot_format_combo.currentData()
        self.snapshot_quality_box.setEnabled(
            format_id in ("jpg", "webp", "webp_anim"))
        animated = is_animated_format(format_id)
        self.snapshot_animation_rows.set_expanded(animated)
        self.snapshot_fps_box.setEnabled(animated)
        self.snapshot_export_speed_box.setEnabled(animated)
        self.snapshot_duration_check.setEnabled(animated)
        self.snapshot_duration_box.setEnabled(
            animated and self.snapshot_duration_check.isChecked())

    def _snapshot_export_options(self) -> dict[str, object]:
        return {
            "format_id": self.snapshot_format_combo.currentData(),
            "quality": self.snapshot_quality_box.value(),
            "fps": self.snapshot_fps_box.value(),
            "speed": self.snapshot_export_speed_box.value(),
            "duration_seconds": (
                self.snapshot_duration_box.value()
                if self.snapshot_duration_check.isChecked() else None),
            "name_mode": self.snapshot_naming_combo.currentData(),
        }

    def _snapshot_source_info(self) -> tuple[int | None, str]:
        obj = self._owner_to_obj.get(self._selected_owner)
        base_object = getattr(obj, "base_object", None)
        resource_name = str(
            self._base_entry_names.get(self._selected_owner)
            or getattr(base_object, "name", "")
            or getattr(base_object, "skeleton_name", "") or "")
        if not resource_name:
            display_name = str(getattr(obj, "display_name", "") or "")
            if not display_name and self._family is not None:
                source_path = (self._family.base_path
                               or self._family.setbas_path)
                display_name = source_path.stem if source_path else ""
            resource_name = display_name or "Snapshot"

        resource_stem = Path(resource_name.replace("\\", "/")).stem
        resource_stem = re.sub(
            r'[<>:"/\\|?*\x00-\x1f]+', "_", resource_stem).strip(" ._")
        resource_stem = resource_stem or "Snapshot"
        vp_id = None
        table = getattr(self, "_vp_table", None)
        if resource_name and table is not None:
            key = normalize_base_name(resource_name)
            entry = next((
                item for item in table.entries
                if normalize_base_name(item.base_name) == key
            ), None)
            if entry is not None:
                vp_id = int(entry.index)
        return vp_id, resource_stem

    def _snapshot_name(self, name_mode: str | None = None) -> str:
        vp_id, resource_name = self._snapshot_source_info()
        if name_mode is None:
            combo = getattr(self, "snapshot_naming_combo", None)
            name_mode = combo.currentData() if combo is not None else "resource"
        if name_mode == "vp_id" and vp_id is not None:
            return f"VP_{vp_id:04d}"
        return resource_name

    def _restore_snapshot_export_state(
            self, camera_state: dict,
            animation_state: dict | None = None) -> None:
        """Restore the viewport even if camera or animation cleanup fails."""

        try:
            self.viewport._set_camera_state(camera_state)
        finally:
            try:
                if animation_state is not None:
                    self.viewport.restore_animation_state(animation_state)
            finally:
                self.viewport.update()

    def _export_snapshot(self) -> None:
        if not self.viewport.has_model:
            QMessageBox.information(self, "No model loaded",
                                    "Load a model before exporting an image.")
            return
        options = self._snapshot_export_options()
        format_id = str(options["format_id"])
        animated = is_animated_format(format_id)
        if animated and not self.viewport.has_animation:
            QMessageBox.warning(
                self, "No animation to export",
                "The selected animated format requires an animated model.")
            return
        extension = output_extension(format_id)
        preset = self.snapshot_view_combo.currentText().replace(" ", "")
        suggested = self._last_directory / (
            f"{self._snapshot_name(str(options['name_mode']))}_"
            f"{preset}.{extension}")
        labels = {
            "png": "PNG (*.png)",
            "jpg": "JPEG (*.jpg)",
            "webp": "WebP (*.webp)",
            "gif": "GIF animation (*.gif)",
            "webp_anim": "WebP animation (*.webp)",
        }
        path_text, _selected_filter = QFileDialog.getSaveFileName(
            self, "Export Snapshot", str(suggested), labels[format_id],
            options=QFileDialog.Option.DontConfirmOverwrite)
        if not path_text:
            return
        path = Path(path_text)
        if path.suffix.lower() != f".{extension}":
            path = path.with_suffix(f".{extension}")
        if path.exists():
            answer = QMessageBox.question(
                self, "Replace existing file?",
                f"The file already exists:\n{path}\n\nReplace it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        background = self._snapshot_background()
        if format_id == "jpg" and background is None:
            background = QColor(96, 96, 96)
        size = self._snapshot_output_size()
        if animated:
            progress = QProgressDialog(
                "Rendering animation frames...", "Cancel", 0, 1, self)
            progress.setWindowModality(Qt.WindowModality.WindowModal)
            progress.setMinimumDuration(0)
            camera = self.viewport._camera_state()
            animation_state = self.viewport.capture_animation_state()

            def cancelled() -> bool:
                QApplication.processEvents()
                return progress.wasCanceled()

            def update_progress(current: int, total: int) -> None:
                progress.setRange(0, total)
                progress.setValue(current)
                progress.setLabelText(
                    f"Rendering animation frame {current}/{total}...")

            try:
                progress.show()
                self.viewport.play_animation(False)
                frame_count = export_animation_atomic(
                    self.viewport, path, format_id, size, background,
                    self._snapshot_include_guides(),
                    fps=int(options["fps"]),
                    speed=float(options["speed"]),
                    quality=int(options["quality"]),
                    duration_seconds=options["duration_seconds"],
                    cancelled=cancelled,
                    progress=update_progress)
            except AnimationExportCancelled:
                self.statusBar().showMessage(
                    "Animation export cancelled; no file was written.", 8000)
                return
            except Exception as exc:
                QMessageBox.warning(self, "Export failed", str(exc))
                return
            finally:
                try:
                    self._restore_snapshot_export_state(
                        camera, animation_state)
                finally:
                    progress.close()
        else:
            try:
                image = self.viewport.render_snapshot(
                    size, background,
                    include_guides=self._snapshot_include_guides())
                write_static_image_atomic(
                    image, path, format_id, int(options["quality"]))
                frame_count = 1
            except Exception as exc:
                QMessageBox.warning(self, "Export failed", str(exc))
                return
        self._last_directory = path.parent
        self.statusBar().showMessage(
            f"Snapshot written to {path} ({size.width()} x {size.height()}, "
            f"{frame_count} frame(s)).")

    @staticmethod
    def _write_snapshot_png_atomic(image: QImage, path: Path) -> None:
        """Write one PNG atomically through the shared still-image writer."""

        write_static_image_atomic(image, path, "png")

    def _export_figurine_set(self) -> None:
        """Export the current model from every canonical Snapshot angle."""

        if not self.viewport.has_model:
            QMessageBox.information(
                self, "No model loaded",
                "Load a model before exporting an Image Set.")
            return
        options = self._snapshot_export_options()
        format_id = str(options["format_id"])
        animated = is_animated_format(format_id)
        if animated and not self.viewport.has_animation:
            QMessageBox.warning(
                self, "No animation to export",
                "The selected animated format requires an animated model.")
            return
        extension = output_extension(format_id)
        output = QFileDialog.getExistingDirectory(
            self, "Choose Image Set destination folder",
            str(self._last_directory))
        if not output:
            return
        root = Path(output)
        root.mkdir(parents=True, exist_ok=True)
        model_name = self._snapshot_name(str(options["name_mode"]))
        targets = []
        for index, view in enumerate(VIEW_PRESET_ANGLES, 1):
            slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", view).strip("._")
            targets.append((
                view, root / f"{model_name}_{index:02d}_{slug}.{extension}"))
        existing = [path for _view, path in targets if path.exists()]
        if existing:
            answer = QMessageBox.question(
                self, "Replace existing Image Set images?",
                f"{len(existing)} destination image(s) already exist. "
                "Replace them?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return

        camera = self.viewport._camera_state()
        preset = self.snapshot_view_combo.currentText()
        zoom_percent = self._snapshot_zoom_percent
        zoom_reference = self._snapshot_zoom_reference
        size = self._snapshot_output_size()
        background = self._snapshot_background()
        if format_id == "jpg" and background is None:
            background = QColor(96, 96, 96)
        include_guides = self._snapshot_include_guides()
        written = []
        progress = None
        animation_state = None
        frames_per_view = 1
        if animated:
            try:
                frames_per_view = animation_frame_count(
                    self.viewport.animation_duration_ms,
                    int(options["fps"]),
                    float(options["speed"]),
                    options["duration_seconds"])
            except (TypeError, ValueError) as exc:
                QMessageBox.warning(self, "Image Set export failed", str(exc))
                return
            progress = QProgressDialog(
                "Rendering Image Set animation...", "Cancel", 0,
                frames_per_view * len(targets), self)
            progress.setWindowModality(Qt.WindowModality.WindowModal)
            progress.setMinimumDuration(0)
            animation_state = self.viewport.capture_animation_state()
        try:
            if animated:
                progress.show()
                self.viewport.play_animation(False)
            for view_index, (view, path) in enumerate(targets):
                self.viewport.apply_snapshot_preset(
                    view, size, zoom_percent)
                if animated:
                    offset = view_index * frames_per_view

                    def cancelled() -> bool:
                        QApplication.processEvents()
                        return progress.wasCanceled()

                    def update_progress(current: int, total: int) -> None:
                        progress.setValue(offset + current)
                        progress.setLabelText(
                            f"{view}: frame {current}/{total}")

                    export_animation_atomic(
                        self.viewport, path, format_id, size, background,
                        include_guides,
                        fps=int(options["fps"]),
                        speed=float(options["speed"]),
                        quality=int(options["quality"]),
                        duration_seconds=options["duration_seconds"],
                        cancelled=cancelled,
                        progress=update_progress)
                else:
                    image = self.viewport.render_snapshot(
                        size, background, include_guides=include_guides)
                    write_static_image_atomic(
                        image, path, format_id, int(options["quality"]))
                written.append(path)
        except AnimationExportCancelled:
            self.statusBar().showMessage(
                f"Image Set export cancelled; {len(written)} completed file(s) "
                "were kept.", 8000)
            return
        except Exception as exc:
            QMessageBox.warning(
                self, "Image Set export failed",
                f"{exc}\n\n{len(written)} image(s) were written before "
                "the failure.")
            return
        finally:
            try:
                self._restore_snapshot_export_state(
                    camera, animation_state)
            finally:
                if progress is not None:
                    progress.close()
                self._restore_snapshot_view_controls(
                    preset, zoom_percent, zoom_reference)

        self._last_directory = root
        background_label = (
            "transparent" if background is None
            else f"background {background.name()}")
        self.statusBar().showMessage(
            f"Image Set exported: {len(written)} {extension.upper()} file(s), "
            f"{background_label}.", 10000)

    def _restore_snapshot_view_controls(
            self, preset: str, zoom_percent: int,
            zoom_reference: float | None) -> None:
        """Put the view and zoom controls back after a multi-view export."""

        self.snapshot_view_combo.blockSignals(True)
        self.snapshot_view_combo.setCurrentText(preset)
        self.snapshot_view_combo.blockSignals(False)
        self._snapshot_zoom_percent = zoom_percent
        self._snapshot_zoom_reference = zoom_reference
        for widget in (self.snapshot_zoom_spin, self.snapshot_zoom_slider):
            widget.blockSignals(True)
            widget.setValue(zoom_percent)
            widget.blockSignals(False)

    def _export_contact_sheet(self) -> None:
        """Export the canonical views of the current model as one PNG sheet."""

        if not self.viewport.has_model:
            QMessageBox.information(
                self, "No model loaded",
                "Load a model before exporting a Contact Sheet.")
            return
        suggested = self._last_directory / (
            f"{self._snapshot_name()}_ContactSheet.png")
        path_text, _selected_filter = QFileDialog.getSaveFileName(
            self, "Export Contact Sheet", str(suggested), "PNG (*.png)",
            options=QFileDialog.Option.DontConfirmOverwrite)
        if not path_text:
            return
        path = Path(path_text)
        if path.suffix.lower() != ".png":
            path = path.with_suffix(".png")
        if path.exists():
            answer = QMessageBox.question(
                self, "Replace existing file?",
                f"The file already exists:\n{path}\n\nReplace it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return

        camera = self.viewport._camera_state()
        preset = self.snapshot_view_combo.currentText()
        zoom_percent = self._snapshot_zoom_percent
        zoom_reference = self._snapshot_zoom_reference
        size = self._snapshot_output_size()
        background = self._snapshot_background() or CONTACT_SHEET_BACKGROUND
        include_guides = self._snapshot_include_guides()
        try:
            cells = []
            for label, view in CONTACT_SHEET_VIEWS:
                self.viewport.apply_snapshot_preset(view, size, zoom_percent)
                cells.append((label, self.viewport.render_snapshot(
                    size, background, include_guides=include_guides)))
            sheet = compose_contact_sheet(cells, background)
            write_static_image_atomic(sheet, path, "png")
        except Exception as exc:
            QMessageBox.warning(self, "Contact Sheet export failed", str(exc))
            return
        finally:
            try:
                self._restore_snapshot_export_state(camera)
            finally:
                self._restore_snapshot_view_controls(
                    preset, zoom_percent, zoom_reference)

        self._last_directory = path.parent
        self.statusBar().showMessage(
            f"Contact Sheet written to {path} "
            f"({sheet.width()} x {sheet.height()}).", 10000)

    # -- texture resolution (session-only overrides) -----------------------------

    def _dependency_target_for_item(
            self, item) -> tuple[str, str | None] | None:
        """Return the resolver target carried by one unified tree row."""

        if item is None:
            return None
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data:
            return None
        kind, payload = data
        if kind == "dependency_candidate":
            return payload
        if kind == "dependency_info":
            return payload[0], None
        if kind == "texture" or kind == "animation":
            return str(payload), None
        if kind == "skeleton" and payload is not None:
            name = getattr(payload.base_object, "skeleton_name", "")
            return (name, None) if name else None
        return None

    def _selected_resolve_target(self) -> tuple[str, str | None] | None:
        """Return the resolver target selected in Asset Dependencies."""

        return self._dependency_target_for_item(self.asset_tree.currentItem())

    def _dependency_candidate_paths(self, name: str, dep=None):
        dep = dep or self._dep_for_name(name)
        candidates: list[tuple[str, str]] = []
        for candidate in getattr(dep, "candidates", ()):
            candidates.append((f"Loose: {candidate}", str(candidate)))
        ref = None
        if self._family is not None:
            ref = (self._matching_ref(
                getattr(self._family, "texture_refs", {}), name)
                or self._matching_ref(
                    getattr(self._family, "animation_refs", {}), name))
            if ref is None:
                all_objects = getattr(self._family, "all_objects", lambda: [])
                for fam_obj in all_objects():
                    skeleton_ref = getattr(fam_obj, "skeleton_ref", None)
                    if skeleton_ref is not None and str(
                            skeleton_ref.logical_name).casefold() \
                            == str(name).casefold():
                        ref = skeleton_ref
                        break
        for embedded in getattr(ref, "embedded_candidates", ()):
            candidates.append((f"SET.BAS fallback: {embedded}", embedded))
        return candidates

    def _apply_dependency_candidate(self, name: str, candidate: str) -> None:
        if candidate.startswith("SET.BAS:"):
            bare = name.replace("\\", "/").split("/")[-1]
            self._apply_override(
                bare, SETBAS_OVERRIDE_PREFIX + candidate[len("SET.BAS:"):])
        else:
            self._apply_override(name, candidate)

    def _select_dependency_resource(self) -> None:
        """Choose a resolver candidate, or browse when none is available."""

        target = self._selected_resolve_target()
        if target is None:
            QMessageBox.information(
                self, "No resource selected",
                "Select the missing or ambiguous resource in Asset "
                "Dependencies first.",
            )
            return
        logical_name, _candidate = target
        candidates = self._dependency_candidate_paths(logical_name)
        browse_label = "Browse for a compatible file"
        choices = [label for label, _path in candidates]
        choices.append(browse_label)
        selected, accepted = QInputDialog.getItem(
            self, "Select Resource",
            f"Choose a source for {logical_name}:",
            choices, 0, False,
        )
        if not accepted:
            return
        if selected == browse_label:
            self._assign_manual_file()
            return
        for label, path in candidates:
            if label == selected:
                self._apply_dependency_candidate(logical_name, path)
                return

    def _apply_override(self, logical_name: str, path: str,
                        trial: bool = True) -> None:
        self._overrides[logical_name] = path
        if trial:
            self._trial_names.add(logical_name)
            self._kept_names.discard(logical_name)
        else:
            self._kept_names.add(logical_name)
            self._trial_names.discard(logical_name)
        self._skipped_names.discard(logical_name)
        self.statusBar().showMessage(
            f"{'Trial-load' if trial else 'Session'} override: "
            f"{logical_name} -> {path}"
        )
        self._reload_with_overrides()

    def _keep_for_session(self) -> None:
        target = self._selected_resolve_target()
        if target is None:
            return
        logical_name, _candidate = target
        bare = logical_name.replace("\\", "/").split("/")[-1]
        for key in (logical_name, bare):
            if key in self._trial_names:
                self._trial_names.discard(key)
                self._kept_names.add(key)
                self.statusBar().showMessage(f"Kept for session: {key}")
                self._refresh_asset_dependencies()
                return
        self.statusBar().showMessage(
            "Keep for session applies to a trial-loaded dependency."
        )


    def _apply_diagnostics_filter(self) -> None:
        if self._family is None:
            return
        filtered = [d for d in self._family.textured_diagnostics
                    if not any(name in d for name in self._skipped_names)]
        self.viewport.set_diagnostics(filtered)

    def _family_face_count(self, family: AssetFamily) -> int:
        return sum(len(g.faces) for o in family.all_objects()
                   for g in o.materials)

    def _default_owner(self, family: AssetFamily) -> str | None:
        for obj in family.all_objects():
            if obj.skeleton is not None:
                return obj.owner_path
        return None

    def _family_descendants(self, family: AssetFamily,
                            owner: str | None) -> set[str] | None:
        if owner is None:
            return None
        prefix = owner + "/"
        return {obj.owner_path for obj in family.all_objects()
                if obj.owner_path == owner
                or obj.owner_path.startswith(prefix)}

    def _select_owner(
            self, owner: str | None, from_viewport: bool = False,
            preserve_asset_selection: bool = False) -> None:
        if self.viewport.paste_preview_active \
                and owner != self.viewport.edit_owner:
            self.viewport.cancel_paste_preview()
            self._notify(
                "Paste preview cancelled because the model owner changed.",
                6000)
        edit_button = getattr(self, "edit_toggle_action", None)
        keep_global_edit = bool(edit_button and edit_button.isChecked())
        previous_owner = self._selected_owner
        self._selected_owner = owner
        self.viewport.set_selected_owner(owner)
        # The polygon workbench (picking / inspector / UV editor) follows the
        # selected object so children of huge families are editable too.
        if owner is not None and self._family is not None:
            obj = self._owner_to_obj.get(owner)
            if obj is not None and obj.skeleton is not None \
                    and obj is not self._workbench_obj:
                self._selected_polys.clear()
                self._rebuild_workbench(self._family, owner)
                self.viewport._primary_owner = owner
            self._apply_selected_children_scope()
        # Asset Dependencies is model-scoped.  Rebuild only when the real
        # active owner changes, never for hover/current-row activity inside the
        # dependency viewer itself.
        if owner is not None and owner != previous_owner \
                and self._family is not None:
            self._fill_asset_tree(self._family)
        if owner and not from_viewport:
            pass  # tree already reflects the click
        if owner and from_viewport:
            item = self._owner_to_item.get(owner)
            if item is not None:
                self.asset_tree.blockSignals(True)
                self.asset_tree.setCurrentItem(item)
                self.asset_tree.blockSignals(False)
                self._on_tree_node_selected(item)
        obj = self._owner_to_obj.get(owner) if owner else None
        label = obj.display_name if obj else "-"
        self.statusBar().showMessage(
            f"Selected object: {label} [{owner or '-'}]"
        )
        self._update_editor_status()
        self._refresh_fx_elements()
        if not preserve_asset_selection:
            self._focus_assets_for_owner(owner, switch_tabs=False)
        self._refresh_texture_usage_highlight()
        self._sync_geometry_save_controls()
        if hasattr(self, "_editor_tabs"):
            if keep_global_edit and not self.edit_toggle_action.isChecked():
                self.edit_toggle_action.blockSignals(True)
                self.edit_toggle_action.setChecked(True)
                self.edit_toggle_action.blockSignals(False)
            self._sync_editor_context()

    def _on_object_picked(self, owner: str) -> None:
        self._select_owner(owner, from_viewport=True)

    def _apply_selected_children_scope(self) -> None:
        if self._family is None:
            return
        owner = self._selected_owner or self._default_owner(self._family)
        visible = self._family_descendants(self._family, owner)
        self.viewport.set_visible_owners(visible)
        self._sync_animation_controls()
        self._update_editor_status()

    def _update_editor_status(self) -> None:
        """Refresh the unsaved-edit markers of the resource rows."""

        self._refresh_dirty_resource_rows()

    def _dirty_resource_names(self):
        from studio.assets.visproto import normalize_base_name
        names = set()
        owners = set(self._geom_dirty)
        owners.update(key[0] for key in self._uv_original)
        owners.update(key[0] for key in self._texture_original)
        if self._vanm_uv_original:
            owners.update(owner for owner in self._owner_to_obj if self._owner_vanm_uv_keys(owner))
        for owner in owners:
            obj = self._owner_to_obj.get(owner)
            if obj is None:
                continue
            for name in (self._base_entry_name(self._family, obj, owner),
                         getattr(obj.skeleton, 'source_name', '')):
                if name:
                    names.add(normalize_base_name(str(name).replace('\\', '/').rsplit('/', 1)[-1]))
        return names

    def _refresh_dirty_resource_rows(self):
        from studio.ui.widgets import mark_resource_modified
        from studio.assets.visproto import normalize_base_name
        if not hasattr(self, 'setbas_tree'):
            return
        browser = getattr(self, 'entity_browser', None)
        if not self.setbas_tree.topLevelItemCount() and (browser is None or not browser.tree.topLevelItemCount()):
            return
        names = self._dirty_resource_names()
        for group_index in range(self.setbas_tree.topLevelItemCount()):
            group = self.setbas_tree.topLevelItem(group_index)
            for index in range(group.childCount()):
                item = group.child(index)
                name = str(item.data(0, _BAS_NAME_ROLE) or '')
                mark_resource_modified(item, normalize_base_name(name.replace('\\', '/').rsplit('/', 1)[-1]) in names)
        if browser is None:
            return
        from studio.ui.browsers import GAME_OBJECT_ROLE
        from studio.assets.catalog import visual_choices
        for index in range(browser.tree.topLevelItemCount()):
            item = browser.tree.topLevelItem(index)
            obj = item.data(0, GAME_OBJECT_ROLE)
            modified = False
            if names and browser.set_number is not None:
                key = (browser.set_number, obj.kind, obj.object_id)
                choices = self._entity_choices.get(key)
                if choices is None:
                    try:
                        choices = visual_choices(obj, browser.set_number)
                    except (OSError, ValueError, RuntimeError):
                        choices = []
                    self._entity_choices[key] = choices
                modified = any(normalize_base_name(choice.base_name or choice.path.name) in names for choice in choices)
            mark_resource_modified(item, modified)

    def _log(self, text: str) -> None:
        self.log_list.addItem(text)
        self.log_list.scrollToBottom()


    def _dep_for_name(self, name: str):
        if self._family is None:
            return None
        for dep in getattr(self._family, "dependencies", ()):
            if dep.raw_ref == name:
                return dep
        return None


    def _saved_choice_for(self, name: str):
        if self._family is None or self._family.base_path is None:
            return None
        dep = self._dep_for_name(name)
        return self._profile.lookup(
            str(self._family.base_path), dep.owner_node if dep else "root",
            dep.kind if dep else "texture", name,
        )


    def _auto_apply_saved_choices(self, base_path: Path) -> None:
        """Pre-seed overrides from the profile (only when the user enabled
        'Apply saved dependency choices automatically')."""

        if not self._profile.auto_apply:
            return
        for saved in self._profile.choices_for(str(base_path)):
            bare = saved.raw_ref.replace("\\", "/").split("/")[-1]
            if bare in self._overrides or saved.raw_ref in self._overrides:
                continue
            if saved.stale:
                self._log(f"profile: skipping stale saved choice "
                          f"{saved.raw_ref} -> {saved.chosen_path}")
                continue
            self._overrides[bare] = saved.chosen_path
            self._kept_names.add(bare)
            self._profile.touch(saved)
            self._log(f"profile: auto-applied {saved.raw_ref} -> "
                      f"{saved.chosen_path}")
        self._profile.save()


    # -- completeness ---------------------------------------------------------------

    def _completeness(self, family: AssetFamily) -> tuple[str, list[str]]:
        deps = family.dependencies
        by_status: dict[str, int] = {}
        for dep in deps:
            status = self._effective_status(dep.raw_ref, dep.status)
            by_status[status] = by_status.get(status, 0) + 1

        objects = family.all_objects()
        skeletons = sum(1 for o in objects if o.skeleton is not None)
        skeleton_refs = sum(1 for o in objects if o.base_object.skeleton_name)
        tex_total = len(family.texture_refs)
        tex_loaded = len(family.textures)
        anm_total = len(family.animation_refs)
        anm_loaded = len(family.animations)
        kids = max(0, len(objects) - 1)
        mapping_warnings = (len(self._mapping_index.unmapped)
                            + len(self._mapping_index.duplicates)
                            + len(self._mapping_index.invalid)
                            if self._mapping_index else 0)
        ambiguous = by_status.get("ambiguous", 0)
        missing = by_status.get("missing", 0)
        unsupported = by_status.get("unsupported_loader", 0)

        details = [
            f"Skeletons loaded: {skeletons}/{skeleton_refs or 1}",
            f"Textures loaded: {tex_loaded}/{tex_total}",
            f"Animations loaded: {anm_loaded}/{anm_total}",
            f"Children: {kids}",
            f"Ambiguous: {ambiguous}  Missing: {missing}  "
            f"Unsupported: {unsupported}  Mapping warnings: {mapping_warnings}",
        ]

        if skeletons == 0:
            status = "Incomplete: missing skeleton"
        elif missing and tex_loaded == 0:
            status = "Geometry only (missing textures)"
        elif ambiguous and tex_loaded < tex_total:
            status = "Incomplete: ambiguous textures (material-group fallback)"
        elif tex_total and tex_loaded == tex_total:
            if anm_total and anm_loaded == anm_total:
                status = "Complete textured preview (with animations)"
            elif anm_total:
                status = "Textured preview with missing animations"
            else:
                status = "Complete textured preview"
        elif tex_loaded:
            status = "Partial textured preview"
        else:
            status = "Material groups only"
        return status, details

    def _reload_with_overrides(self) -> None:
        if self._family and self._family.base_path:
            if self._family_entry_is_setbas_archive():
                owner = self._selected_owner
                exact_names = dict(self._base_entry_names)
                try:
                    family = load_asset_family(
                        self._family.base_path, self._extra_roots,
                        self._overrides, self._setbas)
                except Exception as exc:
                    QMessageBox.warning(
                        self, "Dependency reload failed",
                        f"No file was modified.\n\n{exc}")
                    return
                self._set_family(family)
                self._base_entry_names.update(exact_names)
                self._fill_asset_tree(family)
                if owner in self._owner_to_obj:
                    self._select_owner(owner)
                    self._focus_assets_for_owner(owner, switch_tabs=True)
                return
            self.open_base(self._family.base_path)
        else:
            self.statusBar().showMessage(
                "Override stored; reload the family to apply it."
            )

    def _use_selected_candidate(self) -> None:
        target = self._selected_resolve_target()
        if target is None:
            QMessageBox.information(
                self, "No candidate selected",
                "Select a candidate path in Asset Dependencies first.",
            )
            return
        logical_name, candidate = target
        if candidate is None:
            QMessageBox.information(
                self, "Not a candidate",
                "Select one of the candidate paths (child rows), not the "
                "resource row.",
            )
            return
        if candidate.startswith("SET.BAS:"):
            # Force the embedded resource for this logical name.
            bare = logical_name.replace("\\", "/").split("/")[-1]
            self._apply_override(bare,
                                 SETBAS_OVERRIDE_PREFIX + candidate[8:])
            return
        self._apply_override(logical_name, candidate)

    def _assign_manual_file(self) -> None:
        target = self._selected_resolve_target()
        if target is None:
            QMessageBox.information(
                self, "No resource selected",
                "Select the resource to bind in Asset Dependencies first.",
            )
            return
        logical_name, _candidate = target
        path, _ = QFileDialog.getOpenFileName(
            self, f"Assign a file to {logical_name}",
            str(self._last_directory),
            "Dependencies (*.sklt *.skl *.ilbm *.ilb *.lbm *.iff *.vbmp "
            "*.anm *.vanm *.pal *.SKLT *.SKL *.ILBM *.ILB *.VBMP *.ANM "
            "*.VANM *.PAL);;All files (*)",
        )
        if not path:
            return
        self._apply_override(logical_name, path)

    def _clear_override(self) -> None:
        target = self._selected_resolve_target()
        if target is None:
            return
        logical_name, _candidate = target
        bare = logical_name.replace("\\", "/").split("/")[-1]
        removed = False
        for key in (logical_name, bare):
            if key in self._overrides:
                del self._overrides[key]
                removed = True
            self._trial_names.discard(key)
            self._kept_names.discard(key)
            if key in self._skipped_names:
                self._skipped_names.discard(key)
                removed = True
        if removed:
            self.statusBar().showMessage(
                f"Unloaded / reverted: {logical_name}"
            )
            self._reload_with_overrides()

    def _edit_base_dependencies_for_item(self, item) -> None:
        owner = self._tree_owner_for_item(item)
        if owner is not None:
            self._edit_base_dependencies(owner)

    def _base_dependency_edit_specs(self, fam_obj) -> list[dict]:
        """Describe native BASE references and their proven writer support."""

        specs: list[dict] = []
        base_object = fam_obj.base_object
        if base_object.skeleton_name:
            specs.append({
                "kind": "Skeleton", "name": base_object.skeleton_name,
                "editable": False,
                "reason": "read-only: no structured skeleton-name writer",
            })

        def add_texture(block, block_index: int, slot: str,
                        *, nested: bool = False) -> None:
            texture = getattr(block, slot, None)
            if texture is None or not texture.name:
                return
            supported = bool(
                not nested and texture.kind in ("ilbm", "bmpanim")
                and texture.name_payload_offset >= 0
                and texture.name_capacity > 1)
            label = "Animation" if texture.kind == "bmpanim" else "Texture"
            if slot == "tracy_texture":
                label += " (TRACY)"
            specs.append({
                "kind": label,
                "name": texture.name,
                "editable": supported,
                "reason": (
                    f"writable c-string, max {texture.name_capacity - 1} bytes"
                    if supported else
                    "read-only: nested/unsupported reference has no verified writer"),
                "block_index": block_index,
                "binding_slot": slot,
                "capacity": texture.name_capacity,
            })

        for block_index, block in enumerate(base_object.ades):
            add_texture(block, block_index, "texture")
            add_texture(block, block_index, "tracy_texture")
            for stage in getattr(block, "particle_stages", ()):
                add_texture(stage, block_index, "texture", nested=True)
                add_texture(stage, block_index, "tracy_texture", nested=True)

        animation_names = {
            spec["name"] for spec in specs if spec["kind"] == "Animation"}
        if self._family is not None:
            for animation_name in sorted(animation_names, key=str.casefold):
                animation = next((
                    value for key, value in self._family.animations.items()
                    if str(key).casefold() == animation_name.casefold()), None)
                for bitmap in getattr(animation, "bitmap_names", ()):
                    specs.append({
                        "kind": "VANM frame bitmap", "name": bitmap,
                        "editable": False,
                        "reason": "read-only: stored in the ANM/VANM asset",
                    })
        for index, kid in enumerate(base_object.kids):
            specs.append({
                "kind": f"KIDS #{index}",
                "name": kid.name or kid.skeleton_name or "inline BASE object",
                "editable": False,
                "reason": "read-only: ordered inline object, not a filename link",
            })
        for embedded in base_object.embedded:
            specs.append({
                "kind": f"Embedded {embedded.class_id}",
                "name": embedded.resource_name,
                "editable": False,
                "reason": "read-only: embedded payload/reference is preserved",
            })
        return specs

    def _create_editable_base_copy(
            self, owner: str, target: str | Path,
            edits: list[TextureNameEdit] | None = None) -> Path:
        """Export one archive OBJT as a verified loose BASE, never in-place."""

        family = self._family
        fam_obj = self._owner_to_obj.get(owner)
        tree = getattr(getattr(family, "base_asset", None), "tree", None)
        if family is None or fam_obj is None or tree is None:
            raise MappingEditError("selected BASE has no parsed source OBJT")
        destination = Path(target)
        if destination.suffix.casefold() != ".base":
            destination = destination.with_suffix(".BASE")
        source_path = Path(family.base_path) if family.base_path else None
        if source_path is not None \
                and destination.resolve() == source_path.resolve():
            raise MappingEditError(
                "the editable copy cannot replace its read-only source")
        source_digest = hashlib.sha256(tree.data).digest()
        standalone = export_base_object_bytes(tree.data, fam_obj.base_object)
        # export_base_object_bytes() promotes the selected OBJT to the root of
        # the standalone file.  Remap only the proven direct-reference edits
        # from its archive owner path to that new root identity.
        standalone_edits = [
            TextureNameEdit(
                "root", edit.block_index, edit.name, edit.binding_slot)
            for edit in (edits or [])]
        with tempfile.TemporaryDirectory(
                prefix="OpenNeoUAStudio_editable_base_") as temp_dir:
            staged = Path(temp_dir) / destination.name
            save_model_base_copy(standalone, [], standalone_edits, staged)
            parsed = parse_base_bytes(staged.read_bytes(), destination.name)
            if parsed.root is None:
                raise MappingEditError("editable BASE copy failed round-trip")
            _commit_verified_files([(staged, destination)])
        if hashlib.sha256(tree.data).digest() != source_digest:
            raise MappingEditError("the read-only SET.BAS bytes changed")
        return destination

    def _edit_base_dependencies(self, owner: str) -> None:
        # Editing is always staged in memory first.  SET.BAS remains read-only,
        # but the user chooses the standalone destination only when Save As is
        # pressed inside the dependency editor itself.
        self._open_base_dependency_dialog(owner)

    def _open_base_dependency_dialog(self, owner: str) -> None:
        fam_obj = self._owner_to_obj.get(owner)
        if fam_obj is None:
            return
        specs = self._base_dependency_edit_specs(fam_obj)
        archive_source = self._family_entry_is_setbas_archive()
        dialog = QDialog(self)
        dialog.setWindowTitle("Edit BASE Dependencies")
        dialog.resize(900, 500)
        layout = QVBoxLayout(dialog)
        info = QLabel(
            "Edit the native BASE references supported by the verified writer. "
            + (
                "SET.BAS stays read-only; Save As writes a standalone BASE copy."
                if archive_source else
                "Read-only reference types are shown for context."))
        info.setWordWrap(True)
        layout.addWidget(info)
        tree = QTreeWidget()
        from studio.ui.widgets import use_standard_list_style
        use_standard_list_style(tree)
        tree.setHeaderLabels(["Reference", "Native name", "Writer support"])
        tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        tree.setUniformRowHeights(True)
        header = tree.header()
        header.setSectionsMovable(False)
        header.setStretchLastSection(True)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        tree.setColumnWidth(0, 165)
        tree.setColumnWidth(1, 285)
        base_name = self._base_entry_name(self._family, fam_obj, owner)
        root = QTreeWidgetItem([f"BASE: {base_name}", "", "Entry point"])
        tree.addTopLevelItem(root)
        editors: list[tuple[dict, QLineEdit, str]] = []
        for spec in specs:
            visible_support = (
                f"Editable (max {spec['capacity'] - 1} bytes)"
                if spec["editable"] else "Read-only")
            row = QTreeWidgetItem([
                spec["kind"], "" if spec["editable"] else spec["name"],
                visible_support])
            for column in range(3):
                row.setToolTip(column, spec["reason"])
            root.addChild(row)
            if spec["editable"]:
                edit = QLineEdit(spec["name"])
                edit.setMaxLength(spec["capacity"] - 1)
                edit.setToolTip(spec["reason"])
                tree.setItemWidget(row, 1, edit)
                editors.append((spec, edit, spec["name"]))
            else:
                row.setForeground(2, QColor(170, 170, 170))
        root.setExpanded(True)
        layout.addWidget(tree, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Reset
            | QDialogButtonBox.StandardButton.Close)
        revert = buttons.button(QDialogButtonBox.StandardButton.Reset)
        revert.setText("Revert")
        save = QPushButton("Save As" if archive_source else "Save")
        save.setEnabled(bool(editors))
        buttons.addButton(save, QDialogButtonBox.ButtonRole.AcceptRole)

        def revert_changes() -> None:
            for _spec, edit, initial in editors:
                edit.setText(initial)

        def changed_edits() -> list[TextureNameEdit]:
            return [
                TextureNameEdit(
                    owner, spec["block_index"], edit.text().strip(),
                    spec["binding_slot"])
                for spec, edit, initial in editors
                if edit.text().strip() != initial]

        def save_archive_copy(edits: list[TextureNameEdit]) -> bool:
            base_label = self._base_entry_name(self._family, fam_obj, owner)
            suggested = self._last_directory / Path(base_label).name
            if suggested.suffix.casefold() != ".base":
                suggested = suggested.with_suffix(".BASE")
            output, _ = QFileDialog.getSaveFileName(
                self, "Save BASE Dependencies As", str(suggested),
                "BASE model (*.BASE *.base);;All files (*)")
            if not output:
                return False
            try:
                target = self._create_editable_base_copy(owner, output, edits)
            except (MappingEditError, OSError, _BundleCommitError) as exc:
                QMessageBox.critical(
                    self, "Save As failed - SET.BAS unchanged", str(exc))
                return False
            archive_path = Path(self._setbas.path) if self._setbas else None
            if archive_path is not None:
                for search_root in (archive_path.parent, archive_path.parent.parent):
                    if search_root not in self._extra_roots:
                        self._extra_roots.append(search_root)
            self._last_directory = target.parent
            self.open_base(target, confirm_discard=False)
            self._notify(
                f"BASE dependency copy saved and verified: {target.name}.",
                9000)
            return True

        def save_changes() -> None:
            edits = changed_edits()
            if not edits:
                self._notify("No BASE dependency reference changed.", 3500)
                return
            saved = (save_archive_copy(edits) if archive_source
                     else self._save_base_dependency_edits(owner, edits))
            if saved:
                dialog.accept()

        revert.clicked.connect(revert_changes)
        save.clicked.connect(save_changes)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        self._base_dependency_dialog = dialog
        dialog.finished.connect(
            lambda _result: setattr(self, "_base_dependency_dialog", None))
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _save_base_dependency_edits(
            self, owner: str, edits: list[TextureNameEdit]) -> bool:
        family = self._family
        source = Path(family.base_path) if family and family.base_path else None
        if source is None or not source.is_file() \
                or self._family_entry_is_setbas_archive():
            QMessageBox.critical(
                self, "Save refused",
                "Dependency edits require a standalone loose BASE. SET.BAS "
                "was not modified.")
            return False
        if (self._geom_dirty or self._uv_original or self._vanm_uv_original
                or self._texture_original or self._pending_repairs):
            QMessageBox.information(
                self, "Save other edits first",
                "Finish or export the current model edits before changing "
                "BASE dependency references.")
            return False
        provider = Path(self._setbas.path) if self._setbas is not None else None
        provider_digest = (
            hashlib.sha256(provider.read_bytes()).digest()
            if provider is not None and provider.is_file()
            and provider.resolve() != source.resolve() else None)
        backup = _next_backup_path(source) if backup_enabled() else None
        try:
            original = source.read_bytes()
            with tempfile.TemporaryDirectory(
                    prefix="OpenNeoUAStudio_base_dependencies_") as temp_dir:
                staged = Path(temp_dir) / source.name
                save_model_base_copy(original, [], edits, staged)
                warnings = _commit_verified_files([(staged, source)])
            if provider_digest is not None and hashlib.sha256(
                    provider.read_bytes()).digest() != provider_digest:
                raise MappingEditError("the attached SET.BAS bytes changed")
        except (MappingEditError, OSError, _BundleCommitError) as exc:
            QMessageBox.critical(
                self, "Save failed - original BASE preserved",
                f"{exc}\n\nBackup: {backup if backup and backup.exists() else '-'}")
            return False
        self._log(
            f"BASE dependency references saved to {source}"
            + (f"; backup {backup}" if backup else "")
            + ("; " + "; ".join(warnings) if warnings else ""))
        self.open_base(source, confirm_discard=False)
        self._notify(
            "BASE dependencies saved and verified."
            + (f" Backup: {backup.name}." if backup else ""),
            9000)
        return True

    def _effective_status(self, name: str, status: str) -> str:
        bare = name.replace("\\", "/").split("/")[-1]
        for key in (name, bare):
            if key in self._skipped_names:
                return "skipped"
            if key in self._trial_names:
                return "trial_loaded"
            if key in self._kept_names:
                return "kept_for_session"
        return status

    def _refresh_asset_dependencies(self) -> None:
        if self._family is None:
            return
        owner = self._selected_owner
        self._fill_asset_tree(self._family)
        if owner in self._owner_to_item:
            self._focus_assets_for_owner(owner, switch_tabs=False)
