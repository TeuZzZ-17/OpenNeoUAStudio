from __future__ import annotations

import os
import time
import copy
import math

from PySide6.QtCore import QEvent, QObject, QRunnable, QSize, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import (QAction, QActionGroup, QColor, QIcon, QImage, QKeySequence,
                           QPainter, QPen, QPixmap)
from PySide6.QtWidgets import (QApplication, QAbstractItemView, QComboBox, QDockWidget, QFileDialog,
                               QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListView, QListWidget,
                               QListWidgetItem, QMainWindow, QMessageBox, QPushButton,
                               QSlider, QVBoxLayout,
                               QWidget, QToolButton, QGroupBox, QSpinBox, QCheckBox,
                               QPlainTextEdit, QScrollArea, QDialog, QDialogButtonBox, QMenu)

from ..core.asset_bridge import SetAssets
from ..core.building_defs import load_building_files
from ..core.game_installation import GameInstallation, load_remembered
from ..core.factions import load_owner_colors
from ..core.history import History
from ..core.ldf_model import (DEFAULT_HGT, FACTIONS, HGT_MIN, HGT_MAX,
                              LdfDocument, load_ldf, save_ldf, grid_to_world, world_to_grid, decode_ldf_bytes, SECTOR_SIZE)
from ..render.map_viewport import create_viewport
from ..render.terrain_mesh import HEIGHT_UNIT
from ..render.sector_mesh import SectorMeshLibrary
from ..render.sector_sprite import rasterize_sprite, sprite_input
from ..tools.brush import BrushMode, BrushShape, TerrainBrush
from ..tools.paint import paint_cells
from .dialogs import GameInstallationDialog, LevelInfoPanel, NewMapDialog, ResizeDialog
from .music_preview import MusicPreview
from .building_overlay import BuildingOverlay, CapabilityPreview
from .thumbnail_delegate import ThumbnailDelegate
from .squad_panel import SquadPanel
from .host_panel import HostPanel
from .tech_panel import TechPanel
from .preview_cards import RESOURCE_ROLE
from ..core.ldf_model import ensure_host_defaults, make_host_ai
from .squad_overlay import SquadOverlay
from .colored_tabs import PaletteTabs, TAB_COLORS
from ..render.squad_scene import squad_xz, centered_squad_position, host_position, MAX_PREVIEW_MEMBERS
from .. import bootstrap
from assembly_viewer import VIEW_PRESET_ANGLES

TOOLS = (("select", "Select"), ("sector", "Sector"), ("building", "Building"),
         ("owner", "Faction"), ("terrain", "Terrain"), ("squad", "Squad"))
SECTOR_PREVIEW_SCALES = (0.7, 1.1, 1.8)
SECTOR_PREVIEW_COLUMNS = (3, 2, 1)
BUILDING_PREVIEW_PIXELS = (96, 128, 176)
PALETTE_BATCH_SECONDS = .006


class _IconSignals(QObject):
    finished = Signal(object)


class _IconJob(QRunnable):
    def __init__(self, data, tables, set_number, typ, scale, epoch):
        super().__init__()
        self.signals = _IconSignals()
        self.data, self.tables = data, tables
        self.set_number, self.typ, self.scale, self.epoch = set_number, typ, scale, epoch

    def run(self):
        try:
            sprite = rasterize_sprite(self.data, self.tables, .13 * self.scale)
        except Exception:
            sprite = None
        self.signals.finished.emit((self.set_number, self.typ, self.scale, self.epoch, sprite))


def crop_transparent(image: QImage, margin: int = 3) -> QImage:
    """Trim fully transparent borders so grid icons stay centred and compact."""
    if image.isNull():
        return image
    converted = image.convertToFormat(QImage.Format.Format_RGBA8888)
    width, height = converted.width(), converted.height()
    try:
        import numpy as np
        pixels = np.frombuffer(converted.bits(), np.uint8).reshape(height, width, 4)
        alpha = pixels[:, :, 3]
        rows = np.any(alpha > 8, axis=1)
        cols = np.any(alpha > 8, axis=0)
        if not rows.any() or not cols.any():
            return image
        top, bottom = int(np.argmax(rows)), int(len(rows) - 1 - np.argmax(rows[::-1]))
        left, right = int(np.argmax(cols)), int(len(cols) - 1 - np.argmax(cols[::-1]))
        top = max(0, top - margin)
        left = max(0, left - margin)
        bottom = min(height - 1, bottom + margin)
        right = min(width - 1, right + margin)
        return converted.copy(left, top, right - left + 1, bottom - top + 1)
    except Exception:
        return image


