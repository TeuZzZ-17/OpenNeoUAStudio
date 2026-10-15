"""Sound, PAL and shake rows of Destruction FX: reading, editing and the shake preview."""

import pytest

from entity_designer import destruction_fx as fx
from entity_designer.fx_authoring import FxTextSession, effect_text, parse_effect_text

WEAPON = (
    "new_weapon 9\n"
    "    model = grenade\n"
    "    snd_launch_sample = sounds/launch.wav\n"
    "    snd_launch_volume = 100\n"
    "    pal_hit_slot = 1\n"
    "    pal_hit_time = 200\n"
    "    pal_hit_mag0 = 0.7\n"
    "    pal_hit_mag1 = 0\n"
    "    shk_hit_slot = 1\n"
    "    shk_hit_time = 250\n"
    "    shk_hit_mag0 = 0.8\n"
    "    shk_hit_mag1 = 0.5\n"
    "\n"
    "    shk_launch_player_slot = 4\n"
    "    shk_launch_player_time = 250\n"
    "    shk_launch_player_mag0 = 0.3\n"
    "    shk_launch_player_mag1 = 0.2\n"
    "    vp_normal = 5\n"
    "end\n")


def _document(text: str = WEAPON):
    return fx.extract_fx_document(text, 0, text.count("\n") - 1)


def _simulation(document=None, trigger=fx.WEAPON_HIT_GROUND, **options):
    unit = fx.UnitPhases(normal=fx.VisualRef(60), dead=fx.VisualRef(61), weapon=True)
    return fx.PreviewSimulation(document or _document(), fx.CONTEXT_WEAPON, trigger,
                                unit=unit, **options)


# -- rows ---------------------------------------------------------------------------------

def test_key_groups_become_rows_in_source_order():
    items = _document().items
    assert [(item.kind, item.event) for item in items] == [
        ("sound", "launch"), ("palette", "hit"), ("shake", "hit"),
        ("shake", "launch_player")]
    # launch_player is its own event, not the launch event.
    assert fx.key_group_of("shk_launch_player_slot") == ("shake", "launch_player")
    assert fx.key_group_of("shk_launch_slot") == ("shake", "launch")
    assert fx.key_group_of("snd_mimic_sample") is None


def test_blank_lines_inside_a_group_stay_and_other_keys_split_groups():
    text = ("new_weapon 1\n    shk_hit_slot = 1\n\n    shk_hit_time = 9\n"
            "    vp_normal = 5\n    shk_hit_mag0 = 2\nend\n")
    groups = [item for item in _document(text).items]
    assert [effect_text(item) for item in groups] == [
        "    shk_hit_slot = 1\n\n    shk_hit_time = 9", "    shk_hit_mag0 = 2"]


def test_keys_inside_a_begin_fx_are_not_rows():
    text = ("new_vehicle 1\nbegin_fx\n    trigger = destroyed\n    mode = physical\n"
            "    snd_normal_sample = x.wav\nend\nend\n")
    assert [type(item) for item in _document(text).items] == [fx.FxBlock]


def test_row_summaries_and_issues():
    sound, palette, shake, player = _document().items
    assert fx.key_group_summary(sound) == "launch.wav · vol 100"
    assert fx.key_group_summary(palette) == "slot 1 · 0.7→0 · 200 ms"
    assert fx.key_group_summary(shake) == "slot 1 · 0.8→0.5 · 250 ms"
    assert not fx.key_group_issues(shake, fx.CONTEXT_WEAPON)
    assert not fx.key_group_issues(player, fx.CONTEXT_WEAPON)
    # A weapon event is not a vehicle event: the engine would stop loading the script.
    vehicle = fx.key_group_issues(shake, fx.CONTEXT_VEHICLE)
    assert any(issue.severity == fx.SEVERITY_FATAL for issue in vehicle)
    typo = _document("new_weapon 1\n    shk_hit_slotx = 1\nend\n").items[0]
    assert any("Unknown key" in issue.message for issue in
               fx.key_group_issues(typo, fx.CONTEXT_WEAPON))


# -- the preview --------------------------------------------------------------------------

