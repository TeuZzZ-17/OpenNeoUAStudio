"""ONUA3D V1: canonical Complete Asset Family plus a derived glTF 2.0 GLB.

No UA parsing/resolution lives here. See docs/ONUA3D.md for the wire contract.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import tempfile
import zipfile

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QImage

from asset_family_package import (
    MANIFEST_NAME, AssetFamilyPackageError, validate_family_package,
)
from assembly_viewer import _rotation_matrix, ViewFace, ViewMaterial
from indexed_family_adapter import IndexedFamilyAdapter, UnsupportedIndexedMaterialError
from indexed_renderer import IndexedSurface
from onua3d_preview import (
    TICKS_PER_SECOND, preview_duration, surface_image, tracy_rgba_table, vanm_timeline,
)


FORMAT = "OpenNeoUA ONUA3D"
VERSION = 1
MANIFEST = "onua3d_manifest.json"
# UA's downward Y becomes glTF's upward Y. Preserve numerical UA units;
# 1 UA unit is represented by 1 glTF metre, a bridge convention, not metrology.
AXES = (1.0, -1.0, 1.0)
UV_DIVISOR = 256.0
COORDINATES = {
    "ua_to_gltf": "(x, y, z) -> (x, -y, z)",
    "meters_per_ua_unit": 1.0,
    "physical_scale_known": False,
    "uv": "(u/256, v/256), top-left origin; no image flip",
    "winding": "UA fan (0,j,j-1), reversed after Y reflection: (0,j-1,j)",
    "transforms": "Studio independent object placement: T*S*R; identity owner hierarchy",
}


def _json(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def convert_point(point):
    return tuple(float(value) * axis for value, axis in zip(point, AXES))


def _lookup(values, name):
    matches = [value for key, value in values.items() if key.casefold() == name.casefold()]
    if len(matches) != 1:
        raise AssetFamilyPackageError(f"Missing or ambiguous resolved resource: {name}")
    return matches[0]


class _Glb:
    def __init__(self):
        self.binary = bytearray()
        self.doc = {
            "asset": {"version": "2.0", "generator": "OpenNeoUA Studio ONUA3D V1"},
            "scene": 0, "scenes": [{"nodes": [0]}],
            "nodes": [], "meshes": [], "materials": [], "images": [],
            "textures": [], "samplers": [{"magFilter": 9728, "minFilter": 9728,
                                          "wrapS": 33071, "wrapT": 33071}],
            "bufferViews": [], "accessors": [],
            "extensionsUsed": ["KHR_materials_unlit"],
        }

    def view(self, data, target=None):
        self.binary.extend(b"\0" * (-len(self.binary) % 4))
        row = {"buffer": 0, "byteOffset": len(self.binary), "byteLength": len(data)}
        if target is not None:
            row["target"] = target
        self.binary.extend(data)
        result = len(self.doc["bufferViews"])
        self.doc["bufferViews"].append(row)
        return result

    def accessor(self, values, width, *, indices=False, bounds=False, animation=False):
        flat = [v for value in values for v in value] if width > 1 else values
        data = struct.pack("<" + ("I" if indices else "f") * len(flat), *flat)
        row = {"bufferView": self.view(data, None if animation else 34963 if indices else 34962),
               "componentType": 5125 if indices else 5126,
               "count": len(values), "type": {1: "SCALAR", 2: "VEC2", 3: "VEC3"}[width]}
        if bounds:
            rows = [(v,) for v in values] if width == 1 else values
            row["min"] = [min(v[i] for v in rows) for i in range(width)]
            row["max"] = [max(v[i] for v in rows) for i in range(width)]
        index = len(self.doc["accessors"])
        self.doc["accessors"].append(row)
        return index

    def encode(self):
        self.doc["buffers"] = [{"byteLength": len(self.binary)}]
        # glTF disallows empty arrays for these optional collections.
        doc = {key: value for key, value in self.doc.items() if value != []}
        if not doc.get("materials"):
            doc.pop("extensionsUsed", None)
        if not doc.get("textures"):
            doc.pop("samplers", None)
        encoded = _json(doc)
        encoded += b" " * (-len(encoded) % 4)
        binary = bytes(self.binary) + b"\0" * (-len(self.binary) % 4)
        return (struct.pack("<4sII", b"glTF", 2, 28 + len(encoded) + len(binary))
                + struct.pack("<I4s", len(encoded), b"JSON") + encoded
                + struct.pack("<I4s", len(binary), b"BIN\0") + binary)


def build_scene(family, *, source_owner_path="root"):
    """Return GLB bytes, PNG members and stable node/face/texture mappings."""
    glb = _Glb()
    pngs, textures, mapping, warnings = {}, {}, [], []
    animated_blocks = []
    tracy_projection = None
    adapter, reason = IndexedFamilyAdapter.try_create(family)
    if adapter is None:
        raise AssetFamilyPackageError("Cannot represent the resolved SET palette: " + reason)

    def texture_id(surface):
        nonlocal tracy_projection
        name = surface.name
        key = (name, surface.tracy_mode, surface.shade_mode, surface.shade_value, surface.map_mode)
        if key in textures:
            return textures[key]
        image = _lookup(family.textures, name)
        palette = adapter.tables.display_palette
        if palette is None or image.pixels is None or image.width <= 0 or image.height <= 0:
            raise AssetFamilyPackageError(f"Texture has no decoded pixels/palette: {name}")
        if surface.tracy_mode == "flat" and tracy_projection is None:
            tracy_projection = tracy_rgba_table(adapter.tables)
        qimage, preview_metadata = surface_image(
            image, surface, adapter.tables, tracy_projection)
        qimage = qimage.convertToFormat(QImage.Format.Format_RGBA8888)
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not qimage.save(buffer, "PNG"):
            raise AssetFamilyPackageError(f"PNG encoding failed: {name}")
        png = bytes(buffer.data())
        path = "textures/" + _hash(_json(key))[:24] + ".png"
        pngs[path] = png
        index = len(glb.doc["textures"])
        glb.doc["images"].append({"name": name, "bufferView": glb.view(png),
                                   "mimeType": "image/png",
                                   "extras": {"onua3d": {"logical_name": name,
                                              "png": path, "preview": preview_metadata,
                                              "palette": "SET Retail display palette"}}})
        glb.doc["textures"].append({"source": index, "sampler": 0})
        textures[key] = index
        return index

    def visit(obj, parent=None):
        owner = obj.owner_path
        source_owner = source_owner_path + owner[len("root"):]
        skeleton = obj.skeleton
        if skeleton is None and obj.base_object.skeleton_name:
            raise AssetFamilyPackageError(f"Unresolved skeleton: {owner}")
        points = skeleton.points if skeleton is not None else []
        polygons = skeleton.polygons if skeleton is not None else []
        node_id = len(glb.doc["nodes"])
        placement_id, mesh_node_id = node_id + 1, node_id + 2
        transform = obj.base_object.transform
        rotation = _rotation_matrix(transform.euler if transform else (0, 0, 0), (1, 1, 1))
        matrix = [rotation[row][col] * AXES[row] * AXES[col] if row < 3 and col < 3
                  else float(row == col) for col in range(4) for row in range(4)]
        # Studio applies BASE STRC independently for each object. Logical KIDS
        # therefore hang from identity owner nodes, not placement carriers.
        glb.doc["nodes"].extend([
            {"name": obj.base_object.name or owner, "children": [placement_id],
             "extras": {"onua3d": {"owner_path": owner, "role": "owner",
                                    "source_owner_path": source_owner,
                                    "transform_ua": asdict(transform) if transform else None}}},
            {"name": owner + "/placement", "children": [mesh_node_id],
             "translation": convert_point(transform.position if transform else (0, 0, 0)),
             "scale": list(transform.scale) if transform else [1, 1, 1],
             "extras": {"onua3d": {"owner_path": owner, "role": "placement"}}},
            {"name": owner + "/geometry", "matrix": matrix,
             "extras": {"onua3d": {"owner_path": owner, "role": "geometry"}}},
        ])
        if parent is not None:
            glb.doc["nodes"][parent]["children"].append(node_id)
        row = {"owner_path": owner, "source_owner_path": source_owner,
               "node": node_id, "placement_node": placement_id,
               "geometry_node": mesh_node_id, "source_point_count": len(points),
               "source_polygon_count": len(polygons), "primitives": []}
        mapping.append(row)
        primitives, covered = [], set()

        def primitive(faces, material_id, block_index, *, target_primitives=None,
                      target_node=None, frame_index=None):
            target_primitives = primitives if target_primitives is None else target_primitives
            target_node = mesh_node_id if target_node is None else target_node
            positions, uvs, vertex_ids, indices, face_rows = [], [], [], [], []
            for poly_id, uv, shade in faces:
                if not 0 <= poly_id < len(polygons):
                    raise AssetFamilyPackageError(f"{owner}: invalid polygon {poly_id}")
                poly = polygons[poly_id]
                if len(poly) < 3:
                    continue
                if any(i < 0 or i >= len(points) for i in poly):
                    raise AssetFamilyPackageError(f"{owner}: invalid vertex in polygon {poly_id}")
                if material_id is not None and "baseColorTexture" in glb.doc["materials"][material_id]["pbrMetallicRoughness"] and len(uv) != len(poly):
                    raise AssetFamilyPackageError(f"{owner} polygon {poly_id}: {len(uv)} UVs for {len(poly)} corners")
                start = len(positions)
                for corner, point_id in enumerate(poly):
                    point = convert_point(points[point_id])
                    if not all(math.isfinite(v) for v in point):
                        raise AssetFamilyPackageError(f"{owner}: non-finite vertex {point_id}")
                    positions.append(point)
                    vertex_ids.append(point_id)
                    uvs.append(tuple(v / UV_DIVISOR for v in uv[corner])
                               if corner < len(uv) else (0, 0))
                face_rows.append({"poly_id": poly_id, "first_index": len(indices),
                                  "index_count": 3 * (len(poly) - 2), "shade": shade})
                for j in range(2, len(poly)):
                    indices.extend((start, start + j - 1, start + j))
                covered.add(poly_id)
            if not positions:
                return
            metadata = {"owner_path": owner, "block_index": block_index,
                        "point_ids": vertex_ids, "faces": face_rows}
            if frame_index is not None:
                metadata["vanm_frame"] = frame_index
            attributes = {"POSITION": glb.accessor(positions, 3, bounds=True)}
            if material_id is not None and "baseColorTexture" in glb.doc["materials"][material_id]["pbrMetallicRoughness"]:
                attributes["TEXCOORD_0"] = glb.accessor(uvs, 2)
            prim = {"attributes": attributes, "indices": glb.accessor(indices, 1, indices=True),
                    "mode": 4, "extras": {"onua3d": metadata}}
            if material_id is not None:
                prim["material"] = material_id
            row["primitives"].append({"primitive": len(target_primitives),
                                      "node": target_node, **metadata})
            target_primitives.append(prim)

        for block_index, group in enumerate(obj.materials):
            block = group.block
            if not group.faces:
                if block and block.class_id == "particle.class":
                    warnings.append(f"{owner}/ADES[{block_index}]: particle emitter retained as UA data only")
                continue
            animation, timeline = None, None
            view_material = ViewMaterial(label=group.texture_name)
            if group.kind == "bmpanim":
                animation = _lookup(family.animations, group.texture_name)
                timeline = vanm_timeline(animation, block.texture.anim_type or 0)
                view_material.anim_bitmap_names = list(animation.bitmap_names)
                view_material.anim_uv_groups = animation.texcoord_groups
                view_material.anim_frames = [(f.frame_time, f.frame_id, f.texcoords_id)
                                             for f in animation.frames]
            frame_nodes, state_nodes, material_ids = {}, {}, {}
            for frame_index in range(len(animation.frames) if animation else 1):
                target_primitives, target_node, anim_uvs = primitives, mesh_node_id, None
                if animation:
                    frame = animation.frames[frame_index]
                    if not (0 <= frame.frame_id < len(animation.bitmap_names) and
                            0 <= frame.texcoords_id < len(animation.texcoord_groups)):
                        raise AssetFamilyPackageError(f"{group.texture_name}: invalid frame {frame_index}")
                    state_key = (frame.frame_id, frame.texcoords_id)
                    if state_key in state_nodes:
                        frame_nodes[frame_index] = state_nodes[state_key]
                        continue
                    target_node = len(glb.doc["nodes"])
                    state_nodes[state_key] = frame_nodes[frame_index] = target_node
                    glb.doc["nodes"].append({
                        "name": owner + f"/ADES[{block_index}]/frame[{frame_index}]",
                        "scale": [1, 1, 1] if frame_index == 0 else [0, 0, 0],
                        "extras": {"onua3d": {"owner_path": owner, "role": "vanm_frame",
                            "block_index": block_index, "frame_index": frame_index,
                            "bitmap_id": frame.frame_id, "uv_group_id": frame.texcoords_id}}})
                    glb.doc["nodes"][mesh_node_id].setdefault("children", []).append(target_node)
                    target_primitives = []
                    anim_uvs = adapter.active_uvs(view_material, frame_index)
                face_groups = {}
                for poly, uv, shade in group.faces:
                    face = ViewFace([], [], 0, shade=shade, poly_id=poly,
                                    owner=owner, block_index=block_index)
                    try:
                        surface = adapter.resolve_surface(face, view_material, frame_index)
                    except UnsupportedIndexedMaterialError as exc:
                        # Unknown dispatch (including mapped TRACY) stays an
                        # explicitly labelled raw-bitmap preview, not invented blending.
                        name = adapter.active_texture_name(view_material, frame_index)
                        image = _lookup(family.textures, name)
                        surface = IndexedSurface(name, "texture", image.pixels, image.width,
                            image.height, None, "none", 0, "none", "linear")
                        warning = f"{owner}/ADES[{block_index}]: raw bitmap preview only: {exc}"
                        if warning not in warnings:
                            warnings.append(warning)
                    surface_key = (surface.name, surface.kind, surface.shade_mode,
                                   surface.shade_value, surface.tracy_mode, surface.map_mode)
                    if surface_key not in face_groups:
                        face_groups[surface_key] = (surface, [])
                    if anim_uvs is not None:
                        if not 0 <= poly < len(polygons):
                            raise AssetFamilyPackageError(f"{owner}: invalid animated polygon {poly}")
                        uv = [anim_uvs[j] if j < len(anim_uvs) else (0, 0)
                              for j in range(len(polygons[poly]))]
                    face_groups[surface_key][1].append((poly, uv, shade))
                for surface_key, (surface, faces) in face_groups.items():
                    material_id = material_ids.get(surface_key)
                    if material_id is None:
                        metadata = {"owner_path": owner, "block_index": block_index,
                                    "logical_texture": group.texture_name,
                                    "tracy_mode": surface.tracy_mode,
                                    "shade_mode": surface.shade_mode, "shade_row": surface.shade_value}
                        mat = {"name": owner + f"/ADES[{block_index}]/variant[{len(material_ids)}]",
                               "pbrMetallicRoughness": {"metallicFactor": 0, "roughnessFactor": 1},
                               "extensions": {"KHR_materials_unlit": {}},
                               "extras": {"onua3d": metadata}}
                        if surface.kind == "solid":
                            mat["pbrMetallicRoughness"]["baseColorFactor"] = [0, 0, 0, 1]
                        else:
                            mat["pbrMetallicRoughness"]["baseColorTexture"] = {"index": texture_id(surface)}
                        if surface.tracy_mode == "clear":
                            mat.update(alphaMode="MASK", alphaCutoff=0.5)
                        elif surface.tracy_mode == "flat":
                            mat["alphaMode"] = "BLEND"
                        material_id = material_ids[surface_key] = len(glb.doc["materials"])
                        glb.doc["materials"].append(mat)
                    primitive(faces, material_id, block_index, target_primitives=target_primitives,
                              target_node=target_node, frame_index=frame_index if animation else None)
                if animation and target_primitives:
                    glb.doc["nodes"][target_node]["mesh"] = len(glb.doc["meshes"])
                    glb.doc["meshes"].append({"name": glb.doc["nodes"][target_node]["name"],
                                               "primitives": target_primitives})
            if animation:
                animation_meta = {"owner_path": owner, "block_index": block_index,
                                  "logical_name": group.texture_name,
                                  "anim_type": block.texture.anim_type or 0,
                                  "period_ticks": timeline.period,
                                  "frames": [{"frame_time": f.frame_time, "bitmap_id": f.frame_id,
                                              "uv_group_id": f.texcoords_id, "node": frame_nodes[i]}
                                             for i, f in enumerate(animation.frames)]}
                row.setdefault("vanm", []).append(animation_meta)
                animated_blocks.append((timeline, frame_nodes, animation_meta))
        unmapped = [(i, [], 0) for i in range(len(polygons)) if i not in covered]
        if unmapped:
            primitive(unmapped, None, -1)
            warnings.append(f"{owner}: unmapped/structural polygons retained without an invented UA material")
        if primitives:
            glb.doc["nodes"][mesh_node_id]["mesh"] = len(glb.doc["meshes"])
            glb.doc["meshes"].append({"name": owner, "primitives": primitives})
        for kid in obj.kids:
            visit(kid, node_id)

    visit(family.root_object)
    if not glb.doc["meshes"]:
        raise AssetFamilyPackageError("The family has no exportable surface geometry")
    if animated_blocks:
        duration, common_period = preview_duration([item[0] for item in animated_blocks])
        clip = {"name": "ONUA3D VANM playback", "samplers": [], "channels": [],
                "extras": {"onua3d": {"ticks_per_second": TICKS_PER_SECOND,
                    "duration_ticks": duration, "closed_common_period": common_period,
                    "representation": "STEP frame visibility using node scale; no skinning"}}}
        for timeline, frame_nodes, metadata in animated_blocks:
            transitions = timeline.transitions(duration)
            metadata["preview_duration_ticks"] = duration
            for node in sorted(set(frame_nodes.values())):
                keys, scales = [], []
                for tick, frame in transitions:
                    scale = [1.0] * 3 if frame_nodes[frame] == node else [0.0] * 3
                    if not scales or scale != scales[-1]:
                        keys.append(tick / TICKS_PER_SECOND)
                        scales.append(scale)
                end_time = duration / TICKS_PER_SECOND
                if keys[-1] != end_time:
                    keys.append(end_time)
                    scales.append(scales[-1])
                sampler = len(clip["samplers"])
                clip["samplers"].append({"input": glb.accessor(keys, 1, bounds=True, animation=True),
                    "output": glb.accessor(scales, 3, animation=True), "interpolation": "STEP"})
                clip["channels"].append({"sampler": sampler, "target": {"node": node, "path": "scale"}})
        glb.doc["animations"] = [clip]
        if not common_period:
            warnings.append("VANM common period exceeds 120 seconds: bounded preview, phase discontinuity when the whole clip loops")
        if any(timeline.period is None for timeline, _, _ in animated_blocks):
            warnings.append("A non-positive VANM frame duration freezes playback at that frame, matching Studio")
    warnings.append("GLB bakes static SHADERMP; flat TRACY is a measured source-over approximation of TRACYRMP. Runtime fade, raster interpolation and particle simulation remain UA metadata")
    return glb.encode(), pngs, mapping, warnings


def write_onua3d(package_root, target, *, validation=None, source_owner_path="root"):
    """Consume canonical validated staging; atomically publish a deterministic ZIP."""
    root, target = Path(package_root), Path(target)
    validation = validation or validate_family_package(root)
    if not validation.valid or validation.family is None:
        raise AssetFamilyPackageError("Invalid Asset Family package: " + "; ".join(validation.errors))
    for entry in validation.manifest["entries"]:
        source = Path(entry["resolved_source"])
        if source.exists() and source.resolve() == target.resolve():
            raise AssetFamilyPackageError("ONUA3D cannot replace an original source file")
    try:
        scene, pngs, mapping, warnings = build_scene(
            validation.family, source_owner_path=source_owner_path)
        members = {"scene.glb": scene, **pngs}
        members["ua_family/" + MANIFEST_NAME] = (root / MANIFEST_NAME).read_bytes()
        for entry in validation.manifest["entries"]:
            if entry["status"] == "exported":
                relative = entry["exported_path"]
                members["ua_family/" + relative] = (root / relative).read_bytes()
        manifest = {"format": FORMAT, "version": VERSION, "scene": "scene.glb",
                    "family_manifest": "ua_family/" + MANIFEST_NAME,
                    "semantic_sha256": validation.manifest["semantic_sha256"],
                    "coordinates": COORDINATES, "nodes": mapping, "warnings": warnings,
                    "files": {name: {"sha256": _hash(data), "size": len(data)}
                              for name, data in sorted(members.items())}}
        members[MANIFEST] = _json(manifest)
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name, data in sorted(members.items()):
                    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.create_system = 3
                    info.external_attr = 0o100644 << 16
                    archive.writestr(info, data)
            with zipfile.ZipFile(temporary) as archive:
                if archive.testzip() is not None or any(
                        _hash(archive.read(name)) != _hash(data) for name, data in members.items()):
                    raise AssetFamilyPackageError("ONUA3D ZIP readback failed")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return manifest
    except (ValueError, OverflowError, struct.error) as exc:
        raise AssetFamilyPackageError(f"ONUA3D encoding failed: {exc}") from exc
