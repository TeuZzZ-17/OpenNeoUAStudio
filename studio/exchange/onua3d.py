"""ONUA3D V1 exchange: export a Complete Asset Family to a derived glTF/GLB, import it back,
and the derived preview policies.
"""

from __future__ import annotations

from contextlib import contextmanager
import copy
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import stat
import struct
import tempfile
import zipfile

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QImage

from studio.assets.family import (
    AssetFamily,
    family_semantic_snapshot,
    load_asset_family,
    PackageEntry,
    validate_family_package,
    write_package_manifest,
)
from studio.exchange.portable import (
    _load_json,
    _read_package,
    _require,
    AssetFamilyPackageError,
    ATTRIBUTE,
    AXES,
    commit_verified_files,
    convert_point,
    COORDINATES,
    FORMAT,
    IDENTITY_CONTRACT,
    MANIFEST,
    MANIFEST_NAME,
    MAX_VERTEX_ID,
    Onua3dImportError,
    UV_DIVISOR,
    validate_scene_positions,
    VERSION,
)
from studio.formats.images import image_to_qimage
from studio.formats.sklt import parse_sklt_file, save_sklt_with_poo2_points, SkltParseError
from studio.rendering.asset_viewport import _rotation_matrix, AssetViewport, ViewFace, ViewMaterial
from studio.rendering.indexed import (
    IndexedFamilyAdapter,
    IndexedSurface,
    UnsupportedIndexedMaterialError,
)

# ----------------------------------------------------------------------------
# ONUA3D V1: canonical Complete Asset Family plus a derived glTF 2.0 GLB.
# (formerly onua3d_export.py)
# ----------------------------------------------------------------------------

def _dump_json(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _hash(data):
    return hashlib.sha256(data).hexdigest()


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
        encoded = _dump_json(doc)
        encoded += b" " * (-len(encoded) % 4)
        binary = bytes(self.binary) + b"\0" * (-len(self.binary) % 4)
        return (struct.pack("<4sII", b"glTF", 2, 28 + len(encoded) + len(binary))
                + struct.pack("<I4s", len(encoded), b"JSON") + encoded
                + struct.pack("<I4s", len(binary), b"BIN\0") + binary)


def build_scene(family, *, source_owner_path="root"):
    """Return GLB bytes, PNG members and stable node/face/texture mappings."""
    glb = _Glb()
    pngs, textures, mapping, warnings = {}, {}, [], []
    next_vertex_id = 1
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
        path = "textures/" + _hash(_dump_json(key))[:24] + ".png"
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
               "source_polygon_count": len(polygons), "primitives": [],
               "vertex_instances": {}}
        mapping.append(row)
        primitives, covered = [], set()

        def primitive(faces, material_id, block_index, *, target_primitives=None,
                      target_node=None, frame_index=None):
            nonlocal next_vertex_id
            target_primitives = primitives if target_primitives is None else target_primitives
            target_node = mesh_node_id if target_node is None else target_node
            positions, uvs, vertex_ids, indices, face_rows = [], [], [], [], []
            persistent_ids = []
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
                    if next_vertex_id > MAX_VERTEX_ID:
                        raise AssetFamilyPackageError("ONUA3D persistent vertex ID limit exceeded")
                    origin = {"point_id": point_id, "polygon_id": poly_id,
                              "corner": corner, "block_index": block_index}
                    if frame_index is not None:
                        origin["vanm_frame"] = frame_index
                    row["vertex_instances"][str(next_vertex_id)] = origin
                    persistent_ids.append(next_vertex_id)
                    next_vertex_id += 1
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
            attributes = {"POSITION": glb.accessor(positions, 3, bounds=True),
                          ATTRIBUTE: glb.accessor(persistent_ids, 1)}
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
    warnings.append("GLB bakes static SHADERMP; flat TRACY uses an anchored source-over preview of TRACYRMP with a small dark-FX visibility floor and remains destination-dependent in UA. Runtime fade, raster interpolation and particle simulation remain UA metadata")
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
                    "vertex_identity": IDENTITY_CONTRACT,
                    "files": {name: {"sha256": _hash(data), "size": len(data)}
                              for name, data in sorted(members.items())}}
        members[MANIFEST] = _dump_json(manifest)
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


# ----------------------------------------------------------------------------
# ONUA3D V1 position-only import into the embedded Complete Asset Family.
# (formerly onua3d_import.py)
# ----------------------------------------------------------------------------

