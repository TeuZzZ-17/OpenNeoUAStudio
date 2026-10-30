"""Entity Designer dialogs: import collision, open script object and apply script.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import (
    QSignalBlocker,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from entity_designer.script_model import (
    _data_root_for_path,
    _resolve_patch_target_block,
    _SCRIPT_TAB_ICON,
    apply_editable_overwrite_preview,
    build_editable_overwrite_preview,
    CollisionProject,
    CollisionScriptError,
    find_script_blocks,
    OPENNEOUA_SCRIPT_FILE_FILTER,
    plan_script_update,
    read_script_file,
    SCRIPT_TYPES,
    ScriptBlock,
    VEHICLE,
    VehicleModelReference,
    WEAPON,
    write_script_file,
)
from entity_designer.type_icons import ensure_font_key

class ImportCollisionDialog(QDialog):
    def __init__(self, parent, path: Path, text: str) -> None:
        super().__init__(parent)
        self.setWindowTitle("Import Collision Text")
        self.resize(620, 390)
        self.path = path
        self.text = text
        self.blocks = find_script_blocks(text)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(str(path)))
        form = QFormLayout()
        self.block_combo = QComboBox()
        for block in self.blocks:
            self.block_combo.addItem(block.label, block)
        form.addRow("Definition", self.block_combo)
        compound_note = QLabel(
            "coll_* imports as OpenNeoUA Collision. Vehicle/weapon remains "
            "the target category of the selected script block.")
        compound_note.setWordWrap(True)
        layout.addWidget(compound_note)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Open
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(
            QDialogButtonBox.StandardButton.Open).setText("Import")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected(self):
        block = self.block_combo.currentData()
        target_category = (
            WEAPON if block is not None and "weapon" in block.kind else VEHICLE)
        return block, target_category


class OpenScriptObjectDialog(QDialog):
    """Choose one vehicle/weapon definition and the SET.BAS VP database."""

    def __init__(
            self, parent, script_path: Path,
            references: list[VehicleModelReference],
            current_set_bas: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Import Vehicle / Weapon from Script")
        self.resize(720, 300)
        self._all_references = list(references)
        layout = QVBoxLayout(self)
        path_label = QLabel(str(script_path))
        path_label.setWordWrap(True)
        layout.addWidget(path_label)
        form = QGridLayout()
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(6)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search by name or numeric ID")
        self.block_combo = QComboBox()
        self.vp_label = QLabel("—")
        self.set_path_edit = QLineEdit()
        self.set_path_edit.setPlaceholderText("Select the SET.BAS VP database")
        if current_set_bas is not None:
            self.set_path_edit.setText(str(current_set_bas))
        browse = QPushButton("Browse")
        browse.clicked.connect(self._browse_set)
        form.addWidget(QLabel("Search"), 0, 0)
        form.addWidget(self.search_edit, 0, 1, 1, 2)
        form.addWidget(QLabel("Definition"), 1, 0)
        form.addWidget(self.block_combo, 1, 1, 1, 2)
        form.addWidget(QLabel("Resolved VP"), 2, 0)
        form.addWidget(self.vp_label, 2, 1, 1, 2)
        form.addWidget(QLabel("SET.BAS database"), 3, 0)
        form.addWidget(self.set_path_edit, 3, 1)
        form.addWidget(browse, 3, 2)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Open
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(
            QDialogButtonBox.StandardButton.Open).setText("Import")
        self.open_button = buttons.button(
            QDialogButtonBox.StandardButton.Open)
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.search_edit.textChanged.connect(self._filter_references)
        self.block_combo.currentIndexChanged.connect(self._sync_reference)
        self._filter_references()

    def _browse_set(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select SET.BAS VP database",
            self.set_path_edit.text(),
            "SET.BAS archive (SET.BAS *.bas *.BAS);;All files (*)")
        if path:
            self.set_path_edit.setText(path)

    def _filter_references(self, *_args):
        query = self.search_edit.text().strip().casefold()
        terms = [part for part in query.split() if part]
        current = self.block_combo.currentData()
        with QSignalBlocker(self.block_combo):
            self.block_combo.clear()
            for reference in self._all_references:
                block = reference.block
                haystack = (
                    f"{block.kind} {block.object_id} {block.name} "
                    f"vp_normal {reference.vp_normal}"
                ).casefold()
                if query.isdigit():
                    if block.object_id != int(query):
                        continue
                elif terms and not all(term in haystack for term in terms):
                    continue
                self.block_combo.addItem(reference.label, reference)
            if current is not None:
                for index in range(self.block_combo.count()):
                    candidate = self.block_combo.itemData(index)
                    if candidate == current:
                        self.block_combo.setCurrentIndex(index)
                        break
        self._sync_reference()

    def _sync_reference(self, *_args):
        reference = self.block_combo.currentData()
        if reference is None:
            label = "No matching definition"
        elif reference.vp_normal is None:
            label = reference.label
        else:
            label = f"vp_normal = {reference.vp_normal}"
        self.vp_label.setText(label)
        visual_override = bool(
            reference is not None
            and (reference.three_ds_normal or reference.base_normal))
        self.set_path_edit.setEnabled(
            reference is None or (reference.vp_normal is not None
                                  and not visual_override))
        self.set_path_edit.setToolTip(
            "Optional when base_normal or 3ds_normal supplies the visual."
            if visual_override else "Select the SET.BAS VP database.")
        self.open_button.setEnabled(reference is not None)

    def _accept_if_valid(self):
        if self.block_combo.currentData() is None:
            QMessageBox.warning(
                self, "Definition required",
                "Choose a vehicle or weapon definition first.")
            return
        reference = self.block_combo.currentData()
        needs_set = (
            reference.vp_normal is not None
            and not (reference.three_ds_normal or reference.base_normal))
        path_text = self.set_path_edit.text().strip()
        if needs_set and not Path(path_text).is_file():
            QMessageBox.warning(
                self, "SET.BAS required",
                "Select the SET.BAS archive that defines the selected VP.")
            return
        self.accept()

    def selected(self) -> tuple[VehicleModelReference, Path | None]:
        value = self.set_path_edit.text().strip()
        return self.block_combo.currentData(), Path(value) if value else None


# Compatibility alias for integrations importing the previous class name.


class ApplyScriptDialog(QDialog):
    def __init__(
            self, parent, project: CollisionProject, *,
            replace_all_managed: bool = False,
            initial_path: str | Path | None = None,
            initial_kind: str | None = None,
            initial_id: int | None = None,
            changed_groups: set[str] | frozenset[str] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Apply to Script")
        self.resize(820, 620)
        self.project = project
        self.updated_text = ""
        self._source_text = ""
        self._source_encoding = "utf-8"
        self._source_bom = False
        self.replace_all_managed = bool(replace_all_managed)
        self.changed_groups = (
            None if changed_groups is None else frozenset(changed_groups))
        self._editable_overwrite = (
            self.replace_all_managed and self.changed_groups is not None)
        self._setting_preview = False
        self.applied_groups: set[str] = set()
        self._script_blocks: list[ScriptBlock] = []
        layout = QVBoxLayout(self)
        form = QGridLayout()
        self.path_edit = QLineEdit()
        browse = QPushButton("Browse")
        browse.clicked.connect(self._browse)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search by name or numeric ID")
        self.block_combo = QComboBox()
        self.kind_combo = QComboBox()
        self.kind_combo.addItems(SCRIPT_TYPES)
        default_kind = (
            "modify_weapon" if project.target_category == WEAPON
            else "modify_vehicle")
        self.kind_combo.setCurrentText(default_kind)
        self.id_spin = QSpinBox()
        self.id_spin.setRange(0, 65535)
        self.detected_name = QLabel("—")
        self.comment_missing = QCheckBox(
            "Comment existing collision data when absent in the project")
        self.comment_missing.setChecked(True)
        if self.replace_all_managed:
            self.setWindowTitle("Overwrite Loaded Definition")
            self.comment_missing.setText(
                "Apply only Entity Designer tabs changed since this "
                "definition was loaded")
            self.comment_missing.setChecked(True)
            self.comment_missing.setEnabled(False)
            self.comment_missing.setToolTip(
                "Only tabs whose script data differs from the last loaded "
                "state are included. Unrelated script data is preserved.")
        form.addWidget(QLabel("Script"), 0, 0)
        form.addWidget(self.path_edit, 0, 1)
        form.addWidget(browse, 0, 2)
        form.addWidget(QLabel("Search"), 1, 0)
        form.addWidget(self.search_edit, 1, 1, 1, 2)
        form.addWidget(QLabel("Definition"), 2, 0)
        form.addWidget(self.block_combo, 2, 1, 1, 2)
        form.addWidget(QLabel("Block type"), 3, 0)
        form.addWidget(self.kind_combo, 3, 1, 1, 2)
        form.addWidget(QLabel("Numeric ID"), 4, 0)
        form.addWidget(self.id_spin, 4, 1, 1, 2)
        form.addWidget(QLabel("Detected name"), 5, 0)
        form.addWidget(self.detected_name, 5, 1, 1, 2)
        form.addWidget(self.comment_missing, 6, 0, 1, 3)
        layout.addLayout(form)
        if self._editable_overwrite:
            preview_label = QLabel(
                "Editable Preview — only modified tabs are included. Edit "
                "the code before Apply; remove a whole '; [Tab: ...]' "
                "section to skip that tab.")
            preview_label.setWordWrap(True)
        else:
            preview_label = QLabel(
                "Preview (only supported spatial/collision parameters may "
                "change)")
        layout.addWidget(preview_label)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(not self._editable_overwrite)
        if self._editable_overwrite:
            self.preview.setPlaceholderText(
                "No modified tabs. Edit here manually only if needed.")
        layout.addWidget(self.preview, 1)
        self.error_label = QLabel()
        self.error_label.setStyleSheet("color: #e06060")
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply
            | QDialogButtonBox.StandardButton.Cancel)
        self.apply_button = self.buttons.button(
            QDialogButtonBox.StandardButton.Apply)
        self.apply_button.setEnabled(False)
        # Apply has ApplyRole, so QDialogButtonBox.accepted is not emitted
        # for it.  Connect the actual button or the dialog only previews and
        # never writes the selected script.
        self.apply_button.clicked.connect(self._apply)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.search_edit.textChanged.connect(self._filter_script_blocks)
        self.block_combo.currentIndexChanged.connect(
            self._sync_selected_script_block)
        for widget_signal in (
            self.path_edit.textChanged,
            self.kind_combo.currentTextChanged,
            self.id_spin.valueChanged,
            self.comment_missing.toggled,
        ):
            widget_signal.connect(self.refresh_preview)
        if self._editable_overwrite:
            self.preview.textChanged.connect(self._editable_preview_changed)
        if initial_path is not None:
            self.path_edit.setText(str(initial_path))
        if initial_kind is not None:
            self.kind_combo.setCurrentText(initial_kind)
        if initial_id is not None:
            self.id_spin.setValue(int(initial_id))
        self._load_script_blocks()
        self.refresh_preview()

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select OpenNeoUA script", "",
            OPENNEOUA_SCRIPT_FILE_FILTER)
        if path:
            self.path_edit.setText(path)

    def _load_script_blocks(self, *_args) -> None:
        path = Path(self.path_edit.text())
        self._script_blocks = []
        if path.is_file():
            try:
                source_text, _encoding, _bom = read_script_file(path)
                self._script_blocks = find_script_blocks(source_text)
            except (OSError, UnicodeError):
                pass
        self._filter_script_blocks(refresh=False)

    def _filter_script_blocks(self, *_args, refresh: bool = True) -> None:
        query = self.search_edit.text().strip().casefold()
        terms = [part for part in query.split() if part]
        current = self.block_combo.currentData()
        preferred = (
            self.kind_combo.currentText().casefold(), self.id_spin.value())
        matches = []
        for block in self._script_blocks:
            haystack = (
                f"{block.kind} {block.object_id} {block.name}"
            ).casefold()
            if query.isdigit():
                if block.object_id != int(query):
                    continue
            elif terms and not all(term in haystack for term in terms):
                continue
            matches.append(block)
        with QSignalBlocker(self.block_combo):
            self.block_combo.clear()
            for block in matches:
                self.block_combo.addItem(block.label, block)
            selected = None
            for index, block in enumerate(matches):
                if (block.kind.casefold(), block.object_id) == preferred:
                    selected = index
                    break
            if selected is None and current in matches:
                selected = matches.index(current)
            if selected is None and matches:
                selected = 0
            if selected is not None:
                self.block_combo.setCurrentIndex(selected)
        block = self.block_combo.currentData()
        if block is not None and (
                self.kind_combo.currentText().casefold() != block.kind.casefold()
                or self.id_spin.value() != block.object_id):
            with QSignalBlocker(self.kind_combo), QSignalBlocker(self.id_spin):
                self.kind_combo.setCurrentText(block.kind)
                self.id_spin.setValue(block.object_id)
        if refresh:
            self.refresh_preview()

    def _sync_selected_script_block(self, *_args) -> None:
        block = self.block_combo.currentData()
        if block is None:
            self.refresh_preview()
            return
        with QSignalBlocker(self.kind_combo), QSignalBlocker(self.id_spin):
            self.kind_combo.setCurrentText(block.kind)
            self.id_spin.setValue(block.object_id)
        self.refresh_preview()

    def refresh_preview(self, *_args):
        self.error_label.clear()
        self.apply_button.setEnabled(False)
        self._load_script_blocks()
        path = Path(self.path_edit.text())
        if not path.is_file():
            self.detected_name.setText("—")
            self._setting_preview = True
            self.preview.clear()
            self._setting_preview = False
            return
        try:
            (self._source_text, self._source_encoding,
             self._source_bom) = read_script_file(path)
            if self._editable_overwrite:
                target = _resolve_patch_target_block(
                    self._source_text, self.kind_combo.currentText(),
                    self.id_spin.value())
                preview = build_editable_overwrite_preview(
                    self.kind_combo.currentText(), self.id_spin.value(),
                    self.project, set(self.changed_groups or ()))
                name = target.name
                self._setting_preview = True
                self.preview.setPlainText(preview)
                self._setting_preview = False
                self.detected_name.setText(name or "(name not present)")
                self._editable_preview_changed()
                return
            updated, preview, name = plan_script_update(
                self._source_text,
                self.kind_combo.currentText(),
                self.id_spin.value(),
                self.project,
                comment_missing=self.comment_missing.isChecked(),
                replace_all_managed=self.replace_all_managed,
            )
        except (OSError, UnicodeError, CollisionScriptError) as exc:
            self.detected_name.setText("—")
            self.error_label.setText(str(exc))
            return
        self.updated_text = updated
        self.detected_name.setText(name or "(name not present)")
        self._setting_preview = True
        self.preview.setPlainText(preview or "(No changes)")
        self._setting_preview = False
        self.apply_button.setEnabled(bool(preview))

    def _editable_preview_changed(self):
        if self._setting_preview or not self._editable_overwrite:
            return
        self.error_label.clear()
        try:
            updated, groups = apply_editable_overwrite_preview(
                self._source_text, self.preview.toPlainText(),
                self.kind_combo.currentText(), self.id_spin.value())
        except CollisionScriptError as exc:
            self.updated_text = self._source_text
            self.applied_groups = set()
            self.error_label.setText(str(exc))
            self.apply_button.setEnabled(False)
            return
        self.updated_text = updated
        self.applied_groups = groups
        self.apply_button.setEnabled(updated != self._source_text)

    def _apply(self):
        if self._editable_overwrite:
            self._editable_preview_changed()
            if not self.apply_button.isEnabled():
                return
        path = Path(self.path_edit.text())
        try:
            self._write_type_icon_key(path)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(
                self, "Write failed", f"No file was modified.\n\n{exc}")
            return
        try:
            write_script_file(
                path, self.updated_text,
                self._source_encoding, self._source_bom)
        except OSError as exc:
            QMessageBox.critical(
                self, "Write failed",
                f"No further file was modified.\n\n{exc}")
            return
        self.accept()

    def _write_type_icon_key(self, path: Path) -> None:
        """Add a new type_icon symbol to TYPE_NS.FON before the script uses it.

        Written first: an unused symbol in the font is harmless, a script
        symbol missing from the font shows a wrong icon.
        """

        project = self.project
        if (project.target_category != VEHICLE or not project.type_icon
                or project.type_icon_cell is None
                or "vehicle" not in self.kind_combo.currentText()):
            return
        if (self._editable_overwrite
                and _SCRIPT_TAB_ICON not in self.applied_groups):
            return
        data_root = _data_root_for_path(path)
        if data_root is None:
            raise ValueError(
                "The script is not inside a game Data folder, so "
                "TYPE_NS.FON cannot receive the new type_icon symbol.")
        ensure_font_key(data_root, project.type_icon,
                        project.type_icon_cell, project.name)
