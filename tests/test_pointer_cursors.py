"""Clickable Studio controls show a pointing hand; custom cursors are left alone."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QLineEdit, QPushButton, QRadioButton, QSlider, QTabBar,
    QToolButton, QWidget,
)

from studio.ui.cursor_themes import cursor_kind, studio_cursor
from studio.ui.style import apply_studio_style

HAND = "point"
HAND_SHAPE = Qt.CursorShape.PointingHandCursor
OPEN = "open"
CLOSED = "closed"


@pytest.fixture(scope="module")
def app():
    from studio.ui.cursor_themes import set_cursor_theme

    app = QApplication.instance() or QApplication([])
    apply_studio_style()
    # apply_studio_style restores the cursor theme saved on this machine: tests start from the default.
    set_cursor_theme("classic")
    return app


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path, monkeypatch):
    """Cursor-theme tests save their choice: never into the user's real settings."""

    from PySide6.QtCore import QSettings
    from studio.ui import cursor_themes

    real = QSettings
    monkeypatch.setattr(cursor_themes, "QSettings",
                        lambda *args: real(str(tmp_path / "studio.ini"), real.Format.IniFormat))


def _shown(app, widget):
    widget.show()
    app.processEvents()
    return widget


def test_every_kind_of_clickable_control_gets_the_hand(app):
    host = QWidget()
    controls = [QPushButton("a", host), QToolButton(host), QCheckBox("b", host), QRadioButton("c", host),
                QSlider(Qt.Orientation.Horizontal, host), QComboBox(host), QTabBar(host)]
    _shown(app, host)
    for control in controls:
        # Sliders get the open hand of a grab; everything else clickable the pointing hand.
        expected = OPEN if isinstance(control, QSlider) else HAND
        assert cursor_kind(control.cursor()) == expected, type(control).__name__
    host.close()


def test_a_disabled_control_has_the_arrow_and_gets_the_hand_back_when_enabled(app):
    button = QPushButton("x")
    button.setEnabled(False)
    _shown(app, button)
    assert button.cursor().shape() == Qt.CursorShape.ArrowCursor
    button.setEnabled(True)
    app.processEvents()
    assert cursor_kind(button.cursor()) == HAND
    button.setEnabled(False)
    assert button.cursor().shape() == Qt.CursorShape.ArrowCursor
    button.close()


def test_a_cursor_chosen_by_the_widget_is_kept(app):
    button = QPushButton("x")
    button.setCursor(Qt.CursorShape.CrossCursor)
    _shown(app, button)
    assert button.cursor().shape() == Qt.CursorShape.CrossCursor
    button.setEnabled(False)
    button.setEnabled(True)
    assert button.cursor().shape() == Qt.CursorShape.CrossCursor
    button.close()


def test_text_fields_keep_their_own_cursor(app):
    field = QLineEdit()
    _shown(app, field)
    assert cursor_kind(field.cursor()) != HAND
    field.close()


def test_the_slider_hand_closes_while_the_handle_is_held(app):
    slider = QSlider(Qt.Orientation.Horizontal)
    slider.resize(300, 30)
    _shown(app, slider)
    assert cursor_kind(slider.cursor()) == OPEN
    QTest.mousePress(slider, Qt.MouseButton.LeftButton, pos=slider.rect().center())
    assert cursor_kind(slider.cursor()) == CLOSED
    QTest.mouseRelease(slider, Qt.MouseButton.LeftButton, pos=slider.rect().center())
    assert cursor_kind(slider.cursor()) == OPEN
    slider.setEnabled(False)
    assert slider.cursor().shape() == Qt.CursorShape.ArrowCursor
    slider.close()


def test_a_click_on_a_slider_groove_puts_the_handle_there(app):
    for orientation in (Qt.Orientation.Horizontal, Qt.Orientation.Vertical):
        slider = QSlider(orientation)
        slider.setRange(0, 100)
        slider.setValue(0)
        slider.resize(300, 30) if orientation == Qt.Orientation.Horizontal else slider.resize(30, 300)
        _shown(app, slider)
        rect = slider.rect()
        point = (QPoint(int(rect.width()*0.8), rect.center().y()) if orientation == Qt.Orientation.Horizontal
                 else QPoint(rect.center().x(), int(rect.height()*0.2)))
        QTest.mouseClick(slider, Qt.MouseButton.LeftButton, pos=point)
        # One click lands near the clicked point (the old page-by-page step would give 10).
        assert 65 <= slider.value() <= 90, (orientation, slider.value())
        slider.close()


def test_the_wireframe_detail_slider_has_the_grab_hand(app):
    from entity_designer.wireframe_panel import WireframePanel

    class Viewport:
        def indexed_renderer_info(self):
            return {"available": False}

    import entity_designer.wireframe_panel as module
    original = module._load_wireframe_generator
    module._load_wireframe_generator = lambda: (lambda viewport, **kwargs: (_ for _ in ()).throw(ValueError("no model")))
    try:
        dialog = WireframePanel(Viewport())
        _shown(app, dialog)
        assert cursor_kind(dialog.detail_slider.cursor()) == OPEN
        dialog.close()
        dialog.deleteLater()
        app.sendPostedEvents(None, 0)
    finally:
        module._load_wireframe_generator = original


