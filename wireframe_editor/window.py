"""OpenUAStudio Wireframe Editor window.

This is the complete former 2D editor, integrated into the same QApplication
as the 3D/SET.BAS workbench.
"""

from __future__ import annotations

from pathlib import Path
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QActionGroup, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QStackedWidget,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from outline_editor import OutlineEditor
from sklt_parser import (
    SkltModel,
    SkltParseError,
    create_minimal_sklt_model,
    parse_sklt_file,
    save_sklt_with_otl2_points,
    save_sklt_with_poo2_pol2_structure,
    save_sklt_with_poo2_points,
)
from viewer import WireframeViewer


APP_TITLE = "OpenUAStudio - Wireframe Editor"


class WireframeEditorWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1100, 720)
        self._last_directory = Path.cwd()
        self._current_model: SkltModel | None = None
        self._current_file_path: Path | None = None
        self._skip_save_confirmation_this_session = False

        self.viewer = WireframeViewer()
        self.outline_editor = OutlineEditor()
        self.outline_editor.dirtyChanged.connect(self._outline_dirty_changed)
        self.outline_editor.geometryChanged.connect(self._outline_geometry_changed)
        self.outline_editor.undoRedoChanged.connect(self._update_edit_controls)
        self.outline_editor.resetApplied.connect(self._outline_reset_applied)
        self.outline_editor.selectionChanged.connect(self._outline_selection_changed)
        self.file_value = QLabel("No file loaded")
        self.file_value.setWordWrap(True)
        self.edit_mode_value = QLabel("No editable 2D mode")
        self.points_value = QLabel("0")
        self.polygons_value = QLabel("0")
        self.rendered_polygons_value = QLabel("0")
        self.sensors_value = QLabel("0")
        self.outline_points_value = QLabel("0")
        self.outline_groups_value = QLabel("0")
        self.outline_lines_value = QLabel("0")
        self.chunk_tree = QTreeWidget()
        self.warning_title_label = QLabel("Warnings")
        self.warning_status_label = QLabel("No parsing warnings.")
        self.warning_list = QListWidget()
        self.view_stack: QStackedWidget | None = None
        self.new_action: QAction | None = None
        self.save_action: QAction | None = None
        self.save_as_action: QAction | None = None
        self.undo_action: QAction | None = None
        self.redo_action: QAction | None = None
        self.copy_action: QAction | None = None
        self.cut_action: QAction | None = None
        self.paste_action: QAction | None = None
        self.link_action: QAction | None = None
        self.delete_link_action: QAction | None = None
        self.add_point_action: QAction | None = None
        self.delete_point_action: QAction | None = None
        self.align_horizontal_action: QAction | None = None
        self.align_vertical_action: QAction | None = None
        self.reset_action: QAction | None = None
        self.rotate_mode_action: QAction | None = None
        self.resize_mode_action: QAction | None = None
        self.add_vertex_action: QAction | None = None
        self.add_triangle_action: QAction | None = None
        self.add_square_action: QAction | None = None
        self.add_pentagon_action: QAction | None = None
        self.add_circle_action: QAction | None = None
        self.edit_toolbar: QToolBar | None = None
        self.mode_3d_check = QCheckBox("3D Mode")
        self.mode_3d_check.toggled.connect(self._set_3d_mode)

        self._build_ui()
        self._build_menu()
        self._build_edit_toolbar()
        self._set_3d_mode(False)
        self._update_save_controls()
        self._update_edit_controls()
        self.statusBar().showMessage("Ready")

    def _build_ui(self) -> None:
        side_panel = QWidget()
        side_layout = QVBoxLayout(side_panel)

        metadata = QFormLayout()
        metadata.addRow("File:", self.file_value)
        metadata.addRow("Mode:", self.edit_mode_value)
        metadata.addRow("POO2 vertices:", self.points_value)
        metadata.addRow("POL2 polygons:", self.polygons_value)
        metadata.addRow("Rendered polygons:", self.rendered_polygons_value)
        metadata.addRow("SEN2 vertices:", self.sensors_value)
        metadata.addRow("2D lines:", self.outline_lines_value)
        side_layout.addLayout(metadata)

        self.chunk_tree.setHeaderLabels(["Chunk", "Size", "Offset"])
        self.chunk_tree.setRootIsDecorated(False)
        self.chunk_tree.setAlternatingRowColors(True)
        side_layout.addWidget(self.chunk_tree, 3)

        side_layout.addWidget(self.warning_status_label)
        side_layout.addWidget(self.warning_title_label)
        self.warning_list.setAlternatingRowColors(True)
        side_layout.addWidget(self.warning_list, 2)
        self.warning_title_label.setVisible(False)
        self.warning_list.setVisible(False)

        self.view_stack = QStackedWidget()
        self.view_stack.addWidget(self.outline_editor)
        self.view_stack.addWidget(self.viewer)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.view_stack)
        splitter.addWidget(side_panel)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([820, 280])
        self.setCentralWidget(splitter)

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")

        self.new_action = QAction("&New", self)
        self.new_action.setShortcut(QKeySequence.StandardKey.New)
        self.new_action.triggered.connect(self.new_file)
        file_menu.addAction(self.new_action)

        open_action = QAction("&Load", self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self.open_dialog)
        file_menu.addAction(open_action)

        file_menu.addSeparator()

        self.save_action = QAction("&Save", self)
        self.save_action.setShortcut(QKeySequence.StandardKey.Save)
        self.save_action.triggered.connect(self.save_current_file)
        file_menu.addAction(self.save_action)

        self.save_as_action = QAction("Save &As...", self)
        self.save_as_action.setShortcut(QKeySequence.StandardKey.SaveAs)
        self.save_as_action.triggered.connect(self.save_outline_as)
        file_menu.addAction(self.save_as_action)

        file_menu.addSeparator()

        exit_action = QAction("E&xit", self)
        exit_action.setShortcut(QKeySequence.StandardKey.Quit)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        edit_menu = self.menuBar().addMenu("&Edit")

        self.undo_action = QAction("&Undo", self)
        self.undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        self.undo_action.triggered.connect(self.outline_editor.undo)
        edit_menu.addAction(self.undo_action)

        self.redo_action = QAction("&Redo", self)
        self.redo_action.setShortcuts(
            [QKeySequence.StandardKey.Redo, QKeySequence("Ctrl+Shift+Z")]
        )
        self.redo_action.triggered.connect(self.outline_editor.redo)
        edit_menu.addAction(self.redo_action)

        edit_menu.addSeparator()

        self.copy_action = QAction("&Copy", self)
        self.copy_action.setShortcut(QKeySequence.StandardKey.Copy)
        self.copy_action.triggered.connect(self.outline_editor.copy_selection)
        edit_menu.addAction(self.copy_action)

        self.cut_action = QAction("Cu&t", self)
        self.cut_action.setShortcut(QKeySequence.StandardKey.Cut)
        self.cut_action.triggered.connect(self.outline_editor.cut_selection)
        edit_menu.addAction(self.cut_action)

        self.paste_action = QAction("&Paste", self)
        self.paste_action.setShortcut(QKeySequence.StandardKey.Paste)
        self.paste_action.triggered.connect(self.outline_editor.paste_clipboard)
        edit_menu.addAction(self.paste_action)

        edit_menu.addSeparator()

        self.link_action = QAction("&Link", self)
        self.link_action.triggered.connect(self.outline_editor.start_link)
        edit_menu.addAction(self.link_action)

        self.add_point_action = None

        self.delete_point_action = QAction("&Delete", self)
        self.delete_point_action.setShortcut(QKeySequence(Qt.Key.Key_Delete))
        self.delete_point_action.triggered.connect(self.outline_editor.delete_selected_point)
        edit_menu.addAction(self.delete_point_action)

        edit_menu.addSeparator()

        self.align_horizontal_action = QAction("Align Horizontally", self)
        self.align_horizontal_action.triggered.connect(self.outline_editor.align_selected_horizontal)
        edit_menu.addAction(self.align_horizontal_action)

        self.align_vertical_action = QAction("Align Vertically", self)
        self.align_vertical_action.triggered.connect(self.outline_editor.align_selected_vertical)
        edit_menu.addAction(self.align_vertical_action)

        edit_menu.addSeparator()

        self.reset_action = QAction("&Reset", self)
        self.reset_action.triggered.connect(self.reset_active_view)
        edit_menu.addAction(self.reset_action)

        mode_menu = self.menuBar().addMenu("&Mode")
        mode_group = QActionGroup(self)
        mode_group.setExclusive(True)
        self.rotate_mode_action = QAction("&Rotate", self)
        self.resize_mode_action = QAction("&Resize", self)
        for action in (self.rotate_mode_action, self.resize_mode_action):
            action.setCheckable(True)
            mode_group.addAction(action)
            mode_menu.addAction(action)
        self.rotate_mode_action.triggered.connect(lambda: self._set_transform_mode("rotate"))
        self.resize_mode_action.triggered.connect(lambda: self._set_transform_mode("resize"))

        add_menu = self.menuBar().addMenu("&Add")
        self.add_vertex_action = QAction("&Vertex", self)
        self.add_triangle_action = QAction("&Triangle", self)
        self.add_square_action = QAction("&Square", self)
        self.add_pentagon_action = QAction("&Pentagon", self)
        self.add_circle_action = QAction("&Circle", self)
        self.add_vertex_action.triggered.connect(self.outline_editor.add_point)
        self.add_triangle_action.triggered.connect(lambda: self.outline_editor.add_shape("triangle"))
        self.add_square_action.triggered.connect(lambda: self.outline_editor.add_shape("square"))
        self.add_pentagon_action.triggered.connect(lambda: self.outline_editor.add_shape("pentagon"))
        self.add_circle_action.triggered.connect(lambda: self.outline_editor.add_shape("circle"))
        add_menu.addAction(self.add_vertex_action)
        add_menu.addSeparator()
        add_menu.addAction(self.add_triangle_action)
        add_menu.addAction(self.add_square_action)
        add_menu.addAction(self.add_pentagon_action)
        add_menu.addAction(self.add_circle_action)

    def _build_edit_toolbar(self) -> None:
        self.edit_toolbar = QToolBar("2D Edit Controls", self)
        self.edit_toolbar.setMovable(False)
        self.edit_toolbar.setFloatable(False)

        self.outline_editor.show_indices_check.setText("Vertex IDs")
        self.outline_editor.show_indices_check.toggled.connect(self.viewer.set_show_vertex_indices)
        self.edit_toolbar.addWidget(self.outline_editor.show_indices_check)
        self.edit_toolbar.addSeparator()
        self.edit_toolbar.addWidget(self.outline_editor.auto_align_check)
        self.edit_toolbar.addSeparator()
        self.edit_toolbar.addWidget(self.mode_3d_check)
        self.edit_toolbar.addSeparator()
        self.edit_toolbar.addWidget(QLabel("Vertex:"))
        self.edit_toolbar.addWidget(self.outline_editor.selected_label)
        self.edit_toolbar.addSeparator()

        for label, spinbox in (
            ("X:", self.outline_editor.x_spin),
            ("Y:", self.outline_editor.y_spin),
            ("Z:", self.outline_editor.z_spin),
        ):
            spinbox.setMaximumWidth(120)
            self.edit_toolbar.addWidget(QLabel(label))
            self.edit_toolbar.addWidget(spinbox)

        self.edit_toolbar.addSeparator()
        self.edit_toolbar.addWidget(self.outline_editor.status_label)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self.edit_toolbar)

    def _set_transform_mode(self, mode: str) -> None:
        self.outline_editor.set_transform_mode(mode)
        if mode == "rotate" and self.rotate_mode_action:
            self.rotate_mode_action.setChecked(True)
        elif mode == "resize" and self.resize_mode_action:
            self.resize_mode_action.setChecked(True)
        self._update_edit_controls()

    def _maybe_save_dirty(self) -> bool:
        if not self.outline_editor.is_dirty:
            return True
        title = self._current_file_path.name if self._current_file_path else "current file"
        reply = QMessageBox.question(
            self,
            "Save changes?",
            f"Save changes to {title} before continuing?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if reply == QMessageBox.StandardButton.Cancel:
            return False
        if reply == QMessageBox.StandardButton.Discard:
            return True
        if self._current_file_path:
            self.save_current_file()
        else:
            self.save_outline_as()
        return not self.outline_editor.is_dirty

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._maybe_save_dirty():
            event.accept()
        else:
            event.ignore()

    def new_file(self) -> None:
        if not self._maybe_save_dirty():
            return

        model = create_minimal_sklt_model("Untitled.SKL")
        self._current_file_path = None
        self._current_model = model
        self._skip_save_confirmation_this_session = False
        self._show_model(model, Path("Untitled.SKL"))
        self.outline_editor.mark_dirty()
        self._update_metadata_from_editor()
        self._update_window_title()
        self._update_save_controls()
        self._update_edit_controls()
        self.statusBar().showMessage("New SKLT session: one vertex at origin")

    def open_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Load file",
            str(self._last_directory),
            "Urban Assault skeletons (*.sklt *.SKLT *.skl *.SKL);;All files (*)",
        )
        if path:
            self.open_file(path)

    def save_current_file(self) -> None:
        if not self._current_model:
            return
        if not self.outline_editor.can_save or not self.outline_editor.is_dirty:
            return
        if not self._current_file_path:
            self.save_outline_as()
            return
        if not self._confirm_save_over_original():
            return

        save_mode = self.outline_editor.save_mode
        try:
            self._write_edited_file(self._current_file_path)
            saved_model = parse_sklt_file(self._current_file_path)
        except SkltParseError as exc:
            QMessageBox.warning(self, "Could not save file", str(exc))
            self.statusBar().showMessage("Save failed")
            return

        save_label = "OTL2 outline" if save_mode == "otl2" else "wireframe"
        message = f"Saved edited {save_label} to {self._current_file_path.name}."
        QMessageBox.information(self, "File saved", message)
        self._show_model(saved_model, self._current_file_path)

    def save_outline_as(self) -> None:
        if not self._current_model or not self.outline_editor.can_save:
            QMessageBox.information(
                self,
                "No editable data",
                "This file has no editable OTL2/OLPL outline or projected POO2/POL2 vertex data to save.",
            )
            return

        suggested = self._current_file_path or self._last_directory / "outline.SKL"
        save_mode = self.outline_editor.save_mode
        save_label = "OTL2 outline" if save_mode == "otl2" else "wireframe"
        path, _ = QFileDialog.getSaveFileName(
            self,
            f"Save edited {save_label} as",
            str(suggested),
            "Urban Assault skeletons (*.sklt *.SKLT *.skl *.SKL);;All files (*)",
        )
        if not path:
            return

        output_path = Path(path)
        if self._current_file_path and output_path.resolve() == self._current_file_path.resolve():
            QMessageBox.warning(
                self,
                "Choose a different file",
                "Direct overwrite of the loaded original is disabled. "
                "Choose a different Save As destination.",
            )
            return

        try:
            self._write_edited_file(output_path)
            saved_model = parse_sklt_file(output_path)
        except SkltParseError as exc:
            QMessageBox.warning(self, "Could not save file", str(exc))
            self.statusBar().showMessage("Save As failed")
            return

        message = f"Saved edited {save_label} to {output_path.name}."
        QMessageBox.information(self, "Outline saved", message)

        self._last_directory = output_path.parent
        self._current_file_path = output_path
        self._show_model(saved_model, output_path)

    def _write_edited_file(self, output_path: Path) -> Path | None:
        if not self._current_model:
            raise SkltParseError("No SKLT file is loaded.")

        save_mode = self.outline_editor.save_mode
        if save_mode == "otl2":
            return save_sklt_with_otl2_points(
                self._current_model, self.outline_editor.outline_points, output_path
            )
        if save_mode == "poo2":
            if self.outline_editor.has_structural_changes:
                return save_sklt_with_poo2_pol2_structure(
                    self._current_model,
                    self.outline_editor.projected_points,
                    self.outline_editor.polygons,
                    output_path,
                )
            return save_sklt_with_poo2_points(
                self._current_model, self.outline_editor.projected_points, output_path
            )
        raise SkltParseError("No editable data is available to save.")

    def _confirm_save_over_original(self) -> bool:
        if self._skip_save_confirmation_this_session:
            return True

        message_box = QMessageBox(self)
        message_box.setIcon(QMessageBox.Icon.Warning)
        message_box.setWindowTitle("Overwrite loaded file?")
        message_box.setText("Save will modify the currently loaded SKLT/SKL file.")
        message_box.setInformativeText(
            "Save only when you are ready to overwrite the loaded file."
        )
        message_box.setStandardButtons(
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Cancel
        )
        message_box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        checkbox = QCheckBox("Do not ask again this session")
        message_box.setCheckBox(checkbox)

        accepted = message_box.exec() == QMessageBox.StandardButton.Save
        if accepted and checkbox.isChecked():
            self._skip_save_confirmation_this_session = True
        return accepted

    def open_file(self, path: str | Path) -> None:
        if not self._maybe_save_dirty():
            return
        file_path = Path(path)
        try:
            model = parse_sklt_file(file_path)
        except SkltParseError as exc:
            QMessageBox.warning(self, "Could not load file", str(exc))
            self.statusBar().showMessage("Load failed")
            return
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Unexpected parsing error",
                f"The file was not modified.\n\n{exc}",
            )
            self.statusBar().showMessage("Load failed safely")
            return

        self._last_directory = file_path.parent
        self._current_file_path = file_path
        self._show_model(model, file_path)

    def _show_model(self, model: SkltModel, file_path: Path) -> None:
        self._current_model = model
        self.viewer.set_model(model)
        self.outline_editor.set_model(model)
        self.mode_3d_check.setChecked(False)
        self._set_3d_mode(False)
        self.file_value.setText(file_path.name)
        self.file_value.setToolTip(str(file_path))
        self.sensors_value.setText(str(len(model.sensors)))
        self.outline_points_value.setText(str(len(model.outline_points)))
        self.outline_groups_value.setText(
            f"{model.parsed_outline_group_count} / {model.rendered_outline_group_count}"
        )
        self._update_metadata_from_editor()

        self.chunk_tree.clear()
        for chunk in model.chunks:
            size_text = str(chunk.declared_size)
            if chunk.actual_size != chunk.declared_size:
                size_text = f"{chunk.actual_size} / {chunk.declared_size}"
            item = QTreeWidgetItem(
                [
                    f"{'  ' * chunk.depth}{chunk.display_name}",
                    size_text,
                    f"0x{chunk.offset:X}",
                ]
            )
            self.chunk_tree.addTopLevelItem(item)
        self.chunk_tree.resizeColumnToContents(0)

        self.warning_list.clear()
        if model.warnings:
            self.warning_list.addItems(model.warnings)
            self.warning_title_label.setVisible(True)
            self.warning_list.setVisible(True)
            self.warning_status_label.setVisible(False)
        else:
            self.warning_title_label.setVisible(False)
            self.warning_list.setVisible(False)
            self.warning_status_label.setVisible(True)

        self._update_window_title()
        self._update_save_controls()
        self._update_edit_controls()
        self.statusBar().showMessage(
            f"Loaded {file_path.name}: {len(model.points)} vertices, "
            f"{model.rendered_polygon_count} rendered polygons, "
            f"{len(model.warnings)} warning(s)"
        )

    def _outline_dirty_changed(self, dirty: bool) -> None:
        self._update_window_title()
        self._update_save_controls()

    def _outline_selection_changed(self, index: int) -> None:
        self.viewer.set_selected_index(index)
        self._update_edit_controls()

    def _outline_geometry_changed(self) -> None:
        self.viewer.set_points(self.outline_editor.projected_points)
        self.viewer.set_edges(self.outline_editor.polygons)
        self._update_metadata_from_editor()

    def _outline_reset_applied(self) -> None:
        if self.outline_editor.save_mode == "poo2":
            self._outline_geometry_changed()

    def _update_metadata_from_editor(self) -> None:
        self.edit_mode_value.setText(self.outline_editor.edit_mode_text)
        if self.outline_editor.save_mode == "poo2":
            self.points_value.setText(str(self.outline_editor.editable_point_count))
            self.polygons_value.setText(str(self.outline_editor.editable_polygon_count))
            self.rendered_polygons_value.setText(str(self.outline_editor.editable_polygon_count))
            self.outline_lines_value.setText(str(self.outline_editor.editable_line_count))
        elif self._current_model:
            self.points_value.setText(str(len(self._current_model.points)))
            self.polygons_value.setText(str(self._current_model.parsed_polygon_count))
            self.rendered_polygons_value.setText(str(self._current_model.rendered_polygon_count))
            self.outline_lines_value.setText(str(self.outline_editor.editable_line_count))
        else:
            self.points_value.setText("0")
            self.polygons_value.setText("0")
            self.rendered_polygons_value.setText("0")
            self.outline_lines_value.setText("0")

    def _update_save_controls(self) -> None:
        can_save_as = bool(self._current_model and self.outline_editor.can_save)
        can_save = can_save_as and self.outline_editor.is_dirty
        if self.save_action:
            self.save_action.setEnabled(can_save)
        if self.save_as_action:
            self.save_as_action.setEnabled(can_save_as)

    def _update_edit_controls(self) -> None:
        if self.undo_action:
            self.undo_action.setEnabled(self.outline_editor.can_undo)
        if self.redo_action:
            self.redo_action.setEnabled(self.outline_editor.can_redo)
        can_edit_structure = self.outline_editor.save_mode == "poo2"
        has_selection = self.outline_editor.has_vertex_selection
        has_single_selection = self.outline_editor.selected_index >= 0 and self.outline_editor.selected_vertex_count == 1
        has_any_selection = self.outline_editor.has_vertex_selection or self.outline_editor.has_selected_link
        has_link_selection = self.outline_editor.has_selected_link
        if self.copy_action:
            self.copy_action.setEnabled(can_edit_structure and has_any_selection)
        if self.cut_action:
            self.cut_action.setEnabled(can_edit_structure and has_any_selection)
        if self.paste_action:
            self.paste_action.setEnabled(can_edit_structure and self.outline_editor.has_clipboard)
        if self.link_action:
            self.link_action.setEnabled(can_edit_structure and has_single_selection)
        if self.add_point_action:
            self.add_point_action.setEnabled(can_edit_structure)
        if self.delete_point_action:
            self.delete_point_action.setEnabled(can_edit_structure and has_any_selection)
        if self.align_horizontal_action:
            self.align_horizontal_action.setEnabled(can_edit_structure and has_single_selection)
        if self.align_vertical_action:
            self.align_vertical_action.setEnabled(can_edit_structure and has_single_selection)
        if self.reset_action:
            self.reset_action.setEnabled(self._current_model is not None)
        if self.rotate_mode_action:
            self.rotate_mode_action.setEnabled(can_edit_structure and has_any_selection)
        if self.resize_mode_action:
            self.resize_mode_action.setEnabled(can_edit_structure and has_any_selection)
        mode = self.outline_editor.transform_mode
        if self.rotate_mode_action:
            self.rotate_mode_action.setChecked(mode == "rotate")
        if self.resize_mode_action:
            self.resize_mode_action.setChecked(mode == "resize")
        for action in (
            self.add_vertex_action,
            self.add_triangle_action,
            self.add_square_action,
            self.add_pentagon_action,
            self.add_circle_action,
        ):
            if action:
                action.setEnabled(can_edit_structure)

    def _update_window_title(self) -> None:
        dirty_marker = " *" if self.outline_editor.is_dirty else ""
        if self._current_file_path:
            full_path = self._current_file_path.expanduser().resolve(
                strict=False
            )
            self.setWindowTitle(
                f"{APP_TITLE} - {full_path.name} - "
                f"{full_path}{dirty_marker}"
            )
            return
        if self._current_model:
            self.setWindowTitle(f"{APP_TITLE} - Untitled.SKL{dirty_marker}")
            return
        self.setWindowTitle(APP_TITLE)

    def reset_active_view(self) -> None:
        # Reset must restore the loaded point data in both 2D and 3D mode.
        # The 3D checkbox is only a preview toggle, not a separate edit state.
        self.outline_editor.reset_to_loaded()
        if self.mode_3d_check.isChecked():
            self.viewer.reset_view()

    def _set_3d_mode(self, enabled: bool) -> None:
        if not self.view_stack:
            return
        if enabled:
            self.viewer.set_points(self.outline_editor.projected_points)
            self.viewer.set_edges(self.outline_editor.polygons)
            self.viewer.set_selected_index(self.outline_editor.selected_index)
            self.viewer.set_show_vertex_indices(self.outline_editor.show_indices_check.isChecked())
            self.view_stack.setCurrentWidget(self.viewer)
            self.edit_mode_value.setText("3D Wireframe Preview")
        else:
            self.view_stack.setCurrentWidget(self.outline_editor)
            self._update_metadata_from_editor()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("OpenUAStudio")
    app.setApplicationDisplayName("")
    window = WireframeEditorWindow()
    window.show()

    if len(sys.argv) > 1:
        window.open_file(sys.argv[1])

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
