"""Persistent ONUA3D identity contract; no GLB/native parser or import writes."""
from __future__ import annotations

import math

ATTRIBUTE = "_ONUA3D_VERTEX_ID"
# Every integer in this range is exactly representable as a glTF float32.
MAX_VERTEX_ID = 1 << 24
IDENTITY_CONTRACT = {
    "version": 1,
    "attribute": ATTRIBUTE,
    "component_type": 5126,
    "accessor_type": "SCALAR",
    "duplicates": "identical_position",
}


class VertexIdentityError(ValueError):
    """Missing, unsupported or inconsistent persistent vertex identity."""


def _vertex_id(value):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not 1 <= value <= MAX_VERTEX_ID or int(value) != value):
        raise VertexIdentityError(f"Invalid ONUA3D vertex ID: {value!r}")
    return int(value)


def vertex_identity_mapping(manifest):
    """Flatten the canonical owner-scoped manifest table into ID -> UA origin.

    Legacy packages remain readable by the family loader, but are not eligible
    for this position-only contract until regenerated from their ua_family.
    This validates identity metadata only, not package hashes or native data.
    """
    if not isinstance(manifest, dict) or manifest.get("vertex_identity") != IDENTITY_CONTRACT:
        raise VertexIdentityError("Missing or unsupported persistent vertex mapping; "
                                  "regenerate the GLB from the embedded ua_family")
    nodes = manifest.get("nodes")
    if not isinstance(nodes, list):
        raise VertexIdentityError("Missing ONUA3D owner mapping")
    result, owners = {}, set()
    for node in nodes:
        if not isinstance(node, dict):
            raise VertexIdentityError("Invalid ONUA3D owner mapping")
        owner, vertices = node.get("owner_path"), node.get("vertex_instances")
        if not isinstance(owner, str) or not owner or owner in owners:
            raise VertexIdentityError("Missing or ambiguous ONUA3D owner_path")
        owners.add(owner)
        if not isinstance(vertices, dict):
            raise VertexIdentityError(f"{owner}: missing vertex_instances mapping")
        for key, origin in vertices.items():
            if not isinstance(key, str) or not key.isascii() or not key.isdecimal():
                raise VertexIdentityError(f"Invalid manifest vertex ID: {key!r}")
            ident = _vertex_id(int(key))
            if key != str(ident) or ident in result:
                raise VertexIdentityError(f"Ambiguous manifest vertex ID: {key}")
            if not isinstance(origin, dict):
                raise VertexIdentityError(f"ID {ident}: invalid native origin")
            for field in ("point_id", "polygon_id", "corner", "block_index"):
                value = origin.get(field)
                if type(value) is not int or value < (-1 if field == "block_index" else 0):
                    raise VertexIdentityError(f"ID {ident}: invalid {field}")
            if "vanm_frame" in origin and (type(origin["vanm_frame"]) is not int
                                           or origin["vanm_frame"] < 0):
                raise VertexIdentityError(f"ID {ident}: invalid vanm_frame")
            result[ident] = {**origin, "owner_path": owner}
    if not result:
        raise VertexIdentityError("Empty persistent vertex mapping")
    return result


def canonical_vertex_positions(manifest, primitives):
    """Validate decoded primitive attributes and return sorted ID -> POSITION.

    Each item supplies ATTRIBUTE (scalar values) and POSITION (triples), in
    matching accessor order. The caller decodes GLB accessors and checks the
    scene/placement/topology contract separately. Nodes, primitive order,
    normals and other rendering attributes are not identity keys.

    Blender may materialize multiple vertices for one ID (normal/UV splits).
    All copies MUST have exactly equal finite positions. No epsilon, averaging,
    nearest-point matching or arbitrary selection is performed. Different IDs
    at the same position remain different logical instances.
    """
    expected = vertex_identity_mapping(manifest)
    result = {}
    for primitive in primitives:
        if not isinstance(primitive, dict) or ATTRIBUTE not in primitive or "POSITION" not in primitive:
            raise VertexIdentityError("Primitive is missing persistent ID or POSITION")
        ids, positions = primitive[ATTRIBUTE], primitive["POSITION"]
        if (not isinstance(ids, (list, tuple)) or not isinstance(positions, (list, tuple))
                or len(ids) != len(positions)):
            raise VertexIdentityError("ID/POSITION accessor count mismatch")
        for raw_id, position in zip(ids, positions):
            ident = _vertex_id(raw_id)
            if ident not in expected:
                raise VertexIdentityError(f"Unknown ONUA3D vertex ID: {ident}")
            if (not isinstance(position, (list, tuple)) or len(position) != 3
                    or any(type(v) not in (int, float) or not math.isfinite(v) for v in position)):
                raise VertexIdentityError(f"ID {ident}: invalid POSITION")
            # Normalize signed zero so equivalent groups also have a stable
            # representation independent of encounter order.
            point = tuple(0.0 if v == 0 else float(v) for v in position)
            if ident in result and result[ident] != point:
                raise VertexIdentityError(f"ID {ident}: conflicting POSITION among copies")
            result[ident] = point
    missing = expected.keys() - result.keys()
    if missing:
        raise VertexIdentityError(f"Missing ONUA3D vertex IDs: {sorted(missing)[:8]}")
    return dict(sorted(result.items()))
