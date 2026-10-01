import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


from map_editor import bootstrap
from map_editor.core.building_defs import (
    load_building_files,
    parse_building_text,
)


def test_parse_supports_cfg_and_scr_syntax(tmp_path):
    text = (
        "new_building 1\n"
        "    model = kraftwerk\n"
        "    name = Power_Station_II\n"
        "    sec_type = 201\n"
        "    power = 128\n"
        "    energy = 140000\n"
        "    production_cost = 1400\n"
        "end\n"
        "new_building 54\n"
        "    model = radar\n"
        "    name = Radar_Station\n"
        "    sec_type = 204\n"
        "    power = 15\n"
        "    sbact_act = 0\n"
        "    sbact_vehicle = 98\n"
        "    sbact_pos_x = 30\n"
        "    sbact_pos_y = -530\n"
        "    sbact_pos_z = 0\n"
        "    sbact_dir_x = -1\n"
        "    sbact_dir_y = 0\n"
        "    sbact_dir_z = 0\n"
        "end\n"
    )
    cfg = tmp_path / "Buildings.cfg"
    scr = tmp_path / "legacy.scr"
    cfg.write_text(text, encoding="utf-8")
    scr.write_text(text, encoding="cp1252")
    for path in (cfg, scr):
        definitions = parse_building_text(
            path.read_bytes().decode("cp1252"), path.name)
        power = definitions[1]
        assert power.sec_type == 201 and power.power == 128
        assert power.energy_levels == 3 and not power.has_guns
        assert power.kinds == ("power",)
        radar = definitions[54]
        assert radar.is_radar and radar.has_guns and not radar.has_power
        assert set(radar.kinds) == {"flak", "radar"}
        assert len(radar.guns) == 1 and radar.guns[0].vehicle == 98
    merged = load_building_files(tmp_path)
    assert set(merged) == {1, 54}
    assert merged[1].source == "Buildings.cfg"


def test_installation_scripts_include_scr_and_buildings():
    install = None
    try:
        from map_editor.core.game_installation import GameInstallation
        install = GameInstallation.suggest(bootstrap.game_data_dir())
    except RuntimeError:
        return
    counts = install.inventory()
    assert counts["scripts"] >= 2
    definitions = load_building_files(install.folder("scripts"))
    assert 1 in definitions and 54 in definitions
    assert definitions[1].sec_type == 201
    assert definitions[54].is_radar
    assert any(len(d.guns) >= 4 for d in definitions.values())


def test_building_tool_paints_sector_and_script_id_with_undo(app):
    from PySide6.QtCore import Qt
    from map_editor.core.ldf_model import LdfDocument
    from map_editor.ui.main_window import MainWindow
    win = MainWindow()
    win._icons.stop()
    assert 1 in win.buildings
    assert [win.palette_tabs.tabText(i) for i in range(win.palette_tabs.count())][1] == "Buildings"
    assert win.palette_dock.windowTitle() == "Menu"
    win.set_tool("building")
    win.sel_building = 54
    win._pressed(4, 4, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    win._released(4, 4, Qt.KeyboardModifier.NoModifier)
    definition = win.buildings[54]
    assert win.doc.grids["type"][4][4] == f"{definition.sec_type:02x}"
    assert win.doc.grids["blg"][4][4] == "36"
    assert "Building 54" in win.info.text()
    win.undo()
    assert win.doc.grids["blg"][4][4] == "00"
    win.redo()
    assert win.doc.grids["blg"][4][4] == "36"
    # Plain sectors with the same look carry no script id, so the overlay
    # leaves them alone while marked buildings glow.
    win.set_tool("sector")
    win.sel_typ = definition.sec_type
    win._pressed(5, 5, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    win._released(5, 5, Qt.KeyboardModifier.NoModifier)
    assert win.doc.grids["blg"][5][5] == "00"
    win.building_overlay.update()
    win.dirty = False
    win.close()


def test_building_overlay_paints_without_errors(app):
    from map_editor.core.ldf_model import LdfDocument
    from map_editor.ui.main_window import MainWindow
    win = MainWindow()
    win._icons.stop()
    win.resize(900, 700)
    win.show()
    app.processEvents()
    win.building_overlay.update()
    app.processEvents()
    win.view.grab()
    win.view.grab()
    win.dirty = False
    win.close()
