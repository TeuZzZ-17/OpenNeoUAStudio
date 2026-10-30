"""OpenNeoUA Studio asset workbench window (AssemblyWindow), base of the editor workspaces.
"""

from __future__ import annotations

from pathlib import Path
import sys

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QImage,
    QImageWriter,
    QKeySequence,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QMainWindow,
    QMenu,
    QMessageBox,
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

from studio.assets.dependencies import DependencyProfile
from studio.assets.family import (
    AssetFamily,
    load_asset_family,
    load_manual_family,
    family_object_has_animation,
)
from studio.assets.mapping import (
    MappingIndex,
    MaterialBlockClipboard,
    RepairPlan,
)
from studio.assets.visproto import (
    normalize_base_name,
    normalize_skeleton_name,
    reconstruct_embedded_vps,
    VPEmbeddedError,
    VPTable,
)
from studio.formats.base_format import BaseParseError
from studio.formats.setbas import read_setbas, SetBasArchive, SetBasError
from studio.rendering.asset_viewport import (
    AssetViewport,
    VIEW_MODES,
    VIEW_PRESETS,
)
from studio.rendering.snapshot_export import (
    pillow_supports_animated_webp,
    pillow_supports_static_webp,
)
from studio.rendering.gizmo import ModelSpaceGizmo
from studio.ui.tabs import PaletteTabStrip
from studio.ui.uv_editor import (
    UVEditorWidget,
)
from studio.ui.widgets import (
    CollapsibleSection,
    PresetNumberBox,
    checker_thumbnail as _checker_thumbnail,
    choose_bas_archive,
    configure_operation_status_bar,
    create_import_bas_archive_action,
    install_standard_file_menu_tail,
    ProblemRowDelegate as _ProblemRowDelegate,
    LiveRotateDialog,
    LiveScaleDialog,
    qimage_from_ilbm as _qimage_from_ilbm,
    ResponsiveButtonGrid as _ResponsiveButtonGrid,
    STATUS_COLORS as _STATUS_COLORS,
    ViewportWidthScrollArea as _ViewportWidthScrollArea,
)
from studio.rendering.geometry import (
    FxElement,
)
from studio.workbench.common import (  # re-exported for existing callers
    WINDOW_TITLE as WINDOW_TITLE,
    _BAS_KIND_ROLE as _BAS_KIND_ROLE,
    _BAS_NAME_ROLE as _BAS_NAME_ROLE,
    _TransformStepSpinBox as _TransformStepSpinBox,
    _BasResourceTree as _BasResourceTree,
    _display_path as _display_path,
    _reveal_in_file_manager as _reveal_in_file_manager,
    AddFxElementDialog as AddFxElementDialog,
)
from studio.workbench.window_extraction import _ExtractionMixin
from studio.workbench.window_fx import _FxElementsMixin
from studio.workbench.window_mapping import _PolygonMappingMixin
from studio.workbench.window_geometry import _GeometryEditMixin
from studio.workbench.window_model_io import _ModelFilesMixin
from studio.workbench.window_uv import _UvTexturesMixin
from studio.workbench.window_panels import _FamilyPanelsMixin


