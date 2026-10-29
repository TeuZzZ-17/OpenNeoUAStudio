"""Studio shell shared by every workspace: session set, resource selection, compatibility
checker, Change Set menu, tool selector and the shell installer.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QEvent, QTimer, Qt, Signal
from PySide6.QtGui import QActionGroup, QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from map_editor import bootstrap
from map_editor.core.game_installation import (
    FOLDERS,
    forget_remembered,
    GameInstallation,
    load_remembered,
    save_remembered,
)

# ----------------------------------------------------------------------------
# Selected game set shared by tools in the same Studio application.
# (formerly studio_session.py)
# ----------------------------------------------------------------------------

def selected_set():
    app, install = QApplication.instance(), bootstrap.installation()
    data = getattr(install, 'data', None)
    if app is None or data is None:
        return 1
    stored = app.property('studioSelectedSets') or {}
    number = stored.get(str(data.resolve()).casefold(), 1)
    available = install.available_sets()
    return number if number in available else (1 if 1 in available or not available else available[0])


def remember_set(number):
    app, install = QApplication.instance(), bootstrap.installation()
    data = getattr(install, 'data', None)
    if app is None or data is None or number not in install.available_sets():
        return
    stored = app.property('studioSelectedSets') or {}
    stored[str(data.resolve()).casefold()] = number
    app.setProperty('studioSelectedSets', stored)


# ----------------------------------------------------------------------------
# Shared resource selection for every OpenNeoUA Studio workspace.
# (formerly studio_resources.py)
# ----------------------------------------------------------------------------

# One sheet for the floating dialog and the startup selector, so both look the same.
RESOURCES_STYLE = """
QDialog#resourcesDialog { background: palette(window); }
ResourcesPanel { background: palette(base); border: 1px solid palette(mid); border-radius: 6px; }
ResourcesPanel QLabel { color: palette(window-text); background: transparent; }
ResourcesPanel QLineEdit {
    background: palette(base); color: palette(text); border: 1px solid palette(mid);
    border-radius: 4px; padding: 3px 6px; selection-background-color: palette(highlight);
}
ResourcesPanel QLineEdit:focus { border: 1px solid palette(highlight); }
ResourcesPanel QPushButton {
    background: palette(button); color: palette(button-text); border: 1px solid palette(mid);
    border-radius: 4px; padding: 3px 12px; min-width: 0; min-height: 0;
}
ResourcesPanel QPushButton:hover { background: palette(alternate-base); border-color: palette(highlight); }
QDialog#resourcesDialog QDialogButtonBox QPushButton {
    background: palette(button); color: palette(button-text); border: 1px solid palette(mid);
    border-radius: 4px; padding: 5px 18px; min-width: 70px; min-height: 0;
}
QDialog#resourcesDialog QDialogButtonBox QPushButton:hover { background: palette(alternate-base); border-color: palette(highlight); }
QDialog#resourcesDialog QDialogButtonBox QPushButton:default {
    background: palette(highlight); border-color: palette(highlight); color: palette(highlighted-text); font-weight: 600;
}
"""


class ResourcesPanel(QWidget):
    changed = Signal()
    reset_done = Signal()

    def __init__(self, current=None, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(RESOURCES_STYLE)
        self.preview_backgrounds = dict(current.preview_backgrounds) if current else {}
        self._suggested_data = current.data.resolve() if current else None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(5)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)
        self.root = QLineEdit(str(current.data) if current else "")
        self.root.setPlaceholderText("Game Data folder")
        self._add_row(grid, 0, "Game installation", self.root, self._suggest)
        self.edits = {}
        for row, name in enumerate(FOLDERS, start=1):
            edit = QLineEdit(str(current.folder(name)) if current else "")
            self.edits[name] = edit
            if name == "sets":
                edit.setToolTip("Folder containing Set1, Set2... or, as in vanilla, the folder "
                                "where the loose set1, set2... folders sit. A single SetN folder also works.")
            self._add_row(grid, row, name.capitalize(), edit)
            edit.editingFinished.connect(self._refresh_inventory)
        self.root.editingFinished.connect(self._suggest)

        footer = QHBoxLayout()
        self.inventory_label = QLabel()
        self.inventory_label.setObjectName("resourcesSummary")
        self.inventory_label.setWordWrap(True)
        footer.addWidget(self.inventory_label, 1)
        self.reset_button = QPushButton("Reset Configuration")
        self.reset_button.setObjectName("resetButton")
        self.reset_button.setAutoDefault(False)
        self.reset_button.clicked.connect(self.reset_configuration)
        footer.addWidget(self.reset_button)
        layout.addLayout(footer)
        self._refresh_inventory()

    def _add_row(self, grid, row, label, edit, after=None):
        grid.addWidget(QLabel(label), row, 0)
        grid.addWidget(edit, row, 1)
        button = QPushButton("Browse")
        button.setAutoDefault(False)
        button.clicked.connect(lambda: self._browse(edit, after))
        grid.addWidget(button, row, 2)

    def _browse(self, edit, after):
        chosen = QFileDialog.getExistingDirectory(self, "Select resource folder", edit.text())
        if chosen:
            edit.setText(chosen)
            (after or self._refresh_inventory)()

    def _suggest(self):
        if self.root.text().strip():
            install = GameInstallation.suggest(self.root.text().strip())
            self.root.setText(str(install.data))
            if install.data != self._suggested_data:
                self._suggested_data = install.data
                self.preview_backgrounds.clear()
                for name, edit in self.edits.items():
                    edit.setText(str(install.folder(name)))
            self._refresh_inventory()

    def installation(self):
        if not self.root.text().strip():
            return None
        suggested = GameInstallation.suggest(self.root.text().strip())
        return GameInstallation(suggested.data, {
            name: Path(edit.text().strip()).expanduser().resolve()
            if edit.text().strip() else suggested.folder(name)
            for name, edit in self.edits.items()
        }, dict(self.preview_backgrounds))

    def _refresh_inventory(self):
        install = self.installation()
        if install is None or not install.valid():
            self.inventory_label.setText("Select the game installation.")
        else:
            counts = install.inventory()
            self.inventory_label.setText(
                f"Found: {counts['sets']} sets · {counts['scripts']} scripts · "
                f"{counts['levels']} levels · {counts['models']} models · "
                f"{counts['mb']} briefings · {counts['db']} debriefings")
        self.changed.emit()

    def reset_configuration(self):
        """Forget the saved installation and empty every field, as on first launch."""
        answer = QMessageBox.question(
            self, "Reset Configuration",
            "Clear the game installation and all saved folders?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return False
        forget_remembered()
        self.preview_backgrounds.clear()
        self._suggested_data = None
        self.root.clear()
        for edit in self.edits.values():
            edit.clear()
        self._refresh_inventory()
        self.reset_done.emit()
        return True

    def apply(self):
        install = self.installation()
        if install is None or not install.valid():
            QMessageBox.warning(self, "Resources", "Choose an existing game Data folder containing Sets.")
            return None
        save_remembered(install)
        bootstrap.set_installation(install)
        return install


class ResourcesDialog(QDialog):
    def __init__(self, current=None, parent=None):
        super().__init__(parent)
        self.setObjectName("resourcesDialog")
        self.setStyleSheet(RESOURCES_STYLE)
        self.setWindowTitle("Resources")
        self.resize(640, 0)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 12)
        self.panel = ResourcesPanel(current or bootstrap.installation() or load_remembered(), self)
        layout.addWidget(self.panel)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._apply)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _apply(self):
        if self.panel.apply() is not None:
            self.accept()

    def installation(self):
        return self.panel.installation()




# ----------------------------------------------------------------------------
# Shared compatibility reference, displayed only in the checker dialog.
# (formerly studio_compat.py)
# ----------------------------------------------------------------------------

VANILLA = "vanilla"


MIXED = "mixed"


OPENNEOUA = "openneoua"


PREVIEW = "preview"


UNKNOWN = "unknown"


@dataclass(frozen=True)
class CompatStyle:
    title: str
    color: str
    description: str


COMPAT_STYLES = {
    VANILLA: CompatStyle("Vanilla + OpenNeoUA", "#5cbf72", "Uses vanilla data formats."),
    MIXED: CompatStyle("Partly vanilla", "#e3c447", "Includes OpenNeoUA extensions."),
    OPENNEOUA: CompatStyle("OpenNeoUA only", "#e8873a", "Not supported by vanilla Urban Assault."),
    PREVIEW: CompatStyle("Preview / Studio only", "#5cbf72", "Does not change game data."),
    UNKNOWN: CompatStyle("Depends on the data", "#e3c447", "Check the target game's supported assets and parameters."),
}


def compat_color(level: str) -> QColor:
    return QColor(COMPAT_STYLES[level].color)


def compatibility_entries(window, key):
    """Describe each workspace using its existing pages and supported output formats."""
    if key == "sector_editor":
        doc = getattr(window, "document", None)
        extended = doc is not None and (len(doc.sdf.models) > 256 or len(doc.sdf.buildings) > 256)
        return [("Sector definitions", OPENNEOUA if extended else VANILLA,
                 "Writes native SET.SDF tables and LEGO visual overrides, and packages all set resources. Sector IDs stay 0–255. Tables above 256 model/building rows require the updated OpenNeoUA engine. Compatibility also depends on the selected models, materials and scripts.")]
    if key == "entity_designer":
        tabs = window.properties_tabs
        return [(tabs.tabText(i), *window._tab_compatibility[tabs.widget(i)])
                for i in range(tabs.count())]
    if key == "map_editor":
        tabs = window.palette_tabs
        rows = []
        for i in range(tabs.count()):
            title = tabs.tabText(i)
            if title == 'Locations':
                rows.append((title, OPENNEOUA, 'Optional begin_location polygons, entry titles and sounds require OpenNeoUA. Levels without locations contain no new blocks.'))
            elif title == "Level Info":
                rows.append((title, MIXED, "Standard level properties use vanilla LDF data. Optional begin_briefing_text requires OpenNeoUA and overrides the language-file briefing. Empty text writes no extension and keeps the language fallback. Remove the text for vanilla export."))
            elif title == "Script":
                rows.append((title, MIXED, "Vanilla level script sections remain compatible. Custom script text can contain OpenNeoUA-only parameters; this reference does not validate those lines."))
            else:
                rows.append((title, VANILLA, "Edits existing vanilla LDF level data. Referenced units, buildings, sectors and SET assets must also exist in the target game's installation."))
        return rows
    resources = [
        ("BAS Manager", PREVIEW, "Browsing and extracting SET.BAS resources does not change the archive. Extracted assets retain their source format and compatibility."),
        ("Entity List", PREVIEW, "Selects entities and visual states for the editor. Availability in vanilla depends on the source scripts and assets; listing an entity does not make it vanilla-compatible."),
    ]
    if key == "model_editor":
        return resources + [
            ("Models and Textures", MIXED, "BASE/SKLT geometry, mapping and indexed ILBM/VBMP textures use vanilla formats. External models referenced through OpenNeoUA-only script parameters require OpenNeoUA; the .onua3d exchange format is for Studio import/export, not direct vanilla loading."),
            ("Wireframe Generator", VANILLA, "Shows, generates and exports the vanilla SKLT wireframe named by the entity's wireframe key. Export writes only that file; the script must still reference it."),
            ("Asset Dependencies / Asset Textures", PREVIEW, "Inspects the model's resources and textures. Preview substitutions are session-only; exported assets must satisfy the target game's format and palette requirements."),
            ("Import / Export", MIXED, "Exporting supported native assets preserves their format. PNG, OBJ and .onua3d are presentation or exchange files and require conversion before use as vanilla assets."),
        ]
    if key == "snapshot_studio":
        return [("Snapshot", PREVIEW, "Camera, background, lights, guides and image export affect the presentation only. A screenshot does not alter the entity's runtime compatibility.")] + resources
    if key == "wireframe_editor":
        return [("Wireframe geometry / Save / Export", VANILLA, "Edits the existing 2D SKLT wireframe format. The game must still reference the exported resource correctly."),
                ("View and editing guides", PREVIEW, "Zoom, selection, grid and symmetry guides belong to Studio and add no game parameters.")]
    return []


def install_compatibility_markers(window, key):
    """Use the checker's compatibility entries for every workspace tab marker."""
    from PySide6.QtWidgets import QTabWidget
    palettes = [tabs._palette_strip for tabs in window.findChildren(QTabWidget)
                if hasattr(tabs, "_palette_strip")]
    map_tabs = getattr(window, "palette_tabs", None)
    if not palettes and map_tabs is None:
        return
    entries = compatibility_entries(window, key)
    by_title = {title.casefold(): (level, note)
                for names, level, note in entries for title in names.split(" / ")}
    for name in ("Log", "Warnings", "Validation"):
        by_title[name.casefold()] = (PREVIEW, "Studio diagnostics do not change game data.")
    for strip in palettes:
        markers = {}
        for leaf in strip._collect():
            name = strip._leaf_text(leaf)
            level, note = by_title.get(name.casefold(), (UNKNOWN, COMPAT_STYLES[UNKNOWN].description))
            markers[strip._leaf_page(leaf)] = compat_color(level)
        strip.set_markers(markers)
        for button in strip.buttons():
            level, note = by_title.get(button.text().casefold(), (UNKNOWN, COMPAT_STYLES[UNKNOWN].description))
            button.setToolTip(f"{COMPAT_STYLES[level].title}. {note}")
    if map_tabs is not None and hasattr(map_tabs, "_buttons"):
        for button in map_tabs._buttons:
            level, note = by_title[button.text().casefold()]
            button.marker = compat_color(level)
            button.setToolTip(f"{COMPAT_STYLES[level].title}. {note}")
            button.update()