@dataclass
class Onua3dImportResult:
    family: AssetFamily
    output_root: Path
    changed_points: int
    changed_skeletons: tuple[str, ...]


def _validated(root):
    result = validate_family_package(root)
    _require(result.valid and result.family is not None,
             "Invalid Complete Asset Family: " + "; ".join(result.errors))
    return result


def _lstat(path: Path):
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _is_reparse_or_link(path: Path, info) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction():
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _destination_path(output_root) -> Path:
    """Return an absolute destination after checking every existing component.

    The walk intentionally uses ``lstat`` before ``resolve``.  A symlink,
    junction or Windows reparse point in any existing ancestor would otherwise
    let the selected destination escape into a different tree.
    """

    try:
        absolute = Path(os.path.abspath(os.fspath(output_root)))
    except (TypeError, ValueError, OSError) as exc:
        raise Onua3dImportError(
            f"Invalid Asset Family destination: {output_root}") from exc

    current = Path(absolute.anchor) if absolute.anchor else Path.cwd()
    for part in absolute.parts:
        if part == absolute.anchor:
            continue
        current /= part
        info = _lstat(current)
        if info is None:
            break
        _require(
            not _is_reparse_or_link(current, info),
            "Asset Family destination uses a symlink/junction/reparse point: "
            + str(current),
        )
        _require(
            stat.S_ISDIR(info.st_mode),
            "Asset Family destination ancestor is not a directory: "
            + str(current),
        )

    info = _lstat(absolute)
    if info is not None:
        _require(
            not _is_reparse_or_link(absolute, info),
            "Asset Family destination uses a symlink/junction/reparse point: "
            + str(absolute),
        )
        _require(
            stat.S_ISDIR(info.st_mode),
            "Asset Family destination is not a directory: " + str(absolute),
        )
        try:
            _require(
                not any(absolute.iterdir()),
                "Asset Family destination must be empty: " + str(absolute),
            )
        except OSError as exc:
            raise Onua3dImportError(
                "Cannot inspect Asset Family destination: " + str(absolute)
            ) from exc

    try:
        return absolute.resolve(strict=False)
    except OSError as exc:
        raise Onua3dImportError(
            "Cannot resolve Asset Family destination: " + str(absolute)
        ) from exc


def _destination_inventory(root: Path) -> tuple[set[str], set[str]]:
    files, entries = set(), set()
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        info = _lstat(path)
        _require(info is not None,
                 "Asset Family output entry disappeared: " + relative)
        _require(
            not _is_reparse_or_link(path, info),
            "Asset Family output contains a symlink/junction/reparse point: "
            + relative,
        )
        if stat.S_ISREG(info.st_mode):
            files.add(relative)
        elif not stat.S_ISDIR(info.st_mode):
            raise Onua3dImportError(
                "Asset Family output contains a non-file entry: " + relative)
        entries.add(relative)
    return files, entries


def _expected_inventory(files: set[str]) -> set[str]:
    entries = set(files)
    for name in files:
        parent = PurePosixPath(name).parent
        while parent != PurePosixPath("."):
            entries.add(parent.as_posix())
            parent = parent.parent
    return entries


def _verify_materialized(root: Path, expected_files: set[str]):
    actual_files, actual_entries = _destination_inventory(root)
    _require(
        actual_files == expected_files,
        "Published Asset Family file inventory differs from the staged inventory",
    )
    _require(
        actual_entries == _expected_inventory(expected_files),
        "Published Asset Family directory inventory differs from the staged inventory",
    )
    return _validated(root)


@dataclass
class PreparedOnua3d:
    """Fully validated native staging kept alive by :func:`prepare_onua3d`."""

    staging_root: Path
    expected_files: frozenset[str]
    family: AssetFamily
    changed_points: int
    changed_skeletons: tuple[str, ...]
    _materialized: bool = False

    def materialize(self, output_root) -> Onua3dImportResult:
        """Publish this staging tree into the exact new/empty destination."""

        _require(
            not self._materialized,
            "Prepared ONUA3D staging has already been materialized",
        )
        output = _destination_path(output_root)
        expected_files = set(self.expected_files)
        files = [
            (self.staging_root / name, output / name)
            for name in sorted(expected_files)
        ]
        _require(
            all(source.is_file() for source, _ in files),
            "Prepared ONUA3D staging is incomplete",
        )

        # Repeat the destination check immediately before the no-clobber
        # publication.  The first check protects the caller's selection while
        # the staged file list is built; this one closes the normal race
        # window before commit_verified_files starts creating output entries.
        output = _destination_path(output)
        verified: list = []

        def verify() -> None:
            verified.append(_verify_materialized(output, expected_files))

        commit_verified_files(
            files,
            verify=verify,
            replace_existing=False,
        )
        _require(
            verified and verified[0].family is not None,
            "Published Asset Family did not pass final validation",
        )
        self._materialized = True
        return Onua3dImportResult(
            verified[0].family,
            output,
            self.changed_points,
            self.changed_skeletons,
        )


