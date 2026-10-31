"""QWidget surface backed by a native OpenGL canvas, and the backend indicator.
"""

from __future__ import annotations

import os
import weakref

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import (
    QGuiApplication,
    QOffscreenSurface,
    QOpenGLContext,
    QPainter,
    QSurfaceFormat,
)
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QApplication, QPushButton, QWidget
from studio.ui.cursor_themes import studio_cursor


def request_desktop_opengl() -> None:
    """Keep Qt's default surface settings and request desktop OpenGL."""

    surface = QSurfaceFormat.defaultFormat()
    surface.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
    QSurfaceFormat.setDefaultFormat(surface)


# ----------------------------------------------------------------------------
# QWidget paint surface backed by a native Qt OpenGL canvas when available.
# (formerly gpu_widget.py)
# ----------------------------------------------------------------------------

def _use_opengl_canvas(*, explicit=False) -> bool:
    if not explicit and os.environ.get("NME_RENDERER", "").strip().casefold() == "software":
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
        owner._report_backend()

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

    def release_resources(self):
        self._cleanup_owner_gpu()
        if self._cleanup_context is not None:
            self._cleanup_context.aboutToBeDestroyed.disconnect(self._cleanup_owner_gpu)
            self._cleanup_context = None


class AcceleratedWidget(QWidget):
    """Route widget painting through Qt's OpenGL paint engine on desktop."""

    backendChanged = Signal(str)

    def __init__(self, parent=None) -> None:
        self._gpu_canvas = None
        self._reported_backend = None
        self.renderer_error = ""
        super().__init__(parent)
        # Every 3D/2D view starts with the shared pointing hand, never the system arrow.
        self.setCursor(studio_cursor("point"))
        if _use_opengl_canvas():
            self._gpu_canvas = Canvas(self)
            self._gpu_canvas.setGeometry(self.rect())
            self._gpu_canvas.show()

    @property
    def renderer_mode(self):
        return "gpu" if self._gpu_canvas is not None else "software"

    def set_renderer_mode(self, mode):
        """Replace only the drawing surface, preserving the editing session."""
        if mode not in {"gpu", "software"}:
            raise ValueError(mode)
        if mode == self.renderer_mode:
            return True
        self.renderer_error = ""
        if mode == "gpu":
            if not _use_opengl_canvas(explicit=True):
                self.renderer_error = "OpenGL 3.3 is unavailable. Software rendering remains active."
                self._report_backend()
                return False
            self._gpu_canvas = Canvas(self)
            self._gpu_canvas.setGeometry(self.rect())
            self._gpu_canvas.show()
        else:
            canvas = self._gpu_canvas
            canvas.release_resources()
            self._gpu_canvas = None
            canvas.hide()
            canvas.deleteLater()
        self._renderer_mode_changed()
        self._report_backend()
        self.update()
        return True

    def _renderer_mode_changed(self):
        pass

    @property
    def renderer_name(self):
        canvas = self._gpu_canvas
        return 'OpenGL painter' if canvas is not None and canvas.isValid() else 'Software painter'

    def _report_backend(self):
        name = self.renderer_name
        if name != self._reported_backend:
            self._reported_backend = name
            self.backendChanged.emit(name)

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
        self._report_backend()

    def _paint_viewport(self, painter: QPainter) -> None:
        """Draw the widget contents using the painter supplied by the surface."""


# ----------------------------------------------------------------------------
# One backend indicator for all Studio editor windows.
# (formerly render_status.py)
# ----------------------------------------------------------------------------

class RendererBadge(QPushButton):
    def __init__(self, viewport, parent=None):
        super().__init__(parent)
        self.setObjectName('rendererBadge')
        self.setFlat(True)
        self.viewport = viewport
        self._viewports = [viewport]
        self.clicked.connect(self.toggle_backend)
        viewport.backendChanged.connect(self.set_backend)
        self.set_backend(viewport.renderer_name)

    def add_viewport(self, viewport):
        self._viewports.append(viewport)
        viewport.set_renderer_mode(self.viewport.renderer_mode)

    def set_viewport(self, viewport):
        if viewport is self.viewport:
            return
        self.viewport.backendChanged.disconnect(self.set_backend)
        self.viewport = viewport
        viewport.backendChanged.connect(self.set_backend)
        self.set_backend(viewport.renderer_name)

    def set_backend(self, name):
        self.setText('GPU' if name.startswith('OpenGL') else 'Software')
        target = 'Software' if self.viewport.renderer_mode == 'gpu' else 'GPU'
        self.setToolTip(f'{name}\nClick to switch to {target}.')
        self.setAccessibleName(f'Renderer: {self.text()}. Switch to {target}')

    def toggle_backend(self):
        target = 'software' if self.viewport.renderer_mode == 'gpu' else 'gpu'
        success = True
        for viewport in self._viewports:
            success = viewport.set_renderer_mode(target) and success
        self.set_backend(self.viewport.renderer_name)
        window = self.window()
        if hasattr(window, 'statusBar'):
            message = (next((view.renderer_error for view in self._viewports if view.renderer_error), '') if not success else
                       f'Renderer switched to {target.upper() if target == "gpu" else "Software"}.')
            window.statusBar().showMessage(message, 10000)


def add_renderer_badge(window, viewport):
    badge = RendererBadge(viewport, window)
    window.statusBar().addPermanentWidget(badge)
    return badge