class MainWindow(QMainWindow):
    def __init__(self, doc: LdfDocument | None = None, path: str | None = None):
        super().__init__()
        self.setWindowTitle("OpenNeoUA Studio - Map Editor")
        self.resize(1500, 900)
        self.doc: LdfDocument | None = None
        self.path: str | None = None
        self.dirty = False
        self.history = History()
        self._briefing_pool = QThreadPool(self)
        self._briefing_pool.setMaxThreadCount(1)
        self.libs: dict[int, SectorMeshLibrary] = {}
        self.brush = TerrainBrush()
        self.tool = "sector"
        self.sel_typ = 5
        self.sel_building = 1
        self.sel_owner = 1
        self.buildings: dict = {}
        self._sec_index: dict = {}
        self._bicon_level = 2
        self._stroke_active = False
        self._stroke_changed = False
        self._stroke_mode = BrushMode.RAISE
        self._last_cell: tuple[int, int] | None = None
        self._terrain_tick = 0.0
        self._icon_queue: list[tuple[int, QListWidgetItem]] = []
        self._palette_set = None
        self._icon_cache = {}
        self._icon_items = {}
        self._icon_job = None
        self._icon_level = 1
        self._icon_scale = SECTOR_PREVIEW_SCALES[self._icon_level]
        self._bicon_queue: list = []
        self._bicon_items = {}
        self._bicon_rows = {}
        self._bicon_cache = {}
        self._asset_epoch = 0
        self._sky_state = None
        self.level_panel = None
        self._draft_squads = []
        self._draft_host = None
        self._resource_icons = {}
        self._clipboard = None
        self._draft_grid = None
        self._draft_grid_cell = None
        self._draft_origin = []
        self._draft_previous_selection = set()
        self._live_pending = False
        self._drag_original = {}
        self._drag_cell = None
        self._drag_changed = False
        self._drag_kind = 'squad'
        self._sampling_height = False
        self._script_pending = False
        self._icon_pool = QThreadPool(self)
        self._icon_pool.setMaxThreadCount(1)

        self.view = create_viewport()
        self.music_preview = MusicPreview(self)
        self.view.owner_colors = load_owner_colors()
        self.setCentralWidget(self.view)
        self.building_overlay = BuildingOverlay(self.view, self._overlay_state)
        self.squad_overlay = SquadOverlay(self.view)
        self.view.squadPressed.connect(self._select_squad)
        self.view.hostPressed.connect(self._select_host)
        self.view.actorDragStarted.connect(self._begin_actor_drag)
        self.view.actorDragged.connect(self._move_actor_drag)
        self.view.actorDragFinished.connect(self._finish_actor_drag)
        self.view.cellSelected.connect(self._select_cell)
        self.view.cellSwept.connect(self._sweep_cell)
        self.view.selectionCleared.connect(self._clear_selection)
        self.view.contextRequested.connect(self._map_context_menu)
        self.view.placementConfirmed.connect(self._confirm_placement)
        self.view.operationCancelled.connect(self._cancel_operation)
        self.view.terrain_brush = self.brush
        self.view.cellPressed.connect(self._pressed)
        self.view.cellDragged.connect(self._dragged)
        self.view.cellReleased.connect(self._released)
        self.view.cellDoubleClicked.connect(self._double_clicked)
        self.view.cellHovered.connect(self._hovered)
        self.view.statusMessage.connect(lambda m: self.statusBar().showMessage(m, 4000))
        self._repeat = QTimer(self, interval=60)
        self._repeat.timeout.connect(self._repeat_terrain)
        self._icons = QTimer(self, singleShot=True, interval=1)
        self._icons.timeout.connect(self._make_icon)
        self._script_timer = QTimer(self, singleShot=True, interval=600)
        self._script_timer.timeout.connect(self._finish_script)
        self._live_timer = QTimer(self, singleShot=True, interval=500)
        self._live_timer.timeout.connect(self._finish_live)

        self._build_actions()
        self._build_palette()
        self.info = QLabel("")
        self.statusBar().addPermanentWidget(self.info)
        from render_status import add_renderer_badge
        self.renderer_badge = add_renderer_badge(self, self.view)
        self.statusBar().showMessage("Left drag: paint / move squad · Shift: select sectors · Ctrl+click: multi-select · right: menu / drag rotate · middle: pan", 8000)
        self._new_doc(doc if doc is not None else LdfDocument(mw=15, mh=15), path)

    def _overlay_state(self):
        return {
            "doc": self.doc,
            "lib": self._lib(),
            "buildings": self.buildings,
        }

    def _build_actions(self):
        bar = self.menuBar()
        file_menu = bar.addMenu("&File")
        edit_menu = bar.addMenu("&Edit")
        map_menu = bar.addMenu("&Map")
        view_menu = bar.addMenu("&View")

        def act(menu, text, slot, shortcut=None):
            action = QAction(text, self)
            action.triggered.connect(slot)
            if shortcut:
                action.setShortcut(shortcut)
            menu.addAction(action)
            return action

        act(file_menu, "New...", self.file_new, QKeySequence.StandardKey.New)
        act(file_menu, "Open...", self.file_open, QKeySequence.StandardKey.Open)
        act(file_menu, "Save", self.file_save, QKeySequence.StandardKey.Save)
        act(file_menu, "Save as...", self.file_save_as,
            QKeySequence.StandardKey.SaveAs)
        act(file_menu, "Game folders...", self.file_game_folders)
        self.undo_action = act(edit_menu, "Undo", self.undo, QKeySequence.StandardKey.Undo)
        self.redo_action = act(edit_menu, "Redo", self.redo, QKeySequence.StandardKey.Redo)
        act(map_menu, "Resize...", self.map_resize)
        self.reset_action = act(map_menu, "Reset...", self.map_reset)
        self.reset_camera_action = act(view_menu, 'Reset Camera', self.view.reset_camera, 'Home')
        self.copy_action = act(edit_menu, 'Copy', self._copy_elements, 'Ctrl+C')
        self.paste_action = act(edit_menu, 'Paste', self._paste_elements, 'Ctrl+V')
        act(map_menu, "Fill...", self.map_fill)
        act(map_menu, "Level Info", self.map_level_info)
        tools_menu = edit_menu.addMenu("Tool")
        group = QActionGroup(self)
        self.tool_actions = {}
        for key, label in TOOLS:
            action = QAction(label, self, checkable=True)
            action.triggered.connect(lambda _c, k=key: self.set_tool(k))
            group.addAction(action)
            tools_menu.addAction(action)
            self.tool_actions[key] = action
        self.tool_actions[self.tool].setChecked(True)
        self.grid_action = QAction("Grid", self, checkable=True, checked=True)
        self.grid_action.setShortcut("G")
        self.grid_action.toggled.connect(self.view.set_grid)
        view_menu.addAction(self.grid_action)
        self.sky_action = QAction("Sky background", self, checkable=True, checked=True)
        self.sky_action.toggled.connect(self.view.set_sky_visible)
        view_menu.addAction(self.sky_action)
        self.music_action = QAction("Music preview", self, checkable=True, checked=True)
        self.music_action.toggled.connect(self.music_preview.set_enabled)
        view_menu.addAction(self.music_action)
        self.palette_menu = view_menu

    def _build_palette(self):
        dock = QDockWidget("Menu", self)
        self.palette_dock = dock
        dock.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea
                             | Qt.DockWidgetArea.RightDockWidgetArea)
        tabs = PaletteTabs()
        self.palette_tabs = tabs
        panel = QWidget()
        panel_layout = QVBoxLayout(panel)
        history_row = QHBoxLayout()
        history_row.addStretch()
        self.undo_button = QToolButton()
        self.undo_button.setDefaultAction(self.undo_action)
        self.undo_action.setText("< Undo")
        self.redo_button = QToolButton()
        self.redo_button.setDefaultAction(self.redo_action)
        self.redo_action.setText("Redo >")
        self.reset_button = QToolButton()
        self.reset_button.setDefaultAction(self.reset_action)
        self.reset_action.setText('Reset Map')
        self.reset_camera_button = QToolButton()
        self.reset_camera_button.setDefaultAction(self.reset_camera_action)
        for button in (self.undo_button, self.reset_button, self.reset_camera_button, self.redo_button):
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
            history_row.addWidget(button)
        history_row.addStretch()
        panel_layout.addLayout(history_row)
        preset_form = QFormLayout()
        self.view_preset = QComboBox()
        self.view_preset.addItem('Current View', None)
        for name, angles in VIEW_PRESET_ANGLES.items():
            if angles[1] > 0:
                self.view_preset.addItem(name, angles)
        self.view_preset.activated.connect(self._set_view_preset)
        self.view.cameraChanged.connect(lambda: self.view_preset.setCurrentIndex(0))
        preset_form.addRow('View preset', self.view_preset)
        panel_layout.addLayout(preset_form)
        panel_layout.addWidget(tabs)
        self.sector_list = self._make_list(True)
        self.sector_list.setItemDelegate(ThumbnailDelegate(self.sector_list))
        self.sector_list.viewport().installEventFilter(self)
        self.sector_list.verticalScrollBar().valueChanged.connect(lambda _: self._icons.start())
        self.sector_list.currentItemChanged.connect(self._sector_selected)
        self.sector_list.itemClicked.connect(self._sector_selected)
        self.sector_filter = QLineEdit(placeholderText="Filter sectors...")
        self.sector_filter.textChanged.connect(lambda t: self._filter(self.sector_list, t))
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(self.sector_filter)
        zoom_row = QHBoxLayout()
        zoom_row.addWidget(QLabel("Preview size"))
        zoom_row.addStretch()
        self.sector_zoom_out = QPushButton("−")
        self.sector_zoom_out.setToolTip("Smaller sector previews")
        self.sector_zoom_out.clicked.connect(lambda: self._sector_zoom_step(-1))
        zoom_row.addWidget(self.sector_zoom_out)
        self.sector_zoom_label = QLabel("2/3")
        zoom_row.addWidget(self.sector_zoom_label)
        self.sector_zoom_in = QPushButton("+")
        self.sector_zoom_in.setToolTip("Larger sector previews")
        self.sector_zoom_in.clicked.connect(lambda: self._sector_zoom_step(1))
        zoom_row.addWidget(self.sector_zoom_in)
        layout.addLayout(zoom_row)
        layout.addWidget(self.sector_list)
        tabs.addTab(page, "Sectors")
        self._layout_sector_icons()

        buildings_page = QWidget()
        buildings_layout = QVBoxLayout(buildings_page)
        self.building_filter = QLineEdit(placeholderText="Filter buildings...")
        buildings_layout.addWidget(self.building_filter)
        self.show_special_buildings = QCheckBox('Show special buildings')
        self.show_special_buildings.toggled.connect(lambda: self._fill_buildings(self._lib()) if self._lib() else None)
        buildings_layout.addWidget(self.show_special_buildings)
        building_zoom_row = QHBoxLayout()
        building_zoom_row.addWidget(QLabel("Preview size"))
        building_zoom_row.addStretch()
        self.building_zoom_out = QPushButton("−")
        self.building_zoom_out.setToolTip("Smaller building previews")
        self.building_zoom_out.clicked.connect(lambda: self._building_zoom_step(-1))
        building_zoom_row.addWidget(self.building_zoom_out)
        self.building_zoom_label = QLabel("3/3")
        building_zoom_row.addWidget(self.building_zoom_label)
        self.building_zoom_in = QPushButton("+")
        self.building_zoom_in.setToolTip("Larger building previews")
        self.building_zoom_in.clicked.connect(lambda: self._building_zoom_step(1))
        building_zoom_row.addWidget(self.building_zoom_in)
        self.building_zoom_out.setFixedWidth(32)
        self.building_zoom_in.setFixedWidth(32)
        self.building_zoom_in.setEnabled(False)
        buildings_layout.addLayout(building_zoom_row)
        self.building_list = QListWidget()
        self.building_list.setViewMode(QListWidget.ViewMode.ListMode)
        self.building_list.setFlow(QListView.Flow.TopToBottom)
        self.building_list.setWrapping(False)
        self.building_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.building_list.setMovement(QListWidget.Movement.Static)
        self.building_list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.building_list.setSpacing(4)
        self.building_list.verticalScrollBar().valueChanged.connect(lambda _: self._icons.start())
        self.building_list.currentItemChanged.connect(self._building_selected)
        self.building_list.itemClicked.connect(self._building_selected)
        self.building_filter.textChanged.connect(lambda t: self._filter(self.building_list, t))
        buildings_layout.addWidget(self.building_list)
        tabs.addTab(buildings_page, "Buildings")
        self._layout_building_rows()

        self.owner_list = QListWidget()
        font = self.owner_list.font()
        font.setPointSizeF(max(13, font.pointSizeF() + 4))
        self.owner_list.setFont(font)
        self.owner_list.setIconSize(QSize(36, 36))
        self.owner_list.setSpacing(5)
        for owner, name in FACTIONS.items():
            item = QListWidgetItem(f"{owner} {name}")
            item.setData(Qt.ItemDataRole.UserRole, owner)
            item.setSizeHint(QSize(0, 54))
            self.owner_list.addItem(item)
        self._refresh_owner_palette()
        self.owner_list.setCurrentRow(self.sel_owner)
        self.owner_list.currentItemChanged.connect(self._owner_selected)
        self.owner_list.itemClicked.connect(self._owner_selected)
        tabs.addTab(self.owner_list, "Factions")

        terrain = QWidget()
        form = QFormLayout(terrain)
        self.mode_combo = QComboBox()
        for mode, label in ((BrushMode.RAISE, "Raise (Shift lowers)"),
                            (BrushMode.LOWER, "Lower"),
                            (BrushMode.FLATTEN, "Flatten (Ctrl)"),
                            (BrushMode.SMOOTH, "Smooth (Alt / double-click)")):
            self.mode_combo.addItem(label, mode)
        self.mode_combo.hide()
        mode_row = QHBoxLayout()
        self.terrain_buttons = {}
        for index, text in enumerate(("Raise", "Lower", "Flatten", "Smooth")):
            button = QPushButton(text, checkable=True)
            button.clicked.connect(lambda _checked, i=index: self.mode_combo.setCurrentIndex(i))
            mode_row.addWidget(button)
            self.terrain_buttons[index] = button
        self.mode_combo.currentIndexChanged.connect(self._terrain_tool_changed)
        self._terrain_tool_changed(0)
        self.shape_combo = QComboBox()
        for shape in BrushShape:
            self.shape_combo.addItem(shape.value.title(), shape)
        self.radius_x_slider = QSlider(Qt.Orientation.Horizontal, minimum=1, maximum=24, value=4)
        self.radius_x_slider.setToolTip("West–east brush radius in sectors")
        self.radius_x_slider.valueChanged.connect(self._radius_x_changed)
        self.radius_x_label = QLabel("2.0")
        self.radius_x_label.setMinimumWidth(44)
        radius_x_row = QHBoxLayout()
        radius_x_row.addWidget(self.radius_x_slider)
        radius_x_row.addWidget(self.radius_x_label)
        self.radius_z_slider = QSlider(Qt.Orientation.Horizontal, minimum=1, maximum=24, value=4)
        self.radius_z_slider.setToolTip("North–south brush radius in sectors")
        self.radius_z_slider.valueChanged.connect(self._radius_z_changed)
        self.radius_z_label = QLabel("2.0")
        self.radius_z_label.setMinimumWidth(44)
        radius_z_row = QHBoxLayout()
        radius_z_row.addWidget(self.radius_z_slider)
        radius_z_row.addWidget(self.radius_z_label)
        self.link_radii = QPushButton('Link X / Z', checkable=True, checked=True)
        self.link_radii.setToolTip('When linked, changing either radius moves both sliders')
        self.link_radii.toggled.connect(self._link_radii_changed)
        self.strength = QSlider(Qt.Orientation.Horizontal, minimum=1, maximum=30, value=6)
        self.strength.setToolTip("Terrain force in height steps per second")
        self.strength.valueChanged.connect(self._brush_params)
        self.force_label = QLabel("6")
        self.force_label.setMinimumWidth(30)
        force_row = QHBoxLayout()
        force_row.addWidget(self.strength)
        force_row.addWidget(self.force_label)
        self.shape_combo.currentIndexChanged.connect(self._brush_params)
        self.brush_label = QLabel()
        form.addRow(mode_row)
        form.addRow('Shape', self.shape_combo)
        form.addRow('Radius X', radius_x_row)
        form.addRow('Radius Z', radius_z_row)
        form.addRow(self.link_radii)
        form.addRow("Force", force_row)
        self.flatten_height = QSpinBox(minimum=0, maximum=60, value=30)
        self.flatten_height.setToolTip("Height 0–60; 30 is the original ground level")
        self.flatten_height.valueChanged.connect(self._brush_params)
        self.sample_height = QPushButton("Sample from map", checkable=True)
        self.sample_height.toggled.connect(self._sample_toggled)
        self.sample_height.setToolTip("Click a sector to set the flatten height without changing the map")
        target_row = QHBoxLayout()
        target_row.addWidget(self.flatten_height)
        target_row.addWidget(self.sample_height)
        form.addRow("Target height", target_row)
        form.addRow(self.brush_label)
        hint = QLabel("Drag to sculpt · Shift: lower · Alt: smooth · Ctrl: select")
        hint.setWordWrap(True)
        form.addRow(hint)
        tabs.addTab(terrain, "Terrain")
        self.level_scroll = QScrollArea()
        self.level_scroll.setWidgetResizable(True)
        self.script_page = QWidget()
        script_layout = QVBoxLayout(self.script_page)
        script_row = QHBoxLayout()
        for text, slot in (("Load script…", self._load_script), ("Save script…", self._save_script)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            script_row.addWidget(button)
        script_layout.addLayout(script_row)
        self.script_edit = QPlainTextEdit()
        self.script_edit.setPlaceholderText("Custom LDF commands and includes")
        self.script_edit.setUndoRedoEnabled(False)
        self.script_edit.installEventFilter(self)
        self.script_edit.textChanged.connect(self._script_changed)
        script_layout.addWidget(self.script_edit)
        self.squad_panel = SquadPanel()
        self.squad_panel.selected.connect(self._squad_selected)
        self.squad_panel.addRequested.connect(self._add_squad)
        self.squad_panel.valuesChanged.connect(self._live_squad_changed)
        self.squad_panel.removeRequested.connect(self._delete_squads)
        from .preview_cards import actor_scroll_area
        squad_scroll = actor_scroll_area(self.squad_panel)
        self.squad_tab_index = tabs.addTab(squad_scroll, "Squad")
        self.host_panel = HostPanel()
        self.host_panel.selected.connect(self._host_selected)
        self.host_panel.addRequested.connect(self._add_host)
        self.host_panel.removeRequested.connect(self._delete_host)
        self.host_panel.valuesChanged.connect(self._host_changed)
        self.host_panel.playerRequested.connect(self._host_player)
        host_scroll = actor_scroll_area(self.host_panel)
        self.host_tab_index = tabs.addTab(host_scroll, "Hosts")
        self.tech_panel = TechPanel()
        self.tech_panel.permissionChanged.connect(self._tech_changed)
        self.tech_tab_index = tabs.addTab(self.tech_panel, "Tech")
        for card_list in (self.squad_panel.list, self.host_panel.list, self.tech_panel.list):
            card_list.previewsRequested.connect(lambda: self._icons.start())
        self.script_tab_index = tabs.addTab(self.script_page, "Script")
        self.level_tab_index = tabs.addTab(self.level_scroll, "Level Info")
        self._brush_params()
        dock.setWidget(panel)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)
        dock.setMinimumWidth(400)
        self.palette_menu.addAction(dock.toggleViewAction())
        tabs.currentChanged.connect(self._palette_changed)
        for widget in (self.sector_list,self.building_list,self.owner_list,terrain,
                       self.squad_panel.list,self.host_panel.list,self.script_edit,self.level_scroll):
            widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            widget.customContextMenuRequested.connect(lambda pos, w=widget: self._panel_context_menu(w,pos))
        dock.visibilityChanged.connect(lambda visible: self._icons.start()
                                       if visible and (self._icon_queue or self._bicon_queue) else self._icons.stop())

    @staticmethod
    def _make_list(icons: bool) -> QListWidget:
        widget = QListWidget()
        if icons:
            widget.setViewMode(QListWidget.ViewMode.IconMode)
            widget.setFlow(QListView.Flow.LeftToRight)
            widget.setWrapping(True)
            widget.setUniformItemSizes(True)
            widget.setSpacing(4)
            widget.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
            widget.setIconSize(QSize(96, 80))
            widget.setGridSize(QSize(112, 118))
            widget.setResizeMode(QListWidget.ResizeMode.Adjust)
            widget.setMovement(QListWidget.Movement.Static)
            widget.setWordWrap(True)
        return widget

    def _filter(self, widget: QListWidget, text: str):
        text = text.lower()
        for i in range(widget.count()):
            item = widget.item(i)
            item.setHidden(text not in (item.text() + ' ' + item.toolTip()).lower())
        self._icons.start()

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.KeyPress and obj is self.script_edit:
            if event.matches(QKeySequence.StandardKey.Undo):
                self.undo()
                return True
            if event.matches(QKeySequence.StandardKey.Redo):
                self.redo()
                return True
        if obj is self.sector_list.viewport() and event.type() == QEvent.Type.Resize:
            QTimer.singleShot(0, self._layout_sector_icons)
        return super().eventFilter(obj, event)

    def _layout_sector_icons(self):
        columns = SECTOR_PREVIEW_COLUMNS[self._icon_level]
        style = self.sector_list.style()
        # The viewport already excludes the scrollbar; subtracting it twice
        # left the single-column selection card visibly off centre.
        available = max(60, self.sector_list.viewport().width())
        spacing = self.sector_list.spacing()
        cell_width = max(60, available // columns - 2 * spacing)
        icon_width = max(32, min(round(96 * self._icon_scale), cell_width - 12))
        icon_height = round(icon_width * 80 / 96)
        self.sector_list.setIconSize(QSize(icon_width, icon_height))
        self.sector_list.setGridSize(QSize(cell_width, icon_height + 42))

    def _sector_zoom_step(self, direction: int):
        level = max(0, min(2, self._icon_level + direction))
        if level == self._icon_level:
            return
        self._icon_level = level
        self._icon_scale = SECTOR_PREVIEW_SCALES[level]
        self.sector_zoom_label.setText(f"{level + 1}/3")
        self.sector_zoom_out.setEnabled(level > 0)
        self.sector_zoom_in.setEnabled(level < 2)
        self._layout_sector_icons()
        self._palette_set = None
        lib = self._lib()
        if lib is not None:
            self._fill_palettes(lib)

    def _link_radii_changed(self, checked):
        if checked:
            self.radius_z_slider.blockSignals(True)
            self.radius_z_slider.setValue(self.radius_x_slider.value())
            self.radius_z_slider.blockSignals(False)
        self.link_radii.setText('Linked X / Z' if checked else 'Independent X / Z')
        self._brush_params()

    def _radius_x_changed(self, value: int):
        if self.link_radii.isChecked():
            self.radius_z_slider.blockSignals(True)
            self.radius_z_slider.setValue(value)
            self.radius_z_slider.blockSignals(False)
        self._brush_params()

    def _radius_z_changed(self, value: int):
        if self.link_radii.isChecked():
            self.radius_x_slider.blockSignals(True)
            self.radius_x_slider.setValue(value)
            self.radius_x_slider.blockSignals(False)
        self._brush_params()

    def _brush_params(self):
        radius_x = self.radius_x_slider.value() / 2
        radius_z = self.radius_z_slider.value() / 2
        self.brush.radius_x = radius_x
        self.brush.radius_z = radius_z
        self.radius_x_label.setText(f"{radius_x:g}")
        self.radius_z_label.setText(f"{radius_z:g}")
        self.brush.shape = self.shape_combo.currentData()
        self.brush.strength = float(self.strength.value())
        self.brush.target_height = HGT_MIN + self.flatten_height.value()
        self.force_label.setText(f"{self.brush.strength:.0f}")
        equal = radius_x == radius_z
        shape = self.shape_combo.currentText()
        if self.brush.shape == BrushShape.ROUND:
            shape = 'Circle' if equal else 'Ellipse'
        self.brush_label.setText(f'{shape} · X {radius_x:g} · Z {radius_z:g} · Force {self.brush.strength:.0f} steps/s')
        self.brush_label.setToolTip('Radii in sectors. One step = 100 game units. Height 0–60, initial ground 30.')
        if self.doc is not None and self.view.hover is not None:
            self._hovered(*self.view.hover)

    def _set_view_preset(self, index):
        angles = self.view_preset.itemData(index)
        if angles is not None:
            self.view.set_camera_angles(*angles)
            self.view_preset.setCurrentIndex(index)

    def _terrain_tool_changed(self, index):
        for key, button in self.terrain_buttons.items():
            button.setChecked(key == index)
        self._update_cursor()

    def _update_cursor(self, modifiers=Qt.KeyboardModifier.NoModifier):
        colors = {BrushMode.RAISE: (80, 235, 130), BrushMode.LOWER: (255, 105, 95),
                  BrushMode.FLATTEN: (70, 190, 255), BrushMode.SMOOTH: (215, 130, 255)}
        self.view.cursor_color = (colors[self._terrain_mode(modifiers)] if self.tool == 'terrain'
                                  else TAB_COLORS[self.palette_tabs.currentIndex() % len(TAB_COLORS)])
        self.view.sample_active = self._sampling_height
        self.view.update()

    def _sample_toggled(self, checked):
        self._sampling_height = checked
        self.flatten_height.setStyleSheet('QSpinBox { background: #64531a; border: 2px solid #ffe055; }' if checked else '')
        self._update_cursor()

    def _sample_map_height(self, col, row):
        self.flatten_height.setValue(self.doc.grids['hgt'][row][col] - HGT_MIN)
        self.sample_height.setChecked(False)
        self.view.sample_flash_until = time.monotonic() + 1.2
        self.flatten_height.setStyleSheet('QSpinBox { background: #64531a; border: 2px solid #ffe055; }')
        QTimer.singleShot(1300, self._end_sample_flash)
        self.view.update()

    def _end_sample_flash(self):
        if not self._sampling_height and time.monotonic() >= self.view.sample_flash_until:
            self.flatten_height.setStyleSheet('')
            self.view.update()

    def _rebuild_level_panel(self):
        if self.level_panel is not None:
            self.level_panel.dispose()
            self.level_scroll.takeWidget().deleteLater()
        self.level_panel = LevelInfoPanel(self.doc)
        self._level_values = dict(self.doc.lvl_info)
        self.level_panel.valuesChanged.connect(self._level_changed)
        self.level_panel.generateArtRequested.connect(self._generate_briefing_art)
        self.level_panel.musicChanged.connect(self._play_music)
        self.level_panel.previewReady.connect(self._sky_ready)
        self.level_scroll.setWidget(self.level_panel)

    def _generate_briefing_art(self):
        self._released(-1, -1, Qt.KeyboardModifier.NoModifier)
        lib = self._lib()
        if lib is None:
            return
        from .briefing_dialog import BriefingArtDialog
        directory = self.level_panel.catalog.installation.folder('briefings')
        preferred = os.path.splitext(os.path.basename(self.path))[0] if self.path else 'Untitled'
        dialog = BriefingArtDialog(self.doc, lib, directory, preferred, self._briefing_pool, self)
        if dialog.exec() and dialog.paths:
            mb, db = dialog.paths
            self._level_changed({'mbmap': mb.name, 'dbmap': db.name})
            self._rebuild_level_panel()
            self.statusBar().showMessage(f'Created {mb.name} and {db.name}', 6000)
        if dialog.finished_rendering:
            dialog.deleteLater()

    def _level_changed(self, values):
        if all(self.doc.lvl_info.get(key) == value for key, value in values.items()):
            return
        self._released(-1, -1, Qt.KeyboardModifier.NoModifier)
        self.history.push(self.doc)
        self.doc.lvl_info.update(values)
        self._level_values = dict(self.doc.lvl_info)
        self.dirty = True
        self._refresh_sky()
        self._refresh_music()
        self._refresh_title()

    def _sky_ready(self, path, image):
        if self.doc is not None and path.casefold() == str(self.doc.lvl_info.get('sky', '')).casefold():
            self.view.set_sky_image(image)

    def _sync_side_panels(self):
        self._refresh_script_catalog()
        self.script_edit.blockSignals(True)
        self.script_edit.setPlainText(self.doc.script_content)
        self.script_edit.blockSignals(False)
        self._refresh_squads()
        self._refresh_hosts()
        if self._level_values != self.doc.lvl_info:
            self._rebuild_level_panel()
            self._sky_state = None
            self._refresh_sky()

    def _script_changed(self):
        if self.doc is None:
            return
        text = self.script_edit.toPlainText()
        if text == self.doc.script_content:
            return
        if not self._script_pending:
            self._released(-1, -1, Qt.KeyboardModifier.NoModifier)
            self.history.begin(self.doc)
            self._script_before = self.doc.script_content
            self._script_pending = True
        self.doc.script_content = text
        self.dirty = True
        self._script_timer.start()
        self._refresh_title()

    def _finish_script(self):
        if not self._script_pending:
            return
        self._script_timer.stop()
        self._script_pending = False
        changed = self._script_before != self.doc.script_content
        self.history.commit(self.doc, changed)
        if changed:
            self._refresh_script_catalog()
            self._refresh_squads()
            self._refresh_hosts()
        self._refresh_title()

    def _load_script(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Load custom script', '', 'Scripts (*.scr *.ldf *.cfg *.txt);;All files (*)')
        if path:
            try:
                with open(path, 'rb') as stream:
                    text, _encoding = decode_ldf_bytes(stream.read())
            except (OSError, UnicodeError) as exc:
                QMessageBox.warning(self, 'Script', str(exc))
                return
            self._finish_script()
            self.script_edit.setPlainText(text)
            self._finish_script()

    def _save_script(self):
        self._finish_script()
        path, _ = QFileDialog.getSaveFileName(self, 'Save custom script', '', 'Scripts (*.scr);;Text (*.txt)')
        if path:
            try:
                with open(path, 'w', encoding='utf-8', newline='') as stream:
                    stream.write(self.doc.script_content)
            except OSError as exc:
                QMessageBox.warning(self, 'Script', str(exc))

    def map_reset(self):
        self._released(-1, -1, Qt.KeyboardModifier.NoModifier)
        answer = QMessageBox.question(self, 'Reset map',
            'Create an empty map with the current size and set?\n'
            'This removes buildings, factions, terrain edits, squads, hosts, special objects and custom script.\n'
            'You can undo this reset.', QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.history.push(self.doc)
        self.doc.reset_map()
        self._cancel_operation(clear=False)
        self._after_history()

    def map_fill(self):
        self._show_fill_dialog()

    def fill_selected(self):
        cells = set(self.view.selection)
        if cells:
            self._show_fill_dialog(cells)

    def _show_fill_dialog(self, cells=None):
        self._released(-1, -1, Qt.KeyboardModifier.NoModifier)
        if self._lib() is None:
            return
        dialog = QDialog(self)
        selected = cells is not None
        dialog.setWindowTitle('Fill selected sectors' if selected else 'Fill map with sector')
        layout = QVBoxLayout(dialog)
        choice = QComboBox()
        for i in range(self.sector_list.count()):
            item = self.sector_list.item(i)
            choice.addItem(item.icon(), item.text(), item.data(Qt.ItemDataRole.UserRole))
        choice.setCurrentIndex(choice.findData(self.sel_typ))
        layout.addWidget(choice)
        if selected:
            count = sum(1 for c, r in cells
                        if 1 <= c < self.doc.mw - 1 and 1 <= r < self.doc.mh - 1)
            layout.addWidget(QLabel(
                f'Fills the {count} selected interior sector(s). Buildings in those cells are removed.'))
        else:
            layout.addWidget(QLabel('Fills all interior sectors. The outer border is preserved.'))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() and choice.currentData() is not None:
            self._fill_sectors(choice.currentData(), cells)

    def _fill_sectors(self, typ, cells=None):
        self.history.begin(self.doc)
        if cells is None:
            target_cells = [(c, r) for r in range(1, self.doc.mh - 1)
                            for c in range(1, self.doc.mw - 1)]
        else:
            target_cells = [(c, r) for c, r in cells
                            if 1 <= c < self.doc.mw - 1 and 1 <= r < self.doc.mh - 1]
        changed = paint_cells(self.doc, target_cells, 'type', f'{typ:02x}')
        changed = paint_cells(self.doc, target_cells, 'blg', '00') or changed
        if self.history.commit(self.doc, changed):
            self._after_history()

    def _squad_selected(self, indices):
        self._finish_live()
        self.squad_overlay.selected = set(indices)
        self._update_squad_status(indices)
        self.view.update()

    def _refresh_hosts(self, selected=None):
        lib = self._lib()
        self.host_panel.refresh(self.view.doc or self.doc, lib.vehicles if lib else {},
                                self.view.owner_colors, selected)
        self.tech_panel.refresh(self.doc, lib.vehicles if lib else {}, self.buildings,
                                self.view.owner_colors)

    def _refresh_script_catalog(self):
        lib = self._lib()
        state = (id(lib), self.doc.script_content)
        if lib is None or state == getattr(self, '_script_catalog_state', None):
            return
        self._script_catalog_state = state
        lib.reload_definitions(self.doc.script_content)
        self.buildings = lib.buildings
        self._asset_epoch += 1
        self._resource_icons.clear()
        self._bicon_cache.clear()
        self._palette_set = None
        self._sec_index = {}
        for key, definition in self.buildings.items():
            self._sec_index.setdefault(definition.sec_type, []).append(key)
        self.view.set_library(None)
        self.view.set_library(lib)
        if hasattr(self.view, '_preview_scene'):
            from ..render.gpu_scene import WorldScene
            self.view._preview_scene = WorldScene()
        self._fill_palettes(lib)

    def _host_selected(self, index):
        self._finish_live()
        self.view.selected_host = index
        self.view.update()

    def _select_host(self, index):
        self.palette_tabs.setCurrentIndex(self.host_tab_index)
        self.host_panel.list.setCurrentRow(index)

    def _center_host(self, index):
        hosts = self.host_panel.doc.host_stations
        if not 0 <= index < len(hosts) or not self.doc.cell_is_valid(hosts[index]):
            return
        host = hosts[index]
        if self.view.camera.perspective:
            self.view.reset_camera()
        self.view.camera.center = host_position(host, self.doc, self.view.terrain, self._lib())
        self.view.camera.pan = (0, 0)
        self.view.cameraChanged.emit()
        self.view.update()

    def _host_pov(self, index):
        if not 0 <= index < len(self.doc.host_stations):
            return
        self._cancel_operation(clear=False)
        host = self.doc.host_stations[index]
        lib = self._lib()
        position = host_position(host, self.doc, self.view.terrain, lib)
        visual = lib.vehicles.get(host['veh'])
        offset = visual.viewer if visual is not None else (0, 0, 0)
        eye = tuple(value + delta for value, delta in zip(position, offset))
        angle = math.radians(host['viewangle'])
        self.view.enter_pov_at(eye, (-math.sin(angle), 0, math.cos(angle)))

    def _add_host(self):
        self._released(-1, -1, Qt.KeyboardModifier.NoModifier)
        self._cancel_operation(clear=False)
        try:
            values = self.host_panel.placement_values()
        except ValueError as error:
            self.statusBar().showMessage(str(error), 4000)
            return
        self._draft_host = dict(values, x=self.doc.mw // 2, y=self.doc.mh // 2, _preview=True)
        ensure_host_defaults(self._draft_host)
        self._refresh_squads()
        self._refresh_hosts(len(self.doc.host_stations))

    def _confirm_host(self, cell):
        if cell is None or not (1 <= cell[0] < self.doc.mw - 1 and 1 <= cell[1] < self.doc.mh - 1):
            self.host_panel.status.setText('Place the host inside the map border.')
            self.host_panel.status.show()
            return
        self.history.push(self.doc)
        host = dict(self._draft_host, x=cell[0], y=cell[1])
        host.pop('pos_x', None)
        host.pop('pos_z', None)
        host.pop('_preview', None)
        index = len(self.doc.host_stations)
        self.doc.host_stations.append(host)
        self._draft_host = None
        self.dirty = True
        self._refresh_squads()
        self._refresh_hosts(index)

    def _delete_host(self, index):
        if self._draft_host is not None:
            self._cancel_operation()
            return
        if not 0 <= index < len(self.doc.host_stations):
            return
        self._released(-1, -1, Qt.KeyboardModifier.NoModifier)
        self.history.push(self.doc)
        del self.doc.host_stations[index]
        if self.doc.host_stations and not any(h['owner'] == self.doc.player_owner for h in self.doc.host_stations):
            self.doc.player_owner = self.doc.host_stations[0]['owner']
        self.dirty = True
        self._refresh_squads()
        self._refresh_hosts(min(index, len(self.doc.host_stations) - 1))

    def _host_player(self, index):
        if not 0 <= index < len(self.doc.host_stations):
            return
        owner = self.doc.host_stations[index]['owner']
        if owner == self.doc.player_owner:
            return
        self._released(-1, -1, Qt.KeyboardModifier.NoModifier)
        self.history.push(self.doc)
        self.doc.player_owner = owner
        self.dirty = True
        self._refresh_hosts(index)
        self._refresh_title()

    def _host_changed(self, index, field, value):
        hosts = self.host_panel.doc.host_stations
        if not 0 <= index < len(hosts):
            return
        if self._draft_host is not None and not hosts[index].get('_preview'):
            return
        host = copy.deepcopy(hosts[index])
        if field == 'ai_values':
            host['ai'] = dict(value)
        elif field == 'ai_preset':
            if value == 'Custom':
                host['ai']['preset'] = 'Custom'
            else:
                host['ai'] = make_host_ai(value)
        elif field.startswith('ai.'):
            host['ai'][field[3:]] = value
            host['ai']['preset'] = 'Custom'
        else:
            host[field] = value
            if field in ('x', 'y'):
                host.pop('pos_x' if field == 'x' else 'pos_z', None)
        if host == hosts[index]:
            return
        if not (1 <= host['x'] < self.doc.mw - 1 and 1 <= host['y'] < self.doc.mh - 1):
            self.host_panel.status.setText('The host must stay inside the map border.')
            self.host_panel.status.show()
            return
        if self._draft_host is not None:
            self._draft_host = host
        else:
            if not self._live_pending:
                self._released(-1, -1, Qt.KeyboardModifier.NoModifier)
                self.history.begin(self.doc)
                self._live_pending = True
            self.doc.host_stations[index] = host
            self.dirty = True
            self._live_timer.start()
        self._refresh_squads(fields=False)
        self.host_panel.doc = self.view.doc
        if field in ('owner', 'veh', 'ai_preset', 'ai_values'):
            self._refresh_hosts(index)
        else:
            self.host_panel.update_rows()

    def _tech_changed(self, faction, kind, key, enabled):
        self._released(-1, -1, Qt.KeyboardModifier.NoModifier)
        self.history.push(self.doc)
        if faction not in self.doc.tech_explicit and not any(self.doc.tech[faction].values()):
            lib = self._lib()
            for category, definitions in (('veh', lib.vehicles if lib else {}), ('blg', self.buildings)):
                self.doc.tech[faction][category] = sorted(key for key, definition in definitions.items()
                                                        if faction in definition.enabled_factions)
        active = self.doc.tech[faction][kind]
        if enabled and key not in active:
            active.append(key)
            active.sort()
        elif not enabled and key in active:
            active.remove(key)
        if any(self.doc.tech[faction].values()):
            self.doc.tech_explicit.discard(faction)
        else:
            self.doc.tech_explicit.add(faction)
        self.dirty = True
        self.tech_panel.update_permissions()
        self._refresh_title()

    def _resource_lists(self):
        return (self.squad_panel.list, self.host_panel.list, self.tech_panel.list)

    def _make_resource_icon(self, lib):
        index = self.palette_tabs.currentIndex()
        view = {self.squad_tab_index: self.squad_panel.list, self.host_tab_index: self.host_panel.list,
                self.tech_tab_index: self.tech_panel.list}[index]
        for row in range(view.count()):
            item = view.item(row)
            if item.isHidden() or not view.visualItemRect(item).intersects(view.viewport().rect()):
                continue
            kind, key = item.data(RESOURCE_ROLE)
            cache_key = (id(lib), self._asset_epoch, kind, key)
            if cache_key in self._resource_icons:
                blocked = view.blockSignals(True)
                item.setIcon(self._resource_icons[cache_key])
                view.blockSignals(blocked)
                continue
            try:
                if hasattr(self.view, 'render_icon'):
                    sprite = self.view.render_icon(lib, 0, 1.8,
                                                   vehicle_id=key if kind == 'veh' else None,
                                                   building_id=key if kind == 'blg' else None,
                                                   max_dimension=256)
                    if sprite is not None:
                        self._resource_finished(cache_key, sprite.image)
                        return
                mesh = lib.actor_mesh(key) if kind == 'veh' else lib.building_preview(key)
                data = sprite_input(lib, mesh, -45, 35.26, .23, fast=True, fit=True)
                dimension = max(data[0], data[1])
                if mesh.faces:
                    data = sprite_input(lib, mesh, -45, 35.26, .23 * 250 / max(1, dimension - 6), fast=True, fit=True)
            except Exception:
                self._resource_finished(cache_key, None)
                return
            self._icon_job = _IconJob(data, lib.tables, lib.assets.set_number, key, 1.8, self._asset_epoch)
            self._icon_job.signals.finished.connect(lambda result, cache=cache_key: self._resource_result(cache, result))
            self._icon_pool.start(self._icon_job)
            return

    def _resource_result(self, cache, result):
        self._icon_job = None
        if cache[:2] == (id(self._lib()), self._asset_epoch):
            sprite = result[-1]
            self._resource_finished(cache, sprite.image if sprite is not None else None)
        self._icons.start()

    def _resource_finished(self, cache, image):
        icon = QIcon(QPixmap.fromImage(crop_transparent(image))) if image is not None and not image.isNull() else QIcon()
        self._resource_icons[cache] = icon
        for view in self._resource_lists():
            blocked = view.blockSignals(True)
            for row in range(view.count()):
                item = view.item(row)
                if item.data(RESOURCE_ROLE) == cache[2:]:
                    item.setIcon(icon)
            view.blockSignals(blocked)
        self._icons.start()
        self.view.update()

    def _center_squad(self, index):
        members = [m for m in self.squad_overlay._members(self.view.terrain) if m.squad == index]
        if not members:
            return
        if self.view.camera.perspective:
            self.view.reset_camera()
        self.view.camera.center = tuple(sum(m.position[axis] for m in members)/len(members) for axis in range(3))
        self.view.camera.pan = (0,0)
        self.view.cameraChanged.emit()
        self.view.update()

    def _update_squad_status(self, indices):
        if self.doc is not None:
            squads = [self.squad_panel.doc.squads[i] for i in indices]
            messages = [f'{len(indices)} selected · Changes apply immediately'] if indices else []
            if any(not any(h['owner'] == s['owner'] for h in self.doc.host_stations) for s in squads):
                messages.append('A selected faction has no matching host station; the game needs it to spawn the squad.')
            if any(s['num'] > MAX_PREVIEW_MEMBERS for s in squads):
                messages.append(f'Preview: first {MAX_PREVIEW_MEMBERS} members per squad. The complete count is saved.')
            if self._draft_squads:
                messages.append('Preview only · Left/right click to insert · Esc to cancel')
            self.squad_panel.status.setText('\n'.join(messages))

    def _select_squad(self, index, modifiers=Qt.KeyboardModifier.NoModifier):
        self._released(-1, -1, Qt.KeyboardModifier.NoModifier)
        self.set_tool('squad')
        selected = self.squad_panel.selected_indices()
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            selected ^= {index}
        elif index not in selected:
            selected = {index}
        self.squad_panel.set_selection(selected)

    def _refresh_squads(self, selected=None, fields=True):
        render_doc = self.doc
        if self._draft_squads:
            render_doc = copy.copy(self.doc)
            render_doc.squads = self.doc.squads + self._draft_squads
        self.view.preview_cells = set()
        if self._draft_grid is not None:
            from ..tools.map_clipboard import grid_paste_targets
            targets = grid_paste_targets(self.doc, self._draft_grid, self._draft_grid_cell, interior=True)
            if targets is not None:
                render_doc = copy.copy(render_doc)
                render_doc.grids = {key:[row[:] for row in grid] for key,grid in render_doc.grids.items()}
                for c,r,values in targets:
                    for layer,value in zip(self._draft_grid.layers,values):
                        render_doc.grids[layer][r][c] = value
                    self.view.preview_cells.add((c,r))
        self.view.doc = render_doc
        if self._draft_host is not None:
            render_doc = copy.copy(render_doc)
            render_doc.host_stations = list(self.doc.host_stations)
            render_doc.host_stations.append(self._draft_host)
            self.view.doc = render_doc
        self.view.draft_active = bool(self._draft_squads) or self._draft_grid is not None or self._draft_host is not None
        self.view.scene_changed()
        self.squad_overlay.invalidate()
        if fields:
            self.squad_panel.refresh(render_doc, self._lib().vehicles if self._lib() else {}, selected,
                                     self.view.owner_colors)
        else:
            self.squad_panel.doc = render_doc
            self.squad_panel.update_rows()
            self._update_squad_status(self.squad_panel.selected_indices())
        self._refresh_title()

    def _add_squad(self):
        self._released(-1, -1, Qt.KeyboardModifier.NoModifier)
        self._cancel_operation(clear=False)
        selected = self.squad_panel.selected_indices()
        self._draft_previous_selection = selected.copy()
        col, row = self.view.hover or (self.doc.mw // 2, self.doc.mh // 2)
        vehicles = self._lib().vehicles if self._lib() else {}
        try:
            values = self.squad_panel.placement_values()
        except ValueError as error:
            self.statusBar().showMessage(str(error), 4000)
            return
        self._draft_squads = ([dict(self.doc.squads[i], _preview=True) for i in sorted(selected)] if selected else
            [dict(values, x=col, y=row, _preview=True)])
        first = self._draft_squads[0]
        dx, dz = centered_squad_position(first, col, row)
        old_x, old_z = squad_xz(first)
        for squad in self._draft_squads:
            x, z = squad_xz(squad)
            squad['pos_x'], squad['pos_z'] = x + dx - old_x, z + dz - old_z
            squad['x'], squad['y'] = world_to_grid(squad['pos_x'], squad['pos_z'])
        self._draft_origin = copy.deepcopy(self._draft_squads)
        self.set_tool('squad')
        self._refresh_squads(set(range(len(self.doc.squads), len(self.doc.squads) + len(self._draft_squads))))

    def _move_draft(self, cell):
        if not self._draft_squads or cell is None:
            return
        col, row = cell
        first = self._draft_origin[0]
        x, z = centered_squad_position(first, col, row)
        ox, oz = squad_xz(first)
        for i, original in enumerate(self._draft_origin):
            px, pz = squad_xz(original)
            squad = self._draft_squads[i]
            squad['pos_x'], squad['pos_z'] = px + x - ox, pz + z - oz
            squad['x'], squad['y'] = world_to_grid(squad['pos_x'], squad['pos_z'])
        self._refresh_squads(fields=False)

    def _confirm_add(self, cell):
        if not self._draft_squads or cell is None:
            return
        self._move_draft(cell)
        if any(not (1 <= s['x'] < self.doc.mw - 1 and 1 <= s['y'] < self.doc.mh - 1)
               for s in self._draft_squads):
            self.squad_panel.status.setText('Place the complete selection inside the map border.')
            return
        self._released(-1, -1, Qt.KeyboardModifier.NoModifier)
        self.history.push(self.doc)
        start = len(self.doc.squads)
        for squad in self._draft_squads:
            squad.pop('_preview', None)
            self.doc.squads.append(squad)
        self._draft_squads = []
        self.dirty = True
        self._refresh_squads(set(range(start, len(self.doc.squads))))

    def _delete_squads(self, indices):
        if self._draft_squads:
            self._cancel_operation()
            return
        indices = set(indices)
        if not indices:
            return
        self._released(-1, -1, Qt.KeyboardModifier.NoModifier)
        self.history.push(self.doc)
        self.doc.squads = [s for i, s in enumerate(self.doc.squads) if i not in indices]
        self.dirty = True
        self._refresh_squads(set())

    def _live_squad_changed(self, indices, field, value):
        render_doc = self.squad_panel.doc
        updates = {}
        for index in indices:
            squad = dict(render_doc.squads[index])
            if squad.get(field) == value:
                continue
            squad[field] = value
            if field in ('pos_x', 'pos_z'):
                x, z = squad_xz(squad)
                col, row = world_to_grid(x, z)
                if x <= 0 or z >= 0 or not (1 <= col < self.doc.mw - 1 and 1 <= row < self.doc.mh - 1):
                    self.squad_panel.status.setText('World coordinates must be inside the map border; Z is negative.')
                    return
                squad['x'], squad['y'] = col, row
            updates[index] = squad
        if not updates:
            return
        if self._draft_squads:
            for index, squad in updates.items():
                if index >= len(self.doc.squads):
                    self._draft_squads[index - len(self.doc.squads)] = squad
            self._draft_origin = copy.deepcopy(self._draft_squads)
            self._refresh_squads(fields=False)
            return
        if not self._live_pending:
            self._released(-1, -1, Qt.KeyboardModifier.NoModifier)
            self.history.begin(self.doc)
            self._live_pending = True
        for index, squad in updates.items():
            self.doc.squads[index] = squad
        self.dirty = True
        self._refresh_squads(fields=False)
        self._live_timer.start()

    def _finish_live(self):
        if self._live_pending:
            self._live_timer.stop()
            self._live_pending = False
            self.history.commit(self.doc)
            self._refresh_title()

    def _actor_records(self):
        return self.doc.host_stations if self._drag_kind == 'host' else self.doc.squads

    def _begin_actor_drag(self, kind, cell):
        self._released(-1, -1, Qt.KeyboardModifier.NoModifier)
        self._drag_kind = kind
        self._drag_cell = cell
        records = self._actor_records()
        selected = {self.host_panel.list.currentRow()} if kind == 'host' else self.squad_panel.selected_indices()
        self._drag_original = {i: copy.deepcopy(records[i]) for i in selected if 0 <= i < len(records)}
        self._drag_changed = False
        if self._drag_original and cell is not None:
            self.history.begin(self.doc)

    def _move_actor_drag(self, cell):
        if cell is None or self._drag_cell is None or not self._drag_original:
            return
        dc, dr = cell[0] - self._drag_cell[0], cell[1] - self._drag_cell[1]
        if any(not (1 <= s['x'] + dc < self.doc.mw - 1 and 1 <= s['y'] + dr < self.doc.mh - 1)
               for s in self._drag_original.values()):
            return
        for index, original in self._drag_original.items():
            x, z = squad_xz(original)
            self._actor_records()[index] = dict(original, x=original['x'] + dc, y=original['y'] + dr,
                                           pos_x=x + dc * SECTOR_SIZE, pos_z=z - dr * SECTOR_SIZE)
        self._drag_changed = any(self._actor_records()[i] != s for i, s in self._drag_original.items())
        self._refresh_squads(fields=False)
        if self._drag_kind == 'host':
            self.host_panel.doc = self.view.doc
            self.host_panel.update_rows()

    def _finish_actor_drag(self):
        self.view.unsetCursor()
        if self._drag_original:
            self.history.commit(self.doc, self._drag_changed)
            self.dirty |= self._drag_changed
            self._drag_original = {}
            self._drag_cell = None
            self._refresh_squads()
            self._refresh_hosts()

    def _cancel_operation(self, clear=True):
        if self._draft_host is not None:
            self._draft_host = None
            self._refresh_squads()
            self._refresh_hosts()
            self.view.unsetCursor()
            return
        if self._draft_grid is not None:
            self._draft_grid = None
            self._draft_grid_cell = None
            self._refresh_squads()
            self.view.unsetCursor()
            return
        if self._drag_original:
            self.view.unsetCursor()
            for index, original in self._drag_original.items():
                self._actor_records()[index] = original
            self.history.commit(self.doc, False)
            self._drag_original = {}
            self._drag_cell = None
            self._refresh_squads()
            self._refresh_hosts()
        elif self._draft_squads:
            self._draft_squads = []
            self._refresh_squads(self._draft_previous_selection)
        elif clear:
            self._clear_selection()

    def _clear_selection(self):
        self._released(-1, -1, Qt.KeyboardModifier.NoModifier)
        self.view.selection.clear()
        self.squad_panel.set_selection(set())
        self.host_panel.deselect()
        self.view.update()

    def _sweep_cell(self, cell):
        if cell is not None and not self.view.draft_active:
            self.view.selection.add(cell)
            self.view.update()

    def _copy_elements(self):
        index = self.palette_tabs.currentIndex()
        if index == self.script_tab_index:
            text = self.script_edit.textCursor().selectedText().replace('\u2029','\n') or self.script_edit.toPlainText()
            QApplication.clipboard().setText(text)
            return
        if index == self.level_tab_index:
            self._clipboard = ('info',copy.deepcopy(self.doc.lvl_info))
        elif index == self.squad_tab_index:
            squads = [copy.deepcopy(self.squad_panel.doc.squads[i]) for i in sorted(self.squad_panel.selected_indices())]
            if not squads:
                return
            self._clipboard = ('squad',squads)
        else:
            from ..tools.map_clipboard import copy_grid_cells
            tool = {0:'sector',1:'building',2:'owner',3:'terrain'}.get(index)
            layers = {'sector':('type','blg'),'building':('type','blg'),'owner':('own',),'terrain':('hgt',)}[tool]
            cells = self.view.selection or ({self.view.hover} if self.view.hover is not None else set())
            clipboard = copy_grid_cells(self.doc,cells,tool,layers)
            if clipboard is None:
                return
            self._clipboard = ('grid',clipboard)
        if self._clipboard[0] in ('grid','squad'):
            self._paste_elements()
        else:
            self.statusBar().showMessage('Copied level settings · Paste applies them',4000)

    def _paste_elements(self):
        if self.palette_tabs.currentIndex() == self.script_tab_index:
            self.script_edit.paste()
            return
        if self._clipboard is None:
            return
        if self.view.camera.perspective:
            self.view.reset_camera()
        kind,payload = self._clipboard
        self._released(-1,-1,Qt.KeyboardModifier.NoModifier)
        self._cancel_operation(clear=False)
        if kind == 'info':
            self._level_changed(copy.deepcopy(payload))
            self._rebuild_level_panel()
            self.palette_tabs.setCurrentIndex(self.level_tab_index)
            return
        if kind == 'squad':
            self.set_tool('squad')
            self._draft_previous_selection = self.squad_panel.selected_indices().copy()
            self._draft_squads = [dict(s,_preview=True) for s in copy.deepcopy(payload)]
            self._draft_origin = copy.deepcopy(self._draft_squads)
            self._refresh_squads(set(range(len(self.doc.squads),len(self.doc.squads)+len(self._draft_squads))))
            self._move_draft(self.view.hover or (self.doc.mw//2,self.doc.mh//2))
            self._refresh_squads(set(range(len(self.doc.squads),len(self.doc.squads)+len(self._draft_squads))))
        else:
            self.set_tool(payload.tool)
            self._draft_grid = payload
            self._draft_grid_cell = self.view.hover or (self.doc.mw//2,self.doc.mh//2)
            self._refresh_squads(fields=False)
        self.view.setCursor(Qt.CursorShape.CrossCursor)
        self.statusBar().showMessage('Paste preview · Left/right click: place · Esc: cancel',6000)

    def _confirm_placement(self, cell):
        if self._draft_host is not None:
            return self._confirm_host(cell)
        if self._draft_grid is None:
            return self._confirm_add(cell)
        from ..tools.map_clipboard import grid_paste_targets
        targets = grid_paste_targets(self.doc,self._draft_grid,cell,interior=True)
        if targets is None:
            self.statusBar().showMessage('Place the entire selection inside the map border',4000)
            return
        self.history.begin(self.doc)
        changed = False
        for c,r,values in targets:
            for layer,value in zip(self._draft_grid.layers,values):
                changed |= paint_cells(self.doc,[(c,r)],layer,value)
        if 'hgt' in self._draft_grid.layers:
            changed |= bool(self.doc.normalize_border_heights())
        self.history.commit(self.doc,changed)
        self.dirty |= changed
        self._draft_grid = None
        self._draft_grid_cell = None
        self.view.selection = {(c,r) for c,r,_ in targets}
        self._after_history()

    def _squad_pov(self, index, position=None):
        self._cancel_operation(clear=False)
        members = [m for m in self.squad_overlay._members(self.view.terrain) if m.squad == index]
        member = min(members,key=lambda m: sum((a-b)**2 for a,b in zip(
            self.view.camera.world_to_screen(m.position)[0],(position.x(),position.y())))) if members and position is not None else next(iter(members),None)
        if member is not None:
            self.view.enter_pov(member)

    def _gun_pov(self, cell, index):
        col,row = cell
        definition = self.buildings.get(int(self.doc.grids['blg'][row][col],16))
        if definition is None or not 0 <= index < len(definition.guns):
            return
        from ..render.sector_mesh import gun_rotation
        mount = definition.guns[index]
        actor = self.view.lib.vehicle_mesh(mount.vehicle)
        rotation = gun_rotation(mount.direction)
        offset = rotation @ (0,actor.bounds[1]-30,0)
        origin = self.view.terrain.cell_center(col,row)
        eye = tuple(origin[axis]+mount.pos[axis]+offset[axis] for axis in range(3))
        self._cancel_operation(clear=False)
        self.view.enter_pov_at(eye,tuple(rotation[:,2]))

    def _panel_context_menu(self, widget, position):
        menu = QMenu(self)
        menu.addAction(self.undo_action)
        menu.addAction(self.redo_action)
        menu.addSeparator()
        menu.addAction(self.copy_action)
        menu.addAction(self.paste_action)
        if widget is self.script_edit:
            menu.addAction('Select all',self.script_edit.selectAll)
        if widget is self.squad_panel.list:
            item = widget.itemAt(position)
            if item is not None and not item.isSelected():
                self.squad_panel.set_selection({widget.row(item)})
            menu.addAction('Add preview',self._add_squad)
            menu.addAction('Delete selected squads',lambda: self._delete_squads(self.squad_panel.selected_indices()))
            index = widget.currentRow()
            action = menu.addAction('Squad POV',lambda: self._squad_pov(index))
            action.setEnabled(0 <= index < len(self.doc.squads))
            action = menu.addAction('Squad Focus', lambda: self._center_squad(index))
            action.setEnabled(0 <= index < len(self.doc.squads))
        if widget is self.host_panel.list:
            item = widget.itemAt(position)
            if item is not None:
                widget.setCurrentItem(item)
            index = widget.currentRow()
            for text, slot in (('Host Station POV', self._host_pov), ('Host Station Focus', self._center_host)):
                action = menu.addAction(text, lambda checked=False, fn=slot: fn(index))
                action.setEnabled(0 <= index < len(self.doc.host_stations))
        menu.popup(widget.mapToGlobal(position))
        self._context_menu = menu

    def _select_cell(self, cell, modifiers):
        self._released(-1, -1, Qt.KeyboardModifier.NoModifier)
        additive = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        if not additive:
            self.view.selection.clear()
            self.squad_panel.set_selection(set())
            self.host_panel.deselect()
        if cell is not None:
            if additive:
                self.view.selection ^= {cell}
            else:
                self.view.selection.add(cell)
                if self.tool in ('sector', 'building', 'owner'):
                    self._apply_selection()
        self.view.update()

    def _apply_selection(self, field=None, value=None):
        cells = set(self.view.selection)
        if not cells:
            return
        self._released(-1, -1, Qt.KeyboardModifier.NoModifier)
        self.history.begin(self.doc)
        if field is not None:
            changed = bool(paint_cells(self.doc, cells, field, value))
            if field == 'hgt':
                changed |= bool(self.doc.normalize_border_heights())
        else:
            changed = False
            previous_tool = self.tool
            if self.tool == 'select':
                self.tool = {0: 'sector', 1: 'building', 2: 'owner', 3: 'terrain'}.get(
                    self.palette_tabs.currentIndex(), 'select')
            for col, row in cells:
                self._stroke_changed = False
                self._apply(col, row)
                changed |= self._stroke_changed
            self.tool = previous_tool
        if self.history.commit(self.doc, changed):
            self._after_history()

    def _map_context_menu(self, position, global_position):
        if self.view.camera.perspective:
            menu = QMenu(self)
            menu.addAction('Exit POV',self.view.reset_camera)
            menu.popup(global_position)
            self._context_menu = menu
            return
        self._released(-1, -1, Qt.KeyboardModifier.NoModifier)
        squad = self.view.pick_squad(position.x(), position.y())
        host = self.view.pick_host(position.x(), position.y())
        if host is not None:
            self._select_host(host)
        if squad is not None:
            if squad not in self.squad_panel.selected_indices():
                self._select_squad(squad)
            else:
                self.set_tool('squad')
        cell = self.view.ground_cell(position.x(), position.y())
        if cell is not None and cell not in self.view.selection and self.tool != 'squad':
            self.view.selection = {cell}
        menu = QMenu(self)
        menu.addAction(self.undo_action)
        menu.addAction(self.redo_action)
        menu.addAction(self.copy_action)
        menu.addAction(self.paste_action)
        menu.addSeparator()
        context_tool = ({0: 'sector', 1: 'building', 2: 'owner', 3: 'terrain'}.get(
            self.palette_tabs.currentIndex(), self.tool) if self.tool == 'select' else self.tool)
        if self.palette_tabs.currentIndex() == self.host_tab_index:
            index = host if host is not None else self.host_panel.list.currentRow()
            for text, slot in (('Host Station POV', self._host_pov), ('Host Station Focus', self._center_host)):
                action = menu.addAction(text, lambda checked=False, fn=slot: fn(index))
                action.setEnabled(0 <= index < len(self.doc.host_stations))
        elif context_tool == 'squad':
            index = squad if squad is not None else min(self.squad_panel.selected_indices(),default=-1)
            action = menu.addAction('Squad POV',lambda: self._squad_pov(index,position))
            action.setEnabled(0 <= index < len(self.doc.squads))
            action = menu.addAction('Squad Focus', lambda: self._center_squad(index))
            action.setEnabled(0 <= index < len(self.doc.squads))
            menu.addAction('Add preview', self._add_squad)
            action = menu.addAction('Delete selected squads', lambda: self._delete_squads(self.squad_panel.selected_indices()))
            action.setEnabled(bool(self.squad_panel.selected_indices()))
            menu.addAction('Select all squads', lambda: self.squad_panel.set_selection(set(range(len(self.doc.squads)))))
        elif self.palette_tabs.currentIndex() == self.script_tab_index:
            menu.addAction('Load script…', self._load_script)
            menu.addAction('Save script…', self._save_script)
            menu.addAction('Select script text', self.script_edit.selectAll)
        elif context_tool in ('sector', 'building', 'owner'):
            action = menu.addAction('Apply', lambda: self._apply_selection())
            action.setEnabled(bool(self.view.selection))
            if context_tool == 'sector':
                selected_fill = menu.addAction('Fill selected sectors…', self.fill_selected)
                selected_fill.setEnabled(bool(self.view.selection))
                if cell is not None:
                    menu.addAction('Pick this sector', lambda: self.sector_list.setCurrentItem(
                        self._icon_items.get(int(self.doc.grids['type'][cell[1]][cell[0]], 16))))
            if context_tool == 'building':
                menu.addAction('Remove selected buildings', lambda: self._apply_selection('blg', '00'))
            menu.addAction('Fill map…', self.map_fill)
        elif context_tool == 'terrain':
            for i, name in enumerate(('Raise', 'Lower', 'Flatten', 'Smooth')):
                menu.addAction(name, lambda checked=False, index=i: self._activate_terrain(index))
            if cell is not None:
                menu.addAction('Sample this height', lambda: self._sample_map_height(*cell))
        if cell is not None:
            definition = self.buildings.get(int(self.doc.grids['blg'][cell[1]][cell[0]],16))
            if definition is not None and definition.guns:
                menu.addSeparator()
                if len(definition.guns)==1:
                    menu.addAction('Gun POV',lambda: self._gun_pov(cell,0))
                else:
                    guns = menu.addMenu('Gun POV')
                    for index,mount in enumerate(definition.guns):
                        name = self.view.lib.vehicles.get(mount.vehicle)
                        label = f'Gun {index+1} · {name.name if name and name.name else mount.vehicle}'
                        guns.addAction(label,lambda checked=False,i=index: self._gun_pov(cell,i))
        menu.addSeparator()
        menu.addAction('Selection tool', lambda: self.set_tool('select'))
        menu.addAction('Clear selection', self._clear_selection)
        menu.addAction('Reset map…', self.map_reset)
        menu.addAction(self.reset_camera_action)
        menu.popup(global_position)
        self._context_menu = menu

    def _activate_terrain(self, index):
        self.set_tool('terrain')
        self.mode_combo.setCurrentIndex(index)

    def _fill_palettes(self, lib: SectorMeshLibrary):
        if self._palette_set == lib.assets.set_number:
            return
        self._palette_set = lib.assets.set_number
        self.sector_list.clear()
        self._icon_queue.clear()
        self._icon_items.clear()
        for typ, sector in sorted(lib.assets.sdf.sectors.items()):
            item = QListWidgetItem(f"{typ:02x} {sector.comment}")
            item.setData(Qt.ItemDataRole.UserRole, typ)
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
            self.sector_list.addItem(item)
            self._icon_items[typ] = item
            key = (lib.assets.set_number, typ, self._icon_scale)
            if key in self._icon_cache:
                item.setIcon(self._icon_cache[key])
            else:
                self._icon_queue.append((typ, item))
        self._layout_sector_icons()
        self._fill_buildings(lib)
        self.sector_list.scrollToTop()
        self._icons.start()

    def _make_icon(self):
        lib = self._lib()
        if lib is None or self._icon_job is not None:
            return
        if not self.palette_dock.isVisible():
            return
        if self.view._job is not None or self.view._interacting or self._stroke_active:
            self._icons.start(80)
            return
        tab = self.palette_tabs.currentIndex()
        if tab in (self.squad_tab_index, self.host_tab_index, self.tech_tab_index):
            self._make_resource_icon(lib)
            return
        if tab not in (0, 1):
            return
        queue = self._icon_queue if tab == 0 else self._bicon_queue
        deadline = time.monotonic() + PALETTE_BATCH_SECONDS
        # Bound each group so camera input and widget repainting get a turn.
        for _ in range(8):
            before = len(queue)
            if not before:
                break
            if tab == 0:
                self._make_sector_icon(lib)
            else:
                self._make_building_icon(lib)
            if self._icon_job is not None:
                return
            if len(queue) == before:
                self._icons.stop()
                return
            if time.monotonic() >= deadline:
                break
        if queue:
            self._icons.start()
        else:
            self._icons.stop()

    def _make_sector_icon(self, lib):
        visible = self.sector_list.viewport().rect()
        candidates = [i for i, (_, item) in enumerate(self._icon_queue) if not item.isHidden()]
        if not candidates:
            return
        index = next((i for i in candidates
                      if self.sector_list.visualItemRect(self._icon_queue[i][1]).intersects(visible)), candidates[0])
        typ, item = self._icon_queue.pop(index)
        try:
            if hasattr(self.view, 'render_icon'):
                pixels = round(max(self.sector_list.iconSize().width(),
                                   self.sector_list.iconSize().height()) * self.devicePixelRatioF()) + 4
                sprite = self.view.render_icon(lib, typ, self._icon_scale, max_dimension=pixels)
                if sprite is not None:
                    self._icon_finished((lib.assets.set_number, typ, self._icon_scale,
                                         self._asset_epoch, sprite))
                    return
            data = sprite_input(lib, lib.mesh(typ), -45.0, 35.26,
                                .13 * self._icon_scale, fast=True)
        except Exception:
            self._icon_finished((lib.assets.set_number, typ, self._icon_scale,
                                 self._asset_epoch, None))
            return
        self._icon_job = _IconJob(data, lib.tables, lib.assets.set_number, typ,
                                  self._icon_scale, self._asset_epoch)
        self._icon_job.signals.finished.connect(self._icon_finished)
        self._icon_pool.start(self._icon_job)

    def _icon_finished(self, result):
        number, typ, scale, epoch, sprite = result
        self._icon_job = None
        if scale != self._icon_scale or epoch != self._asset_epoch:
            self._icons.start()
            return
        if sprite is not None:
            icon = QIcon(QPixmap.fromImage(crop_transparent(sprite.image)))
        else:
            icon = QIcon()
        self._icon_cache[(number, typ, scale)] = icon
        if number == self._palette_set and typ in self._icon_items:
            self._icon_items[typ].setIcon(icon)
        if self._icon_queue or self._bicon_queue:
            self._icons.start()

    def _refresh_buildings(self):
        self._bicon_cache.clear()
        install = bootstrap.installation()
        if install is None:
            try:
                install = GameInstallation.suggest(bootstrap.game_data_dir())
            except RuntimeError:
                install = None
        scripts = install.folder("scripts") if install is not None else None
        try:
            self.buildings = load_building_files(scripts) if scripts is not None else {}
        except Exception:
            self.buildings = {}
        self._sec_index = {}
        for bid, definition in self.buildings.items():
            if definition.sec_type:
                self._sec_index.setdefault(definition.sec_type, []).append(bid)
        self._palette_set = None
        lib = self._lib()
        if lib is not None:
            self._fill_palettes(lib)

    def _fill_buildings(self, lib: SectorMeshLibrary):
        self.building_list.clear()
        self._bicon_queue.clear()
        self._bicon_items.clear()
        self._bicon_rows.clear()
        for bid in sorted(self.buildings):
            definition = self.buildings[bid]
            if not self.show_special_buildings.isChecked() and not (definition.has_power or definition.is_radar or definition.has_guns):
                continue
            title = f"{bid:02d} {definition.name}"
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, bid)
            preview = QLabel()
            size = BUILDING_PREVIEW_PIXELS[self._bicon_level]
            preview.setFixedSize(size, size)
            preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
            preview.setStyleSheet("background:#171717; border:1px solid #454545;")
            name_label = QLabel(title)
            name_label.setWordWrap(True)
            name_label.setStyleSheet("font-weight:600;")
            detail = QLabel(self._building_detail(definition))
            detail.setWordWrap(True)
            detail.setStyleSheet("color:#b9b9b9; font-size:11px;")
            badges = QHBoxLayout()
            badges.setContentsMargins(0, 0, 0, 0)
            badges.setSpacing(4)
            badges.addWidget(CapabilityPreview(definition))
            badges.addStretch()
            text_layout = QVBoxLayout()
            text_layout.setContentsMargins(0, 0, 0, 0)
            text_layout.setSpacing(1)
            text_layout.addWidget(name_label)
            text_layout.addWidget(detail)
            text_layout.addLayout(badges)
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(6, 4, 6, 4)
            row_layout.setSpacing(8)
            row_layout.addWidget(preview)
            row_layout.addLayout(text_layout)
            item.setSizeHint(QSize(0, max(64, size + 16)))
            item.setToolTip(self._building_tooltip(definition))
            self.building_list.addItem(item)
            self.building_list.setItemWidget(item, row)
            self._bicon_items[bid] = item
            self._bicon_rows[bid] = preview
            cached = self._bicon_cache.get((lib.assets.set_number, bid, self.devicePixelRatioF()))
            if cached is not None:
                self._bicon_finished(bid, cached)
            else:
                self._bicon_queue.append(bid)
        self._layout_building_rows()
        self._filter(self.building_list, self.building_filter.text())
        if self.buildings and self.sel_building not in self.buildings:
            self.sel_building = next(iter(sorted(self.buildings)))
        self._icons.start()

    @staticmethod
    def _building_detail(definition) -> str:
        parts = [f"SEC {definition.sec_type:02x}"]
        if definition.power:
            parts.append(f"PWR {definition.power}")
        if definition.energy:
            energy = definition.energy
            parts.append(f"HP {energy // 1000}k" if energy >= 1000 else f"HP {energy}")
        if definition.guns:
            parts.append(f"GUNS x{len(definition.guns)}")
        if definition.is_radar:
            parts.append("RADAR")
        if definition.production_cost:
            parts.append(f"COST {definition.production_cost}")
        return " · ".join(parts)

    @staticmethod
    def _building_tooltip(definition) -> str:
        lines = [f"{definition.id:02d} {definition.name}",
                 f"model={definition.model} sec_type={definition.sec_type} ({definition.sec_type:02x})",
                 f"power={definition.power} energy={definition.energy} cost={definition.production_cost}",
                 f"source={definition.source or 'installation scripts'}"]
        for gun in definition.guns:
            lines.append(f"gun vehicle={gun.vehicle} pos={gun.pos} dir={gun.direction}")
        if definition.debug_note:
            lines.append(definition.debug_note)
        return "\n".join(lines)

    def _layout_building_rows(self):
        size = BUILDING_PREVIEW_PIXELS[self._bicon_level]
        for bid, preview in self._bicon_rows.items():
            preview.setFixedSize(size, size)
            item = self._bicon_items.get(bid)
            if item is not None:
                item.setSizeHint(QSize(0, max(64, size + 16)))

    def _building_zoom_step(self, direction: int):
        level = max(0, min(2, self._bicon_level + direction))
        if level == self._bicon_level:
            return
        self._bicon_level = level
        self.building_zoom_label.setText(f"{level + 1}/3")
        self.building_zoom_out.setEnabled(level > 0)
        self.building_zoom_in.setEnabled(level < 2)
        self._layout_building_rows()
        lib = self._lib()
        if lib is not None:
            for bid in self._bicon_rows:
                image = self._bicon_cache.get((lib.assets.set_number, bid, self.devicePixelRatioF()))
                if image is not None:
                    self._bicon_finished(bid, image)
            self._icons.start()

    def _make_building_icon(self, lib: SectorMeshLibrary):
        while self._bicon_queue:
            candidates = [i for i, bid in enumerate(self._bicon_queue)
                          if bid in self._bicon_items and not self._bicon_items[bid].isHidden()]
            if not candidates:
                return
            visible = self.building_list.viewport().rect()
            index = next((i for i in candidates if self.building_list.visualItemRect(
                self._bicon_items[self._bicon_queue[i]]).intersects(visible)), candidates[0])
            bid = self._bicon_queue.pop(index)
            item = self._bicon_items.get(bid)
            if item is None:
                continue
            definition = self.buildings.get(bid)
            if definition is None:
                continue
            sec_type = definition.sec_type
            try:
                if hasattr(self.view, 'render_icon'):
                    pixels = round(max(BUILDING_PREVIEW_PIXELS) * self.devicePixelRatioF()) + 4
                    sprite = self.view.render_icon(lib, sec_type, max(SECTOR_PREVIEW_SCALES),
                                                   building_id=bid, max_dimension=pixels)
                    if sprite is not None:
                        self._bicon_finished(bid, sprite.image)
                        self._icons.start()
                        return
                data = sprite_input(lib, lib.building_preview(bid), -45.0, 35.26,
                                    .13 * max(SECTOR_PREVIEW_SCALES), fast=True, fit=True)
            except Exception:
                self._bicon_finished(bid, None)
                self._icons.start()
                return
            self._icon_job = _IconJob(data, lib.tables, lib.assets.set_number,
                                      sec_type, self._icon_scale, self._asset_epoch)
            self._icon_job.signals.finished.connect(
                lambda result, _bid=bid: self._bicon_from_sector(_bid, result))
            self._icon_pool.start(self._icon_job)
            return

    def _bicon_from_sector(self, bid: int, result):
        number, typ, scale, epoch, sprite = result
        self._icon_job = None
        if number != self._palette_set or epoch != self._asset_epoch:
            self._icons.start()
            return
        self._bicon_finished(bid, sprite.image if sprite is not None else None)
        if self._bicon_queue:
            self._icons.start()

    def _bicon_finished(self, bid: int, image):
        preview = self._bicon_rows.get(bid)
        if preview is None:
            return
        if image is None or image.isNull():
            preview.setText("—")
            preview.setPixmap(QPixmap())
            return
        cropped = crop_transparent(image)
        self._bicon_cache[(self._palette_set, bid, self.devicePixelRatioF())] = image
        size = BUILDING_PREVIEW_PIXELS[self._bicon_level]
        ratio = self.devicePixelRatioF()
        pixmap = QPixmap.fromImage(cropped).scaled(
            QSize(round((size - 4) * ratio), round((size - 4) * ratio)), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        pixmap.setDevicePixelRatio(ratio)
        preview.setPixmap(pixmap)

    def _building_selected(self, item, _prev=None):
        if item:
            self.sel_building = item.data(Qt.ItemDataRole.UserRole)
            if self.tool_actions["building"].isChecked() is False:
                self.tool_actions["building"].trigger()

    def _lib(self) -> SectorMeshLibrary | None:
        return self.libs.get(self.doc.set_number) if self.doc else None

    def _ensure_lib(self):
        number = self.doc.set_number
        if number not in self.libs:
            try:
                self.libs[number] = SectorMeshLibrary(SetAssets(number).load())
            except Exception as exc:
                QMessageBox.warning(self, "Set unavailable",
                                    f"Set {number}: {exc}\nUnable to display this set.")
                self.view.set_library(None)
                return
        lib = self.libs[number]
        self.view.set_library(lib)
        if not self.buildings:
            self._refresh_buildings()
            return
        self._fill_palettes(lib)

    def file_game_folders(self):
        dialog = GameInstallationDialog(bootstrap.installation() or load_remembered(), self)
        if not dialog.exec():
            return
        new = dialog.installation()
        self.view.set_library(None)
        bootstrap.set_installation(new)
        self._asset_epoch += 1
        self._resource_icons.clear()
        self._sky_state = None
        self.libs.clear()
        self._icon_cache.clear()
        self._bicon_cache.clear()
        self._bicon_queue.clear()
        self._palette_set = None
        self.buildings = {}
        self.view.owner_colors = load_owner_colors()
        self._refresh_owner_palette()
        self._ensure_lib()
        self._rebuild_level_panel()
        self._sync_side_panels()
        self._refresh_sky()
        self._refresh_music()
        self.building_overlay.update()
        self.view.scene_changed()

    def _refresh_owner_palette(self):
        for row in range(self.owner_list.count()):
            item = self.owner_list.item(row)
            owner = item.data(Qt.ItemDataRole.UserRole)
            pix = QPixmap(36, 36)
            pix.fill(QColor(*self.view.owner_colors[owner]) if owner else QColor(145, 145, 145))
            painter = QPainter(pix)
            painter.setPen(QPen(QColor(175, 175, 175), 1))
            painter.drawRect(0, 0, 35, 35)
            painter.end()
            item.setIcon(QIcon(pix))

    def _new_doc(self, doc: LdfDocument, path: str | None):
        self._draft_grid = None
        self._draft_grid_cell = None
        self._finish_script()
        self._released(-1, -1, Qt.KeyboardModifier.NoModifier)
        self.doc, self.path, self.dirty = doc, path, False
        self.history.clear()
        self._draft_squads = []
        self._draft_host = None
        self._ensure_lib()
        self._rebuild_level_panel()
        self._sync_side_panels()
        self._refresh_sky()
        self._refresh_music()
        self.view.set_document(doc)
        self.building_overlay.update()
        self._refresh_title()

    def _refresh_title(self):
        name = os.path.basename(self.path) if self.path else "untitled"
        self.setWindowTitle(f"{'*' if self.dirty else ''}{name} - OpenNeoUA Studio · Map Editor")
        self.undo_action.setEnabled(self.history.can_undo or self._script_pending or self._live_pending)
        self.redo_action.setEnabled(self.history.can_redo)

    def _confirm_discard(self) -> bool:
        self._released(-1, -1, Qt.KeyboardModifier.NoModifier)
        if not self.dirty:
            return True
        answer = QMessageBox.question(
            self, "Unsaved changes", "Discard changes?")
        return answer == QMessageBox.StandardButton.Yes

    def closeEvent(self, event):
        if self._confirm_discard():
            self.music_preview.stop()
            self._icons.stop()
            self._icon_queue.clear()
            self._bicon_queue.clear()
            self._icon_pool.waitForDone(10000)
            self._briefing_pool.waitForDone()
            self.view.stop_rendering()
            if self.level_panel is not None:
                self.level_panel.dispose()
            event.accept()
        else:
            event.ignore()

    def file_new(self):
        if not self._confirm_discard():
            return
        dialog = NewMapDialog(self)
        if dialog.exec():
            w, h, set_number = dialog.values()
            doc = LdfDocument(mw=w, mh=h, set_number=set_number)
            self._new_doc(doc, None)

    def _level_dir(self) -> str:
        install = bootstrap.installation()
        if install is not None:
            levels = install.folder("levels")
            single = levels / "Single"
            if single.is_dir():
                return str(single)
            if levels.is_dir():
                return str(levels)
        return ""

    def file_open(self):
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open map", self._level_dir(),
                                             "Levels (*.ldf *.LDF);;Text maps (*.txt);;All files (*)",
                                             options=QFileDialog.Option.DontUseNativeDialog)
        if not path:
            return
        try:
            doc = load_ldf(path)
        except Exception as exc:
            QMessageBox.critical(self, "Error", f"Unable to open: {exc}")
            return
        self._new_doc(doc, path)

    def file_save(self):
        self._released(-1, -1, Qt.KeyboardModifier.NoModifier)
        if not self.path:
            return self.file_save_as()
        self._save_to(self.path)

    def _save_to(self, path):
        try:
            save_ldf(self.doc, path)
        except Exception as exc:
            QMessageBox.critical(self, "Error", f"Save failed: {exc}")
            return
        self.path = path
        self.dirty = False
        self._refresh_title()
        self.statusBar().showMessage(f"Saved {self.path}", 4000)

    def file_save_as(self):
        self._released(-1, -1, Qt.KeyboardModifier.NoModifier)
        path, selected_filter = QFileDialog.getSaveFileName(self, "Save map", self.path or self._level_dir(),
                                             "Levels (*.LDF);;Text maps (*.txt)",
                                             options=QFileDialog.Option.DontUseNativeDialog)
        if path:
            if not os.path.splitext(path)[1]:
                path += '.txt' if selected_filter == 'Text maps (*.txt)' else '.LDF'
            self._save_to(path)

    def undo(self):
        self._cancel_operation(clear=False)
        self._released(-1, -1, Qt.KeyboardModifier.NoModifier)
        if self.history.undo(self.doc):
            self._after_history()

    def redo(self):
        self._cancel_operation(clear=False)
        self._released(-1, -1, Qt.KeyboardModifier.NoModifier)
        if self.history.redo(self.doc):
            self._after_history()

    def _after_history(self):
        self.dirty = True
        self._draft_squads = []
        if (self.doc.mw, self.doc.mh) != (self.view.terrain.width, self.view.terrain.height):
            self.view.set_document(self.doc)
        else:
            self.view.terrain_changed()
            self.view.scene_changed()
            self.view.owners_changed()
        self.building_overlay.update()
        cell = self.view.hover or next(iter(self.view.selection), None)
        if cell is not None:
            self._hovered(*cell)
        self._refresh_sky()
        self._refresh_music()
        self._refresh_title()
        self._sync_side_panels()

    def map_resize(self):
        if not self.doc:
            return
        self._released(-1, -1, Qt.KeyboardModifier.NoModifier)
        result = ResizeDialog.ask(self.doc, self)
        if result is None:
            return
        w, h, remove = result
        self.history.push(self.doc)
        try:
            self.doc.resize(w, h, remove_out_of_bounds=remove)
        except ValueError as exc:
            self.history.undo(self.doc)
            QMessageBox.warning(self, "Resize", str(exc))
            return
        self.dirty = True
        self.view.set_document(self.doc)
        self._draft_squads = []
        self._sync_side_panels()
        self._refresh_title()

    def map_level_info(self):
        self._released(-1, -1, Qt.KeyboardModifier.NoModifier)
        self.palette_tabs.setCurrentIndex(self.level_tab_index)

    def _play_music(self, value: str):
        install = bootstrap.installation() or GameInstallation.suggest(bootstrap.game_data_dir())
        self.music_preview.set_track(install, value)

    def _refresh_music(self):
        if self.doc is not None:
            self._play_music(str(self.doc.lvl_info.get("music", "None")))

    def _refresh_sky(self):
        if self.doc is None:
            return
        install = bootstrap.installation()
        if install is None:
            install = GameInstallation.suggest(bootstrap.game_data_dir())
        state = (install.data, tuple(install.folders.items()), self.doc.set_number,
                 str(self.doc.lvl_info.get("sky", "")))
        if state == self._sky_state:
            return
        self._sky_state = state
        self.view.set_sky_image(None)
        if self.level_panel is not None:
            self.level_panel.request_selected_sky()

    def set_tool(self, key: str):
        if self._draft_host is not None and self.palette_tabs.currentIndex() != self.host_tab_index:
            self._cancel_operation(clear=False)
        if self._stroke_active:
            self._released(-1, -1, Qt.KeyboardModifier.NoModifier)
        self.tool = key
        if key != 'squad':
            self._cancel_operation(clear=False)
        self.view.active_tool = 'host' if self.palette_tabs.currentIndex() == self.host_tab_index else key
        self.tool_actions[key].setChecked(True)
        if key != "select":
            self.palette_tabs.setCurrentIndex({"sector": 0, "building": 1, "owner": 2, "terrain": 3,
                                               "squad": self.squad_tab_index}[key])
        self.view.brush_cells = set()
        self._update_cursor()
        self.view.update()

    def _palette_changed(self, index):
        self._finish_script()
        self.set_tool({0: "sector", 1: "building", 2: "owner", 3: "terrain",
                       self.squad_tab_index: "squad"}.get(index, "select"))
        if index == 0 and self._icon_queue:
            self._icons.start()
        if index == 1 and self._bicon_queue:
            self._icons.start()
        if index in (self.squad_tab_index, self.host_tab_index, self.tech_tab_index):
            self._icons.start()

    def _sector_selected(self, item, _prev=None):
        if item:
            self.sel_typ = item.data(Qt.ItemDataRole.UserRole)
            self.tool_actions["sector"].trigger()

    def _owner_selected(self, item, _prev=None):
        if item:
            self.sel_owner = item.data(Qt.ItemDataRole.UserRole)
            self.tool_actions["owner"].trigger()

    def _hovered(self, col, row):
        if col < 0 or not self.doc:
            self.info.setText("")
            self.view.brush_cells = set()
            self.view.update()
            return
        g = self.doc.grids
        self._update_cursor(QApplication.keyboardModifiers())
        if self._draft_squads:
            self._move_draft((col, row))
        if self._draft_host is not None and 1 <= col < self.doc.mw - 1 and 1 <= row < self.doc.mh - 1:
            self._draft_host.update(x=col, y=row)
            self._draft_host.pop('pos_x', None)
            self._draft_host.pop('pos_z', None)
            self._refresh_squads(fields=False)
        if self._draft_grid is not None:
            self._draft_grid_cell = (col,row)
            self._refresh_squads(fields=False)
        hgt = g['hgt'][row][col]
        limit = " · maximum height" if hgt >= HGT_MAX else " · minimum height" if hgt <= HGT_MIN else ""
        try:
            blg_id = int(str(g['blg'][row][col]), 16)
        except (ValueError, TypeError):
            blg_id = 0
        building = self.buildings.get(blg_id) if blg_id else None
        extra = f" · Building {blg_id:02d} {building.name}" if building else ""
        self.info.setText(
            f"({col},{row}) · Sector {g['type'][row][col]} · {FACTIONS.get(g['own'][row][col], '?')} · "
            f"Height {hgt - HGT_MIN}/60 · {(hgt - DEFAULT_HGT) * HEIGHT_UNIT:+.0f} units{limit}{extra}")
        if self.tool == "terrain":
            self.view.brush_cells = {(c, r) for c, r, _w in self.brush.footprint(self.doc, col, row)}
        self.view.update()

    def _terrain_mode(self, modifiers) -> BrushMode:
        if modifiers & Qt.KeyboardModifier.AltModifier:
            return BrushMode.SMOOTH
        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            return BrushMode.LOWER
        return self.mode_combo.currentData()

    def _pressed(self, col, row, _button, modifiers):
        self._finish_script()
        if self.tool == 'terrain' and self._sampling_height:
            self._sample_map_height(col, row)
            return
        if self.tool == 'squad':
            return
        self.view.selection = {(col, row)}
        if self.tool == "select":
            self.view.update()
            return
        self.history.begin(self.doc)
        self._stroke_active, self._stroke_changed = True, False
        self.view.set_editing(self.tool in ("sector", "building", "terrain"))
        self._last_cell = (col, row)
        if self.tool == "terrain":
            self._stroke_mode = self._terrain_mode(modifiers)
            self.brush.begin_stroke(self.doc, col, row)
            # Un click produce un passo; la pressione continua usa la velocità al secondo.
            self._terrain_tick = time.monotonic() - 1.0 / self.brush.strength
            self._repeat.start()
        self._apply(col, row)

    def _dragged(self, col, row, _button, modifiers):
        if not self._stroke_active:
            return
        self._last_cell = (col, row)
        self.view.selection = {(col, row)}
        if self.tool == "terrain":
            self._stroke_mode = self._terrain_mode(modifiers)
        self._apply(col, row)

    def _repeat_terrain(self):
        if self._stroke_active and self._last_cell and self.tool == "terrain":
            self._stroke_mode = self._terrain_mode(QApplication.keyboardModifiers())
            self._apply(*self._last_cell)

    def _released(self, _col, _row, _modifiers):
        self._finish_script()
        self._finish_live()
        if self._drag_original:
            self._finish_actor_drag()
        self._repeat.stop()
        self.view.set_editing(False)
        if self._stroke_active:
            self._stroke_active = False
            if self.history.commit(self.doc, self._stroke_changed):
                self.dirty = True
            self._refresh_title()

    def _double_clicked(self, col, row, _modifiers):
        if self.tool != "terrain":
            return
        self.history.begin(self.doc)
        self.brush.begin_stroke(self.doc, col, row)
        changed = False
        for _ in range(3):
            changed |= bool(self._apply_terrain(col, row, BrushMode.SMOOTH))
        if self.history.commit(self.doc, changed):
            self.dirty = True
        self._refresh_title()

    def _apply_terrain(self, col, row, mode):
        now = time.monotonic()
        dt = max(0.0, now - self._terrain_tick) if self._stroke_active else 0.06
        self._terrain_tick = now
        touched = self.brush.apply(self.doc, col, row, mode, dt=dt)
        if touched:
            self._stroke_changed = True
            self.view.terrain_changed(touched)
        self._hovered(col, row)
        return touched

    def _apply(self, col, row):
        changed = False
        if self.tool == "terrain":
            self._apply_terrain(col, row, self._stroke_mode)
            return
        if self.tool == "sector":
            changed = paint_cells(self.doc, [(col, row)], 'type', f"{self.sel_typ:02x}")
        elif self.tool == "building":
            definition = self.buildings.get(self.sel_building)
            if definition is not None:
                interior = (1 <= col < self.doc.mw - 1 and 1 <= row < self.doc.mh - 1)
                if interior:
                    changed = paint_cells(self.doc, [(col, row)], 'type', f"{definition.sec_type:02x}")
                    changed = paint_cells(self.doc, [(col, row)], 'blg', f"{definition.id:02x}") or changed
        elif self.tool == "owner":
            changed = paint_cells(self.doc, [(col, row)], 'own', self.sel_owner)
        if changed:
            self._stroke_changed = True
            if self.tool in ("sector", "building"):
                self.view.scene_changed([(col, row)])
            else:
                self.view.owners_changed()
            self.building_overlay.update()
            self._hovered(col, row)