@contextmanager
def prepare_onua3d(source):
    """Validate and stage an ONUA3D package before choosing its destination.

    The temporary directory remains alive through ``yield`` so callers can
    inspect or materialize the already-validated native staging without
    repeating the importer pipeline.
    """

    try:
        manifest, members = _read_package(source)
        with tempfile.TemporaryDirectory(
                prefix="OpenNeoUAStudio_onua3d_import_") as temporary:
            root = Path(temporary)
            for name, data in members.items():
                if name.startswith("ua_family/"):
                    target = root / name[len("ua_family/"):]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
            _load_json((root / MANIFEST_NAME).read_bytes())
            validation = _validated(root)
            native = validation.manifest
            _require(
                manifest.get("semantic_sha256") == native["semantic_sha256"],
                "Embedded family semantic hash mismatch",
            )
            expected_files = {
                MANIFEST_NAME,
                *[
                    e["exported_path"] for e in native["entries"]
                    if e["status"] == "exported"
                ],
            }
            canonical_files = {
                name.casefold(): name for name in expected_files
            }
            _require(
                {
                    p.relative_to(root).as_posix()
                    for p in root.rglob("*")
                    if p.is_file()
                }
                == expected_files,
                "Unexpected files in embedded family",
            )
            source_owner = manifest["nodes"][0]["source_owner_path"]
            baseline, _, mapping, _ = build_scene(
                validation.family, source_owner_path=source_owner)
            _require(
                manifest["nodes"] == mapping,
                "Manifest owner/point/polygon/corner/block mapping differs from native baseline",
            )
            updates = validate_scene_positions(
                manifest, baseline, members["scene.glb"])
            expected_snapshot = copy.deepcopy(native["semantic_snapshot"])
            physical, changed_owners = {}, {}
            for obj in validation.family.all_objects():
                model = obj.skeleton
                if model is None:
                    continue
                # Preserve the original storage representation (including
                # signed zeros) for every point without an actual coordinate
                # change.
                points = [
                    updates.get((obj.owner_path, i), point)
                    for i, point in enumerate(model.points)
                ]
                points = [
                    tuple(a if a == b else b for a, b in zip(old, new))
                    for old, new in zip(model.points, points)
                ]
                ref = obj.skeleton_ref
                changed = [
                    i for i, point in enumerate(points)
                    if point != model.points[i]
                ]
                if ref is None or ref.path is None:
                    _require(
                        not changed,
                        f"{obj.owner_path}: POO2 is not a standalone SKLT resource",
                    )
                    continue
                path = Path(ref.path).resolve()
                relative = canonical_files[
                    path.relative_to(root).as_posix().casefold()
                ]
                _require(
                    relative not in physical
                    or physical[relative][1] == points,
                    f"Conflicting positions for shared native SKLT resource: {relative}",
                )
                physical[relative] = (model, points, changed)
                if changed:
                    changed_owners[obj.owner_path] = points

            def update_snapshot(obj):
                if obj["path"] in changed_owners:
                    obj["skeleton"]["points"] = [
                        list(p) for p in changed_owners[obj["path"]]
                    ]
                for kid in obj["kids"]:
                    update_snapshot(kid)

            update_snapshot(expected_snapshot["object_tree"])
            changed_files, count = [], 0
            for relative, (model, points, changed) in physical.items():
                if not changed:
                    continue
                target = root / relative
                original = target.read_bytes()
                save_sklt_with_poo2_points(
                    model, points, target, update_sensors=False)
                actual = target.read_bytes()
                expected = bytearray(original)
                for i in changed:
                    struct.pack_into(
                        ">fff",
                        expected,
                        model.poo2_payload_offset + i * 12,
                        *points[i],
                    )
                _require(
                    actual == expected,
                    f"Writer changed bytes outside requested POO2 points: {relative}",
                )
                _require(
                    parse_sklt_file(target).points == points,
                    f"SKLT readback mismatch: {relative}",
                )
                changed_files.append(relative)
                count += len(changed)
            if changed_files:
                edited = load_asset_family(
                    root / native["entry_base"], isolated_root=root)
                _require(
                    family_semantic_snapshot(edited) == expected_snapshot,
                    "Imported family changed semantics outside POO2",
                )
                write_package_manifest(
                    root,
                    native["entry_base"],
                    edited,
                    [PackageEntry(**row) for row in native["entries"]],
                    source_name=native["source_name"],
                )
            final_validation = _validated(root)
            yield PreparedOnua3d(
                root,
                frozenset(expected_files),
                final_validation.family,
                count,
                tuple(sorted(changed_files)),
            )
    except Onua3dImportError:
        raise
    except (AssetFamilyPackageError, OSError, SkltParseError, ValueError,
            KeyError, TypeError, IndexError, AttributeError, struct.error,
            OverflowError, zipfile.BadZipFile) as exc:
        raise Onua3dImportError(f"ONUA3D import failed: {exc}") from exc


