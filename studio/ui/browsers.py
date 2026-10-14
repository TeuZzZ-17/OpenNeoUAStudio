"""Entity List panel and the common vertical resource browsers.
"""

import os
from pathlib import Path

from PySide6.QtCore import QEvent, QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from map_editor import bootstrap
from studio.assets.catalog import load_game_objects

# ----------------------------------------------------------------------------
# One Entity List panel and visual-state workflow for Model and Snapshot.
# (formerly entity_list.py)
# ----------------------------------------------------------------------------

class EntityListMixin:
    def _create_entity_list(self):
        panel = QWidget(self)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        self.entity_browser = GameObjectBrowser(self)
        self.entity_browser.enable_state_column()
        self._entity_visual_states = {}
        self._entity_choices = {}
        self.entity_state = QComboBox()
        self.entity_state.setToolTip('Choose the model state used by this game entity.')
        browser_layout = self.entity_browser.layout()
        previous_controls = browser_layout.takeAt(0).layout()
        previous_controls.removeWidget(self.entity_browser.category)
        previous_controls.deleteLater()
        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(5)
        for label, combo, stretch in (('State:', self.entity_state, 1),
                                       ('Type:', self.entity_browser.category, 0)):
            controls.addWidget(QLabel(label))
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(5 if stretch == 0 else 10)
            combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            controls.addWidget(combo, stretch)
        browser_layout.insertLayout(browser_layout.indexOf(self.entity_browser.tree), controls)
        layout.addWidget(self.entity_browser, 1)
        self._entity_controls = controls
        self._entity_panel = panel
        self._current_entity = None
        self._loaded_entity_state = 0
        self._loaded_entity_set = None
        self._entity_loading = False
        self._set_combo = self.entity_browser.set_combo
        self.entity_browser.selected.connect(self._entity_selected)
        self.entity_browser.tree.installEventFilter(self)
        self.entity_browser.setChanged.connect(self._entity_set_changed)
        self.entity_state.currentIndexChanged.connect(self._open_entity_state)
        return panel

    def _reload_entity_resources(self):
        self.entity_browser.reload()
        self.entity_browser.enable_state_column()
        self._entity_choices.clear()
        self._entity_visual_states.clear()
        self.entity_state.clear()
        self._current_entity = None

    def _entity_set_changed(self, number):
        from studio.ui.widgets import loading_resources
        pages = [(tabs, tabs.currentWidget()) for tabs in (self._right_tabs, self._resources_tabs)]
        self._entity_loading = True
        try:
            with loading_resources(self, f"Loading Set {number}. Please wait…"):
                if self._set_selector_changed(number) and self._current_entity is not None:
                    self._entity_selected(self._current_entity)
                self.entity_browser.enable_state_column()
                self._refresh_dirty_resource_rows()
        finally:
            self._entity_loading = False
            for tabs, page in pages:
                if page is not None and tabs.indexOf(page) >= 0:
                    tabs.setCurrentWidget(page)

    def _entity_selected(self, obj):
        from studio.assets.catalog import visual_choices
        try:
            choices = visual_choices(obj, self.entity_browser.set_number)
        except (OSError, ValueError, RuntimeError) as exc:
            QMessageBox.warning(self, "Entity unavailable", str(exc))
            self.entity_browser.select_object(self._current_entity)
            return
        if not choices:
            self.statusBar().showMessage(f"{obj.name}: no model state found in the selected set.")
            self.entity_browser.select_object(self._current_entity)
            return
        previous = self._current_entity
        self.play_button.setChecked(False)
        previous_choices = [(self.entity_state.itemText(i), self.entity_state.itemData(i))
                            for i in range(self.entity_state.count())]
        previous_index = self.entity_state.currentIndex()
        self._current_entity = obj
        key = (self.entity_browser.set_number, obj.kind, obj.object_id)
        self._entity_choices[key] = choices
        with QSignalBlocker(self.entity_state):
            self.entity_state.clear()
            for choice in choices:
                self.entity_state.addItem(choice.label, choice)
            remembered = self._entity_visual_states.get(key)
            if remembered is not None:
                self.entity_state.setCurrentIndex(max(0, self.entity_state.findText(remembered)))
        if not self._open_entity_state():
            self._current_entity = previous
            with QSignalBlocker(self.entity_state):
                self.entity_state.clear()
                for label, choice in previous_choices:
                    self.entity_state.addItem(label, choice)
                self.entity_state.setCurrentIndex(previous_index)
            self.entity_browser.select_object(previous)

    def _open_entity_state(self, *_args):
        choice = self.entity_state.currentData()
        if choice is None or not self._confirm_discard_geometry():
            with QSignalBlocker(self.entity_state):
                self.entity_state.setCurrentIndex(self._loaded_entity_state)
            if self._loaded_entity_set is not None:
                with QSignalBlocker(self.entity_browser.set_combo):
                    index = self.entity_browser.set_combo.findData(self._loaded_entity_set)
                    if index >= 0:
                        self.entity_browser.set_combo.setCurrentIndex(index)
            return False
        skip_warning = self._skip_model_switch_warning
        previous_loading = self._entity_loading
        self._entity_loading = True
        self._skip_model_switch_warning = True
        try:
            if choice.base_name:
                if self._setbas is None or Path(self._setbas.path) != choice.path:
                    self.open_setbas(choice.path)
                if self._activate_setbas_base(choice.base_name) is None:
                    return False
            elif choice.path.suffix.casefold() == ".3ds":
                from entity_designer.script_model import _load_3ds_family
                self._set_family(_load_3ds_family(choice.path))
                self._set_document_title(choice.path)
            else:
                self.open_base(choice.path, confirm_discard=False)
            self.statusBar().showMessage(f"{self._current_entity.name} — {choice.label}")
            self._loaded_entity_state = self.entity_state.currentIndex()
            self._loaded_entity_set = self.entity_browser.set_number
            key = (self._loaded_entity_set, self._current_entity.kind, self._current_entity.object_id)
            self._entity_visual_states[key] = choice.label
            self.entity_browser.set_visual_state(self._current_entity,
                choice.label.split(' — ')[-1] if self._current_entity.kind == 'building' else choice.label.split(' — ')[0])
            self._refresh_dirty_resource_rows()
            return True
        except (OSError, ValueError, RuntimeError) as exc:
            QMessageBox.warning(self, "Entity unavailable", str(exc))
            return False
        finally:
            self._skip_model_switch_warning = skip_warning
            self._entity_loading = previous_loading


