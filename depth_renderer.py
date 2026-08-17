"""Depth-correct polygon ordering for the QPainter model renderer.

QPainter has no depth buffer.  Sorting complete faces (or even complete
triangles) by their average camera depth fails when projected geometry
overlaps or intersects.  This module builds a small camera-space BSP tree,
splits spanning polygons, and returns exact back-to-front pieces while
interpolating their per-vertex attributes.

The input is expected to be convex (the viewer feeds runtime-style fan
triangles).  Splitting a convex polygon by a plane preserves convexity.
"""

from __future__ import annotations

from dataclasses import dataclass
import math


Vec3 = tuple[float, float, float]
Attributes = tuple[float, ...]
_PLANE_EPSILON = 1e-7


@dataclass(frozen=True)
class CameraPolygon:
    """One convex camera-space polygon and its render payload."""

    vertices: tuple[Vec3, ...]
    attributes: tuple[Attributes, ...]
    payload: object
    source_order: int = 0

    def mean_z(self) -> float:
        return sum(vertex[2] for vertex in self.vertices) / len(self.vertices)


@dataclass(frozen=True)
class _Plane:
    normal: Vec3
    offset: float

    def distance(self, point: Vec3) -> float:
        return (
            self.normal[0] * point[0]
            + self.normal[1] * point[1]
            + self.normal[2] * point[2]
            + self.offset
        )


@dataclass
class _BspNode:
    plane: _Plane
    coplanar: list[CameraPolygon]
    front: "_BspNode | None" = None
    back: "_BspNode | None" = None


def _plane_for(polygon: CameraPolygon) -> _Plane | None:
    vertices = polygon.vertices
    if len(vertices) < 3:
        return None
    origin = vertices[0]
    for index in range(1, len(vertices) - 1):
        first = vertices[index]
        second = vertices[index + 1]
        ab = tuple(first[axis] - origin[axis] for axis in range(3))
        ac = tuple(second[axis] - origin[axis] for axis in range(3))
        normal = (
            ab[1] * ac[2] - ab[2] * ac[1],
            ab[2] * ac[0] - ab[0] * ac[2],
            ab[0] * ac[1] - ab[1] * ac[0],
        )
        length = math.sqrt(sum(value * value for value in normal))
        if length <= 1e-12:
            continue
        normal = tuple(value / length for value in normal)
        return _Plane(
            normal,
            -sum(normal[axis] * origin[axis] for axis in range(3)),
        )
    return None


def _classify(polygon: CameraPolygon, plane: _Plane,
              epsilon: float = _PLANE_EPSILON) -> int:
    """Return 1 front, -1 back, 0 coplanar, 2 spanning."""

    front = False
    back = False
    for vertex in polygon.vertices:
        distance = plane.distance(vertex)
        front = front or distance > epsilon
        back = back or distance < -epsilon
        if front and back:
            return 2
    if front:
        return 1
    if back:
        return -1
    return 0


def _interpolate(first, second, factor: float):
    return tuple(
        start + (end - start) * factor
        for start, end in zip(first, second)
    )


def _clip_half_space(
        polygon: CameraPolygon, plane: _Plane, keep_front: bool,
        epsilon: float = _PLANE_EPSILON) -> CameraPolygon | None:
    vertices: list[Vec3] = []
    attributes: list[Attributes] = []
    source_vertices = polygon.vertices
    source_attributes = polygon.attributes

    previous = source_vertices[-1]
    previous_attributes = source_attributes[-1]
    previous_distance = plane.distance(previous)
    previous_inside = (
        previous_distance >= -epsilon
        if keep_front else previous_distance <= epsilon)

    for current, current_attributes in zip(
            source_vertices, source_attributes):
        current_distance = plane.distance(current)
        current_inside = (
            current_distance >= -epsilon
            if keep_front else current_distance <= epsilon)
        if current_inside != previous_inside:
            denominator = previous_distance - current_distance
            factor = (
                0.0 if abs(denominator) <= 1e-12
                else previous_distance / denominator)
            factor = max(0.0, min(1.0, factor))
            vertices.append(_interpolate(previous, current, factor))
            attributes.append(_interpolate(
                previous_attributes, current_attributes, factor))
        if current_inside:
            vertices.append(current)
            attributes.append(current_attributes)
        previous = current
        previous_attributes = current_attributes
        previous_distance = current_distance
        previous_inside = current_inside

    deduplicated_vertices: list[Vec3] = []
    deduplicated_attributes: list[Attributes] = []
    for vertex, values in zip(vertices, attributes):
        if deduplicated_vertices and all(
                abs(vertex[axis] - deduplicated_vertices[-1][axis]) <= 1e-10
                for axis in range(3)):
            continue
        deduplicated_vertices.append(vertex)
        deduplicated_attributes.append(values)
    if len(deduplicated_vertices) >= 2 and all(
            abs(deduplicated_vertices[0][axis]
                - deduplicated_vertices[-1][axis]) <= 1e-10
            for axis in range(3)):
        deduplicated_vertices.pop()
        deduplicated_attributes.pop()

    if len(deduplicated_vertices) < 3:
        return None
    result = CameraPolygon(
        tuple(deduplicated_vertices),
        tuple(deduplicated_attributes),
        polygon.payload,
        polygon.source_order,
    )
    return result if _plane_for(result) is not None else None


