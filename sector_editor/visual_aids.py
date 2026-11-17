"""Compact, read-only previews for native ground fillers and map symbols."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QImage, QPainter, QPixmap
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from map_editor import bootstrap
from map_editor.core.resource_catalog import preview_image
from map_editor.render.sector_sprite import rasterize_sprite, sprite_input


def _image_label(size: QSize, parent: QWidget) -> QLabel:
    label = QLabel("Preview unavailable", parent)
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    label.setFixedSize(size)
    label.setFrameShape(QFrame.Shape.StyledPanel)
    label.setFrameShadow(QFrame.Shadow.Plain)
    label.setWordWrap(True)
    return label


def _show_preview(label: QLabel, image: QImage | None, *, nearest=False) -> None:
    if image is None or image.isNull():
        label.setPixmap(QPixmap())
        label.setText("Preview unavailable")
        return
    mode = (Qt.TransformationMode.FastTransformation if nearest
            else Qt.TransformationMode.SmoothTransformation)
    pixmap = QPixmap.fromImage(image).scaled(
        label.size(), Qt.AspectRatioMode.KeepAspectRatio, mode)
    label.setText("")
    label.setPixmap(pixmap)


def _render_ground(library, ground: int) -> QImage | None:
    heights = (0.0,) * 4 + (-300.0,) * 4 + (-150.0, -150.0)
    mesh = library.filler_mesh(ground, 0, False, heights)
    scale = 0.065
    data = sprite_input(library, mesh, -45, 35.264, scale, fit=True)
    sprite = rasterize_sprite(data, library.tables, scale)
    return sprite.image if sprite is not None else None


class GroundPreview(QWidget):
    """Show the native filler joining one family to family 0."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.preview = _image_label(QSize(80, 44), self)
        self.label = QLabel("Connection to family 0", self)
        self.detail = QLabel("BASE surface unchanged", self)
        self.label.setToolTip(
            "This is only the native transition filler. Each cell's BASE "
            "continues to provide its ground surface.")
        caption = QWidget(self)
        caption_layout = QVBoxLayout(caption)
        caption_layout.setContentsMargins(0, 0, 0, 0)
        caption_layout.setSpacing(2)
        caption_layout.addWidget(self.label)
        caption_layout.addWidget(self.detail)
        caption_layout.addStretch(1)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.preview)
        layout.addWidget(caption, 1)
        self._library = None
        self._images: dict[int, QImage | None] = {}

    def set_ground(self, library, ground: int) -> None:
        if type(ground) is not int or not 0 <= ground <= 5 or library is None:
            _show_preview(self.preview, None)
            return
        if library is not self._library:
            self._library = library
            self._images.clear()
        if ground not in self._images:
            try:
                self._images[ground] = _render_ground(library, ground)
            except (AttributeError, IndexError, KeyError, OSError, RuntimeError,
                    TypeError, ValueError):
                self._images[ground] = None
        _show_preview(self.preview, self._images[ground])


def _font_directory() -> Path | None:
    try:
        data = bootstrap.game_data_dir()
    except RuntimeError:
        return None
    directory = bootstrap.find_ci(data, "Fonts")
    if directory is None and data.name.casefold() in ("data", "dataxp"):
        directory = bootstrap.find_ci(data.parent, "Fonts")
    return directory


@lru_cache(maxsize=4)
def _atlas(path: str, modified: int, size: int) -> QImage:
    del modified, size
    image = preview_image(Path(path)).convertToFormat(
        QImage.Format.Format_RGBA8888).copy()
    if image.isNull():
        return QImage()
    rows = np.frombuffer(image.bits(), dtype=np.uint8).reshape(
        image.height(), image.bytesPerLine())[:, :image.width() * 4]
    rgba = rows.reshape(image.height(), image.width(), 4)
    # The engine treats pure yellow as the tileset's transparent color key.
    color_key = ((rgba[:, :, 0] == 255) & (rgba[:, :, 1] == 255)
                 & (rgba[:, :, 2] == 0))
    rgba[color_key, 3] = 0
    return image