def test_shake_package_is_read_with_the_engine_defaults():
    spec = fx.sound_specs({"shk_hit_slot": "2"}, fx.WEAPON_SOUND_EVENTS)["hit"].shake
    assert spec == fx.ShakeFx(slot=2, mag0=1.0, mag1=0.0, time=1000, pos=(0.2, 0.2, 0.2))
    # The player launch shake clamps negatives and ignores x/y/z/mute.
    player = fx.sound_specs({"shk_launch_player_slot": "3", "shk_launch_player_mag0": "-1",
                             "shk_launch_player_x": "9"}, fx.WEAPON_SOUND_EVENTS)
    assert player["launch_player"].shake == fx.ShakeFx(
        slot=3, mag0=0.0, mag1=0.0, time=1000, pos=(0.2, 0.2, 0.2))
    assert "launch_player" not in fx.sound_specs(
        {"shk_launch_player_slot": "-4"}, fx.WEAPON_SOUND_EVENTS)


def test_shake_follows_its_package_in_time():
    simulation = _simulation()
    impact = fx.WEAPON_FLIGHT_MS
    assert simulation.shake_angles_at(impact - 50) == (0.0, 0.0, 0.0)
    shaking = simulation.shake_angles_at(impact + 100)
    assert any(shaking)
    assert max(abs(value) for value in shaking) <= 0.8 * 0.2 + 1e-9
    assert simulation.shake_angles_at(impact + 100) == shaking          # same frame, same shake
    assert simulation.shake_angles_at(impact + 300) == (0.0, 0.0, 0.0)  # past its 250 ms


def test_player_launch_shake_plays_at_the_launch():
    simulation = _simulation()
    assert any(simulation.shake_angles_at(50))
    assert simulation.shake_angles_at(400) == (0.0, 0.0, 0.0)


def test_hidden_rows_are_silent():
    document = _document()
    shake_row = next(i for i, item in enumerate(document.items)
                     if item.kind == "shake" and item.event == "hit")
    everything = set(range(len(document.items)))
    visible = _simulation(document, enabled=everything)
    hidden = _simulation(document, enabled=everything - {shake_row})
    impact = fx.WEAPON_FLIGHT_MS + 100
    assert any(visible.shake_angles_at(impact))
    assert hidden.shake_angles_at(impact) == (0.0, 0.0, 0.0)
    assert hidden.palette_effects_at(impact) == visible.palette_effects_at(impact)


def test_an_edited_row_changes_the_preview():
    document = _document()
    index = next(i for i, item in enumerate(document.items)
                 if item.kind == "shake" and item.event == "hit")
    document.items[index], _issues = parse_effect_text(
        "    shk_hit_slot = 1\n    shk_hit_time = 250\n    shk_hit_mag0 = 3\n    shk_hit_mag1 = 3",
        fx.CONTEXT_WEAPON)
    shaking = _simulation(document).shake_angles_at(fx.WEAPON_FLIGHT_MS + 100)
    assert max(abs(value) for value in shaking) > 0.8 * 0.2


def _event(spec, start=0.0, end=None):
    return fx.SoundEvent(start, end, spec, "hit", 0, "test")


def test_global_fades_follow_the_engine():
    spec = fx.SoundSpec(shake=fx.ShakeFx(slot=1, mag0=1.0, mag1=1.0, time=1000))
    sound = _event(spec)
    shake = spec.shake
    assert fx._shake_strength(sound, shake, 50, (0, 0)) == 1.0
    assert fx._shake_strength(sound, shake, 50, (100, 200)) == pytest.approx(0.5)
    assert fx._shake_strength(sound, shake, 500, (100, 200)) == pytest.approx(1.0)
    assert fx._shake_strength(sound, shake, 900, (100, 200)) == pytest.approx(0.5)
    # A loop has no end: only the fade-in applies.
    looping = _event(spec, 0.0, 5000.0)
    assert fx._shake_strength(looping, shake, 50, (100, 200)) == pytest.approx(0.5)
    assert fx._shake_strength(looping, shake, 4900, (100, 200)) == pytest.approx(1.0)
    flash = fx.SoundSpec(palette=fx.ScreenFx(slot=1, mag0=1.0, mag1=1.0, time=1000))
    assert fx._screen_fx_strength(_event(flash), flash.palette, 50, (100, 200)) == pytest.approx(0.5)


