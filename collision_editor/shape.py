"""Collision-shape files and isolated deterministic CoACD baking."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Iterable


MAX_HULLS = 64
MAX_VERTICES_PER_HULL = 128
_SOURCE_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

Point3 = tuple[float, float, float]
Triangle3 = tuple[Point3, Point3, Point3]


@dataclass(frozen=True)
class CollisionHull:
    vertices: tuple[Point3, ...]
    faces: tuple[tuple[int, int, int], ...]


@dataclass(frozen=True)
class CollisionShape:
    source: str
    source_hash: str
    scale: Point3
    rotation: Point3
    hulls: tuple[CollisionHull, ...]
    version: int = 1
    asset_set: int = 0


@dataclass(frozen=True)
class ShapeMetrics:
    sampled_surface_distance: float
    sampled_source_to_shape: float
    sampled_shape_to_source: float
    sample_count: int


@dataclass(frozen=True)
class ShapeGenerationResult:
    shape: CollisionShape
    metrics: ShapeMetrics
    warnings: tuple[str, ...] = ()


def _point3(values, label: str) -> Point3:
    if len(values) != 3:
        raise ValueError(f"{label} must contain exactly three coordinates.")
    point = tuple(float(value) for value in values)
    if not all(math.isfinite(value) for value in point):
        raise ValueError(f"{label} must contain finite coordinates.")
    return point


def _fmt(value: float) -> str:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("Collision shape coordinates must be finite.")
    return format(value, ".17g")


def _parse_vector(value: str, label: str) -> Point3:
    pieces = value.strip().split("_")
    try:
        return _point3(pieces, label)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {label}: {value!r}: {exc}") from exc


def _hull_volume(hull: CollisionHull) -> float:
    volume = 0.0
    for a, b, c in hull.faces:
        pa, pb, pc = hull.vertices[a], hull.vertices[b], hull.vertices[c]
        volume += (
            pa[0] * (pb[1] * pc[2] - pb[2] * pc[1])
            + pa[1] * (pb[2] * pc[0] - pb[0] * pc[2])
            + pa[2] * (pb[0] * pc[1] - pb[1] * pc[0])
        ) / 6.0
    return volume


def validate_hull(hull: CollisionHull) -> None:
    """Reject malformed or non-convex hulls before they enter a project."""

    if not 4 <= len(hull.vertices) <= MAX_VERTICES_PER_HULL:
        raise ValueError(
            f"A collision hull must contain 4..{MAX_VERTICES_PER_HULL} vertices.")
    if len(hull.faces) < 4:
        raise ValueError("A collision hull must contain at least four faces.")
    vertices = tuple(_point3(point, "vertex") for point in hull.vertices)
    if any(math.hypot(*point) > 1e7 for point in vertices):
        raise ValueError(
            "Collision hull coordinates must be within the runtime limit of 1e7.")
    if len(set(vertices)) != len(vertices):
        raise ValueError("A collision hull contains duplicate vertices.")
    extent = max(
        max(point[axis] for point in vertices)
        - min(point[axis] for point in vertices)
        for axis in range(3)
    )
    if not math.isfinite(extent) or extent <= 0.0:
        raise ValueError("A collision hull has no volume.")
    # CoACD returns float-backed vertices. A few face-plane distances can
    # therefore land just beyond exact double-precision coplanarity even for
    # convex output. Match the runtime's scale-aware collision-plane tolerance
    # while still rejecting geometrically concave faces.
    epsilon = max(extent * 1e-6, 1e-5)
    edge_counts: dict[tuple[int, int], list[int]] = {}
    for face in hull.faces:
        if len(face) != 3:
            raise ValueError("Collision hull faces must be triangles.")
        a, b, c = (int(index) for index in face)
        if min(a, b, c) < 0 or max(a, b, c) >= len(vertices):
            raise ValueError("A collision hull face has an invalid vertex index.")
        if len({a, b, c}) != 3:
            raise ValueError("A collision hull contains a degenerate face.")
        pa, pb, pc = vertices[a], vertices[b], vertices[c]
        ab = tuple(pb[i] - pa[i] for i in range(3))
        ac = tuple(pc[i] - pa[i] for i in range(3))
        normal = (
            ab[1] * ac[2] - ab[2] * ac[1],
            ab[2] * ac[0] - ab[0] * ac[2],
            ab[0] * ac[1] - ab[1] * ac[0],
        )
        normal_length = math.sqrt(sum(value * value for value in normal))
        if normal_length <= epsilon * epsilon:
            raise ValueError("A collision hull contains a zero-area face.")
        signed = [
            sum(normal[i] * (point[i] - pa[i]) for i in range(3))
            for point in vertices
        ]
        plane_tolerance = normal_length * epsilon
        if max(signed) > plane_tolerance and min(signed) < -plane_tolerance:
            raise ValueError("A collision hull is not convex.")
        for start, end in ((a, b), (b, c), (c, a)):
            key = (min(start, end), max(start, end))
            count = edge_counts.setdefault(key, [0, 0])
            count[0] += 1
            count[1] += 1 if start < end else -1
    if any(count != [2, 0] for count in edge_counts.values()):
        raise ValueError("A collision hull must have closed, consistently wound edges.")
    if abs(_hull_volume(hull)) <= extent ** 3 * 1e-12:
        raise ValueError("A collision hull has zero volume.")


def validate_shape(shape: CollisionShape) -> None:
    if shape.version != 1:
        raise ValueError(f"Unsupported collision shape version: {shape.version}.")
    if not _SOURCE_RE.fullmatch(shape.source):
        raise ValueError("Collision shape source must be an identifier without spaces.")
    if not _HASH_RE.fullmatch(shape.source_hash):
        raise ValueError("Collision shape source_hash must be a SHA-256 hex digest.")
    scale = _point3(shape.scale, "scale")
    if any(value < 0.0 for value in scale):
        raise ValueError("Collision shape scale cannot be negative.")
    _point3(shape.rotation, "rotation")
    try:
        asset_set = int(shape.asset_set)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Collision shape asset_set must be an integer.") from exc
    if isinstance(shape.asset_set, bool) or asset_set != shape.asset_set:
        raise ValueError("Collision shape asset_set must be an integer.")
    if not 0 <= asset_set <= 255:
        raise ValueError("Collision shape asset_set must be in 0..255.")
    if not 1 <= len(shape.hulls) <= MAX_HULLS:
        raise ValueError(f"A collision shape must contain 1..{MAX_HULLS} hulls.")
    for hull in shape.hulls:
        validate_hull(hull)


def collision_shape_text(shape: CollisionShape) -> str:
    """Serialize a validated collision shape using the runtime text format."""

    validate_shape(shape)
    lines = [
        "begin_collision_shape",
        f"version = {shape.version}",
        f"source = {shape.source}",
        f"source_hash = {shape.source_hash}",
        "scale = " + "_".join(_fmt(value) for value in shape.scale),
        "rotation = " + "_".join(
            _fmt(value) for value in shape.rotation),
    ]
    if shape.asset_set:
        lines.append(f"asset_set = {int(shape.asset_set)}")
    for hull in shape.hulls:
        lines.append("begin_hull")
        lines.extend(
            "vertex = " + "_".join(_fmt(value) for value in point)
            for point in hull.vertices
        )
        lines.extend(
            f"face = {a}_{b}_{c}" for a, b, c in hull.faces
        )
        lines.append("end")
    lines.append("end")
    return "\n".join(lines) + "\n"


def parse_collision_shape(text: str) -> CollisionShape:
    """Read and validate one complete collision-shape document."""

    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    if not lines or lines[0] != "begin_collision_shape":
        raise ValueError("Missing begin_collision_shape.")
    if len(lines) < 2 or lines[-1] != "end":
        raise ValueError("Missing final end for collision shape.")
    headers: dict[str, str] = {}
    hulls: list[CollisionHull] = []
    current_vertices: list[Point3] | None = None
    current_faces: list[tuple[int, int, int]] | None = None
    ended = False
    for row in lines[1:-1]:
        if row == "begin_hull":
            if current_vertices is not None or ended:
                raise ValueError("Nested or misplaced begin_hull.")
            current_vertices, current_faces = [], []
            continue
        if row == "end":
            if current_vertices is None or current_faces is None:
                raise ValueError("Misplaced end in collision shape.")
            hulls.append(CollisionHull(
                tuple(current_vertices), tuple(current_faces)))
            current_vertices = current_faces = None
            if len(hulls) > MAX_HULLS:
                raise ValueError(f"Collision shape exceeds {MAX_HULLS} hulls.")
            continue
        if "=" not in row:
            raise ValueError(f"Invalid collision-shape row: {row!r}.")
        key, value = (piece.strip() for piece in row.split("=", 1))
        if current_vertices is not None:
            if key == "vertex":
                if len(current_vertices) >= MAX_VERTICES_PER_HULL:
                    raise ValueError(
                        f"A collision hull exceeds {MAX_VERTICES_PER_HULL} vertices.")
                current_vertices.append(_parse_vector(value, "vertex"))
            elif key == "face":
                if len(current_faces) >= MAX_VERTICES_PER_HULL * 4:
                    raise ValueError("Collision hull has too many faces.")
                try:
                    indices = tuple(int(piece) for piece in value.split("_"))
                except ValueError as exc:
                    raise ValueError(f"Invalid face indices: {value!r}.") from exc
                if len(indices) != 3:
                    raise ValueError("Collision hull faces must contain three indices.")
                current_faces.append(indices)
            else:
                raise ValueError(f"Unknown collision-hull field: {key!r}.")
        else:
            if key in headers:
                raise ValueError(f"Duplicate collision-shape field: {key!r}.")
            if key not in {
                    "version", "source", "source_hash",
                    "scale", "rotation", "asset_set"}:
                raise ValueError(f"Unknown collision-shape field: {key!r}.")
            headers[key] = value
    if current_vertices is not None or current_faces is not None:
        raise ValueError("Missing end for collision hull.")
    required = {
        "version", "source", "source_hash", "scale", "rotation"}
    if set(headers) - {"asset_set"} != required:
        raise ValueError(
            "Collision shape requires version, source, source_hash, "
            "scale and rotation.")
    try:
        version = int(headers["version"])
    except ValueError as exc:
        raise ValueError("Invalid collision-shape version.") from exc
    try:
        asset_set = int(headers.get("asset_set", "0"))
    except ValueError as exc:
        raise ValueError(
            "Collision shape asset_set must be an integer.") from exc
    shape = CollisionShape(
        source=headers["source"],
        source_hash=headers["source_hash"].lower(),
        scale=_parse_vector(headers["scale"], "scale"),
        rotation=_parse_vector(
            headers["rotation"], "rotation"),
        hulls=tuple(hulls), version=version, asset_set=asset_set,
    )
    validate_shape(shape)
    return shape


def read_collision_shape(path: str | Path) -> CollisionShape:
    return parse_collision_shape(Path(path).read_text(encoding="utf-8-sig"))


def write_collision_shape(path: str | Path, shape: CollisionShape) -> None:
    Path(path).write_text(collision_shape_text(shape), encoding="utf-8", newline="\n")


def geometry_fingerprint(parts: Iterable[tuple[str, Iterable[Triangle3]]]) -> str:
    """Hash the chosen raw local geometry, including part identity and order."""

    canonical = []
    for owner, triangles in parts:
        rows = []
        for triangle in triangles:
            rows.append([
                [_fmt(value) for value in _point3(point, "source vertex")]
                for point in triangle
            ])
        canonical.append({"owner": str(owner), "triangles": rows})
    payload = json.dumps(
        canonical, ensure_ascii=True, separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def visual_transform_point(
        point: Point3, scale: Point3, rotation: Point3) -> Point3:
    """Apply the engine's scale-then-Euler visual matrix once before baking."""

    x, y, z = _point3(point, "source vertex")
    sx, sy, sz = (math.radians(value % 360.0) for value in rotation)
    sin_x, cos_x = math.sin(sx), math.cos(sx)
    sin_y, cos_y = math.sin(sy), math.cos(sy)
    sin_z, cos_z = math.sin(sz), math.cos(sz)
    kx, ky, kz = _point3(scale, "scale")
    matrix = (
        ((cos_z * cos_y - sin_z * sin_x * sin_y) * kx,
         -sin_z * cos_x * kx,
         (cos_z * sin_y + sin_z * sin_x * cos_y) * kx),
        ((sin_z * cos_y + cos_z * sin_x * sin_y) * ky,
         cos_z * cos_x * ky,
         (sin_z * sin_y - cos_z * sin_x * cos_y) * ky),
        (-cos_x * sin_y * kz, sin_x * kz, cos_x * cos_y * kz),
    )
    return tuple(sum(matrix[row][axis] * (x, y, z)[axis]
                     for axis in range(3)) for row in range(3))


