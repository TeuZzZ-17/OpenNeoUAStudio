"""Deterministic sphere approximation of a classified physical core.

Geometry classification is identical for all presets. The volume grid measures
coverage and exterior occupancy; output centres are selected on medial ridges.
No reference spheres, unit names or model-specific polygon IDs enter generation.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Iterable
import numpy as np

Point3 = tuple[float, float, float]
Triangle3 = tuple[Point3, Point3, Point3]
UNIT_COLL_MAX_COUNT = 512


@dataclass(frozen=True)
class AccuracyPreset:
    key: str
    label: str
    tolerance_fraction: float
    minimum_gain_fraction: float


ACCURACY_PRESETS = (
    AccuracyPreset("low", "Low Accuracy", 0.15, 0.005),
    AccuracyPreset("medium", "Medium Accuracy", 0.10, 0.002),
    AccuracyPreset("high", "High Accuracy", 0.06, 0.0005),
    AccuracyPreset("ultra", "Ultra Accuracy", 0.035, 0.0002),
)


@dataclass(frozen=True)
class GeneratedSphere:
    x: float
    y: float
    z: float
    radius: float

    @property
    def center(self) -> Point3:
        return self.x, self.y, self.z


@dataclass(frozen=True)
class SphereGenerationResult:
    spheres: tuple[GeneratedSphere, ...]
    sample_count: int
    measured_error: float
    tolerance: float
    hit_safety_cap: bool
    symmetry_detected: bool = False
    symmetry_score: float = 0.0
    external_volume_fraction: float = 0.0
    excluded_triangle_count: int = 0
    structural_sheet_count: int = 0


@dataclass
class CollisionBody:
    mask: np.ndarray
    axes: tuple[np.ndarray, ...]
    spacing: float
    symmetric: bool
    symmetry_score: float
    importance: np.ndarray
    excluded_triangle_count: int
    structural_sheet_count: int


def _distance_field(mask):
    """Exact separable Euclidean distance to false cell centres, using NumPy."""
    values = np.where(mask, np.inf, 0.0)
    for axis in range(3):
        source = np.moveaxis(values, axis, 0)
        target = source.copy()
        for offset in range(1, len(source)):
            cost = float(offset * offset)
            np.minimum(target[offset:], source[:-offset] + cost, out=target[offset:])
            np.minimum(target[:-offset], source[offset:] + cost, out=target[:-offset])
        values = np.moveaxis(target, 0, axis)
    return np.sqrt(values)


def _local_maximum(values):
    result = values.copy()
    padded = np.pad(values, 1, mode="constant")
    nx, ny, nz = values.shape
    for x in range(3):
        for y in range(3):
            for z in range(3):
                np.maximum(
                    result, padded[x : x + nx, y : y + ny, z : z + nz], out=result
                )
    return result


def _clean_triangles(triangles, animated_triangles):
    unique = {}
    animation = set(animated_triangles)
    for index, raw in enumerate(triangles):
        try:
            tri = np.asarray(raw, dtype=float)
        except (TypeError, ValueError):
            continue
        if tri.shape != (3, 3) or not np.isfinite(tri).all():
            continue
        if np.linalg.norm(np.cross(tri[1] - tri[0], tri[2] - tri[0])) <= 1e-12:
            continue
        key = tuple(sorted(map(tuple, tri)))
        unique[key] = unique.get(key, False) or index in animation
    if not unique:
        raise ValueError("The selected model has no usable surface triangles.")
    ordered = sorted(unique)
    return np.array(ordered), np.array([unique[k] for k in ordered])


def triangle_distance(points, tri):
    a, b, c = tri
    u = b - a
    v = c - a
    d = points - a
    uu = u @ u
    vv = v @ v
    uv = u @ v
    denom = uu * vv - uv * uv
    s = (vv * np.sum(d * u, axis=-1) - uv * np.sum(d * v, axis=-1)) / denom
    t = (uu * np.sum(d * v, axis=-1) - uv * np.sum(d * u, axis=-1)) / denom
    n = np.cross(u, v)
    n /= np.linalg.norm(n)
    inside = (s >= 0) & (t >= 0) & (s + t <= 1)
    result = np.where(inside, np.sum(d * n, axis=-1) ** 2, np.inf)
    for p, q in [(a, b), (b, c), (c, a)]:
        e = q - p
        w = points - p
        f = np.clip(np.sum(w * e, axis=-1) / (e @ e), 0, 1)
        result = np.minimum(result, np.sum((w - f[..., None] * e) ** 2, axis=-1))
    return np.sqrt(result)


def components(tris):
    keys = [set(map(tuple, t)) for t in tris]
    pending = set(range(len(tris)))
    result = []
    while pending:
        group = {min(pending)}
        pending -= group
        while True:
            points = set().union(*(keys[i] for i in group))
            found = {i for i in pending if keys[i] & points}
            if not found:
                break
            group |= found
            pending -= found
        result.append(np.array(sorted(group)))
    return result


def _attachment_distance(sheets, solids):
    """Include edge/face intersections when a sheet penetrates a body wall."""
    points = sheets.reshape(-1, 3)
    best = min(float(triangle_distance(points, tri).min()) for tri in solids)
    for sheet in sheets:
        for a, b in ((sheet[0], sheet[1]), (sheet[1], sheet[2]), (sheet[2], sheet[0])):
            edge = b - a
            for tri in solids:
                normal = np.cross(tri[1] - tri[0], tri[2] - tri[0])
                denominator = float(edge @ normal)
                if abs(denominator) < 1e-12:
                    continue
                fraction = float((tri[0] - a) @ normal) / denominator
                if 0 <= fraction <= 1:
                    if triangle_distance(a + fraction * edge, tri) < 1e-7:
                        return 0.0
    return best


def scan(tris, axes):
    votes = np.zeros(tuple(map(len, axes)), dtype=np.uint8)
    for axis in range(3):
        ij = [a for a in range(3) if a != axis]
        grid = np.stack(np.meshgrid(axes[ij[0]], axes[ij[1]], indexing="ij"), axis=-1)
        lo = np.full(grid.shape[:2], np.inf)
        hi = -lo
        for tri in tris:
            a, b, c = tri
            u = b[ij] - a[ij]
            v = c[ij] - a[ij]
            det = u[0] * v[1] - u[1] * v[0]
            if abs(det) < 1e-9:
                continue
            d = grid - a[ij]
            s = (d[..., 0] * v[1] - d[..., 1] * v[0]) / det
            t = (u[0] * d[..., 1] - u[1] * d[..., 0]) / det
            hit = (s >= -1e-8) & (t >= -1e-8) & (s + t <= 1 + 1e-8)
            value = a[axis] + s * (b[axis] - a[axis]) + t * (c[axis] - a[axis])
            lo = np.minimum(lo, np.where(hit, value, np.inf))
            hi = np.maximum(hi, np.where(hit, value, -np.inf))
        shape = [1, 1, 1]
        shape[axis] = len(axes[axis])
        values = axes[axis].reshape(shape)
        l = np.expand_dims(lo, axis)
        h = np.expand_dims(hi, axis)
        votes += (values >= l) & (values <= h) & ((h - l) > 1e-7)
    return votes >= 2


def _build_body(tris, animated):
    solids, sheets = [], []
    for ids in components(tris):
        points = np.unique(tris[ids].reshape(-1, 3), axis=0)
        singular = np.linalg.svd(points - points.mean(0), compute_uv=False)
        # A warped quad is still a sheet, even when its vertices are not coplanar.
        if len(ids) < 4 or singular[-1] < singular[0] * 0.025:
            sheets.append(ids)
        else:
            solids.append(ids)
    if not solids:
        raise ValueError(
            "No physical body was found. Flat visual geometry cannot define collision spheres."
        )
    points = np.concatenate([tris[g].reshape(-1, 3) for g in solids])
    lower, upper = points.min(0), points.max(0)
    span = upper - lower
    # Exclude very slender rods using both section size and aspect ratio.
    # Substantial guns and broad structural tails remain eligible.
    solids = [
        g
        for g in solids
        if not (
            np.sort(np.ptp(tris[g].reshape(-1, 3), axis=0))[1] < min(span) * 0.12
            and max(np.ptp(tris[g].reshape(-1, 3), axis=0))
            > np.sort(np.ptp(tris[g].reshape(-1, 3), axis=0))[1] * 8
        )
    ]
    if not solids:
        raise ValueError("Only thin visual rods were found; no physical body.")
    points = np.concatenate([tris[g].reshape(-1, 3) for g in solids])
    lower, upper = points.min(0), points.max(0)
    span = upper - lower
    scale = float(max(span))
    if scale < 0.01:
        raise ValueError("The model is below runtime collision precision.")
    h = scale / 88
    origin = (lower + upper) * 0.5
    if abs(origin[0]) < span[0] * 0.03:
        origin[0] = 0

    accepted = []
    pending = list(sheets)
    # A sheet needs both structural extent and an attachment to an existing body.
    # Texture animation vetoes sheet promotion, but never removes solid tracks.
    while pending:
        added = []
        for ids in pending:
            p = tris[ids].reshape(-1, 3)
            lo, hi = p.min(0), p.max(0)
            extent, center = hi - lo, p.mean(0)
            axis = int(np.argmax(extent))
            tail = (
                axis == 2
                and abs(center[0] - origin[0]) < span[0] * 0.5
                and (lo[2] < lower[2] - h * 2 or hi[2] > upper[2] + h * 2)
            )
            wing = (
                axis == 0
                and extent[2] > span[2] * 0.08
                and extent[1] < h * 2
                and (lo[0] < lower[0] - h * 2 or hi[0] > upper[0] + h * 2)
                and center[1] > lower[1] + h * 2
            )
            if animated[ids].any() or not (tail or wing):
                continue
            attachment = _attachment_distance(
                tris[ids], np.concatenate([tris[g] for g in solids + accepted])
            )
            if attachment <= h * 2.5:
                accepted.append(ids)
                added.append(ids)
        if not added:
            break
        pending = [g for g in pending if not any(g is a for a in added)]

    selected = solids + accepted
    points = np.concatenate([tris[g].reshape(-1, 3) for g in selected])
    extent = np.maximum(abs(points.min(0) - origin), abs(points.max(0) - origin))
    extent += h * 7
    sizes = np.ceil(extent / h) * 2 + 1
    if np.prod(sizes) > 600000:
        h *= (np.prod(sizes) / 600000) ** (1 / 3)
    axes = tuple(
        np.arange(-np.ceil(e / h), np.ceil(e / h) + 1) * h + origin[a]
        for a, e in enumerate(extent)
    )
    mask = np.zeros(tuple(map(len, axes)), bool)
    importance = np.zeros(mask.shape)
    for ids in solids:
        part = scan(tris[ids], axes)
        if not part.any():
            continue
        mask |= part
        importance[part] = np.maximum(importance[part], 1 / np.sqrt(part.sum()))
        extent = np.ptp(tris[ids].reshape(-1, 3), axis=0)
        if extent[2] < extent[0] * 3 or extent[0] > extent[1] * 0.5:
            continue
        sections = [(z, np.argwhere(part[:, :, z])) for z in range(len(axes[2]))]
        sections = [(z, ij) for z, ij in sections if len(ij)]
        # Round a trusted tail core using its transverse thickness, not its length.
        radius = max(
            h * 2,
            float(np.median([np.ptp(axes[1][ij[:, 1]]) for _, ij in sections])) * 0.5,
        )
        xx, yy = np.meshgrid(axes[0], axes[1], indexing="ij")
        for z, ij in sections:
            cx, cy = np.mean(axes[0][ij[:, 0]]), np.mean(axes[1][ij[:, 1]])
            mask[:, :, z] |= (xx - cx) ** 2 + (yy - cy) ** 2 <= radius**2
    for ids in accepted:
        extent = np.ptp(tris[ids].reshape(-1, 3), axis=0)
        radius = min(np.sort(extent)[1] * 0.45, span[0] * 0.16)
        for tri in tris[ids]:
            lo, hi = tri.min(0) - radius, tri.max(0) + radius
            slices = tuple(
                slice(
                    np.searchsorted(a, lo[i]), np.searchsorted(a, hi[i], side="right")
                )
                for i, a in enumerate(axes)
            )
            coords = np.stack(
                np.meshgrid(*(a[s] for a, s in zip(axes, slices)), indexing="ij"),
                axis=-1,
            )
            mask[slices] |= triangle_distance(coords, tri) <= radius
    if not mask.any():
        raise ValueError("The mesh has no resolvable physical volume.")
    reflected = mask[::-1]
    score = float((mask & reflected).sum() / max(1, (mask | reflected).sum()))
    symmetric = score > 0.82
    if symmetric:
        discrepancy = _distance_field(~reflected)[mask] * h
        symmetric = bool(np.percentile(discrepancy, 99) <= scale * 0.055)
    if symmetric:
        mask |= reflected
        importance = np.maximum(importance, importance[::-1])
    importance = np.maximum(importance, 1 / np.sqrt(mask.sum()))
    return CollisionBody(
        mask,
        axes,
        h,
        symmetric,
        score,
        importance,
        len(tris) - sum(len(g) for g in selected),
        len(accepted),
    )


def _fit_body(mask, axes, h, symmetric, config, importance, cap):
    dist = _distance_field(mask) * h
    coords = np.stack(np.meshgrid(*axes, indexing="ij"), axis=-1)
    # Medial candidates plus lower-clearance candidates near silhouette corners.
    cand = mask & (dist >= h * 1.5)
    plane = float(axes[0][len(axes[0]) // 2])
    ridge = (dist >= _local_maximum(dist) - h * 0.6) & cand
    if symmetric:
        ridge &= coords[..., 0] >= plane - h * 0.1
    pts = coords[ridge]
    radii = dist[ridge] * 1.22
    if not len(pts):
        raise ValueError("Physical features are below the sampling resolution.")
    # Bound every candidate's exterior with deterministic volume samples.
    dirs = np.array(
        [
            (a, b, c)
            for a in np.linspace(-1, 1, 7)
            for b in np.linspace(-1, 1, 7)
            for c in np.linspace(-1, 1, 7)
            if 0 < a * a + b * b + c * c <= 1
        ]
    )
    origin = np.array([a[0] for a in axes])
    shape = np.array(mask.shape)
    for _ in range(4):
        samples = pts[:, None, :] + dirs[None, :, :] * radii[:, None, None]
        ids = np.rint((samples - origin) / h).astype(int)
        valid = ((ids >= 0) & (ids < shape)).all(2)
        ids = np.clip(ids, 0, shape - 1)
        inside = mask[tuple(ids.transpose(2, 0, 1))] & valid
        bad = inside.mean(1) < 0.80
        radii[bad] *= 0.94
    order = np.argsort(-radii, kind="stable")[:5000]
    pts = pts[order]
    radii = radii[order]
    target = mask & (dist >= h * 1.6)
    volume_indices = np.flatnonzero(target)
    stride = max(1, len(volume_indices) // 6000)
    # Keep the medial core at full resolution. A strided volume sample alone
    # can skip whole stretches of a narrow cannon or tail between sample rows.
    medial = target & (dist >= _local_maximum(dist) - h * 0.6)
    sample_indices = np.union1d(volume_indices[::stride], np.flatnonzero(medial))
    targets = coords.reshape(-1, 3)[sample_indices]
    target_clearance = dist.ravel()[sample_indices]
    weights = (
        importance.ravel()[sample_indices]
        / np.maximum(dist.ravel()[sample_indices], h * 2) ** 1.5
    )
    covered = np.zeros(len(targets), bool)
    spheres = []
    bestprev = np.full(len(pts), np.inf)
    cap_blocked = False
    total_weight = np.sum(weights)
    while np.sum(weights[covered]) / total_weight < 1 - config.tolerance_fraction:
        best = None
        bestgain = 0
        for i, (p, r) in enumerate(zip(pts, radii)):
            if bestprev[i] <= bestgain:
                continue
            hits = np.sum((targets - p) ** 2, axis=1) <= r * r
            pair = symmetric and p[0] > plane + h * 0.1
            if len(spheres) + (2 if pair else 1) > cap:
                cap_blocked = True
                continue
            if pair:
                hits |= (
                    np.sum(
                        (targets - np.array([2 * plane - p[0], p[1], p[2]])) ** 2,
                        axis=1,
                    )
                    <= r * r
                )
            gain = np.sum(weights[hits & ~covered]) / (2 if pair else 1)
            bestprev[i] = gain
            if gain > bestgain:
                best = (i, hits, pair, p, r)
                bestgain = gain
        if best is None or bestgain / total_weight < config.minimum_gain_fraction:
            # Broad, nearly isotropic bodies may have only one medial peak.
            # Try uncovered interior points before declaring them fully fitted.
            remaining = np.flatnonzero(~covered)
            if symmetric:
                remaining = remaining[targets[remaining, 0] >= plane - h * 0.1]
            step = max(1, len(remaining) // 256)
            for j in remaining[::step][:256]:
                p = targets[j]
                r = target_clearance[j] * 1.10
                pair = symmetric and p[0] > plane + h * 0.1
                if len(spheres) + (2 if pair else 1) > cap:
                    cap_blocked = True
                    continue
                hits = np.sum((targets - p) ** 2, axis=1) <= r * r
                if pair:
                    mirrored = np.array([2 * plane - p[0], p[1], p[2]])
                    hits |= np.sum((targets - mirrored) ** 2, axis=1) <= r * r
                gain = np.sum(weights[hits & ~covered]) / (2 if pair else 1)
                if gain > bestgain:
                    best = (-1, hits, pair, p, r)
                    bestgain = gain
        if best is None or bestgain / total_weight < config.minimum_gain_fraction:
            break
        i, hits, pair, p, r = best
        spheres.append([*p, r])
        covered |= hits
        if i >= 0:
            bestprev[i] = 0
        if pair:
            spheres.append([2 * plane - p[0], p[1], p[2], r])
    return (
        spheres,
        np.sum(weights[covered]) / np.sum(weights),
        len(targets),
        cap_blocked,
    )


def build_collision_body(triangles: Iterable[Triangle3], *, animated_triangles=()):
    """Classify connected solids and attached structural sheets before fitting."""
    clean, animated = _clean_triangles(triangles, animated_triangles)
    return _build_body(clean, animated)


def sphere_union_mask(body: CollisionBody, spheres):
    """Measure occupied union cells without double-counting overlapping spheres."""
    union = np.zeros(body.mask.shape, bool)
    for sphere in spheres:
        if isinstance(sphere, GeneratedSphere):
            x, y, z, r = sphere.x, sphere.y, sphere.z, sphere.radius
        else:
            x, y, z, r = sphere
        union |= (body.axes[0][:, None, None] - x) ** 2 + (
            body.axes[1][None, :, None] - y
        ) ** 2 + (body.axes[2][None, None, :] - z) ** 2 <= r * r
    return union


def generate_collision_spheres(
    triangles: Iterable[Triangle3],
    preset: str,
    *,
    max_spheres: int = UNIT_COLL_MAX_COUNT,
    animated_triangles=(),
):
    """Generate normal editable spheres, with atomic mirrored pairs when appropriate."""
    key = str(preset).strip().casefold().replace(" accuracy", "")
    config = next((p for p in ACCURACY_PRESETS if p.key == key), None)
    if config is None:
        raise ValueError(f"Unknown collision-sphere accuracy preset: {preset}")
    cap = max(1, min(int(max_spheres), UNIT_COLL_MAX_COUNT))
    body = build_collision_body(triangles, animated_triangles=animated_triangles)
    spheres, coverage, count, capped = _fit_body(
        body.mask, body.axes, body.spacing, body.symmetric, config, body.importance, cap
    )
    spheres = tuple(
        sorted(
            (GeneratedSphere(*(round(float(v), 3) + 0.0 for v in s)) for s in spheres),
            key=lambda s: (s.x, s.y, s.z, s.radius),
        )
    )
    union = sphere_union_mask(body, spheres)
    external = float((union & ~body.mask).sum() / max(1, union.sum()))
    return SphereGenerationResult(
        spheres,
        count,
        float(1 - coverage),
        config.tolerance_fraction,
        bool(capped and 1 - coverage > config.tolerance_fraction),
        body.symmetric,
        body.symmetry_score,
        external,
        body.excluded_triangle_count,
        body.structural_sheet_count,
    )
