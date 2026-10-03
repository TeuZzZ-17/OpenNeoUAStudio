import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import numpy as np
from PySide6.QtCore import QPointF, QSize
from PySide6.QtWidgets import QApplication
from assembly_viewer import AssetViewport, ViewFace, ViewMaterial
from model_editor.wireframe_generator import generate_wireframe, _mask_contours, _simplify
from sklt_parser import parse_sklt_file


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

    def test_material_seam_edges_are_deduplicated(self):
        view = self.viewport([quad(-1,0),quad(0,1,poly_id=1)])
        output = generate_wireframe(view, respect_transparency=False)
        self.assertEqual(len(output.segments), 7)
        self.assertEqual(len({tuple(sorted(p)) for p in output.segments}), 7)

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


if __name__ == '__main__':
    unittest.main()
