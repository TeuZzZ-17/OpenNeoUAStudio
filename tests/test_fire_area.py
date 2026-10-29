"""Fire Area (ai_fire_trigger_*): script reading/writing, engine rules and geometry."""

import math

from entity_designer import fire_area as fa
from entity_designer.script_model import (
    VEHICLE,
    WEAPON,
    CollisionProject,
    apply_editable_overwrite_preview,
    build_editable_overwrite_preview,
    find_script_blocks,
    import_fire_area_block,
    plan_script_update,
    validate_project,
)

WEAPON_SCRIPT = (
    "new_weapon 7\n"
    "    name = Bomb\n"
    "    model = bomb\n"
    "    ai_fire_trigger_cone_below_xyz = 30_20_1200\n"
    "    ai_fire_trigger_min_depth = 150\n"
    "    begin_fx\n"
    "        offset_xyz = 9_8_7\n"
    "    end\n"
    "end\n"
)


def _read(text):
    return import_fire_area_block(text, find_script_blocks(text)[0])


def test_import_reads_values_like_the_engine():
    area = _read(WEAPON_SCRIPT)
    assert area.cone_below == (30.0, 20.0, 1200.0)
    assert area.min_depth == 150.0
    assert area.active_shape() == fa.CONE_BELOW


def test_invalid_and_zero_values_stay_absent():
    for raw in ("0", "1_2", "a_b_c", "-1_2_3", "0_2_3", "nan_1_1"):
        text = f"new_weapon 1\nai_fire_trigger_box_forward_xyz = {raw}\nend\n"
        assert _read(text).box_forward is None
    # A cone needs angles below 180.
    text = "new_weapon 1\nai_fire_trigger_cone_forward_xyz = 180_20_100\nend\n"
    assert _read(text).cone_forward is None
    text = "new_weapon 1\nai_fire_trigger_forward_max_inclination = 90\nend\n"
    assert _read(text).max_inclination == 0.0


def test_several_volumes_have_no_active_shape():
    text = ("new_weapon 1\nai_fire_trigger_box_below_xyz = 1_2_3\n"
            "ai_fire_trigger_box_forward_xyz = 1_2_3\nend\n")
    area = _read(text)
    assert area.active_shape() is None and len(area.authored_shapes()) == 2
    project = CollisionProject(name="W", source_model="m", target_category=WEAPON,
                               fire_area=area)
    errors, warnings = validate_project(project, None)
    assert not errors and any("più volumi" in warning for warning in warnings)


def test_with_only_keeps_one_volume_and_uses_the_guide_default():
    area = fa.FireArea(box_below=(1, 2, 3), min_depth=5.0)
    forward = area.with_only(fa.BOX_FORWARD)
    assert forward.authored_shapes() == (fa.BOX_FORWARD,)
    assert forward.box_forward == fa.DEFAULT_SIZES[fa.BOX_FORWARD]
    assert forward.min_depth == 5.0
    assert area.with_only(fa.BOX_BELOW).box_below == (1.0, 2.0, 3.0)


def test_writers_replace_only_the_fire_area_and_keep_nested_blocks():
    project = CollisionProject(
        name="Bomb", source_model="m", target_category=WEAPON,
        fire_area=fa.FireArea(box_below=(200, 1200, 400), min_depth=100,
                              max_inclination=0))
    for canonical in (False, True):
        text, _, _ = plan_script_update(
            WEAPON_SCRIPT, "new_weapon", 7, project, replace_all_managed=canonical)
        assert "ai_fire_trigger_box_below_xyz = 200_1200_400" in text
        assert "ai_fire_trigger_cone_below_xyz" not in text
        assert text.count("ai_fire_trigger_min_depth = 100") == 1
        assert "offset_xyz = 9_8_7" in text
        assert _read(text) == project.fire_area


def test_editable_overwrite_round_trip_and_vehicle_rejection():
    project = CollisionProject(
        name="Bomb", source_model="m", target_category=WEAPON,
        fire_area=fa.FireArea(cone_forward=(40, 20, 900), max_inclination=25))
    patch = build_editable_overwrite_preview("new_weapon", 7, project, {"fire_area"})
    assert "; [Tab: Fire Area]" in patch
    text, groups = apply_editable_overwrite_preview(WEAPON_SCRIPT, patch, "new_weapon", 7)
    assert groups == {"fire_area"}
    assert _read(text).cone_forward == (40.0, 20.0, 900.0)
    assert _read(text).cone_below is None
    # A vehicle has no Fire Area tab.
    vehicle = CollisionProject(name="V", source_model="m", target_category=VEHICLE)
    assert build_editable_overwrite_preview("new_vehicle", 1, vehicle, {"fire_area"}) == ""