def test_the_default_cursor_theme_is_the_classic_windows_hand(app):
    from studio.ui.cursor_themes import DEFAULT_THEME, active_cursor_theme

    from studio.ui.cursor_themes import restore_cursor_theme

    assert DEFAULT_THEME == "classic" and active_cursor_theme() == "classic"
    assert restore_cursor_theme() == "classic"       # nothing saved (isolated settings)
    assert studio_cursor("point").shape() == Qt.CursorShape.PointingHandCursor
    assert studio_cursor("closed").shape() == Qt.CursorShape.ClosedHandCursor


def test_every_theme_gives_each_role_its_own_cursor_with_the_hot_spot_inside(app):
    from studio.ui.cursor_themes import THEMES, ROLES, set_cursor_theme

    try:
        for theme in THEMES:
            set_cursor_theme(theme)
            seen = []
            for role in ROLES:
                cursor = studio_cursor(role)
                assert cursor_kind(cursor) == role, (theme, role)
                if cursor.shape() == Qt.CursorShape.BitmapCursor:
                    pixmap = cursor.pixmap()
                    assert not pixmap.isNull()
                    assert 0 <= cursor.hotSpot().x() < pixmap.width()
                    assert 0 <= cursor.hotSpot().y() < pixmap.height()
                    seen.append(pixmap.toImage().cacheKey())
                else:
                    seen.append(int(cursor.shape().value))
            assert len(set(seen)) == len(ROLES), theme
    finally:
        set_cursor_theme("classic")
    assert cursor_kind(QCursor(Qt.CursorShape.IBeamCursor)) is None


def test_the_studio_glove_is_smaller_than_the_large_one(app):
    from studio.ui.cursor_themes import set_cursor_theme

    try:
        set_cursor_theme("studio_hand")
        small = studio_cursor("point").pixmap().width()
        set_cursor_theme("studio_hand_large")
        assert small < studio_cursor("point").pixmap().width() <= 34
    finally:
        set_cursor_theme("classic")


def test_switching_theme_changes_open_widgets_and_keeps_widget_made_cursors(app):
    from studio.ui.cursor_themes import set_cursor_theme

    host = QWidget()
    button = QPushButton("a", host)
    slider = QSlider(Qt.Orientation.Horizontal, host)
    own = QPushButton("b", host)
    own.setCursor(QCursor(Qt.CursorShape.IBeamCursor))
    _shown(app, host)
    try:
        assert cursor_kind(button.cursor()) == "point" and button.cursor().shape() == HAND_SHAPE
        set_cursor_theme("studio_hand")
        assert button.cursor().shape() == Qt.CursorShape.BitmapCursor
        assert cursor_kind(button.cursor()) == "point" and cursor_kind(slider.cursor()) == "open"
        set_cursor_theme("precision")
        assert button.cursor().shape() == Qt.CursorShape.ArrowCursor
        assert cursor_kind(slider.cursor()) == "open"
        set_cursor_theme("classic")
        assert button.cursor().shape() == HAND_SHAPE
        assert own.cursor().shape() == Qt.CursorShape.IBeamCursor
    finally:
        set_cursor_theme("classic")
        host.close()


def test_the_cursor_theme_choice_is_saved_and_unknown_ids_fall_back(app, tmp_path):
    from PySide6.QtCore import QSettings
    from studio.ui.cursor_themes import restore_cursor_theme, set_cursor_theme

    try:
        assert set_cursor_theme("studio_hand", persist=True) == "studio_hand"
        set_cursor_theme("classic")
        assert restore_cursor_theme() == "studio_hand"
        assert set_cursor_theme("does_not_exist") == "classic"
    finally:
        set_cursor_theme("classic", persist=True)


def test_the_theme_menu_groups_colors_and_cursors_and_switches_cursors(app):
    from PySide6.QtWidgets import QMainWindow
    from studio.ui.cursor_themes import THEMES, set_cursor_theme
    from studio.ui.style import install_theme_menu

    window = QMainWindow()
    install_theme_menu(window)
    try:
        assert [a.text() for a in window.menuBar().actions()] == ["Theme"]
        assert [a.text() for a in window.studio_theme_menu.actions()] == ["Colors", "Cursors"]
        assert all(a.menu() is not None for a in window.studio_theme_menu.actions())
        choices = {a.property("studioCursorThemeChoice"): a for a in window.studio_cursor_menu.actions()}
        assert set(choices) == set(THEMES) and choices["classic"].isChecked()
        choices["studio_hand"].trigger()
        assert choices["studio_hand"].isChecked() and not choices["classic"].isChecked()
        assert studio_cursor("point").shape() == Qt.CursorShape.BitmapCursor
    finally:
        set_cursor_theme("classic", persist=True)
        window.close()


def test_editors_do_not_use_system_cursors():
    """Cursors come from studio.ui.cursor_themes so every tab looks the same."""

    root = Path(__file__).resolve().parent.parent
    allowed = {root / "studio" / "ui" / "cursor_themes.py"}
    offenders = []
    for folder in ("studio", "entity_designer", "map_editor", "sector_editor", "model_editor",
                   "wireframe_editor", "snapshot_studio"):
        for path in (root / folder).rglob("*.py"):
            if path not in allowed and "CursorShape." in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(root)))
    assert not offenders, offenders
