"""Tidy the raw wireframe lines the way the original Urban Assault outlines look.

The generator samples visible edges pixel by pixel, so its raw lines can be cut in
dashes, stop a pixel short of a corner, run a pixel past it, or lie twice next to
each other. The original game wireframes have the opposite look: few long straight
lines that meet exactly in their corners. ``clean_segments`` turns the first into
the second; all distances are in pixels of the source view.
"""

from __future__ import annotations

import math

import numpy as np

# Lines closer than this fraction of the model size are treated as one corner/line.
SNAP_FRACTION = 0.008
SNAP_MIN, SNAP_MAX = 2.5, 8.0
# Collinear lines whose gap is below this many snap distances are joined.
GAP_SNAPS = 5.0
# A free end is prolonged along its own direction up to this many snap distances
# when that reaches another line (a line that stopped just short of a junction).
REACH_SNAPS = 5.0
# A free-ended line shorter than this many snap distances is noise.
SPUR_SNAPS = 4.0
ISOLATED_SNAPS = 6.0


def snap_distance(extent: float) -> float:
    return float(min(SNAP_MAX, max(SNAP_MIN, extent*SNAP_FRACTION)))


def clean_segments(segments, extent: float):
    """Return cleaned ``[(a, b), ...]`` (numpy 2-vectors) for the raw ``segments``."""

    data = np.array([[a, b] for a, b in segments], dtype=float).reshape(-1, 2, 2)
    if not len(data):
        return []
    snap = snap_distance(extent)
    data = _merge_collinear(data, snap*0.75, snap*GAP_SNAPS)
    data = _cluster_endpoints(data, snap)
    data = _snap_to_lines(data, snap)
    data = _extend_free_ends(data, snap, snap*REACH_SNAPS)
    data = _merge_collinear(data, snap*0.5, snap*GAP_SNAPS)
    data = _cluster_endpoints(data, snap*0.6)
    data = _drop_degenerate(data, snap*0.5)
    for _ in range(2):
        data = _drop_spurs(data, snap)
    return [(segment[0], segment[1]) for segment in data]


def _merge_collinear(data, perp_tol: float, gap_tol: float):
    """Join lines that lie on one straight line and touch, overlap or nearly meet."""

    lengths = np.linalg.norm(data[:, 1]-data[:, 0], axis=1)
    order = np.argsort(-lengths)
    data = data[order]
    alive = np.ones(len(data), dtype=bool)
    result = []
    for i in range(len(data)):
        if not alive[i]:
            continue
        alive[i] = False
        a, b = data[i]
        while True:
            axis = b-a
            length = float(np.linalg.norm(axis))
            if length < 1e-9:
                break
            axis = axis/length
            rest = np.flatnonzero(alive)
            if not len(rest):
                break
            p, q = data[rest, 0], data[rest, 1]
            normal = np.array((-axis[1], axis[0]))
            near = ((np.abs((p-a) @ normal) <= perp_tol)
                    & (np.abs((q-a) @ normal) <= perp_tol))
            tp, tq = (p-a) @ axis, (q-a) @ axis
            low, high = np.minimum(tp, tq), np.maximum(tp, tq)
            close = near & (high >= -gap_tol) & (low <= length+gap_tol)
            if not close.any():
                break
            picked = rest[close]
            alive[picked] = False
            t_min = min(0.0, float(low[close].min()))
            t_max = max(length, float(high[close].max()))
            a, b = a+axis*t_min, a+axis*t_max
        result.append((a, b))
    return np.array(result).reshape(-1, 2, 2)


def _cluster_endpoints(data, distance: float):
    """Move end points that lie within ``distance`` of each other to their centre."""

    points = data.reshape(-1, 2)
    count = len(points)
    parent = list(range(count))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    cell = max(distance, 1e-6)
    grid: dict[tuple[int, int], list[int]] = {}
    for index, (x, y) in enumerate(points):
        key = (int(math.floor(x/cell)), int(math.floor(y/cell)))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for other in grid.get((key[0]+dx, key[1]+dy), ()):
                    if math.dist(points[index], points[other]) <= distance:
                        parent[find(index)] = find(other)
        grid.setdefault(key, []).append(index)
    groups: dict[int, list[int]] = {}
    for index in range(count):
        groups.setdefault(find(index), []).append(index)
    moved = points.copy()
    for members in groups.values():
        if len(members) > 1:
            moved[members] = points[members].mean(axis=0)
    return moved.reshape(-1, 2, 2)