class CompatibilityDialog(QDialog):
    """Scrollable compatibility reference for the editor currently open."""

    def __init__(self, window, key):
        super().__init__(window)
        title = next(option.title for option in TOOL_OPTIONS if option.key == key)
        self.setWindowTitle(f"Vanilla Compatibility Checker — {title}")
        self.resize(740, 620)
        layout = QVBoxLayout(self)
        heading = QLabel(title)
        heading.setStyleSheet("font-size: 19px; font-weight: 600;")
        layout.addWidget(heading)
        intro = QLabel("Compatibility of the editor's features and output formats. This reference does not scan the current file or certify custom assets.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        legend = QLabel("Tab dots: green = vanilla formats or preview without data changes; yellow = mixed or dependent on the data; orange = OpenNeoUA only.")
        legend.setWordWrap(True)
        layout.addWidget(legend)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        cards = QVBoxLayout(content)
        cards.setContentsMargins(0, 0, 8, 0)
        self.entries = compatibility_entries(window, key)
        for name, level, note in self.entries:
            style = COMPAT_STYLES[level]
            card = QFrame()
            card.setStyleSheet("QFrame { background: palette(alternate-base); border-radius: 6px; }")
            row = QVBoxLayout(card)
            label = QLabel(f"{name} · {style.title}")
            label.setWordWrap(True)
            label.setStyleSheet(f"color: {style.color}; font-size: 14px; font-weight: 600;")
            row.addWidget(label)
            detail = QLabel(note)
            detail.setWordWrap(True)
            detail.setTextFormat(Qt.TextFormat.PlainText)
            detail.setStyleSheet("color: palette(window-text); padding: 2px;")
            row.addWidget(detail)
            cards.addWidget(card)
        cards.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


# ----------------------------------------------------------------------------
# Change Set menu of the Model Editor, Entity Designer and Snapshot Studio.
# (formerly set_menu.py)
# ----------------------------------------------------------------------------

# Workspaces that have a Change Set menu (the Map Editor and the Wireframe
# Editor are left out: they do not switch sets this way).
SET_MENU_TOOLS = frozenset({
    "model_editor", "main_suite", "entity_designer", "snapshot_studio", "sector_editor"})


def set_combo_of(window):
    """The hidden combo that holds the window's current set."""

    combo = getattr(window, "_set_combo", None)
    if combo is None:
        browser = getattr(window, "game_object_browser", None)
        combo = getattr(browser, "set_combo", None)
    return combo


def install_set_menu(window, menu_bar):
    """Add "Change Set" to ``menu_bar`` and return its menu."""

    menu = menu_bar.addMenu("Change Set")
    menu.aboutToShow.connect(lambda: _fill_menu(window, menu))
    window.studio_set_menu = menu
    return menu


def _fill_menu(window, menu):
    menu.clear()
    install = bootstrap.installation()
    combo = set_combo_of(window)
    current = combo.currentData() if combo is not None else None
    numbers = install.available_sets() if install is not None else []
    custom = install.custom_sets if install is not None else {}
    group = QActionGroup(menu)
    group.setExclusive(True)
    for number in numbers:
        label = f"Set {number}" + (" (custom)" if number in custom else "")
        action = menu.addAction(label)
        action.setCheckable(True)
        action.setChecked(number == current)
        group.addAction(action)
        action.triggered.connect(
            lambda _checked=False, target=number: change_game_set(window, target))
    if not numbers:
        menu.addAction("No sets found").setEnabled(False)
    menu.addSeparator()
    menu.addAction("Browse…", lambda: browse_for_set(window))


def change_game_set(window, number, *, force=False):
    """Switch the window to set ``number`` through its set combo.

    ``force`` runs the change even when the number is already selected (a
    hand-picked folder can replace the files of the current set).
    """

    combo = set_combo_of(window)
    install = bootstrap.installation()
    if combo is None or install is None:
        return
    from studio.ui.tabs import fill_set_combo
    if force or combo.findData(number) < 0:
        fill_set_combo(combo, install, selected=combo.currentData() or number)
    index = combo.findData(number)
    if index < 0:
        return
    if index == combo.currentIndex():
        if force:
            combo.currentIndexChanged.emit(index)
        return
    combo.setCurrentIndex(index)


def browse_for_set(window):
    """Let the user pick a SetN folder in the file explorer and switch to it."""

    install = bootstrap.installation()
    if install is None:
        QMessageBox.information(
            window, "Change Set",
            "Choose the game folders first (Resources > Game Folders).")
        return
    chosen = QFileDialog.getExistingDirectory(
        window, "Select a set folder (Set1, Set2, …)", str(install.folder("sets")))
    if not chosen:
        return
    try:
        number = install.register_set(chosen)
    except ValueError as exc:
        QMessageBox.warning(window, "Change Set", str(exc))
        return
    change_game_set(window, number, force=True)


# ----------------------------------------------------------------------------
# Shared resources, compatibility and workspace navigation for Studio.
# (formerly studio_shell.py)
# ----------------------------------------------------------------------------

def install_workspace_shell(window, key):
    if hasattr(window, "studio_change_menu"):
        return

    menu_bar = window.menuBar()
    resources = menu_bar.addMenu("Resources")
    resources.addAction("Game Folders", lambda: _edit_resources(window))
    from studio.ui.style import install_backup_menu, install_theme_menu
    install_backup_menu(window)
    install_theme_menu(window)
    if key in SET_MENU_TOOLS:
        install_set_menu(window, menu_bar)
    change = menu_bar.addMenu("Change Editor")
    for option in TOOL_OPTIONS:
        action = change.addAction(option.title)
        current = option.key == key
        # Only the current editor is checkable: a checkable entry ticks itself on click and
        # would stay ticked when the old editor refuses to close and the switch is cancelled.
        action.setCheckable(current)
        action.setChecked(current)
        action.setEnabled(not current)
        action.triggered.connect(lambda _checked=False, target=option.key: switch_workspace(window, target))
    window.studio_change_menu = change
    window.studio_compat_action = menu_bar.addAction("Vanilla Compatibility Checker")
    window.studio_compat_action.triggered.connect(
        # Finish the menu click before opening a modal window.
        lambda: QTimer.singleShot(0, window, lambda: _show_compatibility(window, key)))
    install_compatibility_markers(window, key)


def _edit_resources(window):
    dialog = ResourcesDialog(parent=window)
    if not dialog.exec():
        return
    # Several tool windows can be alive (e.g. one opened from another), so refresh all of them.
    for other in QApplication.topLevelWidgets():
        refresh = getattr(other, "reload_game_resources", None)
        if refresh is not None:
            refresh()


def switch_workspace(source, key):
    from main import create_workspace
    target = create_workspace(key)
    if target is None:
        return
    target.showMaximized()
    # The old tool owns the unsaved-changes prompts; if the user refuses, keep it and drop the new one.
    if source.close():
        source.deleteLater()
    else:
        target.close()
        target.deleteLater()
        source.raise_()
        source.activateWindow()


def _show_compatibility(window, key):
    """Show compatibility of the current editor without occupying its panels."""
    dialog = CompatibilityDialog(window, key)
    try:
        dialog.exec()
    finally:
        window.menuBar().setActiveAction(None)
        window.menuBar().clearFocus()
        dialog.deleteLater()


# ----------------------------------------------------------------------------
# Startup tool selector for OpenNeoUA Studio.
# (formerly startup_selector.py)
# ----------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolOption:
    """One workspace offered by the startup selector."""

    key: str
    title: str
    description: str


TOOL_OPTIONS = (
    ToolOption(
        "model_editor",
        "Model Editor",
        "View, inspect, and edit textured Urban Assault 3D models, asset "
        "families, UVs, materials, and animations.",
    ),
    ToolOption(
        "snapshot_studio",
        "Snapshot Studio",
        "Load textured models and create clean presentation snapshots with "
        "camera presets, animation controls, and export options.",
    ),
    ToolOption(
        "map_editor",
        "Map Editor",
        "Create and edit Urban Assault maps with the GPU terrain viewport.",
    ),
    ToolOption(
        "entity_designer",
        "Entity Designer",
        "Edit collision shapes, fire points, turrets, cockpit views and "
        "briefing framing for game entities.",
    ),
    ToolOption(
        "wireframe_editor",
        "Wireframe Editor",
        "Inspect and edit 2D wireframe geometry stored in SKLT files.",
    ),
    ToolOption("sector_editor", "Sector Editor", "Create sets, compose sector cells and damage states, and export complete set packages."),
)


class _ToolCard(QFrame):
    """One fixed workspace card inside the static launcher panel."""

    clicked = Signal(str)
    double_clicked = Signal(str)

    def __init__(self, option: ToolOption, parent=None) -> None:
        super().__init__(parent)
        self._key = option.key
        self.setObjectName("toolCard")
        self.setProperty("selected", False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setToolTip(option.description)
        self.setFixedHeight(72)

        card_layout = QVBoxLayout(self)
        card_layout.setContentsMargins(13, 8, 13, 8)
        card_layout.setSpacing(3)

        title = QLabel(option.title)
        title.setObjectName("toolTitle")
        title.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        description = QLabel(option.description)
        description.setObjectName("toolDescription")
        description.setWordWrap(True)
        description.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        card_layout.addWidget(title)
        card_layout.addWidget(description)

    @property
    def key(self) -> str:
        return self._key

    def set_selected(self, selected: bool) -> None:
        """Refresh the card's selected-state stylesheet."""

        if bool(self.property("selected")) == selected:
            return
        self.setProperty("selected", selected)
        style = self.style()
        style.unpolish(self)
        style.polish(self)
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._key)
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.double_clicked.emit(self._key)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Select):
            self.clicked.emit(self._key)
            event.accept()
            return
        super().keyPressEvent(event)