class AssemblyWindow(
    _ExtractionMixin,
    _FxElementsMixin,
    _PolygonMappingMixin,
    _GeometryEditMixin,
    _ModelFilesMixin,
    _UvTexturesMixin,
    _FamilyPanelsMixin,
    QMainWindow,
):
    """Asset workbench window. Feature areas live in the mixins above
    (studio/workbench/window_*.py); this module keeps the UI scaffolding and
    the toolbar/menu actions."""

    # Large Family Mode starts above these sizes (see _FamilyPanelsMixin).
    LARGE_OBJECT_THRESHOLD = 50
    LARGE_FACE_THRESHOLD = 20000

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        from studio.ui.style import apply_studio_style
        apply_studio_style()
        self.setWindowTitle(WINDOW_TITLE)
        self.resize(1320, 800)
        self._fx_aliases = {}
        self._family: AssetFamily | None = None
        self._last_directory = Path.home()
        self._last_output_directory: Path | None = None
        self._extra_roots: list[Path] = []
        self._wireframe_windows: list[QMainWindow] = []
        self._collision_windows: list[QMainWindow] = []
        self._map_editor_windows: list[QMainWindow] = []
        self._preview_windows: list[QDialog] = []
        # session-only manual texture/animation bindings: logical name -> path
        self._overrides: dict[str, str] = {}
        # dependency workflow state (session-only, per asset)
        self._trial_names: set[str] = set()
        self._kept_names: set[str] = set()
        self._skipped_names: set[str] = set()
        self._diagnostics_tabs = None
        self._diagnostics_dock = None
        self._diagnostics_initialized = False
        # Large Family Mode / object selection state
        self._large_mode = False
        self._selected_owner: str | None = None
        self._owner_to_obj: dict[str, object] = {}
        self._owner_to_item: dict[str, QTreeWidgetItem] = {}
        # Exact BASE entry names proven by the VP table/embedded OBJT mapping.
        # This is intentionally owner-scoped: skeleton filenames are never
        # guessed into BASE names.
        self._base_entry_names: dict[str, str] = {}
        self._last_component_selection: tuple[str, str] | None = None
        self._base_dependency_dialog: QDialog | None = None
        # optional read-only SET.BAS resource provider
        self._setbas: SetBasArchive | None = None
        self._vp_table: VPTable | None = None
        self._vp_embedded = None
        self._vp_source = "unavailable"
        self._vp_source_path: Path | None = None
        # Polygon Mapping Workbench state
        self._mapping_index: MappingIndex | None = None
        self._workbench_obj = None          # first skeleton-bearing FamilyObject
        self._selected_poly: int | None = None
        self._selected_polys: set[int] = set()
        self._mirror_select_source_polys: set[int] = set()
        self._repair_plan: RepairPlan | None = None
        self._pending_repairs: list[RepairPlan] = []
        self._saved_repair_path: str | None = None
        # geometry Edit Mode: owners with unsaved vertex edits
        self._geom_dirty: dict[str, object] = {}
        self._geom_original: dict[str, list[tuple[float, float, float]]] = {}
        # Model/texture editor UV state; saved only through verified BASE exports.
        self._uv_ctx: tuple | None = None
        self._uv_contexts: dict[tuple[str, int, int], tuple] = {}
        self._uv_original: dict[tuple[str, int, int], list[tuple[int, int]]] = {}
        self._vanm_uv_original: dict[
            tuple[str, int], list[tuple[int, int]]] = {}
        # Session-only logical material bindings; source files stay untouched.
        self._texture_original: dict[tuple[str, int, str], str] = {}
        self._tab_edit_selection: dict[str, set[int]] = {}
        self._setbas_context_item = None
        self._setbas_preview_active = False
        self._fx_preview_cache: dict[tuple, tuple[list[QImage], list[str]]] = {}
        self._mapping_dialog: QDialog | None = None
        self._picked_structure: tuple[str, int, int] | None = None
        self._geometry_clipboard = None
        # Unlike the geometry clipboard, this is an immutable dependency-aware
        # snapshot and intentionally survives loading another family.
        self._material_clipboard: MaterialBlockClipboard | None = None
        self._geometry_writer_capability_cache: dict[tuple, str] = {}
        self._topology_original: dict[str, dict] = {}
        self._edit_undo_stack: list[dict] = []
        self._edit_redo_stack: list[dict] = []
        self._history_replaying = False
        self._uv_history_before: dict[
            tuple[str, int, int], list[tuple[int, int]]] | None = None
        self._object_info_asset_lines = ["No asset selected."]
        self._object_info_polygon_lines: list[str] = []
        self._fx_elements: list[FxElement] = []
        self._snapshot_mode_active = False
        self._snapshot_custom_color: QColor | None = None
        self._snapshot_zoom_percent = 100
        self._snapshot_zoom_reference: float | None = None
        self._snapshot_view_action_state: dict[QAction, bool] | None = None
        self._skip_model_switch_warning = False
        self._bundle_targets: dict[str, tuple[Path, Path]] = {}
        # Last successfully written standalone BASE per owner.  Subsequent
        # Overwrite / Export Asset Family operations build on this verified
        # snapshot so
        # already-saved UV and texture edits are not lost after the dirty
        # markers are cleared.
        self._bundle_base_snapshots: dict[str, bytes] = {}
        self._live_scale_dialog: LiveScaleDialog | None = None
        self._live_rotate_dialog: LiveRotateDialog | None = None
        self._asset_filter_expansion: dict[int, bool] | None = None
        # Asset Dependencies is a viewer/resolver, not a normal selection list.
        # The rows composing the active model keep a persistent visual focus
        # until the selected model owner changes.
        self._asset_focus_owner: str | None = None
        self._asset_focus_restoring = False
        self._texture_preview_cache: dict[tuple[int, str], str | None] = {}
        self._texture_qimage_cache: dict[tuple, QImage] = {}

        self.viewport = AssetViewport()
        from studio.rendering.gpu import add_renderer_badge
        self.renderer_badge = add_renderer_badge(self, self.viewport)
        self.viewport.statusMessage.connect(
            lambda text: self._notify(text, 4500)
        )
        self.viewport.polygonPickedDetailed.connect(self._on_polygon_picked)
        self.viewport.polygonStructurePicked.connect(
            self._on_polygon_structure_picked)
        self.viewport.polygonDeselected.connect(self._on_polygon_deselected)
        self.viewport.selectionCleared.connect(self._on_selection_cleared)
        self.viewport.objectPicked.connect(self._on_object_picked)
        self.viewport.editModeChanged.connect(self._on_edit_mode_toggled)
        self.viewport.editSelectionChanged.connect(
            lambda _count: self._sync_edit_action_states())
        self.viewport.editSelectionDetailsChanged.connect(
            self._on_edit_selection_details_changed)
        self.viewport.geometryEdited.connect(self._on_geometry_edited)
        self.viewport.geometryCommandCommitted.connect(
            self._on_geometry_command_committed)
        self.viewport.pastePreviewConfirmRequested.connect(
            self._confirm_paste_geometry)
        self.viewport.pastePreviewActiveChanged.connect(
            self._on_paste_preview_active_changed)
        self.viewport.movePlacementRequested.connect(
            self._move_selected_geometry)
        self.viewport.undoRequested.connect(self._undo_edit)
        self.viewport.redoRequested.connect(self._redo_edit)
        self.viewport.animationFrameChanged.connect(
            self._on_animation_frame_changed)
        self.viewport.editHint.connect(
            lambda text: self._notify(text, 30000)
        )
        self.viewport.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.viewport.customContextMenuRequested.connect(
            self._show_viewport_context_menu)

        self.asset_tree = QTreeWidget()
        from studio.ui.widgets import use_standard_list_style
        use_standard_list_style(self.asset_tree, selected_color=True)
        self.asset_tree.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.asset_tree.setHeaderLabels(["Asset", "Status", "Source / Path"])
        self.asset_tree.setItemDelegate(_ProblemRowDelegate(self.asset_tree))
        self.asset_tree.setUniformRowHeights(True)
        self.asset_tree.setIndentation(12)
        self.asset_tree.setAnimated(False)
        self.asset_tree.setIconSize(QSize(10, 10))
        self.asset_tree.setStyleSheet(
            self.asset_tree.styleSheet()
            + "QTreeWidget::item { padding-top: 0px; padding-bottom: 0px; "
            "padding-left: 1px; padding-right: 1px; }"
        )
        # The path column stays in the model (tooltips, filter) but is hidden:
        # Asset + Status always fit the panel without a scrollbar.
        asset_header = self.asset_tree.header()
        asset_header.setSectionsMovable(False)
        asset_header.setStretchLastSection(False)
        asset_header.setMinimumSectionSize(40)
        asset_header.setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        asset_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        asset_header.setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents)
        self.asset_tree.setColumnHidden(2, True)
        self.asset_tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.asset_tree.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.asset_tree.currentItemChanged.connect(self._on_tree_node_selected)
        self.asset_tree.itemSelectionChanged.connect(
            self._restore_asset_dependency_focus)
        self.asset_tree.itemClicked.connect(
            lambda item, _column: self._remember_component_selection(item))
        self.asset_tree.itemDoubleClicked.connect(self._on_tree_double_clicked)
        self.asset_tree.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.asset_tree.customContextMenuRequested.connect(
            self._show_asset_context_menu)
        from PySide6.QtWidgets import QLineEdit
        self.tree_search = QLineEdit()
        self.tree_search.setPlaceholderText("Filter objects/textures")
        self.tree_search.textChanged.connect(self._filter_asset_tree)
        self.texture_list = QListWidget()
        use_standard_list_style(self.texture_list)
        # The generic grey hover reads as "texture not loaded": this list uses the
        # theme's row hover color instead (rule in studio.qss).
        self.texture_list.setProperty("studioList", "textures")
        self.texture_list.setItemDelegate(_ProblemRowDelegate(self.texture_list))
        self.texture_list.setIconSize(QPixmap(96, 96).size())
        self.texture_list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        # Asset Textures is a compact resource browser. Long source paths wrap
        # inside the available width instead of forcing a horizontal scrollbar.
        self.texture_list.setWordWrap(True)
        self.texture_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.texture_list.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.texture_list.customContextMenuRequested.connect(
            self._show_texture_context_menu)
        self.texture_list.itemClicked.connect(
            self._load_visual_texture_item)
        self.texture_list.itemClicked.connect(
            lambda item: self._remember_texture_selection(item))
        self.texture_list.itemDoubleClicked.connect(
            self._preview_visual_texture_item)
        self.setbas_tree = _BasResourceTree()
        self.setbas_tree.setHeaderLabels(["Resource", "Payload", "VP"])
        from studio.ui.widgets import use_resource_delegate
        use_resource_delegate(self.setbas_tree)
        self.setbas_tree.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
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
        self.setbas_tree.currentItemChanged.connect(
            self._on_setbas_item_selected)
        self.setbas_tree.installEventFilter(self)
        self.setbas_tree.itemSelectionChanged.connect(
            self._sync_vp_controls)
        self.setbas_tree.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.setbas_tree.customContextMenuRequested.connect(
            self._show_setbas_context_menu)
        setbas_header = self.setbas_tree.header()
        setbas_header.setSectionsMovable(False)
        # The last column absorbs the remaining width, so the header never
        # leaves an empty trailing section.
        setbas_header.setStretchLastSection(True)
        setbas_header.setMinimumSectionSize(40)
        setbas_header.setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        for column in range(3):
            setbas_header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.Interactive)
        # Resource and Payload fit their widest text; VP absorbs every pixel
        # left over, so long ID lists stay readable and no space stays empty.
        self.setbas_tree.setColumnWidth(0, 260)
        self.setbas_tree.setColumnWidth(1, 120)
        self.setbas_tree.setColumnWidth(2, 130)
        self.setbas_tree.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.poly_uv_label = QLabel("Select a polygon in the viewport.")
        self.poly_uv_label.setMinimumHeight(200)
        self.fx_combo = QComboBox()
        self.fx_combo.setToolTip(
            "Choose an FX element to select it and edit its vertices.")
        self.fx_combo.currentIndexChanged.connect(self._on_fx_selected)
        self.blocks_list = QListWidget()
        use_standard_list_style(self.blocks_list)
        self.blocks_list.currentRowChanged.connect(self._on_block_selected)
        self.material_copy_button = QPushButton("Copy Material")
        self.material_copy_button.clicked.connect(self._copy_material_block)
        self.material_paste_button = QPushButton("Paste Material")
        self.material_paste_button.clicked.connect(self._paste_material_block)
        self.material_add_button = QPushButton("Add Compatible Slot")
        self.material_add_button.clicked.connect(self._add_material_slot)
        self.material_delete_button = QPushButton("Delete Material")
        self.material_delete_button.clicked.connect(
            self._delete_material_block)
        self.repair_target_combo = QComboBox()
        self.repair_source_spin = QSpinBox()
        self.repair_source_spin.setRange(0, 65535)
        self.repair_copy_button = QPushButton("Copy Mapping")
        self.repair_copy_button.clicked.connect(self._plan_copy_style)
        self.repair_planar_button = QPushButton("Create Planar Mapping")
        self.repair_planar_button.clicked.connect(self._plan_planar)
        self.repair_preview = QListWidget()
        use_standard_list_style(self.repair_preview)
        self.repair_apply_button = QPushButton("Apply")
        self.repair_apply_button.clicked.connect(self._apply_repair_in_memory)
        self.repair_revert_button = QPushButton("Revert")
        self.repair_revert_button.clicked.connect(self._revert_repairs)
        self.repair_save_button = QPushButton("Export Repaired BASE")
        self.repair_save_button.clicked.connect(self._save_repaired_as)
        self.chunk_tree = QTreeWidget()
        use_standard_list_style(self.chunk_tree)
        self.chunk_tree.setHeaderLabels(["Chunk", "Size", "Offset"])
        self.warning_list = QListWidget()
        self.checks_list = QListWidget()
        self.log_list = QListWidget()
        for view in (self.warning_list, self.checks_list, self.log_list):
            use_standard_list_style(view)
        for widget in (
                self.blocks_list, self.repair_preview,
                self.warning_list, self.checks_list, self.log_list):
            widget.setContextMenuPolicy(
                Qt.ContextMenuPolicy.CustomContextMenu)
            widget.customContextMenuRequested.connect(
                lambda pos, source=widget:
                self._show_generic_item_context_menu(source, pos))
        # tool-side dependency choice profile (~/.openneouastudio), never asset-side
        self._profile = DependencyProfile()
        if self._profile.load_error:
            self.log_list.addItem(f"profile: {self._profile.load_error}")

        self._build_toolbar()
        self._build_layout()
        # The bottom-left status line is the canonical non-blocking operation
        # channel for the workbench. Keep it visually distinct from ordinary
        # labels without replacing confirmations that require a user choice.
        configure_operation_status_bar(self)
        self._sync_geometry_save_controls()
        self._notify(
            "Model exports are verified; overwriting a loose skeleton requires "
            "confirmation (File Backup menu: optional .bak copy).", 12000)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self.viewport.paste_preview_active:
            self.viewport.cancel_paste_preview()
        # Hidden windows are common in focused UI tests and have never exposed
        # user-editable state. A visible workbench must not silently discard it.
        if self.isVisible() and not self._confirm_discard_geometry():
            event.ignore()
            return
        self._cancel_live_transforms()
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
        menu.exec(widget.viewport().mapToGlobal(position))

    def _show_setbas_context_menu(self, position) -> None:
        item = self.setbas_tree.itemAt(position)
        menu = QMenu(self.setbas_tree)
        self._setbas_context_item = item
        resources = self._setbas_selected_resources()
        kind = item.data(0, _BAS_KIND_ROLE) if item is not None else None
        preview = menu.addAction(
            "Preview", lambda: self._preview_setbas_resource(item))
        preview.setEnabled(kind in (
            "base", "sklt.class", "ilbm.class", "bmpanim.class"))
        playing = (item is self.setbas_tree.currentItem()
                   and self.viewport._anim_playing)
        playback = menu.addAction(
            "Pause" if playing else "Play",
            lambda: self._play_setbas_item(item))
        playback.setEnabled(self._setbas_item_has_animation(item))
        menu.addSeparator()
        extract = menu.addAction(
            f"Extract selected ({len(resources)})" if len(resources) > 1
            else "Extract Selected")
        extract.setEnabled(bool(resources))
        extract.triggered.connect(self._extract_setbas_selected)
        menu.addAction("Extract Archive",
                       self._extract_setbas_archive).setEnabled(
                           self._setbas is not None)
        menu.addAction(
            "Export Runtime Loose SET",
            self._export_runtime_loose_set).setEnabled(
                self._setbas is not None)
        export_family = menu.addAction(
            "Export Asset Family",
            lambda: self._export_setbas_resource_family(item))
        export_family.setEnabled(kind in (
            "base", "sklt.class", "ilbm.class", "bmpanim.class"))
        export_onua3d = menu.addAction(
            "Export OpenNeoUA 3D",
            lambda: self._export_setbas_base_onua3d(item))
        export_onua3d.setEnabled(kind == "base" and item is not None)
        self._add_model_generator_context_actions(menu, item)
        if kind == "base" and item is not None:
            base_name = (
                item.data(0, _BAS_NAME_ROLE) or item.text(0)).strip()
            menu.addSeparator()
            menu.addAction(
                "Show Dependencies",
                lambda: self._show_setbas_base_dependencies(base_name))
            menu.addAction(
                "Edit BASE Dependencies",
                lambda: self._edit_setbas_base_dependencies(base_name))
        if resources:
            menu.addSeparator()
            copy_names = menu.addAction("Copy resource name(s)")
            copy_names.triggered.connect(lambda: self._copy_text(
                "\n".join(resource.resource_name for resource in resources),
                f"Copied {len(resources)} BAS resource name(s)."))
        elif kind == "base" and item is not None:
            base_name = (
                item.data(0, _BAS_NAME_ROLE) or item.text(0)).strip()
            menu.addSeparator()
            copy_name = menu.addAction("Copy BASE Name")
            copy_name.triggered.connect(lambda: self._copy_text(
                base_name, "BASE name copied successfully."))
        menu.addSeparator()
        menu.addAction("Select All Resources", self.setbas_tree.selectAll)
        if self._last_output_directory is not None:
            menu.addAction("Open Last Output Folder",
                           self._open_last_output_folder)
        try:
            menu.exec(self.setbas_tree.viewport().mapToGlobal(position))
        finally:
            self._setbas_context_item = None

    def _add_model_generator_context_actions(self, menu, item) -> None:
        """Allow the Model Editor to extend the shared resource menu."""

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
        if kind == "dependency_candidate":
            _name, candidate = payload
            if candidate and not str(candidate).startswith("SET.BAS:"):
                return Path(candidate)
        return None

    def _remember_component_selection(self, item) -> None:
        data = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        if data and data[0] in ("skeleton", "texture", "animation"):
            kind, payload = data
            name = (payload.base_object.skeleton_name
                    if kind == "skeleton" else str(payload))
            self._last_component_selection = (kind, name)
        else:
            self._last_component_selection = None

    def _remember_texture_selection(self, item) -> None:
        name = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        self._last_component_selection = (
            ("texture", str(name)) if name else None)

    def _reveal_asset_item(self, item) -> None:
        path = self._asset_item_path(item)
        if path is None:
            self.statusBar().showMessage("No on-disk file to reveal.")
            return
        _reveal_in_file_manager(path)

    def _play_asset_animation(self, item) -> None:
        if self._family is None or item is None:
            return
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data or data[0] != "animation":
            return
        name = data[1]
        owner = self._tree_owner_for_item(item)
        if owner and owner != self._selected_owner:
            self.statusBar().showMessage(
                "Asset Dependencies is a viewer: select that model first "
                "before playing its animation.", 6000)
            return
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
        if data:
            kind, payload = data
            if kind == "texture":
                preview = menu.addAction("Preview Texture")
                preview.setEnabled(self._family is not None
                                   and self._visual_texture_available(payload))
                preview.triggered.connect(
                    lambda: self._open_visual_texture(
                        payload, show_preview=True, switch_tabs=False))
                export = menu.addAction("Export Texture as PNG")
                export.setEnabled(self._visual_texture_available(payload))
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
            if kind in ("base", "child"):
                menu.addAction(
                    "Edit BASE Dependencies",
                    lambda: self._edit_base_dependencies_for_item(item))
        dependency_target = self._dependency_target_for_item(item)
        if dependency_target is not None:
            logical_name, candidate = dependency_target
            menu.addSeparator()
            select_resource = menu.addAction(
                "Select Resource", self._select_dependency_resource)
            select_resource.setEnabled(bool(logical_name))
            use = menu.addAction(
                "Use Selected Source", self._use_selected_candidate)
            use.setEnabled(bool(candidate))
            keep = menu.addAction("Keep for Session", self._keep_for_session)
            keep.setEnabled(bool(logical_name))
            revert = menu.addAction("Revert Source", self._clear_override)
            revert.setEnabled(bool(logical_name))
            dep = self._dep_for_name(logical_name)
            candidates = self._dependency_candidate_paths(logical_name, dep)
            if candidates:
                candidates_menu = menu.addMenu("Available Sources")
                for label, path in candidates:
                    candidates_menu.addAction(
                        label, lambda checked=False, name=logical_name,
                        selected=path: self._apply_dependency_candidate(
                            name, selected))
        data = item.data(0, Qt.ItemDataRole.UserRole)
        owner = self._tree_owner_for_item(item)
        export_family = menu.addAction("Export Asset Family")
        export_family.setEnabled(bool(owner or (data and data[0] in (
            "skeleton", "texture", "animation"))))

        def export_current_family() -> None:
            if data and data[0] in ("skeleton", "texture", "animation"):
                self._remember_component_selection(item)
            else:
                self._last_component_selection = None
                if owner:
                    self._selected_owner = owner
            self._save_model_as()

        export_family.triggered.connect(export_current_family)
        export_onua3d = menu.addAction("Export OpenNeoUA 3D")
        export_onua3d.setEnabled(bool(data and data[0] == "base"))

        def export_current_onua3d() -> None:
            if not data or data[0] != "base":
                return
            fam_obj = data[1]
            if fam_obj is not None:
                self._selected_owner = fam_obj.owner_path
            self._export_to_blender()

        export_onua3d.triggered.connect(export_current_onua3d)
        path = self._asset_item_path(item)
        if path is not None:
            menu.addAction("Reveal Source File",
                           lambda: self._reveal_asset_item(item))
        menu.addSeparator()
        menu.addAction("Copy Info", lambda: self._copy_text(
            self._widget_item_text(self.asset_tree, item),
            "Asset information copied successfully."))
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
        preview.setEnabled(
            len(names) == 1 and self._visual_texture_available(names[0]))
        preview.triggered.connect(
            lambda: self._preview_family_texture(names[0]) if names else None)
        export = menu.addAction(
            f"Export selected as PNG ({len(names)})" if len(names) > 1
            else "Export Selected as PNG")
        export.setEnabled(bool(names))
        export.triggered.connect(
            lambda: self._export_family_textures_png(names))
        export_family = menu.addAction("Export Asset Family")
        export_family.setEnabled(len(names) == 1 and self._family is not None)

        def export_texture_family() -> None:
            if len(names) != 1:
                return
            self._last_component_selection = ("texture", str(names[0]))
            self._save_model_as()

        export_family.triggered.connect(export_texture_family)
        if len(names) == 1 and self._family is not None:
            ref = self._family.texture_refs.get(names[0])
            if ref and ref.path:
                menu.addAction(
                    "Reveal Source File",
                    lambda: _reveal_in_file_manager(ref.path))
        if names:
            menu.addSeparator()
            menu.addAction("Copy texture name(s)",
                           lambda: self._copy_texture_names(names))
        menu.addSeparator()
        menu.addAction("Select All Textures", self.texture_list.selectAll)
        menu.exec(self.texture_list.viewport().mapToGlobal(position))

    def refresh_studio_theme(self) -> None:
        """Re-tint rows painted with theme colors (used textures, unresolved assets)."""

        if hasattr(self, "texture_list"):
            self._refresh_texture_usage_highlight()
        from studio.ui.widgets import mark_problem_item, PROBLEM_ROLE
        for view in (getattr(self, "asset_tree", None), getattr(self, "texture_list", None)):
            if view is None:
                continue
            items = ([view.item(i) for i in range(view.count())] if hasattr(view, "count")
                     else self._iter_tree_items(view))
            is_tree = hasattr(view, "topLevelItemCount")
            for item in items:
                if item is None:
                    continue
                mark_problem_item(
                    item, bool(item.data(0, PROBLEM_ROLE) if is_tree else item.data(PROBLEM_ROLE)),
                    columns=3 if is_tree else 1)
                if is_tree:
                    # Status text colors depend on the theme mode (darker shades on light themes).
                    status = item.text(1)
                    key = {"auto_loaded": "found", "loaded": "found", "resolved": "found",
                           "kept_for_session": "manual", "trial_loaded": "manual",
                           "failed_load": "decode failed", "skipped": "missing",
                           "unsupported_loader": "ambiguous"}.get(status, status)
                    color = _STATUS_COLORS.get(key)
                    if status and color is not None:
                        item.setForeground(1, color)

    @staticmethod
    def _iter_tree_items(tree):
        stack = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
        while stack:
            item = stack.pop()
            yield item
            stack.extend(item.child(i) for i in range(item.childCount()))

    def _build_toolbar(self) -> None:
        # --- File menu: explicit import/export workflow ---
        file_menu = self.menuBar().addMenu("&File")
        self.file_menu = file_menu
        self.open_bas_archive_action = create_import_bas_archive_action(
            self, self.open_bas_archive_dialog)
        # Retain the public action attribute used by existing integrations;
        # its UI meaning remains the read-only archive import.
        self.open_base_action = self.open_bas_archive_action
        # "Import BASE" is the guided asset-family import: BASE, skeleton,
        # textures and animations chosen in one go (each step is optional).
        self.open_family_action = QAction("Import BASE", self)
        self.open_family_action.triggered.connect(self.open_family_dialog)
        self.import_onua3d_action = QAction("Import OpenNeoUA 3D", self)
        self.import_onua3d_action.triggered.connect(self._import_onua3d)

        self.file_import_menu = file_menu.addMenu("Import")
        for action in (
                self.open_bas_archive_action, self.open_family_action,
                self.import_onua3d_action):
            self.file_import_menu.addAction(action)

        # One shared Runtime Loose action is exposed from both File > Export
        # and Tools > BAS Archive.  Keeping one QAction means both entry
        # points always share the same enabled state and implementation.
        self.export_runtime_loose_action = QAction(
            "Extract Entire Current Set", self)
        self.export_runtime_loose_action.setEnabled(False)
        self.export_runtime_loose_action.triggered.connect(
            self._export_runtime_loose_set)
        # "Export BASE" is the guided asset-family export (BASE, skeleton,
        # textures and animations as one portable package).
        self.save_asset_family_action = QAction("Export BASE", self)
        self.save_asset_family_action.setEnabled(False)
        self.save_asset_family_action.triggered.connect(self._save_model_as)
        self.export_blender_action = QAction("Export OpenNeoUA 3D", self)
        self.export_blender_action.setEnabled(False)
        self.export_blender_action.triggered.connect(self._export_to_blender)
        self.overwrite_action = QAction("Overwrite", self)
        self.overwrite_action.setEnabled(False)
        self.overwrite_action.triggered.connect(self._overwrite_model)

        self.file_export_menu = file_menu.addMenu("Export")
        for action in (
                self.export_runtime_loose_action,
                self.save_asset_family_action,
                self.export_blender_action,
                self.overwrite_action):
            self.file_export_menu.addAction(action)

        (self.close_bas_archive_action,
         self.exit_action) = install_standard_file_menu_tail(
            file_menu, self, close_archive_callback=self.close_current_archive)

        edit_menu = self.menuBar().addMenu("&Edit")
        self.edit_menu = edit_menu
        self.edit_menu = edit_menu
        self.edit_toggle_action = QAction("Edit Mode", self)
        self.edit_toggle_action.setCheckable(True)
        self.edit_toggle_action.setShortcut(QKeySequence("Tab"))
        self.edit_toggle_action.setShortcutContext(
            Qt.ShortcutContext.WindowShortcut)
        self.edit_toggle_action.toggled.connect(self._set_global_edit_mode)
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
        self.edit_reset_action = QAction("Reset Model", self)
        self.edit_reset_action.triggered.connect(self._reset_model)
        edit_menu.addAction(self.edit_reset_action)
        edit_menu.addSeparator()
        self.edit_select_all_action = QAction("Select All Geometry", self)
        self.edit_select_all_action.setStatusTip(
            "Select the whole model shape, protected FX vertices included.")
        self.edit_select_all_action.triggered.connect(
            self.viewport.select_all_edit_vertices)
        edit_menu.addAction(self.edit_select_all_action)
        self.select_all_fx_action = QAction("Select All FX", self)
        self.select_all_fx_action.setStatusTip(
            "Select every FX element of the current model and no other "
            "geometry.")
        self.select_all_fx_action.triggered.connect(
            self._select_all_fx_elements)
        edit_menu.addAction(self.select_all_fx_action)
        self.edit_select_none_action = QAction("Deselect All Geometry", self)
        self.edit_select_none_action.setStatusTip(
            "Clear the model geometry selection.")
        self.edit_select_none_action.triggered.connect(
            self.viewport.select_no_edit_vertices)
        edit_menu.addAction(self.edit_select_none_action)
        edit_menu.addSeparator()
        self.copy_geometry_action = QAction("Copy", self.viewport)
        self.copy_geometry_action.setShortcut(QKeySequence.StandardKey.Copy)
        self.copy_geometry_action.setShortcutContext(
            Qt.ShortcutContext.WidgetShortcut)
        self.copy_geometry_action.setStatusTip(
            "Copy the selected polygons and start a transparent placement preview.")
        self.copy_geometry_action.triggered.connect(self._copy_geometry)
        self.viewport.addAction(self.copy_geometry_action)
        edit_menu.addAction(self.copy_geometry_action)
        self.paste_geometry_action = QAction("Paste", self.viewport)
        self.paste_geometry_action.setShortcut(QKeySequence.StandardKey.Paste)
        self.paste_geometry_action.setShortcutContext(
            Qt.ShortcutContext.WidgetShortcut)
        self.paste_geometry_action.setStatusTip(
            "Confirm the current preview or start a new copy preview.")
        self.paste_geometry_action.triggered.connect(self._paste_geometry)
        self.viewport.addAction(self.paste_geometry_action)
        edit_menu.addAction(self.paste_geometry_action)
        self.cut_geometry_action = QAction("Cut", self.viewport)
        self.cut_geometry_action.setShortcut(QKeySequence.StandardKey.Cut)
        self.cut_geometry_action.setShortcutContext(
            Qt.ShortcutContext.WidgetShortcut)
        self.cut_geometry_action.setStatusTip(
            "Cut the selected polygons: copy them and remove them in one "
            "undoable step.")
        self.cut_geometry_action.triggered.connect(self._cut_geometry)
        self.viewport.addAction(self.cut_geometry_action)
        edit_menu.addAction(self.cut_geometry_action)
        self.delete_geometry_action = QAction("Delete", self.viewport)
        self.delete_geometry_action.setShortcut(
            QKeySequence(Qt.Key.Key_Delete))
        self.delete_geometry_action.setShortcutContext(
            Qt.ShortcutContext.WidgetShortcut)
        self.delete_geometry_action.setStatusTip(
            "Delete the selected polygons and compact unused vertices.")
        self.delete_geometry_action.triggered.connect(self._delete_geometry)
        self.viewport.addAction(self.delete_geometry_action)
        edit_menu.addAction(self.delete_geometry_action)
        self.add_fx_action = QAction("Add FX", self)
        self.add_fx_action.setEnabled(False)
        self.add_fx_action.setStatusTip(
            "Add an FX from this model or another model.")
        self.add_fx_action.triggered.connect(self._add_fx_element)
        edit_menu.addAction(self.add_fx_action)
        self.clone_fx_action = QAction("Clone FX Element", self)
        self.clone_fx_action.triggered.connect(self._copy_geometry)
        edit_menu.addAction(self.clone_fx_action)
        self.rename_fx_action = QAction("Rename FX", self)
        self.rename_fx_action.triggered.connect(self._rename_selected_fx)
        edit_menu.addAction(self.rename_fx_action)
        self.edit_move_action = QAction("Move", self)
        self.edit_move_action.setStatusTip(
            "Move selected vertices/polygons continuously in place (G); "
            "click or Enter confirms, Esc cancels.")
        self.edit_move_action.triggered.connect(self._move_selected_geometry)
        edit_menu.addAction(self.edit_move_action)
        self.edit_scale_action = QAction("Scale", self)
        self.edit_scale_action.triggered.connect(self._scale_selected_geometry)
        edit_menu.addAction(self.edit_scale_action)
        self.edit_rotate_action = QAction("Rotate", self)
        self.edit_rotate_action.triggered.connect(
            self._rotate_selected_geometry)
        edit_menu.addAction(self.edit_rotate_action)
        self.edit_center_action = QAction("Center Model at Origin", self)
        self.edit_center_action.setStatusTip(
            "Move the whole model so the center of its bounding box sits at "
            "the model origin (0, 0, 0). Undo restores it.")
        self.edit_center_action.triggered.connect(self._center_model_at_origin)
        edit_menu.addAction(self.edit_center_action)

        view_menu = self.menuBar().addMenu("&View")
        self.view_menu = view_menu
        self.sen_check = self._checkable("SEN2 volume",
                                         self.viewport.set_show_sen, False)
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
                       self.axes_check, self.grid_check,
                       self.overlay_check, self.mapping_diag_check):
            view_menu.addAction(action)
        self.retail_distance_fade_check = self._checkable(
            "Vanilla distance fade",
            self.viewport.set_retail_distance_fade, False)
        self.retail_distance_fade_check.setStatusTip(
            "Retail AREA distance fade recovered from the fork, disabled by default: "
            "AREA_FLAG_DPTHFADE faces use the vanilla gameplay profile "
            "visLimit 1400, fadeStart 800, fadeLength 600.")
        view_menu.addAction(self.retail_distance_fade_check)
        # Snapshot Studio reuses these exact QAction objects.  Their normal
        # viewport setters already own the feature state; this extra signal
        # only switches the Snapshot renderer out of clean-model-only mode
        # when one of the shared overlays is actually requested.
        for action in (self.sen_check, self.wire_check, self.axes_check,
                       self.grid_check, self.overlay_check,
                       self.mapping_diag_check):
            action.toggled.connect(self._sync_snapshot_view_render_mode)
        view_menu.addSeparator()
        self.reset_camera_action = QAction("Reset View", self)
        self.reset_camera_action.setEnabled(False)
        self.reset_camera_action.triggered.connect(
            self._reset_view_and_gizmo)
        view_menu.addAction(self.reset_camera_action)

        tools_menu = self.menuBar().addMenu("&Tools")
        self.tools_menu = tools_menu
        setbas_tools_menu = tools_menu.addMenu("BAS Archive")
        extract_setbas_action = QAction("Extract Archive", self)
        extract_setbas_action.triggered.connect(self._extract_setbas_archive)
        setbas_tools_menu.addAction(extract_setbas_action)
        setbas_tools_menu.addAction(self.export_runtime_loose_action)
        metadata_action = QAction("Export Scene Metadata", self)
        metadata_action.triggered.connect(self._export_setbas_metadata)
        setbas_tools_menu.addAction(metadata_action)
        open_output = QAction("Open Last Output Folder", self)
        open_output.triggered.connect(self._open_last_output_folder)
        setbas_tools_menu.addAction(open_output)

        conversion_menu = tools_menu.addMenu("Texture Conversion")
        conv_to_png = QAction("ILBM/VBMP to PNG", self)
        conv_to_png.triggered.connect(self._convert_ilbm_to_png_dialog)
        conversion_menu.addAction(conv_to_png)
        conv_vbmp_to_ilbm = QAction("VBMP to Standalone ILBM", self)
        conv_vbmp_to_ilbm.triggered.connect(
            self._convert_vbmp_to_ilbm_dialog)
        conversion_menu.addAction(conv_vbmp_to_ilbm)
        conv_to_ilbm = QAction("PNG to ILBM (matching templates)", self)
        conv_to_ilbm.triggered.connect(self._convert_png_to_ilbm_dialog)
        conversion_menu.addAction(conv_to_ilbm)

        self.wireframe_editor_action = QAction("Wireframe Editor", self)
        self.wireframe_editor_action.triggered.connect(
            self._open_wireframe_editor)
        self.integrated_editors_separator = tools_menu.addSeparator()
        tools_menu.addAction(self.wireframe_editor_action)
        self.entity_designer_action = QAction("Entity Designer", self)
        self.entity_designer_action.triggered.connect(
            self._open_entity_designer)
        tools_menu.addAction(self.entity_designer_action)
        self.map_editor_action = QAction("Map Editor", self)
        self.map_editor_action.triggered.connect(self._open_map_editor)
        tools_menu.addAction(self.map_editor_action)
        self.mapping_repair_action = QAction("Mapping Repair", self)
        self.mapping_repair_action.triggered.connect(
            self._show_mapping_repair)
        tools_menu.addAction(self.mapping_repair_action)

        diagnostics_menu = self.menuBar().addMenu("&Diagnostics")
        self.diagnostics_toggle_action = QAction(
            "Show Diagnostic Panel", self)
        self.diagnostics_toggle_action.triggered.connect(
            self._toggle_diagnostics_panel)
        diagnostics_menu.addAction(self.diagnostics_toggle_action)
        diagnostics_menu.addSeparator()
        self.clear_log_action = QAction("Clear Log", self)
        self.clear_log_action.triggered.connect(self._clear_diagnostics)
        diagnostics_menu.addAction(self.clear_log_action)

        self.object_info_menu = self.menuBar().addMenu("Object Info")
        self._set_object_info(["No asset selected."])

        # --- toolbar: essentials only ---
        toolbar = QToolBar("Workbench", self)
        toolbar.setMovable(False)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, toolbar)

        self.global_undo_button = QPushButton("< Undo")
        self.global_undo_button.setEnabled(False)
        self.global_undo_button.setMinimumWidth(88)
        self.global_undo_button.setToolTip(
            "Undo the latest geometry, texture or UV edit.")
        self.global_undo_button.clicked.connect(self._undo_edit)
        toolbar.addWidget(self.global_undo_button)

        self.global_redo_button = QPushButton("Redo >")
        self.global_redo_button.setEnabled(False)
        self.global_redo_button.setMinimumWidth(88)
        self.global_redo_button.setToolTip(
            "Redo the latest geometry, texture or UV edit.")
        self.global_redo_button.clicked.connect(self._redo_edit)
        toolbar.addWidget(self.global_redo_button)

        self.mode_combo = QComboBox()
        for mode in (candidate for candidate in VIEW_MODES
                     if candidate != "solid"):
            label = {
                "wireframe": "Wireframe",
                "materials": "Material groups",
                "textured": "Textured",
            }[mode]
            self.mode_combo.addItem(label, mode)
        self.mode_combo.setCurrentIndex(
            self.mode_combo.findData("textured"))
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
        self.toolbar_reset_view_button = QPushButton("Reset View")
        self.toolbar_reset_view_button.setEnabled(False)
        self.toolbar_reset_view_button.setToolTip(
            "Restore the camera to the original view used when this model "
            "was loaded.")
        self.toolbar_reset_view_button.clicked.connect(
            self._reset_view_and_gizmo)
        toolbar.addWidget(self.toolbar_reset_view_button)

        self.toolbar_reset_model_button = QPushButton("Reset Model")
        self.toolbar_reset_model_button.setEnabled(False)
        self.toolbar_reset_model_button.setToolTip(
            "Discard unsaved geometry, texture and UV edits for the current model.")
        self.toolbar_reset_model_button.clicked.connect(self._reset_model)
        toolbar.addWidget(self.toolbar_reset_model_button)

        # Animation controls (enabled only when a VANM is loaded)
        anim_bar = QToolBar("Animation", self)
        anim_bar.setMovable(False)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, anim_bar)
        self.animation_toolbar = anim_bar

        anim_bar.addWidget(QLabel(" Animation: "))
        self.play_button = QPushButton("Play")
        self.play_button.setCheckable(True)
        self.play_button.setEnabled(False)
        self.play_button.toggled.connect(self._toggle_play)
        anim_bar.addWidget(self.play_button)

        self.step_button = QPushButton(">>")
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

    def _build_layout(self) -> None:
        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        tabs.setMinimumWidth(330)

        # SET.BAS is a primary workflow, not an advanced diagnostic panel.
        setbas_panel = QWidget()
        setbas_layout = QVBoxLayout(setbas_panel)
        setbas_layout.setContentsMargins(3, 5, 3, 4)
        setbas_layout.setSpacing(4)
        from PySide6.QtWidgets import QLineEdit
        self.setbas_search = QLineEdit()
        self.setbas_search.setPlaceholderText(
            "Search embedded resources (name or class)")
        self.setbas_search.textChanged.connect(self._filter_setbas_tree)
        setbas_layout.addWidget(self.setbas_search)
        setbas_layout.addWidget(self.setbas_tree, 1)
        self.vp_source_label = QLabel("VP source: unavailable")
        self.vp_source_label.setWordWrap(True)
        setbas_layout.addWidget(self.vp_source_label)

        vp_edit_box = QGroupBox("VP Information")
        vp_edit_layout = QGridLayout(vp_edit_box)
        vp_edit_layout.setContentsMargins(5, 4, 5, 5)
        vp_edit_layout.addWidget(QLabel("Selected:"), 0, 0)
        self.vp_selected_base_label = QLabel("-")
        self.vp_selected_base_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        vp_edit_layout.addWidget(self.vp_selected_base_label, 0, 1, 1, 3)
        vp_edit_layout.addWidget(QLabel("VP in SET.BAS:"), 1, 0)
        self.vp_current_label = QLabel("-")
        self.vp_current_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        vp_edit_layout.addWidget(self.vp_current_label, 1, 1, 1, 3)
        setbas_layout.addWidget(vp_edit_box)

        setbas_buttons = QGridLayout()
        setbas_buttons.setHorizontalSpacing(4)
        self.setbas_runtime_loose_button = QPushButton(
            "Export Runtime Loose SET")
        self.setbas_runtime_loose_button.clicked.connect(
            self._export_runtime_loose_set)
        self.setbas_runtime_loose_button.setEnabled(False)
        setbas_buttons.addWidget(self.setbas_runtime_loose_button, 0, 0)
        self.setbas_extract_button = QPushButton("Extract Selected")
        self.setbas_extract_button.clicked.connect(
            self._extract_setbas_selected)
        self.setbas_extract_button.setEnabled(False)
        setbas_buttons.addWidget(self.setbas_extract_button, 0, 1)
        self.setbas_extract_all_button = QPushButton("Extract Archive")
        self.setbas_extract_all_button.clicked.connect(
            self._extract_setbas_archive)
        self.setbas_extract_all_button.setEnabled(False)
        setbas_buttons.addWidget(self.setbas_extract_all_button, 1, 0)
        self.setbas_open_output_button = QPushButton("Open Output Folder")
        self.setbas_open_output_button.clicked.connect(
            self._open_last_output_folder)
        self.setbas_open_output_button.setEnabled(False)
        setbas_buttons.addWidget(self.setbas_open_output_button, 1, 1)
        self.setbas_onua3d_button = QPushButton(
            "Export OpenNeoUA 3D")
        self.setbas_onua3d_button.setToolTip(
            "Export the selected BASE family as an OpenNeoUA 3D (.onua3d) "
            "package for compatible 3D tools.")
        self.setbas_onua3d_button.setEnabled(False)
        self.setbas_onua3d_button.clicked.connect(
            self._export_selected_setbas_base_onua3d)
        setbas_buttons.addWidget(self.setbas_onua3d_button, 2, 0, 1, 2)
        self.setbas_edit_dependencies_button = QPushButton(
            "Edit BASE Dependencies")
        self.setbas_edit_dependencies_button.setEnabled(False)
        self.setbas_edit_dependencies_button.clicked.connect(
            self._edit_selected_setbas_base_dependencies)
        setbas_buttons.addWidget(
            self.setbas_edit_dependencies_button, 3, 0, 1, 2)
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
        self.texture_export_button = QPushButton("Export Selected as PNG")
        self.texture_export_button.clicked.connect(self._export_texture_png)
        textures_layout.addWidget(self.texture_export_button)

        model_panel = QWidget()
        model_layout = QVBoxLayout(model_panel)
        model_layout.setContentsMargins(5, 5, 5, 5)
        model_layout.setSpacing(6)

        self.poly_id_box = QGroupBox("Poly ID")
        self.poly_id_box.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        poly_id_row = QHBoxLayout(self.poly_id_box)
        self.poly_id_row_layout = poly_id_row
        self.poly_id_spin = QSpinBox()
        self.poly_id_spin.setRange(0, 0)
        self.poly_id_spin.setEnabled(False)
        self.poly_id_spin.setKeyboardTracking(False)
        self.poly_id_spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.poly_id_spin.setMinimumWidth(72)
        self.poly_id_spin.setMaximumWidth(96)
        self.poly_id_spin.setToolTip(
            "Select and inspect a polygon by its persistent zero-based ID.")
        self.poly_id_spin.valueChanged.connect(self._on_poly_id_changed)
        self.poly_id_spin.editingFinished.connect(
            lambda: self._on_poly_id_changed(self.poly_id_spin.value()))
        self.poly_id_previous_button = QPushButton("<")
        self.poly_id_previous_button.setEnabled(False)
        self.poly_id_previous_button.setFixedWidth(36)
        self.poly_id_previous_button.setToolTip("Previous polygon")
        self.poly_id_previous_button.clicked.connect(
            lambda: self._step_poly_id(-1))
        self.poly_id_next_button = QPushButton(">")
        self.poly_id_next_button.setEnabled(False)
        self.poly_id_next_button.setFixedWidth(36)
        self.poly_id_next_button.setToolTip("Next polygon")
        self.poly_id_next_button.clicked.connect(
            lambda: self._step_poly_id(1))
        # "<" value ">" centered in the group, arrows on either side.
        poly_id_row.addStretch(1)
        poly_id_row.addWidget(self.poly_id_previous_button)
        poly_id_row.addWidget(self.poly_id_spin)
        poly_id_row.addWidget(self.poly_id_next_button)
        poly_id_row.addStretch(1)
        model_layout.addWidget(self.poly_id_box)

        gizmo_box = QGroupBox("Transform")
        self.transform_box = gizmo_box
        gizmo_box.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        gizmo_layout = QVBoxLayout(gizmo_box)
        gizmo_layout.setContentsMargins(5, 4, 5, 4)
        gizmo_layout.setSpacing(4)
        self._transform_mode = "move"
        self._transform_steps = {
            "move": 0.50,
            "rotate": 1.0,
            "scale": 1.0,
        }
        mode_row = QHBoxLayout()
        mode_row.setSpacing(3)
        self.transform_mode_buttons = {}
        for mode, label in (
                ("move", "Move"), ("rotate", "Rotate"), ("scale", "Scale")):
            button = QPushButton(label)
            button.setCheckable(True)
            button.setChecked(mode == "move")
            button.setMinimumHeight(25)
            button.clicked.connect(
                lambda _checked=False, selected=mode:
                self._set_transform_mode(selected))
            self.transform_mode_buttons[mode] = button
            mode_row.addWidget(button)
        gizmo_layout.addLayout(mode_row)
        self.model_gizmo = ModelSpaceGizmo()
        self.model_gizmo.setEnabled(False)
        self.model_gizmo.nudgeStarted.connect(self._begin_gizmo_nudge)
        self.model_gizmo.directionTriggered.connect(self._apply_gizmo_nudge)
        self.model_gizmo.nudgeFinished.connect(self._finish_gizmo_nudge)
        self._sync_gizmo_camera()
        gizmo_layout.addWidget(self.model_gizmo)
        intensity_row = QHBoxLayout()
        intensity_row.setSpacing(4)
        self.transform_step_label = QLabel("Move step")
        intensity_row.addWidget(self.transform_step_label)
        self.gizmo_intensity_slider = QSlider(Qt.Orientation.Horizontal)
        self.gizmo_intensity_slider.setRange(0, 1000)
        self.gizmo_intensity_slider.setValue(
            self._gizmo_intensity_to_slider(0.50))
        self.gizmo_intensity_slider.setToolTip(
            "Set the click and hold-repeat distance in model-space units.")
        self.gizmo_intensity_spin = _TransformStepSpinBox()
        self.gizmo_intensity_spin.setRange(0.001, 1000.0)
        self.gizmo_intensity_spin.setDecimals(4)
        self.gizmo_intensity_spin.setSingleStep(0.1)
        self.gizmo_intensity_spin.setValue(0.50)
        self.gizmo_intensity_spin.setSuffix(" model units")
        self.gizmo_intensity_spin.setMinimumWidth(135)
        self.gizmo_intensity_spin.setToolTip(
            "Type the exact movement distance applied by each gizmo nudge.")
        self.gizmo_intensity_slider.valueChanged.connect(
            self._set_gizmo_intensity_from_slider)
        self.gizmo_intensity_spin.valueChanged.connect(
            self._set_gizmo_slider_from_intensity)
        intensity_row.addWidget(self.gizmo_intensity_slider, 1)
        intensity_row.addWidget(self.gizmo_intensity_spin)
        gizmo_layout.addLayout(intensity_row)
        transform_options = QGridLayout()
        self._transform_options_layout = transform_options
        transform_options.setContentsMargins(0, 0, 0, 0)
        transform_options.setHorizontalSpacing(12)
        transform_options.setVerticalSpacing(3)
        self.auto_align_check = QCheckBox("Auto Align")
        self.auto_align_check.setChecked(True)
        self.auto_align_check.setToolTip(
            "Snap direct viewport editing to nearby model-space alignment "
            "targets. Gizmo actions always apply their exact configured step.")
        self.auto_align_check.toggled.connect(
            self.viewport.set_auto_align_enabled)
        self.viewport.set_auto_align_enabled(True)
        transform_options.addWidget(self.auto_align_check, 0, 1)
        self.mirror_select_check = QCheckBox("Mirror Select")
        self.mirror_select_check.setChecked(False)
        self.mirror_select_check.setEnabled(False)
        self.mirror_select_check.setToolTip(
            "Keep selected geometry and automatically add every matching "
            "counterpart across the combined X, Y and Z symmetry planes.")
        self.mirror_select_check.toggled.connect(
            self._on_mirror_select_toggled)
        transform_options.addWidget(self.mirror_select_check, 0, 0)
        for column in range(3):
            transform_options.setColumnStretch(column, 1)
        gizmo_layout.addLayout(transform_options)
        self.center_origin_button = QPushButton("Center at Origin")
        self.center_origin_button.setEnabled(False)
        self.center_origin_button.setToolTip(
            "Move the whole model so the center of its bounding box sits at "
            "the model origin (0, 0, 0). Undo restores it.")
        self.center_origin_button.clicked.connect(
            self.edit_center_action.trigger)
        gizmo_layout.addWidget(self.center_origin_button)
        self.viewport.set_direct_transform("move", 0.50)
        self._apply_transform_mode_colors()
        model_layout.addWidget(gizmo_box)

        fx_box = QGroupBox("FX")
        fx_layout = QVBoxLayout(fx_box)
        fx_layout.setContentsMargins(5, 4, 5, 5)
        fx_layout.setSpacing(4)
        self.model_texture_label = QLabel("Current FX Texture: -")
        self.model_texture_label.setWordWrap(True)
        fx_layout.addWidget(self.model_texture_label)
        self.fx_combo.setMinimumWidth(0)
        self.fx_combo.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Fixed,
        )
        fx_layout.addWidget(self.fx_combo)
        self.fx_combo.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.fx_combo.customContextMenuRequested.connect(
            self._show_fx_combo_context_menu)
        self.add_fx_button = QPushButton("Add FX")
        self.add_fx_button.setEnabled(False)
        self.add_fx_button.setToolTip(
            "Add an FX from the current model, another model, or a VP-defined "
            "model in a BAS archive.")
        self.add_fx_button.clicked.connect(self.add_fx_action.trigger)
        self.clone_fx_button = QPushButton("Clone FX")
        self.clone_fx_button.setEnabled(False)
        self.clone_fx_button.clicked.connect(self.clone_fx_action.trigger)
        self.delete_fx_button = QPushButton("Delete FX")
        self.delete_fx_button.setEnabled(False)
        self.delete_fx_button.setToolTip(
            "Delete the complete editable FX element selected in the list. "
            "The operation can be undone.")
        self.delete_fx_button.clicked.connect(
            self._delete_selected_fx_element)
        self.load_texture_button = QPushButton("Replace Texture")
        self.load_texture_button.setEnabled(False)
        self.load_texture_button.setToolTip(
            "Replace the texture only on the selected polygon segments. "
            "Select multiple segments to update them together.")
        self.load_texture_button.clicked.connect(self._load_model_texture)
        fx_layout.addWidget(_ResponsiveButtonGrid([
            self.add_fx_button,
            self.clone_fx_button,
            self.delete_fx_button,
            self.load_texture_button,
        ]))
        model_layout.addWidget(fx_box)

        uv_box = QGroupBox("Texture and FX Editor")
        uv_layout = QVBoxLayout(uv_box)
        uv_layout.setContentsMargins(5, 5, 5, 5)
        self.uv_editor = UVEditorWidget()
        self.uv_editor.loopsChanged.connect(self._on_uv_changed)
        self.uv_editor.editFinished.connect(self._on_uv_edit_finished)
        self.uv_editor.selectionChanged.connect(
            self._sync_uv_add_vertex_button)
        self.uv_editor.handleContextMenuRequested.connect(
            self._show_uv_handle_context_menu)
        uv_layout.addWidget(self.uv_editor, 1)
        self.uv_phase_combo = QComboBox()
        self.uv_phase_combo.setToolTip(
            "Choose which UV phase is visible. All UV phases is the default.")
        self.uv_phase_combo.activated.connect(self._select_uv_phase)
        uv_layout.addWidget(self.uv_phase_combo)
        self.uv_auto_align_check = QCheckBox("Auto Align UV")
        self.uv_auto_align_check.setChecked(True)
        self.uv_auto_align_check.setToolTip(
            "Snap UV handles using screen-distance guides. Hold Alt while "
            "dragging to bypass snapping temporarily.")
        self.uv_auto_align_check.toggled.connect(
            self.uv_editor.set_auto_align_enabled)
        uv_layout.addWidget(self.uv_auto_align_check)
        self.uv_add_vertex_button = QPushButton("Add Vertex")
        self.uv_add_vertex_button.setEnabled(False)
        self.uv_add_vertex_button.clicked.connect(self._add_uv_vertex)
        self.uv_delete_vertex_button = QPushButton("Delete Vertex")
        self.uv_delete_vertex_button.setEnabled(False)
        self.uv_delete_vertex_button.clicked.connect(self._delete_uv_vertex)
        self.uv_revert_button = QPushButton("Revert Selected UV")
        self.uv_revert_button.setEnabled(False)
        self.uv_revert_button.clicked.connect(self._revert_selected_uv)
        self.uv_reset_all_button = QPushButton("Reset All UVs")
        self.uv_reset_all_button.setEnabled(False)
        self.uv_reset_all_button.clicked.connect(self._reset_all_uvs)
        uv_layout.addWidget(_ResponsiveButtonGrid([
            self.uv_add_vertex_button,
            self.uv_delete_vertex_button,
            self.uv_revert_button,
            self.uv_reset_all_button,
        ]))
        model_layout.addWidget(uv_box, 1)

        save_buttons = QHBoxLayout()
        save_buttons.setSpacing(5)
        self.model_save_button = QPushButton("Overwrite")
        self.model_save_button.setEnabled(False)
        self.model_save_button.clicked.connect(self._overwrite_model)
        save_buttons.addWidget(self.model_save_button, 1)
        self.model_save_as_button = QPushButton(
            "Export Asset Family")
        self.model_save_as_button.setEnabled(False)
        self.model_save_as_button.setToolTip(
            "Export a portable BASE package with SKLT, ILBM, VANM, SET "
            "palette/remaps, manifest and isolated round-trip validation.")
        self.model_save_as_button.clicked.connect(self._save_model_as)
        save_buttons.addWidget(self.model_save_as_button, 1)
        model_layout.addLayout(save_buttons)

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
        material_tools = _ResponsiveButtonGrid([
            self.material_copy_button,
            self.material_paste_button,
            self.material_add_button,
            self.material_delete_button,
        ])
        overview_layout.addWidget(material_tools)
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

        view_box = QGroupBox("View Preset")
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
        studio_layout.addWidget(view_box)

        background_box = QGroupBox("Background")
        background_layout = QGridLayout(background_box)
        background_layout.addWidget(QLabel("Custom Color:"), 0, 0)
        self.snapshot_color_button = QPushButton("None")
        self.snapshot_color_button.setFixedSize(64, 26)
        self.snapshot_color_button.setToolTip(
            "No color selected: preview uses the normal dark background and export is transparent.\n"
            "Click to choose a custom background color. In Retail indexed mode "
            "the RGB background is presentation-only; indexed rendering and "
            "TRACY composition are resolved first against palette index 0.")
        self.snapshot_color_button.clicked.connect(
            self._choose_snapshot_color)
        background_layout.addWidget(self.snapshot_color_button, 0, 1)
        self.snapshot_clear_color_button = QPushButton("Remove Color")
        self.snapshot_clear_color_button.clicked.connect(
            self._clear_snapshot_color)
        self.snapshot_clear_color_button.setEnabled(False)
        background_layout.addWidget(self.snapshot_clear_color_button, 0, 2)
        studio_layout.addWidget(background_box)

        output_box = QGroupBox("Output Size")
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

        animation_box = QGroupBox("Animation Frame")
        animation_layout = QVBoxLayout(animation_box)
        self.snapshot_frame_label = QLabel("Current Animation Frame: No animation")
        self.snapshot_frame_label.setWordWrap(True)
        animation_layout.addWidget(self.snapshot_frame_label)
        animation_buttons = QHBoxLayout()
        self.snapshot_previous_frame_button = QPushButton("<< Previous Frame")
        self.snapshot_previous_frame_button.clicked.connect(
            self.viewport.step_animation_backward)
        animation_buttons.addWidget(self.snapshot_previous_frame_button)
        self.snapshot_reset_frame_button = QPushButton("Reset Frame")
        self.snapshot_reset_frame_button.clicked.connect(
            self.viewport.reset_animation)
        animation_buttons.addWidget(self.snapshot_reset_frame_button)
        self.snapshot_next_frame_button = QPushButton("Next Frame >>")
        self.snapshot_next_frame_button.clicked.connect(
            self.viewport.step_animation)
        animation_buttons.addWidget(self.snapshot_next_frame_button)
        animation_layout.addLayout(animation_buttons)
        studio_layout.addWidget(animation_box)

        export_box = QGroupBox("Export")
        export_layout = QGridLayout(export_box)
        export_layout.addWidget(output_box, 0, 0, 1, 2)
        export_layout.addWidget(QLabel("Format:"), 1, 0)
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
        if "webp" in supported or pillow_supports_static_webp():
            self._snapshot_formats.add("webp")
            self.snapshot_format_combo.addItem("WebP (Static)", "webp")
        self.snapshot_format_combo.addItem("GIF (Animated)", "gif")
        if pillow_supports_animated_webp():
            self.snapshot_format_combo.addItem(
                "WebP (Animated)", "webp_anim")
        self.snapshot_format_combo.currentIndexChanged.connect(
            self._on_snapshot_format_changed)
        export_layout.addWidget(self.snapshot_format_combo, 1, 1)
        export_layout.addWidget(QLabel("Quality:"), 2, 0)
        self.snapshot_quality_box = PresetNumberBox(
            (100, 95, 90, 85, 80, 75, 70, 60, 50), minimum=1, maximum=100, value=95)
        self.snapshot_quality_box.setToolTip(
            "Pick a template or type any value from 1 to 100.")
        self.snapshot_quality_box.setEnabled(False)
        export_layout.addWidget(self.snapshot_quality_box, 2, 1)
        export_layout.addWidget(QLabel("Name:"), 3, 0)
        self.snapshot_naming_combo = QComboBox()
        self.snapshot_naming_combo.addItem("Resource Name", "resource")
        self.snapshot_naming_combo.addItem("VP ID", "vp_id")
        self.snapshot_naming_combo.setToolTip(
            "Use the selected model's resource name or its VP ID when one "
            "is available. Duplicate batch resource names receive an ID.")
        export_layout.addWidget(self.snapshot_naming_combo, 3, 1)
        # The three animation rows open and close as one block, with a short
        # animation: hiding them one by one made the panel jump.
        self.snapshot_animation_rows = CollapsibleSection(export_box)
        animation_grid = self.snapshot_animation_rows.grid
        animation_grid.addWidget(QLabel("Animation FPS:"), 0, 0)
        self.snapshot_fps_box = PresetNumberBox(
            (60, 50, 30, 25, 24, 15, 12, 10), minimum=1, maximum=60, value=25)
        self.snapshot_fps_box.setToolTip(
            "Output frame rate; pick a template or type 1 to 60. "
            "This does not change animation speed.")
        animation_grid.addWidget(self.snapshot_fps_box, 0, 1)
        animation_grid.addWidget(QLabel("Animation Speed:"), 1, 0)
        self.snapshot_export_speed_box = PresetNumberBox(
            (0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0), minimum=0.05, maximum=8.0,
            decimals=2, value=1.0)
        self.snapshot_export_speed_box.setToolTip(
            "Animation playback speed used while capturing frames; "
            "pick a template or type 0,05 to 8.")
        animation_grid.addWidget(self.snapshot_export_speed_box, 1, 1)
        self.snapshot_duration_check = QCheckBox("Fixed duration:")
        self.snapshot_duration_check.setToolTip(
            "Off: export one cycle using the duration reported by the "
            "viewport. On: export for a fixed number of seconds.")
        self.snapshot_duration_box = PresetNumberBox(
            (1, 2, 3, 5, 10, 15, 30, 60), minimum=0.1, maximum=3600.0, decimals=1,
            suffix=" s", value=5.0)
        self.snapshot_duration_box.setToolTip(
            "Pick a template or type the seconds (0,1 to 3600).")
        self.snapshot_duration_box.setEnabled(False)
        self.snapshot_duration_check.toggled.connect(
            self.snapshot_duration_box.setEnabled)
        animation_grid.addWidget(self.snapshot_duration_check, 2, 0)
        animation_grid.addWidget(self.snapshot_duration_box, 2, 1)
        # Same equal columns as the rows around the block, so labels align.
        for grid in (animation_grid, export_layout):
            grid.setColumnStretch(0, 1)
            grid.setColumnStretch(1, 1)
        export_layout.addWidget(self.snapshot_animation_rows, 4, 0, 3, 2)
        self.snapshot_export_button = QPushButton("Export Image")
        self.snapshot_export_button.clicked.connect(self._export_snapshot)
        export_layout.addWidget(self.snapshot_export_button, 7, 0, 1, 2)
        self.snapshot_figurine_button = QPushButton("Export Image Set")
        self.snapshot_figurine_button.setToolTip(
            "Export the current model in the selected format from every "
            "canonical angle. "
            "Uses the selected background color, or transparent "
            "alpha when no background is selected.")
        self.snapshot_figurine_button.clicked.connect(
            self._export_figurine_set)
        export_layout.addWidget(self.snapshot_figurine_button, 8, 0, 1, 2)
        self.snapshot_contact_sheet_button = QPushButton(
            "Export Contact Sheet")
        self.snapshot_contact_sheet_button.setToolTip(
            "Export the current model as one PNG that tiles the standard "
            "views (iso, iso_left, iso_back, iso_back_left, left, right, "
            "top, bottom), each with its name in the corner. Uses the "
            "selected background color, or dark grey when none is selected.")
        self.snapshot_contact_sheet_button.clicked.connect(
            self._export_contact_sheet)
        export_layout.addWidget(self.snapshot_contact_sheet_button, 9, 0, 1, 2)
        self._snapshot_export_layout = export_layout
        self._snapshot_export_box = export_box
        self._on_snapshot_format_changed(
            self.snapshot_format_combo.currentIndex())
        studio_layout.addWidget(export_box)
        studio_layout.addStretch(1)
        snapshot_layout.addWidget(studio_box)
        snapshot_layout.addStretch(1)

        def category_tabs() -> QTabWidget:
            # Categories are flattened into the workspace palette below.
            category = QTabWidget()
            category.setDocumentMode(True)
            return category

        resources_tabs = category_tabs()
        resources_tabs.addTab(setbas_panel, "Bas Manager")
        resources_tabs.addTab(asset_panel, "Asset Dependencies")

        # The model editor is taller than the normal windowed viewport.
        # Keep its controls in document flow and scroll the page instead of
        # letting Qt compress the layout below its minimum height, which can
        # make the UV toolbar overlap the texture preview on Windows.
        model_scroll = _ViewportWidthScrollArea()
        model_scroll.setWidgetResizable(True)
        model_scroll.setFrameShape(QFrame.Shape.NoFrame)
        model_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        model_scroll.setWidget(model_panel)

        editor_tabs = category_tabs()
        editor_tabs.addTab(model_scroll, "Models and Textures")

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
        self._model_editor_panel = model_scroll
        self._mapping_panel = mapping_panel
        self._snapshot_panel = snapshot_panel
        editor_tabs.currentChanged.connect(self._on_nested_tab_changed)
        visuals_tabs.currentChanged.connect(self._on_nested_tab_changed)
        tabs.currentChanged.connect(self._on_right_tab_changed)
        tabs.setCurrentWidget(resources_tabs)
        # One flat palette of buttons for every page and sub-page.
        PaletteTabStrip.install(tabs)
        self._update_snapshot_color_button()
        self._sync_animation_controls()

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(2)
        right_layout.addWidget(tabs, 1)

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
        center_layout.addWidget(self.viewport, 1)

        main_split = QSplitter(Qt.Orientation.Horizontal)
        main_split.setChildrenCollapsible(False)
        main_split.setMinimumHeight(0)
        main_split.setSizePolicy(QSizePolicy.Policy.Expanding,
                                 QSizePolicy.Policy.Ignored)
        main_split.addWidget(center_panel)
        main_split.addWidget(right_panel)
        main_split.setStretchFactor(0, 15)
        main_split.setStretchFactor(1, 7)
        main_split.setSizes([900, 420])
        self._main_splitter = main_split
        # Diagnostics live inside the central vertical splitter instead of a
        # QDockWidget.  A dock can increase the main window's minimum height on
        # Windows and push a maximized window below the desktop.  This panel
        # always consumes space *inside* the current window geometry.
        diagnostics_tabs = QTabWidget()
        diagnostics_tabs.setDocumentMode(True)
        diagnostics_tabs.setMinimumHeight(0)
        diagnostics_tabs.setSizePolicy(QSizePolicy.Policy.Expanding,
                                       QSizePolicy.Policy.Ignored)
        diagnostics_tabs.addTab(self.log_list, "Log")
        diagnostics_tabs.addTab(self.warning_list, "Warnings")
        diagnostics_tabs.addTab(self.checks_list, "Validation")
        diagnostics_tabs.setCurrentIndex(0)
        PaletteTabStrip.install(diagnostics_tabs)
        for diagnostic_list in (self.log_list, self.warning_list,
                                self.checks_list):
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

        self._diagnostics_tabs = diagnostics_tabs
        self._diagnostics_dock = diagnostics_panel
        self._diagnostics_splitter = vertical_split
        # Diagnostics are opt-in at startup.  Show Diagnostic Panel reopens
        # the same compact 80 px panel with Log as the default tab.
        diagnostics_panel.hide()

    # -- actions ---------------------------------------------------------------

    def _show_diagnostics(self, index: int | None = None) -> None:
        """Show the shared diagnostics panel without duplicating menu routes."""

        if self._diagnostics_tabs is not None and index is not None:
            index = max(0, min(index,
                               self._diagnostics_tabs.count() - 1))
            self._diagnostics_tabs.setCurrentIndex(index)
        if self._diagnostics_dock is None:
            return
        was_visible = self._diagnostics_dock.isVisible()
        self._diagnostics_dock.show()
        self._sync_diagnostics_menu_label()
        splitter = getattr(self, "_diagnostics_splitter", None)
        if splitter is None:
            return
        sizes = splitter.sizes()
        should_fit = (
            not self._diagnostics_initialized
            or not was_visible
            or not sizes
            or sizes[-1] <= 1)
        self._diagnostics_initialized = True
        if not should_fit:
            return

        def fit_inside_window() -> None:
            target = min(80, max(1, splitter.height() - 1))
            splitter.setSizes([max(1, splitter.height() - target), target])

        QTimer.singleShot(0, fit_inside_window)

    def _hide_diagnostics(self) -> None:
        if self._diagnostics_dock is not None:
            self._diagnostics_dock.hide()
        self._sync_diagnostics_menu_label()

    def _sync_diagnostics_menu_label(self) -> None:
        action = getattr(self, "diagnostics_toggle_action", None)
        panel = getattr(self, "_diagnostics_dock", None)
        if action is not None:
            action.setText(
                "Hide Diagnostic Panel"
                if panel is not None and panel.isVisible()
                else "Show Diagnostic Panel")

    def _toggle_diagnostics_panel(self) -> None:
        panel = getattr(self, "_diagnostics_dock", None)
        if panel is not None and panel.isVisible():
            self._hide_diagnostics()
        else:
            self._show_diagnostics()

    def _clear_diagnostics(self) -> None:
        """Clear the shared diagnostic log without erasing current findings."""

        self.log_list.clear()
        self.statusBar().showMessage("Log cleared.", 2000)

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
            self._notify(
                "No extraction or conversion output folder is available yet.",
                7000)
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

    def _open_entity_designer(self) -> None:
        try:
            from entity_designer import CollisionEditorWindow
        except Exception as exc:
            QMessageBox.critical(
                self, "Entity Designer unavailable",
                f"The integrated editor could not be loaded.\n\n{exc}")
            return
        window = CollisionEditorWindow()
        window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._collision_windows.append(window)

        def forget(*_args) -> None:
            if window in self._collision_windows:
                self._collision_windows.remove(window)

        window.destroyed.connect(forget)
        window.show()
        window.raise_()
        window.activateWindow()

    def _open_map_editor(self) -> None:
        """Open the integrated Map Editor in this Qt application."""

        try:
            from map_editor.editor import create_window
            window = create_window(parent=self)
        except Exception as exc:
            QMessageBox.critical(
                self, "Map Editor unavailable",
                f"The integrated editor could not be loaded.\n\n{exc}")
            return
        if window is None:
            return
        window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._map_editor_windows.append(window)

        def forget(*_args) -> None:
            if window in self._map_editor_windows:
                self._map_editor_windows.remove(window)

        window.destroyed.connect(forget)
        window.show()
        window.raise_()
        window.activateWindow()

    def open_bas_archive_dialog(self) -> None:
        """Import a read-only SET.BAS resource archive."""

        path = choose_bas_archive(self, self._last_directory)
        if not path:
            return
        from studio.ui.widgets import loading_resources
        with loading_resources(self, 'Loading BAS archive. Please wait…'):
            self.open_setbas(Path(path))

    def open_base(self, path: str | Path, *, confirm_discard: bool = True) -> None:
        base_path = Path(path)
        if base_path.name.casefold() == "set.bas" and confirm_discard:
            self.open_setbas(base_path)
            return
        if confirm_discard and not self._confirm_discard_geometry():
            return
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

    def _open_manual_asset_family(
            self, sklt: str | Path | None = None,
            base: str | Path | None = None,
            textures: list[str | Path] | None = None,
            anms: list[str | Path] | None = None) -> None:
        textures = list(textures or [])
        anms = list(anms or [])
        if not sklt and not base and not textures and not anms:
            return
        if not self._confirm_discard_geometry():
            return
        primary_path = base or sklt or (textures[0] if textures else None) \
            or (anms[0] if anms else None)
        if primary_path:
            self._last_directory = Path(primary_path).parent
        try:
            family = load_manual_family(
                str(sklt) if sklt else None,
                [str(path) for path in textures],
                [str(path) for path in anms],
                str(base) if base else None,
                self._extra_roots,
                self._overrides,
                self._setbas,
            )
        except Exception as exc:
            QMessageBox.critical(
                self, "Load failed",
                f"No file was modified.\n\nUnexpected error:\n{exc}",
            )
            return
        self._set_family(family)
        if primary_path:
            self._set_document_title(primary_path)

    def open_family_dialog(self) -> None:
        # Keep the chooser anchored to the most recently selected file during
        # this one Asset Family import.  Optional steps may be skipped, but a
        # successful selection becomes the starting directory for the next
        # requested asset type instead of resetting to the session directory.
        dialog_directory = Path(self._last_directory)

        base, _ = QFileDialog.getOpenFileName(
            self, "Import BASE entry point - optional, Cancel to skip",
            str(dialog_directory),
            "Urban Assault BASE (*.base *.bas *.BASE *.BAS);;All files (*)",
        )
        if base:
            dialog_directory = Path(base).parent

        sklt, _ = QFileDialog.getOpenFileName(
            self, "Import skeleton (.sklt/.skl) - optional, Cancel to skip",
            str(dialog_directory),
            "Skeletons (*.sklt *.skl *.SKLT *.SKL);;All files (*)",
        )
        if sklt:
            dialog_directory = Path(sklt).parent

        textures, _ = QFileDialog.getOpenFileNames(
            self, "Import texture files (.ilbm/.ilb/.vbmp) - optional",
            str(dialog_directory),
            "ILBM/VBMP textures (*.ilbm *.ilb *.lbm *.iff *.vbmp "
            "*.ILBM *.ILB *.LBM *.IFF *.VBMP);;All files (*)",
        )
        if textures:
            dialog_directory = Path(textures[0]).parent

        anms, _ = QFileDialog.getOpenFileNames(
            self, "Import animation files (.anm/.vanm) - optional",
            str(dialog_directory),
            "Animations (*.anm *.vanm *.ANM *.VANM);;All files (*)",
        )
        self._open_manual_asset_family(sklt, base, textures, anms)

    def _sync_close_archive_action(self) -> None:
        """Enable Close Current Resource while any Studio resource is open."""

        action = getattr(self, "close_bas_archive_action", None)
        if action is not None:
            action.setEnabled(self._setbas is not None or self._family is not None)

    def _close_archive_block_reason(self) -> str:
        """Workspace hook for operations that must finish before closing."""

        return ""

    def close_current_archive(self) -> None:
        """Unload the current resource and return to the empty workbench.

        This is deliberately a document close, not an application restart:
        user view preferences and session-level settings stay intact, while
        every resource/family/preview/edit state owned by the open document is
        discarded. SET.BAS is never modified.
        """

        if self._setbas is None and self._family is None:
            self._sync_close_archive_action()
            return
        reason = self._close_archive_block_reason()
        if reason:
            self._notify(reason, 10000)
            return
        if not self._confirm_discard_geometry():
            return

        self._cancel_live_transforms()
        for dialog in list(self._preview_windows):
            try:
                dialog.close()
            except RuntimeError:
                pass
        self._preview_windows.clear()
        for attr in ("_base_dependency_dialog", "_mapping_dialog"):
            dialog = getattr(self, attr, None)
            if dialog is not None:
                try:
                    dialog.close()
                except RuntimeError:
                    pass
            setattr(self, attr, None)

        # Clear the shared viewport first so no stale edit/animation session can
        # reference objects that are about to be detached from the workbench.
        self.viewport.clear()

        self._family = None
        self._setbas = None
        self._setbas_preview_cache = None
        self._vp_embedded = None
        self._vp_source_path = None
        self._overrides.clear()
        self._trial_names.clear()
        self._kept_names.clear()
        self._skipped_names.clear()
        self._large_mode = False
        self._selected_owner = None
        self._owner_to_obj.clear()
        self._owner_to_item.clear()
        self._base_entry_names.clear()
        self._last_component_selection = None
        self._asset_focus_owner = None
        self._asset_focus_restoring = False
        self._asset_filter_expansion = None
        self._mapping_index = None
        self._workbench_obj = None
        self._selected_poly = None
        self._selected_polys.clear()
        self._mirror_select_source_polys.clear()
        self._repair_plan = None
        self._pending_repairs.clear()
        self._saved_repair_path = None
        self._geom_dirty.clear()
        self._geom_original.clear()
        self._topology_original.clear()
        self._geometry_clipboard = None
        self._material_clipboard = None
        self._uv_ctx = None
        self._uv_contexts.clear()
        self._uv_original.clear()
        self._vanm_uv_original.clear()
        self._texture_original.clear()
        self._tab_edit_selection.clear()
        self._setbas_context_item = None
        self._setbas_preview_active = False
        self._fx_preview_cache.clear()
        self._picked_structure = None
        self._geometry_writer_capability_cache.clear()
        self._edit_undo_stack.clear()
        self._edit_redo_stack.clear()
        self._uv_history_before = None
        self._fx_elements.clear()
        self._bundle_targets.clear()
        self._bundle_base_snapshots.clear()
        self._texture_preview_cache.clear()
        self._texture_qimage_cache.clear()
        self._object_info_polygon_lines = []

        # Resource browsers and diagnostics belonging to the closed document.
        self.setbas_tree.clear()
        self.setbas_search.clear()
        self.asset_tree.clear()
        self.tree_search.clear()
        self.texture_list.clear()
        self.chunk_tree.clear()
        self.blocks_list.clear()
        self.repair_preview.clear()
        self.warning_list.clear()
        self.checks_list.clear()
        self._set_object_info(["No asset selected."])

        self._set_vp_table(None, "unavailable")
        self.vp_selected_base_label.setText("-")
        self.vp_current_label.setText("-")
        self.setbas_extract_button.setEnabled(False)
        self.setbas_extract_all_button.setEnabled(False)
        self.setbas_runtime_loose_button.setEnabled(False)
        self.setbas_onua3d_button.setEnabled(False)
        self.setbas_edit_dependencies_button.setEnabled(False)
        self.export_runtime_loose_action.setEnabled(False)

        self._sync_animation_controls()
        self._sync_geometry_save_controls()
        self._update_editor_status()
        self._update_reset_camera_action()
        self._sync_close_archive_action()
        self._set_document_title(None)
        self._notify(
            "Current resource closed. The workbench is empty.", 7000)


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
        self._texture_preview_cache.clear()
        self._setbas = archive
        self._setbas_preview_cache = None
        self._sync_close_archive_action()
        self._set_document_title(archive.path)
        self._load_vp_table(archive)
        self._fill_setbas(archive)
        # Keep archive-only ILBMs visible even when no SKLT resource can be
        # previewed into an AssetFamily.
        self._fill_textures(None)
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
        self._preview_first_setbas_base()
        synchronize = getattr(self, '_sync_imported_set', None)
        if synchronize is not None:
            synchronize(archive.path)

    def _set_vp_table(
            self, table: VPTable | None, source: str) -> None:
        self._vp_table = table
        self._vp_source = source
        label = getattr(self, "vp_source_label", None)
        if label is not None:
            count = len(table) if table is not None else 0
            label.setText(
                f"VP source: {source}"
                + (f" ({count} slots)" if table is not None else ""))
            issues = self._vp_validation_issues()
            label.setToolTip("\n".join(issues) if issues else
                             "Read-only VP IDs reconstructed from the "
                             "embedded visproto.base; IDs are zero-based.")
        self._sync_vp_controls()

    def _load_vp_table(self, archive: SetBasArchive) -> None:
        """Reconstruct the read-only VP table embedded in SET.BAS."""

        self._vp_embedded = None
        self._vp_source_path = Path(archive.path)
        try:
            self._vp_embedded = reconstruct_embedded_vps(archive.path)
        except (VPEmbeddedError, BaseParseError, OSError, ValueError) as exc:
            self._log(f"VP table: embedded reconstruction unavailable: {exc}")

        table = None
        source = "unavailable"
        if self._vp_embedded is not None:
            table = self._vp_embedded.as_table()
            source = "embedded visproto.base"
        self._set_vp_table(table, source)
        self._log(f"VP table: {source}")

    def _vp_base_skeletons(self) -> dict[str, set[str]]:
        values: dict[str, set[str]] = {}
        embedded = self._vp_embedded
        if embedded is None:
            return values
        for entry in embedded.entries:
            base_key = normalize_base_name(entry.base_name)
            skeleton_key = normalize_skeleton_name(entry.skeleton_name)
            if skeleton_key:
                values.setdefault(base_key, set()).add(skeleton_key)
        return values

    def _vp_text_for_base(self, base_name: str) -> str:
        table = self._vp_table
        if table is None:
            return "?"
        indices = table.vps_for_base(base_name)
        return ", ".join(str(index) for index in indices)

    def _vp_text_for_skeleton(self, skeleton_name: str) -> str:
        table = self._vp_table
        if table is None or self._vp_embedded is None:
            return "?"
        target = normalize_skeleton_name(skeleton_name)
        by_base = self._vp_base_skeletons()
        indices = []
        ambiguous = False
        for entry in table:
            candidates = by_base.get(normalize_base_name(entry.base_name), set())
            if target not in candidates:
                continue
            if len(candidates) != 1:
                ambiguous = True
            else:
                indices.append(entry.index)
        if ambiguous:
            return "?"
        return ", ".join(str(index) for index in indices)

    def _vp_validation_issues(self) -> list[str]:
        table = self._vp_table
        if table is None:
            return ["VP table unavailable."]
        issues = []
        seen: dict[str, list[int]] = {}
        for entry in table:
            seen.setdefault(
                normalize_base_name(entry.base_name), []).append(entry.index)
        duplicates = {
            name: slots for name, slots in seen.items()
            if len(slots) > 1 and name != "dummy.base"
        }
        if duplicates:
            sample = ", ".join(
                f"{name} ({','.join(map(str, slots))})"
                for name, slots in list(duplicates.items())[:5])
            issues.append(f"Duplicate non-dummy BASE assignments: {sample}.")
        if self._vp_embedded is None:
            issues.append(
                "Embedded BASE availability cannot be validated.")
        else:
            available = {
                normalize_base_name(entry.base_name)
                for entry in self._vp_embedded.entries
            }
            missing = [
                entry for entry in table
                if normalize_base_name(entry.base_name) not in available
            ]
            if missing:
                sample = ", ".join(
                    f"VP {entry.index}: {entry.base_name}"
                    for entry in missing[:8])
                issues.append(f"Missing BASE entries: {sample}.")
        return issues

    def _current_vp_base_candidates(self) -> list[str]:
        item = self.setbas_tree.currentItem()
        table = self._vp_table
        if item is None or table is None:
            return []
        kind = item.data(0, _BAS_KIND_ROLE)
        name = item.data(0, _BAS_NAME_ROLE) or item.text(0)
        if kind == "base":
            return [str(name)]
        if kind != "sklt.class" or self._vp_embedded is None:
            return []
        target = normalize_skeleton_name(str(name))
        by_base = self._vp_base_skeletons()
        candidates = []
        for entry in table:
            base_name = entry.base_name
            skeletons = by_base.get(normalize_base_name(base_name), set())
            if target in skeletons and base_name not in candidates:
                candidates.append(base_name)
        return candidates

    def _sync_vp_controls(self) -> None:
        table = self._vp_table
        table_available = table is not None and bool(len(table))
        item = self.setbas_tree.currentItem() \
            if hasattr(self, "setbas_tree") else None
        kind = item.data(0, _BAS_KIND_ROLE) if item is not None else None
        selected_name = "-"
        if item is not None and kind != "group":
            selected_name = str(
                item.data(0, _BAS_NAME_ROLE) or item.text(0) or "-")
        candidates = self._current_vp_base_candidates() \
            if table_available and item is not None else []
        if hasattr(self, "vp_selected_base_label"):
            self.vp_selected_base_label.setText(selected_name)
            if not candidates:
                self.vp_current_label.setText("-")
            elif len(candidates) == 1:
                base_name = candidates[0]
                indices = table.vps_for_base(base_name)
                self.vp_current_label.setText(
                    ", ".join(map(str, indices)) or "not assigned")
            else:
                self.vp_current_label.setText(
                    "multiple embedded VP parents")
        resource_index = (
            item.data(0, Qt.ItemDataRole.UserRole)
            if item is not None else None)
        self.setbas_extract_button.setEnabled(resource_index is not None)
        base_selected = self._setbas is not None and kind == "base"
        onua3d_button = getattr(self, "setbas_onua3d_button", None)
        if onua3d_button is not None:
            onua3d_button.setEnabled(base_selected)
        edit_dependencies = getattr(
            self, "setbas_edit_dependencies_button", None)
        if edit_dependencies is not None:
            edit_dependencies.setEnabled(base_selected)

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
            group_hit = any(
                text in group.text(column).lower()
                for column in range(self.setbas_tree.columnCount()))
            any_child = False
            for j in range(group.childCount()):
                child = group.child(j)
                hit = (not text) or group_hit \
                    or any(
                        text in child.text(column).lower()
                        for column in range(
                            self.setbas_tree.columnCount()))
                child.setHidden(not hit)
                any_child = any_child or hit
            group.setHidden(bool(text) and not any_child and not group_hit)
            if text and any_child:
                group.setExpanded(True)

    def _fill_setbas(self, archive: SetBasArchive) -> None:
        self.setbas_tree.clear()
        census = archive.census()
        groups: dict[str, QTreeWidgetItem] = {}
        class_ids = ["base.class"] + [
            class_id for class_id in census
            if class_id.casefold() != "base.class"
        ] + ["other/unknown"]
        for class_id in class_ids:
            item = QTreeWidgetItem([class_id, "", ""])
            # Category rows are expand/collapse controls, not resources.  Keep
            # them out of the selection model so Qt does not draw the accent
            # focus stripe over the branch arrow.
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            item.setData(0, _BAS_KIND_ROLE, "group")
            groups[class_id] = item
            self.setbas_tree.addTopLevelItem(item)

        base_names = []
        if self._vp_table is not None:
            base_names.extend(
                entry.base_name for entry in self._vp_table)
        if self._vp_embedded is not None:
            base_names.extend(
                entry.base_name for entry in self._vp_embedded.entries)
        unique_bases = []
        seen_bases = set()
        for base_name in base_names:
            key = normalize_base_name(base_name)
            if key in seen_bases:
                continue
            seen_bases.add(key)
            unique_bases.append(base_name)
        embedded_names = (
            {
                normalize_base_name(entry.base_name)
                for entry in self._vp_embedded.entries
            }
            if self._vp_embedded is not None else set())
        for base_name in unique_bases:
            normalized = normalize_base_name(base_name)
            payload = "BASE object"
            if self._vp_embedded is not None \
                    and normalized not in embedded_names:
                payload += " (missing)"
            row = QTreeWidgetItem([
                base_name, payload, self._vp_text_for_base(base_name)])
            for column in range(3):
                row.setTextAlignment(
                    column,
                    Qt.AlignmentFlag.AlignLeft
                    | Qt.AlignmentFlag.AlignVCenter)
            row.setData(0, _BAS_KIND_ROLE, "base")
            row.setData(0, _BAS_NAME_ROLE, base_name)
            groups["base.class"].addChild(row)

        for resource in archive.resources:
            group = groups.get(resource.class_id,
                               groups.get("other/unknown"))
            row = QTreeWidgetItem([
                resource.resource_name,
                resource.display_payload + (" (unsupported class)"
                                            if not resource.decodable
                                            and not resource.error else ""),
                (self._vp_text_for_skeleton(resource.resource_name)
                 if resource.class_id.casefold() == "sklt.class" else ""),
            ])
            if resource.error:
                row.setText(1, f"ERROR: {resource.error}")
            for column in range(3):
                row.setTextAlignment(
                    column,
                    Qt.AlignmentFlag.AlignLeft
                    | Qt.AlignmentFlag.AlignVCenter,
                )
            row.setData(0, Qt.ItemDataRole.UserRole, resource.index)
            row.setData(
                0, _BAS_KIND_ROLE, resource.class_id.casefold())
            row.setData(0, _BAS_NAME_ROLE, resource.resource_name)
            group.addChild(row)
        for class_id, item in groups.items():
            noun = "BASE objects" if class_id == "base.class" \
                else "resources"
            item.setText(1, f"{item.childCount()} {noun}")
            for column in range(3):
                item.setTextAlignment(
                    column,
                    Qt.AlignmentFlag.AlignLeft
                    | Qt.AlignmentFlag.AlignVCenter,
                )
            if item.childCount() == 0:
                index = self.setbas_tree.indexOfTopLevelItem(item)
                self.setbas_tree.takeTopLevelItem(index)
        self.setbas_tree.collapseAll()
        self.setbas_extract_all_button.setEnabled(True)
        self.setbas_runtime_loose_button.setEnabled(True)
        self.export_runtime_loose_action.setEnabled(True)
        self._sync_vp_controls()

    @staticmethod
    def _resource_names_match(left: str, right: str) -> bool:
        """Compare archive/dependency names without assuming one path style."""

        def variants(value: str) -> set[str]:
            normalized = str(value or "").strip().replace("\\", "/").casefold()
            if not normalized:
                return set()
            return {normalized, normalized.rsplit("/", 1)[-1]}

        return bool(variants(left).intersection(variants(right)))

    def _setbas_preview_family(self):
        """Return the archive family used by BASE/SKLT/VANM previews."""

        if self._setbas is None:
            return None
        archive_path = Path(self._setbas.path)
        if self._family is not None \
                and self._family.base_path == archive_path:
            return self._family
        cached = getattr(self, "_setbas_preview_cache", None)
        if cached is not None and cached[0] is self._setbas:
            return cached[1]
        return load_asset_family(
            archive_path, self._extra_roots, {}, self._setbas)

    def _animation_owners(self, family, animation_name: str) -> list[str]:
        owners: list[str] = []
        for fam_obj in family.all_objects():
            base_object = getattr(fam_obj, "base_object", None)
            for block in (visual for ade in getattr(base_object, "ades", ())
                          for visual in ade.iter_visual_blocks()):
                for texture in (getattr(block, "texture", None),
                                getattr(block, "tracy_texture", None)):
                    if texture is None \
                            or str(getattr(texture, "kind", "")).casefold() \
                            != "bmpanim":
                        continue
                    if self._resource_names_match(
                            getattr(texture, "name", ""), animation_name):
                        if fam_obj.owner_path not in owners:
                            owners.append(fam_obj.owner_path)
                        break
        return owners

    def _preview_setbas_animation(self, resource) -> None:
        """Preview a VANM on its model, including nested particle stages."""

        if self._setbas is None or resource is None:
            return
        try:
            family = self._setbas_preview_family()
        except Exception as exc:
            QMessageBox.warning(
                self, "Animation preview failed",
                f"{resource.resource_name} could not be loaded.\n\n{exc}")
            return
        if family is None:
            return

        animation_name = next((
            name for name in family.animations
            if self._resource_names_match(name, resource.resource_name)
        ), None)
        owners = self._animation_owners(family, resource.resource_name)
        if animation_name is None:
            QMessageBox.information(
                self, "Animation preview unavailable",
                f"{resource.resource_name} is embedded in SET.BAS, but no "
                "valid animation could be decoded from it.")
            self._raise_setbas_tab()
            return

        if not owners:
            from studio.assets.family import animation_preview_family
            try:
                preview = animation_preview_family(family, animation_name)
            except ValueError as exc:
                self.statusBar().showMessage(str(exc), 8000)
                return
            self._setbas_preview_cache = self._setbas, family
            self._set_family(preview)
            self._raise_setbas_tab()
            return

        owner = (self._selected_owner if self._selected_owner in owners
                 else owners[0])
        if family is not self._family:
            self._set_family(family)
        self._select_owner(owner, preserve_asset_selection=True)
        self._apply_selected_children_scope()
        self._sync_animation_controls()
        if not self.viewport.has_animation:
            self.statusBar().showMessage(
                f"Animation {animation_name} is not available in the "
                "selected model preview.", 8000)
            self._raise_setbas_tab()
            return
        self._raise_setbas_tab()
        self.statusBar().showMessage(
            f"Previewing {animation_name} on {owner}", 8000)

    def _on_setbas_item_selected(self, current, _previous=None) -> None:
        """Preview every supported BAS row selected by mouse or keyboard."""

        if current is None or self._setbas is None \
                or self._setbas_preview_active:
            return
        kind = current.data(0, _BAS_KIND_ROLE)
        if kind not in ("base", "sklt.class", "ilbm.class",
                        "bmpanim.class"):
            return
        self._setbas_preview_active = True
        try:
            self.play_button.setChecked(False)
            self._preview_setbas_resource(current)
        finally:
            self._setbas_preview_active = False

    def _on_setbas_item_double_clicked(self, item, _column: int) -> None:
        if item is None:
            return
        kind = item.data(0, _BAS_KIND_ROLE)
        index = item.data(0, Qt.ItemDataRole.UserRole)
        if kind == "base" or index is not None:
            if self.setbas_tree.currentItem() is item:
                return
            self.setbas_tree.setCurrentItem(item)
            return
        # Class/group rows keep QTreeWidget's normal expand/collapse.
        self._sync_vp_controls()

    def _preview_first_setbas_base(self) -> None:
        """Open the BASE group and preview its first complete model."""
        for group_index in range(self.setbas_tree.topLevelItemCount()):
            group = self.setbas_tree.topLevelItem(group_index)
            if group.text(0).casefold() == "base.class" and group.childCount():
                group.setExpanded(True)
                item = group.child(0)
                self.setbas_tree.setCurrentItem(item)
                self.setbas_tree.scrollToItem(item)
                return

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        browser = getattr(self, "entity_browser", None)
        trees = (self.setbas_tree, getattr(browser, "tree", None))
        if (watched in trees and event.type() == QEvent.Type.KeyPress
                and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and event.modifiers() == Qt.KeyboardModifier.NoModifier
                and watched.state() != QAbstractItemView.State.EditingState):
            item = watched.currentItem()
            if item is None or (watched is self.setbas_tree and item.data(
                    0, _BAS_KIND_ROLE) not in ("base", "sklt.class", "bmpanim.class")):
                return super().eventFilter(watched, event)
            if not event.isAutoRepeat():
                self._toggle_list_animation()
            return True
        return super().eventFilter(watched, event)

    def _toggle_list_animation(self) -> None:
        """Use the same Play/Pause command from both resource lists."""
        if self.viewport.has_animation:
            self.play_button.setChecked(not self.play_button.isChecked())

    def _play_setbas_item(self, item) -> None:
        if item is None:
            return
        self.setbas_tree.setCurrentItem(item)
        self._toggle_list_animation()

    def _setbas_item_has_animation(self, item) -> bool:
        if item is None or self._setbas is None:
            return False
        kind = item.data(0, _BAS_KIND_ROLE)
        if kind not in ("base", "sklt.class", "bmpanim.class"):
            return False
        family = self._setbas_preview_family()
        if family is None:
            return False
        name = item.data(0, _BAS_NAME_ROLE) or item.text(0)
        if kind == "bmpanim.class":
            return any(self._resource_names_match(key, name) and value.frames
                       for key, value in family.animations.items())
        if kind == "base":
            _family, target, _offset = self._resolve_setbas_base(name)
            objects = [target] if target is not None else []
        else:
            objects = [obj for obj in family.all_objects()
                       if obj.skeleton is not None and self._resource_names_match(
                           obj.base_object.skeleton_name, name)]
        return any(family_object_has_animation(family, obj) for obj in objects)

    def _setbas_palette(self):
        """Palette for embedded VBMP previews and conversions."""

        if self._setbas is None:
            return None, ""
        from studio.formats.images import parse_pal_file
        from studio.assets.setbas_export import find_external_palette
        from studio.formats.images import (
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

    def _preview_setbas_resource(self, item=None) -> None:
        """Preview the selected BASE, embedded SKLT or ILBM resource."""

        if self._setbas is None:
            return
        item = item or self.setbas_tree.currentItem()
        if item is None:
            QMessageBox.information(
                self, "No resource selected",
                "Select a BASE, SKLT skeleton or ILBM/VBMP texture first.")
            return
        kind = item.data(0, _BAS_KIND_ROLE)
        if kind == "base":
            base_name = item.data(0, _BAS_NAME_ROLE) or item.text(0)
            self._preview_setbas_base(str(base_name))
            return
        index = item.data(0, Qt.ItemDataRole.UserRole)
        if index is None:
            QMessageBox.information(
                self, "No resource selected",
                "Select a BASE, SKLT skeleton or ILBM/VBMP texture first.")
            return
        resource = self._setbas.resources[index]
        class_id = resource.class_id.lower()
        if class_id == "sklt.class":
            self._preview_setbas_skeleton(resource=resource)
            return
        if class_id == "ilbm.class":
            self._preview_setbas_texture(resource)
            return
        if class_id == "bmpanim.class":
            self._preview_setbas_animation(resource)
            return
        QMessageBox.information(
            self,
            "Preview unavailable",
            f"{resource.resource_name} is {resource.class_id}.\n\n"
            "Preview supports BASE objects, SKLT skeletons, VANM "
            "animations and ILBM/VBMP textures.",
        )

    def _resolve_setbas_base(self, base_name: str):
        """Return the family/object proven by the VP-to-OBJT mapping."""

        if self._setbas is None:
            return None, None, None
        archive_path = Path(self._setbas.path)
        try:
            if self._family is not None \
                    and self._family.base_path == archive_path:
                family = self._family
            else:
                family = load_asset_family(
                    archive_path, self._extra_roots, {}, self._setbas)
        except Exception as exc:
            QMessageBox.warning(
                self, "BASE preview failed",
                f"{base_name} could not be loaded.\n\n{exc}")
            return None, None, None

        source_offset = None
        if self._vp_embedded is not None:
            entry = next((candidate for candidate in self._vp_embedded.entries
                          if candidate.normalized_base
                          == normalize_base_name(base_name)), None)
            if entry is not None and entry.source_objt_offset >= 0:
                source_offset = entry.source_objt_offset
        target = self._resolve_family_base_object(
            family, base_name, source_offset)
        return family, target, source_offset

    @staticmethod
    def _resolve_family_base_object(
            family, base_name: str, source_offset: int | None = None):
        """Resolve one BASE object by embedded offset first, then by name."""

        normalized = normalize_base_name(base_name)
        for candidate in family.all_objects():
            base_object = getattr(candidate, "base_object", None)
            if base_object is None:
                continue
            if source_offset is not None and source_offset >= 0 \
                    and getattr(base_object, "source_objt_offset", -1) \
                    == source_offset:
                return candidate
            name = str(getattr(base_object, "name", "") or "").strip()
            if not name:
                continue
            if not name.casefold().endswith(".base"):
                name += ".base"
            if normalize_base_name(name) == normalized:
                return candidate
        return None

    def _activate_setbas_base(self, base_name: str):
        family, target, source_offset = self._resolve_setbas_base(base_name)
        if target is None:
            QMessageBox.information(
                self, "BASE preview unavailable",
                f"{base_name} is listed in the VP table, but its embedded "
                "BASE object could not be resolved. No file was modified.")
            self._raise_setbas_tab()
            return None
        if family is not self._family:
            self._set_family(family)
        # The Asset Dependencies root represents one active VP BASE at a
        # time.  Do not retain older archive selections: on a later reload
        # their insertion order could otherwise focus the wrong BASE.
        self._base_entry_names.clear()
        self._base_entry_names[target.owner_path] = str(base_name)
        self._selected_owner = target.owner_path
        self._fill_asset_tree(family)
        self._select_owner(target.owner_path)
        self._apply_selected_children_scope()
        return target

    def _preview_setbas_base(self, base_name: str) -> None:
        """Preview one VP BASE object without leaving the Bas Manager tab."""

        target = self._activate_setbas_base(base_name)
        if target is None:
            return
        self._raise_setbas_tab()
        self.statusBar().showMessage(
            f"Previewing BASE {base_name} [{target.owner_path}]", 8000)

    def _show_setbas_base_dependencies(self, base_name: str) -> None:
        target = self._activate_setbas_base(base_name)
        if target is None:
            return
        self._focus_assets_for_owner(target.owner_path, switch_tabs=True)
        self.statusBar().showMessage(
            f"Asset dependencies for {base_name} [{target.owner_path}]", 8000)

    def _edit_setbas_base_dependencies(self, base_name: str) -> None:
        target = self._activate_setbas_base(base_name)
        if target is not None:
            self._edit_base_dependencies(target.owner_path)

    def _export_setbas_base_onua3d(self, item) -> None:
        """Export exactly the BASE selected in the BAS Manager as ONUA3D."""

        if self._setbas is None or item is None \
                or item.data(0, _BAS_KIND_ROLE) != "base":
            return
        base_name = str(
            item.data(0, _BAS_NAME_ROLE) or item.text(0) or "").strip()
        if not base_name:
            self._notify("The selected BASE has no usable name.", 6000)
            return
        target = self._activate_setbas_base(base_name)
        if target is None:
            return
        self._selected_owner = target.owner_path
        self._export_to_blender()

    def _export_selected_setbas_base_onua3d(self) -> None:
        """Button entry point for exporting the selected BAS BASE."""

        item = self.setbas_tree.currentItem()
        if item is None or item.data(0, _BAS_KIND_ROLE) != "base":
            self._notify(
                "Select a BASE entry before exporting OpenNeoUA 3D.", 6000)
            return
        self._export_setbas_base_onua3d(item)

    def _edit_selected_setbas_base_dependencies(self) -> None:
        """Open the same BASE dependency editor used by the context menu."""

        item = self.setbas_tree.currentItem()
        if item is None or item.data(0, _BAS_KIND_ROLE) != "base":
            self._notify(
                "Select a BASE entry before editing its dependencies.", 6000)
            return
        base_name = str(
            item.data(0, _BAS_NAME_ROLE) or item.text(0) or "").strip()
        if not base_name:
            self._notify("The selected BASE has no usable name.", 6000)
            return
        self._edit_setbas_base_dependencies(base_name)

    def _export_setbas_base_family(self, base_name: str) -> None:
        """Export exactly the BASE family selected in the archive browser."""

        target = self._activate_setbas_base(base_name)
        if target is None:
            return
        self._last_component_selection = None
        self._selected_owner = target.owner_path
        self._save_model_as()

    def _export_setbas_resource_family(self, item) -> None:
        """Export the proven BASE owner of one BAS resource."""

        if self._setbas is None or item is None:
            return
        kind = item.data(0, _BAS_KIND_ROLE)
        if kind == "base":
            base_name = item.data(0, _BAS_NAME_ROLE) or item.text(0)
            self._export_setbas_base_family(str(base_name).strip())
            return
        component_kind = {
            "sklt.class": "skeleton",
            "ilbm.class": "texture",
            "bmpanim.class": "animation",
        }.get(kind)
        index = item.data(0, Qt.ItemDataRole.UserRole)
        if component_kind is None or index is None:
            return
        resource = self._setbas.resources[index]
        archive_path = Path(self._setbas.path)
        try:
            if self._family is None or self._family.base_path != archive_path:
                family = load_asset_family(
                    archive_path, self._extra_roots, {}, self._setbas)
                self._set_family(family)
        except Exception as exc:
            QMessageBox.warning(
                self, "Asset Family unavailable",
                f"{resource.resource_name} could not be mapped to its BASE "
                f"family.\n\n{exc}")
            return
        self._last_component_selection = (
            component_kind, str(resource.resource_name))
        self._save_model_as()

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
            from studio.formats.setbas import decode_texture

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
            self, resource=None, *, confirm_discard: bool = True) -> bool:
        if self._setbas is None:
            return False
        if resource is None:
            item = self.setbas_tree.currentItem()
            index = item.data(0, Qt.ItemDataRole.UserRole) if item else None
            if index is None:
                QMessageBox.information(
                    self, "No resource selected",
                    "Select a sklt.class resource in the SET.BAS tree first.",
                )
                return False
            resource = self._setbas.resources[index]
        if resource.class_id.lower() != "sklt.class":
            QMessageBox.information(
                self, "Not a skeleton",
                f"{resource.resource_name} is {resource.class_id}.\n\n"
                "Preview supports only SKLT skeletons and ILBM/VBMP "
                "textures.",
            )
            return False
        if confirm_discard and not self._confirm_discard_geometry():
            return False
        if self._preview_setbas_textured(resource):
            self._raise_setbas_tab()
            return True
        try:
            family = load_manual_family(None, [], [], setbas=self._setbas)
            from studio.assets.family import FamilyObject
            from studio.formats.base_format import BaseObject
            from studio.formats.setbas import decode_skeleton

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
            return False
        self._set_family(family)
        self._raise_setbas_tab()
        self.statusBar().showMessage(
            f"{resource.resource_name}: geometry-only (this archive has no "
            "base.class mapping for it, textures live only in loose .base "
            "files)", 10000)
        return True

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
        self.statusBar().showMessage(
            f"Textured preview from the archive's own base.class mapping: "
            f"{resource.resource_name} [{owner}]", 8000)
        return True



if __name__ == "__main__":
    import sys

    from PySide6.QtWidgets import QApplication
    from studio.rendering.gpu import request_desktop_opengl
    from main import _open_startup_path

    request_desktop_opengl()
    app = QApplication(sys.argv)
    window = AssemblyWindow()
    window.show()
    if len(sys.argv) > 1:
        _open_startup_path(window, sys.argv[1])
    raise SystemExit(app.exec())
