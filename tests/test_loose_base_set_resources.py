"""A loose BASE unit takes its textures and its effect VPs from the active set."""

import os
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

DATA = Path(os.environ.get("NME_GAME_DATA", ""))
BOMB = DATA / "Models/Base/New/Mnosjetz_Bomb/Mnosjetz_Bomb_Normal_Wait/VP_MNOBOMB_NORMAL.BASE"

pytestmark = pytest.mark.skipif(
    not (BOMB.is_file() and (DATA / "Sets/Set1").is_dir()),
    reason="needs the game data with the Mnojet bomb BASE and Set 1")


@pytest.fixture()
def window():
    from map_editor import bootstrap
    from map_editor.core.game_installation import GameInstallation
    QApplication.instance() or QApplication([])
    bootstrap.set_installation(GameInstallation.suggest(DATA))
    from entity_designer.editor import CollisionEditorWindow
    window = CollisionEditorWindow()
    yield window
    window._set_modified(False)
    window.close()
    bootstrap.set_installation(None)


def _mnojet(window):
    from map_editor import bootstrap
    from studio.assets.catalog import load_game_objects
    # The Mnojet bomb is weapon 38 (its name in the script may change).
    obj = next(o for o in load_game_objects(bootstrap.installation())
               if o.kind == "weapon" and o.object_id == 38)
    assert window._open_game_object(obj)


def test_gatling_tracer_is_available_for_collision_generation(window):
    from unittest.mock import patch
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QMessageBox
    from map_editor import bootstrap
    from studio.assets.catalog import load_game_objects

    obj = next(o for o in load_game_objects(bootstrap.installation())
               if o.kind == "weapon" and o.object_id == 33)
    assert window._open_game_object(obj)
    window.properties_tabs.setCurrentIndex(window.collision_shape_tab_index)
    components = window.collision_geometry_widget.selected_components()
    assert components
    points = [p for component in components for triangle in component.triangles
              for p in triangle]
    extent = [max(p[i] for p in points) - min(p[i] for p in points)
              for i in range(3)]
    assert extent == pytest.approx((1, 1, 80))
    assert min(p[2] for p in points) == pytest.approx(-80)
    assert max(p[2] for p in points) == pytest.approx(0)
    assert window.generate_collision_shape_button.isEnabled()
    assert window.viewport._family_ref is window._collision_tracer_family
    assert window.viewport._mode == "solid"
    assert window.viewport._faces
    window.properties_tabs.setCurrentIndex(window.collision_tab_index)
    assert window.viewport._family_ref is window._collision_tracer_family
    window.properties_tabs.setCurrentIndex(window.fx_tab_index)
    assert window.viewport._family_ref is window.family
    window.properties_tabs.setCurrentIndex(window.fire_area_tab_index)
    assert window.viewport._family_ref is window.family
    window.properties_tabs.setCurrentIndex(window.collision_shape_tab_index)
    with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes), \
            patch.object(QMessageBox, "warning"):
        window.generate_collision_shape()
        for _ in range(300):
            if window._collision_bake_process is None:
                break
            QTest.qWait(100)
    assert window._collision_bake_process is None
    assert window.project.collision_shape is not None
    assert window.project.collision_shape.hulls
    window._set_modified(False)
    _mnojet(window)
    assert window._collision_tracer_family is None
    assert window.viewport._family_ref is window.family
    assert window.collision_geometry_widget.selected_components()


def test_ghargoil_one_fire_point_has_mirrored_preview(window):
    from map_editor import bootstrap
    from studio.assets.catalog import load_game_objects

    obj = next(o for o in load_game_objects(bootstrap.installation())
               if o.kind == "vehicle" and o.object_id == 24)
    assert window._open_game_object(obj)
    window.properties_tabs.setCurrentIndex(window.fire_points_tab_index)
    assert len(window.viewport._fire_points) == 1
    assert window.viewport._fire_point_mirrored
    assert window.fire_point_tree.topLevelItemCount() == 1
    start, end = window.viewport._forward_arrow_points()
    assert end[0:2] == start[0:2] and end[2] > start[2]


