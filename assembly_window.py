"""OpenUAStudio: integrated Urban Assault asset workbench.

The main window assembles BASE + skeleton + texture + animation families,
provides the former BASet extraction/conversion workflows, and launches the
integrated Wireframe Editor and Map Editor. Geometry writes are explicit,
verified, and backed up before an original loose skeleton is overwritten.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import re
import os
import tempfile
import time
from pathlib import Path

from PySide6.QtCore import QSize, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QIcon,
    QImage,
    QImageWriter,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QApplication,
    QAbstractItemView,
    QComboBox,
    QColorDialog,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from asset_family import (
    SETBAS_OVERRIDE_PREFIX,
    AssetFamily,
    load_asset_family,
    load_manual_family,
)
from asset_report import family_to_json, family_to_markdown
from assembly_viewer import AssetViewport, VIEW_MODES, VIEW_PRESETS
from base_mapping_editor import (
    MappingEditError,
    MappingIndex,
    RepairPlan,
    TextureNameEdit,
    UVEdit,
    eligible_blocks,
    export_base_object_bytes,
    plan_copy_style,
    plan_planar,
    save_model_base_copy,
    save_repaired_base,
)
from dependency_profile import DependencyProfile
from fx_element_editor import FxElement, detect_fx_elements
from setbas_reader import (
    SetBasArchive,
    SetBasError,
    decode_texture,
    read_setbas,
)
from sklt_parser import (
    SkltParseError,
    parse_sklt_file,
    save_sklt_with_poo2_points,
)
from uv_editor_widget import UVEditorWidget

WINDOW_TITLE = "OpenUAStudio"


def _display_path(path: str | Path) -> Path:
    """Return a stable absolute path for window-title display."""

    return Path(path).expanduser().resolve(strict=False)


class _BundleCommitError(OSError):
    def __init__(self, message: str, *, rollback_complete: bool) -> None:
        super().__init__(message)
        self.rollback_complete = rollback_complete


def _commit_verified_files(
        files: list[tuple[Path, Path]]) -> list[str]:
    """Commit a verified multi-file output with coordinated rollback.

    Sources are first copied to staging files beside every destination. Only
    after all staging succeeds are existing destinations moved to temporary
    backups and replaced. A failure restores the complete old pair whenever
    the filesystem permits it.
    """

    records: list[dict] = []
    try:
        for source, target in files:
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, stage_name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".stage",
                dir=target.parent)
            os.close(fd)
            record = {
                "target": target,
                "stage": Path(stage_name),
                "backup": None,
                "committed": False,
            }
            records.append(record)
            shutil.copy2(source, record["stage"])

        for record in records:
            target = record["target"]
            if target.exists():
                fd, backup_name = tempfile.mkstemp(
                    prefix=f".{target.name}.", suffix=".rollback",
                    dir=target.parent)
                os.close(fd)
                backup = Path(backup_name)
                backup.unlink()
                os.replace(target, backup)
                record["backup"] = backup
            os.replace(record["stage"], target)
            record["committed"] = True
    except OSError as exc:
        rollback_errors = []
        for record in reversed(records):
            target = record["target"]
            backup = record["backup"]
            try:
                if backup is not None and backup.exists():
                    if target.exists():
                        target.unlink()
                    os.replace(backup, target)
                    record["backup"] = None
                elif record["committed"] and target.exists():
                    target.unlink()
            except OSError as rollback_exc:
                rollback_errors.append(f"{target}: {rollback_exc}")
        for record in records:
            stage = record["stage"]
            try:
                if stage.exists():
                    stage.unlink()
            except OSError as cleanup_exc:
                rollback_errors.append(f"{stage}: {cleanup_exc}")
        message = str(exc)
        if rollback_errors:
            message += ("\n\nRollback was incomplete; inspect these paths:\n"
                        + "\n".join(rollback_errors))
        raise _BundleCommitError(
            message, rollback_complete=not rollback_errors) from exc

    warnings = []
    for record in records:
        backup = record["backup"]
        if backup is not None and backup.exists():
            try:
                backup.unlink()
            except OSError as exc:
                warnings.append(
                    f"saved, but temporary backup cleanup failed: "
                    f"{backup} ({exc})")
        stage = record["stage"]
        if stage.exists():
            try:
                stage.unlink()
            except OSError as exc:
                warnings.append(
                    f"saved, but staging cleanup failed: {stage} ({exc})")
    return warnings


STATUS_COLORS = {
    "found": QColor(90, 200, 110),
    "manual": QColor(110, 170, 255),
    "manual (SET.BAS)": QColor(110, 170, 255),
    "setbas": QColor(120, 210, 210),
    "ambiguous": QColor(255, 190, 70),
    "missing": QColor(240, 90, 90),
    "decode failed": QColor(200, 90, 200),
}


class _HoldNudgeButton(QPushButton):
    """Button whose hold timer is not reset by Qt's auto-repeat signals."""

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._nudge_started_at = time.monotonic()
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if not event.isAutoRepeat():
            self._nudge_started_at = time.monotonic()
        super().keyPressEvent(event)


def _status_icon(status: str) -> QPixmap:
    pix = QPixmap(12, 12)
    pix.fill(STATUS_COLORS.get(status, QColor(150, 150, 150)))
    return pix


