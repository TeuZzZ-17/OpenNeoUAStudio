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
