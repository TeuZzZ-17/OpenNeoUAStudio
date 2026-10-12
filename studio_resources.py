"""Shared resource selection for every OpenNeoUA Studio workspace."""

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from map_editor import bootstrap
from map_editor.core.game_installation import (
    FOLDERS, GameInstallation, forget_remembered, load_remembered,
    save_remembered,
)

# One sheet for the floating dialog and the startup selector, so both look the same.
RESOURCES_STYLE = """
QDialog#resourcesDialog { background: #15141b; }
ResourcesPanel { background: #1c1b23; border: 1px solid #34314a; border-radius: 6px; }
ResourcesPanel QLabel { color: #d9d6e6; background: transparent; }
ResourcesPanel QLabel#resourcesSummary { color: #a79fc9; }
ResourcesPanel QLineEdit {
    background: #14131a; color: #ece9f7; border: 1px solid #3a3752;
    border-radius: 4px; padding: 3px 6px; selection-background-color: #6d4fd6;
}
ResourcesPanel QLineEdit:focus { border: 1px solid #9575f2; }
ResourcesPanel QPushButton {
    background: #2a2838; color: #ece9f7; border: 1px solid #45415f;
    border-radius: 4px; padding: 3px 12px; min-width: 0; min-height: 0;
}
ResourcesPanel QPushButton:hover { background: #38344d; border-color: #9575f2; }
ResourcesPanel QPushButton#resetButton { color: #e3a7c0; }
QDialog#resourcesDialog QDialogButtonBox QPushButton {
    background: #2a2838; color: #ece9f7; border: 1px solid #45415f;
    border-radius: 4px; padding: 5px 18px; min-width: 70px; min-height: 0;
}
QDialog#resourcesDialog QDialogButtonBox QPushButton:hover { background: #38344d; border-color: #9575f2; }
QDialog#resourcesDialog QDialogButtonBox QPushButton:default {
    background: #6d4fd6; border-color: #9575f2; color: white; font-weight: 600;
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


def ensure_installation(parent=None):
    install = bootstrap.installation() or load_remembered()
    if install is None:
        dialog = ResourcesDialog(parent=parent)
        if not dialog.exec():
            return None
        install = dialog.installation()
    bootstrap.set_installation(install)
    return install