def test_snapshot_restore_keeps_the_area():
    project = CollisionProject(target_category=WEAPON, fire_area=fa.FireArea(box_below=(1, 2, 3)))
    state = project.snapshot()
    project.fire_area = fa.FireArea()
    project.restore(state)
    assert project.fire_area.box_below == (1.0, 2.0, 3.0)


def test_box_has_twelve_edges_and_the_engine_corners():
    segments = fa.volume_segments(fa.BOX_FORWARD, (100, 80, 1200), 150)
    assert len(segments) == 12
    points = {point for segment in segments for point in segment}
    assert (-50.0, -40.0, 150.0) in points and (50.0, 40.0, 1200.0) in points


def test_below_box_goes_down_and_is_long_along_z():
    points = {p for s in fa.volume_segments(fa.BOX_BELOW, (200, 1200, 400), 0) for p in s}
    assert (-100.0, 0.0, -200.0) in points and (100.0, 1200.0, 200.0) in points


def test_cone_rims_and_longitudinal_lines():
    segments = fa.volume_segments(fa.CONE_FORWARD, (90, 90, 1000), 200)
    # Far rim, near rim and four lengthwise lines.
    assert len(segments) == 32 + 32 + 4
    half = math.tan(math.radians(45.0)) * 1000.0
    far = [p for s in segments for p in s if abs(p[2] - 1000.0) < 1e-6]
    assert math.isclose(max(p[0] for p in far), half)
    assert fa.volume_segments(fa.CONE_FORWARD, (90, 90, 1000), 0).__len__() == 32 + 4


def test_tilt_turns_a_forward_volume_toward_the_ground():
    flat = fa.volume_segments(fa.BOX_FORWARD, (10, 10, 100), 0)
    tilted = fa.volume_segments(fa.BOX_FORWARD, (10, 10, 100), 0, tilt_deg=30)
    assert max(p[1] for s in flat for p in s) < max(p[1] for s in tilted for p in s)


def test_handles_drag_values():
    size, near = fa.dragged(fa.BOX_FORWARD, (100, 80, 1200), 0.0, "width", 10.0)
    assert size == (120.0, 80.0, 1200.0)
    size, near = fa.dragged(fa.BOX_BELOW, (200, 1200, 400), 0.0, "height", 25.0)
    assert size == (200.0, 1200.0, 450.0)
    size, near = fa.dragged(fa.BOX_BELOW, (200, 1200, 400), 0.0, "depth", -200.0)
    assert size == (200.0, 1000.0, 400.0)
    size, near = fa.dragged(fa.CONE_FORWARD, (30, 20, 1000), 0.0, "min_depth", 5000.0)
    assert near == 999.0
    size, _ = fa.dragged(fa.CONE_FORWARD, (30, 20, 1000), 0.0, "angle_x", 0.0)
    assert size[0] == 30.0
    handles = {h.key: h for h in fa.volume_handles(fa.CONE_FORWARD, (90, 90, 1000), 0.0)}
    assert math.isclose(handles["angle_x"].point[0], 1000.0)
    assert handles["depth"].point == (0.0, 0.0, 1000.0)


# -- window integration (no game data needed) -------------------------------------


def _weapon_window(area):
    import pytest
    from PySide6.QtWidgets import QApplication
    from entity_designer.editor import CollisionEditorWindow

    app = QApplication.instance() or QApplication([])
    window = CollisionEditorWindow()
    window.project = CollisionProject(
        name="Bomb", source_model="m", target_category=WEAPON, fire_area=area)
    window._capture_tab_reset_baseline()
    window._sync_all()
    return app, window, pytest


def _close(app, window):
    window._modified = False
    window.close()
    app.processEvents()


