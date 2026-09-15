import copy
import hashlib
import json
import unittest
from unittest.mock import patch
import zipfile

from onua3d_export import MANIFEST, write_onua3d
from onua3d_package import read_onua3d, repack_onua3d, Onua3dImportError
from tests import test_onua3d_export as fixture
from tests.test_onua3d_import import move_ids, edit_glb
from tools.build_blender_onua3d import build_addon


class Onua3dPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): fixture.Onua3dTests.setUpClass()

    def setUp(self):
        fixture.Onua3dTests.setUp(self)
        self.source_package = self.root/'source.onua3d'
        write_onua3d(self.source, self.source_package)
        self.original = self.source_package.read_bytes()
        self.sha = hashlib.sha256(self.original).hexdigest()
        self.manifest, self.members = read_onua3d(self.source_package)
        self.target = self.root/'new.onua3d'

    def test_repack_changes_only_scene_and_its_manifest_hash_and_size(self):
        scene = move_ids(self.members['scene.glb'], {1})
        repack_onua3d(self.source_package, scene, self.target, source_sha256=self.sha)
        manifest, members = read_onua3d(self.target)
        expected = copy.deepcopy(self.manifest)
        expected['files']['scene.glb'].update(size=len(scene), sha256=hashlib.sha256(scene).hexdigest())
        self.assertEqual(manifest, expected)
        self.assertEqual(members, {**self.members, 'scene.glb': scene})
        self.assertEqual(self.source_package.read_bytes(), self.original)

    def test_source_missing_changed_and_existing_output_refused(self):
        for source, sha in ((self.root/'missing.onua3d', self.sha), (self.source_package, 'wrong')):
            with self.assertRaises(Onua3dImportError):
                repack_onua3d(source, self.members['scene.glb'], self.target, source_sha256=sha)
            self.assertFalse(self.target.exists())
        with self.assertRaises(Onua3dImportError):
            repack_onua3d(self.source_package, self.members['scene.glb'], self.source_package, source_sha256=self.sha)
        self.assertEqual(self.source_package.read_bytes(), self.original)

    def test_invalid_scene_is_refused_before_creating_output(self):
        def missing_id(scene, binary):
            del scene.doc['meshes'][0]['primitives'][0]['attributes']['_ONUA3D_VERTEX_ID']
        def changed_transform(scene, binary):
            scene.doc['nodes'][0].pop('matrix', None)
            scene.doc['nodes'][0]['translation'] = [1, 0, 0]
        def missing_primitive(scene, binary):
            scene.doc['meshes'][0]['primitives'].clear()
        for change in (missing_id, changed_transform, missing_primitive):
            with self.assertRaises((Onua3dImportError, ValueError)):
                repack_onua3d(self.source_package, edit_glb(self.members['scene.glb'], change),
                              self.target, source_sha256=self.sha)
            self.assertFalse(self.target.exists())
        self.assertEqual(self.source_package.read_bytes(), self.original)

    def test_corrupt_source_or_missing_capability_is_refused(self):
        for change in ('hash', 'mapping', 'native'):
            members = dict(self.members)
            manifest = copy.deepcopy(self.manifest)
            if change == 'hash': members['scene.glb'] += b'bad'
            if change == 'mapping': del manifest['vertex_identity']
            if change == 'native':
                native_name = next(n for n in members if n.endswith('.SKLT'))
                members[native_name] = b'bad'
            with zipfile.ZipFile(self.target, 'w') as archive:
                for name, data in members.items(): archive.writestr(name, data)
                archive.writestr(MANIFEST, json.dumps(manifest))
            with self.assertRaises((Onua3dImportError, ValueError)): read_onua3d(self.target)

    def test_final_validation_failure_leaves_no_partial_package(self):
        def validate(path):
            if path == self.target: raise Onua3dImportError('injected readback failure')
            return read_onua3d(path)
        with patch('onua3d_package.read_onua3d', side_effect=validate):
            with self.assertRaisesRegex(OSError, 'injected readback failure'):
                repack_onua3d(self.source_package, self.members['scene.glb'], self.target, source_sha256=self.sha)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.source_package.read_bytes(), self.original)

    def test_addon_bundle_is_deterministic_and_contains_no_native_writer(self):
        output = self.root/'addon.zip'
        build_addon(output); first = output.read_bytes()
        build_addon(output); self.assertEqual(output.read_bytes(), first)
        with zipfile.ZipFile(output) as archive:
            self.assertNotIn('openneoua_onua3d/sklt_parser.py', archive.namelist())
            self.assertNotIn('openneoua_onua3d/onua3d_import.py', archive.namelist())
            for name in archive.namelist():
                code = archive.read(name).decode('utf-8')
                compile(code, name, 'exec')
                self.assertNotIn('PySide6', code)


if __name__ == '__main__': unittest.main()