def test_the_global_fades_apply_to_the_preview():
    plain = _simulation()
    faded = _simulation(settings=fx.EffectSettings(shake_fade=(250, 0), palette_fade=(250, 0)))
    at = fx.WEAPON_FLIGHT_MS + 10
    plain_pal = plain.palette_effects_at(at)[0][1]
    assert faded.palette_effects_at(at)[0][1] < plain_pal * 0.2
    assert faded.palette_effects_at(at)[0][0] == plain.palette_effects_at(at)[0][0]
    assert max(map(abs, faded.shake_angles_at(at))) < max(map(abs, plain.shake_angles_at(at)))


def test_palette_limits_of_the_game_settings():
    simulation = _simulation()
    for slot, mag in ((1, 0.2), (2, 0.9), (3, 0.5)):
        spec = fx.SoundSpec(palette=fx.ScreenFx(slot=slot, mag0=mag, mag1=mag, time=1000))
        simulation._sounds.append(_event(spec, 5000.0))
    at = 5100.0
    simulation.settings = fx.EffectSettings(num_palfx=2)
    assert [slot for slot, _ in simulation.palette_effects_at(at)] == [2, 3]   # the two strongest
    simulation.settings = fx.EffectSettings(num_palfx=0)
    assert simulation.palette_effects_at(at) == []
    simulation.settings = fx.EffectSettings(color_effects=0)
    assert simulation.palette_effects_at(at) == []


def test_ini_settings_are_read_like_the_engine():
    assert fx.parse_global_fade("150_300") == (150, 300)
    assert fx.parse_global_fade("0_300") == (0, 300)
    assert fx.parse_global_fade("150") == (0, 0)
    assert fx.parse_global_fade("150_x") == (150, 0)
    assert fx.parse_global_fade("-5_100") == (0, 100)
    assert fx.parse_global_fade("1,5_100") == (0, 100)
    assert fx.parse_global_fade("") == (0, 0)
    assert fx.parse_effect_settings("") == fx.EffectSettings()
    settings = fx.parse_effect_settings(
        "game.global_shk_fade_inout = 150_300 ; shake\n"
        "game.global_pal_fade_inout = 100_200\n"
        "gfx.color_effects = 0\naudio.num_palfx = 20\naudio.volume = 64\n")
    assert settings == fx.EffectSettings(
        shake_fade=(150, 300), palette_fade=(100, 200), color_effects=0,
        num_palfx=8, volume=64)


# -- editing ------------------------------------------------------------------------------

def test_a_row_is_edited_and_saved_in_its_own_lines(tmp_path):
    script = tmp_path / "Weapons.cfg"
    script.write_text(WEAPON, encoding="utf-8")
    document = _document()
    last = WEAPON.count("\n") - 1
    session = FxTextSession(document, fx.CONTEXT_WEAPON, [(script, 0, last)])
    assert not session.source_error
    index = next(i for i, item in enumerate(document.items)
                 if item.kind == "shake" and item.event == "hit")
    session.set_draft(index, "    shk_hit_slot = 1\n    shk_hit_time = 400\n"
                             "    shk_hit_mag0 = 1\n    shk_hit_mag1 = 0")
    assert session.is_dirty(index)
    session.save([index])
    saved = script.read_text(encoding="utf-8")
    assert "shk_hit_time = 400" in saved and "shk_hit_time = 250" not in saved
    # Everything around the row stays exactly as it was.
    assert saved.replace("shk_hit_time = 400", "shk_hit_time = 250").replace(
        "    shk_hit_mag0 = 1\n    shk_hit_mag1 = 0",
        "    shk_hit_mag0 = 0.8\n    shk_hit_mag1 = 0.5") == WEAPON


def test_only_one_row_fits_in_the_editor():
    with pytest.raises(ValueError):
        parse_effect_text("    shk_hit_slot = 1\n    pal_hit_slot = 1", fx.CONTEXT_WEAPON)
    with pytest.raises(ValueError):
        parse_effect_text("    vp_normal = 5", fx.CONTEXT_WEAPON)
    item, issues = parse_effect_text("    shk_hit_slot = 1", fx.CONTEXT_WEAPON)
    assert isinstance(item, fx.FxKeyGroup) and not issues


# -- panel and viewport -------------------------------------------------------------------

@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _panel(app):
    from entity_designer.fx_panel import DestructionFxPanel

    panel = DestructionFxPanel()
    unit = fx.UnitPhases(normal=fx.VisualRef(60), dead=fx.VisualRef(61), weapon=True)
    panel.set_context(fx.CONTEXT_WEAPON, _document(), unit=unit)
    return panel


