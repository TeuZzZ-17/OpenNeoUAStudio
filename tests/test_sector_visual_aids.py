"""Real-asset coverage for the compact native visual aids."""

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

pytest.importorskip("PySide6")
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from map_editor import bootstrap
from map_editor.core.asset_bridge import SetAssets
from map_editor.core.game_installation import GameInstallation
from map_editor.render.sector_mesh import SectorMeshLibrary
from sector_editor.visual_aids import GroundPreview, SymbolPreview


def _visible_pixels(pixmap):
    image = (pixmap if isinstance(pixmap, QImage) else pixmap.toImage())
    image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    rows = np.frombuffer(image.bits(), dtype=np.uint8).reshape(
        image.height(), image.bytesPerLine())[:, :image.width() * 4]
    return np.count_nonzero(rows.reshape(image.height(), image.width(), 4)[:, :, 3])


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def game_assets():
    try:
        data = bootstrap.game_data_dir()
    except RuntimeError:
        pytest.skip("Set NME_GAME_DATA to a game Data folder for asset previews")
    install = GameInstallation.suggest(data)
    sets = install.set_directories()
    if not sets:
        pytest.skip("NME_GAME_DATA has no set assets")
    set_number, set_dir = min(sets.items())
    from unittest.mock import patch

    with patch.object(bootstrap, "_installation", install):
        assets = SetAssets(set_number, set_dir=set_dir).load()
        if assets.family is None:
            pytest.skip("The selected game set has no native filler family")
        library = SectorMeshLibrary(assets)
    return data, library


def test_ground_preview_renders_native_family_connections(app, game_assets):
    _, library = game_assets
    widget = GroundPreview()
    for ground in range(6):
        widget.set_ground(library, ground)
        assert widget.label.text() == "Connection to family 0"
        assert widget.detail.text() == "BASE surface unchanged"
        pixmap = widget.preview.pixmap()
        assert pixmap is not None and not pixmap.isNull(), ground
        assert _visible_pixels(pixmap) > 0
        assert pixmap.width() <= widget.preview.width()
        assert pixmap.height() <= widget.preview.height()


def test_symbol_preview_uses_native_sec4_and_sec8_tiles(app, game_assets):
    data, _ = game_assets
    fonts = bootstrap.find_ci(data, "Fonts")
    if fonts is None and data.name.casefold() in ("data", "dataxp"):
        fonts = bootstrap.find_ci(data.parent, "Fonts")
    if fonts is None or not (
            bootstrap.find_ci(fonts, "sec4.PNG", "sec4.ILB", "sec4.ILBM")
            and bootstrap.find_ci(fonts, "sec8.PNG", "sec8.ILB", "sec8.ILBM")):
        pytest.skip("NME_GAME_DATA has no sec4/sec8 strategic-map atlases")

    widget = SymbolPreview()
    widget.set_symbol(None, 2)
    assert widget.label.text() == "Map symbol · 4px / 8px"
    pixmap = widget.preview.pixmap()
    assert pixmap is not None and not pixmap.isNull()
    assert pixmap.size().width() == 68 and pixmap.size().height() == 32
    assert widget.preview.text() == ""
    image = pixmap.toImage()
    assert _visible_pixels(image.copy(0, 0, 32, 32)) > 0
    assert _visible_pixels(image.copy(36, 0, 32, 32)) > 0


def test_visual_aids_show_readable_fallback_for_invalid_values(app):
    ground = GroundPreview()
    ground.set_ground(None, 6)
    assert ground.preview.text() == "Preview unavailable"

    symbol = SymbolPreview()
    symbol.set_symbol(None, 256)
    assert symbol.preview.text() == "Preview unavailable"


def test_symbol_preview_falls_back_when_game_atlases_are_missing(app, monkeypatch):
    monkeypatch.setattr(bootstrap, "game_data_dir", lambda: Path("missing-data"))
    widget = SymbolPreview()
    widget.set_symbol(None, 2)
    assert widget.preview.text() == "Preview unavailable"
