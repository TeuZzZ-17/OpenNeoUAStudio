"""OpenUAStudio: integrated Urban Assault asset workbench.

The main window assembles BASE + skeleton + texture + animation families,
provides the former BASet extraction/conversion workflows, and launches the
integrated Wireframe Editor and Map Editor. Geometry writes are explicit,
verified, and backed up before an original loose skeleton is overwritten.
"""

from __future__ import annotations

import copy
import hashlib
import math
import shutil
import subprocess
import sys
import re
import os
import tempfile
from pathlib import Path

from PySide6.QtCore import QPointF, QSize, QTimer, QUrl, Qt
from PySide6.QtGui import (
    QAction,
    QColor,
    QCursor,
    QDesktopServices,
    QImage,
    QImageWriter,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
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
    FamilyObject,
    load_asset_family,
    load_manual_family,
    rebuild_materials,
)
from anm_parser import AnmParseError, export_anm_bytes, parse_anm_bytes
from asset_tree_filter import filter_tree as filter_asset_tree
from assembly_viewer import AssetViewport, VIEW_MODES, VIEW_PRESETS
from base_mapping_editor import (
    MappingEditError,
    MappingIndex,
    RepairPlan,
    StructuralBlockState,
    TextureNameEdit,
    UVEdit,
    eligible_blocks,
    export_base_object_bytes,
    plan_copy_style,
    plan_planar,
    rewrite_block_texture_template,
    save_model_base_copy,
    save_repaired_base,
)
from base_parser import BaseParseError, parse_base_bytes
from dependency_profile import DependencyProfile
from editor_widgets import (
    ResponsiveButtonGrid as _ResponsiveButtonGrid,
    STATUS_COLORS as _STATUS_COLORS,
    TexturePickerDialog,
    ViewportWidthScrollArea as _ViewportWidthScrollArea,
    checker_thumbnail as _checker_thumbnail,
    draw_uv_polygon as _draw_uv_polygon,
    qimage_from_ilbm as _qimage_from_ilbm,
    status_icon as _status_icon,
)
from fx_element_editor import (
    FxElement,
    FxElementCapabilities,
    FxElementClipboard,
    append_fx_element_clipboard,
    build_fx_element_clipboard,
    build_fx_elements_clipboard,
    detach_shared_fx_vertices,
    detect_fx_elements,
    validate_fx_element_clipboard,
)
from polygon_reference_graph import diagnose_polygon_references
from setbas_reader import (
    SetBasArchive,
    SetBasError,
    decode_texture,
    read_setbas,
)
from sklt_parser import (
    SkltParseError,
    parse_sklt_bytes,
    parse_sklt_file,
    save_sklt_with_poo2_points,
    save_sklt_with_poo2_pol2_structure,
)
from geometry_editor import (
    GeometryClipboardError,
    apply_delete_geometry,
    append_geometry_clipboard,
    build_geometry_clipboard,
    match_mirror_points,
    match_mirror_polygons,
    plan_delete_geometry,
)
from model_space_gizmo import ModelSpaceGizmo
from texture_catalog import build_texture_catalog
from texture_assignment import classify_texture_assignment
from texture_convert import TextureConvertError, write_image_as_ilbm
from transform_dialogs import LiveRotateDialog, LiveScaleDialog
from uv_editor_widget import UVEditorWidget, UVLoop
from uv_topology_editor import (
    UVTopologyError,
    apply_uv_vertex_deletion,
    apply_uv_vertex_insertion,
    plan_uv_vertex_deletion,
    plan_uv_vertex_insertion,
)
from vp_manager import (
    VPConflictError,
    VPEmbeddedError,
    VPManager,
    VPParseError,
    export_visproto_bytes,
    load_visproto,
    normalize_base_name,
    normalize_skeleton_name,
    reconstruct_embedded_vps,
)

WINDOW_TITLE = "OpenUAStudio"
_BAS_KIND_ROLE = int(Qt.ItemDataRole.UserRole) + 1
_BAS_NAME_ROLE = int(Qt.ItemDataRole.UserRole) + 2


