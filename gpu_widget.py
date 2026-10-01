"""QWidget paint surface backed by a native Qt OpenGL canvas when available."""

from __future__ import annotations

import os
import weakref

from PySide6.QtCore import Qt
from PySide6.QtGui import (
    QGuiApplication,
    QOffscreenSurface,
    QOpenGLContext,
    QPainter,
    QSurfaceFormat,
)
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QApplication, QWidget


def _use_opengl_canvas() -> bool:
    if os.environ.get("NME_RENDERER", "").strip().casefold() == "software":
        return False
    if QApplication.instance() is None:
        return False
    if QGuiApplication.platformName().casefold() in {
        "offscreen", "minimal", "minimalegl",
    }:
        return False
    return _has_opengl_33_context()


def _surface_format() -> QSurfaceFormat:
    surface = QSurfaceFormat()
    surface.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
    surface.setVersion(3, 3)
    surface.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    surface.setDepthBufferSize(24)
    surface.setStencilBufferSize(8)
    return surface


def _has_opengl_33_context() -> bool:
    """Probe the requested format without disturbing an active GL context.

    Creating a context and an offscreen surface is synchronous and does not
    enter the Qt event loop. The probe deliberately never calls
    ``makeCurrent``: a caller's current context, if any, stays current.
    """

    requested = _surface_format()
    surface = None
    context = None
    try:
        surface = QOffscreenSurface()
        surface.setFormat(requested)
        surface.create()
        if not surface.isValid():
            return False

        context = QOpenGLContext()
        context.setFormat(requested)
        if not context.create() or not context.isValid():
            return False

        actual = context.format()
        actual_version = (actual.majorVersion(), actual.minorVersion())
        required_version = (requested.majorVersion(), requested.minorVersion())
        return (
            actual_version >= required_version
            and actual.profile()
            == QSurfaceFormat.OpenGLContextProfile.CoreProfile
        )
    except RuntimeError:
        return False
    finally:
        if surface is not None:
            try:
                surface.destroy()
            except RuntimeError:
                pass
        # QOpenGLContext is not a QObject child here; releasing the local
        # wrapper destroys the probe context without scheduling an event.
        context = None


class Canvas(QOpenGLWidget):
    """OpenGL child that delegates its whole frame to the owning widget."""

    def __init__(self, owner: "AcceleratedWidget") -> None:
        super().__init__(owner)
        self._owner_ref = weakref.ref(owner)
        self._cleanup_context = None
        self.setFormat(_surface_format())
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    @property
    def owner(self) -> "AcceleratedWidget | None":
        return self._owner_ref()

    def initializeGL(self) -> None:  # noqa: N802 - Qt override
        context = self.context()
        if context is not None and context is not self._cleanup_context:
            self._cleanup_context = context
            context.aboutToBeDestroyed.connect(self._cleanup_owner_gpu)

    def paintGL(self) -> None:  # noqa: N802 - Qt override
        owner = self.owner
        if owner is None:
            return
        painter = QPainter(self)
        if not painter.isActive():
            return
        try:
            owner._paint_viewport(painter)
        finally:
            painter.end()

    def _cleanup_owner_gpu(self) -> None:
        owner = self.owner
        if owner is None:
            return
        cleanup = getattr(owner, "_cleanup_gpu", None)
        if not callable(cleanup):
            return
        self.makeCurrent()
        try:
            cleanup()
        finally:
            self.doneCurrent()


class AcceleratedWidget(QWidget):
    """Route widget painting through Qt's OpenGL paint engine on desktop."""

    def __init__(self, parent=None) -> None:
        self._gpu_canvas = None
        super().__init__(parent)
        if _use_opengl_canvas():
            self._gpu_canvas = Canvas(self)
            self._gpu_canvas.setGeometry(self.rect())
            self._gpu_canvas.show()

    def update(self, *args) -> None:  # noqa: N802 - preserve QWidget overloads
        canvas = getattr(self, "_gpu_canvas", None)
        if canvas is None:
            super().update(*args)
        else:
            canvas.update(*args)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        if self._gpu_canvas is not None:
            self._gpu_canvas.setGeometry(self.rect())

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._gpu_canvas is not None:
            return
        painter = QPainter(self)
        if not painter.isActive():
            return
        try:
            self._paint_viewport(painter)
        finally:
            painter.end()

    def _paint_viewport(self, painter: QPainter) -> None:
        """Draw the widget contents using the painter supplied by the surface."""

