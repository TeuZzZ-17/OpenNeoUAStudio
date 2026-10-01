"""Open the integrated OpenNeoUA Studio Map Editor."""

from __future__ import annotations

import os
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from . import bootstrap
from .core.game_installation import GameInstallation, load_remembered


_active_window = None
_active_app = None


def create_window(argv=None, parent=None):
    """Create and show the Map Editor, reusing the active Qt application."""

    global _active_app, _active_window

    args = list(sys.argv[1:] if argv is None else argv)
    app_args = [sys.argv[0], *args]
    app = QApplication.instance()
    if app is None:
        app = QApplication(app_args)
    _active_app = app

    install = load_remembered()
    if install is None:
        hint = os.environ.get("NME_GAME_DATA")
        suggested = GameInstallation.suggest(hint) if hint else None
        from .ui.dialogs import GameInstallationDialog

        folders = GameInstallationDialog(suggested, parent)
        if not folders.exec():
            return None
        install = folders.installation()

    bootstrap.set_installation(install)
    bootstrap.ensure_studio_on_path()

    from .core.ldf_model import LdfDocument, load_ldf
    from .ui.dialogs import NewMapDialog
    from .ui.main_window import MainWindow

    path = args[0] if args else None
    if path:
        doc = load_ldf(path)
    else:
        dialog = NewMapDialog(parent)
        if not dialog.exec():
            return None
        width, height, set_number = dialog.values()
        doc = LdfDocument(mw=width, mh=height, set_number=set_number)

    window = MainWindow(doc, path)
    if parent is not None:
        window.setParent(parent, Qt.WindowType.Window)
    window.setWindowTitle("OpenNeoUA Studio - Map Editor")
    window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    _active_window = window
    window.show()
    return window


def main(argv=None):
    """Launch the integrated editor, preserving an existing QApplication."""

    args = list(sys.argv[1:] if argv is None else argv)
    app = QApplication.instance() or QApplication([sys.argv[0], *args])
    if create_window(args) is None:
        return 0
    return app.exec()
