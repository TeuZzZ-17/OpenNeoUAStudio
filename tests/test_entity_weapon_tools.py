"""Weapon tools share the vehicle paths, except vehicle-only briefing settings."""
from pathlib import Path
from dataclasses import replace
import pytest
from PySide6.QtWidgets import QApplication, QMessageBox
from entity_designer.editor import CollisionEditorWindow
from entity_designer.script_model import WEAPON, VEHICLE, find_script_blocks, script_model_references
from entity_designer.shape import CollisionHull, CollisionShape, read_collision_shape, write_collision_shape
from entity_designer.wireframe_generator import WireframeProjection


@pytest.fixture
def window():
    app = QApplication.instance() or QApplication([])
    win = CollisionEditorWindow()
    yield win
    win._modified = False
    win.close()
    app.processEvents()


def test_weapon_briefing_is_disabled_and_recovers_for_vehicles(window):
    window.project.target_category = WEAPON
    window._sync_all()
    assert not window.properties_tabs.isTabEnabled(window.briefing_tab_index)
    assert window.properties_tabs.isTabEnabled(window.collision_shape_tab_index)
    assert window.properties_tabs.isTabEnabled(window.wireframe_tab_index)
    window.project.target_category = VEHICLE
    window._sync_all()
    assert window.properties_tabs.isTabEnabled(window.briefing_tab_index)


def test_kamikaze_preview_uses_carrier_visuals_and_sources(window, tmp_path):
    from entity_designer import destruction_fx as fx
    from studio.assets.catalog import GameObject

    weapon = "new_weapon 17\n    model = kamikaze\n    vp_normal = 22\nend\n"
    source = tmp_path / "Weapons.cfg"
    source.write_text(weapon, encoding="utf-8")
    body = ("    model = plane\n    weapon_2 = 17\n    vp_normal = 51\n"
            "    scale_xyz = 2|3|4\n    overeof = 20\n"
            "    begin_fx\n        trigger = air_destroyed\n        mode = visual\n"
            "        start_size = 1\n        end_size = 0\n"
            "        duration = 1000\n        vp_model = 160\n    end")
    vehicles = tmp_path / "Vehicles.cfg"
    carrier = GameObject("vehicle", 91, vehicles,
                         {"name": "Yang", "weapon_2": "17"}, [body])
    text = carrier.effective_text
    vehicles.write_text(text, encoding="utf-8")
    carrier.body_sources = [(vehicles, 0, find_script_blocks(text)[0].end_line)]
    reference = script_model_references(weapon)[0]
    window._active_script_path = source
    window._load_fx_context(weapon, reference.block, reference, objects=[carrier])

    panel = window.fx_panel
    assert panel.carrier_combo.currentData() is None
    assert panel.context == fx.CONTEXT_WEAPON
    assert not panel.simulation.shows_unit
    panel.carrier_combo.blockSignals(True)
    panel.carrier_combo.setCurrentIndex(panel.carrier_combo.findData(91))
    panel.carrier_combo.blockSignals(False)
    window._load_fx_context(weapon, reference.block, reference, keep_view=True, objects=[carrier])
    assert panel.carrier_combo.currentData() == 91
    assert panel.context == fx.CONTEXT_VEHICLE
    assert panel.weapon_box.isHidden()
    assert panel.text_session.sources[0].path == vehicles
    assert panel.unit.scale == (2, 3, 4)
    assert panel.origin == (0, -20, 0)
    assert {item.vp for item in panel.simulation.items_at(0) if item.kind == "unit"} == {51}
    assert 160 in panel.required_vps()
    assert any(item.vp == 160 for item in panel.simulation.items_at(200))

    panel.carrier_combo.blockSignals(True)
    panel.carrier_combo.setCurrentIndex(panel.carrier_combo.findData(None))
    panel.carrier_combo.blockSignals(False)
    window._load_fx_context(weapon, reference.block, reference, keep_view=True, objects=[carrier])
    assert panel.context == fx.CONTEXT_WEAPON
    assert panel.text_session.origins[0][0] == source
    assert not panel.simulation.shows_unit


def test_weapon_wireframe_stays_selected_and_loads_its_current_file(window, tmp_path):
    data = tmp_path / 'Data'
    (data / 'Wireless').mkdir(parents=True)
    (data / 'Scripts').mkdir()
    wire = data / 'Wireless' / 'Rocket.SKL'
    WireframeProjection([(0, 0, 0), (100, 0, 100)], [[0, 1]]).save(wire)
    source = data / 'Scripts' / 'Weapons.cfg'
    text = 'new_weapon 5\n name = Rocket\n wireframe = wireless/rocket.sklt\nend\n'
    source.write_text(text, encoding='utf-8')
    window.project.target_category = WEAPON
    window._active_script_path = source
    window._sync_wireframe_current(text, find_script_blocks(text)[0])
    window.properties_tabs.setCurrentIndex(window.wireframe_tab_index)
    window._sync_all()
    assert window.properties_tabs.currentIndex() == window.wireframe_tab_index
    assert window.wireframe_panel.current_path == wire.resolve()
    assert window.wireframe_panel.current_button.isChecked()


def test_weapon_shape_import_export_use_the_shared_controls(window, tmp_path, monkeypatch):
    import entity_designer.editor as editor
    data = tmp_path / 'Data'
    folder = data / 'Collisions'
    folder.mkdir(parents=True)
    source, target = folder / 'Rocket.collision', folder / 'RocketCopy.collision'
    hull = CollisionHull(((0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)),
                         ((0, 2, 1), (0, 1, 3), (0, 3, 2), (1, 2, 3)))
    shape = CollisionShape('Rocket', 'a' * 64, (1, 1, 1), (0, 0, 0), (hull,))
    write_collision_shape(source, shape)
    window.project.target_category = WEAPON
    window._sync_all()
    window.properties_tabs.setCurrentIndex(window.collision_shape_tab_index)
    assert not window.collision_shape_box.isHidden()
    assert not window.collision_geometry_widget.isHidden()
    monkeypatch.setattr(window, '_current_set_bas_path', lambda: data / 'Set1.bas')
    monkeypatch.setattr(editor.QFileDialog, 'getOpenFileName', lambda *a, **k: (str(source), ''))
    monkeypatch.setattr(editor.QFileDialog, 'getSaveFileName', lambda *a, **k: (str(target), ''))
    monkeypatch.setattr(QMessageBox, 'warning', lambda *a: pytest.fail(str(a[1:])))
    window.import_collision_shape()
    assert window.project.collision_shape == shape
    assert window.project.collision_shape_path == 'Data/Collisions/Rocket.collision'
    assert window.export_collision_shape_button.isEnabled()
    assert window.export_collision_shape_action.isEnabled()
    window.export_collision_shape()
    assert read_collision_shape(target) == shape
    changed = replace(shape, source='RocketChanged')
    window.project.collision_shape = changed
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.Yes)
    window.overwrite_collision_shape()
    assert read_collision_shape(target) == changed
    window.delete_collision_shape()
    assert window.project.collision_shape is None
    window.undo()
    assert window.project.collision_shape == changed