def _point_triangle_distance_squared(p: Point3, a: Point3, b: Point3,
                                     c: Point3) -> float:
    """Return the squared distance from a point to a triangle."""

    ab = tuple(b[i] - a[i] for i in range(3))
    ac = tuple(c[i] - a[i] for i in range(3))
    ap = tuple(p[i] - a[i] for i in range(3))
    d1 = sum(ab[i] * ap[i] for i in range(3))
    d2 = sum(ac[i] * ap[i] for i in range(3))
    if d1 <= 0.0 and d2 <= 0.0:
        return sum(value * value for value in ap)
    bp = tuple(p[i] - b[i] for i in range(3))
    d3 = sum(ab[i] * bp[i] for i in range(3))
    d4 = sum(ac[i] * bp[i] for i in range(3))
    if d3 >= 0.0 and d4 <= d3:
        return sum(value * value for value in bp)
    vc = d1 * d4 - d3 * d2
    if vc <= 0.0 and d1 >= 0.0 and d3 <= 0.0:
        t = d1 / (d1 - d3)
        q = tuple(a[i] + t * ab[i] for i in range(3))
        return sum((p[i] - q[i]) ** 2 for i in range(3))
    cp = tuple(p[i] - c[i] for i in range(3))
    d5 = sum(ab[i] * cp[i] for i in range(3))
    d6 = sum(ac[i] * cp[i] for i in range(3))
    if d6 >= 0.0 and d5 <= d6:
        return sum(value * value for value in cp)
    vb = d5 * d2 - d1 * d6
    if vb <= 0.0 and d2 >= 0.0 and d6 <= 0.0:
        t = d2 / (d2 - d6)
        q = tuple(a[i] + t * ac[i] for i in range(3))
        return sum((p[i] - q[i]) ** 2 for i in range(3))
    va = d3 * d6 - d5 * d4
    if va <= 0.0 and (d4 - d3) >= 0.0 and (d5 - d6) >= 0.0:
        t = (d4 - d3) / ((d4 - d3) + (d5 - d6))
        q = tuple(b[i] + t * (c[i] - b[i]) for i in range(3))
        return sum((p[i] - q[i]) ** 2 for i in range(3))
    denominator = 1.0 / (va + vb + vc)
    v, w = vb * denominator, vc * denominator
    q = tuple(a[i] + ab[i] * v + ac[i] * w for i in range(3))
    return sum((p[i] - q[i]) ** 2 for i in range(3))


