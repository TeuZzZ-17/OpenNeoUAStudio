"""The Model and Ground decal rows of the Visuals && FX effects list."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from entity_designer import destruction_fx as fx
from entity_designer import fx_visuals as visuals
from entity_designer.fx_panel import DestructionFxPanel
from entity_designer.script_model import find_script_blocks

SCRIPT = ("new_weapon 6\n    vp_normal = 20\n    base_dead = Data/Models/Base/D.BASE\n"
          "begin_fx\n    trigger = detonate\n    mode = visual\n    vp_model = 5\nend\nend\n")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _panel(app, tmp_path, script=SCRIPT):
    path = tmp_path / "Weapons.cfg"
    path.write_text(script, encoding="utf-8")
    block = find_script_blocks(script)[0]
    panel = DestructionFxPanel()
    panel.set_context(fx.CONTEXT_WEAPON,
                      fx.extract_fx_document(script, block.start_line, block.end_line))
    panel.set_script_sources([(path, block.start_line, block.end_line)])
    return panel, path


def _rows(panel):
    return {panel.effects_list.item(i).data(Qt.ItemDataRole.UserRole): panel.effects_list.item(i)
            for i in range(panel.effects_list.count())}


def test_only_existing_effect_rows_are_shown(app, tmp_path):
    panel, _path = _panel(app, tmp_path)
    try:
        assert set(_rows(panel)) == {visuals.MODEL_INDEX, 0}
        empty = "new_weapon 6\n    name = Test\nend\n"
        path = tmp_path / "Empty.cfg"
        path.write_text(empty, encoding="utf-8")
        block = find_script_blocks(empty)[0]
        panel.set_context(fx.CONTEXT_WEAPON, fx.extract_fx_document(empty, 0, block.end_line))
        panel.set_script_sources([(path, 0, block.end_line)])
        assert panel.effects_list.count() == 0
    finally:
        panel.close()


def test_new_crater_row_is_red_and_disappears_on_reset(app, tmp_path):
    panel, _path = _panel(app, tmp_path)
    panel.show()
    try:
        assert visuals.CRATER_INDEX not in _rows(panel)
        panel.add_effect()
        drawer = panel.code_drawer
        drawer.code.setPlainText("crater_radius = 300\ncrater_depth = 150")
        assert drawer._apply()
        row = _rows(panel)[visuals.CRATER_INDEX]
        assert "Radius 300" in row.text() and "Depth 150" in row.text()
        assert "Not vanilla compatible" in row.toolTip()
        assert row.icon().pixmap(12, 12).toImage().pixelColor(6, 6).name() == "#e0524a"
        drawer._reset()
        assert visuals.CRATER_INDEX not in _rows(panel)
    finally:
        panel.close()


def test_saved_crater_row_is_not_vanilla_compatible(app, tmp_path):
    text = "new_weapon 6\n    crater_radius = 300\n    crater_depth = 150\nend\n"
    path = tmp_path / "Weapons.cfg"
    path.write_text(text, encoding="utf-8")
    block = find_script_blocks(text)[0]
    panel = DestructionFxPanel()
    try:
        panel.set_context(fx.CONTEXT_WEAPON, fx.extract_fx_document(text, 0, block.end_line))
        panel.set_script_sources([(path, 0, block.end_line)])
        row = _rows(panel)[visuals.CRATER_INDEX]
        assert "Not vanilla compatible" in row.toolTip()
        assert row.icon().pixmap(12, 12).toImage().pixelColor(6, 6).name() == "#e0524a"
        assert visuals.DECAL_INDEX not in _rows(panel)
    finally:
        panel.close()


def test_projectile_altitude_default_and_label(app):
    panel = DestructionFxPanel()
    try:
        panel.set_context(fx.CONTEXT_WEAPON, fx.FxDocument(),
                          unit=fx.UnitPhases(normal=fx.VisualRef(60), weapon=True))
        assert panel.altitude_label.text() == "Projectile altitude"
        assert panel.altitude_spin.value() == 300
        assert panel.altitude_slider.value() == 300
        simulation = fx.PreviewSimulation(fx.FxDocument(), fx.CONTEXT_WEAPON,
                                          fx.WEAPON_HIT_GROUND, unit=panel.unit)
        projectile = next(item for item in simulation.items_at(0) if item.kind == "unit")
        assert projectile.position[1] == pytest.approx(-300)
    finally:
        panel.close()


def test_fx_timeline_starts_at_quarter_speed_and_can_be_changed(app):
    panel = DestructionFxPanel()
    assert panel.speed == panel.speed_combo.currentData() == 0.25
    panel.speed_combo.setCurrentIndex(panel.speed_combo.findData(1.0))
    panel.set_context(fx.CONTEXT_WEAPON, fx.FxDocument())
    assert panel.speed == panel.speed_combo.currentData() == 1.0
    panel.close()


@pytest.mark.parametrize(("index", "old", "new"), [
    (visuals.MODEL_INDEX, "vp_normal = 20", "vp_normal = 21"),
    (visuals.DECAL_INDEX, "", "ground_decal_id = 2"),
])
def test_saving_closes_the_editor_even_when_the_visuals_reload(app, tmp_path, index, old, new):
    panel, path = _panel(app, tmp_path)
    panel.show()

    def reload_visuals():
        text = path.read_text(encoding="utf-8")
        block = find_script_blocks(text)[0]
        panel.set_context(fx.CONTEXT_WEAPON,
                          fx.extract_fx_document(text, block.start_line, block.end_line),
                          keep_view=True)
        panel.set_script_sources([(path, block.start_line, block.end_line)])

    panel.visualsSaved.connect(reload_visuals)
    try:
        panel.edit_effect_code(index)
        drawer = panel.code_drawer
        QTest.qWait(200)
        text = drawer.code.toPlainText()
        drawer.code.setPlainText(text.replace(old, new) if old else new)
        drawer._apply()
        drawer.code.save_button.click()
        QTest.qWait(350)
        assert new in path.read_text(encoding="utf-8")
        assert not drawer.expanded
        assert not drawer.isVisible()
        assert not panel.has_unsaved_text
    finally:
        panel.close()


def test_failed_save_keeps_the_editor_open_and_the_draft(app, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    panel, path = _panel(app, tmp_path)
    panel.show()
    try:
        panel.edit_effect_code(visuals.DECAL_INDEX)
        drawer = panel.code_drawer
        drawer.code.setPlainText("ground_decal_id = 2")
        path.write_text(SCRIPT + "; external change\n", encoding="utf-8")
        monkeypatch.setattr(QMessageBox, "warning", lambda *args: None)
        assert not drawer._save()
        assert drawer.expanded
        assert drawer.code.toPlainText() == "ground_decal_id = 2"
        assert panel.has_unsaved_text
        assert "ground_decal_id" not in path.read_text(encoding="utf-8")
    finally:
        panel.close()


def test_one_model_row_leads_the_effects_list(app, tmp_path):
    panel, _path = _panel(app, tmp_path)
    rows = _rows(panel)
    assert list(rows)[0] == visuals.MODEL_INDEX and 0 in rows
    assert [i for i in rows if i < 0] == [visuals.MODEL_INDEX]
    assert rows[visuals.MODEL_INDEX].text().splitlines() == [
        "Model", "Normal: VP 20", "Dead: BASE Data/Models/Base/D.BASE"]
    assert not hasattr(panel, "show_unit")


def test_unchecking_the_model_row_hides_every_model_state(app, tmp_path):
    panel, _path = _panel(app, tmp_path)
    _rows(panel)[visuals.MODEL_INDEX].setCheckState(Qt.CheckState.Unchecked)
    assert panel.hidden_states == {"normal", "fire", "wait", "dead", "megadeth", "genesis"}
    panel.show_all()
    assert panel.hidden_states == set()
    panel.hide_all()
    assert visuals.MODEL_INDEX in panel.hidden_items and panel.hidden_states


def test_the_model_row_opens_one_editor_with_every_state_and_saves_it(app, tmp_path):
    panel, path = _panel(app, tmp_path)
    saved = []
    panel.visualsSaved.connect(lambda: saved.append(True))
    panel.edit_effect_code(visuals.MODEL_INDEX)
    drawer = panel.code_drawer
    assert drawer.title_label.text() == "Model"
    assert drawer.code.toPlainText() == (
        "vp_normal = 20\n\nbase_dead = Data/Models/Base/D.BASE")
    drawer.code.setPlainText(
        "vp_normal = 20\nvp_fire = 33\n3ds_fire = Data/Models/3ds/f.3ds\nvp_genesis = 7")
    drawer._apply()
    assert panel.has_unsaved_text and drawer.title_label.text().endswith("*")
    assert drawer._save()
    text = path.read_text(encoding="utf-8")
    assert "vp_fire = 33" in text and "3ds_fire = Data/Models/3ds/f.3ds" in text
    assert "vp_genesis = 7" in text and "base_dead" not in text  # a removed line leaves the script
    assert saved == [True]


def test_a_wrong_model_text_is_refused_and_the_file_stays(app, tmp_path):
    panel, path = _panel(app, tmp_path)
    panel.edit_effect_code(visuals.MODEL_INDEX)
    drawer = panel.code_drawer
    for bad in ("vp_normal = abc", "energy = 5", "vp_normal"):
        drawer.code.setPlainText(bad)
        assert drawer._apply() is False
    assert path.read_text(encoding="utf-8") == SCRIPT


def test_ground_decal_row_writes_the_size_after_the_decal_id(app, tmp_path):
    path = tmp_path / "Weapons.cfg"
    script = "new_weapon 6\n    vp_normal = 20\n    ground_decal_id = 1\n    energy = 5\nend\n"
    path.write_text(script, encoding="utf-8")
    block = find_script_blocks(script)[0]
    panel = DestructionFxPanel()
    panel.set_context(fx.CONTEXT_WEAPON,
                      fx.extract_fx_document(script, block.start_line, block.end_line))
    panel.set_script_sources([(path, block.start_line, block.end_line)])
    row = _rows(panel)[visuals.DECAL_INDEX]
    assert row.text().splitlines() == ["Ground decal", "ID 1"]
    panel.edit_effect_code(visuals.DECAL_INDEX)
    drawer = panel.code_drawer
    assert drawer.title_label.text() == "Ground decal"
    assert drawer.code.toPlainText() == "ground_decal_id = 1"
    drawer.code.setPlainText("ground_decal_id = 2\nground_decal_size = 30~50")
    assert drawer._save()
    assert path.read_text(encoding="utf-8") == (
        "new_weapon 6\n    vp_normal = 20\n    ground_decal_id = 2\n"
        "    ground_decal_size = 30~50\n    energy = 5\nend\n")
    # The editor reloads the definition after a visual save; do the same here.
    saved_block = find_script_blocks(path.read_text(encoding="utf-8"))[0]
    panel.set_script_sources([(path, saved_block.start_line, saved_block.end_line)])
    assert _rows(panel)[visuals.DECAL_INDEX].text().splitlines() == ["Ground decal", "ID 2 · Size 30~50"]
    panel.edit_effect_code(visuals.DECAL_INDEX)
    drawer = panel.code_drawer
    drawer.code.setPlainText("")
    assert drawer._save()
    assert "ground_decal" not in path.read_text(encoding="utf-8")
    assert visuals.DECAL_INDEX not in _rows(panel)


def test_a_wrong_decal_size_is_refused_and_the_file_stays(app, tmp_path):
    panel, path = _panel(app, tmp_path)
    panel.edit_effect_code(visuals.DECAL_INDEX)
    drawer = panel.code_drawer
    for bad in ("ground_decal_size = abc", "ground_decal_size = 13000", "ground_decal_size = -1~5",
                "ground_decal_size = 1~2~3", "vp_normal = 5", "ground_decal_size",
                "ground_decal_id = abc", "ground_decal_id = -2"):
        drawer.code.setPlainText(bad)
        assert drawer._apply() is False
    assert path.read_text(encoding="utf-8") == SCRIPT


def test_unchecking_the_decal_row_hides_only_the_profile_decal(app, tmp_path):
    script = SCRIPT.replace("    vp_normal = 20\n", "    vp_normal = 20\n    ground_decal_id = 1\n")
    panel, _path = _panel(app, tmp_path, script)
    panel.decals = {1: object()}
    _rows(panel)[visuals.DECAL_INDEX].setCheckState(Qt.CheckState.Unchecked)
    assert visuals.DECAL_INDEX in panel.hidden_items
    assert panel.simulation.decals == {}
    assert panel.hidden_states == set()
    panel.show_all()
    assert panel.simulation.decals == panel.decals


def test_row_dots_follow_the_vanilla_compatibility_level(app, tmp_path):
    from entity_designer import fx_compat
    from studio.ui.shell import OPENNEOUA, RISKY, VANILLA

    def group(*lines):
        return fx.FxKeyGroup(list(lines))

    assert fx_compat.key_group_level(
        group("snd_normal_sample = a.wav", "snd_normal_volume = 60"), fx.CONTEXT_VEHICLE) == VANILLA
    assert fx_compat.key_group_level(
        group("shk_explode_slot = 2", "shk_explode_x = 0.2"), fx.CONTEXT_VEHICLE) == VANILLA
    assert fx_compat.key_group_level(
        group("snd_normal_sample = a.wav", "snd_normal_radius = 500"), fx.CONTEXT_VEHICLE) == RISKY
    assert fx_compat.key_group_level(
        group("snd_normal_sample = a.wav", "snd_normal_pitch = 0~500"), fx.CONTEXT_VEHICLE) == RISKY
    assert fx_compat.key_group_level(
        group("snd_airexplode_sample = a.wav"), fx.CONTEXT_VEHICLE) == OPENNEOUA
    assert fx_compat.key_group_level(
        group("shk_launch_player_slot = 1"), fx.CONTEXT_WEAPON) == OPENNEOUA
    assert fx_compat.model_level({"vp_normal": "20"}) == VANILLA
    assert fx_compat.model_level({"vp_normal": "20", "base_dead": "D.BASE"}) == RISKY
    assert fx_compat.model_level({"base_normal": "N.BASE"}) == OPENNEOUA
    assert fx_compat.decal_level({}) == OPENNEOUA
    assert fx_compat.decal_level({"ground_decal_size": "30~50"}) == OPENNEOUA
    assert fx_compat.decal_level({"ground_decal_id": "1"}) == OPENNEOUA
    assert fx_compat.dest_line_level(fx.DestFxLine("dest_fx = death_46_0_0_0")) == VANILLA
    assert fx_compat.dest_line_level(fx.DestFxLine("ext_dest_fx = death_46_0_0_0")) == OPENNEOUA


def test_the_model_row_cannot_be_deleted(app, tmp_path):
    panel, _path = _panel(app, tmp_path)
    _rows(panel)[visuals.MODEL_INDEX].setSelected(True)
    assert panel.effects_view.selected_indexes() == []
    assert not panel.effects_view.delete_action.isEnabled()


def test_delete_removes_crater_and_decal_from_all_definition_parts(app, tmp_path, monkeypatch):
    base = SCRIPT.replace("    vp_normal = 20\n", "    vp_normal = 20\n"
                          "    crater_radius = 50\n    crater_depth = 20\n"
                          "    ground_decal_id = 1\n")
    override = ("modify_weapon 6\n    crater_radius = 60\n    crater_rim = 15\n"
                "    ground_decal_size = 30~50\n    energy = 5\nend\n")
    panel, path = _panel(app, tmp_path, base)
    extra = tmp_path / "Overrides.cfg"
    extra.write_text(override, encoding="utf-8")
    panel.set_script_sources([(path, 0, find_script_blocks(base)[0].end_line),
                              (extra, 0, find_script_blocks(override)[0].end_line)])
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    for index in (visuals.CRATER_INDEX, visuals.DECAL_INDEX):
        _rows(panel)[index].setSelected(True)
    assert panel.effects_view.delete_button.isEnabled()
    panel.effects_view.delete_button.click()
    assert path.read_text(encoding="utf-8") == SCRIPT
    assert extra.read_text(encoding="utf-8") == "modify_weapon 6\n    energy = 5\nend\n"
    assert set(_rows(panel)) == {visuals.MODEL_INDEX, 0}
    assert panel.text_session.can_add()
    assert panel.delete_effects([0])
    assert "begin_fx" not in path.read_text(encoding="utf-8")


def test_delete_cluster_crater_of_self_spawning_weapon(app, tmp_path, monkeypatch):
    from entity_designer.fx_panel import CHILD_CRATER_INDEX

    script = ("new_weapon 6\n    vp_normal = 20\n    crater_radius = 30\n"
              "    crater_depth = 10\n    cluster_count = 2\n"
              "    cluster_weapon_id = 6\n    cluster_trigger_time = 100\nend\n")
    panel, path = _panel(app, tmp_path, script)
    weapon = fx.weapon_def_from_text(6, script)
    panel.set_context(fx.CONTEXT_WEAPON, weapon.document, unit=weapon.unit, weapons={6: weapon})
    panel.set_script_sources([(path, 0, find_script_blocks(script)[0].end_line)])
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    _rows(panel)[CHILD_CRATER_INDEX].setSelected(True)
    assert panel.effects_view.delete_button.isEnabled()
    panel.effects_view.delete_button.click()
    assert "crater_" not in path.read_text(encoding="utf-8")
    assert CHILD_CRATER_INDEX not in _rows(panel)
    assert visuals.CRATER_INDEX not in _rows(panel)


def test_preview_items_map_to_their_model_state():
    from entity_designer.editor import _fx_item_state
    from entity_designer.destruction_fx import IDENTITY, PreviewItem

    def item(kind, label=""):
        return PreviewItem(-1, kind, 1, (0, 0, 0), IDENTITY, (1, 1, 1), label=label)

    assert _fx_item_state(item("unit", "dead · VP 3")) == "dead"
    assert _fx_item_state(item("unit", "attached · VP 3")) == "normal"
    assert _fx_item_state(item("plasma")) == "genesis"
    assert _fx_item_state(item("tracer")) == "normal"
    assert _fx_item_state(item("visual")) is None
