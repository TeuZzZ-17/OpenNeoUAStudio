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


def test_level_info_media_choices_come_from_current_folders(tmp_path):
    from map_editor.ui.dialogs import LevelInfoPanel

    app = QApplication.instance() or QApplication([])
    data = tmp_path / 'Data'
    for folder, names in (
            ('Music', ('My_New_Ambience.ogg', 'Another.OGG', 'ignore.txt')),
            ('Mov', ('My_New_Intro.mpg', 'Another.MP4', 'ignore.txt'))):
        directory = data / folder
        directory.mkdir(parents=True)
        for name in names:
            (directory / name).write_bytes(b'')
    previous = bootstrap.installation()
    try:
        bootstrap.set_installation(GameInstallation.suggest(data))
        panel = LevelInfoPanel(LdfDocument(mw=7, mh=7))
        music = [panel.music_box.itemText(i) for i in range(panel.music_box.count())]
        movies = [panel.movie_box.itemText(i) for i in range(panel.movie_box.count())]
        assert music == ['None', 'Another.OGG', 'My_New_Ambience.ogg']
        assert movies == ['None', 'Another.MP4', 'My_New_Intro.mpg']
        panel.dispose()
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


def _make_set(folder):
    (folder / "Objects").mkdir(parents=True)
    (folder / "Scripts").mkdir(parents=True)
    (folder / "Objects" / "SET.BAS").write_bytes(b"")
    (folder / "Scripts" / "set.sdf").write_bytes(b"")


def test_vanilla_loose_set_folders_are_found_without_a_sets_parent(tmp_path):
    for number in (1, 4):
        _make_set(tmp_path / f"set{number}")
    install = GameInstallation.suggest(tmp_path)
    assert install.valid()
    assert install.available_sets() == [1, 4]
    previous = bootstrap.installation()
    bootstrap.set_installation(install)
    try:
        assert bootstrap.set_dir(4) == tmp_path / "set4"
    finally:
        bootstrap.set_installation(previous)


def test_sets_field_may_point_to_one_set_or_the_modern_parent(tmp_path):
    _make_set(tmp_path / "Sets" / "Set2")
    _make_set(tmp_path / "loose" / "Set3")
    modern = GameInstallation.suggest(tmp_path)
    assert modern.available_sets() == [2]
    single = GameInstallation(tmp_path, {"sets": tmp_path / "loose" / "Set3"})
    assert single.available_sets() == [3]



def _disk_names(path):
    """Names as stored on disk (Path equality ignores case on Windows)."""
    return list(path.parts)


def test_vanilla_layout_reads_levels_and_music_from_the_game_root(tmp_path):
    # Vanilla keeps LEVELS and MUSIC next to DATA, all in upper case.
    root = tmp_path / "urban-assault"
    data = root / "DATA"
    _make_set(data / "SET1")
    (data / "SCRIPTS").mkdir()
    (root / "LEVELS" / "MBPIX").mkdir(parents=True)
    (root / "LEVELS" / "SINGLE").mkdir()
    (root / "LEVELS" / "SINGLE" / "L0101.LDF").write_bytes(b"")
    (root / "LEVELS" / "MBPIX" / "MB_01.IFF").write_bytes(b"")
    (root / "LEVELS" / "MBPIX" / "DB_01.ILB").write_bytes(b"")
    (root / "MUSIC").mkdir()
    for chosen in (root, data):
        install = GameInstallation.suggest(chosen)
        assert _disk_names(install.data)[-1] == "DATA"
        assert _disk_names(install.folder("levels"))[-2:] == ["urban-assault", "LEVELS"]
        assert _disk_names(install.folder("briefings"))[-2:] == ["LEVELS", "MBPIX"]
        assert _disk_names(install.folder("music"))[-2:] == ["urban-assault", "MUSIC"]
        assert _disk_names(install.folder("scripts"))[-1] == "SCRIPTS"
        counts = install.inventory()
        assert (counts["sets"], counts["levels"], counts["mb"], counts["db"]) == (1, 1, 1, 1)


def test_data_folders_win_over_the_game_root(tmp_path):
    for folder in ("Data/Levels", "Levels", "Data/Music", "Music"):
        (tmp_path / folder).mkdir(parents=True)
    install = GameInstallation.suggest(tmp_path)
    assert install.folder("levels") == tmp_path / "Data" / "Levels"
    assert install.folder("music") == tmp_path / "Data" / "Music"


def test_missing_remembered_folder_is_guessed_again(tmp_path):
    root = tmp_path / "game"
    _make_set(root / "Data" / "Set1")
    (root / "Levels").mkdir()
    install = GameInstallation.suggest(root)
    payload = install.to_json()
    payload["folders"]["levels"] = str(root / "Data" / "Levels")  # wrong, older guess
    restored = GameInstallation.from_json(payload)
    assert restored.folder("levels") == root / "Levels"


def test_find_existing_path_returns_the_disk_spelling(tmp_path):
    from studio.exchange.portable import find_existing_path

    (tmp_path / "DATA" / "Set1" / "objects").mkdir(parents=True)
    (tmp_path / "DATA" / "Set1" / "objects" / "SET.BAS").write_bytes(b"")
    found = find_existing_path(tmp_path, "data", r"set1/OBJECTS\set.bas")
    assert _disk_names(found)[-4:] == ["DATA", "Set1", "objects", "SET.BAS"]
    assert find_existing_path(tmp_path, "data", "missing") is None
    assert find_existing_path(tmp_path / "nowhere", "data") is None
    assert bootstrap.find_ci(tmp_path / "DATA", "nope", "SET1").name == "Set1"
