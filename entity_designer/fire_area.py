"""AI fire area of a weapon (``ai_fire_trigger_*``): data, engine rules and 3D lines.

Qt-free on purpose. The geometry follows ``GetAIFireTriggerArea`` and the orange
New Debug overlay of the engine (``yw_debug.cpp``), so the Designer shows the
same lines the game draws. Coordinates are model space: x right, y DOWN, z forward.
A weapon activates exactly one of the four volumes; with none or with several
the engine ignores the whole area and the AI keeps its vanilla fire control.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

BOX_BELOW = "box_below"
CONE_BELOW = "cone_below"
BOX_FORWARD = "box_forward"
CONE_FORWARD = "cone_forward"
SHAPES = (BOX_BELOW, CONE_BELOW, BOX_FORWARD, CONE_FORWARD)

SHAPE_KEYS = {
    BOX_BELOW: "ai_fire_trigger_box_below_xyz",
    CONE_BELOW: "ai_fire_trigger_cone_below_xyz",
    BOX_FORWARD: "ai_fire_trigger_box_forward_xyz",
    CONE_FORWARD: "ai_fire_trigger_cone_forward_xyz",
}
DEAD_ZONE_KEY = "ai_fire_trigger_dead_zone"
MAX_INCLINATION_KEY = "ai_fire_trigger_forward_max_inclination"
FIRE_AREA_KEYS = frozenset(SHAPE_KEYS.values()) | {DEAD_ZONE_KEY, MAX_INCLINATION_KEY}

SHAPE_LABELS = {
    BOX_BELOW: "Box below",
    CONE_BELOW: "Cone below",
    BOX_FORWARD: "Box forward",
    CONE_FORWARD: "Cone forward",
}
# Values of the parameter guide: a sensible starting size for a new volume.
DEFAULT_SIZES = {
    BOX_BELOW: (200.0, 1200.0, 400.0),
    CONE_BELOW: (30.0, 20.0, 1200.0),
    BOX_FORWARD: (100.0, 80.0, 1200.0),
    CONE_FORWARD: (30.0, 20.0, 1200.0),
}
# Meaning of each X_Y_Z value, as the engine reads it (index in the size tuple).
SIZE_FIELDS = {
    BOX_BELOW: (("Width", 0), ("Depth (down)", 1), ("Length", 2)),
    CONE_BELOW: (("Angle sideways", 0), ("Angle front/back", 1), ("Depth (down)", 2)),
    BOX_FORWARD: (("Width", 0), ("Height", 1), ("Depth", 2)),
    CONE_FORWARD: (("Angle horizontal", 0), ("Angle vertical", 1), ("Depth", 2)),
}
MAX_DEAD_ZONE = 30000.0
MAX_INCLINATION_LIMIT = 90.0
CONE_ANGLE_LIMIT = 180.0
CONE_SEGMENTS = 32

Vec3 = tuple[float, float, float]
Segment = tuple[Vec3, Vec3]


def is_below(shape: str) -> bool:
    return shape in (BOX_BELOW, CONE_BELOW)


def is_cone(shape: str) -> bool:
    return shape in (CONE_BELOW, CONE_FORWARD)


def usable_family(model: str, vertical_laser: bool = False) -> str | None:
    """"below" or "forward": the volumes the engine accepts for this weapon class.

    None when the class is not known here, so no advice is shown for it
    (kamikaze and artillery_shell never use a volume).
    """

    model = model.strip().casefold()
    if model in ("bomb", "homing_bomb") or (model == "laser" and vertical_laser):
        return "below"
    if model in ("rocket", "missile", "grenade", "arc_grenade", "laser"):
        return "forward"
    return None


AIM_LOW = "low"
AIM_HIGH = "high"


def shot_path(shape: str, size, muzzle: Vec3, ground_y: float = 0.0,
              max_inclination: float = 0.0, aim: str = AIM_LOW) -> tuple[Vec3, Vec3]:
    """(launch, target) of the preview shot.

    A dropped weapon falls straight down. A fired one flies, by ``aim``, to the
    ground at the far end of its volume (low zone: with a tilt limit it never aims
    steeper than that) or straight ahead along the untilted volume (high zone).
    """

    launch = tuple(float(value) for value in muzzle)
    if is_below(shape):
        return launch, (launch[0], ground_y, launch[2])
    if aim == AIM_HIGH:
        return launch, (launch[0], launch[1], launch[2] + float(size[2]))
    reach = float(size[2])
    height = ground_y - launch[1]
    if max_inclination > 0.0 and height > 0.0:
        reach = max(reach, height / math.tan(math.radians(max_inclination)))
    return launch, (launch[0], ground_y, launch[2] + reach)


def default_height(shape: str | None, size) -> float:
    """Preview height of the muzzle: a dropped weapon starts one volume depth up
    so the volume ends on the floor; a fired one starts a little above the ground."""

    if shape is None or size is None:
        return 0.0
    if is_below(shape):
        return float(size[1 if shape == BOX_BELOW else 2])
    return 80.0


def valid_size(shape: str, size) -> bool:
    """The engine's rule: three positive finite values, cone angles below 180."""

    if size is None or len(size) != 3:
        return False
    if not all(math.isfinite(value) and value > 0.0 for value in size):
        return False
    return not (is_cone(shape) and (size[0] >= CONE_ANGLE_LIMIT or size[1] >= CONE_ANGLE_LIMIT))


