import numpy as np

from map_editor.core.clouds import (CloudProfile, CloudSimulation, fade_envelope, load_cloud_profiles,
                                    map_particle_count, parse_cloud_profiles)
from map_editor.core.ldf_model import LdfDocument, cloud_id_value, dumps_ldf, loads_ldf
from .test_special_objects import app, win

SCRIPT = '''
new_cloud 2
    name = spores
    mesh_3ds = Data/Models/3ds/Spore.3ds
    count = 900
    lifetime_minmax = 9000_5000
    scale_xyz = 3.2_0_3.2
    tint = 170_255_145
end
new_cloud 7
    name = broken
    count = 10
end
new_cloud 1 ; first
    mesh_3ds = x.3ds
    count = 5
end
'''


def test_cloud_id_roundtrip_and_vanilla_default():
    doc = LdfDocument(mw=9, mh=9)
    assert 'cloud_id' not in dumps_ldf(doc)
    assert loads_ldf(dumps_ldf(doc)).lvl_info['cloud_id'] == -1
    doc.lvl_info['cloud_id'] = 3
    text = dumps_ldf(doc)
    assert 'cloud_id' in text
    restored = loads_ldf(text)
    assert restored.lvl_info['cloud_id'] == 3
    assert dumps_ldf(restored) == text
    for bad in (-4, 'abc', None, 2 ** 40):
        assert cloud_id_value(bad) == -1


def test_profiles_follow_engine_rules():
    profiles = parse_cloud_profiles(SCRIPT)
    assert sorted(profiles) == [1, 2]
    spores = profiles[2]
    assert spores.count == 512 and spores.lifetime == (5000, 9000)
    assert spores.scale == (3.2, 1.0, 3.2)
    assert spores.tint[:3] == (170 / 255, 1.0, 145 / 255) and spores.tint[3] == 1.0
    assert spores.label == '2 - spores'


def test_game_scripts_define_clouds(tmp_path):
    scripts = tmp_path / 'Scripts'
    scripts.mkdir()
    (scripts / 'Startup.cfg').write_text('include Clouds.cfg\n')
    (scripts / 'Clouds.cfg').write_text(SCRIPT)
    assert list(load_cloud_profiles(scripts)) == [1, 2]


def test_fade_and_simulation_stay_bounded():
    age = np.array([0.0, 150.0, 500.0, 950.0, 1000.0])
    fade = fade_envelope(age, np.full(5, 1000.0), 300, 100)
    assert np.allclose(fade, [0, .5, 1, .5, 0])
    profile = parse_cloud_profiles(SCRIPT)[2]
    profile.count = 40
    box = (0.0, 18000.0, -18000.0, 0.0)
    sim = CloudSimulation(profile, box, 0.0, 300, seed=1)
    for _ in range(100):
        sim.step(40)
    assert (sim.age < sim.life).all() and np.isfinite(sim.pos).all()
    assert (sim.pos[:, 0] >= 0).all() and (sim.pos[:, 0] <= 18000).all()
    assert (sim.pos[:, 2] >= -18000).all() and (sim.pos[:, 2] <= 0).all()
    # Spread over the whole box, not clustered in one corner.
    assert sim.pos[:, 0].std() > 3000 and sim.pos[:, 2].std() > 3000


def test_map_count_keeps_game_density():
    profile = CloudProfile(1, mesh_3ds='x.3ds', count=100, spawn_radius=(1000.0, 500.0, 1000.0))
    assert map_particle_count(profile, 4000.0 * 4000.0) == 400
    assert map_particle_count(profile, 1000.0) == 100
    assert map_particle_count(profile, 1e12) == 4000


def test_level_info_cloud_selector(app, win):
    panel = win.level_panel
    assert 'OpenNeoUA only' in panel.cloud_marker.toolTip()
    assert '#e8873a' in panel.cloud_marker.styleSheet()
    assert panel.cloud_box.itemData(0) == -1
    assert win.doc.lvl_info['cloud_id'] == -1
    if panel.cloud_box.count() > 1:
        panel.cloud_box.setCurrentIndex(1)
        panel.cloud_box.activated.emit(1)
        assert win.doc.lvl_info['cloud_id'] == panel.cloud_box.itemData(1)
        assert 'cloud_id' in dumps_ldf(win.doc)
        assert win.view.cloud_overlay.profile() is not None