def _surface_samples(triangles: Iterable[Triangle3], limit: int = 512) -> list[Point3]:
    samples: list[Point3] = []
    for triangle in triangles:
        a, b, c = tuple(_point3(point, "sample vertex") for point in triangle)
        samples.extend((
            a, b, c,
            tuple((a[i] + b[i]) * 0.5 for i in range(3)),
            tuple((b[i] + c[i]) * 0.5 for i in range(3)),
            tuple((c[i] + a[i]) * 0.5 for i in range(3)),
            tuple((a[i] + b[i] + c[i]) / 3.0 for i in range(3)),
        ))
    if len(samples) <= limit:
        return samples
    stride = len(samples) / limit
    return [samples[int(index * stride)] for index in range(limit)]


def _closest_surface_distance(point: Point3, triangles: list[Triangle3]) -> float:
    return math.sqrt(min(
        _point_triangle_distance_squared(point, *triangle)
        for triangle in triangles
    ))


def _convex_hull_planes(hull: CollisionHull):
    center = tuple(
        sum(point[axis] for point in hull.vertices) / len(hull.vertices)
        for axis in range(3))
    planes = []
    for a, b, c in hull.faces:
        pa, pb, pc = hull.vertices[a], hull.vertices[b], hull.vertices[c]
        ab = tuple(pb[i] - pa[i] for i in range(3))
        ac = tuple(pc[i] - pa[i] for i in range(3))
        normal = (
            ab[1] * ac[2] - ab[2] * ac[1],
            ab[2] * ac[0] - ab[0] * ac[2],
            ab[0] * ac[1] - ab[1] * ac[0],
        )
        length = math.sqrt(sum(value * value for value in normal))
        if length == 0.0:
            continue
        inward = sum(normal[i] * (center[i] - pa[i]) for i in range(3)) > 0.0
        sign = -1.0 if inward else 1.0
        outward = tuple(sign * value / length for value in normal)
        planes.append((pa, outward))
    return tuple(planes)


