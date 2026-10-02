"""One backend indicator for all Studio editor windows."""
from PySide6.QtWidgets import QLabel


class RendererBadge(QLabel):
    def __init__(self, viewport, parent=None):
        super().__init__(parent)
        self.setObjectName('rendererBadge')
        viewport.backendChanged.connect(self.set_backend)
        self.set_backend(viewport.renderer_name)

    def set_backend(self, name):
        self.setText('GPU' if name.startswith('OpenGL') else 'Software')
        self.setToolTip(name)


def add_renderer_badge(window, viewport):
    badge = RendererBadge(viewport, window)
    window.statusBar().addPermanentWidget(badge)
    return badge
