"""A loose BASE unit takes its textures and its effect VPs from the active set."""

import os
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

DATA = Path(os.environ.get("NME_GAME_DATA", ""))
BOMB = DATA / "Models/Base/New/Mnosjetz_Bomb_Normal_Wait/VP_MNOBOMB_NORMAL.BASE"

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
    window.close()
    bootstrap.set_installation(None)


def _mnojet(window):
    from map_editor import bootstrap
    from studio.assets.catalog import load_game_objects
    # The Mnojet bomb is weapon 38 (its name in the script may change).
    obj = next(o for o in load_game_objects(bootstrap.installation())
               if o.kind == "weapon" and o.object_id == 38)
    assert window._open_game_object(obj)


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