def test_tab_is_only_for_weapons_and_drag_nudge_undo_work():
    from entity_designer.script_model import _SCRIPT_TAB_FIRE_AREA

    app, window, _pytest = _weapon_window(fa.FireArea(box_forward=(100, 80, 1200)))
    try:
        tabs = window.properties_tabs
        assert tabs.isTabEnabled(window.fire_area_tab_index)
        tabs.setCurrentIndex(window.fire_area_tab_index)
        assert window._is_fire_area_tab_active()
        assert window.viewport._fire_area_visible
        # Entering the tab closes in on the unit (the volume is 1200 deep).
        assert window.viewport._scale >= 1.0 / 400.0
        assert window.transform_box.isHidden()
        assert window._changed_script_tabs() == set()

        # Grip drag: computed from the drag start, one undo step for the whole drag.
        window._fire_area_drag_started("depth")
        window._fire_area_dragged("depth", 100.0)
        window._fire_area_dragged("depth", 250.0)
        assert window.project.fire_area.box_forward == (100.0, 80.0, 1450.0)
        assert _SCRIPT_TAB_FIRE_AREA in window._changed_script_tabs()
        window.undo()
        assert window.project.fire_area.box_forward == (100.0, 80.0, 1200.0)
        assert window._changed_script_tabs() == set()

        # Gizmo nudge moves the selected grip along its own axis.
        window._fire_area_handle_picked("width")
        window._fire_area_nudge((1, 0, 0), 10.0)
        assert window.project.fire_area.box_forward == (120.0, 80.0, 1200.0)
        window._fire_area_nudge((0, 0, 1), 10.0)  # not the width axis: nothing
        assert window.project.fire_area.box_forward == (120.0, 80.0, 1200.0)

        # Choosing another volume keeps only that one.
        window._fire_area_shape_chosen(fa.CONE_FORWARD)
        assert window.project.fire_area.authored_shapes() == (fa.CONE_FORWARD,)
        window._fire_area_value_edited("min_depth", 200.0)
        assert window.project.fire_area.min_depth == 200.0
        window._fire_area_value_edited("size0", 44.0)
        assert window.project.fire_area.cone_forward[0] == 44.0

        # Reset Current Tab brings back the loaded state.
        assert window._current_tab_has_changes()
    finally:
        _close(app, window)


def test_vehicle_can_open_fire_area_but_cannot_edit_weapon_values():
    app, window, _pytest = _weapon_window(fa.FireArea(box_forward=(1, 2, 3)))
    try:
        window.project.target_category = VEHICLE
        window._sync_all()
        assert window.properties_tabs.isTabEnabled(window.fire_area_tab_index)
        assert not window.fire_area_panel.isEnabled()
        window.properties_tabs.setCurrentIndex(window.fire_area_tab_index)
        assert window.properties_tabs.currentIndex() == window.fire_area_tab_index
        assert window.transform_box.isHidden()
        assert window.viewport._fire_area_shape is None
    finally:
        _close(app, window)


def test_fire_area_sliders_and_double_click_deselect():
    from PySide6.QtCore import QPointF, Qt, QEvent
    from PySide6.QtGui import QMouseEvent

    app, window, _pytest = _weapon_window(fa.FireArea(cone_forward=(30, 20, 1200)))
    try:
        window.properties_tabs.setCurrentIndex(window.fire_area_tab_index)
        panel = window.fire_area_panel
        panel.size_sliders[0].setValue(425)
        assert window.project.fire_area.cone_forward[0] == 42.5
        panel.min_depth_slider.setValue(150)
        assert window.project.fire_area.min_depth == 150
        panel.inclination_slider.setValue(255)
        assert window.project.fire_area.max_inclination == 25.5
        panel.size_spins[2].setValue(7500)
        assert window.project.fire_area.cone_forward[2] == 7500
        assert panel.size_sliders[2].value() == 75000
        window._fire_area_handle_picked("min_depth")
        window.viewport._hit_fire_area_handle = lambda _point: "min_depth"
        event = QMouseEvent(QEvent.Type.MouseButtonDblClick, QPointF(5, 5), QPointF(5, 5),
                            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                            Qt.KeyboardModifier.NoModifier)
        window.viewport.mouseDoubleClickEvent(event)
        assert window._fire_area_handle == "min_depth"
        window.viewport._hit_fire_area_handle = lambda _point: ""
        window.viewport.mouseDoubleClickEvent(event)
        assert window._fire_area_handle == ""
        assert window.viewport._fire_area_handle == ""
    finally:
        _close(app, window)


