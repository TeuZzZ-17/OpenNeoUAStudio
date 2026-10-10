"""Initial LDF squad placement shared by both map renderers and selection."""
from dataclasses import dataclass
import math

from .sector_state import sector_type
from ..core.ldf_model import SECTOR_SIZE, grid_to_world

MAX_PREVIEW_MEMBERS = 256


@dataclass(frozen=True)
class SquadMember:
    squad: int
    member: int
    vehicle: int
    owner: int
    position: tuple
    body_angle: float = 0


def body_rotation(degrees):
    import numpy as np
    angle = math.radians(degrees)
    c, s = math.cos(angle), math.sin(angle)
    # World models use the transpose of the engine's RotateY body matrix.
    return np.array(((c, 0, -s), (0, 1, 0), (s, 0, c)))


def squad_xz(squad):
    x, z = grid_to_world(squad['x'], squad['y'])
    return squad.get('pos_x', x), squad.get('pos_z', z)


def centered_squad_position(squad, col, row):
    """Centre the complete runtime formation, rather than its first member."""
    count = max(1, squad['num'])
    columns = int(math.sqrt(count)) + 2
    full_rows, remainder = divmod(count, columns)
    mean_column = (full_rows * columns * (columns - 1) / 2 + remainder * (remainder - 1) / 2) / count
    mean_row = (columns * full_rows * (full_rows - 1) / 2 + full_rows * remainder) / count
    return ((col + .5) * SECTOR_SIZE - 100 * (mean_column - columns / 2) - .3,
            -(row + .5) * SECTOR_SIZE - 100 * mean_row - .3)


def _triangle_height(x, z, points):
    a, b, c = points
    det = (b[2] - c[2]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[2] - c[2])
    if abs(det) < 1e-8:
        return None
    u = ((b[2] - c[2]) * (x - c[0]) + (c[0] - b[0]) * (z - c[2])) / det
    v = ((c[2] - a[2]) * (x - c[0]) + (a[0] - c[0]) * (z - c[2])) / det
    if min(u, v, 1 - u - v) < -1e-6:
        return None
    return u * a[1] + v * b[1] + (1 - u - v) * c[1]


def ground_height(doc, terrain, lib, x, z):
    """Restituisce la superficie reale sotto una posizione X/Z della mappa."""
    col, row = math.floor(x / SECTOR_SIZE), math.floor(-z / SECTOR_SIZE)
    if not (0 <= col < doc.mw and 0 <= row < doc.mh):
        return None
    dx, dz = int((x + 150) / 300), int((-z + 150) / 300)
    c, r = dx // 4, dz // 4
    if not (0 < dx < 4 * doc.mw - 1 and 0 < dz < 4 * doc.mh - 1):
        return terrain.cell_y(col, row)
    hits, polygons = [], []
    if dx % 4 and dz % 4:
        if lib is not None:
            resolved = lib.resolver.resolve_typ(sector_type(doc, lib, c, r))
            index = 4 if resolved.sector.single else (dz % 4 - 1) * 3 + dx % 4 - 1
            sub = next((s for s in resolved.subs if s.index == index), None)
            skeleton = lib.collision_skeleton(sub.sklt_name) if sub and sub.sklt_name else None
            if skeleton is not None:
                origin = (terrain.cell_center(c, r) if resolved.sector.single
                          else (dx * 300, terrain.cell_y(c, r), -dz * 300))
                polygons = [[tuple(v + o for v, o in zip(skeleton.points[i], origin))
                             for i in p] for p in skeleton.polygons]
    else:
        # ColSide/ColCross use the same 300-unit collision patch in every set.
        cx, cz = dx * 300, -dz * 300
        current = terrain.cell_y(c, r)
        if dx % 4 == 0 and dz % 4 == 0:
            heights = (terrain.cell_y(c - 1, r - 1), terrain.cell_y(c, r - 1),
                       current, terrain.cell_y(c - 1, r))
        elif dx % 4 == 0:
            left = terrain.cell_y(c - 1, r)
            heights = (left, current, current, left)
        else:
            up = terrain.cell_y(c, r - 1)
            heights = (up, up, current, current)
        points = [(cx - 150, heights[0], cz + 150), (cx + 150, heights[1], cz + 150),
                  (cx + 150, heights[2], cz - 150), (cx - 150, heights[3], cz - 150)]
        if dx % 4 == 0 and dz % 4 == 0:
            center = (cx, sum(heights) / 4, cz)
            polygons = [(points[i], points[(i + 1) % 4], center) for i in range(4)]
        else:
            polygons = [points]
    for points in polygons:
        for i in range(2, len(points)):
            triangle = (points[0], points[i - 1], points[i])
            a, b, cc = triangle
            normal_y = (b[2] - a[2]) * (cc[0] - b[0]) - (b[0] - a[0]) * (cc[2] - b[2])
            if normal_y <= 0:
                continue
            height = _triangle_height(x, z, triangle)
            if height is not None:
                hits.append(height)
    return min(hits) if hits else terrain.cell_y(col, row)


def _vehicle_ground_offset(lib, vehicle_id):
    if lib is None:
        return 0.0
    mesh = lib.vehicle_mesh(vehicle_id)
    return mesh.bounds[4] if mesh.faces else 0.0


def squad_members(doc, terrain, lib=None):
    for index, squad in enumerate(doc.squads):
        if not doc.cell_is_valid(squad) or squad.get('num', 0) <= 0:
            continue
        x, z = squad_xz(squad)
        # The LDF parser adds 0.3 before MakeSquad uses the saved coordinates.
        x, z = x + .3, z + .3
        count = squad['num']
        columns = int(math.sqrt(count)) + 2
        ground_offset = _vehicle_ground_offset(lib, squad['veh'])
        # The game spawns a formation from one anchor and physics then settles each unit.
        # The editor has no physics step, so every preview member is placed on its own ground point.
        for member in range(min(count, MAX_PREVIEW_MEMBERS)):
            member_x = x + 100 * (member % columns - columns / 2)
            member_z = z + 100 * (member // columns)
            surface_y = ground_height(doc, terrain, lib, member_x, member_z)
            if surface_y is None:
                continue
            yield SquadMember(index, member, squad['veh'], squad['owner'],
                              (member_x, surface_y - ground_offset, member_z))


def host_position(host, doc, terrain, lib=None):
    """LDF host height is an offset from the collision surface, as in LoadRobos."""
    x, z = squad_xz(host)
    x, z = x + .3, z + .3
    surface = ground_height(doc, terrain, lib, x, z)
    return x, host['pos_y'] + .3 + (surface if surface is not None else 0), z


def host_members(doc, terrain, lib=None):
    """Host positions share the actor IDs used by both map renderers."""
    for index, host in enumerate(doc.host_stations):
        if doc.cell_is_valid(host):
            yield SquadMember(len(doc.squads) + index, 0, host['veh'], host['owner'],
                              host_position(host, doc, terrain, lib), host.get('body_angle', 0))
