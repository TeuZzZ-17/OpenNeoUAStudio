"""Shared Studio themes, Theme menu and File Backup menu, used by every workspace.

Every theme is one JSON file in ``studio/themes``. Adding a file there adds a theme
to the Theme menu of every workspace: no code change is needed. A theme file holds:

* ``id`` / ``name`` / ``description`` / ``order``: identity and menu position;
* ``mode``: ``dark`` or ``light`` (selects the arrow/check icons and the status colors);
* ``palette``: the Qt palette roles (also used by ``studio.qss`` as ``@Role`` tokens);
* ``tone.Accent``: color of metrics and warnings;
* ``lists``: row colors (hover, selection of the model rows, unresolved assets, modified rows);
* ``syntax``: colors of the script editor.

Green "loaded" status colors are not part of a theme: they carry meaning.
"""
from pathlib import Path
import json
import re
from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QAction, QActionGroup, QColor, QPalette
from PySide6.QtWidgets import QApplication, QProxyStyle, QStyle

THEMES_DIR = Path(__file__).resolve().parents[1] / "themes"
DEFAULT_THEME = "resistance"
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_PALETTE_KEYS = (
    "Window", "WindowText", "Base", "AlternateBase", "Text", "Button", "ButtonText",
    "Highlight", "HighlightedText", "Mid", "Midlight", "Light", "Dark", "ToolTipBase",
    "ToolTipText", "Disabled.Text", "Disabled.WindowText", "Disabled.ButtonText")
_LIST_KEYS = ("Hover", "Selected", "DependencySelected", "Problem",
              "ModifiedSelected", "ModifiedHover")
_SYNTAX_KEYS = ("key", "function", "number", "string", "comment", "line_number", "error")


class StudioStyle(QProxyStyle):
    """Fusion with sliders that jump to the clicked point instead of moving page by page."""

    def styleHint(self, hint, option=None, widget=None, returnData=None):  # noqa: N802
        if hint == QStyle.StyleHint.SH_Slider_AbsoluteSetButtons:
            return int(Qt.MouseButton.LeftButton.value)
        return super().styleHint(hint, option, widget, returnData)


def _valid(theme: dict) -> bool:
    """A theme is usable only when every required color is a #rrggbb value."""
    try:
        if theme["mode"] not in ("dark", "light") or not str(theme["id"]).strip():
            return False
        groups = ((theme["palette"], _PALETTE_KEYS), (theme["lists"], _LIST_KEYS),
                  (theme["syntax"], _SYNTAX_KEYS), (theme["tone"], ("Accent",)))
        return all(_HEX.match(str(group[key])) for group, keys in groups for key in keys)
    except (KeyError, TypeError):
        return False


def load_themes() -> dict[str, dict]:
    """Read every valid theme file; broken files are skipped, never fatal."""
    themes: dict[str, dict] = {}
    for path in sorted(THEMES_DIR.glob("*.json")):
        try:
            theme = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(theme, dict) and _valid(theme):
            theme.setdefault("name", str(theme["id"]))
            theme.setdefault("description", "")
            theme.setdefault("order", 100)
            themes[str(theme["id"])] = theme
    return dict(sorted(themes.items(), key=lambda item: (item[1]["order"], item[0])))


def theme_list_color(name: str, fallback: str = "#808080") -> QColor:
    """Row color of the current theme (``Hover``, ``DependencySelected``, ``Problem``...)."""
    app = QApplication.instance()
    colors = (app.property("studioListColors") if app is not None else None) or {}
    return QColor(colors.get(name, fallback))