def test_quadda_vp_zero_is_drawn_in_zapper_fire_area(window):
    from map_editor import bootstrap
    from studio.assets.catalog import load_game_objects
    obj = next(o for o in load_game_objects(bootstrap.installation())
               if o.kind == "weapon" and o.object_id == 64)
    assert window._open_game_object(obj)
    window.properties_tabs.setCurrentIndex(window.fire_area_tab_index)
    assert window._fire_area_unit().vehicle_id == 64
    item = window._fire_area_unit_items()[0]
    assert item.vp == 0 and item.vp_zero_valid
    assert 0 in window._fire_area_extra_vps()
    viewport = window.viewport
    start, end = viewport._forward_arrow_points()
    assert end[0:2] == start[0:2] and end[2] > start[2]
    owner = viewport._fx_owner_by_vp.get(0)
    assert owner is not None
    assert viewport._fx_item_faces(item, viewport._fx_templates())


def test_external_3ds_normal_can_be_used_as_collision_geometry(window, tmp_path):
    from unittest.mock import patch
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QMessageBox
    script = tmp_path / "weapon.cfg"
    script.write_text(
        "new_weapon 987\n name = Test\n vp_normal = 22\n"
        " 3ds_normal = Data/Models/3ds/Tracer.3ds\nend\n", encoding="utf-8")
    assert window.open_vehicle_script(
        script, object_id=987, object_kind="new_weapon",
        set_bas_path=DATA / "Sets/Set1/Objects/Set.BAS")
    window.properties_tabs.setCurrentIndex(window.collision_shape_tab_index)
    assert window._active_base_path.suffix.casefold() == ".3ds"
    assert window.collision_geometry_widget.selected_components()
    assert window.generate_collision_shape_button.isEnabled()
    assert window.viewport._mode == "solid" and window.viewport._faces
    with patch.object(QMessageBox, "warning"):
        window.generate_collision_shape()
        for _ in range(300):
            if window._collision_bake_process is None:
                break
            QTest.qWait(100)
    assert window.project.collision_shape and window.project.collision_shape.hulls
    window._set_modified(False)


def test_loose_base_finds_its_textures_in_the_set(window):
    _mnojet(window)
    missing = [w for w in window.family.warnings if "not found" in w]
    assert not missing, missing
    assert window.family.textures


def test_effect_vps_of_the_set_are_drawn_around_a_loose_unit(window):
    _mnojet(window)
    assert window._vp_embedded is None and window._loose_unit_uses_set()
    window.properties_tabs.setCurrentIndex(window.fx_tab_index)
    window._sync_fx_preview()
    window.fx_panel.time_ms = 900.0
    window._push_fx_frame()
    QApplication.processEvents()
    window.viewport.grab()
    viewport = window.viewport
    assert all(viewport._fx_vp_source(vp) for vp in window.fx_panel.required_vps())
    # One shared renderer for all the set's VPs, not one per VP.
    shared = [key for key in viewport._fx_external_cache if key.startswith("set:")]
    assert len(shared) == 1 and viewport._fx_external_cache[shared[0]] is not None
    # Loading the set while painting must not look like an edited file.
    assert window._vp_embedded is None


def test_animation_controls_and_frames_of_a_loose_unit(window):
    _mnojet(window)
    window.properties_tabs.setCurrentIndex(window.fx_tab_index)
    window._sync_fx_preview()
    window._sync_animation_controls()
    # The unit's exhaust is animated: Play / Speed must not stay greyed out.
    assert window.play_button.isEnabled() and window.speed_spin.isEnabled()
    viewport = window.viewport
    window._push_fx_frame()
    viewport.grab()
    unit_helper = next(h for key, h in viewport._fx_external_cache.items()
                       if key.endswith(".BASE") and h is not None)
    animated = [i for i, m in enumerate(viewport._materials) if m.anim_frames]
    assert animated
    # The helper shows the frame the toolbar animation is on.
    viewport._anim_states[animated[0]] = (2, 1)
    viewport.grab()
    assert unit_helper._anim_states[animated[0]] == (2, 1)


def test_firing_unit_in_fire_area_follows_the_toolbar_play(window):
    """The Mnosjetz propellers must turn with the toolbar Play, not only with the shot."""

    _mnojet(window)
    window.properties_tabs.setCurrentIndex(window.fire_area_tab_index)
    window._sync_animation_controls()
    viewport = window.viewport
    unit = next(item for item in viewport._fx_items if item.kind == "unit" and item.vp == 163)
    window.viewport._anim_clock_ms = 0.0
    seen = set()
    for clock in (0.0, 70.0, 130.0, 200.0, 330.0, 470.0):
        # Only the toolbar clock moves: the shot timeline stays at 0.
        viewport._anim_clock_ms = clock
        viewport.grab()
        helper = next(h for key, h in viewport._fx_external_cache.items()
                      if key.startswith("set:") and h is not None)
        materials = {face.material for face, _vertices in viewport._fx_helper_faces(helper, unit)
                     if helper._materials[face.material].anim_frames}
        assert materials
        seen.add(tuple(sorted(helper._anim_states[m] for m in materials)))
    assert len(seen) > 1


