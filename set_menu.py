"""Change Set menu of the Model Editor, Entity Designer and Snapshot Studio.

One menu changes the game set of the whole window. Each window keeps a hidden
set combo (``GameObjectBrowser.set_combo``) as the single holder of the current
set; the menu only drives it, so the existing loading and "unsaved edits"
checks run exactly as when the old visible selectors were used.
"""

from PySide6.QtGui import QActionGroup
from PySide6.QtWidgets import QFileDialog, QMessageBox

from map_editor import bootstrap

# Workspaces that have a Change Set menu (the Map Editor and the Wireframe
# Editor are left out: they do not switch sets this way).
SET_MENU_TOOLS = frozenset({
    "model_editor", "main_suite", "entity_designer", "snapshot_studio"})


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
    from set_tabs import fill_set_combo
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
