from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel

from map_editor.core.ldf_model import LdfDocument
from map_editor.ui import dialogs


class _EmptyCatalog:
    briefings = {"mb": {}, "db": {}}
    skies = {}
    music = []
    movies = []
    palette = None

    @staticmethod
    def briefing(_prefix, _value):
        return None


def test_level_info_sky_label_and_removed_helper_copy(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(dialogs, "ResourceCatalog",
                        lambda _installation, _set_number: _EmptyCatalog())
    monkeypatch.setattr(dialogs.bootstrap, "installation", lambda: None)
    monkeypatch.setattr(dialogs.bootstrap, "game_data_dir", lambda: tmp_path)

    doc = LdfDocument()
    before = dict(doc.lvl_info)
    panel = dialogs.LevelInfoPanel(doc)
    try:
        labels = [label.text() for label in panel.findChildren(QLabel)]
        assert "Artwork is read from your selected game folders." not in labels
        assert "Choose from the installation or type a custom value." not in labels

        sky_label = next(label for label in panel.findChildren(QLabel)
                         if label.text() == "Select Sky:")
        assert sky_label.alignment() & Qt.AlignmentFlag.AlignHCenter
        assert panel.layout().spacing() == 6
        assert doc.lvl_info == before
    finally:
        panel.dispose()
        panel.close()
        app.processEvents()


def test_sky_grid_fits_large_thumbnails_in_narrow_and_wide_views(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(dialogs, "ResourceCatalog",
                        lambda _installation, _set_number: _EmptyCatalog())
    monkeypatch.setattr(dialogs.bootstrap, "installation", lambda: None)
    monkeypatch.setattr(dialogs.bootstrap, "game_data_dir", lambda: tmp_path)

    panel = dialogs.LevelInfoPanel(LdfDocument())
    try:
        assert panel.sky_list.minimumHeight() == 166
        assert panel.sky_list.maximumHeight() == 278
        panel.show()

        for panel_width, expected_columns in ((300, 1), (740, 2)):
            panel.resize(panel_width, 800)
            panel.layout().activate()
            app.processEvents()
            panel._layout_sky_grid()
            viewport_width = panel.sky_list.viewport().width()
            grid = panel.sky_list.gridSize()
            spacing = panel.sky_list.spacing()
            assert grid.height() == 166
            assert panel.sky_list.iconSize().height() == 124
            assert panel.sky_list.iconSize().width() == grid.width() - 18

            if expected_columns == 1:
                assert grid.width() + spacing * 2 <= viewport_width
                assert grid.width() * 2 + spacing * 3 > viewport_width
            else:
                assert grid.width() * 2 + spacing * 3 <= viewport_width

            # ThumbnailDelegate leaves a separate row for the label below the fitted image.
            card_bottom = grid.height() - 4
            image_bottom = card_bottom - 30
            text_top = card_bottom - 26
            assert grid.height() - 42 == 124
            assert image_bottom < text_top
            assert panel.sky_list.iconSize().width() <= grid.width() - 18
    finally:
        panel.dispose()
        panel.close()
        app.processEvents()