def valid_dead_zone(value: float) -> bool:
    return math.isfinite(value) and 0.0 <= value <= MAX_DEAD_ZONE


def valid_max_inclination(value: float) -> bool:
    return math.isfinite(value) and 0.0 <= value < MAX_INCLINATION_LIMIT


@dataclass(frozen=True)
class FireArea:
    """The six ``ai_fire_trigger_*`` values of a weapon; ``None`` / 0 means absent."""

    box_below: tuple[float, float, float] | None = None
    cone_below: tuple[float, float, float] | None = None
    box_forward: tuple[float, float, float] | None = None
    cone_forward: tuple[float, float, float] | None = None
    dead_zone: float = 0.0
    max_inclination: float = 0.0

    def size(self, shape: str):
        return getattr(self, shape)

    def authored_shapes(self) -> tuple[str, ...]:
        return tuple(shape for shape in SHAPES if self.size(shape) is not None)

    def active_shape(self) -> str | None:
        """The volume the engine would use, or None when none/several are set."""

        shapes = self.authored_shapes()
        return shapes[0] if len(shapes) == 1 else None

    def with_size(self, shape: str, size) -> "FireArea":
        return replace(self, **{shape: None if size is None else tuple(float(v) for v in size)})

    def with_only(self, shape: str | None) -> "FireArea":
        """Keep a single volume (its size, or the guide default) and drop the others."""

        if shape is None:
            return replace(self, box_below=None, cone_below=None,
                           box_forward=None, cone_forward=None)
        size = self.size(shape) or DEFAULT_SIZES[shape]
        return self.with_only(None).with_size(shape, size)

    def is_empty(self) -> bool:
        return (not self.authored_shapes() and self.dead_zone <= 0.0
                and self.max_inclination <= 0.0)


def fire_area_from_values(values: dict[str, str]) -> FireArea:
    """Read the raw script values (lower-case key -> text) like the engine parser.

    ``0`` and invalid values leave a parameter absent; the engine logs a warning
    and disables it too.
    """

    area = FireArea()
    for shape, key in SHAPE_KEYS.items():
        raw = values.get(key)
        if raw is None or raw.strip() == "0":
            continue
        token = raw.strip().split(maxsplit=1)[0]
        try:
            size = tuple(float(part) for part in token.split("_"))
        except ValueError:
            continue
        if valid_size(shape, size):
            area = area.with_size(shape, size)
    for key, field, check in ((DEAD_ZONE_KEY, "dead_zone", valid_dead_zone),
                              (MAX_INCLINATION_KEY, "max_inclination", valid_max_inclination)):
        raw = values.get(key)
        if raw is None:
            continue
        try:
            number = float(raw.strip().split(maxsplit=1)[0])
        except (ValueError, IndexError):
            continue
        if check(number):
            area = replace(area, **{field: number})
    return area


# -- geometry ---------------------------------------------------------------------------


def _axes(shape: str, tilt_deg: float = 0.0) -> tuple[Vec3, Vec3, Vec3]:
    """Axes (right, second cross-section, depth) of a volume in model space.

    Below volumes stay plumb: depth goes down (+y) and the second axis is the
    heading (+z). A forward volume tilted by ``tilt_deg`` turns toward the ground.
    """

    if is_below(shape):
        return (1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, 1.0, 0.0)
    tilt = math.radians(tilt_deg)
    c, s = math.cos(tilt), math.sin(tilt)
    return (1.0, 0.0, 0.0), (0.0, c, -s), (0.0, s, c)


def _layout(shape: str, size) -> tuple[float, float, float, float]:
    """(half width, half second extent / cone tan, depth, cone tan y) of a volume."""

    if is_cone(shape):
        return (math.tan(math.radians(size[0]) / 2.0), 0.0, size[2],
                math.tan(math.radians(size[1]) / 2.0))
    if shape == BOX_BELOW:
        return size[0] / 2.0, size[2] / 2.0, size[1], 0.0
    return size[0] / 2.0, size[1] / 2.0, size[2], 0.0


def _point(origin: Vec3, axes, along: Vec3) -> Vec3:
    return tuple(origin[i] + axes[0][i] * along[0] + axes[1][i] * along[1] + axes[2][i] * along[2]
                 for i in range(3))


def x_span_of(fire_x: float) -> tuple[float, float]:
    """Sideways extent of the muzzles of a unit: the engine uses +-|fire_x|."""

    side = abs(float(fire_x))
    return -side, side