def _checker_thumbnail(image: QImage, size: int = 96) -> QPixmap:
    """Thumbnail over an alpha checkerboard so transparency is visible."""

    scaled = image.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                          Qt.TransformationMode.FastTransformation)
    pix = QPixmap(scaled.size())
    painter = QPainter(pix)
    cell = 8
    for y in range(0, scaled.height(), cell):
        for x in range(0, scaled.width(), cell):
            light = ((x // cell) + (y // cell)) % 2 == 0
            painter.fillRect(x, y, cell, cell,
                             QColor(200, 200, 200) if light
                             else QColor(140, 140, 140))
    painter.drawImage(0, 0, scaled)
    painter.end()
    return pix


def _qimage_from_ilbm(image, palette_override=None) -> QImage | None:
    """Convert a decoded ILBM/VBMP image to a Qt image for preview."""

    if image is None:
        return None
    rgba = image.to_rgba_bytes(
        palette_override=palette_override,
        alpha_mode="chroma",
    )
    if rgba is None:
        return None
    qimage = QImage(
        rgba,
        image.width,
        image.height,
        image.width * 4,
        QImage.Format.Format_RGBA8888,
    )
    return qimage.convertToFormat(QImage.Format.Format_ARGB32)


def _draw_uv_polygon(painter: QPainter, uvs, size: int) -> None:
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QPolygonF

    points = [QPointF(u / 256 * size, v / 256 * size) for u, v in uvs]
    if len(points) >= 2:
        painter.drawPolygon(QPolygonF(points))
    for p in points:
        painter.drawEllipse(p, 2.0, 2.0)


class TexturePickerDialog(QDialog):
    """Searchable texture chooser with incrementally loaded thumbnails."""

    def __init__(self, names: list[str], current: str, thumbnail_loader,
                 parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Choose Texture")
        self.resize(720, 520)
        self._thumbnail_loader = thumbnail_loader
        self._pending_items: list[QListWidgetItem] = []

        layout = QVBoxLayout(self)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter textures...")
        self.search.textChanged.connect(self._filter_items)
        layout.addWidget(self.search)

        self.list = QListWidget()
        self.list.setViewMode(QListView.ViewMode.IconMode)
        self.list.setResizeMode(QListView.ResizeMode.Adjust)
        self.list.setMovement(QListView.Movement.Static)
        self.list.setIconSize(QSize(96, 96))
        self.list.setGridSize(QSize(132, 128))
        self.list.setWordWrap(True)
        self.list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        layout.addWidget(self.list, 1)

        placeholder = QPixmap(96, 96)
        placeholder.fill(QColor(42, 44, 50))
        current_item = None
        for name in names:
            item = QListWidgetItem(QIcon(placeholder), name)
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
            self.list.addItem(item)
            self._pending_items.append(item)
            if name.lower() == current.lower():
                current_item = item
        if current_item is not None:
            self.list.setCurrentItem(current_item)
            self.list.scrollToItem(current_item)
        elif self.list.count():
            self.list.setCurrentRow(0)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.list.itemDoubleClicked.connect(lambda _item: self.accept())
        QTimer.singleShot(0, self._load_thumbnail_batch)

    def _filter_items(self, text: str) -> None:
        needle = text.strip().lower()
        first_visible = None
        for index in range(self.list.count()):
            item = self.list.item(index)
            visible = not needle or needle in item.text().lower()
            item.setHidden(not visible)
            if visible and first_visible is None:
                first_visible = item
        current = self.list.currentItem()
        if current is None or current.isHidden():
            self.list.setCurrentItem(first_visible)

    def _load_thumbnail_batch(self) -> None:
        for _ in range(min(6, len(self._pending_items))):
            item = self._pending_items.pop(0)
            try:
                image = self._thumbnail_loader(
                    item.data(Qt.ItemDataRole.UserRole))
            except Exception:
                image = None
            if image is not None and not image.isNull():
                item.setIcon(QIcon(_checker_thumbnail(image, 96)))
        if self._pending_items and self.isVisible():
            QTimer.singleShot(0, self._load_thumbnail_batch)

    def selected_name(self) -> str | None:
        item = self.list.currentItem()
        return (item.data(Qt.ItemDataRole.UserRole)
                if item is not None else None)


class LiveScaleDialog(QDialog):
    """Scale slider whose changes are previewed live by the viewport."""

    factorChanged = Signal(float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Scale Selection / Model")
        self.setWindowModality(Qt.WindowModality.NonModal)
        layout = QVBoxLayout(self)
        self.value_label = QLabel("Scale: 1.00x")
        self.value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.value_label)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(10, 400)
        self.slider.setValue(100)
        self.slider.setTickInterval(25)
        self.slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.slider.valueChanged.connect(self._value_changed)
        layout.addWidget(self.slider)
        hint = QLabel("Drag the slider to preview the scale in real time.")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(360, self.sizeHint().height())

    def factor(self) -> float:
        return self.slider.value() / 100.0

    def _value_changed(self, value: int) -> None:
        factor = value / 100.0
        self.value_label.setText(f"Scale: {factor:.2f}x")
        self.factorChanged.emit(factor)


class AssemblyWindow(QMainWindow):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(WINDOW_TITLE)
        self.resize(1320, 800)
        self._family: AssetFamily | None = None
        self._last_directory = Path.home()
        self._last_output_directory: Path | None = None
        self._extra_roots: list[Path] = []
        self._wireframe_windows: list[QMainWindow] = []
        self._preview_windows: list[QDialog] = []
        # session-only manual texture/animation bindings: logical name -> path
        self._overrides: dict[str, str] = {}
        # dependency workflow state (session-only, per asset)
        self._trial_names: set[str] = set()
        self._kept_names: set[str] = set()
        self._skipped_names: set[str] = set()
        self._diagnostics_tabs = None
        self._diagnostics_dock = None
        # Large Family Mode / object selection state
        self._large_mode = False
        self._selected_owner: str | None = None
        self._owner_to_obj: dict[str, object] = {}
        self._owner_to_item: dict[str, QTreeWidgetItem] = {}
        # optional read-only SET.BAS resource provider
        self._setbas: SetBasArchive | None = None
        # Polygon Mapping Workbench state
        self._mapping_index: MappingIndex | None = None
        self._workbench_obj = None          # first skeleton-bearing FamilyObject
        self._selected_poly: int | None = None
        self._selected_polys: set[int] = set()
        self._repair_plan: RepairPlan | None = None
        self._pending_repairs: list[RepairPlan] = []
        self._saved_repair_path: str | None = None
        # geometry Edit Mode: owners with unsaved vertex edits
        self._geom_dirty: dict[str, object] = {}
        self._geom_original: dict[str, list[tuple[float, float, float]]] = {}
        # Model/texture editor UV state; saved only through verified BASE Save As.
        self._uv_ctx: tuple | None = None
        self._uv_original: dict[tuple[str, int, int], list[tuple[int, int]]] = {}
        # Session-only material previews; SET.BAS and BASE stay untouched.
        self._texture_original: dict[tuple[str, int], str | None] = {}
        self._copied_vertex_shape: dict | None = None
        self._edit_undo_stack: list[dict] = []
        self._edit_redo_stack: list[dict] = []
        self._history_replaying = False
        self._uv_history_before: tuple[tuple[str, int, int], list] | None = None
        self._object_info_asset_lines = ["No asset selected."]
        self._object_info_polygon_lines: list[str] = []
        self._fx_elements: list[FxElement] = []
        self._snapshot_mode_active = False
        self._snapshot_custom_color: QColor | None = None
        self._snapshot_zoom_percent = 100
        self._skip_model_switch_warning = False
        self._bundle_targets: dict[str, tuple[Path, Path]] = {}
        self._live_scale_dialog: LiveScaleDialog | None = None

        self.viewport = AssetViewport()
        self.viewport.statusMessage.connect(
            lambda text: self._notify(text, 4500)
        )
        self.viewport.polygonPickedDetailed.connect(self._on_polygon_picked)
        self.viewport.polygonDeselected.connect(self._on_polygon_deselected)
        self.viewport.selectionCleared.connect(self._on_selection_cleared)
        self.viewport.objectPicked.connect(self._on_object_picked)
        self.viewport.editModeChanged.connect(self._on_edit_mode_toggled)
        self.viewport.editSelectionChanged.connect(
            lambda _count: self._sync_edit_action_states())
        self.viewport.geometryEdited.connect(self._on_geometry_edited)
        self.viewport.geometryCommandCommitted.connect(
            self._on_geometry_command_committed)
        self.viewport.undoRequested.connect(self._undo_edit)
        self.viewport.redoRequested.connect(self._redo_edit)
        self.viewport.animationFrameChanged.connect(
            self._update_snapshot_frame_text)
        self.viewport.editHint.connect(
            lambda text: self._notify(text, 30000)
        )
        self.viewport.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.viewport.customContextMenuRequested.connect(
            self._show_viewport_context_menu)

        self.asset_tree = QTreeWidget()
        self.asset_tree.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.asset_tree.setHeaderLabels(["Asset", "Status"])
        self.asset_tree.setUniformRowHeights(True)
        self.asset_tree.setIndentation(12)
        self.asset_tree.setAnimated(False)
        self.asset_tree.setIconSize(QSize(10, 10))
        self.asset_tree.setStyleSheet(
            "QTreeWidget::item { padding-top: 0px; padding-bottom: 0px; "
            "padding-left: 1px; padding-right: 1px; }"
        )
        asset_header = self.asset_tree.header()
        asset_header.setSectionsMovable(False)
        asset_header.setStretchLastSection(False)
        asset_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        asset_header.setSectionResizeMode(1,
                                          QHeaderView.ResizeMode.ResizeToContents)
        self.asset_tree.currentItemChanged.connect(self._on_tree_node_selected)
        self.asset_tree.itemDoubleClicked.connect(self._on_tree_double_clicked)
        self.asset_tree.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.asset_tree.customContextMenuRequested.connect(
            self._show_asset_context_menu)
        from PySide6.QtWidgets import QLineEdit
        self.tree_search = QLineEdit()
        self.tree_search.setPlaceholderText("Filter objects/textures...")
        self.tree_search.textChanged.connect(self._filter_asset_tree)
        self.texture_list = QListWidget()
        self.texture_list.setIconSize(QPixmap(96, 96).size())
        self.texture_list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.texture_list.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.texture_list.customContextMenuRequested.connect(
            self._show_texture_context_menu)
        self.texture_list.itemDoubleClicked.connect(
            lambda item: self._preview_family_texture(
                item.data(Qt.ItemDataRole.UserRole)))
        self.resolve_tree = QTreeWidget()
        self.resolve_tree.setHeaderLabels(["Resource", "Status", "Path"])
        self.resolve_tree.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.resolve_tree.customContextMenuRequested.connect(
            self._show_resolve_context_menu)
        self.setbas_tree = QTreeWidget()
        self.setbas_tree.setHeaderLabels(["Resource", "Payload", "Size", "Offset"])
        self.setbas_tree.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        select_all_setbas = QAction(self.setbas_tree)
        select_all_setbas.setShortcut(QKeySequence.StandardKey.SelectAll)
        select_all_setbas.setShortcutContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut)
        select_all_setbas.triggered.connect(self.setbas_tree.selectAll)
        self.setbas_tree.addAction(select_all_setbas)
        self.setbas_tree.setIndentation(10)
        self.setbas_tree.setUniformRowHeights(True)
        self.setbas_tree.itemDoubleClicked.connect(
            self._on_setbas_item_double_clicked)
        self.setbas_tree.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.setbas_tree.customContextMenuRequested.connect(
            self._show_setbas_context_menu)
        setbas_header = self.setbas_tree.header()
        setbas_header.setSectionsMovable(False)
        setbas_header.setStretchLastSection(False)
        setbas_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 4):
            setbas_header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents)
        self.setbas_label = QLabel("No SET.BAS loaded.")
        self.setbas_label.setWordWrap(True)
        self.poly_uv_label = QLabel("Select a polygon in the viewport.")
        self.poly_uv_label.setMinimumHeight(200)
        self.fx_combo = QComboBox()
        self.fx_combo.setToolTip(
            "Choose an FX element to select it and edit its vertices.")
        self.fx_combo.currentIndexChanged.connect(self._on_fx_selected)
        self.blocks_list = QListWidget()
        self.blocks_list.currentRowChanged.connect(self._on_block_selected)
        self.repair_target_combo = QComboBox()
        self.repair_source_spin = QSpinBox()
        self.repair_source_spin.setRange(0, 65535)
        self.repair_copy_button = QPushButton("Copy Mapping")
        self.repair_copy_button.clicked.connect(self._plan_copy_style)
        self.repair_planar_button = QPushButton("Create Planar Mapping")
        self.repair_planar_button.clicked.connect(self._plan_planar)
        self.repair_preview = QListWidget()
        self.repair_apply_button = QPushButton("Apply")
        self.repair_apply_button.clicked.connect(self._apply_repair_in_memory)
        self.repair_revert_button = QPushButton("Revert")
        self.repair_revert_button.clicked.connect(self._revert_repairs)
        self.repair_save_button = QPushButton("Save As...")
        self.repair_save_button.clicked.connect(self._save_repaired_as)
        self.chunk_tree = QTreeWidget()
        self.chunk_tree.setHeaderLabels(["Chunk", "Size", "Offset"])
        self.warning_list = QListWidget()
        self.checks_list = QListWidget()
        self.log_list = QListWidget()
        for widget in (
                self.blocks_list, self.repair_preview,
                self.warning_list, self.checks_list, self.log_list):
            widget.setContextMenuPolicy(
                Qt.ContextMenuPolicy.CustomContextMenu)
            widget.customContextMenuRequested.connect(
                lambda pos, source=widget:
                self._show_generic_item_context_menu(source, pos))
        # tool-side dependency choice profile (~/.openuastudio), never asset-side
        self._profile = DependencyProfile()
        if self._profile.load_error:
            self.log_list.addItem(f"profile: {self._profile.load_error}")

        self._build_toolbar()
        self._build_layout()
        self._sync_geometry_save_controls()
        self.statusBar().showMessage(
            "Model saves are verified; overwriting a loose skeleton requires "
            "confirmation and creates a .bak backup."
        )

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        # Hidden windows are common in focused UI tests and have never exposed
        # user-editable state. A visible workbench must not silently discard it.
        if self.isVisible() and not self._confirm_discard_geometry():
            event.ignore()
            return
        self._cancel_live_scale()
        event.accept()

    # -- UI scaffolding --------------------------------------------------------

    def _notify(self, text: str, timeout: int = 5000) -> None:
        """Publish concise user-facing activity in the bottom status bar."""

        if text:
            self.statusBar().showMessage(text, timeout)

    def _checkable(self, text: str, slot, checked: bool = False) -> QAction:
        action = QAction(text, self)
        action.setCheckable(True)
        action.toggled.connect(slot)
        action.setChecked(checked)
        return action

    @staticmethod
    def _prepare_context_item(widget, position):
        item = widget.itemAt(position)
        if item is not None and not item.isSelected() \
                and item.flags() & Qt.ItemFlag.ItemIsSelectable:
            widget.clearSelection()
            item.setSelected(True)
            widget.setCurrentItem(item)
        return item

    @staticmethod
    def _widget_item_text(widget, item) -> str:
        if isinstance(widget, QTreeWidget):
            return "\t".join(item.text(column)
                             for column in range(widget.columnCount()))
        return item.text()

    def _copy_widget_items(self, widget, selected_only: bool = True) -> None:
        items = widget.selectedItems() if selected_only else []
        if not selected_only:
            if isinstance(widget, QTreeWidget):
                def collect(parent):
                    rows = [parent]
                    for index in range(parent.childCount()):
                        rows.extend(collect(parent.child(index)))
                    return rows
                for index in range(widget.topLevelItemCount()):
                    items.extend(collect(widget.topLevelItem(index)))
            else:
                items = [widget.item(index) for index in range(widget.count())]
        text = "\n".join(self._widget_item_text(widget, item)
                         for item in items)
        if text:
            QApplication.clipboard().setText(text)
            self._notify(
                f"Copied {len(items)} item(s) to the clipboard.", 5000)

    def _copy_text(self, text: str, success_message: str) -> None:
        if not text:
            self._notify("Nothing was copied.")
            return
        QApplication.clipboard().setText(text)
        self._notify(success_message, 5000)

    def _copy_texture_names(self, names: list[str]) -> None:
        count = len(names)
        message = ("Texture name copied successfully."
                   if count == 1 else
                   f"{count} texture names copied successfully.")
        self._copy_text("\n".join(names), message)

    def _show_generic_item_context_menu(self, widget, position) -> None:
        self._prepare_context_item(widget, position)
        menu = QMenu(widget)
        selected = widget.selectedItems()
        copy_selected = menu.addAction(
            f"Copy selected ({len(selected)})" if len(selected) > 1
            else "Copy selected")
        copy_selected.setEnabled(bool(selected))
        copy_selected.triggered.connect(
            lambda: self._copy_widget_items(widget, True))
        copy_all = menu.addAction("Copy all")
        copy_all.triggered.connect(
            lambda: self._copy_widget_items(widget, False))
        menu.addSeparator()
        select_all = menu.addAction("Select all")
        select_all.triggered.connect(widget.selectAll)
        if isinstance(widget, QTreeWidget):
            menu.addSeparator()
            menu.addAction("Expand all", widget.expandAll)
            menu.addAction("Collapse all", widget.collapseAll)
        menu.exec(widget.viewport().mapToGlobal(position))

    def _show_setbas_context_menu(self, position) -> None:
        item = self._prepare_context_item(self.setbas_tree, position)
        menu = QMenu(self.setbas_tree)
        if item is not None and item.data(0, Qt.ItemDataRole.UserRole) is None:
            self.setbas_tree.clearSelection()
            label = "Collapse group" if item.isExpanded() else "Expand group"
            menu.addAction(label, lambda: item.setExpanded(not item.isExpanded()))
            menu.addSeparator()
        resources = self._setbas_selected_resources()
        preview = menu.addAction("Preview")
        preview.setEnabled(
            len(resources) == 1
            and resources[0].class_id.lower() in ("sklt.class", "ilbm.class"))
        preview.triggered.connect(self._preview_setbas_resource)
        extract = menu.addAction(
            f"Extract selected... ({len(resources)})" if len(resources) > 1
            else "Extract selected...")
        extract.setEnabled(bool(resources))
        extract.triggered.connect(self._extract_setbas_selected)
        menu.addAction("Extract entire archive...",
                       self._extract_setbas_archive).setEnabled(
                           self._setbas is not None)
        if resources:
            menu.addSeparator()
            copy_names = menu.addAction("Copy resource name(s)")
            copy_names.triggered.connect(lambda: self._copy_text(
                "\n".join(resource.resource_name for resource in resources),
                f"Copied {len(resources)} BAS resource name(s)."))
        menu.addSeparator()
        menu.addAction("Select all resources", self.setbas_tree.selectAll)
        menu.addAction("Expand all", self.setbas_tree.expandAll)
        menu.addAction("Collapse all", self.setbas_tree.collapseAll)
        if self._last_output_directory is not None:
            menu.addAction("Open last output folder",
                           self._open_last_output_folder)
        menu.exec(self.setbas_tree.viewport().mapToGlobal(position))

    def _asset_item_path(self, item) -> Path | None:
        if item is None or self._family is None:
            return None
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data:
            return None
        kind, payload = data
        if kind == "base":
            return self._family.base_path
        if kind in ("skeleton", "child") and payload is not None:
            ref = getattr(payload, "skeleton_ref", None)
            return Path(ref.path) if ref and ref.path else None
        if kind == "texture":
            ref = self._family.texture_refs.get(payload)
            return Path(ref.path) if ref and ref.path else None
        if kind == "animation":
            ref = self._family.animation_refs.get(payload)
            return Path(ref.path) if ref and ref.path else None
        return None

    def _reveal_asset_item(self, item) -> None:
        path = self._asset_item_path(item)
        if path is None:
            self.statusBar().showMessage("No on-disk file to reveal.")
            return
        subprocess.Popen(["explorer", "/select,", str(path)])

    def _select_and_frame_owner(self, owner: str) -> None:
        self._select_owner(owner)
        self.viewport.frame_owner(owner)

    def _play_asset_animation(self, item) -> None:
        if self._family is None or item is None:
            return
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data or data[0] != "animation":
            return
        name = data[1]
        owner = self._tree_owner_for_item(item)
        if owner:
            self._select_owner(owner)
        self._sync_animation_controls()
        if name not in self._family.animations or not self.viewport.has_animation:
            self.statusBar().showMessage(
                f"Animation {name} is not available in the current preview.")
            return
        self.play_button.setChecked(True)
        self.statusBar().showMessage(f"Playing animation: {name}")

    def _pause_asset_animation(self) -> None:
        self.play_button.setChecked(False)
        self.statusBar().showMessage("Animation paused.")

    def _show_asset_context_menu(self, position) -> None:
        item = self._prepare_context_item(self.asset_tree, position)
        if item is None:
            return
        data = item.data(0, Qt.ItemDataRole.UserRole)
        menu = QMenu(self.asset_tree)
        owner = self._tree_owner_for_item(item)
        if owner:
            menu.addAction("Select and frame model",
                           lambda: self._select_and_frame_owner(owner))
        if data:
            kind, payload = data
            if kind == "texture":
                preview = menu.addAction("Preview texture")
                preview.setEnabled(self._family is not None
                                   and payload in self._family.textures)
                preview.triggered.connect(
                    lambda: self._preview_family_texture(payload))
                export = menu.addAction("Export texture as PNG...")
                export.setEnabled(self._family is not None
                                  and payload in self._family.textures)
                export.triggered.connect(
                    lambda: self._export_family_textures_png([payload]))
            elif kind == "animation":
                if self.play_button.isChecked():
                    pause = menu.addAction("Pause")
                    pause.triggered.connect(self._pause_asset_animation)
                else:
                    play = menu.addAction("Play")
                    play.setEnabled(self._family is not None
                                    and payload in self._family.animations)
                    play.triggered.connect(
                        lambda: self._play_asset_animation(item))
        path = self._asset_item_path(item)
        if path is not None:
            menu.addAction("Reveal source file",
                           lambda: self._reveal_asset_item(item))
        menu.addSeparator()
        menu.addAction("Copy item", lambda: self._copy_text(
            self._widget_item_text(self.asset_tree, item),
            "Asset information copied successfully."))
        menu.addAction("Expand all", self.asset_tree.expandAll)
        menu.addAction("Collapse all", self.asset_tree.collapseAll)
        menu.exec(self.asset_tree.viewport().mapToGlobal(position))

    def _selected_texture_names(self) -> list[str]:
        names = []
        for item in self.texture_list.selectedItems():
            name = item.data(Qt.ItemDataRole.UserRole)
            if name and name not in names:
                names.append(name)
        return names

    def _show_texture_context_menu(self, position) -> None:
        self._prepare_context_item(self.texture_list, position)
        names = self._selected_texture_names()
        menu = QMenu(self.texture_list)
        preview = menu.addAction("Preview")
        preview.setEnabled(len(names) == 1)
        preview.triggered.connect(
            lambda: self._preview_family_texture(names[0]) if names else None)
        export = menu.addAction(
            f"Export selected as PNG... ({len(names)})" if len(names) > 1
            else "Export selected as PNG...")
        export.setEnabled(bool(names))
        export.triggered.connect(
            lambda: self._export_family_textures_png(names))
        if len(names) == 1 and self._family is not None:
            ref = self._family.texture_refs.get(names[0])
            if ref and ref.path:
                menu.addAction(
                    "Reveal source file",
                    lambda: subprocess.Popen(
                        ["explorer", "/select,", str(ref.path)]))
        if names:
            menu.addSeparator()
            menu.addAction("Copy texture name(s)",
                           lambda: self._copy_texture_names(names))
        menu.addSeparator()
        menu.addAction("Select all textures", self.texture_list.selectAll)
        menu.exec(self.texture_list.viewport().mapToGlobal(position))

    def _show_resolve_context_menu(self, position) -> None:
        item = self._prepare_context_item(self.resolve_tree, position)
        if item is None:
            return
        menu = QMenu(self.resolve_tree)
        target = self._selected_resolve_target()
        for text, slot in (
                ("Use Selected Source", self._use_selected_candidate),
                ("Keep for Session", self._keep_for_session),
                ("Assign File...", self._assign_manual_file),
                ("Revert Source", self._clear_override),
                ("Reveal Source File", self._reveal_in_folder)):
            action = menu.addAction(text, slot)
            action.setEnabled(target is not None)
        menu.addSeparator()
        menu.addAction("Copy row", lambda: self._copy_text(
            self._widget_item_text(self.resolve_tree, item),
            "Dependency row copied successfully."))
        menu.exec(self.resolve_tree.viewport().mapToGlobal(position))

    def _build_toolbar(self) -> None:
        # --- menus: everything advanced lives here, not on the toolbar ---
        file_menu = self.menuBar().addMenu("&File")
        open_base = QAction("Open BASE...", self)
        open_base.setShortcut(QKeySequence.StandardKey.Open)
        open_base.triggered.connect(self.open_base_dialog)
        file_menu.addAction(open_base)
        open_family = QAction("Open Asset Family (manual)...", self)
        open_family.triggered.connect(self.open_family_dialog)
        file_menu.addAction(open_family)
        open_setbas = QAction("Open BAS Archive", self)
        open_setbas.triggered.connect(self.open_setbas_dialog)
        file_menu.addAction(open_setbas)
        add_root = QAction("Select extra asset root...", self)
        add_root.triggered.connect(self.select_root_dialog)
        file_menu.addAction(add_root)
        file_menu.addSeparator()
        reload_action = QAction("Reload", self)
        reload_action.setShortcut(QKeySequence.StandardKey.Refresh)
        reload_action.triggered.connect(self.reload_family)
        file_menu.addAction(reload_action)
        file_menu.addSeparator()
        export_md = QAction("Export Markdown report...", self)
        export_md.triggered.connect(lambda: self.export_report("md"))
        file_menu.addAction(export_md)
        export_json = QAction("Export JSON report...", self)
        export_json.triggered.connect(lambda: self.export_report("json"))
        file_menu.addAction(export_json)

        edit_menu = self.menuBar().addMenu("&Edit")
        self.edit_menu = edit_menu
        self.edit_toggle_action = QAction("Edit Mode", self)
        self.edit_toggle_action.setShortcut(QKeySequence("Tab"))
        self.edit_toggle_action.setShortcutContext(
            Qt.ShortcutContext.WindowShortcut)
        self.edit_toggle_action.triggered.connect(
            self._toggle_global_edit_mode)
        edit_menu.addAction(self.edit_toggle_action)
        edit_menu.addSeparator()
        self.edit_undo_action = QAction("Undo", self)
        self.edit_undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        self.edit_undo_action.setShortcutContext(
            Qt.ShortcutContext.WindowShortcut)
        self.edit_undo_action.triggered.connect(self._undo_edit)
        edit_menu.addAction(self.edit_undo_action)
        self.edit_redo_action = QAction("Redo", self)
        self.edit_redo_action.setShortcuts([
            QKeySequence(QKeySequence.StandardKey.Redo),
            QKeySequence("Ctrl+Shift+Z"),
        ])
        self.edit_redo_action.setShortcutContext(
            Qt.ShortcutContext.WindowShortcut)
        self.edit_redo_action.triggered.connect(self._redo_edit)
        edit_menu.addAction(self.edit_redo_action)
        self.edit_reset_action = QAction("Reset Model...", self)
        self.edit_reset_action.triggered.connect(self._reset_model)
        edit_menu.addAction(self.edit_reset_action)
        edit_menu.addSeparator()
        self.edit_select_all_action = QAction("Select All Vertices", self)
        self.edit_select_all_action.triggered.connect(
            self.viewport.select_all_edit_vertices)
        edit_menu.addAction(self.edit_select_all_action)
        self.edit_select_none_action = QAction("Deselect All Vertices", self)
        self.edit_select_none_action.triggered.connect(
            self.viewport.select_no_edit_vertices)
        edit_menu.addAction(self.edit_select_none_action)
        edit_menu.addSeparator()
        self.edit_copy_action = QAction(
            "Copy Selected Vertex Positions", self)
        self.edit_copy_action.triggered.connect(
            self._copy_selected_vertex_positions)
        edit_menu.addAction(self.edit_copy_action)
        self.edit_paste_action = QAction("Paste Vertex Positions", self)
        self.edit_paste_action.triggered.connect(
            self._paste_selected_vertex_positions)
        edit_menu.addAction(self.edit_paste_action)
        self.edit_scale_action = QAction("Scale Selection / Model...", self)
        self.edit_scale_action.triggered.connect(self._scale_selected_geometry)
        edit_menu.addAction(self.edit_scale_action)
        self.edit_frame_action = QAction("Frame Selected Model", self)
        self.edit_frame_action.triggered.connect(self._frame_selected)
        edit_menu.addAction(self.edit_frame_action)
        edit_menu.addSeparator()
        self.overwrite_action = QAction("Overwrite", self)
        self.overwrite_action.setEnabled(False)
        self.overwrite_action.triggered.connect(self._overwrite_model)
        edit_menu.addAction(self.overwrite_action)
        self.save_as_action = QAction("Save As...", self)
        self.save_as_action.setEnabled(False)
        self.save_as_action.triggered.connect(self._save_model_as)
        edit_menu.addAction(self.save_as_action)

        view_menu = self.menuBar().addMenu("&View")
        self.sen_check = self._checkable("SEN2 volume",
                                         self.viewport.set_show_sen, True)
        self.wire_check = self._checkable("Wire overlay",
                                          self.viewport.set_wire_overlay, True)
        self.cull_check = self._checkable("Backface cull",
                                          self.viewport.set_backface_cull,
                                          True)
        self.axes_check = self._checkable("Axes",
                                          self.viewport.set_show_axes, True)
        self.grid_check = self._checkable("Grid",
                                          self.viewport.set_show_grid, True)
        self.overlay_check = self._checkable(
            "Preview issues overlay", self.viewport.set_overlay_visible, True)
        self.mapping_diag_check = self._checkable(
            "Mapping diagnostics", self._set_mapping_diagnostics, True)
        for action in (self.sen_check, self.wire_check, self.cull_check,
                       self.axes_check, self.grid_check, self.overlay_check,
                       self.mapping_diag_check):
            view_menu.addAction(action)
        view_menu.addSeparator()
        frame_all_action = QAction("Frame full family", self)
        frame_all_action.triggered.connect(self.viewport.frame_all)
        view_menu.addAction(frame_all_action)
        reset_cam = QAction("Reset camera", self)
        reset_cam.triggered.connect(self.viewport.reset_view)
        view_menu.addAction(reset_cam)
        help_action = QAction("Navigation help", self)
        help_action.triggered.connect(lambda: self.statusBar().showMessage(
            "LMB orbit | RMB/MMB pan | wheel zoom | click select | "
            "double-click / F frame selected | "
            "Tab Edit Mode (drag selected vertex move, G/R/S transform, "
            "X/Y/Z axis, Ctrl+drag empty space box-select, A all, "
            "Ctrl+Z undo)",
            10000))
        view_menu.addAction(help_action)

        tools_menu = self.menuBar().addMenu("&Tools")
        goto_action = QAction("Go to polyID...", self)
        goto_action.triggered.connect(self._goto_poly_dialog)
        tools_menu.addAction(goto_action)
        tools_menu.addSeparator()
        setbas_tools_menu = tools_menu.addMenu("BAS Archive")
        extract_setbas_action = QAction("Extract current archive...", self)
        extract_setbas_action.triggered.connect(self._extract_setbas_archive)
        setbas_tools_menu.addAction(extract_setbas_action)
        metadata_action = QAction("Export scene metadata...", self)
        metadata_action.triggered.connect(self._export_setbas_metadata)
        setbas_tools_menu.addAction(metadata_action)
        open_output = QAction("Open last output folder", self)
        open_output.triggered.connect(self._open_last_output_folder)
        setbas_tools_menu.addAction(open_output)

        conversion_menu = tools_menu.addMenu("Texture conversion")
        conv_to_png = QAction("ILBM/VBMP to PNG...", self)
        conv_to_png.triggered.connect(self._convert_ilbm_to_png_dialog)
        conversion_menu.addAction(conv_to_png)
        conv_vbmp_to_ilbm = QAction("VBMP to standalone ILBM...", self)
        conv_vbmp_to_ilbm.triggered.connect(
            self._convert_vbmp_to_ilbm_dialog)
        conversion_menu.addAction(conv_vbmp_to_ilbm)
        conv_to_ilbm = QAction("PNG to ILBM (matching templates)...", self)
        conv_to_ilbm.triggered.connect(self._convert_png_to_ilbm_dialog)
        conversion_menu.addAction(conv_to_ilbm)

        wireframe_action = QAction("Wireframe Editor", self)
        wireframe_action.triggered.connect(self._open_wireframe_editor)
        tools_menu.addSeparator()
        tools_menu.addAction(wireframe_action)
        map_editor_action = QAction("Map Editor", self)
        map_editor_action.triggered.connect(self._open_map_editor)
        tools_menu.addAction(map_editor_action)

        diagnostics_menu = self.menuBar().addMenu("&Diagnostics")
        show_warnings = QAction("Warnings", self)
        show_warnings.triggered.connect(lambda: self._show_diagnostics(0))
        diagnostics_menu.addAction(show_warnings)
        show_validation = QAction("Validation", self)
        show_validation.triggered.connect(lambda: self._show_diagnostics(1))
        diagnostics_menu.addAction(show_validation)
        show_log = QAction("Log", self)
        show_log.triggered.connect(lambda: self._show_diagnostics(2))
        diagnostics_menu.addAction(show_log)
        diagnostics_menu.addSeparator()
        hide_diagnostics = QAction("Hide diagnostics panel", self)
        hide_diagnostics.triggered.connect(self._hide_diagnostics)
        diagnostics_menu.addAction(hide_diagnostics)
        diagnostics_menu.addSeparator()
        clear_log = QAction("Clear log", self)
        clear_log.triggered.connect(self._clear_diagnostics)
        diagnostics_menu.addAction(clear_log)

        self.object_info_menu = self.menuBar().addMenu("Object Info")
        self.object_info_menu.setStyleSheet("""
            QMenu::item { color: #69c9e8; padding: 5px 12px; }
            QMenu::item:disabled { color: #69c9e8; }
            QMenu::separator { background: #397f96; height: 1px;
                               margin: 4px 8px; }
        """)
        self._set_object_info(["No asset selected."])

        # --- toolbar: essentials only ---
        toolbar = QToolBar("Workbench", self)
        toolbar.setMovable(False)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, toolbar)

        toolbar.addAction(open_base)
        toolbar.addAction(open_setbas)
        toolbar.addAction(reload_action)
        toolbar.addSeparator()

        self.mode_combo = QComboBox()
        for mode in VIEW_MODES:
            label = {"wireframe": "Wireframe",
                     "solid": "Solid",
                     "materials": "Material groups",
                     "textured": "Textured"}[mode]
            self.mode_combo.addItem(label, mode)
        self.mode_combo.setCurrentIndex(VIEW_MODES.index("textured"))
        self.mode_combo.currentIndexChanged.connect(
            self._on_view_mode_changed
        )
        self.viewport.set_mode("textured")
        toolbar.addWidget(QLabel(" View: "))
        toolbar.addWidget(self.mode_combo)

        toolbar.addWidget(QLabel(" View preset: "))
        self.toolbar_view_preset_combo = QComboBox()
        self.toolbar_view_preset_combo.addItems(VIEW_PRESETS)
        self.toolbar_view_preset_combo.currentTextChanged.connect(
            self._on_toolbar_view_preset_changed)
        self.viewport.manualCameraChanged.connect(
            self._on_manual_camera_changed)
        toolbar.addWidget(self.toolbar_view_preset_combo)

        # Animation controls (enabled only when a VANM is loaded)
        anim_bar = QToolBar("Animation", self)
        anim_bar.setMovable(False)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, anim_bar)

        anim_bar.addWidget(QLabel(" VANM preview: "))
        self.play_button = QPushButton("Play")
        self.play_button.setCheckable(True)
        self.play_button.setEnabled(False)
        self.play_button.toggled.connect(self._toggle_play)
        anim_bar.addWidget(self.play_button)

        self.step_button = QPushButton("Step")
        self.step_button.setEnabled(False)
        self.step_button.clicked.connect(self.viewport.step_animation)
        anim_bar.addWidget(self.step_button)

        anim_bar.addWidget(QLabel(" Speed: "))
        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setRange(0.05, 8.0)
        self.speed_spin.setSingleStep(0.25)
        self.speed_spin.setValue(1.0)
        self.speed_spin.valueChanged.connect(self._on_animation_speed_changed)
        anim_bar.addWidget(self.speed_spin)
        self.global_undo_button = QPushButton("< Undo")
        self.global_undo_button.setEnabled(False)
        self.global_undo_button.setMinimumWidth(88)
        self.global_undo_button.setToolTip(
            "Undo the latest geometry, texture or UV edit.")
        self.global_undo_button.setStyleSheet(
            "QPushButton { background: #276c7a; color: white; "
            "border: 1px solid #58b7c8; padding: 4px 10px; } "
            "QPushButton:hover:enabled { background: #33899a; }")
        self.global_undo_button.clicked.connect(self._undo_edit)
        self.global_edit_button = QPushButton("Edit Mode")
        self.global_edit_button.setCheckable(True)
        self.global_edit_button.setMinimumWidth(96)
        self.global_edit_button.setToolTip(
            "Enable vertex editing globally in every tab (shortcut: Tab).")
        self.global_edit_button.setStyleSheet(
            "QPushButton { background: #7b3947; color: white; "
            "border: 1px solid #c76573; padding: 4px 10px; } "
            "QPushButton:hover { background: #914555; } "
            "QPushButton:checked { background: #c24d5e; "
            "border-color: #ff8997; font-weight: bold; }")
        self.global_edit_button.toggled.connect(self._set_global_edit_mode)
        self.global_redo_button = QPushButton("Redo >")
        self.global_redo_button.setEnabled(False)
        self.global_redo_button.setMinimumWidth(88)
        self.global_redo_button.setToolTip(
            "Redo the latest geometry, texture or UV edit.")
        self.global_redo_button.setStyleSheet(
            "QPushButton { background: #276c7a; color: white; "
            "border: 1px solid #58b7c8; padding: 4px 10px; } "
            "QPushButton:hover:enabled { background: #33899a; }")
        self.global_redo_button.clicked.connect(self._redo_edit)

    def _build_layout(self) -> None:
        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        tabs.setUsesScrollButtons(True)
        tabs.tabBar().setExpanding(True)
        tabs.tabBar().setStyleSheet("""
            QTabBar::tab {
                background: #602d37;
                color: #f8edef;
                border: 1px solid #7c4049;
                border-bottom: none;
                padding: 7px 14px;
                font-weight: normal;
            }
            QTabBar::tab:selected {
                background: #a84552;
                border-color: #d66b77;
                color: #ffffff;
            }
            QTabBar::tab:hover:!selected {
                background: #7b3742;
            }
        """)
        tabs.setMinimumWidth(330)

        # SET.BAS is a primary workflow, not an advanced diagnostic panel.
        setbas_panel = QWidget()
        setbas_layout = QVBoxLayout(setbas_panel)
        setbas_layout.setContentsMargins(3, 5, 3, 4)
        setbas_layout.setSpacing(4)
        setbas_layout.addWidget(self.setbas_label)
        from PySide6.QtWidgets import QLineEdit
        self.setbas_search = QLineEdit()
        self.setbas_search.setPlaceholderText(
            "Search embedded resources (name or class)...")
        self.setbas_search.textChanged.connect(self._filter_setbas_tree)
        setbas_layout.addWidget(self.setbas_search)
        setbas_layout.addWidget(self.setbas_tree, 1)
        setbas_buttons = QGridLayout()
        setbas_buttons.setHorizontalSpacing(4)
        self.setbas_preview_button = QPushButton("Preview")
        self.setbas_preview_button.clicked.connect(
            self._preview_setbas_resource)
        self.setbas_preview_button.setEnabled(False)
        setbas_buttons.addWidget(self.setbas_preview_button, 0, 0)
        self.setbas_extract_button = QPushButton("Extract selected...")
        self.setbas_extract_button.clicked.connect(
            self._extract_setbas_selected)
        self.setbas_extract_button.setEnabled(False)
        setbas_buttons.addWidget(self.setbas_extract_button, 0, 1)
        self.setbas_extract_all_button = QPushButton("Extract archive...")
        self.setbas_extract_all_button.clicked.connect(
            self._extract_setbas_archive)
        self.setbas_extract_all_button.setEnabled(False)
        setbas_buttons.addWidget(self.setbas_extract_all_button, 1, 0)
        self.setbas_open_output_button = QPushButton("Open output folder")
        self.setbas_open_output_button.clicked.connect(
            self._open_last_output_folder)
        self.setbas_open_output_button.setEnabled(False)
        setbas_buttons.addWidget(self.setbas_open_output_button, 1, 1)
        setbas_layout.addLayout(setbas_buttons)

        # Asset family browser moved to the right so the 3D viewport gets the
        # whole left/centre area.
        asset_panel = QWidget()
        asset_layout = QVBoxLayout(asset_panel)
        asset_layout.setContentsMargins(2, 2, 2, 2)
        asset_layout.setSpacing(2)
        asset_layout.addWidget(self.tree_search)
        asset_layout.addWidget(self.asset_tree, 1)

        textures_panel = QWidget()
        textures_layout = QVBoxLayout(textures_panel)
        textures_layout.setContentsMargins(5, 5, 5, 5)
        textures_layout.setSpacing(4)
        textures_layout.addWidget(self.texture_list, 1)
        self.texture_export_button = QPushButton("Export selected as PNG...")
        self.texture_export_button.clicked.connect(self._export_texture_png)
        textures_layout.addWidget(self.texture_export_button)

        model_panel = QWidget()
        model_layout = QVBoxLayout(model_panel)
        model_layout.setContentsMargins(5, 5, 5, 5)
        model_layout.setSpacing(6)
        model_box = QGroupBox("Model and texture editing")
        model_box_layout = QVBoxLayout(model_box)

        nudge_box = QGroupBox("Nudge selected vertices")
        nudge_layout = QGridLayout(nudge_box)
        nudge_layout.setContentsMargins(6, 6, 6, 6)
        nudge_layout.setHorizontalSpacing(3)
        nudge_layout.setVerticalSpacing(3)
        self.nudge_buttons = []
        directions = (
            ("↖", -1, -1, 0, 0), ("↑", 0, -1, 0, 1),
            ("↗", 1, -1, 0, 2), ("←", -1, 0, 1, 0),
            ("→", 1, 0, 1, 2), ("↙", -1, 1, 2, 0),
            ("↓", 0, 1, 2, 1), ("↘", 1, 1, 2, 2),
        )
        center_hint = QLabel("MOVE")
        center_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        center_hint.setStyleSheet("color: #d7d7d7; font-size: 9px;")
        nudge_layout.addWidget(center_hint, 1, 1)
        for text, dx, dy, row, column in directions:
            button = _HoldNudgeButton(text)
            button.setEnabled(False)
            button.setAutoRepeat(True)
            button.setAutoRepeatDelay(350)
            button.setAutoRepeatInterval(70)
            button.setMinimumHeight(28)
            button.setToolTip(
                "Move selected vertices in the current view; hold for "
                "progressively faster movement.")
            button.clicked.connect(
                lambda _checked=False, x=dx, y=dy, current=button:
                self._nudge_selected_vertices(x, y, current))
            nudge_layout.addWidget(button, row, column)
            self.nudge_buttons.append(button)
        for column in range(3):
            nudge_layout.setColumnStretch(column, 1)
        model_box_layout.addWidget(nudge_box)

        self.model_texture_label = QLabel("Current texture: -")
        self.model_texture_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.model_texture_label.setWordWrap(True)
        model_box_layout.addWidget(self.model_texture_label)
        self.load_texture_button = QPushButton("Load Texture...")
        self.load_texture_button.setEnabled(False)
        self.load_texture_button.setToolTip(
            "Replace the selected material using a texture already available "
            "in the loaded family or SET.BAS. Areas sharing that material "
            "change together, exactly as they will after Save As.")
        self.load_texture_button.clicked.connect(self._load_model_texture)
        model_box_layout.addWidget(self.load_texture_button)

        fx_row = QHBoxLayout()
        fx_row.addWidget(QLabel("FX element:"))
        fx_row.addWidget(self.fx_combo, 1)
        model_box_layout.addLayout(fx_row)

        uv_box = QGroupBox("Texture UV preview")
        uv_layout = QVBoxLayout(uv_box)
        uv_layout.setContentsMargins(5, 5, 5, 5)
        self.uv_editor = UVEditorWidget()
        self.uv_editor.uvChanged.connect(self._on_uv_changed)
        self.uv_editor.editFinished.connect(self._on_uv_edit_finished)
        uv_layout.addWidget(self.uv_editor, 1)
        uv_tools = QHBoxLayout()
        self.uv_select_all_button = QPushButton("Select All UVs")
        self.uv_select_all_button.clicked.connect(self.uv_editor.select_all)
        uv_tools.addWidget(self.uv_select_all_button)
        self.uv_clear_selection_button = QPushButton("Clear Selection")
        self.uv_clear_selection_button.clicked.connect(
            self.uv_editor.select_none)
        uv_tools.addWidget(self.uv_clear_selection_button)
        self.uv_align_horizontal_button = QPushButton("Align Horizontal")
        self.uv_align_horizontal_button.clicked.connect(
            self.uv_editor.align_selected_horizontal)
        uv_tools.addWidget(self.uv_align_horizontal_button)
        self.uv_align_vertical_button = QPushButton("Align Vertical")
        self.uv_align_vertical_button.clicked.connect(
            self.uv_editor.align_selected_vertical)
        uv_tools.addWidget(self.uv_align_vertical_button)
        uv_layout.addLayout(uv_tools)
        uv_buttons = QHBoxLayout()
        self.uv_revert_button = QPushButton("Revert Selected UV")
        self.uv_revert_button.setEnabled(False)
        self.uv_revert_button.clicked.connect(self._revert_selected_uv)
        uv_buttons.addWidget(self.uv_revert_button)
        uv_layout.addLayout(uv_buttons)
        model_box_layout.addWidget(uv_box, 1)

        save_buttons = QGridLayout()
        save_buttons.setHorizontalSpacing(5)
        self.model_save_button = QPushButton("Overwrite")
        self.model_save_button.setEnabled(False)
        self.model_save_button.clicked.connect(self._overwrite_model)
        save_buttons.addWidget(self.model_save_button, 0, 0)
        self.model_save_as_button = QPushButton("Save As...")
        self.model_save_as_button.setEnabled(False)
        self.model_save_as_button.setToolTip(
            "Save the model and its BASE texture/mapping data together.")
        self.model_save_as_button.clicked.connect(self._save_model_as)
        save_buttons.addWidget(self.model_save_as_button, 0, 1)
        save_buttons.setColumnStretch(0, 1)
        save_buttons.setColumnStretch(1, 1)
        model_box_layout.addLayout(save_buttons)

        model_help = QLabel(
            "Drag red vertex: move | Ctrl+click: add/remove | Ctrl+drag "
            "empty space: box select | A / Alt+A: all / none | "
            "G/R/S: move/rotate/scale | "
            "X/Y/Z: constrain | Enter/LMB: confirm | Esc/RMB: cancel | "
            "Double-click empty space: deselect | RMB: edit menu | "
            "Ctrl+Z: undo | Ctrl+Y or Ctrl+Shift+Z: redo"
        )
        model_help.setWordWrap(True)
        model_box_layout.addWidget(model_help)
        model_layout.addWidget(model_box, 1)

        # Mapping repair reuses the existing MappingIndex, material blocks,
        # preview and repair controls; only their UI container changes.
        mapping_panel = QWidget()
        mapping_layout = QVBoxLayout(mapping_panel)
        mapping_layout.setContentsMargins(5, 5, 5, 5)
        mapping_layout.setSpacing(4)
        mapping_overview = QGroupBox("Mapping Overview")
        overview_layout = QVBoxLayout(mapping_overview)
        overview_layout.setContentsMargins(6, 6, 6, 6)
        overview_layout.setSpacing(4)
        self.mapping_diagnostics_label = QLabel()
        self.mapping_diagnostics_label.setWordWrap(True)
        overview_layout.addWidget(self.mapping_diagnostics_label)
        self._update_mapping_diagnostics_summary()
        self.poly_uv_label.setMinimumHeight(90)
        self.poly_uv_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.poly_uv_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        overview_layout.addWidget(self.poly_uv_label, 1)
        self.blocks_list.setMaximumHeight(145)
        overview_layout.addWidget(self.blocks_list)
        mapping_layout.addWidget(mapping_overview, 1)

        repair_box = QGroupBox("Repair Selected Unmapped Polygon")
        repair_layout = QVBoxLayout(repair_box)
        repair_layout.setContentsMargins(6, 6, 6, 6)
        repair_layout.setSpacing(5)
        repair_grid = QGridLayout()
        repair_grid.setHorizontalSpacing(5)
        repair_grid.setVerticalSpacing(4)
        repair_grid.addWidget(QLabel("Target material:"), 0, 0)
        repair_grid.addWidget(self.repair_target_combo, 0, 1)
        repair_grid.addWidget(QLabel("Source polyID:"), 1, 0)
        repair_grid.addWidget(self.repair_source_spin, 1, 1)
        repair_grid.addWidget(self.repair_copy_button, 2, 0)
        repair_grid.addWidget(self.repair_planar_button, 2, 1)
        repair_layout.addLayout(repair_grid)
        self.repair_preview.setMaximumHeight(90)
        repair_layout.addWidget(self.repair_preview)
        repair_buttons = QGridLayout()
        repair_buttons.setHorizontalSpacing(5)
        repair_buttons.addWidget(self.repair_apply_button, 0, 0)
        repair_buttons.addWidget(self.repair_revert_button, 0, 1)
        repair_buttons.addWidget(self.repair_save_button, 0, 2)
        repair_layout.addLayout(repair_buttons)
        mapping_layout.addWidget(repair_box)

        snapshot_panel = QWidget()
        snapshot_layout = QVBoxLayout(snapshot_panel)
        snapshot_layout.setContentsMargins(5, 5, 5, 5)
        snapshot_layout.setSpacing(4)
        studio_box = QGroupBox("Photo Studio")
        studio_layout = QVBoxLayout(studio_box)
        studio_layout.setContentsMargins(6, 6, 6, 6)
        studio_layout.setSpacing(5)

        view_box = QGroupBox("View preset")
        view_layout = QGridLayout(view_box)
        view_layout.setHorizontalSpacing(5)
        view_layout.setVerticalSpacing(4)
        self.snapshot_view_combo = QComboBox()
        self.snapshot_view_combo.addItems(VIEW_PRESETS)
        self.snapshot_view_combo.currentTextChanged.connect(
            self._on_snapshot_preset_changed)
        view_layout.addWidget(self.snapshot_view_combo, 0, 0, 1, 2)
        view_layout.addWidget(QLabel("Zoom:"), 1, 0)
        self.snapshot_zoom_spin = QSpinBox()
        self.snapshot_zoom_spin.setRange(25, 300)
        self.snapshot_zoom_spin.setSuffix("%")
        self.snapshot_zoom_spin.setValue(100)
        self.snapshot_zoom_spin.valueChanged.connect(
            self._on_snapshot_zoom_changed)
        view_layout.addWidget(self.snapshot_zoom_spin, 1, 1)
        self.snapshot_zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self.snapshot_zoom_slider.setRange(25, 300)
        self.snapshot_zoom_slider.setValue(100)
        self.snapshot_zoom_slider.setSingleStep(1)
        self.snapshot_zoom_slider.setPageStep(10)
        self.snapshot_zoom_slider.valueChanged.connect(
            self._on_snapshot_zoom_changed)
        view_layout.addWidget(self.snapshot_zoom_slider, 2, 0, 1, 2)
        self.snapshot_guides_button = QPushButton("Show Guides and Overlays")
        self.snapshot_guides_button.setCheckable(True)
        self.snapshot_guides_button.setChecked(False)
        self.snapshot_guides_button.toggled.connect(
            self._on_snapshot_guides_toggled)
        view_layout.addWidget(self.snapshot_guides_button, 3, 0, 1, 2)
        studio_layout.addWidget(view_box)

        background_box = QGroupBox("Background")
        background_layout = QGridLayout(background_box)
        background_layout.addWidget(QLabel("Custom Color:"), 0, 0)
        self.snapshot_color_button = QPushButton("None")
        self.snapshot_color_button.setFixedSize(64, 26)
        self.snapshot_color_button.setToolTip(
            "No color selected: preview uses the normal dark background and export is transparent.\n"
            "Click to choose a custom background color.")
        self.snapshot_color_button.clicked.connect(
            self._choose_snapshot_color)
        background_layout.addWidget(self.snapshot_color_button, 0, 1)
        self.snapshot_clear_color_button = QPushButton("Clear")
        self.snapshot_clear_color_button.clicked.connect(
            self._clear_snapshot_color)
        self.snapshot_clear_color_button.setEnabled(False)
        background_layout.addWidget(self.snapshot_clear_color_button, 0, 2)
        studio_layout.addWidget(background_box)

        output_box = QGroupBox("Output size")
        output_layout = QGridLayout(output_box)
        self.snapshot_size_combo = QComboBox()
        self.snapshot_size_combo.addItems([
            "Current Viewport", "512 x 512", "1024 x 1024",
            "1920 x 1080", "Custom",
        ])
        self.snapshot_size_combo.setCurrentText("1024 x 1024")
        self.snapshot_size_combo.currentTextChanged.connect(
            self._on_snapshot_size_changed)
        output_layout.addWidget(self.snapshot_size_combo, 0, 0, 1, 2)
        self.snapshot_width_label = QLabel("Width:")
        self.snapshot_width_spin = QSpinBox()
        self.snapshot_width_spin.setRange(64, 8192)
        self.snapshot_width_spin.setValue(1024)
        self.snapshot_height_label = QLabel("Height:")
        self.snapshot_height_spin = QSpinBox()
        self.snapshot_height_spin.setRange(64, 8192)
        self.snapshot_height_spin.setValue(1024)
        for spin in (self.snapshot_width_spin, self.snapshot_height_spin):
            spin.valueChanged.connect(self._on_snapshot_size_changed)
        output_layout.addWidget(self.snapshot_width_label, 1, 0)
        output_layout.addWidget(self.snapshot_width_spin, 1, 1)
        output_layout.addWidget(self.snapshot_height_label, 2, 0)
        output_layout.addWidget(self.snapshot_height_spin, 2, 1)
        self._set_snapshot_custom_size_visible(False)
        studio_layout.addWidget(output_box)

        animation_box = QGroupBox("Animation frame")
        animation_layout = QVBoxLayout(animation_box)
        animation_layout.addWidget(QLabel("Current animation frame"))
        self.snapshot_frame_label = QLabel("No animation")
        self.snapshot_frame_label.setWordWrap(True)
        animation_layout.addWidget(self.snapshot_frame_label)
        animation_buttons = QHBoxLayout()
        self.snapshot_next_frame_button = QPushButton("Next Frame")
        self.snapshot_next_frame_button.clicked.connect(
            self.viewport.step_animation)
        animation_buttons.addWidget(self.snapshot_next_frame_button)
        self.snapshot_reset_frame_button = QPushButton("Reset Frame")
        self.snapshot_reset_frame_button.clicked.connect(
            self.viewport.reset_animation)
        animation_buttons.addWidget(self.snapshot_reset_frame_button)
        animation_layout.addLayout(animation_buttons)
        studio_layout.addWidget(animation_box)

        export_box = QGroupBox("Export")
        export_layout = QGridLayout(export_box)
        export_layout.addWidget(QLabel("Format:"), 0, 0)
        self.snapshot_format_combo = QComboBox()
        supported = {
            bytes(fmt).decode("ascii", errors="ignore").lower()
            for fmt in QImageWriter.supportedImageFormats()
        }
        self._snapshot_formats = {"png"}
        self.snapshot_format_combo.addItem("PNG", "png")
        if "jpeg" in supported or "jpg" in supported:
            self._snapshot_formats.add("jpg")
            self.snapshot_format_combo.addItem("JPEG", "jpg")
        if "webp" in supported:
            self._snapshot_formats.add("webp")
            self.snapshot_format_combo.addItem("WebP", "webp")
        self.snapshot_format_combo.currentIndexChanged.connect(
            self._on_snapshot_format_changed)
        export_layout.addWidget(self.snapshot_format_combo, 0, 1)
        export_layout.addWidget(QLabel("Quality:"), 1, 0)
        self.snapshot_quality_spin = QSpinBox()
        self.snapshot_quality_spin.setRange(1, 100)
        self.snapshot_quality_spin.setValue(95)
        self.snapshot_quality_spin.setEnabled(False)
        export_layout.addWidget(self.snapshot_quality_spin, 1, 1)
        self.snapshot_export_button = QPushButton("Export Image As...")
        self.snapshot_export_button.clicked.connect(self._export_snapshot)
        export_layout.addWidget(self.snapshot_export_button, 2, 0, 1, 2)
        studio_layout.addWidget(export_box)
        studio_layout.addStretch(1)
        snapshot_layout.addWidget(studio_box)
        snapshot_layout.addStretch(1)

        # Dependency resolution remains a resource workflow; editing panels
        # are grouped separately below.
        resolve_panel = QWidget()
        resolve_layout = QVBoxLayout(resolve_panel)
        resolve_layout.setContentsMargins(5, 5, 5, 5)
        resolve_layout.setSpacing(4)
        resolve_help = QLabel(
            "Choose a source for missing or ambiguous dependencies. "
            "Changes apply only to this session.")
        resolve_help.setWordWrap(True)
        resolve_layout.addWidget(resolve_help)
        resolve_layout.addWidget(self.resolve_tree, 1)
        resolve_buttons = QGridLayout()
        resolve_buttons.setHorizontalSpacing(4)
        resolve_buttons.setVerticalSpacing(3)
        self.use_candidate_button = QPushButton("Use Selected Source")
        self.use_candidate_button.clicked.connect(self._use_selected_candidate)
        resolve_buttons.addWidget(self.use_candidate_button, 0, 0)
        self.keep_button = QPushButton("Keep for Session")
        self.keep_button.clicked.connect(self._keep_for_session)
        resolve_buttons.addWidget(self.keep_button, 0, 1)
        self.assign_manual_button = QPushButton("Assign File...")
        self.assign_manual_button.clicked.connect(self._assign_manual_file)
        resolve_buttons.addWidget(self.assign_manual_button, 1, 0)
        self.unload_button = QPushButton("Revert Source")
        self.unload_button.clicked.connect(self._clear_override)
        resolve_buttons.addWidget(self.unload_button, 1, 1)
        resolve_layout.addLayout(resolve_buttons)

        def category_tabs() -> QTabWidget:
            category = QTabWidget()
            category.setDocumentMode(True)
            category.setUsesScrollButtons(True)
            category.tabBar().setExpanding(True)
            category.tabBar().setStyleSheet("""
                QTabBar::tab {
                    background: #60442e;
                    color: #fff3e8;
                    border: 1px solid #825d3d;
                    border-bottom: none;
                    padding: 5px 11px;
                    font-weight: normal;
                }
                QTabBar::tab:selected {
                    background: #c27635;
                    border-color: #e5a45b;
                    color: #ffffff;
                }
                QTabBar::tab:hover:!selected {
                    background: #7b5638;
                }
            """)
            return category

        resources_tabs = category_tabs()
        resources_tabs.addTab(setbas_panel, "BAS")
        resources_tabs.addTab(asset_panel, "Assets")
        resources_tabs.addTab(resolve_panel, "Dependencies")

        editor_tabs = category_tabs()
        editor_tabs.addTab(model_panel, "Model and Texture Editor")
        editor_tabs.addTab(mapping_panel, "Mapping Repair")

        visuals_tabs = category_tabs()
        visuals_tabs.addTab(textures_panel, "Textures")
        visuals_tabs.addTab(snapshot_panel, "Snapshot")

        tabs.addTab(resources_tabs, "Resources")
        tabs.addTab(editor_tabs, "Editor")
        tabs.addTab(visuals_tabs, "Visuals")
        self._right_tabs = tabs
        self._setbas_panel = resources_tabs
        self._bas_panel = setbas_panel
        self._resources_tabs = resources_tabs
        self._assets_panel = asset_panel
        self._editor_tabs = editor_tabs
        self._visuals_tabs = visuals_tabs
        self._model_editor_panel = model_panel
        self._mapping_panel = mapping_panel
        self._snapshot_panel = snapshot_panel
        editor_tabs.currentChanged.connect(self._on_nested_tab_changed)
        visuals_tabs.currentChanged.connect(self._on_nested_tab_changed)
        tabs.currentChanged.connect(self._on_right_tab_changed)
        tabs.setCurrentWidget(resources_tabs)
        self._update_snapshot_color_button()
        self._sync_animation_controls()

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(2)
        edit_controls = QWidget()
        edit_controls_layout = QHBoxLayout(edit_controls)
        edit_controls_layout.setContentsMargins(4, 3, 4, 1)
        edit_controls_layout.setSpacing(4)
        edit_controls_layout.addStretch(1)
        edit_controls_layout.addWidget(self.global_undo_button)
        edit_controls_layout.addWidget(self.global_edit_button)
        edit_controls_layout.addWidget(self.global_redo_button)
        edit_controls_layout.addStretch(1)
        right_layout.addWidget(edit_controls)
        right_layout.addWidget(tabs, 1)
        self._edit_controls_bar = edit_controls

        center_panel = QWidget()
        center_panel.setMinimumHeight(0)
        center_panel.setSizePolicy(QSizePolicy.Policy.Expanding,
                                   QSizePolicy.Policy.Ignored)
        tabs.setMinimumHeight(0)
        tabs.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Ignored)
        center_layout = QVBoxLayout(center_panel)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(2)
        self.completeness_label = QLabel(
            "Open a .base to assemble resources automatically, or a .bas "
            "to browse all packed resources.")
        self.completeness_label.setWordWrap(False)
        self.completeness_label.setMaximumHeight(42)
        self.completeness_label.setMargin(4)
        center_layout.addWidget(self.completeness_label)
        center_layout.addWidget(self.viewport, 1)

        main_split = QSplitter(Qt.Orientation.Horizontal)
        main_split.setChildrenCollapsible(False)
        main_split.setMinimumHeight(0)
        main_split.setSizePolicy(QSizePolicy.Policy.Expanding,
                                 QSizePolicy.Policy.Ignored)
        main_split.addWidget(center_panel)
        main_split.addWidget(right_panel)
        main_split.setStretchFactor(0, 7)
        main_split.setStretchFactor(1, 3)
        main_split.setSizes([930, 390])
        # Diagnostics live inside the central vertical splitter instead of a
        # QDockWidget.  A dock can increase the main window's minimum height on
        # Windows and push a maximized window below the desktop.  This panel
        # always consumes space *inside* the current window geometry.
        diagnostics_tabs = QTabWidget()
        diagnostics_tabs.setDocumentMode(True)
        diagnostics_tabs.setMinimumHeight(0)
        diagnostics_tabs.setSizePolicy(QSizePolicy.Policy.Expanding,
                                       QSizePolicy.Policy.Ignored)
        diagnostics_tabs.addTab(self.warning_list, "Warnings")
        diagnostics_tabs.addTab(self.checks_list, "Validation")
        diagnostics_tabs.addTab(self.log_list, "Log")
        for diagnostic_list in (self.warning_list, self.checks_list,
                                self.log_list):
            diagnostic_list.setMinimumHeight(0)

        diagnostics_panel = QWidget()
        diagnostics_layout = QVBoxLayout(diagnostics_panel)
        diagnostics_layout.setContentsMargins(0, 0, 0, 0)
        diagnostics_layout.addWidget(diagnostics_tabs)
        diagnostics_panel.setMinimumHeight(0)
        diagnostics_panel.setMaximumHeight(200)
        diagnostics_panel.setSizePolicy(QSizePolicy.Policy.Expanding,
                                        QSizePolicy.Policy.Ignored)

        vertical_split = QSplitter(Qt.Orientation.Vertical)
        vertical_split.setChildrenCollapsible(True)
        vertical_split.setMinimumHeight(0)
        vertical_split.setSizePolicy(QSizePolicy.Policy.Expanding,
                                     QSizePolicy.Policy.Ignored)
        vertical_split.addWidget(main_split)
        vertical_split.addWidget(diagnostics_panel)
        vertical_split.setStretchFactor(0, 1)
        vertical_split.setStretchFactor(1, 0)
        self.setCentralWidget(vertical_split)

        diagnostics_panel.hide()
        self._diagnostics_tabs = diagnostics_tabs
        self._diagnostics_dock = diagnostics_panel
        self._diagnostics_splitter = vertical_split

    # -- actions ---------------------------------------------------------------

    def _show_diagnostics(self, index: int) -> None:
        """Show diagnostics strictly inside the existing main-window area."""

        if self._diagnostics_tabs is not None:
            index = max(0, min(index,
                               self._diagnostics_tabs.count() - 1))
            self._diagnostics_tabs.setCurrentIndex(index)
        if self._diagnostics_dock is None:
            return
        self._diagnostics_dock.show()
        splitter = getattr(self, "_diagnostics_splitter", None)
        if splitter is None:
            return

        def fit_inside_window() -> None:
            target = max(96, min(180, splitter.height() // 5))
            splitter.setSizes([max(1, splitter.height() - target), target])

        QTimer.singleShot(0, fit_inside_window)

    def _hide_diagnostics(self) -> None:
        if self._diagnostics_dock is not None:
            self._diagnostics_dock.hide()

    def _clear_diagnostics(self) -> None:
        """Clear every visible diagnostics stream and its viewport overlay."""

        self.warning_list.clear()
        self.checks_list.clear()
        self.log_list.clear()
        self.viewport.set_diagnostics([])
        self.statusBar().showMessage("Diagnostics cleared.", 2000)

    def _set_object_info(self, lines: list[str]) -> None:
        """Store selected-asset details and rebuild the compact info menu."""

        self._object_info_asset_lines = lines or ["No asset selected."]
        self._refresh_object_info_menu()

    def _set_polygon_object_info(self, lines: list[str]) -> None:
        """Keep polygon diagnostics beside the asset data, without repeats."""

        self._object_info_polygon_lines = lines
        self._refresh_object_info_menu()

    def _refresh_object_info_menu(self) -> None:
        menu = getattr(self, "object_info_menu", None)
        if menu is None:
            return
        menu.clear()
        seen = set()
        sections = (self._object_info_asset_lines,
                    self._object_info_polygon_lines)
        wrote_section = False
        for lines in sections:
            unique = []
            for line in lines:
                if line and line not in seen:
                    unique.append(line)
                    seen.add(line)
            if not unique:
                continue
            if wrote_section:
                menu.addSeparator()
            for line in unique:
                action = menu.addAction(line)
                action.setEnabled(False)
            wrote_section = True

    def _window_mode_snapshot(self):
        """Remember the main-window mode before opening auxiliary UI.

        Windows/Qt can report a borderless or snapped-maximized window as a
        normal one.  Record both the state flags and its actual relationship
        to the usable desktop so Preview and Diagnostics cannot restore the
        old, oversized normal geometry outside the monitor.
        """

        screen = self.screen() or QApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else None
        frame = self.frameGeometry()
        near_maximized = False
        if available is not None:
            tolerance = 12
            near_maximized = (
                abs(frame.left() - available.left()) <= tolerance
                and abs(frame.top() - available.top()) <= tolerance
                and abs(frame.right() - available.right()) <= tolerance
                and abs(frame.bottom() - available.bottom()) <= tolerance
            )
        return {
            "full_screen": self.isFullScreen(),
            "maximized": self.isMaximized(),
            "near_maximized": near_maximized,
            "state": self.windowState(),
            "geometry": self.geometry(),
            "available": available,
        }

    def _restore_window_mode(self, snapshot) -> None:
        """Restore/clamp the main window after a child UI layout change.

        A few delayed passes are intentional: native Windows dialogs and Qt
        splitters can post their geometry changes after the triggering slot
        returns.  The passes only enforce the mode that was already active;
        they never maximize a genuinely normal window.
        """

        def restore() -> None:
            if snapshot["full_screen"]:
                if not self.isFullScreen():
                    self.showFullScreen()
                return
            if snapshot["maximized"] or snapshot["near_maximized"]:
                if not self.isMaximized():
                    self.showMaximized()
                return

            screen = self.screen() or QApplication.primaryScreen()
            available = (screen.availableGeometry() if screen is not None
                         else snapshot["available"])
            if available is None:
                self.setWindowState(snapshot["state"])
                return
            rect = snapshot["geometry"]
            # QRect is implicitly shared; copy it before each delayed pass.
            rect = type(rect)(rect)
            rect.setWidth(max(320, min(rect.width(), available.width())))
            rect.setHeight(max(240, min(rect.height(), available.height())))
            max_left = available.right() - rect.width() + 1
            max_top = available.bottom() - rect.height() + 1
            rect.moveLeft(max(available.left(), min(rect.left(), max_left)))
            rect.moveTop(max(available.top(), min(rect.top(), max_top)))
            self.setGeometry(rect)
            self.setWindowState(snapshot["state"])

        delayed = (0, 60, 180) if (
            snapshot["full_screen"] or snapshot["maximized"]
            or snapshot["near_maximized"]
        ) else (0,)
        for delay in delayed:
            QTimer.singleShot(delay, restore)

    def _remember_output_folder(self, folder: str | Path) -> None:
        path = Path(folder)
        if path.is_file():
            path = path.parent
        self._last_output_directory = path
        button = getattr(self, "setbas_open_output_button", None)
        if button is not None:
            button.setEnabled(path.is_dir())

    def _open_last_output_folder(self) -> None:
        folder = self._last_output_directory
        if folder is None or not folder.is_dir():
            QMessageBox.information(
                self, "No output folder",
                "No extraction or conversion output folder is available "
                "yet.")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
            QMessageBox.warning(
                self, "Could not open folder", str(folder))

    def _open_wireframe_editor(self) -> None:
        try:
            from wireframe_editor.window import WireframeEditorWindow
        except Exception as exc:
            QMessageBox.critical(
                self, "Wireframe Editor unavailable",
                f"The integrated editor could not be loaded.\n\n{exc}")
            return
        window = WireframeEditorWindow()
        window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._wireframe_windows.append(window)

        def forget(*_args) -> None:
            if window in self._wireframe_windows:
                self._wireframe_windows.remove(window)

        window.destroyed.connect(forget)
        window.show()
        window.raise_()
        window.activateWindow()

    def _open_map_editor(self) -> None:
        """Launch the integrated Map Editor in a separate process.

        Map Editor uses Tk while the main workbench uses Qt. Keeping their
        event loops in separate processes preserves the original editor
        behavior and avoids toolkit conflicts.
        """

        if getattr(sys, "frozen", False):
            command = [sys.executable, "--map-editor"]
            working_directory = Path(sys.executable).resolve().parent
        else:
            main_path = Path(__file__).resolve().with_name("main.py")
            command = [sys.executable, str(main_path), "--map-editor"]
            working_directory = main_path.parent

        try:
            subprocess.Popen(command, cwd=str(working_directory))
        except OSError as exc:
            QMessageBox.critical(
                self,
                "Map Editor unavailable",
                "The integrated Map Editor could not be launched.\n\n"
                f"{exc}",
            )

    def open_base_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open BASE asset", str(self._last_directory),
            "Urban Assault BASE (*.base *.bas *.BASE *.BAS);;All files (*)",
        )
        if path:
            self.open_base(path)

    def open_base(self, path: str | Path, *, confirm_discard: bool = True) -> None:
        if confirm_discard and not self._confirm_discard_geometry():
            return
        base_path = Path(path)
        if self._family is None or self._family.base_path != base_path:
            # Overrides are per-asset session state; a different asset starts
            # clean so stale bindings cannot leak between files.
            self._overrides = {}
            self._trial_names = set()
            self._kept_names = set()
            self._skipped_names = set()
            self._auto_apply_saved_choices(base_path)
        self._last_directory = base_path.parent
        try:
            family = load_asset_family(base_path, self._extra_roots,
                                       self._overrides, self._setbas)
        except Exception as exc:
            QMessageBox.critical(
                self, "Load failed",
                f"No file was modified.\n\nUnexpected error:\n{exc}",
            )
            return
        self._set_family(family)

    def open_family_dialog(self) -> None:
        sklt, _ = QFileDialog.getOpenFileName(
            self, "Pick the skeleton (.sklt/.skl) - optional, Cancel to skip",
            str(self._last_directory),
            "Skeletons (*.sklt *.skl *.SKLT *.SKL);;All files (*)",
        )
        base, _ = QFileDialog.getOpenFileName(
            self, "Pick the .base file - optional, Cancel to skip",
            str(self._last_directory),
            "Urban Assault BASE (*.base *.bas *.BASE *.BAS);;All files (*)",
        )
        textures, _ = QFileDialog.getOpenFileNames(
            self, "Pick texture files (.ilbm/.ilb/.vbmp) - optional",
            str(self._last_directory),
            "ILBM/VBMP textures (*.ilbm *.ilb *.lbm *.iff *.vbmp "
            "*.ILBM *.ILB *.LBM *.IFF *.VBMP);;All files (*)",
        )
        anms, _ = QFileDialog.getOpenFileNames(
            self, "Pick animation files (.anm/.vanm) - optional",
            str(self._last_directory),
            "Animations (*.anm *.vanm *.ANM *.VANM);;All files (*)",
        )
        if not sklt and not base and not textures and not anms:
            return
        if not self._confirm_discard_geometry():
            return
        if sklt:
            self._last_directory = Path(sklt).parent
        try:
            family = load_manual_family(
                sklt or None, textures, anms, base or None, self._extra_roots,
                self._overrides, self._setbas,
            )
        except Exception as exc:
            QMessageBox.critical(
                self, "Load failed",
                f"No file was modified.\n\nUnexpected error:\n{exc}",
            )
            return
        self._set_family(family)
        primary_path = (
            base or sklt or (textures[0] if textures else None)
            or (anms[0] if anms else None)
        )
        if primary_path:
            self._set_document_title(primary_path)

    def open_setbas_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open SET.BAS as read-only resource provider",
            str(self._last_directory),
            "SET.BAS archives (SET.BAS *.bas *.BAS);;All files (*)",
        )
        if path:
            self.open_setbas(path)

    def open_setbas(self, path: str | Path) -> None:
        try:
            archive = read_setbas(path)
        except SetBasError as exc:
            QMessageBox.warning(
                self, "SET.BAS parse failed",
                f"No file was modified.\n\n{exc}",
            )
            self.statusBar().showMessage("SET.BAS parse failed")
            return
        if not self._confirm_discard_geometry():
            return
        self._setbas = archive
        self._set_document_title(archive.path)
        self._fill_setbas(archive)
        self._raise_setbas_tab()
        census = archive.census()
        summary = ", ".join(f"{count} {cls}" for cls, count in census.items())
        self.statusBar().showMessage(
            f"SET.BAS provider active: {archive.path} "
            f"({len(archive.resources)} resources: {summary})"
        )
        self._log(
            f"SET.BAS provider loaded: {archive.path} "
            f"({len(archive.resources)} resources)"
        )
        if self._family and self._family.base_path:
            # Re-resolve the current family with the archive as fallback.
            self.open_base(self._family.base_path, confirm_discard=False)
            # The archive is the file the user just opened, so keep its path
            # visible after the internal family refresh.
            self._set_document_title(archive.path)
        self._preview_first_setbas_skeleton(confirm_discard=False)

    def _raise_setbas_tab(self) -> None:
        """Bring the primary BAS panel to the front after loading it."""

        right_tabs = getattr(self, "_right_tabs", None)
        panel = getattr(self, "_setbas_panel", None)
        if right_tabs is not None and panel is not None:
            right_tabs.setCurrentWidget(panel)
            panel.setCurrentWidget(self._bas_panel)

    def _filter_setbas_tree(self, text: str) -> None:
        text = text.strip().lower()
        for i in range(self.setbas_tree.topLevelItemCount()):
            group = self.setbas_tree.topLevelItem(i)
            group_hit = text in group.text(0).lower()
            any_child = False
            for j in range(group.childCount()):
                child = group.child(j)
                hit = (not text) or group_hit \
                    or text in child.text(0).lower()
                child.setHidden(not hit)
                any_child = any_child or hit
            group.setHidden(bool(text) and not any_child and not group_hit)
            if text and any_child:
                group.setExpanded(True)

    def _fill_setbas(self, archive: SetBasArchive) -> None:
        self.setbas_tree.clear()
        census = archive.census()
        summary = ", ".join(f"{cls}: {count}" for cls, count in census.items())
        self.setbas_label.setText(
            f"Archive loaded as READ-ONLY resource provider:\n{archive.path}\n"
            f"{len(archive.resources)} EMRS resources ({summary})."
        )
        groups: dict[str, QTreeWidgetItem] = {}
        for class_id in list(census) + ["other/unknown"]:
            item = QTreeWidgetItem([class_id, "", "", ""])
            # Category rows are expand/collapse controls, not resources.  Keep
            # them out of the selection model so Qt does not draw the accent
            # focus stripe over the branch arrow.
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            groups[class_id] = item
            self.setbas_tree.addTopLevelItem(item)
        for resource in archive.resources:
            group = groups.get(resource.class_id,
                               groups.get("other/unknown"))
            row = QTreeWidgetItem([
                resource.resource_name,
                resource.display_payload + (" (unsupported class)"
                                            if not resource.decodable
                                            and not resource.error else ""),
                str(resource.payload_size),
                f"0x{resource.payload_offset:X}",
            ])
            if resource.error:
                row.setText(1, f"ERROR: {resource.error}")
            for column in range(4):
                row.setTextAlignment(
                    column,
                    Qt.AlignmentFlag.AlignLeft
                    | Qt.AlignmentFlag.AlignVCenter,
                )
            row.setData(0, Qt.ItemDataRole.UserRole, resource.index)
            group.addChild(row)
        for class_id, item in groups.items():
            item.setText(1, f"{item.childCount()} resources")
            for column in range(4):
                item.setTextAlignment(
                    column,
                    Qt.AlignmentFlag.AlignLeft
                    | Qt.AlignmentFlag.AlignVCenter,
                )
            if item.childCount() == 0:
                index = self.setbas_tree.indexOfTopLevelItem(item)
                self.setbas_tree.takeTopLevelItem(index)
        self.setbas_tree.collapseAll()
        self.setbas_preview_button.setEnabled(True)
        self.setbas_extract_button.setEnabled(True)
        self.setbas_extract_all_button.setEnabled(True)

    def _on_setbas_item_double_clicked(self, item, _column: int) -> None:
        index = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if index is None:
            # Class/group rows keep QTreeWidget's normal expand/collapse.
            return
        self.setbas_tree.setCurrentItem(item)
        self._preview_setbas_resource()

    def _preview_first_setbas_skeleton(
            self, *, confirm_discard: bool = True) -> None:
        """Load the first embedded SKLT, using archive mappings for textures."""

        if self._setbas is None:
            return
        resource = next(
            (entry for entry in self._setbas.resources
             if entry.class_id.lower() == "sklt.class" and not entry.error),
            None)
        if resource is None:
            return
        for group_index in range(self.setbas_tree.topLevelItemCount()):
            group = self.setbas_tree.topLevelItem(group_index)
            for child_index in range(group.childCount()):
                item = group.child(child_index)
                if item.data(0, Qt.ItemDataRole.UserRole) == resource.index:
                    group.setExpanded(True)
                    self.setbas_tree.setCurrentItem(item)
                    self.setbas_tree.scrollToItem(item)
                    self._preview_setbas_skeleton(
                        confirm_discard=confirm_discard)
                    return

    def _setbas_palette(self):
        """Palette for embedded VBMP previews and conversions."""

        if self._setbas is None:
            return None, ""
        from ilbm_parser import parse_pal_file
        from setbas_export import find_external_palette
        from texture_convert import (
            BUILTIN_AIR1TXT_CMAP,
            BUILTIN_PALETTE_SOURCE,
            cmap_to_palette,
        )

        palette_path = find_external_palette(Path(self._setbas.path))
        if palette_path is not None:
            palette = parse_pal_file(palette_path)
            if palette is not None:
                return palette, str(palette_path)
        return cmap_to_palette(BUILTIN_AIR1TXT_CMAP), BUILTIN_PALETTE_SOURCE

    def _preview_setbas_resource(self) -> None:
        """Preview the selected embedded SKLT or ILBM resource."""

        if self._setbas is None:
            return
        item = self.setbas_tree.currentItem()
        index = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if index is None:
            QMessageBox.information(
                self,
                "No resource selected",
                "Select a sklt.class or ilbm.class resource first.",
            )
            return
        resource = self._setbas.resources[index]
        class_id = resource.class_id.lower()
        if class_id == "sklt.class":
            self._preview_setbas_skeleton()
            return
        if class_id == "ilbm.class":
            self._preview_setbas_texture(resource)
            return
        QMessageBox.information(
            self,
            "Preview unavailable",
            f"{resource.resource_name} is {resource.class_id}.\n\n"
            "Preview supports only SKLT skeletons and ILBM/VBMP textures.",
        )

    def _show_image_preview(self, title: str, info_text: str,
                            image: QImage, tooltip: str = "") -> None:
        """Open the shared non-modal texture preview window."""

        dialog = QDialog(None)
        dialog.setWindowTitle(title)
        dialog.setWindowModality(Qt.WindowModality.NonModal)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        info = QLabel(info_text)
        if tooltip:
            info.setToolTip(tooltip)
        layout.addWidget(info)
        preview = QLabel()
        preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        screen = self.screen() or QApplication.primaryScreen()
        available = screen.availableGeometry() if screen else None
        preview_limit = 720
        if available is not None:
            preview_limit = max(
                240, min(720, available.width() - 180,
                         available.height() - 220))
        preview.setPixmap(_checker_thumbnail(image, preview_limit))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(preview)
        layout.addWidget(scroll, 1)
        close_button = QPushButton("Close")
        close_button.clicked.connect(dialog.close)
        layout.addWidget(close_button)
        width = max(360, preview.pixmap().width() + 48)
        height = max(300, preview.pixmap().height() + 116)
        if available is not None:
            width = min(width, max(360, int(available.width() * 0.82)))
            height = min(height, max(300, int(available.height() * 0.82)))
        dialog.resize(width, height)
        dialog.setModal(False)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._preview_windows.append(dialog)

        def forget_preview(*_args) -> None:
            if dialog in self._preview_windows:
                self._preview_windows.remove(dialog)

        dialog.destroyed.connect(forget_preview)
        dialog.show()
        if available is not None:
            frame = dialog.frameGeometry()
            frame.moveCenter(available.center())
            dialog.move(frame.topLeft())
        dialog.raise_()
        dialog.activateWindow()

    def _preview_setbas_texture(self, resource) -> None:
        try:
            from setbas_reader import decode_texture

            decoded = decode_texture(self._setbas, resource)

            palette = decoded.palette
            palette_source = "embedded CMAP" if palette is not None else ""
            if palette is None:
                palette, palette_source = self._setbas_palette()

            image = _qimage_from_ilbm(decoded, palette)
            if image is None or image.isNull():
                raise ValueError("the texture decoded to an empty image")
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Texture preview failed",
                f"No file was modified.\n\n{exc}",
            )
            return

        self._show_image_preview(
            f"Texture preview - {resource.resource_name}",
            f"{resource.resource_name}  |  {decoded.width} x "
            f"{decoded.height}  |  {resource.display_payload}",
            image, f"Palette: {palette_source}")

    def _preview_setbas_skeleton(
            self, *, confirm_discard: bool = True) -> None:
        if self._setbas is None:
            return
        item = self.setbas_tree.currentItem()
        index = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if index is None:
            QMessageBox.information(
                self, "No resource selected",
                "Select a sklt.class resource in the SET.BAS tree first.",
            )
            return
        resource = self._setbas.resources[index]
        if resource.class_id.lower() != "sklt.class":
            QMessageBox.information(
                self, "Not a skeleton",
                f"{resource.resource_name} is {resource.class_id}.\n\n"
                "Preview supports only SKLT skeletons and ILBM/VBMP "
                "textures.",
            )
            return
        if confirm_discard and not self._confirm_discard_geometry():
            return
        if self._preview_setbas_textured(resource):
            self._raise_setbas_tab()
            return
        try:
            family = load_manual_family(None, [], [], setbas=self._setbas)
            from asset_family import FamilyObject
            from base_parser import BaseObject
            from setbas_reader import decode_skeleton

            fake = BaseObject()
            fake.skeleton_name = resource.resource_name
            fam_obj = FamilyObject(base_object=fake)
            fam_obj.skeleton = decode_skeleton(self._setbas, resource)
            family.root_object = fam_obj
            family.warnings.append(
                f"Geometry-only preview of embedded {resource.resource_name}: "
                "no base.class object inside this SET.BAS maps this skeleton, "
                "so the archive holds no texture/UV data for it."
            )
        except Exception as exc:
            QMessageBox.warning(
                self, "Preview failed",
                f"No file was modified.\n\n{exc}",
            )
            return
        self._set_family(family)
        self._raise_setbas_tab()
        self.statusBar().showMessage(
            f"{resource.resource_name}: geometry-only (this archive has no "
            "base.class mapping for it, textures live only in loose .base "
            "files)", 10000)

    def _preview_setbas_textured(self, resource) -> bool:
        """Textured preview of an embedded skeleton via the archive's own
        base.class mapping.

        SET.BAS embeds full base.class objects (the same ones shown when the
        archive is opened as a family): when one of them references the
        selected skeleton, preview that object with its materials and
        textures instead of the bare geometry.  Returns False when nothing
        in the archive maps the skeleton."""

        archive_path = Path(self._setbas.path)

        def norm(name: str) -> str:
            return name.replace("\\", "/").lower()

        target = norm(resource.resource_name)
        if self._family is not None \
                and self._family.base_path == archive_path:
            family = self._family      # already browsing this archive
        else:
            try:
                family = load_asset_family(archive_path, self._extra_roots,
                                           {}, self._setbas)
            except Exception as exc:
                self._log(f"SET.BAS textured preview unavailable: {exc}")
                return False

        owner = next(
            (o.owner_path for o in family.all_objects()
             if o.base_object.skeleton_name
             and norm(o.base_object.skeleton_name) == target
             and o.skeleton is not None),
            None,
        )
        if owner is None:
            return False

        self._selected_owner = owner
        if family is not self._family:
            self._set_family(family)
        self._select_owner(owner)
        self._apply_selected_children_scope()
        self._frame_selected()
        self.statusBar().showMessage(
            f"Textured preview from the archive's own base.class mapping: "
            f"{resource.resource_name} [{owner}]", 8000)
        return True

    # -- extraction / conversion (BASet capability merge) -------------------------

    def _setbas_selected_resources(self) -> list:
        if self._setbas is None:
            return []
        resources = []
        seen: set[int] = set()
        items = self.setbas_tree.selectedItems()
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
            from setbas_reader import decode_texture
            from texture_convert import write_image_as_ilbm

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

        from setbas_export import extract_resource
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

        from setbas_export import flattened_resource_name

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
        meta_check = QCheckBox("Export BASE/KIDS scene metadata JSON")
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
        from setbas_export import extract_archive
        self.statusBar().showMessage("Extracting archive...")
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
        self.statusBar().showMessage(message, 10000)
        QMessageBox.information(self, "Extraction complete",
                                f"{message}\n\nOutput: {out_dir}")

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
            import base_kids_export
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
        QMessageBox.information(
            self, "Metadata export complete",
            f"{message}\n\nOutput: {metadata_dir}")

    def _preview_family_texture(self, name: str) -> None:
        if self._family is None:
            return
        image = self._family.textures.get(name)
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
        valid = [(name, self._family.textures.get(name)) for name in names]
        valid = [(name, image) for name, image in valid
                 if image is not None and image.has_body]
        if not valid:
            self.statusBar().showMessage(
                "Select one or more decoded textures first.")
            return
        from texture_convert import TextureConvertError, ilbm_image_to_png

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
        from texture_convert import TextureConvertError, convert_to_png
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
        from texture_convert import TextureConvertError, convert_vbmp_to_ilbm

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

        from texture_convert import TextureConvertError, convert_png_to_ilbm

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
        self.statusBar().showMessage(message, 10000)
        if failed or skipped:
            QMessageBox.warning(
                self, "Conversion completed with issues",
                message + "\n\nSee Diagnostics > Log.")
        else:
            QMessageBox.information(
                self, "ILBM conversion complete",
                message + f"\n\nOutput: {output_folder}")

    def select_root_dialog(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "Select an additional asset root directory",
            str(self._last_directory),
        )
        if directory:
            self._extra_roots.append(Path(directory))
            self.statusBar().showMessage(
                f"Added asset root: {directory} (Reload to apply)"
            )

    def reload_family(self) -> None:
        if self._family and self._family.base_path:
            # Pick up files added/removed on disk since the last scan.
            from asset_resolver import DirectoryIndex

            DirectoryIndex.clear_cache()
            self.open_base(self._family.base_path)

    def export_report(self, fmt: str) -> None:
        if self._family is None:
            QMessageBox.information(self, "No asset loaded",
                                    "Load an asset family first.")
            return
        base_name = (self._family.base_path.stem
                     if self._family.base_path else "asset_family")
        suggested = self._last_directory / f"{base_name}_report.{fmt}"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export technical report", str(suggested),
            "Markdown (*.md);;JSON (*.json);;All files (*)"
            if fmt == "md" else
            "JSON (*.json);;Markdown (*.md);;All files (*)",
        )
        if not path:
            return
        workbench = self._workbench_report()
        text = (family_to_markdown(self._family, None, workbench)
                if Path(path).suffix.lower() != ".json"
                else family_to_json(self._family, None, workbench))
        try:
            Path(path).write_text(text, encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "Export failed", str(exc))
            return
        self.statusBar().showMessage(f"Report written to {path}")

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

    def _selected_polygon_vertices(self) -> tuple[str, list[int]] | None:
        obj = self._workbench_obj
        poly_id = self._selected_poly
        if obj is None or obj.skeleton is None or poly_id is None \
                or not (0 <= poly_id < len(obj.skeleton.polygons)):
            return None
        selected = self._selected_polys or {poly_id}
        vertices = {
            vertex
            for selected_poly in selected
            if 0 <= selected_poly < len(obj.skeleton.polygons)
            for vertex in obj.skeleton.polygons[selected_poly]
        }
        return obj.owner_path, sorted(vertices)

    def _sync_editor_context(self) -> None:
        edit_button = getattr(self, "global_edit_button", None)
        if edit_button is None or not edit_button.isChecked():
            if self.viewport.is_edit_mode:
                self.viewport.exit_edit_mode()
            return

        panel = self._active_editor_panel()
        if panel is self._model_editor_panel:
            self.viewport.set_highlight_polys(set())
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

    def _on_right_tab_changed(self, index: int) -> None:
        self._sync_animation_controls()
        tabs = getattr(self, "_right_tabs", None)
        snapshot_panel = getattr(self, "_snapshot_panel", None)
        visuals_tabs = getattr(self, "_visuals_tabs", None)
        entering = (tabs is not None and visuals_tabs is not None
                    and snapshot_panel is not None
                    and tabs.currentWidget() is visuals_tabs
                    and visuals_tabs.currentWidget() is snapshot_panel)
        if entering and not self._snapshot_mode_active:
            self._snapshot_mode_active = True
            self.viewport.begin_snapshot_mode(self._snapshot_background())
            self._snapshot_zoom_percent = 100
            for widget in (self.snapshot_zoom_spin,
                           self.snapshot_zoom_slider):
                widget.blockSignals(True)
                widget.setValue(100)
                widget.blockSignals(False)
            self.snapshot_guides_button.setChecked(False)
            self.viewport.set_snapshot_guides_visible(False)
            self.mode_combo.blockSignals(True)
            self.mode_combo.setCurrentIndex(VIEW_MODES.index("textured"))
            self.mode_combo.blockSignals(False)
            self.mode_combo.setEnabled(False)
            self.global_edit_button.setEnabled(False)
            self.toolbar_view_preset_combo.setEnabled(False)
            for action in (self.sen_check, self.wire_check, self.axes_check,
                           self.grid_check, self.overlay_check,
                           self.mapping_diag_check):
                action.setEnabled(False)
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
            self.global_edit_button.setEnabled(True)
            self.toolbar_view_preset_combo.setEnabled(True)
            for action in (self.sen_check, self.wire_check, self.axes_check,
                           self.grid_check, self.overlay_check,
                           self.mapping_diag_check):
                action.setEnabled(True)
        self._sync_editor_context()
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

    def _on_snapshot_guides_toggled(self, visible: bool) -> None:
        self.snapshot_guides_button.setText(
            "Hide Guides and Overlays" if visible
            else "Show Guides and Overlays")
        self.viewport.set_snapshot_guides_visible(visible)

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

    def _on_toolbar_view_preset_changed(self, preset: str) -> None:
        if self._snapshot_mode_active:
            return
        self.viewport.apply_view_preset(
            preset,
            QSize(max(1, self.viewport.width()),
                  max(1, self.viewport.height())))
        self._notify(f"View preset changed to {preset}.", 3500)

    def _on_manual_camera_changed(self) -> None:
        """Mark the active preset as Current View after user navigation."""

        combo = (self.snapshot_view_combo if self._snapshot_mode_active
                 else self.toolbar_view_preset_combo)
        if combo.currentText() == "Current View":
            return
        combo.blockSignals(True)
        combo.setCurrentText("Current View")
        combo.blockSignals(False)

    def _snapshot_background(self) -> QColor | None:
        return (QColor(self._snapshot_custom_color)
                if self._snapshot_custom_color is not None else None)

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
        self.viewport.set_snapshot_background(color)

    def _clear_snapshot_color(self) -> None:
        self._snapshot_custom_color = None
        self._update_snapshot_color_button()
        self.viewport.set_snapshot_background(None)

    def _update_snapshot_frame_text(self, _text: str = "") -> None:
        if not hasattr(self, "snapshot_frame_label"):
            return
        has_animation = self.viewport.has_animation
        text = self.viewport.current_frame_text() if has_animation else "No animation"
        self.snapshot_frame_label.setText(text)
        self.snapshot_next_frame_button.setEnabled(has_animation)
        self.snapshot_reset_frame_button.setEnabled(has_animation)

    def _on_snapshot_format_changed(self, _index: int) -> None:
        self.snapshot_quality_spin.setEnabled(
            self.snapshot_format_combo.currentData() != "png")

    def _snapshot_name(self) -> str:
        obj = self._owner_to_obj.get(self._selected_owner)
        if obj is not None:
            name = getattr(obj, "display_name", "")
        elif self._family is not None and self._family.base_path:
            name = self._family.base_path.stem
        elif self._family is not None and self._family.setbas_path:
            name = self._family.setbas_path.stem
        else:
            name = "Snapshot"
        name = Path(str(name).replace("\\", "/")).stem
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" ._")
        return name or "Snapshot"

    def _export_snapshot(self) -> None:
        if not self.viewport.has_model:
            QMessageBox.information(self, "No model loaded",
                                    "Load a model before exporting an image.")
            return
        selected_format = self.snapshot_format_combo.currentData()
        preset = self.snapshot_view_combo.currentText().replace(" ", "")
        suggested = self._last_directory / (
            f"{self._snapshot_name()}_{preset}.{selected_format}")
        labels = {"png": "PNG (*.png)", "jpg": "JPEG (*.jpg *.jpeg)",
                  "webp": "WebP (*.webp)"}
        formats = [selected_format] + sorted(
            self._snapshot_formats - {selected_format})
        filters = ";;".join(labels[fmt] for fmt in formats)
        path_text, _selected_filter = QFileDialog.getSaveFileName(
            self, "Export Snapshot", str(suggested), filters,
            options=QFileDialog.Option.DontConfirmOverwrite)
        if not path_text:
            return
        path = Path(path_text)
        if not path.suffix:
            dialog_format = next(
                (fmt for fmt, label in labels.items()
                 if label == _selected_filter), selected_format)
            path = path.with_suffix(f".{dialog_format}")
        suffix = path.suffix.lower().lstrip(".")
        image_format = {"png": "png", "jpg": "jpg", "jpeg": "jpg",
                        "webp": "webp"}.get(suffix)
        if image_format not in self._snapshot_formats:
            QMessageBox.warning(self, "Unsupported format",
                                f"The format '.{suffix}' is not supported by "
                                "this Qt runtime.")
            return
        if path.exists():
            answer = QMessageBox.question(
                self, "Replace existing file?",
                f"The file already exists:\n{path}\n\nReplace it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        background = self._snapshot_background()
        if image_format == "jpg" and background is None:
            background = QColor(96, 96, 96)
        image = self.viewport.render_snapshot(
            self._snapshot_output_size(), background,
            include_guides=self.snapshot_guides_button.isChecked())
        if image.isNull():
            QMessageBox.warning(self, "Export failed",
                                "The snapshot image could not be rendered.")
            return
        writer_format = "jpeg" if image_format == "jpg" else image_format
        writer = QImageWriter(str(path), writer_format.encode("ascii"))
        if image_format != "png":
            writer.setQuality(self.snapshot_quality_spin.value())
        if not writer.write(image):
            QMessageBox.warning(
                self, "Export failed",
                writer.errorString() or f"Could not write {path}.")
            return
        self._last_directory = path.parent
        self.statusBar().showMessage(
            f"Snapshot written to {path} ({image.width()} x {image.height()})")

    # -- texture resolution (session-only overrides) -----------------------------

    def _selected_resolve_target(self) -> tuple[str, str | None] | None:
        """Returns (logical_name, candidate_path or None) for the selection."""

        item = self.resolve_tree.currentItem()
        if item is None:
            return None
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data is None:
            return None
        return data

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
                self._fill_resolve(self._family)
                return
        self.statusBar().showMessage(
            "Keep for session applies to a trial-loaded dependency."
        )

    def _skip_dependency(self) -> None:
        target = self._selected_resolve_target()
        if target is None:
            return
        logical_name, _candidate = target
        self._skipped_names.add(logical_name)
        self._fill_resolve(self._family)
        self._apply_diagnostics_filter()
        self.statusBar().showMessage(f"Skipped for this session: {logical_name}")

    def _apply_diagnostics_filter(self) -> None:
        if self._family is None:
            return
        filtered = [d for d in self._family.textured_diagnostics
                    if not any(name in d for name in self._skipped_names)]
        self.viewport.set_diagnostics(filtered)

    # -- object selection / Large Family Mode --------------------------------------

    LARGE_OBJECT_THRESHOLD = 50
    LARGE_FACE_THRESHOLD = 20000

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

    def _select_owner(self, owner: str | None,
                      from_viewport: bool = False) -> None:
        edit_button = getattr(self, "global_edit_button", None)
        keep_global_edit = bool(edit_button and edit_button.isChecked())
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
        self._update_banner()
        self._refresh_fx_elements()
        self._focus_assets_for_owner(owner, switch_tabs=False)
        self._sync_geometry_save_controls()
        if hasattr(self, "_editor_tabs"):
            if keep_global_edit and not self.global_edit_button.isChecked():
                self.global_edit_button.blockSignals(True)
                self.global_edit_button.setChecked(True)
                self.global_edit_button.blockSignals(False)
            self._sync_editor_context()

    def _on_object_picked(self, owner: str) -> None:
        self._select_owner(owner, from_viewport=True)

    def _frame_selected(self) -> None:
        if self._selected_owner:
            self.viewport.frame_owner(self._selected_owner)
        else:
            self.viewport.frame_all()

    def _apply_selected_children_scope(self) -> None:
        if self._family is None:
            return
        owner = self._selected_owner or self._default_owner(self._family)
        visible = self._family_descendants(self._family, owner)
        self.viewport.set_visible_owners(visible)
        self._sync_animation_controls()
        self._update_banner()

    def _update_banner(self) -> None:
        if self._family is None:
            return
        status, details = self._completeness(self._family)
        visible = self.viewport.visible_owners()
        scope = ("-" if visible is None
                 else f"{len(visible)}/{len(self.viewport.owners())}")
        obj = (self._owner_to_obj.get(self._selected_owner)
               if self._selected_owner else None)
        selected = obj.display_name if obj else "-"
        large = (" | large family" if self._large_mode else "")
        n_dirty = len(self._geom_dirty) + len(self._uv_original)
        dirty = (f" | <b>UNSAVED EDITS: {n_dirty}</b>" if n_dirty else "")
        preview = (f" | <b>TEXTURE PREVIEW: {len(self._texture_original)}</b>"
                   if self._texture_original else "")
        summary = (f"<b>{status}</b> | selected: {selected} | "
                   f"selected + children: {scope}{large}{dirty}{preview}")
        self.completeness_label.setText(summary)
        self.completeness_label.setToolTip("\n".join(details))

    # -- log / profile / candidate tools ------------------------------------------

    def _log(self, text: str) -> None:
        self.log_list.addItem(text)
        self.log_list.scrollToBottom()

    def _toggle_auto_apply(self, enabled: bool) -> None:
        self._profile.auto_apply = enabled
        error = self._profile.save()
        if error:
            self._log(f"profile: {error}")

    def _dep_for_name(self, name: str):
        if self._family is None:
            return None
        for dep in self._family.dependencies:
            if dep.raw_ref == name:
                return dep
        return None

    def _save_choice(self) -> None:
        target = self._selected_resolve_target()
        if target is None:
            return
        name, candidate = target
        bare = name.replace("\\", "/").split("/")[-1]
        chosen = (candidate or self._overrides.get(name)
                  or self._overrides.get(bare))
        if not chosen:
            ref = (self._family.texture_refs.get(name)
                   or self._family.animation_refs.get(name)
                   if self._family else None)
            chosen = str(ref.path) if ref and ref.path else None
        if not chosen:
            self.statusBar().showMessage(
                "Select a candidate (or trial-load one) before saving."
            )
            return
        dep = self._dep_for_name(name)
        self._profile.remember(
            str(self._family.base_path), dep.owner_node if dep else "root",
            dep.kind if dep else "texture", name, chosen,
            source=dep.source if dep else "",
        )
        error = self._profile.save()
        self._log(f"profile: saved choice {name} -> {chosen}"
                  + (f" ({error})" if error else ""))
        self._fill_resolve(self._family)

    def _forget_choice(self) -> None:
        target = self._selected_resolve_target()
        if target is None or self._family is None:
            return
        name, _candidate = target
        dep = self._dep_for_name(name)
        removed = self._profile.forget(
            str(self._family.base_path), dep.owner_node if dep else "root",
            dep.kind if dep else "texture", name,
        )
        error = self._profile.save()
        self._log(f"profile: {'forgot' if removed else 'no saved choice for'} "
                  f"{name}" + (f" ({error})" if error else ""))
        self._fill_resolve(self._family)

    def _saved_choice_for(self, name: str):
        if self._family is None or self._family.base_path is None:
            return None
        dep = self._dep_for_name(name)
        return self._profile.lookup(
            str(self._family.base_path), dep.owner_node if dep else "root",
            dep.kind if dep else "texture", name,
        )

    def _apply_saved_choice(self) -> None:
        target = self._selected_resolve_target()
        if target is None:
            return
        name, _candidate = target
        saved = self._saved_choice_for(name)
        if saved is None:
            self.statusBar().showMessage(f"No saved choice for {name}.")
            return
        if saved.stale:
            self._log(f"profile: saved choice for {name} is STALE "
                      f"({saved.chosen_path} no longer exists).")
            return
        self._profile.touch(saved)
        self._profile.save()
        bare = name.replace("\\", "/").split("/")[-1]
        self._apply_override(bare, saved.chosen_path, trial=False)
        self._log(f"profile: applied saved choice {name} -> "
                  f"{saved.chosen_path}")

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

    def _reveal_in_folder(self) -> None:
        target = self._selected_resolve_target()
        if target is None:
            return
        name, candidate = target
        path = candidate
        if not path:
            ref = (self._family.texture_refs.get(name)
                   or self._family.animation_refs.get(name)
                   if self._family else None)
            path = str(ref.path) if ref and ref.path else None
        if not path or path.startswith("SET.BAS:"):
            self.statusBar().showMessage("No on-disk file to reveal.")
            return
        import subprocess

        subprocess.Popen(["explorer", "/select,", str(Path(path))])

    def _compare_candidate(self) -> None:
        """Diff the selected candidate against the embedded SET.BAS copy
        and/or the currently loaded texture, logging the verdict."""

        target = self._selected_resolve_target()
        if target is None or self._family is None:
            self.statusBar().showMessage(
                "Select a candidate row in the Dependencies tree first."
            )
            return
        name, candidate = target
        if not candidate or candidate.startswith("SET.BAS:"):
            self.statusBar().showMessage(
                "Select a loose candidate row to compare."
            )
            return

        from asset_diff import DiffEntry, diff_textures
        from ilbm_parser import parse_ilbm_file
        from setbas_reader import decode_texture

        try:
            candidate_img = parse_ilbm_file(candidate)
        except Exception as exc:
            self._log(f"compare {name}: candidate failed to decode: {exc}")
            return

        palette = self._family.external_palette
        compared = False

        if self._setbas is not None:
            matches = self._setbas.find(name, "ilbm.class")
            if matches:
                try:
                    embedded = decode_texture(self._setbas, matches[0])
                    entry = DiffEntry(name=name, kind="texture")
                    diff_textures(entry, candidate_img, embedded, palette,
                                  rgb=True, keep_rgba=False)
                    self._log(
                        f"compare {Path(candidate).parent.name}/"
                        f"{Path(candidate).name} vs SET.BAS embedded: "
                        f"{entry.visual or entry.status} - {entry.summary} "
                        f"{entry.metrics or ''}"
                    )
                    compared = True
                except Exception as exc:
                    self._log(f"compare {name} vs SET.BAS failed: {exc}")

        loaded = self._family.textures.get(name)
        if loaded is not None and loaded.source_name != Path(candidate).name:
            try:
                entry = DiffEntry(name=name, kind="texture")
                diff_textures(entry, candidate_img, loaded, palette,
                              rgb=True, keep_rgba=False)
                self._log(
                    f"compare {Path(candidate).parent.name}/"
                    f"{Path(candidate).name} vs currently loaded: "
                    f"{entry.visual or entry.status} - {entry.summary}"
                )
                compared = True
            except Exception as exc:
                self._log(f"compare {name} vs loaded failed: {exc}")

        if not compared:
            self._log(
                f"compare {name}: nothing to compare against (open a SET.BAS "
                "provider or trial-load another candidate first)."
            )
        self._show_diagnostics(2)

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
                "Select a candidate path in the Resolve tree first.",
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
                "Select the resource to bind in the Resolve tree first.",
            )
            return
        logical_name, _candidate = target
        path, _ = QFileDialog.getOpenFileName(
            self, f"Assign a file to {logical_name}",
            str(self._last_directory),
            "Textures/animations (*.ilbm *.ilb *.lbm *.iff *.anm *.vanm "
            "*.ILBM *.ILB *.ANM *.VANM);;All files (*)",
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

    def _fill_resolve(self, family: AssetFamily) -> None:
        self.resolve_tree.clear()
        status_key = {
            "auto_loaded": "found", "resolved": "found",
            "trial_loaded": "manual", "kept_for_session": "manual",
            "ambiguous": "ambiguous", "missing": "missing",
            "failed_load": "decode failed",
            "unsupported_loader": "ambiguous", "skipped": "missing",
        }
        for dep in family.dependencies:
            status = self._effective_status(dep.raw_ref, dep.status)
            owner = f" [{dep.owner_node}]" if dep.owner_node else ""
            top = QTreeWidgetItem([
                f"{dep.kind}: {dep.raw_ref}{owner}",
                status,
                dep.display_path() if dep.resolved_path else
                (dep.error or dep.source or "-"),
            ])
            top.setIcon(0, _status_icon(status_key.get(status, status)))
            top.setData(0, Qt.ItemDataRole.UserRole, (dep.raw_ref, None))
            self.resolve_tree.addTopLevelItem(top)

            ref = (family.texture_refs.get(dep.raw_ref)
                   or family.animation_refs.get(dep.raw_ref))
            for candidate in dep.candidates:
                child = QTreeWidgetItem(
                    ["candidate (loose)",
                     "current" if dep.resolved_path == candidate else "",
                     str(candidate)]
                )
                child.setData(0, Qt.ItemDataRole.UserRole,
                              (dep.raw_ref, str(candidate)))
                top.addChild(child)
            if ref is not None:
                for embedded in ref.embedded_candidates:
                    child = QTreeWidgetItem(
                        ["candidate (SET.BAS)",
                         "current" if ref.status in ("setbas",
                                                     "manual (SET.BAS)")
                         else "",
                         embedded]
                    )
                    child.setData(0, Qt.ItemDataRole.UserRole,
                                  (dep.raw_ref, embedded))
                    top.addChild(child)
            bare = dep.raw_ref.replace("\\", "/").split("/")[-1]
            override = (family.overrides.get(dep.raw_ref)
                        or family.overrides.get(bare))
            if override:
                note = QTreeWidgetItem(
                    ["session override", "active", override]
                )
                note.setData(0, Qt.ItemDataRole.UserRole, (dep.raw_ref, None))
                top.addChild(note)
            saved = self._saved_choice_for(dep.raw_ref)
            if saved is not None:
                saved_row = QTreeWidgetItem([
                    "saved choice (profile)",
                    "STALE" if saved.stale else "available",
                    saved.chosen_path,
                ])
                saved_row.setData(0, Qt.ItemDataRole.UserRole,
                                  (dep.raw_ref,
                                   None if saved.stale else saved.chosen_path))
                if not saved.stale:
                    saved_row.setForeground(0, QColor(110, 170, 255))
                top.addChild(saved_row)
        self.resolve_tree.expandAll()
        for column in range(3):
            self.resolve_tree.resizeColumnToContents(column)

    def _workbench_report(self) -> dict | None:
        if self._mapping_index is None:
            return None
        wb: dict = {
            "unmapped_polygons": self._mapping_index.unmapped,
            "duplicate_mapped": self._mapping_index.duplicates,
            "invalid_polyids": self._mapping_index.invalid,
        }
        if self._selected_poly is not None:
            wb["selected_polygon"] = self._selected_poly
            wb["selected_status"] = self._mapping_index.status(
                self._selected_poly)
            for ref in self._mapping_index.refs.get(self._selected_poly, []):
                block = ref.block
                wb["selected_material_block"] = ref.block_index
                wb["selected_texture"] = (block.texture.name
                                          if block.texture else None)
                uvs = (block.olpl[ref.atts_index]
                       if ref.atts_index < len(block.olpl) else [])
                wb["selected_uvs"] = [list(uv) for uv in uvs]
        if self._repair_plan is not None:
            wb["repair_preview"] = self._repair_plan.describe()
        if self._pending_repairs:
            wb["pending_repairs"] = [p.describe()[0]
                                     for p in self._pending_repairs]
        if self._saved_repair_path:
            wb["saved_repair_path"] = self._saved_repair_path
        return wb

    # -- FX Elements ---------------------------------------------------------------

    def _selected_fx_element(self) -> FxElement | None:
        value = self.fx_combo.currentData()
        return value if isinstance(value, FxElement) else None

    def _refresh_fx_elements(self) -> None:
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
            status = ("Bilateral | Editable" if element.bilateral else
                      ("Editable" if element.editable else "Shared vertices"))
            label = (
                f"{element.fx_name} - polyIDs "
                f"{','.join(str(poly_id) for poly_id in element.poly_ids)} "
                f"({status})"
            )
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
                    "Editing is disabled because it would deform other geometry."
                )
            details.extend(element.warnings)
            self.fx_combo.setItemData(
                index, "\n".join(details), Qt.ItemDataRole.ToolTipRole)
            if element.identity == previous_identity:
                selected_index = index
        self.fx_combo.setCurrentIndex(selected_index)
        self.fx_combo.setEnabled(bool(self._fx_elements))
        self.fx_combo.blockSignals(False)

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
            return
        self._select_highlight_fx()
        if element.editable:
            if not self.global_edit_button.isChecked():
                self.global_edit_button.setChecked(True)
            self._edit_selected_fx()

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
        self._selected_poly = primary_poly
        self._selected_polys = set(element.poly_ids)
        self.viewport.set_selected_polygon(primary_poly)
        self.viewport.set_highlight_polys(self._selected_polys)
        self._fill_polygon_inspector(primary_poly)
        self._update_repair_buttons()
        self.statusBar().showMessage(
            f"Selected {element.fx_name} polyIDs "
            f"{','.join(str(poly_id) for poly_id in element.poly_ids)} "
            f"(material blocks "
            f"{','.join(str(index) for index in element.block_indices)})"
        )

    def _edit_selected_fx(self) -> None:
        element = self._selected_fx_element()
        if element is None:
            return
        if not element.editable:
            QMessageBox.information(
                self, "Shared vertices",
                "This FX element has unsafe or ambiguous POO2 sharing.\n\n"
                "Editing is disabled because it could deform other geometry."
            )
            return
        self._select_highlight_fx()
        self._remember_geometry_original(element.owner_path)
        if self.viewport.enter_edit_mode_with_vertices(
                element.owner_path, element.vertex_indices):
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

    def _goto_poly_dialog(self) -> None:
        if self._mapping_index is None:
            return
        poly_id, ok = QInputDialog.getInt(
            self, "Go to polyID",
            f"Polygon ID (0..{self._mapping_index.poly_count - 1}):",
            0, 0, max(0, self._mapping_index.poly_count - 1),
        )
        if ok:
            self.mapping_diag_check.setChecked(True)
            self.viewport.set_selected_polygon(poly_id)
            self._on_polygon_picked(poly_id)

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

    def _on_polygon_picked(self, poly_id: int,
                           additive: bool = False) -> None:
        if additive:
            if poly_id in self._selected_polys:
                self._selected_polys.remove(poly_id)
            else:
                self._selected_polys.add(poly_id)
        else:
            self._selected_polys = {poly_id}
        self._selected_poly = (
            poly_id if poly_id in self._selected_polys else
            (next(iter(self._selected_polys), None)))
        self.viewport.set_selected_polygon(self._selected_poly)
        self.viewport.set_highlight_polys(self._selected_polys)
        self._repair_plan = None
        self.repair_preview.clear()
        self._fill_polygon_inspector(self._selected_poly)
        self._update_repair_buttons()
        if self._mapping_index is not None:
            status = (self._mapping_index.status(self._selected_poly)
                      if self._selected_poly is not None else "none")
            self.statusBar().showMessage(
                f"Selected {len(self._selected_polys)} polygon(s); "
                f"current #{self._selected_poly}: {status}"
            )
        element = next(
            (candidate for candidate in self._fx_elements
             if self._selected_poly in candidate.poly_ids),
            None,
        )
        self.fx_combo.blockSignals(True)
        self.fx_combo.setCurrentIndex(
            self._fx_combo_index(element.identity) if element else 0)
        self.fx_combo.blockSignals(False)
        if self._active_editor_panel() is not self._model_editor_panel \
                or not self.global_edit_button.isChecked() \
                or not self.viewport.is_edit_mode:
            return
        target = self._selected_polygon_vertices()
        if target is not None:
            owner, vertices = target
            self.viewport.enter_edit_mode_with_vertices(
                owner, vertices, pick_polygons=True)
            self.viewport.configure_edit_interaction(
                selected_only=False, pick_polygons=True)

    def _fill_polygon_inspector(self, poly_id: int | None) -> None:
        self._update_uv_editor(poly_id)
        self.poly_uv_label.clear()
        self.poly_uv_label.setText("Select a polygon in the viewport.")
        self.model_texture_label.setText("Current texture: -")
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

        polygon = skeleton.polygons[poly_id]
        lines = [
            f"polyID {poly_id} | {len(polygon)} vertices | "
            f"POO2 {list(polygon)}"]

        status = self._mapping_index.status(poly_id)
        lines.append(f"mapping: {status.upper()}")
        if status == "unmapped":
            lines.append("No ATTS material: polygon is invisible in-game.")
            self._set_polygon_object_info(lines)
            return

        texture_names = []
        texture_refs = []
        override = self.viewport.polygon_texture_override(
            obj.owner_path, poly_id)
        for ref in self._mapping_index.refs.get(poly_id, []):
            block = ref.block
            tex = override or (block.texture.name if block.texture else "-")
            texture_names.append(tex)
            if block.texture is not None:
                texture_refs.append(ref)
            lines.append(
                f"block #{ref.block_index} | texture: {tex} | "
                f"ATTS #{ref.atts_index}")
            uvs = (block.olpl[ref.atts_index]
                   if ref.atts_index < len(block.olpl) else [])
            self._draw_uv_overlay(tex, uvs, poly_id)
        self.model_texture_label.setText(
            "Current texture: " + ", ".join(dict.fromkeys(texture_names)))
        replaceable = (
            len(texture_refs) == 1
            and texture_refs[0].block.texture.kind == "ilbm"
        )
        self.load_texture_button.setEnabled(replaceable)
        self._set_polygon_object_info(lines)

    def _texture_qimage(self, tex_name: str) -> QImage | None:
        family = self._family
        if family is None:
            return None
        img = family.textures.get(tex_name)
        if img is None:
            anm = family.animations.get(tex_name)
            if anm and anm.bitmap_names:
                img = family.textures.get(anm.bitmap_names[0])
        if img is None or not img.has_body:
            return None
        rgba = img.to_rgba_bytes(
            family.external_palette if not img.palette else None, "chroma"
        )
        return QImage(rgba, img.width, img.height, img.width * 4,
                      QImage.Format.Format_RGBA8888).copy()

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

    # -- Model and Texture Editor -------------------------------------------------

    @staticmethod
    def _nudge_hold_multiplier(button: QPushButton | None) -> float:
        if button is None:
            return 1.0
        started = getattr(button, "_nudge_started_at", time.monotonic())
        held = max(0.0, time.monotonic() - started)
        if held < 0.6:
            return 1.0
        return float(min(6, 2 + int((held - 0.6) / 0.45)))

    def _nudge_selected_vertices(self, dx: int, dy: int,
                                  button: QPushButton | None = None) -> None:
        multiplier = self._nudge_hold_multiplier(button)
        if self.viewport.nudge_edit_selection(
                dx * 4.0 * multiplier, dy * 4.0 * multiplier) \
                and (button is None or not button.isDown()):
            self.viewport.setFocus()

    def _copy_selected_vertex_positions(self) -> None:
        session = self.viewport.edit_session
        selected = self.viewport.selected_edit_points()
        if session is None or not selected:
            self._notify(
                "Copy: enable Edit Mode and select one or more vertices.")
            return
        count = len(selected)
        pivot = tuple(
            sum(point[axis] for _index, point in selected) / count
            for axis in range(3))
        offsets = [tuple(point[axis] - pivot[axis] for axis in range(3))
                   for _index, point in selected]
        self._copied_vertex_shape = {
            "owner": self.viewport.edit_owner,
            "indices": tuple(index for index, _point in selected),
            "offsets": offsets,
        }
        text = "\n".join(
            f"{index}: {point[0]:.6g}, {point[1]:.6g}, {point[2]:.6g}"
            for index, point in selected)
        QApplication.clipboard().setText(text)
        self._sync_edit_action_states()
        self._notify(
            f"Copied {count} selected vertex position(s) successfully.",
            6000)

    def _paste_selected_vertex_positions(self) -> None:
        copied = self._copied_vertex_shape
        session = self.viewport.edit_session
        if copied is None:
            self._notify("Paste: no copied vertex positions are available.")
            return
        if session is None or not session.selection:
            self._notify(
                "Paste: enable Edit Mode and select destination vertices.")
            return
        if copied["owner"] != self.viewport.edit_owner:
            self._notify(
                "Paste: vertex positions can only be pasted within the "
                "model they were copied from.", 7000)
            return
        offsets = copied["offsets"]
        indices = sorted(session.selection)
        if len(indices) != len(offsets):
            self._notify(
                "Paste: copied and selected vertex counts do not match.",
                7000)
            return
        points = [session.model.points[index] for index in indices]
        count = len(points)
        pivot = tuple(sum(point[axis] for point in points) / count
                      for axis in range(3))
        replacements = [
            tuple(pivot[axis] + offset[axis] for axis in range(3))
            for offset in offsets
        ]
        if self.viewport.replace_selected_edit_points(replacements):
            self.viewport.setFocus()

    def _scale_selected_geometry(self) -> None:
        if self._family is None:
            self._notify("Scale: load a model first.")
            return
        if self._live_scale_dialog is not None \
                and self._live_scale_dialog.isVisible():
            self._live_scale_dialog.raise_()
            self._live_scale_dialog.activateWindow()
            return
        if not self.viewport.is_edit_mode:
            self.global_edit_button.setChecked(True)
        if not self.viewport.is_edit_mode:
            self._notify("Scale: the current model cannot enter Edit Mode.")
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

    def _create_viewport_context_menu(self, position=None) -> QMenu:
        menu = QMenu(self.viewport)
        session = self.viewport.edit_session
        active = session is not None
        has_selection = bool(session and session.selection)
        mode_text = "View Mode" if active else "Edit Mode"
        menu.addAction(mode_text, self._toggle_global_edit_mode)
        menu.addSeparator()
        undo = menu.addAction("Undo", self._undo_edit)
        undo.setEnabled(bool(self._edit_undo_stack))
        redo = menu.addAction("Redo", self._redo_edit)
        redo.setEnabled(bool(self._edit_redo_stack))
        reset = menu.addAction("Reset Model...", self._reset_model)
        reset.setEnabled(getattr(self, "edit_reset_action", None) is not None
                         and self.edit_reset_action.isEnabled())
        menu.addSeparator()
        select_all = menu.addAction(
            "Select All Vertices", self.viewport.select_all_edit_vertices)
        select_all.setEnabled(active)
        select_none = menu.addAction(
            "Deselect All Vertices", self.viewport.select_no_edit_vertices)
        select_none.setEnabled(active)
        deselect = menu.addAction(
            "Deselect",
            lambda: self.viewport.deselect_at(position)
            if position is not None else
            self.viewport.select_no_edit_vertices())
        deselect.setEnabled(self._family is not None)
        menu.addSeparator()
        copy = menu.addAction(
            "Copy Selected Vertex Positions",
            self._copy_selected_vertex_positions)
        copy.setEnabled(has_selection)
        paste = menu.addAction(
            "Paste Vertex Positions",
            self._paste_selected_vertex_positions)
        paste.setEnabled(
            has_selection and self._copied_vertex_shape is not None)
        scale = menu.addAction(
            "Scale Selection / Model...", self._scale_selected_geometry)
        scale.setEnabled(self._family is not None)
        menu.addSeparator()
        frame = menu.addAction("Frame Selected Model", self._frame_selected)
        frame.setEnabled(self._selected_owner is not None)
        return menu

    def _show_viewport_context_menu(self, position) -> None:
        menu = self._create_viewport_context_menu(position)
        menu.exec(self.viewport.mapToGlobal(position))

    def _remember_geometry_original(self, owner: str | None) -> None:
        if owner is None or owner in self._geom_original:
            return
        fam_obj = self._owner_to_obj.get(owner)
        model = getattr(fam_obj, "skeleton", None)
        if model is not None:
            self._geom_original[owner] = list(model.points)

    def _toggle_global_edit_mode(self, _checked=False) -> None:
        self.global_edit_button.setChecked(
            not self.global_edit_button.isChecked())

    def _show_model_editor(self) -> None:
        if hasattr(self, "_right_tabs") and hasattr(self, "_editor_tabs"):
            self._right_tabs.setCurrentWidget(self._editor_tabs)
            self._editor_tabs.setCurrentWidget(self._model_editor_panel)

    def _set_global_edit_mode(self, enabled: bool) -> None:
        if enabled:
            self._show_model_editor()
            self._remember_geometry_original(self._selected_owner)
            self._sync_editor_context()
            if not self.viewport.is_edit_mode:
                self.global_edit_button.blockSignals(True)
                self.global_edit_button.setChecked(False)
                self.global_edit_button.blockSignals(False)
                return
            self.viewport.setFocus()
        else:
            self._cancel_live_scale()
            if self.viewport.is_edit_mode:
                self.viewport.exit_edit_mode()

    def _on_edit_mode_toggled(self, active: bool) -> None:
        button = getattr(self, "global_edit_button", None)
        if button is not None and button.isChecked() != active:
            button.blockSignals(True)
            button.setChecked(active)
            button.blockSignals(False)
        if active:
            self._show_model_editor()
        for nudge in getattr(self, "nudge_buttons", []):
            nudge.setEnabled(active)
        self._sync_edit_action_states()
        self._notify("Edit Mode enabled." if active else
                     "View Mode enabled.", 4000)

    def _sync_edit_action_states(self) -> None:
        session = self.viewport.edit_session
        active = session is not None
        has_selection = bool(session and session.selection)
        if hasattr(self, "edit_toggle_action"):
            self.edit_toggle_action.setText(
                "View Mode" if active else "Edit Mode")
        for name in ("edit_select_all_action", "edit_select_none_action"):
            action = getattr(self, name, None)
            if action is not None:
                action.setEnabled(active)
        if hasattr(self, "edit_undo_action"):
            self.edit_undo_action.setEnabled(bool(self._edit_undo_stack))
        if hasattr(self, "edit_redo_action"):
            self.edit_redo_action.setEnabled(bool(self._edit_redo_stack))
        if hasattr(self, "global_undo_button"):
            self.global_undo_button.setEnabled(bool(self._edit_undo_stack))
        if hasattr(self, "global_redo_button"):
            self.global_redo_button.setEnabled(bool(self._edit_redo_stack))
        if hasattr(self, "edit_copy_action"):
            self.edit_copy_action.setEnabled(has_selection)
        if hasattr(self, "edit_paste_action"):
            self.edit_paste_action.setEnabled(
                has_selection and self._copied_vertex_shape is not None)
        if hasattr(self, "edit_scale_action"):
            self.edit_scale_action.setEnabled(self._family is not None)
        if hasattr(self, "edit_frame_action"):
            self.edit_frame_action.setEnabled(self._selected_owner is not None)

    def _sync_geometry_save_controls(self) -> None:
        owner_obj = (self._owner_to_obj.get(self._selected_owner)
                     if self._selected_owner else None)
        save_enabled = bool(
            owner_obj is not None
            and getattr(owner_obj, "skeleton", None) is not None
            and getattr(getattr(self._family, "base_asset", None),
                        "tree", None) is not None)
        overwrite_enabled = bool(
            save_enabled and self._selected_owner in self._bundle_targets)
        self.model_save_button.setEnabled(overwrite_enabled)
        self.model_save_as_button.setEnabled(save_enabled)
        self.overwrite_action.setEnabled(overwrite_enabled)
        self.save_as_action.setEnabled(save_enabled)
        owner = self._selected_owner
        can_reset = bool(
            owner in self._geom_dirty
            or any(key[0] == owner for key in self._texture_original)
            or any(key[0] == owner for key in self._uv_original))
        reset_action = getattr(self, "edit_reset_action", None)
        if reset_action is not None:
            reset_action.setEnabled(can_reset)
        self._sync_edit_action_states()

    def _on_geometry_edited(self, owner: str) -> None:
        fam_obj = self._owner_to_obj.get(owner)
        if fam_obj is not None and getattr(fam_obj, "skeleton", None) \
                is not None:
            self._remember_geometry_original(owner)
            self._geom_dirty[owner] = fam_obj
        self._sync_geometry_save_controls()
        self._update_banner()

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
        restore = {}
        for key, target in snapshot.items():
            current = self.viewport.polygon_texture_override(*key)
            if key not in self._texture_original and current != target:
                self._texture_original[key] = current
            original = self._texture_original.get(key)
            restore[key] = target
            if target == original:
                self._texture_original.pop(key, None)
        self.viewport.restore_polygon_texture_overrides(restore)
        return True

    def _apply_uv_history(self, key, target) -> bool:
        owner, block_index, atts_index = key
        fam_obj = self._owner_to_obj.get(owner)
        if fam_obj is None or block_index >= len(fam_obj.base_object.ades):
            return False
        block = fam_obj.base_object.ades[block_index]
        if atts_index >= len(block.olpl):
            return False
        current = list(block.olpl[atts_index])
        if key not in self._uv_original and current != target:
            self._uv_original[key] = current
        original = self._uv_original.get(key)
        self._restore_uv(key, target)
        if list(target) == original:
            self._uv_original.pop(key, None)
        self.viewport.refresh_family_materials()
        return True

    def _apply_edit_command(self, command: dict, redo: bool) -> bool:
        target = command["after" if redo else "before"]
        kind = command.get("kind")
        self._history_replaying = True
        try:
            if kind == "geometry":
                applied = self._apply_geometry_history(command["owner"], target)
            elif kind == "texture":
                applied = self._apply_texture_history(target)
            elif kind == "uv":
                applied = self._apply_uv_history(command["key"], target)
            elif kind == "model_state":
                applied = True
                geometry = target.get("geometry")
                if geometry is not None:
                    applied = self._apply_geometry_history(
                        command["owner"], geometry) and applied
                if target.get("textures"):
                    applied = self._apply_texture_history(
                        target["textures"]) and applied
                for key, uvs in target.get("uvs", {}).items():
                    applied = self._apply_uv_history(key, uvs) and applied
            else:
                applied = False
        finally:
            self._history_replaying = False
        if not applied:
            return False
        self._fill_polygon_inspector(self._selected_poly)
        self._sync_geometry_save_controls()
        self._update_banner()
        self._sync_editor_context()
        return True

    def _undo_edit(self) -> None:
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
        """Confirm switching away from a model with unsaved changes."""

        if not self._geom_dirty and not self._texture_original \
                and not self._uv_original and not self._pending_repairs:
            return True
        if self._skip_model_switch_warning:
            return True
        box = QMessageBox(QMessageBox.Icon.Question, "Unsaved changes",
                          "This model has unsaved changes.\n"
                          "Switching models will discard them. Continue?",
                          QMessageBox.StandardButton.Yes
                          | QMessageBox.StandardButton.No, self)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        skip = QCheckBox("Don't show this again during this session")
        box.setCheckBox(skip)
        accepted = box.exec() == QMessageBox.StandardButton.Yes
        if accepted and skip.isChecked():
            self._skip_model_switch_warning = True
        return accepted

    def _bundle_skeleton_relative_path(self, fam_obj) -> Path:
        logical = (fam_obj.base_object.skeleton_name
                   or getattr(fam_obj.skeleton, "source_name", "")
                   or "MODEL.SKLT")
        logical = logical.replace("SET.BAS:", "").replace("\\", "/")
        parts = []
        for raw in logical.split("/"):
            if not raw or raw in (".", ".."):
                continue
            clean = re.sub(r'[^A-Za-z0-9_. -]', "_", raw).strip()
            if clean:
                parts.append(clean)
        relative = Path(*parts) if parts else Path("MODEL.SKLT")
        if relative.suffix.lower() not in (".skl", ".sklt"):
            relative = relative.with_suffix(".SKLT")
        return relative

    def _bundle_base_edits(self, owner: str, fam_obj):
        texture_edits: list[TextureNameEdit] = []
        for block_index, block in enumerate(fam_obj.base_object.ades):
            texture = block.texture
            if texture is None or not texture.name:
                continue
            poly_ids = {entry.poly_id for entry in block.atts}
            changed = {poly_id for changed_owner, poly_id
                       in self._texture_original
                       if changed_owner == owner and poly_id in poly_ids}
            if not changed:
                continue
            desired = {
                self.viewport.polygon_texture_override(owner, poly_id)
                for poly_id in changed
            }
            desired.discard(None)
            if not desired:
                continue
            if len(desired) > 1:
                raise MappingEditError(
                    f"Material #{block_index} has conflicting texture "
                    "changes. Use one texture for that material before "
                    "saving.")
            name = next(iter(desired))
            if name.lower() != texture.name.lower():
                texture_edits.append(TextureNameEdit(
                    "root", block_index, name))

        uv_edits: list[UVEdit] = []
        for changed_owner, block_index, atts_index in self._uv_original:
            if changed_owner != owner:
                continue
            blocks = fam_obj.base_object.ades
            if block_index >= len(blocks) \
                    or atts_index >= len(blocks[block_index].olpl):
                raise MappingEditError(
                    "an edited UV group no longer exists in the selected BASE")
            uv_edits.append(UVEdit(
                "root", block_index, atts_index,
                list(blocks[block_index].olpl[atts_index])))
        return uv_edits, texture_edits

    def _model_save_context(self):
        owner = self._selected_owner
        family = self._family
        fam_obj = self._owner_to_obj.get(owner) if owner else None
        model = getattr(fam_obj, "skeleton", None)
        base_asset = getattr(family, "base_asset", None)
        tree = getattr(base_asset, "tree", None)
        if owner is None or fam_obj is None or model is None \
                or tree is None:
            QMessageBox.information(
                self, "Save unavailable",
                "This model does not contain both model and BASE data.")
            return None
        return owner, family, fam_obj, model, tree

    def _save_model_as(self) -> None:
        context = self._model_save_context()
        if context is None:
            return
        owner, family, fam_obj, _model, _tree = context
        output = QFileDialog.getExistingDirectory(
            self, "Save model as",
            str(self._last_directory))
        if not output:
            self._notify("Save As cancelled.", 3000)
            return
        output_root = Path(output)
        skeleton_relative = self._bundle_skeleton_relative_path(fam_obj)
        skeleton_target = output_root / skeleton_relative
        base_label = (fam_obj.base_object.name
                      or skeleton_relative.stem)
        base_stem = re.sub(r'[^A-Za-z0-9_.-]', "_",
                           base_label) or "MODEL"
        base_target = output_root / f"{base_stem}.BASE"

        if self._write_model_files(
                owner, family, fam_obj, skeleton_target, base_target,
                ask_replace=True):
            self._bundle_targets[owner] = (skeleton_target, base_target)
            self._last_directory = output_root
            self._sync_geometry_save_controls()
            self._notify(
                f"Saved {skeleton_target.name} and {base_target.name}.",
                9000)

    def _overwrite_model(self) -> None:
        context = self._model_save_context()
        if context is None:
            return
        owner, family, fam_obj, _model, _tree = context
        targets = self._bundle_targets.get(owner)
        if targets is None:
            self._notify("Use Save As before Overwrite.", 5000)
            return
        skeleton_target, base_target = targets
        if self._write_model_files(
                owner, family, fam_obj, skeleton_target, base_target,
                ask_replace=False):
            self._notify(
                f"Overwritten {skeleton_target.name} and "
                f"{base_target.name}.", 9000)

    def _write_model_files(self, owner: str, family: AssetFamily, fam_obj,
                           skeleton_target: Path, base_target: Path,
                           *, ask_replace: bool) -> bool:
        """Write and verify the model plus its standalone BASE companion."""

        model = fam_obj.skeleton
        tree = family.base_asset.tree

        ref = getattr(fam_obj, "skeleton_ref", None)
        skeleton_source = (Path(ref.path) if ref is not None
                           and getattr(ref, "path", None) else None)
        base_source = Path(family.base_path) if family.base_path else None
        forbidden = [path.resolve() for path in (skeleton_source, base_source)
                     if path is not None and path.exists()]
        if skeleton_target.resolve() in forbidden \
                or base_target.resolve() in forbidden:
            QMessageBox.warning(
                self, "Choose another output folder",
                "Save As cannot replace the model currently open in the "
                "editor. Choose another folder.")
            return False
        existing = [path for path in (skeleton_target, base_target)
                    if path.exists()]
        if ask_replace and existing:
            answer = QMessageBox.warning(
                self, "Replace saved files?",
                "The following file(s) already exist:\n"
                + "\n".join(str(path) for path in existing)
                + "\n\nReplace them?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return False

        try:
            standalone = export_base_object_bytes(
                tree.data, fam_obj.base_object)
            uv_edits, texture_edits = self._bundle_base_edits(owner, fam_obj)
            with tempfile.TemporaryDirectory(
                    prefix="OpenUAStudio_bundle_") as temp_dir:
                temp_root = Path(temp_dir)
                temp_skeleton = temp_root / skeleton_target.name
                temp_base = temp_root / base_target.name
                save_sklt_with_poo2_points(
                    model, model.points, temp_skeleton)
                verify = parse_sklt_file(temp_skeleton)
                matches = (
                    len(verify.points) == len(model.points)
                    and all(
                        abs(a[axis] - b[axis])
                        <= 1e-3 + abs(b[axis]) * 1e-5
                        for a, b in zip(verify.points, model.points)
                        for axis in range(3)
                    )
                )
                if not matches:
                    raise MappingEditError(
                        "exported SKLT failed coordinate round-trip verification")
                notes = save_model_base_copy(
                    standalone, uv_edits, texture_edits, temp_base)
                notes.extend(_commit_verified_files([
                    (temp_skeleton, skeleton_target),
                    (temp_base, base_target),
                ]))
        except _BundleCommitError as exc:
            title = (
                "Save failed - current files unchanged"
                if exc.rollback_complete else
                "Save failed - inspect output files"
            )
            QMessageBox.critical(self, title, str(exc))
            return False
        except (MappingEditError, SkltParseError, OSError) as exc:
            QMessageBox.critical(
                self, "Save failed - current files unchanged", str(exc))
            return False

        self._update_banner()
        self._log("Model saved: " + "; ".join(notes[-3:]))
        return True

    def _uv_key(self) -> tuple[str, int, int] | None:
        if self._uv_ctx is None:
            return None
        fam_obj, _block, block_index, atts_index, _poly_id = self._uv_ctx
        return fam_obj.owner_path, block_index, atts_index

    def _update_uv_editor(self, poly_id: int | None) -> None:
        self._uv_ctx = None
        if poly_id is None or self._workbench_obj is None \
                or self._mapping_index is None:
            self.uv_editor.set_data(
                None, [], False,
                "Select a mapped polygon to edit its texture coordinates.")
            self.uv_revert_button.setEnabled(False)
            return
        refs = self._mapping_index.refs.get(poly_id, [])
        if not refs:
            self.uv_editor.set_data(
                None, [], False,
                "This polygon has no UV mapping. Use Mapping Repair first.")
            self.uv_revert_button.setEnabled(False)
            return
        ref = refs[0]
        block = ref.block
        uvs = (block.olpl[ref.atts_index]
               if ref.atts_index < len(block.olpl) else [])
        texture_name = (
            self.viewport.polygon_texture_override(
                self._workbench_obj.owner_path, poly_id)
            or (block.texture.name if block.texture else "")
        )
        image = self._texture_qimage(texture_name) if texture_name else None
        editable = bool(uvs) and (block.class_id or "").lower() == "amesh.class"
        message = "Drag the yellow UV handles over the texture."
        if image is None:
            message += " Texture pixels are unavailable; coordinates remain editable."
        if not editable:
            message = "This material has no editable OLPL UV group."
        self._uv_ctx = (self._workbench_obj, block, ref.block_index,
                        ref.atts_index, poly_id)
        self.uv_editor.set_data(image, uvs, editable, message)
        self.uv_revert_button.setEnabled(
            self._uv_key() in self._uv_original)

    def _on_uv_changed(self, uvs: list[tuple[int, int]]) -> None:
        if self._uv_ctx is None:
            return
        fam_obj, block, block_index, atts_index, poly_id = self._uv_ctx
        key = self._uv_key()
        if key is None or atts_index >= len(block.olpl):
            return
        if self._uv_history_before is None \
                or self._uv_history_before[0] != key:
            self._uv_history_before = (key, list(block.olpl[atts_index]))
        if key not in self._uv_original:
            self._uv_original[key] = list(block.olpl[atts_index])
        updated = [tuple(uv) for uv in uvs]
        block.olpl[atts_index] = updated
        if block_index < len(fam_obj.materials):
            group = fam_obj.materials[block_index]
            for index, (pid, _old_uvs, shade) in enumerate(group.faces):
                if pid == poly_id:
                    group.faces[index] = (pid, updated, shade)
                    break
        if self._uv_original.get(key) == updated:
            del self._uv_original[key]
        self.uv_revert_button.setEnabled(key in self._uv_original)
        self._sync_geometry_save_controls()
        self._update_banner()

    def _on_uv_edit_finished(self) -> None:
        if self._uv_ctx is None or self._family is None:
            return
        history = self._uv_history_before
        self._uv_history_before = None
        if history is not None:
            key, before = history
            fam_obj, block, _block_index, atts_index, _poly_id = self._uv_ctx
            if key == self._uv_key() and atts_index < len(block.olpl):
                self._record_edit_command({
                    "kind": "uv",
                    "key": key,
                    "before": list(before),
                    "after": list(block.olpl[atts_index]),
                    "label": "UV edit",
                })
        poly_id = self._uv_ctx[4]
        self.viewport.refresh_family_materials()
        self.viewport.set_selected_polygon(poly_id)
        self._sync_animation_controls()
        self._sync_editor_context()

    def _restore_uv(self, key: tuple[str, int, int],
                    uvs: list[tuple[int, int]]) -> None:
        owner, block_index, atts_index = key
        fam_obj = self._owner_to_obj.get(owner)
        if fam_obj is None or block_index >= len(fam_obj.base_object.ades):
            return
        block = fam_obj.base_object.ades[block_index]
        if atts_index >= len(block.olpl):
            return
        updated = [tuple(uv) for uv in uvs]
        block.olpl[atts_index] = updated
        entry = block.atts[atts_index] if atts_index < len(block.atts) else None
        if block_index < len(fam_obj.materials) and entry is not None:
            group = fam_obj.materials[block_index]
            for index, (pid, _old_uvs, shade) in enumerate(group.faces):
                if pid == entry.poly_id:
                    group.faces[index] = (pid, updated, shade)
                    break

    def _revert_selected_uv(self) -> None:
        key = self._uv_key()
        if key is None or key not in self._uv_original:
            return
        before = self.uv_editor.uvs()
        target = list(self._uv_original.pop(key))
        self._restore_uv(key, target)
        self._record_edit_command({
            "kind": "uv", "key": key,
            "before": before, "after": target,
            "label": "UV revert",
        })
        poly_id = self._uv_ctx[4] if self._uv_ctx else None
        self._update_uv_editor(poly_id)
        self._on_uv_edit_finished()
        self._sync_geometry_save_controls()
        self._update_banner()
        self.statusBar().showMessage("Selected UV mapping reset.", 4000)

    def _reset_model(self) -> None:
        owner = self._selected_owner
        has_geometry = owner in self._geom_dirty
        has_texture = any(key[0] == owner for key in self._texture_original)
        has_uv = any(key[0] == owner for key in self._uv_original)
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
        before_state = {
            "geometry": list(model.points) if model is not None else None,
            "textures": {
                key: self.viewport.polygon_texture_override(*key)
                for key in texture_keys
            },
            "uvs": {
                key: list(self._owner_to_obj[key[0]].base_object
                          .ades[key[1]].olpl[key[2]])
                for key in uv_keys
            },
        }
        resume_edit = self.global_edit_button.isChecked()
        if self.viewport.is_edit_mode:
            self.viewport.exit_edit_mode()
        original = self._geom_original.get(owner)
        if model is not None and original is not None:
            model.points[:] = list(original)
        texture_restore = {
            key: original
            for key, original in self._texture_original.items()
            if key[0] == owner
        }
        if texture_restore:
            self.viewport.restore_polygon_texture_overrides(texture_restore)
            for key in texture_restore:
                del self._texture_original[key]
        for key, uvs in list(self._uv_original.items()):
            if key[0] != owner:
                continue
            self._restore_uv(key, uvs)
            del self._uv_original[key]
        after_state = {
            "geometry": list(model.points) if model is not None else None,
            "textures": dict(texture_restore),
            "uvs": {
                key: list(self._owner_to_obj[key[0]].base_object
                          .ades[key[1]].olpl[key[2]])
                for key in uv_keys
            },
        }
        self._geom_dirty.pop(owner, None)
        if not texture_restore:
            self.viewport.refresh_family_materials()
        self._fill_polygon_inspector(self._selected_poly)
        self._sync_geometry_save_controls()
        self._update_banner()
        if resume_edit:
            self.global_edit_button.setChecked(True)
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
        names = list(family.textures)
        archive = family.setbas_archive or self._setbas
        if archive is not None:
            names.extend(
                resource.resource_name for resource in archive.resources
                if resource.decodable
                and resource.class_id.lower() == "ilbm.class"
            )
        unique = {}
        for name in names:
            unique.setdefault(name.lower(), name)
        return sorted(unique.values(), key=str.lower)

    def _ensure_model_texture_loaded(self, name: str) -> str | None:
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
        resources = archive.find(name, "ilbm.class")
        resource = next((item for item in resources if item.decodable), None)
        if resource is None:
            return None
        try:
            image = decode_texture(archive, resource)
        except Exception as exc:
            QMessageBox.warning(self, "Texture load failed", str(exc))
            return None
        family.textures[resource.resource_name] = image
        return resource.resource_name

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

    def _choose_model_texture(self, names: list[str],
                              current: str) -> str | None:
        dialog = TexturePickerDialog(
            names, current, self._texture_thumbnail_for_picker, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self._notify("Texture selection cancelled.", 3000)
            return None
        return dialog.selected_name()

    def _load_model_texture(self) -> None:
        if self._mapping_index is None or self._selected_poly is None \
                or self._workbench_obj is None:
            return
        refs = [
            ref for ref in self._mapping_index.refs.get(
                self._selected_poly, [])
            if ref.block.texture is not None
        ]
        if not refs:
            QMessageBox.information(
                self, "No textured material",
                "The selected polygon has no replaceable texture reference.")
            return
        if len(refs) != 1:
            QMessageBox.information(
                self, "Texture replacement unavailable",
                "The selected polygon belongs to more than one material. "
                "Resolve the duplicate mapping before replacing its texture.")
            return
        ref = refs[0]
        if ref.block.texture.kind != "ilbm":
            QMessageBox.information(
                self, "Animated material",
                "This polygon uses an animated texture. Replacing it with a "
                "static ILBM would change the BASE material structure and "
                "cannot be saved safely.")
            return
        names = self._available_model_textures()
        if not names:
            QMessageBox.information(
                self, "No textures available",
                "No decoded ILBM texture is available in this family or SET.BAS.")
            return
        owner = self._workbench_obj.owner_path
        current = (self.viewport.polygon_texture_override(
            owner, self._selected_poly) or ref.block.texture.name)
        chosen = self._choose_model_texture(names, current)
        if not chosen:
            return
        loaded_name = self._ensure_model_texture_loaded(chosen)
        if loaded_name is None:
            QMessageBox.warning(
                self, "Texture load failed",
                f"{chosen} could not be decoded from the loaded sources.")
            return
        selected = self._selected_polys or {self._selected_poly}
        selected_refs = []
        for poly_id in selected:
            poly_refs = [
                item for item in self._mapping_index.refs.get(poly_id, [])
                if item.block.texture is not None
            ]
            if len(poly_refs) != 1 \
                    or poly_refs[0].block.texture.kind != "ilbm":
                QMessageBox.information(
                    self, "Texture replacement unavailable",
                    "Every selected polygon must have one unambiguous static "
                    "ILBM material. No texture was changed.")
                return
            selected_refs.append(poly_refs[0])

        applicable: set[int] = set()
        for item in selected_refs:
            block_ids = {entry.poly_id for entry in item.block.atts}
            for poly_id in block_ids:
                mapped = self._mapping_index.refs.get(poly_id, [])
                if len(mapped) != 1 or mapped[0].block is not item.block:
                    QMessageBox.information(
                        self, "Texture replacement unavailable",
                        "A selected material overlaps duplicate mapping. "
                        "Resolve it before replacing the texture.")
                    return
            applicable.update(block_ids)
        before_overrides = {
            (owner, poly_id): self.viewport.polygon_texture_override(
                owner, poly_id)
            for poly_id in applicable
        }
        restore = {}
        apply = set()
        for poly_id in applicable:
            key = (owner, poly_id)
            if key not in self._texture_original:
                self._texture_original[key] = (
                    self.viewport.polygon_texture_override(owner, poly_id))
            original_override = self._texture_original[key]
            poly_refs = self._mapping_index.refs.get(poly_id, [])
            base_name = next(
                (item.block.texture.name for item in poly_refs
                 if item.block.texture is not None), "")
            original_name = original_override or base_name
            if loaded_name.lower() == original_name.lower():
                restore[key] = original_override
                del self._texture_original[key]
            else:
                apply.add(poly_id)
        if restore:
            self.viewport.restore_polygon_texture_overrides(restore)
        if apply:
            self.viewport.set_polygon_texture_overrides(
                owner, apply, loaded_name)
        self._selected_polys = set(applicable)
        self.viewport.set_highlight_polys(self._selected_polys)
        after_overrides = {
            key: self.viewport.polygon_texture_override(*key)
            for key in before_overrides
        }
        self._record_edit_command({
            "kind": "texture",
            "before": before_overrides,
            "after": after_overrides,
            "label": "texture assignment",
        })
        self._fill_polygon_inspector(self._selected_poly)
        self._sync_geometry_save_controls()
        self._update_banner()
        self._sync_editor_context()
        self.statusBar().showMessage(
            f"Texture {loaded_name} applied to {len(applicable)} "
            "material-linked area(s).",
            8000)

    # -- repair -----------------------------------------------------------------

    def _update_repair_buttons(self) -> None:
        can_plan = (
            self._selected_poly is not None
            and self._mapping_index is not None
            and self._mapping_index.status(self._selected_poly) == "unmapped"
            and self._family is not None
            and self._family.base_path is not None
            and self.repair_target_combo.count() > 0
        )
        self.repair_copy_button.setEnabled(can_plan)
        self.repair_planar_button.setEnabled(can_plan)
        self.repair_apply_button.setEnabled(self._repair_plan is not None)
        self.repair_revert_button.setEnabled(bool(self._pending_repairs))
        self.repair_save_button.setEnabled(bool(self._pending_repairs))

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
        plan = self._repair_plan
        if plan is None or self._workbench_obj is None:
            return
        from base_parser import AttsEntry

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
            "Use Save As to keep it."
        )

    def _revert_repairs(self) -> None:
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
            self, "Save mapping as",
            str(suggested),
            "Urban Assault BASE (*.base *.bas);;All files (*)",
        )
        if not path:
            return
        try:
            notes = save_repaired_base(self._family, self._workbench_obj,
                                       self._pending_repairs, path)
        except MappingEditError as exc:
            QMessageBox.critical(self, "Save failed - nothing written",
                                 str(exc))
            return
        self._saved_repair_path = path
        self._pending_repairs = []
        self._log("; ".join(notes))
        self._notify(f"Mapping saved: {Path(path).name}.", 8000)
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
        if family is not self._family:
            self._bundle_targets.clear()
        self._family = family
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
        self._fill_asset_tree(family)
        if self._selected_owner:
            selected_item = self._owner_to_item.get(self._selected_owner)
            if selected_item is not None:
                self.asset_tree.setCurrentItem(selected_item)
        self._fill_textures(family)
        self._fill_resolve(family)
        self._fill_chunks(family)
        self._fill_checks(family)
        self._pending_repairs = []
        self._geom_dirty = {}
        self._geom_original = {}
        self._uv_ctx = None
        self._uv_original = {}
        self._texture_original = {}
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

        self._update_banner()
        self._sync_editor_context()
        status, _details = self._completeness(family)
        self._log(f"loaded {family.base_path}: {status}")

        title_path = family.base_path or family.setbas_path
        self._set_document_title(title_path)
        name = title_path.name if title_path else "manual family"
        diag = (f", textured preview incomplete "
                f"({len(family.textured_diagnostics)} issue(s), see Resolve)"
                if family.textured_diagnostics else "")
        self.statusBar().showMessage(
            f"Loaded {name}: {len(family.all_objects())} object(s), "
            f"{len(family.textures)} texture(s), "
            f"{len(family.animations)} animation(s), "
            f"{len(family.warnings)} warning(s)"
            f"{diag}"
        )

    def _tree_item(self, label: str, status: str, node_data) -> QTreeWidgetItem:
        item = QTreeWidgetItem([label, status])
        if node_data and node_data[0] == "group":
            # Pure category rows remain clickable for expand/collapse but are
            # not selectable, avoiding the accent stripe over their arrow.
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
        item.setIcon(0, _status_icon({
            "auto_loaded": "found", "loaded": "found", "resolved": "found",
            "kept_for_session": "manual", "trial_loaded": "manual",
            "ambiguous": "ambiguous", "missing": "missing",
            "failed_load": "decode failed", "skipped": "missing",
            "unsupported_loader": "ambiguous",
        }.get(status, status)))
        item.setData(0, Qt.ItemDataRole.UserRole, node_data)
        return item

    def _fill_asset_tree(self, family: AssetFamily) -> None:
        self.asset_tree.clear()
        self._owner_to_item = {}
        root_label = family.base_path.name if family.base_path else "manual family"
        root_item = self._tree_item(f"Root BASE: {root_label}", "loaded",
                                    ("base", None))
        self.asset_tree.addTopLevelItem(root_item)
        if family.root_object is not None:
            self._owner_to_item[family.root_object.owner_path] = root_item

        dep_status = {d.raw_ref: self._effective_status(d.raw_ref, d.status)
                      for d in family.dependencies}

        def texture_node(name: str) -> QTreeWidgetItem:
            status = dep_status.get(name, "?")
            return self._tree_item(name, status, ("texture", name))

        def animation_node(name: str) -> QTreeWidgetItem:
            status = dep_status.get(name, "?")
            return self._tree_item(name, status, ("animation", name))

        def add_object(fam_obj, parent_item, owner: str):
            skel_name = fam_obj.base_object.skeleton_name
            if skel_name:
                status = dep_status.get(
                    skel_name, "auto_loaded" if fam_obj.skeleton else "missing")
                parent_item.addChild(self._tree_item(
                    f"Skeleton: {skel_name}", status,
                    ("skeleton", fam_obj)))

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
                    group.addChild(texture_node(name))
            if animations:
                group = self._tree_item(f"Animations ({len(animations)})",
                                        "", ("group", None))
                parent_item.addChild(group)
                for name in animations:
                    group.addChild(animation_node(name))

            if fam_obj.kids:
                kids_group = self._tree_item(
                    f"Children / KIDS ({len(fam_obj.kids)})", "",
                    ("group", None))
                parent_item.addChild(kids_group)
                for index, kid in enumerate(fam_obj.kids):
                    polys = (kid.skeleton.parsed_polygon_count
                             if kid.skeleton else 0)
                    kid_item = self._tree_item(
                        f"Child {index}: {kid.display_name} "
                        f"({polys} polys)",
                        "resolved" if kid.skeleton else "missing",
                        ("child", kid))
                    kid_item.setToolTip(0, kid.owner_path)
                    kids_group.addChild(kid_item)
                    self._owner_to_item[kid.owner_path] = kid_item
                    add_object(kid, kid_item, kid.owner_path)

        if family.root_object:
            add_object(family.root_object, root_item, "root")

        # VANM frame bitmaps not already shown
        anm_bitmaps = [d.raw_ref for d in family.dependencies
                       if d.kind == "anm_bitmap"]
        if anm_bitmaps:
            group = self._tree_item(
                f"VANM frame bitmaps ({len(anm_bitmaps)})", "",
                ("group", None))
            root_item.addChild(group)
            for name in anm_bitmaps:
                group.addChild(texture_node(name))

        self.asset_tree.expandAll()
        self.asset_tree.resizeColumnToContents(0)

    def _focus_assets_for_owner(self, owner: str | None,
                                switch_tabs: bool = True) -> None:
        """Show and highlight the files composing the newly opened model."""

        if owner is None or owner not in self._owner_to_item:
            return
        if switch_tabs:
            self._right_tabs.setCurrentWidget(self._resources_tabs)
            self._resources_tabs.setCurrentWidget(self._assets_panel)
        owner_item = self._owner_to_item[owner]
        focus_item = owner_item
        self.asset_tree.blockSignals(True)
        self.asset_tree.clearSelection()
        owner_item.setSelected(True)
        owner_item.setExpanded(True)

        def select_components(item) -> None:
            nonlocal focus_item
            for index in range(item.childCount()):
                child = item.child(index)
                data = child.data(0, Qt.ItemDataRole.UserRole)
                kind = data[0] if data else ""
                if kind == "child":
                    continue
                if kind in ("skeleton", "texture", "animation"):
                    child.setSelected(True)
                    if kind == "skeleton":
                        focus_item = child
                elif kind == "group" and not child.text(0).startswith(
                        "Children / KIDS"):
                    child.setExpanded(True)
                    select_components(child)

        select_components(owner_item)
        self.asset_tree.setCurrentItem(focus_item)
        # setCurrentItem clears an extended selection; restore the complete
        # component highlight after establishing the keyboard/current row.
        owner_item.setSelected(True)
        select_components(owner_item)
        self.asset_tree.scrollToItem(
            focus_item, QAbstractItemView.ScrollHint.PositionAtCenter)
        self.asset_tree.blockSignals(False)

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
                    return self._family.root_object.owner_path
            current = current.parent()
        return None

    def _on_tree_double_clicked(self, item, _column=0) -> None:
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data:
            kind, payload = data
            if kind == "texture":
                self._preview_family_texture(payload)
                return
            if kind == "animation":
                self._play_asset_animation(item)
                return
        owner = self._tree_owner_for_item(item)
        if owner:
            self._select_owner(owner)
            self.viewport.frame_owner(owner)

    def _filter_asset_tree(self, text: str) -> None:
        text = text.strip().lower()

        def visit(item) -> bool:
            child_hit = any(visit(item.child(i))
                            for i in range(item.childCount()))
            hit = (not text) or text in item.text(0).lower() or child_hit
            item.setHidden(not hit)
            return hit

        for i in range(self.asset_tree.topLevelItemCount()):
            visit(self.asset_tree.topLevelItem(i))

    def _route_texture_editor(self, name: str) -> None:
        if not name or not hasattr(self, "_editor_tabs"):
            return
        normalized = Path(name.replace("\\", "/")).name.lower()
        fx_materials = {
            Path(material.replace("\\", "/")).name.lower()
            for element in self._fx_elements
            for material in element.material_names
        }
        self._right_tabs.setCurrentWidget(self._editor_tabs)
        self._editor_tabs.setCurrentWidget(self._model_editor_panel)
        is_fx = (normalized in fx_materials
                 or Path(normalized).stem in {"fx1", "fx2"})
        if is_fx:
            matching = next(
                (element for element in self._fx_elements
                 if any(Path(material.replace("\\", "/")).name.lower()
                        == normalized for material in element.material_names)),
                None,
            )
            if matching is not None:
                self.fx_combo.blockSignals(True)
                self.fx_combo.setCurrentIndex(
                    self._fx_combo_index(matching.identity))
                self.fx_combo.blockSignals(False)

    def _on_polygon_deselected(self, poly_id: int) -> None:
        self._selected_polys.discard(poly_id)
        self._selected_poly = next(iter(self._selected_polys), None)
        self.viewport.set_selected_polygon(self._selected_poly)
        self.viewport.set_highlight_polys(self._selected_polys)
        self._fill_polygon_inspector(self._selected_poly)
        self._update_repair_buttons()
        self._notify(
            f"Deselected polygon #{poly_id}."
            if self._selected_poly is not None else
            "Selection cleared.", 3500)

    def _on_selection_cleared(self) -> None:
        self._selected_polys.clear()
        self._selected_poly = None
        self.viewport.set_selected_polygon(None)
        self.viewport.set_highlight_polys(set())
        self._fill_polygon_inspector(None)
        self._update_repair_buttons()
        self._notify("Selection cleared.", 3500)

    def _on_tree_node_selected(self, current, _previous=None) -> None:
        if current is None or self._family is None:
            self._set_object_info(["No asset selected."])
            return
        data = current.data(0, Qt.ItemDataRole.UserRole)
        if not data:
            self._set_object_info(["No asset information available."])
            return
        owner = self._tree_owner_for_item(current)
        if owner:
            self._select_owner(owner)
        kind, payload = data
        family = self._family
        lines = []

        if kind == "base":
            lines.extend(["BASE prefab", f"path: {family.base_path}",
                          f"search root: {family.search_root}"])
            objects = family.all_objects()
            lines.append(f"objects: {len(objects)} "
                         f"(children: {max(0, len(objects) - 1)})")
            from base_dependency_resolver import summarize
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
        if kind == "texture":
            self._route_texture_editor(payload)

    def _fill_textures(self, family: AssetFamily) -> None:
        self.texture_list.clear()
        for name, ref in family.texture_refs.items():
            img = family.textures.get(name)
            status = ref.status
            if ref.path is not None and img is None:
                status = "decode failed"

            lines = [f"[{status.upper()}] {name}"
                     + (f" (source: {ref.source})" if ref.source else "")]
            if ref.found:
                lines.append(ref.display_path)
            else:
                lines.append("NOT FOUND - use the Resolve panel to assign a file")
            if len(ref.candidates) > 1:
                lines.append(f"{len(ref.candidates)} candidates "
                             "(see Resolve panel)")
            tracy_modes = sorted(family.texture_tracy_usage.get(name, set())
                                 - {"none"})
            if tracy_modes:
                lines.append(f"tracy transparency used: {', '.join(tracy_modes)}")
            if img:
                compression = "ByteRun1" if img.compression else "none"
                palette = ("own CMAP" if img.palette else
                           ("external PAL" if family.external_palette
                            else "MISSING (grayscale preview)"))
                body = "decoded" if img.has_body else "no BODY"
                lines.append(f"{img.kind} {img.width}x{img.height}, "
                             f"{img.n_planes} planes, {compression}, "
                             f"{palette}, {body}")
                if img.has_body:
                    chroma = img.chroma_transparent_count(
                        family.external_palette if not img.palette else None
                    )
                    if chroma:
                        lines.append(f"{chroma} px chroma-transparent "
                                     "(palette yellow 255,255,0)")
                for warning in img.warnings:
                    lines.append(f"warning: {warning}")
            item = QListWidgetItem("\n".join(lines))
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setForeground(STATUS_COLORS.get(status, QColor(220, 220, 220)))
            if img is not None and img.has_body:
                rgba = img.to_rgba_bytes(
                    family.external_palette if not img.palette else None,
                    "chroma",
                )
                qimage = QImage(rgba, img.width, img.height, img.width * 4,
                                QImage.Format.Format_RGBA8888)
                item.setIcon(_checker_thumbnail(qimage))
            else:
                item.setIcon(_status_icon(status))
            self.texture_list.addItem(item)

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


if __name__ == "__main__":
    import sys

    from PySide6.QtWidgets import QApplication
    from main import _open_startup_path

    app = QApplication(sys.argv)
    window = AssemblyWindow()
    window.show()
    if len(sys.argv) > 1:
        _open_startup_path(window, sys.argv[1])
    raise SystemExit(app.exec())