# -- firing unit, shot and fire_x range ----------------------------------------------


def test_x_span_widens_box_and_cone():
    span = fa.x_span_of(-30.0)
    assert span == (-30.0, 30.0)
    points = {p for s in fa.volume_segments(fa.BOX_FORWARD, (100, 80, 1200), 0, x_span=span)
              for p in s}
    assert (-80.0, -40.0, 0.0) in points and (80.0, 40.0, 1200.0) in points
    cone = fa.volume_segments(fa.CONE_FORWARD, (90, 90, 1000), 0, x_span=span)
    # The lengthwise lines plus the line over the muzzle range at the near end.
    assert len(cone) == 32 + 4 + 1
    assert fa.volume_handles(fa.BOX_FORWARD, (100, 80, 1200), 0, x_span=span)[0].point[0] == 80.0


def test_shot_path_drops_bombs_and_fires_ahead_within_the_tilt_limit():
    launch, target = fa.shot_path(fa.CONE_BELOW, (15, 15, 1800), (0, -500, 10))
    assert launch == (0.0, -500.0, 10.0) and target == (0.0, 0.0, 10.0)
    launch, target = fa.shot_path(fa.BOX_FORWARD, (100, 80, 1000), (0, -80, 0))
    assert target == (0.0, 0.0, 1000.0)
    # From 500 up with a 25 degree limit the ground is reached no closer than 500 / tan(25).
    _launch, target = fa.shot_path(fa.BOX_FORWARD, (100, 80, 2000), (0, -500, 0), 0.0, 25.0)
    assert target[2] == 2000.0
    _launch, target = fa.shot_path(fa.BOX_FORWARD, (100, 80, 300), (0, -500, 0), 0.0, 25.0)
    assert math.isclose(target[2], 500.0 / math.tan(math.radians(25.0)))


def test_firing_units_come_from_the_catalog_and_the_script():
    from types import SimpleNamespace

    from entity_designer.fire_area_units import find_firing_units

    catalog = [
        SimpleNamespace(kind="vehicle", object_id=5, values={
            "name": "Hawk", "weapon": "38", "vp_normal": "163", "fire_x": "-12",
            "fire_y": "45", "fire_z": "-10", "model": "plane", "overeof": "25"}),
        SimpleNamespace(kind="vehicle", object_id=6, values={
            "name": "Other", "weapon": "1", "vp_normal": "9"}),
        SimpleNamespace(kind="vehicle", object_id=7, values={
            "name": "Two guns", "weapon": "1", "weapon_2": "38", "vp_normal": "9"}),
        SimpleNamespace(kind="weapon", object_id=38, values={"weapon": "38", "vp_normal": "1"}),
    ]
    script = "new_vehicle 5\n name = Script Hawk\n weapon = 38\n vp_normal = 170\nend\n"
    units = find_firing_units(38, catalog, script)
    # Sorted by name; the script vehicle replaces the catalog one with the same id.
    assert [(u.vehicle_id, u.name, u.vp) for u in units] == [
        (5, "Script Hawk", 170), (7, "Two guns", 9)]
    assert units[0].flies is False
    catalog_only = next(u for u in find_firing_units(38, catalog) if u.vehicle_id == 5)
    assert catalog_only.fire == (-12.0, 45.0, -10.0) and catalog_only.flies


def test_simulation_fires_the_weapon_from_the_muzzle():
    from entity_designer import destruction_fx as fx

    unit = fx.UnitPhases(weapon=True, normal=fx.VisualRef(vp=5))
    shot = ((0.0, -80.0, 18.0), (0.0, 0.0, 1018.0))
    simulation = fx.PreviewSimulation(
        fx.FxDocument(), fx.CONTEXT_WEAPON, fx.WEAPON_HIT_GROUND, unit=unit, shot=shot)
    start = next(i for i in simulation.items_at(0.0) if i.kind == "unit")
    assert start.position == shot[0]
    last = simulation.duration - 1.0
    near_end = [i for i in simulation.items_at(last) if i.kind == "unit"]
    assert near_end and near_end[0].position[2] > 900.0
    # Without a shot the weapon still falls straight onto its target.
    fall = fx.PreviewSimulation(fx.FxDocument(), fx.CONTEXT_WEAPON, fx.WEAPON_HIT_GROUND, unit=unit)
    assert next(i for i in fall.items_at(0.0) if i.kind == "unit").position[2] == 0.0


