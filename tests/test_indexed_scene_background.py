"""CPU integration coverage for the indexed viewport destination bridge."""

import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from assembly_viewer import AssetViewport
from indexed_renderer import IndexedTables


class IndexedSceneBackgroundTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _viewport():
        viewport = AssetViewport()
        palette = tuple((index, index, index) for index in range(256))
        identity = bytes(range(256)) * 256
        viewport._indexed_adapter = SimpleNamespace(
            tables=IndexedTables(palette, identity, identity))
        return viewport

    def test_rgb_viewport_background_maps_to_tight_index_rows(self):
        viewport = self._viewport()
        image = QImage(3, 2, QImage.Format.Format_RGB32)
        expected = bytes((10, 20, 30, 40, 50, 60))
        for y in range(image.height()):
            for x in range(image.width()):
                value = expected[y * image.width() + x]
                image.setPixelColor(x, y, QColor(value, value, value))

        indices = viewport._indexed_background_bytes(image)

        self.assertEqual(indices, expected)
        self.assertEqual(len(indices), image.width() * image.height())

    def test_indexed_scene_grid_matches_crop_of_standard_viewport_grid(self):
        viewport = self._viewport()
        viewport._show_grid = True
        viewport._show_axes = False
        target = QRectF(11, 9, 64, 64)
        camera = viewport._camera_state()
        background = QColor(3, 4, 5)

        # The regular viewport draws into local widget coordinates. Give the
        # indexed helper an equivalent target offset in its parent painter and
        # verify the translated capture matches the standard local grid.
        standard_crop = QImage(64, 64, QImage.Format.Format_RGB32)
        standard_crop.fill(background)
        painter = QPainter(standard_crop)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        viewport._draw_grid(painter, QRectF(0, 0, 64, 64), camera)
        painter.end()

        indexed_background = viewport._indexed_scene_background(
            target, camera, background, clean=False)

        self.assertEqual(indexed_background.size(), standard_crop.size())
        channel_deltas = []
        for y in range(64):
            for x in range(64):
                actual = indexed_background.pixelColor(x, y)
                expected = standard_crop.pixelColor(x, y)
                channel_deltas.extend((
                    abs(actual.red() - expected.red()),
                    abs(actual.green() - expected.green()),
                    abs(actual.blue() - expected.blue()),
                ))
        self.assertEqual(max(channel_deltas), 0)
        self.assertTrue(any(
            standard_crop.pixel(x, y) != background.rgb()
            for y in range(64) for x in range(64)))


if __name__ == "__main__":
    unittest.main()
