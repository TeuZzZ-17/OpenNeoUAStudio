import unittest

from collision_editor import (
    CollisionProject,
    CollisionScriptError,
    CollisionSphere,
    GunPoint,
    OPENNEOUA,
    TurretLimits,
    VEHICLE,
    apply_editable_overwrite_preview,
    build_editable_overwrite_preview,
    plan_script_update,
)


def _apply(source: str, patch: str) -> str:
    updated, _groups = apply_editable_overwrite_preview(
        source, patch, "new_vehicle", 7)
    return updated


class CollisionEditorOverwriteV2Tests(unittest.TestCase):
    def test_noop_overwrite_has_no_patch_and_changes_nothing(self):
        source = (
            "; keep\n"
            "new_vehicle 7\n"
            "    name = Test\n"
            "    radius = 20\n"
            "end\n"
        )
        project = CollisionProject(
            target_category=VEHICLE,
            legacy=CollisionSphere("legacy", radius=20),
        )
        patch = build_editable_overwrite_preview(
            "new_vehicle", 7, project, set())
        self.assertEqual(patch, "")
        self.assertEqual(_apply(source, patch), source)

    def test_collision_replacement_keeps_anchor_and_removes_every_old_copy(self):
        source = (
            "new_vehicle 7\n"
            "    mass = 100\n"
            "\n"
            "    coll_num = 1\n"
            "\n"
            "    coll_act = 0\n"
            "    coll_x = 1\n"
            "    coll_y = 2\n"
            "    coll_z = 3\n"
            "    coll_radius = 4\n"
            "\n"
            "    job_fightrobo = 9\n"
            "    coll_num = 1\n"
            "    coll_act = 0\n"
            "    coll_x = 90\n"
            "    coll_y = 91\n"
            "    coll_z = 92\n"
            "    coll_radius = 93\n"
            "end\n"
        )
        patch = (
            "new_vehicle 7\n"
            "    ; [Tab: Collision]\n"
            "    coll_num = 2\n"
            "\n"
            "    coll_act = 0\n"
            "    coll_x = 10\n"
            "    coll_y = 20\n"
            "    coll_z = 30\n"
            "    coll_radius = 40\n"
            "\n"
            "    coll_act = 1\n"
            "    coll_x = -10\n"
            "    coll_y = -20\n"
            "    coll_z = -30\n"
            "    coll_radius = 14\n"
            "end\n"
        )
        updated = _apply(source, patch)
        self.assertLess(updated.index("coll_num = 2"),
                        updated.index("job_fightrobo = 9"))
        self.assertEqual(updated.count("coll_num ="), 1)
        self.assertEqual(updated.count("coll_act ="), 2)
        self.assertNotIn("coll_x = 1\n", updated)
        self.assertNotIn("coll_x = 90", updated)
        self.assertNotIn("Collision Editor disabled", updated)
        self.assertNotIn("\n\n\n", updated)

    def test_more_than_fifty_collision_spheres_round_trip_at_same_anchor(self):
        source = (
            "new_vehicle 7\n"
            "    name = Many\n"
            "    coll_num = 1\n"
            "    coll_act = 0\n"
            "    coll_x = 0\n"
            "    coll_y = 0\n"
            "    coll_z = 0\n"
            "    coll_radius = 1\n"
            "    mass = 500\n"
            "end\n"
        )
        project = CollisionProject(
            target_category=VEHICLE,
            compound=[
                CollisionSphere(OPENNEOUA, index, index + 1, index + 2, 3)
                for index in range(58)
            ],
        )
        patch = build_editable_overwrite_preview(
            "new_vehicle", 7, project, {"collision"})
        updated = _apply(source, patch)
        self.assertIn("coll_num = 58", updated)
        self.assertEqual(updated.count("coll_act ="), 58)
        self.assertLess(updated.index("coll_num = 58"), updated.index("mass = 500"))

    def test_scattered_duplicates_use_first_anchor_and_preserve_custom_data(self):
        source = (
            "new_vehicle 7\n"
            "    name = Duplicate\n"
            "    fire_x = 1\n"
            "    fire_y = 2\n"
            "    fire_z = 3\n"
            "    num_weapons = 1\n"
            "    custom_a = 10\n"
            "    fire_x = 99\n"
            "    fire_y = 98\n"
            "    fire_z = 97\n"
            "    num_weapons = 4\n"
            "    custom_b = 20\n"
            "end\n"
        )
        patch = (
            "new_vehicle 7\n"
            "    ; [Tab: Fire Points]\n"
            "    fire_x = 11\n"
            "    fire_y = 12\n"
            "    fire_z = 13\n"
            "    num_weapons = 2\n"
            "end\n"
        )
        updated = _apply(source, patch)
        self.assertLess(updated.index("fire_x = 11"), updated.index("custom_a = 10"))
        self.assertLess(updated.index("custom_a = 10"), updated.index("custom_b = 20"))
        self.assertEqual(updated.count("fire_x ="), 1)
        self.assertEqual(updated.count("num_weapons ="), 1)

    def test_new_gun_family_is_inserted_immediately_before_vehicle_end(self):
        source = (
            "new_vehicle 7\n"
            "    name = NoGuns\n"
            "    mass = 50\n"
            "end\n"
        )
        patch = (
            "new_vehicle 7\n"
            "    ; [Tab: Gun Points]\n"
            "    unit_num_guns = 1\n"
            "    unit_act_gun = 0\n"
            "    unit_gun_pos_x = 1\n"
            "    unit_gun_pos_y = 2\n"
            "    unit_gun_pos_z = 3\n"
            "    unit_gun_dir_x = 0\n"
            "    unit_gun_dir_y = 0\n"
            "    unit_gun_dir_z = 1\n"
            "    unit_gun_type = 9\n"
            "end\n"
        )
        updated = _apply(source, patch)
        body = updated.splitlines()
        self.assertEqual(body[-2].strip(), "unit_gun_type = 9")
        self.assertEqual(body[-1], "end")

    def test_fire_points_stay_at_original_position(self):
        source = (
            "new_vehicle 7\n"
            "    before = 1\n"
            "    fire_x = 1\n"
            "    fire_y = 2\n"
            "    fire_z = 3\n"
            "    num_weapons = 1\n"
            "    after = 2\n"
            "end\n"
        )
        patch = (
            "new_vehicle 7\n"
            "    ; [Tab: Fire Points]\n"
            "    fire_x = 4\n"
            "    fire_y = 5\n"
            "    fire_z = 6\n"
            "    num_weapons = 3\n"
            "end\n"
        )
        updated = _apply(source, patch)
        self.assertLess(updated.index("before = 1"), updated.index("fire_x = 4"))
        self.assertLess(updated.index("fire_x = 4"), updated.index("after = 2"))

    def test_gun_points_stay_at_original_position(self):
        source = (
            "new_vehicle 7\n"
            "    before = 1\n"
            "    unit_num_guns = 1\n"
            "    unit_act_gun = 0\n"
            "    unit_gun_pos_x = 1\n"
            "    unit_gun_type = 2\n"
            "    after = 2\n"
            "end\n"
        )
        project = CollisionProject(
            target_category=VEHICLE,
            gun_points_enabled=True,
            gun_points=[GunPoint(x=9, gun_type=8)],
        )
        patch = build_editable_overwrite_preview(
            "new_vehicle", 7, project, {"gun"})
        updated = _apply(source, patch)
        self.assertLess(updated.index("before = 1"),
                        updated.index("unit_num_guns = 1"))
        self.assertLess(updated.index("unit_gun_type = 8"),
                        updated.index("after = 2"))
        self.assertEqual(updated.count("unit_num_guns ="), 1)

    def test_cockpit_view_stays_at_original_position(self):
        source = (
            "new_vehicle 7\n"
            "    before = 1\n"
            "    cockpit_camera_offset_x = 1\n"
            "    cockpit_camera_offset_y = 2\n"
            "    cockpit_camera_offset_z = 3\n"
            "    after = 2\n"
            "end\n"
        )
        patch = (
            "new_vehicle 7\n"
            "    ; [Tab: Cockpit View]\n"
            "    cockpit_camera_offset_x = 7\n"
            "    cockpit_camera_offset_y = 8\n"
            "    cockpit_camera_offset_z = 9\n"
            "end\n"
        )
        updated = _apply(source, patch)
        self.assertLess(updated.index("before = 1"),
                        updated.index("cockpit_camera_offset_x = 7"))
        self.assertLess(updated.index("cockpit_camera_offset_z = 9"),
                        updated.index("after = 2"))

    def test_overwrite_is_byte_stable_on_second_application(self):
        source = (
            "new_vehicle 7\r\n"
            "    name = Stable\r\n"
            "\r\n"
            "    ; Collision Editor disabled: compound collisions\r\n"
            "    ; coll_num = 1\r\n"
            "    ; coll_act = 0\r\n"
            "    ; coll_x = 1\r\n"
            "    ; coll_y = 2\r\n"
            "    ; coll_z = 3\r\n"
            "    ; coll_radius = 4\r\n"
            "    coll_num = 1\r\n"
            "    coll_act = 0\r\n"
            "    coll_x = 5\r\n"
            "    coll_y = 6\r\n"
            "    coll_z = 7\r\n"
            "    coll_radius = 8\r\n"
            "end\r\n"
        )
        patch = (
            "new_vehicle 7\n"
            "    ; [Tab: Collision]\n"
            "    coll_num = 1\n"
            "    coll_act = 0\n"
            "    coll_x = 10\n"
            "    coll_y = 20\n"
            "    coll_z = 30\n"
            "    coll_radius = 40\n"
            "end\n"
        )
        first = _apply(source, patch)
        second = _apply(first, patch)
        self.assertEqual(second, first)
        self.assertNotIn("Collision Editor disabled", first)
        self.assertIn("\r\n", first)

    def test_new_family_reuses_existing_block_indentation(self):
        source = "new_vehicle 7\n name = Indented\n mass = 10\nend\n"
        patch = (
            "new_vehicle 7\n"
            " ; [Tab: Cockpit View]\n"
            " cockpit_camera_offset_x = 1\n"
            " cockpit_camera_offset_y = 2\n"
            " cockpit_camera_offset_z = 3\n"
            "end\n"
        )
        updated = _apply(source, patch)
        self.assertIn("\n cockpit_camera_offset_x = 1\n", updated)
        self.assertNotIn("\n    cockpit_camera_offset_x", updated)

    def test_incomplete_editable_preview_is_rejected(self):
        source = "new_vehicle 7\n    radius = 20\nend\n"
        patch = (
            "new_vehicle 7\n"
            "    ; [Tab: Collision]\n"
            "    radius = 30\n"
        )
        with self.assertRaises(CollisionScriptError):
            _apply(source, patch)

    def test_deduplication_is_scoped_to_the_selected_vehicle(self):
        source = (
            "new_vehicle 7\n"
            "    coll_num = 1\n"
            "    coll_act = 0\n"
            "    coll_x = 1\n"
            "    coll_y = 2\n"
            "    coll_z = 3\n"
            "    coll_radius = 4\n"
            "end\n\n"
            "new_vehicle 8\n"
            "    coll_num = 1\n"
            "    coll_act = 0\n"
            "    coll_x = 81\n"
            "    coll_y = 82\n"
            "    coll_z = 83\n"
            "    coll_radius = 84\n"
            "end\n"
        )
        patch = (
            "new_vehicle 7\n"
            "    ; [Tab: Collision]\n"
            "    coll_num = 1\n"
            "    coll_act = 0\n"
            "    coll_x = 10\n"
            "    coll_y = 20\n"
            "    coll_z = 30\n"
            "    coll_radius = 40\n"
            "end\n"
        )
        untouched = source[source.index("new_vehicle 8"):]
        updated = _apply(source, patch)
        self.assertEqual(updated[updated.index("new_vehicle 8"):], untouched)

    def test_old_generated_header_is_removed_but_manual_comment_is_preserved(self):
        source = (
            "new_vehicle 7\n"
            "    ; user's collision note\n"
            "    ; Collision Editor disabled: compound collisions\n"
            "    ; coll_num = 1\n"
            "    ; coll_act = 0\n"
            "    ; coll_x = 1\n"
            "    ; coll_y = 2\n"
            "    ; coll_z = 3\n"
            "    ; coll_radius = 4\n"
            "end\n"
        )
        patch = (
            "new_vehicle 7\n"
            "    ; [Tab: Collision]\n"
            "    coll_num = 1\n"
            "    coll_act = 0\n"
            "    coll_x = 5\n"
            "    coll_y = 6\n"
            "    coll_z = 7\n"
            "    coll_radius = 8\n"
            "end\n"
        )
        updated = _apply(source, patch)
        self.assertIn("; user's collision note", updated)
        self.assertNotIn("Collision Editor disabled", updated)
        self.assertNotIn("; coll_num", updated)

    def test_overwrite_removes_disabled_turret_data_without_a_header(self):
        source = (
            "new_vehicle 7\n"
            "    name = Carrier\n"
            "end\n\n"
            "new_vehicle 90\n"
            "    name = ReferencedGun\n"
            "    ; keep this manual comment\n"
            "    gun_side_angle = 500\n"
            "    gun_up_angle = 600\n"
            "    gun_down_angle = 700\n"
            "end\n"
        )
        project = CollisionProject(
            target_category=VEHICLE,
            turret_limits={
                90: TurretLimits(
                    90, enabled=False, source_kind="new_vehicle",
                    source_name="ReferencedGun", dirty=True),
            },
        )
        updated, _preview, _name = plan_script_update(
            source, "new_vehicle", 7, project,
            replace_all_managed=True)
        self.assertNotIn("gun_side_angle", updated)
        self.assertNotIn("gun_up_angle", updated)
        self.assertNotIn("gun_down_angle", updated)
        self.assertNotIn("Collision Editor disabled", updated)
        self.assertIn("; keep this manual comment", updated)


if __name__ == "__main__":
    unittest.main()
