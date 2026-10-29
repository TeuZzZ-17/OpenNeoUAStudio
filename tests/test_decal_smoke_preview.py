from entity_designer import destruction_fx as fx


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
    sim=simulation(' smoke_count = 5\n smoke_duration = 2000\n smoke_fade_inout = 0_400')
    when=fx.WEAPON_FLIGHT_MS+1000
    puffs=[item for item in sim.items_at(when) if item.kind=='decal_smoke']
    assert puffs and not any(item.additive for item in puffs)
    assert all(item.scale[0]==item.scale[1]==item.scale[2] for item in puffs)
    assert not [item for item in sim.items_at(when+3000) if item.kind=='decal_smoke']


def test_source_size_is_bounded_and_absent_is_zero():
    assert fx.unit_look({},weapon=False)['decal_size']==(0.,0.)
    assert fx.unit_look({'ground_decal_size':'120_80'},weapon=False)['decal_size']==(80.,120.)
    assert fx.unit_look({'ground_decal_size':'12001'},weapon=False)['decal_size']==(0.,0.)
