"""Point-tab navigation and referenced turret definitions from the game catalog."""

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from entity_designer import CollisionEditorWindow, GunPoint
from entity_designer.script_model import import_turret_limits, plan_script_update


def test_entering_point_tabs_selects_first_even_after_a_previous_selection():
    app = QApplication.instance() or QApplication([])
    window = CollisionEditorWindow()
    try:
        window.project.fire_points_enabled = True
        window.project.num_weapons = 2
        window.project.gun_points_enabled = True
        window.project.gun_points = [GunPoint(), GunPoint(x=10)]
        window._sync_all()
        for tab, tree in ((window.fire_points_tab_index, window.fire_point_tree),
                          (window.gun_points_tab_index, window.gun_point_tree)):
            window.properties_tabs.setCurrentIndex(tab)
            assert tree.currentItem() == tree.topLevelItem(0)
            tree.setCurrentItem(tree.topLevelItem(1))
            window.properties_tabs.setCurrentIndex(window.collision_tab_index)
            window.properties_tabs.setCurrentIndex(tab)
            assert tree.currentItem() == tree.topLevelItem(0)
    finally:
        window._modified = False
        window.close()


def test_catalog_turret_is_editable_and_patches_the_actual_modify_block(tmp_path):
    app = QApplication.instance() or QApplication([])
    source = ("new_vehicle 56\n name = Carrier\nend\n"
              "new_vehicle 147\n model = gun\n name = Turret\n"
              " gun_side_angle = 3200\n gun_up_angle = 500\n gun_down_angle = 1300\nend\n"
              "modify_vehicle 147\n gun_up_angle = 1500\nend\n")
    path = tmp_path / "Vehicles.cfg"
    path.write_text(source)
    window = CollisionEditorWindow()
    try:
        window.game_object_browser.objects = [SimpleNamespace(
            kind="vehicle", object_id=147, source=path,
            effective_text="new_vehicle 147\n model = gun\n name = Turret\n"
                           " gun_side_angle = 3200\n gun_up_angle = 1500\n"
                           " gun_down_angle = 1300\nend\n")]
        window.project.gun_points_enabled = True
        window.project.gun_points = [GunPoint(gun_type=147)]
        window._capture_tab_reset_baseline()
        window.properties_tabs.setCurrentIndex(window.gun_points_tab_index)
        limits = window.project.turret_limits[147]
        assert (limits.side, limits.up, limits.down) == (3200, 1500, 1300)
        assert limits.source_kind == "modify_vehicle" and not limits.dirty
        assert all(control.isEnabled() for control in window.turret_limit_spins.values())
        window.turret_limit_spins["up"].setValue(1200)
        updated, _, _ = plan_script_update(source, "new_vehicle", 56, window.project)
        assert import_turret_limits(updated)[147].up == 1200
        assert "new_vehicle 147\n model = gun\n name = Turret\n gun_side_angle = 3200\n gun_up_angle = 500" in updated
        assert path.read_text() == source
    finally:
        window._modified = False
        window.close()
