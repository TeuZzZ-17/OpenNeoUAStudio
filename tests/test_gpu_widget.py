import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QSurfaceFormat
from PySide6.QtWidgets import QApplication

import gpu_widget
from gpu_widget import AcceleratedWidget


class _FormatProbe:
    def __init__(self, major, minor, profile):
        self._major = major
        self._minor = minor
        self._profile = profile

    def majorVersion(self):
        return self._major

    def minorVersion(self):
        return self._minor

    def profile(self):
        return self._profile


class _PaintProbe(AcceleratedWidget):
    def __init__(self):
        super().__init__()
        self.paint_count = 0

    def _paint_viewport(self, painter):
        self.paint_count += 1
        painter.fillRect(self.rect(), QColor(34, 56, 78))


class AcceleratedWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_software_mode_uses_widget_paint_surface(self):
        previous = os.environ.get("NME_RENDERER")
        os.environ["NME_RENDERER"] = "software"
        try:
            widget = _PaintProbe()
        finally:
            if previous is None:
                os.environ.pop("NME_RENDERER", None)
            else:
                os.environ["NME_RENDERER"] = previous
        self.addCleanup(widget.close)
        widget.resize(120, 80)
        self.assertIsNone(widget._gpu_canvas)

        widget.show()
        self.app.processEvents()

        self.assertGreater(widget.paint_count, 0)
        self.assertEqual(
            widget.grab().toImage().pixelColor(8, 8), QColor(34, 56, 78))

    def _probe_mocks(self, *, created=True, valid=True, major=3, minor=3,
                     profile=QSurfaceFormat.OpenGLContextProfile.CoreProfile):
        surface = Mock()
        surface.isValid.return_value = True
        context = Mock()
        context.create.return_value = created
        context.isValid.return_value = valid
        context.format.return_value = _FormatProbe(major, minor, profile)
        return surface, context

    def test_failed_context_creation_selects_software_widget(self):
        surface, context = self._probe_mocks(created=False, valid=False)
        with (
            patch.dict(os.environ, {"NME_RENDERER": "gpu"}),
            patch.object(gpu_widget.QGuiApplication, "platformName",
                         return_value="windows"),
            patch("gpu_widget.QOffscreenSurface", return_value=surface),
            patch("gpu_widget.QOpenGLContext", return_value=context),
        ):
            widget = _PaintProbe()

        self.addCleanup(widget.close)
        self.assertIsNone(widget._gpu_canvas)
        context.create.assert_called_once()
        surface.destroy.assert_called_once()

    def test_probe_rejects_context_below_opengl_33(self):
        surface, context = self._probe_mocks(major=3, minor=2)
        with (
            patch("gpu_widget.QOffscreenSurface", return_value=surface),
            patch("gpu_widget.QOpenGLContext", return_value=context),
        ):
            self.assertFalse(gpu_widget._has_opengl_33_context())

        context.makeCurrent.assert_not_called()
        surface.destroy.assert_called_once()

    def test_probe_accepts_opengl_33_core_without_changing_current_context(self):
        surface, context = self._probe_mocks()
        current = object()
        context_type = Mock(return_value=context)
        context_type.currentContext.return_value = current
        with (
            patch("gpu_widget.QOffscreenSurface", return_value=surface),
            patch("gpu_widget.QOpenGLContext", context_type),
        ):
            self.assertTrue(gpu_widget._has_opengl_33_context())

        self.assertIs(context_type.currentContext(), current)
        context.makeCurrent.assert_not_called()
        surface.destroy.assert_called_once()


if __name__ == "__main__":
    unittest.main()
