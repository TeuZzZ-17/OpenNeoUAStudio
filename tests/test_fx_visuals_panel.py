"""The single Model row of the Visuals && FX effects list."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from entity_designer import destruction_fx as fx
from entity_designer import fx_visuals as visuals
from entity_designer.fx_panel import DestructionFxPanel
from entity_designer.script_model import find_script_blocks

SCRIPT = ("new_weapon 6\n    vp_normal = 20\n    base_dead = Data/Models/Base/D.BASE\n"
          "begin_fx\n    trigger = detonate\n    mode = visual\n    vp_model = 5\nend\nend\n")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _panel(app, tmp_path):
    path = tmp_path / "Weapons.cfg"
    path.write_text(SCRIPT, encoding="utf-8")
    block = find_script_blocks(SCRIPT)[0]
    panel = DestructionFxPanel()
    panel.set_context(fx.CONTEXT_WEAPON,
                      fx.extract_fx_document(SCRIPT, block.start_line, block.end_line))
    panel.set_script_sources([(path, block.start_line, block.end_line)])
    return panel, path


def _rows(panel):
    return {panel.effects_list.item(i).data(Qt.ItemDataRole.UserRole): panel.effects_list.item(i)
            for i in range(panel.effects_list.count())}


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
    assert panel.hidden_states == {"normal", "fire", "wait", "dead", "megadeth", "genesis", "launch"}
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
        "vp_normal = 20\nvp_fire = 33\n3ds_fire = Data/Models/3ds/f.3ds\nvp_launch = 7")
    drawer._apply()
    assert panel.has_unsaved_text and drawer.title_label.text().endswith("*")
    assert drawer._save()
    text = path.read_text(encoding="utf-8")
    assert "vp_fire = 33" in text and "3ds_fire = Data/Models/3ds/f.3ds" in text
    assert "vp_launch = 7" in text and "base_dead" not in text  # a removed line leaves the script
    assert saved == [True]


def test_a_wrong_model_text_is_refused_and_the_file_stays(app, tmp_path):
    panel, path = _panel(app, tmp_path)
    panel.edit_effect_code(visuals.MODEL_INDEX)
    drawer = panel.code_drawer
    for bad in ("vp_normal = abc", "energy = 5", "vp_normal"):
        drawer.code.setPlainText(bad)
        assert drawer._apply() is False
    assert path.read_text(encoding="utf-8") == SCRIPT


def test_the_model_row_cannot_be_deleted(app, tmp_path):
    panel, _path = _panel(app, tmp_path)
    _rows(panel)[visuals.MODEL_INDEX].setSelected(True)
    assert panel.effects_view.selected_indexes() == []
    assert not panel.effects_view.delete_action.isEnabled()


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
