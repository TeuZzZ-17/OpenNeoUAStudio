"""Construction metadata and preservation during Entity Designer edits."""
import unittest
from entity_designer import CollisionProject, CollisionSphere, VEHICLE, BUILDING
from entity_designer.script_model import apply_editable_overwrite_preview, build_editable_overwrite_preview
from map_editor.core.building_defs import parse_building_text
from map_editor.core.vehicle_defs import parse_vehicle_text

class BuildingTimeTests(unittest.TestCase):
    def test_metadata_range_and_modify(self):
        for kind, parse in (("vehicle", parse_vehicle_text), ("building", parse_building_text)):
            with self.subTest(kind=kind):
                self.assertIsNone(parse(f"new_{kind} 3\nend\n")[3].building_time)
                for raw, expected in (("0", 0.0), ("0.4", 0.4), ("4.4", 4.4), ("10", 10.0), ("50", 50.0), ("100", 100.0), ("101", None), ("-1", None), ("nan", None), ("inf", None), ("4.4junk", None), ("0,4", None)):
                    text = f"new_{kind} 3\n building_time = {raw}\nend\n"
                    self.assertEqual(parse(text)[3].building_time, expected)
                    self.assertEqual(parse(text + f"modify_{kind} 3\n name = Probe\nend\n")[3].building_time, expected)
                    self.assertEqual(parse(text + f"modify_{kind} 3\n building_time = 0.4\nend\n")[3].building_time, 0.4)

    def test_entity_designer_preserves_time_when_editing_collision(self):
        for category in (VEHICLE, BUILDING):
            # Buildings have no pal/shk_genesis_time: their effects span the construction.
            keys = [("snd_genesis_sample", "sounds/build.wav"), ("snd_genesis_volume", "120"),
                    ("snd_genesis_pitch", "-100"), ("pal_genesis_slot", "1"),
                    ("pal_genesis_time", "1000"), ("pal_genesis_mag0", "0.25"), ("pal_genesis_mag1", "0"),
                    ("shk_genesis_slot", "1"), ("shk_genesis_time", "1000"),
                    ("shk_genesis_mag0", "0.5"), ("shk_genesis_mag1", "0")]
            if category == BUILDING:
                keys = [(key, value) for key, value in keys if not key.endswith("_time")]
            source = f"new_{category} 7\n model = tank\n building_time = 4.4\n radius = 20\n" + "".join(
                f" {key} = {value}\n" for key, value in keys) + "end\n"
            project = CollisionProject(target_category=category, legacy=CollisionSphere("legacy", radius=21))
            patch = build_editable_overwrite_preview(f"new_{category}", 7, project, set())
            updated, _groups = apply_editable_overwrite_preview(source, patch, f"new_{category}", 7)
            self.assertIn("building_time = 4.4", updated)
            for prefix in ("snd_genesis", "pal_genesis", "shk_genesis"):
                original = [line.strip() for line in source.splitlines() if line.strip().startswith(prefix)]
                actual = [line.strip() for line in updated.splitlines() if line.strip().startswith(prefix)]
                self.assertEqual(actual, original)

if __name__ == "__main__":
    unittest.main()
