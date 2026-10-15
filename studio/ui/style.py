"""Shared Studio themes and menu, used by every workspace."""
from pathlib import Path
import re
from PySide6.QtCore import QSettings
from PySide6.QtGui import QAction, QActionGroup, QColor, QPalette
from PySide6.QtWidgets import QApplication


def apply_studio_style(theme: str | None = None, *, persist: bool = False) -> None:
    """Apply one theme to every open editor without changing document state."""
    app = QApplication.instance()
    if theme is None and app.property("studioStyleLoaded"):
        return
    settings = QSettings("OpenNeoUA", "Studio")
    theme = theme or str(settings.value("ui/theme", "dark"))
    theme = theme if theme in {"dark", "light"} else "dark"
    if not app.property("studioStyleLoaded"):
        app.setStyle("Fusion")
    sheet = Path(__file__).with_name("studio.qss").read_text(encoding="utf-8")
    colors = dict(re.findall(r"Palette\.((?:Disabled\.)?\w+)\s*=\s*(#[0-9a-fA-F]{6})", sheet))
    if theme == "light":
        colors.update(dict(re.findall(r"Light\.((?:Disabled\.)?\w+)\s*=\s*(#[0-9a-fA-F]{6})", sheet)))
    palette = QPalette()
    for name, color in colors.items():
        role = getattr(QPalette.ColorRole, name.removeprefix("Disabled."))
        if name.startswith("Disabled."):
            palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(color))
        else:
            palette.setColor(role, QColor(color))
    colors.update(dict(re.findall(r"(?<!Light)Tone\.(\w+)\s*=\s*(#[0-9a-fA-F]{6})", sheet)))
    if theme == "light":
        colors.update(dict(re.findall(r"LightTone\.(\w+)\s*=\s*(#[0-9a-fA-F]{6})", sheet)))
    icons = Path(__file__).resolve().parents[2] / "icons"
    for name, shape in (("ArrowDown", "down"), ("ArrowUp", "up"),
                        ("CheckMark", "check"), ("PartialMark", "partial")):
        colors[name] = (icons / f"studio-{theme}-{shape}.png").as_posix()
    app.setProperty("studioSyntaxColors", dict(re.findall(
        r"LightSyntax\.(\w+)\s*=\s*(#[0-9a-fA-F]{6})", sheet)) if theme == "light" else {})
    app.setProperty("studioTheme", theme)
    app.setPalette(palette)
    sheet = re.sub(r"@((?:Disabled\.)?\w+)", lambda match: colors[match[1]], sheet)
    app.setStyleSheet(sheet)
    app.setProperty("studioStyleLoaded", True)
    from studio.ui.widgets import SELECTION_STYLE
    for widget in app.allWidgets():
        if SELECTION_STYLE in widget.styleSheet():
            widget.setStyleSheet(widget.styleSheet().replace(SELECTION_STYLE, ""))
        refresh = getattr(widget, "refresh_studio_theme", None)
        if refresh:
            refresh()
        widget.update()
    for window in app.topLevelWidgets():
        for action in window.findChildren(QAction):
            choice = action.property("studioThemeChoice")
            if choice:
                action.setChecked(choice == theme)
    if persist:
        settings.setValue("ui/theme", theme)


def install_theme_menu(window) -> None:
    """Expose the shared Dark and Light themes in the workspace menu."""
    menu = window.menuBar().addMenu("Theme")
    group = QActionGroup(menu)
    group.setExclusive(True)
    for theme, title in (("dark", "Dark"), ("light", "Light")):
        action = menu.addAction(title)
        action.setCheckable(True)
        action.setProperty("studioThemeChoice", theme)
        action.setChecked(QApplication.instance().property("studioTheme") == theme)
        group.addAction(action)
        action.triggered.connect(lambda checked=False, choice=theme:
                                 apply_studio_style(choice, persist=True))
    window.studio_theme_menu = menu