# ----------------------------------------------------------------------------
# Common vertical resource browsers used by Studio workspaces.
# (formerly game_browser.py)
# ----------------------------------------------------------------------------

GAME_OBJECT_ROLE = int(Qt.ItemDataRole.UserRole) + 64


def configure_resource_tree(tree):
    from studio.ui.widgets import use_resource_delegate
    use_resource_delegate(tree)
    tree.setAlternatingRowColors(True)
    tree.setUniformRowHeights(True)
    tree.setRootIsDecorated(False)
    tree.setSelectionBehavior(QTreeWidget.SelectionBehavior.SelectRows)
    tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    tree.header().setStretchLastSection(False)
    tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    for column in range(1, tree.columnCount()):
        tree.header().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)


def game_files(install, suffixes):
    """Scan live resources and manual folder overrides, excluding build output."""
    roots = [install.data, *install.folders.values()]
    files = set()
    visited = set()
    ignored = {".git", ".codex", "__pycache__", ".pytest_cache", "build", "dist", "outputs"}
    for root in roots:
        if not root.is_dir():
            continue
        for directory, children, names in os.walk(root):
            resolved = Path(directory).resolve()
            if resolved in visited:
                children.clear()
                continue
            visited.add(resolved)
            children[:] = [name for name in children if name.casefold() not in ignored]
            for name in names:
                if Path(name).suffix.casefold() in suffixes:
                    files.add(resolved / name)
    return sorted(files, key=lambda path: str(path).casefold())