class _StaticToolPanel(QFrame):
    """True static workspace panel with no item-view scrolling machinery."""

    double_activated = Signal(str)

    def __init__(self, options: tuple[ToolOption, ...], parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("toolList")
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self._cards: list[_ToolCard] = []
        self._current_index = -1

        panel_layout = QVBoxLayout(self)
        panel_layout.setContentsMargins(6, 6, 6, 6)
        panel_layout.setSpacing(5)

        for option in options:
            card = _ToolCard(option, self)
            card.clicked.connect(self.set_current_key)
            card.double_clicked.connect(self._activate_key)
            panel_layout.addWidget(card)
            self._cards.append(card)

        # Keep this panel static, but let the wheel move the selection instead
        # of trying to scroll content that is already fully visible.
        self.installEventFilter(self)
        for card in self._cards:
            card.installEventFilter(self)

        card_height = 72
        spacing = panel_layout.spacing() * max(0, len(self._cards) - 1)
        margins = (
            panel_layout.contentsMargins().top()
            + panel_layout.contentsMargins().bottom()
        )
        self.setFixedHeight(card_height * len(self._cards) + spacing + margins)
        self.set_current_row(0)

    def count(self) -> int:
        """Compatibility helper retained for selector tests."""

        return len(self._cards)

    def current_row(self) -> int:
        return self._current_index

    def set_current_row(self, row: int) -> None:
        if not 0 <= row < len(self._cards):
            return
        self._set_current_index(row)

    def current_key(self) -> str | None:
        if not 0 <= self._current_index < len(self._cards):
            return None
        return self._cards[self._current_index].key

    def set_current_key(self, key: str) -> None:
        for index, card in enumerate(self._cards):
            if card.key == key:
                self._set_current_index(index)
                return

    def _set_current_index(self, index: int) -> None:
        self._current_index = index
        for card_index, card in enumerate(self._cards):
            card.set_selected(card_index == index)

    def _activate_key(self, key: str) -> None:
        self.set_current_key(key)
        self.double_activated.emit(key)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt override
        if (watched is self or watched in self._cards) and \
                event.type() == QEvent.Type.Wheel:
            delta = event.angleDelta().y() or event.pixelDelta().y()
            if delta:
                self.set_current_row(
                    self._current_index + (1 if delta < 0 else -1))
                event.accept()
                return True
        return super().eventFilter(watched, event)


class StartupToolSelector(QDialog):
    """Ask which OpenNeoUA Studio workspace should be opened."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("OpenNeoUA Studio - Select Tool")
        self.setModal(True)
        self.setMinimumSize(720, 650)
        self.resize(860, 920)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)

        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QFrame()
        scroll.setWidget(body)
        outer.addWidget(scroll)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(28, 24, 28, 22)
        layout.setSpacing(12)

        title = QLabel("Choose a workspace")
        title.setObjectName("selectorTitle")
        title.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        layout.addWidget(title)

        subtitle = QLabel(
            "Select the tool that matches the task you want to perform."
        )
        subtitle.setObjectName("selectorSubtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        # This is a plain fixed QWidget layout, not a QListWidget/QAbstractItemView.
        # Selection can therefore never scroll, reposition or hide another card.
        self.tool_list = _StaticToolPanel(TOOL_OPTIONS, self)
        self.tool_list.double_activated.connect(
            lambda _key: self.accept())
        layout.addWidget(self.tool_list)

        from map_editor.core.game_installation import load_remembered
        self.resources_panel = ResourcesPanel(load_remembered(), self)
        for edit in (self.resources_panel.root, *self.resources_panel.edits.values()):
            edit.installEventFilter(self)
        layout.addWidget(self.resources_panel)

        # WidgetWithChildrenShortcut keeps navigation available when focus is
        # on either a workspace card or one of the dialog's buttons.
        self._selection_shortcuts = []
        for key, step in (
                (Qt.Key.Key_W, -1), (Qt.Key.Key_S, 1),
                (Qt.Key.Key_Up, -1), (Qt.Key.Key_Down, 1)):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(
                lambda direction=step: self._move_tool_selection(direction))
            self._selection_shortcuts.append(shortcut)

        # Enter always opens the highlighted workspace. A card can retain
        # keyboard focus while the mouse wheel moves the visual selection, so
        # the focused card must not decide which workspace gets launched.
        for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(self.accept)
            self._selection_shortcuts.append(shortcut)

        note = QLabel(
            "Each workspace shares the same OpenNeoUA Studio asset pipeline."
        )
        note.setObjectName("selectorNote")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)

        buttons_layout = QHBoxLayout()
        buttons_layout.addStretch(1)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        buttons_layout.addWidget(self.cancel_button)
        self.open_button = QPushButton("Open Tool")
        self.open_button.setObjectName("openToolButton")
        self.open_button.setDefault(True)
        self.open_button.clicked.connect(self.accept)
        buttons_layout.addWidget(self.open_button)
        outer.addLayout(buttons_layout)

        self.setStyleSheet(
            """
            QDialog {
                background: palette(window);
            }
            QLabel#selectorTitle {
                color: palette(window-text);
                font-size: 21px;
                font-weight: 600;
            }
            QLabel#selectorSubtitle, QLabel#selectorNote {
                color: palette(window-text);
                font-size: 11px;
            }
            QFrame#toolList {
                background: palette(base);
                border: 1px solid palette(mid);
                border-radius: 6px;
            }
            QFrame#toolCard {
                background: transparent;
                border: 1px solid transparent;
                border-radius: 5px;
            }
            QFrame#toolCard:hover {
                background: palette(alternate-base);
            }
            QFrame#toolCard[selected="true"] {
                background: palette(highlight);
                border: 1px solid palette(highlight);
            }
            QLabel#toolTitle {
                color: palette(window-text);
                font-size: 13px;
                font-weight: 600;
                background: transparent;
                border: none;
            }
            QLabel#toolDescription {
                color: palette(window-text);
                font-size: 11px;
                background: transparent;
                border: none;
            }
            QFrame#toolCard[selected="true"] QLabel {
                color: palette(highlighted-text);
            }
            QPushButton {
                min-width: 92px;
                min-height: 30px;
                padding: 4px 14px;
            }
            QPushButton#openToolButton {
                background: palette(highlight);
                border: 1px solid palette(highlight);
                border-radius: 4px;
                color: palette(highlighted-text);
                font-weight: 600;
            }
            QPushButton#openToolButton:hover {
                background: palette(highlight);
                border-color: palette(window-text);
            }
            """)

    def eventFilter(self, watched, event):
        if isinstance(watched, QLineEdit) and event.type() == QEvent.Type.ShortcutOverride:
            event.accept()
            return True
        return super().eventFilter(watched, event)

    def accept(self):
        if self.resources_panel.apply() is not None:
            super().accept()

    def selected_tool(self) -> str | None:
        """Return the selected tool key, if a card is selected."""

        return self.tool_list.current_key()

    def _move_tool_selection(self, step: int) -> None:
        current = self.tool_list.current_row()
        last = self.tool_list.count() - 1
        self.tool_list.set_current_row(max(0, min(last, current + step)))