def import_onua3d(source, output_root):
    """Backward-compatible validate, stage and publish wrapper."""

    with prepare_onua3d(source) as prepared:
        return prepared.materialize(output_root)


# ----------------------------------------------------------------------------
# Derived glTF preview policies; UA data stays in the canonical family.
# (formerly onua3d_preview.py)
# ----------------------------------------------------------------------------

TICKS_PER_SECOND = 1024


# glTF has no infinite-cycle or scene timeline-range property. Supply repeated
# cycles covering Blender's default timeline, with a bounded common period.
PREVIEW_SECONDS = 60


MAX_COMMON_PERIOD_SECONDS = 120


MAX_TIMELINE_TRANSITIONS = 100_000


# Dark flat-TRACY FX can legitimately resolve to near-black against Studio's
# initial framebuffer, yet a pure global least-squares fit may collapse them
# toward invisibility. Keep a small minimum opacity for such non-identity
# columns so Blender still previews their silhouettes, without the fully opaque
# over-dark result of the previous background-0 bake.
MIN_DARK_TRACY_ALPHA = 0.18


def _linear(channel):
    c = channel / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _srgb(channel):
    c = min(1.0, max(0.0, channel))
    return round(255 * (12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055))


def _luminance(rgb):
    return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]


def tracy_rgba_table(tables):
    """Project flat TRACY to a deterministic Blender-friendly RGBA table.

    UA flat TRACY is destination-dependent: TRACYRMP maps
    ``[background][source]`` to a new indexed framebuffer value, so no single
    ordinary glTF alpha-blended texture can reproduce it for every possible
    destination color.

    For the editable Blender preview, keep any true identity/background column
    transparent. For non-identity columns, start from the previous global
    least-squares source-over fit, but anchor the preview to Studio's exact
    background-0 result and keep a small visibility floor for very dark FX.
    This avoids both previous failure modes:
    - disappearing dark silhouettes/outlines from an over-transparent fit;
    - fully opaque, over-dark planes from a pure background-0 bake.

    Numeric source zero remains clear. The returned residual still measures the
    approximation over all 256 possible destination indices, so ONUA3D
    continues to report honestly how non-representable a particular TRACY
    source is in standard glTF.
    """
    palette = [tuple(_linear(c) for c in rgb) for rgb in tables.display_palette]
    mean_b = [sum(rgb[k] for rgb in palette) / 256 for k in range(3)]
    variance = sum((rgb[k] - mean_b[k]) ** 2 for rgb in palette for k in range(3))
    result, errors = [], []
    for source in range(256):
        outputs = [palette[tables.tracy_index(background, source)]
                   for background in range(256)]
        transparent_column = (
            source == 0 or
            all(tables.tracy_index(background, source) == background
                for background in range(256))
        )
        if transparent_column:
            rgb = (0, 0, 0)
            alpha = 0
            encoded = (0.0, 0.0, 0.0)
        else:
            mean_o = [sum(rgb[k] for rgb in outputs) / 256 for k in range(3)]
            covariance = sum((background[k] - mean_b[k]) * (output[k] - mean_o[k])
                             for background, output in zip(palette, outputs)
                             for k in range(3))
            alpha_fit = 1.0 - (min(1.0, max(0.0, covariance / variance)) if variance else 0.0)
            anchor = outputs[0]
            alpha_floor = MIN_DARK_TRACY_ALPHA * max(0.0, 1.0 - _luminance(anchor))
            alpha_linear_min = max(anchor)
            alpha_value = min(1.0, max(alpha_fit, alpha_floor, alpha_linear_min))
            rgb = tuple(_srgb(channel / alpha_value) for channel in anchor)
            alpha = round(255 * alpha_value)
            encoded = tuple(_linear(c) for c in rgb)
        result.append((*rgb, alpha))
        a = alpha / 255.0
        errors.append(math.sqrt(sum(
            (a * encoded[k] + (1.0 - a) * background[k]
             - outputs[background_index][k]) ** 2
            for background_index, background in enumerate(palette)
            for k in range(3)) / 768))
    return result, errors