def volume_segments(shape: str, size, dead_zone: float, origin: Vec3 = (0.0, 0.0, 0.0),
                    tilt_deg: float = 0.0,
                    x_span: tuple[float, float] = (0.0, 0.0)) -> list[Segment]:
    """Lines of one volume, the same set the engine's New Debug draws.

    ``x_span`` is the sideways range of the unit's muzzles (fire_x): the volume is
    swept over it, so a wide rack gets a wider volume.
    """

    if not valid_size(shape, size):
        return []
    axes = _axes(shape, tilt_deg)
    half_w, half_s, depth, cone_y = _layout(shape, size)
    near = dead_zone if 0.0 <= dead_zone < depth else 0.0
    min_x, max_x = x_span
    segments: list[Segment] = []
    if not is_cone(shape):
        corners = [
            _point(origin, axes, (max_x + half_w if i & 1 else min_x - half_w,
                                  half_s if i & 2 else -half_s,
                                  depth if i & 4 else near))
            for i in range(8)
        ]
        for i in range(8):
            for bit in (1, 2, 4):
                if not i & bit:
                    segments.append((corners[i], corners[i | bit]))
        return segments

    def rim(angle: float, distance: float) -> Vec3:
        cosine = math.cos(angle)
        return _point(origin, axes, ((min_x if cosine < 0.0 else max_x) + cosine * half_w * distance,
                                     math.sin(angle) * cone_y * distance, distance))

    for i in range(CONE_SEGMENTS):
        angle = 2.0 * math.pi * i / CONE_SEGMENTS
        following = 2.0 * math.pi * (i + 1) / CONE_SEGMENTS
        segments.append((rim(angle, depth), rim(following, depth)))
        if near > 0.0:
            segments.append((rim(angle, near), rim(following, near)))
        if i % (CONE_SEGMENTS // 4) == 0:
            segments.append((rim(angle, near), rim(angle, depth)))
    if max_x > min_x:
        segments.append((_point(origin, axes, (min_x, 0.0, near)),
                         _point(origin, axes, (max_x, 0.0, near))))
    return segments


@dataclass(frozen=True)
class AreaHandle:
    """A grip on the volume: dragging it along ``axis`` changes one value."""

    key: str
    label: str
    point: Vec3
    axis: Vec3


def volume_handles(shape: str, size, dead_zone: float,
                   origin: Vec3 = (0.0, 0.0, 0.0),
                   x_span: tuple[float, float] = (0.0, 0.0)) -> list[AreaHandle]:
    if not valid_size(shape, size):
        return []
    axes = _axes(shape)
    half_w, half_s, depth, cone_y = _layout(shape, size)
    near = dead_zone if 0.0 <= dead_zone < depth else 0.0
    max_x = x_span[1]
    if is_cone(shape):
        grips = [
            ("angle_x", f"{size[0]:g}°", (max_x + half_w * depth, 0.0, depth), axes[0]),
            ("angle_y", f"{size[1]:g}°", (0.0, cone_y * depth, depth), axes[1]),
        ]
    else:
        height_index = _size_index(shape, "height")
        grips = [
            ("width", f"{size[0]:g}", (max_x + half_w, 0.0, depth), axes[0]),
            ("height", f"{size[height_index]:g}", (0.0, half_s, depth), axes[1]),
        ]
    depth_index = 1 if shape == BOX_BELOW else 2
    grips.append(("depth", f"{size[depth_index]:g}", (0.0, 0.0, depth), axes[2]))
    grips.append(("dead_zone", f"{near:g}", (0.0, 0.0, near), axes[2]))
    return [AreaHandle(key, label, _point(origin, axes, along), axis)
            for key, label, along, axis in grips]


def _size_index(shape: str, key: str) -> int:
    if key in ("angle_x", "width"):
        return 0
    if key == "angle_y":
        return 1
    if key == "height":
        return 2 if shape == BOX_BELOW else 1
    return 1 if shape == BOX_BELOW else 2  # depth


def dragged(shape: str, size, dead_zone: float, key: str, delta: float):
    """(size, dead_zone) after moving handle ``key`` by ``delta`` along its axis."""

    values = list(size)
    if key == "dead_zone":
        depth = values[_size_index(shape, "depth")]
        return tuple(values), max(0.0, min(dead_zone + delta, depth - 1.0))
    index = _size_index(shape, key)
    if key == "depth":
        values[index] = max(1.0, round(values[index] + delta))
    elif key in ("width", "height"):
        # The handle sits on the half extent, the value is the whole one.
        values[index] = max(1.0, round(values[index] + 2.0 * delta))
    else:
        depth = values[_size_index(shape, "depth")]
        half = math.tan(math.radians(values[index]) / 2.0) * depth + delta
        values[index] = max(1.0, min(round(math.degrees(2.0 * math.atan2(half, depth))),
                                     CONE_ANGLE_LIMIT - 1.0))
    return tuple(values), dead_zone


def bounds_of(segments) -> tuple[Vec3, float]:
    """(centre, radius) of a set of lines, for framing the camera."""

    points = [point for segment in segments for point in segment]
    if not points:
        return (0.0, 0.0, 0.0), 1.0
    low = [min(point[i] for point in points) for i in range(3)]
    high = [max(point[i] for point in points) for i in range(3)]
    center = tuple((low[i] + high[i]) / 2.0 for i in range(3))
    radius = max(math.dist(low, high) / 2.0, 1.0)
    return center, radius
