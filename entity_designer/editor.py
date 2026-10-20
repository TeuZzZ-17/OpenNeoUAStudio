"""The Entity Designer main window (``CollisionEditorWindow``).

It ties the tabs together: collision spheres/shape, ground, fire and gun points, cockpit, briefing size and
the Destruction FX preview. Script reading/writing lives in ``script_model``; the viewport, widgets and dialogs
have their own modules (see ``entity_designer/__init__.py``).
"""

from __future__ import annotations

from dataclasses import replace
import json
import math
from pathlib import Path
import re
import sys
import tempfile

from PySide6.QtCore import (
    QEvent,
    QFileSystemWatcher,
    QItemSelectionModel,
    QPoint,
    QPointF,
    QProcess,
    QProcessEnvironment,
    QSignalBlocker,
    Qt,
    QTimer,
)
from PySide6.QtGui import (
    QAction,
    QBrush,
    QCloseEvent,
    QKeySequence,
    QShortcut,
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
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QToolBar,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from entity_designer import destruction_fx as fx
from entity_designer.dialogs import (
    ApplyScriptDialog,
    ImportCollisionDialog,
    OpenScriptObjectDialog,
)
from entity_designer.fx_panel import DestructionFxPanel
from entity_designer.script_model import (
    _clamp_gun_angle,
    _collision_shape_export_filename,
    _data_root_for_path,
    _fire_point_preview_count,
    _gun_angle_max,
    _load_3ds_family,
    _normalized_vector3,
    _num_weapons_text,
    _number,
    _project_num_weapons_range,
    _public_dependency,
    _RADIUS_LOG_MAX,
    _RADIUS_LOG_MIN,
    _radius_number,
    _RADIUS_SLIDER_STEPS,
    _reference_for_visual_source,
    _resolve_data_asset_path,
    _resolve_patch_target_block,
    _resolve_script_visual_source,
    _SCRIPT_TAB_BRIEFING,
    _SCRIPT_TAB_COCKPIT,
    _SCRIPT_TAB_COLLISION,
    _SCRIPT_TAB_FIRE,
    _SCRIPT_TAB_GUN,
    _SCRIPT_TAB_LABELS,
    _SCRIPT_TAB_ORDER,
    _sphere_number,
    CollisionProject,
    CollisionScriptError,
    CollisionSphere,
    effective_runtime_radius,
    export_collision_text,
    find_script_blocks,
    fire_point_positions,
    gun_angle_degrees,
    gun_direction_index,
    GUN_DIRECTION_LABELS,
    GUN_DIRECTION_SEQUENCE,
    GUN_FREE_SIDE_THRESHOLD,
    gun_point_families_in_block,
    GUN_POINT_TABLE_BASE_WIDTHS,
    GUN_POINT_TABLE_GROWTH,
    GunPoint,
    import_briefing_size_block,
    import_cockpit_camera_block,
    import_collision_block,
    import_collision_shape_reference,
    import_fire_points_block,
    import_gun_points_block,
    import_overeof_block,
    import_turret_limits,
    LEGACY,
    OPENNEOUA,
    OPENNEOUA_SCRIPT_FILE_FILTER,
    read_script_file,
    runtime_vp_table,
    script_model_references,
    ScriptBlock,
    TurretLimits,
    TYPE_LABELS,
    validate_project,
    VEHICLE,
    VehicleModelReference,
    WEAPON,
    WINDOW_TITLE,
)
from entity_designer.shape import (
    geometry_fingerprint,
    parse_collision_shape,
    read_collision_shape,
    visual_transform_point,
    write_collision_shape,
)
from entity_designer.sphere_generator import (
    ACCURACY_PRESETS,
    generate_collision_spheres,
    UNIT_COLL_MAX_COUNT,
)
from entity_designer.viewport import CollisionMoveGizmo, CollisionViewport
from entity_designer.widgets import (
    _geometry_component_key,
    _GEOMETRY_COMPONENT_ROLE,
    _type_button_stylesheet,
    CompactScaleSpinBox,
    GeometryPartSelectionWidget,
    SphereTreeWidget,
)
from studio.assets.dependencies import filter_tree as filter_asset_tree
from studio.assets.family import AssetFamily, load_asset_family, load_manual_family
from studio.assets.visproto import (
    EmbeddedVPSet,
    normalize_base_name,
    normalize_skeleton_name,
    reconstruct_embedded_vps,
    VPEmbeddedError,
    VPTable,
)
from studio.rendering.asset_viewport import (
    VIEW_PRESETS,
)
from studio.ui.shell import compat_color, MIXED, OPENNEOUA as COMPAT_OPENNEOUA, VANILLA
from studio.ui.tabs import PaletteTabStrip
from studio.ui.widgets import (
    configure_operation_status_bar,
    create_import_bas_archive_action,
    install_standard_file_menu_tail,
)
from entity_designer.widgets import (
    FIRE_POINT_COLOR,
    GUN_POINT_COLOR,
    TURRET_DOWN_COLOR,
    TURRET_SIDE_COLOR,
    TURRET_UP_COLOR,
    TYPE_COLORS,
)


_MODEL_NAME_ROLE = int(Qt.ItemDataRole.UserRole) + 1


_SPHERE_INDEX_ROLE = int(Qt.ItemDataRole.UserRole) + 2


_MODEL_VP_ROLE = int(Qt.ItemDataRole.UserRole) + 3


_FIRE_POINT_INDEX_ROLE = int(Qt.ItemDataRole.UserRole) + 4


_GUN_POINT_INDEX_ROLE = int(Qt.ItemDataRole.UserRole) + 5


# Left-list row carrying the script unit it can load (VehicleModelReference).
_SCRIPT_REFERENCE_ROLE = int(Qt.ItemDataRole.UserRole) + 6


def _fx_item_state(item) -> str | None:
    """The model state a preview item belongs to (None = a plain effect)."""

    if item.kind == "unit":
        state = item.label.split(" · ", 1)[0]
        return "normal" if state == "attached" else state
    return {"plasma": "genesis", "launch": "launch", "decoration": "normal",
            "tracer": "normal", "cluster": "normal", "laser": "normal"}.get(item.kind)


class CollisionEditorWindow(QMainWindow):
    """Manual collision editor built on OpenNeoUAStudio's existing systems."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        from studio.ui.style import apply_studio_style
        apply_studio_style()
        self.setWindowTitle(WINDOW_TITLE)
        self.resize(1100, 720)
        self.project = CollisionProject()
        self._hide_splitter_grip = False
        self._splitter_grip_filtered = False
        self.family: AssetFamily | None = None
        self._vp_embedded: EmbeddedVPSet | None = None
        self._vp_table: VPTable | None = None
        self._vp_table_source: str = ""
        # vp_id -> (owner path, display name), resolved while filling the
        # model list. Script imports bind their unit to one VP through it.
        self._model_info_by_vp: dict[int, tuple[str, str]] = {}
        self._active_script_path: Path | None = None
        self._active_script_kind: str = ""
        self._active_script_id: int | None = None
        self._active_model_reference: VehicleModelReference | None = None
        self._active_visual_reference: VehicleModelReference | None = None
        self._active_base_path: Path | None = None
        self._current_owner: str | None = None
        self._current_owner_base_bounds: tuple[float, float, float, float, float, float] | None = None
        self._viewport_owner: str | None = None
        self._selected = -1
        self._selected_spheres: set[int] = set()
        self._selected_fire_point = -1
        self._selected_gun_point = -1
        # Family used only when authoring a new Gun Point. Existing imported
        # points keep their original robo_* / unit_* scheme unless explicitly
        # removed and recreated, avoiding silent script-family conversions.
        self._new_gun_point_scheme = "unit"
        # Reset All returns Gun Points and their referenced turret limits to
        # the source state captured by the last explicit script load/import.
        self._loaded_gun_points_enabled = False
        self._loaded_gun_points: list[GunPoint] = []
        self._loaded_unit_gun_default_icon = ""
        self._loaded_gun_point_scheme = "unit"
        self._loaded_turret_limits: dict[int, TurretLimits] = {}
        # Script-authored cockpit baseline. This is source state, not part of
        # undo/redo: Restore Script Position always returns to the coordinates
        # read during the last explicit script load/import.
        self._loaded_cockpit_camera_enabled = False
        self._loaded_cockpit_camera_offset = (0.0, 0.0, 0.0)
        # Source/import baseline used by the toolbar Reset command.  Reset is
        # deliberately workspace-scoped: Collision, Fire Points, Gun Points
        # and Cockpit View can be reverted independently without disturbing
        # edits made in the other tabs.
        self._tab_reset_baseline = self.project.snapshot()
        self._export_baseline = self.project.snapshot()
        self._tab_reset_gun_point_scheme = self._new_gun_point_scheme
        self._modified = False
        self._syncing = False
        self._last_directory = Path.home()
        self._undo: list[tuple] = []
        self._redo: list[tuple] = []
        self._radius_slider_active = False
        self._radius_spin_active = False
        self._overeof_spin_active = False
        self._vehicle_preview_active_edits: set[str] = set()
        self._turret_slider_active_edits: set[str] = set()
        self._gun_direction_slider_active = False
        self._model_filter_expansion: dict[int, bool] | None = None
        # The resource list must not be allowed to change the VP/model behind
        # an imported script definition.  It becomes interactive only in the
        # normal archive/SKLT browsing workflow.
        self._model_browser_enabled = False
        self._collision_bake_process: QProcess | None = None
        self._collision_bake_cancelled = False
        self._collision_bake_context: dict = {}
        self._collision_bake_tempdir = None
        self._collision_geometry_cache_key = None
        self._collision_geometry_components = ()
        self._collision_geometry_triangles_by_owner = {}
        self._collision_geometry_owners = ()
        self._collision_geometry_fingerprints = {}
        self._collision_geometry_preview = None
        # Left list content: archive models normally, or the unit browser of
        # the last imported script (see _set_script_unit_mode).
        self._script_unit_mode = False
        # Units currently displayed in that browser. Switching unit must not
        # rebuild identical rows, or the click doing the switch gets corrupted.
        self._script_unit_references: list[VehicleModelReference] = []
        self._script_unit_visual_reference: VehicleModelReference | None = None
        # Destruction FX preview: FX vehicles for dest_fx, the viewport scope
        # widened with the effect VP owners while the tab is shown, the VP
        # owner map, the definition already framed once, and the script
        # files watched by Auto reload.
        self._fx_catalog: dict[int, fx.LegacyFxVehicle] = {}
        self._fx_scope: set[str] | None = None
        self._fx_owner_map: dict[int, str] = {}
        self._fx_fitted_definition = None
        self._fx_edit_camera = None
        self._fx_source_from_catalog = False
        self._fx_script_sources = set()
        self._fx_asset_paths = set()
        self._fx_watch_signatures = {}
        self._fx_changed_assets = set()
        self._fx_watcher = QFileSystemWatcher(self)
        self._fx_watcher.fileChanged.connect(self._fx_watched_file_changed)
        self._fx_watcher.directoryChanged.connect(self._fx_watched_directory_changed)
        self._fx_reload_timer = QTimer(self)
        self._fx_reload_timer.setSingleShot(True)
        self._fx_reload_timer.setInterval(300)
        self._fx_reload_timer.timeout.connect(self.reload_fx_from_file)
        # Safety net for the file watcher: while the FX tab is open the files
        # are also compared with what was loaded, so no edit is ever missed
        # (editors that save by renaming, locked files, a tab left and reopened).
        self._fx_tracked: set[str] = set()
        self._fx_loaded_signatures: dict = {}
        self._fx_failed_signatures = None
        self._fx_script_snapshot = None
        self._fx_poll_timer = QTimer(self)
        self._fx_poll_timer.setInterval(400)
        self._fx_poll_timer.timeout.connect(self._fx_poll)

        configure_operation_status_bar(self)

        self.viewport = CollisionViewport()
        from studio.rendering.gpu import add_renderer_badge
        self.renderer_badge = add_renderer_badge(self, self.viewport)
        self.viewport.spherePicked.connect(self._select_sphere)
        self.viewport.sphereToggleRequested.connect(self._toggle_sphere)
        self.viewport.sphereBoxSelectionRequested.connect(
            self._select_sphere_box)
        self.viewport.firePointPicked.connect(self._select_fire_point)
        self.viewport.gunPointPicked.connect(self._select_gun_point)
        self.viewport.sphereContextMenuRequested.connect(
            self._show_sphere_context_menu)
        self.viewport.sphereNudgeRequested.connect(self._gizmo_nudge)
        self.viewport.cockpitNudgeRequested.connect(self._cockpit_nudge)
        self.viewport.statusMessage.connect(
            lambda text: self.statusBar().showMessage(text, 4500))
        self.viewport.animationFrameChanged.connect(
            self._on_animation_frame_changed)
        self.model_tree = QTreeWidget()
        from studio.ui.widgets import use_standard_list_style
        use_standard_list_style(self.model_tree)
        self.model_tree.setHeaderLabels(["Internal path", "VP"])
        self.model_tree.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.model_tree.currentItemChanged.connect(self._model_changed)
        self.sphere_tree = SphereTreeWidget()
        use_standard_list_style(self.sphere_tree)
        self.sphere_tree.setHeaderLabels(
            ["Sphere", "Radius", "Visible", "X", "Y", "Z", "Index"])
        self.sphere_tree.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        # Collision rows are flat, not hierarchical. Removing the tree
        # indentation gives the Sphere label the full first-column width and
        # keeps the descriptive text anchored to the left edge.
        self.sphere_tree.setRootIsDecorated(False)
        self.sphere_tree.setIndentation(0)
        self.sphere_tree.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.sphere_tree.itemSelectionChanged.connect(
            self._sphere_tree_selection_changed)
        self.fire_point_tree = QTreeWidget()
        use_standard_list_style(self.fire_point_tree)
        self.fire_point_tree.setHeaderLabels(["Point", "X", "Y", "Z"])
        self.fire_point_tree.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.fire_point_tree.currentItemChanged.connect(
            self._fire_point_tree_selection_changed)
        self.gun_point_tree = QTreeWidget()
        use_standard_list_style(self.gun_point_tree)
        self.gun_point_tree.setHeaderLabels([
            "Gun", "Family", "Vehicle", "Side", "Up", "Down", "X", "Y", "Z",
        ])
        self.gun_point_tree.setRootIsDecorated(False)
        self.gun_point_tree.setIndentation(0)
        self.gun_point_tree.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.gun_point_tree.currentItemChanged.connect(
            self._gun_point_tree_selection_changed)

        self.sphere_tree.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.sphere_tree.customContextMenuRequested.connect(
            self._show_sphere_tree_context_menu)
        self.fire_point_tree.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.fire_point_tree.customContextMenuRequested.connect(
            self._show_fire_point_tree_context_menu)
        self.gun_point_tree.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.gun_point_tree.customContextMenuRequested.connect(
            self._show_gun_point_tree_context_menu)

        self._build_actions()
        self._build_ui()
        # F5 reloads the Destruction FX preview from the script file.
        reload_shortcut = QShortcut(QKeySequence(Qt.Key.Key_F5), self)
        reload_shortcut.activated.connect(
            lambda: self._is_fx_tab_active() and self.reload_fx_from_file())
        self._set_model_browser_enabled(False)
        self._fit_initial_window_to_screen()
        self._sync_all()
        self.statusBar().showMessage(
            "BASE/SKLT assets are read-only; only explicit text exports or "
            "confirmed script application write files.")

    def _build_actions(self):
        file_menu = self.menuBar().addMenu("&File")
        self.file_menu = file_menu
        self.open_base_action = create_import_bas_archive_action(
            self, self.open_base_dialog, shortcut=True)
        self.open_base_action.setToolTip(
            "Import a BASE/SET archive or an external 3DS visual model.")
        self.open_sklt_action = QAction("Import SKLT", self)
        self.open_sklt_action.triggered.connect(self.open_sklt_dialog)
        self.open_vehicle_script_action = QAction(
            "Import Vehicle / Weapon from Script", self)
        self.open_vehicle_script_action.setIconText("Import Script Object")
        self.open_vehicle_script_action.triggered.connect(
            self.open_vehicle_script_dialog)
        self.import_action = QAction("Import Collision Text", self)
        self.import_action.setIconText("Import Collision Text")
        self.import_action.triggered.connect(self.import_collisions)

        self.file_import_menu = file_menu.addMenu("Import")
        for action in (
                self.open_base_action, self.open_sklt_action,
                self.open_vehicle_script_action, self.import_action):
            self.file_import_menu.addAction(action)

        self.export_collision_shape_action = QAction("Export Collision Shape", self)
        self.export_collision_shape_action.triggered.connect(self.export_collision_shape)
        self.export_collision_shape_action.setEnabled(False)
        self.copy_output_action = QAction(
            "Copy Output to Clipboard", self)
        self.copy_output_action.triggered.connect(self.copy_output)

        self.file_export_menu = file_menu.addMenu("Export")
        for action in (self.copy_output_action,
                       self.export_collision_shape_action):
            self.file_export_menu.addAction(action)

        self.apply_script_action = QAction("Apply to Script", self)
        self.apply_script_action.setIconText("Apply to Script")
        self.apply_script_action.triggered.connect(self.apply_to_script)
        self.save_loaded_script_action = QAction(
            "Overwrite to Existing Script", self)
        self.save_loaded_script_action.setIconText(
            "Overwrite to Existing Script")
        self.save_loaded_script_action.setToolTip(
            "Overwrite the vehicle or weapon definition previously imported "
            "from a script. A preview is provided before overwrite.")
        self.save_loaded_script_action.triggered.connect(
            self.overwrite_loaded_script)
        self.overwrite_loaded_action = self.save_loaded_script_action

        self.file_export_menu.addSeparator()
        self.file_export_menu.addAction(self.apply_script_action)
        self.file_export_menu.addAction(self.save_loaded_script_action)

        (self.close_bas_archive_action,
         self.exit_action) = install_standard_file_menu_tail(
            file_menu, self, close_archive_callback=self.close_current_archive)
        edit_menu = self.menuBar().addMenu("&Edit")
        self.undo_action = QAction("Undo", self)
        self.undo_action.setIconText("< Undo")
        self.undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        self.undo_action.triggered.connect(self.undo)
        edit_menu.addAction(self.undo_action)
        self.redo_action = QAction("Redo", self)
        self.redo_action.setIconText("Redo >")
        self.redo_action.setShortcuts([
            QKeySequence.StandardKey.Redo, QKeySequence("Ctrl+Shift+Z")])
        self.redo_action.triggered.connect(self.redo)
        edit_menu.addAction(self.redo_action)

        self.add_legacy_action = QAction("Add Legacy Radius", self)
        self.add_legacy_action.triggered.connect(self.add_legacy)
        self.add_openneoua_action = QAction("Add OpenNeoUA Collision", self)
        self.add_openneoua_action.triggered.connect(
            lambda: self.add_compound(OPENNEOUA))
        self.duplicate_action = QAction("Duplicate Sphere", self)
        self.duplicate_action.setShortcut(QKeySequence("Ctrl+D"))
        self.duplicate_action.triggered.connect(self.duplicate_sphere)
        self.delete_action = QAction("Delete Sphere", self)
        self.delete_action.setShortcut(QKeySequence.StandardKey.Delete)
        self.delete_action.triggered.connect(self.delete_sphere)
        self.change_to_legacy_action = QAction(
            "Change to Legacy Radius", self)
        self.change_to_openneoua_action = QAction(
            "Change to OpenNeoUA Collision", self)
        self.change_to_legacy_action.triggered.connect(
            lambda: self.change_sphere_type(LEGACY))
        self.change_to_openneoua_action.triggered.connect(
            lambda: self.change_sphere_type(OPENNEOUA))
        self.mirror_x_action = QAction("Mirror on X Axis", self)
        self.mirror_y_action = QAction("Mirror on Y Axis", self)
        self.mirror_z_action = QAction("Mirror on Z Axis", self)
        self.mirror_x_action.triggered.connect(
            lambda: self.mirror_selected_sphere("x"))
        self.mirror_y_action.triggered.connect(
            lambda: self.mirror_selected_sphere("y"))
        self.mirror_z_action.triggered.connect(
            lambda: self.mirror_selected_sphere("z"))
        self.generate_collision_sphere_actions = {}
        for preset in ACCURACY_PRESETS:
            action = QAction(preset.label, self)
            action.triggered.connect(
                lambda _checked=False, key=preset.key:
                self.generate_collision_spheres(key))
            self.generate_collision_sphere_actions[preset.key] = action
        # Sphere-specific editing controls live inside the Collision tab.
        # Edit keeps only the standard history commands, avoiding a second,
        # generic home for controls that are meaningful only for collisions.

        # Entity Designer owns this menu locally.  The three creation actions
        # no longer consume toolbar width and no longer live under Edit.
        self.add_menu = self.menuBar().addMenu("&Add")
        self.add_menu.addAction(self.add_legacy_action)
        self.add_menu.addAction(self.add_openneoua_action)
        self.generate_collision_spheres_menu = self.add_menu.addMenu(
            "Generate Collision Spheres")
        self._populate_generate_collision_spheres_menu(
            self.generate_collision_spheres_menu)

        self.viewpoint_menu = self.menuBar().addMenu("Viewpoint")
        self.viewpoint_actions = {}
        viewpoint_options = (
            ("Show Model", True, self.viewport.set_model_visible),
            ("Show Textures", True, self.viewport.set_textures_visible),
            ("Show Legacy Radius", True, lambda value:
             self.viewport.set_collision_category_visible(LEGACY, value)),
            ("Show OpenNeoUA Collisions", True, lambda value:
             self.viewport.set_collision_category_visible(OPENNEOUA, value)),
            ("Show Ground Simulation", True,
             self.viewport.set_ground_simulation_visible),
            ("Show Overeof", True,
             self.viewport.set_overeof_visible),
            ("Show Fire Points", True,
             self.viewport.set_fire_points_visible),
            ("Show Gun Points", True,
             self.viewport.set_gun_points_visible),
            ("Show Turret Limits", True,
             self.viewport.set_turret_limits_visible),
        )
        for text, checked, slot in viewpoint_options:
            action = QAction(text, self)
            action.setCheckable(True)
            action.setChecked(checked)
            action.toggled.connect(slot)
            if text in {
                    "Show Legacy Radius", "Show OpenNeoUA Collisions",
                    "Show Ground Simulation", "Show Overeof",
                    "Show Fire Points", "Show Gun Points",
                    "Show Turret Limits"}:
                action.toggled.connect(self._sync_workspace_overlay_visibility)
            self.viewpoint_menu.addAction(action)
            self.viewpoint_actions[text] = action

        self.project_summary_menu = self.menuBar().addMenu("Project Summary")
        self.reset_view_action = QAction("Reset View", self)
        self.reset_view_action.triggered.connect(self._reset_view)
        self._build_model_preview_toolbar()
        self.viewport.manualCameraChanged.connect(
            self._on_manual_camera_changed)

    def _build_model_preview_toolbar(self):
        """Create the single compact toolbar used by the Entity Designer."""

        toolbar = QToolBar("Model Preview Scale", self)
        toolbar.setObjectName("modelPreviewScaleTools")
        toolbar.setMovable(False)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, toolbar)

        # Match the main Model Editor toolbar: expose the same Undo/Redo
        # history actions as compact buttons before View preset.
        self.toolbar_undo_button = QPushButton("< Undo")
        self.toolbar_undo_button.setEnabled(False)
        self.toolbar_undo_button.setMinimumWidth(88)
        self.toolbar_undo_button.setToolTip(
            "Undo the latest Entity Designer edit.")
        self.toolbar_undo_button.clicked.connect(self.undo_action.trigger)
        toolbar.addWidget(self.toolbar_undo_button)

        self.toolbar_redo_button = QPushButton("Redo >")
        self.toolbar_redo_button.setEnabled(False)
        self.toolbar_redo_button.setMinimumWidth(88)
        self.toolbar_redo_button.setToolTip(
            "Redo the latest Entity Designer edit.")
        self.toolbar_redo_button.clicked.connect(self.redo_action.trigger)
        toolbar.addWidget(self.toolbar_redo_button)

        toolbar.addWidget(QLabel(" View preset: "))
        self.toolbar_view_preset_combo = QComboBox()
        self.toolbar_view_preset_combo.addItems(VIEW_PRESETS)
        self.toolbar_view_preset_combo.currentTextChanged.connect(
            self._on_view_preset_changed)
        toolbar.addWidget(self.toolbar_view_preset_combo)
        toolbar.addSeparator()
        self.reset_tab_button = QPushButton("Reset")
        self.reset_tab_button.setToolTip(
            "Reset all authored changes in the current Entity Designer tab "
            "to the last loaded/imported state. Confirmation is required.")
        self.reset_tab_button.clicked.connect(self._reset_current_tab)
        toolbar.addWidget(self.reset_tab_button)
        # Compatibility alias for older integrations that only locate the
        # button by attribute; its meaning is now the requested tab reset.
        self.reset_model_scale_button = self.reset_tab_button

        # Match the Model Editor's Reset View control and semantics: same
        # button text/tooltip, immediately after Reset, and a stable captured
        # camera destination for each selected model.
        self.toolbar_reset_view_button = QPushButton("Reset View")
        self.toolbar_reset_view_button.setEnabled(False)
        self.toolbar_reset_view_button.setToolTip(
            "Restore the camera to the original view used when this model "
            "was loaded.")
        self.toolbar_reset_view_button.clicked.connect(
            self.reset_view_action.trigger)
        toolbar.addWidget(self.toolbar_reset_view_button)
        toolbar.addSeparator()

        # Reuse AssetViewport's animation engine exactly as the Model Editor
        # does.  Only the compact UI shell is repeated here; frame stepping,
        # timing and VANM playback remain one shared implementation.
        toolbar.addWidget(QLabel(" VANM preview: "))
        self.play_button = QPushButton("Play")
        self.play_button.setCheckable(True)
        self.play_button.setEnabled(False)
        self.play_button.toggled.connect(self._toggle_play)
        toolbar.addWidget(self.play_button)
        self.step_button = QPushButton(">>")
        self.step_button.setEnabled(False)
        self.step_button.clicked.connect(self.viewport.step_animation)
        toolbar.addWidget(self.step_button)
        toolbar.addWidget(QLabel(" Speed: "))
        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setRange(0.05, 8.0)
        self.speed_spin.setSingleStep(0.25)
        self.speed_spin.setValue(1.0)
        self.speed_spin.setEnabled(False)
        self.speed_spin.valueChanged.connect(
            self._on_animation_speed_changed)
        toolbar.addWidget(self.speed_spin)
        toolbar.addSeparator()

        toolbar.addWidget(QLabel(" Model Preview Scale: "))
        self.model_scale_spins = {}
        for axis in ("X", "Y", "Z"):
            toolbar.addWidget(QLabel(f" {axis} "))
            spin = CompactScaleSpinBox()
            spin.setRange(0.0, 1000.0)
            spin.setDecimals(3)
            spin.setSingleStep(0.1)
            spin.setValue(1.0)
            spin.setKeyboardTracking(False)
            spin.setMinimumWidth(92)
            spin.setMaximumWidth(118)
            spin.setToolTip(
                f"OpenNeoUA scale_{axis.lower()} preview. "
                "The model changes size; collision spheres and exported "
                "coll_* values are not transformed automatically.")
            spin.valueChanged.connect(self._model_preview_scale_changed)
            self.model_scale_spins[axis.lower()] = spin
            toolbar.addWidget(spin)
        self.model_scale_x_spin = self.model_scale_spins["x"]
        self.model_scale_y_spin = self.model_scale_spins["y"]
        self.model_scale_z_spin = self.model_scale_spins["z"]
        toolbar.addSeparator()
        self.model_preview_scale_toolbar = toolbar
        # Compatibility alias retained for integrations that only used this
        # attribute to locate the preview-scale controls.
        self.model_preview_scale_box = toolbar

    def _build_ui(self):
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter = splitter
        self.setCentralWidget(splitter)

        source_panel = QWidget()
        source_panel.setMinimumWidth(230)
        source_panel.setMaximumWidth(400)
        source_panel.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        source_layout = QVBoxLayout(source_panel)
        source_layout.addWidget(QLabel("Game resources"))
        from studio.ui.browsers import GameObjectBrowser, configure_resource_tree
        self.game_object_browser = GameObjectBrowser(self, kinds=("vehicle", "weapon"))
        self.game_object_browser.selected.connect(self._open_game_object)
        self.game_object_browser.setChanged.connect(self._game_set_changed)
        self.model_search = QLineEdit(self)
        self.model_search.setPlaceholderText("Search model names")
        self.model_search.setClearButtonEnabled(True)
        self.model_search.setToolTip(
            "Filter models by name, internal path or owner path.")
        self.model_search.textChanged.connect(self._filter_models)
        # The internal model index still resolves VP owners for script imports.
        self.model_search.hide()
        self.model_tree.setParent(self)
        self.model_tree.hide()
        configure_resource_tree(self.model_tree)
        source_layout.addWidget(self.game_object_browser, 1)
        self.source_label = QLabel("No source loaded.")
        self.source_label.setWordWrap(True)
        source_layout.addWidget(self.source_label)
        model_header = self.model_tree.header()
        model_header.setMinimumSectionSize(36)
        # QHeaderView stretches the last section by default.  With VP as the
        # last column that silently consumed half the list, leaving a large
        # empty gap and truncating Internal path.  Keep VP compact so the
        # existing panel width is spent on the model path instead.
        model_header.setStretchLastSection(False)
        model_header.setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        model_header.setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents)
        self.model_tree.headerItem().setTextAlignment(
            1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.model_tree.setColumnWidth(1, 48)
        self.model_tree.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.model_tree.setTextElideMode(Qt.TextElideMode.ElideNone)
        splitter.addWidget(source_panel)

        # The viewport stays dedicated to the asset preview.  The shared move
        # gizmo lives in the right properties column below the domain tabs so
        # it can use substantially more room without covering the model.
        viewport_panel = QWidget()
        self.viewport_panel = viewport_panel
        viewport_layout = QGridLayout(viewport_panel)
        self.viewport_layout = viewport_layout
        viewport_layout.setContentsMargins(0, 0, 0, 0)
        viewport_layout.setSpacing(0)
        viewport_layout.addWidget(self.viewport, 0, 0)
        splitter.addWidget(viewport_panel)

        properties = QWidget()
        right = QVBoxLayout(properties)
        # Leave a small visual gap below the top toolbar so the preview-scale
        # controls do not look fused with the right-side authoring panel.
        self._properties_layout = right
        self._properties_layout_margins = (4, 7, 4, 3)
        right.setContentsMargins(*self._properties_layout_margins)
        right.setSpacing(1)
        # Keep the right-side workspaces explicit. Collision, Fire Points, Gun
        # Points and the OpenNeoUA-only Cockpit View share one compact properties
        # column instead of stacking unrelated controls vertically.
        self.properties_tabs = QTabWidget()
        self.properties_tabs.setDocumentMode(True)
        self.collision_tab = QWidget()
        self.collision_shape_tab = QWidget()
        self.ground_tab = QWidget()
        self.fire_points_tab = QWidget()
        self.gun_points_tab = QWidget()
        self.cockpit_tab = QWidget()
        self.collision_tab_layout = QVBoxLayout(self.collision_tab)
        self.collision_shape_tab_layout = QVBoxLayout(
            self.collision_shape_tab)
        self.ground_tab_layout = QVBoxLayout(self.ground_tab)
        self.fire_points_tab_layout = QVBoxLayout(self.fire_points_tab)
        self.gun_points_tab_layout = QVBoxLayout(self.gun_points_tab)
        self.cockpit_tab_layout = QVBoxLayout(self.cockpit_tab)
        for tab, layout in (
                (self.collision_tab, self.collision_tab_layout),
                (self.collision_shape_tab, self.collision_shape_tab_layout),
                (self.ground_tab, self.ground_tab_layout),
                (self.fire_points_tab, self.fire_points_tab_layout),
                (self.gun_points_tab, self.gun_points_tab_layout),
                (self.cockpit_tab, self.cockpit_tab_layout)):
            layout.setContentsMargins(2, 3, 2, 3)
            layout.setSpacing(3)
            tab.setMinimumSize(0, 0)
            tab.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.collision_tab_index = self.properties_tabs.addTab(
            self.collision_tab, "Collision Spheres")
        self.collision_shape_tab_index = self.properties_tabs.addTab(
            self.collision_shape_tab, "Collision Shape")
        # Ground alignment (overeof) is a vehicle placement setting, not a
        # collision sphere: it has its own page.
        self.ground_tab_index = self.properties_tabs.addTab(
            self.ground_tab, "Ground Alignment")
        self.fire_points_tab_index = self.properties_tabs.addTab(
            self.fire_points_tab, "Fire Points")
        self.gun_points_tab_index = self.properties_tabs.addTab(
            self.gun_points_tab, "Gun Points")
        self.cockpit_tab_index = self.properties_tabs.addTab(
            self.cockpit_tab, "Cockpit View")
        from entity_designer.briefing import BriefingControls, BriefingPreview
        self.briefing_tab = BriefingControls(self)
        self.briefing_tab_index = self.properties_tabs.addTab(self.briefing_tab, "Briefing Model Size")
        self.briefing_preview = BriefingPreview(self)
        self.renderer_badge.add_viewport(self.briefing_preview)
        self.viewport_layout.addWidget(self.briefing_preview, 0, 0)
        self.briefing_preview.hide()
        # Al posto dell'anteprima, nella tab Collision Shape delle armi.
        self.shape_weapon_notice = QLabel(
            "Collision Shape mode is not intended for, or available on, "
            "weapons.\nIt is available only for vehicles.")
        self.shape_weapon_notice.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.shape_weapon_notice.setWordWrap(True)
        self.shape_weapon_notice.setMinimumSize(0, 0)
        self.viewport_layout.addWidget(self.shape_weapon_notice, 0, 0)
        self.shape_weapon_notice.hide()
        self.fx_panel = DestructionFxPanel(self)
        self.fx_tab_index = self.properties_tabs.addTab(
            self.fx_panel, "Visuals && FX")
        self.fx_panel.sound.resolver = self._resolve_fx_sound
        self.fx_panel.frameChanged.connect(self._push_fx_frame)
        self.fx_panel.fitRequested.connect(self._fit_fx_view)
        self.fx_panel.documentEdited.connect(self._fx_document_edited)
        self.fx_panel.draftStateChanged.connect(self._fx_draft_state_changed)
        self.fx_panel.sourceSaved.connect(self._fx_sources_saved)
        self.fx_panel.visualsSaved.connect(self.reload_fx_from_file)
        # The playback bar uses the full width under the viewport.
        self.viewport_layout.addWidget(self.fx_panel.transport_bar, 1, 0)
        # The bar keeps its natural height; the viewport takes the rest.
        self.viewport_layout.setRowStretch(0, 1)
        self.viewport_layout.setRowStretch(1, 0)
        self.fx_panel.transport_bar.hide()
        # Wireframe: the one the definition has now, or a new one generated from its model.
        from entity_designer.wireframe_panel import TAB_TITLE, WireframePanel
        self.wireframe_panel = WireframePanel(self.viewport, self)
        self.wireframe_tab_index = self.properties_tabs.addTab(
            self.wireframe_panel, TAB_TITLE)
        self.properties_tabs.currentChanged.connect(
            self._properties_tab_changed)
        self.properties_tabs.setMinimumHeight(0)
        self.properties_tabs.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        # The tab workspace owns the flexible vertical area; the shared gizmo
        # below has a stable height on every tab. This also lets the lists
        # extend down to the gizmo instead of leaving dead space.
        # Studio-wide palette tabs; the dot shows each page's compatibility.
        self._tab_compatibility = {
            self.collision_tab: (
                MIXED, "Legacy Radius (radius) works in vanilla Urban "
                "Assault; OpenNeoUA Collision spheres (coll_*) work only in "
                "OpenNeoUA."),
            self.collision_shape_tab: (
                COMPAT_OPENNEOUA, "collision_shape is read only by OpenNeoUA; "
                "vanilla keeps using the radius and collision spheres."),
            self.ground_tab: (
                VANILLA, "overeof (height of the vehicle above the ground) "
                "works in vanilla Urban Assault and in OpenNeoUA."),
            self.fire_points_tab: (
                MIXED, "fire_x/y/z and a fixed num_weapons work in vanilla; a "
                "Min/Max weapons range is OpenNeoUA only."),
            self.gun_points_tab: (
                MIXED, "robo_* gun points (Host Stations) and turret angles "
                "are vanilla; unit_* gun points on other vehicles are "
                "OpenNeoUA only."),
            self.cockpit_tab: (
                COMPAT_OPENNEOUA, "Cockpit camera offsets exist only in "
                "OpenNeoUA. The preview keeps the vehicle's real orientation; "
                "only the camera position is authored."),
            self.briefing_tab: (
                COMPAT_OPENNEOUA, "briefing_size exists only in OpenNeoUA."),
            self.fx_panel: (
                MIXED, "Code edits update the preview; Save writes the source script. Vanilla "
                "dest_fx/scale_fx effects work everywhere; begin_fx effects "
                "and the OpenNeoUA weapon/vehicle FX (tracers, decorations, "
                "clusters, lasers, debuffs, palette flashes) need OpenNeoUA, "
                "as labelled in the list. Camera shake (shk_*) is not shown."),
            self.wireframe_panel: (
                VANILLA, "The wireframe is the vanilla SKLT file named by the "
                "wireframe key. Export writes only that file: the script must "
                "still reference it."),
        }
        self.properties_tab_strip = PaletteTabStrip.install(
            self.properties_tabs, markers={
                page: compat_color(level)
                for page, (level, _note) in self._tab_compatibility.items()})
        right.addWidget(self.properties_tabs, 1)

        self.ground_alignment_box = QGroupBox("Ground Alignment")
        ground_layout = QVBoxLayout(self.ground_alignment_box)
        ground_layout.setContentsMargins(6, 4, 6, 4)
        ground_layout.setSpacing(3)
        ground_controls = QGridLayout()
        ground_controls.setHorizontalSpacing(5)
        ground_controls.setVerticalSpacing(3)
        self.overeof_check = QCheckBox("Include Overeof")
        self.overeof_check.setToolTip(
            "When enabled, the model and collision preview follow the "
            "OpenNeoUA placement rule actor_y = ground_y - overeof, and the "
            "value is included in vehicle text output.")
        self.overeof_check.toggled.connect(
            self._overeof_enabled_changed)
        ground_controls.addWidget(self.overeof_check, 0, 0)
        self.overeof_spin = self._coordinate_spin()
        # Overeof is authored as a practical placement value. One visible
        # decimal keeps useful half/tenth-unit precision without presenting
        # noisy values such as 20,0000 in the compact properties panel.
        self.overeof_spin.setDecimals(1)
        self.overeof_spin.setSingleStep(1.0)
        self.overeof_spin.setKeyboardTracking(True)
        self.overeof_spin.setMinimumWidth(105)
        self.overeof_spin.valueChanged.connect(
            self._overeof_value_changed)
        self.overeof_spin.editingFinished.connect(
            self._finish_overeof_edit)
        ground_controls.addWidget(self.overeof_spin, 0, 1)
        self.suggest_overeof_button = QPushButton("Suggest from Model")
        self.suggest_overeof_button.setToolTip(
            "Use the lowest scaled model point as a starting Overeof "
            "estimate. The result remains manually editable.")
        self.suggest_overeof_button.clicked.connect(
            self._suggest_overeof_from_bounds)
        ground_controls.addWidget(self.suggest_overeof_button, 1, 0)
        self.reset_overeof_button = QPushButton("Reset")
        self.reset_overeof_button.setToolTip(
            "Reset Overeof preview to 0 and omit it from output.")
        self.reset_overeof_button.clicked.connect(self._reset_overeof)
        ground_controls.addWidget(self.reset_overeof_button, 1, 1)
        ground_controls.setColumnStretch(1, 1)
        ground_layout.addLayout(ground_controls)
        self.ground_alignment_notice = QLabel("")
        self.ground_alignment_notice.hide()
        self.ground_tab_layout.addWidget(self.ground_alignment_box)
        self.ground_tab_layout.addStretch(1)

        shape_box = QGroupBox("Convex Collision Shape")
        shape_box.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        shape_box.customContextMenuRequested.connect(
            self._show_collision_shape_context_menu)
        shape_layout = QVBoxLayout(shape_box)
        shape_layout.setContentsMargins(6, 4, 6, 4)
        shape_layout.setSpacing(4)
        self.collision_shape_quality = QComboBox()
        self.collision_shape_quality.addItem("Low quality", "low")
        self.collision_shape_quality.addItem("Normal quality", "normal")
        self.collision_shape_quality.addItem("High quality", "high")
        self.collision_shape_quality.setCurrentIndex(1)
        self.generate_collision_shape_button = QPushButton(
            "Generate OpenNeoUA Collision Shape")
        self.generate_collision_shape_button.clicked.connect(
            self.generate_collision_shape)
        self.import_collision_shape_button = QPushButton("Import")
        self.import_collision_shape_button.clicked.connect(
            self.import_collision_shape)
        self.export_collision_shape_button = QPushButton("Export")
        self.export_collision_shape_button.clicked.connect(
            self.export_collision_shape)
        self.delete_collision_shape_button = QPushButton("Delete Collision Shape")
        self.delete_collision_shape_button.clicked.connect(
            self.delete_collision_shape)
        self.delete_collision_shape_button.setEnabled(False)
        shape_buttons = QHBoxLayout()
        shape_buttons.addWidget(self.generate_collision_shape_button, 1)
        shape_buttons.addWidget(self.collision_shape_quality)
        shape_layout.addLayout(shape_buttons)
        shape_io = QHBoxLayout()
        shape_io.addWidget(self.import_collision_shape_button)
        shape_io.addWidget(self.export_collision_shape_button)
        shape_io.addWidget(self.delete_collision_shape_button)
        shape_layout.addLayout(shape_io)
        self.collision_shape_status = QLabel("No convex shape loaded.")
        self.collision_shape_status.setWordWrap(True)
        self.collision_shape_progress = QProgressBar()
        self.collision_shape_progress.setRange(0, 0)
        self.collision_shape_progress.setTextVisible(False)
        self.collision_shape_progress.setFixedHeight(10)
        self.collision_shape_progress.hide()
        shape_layout.addWidget(self.collision_shape_progress)
        shape_layout.addWidget(self.collision_shape_status)
        self.collision_shape_tab_layout.addWidget(shape_box)
        self.collision_shape_box = shape_box

        self.collision_geometry_widget = GeometryPartSelectionWidget(
            self, on_component_highlight=self._set_collision_geometry_preview)
        self.collision_geometry_widget.selectionChanged.connect(
            self._sync_collision_generation_controls)
        self.collision_geometry_widget.selectionChanged.connect(
            self._collision_shape_components_changed)
        self.collision_geometry_widget.shapeHighlightChanged.connect(
            self.viewport.set_shape_selected_indices)
        self.viewport.shapeComponentPicked.connect(
            self.collision_geometry_widget.select_shape_component)
        self.viewport.shapeDeselectRequested.connect(
            self.collision_geometry_widget.deselect_component)
        self.collision_shape_tab_layout.addWidget(
            self.collision_geometry_widget, 1)

        spheres_box = QGroupBox("Spheres")
        spheres_layout = QVBoxLayout(spheres_box)
        spheres_layout.setContentsMargins(6, 4, 6, 4)
        spheres_layout.setSpacing(3)
        self.spheres_box = spheres_box

        sphere_buttons_top = QHBoxLayout()
        sphere_buttons_top.setContentsMargins(0, 0, 0, 0)
        sphere_buttons_top.setSpacing(5)
        self.add_legacy_button = QPushButton("Add Legacy Radius")
        self.add_openneoua_collision_button = QPushButton(
            "Add OpenNeoUA Collision")
        self.add_legacy_button.setStyleSheet(
            _type_button_stylesheet(LEGACY))
        self.add_openneoua_collision_button.setStyleSheet(
            _type_button_stylesheet(OPENNEOUA))
        self.add_legacy_button.clicked.connect(
            self.add_legacy_action.trigger)
        self.add_openneoua_collision_button.clicked.connect(
            self.add_openneoua_action.trigger)
        sphere_buttons_top.addWidget(self.add_legacy_button)
        sphere_buttons_top.addWidget(self.add_openneoua_collision_button)
        spheres_layout.addLayout(sphere_buttons_top)

        self.collision_sphere_count_label = QLabel(
            f"OpenNeoUA Spheres: 0 / {UNIT_COLL_MAX_COUNT}")
        self.collision_sphere_count_label.setAlignment(
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
        self.collision_sphere_count_label.setToolTip(
            "Current OpenNeoUA compound collision spheres and the maximum "
            "supported by the runtime parser.")
        spheres_layout.addWidget(self.collision_sphere_count_label)

        sphere_edit_buttons = QHBoxLayout()
        sphere_edit_buttons.setContentsMargins(0, 0, 0, 0)
        sphere_edit_buttons.setSpacing(5)
        self.duplicate_sphere_button = QPushButton("Duplicate Sphere")
        self.delete_sphere_button = QPushButton("Delete Sphere")
        self.duplicate_sphere_button.clicked.connect(
            self.duplicate_action.trigger)
        self.delete_sphere_button.clicked.connect(self.delete_action.trigger)
        self.generate_collision_spheres_button = QToolButton()
        self.generate_collision_spheres_button.setText(
            "Generate Collision Spheres")
        self.generate_collision_spheres_button.setToolTip(
            "Approximate the selected model with editable OpenNeoUA collision "
            "spheres at the chosen geometric accuracy.")
        self.generate_collision_spheres_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup)
        generate_menu = QMenu(self.generate_collision_spheres_button)
        self._populate_generate_collision_spheres_menu(generate_menu)
        self.generate_collision_spheres_button.setMenu(generate_menu)
        self.generate_collision_spheres_button.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        sphere_edit_buttons.addWidget(self.duplicate_sphere_button, 1)
        sphere_edit_buttons.addWidget(self.delete_sphere_button, 1)
        sphere_edit_buttons.addWidget(
            self.generate_collision_spheres_button, 1)
        spheres_layout.addLayout(sphere_edit_buttons)

        sphere_selection_buttons = QHBoxLayout()
        sphere_selection_buttons.setContentsMargins(0, 0, 0, 0)
        sphere_selection_buttons.setSpacing(5)
        self.select_all_spheres_button = QPushButton("Select All Spheres")
        self.select_all_spheres_button.setToolTip(
            "Select every collision sphere in the current Collision tab.")
        self.select_all_spheres_button.clicked.connect(
            self.select_all_spheres)
        self.hide_spheres_button = QPushButton("Hide Sphere")
        self.hide_spheres_button.setToolTip(
            "Hide the selected collision spheres in the 3D preview. Select "
            "hidden rows in the list to show them again.")
        self.hide_spheres_button.clicked.connect(
            self._toggle_selected_sphere_visibility)
        self.isolate_sphere_button = QPushButton("Isolate Sphere")
        self.isolate_sphere_button.setToolTip(
            "Show only the selected collision sphere. Use Unisolate Sphere "
            "to show all spheres again.")
        self.isolate_sphere_button.clicked.connect(
            self._toggle_selected_sphere_isolation)
        sphere_selection_buttons.addWidget(self.select_all_spheres_button, 1)
        sphere_selection_buttons.addWidget(self.hide_spheres_button, 1)
        sphere_selection_buttons.addWidget(self.isolate_sphere_button, 1)
        spheres_layout.addLayout(sphere_selection_buttons)

        self.hide_all_spheres_button = QPushButton("Hide All Spheres")
        self.hide_all_spheres_button.setToolTip(
            "Hide every collision sphere in the 3D preview. When all spheres "
            "are hidden, the same button becomes Show All Spheres.")
        self.hide_all_spheres_button.clicked.connect(
            self._toggle_all_sphere_visibility)
        spheres_layout.addWidget(self.hide_all_spheres_button)

        sphere_modifier_buttons = QHBoxLayout()
        sphere_modifier_buttons.setContentsMargins(0, 0, 0, 0)
        sphere_modifier_buttons.setSpacing(5)
        self.change_type_button = QToolButton()
        self.change_type_button.setText("Change Sphere Type")
        self.change_type_button.setToolTip(
            "Choose exactly which collision category the selected sphere "
            "should become.")
        self.change_type_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup)
        change_type_button_menu = QMenu(self.change_type_button)
        self._populate_change_type_menu(change_type_button_menu)
        self.change_type_button.setMenu(change_type_button_menu)
        self.change_type_button.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.mirror_sphere_button = QToolButton()
        self.mirror_sphere_button.setText("Mirror Selected Sphere")
        self.mirror_sphere_button.setToolTip(
            "Duplicate the selected compound sphere on the opposite side "
            "of the chosen model axis.")
        self.mirror_sphere_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup)
        mirror_button_menu = QMenu(self.mirror_sphere_button)
        mirror_button_menu.addAction(self.mirror_x_action)
        mirror_button_menu.addAction(self.mirror_y_action)
        mirror_button_menu.addAction(self.mirror_z_action)
        self.mirror_sphere_button.setMenu(mirror_button_menu)
        self.mirror_sphere_button.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        sphere_modifier_buttons.addWidget(self.change_type_button, 1)
        sphere_modifier_buttons.addWidget(self.mirror_sphere_button, 1)
        spheres_layout.addLayout(sphere_modifier_buttons)

        sphere_history_buttons = QHBoxLayout()
        sphere_history_buttons.setContentsMargins(0, 0, 0, 0)
        sphere_history_buttons.setSpacing(5)
        self.undo_button = QPushButton("Undo")
        self.redo_button = QPushButton("Redo")
        self.undo_button.clicked.connect(self.undo_action.trigger)
        self.redo_button.clicked.connect(self.redo_action.trigger)
        sphere_history_buttons.addWidget(self.undo_button, 1)
        sphere_history_buttons.addWidget(self.redo_button, 1)
        spheres_layout.addLayout(sphere_history_buttons)

        self.sphere_tree.setMinimumHeight(82)
        self.sphere_tree.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        spheres_box.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        sphere_header = self.sphere_tree.header()
        sphere_header.setMinimumSectionSize(38)
        # Keep the complete type name readable at the compact pane width.
        # The remaining columns stay available through horizontal scrolling.
        sphere_header.setStretchLastSection(False)
        sphere_header.setSectionResizeMode(
            0, QHeaderView.ResizeMode.Interactive)
        type_width = max(
            self.sphere_tree.fontMetrics().horizontalAdvance(label)
            for label in TYPE_LABELS.values()) + 16
        self.sphere_tree.setColumnWidth(0, type_width)
        self.sphere_tree.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        # Show up to three decimals here while keeping finer imported values
        # in the project and script writer.
        compact_widths = {1: 62, 2: 56, 3: 60, 4: 60, 5: 60}
        for column, width in compact_widths.items():
            sphere_header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.Fixed)
            self.sphere_tree.setColumnWidth(column, width)
            self.sphere_tree.headerItem().setTextAlignment(
                column,
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        sphere_header.setSectionResizeMode(
            6, QHeaderView.ResizeMode.Fixed)
        self.sphere_tree.setColumnWidth(6, 42)
        self.sphere_tree.headerItem().setTextAlignment(
            6, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        spheres_layout.addWidget(self.sphere_tree)

        # Radius belongs to the Spheres workspace rather than being another
        # full-height properties group.  Keep it compact under the sphere list,
        # mirroring the Move Element panel's "control + strength" structure.
        self.radius_title = QLabel("Sphere Radius", spheres_box)
        # Compatibility attribute only.  The Spheres group already provides
        # the section title, so another heading here created a large dead zone
        # between the table and the actual Radius control.
        self.radius_title.hide()
        radius_layout = QHBoxLayout()
        self.radius_layout = radius_layout
        radius_layout.setContentsMargins(0, 0, 0, 0)
        radius_layout.setSpacing(5)
        radius_layout.addWidget(QLabel("Radius"))
        self.radius_slider = QSlider(Qt.Orientation.Horizontal)
        self.radius_slider.setRange(0, _RADIUS_SLIDER_STEPS)
        self.radius_slider.sliderPressed.connect(
            self._begin_radius_slider)
        self.radius_slider.valueChanged.connect(
            self._radius_slider_changed)
        self.radius_slider.sliderReleased.connect(
            self._finish_radius_slider)
        radius_layout.addWidget(self.radius_slider, 1)
        self.radius_spin = self._coordinate_spin()
        self.radius_spin.setMinimum(1.0)
        self.radius_spin.setDecimals(1)
        self.radius_spin.setSingleStep(1.0)
        self.radius_spin.setKeyboardTracking(True)
        self.radius_spin.valueChanged.connect(
            self._radius_spin_changed)
        self.radius_spin.editingFinished.connect(
            self._finish_radius_spin_edit)
        # Reserve enough room for the largest displayed radius and spin
        # arrows; the slider uses the remaining space.
        self.radius_spin.setMinimumWidth(self.radius_spin.sizeHint().width())
        radius_layout.addWidget(self.radius_spin)
        spheres_layout.addLayout(radius_layout)

        self.collision_tab_layout.addWidget(spheres_box, 1)

        self.fire_points_box = QGroupBox("Fire Points")
        self.fire_points_box.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.fire_points_box.customContextMenuRequested.connect(
            self._show_fire_points_context_menu)
        fire_layout = QVBoxLayout(self.fire_points_box)
        fire_layout.setContentsMargins(6, 4, 6, 4)
        fire_layout.setSpacing(3)
        fire_buttons = QHBoxLayout()
        fire_buttons.setContentsMargins(0, 0, 0, 0)
        fire_buttons.setSpacing(5)
        self.add_fire_point_button = QPushButton("Add Fire Point")
        self.add_fire_point_button.setToolTip(
            "Enable the vanilla fire_x/fire_y/fire_z rack or add one more "
            "derived muzzle by increasing num_weapons. Vanilla Fire Points "
            "share one symmetric rack rather than independent offsets.")
        self.add_fire_point_button.clicked.connect(self.add_fire_point)
        self.remove_fire_point_button = QPushButton("Remove")
        self.remove_fire_point_button.setToolTip(
            "Remove one muzzle from the vanilla rack. With multiple muzzles "
            "the remaining Fire Points are re-spaced symmetrically by the "
            "vanilla num_weapons rule; removing the last point disables the "
            "fire-point block.")
        self.remove_fire_point_button.clicked.connect(self.remove_fire_point)
        self.reset_fire_point_button = QPushButton("Reset Position")
        self.reset_fire_point_button.setToolTip(
            "Reset vanilla fire_x/fire_y/fire_z to 0 / 0 / 0. If no Fire "
            "Point is active, this also creates one muzzle at the origin.")
        self.reset_fire_point_button.clicked.connect(
            self._reset_fire_point_position)
        fire_buttons.addWidget(self.add_fire_point_button, 1)
        fire_buttons.addWidget(self.remove_fire_point_button, 1)
        fire_buttons.addWidget(self.reset_fire_point_button, 1)
        fire_layout.addLayout(fire_buttons)
        fire_grid = QGridLayout()
        fire_grid.setHorizontalSpacing(5)
        fire_grid.setVerticalSpacing(3)
        self.fire_point_spins = {}
        for index, axis in enumerate(("X", "Y", "Z")):
            column = index * 2
            fire_grid.addWidget(QLabel(axis), 0, column)
            spin = CompactScaleSpinBox()
            spin.setRange(-1_000_000.0, 1_000_000.0)
            spin.setDecimals(3)
            spin.setSingleStep(1.0)
            spin.setKeyboardTracking(True)
            spin.setMinimumWidth(72)
            spin.setToolTip(
                f"Vanilla fire_{axis.lower()} projectile spawn offset.")
            key = f"fire_{axis.lower()}"
            spin.valueChanged.connect(
                lambda value, field=key:
                self._fire_point_value_changed(field, value))
            spin.editingFinished.connect(
                lambda field=key: self._finish_vehicle_preview_edit(field))
            self.fire_point_spins[axis.lower()] = spin
            fire_grid.addWidget(spin, 0, column + 1)
        self.fire_x_spin = self.fire_point_spins["x"]
        self.fire_y_spin = self.fire_point_spins["y"]
        self.fire_z_spin = self.fire_point_spins["z"]
        fire_grid.addWidget(QLabel("Weapons Min"), 1, 0)
        self.num_weapons_spin = QSpinBox()
        self.num_weapons_spin.setRange(0, 255)
        self.num_weapons_spin.setValue(1)
        self.num_weapons_spin.setMinimumWidth(72)
        self.num_weapons_spin.setToolTip(
            "OpenNeoUA num_weapons lower endpoint. Set Min = Max for a fixed "
            "count; Min < Max exports the inclusive min_max range. Fixed 0 "
            "keeps vanilla one-projectile semantics.")
        self.num_weapons_spin.valueChanged.connect(
            self._num_weapons_changed)
        self.num_weapons_spin.editingFinished.connect(
            lambda: self._finish_vehicle_preview_edit("num_weapons"))
        fire_grid.addWidget(self.num_weapons_spin, 1, 1, 1, 2)
        fire_grid.addWidget(QLabel("Weapons Max"), 1, 3)
        self.num_weapons_max_spin = QSpinBox()
        self.num_weapons_max_spin.setRange(0, 255)
        self.num_weapons_max_spin.setValue(1)
        self.num_weapons_max_spin.setMinimumWidth(72)
        self.num_weapons_max_spin.setToolTip(
            "OpenNeoUA num_weapons upper endpoint. The 3D Fire Point preview "
            "shows the largest possible salvo; lower random rolls are re-spaced "
            "by the runtime using the same vanilla symmetric rack rule.")
        self.num_weapons_max_spin.valueChanged.connect(
            self._num_weapons_max_changed)
        self.num_weapons_max_spin.editingFinished.connect(
            lambda: self._finish_vehicle_preview_edit("num_weapons_max"))
        fire_grid.addWidget(self.num_weapons_max_spin, 1, 4, 1, 2)
        for column in (1, 3, 5):
            fire_grid.setColumnStretch(column, 1)
        fire_layout.addLayout(fire_grid)
        self.fire_point_notice = QLabel("")
        self.fire_point_notice.hide()
        self.fire_point_tree.setMinimumHeight(82)
        self.fire_point_tree.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        fire_header = self.fire_point_tree.header()
        fire_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            fire_header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents)
            self.fire_point_tree.headerItem().setTextAlignment(
                column, Qt.AlignmentFlag.AlignRight
                | Qt.AlignmentFlag.AlignVCenter)
        fire_layout.addWidget(self.fire_point_tree, 1)
        self.fire_points_box.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.fire_points_tab_layout.addWidget(self.fire_points_box, 1)

        self.gun_points_box = QGroupBox("Gun Points")
        self.gun_points_box.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.gun_points_box.customContextMenuRequested.connect(
            self._show_gun_points_context_menu)
        gun_layout = QVBoxLayout(self.gun_points_box)
        gun_layout.setContentsMargins(6, 4, 6, 4)
        gun_layout.setSpacing(3)

        gun_family_row = QHBoxLayout()
        gun_family_row.setContentsMargins(0, 0, 0, 0)
        gun_family_row.setSpacing(5)
        gun_family_row.addWidget(QLabel("Gun Point type"))
        self.gun_point_type_combo = QComboBox()
        self.gun_point_type_combo.addItem(
            "Vanilla (Robo only)", "robo")
        self.gun_point_type_combo.addItem(
            "OpenNeoUA (All vehicle classes)", "unit")
        self.gun_point_type_combo.setCurrentIndex(1)
        self.gun_point_type_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.gun_point_type_combo.setMinimumContentsLength(12)
        self.gun_point_type_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.gun_point_type_combo.setToolTip(
            "Chooses the script family for the selected Gun Point, converting "
            "it immediately when one is selected. With no point selected it "
            "sets the family used by newly-added Gun Points. Vanilla writes "
            "robo_* and is intended for Host Station/Robo classes; OpenNeoUA "
            "writes generic unit_* and works on all vehicle classes.")
        self.gun_point_type_combo.currentIndexChanged.connect(
            self._new_gun_point_scheme_changed)
        gun_family_row.addWidget(self.gun_point_type_combo, 1)
        gun_layout.addLayout(gun_family_row)

        gun_buttons = QGridLayout()
        gun_buttons.setHorizontalSpacing(5)
        gun_buttons.setVerticalSpacing(3)
        self.add_gun_point_button = QPushButton("Add Gun Point")
        self.add_gun_point_button.setToolTip(
            "Add a real vehicle-side gun mount using the Gun Point type "
            "selected above.")
        self.add_gun_point_button.clicked.connect(self.add_gun_point)
        self.remove_gun_point_button = QPushButton("Remove")
        self.remove_gun_point_button.clicked.connect(self.remove_gun_point)
        self.reset_gun_point_button = QPushButton("Reset Position")
        self.reset_gun_point_button.setToolTip(
            "Reset the selected Gun Point position to 0 / 0 / 0 without "
            "changing its direction, type, name or script family.")
        self.reset_gun_point_button.clicked.connect(
            self._reset_gun_point_position)
        self.reset_all_gun_points_button = QPushButton("Reset All")
        self.reset_all_gun_points_button.setToolTip(
            "Restore every Gun Point and all referenced turret angles to the "
            "values loaded from the current script. If the loaded source "
            "contained no Gun Points, remove all points authored after load.")
        self.reset_all_gun_points_button.clicked.connect(
            self._reset_all_gun_points)
        gun_buttons.addWidget(self.add_gun_point_button, 0, 0)
        gun_buttons.addWidget(self.remove_gun_point_button, 0, 1)
        gun_buttons.addWidget(self.reset_gun_point_button, 1, 0)
        gun_buttons.addWidget(self.reset_all_gun_points_button, 1, 1)
        gun_buttons.setColumnStretch(0, 1)
        gun_buttons.setColumnStretch(1, 1)
        gun_layout.addLayout(gun_buttons)

        self.gun_family_value = QLabel("No point selected")
        self.gun_family_value.setStyleSheet(
            "color: rgb(178, 78, 238); font-size: 10px;")
        self.gun_family_value.setToolTip(
            "Shows the actual script family of the selected point. Changing "
            "Gun Point type converts between legacy robo_* and generic unit_* "
            "syntax and the exported script follows the new family.")
        gun_layout.addWidget(self.gun_family_value)

        # The right pane is now deliberately wider, so Gun Point authoring can
        # stay compact without truncating signed coordinates: X/Y/Z share one
        # row, direction shares a second row, and Vehicle/Name share a third.
        # Dir Y remains intentionally non-authorable; imported values are
        # preserved internally and on rewrite.
        gun_fields_grid = QGridLayout()
        self.gun_fields_grid = gun_fields_grid
        gun_fields_grid.setHorizontalSpacing(5)
        gun_fields_grid.setVerticalSpacing(3)
        self.gun_point_spins = {}
        self.gun_dir_spins = {}

        for pair, axis in enumerate(("X", "Y", "Z")):
            label_column = pair * 2
            field_column = label_column + 1
            gun_fields_grid.addWidget(QLabel(axis), 0, label_column)
            spin = CompactScaleSpinBox()
            spin.setRange(-1_000_000.0, 1_000_000.0)
            spin.setDecimals(3)
            spin.setSingleStep(1.0)
            spin.setKeyboardTracking(True)
            spin.setMinimumWidth(82)
            spin.setMaximumWidth(132)
            spin.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            spin.setToolTip(f"Gun mount local {axis} position.")
            field = axis.lower()
            spin.valueChanged.connect(
                lambda value, name=field:
                self._gun_point_value_changed(name, value))
            spin.editingFinished.connect(
                lambda name=field: self._finish_vehicle_preview_edit(
                    f"gun_{name}"))
            self.gun_point_spins[field] = spin
            gun_fields_grid.addWidget(spin, 0, field_column)
            gun_fields_grid.setColumnStretch(field_column, 1)

        for pair, axis in enumerate(("X", "Z")):
            label_column = pair * 2
            field_column = label_column + 1
            gun_fields_grid.addWidget(QLabel(f"Dir {axis}"), 1, label_column)
            dir_spin = CompactScaleSpinBox()
            dir_spin.setRange(-1.0, 1.0)
            dir_spin.setDecimals(0)
            dir_spin.setSingleStep(1.0)
            dir_spin.setKeyboardTracking(True)
            dir_spin.setMinimumWidth(82)
            dir_spin.setMaximumWidth(132)
            dir_spin.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            dir_spin.setToolTip(
                f"Gun mount local direction {axis}: canonical values -1, 0 or 1. "
                "Dir Y is intentionally not authorable in this editor.")
            dir_field = f"dir_{axis.lower()}"
            dir_spin.valueChanged.connect(
                lambda value, name=dir_field:
                self._gun_point_value_changed(name, value))
            dir_spin.editingFinished.connect(
                lambda name=dir_field: self._finish_vehicle_preview_edit(
                    f"gun_{name}"))
            self.gun_dir_spins[axis.lower()] = dir_spin
            gun_fields_grid.addWidget(dir_spin, 1, field_column)

        gun_fields_grid.addWidget(QLabel("Direction"), 2, 0)
        self.gun_direction_slider = QSlider(Qt.Orientation.Horizontal)
        self.gun_direction_slider.setRange(0, len(GUN_DIRECTION_SEQUENCE) - 1)
        self.gun_direction_slider.setSingleStep(1)
        self.gun_direction_slider.setPageStep(1)
        self.gun_direction_slider.setTickInterval(1)
        self.gun_direction_slider.setTickPosition(
            QSlider.TickPosition.TicksBelow)
        self.gun_direction_slider.setToolTip(
            "Rotate gun_dir through adjacent X/Z directions: +Z, +X/+Z, +X, "
            "+X/-Z, -Z, -X/-Z, -X, -X/+Z. The 3D envelope updates live.")
        self.gun_direction_slider.sliderPressed.connect(
            self._begin_gun_direction_slider)
        self.gun_direction_slider.valueChanged.connect(
            self._gun_direction_slider_changed)
        self.gun_direction_slider.sliderReleased.connect(
            self._finish_gun_direction_slider)
        gun_fields_grid.addWidget(self.gun_direction_slider, 2, 1, 1, 4)
        self.gun_direction_value = QLabel("")
        self.gun_direction_value.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.gun_direction_value.setMinimumWidth(108)
        self.gun_direction_value.setStyleSheet(
            "font-size: 10px; color: #aeb6c2;")
        gun_fields_grid.addWidget(self.gun_direction_value, 2, 5)

        gun_fields_grid.addWidget(QLabel("Vehicle"), 3, 0)
        self.gun_type_spin = QSpinBox()
        self.gun_type_spin.setRange(0, 255)
        self.gun_type_spin.setMinimumWidth(82)
        self.gun_type_spin.setMaximumWidth(132)
        self.gun_type_spin.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.gun_type_spin.setToolTip(
            "Vehicle prototype ID used for the mounted gun/flak.")
        self.gun_type_spin.valueChanged.connect(self._gun_type_changed)
        self.gun_type_spin.editingFinished.connect(
            lambda: self._finish_vehicle_preview_edit("gun_type"))
        gun_fields_grid.addWidget(self.gun_type_spin, 3, 1)

        gun_fields_grid.addWidget(QLabel("Name"), 3, 2)
        self.gun_name_edit = QLineEdit()
        self.gun_name_edit.setPlaceholderText("optional")
        self.gun_name_edit.setToolTip(
            "When the referenced Vehicle is loaded, this shows its script "
            "name read-only. Otherwise edit robo_gun_name / unit_gun_name "
            "for this mount.")
        self.gun_name_edit.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.gun_name_edit.editingFinished.connect(self._gun_name_changed)
        gun_fields_grid.addWidget(self.gun_name_edit, 3, 3, 1, 3)
        gun_layout.addLayout(gun_fields_grid)

        self.turret_limits_group = QGroupBox("Turret Limits (referenced Vehicle)")
        turret_layout = QGridLayout(self.turret_limits_group)
        turret_layout.setContentsMargins(6, 4, 6, 4)
        turret_layout.setHorizontalSpacing(6)
        turret_layout.setVerticalSpacing(3)
        self.turret_limits_source = QLabel("Select a Gun Point")
        self.turret_limits_source.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.turret_limits_source.setStyleSheet(
            "font-size: 10px; color: #aeb6c2;")
        turret_layout.addWidget(self.turret_limits_source, 0, 0, 1, 4)
        self.turret_limit_sliders = {}
        self.turret_limit_spins = {}
        angle_specs = (
            ("side", "Side", TURRET_SIDE_COLOR),
            ("up", "Up", TURRET_UP_COLOR),
            ("down", "Down", TURRET_DOWN_COLOR),
        )
        for row, (field, label, color) in enumerate(angle_specs, 1):
            title = QLabel(label)
            title.setStyleSheet(
                f"color: rgb({color.red()}, {color.green()}, {color.blue()});")
            turret_layout.addWidget(title, row, 0)
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(0, _gun_angle_max(field))
            slider.setSingleStep(10)
            slider.setPageStep(100)
            slider.setToolTip(
                f"gun_{field}_angle in radians x 1000. The 3D arc updates "
                "while this slider moves.")
            slider.sliderPressed.connect(
                lambda name=field: self._begin_turret_limit_slider(name))
            slider.valueChanged.connect(
                lambda value, name=field:
                self._turret_limit_value_changed(
                    name, value, from_slider=True))
            slider.sliderReleased.connect(
                lambda name=field: self._finish_turret_limit_slider(name))
            turret_layout.addWidget(slider, row, 1, 1, 2)
            spin = QSpinBox()
            spin.setRange(0, _gun_angle_max(field))
            spin.setSingleStep(10)
            spin.setKeyboardTracking(True)
            spin.setMinimumWidth(76)
            spin.setMaximumWidth(104)
            spin.setToolTip(
                f"Exact gun_{field}_angle script value (radians x 1000).")
            spin.valueChanged.connect(
                lambda value, name=field:
                self._turret_limit_value_changed(name, value))
            spin.editingFinished.connect(
                lambda name=field:
                self._finish_vehicle_preview_edit(f"turret_{name}"))
            turret_layout.addWidget(spin, row, 3)
            self.turret_limit_sliders[field] = slider
            self.turret_limit_spins[field] = spin
        turret_layout.setColumnStretch(1, 1)
        turret_layout.setColumnStretch(2, 1)
        self.turret_limits_effective = QLabel("")
        self.turret_limits_effective.setWordWrap(True)
        self.turret_limits_effective.setStyleSheet(
            "font-size: 10px; color: #aeb6c2;")
        turret_layout.addWidget(self.turret_limits_effective, 4, 0, 1, 4)
        gun_layout.addWidget(self.turret_limits_group)

        self.gun_point_tree.setMinimumHeight(82)
        self.gun_point_tree.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        gun_header = self.gun_point_tree.header()
        gun_header.setStretchLastSection(False)
        for column, width in enumerate(GUN_POINT_TABLE_BASE_WIDTHS):
            gun_header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
            self.gun_point_tree.setColumnWidth(column, width)
            self.gun_point_tree.headerItem().setTextAlignment(
                column, (Qt.AlignmentFlag.AlignLeft if column in (0, 1)
                         else Qt.AlignmentFlag.AlignRight)
                | Qt.AlignmentFlag.AlignVCenter)
        self.gun_point_tree.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.gun_point_tree.viewport().installEventFilter(self)
        self._layout_gun_point_columns(
            self.gun_point_tree.viewport().width())
        gun_layout.addWidget(self.gun_point_tree, 1)
        self.gun_points_box.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.gun_points_tab_layout.addWidget(self.gun_points_box, 1)

        self.cockpit_box = QGroupBox("Cockpit View")
        self.cockpit_box.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.cockpit_box.customContextMenuRequested.connect(
            self._show_cockpit_context_menu)
        cockpit_layout = QVBoxLayout(self.cockpit_box)
        cockpit_layout.setContentsMargins(6, 4, 6, 4)
        cockpit_layout.setSpacing(3)

        self.cockpit_camera_button = QPushButton("Enable Cockpit Camera Offset")
        self.cockpit_camera_button.setToolTip(
            "Enable the OpenNeoUA cockpit-camera authoring workspace. Until this "
            "button is pressed, no cockpit parameters are written and the "
            "cockpit preview remains inactive. Imported scripts that already "
            "contain cockpit offsets enable it automatically.")
        self.cockpit_camera_button.clicked.connect(
            self._toggle_cockpit_camera)
        cockpit_layout.addWidget(self.cockpit_camera_button)

        cockpit_preview_grid = QGridLayout()
        cockpit_preview_grid.setHorizontalSpacing(6)
        cockpit_preview_grid.setVerticalSpacing(2)
        cockpit_preview_grid.addWidget(QLabel("Runtime model"), 0, 0)
        self.cockpit_model_state_combo = QComboBox()
        self.cockpit_model_state_combo.addItem(
            "Player / normal (vp_normal)", "normal")
        self.cockpit_model_state_combo.addItem(
            "Idle / stationary (vp_wait)", "idle")
        self.cockpit_model_state_combo.setToolTip(
            "Preview-only VP state. OpenNeoUA can render vp_wait while the "
            "vehicle is idle and vp_normal while moving; this selector never "
            "changes script data.")
        self.cockpit_model_state_combo.currentIndexChanged.connect(
            self._cockpit_preview_setting_changed)
        cockpit_preview_grid.addWidget(self.cockpit_model_state_combo, 0, 1)

        cockpit_preview_grid.addWidget(QLabel("Runtime aspect"), 1, 0)
        self.cockpit_runtime_aspect_combo = QComboBox()
        self.cockpit_runtime_aspect_combo.addItem(
            "4:3 (default / vanilla)", 4.0 / 3.0)
        self.cockpit_runtime_aspect_combo.addItem("16:10", 16.0 / 10.0)
        self.cockpit_runtime_aspect_combo.addItem("16:9", 16.0 / 9.0)
        self.cockpit_runtime_aspect_combo.setToolTip(
            "Logical OpenNeoUA graphics aspect used by matrixAspectCorrection(). "
            "4:3 is the vanilla-safe default and matches a 640x480 vid.def. "
            "Changing it updates only the live preview; cockpit X/Y/Z output "
            "is never modified.")
        self.cockpit_runtime_aspect_combo.currentIndexChanged.connect(
            self._cockpit_preview_setting_changed)
        cockpit_preview_grid.addWidget(
            self.cockpit_runtime_aspect_combo, 1, 1)
        cockpit_preview_grid.setColumnStretch(1, 1)
        cockpit_layout.addLayout(cockpit_preview_grid)

        self.cockpit_preview_info = QLabel("")
        self.cockpit_preview_info.setWordWrap(True)
        self.cockpit_preview_info.setStyleSheet(
            "font-size: 10px; color: #aeb6c2;")
        cockpit_layout.addWidget(self.cockpit_preview_info)

        self.cockpit_controls = QWidget()
        cockpit_controls_layout = QVBoxLayout(self.cockpit_controls)
        cockpit_controls_layout.setContentsMargins(0, 0, 0, 0)
        cockpit_controls_layout.setSpacing(3)

        cockpit_grid = QGridLayout()
        cockpit_grid.setHorizontalSpacing(6)
        cockpit_grid.setVerticalSpacing(3)
        self.cockpit_camera_spins = {}
        for column, axis in enumerate(("X", "Y", "Z")):
            cockpit_grid.addWidget(QLabel(axis), 0, column)
            spin = CompactScaleSpinBox()
            spin.setRange(-1_000_000.0, 1_000_000.0)
            spin.setDecimals(3)
            spin.setSingleStep(1.0)
            spin.setKeyboardTracking(True)
            spin.setMinimumWidth(88)
            spin.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            spin.setToolTip(
                f"{axis} of OpenNeoUA cockpit_camera_offset_xyz in "
                "vehicle-local coordinates.")
            field = axis.lower()
            spin.valueChanged.connect(
                lambda value, name=field:
                self._cockpit_value_changed(name, value))
            spin.editingFinished.connect(
                lambda name=field:
                self._finish_vehicle_preview_edit(f"cockpit_{name}"))
            self.cockpit_camera_spins[field] = spin
            cockpit_grid.addWidget(spin, 1, column)
            cockpit_grid.setColumnStretch(column, 1)
        self.cockpit_x_spin = self.cockpit_camera_spins["x"]
        self.cockpit_y_spin = self.cockpit_camera_spins["y"]
        self.cockpit_z_spin = self.cockpit_camera_spins["z"]
        cockpit_controls_layout.addLayout(cockpit_grid)

        cockpit_buttons = QHBoxLayout()
        self.reset_cockpit_button = QPushButton("Reset Position to 0 / 0 / 0")
        self.reset_cockpit_button.setToolTip(
            "Reset the active cockpit camera position to the vehicle origin. "
            "Use Disable Cockpit Camera Offset to remove the parameters.")
        self.reset_cockpit_button.clicked.connect(self._reset_cockpit_camera)
        cockpit_buttons.addWidget(self.reset_cockpit_button)
        self.restore_script_cockpit_button = QPushButton(
            "Restore Script Position")
        self.restore_script_cockpit_button.setToolTip(
            "Restore the cockpit X/Y/Z coordinates read from the last loaded "
            "or imported vehicle script. Disabled when that script did not "
            "contain cockpit_camera_offset_xyz.")
        self.restore_script_cockpit_button.clicked.connect(
            self._restore_loaded_cockpit_camera)
        cockpit_buttons.addWidget(self.restore_script_cockpit_button)
        cockpit_controls_layout.addLayout(cockpit_buttons)

        # This output is display-only and always contains at most the three
        # cockpit offset lines. A QLabel is a better fit than a scrollable
        # text editor here: it gives deterministic centered text and cannot
        # expose an unnecessary horizontal scrollbar/focus underline.
        self.cockpit_output = QLabel()
        self.cockpit_output.setTextFormat(Qt.TextFormat.PlainText)
        self.cockpit_output.setAlignment(
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
        self.cockpit_output.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.cockpit_output.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.cockpit_output.setFrameShape(QFrame.Shape.StyledPanel)
        self.cockpit_output.setFrameShadow(QFrame.Shadow.Sunken)
        self.cockpit_output.setMinimumHeight(56)
        self.cockpit_output.setMaximumHeight(72)
        self.cockpit_output.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        cockpit_controls_layout.addWidget(self.cockpit_output)
        cockpit_layout.addWidget(self.cockpit_controls)
        cockpit_layout.addStretch(1)
        self.cockpit_box.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.cockpit_tab_layout.addWidget(self.cockpit_box, 1)

        self.type_value = QLabel("None", self)
        self.index_value = QLabel("None", self)
        self.selected_element_label = QLabel("None", self)
        self.visible_check = QCheckBox("Active / Visible", self)
        self.visible_check.toggled.connect(self._visibility_changed)
        self.runtime_radius_value = QLabel("", self)
        for control in (self.type_value, self.index_value, self.selected_element_label,
                        self.visible_check, self.runtime_radius_value):
            control.hide()

        transform_box = QGroupBox("Move Element")
        self.transform_box = transform_box
        transform_box.setToolTip(
            "Move the selected compound sphere or Fire Point. "
            "With the viewport focused: Left/Right move on X, Up/Down move "
            "on Z, and Page Up/Page Down move on Y. Every key press uses "
            "the current Move strength value.")
        transform_layout = QVBoxLayout(transform_box)
        transform_layout.setContentsMargins(6, 5, 6, 5)
        transform_layout.setSpacing(4)
        self.gizmo = CollisionMoveGizmo()
        self.gizmo.setMinimumSize(300, 150)
        self.gizmo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.gizmo.directionTriggered.connect(self._gizmo_nudge)
        transform_layout.addWidget(self.gizmo, 1)
        self.gizmo_view_hint = QLabel(
            "Drag empty gizmo space to rotate its controls only. "
            "Double-click empty space to realign.")
        self.gizmo_view_hint.setWordWrap(True)
        self.gizmo_view_hint.setProperty("studioRole", "hint")
        transform_layout.addWidget(self.gizmo_view_hint)
        strength_row = QHBoxLayout()
        strength_row.addWidget(QLabel("Move strength"))
        self.move_strength_slider = QSlider(Qt.Orientation.Horizontal)
        self.move_strength_slider.setRange(1, 500)
        self.move_strength_slider.setValue(1)
        self.move_strength_spin = QSpinBox()
        self.move_strength_spin.setRange(1, 1_000_000)
        self.move_strength_spin.setValue(1)
        # Leave enough room for the full 1,000,000 range plus the
        # QSpinBox step buttons; the old 58-72 px box clipped typed values.
        self.move_strength_spin.setMinimumWidth(96)
        self.move_strength_spin.setMaximumWidth(112)
        self.move_strength_value = self.move_strength_spin
        self.move_strength_slider.valueChanged.connect(
            self._move_strength_slider_changed)
        self.move_strength_spin.valueChanged.connect(
            self._move_strength_spin_changed)
        strength_row.addWidget(self.move_strength_slider, 1)
        strength_row.addWidget(self.move_strength_spin)
        transform_layout.addLayout(strength_row)
        # Fixed workspace height keeps the gizmo identical when switching
        # tabs; only the properties area above grows/shrinks with the window.
        transform_box.setFixedHeight(248)
        transform_box.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        # One shared gizmo is reused by every domain tab.  Keeping it directly
        # below the tabs avoids four duplicate widgets and uses the newly freed
        # lower-right area for a much larger, easier-to-read control.
        right.addWidget(transform_box, 0)
        properties_scroll = QScrollArea()
        properties_scroll.setWidgetResizable(True)
        properties_scroll.setFrameShape(QFrame.Shape.NoFrame)
        # Keep the authoring column compact and horizontally stable. Lists may
        # still scroll internally, while the outer pane is allowed to scroll
        # vertically when the active workspace is taller than the window.
        properties_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Keep the properties pane vertically scrollable when the current
        # workspace is taller than the available window.  Do not pin the
        # scrollbar: Qt also uses it internally to bring focused controls and
        # tab pages into view, so forcing it back to zero breaks normal tab
        # interaction and mouse-wheel navigation.
        properties_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        properties_scroll_bar = properties_scroll.verticalScrollBar()
        properties_scroll_bar.setEnabled(
            properties_scroll_bar.maximum() > properties_scroll_bar.minimum())
        properties_scroll_bar.rangeChanged.connect(
            lambda minimum, maximum, bar=properties_scroll_bar:
            bar.setEnabled(maximum > minimum))
        # Keep the authoring column at the user-validated compact width while
        # still leaving room for all four full tab labels and controls.
        # The user can continue widening it manually when desired.
        properties_scroll.setMinimumWidth(420)
        properties_scroll.setMaximumWidth(760)
        properties.setMinimumWidth(0)
        properties.setMinimumHeight(0)
        properties.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        properties_scroll.setWidget(properties)
        self.properties_scroll = properties_scroll
        splitter.addWidget(properties_scroll)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        # Start at the user-validated splitter proportions: the archive list is
        # wide enough for full resource names, while the properties column is
        # compact and the viewport remains the flexible central workspace.
        splitter.setSizes([230, 850, 430])

    def eventFilter(self, watched, event):
        if (self._hide_splitter_grip
                and event.type() == QEvent.Type.Paint
                and watched is self.main_splitter.handle(2)):
            return True
        if (hasattr(self, "gun_point_tree")
                and watched is self.gun_point_tree.viewport()
                and event.type() == QEvent.Type.Resize):
            self._layout_gun_point_columns(event.size().width())
        return super().eventFilter(watched, event)

    def _layout_gun_point_columns(self, available_width: int) -> None:
        """Fit every Gun Point column while preserving useful proportions."""

        base_total = sum(GUN_POINT_TABLE_BASE_WIDTHS)
        available = max(base_total, int(available_width))
        extra = available - base_total
        widths = [
            base + extra * growth // 100
            for base, growth in zip(
                GUN_POINT_TABLE_BASE_WIDTHS, GUN_POINT_TABLE_GROWTH)
        ]
        widths[-1] += available - sum(widths)
        for column, width in enumerate(widths):
            self.gun_point_tree.setColumnWidth(column, width)

    def _fit_initial_window_to_screen(self) -> None:
        """Keep the initial editor window inside the usable desktop area."""

        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            self.resize(1100, 720)
            return

        available = screen.availableGeometry()
        # Qt reports availableGeometry in device-independent pixels, so this
        # also fixes high-DPI setups where a fixed 1100px logical window can
        # otherwise extend beyond the physical desktop.
        target_width = min(1500, max(1100, int(available.width() * 0.96)))
        target_height = min(900, max(720, int(available.height() * 0.94)))
        target_width = min(target_width, available.width())
        target_height = min(target_height, available.height())
        self.resize(target_width, target_height)

    @staticmethod
    def _coordinate_spin() -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(-1000000.0, 1000000.0)
        spin.setDecimals(4)
        spin.setSingleStep(1.0)
        return spin

    @staticmethod
    def _radius_to_slider(radius: float) -> int:
        radius = max(10 ** _RADIUS_LOG_MIN, min(
            10 ** _RADIUS_LOG_MAX, float(radius)))
        ratio = (
            (math.log10(radius) - _RADIUS_LOG_MIN)
            / (_RADIUS_LOG_MAX - _RADIUS_LOG_MIN)
        )
        return round(ratio * _RADIUS_SLIDER_STEPS)

    @staticmethod
    def _slider_to_radius(value: int) -> float:
        ratio = max(0.0, min(1.0, value / _RADIUS_SLIDER_STEPS))
        exponent = (
            _RADIUS_LOG_MIN
            + ratio * (_RADIUS_LOG_MAX - _RADIUS_LOG_MIN)
        )
        return 10 ** exponent

    def _push_undo(self):
        state = self.project.snapshot()
        if not self._undo or self._undo[-1][0] != state:
            self._undo.append((state, self._sphere_selection_state()))
            if len(self._undo) > 100:
                del self._undo[0]
        self._redo.clear()

    def _sphere_selection_state(self) -> tuple[int, tuple[int, ...]]:
        return self._selected, tuple(sorted(self._selected_sphere_indices()))

    def _restore_sphere_selection_state(self, state) -> None:
        selected, indices = state
        count = len(self.project.spheres())
        valid = {
            int(index) for index in indices
            if isinstance(index, int) and 0 <= index < count
        }
        self._selected_spheres = valid
        self._selected = (
            int(selected) if selected in valid
            else min(valid, default=-1))

    def _active_internal_base_name(self) -> str:
        """Return the BASE backing the primary vehicle/model when known."""

        vp_id = None
        if self._active_model_reference is not None:
            vp_id = self._active_model_reference.vp_normal
        elif hasattr(self, "model_tree"):
            item = self.model_tree.currentItem()
            if item is not None:
                vp_ids = item.data(0, _MODEL_VP_ROLE) or ()
                if vp_ids:
                    vp_id = int(vp_ids[0])
        if self._vp_table is not None and vp_id is not None \
                and 0 <= vp_id < len(self._vp_table.entries):
            return self._vp_table.entry(vp_id).base_name
        if self._active_base_path is not None \
                and self._active_base_path.name.casefold() != "set.bas":
            return self._active_base_path.name
        return ""

    def _update_window_title(self) -> None:
        parts = [WINDOW_TITLE]
        if self._active_script_path is not None and self.project.name.strip():
            parts.append(self.project.name.strip())

        internal_base = self._active_internal_base_name()
        if internal_base:
            parts.append(internal_base)

        if self._active_base_path is not None:
            full_path = self._active_base_path.expanduser().resolve(
                strict=False)
            if (not internal_base
                    or internal_base.casefold() != full_path.name.casefold()):
                parts.append(full_path.name)
            parts.append(str(full_path))
        elif self._active_script_path is not None:
            full_script = self._active_script_path.expanduser().resolve(
                strict=False)
            parts.append(str(full_script))

        self.setWindowTitle(
            ("* " if self._modified else "") + " - ".join(parts))

    def _set_modified(self, modified=True):
        self._modified = bool(modified)
        if not self._modified:
            self._export_baseline = self.project.snapshot()
        self._update_window_title()
        browser = getattr(self, 'game_object_browser', None)
        if browser is not None:
            from studio.ui.browsers import GAME_OBJECT_ROLE
            from studio.ui.widgets import mark_resource_modified
            for index in range(browser.tree.topLevelItemCount()):
                item = browser.tree.topLevelItem(index)
                obj = item.data(0, GAME_OBJECT_ROLE)
                matching = (self._active_script_id == obj.object_id
                            and self._active_script_kind.endswith('_' + obj.kind)
                            and self._active_script_path == obj.source)
                mark_resource_modified(item, self._modified and matching)

    def _mark_collision_shape_exported(self) -> None:
        baseline = CollisionProject()
        baseline.restore(self._export_baseline)
        for name in (
                "source_model", "source_base",
                "collision_shape", "collision_shape_file_path",
                "collision_shape_owners", "collision_shape_components",
                "collision_shape_warnings", "model_scale_x", "model_scale_y", "model_scale_z",
                "model_rotation_x", "model_rotation_y", "model_rotation_z"):
            setattr(baseline, name, getattr(self.project, name))
        # File export does not apply a linked unit's shape path or sphere removal to its script.
        if self._active_model_reference is None:
            baseline.collision_shape_path = self.project.collision_shape_path
            if not self.project.spheres():
                baseline.legacy = None
                baseline.compound = []
                baseline.sphere_isolation_active = self.project.sphere_isolation_active
        self._export_baseline = baseline.snapshot()
        self._set_modified(self.project.snapshot() != self._export_baseline)

    def _unsaved_changes_message(self, action) -> str:
        baseline = CollisionProject()
        baseline.restore(self._export_baseline)
        if (self.project.collision_shape is not None
                and self.project.collision_shape == baseline.collision_shape
                and self.project.collision_shape_file_path):
            return ("The collision shape file is saved, but other project or script "
                    f"changes are unsaved. {action} anyway?")
        return f"The collision project has unexported changes. {action} anyway?"

    def undo(self):
        if not self._undo:
            return
        self._radius_spin_active = False
        self._vehicle_preview_active_edits.clear()
        self._turret_slider_active_edits.clear()
        self._redo.append((
            self.project.snapshot(), self._sphere_selection_state()))
        state, selection_state = self._undo.pop()
        self.project.restore(state)
        self._restore_sphere_selection_state(selection_state)
        self._selected = min(
            self._selected, len(self.project.spheres()) - 1)
        self._selected_gun_point = min(
            self._selected_gun_point, len(self.project.gun_points) - 1)
        self._shape_preview_settings()
        self._set_modified(self.project.snapshot() != self._export_baseline)
        self._sync_all()

    def redo(self):
        if not self._redo:
            return
        self._radius_spin_active = False
        self._vehicle_preview_active_edits.clear()
        self._turret_slider_active_edits.clear()
        self._undo.append((
            self.project.snapshot(), self._sphere_selection_state()))
        state, selection_state = self._redo.pop()
        self.project.restore(state)
        self._restore_sphere_selection_state(selection_state)
        self._selected = min(
            self._selected, len(self.project.spheres()) - 1)
        self._selected_gun_point = min(
            self._selected_gun_point, len(self.project.gun_points) - 1)
        self._shape_preview_settings()
        self._set_modified(self.project.snapshot() != self._export_baseline)
        self._sync_all()

    def _sync_gizmo_camera(self):
        if self._is_cockpit_tab_active():
            # Keep the cockpit *camera* fixed to OpenNeoUA semantics, but do not
            # force the gizmo into that front-on projection: +Z/-Z would sit
            # on top of each other and become awkward to click.  The gizmo is
            # only a control surface, so start from the familiar oblique view
            # and let the user orbit it independently by dragging empty space.
            self.gizmo.set_camera_orientation(-35.0, 20.0)
        else:
            self.gizmo.set_camera_orientation(
                self.viewport._yaw, self.viewport._pitch)

    def _on_manual_camera_changed(self):
        with QSignalBlocker(self.toolbar_view_preset_combo):
            self.toolbar_view_preset_combo.setCurrentText("Current View")
        self._sync_gizmo_camera()
        self._update_reset_view_controls()

    def _on_view_preset_changed(self, preset: str):
        if preset != "Current View":
            self.viewport.apply_view_preset(
                preset, self.viewport.size(), 100)
        self._sync_gizmo_camera()
        self._update_reset_view_controls()

    def _reset_view(self):
        if not self.viewport.can_reset_camera:
            self._update_reset_view_controls()
            return
        self.viewport.reset_view()
        with QSignalBlocker(self.toolbar_view_preset_combo):
            self.toolbar_view_preset_combo.setCurrentText("Current View")
        self._sync_gizmo_camera()
        self._update_reset_view_controls()

    def _update_reset_view_controls(self) -> None:
        """Mirror Model Editor Reset View enable/disable semantics."""

        enabled = (
            self.viewport.can_reset_camera
            and not self._is_cockpit_tab_active()
        )
        if hasattr(self, "reset_view_action"):
            self.reset_view_action.setEnabled(enabled)
        if hasattr(self, "toolbar_reset_view_button"):
            self.toolbar_reset_view_button.setEnabled(enabled)

    def _toggle_play(self, playing: bool) -> None:
        """Use AssetViewport's shared VANM playback implementation."""

        self.viewport.play_animation(playing)
        self.play_button.setText("Pause" if playing else "Play")
        self.statusBar().showMessage(
            "Animation playing." if playing else "Animation paused.", 3500)

    def _on_animation_speed_changed(self, speed: float) -> None:
        self.viewport.set_animation_speed(speed)
        self.statusBar().showMessage(
            f"Animation speed set to {speed:.2f}x.", 3000)

    def _on_animation_frame_changed(self, text: str) -> None:
        # While playing, the per-frame detail only floods the status bar.
        if self.viewport._anim_playing:
            if self.statusBar().currentMessage() != "Animation playing.":
                self.statusBar().showMessage("Animation playing.")
        elif text:
            self.statusBar().showMessage(text, 2500)

    def _sync_animation_controls(self) -> None:
        """Keep Entity Designer animation controls honest after reloads."""

        if not hasattr(self, "play_button"):
            return
        has_anim = self.viewport.has_animation
        if self._is_fx_tab_active():
            unit = self.fx_panel.unit
            owners = {self._owner_for_vp(ref.vp) for ref in (unit.normal, unit.dead, unit.megadeth)
                      if ref.vp > 0}
            owners.add(self._current_owner)
            has_anim = any(self.viewport._materials[face.material].anim_frames
                           for face in self.viewport._faces if face.owner in owners)
        # Destruction FX: the animation starts playing by itself on entering
        # the tab or switching unit; the user may still pause it afterwards.
        if not self._is_fx_tab_active():
            self._fx_autoplay_key = None
        elif has_anim:
            key = (self._active_script_path, self._active_script_kind,
                   self._active_script_id, self._current_owner)
            if key != getattr(self, "_fx_autoplay_key", None):
                self._fx_autoplay_key = key
                with QSignalBlocker(self.play_button):
                    self.play_button.setChecked(True)
        self.play_button.setEnabled(has_anim)
        self.step_button.setEnabled(has_anim)
        self.speed_spin.setEnabled(has_anim)
        if not has_anim:
            with QSignalBlocker(self.play_button):
                self.play_button.setChecked(False)
            self.play_button.setText("Play")
            self.viewport.play_animation(False)
            if self.statusBar().currentMessage() == "Animation playing.":
                self.statusBar().clearMessage()
            return
        self.play_button.setText(
            "Pause" if self.play_button.isChecked() else "Play")
        self.viewport.play_animation(self.play_button.isChecked())

    def open_base_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import BASE / SET.BAS / 3DS", str(self._last_directory),
            "Urban Assault assets (*.base *.bas *.BASE *.BAS *.3ds *.3DS);;"
            "All files (*)")
        if path:
            from studio.ui.widgets import loading_resources
            selected = Path(path)
            label = ("Loading BAS archive. Please wait…" if selected.suffix.casefold() == ".bas"
                     else "Importing model. Please wait…")
            with loading_resources(self, label):
                self.open_base(selected)

    def _set_model_browser_enabled(self, enabled: bool) -> None:
        """Enable model browsing only for the normal archive/SKLT workflow."""

        self._model_browser_enabled = bool(enabled)
        self._apply_model_browser_state()

    def _set_script_unit_mode(self, active: bool) -> None:
        """Switch the left list between archive models and script units.

        Script mode starts only after a script import and lists every loadable
        definition of the source script, so moving to another vehicle or
        weapon never needs a second import.
        """

        self._script_unit_mode = bool(active)
        if hasattr(self, "model_tree"):
            self.model_tree.setHeaderLabels(
                ["Unit", "VP"] if self._script_unit_mode
                else ["Internal path", "VP"])
            self.model_tree.headerItem().setTextAlignment(
                1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if hasattr(self, "model_search"):
            self.model_search.setPlaceholderText(
                "Search vehicle / weapon names" if self._script_unit_mode
                else "Search model names")
            self.model_search.setToolTip(
                "Filter the source script units by vehicle/weapon name."
                if self._script_unit_mode else
                "Filter models by name, internal path or owner path.")
        self._apply_model_browser_state()

    def _apply_model_browser_state(self) -> None:
        """One place for the left list's enabled state and its help text."""

        if self._script_unit_mode:
            enabled = True
            tree_tooltip = (
                "Select a vehicle or weapon to load its collision data from "
                "the source script.")
        elif self._model_browser_enabled:
            enabled = True
            tree_tooltip = "Browse models from the imported BAS/SKLT resource."
        else:
            enabled = False
            tree_tooltip = (
                "Model selection is locked while a script-imported vehicle "
                "or weapon is active. Import a BAS archive or SKLT normally "
                "to browse this list.")
        if hasattr(self, "model_tree"):
            self.model_tree.setEnabled(enabled)
            self.model_tree.setToolTip(tree_tooltip)
        if hasattr(self, "model_search"):
            self.model_search.setEnabled(enabled)

    def _current_visual_resource_is_open(self) -> bool:
        return bool(self.family is not None or self._active_base_path is not None)

    def _sync_close_archive_action(self) -> None:
        action = getattr(self, "close_bas_archive_action", None)
        if action is not None:
            action.setEnabled(self._current_visual_resource_is_open())

    def close_current_archive(self) -> None:
        """Close the current resource and clear its whole authoring session.

        Collision data belongs to the unit/model currently being edited.
        Closing that resource must therefore clear the linked script, spheres,
        shape, Fire Points, Gun Points, cockpit data and undo/redo history too.
        """

        if not self.fx_panel.confirm_text_changes():
            return
        if not self._current_visual_resource_is_open():
            self._sync_close_archive_action()
            return

        process = self._collision_bake_process
        if process is not None:
            self._collision_bake_cancelled = True
            process.kill()
            process.waitForFinished(3000)
            if self._collision_bake_process is process:
                self._collision_bake_process = None
            process.deleteLater()
        self._cleanup_collision_bake_tempdir()
        self._collision_bake_context = {}
        self._collision_bake_cancelled = False

        self.viewport.clear()
        self.family = None
        self._vp_embedded = None
        self._vp_table = None
        self._vp_table_source = ""
        self._active_base_path = None
        self._current_owner = None
        self._current_owner_base_bounds = None
        self._viewport_owner = None
        self._clear_script_link()

        self.project = CollisionProject()
        self._new_gun_point_scheme = "unit"
        self._capture_loaded_gun_points()
        self._capture_loaded_cockpit_camera()
        self._capture_tab_reset_baseline()
        self._selected = -1
        self._selected_spheres.clear()
        self._selected_fire_point = -1
        self._selected_gun_point = -1
        self._undo.clear()
        self._redo.clear()
        self._vehicle_preview_active_edits.clear()
        self._turret_slider_active_edits.clear()
        self._gun_direction_slider_active = False

        self._collision_geometry_cache_key = None
        self._collision_geometry_components = ()
        self._collision_geometry_triangles_by_owner = {}
        self._collision_geometry_owners = ()
        self._collision_geometry_fingerprints.clear()
        widget = getattr(self, "collision_geometry_widget", None)
        if widget is not None:
            widget.set_geometry(None, None, (), {}, None)
        self._set_collision_geometry_preview(None)

        self.model_tree.clear()
        self._model_info_by_vp = {}
        self._script_unit_references = []
        self._script_unit_visual_reference = None
        self._set_script_unit_mode(False)
        if hasattr(self, "model_search"):
            self.model_search.clear()
        self.source_label.setText("No source loaded.")
        self._set_model_browser_enabled(False)
        self._set_modified(False)
        self._sync_animation_controls()
        self._sync_all()
        self._update_window_title()
        self._sync_close_archive_action()
        self.statusBar().showMessage(
            "Current resource closed. Related Entity Designer data cleared.",
            5000)

    def _current_set_bas_path(self) -> Path | None:
        if self._vp_embedded is None:
            return None
        return Path(self._vp_embedded.source_path)

    def _clear_script_link(self) -> None:
        self._active_script_path = None
        if hasattr(self, "fx_panel"):
            self.fx_panel.set_context(fx.CONTEXT_VEHICLE, fx.FxDocument())
        self._active_script_kind = ""
        self._active_script_id = None
        self._active_model_reference = None
        self._active_visual_reference = None
        self._loaded_gun_points_enabled = False
        self._loaded_gun_points = []
        self._loaded_unit_gun_default_icon = ""
        self._loaded_gun_point_scheme = "unit"
        self._loaded_turret_limits = {}
        self._loaded_cockpit_camera_enabled = False
        self._loaded_cockpit_camera_offset = (0.0, 0.0, 0.0)

    def reload_game_resources(self):
        from studio.assets.catalog import clear_visual_cache
        clear_visual_cache()
        self.game_object_browser.reload()

    def _game_set_changed(self, number):
        from studio.ui.widgets import loading_resources
        with loading_resources(self, f"Loading Set {number}. Please wait…"):
            if self._load_game_set(number):
                from studio.ui.shell import remember_set
                remember_set(number)

    def _load_game_set(self, number):
        current = self.game_object_browser.tree.currentItem()
        if current is not None:
            from studio.ui.browsers import GAME_OBJECT_ROLE
            obj = current.data(0, GAME_OBJECT_ROLE)
            if obj is not None:
                return self._open_game_object(obj)
        return True

    def _open_game_object(self, obj):
        if self._modified and not self._confirm_script_unit_switch():
            return False
        try:
            from studio.assets.catalog import set_assets
            assets = set_assets(self.game_object_browser.set_number)
            result = self.open_vehicle_script(obj.source, object_id=obj.object_id,
                object_kind=f"new_{obj.kind}", set_bas_path=assets.setbas_path,
                resolved_text=obj.effective_text)
            return result is not False
        except (OSError, ValueError, RuntimeError) as exc:
            QMessageBox.warning(self, "Game object unavailable", str(exc))
            return False

    def open_vehicle_script_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import vehicle / weapon definition",
            str(self._last_directory),
            OPENNEOUA_SCRIPT_FILE_FILTER)
        if path:
            self.open_vehicle_script(path)

    def open_vehicle_script(
            self, path: str | Path, *, vehicle_id: int | None = None,
            object_id: int | None = None, object_kind: str | None = None,
            set_bas_path: str | Path | None = None,
            resolved_text: str | None = None) -> bool:
        """Load one ``new_vehicle`` or ``new_weapon`` through ``vp_normal``.

        ``vehicle_id`` remains as a compatibility argument and implies
        ``new_vehicle``. New callers may use ``object_id`` plus ``object_kind``.
        The linked asset and script are read-only until an explicit Apply or
        Overwrite action is confirmed.
        """

        script_path = Path(path)
        if not self.fx_panel.confirm_text_changes():
            return False
        try:
            text, _encoding, _bom = read_script_file(script_path)
            if resolved_text is not None:
                text = resolved_text
            self._fx_source_from_catalog = resolved_text is not None
            references = script_model_references(text)
            script_turret_limits = import_turret_limits(text)
        except (OSError, UnicodeError, CollisionScriptError) as exc:
            QMessageBox.critical(
                self, "Script object load failed", str(exc))
            return False
        if not references:
            QMessageBox.warning(
                self, "No resolvable definition",
                "No complete new_vehicle or new_weapon block with an active "
                "vp_normal was found. modify_* inheritance is not guessed.")
            return False

        reference = None
        requested_id = object_id
        requested_kind = object_kind.lower() if object_kind else None
        if vehicle_id is not None:
            requested_id = int(vehicle_id)
            requested_kind = "new_vehicle"
        if requested_id is not None:
            matches = [
                candidate for candidate in references
                if candidate.block.object_id == int(requested_id)
                and (requested_kind is None
                     or candidate.block.kind == requested_kind)
            ]
            if len(matches) != 1:
                kind_text = requested_kind or "new_vehicle/new_weapon"
                QMessageBox.warning(
                    self, "Definition not found",
                    f"Expected exactly one {kind_text} {requested_id} with "
                    "vp_normal.")
                return False
            reference = matches[0]

        current_set = self._current_set_bas_path()
        chosen_set = Path(set_bas_path) if set_bas_path is not None else current_set
        if reference is None or (
                reference.vp_normal is not None and chosen_set is None
                and not (reference.three_ds_normal or reference.base_normal)):
            dialog = OpenScriptObjectDialog(
                self, script_path, references, current_set)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return False
            reference, chosen_set = dialog.selected()
        visual_source, override, visual_override, visual_warnings = (
            _resolve_script_visual_source(
                reference, script_path, chosen_set, current_set))
        active_visual_reference = _reference_for_visual_source(
            reference, visual_source)
        chosen_asset: Path
        if visual_source in {"3ds_normal", "base_normal"}:
            if override is None:
                QMessageBox.critical(
                    self, "Script visual unavailable",
                    "The selected script visual could not be resolved.")
                return False
            chosen_asset = override
            if (self._active_base_path is None
                    or self._active_base_path.resolve(strict=False)
                    != chosen_asset.resolve(strict=False)):
                if not self.open_base(chosen_asset):
                    return False
            source_model = chosen_asset.stem
        else:
            if visual_source == "unavailable":
                QMessageBox.critical(
                    self, "Visual source unavailable",
                    "No usable 3DS, BASE, or SET VP visual source was found. "
                    + "\n".join(visual_warnings))
                return False
            if chosen_set is None:
                fallback_set, _ = QFileDialog.getOpenFileName(
                    self, "Select SET.BAS for vp_normal fallback",
                    str(script_path.parent),
                    "SET.BAS archive (SET.BAS *.bas *.BAS);;All files (*)")
                if not fallback_set:
                    return False
                chosen_set = Path(fallback_set)
            chosen_set = Path(chosen_set)
            if (current_set is None
                    or current_set.resolve(strict=False)
                    != chosen_set.resolve(strict=False)):
                if not self.open_base(chosen_set):
                    return False
            if self._vp_embedded is None or self._vp_table is None:
                QMessageBox.critical(
                    self, "VP database unavailable",
                    "The selected archive did not expose a SET.BAS VP table.")
                return False
            if not 0 <= reference.vp_normal < len(self._vp_table.entries):
                QMessageBox.critical(
                    self, "VP out of range",
                    f"vp_normal {reference.vp_normal} is outside the SET.BAS "
                    f"database (0..{len(self._vp_table.entries) - 1}).")
                return False
            model_info = self._model_info_by_vp.get(reference.vp_normal)
            if model_info is None:
                entry = self._vp_table.entry(reference.vp_normal)
                QMessageBox.critical(
                    self, "VP model unavailable",
                    f"VP {reference.vp_normal} resolves to "
                    f"{entry.base_name}, but its SKLT could not be selected "
                    "from the loaded SET.BAS family. The active VP table "
                    "may reference a loose BASE that is not embedded in this SET.")
                return False
            _owner_path, source_model = model_info
            chosen_asset = chosen_set

        block = reference.block
        target_category = (
            WEAPON if block.kind == "new_weapon" else VEHICLE)
        try:
            legacy, compounds, warnings = import_collision_block(
                text, block, OPENNEOUA)
            warnings.extend(visual_warnings)
            shape_path = import_collision_shape_reference(text, block)
            if target_category == VEHICLE:
                overeof_enabled, overeof = import_overeof_block(text, block)
                (fire_enabled, fire_x, fire_y, fire_z,
                 num_weapons, num_weapons_max) = import_fire_points_block(
                    text, block)
                (cockpit_enabled, cockpit_x, cockpit_y,
                 cockpit_z) = import_cockpit_camera_block(text, block)
                (gun_enabled, gun_points,
                 unit_gun_default_icon) = import_gun_points_block(text, block)
            else:
                overeof_enabled, overeof = False, 0.0
                fire_enabled, fire_x, fire_y, fire_z = False, 0.0, 0.0, 0.0
                num_weapons, num_weapons_max = 1, 1
                cockpit_enabled, cockpit_x, cockpit_y, cockpit_z = (
                    False, 0.0, 0.0, 0.0)
                gun_enabled, gun_points, unit_gun_default_icon = False, [], ""
        except CollisionScriptError as exc:
            QMessageBox.critical(
                self, "Script parameter import failed", str(exc))
            return False

        fallback_name = (
            "Weapon" if target_category == WEAPON else "Vehicle")
        shape = None
        shape_file_path = ""
        if shape_path:
            shape_file = _resolve_data_asset_path(
                shape_path, script_path, chosen_asset, chosen_set)
            if shape_file is None:
                warnings.append(
                    f"collision_shape file was not found: {shape_path}")
            else:
                try:
                    shape = read_collision_shape(shape_file)
                    shape_file_path = str(shape_file)
                except (OSError, ValueError) as exc:
                    warnings.append(
                        f"collision_shape could not be read: {exc}")

        self.project = CollisionProject(
            name=block.name or f"{fallback_name}_{block.object_id}",
            source_model=source_model,
            source_base=chosen_asset.name,
            target_category=target_category,
            model_scale_x=reference.scale_x,
            model_scale_y=reference.scale_y,
            model_scale_z=reference.scale_z,
            model_rotation_x=reference.rotation_x,
            model_rotation_y=reference.rotation_y,
            model_rotation_z=reference.rotation_z,
            overeof_enabled=overeof_enabled,
            overeof=overeof,
            fire_points_enabled=fire_enabled,
            fire_x=fire_x,
            fire_y=fire_y,
            fire_z=fire_z,
            num_weapons=num_weapons,
            num_weapons_max=num_weapons_max,
            gun_points_enabled=gun_enabled,
            gun_points=gun_points,
            unit_gun_default_icon=unit_gun_default_icon,
            turret_limits=script_turret_limits if target_category == VEHICLE else {},
            cockpit_camera_enabled=cockpit_enabled,
            cockpit_camera_offset_x=cockpit_x,
            cockpit_camera_offset_y=cockpit_y,
            cockpit_camera_offset_z=cockpit_z,
            briefing_size_enabled=import_briefing_size_block(text, block)[0] if target_category == VEHICLE else False,
            briefing_size=import_briefing_size_block(text, block)[1] if target_category == VEHICLE else 0.0,
            legacy=legacy,
            compound=compounds,
            collision_shape=shape,
            collision_shape_path=shape_path,
            collision_shape_file_path=shape_file_path,
        )
        imported_gun_schemes = gun_point_families_in_block(text, block)
        if len(imported_gun_schemes) == 1:
            # A script that already uses one family supplies the missing
            # semantic context that a bare SET.BAS model cannot provide.
            self._new_gun_point_scheme = next(iter(imported_gun_schemes))
        elif "unit" in imported_gun_schemes:
            # Mixed scripts remain mixed. Prefer the generic OpenNeoUA family for
            # a brand-new point until the user selects a specific existing one.
            self._new_gun_point_scheme = "unit"
        self._capture_loaded_gun_points()
        self._capture_loaded_cockpit_camera()
        self._capture_tab_reset_baseline()
        self._active_script_path = script_path
        self._active_script_kind = block.kind
        self._active_script_id = block.object_id
        self._active_model_reference = reference
        self._active_visual_reference = active_visual_reference
        self._active_base_path = chosen_asset
        if hasattr(self, "cockpit_model_state_combo"):
            with QSignalBlocker(self.cockpit_model_state_combo):
                self.cockpit_model_state_combo.setCurrentIndex(0)
        self._selected = 0 if self.project.spheres() else -1
        self._selected_spheres = (
            {self._selected} if self._selected >= 0 else set())
        self._selected_fire_point = -1
        self._selected_gun_point = -1
        self._undo.clear()
        self._redo.clear()
        self._last_directory = script_path.parent
        # The left list becomes the unit browser of this script: every other
        # loadable definition is one click away, without importing again.
        self._set_script_unit_mode(True)
        resolved_unit_reference = (
            active_visual_reference
            if active_visual_reference != reference else None)
        self._fill_script_units(
            references, active_visual_reference=resolved_unit_reference)
        self._show_model_item(self._select_script_unit_item(reference))
        self._load_fx_context(text, block, reference)
        self._set_modified(False)
        self.source_label.setText(
            (f"Visual override: {visual_override}" if visual_override
             else f"VP {reference.vp_normal}: {source_model or '<model>'}")
            + "\n"
            f"{script_path.name} — {block.kind} {block.object_id}")
        self._sync_all()
        message = (
            f"Loaded {block.kind} {block.object_id} using "
            f"{visual_override if visual_override else 'VP ' + str(reference.vp_normal)} "
            f"with preview scale {_number(reference.scale_x)}/"
            f"{_number(reference.scale_y)}/{_number(reference.scale_z)}.")
        if warnings:
            message += " " + " ".join(warnings)
        self.statusBar().showMessage(message, 7000)
        return True

    def _cockpit_requested_vp(self) -> tuple[int | None, str]:
        reference = self._active_model_reference
        if reference is None or self.project.target_category != VEHICLE:
            return None, ""
        if reference.three_ds_normal or reference.base_normal:
            return None, ""
        state = (
            self.cockpit_model_state_combo.currentData()
            if hasattr(self, "cockpit_model_state_combo") else "normal")
        if state == "idle" and reference.vp_wait is not None:
            return reference.vp_wait, "vp_wait"
        return reference.vp_normal, "vp_normal"

    def _sync_cockpit_preview_model(self) -> None:
        """Load the runtime VP state without changing authored project data."""

        if self.family is None:
            if hasattr(self, "cockpit_preview_info"):
                self.cockpit_preview_info.setText("")
            return

        cockpit_active = self._is_cockpit_tab_active()
        desired_owner = self._current_owner
        info = ""
        if cockpit_active and self._active_model_reference is not None:
            vp_id, state_name = self._cockpit_requested_vp()
            owner = self._owner_for_vp(vp_id) if vp_id is not None else None
            if owner is None and state_name == "vp_wait":
                vp_id = self._active_model_reference.vp_normal
                state_name = "vp_normal fallback"
                owner = self._owner_for_vp(vp_id)
            if owner is not None:
                desired_owner = owner
                base_name = ""
                if (self._vp_table is not None and vp_id is not None
                        and 0 <= vp_id < len(self._vp_table.entries)):
                    base_name = self._vp_table.entry(vp_id).base_name
                info = f"Preview: {state_name} {vp_id}"
                if base_name:
                    info += f" — {base_name}"
            else:
                info = (
                    "Preview: script VP unavailable in the loaded archive; "
                    "using the selected model.")
        elif cockpit_active:
            info = (
                "Preview: selected model. Import a vehicle script to switch "
                "between its vp_wait and vp_normal states automatically.")

        if desired_owner is not None and desired_owner != self._viewport_owner:
            self.viewport.load_family(
                self.family, {desired_owner}, keep_camera=True,
                primary_owner=desired_owner)
            self.viewport.set_model_preview_scale(
                self.project.model_scale_x,
                self.project.model_scale_y,
                self.project.model_scale_z)
            self.viewport.set_ground_alignment(
                self.project.target_category == VEHICLE,
                self.project.overeof_enabled,
                self.project.overeof)
            self._viewport_owner = desired_owner
            self._sync_animation_controls()

        if hasattr(self, "cockpit_preview_info"):
            self.cockpit_preview_info.setText(info)

    def _cockpit_preview_setting_changed(self, *_args) -> None:
        if hasattr(self, "cockpit_runtime_aspect_combo"):
            self.viewport.set_cockpit_runtime_aspect(
                self.cockpit_runtime_aspect_combo.currentData())
        self._sync_cockpit_preview_model()

    def overwrite_loaded_script(self):
        if (self._active_script_path is None
                or self._active_script_id is None
                or not self._active_script_kind):
            QMessageBox.information(
                self, "No linked script definition",
                "Import a vehicle or weapon from a script first. For an "
                "arbitrary target, use Apply to Script.")
            return
        expected_category = (
            WEAPON if "weapon" in self._active_script_kind else VEHICLE)
        if self.project.target_category != expected_category:
            QMessageBox.warning(
                self, "Target category mismatch",
                "The linked script definition and current project category "
                "do not match.")
            return
        if (self.project.collision_shape is not None
                and not self.project.collision_shape_file_path):
            QMessageBox.warning(
                self, "Export Collision Shape first",
                "The current Collision Shape was generated but has not been "
                "exported yet. It cannot be applied to the linked vehicle or "
                "weapon until it has been exported.\n\nExport the Collision "
                "Shape first and then use Overwrite to Existing Script, or "
                "delete the Collision Shape if you do not want to use it.")
            return
        if not self._validate_for_output():
            return
        dialog = ApplyScriptDialog(
            self, self.project,
            replace_all_managed=True,
            initial_path=self._active_script_path,
            initial_kind=self._active_script_kind,
            initial_id=self._active_script_id,
            changed_groups=self._changed_script_tabs(),
        )
        dialog.path_edit.setReadOnly(True)
        dialog.search_edit.setEnabled(False)
        dialog.block_combo.setEnabled(False)
        dialog.kind_combo.setEnabled(False)
        dialog.id_spin.setEnabled(False)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            baseline_refreshed = self._refresh_linked_script_baseline(
                dialog.applied_groups)
            if not baseline_refreshed:
                self.statusBar().showMessage(
                    "Definition overwritten, but the source baseline could "
                    "not be refreshed. Reload the script before the next "
                    "overwrite.", 9000)
                return
            remaining = self._changed_script_tabs()
            if remaining:
                names = ", ".join(
                    _SCRIPT_TAB_LABELS[group]
                    for group in _SCRIPT_TAB_ORDER if group in remaining)
                self.statusBar().showMessage(
                    "Definition overwritten. Still different from the script: "
                    f"{names}.", 9000)
            else:
                self.statusBar().showMessage(
                    "Loaded definition overwritten.", 7000)

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
            self.collision_shape_progress.setVisible(
                process_running and not self._collision_bake_cancelled)
            if widget is not None:
                widget.setEnabled(not process_running)

    def _collision_shape_blocked_for_weapon(self, warn: bool = False) -> bool:
        """Collision Shape is a vehicle-only tool: weapons never get one."""

        blocked = self.project.target_category == WEAPON
        if blocked and warn:
            QMessageBox.warning(
                self, "Collision Shape not available for weapons",
                "Collision Shape is intended exclusively for creating "
                "shapes for vehicles, not weapons.")
        return blocked

    def _sync_collision_shape_weapon_block(self) -> None:
        """In the Collision Shape tab of a weapon, hide the model and the panel."""

        if not hasattr(self, "shape_weapon_notice"):
            return
        blocked = self._collision_shape_blocked_for_weapon()
        on_tab = (self.properties_tabs.currentIndex()
                  == self.collision_shape_tab_index)
        self.collision_shape_box.setVisible(not blocked)
        self.collision_geometry_widget.setVisible(not blocked)
        self.export_collision_shape_action.setEnabled(
            not blocked and self.export_collision_shape_button.isEnabled())
        show_notice = blocked and on_tab
        # No resize grip next to the empty weapon panel. QSplitter re-shows
        # its handles on every layout, so the handle is made inert and its
        # painting is suppressed (see eventFilter) instead of hiding it.
        handle = self.main_splitter.handle(2)
        if not self._splitter_grip_filtered:
            handle.installEventFilter(self)
            self._splitter_grip_filtered = True
        handle.setEnabled(not show_notice)
        self._hide_splitter_grip = show_notice
        handle.update()
        self.shape_weapon_notice.setVisible(show_notice)
        if show_notice:
            self.viewport.setVisible(False)
        elif self.properties_tabs.currentIndex() != self.briefing_tab_index:
            self.viewport.setVisible(True)

    def generate_collision_shape(self) -> None:
        if self._collision_shape_blocked_for_weapon(warn=True):
            return
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
        if self._collision_shape_blocked_for_weapon(warn=True):
            return
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
        if self._collision_shape_blocked_for_weapon(warn=True):
            return
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


    def open_base(self, path: str | Path):
        if not self.fx_panel.confirm_text_changes():
            return False
        self._clear_script_link()
        path = Path(path)
        embedded = None
        vp_table = None
        vp_source = ""
        vp_warnings: list[str] = []
        if path.suffix.casefold() == ".3ds":
            try:
                family = _load_3ds_family(path)
            except Exception as exc:
                QMessageBox.critical(
                    self, "3DS load failed",
                    f"No file was modified.\n\n{exc}")
                return False
            self.family = family
            self._vp_embedded = None
            self._vp_table = None
            self._vp_table_source = ""
            self._active_base_path = path
            self._last_directory = path.parent
            self.project.source_base = path.name
            self._fill_models(family)
            self.source_label.setText(
                f"{path.name}\n{len(family.all_objects()) - 1} mesh parts; "
                "geometry-only preview through AssetViewport")
            self._set_model_browser_enabled(True)
            self._sync_close_archive_action()
            self._sync_animation_controls()
            self._sync_all()
            self._set_modified()
            return True
        if path.name.casefold() == "set.bas":
            try:
                embedded = reconstruct_embedded_vps(path)
                vp_table, vp_source, vp_warnings = runtime_vp_table(
                    path, embedded)
            except (VPEmbeddedError, OSError, ValueError,
                    CollisionScriptError) as exc:
                QMessageBox.critical(
                    self, "VP database unavailable",
                    "SET.BAS was found, but its visual-prototype table could "
                    f"not be reconstructed.\n\n{exc}")
                return False
        try:
            family = _public_dependency(
                "load_asset_family", load_asset_family)(path)
        except Exception as exc:
            QMessageBox.critical(
                self, "Load failed", f"No file was modified.\n\n{exc}")
            return False
        if not any(obj.skeleton is not None for obj in family.all_objects()):
            QMessageBox.warning(
                self, "No models",
                "The BASE was parsed, but no SKLT model could be resolved.")
        self.family = family
        self._vp_embedded = embedded
        self._vp_table = vp_table
        self._vp_table_source = vp_source
        self._active_base_path = path
        self._last_directory = path.parent
        self.project.source_base = path.name
        self._fill_models(family)
        if path.suffix.casefold() == '.bas':
            from studio.ui.tabs import sync_archive_set_selector
            sync_archive_set_selector(self.game_object_browser.set_combo, path)
            self.game_object_browser.select_object(None)
        self.source_label.setText(
            f"{path.name}\n"
            f"{len([o for o in family.all_objects() if o.skeleton])} models")
        self._set_model_browser_enabled(True)
        if vp_warnings:
            self.statusBar().showMessage(" ".join(vp_warnings), 9000)
        self._set_modified()
        self._sync_close_archive_action()
        return True

    def open_sklt_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import external SKLT", str(self._last_directory),
            "Skeletons (*.sklt *.skl *.SKLT *.SKL);;All files (*)")
        if path:
            self.open_sklt(path)

    def open_sklt(self, path: str | Path):
        if not self.fx_panel.confirm_text_changes():
            return False
        self._clear_script_link()
        try:
            family = _public_dependency(
                "load_manual_family", load_manual_family)(path, [], [])
        except Exception as exc:
            QMessageBox.critical(
                self, "Load failed", f"No file was modified.\n\n{exc}")
            return
        if not any(obj.skeleton is not None for obj in family.all_objects()):
            QMessageBox.warning(
                self, "SKLT parse failed",
                "\n".join(family.warnings) or "The model could not be loaded.")
            return
        self.family = family
        self._vp_embedded = None
        self._vp_table = None
        self._vp_table_source = ""
        self._active_base_path = Path(path)
        self._last_directory = Path(path).parent
        self.project.source_base = ""
        self._fill_models(family)
        self.source_label.setText(
            f"{Path(path).name}\nExternal SKLT; geometry-only unless its "
            "material mapping is supplied by a BASE.")
        self._set_model_browser_enabled(True)
        self._set_modified()
        self._sync_close_archive_action()

    def _fill_models(self, family: AssetFamily):
        self._set_script_unit_mode(False)
        self.model_tree.clear()
        self._model_info_by_vp = {}
        self._script_unit_references = []
        self._script_unit_visual_reference = None
        model_index = 0
        vp_ids_by_offset: dict[int, list[int]] = {}
        vp_ids_by_skeleton: dict[str, list[int]] = {}
        if self._vp_embedded is not None and self._vp_table is not None:
            embedded_by_base: dict[str, list] = {}
            for entry in self._vp_embedded.entries:
                embedded_by_base.setdefault(
                    normalize_base_name(entry.base_name), []).append(entry)
            for active_entry in self._vp_table.entries:
                candidates = embedded_by_base.get(
                    normalize_base_name(active_entry.base_name), [])
                selected_entry = None
                # Repeated dummy/BASE assignments are common.  Preserve the
                # positional match when the active table and embedded table
                # agree at this slot; otherwise accept only a unique match.
                if active_entry.index < len(self._vp_embedded.entries):
                    positional = self._vp_embedded.entries[active_entry.index]
                    if (normalize_base_name(positional.base_name)
                            == normalize_base_name(active_entry.base_name)):
                        selected_entry = positional
                if selected_entry is None and len(candidates) == 1:
                    selected_entry = candidates[0]
                if selected_entry is None:
                    continue
                if selected_entry.source_objt_offset >= 0:
                    vp_ids_by_offset.setdefault(
                        selected_entry.source_objt_offset, []).append(
                            active_entry.index)
                key = normalize_skeleton_name(selected_entry.skeleton_name)
                if key:
                    vp_ids_by_skeleton.setdefault(key, []).append(
                        active_entry.index)
        for obj in family.all_objects():
            if obj.skeleton is None:
                continue
            source_offset = int(getattr(
                obj.base_object, "source_objt_offset", -1))
            vp_ids = list(vp_ids_by_offset.get(source_offset, []))
            if not vp_ids and not vp_ids_by_offset:
                skeleton_key = normalize_skeleton_name(
                    obj.base_object.skeleton_name or "")
                candidates = vp_ids_by_skeleton.get(skeleton_key, [])
                if candidates:
                    vp_ids = list(dict.fromkeys(candidates))
            if not vp_ids and self._vp_embedded is None:
                vp_ids = [model_index]
            vp_ids = sorted(set(vp_ids))
            exact_vp = bool(vp_ids)
            if exact_vp:
                # First family object wins a VP, matching the first-match
                # lookup previously used to resolve script imports.
                for vp_id in vp_ids:
                    self._model_info_by_vp.setdefault(
                        vp_id, (obj.owner_path, obj.display_name))
            vp_text = ", ".join(map(str, vp_ids)) if vp_ids else "—"
            item = QTreeWidgetItem([
                obj.base_object.skeleton_name or obj.owner_path,
                vp_text,
            ])
            full_path = obj.base_object.skeleton_name or obj.owner_path
            item.setData(0, Qt.ItemDataRole.UserRole, obj.owner_path)
            item.setData(0, _MODEL_NAME_ROLE, obj.display_name)
            item.setData(
                0, _MODEL_VP_ROLE, tuple(vp_ids) if exact_vp else ())
            item.setToolTip(0, full_path)
            item.setToolTip(
                1, f"VP {vp_text}" if exact_vp
                else "Child model without its own VP slot")
            item.setTextAlignment(
                1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.model_tree.addTopLevelItem(item)
            model_index += 1
        self.model_tree.resizeColumnToContents(1)
        self._filter_models(self.model_search.text())
        for index in range(self.model_tree.topLevelItemCount()):
            item = self.model_tree.topLevelItem(index)
            if not item.isHidden():
                self.model_tree.setCurrentItem(item)
                break

    def _filter_models(self, text: str) -> None:
        self._model_filter_expansion = filter_asset_tree(
            self.model_tree, text, self._model_item_search_metadata,
            self._model_filter_expansion)

    @staticmethod
    def _model_item_search_metadata(item) -> str:
        display_name = item.data(0, _MODEL_NAME_ROLE) or ""
        owner_path = item.data(0, Qt.ItemDataRole.UserRole) or ""
        reference = item.data(0, _SCRIPT_REFERENCE_ROLE)
        unit_label = reference.label if reference is not None else ""
        return f"{display_name} {owner_path} {unit_label}"

    def _fill_script_units(
            self, references: list[VehicleModelReference], *,
            active_visual_reference: VehicleModelReference | None = None
            ) -> None:
        """List the loadable units of the source script in the left panel.

        Rows are rebuilt only when the content really changes: rebuilding the
        same list while Qt is still handling the click that selected a unit
        would reset the scroll position and leave the selection on an
        unrelated row. Signals stay blocked during a rebuild so filling the
        list can never trigger a unit load on its own.
        """

        if (references == self._script_unit_references
                and active_visual_reference
                == self._script_unit_visual_reference):
            return
        self._script_unit_references = list(references)
        self._script_unit_visual_reference = active_visual_reference
        with QSignalBlocker(self.model_tree):
            self.model_tree.clear()
            for reference in references:
                block = reference.block
                row_reference = (
                    active_visual_reference
                    if (active_visual_reference is not None
                        and active_visual_reference.block == block)
                    else reference)
                if (row_reference.three_ds_normal
                        or row_reference.base_normal):
                    owner_path = ""
                    display_name = Path(
                        row_reference.three_ds_normal or
                        row_reference.base_normal).stem
                else:
                    owner_path, display_name = self._model_info_by_vp.get(
                        row_reference.vp_normal, ("", ""))
                if not owner_path and row_reference.vp_normal is None:
                    obj = next((candidate for candidate in self.family.all_objects()
                                if candidate.skeleton is not None), None) \
                        if self.family is not None else None
                    if obj is not None:
                        owner_path, display_name = (
                            obj.owner_path, obj.display_name)
                vp_text = (
                    str(reference.vp_normal)
                    if reference.vp_normal is not None else "—")
                item = QTreeWidgetItem([
                    block.name or f"{block.kind} {block.object_id}",
                    vp_text,
                ])
                item.setData(0, Qt.ItemDataRole.UserRole, owner_path)
                item.setData(0, _MODEL_NAME_ROLE, display_name)
                item.setData(
                    0, _MODEL_VP_ROLE,
                    (reference.vp_normal,)
                    if reference.vp_normal is not None else ())
                item.setData(0, _SCRIPT_REFERENCE_ROLE, reference)
                item.setToolTip(0, reference.label)
                item.setToolTip(
                    1, f"VP {reference.vp_normal}"
                    if reference.vp_normal is not None
                    else "Visual override; no SET VP needed")
                item.setTextAlignment(
                    1,
                    Qt.AlignmentFlag.AlignRight
                    | Qt.AlignmentFlag.AlignVCenter)
                self.model_tree.addTopLevelItem(item)
            self.model_tree.resizeColumnToContents(1)
        self._filter_models(self.model_search.text())

    def _select_script_unit_item(self, reference: VehicleModelReference):
        """Select one unit row without re-entering its load path."""

        for index in range(self.model_tree.topLevelItemCount()):
            item = self.model_tree.topLevelItem(index)
            if item.data(0, _SCRIPT_REFERENCE_ROLE) == reference:
                with QSignalBlocker(self.model_tree):
                    self.model_tree.setCurrentItem(item)
                    item.setSelected(True)
                return item
        return self.model_tree.currentItem()

    def _restore_script_unit_item(self) -> None:
        """Return the selection to the loaded unit after a cancelled switch."""

        if self._active_model_reference is not None:
            self._select_script_unit_item(self._active_model_reference)

    def _confirm_script_unit_switch(self) -> bool:
        """Ask before replacing unexported work with another script unit."""

        answer = QMessageBox.question(
            self, "Unsaved Entity Designer changes",
            self._unsaved_changes_message("Load another unit"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        return answer == QMessageBox.StandardButton.Yes

    def _owner_for_vp(self, vp_id: int) -> str | None:
        """Owner path of the model a VP resolves to, in any list mode."""

        info = self._model_info_by_vp.get(int(vp_id))
        return info[0] if info is not None else None

    def _collision_shape_root_owner(self) -> str | None:
        """Return the owner subtree for the visual source actually loaded."""

        reference = self._active_visual_reference
        if reference is not None and (
                reference.three_ds_normal or reference.base_normal):
            return "root"
        if reference is not None and reference.vp_normal is not None:
            return self._owner_for_vp(reference.vp_normal)
        if (self._active_base_path is not None
                and self._active_base_path.suffix.casefold() == ".3ds"):
            return "root"
        return self._current_owner

    def _model_changed(self, current, _previous):
        if current is None or self.family is None:
            return
        reference = current.data(0, _SCRIPT_REFERENCE_ROLE)
        if reference is not None:
            if reference != self._active_model_reference:
                # Qt is still handling the click or key press that changed
                # this row. Loading here would rebuild the list in the middle
                # of that interaction: the scroll position resets and the
                # selection lands on an unrelated row. Load after it ends.
                QTimer.singleShot(
                    0, lambda: self._load_script_unit(reference))
                return
            # The loaded unit reselected: refresh the preview state only.
            self._show_model_item(current)
            self._sync_all()
            return
        self._show_model_item(current)
        self._set_modified()
        self._sync_all()

    def _load_script_unit(self, reference: VehicleModelReference) -> None:
        """Load one listed unit once the selection interaction has ended."""

        if reference == self._active_model_reference:
            return
        if self._modified and not self._confirm_script_unit_switch():
            self._restore_script_unit_item()
            return
        if not self.open_vehicle_script(
                self._active_script_path,
                object_id=reference.block.object_id,
                object_kind=reference.block.kind):
            self._restore_script_unit_item()

    def _show_model_item(self, item) -> None:
        """Present one left-list model or script unit in the viewport."""

        owner = item.data(0, Qt.ItemDataRole.UserRole)
        owner = owner or None
        self._current_owner = owner
        # Capture the new unit's orbit view; _sync_all restores the active tab.
        self.viewport.set_cockpit_preview_active(False)
        self.viewport.load_family(
            self.family, {owner} if owner else None,
            primary_owner=owner)
        self._fx_scope = None
        self._current_owner_base_bounds = (
            self.viewport._model_preview_base_owner_bounds.get(owner))
        self._viewport_owner = owner
        # Frame and capture Reset View using this unit's preview settings,
        # before _sync_all updates the remaining controls and overlays.
        self.viewport.set_model_preview_scale(
            self.project.model_scale_x, self.project.model_scale_y,
            self.project.model_scale_z)
        self.viewport.set_ground_alignment(
            self.project.target_category == VEHICLE,
            self.project.overeof_enabled, self.project.overeof)
        if owner:
            self.viewport.frame_owner(owner)
        else:
            self.viewport.frame_all()
        # Same contract as Model Editor: Reset View restores the exact camera
        # first presented for this selected model, not a later re-fit.
        self.viewport.capture_reset_view()
        # A model selected inside FX needs its own normal editing view.
        self._fx_edit_camera = (self.viewport._camera_state()
                                if self._is_fx_tab_active() else None)
        self.project.source_model = item.data(0, _MODEL_NAME_ROLE)
        self._refresh_collision_geometry_widget()
        with QSignalBlocker(self.toolbar_view_preset_combo):
            self.toolbar_view_preset_combo.setCurrentText("Current View")
        self._sync_animation_controls()
        self._sync_gizmo_camera()
        self._update_reset_view_controls()

    def _model_bounds(self):
        # Cockpit View may temporarily render vp_wait while collision data
        # remains authored against the selected vp_normal owner.
        bounds = self._current_owner_base_bounds
        if bounds is None:
            return self.viewport.local_scaled_owner_bounds(self._current_owner)
        x0, y0, z0, x1, y1, z1 = bounds
        scale = (
            self.project.model_scale_x,
            self.project.model_scale_y,
            self.project.model_scale_z,
        )
        a = (x0 * scale[0], y0 * scale[1], z0 * scale[2])
        b = (x1 * scale[0], y1 * scale[1], z1 * scale[2])
        return (
            min(a[0], b[0]), min(a[1], b[1]), min(a[2], b[2]),
            max(a[0], b[0]), max(a[1], b[1]), max(a[2], b[2]),
        )

    def _default_sphere(self, category: str) -> CollisionSphere:
        bounds = self._model_bounds()
        if bounds is None:
            return CollisionSphere(category, radius=1.0)
        x0, y0, z0, x1, y1, z1 = bounds
        radius = max(x1 - x0, y1 - y0, z1 - z0, 1.0) * 0.5
        return CollisionSphere(
            category,
            (x0 + x1) * 0.5,
            (y0 + y1) * 0.5,
            (z0 + z1) * 0.5,
            radius,
        )

    def add_legacy(self):
        self._push_undo()
        if self.project.legacy is None:
            sphere = self._default_sphere(LEGACY)
            sphere.x = sphere.y = sphere.z = 0.0
            self.project.legacy = sphere
        self._selected = 0
        self._selected_spheres = {0}
        self._selected_fire_point = -1
        self._selected_gun_point = -1
        self._set_modified()
        self.properties_tabs.setCurrentIndex(self.collision_tab_index)
        self._sync_all()

    def _ensure_compound_capacity(
            self, additional: int, operation: str) -> bool:
        """Keep authored compound collisions within the runtime safety cap."""

        available = max(0, UNIT_COLL_MAX_COUNT - len(self.project.compound))
        if additional <= available:
            return True
        self.statusBar().showMessage(
            f"{operation} would exceed the OpenNeoUA collision sphere limit "
            f"({len(self.project.compound)} / {UNIT_COLL_MAX_COUNT}; "
            f"{available} slot{'s' if available != 1 else ''} available).",
            6500)
        return False

    def add_compound(self, category: str):
        if not self._ensure_compound_capacity(1, "Add sphere"):
            return
        self._push_undo()
        self.project.compound.append(self._default_sphere(category))
        self._selected = len(self.project.spheres()) - 1
        self._selected_spheres = {self._selected}
        self._selected_fire_point = -1
        self._selected_gun_point = -1
        self._set_modified()
        self.properties_tabs.setCurrentIndex(self.collision_tab_index)
        self._sync_all()

    def generate_collision_spheres(self, preset_key: str):
        """Replace compound spheres with a classified physical-core approximation."""

        preset = next((
            candidate for candidate in ACCURACY_PRESETS
            if candidate.key == preset_key
        ), None)
        if preset is None:
            raise ValueError(
                f"Unsupported collision-sphere accuracy: {preset_key}")
        animated_indices = []
        triangles = self.viewport.local_owner_triangles(
            self._current_owner, animated_indices=animated_indices)
        if not triangles:
            QMessageBox.warning(
                self, "No model geometry",
                "Load and select a model with usable surface geometry first.")
            return

        if self.project.compound and not self._confirm_generated_replacement():
            return

        self.statusBar().showMessage(
            f"Generating collision spheres — {preset.label}")
        QApplication.processEvents()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = generate_collision_spheres(
                triangles, preset.key, max_spheres=UNIT_COLL_MAX_COUNT,
                animated_triangles=animated_indices)
        except ValueError as exc:
            QMessageBox.warning(
                self, "Collision sphere generation failed", str(exc))
            return
        finally:
            QApplication.restoreOverrideCursor()

        self._push_undo()
        self.project.compound = [
            CollisionSphere(
                OPENNEOUA, sphere.x, sphere.y, sphere.z, sphere.radius)
            for sphere in result.spheres
        ]
        self.project.sphere_isolation_active = False
        self._selected = 1 if self.project.legacy is not None else 0
        self._selected_spheres = {self._selected}
        self._selected_fire_point = -1
        self._selected_gun_point = -1
        self._set_modified()
        self.properties_tabs.setCurrentIndex(self.collision_tab_index)
        self._sync_all()
        cap_note = " — safety cap reached" if result.hit_safety_cap else ""
        symmetry_note = " — mirrored pairs" if result.symmetry_detected else ""
        coverage_note = (
            " — coverage target not reached"
            if result.measured_error > result.tolerance + 1e-6
            and not result.hit_safety_cap else "")
        self.statusBar().showMessage(
            f"Generated {len(result.spheres)} collision spheres — "
            f"{preset.label}{symmetry_note}{cap_note}{coverage_note}", 8000)

    def _confirm_generated_replacement(self) -> bool:
        confirmation = QMessageBox(self)
        confirmation.setIcon(QMessageBox.Icon.Warning)
        confirmation.setWindowTitle("Replace OpenNeoUA Collision Spheres")
        confirmation.setText(
            "Existing OpenNeoUA collision spheres will be replaced.\n"
            "The Legacy Radius will be preserved.\n\nContinue?")
        generate_button = confirmation.addButton(
            "Generate", QMessageBox.ButtonRole.AcceptRole)
        cancel_button = confirmation.addButton(
            "Cancel", QMessageBox.ButtonRole.RejectRole)
        confirmation.setDefaultButton(cancel_button)
        confirmation.exec()
        return confirmation.clickedButton() is generate_button

    def duplicate_sphere(self):
        entries = self._selected_sphere_entries()
        selected_compound = [
            sphere for _index, sphere in entries
            if sphere.category != LEGACY
        ]
        if not entries:
            return
        if not selected_compound:
            QMessageBox.information(
                self, "Single legacy radius",
                "A project can contain only one Legacy Radius.")
            return
        if not self._ensure_compound_capacity(
                len(selected_compound), "Duplicate selection"):
            return

        self._push_undo()
        selected_ids = {id(sphere) for sphere in selected_compound}
        duplicates = []
        rebuilt = []
        for sphere in self.project.compound:
            rebuilt.append(sphere)
            if id(sphere) in selected_ids:
                duplicate = sphere.clone()
                rebuilt.append(duplicate)
                duplicates.append(duplicate)
        self.project.compound[:] = rebuilt
        self._select_sphere_objects(duplicates)
        self._selected_fire_point = -1
        self._selected_gun_point = -1
        self._set_modified()
        self._sync_all()
        skipped = len(entries) - len(selected_compound)
        detail = " Legacy Radius was skipped." if skipped else ""
        self.statusBar().showMessage(
            f"Duplicated {len(duplicates)} selected collision sphere"
            f"{'s' if len(duplicates) != 1 else ''}.{detail}", 4500)

    def _populate_change_type_menu(self, menu: QMenu) -> None:
        menu.addAction(self.change_to_legacy_action)
        menu.addAction(self.change_to_openneoua_action)

    def _populate_generate_collision_spheres_menu(self, menu: QMenu) -> None:
        for preset in ACCURACY_PRESETS:
            menu.addAction(self.generate_collision_sphere_actions[preset.key])

    def change_sphere_type(self, target_category: str):
        """Convert the complete current sphere selection when valid."""

        if target_category not in (LEGACY, OPENNEOUA):
            raise ValueError(
                f"Unsupported collision category: {target_category}")
        entries = self._selected_sphere_entries()
        if not entries:
            return

        selected_spheres = [sphere for _index, sphere in entries]
        if target_category == LEGACY:
            if len(selected_spheres) != 1:
                self.statusBar().showMessage(
                    "Legacy Radius is singular: select exactly one sphere "
                    "before converting to Legacy Radius.", 5500)
                return
            sphere = selected_spheres[0]
            if sphere.category == LEGACY:
                return
            if self.project.legacy is not None:
                self.statusBar().showMessage(
                    "A project can contain only one Legacy Radius.", 5000)
                return
            compound_index = next((
                index for index, candidate in enumerate(self.project.compound)
                if candidate is sphere
            ), None)
            if compound_index is None:
                return
            self._push_undo()
            del self.project.compound[compound_index]
            sphere.category = LEGACY
            sphere.x = sphere.y = sphere.z = 0.0
            self.project.legacy = sphere
            self._select_sphere_objects([sphere])
            detail = (
                " Positional offsets were reset because vanilla radius "
                "has no offset.")
            changed_count = 1
        else:
            changing = [
                sphere for sphere in selected_spheres
                if sphere.category != target_category
            ]
            if not changing:
                return
            legacy = self.project.legacy
            converted_legacy = (
                legacy is not None
                and any(candidate is legacy for candidate in changing))
            if converted_legacy and not self._ensure_compound_capacity(
                    1, "Convert Legacy Radius"):
                return
            self._push_undo()
            if converted_legacy:
                self.project.legacy = None
                legacy.category = target_category
                # Legacy has no authored offset, so its existing origin is the
                # correct compound position when it becomes movable.
                self.project.compound.insert(0, legacy)
            for sphere in changing:
                if sphere is legacy:
                    continue
                sphere.category = target_category
            self._select_sphere_objects(selected_spheres)
            detail = ""
            changed_count = len(changing)

        self._selected_fire_point = -1
        self._selected_gun_point = -1
        self._set_modified()
        self._sync_all()
        label = TYPE_LABELS[target_category]
        self.statusBar().showMessage(
            f"Changed {changed_count} selected sphere"
            f"{'s' if changed_count != 1 else ''} to {label}.{detail}", 5000)

    def mirror_selected_sphere(self, axis: str):
        """Mirror every selected compound sphere across one model axis."""

        if axis not in ("x", "y", "z"):
            raise ValueError(f"Unsupported mirror axis: {axis}")
        entries = self._selected_sphere_entries()
        if not entries:
            return
        selected_compound = [
            sphere for _index, sphere in entries
            if sphere.category != LEGACY
        ]
        if not selected_compound:
            self.statusBar().showMessage(
                "Legacy Radius is fixed at the origin and cannot be mirrored.",
                5000)
            return
        if not self._ensure_compound_capacity(
                len(selected_compound), "Mirror selection"):
            return

        self._push_undo()
        selected_ids = {id(sphere) for sphere in selected_compound}
        mirrored_spheres = []
        rebuilt = []
        for sphere in self.project.compound:
            rebuilt.append(sphere)
            if id(sphere) in selected_ids:
                mirrored = sphere.clone()
                setattr(mirrored, axis, -getattr(mirrored, axis))
                rebuilt.append(mirrored)
                mirrored_spheres.append(mirrored)
        self.project.compound[:] = rebuilt
        self._select_sphere_objects(mirrored_spheres)
        self._selected_fire_point = -1
        self._selected_gun_point = -1
        self._set_modified()
        self._sync_all()
        skipped = len(entries) - len(selected_compound)
        detail = " Legacy Radius was skipped." if skipped else ""
        self.statusBar().showMessage(
            f"Mirrored {len(mirrored_spheres)} selected collision sphere"
            f"{'s' if len(mirrored_spheres) != 1 else ''} across the "
            f"{axis.upper()} axis.{detail}", 4500)

    def delete_sphere(self):
        entries = self._selected_sphere_entries()
        if not entries:
            return
        selected_indices = {index for index, _sphere in entries}
        selected_ids = {id(sphere) for _index, sphere in entries}
        anchor = min(selected_indices)

        self._push_undo()
        if self.project.legacy is not None \
                and id(self.project.legacy) in selected_ids:
            self.project.legacy = None
        self.project.compound[:] = [
            sphere for sphere in self.project.compound
            if id(sphere) not in selected_ids
        ]
        remaining = self.project.spheres()
        if self.project.sphere_isolation_active and (
                not remaining
                or not any(sphere.visible for sphere in remaining)
                or not any(not sphere.visible for sphere in remaining)):
            self.project.sphere_isolation_active = False
        count = len(self.project.spheres())
        self._selected = min(anchor, count - 1) if count else -1
        self._selected_spheres = (
            {self._selected} if self._selected >= 0 else set())
        self._selected_fire_point = -1
        self._selected_gun_point = -1
        self._set_modified()
        self._sync_all()
        self.statusBar().showMessage(
            f"Deleted {len(entries)} selected collision sphere"
            f"{'s' if len(entries) != 1 else ''}.", 4000)

    def delete_all_collisions(self):
        if not self.project.spheres():
            return
        result = QMessageBox.question(
            self, "Delete All Collisions",
            "Delete every Legacy, Vehicle and Weapon collision sphere?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if result != QMessageBox.StandardButton.Yes:
            return
        self._push_undo()
        self.project.legacy = None
        self.project.compound.clear()
        self.project.sphere_isolation_active = False
        self._selected = -1
        self._selected_spheres.clear()
        self._selected_fire_point = -1
        self._selected_gun_point = -1
        self._set_modified()
        self._sync_all()


    def _create_sphere_context_menu(self, index: int) -> QMenu:
        menu = QMenu(self)
        menu.addAction(self.undo_action)
        menu.addAction(self.redo_action)
        menu.addSeparator()
        menu.addAction(self.add_legacy_action)
        menu.addAction(self.add_openneoua_action)
        menu.addSeparator()
        self._context_action(
            menu, "Select All Spheres",
            lambda _checked=False: self.select_all_spheres(),
            bool(self.project.spheres()))
        menu.addAction(self.duplicate_action)
        menu.addAction(self.delete_action)
        entries = self._selected_sphere_entries()
        all_hidden = bool(entries) and all(
            not sphere.visible for _index, sphere in entries)
        visibility_text = (
            "Show Spheres" if all_hidden and len(entries) > 1
            else "Show Sphere" if all_hidden
            else "Hide Spheres" if len(entries) > 1
            else "Hide Sphere")
        self._context_action(
            menu, visibility_text, self._toggle_selected_sphere_visibility,
            bool(entries))
        spheres = self.project.spheres()
        self._context_action(
            menu, "Hide All Spheres",
            lambda _checked=False: self._set_all_sphere_visibility(False),
            any(sphere.visible for sphere in spheres))
        self._context_action(
            menu, "Show All Spheres",
            lambda _checked=False: self._set_all_sphere_visibility(True),
            any(not sphere.visible for sphere in spheres))
        if self.project.sphere_isolation_active:
            self._context_action(
                menu, "Unisolate Spheres" if len(
                    self._selected_sphere_indices()) > 1
                else "Unisolate Sphere",
                self._unisolate_sphere_visibility, bool(spheres))
        elif 0 <= index < len(spheres):
            selected = self._selected_sphere_indices()
            self._context_action(
                menu, "Isolate Spheres" if len(selected) > 1
                else "Isolate Sphere",
                self._toggle_selected_sphere_isolation)
        change_type_menu = menu.addMenu(
            "Change Sphere Types" if len(entries) > 1
            else "Change Sphere Type")
        self._populate_change_type_menu(change_type_menu)
        mirror_menu = menu.addMenu(
            "Mirror Selected Spheres" if len(entries) > 1
            else "Mirror Selected Sphere")
        mirror_menu.addAction(self.mirror_x_action)
        mirror_menu.addAction(self.mirror_y_action)
        mirror_menu.addAction(self.mirror_z_action)
        menu.addSeparator()
        menu.addAction(self.reset_view_action)
        menu.addSeparator()
        menu.addAction(self.open_base_action)
        menu.addAction(self.open_sklt_action)
        preset_menu = menu.addMenu("View Preset")
        self._populate_view_preset_context_menu(preset_menu)
        return menu

    def _populate_view_preset_context_menu(self, menu: QMenu) -> None:
        """Populate the same read-only camera presets in every workspace menu."""

        for preset in VIEW_PRESETS:
            action = menu.addAction(preset)
            action.setCheckable(True)
            action.setChecked(
                preset == self.toolbar_view_preset_combo.currentText())
            action.triggered.connect(
                lambda _checked=False, value=preset:
                self.toolbar_view_preset_combo.setCurrentText(value))

    @staticmethod
    def _context_action(menu: QMenu, text: str, callback, enabled: bool = True):
        action = menu.addAction(text)
        action.setEnabled(enabled)
        action.triggered.connect(callback)
        return action

    def _add_workspace_context_tail(self, menu: QMenu, visibility_action=None) -> None:
        if visibility_action is not None:
            menu.addSeparator()
            menu.addAction(visibility_action)
        menu.addAction(self.reset_view_action)
        menu.addSeparator()
        menu.addAction(self.open_base_action)
        menu.addAction(self.open_sklt_action)
        preset_menu = menu.addMenu("View Preset")
        self._populate_view_preset_context_menu(preset_menu)

    def _create_collision_shape_context_menu(self, component_key=None) -> QMenu:
        menu = QMenu(self)
        if component_key is not None:
            widget = self.collision_geometry_widget
            item = next((row for row in widget._items()
                         if row.data(0, _GEOMETRY_COMPONENT_ROLE) == component_key), None)
            if item is not None:
                if not item.isSelected():
                    widget.select_shape_component(component_key)
                else:
                    widget.tree.setCurrentItem(item, 0, QItemSelectionModel.SelectionFlag.NoUpdate)
                widget._add_component_toggle_action(menu, item)
                menu.addSeparator()
        self.collision_geometry_widget._add_component_deselect_action(menu)
        menu.addSeparator()
        menu.addAction(self.undo_action)
        menu.addAction(self.redo_action)
        menu.addSeparator()
        self._add_workspace_context_tail(menu)
        return menu

    def _create_fire_point_context_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.addAction(self.undo_action)
        menu.addAction(self.redo_action)
        menu.addSeparator()
        self._context_action(menu, "Add Fire Point", self.add_fire_point,
                             self.add_fire_point_button.isEnabled())
        self._context_action(
            menu, "Remove Selected Fire Point", self.remove_fire_point,
            self.remove_fire_point_button.isEnabled())
        self._context_action(
            menu, "Reset Position to 0 / 0 / 0",
            self._reset_fire_point_position,
            self.reset_fire_point_button.isEnabled())
        self._add_workspace_context_tail(
            menu, self.viewpoint_actions.get("Show Fire Points"))
        return menu

    def _create_gun_point_context_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.addAction(self.undo_action)
        menu.addAction(self.redo_action)
        menu.addSeparator()
        self._context_action(menu, "Add Gun Point", self.add_gun_point,
                             self.add_gun_point_button.isEnabled())
        self._context_action(
            menu, "Remove Selected Gun Point", self.remove_gun_point,
            self.remove_gun_point_button.isEnabled())
        self._context_action(
            menu, "Reset Selected Position to 0 / 0 / 0",
            self._reset_gun_point_position,
            self.reset_gun_point_button.isEnabled())
        self._context_action(
            menu, "Reset All", self._reset_all_gun_points,
            self.reset_all_gun_points_button.isEnabled())

        type_menu = menu.addMenu("Gun Point Type")
        for index in range(self.gun_point_type_combo.count()):
            text = self.gun_point_type_combo.itemText(index)
            data = self.gun_point_type_combo.itemData(index)
            action = type_menu.addAction(text)
            action.setCheckable(True)
            action.setChecked(data == self._new_gun_point_scheme)
            action.triggered.connect(
                lambda _checked=False, value=data:
                self.gun_point_type_combo.setCurrentIndex(
                    self.gun_point_type_combo.findData(value)))

        self._add_workspace_context_tail(
            menu, self.viewpoint_actions.get("Show Gun Points"))
        return menu

    def _create_cockpit_context_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.addAction(self.undo_action)
        menu.addAction(self.redo_action)
        menu.addSeparator()

        cockpit_enabled = (self.project.target_category == VEHICLE
                           and self.project.cockpit_camera_enabled)
        toggle_text = (
            "Disable Cockpit Camera Offset" if cockpit_enabled
            else "Enable Cockpit Camera Offset")
        self._context_action(
            menu, toggle_text, self._toggle_cockpit_camera,
            self.project.target_category == VEHICLE)
        self._context_action(
            menu, "Reset Position to 0 / 0 / 0", self._reset_cockpit_camera,
            cockpit_enabled)
        self._context_action(
            menu, "Restore Script Position",
            self._restore_loaded_cockpit_camera,
            self.restore_script_cockpit_button.isEnabled())

        model_menu = menu.addMenu("Runtime Model")
        for index in range(self.cockpit_model_state_combo.count()):
            text = self.cockpit_model_state_combo.itemText(index)
            data = self.cockpit_model_state_combo.itemData(index)
            action = model_menu.addAction(text)
            action.setCheckable(True)
            action.setChecked(
                data == self.cockpit_model_state_combo.currentData())
            action.triggered.connect(
                lambda _checked=False, value=data:
                self.cockpit_model_state_combo.setCurrentIndex(
                    self.cockpit_model_state_combo.findData(value)))

        aspect_menu = menu.addMenu("Runtime Aspect")
        for index in range(self.cockpit_runtime_aspect_combo.count()):
            text = self.cockpit_runtime_aspect_combo.itemText(index)
            data = self.cockpit_runtime_aspect_combo.itemData(index)
            action = aspect_menu.addAction(text)
            action.setCheckable(True)
            action.setChecked(
                data == self.cockpit_runtime_aspect_combo.currentData())
            action.triggered.connect(
                lambda _checked=False, value=data:
                self.cockpit_runtime_aspect_combo.setCurrentIndex(
                    self.cockpit_runtime_aspect_combo.findData(value)))

        menu.addSeparator()
        menu.addAction(self.open_base_action)
        menu.addAction(self.open_sklt_action)
        return menu

    def _show_collision_shape_context_menu(self, local_pos: QPoint) -> None:
        self._create_collision_shape_context_menu().exec(
            self.collision_shape_box.mapToGlobal(local_pos))

    def _show_fire_points_context_menu(self, local_pos: QPoint) -> None:
        self._create_fire_point_context_menu().exec(
            self.fire_points_box.mapToGlobal(local_pos))

    def _show_gun_points_context_menu(self, local_pos: QPoint) -> None:
        self._create_gun_point_context_menu().exec(
            self.gun_points_box.mapToGlobal(local_pos))

    def _show_cockpit_context_menu(self, local_pos: QPoint) -> None:
        self._create_cockpit_context_menu().exec(
            self.cockpit_box.mapToGlobal(local_pos))

    def _show_fire_point_tree_context_menu(self, local_pos: QPoint) -> None:
        item = self.fire_point_tree.itemAt(local_pos)
        if item is not None:
            self.fire_point_tree.setCurrentItem(item)
        self._create_fire_point_context_menu().exec(
            self.fire_point_tree.viewport().mapToGlobal(local_pos))

    def _show_gun_point_tree_context_menu(self, local_pos: QPoint) -> None:
        item = self.gun_point_tree.itemAt(local_pos)
        if item is not None:
            self.gun_point_tree.setCurrentItem(item)
        self._create_gun_point_context_menu().exec(
            self.gun_point_tree.viewport().mapToGlobal(local_pos))

    def _create_fx_context_menu(self) -> QMenu:
        """Expose only existing destruction-preview controls in the FX viewport."""
        menu = QMenu(self)
        panel = self.fx_panel
        menu.addAction("Pause" if panel.is_playing() else "Play", panel.play_button.click)
        menu.addAction("Restart", panel.restart)
        menu.addAction("Stop", panel.stop)
        menu.addSeparator()
        menu.addAction(self.reset_view_action)
        return menu

    def _show_sphere_context_menu(self, index: int, global_pos: QPoint):
        if self._is_fx_tab_active():
            self._create_fx_context_menu().exec(global_pos)
            return
        if (self.properties_tabs.currentIndex()
                == self.collision_shape_tab_index):
            key = self.viewport._hit_shape_component(
                QPointF(self.viewport.mapFromGlobal(global_pos)))
            self._create_collision_shape_context_menu(key).exec(global_pos)
            return
        self._create_sphere_context_menu(index).exec(global_pos)

    def _show_sphere_tree_context_menu(self, local_pos: QPoint):
        item = self.sphere_tree.itemAt(local_pos)
        index = -1
        if item is not None:
            candidate = item.data(0, _SPHERE_INDEX_ROLE)
            if isinstance(candidate, int):
                index = candidate
                if index not in self._selected_sphere_indices():
                    self._select_sphere(index)
        self._show_sphere_context_menu(
            index, self.sphere_tree.viewport().mapToGlobal(local_pos))

    def _selected_sphere(self):
        spheres = self.project.spheres()
        return spheres[self._selected] if 0 <= self._selected < len(
            spheres) else None

    def _selected_sphere_indices(self) -> set[int]:
        count = len(self.project.spheres())
        selected = {
            index for index in self._selected_spheres
            if 0 <= index < count
        }
        if selected:
            if self._selected not in selected:
                self._selected = min(selected)
        elif 0 <= self._selected < count:
            selected = {self._selected}
        else:
            self._selected = -1
        self._selected_spheres = selected
        return set(selected)

    def _selected_sphere_entries(self) -> list[tuple[int, CollisionSphere]]:
        spheres = self.project.spheres()
        return [
            (index, spheres[index])
            for index in sorted(self._selected_sphere_indices())
            if 0 <= index < len(spheres)
        ]

    def _selected_spheres_with_common_radius(self) -> list[CollisionSphere]:
        """Return the selection only when every authored radius matches."""

        spheres = [sphere for _index, sphere in self._selected_sphere_entries()]
        if spheres and all(sphere.radius == spheres[0].radius
                           for sphere in spheres):
            return spheres
        return []

    def _select_sphere_objects(self, spheres) -> None:
        """Restore a selection by object identity after list mutations."""

        wanted = {id(sphere) for sphere in spheres}
        selected = {
            index for index, sphere in enumerate(self.project.spheres())
            if id(sphere) in wanted
        }
        self._selected_spheres = selected
        self._selected = min(selected, default=-1)

    def _selected_compound_index(self) -> int | None:
        if self._selected < 0:
            return None
        offset = 1 if self.project.legacy is not None else 0
        compound_index = self._selected - offset
        if 0 <= compound_index < len(self.project.compound):
            return compound_index
        return None

    def _select_sphere(self, index: int):
        self._radius_spin_active = False
        self._vehicle_preview_active_edits.clear()
        count = len(self.project.spheres())
        self._selected = index if 0 <= index < count else -1
        self._selected_spheres = (
            {self._selected} if self._selected >= 0 else set())
        self._selected_fire_point = -1
        self._selected_gun_point = -1
        if (0 <= index < len(self.project.spheres())
                and self.properties_tabs.currentIndex()
                != self.collision_tab_index):
            with QSignalBlocker(self.properties_tabs):
                self.properties_tabs.setCurrentIndex(self.collision_tab_index)
        self._sync_all()

    def _toggle_sphere(self, index: int):
        self._radius_spin_active = False
        self._vehicle_preview_active_edits.clear()
        count = len(self.project.spheres())
        if not (0 <= index < count):
            return
        selected = self._selected_sphere_indices()
        if index in selected:
            selected.remove(index)
            self._selected = min(selected, default=-1)
        else:
            selected.add(index)
            self._selected = index
        self._selected_spheres = selected
        self._selected_fire_point = -1
        self._selected_gun_point = -1
        if (selected and self.properties_tabs.currentIndex()
                != self.collision_tab_index):
            with QSignalBlocker(self.properties_tabs):
                self.properties_tabs.setCurrentIndex(self.collision_tab_index)
        self._sync_all()

    def _select_sphere_box(self, indices, additive: bool = True):
        self._radius_spin_active = False
        self._vehicle_preview_active_edits.clear()
        count = len(self.project.spheres())
        hits = {
            int(index) for index in indices
            if isinstance(index, int) and 0 <= index < count
        }
        selected = self._selected_sphere_indices() if additive else set()
        selected |= hits
        self._selected_spheres = selected
        if hits:
            self._selected = min(hits)
        elif self._selected not in selected:
            self._selected = min(selected, default=-1)
        self._selected_fire_point = -1
        self._selected_gun_point = -1
        if (selected and self.properties_tabs.currentIndex()
                != self.collision_tab_index):
            with QSignalBlocker(self.properties_tabs):
                self.properties_tabs.setCurrentIndex(self.collision_tab_index)
        self._sync_all()

    def select_all_spheres(self):
        count = len(self.project.spheres())
        if count <= 0:
            return
        selected = set(range(count))
        self._selected_spheres = selected
        if self._selected not in selected:
            self._selected = 0
        self._selected_fire_point = -1
        self._selected_gun_point = -1
        if self.properties_tabs.currentIndex() != self.collision_tab_index:
            with QSignalBlocker(self.properties_tabs):
                self.properties_tabs.setCurrentIndex(self.collision_tab_index)
        self._sync_all()

    def _select_fire_point(self, index: int):
        self._radius_spin_active = False
        self._vehicle_preview_active_edits.clear()
        point_count = len(fire_point_positions(self.project))
        self._selected_fire_point = (
            index if (self.project.fire_points_enabled
                      and 0 <= index < point_count) else -1)
        self._selected_gun_point = -1
        self._selected = -1
        self._selected_spheres.clear()
        if (self._selected_fire_point >= 0
                and self.properties_tabs.currentIndex()
                != self.fire_points_tab_index):
            # Viewport picks are domain navigation too: jump directly to the
            # controls for the selected muzzle without triggering a redundant
            # currentChanged sync before the normal _sync_all below.
            with QSignalBlocker(self.properties_tabs):
                self.properties_tabs.setCurrentIndex(
                    self.fire_points_tab_index)
        self._sync_all()

    def _selected_gun(self) -> GunPoint | None:
        index = self._selected_gun_point
        if (self.project.gun_points_enabled
                and 0 <= index < len(self.project.gun_points)):
            return self.project.gun_points[index]
        return None

    def _selected_turret_limits(self) -> TurretLimits | None:
        gun = self._selected_gun()
        if gun is None:
            return None
        return self.project.turret_limits.get(int(gun.gun_type))

    def _resolve_referenced_turret_limits(self, vehicle_id: int) -> TurretLimits | None:
        """Resolve a mount's Vehicle through the same catalog as the browser."""
        existing = self.project.turret_limits.get(vehicle_id)
        if existing is not None:
            return existing
        obj = next((item for item in self.game_object_browser.objects
                    if item.kind == "vehicle" and item.object_id == vehicle_id), None)
        if obj is None:
            return None
        try:
            limits = import_turret_limits(obj.effective_text).get(vehicle_id)
            if limits is None:
                return None
            # Effective catalog text is a synthetic new_* block. Saving must
            # retain the actual new/modify block kind in the source file.
            source_text, _encoding, _bom = read_script_file(obj.source)
            source_limits = import_turret_limits(source_text).get(vehicle_id)
            if source_limits is not None:
                limits.source_kind = source_limits.source_kind
        except (OSError, UnicodeError, CollisionScriptError):
            return None
        self.project.turret_limits[vehicle_id] = limits
        self._loaded_turret_limits[vehicle_id] = limits.clone()
        # Reading a referenced definition is not an authored change.
        if self._tab_reset_baseline is not None:
            baseline = CollisionProject()
            baseline.restore(self._tab_reset_baseline)
            baseline.turret_limits[vehicle_id] = limits.clone()
            self._tab_reset_baseline = baseline.snapshot()
        return limits

    def _select_gun_point(self, index: int):
        self._radius_spin_active = False
        self._vehicle_preview_active_edits.clear()
        self._selected_gun_point = (
            index if (self.project.gun_points_enabled
                      and 0 <= index < len(self.project.gun_points)) else -1)
        selected = self._selected_gun()
        if selected is not None and selected.scheme in {"robo", "unit"}:
            # Selection only changes the default family for the *next* mount.
            # The selected point itself is never converted implicitly.
            self._new_gun_point_scheme = selected.scheme
        self._selected_fire_point = -1
        self._selected = -1
        self._selected_spheres.clear()
        if (self._selected_gun_point >= 0
                and self.properties_tabs.currentIndex()
                != self.gun_points_tab_index):
            with QSignalBlocker(self.properties_tabs):
                self.properties_tabs.setCurrentIndex(
                    self.gun_points_tab_index)
        self._sync_all()


    def _fire_point_tree_selection_changed(self, current, _previous):
        if self._syncing or current is None:
            return
        index = current.data(0, _FIRE_POINT_INDEX_ROLE)
        if isinstance(index, int):
            self._select_fire_point(index)


    def _gun_point_tree_selection_changed(self, current, _previous):
        if self._syncing or current is None:
            return
        index = current.data(0, _GUN_POINT_INDEX_ROLE)
        if isinstance(index, int):
            self._select_gun_point(index)


    def _sphere_tree_selection_changed(self):
        if self._syncing:
            return
        selected: set[int] = set()
        for item in self.sphere_tree.selectedItems():
            index = item.data(0, _SPHERE_INDEX_ROLE)
            if isinstance(index, int):
                selected.add(index)
        current = self.sphere_tree.currentItem()
        primary = -1
        if current is not None:
            index = current.data(0, _SPHERE_INDEX_ROLE)
            if isinstance(index, int) and index in selected:
                primary = index
        if primary < 0:
            primary = min(selected, default=-1)
        self._radius_spin_active = False
        self._vehicle_preview_active_edits.clear()
        self._selected_spheres = selected
        self._selected = primary
        self._selected_fire_point = -1
        self._selected_gun_point = -1
        self._sync_all()

    def _move_strength_slider_changed(self, value: int):
        with QSignalBlocker(self.move_strength_spin):
            self.move_strength_spin.setValue(value)

    def _move_strength_spin_changed(self, value: int):
        # A typed value may exceed the normal quick-slider range. Expand only
        # while necessary, then restore the useful fine-control range.
        with QSignalBlocker(self.move_strength_slider):
            self.move_strength_slider.setMaximum(max(500, value))
            self.move_strength_slider.setValue(value)

    def _model_preview_scale_changed(self, *_args):
        if self._syncing:
            return
        values = (
            self.model_scale_x_spin.value(),
            self.model_scale_y_spin.value(),
            self.model_scale_z_spin.value(),
        )
        current = (
            self.project.model_scale_x,
            self.project.model_scale_y,
            self.project.model_scale_z,
        )
        if all(abs(a - b) < 1e-9 for a, b in zip(values, current)):
            return
        self._push_undo()
        (self.project.model_scale_x,
         self.project.model_scale_y,
         self.project.model_scale_z) = values
        self.viewport.set_model_preview_scale(*values)
        self._set_modified()
        self._sync_all()

    def _reset_model_preview_scale(self):
        current = (
            self.project.model_scale_x,
            self.project.model_scale_y,
            self.project.model_scale_z,
        )
        if all(abs(value - 1.0) < 1e-9 for value in current):
            return
        self._push_undo()
        self.project.model_scale_x = 1.0
        self.project.model_scale_y = 1.0
        self.project.model_scale_z = 1.0
        self.viewport.set_model_preview_scale(1.0, 1.0, 1.0)
        self._set_modified()
        self._sync_all()

    def _overeof_enabled_changed(self, enabled: bool):
        if self._syncing or self.project.target_category != VEHICLE:
            return
        enabled = bool(enabled)
        if enabled == self.project.overeof_enabled:
            return
        self._overeof_spin_active = False
        self._push_undo()
        self.project.overeof_enabled = enabled
        self._set_modified()
        self._sync_all()

    def _overeof_value_changed(self, value: float):
        if self._syncing or self.project.target_category != VEHICLE:
            return
        value = float(value)
        if not math.isfinite(value) or abs(value - self.project.overeof) < 1e-9:
            return
        if not self._overeof_spin_active:
            self._push_undo()
            self._overeof_spin_active = True
        self.project.overeof = value
        self.project.overeof_enabled = True
        self._set_modified()
        self._sync_all()

    def _finish_overeof_edit(self):
        self._overeof_spin_active = False

    def _suggest_overeof_from_bounds(self):
        bounds = self._model_bounds()
        if bounds is None:
            QMessageBox.warning(
                self, "No model", "Load and select a model first.")
            return
        # OpenNeoUA places the actor origin at ground_y - overeof.  In the
        # model convention used by UA the lowest point is the maximum local Y.
        suggested = float(bounds[4])
        if (self.project.overeof_enabled
                and abs(self.project.overeof - suggested) < 1e-9):
            return
        self._overeof_spin_active = False
        self._push_undo()
        self.project.overeof = suggested
        self.project.overeof_enabled = True
        self._set_modified()
        self._sync_all()
        self.statusBar().showMessage(
            "Suggested Overeof uses the lowest scaled model point; verify "
            "the final terrain contact in-game.", 7000)

    def _reset_overeof(self):
        if (not self.project.overeof_enabled
                and abs(self.project.overeof) < 1e-9):
            return
        self._overeof_spin_active = False
        self._push_undo()
        self.project.overeof_enabled = False
        self.project.overeof = 0.0
        self._set_modified()
        self._sync_all()

    def _sync_workspace_overlay_visibility(self, *_args) -> None:
        """Show only overlays owned by the active properties workspace.

        Viewpoint menu actions remain per-overlay user preferences, but they
        cannot make an overlay leak into another tab. Model/textures stay
        global because they are the common editing reference for every tab.
        """

        if not (hasattr(self, "properties_tabs")
                and hasattr(self, "viewpoint_actions")):
            return

        active = self.properties_tabs.currentIndex()
        collision_tab = active == self.collision_tab_index
        ground_tab = active == self.ground_tab_index
        shape_tab = active == self.collision_shape_tab_index
        fire_tab = active == self.fire_points_tab_index
        gun_tab = active == self.gun_points_tab_index

        def wanted(label: str) -> bool:
            action = self.viewpoint_actions.get(label)
            return action is None or action.isChecked()

        self.viewport.set_collision_category_visible(
            LEGACY, collision_tab and wanted("Show Legacy Radius"))
        self.viewport.set_collision_category_visible(
            OPENNEOUA, collision_tab and wanted("Show OpenNeoUA Collisions"))
        # The ground plane is also the reference surface of Destruction FX:
        # wrecks, fragments and decals land on it.
        self.viewport.set_ground_simulation_visible(
            (collision_tab or ground_tab or self._is_fx_tab_active())
            and wanted("Show Ground Simulation"))
        self.viewport.set_overeof_visible(
            ground_tab and wanted("Show Overeof"))
        self.viewport.set_collision_shape_overlay_visible(shape_tab)

        self.viewport.set_fire_points_visible(
            fire_tab and wanted("Show Fire Points"))

        self.viewport.set_gun_points_visible(
            gun_tab and wanted("Show Gun Points"))
        self.viewport.set_turret_limits_visible(
            gun_tab and wanted("Show Turret Limits"))

    def _is_cockpit_tab_selected(self) -> bool:
        return (hasattr(self, "properties_tabs")
                and self.properties_tabs.currentIndex()
                == self.cockpit_tab_index
                and self.project.target_category == VEHICLE)

    def _is_cockpit_tab_active(self) -> bool:
        # Entering the Cockpit tab is a pure viewport switch: the runtime-style
        # preview shows right away and the orbit camera state is never touched,
        # so leaving the tab restores the exact previous editor view.
        return self._is_cockpit_tab_selected()

    def _properties_tab_changed(self, _index: int) -> None:
        # Changing workspace must never modify authored data. It only switches
        # the viewport presentation and which object the shared gizmo moves.
        briefing_active = self.properties_tabs.currentIndex() == self.briefing_tab_index
        self.renderer_badge.set_viewport(self.briefing_preview if briefing_active else self.viewport)
        self.briefing_preview.setVisible(briefing_active)
        self.viewport.setVisible(not briefing_active)
        if briefing_active:
            self.briefing_preview.refresh()
        cockpit_selected = self._is_cockpit_tab_selected()
        cockpit_active = self._is_cockpit_tab_active()
        shape_tab_active = (
            hasattr(self, "collision_shape_tab_index")
            and self.properties_tabs.currentIndex()
            == self.collision_shape_tab_index)
        self._sync_workspace_overlay_visibility()
        self.viewport.set_cockpit_preview_active(cockpit_active)
        if hasattr(self, "toolbar_view_preset_combo"):
            self.toolbar_view_preset_combo.setEnabled(not cockpit_active)
        self._update_reset_view_controls()
        if hasattr(self, "transform_box"):
            self.transform_box.setVisible(
                not shape_tab_active and not briefing_active
                and self.properties_tabs.currentIndex() != self.ground_tab_index
                and self.properties_tabs.currentIndex() != self.wireframe_tab_index
                and not self._is_fx_tab_active())
            self.transform_box.setTitle(
                "Move Cockpit Camera" if cockpit_selected else "Move Element")
        if hasattr(self, "cockpit_runtime_aspect_combo"):
            self.viewport.set_cockpit_runtime_aspect(
                self.cockpit_runtime_aspect_combo.currentData())
        # Restore the unit-only viewport scope before Cockpit View loads its
        # own preview model.
        self._sync_fx_preview()
        self._sync_cockpit_preview_model()
        self._sync_gizmo_camera()
        if cockpit_active:
            self.viewport.setFocus(Qt.FocusReason.OtherFocusReason)
        if not self._syncing:
            self._sync_all()
        self._sync_collision_shape_weapon_block()
        tree = {
            self.fire_points_tab_index: self.fire_point_tree,
            self.gun_points_tab_index: self.gun_point_tree,
        }.get(_index)
        if tree is not None and tree.topLevelItemCount():
            tree.clearSelection()
            tree.setCurrentItem(tree.topLevelItem(0))

    # -- Destruction FX preview -------------------------------------------------------

    def _is_fx_tab_active(self) -> bool:
        return (hasattr(self, "fx_tab_index")
                and self.properties_tabs.currentIndex() == self.fx_tab_index)

    def _fx_unit_radius(self) -> float:
        # StartDestFX scales dest_fx offsets by the unit's vanilla radius.
        legacy = self.project.legacy
        return (legacy.radius if legacy is not None
                else effective_runtime_radius(self.project))

    def _fx_unit_scale_rotation(self):
        """Preview scale and Euler rotation (degrees) of the unit."""

        return ((self.project.model_scale_x, self.project.model_scale_y,
                 self.project.model_scale_z),
                (self.project.model_rotation_x, self.project.model_rotation_y,
                 self.project.model_rotation_z))

    def _fx_model_diameter(self, scale) -> float:
        """Diameter of the bounding sphere of the unit's normal model, scale applied.

        It sizes the ground decal (size_by_model_%). 0 while no model is loaded.
        """

        return self.viewport.owner_model_sphere_diameter(self._current_owner, scale)

    def _fx_unit_transform(self):
        """Preview scale/rotation of the unit as a matrix (static preview)."""

        scale, rotation = self._fx_unit_scale_rotation()
        columns = [visual_transform_point(axis, scale, rotation)
                   for axis in ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))]
        return tuple(tuple(columns[column][row] for column in range(3))
                     for row in range(3))

    def _build_fx_catalog(self, text: str, objects=None):
        """FX vehicles for dest_fx previews: game catalog plus this script."""

        catalog: dict[int, fx.LegacyFxVehicle] = {}
        if objects is None:
            objects = getattr(self.game_object_browser, "objects", None) or []
        for obj in objects:
            if obj.kind == "vehicle":
                catalog[obj.object_id] = fx.legacy_vehicle_from_text(
                    obj.object_id, obj.effective_text, str(obj.source))
        lines = text.splitlines()
        for block in find_script_blocks(text):
            if block.kind == "new_vehicle" and block.complete                     and block.object_id not in catalog:
                body = "\n".join(lines[block.start_line:block.end_line + 1])
                catalog[block.object_id] = fx.legacy_vehicle_from_text(
                    block.object_id, body)
        return catalog

    def _build_fx_weapons(self, text: str, objects=None):
        """Weapons a cluster can spawn: the game catalog plus this script."""

        weapons: dict[int, fx.WeaponDef] = {}
        if objects is None:
            objects = getattr(self.game_object_browser, "objects", None) or []
        for obj in objects:
            if obj.kind == "weapon":
                weapons[obj.object_id] = fx.weapon_def_from_text(
                    obj.object_id, obj.effective_text)
        lines = text.splitlines()
        for block in find_script_blocks(text):
            if block.kind == "new_weapon" and block.complete:
                body = "\n".join(lines[block.start_line:block.end_line + 1])
                weapons[block.object_id] = fx.weapon_def_from_text(
                    block.object_id, body + "\n")
        return weapons

    def _build_fx_debuffs(self, text: str):
        """Debuff profiles of the game scripts plus this script."""

        debuffs: dict[int, fx.DebuffSpec] = {}
        try:
            from map_editor import bootstrap
            from map_editor.core.script_catalog import script_texts
            install = bootstrap.installation()
            if install is not None:
                for body in script_texts(install.folder("scripts")):
                    debuffs.update(fx.debuff_profiles_from_text(body))
        except (OSError, ValueError, RuntimeError, TypeError):
            pass
        debuffs.update(fx.debuff_profiles_from_text(text))
        return debuffs

    def _build_fx_decals(self, text: str):
        """Ground decal profiles (new_decal) of the game scripts plus this script."""

        decals: dict[int, fx.DecalProfile] = {}
        try:
            from map_editor import bootstrap
            from map_editor.core.script_catalog import script_texts
            install = bootstrap.installation()
            if install is not None:
                for body in script_texts(install.folder("scripts")):
                    decals.update(fx.decal_profiles_from_text(body))
        except (OSError, ValueError, RuntimeError, TypeError):
            pass
        decals.update(fx.decal_profiles_from_text(text))
        return decals

    def _fx_unit_phases(self, text: str, block: ScriptBlock,
                        reference: VehicleModelReference) -> fx.UnitPhases:
        """Death models of the definition, resolved like the engine."""

        def value(key: str) -> str:
            return fx.top_level_value(
                text, block.start_line, block.end_line, key) or ""

        def state(name: str) -> fx.VisualRef:
            # Engine defaults when the key is absent: vp_dead 4, vp_megadeth 2.
            vp = (reference.vp_normal or 0 if name == "normal"
                  else fx.script_int(value(f"vp_{name}"),
                                     {"dead": 4, "megadeth": 2}.get(name, 0)))
            return fx.VisualRef(vp=vp, mesh3ds=value(f"3ds_{name}"),
                                base_path=value(f"base_{name}"))

        weapon = block.kind.endswith("weapon")
        model = value("model").casefold()
        scale, euler = self._fx_unit_scale_rotation()
        look = fx.unit_look(
            fx.top_level_values(text, block.start_line, block.end_line),
            weapon=weapon)
        unit = fx.UnitPhases(
            normal=state("normal"), dead=state("dead"),
            megadeth=state("megadeth"), model=model, weapon=weapon,
            transform=self._fx_unit_transform(),
            overeof=(self.project.overeof
                     if self.project.overeof_enabled else 0.0),
            scale=scale, euler=euler, **look)
        if unit.flies:
            unit = replace(unit, altitude=fx.DEFAULT_FLIGHT_ALTITUDE)
        elif weapon:
            # Artillery shells fall from their arc height, other weapons from 550.
            unit = replace(unit, altitude=unit.altitude or fx.WEAPON_LAUNCH_HEIGHT)
        return unit

    def _sync_wireframe_current(self, text: str, block: ScriptBlock) -> None:
        """Give the Wireframe tab the file named by ``wireframe =`` of the loaded definition."""

        from entity_designer.wireframe_panel import resolve_wireframe_path

        value = fx.top_level_value(text, block.start_line, block.end_line, "wireframe")
        self.wireframe_panel.set_current_wireframe(
            resolve_wireframe_path(
                value, self._active_script_path, self._active_base_path))

    def _load_fx_context(self, text: str, block: ScriptBlock,
                         reference: VehicleModelReference, *,
                         keep_view: bool = False, objects=None) -> None:
        """Show one loaded definition in the Destruction FX preview."""

        self._fx_catalog = self._build_fx_catalog(text, objects)
        self._sync_wireframe_current(text, block)
        if not keep_view:
            self._fx_changed_assets.clear()
        if not keep_view or self._fx_source_from_catalog:
            self._fx_script_sources = set()
            if self._fx_source_from_catalog:
                from map_editor import bootstrap
                from map_editor.core.script_catalog import script_texts
                scripts = bootstrap.installation().folder("scripts")
                self._fx_script_sources.update(str((scripts / source).resolve())
                                               for _body, source in script_texts(scripts, with_source=True))
                self._fx_script_sources.update(str(path.resolve()) for path in
                                               (bootstrap.find_ci(scripts, "Startup.cfg"),
                                                bootstrap.find_ci(scripts, "startup.scr"))
                                               if path is not None and path.is_file())
        unit = self._fx_unit_phases(text, block, reference)
        if keep_view and (unit.weapon or self.fx_panel.unit.flies):
            unit = replace(unit, altitude=self.fx_panel.unit.altitude)
        self.fx_panel.set_context(
            fx.CONTEXT_WEAPON if unit.weapon else fx.CONTEXT_VEHICLE,
            fx.extract_fx_document(text, block.start_line, block.end_line),
            catalog=self._fx_catalog, unit=unit, keep_view=keep_view,
            weapons=self._build_fx_weapons(text, objects),
            debuffs=self._build_fx_debuffs(text),
            decals=self._build_fx_decals(text),
            settings=self._fx_effect_settings())
        origins = [(self._active_script_path, block.start_line, block.end_line)]
        if self._fx_source_from_catalog:
            candidates = objects if objects is not None else self.game_object_browser.objects
            source = next((obj for obj in candidates
                           if obj.object_id == block.object_id
                           and f"new_{obj.kind}" == block.kind), None)
            origins = getattr(source, "body_sources", ())
        self.fx_panel.set_script_sources(origins if self._active_script_path else ())
        self._fx_update_watcher()
        if not keep_view:
            # A fresh definition: what is on disk now is what the tab shows.
            self._fx_script_snapshot = self._fx_script_values(text, block, reference)
            self._fx_loaded_signatures = self._fx_signatures(self._fx_tracked)
            self._fx_failed_signatures = None
        self._sync_fx_preview()

    def _fx_script_values(self, text: str, block: ScriptBlock,
                          reference: VehicleModelReference) -> dict:
        """The script values the unit preview depends on (see _fx_apply_script_changes)."""

        values = {
            "scale": (reference.scale_x, reference.scale_y, reference.scale_z),
            "rotation": (reference.rotation_x, reference.rotation_y, reference.rotation_z),
            "overeof": None, "legacy": None, "legacy_radius": None}
        try:
            legacy, _compounds, _warnings = import_collision_block(text, block, OPENNEOUA)
            values["legacy"] = legacy
            values["legacy_radius"] = legacy.radius if legacy is not None else None
            if not block.kind.endswith("weapon"):
                values["overeof"] = import_overeof_block(text, block)
        except CollisionScriptError:
            pass
        return values

    def _fx_apply_script_changes(self, text: str, block: ScriptBlock,
                                 reference: VehicleModelReference) -> None:
        """Bring the project in line with script edits that change the preview.

        scale/rotation, overeof and radius are copied only when the script
        value itself changed since the last load, so values the user is
        trying out in the Designer are not overwritten by a reload.
        """

        current = self._fx_script_values(text, block, reference)
        previous = self._fx_script_snapshot
        self._fx_script_snapshot = current
        if previous is None:
            return
        project = self.project
        changed = False
        if current["scale"] != previous["scale"]:
            project.model_scale_x, project.model_scale_y, project.model_scale_z = current["scale"]
            changed = True
        if current["rotation"] != previous["rotation"]:
            (project.model_rotation_x, project.model_rotation_y,
             project.model_rotation_z) = current["rotation"]
            changed = True
        if current["overeof"] != previous["overeof"] and current["overeof"] is not None:
            project.overeof_enabled, project.overeof = current["overeof"]
            changed = True
        if current["legacy_radius"] != previous["legacy_radius"]:
            project.legacy = current["legacy"]
            changed = True
        if changed:
            self._sync_all()

    def reload_fx_from_file(self) -> None:
        """Read the definition again from disk and refresh the preview."""

        if self.fx_panel.has_unsaved_text:
            self._fx_failed_signatures = self._fx_signatures(self._fx_tracked)
            self.statusBar().showMessage(
                "Source files changed. Save or revert the effect-code drafts before reloading.", 6000)
            return

        if (self._active_script_path is None or self._active_script_id is None
                or self._active_model_reference is None):
            self.statusBar().showMessage(
                "Load a vehicle or weapon from a script before reloading.", 5000)
            return
        kind = self._active_script_kind
        objects = None
        # Taken before reading: a save during the read is caught by the next poll.
        snapshot = self._fx_signatures(self._fx_tracked)
        try:
            if self._fx_source_from_catalog:
                # Game catalog definitions merge every script of the game.
                from studio.assets.catalog import load_game_objects
                from map_editor import bootstrap
                objects = load_game_objects(bootstrap.installation())
                match = next(
                    (obj for obj in objects
                     if f"new_{obj.kind}" == kind
                     and obj.object_id == self._active_script_id), None)
                if match is None:
                    raise CollisionScriptError(
                        f"{kind} {self._active_script_id} is no longer in the game scripts.")
                text = match.effective_text
            else:
                text, _encoding, _bom = read_script_file(self._active_script_path)
            block = _resolve_patch_target_block(
                text, kind, self._active_script_id)
        except (OSError, UnicodeError, CollisionScriptError, RuntimeError) as exc:
            # Not retried until a file changes again (the poll would loop).
            self._fx_failed_signatures = snapshot
            self.statusBar().showMessage(f"Reload failed: {exc}", 8000)
            return
        if self._fx_changed_assets:
            try:
                self._reload_fx_assets()
            except (OSError, ValueError, RuntimeError) as exc:
                self._fx_failed_signatures = snapshot
                self.statusBar().showMessage(f"Asset reload failed: {exc}", 8000)
                return
        reference = next((ref for ref in script_model_references(text)
                          if ref.block.kind == kind and ref.block.object_id == self._active_script_id),
                         self._active_model_reference)
        self._active_model_reference = reference
        self._fx_apply_script_changes(text, block, reference)
        self._load_fx_context(text, block, reference,
                              keep_view=True, objects=objects)
        self._fx_failed_signatures = None
        self._fx_loaded_signatures = {
            path: snapshot[path] if path in snapshot else self._fx_file_signature(path)
            for path in self._fx_tracked}
        self.statusBar().showMessage(
            f"Visuals & FX reloaded from {self._active_script_path.name}.", 4000)

    def _fx_dependency_paths(self):
        """Collect loaded model, animation and texture sources plus external FX assets."""
        paths = set()
        families = [self.family]
        families.extend(helper._family_ref for helper in self.viewport._fx_external_cache.values()
                        if helper is not None)
        for family in families:
            if family is None:
                continue
            paths.update(str(path.resolve()) for path in
                         (family.base_path, family.setbas_path, family.external_palette_path) if path)
            paths.update(str(dep.resolved_path.resolve()) for dep in family.dependencies
                         if dep.resolved_path is not None)
            paths.update(str(path.resolve()) for path in family.effect_override_paths.values())
        unit = self.fx_panel.unit
        visuals = [unit.normal, unit.dead, unit.megadeth, unit.genesis, unit.launch]
        values = []
        for item in self.fx_panel.document.items:
            if isinstance(item, fx.FxBlock):
                spec = fx.interpret_block(item, self.fx_panel.context)
                visuals.extend(spec.visuals)
                if spec.physical_model is not None:
                    visuals.append(spec.physical_model)
        # Ground decal textures come from the linked new_decal profiles.
        for owner in (unit, *(child.unit for child in
                              fx.cluster_chain(unit, self.fx_panel.weapons))):
            profile = self.fx_panel.decals.get(owner.ground_decal_id)
            if profile is not None and profile.spec.texture:
                values.append(profile.spec.texture)
        if unit.decoration is not None:
            visuals.append(unit.decoration.visual)
        if unit.tracer is not None:
            values.append(unit.tracer.mesh)
        samples = [spec.sample for spec in unit.sounds.values() if spec.sample]
        for item in self.fx_panel.document.items:
            if isinstance(item, fx.FxBlock):
                samples.extend(spec.sample for spec in fx.interpret_block(
                    item, self.fx_panel.context).sounds.values() if spec.sample)
            elif isinstance(item, fx.DestFxLine) and item.parsed is not None:
                vehicle = self.fx_panel.catalog.get(item.parsed.vehicle_id)
                if vehicle is not None:
                    samples.extend(spec.sample for spec in vehicle.sounds.values()
                                   if spec.sample)
        if unit.cluster is not None and unit.cluster.sound is not None:
            samples.append(unit.cluster.sound.sample)
        for sample in samples:
            path = self._resolve_fx_sound(sample)
            if path is not None:
                paths.add(str(path.resolve()))
        for child in fx.cluster_chain(unit, self.fx_panel.weapons):
            samples = [spec.sample for spec in child.unit.sounds.values() if spec.sample]
            for sample in samples:
                path = self._resolve_fx_sound(sample)
                if path is not None:
                    paths.add(str(path.resolve()))
            visuals.extend((child.unit.normal, child.unit.dead, child.unit.megadeth,
                            child.unit.launch))
            if child.unit.tracer is not None:
                values.append(child.unit.tracer.mesh)
            if child.unit.decoration is not None:
                visuals.append(child.unit.decoration.visual)
        for visual in visuals:
            values.extend(value for value in (visual.mesh3ds, visual.base_path) if value)
        for value in values:
            path = _resolve_data_asset_path(value, self._active_script_path, self._active_base_path)
            if path is not None:
                paths.add(str(path.resolve()))
        return paths

    @staticmethod
    def _fx_file_signature(path):
        try:
            stat = Path(path).stat()
            return stat.st_mtime_ns, stat.st_size
        except OSError:
            return None

    def _fx_update_watcher(self) -> None:
        """Always watch the active FX definition and its loaded dependencies."""
        watched = self._fx_watcher.files() + self._fx_watcher.directories()
        if watched:
            self._fx_watcher.removePaths(watched)
        self._fx_watch_signatures = {}
        if not self._is_fx_tab_active() or self._active_script_path is None:
            self._fx_reload_timer.stop()
            self._fx_poll_timer.stop()
            return
        paths = {str(self._active_script_path.resolve())} | self._fx_script_sources
        paths.update(str(Path(vehicle.source).resolve()) for vehicle in self._fx_catalog.values()
                     if vehicle.source)
        self._fx_asset_paths = self._fx_dependency_paths()
        paths.update(self._fx_asset_paths)
        self._fx_tracked = set(paths)
        self._fx_watch_signatures = {path: self._fx_file_signature(path) for path in paths}
        if not self._fx_poll_timer.isActive():
            self._fx_poll_timer.start()
        # Files edited while the tab was away are picked up right now.
        QTimer.singleShot(0, self._fx_poll)
        existing = [path for path in paths if Path(path).is_file()]
        directories = {str(Path(path).parent) for path in paths if Path(path).parent.is_dir()}
        if existing or directories:
            self._fx_watcher.addPaths(existing + sorted(directories))

    def _fx_signatures(self, paths) -> dict:
        return {path: self._fx_file_signature(path) for path in paths}

    def _fx_poll(self) -> None:
        """Reload when a tracked file differs from what the tab loaded."""

        if (not self._is_fx_tab_active() or self._active_script_path is None
                or self._fx_reload_timer.isActive()):
            return
        current = self._fx_signatures(self._fx_tracked)
        if current == self._fx_loaded_signatures or current == self._fx_failed_signatures:
            return
        for path, signature in current.items():
            if (self._fx_loaded_signatures.get(path) != signature
                    and path in self._fx_asset_paths):
                self._fx_changed_assets.add(path)
        self._fx_reload_timer.start()

    def _fx_watched_directory_changed(self, directory):
        # Atomic saves replace files; directory notifications keep their watches alive.
        for path, signature in tuple(self._fx_watch_signatures.items()):
            if str(Path(path).parent) == directory and self._fx_file_signature(path) != signature:
                self._fx_watched_file_changed(path)

    def _fx_watched_file_changed(self, path: str) -> None:
        if not self._is_fx_tab_active():
            return
        if path in self._fx_asset_paths:
            self._fx_changed_assets.add(path)
        self._fx_watch_signatures[path] = self._fx_file_signature(path)
        self._fx_reload_timer.start()

    def _reload_fx_assets(self):
        """Refresh rendered assets through the existing loaders, preserving authoring data."""
        family = self.family
        if family is not None and family.base_path is not None:
            path = family.base_path
            if path.suffix.casefold() == ".3ds":
                refreshed = _load_3ds_family(path)
            else:
                refreshed = _public_dependency("load_asset_family", load_asset_family)(
                    path, extra_roots=family.search_roots, overrides=family.overrides,
                    setbas=family.setbas_path)
            references = list(self._script_unit_references)
            visual_reference = self._script_unit_visual_reference
            unit_mode = self._script_unit_mode
            self.family = refreshed
            if family.setbas_path and str(family.setbas_path.resolve()) in self._fx_changed_assets:
                self._vp_embedded = reconstruct_embedded_vps(family.setbas_path)
                self._vp_table, self._vp_table_source, _warnings = runtime_vp_table(
                    family.setbas_path, self._vp_embedded)
            with QSignalBlocker(self.model_tree):
                self._fill_models(refreshed)
                if unit_mode:
                    self._fill_script_units(references, active_visual_reference=visual_reference)
                    self._select_script_unit_item(self._active_model_reference)
            self._load_viewport_scope(self.viewport.visible_owners())
            self._sync_animation_controls()
        for helper in self.viewport._fx_external_cache.values():
            if helper is not None:
                helper.close()
        self.viewport._fx_external_cache.clear()
        self.viewport._fx_decal_cache.clear()
        self.viewport._fx_emitter_cache.clear()
        self.viewport._fx_tinted_stage_cache.clear()
        self._fx_changed_assets.clear()

    def _fx_document_edited(self) -> None:
        """Resolve newly referenced VP models through the normal preview path."""
        self._sync_fx_preview()

    def _fx_draft_state_changed(self) -> None:
        if not self.fx_panel.has_unsaved_text:
            self._fx_failed_signatures = None

    def _fx_sources_saved(self, paths) -> None:
        # Our own save is already in the preview. Reloading it would erase
        # the text editor's undo history and move its cursor unnecessarily.
        for path in paths:
            key = str(Path(path).resolve())
            signature = self._fx_file_signature(path)
            self._fx_loaded_signatures[key] = signature
            self._fx_watch_signatures[key] = signature
        self._fx_failed_signatures = None

    def _sync_fx_preview(self) -> None:
        """Enter/leave the FX viewport mode and load the effect VP owners."""

        if not hasattr(self, "fx_panel"):
            return
        active = self._is_fx_tab_active()
        if active and self._fx_edit_camera is None:
            self._fx_edit_camera = self.viewport._camera_state()
        self.fx_panel.transport_bar.setVisible(active)
        self._fx_owner_map = {
            vp: owner for vp in self.fx_panel.required_vps()
            if (owner := self._owner_for_vp(vp)) is not None}
        if active and self.family is not None:
            owners = set(self._fx_owner_map.values())
            if self._current_owner:
                owners.add(self._current_owner)
            if owners != (self.viewport.visible_owners() or set()):
                self._fx_scope = owners
                self._load_viewport_scope(owners)
        else:
            if self.fx_panel.is_playing():
                self.fx_panel.play_button.setChecked(False)
            if self._fx_scope is not None:
                self._fx_scope = None
                self._load_viewport_scope(
                    {self._current_owner} if self._current_owner else None)
        if not active and self._fx_edit_camera is not None:
            self.viewport._set_camera_state(self._fx_edit_camera)
            self._fx_edit_camera = None
            self._fx_fitted_definition = None
            self.viewport.update()
        scale, euler = self._fx_unit_scale_rotation()
        self.fx_panel.set_scene(
            (0.0, self.viewport.overeof_preview_offset, 0.0), 0.0,
            self._fx_unit_radius(), transform=self._fx_unit_transform(),
            overeof=self.project.overeof if self.project.overeof_enabled else 0.,
            scale=scale, euler=euler, model_diameter=self._fx_model_diameter(scale))
        self._fx_update_watcher()
        definition = (self._active_script_path, self._active_script_kind,
                      self._active_script_id)
        if (active and self.family is not None
                and definition != self._fx_fitted_definition):
            # Effects are usually far larger than the unit: frame them once
            # on entry or a definition change; later camera moves stay manual.
            self._fx_fitted_definition = definition
            self._fit_fx_view()
        self._push_fx_frame()
        self._sync_animation_controls()

    def _load_viewport_scope(self, owners) -> None:
        """Reload the visible owners without moving the camera."""

        camera = self.viewport._camera_state()
        home = self.viewport._home_camera_state
        self.viewport.load_family(
            self.family, owners, keep_camera=True,
            primary_owner=self._current_owner)
        self.viewport.set_model_preview_scale(
            self.project.model_scale_x, self.project.model_scale_y,
            self.project.model_scale_z)
        self.viewport.set_ground_alignment(
            self.project.target_category == VEHICLE,
            self.project.overeof_enabled, self.project.overeof)
        self.viewport._set_camera_state(camera)
        self.viewport._home_camera_state = home

    def _fit_fx_view(self) -> None:
        simulation = self.fx_panel.simulation
        extent = simulation.extent() if simulation is not None else None
        if extent is None:
            return
        center, radius = extent
        if self.fx_panel.follow_unit.isChecked():
            bounds = self.viewport._owner_bounds.get(self._current_owner)
            if bounds is not None:
                center = tuple((bounds[axis] + bounds[axis + 3]) * .5 for axis in range(3))
                # Leave room around the unit while avoiding the full debris envelope.
                radius = max(max(bounds[axis + 3] - bounds[axis] for axis in range(3)) * .75,
                             fx.FOLLOW_MIN_RADIUS, self.fx_panel.unit.look_reach())
        self.viewport._center = center
        self.viewport._scale = 1.0 / radius
        self.viewport._zoom = 1.0
        self.viewport._pan = QPointF(0.0, 0.0)
        if self._is_fx_tab_active():
            self._push_fx_frame()
        self.viewport._fx_home_camera = self.viewport._camera_state()
        self.viewport.update()

    def _fx_palette(self):
        """(normclr, invclr) of the palette flash playing now, or None."""

        palettes = self.fx_panel.palette_effects()
        if not palettes:
            return None
        custom, powers = self._fx_color_settings()
        return fx.palette_colors(palettes, custom, powers)

    def _fx_ini_settings(self):
        """(custom palette, slot powers, effect settings) of the game's OpenNeoUA.ini."""

        from map_editor import bootstrap
        install = bootstrap.installation()
        ini = None
        if install is not None:
            ini = bootstrap.find_ci(Path(install.data), "OpenNeoUA.ini")
        if ini is None:
            root = _data_root_for_path(self._active_script_path, self._active_base_path)
            ini = bootstrap.find_ci(root, "OpenNeoUA.ini") if root is not None else None
        signature = (str(ini), self._fx_file_signature(ini)) if ini is not None else None
        cache = getattr(self, "_fx_color_cache", None)
        if cache is None or cache[0] != signature:
            settings = (({}, {}), fx.EffectSettings())
            if ini is not None:
                try:
                    text = Path(ini).read_text(encoding="utf-8", errors="replace")
                    settings = (fx.parse_color_settings(text),
                                fx.parse_effect_settings(text))
                except OSError:
                    pass
            self._fx_color_cache = (signature, settings)
        return self._fx_color_cache[1]

    def _fx_color_settings(self):
        """Custom palette slots and slot powers of the game's OpenNeoUA.ini."""

        return self._fx_ini_settings()[0]

    def _fx_effect_settings(self):
        """Global fades, palette limits and master volume of OpenNeoUA.ini."""

        return self._fx_ini_settings()[1]

    def _resolve_fx_sound(self, name: str) -> Path | None:
        """Sound sample named by a script, relative to the game Data folder."""

        raw = name.strip().replace("\\", "/")
        if not raw:
            return None
        relative = raw[5:] if raw.casefold().startswith("data/") else raw
        roots = []
        from map_editor import bootstrap
        install = bootstrap.installation()
        if install is not None:
            roots.append(Path(install.data))
        root = _data_root_for_path(self._active_script_path, self._active_base_path)
        if root is not None:
            roots.append(root)
        for folder in roots:
            candidate = folder / relative
            if candidate.is_file():
                return candidate
        return None

    def _load_fx_external_model(self, value: str):
        """Family of an external 3DS/BASE model named by a script path."""

        path = _resolve_data_asset_path(
            value, self._active_script_path, self._active_base_path)
        if path is None:
            return None
        try:
            if path.suffix.casefold() == ".3ds":
                family = _load_3ds_family(path)
            else:
                family = _public_dependency("load_asset_family", load_asset_family)(path)
            QTimer.singleShot(0, self._fx_update_watcher)
            return family
        except (OSError, ValueError, RuntimeError):
            return None

    def _push_fx_frame(self) -> None:
        """Send the current preview time of the FX tab to the viewport."""

        if not hasattr(self, "fx_panel"):
            return
        if not self._is_fx_tab_active():
            if self.viewport.fx_preview_active:
                self.viewport.set_fx_preview(active=False)
            return
        panel = self.fx_panel
        items = panel.preview_items()
        tracked_unit = next((item for item in items if item.kind == "unit"), None)
        # A model state whose row is unchecked in the list is not drawn.
        hidden_states = panel.hidden_states
        show_unit = "normal" not in hidden_states
        if hidden_states:
            items = [item for item in items
                     if _fx_item_state(item) not in hidden_states]
        # The unit is drawn by its phase items (normal -> dead -> megadeth)
        # instead of the static model whenever the preview plays a sequence.
        phased = panel.simulation is not None and panel.simulation.shows_unit
        particles = []
        if panel.simulation is not None:
            emitters = {
                vp: self.viewport.fx_emitter_specs(owner)
                for vp, owner in self._fx_owner_map.items()}
            particles = panel.simulation.particles_at(panel.time_ms, emitters)
            if not show_unit:
                # Decorations and the tracer belong to the normal model and hide with it.
                particles = [sprite for sprite in particles if sprite.source >= 0]
        self.viewport.set_fx_preview(
            active=True, items=items, owner_by_vp=self._fx_owner_map,
            unit_owners={self._current_owner} if self._current_owner else set(),
            boxes=panel.offset_boxes(), time_ms=panel.time_ms,
            show_unit=show_unit and not phased,
            weapon_context=panel.context == fx.CONTEXT_WEAPON,
            data_root=_data_root_for_path(
                self._active_script_path, self._active_base_path),
            highlight=panel.highlight, particles=particles,
            external_loader=self._load_fx_external_model,
            palette=self._fx_palette(), shake=panel.shake_angles())
        self.viewport.set_fx_follow_unit(
            panel.follow_unit.isChecked(), tracked_unit, self._current_owner)

    def _toggle_cockpit_camera(self) -> None:
        if self._syncing or self.project.target_category != VEHICLE:
            return
        self._vehicle_preview_active_edits.clear()
        self._push_undo()
        self.project.cockpit_camera_enabled = (
            not self.project.cockpit_camera_enabled)
        self._set_modified()
        self._sync_all()

    def _cockpit_value_changed(self, axis: str, value: float) -> None:
        if self._syncing or self.project.target_category != VEHICLE:
            return
        value = float(value)
        if not math.isfinite(value) or axis not in {"x", "y", "z"}:
            return
        field = f"cockpit_camera_offset_{axis}"
        if abs(float(getattr(self.project, field)) - value) < 1e-9:
            return
        self._begin_vehicle_preview_edit(f"cockpit_{axis}")
        setattr(self.project, field, value)
        self._set_modified()
        self._sync_all()

    def _reset_cockpit_camera(self) -> None:
        if self.project.target_category != VEHICLE:
            return
        values = (
            self.project.cockpit_camera_enabled,
            self.project.cockpit_camera_offset_x,
            self.project.cockpit_camera_offset_y,
            self.project.cockpit_camera_offset_z,
        )
        if values == (False, 0.0, 0.0, 0.0):
            return
        self._vehicle_preview_active_edits.clear()
        self._push_undo()
        self.project.cockpit_camera_offset_x = 0.0
        self.project.cockpit_camera_offset_y = 0.0
        self.project.cockpit_camera_offset_z = 0.0
        self._set_modified()
        self._sync_all()

    def _cockpit_nudge(self, direction) -> None:
        if not self._is_cockpit_tab_active():
            return
        step = float(self.move_strength_spin.value())
        self._vehicle_preview_active_edits.clear()
        self._push_undo()
        # Moving the cockpit camera is the explicit request for the offset:
        # enable its output so the edit is not silently discarded on export.
        self.project.cockpit_camera_enabled = True
        self.project.cockpit_camera_offset_x += float(direction[0]) * step
        self.project.cockpit_camera_offset_y += float(direction[1]) * step
        self.project.cockpit_camera_offset_z += float(direction[2]) * step
        self._set_modified()
        self._sync_all()

    def _begin_vehicle_preview_edit(self, field: str) -> None:
        if field not in self._vehicle_preview_active_edits:
            self._push_undo()
            self._vehicle_preview_active_edits.add(field)

    def _finish_vehicle_preview_edit(self, field: str) -> None:
        self._vehicle_preview_active_edits.discard(field)

    def add_fire_point(self) -> None:
        if self.project.target_category != VEHICLE:
            return
        if self.project.fire_points_enabled:
            minimum, maximum = _project_num_weapons_range(self.project)
            count = max(1, maximum)
            if count >= 255:
                self.statusBar().showMessage(
                    "Vanilla num_weapons is already at its maximum (255).",
                    4500)
                return
        self._vehicle_preview_active_edits.clear()
        self._push_undo()
        if not self.project.fire_points_enabled:
            # fire_x/y/z + num_weapons are one vanilla rack. Add means one
            # authored muzzle, not resurrection of a stale disabled count.
            self.project.fire_points_enabled = True
            self.project.num_weapons = 1
            self.project.num_weapons_max = 1
            self._selected_fire_point = 0
        else:
            if minimum == maximum:
                self.project.num_weapons = count + 1
                self.project.num_weapons_max = count + 1
            else:
                self.project.num_weapons_max = count + 1
            self._selected_fire_point = count
        self._selected = -1
        self._selected_gun_point = -1
        self._set_modified()
        self._sync_all()

    def remove_fire_point(self) -> None:
        if (self.project.target_category != VEHICLE
                or not self.project.fire_points_enabled):
            return
        minimum, maximum = _project_num_weapons_range(self.project)
        count = max(1, maximum)
        index = self._selected_fire_point
        if not (0 <= index < count):
            return
        self._vehicle_preview_active_edits.clear()
        self._push_undo()
        if count <= 1:
            # Keep authored fire_x/y/z values in memory so re-adding a point
            # is non-destructive, but omit the whole Fire Point block from
            # export/overwrite until the user adds it again.
            self.project.fire_points_enabled = False
            self.project.num_weapons = 1
            self.project.num_weapons_max = 1
            self._selected_fire_point = -1
        else:
            if minimum == maximum:
                self.project.num_weapons = count - 1
                self.project.num_weapons_max = count - 1
            else:
                self.project.num_weapons_max = max(minimum, count - 1)
            self._selected_fire_point = min(
                index, _fire_point_preview_count(self.project) - 1)
            self.statusBar().showMessage(
                "Vanilla Fire Points share one symmetric rack; removing a "
                "point re-spaced the remaining muzzles.", 5000)
        self._set_modified()
        self._sync_all()

    def _reset_fire_point_position(self) -> None:
        """Reset the authored vanilla Fire Point rack to the model origin."""

        if self.project.target_category != VEHICLE:
            return
        was_enabled = self.project.fire_points_enabled
        if (was_enabled
                and self.project.fire_x == 0.0
                and self.project.fire_y == 0.0
                and self.project.fire_z == 0.0):
            return
        self._vehicle_preview_active_edits.clear()
        self._push_undo()
        self.project.fire_points_enabled = True
        self.project.fire_x = 0.0
        self.project.fire_y = 0.0
        self.project.fire_z = 0.0
        if not was_enabled:
            self.project.num_weapons = 1
            self.project.num_weapons_max = 1
        if self._selected_fire_point < 0:
            self._selected_fire_point = 0
        self._selected = -1
        self._selected_gun_point = -1
        self._set_modified()
        self._sync_all()

    def _fire_point_value_changed(self, field: str, value: float) -> None:
        if self._syncing or self.project.target_category != VEHICLE:
            return
        value = float(value)
        if not math.isfinite(value):
            return
        current = float(getattr(self.project, field))
        if abs(current - value) < 1e-9:
            return
        self._begin_vehicle_preview_edit(field)
        setattr(self.project, field, value)
        self.project.fire_points_enabled = True
        self._set_modified()
        self._sync_all()

    def _num_weapons_changed(self, value: int) -> None:
        if self._syncing or self.project.target_category != VEHICLE:
            return
        value = max(0, min(255, int(value)))
        old_min, old_max = _project_num_weapons_range(self.project)
        if value == old_min:
            return
        self._begin_vehicle_preview_edit("num_weapons")
        self.project.num_weapons = value
        if value == 0:
            self.project.num_weapons_max = 0
        elif old_min == old_max:
            self.project.num_weapons_max = value
        else:
            self.project.num_weapons_max = max(value, old_max)
        self.project.fire_points_enabled = True
        point_count = _fire_point_preview_count(self.project)
        if self._selected_fire_point >= point_count:
            self._selected_fire_point = point_count - 1
        self._set_modified()
        self._sync_all()

    def _num_weapons_max_changed(self, value: int) -> None:
        if self._syncing or self.project.target_category != VEHICLE:
            return
        value = max(0, min(255, int(value)))
        old_min, old_max = _project_num_weapons_range(self.project)
        if value == old_max:
            return
        self._begin_vehicle_preview_edit("num_weapons_max")
        if value == 0:
            self.project.num_weapons = 0
            self.project.num_weapons_max = 0
        else:
            if old_min == 0:
                self.project.num_weapons = 1
                old_min = 1
            self.project.num_weapons_max = max(old_min, value)
        self.project.fire_points_enabled = True
        point_count = _fire_point_preview_count(self.project)
        if self._selected_fire_point >= point_count:
            self._selected_fire_point = point_count - 1
        self._set_modified()
        self._sync_all()



    def add_gun_point(self):
        if self.project.target_category != VEHICLE:
            return
        scheme = (
            self._new_gun_point_scheme
            if self._new_gun_point_scheme in {"robo", "unit"}
            else "unit")
        family_count = sum(
            point.scheme == scheme for point in self.project.gun_points)
        if family_count >= 20:
            family_name = (
                "Vanilla robo_*" if scheme == "robo"
                else "OpenNeoUA unit_*")
            self.statusBar().showMessage(
                f"{family_name} supports at most 20 gun points.", 5000)
            return
        self._vehicle_preview_active_edits.clear()
        self._push_undo()
        point = GunPoint(scheme=scheme)
        self.project.gun_points.append(point)
        self.project.gun_points_enabled = True
        self._selected_gun_point = len(self.project.gun_points) - 1
        self._selected_fire_point = -1
        self._selected = -1
        self._set_modified()
        self._sync_all()

    def remove_gun_point(self):
        point = self._selected_gun()
        if point is None:
            return
        index = self._selected_gun_point
        self._vehicle_preview_active_edits.clear()
        self._push_undo()
        del self.project.gun_points[index]
        # Keep management enabled so Apply/Overwrite removes the old script
        # rows when the user intentionally deletes the final mount.
        self.project.gun_points_enabled = True
        self._selected_gun_point = min(
            index, len(self.project.gun_points) - 1)
        self._set_modified()
        self._sync_all()

    def _capture_loaded_cockpit_camera(self) -> None:
        """Snapshot source-authored cockpit coordinates for later restore."""

        self._loaded_cockpit_camera_enabled = bool(
            self.project.cockpit_camera_enabled)
        self._loaded_cockpit_camera_offset = (
            float(self.project.cockpit_camera_offset_x),
            float(self.project.cockpit_camera_offset_y),
            float(self.project.cockpit_camera_offset_z),
        )

    def _restore_loaded_cockpit_camera(self) -> None:
        """Restore cockpit X/Y/Z exactly as read from the source script."""

        if (self.project.target_category != VEHICLE
                or not self._loaded_cockpit_camera_enabled):
            return
        baseline = self._loaded_cockpit_camera_offset
        current = (
            float(self.project.cockpit_camera_offset_x),
            float(self.project.cockpit_camera_offset_y),
            float(self.project.cockpit_camera_offset_z),
        )
        if (self.project.cockpit_camera_enabled
                and all(abs(a - b) < 1e-9
                        for a, b in zip(current, baseline))):
            return
        self._vehicle_preview_active_edits.clear()
        self._push_undo()
        self.project.cockpit_camera_enabled = True
        (self.project.cockpit_camera_offset_x,
         self.project.cockpit_camera_offset_y,
         self.project.cockpit_camera_offset_z) = baseline
        self._set_modified()
        self._sync_all()

    def _capture_loaded_gun_points(self) -> None:
        """Snapshot source-authored mounts and turret limits for Reset All."""

        self._loaded_gun_points_enabled = bool(
            self.project.gun_points_enabled)
        self._loaded_gun_points = [
            point.clone() for point in self.project.gun_points]
        self._loaded_unit_gun_default_icon = (
            self.project.unit_gun_default_icon)
        self._loaded_gun_point_scheme = self._new_gun_point_scheme
        self._loaded_turret_limits = {
            vehicle_id: limits.clone()
            for vehicle_id, limits in self.project.turret_limits.items()
        }

    def _capture_tab_reset_baseline(self) -> None:
        """Remember the last explicit source/import state for tab Reset."""

        self._tab_reset_baseline = self.project.snapshot()
        self._tab_reset_gun_point_scheme = self._new_gun_point_scheme

    def _baseline_project(self) -> CollisionProject:
        baseline = CollisionProject()
        baseline.restore(self._tab_reset_baseline)
        return baseline

    @staticmethod
    def _script_tab_state(project: CollisionProject, group: str) -> tuple:
        """Compare authored values using the precision written to scripts.

        Disabled controls and the source block used for a turret are editor
        metadata, not distinct game parameters. Ignore them when checking
        whether the exported definition still differs from this project.
        """

        def authored_number(value: float) -> float:
            return float(_number(float(value)))

        if group == _SCRIPT_TAB_COLLISION:
            legacy = (
                None if project.legacy is None
                else int(round(project.legacy.radius)))
            compound = tuple(
                (authored_number(sphere.x), authored_number(sphere.y),
                 authored_number(sphere.z), authored_number(sphere.radius))
                for sphere in project.compound)
            shape_path = (
                project.collision_shape_path.replace("\\", "/").strip())
            return (
                bool(project.overeof_enabled),
                authored_number(project.overeof)
                if project.overeof_enabled else None,
                legacy, compound, shape_path,
            )
        if group == _SCRIPT_TAB_FIRE:
            if not project.fire_points_enabled:
                return (False,)
            return (
                True,
                authored_number(project.fire_x),
                authored_number(project.fire_y),
                authored_number(project.fire_z),
                *_project_num_weapons_range(project),
            )
        if group == _SCRIPT_TAB_GUN:
            authored_points = (
                project.gun_points if project.gun_points_enabled else [])
            points = tuple((
                point.scheme, authored_number(point.x),
                authored_number(point.y), authored_number(point.z),
                authored_number(point.dir_x), authored_number(point.dir_y),
                authored_number(point.dir_z), int(point.gun_type),
                point.name, point.icon,
            ) for point in authored_points)
            default_icon = (
                project.unit_gun_default_icon
                if project.gun_points_enabled else "")
            # The script catalog provides limits for every vehicle in the
            # file; only guns mounted here (or explicitly edited limits)
            # belong to this entity's Gun Points export status.
            referenced_types = {point.gun_type for point in authored_points}
            limits = tuple((
                vehicle_id, int(limit.side), int(limit.up),
                int(limit.down),
            ) for vehicle_id, limit in sorted(project.turret_limits.items())
                if limit.enabled and
                (vehicle_id in referenced_types or limit.dirty))
            return (points, default_icon, limits)
        if group == _SCRIPT_TAB_BRIEFING:
            return (
                bool(project.briefing_size_enabled),
                int(round(project.briefing_size))
                if project.briefing_size_enabled else None,
            )
        if group == _SCRIPT_TAB_COCKPIT:
            if not project.cockpit_camera_enabled:
                return (False,)
            return (
                True,
                authored_number(project.cockpit_camera_offset_x),
                authored_number(project.cockpit_camera_offset_y),
                authored_number(project.cockpit_camera_offset_z),
            )
        raise ValueError(group)

    def _changed_script_tabs(self) -> set[str]:
        """Return tabs whose script-authored state differs from the source."""

        baseline = self._baseline_project()
        groups = {_SCRIPT_TAB_COLLISION}
        if self.project.target_category == VEHICLE:
            groups.update((
                _SCRIPT_TAB_FIRE, _SCRIPT_TAB_GUN, _SCRIPT_TAB_COCKPIT, _SCRIPT_TAB_BRIEFING))
        return {
            group for group in groups
            if self._script_tab_state(self.project, group)
            != self._script_tab_state(baseline, group)
        }

    def _refresh_linked_script_baseline(
            self, applied_groups: set[str] | None = None) -> bool:
        """Re-read exported tabs without replacing untouched import state.

        The game catalog may have loaded an effective/merged definition while
        the actual saved file contains only its own blocks. Refreshing every
        tab from that file made unrelated Gun Points appear dirty after a
        Cockpit or Collision export. Only explicitly applied groups change
        their source baseline; skipped preview sections remain pending.
        """

        if (self._active_script_path is None
                or self._active_script_id is None
                or not self._active_script_kind):
            return False
        groups = (
            set(_SCRIPT_TAB_ORDER) if applied_groups is None
            else set(applied_groups))
        if not groups:
            return True
        try:
            text, _encoding, _bom = read_script_file(self._active_script_path)
            block = _resolve_patch_target_block(
                text, self._active_script_kind, self._active_script_id)
            baseline = self._baseline_project()
            if _SCRIPT_TAB_COLLISION in groups:
                baseline.legacy, baseline.compound, _warnings = (
                    import_collision_block(text, block, OPENNEOUA))
                baseline.collision_shape_path = import_collision_shape_reference(
                    text, block)
                if baseline.target_category == VEHICLE:
                    baseline.overeof_enabled, baseline.overeof = (
                        import_overeof_block(text, block))
            if baseline.target_category == VEHICLE:
                if _SCRIPT_TAB_FIRE in groups:
                    (baseline.fire_points_enabled, baseline.fire_x,
                     baseline.fire_y, baseline.fire_z, baseline.num_weapons,
                     baseline.num_weapons_max) = import_fire_points_block(
                        text, block)
                if _SCRIPT_TAB_GUN in groups:
                    (baseline.gun_points_enabled, baseline.gun_points,
                     baseline.unit_gun_default_icon) = import_gun_points_block(
                        text, block)
                    # Turret limits belong to their own vehicle definitions.
                    # A merged game-catalog import can contain angles sourced
                    # from other files: replace only limits actually edited
                    # and written by this workspace, not every catalog entry.
                    source_limits = import_turret_limits(text)
                    for vehicle_id, current in self.project.turret_limits.items():
                        if current.dirty:
                            if vehicle_id in source_limits:
                                baseline.turret_limits[vehicle_id] = (
                                    source_limits[vehicle_id])
                            else:
                                baseline.turret_limits.pop(vehicle_id, None)
                if _SCRIPT_TAB_BRIEFING in groups:
                    (baseline.briefing_size_enabled,
                     baseline.briefing_size) = import_briefing_size_block(
                        text, block)
                if _SCRIPT_TAB_COCKPIT in groups:
                    (baseline.cockpit_camera_enabled,
                     baseline.cockpit_camera_offset_x,
                     baseline.cockpit_camera_offset_y,
                     baseline.cockpit_camera_offset_z) = (
                        import_cockpit_camera_block(text, block))

            if _SCRIPT_TAB_GUN in groups:
                imported_schemes = gun_point_families_in_block(text, block)
                if len(imported_schemes) == 1:
                    baseline_scheme = next(iter(imported_schemes))
                elif "unit" in imported_schemes:
                    baseline_scheme = "unit"
                else:
                    baseline_scheme = self._tab_reset_gun_point_scheme
                self._tab_reset_gun_point_scheme = baseline_scheme
                self._loaded_gun_points_enabled = baseline.gun_points_enabled
                self._loaded_gun_points = [
                    point.clone() for point in baseline.gun_points]
                self._loaded_unit_gun_default_icon = (
                    baseline.unit_gun_default_icon)
                self._loaded_gun_point_scheme = baseline_scheme
                self._loaded_turret_limits = {
                    vehicle_id: limits.clone()
                    for vehicle_id, limits in baseline.turret_limits.items()
                }
                for vehicle_id, limits in self.project.turret_limits.items():
                    source = baseline.turret_limits.get(vehicle_id)
                    if source is not None:
                        limits.source_kind = source.source_kind
                        limits.source_name = source.source_name
                    limits.dirty = (
                        source is None or
                        (limits.enabled, limits.side if limits.enabled else 0,
                         limits.up if limits.enabled else 0,
                         limits.down if limits.enabled else 0)
                        != (source.enabled,
                            source.side if source.enabled else 0,
                            source.up if source.enabled else 0,
                            source.down if source.enabled else 0))
            if _SCRIPT_TAB_COCKPIT in groups:
                self._loaded_cockpit_camera_enabled = (
                    baseline.cockpit_camera_enabled)
                self._loaded_cockpit_camera_offset = (
                    baseline.cockpit_camera_offset_x,
                    baseline.cockpit_camera_offset_y,
                    baseline.cockpit_camera_offset_z,
                )
            self._tab_reset_baseline = baseline.snapshot()
            self._set_modified(bool(self._changed_script_tabs()))
            return True
        except (OSError, UnicodeError, CollisionScriptError):
            return False

    def _workspace_state(self, project: CollisionProject,
                         tab_index: int) -> tuple:
        def sphere_state(sphere):
            return None if sphere is None else (
                sphere.category, sphere.x, sphere.y, sphere.z,
                sphere.radius, sphere.visible,
            )

        if tab_index == self.ground_tab_index:
            return (project.overeof_enabled, project.overeof)
        if tab_index == self.collision_tab_index:
            return (
                sphere_state(project.legacy),
                tuple(sphere_state(sphere) for sphere in project.compound),
                project.sphere_isolation_active,
            )
        if tab_index == self.collision_shape_tab_index:
            return (
                project.collision_shape,
                project.collision_shape_path,
                project.collision_shape_file_path,
                project.collision_shape_owners,
                project.collision_shape_components,
                project.collision_shape_warnings,
            )
        if tab_index == self.fire_points_tab_index:
            return (
                project.fire_points_enabled,
                project.fire_x, project.fire_y, project.fire_z,
                project.num_weapons, project.num_weapons_max,
            )
        if tab_index == self.gun_points_tab_index:
            return (
                project.gun_points_enabled,
                project.unit_gun_default_icon,
                tuple((
                    point.scheme, point.x, point.y, point.z,
                    point.dir_x, point.dir_y, point.dir_z,
                    point.gun_type, point.name, point.icon,
                ) for point in project.gun_points),
                tuple((
                    vehicle_id, limits.enabled, limits.side, limits.up,
                    limits.down, limits.source_kind, limits.source_name,
                    limits.dirty,
                ) for vehicle_id, limits in sorted(
                    project.turret_limits.items())),
            )
        if tab_index == self.briefing_tab_index:
            return project.briefing_size_enabled, project.briefing_size
        if tab_index == self.cockpit_tab_index:
            return (
                project.cockpit_camera_enabled,
                project.cockpit_camera_offset_x,
                project.cockpit_camera_offset_y,
                project.cockpit_camera_offset_z,
            )
        return ()

    def _current_tab_has_changes(self) -> bool:
        if not hasattr(self, "properties_tabs"):
            return False
        tab_index = self.properties_tabs.currentIndex()
        baseline = self._baseline_project()
        return (
            self._workspace_state(self.project, tab_index)
            != self._workspace_state(baseline, tab_index)
        )

    def _reset_current_tab(self) -> None:
        """Revert only the active authoring workspace after confirmation."""

        if not hasattr(self, "properties_tabs"):
            return
        tab_index = self.properties_tabs.currentIndex()
        if not self._current_tab_has_changes():
            return
        tab_name = self.properties_tabs.tabText(tab_index)
        answer = QMessageBox.question(
            self, "Reset Current Tab",
            f"Reset all changes in the {tab_name} tab to the last "
            "loaded/imported state?\n\nChanges in the other tabs are kept.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return

        baseline = self._baseline_project()
        self._vehicle_preview_active_edits.clear()
        self._push_undo()

        if tab_index == self.ground_tab_index:
            self.project.overeof_enabled = baseline.overeof_enabled
            self.project.overeof = baseline.overeof
        elif tab_index == self.collision_tab_index:
            self.project.legacy = (
                baseline.legacy.clone() if baseline.legacy is not None
                else None)
            self.project.compound = [
                sphere.clone() for sphere in baseline.compound]
            self.project.sphere_isolation_active = (
                baseline.sphere_isolation_active)
            self._selected = min(
                self._selected, len(self.project.spheres()) - 1)
        elif tab_index == self.collision_shape_tab_index:
            self.project.collision_shape = baseline.collision_shape
            self.project.collision_shape_path = baseline.collision_shape_path
            self.project.collision_shape_file_path = (
                baseline.collision_shape_file_path)
            self.project.collision_shape_owners = (
                baseline.collision_shape_owners)
            self.project.collision_shape_components = (
                baseline.collision_shape_components)
            self.project.collision_shape_warnings = (
                baseline.collision_shape_warnings)
        elif tab_index == self.fire_points_tab_index:
            self.project.fire_points_enabled = baseline.fire_points_enabled
            self.project.fire_x = baseline.fire_x
            self.project.fire_y = baseline.fire_y
            self.project.fire_z = baseline.fire_z
            self.project.num_weapons = baseline.num_weapons
            self.project.num_weapons_max = baseline.num_weapons_max
            self._selected_fire_point = -1
        elif tab_index == self.gun_points_tab_index:
            self.project.gun_points_enabled = baseline.gun_points_enabled
            self.project.gun_points = [
                point.clone() for point in baseline.gun_points]
            self.project.unit_gun_default_icon = (
                baseline.unit_gun_default_icon)
            self.project.turret_limits = {
                vehicle_id: limits.clone()
                for vehicle_id, limits in baseline.turret_limits.items()
            }
            self._new_gun_point_scheme = self._tab_reset_gun_point_scheme
            self._selected_gun_point = min(
                self._selected_gun_point,
                len(self.project.gun_points) - 1)
        elif tab_index == self.briefing_tab_index:
            self.project.briefing_size_enabled = baseline.briefing_size_enabled
            self.project.briefing_size = baseline.briefing_size
        elif tab_index == self.cockpit_tab_index:
            self.project.cockpit_camera_enabled = (
                baseline.cockpit_camera_enabled)
            self.project.cockpit_camera_offset_x = (
                baseline.cockpit_camera_offset_x)
            self.project.cockpit_camera_offset_y = (
                baseline.cockpit_camera_offset_y)
            self.project.cockpit_camera_offset_z = (
                baseline.cockpit_camera_offset_z)

        self._set_modified(
            self.project.snapshot() != self._tab_reset_baseline)
        self._sync_all()
        self.statusBar().showMessage(
            f"{tab_name} restored to the last loaded/imported state.", 5000)

    def _reset_all_gun_points(self) -> None:
        """Restore all mounts and turret limits captured from the script."""

        if self.project.target_category != VEHICLE:
            return
        selected_index = self._selected_gun_point
        baseline = [point.clone() for point in self._loaded_gun_points]
        current = [point.clone() for point in self.project.gun_points]
        same_points = (current == baseline)
        baseline_limits = {
            vehicle_id: limits.clone()
            for vehicle_id, limits in self._loaded_turret_limits.items()
        }
        if (same_points
                and self.project.turret_limits == baseline_limits
                and self.project.gun_points_enabled
                    == self._loaded_gun_points_enabled
                and self.project.unit_gun_default_icon
                    == self._loaded_unit_gun_default_icon):
            return
        self._vehicle_preview_active_edits.clear()
        self._push_undo()
        self.project.gun_points_enabled = self._loaded_gun_points_enabled
        self.project.gun_points = baseline
        self.project.unit_gun_default_icon = (
            self._loaded_unit_gun_default_icon)
        self.project.turret_limits = baseline_limits
        self._new_gun_point_scheme = self._loaded_gun_point_scheme
        self._selected_gun_point = (
            selected_index
            if 0 <= selected_index < len(baseline)
            else -1)
        self._set_modified()
        self._sync_all()

    def _reset_gun_point_position(self) -> None:
        """Reset only the selected Gun Point translation to the origin."""

        point = self._selected_gun()
        if point is None or point.position == (0.0, 0.0, 0.0):
            return
        self._vehicle_preview_active_edits.clear()
        self._push_undo()
        point.x = 0.0
        point.y = 0.0
        point.z = 0.0
        self.project.gun_points_enabled = True
        self._set_modified()
        self._sync_all()

    def _gun_point_value_changed(self, field: str, value: float) -> None:
        if self._syncing or self.project.target_category != VEHICLE:
            return
        point = self._selected_gun()
        if point is None or not hasattr(point, field):
            return
        value = float(value)
        if not math.isfinite(value):
            return
        if abs(float(getattr(point, field)) - value) < 1e-9:
            return
        self._begin_vehicle_preview_edit(f"gun_{field}")
        setattr(point, field, value)
        self.project.gun_points_enabled = True
        self._set_modified()
        self._sync_all()

    def _begin_gun_direction_slider(self) -> None:
        point = self._selected_gun()
        if point is None:
            return
        self._vehicle_preview_active_edits.clear()
        self._push_undo()
        self._gun_direction_slider_active = True
        if gun_direction_index(point.dir_x, point.dir_z) is None:
            point.dir_x, point.dir_z = GUN_DIRECTION_SEQUENCE[
                self.gun_direction_slider.value()]
            self.project.gun_points_enabled = True
            self._set_modified()
            self._sync_all()

    def _finish_gun_direction_slider(self) -> None:
        self._gun_direction_slider_active = False

    def _gun_direction_slider_changed(self, index: int) -> None:
        if self._syncing or self.project.target_category != VEHICLE:
            return
        point = self._selected_gun()
        if point is None:
            return
        index = max(0, min(len(GUN_DIRECTION_SEQUENCE) - 1, int(index)))
        dir_x, dir_z = GUN_DIRECTION_SEQUENCE[index]
        if (abs(float(point.dir_x) - dir_x) < 1e-9
                and abs(float(point.dir_z) - dir_z) < 1e-9):
            return
        if not self._gun_direction_slider_active:
            self._vehicle_preview_active_edits.clear()
            self._push_undo()
        point.dir_x = dir_x
        point.dir_z = dir_z
        self.project.gun_points_enabled = True
        self._set_modified()
        self._sync_all()

    def _begin_turret_limit_slider(self, field: str) -> None:
        limits = self._selected_turret_limits()
        if limits is None:
            return
        self._vehicle_preview_active_edits.discard(f"turret_{field}")
        self._push_undo()
        self._turret_slider_active_edits.add(field)

    def _finish_turret_limit_slider(self, field: str) -> None:
        self._turret_slider_active_edits.discard(field)

    def _turret_limit_value_changed(
            self, field: str, value: int, *, from_slider: bool = False) -> None:
        if self._syncing or self.project.target_category != VEHICLE:
            return
        limits = self._selected_turret_limits()
        if limits is None or field not in {"side", "up", "down"}:
            return
        value = _clamp_gun_angle(field, value)
        if int(getattr(limits, field)) == value:
            return
        if from_slider:
            if field not in self._turret_slider_active_edits:
                self._push_undo()
        else:
            self._begin_vehicle_preview_edit(f"turret_{field}")
        setattr(limits, field, value)
        limits.enabled = True
        limits.dirty = True
        self._set_modified()
        self._sync_all()

    def _gun_type_changed(self, value: int) -> None:
        if self._syncing or self.project.target_category != VEHICLE:
            return
        point = self._selected_gun()
        if point is None:
            return
        value = max(0, min(255, int(value)))
        if point.gun_type == value:
            return
        self._begin_vehicle_preview_edit("gun_type")
        point.gun_type = value
        self.project.gun_points_enabled = True
        self._set_modified()
        self._sync_all()

    def _gun_name_changed(self) -> None:
        if self._syncing or self.project.target_category != VEHICLE:
            return
        point = self._selected_gun()
        if point is None:
            return
        value = self.gun_name_edit.text().strip()
        if point.name == value:
            return
        self._push_undo()
        point.name = value
        self.project.gun_points_enabled = True
        self._set_modified()
        self._sync_all()


    def _gizmo_nudge(self, direction):
        if self._is_cockpit_tab_active():
            self._cockpit_nudge(direction)
            return
        step = float(self.move_strength_spin.value())
        active_tab = self.properties_tabs.currentIndex()

        if active_tab == self.collision_tab_index:
            spheres = self.project.spheres()
            selected_indices = self._selected_sphere_indices()
            selected_spheres = [
                spheres[index] for index in sorted(selected_indices)
                if 0 <= index < len(spheres)
            ]
            if not selected_spheres:
                return
            movable = [
                sphere for sphere in selected_spheres
                if sphere.category != LEGACY
            ]
            if not movable:
                self.statusBar().showMessage(
                    "Legacy Radius has no script offset and remains at origin.",
                    5000)
                return
            self._push_undo()
            for sphere in movable:
                sphere.x += direction[0] * step
                sphere.y += direction[1] * step
                sphere.z += direction[2] * step
            self._set_modified()
            self._sync_all()
            if len(movable) > 1:
                self.statusBar().showMessage(
                    f"Moved {len(movable)} selected collision spheres.", 3000)
            return

        if active_tab == self.gun_points_tab_index:
            gun = self._selected_gun()
            if gun is None:
                return
            self._push_undo()
            gun.x += float(direction[0]) * step
            gun.y += float(direction[1]) * step
            gun.z += float(direction[2]) * step
            self.project.gun_points_enabled = True
            self._set_modified()
            self._sync_all()
            return

        if active_tab != self.fire_points_tab_index:
            return

        index = self._selected_fire_point
        points = fire_point_positions(self.project)
        if not (self.project.fire_points_enabled
                and 0 <= index < len(points)):
            return

        dx = float(direction[0]) * step
        dy = float(direction[1]) * step
        dz = float(direction[2]) * step
        count = _fire_point_preview_count(self.project)
        factor = None
        if dx and count > 1:
            factor = -1.0 + (2.0 * index / (count - 1))
            if abs(factor) <= 1e-9 and not (dy or dz):
                self.statusBar().showMessage(
                    "The center Fire Point of an odd vanilla rack is "
                    "locked to X = 0. Select another point to change "
                    "the symmetric spacing.", 6500)
                return

        self._push_undo()
        changed = False
        if dy:
            self.project.fire_y += dy
            changed = True
        if dz:
            self.project.fire_z += dz
            changed = True
        if dx:
            if count == 1:
                self.project.fire_x += dx
                changed = True
            elif factor is not None and abs(factor) <= 1e-9:
                self.statusBar().showMessage(
                    "The center Fire Point of an odd vanilla rack is "
                    "locked to X = 0. Y/Z movement still moved the full "
                    "rack.", 6500)
            else:
                current_x = factor * abs(float(self.project.fire_x))
                new_extent = max(0.0, (current_x + dx) / factor)
                sign = -1.0 if self.project.fire_x < 0.0 else 1.0
                self.project.fire_x = sign * new_extent
                changed = True
                self.statusBar().showMessage(
                    "Vanilla multi-weapon Fire Points stay symmetric; "
                    "X movement changed the spacing of the full rack.",
                    5000)
        if not changed:
            return
        self.project.fire_points_enabled = True
        self._set_modified()
        self._sync_all()


    def _new_gun_point_scheme_changed(self, _index: int) -> None:
        if self._syncing:
            return
        scheme = self.gun_point_type_combo.currentData()
        if scheme not in {"robo", "unit"}:
            return

        selected = self._selected_gun()
        if selected is None:
            self._new_gun_point_scheme = scheme
            return

        if selected.scheme == scheme:
            self._new_gun_point_scheme = scheme
            return

        # Each runtime family has its own 20-slot array. Converting an
        # existing mount must therefore respect the same limit as Add Gun
        # Point instead of generating an invalid script on export.
        target_count = sum(
            point.scheme == scheme for point in self.project.gun_points)
        if target_count >= 20:
            self.statusBar().showMessage(
                "Cannot convert Gun Point: target family already has 20 "
                "mounts.", 5000)
            self._sync_all()
            return

        self._push_undo()
        old_scheme = selected.scheme
        selected.scheme = scheme
        if (old_scheme == "unit" and scheme == "robo"
                and not any(
                    point is not selected and point.scheme == "unit"
                    for point in self.project.gun_points)):
            # unit_gun_icon is meaningful only for the generic unit_* family.
            # Converting the final generic mount to vanilla must not leave an
            # otherwise-empty unit_* family behind in canonical script output.
            self.project.unit_gun_default_icon = ""
        self._new_gun_point_scheme = scheme
        self._set_modified()
        self._sync_all()

    def _toggle_selected_sphere_visibility(self):
        entries = self._selected_sphere_entries()
        if not entries:
            return
        # Mixed selections collapse to hidden first; once every selected row
        # is hidden, the same command becomes Show and restores them all.
        target_visible = all(
            not sphere.visible for _index, sphere in entries)
        changing = [
            sphere for _index, sphere in entries
            if sphere.visible != target_visible
        ]
        if not changing and not self.project.sphere_isolation_active:
            return
        self._push_undo()
        for sphere in changing:
            sphere.visible = target_visible
        # Manual visibility editing supersedes Isolate mode. This prevents an
        # old isolation session from changing the meaning of the buttons after
        # the user starts explicitly hiding/showing individual rows.
        self.project.sphere_isolation_active = False
        self._set_modified()
        self._sync_all()

    def _set_all_sphere_visibility(self, visible: bool) -> None:
        spheres = self.project.spheres()
        if not spheres:
            return
        target_visible = bool(visible)
        changing = [
            sphere for sphere in spheres
            if sphere.visible != target_visible
        ]
        if not changing and not self.project.sphere_isolation_active:
            return
        self._push_undo()
        for sphere in changing:
            sphere.visible = target_visible
        self.project.sphere_isolation_active = False
        self._set_modified()
        self._sync_all()

    def _toggle_all_sphere_visibility(self) -> None:
        spheres = self.project.spheres()
        if not spheres:
            return
        all_hidden = all(not sphere.visible for sphere in spheres)
        self._set_all_sphere_visibility(all_hidden)


    def _toggle_selected_sphere_isolation(self):
        if self.project.sphere_isolation_active:
            self._unisolate_sphere_visibility()
            return
        selected = self._selected_sphere_indices()
        if not selected:
            return
        self._isolate_sphere_visibility(selected)

    def _isolate_sphere_visibility(self, selected: set[int]):
        spheres = self.project.spheres()
        if not selected or any(index < 0 or index >= len(spheres)
                               for index in selected):
            return
        changing = [
            sphere for sphere_index, sphere in enumerate(spheres)
            if sphere.visible != (sphere_index in selected)
        ]
        if not changing and self.project.sphere_isolation_active:
            return
        self._push_undo()
        for sphere_index, sphere in enumerate(spheres):
            sphere.visible = sphere_index in selected
        self.project.sphere_isolation_active = True
        self._set_modified()
        self._sync_all()

    def _unisolate_sphere_visibility(self, _index: int | None = None):
        spheres = self.project.spheres()
        if not spheres:
            return
        changing = [sphere for sphere in spheres if not sphere.visible]
        if not changing and not self.project.sphere_isolation_active:
            return
        self._push_undo()
        for sphere in changing:
            sphere.visible = True
        self.project.sphere_isolation_active = False
        self._set_modified()
        self._sync_all()

    def _visibility_changed(self, visible: bool):
        if self._syncing:
            return
        entries = self._selected_sphere_entries()
        if not entries:
            return
        value = bool(visible)
        changing = [
            sphere for _index, sphere in entries
            if sphere.visible != value
        ]
        if not changing and not self.project.sphere_isolation_active:
            return
        self._push_undo()
        for sphere in changing:
            sphere.visible = value
        self.project.sphere_isolation_active = False
        self._set_modified()
        self._sync_all()

    def _begin_radius_slider(self):
        if not self._selected_spheres_with_common_radius():
            return
        self._radius_spin_active = False
        self._push_undo()
        self._radius_slider_active = True

    def _radius_slider_changed(self, value: int):
        if self._syncing:
            return
        spheres = self._selected_spheres_with_common_radius()
        if not spheres:
            return
        if not self._radius_slider_active:
            self._push_undo()
        radius = max(1.0, round(self._slider_to_radius(value)))
        for sphere in spheres:
            sphere.radius = radius
        self._set_modified()
        self._sync_all()

    def _finish_radius_slider(self):
        self._radius_slider_active = False

    def _radius_spin_changed(self, value: float):
        if self._syncing:
            return
        spheres = self._selected_spheres_with_common_radius()
        if not spheres:
            return
        if abs(value - spheres[0].radius) < 1e-9:
            return
        if not self._radius_spin_active:
            self._push_undo()
            self._radius_spin_active = True
        radius = round(value, 0 if any(
            sphere.category == LEGACY for sphere in spheres) else 3)
        for sphere in spheres:
            sphere.radius = radius
        self._set_modified()
        self._sync_all()

    def _finish_radius_spin_edit(self):
        self._radius_spin_active = False

    def _sphere_display_name(
            self, sphere: CollisionSphere,
            compound_index: int | None = None) -> str:
        if sphere.category == LEGACY:
            return "Legacy Radius"
        if compound_index is None:
            for index, candidate in enumerate(self.project.compound):
                if candidate is sphere:
                    compound_index = index
                    break
        if compound_index is None:
            compound_index = -1
        return f"{TYPE_LABELS[sphere.category]} {compound_index}"

    def _refresh_sphere_tree(self):
        with QSignalBlocker(self.sphere_tree):
            self.sphere_tree.clear()
            selected_items = []
            primary_item = None
            selected_indices = self._selected_sphere_indices()
            compound_index = 0
            for flat_index, sphere in enumerate(self.project.spheres()):
                current_compound = None
                if sphere.category != LEGACY:
                    current_compound = compound_index
                    compound_index += 1
                index_text = (
                    "Legacy" if sphere.category == LEGACY
                    else str(current_compound)
                )
                radius_text = (
                    _radius_number(sphere.radius)
                    if sphere.category == LEGACY
                    else _sphere_number(sphere.radius))
                item = QTreeWidgetItem([
                    TYPE_LABELS[sphere.category],
                    radius_text,
                    "Visible" if sphere.visible else "Hidden",
                    _sphere_number(sphere.x),
                    _sphere_number(sphere.y),
                    _sphere_number(sphere.z),
                    index_text,
                ])
                item.setData(0, _SPHERE_INDEX_ROLE, flat_index)
                item.setForeground(0, QBrush(TYPE_COLORS[sphere.category]))
                item.setTextAlignment(
                    0, Qt.AlignmentFlag.AlignLeft
                    | Qt.AlignmentFlag.AlignVCenter)
                for column in range(1, 7):
                    item.setTextAlignment(
                        column, Qt.AlignmentFlag.AlignRight
                        | Qt.AlignmentFlag.AlignVCenter)
                self.sphere_tree.addTopLevelItem(item)
                if flat_index in selected_indices:
                    selected_items.append(item)
                if flat_index == self._selected:
                    primary_item = item
            if primary_item is not None:
                self.sphere_tree.setCurrentItem(primary_item)
                for item in selected_items:
                    item.setSelected(True)
            else:
                self.sphere_tree.setCurrentItem(None)
                self.sphere_tree.clearSelection()

    def _refresh_fire_point_tree(self, points):
        with QSignalBlocker(self.fire_point_tree):
            self.fire_point_tree.clear()
            selected_item = None
            for index, point in enumerate(points):
                item = QTreeWidgetItem([
                    f"F{index + 1}",
                    _number(point[0]), _number(point[1]), _number(point[2]),
                ])
                item.setData(0, _FIRE_POINT_INDEX_ROLE, index)
                item.setForeground(0, QBrush(FIRE_POINT_COLOR))
                for column in (1, 2, 3):
                    item.setTextAlignment(
                        column, Qt.AlignmentFlag.AlignRight
                        | Qt.AlignmentFlag.AlignVCenter)
                self.fire_point_tree.addTopLevelItem(item)
                if index == self._selected_fire_point:
                    selected_item = item
            if selected_item is not None:
                self.fire_point_tree.setCurrentItem(selected_item)
            else:
                self.fire_point_tree.setCurrentItem(None)
                self.fire_point_tree.clearSelection()


    def _refresh_gun_point_tree(self):
        with QSignalBlocker(self.gun_point_tree):
            self.gun_point_tree.clear()
            selected_item = None
            if (self.project.target_category == VEHICLE
                    and self.project.gun_points_enabled):
                for index, point in enumerate(self.project.gun_points):
                    label = f"G{index + 1}"
                    if point.name:
                        label += f" — {point.name}"
                    full_label = label
                    label = f"G{index + 1}"
                    family_label = (
                        "Vanilla" if point.scheme == "robo" else "OpenNeoUA")
                    limits = self.project.turret_limits.get(
                        int(point.gun_type))
                    angle_values = (
                        [str(_clamp_gun_angle(field, value)) for field, value in
                         (("side", limits.side), ("up", limits.up),
                          ("down", limits.down))]
                        if limits is not None
                        else ["-", "-", "-"]
                    )
                    item = QTreeWidgetItem([
                        label, family_label, str(int(point.gun_type)),
                        *angle_values,
                        _number(point.x), _number(point.y), _number(point.z),
                    ])
                    item.setData(0, _GUN_POINT_INDEX_ROLE, index)
                    item.setForeground(0, QBrush(GUN_POINT_COLOR))
                    item.setTextAlignment(
                        0, Qt.AlignmentFlag.AlignCenter
                        | Qt.AlignmentFlag.AlignVCenter)
                    family = (
                        "Legacy Host Station robo_* gun mount"
                        if point.scheme == "robo"
                        else "Generic OpenNeoUA unit_* vehicle gun mount")
                    item.setToolTip(0, f"{full_label}\n{family}")
                    item.setToolTip(1, family)
                    item.setToolTip(
                        2, "Referenced gun vehicle prototype ID.")
                    if limits is None:
                        angle_tip = (
                            f"Vehicle {int(point.gun_type)} was not found in "
                            "the loaded/imported script.")
                    else:
                        angle_tip = (
                            "Shared vanilla turret limits for referenced "
                            f"Vehicle {int(point.gun_type)} (radians x 1000).")
                    for column in (3, 4, 5):
                        item.setToolTip(column, angle_tip)
                    for column in range(2, 9):
                        item.setTextAlignment(
                            column, Qt.AlignmentFlag.AlignRight
                            | Qt.AlignmentFlag.AlignVCenter)
                    self.gun_point_tree.addTopLevelItem(item)
                    if index == self._selected_gun_point:
                        selected_item = item
            if selected_item is not None:
                self.gun_point_tree.setCurrentItem(selected_item)
            else:
                self.gun_point_tree.setCurrentItem(None)
                self.gun_point_tree.clearSelection()


    def _refresh_project_summary_menu(self):
        menu = self.project_summary_menu
        menu.clear()
        openneoua_count = sum(
            sphere.category == OPENNEOUA for sphere in self.project.compound)
        sphere = self._selected_sphere()
        selected_sphere_count = len(self._selected_sphere_indices())
        compound_index = self._selected_compound_index()
        if selected_sphere_count > 1:
            selected = f"{selected_sphere_count} Collision Spheres"
        elif sphere is not None:
            selected = self._sphere_display_name(sphere, compound_index)
        elif (self.project.target_category == VEHICLE
              and self.project.gun_points_enabled
              and 0 <= self._selected_gun_point
              < len(self.project.gun_points)):
            selected = f"Gun Point G{self._selected_gun_point + 1}"
        elif (self.project.target_category == VEHICLE
              and self.project.fire_points_enabled
              and 0 <= self._selected_fire_point
              < len(fire_point_positions(self.project))):
            selected = f"Fire Point F{self._selected_fire_point + 1}"
        else:
            selected = "None"
        compound_mode = bool(self.project.compound)
        if self.project.legacy is None:
            legacy_status = "Missing"
        elif compound_mode:
            legacy_status = "Disabled by compound collisions"
        else:
            legacy_status = "Active"
        collision_mode = (
            "OpenNeoUA compound (Legacy Radius disabled)"
            if compound_mode else "Vanilla Legacy Radius"
        )
        lines = [
            f"Project: {self.project.name or '<unnamed>'}",
            f"Source model: {self.project.source_model or '<none>'}",
            "Linked script object: "
            + (f"{self._active_script_path.name} — "
               f"{self._active_script_kind} {self._active_script_id}"
               if (self._active_script_path is not None
                   and self._active_script_id is not None)
               else "Not linked"),
            "Model preview scale: "
            f"X {_number(self.project.model_scale_x)}  "
            f"Y {_number(self.project.model_scale_y)}  "
            f"Z {_number(self.project.model_scale_z)}",
            "Ground alignment: "
            + ("Not shown for weapon target"
               if self.project.target_category == WEAPON
               else (f"Overeof {_number(self.project.overeof)}"
                     if self.project.overeof_enabled
                     else "Overeof not included")),
            "Fire points: "
            + (f"{_num_weapons_text(self.project)} "
               f"(preview {_fire_point_preview_count(self.project)}) at "
               f"({_number(self.project.fire_x)}, "
               f"{_number(self.project.fire_y)}, "
               f"{_number(self.project.fire_z)})"
               if (self.project.target_category == VEHICLE
                   and self.project.fire_points_enabled)
               else "Not included"),
            "Gun points: "
            + (str(len(self.project.gun_points))
               if (self.project.target_category == VEHICLE
                   and self.project.gun_points_enabled)
               else "Not included"),
            "Cockpit camera: "
            + (f"X {_number(self.project.cockpit_camera_offset_x)}  "
               f"Y {_number(self.project.cockpit_camera_offset_y)}  "
               f"Z {_number(self.project.cockpit_camera_offset_z)}"
               if (self.project.target_category == VEHICLE
                   and self.project.cockpit_camera_enabled)
               else "Not included"),
            f"Collision mode: {collision_mode}",
            f"Legacy Radius: {legacy_status}",
            f"Internal broad-phase extent: "
            f"{_radius_number(effective_runtime_radius(self.project))}",
            f"OpenNeoUA Collision Spheres: {openneoua_count}",
            f"Total Compound Spheres: {len(self.project.compound)}",
            f"Selected Element: {selected}",
        ]
        for line in lines:
            action = menu.addAction(line)
            action.setEnabled(False)

    def _preview_spheres(self) -> list[CollisionSphere]:
        """Return collision preview matching the shared runtime rule.

        ``coll_*`` without an authored ``radius`` is compound-only. If radius
        is explicitly present, Legacy Radius and OpenNeoUA Collision coexist.
        """

        offset_y = (
            -self.project.overeof
            if (self.project.target_category == VEHICLE
                and self.project.overeof_enabled)
            else 0.0
        )
        previews = []
        compound_active = bool(self.project.compound)
        for sphere in self.project.spheres():
            preview = sphere.clone()
            preview.y += offset_y
            if compound_active and sphere.category == LEGACY:
                # Compound spheres drive the runtime extent, so the legacy ring
                # would only add a misleading outline around the model.
                preview.visible = False
            previews.append(preview)
        return previews

    def _sync_all(self):
        self._selected_sphere_indices()
        self._syncing = True
        blockers = [
            QSignalBlocker(widget) for widget in (
                self.radius_slider,
                self.radius_spin, self.visible_check,
                self.model_scale_x_spin, self.model_scale_y_spin,
                self.model_scale_z_spin, self.overeof_check,
                self.overeof_spin,
                self.fire_x_spin, self.fire_y_spin, self.fire_z_spin,
                self.num_weapons_spin, self.num_weapons_max_spin,
                self.cockpit_camera_spins["x"],
                self.cockpit_camera_spins["y"],
                self.cockpit_camera_spins["z"],
                self.gun_point_spins["x"], self.gun_point_spins["y"],
                self.gun_point_spins["z"],
                self.gun_dir_spins["x"], self.gun_dir_spins["z"],
                self.gun_direction_slider, self.gun_type_spin,
                self.gun_name_edit, self.gun_point_type_combo,
                *self.turret_limit_sliders.values(),
                *self.turret_limit_spins.values())
        ]
        self.model_scale_x_spin.setValue(self.project.model_scale_x)
        self.model_scale_y_spin.setValue(self.project.model_scale_y)
        self.model_scale_z_spin.setValue(self.project.model_scale_z)
        self.viewport.set_model_preview_scale(
            self.project.model_scale_x, self.project.model_scale_y,
            self.project.model_scale_z)
        vehicle_mode = self.project.target_category == VEHICLE
        fire_count = (len(fire_point_positions(self.project))
                      if (vehicle_mode and self.project.fire_points_enabled)
                      else 0)
        if not (0 <= self._selected_fire_point < fire_count):
            self._selected_fire_point = -1
        gun_count = (len(self.project.gun_points)
                     if (vehicle_mode and self.project.gun_points_enabled)
                     else 0)
        if not (0 <= self._selected_gun_point < gun_count):
            self._selected_gun_point = -1
        self.viewport.set_ground_alignment(
            vehicle_mode, self.project.overeof_enabled,
            self.project.overeof)
        for index in (
                self.ground_tab_index,
                self.fire_points_tab_index, self.gun_points_tab_index,
                self.cockpit_tab_index):
            self.properties_tabs.setTabEnabled(index, vehicle_mode)
        # Weapons keep the tabs that also apply to projectiles.
        if (not vehicle_mode
                and self.properties_tabs.currentIndex()
                not in (self.collision_tab_index, self.collision_shape_tab_index,
                        self.fx_tab_index)):
            self.properties_tabs.setCurrentIndex(self.collision_tab_index)

        cockpit_enabled = vehicle_mode and self.project.cockpit_camera_enabled
        self.cockpit_camera_button.setEnabled(vehicle_mode)
        self.cockpit_camera_button.setText(
            "Disable Cockpit Camera Offset" if cockpit_enabled
            else "Enable Cockpit Camera Offset")
        self.cockpit_controls.setVisible(cockpit_enabled)
        self.cockpit_x_spin.setValue(self.project.cockpit_camera_offset_x)
        self.cockpit_y_spin.setValue(self.project.cockpit_camera_offset_y)
        self.cockpit_z_spin.setValue(self.project.cockpit_camera_offset_z)
        for spin in self.cockpit_camera_spins.values():
            spin.setEnabled(cockpit_enabled)
        self.reset_cockpit_button.setEnabled(
            cockpit_enabled and any(abs(value) > 1e-9 for value in (
                self.project.cockpit_camera_offset_x,
                self.project.cockpit_camera_offset_y,
                self.project.cockpit_camera_offset_z)))
        cockpit_current = (
            float(self.project.cockpit_camera_offset_x),
            float(self.project.cockpit_camera_offset_y),
            float(self.project.cockpit_camera_offset_z),
        )
        cockpit_source_available = (
            vehicle_mode and self._loaded_cockpit_camera_enabled)
        cockpit_matches_source = all(
            abs(a - b) < 1e-9 for a, b in zip(
                cockpit_current, self._loaded_cockpit_camera_offset))
        self.restore_script_cockpit_button.setEnabled(
            cockpit_enabled and cockpit_source_available
            and not cockpit_matches_source)
        if cockpit_source_available:
            source_x, source_y, source_z = self._loaded_cockpit_camera_offset
            self.restore_script_cockpit_button.setToolTip(
                "Restore the cockpit position read from the source script: "
                f"X {_number(source_x)}, Y {_number(source_y)}, "
                f"Z {_number(source_z)}.")
        else:
            self.restore_script_cockpit_button.setToolTip(
                "No cockpit_camera_offset_xyz coordinates were found in "
                "the last loaded or imported vehicle script.")
        cockpit_lines = [
            "cockpit_camera_offset_xyz = "
            f"{_number(self.project.cockpit_camera_offset_x)}_"
            f"{_number(self.project.cockpit_camera_offset_y)}_"
            f"{_number(self.project.cockpit_camera_offset_z)}",
        ]
        if not vehicle_mode:
            cockpit_lines = [
                "; Cockpit View is available only for vehicle definitions"]
        elif not cockpit_enabled:
            cockpit_lines = []
        self.cockpit_output.setText("\n".join(cockpit_lines))
        self.viewport.set_cockpit_camera_offset(
            self.project.cockpit_camera_offset_x,
            self.project.cockpit_camera_offset_y,
            self.project.cockpit_camera_offset_z)
        cockpit_active = self._is_cockpit_tab_active()
        self.viewport.set_cockpit_preview_active(cockpit_active)
        if hasattr(self, "cockpit_runtime_aspect_combo"):
            self.viewport.set_cockpit_runtime_aspect(
                self.cockpit_runtime_aspect_combo.currentData())
        self._sync_cockpit_preview_model()
        self.toolbar_view_preset_combo.setEnabled(not cockpit_active)
        self.reset_view_action.setEnabled(not cockpit_active)
        shape_tab_active = (
            self.properties_tabs.currentIndex()
            == self.collision_shape_tab_index)
        briefing_active = self.properties_tabs.currentIndex() == self.briefing_tab_index
        self.transform_box.setVisible(
            not shape_tab_active and not briefing_active
            and self.properties_tabs.currentIndex() != self.ground_tab_index
            and self.properties_tabs.currentIndex() != self.wireframe_tab_index
            and not self._is_fx_tab_active())
        self._sync_fx_preview()

        self.ground_alignment_box.setVisible(vehicle_mode)
        self.overeof_check.setChecked(
            vehicle_mode and self.project.overeof_enabled)
        self.overeof_spin.setValue(self.project.overeof)
        self.overeof_spin.setEnabled(
            vehicle_mode and self.project.overeof_enabled)
        self.suggest_overeof_button.setEnabled(
            vehicle_mode and self._model_bounds() is not None)
        self.reset_overeof_button.setEnabled(
            vehicle_mode and (self.project.overeof_enabled
                              or abs(self.project.overeof) > 1e-9))
        self.fire_points_box.setVisible(vehicle_mode)
        self.add_fire_point_button.setEnabled(vehicle_mode)
        self.fire_x_spin.setValue(self.project.fire_x)
        self.fire_y_spin.setValue(self.project.fire_y)
        self.fire_z_spin.setValue(self.project.fire_z)
        num_weapons_min, num_weapons_max = _project_num_weapons_range(
            self.project)
        self.num_weapons_spin.setValue(
            max(0, min(255, num_weapons_min)))
        self.num_weapons_max_spin.setValue(
            max(0, min(255, num_weapons_max)))
        fire_controls_enabled = (
            vehicle_mode and self.project.fire_points_enabled)
        self.remove_fire_point_button.setEnabled(
            fire_controls_enabled and self._selected_fire_point >= 0)
        self.reset_fire_point_button.setEnabled(vehicle_mode)
        for widget in (
                self.fire_x_spin, self.fire_y_spin, self.fire_z_spin,
                self.num_weapons_spin, self.num_weapons_max_spin):
            widget.setEnabled(fire_controls_enabled)

        self.gun_points_box.setVisible(vehicle_mode)
        gun = self._selected_gun() if vehicle_mode else None
        displayed_scheme = (
            gun.scheme if gun is not None and gun.scheme in {"robo", "unit"}
            else self._new_gun_point_scheme)
        scheme_index = self.gun_point_type_combo.findData(displayed_scheme)
        self.gun_point_type_combo.setCurrentIndex(
            scheme_index if scheme_index >= 0 else 1)
        self.gun_point_type_combo.setEnabled(vehicle_mode)
        self.add_gun_point_button.setEnabled(vehicle_mode)
        self.remove_gun_point_button.setEnabled(gun is not None)
        self.reset_gun_point_button.setEnabled(gun is not None)
        self.reset_all_gun_points_button.setEnabled(vehicle_mode)
        referenced_vehicle = (
            self._resolve_referenced_turret_limits(int(gun.gun_type))
            if gun is not None else None)
        direction_index = None
        if gun is None:
            self.gun_family_value.setText("No point selected")
            for spin in self.gun_point_spins.values():
                spin.setValue(0.0)
            for spin in self.gun_dir_spins.values():
                spin.setValue(0.0)
            self.gun_direction_slider.setValue(0)
            self.gun_direction_value.clear()
            self.gun_type_spin.setValue(0)
            self.gun_name_edit.clear()
            self.gun_name_edit.setReadOnly(True)
        else:
            family_text = (
                "Host Station legacy · robo_*" if gun.scheme == "robo"
                else "Generic vehicle · unit_*")
            self.gun_family_value.setText(family_text)
            self.gun_point_spins["x"].setValue(gun.x)
            self.gun_point_spins["y"].setValue(gun.y)
            self.gun_point_spins["z"].setValue(gun.z)
            self.gun_dir_spins["x"].setValue(gun.dir_x)
            self.gun_dir_spins["z"].setValue(gun.dir_z)
            direction_index = gun_direction_index(gun.dir_x, gun.dir_z)
            if direction_index is None:
                self.gun_direction_slider.setValue(0)
                self.gun_direction_value.setText("Unset")
            else:
                self.gun_direction_slider.setValue(direction_index)
                self.gun_direction_value.setText(
                    GUN_DIRECTION_LABELS[direction_index])
            self.gun_type_spin.setValue(
                max(0, min(255, int(gun.gun_type))))
            if referenced_vehicle is not None:
                self.gun_name_edit.setText(referenced_vehicle.source_name)
                self.gun_name_edit.setReadOnly(True)
                self.gun_name_edit.setToolTip(
                    f"Read-only script name of referenced Vehicle "
                    f"{int(gun.gun_type)}. The mount's "
                    "robo_gun_name / unit_gun_name is preserved unchanged.")
            else:
                self.gun_name_edit.setText(gun.name)
                self.gun_name_edit.setReadOnly(False)
                self.gun_name_edit.setToolTip(
                    "Referenced Vehicle not found in the loaded script. "
                    "Edit robo_gun_name / unit_gun_name for this mount.")
        for widget in (
                *self.gun_point_spins.values(),
                *self.gun_dir_spins.values(),
                self.gun_direction_slider,
                self.gun_type_spin, self.gun_name_edit):
            widget.setEnabled(gun is not None)
        self.gun_direction_value.setEnabled(gun is not None)

        limits = referenced_vehicle
        limits_available = vehicle_mode and gun is not None and limits is not None
        if gun is None:
            self.turret_limits_source.setText("Select a Gun Point")
        elif limits is None:
            self.turret_limits_source.setText(
                f"Vehicle {int(gun.gun_type)} not found in game catalog or loaded script")
        else:
            suffix = f" - {limits.source_name}" if limits.source_name else ""
            self.turret_limits_source.setText(
                f"Vehicle {limits.vehicle_id}{suffix}")
        for axis in ("side", "up", "down"):
            value = (_clamp_gun_angle(axis, getattr(limits, axis))
                     if limits is not None else 0)
            self.turret_limit_spins[axis].setValue(value)
            self.turret_limit_sliders[axis].setValue(value)
            self.turret_limit_spins[axis].setEnabled(bool(limits_available))
            self.turret_limit_sliders[axis].setEnabled(bool(limits_available))
        if limits is None:
            limits_text = (
                "Load/import the script containing this Vehicle ID to edit "
                "its vanilla gun limits.")
        else:
            side_text = (
                "free 360 deg" if limits.side > GUN_FREE_SIDE_THRESHOLD
                else f"+/-{gun_angle_degrees(limits.side):.1f} deg")
            limits_text = (
                f"Effective: Side {side_text}; "
                f"Up {gun_angle_degrees(limits.up):.1f} deg; "
                f"Down {gun_angle_degrees(limits.down):.1f} deg.")
            if _normalized_vector3(gun.direction) is None:
                limits_text += " gun_dir is zero, so no oriented arc can be drawn."
        self.turret_limits_effective.setText(limits_text)

        sphere = self._selected_sphere()
        selected_sphere_entries = self._selected_sphere_entries()
        selected_sphere_count = len(selected_sphere_entries)
        fire_selected = (
            vehicle_mode and self.project.fire_points_enabled
            and 0 <= self._selected_fire_point
            < len(fire_point_positions(self.project)))
        gun_selected = gun is not None
        enabled = sphere is not None
        radius_enabled = bool(self._selected_spheres_with_common_radius())
        self.radius_slider.setEnabled(radius_enabled)
        self.radius_spin.setEnabled(radius_enabled)
        self.visible_check.setEnabled(enabled)
        self.visible_check.setVisible(enabled)
        self.visible_check.setTristate(selected_sphere_count > 1)
        if selected_sphere_count > 1:
            radius_tip = (
                "Selected spheres have different radii. Select spheres with "
                "the same radius to edit them together."
                if not radius_enabled else
                "Edit the radius of all selected spheres together.")
            self.radius_slider.setToolTip(radius_tip)
            self.radius_spin.setToolTip(radius_tip)
        else:
            self.radius_slider.setToolTip("")
        active_tab = self.properties_tabs.currentIndex()
        cockpit_selected = self._is_cockpit_tab_selected()
        gizmo_enabled = False
        if cockpit_active:
            gizmo_enabled = True
        elif active_tab == self.collision_tab_index:
            spheres = self.project.spheres()
            gizmo_enabled = any(
                spheres[index].category != LEGACY
                for index in self._selected_sphere_indices()
                if 0 <= index < len(spheres))
        elif active_tab == self.fire_points_tab_index:
            gizmo_enabled = fire_selected
        elif active_tab == self.gun_points_tab_index:
            gizmo_enabled = gun_selected
        self.gizmo.setEnabled(gizmo_enabled)
        if cockpit_selected:
            self.transform_box.setTitle("Move Cockpit Camera")
            self.transform_box.setToolTip(
                "Move the OpenNeoUA cockpit camera in vehicle-local X/Y/Z after "
                "Cockpit Camera Offset is enabled. Left/Right = X, Up/Down = "
                "Z, Page Up/Page Down = Y. The camera keeps the vehicle's real "
                "orientation. Drag empty gizmo space to rotate only the "
                "control view.")
        else:
            self.transform_box.setToolTip(
                "Move the selected element from the active property tab. "
                "With the viewport focused: Left/Right move on X, Up/Down "
                "move on Z, and Page Up/Page Down move on Y. Drag empty "
                "gizmo space to rotate only the control view.")
            if active_tab == self.gun_points_tab_index:
                self.transform_box.setTitle("Move Gun Point")
            elif active_tab == self.fire_points_tab_index:
                self.transform_box.setTitle("Move Fire Point")
            else:
                self.transform_box.setTitle(
                    "Move Collision Spheres" if selected_sphere_count > 1
                    else "Move Collision Sphere")
        if sphere is None:
            if gun_selected:
                self.type_value.setText("Gun Point")
                self.index_value.setText(str(self._selected_gun_point))
                self.selected_element_label.setText(
                    f"Gun Point G{self._selected_gun_point + 1}")
            elif fire_selected:
                self.type_value.setText("Fire Point")
                self.index_value.setText(str(self._selected_fire_point))
                self.selected_element_label.setText(
                    f"Fire Point F{self._selected_fire_point + 1}")
            else:
                self.type_value.setText("None")
                self.index_value.setText("None")
                self.selected_element_label.setText("None")
            self.radius_spin.setValue(1.0)
            self.radius_slider.setValue(0)
            self.visible_check.setChecked(False)
            self.runtime_radius_value.clear()
            self.runtime_radius_value.hide()
            self.radius_spin.setToolTip("")
        else:
            if selected_sphere_count > 1:
                self.selected_element_label.setText(
                    f"{selected_sphere_count} Collision Spheres Selected")
                categories = {
                    candidate.category
                    for _index, candidate in selected_sphere_entries
                }
                self.type_value.setText(
                    TYPE_LABELS[next(iter(categories))]
                    if len(categories) == 1 else "Mixed")
                self.index_value.setText("Multiple")
            else:
                self.selected_element_label.setText(
                    self._sphere_display_name(
                        sphere, self._selected_compound_index()))
                self.type_value.setText(TYPE_LABELS[sphere.category])
                if sphere.category == LEGACY:
                    index_text = "Legacy"
                else:
                    compound_index = self._selected_compound_index()
                    index_text = (
                        str(compound_index)
                        if compound_index is not None else "None")
                self.index_value.setText(index_text)
            self.radius_spin.setDecimals(0 if any(
                candidate.category == LEGACY
                for _index, candidate in selected_sphere_entries) else 1)
            self.radius_spin.setValue(sphere.radius)
            self.radius_slider.setValue(
                self._radius_to_slider(sphere.radius))
            visibility_values = {
                candidate.visible
                for _index, candidate in selected_sphere_entries
            }
            if selected_sphere_count > 1 and len(visibility_values) > 1:
                self.visible_check.setCheckState(
                    Qt.CheckState.PartiallyChecked)
            else:
                self.visible_check.setChecked(sphere.visible)
            self.runtime_radius_value.clear()
            self.runtime_radius_value.hide()
            if sphere.category == LEGACY:
                # Compound spheres own the runtime extent: the explicit legacy
                # radius stays in the script but is not used at runtime.
                self.runtime_radius_value.setText(
                    "Runtime Radius: disabled by OpenNeoUA Collision"
                    if self.project.compound else
                    f"Runtime Radius: {_radius_number(sphere.radius)}")
                self.runtime_radius_value.hide()
            if selected_sphere_count == 1:
                self.radius_spin.setToolTip(
                    "Explicit radius remains active alongside OpenNeoUA "
                    "Collision spheres." if (
                        sphere.category == LEGACY and self.project.compound)
                    else "")
        self._refresh_sphere_tree()
        if self.properties_tabs.currentIndex() == self.gun_points_tab_index:
            self._refresh_gun_point_tree()
        self._refresh_project_summary_menu()
        self.viewport.set_collision_spheres(
            self._preview_spheres(), self._selected,
            self._selected_sphere_indices())
        preview_offset_y = (
            -self.project.overeof
            if (vehicle_mode and self.project.overeof_enabled)
            else 0.0)
        authored_points = []
        points = []
        if vehicle_mode and self.project.fire_points_enabled:
            authored_points = fire_point_positions(self.project)
            points = [
                (x, y + preview_offset_y, z)
                for x, y, z in authored_points
            ]
        self._refresh_fire_point_tree(authored_points)
        self.viewport.set_fire_points(points, self._selected_fire_point)
        gun_points = []
        gun_directions = []
        if vehicle_mode and self.project.gun_points_enabled:
            gun_points = [
                (point.x, point.y + preview_offset_y, point.z)
                for point in self.project.gun_points
            ]
            gun_directions = [
                (point.dir_x, point.dir_y, point.dir_z)
                for point in self.project.gun_points
            ]
        self.viewport.set_gun_points(
            gun_points, gun_directions, self._selected_gun_point)
        if gun is not None and limits is not None:
            self.viewport.set_turret_limits(
                (gun.x, gun.y + preview_offset_y, gun.z),
                gun.direction, limits)
        else:
            self.viewport.set_turret_limits((0.0, 0.0, 0.0),
                                            (0.0, 0.0, 1.0), None)
        self._sync_gizmo_camera()
        self.undo_action.setEnabled(bool(self._undo))
        self.redo_action.setEnabled(bool(self._redo))
        self.save_loaded_script_action.setEnabled(
            self._active_script_path is not None
            and self._active_script_id is not None)
        self.add_legacy_action.setEnabled(self.project.legacy is None)
        self.add_legacy_button.setEnabled(self.project.legacy is None)
        compound_count = len(self.project.compound)
        compound_has_capacity = compound_count < UNIT_COLL_MAX_COUNT
        self.add_openneoua_action.setEnabled(compound_has_capacity)
        self.add_openneoua_collision_button.setEnabled(compound_has_capacity)
        self.collision_sphere_count_label.setText(
            f"OpenNeoUA Spheres: {compound_count} / {UNIT_COLL_MAX_COUNT}")
        selected_spheres = [
            sphere for _index, sphere in selected_sphere_entries
        ]
        compound_selected = [
            sphere for sphere in selected_spheres
            if sphere.category != LEGACY
        ]
        multi_selected = selected_sphere_count > 1
        duplicate_enabled = bool(compound_selected)
        self.duplicate_action.setEnabled(duplicate_enabled)
        self.duplicate_action.setText(
            "Duplicate Spheres" if multi_selected else "Duplicate Sphere")
        self.duplicate_sphere_button.setEnabled(duplicate_enabled)
        self.duplicate_sphere_button.setText(self.duplicate_action.text())
        self.delete_action.setEnabled(bool(selected_spheres))
        self.delete_action.setText(
            "Delete Spheres" if multi_selected else "Delete Sphere")
        self.delete_sphere_button.setEnabled(bool(selected_spheres))
        self.delete_sphere_button.setText(self.delete_action.text())
        self.change_type_button.setEnabled(bool(selected_spheres))
        self.change_type_button.setText(
            "Change Sphere Types" if multi_selected else "Change Sphere Type")
        self.change_type_button.setToolTip(
            "Choose which collision category the selected spheres should "
            "become." if multi_selected else
            "Choose exactly which collision category the selected sphere "
            "should become.")
        self.change_to_legacy_action.setEnabled(
            selected_sphere_count == 1
            and selected_spheres[0].category != LEGACY
            and self.project.legacy is None)
        self.change_to_openneoua_action.setEnabled(
            bool(selected_spheres)
            and any(
                sphere.category != OPENNEOUA
                for sphere in selected_spheres))
        mirror_enabled = bool(compound_selected)
        for action in (
                self.mirror_x_action, self.mirror_y_action,
                self.mirror_z_action):
            action.setEnabled(mirror_enabled)
        self.mirror_sphere_button.setEnabled(mirror_enabled)
        self.mirror_sphere_button.setText(
            "Mirror Selected Spheres" if multi_selected
            else "Mirror Selected Sphere")
        self.mirror_sphere_button.setToolTip(
            "Duplicate the selected compound spheres on the opposite side "
            "of the chosen model axis." if multi_selected else
            "Duplicate the selected compound sphere on the opposite side "
            "of the chosen model axis.")
        collisions_present = bool(self.project.spheres())
        self.select_all_spheres_button.setEnabled(collisions_present)
        visibility_entries = self._selected_sphere_entries()
        all_hidden = bool(visibility_entries) and all(
            not sphere.visible for _index, sphere in visibility_entries)
        self.hide_spheres_button.setEnabled(bool(visibility_entries))
        selected_visibility_plural = len(visibility_entries) > 1
        self.hide_spheres_button.setText(
            "Show Spheres" if all_hidden and selected_visibility_plural
            else "Show Sphere" if all_hidden
            else "Hide Spheres" if selected_visibility_plural
            else "Hide Sphere")
        all_spheres_hidden = collisions_present and all(
            not sphere.visible for sphere in self.project.spheres())
        self.hide_all_spheres_button.setEnabled(collisions_present)
        self.hide_all_spheres_button.setText(
            "Show All Spheres" if all_spheres_hidden
            else "Hide All Spheres")
        selected_indices = self._selected_sphere_indices()
        isolate_enabled = (
            len(self.project.spheres()) > 1
            and (self.project.sphere_isolation_active or bool(selected_indices)))
        self.isolate_sphere_button.setEnabled(isolate_enabled)
        self.isolate_sphere_button.setText(
            ("Unisolate Spheres" if len(selected_indices) > 1
             else "Unisolate Sphere")
            if self.project.sphere_isolation_active else
            ("Isolate Spheres" if len(selected_indices) > 1
             else "Isolate Sphere"))
        self.isolate_sphere_button.setToolTip(
            "Show only the selected collision spheres. Use Unisolate Spheres "
            "to show all spheres again." if len(selected_indices) > 1 else
            "Show only the selected collision sphere. Use Unisolate Sphere "
            "to show all spheres again.")
        generation_enabled = bool(
            self._collision_geometry_triangles_by_owner.get(
                self._current_owner, ()))
        self.generate_collision_spheres_button.setEnabled(generation_enabled)
        for action in self.generate_collision_sphere_actions.values():
            action.setEnabled(generation_enabled)
        self._sync_collision_generation_controls()
        self._sync_collision_shape_status()
        self.undo_button.setEnabled(bool(self._undo))
        self.redo_button.setEnabled(bool(self._redo))
        self.toolbar_undo_button.setEnabled(bool(self._undo))
        self.toolbar_redo_button.setEnabled(bool(self._redo))
        if hasattr(self, "reset_tab_button"):
            self.reset_tab_button.setEnabled(self._current_tab_has_changes())
        self._update_reset_view_controls()
        self._sync_workspace_overlay_visibility()
        del blockers
        self._syncing = False
        self.briefing_tab.sync()
        if self.briefing_preview.isVisible():
            self.briefing_preview.refresh()
        self._sync_collision_shape_weapon_block()

    def _validate_for_output(self) -> bool:
        errors, warnings = validate_project(
            self.project, self._model_bounds())
        if errors:
            QMessageBox.warning(
                self, "Collision validation failed", "\n".join(errors))
            return False
        if warnings:
            result = QMessageBox.warning(
                self, "Collision validation warnings",
                "\n".join(warnings) + "\n\nContinue?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes)
            return result == QMessageBox.StandardButton.Yes
        return True

    def copy_output(self):
        if not self._validate_for_output():
            return
        QApplication.clipboard().setText(export_collision_text(self.project))
        self.statusBar().showMessage(
            "Collision output copied to clipboard.", 5000)

    def import_collisions(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import collisions from script",
            str(self._last_directory),
            OPENNEOUA_SCRIPT_FILE_FILTER)
        if not path:
            return
        try:
            text, _encoding, _bom = read_script_file(path)
        except (OSError, UnicodeError) as exc:
            QMessageBox.critical(self, "Import failed", str(exc))
            return
        dialog = ImportCollisionDialog(self, Path(path), text)
        if not dialog.blocks:
            QMessageBox.warning(
                self, "No definitions",
                "No supported new/modify vehicle/weapon block was found.")
            return
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        block, category = dialog.selected()
        try:
            legacy, compound, warnings = import_collision_block(
                text, block, OPENNEOUA)
            overeof_enabled, overeof = import_overeof_block(text, block)
            (fire_enabled, fire_x, fire_y, fire_z,
             num_weapons, num_weapons_max) = (
                import_fire_points_block(text, block))
            cockpit_enabled, cockpit_x, cockpit_y, cockpit_z = (
                import_cockpit_camera_block(text, block))
            gun_enabled, gun_points, unit_gun_default_icon = (
                import_gun_points_block(text, block))
            script_turret_limits = import_turret_limits(text)
        except CollisionScriptError as exc:
            QMessageBox.warning(self, "Import failed", str(exc))
            return
        self._clear_script_link()
        self._push_undo()
        self.project.legacy = legacy
        self.project.compound = compound
        self.project.target_category = (
            WEAPON if "weapon" in block.kind else VEHICLE)
        self.project.overeof_enabled = (
            self.project.target_category == VEHICLE and overeof_enabled)
        self.project.overeof = (
            overeof if self.project.overeof_enabled else 0.0)
        vehicle_block = self.project.target_category == VEHICLE
        self.project.briefing_size_enabled, self.project.briefing_size = (
            import_briefing_size_block(text, block) if vehicle_block else (False, 0.0))
        self.project.fire_points_enabled = vehicle_block and fire_enabled
        self.project.fire_x = fire_x if vehicle_block else 0.0
        self.project.fire_y = fire_y if vehicle_block else 0.0
        self.project.fire_z = fire_z if vehicle_block else 0.0
        self.project.num_weapons = num_weapons if vehicle_block else 1
        self.project.num_weapons_max = (
            num_weapons_max if vehicle_block else 1)
        self.project.cockpit_camera_enabled = (
            vehicle_block and cockpit_enabled)
        self.project.cockpit_camera_offset_x = (
            cockpit_x if vehicle_block else 0.0)
        self.project.cockpit_camera_offset_y = (
            cockpit_y if vehicle_block else 0.0)
        self.project.cockpit_camera_offset_z = (
            cockpit_z if vehicle_block else 0.0)
        self.project.gun_points_enabled = vehicle_block and gun_enabled
        self.project.gun_points = gun_points if vehicle_block else []
        self.project.unit_gun_default_icon = (
            unit_gun_default_icon if vehicle_block else "")
        self.project.turret_limits = (
            script_turret_limits if vehicle_block else {})
        self._capture_loaded_gun_points()
        self._capture_loaded_cockpit_camera()
        self._capture_tab_reset_baseline()
        if block.name:
            self.project.name = block.name
        self._selected = 0 if self.project.spheres() else -1
        self._selected_spheres = (
            {self._selected} if self._selected >= 0 else set())
        self._selected_fire_point = -1
        self._selected_gun_point = -1
        self._last_directory = Path(path).parent
        self._set_modified()
        self._sync_all()
        message = (
            f"Imported {len(compound)} compound sphere(s)"
            + (" and Legacy Radius" if legacy else "")
            + (" and Overeof" if self.project.overeof_enabled else "")
            + (" and Fire Points" if self.project.fire_points_enabled else "")
            + (" and Gun Points" if self.project.gun_points_enabled else "")
            + (" and Cockpit View"
               if self.project.cockpit_camera_enabled else "")
            + ".")
        if warnings:
            message += "\n\n" + "\n".join(warnings)
            QMessageBox.warning(self, "Imported with warnings", message)
        else:
            self.statusBar().showMessage(message, 7000)

    def apply_to_script(self):
        if not self._validate_for_output():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Apply to Script", str(self._last_directory),
            OPENNEOUA_SCRIPT_FILE_FILTER)
        if not path:
            return
        self._last_directory = Path(path).parent
        dialog = ApplyScriptDialog(
            self, self.project, initial_path=path)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            # Apply to Script can target a different definition: updating it
            # must not silently mark the currently loaded source as saved.
            same_target = (
                self._active_script_path is not None
                and self._active_script_id == dialog.id_spin.value()
                and self._active_script_kind == dialog.kind_combo.currentText()
                and self._active_script_path.resolve(strict=False)
                == Path(path).resolve(strict=False))
            if same_target:
                if self._refresh_linked_script_baseline():
                    remaining = self._changed_script_tabs()
                    if remaining:
                        names = ", ".join(
                            _SCRIPT_TAB_LABELS[group]
                            for group in _SCRIPT_TAB_ORDER if group in remaining)
                        self.statusBar().showMessage(
                            "Script updated. Still different from the script: "
                            f"{names}.", 10000)
                    else:
                        self.statusBar().showMessage(
                            "Script updated. All changes applied.", 10000)
                else:
                    self.statusBar().showMessage(
                        "Script updated, but the loaded baseline could not be "
                        "verified. Reload the definition.", 10000)
            elif self._active_script_path is None:
                self._capture_tab_reset_baseline()
                self._set_modified(False)
                self.statusBar().showMessage("Script updated.", 10000)
            else:
                self.statusBar().showMessage(
                    "Script updated. The loaded definition was not changed.",
                    10000)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if not self.fx_panel.confirm_text_changes():
            event.ignore()
            return
        if self._modified:
            answer = QMessageBox.question(
                self, "Unsaved Entity Designer changes",
                self._unsaved_changes_message("Close"),
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        if self._collision_bake_process is not None:
            self._collision_bake_cancelled = True
            self._collision_bake_process.kill()
            self._collision_bake_process.waitForFinished(3000)
        self._cleanup_collision_bake_tempdir()
        event.accept()
