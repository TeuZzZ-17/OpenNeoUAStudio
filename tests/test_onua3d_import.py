from __future__ import annotations

import copy
import hashlib
import json
import struct
import unittest
from contextlib import contextmanager, nullcontext
from unittest.mock import MagicMock, patch
import zipfile

from asset_family import load_asset_family, rebuild_materials
from asset_family_package import MANIFEST_NAME, PackageEntry, validate_family_package, write_package_manifest
from base_parser import parse_base_bytes
from onua3d_export import MANIFEST, _Glb, build_scene, write_onua3d
from onua3d_identity import ATTRIBUTE, IDENTITY_CONTRACT, vertex_identity_mapping
from onua3d_import import Onua3dImportError, import_onua3d, validate_scene_positions
from sklt_parser import parse_sklt_file
from tests import test_onua3d_export as fixture
from tests.test_onua3d_identity_blender import reorder_scene
from tests.test_asset_family_package import _chunk, _form


def edit_glb(scene, edit):
    doc, binary = fixture.decode_glb(scene)
    glb = _Glb(); glb.doc = doc; glb.binary = bytearray(binary)
    edit(glb, binary)
    return glb.encode()


def move_ids(scene, selected, delta=(0.25, 0.5, -0.75)):
    def edit(glb, binary):
        for mesh in glb.doc['meshes']:
            for primitive in mesh['primitives']:
                attrs = primitive['attributes']
                ids = fixture.values(glb.doc, binary, attrs[ATTRIBUTE])
                positions = fixture.values(glb.doc, binary, attrs['POSITION'])
                attrs['POSITION'] = glb.accessor([
                    tuple(v+d for v,d in zip(p, delta)) if i in selected else p
                    for i,p in zip(ids, positions)], 3)
    return edit_glb(scene, edit)


def repackage(source, target, *, scene=None, manifest_edit=None, refresh_hash=True):
    with zipfile.ZipFile(source) as archive:
        members = {n: archive.read(n) for n in archive.namelist()}
    manifest = json.loads(members[MANIFEST])
    if scene is not None:
        members['scene.glb'] = scene
        if refresh_hash:
            manifest['files']['scene.glb'] = {'size': len(scene), 'sha256': hashlib.sha256(scene).hexdigest()}
    if manifest_edit:
        manifest_edit(manifest)
    members[MANIFEST] = json.dumps(manifest).encode()
    with zipfile.ZipFile(target, 'w') as archive:
        for name, data in members.items(): archive.writestr(name, data)


