from __future__ import annotations

import os
import time

from PySide6.QtCore import QEvent, QObject, QRunnable, QSize, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import (QAction, QActionGroup, QColor, QIcon, QImage, QKeySequence,
                           QPainter, QPen, QPixmap)
from PySide6.QtWidgets import (QApplication, QAbstractItemView, QComboBox, QDockWidget, QFileDialog,
                               QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListView, QListWidget,
                               QListWidgetItem, QMainWindow, QMessageBox, QPushButton,
                               QSlider, QTabWidget, QVBoxLayout,
                               QWidget)

from ..core.asset_bridge import SetAssets
from ..core.building_defs import load_building_files
from ..core.game_installation import GameInstallation, load_remembered
from ..core.factions import load_owner_colors
from ..core.history import History
from ..core.ldf_model import (DEFAULT_HGT, FACTIONS, HGT_MIN, HGT_MAX,
                              LdfDocument, load_ldf, save_ldf)
from ..core.resource_catalog import ResourceCatalog, preview_image
from ..render.map_viewport import create_viewport
from ..render.terrain_mesh import HEIGHT_UNIT
from ..render.sector_mesh import SectorMeshLibrary
from ..render.sector_sprite import rasterize_sprite, sprite_input
from ..tools.brush import BrushMode, BrushShape, TerrainBrush
from ..tools.paint import paint_cells
from .dialogs import GameInstallationDialog, LevelInfoDialog, NewMapDialog, ResizeDialog
from .music_preview import MusicPreview
from .building_overlay import BuildingOverlay, CapabilityPreview
from .. import bootstrap
from assembly_viewer import VIEW_PRESET_ANGLES