@lru_cache(maxsize=512)
def _symbol_tile(path: str, modified: int, size: int, symbol: int,
                 tile_size: int) -> QImage:
    image = _atlas(path, modified, size)
    x = (symbol % 16) * tile_size
    y = (symbol // 16) * tile_size
    if image.isNull() or x + tile_size > image.width() or y + tile_size > image.height():
        return QImage()
    return image.copy(x, y, tile_size, tile_size)


def _map_symbol_image(symbol: int) -> QImage | None:
    directory = _font_directory()
    if directory is None:
        return None
    tiles = []
    for tile_size in (4, 8):
        stem = f"sec{tile_size}"
        path = bootstrap.find_ci(directory, stem + ".PNG", stem + ".ILB",
                                 stem + ".ILBM")
        if path is None or not path.is_file():
            return None
        stat = path.stat()
        tile = _symbol_tile(str(path), stat.st_mtime_ns, stat.st_size,
                            symbol, tile_size)
        if tile.isNull():
            return None
        tiles.append(tile.scaled(
            QSize(32, 32), Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.FastTransformation))

    image = QImage(68, 32, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
    painter.drawImage(0, 0, tiles[0])
    painter.drawImage(36, 0, tiles[1])
    painter.end()
    return image


class SymbolPreview(QWidget):
    """Show the real strategic-map cell glyph at both native tile sizes."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.preview = _image_label(QSize(68, 32), self)
        self.label = QLabel("Map symbol · 4px / 8px", self)
        self.label.setToolTip(
            "Native map glyph, enlarged from the game's 4 px and 8 px tiles.")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.preview)
        layout.addWidget(self.label, 1)

    def set_symbol(self, library, symbol: int) -> None:
        if type(symbol) is not int or not 0 <= symbol <= 255:
            _show_preview(self.preview, None)
            return
        try:
            image = _map_symbol_image(symbol)
        except (OSError, RuntimeError, TypeError, ValueError):
            image = None
        _show_preview(self.preview, image, nearest=True)


# --- strategic-map icons of a sector at the five map zoom levels ----------------------------

MAP_ZOOM_TITLES = ("Zoom 1", "Zoom 2", "Zoom 3", "Zoom 4", "Zoom 5")


def _font_tile(stem: str, tile_size: int, symbol: int) -> QImage:
    directory = _font_directory()
    path = bootstrap.find_ci(directory, stem + ".PNG", stem + ".ILB", stem + ".ILBM") if directory else None
    if path is None or not path.is_file():
        return QImage()
    stat = path.stat()
    return _symbol_tile(str(path), stat.st_mtime_ns, stat.st_size, symbol & 0xFF, tile_size)


def _icon_image(files, zoom_index: int, size: int) -> QImage:
    """One of the authored/generated icon files (1 for every zoom, or one per zoom)."""
    if not files:
        return QImage()
    path = files[0 if len(files) == 1 else min(zoom_index, len(files) - 1)]
    image = QImage(str(path))
    if image.isNull():
        return image
    return image.scaled(QSize(size, size), Qt.AspectRatioMode.IgnoreAspectRatio,
                        Qt.TransformationMode.FastTransformation)


def map_zoom_image(document, number: int, state: int, zoom: int) -> QImage | None:
    """The sector as the game map draws it: own icons where set, vanilla glyphs elsewhere."""
    sector = document.sdf.sectors.get(number) if document else None
    if sector is None:
        return None
    if zoom < 2:
        size = 4 << zoom
        image = _icon_image(document.map_icons.get(("sector", number)), zoom, size)
        return image if not image.isNull() else _font_tile(f"sec{size}", size, sector.symbol)
    cell = 4 << (zoom - 2)
    stem = f"lego{cell}"
    canvas = QImage(cell * 3, cell * 3, QImage.Format.Format_ARGB32_Premultiplied)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    models = document.sdf.models
    for index, bid in enumerate(sector.subsects):
        model = models[document.sdf.buildings[bid].state(state)]
        icons = document.map_icons.get(("model", model.id))
        if sector.single:
            image = _icon_image(icons, zoom - 2, cell * 3)
            if not image.isNull():
                painter.drawImage(0, 0, image)
            else:
                for row in range(3):
                    for col in range(3):
                        painter.drawImage(col * cell, row * cell,
                                          _font_tile(stem, cell, model.numbers[1] + 16 * row + col))
            break
        image = _icon_image(icons, zoom - 2, cell)
        if image.isNull():
            image = _font_tile(stem, cell, model.numbers[1])
        painter.drawImage((index % 3) * cell, (index // 3) * cell, image)
    painter.end()
    return canvas


class MapIconPreview(QWidget):
    """The current sector at every strategic-map zoom, enlarged without smoothing."""

    def __init__(self, parent=None):
        super().__init__(parent)
        from PySide6.QtWidgets import QGridLayout
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.previews = []
        for zoom, title in enumerate(MAP_ZOOM_TITLES):
            label = _image_label(QSize(96, 96), self)
            label.setToolTip(f"{title}: " + ("one symbol per sector" if zoom < 2 else "one symbol per cell"))
            caption = QLabel(title, self)
            caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(label, (zoom // 3) * 2, zoom % 3)
            layout.addWidget(caption, (zoom // 3) * 2 + 1, zoom % 3)
            self.previews.append(label)

    def show_sector(self, document, number: int, state: int) -> None:
        for zoom, label in enumerate(self.previews):
            try:
                image = map_zoom_image(document, number, state, zoom)
            except (IndexError, OSError, RuntimeError, ValueError):
                image = None
            _show_preview(label, image, nearest=True)