class Onua3dImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.Onua3dTests.setUpClass()

    def setUp(self):
        fixture.Onua3dTests.setUp(self)
        # Add a real SEN2 chunk to the valid package's existing skeleton.
        path = self.source / 'Skeleton/TEST.SKLT'
        data = path.read_bytes()
        sensor = _chunk(b'SEN2', struct.pack('>fff', 8, 9, 10))
        path.write_bytes(data[:4] + struct.pack('>I', len(data)-8+len(sensor)) + data[8:] + sensor)
        self.refresh_family()
        self.package = self.root / 'baseline.onua3d'
        write_onua3d(self.source, self.package)
        with zipfile.ZipFile(self.package) as archive:
            self.scene = archive.read('scene.glb')
            self.manifest = json.loads(archive.read(MANIFEST))

    def refresh_family(self):
        native = json.loads((self.source / MANIFEST_NAME).read_text())
        self.family = load_asset_family(self.source / 'TEST.BASE', isolated_root=self.source)
        write_package_manifest(self.source, 'TEST.BASE', self.family,
                               [PackageEntry(**row) for row in native['entries']])

    def imported(self, scene=None, *, manifest_edit=None):
        source = self.root / 'edited.onua3d'
        repackage(self.package, source, scene=scene, manifest_edit=manifest_edit)
        return import_onua3d(source, self.root / 'result')

    def assert_native_delta(self, result, deltas):
        for path in self.source.rglob('*'):
            if not path.is_file() or path.name == MANIFEST_NAME: continue
            relative = path.relative_to(self.source)
            expected = bytearray(path.read_bytes())
            if relative.as_posix() in deltas:
                model = parse_sklt_file(path)
                for point_id, point in deltas[relative.as_posix()].items():
                    struct.pack_into('>fff', expected, model.poo2_payload_offset + point_id*12, *point)
            self.assertEqual((result.output_root / relative).read_bytes(), expected, str(relative))
        validation = validate_family_package(result.output_root)
        self.assertTrue(validation.valid, validation.errors)

    def test_noop_copies_every_native_byte_including_manifest(self):
        before = fixture.source_hashes(self.source)
        result = self.imported()
        self.assertEqual(result.changed_points, 0)
        self.assertEqual(before, fixture.source_hashes(result.output_root))
        self.assertEqual(before, fixture.source_hashes(self.source))

    def test_single_vertex_inverse_coordinates_only_requested_poo2_bytes(self):
        result = self.imported(move_ids(self.scene, {1}))
        self.assertEqual(result.changed_points, 1)
        self.assertEqual(result.family.root_object.skeleton.sensors, self.family.root_object.skeleton.sensors)
        self.assert_native_delta(result, {'Skeleton/TEST.SKLT': {0: (0.25, -0.5, -0.75)}})

    def test_global_vertex_move_does_not_move_sen2(self):
        result = self.imported(move_ids(self.scene, {1, 2, 3}))
        self.assertEqual(result.changed_points, 3)
        self.assert_native_delta(result, {'Skeleton/TEST.SKLT': {
            i: (x+0.25, y-0.5, z-0.75) for i,(x,y,z) in enumerate(self.family.root_object.skeleton.points)}})

    def duplicated(self, conflicting=False):
        def edit(glb, binary):
            attrs = glb.doc['meshes'][0]['primitives'][0]['attributes']
            for name, accessor in list(attrs.items()):
                rows = fixture.values(glb.doc, binary, accessor)
                extra = rows[0]
                if name == 'POSITION' and conflicting: extra = (1, 2, 3)
                width = {'SCALAR': 1, 'VEC2': 2, 'VEC3': 3}[glb.doc['accessors'][accessor]['type']]
                attrs[name] = glb.accessor(rows + [extra], width)
        return edit_glb(self.scene, edit)

    def test_equal_duplicate_id_is_valid(self):
        self.assertEqual(self.imported(self.duplicated()).changed_points, 0)

    def test_conflicting_duplicate_id_fails_without_output(self):
        with self.assertRaisesRegex(Onua3dImportError, 'conflicting POSITION'):
            self.imported(self.duplicated(True))
        self.assertFalse((self.root / 'result').exists())

    def seam_scene(self):
        obj = self.family.root_object
        obj.skeleton.polygons.append([0, 2, 1])
        block = copy.deepcopy(obj.base_object.ades[0]); block.atts[0].poly_id = 1
        block.olpl[0] = [(32, 64), (128, 64), (64, 128)]
        obj.base_object.ades.append(block)
        rebuild_materials(obj, self.family)
        scene, _, nodes, _ = build_scene(self.family)
        manifest = {'vertex_identity': IDENTITY_CONTRACT, 'nodes': nodes}
        return scene, manifest

    def test_distinct_ids_for_same_sklt_point_accept_only_exact_agreement(self):
        scene, manifest = self.seam_scene()
        ids = {i for i,o in vertex_identity_mapping(manifest).items() if o['point_id'] == 0}
        self.assertEqual(len(ids), 2)
        moved = move_ids(scene, ids)
        self.assertEqual(validate_scene_positions(manifest, scene, moved)[('root', 0)], (0.25, -0.5, -0.75))
        for selected in ({min(ids)}, {max(ids)}):
            with self.assertRaisesRegex(Onua3dImportError, 'Conflicting POSITION for SKLT'):
                validate_scene_positions(manifest, scene, move_ids(scene, selected, (1e-8, 0, 0)))

    def test_node_primitive_vertex_reorder_and_uv_seam(self):
        scene, manifest = self.seam_scene()
        self.assertEqual(validate_scene_positions(manifest, scene, reorder_scene(scene)),
                         validate_scene_positions(manifest, scene, scene))

    def test_transform_threshold_finite_and_real_changes(self):
        for value, accepted in ((1e-6, True), (1.0001e-6, False), (0.25, False), (float('nan'), False)):
            def edit(glb, binary):
                glb.doc['nodes'][0]['translation'] = [value, 0, 0]
            if value != value:
                # Export encoder correctly rejects non-finite JSON too.
                with self.assertRaises(ValueError): edit_glb(self.scene, edit)
                continue
            changed = edit_glb(self.scene, edit)
            if accepted:
                self.assertEqual(validate_scene_positions(self.manifest, self.scene, changed)[('root', 0)], (0, 0, 0))
            else:
                with self.assertRaisesRegex(Onua3dImportError, 'Object Mode transform changed'):
                    validate_scene_positions(self.manifest, self.scene, changed)

    def test_topology_mapping_attribute_and_encoding_corruption_fail_closed(self):
        def topology(g, b): g.doc['meshes'][0]['primitives'][0]['indices'] = g.accessor([0, 2, 1], 1, indices=True)
        def missing(g, b): del g.doc['meshes'][0]['primitives'][0]['attributes'][ATTRIBUTE]
        def fraction(g, b):
            g.doc['meshes'][0]['primitives'][0]['attributes'][ATTRIBUTE] = g.accessor([1.5, 2, 3], 1)
        def sparse(g, b):
            i = g.doc['meshes'][0]['primitives'][0]['attributes'][ATTRIBUTE]; g.doc['accessors'][i]['sparse'] = {}
        def outside(g, b): g.doc['accessors'][0]['count'] = 999999
        def uv(g, b):
            g.doc['meshes'][0]['primitives'][0]['attributes']['TEXCOORD_0'] = g.accessor([(0.5, 0.5)]*3, 2)
        for mutate in (topology, missing, fraction, sparse, outside, uv):
            with self.subTest(mutate=mutate.__name__), self.assertRaises(Onua3dImportError):
                self.imported(edit_glb(self.scene, mutate))
            self.assertFalse((self.root / 'result').exists())

    def test_manifest_corruption_legacy_and_scene_hash_fail_closed(self):
        changes = [lambda m: m.pop('vertex_identity'), lambda m: m.update(version=2),
                   lambda m: m['nodes'][0]['vertex_instances']['1'].update(point_id=999),
                   lambda m: m['files']['scene.glb'].update(sha256='bad')]
        for mutate in changes:
            with self.subTest(mutate=mutate), self.assertRaises(Onua3dImportError):
                self.imported(manifest_edit=mutate)
        damaged = self.root / 'bad.onua3d'
        repackage(self.package, damaged, scene=move_ids(self.scene, {1}), refresh_hash=False)
        with self.assertRaisesRegex(Onua3dImportError, 'hash/size mismatch'):
            import_onua3d(damaged, self.root / 'result')
        with zipfile.ZipFile(damaged, 'w') as archive: archive.writestr('scene.glb', self.scene)
        with self.assertRaisesRegex(Onua3dImportError, 'Missing ONUA3D manifest'):
            import_onua3d(damaged, self.root / 'result')

    def test_existing_output_folder_is_protected(self):
        output = self.root / 'result'; output.mkdir()
        sentinel = output / 'mine.txt'; sentinel.write_text('keep')
        with self.assertRaisesRegex(Onua3dImportError, 'must be empty'): self.imported()
        self.assertEqual(sentinel.read_text(), 'keep')

    def test_unsafe_zip_member_is_refused_before_extraction(self):
        from asset_family_package import AssetFamilyPackageError
        source = self.root / 'unsafe.onua3d'
        for name in ('ua_family/../escape.sklt', 'ua_family/NUL.sklt', '/absolute.sklt'):
            with zipfile.ZipFile(source, 'w') as archive: archive.writestr(name, b'bad')
            with self.assertRaises(AssetFamilyPackageError): import_onua3d(source, self.root / 'result')
            self.assertFalse((self.root / 'result').exists())

    def test_structural_geometry_without_material_is_supported(self):
        self.family.root_object.base_object.ades.clear()
        rebuild_materials(self.family.root_object, self.family)
        scene, _, nodes, _ = build_scene(self.family)
        manifest = {'vertex_identity': IDENTITY_CONTRACT, 'nodes': nodes}
        self.assertEqual(validate_scene_positions(manifest, scene, scene)[('root', 0)], (0, 0, 0))

    def test_wrong_json_types_and_object_animation_are_rejected(self):
        def bad_node(g, b): g.doc['nodes'][0] = []
        def bad_mesh(g, b): g.doc['meshes'][0] = []
        def animated_object(g, b):
            g.doc['animations'] = [{'channels': [{'target': {'node': 0, 'path': 'translation'}}]}]
        for edit in (bad_node, bad_mesh, animated_object):
            with self.assertRaises(Onua3dImportError): self.imported(edit_glb(self.scene, edit))
        with self.assertRaises(Onua3dImportError):
            self.imported(manifest_edit=lambda m: m['files'].update({'scene.glb': []}))

    def test_ui_action_imports_through_core_and_protects_loaded_family_on_error(self):
        window = fixture.Onua3dTests.window(self)
        self.assertIn(window.import_onua3d_action, window.file_import_menu.actions())
        destination = self.root / 'ui_result'
        destination.mkdir()
        with patch('assembly_window.QFileDialog.getOpenFileName', return_value=(str(self.package), '')), \
                patch('assembly_window.QFileDialog.getExistingDirectory', return_value=str(destination)), \
                patch.object(window, '_confirm_discard_geometry', return_value=True), \
                patch('assembly_window.QMessageBox.critical') as errors:
            window.import_onua3d_action.trigger()
            errors.assert_not_called()
            loaded = window._family
            self.assertEqual(loaded.base_path.parent, destination)
            # Existing output is refused; the successfully loaded document remains.
            window.import_onua3d_action.trigger()
            errors.assert_called_once()
            self.assertIs(window._family, loaded)

    def test_ui_discard_cancel_runs_prepare_but_never_materializes(self):
        window = fixture.Onua3dTests.window(self)
        with patch('assembly_window.QFileDialog.getOpenFileName', return_value=(str(self.package), '')), \
                patch.object(window, '_confirm_discard_geometry', return_value=False), \
                patch('assembly_window.QFileDialog.getExistingDirectory') as destination, \
                patch('onua3d_import.prepare_onua3d') as prepare:
            prepared = MagicMock()
            prepare.return_value = nullcontext(prepared)
            window.import_onua3d_action.trigger()
            prepare.assert_called_once()
            prepared.materialize.assert_not_called()
            destination.assert_not_called()
            self.assertIs(window._family, self.family)

    def test_ui_prepare_finishes_before_destination_dialog(self):
        window = fixture.Onua3dTests.window(self)
        destination = self.root / 'ordered'
        destination.mkdir()
        events = []
        from onua3d_import import prepare_onua3d as real_prepare

        @contextmanager
        def tracked_prepare(source):
            with real_prepare(source) as prepared:
                events.append('prepared')
                yield prepared

        def destination_dialog(*_args, **_kwargs):
            events.append('destination-dialog')
            return str(destination)

        with patch('assembly_window.QFileDialog.getOpenFileName', return_value=(str(self.package), '')), \
                patch('assembly_window.QFileDialog.getExistingDirectory', side_effect=destination_dialog), \
                patch.object(window, '_confirm_discard_geometry', return_value=True), \
                patch('onua3d_import.prepare_onua3d', side_effect=tracked_prepare), \
                patch('assembly_window.QMessageBox.critical') as errors:
            window.import_onua3d_action.trigger()
            errors.assert_not_called()
        self.assertEqual(events[:2], ['prepared', 'destination-dialog'])
        self.assertEqual(window._family.base_path.parent, destination)

    def test_ui_invalid_package_never_opens_destination_dialog(self):
        window = fixture.Onua3dTests.window(self)
        with patch('assembly_window.QFileDialog.getOpenFileName', return_value=(str(self.package), '')), \
                patch('assembly_window.QFileDialog.getExistingDirectory') as destination, \
                patch('onua3d_import.prepare_onua3d', side_effect=Onua3dImportError('invalid package')), \
                patch('assembly_window.QMessageBox.critical') as errors:
            window.import_onua3d_action.trigger()
            destination.assert_not_called()
            errors.assert_called_once()
        self.assertIs(window._family, self.family)

    def test_ui_destination_cancel_preserves_loaded_document(self):
        window = fixture.Onua3dTests.window(self)
        prepared = MagicMock()
        with patch('assembly_window.QFileDialog.getOpenFileName', return_value=(str(self.package), '')), \
                patch('assembly_window.QFileDialog.getExistingDirectory', return_value=''), \
                patch.object(window, '_confirm_discard_geometry', return_value=True), \
                patch('onua3d_import.prepare_onua3d', return_value=nullcontext(prepared)):
            window.import_onua3d_action.trigger()
        prepared.materialize.assert_not_called()
        self.assertIs(window._family, self.family)

    def test_native_kids_shared_sklt(self):
        data = (self.source / 'TEST.BASE').read_bytes()
        parsed = parse_base_bytes(data)
        base = next(c for c in parsed.tree.roots[0].children[0].children if c.form_type == 'BASE')
        child_objt = parsed.tree.roots[0].children[0]
        kid_bytes = data[child_objt.offset:child_objt.offset+8+child_objt.size]
        base_children = data[base.payload_offset+4:base.payload_offset+base.size]
        # Build a fixture with a native KIDS record, using existing IFF test helpers.
        encoded = _form(b'MC2 ', _form(b'OBJT', _chunk(b'CLID', b'base.class\0') +
                        _form(b'BASE', base_children + _form(b'KIDS', kid_bytes))))
        (self.source / 'TEST.BASE').write_bytes(encoded)
        self.refresh_family()
        self.assertEqual(len(self.family.all_objects()), 2)
        write_onua3d(self.source, self.package)
        with zipfile.ZipFile(self.package) as archive:
            scene = archive.read('scene.glb'); manifest = json.loads(archive.read(MANIFEST))
        ids = {i for i,o in vertex_identity_mapping(manifest).items() if o['point_id'] == 0}
        with self.assertRaisesRegex(Onua3dImportError, 'shared native SKLT'):
            self.imported(move_ids(scene, {min(ids)}))
        self.assertFalse((self.root / 'result').exists())
        # Different owners share the physical SKLT: agreeing updates write it once.
        result = self.imported(reorder_scene(move_ids(scene, ids)))
        self.assertEqual(result.changed_points, 1)
        self.assert_native_delta(result, {'Skeleton/TEST.SKLT': {0: (0.25, -0.5, -0.75)}})


if __name__ == '__main__':
    unittest.main()
