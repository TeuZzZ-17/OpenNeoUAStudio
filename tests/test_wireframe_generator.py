import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import numpy as np
from PySide6.QtCore import QPointF, QSize
from PySide6.QtWidgets import QApplication
from studio.rendering.asset_viewport import AssetViewport, ViewFace, ViewMaterial
from entity_designer.wireframe_detail import DEFAULT_DETAIL, DETAIL_LEVELS, detail_settings
from entity_designer.wireframe_generator import generate_wireframe, _mask_contours, _simplify, _smooth_mask, _visvalingam, _convex_loop
from studio.formats.sklt import parse_sklt_file


def quad(x0, x1, y0=-1, y1=1, z=0, poly_id=0, owner='root'):
    return ViewFace([(x1,y1,z),(x1,y0,z),(x0,y0,z),(x0,y1,z)], [], 0,
                    poly_id=poly_id, owner=owner)


class WireframeGeneratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def viewport(self, faces):
        view = AssetViewport()
        self.addCleanup(view.close)
        view._faces = faces
        view._materials = [ViewMaterial('geometry')]
        camera = view._camera_state()
        camera.update(center=(0,0,0), scale=1, yaw=0, pitch=0, zoom=0.8,
                      pan=QPointF(0,0))
        view._set_camera_state(camera)
        return view

    def test_source_quad_has_no_fan_diagonal_and_export_roundtrips(self):
        view = self.viewport([quad(-1,1)])
        output = generate_wireframe(view, respect_transparency=False)
        self.assertEqual(len(output.segments), 4)
        self.assertTrue(all(p[1] == 0 for p in output.points))
        self.assertAlmostEqual(max(p[0] for p in output.points)-min(p[0] for p in output.points),2000)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'quad.SKL'
            output.save(path)
            model = parse_sklt_file(path)
            self.assertEqual(model.warnings, [])
            self.assertEqual(model.polygons, output.segments)
            self.assertEqual(model.rendered_polygon_line_count, 4)
        self.assertFalse(output.preview_image(QSize(128,128)).isNull())

    def test_assembly_instances_with_repeated_polyids_remain_distinct(self):
        view = self.viewport([quad(-1.2,-0.2,y0=-0.5,y1=0.5,poly_id=7,owner='left'),
                              quad(0.2,1.2,y0=-0.5,y1=0.5,poly_id=7,owner='right')])
        output = generate_wireframe(view, respect_transparency=False)
        self.assertEqual(len(output.segments), 8)

    def test_hidden_rear_quad_contributes_no_lines(self):
        view = self.viewport([quad(-0.5,0.5,y0=-0.5,y1=0.5,z=-1),
                              quad(-1,1,z=0,poly_id=1)])
        output = generate_wireframe(view, respect_transparency=False)
        self.assertEqual(len(output.segments), 4)

    def test_seam_inside_one_flat_surface_is_not_drawn(self):
        # Two coplanar quads (a material seam) look like one rectangle.
        view = self.viewport([quad(-1,0),quad(0,1,poly_id=1)])
        output = generate_wireframe(view, respect_transparency=False)
        self.assertEqual(len(output.segments), 4)
        self.assertEqual(len({tuple(sorted(p)) for p in output.segments}), 4)

    def test_fold_between_two_faces_keeps_its_line(self):
        folded = ViewFace([(1,1,0.8),(1,-1,0.8),(0,-1,0),(0,1,0)], [], 0, poly_id=1)
        view = self.viewport([quad(-1,0), folded])
        output = generate_wireframe(view, respect_transparency=False)
        xs = [p[0] for p in output.points]
        middle = [seg for seg in output.segments
                  if all(abs(output.points[i][0] - (min(xs)+max(xs))/2) < 400 for i in seg)
                  and abs(output.points[seg[0]][2] - output.points[seg[1]][2]) > 1000]
        self.assertTrue(middle, "the fold edge must stay")

    def test_preset_does_not_change_parent_camera_pose_or_diagnostics(self):
        view = self.viewport([quad(-1,1)])
        before_camera = view._camera_state()
        before_faces = [tuple(f.vertices) for f in view._faces]
        view._last_effective_renderer = 'existing'
        view._last_indexed_stats = {'existing':1}
        generate_wireframe(view,'Isometric Front Right',respect_transparency=False)
        self.assertEqual(view._camera_state(), before_camera)
        self.assertEqual([tuple(f.vertices) for f in view._faces], before_faces)
        self.assertEqual(view._last_effective_renderer, 'existing')
        self.assertEqual(view._last_indexed_stats, {'existing':1})

    def test_missing_textures_are_explicit_and_geometry_still_works(self):
        view = self.viewport([quad(-1,1)])
        with self.assertRaisesRegex(ValueError,'requires resolved SET'):
            generate_wireframe(view,respect_transparency=True)
        self.assertEqual(len(generate_wireframe(view,respect_transparency=False).segments),4)

    def test_backfacing_scene_is_not_exported(self):
        face = quad(-1,1)
        face.vertices.reverse()
        view = self.viewport([face])
        with self.assertRaisesRegex(ValueError,'no visible faces'):
            generate_wireframe(view,respect_transparency=False)

    def test_near_plane_clip_produces_finite_exportable_lines(self):
        view = self.viewport([ViewFace([(0.1,0.1,3.9),(0.1,-0.1,3.9),
                                      (-0.1,-0.1,3.7),(-0.1,0.1,3.7)],[],0)])
        output = generate_wireframe(view,respect_transparency=False)
        self.assertGreater(len(output.segments),0)
        self.assertTrue(all(np.isfinite(p).all() for p in output.points))

    def test_contours_keep_holes_and_disconnected_islands(self):
        mask = np.zeros((30,30), dtype=bool)
        mask[2:15,2:15] = True
        mask[5:10,5:10] = False
        mask[20:26,20:26] = True
        loops = list(_mask_contours(mask))
        self.assertEqual(len(loops),3)
        self.assertTrue(all(p[0] == p[-1] for p in loops))

    def test_contour_simplification_handles_long_staircase(self):
        line = [(i, i%2) for i in range(20000)]
        self.assertEqual(_simplify(line,3),[line[0],line[-1]])


    def test_smoothing_removes_specks_and_ragged_edges_but_keeps_the_shape(self):
        mask = np.zeros((80,80), dtype=bool)
        mask[20:60,10:70] = True
        mask[18:20,30:32] = True            # a speck on the edge
        mask[5,5] = True                    # an isolated pixel
        mask[20:60:2,69] = False            # a ragged right edge
        smooth = _smooth_mask(mask, 3)
        self.assertFalse(smooth[5,5] or smooth[18,30:32].any())
        self.assertTrue(smooth[25:55,15:65].all())
        loops = list(_mask_contours(smooth))
        self.assertEqual(len(loops),1)
        self.assertLessEqual(len(_simplify(loops[0],3.0)),12)

    def test_smoothing_keeps_shapes_that_touch_the_image_border(self):
        mask = np.zeros((40,40), dtype=bool)
        mask[:,:20] = True
        self.assertTrue(_smooth_mask(mask,3)[:,:17].all())

    def test_corner_removal_keeps_real_corners_and_drops_small_teeth(self):
        # A saw-tooth along a straight edge with a real 90-degree corner at the end.
        line = [(0, 0)]
        for x in range(10, 200, 10):
            line.append((x, 3 if x % 20 else -3))
        line += [(200, 0), (200, 120)]
        simple = _visvalingam(line, 400.0)
        self.assertEqual(simple[0], (0, 0))
        self.assertEqual(simple[-1], (200, 120))
        self.assertIn((200, 0), simple)
        self.assertLessEqual(len(simple), 4)
        self.assertEqual(_visvalingam(line, 0.0), line)

    def test_a_sprite_outline_is_a_simple_convex_polygon(self):
        mask = np.zeros((60,120), dtype=bool)
        mask[10:50,10:110] = True
        mask[10:50:3,109] = False           # ragged right edge
        loop = _convex_loop(mask)
        self.assertEqual(loop[0], loop[-1])
        xs = [p[0] for p in loop]
        ys = [p[1] for p in loop]
        self.assertEqual((min(xs), max(xs), min(ys), max(ys)), (10, 110, 10, 50))
        self.assertIsNone(_convex_loop(np.zeros((10,10), dtype=bool)))

    def test_detail_level_zero_keeps_the_flat_seam_that_the_default_removes(self):
        view = self.viewport([quad(-1,0),quad(0,1,poly_id=1)])
        raw = generate_wireframe(view, respect_transparency=False, detail=0)
        default = generate_wireframe(view, respect_transparency=False)
        self.assertEqual(len(raw.segments), 7)
        self.assertEqual(len(default.segments), 4)
        explicit = generate_wireframe(view, respect_transparency=False, detail=DEFAULT_DETAIL)
        self.assertEqual((explicit.points, explicit.segments), (default.points, default.segments))

    def test_every_detail_level_works_and_a_bad_level_is_refused(self):
        view = self.viewport([quad(-1,0),quad(0,1,poly_id=1)])
        for level in range(len(DETAIL_LEVELS)):
            self.assertTrue(generate_wireframe(view, respect_transparency=False, detail=level).segments)
        for between in (0.5, 1.5, 2.9, 3.4):
            self.assertTrue(generate_wireframe(view, respect_transparency=False, detail=between).segments)
        for bad in (-1, len(DETAIL_LEVELS), 4.01, float("nan"), "2", True):
            with self.assertRaises(ValueError):
                generate_wireframe(view, respect_transparency=False, detail=bad)

    def test_a_detail_between_two_levels_blends_them(self):
        low, high = DETAIL_LEVELS[2], DETAIL_LEVELS[3]
        mid = detail_settings(2.5)
        self.assertAlmostEqual(mid.contour_fraction, (low.contour_fraction+high.contour_fraction)/2)
        self.assertAlmostEqual(mid.tooth_area, (low.tooth_area+high.tooth_area)/2)
        self.assertEqual(detail_settings(3), DETAIL_LEVELS[3])
        self.assertEqual(detail_settings(0), DETAIL_LEVELS[0])


if __name__ == '__main__':
    unittest.main()
