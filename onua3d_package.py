"""Shared ONUA3D package/GLB validation. No native parser, writer or Qt dependency."""
from collections import Counter
import copy
import hashlib
import io
import json
import math
from pathlib import Path, PurePosixPath
import stat
import struct
import tempfile
import zipfile
from asset_package_paths import (AssetFamilyPackageError, MANIFEST_NAME, MANIFEST_FORMAT,
                                 MANIFEST_VERSION, package_relative_path)
from onua3d_contract import COORDINATES, FORMAT, MANIFEST, VERSION, convert_point
from onua3d_identity import ATTRIBUTE, canonical_vertex_positions, vertex_identity_mapping
from verified_io import commit_verified_files

# ONLY Object Mode matrix equivalence. Never use this for vertex positions.
TRANSFORM_EPSILON = 1e-6


class Onua3dImportError(AssetFamilyPackageError):
    pass


def _require(condition, message):
    if not condition:
        raise Onua3dImportError(message)


def _json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    def constant(value):
        raise Onua3dImportError(f"Non-finite JSON value: {value}")

    return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)


def _index(rows, index):
    _require(type(index) is int and 0 <= index < len(rows), "Invalid glTF index")
    return rows[index]


def _vector(values, size):
    _require(isinstance(values, list) and len(values) == size
             and all(type(v) in (int, float) and math.isfinite(v) for v in values),
             "Invalid or non-finite glTF transform")
    return values


def node_matrix(node):
    """Canonical column-major local matrix; glTF T*R*S, without corrections."""
    if "matrix" in node:
        _require(not any(k in node for k in ("translation", "rotation", "scale")),
                 "Node mixes matrix and TRS")
        return _vector(node["matrix"], 16)
    x, y, z, w = _vector(node.get("rotation", [0, 0, 0, 1]), 4)
    sx, sy, sz = _vector(node.get("scale", [1, 1, 1]), 3)
    tx, ty, tz = _vector(node.get("translation", [0, 0, 0]), 3)
    # Do not normalize or repair the imported quaternion.
    result = [(1-2*(y*y+z*z))*sx, 2*(x*y+z*w)*sx, 2*(x*z-y*w)*sx, 0,
              2*(x*y-z*w)*sy, (1-2*(x*x+z*z))*sy, 2*(y*z+x*w)*sy, 0,
              2*(x*z+y*w)*sz, 2*(y*z-x*w)*sz, (1-2*(x*x+y*y))*sz, 0,
              tx, ty, tz, 1]
    return _vector(result, 16)