def test_list_shows_sound_pal_and_shake_rows(app):
    panel = _panel(app)
    titles = [panel.effects_list.item(i).text().split("\n")[0]
              for i in range(panel.effects_list.count())]
    assert titles == ["Sound · launch", "PAL · hit", "Shake · hit", "Shake · launch_player"]
    row = panel.effects_list.item(2)
    assert row.text().split("\n")[1] == "slot 1 · 0.8→0.5 · 250 ms"
    # A click opens the same editor as every other effect, with the row's own keys.
    panel.edit_effect_code(2)
    assert panel.code_drawer.code.toPlainText() == effect_text(panel.document.items[2])


def test_shake_button_sits_between_sound_and_palette_and_switches_the_shake_off(app):
    panel = _panel(app)
    layout = panel.mute_button.parentWidget().layout().itemAt(0).layout()
    assert layout.indexOf(panel.shake_button) == layout.indexOf(panel.mute_button) + 1
    assert layout.indexOf(panel.pal_button) == layout.indexOf(panel.shake_button) + 1
    panel.set_hidden_items(set())
    panel._set_trigger(fx.WEAPON_HIT_GROUND)
    panel.seek(fx.WEAPON_FLIGHT_MS + 100)
    assert any(panel.shake_angles())
    panel.shake_button.setChecked(True)
    assert panel.shake_angles() == (0.0, 0.0, 0.0)
    image = panel.shake_button.icon().pixmap(20, 20).toImage()
    assert image.pixelColor(10, 10).red() > 200          # the white slash of the muted icon


def test_hiding_a_row_from_the_list_silences_it(app):
    panel = _panel(app)
    panel._set_trigger(fx.WEAPON_HIT_GROUND)
    panel.seek(fx.WEAPON_FLIGHT_MS + 100)
    assert any(panel.shake_angles())
    panel.set_hidden_items({2})
    assert panel.shake_angles() == (0.0, 0.0, 0.0)


def test_viewport_turns_the_whole_scene_for_a_shake(app):
    from PySide6.QtGui import QImage, QPainter
    from entity_designer.viewport import CollisionViewport

    viewport = CollisionViewport()
    viewport.resize(400, 300)
    seen = []
    viewport._paint_fx_scene = lambda painter: seen.append(
        (painter.worldTransform().dx(), painter.worldTransform().dy()))
    image = QImage(400, 300, QImage.Format.Format_ARGB32)
    painter = QPainter(image)
    try:
        viewport._fx_shake = (0.0, 0.0, 0.0)
        viewport._paint_shaken_scene(painter)
        viewport._fx_shake = (0.05, 0.05, 0.0)
        viewport._paint_shaken_scene(painter)
    finally:
        painter.end()
    assert seen[0] == (0.0, 0.0)
    assert seen[1][0] != 0.0 and seen[1][1] != 0.0


def test_code_editor_has_the_same_controls_as_the_transport_bar(app):
    panel = _panel(app)
    panel.edit_effect_code(0)
    drawer = panel.code_drawer
    names = ("play", "loop", "mute", "shake", "pal")
    for name in names:
        mirror, source = getattr(drawer, f"{name}_button"), getattr(panel, f"{name}_button")
        assert mirror.isCheckable() and mirror.isChecked() == source.isChecked()
        mirror.click()                                    # the editor button drives the bar
        assert source.isChecked() == mirror.isChecked()
        source.setChecked(not source.isChecked())         # and follows it back
        assert mirror.isChecked() == source.isChecked()
        assert mirror.toolTip() == source.toolTip()
    panel.set_hidden_items(set())
    panel.seek(500)
    drawer.stop_button.click()
    assert panel.time_ms == 0.0
    drawer.restart_button.click()                         # restart and stop share the bar's actions


def test_a_short_shake_is_not_lost_when_frames_are_skipped():
    simulation = _simulation()
    impact = fx.WEAPON_FLIGHT_MS
    # The painted frame after a long gap looks back over the frames it skipped.
    strongest = simulation.shake_angles_between(impact - 20, impact + 200)
    assert any(strongest)
    single = simulation.shake_angles_at(impact + 200)
    norm = lambda angles: sum(value * value for value in angles)
    assert norm(strongest) >= norm(single)
    # Nothing is shaking before the hit, whatever the gap.
    assert simulation.shake_angles_between(0.0, impact - 100) == simulation.shake_angles_at(impact - 100)
