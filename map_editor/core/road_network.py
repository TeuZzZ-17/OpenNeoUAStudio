"""Road connections of the sector types, and a planner that joins neighbouring roads.

Urban Assault draws a road stub between any two touching sectors of a road ground, whether or not
their roads meet. A sector whose road does not reach that border leaves an orphan stub, and a road
that runs into a sector without exit looks cut off. This module reads, for every sector type, on
which sides its road really leaves the sector, and picks the type that matches the neighbours.

The exits are read from the picture of the sector, not from names or model IDs, so modded sets work
as long as their roads are dark paint on a lighter ground: the sector is drawn from above and, in
a strip at the middle of each side, the share of road-dark pixels decides whether the road crosses.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

NORTH, EAST, SOUTH, WEST = 1, 2, 4, 8
# Side -> (column step, row step); north is the previous row.
STEPS = {NORTH: (0, -1), EAST: (1, 0), SOUTH: (0, 1), WEST: (-1, 0)}
# Centre of the probe strip of each side in sector coordinates (x east, z north).
PROBES = {NORTH: (0.0, 400.0), SOUTH: (0.0, -400.0), EAST: (400.0, 0.0), WEST: (-400.0, 0.0)}
PROBE_LENGTH, PROBE_DEPTH = 240.0, 90.0
# A pixel is road when it is clearly darker than the sector's median ground.
DARK_RATIO, DARK_SHARE = .72, .30
PICTURE_SCALE = .1
# Sector types that serve a special purpose and must never be swapped for a plain road piece.
SPECIAL_NAMES = ("GEM", "SUPERITEM", "ENDLEVEL", "GATE", "KEY")
SPECIAL_TYPES = {0xf3, 0xf4}


def side_names(mask: int) -> str:
    return "".join(name for bit, name in ((NORTH, "N"), (EAST, "E"), (SOUTH, "S"), (WEST, "W")) if mask & bit)


@dataclass
class RoadPlan:
    changes: list = field(default_factory=list)      # (col, row, old_type, new_type)
    unresolved: list = field(default_factory=list)   # (col, row, wanted_mask): no sector type has that shape


class RoadMasks:
    """Exit masks of every sector type of one set (each computed once, on demand)."""

    def __init__(self, lib):
        self.lib = lib
        self._masks: dict[int, int] = {}
        self._road_grounds: dict[int, bool] = {}

    def road_ground(self, ground):
        """Recognise a road surface from its own transition texture, including modified sets."""
        if ground in self._road_grounds:
            return self._road_grounds[ground]
        from ..render.camera import IsoCamera
        from ..render.sector_sprite import rasterize_sprite, sprite_input
        camera = IsoCamera(yaw=0, pitch=90, zoom=PICTURE_SCALE)
        found = False
        for vertical in (False, True):
            mesh = self.lib.filler_mesh(ground, ground, vertical, (0,) * 10)
            sprite = rasterize_sprite(sprite_input(self.lib, mesh, 0, 90, PICTURE_SCALE), self.lib.tables, PICTURE_SCALE)
            if sprite is None:
                continue
            image = sprite.image
            pixels = np.frombuffer(image.constBits(), np.uint8).reshape(image.height(), image.width(), 4)
            luma = pixels[:, :, :3] @ np.array((.299, .587, .114))
            solid = pixels[:, :, 3] > 0
            if not solid.any():
                continue
            bounds = mesh.bounds
            point = ((bounds[0] + bounds[3]) / 2, 0, (bounds[2] + bounds[5]) / 2)
            x, y = camera.to_screen(camera.to_camera(point, True), (0, 0))
            x, y = round(sprite.anchor[0] + x), round(sprite.anchor[1] + y)
            patch = luma[max(0, y - 5):y + 5, max(0, x - 5):x + 5]
            if patch.size and float(np.median(patch)) < float(np.median(luma[solid])) * DARK_RATIO:
                found = True
                break
        self._road_grounds[ground] = found
        return found

    def mask(self, typ: int) -> int:
        if typ not in self._masks:
            self._masks[typ] = self._compute(typ)
        return self._masks[typ]

    def _compute(self, typ: int) -> int:
        from ..render.sector_sprite import rasterize_sprite, sprite_input
        from ..render.sector_mesh import SectorMesh
        from ..render.camera import IsoCamera

        lib = self.lib
        if typ not in lib.assets.sdf.sectors:
            return 0
        if not self.road_ground(lib.assets.sdf.sectors[typ].ground):
            return 0
        source = lib.mesh(typ)
        # Buildings can hide a road or cast dark shapes across an unrelated edge.
        mesh = SectorMesh(typ, faces=[(face, ox, oz) for face, ox, oz in source.faces
                                     if all(abs(v[1]) < 1e-6 for v in face.vertices)])
        if not mesh.faces:
            return 0
        sprite = rasterize_sprite(sprite_input(lib, mesh, 0, 90, PICTURE_SCALE), lib.tables, PICTURE_SCALE)
        if sprite is None:
            return 0
        image = sprite.image
        width, height = image.width(), image.height()
        raw = np.frombuffer(image.constBits(), np.uint8).reshape(height, image.bytesPerLine())[:, :width * 4]
        pixels = raw.reshape(height, width, 4)
        luma = pixels[:, :, :3] @ np.array((.299, .587, .114))
        solid = pixels[:, :, 3] > 0
        if not solid.any():
            return 0
        dark = luma < DARK_RATIO * float(np.median(luma[solid]))
        camera = IsoCamera(yaw=0, pitch=90, zoom=PICTURE_SCALE)

        def pixel(x, z):
            sx, sy = camera.to_screen(camera.to_camera((x, 0, z), True), (0, 0))
            return int(round(sprite.anchor[0] + sx)), int(round(sprite.anchor[1] + sy))

        mask = 0
        for side, (cx, cz) in PROBES.items():
            along_x = side in (NORTH, SOUTH)
            hit = total = 0
            for a in np.linspace(-PROBE_LENGTH / 2, PROBE_LENGTH / 2, 13):
                for d in np.linspace(-PROBE_DEPTH / 2, PROBE_DEPTH / 2, 5):
                    x, z = (cx + a, cz + d) if along_x else (cx + d, cz + a)
                    px, py = pixel(x, z)
                    if 0 <= px < width and 0 <= py < height and solid[py, px]:
                        total += 1
                        hit += bool(dark[py, px])
            if total and hit / total >= DARK_SHARE:
                mask |= side
        return mask

    def is_special(self, typ: int) -> bool:
        sector = self.lib.assets.sdf.sectors.get(typ)
        name = (sector.comment if sector else "").upper()
        return typ in SPECIAL_TYPES or typ >= 0xf8 or any(word in name for word in SPECIAL_NAMES)

    def road_types(self) -> list[int]:
        return [typ for typ in sorted(self.lib.assets.sdf.sectors)
                if self.mask(typ) and not self.is_special(typ)]


def _similarity(lib, a: int, b: int) -> int:
    """How many of the nine sub-buildings two sector types share at the same place."""
    left, right = lib.assets.sdf.sectors.get(a), lib.assets.sdf.sectors.get(b)
    if left is None or right is None:
        return 0
    return sum(1 for x, y in zip(left.subsects, right.subsects) if x and x == y)


def plan_road_fixes(doc, lib, masks: RoadMasks, cells=None) -> RoadPlan:
    """Sector changes that make every road cell leave exactly towards its road neighbours.

    Only plain interior sectors (no building) are touched; ``cells`` limits the changes to a
    selection while the neighbours outside it still count as road.
    """
    plan = RoadPlan()
    types = [[int(str(value), 16) for value in row] for row in doc.grids['type']]
    protected = set()
    for name in ('gates', 'items', 'gems'):
        for value in getattr(doc, name, {}).values():
            protected.add((value.get('x', -1), value.get('y', -1)))
            protected.update(tuple(cell) for cell in value.get('keys', []))
    plain = lambda c, r: str(doc.grids['blg'][r][c]).lower() == '00' and (c, r) not in protected
    interior = lambda c, r: 1 <= c < doc.mw - 1 and 1 <= r < doc.mh - 1
    road = lambda c, r: interior(c, r) and masks.mask(types[r][c]) != 0
    by_mask: dict[tuple[int, int], list[int]] = {}
    for typ in masks.road_types():
        by_mask.setdefault((lib.assets.sdf.sectors[typ].ground, masks.mask(typ)), []).append(typ)
    scope = [(c, r) for r in range(1, doc.mh - 1) for c in range(1, doc.mw - 1)] if cells is None else sorted(cells)
    for col, row in scope:
        if not interior(col, row) or not plain(col, row) or not road(col, row) or masks.is_special(types[row][col]):
            continue
        wanted = 0
        for side, (dc, dr) in STEPS.items():
            if road(col + dc, row + dr):
                wanted |= side
        current = masks.mask(types[row][col])
        if wanted == current:
            continue
        options = by_mask.get((lib.assets.sdf.sectors[types[row][col]].ground, wanted))
        if not options:
            plan.unresolved.append((col, row, wanted))
            continue
        best = max(options, key=lambda t: (_similarity(lib, t, types[row][col]), -t))
        plan.changes.append((col, row, types[row][col], best))
    return plan
