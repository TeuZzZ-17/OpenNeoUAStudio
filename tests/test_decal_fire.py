from entity_designer import destruction_fx as fx
from entity_designer import decal_smoke as ds


def profile(rows):
    return fx.decal_profiles_from_text('new_decal 1\n duration = permanent\n'+rows+'\nend\n')[1]


def simulation(rows):
    unit=fx.UnitPhases(weapon=True,ground_decal_id=1,decal_size=(100.,100.))
    document=fx.extract_fx_document('new_weapon 1\nend\n',0,1)
    return fx.PreviewSimulation(document,fx.CONTEXT_WEAPON,fx.WEAPON_HIT_GROUND,
        unit=unit,decals={1:profile(rows)})


def test_gate_uses_authored_percentage():
    assert profile(' fire_change = 35%').spec.fire.chance==35
    assert profile(' fire_change = 100%').spec.fire.flame
    assert profile(' fire_duration = 1000_2000').spec.fire is None
    assert profile(' fire_change = nope').spec.fire is None
    assert profile(' fire_change = 0%').spec.fire is None


def test_overrides_and_clamps_match_engine():
    fire=profile(' fire_change = 100\n fire_count = 6_2\n fire_size = 12_20\n fire_fade_inout = 20_100\n fire_propagation_chance = 50%\n fire_propagation_count = 99\n fire_propagation_delay = 0').spec
    assert fire.fire.count==(2,6)
    assert fire.fire.size==(12.,20.)
    assert (fire.fire.fade_in,fire.fire.fade_out)==(20,100)
    assert fire.fire_propagation_chance==50
    assert fire.fire_propagation_count==8
    assert fire.fire_propagation_delay==0


def test_preview_uses_flame_particles_and_stops():
    sim=simulation(' fire_change = 100%\n fire_duration = 2000\n fire_fade_inout = 0_400')
    when=fx.WEAPON_FLIGHT_MS+1000
    flames=[item for item in sim.items_at(when) if item.kind=='decal_fire']
    assert flames and all(item.additive for item in flames)
    assert all(item.scale[1]>item.scale[0] for item in flames)
    assert not [item for item in sim.items_at(when+3000) if item.kind=='decal_fire']


def test_propagation_is_delayed_and_one_generation():
    sim=simulation(' fire_change = 100%\n fire_propagation_chance = 100%\n fire_propagation_count = 2\n fire_propagation_radius = 50\n fire_propagation_delay = 1000')
    before=[i for i in sim.items_at(fx.WEAPON_FLIGHT_MS+500) if i.kind==fx.MODE_DECAL]
    after=[i for i in sim.items_at(fx.WEAPON_FLIGHT_MS+2000) if i.kind==fx.MODE_DECAL]
    assert len(before)==1
    assert len(after)==3


def test_source_size_is_bounded_and_absent_is_zero():
    assert fx.unit_look({},weapon=False)['decal_size']==(0.,0.)
    assert fx.unit_look({'ground_decal_size':'120_80'},weapon=False)['decal_size']==(80.,120.)
    assert fx.unit_look({'ground_decal_size':'12001'},weapon=False)['decal_size']==(0.,0.)
