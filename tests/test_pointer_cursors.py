"""Clickable Studio controls show a pointing hand; custom cursors are left alone."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QLineEdit, QPushButton, QRadioButton, QSlider, QTabBar,
    QToolButton, QWidget,
)

from studio.ui.style import apply_studio_style

HAND = Qt.CursorShape.PointingHandCursor
OPEN = Qt.CursorShape.OpenHandCursor
CLOSED = Qt.CursorShape.ClosedHandCursor


@pytest.fixture(scope="module")
def app():
    app = QApplication.instance() or QApplication([])
    apply_studio_style()
    return app


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
        assert control.cursor().shape() == expected, type(control).__name__
    host.close()


def test_a_disabled_control_has_the_arrow_and_gets_the_hand_back_when_enabled(app):
    button = QPushButton("x")
    button.setEnabled(False)
    _shown(app, button)
    assert button.cursor().shape() == Qt.CursorShape.ArrowCursor
    button.setEnabled(True)
    app.processEvents()
    assert button.cursor().shape() == HAND
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
    assert field.cursor().shape() != HAND
    field.close()


def test_the_slider_hand_closes_while_the_handle_is_held(app):
    slider = QSlider(Qt.Orientation.Horizontal)
    slider.resize(300, 30)
    _shown(app, slider)
    assert slider.cursor().shape() == OPEN
    QTest.mousePress(slider, Qt.MouseButton.LeftButton, pos=slider.rect().center())
    assert slider.cursor().shape() == CLOSED
    QTest.mouseRelease(slider, Qt.MouseButton.LeftButton, pos=slider.rect().center())
    assert slider.cursor().shape() == OPEN
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
        assert dialog.detail_slider.cursor().shape() == OPEN
        dialog.close()
        dialog.deleteLater()
        app.sendPostedEvents(None, 0)
    finally:
        module._load_wireframe_generator = original
