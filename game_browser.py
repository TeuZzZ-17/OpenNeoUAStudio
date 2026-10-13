"""Common vertical resource browsers used by Studio workspaces."""

import os
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QSignalBlocker
from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QMenu,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from map_editor import bootstrap
from game_object_catalog import load_game_objects

GAME_OBJECT_ROLE = int(Qt.ItemDataRole.UserRole) + 64


def configure_resource_tree(tree):
    from resource_status import use_resource_delegate
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

    def __init__(self, parent=None, tree=None, inline_set_selector=True, kinds=None):
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
        self.set_combo = QComboBox()
        controls.addWidget(self.category, 1)
        # A host that shares the set between several panels passes
        # inline_set_selector=False and places ``set_combo`` itself.
        if inline_set_selector:
            controls.addWidget(self.set_combo)
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
        self.reload()

    def _context_menu(self, item):
        menu = QMenu(self)
        obj = item.data(0, GAME_OBJECT_ROLE) if item is not None else None
        if obj is not None:
            for label, value in (('Copy Name', obj.name), ('Copy ID', str(obj.object_id)), ('Copy Script Path', str(obj.source))):
                menu.addAction(label, lambda _checked=False, text=value, what=label: self._copy(what, text))
        return menu

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
        from studio_session import selected_set
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
