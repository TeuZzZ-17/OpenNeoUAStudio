"""Icon Selector (type_icon + TYPE_NS.FON) and the script wireframe value."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import random

import pytest
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from entity_designer.script_model import (
    _SCRIPT_TAB_ICON,
    _SCRIPT_TAB_WIREFRAME,
    CollisionProject,
    apply_editable_overwrite_preview,
    build_editable_overwrite_preview,
    export_collision_text,
    find_script_blocks,
    import_type_icon_block,
    import_wireframe_block,
    plan_script_update,
    WEAPON,
)
from entity_designer.type_icons import (
    FREE, SHARED, TAKEN, ensure_font_key, load_atlas, new_key_allowed, parse_symbol,
    symbol_text, vanilla_readable,
)
from studio.formats.font_def import glyph_token, read_font, script_glyph_key

FON = (b"PAGE.ILBM 16   ; test\n"
       b"A    1   1 20     ; USR   TIGER\n"
       b"B   22   1 20     ; USR   PICO\n"
       b"\xa3  1  18 20     ; LEOPARD\n"
       b"U+2206  1  18 20 ; alias\n"
       b"?  291    1  4     ; corner\n")


@pytest.fixture
def data(tmp_path):
    root = tmp_path / "Data"
    (root / "Fonts" / "HFonts").mkdir(parents=True)
    (root / "Fonts" / "PAGE.PNG").write_bytes(b"")
    (root / "Fonts" / "HFonts" / "TYPE_NS.FON").write_bytes(FON)
    return root


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _atlas(root):
    return load_atlas(root, lambda _path: (316, 205))


def test_font_keys_follow_the_engine():
    assert script_glyph_key("A") == 65
    assert script_glyph_key("U+2206") == 0x2206
    assert script_glyph_key("\xec") == 0xec
    assert script_glyph_key("AB") == 65            # first byte, like GlyphKey::Parse
    assert script_glyph_key("#12") == ord("#")     # scripts have no numeric keys
    assert glyph_token(65) == "A"
    assert glyph_token(ord(";")) == "U+003B"
    assert glyph_token(0xec) == "U+00EC"
    assert parse_symbol("é") == 0xe9 and parse_symbol("u+00e9") is None
    assert parse_symbol("U+00E9") == 0xe9 and parse_symbol(" ") is None
    assert symbol_text(0xad) == "U+00AD"


def test_atlas_cells_and_symbol_checks(data):
    atlas = _atlas(data)
    assert (atlas.columns, atlas.rows, atlas.cell_width) == (15, 12, 20)
    assert atlas.cell_of_key(65) == (0, 0)
    assert [glyph.code for glyph in atlas.keys_on((0, 1))] == [0xa3, 0x2206]
    assert atlas.check(65, (0, 0))[0] == SHARED
    assert atlas.check(65, (3, 3))[0] == TAKEN
    assert atlas.check(0xe9, (3, 3))[0] == FREE
    free = atlas.random_free_key(random.Random(1))
    assert atlas.owner(free) is None


def test_new_symbol_is_appended_once_and_taken_symbols_are_refused(data):
    path = data / "Fonts" / "HFonts" / "TYPE_NS.FON"
    assert ensure_font_key(data, 0xe9, (3, 2), "New Tank")
    # One raw byte, like the vanilla loader reads it (never U+XXXX).
    assert path.read_bytes().endswith(b"\xe9  64  35 20 ; New Tank\n")
    assert path.read_bytes().startswith(FON)
    assert read_font(path).glyph(0xe9).x == 64
    assert not ensure_font_key(data, 0xe9, (3, 2))          # already there
    with pytest.raises(ValueError, match="TIGER"):
        ensure_font_key(data, 65, (3, 2))
    with pytest.raises(ValueError, match="one-byte"):
        ensure_font_key(data, 0x173, (3, 3))


def test_random_prefers_vanilla_readable_ascii(data):
    atlas = _atlas(data)
    for seed in range(20):
        code = atlas.random_free_key(random.Random(seed))
        assert vanilla_readable(code) and atlas.owner(code) is None
    assert not vanilla_readable(0xe9) and new_key_allowed(0xe9)
    assert not new_key_allowed(0x173) and not new_key_allowed(ord(";"))


def test_png_page_converts_to_ilbm_and_back(app, tmp_path):
    from entity_designer.icon_selector_panel import convert_font_page
    from studio.formats.images import parse_ilbm_file

    png = tmp_path / "PAGE.PNG"
    page = QImage(21, 17, QImage.Format.Format_RGB32)
    page.fill(QColor(255, 255, 0))
    page.setPixelColor(3, 4, QColor(10, 20, 30))
    page.save(str(png))
    ilbm = tmp_path / "PAGE.ILBM"
    convert_font_page(png, ilbm)
    image = parse_ilbm_file(ilbm)
    assert (image.width, image.height) == (21, 17)
    assert image.palette[image.pixels[4 * 21 + 3]] == (10, 20, 30)
    assert image.palette[image.pixels[0]] == (255, 255, 0)
    png.unlink()
    convert_font_page(ilbm, png)
    assert QImage(str(png)).pixelColor(3, 4) == QColor(10, 20, 30)


SCRIPT = ("new_vehicle 7\n"
          "    name = Tank\n"
          "    type_icon = A\n"
          "    wireframe = wireless/old.sklt\n"
          "end\n"
          "new_weapon 3\n"
          "    name = Shot\n"
          "end\n")


def test_script_values_are_read_written_and_copied():
    vehicle, weapon = find_script_blocks(SCRIPT)
    assert import_type_icon_block(SCRIPT, vehicle) == 65
    assert import_wireframe_block(SCRIPT, vehicle) == "wireless/old.sklt"
    project = CollisionProject(type_icon=0xe9, wireframe="Wireless/New.SKL")
    preview = build_editable_overwrite_preview(
        "new_vehicle", 7, project, {_SCRIPT_TAB_ICON, _SCRIPT_TAB_WIREFRAME})
    assert "; [Tab: Icon Selector]" in preview
    assert "type_icon = U+00E9" in preview
    assert "wireframe = Wireless/New.SKL" in preview
    updated, groups = apply_editable_overwrite_preview(SCRIPT, preview, "new_vehicle", 7)
    assert groups == {_SCRIPT_TAB_ICON, _SCRIPT_TAB_WIREFRAME}
    assert "type_icon = U+00E9" in updated and "type_icon = A" not in updated
    assert "wireframe = Wireless/New.SKL" in updated
    assert "wireless/old.sklt" not in updated.replace("; wireframe", "")
    applied, _diff, _name = plan_script_update(SCRIPT, "new_vehicle", 7, project)
    assert "type_icon = U+00E9" in applied and "wireframe = Wireless/New.SKL" in applied
    copied = export_collision_text(project)
    assert "type_icon = U+00E9" in copied and "wireframe = Wireless/New.SKL" in copied
    # Weapons get the wireframe, never the icon tab.
    weapon_project = CollisionProject(target_category=WEAPON, type_icon=65,
                                      wireframe="Wireless/Shot.SKL")
    preview = build_editable_overwrite_preview(
        "new_weapon", 3, weapon_project, {_SCRIPT_TAB_ICON, _SCRIPT_TAB_WIREFRAME})
    assert "Icon Selector" not in preview and "type_icon" not in preview
    assert "wireframe = Wireless/Shot.SKL" in preview


def _window(app):
    from entity_designer.editor import CollisionEditorWindow
    window = CollisionEditorWindow()
    window.show()
    app.processEvents()
    return window


def test_exported_wireframe_becomes_the_script_value(app, tmp_path):
    window = _window(app)
    try:
        script = tmp_path / "Data" / "Scripts" / "Vehicles.cfg"
        target = tmp_path / "Data" / "Wireless" / "New" / "Cube.SKL"
        target.parent.mkdir(parents=True)
        script.parent.mkdir(parents=True)
        window._active_script_path = script
        message = window.wireframe_exported(target)
        assert window.project.wireframe == "Wireless/New/Cube.SKL"
        assert "wireframe = Wireless/New/Cube.SKL" in message
        outside = window.wireframe_exported(tmp_path / "Other.SKL")
        assert "outside" in outside
        assert window.project.wireframe == "Wireless/New/Cube.SKL"
    finally:
        window._modified = False
        window.close()


def test_icon_tab_picks_cells_and_blocks_taken_symbols(app, data, monkeypatch):
    import entity_designer.icon_selector_panel as panel_module

    page = QImage(316, 205, QImage.Format.Format_RGBA8888)
    page.fill(QColor(255, 255, 0))
    monkeypatch.setattr(panel_module, "page_image", lambda _path: page)
    monkeypatch.setattr(panel_module.IconSelectorControls, "_data_root", lambda self: data)
    window = _window(app)
    try:
        window.properties_tabs.setCurrentIndex(window.icon_tab_index)
        app.processEvents()
        controls = window.icon_tab
        assert controls.view.isVisible() and not window.viewport.isVisible()
        controls._cell_picked(1, 0)                         # cell of 'B'
        assert window.project.type_icon == ord("B")
        assert window.project.type_icon_cell is None
        controls._cell_picked(4, 4)                         # empty cell, no free symbol yet
        assert window.project.type_icon == ord("B")
        controls.symbol_edit.setText("A")
        controls._symbol_edited("A")                        # 'A' draws another cell
        assert window.project.type_icon == ord("B")
        assert "TIGER" in controls.status_label.text()
        controls.symbol_edit.setText("U+0173")
        controls._symbol_edited("U+0173")                   # not one byte
        assert window.project.type_icon == ord("B")
        assert "one-byte" in controls.status_label.text()
        controls._random_symbol()
        assert window.project.type_icon not in (0, ord("A"), ord("B"))
        assert vanilla_readable(window.project.type_icon)
        assert window.project.type_icon_cell == (4, 4)
        window.undo()
        assert window.project.type_icon == ord("B")
        # A weapon can browse the page but gets no icon.
        window.project.target_category = WEAPON
        window._sync_all()
        assert window.properties_tabs.isTabEnabled(window.icon_tab_index)
        assert not controls.symbol_edit.isEnabled()
        icon = window.project.type_icon
        controls._cell_picked(0, 0)
        assert controls.view.selected == (0, 0)
        assert window.project.type_icon == icon
        assert "Open a vehicle" in controls.status_label.text()
    finally:
        window._modified = False
        window.close()
