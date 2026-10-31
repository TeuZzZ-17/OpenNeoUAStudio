import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent, QPalette, QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QMainWindow,
                              QMenu, QScrollBar, QStyle, QStyleOptionComboBox)
from studio.ui.style import apply_studio_style, install_theme_menu, load_themes
from studio.ui.code_editor import ScriptCodeEditor

@pytest.fixture
def app():
    app = QApplication.instance() or QApplication([])
    apply_studio_style('dark')
    yield app
    apply_studio_style('dark')


def test_theme_switch_updates_open_menus_and_preserves_script_undo(app):
    windows = [QMainWindow(), QMainWindow()]
    editor = ScriptCodeEditor()
    try:
        for window in windows:
            install_theme_menu(window)
        editor.setPlainText('begin_fx\n    lifetime = 400\nend')
        cursor = editor.editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText('\n; retained edit')
        editor.editor.setTextCursor(cursor)
        position = cursor.position()
        apply_studio_style('metropolis')
        light = load_themes()['metropolis']
        assert app.palette().color(QPalette.ColorRole.Base).name() == light['palette']['Base']
        assert editor._theme['key'] == light['syntax']['key']
        assert editor.editor.textCursor().position() == position
        assert editor.toPlainText().endswith('; retained edit')
        assert editor.editor.document().isUndoAvailable()
        for window in windows:
            choices = [a for a in window.studio_color_menu.actions() if a.property('studioThemeChoice')]
            assert choices[0].property('studioThemeChoice') == 'resistance'
            assert choices[-1].property('studioThemeChoice') == 'dark'
            assert not [a for a in window.studio_color_menu.actions() if a.isSeparator()]
            assert {a.property('studioThemeChoice') for a in choices} == set(load_themes())
            assert [a.property('studioThemeChoice') for a in choices if a.isChecked()] == ['metropolis']
        editor.editor.undo()
        assert '; retained edit' not in editor.toPlainText()
        apply_studio_style('dark')
        assert editor._theme['key'] == load_themes()['dark']['syntax']['key']
    finally:
        editor.close()
        for window in windows:
            window.close()


@pytest.mark.parametrize('theme', ['dark', 'metropolis'])
def test_submenu_hover_is_visible_and_fields_have_inset(app, theme):
    apply_studio_style(theme)
    menu = QMenu()
    submenu = menu.addMenu('Import')
    first = submenu.addAction('Import BAS Archive')
    submenu.addAction('Import BASE')
    combo = QComboBox()
    combo.addItem('4:3 (default / vanilla)')
    combo.resize(260, 36)
    try:
        combo.show()
        submenu.popup(QPoint(40, 40))
        QTest.qWait(30)
        QTest.mouseMove(submenu, QPoint(1, 1))
        QTest.qWait(10)
        before = submenu.grab().toImage()
        rect = submenu.actionGeometry(first)
        app.sendEvent(submenu, QMouseEvent(QEvent.Type.MouseMove, QPointF(rect.center()),
                      QPointF(submenu.mapToGlobal(rect.center())), Qt.MouseButton.NoButton,
                      Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))
        QTest.qWait(30)
        assert submenu.activeAction() is first
        after = submenu.grab().toImage()
        point = QPoint(rect.left() + 2, rect.center().y())
        assert before.pixelColor(point) != after.pixelColor(point)
        assert after.pixelColor(point).name() == app.palette().color(QPalette.ColorRole.Highlight).name()
        option = QStyleOptionComboBox()
        combo.initStyleOption(option)
        text_rect = combo.style().subControlRect(QStyle.ComplexControl.CC_ComboBox, option,
                                                QStyle.SubControl.SC_ComboBoxEditField, combo)
        assert text_rect.left() >= 8
        assert text_rect.right() < combo.width() - 15
    finally:
        menu.close()
        submenu.close()
        combo.close()


def test_checkboxes_and_scrollbars_share_style_across_windows(app):
    windows = [QMainWindow(), QMainWindow()]
    controls = []
    try:
        for window in windows:
            check = QCheckBox('Visible', window)
            check.setChecked(True)
            check.resize(120, 32)
            bar = QScrollBar(Qt.Orientation.Vertical, window)
            bar.setRange(0, 100)
            bar.setValue(30)
            bar.resize(12, 180)
            controls.append((check, bar))
        for theme in ('dark', 'metropolis'):
            apply_studio_style(theme)
            assert controls[0][0].grab().toImage() == controls[1][0].grab().toImage()
            assert controls[0][1].grab().toImage() == controls[1][1].grab().toImage()
            assert all(not control.styleSheet() for pair in controls for control in pair)
            image = controls[0][0].grab().toImage()
            indicator_colors = {image.pixelColor(x, y).name()
                                for x in range(18) for y in range(8, 25)}
            assert '#ffffff' in indicator_colors
            assert app.palette().highlight().color().name() in indicator_colors
    finally:
        for window in windows:
            window.close()


def test_menu_choice_is_remembered_for_next_application_start(app, monkeypatch):
    import studio.ui.style as style
    values = {}
    class Settings:
        def __init__(self, *args):
            pass
        def value(self, key, default):
            return values.get(key, default)
        def setValue(self, key, value):
            values[key] = value
    monkeypatch.setattr(style, "QSettings", Settings)
    window = QMainWindow()
    try:
        install_theme_menu(window)
        next(a for a in window.studio_color_menu.actions()
             if a.property('studioThemeChoice') == 'metropolis').trigger()
        assert values == {"ui/theme": "metropolis"}
        app.setProperty("studioStyleLoaded", False)
        apply_studio_style()
        assert app.property("studioTheme") == "metropolis"
    finally:
        window.close()


def test_every_theme_file_is_valid_and_readable(app):
    """Each theme in studio/themes must load, apply and keep text readable on its backgrounds."""
    def lum(color):
        r, g, b = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
        f = lambda v: v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)

    def ratio(a, b):
        hi, lo = sorted((lum(a), lum(b)), reverse=True)
        return (hi + 0.05) / (lo + 0.05)

    themes = load_themes()
    assert len(themes) >= 10 and {'dark', 'resistance'} <= set(themes) and 'light' not in themes
    for theme_id, theme in themes.items():
        pal = theme['palette']
        assert ratio(pal['Text'], pal['Base']) >= 7, theme_id
        assert ratio(pal['WindowText'], pal['Window']) >= 7, theme_id
        assert ratio(pal['HighlightedText'], pal['Highlight']) >= 4.5, theme_id
        assert ratio(pal['Text'], theme['lists']['Hover']) >= 4.5, theme_id
        assert ratio(pal['Text'], theme['lists']['DependencySelected']) >= 4.5, theme_id
        assert ratio(pal['Text'], theme['lists']['Problem']) >= 4.5, theme_id
        assert ratio(theme['tone']['Accent'], pal['Window']) >= 4.5, theme_id
        for name, color in theme['syntax'].items():
            if name not in ('error', 'line_number'):
                assert ratio(color, pal['Base']) >= 4.5, (theme_id, name)
        apply_studio_style(theme_id)
        assert app.property('studioTheme') == theme_id
        assert app.property('studioThemeMode') == theme['mode']
        assert app.palette().color(QPalette.ColorRole.Base).name() == pal['Base'].lower()


def test_unknown_theme_falls_back_to_the_default_theme(app):
    apply_studio_style('does-not-exist')
    assert app.property('studioTheme') == 'resistance'
