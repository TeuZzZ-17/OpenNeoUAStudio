"""Deterministic adaptive sphere approximation for Collision Editor meshes."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable, Sequence


Point3 = tuple[float, float, float]
Triangle3 = tuple[Point3, Point3, Point3]

# OpenNeoUA's runtime parser clamps compound collisions to this safety cap.
UNIT_COLL_MAX_COUNT = 512


@dataclass(frozen=True)
class AccuracyPreset:
    key: str
    label: str
    tolerance_fraction: float
    sample_spacing_fraction: float
    sample_cap: int
    minimum_gain_fraction: float


ACCURACY_PRESETS = (
    AccuracyPreset("low", "Low Accuracy", 0.120, 0.080, 2500, 0.0015),
    AccuracyPreset("medium", "Medium Accuracy", 0.080, 0.055, 4000, 0.0010),
    AccuracyPreset("high", "High Accuracy", 0.050, 0.038, 7000, 0.0007),
    AccuracyPreset("ultra", "Ultra Accuracy", 0.032, 0.026, 10000, 0.0005),
)
_PRESETS_BY_KEY = {preset.key: preset for preset in ACCURACY_PRESETS}


@dataclass(frozen=True)
class GeneratedSphere:
    x: float
    y: float
    z: float
    radius: float

    @property
    def center(self) -> Point3:
        return (self.x, self.y, self.z)


@dataclass(frozen=True)
class SphereGenerationResult:
    spheres: tuple[GeneratedSphere, ...]
    sample_count: int
    measured_error: float
    tolerance: float
    hit_safety_cap: bool


@dataclass
class _Cluster:
    indices: tuple[int, ...]
    sphere: GeneratedSphere
    error: float
    splittable: bool = True


def _sub(a: Point3, b: Point3) -> Point3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a: Point3, b: Point3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _length(vector: Point3) -> float:
    return math.sqrt(_dot(vector, vector))


def _distance(a: Point3, b: Point3) -> float:
    return _length(_sub(a, b))


def _cross(a: Point3, b: Point3) -> Point3:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _triangle_area(triangle: Triangle3) -> float:
    return 0.5 * _length(_cross(
        _sub(triangle[1], triangle[0]),
        _sub(triangle[2], triangle[0])))


def _percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = max(0, min(
        len(ordered) - 1,
        int(math.ceil(fraction * len(ordered))) - 1,
    ))
    return ordered[position]


def _radical_inverse_base_two(value: int) -> float:
    result = 0.0
    factor = 0.5
    while value:
        if value & 1:
            result += factor
        value >>= 1
        factor *= 0.5
    return result


def _clean_triangles(triangles: Iterable[Triangle3]) -> list[Triangle3]:
    clean: list[Triangle3] = []
    seen: set[tuple[Point3, Point3, Point3]] = set()
    for raw in triangles:
        if len(raw) != 3:
            continue
        try:
            triangle = tuple(
                tuple(float(value) for value in point) for point in raw)
        except (TypeError, ValueError):
            continue
        if any(len(point) != 3 for point in triangle):
            continue
        if not all(math.isfinite(value)
                   for point in triangle for value in point):
            continue
        typed = (triangle[0], triangle[1], triangle[2])
        if _triangle_area(typed) <= 1e-12:
            continue
        key = tuple(sorted(tuple(round(value, 9) for value in point)
                           for point in typed))
        if key in seen:
            continue
        seen.add(key)
        clean.append(typed)
    return clean


def _surface_samples(
        triangles: list[Triangle3], preset: AccuracyPreset,
) -> tuple[list[Point3], float, float]:
    vertices = [point for triangle in triangles for point in triangle]
    mins = tuple(min(point[axis] for point in vertices) for axis in range(3))
    maxs = tuple(max(point[axis] for point in vertices) for axis in range(3))
    diagonal = _distance(mins, maxs)
    if diagonal <= 1e-9:
        raise ValueError("The selected model has no usable geometric extent.")

    spacing = max(diagonal * preset.sample_spacing_fraction, 1e-6)
    areas = [_triangle_area(triangle) for triangle in triangles]
    raw_counts = [
        max(1, int(math.ceil(area / max(spacing * spacing * 0.75, 1e-12))))
        for area in areas
    ]
    raw_total = sum(raw_counts)
    scale = min(1.0, preset.sample_cap / max(1, raw_total))
    counts = [max(1, int(math.floor(count * scale)))
              for count in raw_counts]

    quantization = max(diagonal * 1e-9, 1e-9)
    samples: list[Point3] = []
    sample_keys: set[tuple[int, int, int]] = set()

    def add(point: Point3) -> None:
        key = tuple(int(round(value / quantization)) for value in point)
        if key in sample_keys:
            return
        sample_keys.add(key)
        samples.append(point)

    for triangle, count in zip(triangles, counts):
        a, b, c = triangle
        add(a)
        add(b)
        add(c)
        for index in range(count):
            # This low-discrepancy barycentric sequence is deterministic and
            # distributes samples by triangle area without relying on random.
            u = (index + 0.5) / count
            v = _radical_inverse_base_two(index + 1)
            root = math.sqrt(u)
            weights = (1.0 - root, root * (1.0 - v), root * v)
            add((
                weights[0] * a[0] + weights[1] * b[0] + weights[2] * c[0],
                weights[0] * a[1] + weights[1] * b[1] + weights[2] * c[1],
                weights[0] * a[2] + weights[1] * b[2] + weights[2] * c[2],
            ))
    if len(samples) < 4:
        raise ValueError("The selected model has too little usable surface geometry.")
    return samples, diagonal, spacing


_ERROR_DIRECTIONS: tuple[Point3, ...] = tuple(
    (x / length, y / length, z / length)
    for x in (-1.0, 0.0, 1.0)
    for y in (-1.0, 0.0, 1.0)
    for z in (-1.0, 0.0, 1.0)
    if (x, y, z) != (0.0, 0.0, 0.0)
    for length in (math.sqrt(x * x + y * y + z * z),)
)


def _fit_sphere(points: Sequence[Point3], indices: Sequence[int]) -> GeneratedSphere:
    centroid = tuple(
        sum(points[index][axis] for index in indices) / len(indices)
        for axis in range(3)
    )

    def farthest(origin: Point3) -> int:
        return max(indices, key=lambda index: (
            _distance(points[index], origin), -index))

    first = farthest(centroid)
    second = farthest(points[first])
    a, b = points[first], points[second]
    center = tuple((a[axis] + b[axis]) * 0.5 for axis in range(3))
    radius = _distance(a, b) * 0.5

    # Deterministic Ritter refinement keeps every assigned surface sample
    # inside while avoiding the oversized centroid bounding sphere.
    for index in indices:
        point = points[index]
        distance = _distance(point, center)
        if distance <= radius + 1e-12:
            continue
        new_radius = (radius + distance) * 0.5
        if distance > 1e-12:
            shift = (distance - radius) / (2.0 * distance)
            center = tuple(
                center[axis] + (point[axis] - center[axis]) * shift
                for axis in range(3)
            )
        radius = new_radius

    radius = max(_distance(points[index], center) for index in indices)
    return GeneratedSphere(center[0], center[1], center[2], radius)


def _sphere_error(
        points: Sequence[Point3], indices: Sequence[int],
        sphere: GeneratedSphere, spacing: float,
) -> float:
    center = sphere.center
    radial_slack = [
        max(0.0, sphere.radius - _distance(points[index], center))
        for index in indices
    ]
    radial_error = _percentile(radial_slack, 0.90)

    directional_gaps: list[float] = []
    for direction in _ERROR_DIRECTIONS:
        boundary = tuple(
            center[axis] + sphere.radius * direction[axis]
            for axis in range(3)
        )
        directional_gaps.append(min(
            _distance(boundary, points[index]) for index in indices))
    directional_error = max(
        0.0, _percentile(directional_gaps, 0.75) - spacing * 0.75)
    return max(radial_error, directional_error * 0.65)


def _make_cluster(
        points: Sequence[Point3], indices: Sequence[int], spacing: float,
) -> _Cluster:
    sphere = _fit_sphere(points, indices)
    error = _sphere_error(points, indices, sphere, spacing)
    return _Cluster(tuple(indices), sphere, error)


def _principal_axis(points: Sequence[Point3], indices: Sequence[int]) -> Point3:
    mean = tuple(
        sum(points[index][axis] for index in indices) / len(indices)
        for axis in range(3)
    )
    covariance = [[0.0] * 3 for _ in range(3)]
    for index in indices:
        delta = _sub(points[index], mean)
        for row in range(3):
            for column in range(3):
                covariance[row][column] += delta[row] * delta[column]
    start_axis = max(range(3), key=lambda axis: (covariance[axis][axis], -axis))
    vector = tuple(1.0 if axis == start_axis else 0.0 for axis in range(3))
    for _iteration in range(12):
        candidate = tuple(
            sum(covariance[row][column] * vector[column]
                for column in range(3))
            for row in range(3)
        )
        length = _length(candidate)
        if length <= 1e-15:
            break
        vector = tuple(value / length for value in candidate)
    for value in vector:
        if abs(value) <= 1e-12:
            continue
        if value < 0.0:
            vector = tuple(-component for component in vector)
        break
    return vector


def _split_cluster(
        points: Sequence[Point3], cluster: _Cluster, spacing: float,
) -> tuple[_Cluster, _Cluster] | None:
    # A sparse old/open mesh may leave just a few samples very far apart.  Such
    # a cluster must remain splittable down to individual samples; otherwise a
    # three-point outlier can survive as one enormous false-positive sphere.
    # The final radius floor below still prevents zero-radius output spheres.
    if len(cluster.indices) < 2 or cluster.sphere.radius <= spacing * 0.8:
        return None
    axis = _principal_axis(points, cluster.indices)
    ordered = sorted(
        cluster.indices,
        key=lambda index: (_dot(points[index], axis), index),
    )
    midpoint = len(ordered) // 2
    if midpoint < 1 or len(ordered) - midpoint < 1:
        return None
    left = _make_cluster(points, ordered[:midpoint], spacing)
    right = _make_cluster(points, ordered[midpoint:], spacing)
    return left, right


def _point_inside_sphere(
        point: Point3, sphere: GeneratedSphere, slack: float = 0.0) -> bool:
    return _distance(point, sphere.center) <= sphere.radius + slack


def _prune_redundant(
        points: Sequence[Point3], clusters: list[_Cluster],
        padding: float,
) -> list[_Cluster]:
    active = set(range(len(clusters)))
    # Prefer the tighter local spheres when two leaves cover the same samples.
    # Removing small leaves first would retain a redundant enclosing "balloon",
    # which is precisely the empty-volume failure this generator must avoid.
    order = sorted(
        active,
        key=lambda index: (-clusters[index].sphere.radius, index),
    )
    for index in order:
        if index not in active or len(active) <= 1:
            continue
        others = [candidate for candidate in active if candidate != index]
        if all(any(
            _point_inside_sphere(
                points[point_index], clusters[candidate].sphere,
                padding * 0.25)
            for candidate in others
        ) for point_index in clusters[index].indices):
            active.remove(index)
    return [cluster for index, cluster in enumerate(clusters) if index in active]


def generate_collision_spheres(
        triangles: Iterable[Triangle3], preset: str,
        *, max_spheres: int = UNIT_COLL_MAX_COUNT,
) -> SphereGenerationResult:
    """Approximate a model surface with deterministic adaptive spheres.

    The algorithm needs only the available surface. It does not assume a
    watertight or manifold mesh, which keeps old Urban Assault assets usable.
    """

    preset_key = str(preset).strip().casefold().replace(" accuracy", "")
    config = _PRESETS_BY_KEY.get(preset_key)
    if config is None:
        raise ValueError(f"Unknown collision-sphere accuracy preset: {preset}")
    safety_cap = max(1, min(int(max_spheres), UNIT_COLL_MAX_COUNT))
    clean = _clean_triangles(triangles)
    if not clean:
        raise ValueError("The selected model has no usable surface triangles.")
    samples, diagonal, spacing = _surface_samples(clean, config)
    tolerance = diagonal * config.tolerance_fraction
    minimum_gain = diagonal * config.minimum_gain_fraction

    leaves = [_make_cluster(samples, tuple(range(len(samples))), spacing)]
    while len(leaves) < safety_cap:
        candidates = [
            (cluster.error, cluster.sphere.radius, -index, index)
            for index, cluster in enumerate(leaves)
            if (cluster.splittable
                and cluster.error > tolerance + minimum_gain)
        ]
        if not candidates:
            break
        _error, _radius, _stable, index = max(candidates)
        parent = leaves[index]
        children = _split_cluster(samples, parent, spacing)
        if children is None:
            parent.splittable = False
            continue
        # A principal-axis split may need more than one level before its
        # geometric benefit becomes visible (concave and open UA meshes often
        # do).  Rejecting a split on its immediate child error can therefore
        # strand a very large sphere.  The tolerance plus minimum-gain band
        # above is the stable negligible-benefit stop instead.
        leaves[index:index + 1] = list(children)

    hit_safety_cap = (
        len(leaves) >= safety_cap
        and any(cluster.error > tolerance and cluster.splittable
                for cluster in leaves)
    )
    padding = min(spacing * 0.30, tolerance * 0.10)
    leaves = _prune_redundant(samples, leaves, padding)
    spheres = [
        GeneratedSphere(
            cluster.sphere.x, cluster.sphere.y, cluster.sphere.z,
            max(cluster.sphere.radius + padding, spacing * 0.25),
        )
        for cluster in leaves
    ]
    spheres.sort(key=lambda sphere: (
        round(sphere.x, 9), round(sphere.y, 9), round(sphere.z, 9),
        round(sphere.radius, 9),
    ))
    measured_error = max(
        (cluster.error for cluster in leaves), default=0.0) / diagonal
    return SphereGenerationResult(
        tuple(spheres), len(samples), measured_error,
        config.tolerance_fraction, hit_safety_cap)
