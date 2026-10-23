"""Weapon tools share the vehicle paths, except vehicle-only briefing settings."""
from pathlib import Path
from dataclasses import replace
import pytest
from PySide6.QtWidgets import QApplication, QMessageBox
from entity_designer.editor import CollisionEditorWindow
from entity_designer.script_model import WEAPON, VEHICLE, find_script_blocks
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
    folder = data / 'Models' / 'Collision'
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
    assert window.project.collision_shape_path == 'Data/Models/Collision/Rocket.collision'
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