def test_the_shot_only_runs_while_the_tab_is_open():
    from entity_designer.fire_area_units import FiringUnit

    app, window, _pytest = _weapon_window(fa.FireArea(box_forward=(100, 80, 1200)))
    try:
        window._fire_area_units = [FiringUnit(
            5, "Plane", 30, (0.0, -5.0, 18.0), "plane", 10.0, (1, 1, 1), (0, 0, 0))]
        window._fire_area_unit_id = 5
        window._fire_area_reset_height()
        assert window.fx_panel.shot is None
        window.properties_tabs.setCurrentIndex(window.fire_area_tab_index)
        launch, target = window.fx_panel.shot
        assert launch == (0.0, -1015.0, 18.0) and target == (0.0, 0.0, 1218.0)
        assert window.viewport._fire_area_muzzle == launch
        assert window._fire_area_unit_items()[0].position == (0.0, -1010.0, 0.0)
        assert window.fire_area_panel.height_slider.maximum() == 5000
        assert window.fire_area_panel.height_value.value() == 1000
        window.fire_area_panel.height_value.setValue(650)
        assert window.fire_area_panel.height_slider.value() == 650
        assert window._fire_area_height == 650.0
        floor = window.viewport._ground_grid_spec()
        window.viewport._scale *= 2.0
        assert window.viewport._ground_grid_spec() == floor
        window.project.model_scale_x = 2.0
        assert window._fire_area_unit_items()[0].scale == (2.0, 1.0, 1.0)
        # Choosing "None" in the preview drops the unit and starts the shot at the weapon.
        window._fire_area_unit_chosen(None)
        assert window.fx_panel.shot[0] == (0.0, -1000.0, 0.0)
        assert window._fire_area_unit_items() == []
        window.properties_tabs.setCurrentIndex(window.collision_tab_index)
        assert window.fx_panel.shot is None
    finally:
        _close(app, window)


def test_ground_units_stand_on_the_floor_and_flyers_keep_the_default_height():
    from entity_designer.fire_area_units import FiringUnit

    app, window, _pytest = _weapon_window(fa.FireArea(box_forward=(100, 80, 1200)))
    try:
        tank = FiringUnit(5, "Tank", 30, (0.0, -5.0, 18.0), "tank", 10.0, (1, 1, 1), (0, 0, 0))
        plane = FiringUnit(6, "Plane", 31, (0.0, -5.0, 18.0), "plane", 10.0, (1, 1, 1), (0, 0, 0))
        window._fire_area_units = [tank, plane]
        window.properties_tabs.setCurrentIndex(window.fire_area_tab_index)
        window._fire_area_unit_chosen(5)
        assert window._fire_area_height == 0.0
        # Rests on its own ground offset (overeof), not in the air.
        assert window._fire_area_unit_items()[0].position == (0.0, -10.0, 0.0)
        window._fire_area_unit_chosen(6)
        assert window._fire_area_height == 1000.0
        window._fire_area_unit_chosen(None)
        assert window._fire_area_height == 1000.0
    finally:
        _close(app, window)


def test_edits_keep_the_camera_and_a_new_definition_focuses_the_volume():
    app, window, _pytest = _weapon_window(fa.FireArea(cone_forward=(30, 20, 1200)))
    try:
        window.properties_tabs.setCurrentIndex(window.fire_area_tab_index)
        viewport = window.viewport
        # Entering the tab frames the unit.
        assert viewport._scale >= 1.0 / 400.0
        moved = {"center": (11.0, 22.0, 33.0), "scale": 0.0123, "zoom": 2.0}
        viewport._center, viewport._scale, viewport._zoom = (
            moved["center"], moved["scale"], moved["zoom"])
        edits = (
            lambda: window._fire_area_value_edited("size0", 44.0),
            lambda: window._fire_area_value_edited("min_depth", 200.0),
            lambda: window._fire_area_value_edited("height", 300.0),
            lambda: window._fire_area_shape_chosen(fa.BOX_FORWARD),
            lambda: window._fire_area_unit_chosen(None),
            lambda: (window._fire_area_drag_started("depth"),
                     window._fire_area_dragged("depth", 50.0)),
        )
        for edit in edits:
            edit()
            app.processEvents()
            assert (viewport._center, viewport._scale, viewport._zoom) == (
                moved["center"], moved["scale"], moved["zoom"])
        # Opening another weapon asks for a new framing on the volume.
        window._fire_area_load_defaults("", type("Block", (), {
            "start_line": 0, "end_line": 0, "object_id": 1, "kind": "new_weapon"})())
        assert window._fire_area_frame_pending
    finally:
        _close(app, window)


