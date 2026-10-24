import unittest

from entity_designer import CollisionProject, CollisionSphere, VEHICLE
from entity_designer.script_model import (
    apply_editable_overwrite_preview,
    build_editable_overwrite_preview,
)
from map_editor.core.building_defs import parse_building_text


class DatabaseMetadataRoundTripTests(unittest.TestCase):
    def test_vehicle_description_and_purchase_cost_survive_unrelated_noop(self):
        source = (
            "new_vehicle 7\n"
            "    model = tank\n"
            "    vehicle_description = Heavy probe with multi word text\n"
            "    purchase_cost = 123456789\n"
            "    radius = 20\n"
            "end\n"
        )
        project = CollisionProject(
            target_category=VEHICLE,
            legacy=CollisionSphere("legacy", radius=20),
        )
        patch = build_editable_overwrite_preview("new_vehicle", 7, project, set())
        updated, _groups = apply_editable_overwrite_preview(
            source, patch, "new_vehicle", 7)
        self.assertIn("vehicle_description = Heavy probe with multi word text", updated)
        self.assertIn("purchase_cost = 123456789", updated)

    def test_building_description_is_parsed_and_building_has_no_purchase_cost_field(self):
        parsed = parse_building_text(
            "new_building 3\n"
            "    model = building\n"
            "    building_description = Relay station with guns\n"
            "    purchase_cost = 99\n"
            "end\n"
        )
        building = parsed[3]
        self.assertEqual(building.description, "Relay station with guns")
        self.assertFalse(hasattr(building, "database_visibility"))
        self.assertFalse(hasattr(building, "purchase_cost"))
        self.assertNotIn("purchase_cost", {"name", "model", "description", "energy", "production_cost"})


if __name__ == "__main__":
    unittest.main()