def _inside_convex_hull(point: Point3, planes, tolerance: float) -> bool:
    return all(
        sum(normal[i] * (point[i] - origin[i]) for i in range(3))
        <= tolerance
        for origin, normal in planes
    )


def _exterior_hull_surface(hulls, limit: int):
    """Return sampled union boundary and its exposed source triangles.

    Each surface sample is nudged along its face's outward normal. Samples
    buried inside any other convex hull are internal cuts of the union and
    cannot contribute to either directed shape-distance measurement.
    """

    hulls = tuple(hulls)
    all_points = [point for hull in hulls for point in hull.vertices]
    extent = max((
        max(point[axis] for point in all_points)
        - min(point[axis] for point in all_points)
        for axis in range(3)), default=0.0)
    tolerance = max(1e-5, extent * 1e-6)
    offset = tolerance * 4.0
    planes = [_convex_hull_planes(hull) for hull in hulls]
    target_triangles = []
    exterior_samples = []
    for hull_index, hull in enumerate(hulls):
        other_hulls = [
            planes[index] for index in range(len(hulls))
            if index != hull_index
        ]
        for a, b, c in hull.faces:
            triangle = (
                hull.vertices[a], hull.vertices[b], hull.vertices[c])
            pa, pb, pc = triangle
            ab = tuple(pb[i] - pa[i] for i in range(3))
            ac = tuple(pc[i] - pa[i] for i in range(3))
            normal = (
                ab[1] * ac[2] - ab[2] * ac[1],
                ab[2] * ac[0] - ab[0] * ac[2],
                ab[0] * ac[1] - ab[1] * ac[0],
            )
            length = math.sqrt(sum(value * value for value in normal))
            if length == 0.0:
                continue
            center = tuple(
                sum(point[axis] for point in hull.vertices)
                / len(hull.vertices)
                for axis in range(3))
            if sum(normal[i] * (center[i] - pa[i]) for i in range(3)) > 0.0:
                normal = tuple(-value for value in normal)
            outward = tuple(value / length for value in normal)
            samples = (
                pa, pb, pc,
                tuple((pa[i] + pb[i]) * 0.5 for i in range(3)),
                tuple((pb[i] + pc[i]) * 0.5 for i in range(3)),
                tuple((pc[i] + pa[i]) * 0.5 for i in range(3)),
                tuple((pa[i] + pb[i] + pc[i]) / 3.0 for i in range(3)),
                tuple((2.0 * pa[i] + pb[i] + pc[i]) * 0.25
                      for i in range(3)),
                tuple((pa[i] + 2.0 * pb[i] + pc[i]) * 0.25
                      for i in range(3)),
                tuple((pa[i] + pb[i] + 2.0 * pc[i]) * 0.25
                      for i in range(3)),
            )
            exposed = False
            for point in samples:
                probe = tuple(
                    point[i] + outward[i] * offset for i in range(3))
                if any(_inside_convex_hull(probe, other, tolerance)
                       for other in other_hulls):
                    continue
                exterior_samples.append(point)
                exposed = True
            if exposed:
                target_triangles.append(triangle)
    if len(exterior_samples) > limit:
        stride = len(exterior_samples) / limit
        exterior_samples = [
            exterior_samples[int(index * stride)] for index in range(limit)]
    return target_triangles, exterior_samples


