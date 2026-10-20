import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
os.environ.setdefault('NME_RENDERER','software')
import pytest
from PySide6.QtCore import QPoint,Qt
from PySide6.QtGui import QImage,QPainter,QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QAbstractItemView,QDockWidget,QLabel
from studio.ui.style import apply_studio_style
from studio.ui.uv_editor import UVEditorWidget
from model_editor.window import ModelEditorWindow
from map_editor.ui.main_window import MainWindow
from map_editor.core.ldf_model import LdfDocument

@pytest.fixture
def app():
    app=QApplication.instance() or QApplication([])
    apply_studio_style('dark')
    yield app
    apply_studio_style('dark')


def test_map_palette_is_fixed_and_preset_has_full_width(app, tmp_path, monkeypatch):
    from map_editor import bootstrap
    from map_editor.core.game_installation import GameInstallation
    monkeypatch.setattr(bootstrap, '_installation', GameInstallation.suggest(tmp_path))
    monkeypatch.setattr(MainWindow, '_ensure_lib', lambda self: None)
    window=MainWindow(LdfDocument(mw=15,mh=15,set_number=1))
    try:
        window.resize(1200,800);window.show();QTest.qWait(100)
        dock=window.palette_dock
        assert dock.features()==QDockWidget.DockWidgetFeature.NoDockWidgetFeatures
        assert dock.titleBarWidget() is not None
        assert not dock.windowTitle()
        assert dock.toggleViewAction() not in window.palette_menu.actions()
        assert not any(label.text()=='View Preset' for label in dock.findChildren(QLabel))
        assert window.view_preset.geometry().left()==window.hover_height.geometry().left()
        assert window.view_preset.width()==window.hover_height.width()
    finally:
        window.close()


@pytest.mark.parametrize('theme',['dark','metropolis'])
def test_model_tabs_stay_fixed_and_texture_rows_follow_palette(app,theme):
    apply_studio_style(theme)
    window=ModelEditorWindow()
    try:
        window.resize(1200,800);window.show();QTest.qWait(500)
        strip=window._right_tabs._palette_strip
        scroll=window._model_editor_panel
        # The palette never moves into the scroll area: same size and place on every tab.
        assert strip.parentWidget() is strip.container
        assert strip.parentWidget() is not scroll.widget()
        editor_geometry=[(b.x(),b.y(),b.width(),b.height()) for b in strip.buttons()]
        editor_origin=strip.mapTo(window,QPoint(0,0))
        window._right_tabs.setCurrentWidget(window._resources_tabs)
        app.processEvents()
        assert strip.parentWidget() is strip.container
        assert [(b.x(),b.y(),b.width(),b.height()) for b in strip.buttons()]==editor_geometry
        assert strip.mapTo(window,QPoint(0,0))==editor_origin
        assert not any(view.alternatingRowColors() for view in window.findChildren(QAbstractItemView))
        window._refresh_texture_usage_highlight()
        assert all(window.texture_list.item(i).foreground().style()==Qt.BrushStyle.NoBrush
                   for i in range(window.texture_list.count()))
    finally:
        window.close()


def test_uv_background_changes_without_changing_texture_pixels(app):
    widget=UVEditorWidget();widget.resize(320,220)
    image=QImage(16,16,QImage.Format.Format_ARGB32);image.fill(Qt.GlobalColor.red)
    widget.set_data(image,[],False)
    try:
        for theme in ('dark','metropolis'):
            apply_studio_style(theme)
            output=QImage(widget.size(),QImage.Format.Format_ARGB32)
            painter=QPainter(output);widget._paint_viewport(painter);painter.end()
            assert output.pixelColor(0,0)==app.palette().color(QPalette.ColorRole.Base)
            assert output.pixelColor(output.rect().center()).name()=='#ff0000'
        assert widget._image==image
    finally:
        widget.close()