def test_each_volume_keeps_its_edit_and_reset_goes_back_to_the_script():
    app, window, _pytest = _weapon_window(fa.FireArea(cone_below=(30, 20, 1200)))
    try:
        window._fire_area_loaded = window.project.fire_area
        window.properties_tabs.setCurrentIndex(window.fire_area_tab_index)
        window._fire_area_value_edited("size0", 0.1)
        window._fire_area_value_edited("size1", 0.1)
        assert window.project.fire_area.cone_below == (0.1, 0.1, 1200.0)
        window._fire_area_shape_chosen(fa.BOX_FORWARD)
        assert window.project.fire_area.authored_shapes() == (fa.BOX_FORWARD,)
        window._fire_area_value_edited("size2", 777.0)
        window._fire_area_shape_chosen(fa.CONE_BELOW)
        assert window.project.fire_area.cone_below == (0.1, 0.1, 1200.0)
        window._fire_area_shape_chosen(fa.BOX_FORWARD)
        assert window.project.fire_area.box_forward[2] == 777.0
        # A volume never edited here comes from the script, else the guide default.
        window._fire_area_shape_chosen(fa.CONE_BELOW)
        window._fire_area_shape_chosen(fa.BOX_BELOW)
        assert window.project.fire_area.box_below == fa.DEFAULT_SIZES[fa.BOX_BELOW]
        # Reset: the shown volume and the remembered ones return to the script sizes.
        window._fire_area_shape_chosen(fa.CONE_BELOW)
        window._fire_area_reset_all()
        assert window.project.fire_area.cone_below == (30.0, 20.0, 1200.0)
        window._fire_area_shape_chosen(fa.BOX_FORWARD)
        assert window.project.fire_area.box_forward == fa.DEFAULT_SIZES[fa.BOX_FORWARD]
        window.undo()
        assert window.project.fire_area.cone_below is not None
    finally:
        _close(app, window)


def test_the_unit_animation_follows_play_and_pause_of_the_shot():
    app, window, _pytest = _weapon_window(fa.FireArea(box_forward=(100, 80, 1200)))
    try:
        window.properties_tabs.setCurrentIndex(window.fire_area_tab_index)
        assert not window.play_button.isChecked()
        window.fx_panel.play_button.setChecked(True)
        assert window.play_button.isChecked()
        window.fx_panel.play_button.setChecked(False)
        assert not window.play_button.isChecked()
        # Leaving the tab stops following the shot.
        window.properties_tabs.setCurrentIndex(window.collision_tab_index)
        window.fx_panel.play_button.setChecked(True)
        assert not window.play_button.isChecked()
        window.fx_panel.play_button.setChecked(False)
    finally:
        _close(app, window)


def _unit(**values):
    from entity_designer.fire_area_units import unit_from_values

    base = {"name": "U", "weapon": "9", "vp_normal": "5", "fire_x": "-30"}
    base.update({key: str(value) for key, value in values.items()})
    return unit_from_values(1, base, 9)