def sampled_surface_metrics(
        source_triangles: Iterable[Triangle3], hulls: Iterable[CollisionHull],
        limit: int = 512) -> ShapeMetrics:
    """Measure deterministic bidirectional distance using bounded surface samples."""

    source = list(source_triangles)
    hulls = tuple(hulls)
    target, target_samples = _exterior_hull_surface(hulls, limit)
    if not source or not target or not target_samples:
        raise ValueError("Both source and generated geometry need faces.")
    source_samples = _surface_samples(source, limit)
    forward = [
        _closest_surface_distance(point, target) for point in source_samples]
    backward = [
        _closest_surface_distance(point, source) for point in target_samples]
    # The reported value is symmetric sampled Hausdorff distance. The two
    # directed values are retained so callers can explain which side dominates.
    forward_max = max(forward)
    backward_max = max(backward)
    return ShapeMetrics(
        sampled_surface_distance=max(forward_max, backward_max),
        sampled_source_to_shape=forward_max,
        sampled_shape_to_source=backward_max,
        sample_count=len(source_samples) + len(target_samples),
    )


def _deduplicate_triangles(parts, scale, rotation):
    prepared = []
    all_triangles = []
    for owner, triangles in parts:
        transformed = []
        for triangle in triangles:
            points = tuple(
                visual_transform_point(point, scale, rotation)
                for point in triangle
            )
            ab = tuple(points[1][i] - points[0][i] for i in range(3))
            ac = tuple(points[2][i] - points[0][i] for i in range(3))
            normal = (
                ab[1] * ac[2] - ab[2] * ac[1],
                ab[2] * ac[0] - ab[0] * ac[2],
                ab[0] * ac[1] - ab[1] * ac[0],
            )
            if sum(value * value for value in normal) <= 1e-60:
                # The input can contain repeated vertices after triangulation;
                # such a triangle has no surface to approximate.
                continue
            transformed.append(points)
            all_triangles.append(points)
        if transformed:
            prepared.append((str(owner), transformed))
    if not prepared:
        raise ValueError("Selected geometry contains no non-degenerate triangles.")
    return prepared, all_triangles


