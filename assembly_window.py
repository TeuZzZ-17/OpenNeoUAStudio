"""OpenUAStudio: integrated Urban Assault asset workbench.

The main window assembles BASE + skeleton + texture + animation families,
provides the former BASet extraction/conversion workflows, and launches the
integrated Wireframe Editor and Map Editor.  Original assets are only written through
explicit verified Save As or extraction actions.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import re
from pathlib import Path

from PySide6.QtCore import QSize, QTimer, QUrl, Qt
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QDesktopServices,
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
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
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
from asset_diff import SourceDiff, diff_family
from asset_report import family_to_json, family_to_markdown
from assembly_viewer import AssetViewport, VIEW_MODES, VIEW_PRESETS
from base_mapping_editor import (
    MappingEditError,
    MappingIndex,
    RepairPlan,
    eligible_blocks,
    plan_copy_style,
    plan_planar,
    save_repaired_base,
)
from dependency_profile import DependencyProfile
from setbas_reader import SetBasArchive, SetBasError, read_setbas
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


STATUS_COLORS = {
    "found": QColor(90, 200, 110),
    "manual": QColor(110, 170, 255),
    "manual (SET.BAS)": QColor(110, 170, 255),
    "setbas": QColor(120, 210, 210),
    "ambiguous": QColor(255, 190, 70),
    "missing": QColor(240, 90, 90),
    "decode failed": QColor(200, 90, 200),
}


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


def _polygon_normal(points) -> tuple[float, float, float] | None:
    if len(points) < 3:
        return None
    nx = ny = nz = 0.0
    for i, (x0, y0, z0) in enumerate(points):
        x1, y1, z1 = points[(i + 1) % len(points)]
        nx += (y0 - y1) * (z0 + z1)
        ny += (z0 - z1) * (x0 + x1)
        nz += (x0 - x1) * (y0 + y1)
    length = (nx * nx + ny * ny + nz * nz) ** 0.5
    if length < 1e-9:
        return None
    return (nx / length, ny / length, nz / length)


def _draw_uv_polygon(painter: QPainter, uvs, size: int) -> None:
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QPolygonF

    points = [QPointF(u / 256 * size, v / 256 * size) for u, v in uvs]
    if len(points) >= 2:
        painter.drawPolygon(QPolygonF(points))
    for p in points:
        painter.drawEllipse(p, 2.0, 2.0)


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
        self._repair_dialog: QDialog | None = None
        # Large Family Mode / object selection state
        self._large_mode = False
        self._selected_owner: str | None = None
        self._owner_to_obj: dict[str, object] = {}
        self._owner_to_item: dict[str, QTreeWidgetItem] = {}
        # UV/ATTS editor state: in-memory only until Save BASE As (edits)
        self._uv_ctx: tuple | None = None
        self._uv_original: dict[tuple, list] = {}
        # key -> original (color, shade, tracy) of the edited ATTS entry
        self._atts_original: dict[tuple, tuple] = {}
        # optional read-only SET.BAS resource provider
        self._setbas: SetBasArchive | None = None
        # last source diff result (for the panel and report export)
        self._diff: SourceDiff | None = None
        # Polygon Mapping Workbench state
        self._mapping_index: MappingIndex | None = None
        self._workbench_obj = None          # first skeleton-bearing FamilyObject
        self._selected_poly: int | None = None
        self._repair_plan: RepairPlan | None = None
        self._pending_repairs: list[RepairPlan] = []
        self._saved_repair_path: str | None = None
        # geometry Edit Mode: owners with unsaved vertex edits
        self._geom_dirty: dict[str, object] = {}
        self._snapshot_mode_active = False
        self._snapshot_custom_color: QColor | None = None
        self._snapshot_zoom_percent = 100

        self.viewport = AssetViewport()
        self.viewport.statusMessage.connect(
            lambda text: self.statusBar().showMessage(text, 1500)
        )
        self.viewport.polygonPicked.connect(self._on_polygon_picked)
        self.viewport.objectPicked.connect(self._on_object_picked)
        self.viewport.editModeChanged.connect(self._on_edit_mode_toggled)
        self.viewport.geometryEdited.connect(self._on_geometry_edited)
        self.viewport.animationFrameChanged.connect(
            self._update_snapshot_frame_text)
        self.viewport.editHint.connect(
            lambda text: self.statusBar().showMessage(text, 30000)
        )

        self.asset_tree = QTreeWidget()
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
        self.refs_tree = QTreeWidget()
        self.refs_tree.setHeaderLabels(["Reference", "Value"])
        self.stats_tree = QTreeWidget()
        self.stats_tree.setHeaderLabels(["Stat", "Value"])
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
        self.diff_tree = QTreeWidget()
        self.diff_tree.setHeaderLabels(
            ["Resource", "Kind", "Status", "Summary"]
        )
        self.diff_tree.currentItemChanged.connect(self._show_diff_details)
        self.diff_label = QLabel(
            "Load a .base family, open a SET.BAS provider, then use "
            "'Compare with SET.BAS source...'."
        )
        self.diff_label.setWordWrap(True)
        self.diff_filter = QComboBox()
        for label in ("Show all", "Only differences", "Only missing",
                      "Only decode failures", "Only mapping warnings"):
            self.diff_filter.addItem(label)
        self.diff_filter.currentIndexChanged.connect(
            lambda _: self._fill_diff()
        )
        self.diff_details = QListWidget()
        self.diff_thumbs = QLabel()
        self.diff_thumbs.setVisible(False)

        # Polygon Inspector widgets
        self.poly_info = QListWidget()
        self.poly_uv_label = QLabel("Select a polygon in the viewport.")
        self.poly_uv_label.setMinimumHeight(200)
        self.blocks_list = QListWidget()
        self.blocks_list.currentRowChanged.connect(self._on_block_selected)
        self.repair_target_combo = QComboBox()
        self.repair_source_spin = QSpinBox()
        self.repair_source_spin.setRange(0, 65535)
        self.repair_copy_button = QPushButton("Copy style from polyID")
        self.repair_copy_button.clicked.connect(self._plan_copy_style)
        self.repair_planar_button = QPushButton("Planar UV to target block")
        self.repair_planar_button.clicked.connect(self._plan_planar)
        self.repair_preview = QListWidget()
        self.repair_apply_button = QPushButton("Apply in memory")
        self.repair_apply_button.clicked.connect(self._apply_repair_in_memory)
        self.repair_revert_button = QPushButton("Revert in memory")
        self.repair_revert_button.clicked.connect(self._revert_repairs)
        self.repair_save_button = QPushButton("Save As...")
        self.repair_save_button.clicked.connect(self._save_repaired_as)
        self.anim_tree = QTreeWidget()
        self.anim_tree.setHeaderLabels(["Animation", "Detail"])
        self.chunk_tree = QTreeWidget()
        self.chunk_tree.setHeaderLabels(["Chunk", "Size", "Offset"])
        self.warning_list = QListWidget()
        self.checks_list = QListWidget()
        self.log_list = QListWidget()
        self.node_inspector = QListWidget()
        for widget in (
                self.node_inspector, self.poly_info, self.blocks_list,
                self.repair_preview, self.diff_tree, self.anim_tree,
                self.refs_tree, self.stats_tree, self.warning_list,
                self.checks_list, self.log_list):
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
        self.statusBar().showMessage(
            "Original assets are never overwritten; edits use verified Save As."
        )

    # -- UI scaffolding --------------------------------------------------------

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
        if widget is not self.node_inspector:
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
            copy_names.triggered.connect(lambda: QApplication.clipboard().setText(
                "\n".join(resource.resource_name for resource in resources)))
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
        path = self._asset_item_path(item)
        if path is not None:
            menu.addAction("Reveal source file",
                           lambda: self._reveal_asset_item(item))
        menu.addSeparator()
        menu.addAction("Copy item", lambda: QApplication.clipboard().setText(
            self._widget_item_text(self.asset_tree, item)))
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
            menu.addAction("Copy texture name(s)", lambda:
                           QApplication.clipboard().setText("\n".join(names)))
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
                ("Trial-load", self._use_selected_candidate),
                ("Keep for session", self._keep_for_session),
                ("Compare...", self._compare_candidate),
                ("Reveal in folder", self._reveal_in_folder),
                ("Assign file...", self._assign_manual_file),
                ("Unload / revert", self._clear_override),
                ("Save choice", self._save_choice),
                ("Apply saved", self._apply_saved_choice),
                ("Forget saved", self._forget_choice)):
            action = menu.addAction(text, slot)
            action.setEnabled(target is not None)
        menu.addSeparator()
        menu.addAction("Copy row", lambda:
                       QApplication.clipboard().setText(
                           self._widget_item_text(self.resolve_tree, item)))
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
        toggle_edit = QAction("Geometry Edit Mode\tTab", self)
        toggle_edit.triggered.connect(self.viewport.toggle_edit_mode)
        edit_menu.addAction(toggle_edit)
        edit_menu.addSeparator()
        undo_geo = QAction("Undo vertex edit\tCtrl+Z (viewport)", self)
        undo_geo.triggered.connect(self.viewport.edit_undo)
        edit_menu.addAction(undo_geo)
        redo_geo = QAction("Redo vertex edit\tCtrl+Shift+Z (viewport)", self)
        redo_geo.triggered.connect(self.viewport.edit_redo)
        edit_menu.addAction(redo_geo)
        edit_menu.addSeparator()
        self.save_skeleton_action = QAction("Save edited skeleton As...",
                                            self)
        self.save_skeleton_action.setEnabled(False)
        self.save_skeleton_action.triggered.connect(self._save_skeleton_as)
        edit_menu.addAction(self.save_skeleton_action)

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
            "Tab Edit Mode (G move, R rotate, S scale, X/Y/Z axis, B box, "
            "A all, Ctrl+Z undo)",
            10000))
        view_menu.addAction(help_action)

        tools_menu = self.menuBar().addMenu("&Tools")
        compare_action = QAction("Compare with SET.BAS source...", self)
        compare_action.triggered.connect(self.run_source_diff)
        tools_menu.addAction(compare_action)
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
        show_log = QAction("Log / Diff results", self)
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

        # --- toolbar: essentials only ---
        toolbar = QToolBar("Workbench", self)
        toolbar.setMovable(False)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, toolbar)

        toolbar.addAction(open_base)
        toolbar.addAction(open_setbas)
        toolbar.addAction(reload_action)
        toolbar.addSeparator()

        self.edit_mode_combo = QComboBox()
        self.edit_mode_combo.addItem("Select", "select")
        self.edit_mode_combo.addItem("Edit vertices", "edit")
        self.edit_mode_combo.currentIndexChanged.connect(
            self._on_edit_mode_changed)
        toolbar.addWidget(QLabel(" Mode: "))
        toolbar.addWidget(self.edit_mode_combo)

        self.mode_combo = QComboBox()
        for mode in VIEW_MODES:
            label = {"wireframe": "Wireframe",
                     "solid": "Solid",
                     "materials": "Material groups",
                     "textured": "Textured"}[mode]
            self.mode_combo.addItem(label, mode)
        self.mode_combo.setCurrentIndex(VIEW_MODES.index("textured"))
        self.mode_combo.currentIndexChanged.connect(
            lambda _: self.viewport.set_mode(self.mode_combo.currentData())
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
        self.speed_spin.valueChanged.connect(self.viewport.set_animation_speed)
        anim_bar.addWidget(self.speed_spin)

    def _build_layout(self) -> None:
        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        tabs.setUsesScrollButtons(True)
        tabs.tabBar().setExpanding(False)
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

        inspector_panel = QWidget()
        inspector_layout = QVBoxLayout(inspector_panel)
        inspector_layout.setContentsMargins(5, 5, 5, 5)
        inspector_layout.addWidget(self.node_inspector, 1)

        textures_panel = QWidget()
        textures_layout = QVBoxLayout(textures_panel)
        textures_layout.setContentsMargins(5, 5, 5, 5)
        textures_layout.setSpacing(4)
        textures_layout.addWidget(self.texture_list, 1)
        self.texture_export_button = QPushButton("Export selected as PNG...")
        self.texture_export_button.clicked.connect(self._export_texture_png)
        textures_layout.addWidget(self.texture_export_button)

        # Polygon/UV tools use a narrow, vertically stacked design.  Nothing
        # here should force the entire right panel to become excessively wide.
        poly_panel = QWidget()
        poly_layout = QVBoxLayout(poly_panel)
        poly_layout.setContentsMargins(5, 5, 5, 5)
        poly_layout.setSpacing(4)
        self.poly_info.setMaximumHeight(92)
        poly_layout.addWidget(self.poly_info)

        uv_box = QGroupBox("UVs and polygon attributes (in memory)")
        uv_layout = QVBoxLayout(uv_box)
        uv_layout.setContentsMargins(5, 5, 5, 5)
        uv_layout.setSpacing(4)
        self.uv_editor = UVEditorWidget()
        self.uv_editor.uvChanged.connect(self._on_uv_changed)
        self.uv_editor.editFinished.connect(self._on_uv_edit_finished)
        self.uv_editor.pointSelected.connect(self._on_uv_point_selected)
        uv_layout.addWidget(self.uv_editor, 1)

        uv_grid = QGridLayout()
        uv_grid.setHorizontalSpacing(4)
        uv_grid.setVerticalSpacing(3)
        self.uv_point_label = QLabel("Point: -")
        uv_grid.addWidget(self.uv_point_label, 0, 0, 1, 2)
        self.uv_dirty_label = QLabel("")
        uv_grid.addWidget(self.uv_dirty_label, 0, 2, 1, 2)
        uv_grid.addWidget(QLabel("U (0-255):"), 1, 0)
        self.uv_u_spin = QSpinBox()
        self.uv_u_spin.setRange(0, 255)
        self.uv_u_spin.editingFinished.connect(self._apply_uv_spins)
        uv_grid.addWidget(self.uv_u_spin, 1, 1)
        uv_grid.addWidget(QLabel("V (0-255):"), 1, 2)
        self.uv_v_spin = QSpinBox()
        self.uv_v_spin.setRange(0, 255)
        self.uv_v_spin.editingFinished.connect(self._apply_uv_spins)
        uv_grid.addWidget(self.uv_v_spin, 1, 3)

        uv_grid.addWidget(QLabel("Color:"), 2, 0)
        self.atts_color_spin = QSpinBox()
        self.atts_color_spin.setRange(0, 255)
        self.atts_color_spin.setToolTip(
            "ATTS ColorVal: palette index used when the face is drawn "
            "without texture (material color fallback).")
        self.atts_color_spin.valueChanged.connect(
            lambda _value: self._apply_atts_spins())
        uv_grid.addWidget(self.atts_color_spin, 2, 1)
        uv_grid.addWidget(QLabel("Shade:"), 2, 2)
        self.atts_shade_spin = QSpinBox()
        self.atts_shade_spin.setRange(0, 255)
        self.atts_shade_spin.setToolTip(
            "ATTS ShadeVal: darkening, brightness = 1 - shade/256 "
            "(CONFIRMED, amesh.cpp).")
        self.atts_shade_spin.valueChanged.connect(
            lambda _value: self._apply_atts_spins())
        uv_grid.addWidget(self.atts_shade_spin, 2, 3)
        uv_grid.addWidget(QLabel("Tracy:"), 3, 0)
        self.atts_tracy_spin = QSpinBox()
        self.atts_tracy_spin.setRange(0, 255)
        self.atts_tracy_spin.setToolTip(
            "ATTS TracyVal: transparency mode of the face (0 = opaque; "
            "engine-specific modes - changing this is STRONG, not "
            "CONFIRMED, verify in game).")
        self.atts_tracy_spin.valueChanged.connect(
            lambda _value: self._apply_atts_spins())
        uv_grid.addWidget(self.atts_tracy_spin, 3, 1)
        uv_layout.addLayout(uv_grid)

        uv_buttons = QGridLayout()
        uv_buttons.setHorizontalSpacing(4)
        self.uv_revert_button = QPushButton("Revert polygon")
        self.uv_revert_button.clicked.connect(self._revert_uv_selected)
        uv_buttons.addWidget(self.uv_revert_button, 0, 0)
        self.uv_revert_all_button = QPushButton("Revert all edits")
        self.uv_revert_all_button.clicked.connect(self._revert_uv_all)
        uv_buttons.addWidget(self.uv_revert_all_button, 0, 1)
        self.uv_save_button = QPushButton("Save BASE As (edits)...")
        self.uv_save_button.clicked.connect(self._save_uv_edits_as)
        uv_buttons.addWidget(self.uv_save_button, 1, 0, 1, 2)
        uv_layout.addLayout(uv_buttons)
        poly_layout.addWidget(uv_box, 4)

        poly_layout.addWidget(QLabel("UV islands / repair preview:"))
        self.poly_uv_label.setMinimumHeight(120)
        poly_layout.addWidget(self.poly_uv_label, 1)
        poly_layout.addWidget(QLabel("Material blocks:"))
        poly_layout.addWidget(self.blocks_list, 1)

        # Keep the repair workflow, but move its dense controls out of the
        # always-visible inspector.  This restores vertical room to the UV map
        # and material previews without deleting any capability.
        self.repair_dialog_button = QPushButton("Repair unmapped polygon...")
        self.repair_dialog_button.clicked.connect(self._show_repair_dialog)
        poly_layout.addWidget(self.repair_dialog_button)

        repair_dialog = QDialog(self)
        repair_dialog.setWindowTitle("Repair unmapped polygon")
        repair_dialog.setWindowModality(Qt.WindowModality.NonModal)
        repair_dialog.setMinimumSize(480, 420)
        repair_dialog.resize(580, 520)
        repair_dialog_layout = QVBoxLayout(repair_dialog)
        repair_dialog_layout.setContentsMargins(8, 8, 8, 8)
        repair_dialog_layout.setSpacing(6)

        repair_box = QGroupBox("Repair unmapped polygon (Save As only)")
        repair_layout = QVBoxLayout(repair_box)
        repair_layout.setContentsMargins(6, 6, 6, 6)
        repair_layout.setSpacing(5)
        repair_grid = QGridLayout()
        repair_grid.setHorizontalSpacing(5)
        repair_grid.setVerticalSpacing(4)
        repair_grid.addWidget(QLabel("Target block:"), 0, 0)
        repair_grid.addWidget(self.repair_target_combo, 0, 1)
        repair_grid.addWidget(self.repair_copy_button, 1, 0)
        repair_grid.addWidget(self.repair_source_spin, 1, 1)
        repair_grid.addWidget(self.repair_planar_button, 2, 0, 1, 2)
        repair_layout.addLayout(repair_grid)
        repair_layout.addWidget(self.repair_preview, 1)
        repair_buttons = QGridLayout()
        repair_buttons.setHorizontalSpacing(5)
        repair_buttons.addWidget(self.repair_apply_button, 0, 0)
        repair_buttons.addWidget(self.repair_revert_button, 0, 1)
        repair_buttons.addWidget(self.repair_save_button, 1, 0, 1, 2)
        repair_layout.addLayout(repair_buttons)
        repair_dialog_layout.addWidget(repair_box, 1)
        repair_close_button = QPushButton("Close")
        repair_close_button.clicked.connect(repair_dialog.hide)
        repair_dialog_layout.addWidget(repair_close_button)
        self._repair_dialog = repair_dialog

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

        # Advanced keeps the technical panels and absorbs Animations and
        # Dependencies so the primary tab row stays short and readable.
        resolve_panel = QWidget()
        resolve_layout = QVBoxLayout(resolve_panel)
        resolve_layout.setContentsMargins(5, 5, 5, 5)
        resolve_layout.setSpacing(4)
        resolve_help = QLabel(
            "Resolve missing or ambiguous references. Session choices never "
            "modify the original asset.")
        resolve_help.setWordWrap(True)
        resolve_layout.addWidget(resolve_help)
        resolve_layout.addWidget(self.resolve_tree, 1)
        resolve_buttons = QGridLayout()
        resolve_buttons.setHorizontalSpacing(4)
        resolve_buttons.setVerticalSpacing(3)
        self.use_candidate_button = QPushButton("Trial-load")
        self.use_candidate_button.clicked.connect(self._use_selected_candidate)
        resolve_buttons.addWidget(self.use_candidate_button, 0, 0)
        self.keep_button = QPushButton("Keep for session")
        self.keep_button.clicked.connect(self._keep_for_session)
        resolve_buttons.addWidget(self.keep_button, 0, 1)
        self.unload_button = QPushButton("Unload / revert")
        self.unload_button.clicked.connect(self._clear_override)
        resolve_buttons.addWidget(self.unload_button, 1, 0)
        self.skip_button = QPushButton("Skip")
        self.skip_button.clicked.connect(self._skip_dependency)
        resolve_buttons.addWidget(self.skip_button, 1, 1)
        self.assign_manual_button = QPushButton("Assign file...")
        self.assign_manual_button.clicked.connect(self._assign_manual_file)
        resolve_buttons.addWidget(self.assign_manual_button, 2, 0)
        self.compare_candidate_button = QPushButton("Compare...")
        self.compare_candidate_button.clicked.connect(self._compare_candidate)
        resolve_buttons.addWidget(self.compare_candidate_button, 2, 1)
        self.reveal_button = QPushButton("Reveal in folder")
        self.reveal_button.clicked.connect(self._reveal_in_folder)
        resolve_buttons.addWidget(self.reveal_button, 3, 0)
        self.save_choice_button = QPushButton("Save choice")
        self.save_choice_button.clicked.connect(self._save_choice)
        resolve_buttons.addWidget(self.save_choice_button, 3, 1)
        self.apply_saved_button = QPushButton("Apply saved")
        self.apply_saved_button.clicked.connect(self._apply_saved_choice)
        resolve_buttons.addWidget(self.apply_saved_button, 4, 0)
        self.forget_choice_button = QPushButton("Forget saved")
        self.forget_choice_button.clicked.connect(self._forget_choice)
        resolve_buttons.addWidget(self.forget_choice_button, 4, 1)
        self.auto_apply_check = QCheckBox("Apply saved choices automatically")
        self.auto_apply_check.setChecked(self._profile.auto_apply)
        self.auto_apply_check.toggled.connect(self._toggle_auto_apply)
        resolve_buttons.addWidget(self.auto_apply_check, 5, 0, 1, 2)
        resolve_layout.addLayout(resolve_buttons)

        diff_panel = QWidget()
        diff_layout = QVBoxLayout(diff_panel)
        diff_layout.setContentsMargins(5, 5, 5, 5)
        diff_layout.setSpacing(4)
        diff_layout.addWidget(self.diff_label)
        diff_filter_row = QHBoxLayout()
        diff_filter_row.addWidget(QLabel("Filter:"))
        diff_filter_row.addWidget(self.diff_filter, 1)
        diff_layout.addLayout(diff_filter_row)
        diff_layout.addWidget(self.diff_tree, 3)
        diff_layout.addWidget(QLabel("Selected resource details:"))
        diff_layout.addWidget(self.diff_thumbs)
        diff_layout.addWidget(self.diff_details, 2)

        adv_tabs = QTabWidget()
        adv_tabs.setDocumentMode(True)
        adv_tabs.setUsesScrollButtons(True)
        adv_tabs.tabBar().setExpanding(False)
        adv_tabs.addTab(diff_panel, "Source Diff")
        adv_tabs.addTab(self.anim_tree, "Animations")
        adv_tabs.addTab(resolve_panel, "Dependencies")
        adv_tabs.addTab(self.refs_tree, "BASE References")
        adv_tabs.addTab(self.stats_tree, "Geometry Stats")
        # Chunk Tree is a raw parser/forensics view.  It remains populated in
        # memory for development, but is intentionally hidden from the normal
        # editor UI because it does not help ordinary asset editing and made
        # the Advanced section noisier than useful.
        self._adv_tabs = adv_tabs
        advanced_panel = QWidget()
        advanced_layout = QVBoxLayout(advanced_panel)
        advanced_layout.setContentsMargins(0, 0, 0, 0)
        advanced_layout.addWidget(adv_tabs)

        tabs.addTab(setbas_panel, "BAS")
        tabs.addTab(asset_panel, "Assets")
        tabs.addTab(textures_panel, "Textures")
        tabs.addTab(inspector_panel, "Inspector")
        tabs.addTab(poly_panel, "Poly Inspector")
        tabs.addTab(snapshot_panel, "Snapshot")
        tabs.addTab(advanced_panel, "Advanced")
        self._right_tabs = tabs
        self._setbas_panel = setbas_panel
        self._snapshot_panel = snapshot_panel
        tabs.currentChanged.connect(self._on_right_tab_changed)
        tabs.setCurrentWidget(setbas_panel)
        self._update_snapshot_color_button()
        self._sync_animation_controls()

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
        main_split.addWidget(tabs)
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
        diagnostics_tabs.addTab(self.log_list, "Log / Diff results")
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

    def _show_repair_dialog(self) -> None:
        """Open the full repair workbench without occupying inspector space."""

        if self._repair_dialog is None:
            return
        self._update_repair_buttons()
        self._repair_dialog.show()
        self._repair_dialog.raise_()
        self._repair_dialog.activateWindow()

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

    def open_base(self, path: str | Path) -> None:
        if not self._confirm_discard_geometry():
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
            self, "Pick texture files (.ilbm/.ilb) - optional",
            str(self._last_directory),
            "ILBM textures (*.ilbm *.ilb *.lbm *.iff *.ILBM *.ILB);;All files (*)",
        )
        anms, _ = QFileDialog.getOpenFileNames(
            self, "Pick animation files (.anm/.vanm) - optional",
            str(self._last_directory),
            "Animations (*.anm *.vanm *.ANM *.VANM);;All files (*)",
        )
        if not sklt and not base and not textures and not anms:
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
            self.open_base(self._family.base_path)
            # The archive is the file the user just opened, so keep its path
            # visible after the internal family refresh.
            self._set_document_title(archive.path)
        self._preview_first_setbas_skeleton()

    def _raise_setbas_tab(self) -> None:
        """Bring the primary BAS panel to the front after loading it."""

        right_tabs = getattr(self, "_right_tabs", None)
        panel = getattr(self, "_setbas_panel", None)
        if right_tabs is not None and panel is not None:
            right_tabs.setCurrentWidget(panel)

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

    def _preview_first_setbas_skeleton(self) -> None:
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
                    self._preview_setbas_skeleton()
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

    def _preview_setbas_skeleton(self) -> None:
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
        if not self._confirm_discard_geometry():
            return
        if self._preview_setbas_textured(resource):
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
                message + "\n\nSee Diagnostics > Log / Diff results.")
        else:
            QMessageBox.information(
                self, "ILBM conversion complete",
                message + f"\n\nOutput: {output_folder}")

    # -- source diff (read-only comparison) --------------------------------------

    def run_source_diff(self) -> None:
        if self._family is None:
            QMessageBox.information(self, "No asset loaded",
                                    "Load a .base family first.")
            return
        if self._setbas is None:
            QMessageBox.information(
                self, "No SET.BAS provider",
                "Open a SET.BAS archive first ('Open SET.BAS...') to compare "
                "the loose family against the embedded game resources.",
            )
            return
        self._diff = diff_family(self._family)
        self._fill_diff()
        self.statusBar().showMessage(
            f"Source diff: {self._diff.summary_line()}"
        )

    def _diff_filter_keep(self, entry) -> bool:
        mode = self.diff_filter.currentIndex()
        if mode == 1:
            return entry.is_difference
        if mode == 2:
            return entry.is_missing
        if mode == 3:
            return entry.status == "decode failed"
        if mode == 4:
            return entry.kind == "mapping" and entry.status == "warning"
        return True

    def _fill_diff(self) -> None:
        self.diff_tree.clear()
        self.diff_details.clear()
        if self._diff is None:
            return
        header = (f"Base: {self._diff.base_path}  |  "
                  f"SET.BAS: {self._diff.setbas_path}\n"
                  f"Result: {self._diff.summary_line()}")
        for warning in self._diff.warnings:
            header += f"\nWARNING: {warning}"
        self.diff_label.setText(header)

        status_color = {
            "identical": QColor(90, 200, 110),
            "different": QColor(255, 190, 70),
            "count mismatch": QColor(240, 90, 90),
            "warning": QColor(255, 190, 70),
            "missing loose": QColor(120, 210, 210),
            "missing embedded": QColor(180, 180, 180),
            "decode failed": QColor(200, 90, 200),
        }
        for index, entry in enumerate(self._diff.entries):
            if not self._diff_filter_keep(entry):
                continue
            item = QTreeWidgetItem(
                [entry.name, entry.kind, entry.status, entry.summary]
            )
            pix = QPixmap(12, 12)
            pix.fill(status_color.get(entry.status, QColor(150, 150, 150)))
            item.setIcon(0, pix)
            item.setData(0, Qt.ItemDataRole.UserRole, index)
            self.diff_tree.addTopLevelItem(item)
        for column in range(4):
            self.diff_tree.resizeColumnToContents(column)

    def _show_diff_details(self, current, _previous=None) -> None:
        self.diff_details.clear()
        self.diff_thumbs.setVisible(False)
        if current is None or self._diff is None:
            return
        index = current.data(0, Qt.ItemDataRole.UserRole)
        if index is None:
            return
        entry = self._diff.entries[index]
        self.diff_details.addItem(f"loose: {entry.loose}")
        self.diff_details.addItem(f"SET.BAS: {entry.embedded}")
        if entry.visual:
            self.diff_details.addItem(f"visual classification: {entry.visual}")
        for key, value in (entry.metrics or {}).items():
            self.diff_details.addItem(f"{key}: {value}")
        for detail in entry.details:
            self.diff_details.addItem(detail)
        if not entry.details:
            self.diff_details.addItem("(no differences)")
        self._show_diff_thumbnails(entry)

    def _show_diff_thumbnails(self, entry) -> None:
        if not entry.loose_rgba or not entry.embedded_rgba \
                or entry.thumb_size == (0, 0):
            return
        width, height = entry.thumb_size
        thumb = 128
        gap = 10
        panels = [("loose / dev", entry.loose_rgba),
                  ("SET.BAS / release", entry.embedded_rgba)]
        if entry.diff_rgba:
            panels.append(("RGB diff heatmap", entry.diff_rgba))

        total_w = len(panels) * thumb + (len(panels) - 1) * gap
        caption_h = 16
        canvas = QPixmap(total_w, thumb + caption_h)
        canvas.fill(QColor(30, 32, 38))
        painter = QPainter(canvas)
        x = 0
        for caption, rgba in panels:
            qimage = QImage(rgba, width, height, width * 4,
                            QImage.Format.Format_RGBA8888)
            scaled = qimage.scaled(
                thumb, thumb, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )
            # checkerboard behind alpha
            cell = 8
            for cy in range(0, scaled.height(), cell):
                for cx in range(0, scaled.width(), cell):
                    light = ((cx // cell) + (cy // cell)) % 2 == 0
                    painter.fillRect(x + cx, cy, cell, cell,
                                     QColor(190, 190, 190) if light
                                     else QColor(130, 130, 130))
            painter.drawImage(x, 0, scaled)
            painter.setPen(QColor(220, 220, 220))
            painter.drawText(x, thumb + caption_h - 4, caption)
            x += thumb + gap
        painter.end()
        self.diff_thumbs.setPixmap(canvas)
        self.diff_thumbs.setVisible(True)

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
        text = (family_to_markdown(self._family, self._diff, workbench)
                if Path(path).suffix.lower() != ".json"
                else family_to_json(self._family, self._diff, workbench))
        try:
            Path(path).write_text(text, encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "Export failed", str(exc))
            return
        self.statusBar().showMessage(f"Report written to {path}")

    def _toggle_play(self, playing: bool) -> None:
        self.viewport.play_animation(playing)
        self.play_button.setText("Pause" if playing else "Play")

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

    def _on_right_tab_changed(self, index: int) -> None:
        self._sync_animation_controls()
        tabs = getattr(self, "_right_tabs", None)
        snapshot_panel = getattr(self, "_snapshot_panel", None)
        entering = (tabs is not None and snapshot_panel is not None
                    and tabs.widget(index) is snapshot_panel)
        if entering:
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
            self.edit_mode_combo.setEnabled(False)
            self.toolbar_view_preset_combo.setEnabled(False)
            for action in (self.sen_check, self.wire_check, self.axes_check,
                           self.grid_check, self.overlay_check,
                           self.mapping_diag_check):
                action.setEnabled(False)
            self._on_snapshot_preset_changed(
                self.snapshot_view_combo.currentText())
        else:
            self._snapshot_mode_active = False
            restored_mode = self.viewport.end_snapshot_mode()
            restored_index = self.mode_combo.findData(restored_mode)
            if restored_index >= 0:
                self.mode_combo.blockSignals(True)
                self.mode_combo.setCurrentIndex(restored_index)
                self.mode_combo.blockSignals(False)
            self.mode_combo.setEnabled(True)
            self.edit_mode_combo.setEnabled(True)
            self.toolbar_view_preset_combo.setEnabled(True)
            for action in (self.sen_check, self.wire_check, self.axes_check,
                           self.grid_check, self.overlay_check,
                           self.mapping_diag_check):
                action.setEnabled(True)

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
        self._selected_owner = owner
        self.viewport.set_selected_owner(owner)
        # The polygon workbench (picking / inspector / UV editor) follows the
        # selected object so children of huge families are editable too.
        if owner is not None and self._family is not None:
            obj = self._owner_to_obj.get(owner)
            if obj is not None and obj.skeleton is not None \
                    and obj is not self._workbench_obj:
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
        n_dirty = len(self._uv_original) + len(self._atts_original)
        dirty = (f" | <b>UNSAVED EDITS: {n_dirty}</b>" if n_dirty else "")
        summary = (f"<b>{status}</b> | selected: {selected} | "
                   f"selected + children: {scope}{large}{dirty}")
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
                                  rgb=True, keep_rgba=True)
                    self._log(
                        f"compare {Path(candidate).parent.name}/"
                        f"{Path(candidate).name} vs SET.BAS embedded: "
                        f"{entry.visual or entry.status} - {entry.summary} "
                        f"{entry.metrics or ''}"
                    )
                    self._show_compare_thumbs(entry)
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

    def _show_compare_thumbs(self, entry) -> None:
        """Reuse the Source Diff thumbnail panel for candidate comparisons."""

        if entry.loose_rgba and entry.thumb_size != (0, 0):
            self._show_diff_thumbnails(entry)

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
        header = QTreeWidgetItem([
            f"search root: {family.search_root or '-'}",
            f"{len(family.dependencies)} dependencies", "",
        ])
        self.resolve_tree.addTopLevelItem(header)

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

    # -- Polygon Mapping Workbench -------------------------------------------------

    def _set_mapping_diagnostics(self, enabled: bool) -> None:
        self.viewport.set_mapping_diagnostics(enabled)
        if enabled and self._mapping_index is not None:
            unmapped = self._mapping_index.unmapped
            duplicates = self._mapping_index.duplicates
            self.statusBar().showMessage(
                f"Mapping diagnostics: {len(unmapped)} unmapped "
                f"{unmapped if unmapped else ''}, "
                f"{len(duplicates)} duplicate-mapped, "
                f"{len(self._mapping_index.invalid)} invalid"
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
            rng = f"{min(ids)}..{max(ids)}" if ids else "-"
            ref = (family.texture_refs.get(tex)
                   or family.animation_refs.get(tex))
            source = ref.source if ref and ref.source else "?"
            item = QListWidgetItem(
                f"#{index} {tex} [{source}] - {len(ids)} faces, polyID {rng}, "
                f"{block.describe_polflags()}"
            )
            item.setData(Qt.ItemDataRole.UserRole, index)
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

    def _on_polygon_picked(self, poly_id: int) -> None:
        self._selected_poly = poly_id
        self._repair_plan = None
        self.repair_preview.clear()
        self._fill_polygon_inspector(poly_id)
        self._update_repair_buttons()
        if self._mapping_index is not None:
            status = self._mapping_index.status(poly_id)
            self.statusBar().showMessage(
                f"Selected polygon #{poly_id}: {status}"
            )

    def _fill_polygon_inspector(self, poly_id: int | None) -> None:
        self._update_uv_editor(poly_id)
        self.poly_info.clear()
        self.poly_uv_label.clear()
        self.poly_uv_label.setText("Select a polygon in the viewport.")
        if poly_id is None or self._workbench_obj is None \
                or self._workbench_obj.skeleton is None:
            return
        family = self._family
        obj = self._workbench_obj
        skeleton = obj.skeleton
        if not (0 <= poly_id < len(skeleton.polygons)):
            self.poly_info.addItem(f"polygon #{poly_id}: out of range")
            return

        polygon = skeleton.polygons[poly_id]
        points = [skeleton.points[i] for i in polygon]
        info = self.poly_info
        info.addItem(f"polyID: {poly_id}")
        info.addItem(f"skeleton: {obj.base_object.skeleton_name}")
        info.addItem(f"vertices: {len(polygon)}  indices: {list(polygon)}")
        for vi, (index, p) in enumerate(zip(polygon, points)):
            info.addItem(f"  v{vi} [POO2 {index}]: "
                         f"({p[0]:.1f}, {p[1]:.1f}, {p[2]:.1f})")
        normal = _polygon_normal(points)
        if normal:
            info.addItem(f"normal: ({normal[0]:.3f}, {normal[1]:.3f}, "
                         f"{normal[2]:.3f})")

        status = self._mapping_index.status(poly_id)
        info.addItem(f"mapping status: {status.upper()}")
        if status == "unmapped":
            info.addItem("This polygon exists in POL2 but has no ATTS "
                         "material entry and will be invisible in-game.")
            return

        for ref in self._mapping_index.refs.get(poly_id, []):
            block = ref.block
            entry = block.atts[ref.atts_index]
            tex = block.texture.name if block.texture else "-"
            tex_ref = (family.texture_refs.get(tex)
                       or family.animation_refs.get(tex))
            info.addItem(f"material block #{ref.block_index} "
                         f"({block.class_id}), ATTS entry #{ref.atts_index}")
            info.addItem(f"  texture: {tex} "
                         f"[{tex_ref.source if tex_ref and tex_ref.source else '?'}] "
                         f"{tex_ref.display_path if tex_ref else ''}")
            info.addItem(f"  shadeVal={entry.shade_val} "
                         f"colorVal={entry.color_val} "
                         f"tracyVal={entry.tracy_val} "
                         f"tracy mode={block.tracy_mode}"
                         + (f" tracy tex={block.tracy_texture.name}"
                            if block.tracy_texture else ""))
            info.addItem(f"  polflags: {block.describe_polflags()}")
            uvs = (block.olpl[ref.atts_index]
                   if ref.atts_index < len(block.olpl) else [])
            info.addItem(f"  OLPL UVs: "
                         + (" ".join(f"({u},{v})" for u, v in uvs) or "(none)"))
            self._draw_uv_overlay(tex, uvs, poly_id)

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
        size = 192
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

    # -- Mini UV Editor V1 ---------------------------------------------------------

    def _on_edit_mode_changed(self, index: int) -> None:
        mode = self.edit_mode_combo.itemData(index)
        if mode == "edit":
            if not self.viewport.enter_edit_mode(self._selected_owner):
                self.edit_mode_combo.blockSignals(True)
                self.edit_mode_combo.setCurrentIndex(0)
                self.edit_mode_combo.blockSignals(False)
            return
        if self.viewport.is_edit_mode:
            self.viewport.exit_edit_mode()

    def _on_edit_mode_toggled(self, active: bool) -> None:
        desired = 1 if active else 0
        if self.edit_mode_combo.currentIndex() != desired:
            self.edit_mode_combo.blockSignals(True)
            self.edit_mode_combo.setCurrentIndex(desired)
            self.edit_mode_combo.blockSignals(False)

    def _on_geometry_edited(self, owner: str) -> None:
        fam_obj = self._owner_to_obj.get(owner)
        if fam_obj is not None and getattr(fam_obj, "skeleton", None) \
                is not None:
            self._geom_dirty[owner] = fam_obj
        self.save_skeleton_action.setEnabled(bool(self._geom_dirty))

    def _confirm_discard_geometry(self) -> bool:
        """True when it is safe to drop unsaved vertex edits."""

        if not self._geom_dirty:
            return True
        owners = ", ".join(sorted(self._geom_dirty))
        answer = QMessageBox.question(
            self, "Unsaved geometry edits",
            f"Vertex edits on [{owners}] were not saved.\n"
            "Reloading discards them. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _save_skeleton_as(self) -> None:
        for owner, fam_obj in list(self._geom_dirty.items()):
            model = getattr(fam_obj, "skeleton", None)
            if model is None:
                del self._geom_dirty[owner]
                continue
            ref = getattr(fam_obj, "skeleton_ref", None)
            source = (Path(ref.path) if ref is not None
                      and getattr(ref, "path", None) is not None else None)
            if source is not None:
                suggested = source.with_name(
                    f"{source.stem}.edit{source.suffix}")
            else:
                # SET.BAS-embedded skeleton: save as a standalone .skl copy.
                name = Path((model.source_name or "skeleton")
                            .replace("SET.BAS:", "")
                            .replace("\\", "/")).name or "skeleton.skl"
                base_dir = (self._family.base_path.parent
                            if self._family and self._family.base_path
                            else self._last_directory)
                suggested = Path(base_dir) / f"{Path(name).stem}.edit.skl"
            path, _ = QFileDialog.getSaveFileName(
                self, f"Save edited skeleton [{owner}] as",
                str(suggested),
                "Urban Assault skeleton (*.skl *.sklt);;All files (*)",
            )
            if not path:
                continue
            target = Path(path)
            try:
                if source is not None and target.exists() \
                        and target.resolve() == source.resolve():
                    backup = source.with_suffix(source.suffix + ".bak")
                    shutil.copy2(source, backup)
                    self._log(f"backup written: {backup.name}")
                save_sklt_with_poo2_points(model, model.points, target)
                verify = parse_sklt_file(target)
                matches = (
                    len(verify.points) == len(model.points)
                    and all(
                        abs(a[i] - b[i]) <= 1e-3 + abs(b[i]) * 1e-5
                        for a, b in zip(verify.points, model.points)
                        for i in range(3)
                    )
                )
                if not matches:
                    raise SkltParseError(
                        "verification re-parse does not match the edited "
                        "points; the written file may be invalid"
                    )
            except (SkltParseError, OSError) as exc:
                QMessageBox.critical(self, "Save failed", str(exc))
                continue
            del self._geom_dirty[owner]
            self._log(f"saved edited skeleton [{owner}] -> {target}")
            self.statusBar().showMessage(
                f"Edited skeleton saved and verified: {target.name}", 8000)
        self.save_skeleton_action.setEnabled(bool(self._geom_dirty))

    def _update_uv_editor(self, poly_id: int | None) -> None:
        self._uv_ctx = None
        if poly_id is None or self._workbench_obj is None \
                or self._mapping_index is None:
            self.uv_editor.set_data(None, [], False,
                                    "Select a mapped polygon to edit its UVs.")
            self._sync_uv_fields()
            return
        status = self._mapping_index.status(poly_id)
        if status == "unmapped":
            self.uv_editor.set_data(
                None, [], False,
                "Polygon has no OLPL mapping yet. Use the repair panel "
                "below (planar / copy style) first."
            )
            self._sync_uv_fields()
            return
        refs = self._mapping_index.refs.get(poly_id, [])
        if not refs:
            self.uv_editor.set_data(None, [], False,
                                    f"polygon #{poly_id}: {status}")
            self._sync_uv_fields()
            return
        ref = refs[0]
        block = ref.block
        uvs = (block.olpl[ref.atts_index]
               if ref.atts_index < len(block.olpl) else [])
        tex_name = block.texture.name if block.texture else ""
        image = self._texture_qimage(tex_name) if tex_name else None

        editable = bool(uvs)
        notes = []
        if (block.class_id or "").lower() != "amesh.class":
            editable = False
        if image is None and tex_name:
            tex_ref = (self._family.texture_refs.get(tex_name)
                       or self._family.animation_refs.get(tex_name))
            if tex_ref is not None and tex_ref.status == "ambiguous":
                editable = False
                notes.append(f"Texture not loaded ({tex_name} is ambiguous). "
                             "Resolve or trial-load the dependency first.")
            else:
                editable = False
                notes.append(f"Texture not loaded ({tex_name}). Resolve the "
                             "dependency first.")
        if block.texture is not None and block.texture.kind == "bmpanim" \
                and editable:
            notes.append("Animated material: the 3D preview uses the VANM "
                         "frame UVs; you are editing the on-disk OLPL group.")

        self._uv_ctx = (self._workbench_obj, block, ref.block_index,
                        ref.atts_index, poly_id)
        self.uv_editor.set_data(image, uvs, editable, " ".join(notes))
        self._sync_uv_fields()

    def _uv_key(self) -> tuple | None:
        if self._uv_ctx is None:
            return None
        fam_obj, _block, block_index, atts_index, _poly = self._uv_ctx
        return (fam_obj.owner_path, block_index, atts_index)

    def _sync_uv_fields(self) -> None:
        uvs = self.uv_editor.uvs()
        index = self.uv_editor.active_point()
        enabled = 0 <= index < len(uvs)
        self.uv_point_label.setText(f"Point: {index if enabled else '-'}"
                                    f"/{len(uvs)}")
        self.uv_u_spin.blockSignals(True)
        self.uv_v_spin.blockSignals(True)
        if enabled:
            self.uv_u_spin.setValue(uvs[index][0])
            self.uv_v_spin.setValue(uvs[index][1])
        self.uv_u_spin.setEnabled(enabled)
        self.uv_v_spin.setEnabled(enabled)
        self.uv_u_spin.blockSignals(False)
        self.uv_v_spin.blockSignals(False)
        dirty = len(self._uv_original) + len(self._atts_original)
        self.uv_dirty_label.setText(
            f"UNSAVED EDITS: {dirty}" if dirty else ""
        )
        key = self._uv_key()
        self.uv_revert_button.setEnabled(key in self._uv_original
                                         or key in self._atts_original)
        self.uv_revert_all_button.setEnabled(dirty > 0)
        self.uv_save_button.setEnabled(dirty > 0)
        self._sync_atts_fields()

    def _atts_entry(self):
        """The on-disk ATTS entry behind the selected polygon, or None if
        the block has no editable ATTS chunk (area.class is synthesized)."""

        if self._uv_ctx is None:
            return None
        _fam_obj, block, _bi, atts_index, _poly = self._uv_ctx
        if (block.class_id or "").lower() != "amesh.class":
            return None
        if block.atts_chunk_offset < 0 or atts_index >= len(block.atts):
            return None
        return block.atts[atts_index]

    def _sync_atts_fields(self) -> None:
        entry = self._atts_entry()
        enabled = entry is not None
        for spin in (self.atts_color_spin, self.atts_shade_spin,
                     self.atts_tracy_spin):
            spin.blockSignals(True)
            spin.setEnabled(enabled)
        if entry is not None:
            self.atts_color_spin.setValue(entry.color_val)
            self.atts_shade_spin.setValue(entry.shade_val)
            self.atts_tracy_spin.setValue(entry.tracy_val)
        for spin in (self.atts_color_spin, self.atts_shade_spin,
                     self.atts_tracy_spin):
            spin.blockSignals(False)

    def _apply_atts_spins(self) -> None:
        entry = self._atts_entry()
        if entry is None or self._uv_ctx is None:
            return
        new = (self.atts_color_spin.value(), self.atts_shade_spin.value(),
               self.atts_tracy_spin.value())
        if new == (entry.color_val, entry.shade_val, entry.tracy_val):
            return
        fam_obj, _block, block_index, _atts_index, poly_id = self._uv_ctx
        key = self._uv_key()
        if key not in self._atts_original:
            self._atts_original[key] = (entry.color_val, entry.shade_val,
                                        entry.tracy_val)
        entry.color_val, entry.shade_val, entry.tracy_val = new
        if block_index < len(fam_obj.materials):
            group = fam_obj.materials[block_index]
            for i, (pid, uvs, _shade) in enumerate(group.faces):
                if pid == poly_id:
                    group.faces[i] = (pid, uvs, entry.shade_val)
                    break
        if self._atts_original.get(key) == new:
            del self._atts_original[key]
        self._on_uv_edit_finished()
        self._sync_uv_fields()

    def _on_uv_point_selected(self, _index: int) -> None:
        self._sync_uv_fields()

    def _on_uv_changed(self, uvs: list) -> None:
        """Live during drag: update the in-memory mapping structures."""

        if self._uv_ctx is None:
            return
        fam_obj, block, block_index, atts_index, poly_id = self._uv_ctx
        key = self._uv_key()
        if key not in self._uv_original:
            self._uv_original[key] = [tuple(uv) for uv in
                                      block.olpl[atts_index]]
        block.olpl[atts_index] = [tuple(uv) for uv in uvs]
        if block_index < len(fam_obj.materials):
            group = fam_obj.materials[block_index]
            for i, (pid, _old, shade) in enumerate(group.faces):
                if pid == poly_id:
                    group.faces[i] = (pid, [tuple(uv) for uv in uvs], shade)
                    break
        self._sync_uv_fields()

    def _on_uv_edit_finished(self) -> None:
        """Drag released: refresh the 3D textured preview in memory."""

        if self._uv_ctx is None or self._family is None:
            return
        poly_id = self._uv_ctx[4]
        self.viewport.set_visible_owners(self.viewport.visible_owners())
        self._sync_animation_controls()
        self.viewport.set_selected_polygon(poly_id)
        self._update_banner()

    def _apply_uv_spins(self) -> None:
        index = self.uv_editor.active_point()
        if index < 0:
            return
        self.uv_editor.set_point(index, self.uv_u_spin.value(),
                                 self.uv_v_spin.value())
        self._on_uv_edit_finished()

    def _restore_uv(self, key: tuple, uvs: list) -> None:
        owner, block_index, atts_index = key
        fam_obj = self._owner_to_obj.get(owner)
        if fam_obj is None:
            return
        block = fam_obj.base_object.ades[block_index]
        block.olpl[atts_index] = [tuple(uv) for uv in uvs]
        entry = block.atts[atts_index] if atts_index < len(block.atts) else None
        if block_index < len(fam_obj.materials) and entry is not None:
            group = fam_obj.materials[block_index]
            for i, (pid, _old, shade) in enumerate(group.faces):
                if pid == entry.poly_id:
                    group.faces[i] = (pid, [tuple(uv) for uv in uvs], shade)
                    break

    def _restore_atts(self, key: tuple, values: tuple) -> None:
        owner, block_index, atts_index = key
        fam_obj = self._owner_to_obj.get(owner)
        if fam_obj is None:
            return
        block = fam_obj.base_object.ades[block_index]
        if atts_index >= len(block.atts):
            return
        entry = block.atts[atts_index]
        entry.color_val, entry.shade_val, entry.tracy_val = values
        if block_index < len(fam_obj.materials):
            group = fam_obj.materials[block_index]
            for i, (pid, uvs, _shade) in enumerate(group.faces):
                if pid == entry.poly_id:
                    group.faces[i] = (pid, uvs, entry.shade_val)
                    break

    def _revert_uv_selected(self) -> None:
        key = self._uv_key()
        if key is None:
            return
        changed = False
        if key in self._uv_original:
            self._restore_uv(key, self._uv_original.pop(key))
            changed = True
        if key in self._atts_original:
            self._restore_atts(key, self._atts_original.pop(key))
            changed = True
        if not changed:
            return
        self._update_uv_editor(self._uv_ctx[4] if self._uv_ctx else None)
        self._on_uv_edit_finished()
        self.statusBar().showMessage("Polygon edits reverted (in memory).")

    def _revert_uv_all(self) -> None:
        for key, uvs in list(self._uv_original.items()):
            self._restore_uv(key, uvs)
        self._uv_original.clear()
        for key, values in list(self._atts_original.items()):
            self._restore_atts(key, values)
        self._atts_original.clear()
        self._update_uv_editor(self._uv_ctx[4] if self._uv_ctx else None)
        self._on_uv_edit_finished()
        self.statusBar().showMessage("All unsaved edits reverted.")

    def _save_uv_edits_as(self) -> None:
        if (not self._uv_original and not self._atts_original) \
                or self._family is None or self._family.base_path is None:
            return
        from base_mapping_editor import (AttsValueEdit, UVEdit,
                                         save_family_edits)

        edits = []
        for (owner, block_index, atts_index) in self._uv_original:
            fam_obj = self._owner_to_obj.get(owner)
            if fam_obj is None:
                continue
            edits.append(UVEdit(
                owner_path=owner, block_index=block_index,
                atts_index=atts_index,
                uvs=list(fam_obj.base_object.ades[block_index]
                         .olpl[atts_index]),
            ))
        atts_edits = []
        for (owner, block_index, atts_index) in self._atts_original:
            fam_obj = self._owner_to_obj.get(owner)
            if fam_obj is None:
                continue
            entry = fam_obj.base_object.ades[block_index].atts[atts_index]
            atts_edits.append(AttsValueEdit(
                owner_path=owner, block_index=block_index,
                atts_index=atts_index, color_val=entry.color_val,
                shade_val=entry.shade_val, tracy_val=entry.tracy_val,
            ))
        source = self._family.base_path
        suggested = source.with_name(f"{source.stem}.edit{source.suffix}")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save edited BASE as (never the original; SET.BAS is "
                  "never modified)",
            str(suggested),
            "Urban Assault BASE (*.base *.bas);;All files (*)",
        )
        if not path:
            return
        try:
            notes = save_family_edits(self._family, edits, atts_edits, path)
        except MappingEditError as exc:
            QMessageBox.critical(self, "Save failed - nothing written",
                                 str(exc))
            return
        self._log("Save As (edits): " + "; ".join(notes[-2:]))
        QMessageBox.information(self, "Edited BASE saved",
                                "\n".join(notes))
        self._uv_original.clear()
        self._atts_original.clear()
        for root in (source.parent, source.parent.parent):
            if root not in self._extra_roots:
                self._extra_roots.append(root)
        self.open_base(path)

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
            f"Repair applied in memory: polygon #{plan.poly_id} -> block "
            f"#{plan.block_index} ({len(self._pending_repairs)} pending, "
            "not saved). Use Save As... to write a new .base copy."
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
            self, "Save repaired BASE as (never the original)",
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
        QMessageBox.information(self, "Repaired BASE saved",
                                "\n".join(notes))
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
        self._family = family
        self._diff = None
        self.diff_tree.clear()
        self.diff_details.clear()
        self.diff_label.setText(
            "Family reloaded: run 'Compare with SET.BAS source...' to refresh "
            "the diff." if self._setbas else
            "Open a SET.BAS provider, then use 'Compare with SET.BAS "
            "source...'."
        )
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
        self._fill_refs(family)
        self._fill_stats(family)
        self._fill_textures(family)
        self._fill_resolve(family)
        self._fill_animations(family)
        self._fill_chunks(family)
        self._fill_checks(family)
        self._pending_repairs = []
        self._uv_original = {}
        self._atts_original = {}
        self._uv_ctx = None
        self._geom_dirty = {}
        self.save_skeleton_action.setEnabled(False)
        self._rebuild_workbench(family, self._selected_owner)
        self.viewport.set_mapping_diagnostics(
            self.mapping_diag_check.isChecked())
        self._apply_diagnostics_filter()

        self._sync_animation_controls()

        self._update_banner()
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
        extra = [n for n in family.texture_refs
                 if not any(n == d.raw_ref and d.kind != "anm_bitmap"
                            for d in family.dependencies)]
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

    def _tree_owner_for_item(self, item) -> str | None:
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data:
            return None
        kind, payload = data
        if kind in ("skeleton", "child") and payload is not None:
            return getattr(payload, "owner_path", None)
        if kind == "base" and self._family and self._family.root_object:
            return self._family.root_object.owner_path
        return None

    def _on_tree_double_clicked(self, item, _column=0) -> None:
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

    def _on_tree_node_selected(self, current, _previous=None) -> None:
        self.node_inspector.clear()
        if current is None or self._family is None:
            return
        data = current.data(0, Qt.ItemDataRole.UserRole)
        if not data:
            return
        owner = self._tree_owner_for_item(current)
        if owner:
            self._select_owner(owner)
        kind, payload = data
        family = self._family
        info = self.node_inspector

        if kind == "base":
            info.addItem("BASE prefab")
            info.addItem(f"path: {family.base_path}")
            info.addItem(f"search root: {family.search_root}")
            objects = family.all_objects()
            info.addItem(f"objects: {len(objects)} "
                         f"(children: {max(0, len(objects) - 1)})")
            from base_dependency_resolver import summarize
            info.addItem("dependencies: " + ", ".join(
                f"{v} {k}" for k, v in summarize(family.dependencies).items()))
            if self._mapping_index:
                info.addItem(f"mapping: {self._mapping_index.poly_count} "
                             f"polygons, unmapped "
                             f"{self._mapping_index.unmapped or 'none'}")
            info.addItem(f"warnings: {len(family.warnings)}")
        elif kind in ("skeleton", "child"):
            fam_obj = payload
            skeleton = fam_obj.skeleton
            info.addItem("skeleton" if kind == "skeleton"
                         else "child BASE object (KIDS)")
            info.addItem(f"reference: {fam_obj.base_object.skeleton_name}")
            if fam_obj.skeleton_ref:
                info.addItem(f"source: {fam_obj.skeleton_ref.source or '?'} "
                             f"-> {fam_obj.skeleton_ref.display_path}")
            if skeleton:
                info.addItem(f"vertices: {len(skeleton.points)}")
                info.addItem(f"polygons: {skeleton.parsed_polygon_count}")
                info.addItem(f"SEN2 points: {len(skeleton.sensors)}")
                mapped = {p for g in fam_obj.materials for p, _u, _s in g.faces}
                info.addItem(f"mapped polygons: {len(mapped)}/"
                             f"{skeleton.parsed_polygon_count}")
            else:
                info.addItem("skeleton not loaded")
            transform = fam_obj.base_object.transform
            if transform:
                info.addItem(f"STRC: pos={transform.position} "
                             f"scale={transform.scale} "
                             f"euler(deg)={transform.euler}")
        elif kind == "texture":
            name = payload
            ref = family.texture_refs.get(name)
            img = family.textures.get(name)
            info.addItem("[READ-ONLY] texture")
            info.addItem(f"reference: {name}")
            status = self._effective_status(
                name, next((d.status for d in family.dependencies
                            if d.raw_ref == name), "?"))
            info.addItem(f"status: {status}")
            if ref:
                info.addItem(f"source: {ref.source or '-'} "
                             f"-> {ref.display_path}")
                if ref.candidates:
                    info.addItem(f"candidates: {len(ref.candidates)}")
            saved = self._saved_choice_for(name)
            if saved:
                info.addItem(f"saved choice: {saved.chosen_path}"
                             + (" [STALE]" if saved.stale else ""))
            if img:
                info.addItem(f"format: {img.kind} {img.width}x{img.height}, "
                             f"{img.n_planes} planes, "
                             f"{'ByteRun1' if img.compression else 'raw'}")
                info.addItem("palette: "
                             + ("own CMAP" if img.palette else
                                ("external PAL" if family.external_palette
                                 else "grayscale fallback")))
        elif kind == "animation":
            name = payload
            anm = family.animations.get(name)
            info.addItem("[READ-ONLY] animation (bmpanim/VANM)")
            info.addItem(f"reference: {name}")
            if anm:
                info.addItem(f"bitmaps: {anm.bitmap_names}")
                info.addItem(f"frames: {len(anm.frames)} "
                             f"(~{anm.total_duration_ms:.0f} ms/cycle)")
                info.addItem("playback: supported "
                             "(Play/Step/Speed in the toolbar)")
            else:
                info.addItem("not loaded / unsupported")
        else:
            info.addItem("(group node)")

    def _fill_refs(self, family: AssetFamily) -> None:
        self.refs_tree.clear()
        if family.base_path:
            self.refs_tree.addTopLevelItem(
                QTreeWidgetItem(["base file", str(family.base_path)])
            )
        for root in family.search_roots:
            self.refs_tree.addTopLevelItem(QTreeWidgetItem(["search root", root]))
        for fam_obj in family.all_objects():
            obj = fam_obj.base_object
            if obj.skeleton_name:
                resolved = (fam_obj.skeleton_ref.display_path
                            if fam_obj.skeleton_ref
                            and fam_obj.skeleton_ref.found else "NOT FOUND")
                self.refs_tree.addTopLevelItem(
                    QTreeWidgetItem([f"skeleton: {obj.skeleton_name}", resolved])
                )
            if obj.transform:
                t = obj.transform
                self.refs_tree.addTopLevelItem(QTreeWidgetItem([
                    "transform (STRC)",
                    f"pos={t.position} scale={t.scale} euler(deg)={t.euler} "
                    f"visLimit={t.vis_limit} ambient={t.ambient_light}",
                ]))
            for i, block in enumerate(obj.ades):
                label = block.texture.name if block.texture else f"block {i}"
                self.refs_tree.addTopLevelItem(QTreeWidgetItem([
                    f"material block: {label}",
                    f"ATTS={len(block.atts)} OLPL={len(block.olpl)} "
                    f"{block.describe_polflags()}",
                ]))
            for res in obj.embedded:
                self.refs_tree.addTopLevelItem(QTreeWidgetItem([
                    f"embedded (EMRS): {res.resource_name}",
                    f"{res.class_id} {res.payload_form_type or res.payload_tag}",
                ]))
        if family.external_palette_path:
            self.refs_tree.addTopLevelItem(QTreeWidgetItem(
                ["external palette", str(family.external_palette_path)]
            ))
        self.refs_tree.resizeColumnToContents(0)

    def _fill_stats(self, family: AssetFamily) -> None:
        self.stats_tree.clear()
        for fam_obj in family.all_objects():
            skeleton = fam_obj.skeleton
            name = fam_obj.base_object.skeleton_name or "(object)"
            item = QTreeWidgetItem([name, ""])
            self.stats_tree.addTopLevelItem(item)
            if skeleton is None:
                item.setText(1, "skeleton not loaded")
                continue
            hist: dict[int, int] = {}
            for polygon in skeleton.polygons:
                hist[len(polygon)] = hist.get(len(polygon), 0) + 1
            xs = [p[0] for p in skeleton.points] or [0]
            ys = [p[1] for p in skeleton.points] or [0]
            zs = [p[2] for p in skeleton.points] or [0]
            rows = [
                ("POO2 vertices", str(len(skeleton.points))),
                ("POL2 polygons", str(skeleton.parsed_polygon_count)),
                ("SEN2 points (bounding/culling volume)",
                 str(len(skeleton.sensors))),
                ("bounds X", f"{min(xs):.1f} .. {max(xs):.1f}"),
                ("bounds Y", f"{min(ys):.1f} .. {max(ys):.1f} (negative = up)"),
                ("bounds Z", f"{min(zs):.1f} .. {max(zs):.1f}"),
                ("polygon sizes",
                 ", ".join(f"{k}-gon: {v}" for k, v in sorted(hist.items()))),
            ]
            for key, value in rows:
                item.addChild(QTreeWidgetItem([key, value]))
        self.stats_tree.expandAll()
        self.stats_tree.resizeColumnToContents(0)

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

    def _fill_animations(self, family: AssetFamily) -> None:
        self.anim_tree.clear()
        for name, ref in family.animation_refs.items():
            anm = family.animations.get(name)
            top = QTreeWidgetItem([name, ref.status if not ref.path
                                   else str(ref.path)])
            self.anim_tree.addTopLevelItem(top)
            if anm is None:
                continue
            top.addChild(QTreeWidgetItem(
                ["bitmap class", anm.bitmap_class or "?"]))
            top.addChild(QTreeWidgetItem(
                ["bitmaps", ", ".join(anm.bitmap_names) or "-"]))
            top.addChild(QTreeWidgetItem(
                ["UV groups",
                 ", ".join(str(len(g)) + " pts" for g in anm.texcoord_groups)]))
            for i, frame in enumerate(anm.frames):
                top.addChild(QTreeWidgetItem([
                    f"frame {i}",
                    f"bitmap #{frame.frame_id}, UV group #{frame.texcoords_id}, "
                    f"{frame.frame_time} ticks (~{frame.duration_ms:.1f} ms; "
                    "1024 Hz game clock)",
                ]))
            top.addChild(QTreeWidgetItem(
                ["cycle", f"~{anm.total_duration_ms:.1f} ms"]))
            top.addChild(QTreeWidgetItem(
                ["interpretation",
                 "texture/material animation (CONFIRMED); "
                 "no skeletal/vertex animation data"]))
        self.anim_tree.expandAll()
        self.anim_tree.resizeColumnToContents(0)

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

    app = QApplication(sys.argv)
    window = AssemblyWindow()
    window.show()
    if len(sys.argv) > 1:
        window.open_base(sys.argv[1])
    raise SystemExit(app.exec())