class _Scene:
    """Decode the uncompressed embedded glTF subset used by this contract."""

    def __init__(self, data):
        _require(len(data) >= 28, "Truncated GLB")
        _require(struct.unpack_from("<4sII", data) == (b"glTF", 2, len(data)),
                 "Invalid GLB header/version/length")
        length, tag = struct.unpack_from("<I4s", data, 12)
        _require(tag == b"JSON" and length % 4 == 0 and 28 + length <= len(data),
                 "Invalid GLB JSON chunk")
        self.doc = _json(data[20:20+length])
        size, tag = struct.unpack_from("<I4s", data, 20+length)
        _require(tag == b"BIN\0" and size % 4 == 0 and 28+length+size == len(data),
                 "Invalid GLB binary chunk")
        self.binary = data[28+length:]
        d = self.doc
        _require(d["asset"]["version"] == "2.0" and not d.get("skins"),
                 "Unsupported glTF version or skinning")
        buffers = d["buffers"]
        _require(len(buffers) == 1 and "uri" not in buffers[0]
                 and type(buffers[0]["byteLength"]) is int
                 and 0 <= size - buffers[0]["byteLength"] <= 3,
                 "GLB must use one embedded buffer")
        self.buffer_size = buffers[0]["byteLength"]
        self.nodes, self.parents = {}, {}
        for index, node in enumerate(d["nodes"]):
            _require(not any(k in node for k in ("skin", "weights", "extensions")),
                     "Unsupported glTF node deformation/extension")
            meta = node.get("extras", {}).get("onua3d")
            _require(isinstance(meta, dict) and "owner_path" in meta and "role" in meta,
                     "Missing ONUA3D node metadata")
            key = json.dumps(meta, sort_keys=True, allow_nan=False)
            _require(key not in self.nodes, "Ambiguous ONUA3D node metadata")
            self.nodes[key] = (index, node)
            for child in node.get("children", []):
                _index(d["nodes"], child)
                _require(child not in self.parents, "Node has multiple parents")
                self.parents[child] = index
        self.keys = {index: key for key, (index, _) in self.nodes.items()}
        _require(len(d["scenes"]) == 1 and d.get("scene", 0) == 0,
                 "V1 requires one scene")
        roots = d["scenes"][0]["nodes"]
        for index in roots:
            _index(d["nodes"], index)
        _require(len(set(roots)) == len(roots)
                 and set(roots) == set(self.keys) - self.parents.keys(),
                 "Invalid scene roots")
        for index in self.keys:
            seen = set()
            while index in self.parents:
                _require(index not in seen, "Cyclic node hierarchy")
                seen.add(index)
                index = self.parents[index]
        # VANM preview visibility is derived (Blender resamples its scale
        # curves). It never edits native ANM. Object transform animation and
        # deformation channels, however, cannot bypass the Object Mode gate.
        for animation in d.get("animations", []):
            for channel in animation["channels"]:
                target = channel["target"]
                node = _index(d["nodes"], target["node"])
                _require(node["extras"]["onua3d"]["role"] == "vanm_frame"
                         and target["path"] == "scale",
                         "Object transform/geometry animation editing is not supported")

    def values(self, index, kind, components):
        a = _index(self.doc["accessors"], index)
        _require(a.get("type") == kind and a.get("componentType") in components
                 and not a.get("normalized", False) and "sparse" not in a,
                 "Unsupported or corrupt glTF accessor encoding")
        view = _index(self.doc["bufferViews"], a["bufferView"])
        _require(view.get("buffer") == 0 and not view.get("extensions"),
                 "Unsupported glTF buffer view")
        width = {"SCALAR": 1, "VEC2": 2, "VEC3": 3}[kind]
        fmt = {5121: "B", 5123: "H", 5125: "I", 5126: "f"}[a["componentType"]]
        element = struct.calcsize("<" + fmt * width)
        start, size = view.get("byteOffset", 0), view["byteLength"]
        offset, count = a.get("byteOffset", 0), a["count"]
        stride = view.get("byteStride", element)
        _require(all(type(v) is int and v >= 0 for v in (start, size, offset, count, stride))
                 and count > 0 and stride >= element and start+size <= self.buffer_size
                 and offset+(count-1)*stride+element <= size,
                 "glTF accessor is outside its buffer view")
        rows = [struct.unpack_from("<" + fmt * width, self.binary, start+offset+i*stride)
                for i in range(count)]
        return [r[0] for r in rows] if width == 1 else rows

    def geometry(self):
        attributes, triangles, uvs, meshes = [], Counter(), {}, set()
        for key, (_, node) in self.nodes.items():
            if "mesh" not in node:
                continue
            mesh_index = node["mesh"]
            mesh = _index(self.doc["meshes"], mesh_index)
            _require(not mesh.get("weights"), "Morph weights are not supported")
            meshes.add(mesh_index)
            for primitive in mesh["primitives"]:
                _require(primitive.get("mode", 4) == 4 and not primitive.get("targets")
                         and not primitive.get("extensions"), "V1 requires uncompressed triangles")
                attrs = primitive["attributes"]
                _require(ATTRIBUTE in attrs and "POSITION" in attrs,
                         "Primitive is missing persistent ID or POSITION")
                ids = self.values(attrs[ATTRIBUTE], "SCALAR", (5126,))
                points = self.values(attrs["POSITION"], "VEC3", (5126,))
                attributes.append({ATTRIBUTE: ids, "POSITION": points})
                indices = (self.values(primitive["indices"], "SCALAR", (5121, 5123, 5125))
                           if "indices" in primitive else list(range(len(points))))
                _require(len(ids) == len(points) and len(indices) % 3 == 0
                         and all(0 <= i < len(ids) for i in indices), "Invalid triangle indices/count")
                material = (_index(self.doc.get("materials", []), primitive["material"])
                            if "material" in primitive else {})
                material_key = json.dumps(material.get("extras", {}).get("onua3d"), sort_keys=True)
                for offset in range(0, len(indices), 3):
                    tri = tuple(ids[i] for i in indices[offset:offset+3])
                    tri = min(tri, tri[1:]+tri[:1], tri[2:]+tri[:2])
                    triangles[(key, material_key, tri)] += 1
                if "TEXCOORD_0" in attrs:
                    uv = self.values(attrs["TEXCOORD_0"], "VEC2", (5126,))
                    _require(len(uv) == len(ids), "UV accessor count mismatch")
                    for i in set(indices):
                        ident = ids[i]
                        _require(ident not in uvs or uvs[ident] == uv[i], "Conflicting UV copies")
                        uvs[ident] = uv[i]
        _require(meshes == set(range(len(self.doc.get("meshes", [])))), "Unreferenced glTF mesh")
        return attributes, triangles, uvs


