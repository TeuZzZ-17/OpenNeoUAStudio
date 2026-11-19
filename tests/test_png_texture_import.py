"""External PNG import for Replace Texture: exact-palette-match guard."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtGui import QColor, QImage

from studio.formats.images import TextureConvertError, load_png_matching_palette


PALETTE = [(0, 0, 0), (255, 0, 0), (0, 255, 0), (0, 0, 255)]


def _write_png(path, pixels):
    width = len(pixels[0])
    height = len(pixels)
    image = QImage(width, height, QImage.Format.Format_RGB32)
    for y, row in enumerate(pixels):
        for x, color in enumerate(row):
            image.setPixelColor(x, y, QColor(*color))
    assert image.save(str(path), "PNG")


def test_load_png_matching_palette_accepts_exact_colors(tmp_path):
    path = tmp_path / "patch.png"
    _write_png(path, [[(255, 0, 0), (0, 255, 0)],
                      [(0, 0, 255), (0, 0, 0)]])
    image = load_png_matching_palette(path, PALETTE)
    assert image.width == 2 and image.height == 2
    assert image.palette == PALETTE
    assert list(image.pixels) == [1, 2, 3, 0]


def test_load_png_matching_palette_rejects_out_of_palette_colors(tmp_path):
    path = tmp_path / "patch.png"
    _write_png(path, [[(255, 0, 0), (128, 64, 200)]])
    with pytest.raises(TextureConvertError, match="colors outside"):
        load_png_matching_palette(path, PALETTE)


def test_load_png_matching_palette_reports_missing_file(tmp_path):
    with pytest.raises(TextureConvertError, match="could not read"):
        load_png_matching_palette(tmp_path / "missing.png", PALETTE)
