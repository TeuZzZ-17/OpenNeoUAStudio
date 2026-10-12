from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest.mock import patch

import pytest
from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QApplication, QMainWindow, QWidget

import gpu_widget
from assembly_viewer import AssetViewport
from map_editor.core.ldf_model import LdfDocument
import map_editor.render.gpu_viewport as map_gpu
from render_status import add_renderer_badge


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class FakeCanvas(QWidget):
    """QWidget canvas stub that never creates an OpenGL context."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.release_calls = 0
        self.delete_later_calls = 0

    def isValid(self):
        return True

    def release_resources(self):
        self.release_calls += 1

    def deleteLater(self):  # noqa: N802 - Qt API
        self.delete_later_calls += 1
        super().deleteLater()


def _window_with_badge(viewport):
    window = QMainWindow()
    window.setCentralWidget(viewport)
    badge = add_renderer_badge(window, viewport)
    return window, badge


def _set_asset_state(view):
    family = object()
    view._family_ref = family
    view._yaw = 41.0
    view._pitch = 17.5
    view._zoom = 1.8
    view._pan = QPointF(12.0, -7.0)
    view._center = (2.0, 3.0, 4.0)
    view._scale = 1.25
    view._selected_poly = 9
    view._selected_owner = "Data/Models/Base/vehicle.base"
    view._highlight_polys = {2, 9}
    camera = view._camera_state()
    return family, camera


def _assert_asset_state(view, family, camera):
    assert view._family_ref is family
    assert view._camera_state() == camera
    assert view._selected_poly == 9
    assert view._selected_owner == "Data/Models/Base/vehicle.base"
    assert view._highlight_polys == {2, 9}


def _set_map_state(view):
    doc = LdfDocument(mw=7, mh=5)
    view.set_document(doc)
    view.selection.add((2, 3))
    view.camera.yaw = 123.0
    view.camera.pitch = 48.0
    view.camera.zoom = 0.24
    view.camera.pan = (6.0, -8.0)
    view.camera.center = (1200.0, -30.0, -2400.0)
    camera_ref = view.camera
    camera = view.camera.copy()
    selection = view.selection
    selected = set(selection)
    return doc, camera_ref, camera, selection, selected


def _assert_map_state(view, doc, camera_ref, camera, selection, selected):
    assert view.doc is doc
    assert view.camera is camera_ref
    assert view.camera == camera
    assert view.selection is selection
    assert view.selection == selected


def test_renderer_badge_clicks_toggle_generic_backend_with_state_intact(app):
    with (
        patch("gpu_widget._use_opengl_canvas", return_value=True),
        patch("gpu_widget.Canvas", FakeCanvas),
    ):
        view = AssetViewport()
        window, badge = _window_with_badge(view)
        family, camera = _set_asset_state(view)
        first_canvas = view._gpu_canvas
        try:
            assert badge.text() == "GPU"
            assert "Click to switch to Software" in badge.toolTip()

            badge.click()
            assert view.renderer_mode == "software"
            assert badge.text() == "Software"
            assert window.statusBar().currentMessage() == "Renderer switched to Software."
            assert first_canvas.release_calls == 1
            assert first_canvas.delete_later_calls == 1
            _assert_asset_state(view, family, camera)

            badge.click()
            second_canvas = view._gpu_canvas
            assert view.renderer_mode == "gpu"
            assert badge.text() == "GPU"
            assert window.statusBar().currentMessage() == "Renderer switched to GPU."
            assert second_canvas is not first_canvas
            _assert_asset_state(view, family, camera)
        finally:
            if view.renderer_mode == "gpu":
                view.set_renderer_mode("software")
            window.close()


def test_unavailable_gpu_keeps_generic_state_and_reports_status(app):
    with patch("gpu_widget._use_opengl_canvas", return_value=False):
        view = AssetViewport()
        window, badge = _window_with_badge(view)
        family, camera = _set_asset_state(view)
        try:
            assert view.renderer_mode == "software"
            badge.click()
            assert view.renderer_mode == "software"
            assert "OpenGL 3.3 is unavailable" in window.statusBar().currentMessage()
            assert badge.text() == "Software"
            _assert_asset_state(view, family, camera)
        finally:
            window.close()


def test_map_gpu_software_gpu_roundtrip_preserves_document_camera_and_selection(app):
    with (
        patch("map_editor.render.map_viewport.load_owner_colors", return_value={}),
        patch("map_editor.render.gpu_viewport.Canvas", FakeCanvas),
        patch("map_editor.render.gpu_viewport.available", return_value=True),
    ):
        view = map_gpu.GpuMapViewport(enable_gpu=True)
        doc, camera_ref, camera, selection, selected = _set_map_state(view)
        first_canvas = view._canvas
        try:
            assert view.renderer_mode == "gpu"
            assert view.set_renderer_mode("software")
            assert view.renderer_mode == "software"
            assert first_canvas.release_calls == 1
            assert first_canvas.delete_later_calls == 1
            _assert_map_state(view, doc, camera_ref, camera, selection, selected)

            assert view.set_renderer_mode("gpu")
            second_canvas = view._canvas
            assert view.renderer_mode == "gpu"
            assert second_canvas is not first_canvas
            _assert_map_state(view, doc, camera_ref, camera, selection, selected)
        finally:
            if view.renderer_mode == "gpu":
                view.set_renderer_mode("software")
            view.close()
            view._pool.waitForDone(10000)


def test_unavailable_map_gpu_keeps_document_camera_selection_and_status(app):
    with (
        patch("map_editor.render.map_viewport.load_owner_colors", return_value={}),
        patch("map_editor.render.gpu_viewport.available", return_value=False),
    ):
        view = map_gpu.GpuMapViewport(enable_gpu=False)
        window, badge = _window_with_badge(view)
        doc, camera_ref, camera, selection, selected = _set_map_state(view)
        try:
            assert view.renderer_mode == "software"
            badge.click()
            assert view.renderer_mode == "software"
            assert "OpenGL 3.3 is unavailable" in window.statusBar().currentMessage()
            assert badge.text() == "Software"
            _assert_map_state(view, doc, camera_ref, camera, selection, selected)
        finally:
            window.close()
            view._pool.waitForDone(10000)


def test_map_software_picking_uses_the_software_frame(app):
    with patch("map_editor.render.map_viewport.load_owner_colors", return_value={}):
        view = map_gpu.GpuMapViewport(enable_gpu=False)
        try:
            with patch("map_editor.render.map_viewport.MapViewport._pick_code", return_value=-42) as pick:
                assert view._pick_code(12, 18) == -42
                pick.assert_called_once_with(12, 18)
        finally:
            view.close()


def test_badge_switches_active_preview_and_main_view_together(app):
    with patch("gpu_widget._use_opengl_canvas", return_value=True), patch("gpu_widget.Canvas", FakeCanvas):
        main = AssetViewport()
        preview = AssetViewport()
        window, badge = _window_with_badge(main)
        badge.add_viewport(preview)
        try:
            badge.set_viewport(preview)
            badge.click()
            assert main.renderer_mode == preview.renderer_mode == "software"
            assert badge.text() == "Software"
            badge.click()
            assert main.renderer_mode == preview.renderer_mode == "gpu"
            badge.set_viewport(main)
            assert badge.text() == "GPU"
        finally:
            main.set_renderer_mode("software")
            preview.set_renderer_mode("software")
            preview.close()
            window.close()
