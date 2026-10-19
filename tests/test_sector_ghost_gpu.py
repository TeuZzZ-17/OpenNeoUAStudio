"""Real OpenGL acceptance for camera-stable, depth-tested sector ghosts."""

import os
from copy import deepcopy

import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from PySide6.QtOpenGL import QOpenGLFramebufferObject
from OpenGL import GL

from map_editor import bootstrap
from map_editor.render.gpu_viewport import available
from sector_editor import SectorEditorWindow


def test_gpu_ghost_remains_grey_and_present_through_full_camera_orbit():
    app = QApplication.instance() or QApplication([])
    if os.environ.get("NME_RENDERER", "").casefold() == "software" or not available():
        pytest.skip("Run with QT_QPA_PLATFORM=windows and OpenGL 3.3")
    try:
        bootstrap.game_data_dir()
    except RuntimeError:
        pytest.skip("Game assets not configured")
    window = SectorEditorWindow()
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    window.show()
    window.sectors.setCurrentRow(142)
    window.tabs.setCurrentWidget(window.models_page)
    window.models.setCurrentRow(15)
    QTest.qWait(100)
    view = window.view
    before = deepcopy(window.document.sdf)
    target = None
    current = False
    try:
        assert view.renderer is not None and view._ghost_mesh is not None
        library, ghost = view.lib, view._ghost_mesh
        view._canvas.makeCurrent()
        current = True
        target = QOpenGLFramebufferObject(384, 320)
        for yaw in range(0, 360, 30):
            view.camera.yaw = yaw
            view._paint_gpu()
            camera = view.camera.copy()
            camera.width, camera.height, camera.pan = 384, 320, (0, 0)
            camera.zoom = .16
            view.renderer.render(camera, 384, 320, target.handle(),
                owner_colors={}, grid=False, sky=False, overlays=False,
                transparent_background=True)
            image = target.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
            rgba = np.frombuffer(image.constBits(), np.uint8).reshape(320, 384, 4)
            ids = view.renderer.read_ids()
            mask = ids == -2
            assert mask.any(), yaw
            rgb = rgba[mask, :3].astype(int)
            assert np.all(np.max(rgb, axis=1) - np.min(rgb, axis=1) <= 1), yaw
            assert view.lib is library and view._ghost_mesh is ghost
        assert window.document.sdf == before
        assert not view.renderer_error and not view._overlay_errors
    finally:
        target = None
        if current:
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, view._canvas.defaultFramebufferObject())
            view._canvas.doneCurrent()
        window._model_snapshot = window._state_snapshot = None
        window.document.saved = deepcopy(window.document.sdf)
        window.close()
