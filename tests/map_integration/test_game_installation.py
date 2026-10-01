from pathlib import Path
import os

from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from map_editor import bootstrap
from map_editor.core.game_installation import (GameInstallation,
                                                   forget_remembered,
                                                   load_remembered,
                                                   save_remembered)
from map_editor.core.ldf_model import LdfDocument
from map_editor.core.resource_catalog import ResourceCatalog, music_path, preview_image
from map_editor.ui.dialogs import LevelInfoDialog


def test_suggestions_catalog_and_opt_in_paths(tmp_path):
    data = tmp_path / "Data"
    for folder in ("Sets", "Levels/Mbpix", "Objects", "Music", "Mov", "Scripts"):
        (data / folder).mkdir(parents=True)
    QImage(4, 4, QImage.Format.Format_RGB32).save(str(data / "Levels/Mbpix/MB_CUSTOM.PNG"))
    QImage(4, 4, QImage.Format.Format_RGB32).save(str(data / "Levels/Mbpix/DB_CUSTOM.PNG"))
    (data / "Music" / "custom.ogg").write_bytes(b"")
    (data / "Mov" / "opening.mp4").write_bytes(b"")
    (data / "Objects" / "newworld.bas").write_bytes(b"")
    install = GameInstallation.suggest(tmp_path)
    assert install.valid()
    assert install.folder("briefings") == data / "Levels/Mbpix"
    assert install.folder("skies") == data / "Objects"
    config = tmp_path / "config" / "installation.json"
    assert load_remembered(config) is None
    save_remembered(install, config)
    assert load_remembered(config) == install
    assert "MB_CUSTOM" not in config.read_text()
    catalog = ResourceCatalog(install, 1)
    assert catalog.briefing("mb", "MB_CUSTOM.IFF").name == "MB_CUSTOM.PNG"
    assert catalog.briefing("db", "DB_CUSTOM.PNG").name == "DB_CUSTOM.PNG"
    assert "newworld" in catalog.skies
    assert catalog.music == ["custom.ogg"]
    assert catalog.movies == ["opening.mp4"]
    forget_remembered(config)
    assert load_remembered(config) is None


def test_level_info_preserves_modern_briefing_and_decodes_installed_sky():
    app = QApplication.instance() or QApplication([])
    previous = bootstrap.installation()
    install = GameInstallation.suggest(bootstrap.game_data_dir())
    bootstrap.set_installation(install)
    try:
        doc = LdfDocument(mw=5, mh=5, set_number=1)
        doc.lvl_info.update(mbmap="MB_02.PNG", dbmap="DB_02.PNG",
                            sky="objects/asky2.bas", music="2", movie="None")
        dialog = LevelInfoDialog(doc)
        values = dialog.values()
        assert values["mbmap"] == "MB_02.PNG"
        assert values["dbmap"] == "DB_02.PNG"
        assert values["music"] == "2"
        sky = dialog.selected_sky_image()
        assert sky is not None and not sky.isNull()
        assert music_path(install, "5_2500_40000").name == "5.ogg"
        assert music_path(install, "None") is None
        assert not preview_image(install.folder("briefings") / "MB_02.PNG").isNull()
        dialog.close()
    finally:
        bootstrap.set_installation(previous)


def test_preview_cache_invalidates_local_file_changes(tmp_path):
    path=tmp_path/'preview.png'
    image=QImage(4,4,QImage.Format.Format_RGB32)
    image.fill(0xffff0000); image.save(str(path))
    assert preview_image(path).pixelColor(0,0).red()==255
    first=path.stat().st_mtime_ns
    image.fill(0xff0000ff); image.save(str(path))
    os.utime(path,ns=(first+1000000,first+1000000))
    assert preview_image(path).pixelColor(0,0).blue()==255