def validate_scene_positions(manifest, baseline_glb, imported_glb):
    """Return (owner_path, point_id) -> UA local position, after all V1 checks."""
    try:
        origins = vertex_identity_mapping(manifest)
        baseline, imported = _Scene(baseline_glb), _Scene(imported_glb)
        _require(baseline.nodes.keys() == imported.nodes.keys(), "ONUA3D node mapping changed")
        for key, (index, node) in baseline.nodes.items():
            other_index, other = imported.nodes[key]
            _require(baseline.keys.get(baseline.parents.get(index))
                     == imported.keys.get(imported.parents.get(other_index)), "Node hierarchy changed")
            _require(("mesh" in node) == ("mesh" in other), "Node mesh placement changed")
            error = max(abs(a-b) for a, b in zip(node_matrix(node), node_matrix(other)))
            _require(error <= TRANSFORM_EPSILON,
                     f"Object Mode transform changed: {node['extras']['onua3d']} "
                     f"(maximum matrix difference {error:.9g} > {TRANSFORM_EPSILON:g})")
        base_attrs, base_triangles, base_uvs = baseline.geometry()
        attrs, triangles, uvs = imported.geometry()
        canonical_vertex_positions(manifest, base_attrs)
        positions = canonical_vertex_positions(manifest, attrs)
        _require(base_triangles == triangles, "Topology or owner/block/material assignment changed")
        _require(all(uvs.get(ident) == uv for ident, uv in base_uvs.items()),
                 "UV editing is not supported by ONUA3D V1")
        result = {}
        for ident, position in positions.items():
            origin = origins[ident]
            key = (origin["owner_path"], origin["point_id"])
            point = convert_point(position)  # reflection is its own inverse
            _require(key not in result or result[key] == point,
                     f"Conflicting POSITION for SKLT point {key}: distinct ONUA3D IDs disagree")
            result[key] = point
        return result
    except (KeyError, TypeError, ValueError, IndexError, AttributeError, struct.error, OverflowError) as exc:
        raise Onua3dImportError(f"Invalid ONUA3D scene: {exc}") from exc


def _read_package(source):
    with zipfile.ZipFile(source) as archive:
        members, seen = {}, set()
        for info in archive.infolist():
            name = info.filename
            path = PurePosixPath(name)
            _require(not path.is_absolute() and path.as_posix() == name
                     and all(part not in ("", ".", "..") and not part.endswith((".", " "))
                             and not any(c in part for c in '\\:<>"|?*\0') for part in path.parts)
                     and not info.is_dir() and not stat.S_ISLNK(info.external_attr >> 16),
                     f"Unsafe ONUA3D member: {name}")
            _require(name.casefold() not in seen, f"Duplicate ONUA3D member: {name}")
            _require(package_relative_path(name, default_name="", extensions=(path.suffix,)).as_posix() == name,
                     f"Non-canonical ONUA3D member: {name}")
            seen.add(name.casefold())
            members[name] = archive.read(info)
    _require(MANIFEST in members, "Missing ONUA3D manifest")
    manifest = _json(members.pop(MANIFEST))
    _require(manifest.get("format") == FORMAT and type(manifest.get("version")) is int
             and manifest["version"] == VERSION, "Unsupported ONUA3D format/version")
    _require(manifest.get("scene") == "scene.glb"
             and manifest.get("family_manifest") == "ua_family/" + MANIFEST_NAME
             and manifest.get("coordinates") == COORDINATES, "Invalid ONUA3D manifest layout/coordinates")
    vertex_identity_mapping(manifest)
    _require(isinstance(manifest.get("files"), dict) and members.keys() == manifest["files"].keys(),
             "ONUA3D manifest member list mismatch")
    for name, data in members.items():
        expected = manifest["files"][name]
        _require(type(expected.get("size")) is int and expected["size"] == len(data)
                 and expected.get("sha256") == hashlib.sha256(data).hexdigest(),
                 f"ONUA3D member hash/size mismatch: {name}")
    _require("scene.glb" in members and "ua_family/" + MANIFEST_NAME in members,
             "Missing scene or embedded Complete Asset Family")
    return manifest, members


