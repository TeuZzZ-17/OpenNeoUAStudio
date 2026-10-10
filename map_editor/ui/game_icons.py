"""Original UA font glyphs, decoded once per selected installation."""
from functools import lru_cache
from pathlib import Path

import numpy as np
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QImage, QColor

from .. import bootstrap
from ..core.resource_catalog import preview_image


def game_icon(kind, owner=1, tight=True, grey=False):
    if kind in ('power', 'gate', 'gem', 'item', 'gate_key', 'item_key', 'wave', 'wave_key'):
        font, code = 'MAPCUR64', dict(power=137, gate=147, gem=136, item=139,
                                     gate_key=138, item_key=139, wave=143, wave_key=143)[kind]
    else:
        font, code = 'TYPE_NS', {'radar': ord('U'), 'flak': ord('V')}[kind]
    root = bootstrap.game_data_dir()
    return _glyph(str(root), font, code, tight, grey)


@lru_cache(maxsize=48)
def _glyph(root, font, code, tight, grey):
    directory = bootstrap.find_ci(Path(root), 'Fonts')
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
        if font == 'MAPCUR64' and code in (147, 148):
            # The gate tile includes the atlas's blue separator on its right.
            # Exclude it before finding the visible bounds of the real symbol.
            separator = (rgba[:, -1, :3] == (0, 52, 255)).all(axis=1)
            rgba[separator, -1, 3] = 0
        if grey:
            luma = np.rint(rgba[:, :, :3] @ np.array((.299, .587, .114))).astype(np.uint8)
            rgba[:, :, :3] = luma[:, :, None]
        if tight:
            yy, xx = np.where(rgba[:, :, 3] > 0)
            if len(xx):
                glyph = glyph.copy(int(xx.min()), int(yy.min()), int(xx.max()-xx.min()+1), int(yy.max()-yy.min()+1))
        return glyph
    return QImage()


def draw_game_badge(painter, center, size, kind, color, moment, owner=1):
    import math
    image = game_icon(kind, owner, grey=color.red() == color.green() == color.blue())
    if image.isNull():
        return False
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
