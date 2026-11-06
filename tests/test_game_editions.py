"""The loader finds and catalogues OpenNeoUA, the original game and its expansion by itself."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from map_editor.core.game_installation import (
    EXPANSION,
    GameInstallation,
    locate_data,
    MISSING,
    NOT_IN_EDITION,
    OPENNEOUA,
    READY,
    REQUIRED_MISSING,
    VANILLA,
)
from studio.ui.shell import ResourcesPanel
from studio.workbench.model_import import ModelImportDialog

ORIGINALS = Path(__file__).resolve().parents[2] / "Urban Assault + Espansione Originale"


def make_set(folder: Path) -> None:
    (folder / "OBJECTS").mkdir(parents=True)
    (folder / "SCRIPTS").mkdir()
    (folder / "OBJECTS" / "SET.BAS").touch()
    (folder / "SCRIPTS" / "SET.SDF").touch()


@pytest.fixture
def vanilla(tmp_path):
    root = tmp_path / "Urban Assault"
    make_set(root / "DATA" / "SET1")
    (root / "UA.EXE").touch()
    (root / "DATA" / "SCRIPTS").mkdir()
    (root / "DATA" / "SCRIPTS" / "STARTUP.SCR").touch()
    (root / "DATA" / "OBJECTS").mkdir()
    (root / "DATA" / "OBJECTS" / "SKY.BAS").touch()
    (root / "LEVELS" / "SINGLE").mkdir(parents=True)
    (root / "LEVELS" / "SINGLE" / "L0101.LDF").touch()
    return root


@pytest.fixture
def expansion(tmp_path):
    root = tmp_path / "Pack" / "Pack" / "Game"
    make_set(root / "dataxp" / "SET1")
    (root / "Ua_xp.exe").touch()
    (root / "dataxp" / "Scripts").mkdir()
    (root / "dataxp" / "Scripts" / "startup.scr").touch()
    (root / "levelsxp").mkdir()
    (root / "levelsxp" / "L0101.LDF").touch()
    return root


@pytest.fixture
def openneoua(tmp_path):
    root = tmp_path / "UA Rising"
    make_set(root / "Data" / "Sets" / "Set1")
    (root / "OpenNeoUA.exe").touch()
    (root / "Data" / "Scripts").mkdir()
    (root / "Data" / "Scripts" / "Startup.cfg").touch()
    (root / "Data" / "Models" / "Base").mkdir(parents=True)
    (root / "Data" / "Models" / "Base" / "One.BASE").touch()
    (root / "Data" / "Graphics" / "StatusIcons").mkdir(parents=True)
    (root / "Data" / "Graphics" / "StatusIcons" / "a.png").touch()
    (root / "Data" / "Collisions").mkdir()
    (root / "Data" / "Collisions" / "Tank.collision").touch()
    return root


def states(install):
    return {item.key: item.state for item in install.report()}


def test_any_folder_of_the_game_leads_to_the_same_installation(openneoua):
    data = openneoua / "Data"
    for chosen in (openneoua, data, data / "Sets", data / "Sets" / "Set1", data / "Scripts"):
        assert locate_data(chosen) == data
    assert GameInstallation.suggest(data).root == openneoua


def test_the_expansion_is_found_inside_its_wrapper_folders(expansion):
    install = GameInstallation.suggest(expansion.parent.parent)
    assert install.data == expansion / "dataxp"
    assert install.root == expansion
    assert install.edition == EXPANSION
    assert install.available_sets() == [1]


def test_each_edition_is_recognised(vanilla, expansion, openneoua):
    assert GameInstallation.suggest(vanilla).edition == VANILLA
    assert GameInstallation.suggest(expansion).edition == EXPANSION
    assert GameInstallation.suggest(openneoua).edition == OPENNEOUA


def test_openneoua_folders_are_not_missing_in_the_original_game(vanilla):
    found = states(GameInstallation.suggest(vanilla))
    assert found["models"] == found["graphics"] == found["collisions"] == NOT_IN_EDITION
    assert found["sets"] == found["scripts"] == found["levels"] == READY
    assert found["music"] == MISSING


def test_openneoua_reports_its_own_folders(openneoua):
    found = states(GameInstallation.suggest(openneoua))
    assert found["models"] == found["graphics"] == found["collisions"] == READY


def test_a_set_of_folder_inside_a_set_is_not_taken_for_the_skies(expansion):
    install = GameInstallation.suggest(expansion)
    assert states(install)["skies"] == MISSING


def test_an_installed_expansion_borrows_what_it_lacks_from_the_base_game(expansion):
    (expansion / "Data" / "Objects").mkdir(parents=True)
    (expansion / "Data" / "Objects" / "SKY.BAS").touch()
    install = GameInstallation.suggest(expansion / "dataxp")
    assert install.data == expansion / "dataxp"
    assert install.folder("skies") == expansion / "Data" / "Objects"


def test_a_required_folder_that_is_missing_is_named(openneoua):
    install = GameInstallation.suggest(openneoua)
    install.folders["scripts"] = openneoua / "Nowhere"
    assert states(install)["scripts"] == REQUIRED_MISSING


def test_the_panel_shows_the_game_folder_and_locks_what_the_game_does_not_have(vanilla):
    app = QApplication.instance() or QApplication([])
    panel = ResourcesPanel(GameInstallation.suggest(vanilla))
    assert panel.root.text() == str(vanilla)
    assert not panel.edits["models"].isEnabled()
    assert not panel.browse_buttons["graphics"].isEnabled()
    assert panel.edits["music"].isEnabled()
    assert panel.edits["music"].text() == ""
    assert "1 sets" in panel.states["sets"].text()
    assert "Urban Assault (original)" in panel.inventory_label.text()
    panel.close()


def test_a_saved_data_folder_is_still_understood(openneoua):
    remembered = {"data": str(openneoua / "Data"), "folders": {}}
    install = GameInstallation.from_json(remembered)
    assert install.root == openneoua


def test_the_model_picker_starts_from_the_game_folder_even_with_a_model_open(openneoua, tmp_path):
    app = QApplication.instance() or QApplication([])
    elsewhere = tmp_path / "elsewhere" / "Tank.BASE"
    elsewhere.parent.mkdir()
    elsewhere.write_bytes(b"")
    dialog = ModelImportDialog(initial_directory=openneoua, initial_base=elsewhere)
    assert dialog._base_start == openneoua
    dialog.close()


@pytest.mark.parametrize("folder, edition, sets", [
    ("Microsoft Urban Assault/Microsoft Urban Assault", VANILLA, [1, 2, 3, 4, 5, 6, 46]),
    ("Microsoft Urban Assault Espansione", EXPANSION, [1, 2, 3, 4, 5, 6, 46]),
])
def test_the_real_original_games(folder, edition, sets):
    if not ORIGINALS.is_dir():
        pytest.skip("original games are not on this computer")
    install = GameInstallation.suggest(ORIGINALS / folder)
    assert install.edition == edition
    assert install.available_sets() == sets
    assert states(install)["levels"] == READY