class _TransformStepSpinBox(QDoubleSpinBox):
    """Keep full spin-box precision while presenting compact step text."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.display_mode = "move"

    def textFromValue(self, value: float) -> str:  # noqa: N802 - Qt override
        if self.display_mode == "move":
            return f"{value:.2f}"
        return f"{value:g}"


class _BasResourceTree(QTreeWidget):
    """Keep right-click purely contextual for every BAS resource class."""

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.RightButton:
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.RightButton:
            event.accept()
            return
        super().mouseReleaseEvent(event)


def _display_path(path: str | Path) -> Path:
    """Return a stable absolute path for window-title display."""

    return Path(path).expanduser().resolve(strict=False)


def _next_backup_path(source: Path) -> Path:
    """Return a non-destructive .bak path beside a loose source file."""

    candidate = source.with_name(source.name + ".bak")
    if not candidate.exists():
        return candidate
    index = 1
    while True:
        candidate = source.with_name(source.name + f".{index}.bak")
        if not candidate.exists():
            return candidate
        index += 1


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


class AddFxElementDialog(QDialog):
    """Choose one compatible FX element to clone without reconstruction."""

    def __init__(self, templates: list[FxElement], parent=None,
                 preferred: FxElement | None = None,
                 preview_provider=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add FX Element")
        self._preview_provider = preview_provider
        self._preview_frames: list[QImage] = []
        self._preview_frame = 0
        self._preview_timer = QTimer(self)
        self._preview_timer.setInterval(180)
        self._preview_timer.timeout.connect(self._advance_preview)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Existing FX element"))
        self.material_combo = QComboBox()
        for element in templates:
            kind = "VANM" if element.source_kind == "VANM" else "direct"
            material = ", ".join(element.material_names)
            self.material_combo.addItem(
                f"{element.fx_name} | {kind} | {material}", element)
            if preferred is not None \
                    and element.identity == preferred.identity:
                self.material_combo.setCurrentIndex(
                    self.material_combo.count() - 1)
        layout.addWidget(self.material_combo)
        self.preview_label = QLabel("Preview unavailable")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumSize(180, 120)
        self.preview_label.setStyleSheet(
            "background: #22252b; border: 1px solid #555b66;")
        layout.addWidget(self.preview_label)
        self.preview_info = QLabel()
        self.preview_info.setWordWrap(True)
        layout.addWidget(self.preview_info)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.material_combo.currentIndexChanged.connect(
            self._refresh_preview)
        self._refresh_preview()

    def selected_template(self) -> FxElement | None:
        value = self.material_combo.currentData()
        return value if isinstance(value, FxElement) else None

    def _show_preview_frame(self) -> None:
        if not self._preview_frames:
            self.preview_label.setPixmap(QPixmap())
            self.preview_label.setText("Missing or unreadable asset")
            return
        image = self._preview_frames[
            self._preview_frame % len(self._preview_frames)]
        if image.isNull():
            self.preview_label.setPixmap(QPixmap())
            self.preview_label.setText("Missing or unreadable asset")
            return
        pixmap = QPixmap.fromImage(image).scaled(
            self.preview_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation)
        self.preview_label.setText("")
        self.preview_label.setPixmap(pixmap)

    def _refresh_preview(self, _index: int = 0) -> None:
        self._preview_timer.stop()
        self._preview_frames = []
        self._preview_frame = 0
        element = self.selected_template()
        info = []
        if element is not None and self._preview_provider is not None:
            try:
                frames, info = self._preview_provider(element)
                self._preview_frames = [
                    frame for frame in frames
                    if isinstance(frame, QImage) and not frame.isNull()]
            except Exception as exc:
                info = [f"Preview diagnostic: {exc}"]
        self.preview_info.setText("\n".join(info))
        self._show_preview_frame()
        if len(self._preview_frames) > 1:
            self._preview_timer.start()

    def _advance_preview(self) -> None:
        if len(self._preview_frames) < 2:
            self._preview_timer.stop()
            return
        self._preview_frame = (self._preview_frame + 1) % len(
            self._preview_frames)
        self._show_preview_frame()

    def done(self, result: int) -> None:
        self._preview_timer.stop()
        super().done(result)


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
        self._collision_windows: list[QMainWindow] = []
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
        # optional read-only SET.BAS resource provider
        self._setbas: SetBasArchive | None = None
        self._vp_manager: VPManager | None = None
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
        self._texture_preview_cache: dict[tuple[int, str], str | None] = {}
        self._texture_qimage_cache: dict[tuple, QImage] = {}

        self.viewport = AssetViewport()
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
        asset_header.setMinimumSectionSize(40)
        asset_header.setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        for column in range(2):
            asset_header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.Interactive)
        self.asset_tree.setColumnWidth(0, 300)
        self.asset_tree.setColumnWidth(1, 105)
        self.asset_tree.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
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
        self.texture_list.itemClicked.connect(
            self._load_visual_texture_item)
        self.texture_list.itemDoubleClicked.connect(
            self._preview_visual_texture_item)
        self.resolve_tree = QTreeWidget()
        self.resolve_tree.setHeaderLabels(["Resource", "Status", "Path"])
        resolve_header = self.resolve_tree.header()
        resolve_header.setSectionsMovable(False)
        resolve_header.setStretchLastSection(False)
        resolve_header.setMinimumSectionSize(40)
        resolve_header.setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        for column in range(3):
            resolve_header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.Interactive)
        # Default Dependencies layout mirrors the compact, readable Windows
        # arrangement: the resource remains dominant, status stays narrow,
        # and the path receives the remaining useful width.  Every section
        # remains Interactive, so users can still tune it manually.
        self.resolve_tree.setColumnWidth(0, 260)
        self.resolve_tree.setColumnWidth(1, 70)
        self.resolve_tree.setColumnWidth(2, 165)
        self.resolve_tree.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.resolve_tree.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.resolve_tree.customContextMenuRequested.connect(
            self._show_resolve_context_menu)
        self.setbas_tree = _BasResourceTree()
        self.setbas_tree.setHeaderLabels(["Resource", "Payload", "VP"])
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
        self.setbas_tree.itemSelectionChanged.connect(
            self._sync_vp_controls)
        self.setbas_tree.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.setbas_tree.customContextMenuRequested.connect(
            self._show_setbas_context_menu)
        setbas_header = self.setbas_tree.header()
        setbas_header.setSectionsMovable(False)
        setbas_header.setStretchLastSection(False)
        setbas_header.setMinimumSectionSize(40)
        setbas_header.setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        for column in range(3):
            setbas_header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.Interactive)
        self.setbas_tree.setColumnWidth(0, 285)
        self.setbas_tree.setColumnWidth(1, 105)
        self.setbas_tree.setColumnWidth(2, 55)
        self.setbas_tree.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
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
        self.repair_save_button = QPushButton("Export Repaired BASE")
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
            "Model exports are verified; overwriting a loose skeleton requires "
            "confirmation and creates a .bak backup."
        )

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
        extract = menu.addAction(
            f"Extract selected... ({len(resources)})" if len(resources) > 1
            else "Extract selected...")
        extract.setEnabled(bool(resources))
        extract.triggered.connect(self._extract_setbas_selected)
        menu.addAction("Extract archive",
                       self._extract_setbas_archive).setEnabled(
                           self._setbas is not None)
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
            copy_name = menu.addAction("Copy BASE name")
            copy_name.triggered.connect(lambda: self._copy_text(
                base_name, "BASE name copied successfully."))
        menu.addSeparator()
        menu.addAction("Select all resources", self.setbas_tree.selectAll)
        if self._last_output_directory is not None:
            menu.addAction("Open last output folder",
                           self._open_last_output_folder)
        try:
            menu.exec(self.setbas_tree.viewport().mapToGlobal(position))
        finally:
            self._setbas_context_item = None

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

    def _select_owner_from_asset_menu(self, owner: str) -> None:
        self._select_owner(owner)

    def _play_asset_animation(self, item) -> None:
        if self._family is None or item is None:
            return
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data or data[0] != "animation":
            return
        name = data[1]
        owner = self._tree_owner_for_item(item)
        if owner:
            self._select_owner(owner, preserve_asset_selection=True)
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
            menu.addAction("Select model",
                           lambda: self._select_owner_from_asset_menu(owner))
        if data:
            kind, payload = data
            if kind == "texture":
                preview = menu.addAction("Preview texture")
                preview.setEnabled(self._family is not None
                                   and self._visual_texture_available(payload))
                preview.triggered.connect(
                    lambda: self._open_visual_texture(
                        payload, show_preview=True, switch_tabs=False))
                export = menu.addAction("Export texture as PNG...")
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
        path = self._asset_item_path(item)
        if path is not None:
            menu.addAction("Reveal source file",
                           lambda: self._reveal_asset_item(item))
        menu.addSeparator()
        menu.addAction("Copy info", lambda: self._copy_text(
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
        # --- File menu: explicit import/export workflow ---
        file_menu = self.menuBar().addMenu("&File")
        self.file_menu = file_menu
        self.open_base_action = QAction("Import BAS Archive", self)
        self.open_base_action.triggered.connect(self.open_bas_archive_dialog)
        self.open_sklt_action = QAction("Import SKLT", self)
        self.open_sklt_action.triggered.connect(self.open_sklt_dialog)
        self.open_ilbm_action = QAction("Import ILBM", self)
        self.open_ilbm_action.triggered.connect(self.open_ilbm_dialog)
        self.open_family_action = QAction("Import Asset Family", self)
        self.open_family_action.triggered.connect(self.open_family_dialog)

        self.file_import_menu = file_menu.addMenu("Import")
        for action in (
                self.open_base_action, self.open_sklt_action,
                self.open_ilbm_action, self.open_family_action):
            self.file_import_menu.addAction(action)

        self.save_asset_family_action = QAction("Export Asset Family", self)
        self.save_asset_family_action.setEnabled(False)
        self.save_asset_family_action.triggered.connect(self._save_model_as)
        self.save_base_action = QAction("Export BASE", self)
        self.save_base_action.setEnabled(False)
        self.save_base_action.triggered.connect(self._save_base_as)
        self.save_sklt_action = QAction("Export SKLT", self)
        self.save_sklt_action.setEnabled(False)
        self.save_sklt_action.triggered.connect(self._save_sklt_as)
        self.save_ilbm_action = QAction("Export ILBM", self)
        self.save_ilbm_action.setEnabled(False)
        self.save_ilbm_action.triggered.connect(self._save_ilbm_as)
        self.overwrite_action = QAction("Overwrite", self)
        self.overwrite_action.setEnabled(False)
        self.overwrite_action.triggered.connect(self._overwrite_model)

        self.file_export_menu = file_menu.addMenu("Export")
        for action in (
                self.save_asset_family_action, self.save_base_action,
                self.save_sklt_action, self.save_ilbm_action,
                self.overwrite_action):
            self.file_export_menu.addAction(action)

        file_menu.addSeparator()
        self.exit_action = QAction("Exit", self)
        self.exit_action.triggered.connect(self.close)
        file_menu.addAction(self.exit_action)

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
            "Copy and atomically remove the selected polygons.")
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
        self.add_fx_action = QAction("Add FX Element...", self)
        self.add_fx_action.setEnabled(False)
        self.add_fx_action.setStatusTip(
            "Clone one compatible existing FX element exactly.")
        self.add_fx_action.triggered.connect(self._add_fx_element)
        edit_menu.addAction(self.add_fx_action)
        self.edit_scale_action = QAction("Scale...", self)
        self.edit_scale_action.triggered.connect(self._scale_selected_geometry)
        edit_menu.addAction(self.edit_scale_action)
        self.edit_rotate_action = QAction("Rotate...", self)
        self.edit_rotate_action.triggered.connect(
            self._rotate_selected_geometry)
        edit_menu.addAction(self.edit_rotate_action)

        view_menu = self.menuBar().addMenu("&View")
        self.view_menu = view_menu
        self.sen_check = self._checkable("SEN2 volume",
                                         self.viewport.set_show_sen, False)
        self.owner_bounds_check = self._checkable(
            "Selected Model Bounds",
            self.viewport.set_show_owner_bbox, False)
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
        for action in (self.sen_check, self.owner_bounds_check,
                       self.wire_check, self.cull_check,
                       self.axes_check, self.grid_check,
                       self.overlay_check, self.mapping_diag_check):
            view_menu.addAction(action)
        view_menu.addSeparator()
        self.reset_camera_action = QAction("Reset camera", self)
        self.reset_camera_action.setEnabled(False)
        self.reset_camera_action.triggered.connect(
            self._reset_view_and_gizmo)
        view_menu.addAction(self.reset_camera_action)

        tools_menu = self.menuBar().addMenu("&Tools")
        self.tools_menu = tools_menu
        setbas_tools_menu = tools_menu.addMenu("BAS Archive")
        extract_setbas_action = QAction("Extract archive", self)
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

        self.wireframe_editor_action = QAction("Wireframe Editor", self)
        self.wireframe_editor_action.triggered.connect(
            self._open_wireframe_editor)
        self.integrated_editors_separator = tools_menu.addSeparator()
        tools_menu.addAction(self.wireframe_editor_action)
        self.collision_editor_action = QAction("Collision Editor", self)
        self.collision_editor_action.triggered.connect(
            self._open_collision_editor)
        tools_menu.addAction(self.collision_editor_action)
        self.map_editor_action = QAction("Map Editor", self)
        self.map_editor_action.triggered.connect(self._open_map_editor)
        tools_menu.addAction(self.map_editor_action)
        self.mapping_repair_action = QAction("Mapping Repair...", self)
        self.mapping_repair_action.triggered.connect(
            self._show_mapping_repair)
        tools_menu.addAction(self.mapping_repair_action)

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


        self.mode_combo = QComboBox()
        for mode in (candidate for candidate in VIEW_MODES
                     if candidate != "solid"):
            label = {"wireframe": "Wireframe",
                     "materials": "Material groups",
                     "textured": "Textured"}[mode]
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

        # Animation controls (enabled only when a VANM is loaded)
        anim_bar = QToolBar("Animation", self)
        anim_bar.setMovable(False)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, anim_bar)
        self.animation_toolbar = anim_bar

        anim_bar.addWidget(QLabel(" VANM preview: "))
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
        self.global_undo_button = QPushButton("< Undo")
        self.global_undo_button.setEnabled(False)
        self.global_undo_button.setMinimumWidth(88)
        self.global_undo_button.setToolTip(
            "Undo the latest geometry, texture or UV edit.")
        self.global_undo_button.clicked.connect(self._undo_edit)
        self.global_redo_button = QPushButton("Redo >")
        self.global_redo_button.setEnabled(False)
        self.global_redo_button.setMinimumWidth(88)
        self.global_redo_button.setToolTip(
            "Redo the latest geometry, texture or UV edit.")
        self.global_redo_button.clicked.connect(self._redo_edit)
        anim_bar.addWidget(self.global_undo_button)
        anim_bar.addWidget(self.global_redo_button)

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
        vp_source_row = QHBoxLayout()
        self.vp_source_label = QLabel("VP source: unavailable")
        self.vp_source_label.setWordWrap(True)
        vp_source_row.addWidget(self.vp_source_label, 1)
        self.vp_import_button = QPushButton("Import VISPROTO.LST...")
        self.vp_import_button.clicked.connect(self._import_visproto)
        self.vp_import_button.setEnabled(False)
        vp_source_row.addWidget(self.vp_import_button)
        setbas_layout.addLayout(vp_source_row)

        vp_edit_box = QGroupBox("VP assignment")
        vp_edit_layout = QGridLayout(vp_edit_box)
        vp_edit_layout.setContentsMargins(5, 4, 5, 5)
        vp_edit_layout.addWidget(QLabel("Selected BASE:"), 0, 0)
        self.vp_selected_base_label = QLabel("-")
        self.vp_selected_base_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        vp_edit_layout.addWidget(self.vp_selected_base_label, 0, 1, 1, 3)
        vp_edit_layout.addWidget(QLabel("Current VP:"), 1, 0)
        self.vp_current_label = QLabel("-")
        vp_edit_layout.addWidget(self.vp_current_label, 1, 1)
        vp_edit_layout.addWidget(QLabel("New VP:"), 1, 2)
        self.vp_new_spin = QSpinBox()
        self.vp_new_spin.setRange(0, 0)
        self.vp_new_spin.setEnabled(False)
        vp_edit_layout.addWidget(self.vp_new_spin, 1, 3)
        self.vp_assign_button = QPushButton("Assign / Swap VP...")
        self.vp_assign_button.clicked.connect(self._assign_or_swap_vp)
        self.vp_assign_button.setEnabled(False)
        vp_edit_layout.addWidget(self.vp_assign_button, 2, 0, 1, 2)
        self.vp_undo_button = QPushButton("Undo VP")
        self.vp_undo_button.clicked.connect(self._undo_vp)
        self.vp_undo_button.setEnabled(False)
        vp_edit_layout.addWidget(self.vp_undo_button, 2, 2)
        self.vp_redo_button = QPushButton("Redo VP")
        self.vp_redo_button.clicked.connect(self._redo_vp)
        self.vp_redo_button.setEnabled(False)
        vp_edit_layout.addWidget(self.vp_redo_button, 2, 3)
        self.vp_export_button = QPushButton("Export VISPROTO.LST...")
        self.vp_export_button.clicked.connect(self._export_visproto)
        self.vp_export_button.setEnabled(False)
        vp_edit_layout.addWidget(self.vp_export_button, 3, 0, 1, 4)
        setbas_layout.addWidget(vp_edit_box)

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
        self.setbas_extract_all_button = QPushButton("Extract archive")
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
        model_box_layout.setSpacing(5)

        poly_id_row = QHBoxLayout()
        poly_id_row.setSpacing(4)
        poly_id_row.addWidget(QLabel("Poly ID:"))
        self.poly_id_spin = QSpinBox()
        self.poly_id_spin.setRange(0, 0)
        self.poly_id_spin.setEnabled(False)
        self.poly_id_spin.setKeyboardTracking(False)
        self.poly_id_spin.setToolTip(
            "Select and inspect a polygon by its persistent zero-based ID.")
        self.poly_id_spin.valueChanged.connect(self._on_poly_id_changed)
        self.poly_id_spin.editingFinished.connect(
            lambda: self._on_poly_id_changed(self.poly_id_spin.value()))
        poly_id_row.addWidget(self.poly_id_spin, 1)
        self.poly_id_previous_button = QPushButton("<")
        self.poly_id_previous_button.setEnabled(False)
        self.poly_id_previous_button.setToolTip("Previous polygon")
        self.poly_id_previous_button.clicked.connect(
            lambda: self._step_poly_id(-1))
        poly_id_row.addWidget(self.poly_id_previous_button)
        self.poly_id_next_button = QPushButton(">")
        self.poly_id_next_button.setEnabled(False)
        self.poly_id_next_button.setToolTip("Next polygon")
        self.poly_id_next_button.clicked.connect(
            lambda: self._step_poly_id(1))
        poly_id_row.addWidget(self.poly_id_next_button)
        self.poly_select_all_button = QPushButton("Select All")
        self.poly_select_all_button.setEnabled(False)
        self.poly_select_all_button.clicked.connect(
            self.viewport.select_all_edit_vertices)
        poly_id_row.addWidget(self.poly_select_all_button)
        self.poly_deselect_all_button = QPushButton("Deselect All")
        self.poly_deselect_all_button.setEnabled(False)
        self.poly_deselect_all_button.clicked.connect(
            self.viewport.select_no_edit_vertices)
        poly_id_row.addWidget(self.poly_deselect_all_button)
        model_box_layout.addLayout(poly_id_row)

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
        self.transform_help_label = QLabel(
            "UA axes: -Y = Up, +Y = Down. "
            "Diagonal handles apply the full step on every active axis.")
        self.transform_help_label.setWordWrap(True)
        self.transform_help_label.setStyleSheet(
            "color: #b9c1ca; font-size: 9px;")
        gizmo_layout.addWidget(self.transform_help_label)
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
        self.viewport.set_direct_transform("move", 0.50)
        self._apply_transform_mode_colors()
        model_box_layout.addWidget(gizmo_box)

        self.model_texture_label = QLabel("Current texture: -")
        self.model_texture_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.model_texture_label.setWordWrap(True)
        model_box_layout.addWidget(self.model_texture_label)
        self.load_texture_button = QPushButton("Load Texture...")
        self.load_texture_button.setEnabled(False)
        self.load_texture_button.setToolTip(
            "Replace the texture only on the selected polygon segments. "
            "Select multiple segments to update them together.")
        self.load_texture_button.clicked.connect(self._load_model_texture)
        model_box_layout.addWidget(self.load_texture_button)

        fx_row = QHBoxLayout()
        fx_row.addWidget(QLabel("FX element:"))
        self.fx_combo.setMinimumWidth(0)
        self.fx_combo.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Fixed,
        )
        fx_row.addWidget(self.fx_combo, 1)
        model_box_layout.addLayout(fx_row)
        self.fx_combo.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.fx_combo.customContextMenuRequested.connect(
            self._show_fx_combo_context_menu)
        fx_buttons = QHBoxLayout()
        self.add_fx_button = QPushButton("Add FX Element...")
        self.add_fx_button.setEnabled(False)
        self.add_fx_button.setToolTip(
            "Clone one compatible existing FX element exactly, then choose "
            "only its position in the viewport.")
        self.add_fx_button.clicked.connect(self._add_fx_element)
        fx_buttons.addWidget(self.add_fx_button)
        self.delete_fx_button = QPushButton("Delete Selected FX")
        self.delete_fx_button.setEnabled(False)
        self.delete_fx_button.setToolTip(
            "Delete the complete editable FX element selected in the list. "
            "The operation can be undone.")
        self.delete_fx_button.clicked.connect(
            self._delete_selected_fx_element)
        fx_buttons.addWidget(self.delete_fx_button)
        model_box_layout.addLayout(fx_buttons)

        uv_box = QGroupBox("Texture UV preview")
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
        self.uv_auto_align_check = QCheckBox("Auto Align UV")
        self.uv_auto_align_check.setChecked(True)
        self.uv_auto_align_check.setToolTip(
            "Snap UV handles using screen-distance guides. Hold Alt while "
            "dragging to bypass snapping temporarily.")
        self.uv_auto_align_check.toggled.connect(
            self.uv_editor.set_auto_align_enabled)
        uv_layout.addWidget(self.uv_auto_align_check)
        self.uv_select_all_button = QPushButton("Select All UVs")
        self.uv_select_all_button.clicked.connect(self.uv_editor.select_all)
        self.uv_clear_selection_button = QPushButton("Clear Selection")
        self.uv_clear_selection_button.clicked.connect(
            self.uv_editor.select_none)
        self.uv_add_vertex_button = QPushButton("Add Vertex")
        self.uv_add_vertex_button.setEnabled(False)
        self.uv_add_vertex_button.clicked.connect(self._add_uv_vertex)
        self.uv_delete_vertex_button = QPushButton("Delete Vertex")
        self.uv_delete_vertex_button.setEnabled(False)
        self.uv_delete_vertex_button.clicked.connect(self._delete_uv_vertex)
        uv_tools = _ResponsiveButtonGrid([
            self.uv_select_all_button,
            self.uv_clear_selection_button,
            self.uv_add_vertex_button,
            self.uv_delete_vertex_button,
        ])
        uv_layout.addWidget(uv_tools)
        uv_buttons = QHBoxLayout()
        self.uv_revert_button = QPushButton("Revert Selected UV")
        self.uv_revert_button.setEnabled(False)
        self.uv_revert_button.clicked.connect(self._revert_selected_uv)
        uv_buttons.addWidget(self.uv_revert_button)
        self.uv_reset_all_button = QPushButton("Reset All UVs")
        self.uv_reset_all_button.setEnabled(False)
        self.uv_reset_all_button.clicked.connect(self._reset_all_uvs)
        uv_buttons.addWidget(self.uv_reset_all_button)
        uv_layout.addLayout(uv_buttons)
        model_box_layout.addWidget(uv_box, 1)

        save_buttons = QGridLayout()
        save_buttons.setHorizontalSpacing(5)
        self.model_save_button = QPushButton("Overwrite")
        self.model_save_button.setEnabled(False)
        self.model_save_button.clicked.connect(self._overwrite_model)
        save_buttons.addWidget(self.model_save_button, 0, 0)
        self.model_save_as_button = QPushButton("Export Asset Family")
        self.model_save_as_button.setEnabled(False)
        self.model_save_as_button.setToolTip(
            "Export the selected BASE, SKLT, textures and animations together.")
        self.model_save_as_button.clicked.connect(self._save_model_as)
        save_buttons.addWidget(self.model_save_as_button, 0, 1)
        save_buttons.setColumnStretch(0, 1)
        save_buttons.setColumnStretch(1, 1)
        model_box_layout.addLayout(save_buttons)

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
        resources_tabs.addTab(setbas_panel, "Bas Manager")
        resources_tabs.addTab(asset_panel, "Assets")
        resources_tabs.addTab(resolve_panel, "Dependencies")

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
        editor_tabs.addTab(model_scroll, "Model and Texture Editor")

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
        self._update_snapshot_color_button()
        self._sync_animation_controls()

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(2)
        editor_status_row = QWidget()
        editor_status_layout = QHBoxLayout(editor_status_row)
        editor_status_layout.setContentsMargins(0, 0, 0, 0)
        editor_status_layout.setSpacing(0)
        editor_status_layout.addStretch(1)
        self.editor_status_panel = QWidget()
        self.editor_status_panel.setObjectName("editorStatusPanel")
        self.editor_status_panel.setMinimumHeight(44)
        self.editor_status_panel.setStyleSheet("""
            QWidget#editorStatusPanel {
                background: #202126;
                border: 2px solid #b3485c;
            }
            QLabel {
                border: none;
                background: transparent;
            }
        """)
        editor_status_center = QHBoxLayout(self.editor_status_panel)
        editor_status_center.setContentsMargins(14, 3, 14, 3)
        editor_status_center.setSpacing(18)
        editor_status_center.addStretch(1)
        self.loaded_resource_label = QLabel("No Resource Loaded")
        self.loaded_resource_label.setAlignment(
            Qt.AlignmentFlag.AlignCenter)
        resource_font = self.loaded_resource_label.font()
        resource_font.setPointSize(13)
        resource_font.setBold(True)
        self.loaded_resource_label.setFont(resource_font)
        self.loaded_resource_label.setStyleSheet("color: #ffffff;")
        editor_status_center.addWidget(self.loaded_resource_label)
        self.unsaved_edits_label = QLabel()
        self.unsaved_edits_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        unsaved_font = self.unsaved_edits_label.font()
        unsaved_font.setPointSize(13)
        unsaved_font.setBold(True)
        self.unsaved_edits_label.setFont(unsaved_font)
        self.unsaved_edits_label.setStyleSheet("color: #ffffff;")
        self.unsaved_edits_label.hide()
        editor_status_center.addWidget(self.unsaved_edits_label)
        editor_status_center.addStretch(1)
        editor_status_layout.addWidget(self.editor_status_panel, 8)
        editor_status_layout.addStretch(1)
        right_layout.addWidget(editor_status_row)
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

        self._diagnostics_tabs = diagnostics_tabs
        self._diagnostics_dock = diagnostics_panel
        self._diagnostics_splitter = vertical_split
        # Diagnostics are opt-in at startup.  The first explicit Warnings,
        # Validation or Log command reopens the same compact 80 px panel.
        diagnostics_panel.hide()

    # -- actions ---------------------------------------------------------------

    def _show_diagnostics(self, index: int) -> None:
        """Show diagnostics strictly inside the existing main-window area."""

        if self._diagnostics_tabs is not None:
            index = max(0, min(index,
                               self._diagnostics_tabs.count() - 1))
            self._diagnostics_tabs.setCurrentIndex(index)
        if self._diagnostics_dock is None:
            return
        was_visible = self._diagnostics_dock.isVisible()
        self._diagnostics_dock.show()
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

    def _open_collision_editor(self) -> None:
        try:
            from collision_editor import CollisionEditorWindow
        except Exception as exc:
            QMessageBox.critical(
                self, "Collision Editor unavailable",
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
        """Launch the integrated Map Editor in a separate process.

        Map Editor uses Tk while the main workbench uses Qt. Keeping their
        event loops in separate processes preserves the original editor
        behavior and avoids toolkit conflicts.
        """

        try:
            from main import launch_map_editor_process

            launch_map_editor_process()
        except OSError as exc:
            QMessageBox.critical(
                self,
                "Map Editor unavailable",
                "The integrated Map Editor could not be launched.\n\n"
                f"{exc}",
            )

    def open_bas_archive_dialog(self) -> None:
        """Import SET.BAS through the archive browser, or a standalone BASE."""

        path, _ = QFileDialog.getOpenFileName(
            self, "Import BAS Archive", str(self._last_directory),
            "Urban Assault BAS/BASE (*.bas *.base *.BAS *.BASE);;All files (*)",
        )
        if not path:
            return
        source = Path(path)
        if source.name.casefold() == "set.bas":
            self.open_setbas(source)
        else:
            self.open_base(source)

    def open_base_dialog(self) -> None:
        """Backward-compatible standalone BASE importer."""

        path, _ = QFileDialog.getOpenFileName(
            self, "Import BASE asset", str(self._last_directory),
            "Urban Assault BASE (*.base *.bas *.BASE *.BAS);;All files (*)",
        )
        if path:
            source = Path(path)
            if source.name.casefold() == "set.bas":
                self.open_setbas(source)
            else:
                self.open_base(source)

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

    def open_sklt_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import SKLT", str(self._last_directory),
            "Skeletons (*.sklt *.skl *.SKLT *.SKL);;All files (*)",
        )
        if path:
            self._open_manual_asset_family(sklt=path)

    def open_ilbm_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import ILBM", str(self._last_directory),
            "ILBM textures (*.ilbm *.ilb *.lbm *.iff *.vbmp "
            "*.ILBM *.ILB *.LBM *.IFF *.VBMP);;All files (*)",
        )
        if not path:
            return
        source = Path(path)
        self._last_directory = source.parent
        try:
            from ilbm_parser import parse_ilbm_file

            image = parse_ilbm_file(source)
            if not image.has_body:
                raise ValueError("the selected texture has no decodable BODY")
            palette = image.palette
            palette_source = "embedded CMAP" if palette is not None else ""
            if palette is None and self._family is not None:
                palette = self._family.external_palette
                if palette is not None:
                    palette_source = "loaded family palette"
            if palette is None and self._setbas is not None:
                palette, palette_source = self._setbas_palette()
            qimage = _qimage_from_ilbm(image, palette)
            if qimage is None or qimage.isNull():
                raise ValueError("the selected texture decoded to an empty image")
        except Exception as exc:
            QMessageBox.warning(
                self, "Texture preview failed",
                f"No file was modified.\n\n{exc}",
            )
            return
        self._show_image_preview(
            f"Texture preview - {source.name}",
            f"{source.name}  |  {image.width} x {image.height}  | "
            f"{image.kind}",
            qimage,
            f"Palette: {palette_source or 'grayscale fallback'}",
        )

    def open_family_dialog(self) -> None:
        sklt, _ = QFileDialog.getOpenFileName(
            self, "Import skeleton (.sklt/.skl) - optional, Cancel to skip",
            str(self._last_directory),
            "Skeletons (*.sklt *.skl *.SKLT *.SKL);;All files (*)",
        )
        base, _ = QFileDialog.getOpenFileName(
            self, "Import BASE file - optional, Cancel to skip",
            str(self._last_directory),
            "Urban Assault BASE (*.base *.bas *.BASE *.BAS);;All files (*)",
        )
        textures, _ = QFileDialog.getOpenFileNames(
            self, "Import texture files (.ilbm/.ilb/.vbmp) - optional",
            str(self._last_directory),
            "ILBM/VBMP textures (*.ilbm *.ilb *.lbm *.iff *.vbmp "
            "*.ILBM *.ILB *.LBM *.IFF *.VBMP);;All files (*)",
        )
        anms, _ = QFileDialog.getOpenFileNames(
            self, "Import animation files (.anm/.vanm) - optional",
            str(self._last_directory),
            "Animations (*.anm *.vanm *.ANM *.VANM);;All files (*)",
        )
        self._open_manual_asset_family(sklt, base, textures, anms)

    def open_setbas_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import SET.BAS as read-only resource provider",
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
        self._texture_preview_cache.clear()
        self._setbas = archive
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
        self._preview_first_setbas_skeleton(confirm_discard=False)

    @staticmethod
    def _set_root_for_archive(path: str | Path) -> Path:
        archive_path = Path(path)
        parent = archive_path.parent
        if parent.name.casefold() in ("objects", "object"):
            return parent.parent
        return parent

    def _vp_paths(self) -> tuple[Path | None, Path | None]:
        if self._setbas is None:
            return None, None
        set_root = self._set_root_for_archive(self._setbas.path)
        return (
            set_root / "Loose" / "VISPROTO.LST",
            set_root / "Scripts" / "VISPROTO.LST",
        )

    def _set_vp_manager(
            self, manager: VPManager | None, source: str,
            source_path: Path | None = None) -> None:
        self._vp_manager = manager
        self._vp_source = source
        self._vp_source_path = source_path
        label = getattr(self, "vp_source_label", None)
        if label is not None:
            count = len(manager.table) if manager is not None else 0
            label.setText(
                f"VP source: {source}"
                + (f" ({count} slots)" if manager is not None else ""))
            issues = self._vp_validation_issues()
            label.setToolTip("\n".join(issues) if issues else
                             "VP IDs are zero-based list positions.")
        self._sync_vp_controls()

    def _load_vp_table(self, archive: SetBasArchive) -> None:
        """Choose one read-only VP source without altering SET.BAS."""

        self._vp_embedded = None
        try:
            self._vp_embedded = reconstruct_embedded_vps(archive.path)
        except (VPEmbeddedError, BaseParseError, OSError, ValueError) as exc:
            self._log(f"VP table: embedded reconstruction unavailable: {exc}")

        set_root = self._set_root_for_archive(archive.path)
        loose_path = set_root / "Loose" / "VISPROTO.LST"
        scripts_path = set_root / "Scripts" / "VISPROTO.LST"
        manager = None
        source = "unavailable"
        source_path = None
        if loose_path.is_file():
            try:
                manager = VPManager(load_visproto(
                    loose_path, require_terminator=True))
                source = "Loose VISPROTO.LST"
                source_path = loose_path
            except (OSError, VPParseError) as exc:
                self._log(
                    f"VP table: invalid loose VISPROTO.LST ignored: {exc}")
        if manager is None and self._vp_embedded is not None:
            manager = VPManager(self._vp_embedded.as_table())
            source = "embedded visproto.base"
            source_path = Path(archive.path)
        if manager is None and scripts_path.is_file():
            try:
                manager = VPManager(load_visproto(
                    scripts_path, require_terminator=False))
                source = "fallback scripts/visproto.lst"
                source_path = scripts_path
            except (OSError, VPParseError) as exc:
                self._log(
                    f"VP table: legacy scripts/visproto.lst unavailable: "
                    f"{exc}")
        self._set_vp_manager(manager, source, source_path)
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
        manager = self._vp_manager
        if manager is None:
            return "?"
        indices = manager.table.vps_for_base(base_name)
        return ", ".join(str(index) for index in indices)

    def _vp_text_for_skeleton(self, skeleton_name: str) -> str:
        manager = self._vp_manager
        if manager is None or self._vp_embedded is None:
            return "?"
        target = normalize_skeleton_name(skeleton_name)
        by_base = self._vp_base_skeletons()
        indices = []
        ambiguous = False
        for entry in manager.table:
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
        manager = self._vp_manager
        if manager is None:
            return ["VP table unavailable."]
        table = manager.table
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

    def _refresh_vp_column(self) -> None:
        for group_index in range(self.setbas_tree.topLevelItemCount()):
            group = self.setbas_tree.topLevelItem(group_index)
            for row_index in range(group.childCount()):
                row = group.child(row_index)
                kind = row.data(0, _BAS_KIND_ROLE)
                name = row.data(0, _BAS_NAME_ROLE) or row.text(0)
                if kind == "base":
                    row.setText(2, self._vp_text_for_base(name))
                elif kind == "sklt.class":
                    row.setText(2, self._vp_text_for_skeleton(name))
                else:
                    row.setText(2, "")
        self._set_vp_manager(
            self._vp_manager, self._vp_source, self._vp_source_path)

    def _current_vp_base_candidates(self) -> list[str]:
        item = self.setbas_tree.currentItem()
        manager = self._vp_manager
        if item is None or manager is None:
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
        for entry in manager.table:
            base_name = entry.base_name
            skeletons = by_base.get(normalize_base_name(base_name), set())
            if target in skeletons and base_name not in candidates:
                candidates.append(base_name)
        return candidates

    def _sync_vp_controls(self) -> None:
        manager = self._vp_manager
        table_available = manager is not None and bool(len(manager.table))
        candidates = self._current_vp_base_candidates() \
            if table_available and hasattr(self, "setbas_tree") else []
        if hasattr(self, "vp_import_button"):
            self.vp_import_button.setEnabled(self._setbas is not None)
            self.vp_export_button.setEnabled(table_available)
            self.vp_new_spin.setEnabled(table_available and bool(candidates))
            if table_available:
                maximum = len(manager.table) - 1
                self.vp_new_spin.setRange(0, maximum)
            else:
                self.vp_new_spin.setRange(0, 0)
            self.vp_assign_button.setEnabled(bool(candidates))
            self.vp_undo_button.setEnabled(
                bool(manager and manager.can_undo))
            self.vp_redo_button.setEnabled(
                bool(manager and manager.can_redo))
            if not candidates:
                self.vp_selected_base_label.setText("-")
                self.vp_current_label.setText("-")
            elif len(candidates) == 1:
                base_name = candidates[0]
                indices = manager.table.vps_for_base(base_name)
                self.vp_selected_base_label.setText(base_name)
                self.vp_current_label.setText(
                    ", ".join(map(str, indices)) or "not assigned")
                if indices:
                    self.vp_new_spin.setValue(indices[0])
            else:
                self.vp_selected_base_label.setText(
                    f"{len(candidates)} BASE parents")
                self.vp_current_label.setText(
                    "choose BASE when assigning")
        if hasattr(self, "setbas_preview_button"):
            item = self.setbas_tree.currentItem()
            resource_index = (
                item.data(0, Qt.ItemDataRole.UserRole)
                if item is not None else None)
            kind = item.data(0, _BAS_KIND_ROLE) if item is not None else None
            previewable = kind == "base"
            if resource_index is not None and self._setbas is not None:
                class_id = self._setbas.resources[resource_index].class_id.lower()
                previewable = class_id in ("sklt.class", "ilbm.class")
            self.setbas_preview_button.setEnabled(previewable)
            self.setbas_extract_button.setEnabled(resource_index is not None)

    def _choose_vp_base(self) -> str | None:
        candidates = self._current_vp_base_candidates()
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        selected, accepted = QInputDialog.getItem(
            self, "Choose BASE parent",
            "This SKLT is referenced by more than one BASE. Edit:",
            candidates, 0, False)
        return str(selected) if accepted else None

    def _choose_vp_slot(self, base_name: str, slots: tuple[int, ...]) -> int | None:
        if len(slots) == 1:
            return slots[0]
        labels = [str(slot) for slot in slots]
        selected, accepted = QInputDialog.getItem(
            self, "Choose current VP",
            f"{base_name} occupies multiple slots. Move:",
            labels, 0, False)
        return int(selected) if accepted else None

    def _assign_or_swap_vp(self) -> None:
        manager = self._vp_manager
        base_name = self._choose_vp_base()
        if manager is None or base_name is None:
            return
        destination = self.vp_new_spin.value()
        slots = manager.table.vps_for_base(base_name)
        changed = False
        if slots:
            source = self._choose_vp_slot(base_name, slots)
            if source is None or source == destination:
                return
            occupied = manager.table.entry(destination).base_name
            answer = QMessageBox.question(
                self, "Swap VP assignments?",
                f"VP {destination} is occupied by {occupied}.\n\n"
                f"Swap VP {source} ({base_name}) with VP {destination}?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
            changed = manager.swap(source, destination)
        else:
            occupied = manager.table.entry(destination).base_name
            answer = QMessageBox.warning(
                self, "Replace VP assignment?",
                f"{base_name} is not currently assigned.\n"
                f"VP {destination} contains {occupied}.\n\n"
                "Replace it explicitly? No other slot is renumbered.",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
            try:
                changed = manager.assign(
                    destination, base_name, allow_replace=True)
            except VPConflictError as exc:
                self._notify(f"VP assignment refused: {exc}", 8000)
                return
        if changed:
            self._refresh_vp_column()
            self._notify(
                "VP assignment updated in memory. Export VISPROTO.LST to "
                "persist it; SET.BAS is unchanged.", 9000)

    def _undo_vp(self) -> None:
        if self._vp_manager is not None and self._vp_manager.undo():
            self._refresh_vp_column()
            self._notify("VP assignment undone in memory.", 5000)

    def _redo_vp(self) -> None:
        if self._vp_manager is not None and self._vp_manager.redo():
            self._refresh_vp_column()
            self._notify("VP assignment redone in memory.", 5000)

    def _import_visproto(self) -> None:
        if self._setbas is None:
            return
        loose_path, scripts_path = self._vp_paths()
        start = loose_path.parent if loose_path is not None \
            else self._last_directory
        path, _ = QFileDialog.getOpenFileName(
            self, "Import VISPROTO.LST (SET.BAS remains unchanged)",
            str(start), "VISPROTO tables (VISPROTO.LST *.lst *.LST);;"
            "All files (*)")
        if not path:
            return
        try:
            manager = VPManager(load_visproto(
                Path(path), require_terminator=False))
        except (OSError, VPParseError) as exc:
            QMessageBox.warning(
                self, "VISPROTO import failed",
                f"No file or archive was modified.\n\n{exc}")
            return
        if not self._confirm_discard_vp_changes(
                "Importing another table will discard the current "
                "unexported VP assignments. Continue?"):
            return
        imported_path = Path(path)
        if loose_path is not None \
                and imported_path.resolve() == loose_path.resolve():
            source = "Loose VISPROTO.LST"
        elif scripts_path is not None \
                and imported_path.resolve() == scripts_path.resolve():
            source = "fallback scripts/visproto.lst"
        else:
            source = "Loose VISPROTO.LST (imported)"
        self._set_vp_manager(manager, source, imported_path)
        self._fill_setbas(self._setbas)
        issues = self._vp_validation_issues()
        if issues:
            QMessageBox.warning(
                self, "VISPROTO imported with validation issues",
                "\n".join(issues)
                + "\n\nSET.BAS was not modified.")
        self._last_directory = imported_path.parent
        self._notify(
            f"Imported {len(manager.table)} zero-based VP slots; "
            "SET.BAS is unchanged.", 8000)

    def _export_visproto(self) -> None:
        manager = self._vp_manager
        if manager is None:
            return
        issues = self._vp_validation_issues()
        if issues:
            answer = QMessageBox.warning(
                self, "VP validation issues",
                "\n".join(issues)
                + "\n\nExport anyway? OpenUA will reject a loose table "
                "if a listed BASE cannot load.",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        loose_path, _scripts_path = self._vp_paths()
        default_path = loose_path or (
            self._last_directory / "VISPROTO.LST")
        dialog = QFileDialog(
            self, "Export per-SET VISPROTO.LST",
            str(default_path),
            "VISPROTO tables (VISPROTO.LST *.lst *.LST);;All files (*)")
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dialog.setFileMode(QFileDialog.FileMode.AnyFile)
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        dialog.selectFile(Path(default_path).name)
        note = QLabel(
            "OpenUA reads this file as a per-SET VP table. The file does not "
            "modify SET.BAS. VP IDs are determined by entry order starting "
            "from 0.")
        note.setWordWrap(True)
        layout = dialog.layout()
        if isinstance(layout, QGridLayout):
            layout.addWidget(
                note, layout.rowCount(), 0, 1,
                max(1, layout.columnCount()))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        selected = dialog.selectedFiles()
        if not selected:
            return
        target = Path(selected[0])
        if not target.suffix:
            target = target.with_suffix(".LST")
        try:
            payload = export_visproto_bytes(
                manager.table, include_comments=True,
                encoding="cp1252", newline="\r\n")
            with tempfile.TemporaryDirectory(
                    prefix="openuastudio_vp_") as tmp_dir:
                staged = Path(tmp_dir) / "VISPROTO.LST"
                staged.write_bytes(payload)
                verified = load_visproto(
                    staged, require_terminator=True)
                if [entry.base_name for entry in verified] != [
                        entry.base_name for entry in manager.table]:
                    raise VPParseError(
                        "round-trip verification changed VP assignments")
                warnings = _commit_verified_files([(staged, target)])
        except (OSError, UnicodeError, VPParseError,
                _BundleCommitError) as exc:
            QMessageBox.critical(
                self, "VISPROTO export failed",
                f"SET.BAS was not modified.\n\n{exc}")
            return
        self._last_directory = target.parent
        self._set_vp_manager(
            VPManager(manager.table), "Loose VISPROTO.LST", target)
        for warning in warnings:
            self._log(f"VISPROTO export: {warning}")
        self._notify(
            f"Exported {len(manager.table)} VP slots to {target}. "
            "SET.BAS was not modified.", 10000)

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
        summary = ", ".join(f"{cls}: {count}" for cls, count in census.items())
        self.setbas_label.setText(
            f"Archive loaded as READ-ONLY resource provider:\n{archive.path}\n"
            f"{len(archive.resources)} EMRS resources ({summary})."
        )
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
        if self._vp_manager is not None:
            base_names.extend(
                entry.base_name for entry in self._vp_manager.table)
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
        return load_asset_family(
            archive_path, self._extra_roots, {}, self._setbas)

    def _animation_owners(self, family, animation_name: str) -> list[str]:
        owners: list[str] = []
        for fam_obj in family.all_objects():
            base_object = getattr(fam_obj, "base_object", None)
            for block in getattr(base_object, "ades", ()):
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
        """Select the model using one embedded VANM and start playback."""

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
        if animation_name is None or not owners:
            QMessageBox.information(
                self, "Animation preview unavailable",
                f"{resource.resource_name} is embedded in SET.BAS, but no "
                "resolved BASE object in this archive uses it.")
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
        self.play_button.setChecked(True)
        self._raise_setbas_tab()
        self.statusBar().showMessage(
            f"Playing {animation_name} on {owner}", 8000)

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
                    changed = self.setbas_tree.currentItem() is not item
                    if changed and not confirm_discard:
                        self._setbas_preview_active = True
                    try:
                        self.setbas_tree.setCurrentItem(item)
                    finally:
                        if changed and not confirm_discard:
                            self._setbas_preview_active = False
                    self.setbas_tree.scrollToItem(item)
                    if not changed or not confirm_discard:
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
            self._preview_setbas_skeleton()
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

    def _preview_setbas_base(self, base_name: str) -> None:
        """Preview one VP BASE object without leaving the Bas Manager tab."""

        if self._setbas is None:
            return
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
            return

        normalized = normalize_base_name(base_name)
        source_offset = None
        if self._vp_embedded is not None:
            entry = next((candidate for candidate in self._vp_embedded.entries
                          if candidate.normalized_base == normalized), None)
            if entry is not None and entry.source_objt_offset >= 0:
                source_offset = entry.source_objt_offset

        def matches(candidate) -> bool:
            base_object = getattr(candidate, "base_object", None)
            if base_object is None:
                return False
            if source_offset is not None \
                    and getattr(base_object, "source_objt_offset", -1) \
                    == source_offset:
                return True
            name = str(getattr(base_object, "name", "") or "").strip()
            if not name:
                return False
            if not name.casefold().endswith(".base"):
                name += ".base"
            return normalize_base_name(name) == normalized

        target = next((obj for obj in family.all_objects()
                       if matches(obj)), None)
        if target is None:
            QMessageBox.information(
                self, "BASE preview unavailable",
                f"{base_name} is listed in the VP table, but its embedded "
                "BASE object could not be resolved. No file was modified.")
            self._raise_setbas_tab()
            return
        if family is not self._family:
            self._set_family(family)
        self._select_owner(target.owner_path)
        self._apply_selected_children_scope()
        self._raise_setbas_tab()
        self.statusBar().showMessage(
            f"Previewing BASE {base_name} [{target.owner_path}]", 8000)

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
        if self._family is None:
            return False
        owner = self._selected_owner
        obj = self._owner_to_obj.get(owner) if owner else None
        return bool(obj is not None and obj.skeleton is not None)

    def _editing_allowed(self) -> bool:
        """Single source of truth for every model-mutating operation."""

        button = getattr(self, "edit_toggle_action", None)
        return bool(
            button is not None and button.isChecked()
            and self.viewport.is_edit_mode
            and not self._snapshot_mode_active)

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
            self.snapshot_guides_button.setChecked(False)
            self.viewport.set_snapshot_guides_visible(False)
            self.mode_combo.blockSignals(True)
            self.mode_combo.setCurrentIndex(
                self.mode_combo.findData("textured"))
            self.mode_combo.blockSignals(False)
            self.mode_combo.setEnabled(False)
            self.edit_toggle_action.setEnabled(False)
            self.toolbar_view_preset_combo.setEnabled(False)
            for action in (self.sen_check, self.wire_check,
                           self.owner_bounds_check, self.axes_check,
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
            self.edit_toggle_action.setEnabled(True)
            self.toolbar_view_preset_combo.setEnabled(True)
            for action in (self.sen_check, self.wire_check,
                           self.owner_bounds_check, self.axes_check,
                           self.grid_check, self.overlay_check,
                           self.mapping_diag_check):
                action.setEnabled(True)
        self._sync_tab_edit_mode()
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
        self._sync_gizmo_camera()
        self._update_reset_camera_action()
        self._notify(f"View preset changed to {preset}.", 3500)

    def _on_manual_camera_changed(self) -> None:
        """Mark the active preset as Current View after user navigation."""

        self._sync_gizmo_camera()
        self._update_reset_camera_action()
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
        if action is not None:
            action.setEnabled(self.viewport.can_reset_camera)

    def _reset_view_and_gizmo(self) -> None:
        if not self.viewport.can_reset_camera:
            self._update_reset_camera_action()
            return
        self.viewport.reset_view()
        self._sync_gizmo_camera()
        self._update_reset_camera_action()

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

    def _on_animation_frame_changed(self, text: str = "") -> None:
        self._update_snapshot_frame_text(text)
        element = self._fx_element_for_polygon(self._selected_poly)
        if element is not None and element.source_kind == "VANM":
            self._update_uv_editor(self._selected_poly)
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
        self._update_editor_status()
        self._refresh_fx_elements()
        if not preserve_asset_selection:
            self._focus_assets_for_owner(owner, switch_tabs=False)
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
        if self._family is None:
            self.loaded_resource_label.setText("No Resource Loaded")
            self.loaded_resource_label.setToolTip("")
            self.unsaved_edits_label.clear()
            self.unsaved_edits_label.hide()
            return
        obj = (self._owner_to_obj.get(self._selected_owner)
               if self._selected_owner else None)
        selected = obj.display_name if obj else "NO RESOURCE SELECTED"
        n_dirty = (
            len(self._geom_dirty) + len(self._uv_original)
            + len(self._vanm_uv_original) + len(self._texture_original))
        self.loaded_resource_label.setText(selected)
        self.loaded_resource_label.setToolTip(selected)
        self.unsaved_edits_label.setText(f"Unsave edits: {n_dirty}")
        self.unsaved_edits_label.setVisible(n_dirty > 0)

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
                and (resolved.intersection(selected.poly_ids)
                     or self.fx_combo.currentIndex() > 0):
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
                "Copy, Cut or Delete operation")
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
                f"cut={capabilities.can_cut}, "
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
            self.viewport.set_highlight_polys(set())
            self._sync_edit_action_states()
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
                    element.owner_path, selected))
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
            can_cut=can_copy and can_delete,
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

    def _show_fx_combo_context_menu(self, position) -> None:
        menu = QMenu(self.fx_combo)
        add_action = menu.addAction(
            "Add FX Element...", self._add_fx_element)
        add_action.setEnabled(self._can_add_fx_element())
        delete_action = menu.addAction(
            "Delete Selected FX Element...",
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
    def _fx_uv_preview_image(
            image: QImage,
            groups: list[list[tuple[int, int]]]) -> QImage:
        """Draw the exact cloned UV outlines with the classic yellow style."""

        output = image.convertToFormat(
            QImage.Format.Format_ARGB32).copy()
        if output.isNull():
            return output
        painter = QPainter(output)
        color = QColor(255, 255, 90)
        painter.setPen(QPen(color, 1.6))
        painter.setBrush(color)
        width = output.width()
        height = output.height()
        for group in groups:
            points = QPolygonF([
                QPointF(
                    float(u) / 256.0 * width,
                    float(v) / 256.0 * height)
                for u, v in group
            ])
            if len(points) >= 2:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPolygon(points)
                painter.setBrush(color)
            for point in points:
                painter.drawEllipse(point, 2.5, 2.5)
        painter.end()
        return output

    def _can_add_fx_element(self) -> bool:
        return bool(
            self._editing_allowed()
            and not self.viewport.paste_preview_active
            and self._fx_add_templates())

    def _add_fx_element(
            self, *, preferred: FxElement | None = None) -> None:
        if not self._require_editing("Add FX Element"):
            return
        templates = self._fx_add_templates()
        if not templates:
            self._notify(
                "Add FX Element unavailable: this owner has no compatible "
                "existing FX1/FX2 or VANM quad material.", 9000)
            return
        dialog = AddFxElementDialog(
            templates, self, preferred=preferred,
            preview_provider=self._fx_template_preview)
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
            clipboard = build_fx_element_clipboard(
                fam_obj, template, family.animations)
        except GeometryClipboardError as exc:
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
            if additive:
                if was_selected:
                    self._selected_polys.remove(poly_id)
                else:
                    self._selected_polys.add(poly_id)
            else:
                self._selected_polys = {poly_id}
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
        obj = self._owner_to_obj.get(element.owner_path)
        model = getattr(obj, "skeleton", None)
        loops = []
        image = None
        descriptions = []
        editable_count = 0
        loop_keys = set()
        for poly_id, block_index, atts_index, block, material in \
                self._fx_face_records(element):
            if model is None or not (0 <= poly_id < len(model.polygons)):
                continue
            vertex_count = len(model.polygons[poly_id])
            uvs = []
            if element.source_kind == "VANM" \
                    or (block.texture is not None
                        and block.texture.kind == "bmpanim"):
                frame = self.viewport.current_animation_frame_data(
                    element.owner_path, block_index)
                animation_group = self.viewport.current_animation_group(
                    element.owner_path, block_index)
                if frame is not None:
                    if image is None:
                        image = frame[0]
                    uvs = list(frame[1][:vertex_count])
                    descriptions.append(frame[2])
            if not uvs and 0 <= atts_index < len(block.olpl):
                uvs = list(block.olpl[atts_index])
            if not uvs and poly_id == element.poly_ids[0]:
                uvs = list(element.olpl_uvs[:vertex_count])
            if image is None and material and material != "-":
                image = self._texture_qimage(material)
            if uvs:
                vanm_context = (
                    animation_group
                    if element.source_kind == "VANM" else None)
                key = (
                    ("@VANM", vanm_context[0], vanm_context[1])
                    if vanm_context is not None else
                    (element.owner_path, block_index, atts_index))
                if key in loop_keys:
                    continue
                loop_keys.add(key)
                editable = bool(self._editing_allowed() and (
                    (vanm_context is not None
                     and self._animation_uv_writable(
                         vanm_context[0], vanm_context[1]))
                    or (
                        element.editable
                        and element.source_kind == "direct"
                        and element.uv_source == "OLPL"
                        and (block.class_id or "").casefold() == "amesh.class"
                        and 0 <= atts_index < len(block.olpl)
                        and len(block.olpl[atts_index]) == vertex_count)))
                if editable:
                    context = (
                        (obj, None, vanm_context[0],
                         vanm_context[1], poly_id)
                        if vanm_context is not None else
                        (obj, block, block_index, atts_index, poly_id))
                    self._uv_contexts[key] = context
                    if self._uv_ctx is None and vanm_context is None:
                        self._uv_ctx = context
                    editable_count += 1
                loops.append(UVLoop(
                    key, poly_id, uvs, editable))
        if editable_count:
            message = (
                f"UV source: VANM. {editable_count} current frame group(s) "
                "are editable and exported as verified ANM files by Export "
                "Asset Family. "
                + "; ".join(dict.fromkeys(descriptions))
                if element.source_kind == "VANM" else
                f"UV source: OLPL. {editable_count} direct {element.fx_name} "
                "loop(s) are editable and exported through verified BASE "
                "output.")
        elif descriptions:
            message = (
                "UV source: VANM. No writable fixed-size UV group is "
                "available. Current "
                + "; ".join(dict.fromkeys(descriptions)))
        else:
            message = (
                f"UV source: {element.uv_source}. No unambiguous writable "
                "UV group is available for this element.")
        self.uv_editor.set_loops(image, loops, message)
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
            "Current texture: " + (label or "-"))
        self.load_texture_button.setToolTip(
            "Replace the selected FX segment texture or VANM binding without "
            "changing its geometry, winding or animation data.")
        self._set_polygon_object_info(lines)

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
            "Current texture: " + ", ".join(dict.fromkeys(texture_names)))
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

    # -- Model and Texture Editor -------------------------------------------------

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
        self.transform_help_label.setText(settings["help"])
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
        selected = set(selected_polys)
        unsafe = set()
        for element in self._fx_elements:
            if element.owner_path != owner:
                continue
            element_polys = set(element.poly_ids)
            if not (selected & element_polys):
                continue
            if element.shared_state == "invalid" \
                    or (element.bilateral
                        and not element_polys.issubset(selected)):
                unsafe.update(selected & element_polys)
        return unsafe

    def _copy_geometry(self) -> None:
        if self.viewport.paste_preview_active:
            self.viewport.cancel_paste_preview()
        reason = self._copy_geometry_reason()
        if reason:
            self._notify(f"Copy refused: {reason}.", 7000)
            return
        fx_elements = self._active_fx_elements()
        if fx_elements:
            self._copy_fx_elements(fx_elements)
            return
        owner = self.viewport.edit_owner
        fam_obj = self._owner_to_obj[owner]
        selected = self._resolved_geometry_polys()
        mapping = MappingIndex(fam_obj)
        try:
            clipboard = build_geometry_clipboard(
                fam_obj, owner, selected, mapping,
                self._unsafe_fx_geometry_polys(
                    owner, selected),
                require_writable_chunks=False)
        except GeometryClipboardError as exc:
            self._notify(f"Copy refused: {exc}.", 8000)
            return
        self._geometry_clipboard = clipboard
        position = self._current_viewport_position()
        started = self.viewport.begin_paste_preview(clipboard, position)
        self._sync_edit_action_states()
        self.viewport.setFocus()
        if not started:
            self._notify(
                f"Copied {len(clipboard.polygons)} polygon(s), but the preview "
                f"could not start: {self._preview_geometry_reason()}.", 9000)
            return
        self._notify(
            f"Copied {len(clipboard.polygons)} polygon(s). Move the transparent "
            "preview and press Paste, LMB or Enter to confirm.", 12000)

    def _copy_fx_element(self, element: FxElement) -> None:
        self._copy_fx_elements((element,))

    def _copy_fx_elements(self, elements) -> None:
        elements = tuple(elements)
        if not elements:
            return
        element = elements[0]
        family = self._family
        fam_obj = self._owner_to_obj.get(element.owner_path)
        if family is None or fam_obj is None:
            self._notify("Copy FX refused: the source model is unavailable.",
                         8000)
            return
        try:
            clipboard = build_fx_elements_clipboard(
                fam_obj, elements, family.animations)
        except GeometryClipboardError as exc:
            self._notify(f"Copy FX refused: {exc}.", 9000)
            return
        self._selected_polys = {
            poly_id for selected in elements for poly_id in selected.poly_ids}
        self.viewport.set_highlight_polys(self._selected_polys)
        self._geometry_clipboard = clipboard
        position = self._current_viewport_position()
        started = self.viewport.begin_paste_preview(clipboard, position)
        self._sync_edit_action_states()
        self.viewport.setFocus()
        descriptor = (
            f"{len(elements)} mirrored {element.fx_name} "
            f"{'VANM ' if element.source_kind == 'VANM' else ''}elements"
            if len(elements) > 1 else
            f"{element.fx_name} "
            f"{'VANM ' if element.source_kind == 'VANM' else ''}Element")
        if not started:
            self._notify(
                f"Copied {descriptor}, but its preview could not start: "
                f"{self._preview_geometry_reason()}.", 9000)
            return
        self._notify(
            f"Copied {descriptor}. Move the transparent FX preview and press "
            "Paste, LMB or Enter to confirm.", 12000)

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
            element = next(
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
        self._sync_edit_action_states()

    def _copy_geometry_reason(self) -> str:
        session, owner, fam_obj = self._geometry_context()
        if self.viewport.paste_preview_active:
            return "finish or cancel the active Copy Preview first"
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

    def can_copy_geometry(self) -> bool:
        return not self._copy_geometry_reason()

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
                validate_fx_element_clipboard(
                    fam_obj, clipboard,
                    self._family.animations if self._family else {})
            except GeometryClipboardError as exc:
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

    def can_export_ilbm(self) -> bool:
        family = self._family
        return bool(
            family is not None
            and any(getattr(image, "has_body", False)
                    for image in family.textures.values()))

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

    def can_overwrite_geometry(self) -> bool:
        owner = self.viewport.edit_owner or self._selected_owner
        fam_obj = self._owner_to_obj.get(owner) if owner else None
        if self.can_save_as_geometry() and owner in self._bundle_targets:
            return True
        return bool(
            self.can_export_sklt() and fam_obj is not None
            and self._standalone_sklt_source(fam_obj) is not None)

    def _is_embedded_geometry_source(self, fam_obj) -> bool:
        ref = getattr(fam_obj, "skeleton_ref", None)
        path = getattr(ref, "path", None)
        status = (getattr(ref, "status", "") or "").lower()
        source = (getattr(ref, "source", "") or "").lower()
        return path is None or status in ("setbas", "manual (set.bas)") \
            or source == "set.bas"

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
                self._geometry_clipboard, position):
            self.viewport.setFocus()
            label = (
                "Paste FX Preview"
                if isinstance(self._geometry_clipboard, FxElementClipboard)
                else "Copy Preview")
            self._notify(
                f"{label}: move the transparent copy; Paste/LMB/Enter "
                "confirms; RMB/Esc cancels.", 12000)

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

    def _confirm_paste_geometry(self) -> None:
        if not self._require_editing("Paste Geometry"):
            if self.viewport.paste_preview_active:
                self.viewport.cancel_paste_preview()
            return
        clipboard = self._geometry_clipboard
        delta = self.viewport.paste_preview_delta
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
            if isinstance(clipboard, FxElementClipboard):
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
                "kind": "topology",
                "owner": owner,
                "before": before,
                "after": after,
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
            self._unsafe_fx_geometry_polys(owner, selected))

    def _delete_geometry_reason(self) -> str:
        if not self._editing_allowed():
            return "enable Edit Mode on one model"
        session, owner, fam_obj = self._geometry_context()
        if self.viewport.paste_preview_active:
            return "cancel the active Copy Preview first"
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
        selected = (
            set(element.poly_ids) if element is not None
            else self._resolved_geometry_polys())
        if not selected:
            return "select one or more complete polygons first"
        if any(key[0] == owner for key in self._uv_original):
            return "save or reset pending UV edits before changing topology"
        if self._owner_vanm_uv_keys(owner):
            return "save or reset pending VANM UV edits before changing topology"
        if any(key[0] == owner for key in self._texture_original):
            return (
                "save or reset pending material previews before changing "
                "topology")
        try:
            mapping = MappingIndex(fam_obj)
            plan_delete_geometry(
                fam_obj, selected, mapping,
                self._unsafe_fx_geometry_polys(
                    owner, selected))
        except GeometryClipboardError as exc:
            return str(exc)
        return ""

    def can_delete_geometry(self) -> bool:
        return not self._delete_geometry_reason()

    def can_cut_geometry(self) -> bool:
        return self.can_copy_geometry() and self.can_delete_geometry()

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
            f"Delete {len(fx_elements)} complete mirrored FX elements "
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
                    "Delete mirrored FX elements"
                    if len(fx_elements) > 1 else
                    f"Delete {element.fx_name} FX"
                    if element is not None else "Delete Geometry"))
        except GeometryClipboardError as exc:
            self._notify(f"Delete refused: {exc}.", 8000)
            return
        deleted_message = (
            f"Deleted {len(fx_elements)} complete mirrored FX elements. "
            if len(fx_elements) > 1 else
            f"Deleted complete {element.fx_name} FX element. "
            if element is not None else
            f"Deleted {deleted} selected polygon(s). "
        ) + "This operation can be undone."
        self._notify(deleted_message, 8000)

    def _cut_geometry(self) -> None:
        if not self._require_editing("Cut Geometry"):
            return
        reason = self._copy_geometry_reason()
        if reason:
            self._notify(f"Cut refused: {reason}.", 8000)
            return
        owner = self.viewport.edit_owner
        fam_obj = self._owner_to_obj[owner]
        element = self._active_fx_selection()
        fx_elements = self._active_fx_elements()
        selected = self._resolved_geometry_polys()
        mapping = MappingIndex(fam_obj)
        try:
            if fx_elements:
                clipboard = build_fx_elements_clipboard(
                    fam_obj, fx_elements,
                    self._family.animations if self._family else {})
            else:
                clipboard = build_geometry_clipboard(
                    fam_obj, owner, selected, mapping,
                    self._unsafe_fx_geometry_polys(
                        owner, selected),
                    require_writable_chunks=False)
            plan = plan_delete_geometry(
                fam_obj, selected, mapping,
                self._unsafe_fx_geometry_polys(
                    owner, selected))
        except GeometryClipboardError as exc:
            self._notify(f"Cut refused: {exc}.", 8000)
            return
        previous_clipboard = self._geometry_clipboard
        try:
            cut_count = self._apply_delete_plan(
                owner, fam_obj, plan,
                label=(
                    "Cut mirrored FX elements"
                    if len(fx_elements) > 1 else
                    f"Cut {element.fx_name} FX"
                    if element is not None else "Cut Geometry"))
        except GeometryClipboardError as exc:
            self._geometry_clipboard = previous_clipboard
            self._notify(f"Cut refused: {exc}.", 8000)
            return
        self._geometry_clipboard = clipboard
        self.viewport.setFocus()
        self._sync_edit_action_states()
        self._notify(
            (
                f"Cut {len(fx_elements)} mirrored FX elements. "
                "Press Paste to place the copied FX selection."
                if len(fx_elements) > 1 else
                f"Cut {element.fx_name} "
                f"{'VANM ' if element.source_kind == 'VANM' else ''}"
                "FX Element. Press Paste to place the copied FX."
                if element is not None else
                f"Cut {cut_count} polygon(s). Press Paste to place the "
                "copied geometry."
            ), 9000)

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
        has_selection = bool(self._selected_polys)
        if session is not None:
            has_selection = has_selection or bool(session.selection)
        has_selection = (
            has_selection or self._active_fx_selection() is not None)
        deselect.setEnabled(has_selection)
        menu.addSeparator()
        active_fx = self._active_fx_selection()
        copy_label = "Copy FX Element" if active_fx is not None else "Copy"
        copy_geometry = menu.addAction(copy_label, self._copy_geometry)
        copy_geometry.setEnabled(self.can_copy_geometry())
        clipboard_is_fx = isinstance(
            self._geometry_clipboard, FxElementClipboard)
        paste_label = "Paste FX Element" if clipboard_is_fx else "Paste"
        paste_geometry = menu.addAction(
            paste_label, lambda: self._paste_geometry(position))
        paste_geometry.setEnabled(
            editing and self._geometry_clipboard is not None)
        preview_reason = self._preview_geometry_reason()
        commit_reason = (
            self._commit_geometry_reason() if not preview_reason else "")
        paste_geometry.setToolTip(
            "Confirm the current Copy Preview."
            if self.viewport.paste_preview_active else
            preview_reason or (
                f"Preview is available; confirmation will be refused: "
                f"{commit_reason}."
                if commit_reason else
                "Start a Copy Preview."))
        cut_geometry = menu.addAction(
            "Cut FX Element" if active_fx is not None else "Cut",
            self._cut_geometry)
        cut_geometry.setEnabled(self.can_cut_geometry())
        if active_fx is not None:
            active_fx_elements = self._active_fx_elements()
            delete_geometry = menu.addAction(
                "Delete Mirrored FX Elements..."
                if len(active_fx_elements) > 1 else
                "Delete FX Element...",
                self._delete_geometry
                if len(active_fx_elements) > 1 else
                self._delete_selected_fx_element)
            delete_geometry.setEnabled(self.can_delete_geometry())
        else:
            delete_geometry = menu.addAction("Delete", self._delete_geometry)
            delete_geometry.setEnabled(self.can_delete_geometry())
        add_fx = menu.addAction(
            "Add Similar FX Element..." if active_fx is not None
            else "Add FX Element...",
            (lambda: self._add_fx_element(preferred=active_fx))
            if active_fx is not None else self._add_fx_element)
        add_fx.setEnabled(self._can_add_fx_element())
        scale = menu.addAction(
            "Scale...", self._scale_selected_geometry)
        scale.setEnabled(editing and self._family is not None)
        rotate = menu.addAction(
            "Rotate...", self._rotate_selected_geometry)
        rotate.setEnabled(editing and self._family is not None)
        menu.addSeparator()
        reset_camera = menu.addAction(
            "Reset camera", self._reset_view_and_gizmo)
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
        copy_reason = self._copy_geometry_reason()
        delete_reason = self._delete_geometry_reason()
        can_copy = not copy_reason
        can_delete = not delete_reason
        if hasattr(self, "edit_toggle_action"):
            self.edit_toggle_action.setText(
                "View Mode" if active else "Edit Mode")
            self.edit_toggle_action.setEnabled(not paste_preview)
        for name in ("edit_select_all_action", "edit_select_none_action"):
            action = getattr(self, name, None)
            if action is not None:
                action.setEnabled(active and not paste_preview)
        for name in ("poly_select_all_button", "poly_deselect_all_button"):
            button = getattr(self, name, None)
            if button is not None:
                button.setEnabled(
                    editing and active and not paste_preview)
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
        if hasattr(self, "paste_geometry_action"):
            has_clipboard = self._geometry_clipboard is not None
            self.paste_geometry_action.setEnabled(
                editing and (has_clipboard or paste_preview))
            fx_clipboard = isinstance(
                self._geometry_clipboard, FxElementClipboard)
            paste_reason = (
                "Confirm the current Paste FX Preview."
                if paste_preview and fx_clipboard else
                "Confirm the current Copy Preview."
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
        if hasattr(self, "cut_geometry_action"):
            self.cut_geometry_action.setEnabled(
                editing and can_copy and can_delete)
            cut_tip = (
                f"Copy and atomically remove the complete {fx_descriptor}."
                if active_fx is not None else
                "Copy and atomically remove the selected geometry.")
            self.cut_geometry_action.setToolTip(cut_tip)
            self.cut_geometry_action.setStatusTip(cut_tip)
        if hasattr(self, "delete_geometry_action"):
            self.delete_geometry_action.setEnabled(
                editing and can_delete)
            delete_tip = (
                f"Delete the complete {fx_descriptor}."
                if active_fx is not None else
                "Delete selected geometry and compact unused vertices.")
            self.delete_geometry_action.setToolTip(delete_tip)
            self.delete_geometry_action.setStatusTip(delete_tip)
        can_add_fx = editing and self._can_add_fx_element()
        if hasattr(self, "add_fx_action"):
            self.add_fx_action.setEnabled(can_add_fx)
        if hasattr(self, "add_fx_button"):
            self.add_fx_button.setEnabled(can_add_fx)
        if hasattr(self, "delete_fx_button"):
            can_delete_fx = (
                editing
                and self._selected_fx_element() is not None
                and self._selected_fx_element().shared_state != "invalid"
                and can_delete)
            self.delete_fx_button.setEnabled(can_delete_fx)
            element = self._selected_fx_element()
            if element is None:
                delete_fx_tip = "Select an FX element from the list first."
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
        if hasattr(self, "edit_scale_action"):
            self.edit_scale_action.setEnabled(
                editing and self._family is not None and not paste_preview)
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
        sklt_export_enabled = self.can_export_sklt()
        ilbm_export_enabled = self.can_export_ilbm()
        overwrite_enabled = self.can_overwrite_geometry()
        self.model_save_button.setEnabled(overwrite_enabled)
        self.model_save_as_button.setEnabled(family_export_enabled)
        self.save_asset_family_action.setEnabled(family_export_enabled)
        self.save_base_action.setEnabled(family_export_enabled)
        self.save_sklt_action.setEnabled(sklt_export_enabled)
        self.save_ilbm_action.setEnabled(ilbm_export_enabled)
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

    def _has_unsaved_vp_changes(self) -> bool:
        return bool(self._vp_manager and self._vp_manager.can_undo)

    def _confirm_discard_vp_changes(self, message: str) -> bool:
        if not self._has_unsaved_vp_changes():
            return True
        answer = QMessageBox.question(
            self, "Unexported VP assignments",
            message + "\n\nSET.BAS has not been modified.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        return answer == QMessageBox.StandardButton.Yes

    def _confirm_discard_geometry(self) -> bool:
        """Confirm switching away from unsaved model or VP state."""

        model_dirty = bool(
            self._geom_dirty or self._texture_original
            or self._uv_original or self._vanm_uv_original
            or self._pending_repairs)
        vp_dirty = self._has_unsaved_vp_changes()
        if not model_dirty and not vp_dirty:
            return True
        if self._skip_model_switch_warning and not vp_dirty:
            return True
        if model_dirty and vp_dirty:
            message = (
                "This workbench has unsaved model changes and unexported "
                "VP assignments.\nSwitching will discard them. Continue?")
        elif vp_dirty:
            message = (
                "This VP table has unexported assignments.\n"
                "Switching will discard them. Continue?")
        else:
            message = (
                "This model has unsaved changes.\n"
                "Switching models will discard them. Continue?")
        box = QMessageBox(QMessageBox.Icon.Question, "Unsaved changes",
                          message,
                          QMessageBox.StandardButton.Yes
                          | QMessageBox.StandardButton.No, self)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        skip = None
        if not vp_dirty:
            skip = QCheckBox("Don't show this again during this session")
            box.setCheckBox(skip)
        accepted = box.exec() == QMessageBox.StandardButton.Yes
        if accepted and skip is not None and skip.isChecked():
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

    @staticmethod
    def _bundle_animation_relative_path(name: str) -> Path:
        logical = str(name).replace("\\", "/")
        parts = []
        for raw in logical.split("/"):
            if not raw or raw in (".", ".."):
                continue
            clean = re.sub(r'[^A-Za-z0-9_. -]', "_", raw).strip()
            if clean:
                parts.append(clean)
        relative = Path(*parts) if parts else Path("ANIMATION.ANM")
        if relative.suffix.lower() not in (".anm", ".vanm"):
            relative = relative.with_suffix(".ANM")
        return relative

    @staticmethod
    def _bundle_texture_relative_path(name: str) -> Path:
        logical = str(name).replace("\\", "/")
        parts = []
        for raw in logical.split("/"):
            if not raw or raw in (".", ".."):
                continue
            clean = re.sub(r'[^A-Za-z0-9_. -]', "_", raw).strip()
            if clean:
                parts.append(clean)
        relative = Path(*parts) if parts else Path("TEXTURE.ILBM")
        if relative.suffix.lower() not in (
                ".ilbm", ".ilb", ".lbm", ".iff", ".vbmp"):
            relative = relative.with_suffix(".ILBM")
        return relative

    @staticmethod
    def _matching_ref(mapping: dict, logical_name: str):
        return next((value for key, value in mapping.items()
                     if str(key).casefold() == str(logical_name).casefold()),
                    None)

    def _texture_export_source(self, family: AssetFamily, name: str):
        """Return raw texture bytes and its loose source path when available."""

        ref = self._matching_ref(family.texture_refs, name)
        source = (Path(ref.path) if ref is not None
                  and getattr(ref, "path", None) else None)
        if source is not None and source.is_file():
            return source.read_bytes(), source
        archive = family.setbas_archive or self._setbas
        if archive is not None:
            resource = next((entry for entry in archive.find(
                name, "ilbm.class") if entry.decodable), None)
            if resource is not None:
                return archive.payload_bytes(resource), None
        return None, source

    def _bundle_base_edits(self, owner: str, fam_obj):
        texture_edits: list[TextureNameEdit] = []
        for (changed_owner, block_index, binding_slot), original in \
                self._texture_original.items():
            if changed_owner != owner:
                continue
            blocks = fam_obj.base_object.ades
            if not (0 <= block_index < len(blocks)) \
                    or binding_slot not in ("texture", "tracy_texture") \
                    or getattr(blocks[block_index], binding_slot) is None:
                raise MappingEditError(
                    "an edited texture binding no longer exists")
            name = getattr(blocks[block_index], binding_slot).name
            if name.casefold() != original.casefold():
                texture_edits.append(TextureNameEdit(
                    "root", block_index, name, binding_slot))

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
                self, "Export unavailable",
                "This model does not contain both model and BASE data to "
                "export.")
            return None
        return owner, family, fam_obj, model, tree

    def _bundle_topology_states(
            self, data: bytes, fam_obj) -> list[StructuralBlockState]:
        parsed = parse_base_bytes(data, "<saved-model-base>")
        if parsed.root is None:
            raise MappingEditError(
                "the saved BASE snapshot has no parseable root object")
        saved_blocks = parsed.root.ades
        current_blocks = fam_obj.base_object.ades
        model = fam_obj.skeleton
        mapping = MappingIndex(fam_obj)
        if mapping.invalid:
            raise MappingEditError(
                "the in-memory BASE has invalid ATTS polygon references")
        if mapping.duplicates:
            raise MappingEditError(
                "the in-memory BASE has duplicate polygon mappings")
        diagnostics = [
            issue for issue in diagnose_polygon_references(
                fam_obj, detect_fx_elements(
                    fam_obj, self._family.animations
                    if self._family else {}))
            if "missing typed handler" not in issue
        ]
        if diagnostics:
            raise MappingEditError(
                "the in-memory reference graph is invalid: "
                + "; ".join(diagnostics[:4]))

        def template_signature(block) -> tuple:
            texture = block.texture
            tracy = block.tracy_texture
            return (
                (block.class_id or "").lower(),
                block.payload_form_type,
                block.ade_flags,
                block.ade_point_id,
                block.area_flags,
                block.polflags,
                block.color_val,
                block.tracy_val,
                block.shade_val,
                (
                    texture.class_id, texture.kind, texture.name,
                    texture.anim_type, tuple(texture.outline_uvs)
                ) if texture is not None else None,
                (
                    tracy.class_id, tracy.kind, tracy.name,
                    tracy.anim_type, tuple(tracy.outline_uvs)
                ) if tracy is not None else None,
            )

        def template_for(block, block_index: int) -> bytes:
            source = block.source_objt_bytes
            exact = next(
                (saved.source_objt_bytes for saved in saved_blocks
                 if source and saved.source_objt_bytes == source), None)
            if exact is not None:
                return exact
            signature = template_signature(block)
            compatible = [
                saved for saved in saved_blocks
                if template_signature(saved) == signature
            ]
            if compatible:
                candidate = (
                    saved_blocks[block_index]
                    if block_index < len(saved_blocks)
                    and saved_blocks[block_index] in compatible
                    else compatible[0])
                return candidate.source_objt_bytes
            if source:
                return source
            raise MappingEditError(
                f"block #{block_index} ({block.class_id or 'unknown class'}) "
                "has no parsed source OBJT")

        states = []
        for block_index, current in enumerate(current_blocks):
            class_id = (current.class_id or "").lower()
            atts_only = bool(
                class_id == "amesh.class"
                and current.texture is not None
                and current.texture.kind == "bmpanim"
                and not current.olpl)
            if class_id == "amesh.class" \
                    and not atts_only \
                    and len(current.atts) != len(current.olpl):
                raise MappingEditError(
                    f"material block #{block_index} has ambiguous "
                    "ATTS/OLPL counts")
            for index, entry in enumerate(current.atts):
                if not (0 <= entry.poly_id < len(model.polygons)):
                    raise MappingEditError(
                        f"block #{block_index} mapping #{index} has invalid "
                        f"polyID {entry.poly_id}")
                if atts_only:
                    continue
                if index >= len(current.olpl):
                    if class_id != "area.class":
                        raise MappingEditError(
                            f"block #{block_index} mapping #{index} has no "
                            "matching OLPL group")
                    continue
                uvs = current.olpl[index]
                expected_uvs = len(model.polygons[entry.poly_id])
                if len(uvs) != expected_uvs:
                    raise MappingEditError(
                        f"block #{block_index} mapping #{index} has "
                        f"{len(uvs)} UVs for {expected_uvs} vertices")
            states.append(StructuralBlockState(
                block_index=block_index,
                atts=tuple(copy.deepcopy(current.atts)),
                olpl=(
                    None if atts_only else tuple(
                        tuple(tuple(uv) for uv in group)
                        for group in current.olpl)),
                class_id=current.class_id,
                payload_form_type=current.payload_form_type,
                ade_poly_id=(
                    current.ade_poly_id
                    if class_id == "area.class" else None),
                template_objt=template_for(current, block_index),
            ))
        return states

    @staticmethod
    def _adopt_saved_block_sources(fam_obj, data: bytes) -> None:
        parsed = parse_base_bytes(data, "<saved-block-sources>")
        if parsed.root is None \
                or len(parsed.root.ades) != len(fam_obj.base_object.ades):
            raise MappingEditError(
                "verified BASE block sources do not match in-memory ADES")
        source_fields = (
            "payload_form_type", "source_ades_index",
            "source_objt_offset", "source_objt_size", "source_objt_bytes",
            "ade_strc_chunk_offset", "ade_strc_chunk_size",
            "atts_chunk_offset", "atts_chunk_size",
            "olpl_chunk_offset", "olpl_chunk_size",
        )
        for current, saved in zip(
                fam_obj.base_object.ades, parsed.root.ades):
            if (current.class_id or "").lower() \
                    != (saved.class_id or "").lower():
                raise MappingEditError(
                    "verified BASE block class order changed")
            for name in source_fields:
                setattr(current, name, copy.deepcopy(getattr(saved, name)))

    @staticmethod
    def _write_verified_sklt(model, output_path: str | Path):
        """Write a SKLT copy and verify its geometry round-trip."""

        original = parse_sklt_bytes(
            model.original_data, "<model-save-baseline>")
        structural = (
            len(model.points) != len(original.points)
            or model.polygons != original.polygons
        )
        if structural:
            save_sklt_with_poo2_pol2_structure(
                model, model.points, model.polygons, output_path)
        else:
            save_sklt_with_poo2_points(model, model.points, output_path)
        verified = parse_sklt_file(output_path)
        matches = (
            len(verified.points) == len(model.points)
            and verified.polygons == model.polygons
            and all(
                abs(a[axis] - b[axis])
                <= 1e-3 + abs(b[axis]) * 1e-5
                for a, b in zip(verified.points, model.points)
                for axis in range(3)
            )
        )
        if not matches:
            raise MappingEditError(
                "exported SKLT failed coordinate round-trip verification")
        return verified

    def _save_base_as(self) -> None:
        context = self._model_save_context()
        if context is None:
            return
        owner, family, fam_obj, _model, tree = context
        base_label = fam_obj.base_object.name or "MODEL"
        suggested = self._last_directory / f"{Path(base_label).stem}.BASE"
        output, _selected_filter = QFileDialog.getSaveFileName(
            self, "Export BASE", str(suggested),
            "BASE model (*.BASE *.base);;All files (*)")
        if not output:
            return
        target = Path(output)
        if target.suffix.casefold() != ".base":
            target = target.with_suffix(".BASE")
        source = Path(family.base_path) if family.base_path else None
        if source is not None and target.resolve() == source.resolve():
            QMessageBox.warning(
                self, "Choose another output file",
                "Export BASE cannot replace the BASE currently open. "
                "Choose a different destination.")
            return
        try:
            standalone = self._bundle_base_snapshots.get(owner)
            if standalone is None:
                standalone = export_base_object_bytes(
                    tree.data, fam_obj.base_object)
            uv_edits, texture_edits = self._bundle_base_edits(owner, fam_obj)
            structural_blocks = self._bundle_topology_states(
                standalone, fam_obj)
            notes = save_model_base_copy(
                standalone, uv_edits, texture_edits, target,
                structural_blocks=structural_blocks)
        except (MappingEditError, OSError) as exc:
            QMessageBox.critical(
                self, "Export BASE failed - nothing written", str(exc))
            return
        self._last_directory = target.parent
        self._log("BASE export: " + "; ".join(notes))
        self._notify(f"BASE exported to {target}.", 8000)

    def _save_sklt_as(self) -> None:
        context = self._sklt_save_context(notify=True)
        if context is None:
            return
        _owner, _family, fam_obj, model = context
        relative = self._bundle_skeleton_relative_path(fam_obj)
        suggested = self._last_directory / relative.name
        output, _selected_filter = QFileDialog.getSaveFileName(
            self, "Export SKLT", str(suggested),
            "Skeleton (*.SKLT *.sklt);;All files (*)")
        if not output:
            return
        target = Path(output)
        if target.suffix.casefold() not in (".sklt", ".skl"):
            target = target.with_suffix(".SKLT")
        source_ref = getattr(fam_obj, "skeleton_ref", None)
        source = getattr(source_ref, "path", None)
        if source is not None and target.resolve() == Path(source).resolve():
            QMessageBox.warning(
                self, "Choose another output file",
                "Export SKLT cannot replace the skeleton currently open. "
                "Choose a different destination.")
            return
        try:
            with tempfile.TemporaryDirectory(
                    prefix="OpenUAStudio_sklt_") as temp_dir:
                temporary = Path(temp_dir) / target.name
                self._write_verified_sklt(model, temporary)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(temporary.read_bytes())
        except (MappingEditError, OSError, SkltParseError) as exc:
            QMessageBox.critical(
                self, "Export SKLT failed - nothing written", str(exc))
            return
        self._last_directory = target.parent
        self._notify(f"SKLT exported to {target}.", 8000)

    def _current_model_texture_name(self) -> str | None:
        """Return the texture bound to the current polygon/UV selection."""

        if self._uv_ctx is not None:
            _fam_obj, block, _block_index, _atts_index, _poly_id = self._uv_ctx
            texture = getattr(block, "texture", None) if block is not None else None
            name = getattr(texture, "name", "") if texture is not None else ""
            if name:
                return str(name)
        if self._selected_poly is None or self._mapping_index is None:
            return None
        refs = self._mapping_index.refs.get(self._selected_poly, [])
        if len(refs) != 1:
            return None
        texture = getattr(refs[0].block, "texture", None)
        name = getattr(texture, "name", "") if texture is not None else ""
        return str(name) if name else None

    def _save_ilbm_as(self) -> None:
        if self._family is None:
            self._notify("Import an asset family before exporting an ILBM.", 5000)
            return
        names = self._selected_texture_names()
        if not names:
            item = self.texture_list.currentItem()
            name = item.data(Qt.ItemDataRole.UserRole) if item else None
            if name:
                names = [str(name)]
        if not names:
            current_name = self._current_model_texture_name()
            if current_name:
                names = [current_name]
        if not names:
            self._notify(
                "Select a texture in the list or a textured polygon first.",
                5000)
            return
        name = names[0]
        loaded_name = self._ensure_model_texture_loaded(name)
        image = (self._family.textures.get(loaded_name)
                 if loaded_name is not None else None)
        if image is None or not image.has_body:
            self._notify("The selected texture is not decoded.", 5000)
            return
        safe_name = Path(name.replace("\\", "/")).stem or "texture"
        suggested = self._last_directory / f"{safe_name}.ILBM"
        output, _selected_filter = QFileDialog.getSaveFileName(
            self, "Export ILBM", str(suggested),
            "ILBM texture (*.ILBM *.ilbm);;All files (*)")
        if not output:
            return
        target = Path(output)
        if target.suffix.casefold() not in (".ilbm", ".ilb"):
            target = target.with_suffix(".ILBM")
        palette = image.palette
        if palette is None:
            palette = self._family.external_palette
        if palette is None and self._setbas is not None:
            palette, _palette_source = self._setbas_palette()
        try:
            result = write_image_as_ilbm(
                image, target, palette, source=name)
        except (TextureConvertError, OSError) as exc:
            QMessageBox.critical(
                self, "Export ILBM failed - nothing written", str(exc))
            return
        self._last_directory = target.parent
        self._notify(f"ILBM exported to {result.output}.", 8000)

    def _save_model_as(self) -> None:
        context = self._model_save_context()
        if context is None:
            return
        owner, family, fam_obj, _model, _tree = context
        skeleton_relative = self._bundle_skeleton_relative_path(fam_obj)
        base_label = (fam_obj.base_object.name
                      or skeleton_relative.stem)
        base_stem = re.sub(r'[^A-Za-z0-9_.-]', "_",
                           base_label) or "MODEL"
        if Path(base_stem).suffix.casefold() == ".base":
            base_stem = Path(base_stem).stem or "MODEL"
        suggested = self._last_directory / f"{base_stem}.BASE"
        output, _selected_filter = QFileDialog.getSaveFileName(
            self, "Export Asset Family", str(suggested),
            "BASE model (*.BASE *.base);;All files (*.*)",
            options=QFileDialog.Option.DontConfirmOverwrite)
        if not output:
            self._notify("Asset-family export cancelled.", 3000)
            return
        base_target = Path(output)
        if base_target.suffix.casefold() != ".base":
            base_target = base_target.with_suffix(".BASE")
        output_root = base_target.parent
        skeleton_target = output_root / skeleton_relative
        if self._write_model_files(
                owner, family, fam_obj, skeleton_target, base_target,
                ask_replace=True):
            self._bundle_targets[owner] = (skeleton_target, base_target)
            self._last_directory = output_root
            self._sync_geometry_save_controls()
            self._notify(
                f"Exported complete asset family rooted at "
                f"{base_target.name}.", 9000)

    def _overwrite_model(self) -> None:
        sklt_context = self._sklt_save_context()
        if sklt_context is not None:
            owner, _family, fam_obj, model = sklt_context
            source = self._standalone_sklt_source(fam_obj)
            if source is not None:
                answer = QMessageBox.question(
                    self, "Overwrite SKLT",
                    f"Overwrite the imported skeleton?\n\n{source}\n\n"
                    "A .bak copy will be created first.",
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
                backup = _next_backup_path(source)
                try:
                    source.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, backup)
                    with tempfile.TemporaryDirectory(
                            prefix="OpenUAStudio_overwrite_sklt_") as temp_dir:
                        temporary = Path(temp_dir) / source.name
                        self._write_verified_sklt(model, temporary)
                        warnings = _commit_verified_files(
                            [(temporary, source)])
                except (MappingEditError, OSError, SkltParseError,
                        _BundleCommitError) as exc:
                    QMessageBox.critical(
                        self, "Overwrite SKLT failed - original preserved",
                        f"{exc}\n\nBackup: "
                        f"{backup if backup.exists() else '-'}")
                    return
                self._geom_dirty.pop(owner, None)
                self._geom_original.pop(owner, None)
                self._topology_original.pop(owner, None)
                details = f" Backup: {backup.name}."
                if warnings:
                    details += " " + " ".join(warnings)
                self._sync_geometry_save_controls()
                self._update_editor_status()
                self._notify(
                    f"SKLT overwritten safely: {source.name}.{details}",
                    10000)
                return

        context = self._model_save_context()
        if context is None:
            return
        owner, family, fam_obj, _model, _tree = context
        targets = self._bundle_targets.get(owner)
        if targets is None:
            self._notify("Export Asset Family before Overwrite.", 5000)
            return
        skeleton_target, base_target = targets
        if self._write_model_files(
                owner, family, fam_obj, skeleton_target, base_target,
                ask_replace=False):
            self._notify(
                f"Overwritten complete asset family rooted at "
                f"{base_target.name}.", 9000)

    def _write_model_files(self, owner: str, family: AssetFamily, fam_obj,
                           skeleton_target: Path, base_target: Path,
                           *, ask_replace: bool) -> bool:
        """Export one complete selected asset family with safe verification."""

        model = fam_obj.skeleton
        tree = family.base_asset.tree
        source_archive_digest = hashlib.sha256(tree.data).digest()
        base_source = Path(family.base_path) if family.base_path else None

        # Scope dependencies to the selected BASE object and its KIDS.  A
        # SET.BAS session can contain hundreds of unrelated resources; an
        # asset-family export must never dump the whole archive by accident.
        family_objects = list(fam_obj.iter_tree())
        skeleton_exports = []
        skeleton_targets_by_key: dict[str, tuple[object, Path]] = {}
        for object_index, current_obj in enumerate(family_objects):
            current_model = getattr(current_obj, "skeleton", None)
            if current_model is None:
                QMessageBox.critical(
                    self, "Export failed - current files unchanged",
                    f"Skeleton dependency for {current_obj.display_name} is "
                    "unresolved; the asset family would be incomplete.")
                return False
            relative = self._bundle_skeleton_relative_path(current_obj)
            target = (skeleton_target if object_index == 0
                      else base_target.parent / relative)
            key = str(target.resolve()).casefold()
            previous = skeleton_targets_by_key.get(key)
            if previous is not None:
                previous_model, previous_target = previous
                if previous_model is not current_model:
                    QMessageBox.critical(
                        self, "Export failed - current files unchanged",
                        f"Two different skeletons resolve to the same output "
                        f"path: {previous_target}")
                    return False
                continue
            skeleton_targets_by_key[key] = (current_model, target)
            ref = getattr(current_obj, "skeleton_ref", None)
            source = (Path(ref.path) if ref is not None
                      and getattr(ref, "path", None) else None)
            skeleton_exports.append(
                (current_obj, current_model, relative, target, source))

        animation_names: set[str] = set()
        texture_names: set[str] = set()
        for current_obj in family_objects:
            for block in current_obj.base_object.ades:
                visual_blocks = (block.iter_visual_blocks()
                                 if hasattr(block, "iter_visual_blocks")
                                 else (block,))
                for visual in visual_blocks:
                    for texture in (
                            getattr(visual, "texture", None),
                            getattr(visual, "tracy_texture", None)):
                        name = str(getattr(texture, "name", "") or "")
                        if not name:
                            continue
                        if str(getattr(texture, "kind", "")).casefold() \
                                == "bmpanim":
                            animation_names.add(name)
                        else:
                            texture_names.add(name)

        animation_exports = []
        for canonical in sorted(animation_names, key=str.casefold):
            animation = next((
                value for key, value in family.animations.items()
                if key.casefold() == canonical.casefold()), None)
            if animation is None:
                QMessageBox.critical(
                    self, "Export failed - current files unchanged",
                    f"Animation dependency {canonical} is unresolved; the "
                    "asset family would be incomplete.")
                return False
            animation_exports.append((
                canonical, animation,
                self._bundle_animation_relative_path(canonical),
            ))
            texture_names.update(animation.bitmap_names)

        texture_exports = []
        for canonical in sorted(texture_names, key=str.casefold):
            try:
                raw_data, loose_source = self._texture_export_source(
                    family, canonical)
            except OSError as exc:
                QMessageBox.critical(
                    self, "Export failed - current files unchanged",
                    f"Texture dependency {canonical} could not be read: "
                    f"{exc}")
                return False
            image = next((
                value for key, value in family.textures.items()
                if key.casefold() == canonical.casefold()), None)
            if raw_data is None and image is None:
                QMessageBox.critical(
                    self, "Export failed - current files unchanged",
                    f"Texture dependency {canonical} is unresolved; the asset "
                    "family would be incomplete.")
                return False
            texture_exports.append((
                canonical, raw_data, image,
                self._bundle_texture_relative_path(canonical),
                loose_source,
            ))

        forbidden = [
            path.resolve() for path in (base_source,)
            if path is not None and path.exists()]
        for _obj, _model, _relative, _target, source in skeleton_exports:
            if source is not None and source.exists():
                forbidden.append(source.resolve())
        for canonical, _animation, _relative in animation_exports:
            ref_entry = self._matching_ref(
                family.animation_refs, canonical)
            source = (Path(ref_entry.path)
                      if ref_entry is not None and ref_entry.path else None)
            if source is not None and source.exists():
                forbidden.append(source.resolve())
        for _canonical, _raw, _image, _relative, source in texture_exports:
            if source is not None and source.exists():
                forbidden.append(source.resolve())

        skeleton_targets = [entry[3] for entry in skeleton_exports]
        animation_targets = [
            base_target.parent / relative
            for _name, _animation, relative in animation_exports]
        texture_targets = [
            base_target.parent / relative
            for _name, _raw, _image, relative, _source in texture_exports]
        all_targets = [
            *skeleton_targets, base_target,
            *animation_targets, *texture_targets]
        target_keys = [str(path.resolve()).casefold() for path in all_targets]
        if len(target_keys) != len(set(target_keys)):
            QMessageBox.critical(
                self, "Export failed - current files unchanged",
                "Two asset dependencies resolve to the same output path. "
                "Rename the conflicting source resources before exporting.")
            return False
        if any(target.resolve() in forbidden for target in all_targets):
            QMessageBox.warning(
                self, "Choose another output folder",
                "Asset-family export cannot replace a BASE, skeleton, "
                "texture or animation currently open in the editor. Choose "
                "another folder.")
            return False

        existing = [path for path in all_targets if path.exists()]
        if ask_replace and existing:
            answer = QMessageBox.warning(
                self, "Replace exported files?",
                "The following file(s) already exist:\n"
                + "\n".join(str(path) for path in existing)
                + "\n\nReplace them?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return False

        verified_base_bytes: bytes | None = None
        try:
            standalone = self._bundle_base_snapshots.get(owner)
            if standalone is None:
                standalone = export_base_object_bytes(
                    tree.data, fam_obj.base_object)
            uv_edits, texture_edits = self._bundle_base_edits(owner, fam_obj)
            structural_blocks = self._bundle_topology_states(
                standalone, fam_obj)
            with tempfile.TemporaryDirectory(
                    prefix="OpenUAStudio_bundle_") as temp_dir:
                temp_root = Path(temp_dir)
                temp_base = temp_root / "BASE" / base_target.name
                temp_base.parent.mkdir(parents=True, exist_ok=True)
                temp_skeletons = []
                primary_verify = None
                for object_index, (
                        _current_obj, current_model, relative, target,
                        _source) in enumerate(skeleton_exports):
                    temp_skeleton = temp_root / "SKLT" / relative
                    temp_skeleton.parent.mkdir(parents=True, exist_ok=True)
                    verified = self._write_verified_sklt(
                        current_model, temp_skeleton)
                    if object_index == 0:
                        primary_verify = verified
                    temp_skeletons.append((temp_skeleton, target))
                if primary_verify is None:
                    raise SkltParseError(
                        "the selected asset has no verified skeleton")

                notes = save_model_base_copy(
                    standalone, uv_edits, texture_edits, temp_base,
                    structural_blocks=structural_blocks)
                verified_base_bytes = temp_base.read_bytes()
                reloaded = load_asset_family(temp_base)
                reloaded_obj = reloaded.root_object
                reloaded_model = (
                    reloaded_obj.skeleton if reloaded_obj is not None else None)
                if reloaded_model is None:
                    reloaded_base = parse_base_bytes(
                        verified_base_bytes, "<exported-family-base>")
                    if reloaded_base.root is None:
                        raise MappingEditError(
                            "exported BASE failed family reconstruction")
                    reloaded_obj = FamilyObject(
                        base_object=reloaded_base.root,
                        skeleton=primary_verify,
                        owner_path="root")
                    reloaded_model = primary_verify
                if reloaded_model.polygons != model.polygons:
                    raise MappingEditError(
                        "exported BASE/SKLT family failed topology reload")
                roundtrip_issues = [
                    issue for issue in diagnose_polygon_references(reloaded_obj)
                    if "missing typed handler" not in issue]
                if roundtrip_issues:
                    raise MappingEditError(
                        "exported reference graph failed verification: "
                        + "; ".join(roundtrip_issues[:4]))
                if hashlib.sha256(tree.data).digest() \
                        != source_archive_digest:
                    raise MappingEditError(
                        "the read-only source BASE/SET.BAS bytes changed")

                temp_animations = []
                for canonical, animation, relative in animation_exports:
                    data = export_anm_bytes(animation)
                    verified = parse_anm_bytes(data, canonical)
                    if verified.texcoord_groups != animation.texcoord_groups:
                        raise AnmParseError(
                            f"{canonical} failed UV round-trip verification")
                    temp_animation = temp_root / "ANM" / relative
                    temp_animation.parent.mkdir(parents=True, exist_ok=True)
                    temp_animation.write_bytes(data)
                    target = base_target.parent / relative
                    temp_animations.append((temp_animation, target))

                from ilbm_parser import parse_ilbm_bytes
                temp_textures = []
                for canonical, raw_data, image, relative, _source in \
                        texture_exports:
                    temp_texture = temp_root / "ILBM" / relative
                    temp_texture.parent.mkdir(parents=True, exist_ok=True)
                    parsed_raw = (parse_ilbm_bytes(raw_data, canonical)
                                  if raw_data is not None else None)
                    raw_is_loose_vbmp = (
                        parsed_raw is not None
                        and parsed_raw.kind == "VBMP"
                        and relative.suffix.casefold() == ".vbmp")
                    if parsed_raw is not None \
                            and parsed_raw.kind == "ILBM":
                        if not parsed_raw.has_body:
                            raise TextureConvertError(
                                f"{canonical}: ILBM has no decodable BODY")
                        temp_texture.write_bytes(raw_data)
                    elif raw_is_loose_vbmp:
                        if not parsed_raw.has_body:
                            raise TextureConvertError(
                                f"{canonical}: VBMP has no decodable BODY")
                        temp_texture.write_bytes(raw_data)
                    else:
                        export_image = image or parsed_raw
                        if export_image is None or not export_image.has_body:
                            raise TextureConvertError(
                                f"{canonical}: texture has no decodable BODY")
                        palette = (getattr(export_image, "palette", None)
                                   or family.external_palette)
                        if palette is None and self._setbas is not None:
                            palette, _palette_source = self._setbas_palette()
                        # Embedded SET.BAS textures are FORM VBMP. Loose
                        # dependencies expect a standalone FORM ILBM.
                        write_image_as_ilbm(
                            export_image, temp_texture, palette,
                            source=canonical)
                    verified_texture = parse_ilbm_bytes(
                        temp_texture.read_bytes(), canonical)
                    if not verified_texture.has_body:
                        raise TextureConvertError(
                            f"{canonical}: exported texture failed verification")
                    target = base_target.parent / relative
                    temp_textures.append((temp_texture, target))

                notes.extend(_commit_verified_files([
                    *temp_skeletons,
                    (temp_base, base_target),
                    *temp_animations,
                    *temp_textures,
                ]))
        except _BundleCommitError as exc:
            title = (
                "Export failed - current files unchanged"
                if exc.rollback_complete else
                "Export failed - inspect output files")
            QMessageBox.critical(self, title, str(exc))
            return False
        except (AnmParseError, MappingEditError, SkltParseError,
                TextureConvertError, OSError) as exc:
            QMessageBox.critical(
                self, "Export failed - current files unchanged", str(exc))
            return False

        if verified_base_bytes is None:
            QMessageBox.critical(
                self, "Export verification failed",
                "The files were written, but the verified BASE snapshot was "
                "not retained. Reload the exported model before editing again.")
            return False
        self._bundle_base_snapshots[owner] = verified_base_bytes
        self._adopt_saved_block_sources(fam_obj, verified_base_bytes)

        self._geom_dirty.pop(owner, None)
        self._geom_original[owner] = list(model.points)
        if owner in self._topology_original:
            current = self._capture_topology_state(owner)
            if current is not None:
                self._topology_original[owner] = current
        for key in [key for key in self._uv_original if key[0] == owner]:
            del self._uv_original[key]
        for key in [key for key in self._texture_original if key[0] == owner]:
            del self._texture_original[key]
        for key in [key for key in self._vanm_uv_original if key[0] == owner]:
            del self._vanm_uv_original[key]
        self._pending_repairs = []
        self._repair_plan = None
        self._sync_geometry_save_controls()
        self._update_editor_status()
        self._sync_uv_add_vertex_button()
        self._log("Asset family exported: " + "; ".join(notes[-3:]))
        return True

    def _uv_key(self) -> tuple[str, int, int] | None:
        if self._uv_ctx is None:
            return None
        fam_obj, _block, block_index, atts_index, _poly_id = self._uv_ctx
        return fam_obj.owner_path, block_index, atts_index

    @staticmethod
    def _uv_reason_text(exc: Exception) -> str:
        text = str(exc).rstrip(".")
        return text[:1].upper() + text[1:] + "."

    def _uv_topology_reason(
            self, operation: str,
            handle: tuple[tuple[str, int, int], int] | None = None) -> str:
        if not self._editing_allowed():
            return "Enter Edit Mode before changing UV topology."
        if self.viewport.paste_preview_active:
            return "Finish or cancel the active paste preview first."
        if len(self._uv_contexts) != 1 or self.uv_editor.loop_count() != 1:
            return "Select exactly one editable UV loop/polygon first."
        key, context = next(iter(self._uv_contexts.items()))
        if key[0] == "@VANM":
            return (
                "VANM supports coordinate editing only; changing the UV "
                "group length would invalidate its frame layout.")
        fam_obj, block, _block_index, atts_index, poly_id = context
        selected = self.uv_editor.selected_handles()
        if handle is None:
            if len(selected) != 1:
                return "Select exactly one UV handle first."
            handle = next(iter(selected))
        if handle[0] != key or not (0 <= handle[1] < len(
                block.olpl[atts_index]
                if atts_index < len(block.olpl) else [])):
            return "Select exactly one handle in the visible UV loop."
        if not self.uv_editor.loop_editable(key):
            return "The visible UV loop is read-only."
        if (block.class_id or "").lower() != "amesh.class":
            return "Topology editing is available only for amesh OLPL."
        owner = fam_obj.owner_path
        if self._mapping_index is None:
            return "The polygon mapping index is unavailable."
        refs = self._mapping_index.refs.get(poly_id, [])
        if len(refs) != 1 \
                or refs[0].block is not block \
                or refs[0].atts_index != atts_index:
            return "The polygon must have one unambiguous ATTS/OLPL mapping."
        if any(
                element.owner_path == owner and poly_id in element.poly_ids
                for element in self._fx_elements):
            return (
                "Fixed-shape FX polygons do not support vertex topology edits.")
        model = getattr(fam_obj, "skeleton", None)
        if model is None:
            return "The selected model has no editable skeleton."
        try:
            planner = (
                plan_uv_vertex_insertion
                if operation == "add" else plan_uv_vertex_deletion)
            planner(model, block, poly_id, atts_index, handle[1])
        except UVTopologyError as exc:
            return self._uv_reason_text(exc)
        return ""

    def _uv_add_vertex_reason(self) -> str:
        return self._uv_topology_reason("add")

    def _uv_delete_vertex_reason(self) -> str:
        return self._uv_topology_reason("delete")

    def _uv_revert_reason(self) -> str:
        if not self._editing_allowed():
            return "Enter Edit Mode before changing UV coordinates."
        selected = self.uv_editor.selected_handles()
        if not selected:
            return "Select one or more edited UV handles first."
        for key, index in selected:
            tracker, tracked_key = self._uv_original_tracker(key)
            original = tracker.get(tracked_key)
            storage = self._uv_storage(key)
            if original is None or storage is None:
                continue
            current = self._uv_storage_points(storage)
            if index < len(original) and index < len(current) \
                    and tuple(current[index]) != tuple(original[index]):
                return ""
        return "The selected UV handles have no pending changes to revert."

    def _sync_uv_add_vertex_button(self, *_args) -> None:
        add_button = getattr(self, "uv_add_vertex_button", None)
        if add_button is not None:
            reason = self._uv_add_vertex_reason()
            add_button.setEnabled(not reason)
            add_button.setToolTip(
                reason or
                "Insert one midpoint in POO2, POL2 and OLPL after the selected "
                "handle. The operation can be undone.")
        delete_button = getattr(self, "uv_delete_vertex_button", None)
        if delete_button is not None:
            reason = self._uv_delete_vertex_reason()
            delete_button.setEnabled(not reason)
            delete_button.setToolTip(
                reason or
                "Remove the selected POL2 winding and OLPL coordinate while "
                "retaining POO2 indices. The operation can be undone.")
        revert_button = getattr(self, "uv_revert_button", None)
        if revert_button is not None:
            reason = self._uv_revert_reason()
            revert_button.setEnabled(not reason)
            revert_button.setToolTip(
                reason or "Revert only the selected edited UV handles.")
        reset_all = getattr(self, "uv_reset_all_button", None)
        if reset_all is not None:
            owner = self._selected_owner
            changed = bool(
                self._editing_allowed() and owner is not None
                and any(key[0] == owner for key in self._uv_original))
            reset_all.setEnabled(changed)
            reset_all.setToolTip(
                "Restore every changed editable UV loop in the active model."
                if changed else
                "No changed editable UV loop is available to reset.")
        select_all = getattr(self, "uv_select_all_button", None)
        if select_all is not None:
            select_all.setEnabled(self.uv_editor.loop_count() > 0)
        clear = getattr(self, "uv_clear_selection_button", None)
        if clear is not None:
            clear.setEnabled(bool(self.uv_editor.selected_handles()))

    @staticmethod
    def _configure_uv_menu_action(action, reason: str,
                                  description: str) -> None:
        action.setEnabled(not reason)
        action.setToolTip(reason or description)
        action.setStatusTip(reason or description)

    def _show_uv_handle_context_menu(
            self, key, index: int, global_position) -> None:
        if key not in self.uv_editor.loop_keys():
            return
        handle = (key, index)
        menu = QMenu(self.uv_editor)
        add = menu.addAction("Add Vertex After")
        add_reason = self._uv_topology_reason("add", handle)
        self._configure_uv_menu_action(
            add, add_reason,
            "Insert one midpoint in POO2, POL2 and OLPL after the selected "
            "handle.")
        add.triggered.connect(
            lambda _checked=False: self._run_uv_topology_from_handle(
                "add", handle))
        delete = menu.addAction("Delete Vertex")
        delete_reason = self._uv_topology_reason("delete", handle)
        self._configure_uv_menu_action(
            delete, delete_reason,
            "Remove this winding/OLPL vertex without compacting POO2.")
        delete.triggered.connect(
            lambda _checked=False: self._run_uv_topology_from_handle(
                "delete", handle))
        menu.addSeparator()
        select_this = menu.addAction("Select This Vertex")
        select_this.triggered.connect(
            lambda _checked=False: self.uv_editor.select_handle(*handle))
        select_all = menu.addAction("Select All UVs")
        self._configure_uv_menu_action(
            select_all,
            "" if self.uv_editor.loop_count() else "No UV loop is visible.",
            "Select every handle in every visible UV loop.")
        select_all.triggered.connect(self.uv_editor.select_all)
        clear = menu.addAction("Clear Selection")
        self._configure_uv_menu_action(
            clear,
            "" if self.uv_editor.selected_handles()
            else "No UV handle is selected.",
            "Clear the current UV handle selection.")
        clear.triggered.connect(self.uv_editor.select_none)
        menu.addSeparator()
        revert = menu.addAction("Revert Selected UV")
        revert_reason = self._uv_revert_reason()
        self._configure_uv_menu_action(
            revert, revert_reason,
            "Restore the selected handles to their original UV coordinates.")
        revert.triggered.connect(self._revert_selected_uv)
        menu.exec(global_position)

    def _run_uv_topology_from_handle(self, operation: str, handle) -> None:
        self.uv_editor.select_handle(*handle)
        if operation == "add":
            self._add_uv_vertex()
        else:
            self._delete_uv_vertex()

    def _add_uv_vertex(self) -> None:
        reason = self._uv_add_vertex_reason()
        if reason:
            self._notify(f"Add Vertex refused: {reason}", 8000)
            return
        self._apply_uv_topology_edit("add")

    def _delete_uv_vertex(self) -> None:
        reason = self._uv_delete_vertex_reason()
        if reason:
            self._notify(f"Delete Vertex refused: {reason}", 8000)
            return
        self._apply_uv_topology_edit("delete")

    def _apply_uv_topology_edit(self, operation: str) -> None:
        key, context = next(iter(self._uv_contexts.items()))
        fam_obj, block, _block_index, atts_index, poly_id = context
        owner = fam_obj.owner_path
        model = fam_obj.skeleton
        selected = next(iter(self.uv_editor.selected_handles()))[1]
        label = "Add Vertex" if operation == "add" else "Delete Vertex"
        before = self._capture_topology_state(owner)
        if before is None:
            self._notify(
                f"{label} refused: topology snapshot unavailable.", 8000)
            return
        had_topology_original = owner in self._topology_original
        previous_geom_original = copy.deepcopy(
            self._geom_original.get(owner))
        previous_dirty = self._geom_dirty.get(owner)
        previous_undo = list(self._edit_undo_stack)
        previous_redo = list(self._edit_redo_stack)
        try:
            if operation == "add":
                plan = plan_uv_vertex_insertion(
                    model, block, poly_id, atts_index, selected)
            else:
                plan = plan_uv_vertex_deletion(
                    model, block, poly_id, atts_index, selected)
            self._topology_original.setdefault(owner, copy.deepcopy(before))
            self._remember_geometry_original(owner)
            if operation == "add":
                apply_uv_vertex_insertion(model, block, plan)
                selected_handle = plan.insert_at
                selected_vertex = plan.new_point_index
            else:
                apply_uv_vertex_deletion(model, block, plan)
                selected_handle = min(
                    plan.vertex_index, len(model.polygons[poly_id]) - 1)
                selected_vertex = model.polygons[poly_id][selected_handle]
            self._uv_history_before = None
            self._refresh_object_material_faces(fam_obj)
            self._selected_owner = owner
            self._selected_polys = {poly_id}
            self._selected_poly = poly_id
            self.viewport.refresh_family_materials()
            self._rebuild_workbench(self._family, owner)
            self._refresh_fx_elements()
            self.viewport.set_selected_owner(owner)
            self.viewport.set_selected_polygon(poly_id)
            self.viewport.set_highlight_polys({poly_id})
            if self.edit_toggle_action.isChecked():
                self.viewport.enter_edit_mode_with_vertices(
                    owner, [selected_vertex], pick_polygons=True)
                self.viewport.configure_edit_interaction(
                    selected_only=False, pick_polygons=True)
                session = self.viewport.edit_session
                if session is not None:
                    session._undo.clear()
                    session._redo.clear()
            self._on_geometry_edited(owner)
            after = self._capture_topology_state(owner)
            if after is None:
                raise UVTopologyError(
                    "the committed topology could not be verified")
            self._record_edit_command({
                "kind": "topology",
                "owner": owner,
                "before": before,
                "after": after,
                "label": label,
            })
            self._fill_polygon_inspector(poly_id)
            self.uv_editor.select_handle(key, selected_handle)
            self._sync_edit_action_states()
            if operation == "add":
                message = (
                    f"Added POO2 #{plan.new_point_index} and connected it "
                    f"between winding vertices {plan.edge_start} and "
                    f"{plan.insert_at}. Undo is available.")
            else:
                message = (
                    f"Removed winding/OLPL vertex {plan.vertex_index}; "
                    f"POO2 #{plan.point_index} was retained to preserve shared "
                    "indices. Undo is available.")
            self._notify(message, 8000)
        except Exception as exc:
            try:
                rolled_back = self._restore_topology_state(owner, before)
            except Exception as rollback_exc:
                rolled_back = (
                    self._topology_content(
                        self._capture_topology_state(owner))
                    == self._topology_content(before))
                self._log(
                    f"{label} rollback UI refresh failed after data restore: "
                    f"{rollback_exc}")
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
            self._uv_history_before = None
            try:
                self._refresh_object_material_faces(fam_obj)
                self.viewport.refresh_family_materials()
                self._rebuild_workbench(self._family, owner)
                self._refresh_fx_elements()
                self.viewport.set_selected_owner(owner)
                self.viewport.set_selected_polygon(poly_id)
                self.viewport.set_highlight_polys({poly_id})
                self._fill_polygon_inspector(poly_id)
                self._sync_editor_context()
            except Exception as refresh_exc:
                self._log(
                    f"{label} rollback UI refresh failed: {refresh_exc}")
            self._notify(
                f"{label} failed and "
                f"{'was rolled back' if rolled_back else 'rollback failed'}: "
                f"{exc}.", 8000)
        finally:
            self._sync_uv_add_vertex_button()

    def _update_uv_editor(self, poly_id: int | None) -> None:
        self._uv_ctx = None
        self._uv_contexts = {}
        if poly_id is None or self._workbench_obj is None \
                or self._mapping_index is None:
            self.uv_editor.set_loops(
                None, [],
                "Select a mapped polygon to edit its texture coordinates.")
            self._sync_uv_add_vertex_button()
            return

        element = self._fx_element_for_polygon(poly_id)
        if element is not None:
            self._update_fx_uv_editor(element)
            return

        requested = self._resolved_geometry_polys()
        if not requested:
            requested = set(self._selected_polys)
        if not requested:
            requested = {poly_id}
        ordered = [poly_id] + sorted(requested - {poly_id})
        loops = []
        skipped: list[tuple[int, str]] = []
        image = None
        message = ""
        model = self._workbench_obj.skeleton
        for selected_poly in ordered:
            if not (0 <= selected_poly < len(model.polygons)):
                skipped.append((selected_poly, "invalid polygon"))
                continue
            refs = self._mapping_index.refs.get(selected_poly, [])
            if len(refs) != 1:
                skipped.append((
                    selected_poly,
                    "unmapped" if not refs else "duplicate mapping"))
                continue
            ref = refs[0]
            block = ref.block
            texture = block.texture
            if texture is not None and texture.kind == "bmpanim":
                if len(requested) != 1:
                    skipped.append((selected_poly, "animated/VANM"))
                    continue
                frame = self.viewport.current_animation_frame_data(
                    self._workbench_obj.owner_path, ref.block_index)
                animation_group = self.viewport.current_animation_group(
                    self._workbench_obj.owner_path, ref.block_index)
                source_uvs = []
                if frame is not None:
                    source_uvs = list(
                        frame[1][:len(model.polygons[selected_poly])])
                    image = frame[0]
                    message = (
                        f"UV source: VANM current {frame[2]}. Coordinate edits "
                        "are exported as a verified ANM file by Export Asset "
                        "Family.")
                elif ref.atts_index < len(block.olpl):
                    source_uvs = list(block.olpl[ref.atts_index])
                    message = "The current VANM frame group is unavailable."
                if source_uvs:
                    key = (
                        ("@VANM", animation_group[0], animation_group[1])
                        if animation_group is not None else
                        (self._workbench_obj.owner_path,
                         ref.block_index, ref.atts_index))
                    editable = bool(
                        animation_group is not None
                        and self._animation_uv_writable(
                            animation_group[0], animation_group[1]))
                    if editable:
                        self._uv_contexts[key] = (
                            self._workbench_obj, None,
                            animation_group[0], animation_group[1],
                            selected_poly)
                    loops.append(UVLoop(
                        key, selected_poly, source_uvs, editable))
                else:
                    skipped.append((selected_poly, "VANM frame unavailable"))
                continue
            block_class = (block.class_id or "").lower()
            if block_class == "area.class":
                # Sky panels and other area.class faces use the texture OTL2
                # outline directly. It is read-only, but belongs in the UV
                # preview and must use the same lazy texture loader as amesh.
                if ref.atts_index >= len(block.olpl):
                    skipped.append((selected_poly, "missing OTL2 outline"))
                    continue
                uvs = list(block.olpl[ref.atts_index])
                if not uvs or len(uvs) != len(model.polygons[selected_poly]):
                    skipped.append((
                        selected_poly, "POL2/OTL2 count mismatch"))
                    continue
                key = (self._workbench_obj.owner_path,
                       ref.block_index, ref.atts_index)
                loops.append(UVLoop(key, selected_poly, uvs, False))
                if image is None:
                    texture_name = texture.name if texture else ""
                    if texture_name:
                        loaded_name = self._ensure_model_texture_loaded(
                            texture_name, show_error=False)
                        image = self._texture_qimage(
                            loaded_name or texture_name)
                message = "area.class OTL2 preview (read-only)."
                continue
            if block_class != "amesh.class":
                skipped.append((selected_poly, "non-amesh mapping"))
                continue
            if ref.atts_index >= len(block.olpl):
                skipped.append((selected_poly, "missing OLPL"))
                continue
            uvs = list(block.olpl[ref.atts_index])
            if not uvs or len(uvs) != len(model.polygons[selected_poly]):
                skipped.append((selected_poly, "POL2/OLPL count mismatch"))
                continue
            key = (
                self._workbench_obj.owner_path,
                ref.block_index, ref.atts_index)
            context = (
                self._workbench_obj, block, ref.block_index,
                ref.atts_index, selected_poly)
            self._uv_contexts[key] = context
            loops.append(UVLoop(
                key, selected_poly, uvs, self._editing_allowed()))
            if image is None:
                texture_name = (
                    texture.name if texture else "")
                if texture_name:
                    # Archive-only textures are decoded lazily when selected
                    # in the resource list.  Polygon selection reaches the
                    # UV editor directly, so request the same lazy load here
                    # or the 3D viewport can be textured while UV stays on
                    # its checkerboard.
                    loaded_name = self._ensure_model_texture_loaded(
                        texture_name, show_error=False)
                    image = self._texture_qimage(
                        loaded_name or texture_name)

        if loops:
            first_key = next(
                (loop.key for loop in loops
                 if loop.key in self._uv_contexts), None)
            if first_key is not None:
                self._uv_ctx = self._uv_contexts[first_key]
        category_counts = {}
        for _skipped_poly, category in skipped:
            category_counts[category] = category_counts.get(category, 0) + 1
        if len(requested) > 1:
            details = ", ".join(
                f"{count} {category}"
                for category, count in sorted(category_counts.items()))
            message = (
                f"{len(loops)} compatible UV loop(s) shown; "
                f"{len(skipped)} incompatible polygon(s) skipped"
                + (f" ({details})." if details else "."))
            self.statusBar().showMessage(message, 7000)
        elif not loops:
            if skipped and skipped[0][1] == "unmapped":
                message = (
                    "This polygon has no UV mapping. Open Tools > "
                    "Mapping Repair... first.")
            else:
                reason = skipped[0][1] if skipped else "UV data unavailable"
                message = f"This polygon is not editable: {reason}."
        self.uv_editor.set_loops(image, loops, message)
        self._sync_uv_add_vertex_button()

    def _on_uv_changed(self, loop_uvs: dict) -> None:
        if not self._editing_allowed():
            self._update_uv_editor(self._selected_poly)
            return
        if not self._uv_contexts:
            return
        changed = {}
        before = {}
        for key, uvs in loop_uvs.items():
            if key not in self._uv_contexts:
                continue
            storage = self._uv_storage(key)
            if storage is None:
                return
            current = self._uv_storage_points(storage)
            updated = [tuple(uv) for uv in uvs]
            if current != updated:
                before[key] = current
                changed[key] = updated
        if not changed:
            return
        history_before = copy.deepcopy(self._uv_history_before)
        if self._uv_history_before is None:
            self._uv_history_before = {}
        for key, current in before.items():
            self._uv_history_before.setdefault(key, current)
        if not self._apply_uv_histories(changed, refresh=False):
            self._uv_history_before = history_before
            self._notify(
                "UV edit failed; all selected loops were left unchanged.",
                7000)
            self._update_uv_editor(self._selected_poly)
            return
        self._sync_geometry_save_controls()
        self._update_editor_status()
        self._sync_uv_add_vertex_button()

    def _on_uv_edit_finished(self) -> None:
        if not self._editing_allowed():
            return
        if not self._uv_contexts or self._family is None:
            return
        history = self._uv_history_before or {}
        self._uv_history_before = None
        before_snapshot = {}
        after_snapshot = {}
        for key, before in history.items():
            storage = self._uv_storage(key)
            if storage is None:
                continue
            after = self._uv_storage_points(storage)
            if list(before) != after:
                before_snapshot[key] = list(before)
                after_snapshot[key] = after
        if before_snapshot:
            self._record_edit_command({
                "kind": "uv_multi",
                "before": before_snapshot,
                "after": after_snapshot,
                "label": (
                    "UV multi-loop edit"
                    if len(before_snapshot) > 1 else "UV edit"),
            })
        poly_id = self._uv_ctx[4] if self._uv_ctx else self._selected_poly
        self.viewport.refresh_family_materials()
        self.viewport.set_selected_polygon(poly_id)
        self._sync_animation_controls()
        self._sync_editor_context()
        self._sync_uv_add_vertex_button()

    def _restore_uv(self, key: tuple[str, int, int],
                    uvs: list[tuple[int, int]]) -> None:
        storage = self._uv_storage(key)
        if storage is None:
            return
        updated = [tuple(uv) for uv in uvs]
        kind, fam_obj_or_name, container, atts_index = storage
        if kind == "vanm":
            container.texcoord_groups[atts_index] = updated
            return
        fam_obj = fam_obj_or_name
        block = container
        block_index = int(key[1])
        block.olpl[atts_index] = updated
        entry = block.atts[atts_index] if atts_index < len(block.atts) else None
        if block_index < len(fam_obj.materials) and entry is not None:
            group = fam_obj.materials[block_index]
            for index, (pid, _old_uvs, shade) in enumerate(group.faces):
                if pid == entry.poly_id:
                    group.faces[index] = (pid, updated, shade)
                    break

    def _revert_selected_uv(self) -> None:
        reason = self._uv_revert_reason()
        if reason:
            self._notify(f"UV revert refused: {reason}", 6000)
            return
        selected = self.uv_editor.selected_handles()
        targets = {}
        before = {}
        reverted_count = 0
        for key, index in selected:
            tracker, tracked_key = self._uv_original_tracker(key)
            original = tracker.get(tracked_key)
            storage = self._uv_storage(key)
            if original is None or storage is None:
                continue
            current = self._uv_storage_points(storage)
            if not (index < len(original) and index < len(current)) \
                    or current[index] == tuple(original[index]):
                continue
            before.setdefault(key, current)
            target = targets.setdefault(key, list(current))
            target[index] = tuple(original[index])
            reverted_count += 1
        if not targets or not self._apply_uv_histories(targets):
            self._notify(
                "UV revert failed; all selected loops were left unchanged.",
                7000)
            return
        self._record_edit_command({
            "kind": "uv_multi",
            "before": before,
            "after": targets,
            "label": "UV revert",
        })
        poly_id = self._uv_ctx[4] if self._uv_ctx else None
        self._update_uv_editor(poly_id)
        self.uv_editor.select_handles(selected)
        self._sync_geometry_save_controls()
        self._update_editor_status()
        self._sync_uv_add_vertex_button()
        self.statusBar().showMessage(
            f"Reverted {reverted_count} selected UV handle(s).", 4000)

    def _reset_all_uvs(self) -> None:
        if not self._require_editing("Reset All UVs"):
            return
        owner = self._selected_owner
        targets = {
            key: list(original)
            for key, original in self._uv_original.items()
            if key[0] == owner and self._uv_storage(key) is not None
        }
        targets.update({
            ("@VANM", name, group_index): list(original)
            for (name, group_index), original
            in self._vanm_uv_original.items()
            if (name, group_index) in self._owner_vanm_uv_keys(owner)
            and self._uv_storage(
                ("@VANM", name, group_index)) is not None
        })
        if not targets:
            return
        before = {}
        poly_ids = set()
        for key in targets:
            storage = self._uv_storage(key)
            before[key] = self._uv_storage_points(storage)
            if storage[0] == "olpl":
                _kind, _fam_obj, block, atts_index = storage
                if atts_index < len(block.atts):
                    poly_ids.add(block.atts[atts_index].poly_id)
        if len(targets) > 1 or len(poly_ids) > 1:
            answer = QMessageBox.warning(
                self, "Reset all UVs?",
                f"Restore {len(targets)} changed UV loops across "
                f"{len(poly_ids)} polygon(s)?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        if not self._apply_uv_histories(targets):
            self._notify(
                "Reset All UVs failed; all loops were left unchanged.", 7000)
            return
        self._record_edit_command({
            "kind": "uv_multi",
            "before": before,
            "after": targets,
            "label": "reset all UVs",
        })
        self._update_uv_editor(self._selected_poly)
        self._sync_geometry_save_controls()
        self._update_editor_status()
        self._sync_uv_add_vertex_button()
        self._notify(
            f"Reset {len(targets)} UV loop(s) to their loaded positions.",
            5000)

    def _reset_model(self) -> None:
        if not self._require_editing("Reset Model"):
            return
        owner = self._selected_owner
        has_geometry = owner in self._geom_dirty
        has_texture = any(key[0] == owner for key in self._texture_original)
        has_uv = (
            any(key[0] == owner for key in self._uv_original)
            or bool(self._owner_vanm_uv_keys(owner)))
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
        vanm_uv_keys = self._owner_vanm_uv_keys(owner)
        before_state = {
            "topology": self._capture_topology_state(owner)
            if owner in self._topology_original else None,
            "geometry": (
                list(model.points)
                if model is not None and owner not in self._topology_original
                else None),
            "textures": {
                key: getattr(
                    self._owner_to_obj[key[0]].base_object.ades[key[1]],
                    key[2]).name
                for key in texture_keys
            },
            "uvs": {
                key: list(self._owner_to_obj[key[0]].base_object
                          .ades[key[1]].olpl[key[2]])
                for key in uv_keys
            },
            "vanm_uvs": {
                key: self._uv_storage_points(
                    self._uv_storage(("@VANM", key[0], key[1])))
                for key in vanm_uv_keys
            },
        }
        resume_edit = self.edit_toggle_action.isChecked()
        if self.viewport.is_edit_mode:
            self.viewport.exit_edit_mode()
        original = self._geom_original.get(owner)
        topology_original = self._topology_original.get(owner)
        if topology_original is not None:
            self._restore_topology_state(owner, topology_original)
        elif model is not None and original is not None:
            model.points[:] = list(original)
        texture_restore = {
            key: original
            for key, original in self._texture_original.items()
            if key[0] == owner
        }
        if texture_restore:
            self._apply_texture_history(texture_restore)
        for key, uvs in list(self._uv_original.items()):
            if key[0] != owner:
                continue
            self._restore_uv(key, uvs)
            del self._uv_original[key]
        for key in list(vanm_uv_keys):
            self._restore_uv(
                ("@VANM", key[0], key[1]),
                self._vanm_uv_original[key])
            del self._vanm_uv_original[key]
        after_state = {
            "topology": self._capture_topology_state(owner)
            if topology_original is not None else None,
            "geometry": (
                list(model.points)
                if model is not None and topology_original is None
                else None),
            "textures": {
                key: getattr(
                    self._owner_to_obj[key[0]].base_object.ades[key[1]],
                    key[2]).name
                for key in texture_keys
            },
            "uvs": {
                key: list(self._owner_to_obj[key[0]].base_object
                          .ades[key[1]].olpl[key[2]])
                for key in uv_keys
            },
            "vanm_uvs": {
                key: self._uv_storage_points(
                    self._uv_storage(("@VANM", key[0], key[1])))
                for key in vanm_uv_keys
            },
        }
        self._geom_dirty.pop(owner, None)
        if not texture_restore:
            self.viewport.refresh_family_materials()
        self._fill_polygon_inspector(self._selected_poly)
        self._sync_geometry_save_controls()
        self._update_editor_status()
        if resume_edit:
            self.edit_toggle_action.setChecked(True)
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
        archive = family.setbas_archive or self._setbas
        loaded = {str(name).casefold() for name in family.textures}
        return [
            entry.name for entry in build_texture_catalog(family, archive)
            if (
                entry.archive_resource is not None and entry.decodable
            ) or entry.name.casefold() in loaded
        ]

    def _available_model_animations(self) -> list[str]:
        family = self._family
        if family is None:
            return []
        return sorted(
            family.animations,
            key=lambda value: value.casefold())

    def _visual_texture_available(self, name: str) -> bool:
        family = self._family
        if family is None:
            return False
        if any(key.casefold() == str(name).casefold()
               for key in family.textures):
            return True
        archive = family.setbas_archive or self._setbas
        return bool(
            archive is not None
            and any(resource.decodable
                    for resource in archive.find(name, "ilbm.class")))

    def _ensure_model_texture_loaded(
            self, name: str, *, show_error: bool = True) -> str | None:
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
        cache_key = (id(archive), str(name).casefold())
        if cache_key in self._texture_preview_cache:
            return self._texture_preview_cache[cache_key]
        resources = archive.find(name, "ilbm.class")
        resource = next((item for item in resources if item.decodable), None)
        if resource is None:
            self._texture_preview_cache[cache_key] = None
            return None
        try:
            image = decode_texture(archive, resource)
        except Exception as exc:
            self._texture_preview_cache[cache_key] = None
            if show_error:
                QMessageBox.warning(self, "Texture load failed", str(exc))
            return None
        family.textures[resource.resource_name] = image
        self._texture_preview_cache[cache_key] = resource.resource_name
        return resource.resource_name

    def _load_visual_texture_item(self, item) -> None:
        if item is None or self._family is None:
            return
        name = item.data(Qt.ItemDataRole.UserRole)
        if not name:
            return
        loaded_name = self._ensure_model_texture_loaded(
            name, show_error=False)
        if loaded_name is None:
            return
        qimage = self._texture_qimage(loaded_name)
        if qimage is not None and not qimage.isNull():
            item.setIcon(_checker_thumbnail(qimage))
            item.setToolTip(
                f"{name}\nDecoded lazily from the active texture sources.")

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

    def _binding_thumbnail_for_picker(
            self, name: str, binding_kind: str) -> QImage | None:
        if binding_kind != "bmpanim":
            return self._texture_thumbnail_for_picker(name)
        family = self._family
        if family is None:
            return None
        animation = next((
            value for key, value in family.animations.items()
            if key.casefold() == name.casefold()
        ), None)
        if animation is None:
            return None
        for bitmap_name in animation.bitmap_names:
            image = self._texture_thumbnail_for_picker(bitmap_name)
            if image is not None and not image.isNull():
                return image
        return None

    def _choose_model_texture(self, names: list[str],
                              current: str,
                              binding_kind: str = "ilbm") -> str | None:
        dialog = TexturePickerDialog(
            names, current,
            lambda name: self._binding_thumbnail_for_picker(
                name, binding_kind),
            self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self._notify("Texture selection cancelled.", 3000)
            return None
        return dialog.selected_name()

    def _choose_texture_binding_group(self, groups):
        if len(groups) == 1:
            return groups[0]
        labels = [
            f"{len(group.bindings)} block(s) {group.binding_slot}: "
            f"{group.binding_name} "
            f"({group.binding_kind}, {len(group.affected_polys)} polygons)"
            for group in groups
        ]
        selected, accepted = QInputDialog.getItem(
            self, "Choose texture binding",
            "Replace which logical material binding?",
            labels, 0, False)
        if not accepted:
            return None
        return groups[labels.index(selected)]

    def _replace_selected_texture_structurally(
            self, owner: str, group, bindings,
            loaded_name: str) -> bool:
        """Split shared AMESH mappings so only selected POL2s are retargeted."""

        fam_obj = self._owner_to_obj.get(owner)
        if fam_obj is None:
            return False
        reason = self._geometry_writer_reason(owner, fam_obj)
        if reason:
            self._notify(f"Load Texture refused: {reason}.", 8000)
            return False
        before = self._capture_topology_state(owner)
        if before is None:
            return False
        had_topology_original = owner in self._topology_original
        had_geom_original = owner in self._geom_original
        previous_geom_original = copy.deepcopy(
            self._geom_original.get(owner))
        had_dirty = owner in self._geom_dirty
        previous_dirty = self._geom_dirty.get(owner)
        previous_undo = list(self._edit_undo_stack)
        previous_redo = list(self._edit_redo_stack)
        try:
            self._topology_original.setdefault(
                owner, copy.deepcopy(before))
            blocks = fam_obj.base_object.ades
            split_count = 0
            for binding in bindings:
                if not (0 <= binding.block_index < len(blocks)):
                    raise MappingEditError(
                        "the selected material block no longer exists")
                block = blocks[binding.block_index]
                selected = set(binding.selected_polys)
                selected_positions = [
                    index for index, entry in enumerate(block.atts)
                    if entry.poly_id in selected
                ]
                if not selected_positions:
                    raise MappingEditError(
                        "the selected segment is no longer in its material")
                new_template = rewrite_block_texture_template(
                    block, loaded_name, binding.binding_slot)
                if len(selected_positions) == len(block.atts):
                    getattr(block, binding.binding_slot).name = loaded_name
                    block.source_objt_bytes = new_template
                    continue
                if (block.class_id or "").casefold() != "amesh.class":
                    raise MappingEditError(
                        "only AMESH material blocks can be split by segment")
                atts_only = bool(
                    block.texture is not None
                    and block.texture.kind == "bmpanim"
                    and not block.olpl)
                if not atts_only and len(block.atts) != len(block.olpl):
                    raise MappingEditError(
                        "the shared material has ambiguous ATTS/OLPL counts")
                selected_set = set(selected_positions)
                clone = copy.deepcopy(block)
                clone.atts = [
                    copy.deepcopy(block.atts[index])
                    for index in selected_positions]
                clone.olpl = (
                    [] if atts_only else [
                        copy.deepcopy(block.olpl[index])
                        for index in selected_positions])
                block.atts = [
                    entry for index, entry in enumerate(block.atts)
                    if index not in selected_set]
                if not atts_only:
                    block.olpl = [
                        uvs for index, uvs in enumerate(block.olpl)
                        if index not in selected_set]
                getattr(clone, binding.binding_slot).name = loaded_name
                clone.source_objt_bytes = new_template
                blocks.append(clone)
                split_count += 1

            self._refresh_object_material_faces(fam_obj)
            self._selected_owner = owner
            self._selected_polys = set(group.selected_polys)
            self._selected_poly = min(
                self._selected_polys,
                default=self._selected_poly)
            self.viewport.refresh_family_materials()
            self._rebuild_workbench(self._family, owner)
            self._refresh_fx_elements()
            self.viewport.set_selected_owner(owner)
            self.viewport.set_selected_polygon(self._selected_poly)
            self.viewport.set_highlight_polys(self._selected_polys)
            self._on_geometry_edited(owner)
            after = self._capture_topology_state(owner)
            if after is None:
                raise MappingEditError(
                    "the segment-scoped texture edit could not be verified")
            self._record_edit_command({
                "kind": "topology",
                "owner": owner,
                "before": before,
                "after": after,
                "label": "segment texture assignment",
            })
            self._fill_polygon_inspector(self._selected_poly)
            self._sync_geometry_save_controls()
            self._update_editor_status()
            self._sync_editor_context()
            message = (
                f"{len(group.selected_polys)} selected polygon(s) changed"
                + (f"; {split_count} shared material block(s) isolated."
                   if split_count else "."))
            self.statusBar().showMessage(message, 9000)
            self._log(f"Load Texture: {message}")
            return True
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
            self._notify(
                f"Load Texture failed and was rolled back: {exc}.", 9000)
            return False

    def _load_model_texture(self) -> None:
        if not self._require_editing("Load Texture"):
            return
        if self._mapping_index is None or self._workbench_obj is None:
            return
        requested = self._resolved_geometry_polys()
        if not requested:
            requested = {
                poly_id for poly_id in self._selected_polys
                if 0 <= poly_id < self._mapping_index.poly_count
            }
        if not requested and self._selected_poly is not None:
            requested = {self._selected_poly}
        selected_classification = classify_texture_assignment(
            self._mapping_index, requested)
        if selected_classification.skipped:
            reason_labels = {
                "unmapped": (
                    "has no ATTS material mapping in the BASE file"),
                "duplicate-mapped": (
                    "has more than one material mapping"),
                "invalid": "is not a valid polygon",
                "material has no texture": (
                    "uses a color-only material with no texture binding"),
                "material has no valid polygons": (
                    "belongs to an invalid material block"),
                "material overlaps duplicate mapping": (
                    "belongs to an ambiguous shared material"),
            }
            details = [
                f"Polygon #{poly_id}: "
                f"{reason_labels.get(reason, reason)}."
                for poly_id, reason in selected_classification.skipped[:12]
            ]
            remaining = len(selected_classification.skipped) - len(details)
            if remaining:
                details.append(f"...and {remaining} more polygon(s).")
            if selected_classification.groups:
                summary = (
                    "Some selected segments cannot have their texture "
                    "replaced and will be left unchanged.")
            else:
                summary = (
                    "The selected segment(s) cannot have their texture "
                    "replaced. The engine data contains no compatible texture "
                    "binding to change.")
            QMessageBox.warning(
                self, "Texture cannot be replaced",
                summary + "\n\n" + "\n".join(details)
                + "\n\nNo unsupported material data was modified.")
        if not selected_classification.groups:
            return
        group = self._choose_texture_binding_group(
            list(selected_classification.groups))
        if group is None:
            self._notify("Texture binding selection cancelled.", 3000)
            return
        names = (
            self._available_model_animations()
            if group.binding_kind == "bmpanim"
            else self._available_model_textures())
        if not names:
            QMessageBox.information(
                self, "No textures available",
                "No compatible decoded texture or animation binding is "
                "available in this family or SET.BAS.")
            return
        owner = self._workbench_obj.owner_path
        current = group.binding_name
        chosen = self._choose_model_texture(
            names, current, group.binding_kind)
        if not chosen:
            return
        if group.binding_kind == "bmpanim":
            loaded_name = next((
                name for name in self._family.animations
                if name.casefold() == chosen.casefold()
            ), None)
        else:
            loaded_name = self._ensure_model_texture_loaded(chosen)
        if loaded_name is None:
            QMessageBox.warning(
                self, "Texture load failed",
                f"{chosen} could not be decoded from the loaded sources.")
            return
        bindings = {
            (binding.block_index, binding.binding_slot): binding
            for binding in group.bindings
        }
        affected_polys = set(group.affected_polys)
        before = {
            (owner, binding.block_index, binding.binding_slot):
            getattr(binding.block, binding.binding_slot).name
            for binding in bindings.values()
        }
        changes = {
            key: loaded_name for key, old_name in before.items()
            if old_name.casefold() != loaded_name.casefold()
        }
        if not changes:
            self._notify(
                "Every selected compatible section already uses that resource.",
                         3500)
            return
        changed_bindings = [
            binding for binding in bindings.values()
            if (owner, binding.block_index, binding.binding_slot) in changes
        ]
        structural_needed = any(
            len(binding.selected_polys)
            < len(getattr(binding.block, "atts", ()))
            for binding in changed_bindings)
        if structural_needed:
            self._replace_selected_texture_structurally(
                owner, group, changed_bindings, loaded_name)
            return
        if not self._apply_texture_history(changes):
            self._notify(
                "Texture replacement failed; the binding was left unchanged.",
                7000)
            return
        after = {
            key: getattr(
                self._owner_to_obj[key[0]].base_object.ades[key[1]],
                key[2]).name
            for key in before
        }
        self._selected_polys = set(group.selected_polys)
        self.viewport.set_highlight_polys(self._selected_polys)
        self._record_edit_command({
            "kind": "texture",
            "before": before,
            "after": after,
            "label": "texture binding assignment",
        })
        self._fill_polygon_inspector(self._selected_poly)
        self._sync_geometry_save_controls()
        self._update_editor_status()
        self._sync_editor_context()
        changed_blocks = len(changes)
        changed_polygons = len(affected_polys)
        skipped = len(selected_classification.skipped)
        message = (
            f"{changed_blocks} material block(s) changed, "
            f"{changed_polygons} polygon(s) affected, "
            f"{skipped} incompatible polygon(s) skipped.")
        self.statusBar().showMessage(message, 9000)
        self._log(f"Load Texture: {message}")

    # -- repair -----------------------------------------------------------------

    def _update_repair_buttons(self) -> None:
        editing = (
            self._editing_allowed()
            and not self.viewport.paste_preview_active)
        can_plan = (
            editing
            and self._selected_poly is not None
            and self._mapping_index is not None
            and self._mapping_index.status(self._selected_poly) == "unmapped"
            and self._family is not None
            and self._family.base_path is not None
            and self.repair_target_combo.count() > 0
        )
        self.repair_copy_button.setEnabled(can_plan)
        self.repair_planar_button.setEnabled(can_plan)
        self.repair_apply_button.setEnabled(
            editing and self._repair_plan is not None)
        self.repair_revert_button.setEnabled(
            editing and bool(self._pending_repairs))
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
        if not self._require_editing("Apply Mapping Repair"):
            return
        if self.viewport.paste_preview_active:
            self._notify(
                "Finish or cancel Paste Geometry before applying a mapping "
                "repair.", 6000)
            return
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
            "Use Export Asset Family to keep it."
        )

    def _revert_repairs(self) -> None:
        if not self._require_editing("Revert Mapping Repairs"):
            return
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
            self, "Export mapping as",
            str(suggested),
            "Urban Assault BASE (*.base *.bas);;All files (*)",
        )
        if not path:
            return
        try:
            notes = save_repaired_base(self._family, self._workbench_obj,
                                       self._pending_repairs, path)
        except MappingEditError as exc:
            QMessageBox.critical(self, "Export failed - nothing written",
                                 str(exc))
            return
        self._saved_repair_path = path
        self._pending_repairs = []
        self._log("; ".join(notes))
        self._notify(f"Mapping exported: {Path(path).name}.", 8000)
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
        self._texture_preview_cache.clear()
        self._texture_qimage_cache.clear()
        if family is not self._family:
            self._bundle_targets.clear()
            self._bundle_base_snapshots.clear()
            self._geometry_writer_capability_cache.clear()
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
        self._topology_original = {}
        self._geometry_clipboard = None
        self._uv_ctx = None
        self._uv_contexts = {}
        self._uv_original = {}
        self._vanm_uv_original = {}
        self._texture_original = {}
        self._tab_edit_selection = {}
        self._fx_preview_cache.clear()
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

        self._update_editor_status()
        # Reapply the active main-tab contract after the viewport rebuild.
        self._sync_tab_edit_mode()
        self._update_reset_camera_action()
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
        self._asset_filter_expansion = None
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
        if self.tree_search.text().strip():
            self._filter_asset_tree(self.tree_search.text())

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
                self._open_visual_texture(
                    payload, show_preview=True, switch_tabs=False)
                return
            if kind == "animation":
                self._play_asset_animation(item)
                return
        owner = self._tree_owner_for_item(item)
        if owner:
            self._select_owner(owner)

    def _filter_asset_tree(self, text: str) -> None:
        self._asset_filter_expansion = filter_asset_tree(
            self.asset_tree, text, self._asset_item_search_metadata,
            self._asset_filter_expansion)

    def _asset_item_search_metadata(self, item) -> str:
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data:
            return ""
        kind, payload = data
        owner = self._tree_owner_for_item(item) or ""
        class_id = {
            "base": "base.class",
            "texture": "ilbm.class",
            "animation": "bmpanim.class VANM",
        }.get(kind, "")
        if kind in ("skeleton", "child") and payload is not None:
            base_object = getattr(payload, "base_object", None)
            skeleton_class = (
                getattr(base_object, "skeleton_class", "") or "sklt.class")
            class_id = (
                f"base.class {skeleton_class}"
                if kind == "child" else skeleton_class)
        return f"{kind} {class_id} {owner}"

    def _open_visual_texture(
            self, name: str, *, show_preview: bool = False,
            switch_tabs: bool = True) -> None:
        """Resolve a texture and optionally show it without forced tab jumps."""

        if not name or not hasattr(self, "_visuals_tabs"):
            return
        if switch_tabs:
            self._right_tabs.setCurrentWidget(self._visuals_tabs)
            self._visuals_tabs.setCurrentIndex(0)
        match = None
        for row in range(self.texture_list.count()):
            item = self.texture_list.item(row)
            value = item.data(Qt.ItemDataRole.UserRole)
            if value and str(value).casefold() == str(name).casefold():
                match = item
                break
        if match is not None:
            self.texture_list.setCurrentItem(match)
            self.texture_list.scrollToItem(
                match, QAbstractItemView.ScrollHint.PositionAtCenter)
            self._load_visual_texture_item(match)
        if show_preview:
            self._preview_family_texture(name)

    def _preview_visual_texture_item(self, item) -> None:
        if item is not None:
            self._preview_family_texture(
                item.data(Qt.ItemDataRole.UserRole))

    def _on_polygon_deselected(self, poly_id: int) -> None:
        mirror_active = self._mirror_select_active()
        if mirror_active:
            self._mirror_select_source_polys.difference_update(
                self._mirrored_polygon_selection({poly_id}))
            self._selected_polys = self._mirrored_polygon_selection(
                self._mirror_select_source_polys)
        else:
            self._selected_polys.discard(poly_id)
        context = self._mirror_selection_context()
        if context is not None:
            session, _owner, fam_obj = context
            vertices = self._polygon_vertices(
                fam_obj.skeleton, self._selected_polys)
            session.set_selection(vertices)
            if mirror_active:
                self._set_mirror_transform_source(
                    self._polygon_vertices(
                        fam_obj.skeleton,
                        self._mirror_select_source_polys))
        self._selected_poly = min(self._selected_polys, default=None)
        self.viewport.set_selected_polygon(self._selected_poly)
        self.viewport.set_highlight_polys(self._selected_polys)
        self._sync_poly_id_control()
        self._fill_polygon_inspector(self._selected_poly)
        self._update_repair_buttons()
        self._sync_edit_action_states()
        self._notify(
            f"Deselected polygon #{poly_id}."
            if self._selected_poly is not None else
            "Selection cleared.", 3500)

    def _on_selection_cleared(self) -> None:
        self._clear_model_selection(notify=True)

    def _clear_model_selection(self, notify: bool = False) -> None:
        """Clear every polygon/FX selection without changing the owner."""

        self._selected_polys.clear()
        self._mirror_select_source_polys.clear()
        self._selected_poly = None
        session = self.viewport.edit_session
        if session is not None:
            session.select_none()
        self.viewport.set_selected_polygon(None)
        self.viewport.set_highlight_polys(set())
        self._sync_poly_id_control()
        if self.fx_combo.currentIndex() > 0:
            self.fx_combo.blockSignals(True)
            self.fx_combo.setCurrentIndex(0)
            self.fx_combo.blockSignals(False)
        self._fill_polygon_inspector(None)
        self._update_repair_buttons()
        self._sync_edit_action_states()
        if notify:
            self._notify("Selection cleared.", 3500)

    def _on_tree_node_selected(self, current, _previous=None) -> None:
        if current is None or self._family is None:
            self._set_object_info(["No asset selected."])
            return
        data = current.data(0, Qt.ItemDataRole.UserRole)
        if not data:
            self._set_object_info(["No asset information available."])
            return
        kind, payload = data
        owner = self._tree_owner_for_item(current)
        if owner:
            self._select_owner(owner, preserve_asset_selection=True)
        if kind == "animation":
            self._play_asset_animation(current)
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

    def _fill_textures(self, family: AssetFamily | None) -> None:
        self.texture_list.clear()
        archive = getattr(family, "setbas_archive", None) or self._setbas
        family_textures = getattr(family, "textures", {})
        tracy_usage = getattr(family, "texture_tracy_usage", {})
        external_palette = getattr(family, "external_palette", None)
        for entry in build_texture_catalog(family, archive):
            name = entry.name
            ref = entry.reference
            img_name = next(
                (key for key in family_textures
                 if key.casefold() == name.casefold()), None)
            img = family_textures.get(img_name) if img_name else None
            status = entry.status
            if ref is not None and ref.path is not None and img is None:
                status = "decode failed"

            source = getattr(ref, "source", "") if ref is not None else ""
            if not source and entry.archive_resource is not None:
                source = "SET.BAS"
            lines = [f"[{status.upper()}] {name}"
                     + (f" (source: {source})" if source else "")]
            if ref is not None:
                if ref.found:
                    lines.append(ref.display_path)
                elif entry.archive_resource is None:
                    lines.append(
                        "NOT FOUND - use Dependencies to assign a file")
                if len(ref.candidates) > 1:
                    lines.append(
                        f"{len(ref.candidates)} candidates "
                        "(see Dependencies)")
            resource = entry.archive_resource
            if resource is not None:
                if resource.error:
                    lines.append(f"archive error: {resource.error}")
                elif not resource.decodable:
                    lines.append(
                        f"{resource.display_payload}: preview unavailable")
                elif img is None:
                    lines.append(
                        "archive texture indexed; decoded on selection/preview")
            tracy_modes = sorted(
                tracy_usage.get(name, set()) - {"none"})
            if tracy_modes:
                lines.append(
                    f"tracy transparency used: {', '.join(tracy_modes)}")
            if img:
                compression = "ByteRun1" if img.compression else "none"
                palette = ("own CMAP" if img.palette else
                           ("external PAL" if external_palette
                            else "MISSING (grayscale preview)"))
                body = "decoded" if img.has_body else "no BODY"
                lines.append(
                    f"{img.kind} {img.width}x{img.height}, "
                    f"{img.n_planes} planes, {compression}, {palette}, {body}")
                for warning in img.warnings:
                    lines.append(f"warning: {warning}")
            item = QListWidgetItem("\n".join(lines))
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setForeground(
                _STATUS_COLORS.get(status, QColor(220, 220, 220)))
            qimage = self._texture_qimage(img_name) if img_name else None
            if qimage is not None and not qimage.isNull():
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
