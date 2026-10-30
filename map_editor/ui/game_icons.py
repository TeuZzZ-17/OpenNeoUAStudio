"""Original UA font glyphs, decoded once per selected installation."""
import math
from functools import lru_cache

import numpy as np
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QColor, QPainter

from .. import bootstrap
from ..core.resource_catalog import preview_image
from studio.formats.font_def import glyph_key as _glyph_key, locate_font, read_font


MAP_BADGE_SIZE = 17.0 * 1.35


def game_icon(kind, owner=1, tight=True, grey=False, outlined=False, glyph_key=None):
    if kind == 'player':
        return _player_symbol().copy()
    if kind == 'squad':
        root = bootstrap.game_data_dir()
        return _glyph(str(root), 'TYPE_NS', ord('!'), tight, grey, outlined)
    if kind == 'host':
        root = bootstrap.game_data_dir()
        if glyph_key:
            try:
                code = _glyph_key(glyph_key.encode('utf-8'))
                image = _glyph(str(root), 'TYPE_NS', code, tight, grey, outlined)
                if image.isNull() and code > 255:
                    legacy = chr(code).encode('cp1252')
                    image = _glyph(str(root), 'TYPE_NS', legacy[0], tight, grey, outlined)
                if not image.isNull():
                    return image
            except (ValueError, UnicodeError):
                pass
        # Vanilla fallback when the placed vehicle has no authored type icon.
        return _glyph(str(root), 'MAPROBO', 24 + max(0,min(7,int(owner))), tight, grey, outlined)
    if kind in ('power', 'gate', 'gem', 'item', 'gate_key', 'item_key', 'wave', 'wave_key'):
        font, code = 'MAPCUR64', dict(power=137, gate=147, gem=136, item=139,
                                     gate_key=138, item_key=139, wave=143, wave_key=143)[kind]
    else:
        font, code = 'TYPE_NS', {'radar': ord('U'), 'flak': ord('V')}[kind]
    root = bootstrap.game_data_dir()
    return _glyph(str(root), font, code, tight, grey, outlined)


@lru_cache(maxsize=1)
def _player_symbol():
    image = QImage(32,32,QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(Qt.GlobalColor.white)
    painter.drawEllipse(QRectF(11,3,10,10))
    painter.drawRoundedRect(QRectF(6,16,20,12),5,5)
    painter.end()
    return image


@lru_cache(maxsize=48)
def _glyph(root, font, code, tight, grey, outlined):
    path, image_path = locate_font(root, font)
    if path is None or image_path is None:
        return QImage()
    definition = read_font(path)
    entry = definition.glyph(code)
    if entry is None:
        return QImage()
    image = preview_image(image_path).convertToFormat(QImage.Format.Format_RGBA8888)
    glyph = image.copy(entry.x, entry.y, entry.width, definition.height)
    rgba = np.frombuffer(glyph.bits(), np.uint8).reshape(glyph.height(), glyph.bytesPerLine())[:, :glyph.width()*4].reshape(glyph.height(), glyph.width(), 4)
    rgba[(rgba[:, :, :3] == (255,255,0)).all(axis=2), 3] = 0
    if font == 'MAPCUR64':
        # Every MAPCUR64 tile carries the atlas's blue separator on its right edge. Drawn on
        # the map it showed up as a stray blue line beside key sectors, so exclude it
        # before finding the visible bounds of the real symbol.
        separator = (rgba[:, -1, :3] == (0, 52, 255)).all(axis=1)
        rgba[separator, -1, 3] = 0
    if grey:
        luma = np.rint(rgba[:, :, :3] @ np.array((.299, .587, .114))).astype(np.uint8)
        rgba[:, :, :3] = luma[:, :, None]
    if outlined:
        # Outline only visible glyph pixels, leaving the icon and its transparent gaps intact.
        mask = rgba[:, :, 3] > 0
        padded = np.pad(mask, 1)
        expanded = np.zeros_like(mask)
        for dy in range(3):
            for dx in range(3):
                expanded |= padded[dy:dy + mask.shape[0], dx:dx + mask.shape[1]]
        rgba[expanded & ~mask] = (255, 255, 255, 255)
    if tight:
        yy, xx = np.where(rgba[:, :, 3] > 0)
        if len(xx):
            glyph = glyph.copy(int(xx.min()), int(yy.min()), int(xx.max()-xx.min()+1), int(yy.max()-yy.min()+1))
    return glyph


def draw_game_badge(painter, center, size, kind, color, moment, owner=1, glyph_key=None):
    grey = kind not in ('host', 'squad') and color.red() == color.green() == color.blue()
    image = game_icon(kind, owner, grey=grey,
                      glyph_key=glyph_key)
    if image.isNull():
        return False
    size *= .75
    painter.save()
    pulse = .85 + .15*math.sin(moment*3.2)
    glow = QColor(color); glow.setAlpha(int(65 + 35*pulse))
    painter.setPen(QColor(color)); painter.setBrush(glow)
    painter.drawEllipse(center, size*.75, size*.75)
    # Faction colour belongs to the circle; original icon pixels stay opaque.
    painter.setOpacity(1.0)
    factor = size/max(image.width(), image.height())
    w, h = image.width()*factor, image.height()*factor
    painter.drawImage(QRectF(center.x()-w/2, center.y()-h/2, w, h), image)
    painter.restore()
    return True