def folder_files(root, suffixes):
    """Scan one user-chosen folder recursively; unlike game_files it skips nothing."""
    files = set()
    visited = set()
    for directory, children, names in os.walk(root):
        resolved = Path(directory).resolve()
        if resolved in visited:
            children.clear()
            continue
        visited.add(resolved)
        for name in names:
            if Path(name).suffix.casefold() in suffixes:
                files.add(resolved / name)
    return sorted(files, key=lambda path: str(path).casefold())


class GameFileBrowser(QWidget):
    selected = Signal(object)

    def __init__(self, suffixes, parent=None, allow_folder=False):
        super().__init__(parent)
        self.suffixes = set(suffixes)
        self.folder = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        # The folder picker is opt-in so other tools keep the plain game list.
        self.folder_button = self.game_button = self.folder_label = None
        if allow_folder:
            row = QHBoxLayout()
            self.folder_button = QPushButton("Open Folder...")
            self.folder_button.setToolTip("Load only the files found in a folder you choose.")
            self.game_button = QPushButton("Game Files")
            self.game_button.setToolTip("Go back to the files of the configured game.")
            self.game_button.setEnabled(False)
            row.addWidget(self.folder_button, 1)
            row.addWidget(self.game_button, 1)
            layout.addLayout(row)
            self.folder_label = QLabel()
            self.folder_label.setWordWrap(True)
            self.folder_label.hide()
            layout.addWidget(self.folder_label)
            self.folder_button.clicked.connect(self.choose_folder)
            self.game_button.clicked.connect(lambda: self.set_folder(None))
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search game files")
        self.search.setClearButtonEnabled(True)
        layout.addWidget(self.search)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Browse"])
        configure_resource_tree(self.tree)
        layout.addWidget(self.tree, 1)
        self.search.textChanged.connect(self._filter)
        self.tree.currentItemChanged.connect(self._selected)
        self.reload()

    def choose_folder(self):
        install = bootstrap.installation()
        start = self.folder or (install.data if install else Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Open folder", str(start))
        if chosen:
            self.set_folder(Path(chosen))

    def set_folder(self, folder):
        """Show only the files of ``folder``; ``None`` returns to the game files."""
        self.folder = Path(folder) if folder else None
        if self.folder_button is not None:
            self.game_button.setEnabled(self.folder is not None)
            self.folder_label.setVisible(self.folder is not None)
            self.folder_label.setText(f"Folder: {self.folder}" if self.folder else "")
        self.reload()

    def reload(self):
        install = bootstrap.installation()
        if self.folder is not None:
            base, paths = self.folder, folder_files(self.folder, self.suffixes)
        else:
            base, paths = (install.data, game_files(install, self.suffixes)) if install else (None, [])
        with QSignalBlocker(self.tree):
            self.tree.clear()
            for path in paths:
                try:
                    label = str(path.relative_to(base))
                except ValueError:
                    label = str(path)
                item = QTreeWidgetItem([label])
                item.setData(0, Qt.ItemDataRole.UserRole, path)
                item.setToolTip(0, str(path))
                self.tree.addTopLevelItem(item)
        self._filter(self.search.text())

    def _filter(self, text):
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            item.setHidden(text.casefold() not in item.text(0).casefold())

    def _selected(self, current, previous):
        if current is not None:
            self.selected.emit(current.data(0, Qt.ItemDataRole.UserRole))

    def select_path(self, path):
        with QSignalBlocker(self.tree):
            selected = None
            for index in range(self.tree.topLevelItemCount()):
                item = self.tree.topLevelItem(index)
                candidate = item.data(0, Qt.ItemDataRole.UserRole)
                if path is not None and candidate is not None and Path(candidate).resolve() == Path(path).resolve():
                    selected = item
                    break
            self.tree.setCurrentItem(selected)


class GameObjectBrowser(QWidget):
    selected = Signal(object)
    setChanged = Signal(int)

    def __init__(self, parent=None, tree=None, kinds=None):
        super().__init__(parent)
        # ``kinds`` limits the list to some game-object kinds; None shows all of them.
        self.kinds = tuple(kinds) if kinds else None
        self.objects = []
        self._state_column = False
        self._visual_states = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        controls = QHBoxLayout()
        self.category = QComboBox()
        for label, kind in (("All", ""), ("Vehicles", "vehicle"), ("Weapons", "weapon"), ("Buildings", "building")):
            if kind == "" or self.kinds is None or kind in self.kinds:
                self.category.addItem(label, kind)
        # The set is chosen from the window's Change Set menu. This combo is
        # never shown: it only holds the current set and its change signal.
        self.set_combo = QComboBox(self)
        self.set_combo.hide()
        controls.addWidget(self.category, 1)
        layout.addLayout(controls)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search name or ID")
        self.search.setClearButtonEnabled(True)
        layout.addWidget(self.search)
        self.tree = tree if tree is not None else QTreeWidget()
        self.tree.setHeaderLabels(["Game object", "Type", "ID"])
        configure_resource_tree(self.tree)
        layout.addWidget(self.tree, 1)
        self.search.textChanged.connect(self.filter)
        self.category.currentIndexChanged.connect(self.filter)
        self.set_combo.currentIndexChanged.connect(self._set_changed)
        self.tree.currentItemChanged.connect(self._selected)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_context_menu)
        self.tree.installEventFilter(self)
        self.reload()

    def _context_menu(self, item):
        menu = QMenu(self)
        obj = item.data(0, GAME_OBJECT_ROLE) if item is not None else None
        window = self.window()
        if obj is not None and hasattr(window, "viewport") and hasattr(window, "play_button"):
            button, current_available = self._playback_control(window)
            playing = item is self.tree.currentItem() and button.isChecked()
            action = menu.addAction("Pause" if playing else "Play")
            # A right click only inspects the row; loading starts on activation.
            if item is self.tree.currentItem():
                available = current_available
            elif hasattr(window, "_resolve_setbas_base"):
                from studio.assets.catalog import visual_choices
                from studio.assets.family import family_object_has_animation
                choices = visual_choices(obj, self.set_number)
                available = False
                for choice in choices:
                    if not choice.base_name or window._setbas is None:
                        continue
                    family, target, _offset = window._resolve_setbas_base(choice.base_name)
                    if target is not None:
                        available = family_object_has_animation(family, target)
                        break
            else:
                available = False
            action.setEnabled(bool(available))
            action.triggered.connect(
                lambda: self._play_item(item, window))
            menu.addSeparator()
        if obj is not None:
            for label, value in (('Copy Name', obj.name), ('Copy ID', str(obj.object_id)), ('Copy Script Path', str(obj.source))):
                menu.addAction(label, lambda _checked=False, text=value, what=label: self._copy(what, text))
        return menu

    def _play_item(self, item, window):
        self.tree.setCurrentItem(item)
        self._toggle_playback(window)

    @staticmethod
    def _playback_control(window):
        panel = getattr(window, "fx_panel", None)
        if panel is not None and window._is_fx_tab_active():
            return panel.play_button, bool(panel.document.items)
        return window.play_button, window.viewport.has_animation

    def _toggle_playback(self, window):
        if hasattr(window, "_toggle_list_animation"):
            window._toggle_list_animation()
        elif hasattr(window, "viewport") and hasattr(window, "play_button"):
            button, available = self._playback_control(window)
            if available:
                button.setChecked(not button.isChecked())

    def eventFilter(self, watched, event):  # noqa: N802
        if (watched is self.tree and event.type() == QEvent.Type.KeyPress
                and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and event.modifiers() == Qt.KeyboardModifier.NoModifier
                and self.tree.state() != self.tree.State.EditingState
                and self.tree.currentItem() is not None):
            if not event.isAutoRepeat():
                self._toggle_playback(self.window())
            return True
        return super().eventFilter(watched, event)

    def _copy(self, what, text):
        QApplication.clipboard().setText(text)
        # Same bottom-left channel as the other Studio operations.
        window = self.window()
        if hasattr(window, 'statusBar'):
            window.statusBar().showMessage(f"{what.replace('Copy ', 'Copied ')}: {text}", 4000)

    def _show_context_menu(self, position):
        item = self.tree.itemAt(position) or self.tree.currentItem()
        self._entity_menu = self._context_menu(item)
        if self._entity_menu.isEmpty():
            return
        self._entity_menu.popup(self.tree.viewport().mapToGlobal(position))

    @property
    def set_number(self):
        return self.set_combo.currentData()

    def reload(self):
        install = bootstrap.installation()
        from studio.ui.shell import selected_set
        selected_number = self.set_number or selected_set()
        with QSignalBlocker(self.set_combo):
            self.set_combo.clear()
            for number in install.available_sets() if install else []:
                self.set_combo.addItem(f"Set {number}", number)
            index = self.set_combo.findData(selected_number)
            self.set_combo.setCurrentIndex(index if index >= 0 else 0)
        self.objects = [obj for obj in (load_game_objects(install) if install else [])
                        if self.kinds is None or obj.kind in self.kinds]
        self.fill()

    def fill(self, selected=None):
        with QSignalBlocker(self.tree):
            self.tree.clear()
            for obj in self.objects:
                texts = [obj.name, obj.kind.title(), str(obj.object_id)]
                if self._state_column:
                    key = (self.set_number, obj.kind, obj.object_id)
                    texts.insert(1, self._visual_states.get(key, self.default_visual_state(obj)))
                item = QTreeWidgetItem(texts)
                item.setData(0, GAME_OBJECT_ROLE, obj)
                item.setToolTip(0, f"{obj.source}\n{obj.kind} {obj.object_id}")
                self.tree.addTopLevelItem(item)
                if selected is not None and (obj.kind, obj.object_id) == (selected.kind, selected.object_id):
                    self.tree.setCurrentItem(item)
        self.filter()

    def filter(self, *_args):
        text, kind = self.search.text().casefold(), self.category.currentData()
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            obj = item.data(0, GAME_OBJECT_ROLE)
            if obj is not None:
                item.setHidden(bool(kind and obj.kind != kind) or text not in f"{obj.name} {obj.object_id}".casefold())

    def _selected(self, current, previous):
        if current is not None:
            obj = current.data(0, GAME_OBJECT_ROLE)
            if obj is not None:
                self.selected.emit(obj)

    def _set_changed(self):
        if self.set_number is not None:
            self.setChanged.emit(self.set_number)

    def select_object(self, obj):
        with QSignalBlocker(self.tree):
            selected = None
            for index in range(self.tree.topLevelItemCount()):
                item = self.tree.topLevelItem(index)
                candidate = item.data(0, GAME_OBJECT_ROLE)
                if obj is not None and (candidate.kind, candidate.object_id) == (obj.kind, obj.object_id):
                    selected = item
                    break
            self.tree.setCurrentItem(selected)

    def enable_state_column(self):
        self._state_column = True
        self.tree.setHeaderLabels(["Game object", "State", "Type", "ID"])
        configure_resource_tree(self.tree)
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            obj = item.data(0, GAME_OBJECT_ROLE)
            key = (self.set_number, obj.kind, obj.object_id)
            item.setText(1, self._visual_states.get(key, self.default_visual_state(obj)))
            item.setText(2, obj.kind.title())
            item.setText(3, str(obj.object_id))

    def set_visual_state(self, obj, state):
        self._visual_states[(self.set_number, obj.kind, obj.object_id)] = state
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            candidate = item.data(0, GAME_OBJECT_ROLE)
            if (candidate.kind, candidate.object_id) == (obj.kind, obj.object_id):
                item.setText(1, state)
                break

    @staticmethod
    def default_visual_state(obj):
        if obj.kind == 'building':
            return 'Intact'
        return next((state.title() for state in ('normal', 'fire', 'wait', 'dead', 'megadeth', 'genesis', 'launch')
                     if any(f'{prefix}_{state}' in obj.values for prefix in ('vp', 'base', '3ds'))), '—')
