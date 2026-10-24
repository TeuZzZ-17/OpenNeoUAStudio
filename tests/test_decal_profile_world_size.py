from entity_designer.destruction_fx import decal_profiles_from_text

def test_decal_profile_uses_authored_world_unit_size_and_no_explosion_reduction():
    profile = decal_profiles_from_text(
        "new_decal 1\n"
        "    texture = Interface/GroundDecals/impact.png\n"
        "    duration = permanent\n"
        "end\n"
    )[1]
    assert profile.spec.size == (0.0, 0.0)
    assert profile.resolve("destroyed", (120.0, 600.0)).size == (120.0, 600.0)

def test_old_automatic_size_key_is_ignored():
    profiles = decal_profiles_from_text(
        "new_decal 1\n"
        "    duration = permanent\n"
        "end\n"
    )
    assert profiles[1].spec.size == (0.0, 0.0)


def test_fire_change_is_sole_gate_and_defaults_are_authored():
    from entity_designer.destruction_fx import decal_profiles_from_text
    p = decal_profiles_from_text('new_decal 4\n fire_duration = 100_200\n duration = permanent\n end\n')[4]
    assert p.spec.fire is None
    p = decal_profiles_from_text('new_decal 4\n fire_change = 1\n duration = permanent\n end\n')[4]
    assert p.spec.fire is not None and p.spec.fire.duration == (8000, 16000)
