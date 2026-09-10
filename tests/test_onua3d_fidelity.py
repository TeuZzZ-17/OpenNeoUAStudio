from __future__ import annotations

import copy
import unittest

from anm_parser import VanmData, VanmFrame
from indexed_renderer import IndexedTables
from onua3d_export import build_scene
from onua3d_preview import (
    TICKS_PER_SECOND,
    tracy_rgba_table,
    vanm_timeline,
    preview_duration,
    surface_image,
)
from tests import test_onua3d_export as export_fixtures
from asset_family_package import AssetFamilyPackageError


class Onua3dFidelityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        export_fixtures.Onua3dTests.setUpClass()

    def setUp(self):
        export_fixtures.Onua3dTests.setUp(self)

    def _animated_fixture(self, *, pingpong=0, zero_time=False):
        obj = self.family.root_object
        group = obj.materials[0]
        group.kind, group.texture_name = "bmpanim", "TEST.ANM"
        source = self.family.textures["Texture:STATIC.ILB"]
        self.family.textures["Texture:SECOND.ILB"] = copy.deepcopy(source)
        uvs = [[(10 + i, 20 + i), (30 + i, 40 + i), (50 + i, 60 + i)]
               for i in range(8)]
        times = [0 if zero_time else 40] + [40] * 7
        self.family.animations["TEST.ANM"] = VanmData(
            bitmap_names=["Texture:STATIC.ILB", "Texture:SECOND.ILB"],
            texcoord_groups=uvs,
            frames=[VanmFrame(times[i], i % 2, i) for i in range(8)],
        )
        group.block.texture.anim_type = pingpong
        return obj

    def test_vanm_starts_at_zero_and_uses_exact_tick_boundaries(self):
        animation = VanmData(
            bitmap_names=["A"], texcoord_groups=[[(0, 0)]],
            frames=[VanmFrame(40, 0, 0), VanmFrame(40, 0, 0)],
        )
        timeline = vanm_timeline(animation, 0)
        self.assertEqual(timeline.starts[:3], [(0, 0), (40, 1)])
        self.assertEqual(timeline.period, 80)
        self.assertEqual(timeline.transitions(80), [(0, 0), (40, 1), (80, 0)])

    def test_glb_vanm_deduplicates_state_nodes_and_switches_bitmap_uv_with_step(self):
        self._animated_fixture()
        scene, pngs, _mapping, _warnings = build_scene(self.family)
        doc, binary = export_fixtures.decode_glb(scene)
        self.assertEqual(len(pngs), 2)
        animation = doc["animations"][0]
        self.assertEqual(animation["extras"]["onua3d"]["ticks_per_second"], 1024)
        self.assertTrue(all(s["interpolation"] == "STEP" for s in animation["samplers"]))
        # Each frame has a distinct (bitmap, UV) state, so every state is
        # represented once and visibility is selected by scale tracks.
        frame_nodes = [n for n in doc["nodes"]
                       if n.get("extras", {}).get("onua3d", {}).get("role") == "vanm_frame"]
        self.assertEqual(len(frame_nodes), 8)
        self.assertEqual(len(animation["channels"]), 8)
        # The first frame is visible at t=0; the next state begins exactly at
        # 40/1024 seconds, with no interpolated midpoint value.
        times = []
        for sampler in animation["samplers"]:
            times.extend(export_fixtures.values(doc, binary, sampler["input"]))
        self.assertIn(0.0, times)
        self.assertIn(40 / 1024, times)
        self.assertNotIn(20 / 1024, times)
        self.assertTrue(any(n.get("scale") == [1, 1, 1] for n in frame_nodes))
        self.assertTrue(any(n.get("scale") == [0, 0, 0] for n in frame_nodes))

    def test_repeated_vanm_states_deduplicate_nodes_but_keep_logical_frames(self):
        obj = self._animated_fixture()
        animation = self.family.animations["TEST.ANM"]
        animation.frames = [VanmFrame(40, i % 2, i % 2) for i in range(8)]
        scene, _pngs, mapping, _warnings = build_scene(self.family)
        doc, _binary = export_fixtures.decode_glb(scene)
        frame_nodes = [n for n in doc["nodes"]
                       if n.get("extras", {}).get("onua3d", {}).get("role") == "vanm_frame"]
        self.assertEqual(len(frame_nodes), 2)
        self.assertEqual(len(mapping[0]["vanm"][0]["frames"]), 8)
        self.assertEqual(len(doc["animations"][0]["channels"]), 2)

    def test_static_and_vanm_sibling_groups_keep_separate_carriers(self):
        obj = self._animated_fixture()
        obj.skeleton.points.append((0, 0, 1))
        obj.skeleton.polygons.append([0, 1, 3])
        animated = copy.deepcopy(obj.materials[0])
        animated.kind, animated.texture_name = "bmpanim", "TEST.ANM"
        animated.faces = [(1, [(10, 20), (30, 40), (50, 60)], 0)]
        obj.materials[0].kind, obj.materials[0].texture_name = "ilbm", "Texture:STATIC.ILB"
        obj.materials.append(animated)
        scene, _pngs, mapping, _warnings = build_scene(self.family)
        doc, _binary = export_fixtures.decode_glb(scene)
        root_row = mapping[0]
        static_mesh = doc["nodes"][root_row["geometry_node"]]["mesh"]
        static_faces = [face["poly_id"] for primitive in doc["meshes"][static_mesh]["primitives"]
                        for face in primitive["extras"]["onua3d"]["faces"]]
        self.assertIn(0, static_faces)
        self.assertNotIn(1, static_faces)
        animated_faces = [face["poly_id"] for mesh in doc["meshes"]
                          if "/ADES[1]/frame[" in mesh["name"]
                          for primitive in mesh["primitives"]
                          for face in primitive["extras"]["onua3d"]["faces"]]
        self.assertTrue(animated_faces)
        self.assertTrue(all(poly == 1 for poly in animated_faces))

    def test_vanm_pingpong_preserves_duplicated_endpoints(self):
        animation = VanmData(
            bitmap_names=["A"], texcoord_groups=[[(0, 0)]],
            frames=[VanmFrame(40, 0, 0), VanmFrame(40, 0, 0), VanmFrame(40, 0, 0)],
        )
        timeline = vanm_timeline(animation, 1)
        self.assertEqual([frame for _tick, frame in timeline.starts],
                         [0, 1, 2, 2, 1, 0])
        self.assertEqual(timeline.period, 240)

    def test_nonpositive_vanm_duration_freezes_and_is_reported(self):
        animation = VanmData(
            bitmap_names=["A"], texcoord_groups=[[(0, 0)]],
            frames=[VanmFrame(0, 0, 0), VanmFrame(40, 0, 0)],
        )
        timeline = vanm_timeline(animation, 0)
        self.assertIsNone(timeline.period)
        self.assertEqual(timeline.starts, [(0, 0)])
        duration, closed = preview_duration([timeline])
        self.assertEqual((duration, closed), (60 * TICKS_PER_SECOND, True))

    def test_vanm_without_frames_is_rejected(self):
        with self.assertRaisesRegex(AssetFamilyPackageError, "VANM has no frames"):
            vanm_timeline(VanmData(bitmap_names=[], texcoord_groups=[], frames=[]), 0)

    def test_surface_image_bakes_shader_lookup_into_png_palette(self):
        image = self.family.textures["Texture:STATIC.ILB"]
        image.width, image.height, image.pixels = 1, 1, b"\x08"
        shader = bytearray(bytes(range(256)) * 256)
        shader[128 * 256 + 8] = 12
        tables = IndexedTables(tuple((i, i, i) for i in range(256)), bytes(shader), bytes(range(256)) * 256)
        from indexed_renderer import IndexedSurface
        surface = IndexedSurface("Texture:STATIC.ILB", "texture", b"\x08", 1, 1, None,
                                 "gradient", 128, "none", "linear")
        qimage, _metadata = surface_image(image, surface, tables)
        self.assertEqual(qimage.pixelColor(0, 0).getRgb(), (12, 12, 12, 255))

    def test_flat_preview_keeps_blend_and_reports_used_index_rmse(self):
        image = self.family.textures["Texture:STATIC.ILB"]
        image.width, image.height, image.pixels = 2, 1, b"\x08\x00"
        identity = bytes(range(256)) * 256
        tracy = bytearray(identity)
        for background in range(256):
            tracy[background * 256] = background
        tables = IndexedTables(tuple((i, i, i) for i in range(256)), identity, bytes(tracy))
        from indexed_renderer import IndexedSurface
        surface = IndexedSurface("Texture:STATIC.ILB", "texture", image.pixels, 2, 1, None,
                                 "none", 0, "flat", "linear")
        qimage, metadata = surface_image(image, surface, tables, tracy_rgba_table(tables))
        self.assertEqual(qimage.pixelColor(0, 0).alpha(), 255)
        self.assertEqual(qimage.pixelColor(1, 0).alpha(), 0)
        self.assertEqual(metadata["tracy"], "flat")
        self.assertIn("linear_rgb_rmse_max_used_index", metadata)

    def test_shader_lookup_and_nnn_black_are_distinct_from_transparency(self):
        obj = self.family.root_object
        # Retail NNN is selected by the polygon flag code, independently of
        # the parsed texture label.
        obj.materials[0].block.polflags = 0
        scene, _pngs, _mapping, _warnings = build_scene(self.family)
        doc, _binary = export_fixtures.decode_glb(scene)
        # Structural NNN/black is an opaque factor, not alpha-zero masking.
        black = [m for m in doc["materials"]
                 if m.get("pbrMetallicRoughness", {}).get("baseColorFactor") == [0, 0, 0, 1]]
        self.assertTrue(black)
        palette = tuple((i, i, i) for i in range(256))
        shader = bytes((0 if shade == 255 or source == 0 else source
                        for shade in range(256) for source in range(256)))
        tables = IndexedTables(palette, shader, bytes(range(256)) * 256)
        self.assertEqual(tables.shade_index(0, 17), 17)
        self.assertEqual(tables.shade_index(255, 17), 0)

    def test_flat_tracy_preview_preserves_dark_nonzero_fx_against_studio_background(self):
        palette = tuple((i, i, i) for i in range(256))
        identity = bytes(range(256)) * 256
        tracy = bytearray(identity)
        # Model a destination-dependent FX source which the previous global
        # source-over fit could collapse toward transparency.  Studio starts
        # the indexed framebuffer at destination index 0, where this source
        # is deliberately dark/black.  ONUA3D must preserve that visible
        # silhouette while keeping numeric source zero clear.
        for background in range(256):
            tracy[background * 256 + 7] = background
        # One destination is actually changed, so this is not a truly
        # transparent identity column even though background 0 maps to black.
        tracy[128 * 256 + 7] = 127
        tables = IndexedTables(palette, identity, bytes(tracy))
        colors, _errors = tracy_rgba_table(tables)
        self.assertEqual(colors[0][3], 0)
        self.assertEqual(colors[7][:3], (0, 0, 0))
        self.assertGreater(colors[7][3], 0)
        self.assertLess(colors[7][3], 255)


    def test_flat_tracy_preview_is_no_longer_forced_fully_opaque(self):
        palette = tuple((i, i, i) for i in range(256))
        identity = bytes(range(256)) * 256
        tracy = bytearray(identity)
        for background in range(256):
            tracy[background * 256 + 19] = max(0, background - 32)
        tables = IndexedTables(palette, identity, bytes(tracy))
        colors, _errors = tracy_rgba_table(tables)
        self.assertGreater(colors[19][3], 0)
        self.assertLess(colors[19][3], 255)

    def test_flat_tracy_preview_matches_background0_anchor_when_composited(self):
        palette = tuple((i, i, i) for i in range(256))
        identity = bytes(range(256)) * 256
        tracy = bytearray(identity)
        for background in range(256):
            tracy[background * 256 + 23] = 64 if background == 0 else min(255, background + 10)
        tables = IndexedTables(palette, identity, bytes(tracy))
        colors, _errors = tracy_rgba_table(tables)
        r, g, b, a = colors[23]
        # Background 0 is black in the display palette, so source-over reduces
        # to the premultiplied foreground. The preview should stay anchored to
        # the exact visible Studio result there, modulo 8-bit quantisation in
        # the derived PNG table.
        def linear(channel):
            c = channel / 255.0
            return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
        reconstructed = round(255 * (a / 255.0) * linear(r))
        self.assertAlmostEqual(reconstructed, 64, delta=2)
        self.assertEqual((g, b), (r, r))

    def test_flat_tracy_true_identity_column_stays_transparent(self):
        palette = tuple((i, i, i) for i in range(256))
        identity = bytes(range(256)) * 256
        tracy = bytearray(identity)
        for background in range(256):
            tracy[background * 256 + 7] = background
        tables = IndexedTables(palette, identity, bytes(tracy))
        colors, _errors = tracy_rgba_table(tables)
        self.assertEqual(colors[7][3], 0)

    def test_tracy_projection_measures_nonrepresentable_lookup_error(self):
        palette = tuple((i, i, i) for i in range(256))
        identity = bytes(range(256)) * 256
        tables = IndexedTables(palette, identity, identity)
        colors, errors = tracy_rgba_table(tables)
        # Identity source columns are opaque and have no projection error.
        self.assertEqual(colors[37], (37, 37, 37, 255))
        self.assertAlmostEqual(errors[37], 0.0, places=12)

        raw = bytearray(identity)
        # A deliberately non-source-over column: alternating unrelated output
        # indices across backgrounds. The test checks measured residual rather
        # than accepting a black/transparent heuristic.
        for background in range(256):
            raw[background * 256 + 7] = (17 if background % 2 else 231)
        nonlinear = IndexedTables(palette, identity, bytes(raw))
        colors, errors = tracy_rgba_table(nonlinear)
        self.assertGreater(errors[7], 0.05)
        self.assertNotEqual(colors[7][3], 0)


if __name__ == "__main__":
    unittest.main()