def _snap_to_lines(data, distance: float):
    """Pull an end point that almost touches the middle of another line onto it."""

    data = data.copy()
    start, end = data[:, 0], data[:, 1]
    delta = end-start
    squared = np.maximum((delta*delta).sum(axis=1), 1e-12)
    for index in range(len(data)):
        for side in (0, 1):
            point = data[index, side]
            t = ((point-start) * delta).sum(axis=1)/squared
            foot = start+np.clip(t, 0.0, 1.0)[:, None]*delta
            gap = np.linalg.norm(foot-point, axis=1)
            gap[index] = np.inf
            # Only the inside of a line: its end points were joined by clustering.
            gap[(t <= 0.02) | (t >= 0.98)] = np.inf
            nearest = int(np.argmin(gap))
            if gap[nearest] <= distance:
                data[index, side] = foot[nearest]
    return data


def _extend_free_ends(data, snap: float, reach: float):
    """Prolong a free end to the first line it points at, if that is close."""

    data = data.copy()
    free = _free_ends(data, snap)
    for index in range(len(data)):
        for side in (0, 1):
            if not free[index, side]:
                continue
            point, other = data[index, side], data[index, 1-side]
            direction = point-other
            length = float(np.linalg.norm(direction))
            if length < 1e-9:
                continue
            direction = direction/length
            start, end = data[:, 0], data[:, 1]
            edge = end-start
            # Solve point + t*direction = start + u*edge for every other line.
            denominator = direction[0]*edge[:, 1]-direction[1]*edge[:, 0]
            valid = np.abs(denominator) > 1e-9
            safe = np.where(valid, denominator, 1.0)
            offset = start-point
            t = (offset[:, 0]*edge[:, 1]-offset[:, 1]*edge[:, 0])/safe
            u = (offset[:, 0]*direction[1]-offset[:, 1]*direction[0])/safe
            hit = valid & (t > 1e-6) & (t <= reach) & (u >= -0.02) & (u <= 1.02)
            hit[index] = False
            if hit.any():
                nearest = int(np.argmin(np.where(hit, t, np.inf)))
                data[index, side] = point+direction*t[nearest]
    return data


def _drop_degenerate(data, minimum: float):
    lengths = np.linalg.norm(data[:, 1]-data[:, 0], axis=1)
    data = data[lengths >= minimum]
    seen, kept = set(), []
    for a, b in data:
        key = tuple(sorted((tuple(np.round(a, 3)), tuple(np.round(b, 3)))))
        if key not in seen:
            seen.add(key)
            kept.append((a, b))
    return np.array(kept).reshape(-1, 2, 2)


def _drop_spurs(data, snap: float):
    """Remove short lines that end freely: stubs on a corner and lone specks."""

    if not len(data):
        return data
    free = _free_ends(data, snap)
    lengths = np.linalg.norm(data[:, 1]-data[:, 0], axis=1)
    free_count = free.sum(axis=1)
    noise = ((free_count == 2) & (lengths < snap*ISOLATED_SNAPS)) | \
            ((free_count == 1) & (lengths < snap*SPUR_SNAPS))
    return data[~noise]


def _free_ends(data, snap: float):
    """Per line, which of its two ends touches no other line."""

    start, end = data[:, 0], data[:, 1]
    delta = end-start
    squared = np.maximum((delta*delta).sum(axis=1), 1e-12)
    free = np.ones((len(data), 2), dtype=bool)
    for index in range(len(data)):
        for side in (0, 1):
            point = data[index, side]
            t = ((point-start)*delta).sum(axis=1)/squared
            foot = start+np.clip(t, 0.0, 1.0)[:, None]*delta
            gap = np.linalg.norm(foot-point, axis=1)
            gap[index] = np.inf
            free[index, side] = not (gap <= snap*0.6).any()
    return free
