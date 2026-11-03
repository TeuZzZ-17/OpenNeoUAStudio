"""Sector Editor workspace, sharing the Studio shell, resource loaders and renderer."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import math
from pathlib import Path
import tempfile
from zipfile import ZipFile, ZIP_DEFLATED

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QTimer, Qt, QItemSelectionModel, QEvent, Signal
from PySide6.QtGui import QAction, QIcon, QKeySequence, QKeyEvent
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QAbstractItemView, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QRadioButton,
    QSizePolicy, QSpinBox, QSplitter, QTabWidget, QToolButton, QVBoxLayout, QWidget,
)

from map_editor import bootstrap
from map_editor.core.asset_bridge import SetAssets
from map_editor.core.sector_resolver import SectorResolver
from map_editor.render.sector_mesh import SectorMeshLibrary
from map_editor.ui.preview_cards import DETAIL_ROLE, set_card
from studio.exchange.portable import DirectoryIndex, commit_verified_files
from studio.rendering.asset_viewport import VIEW_PRESETS, VIEW_PRESET_ANGLES
from studio.rendering.snapshot_export import (
    CONTACT_SHEET_BACKGROUND, compose_contact_sheet, write_static_image_atomic,
)
from studio.ui.tabs import PaletteTabStrip
from studio.ui.widgets import (
    configure_operation_status_bar, install_standard_file_menu_tail,
    loading_resources, use_standard_list_style, ViewportWidthScrollArea,
)
from .document import AUTO, SetDocument, SET_FOLDERS, table_issues, MODEL_LIMIT, BUILDING_LIMIT
from .viewport import SectorViewport, covers_sector
from .interactions import ModelList, SectorList
from .visual_aids import GroundPreview, SymbolPreview
from .previews import CardPreviews, visible_items
from .textures import LOOSE_SETS, import_textures
from studio.ui.cursor_themes import studio_cursor

STATES = ("Intact", "Damaged", "Heavily damaged", "Destroyed")
COLLISION_MODES = ("Own collision skeleton", "Standard small cell", "Standard large cell")
PREVIEW_ROLE = int(Qt.ItemDataRole.UserRole) + 10
# The Bottom view looks at the underside of the ground: never useful for sectors.
SECTOR_VIEWS = tuple(name for name in VIEW_PRESETS if name != "Bottom")


def button(text, callback, layout):
    control = QPushButton(text)
    control.clicked.connect(callback)
    layout.addWidget(control)
    return control


def scroll_page(content):
    scroll = ViewportWidthScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.setWidget(content)
    return scroll


def png_bytes(image):
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if image.isNull() or not image.save(buffer, "PNG"):
        raise OSError("The sector image could not be encoded.")
    return bytes(data)


class StateSelector(QWidget):
    """The damage state shown everywhere: previews, sector cards and the cell editor."""
    currentIndexChanged = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        title = QLabel("Damage state")
        font = title.font()
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)
        grid = QGridLayout()
        grid.setSpacing(3)
        layout.addLayout(grid)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        for index, name in enumerate(STATES):
            choice = QPushButton(name)
            choice.setCheckable(True)
            choice.setMinimumHeight(30)
            choice.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self.group.addButton(choice, index)
            grid.addWidget(choice, index // 2, index % 2)
        self.group.button(0).setChecked(True)
        self.setStyleSheet("QPushButton:checked { background: palette(highlight); "
                           "color: palette(highlighted-text); font-weight: bold; }")
        self.group.idToggled.connect(lambda index, checked: checked and self.currentIndexChanged.emit(index))

    def currentIndex(self):
        return self.group.checkedId()

    def setCurrentIndex(self, index):
        self.group.button(index).setChecked(True)

    def count(self):
        return len(STATES)


class NewSetDialog(QDialog):
    def __init__(self, source=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Create Set")
        self.resize(650, 380)
        layout = QVBoxLayout(self)
        hint = QLabel("Choose the five resource folders. An empty sector list keeps their model and building definitions ready to use.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        form = QFormLayout()
        layout.addLayout(form)
        self.paths = {}
        for name in (*SET_FOLDERS, "Destination"):
            row = QHBoxLayout()
            edit = QLineEdit()
            if source and name != "Destination":
                found = bootstrap.find_ci(Path(source), name)
                if found:
                    edit.setText(str(found))
            edit.setPlaceholderText("New folder named Set<number>" if name == "Destination" else f"Select {name} folder")
            row.addWidget(edit, 1)
            browse = QPushButton("Browse…")
            browse.clicked.connect(lambda checked=False, field=edit, dest=name == "Destination": self._browse(field, dest))
            row.addWidget(browse)
            form.addRow(name, row)
            self.paths[name] = edit
        self.keep = QCheckBox("Keep sector definitions from the selected Scripts folder")
        layout.addWidget(self.keep)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse(self, field, destination):
        if destination:
            parent = QFileDialog.getExistingDirectory(self, "Choose parent folder for the new set")
            if parent:
                number, ok = QInputDialog.getInt(self, "Set number", "New set number:", 8, 1, 255)
                if ok:
                    field.setText(str(Path(parent) / f"Set{number}"))
        else:
            path = QFileDialog.getExistingDirectory(self, "Choose resource folder", field.text())
            if path:
                field.setText(path)


class ExportSetDialog(QDialog):
    """Export the complete set as a plain folder or as one ZIP archive."""

    def __init__(self, document, parent=None):
        super().__init__(parent)
        self.document = document
        self.setWindowTitle("Export Set")
        self.resize(560, 210)
        layout = QVBoxLayout(self)
        self.folder = QRadioButton("Folder (unpacked set, ready to copy into Data/Sets)")
        self.archive = QRadioButton("ZIP archive")
        self.folder.setChecked(True)
        layout.addWidget(self.folder)
        layout.addWidget(self.archive)
        form = QFormLayout()
        row = QHBoxLayout()
        self.path = QLineEdit()
        row.addWidget(self.path, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row.addWidget(browse)
        form.addRow("Destination", row)
        self.number = QSpinBox()
        self.number.setRange(1, 255)
        self.number.setValue(document.number)
        form.addRow("Set number", self.number)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        for choice in (self.folder, self.archive):
            choice.toggled.connect(self._format_changed)
        self._format_changed()

    def _format_changed(self):
        self.number.setEnabled(self.folder.isChecked())
        self.path.setPlaceholderText("Parent folder: a new Set<number> folder is created inside"
                                     if self.folder.isChecked() else "ZIP file")
        self.path.clear()

    def _browse(self):
        if self.folder.isChecked():
            path = QFileDialog.getExistingDirectory(self, "Choose parent folder for the exported set")
        else:
            path, _ = QFileDialog.getSaveFileName(self, "Export complete set",
                                                  f"Set{self.document.number}.zip", "ZIP archive (*.zip)")
        if path:
            self.path.setText(path)

    def target(self):
        text = self.path.text().strip()
        if not text:
            return None
        return Path(text) / f"Set{self.number.value()}" if self.folder.isChecked() else Path(text)


class SectorEditorWindow(QMainWindow):
    def __init__(self, startup_path=None, parent=None):
        super().__init__(parent)
        self.document = None
        self.assets = self.library = None
        self.current_sector = None
        self.selected_cells = {4}
        self._syncing = False
        self._copied_positions = ()
        self._copied_cells = None
        self._copied_sectors = None
        self._placement = None
        self._browse_preview = False
        self._selection_anchor = 4
        self._placement_hover = None
        self._model_titles = None
        self._editing_model_id = None
        self._model_snapshot = None
        self._model_hidden_numbers = (0, 1)
        self._sector_fields_number = None
        self._description_snapshot = None
        self.setWindowTitle("OpenNeoUA Studio - Sector Editor")
        self.resize(1450, 900)
        configure_operation_status_bar(self)
        self._build_ui()
        self._build_actions()
        self.previews = CardPreviews(self.view, self)
        self.previews.ready.connect(self._card_ready)
        self.preview_timer = QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.setInterval(60)
        self.preview_timer.timeout.connect(self._request_previews)
        for browser in (self.sectors, self.models):
            browser.previewsRequested.connect(self.preview_timer.start)
        self.tabs.currentChanged.connect(lambda: self.preview_timer.start())
        self.reload_game_resources(initial=True)
        if startup_path:
            self.open_set(startup_path)
        elif self.sets.count():
            from studio.ui.shell import selected_set
            index = self.sets.findData(selected_set())
            self.sets.blockSignals(True)
            self.sets.setCurrentIndex(max(0, index))
            self.sets.blockSignals(False)
            self.open_set(bootstrap.set_dir(self.sets.currentData()))

    def _add_actions(self, menu, entries):
        for key, text, shortcut, callback in entries:
            action = QAction(text, self)
            if shortcut is not None:
                action.setShortcut(shortcut)
            action.triggered.connect(callback)
            menu.addAction(action)
            self.actions[key] = action

    def _build_actions(self):
        self.actions = {}
        file = self.menuBar().addMenu("File")
        self._add_actions(file, (
            ("new", "New Set…", QKeySequence.StandardKey.New, self.new_set),
            ("open", "Open Set…", QKeySequence.StandardKey.Open, self.choose_set),
            ("save", "Overwrite Set", QKeySequence.StandardKey.Save, self.save),
            ("export", "Export Set…", QKeySequence.StandardKey.SaveAs, self.export_set),
        ))
        file.addSeparator()
        self._add_actions(file, (("textures", "Import custom textures…", None, self.import_custom_textures),))
        file.addSeparator()
        self._add_actions(file, (
            ("image", "Export current sector image…", None, self.export_preview),
            ("images", "Export all sector images (ZIP)…", None, self.export_all_images),
            ("sheet", "Export sector sheet…", None, self.export_sheet),
        ))
        install_standard_file_menu_tail(file, self)

        edit = self.menuBar().addMenu("Edit")
        self._add_actions(edit, (
            ("undo", "< Undo", QKeySequence.StandardKey.Undo, self.undo),
            ("redo", "Redo >", QKeySequence.StandardKey.Redo, self.redo),
            ("reset", "Reset Set…", None, self.reset_set),
        ))
        edit.addSeparator()
        self._add_actions(edit, (
            ("copy", "Copy cell", QKeySequence.StandardKey.Copy, self.copy_cells),
            ("cut", "Cut cell", QKeySequence.StandardKey.Cut, self.cut_cells),
            ("paste", "Paste cell", QKeySequence.StandardKey.Paste, self.paste_cells),
            ("clear", "Clear cell", "Delete", self.clear_cells),
            ("rotate", "Rotate cell…", "R", self.start_rotation),
            ("select_all", "Select all cells", QKeySequence.StandardKey.SelectAll, self._select_all),
        ))
        self.state_menu = edit.addMenu("Change cell state")
        for state, name in enumerate(STATES):
            self.state_menu.addAction(name, lambda state=state: self.change_cell_state(state))
        cancel = QAction("Cancel preview / placement", self)
        cancel.setShortcut("Escape")
        cancel.triggered.connect(self.cancel_interaction)
        self.addAction(cancel)
        self.actions["cancel_preview"] = cancel

        sector = self.menuBar().addMenu("Sector")
        self._add_actions(sector, (
            ("new_sector", "New sector", None, self.add_sector),
            ("copy_sector", "Copy sector", None, lambda: self.copy_sectors()),
            ("cut_sector", "Cut sector", None, lambda: self.copy_sectors(cut=True)),
            ("clear_sector", "Clear sector", None, self.clear_sectors),
            ("wipe_sector", "Wipe sector…", None, self.wipe_sectors),
        ))
        sector.addSeparator()
        self._add_actions(sector, (
            ("import_sector", "Import sector definition…", None, self.import_definitions),
            ("export_sector", "Export sector definition…", None, self.export_definition),
        ))

        view = self.menuBar().addMenu("View")
        view.addAction("Reset View", self.view.reset_camera)
        view.addAction("Top View", lambda: self.view.set_camera_angles(0, 90))
        action = view.addAction("Frame selected cells", self.view.frame_selection)
        action.setShortcut("F")
        action = view.addAction("Reload set assets", self.reload_assets)
        action.setShortcut("F5")
        self.grid_action = view.addAction("Show cell grid")
        self.grid_action.setCheckable(True)
        self.grid_action.setChecked(True)
        self.grid_action.toggled.connect(lambda checked: self.view.update())
        self.validation_action = view.addAction("Validation Report…", self.show_validation)
        self.validation_status = QLabel()
        self.validation_status.linkActivated.connect(self.show_validation)
        self.statusBar().addPermanentWidget(self.validation_status)
        self.validation_status.hide()

        toolbar = self.addToolBar("Set")
        toolbar.setMovable(False)
        for key in ("undo", "reset", "redo"):
            control = QToolButton()
            control.setDefaultAction(self.actions[key])
            control.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
            toolbar.addWidget(control)
        self.actions["reset"].setToolTip("Reset the whole set (asks first)")
        toolbar.addSeparator()
        toolbar.addWidget(QLabel("View "))
        self.view_preset = QComboBox()
        self.view_preset.addItems(SECTOR_VIEWS)
        self.view_preset.activated.connect(self._change_view)
        toolbar.addWidget(self.view_preset)
        toolbar.addAction("Reset View", self.view.reset_camera)
        for menu in self.findChildren(QMenu):
            menu.installEventFilter(self)

    def _build_ui(self):
        splitter = QSplitter()
        self.setCentralWidget(splitter)
        left = QWidget()
        layout = QVBoxLayout(left)
        self.sets = QComboBox()
        self._set_combo = self.sets
        self.sets.setParent(left)
        self.sets.hide()
        self.sets.currentIndexChanged.connect(self._choose_installed)
        self.path_label = QLabel("Sectors")
        self.path_label.setWordWrap(True)
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.path_label)
        self.state = StateSelector()
        self.state.currentIndexChanged.connect(self._state_view_changed)
        layout.addWidget(self.state)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Search ID, hex or description…")
        self.filter.textChanged.connect(self._filter_sectors)
        layout.addWidget(self.filter)
        self.slot_filter = QComboBox()
        self.slot_filter.addItems(("All slots", "Defined sectors", "Free slots", "Using selected model"))
        self.slot_filter.currentIndexChanged.connect(self._filter_sectors)
        layout.addWidget(self.slot_filter)
        self.sectors = SectorList(large_text=False)
        use_standard_list_style(self.sectors)
        self.sectors.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.sectors.currentItemChanged.connect(self._sector_selected)
        self.sectors.applyRequested.connect(self.confirm_placement)
        self.sectors.itemSelectionChanged.connect(self._update_sector_actions)
        self.sectors.itemDoubleClicked.connect(self._sector_activated)
        self.sectors.carryDropped.connect(self.paste_sectors)
        self.sectors.carryCancelled.connect(self.cancel_sector_copy)
        self.sectors.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.sectors.customContextMenuRequested.connect(self._sector_menu)
        layout.addWidget(self.sectors, 1)
        layout.addWidget(self.sectors.zoom_controls())
        row = QHBoxLayout()
        button("New", self.add_sector, row)
        layout.addLayout(row)
        self.counts = QLabel()
        self.counts.setWordWrap(True)
        layout.addWidget(self.counts)
        splitter.addWidget(left)

        center = QWidget()
        layout = QVBoxLayout(center)
        row = QHBoxLayout()
        self.preview_mode = QComboBox()
        self.preview_mode.addItems(("Sector preview", "Selected model preview"))
        self.preview_mode.currentIndexChanged.connect(lambda: self._preview(reset=True))
        row.addWidget(self.preview_mode, 1)
        button("Frame selection", lambda: self.view.frame_selection(), row)
        layout.addLayout(row)
        self.view = SectorViewport()
        self._connect_cell_view(self.view)
        self.view.backendChanged.connect(lambda name: self.statusBar().showMessage(name, 5000))
        layout.addWidget(self.view, 1)
        self.free_hint = QLabel("Free sector slot\n\nDouble-click it in the list, or press New, to create a sector here.")
        self.free_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.free_hint.setWordWrap(True)
        font = self.free_hint.font()
        font.setPointSizeF(font.pointSizeF() + 2)
        self.free_hint.setFont(font)
        self.free_hint.hide()
        layout.addWidget(self.free_hint, 1)
        self.preview_status = QLabel("Right drag: orbit · Middle drag: pan · Wheel: zoom · Ctrl+click: select several cells")
        self.preview_status.setWordWrap(True)
        layout.addWidget(self.preview_status)
        splitter.addWidget(center)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        self.tabs = QTabWidget()
        right_layout.addWidget(self.tabs)
        cells = QWidget()
        layout = QVBoxLayout(cells)
        form = QFormLayout()
        self.sector_title = QLabel("No sector selected")
        layout.addWidget(self.sector_title)
        self.layout_mode = QComboBox()
        self.layout_mode.addItems(("3 × 3 cells", "One large cell"))
        self.layout_mode.activated.connect(self._layout_changed)
        form.addRow("Layout", self.layout_mode)
        self.ground = QSpinBox()
        self.ground.setRange(AUTO, 5)
        self.ground.setSpecialValueText("Automatic")
        self.ground.setToolTip("Filler family between neighboring sectors (0–5). Automatic copies it from the most similar sector.")
        self.ground.valueChanged.connect(self._sector_fields_changed)
        form.addRow("Connection family", self.ground)
        self.ground_preview = GroundPreview()
        form.addRow(self.ground_preview)
        self.symbol = QSpinBox()
        self.symbol.setRange(AUTO, 255)
        self.symbol.setSpecialValueText("Automatic")
        self.symbol.setToolTip("Glyph of this sector on the strategic map. Automatic copies it from the most similar sector.")
        self.symbol.valueChanged.connect(self._sector_fields_changed)
        form.addRow("Minimap symbol", self.symbol)
        self.symbol_preview = SymbolPreview()
        form.addRow(self.symbol_preview)
        self.description = QLineEdit()
        self.description.editingFinished.connect(self._sector_fields_changed)
        self.description.textChanged.connect(self._model_form_changed)
        form.addRow("Description", self.description)
        layout.addLayout(form)
        self.cell_grid = SectorViewport(top_down=True)
        self.cell_grid.setMinimumSize(0, 230)
        self.cell_grid.setMaximumHeight(280)
        self.cell_grid.setToolTip("Click: select · Ctrl+click: add/remove · Drag: move · Double-click: place the previewed model · Right click: tools")
        self._connect_cell_view(self.cell_grid)
        layout.addWidget(self.cell_grid)
        self.selection_info = QLabel()
        self.selection_info.setWordWrap(True)
        layout.addWidget(self.selection_info)
        content = QLabel("Cell content")
        font = content.font()
        font.setBold(True)
        content.setFont(font)
        content.setToolTip("The model shown in each damage state. Choosing the Intact model fills the other states automatically.")
        layout.addWidget(content)
        form = QFormLayout()
        self.state_models = []
        for state, title in enumerate(STATES):
            combo = QComboBox()
            combo.currentIndexChanged.connect(lambda _index, state=state: self._state_model_changed(state))
            self.state_models.append(combo)
            form.addRow(title, combo)
        self.destructible = QCheckBox("Destructible")
        self.destructible.toggled.connect(lambda: self._state_model_changed(None))
        form.addRow(self.destructible)
        layout.addLayout(form)
        layout.addStretch()
        self.cells_page = scroll_page(cells)
        self.tabs.addTab(self.cells_page, "Cells")

        self.models_page = QWidget()
        layout = QVBoxLayout(self.models_page)
        self.model_target = QLabel()
        self.model_target.setWordWrap(True)
        layout.addWidget(self.model_target)
        self.model_search = QLineEdit()
        self.model_search.setPlaceholderText("Search set models…")
        self.model_search.textChanged.connect(self._filter_models)
        self.model_search.returnPressed.connect(self.confirm_placement)
        layout.addWidget(self.model_search)
        self.models = ModelList(large_text=False)
        # The narrower right panel shows two model tiles per row by default.
        self.models.tile_level = 1
        self.models.setMinimumHeight(120)
        use_standard_list_style(self.models)
        self.models.currentItemChanged.connect(self._model_selected)
        self.models.applyRequested.connect(self.confirm_placement)
        self.models.itemClicked.connect(self._model_selected)
        self.models.itemDoubleClicked.connect(lambda item: self.apply_model())
        self.models.dragFinished.connect(self.cancel_preview)
        self.view.model_source = self.cell_grid.model_source = self.models
        self.models.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.models.customContextMenuRequested.connect(self._model_menu)
        layout.addWidget(self.models, 1)
        layout.addWidget(self.models.zoom_controls())
        self.apply_model_button = button("Apply model to selected cells", self.apply_model, layout)
        self.tabs.addTab(self.models_page, "Models")
        self.tabs.currentChanged.connect(self._browse_tab_changed)

        definitions = QWidget()
        layout = QVBoxLayout(definitions)
        intro = QLabel("A model is one 3D BASE object with its collision. Cells use models for their "
                       "four damage states, so a change here updates every cell that uses this model.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.model_combo = QComboBox()
        self.model_combo.currentIndexChanged.connect(self._load_model_fields)
        layout.addWidget(self.model_combo)
        self.model_usage = QLabel()
        self.model_usage.setWordWrap(True)
        layout.addWidget(self.model_usage)
        form = QFormLayout()
        self.base = QComboBox()
        self.base.setEditable(True)
        form.addRow("BASE model", self.base)
        self.sklt = QComboBox()
        self.sklt.setEditable(True)
        form.addRow("Collision skeleton", self.sklt)
        self.collision_mode = QComboBox()
        self.collision_mode.addItems(COLLISION_MODES)
        self.collision_mode.setToolTip("Own: use the skeleton above · Standard: the engine's ready-made shape for a small or large cell")
        form.addRow("Collision", self.collision_mode)
        self.shield = QSpinBox()
        self.shield.setRange(0, 100)
        self.shield.setSuffix(" %")
        self.shield.setToolTip("Damage reduction of the building in this model.")
        form.addRow("Shield", self.shield)
        self.model_comment = QLineEdit()
        form.addRow("Description", self.model_comment)
        layout.addLayout(form)
        effect_hint = QLabel("Destruction effects · one per line: effect ID, X, Z, Y")
        effect_hint.setWordWrap(True)
        layout.addWidget(effect_hint)
        self.effects = QPlainTextEdit()
        self.effects.setMaximumHeight(110)
        layout.addWidget(self.effects)
        self.update_model_button = button("Apply changes", self.update_model, layout)
        for control, signal in ((self.base, "currentTextChanged"), (self.sklt, "currentTextChanged"),
                                (self.model_comment, "textChanged"), (self.effects, "textChanged"),
                                (self.collision_mode, "currentIndexChanged"), (self.shield, "valueChanged")):
            getattr(control, signal).connect(self._model_form_changed)
        row = QHBoxLayout()
        button("New model", self.add_model, row)
        button("Duplicate model", lambda: self.add_model(duplicate=True), row)
        layout.addLayout(row)
        layout.addStretch()
        self.definitions_page = scroll_page(definitions)
        self.tabs.addTab(self.definitions_page, "Definitions")
        self.issues = QPlainTextEdit(self)
        self.issues.setReadOnly(True)
        self.issues.hide()
        PaletteTabStrip.install(self.tabs)
        splitter.addWidget(right)
        left.setMinimumWidth(205)
        right.setMinimumWidth(330)
        for combo in right.findChildren(QComboBox):
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(8)
            combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        splitter.setSizes([280, 760, 385])

    def reload_game_resources(self, initial=False):
        self.sets.blockSignals(True)
        self.sets.clear()
        install = bootstrap.installation()
        numbers = install.available_sets() if install else []
        if not numbers:
            try:
                root = bootstrap.find_ci(bootstrap.game_data_dir(), "Sets") or bootstrap.game_data_dir() / "Sets"
                numbers = sorted(int(p.name[3:]) for p in root.iterdir()
                                 if p.is_dir() and p.name.lower().startswith("set") and p.name[3:].isdigit())
            except (OSError, RuntimeError):
                numbers = []
        for number in numbers:
            self.sets.addItem(f"Set {number}", number)
        self.sets.blockSignals(False)
        if self.document is not None:
            self._sync_set_combo()
        if not initial and self.document is not None:
            self.reload_assets()

    def reload_assets(self):
        if self.document is None or not self._resolve_model_form():
            return
        self._commit_sector_fields()
        try:
            with loading_resources(self, "Reloading set assets…"):
                # Files added since the last scan (custom textures, BASE) must be seen.
                DirectoryIndex.clear_cache()
                assets = SetAssets(self.document.number, set_dir=self.document.folder).load()
                assets.sdf = self.document.sdf
                library = SectorMeshLibrary(assets, set_only=True)
            self.assets, self.library = assets, library
            self.document.assets = assets
            self.document._asset_check = None
            self.document.embedded_names = [obj.base_object.name for obj in assets.family.root_object.kids[1].kids]
            self.previews.reset(library)
            self._model_snapshot = None
            self._load_asset_names()
            self._refresh()
            self.statusBar().showMessage("Set assets reloaded. Current edits have been preserved.", 5000)
        except (OSError, ValueError, RuntimeError) as exc:
            QMessageBox.warning(self, "Cannot reload set assets", str(exc))

    def _change_view(self, index):
        name = self.view_preset.itemText(index)
        if name in VIEW_PRESET_ANGLES:
            self.view.set_camera_angles(*VIEW_PRESET_ANGLES[name])

    def _state_view_changed(self, index):
        self._preview()
        self._load_sector_fields()

    def _sector_menu(self, point):
        item = self.sectors.itemAt(point)
        if item is None or self.document is None or self.sectors.carrying:
            return
        number = item.data(Qt.ItemDataRole.UserRole)
        if item.isSelected():
            self.sectors.setCurrentItem(item, QItemSelectionModel.SelectionFlag.NoUpdate)
        else:
            self.sectors.setCurrentItem(item)
        if self.current_sector != number:
            return
        self._update_sector_actions()
        menu = QMenu(self)
        if number not in self.document.sdf.sectors:
            menu.addAction("Create sector here", self.add_sector)
            menu.addSeparator()
        for key in ("import_sector", "export_sector", None, "wipe_sector", "clear_sector", None,
                    "copy_sector", "cut_sector"):
            if key is None:
                menu.addSeparator()
            else:
                menu.addAction(self.actions[key])
        self._prepare_menu(menu)
        menu.exec(self.sectors.viewport().mapToGlobal(point))

    def _model_menu(self, point):
        item = self.models.itemAt(point)
        if item is None:
            return
        mid = item.data(Qt.ItemDataRole.UserRole)
        self.models.setCurrentItem(item)
        if self.model_combo.currentData() != mid:
            return
        menu = QMenu(self)
        menu.addAction("Apply to selected cells", self.apply_model)
        menu.addAction("Preview model", lambda: self.preview_mode.setCurrentIndex(1))
        menu.addAction("Edit definition", lambda: self.tabs.setCurrentWidget(self.definitions_page))
        menu.addAction("Find sectors using this model", self.find_model_usage)
        menu.addAction("Duplicate model", lambda: self.add_model(duplicate=True))
        self._prepare_menu(menu)
        menu.exec(self.models.viewport().mapToGlobal(point))

    def _connect_cell_view(self, view):
        view.cellsPicked.connect(self.select_cell)
        view.cellsRangePicked.connect(self.select_cell_range)
        view.cellContextRequested.connect(self._cell_menu)
        view.modelDropped.connect(self.drop_model)
        view.placementRequested.connect(self.finish_cell_placement)
        view.placementHovered.connect(self.preview_cell_placement)
        view.operationCancelled.connect(self.cancel_interaction)
        view.previewFailed.connect(lambda message: self.statusBar().showMessage(message, 6000))
        view.cellsBoxPicked.connect(self.select_cell_box)
        view.selectionCleared.connect(self.clear_cell_selection)
        view.cellDragStarted.connect(self.start_cell_drag)
        view.navigationRequested.connect(self.navigate_browser)
        view.applyRequested.connect(self.confirm_placement)
        view.cellActivated.connect(self._cell_activated)
        view.rotationChosen.connect(self.finish_rotation)

    def _cell_menu(self, index, global_point):
        if self.document is None or self.current_sector not in self.document.sdf.sectors:
            return
        if index not in self.selected_cells:
            self.select_cell(index)
        if index not in self.selected_cells:
            return
        self.cancel_preview()
        menu = QMenu(self)
        for key in ("copy", "cut", "paste", "clear", "rotate", "select_all"):
            menu.addAction(self.actions[key])
        menu.addMenu(self.state_menu)
        self._prepare_menu(menu)
        menu.exec(global_point)

    def _selected_sector_numbers(self):
        """Defined sectors among the selected slots, always including the current one."""
        numbers = {item.data(Qt.ItemDataRole.UserRole) for item in self.sectors.selectedItems()}
        numbers.add(self.current_sector)
        return sorted(n for n in numbers if n in self.document.sdf.sectors)

    def _selected_slots(self):
        numbers = {item.data(Qt.ItemDataRole.UserRole) for item in self.sectors.selectedItems()}
        return sorted(numbers | {self.current_sector})

    def _update_sector_actions(self):
        defined = bool(self.document and self._selected_sector_numbers())
        for key in ("copy_sector", "cut_sector", "clear_sector", "wipe_sector", "export_sector"):
            self.actions[key].setEnabled(defined)
        self.actions["import_sector"].setEnabled(self.document is not None)
        if self.document is not None:
            count = len(self.sectors.selectedItems())
            if count > 1 and not self._syncing:
                self.statusBar().showMessage(f"{count} sector slots selected · Sector tools and the connection family apply to all of them.", 5000)

    def _browse_tab_changed(self, index):
        self.cancel_preview()
        if hasattr(self, "models_page") and self.tabs.widget(index) is self.models_page:
            self._browse_preview = True
            self._update_ghost()

    def cancel_interaction(self):
        self.cancel_preview()
        self.cancel_sector_copy()
        for view in (self.view, self.cell_grid):
            view.cancel_gesture()

    def cancel_preview(self):
        self._placement = None
        self._placement_hover = None
        self._browse_preview = False
        for view in (self.view, self.cell_grid):
            view.placement_active = False
            view.rotation_choices = None
            view.set_ghost()
            view.setCursor(studio_cursor("point"))
            view.update()

    def _update_ghost(self):
        if self._placement is not None:
            return
        defined = self.document is not None and self.current_sector in self.document.sdf.sectors
        model = self.model_combo.currentData() if self._browse_preview and defined else None
        for view in (self.view, self.cell_grid):
            view.set_ghost(model, self.selected_cells if model is not None else ())

    def _place_model(self, model):
        """Put a model into the selected cells; a whole-sector model takes the full sector."""
        sector = self.document.sdf.sectors[self.current_sector]
        whole = not sector.single and covers_sector(self.library, model)

        def apply():
            self.document.assign_model(self.current_sector, self.selected_cells, model,
                                       self.state.currentIndex(), whole=whole)
            if whole:
                self.selected_cells = {4}
                self.statusBar().showMessage("This model covers the whole sector: the sector is now one large cell.", 6000)
        self._run_edit(apply)

    def drop_model(self, model, cell):
        self.cancel_preview()
        if self.document is None or self.current_sector not in self.document.sdf.sectors:
            return
        if cell not in self.selected_cells:
            self.select_cell(cell)
        if cell in self.selected_cells:
            self._place_model(model)

    def _cell_activated(self, cell):
        """Double-click in a cell view places the model previewed from the Models tab."""
        if not self._browse_preview or self.tabs.currentWidget() is not self.models_page:
            return
        if cell not in self.selected_cells:
            self.select_cell(cell)
        self.apply_model()

    def _sector_activated(self, item):
        if self.document is not None and item.data(Qt.ItemDataRole.UserRole) not in self.document.sdf.sectors:
            self.add_sector()

    def select_cell_range(self, index):
        anchor = self._selection_anchor
        rows = sorted((anchor // 3, index // 3))
        cols = sorted((anchor % 3, index % 3))
        self._commit_sector_fields()
        self.selected_cells = {row * 3 + col for row in range(rows[0], rows[1] + 1)
                               for col in range(cols[0], cols[1] + 1)}
        self._load_sector_fields()

    def _prepare_menu(self, menu):
        for item in (menu, *menu.findChildren(QMenu)):
            item.installEventFilter(self)

    def eventFilter(self, watched, event):
        if isinstance(watched, QMenu) and event.type() == QEvent.Type.KeyPress:
            keys = {Qt.Key.Key_W: Qt.Key.Key_Up, Qt.Key.Key_S: Qt.Key.Key_Down,
                    Qt.Key.Key_A: Qt.Key.Key_Left, Qt.Key.Key_D: Qt.Key.Key_Right}
            if event.key() in keys:
                QApplication.sendEvent(watched, QKeyEvent(event.type(), keys[event.key()], event.modifiers()))
                return True
        return super().eventFilter(watched, event)

    def navigate_browser(self, key, modifiers):
        browser = self.models if self.tabs.currentWidget() is self.models_page else self.sectors
        browser.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, key, modifiers))

    def confirm_placement(self):
        if self._placement is not None:
            target = self._placement.get("target")
            if target is not None:
                self.finish_cell_placement(target)
        elif self.selected_cells:
            self.apply_model()

    def select_cell_box(self, cells, additive=False):
        self._commit_sector_fields()
        self.cancel_preview()
        self.selected_cells = (self.selected_cells | set(cells)) if additive else set(cells)
        self._load_sector_fields()

    def clear_cell_selection(self):
        self.select_cell_box(set())
        if not self.selected_cells:
            self.sectors.clearSelection()

    def start_cell_drag(self, origin):
        if origin not in self.selected_cells:
            self.select_cell(origin)
        if origin not in self.selected_cells:
            return
        self.start_cell_placement()
        if self._placement is not None:
            self._placement["origin"] = origin

    def start_cell_placement(self, checked=False):
        if not self.selected_cells or self.document is None or self.current_sector not in self.document.sdf.sectors:
            return
        self._commit_sector_fields()
        self.cancel_preview()
        self._placement = {"kind": "move", "number": self.current_sector,
                           "sources": tuple(sorted(self.selected_cells)), "target": None}
        for view in (self.view, self.cell_grid):
            view.placement_active = True
            view.setCursor(studio_cursor("move"))
        self.statusBar().showMessage("Move: drag or click a destination | Enter: confirm | Esc: cancel")

    def _arm_clipboard(self):
        if self._copied_cells is None or self.document is None or self.current_sector not in self.document.sdf.sectors:
            return
        self.cancel_preview()
        self._placement = {"kind": "paste", "clipboard": self._copied_cells,
                           "sources": self._copied_positions, "target": None}
        for view in (self.view, self.cell_grid):
            view.placement_active = True
            view.setCursor(studio_cursor("copy"))
        self.preview_cell_placement(min(self.selected_cells, default=4))
        self.statusBar().showMessage("Paste preview: point at a destination | Click / Enter / Ctrl+V: place | Esc: cancel")

    def _placement_cells(self, target):
        operation = self._placement
        sector = self.document.sdf.sectors[self.current_sector]
        sources = operation["sources"]
        if target is None or not sources:
            return ()
        if sector.single:
            return (4,) if len(sources) == 1 and target == 4 else ()
        if operation["kind"] == "paste" and len(sources) == 1 and target in self.selected_cells:
            return tuple(sorted(self.selected_cells))
        origin = operation.get("origin", min(sources))
        dr, dc = target // 3 - origin // 3, target % 3 - origin % 3
        cells = [(cell // 3 + dr, cell % 3 + dc) for cell in sources]
        return tuple(row * 3 + col for row, col in cells) if all(0 <= row < 3 and 0 <= col < 3 for row, col in cells) else ()

    def preview_cell_placement(self, target):
        if self._placement is None or target == self._placement_hover:
            return
        self._placement_hover = target
        operation = self._placement
        cells = self._placement_cells(target)
        operation["target"] = target if cells else None
        operation["cells"] = cells
        placements = []
        if cells:
            if operation["kind"] == "paste":
                clipboard = operation["clipboard"]
                for index, cell in enumerate(cells):
                    bid = clipboard.cells[index % len(clipboard.cells)]
                    mid = clipboard.tables.buildings[bid].state(self.state.currentIndex())
                    placements.append((clipboard.tables.models[mid].base, cell))
            else:
                sector = self.document.sdf.sectors[operation["number"]]
                for source, cell in zip(operation["sources"], cells):
                    bid = sector.subsects[0 if sector.single else source]
                    mid = self.document.sdf.buildings[bid].state(self.state.currentIndex())
                    placements.append((mid, cell))
        hidden = operation["sources"] if cells and operation["kind"] == "move" else ()
        for view in (self.view, self.cell_grid):
            view.set_ghost(cells=cells, placements=placements, hidden=hidden)
            view.setCursor(studio_cursor("forbidden") if not cells else
                           studio_cursor("move") if operation["kind"] == "move" else studio_cursor("copy"))

    def finish_cell_placement(self, target):
        if self._placement is None:
            return
        self._placement_hover = None
        self.preview_cell_placement(target)
        operation = self._placement
        cells = operation.get("cells", ())
        if not cells:
            self.statusBar().showMessage("The full selection must fit inside the sector.", 4000)
            return
        def apply():
            if operation["kind"] == "paste":
                self.document.paste_cells(self.current_sector, cells, operation["clipboard"])
            else:
                self.document.relocate_cells(operation["number"], operation["sources"], min(cells), move=True)
            self.selected_cells = set(cells)
        self._run_edit(apply)

    def cut_cells(self):
        if self.copy_cells(preview=False):
            self.clear_cells()
            self._arm_clipboard()

    def clear_cells(self):
        if self.selected_cells and self.document and self.current_sector in self.document.sdf.sectors:
            self._run_edit(lambda: self.document.assign_building(self.current_sector, self.selected_cells, 0))

    def change_cell_state(self, state):
        if self.selected_cells and self.document and self.current_sector in self.document.sdf.sectors:
            self._run_edit(lambda: self.document.shift_state(self.current_sector, self.selected_cells, state))

    def start_rotation(self):
        """Show an arrow for every way the selected cells can turn; Esc or a click elsewhere cancels."""
        if self.document is None or self.current_sector not in self.document.sdf.sectors:
            return
        self._commit_sector_fields()
        self.cancel_preview()
        if self.document.sdf.sectors[self.current_sector].single:
            self.statusBar().showMessage("A one large cell sector has nothing to rotate.", 5000)
            return
        choices = {0: True}
        for quarter in (1, 2, 3):
            mapping = self.document.rotated_cells(self.selected_cells, quarter)
            choices[quarter] = mapping is not None and any(a != b for a, b in mapping.items())
        if not any(choices[q] for q in (1, 2, 3)):
            self.statusBar().showMessage("Select two or more cells to rotate them.", 5000)
            return
        for view in (self.view, self.cell_grid):
            view.rotation_choices = choices
            view.update()
        self.statusBar().showMessage("Rotate: click the arrow of the new direction | Esc: cancel")

    def finish_rotation(self, quarters):
        if quarters == 0:
            self.cancel_preview()
            return
        def apply():
            self.selected_cells = self.document.rotate_cells(self.current_sector, self.selected_cells, quarters)
        self._run_edit(apply)

    def copy_sectors(self, *, cut=False):
        """Pick up the selected sectors; releasing the mouse on a slot places them."""
        if self.document is None or not self._commit_forms():
            return
        numbers = self._selected_sector_numbers()
        if not numbers:
            return
        self._copied_sectors = {"sectors": [deepcopy(self.document.sdf.sectors[n]) for n in numbers],
                                "cut": cut, "sources": numbers}
        self.sectors.start_carry(self.sectors.item(numbers[0]).icon(), len(numbers))
        verb = "Move" if cut else "Copy"
        self.statusBar().showMessage(f"{verb} {len(numbers)} sector(s): release the mouse on a slot to place | Esc: cancel")

    def cancel_sector_copy(self):
        if self.sectors.carrying:
            self.sectors.stop_carry()
            self._copied_sectors = None
            self.statusBar().clearMessage()

    def paste_sectors(self, target):
        clip = self._copied_sectors
        self.cancel_sector_copy()
        if clip is None or self.document is None:
            return
        def apply():
            placed = self.document.paste_sectors(clip["sectors"], target,
                                                 remove=clip["sources"] if clip["cut"] else ())
            self.current_sector = placed[0]
            self.selected_cells = {4}
            self.statusBar().showMessage(f"Placed {len(placed)} sector(s) from slot {placed[0]:03d}.", 5000)
        self._run_edit(apply, reset=True)

    def clear_sectors(self):
        if self.document is not None and self._selected_sector_numbers():
            self._run_edit(lambda: self.document.clear_sectors(self._selected_sector_numbers()))

    def wipe_sectors(self):
        numbers = self._selected_sector_numbers() if self.document is not None else []
        if not numbers:
            return
        choice = QMessageBox.question(self, "Wipe sector",
            f"Remove {len(numbers)} sector definition(s)? Their slots become free. "
            "Existing maps may still refer to these sector IDs.")
        if choice == QMessageBox.StandardButton.Yes:
            self._run_edit(lambda: self.document.wipe_sectors(numbers))

    def import_definitions(self):
        if self.document is None or not self._commit_forms():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Import native SDF definitions using this set's assets", "", "Set definitions (*.sdf *.SDF)")
        if not path:
            return
        targets = self._selected_slots()
        defined = [n for n in targets if n in self.document.sdf.sectors]
        if defined:
            choice = QMessageBox.question(self, "Import sector definition",
                f"Replace the {len(defined)} selected defined sector(s) with the imported definitions? "
                "Extra definitions go into free slots.")
            if choice != QMessageBox.StandardButton.Yes:
                return
        def apply():
            numbers = self.document.import_definitions(path, targets)
            self.current_sector = numbers[0]
            self.statusBar().showMessage(f"Imported {len(numbers)} sector(s) from slot {numbers[0]:03d}.", 6000)
        self._run_edit(apply, reset=True)

    def export_definition(self):
        if self.document is None or not self._commit_forms():
            return
        numbers = self._selected_sector_numbers()
        if not numbers:
            return
        name = f"Sector{numbers[0]:03d}.sdf" if len(numbers) == 1 else f"Set{self.document.number}-sectors.sdf"
        path, _ = QFileDialog.getSaveFileName(self, "Export native definitions (assets remain in the set)",
                                              name, "Set definitions (*.sdf)")
        if path:
            try:
                self.document.export_definitions(numbers, path)
                self.statusBar().showMessage(f"Exported {len(numbers)} definition(s) to {path}", 6000)
            except (OSError, ValueError) as exc:
                QMessageBox.warning(self, "Cannot export definitions", str(exc))

    def _sector_image(self, number, width):
        self.view.show_sector(self.library, number, state=self.state.currentIndex(), reset=True)
        image = self.view.capture_preview(width)
        if image.isNull():
            raise OSError(f"Sector {number} could not be rendered.")
        return image

    def _render_sectors(self, width, label):
        """Render every defined sector with the same framing, then restore the view."""
        numbers = sorted(self.document.sdf.sectors)
        images = []
        self.cancel_preview()
        self.view.show()
        try:
            with loading_resources(self, label):
                for number in numbers:
                    images.append((number, self._sector_image(number, width)))
                    QApplication.processEvents()
        finally:
            self._preview(reset=True)
        return images

    def export_preview(self):
        if self.library is None or self.current_sector not in self.document.sdf.sectors:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export current sector image", f"Sector{self.current_sector:03d}.png", "PNG image (*.png)")
        if path:
            try:
                write_static_image_atomic(self.view.capture_preview(), Path(path), "png")
                self.statusBar().showMessage(f"Saved sector image to {path}", 6000)
            except (OSError, ValueError, RuntimeError) as exc:
                QMessageBox.warning(self, "Cannot export image", str(exc))

    def export_all_images(self):
        if self.library is None or not self.document.sdf.sectors:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export all sector images",
                                              f"Set{self.document.number}-sectors.zip", "ZIP archive (*.zip)")
        if not path:
            return
        try:
            images = self._render_sectors(768, "Rendering sector images…")
            with tempfile.TemporaryDirectory(prefix="sector-images-") as directory:
                stage = Path(directory) / "images.zip"
                with ZipFile(stage, "w", ZIP_DEFLATED) as archive:
                    for number, image in images:
                        archive.writestr(f"Sector{number:03d}.png", png_bytes(image))
                with ZipFile(stage) as archive:
                    if archive.testzip() is not None:
                        raise OSError("ZIP verification failed.")
                commit_verified_files([(stage, Path(path))])
            self.statusBar().showMessage(f"Exported {len(images)} sector images to {path}", 8000)
        except (OSError, ValueError, RuntimeError) as exc:
            QMessageBox.warning(self, "Cannot export images", str(exc))

    def export_sheet(self):
        if self.library is None or not self.document.sdf.sectors:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export sector sheet",
                                              f"Set{self.document.number}-sheet.png", "PNG image (*.png)")
        if not path:
            return
        try:
            images = self._render_sectors(320, "Rendering sector sheet…")
            cells = [(f"{number:03d} [{number:02X}]", image) for number, image in images]
            sheet = compose_contact_sheet(cells, CONTACT_SHEET_BACKGROUND, columns=math.ceil(math.sqrt(len(cells))))
            write_static_image_atomic(sheet, Path(path), "png")
            self.statusBar().showMessage(f"Saved sheet with {len(cells)} sectors to {path}", 8000)
        except (OSError, ValueError, RuntimeError) as exc:
            QMessageBox.warning(self, "Cannot export sheet", str(exc))

    def import_custom_textures(self):
        if self.document is None or self.assets is None:
            return
        if self.document.number not in LOOSE_SETS:
            QMessageBox.information(self, "Custom textures",
                "The game reads custom set textures only for Set1 to Set7. Studio will still show them.")
        folder = QFileDialog.getExistingDirectory(self, "Choose the folder with the custom textures (ILBM or PNG, vanilla names)")
        if not folder:
            return
        try:
            written, skipped = import_textures(self.document.folder, self.assets.archive, [folder])
        except OSError as exc:
            QMessageBox.warning(self, "Cannot import textures", str(exc))
            return
        if written:
            self.reload_assets()
        lines = [f"Imported {len(written)} texture(s) into {Path(self.document.folder) / 'Loose' / 'ILBM'}."]
        if skipped:
            lines += ["", f"Skipped {len(skipped)}:", *skipped[:15]]
            if len(skipped) > 15:
                lines.append(f"… and {len(skipped) - 15} more")
        QMessageBox.information(self, "Custom textures", "\n".join(lines))

    def _choose_installed(self, index):
        if index < 0:
            return
        number = self.sets.itemData(index)
        path = self.sets.itemData(index, Qt.ItemDataRole.UserRole + 1) or bootstrap.set_dir(number)
        if not self.open_set(path) and self.document is not None:
            self._sync_set_combo()

    def _sync_set_combo(self):
        self.sets.blockSignals(True)
        number = self.document.number
        index = self.sets.findData(number)
        if index < 0:
            self.sets.addItem(f"Set {number} (opened folder)", number)
            index = self.sets.count() - 1
        self.sets.setItemData(index, str(self.document.folder), Qt.ItemDataRole.UserRole + 1)
        self.sets.setCurrentIndex(index)
        self.sets.blockSignals(False)

    def choose_set(self):
        path = QFileDialog.getExistingDirectory(self, "Open Set folder", str(self.document.folder) if self.document else "")
        if path:
            self.open_set(path)

    def _guard_unsaved(self):
        if self.document is None or not (self.document.dirty or self._pending_model() or self._pending_sector_fields()):
            return True
        choice = QMessageBox.question(self, "Unsaved set", "Overwrite this set with your changes?",
                                      QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel)
        if choice == QMessageBox.StandardButton.Save:
            return self.save()
        return choice == QMessageBox.StandardButton.Discard

    def open_set(self, path, *, check_unsaved=True):
        if check_unsaved and not self._guard_unsaved():
            return False
        try:
            with loading_resources(self, "Loading sector models and set materials…"):
                document = SetDocument.open(path)
                assets = SetAssets(document.number, set_dir=document.folder).load()
                assets.sdf = document.sdf
                if assets.family is None:
                    raise ValueError("Objects/SET.BAS is required to preview this set.")
                library = SectorMeshLibrary(assets, set_only=True)
            self.document, self.assets, self.library = document, assets, library
            self._sync_set_combo()
            document.assets = assets
            document.embedded_names = [obj.base_object.name for obj in assets.family.root_object.kids[1].kids]
            self.cancel_interaction()
            self._copied_cells = None
            self._model_titles = None
            self._editing_model_id = self._model_snapshot = None
            self._sector_fields_number = None
            self.previews.reset(library)
            self.current_sector = min(document.sdf.sectors, default=0)
            self.path_label.setText(f"Set {document.number} · Sectors")
            self.path_label.setToolTip(str(document.folder))
            self._load_asset_names()
            self._refresh(reset=True)
            return True
        except (OSError, ValueError, RuntimeError) as exc:
            QMessageBox.warning(self, "Cannot open set", str(exc))
            return False

    def _load_asset_names(self):
        names = {getattr(obj.base_object, "name", "") for obj in self.assets.family.all_objects()} - {""}
        names = sorted({name if name.casefold().endswith(".base") else name + ".base" for name in names}, key=str.casefold)
        names = set(names)
        names.update(path.with_suffix(".base").name for path in self.assets._index_loose().values()
                     if path.is_relative_to(self.assets.set_dir))
        names = sorted(set(names) | {m.base for m in self.document.sdf.models}, key=str.casefold)
        self.base.clear()
        self.base.addItems(names)
        skeletons = {m.sklt for m in self.document.sdf.models}
        if self.assets.archive:
            skeletons.update(r.resource_name for r in self.assets.archive.resources if r.class_id.lower() == "sklt.class")
        self.sklt.clear()
        self.sklt.addItems(sorted(skeletons, key=str.casefold))

    def _bind_tables(self):
        self.assets.sdf = self.document.sdf
        self.library.resolver = SectorResolver(self.document.sdf)
        self.library._meshes.clear()

    def _refresh(self, *, reset=False):
        if self.document is None:
            return
        self._syncing = True
        self._bind_tables()
        sdf = self.document.sdf
        selected_sectors = {item.data(Qt.ItemDataRole.UserRole) for item in self.sectors.selectedItems()}
        self.sectors.blockSignals(True)
        self.sectors.clear()
        for number in range(256):
            sector = sdf.sectors.get(number)
            item = QListWidgetItem()
            detail = (sector.comment or ("One large cell" if sector.single else "3 × 3 cells")) if sector else "Free slot · Double-click to create"
            if number >= 248:
                detail += "\nMap border"
            color = (75, 195, 220) if sector else (130, 135, 145)
            set_card(item, f"Sector {number:03d} [{number:02X}]", detail, color, number)
            item.setData(Qt.ItemDataRole.UserRole, number)
            self.sectors.addItem(item)
        self.sectors.setCurrentRow(self.current_sector or 0, QItemSelectionModel.SelectionFlag.NoUpdate)
        for number in selected_sectors or {self.current_sector or 0}:
            self.sectors.item(number).setSelected(True)
        self.sectors.blockSignals(False)
        self._filter_sectors()
        selected_model = self.model_combo.currentData()
        model_titles = tuple(f"{m.id:03d}  {m.base}" for m in sdf.models)
        model_signature = tuple((title, m.comment) for title, m in zip(model_titles, sdf.models))
        if model_signature != self._model_titles:
            self.model_combo.blockSignals(True)
            self.models.blockSignals(True)
            self.models.clear()
            self.model_combo.clear()
            for combo in self.state_models:
                combo.clear()
            for model, title in zip(sdf.models, model_titles):
                item = QListWidgetItem()
                set_card(item, f"{model.id:03d}  {model.base}", model.comment or "Set model",
                         (240, 153, 75), model.id)
                item.setData(Qt.ItemDataRole.UserRole, model.id)
                self.models.addItem(item)
                self.model_combo.addItem(title, model.id)
                for combo in self.state_models:
                    combo.addItem(title, model.id)
            model_index = max(0, self.model_combo.findData(selected_model))
            self.model_combo.setCurrentIndex(model_index)
            self.models.setCurrentRow(model_index)
            self.models.blockSignals(False)
            self.model_combo.blockSignals(False)
            self._model_titles = model_signature
        self.counts.setText(f"{len(sdf.sectors)} defined · {256-len(sdf.sectors)} free\nModels {len(sdf.models)}/{MODEL_LIMIT} · Buildings {len(sdf.buildings)}/{BUILDING_LIMIT}")
        self._syncing = False
        self._filter_models()
        self._load_sector_fields()
        self._load_model_fields()
        self._update_model_usage()
        self._update_issues()
        self._update_sector_actions()
        self._preview(reset=reset)
        for key, stack in (("undo", self.document._undo), ("redo", self.document._redo)):
            self.actions[key].setEnabled(bool(stack))
            self.actions[key].setToolTip(f"{key.title()}: {stack[-1][0]}" if stack else key.title())
        self._model_form_changed()
        self.preview_timer.start()

    def _filter_sectors(self):
        query = self.filter.text().casefold()
        mode = self.slot_filter.currentIndex()
        usage = self.document.sectors_using_model(self.model_combo.currentData()) if self.document and mode == 3 else set()
        for row in range(self.sectors.count()):
            item = self.sectors.item(row)
            defined = self.document is not None and row in self.document.sdf.sectors
            text = f"{item.text()} {item.data(DETAIL_ROLE) or ''}".casefold()
            item.setHidden(query not in text or (mode == 1 and not defined) or (mode == 2 and defined)
                           or (mode == 3 and row not in usage))
        if hasattr(self, "preview_timer"):
            self.preview_timer.start()

    def _filter_models(self):
        query = self.model_search.text().casefold()
        for row in range(self.models.count()):
            item = self.models.item(row)
            item.setHidden(query not in f"{item.text()} {item.data(DETAIL_ROLE) or ''}".casefold())
        if hasattr(self, "preview_timer"):
            self.preview_timer.start()

    def _request_previews(self):
        if self.library is None:
            return
        sdf = self.document.sdf
        requests = []
        for kind, browser in (("sector", self.sectors), ("model", self.models)):
            # Render at the tile's real pixel size, so zoomed tiles stay sharp.
            size = min(256, max(48, round(browser.image_side * browser.devicePixelRatioF())))
            for item in visible_items(browser):
                number = item.data(Qt.ItemDataRole.UserRole)
                if kind == "sector":
                    sector = sdf.sectors.get(number)
                    if sector is None:
                        continue
                    bases = []
                    for bid in sector.subsects:
                        if 0 <= bid < len(sdf.buildings):
                            mid = sdf.buildings[bid].state(self.state.currentIndex())
                            bases.append(sdf.models[mid].base if 0 <= mid < len(sdf.models) else "")
                    signature = (sector.kind, tuple(bases))
                else:
                    signature = sdf.models[number].base
                key = (self.previews.generation, kind, number, signature, size)
                if item.data(PREVIEW_ROLE) != key:
                    if item.data(PREVIEW_ROLE) is None or item.data(PREVIEW_ROLE)[:4] != key[:4]:
                        item.setIcon(QIcon())
                    item.setData(PREVIEW_ROLE, key)
                requests.append((key, kind, number, self.state.currentIndex(), size))
        self.previews.request(requests)

    def _card_ready(self, key, icon):
        browser = self.sectors if key[1] == "sector" else self.models
        item = browser.item(key[2])
        if item is not None and item.data(PREVIEW_ROLE) == key:
            item.setIcon(icon)

    def _sector_selected(self, item, previous=None):
        if item is None or self._syncing:
            return
        number = item.data(Qt.ItemDataRole.UserRole)
        self._commit_sector_fields()
        self.cancel_preview()
        self.current_sector = number
        self.sectors.blockSignals(True)
        self.sectors.setCurrentRow(number, QItemSelectionModel.SelectionFlag.NoUpdate)
        self.sectors.blockSignals(False)
        self.selected_cells = {4}
        self._selection_anchor = 4
        self._load_sector_fields()
        self._update_sector_actions()
        self._preview(reset=True)

    def _load_sector_fields(self):
        if self.document is None:
            return
        sector = self.document.sdf.sectors.get(self.current_sector)
        pending_description = self._pending_sector_fields()
        if sector and sector.single and self.selected_cells:
            self.selected_cells = {4}
        self._syncing = True
        layout = "One large cell" if sector and sector.single else "3 × 3 cells"
        self.sector_title.setText(f"Sector {self.current_sector:03d} [{self.current_sector:02X}] · {layout}"
                                  if sector else f"Slot {self.current_sector:03d} is free")
        for field in (self.layout_mode, self.ground, self.symbol, self.description):
            field.setEnabled(sector is not None)
        if sector:
            self.layout_mode.setCurrentIndex(int(sector.single))
            self.ground.setValue(sector.ground)
            self.symbol.setValue(sector.symbol)
            if not pending_description:
                self.description.setText(sector.comment)
                self._description_snapshot = sector.comment
        else:
            self.description.clear()
        if sector and self.selected_cells:
            bid = sector.subsects[0 if sector.single else min(self.selected_cells, default=4)]
            if 0 <= bid < len(self.document.sdf.buildings):
                row = self.document.sdf.buildings[bid]
                for state, combo in enumerate(self.state_models):
                    combo.setCurrentIndex(combo.findData(row.state(state)))
                self.destructible.setChecked(bool(row.destructible))
        for field in (*self.state_models, self.destructible):
            field.setEnabled(sector is not None and bool(self.selected_cells))
        self._sector_fields_number = self.current_sector
        self._syncing = False
        details = {}
        if sector:
            for cell in ({4} if sector.single else range(9)):
                bid = sector.subsects[0 if sector.single else cell]
                if 0 <= bid < len(self.document.sdf.buildings):
                    building = self.document.sdf.buildings[bid]
                    states = []
                    for state in range(4):
                        mid = building.state(state)
                        name = (self.document.sdf.models[mid].base
                                if 0 <= mid < len(self.document.sdf.models) else "Missing model")
                        states.append(f"{STATES[state]}: {mid} | {name}")
                    details[cell] = f"Cell {cell + 1} | Building {bid}\n" + "\n".join(states)
        occupied = {4 if sector.single else cell for cell, bid in enumerate(sector.subsects) if bid != 0} if sector else set()
        for view in (self.view, self.cell_grid):
            view.cell_details = details
            view.occupied_cells = occupied
            view.selected_cells = set(self.selected_cells)
            view.update()
        self._update_cell_actions()
        if self.library is not None and sector:
            self.ground_preview.set_ground(self.library, sector.ground)
            self.symbol_preview.set_symbol(self.library, sector.symbol)
        self._update_ghost()

    def _update_cell_actions(self):
        sector = self.document.sdf.sectors.get(self.current_sector) if self.document else None
        defined = sector is not None
        selected = sorted(self.selected_cells)
        bids = [sector.subsects[0 if sector.single else c] for c in selected] if sector else []
        valid = defined and bool(selected) and all(0 <= bid < len(self.document.sdf.buildings) for bid in bids)
        for key in ("copy", "cut", "clear", "rotate"):
            self.actions[key].setEnabled(valid)
        self.state_menu.setEnabled(valid)
        self.actions["select_all"].setEnabled(defined)
        self.actions["paste"].setEnabled(defined and self._copied_cells is not None)
        self.apply_model_button.setEnabled(defined and bool(selected))
        if not sector:
            self.selection_info.setText("Create a sector in this free slot to edit its cells.")
            self.model_target.setText(f"Slot {self.current_sector:03d} is free. Create a sector before applying models.")
            return
        if not selected:
            self.selection_info.setText("No cells selected · Click a cell or drag a selection box")
            self.model_target.setText(f"Sector {self.current_sector:03d} · No cells selected")
            return
        if sector.single:
            summary = "The whole sector is one large cell"
        elif len(selected) == 1:
            summary = f"Cell {selected[0]+1} selected"
        else:
            summary = f"{len(selected)} cells selected" + (" · Different content" if len(set(bids)) > 1 else "")
        self.selection_info.setText(summary)
        self.model_target.setText(f"Sector {self.current_sector:03d} · {summary} · Double-click a cell to place the model")

    def _pending_sector_fields(self):
        sector = self.document.sdf.sectors.get(self.current_sector) if self.document else None
        return (sector is not None and self._sector_fields_number == self.current_sector
                and self._description_snapshot is not None
                and self.description.text() != self._description_snapshot)

    def _commit_sector_fields(self):
        if not self._pending_sector_fields():
            return
        number = self.current_sector
        updated = replace(self.document.sdf.sectors[number], comment=self.description.text())
        self.document.change("Sector description", lambda: self.document.sdf.sectors.__setitem__(number, updated))
        self._description_snapshot = self.description.text()

    def _commit_forms(self):
        pending = self._pending_sector_fields() or self._pending_model()
        self._commit_sector_fields()
        if not self._commit_model_form():
            return False
        if pending:
            self._refresh()
        return True

    def _sector_fields_changed(self):
        if self._syncing or self.document is None or self.current_sector not in self.document.sdf.sectors:
            return
        sector = self.document.sdf.sectors[self.current_sector]
        ground, symbol = self.ground.value(), self.symbol.value()
        if (ground, symbol) == (sector.ground, sector.symbol):
            self._commit_sector_fields()
            self._refresh()
            return
        self._commit_sector_fields()
        numbers = self._selected_sector_numbers()
        self._run_edit(lambda: self.document.set_sector_fields(
            numbers, ground=None if ground == sector.ground else ground,
            symbol=None if symbol == sector.symbol else symbol))

    def _layout_changed(self, index):
        if self.document and self.current_sector in self.document.sdf.sectors:
            self._commit_sector_fields()
            self.document.set_layout(self.current_sector, bool(index))
            self.selected_cells = {4}
            self._refresh(reset=True)

    def _state_model_changed(self, state):
        """Apply a cell content change at once; Intact also fills the later states."""
        if self._syncing or self.document is None or self.current_sector not in self.document.sdf.sectors:
            return
        if not self.selected_cells:
            return
        if state is None:
            states = tuple(combo.currentData() for combo in self.state_models)
            self._run_edit(lambda: self.document.assign_states(
                self.current_sector, self.selected_cells, states, int(self.destructible.isChecked())))
            return
        model = self.state_models[state].currentData()
        if model is None:
            return
        self._run_edit(lambda: self.document.assign_model(self.current_sector, self.selected_cells, model, state))

    def select_cell(self, index, additive=False):
        selected = self.selected_cells.symmetric_difference({index}) if additive else {index}
        self._commit_sector_fields()
        if self._placement is not None:
            self.cancel_preview()
        self._selection_anchor = index
        self.selected_cells = selected
        self._load_sector_fields()

    def _select_all(self):
        sector = self.document.sdf.sectors.get(self.current_sector) if self.document else None
        self._commit_sector_fields()
        self.selected_cells = {4} if sector is None or sector.single else set(range(9))
        self._load_sector_fields()

    def _model_selected(self, item, previous=None):
        self._browse_preview = True
        if item and not self._syncing:
            self.model_combo.setCurrentIndex(self.model_combo.findData(item.data(Qt.ItemDataRole.UserRole)))
            self._update_ghost()

    def _load_model_fields(self):
        if self.document is None or self._syncing:
            return
        mid = self.model_combo.currentData()
        if mid is None:
            return
        changed = mid != self._editing_model_id
        if self._pending_model():
            if mid == self._editing_model_id:
                return
            if not self._resolve_model_form():
                self.model_combo.blockSignals(True)
                self.model_combo.setCurrentIndex(self.model_combo.findData(self._editing_model_id))
                self.model_combo.blockSignals(False)
                self.models.blockSignals(True)
                self.models.setCurrentRow(self._editing_model_id)
                self.models.blockSignals(False)
                return
        self._syncing = True
        self._editing_model_id = mid
        model = self.document.sdf.models[mid]
        self.base.setCurrentText(model.base)
        self.sklt.setCurrentText(model.sklt)
        # Legacy field and building symbol are kept as they are; they are not edited here.
        self._model_hidden_numbers = model.numbers[:2]
        self.shield.setValue(max(0, min(100, model.numbers[2])))
        self.collision_mode.setCurrentIndex(max(0, min(2, model.numbers[3])))
        self.model_comment.setText(model.comment)
        self.effects.setPlainText("\n".join(" ".join(map(str, group)) for group in model.effects))
        self._update_model_usage()
        self.models.blockSignals(True)
        self.models.setCurrentRow(mid)
        self.models.blockSignals(False)
        self._model_snapshot = self._model_form_values()
        self._syncing = False
        self._model_form_changed()
        if self.preview_mode.currentIndex() == 1:
            self._preview(reset=changed)

    def _model_numbers(self):
        return (*self._model_hidden_numbers, self.shield.value(), self.collision_mode.currentIndex())

    def _model_form_values(self):
        return (self.base.currentText(), self.sklt.currentText(), self._model_numbers(),
                self.effects.toPlainText(), self.model_comment.text())

    def _pending_model(self):
        return self._model_snapshot is not None and self._model_snapshot != self._model_form_values()

    def _model_form_changed(self):
        if self._syncing or self.document is None:
            return
        pending = self._pending_model()
        self.update_model_button.setEnabled(pending)
        dirty = self.document.dirty or pending or self._pending_sector_fields()
        self.setWindowTitle(f"{'* ' if dirty else ''}Set {self.document.number} · {self.document.folder}"
                            " - OpenNeoUA Studio - Sector Editor")
        if self.slot_filter.currentIndex() == 3:
            self._filter_sectors()

    def find_model_usage(self):
        self.filter.clear()
        self.slot_filter.setCurrentIndex(3)
        self._filter_sectors()
        count = len(self.document.sectors_using_model(self.model_combo.currentData()))
        self.statusBar().showMessage(f"{count} sector(s) use this model in one or more damage states.", 6000)

    def _update_model_usage(self):
        mid = self.model_combo.currentData()
        if self.document is None or mid is None:
            return
        used = len(self.document.sectors_using_model(mid))
        self.model_usage.setText(f"Model {mid} is used in {used} sector(s).")

    def _resolve_model_form(self):
        if not self._pending_model():
            return True
        choice = QMessageBox.question(self, "Model changes", f"Apply changes to model {self._editing_model_id}?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel)
        if choice == QMessageBox.StandardButton.Save:
            if not self._commit_model_form():
                return False
            self._refresh()
            return True
        if choice == QMessageBox.StandardButton.Discard:
            self._model_snapshot = None
            self._load_model_fields()
            return True
        return False

    def _commit_model_form(self):
        if not self._pending_model():
            return True
        try:
            effects = []
            for line in self.effects.toPlainText().splitlines():
                if not line.strip():
                    continue
                values = tuple(map(int, line.split()))
                if len(values) != 4:
                    raise ValueError("Each destruction effect needs exactly four integers.")
                effects.append(values)
            mid = self._editing_model_id
            model = replace(self.document.sdf.models[mid], base=self.base.currentText().strip(),
                            sklt=self.sklt.currentText().strip(), numbers=self._model_numbers(),
                            effects=tuple(effects), comment=self.model_comment.text())
            def apply():
                self.document.sdf.models[mid] = model
                problems = table_issues(self.document.sdf)
                if problems:
                    raise ValueError("\n".join(problems[:20]))
            self.document.change("Model definition", apply)
            self._model_snapshot = self._model_form_values()
            self._bind_tables()
            self._model_form_changed()
            self.preview_timer.start()
            self._preview()
            return True
        except (ValueError, IndexError) as exc:
            self._bind_tables()
            QMessageBox.warning(self, "Cannot update model", str(exc))
            return False

    def _preview(self, *args, reset=False):
        if self.library is None:
            return
        self.preview_timer.start()
        mid = self.model_combo.currentData() if self.preview_mode.currentIndex() == 1 else None
        if self.current_sector not in self.document.sdf.sectors and mid is None:
            self.view.hide()
            self.cell_grid.hide()
            self.free_hint.show()
            self.preview_status.clear()
            return
        self.free_hint.hide()
        self.view.show()
        defined = self.current_sector in self.document.sdf.sectors
        self.cell_grid.setVisible(defined)
        try:
            if defined:
                self.cell_grid.show_sector(self.library, self.current_sector, state=self.state.currentIndex())
            self.view.show_sector(self.library, self.current_sector or 0,
                                  state=self.state.currentIndex(), model=mid, reset=reset)
            mesh = self.view.lib.mesh(self.current_sector or 0)
            messages = mesh.missing + (mesh.resolved.warnings if mesh.resolved else [])
            self.preview_status.setText("Missing: " + ", ".join(messages) if messages else
                                        f"{len(mesh.faces)} faces | {self.view.renderer_name} | WASD: browse | Enter: apply | Drag selected: move | Drag background: select | Right click: tools")
            if self._placement is not None:
                target = self._placement.get("target")
                self._placement_hover = None
                self.preview_cell_placement(target)
            else:
                self._update_ghost()
        except (ValueError, OSError, RuntimeError, IndexError) as exc:
            self.preview_status.setText(f"Preview unavailable: {exc}")

    def _run_edit(self, callback, *, reset=False):
        if self.document is None:
            return
        self._commit_sector_fields()
        self.cancel_preview()
        try:
            result = callback()
            self._refresh(reset=reset)
            return result
        except (ValueError, OSError, IndexError) as exc:
            self._refresh()
            QMessageBox.warning(self, "Cannot edit set", str(exc))

    def add_sector(self):
        def add():
            number = self.current_sector if self.current_sector not in self.document.sdf.sectors else None
            self.current_sector = self.document.add_sector(number=number)
        self._run_edit(add, reset=True)

    def apply_model(self):
        if not self.selected_cells:
            return
        if self.document and self.current_sector in self.document.sdf.sectors:
            self._place_model(self.model_combo.currentData())

    def update_model(self):
        if self.document is not None and self._commit_model_form():
            self._refresh()

    def add_model(self, checked=False, *, duplicate=False):
        if self.document is None or not self._commit_model_form():
            return
        result = self._run_edit(lambda: self.document.add_model(self.base.currentText().strip(), self.sklt.currentText().strip(),
                                source=self.model_combo.currentData() if duplicate else None))
        if result is not None:
            self.model_combo.setCurrentIndex(self.model_combo.findData(result))

    def copy_cells(self, checked=False, *, preview=True):
        if self.selected_cells and self.document and self.current_sector in self.document.sdf.sectors:
            self._commit_sector_fields()
            try:
                self._copied_cells = self.document.copy_cells(self.current_sector, self.selected_cells)
                self._copied_positions = tuple(sorted(self.selected_cells))
                self._update_cell_actions()
                if preview:
                    self._arm_clipboard()
                return True
            except (ValueError, IndexError) as exc:
                QMessageBox.warning(self, "Cannot copy cells", str(exc))

    def paste_cells(self):
        if self._placement is not None and self._placement["kind"] == "paste":
            self.confirm_placement()
        else:
            self._arm_clipboard()

    def show_validation(self, *args):
        dialog = QDialog(self)
        dialog.setWindowTitle("Set Validation")
        dialog.resize(620, 360)
        layout = QVBoxLayout(dialog)
        report = QPlainTextEdit()
        report.setReadOnly(True)
        report.setPlainText(self.issues.toPlainText())
        layout.addWidget(report)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def undo(self):
        if not self._resolve_model_form():
            return
        self._run_edit(self.document.undo if self.document else lambda: None)

    def redo(self):
        if not self._resolve_model_form():
            return
        self._run_edit(self.document.redo if self.document else lambda: None)

    def reset_set(self):
        """Empty the set or go back to the last saved state; both stay undoable."""
        if self.document is None or not self._resolve_model_form():
            return
        box = QMessageBox(QMessageBox.Icon.Warning, "Reset Set",
                          "Warning: this changes the whole set.\n\n"
                          "Remove all sectors: every sector slot becomes free (models and buildings stay).\n"
                          "Restore last saved: discard every change since the set was opened or overwritten.\n\n"
                          "Undo can still bring the set back.", parent=self)
        empty = box.addButton("Remove all sectors", QMessageBox.ButtonRole.DestructiveRole)
        saved = box.addButton("Restore last saved", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() not in (empty, saved):
            return
        self._model_snapshot = None
        self._run_edit(lambda: self.document.reset_sectors(saved=box.clickedButton() is saved), reset=True)

    def _update_issues(self):
        problems = table_issues(self.document.sdf)
        problems.extend(self.document.resource_issues())
        warnings = list(self.document.sdf.warnings)
        if len(self.document.sdf.models) > 256 or len(self.document.sdf.buildings) > 256:
            warnings.append("Extended model/building tables require the updated OpenNeoUA engine (4096 slots). Sector IDs and map/save formats remain unchanged.")
        for number, sector in self.document.sdf.sectors.items():
            if number >= 248:
                continue
            warnings.extend(f"Sector {number}: {message}" for message in self.library.resolver.resolve_typ(number).warnings)
        count = len(problems) + len(warnings)
        self.validation_status.setText(f'<a href="report">Set issues: {count}</a>')
        self.validation_status.setVisible(bool(count))
        self.validation_action.setText(f"Validation Report ({count})…" if count else "Validation Report…")
        self.issues.setPlainText("\n".join([*(f"ERROR: {p}" for p in problems), *warnings]) or
                                "Native table references and model resources are valid.\n\nSector IDs: 0–255. Slots 248–255 are used for map borders.\n\nOverwrite Set writes SET.SDF and the native LEGO visual overrides when needed. Export Set includes the complete set resources.\n\nRotate moves the selected cells; each model keeps its own orientation.")

    def save(self):
        if self.document is None or not self._commit_forms():
            return False
        try:
            self.document.save()
            self._refresh()
            self.statusBar().showMessage("SET.SDF overwritten and reloaded successfully.", 5000)
            return True
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Cannot overwrite set", str(exc))
            return False

    def export_set(self):
        if self.document is None or not self._commit_forms():
            return
        dialog = ExportSetDialog(self.document, self)
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.target() is None:
            return
        target = dialog.target()
        try:
            if dialog.archive.isChecked():
                with loading_resources(self, "Packaging and verifying complete set…"):
                    count = self.document.export_zip(target)
                self.statusBar().showMessage(f"Exported {count} files to {target}", 8000)
            else:
                with loading_resources(self, "Copying complete set…"):
                    copied = self.document.copy_to(target)
                self.open_set(copied.folder, check_unsaved=False)
                self.statusBar().showMessage(f"Complete set exported to {copied.folder}", 8000)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Cannot export set", str(exc))

    def new_set(self):
        if not self._guard_unsaved():
            return
        dialog = NewSetDialog(self.document.folder if self.document else None, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            with loading_resources(self, "Creating independent set resources…"):
                document = SetDocument.create(dialog.paths["Destination"].text(),
                    {name: dialog.paths[name].text() for name in SET_FOLDERS}, keep_sectors=dialog.keep.isChecked())
            self.open_set(document.folder, check_unsaved=False)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Cannot create set", str(exc))

    def closeEvent(self, event):
        if not self._guard_unsaved():
            event.ignore()
            return
        self.view.stop_rendering()
        self.cell_grid.stop_rendering()
        self.previews.stop()
        super().closeEvent(event)
