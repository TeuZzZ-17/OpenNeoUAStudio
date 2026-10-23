"""Original UA font glyphs, decoded once per selected installation."""
import math
from functools import lru_cache
from pathlib import Path

import numpy as np
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QColor, QPainter, QPen

from .. import bootstrap
from ..core.resource_catalog import preview_image


def game_icon(kind, owner=1, tight=True, grey=False, outlined=False):
    if kind in ('squad', 'player'):
        return _badge_symbol(kind).copy()
    if kind == 'host':
        root = bootstrap.game_data_dir()
        # The strategic map uses MAPROBO's largest owner group (0x18 + owner).
        return _glyph(str(root), 'MAPROBO', 24 + max(0,min(7,int(owner))), tight, grey, outlined)
    if kind in ('power', 'gate', 'gem', 'item', 'gate_key', 'item_key', 'wave', 'wave_key'):
        font, code = 'MAPCUR64', dict(power=137, gate=147, gem=136, item=139,
                                     gate_key=138, item_key=139, wave=143, wave_key=143)[kind]
    else:
        font, code = 'TYPE_NS', {'radar': ord('U'), 'flak': ord('V')}[kind]
    root = bootstrap.game_data_dir()
    return _glyph(str(root), font, code, tight, grey, outlined)


@lru_cache(maxsize=2)
def _badge_symbol(kind):
    image = QImage(32,32,QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(Qt.GlobalColor.white)
    if kind == 'player':
        painter.drawEllipse(QRectF(11,3,10,10))
        painter.drawRoundedRect(QRectF(6,16,20,12),5,5)
    else:
        for x,y in ((5,18),(20,18),(12,4)):
            painter.drawRoundedRect(QRectF(x,y,8,9),2,2)
            painter.drawRect(QRectF(x+3,y-4,2,8))
    painter.end()
    return image


@lru_cache(maxsize=48)
def _glyph(root, font, code, tight, grey, outlined):
    data = Path(root)
    directory = bootstrap.find_ci(data, 'Fonts')
    if directory is None and data.name.casefold() == 'data':
        # Like the engine, fall back to the game root used by the vanilla layout.
        directory = bootstrap.find_ci(data.parent, 'Fonts')
    definitions = bootstrap.find_ci(directory, 'HFonts') if directory else None
    path = bootstrap.find_ci(definitions, font + '.FON', font + '.font') if definitions else None
    if path is None:
        return QImage()
    lines = path.read_text(encoding='cp1252').splitlines()
    page, height = lines[0].split(';')[0].split()[:2]
    stem = Path(page).stem
    image_path = bootstrap.find_ci(directory, stem+'.PNG', page, stem+'.ILB', stem+'.ILBM')
    if image_path is None:
        return QImage()
    image = preview_image(image_path).convertToFormat(QImage.Format.Format_RGBA8888)
    for line in lines[1:]:
        parts = line.split(';')[0].split()
        if len(parts) < 4:
            continue
        token = parts[0]
        index = int(token[1:], 0) if token.startswith('#') else ord(token[0])
        if index != code:
            continue
        x, y, width = map(int, parts[1:4])
        glyph = image.copy(x, y, width, int(height))
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
    return QImage()


def _unit_color(color):
    """Keep dark faction colours readable on the dark backing disc."""
    value = QColor(color)
    if value.lightness() < 125:
        mix = .32
        value = QColor(round(value.red()*(1-mix) + 255*mix),
                       round(value.green()*(1-mix) + 255*mix),
                       round(value.blue()*(1-mix) + 255*mix))
    return value


def _draw_unit_badge(painter, center, size, image, color, moment):
    radius = max(8.0, size*.63)
    ring = _unit_color(color)
    shadow = QRectF(center.x()-radius-1, center.y()-radius+1.5,
                    (radius+1)*2, (radius+1)*2)
    disc = QRectF(center.x()-radius, center.y()-radius, radius*2, radius*2)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(0, 0, 0, 115))
    painter.drawEllipse(shadow)
    painter.setBrush(QColor(9, 15, 21, 225))
    painter.setPen(QPen(QColor(0, 0, 0, 220), 4))
    painter.drawEllipse(disc)
    pulse = .85 + .15*math.sin(moment*3.2)
    ring_color = QColor(ring)
    ring_color.setAlpha(int(225 + 30*pulse))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(ring_color, 2))
    painter.drawEllipse(disc.adjusted(.6, .6, -.6, -.6))
    painter.setPen(QPen(QColor(255, 255, 255, 85), 1))
    painter.drawArc(disc.adjusted(3.2, 3.2, -3.2, -3.2), 45*16, 100*16)
    factor = radius*1.30/max(1, image.width(), image.height())
    w, h = image.width()*factor, image.height()*factor
    painter.drawImage(QRectF(center.x()-w/2, center.y()-h/2, w, h), image)
    painter.restore()


def draw_game_badge(painter, center, size, kind, color, moment, owner=1):
    image = game_icon(kind, owner, grey=color.red() == color.green() == color.blue())
    if image.isNull():
        return False
    if kind in ('squad', 'host', 'player'):
        tint = QPainter(image)
        tint.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        tint.fillRect(image.rect(), _unit_color(color))
        tint.end()
    if kind in ('squad', 'host', 'player'):
        _draw_unit_badge(painter, center, size, image, color, moment)
        return True
    size *= .75
    painter.save()
    pulse = .85 + .15*math.sin(moment*3.2)
    glow = QColor(color); glow.setAlpha(int(65 + 35*pulse))
    painter.setPen(QColor(color)); painter.setBrush(glow)
    painter.drawEllipse(center, size*.75, size*.75)
    painter.setOpacity(pulse)
    factor = size/max(image.width(), image.height())
    w, h = image.width()*factor, image.height()*factor
    painter.drawImage(QRectF(center.x()-w/2, center.y()-h/2, w, h), image)
    painter.restore()
    return True
