from entity_designer import destruction_fx as fx

IMPACT_MS = fx.WEAPON_FLIGHT_MS * fx.DEFAULT_PROJECTILE_ALTITUDE / fx.WEAPON_LAUNCH_HEIGHT


def profile(rows):
    return fx.decal_profiles_from_text('new_decal 1\n duration = permanent\n'+rows+'\nend\n')[1]


def simulation(rows):
    unit=fx.UnitPhases(weapon=True,ground_decal_id=1,decal_size=(100.,100.))
    document=fx.extract_fx_document('new_weapon 1\nend\n',0,1)
    return fx.PreviewSimulation(document,fx.CONTEXT_WEAPON,fx.WEAPON_HIT_GROUND,
        unit=unit,decals={1:profile(rows)})


def test_old_fire_keys_are_ignored():
    sim=simulation(' fire_change = 100%\n fire_duration = 2000')
    when=fx.WEAPON_FLIGHT_MS+1000
    assert not [item for item in sim.items_at(when) if item.kind=='decal_smoke']


def test_preview_smoke_uses_round_puffs_and_stops():
    sim=simulation(' effect_3ds = Data/Models/3ds/Smoke.3ds\n effect_mode = rise\n effect_count = 5\n effect_active_time = 2000\n effect_fade_inout = 0|400')
    when=fx.WEAPON_FLIGHT_MS+1000
    puffs=[item for item in sim.items_at(when) if item.kind=='decal_smoke']
    assert puffs and not any(item.additive for item in puffs)
    assert all(item.scale[0]==item.scale[1]==item.scale[2] for item in puffs)
    assert not [item for item in sim.items_at(when+3000) if item.kind=='decal_smoke']


def test_effect_without_a_model_draws_nothing():
    base = ' effect_base = Data/Models/Base/New/Effect_Smoke/Effect_Smoke.BASE\n'
    for rows in (' effect_count = 5\n effect_active_time = 2000',
                 ' effect_mode = rise\n' + base + ' effect_active_time = 2000'):
        sim=simulation(rows)
        when=fx.WEAPON_FLIGHT_MS+1000
        assert not [item for item in sim.items_at(when) if item.kind in ('decal_smoke','visual')]


def test_source_size_is_bounded_and_absent_is_zero():
    assert fx.unit_look({},weapon=False)['decal_size']==(0.,0.)
    assert fx.unit_look({'ground_decal_size':'120~80'},weapon=False)['decal_size']==(80.,120.)
    assert fx.unit_look({'ground_decal_size':'12001'},weapon=False)['decal_size']==(0.,0.)


def test_mesh_puffs_are_short_pulses_instead_of_smoke_discs():
    sim = simulation(' effect_mode = ground_pulse\n effect_3ds = Data/Models/3ds/Spark.3ds\n'
                     ' effect_pulse_interval = 900\n effect_pulse_time = 120\n'
                     ' effect_active_time = 4000\n effect_fade_inout = 0|0')
    start = IMPACT_MS
    first = sim.items_at(start + 30)
    assert any(item.label == 'decal mesh pulse' for item in first)
    assert not any(item.kind == 'decal_smoke' for item in first)
    assert not any(item.label == 'decal mesh pulse' for item in sim.items_at(start + 300))
    assert any(item.label == 'decal mesh pulse' for item in sim.items_at(start + 930))
    assert not any(item.label == 'decal mesh pulse' for item in sim.items_at(start + 5000))


def test_original_vp_pulses_use_active_set_without_external_mesh():
    sim = simulation(' effect_mode = ground_pulse\n effect_vp = 2\n effect_active_time = 4000\n effect_fade_inout = 0|0')
    items = sim.items_at(IMPACT_MS + 30)
    pulse = next(item for item in items if item.label == 'decal mesh pulse')
    assert pulse.vp == 2 and not pulse.external
    assert not any(item.kind == 'decal_smoke' for item in items)
    assert not any(item.label == 'decal mesh pulse'
                   for item in sim.items_at(IMPACT_MS + 300))


def test_base_model_is_an_effect_source():
    path = 'Data/Models/Base/New/Effect_Smoke/Effect_Smoke.BASE'
    sim = simulation(f' effect_mode = ground_pulse\n effect_base = {path}\n effect_active_time = 4000\n effect_fade_inout = 0|0')
    pulse = next(item for item in sim.items_at(IMPACT_MS + 30)
                 if item.label == 'decal mesh pulse')
    assert pulse.external == (path,)


def test_external_plume_uses_puffs_instead_of_pulses():
    sim = simulation(' effect_3ds = Data/Models/3ds/Smoke.3ds\n effect_mode = rise\n'
                     ' effect_active_time = 4000\n effect_fade_inout = 0|0')
    items = sim.items_at(fx.WEAPON_FLIGHT_MS + 1000)
    assert any(item.kind == 'decal_smoke' for item in items)
    assert not any(item.label == 'decal mesh pulse' for item in items)


def test_pulse_interval_range_rolls_per_pulse_and_fixed_stays_constant():
    from entity_designer import decal_smoke as ds
    ranged = profile(' effect_mode = ground_pulse\n effect_3ds = Data/Models/3ds/Spark.3ds\n effect_pulse_interval = 1500~700').spec.smoke
    assert ranged.pulse_interval == (700, 1500)
    lengths = [ds.pulse_interval(ranged, 77, cycle) for cycle in range(50)]
    assert all(700 <= value <= 1500 for value in lengths) and len(set(lengths)) > 1
    cycle, start, length = ds.pulse_cycle(ranged, 77, sum(lengths[:3]) + 5)
    assert (cycle, start, length) == (3, sum(lengths[:3]), lengths[3])
    fixed = profile(' effect_mode = ground_pulse\n effect_3ds = Data/Models/3ds/Spark.3ds\n effect_pulse_interval = 900').spec.smoke
    assert ds.pulse_cycle(fixed, 77, 1900) == (2, 1800, 900)


def test_effect_mode_is_mandatory_and_wrong_mode_keys_warn():
    from entity_designer import decal_smoke as ds
    no_mode = profile(' effect_3ds = Data/Models/3ds/Smoke.3ds')
    assert no_mode.spec.smoke.mode == '' and not ds.emits(no_mode.spec.smoke)
    assert any('without effect_mode' in issue.message for issue in no_mode.spec.issues)
    old = profile(' effect_3ds = Data/Models/3ds/Smoke.3ds\n effect_mode = plume')
    assert old.spec.smoke.mode == '' and any('Invalid effect_mode' in i.message for i in old.spec.issues)
    wrong = profile(' effect_mode = ground_pulse\n effect_vp = 2\n effect_rise_speed = 20\n effect_count = 5')
    messages = [issue.message for issue in wrong.spec.issues]
    assert any('effect_rise_speed is ignored' in m for m in messages)
    assert any('effect_count is ignored' in m for m in messages)
    rise = profile(' effect_mode = rise\n effect_3ds = Data/Models/3ds/Smoke.3ds\n effect_pulse_time = 100')
    assert any('effect_pulse_time is ignored' in i.message for i in rise.spec.issues)
    lacking = profile(' effect_mode = rise\n effect_vp = 2')
    assert any('needs effect_3ds' in i.message for i in lacking.spec.issues) and not ds.emits(lacking.spec.smoke)