TOOLS = (("select", "Select"), ("sector", "Sector"), ("building", "Building"),
         ("owner", "Faction"), ("terrain", "Terrain"))
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
        self.libs: dict[int, SectorMeshLibrary] = {}
        self.brush = TerrainBrush()
        self.tool = "sector"
        self.sel_typ = 5
        self.sel_building = 1
        self.sel_owner = 1
        self.buildings: dict = {}
        self._sec_index: dict = {}
        self._bicon_level = 1
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
        self._icon_pool = QThreadPool(self)
        self._icon_pool.setMaxThreadCount(1)

        self.view = create_viewport()
        self.music_preview = MusicPreview(self)
        self.view.owner_colors = load_owner_colors()
        self.setCentralWidget(self.view)
        self.building_overlay = BuildingOverlay(self.view, self._overlay_state)
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

        self._build_actions()
        self._build_palette()
        self.info = QLabel("")
        self.statusBar().addPermanentWidget(self.info)
        self.renderer_badge = QLabel()
        self.statusBar().addPermanentWidget(self.renderer_badge)
        self.view.backendChanged.connect(self._renderer_changed)
        self._renderer_changed(self.view.renderer_name)
        self.statusBar().showMessage("Click: edit · right drag: rotate · middle drag: pan · wheel: zoom", 8000)
        self._new_doc(doc if doc is not None else LdfDocument(mw=15, mh=15), path)

    def _overlay_state(self):
        return {
            "doc": self.doc,
            "lib": self._lib(),
            "buildings": self.buildings,
        }

    def _renderer_changed(self, name):
        self.renderer_badge.setText('GPU' if name.startswith('OpenGL') else 'Software')
        self.renderer_badge.setToolTip(name)

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
        act(map_menu, "Level Info...", self.map_level_info)
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
        tabs = QTabWidget()
        self.palette_tabs = tabs
        panel = QWidget()
        panel_layout = QVBoxLayout(panel)
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
        building_zoom_row = QHBoxLayout()
        building_zoom_row.addWidget(QLabel("Preview size"))
        building_zoom_row.addStretch()
        self.building_zoom_out = QPushButton("−")
        self.building_zoom_out.setToolTip("Smaller building previews")
        self.building_zoom_out.clicked.connect(lambda: self._building_zoom_step(-1))
        building_zoom_row.addWidget(self.building_zoom_out)
        self.building_zoom_label = QLabel("2/3")
        building_zoom_row.addWidget(self.building_zoom_label)
        self.building_zoom_in = QPushButton("+")
        self.building_zoom_in.setToolTip("Larger building previews")
        self.building_zoom_in.clicked.connect(lambda: self._building_zoom_step(1))
        building_zoom_row.addWidget(self.building_zoom_in)
        self.building_zoom_out.setFixedWidth(32)
        self.building_zoom_in.setFixedWidth(32)
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
        for owner, name in FACTIONS.items():
            item = QListWidgetItem(f"{owner} {name}")
            item.setData(Qt.ItemDataRole.UserRole, owner)
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
        self.shape_combo = QComboBox()
        self.shape_combo.addItem('Square', BrushShape.SQUARE)
        self.shape_combo.addItem('Round', BrushShape.ROUND)
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
        self.radius_slider = QSlider(Qt.Orientation.Horizontal, minimum=1, maximum=24, value=4)
        self.radius_slider.setToolTip("Set both X and Z radii; use the sliders below for independent values")
        self.radius_slider.valueChanged.connect(self._radius_slider_changed)
        self.radius_slider_label = QLabel("2.0")
        self.radius_slider_label.setMinimumWidth(44)
        radius_row = QHBoxLayout()
        radius_row.addWidget(self.radius_slider)
        radius_row.addWidget(self.radius_slider_label)
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
        form.addRow("Mode", self.mode_combo)
        form.addRow('Shape', self.shape_combo)
        form.addRow('Radius', radius_row)
        form.addRow('Radius X', radius_x_row)
        form.addRow('Radius Z', radius_z_row)
        form.addRow("Force", force_row)
        form.addRow(self.brush_label)
        tabs.addTab(terrain, "Terrain")
        self._brush_params()
        dock.setWidget(panel)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)
        dock.setMinimumWidth(400)
        self.palette_menu.addAction(dock.toggleViewAction())
        tabs.currentChanged.connect(self._palette_changed)
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
            widget.item(i).setHidden(text not in widget.item(i).text().lower())
        self._icons.start()

    def eventFilter(self, obj, event):
        if obj is self.sector_list.viewport() and event.type() == QEvent.Type.Resize:
            QTimer.singleShot(0, self._layout_sector_icons)
        return super().eventFilter(obj, event)

    def _layout_sector_icons(self):
        columns = SECTOR_PREVIEW_COLUMNS[self._icon_level]
        style = self.sector_list.style()
        scroll_width = style.pixelMetric(style.PixelMetric.PM_ScrollBarExtent)
        available = max(60, self.sector_list.viewport().width() - 4)
        if self.sector_list.verticalScrollBar().isVisible():
            available = max(60, available - scroll_width)
        spacing = self.sector_list.spacing()
        cell_width = max(60, (available - spacing * (columns - 1)) // columns)
        icon_width = max(32, min(round(96 * self._icon_scale), cell_width - 12))
        icon_height = round(80 * self._icon_scale)
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

    def _radius_slider_changed(self, value: int):
        for slider in (self.radius_x_slider, self.radius_z_slider):
            slider.blockSignals(True)
            slider.setValue(value)
            slider.blockSignals(False)
        self._brush_params()

    def _radius_x_changed(self, value: int):
        self._brush_params()

    def _radius_z_changed(self, value: int):
        self._brush_params()

    def _brush_params(self):
        radius_x = self.radius_x_slider.value() / 2
        radius_z = self.radius_z_slider.value() / 2
        self.brush.radius_x = radius_x
        self.brush.radius_z = radius_z
        self.radius_x_label.setText(f"{radius_x:g}")
        self.radius_z_label.setText(f"{radius_z:g}")
        self.radius_slider.blockSignals(True)
        self.radius_slider.setValue(round(max(radius_x, radius_z) * 2))
        self.radius_slider.blockSignals(False)
        self.radius_slider_label.setText(
            f"{radius_x:g}" if radius_x == radius_z
            else f"X {radius_x:g} · Z {radius_z:g}")
        self.brush.shape = self.shape_combo.currentData()
        self.brush.strength = float(self.strength.value())
        self.force_label.setText(f"{self.brush.strength:.0f}")
        equal = radius_x == radius_z
        shape = ('Square' if equal else 'Rectangle') if self.brush.shape == BrushShape.SQUARE else ('Circle' if equal else 'Ellipse')
        self.brush_label.setText(f'{shape} · X {radius_x:g} · Z {radius_z:g} · Force {self.brush.strength:.0f} steps/s')
        self.brush_label.setToolTip('Radii in sectors. One step = 100 game units. Height 0–60, initial ground 30.')
        if self.doc is not None and self.view.hover is not None:
            self._hovered(*self.view.hover)

    def _set_view_preset(self, index):
        angles = self.view_preset.itemData(index)
        if angles is not None:
            self.view.set_camera_angles(*angles)
            self.view_preset.setCurrentIndex(index)

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
        # Sector icons share one fixed isometric canvas on purpose: every
        # preview keeps the same size and baseline so the grid stays aligned.
        if sprite is not None:
            icon = QIcon(QPixmap.fromImage(sprite.image))
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
        self._refresh_sky()
        self._refresh_music()
        self.building_overlay.update()
        self.view.scene_changed()

    def _refresh_owner_palette(self):
        for row in range(self.owner_list.count()):
            item = self.owner_list.item(row)
            owner = item.data(Qt.ItemDataRole.UserRole)
            pix = QPixmap(16, 16)
            pix.fill(QColor(*self.view.owner_colors[owner]) if owner else QColor(145, 145, 145))
            painter = QPainter(pix)
            painter.setPen(QPen(QColor(175, 175, 175), 1))
            painter.drawRect(0, 0, 15, 15)
            painter.end()
            item.setIcon(QIcon(pix))

    def _new_doc(self, doc: LdfDocument, path: str | None):
        self._released(-1, -1, Qt.KeyboardModifier.NoModifier)
        self.doc, self.path, self.dirty = doc, path, False
        self.history.clear()
        self._ensure_lib()
        self._refresh_sky()
        self._refresh_music()
        self.view.set_document(doc)
        self.building_overlay.update()
        self._refresh_title()

    def _refresh_title(self):
        name = os.path.basename(self.path) if self.path else "untitled"
        self.setWindowTitle(f"{'*' if self.dirty else ''}{name} - OpenNeoUA Studio · Map Editor")
        self.undo_action.setEnabled(self.history.can_undo)
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
        path, _ = QFileDialog.getOpenFileName(self, "Open LDF", self._level_dir(), "Levels (*.ldf *.LDF)",
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
        path, _ = QFileDialog.getSaveFileName(self, "Save LDF", self.path or self._level_dir(), "Levels (*.LDF)",
                                             options=QFileDialog.Option.DontUseNativeDialog)
        if path:
            self._save_to(path)

    def undo(self):
        self._released(-1, -1, Qt.KeyboardModifier.NoModifier)
        if self.history.undo(self.doc):
            self._after_history()

    def redo(self):
        self._released(-1, -1, Qt.KeyboardModifier.NoModifier)
        if self.history.redo(self.doc):
            self._after_history()

    def _after_history(self):
        self.dirty = True
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
        self._refresh_title()

    def map_level_info(self):
        if not self.doc:
            return
        self._released(-1, -1, Qt.KeyboardModifier.NoModifier)
        dialog = LevelInfoDialog(self.doc, self)
        dialog.musicChanged.connect(lambda value: self._play_music(value))
        if dialog.exec():
            values = dialog.values()
            if all(self.doc.lvl_info.get(key) == value for key, value in values.items()):
                return
            self.history.push(self.doc)
            self.doc.lvl_info.update(values)
            self.view.set_sky_image(dialog.selected_sky_image())
            self._refresh_music()
            self.dirty = True
            self._refresh_title()
        else:
            self._refresh_music()

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
        catalog = ResourceCatalog(install, self.doc.set_number)
        name = os.path.splitext(os.path.basename(
            str(self.doc.lvl_info.get("sky", "")).replace("\\", "/")))[0].casefold()
        source = catalog.skies.get(name)
        try:
            image = preview_image(source, catalog.palette) if source else None
        except Exception as exc:
            image = None
            self.statusBar().showMessage(f"Unable to preview sky: {exc}", 4000)
        self.view.set_sky_image(image)

    def set_tool(self, key: str):
        if self._stroke_active:
            self._released(-1, -1, Qt.KeyboardModifier.NoModifier)
        self.tool = key
        self.tool_actions[key].setChecked(True)
        if key != "select":
            self.palette_tabs.setCurrentIndex({"sector": 0, "building": 1, "owner": 2, "terrain": 3}[key])
        self.view.brush_cells = set()
        self.view.update()

    def _palette_changed(self, index):
        self.set_tool(("sector", "building", "owner", "terrain")[index])
        if index == 0 and self._icon_queue:
            self._icons.start()
        if index == 1 and self._bicon_queue:
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
            self.view.brush_cells = {(c, r) for c, r, _w in
                                     self.brush.footprint(self.doc, col, row)}
        self.view.update()

    def _terrain_mode(self, modifiers) -> BrushMode:
        if modifiers & Qt.KeyboardModifier.AltModifier:
            return BrushMode.SMOOTH
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            return BrushMode.FLATTEN
        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            return BrushMode.LOWER
        return self.mode_combo.currentData()

    def _pressed(self, col, row, _button, modifiers):
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