def apply_studio_style(theme: str | None = None, *, persist: bool = False) -> None:
    """Apply one theme to every open editor without changing document state."""
    app = QApplication.instance()
    if theme is None and app.property("studioStyleLoaded"):
        return
    settings = QSettings("OpenNeoUA", "Studio")
    themes = load_themes()
    theme = theme or str(settings.value("ui/theme", DEFAULT_THEME))
    if theme not in themes:
        theme = DEFAULT_THEME if DEFAULT_THEME in themes else next(iter(themes))
    data = themes[theme]
    mode = data["mode"]
    if not app.property("studioStyleLoaded"):
        app.setStyle(StudioStyle("Fusion"))
    sheet = Path(__file__).with_name("studio.qss").read_text(encoding="utf-8")
    colors = dict(data["palette"])
    palette = QPalette()
    for name, color in colors.items():
        role = getattr(QPalette.ColorRole, name.removeprefix("Disabled."))
        if name.startswith("Disabled."):
            palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(color))
        else:
            palette.setColor(role, QColor(color))
    colors["Accent"] = data["tone"]["Accent"]
    for key, value in data["lists"].items():
        colors["List" + key] = value
    icons = Path(__file__).resolve().parents[2] / "icons"
    for name, shape in (("ArrowDown", "down"), ("ArrowUp", "up"),
                        ("CheckMark", "check"), ("PartialMark", "partial")):
        colors[name] = (icons / f"studio-{mode}-{shape}.png").as_posix()
    app.setProperty("studioSyntaxColors", dict(data["syntax"]))
    app.setProperty("studioListColors", dict(data["lists"]))
    app.setProperty("studioTheme", theme)
    app.setProperty("studioThemeMode", mode)
    app.setPalette(palette)
    sheet = re.sub(r"@((?:Disabled\.)?\w+)", lambda match: colors[match[1]], sheet)
    app.setStyleSheet(sheet)
    app.setProperty("studioStyleLoaded", True)
    from studio.ui.pointer_cursors import install_pointer_cursors
    install_pointer_cursors(app)
    from studio.ui.widgets import apply_status_colors_for_mode
    apply_status_colors_for_mode(mode)
    for widget in app.allWidgets():
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


def load_backup_setting() -> bool:
    """Apply the saved Backup choice to the writers; disabled when nothing was saved."""
    from studio.exchange.portable import set_backup_enabled
    enabled = QSettings("OpenNeoUA", "Studio").value("backup/enabled", False, type=bool)
    set_backup_enabled(enabled)
    return enabled


def set_backup_option(enabled: bool) -> None:
    """Enable or disable the ``.bak`` safety copy in every workspace and remember the choice."""
    from studio.exchange.portable import set_backup_enabled
    set_backup_enabled(enabled)
    QSettings("OpenNeoUA", "Studio").setValue("backup/enabled", enabled)
    for window in QApplication.topLevelWidgets():
        for action in window.findChildren(QAction):
            choice = action.property("studioBackupChoice")
            if choice:
                action.setChecked((choice == "enable") == enabled)


def install_backup_menu(window) -> None:
    """Add the File Backup menu (Enabled / Disabled); when Enabled, a file about to be
    overwritten is first copied to ``<name>.bak``."""
    enabled = load_backup_setting()
    menu = window.menuBar().addMenu("File Backup")
    group = QActionGroup(menu)
    group.setExclusive(True)
    for choice, title in (("enable", "Enabled"), ("disable", "Disabled")):
        action = menu.addAction(title)
        action.setCheckable(True)
        action.setToolTip("Save a .bak copy of a file before it is overwritten."
                          if choice == "enable" else "Overwrite files without a .bak copy (default).")
        action.setProperty("studioBackupChoice", choice)
        action.setChecked((choice == "enable") == enabled)
        group.addAction(action)
        action.triggered.connect(lambda checked=False, value=(choice == "enable"):
                                 set_backup_option(value))
    window.studio_backup_menu = menu


def install_theme_menu(window) -> None:
    """List every theme found in ``studio/themes`` in the workspace Theme menu."""
    menu = window.menuBar().addMenu("Theme")
    group = QActionGroup(menu)
    group.setExclusive(True)
    current = QApplication.instance().property("studioTheme")
    for theme_id, data in load_themes().items():
        action = menu.addAction(data["name"])
        action.setCheckable(True)
        action.setToolTip(data["description"])
        action.setProperty("studioThemeChoice", theme_id)
        action.setChecked(current == theme_id)
        group.addAction(action)
        action.triggered.connect(lambda checked=False, choice=theme_id:
                                 apply_studio_style(choice, persist=True))
    window.studio_theme_menu = menu
