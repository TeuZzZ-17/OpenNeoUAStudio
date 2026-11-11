import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QMenu, QPushButton

from studio.workbench.window import AssemblyWindow


def test_emitter_commands_keep_copy_and_cut_without_redundant_options(monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = AssemblyWindow()
    try:
        monkeypatch.setattr(window, "_selected_emitter", lambda: (0, object()))
        monkeypatch.setattr(window, "_active_fx_selection", lambda: None)
        viewport_menu = window._create_viewport_context_menu()
        viewport_labels = {action.text() for action in viewport_menu.actions()}
        removed = {"Clone Emitter", "Move Emitter", "Paste Emitter"}
        assert {"Copy Emitter", "Cut Emitter"} <= viewport_labels
        assert not removed & viewport_labels

        emitter_labels = []
        class RecordingMenu(QMenu):
            def exec(self, *_):
                emitter_labels.extend(action.text() for action in self.actions())

        monkeypatch.setattr("studio.workbench.window_emitters.QMenu", RecordingMenu)
        window._show_emitter_context_menu(QPoint())
        assert {"Copy Emitter", "Cut Emitter"} <= set(emitter_labels)
        assert not removed & set(emitter_labels)

        assert not removed & {
            action.text() for action in window.findChildren(QAction)}
        assert "Clone Emitter" not in {
            button.text() for button in window.findChildren(QPushButton)}
    finally:
        window.close()
