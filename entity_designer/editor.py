"""The Entity Designer main window (``CollisionEditorWindow``).

It ties the tabs together: collision spheres/shape, ground, fire and gun points, cockpit, briefing size and
the Destruction FX preview. Script reading/writing lives in ``script_model``; the viewport, widgets and dialogs
have their own modules (see ``entity_designer/__init__.py``).
"""

from __future__ import annotations

import math
from pathlib import Path

from PySide6.QtCore import (
    QEvent,
    QFileSystemWatcher,
    QProcess,
    QSignalBlocker,
    Qt,
    QTimer,
)
from PySide6.QtGui import (
    QAction,
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
    QVBoxLayout,
    QWidget,
)

from entity_designer import destruction_fx as fx
from entity_designer.dialogs import (
    ApplyScriptDialog,
    ImportCollisionDialog,
)
from entity_designer.fire_area import FireArea
from entity_designer.fire_area_editing import FireAreaEditingMixin
from entity_designer.fx_panel import DestructionFxPanel
from entity_designer.script_model import (
    _clamp_gun_angle,
    _gun_angle_max,
    _normalized_vector3,
    _number,
    _project_num_weapons_range,
    _RADIUS_LOG_MAX,
    _RADIUS_LOG_MIN,
    _radius_number,
    _RADIUS_SLIDER_STEPS,
    _SCRIPT_TAB_LABELS,
    _SCRIPT_TAB_ORDER,
    CollisionProject,
    CollisionScriptError,
    export_collision_text,
    fire_point_positions,
    gun_angle_degrees,
    gun_direction_index,
    GUN_DIRECTION_LABELS,
    GUN_DIRECTION_SEQUENCE,
    GUN_FREE_SIDE_THRESHOLD,
    GUN_POINT_TABLE_BASE_WIDTHS,
    GUN_POINT_TABLE_GROWTH,
    GunPoint,
    import_briefing_size_block,
    import_briefing_xyz_block,
    import_cockpit_camera_block,
    import_collision_block,
    import_fire_area_block,
    import_type_icon_block,
    import_wireframe_block,
    import_fire_points_block,
    import_gun_points_block,
    import_overeof_block,
    import_turret_limits,
    LEGACY,
    OPENNEOUA,
    OPENNEOUA_SCRIPT_FILE_FILTER,
    read_script_file,
    TurretLimits,
    TYPE_LABELS,
    validate_project,
    VEHICLE,
    VehicleModelReference,
    WEAPON,
    WINDOW_TITLE,
)
from entity_designer.sphere_generator import (
    ACCURACY_PRESETS,
    UNIT_COLL_MAX_COUNT,
)
from entity_designer.viewport import CollisionMoveGizmo, CollisionViewport
from entity_designer.widgets import (
    _type_button_stylesheet,
    CompactScaleSpinBox,
    GeometryPartSelectionWidget,
    SphereTreeWidget,
)
from studio.assets.family import AssetFamily
from studio.assets.visproto import (
    EmbeddedVPSet,
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
    TURRET_DOWN_COLOR,
    TURRET_SIDE_COLOR,
    TURRET_UP_COLOR,
)
from entity_designer.editor_common import (  # re-exported for existing callers
    _NO_HEIGHT_LIMIT as _NO_HEIGHT_LIMIT,
    _MODEL_NAME_ROLE as _MODEL_NAME_ROLE,
    _SPHERE_INDEX_ROLE as _SPHERE_INDEX_ROLE,
    _MODEL_VP_ROLE as _MODEL_VP_ROLE,
    _FIRE_POINT_INDEX_ROLE as _FIRE_POINT_INDEX_ROLE,
    _GUN_POINT_INDEX_ROLE as _GUN_POINT_INDEX_ROLE,
    _SCRIPT_REFERENCE_ROLE as _SCRIPT_REFERENCE_ROLE,
    _fx_item_state as _fx_item_state,
    _vp_ids_per_model as _vp_ids_per_model,
)
from entity_designer.editor_files import _ScriptFilesMixin
from entity_designer.editor_collision_shape import _CollisionShapeMixin
from entity_designer.editor_models import _ModelBrowserMixin
from entity_designer.editor_spheres import _SpheresMixin
from entity_designer.editor_fx import _FxPreviewMixin
from entity_designer.editor_points import _WeaponPointsMixin
from entity_designer.editor_baseline import _TabBaselineMixin
from entity_designer.editor_controls import _EditControlsMixin


