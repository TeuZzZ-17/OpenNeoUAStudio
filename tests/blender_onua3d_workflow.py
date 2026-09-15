"""Exercise the installed add-on in an isolated Blender user configuration."""
import hashlib
import json
from pathlib import Path
import sys
from collections import Counter

import bpy
import bmesh

job = json.loads(Path(sys.argv[-1]).read_text(encoding='utf-8'))
root = Path(job['output']).resolve()
root.mkdir(parents=True, exist_ok=True)
scripts = Path(bpy.utils.user_resource('SCRIPTS')).resolve()
assert scripts.is_relative_to(root), scripts
assert bpy.ops.preferences.addon_install(filepath=job['addon']) == {'FINISHED'}
assert bpy.ops.preferences.addon_enable(module='openneoua_onua3d') == {'FINISHED'}
from openneoua_onua3d.onua3d_package import read_onua3d, _Scene
from openneoua_onua3d.onua3d_identity import vertex_identity_mapping
from openneoua_onua3d import export_onua3d

original_scene = bpy.context.scene
original_objects = set(original_scene.objects)
try:
    export_onua3d(bpy.context, root/'unassociated.onua3d')
except RuntimeError as error:
    assert 'association' in str(error)
else: raise AssertionError('unassociated scene was accepted')
assert bpy.ops.import_scene.onua3d(filepath=job['source']) == {'FINISHED'}
assert bpy.context.scene != original_scene
assert set(original_scene.objects) == original_objects
manifest, members = read_onua3d(job['source'])
source_sha = hashlib.sha256(Path(job['source']).read_bytes()).hexdigest()
assert bpy.context.scene.onua3d_source.sha256 == source_sha
blend = root/'edit.blend'
assert bpy.ops.wm.save_as_mainfile(filepath=str(blend)) == {'FINISHED'}
assert bpy.ops.wm.open_mainfile(filepath=str(blend)) == {'FINISHED'}
assert bpy.context.scene.onua3d_source.sha256 == source_sha
assert Path(bpy.context.scene.onua3d_source.filepath) == Path(job['source'])

noop = root/'noop.onua3d'
assert bpy.ops.export_scene.onua3d(filepath=str(noop)) == {'FINISHED'}
_, no_members = read_onua3d(noop)
assert all(no_members[n] == data for n,data in members.items() if n != 'scene.glb')

origins = vertex_identity_mapping(manifest)
attributes, _, _ = _Scene(no_members['scene.glb']).geometry()
counts = Counter(int(i) for row in attributes for i in row['_ONUA3D_VERTEX_ID'])
split = sorted(i for i, count in counts.items() if count > 1)
chosen = origins[split[0] if split else min(origins)]
ids = {i for i,o in origins.items() if (o['owner_path'], o['point_id']) == (chosen['owner_path'], chosen['point_id'])}
moved = set()
for obj in list(bpy.context.scene.objects):
    if obj.type != 'MESH': continue
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode='EDIT')
    mesh = bmesh.from_edit_mesh(obj.data)
    identity = mesh.verts.layers.float.get('_ONUA3D_VERTEX_ID')
    assert identity is not None
    for vert in mesh.verts:
        if int(vert[identity]) in ids:
            vert.co.x += 0.25
            moved.add(int(vert[identity]))
    bmesh.update_edit_mesh(obj.data)
    bpy.ops.object.mode_set(mode='OBJECT')
assert moved == ids
edited = root/'edited.onua3d'
assert bpy.ops.export_scene.onua3d(filepath=str(edited)) == {'FINISHED'}

saved = bpy.context.scene.onua3d_source.filepath
bpy.context.scene.onua3d_source.filepath = str(root/'missing.onua3d')
try:
    export_onua3d(bpy.context, root/'must_not_exist.onua3d')
except RuntimeError as error:
    assert 'missing' in str(error)
else: raise AssertionError('missing source was accepted')
bpy.context.scene.onua3d_source.filepath = saved
bad = root/'corrupt.onua3d'; bad.write_bytes(b'corrupt')
scene_count = len(bpy.data.scenes)
try:
    bpy.ops.import_scene.onua3d(filepath=str(bad))
except RuntimeError:
    pass
else: raise AssertionError('corrupt package was accepted')
assert len(bpy.data.scenes) == scene_count
assert not (root/'must_not_exist.onua3d').exists()
(root/'result.json').write_text(json.dumps({'owner': chosen['owner_path'], 'point_id': chosen['point_id'],
    'ids': sorted(ids), 'source_sha256': source_sha, 'noop': str(noop), 'edited': str(edited),
    'split_ids': len(split),
    'blend_source_persisted': True, 'missing_source_rejected': True, 'corrupt_source_rejected': True}, indent=2))
print('ONUA3D_ADDON_WORKFLOW_PASS', flush=True)
