"""One backend indicator for all Studio editor windows."""
from PySide6.QtWidgets import QPushButton


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