def read_onua3d(source):
    """Validate portable container, embedded manifest and ID/native snapshot links.

    No native resource is reconstructed here. Studio additionally reopens the
    embedded native files and checks its regenerated mapping before import.
    """
    try:
        manifest, members = _read_package(source)
        native = _json(members['ua_family/' + MANIFEST_NAME])
        _require(native['format'] == MANIFEST_FORMAT and type(native['version']) is int
                 and native['version'] == MANIFEST_VERSION, 'Invalid embedded family manifest')
        snapshot = native['semantic_snapshot']
        semantic = hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                                            separators=(',', ':')).encode('utf-8')).hexdigest()
        _require(semantic == native['semantic_sha256'] == manifest['semantic_sha256'],
                 'Embedded family semantic hash mismatch')
        expected = {'ua_family/' + MANIFEST_NAME}
        seen = set()
        entries = []
        for entry in native['entries']:
            name = entry['exported_path']
            _require(name.casefold() not in seen, 'Duplicate embedded family entry')
            seen.add(name.casefold())
            if entry['status'] != 'exported':
                continue
            key = 'ua_family/' + name
            expected.add(key)
            _require(key in members and hashlib.sha256(members[key]).hexdigest() == entry['sha256'],
                     f'Embedded family member hash mismatch: {name}')
            if name == native['entry_base'] and entry['asset_class'] == 'base.class':
                entries.append(entry)
        _require(len(entries) == 1 and expected == {n for n in members if n.startswith('ua_family/')},
                 'Invalid embedded family inventory/entry BASE')
        owners = {}
        def visit(node):
            _require(node['path'] not in owners, 'Ambiguous native snapshot owner')
            owners[node['path']] = node
            for child in node['kids']: visit(child)
        visit(snapshot['object_tree'])
        for origin in vertex_identity_mapping(manifest).values():
            node = owners[origin['owner_path']]
            skeleton = node['skeleton']
            polygon = _index(skeleton['polygons'], origin['polygon_id'])
            _index(skeleton['points'], origin['point_id'])
            _require(_index(polygon, origin['corner']) == origin['point_id'],
                     'Vertex mapping disagrees with embedded polygon/corner')
            if origin['block_index'] != -1:
                _index(node['ades'], origin['block_index'])
        validate_scene_positions(manifest, members['scene.glb'], members['scene.glb'])
        return manifest, members
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, zipfile.BadZipFile) as exc:
        raise Onua3dImportError(f'Invalid ONUA3D package: {exc}') from exc


def repack_onua3d(source, scene_glb, target, *, source_sha256):
    """Copy a pinned source package, replacing only scene.glb and its hash/size.

    A NEW output is required. The original package, embedded native members,
    other ZIP members and all other manifest values are preserved.
    """
    source, target = Path(source), Path(target)
    _require(source.is_file(), 'Source ONUA3D package is missing; open it again')
    _require(not target.exists() and not target.is_symlink(), 'Choose a NEW ONUA3D output file')
    data = source.read_bytes()
    _require(hashlib.sha256(data).hexdigest() == source_sha256,
             'Source ONUA3D package changed; open it again before exporting')
    manifest, members = read_onua3d(io.BytesIO(data))
    validate_scene_positions(manifest, members['scene.glb'], scene_glb)
    updated = copy.deepcopy(manifest)
    updated['files']['scene.glb'].update(size=len(scene_glb), sha256=hashlib.sha256(scene_glb).hexdigest())
    replacement_manifest = (json.dumps(updated, ensure_ascii=False, indent=2, allow_nan=False)+'\n').encode('utf-8')
    def verify(path):
        actual_manifest, actual_members = read_onua3d(path)
        _require(actual_manifest == updated, 'ONUA3D manifest readback mismatch')
        _require(actual_members['scene.glb'] == scene_glb, 'ONUA3D scene readback mismatch')
        _require(all(actual_members[name] == content for name, content in members.items()
                     if name != 'scene.glb'), 'ONUA3D repack changed an original member')
    with tempfile.TemporaryDirectory(prefix='OpenNeoUA_onua3d_repack_') as temporary:
        staged = Path(temporary) / 'result.onua3d'
        with zipfile.ZipFile(io.BytesIO(data)) as original, zipfile.ZipFile(staged, 'w') as output:
            for info in original.infolist():
                payload = (replacement_manifest if info.filename == MANIFEST else
                           scene_glb if info.filename == 'scene.glb' else members[info.filename])
                output.writestr(info, payload)
        verify(staged)
        commit_verified_files([(staged, target)], replace_existing=False, verify=lambda: verify(target))
    return updated
