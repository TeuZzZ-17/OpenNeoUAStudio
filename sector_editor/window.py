"""Sector Editor workspace, sharing the Studio shell, resource loaders and renderer."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QTimer, Qt, QItemSelectionModel, QEvent
from PySide6.QtGui import QAction, QIcon, QKeySequence, QKeyEvent
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QAbstractItemView, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton,
    QSizePolicy, QSpinBox, QSplitter, QTabWidget, QVBoxLayout, QWidget,
)

from map_editor import bootstrap
from map_editor.core.asset_bridge import SetAssets
from map_editor.core.sector_resolver import SectorResolver
from map_editor.render.sector_mesh import SectorMeshLibrary
from map_editor.ui.preview_cards import DETAIL_ROLE, set_card
from studio.rendering.asset_viewport import VIEW_PRESETS, VIEW_PRESET_ANGLES
from studio.ui.tabs import PaletteTabStrip
from studio.ui.widgets import (
    configure_operation_status_bar, install_standard_file_menu_tail,
    loading_resources, use_standard_list_style, ViewportWidthScrollArea,
)
from .document import SetDocument, SET_FOLDERS, table_issues, MODEL_LIMIT, BUILDING_LIMIT
from .viewport import SectorViewport
from .interactions import KeyboardList, ModelList
from .visual_aids import GroundPreview, SymbolPreview
from .previews import CardPreviews, visible_items

STATES = ("Intact", "Damaged", "Heavily damaged", "Destroyed")
PREVIEW_ROLE = int(Qt.ItemDataRole.UserRole) + 10


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
        self._placement = None
        self._browse_preview = False
        self._selection_anchor = 4
        self._sampled_ground = None
        self._placement_hover = None
        self._model_titles = None
        self._building_titles = None
        self._editing_model_id = None
        self._model_snapshot = None
        self._state_snapshot = None
        self._state_target = None
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

    def _build_actions(self):
        file = self.menuBar().addMenu("File")
        self.actions = {}
        toolbar = self.addToolBar("Set")
        toolbar.setMovable(False)
        for key, text, shortcut, callback in (
            ("new", "New Set…", QKeySequence.StandardKey.New, self.new_set),
            ("open", "Open Set…", QKeySequence.StandardKey.Open, self.choose_set),
            ("save", "Save", QKeySequence.StandardKey.Save, self.save),
            ("save_as", "Save Set As…", QKeySequence.StandardKey.SaveAs, self.save_as),
            ("zip", "Export Complete Set as ZIP…", None, self.export_zip),
        ):
            action = QAction(text, self)
            if shortcut is not None:
                action.setShortcut(shortcut)
            action.triggered.connect(callback)
            file.addAction(action)
            toolbar.addAction(action)
            self.actions[key] = action
        file.addSeparator()
        file.addAction("Import sector definitions…", self.import_definitions)
        file.addAction("Export selected sector definition…", self.export_definition)
        file.addAction("Export all sector definitions…", lambda: self.export_definition(all_sectors=True))
        file.addAction("Export current view as PNG…", self.export_preview)
        install_standard_file_menu_tail(file, self)
        edit = self.menuBar().addMenu("Edit")
        for key, text, shortcut, callback in (
            ("undo", "Undo", QKeySequence.StandardKey.Undo, self.undo),
            ("redo", "Redo", QKeySequence.StandardKey.Redo, self.redo),
            ("copy", "Copy selected cells", QKeySequence.StandardKey.Copy, self.copy_cells),
            ("paste", "Paste cells", QKeySequence.StandardKey.Paste, self.paste_cells),
            ("cut", "Cut selected cells", QKeySequence.StandardKey.Cut, self.cut_cells),
            ("move", "Move selected cells…", None, self.start_cell_placement),
        ):
            action = QAction(text, self)
            if shortcut is not None:
                action.setShortcut(shortcut)
            action.triggered.connect(callback)
            edit.addAction(action)
            self.actions[key] = action
        edit.addSeparator()
        for key, text, shortcut, callback in (
            ("select_all", "Select all cells", QKeySequence.StandardKey.SelectAll, self._select_all),
            ("select_matching", "Select matching buildings", None, self.select_matching_cells),
            ("inspect", "Inspect assigned model", None, self.inspect_cell_model),
            ("choose_model", "Choose replacement model", None, self.choose_cell_model),
            ("clear", "Clear selected cells", "Delete", lambda: self._assign_building(0)),
        ):
            action = QAction(text, self)
            if shortcut is not None:
                action.setShortcut(shortcut)
            action.triggered.connect(callback)
            edit.addAction(action)
            self.actions[key] = action
        cancel = QAction("Cancel preview / placement", self)
        cancel.setShortcut("Escape")
        cancel.triggered.connect(self.cancel_interaction)
        self.addAction(cancel)
        self.actions["cancel_preview"] = cancel
        toolbar.addSeparator()
        toolbar.addAction(self.actions["undo"])
        toolbar.addAction(self.actions["redo"])
        toolbar.addSeparator()
        toolbar.addWidget(QLabel("View "))
        self.view_preset = QComboBox()
        self.view_preset.addItems(VIEW_PRESETS)
        self.view_preset.activated.connect(self._change_view)
        toolbar.addWidget(self.view_preset)
        toolbar.addAction("Reset View", self.view.reset_camera)
        sector = self.menuBar().addMenu("Sector")
        for text, callback in (("New in first free slot", self.add_sector),
                               ("Duplicate to free slot", self.duplicate_sector),
                               ("Remove definition / free slot", self.remove_sector),
                               ("Copy to another slot…", self.copy_sector_to),
                               ("Rotate cells clockwise", lambda: self.transform_cells("rotate")),
                               ("Mirror cells horizontally", lambda: self.transform_cells("horizontal")),
                               ("Mirror cells vertically", lambda: self.transform_cells("vertical")),
                               ("Restore saved sector layout", self.restore_sector),
                               ("Replace building in all sectors…", self.replace_building)):
            sector.addAction(text, callback)
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
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Search ID, hex or description…")
        self.filter.textChanged.connect(self._filter_sectors)
        layout.addWidget(self.filter)
        self.slot_filter = QComboBox()
        self.slot_filter.addItems(("All slots", "Defined sectors", "Free slots", "Using selected model"))
        self.slot_filter.currentIndexChanged.connect(self._filter_sectors)
        layout.addWidget(self.slot_filter)
        self.sectors = KeyboardList(large_text=False)
        use_standard_list_style(self.sectors)
        self.sectors.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.sectors.currentItemChanged.connect(self._sector_selected)
        self.sectors.applyRequested.connect(self.confirm_placement)
        self.sectors.itemSelectionChanged.connect(self._sector_selection_changed)
        self.sectors.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.sectors.customContextMenuRequested.connect(self._sector_menu)
        layout.addWidget(self.sectors, 1)
        row = QHBoxLayout()
        button("New", self.add_sector, row)
        button("Duplicate", self.duplicate_sector, row)
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
        self.state = QComboBox()
        self.state.addItems(STATES)
        self.state.currentIndexChanged.connect(self._preview)
        row.addWidget(self.state)
        button("Frame selection", lambda: self.view.frame_selection(), row)
        layout.addLayout(row)
        self.view = SectorViewport()
        self._connect_cell_view(self.view)
        self.view.backendChanged.connect(lambda name: self.statusBar().showMessage(name, 5000))
        layout.addWidget(self.view, 1)
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
        self.ground.setRange(0, 5)
        self.ground.setToolTip("Selects the filler family between neighboring sectors. The ground surface itself belongs to the BASE models.")
        self.ground.valueChanged.connect(self._sector_fields_changed)
        form.addRow("Connection family (0–5)", self.ground)
        self.ground_preview = GroundPreview()
        form.addRow(self.ground_preview)
        self.symbol = QSpinBox()
        self.symbol.setRange(0, 255)
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
        self.cell_grid.setToolTip("Top view · Click: select · Ctrl+click: add/remove · Shift+click: rectangle · Drop a BASE to apply · Right click: tools")
        self._connect_cell_view(self.cell_grid)
        layout.addWidget(self.cell_grid)
        self.selection_info = QLabel()
        self.selection_info.setWordWrap(True)
        layout.addWidget(self.selection_info)
        row = QHBoxLayout()
        self.select_all_button = button("Select all", lambda: self._select_all(), row)
        self.select_matching_button = button("Select matching", self.select_matching_cells, row)
        self.clear_cells_button = button("Clear cells", lambda: self._assign_building(0), row)
        layout.addLayout(row)
        self.building = QComboBox()
        layout.addWidget(QLabel("Existing building (includes all four states)"))
        layout.addWidget(self.building)
        self.apply_building_button = button("Apply building to cells", lambda: self._assign_building(self.building.currentData()), layout)
        row = QHBoxLayout()
        self.choose_model_button = button("Choose model…", self.choose_cell_model, row)
        self.inspect_button = button("Inspect model", self.inspect_cell_model, row)
        layout.addLayout(row)
        state_hint = QLabel("Damage states for selected cells")
        layout.addWidget(state_hint)
        form = QFormLayout()
        self.state_models = []
        for title in STATES:
            combo = QComboBox()
            combo.currentIndexChanged.connect(self._model_form_changed)
            self.state_models.append(combo)
            form.addRow(title, combo)
        self.destructible = QCheckBox("Destructible")
        self.destructible.toggled.connect(self._model_form_changed)
        form.addRow(self.destructible)
        layout.addLayout(form)
        self.apply_states_button = button("Apply independent state row to cells", self.apply_states, layout)
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
        self.all_states = QCheckBox("Use this model in all four damage states")
        layout.addWidget(self.all_states)
        self.apply_model_button = button("Apply model to selected cells", self.apply_model, layout)
        self.tabs.addTab(self.models_page, "Models")
        self.tabs.currentChanged.connect(self._browse_tab_changed)

        definitions = QWidget()
        layout = QVBoxLayout(definitions)
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
        self.model_numbers = []
        for title in ("Unused legacy field", "Building symbol", "Shield / damage reduction (%)", "Collision mode (0/1/2)"):
            spin = QSpinBox()
            spin.setRange(-32768, 32767)
            form.addRow(title, spin)
            self.model_numbers.append(spin)
        self.model_numbers[1].setRange(0, 255)
        self.model_numbers[2].setRange(0, 100)
        self.model_numbers[3].setRange(0, 2)
        self.model_numbers[3].setToolTip("0: this model's collision skeleton · 1: standard small-cell collision · 2: standard large-cell collision")
        self.model_comment = QLineEdit()
        form.addRow("Description", self.model_comment)
        layout.addLayout(form)
        effect_hint = QLabel("Destruction effects: effect ID, X, Z, Y (one group per line)")
        effect_hint.setWordWrap(True)
        layout.addWidget(effect_hint)
        self.effects = QPlainTextEdit()
        self.effects.setMaximumHeight(120)
        layout.addWidget(self.effects)
        self.update_model_button = button("Update shared model definition", self.update_model, layout)
        for control, signal in ((self.base, "currentTextChanged"), (self.sklt, "currentTextChanged"),
                                (self.model_comment, "textChanged"), (self.effects, "textChanged"),
                                *((spin, "valueChanged") for spin in self.model_numbers)):
            getattr(control, signal).connect(self._model_form_changed)
        row = QHBoxLayout()
        button("Add model slot", self.add_model, row)
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
        splitter.setSizes([255, 785, 385])

    def reload_game_resources(self, initial=False):
        self.sets.blockSignals(True)
        self.sets.clear()
        install = bootstrap.installation()
        numbers = install.available_sets() if install else []
        if not numbers:
            try:
                root = bootstrap.game_data_dir() / "Sets"
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

    def _sector_menu(self, point):
        item = self.sectors.itemAt(point)
        if item is None or self.document is None:
            return
        number = item.data(Qt.ItemDataRole.UserRole)
        if item.isSelected():
            self.sectors.setCurrentItem(item, QItemSelectionModel.SelectionFlag.NoUpdate)
        else:
            self.sectors.setCurrentItem(item)
        if self.current_sector != number:
            return
        menu = QMenu(self)
        if self.current_sector in self.document.sdf.sectors:
            menu.addAction("Duplicate to free slot", self.duplicate_sector)
            menu.addAction("Copy to another slot…", self.copy_sector_to)
            menu.addAction("Export definition…", self.export_definition)
            menu.addSeparator()
            menu.addAction("Restore saved layout", self.restore_sector)
            menu.addAction("Remove definition / free slot", self.remove_sector)
        else:
            menu.addAction("Create in this slot", self.add_sector)
            menu.addAction("Import definitions into free slots…", self.import_definitions)
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
        menu.addAction("Duplicate model slot", lambda: self.add_model(duplicate=True))
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

    def _cell_menu(self, index, global_point):
        if self.document is None or self.current_sector not in self.document.sdf.sectors:
            return
        if index not in self.selected_cells:
            self.select_cell(index)
        if index not in self.selected_cells or self.current_sector not in self.document.sdf.sectors:
            return
        self.cancel_preview()
        menu = QMenu(self)
        for key in ("move", "copy", "cut", "paste", "clear"):
            menu.addAction(self.actions[key])
        menu.addSeparator()
        menu.addAction("Rotate layout clockwise", lambda: self.transform_cells("rotate"))
        menu.addAction("Mirror layout horizontally", lambda: self.transform_cells("horizontal"))
        ground = menu.addMenu("Ground / filler")
        ground.addAction("Fill selected sectors…", self.fill_ground)
        ground.addAction("Replace matching in selected sectors…", self.replace_ground)
        ground.addAction("Pick from this sector", self.pick_ground)
        ground.setToolTipsVisible(True)
        ground.setToolTip("Native filler family (0–5). Buildings and BASE surfaces stay intact.")
        menu.addSeparator()
        for key in ("choose_model", "inspect", "select_matching", "select_all"):
            menu.addAction(self.actions[key])
        self._prepare_menu(menu)
        menu.exec(global_point)

    def _sector_selection_changed(self):
        if not self._syncing and self.document is not None:
            count = len(self.sectors.selectedItems())
            if count > 1:
                self.statusBar().showMessage(f"{count} sector slots selected · Ground / filler tools apply to the defined sectors in this selection.", 5000)

    def _selected_sector_numbers(self):
        return {item.data(Qt.ItemDataRole.UserRole) for item in self.sectors.selectedItems()
                if item.data(Qt.ItemDataRole.UserRole) in self.document.sdf.sectors}

    def fill_ground(self, checked=False, *, replace_matching=False):
        if self.document is None or self.current_sector not in self.document.sdf.sectors:
            return
        numbers = self._selected_sector_numbers() or {self.current_sector}
        source = self.document.sdf.sectors[self.current_sector].ground
        ground, ok = QInputDialog.getInt(self, "Ground / filler",
            "Filler family (0–5). Keeps buildings and BASE surfaces intact:",
            self._sampled_ground if self._sampled_ground is not None else source, 0, 5)
        if ok:
            self._run_edit(lambda: self.document.set_ground(numbers, ground,
                           replace_ground=source if replace_matching else None))

    def replace_ground(self):
        self.fill_ground(replace_matching=True)

    def pick_ground(self):
        if self.document and self.current_sector in self.document.sdf.sectors:
            value = self.document.sdf.sectors[self.current_sector].ground
            self._sampled_ground = value
            self.ground.setFocus()
            self.statusBar().showMessage(f"Picked native ground / filler family {value}. BASE terrain is part of each cell model.", 6000)

    def _browse_tab_changed(self, index):
        self.cancel_preview()
        if hasattr(self, "models_page") and self.tabs.widget(index) is self.models_page:
            self._browse_preview = True
            self._update_ghost()

    def cancel_interaction(self):
        self.cancel_preview()
        for view in (self.view, self.cell_grid):
            view.cancel_gesture()

    def cancel_preview(self):
        self._placement = None
        self._placement_hover = None
        self._browse_preview = False
        for view in (self.view, self.cell_grid):
            view.placement_active = False
            view.set_ghost()
            view.setCursor(Qt.CursorShape.CrossCursor)

    def _update_ghost(self):
        if self._placement is not None:
            return
        defined = self.document is not None and self.current_sector in self.document.sdf.sectors
        model = self.model_combo.currentData() if self._browse_preview and defined else None
        for view in (self.view, self.cell_grid):
            view.set_ghost(model, self.selected_cells if model is not None else ())

    def drop_model(self, model, cell):
        self.cancel_preview()
        if self.document is None or self.current_sector not in self.document.sdf.sectors:
            return
        if cell not in self.selected_cells:
            self.select_cell(cell)
        if cell not in self.selected_cells:
            return
        self._run_edit(lambda: self.document.assign_model(self.current_sector, self.selected_cells,
                       model, self.state.currentIndex(), self.all_states.isChecked()))

    def select_cell_range(self, index):
        anchor = self._selection_anchor
        rows = sorted((anchor // 3, index // 3))
        cols = sorted((anchor % 3, index % 3))
        if not self._resolve_state_form():
            return
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
        if not self._resolve_state_form():
            return
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
        if not self._resolve_state_form():
            return
        self._commit_sector_fields()
        self.cancel_preview()
        self._placement = {"kind": "move", "number": self.current_sector,
                           "sources": tuple(sorted(self.selected_cells)), "target": None}
        for view in (self.view, self.cell_grid):
            view.placement_active = True
            view.setCursor(Qt.CursorShape.SizeAllCursor)
        self.statusBar().showMessage("Move: drag or click a destination | Enter: confirm | Esc: cancel")

    def _arm_clipboard(self):
        if self._copied_cells is None or self.document is None or self.current_sector not in self.document.sdf.sectors:
            return
        if not self._resolve_state_form():
            return
        self.cancel_preview()
        self._placement = {"kind": "paste", "clipboard": self._copied_cells,
                           "sources": self._copied_positions, "target": None}
        for view in (self.view, self.cell_grid):
            view.placement_active = True
            view.setCursor(Qt.CursorShape.DragCopyCursor)
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
            view.setCursor(Qt.CursorShape.ForbiddenCursor if not cells else
                           Qt.CursorShape.SizeAllCursor if operation["kind"] == "move" else Qt.CursorShape.DragCopyCursor)

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
            self._assign_building(0)
            self._arm_clipboard()

    def choose_cell_model(self):
        if self.document and self.current_sector in self.document.sdf.sectors:
            self.preview_mode.setCurrentIndex(0)
            self.tabs.setCurrentWidget(self.models_page)
            self.model_search.setFocus()

    def inspect_cell_model(self):
        if not self.selected_cells:
            return
        if self.document is None or self.current_sector not in self.document.sdf.sectors:
            return
        sector = self.document.sdf.sectors[self.current_sector]
        bid = sector.subsects[0 if sector.single else min(self.selected_cells, default=4)]
        if not 0 <= bid < len(self.document.sdf.buildings):
            return
        mid = self.document.sdf.buildings[bid].state(self.state.currentIndex())
        index = self.model_combo.findData(mid)
        if index < 0:
            return
        self.model_combo.setCurrentIndex(index)
        if self.model_combo.currentData() != mid:
            return
        self.model_search.clear()
        self.tabs.setCurrentWidget(self.definitions_page)
        self.preview_mode.setCurrentIndex(1)

    def select_matching_cells(self):
        if not self.selected_cells:
            return
        if self.document is None or self.current_sector not in self.document.sdf.sectors:
            return
        if not self._resolve_state_form():
            return
        self._commit_sector_fields()
        sector = self.document.sdf.sectors[self.current_sector]
        bid = sector.subsects[0 if sector.single else min(self.selected_cells, default=4)]
        self.selected_cells = {4} if sector.single else {i for i, value in enumerate(sector.subsects) if value == bid}
        self._load_sector_fields()

    def restore_sector(self):
        if self.document is not None:
            self._run_edit(lambda: self.document.restore_sector(self.current_sector), reset=True)

    def import_definitions(self):
        if self.document is None or not self._commit_forms():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Import native SDF definitions using this set's assets", "", "Set definitions (*.sdf)")
        if not path:
            return
        def apply():
            numbers = self.document.import_definitions(path)
            self.current_sector = numbers[0]
            self.statusBar().showMessage(f"Imported {len(numbers)} sector(s) into free slots.", 6000)
        self._run_edit(apply, reset=True)

    def export_definition(self, checked=False, *, all_sectors=False):
        if self.document is None or not self._commit_forms():
            return
        numbers = list(self.document.sdf.sectors) if all_sectors else [self.current_sector]
        if not numbers or any(n not in self.document.sdf.sectors for n in numbers):
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export native definitions (assets remain in the set)",
            f"Set{self.document.number}-sectors.sdf" if all_sectors else f"Sector{self.current_sector:03d}.sdf",
            "Set definitions (*.sdf)")
        if path:
            try:
                self.document.export_definitions(numbers, path)
                self.statusBar().showMessage(f"Exported {len(numbers)} definition(s) to {path}", 6000)
            except (OSError, ValueError) as exc:
                QMessageBox.warning(self, "Cannot export definitions", str(exc))

    def export_preview(self):
        if self.library is None or not self.view.isVisible():
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export current view", f"Sector{self.current_sector:03d}.png", "PNG image (*.png)")
        if path:
            try:
                image = self.view.capture_preview()
                if image.isNull() or not image.save(path, "PNG"):
                    raise OSError("The preview image could not be written.")
                self.statusBar().showMessage(f"Saved view to {path}", 6000)
            except (OSError, ValueError, RuntimeError) as exc:
                QMessageBox.warning(self, "Cannot export preview", str(exc))

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
        if self.document is None or not (self.document.dirty or self._pending_model()
                                        or self._pending_states() or self._pending_sector_fields()):
            return True
        choice = QMessageBox.question(self, "Unsaved set", "Save changes to this set?",
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
            self.cancel_preview()
            self._copied_cells = None
            self._sampled_ground = None
            self._model_titles = None
            self._building_titles = None
            self._editing_model_id = self._model_snapshot = None
            self._state_snapshot = self._state_target = self._sector_fields_number = None
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
        pending_states = self._state_form_values() if self._pending_states() else None
        self._syncing = True
        self._bind_tables()
        sdf = self.document.sdf
        selected_sectors = {item.data(Qt.ItemDataRole.UserRole) for item in self.sectors.selectedItems()}
        self.sectors.blockSignals(True)
        self.sectors.clear()
        for number in range(256):
            sector = sdf.sectors.get(number)
            item = QListWidgetItem()
            detail = (sector.comment or ("Single cell" if sector.single else "3 × 3 cells")) if sector else "Free slot · Create here"
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
        if pending_states is not None:
            for combo, mid in zip(self.state_models, pending_states[0]):
                combo.setCurrentIndex(combo.findData(mid))
            self.destructible.setChecked(pending_states[1])
        building_titles = tuple(f"{row.id:03d}  " + (sdf.models[row.intact].base if 0 <= row.intact < len(sdf.models) else "Missing model") for row in sdf.buildings)
        if building_titles != self._building_titles:
            self.building.clear()
            for row, title in zip(sdf.buildings, building_titles):
                self.building.addItem(title, row.id)
            self._building_titles = building_titles
        self.counts.setText(f"{len(sdf.sectors)} defined · {256-len(sdf.sectors)} free\nModels {len(sdf.models)}/{MODEL_LIMIT} · Buildings {len(sdf.buildings)}/{BUILDING_LIMIT}")
        self._syncing = False
        self._filter_models()
        self._load_sector_fields()
        self._load_model_fields()
        self._update_model_usage()
        self._update_issues()
        self._preview(reset=reset)
        self.actions["undo"].setEnabled(bool(self.document._undo))
        self.actions["redo"].setEnabled(bool(self.document._redo))
        for key, stack in (("undo", self.document._undo), ("redo", self.document._redo)):
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
                key = (self.previews.generation, kind, number, signature)
                if item.data(PREVIEW_ROLE) != key:
                    item.setIcon(QIcon())
                    item.setData(PREVIEW_ROLE, key)
                requests.append((key, kind, number, self.state.currentIndex()))
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
        if not self._resolve_state_form():
            self.sectors.blockSignals(True)
            self.sectors.setCurrentRow(self.current_sector)
            self.sectors.blockSignals(False)
            return
        self._commit_sector_fields()
        self.cancel_preview()
        self.current_sector = number
        self.sectors.blockSignals(True)
        self.sectors.setCurrentRow(number, QItemSelectionModel.SelectionFlag.NoUpdate)
        self.sectors.blockSignals(False)
        self.selected_cells = {4}
        self._selection_anchor = 4
        self._load_sector_fields()
        self._preview(reset=True)

    def _load_sector_fields(self):
        if self.document is None:
            return
        sector = self.document.sdf.sectors.get(self.current_sector)
        pending_description = self._pending_sector_fields()
        if sector and sector.single and self.selected_cells:
            self.selected_cells = {4}
        target = (self.current_sector, tuple(sorted(self.selected_cells)))
        pending_states = self._pending_states() and target == self._state_target
        self._syncing = True
        self.sector_title.setText(f"Sector {self.current_sector:03d} [{self.current_sector:02X}]" if sector else f"Slot {self.current_sector:03d} is free — click New")
        for field in (self.layout_mode, self.ground, self.symbol, self.description, self.building):
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
            index = 0 if sector.single else min(self.selected_cells, default=4)
            bid = sector.subsects[index]
            self.building.setCurrentIndex(self.building.findData(bid))
            if 0 <= bid < len(self.document.sdf.buildings):
                row = self.document.sdf.buildings[bid]
                if not pending_states:
                    for state, combo in enumerate(self.state_models):
                        combo.setCurrentIndex(combo.findData(row.state(state)))
                    self.destructible.setChecked(bool(row.destructible))
        for field in (*self.state_models, self.destructible, self.apply_states_button):
            field.setEnabled(sector is not None and bool(self.selected_cells))
        self._sector_fields_number = self.current_sector
        if not pending_states:
            self._state_target = target
            self._state_snapshot = self._state_form_values() if sector and self.selected_cells else None
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
        for key in ("select_all", "select_matching", "inspect", "choose_model", "copy", "paste", "clear", "cut", "move"):
            self.actions[key].setEnabled(defined and bool(selected))
        self.actions["select_all"].setEnabled(defined)
        for key in ("inspect", "copy", "cut", "move"):
            self.actions[key].setEnabled(valid)
        can_paste = defined and self._copied_cells is not None
        self.actions["paste"].setEnabled(can_paste)
        self.actions["undo"].setEnabled(bool(self.document and self.document._undo))
        self.actions["redo"].setEnabled(bool(self.document and self.document._redo))
        for field in (self.apply_model_button, self.apply_building_button, self.choose_model_button,
                      self.select_all_button, self.select_matching_button, self.clear_cells_button):
            field.setEnabled(defined and bool(selected))
        self.select_all_button.setEnabled(defined)
        self.inspect_button.setEnabled(valid)
        if not sector:
            self.selection_info.setText("Create a sector in this free slot to edit cells.")
            self.model_target.setText(f"Slot {self.current_sector:03d} is free. Create a sector before applying models.")
            return
        if not selected:
            self.selection_info.setText("No cells selected | Click a cell or drag a selection box")
            self.model_target.setText(f"Sector {self.current_sector:03d} | No cells selected")
            return
        if len(selected) == 1:
            summary = f"Cell {selected[0]+1} selected"
        elif len(set(bids)) > 1:
            summary = f"{len(selected)} cells selected · Different buildings"
        else:
            summary = f"{len(selected)} cells selected · Same building"
        self.selection_info.setText(summary)
        self.selection_info.setToolTip(f"Damage-state fields start from cell {selected[0]+1}. Apply replaces the full state row for all selected cells.")
        self.model_target.setText(f"Sector {self.current_sector:03d} · {summary}")

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

    def _state_form_values(self):
        return tuple(combo.currentData() for combo in self.state_models), self.destructible.isChecked()

    def _pending_states(self):
        return self._state_snapshot is not None and self._state_snapshot != self._state_form_values()

    def _resolve_state_form(self):
        if not self._pending_states():
            return True
        choice = QMessageBox.question(self, "Building state changes", "Apply pending damage states to the selected cells?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel)
        if choice == QMessageBox.StandardButton.Save:
            if not self._commit_state_form():
                return False
            self._refresh()
            return True
        if choice == QMessageBox.StandardButton.Discard:
            self._state_snapshot = None
            self._load_sector_fields()
            return True
        return False

    def _commit_state_form(self, *, force=False):
        if not force and not self._pending_states():
            return True
        try:
            number, cells = self._state_target
            states, destructible = self._state_form_values()
            self.document.assign_states(number, cells, states, int(destructible))
            self._state_snapshot = self._state_form_values()
            self._bind_tables()
            self._preview()
            self._update_model_usage()
            self._model_form_changed()
            return True
        except (ValueError, IndexError) as exc:
            self._bind_tables()
            QMessageBox.warning(self, "Cannot update building states", str(exc))
            return False

    def _commit_forms(self):
        pending = self._pending_sector_fields() or self._pending_model() or self._pending_states()
        self._commit_sector_fields()
        if not (self._commit_model_form() and self._commit_state_form()):
            return False
        if pending:
            self._refresh()
        return True

    def _sector_fields_changed(self):
        if self._syncing or self.document is None or self.current_sector not in self.document.sdf.sectors:
            return
        sector = self.document.sdf.sectors[self.current_sector]
        updated = replace(sector, ground=self.ground.value(), symbol=self.symbol.value(), comment=self.description.text())
        self.document.change("Sector properties", lambda: self.document.sdf.sectors.__setitem__(self.current_sector, updated))
        self._description_snapshot = self.description.text()
        self._refresh()

    def _layout_changed(self, index):
        if self.document and self.current_sector in self.document.sdf.sectors:
            if not self._resolve_state_form():
                self._load_sector_fields()
                return
            self._commit_sector_fields()
            self.document.set_layout(self.current_sector, bool(index))
            self.selected_cells = {4}
            self._refresh(reset=True)

    def select_cell(self, index, additive=False):
        selected = self.selected_cells.symmetric_difference({index}) if additive else {index}
        if selected != self.selected_cells and not self._resolve_state_form():
            self._load_sector_fields()
            return
        self._commit_sector_fields()
        if self._placement is not None:
            self.cancel_preview()
        self._selection_anchor = index
        self.selected_cells = selected
        self._load_sector_fields()

    def _select_all(self):
        sector = self.document.sdf.sectors.get(self.current_sector) if self.document else None
        if not self._resolve_state_form():
            return
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
        for spin, value in zip(self.model_numbers, model.numbers):
            spin.setValue(value)
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

    def _model_form_values(self):
        return (self.base.currentText(), self.sklt.currentText(),
                tuple(spin.value() for spin in self.model_numbers),
                self.effects.toPlainText(), self.model_comment.text())

    def _pending_model(self):
        return self._model_snapshot is not None and self._model_snapshot != self._model_form_values()

    def _model_form_changed(self):
        if self._syncing or self.document is None:
            return
        pending = self._pending_model()
        self.update_model_button.setText("Apply pending model changes" if pending else "Update shared model definition")
        states = self._pending_states()
        self.apply_states_button.setText("Apply pending damage states to cells" if states else "Apply independent state row to cells")
        dirty = self.document.dirty or pending or states or self._pending_sector_fields()
        self.setWindowTitle(f"{'* ' if dirty else ''}Set {self.document.number} - OpenNeoUA Studio - Sector Editor")
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
        count = sum(mid == b.state(s) for b in self.document.sdf.buildings for s in range(4))
        used = len(self.document.sectors_using_model(mid))
        self.model_usage.setText(f"Model {mid}: used in {used} sectors and {count} building states. Updating this definition affects all of them.")

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
                            sklt=self.sklt.currentText().strip(), numbers=tuple(s.value() for s in self.model_numbers),
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
            self.preview_status.setText("Free sector slot. Click New to define it, or duplicate an existing sector here.")
            return
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

    def _run_edit(self, callback, *, reset=False, resolve_states=True):
        if self.document is None:
            return
        if resolve_states and not self._resolve_state_form():
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

    def duplicate_sector(self):
        if self.document and self.current_sector in self.document.sdf.sectors:
            self._run_edit(lambda: setattr(self, "current_sector", self.document.add_sector(self.current_sector)), reset=True)

    def copy_sector_to(self):
        if self.document is None or self.current_sector not in self.document.sdf.sectors:
            return
        number, ok = QInputDialog.getInt(self, "Copy sector", "Unused destination ID:", self.document.free_sector() or 1, 0, 255)
        if ok:
            self._run_edit(lambda: setattr(self, "current_sector", self.document.add_sector(self.current_sector, number)), reset=True)

    def remove_sector(self):
        if self.document and self.current_sector in self.document.sdf.sectors:
            choice = QMessageBox.question(self, "Free sector slot", "Remove this definition? Existing maps may still refer to this sector ID.")
            if choice == QMessageBox.StandardButton.Yes:
                self._run_edit(lambda: self.document.change("Remove sector", lambda: self.document.sdf.sectors.pop(self.current_sector)))

    def _assign_building(self, bid):
        if bid is not None and self.document and self.current_sector in self.document.sdf.sectors:
            self._run_edit(lambda: self.document.assign_building(self.current_sector, self.selected_cells, bid))

    def apply_model(self):
        if not self.selected_cells:
            return
        if self.document and self.current_sector in self.document.sdf.sectors:
            self._run_edit(lambda: self.document.assign_model(self.current_sector, self.selected_cells,
                           self.model_combo.currentData(), self.state.currentIndex(), self.all_states.isChecked()))

    def apply_states(self):
        if not self.selected_cells:
            return
        if self.document is None or self.current_sector not in self.document.sdf.sectors:
            return
        self._commit_sector_fields()
        if self._commit_state_form(force=True):
            self._refresh()

    def update_model(self):
        if self.document is not None and self._commit_model_form():
            self._refresh()

    def add_model(self, checked=False, *, duplicate=False):
        if self.document is None or not self._commit_model_form():
            return
        result = self._run_edit(lambda: self.document.add_model(self.base.currentText().strip(), self.sklt.currentText().strip(),
                                source=self.model_combo.currentData() if duplicate else None), resolve_states=False)
        if result is not None:
            self.model_combo.setCurrentIndex(self.model_combo.findData(result))

    def transform_cells(self, operation):
        if self.document is None or self.current_sector not in self.document.sdf.sectors:
            return
        sector = self.document.sdf.sectors[self.current_sector]
        if sector.single:
            return
        mappings = {"rotate": (6, 3, 0, 7, 4, 1, 8, 5, 2),
                    "horizontal": (2, 1, 0, 5, 4, 3, 8, 7, 6),
                    "vertical": (6, 7, 8, 3, 4, 5, 0, 1, 2)}
        def apply():
            sector = self.document.sdf.sectors[self.current_sector]
            updated = replace(sector, subsects=tuple(sector.subsects[i] for i in mappings[operation]))
            self.document.change("Rearrange cells", lambda: self.document.sdf.sectors.__setitem__(self.current_sector, updated))
        self._run_edit(apply)

    def replace_building(self):
        if self.document is None:
            return
        old, ok = QInputDialog.getInt(self, "Replace building", "Building ID to replace in all sectors:", 0, 0, len(self.document.sdf.buildings)-1)
        if not ok:
            return
        new, ok = QInputDialog.getInt(self, "Replace building", "Replacement building ID:", 0, 0, len(self.document.sdf.buildings)-1)
        if ok:
            def apply():
                def edit():
                    for number, sector in self.document.sdf.sectors.items():
                        self.document.sdf.sectors[number] = replace(sector, subsects=tuple(new if b == old else b for b in sector.subsects))
                self.document.change("Replace building throughout set", edit)
            self._run_edit(apply)

    def copy_cells(self, checked=False, *, preview=True):
        if self.selected_cells and self.document and self.current_sector in self.document.sdf.sectors:
            if not self._resolve_state_form():
                return
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
                                "Native table references and model resources are valid.\n\nSector IDs: 0–255. Slots 248–255 are used for map borders.\n\nSave writes SET.SDF and the native LEGO visual overrides when needed. Save Set As and Export ZIP include the complete set resources.\n\nRotating/mirroring rearranges cell positions; it does not rotate the geometry of each model.")

    def save(self):
        if self.document is None or not self._commit_forms():
            return False
        try:
            self.document.save()
            self._refresh()
            self.statusBar().showMessage("SET.SDF saved and reloaded successfully.", 5000)
            return True
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Cannot save set", str(exc))
            return False

    def save_as(self):
        if self.document is None or not self._commit_forms():
            return
        parent = QFileDialog.getExistingDirectory(self, "Choose parent folder for the copied set")
        if not parent:
            return
        number, ok = QInputDialog.getInt(self, "Set number", "New set number:", self.document.number, 1, 255)
        if ok:
            try:
                copied = self.document.copy_to(Path(parent) / f"Set{number}")
                self.open_set(copied.folder, check_unsaved=False)
                self.statusBar().showMessage(f"Complete set copied to {copied.folder}", 8000)
            except (OSError, ValueError) as exc:
                QMessageBox.warning(self, "Cannot copy set", str(exc))

    def export_zip(self):
        if self.document is None or not self._commit_forms():
            return
        target, _ = QFileDialog.getSaveFileName(self, "Export complete set", f"Set{self.document.number}.zip", "ZIP archive (*.zip)")
        if target:
            try:
                with loading_resources(self, "Packaging and verifying complete set…"):
                    count = self.document.export_zip(target)
                self.statusBar().showMessage(f"Exported {count} files to {target}", 8000)
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
