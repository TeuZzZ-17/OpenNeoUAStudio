"""Headless Blender helper, launched only by the optional integration tests."""
import json
from pathlib import Path
import sys

import bpy

job = json.loads(Path(sys.argv[-1]).read_text(encoding="utf-8"))
bpy.ops.wm.read_factory_settings(use_empty=True)
assert bpy.ops.import_scene.gltf(filepath=job["input"], merge_vertices=False) == {"FINISHED"}
moved = set()
for obj in bpy.data.objects:
    if obj.type != "MESH":
        continue
    attribute = obj.data.attributes.get("_ONUA3D_VERTEX_ID")
    assert attribute is not None, "Blender lost the ID attribute"
    for i, value in enumerate(attribute.data):
        if value.value in job["move_ids"]:
            obj.data.vertices[i].co.x += 0.25
            moved.add(int(value.value))
assert moved == set(job["move_ids"]), "Requested logical vertex was not moved"
if job.get("object_move"):
    geometry = [obj for obj in bpy.data.objects
                if obj.get("onua3d", {}).get("role") == "geometry"]
    assert geometry, "No canonical geometry carrier found"
    geometry[0].location.x += 0.25
assert bpy.ops.export_scene.gltf(
    filepath=job["output"], export_format="GLB", export_attributes=True,
    export_extras=True, export_normals=True) == {"FINISHED"}
