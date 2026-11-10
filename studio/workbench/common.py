"""Shared pieces of the asset workbench window: item roles, small widgets,
path helpers and the Add FX Element dialog."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDoubleSpinBox, QTreeWidget

from studio.workbench.effect_catalog import AddFxElementDialog as AddFxElementDialog


WINDOW_TITLE = "OpenNeoUA Studio"


_BAS_KIND_ROLE = int(Qt.ItemDataRole.UserRole) + 1


_BAS_NAME_ROLE = int(Qt.ItemDataRole.UserRole) + 2


class _TransformStepSpinBox(QDoubleSpinBox):
    """Keep full spin-box precision while presenting compact step text."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.display_mode = "move"

    def textFromValue(self, value: float) -> str:  # noqa: N802 - Qt override
        if self.display_mode == "move":
            return f"{value:.2f}"
        return f"{value:g}"


class _BasResourceTree(QTreeWidget):
    """Keep right-click purely contextual for every BAS resource class."""

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.RightButton:
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.RightButton:
            event.accept()
            return
        super().mouseReleaseEvent(event)


def _display_path(path: str | Path) -> Path:
    """Return a stable absolute path for window-title display."""

    return Path(path).expanduser().resolve(strict=False)


def _reveal_in_file_manager(path: str | Path) -> None:
    """Show a file in the system file manager: selected on Windows, its folder elsewhere."""
    if sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", str(path)])
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent)))