def test_muzzle_positions_follow_fire_x_and_its_modes_like_the_engine():
    # Vanilla: one projectile at fire_x, several spread evenly over +-|fire_x|.
    assert _unit().muzzle_xs() == [-30.0] and _unit().x_span == (-30.0, 30.0)
    assert _unit(num_weapons=3).muzzle_xs() == [-30.0, 0.0, 30.0]
    # num_weapons accepts a min_max range (the preview uses the minimum).
    assert len(_unit(num_weapons="2_4").muzzle_xs()) == 2
    # Salve modes: the whole salvo on one side, or half and half with the odd one centred.
    assert _unit(fire_x_mode="salve_sequence", num_weapons=2).muzzle_xs() == [-30.0, -30.0]
    assert _unit(fire_x_mode="salve_mirror", num_weapons=5).muzzle_xs() == [
        -30.0, -30.0, 30.0, 30.0, 0.0]
    # Simple sequence alternates, starting on the opposite side of fire_x.
    assert _unit(fire_x_mode="sequence", num_weapons=2).muzzle_xs() == [30.0, -30.0]
    # Advanced rack: start + slot * step, and the span is the rack itself.
    rack = _unit(fire_x_mode="sequence", fire_x_start=-20, fire_x_step=10,
                 fire_x_slots=3, num_weapons=4)
    assert rack.muzzle_xs() == [-20.0, -10.0, 0.0, -20.0] and rack.x_span == (-20.0, 0.0)
    # An invalid rack falls back to the simple mode, like the engine.
    bad = _unit(fire_x_mode="random", fire_x_start=0, fire_x_step=5, fire_x_slots=0)
    assert bad.rack is None and bad.x_span == (-30.0, 30.0)
    # The second weapon slot reads num_weapons_2.
    second = _unit(weapon=1, weapon_2=9, num_weapons=1, num_weapons_2=3)
    assert len(second.muzzle_xs()) == 3


def test_a_volley_flies_every_projectile_of_the_salvo():
    from entity_designer import destruction_fx as fx

    unit = fx.UnitPhases(weapon=True, normal=fx.VisualRef(vp=5))
    shot = ((-30.0, -80.0, 18.0), (-30.0, 0.0, 1018.0))
    volley = (((30.0, -80.0, 18.0), (30.0, 0.0, 1018.0)),)
    simulation = fx.PreviewSimulation(
        fx.FxDocument(), fx.CONTEXT_WEAPON, fx.WEAPON_HIT_GROUND, unit=unit,
        shot=shot, volley=volley)
    starts = {i.position[0] for i in simulation.items_at(0.0) if i.kind == "unit"}
    assert starts == {-30.0, 30.0}


def test_forward_shot_can_go_to_the_low_or_the_high_zone():
    app, window, _pytest = _weapon_window(
        fa.FireArea(box_forward=(100, 80, 1000), max_inclination=25.0))
    try:
        window.properties_tabs.setCurrentIndex(window.fire_area_tab_index)
        launch, low = window._fire_area_shots()[0]
        assert low[1] == 0.0 and window.fire_area_panel.aim_combo.isEnabled()
        window.fire_area_panel.aim_combo.setCurrentIndex(1)
        assert window._fire_area_aim == fa.AIM_HIGH
        launch, high = window._fire_area_shots()[0]
        assert high == (launch[0], launch[1], launch[2] + 1000.0)
        assert window.fx_panel.shot == (launch, high)
        # Below volumes always fall straight down: the choice is disabled.
        window._fire_area_shape_chosen(fa.CONE_BELOW)
        assert not window.fire_area_panel.aim_combo.isEnabled()
        assert window._fire_area_shots()[0][1][1] == 0.0
    finally:
        _close(app, window)


def test_reset_brings_back_every_value_of_the_tab():
    area = fa.FireArea(cone_forward=(30, 20, 1200), min_depth=100.0, max_inclination=20.0)
    app, window, _pytest = _weapon_window(area)
    try:
        window._fire_area_loaded = area
        window.properties_tabs.setCurrentIndex(window.fire_area_tab_index)
        window._fire_area_value_edited("size0", 5.0)
        window._fire_area_value_edited("min_depth", 350.0)
        window._fire_area_value_edited("max_inclination", 42.3)
        window._fire_area_value_edited("height", 1260.0)
        window._fire_area_aim_chosen(fa.AIM_HIGH)
        window._fire_area_shape_chosen(fa.BOX_FORWARD)
        window._fire_area_reset_all()
        assert window.project.fire_area == area
        assert window._fire_area_aim == fa.AIM_LOW and window._fire_area_height == 1000.0
        window.undo()
        assert window.project.fire_area.box_forward is not None
        # A script without a volume: back to no volume at all, like when it was opened.
        window._fire_area_loaded = fa.FireArea()
        window._fire_area_reset_all()
        assert window.project.fire_area == fa.FireArea()
        assert window.fire_area_panel.reset_button.isEnabled()
    finally:
        _close(app, window)