def test_fire_area_renders_firing_unit_with_gpu_when_available(window):
    from PySide6.QtTest import QTest

    _mnojet(window)
    window.properties_tabs.setCurrentIndex(window.fire_area_tab_index)
    window.show()
    window._push_fx_frame()
    QTest.qWait(500)
    viewport = window.viewport
    if viewport.renderer_mode != "gpu" or not viewport._gpu_canvas.isValid():
        pytest.skip("A working OpenGL canvas is required")
    viewport._gpu_canvas.grabFramebuffer()
    helper = next(h for key, h in viewport._fx_external_cache.items()
                  if key.startswith("set:") and h is not None)
    assert helper._last_effective_renderer == "retail_indexed_gpu"
    assert viewport.renderer_name.startswith("OpenGL")
    viewport._gpu_indexed_unavailable = True
    viewport._gpu_canvas.grabFramebuffer()
    assert helper._last_effective_renderer == "retail_indexed_reconstructed"
    assert viewport.renderer_name.startswith("Software")
    assert viewport.set_renderer_mode("software")
    viewport.grab()
    assert helper._last_effective_renderer == "retail_indexed_reconstructed"


def test_briefing_dragonfly_animation_plays_when_main_view_is_paused(window):
    from PySide6.QtCore import QSize
    from PySide6.QtTest import QTest
    from map_editor import bootstrap
    from studio.assets.catalog import load_game_objects

    unit = next(o for o in load_game_objects(bootstrap.installation())
                if o.kind == "vehicle" and o.object_id == 14)
    assert window._open_game_object(unit)
    window.play_button.setChecked(False)
    window.show()
    window.properties_tabs.setCurrentIndex(window.briefing_tab_index)
    view = window.briefing_preview
    assert view.renderer.has_animation
    states = set()
    for _ in range(8):
        QTest.qWait(130)
        states.add(tuple(sorted(view.renderer._anim_states.items())))
    assert len(states) > 1
    assert not window.play_button.isChecked()
    view.timer.stop()
    view.angle = 0
    renderer = view.renderer
    renderer.seek_animation(0)
    first = renderer.render_snapshot(QSize(320, 200), None)
    frame = next(material.anim_frames[0][0] for material in renderer._materials
                 if len(material.anim_frames) > 1 and material.anim_frames[0][0] > 0)
    renderer.seek_animation(frame * 1000 / 1024 + 1)
    second = renderer.render_snapshot(QSize(320, 200), None)
    assert first != second


def test_dragonfly_source_component_can_be_picked_without_a_collision_shape(window):
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtTest import QTest
    from map_editor import bootstrap
    from studio.assets.catalog import load_game_objects
    from entity_designer.widgets import _GEOMETRY_COMPONENT_ROLE

    unit = next(o for o in load_game_objects(bootstrap.installation())
                if o.kind == "vehicle" and o.object_id == 14)
    assert window._open_game_object(unit)
    window.project.collision_shape = None
    window.show()
    window.properties_tabs.setCurrentIndex(window.collision_shape_tab_index)
    window._sync_all()
    view = window.viewport
    QApplication.processEvents()
    point = key = None
    for candidate, triangles in view._geometry_component_picks.items():
        for triangle in triangles:
            projected = [view._project_visible_world(view._preview_point(p)) for p in triangle]
            if any(p is None for p in projected):
                continue
            center = QPointF(sum(p.x() for p in projected) / 3,
                             sum(p.y() for p in projected) / 3).toPoint()
            if view.rect().contains(center) and view._hit_geometry_component(QPointF(center)) == candidate:
                point, key = center, candidate
                break
        if key is not None:
            break
    assert key is not None
    QTest.mouseClick(view, Qt.MouseButton.LeftButton, pos=point)
    assert [row.data(0, _GEOMETRY_COMPONENT_ROLE)
            for row in window.collision_geometry_widget.tree.selectedItems()] == [key]
    assert view._geometry_component_highlight