def _source_topology_warnings(parts) -> tuple[str, ...]:
    """Describe source mesh gaps that CoACD auto-preprocessing must repair."""

    warnings = []
    for owner, triangles in parts:
        edges: dict[tuple[Point3, Point3], int] = {}
        for triangle in triangles:
            points = tuple(_point3(point, "source vertex")
                           for point in triangle)
            for start, end in ((points[0], points[1]),
                               (points[1], points[2]),
                               (points[2], points[0])):
                key = (start, end) if start < end else (end, start)
                edges[key] = edges.get(key, 0) + 1
        boundary = sum(count == 1 for count in edges.values())
        non_manifold = sum(count > 2 for count in edges.values())
        if boundary or non_manifold:
            warnings.append(
                f"{owner}: source mesh has {boundary} open and "
                f"{non_manifold} non-manifold edges; exact T-junction and "
                "simple planar-loop repair is attempted before CoACD "
                "auto-preprocess handles any remaining gaps.")
    return tuple(warnings)


def generate_collision_shape(
        parts: Iterable[tuple[str, Iterable[Triangle3]]], *, source: str,
        scale: Point3 = (1.0, 1.0, 1.0),
        rotation: Point3 = (0.0, 0.0, 0.0),
        asset_set: int = 0,
        quality: str = "normal") -> ShapeGenerationResult:
    """Bake each selected part separately with CoACD 1.0.14."""

    try:
        import numpy as np
        import coacd
    except ImportError as exc:
        if getattr(sys, "frozen", False):
            guidance = (
                "The collision library bundled with OpenNeoUA Studio could "
                "not be loaded.")
        else:
            guidance = (
                "Install the Studio requirements with this source Python "
                "interpreter.")
        raise RuntimeError(
            f"CoACD 1.0.14 is unavailable: {exc}. {guidance}") from exc
    raw_parts = [(str(owner), list(triangles)) for owner, triangles in parts]
    if not raw_parts:
        raise ValueError("Select at least one geometry part.")
    clean_source = re.sub(r"[^A-Za-z0-9_.-]+", "_", source.strip()).strip("_.-")
    if not clean_source:
        clean_source = "Model"
    fingerprint = geometry_fingerprint(raw_parts)
    scale = _point3(scale, "scale")
    rotation = _point3(rotation, "rotation")
    prepared, transformed_source = _deduplicate_triangles(
        raw_parts, scale, rotation)
    prepared_owners = {owner for owner, _triangles in prepared}
    empty_owners = [
        owner for owner, triangles in raw_parts
        if owner not in prepared_owners
    ]
    if empty_owners:
        raise ValueError(
            "Selected part(s) contain no non-degenerate triangles: "
            + ", ".join(empty_owners))
    warnings = list(_source_topology_warnings(prepared))
    from collision_editor.mesh_parts import prepare_component_mesh

    prepared_for_bake = []
    for owner, triangles in prepared:
        repaired, repair_warnings = prepare_component_mesh(triangles)
        prepared_for_bake.append((owner, list(repaired)))
        warnings.extend(f"{owner}: {warning}" for warning in repair_warnings)
    prepared = prepared_for_bake
    if quality not in {"low", "normal", "high"}:
        raise ValueError(f"Unsupported collision-shape quality: {quality}.")
    settings = {
        "threshold": 0.05,
        "preprocess_mode": "auto",
        "preprocess_resolution": 32,
        "resolution": 1000,
        "mcts_nodes": 10,
        "mcts_iterations": 40,
        "decimate": True,
        "max_ch_vertex": MAX_VERTICES_PER_HULL,
        "seed": 0,
        # CoACD extrudes only vertices shared by adjacent decomposed faces;
        # its API leaves external faces unchanged. This closes small numeric
        # seams between hulls without expanding the model's outer silhouette.
        "extrude": True,
        "extrude_margin": 0.001,
    }
    if quality == "low":
        settings.update(
            threshold=0.10, preprocess_resolution=24, resolution=500,
            mcts_nodes=5, mcts_iterations=20)
    elif quality == "high":
        settings.update(
            preprocess_resolution=50, resolution=2000,
            mcts_nodes=20, mcts_iterations=150)
    hulls: list[CollisionHull] = []
    for _owner, triangles in prepared:
        vertices: list[Point3] = []
        vertex_map: dict[Point3, int] = {}
        indices: list[tuple[int, int, int]] = []
        for triangle in triangles:
            face = []
            for point in triangle:
                index = vertex_map.get(point)
                if index is None:
                    index = len(vertices)
                    vertex_map[point] = index
                    vertices.append(point)
                face.append(index)
            if len(set(face)) == 3:
                indices.append(tuple(face))
        if len(vertices) < 4 or not indices:
            continue
        mesh = coacd.Mesh(
            np.asarray(vertices, dtype=np.float64),
            np.asarray(indices, dtype=np.int32),
        )
        # 65 is a sentinel: returning it means the runtime cap was reached,
        # so fail rather than silently dropping or truncating hulls.
        # Keep native CoACD progress logs off the worker's stdout, which is a
        # single machine-readable response. Redirecting the Windows CRT file
        # descriptor breaks CoACD's console sink, so use its logging API.
        set_log_level = getattr(coacd, "set_log_level", None)
        if callable(set_log_level):
            set_log_level("off")
        baked = coacd.run_coacd(
            mesh, max_convex_hull=MAX_HULLS + 1, **settings)
        if len(baked) >= MAX_HULLS + 1:
            raise ValueError(
                f"Geometry requires more than {MAX_HULLS} collision hulls.")
        for baked_vertices, baked_faces in baked:
            candidate = CollisionHull(
                tuple(_point3(point, "generated vertex")
                      for point in baked_vertices),
                tuple(tuple(int(index) for index in face)
                      for face in baked_faces),
            )
            validate_hull(candidate)
            hulls.append(candidate)
            if len(hulls) > MAX_HULLS:
                raise ValueError(
                    f"Geometry requires more than {MAX_HULLS} collision hulls.")
    shape = CollisionShape(
        source=clean_source,
        source_hash=fingerprint,
        scale=scale,
        rotation=rotation,
        hulls=tuple(hulls),
        asset_set=asset_set,
    )
    validate_shape(shape)
    metrics = sampled_surface_metrics(transformed_source, shape.hulls)
    return ShapeGenerationResult(shape, metrics, tuple(warnings))


