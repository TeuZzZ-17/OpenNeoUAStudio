"""Real Blender -> native-family integration, opt-in via the existing env fixtures."""
import hashlib
import json
import os
from pathlib import Path
import struct
import unittest
from unittest.mock import patch
import zipfile

from assembly_window import AssemblyWindow
from onua3d_export import MANIFEST, write_onua3d
from onua3d_identity import vertex_identity_mapping
from onua3d_import import Onua3dImportError, import_onua3d
from sklt_parser import parse_sklt_file
from tests import test_onua3d_export as fixtures
from tests import test_onua3d_identity_blender as blender_fixture
from tests import test_onua3d_import as import_fixture
from tests.test_onua3d_identity_blender import reorder_scene, triangles_and_copies
from tests.test_onua3d_import import repackage


class BlenderImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.blender = os.environ.get('ONUA3D_BLENDER', '')
        if not Path(cls.blender).is_file():
            raise unittest.SkipTest('set ONUA3D_BLENDER to a real Blender executable')
        fixtures.Onua3dTests.setUpClass()

    def setUp(self):
        import_fixture.Onua3dImportTests.setUp(self)

    refresh_family = import_fixture.Onua3dImportTests.refresh_family
    _blender = blender_fixture.BlenderIdentityTests._blender

    def verify_roundtrip(self, package, label, *, require_normal_split=False):
        with zipfile.ZipFile(package) as archive:
            manifest = json.loads(archive.read(MANIFEST))
            scene = archive.read('scene.glb')
            native = {n[10:]: archive.read(n) for n in archive.namelist() if n.startswith('ua_family/')}
        source = self.root / (label + '.glb'); source.write_bytes(scene)
        noop = self._blender(source, self.root / (label + '_noop.glb'), [])
        repacked = self.root / (label + '_noop.onua3d')
        repackage(package, repacked, scene=noop)
        output = self.root / (label + '_noop_family')
        result = import_onua3d(repacked, output)
        self.assertEqual(result.changed_points, 0)
        self.assertEqual({p.relative_to(output).as_posix(): p.read_bytes()
                          for p in output.rglob('*') if p.is_file()}, native)
        origins = vertex_identity_mapping(manifest)
        _, copies, normals = triangles_and_copies(noop)
        split = [i for i, count in copies.items() if count > 1 and len(normals.get(i, ())) > 1]
        if require_normal_split: self.assertTrue(split)
        chosen = origins[split[0] if split else min(origins)]
        ids = {i for i, o in origins.items()
               if (o['owner_path'], o['point_id']) == (chosen['owner_path'], chosen['point_id'])}
        moved = self._blender(source, self.root / (label + '_move.glb'), sorted(ids))
        # Reordering after Blender exercises the actual import mapping as well.
        repackage(package, repacked, scene=reorder_scene(moved))
        edited = import_onua3d(repacked, self.root / (label + '_edited_family'))
        self.assertEqual(edited.changed_points, 1)
        owner = next(o for o in result.family.all_objects() if o.owner_path == chosen['owner_path'])
        relative = owner.skeleton_ref.path.relative_to(output).as_posix()
        relative = next(name for name in native if name.casefold() == relative.casefold())
        model = owner.skeleton
        point_id = chosen['point_id']
        x,y,z = model.points[point_id]
        expected_point = (x+0.25, y, z)
        expected = dict(native)
        data = bytearray(expected[relative])
        struct.pack_into('>fff', data, model.poo2_payload_offset+point_id*12, *expected_point)
        expected[relative] = bytes(data)
        for name, data in expected.items():
            if name != 'asset_family_manifest.json':
                self.assertEqual((edited.output_root / name).read_bytes(), data, name)
        self.assertEqual(parse_sklt_file(edited.output_root / relative).sensors, model.sensors)
        object_moved = self._blender(source, self.root / (label + '_object_move.glb'), [], object_move=True)
        repackage(package, repacked, scene=object_moved)
        with self.assertRaisesRegex(Onua3dImportError, 'Object Mode transform changed'):
            import_onua3d(repacked, self.root / (label + '_object_rejected'))
        # Moving just one seam/frame ID must fail the native point grouping.
        if len(ids) > 1:
            partial = self._blender(source, self.root / (label + '_partial.glb'), [min(ids)])
            repackage(package, repacked, scene=partial)
            with self.assertRaisesRegex(Onua3dImportError, 'Conflicting POSITION for SKLT'):
                import_onua3d(repacked, self.root / (label + '_rejected'))
        print(f'{label}: Blender no-op byte-identical; Edit Mode point {point_id}, '
              f'{len(ids)} logical IDs, {len(split)} normal-split IDs; native POO2-only PASS; Object Mode rejected')

    def test_synthetic_nontrivial_native_transform_noop_and_edit(self):
        # STRC layout comes from the existing BASE parser/writer contract.
        from base_parser import parse_base_bytes
        path = self.source / 'TEST.BASE'
        data = bytearray(path.read_bytes())
        parsed = parse_base_bytes(data)
        def visit(chunk):
            if chunk.form_type == 'BASE':
                strc = next(c for c in chunk.children if c.tag == 'STRC')
                struct.pack_into('>fff', data, strc.payload_offset+2, 1.25, 2.5, -3.75)
                struct.pack_into('>fff', data, strc.payload_offset+26, 1.5, 0.75, 2.25)
                struct.pack_into('>hhh', data, strc.payload_offset+38, 1234, 3456, 5678)
            for child in chunk.children: visit(child)
        for root in parsed.tree.roots: visit(root)
        path.write_bytes(data)
        self.refresh_family()
        write_onua3d(self.source, self.package)
        self.verify_roundtrip(self.package, 'synthetic_rotated')

    def test_retail_vp_hubi2(self):
        source = Path(os.environ.get('ONUA3D_HUBI2_PACKAGE', ''))
        if not source.is_file(): self.skipTest('set ONUA3D_HUBI2_PACKAGE')
        before = source.read_bytes()
        with zipfile.ZipFile(source) as archive: archive.extractall(self.root / 'legacy')
        package = self.root / 'VP_HUBI2.onua3d'
        write_onua3d(self.root / 'legacy/ua_family', package)
        self.verify_roundtrip(package, 'VP_HUBI2', require_normal_split=True)
        self.assertEqual(source.read_bytes(), before)

    def test_retail_vp_hubi4(self):
        source = Path(os.environ.get('ONUA3D_RETAIL_ARCHIVE', ''))
        if not source.is_file(): self.skipTest('set ONUA3D_RETAIL_ARCHIVE')
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        window = AssemblyWindow()
        try:
            window.open_setbas(str(source))
            self.assertIsNotNone(window._activate_setbas_base('VP_HUBI4.base'))
            package = self.root / 'VP_HUBI4.onua3d'
            with patch('assembly_window.QFileDialog.getSaveFileName', return_value=(str(package), '')), \
                    patch('assembly_window.QMessageBox.critical') as errors:
                window._export_to_blender(); errors.assert_not_called()
            self.verify_roundtrip(package, 'VP_HUBI4', require_normal_split=True)
        finally:
            window.close(); window.deleteLater()
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)


if __name__ == '__main__':
    unittest.main()
