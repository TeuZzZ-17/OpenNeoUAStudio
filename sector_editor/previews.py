"""Visible resource cards rendered by the shared map preview pipeline."""

from collections import OrderedDict

from PySide6.QtCore import QObject, QPoint, QRunnable, QThreadPool, QTimer, Signal
from PySide6.QtGui import QIcon, QPixmap

from map_editor.render.sector_sprite import rasterize_sprite, sprite_input
from .viewport import PreviewLibrary


class _Result(QObject):
    ready = Signal(object, object)


class _RasterTask(QRunnable):
    def __init__(self, key, data, tables, scale, result):
        super().__init__()
        self.key, self.data, self.tables = key, data, tables
        self.scale, self.result = scale, result

    def run(self):
        sprite = rasterize_sprite(self.data, self.tables, self.scale)
        self.result.ready.emit(self.key, sprite.image if sprite else None)


class CardPreviews(QObject):
    """Bound work to visible cards and retain a small image cache while browsing."""
    ready = Signal(object, object)

    def __init__(self, viewport, parent=None):
        super().__init__(parent)
        self.viewport = viewport
        self.library = None
        self.cache = OrderedDict()
        self.pending = OrderedDict()
        self.generation = 0
        self.busy = False
        self.active = None
        self.rendered = 0
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.result = _Result(self)
        self.result.ready.connect(self._complete)
        self.timer = QTimer(self)
        self.timer.setInterval(20)
        self.timer.timeout.connect(self._next)

    def reset(self, library):
        self.library = library
        self.generation += 1
        self.pending.clear()
        self.cache.clear()

    def request(self, requests):
        # Scrolling replaces queued work; it never renders the whole model table.
        self.pending.clear()
        for key, kind, number, state in requests:
            if key in self.cache:
                icon = self.cache.pop(key)
                self.cache[key] = icon
                self.ready.emit(key, icon)
            elif key != self.active:
                self.pending[key] = (kind, number, state)
        if self.pending:
            self.timer.start()

    def _next(self):
        if self.busy:
            return
        if not self.pending or self.library is None:
            self.timer.stop()
            return
        key, (kind, number, state) = self.pending.popitem(last=False)
        library = PreviewLibrary(self.library, state, number if kind == "model" else None)
        self.busy = True
        self.active = key
        self.rendered += 1
        try:
            sprite = self.viewport.render_icon(library, 0 if kind == "model" else number,
                                               1, max_dimension=112)
            if sprite is not None:
                self._complete(key, sprite.image)
                return
            mesh = library.mesh(0 if kind == "model" else number)
            if not mesh.faces:
                self._complete(key, None)
                return
            extent = max(1, *(mesh.bounds[i + 3] - mesh.bounds[i] for i in range(3)))
            scale = min(.13, 90 / extent)
            data = sprite_input(library, mesh, -45, 35.264, scale, fit=True)
            self.pool.start(_RasterTask(key, data, library.tables, scale, self.result))
        except (OSError, ValueError, IndexError, RuntimeError):
            self._complete(key, None)

    def _complete(self, key, image):
        self.busy = False
        self.active = None
        if key[0] != self.generation:
            return
        icon = QIcon(QPixmap.fromImage(image)) if image is not None else QIcon()
        self.cache[key] = icon
        while len(self.cache) > 256:
            self.cache.popitem(last=False)
        self.ready.emit(key, icon)

    def stop(self):
        self.timer.stop()
        self.pending.clear()
        self.pool.waitForDone()


def visible_items(view):
    """Find only rows intersecting the visible list viewport."""
    if not view.isVisible():
        return []
    rect = view.viewport().rect()
    first = view.indexAt(QPoint(rect.center().x(), 6)).row()
    if first < 0:
        first = view.indexAt(QPoint(rect.center().x(), rect.center().y())).row()
    if first < 0:
        return []
    items = []
    for row in range(first, view.count()):
        item = view.item(row)
        if item.isHidden():
            continue
        item_rect = view.visualItemRect(item)
        if item_rect.top() > rect.bottom():
            break
        if item_rect.intersects(rect):
            items.append(item)
    return items
