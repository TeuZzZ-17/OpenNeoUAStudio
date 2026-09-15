"""Installed Blender add-on -> complete native family -> normal Studio viewer."""
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import unittest
from unittest.mock import patch
import zipfile

from assembly_window import AssemblyWindow
from asset_family_package import validate_family_package
from onua3d_export import write_onua3d
from onua3d_import import import_onua3d
from onua3d_package import read_onua3d
from tests import test_onua3d_export as fixtures
from tools.build_blender_onua3d import build_addon


class Onua3dWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.blender = os.environ.get('ONUA3D_BLENDER', '')
        if not Path(cls.blender).is_file():
            raise unittest.SkipTest('set ONUA3D_BLENDER to a real Blender executable')
        fixtures.Onua3dTests.setUpClass()

    def setUp(self):
        fixtures.Onua3dTests.setUp(self)

    def verify_workflow(self, package, label, *, retail=False):
        original = package.read_bytes()
        manifest, members = read_onua3d(package)
        native = {n[10:]: data for n, data in members.items() if n.startswith('ua_family/')}
        addon = self.root/'addon.zip'
        build_addon(addon)
        output = self.root/label
        scripts, config = output/'scripts', output/'config'
        scripts.mkdir(parents=True); config.mkdir()
        job = output/'job.json'
        job.write_text(json.dumps(dict(source=str(package), addon=str(addon), output=str(output))))
        process = subprocess.run([self.blender, '--background', '--factory-startup',
                                  '--python-exit-code', '1', '--python',
                                  str(Path(__file__).with_name('blender_onua3d_workflow.py')),
                                  '--', str(job)], capture_output=True, text=True,
                                 env={**os.environ, 'BLENDER_USER_SCRIPTS': str(scripts),
                                      'BLENDER_USER_CONFIG': str(config)}, timeout=240)
        log = process.stdout + process.stderr
        (output/'blender.log').write_text(log, encoding='utf-8')
        self.assertEqual(process.returncode, 0, log[-12000:])
        self.assertIn('ONUA3D_ADDON_WORKFLOW_PASS', log)
        result = json.loads((output/'result.json').read_text())
        if retail: self.assertGreater(result['split_ids'], 0)
        self.assertTrue(result['blend_source_persisted'])
        self.assertTrue(result['missing_source_rejected'])
        self.assertTrue(result['corrupt_source_rejected'])
        for kind in ('noop', 'edited'):
            updated, contents = read_onua3d(result[kind])
            self.assertNotEqual(contents['scene.glb'], members['scene.glb'])
            expected_manifest = json.loads(json.dumps(manifest))
            expected_manifest['files']['scene.glb'] = dict(size=len(contents['scene.glb']),
                sha256=hashlib.sha256(contents['scene.glb']).hexdigest())
            self.assertEqual(updated, expected_manifest)
            self.assertEqual({n:d for n,d in contents.items() if n != 'scene.glb'},
                             {n:d for n,d in members.items() if n != 'scene.glb'})
        noop = import_onua3d(result['noop'], output/'native_noop')
        self.assertEqual(noop.changed_points, 0)
        self.assertEqual({p.relative_to(noop.output_root).as_posix():p.read_bytes()
                          for p in noop.output_root.rglob('*') if p.is_file()}, native)
        # Use the real UI handler and existing normal viewer, only replace dialogs.
        destination = output/'native_edited'; destination.mkdir()
        window = AssemblyWindow()
        try:
            with patch('assembly_window.QFileDialog.getOpenFileName', return_value=(result['edited'], '')), \
                 patch('assembly_window.QFileDialog.getExistingDirectory', return_value=str(destination)), \
                 patch('assembly_window.QMessageBox.critical') as errors:
                window._import_onua3d()
                errors.assert_not_called()
            self.assertIsNotNone(window._family)
            self.assertIs(window.viewport._family_ref, window._family)
            self.assertEqual(window._family.base_path.parent, destination)
            self.assertEqual(window._last_directory, destination)
            verified = validate_family_package(destination)
            self.assertTrue(verified.valid, verified.errors)
            owner = next(o for o in noop.family.all_objects() if o.owner_path == result['owner'])
            relative = owner.skeleton_ref.path.relative_to(noop.output_root).as_posix()
            relative = next(n for n in native if n.casefold() == relative.casefold())
            expected = bytearray(native[relative]); point = result['point_id']
            x,y,z = owner.skeleton.points[point]
            struct.pack_into('>fff', expected, owner.skeleton.poo2_payload_offset + point*12, x+.25,y,z)
            for name, data in native.items():
                if name == 'asset_family_manifest.json': continue
                self.assertEqual((destination/name).read_bytes(), bytes(expected) if name == relative else data, name)
            self.assertEqual(next(o for o in window._family.all_objects() if o.owner_path == result['owner']).skeleton.sensors,
                             owner.skeleton.sensors)
        finally:
            window.close(); window.deleteLater()
        self.assertEqual(package.read_bytes(), original)
        print(f'{label}: installed add-on no-op/Edit Mode PASS; {len(native)} native members, '
              f'{result["split_ids"]} duplicate IDs; only point {result["point_id"]} POO2 changed; '
              'source persists across .blend reopen; Studio destination and auto-open PASS')

    def test_synthetic(self):
        package = self.root/'synthetic.onua3d'
        write_onua3d(self.source, package)
        self.verify_workflow(package, 'synthetic')

    def test_retail_hubi2(self):
        source = Path(os.environ.get('ONUA3D_HUBI2_PACKAGE', ''))
        if not source.is_file(): self.skipTest('set ONUA3D_HUBI2_PACKAGE')
        before = hashlib.sha256(source.read_bytes()).digest()
        with zipfile.ZipFile(source) as archive: archive.extractall(self.root/'retail')
        package = self.root/'VP_HUBI2.onua3d'
        write_onua3d(self.root/'retail/ua_family', package)
        self.verify_workflow(package, 'VP_HUBI2', retail=True)
        self.assertEqual(hashlib.sha256(source.read_bytes()).digest(), before)

    def test_retail_hubi4(self):
        source = Path(os.environ.get('ONUA3D_RETAIL_ARCHIVE', ''))
        if not source.is_file(): self.skipTest('set ONUA3D_RETAIL_ARCHIVE')
        before = hashlib.sha256(source.read_bytes()).digest()
        window = AssemblyWindow()
        try:
            window.open_setbas(str(source))
            self.assertIsNotNone(window._activate_setbas_base('VP_HUBI4.base'))
            package = self.root/'VP_HUBI4.onua3d'
            with patch('assembly_window.QFileDialog.getSaveFileName', return_value=(str(package), '')), \
                 patch('assembly_window.QMessageBox.critical') as errors:
                window._export_to_blender(); errors.assert_not_called()
            self.verify_workflow(package, 'VP_HUBI4', retail=True)
        finally:
            window.close(); window.deleteLater()
        self.assertEqual(hashlib.sha256(source.read_bytes()).digest(), before)


if __name__ == '__main__': unittest.main()