def surface_image(image, surface, tables, tracy_projection=None):
    """Reuse indexed decoding, replacing only the derived PNG color table."""
    rgba, residuals = [], []
    for raw in range(256):
        source = (tables.shade_index(surface.shade_value, raw)
                  if surface.shade_mode != "none" else raw)
        if surface.tracy_mode == "flat":
            colors, errors = tracy_projection
            color = colors[source]
            residuals.append(errors[source])
        else:
            color = (*tables.display_palette[source],
                     0 if surface.tracy_mode == "clear" and raw == 0 else 255)
        rgba.append(color)
    qimage = image_to_qimage(image, tables.display_palette)
    qimage.setColorTable([(a << 24) | (r << 16) | (g << 8) | b for r, g, b, a in rgba])
    used = sorted(set(image.pixels))
    metadata = {
        "tracy": surface.tracy_mode, "shade_mode": surface.shade_mode,
        "shade_row": surface.shade_value, "map_mode": surface.map_mode,
        "alpha": "TRACYRMP anchored source-over fit with dark-FX visibility floor" if residuals else
                 "numeric source zero" if surface.tracy_mode == "clear" else "opaque",
    }
    if residuals:
        metadata["linear_rgb_rmse_max_used_index"] = max(residuals[i] for i in used)
        metadata["linear_rgb_rmse_mean_used_index"] = sum(residuals[i] for i in used) / len(used)
    return qimage, metadata


@dataclass
class VanmTimeline:
    # (start tick, frame index) for one exact cycle or a terminal prefix.
    starts: list[tuple[int, int]]
    period: int | None

    def transitions(self, duration):
        if self.period is None:
            return [(t, frame) for t, frame in self.starts if t <= duration]
        rows = []
        for offset in range(0, duration + 1, self.period):
            rows.extend((offset + t, frame) for t, frame in self.starts if offset + t <= duration)
            if len(rows) > MAX_TIMELINE_TRANSITIONS:
                raise AssetFamilyPackageError("VANM preview exceeds the documented timeline transition limit")
        return rows


def vanm_timeline(animation, anim_type=0):
    if not animation.frames:
        raise AssetFamilyPackageError("VANM has no frames")
    material = ViewMaterial(label=animation.source_name, anim_type=anim_type,
                            anim_frames=[(f.frame_time, f.frame_id, f.texcoords_id)
                                         for f in animation.frames])
    state, tick, starts, seen = (0, 1), 0, [], set()
    while state not in seen:
        seen.add(state)
        frame = animation.frames[state[0]]
        if not (0 <= frame.frame_id < len(animation.bitmap_names) and
                0 <= frame.texcoords_id < len(animation.texcoord_groups)):
            raise AssetFamilyPackageError(f"VANM frame {state[0]} has invalid bitmap/UV IDs")
        starts.append((tick, state[0]))
        if frame.frame_time <= 0:
            # Same halt semantics as Studio's _advance_animation; no invented fps.
            return VanmTimeline(starts, None)
        tick += frame.frame_time
        # The existing viewer owns loop/ping-pong semantics, including held ends.
        state = AssetViewport._next_frame(None, material, *state)
    return VanmTimeline(starts, tick)


def preview_duration(timelines):
    period = 1
    for timeline in timelines:
        if timeline.period:
            period = math.lcm(period, timeline.period)
    minimum = PREVIEW_SECONDS * TICKS_PER_SECOND
    if period <= MAX_COMMON_PERIOD_SECONDS * TICKS_PER_SECOND:
        return max(period, math.ceil(minimum / period) * period), True
    return MAX_COMMON_PERIOD_SECONDS * TICKS_PER_SECOND, False
