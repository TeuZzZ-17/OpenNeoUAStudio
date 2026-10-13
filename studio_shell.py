"""Shared resources, compatibility and workspace navigation for Studio."""

from PySide6.QtWidgets import QApplication, QMessageBox

from studio_resources import ResourcesDialog


def install_workspace_shell(window, key):
    if hasattr(window, "studio_change_menu"):
        return
    from startup_selector import TOOL_OPTIONS

    menu_bar = window.menuBar()
    resources = menu_bar.addMenu("Resources")
    resources.addAction("Game Folders", lambda: _edit_resources(window))
    window.studio_compat_action = menu_bar.addAction("Vanilla Compatibility Checker")
    window.studio_compat_action.triggered.connect(
        lambda: _show_compatibility(window, key))
    change = menu_bar.addMenu("Change Editor")
    for option in TOOL_OPTIONS:
        action = change.addAction(option.title)
        action.setCheckable(True)
        current = option.key == key
        action.setChecked(current)
        action.setEnabled(not current)
        action.triggered.connect(lambda _checked=False, target=option.key: switch_workspace(window, target))
    window.studio_change_menu = change
    from studio_compat import install_compatibility_markers
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
    if key == "sector_editor":
        QMessageBox.information(source, "Sector Editor", "Sector Editor will be available soon.\nIt will edit sector placement, states and SET.SDF definitions.")
        return
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
    from studio_compat import CompatibilityDialog
    dialog = CompatibilityDialog(window, key)
    dialog.exec()