def _split(
        polygon: CameraPolygon, plane: _Plane
        ) -> tuple[CameraPolygon | None, CameraPolygon | None]:
    return (
        _clip_half_space(polygon, plane, True),
        _clip_half_space(polygon, plane, False),
    )


def _partition_score(polygons: list[CameraPolygon], plane: _Plane) -> int:
    front = 0
    back = 0
    spanning = 0
    for polygon in polygons:
        classification = _classify(polygon, plane)
        if classification == 1:
            front += 1
        elif classification == -1:
            back += 1
        elif classification == 2:
            spanning += 1
    return spanning * 8 + abs(front - back)


def _choose_partition(
        polygons: list[CameraPolygon]
        ) -> tuple[_Plane | None, int]:
    # A small balanced sample avoids pathological trees without turning this
    # compact renderer helper into an expensive global optimiser.
    count = min(12, len(polygons))
    if count == len(polygons):
        indices = range(len(polygons))
    else:
        indices = {
            round(index * (len(polygons) - 1) / (count - 1))
            for index in range(count)
        }
    best_plane = None
    best_index = -1
    best_score = None
    for index in indices:
        plane = _plane_for(polygons[index])
        if plane is None:
            continue
        score = _partition_score(polygons, plane)
        if best_score is None or score < best_score:
            best_plane = plane
            best_index = index
            best_score = score
    return best_plane, best_index


def _build(polygons: list[CameraPolygon]) -> _BspNode | None:
    if not polygons:
        return None
    plane, partition_index = _choose_partition(polygons)
    if plane is None:
        return None

    coplanar: list[CameraPolygon] = []
    front: list[CameraPolygon] = []
    back: list[CameraPolygon] = []
    for index, polygon in enumerate(polygons):
        classification = (
            0 if index == partition_index else _classify(polygon, plane))
        if classification == 0:
            coplanar.append(polygon)
        elif classification == 1:
            front.append(polygon)
        elif classification == -1:
            back.append(polygon)
        else:
            front_piece, back_piece = _split(polygon, plane)
            if front_piece is not None:
                front.append(front_piece)
            if back_piece is not None:
                back.append(back_piece)

    node = _BspNode(plane, coplanar)
    node.front = _build(front)
    node.back = _build(back)
    return node


def _walk_back_to_front(
        node: _BspNode | None, eye: Vec3,
        output: list[CameraPolygon]) -> None:
    if node is None:
        return
    eye_in_front = node.plane.distance(eye) >= 0.0
    far_node = node.back if eye_in_front else node.front
    near_node = node.front if eye_in_front else node.back
    _walk_back_to_front(far_node, eye, output)
    output.extend(sorted(
        node.coplanar,
        key=lambda polygon: (polygon.mean_z(), polygon.source_order),
    ))
    _walk_back_to_front(near_node, eye, output)


def order_camera_polygons(
        polygons: list[CameraPolygon],
        eye: Vec3 = (0.0, 0.0, 4.0)) -> list[CameraPolygon]:
    """Return convex pieces in exact painter back-to-front order."""

    valid = [
        polygon for polygon in polygons
        if len(polygon.vertices) >= 3
        and len(polygon.attributes) == len(polygon.vertices)
        and _plane_for(polygon) is not None
    ]
    tree = _build(valid)
    ordered: list[CameraPolygon] = []
    _walk_back_to_front(tree, eye, ordered)
    return ordered


def order_camera_polygons_fast(
        polygons: list[CameraPolygon]) -> list[CameraPolygon]:
    """Cheap camera-drag ordering; the exact BSP is restored on release."""

    return sorted(
        (polygon for polygon in polygons
         if len(polygon.vertices) >= 3
         and len(polygon.attributes) == len(polygon.vertices)),
        key=lambda polygon: (polygon.mean_z(), polygon.source_order),
    )


def clip_camera_polygon_near(
        polygon: CameraPolygon,
        camera_distance: float = 4.0,
        minimum_distance: float = 0.2,
        ) -> CameraPolygon | None:
    """Clip one polygon before projection instead of clamping vertices."""

    maximum_z = camera_distance - minimum_distance
    plane = _Plane((0.0, 0.0, 1.0), -maximum_z)
    return _clip_half_space(polygon, plane, False)