def test_fire_area_loads_air_cube_external_normal_model(window):
    from map_editor import bootstrap
    from studio.assets.catalog import load_game_objects

    weapon = next(o for o in load_game_objects(bootstrap.installation())
                  if o.kind == "weapon" and o.object_id == 41)
    assert window._open_game_object(weapon)
    window.properties_tabs.setCurrentIndex(window.fire_area_tab_index)
    window._fire_area_unit_chosen(149)
    window._push_fx_frame()
    item = next(item for item in window.viewport._fx_items
                if item.label.startswith("firing unit ·"))
    assert item.external == (
        "Data/Models/Base/Original_Refined/6P0_Air_Cube/6P0_Air_Cube_Normal/6P0_Air_Cube_Normal.BASE",)
    helper = window.viewport._fx_external_for(item)
    assert helper is not None
    assert helper._family_ref.base_path.name == "6P0_Air_Cube_Normal.BASE"


def test_fire_area_tab_is_grey_on_a_vehicle_and_open_on_its_weapon(window):
    from map_editor import bootstrap
    from studio.assets.catalog import load_game_objects
    objects = load_game_objects(bootstrap.installation())
    vehicle = next(o for o in objects if o.kind == "vehicle" and o.object_id == 38)
    tabs = window.properties_tabs
    _mnojet(window)
    assert tabs.isTabEnabled(window.fire_area_tab_index)
    tabs.setCurrentIndex(window.fire_area_tab_index)
    # Opening the vehicle leaves Fire Area: its parameters belong to the weapon.
    assert window._open_game_object(vehicle)
    assert not tabs.isTabEnabled(window.fire_area_tab_index)
    assert tabs.currentIndex() != window.fire_area_tab_index


def test_gizmo_rises_under_the_short_cockpit_tab(window):
    import time
    from map_editor import bootstrap
    from studio.assets.catalog import load_game_objects

    vehicle = next(o for o in load_game_objects(bootstrap.installation())
                   if o.kind == "vehicle" and o.object_id == 38)
    assert window._open_game_object(vehicle)
    # Tall window: any leftover space would show up as gaps in the column.
    window.resize(1500, 1500)
    window.show()

    def settle():
        end = time.time() + 0.4
        while time.time() < end:
            QApplication.processEvents()

    tabs = window.properties_tabs
    tabs.setCurrentIndex(window.collision_tab_index)
    settle()
    full_height = tabs.height()
    tabs.setCurrentIndex(window.cockpit_tab_index)
    settle()
    box = window.cockpit_box
    gap = (window.transform_box.mapTo(window, window.transform_box.rect().topLeft()).y()
           - box.mapTo(window, box.rect().bottomLeft()).y())
    strip = tabs._palette_strip
    gap_above = (tabs.mapTo(window, tabs.rect().topLeft()).y()
                 - strip.mapTo(window, strip.rect().bottomLeft()).y())
    # Buttons, Cockpit View rows and gizmo form one compact block.
    assert tabs.height() < full_height
    assert 0 <= gap < 30 and 0 <= gap_above < 30
    tabs.setCurrentIndex(window.fire_points_tab_index)
    settle()
    assert tabs.height() == full_height
    window._modified = False


def test_loose_texture_beside_the_base_wins_over_the_set(tmp_path):
    """External files are read first; the SET.BAS only fills in what is missing.

    The file is added after a first load of the same BASE: the folder listing
    of that first load must not hide it.
    """

    import shutil
    from entity_designer.editor import CollisionEditorWindow
    from studio.formats.setbas import read_setbas
    set_bas = DATA / "Sets/Set1/Objects/Set.BAS"
    archive = read_setbas(set_bas)
    resources = {r.resource_name.upper(): r for r in archive.resources}
    folder = tmp_path / "model"
    shutil.copytree(BOMB.parent, folder)
    base = folder / BOMB.name
    load = CollisionEditorWindow._load_family
    from_set = load(base, setbas=set_bas).textures["FAERROBO.ILBM"]
    assert from_set.source_name.startswith("SET.BAS")
    # A different picture under the same name, as a loose file next to the BASE.
    (folder / "FAERROBO.ILBM").write_bytes(
        archive.payload_bytes(resources["FBAGGER.ILBM"]))
    family = load(base, setbas=set_bas)
    other = family.textures["FAERROBO.ILBM"]
    assert not other.source_name.startswith("SET.BAS")
    assert bytes(other.pixels) != bytes(from_set.pixels)
    assert any("loose wins" in warning for warning in family.warnings)