class CollisionEditorWindow(
    _ScriptFilesMixin,
    _CollisionShapeMixin,
    _ModelBrowserMixin,
    _SpheresMixin,
    _FxPreviewMixin,
    _WeaponPointsMixin,
    _TabBaselineMixin,
    _EditControlsMixin,
    FireAreaEditingMixin, QMainWindow,
):
    """Manual collision editor built on OpenNeoUAStudio's existing systems."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        from studio.ui.style import apply_studio_style
        apply_studio_style()
        self.setWindowTitle(WINDOW_TITLE)
        self.resize(1100, 720)
        self.project = CollisionProject()
        self._init_fire_area()
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
        # Model state shown in the viewport (preview only, never written to the
        # script): state name -> VP id of the unit, and the state on screen.
        self._model_state_vps: dict[str, int] = {}
        self._model_state = "normal"
        self._fx_base_unit = None
        self._normal_state_owner: str | None = None
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
        # (SET.BAS path, (family, {vp: owner})) read on demand for loose-BASE units.
        self._fx_set_cache = None
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
                self.open_base_action,
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
        toolbar.addWidget(QLabel(" Model state: "))
        self.toolbar_model_state_combo = QComboBox()
        self.toolbar_model_state_combo.setMinimumWidth(150)
        self.toolbar_model_state_combo.setToolTip(
            "Model state shown in the viewport: Normal, or any other state "
            "(Wait, Fire, Dead...) this unit defines. The collision shape is "
            "generated from the state on screen. Preview only: the script is "
            "not changed.")
        self.toolbar_model_state_combo.addItem("Normal", "normal")
        self.toolbar_model_state_combo.setEnabled(False)
        self.toolbar_model_state_combo.currentIndexChanged.connect(
            self._on_model_state_changed)
        toolbar.addWidget(self.toolbar_model_state_combo)
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
        self.briefing_tab_index = self.properties_tabs.addTab(self.briefing_tab, "Briefing Model Settings")
        self.briefing_preview = BriefingPreview(self)
        self.renderer_badge.add_viewport(self.briefing_preview)
        self.viewport_layout.addWidget(self.briefing_preview, 0, 0)
        self.briefing_preview.hide()
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
        # Icon Selector: the font page replaces the viewport while the tab is open.
        from entity_designer.icon_selector_panel import (
            TAB_TITLE as ICON_TAB_TITLE, IconSelectorControls)
        self.icon_tab = IconSelectorControls(self)
        self.icon_tab_index = self.properties_tabs.addTab(
            self.icon_tab, ICON_TAB_TITLE)
        self.viewport_layout.addWidget(self.icon_tab.view, 0, 0)
        self.icon_tab.view.hide()
        self.fire_area_tab = self._build_fire_area_tab()
        self.fire_area_tab_index = self.properties_tabs.addTab(
            self.fire_area_tab, "Fire Area")
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
                "wireframe key. An exported file inside Data becomes the new "
                "wireframe value, written by Overwrite, Apply to Script and Copy."),
            self.fire_area_tab: (
                COMPAT_OPENNEOUA, "ai_fire_trigger_* of a weapon are read "
                "only by OpenNeoUA, and only by the AI: they decide where "
                "it opens fire. Vanilla ignores them."),
            self.icon_tab: (
                MIXED, "type_icon is vanilla. New symbols are one byte in "
                "TYPE_NS.FON, as vanilla reads them. Plain ASCII symbols work "
                "in vanilla from any script; other symbols are written as "
                "U+XXXX, which only OpenNeoUA reads. Weapons can browse the "
                "page but get no icon."),
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
        self.delete_collision_shape_button = QPushButton("Delete")
        self.delete_collision_shape_button.setToolTip(
            "Delete the collision shape loaded in this unit.")
        self.delete_collision_shape_button.clicked.connect(
            self.delete_collision_shape)
        self.delete_collision_shape_button.setEnabled(False)
        self.overwrite_collision_shape_button = QPushButton("Overwrite")
        self.overwrite_collision_shape_button.setToolTip(
            "Write the shape back to the .collision file it was imported "
            "from or last exported to. Export a new shape first.")
        self.overwrite_collision_shape_button.clicked.connect(
            self.overwrite_collision_shape)
        self.overwrite_collision_shape_button.setEnabled(False)
        shape_buttons = QHBoxLayout()
        shape_buttons.addWidget(self.generate_collision_shape_button, 1)
        shape_buttons.addWidget(self.collision_shape_quality)
        shape_layout.addLayout(shape_buttons)
        # Two rows of two buttons: every column takes half of the panel width.
        shape_io = QGridLayout()
        shape_io.setSpacing(4)
        shape_io.addWidget(self.import_collision_shape_button, 0, 0)
        shape_io.addWidget(self.export_collision_shape_button, 0, 1)
        shape_io.addWidget(self.delete_collision_shape_button, 1, 0)
        shape_io.addWidget(self.overwrite_collision_shape_button, 1, 1)
        shape_io.setColumnStretch(0, 1)
        shape_io.setColumnStretch(1, 1)
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
        # Free space below the gizmo; only Cockpit View uses it (see _fit_cockpit_tab_height).
        right.addStretch(0)
        self._properties_tail_index = right.count() - 1
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

    def _has_unexported_work(self) -> bool:
        """True only when closing or switching unit would lose real edits.

        ``_modified`` is also raised by merely opening a model or picking one
        from the list. Those only change which model is shown (source_model /
        source_base) and can be redone with one click, so they do not count.
        """

        if not self._modified:
            return False
        baseline = CollisionProject()
        baseline.restore(self._export_baseline)
        baseline.source_model = self.project.source_model
        baseline.source_base = self.project.source_base
        return (self.project.snapshot() != baseline.snapshot()
                or bool(self._changed_script_tabs()))

    def _unsaved_changes_message(self, action) -> str:
        baseline = CollisionProject()
        baseline.restore(self._export_baseline)
        # Tabs whose values are not yet written to the script, so the user can see
        # what is still pending (e.g. Fire Area is never written by the shape export).
        pending = self._changed_script_tabs()
        names = ", ".join(_SCRIPT_TAB_LABELS[group]
                          for group in _SCRIPT_TAB_ORDER if group in pending)
        detail = f" Not written to the script yet: {names}." if names else ""
        if (self.project.collision_shape is not None
                and self.project.collision_shape == baseline.collision_shape
                and self.project.collision_shape_file_path):
            return ("The collision shape file is saved, but other project or script "
                    f"changes are unsaved.{detail} {action} anyway?")
        return f"The collision project has unexported changes.{detail} {action} anyway?"

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
            # A loose BASE unit is not in the VP list: its owner comes from here.
            owners.add(self._collision_shape_root_owner())
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
        # Weapons keep the tabs that also apply to projectiles.
        if (not vehicle_mode
                and self.properties_tabs.currentIndex()
                not in (self.collision_tab_index, self.collision_shape_tab_index,
                        self.fx_tab_index, self.wireframe_tab_index,
                        self.icon_tab_index, self.fire_area_tab_index)):
            self.properties_tabs.setCurrentIndex(self.collision_tab_index)
        for index in (
                self.ground_tab_index,
                self.fire_points_tab_index, self.gun_points_tab_index,
                self.cockpit_tab_index, self.briefing_tab_index):
            self.properties_tabs.setTabEnabled(index, vehicle_mode)
        # ai_fire_trigger_* belong to the weapon: the tab is grey once a vehicle
        # definition is loaded. With nothing loaded yet it stays open and empty.
        vehicle_loaded = vehicle_mode and self._active_script_kind == "new_vehicle"
        if vehicle_loaded and self.properties_tabs.currentIndex() == self.fire_area_tab_index:
            self.properties_tabs.setCurrentIndex(self.collision_tab_index)
        self.properties_tabs.setTabEnabled(self.fire_area_tab_index, not vehicle_loaded)
        self._sync_fire_area()

        cockpit_enabled = vehicle_mode and self.project.cockpit_camera_enabled
        self.cockpit_camera_button.setEnabled(vehicle_mode)
        self.cockpit_camera_button.setText(
            "Disable Cockpit Camera Offset" if cockpit_enabled
            else "Enable Cockpit Camera Offset")
        self.cockpit_controls.setVisible(cockpit_enabled)
        # The rows above changed: the Cockpit View height follows them.
        QTimer.singleShot(0, self._fit_cockpit_tab_height)
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
        # Briefing and Icon Selector pages replace the viewport: nothing to move.
        briefing_active = self.properties_tabs.currentIndex() in (
            self.briefing_tab_index, self.icon_tab_index)
        self.transform_box.setVisible(
            not shape_tab_active and not briefing_active
            and self.properties_tabs.currentIndex() != self.ground_tab_index
            and self.properties_tabs.currentIndex() != self.wireframe_tab_index
            and not self._is_fx_tab_active()
            and self.properties_tabs.currentIndex() != self.fire_area_tab_index)
        self._sync_fx_preview()
        if self._fire_area_frame_pending and self._is_fire_area_tab_active():
            self._fire_area_frame_pending = False
            self._fire_area_frame(unit_only=True)

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
        self.icon_tab.sync()

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
            imported_fire_area = (
                import_fire_area_block(text, block)
                if "weapon" in block.kind else FireArea())
        except CollisionScriptError as exc:
            QMessageBox.warning(self, "Import failed", str(exc))
            return
        self._clear_script_link()
        self._push_undo()
        self.project.legacy = legacy
        self.project.compound = compound
        self.project.fire_area = imported_fire_area
        self.project.target_category = (
            WEAPON if "weapon" in block.kind else VEHICLE)
        self.project.type_icon = (
            import_type_icon_block(text, block)
            if self.project.target_category == VEHICLE else 0)
        self.project.type_icon_cell = None
        self.project.wireframe = import_wireframe_block(text, block)
        self.project.overeof_enabled = (
            self.project.target_category == VEHICLE and overeof_enabled)
        self.project.overeof = (
            overeof if self.project.overeof_enabled else 0.0)
        vehicle_block = self.project.target_category == VEHICLE
        self.project.briefing_size_enabled, self.project.briefing_size = (
            import_briefing_size_block(text, block) if vehicle_block else (False, 0.0))
        xyz = import_briefing_xyz_block(text, block) if vehicle_block else (False, 0.0, 0.0, 0.0)
        self.project.briefing_xyz_enabled, self.project.briefing_x, self.project.briefing_y, self.project.briefing_z = xyz
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
        if self._has_unexported_work():
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
