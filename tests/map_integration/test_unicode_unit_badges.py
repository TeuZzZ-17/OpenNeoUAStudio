import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import numpy as np
import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from map_editor.core.vehicle_defs import parse_vehicle_text
from map_editor.ui import game_icons
from entity_designer.script_model import write_script_file


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


def test_vehicle_icon_follows_new_and_modify_blocks():
    values = parse_vehicle_text('new_vehicle 56\nmodel = robo\ntype_icon = ∆\nend\n'
                                'modify_vehicle 56\nname = Host\nend')
    assert values[56].type_icon == '∆'
    parse_vehicle_text('modify_vehicle 56\ntype_icon = U+1F5F2\nend', values)
    assert values[56].type_icon == 'U+1F5F2'


def test_legacy_script_keeps_bytes_when_writing_new_icon(tmp_path):
    path = tmp_path / 'vehicles.cfg'
    text = '; legacy byte \xa3\nnew_vehicle 1\ntype_icon = ∆\nend\n'
    write_script_file(path, text, 'latin-1', False)
    assert path.read_bytes() == b'; legacy byte \xa3\nnew_vehicle 1\ntype_icon = U+2206\nend\n'
    write_script_file(path, 'type_icon = ★\n', 'utf-8', False)
    assert path.read_text(encoding='utf-8') == 'type_icon = ★\n'


def test_font_accepts_legacy_utf8_and_wide_explicit_keys(tmp_path, monkeypatch, app):
    directory = tmp_path / 'Fonts' / 'HFonts'
    directory.mkdir(parents=True)
    (tmp_path / 'Fonts' / 'page.PNG').touch()
    (directory / 'TEST.FON').write_bytes(
        b'page.PNG 16\n\xa3 1 1 20\n' + '∆ 1 1 20\n'.encode() + b'U+1F5F2 1 1 20\n')
    page = QImage(24, 20, QImage.Format.Format_RGBA8888)
    page.fill(QColor(255, 255, 0))
    painter = QPainter(page)
    painter.fillRect(4, 4, 8, 8, Qt.GlobalColor.white)
    painter.end()
    monkeypatch.setattr(game_icons, 'preview_image', lambda path: page)
    for key in (0xa3, 0x2206, 0x1f5f2):
        image = game_icons._glyph(str(tmp_path), 'TEST', key, True, False, False)
        assert image.size().width() == image.size().height() == 8
        assert image.pixelColor(0, 0).alpha() == 255
    assert game_icons._glyph_key('🗲'.encode()) == 0x1f5f2
    for bad in (b'U+D800', b'U+110000', '�'.encode()):
        with pytest.raises(ValueError):
            game_icons._glyph_key(bad)


def test_host_uses_authored_type_icon_and_shared_badge_size(app, monkeypatch):
    root = Path(__file__).resolve().parents[4] / 'UA Rising' / 'Data'
    monkeypatch.setattr(game_icons.bootstrap, 'game_data_dir', lambda: root)
    host = game_icons.game_icon('host', 1, glyph_key='∆')
    assert not host.isNull()
    assert host == game_icons._glyph(str(root), 'TYPE_NS', 0x2206, True, False, False)
    bounds = []
    for kind in ('host', 'squad', 'radar', 'flak'):
        image = QImage(64, 64, QImage.Format.Format_RGBA8888)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        assert game_icons.draw_game_badge(painter, QPointF(32, 32),
            game_icons.MAP_BADGE_SIZE, kind, QColor(255, 160, 0), 0,
            glyph_key='∆' if kind == 'host' else None)
        painter.end()
        pixels = np.frombuffer(image.bits(), np.uint8).reshape(64, 64, 4)
        yy, xx = np.where(pixels[:, :, 3] > 0)
        bounds.append((xx.min(), yy.min(), xx.max(), yy.max()))
        assert not ((pixels[:, :, 0] == 255) & (pixels[:, :, 1] == 255)
                    & (pixels[:, :, 2] == 0) & (pixels[:, :, 3] > 0)).any()
    assert len(set(bounds)) == 1


def test_squad_symbol_uses_original_pixels(app):
    root = game_icons.bootstrap.game_data_dir()
    assert game_icons.game_icon('squad') == game_icons._glyph(str(root), 'TYPE_NS', 33, True, False, False)


def test_faction_colour_does_not_repaint_native_host_pixels(app):
    original = game_icons.game_icon('host', 1, glyph_key='Q').convertToFormat(QImage.Format.Format_RGBA8888)
    outputs = []
    for color in (QColor('red'), QColor('blue'), QColor(160, 160, 160)):
        image = QImage(80, 80, QImage.Format.Format_RGBA8888)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        game_icons.draw_game_badge(painter, QPointF(20+original.width()/2, 20+original.height()/2), max(original.width(), original.height())/.75,
                                  'host', color, 0, glyph_key='Q')
        painter.end()
        crop = image.copy(20, 20,
                          original.width(), original.height())
        pixels = np.frombuffer(crop.bits(), np.uint8).reshape(crop.height(), crop.width(), 4).copy()
        outputs.append(pixels)
    source = np.frombuffer(original.bits(), np.uint8).reshape(original.height(), original.width(), 4)
    mask = source[:, :, 3] == 255
    assert mask.any()
    assert np.array_equal(outputs[0][mask], source[mask])
    assert np.array_equal(outputs[1][mask], source[mask])
    assert np.array_equal(outputs[2][mask], source[mask])