def _write_worker_response(path: str | Path, response: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n",
                dir=target.parent, prefix=target.name + ".",
                suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(response, handle, allow_nan=False,
                      ensure_ascii=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _worker_response(request: dict) -> dict:
    result = generate_collision_shape(
        [
            (part["owner"], part["triangles"])
            for part in request["parts"]
        ],
        source=request["source"],
        scale=request["scale"],
        rotation=request["rotation"],
        asset_set=request.get("asset_set", 0),
        quality=request.get("quality", "normal"),
    )
    return {
        "shape": collision_shape_text(result.shape),
        "metrics": {
            "sampled_surface_distance": result.metrics.sampled_surface_distance,
            "sampled_source_to_shape": result.metrics.sampled_source_to_shape,
            "sampled_shape_to_source": result.metrics.sampled_shape_to_source,
            "sample_count": result.metrics.sample_count,
        },
        "warnings": list(result.warnings),
    }


def worker_main(input_path: str | Path | None = None,
                output_path: str | Path | None = None) -> int:
    """Bake one JSON job; production calls use atomic input/output files.

    The stdin/stdout branch remains only for source-level tests and manual
    diagnostics. Frozen GUI workers never depend on console streams.
    """

    file_mode = input_path is not None or output_path is not None
    try:
        if file_mode:
            if input_path is None or output_path is None:
                raise ValueError("Both input and output JSON paths are required.")
            request = json.loads(
                Path(input_path).read_text(encoding="utf-8"))
        else:
            if getattr(sys, "stdin", None) is None:
                raise RuntimeError("Worker input must use JSON files.")
            request = json.load(sys.stdin)
        response = _worker_response(request)
        if file_mode:
            _write_worker_response(output_path, response)
        else:
            if getattr(sys, "stdout", None) is None:
                raise RuntimeError("Worker output must use JSON files.")
            sys.stdout.write(json.dumps(response, allow_nan=False))
            sys.stdout.flush()
        return 0
    except Exception as exc:
        if file_mode and output_path is not None:
            try:
                _write_worker_response(output_path, {"error": str(exc)})
            except Exception:
                pass
        error_stream = getattr(sys, "stderr", None)
        if error_stream is not None:
            try:
                error_stream.write(
                    f"Collision shape generation failed: {exc}\n")
                error_stream.flush()
            except Exception:
                pass
        return 2
