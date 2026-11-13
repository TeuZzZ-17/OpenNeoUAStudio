"""Effect-list summaries expose authored models and header comments."""

import unittest

from entity_designer import destruction_fx as fx


class EffectSummaryTests(unittest.TestCase):
    def block(self, header, body):
        return fx.FxBlock([header, *body.splitlines(), "end"])

    def test_flight_preview_default_is_300(self):
        self.assertEqual(fx.DEFAULT_FLIGHT_ALTITUDE, 300.0)

    def test_visual_lists_every_vp_and_external_filename(self):
        block = self.block("begin_fx ; Frammenti", """\
trigger = air_destroyed|destroyed
mode = visual
vp_model = 204
vp_model = 145
base_model = Data/Models/Base/Organic.BASE
3ds_model = Data\\Models\\Shard.3ds
vp_model = 204
duration = 850
end_size = 5""")
        summary = fx.block_summary(block, fx.CONTEXT_VEHICLE)
        self.assertEqual(block.comment, "Frammenti")
        self.assertIn("3 VP: 204, 145, 204", summary)
        self.assertIn("BASE Organic.BASE", summary)
        self.assertIn("3DS Shard.3ds", summary)
        self.assertIn("850 ms", summary)

    def test_physical_lists_vps_and_impact_vps(self):
        block = self.block("begin_fx", """\
trigger = destroyed
mode = physical
vp_model = 10|10|20
vp_impact = 3|3|4
mass = 30
radius = 5
count = 3~5""")
        summary = fx.block_summary(block, fx.CONTEXT_VEHICLE)
        self.assertEqual(block.comment, "")
        self.assertIn("3 VP: 10, 10, 20", summary)
        self.assertIn("Impact VP: 3, 3, 4", summary)
        self.assertIn("×3~5", summary)

    def test_physical_external_model_without_vp_and_incomplete_effect(self):
        block = self.block("  begin_fx; Spore", """\
trigger = destroyed
mode = physical
base_model = Data\\Models\\Flesh.BASE
vp_impact = 145
count = 2""")
        summary = fx.block_summary(block, fx.CONTEXT_VEHICLE)
        self.assertFalse(fx.interpret_block(block, fx.CONTEXT_VEHICLE).active)
        self.assertEqual(block.comment, "Spore")
        self.assertIn("BASE Flesh.BASE", summary)
        self.assertIn("Impact VP: 145", summary)
        self.assertNotIn("0 VP", summary)

    def test_physical_repeated_model_key_uses_last_list(self):
        block = self.block("begin_fx ;  ", """\
trigger = destroyed
mode = physical
vp_model = 10|20
vp_model = 30
mass = 1
radius = 1""")
        self.assertEqual(block.comment, "")
        self.assertIn("1 VP: 30", fx.block_summary(block, fx.CONTEXT_VEHICLE))


if __name__ == "__main__":
    unittest.main()
