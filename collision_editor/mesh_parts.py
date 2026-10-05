"""Inspect render-mesh components and repair exact topological gaps for baking.

The source geometry is preserved for selection, fingerprints and distance
measurements. Preparation splits T-junctions on existing surfaces and caps only
simple planar boundary loops. It never invents a volume for a planar sheet.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Iterable

Point3 = tuple[float, float, float]
Triangle3 = tuple[Point3, Point3, Point3]


def _point(value) -> Point3:
    if len(value) != 3:
        raise ValueError("A geometry vertex needs three coordinates.")
    point = tuple(float(n) for n in value)
    if not all(math.isfinite(n) for n in point):
        raise ValueError("Geometry vertices must be finite.")
    return point


def _sub(a, b):
    return tuple(a[i] - b[i] for i in range(3))


def _dot(a, b):
    return sum(a[i] * b[i] for i in range(3))


def _cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2],
            a[0]*b[1]-a[1]*b[0])


def _extent(points):
    return max(max(p[a] for p in points)-min(p[a] for p in points)
               for a in range(3)) if points else 0.0


def _plane(points, tolerance):
    """A supporting plane, or None for a line/point."""
    if len(points) < 3:
        return None
    origin = points[0]
    second = max(points[1:], key=lambda p: _dot(_sub(p, origin), _sub(p, origin)))
    edge = _sub(second, origin)
    normals = [_cross(edge, _sub(p, origin)) for p in points[1:]]
    normal = max(normals, key=lambda n: _dot(n, n))
    length = math.sqrt(_dot(normal, normal))
    if length <= tolerance*tolerance:
        return None
    return origin, tuple(n/length for n in normal)


def _edges(triangles):
    result = {}
    for triangle in triangles:
        for a, b in zip(triangle, triangle[1:] + triangle[:1]):
            key = tuple(sorted((a, b)))
            result.setdefault(key, []).append((a, b))
    return result


@dataclass(frozen=True)
class GeometryComponent:
    owner: str
    component_id: str
    triangles: tuple[Triangle3, ...]
    boundary_edges: int
    non_manifold_edges: int
    planar: bool
    has_volume: bool


def split_triangle_components(parts: Iterable) -> tuple[GeometryComponent, ...]:
    """Group faces sharing vertices, retaining every face and stable IDs."""
    components = []
    for owner, source in parts:
        triangles = [tuple(_point(p) for p in t) for t in source]
        if any(len(t) != 3 for t in triangles):
            raise ValueError("Geometry components require triangles.")
        parents = list(range(len(triangles)))

        def root(i):
            while parents[i] != i:
                parents[i] = parents[parents[i]]
                i = parents[i]
            return i

        vertices = {}
        for i, triangle in enumerate(triangles):
            for p in triangle:
                if p in vertices:
                    a, b = root(i), root(vertices[p])
                    parents[max(a, b)] = min(a, b)
                else:
                    vertices[p] = i
        groups = {}
        for i, triangle in enumerate(triangles):
            groups.setdefault(root(i), []).append(triangle)
        for group in groups.values():
            points = sorted(set(p for t in group for p in t))
            tolerance = max(1e-10, _extent(points)*1e-7)
            plane = _plane(points, tolerance)
            planar = plane is None or all(
                abs(_dot(_sub(p, plane[0]), plane[1])) <= tolerance
                for p in points)
            counts = _edges(group)
            canonical = sorted(tuple(sorted(t)) for t in group)
            digest = hashlib.sha256(json.dumps(canonical, separators=(",", ":"),
                                               allow_nan=False).encode()).hexdigest()[:16]
            components.append(GeometryComponent(
                str(owner), digest, tuple(group),
                sum(len(e) == 1 for e in counts.values()),
                sum(len(e) > 2 for e in counts.values()), planar, not planar))
    return tuple(sorted(components, key=lambda c: (c.owner, c.component_id)))


def topology_diagnostic(component: GeometryComponent) -> str:
    kind = "planar surface (no enclosed volume)" if component.planar else "3D geometry"
    return (f"{len(component.triangles)} triangles; {component.boundary_edges} open "
            f"edges; {component.non_manifold_edges} non-manifold edges; {kind}")


def _split_junctions(triangles, tolerance):
    points = sorted(set(p for t in triangles for p in t))
    output = []
    split_count = 0
    for triangle in triangles:
        perimeter = []
        for a, b in zip(triangle, triangle[1:] + triangle[:1]):
            edge = _sub(b, a)
            length2 = _dot(edge, edge)
            mids = []
            if length2 > tolerance*tolerance:
                for p in points:
                    if p == a or p == b:
                        continue
                    delta = _sub(p, a)
                    t = _dot(delta, edge)/length2
                    if not 1e-8 < t < 1-1e-8:
                        continue
                    residual = tuple(delta[i]-t*edge[i] for i in range(3))
                    if _dot(residual, residual) <= tolerance*tolerance:
                        mids.append((t, p))
            perimeter.append(a)
            perimeter.extend(p for _, p in sorted(mids))
            split_count += len(mids)
        if len(perimeter) == 3:
            output.append(triangle)
        else:
            # A fan from the face centre preserves intermediate collinear
            # boundary vertices without adding zero-area triangles.
            centre = tuple(sum(p[i] for p in triangle)/3 for i in range(3))
            for a, b in zip(perimeter, perimeter[1:] + perimeter[:1]):
                output.append((centre, a, b))
    return output, split_count


def _orient(triangles):
    """Make neighbouring faces consistent; reject non-manifold adjacency."""
    edges = _edges(triangles)
    if any(len(links) > 2 for links in edges.values()):
        return triangles, False
    face_edges = {}
    for i, t in enumerate(triangles):
        for a, b in zip(t, t[1:] + t[:1]):
            face_edges.setdefault(tuple(sorted((a, b))), []).append((i, a < b))
    adjacency = [[] for _ in triangles]
    for links in face_edges.values():
        if len(links) == 2:
            (i, direction), (j, other) = links
            different = direction == other
            adjacency[i].append((j, different)); adjacency[j].append((i, different))
    flips = {}
    for seed in range(len(triangles)):
        if seed in flips:
            continue
        flips[seed] = False
        pending = [seed]
        while pending:
            i = pending.pop()
            for j, different in adjacency[i]:
                value = flips[i] ^ different
                if j in flips and flips[j] != value:
                    return triangles, False
                if j not in flips:
                    flips[j] = value; pending.append(j)
    return [(t[0], t[2], t[1]) if flips[i] else t
            for i, t in enumerate(triangles)], True


def _triangulate_loop(loop, tolerance):
    plane = _plane(loop, tolerance)
    if plane is None or any(abs(_dot(_sub(p, plane[0]), plane[1])) > tolerance for p in loop):
        return None
    axis = max(range(3), key=lambda a: abs(plane[1][a]))
    points = [tuple(p[i] for i in range(3) if i != axis) for p in loop]

    def cross2(a, b, c):
        return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])

    # An ear clip is only valid for a simple polygon. Refuse crossing or
    # touching non-adjacent edges instead of manufacturing a cap over them.
    def intersects(a, b, c, d):
        eps = tolerance*tolerance
        values = (cross2(a,b,c), cross2(a,b,d), cross2(c,d,a), cross2(c,d,b))
        if values[0]*values[1] < -eps*eps and values[2]*values[3] < -eps*eps:
            return True
        return any(abs(value) <= eps and all(min(x[k],y[k])-tolerance <= p[k] <= max(x[k],y[k])+tolerance
                                             for k in range(2))
                   for value,p,x,y in ((values[0],c,a,b),(values[1],d,a,b),
                                       (values[2],a,c,d),(values[3],b,c,d)))
    for i in range(len(points)):
        for j in range(i+1, len(points)):
            if j == i+1 or (i == 0 and j == len(points)-1):
                continue
            if intersects(points[i], points[(i+1)%len(points)], points[j], points[(j+1)%len(points)]):
                return None
    area = sum(points[i][0]*points[(i+1)%len(points)][1]-
               points[(i+1)%len(points)][0]*points[i][1] for i in range(len(points)))
    if abs(area) <= tolerance*tolerance:
        return None
    sign = 1 if area > 0 else -1
    remaining = list(range(len(loop)))
    output = []
    while len(remaining) > 3:
        found = False
        for index in range(len(remaining)):
            a, b, c = remaining[index-1], remaining[index], remaining[(index+1)%len(remaining)]
            if sign*cross2(points[a], points[b], points[c]) <= tolerance*tolerance:
                continue
            # Points on an ear's boundary prevent clipping that ear: this keeps
            # a collinear boundary vertex connected to both adjacent surfaces.
            if any(all(sign*cross2(points[x], points[y], points[p]) >= -tolerance*tolerance
                       for x, y in ((a,b), (b,c), (c,a)))
                   for p in remaining if p not in (a,b,c)):
                continue
            output.append((loop[a], loop[b], loop[c])); remaining.pop(index)
            found = True; break
        if not found:
            return None
    a, b, c = remaining
    if sign*cross2(points[a], points[b], points[c]) <= tolerance*tolerance:
        return None
    output.append((loop[a], loop[b], loop[c]))
    return output


def prepare_component_mesh(triangles: Iterable[Triangle3]):
    """Return prepared faces and explicit repair diagnostics, with no scaling."""
    source = [tuple(_point(p) for p in t) for t in triangles]
    components = split_triangle_components([("part", source)])
    if not components or all(c.planar for c in components):
        return tuple(source), ("Selected geometry is planar: it does not define a solid body.",)
    points = sorted(set(p for t in source for p in t))
    tolerance = max(1e-10, _extent(points)*1e-7)
    prepared, split_count = _split_junctions(source, tolerance)
    prepared, consistent = _orient(prepared)
    warnings = []
    if split_count:
        warnings.append(f"Split {split_count} existing edge T-junctions without changing the surface.")
    if not consistent:
        warnings.append("Source adjacency is non-manifold or non-orientable; CoACD preprocessing is required.")
        return tuple(prepared), tuple(warnings)
    edges = _edges(prepared)
    boundary = [links[0] for links in edges.values() if len(links) == 1]
    outgoing = {}; incoming = {}
    for a, b in boundary:
        outgoing.setdefault(a, []).append(b); incoming.setdefault(b, []).append(a)
    if any(len(v) != 1 for v in outgoing.values()) or any(len(v) != 1 for v in incoming.values()):
        if boundary:
            warnings.append("Source has branching boundaries; CoACD preprocessing is required.")
        return tuple(prepared), tuple(warnings)
    pending = set(outgoing)
    caps = 0; unresolved = 0
    while pending:
        start = min(pending); loop = [start]; pending.remove(start)
        current = outgoing[start][0]
        while current != start and current in pending:
            loop.append(current); pending.remove(current); current = outgoing[current][0]
        if current != start:
            unresolved += 1; continue
        # Reverse the directed boundary so the new faces share edges with
        # opposite winding. Non-planar holes remain explicitly unresolved.
        cap = _triangulate_loop(list(reversed(loop)), tolerance)
        if cap is None:
            unresolved += 1
        else:
            prepared.extend(cap); caps += 1
    if caps:
        warnings.append(f"Closed {caps} simple planar boundary loops on their existing perimeter.")
    if unresolved:
        warnings.append(f"{unresolved} non-planar or unresolved boundary loops require CoACD preprocessing.")
    # For a closed oriented surface, choose outward winding. CoACD expects
    # positive signed volume; source render culling conventions may differ.
    final_edges = _edges(prepared)
    if final_edges and all(len(e) == 2 for e in final_edges.values()):
        volume6 = sum(_dot(t[0], _cross(t[1], t[2])) for t in prepared)
        if volume6 < 0:
            prepared = [(t[0], t[2], t[1]) for t in prepared]
    return tuple(prepared), tuple(warnings)
