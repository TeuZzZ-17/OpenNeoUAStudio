"""Vehicle/weapon script model of the Entity Designer: collision project data classes, reading
and writing script blocks, overwrite previews and validation. Pure Python, no Qt.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import difflib
import math
from pathlib import Path
import re
import sys


from entity_designer.shape import (
    CollisionShape,
)
from entity_designer.sphere_generator import (
    UNIT_COLL_MAX_COUNT,
)
from map_editor.core.three_ds import read_3ds
from studio.assets.family import AssetFamily, FamilyObject, load_asset_family, load_manual_family
from studio.assets.visproto import (
    EmbeddedVPSet,
    load_visproto,
    VPParseError,
    VPTable,
)
from studio.formats.base_format import BaseObject
from studio.formats.sklt import SkltModel

_PUBLIC_DEPENDENCY_DEFAULTS = {
    "load_asset_family": load_asset_family,
    "load_manual_family": load_manual_family,
}


LEGACY = "legacy"


OPENNEOUA = "openneoua"


VEHICLE = "vehicle"  # target category only


WEAPON = "weapon"    # target category only


WINDOW_TITLE = "Entity Designer — OpenNeoUA Studio"


COMPOUND_TYPES = (OPENNEOUA,)


TYPE_LABELS = {
    LEGACY: "Legacy Radius",
    OPENNEOUA: "OpenNeoUA Collision",
}


GUN_ANGLE_SCALE = 1000.0


GUN_SIDE_ANGLE_MAX = 3200


GUN_VERTICAL_ANGLE_MAX = 1600


# NC_STACK_ypagun disables the side clamp above 3.1 radians. 3200 is the
# conventional script value for a freely rotating turret. Up/Down remain
# intentionally limited to 1600 (~91.7 deg) in the editor.
GUN_FREE_SIDE_THRESHOLD = 3100


GUN_POINT_TABLE_BASE_WIDTHS = (38, 76, 56, 44, 34, 48, 34, 34, 34)


GUN_POINT_TABLE_GROWTH = (0, 18, 12, 14, 12, 14, 10, 10, 10)


# Ordered around the XZ plane from +Z toward +X. Diagonals intentionally keep
# the vanilla script form (1/1) rather than storing normalized decimals; the
# runtime normalizes gun_dir when it builds the gun basis.
GUN_DIRECTION_SEQUENCE = (
    (0.0, 1.0),
    (1.0, 1.0),
    (1.0, 0.0),
    (1.0, -1.0),
    (0.0, -1.0),
    (-1.0, -1.0),
    (-1.0, 0.0),
    (-1.0, 1.0),
)


GUN_DIRECTION_LABELS = (
    "0 deg (+Z)",
    "45 deg (+X +Z)",
    "90 deg (+X)",
    "135 deg (+X -Z)",
    "180 deg (-Z)",
    "225 deg (-X -Z)",
    "270 deg (-X)",
    "315 deg (-X +Z)",
)


def _gun_angle_max(field: str) -> int:
    """Return the editor/runtime-safe maximum for one gun-angle field."""

    return (
        GUN_SIDE_ANGLE_MAX
        if field == "side" else GUN_VERTICAL_ANGLE_MAX
    )


def _clamp_gun_angle(field: str, value: int) -> int:
    return max(0, min(_gun_angle_max(field), int(value)))


SCRIPT_TYPES = (
    "new_vehicle", "modify_vehicle", "new_weapon", "modify_weapon",
)


OPENNEOUA_SCRIPT_FILE_FILTER = (
    "OpenNeoUA scripts (*.cfg *.scr *.txt *.ini *.ldf);;All files (*)"
)


_SCRIPT_TAB_COLLISION = "collision"


_SCRIPT_TAB_FIRE = "fire"


_SCRIPT_TAB_GUN = "gun"


_SCRIPT_TAB_COCKPIT = "cockpit"


_SCRIPT_TAB_BRIEFING = "briefing"


_SCRIPT_TAB_ORDER = (
    _SCRIPT_TAB_COLLISION, _SCRIPT_TAB_FIRE,
    _SCRIPT_TAB_GUN, _SCRIPT_TAB_COCKPIT, _SCRIPT_TAB_BRIEFING,
)


_SCRIPT_TAB_LABELS = {
    _SCRIPT_TAB_COLLISION: "Collision",
    _SCRIPT_TAB_FIRE: "Fire Points",
    _SCRIPT_TAB_GUN: "Gun Points",
    _SCRIPT_TAB_COCKPIT: "Cockpit View",
    _SCRIPT_TAB_BRIEFING: "Briefing Model Size",
}


_SCRIPT_TAB_MARKER_RE = re.compile(
    r"^\s*;\s*\[Tab:\s*(Collision|Fire Points|Gun Points|Cockpit View|Briefing Model Size|Briefing Radius)\]\s*$",
    re.IGNORECASE,
)


_RADIUS_SLIDER_STEPS = 10000


_RADIUS_LOG_MIN = -3.0


_RADIUS_LOG_MAX = 6.0


_HEADER_RE = re.compile(
    r"^\s*(new_vehicle|modify_vehicle|new_weapon|modify_weapon)"
    r"\s+(-?\d+)\b", re.IGNORECASE,
)


_PARAM_RE = re.compile(
    r"^(?P<indent>\s*)(?P<key>radius|overeof|fire_x|fire_y|fire_z|"
    r"num_weapons|briefing_size|cockpit_camera_offset_xyz|"
    r"robo_num_guns|robo_act_gun|robo_gun_pos_x|robo_gun_pos_y|"
    r"robo_gun_pos_z|robo_gun_dir_x|robo_gun_dir_y|robo_gun_dir_z|"
    r"robo_gun_type|robo_gun_name|"
    r"unit_num_guns|unit_act_gun|unit_gun_pos_xyz|unit_gun_dir_xyz|"
    r"unit_gun_type|unit_gun_name|unit_gun_icon|"
    r"coll_num|coll_act|"
    r"coll_xyz|coll_radius)\s*=\s*(?P<value>[^;#\r\n]+)",
    re.IGNORECASE,
)


_GENERIC_PARAM_RE = re.compile(
    r"^(?P<indent>\s*)(?P<key>[A-Za-z0-9_][A-Za-z0-9_]*)\s*=\s*"
    r"(?P<value>[^;#\r\n]+)",
    re.IGNORECASE,
)


def _number(value: float) -> str:
    if math.isfinite(value) and abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _float_tuple(raw: str, count: int, key: str) -> tuple[float, ...]:
    """Read an OpenNeoUA compact ``a_b_c`` value holding exactly *count* numbers."""

    token = raw.strip().split(maxsplit=1)[0] if raw.strip() else ""
    parts = token.split("_")
    if len(parts) != count or not all(parts):
        raise CollisionScriptError(
            f"Valore non valido per {key}: {raw} "
            f"(attesi {count} numeri separati da _).")
    try:
        values = tuple(float(part) for part in parts)
    except ValueError as exc:
        raise CollisionScriptError(
            f"Valore non numerico per {key}: {raw}") from exc
    if not all(math.isfinite(value) for value in values):
        raise CollisionScriptError(f"Valore non finito per {key}: {raw}")
    return values


def _tuple_text(*values: float) -> str:
    """Render numbers as an OpenNeoUA compact ``a_b_c`` value."""

    return "_".join(_number(value) for value in values)


def _data_root_for_path(*paths: str | Path | None) -> Path | None:
    """Find the nearest explicit game ``Data`` folder beside a source path."""

    for value in paths:
        if not value:
            continue
        path = Path(value).expanduser().resolve(strict=False)
        candidates = (path, *path.parents)
        for candidate in candidates:
            if candidate.name.casefold() == "data" and candidate.is_dir():
                return candidate
    return None


def _resolve_data_asset_path(
        value: str, *source_paths: str | Path | None) -> Path | None:
    """Resolve a script visual/collision reference without doubling Data/."""

    raw = value.strip().replace("\\", "/")
    if not raw or raw.casefold() == "0":
        return None
    candidates: list[Path] = []
    from map_editor import bootstrap
    install = bootstrap.installation()
    if install is not None and raw.casefold().startswith("data/"):
        parts = raw.split("/")[1:]
        folder_keys = {"models": "models", "sets": "sets", "scripts": "scripts",
                       "levels": "levels", "interface": "interface", "objects": "skies"}
        if parts and parts[0].casefold() in folder_keys:
            candidates.append(install.folder(folder_keys[parts[0].casefold()]).joinpath(*parts[1:]))
        candidates.append(install.data.joinpath(*parts))
    if raw.casefold().startswith("data/"):
        for source in source_paths:
            data_root = _data_root_for_path(source)
            if data_root is not None:
                candidates.append(data_root.parent / Path(raw))
        relative = Path(*raw.split("/")[1:])
        for source in source_paths:
            if source:
                root = _data_root_for_path(source)
                if root is not None:
                    candidates.append(root / relative)
    else:
        relative = Path(raw)
        for source in source_paths:
            if source:
                path = Path(source).expanduser().resolve(strict=False)
                anchor = path if path.is_dir() else path.parent
                candidates.extend((anchor / relative,))
                data_root = _data_root_for_path(path)
                if data_root is not None:
                    candidates.append(data_root / relative)
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve(strict=False)
    return None


def _collision_shape_export_filename(project: "CollisionProject") -> str:
    """Return a safe, readable Windows filename for one unit's shape."""

    display_name = project.name.strip()
    if display_name.casefold() in {"", "model", "unknown"}:
        model_path = (project.source_model or project.source_base).replace(
            "\\", "/")
        display_name = Path(model_path).stem
    candidate = re.sub(r'[<>:"/\\|?*\x00-\x1f\s]+', "_", display_name)
    candidate = candidate.strip(" ._")
    if not candidate:
        candidate = "Model"
    if candidate.upper() in {
            "CON", "PRN", "AUX", "NUL",
            *(f"COM{index}" for index in range(1, 10)),
            *(f"LPT{index}" for index in range(1, 10)),
    }:
        candidate += "_"
    return f"{candidate}.collision"


def _sphere_number(value: float) -> str:
    """Compact compound-sphere number with at most three decimals."""

    if not math.isfinite(value):
        return str(value)
    rounded = round(value, 3)
    if rounded == 0.0:
        rounded = 0.0
    return f"{rounded:.3f}".rstrip("0").rstrip(".")


def _radius_number(value: float) -> str:
    """Engine-safe whole-number radius used by lists and text output."""

    return str(int(round(value)))


def _parse_num_weapons_range(raw: str) -> tuple[int, int]:
    """Parse OpenNeoUA's fixed ``value`` or inclusive ``min_max`` syntax."""

    parts = raw.strip().split("_")
    if len(parts) not in (1, 2) or any(not part for part in parts):
        raise ValueError(raw)
    try:
        values = [int(part, 0) for part in parts]
    except ValueError as exc:
        raise ValueError(raw) from exc
    if len(values) == 1:
        minimum = maximum = values[0]
    else:
        minimum, maximum = sorted(values)

    # Match the runtime contract: fixed 0 preserves vanilla one-projectile
    # semantics. Mixed zero ranges are invalid; authored ranges are 1..255.
    if minimum == 0 and maximum == 0:
        return 0, 0
    if minimum < 1 or maximum > 255:
        raise ValueError(raw)
    return minimum, maximum


def _project_num_weapons_range(
        project: "CollisionProject",
) -> tuple[int, int]:
    minimum = int(project.num_weapons)
    maximum = (
        minimum if int(project.num_weapons_max) < 0
        else int(project.num_weapons_max)
    )
    if maximum < minimum:
        minimum, maximum = maximum, minimum
    return minimum, maximum


def _num_weapons_text(project: "CollisionProject") -> str:
    minimum, maximum = _project_num_weapons_range(project)
    return str(minimum) if minimum == maximum else f"{minimum}_{maximum}"


def _fire_point_preview_count(project: "CollisionProject") -> int:
    """Preview the largest possible random salvo in the shared 3D rack."""

    _minimum, maximum = _project_num_weapons_range(project)
    return max(1, maximum)


def fire_point_positions(project: "CollisionProject") -> list[tuple[float, float, float]]:
    """Return the exact vanilla projectile spawn offsets for a vehicle.

    Vanilla Urban Assault treats ``num_weapons`` values of zero and one as a
    single muzzle at the authored ``fire_x/y/z`` position.  With two or more
    weapons it distributes the muzzles evenly on local X from
    ``-abs(fire_x)`` to ``+abs(fire_x)`` while preserving fire Y and Z.
    """

    count = _fire_point_preview_count(project)
    if count == 1:
        xs = [float(project.fire_x)]
    else:
        extent = abs(float(project.fire_x))
        xs = [
            (index * 2.0 * extent / (count - 1)) - extent
            for index in range(count)
        ]
    return [
        (x, float(project.fire_y), float(project.fire_z))
        for x in xs
    ]


def effective_runtime_radius(project: "CollisionProject") -> float:
    """Return the collision broad-phase extent used internally by OpenNeoUA.

    Manual ``coll_*`` spheres replace the vanilla Legacy Radius.  When at
    least one compound sphere exists, the engine derives its internal broad
    extent exclusively from those spheres using:

    ``max(length(coll_center) + coll_radius)``.

    The returned value is *not* a visible red F10 collision sphere.  Legacy
    Radius is used only when no manual compound spheres are present.  The
    historical function name is kept as a small public-API compatibility shim.
    """

    if not project.compound:
        return (
            max(0.0, float(project.legacy.radius))
            if project.legacy is not None else 0.0
        )

    broad = 0.0
    for sphere in project.compound:
        if not all(math.isfinite(value) for value in (
                sphere.x, sphere.y, sphere.z, sphere.radius)):
            continue
        if sphere.radius <= 0.0:
            continue
        extent = math.sqrt(
            sphere.x * sphere.x
            + sphere.y * sphere.y
            + sphere.z * sphere.z
        ) + sphere.radius
        broad = max(broad, extent)
    return broad


def _public_dependency(name: str, fallback):
    """Honor legacy patches made against the public package namespace."""

    default = _PUBLIC_DEPENDENCY_DEFAULTS.get(name, fallback)
    if fallback is not default:
        return fallback
    package = sys.modules.get(__package__)
    return getattr(package, name, fallback) if package is not None else fallback


@dataclass
class CollisionSphere:
    category: str
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    radius: float = 1.0
    visible: bool = True

    @property
    def center(self) -> tuple[float, float, float]:
        return (self.x, self.y, self.z)

    def clone(self) -> "CollisionSphere":
        return CollisionSphere(
            self.category, self.x, self.y, self.z, self.radius, self.visible,
        )


@dataclass
class GunPoint:
    """One authored gun mount from either legacy Robo or generic OpenNeoUA data."""

    scheme: str = "unit"
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    dir_x: float = 0.0
    dir_y: float = 0.0
    dir_z: float = 0.0
    gun_type: int = 0
    name: str = ""
    icon: str = ""

    @property
    def position(self) -> tuple[float, float, float]:
        return (self.x, self.y, self.z)

    @property
    def direction(self) -> tuple[float, float, float]:
        return (self.dir_x, self.dir_y, self.dir_z)

    def clone(self) -> "GunPoint":
        return GunPoint(
            self.scheme, self.x, self.y, self.z,
            self.dir_x, self.dir_y, self.dir_z,
            self.gun_type, self.name, self.icon,
        )


@dataclass
class TurretLimits:
    """Runtime gun envelope owned by one referenced vehicle prototype.

    Gun Points store only mount position/direction and the referenced vehicle
    ID. ``gun_side_angle``/``gun_up_angle``/``gun_down_angle`` belong to that
    referenced vehicle definition, so all mounts using the same ID deliberately
    share one instance in :class:`CollisionProject`.
    """

    vehicle_id: int
    enabled: bool = False
    side: int = 0
    up: int = 0
    down: int = 0
    source_kind: str = "new_vehicle"
    source_name: str = ""
    dirty: bool = False

    def clone(self) -> "TurretLimits":
        return TurretLimits(
            self.vehicle_id, self.enabled, self.side, self.up, self.down,
            self.source_kind, self.source_name, self.dirty,
        )


@dataclass
class CollisionProject:
    name: str = ""
    source_model: str = ""
    source_base: str = ""
    target_category: str = VEHICLE
    # Visual-only preview of OpenNeoUA's scale_xyz. These values never
    # modify the source model and are never emitted as collision parameters.
    model_scale_x: float = 1.0
    model_scale_y: float = 1.0
    model_scale_z: float = 1.0
    model_rotation_x: float = 0.0
    model_rotation_y: float = 0.0
    model_rotation_z: float = 0.0
    # Vehicle-only ground alignment.  When enabled, the preview follows the
    # engine placement rule ``actor_y = ground_y - overeof`` and the value is
    # included in text export/application.
    overeof_enabled: bool = False
    overeof: float = 0.0
    # Vanilla vehicle projectile-spawn preview.  These fields are written only
    # when explicitly enabled and never modify the loaded BAS/SKLT asset.
    fire_points_enabled: bool = False
    fire_x: float = 0.0
    fire_y: float = 0.0
    fire_z: float = 0.0
    num_weapons: int = 1
    # -1 means "same as num_weapons" for backward compatibility with older
    # callers/tests that construct CollisionProject with only the scalar.
    num_weapons_max: int = -1
    # Vehicle gun mounts. ``robo`` preserves the original Host Station syntax;
    # ``unit`` is OpenNeoUA's generic vehicle-side implementation. Existing
    # scripts retain their family; the editor explicitly chooses the family
    # for each newly-authored point instead of inferring it from a bare model.
    gun_points_enabled: bool = False
    gun_points: list[GunPoint] = field(default_factory=list)
    unit_gun_default_icon: str = ""
    # Effective angle limits indexed by the mounted gun's vehicle prototype ID.
    # Entries are populated from the same script that supplied the Gun Points;
    # only explicitly edited (dirty) entries are written back.
    turret_limits: dict[int, TurretLimits] = field(default_factory=dict)
    # OpenNeoUA-only modern cockpit camera position. The engine consumes these
    # values directly in vehicle-local model space; there is deliberately no
    # editor-authored rotation because OpenNeoUA keeps the vehicle orientation.
    cockpit_camera_enabled: bool = False
    cockpit_camera_offset_x: float = 0.0
    cockpit_camera_offset_y: float = 0.0
    cockpit_camera_offset_z: float = 0.0
    briefing_size_enabled: bool = False
    briefing_size: float = 0.0
    legacy: CollisionSphere | None = None
    compound: list[CollisionSphere] = field(default_factory=list)
    collision_shape: CollisionShape | None = None
    collision_shape_path: str = ""
    collision_shape_file_path: str = ""
    collision_shape_owners: tuple[str, ...] | None = None
    collision_shape_components: tuple[str, ...] | None = None
    collision_shape_warnings: tuple[str, ...] = ()
    # Editor-only focus state. It is kept in undo/redo snapshots so adding a
    # new sphere while isolated does not make the visibility controls forget
    # that the user is still working in isolation mode.
    sphere_isolation_active: bool = False

    def spheres(self) -> list[CollisionSphere]:
        return ([self.legacy] if self.legacy is not None else []) + list(
            self.compound)

    def snapshot(self) -> tuple:
        def one(sphere):
            return None if sphere is None else (
                sphere.category, sphere.x, sphere.y, sphere.z,
                sphere.radius, sphere.visible,
            )
        return (
            self.name, self.source_model, self.source_base,
            self.target_category,
            self.model_scale_x, self.model_scale_y, self.model_scale_z,
            self.model_rotation_x, self.model_rotation_y,
            self.model_rotation_z,
            self.overeof_enabled, self.overeof,
            self.fire_points_enabled,
            self.fire_x, self.fire_y, self.fire_z,
            self.num_weapons, self.num_weapons_max,
            self.gun_points_enabled, self.unit_gun_default_icon,
            self.cockpit_camera_enabled,
            self.cockpit_camera_offset_x, self.cockpit_camera_offset_y,
            self.cockpit_camera_offset_z,
            self.briefing_size_enabled, self.briefing_size,
            tuple((
                point.scheme, point.x, point.y, point.z,
                point.dir_x, point.dir_y, point.dir_z, point.gun_type,
                point.name, point.icon,
            ) for point in self.gun_points),
            tuple((
                vehicle_id, limits.enabled, limits.side, limits.up,
                limits.down, limits.source_kind, limits.source_name,
                limits.dirty,
            ) for vehicle_id, limits in sorted(self.turret_limits.items())),
            one(self.legacy),
            tuple(one(sphere) for sphere in self.compound),
            self.collision_shape, self.collision_shape_path,
            self.collision_shape_file_path, self.collision_shape_owners,
            self.collision_shape_components,
            self.collision_shape_warnings,
            self.sphere_isolation_active,
        )

    def restore(self, state: tuple) -> None:
        def one(values):
            return None if values is None else CollisionSphere(*values)
        (self.name, self.source_model, self.source_base,
         self.target_category,
         self.model_scale_x, self.model_scale_y, self.model_scale_z,
         self.model_rotation_x, self.model_rotation_y,
         self.model_rotation_z,
         self.overeof_enabled, self.overeof,
         self.fire_points_enabled,
         self.fire_x, self.fire_y, self.fire_z,
         self.num_weapons, self.num_weapons_max,
         self.gun_points_enabled, self.unit_gun_default_icon,
         self.cockpit_camera_enabled,
         self.cockpit_camera_offset_x, self.cockpit_camera_offset_y,
         self.cockpit_camera_offset_z,
         self.briefing_size_enabled, self.briefing_size, gun_points, turret_limits,
         legacy, compound, self.collision_shape,
         self.collision_shape_path, self.collision_shape_file_path,
         self.collision_shape_owners,
         self.collision_shape_components,
         self.collision_shape_warnings,
         self.sphere_isolation_active) = state
        self.gun_points = [GunPoint(*values) for values in gun_points]
        self.turret_limits = {
            values[0]: TurretLimits(*values) for values in turret_limits
        }
        self.legacy = one(legacy)
        self.compound = [one(values) for values in compound]


@dataclass(frozen=True)
class ScriptBlock:
    kind: str
    object_id: int
    start_line: int
    end_line: int
    name: str = ""
    complete: bool = True

    @property
    def label(self) -> str:
        suffix = f" — {self.name}" if self.name else ""
        return f"{self.kind} {self.object_id}{suffix}"


@dataclass(frozen=True)
class VehicleModelReference:
    """One script definition that can resolve a visual prototype."""

    block: ScriptBlock
    vp_normal: int | None
    vp_wait: int | None = None
    scale_x: float = 1.0
    scale_y: float = 1.0
    scale_z: float = 1.0
    rotation_x: float = 0.0
    rotation_y: float = 0.0
    rotation_z: float = 0.0
    base_normal: str = ""
    three_ds_normal: str = ""
    collision_shape_path: str = ""

    @property
    def label(self) -> str:
        suffix = f" — {self.block.name}" if self.block.name else ""
        wait = (
            f", vp_wait {self.vp_wait}"
            if self.vp_wait is not None else "")
        visual = (
            f"3ds_normal {self.three_ds_normal}"
            if self.three_ds_normal else
            f"base_normal {self.base_normal}"
            if self.base_normal else
            f"vp_normal {self.vp_normal}"
        )
        return (
            f"{self.block.kind} {self.block.object_id}{suffix}  "
            f"[{visual}{wait}]"
        )


class CollisionScriptError(ValueError):
    pass


def _active_code(line: str) -> str:
    stripped = line.lstrip()
    if not stripped or stripped.startswith((";", "#", "//")):
        return ""
    return re.split(r";|//|#", stripped, maxsplit=1)[0].strip()


def _opens_nested_scope(token: str) -> bool:
    return token == "begin" or token.startswith("begin_")


def find_script_blocks(text: str) -> list[ScriptBlock]:
    """Find complete target definitions while respecting nested begin/end."""

    lines = text.splitlines()
    blocks: list[ScriptBlock] = []
    index = 0
    while index < len(lines):
        code = _active_code(lines[index])
        match = _HEADER_RE.match(code)
        if match is None:
            index += 1
            continue
        kind = match.group(1).lower()
        object_id = int(match.group(2))
        depth = 1
        end_line = len(lines)
        complete = False
        name = ""
        cursor = index + 1
        while cursor < len(lines):
            nested = _active_code(lines[cursor])
            token = nested.split(None, 1)[0].lower() if nested else ""
            if _opens_nested_scope(token):
                depth += 1
            elif token == "end":
                depth -= 1
                if depth == 0:
                    end_line = cursor
                    complete = True
                    break
            if depth == 1 and not name:
                name_match = re.match(
                    r"^name\s*=\s*(.+?)\s*$", nested, re.IGNORECASE)
                if name_match:
                    name = name_match.group(1).strip().strip('"')
            cursor += 1
        blocks.append(ScriptBlock(
            kind, object_id, index, end_line, name, complete))
        index = cursor + 1 if complete else len(lines)
    return blocks


def _parameter_rows(lines: list[str], block: ScriptBlock):
    rows = []
    nested_depth = 0
    for index in range(block.start_line + 1, block.end_line):
        code = _active_code(lines[index])
        token = code.split(None, 1)[0].lower() if code else ""
        if _opens_nested_scope(token):
            nested_depth += 1
            continue
        if token == "end" and nested_depth:
            nested_depth -= 1
            continue
        if nested_depth:
            continue
        match = _PARAM_RE.match(lines[index])
        if match is None or not code:
            continue
        raw_value = match.group("value").strip().split()[0]
        rows.append((
            index, match.group("key").lower(), raw_value,
            match.group("indent"),
        ))
    return rows


def _generic_parameter_rows(lines: list[str], block: ScriptBlock):
    """Return every active top-level ``key = value`` row in one block."""

    rows = []
    nested_depth = 0
    for index in range(block.start_line + 1, block.end_line):
        code = _active_code(lines[index])
        token = code.split(None, 1)[0].lower() if code else ""
        if _opens_nested_scope(token):
            nested_depth += 1
            continue
        if token == "end" and nested_depth:
            nested_depth -= 1
            continue
        if nested_depth or not code:
            continue
        match = _GENERIC_PARAM_RE.match(lines[index])
        if match is None:
            continue
        rows.append((
            index,
            match.group("key").lower(),
            match.group("value").strip().strip('"').split(maxsplit=1)[0]
            if match.group("value").strip().strip('"') else "",
            match.group("indent"),
        ))
    return rows


def script_model_references(text: str) -> list[VehicleModelReference]:
    """Read visual-prototype references from new vehicles and weapons.

    The final active assignment wins, matching Urban Assault script override
    behavior. ``modify_vehicle`` and ``modify_weapon`` are deliberately
    excluded because resolving inherited visual prototypes would require the
    full include chain rather than one source file. ``vp_wait`` is retained so
    Cockpit View can preview the same stationary model used by the runtime.
    OpenNeoUA's ``scale_xyz`` drives the preview scale and ``rotation_xyz``
    the preview rotation.
    """

    lines = text.splitlines()
    references: list[VehicleModelReference] = []
    for block in find_script_blocks(text):
        if block.kind not in ("new_vehicle", "new_weapon") \
                or not block.complete:
            continue
        values = {
            "vp_normal": None,
            "vp_wait": None,
        }
        tuples: dict[str, tuple[float, ...] | None] = {
            "scale_xyz": None,
            "rotation_xyz": None,
        }
        visual_paths = {
            "base_normal": "",
            "3ds_normal": "",
            "collision_shape": "",
        }
        for _line, key, raw, _indent in _generic_parameter_rows(lines, block):
            if key in visual_paths:
                visual_paths[key] = raw
            elif key in tuples:
                try:
                    tuples[key] = _float_tuple(raw, 3, key)
                except CollisionScriptError as exc:
                    raise CollisionScriptError(
                        f"Invalid {key} in {block.kind} "
                        f"{block.object_id}: {raw}") from exc
            elif key in values:
                try:
                    values[key] = float(raw)
                except ValueError as exc:
                    raise CollisionScriptError(
                        f"Non-numeric {key} in {block.kind} "
                        f"{block.object_id}: {raw}") from exc
        raw_vp = values["vp_normal"]
        if (raw_vp is None and not visual_paths["base_normal"]
                and not visual_paths["3ds_normal"]):
            continue
        if raw_vp is not None and not math.isfinite(raw_vp):
            raise CollisionScriptError(
                f"Non-finite vp_normal in {block.kind} "
                f"{block.object_id}: {raw_vp}")
        vp_id = int(raw_vp) if raw_vp is not None else None
        if (raw_vp is not None
                and (abs(raw_vp - vp_id) > 1e-9 or vp_id < 0)):
            raise CollisionScriptError(
                f"Invalid vp_normal in {block.kind} "
                f"{block.object_id}: {raw_vp}")
        raw_wait = values["vp_wait"]
        wait_vp = None
        if raw_wait is not None:
            if not math.isfinite(raw_wait):
                raise CollisionScriptError(
                    f"Non-finite vp_wait in {block.kind} "
                    f"{block.object_id}: {raw_wait}")
            wait_vp = int(raw_wait)
            if abs(raw_wait - wait_vp) > 1e-9 or wait_vp < 0:
                raise CollisionScriptError(
                    f"Invalid vp_wait in {block.kind} "
                    f"{block.object_id}: {raw_wait}")
        scales = tuples["scale_xyz"] or (1.0, 1.0, 1.0)
        if not all(math.isfinite(value) and value >= 0.0 for value in scales):
            raise CollisionScriptError(
                f"Invalid scale_xyz in {block.kind} "
                f"{block.object_id}: {scales}")
        rotations = tuples["rotation_xyz"] or (0.0, 0.0, 0.0)
        shape_path = visual_paths["collision_shape"].strip()
        if shape_path.casefold() == "0":
            shape_path = ""
        references.append(VehicleModelReference(
            block=block,
            vp_normal=vp_id,
            vp_wait=wait_vp,
            scale_x=scales[0],
            scale_y=scales[1],
            scale_z=scales[2],
            rotation_x=rotations[0],
            rotation_y=rotations[1],
            rotation_z=rotations[2],
            base_normal=visual_paths["base_normal"],
            three_ds_normal=visual_paths["3ds_normal"],
            collision_shape_path=shape_path,
        ))
    return references


def vehicle_model_references(text: str) -> list[VehicleModelReference]:
    """Compatibility helper returning only complete ``new_vehicle`` rows."""

    return [
        reference for reference in script_model_references(text)
        if reference.block.kind == "new_vehicle"
    ]


def weapon_model_references(text: str) -> list[VehicleModelReference]:
    """Return complete ``new_weapon`` rows with resolvable visual models."""

    return [
        reference for reference in script_model_references(text)
        if reference.block.kind == "new_weapon"
    ]


def runtime_vp_table(
        set_bas_path: str | Path,
        embedded: EmbeddedVPSet,
) -> tuple[VPTable, str, list[str]]:
    """Resolve the same VISPROTO.LST precedence used by OpenNeoUA.

    ``Loose/VISPROTO.LST`` is the editable runtime override. If it is absent
    or invalid, the normal ``Scripts/VISPROTO.LST`` is used. The embedded
    binary visproto.base is intentionally not selected as a runtime VP table.
    """

    del embedded  # Kept in the signature for callers that already reconstructed it.
    path = Path(set_bas_path)
    set_root = path.parent.parent if path.parent.name.casefold() in (
        "objects", "object") else path.parent
    loose = set_root / "Loose" / "VISPROTO.LST"
    scripts = set_root / "Scripts" / "VISPROTO.LST"
    warnings: list[str] = []
    if loose.is_file():
        try:
            return (
                load_visproto(loose, require_terminator=True),
                "Loose VISPROTO.LST",
                warnings,
            )
        except (OSError, VPParseError) as exc:
            warnings.append(
                f"Invalid Loose VISPROTO.LST ignored: {exc}")
    if scripts.is_file():
        try:
            return (
                load_visproto(scripts, require_terminator=False),
                "Scripts/VISPROTO.LST",
                warnings,
            )
        except (OSError, VPParseError) as exc:
            warnings.append(
                f"Invalid Scripts/VISPROTO.LST ignored: {exc}")
    raise CollisionScriptError(
        "No usable VISPROTO.LST was found beside the selected SET.BAS.")


def import_collision_block(
        text: str, block: ScriptBlock, compound_category: str,
) -> tuple[CollisionSphere | None, list[CollisionSphere], list[str]]:
    if not block.complete:
        raise CollisionScriptError(
            f"Parsing incompleto: manca end per {block.kind} {block.object_id}.")
    if compound_category not in COMPOUND_TYPES:
        raise CollisionScriptError("Categoria compound non valida.")

    lines = text.splitlines()
    rows = _parameter_rows(lines, block)
    legacy = None
    expected = None
    current = None
    compounds: list[CollisionSphere] = []
    warnings: list[str] = []

    collision_keys = {
        "radius", "coll_num", "coll_act", "coll_xyz", "coll_radius",
    }
    for _line, key, raw, _indent in rows:
        if key not in collision_keys:
            continue
        if key == "coll_xyz":
            if current is None:
                raise CollisionScriptError(
                    f"{key} compare prima del primo coll_act.")
            current.x, current.y, current.z = _float_tuple(raw, 3, key)
            continue
        try:
            value = float(raw)
        except ValueError as exc:
            raise CollisionScriptError(
                f"Valore non numerico per {key}: {raw}") from exc
        if key == "radius":
            legacy = CollisionSphere(LEGACY, radius=value)
        elif key == "coll_num":
            expected = int(value)
        elif key == "coll_act":
            current = CollisionSphere(compound_category)
            compounds.append(current)
            if int(value) != len(compounds) - 1:
                warnings.append(
                    f"coll_act {int(value)} rinumerato a {len(compounds) - 1}.")
        elif key.startswith("coll_"):
            if current is None:
                raise CollisionScriptError(
                    f"{key} compare prima del primo coll_act.")
            if key == "coll_radius":
                current.radius = value

    if expected is not None and expected != len(compounds):
        warnings.append(
            f"coll_num={expected}, ma sono stati letti "
            f"{len(compounds)} blocchi coll_act.")
    return legacy, compounds, warnings


def import_collision_shape_reference(
        text: str, block: ScriptBlock) -> str:
    """Read the final active collision_shape path from one script block."""

    value = ""
    for _line, key, raw, _indent in _generic_parameter_rows(
            text.splitlines(), block):
        if key == "collision_shape":
            value = raw.strip()
    return "" if value.casefold() in {"", "0"} else value


def _load_3ds_family(path: str | Path) -> AssetFamily:
    """Wrap existing 3DS meshes as a geometry-only AssetFamily."""

    source = Path(path)
    meshes, _materials = read_3ds(source)
    family = AssetFamily(
        base_path=source,
        search_root=str(source.parent),
        search_roots=[str(source.parent)],
    )
    root = FamilyObject(
        base_object=BaseObject(name=source.stem), owner_path="root")
    for index, mesh in enumerate(meshes):
        name = f"{source.stem}_{index + 1}"
        skeleton = SkltModel(
            source_name=f"{source.name}:{index + 1}",
            points=[tuple(point) for point in mesh.vertices],
            polygons=[list(face) for face in mesh.faces],
            parsed_polygon_count=len(mesh.faces),
        )
        child = FamilyObject(
            base_object=BaseObject(name=name, skeleton_name=name),
            skeleton=skeleton,
            owner_path=f"root/kid[{index}]",
        )
        root.kids.append(child)
    family.root_object = root
    family.warnings.append(
        "3DS geometry loaded through the shared AssetViewport; textures and "
        "per-object animation are not represented in this geometry-only view.")
    return family


def _resolve_script_visual_source(
        reference: VehicleModelReference, script_path: str | Path,
        chosen_set: str | Path | None, current_set: str | Path | None,
        family_loader=None):
    """Resolve runtime visual priority: 3DS, BASE, then SET VP.

    A failed higher-priority override is returned as a warning while the next
    source is tried. Asset probing stays read-only and never opens a modal UI.
    """

    loader = family_loader or _public_dependency(
        "load_asset_family", load_asset_family)
    failures = []
    candidates = (
        ("3ds_normal", reference.three_ds_normal),
        ("base_normal", reference.base_normal),
    )
    for label, value in candidates:
        if not value or value.strip().casefold() in {"", "0"}:
            continue
        path = _resolve_data_asset_path(
            value, script_path, chosen_set, current_set)
        if path is None:
            failures.append(
                f"{label} ignored because {value} could not be resolved")
            continue
        try:
            family = (_load_3ds_family(path)
                      if path.suffix.casefold() == ".3ds"
                      else loader(path))
            if not any(
                    obj.skeleton is not None
                    and getattr(obj.skeleton, "polygons", None)
                    for obj in family.all_objects()):
                raise ValueError("asset has no polygon geometry")
        except Exception as exc:
            failures.append(f"{label} ignored: {exc}")
            continue
        return label, path, value, tuple(failures)
    if reference.vp_normal is not None:
        return "vp_normal", None, "", tuple(failures)
    return "unavailable", None, "", tuple(failures)


def _reference_for_visual_source(
        reference: VehicleModelReference,
        visual_source: str) -> VehicleModelReference:
    """Keep script data intact while describing the visual that loaded."""

    if visual_source == "base_normal" and reference.three_ds_normal:
        return replace(reference, three_ds_normal="")
    if (visual_source == "vp_normal"
            and (reference.three_ds_normal or reference.base_normal)):
        return replace(reference, three_ds_normal="", base_normal="")
    return reference


def import_overeof_block(
        text: str, block: ScriptBlock,
) -> tuple[bool, float]:
    """Read the active top-level vehicle ``overeof`` without changing the
    established :func:`import_collision_block` return contract.

    Commented lines are intentionally ignored by ``_parameter_rows`` exactly
    as they are by Urban Assault.  If more than one active assignment exists,
    the final one wins, matching normal script override behavior.
    """

    if not block.complete:
        raise CollisionScriptError(
            f"Parsing incompleto: manca end per {block.kind} "
            f"{block.object_id}.")
    enabled = False
    value = 0.0
    for _line, key, raw, _indent in _parameter_rows(
            text.splitlines(), block):
        if key != "overeof":
            continue
        try:
            value = float(raw)
        except ValueError as exc:
            raise CollisionScriptError(
                f"Valore non numerico per overeof: {raw}") from exc
        enabled = True
    return enabled, value


def import_fire_points_block(
        text: str, block: ScriptBlock,
) -> tuple[bool, float, float, float, int, int]:
    """Read active top-level vanilla vehicle fire-point parameters.

    ``num_weapons`` is a projectile-count parameter used independently by the
    runtime, so its presence alone must not invent an authored muzzle at
    0 / 0 / 0 in the editor.  The Fire Point workspace becomes active only
    when at least one explicit ``fire_x``, ``fire_y`` or ``fire_z`` assignment
    exists; ``num_weapons`` is still read so an existing rack keeps its count.
    """

    if not block.complete:
        raise CollisionScriptError(
            f"Parsing incompleto: manca end per {block.kind} "
            f"{block.object_id}.")
    values = {
        "fire_x": 0.0,
        "fire_y": 0.0,
        "fire_z": 0.0,
    }
    num_weapons_min = 1
    num_weapons_max = 1
    enabled = False
    for _line, key, raw, _indent in _parameter_rows(
            text.splitlines(), block):
        if key == "num_weapons":
            try:
                num_weapons_min, num_weapons_max = (
                    _parse_num_weapons_range(raw))
            except ValueError as exc:
                raise CollisionScriptError(
                    f"Valore non valido per num_weapons: {raw}. "
                    "Usa 0, un valore 1-255 o un range min_max 1-255."
                ) from exc
            continue
        if key not in values:
            continue
        try:
            values[key] = float(raw)
        except ValueError as exc:
            raise CollisionScriptError(
                f"Valore non numerico per {key}: {raw}") from exc
        if key in {"fire_x", "fire_y", "fire_z"}:
            enabled = True
    return (
        enabled,
        values["fire_x"], values["fire_y"], values["fire_z"],
        num_weapons_min, num_weapons_max,
    )


def import_briefing_size_block(text: str, block: ScriptBlock) -> tuple[bool, float]:
    value = 0.0
    for _line, key, raw, _indent in _parameter_rows(text.splitlines(), block):
        if key == "briefing_size":
            try:
                parsed = float(raw)
                value = parsed if math.isfinite(parsed) and 1 <= parsed <= 10000 else 0.0
            except ValueError:
                value = 0.0
    return value > 0, value


def briefing_size_data_lines(project: CollisionProject) -> list[str]:
    if project.target_category != VEHICLE or not project.briefing_size_enabled:
        return []
    return [f"briefing_size = {int(round(project.briefing_size))}"]


def import_cockpit_camera_block(
        text: str, block: ScriptBlock,
) -> tuple[bool, float, float, float]:
    """Read OpenNeoUA's vehicle-local modern cockpit camera offset.

    Missing axes stay zero, matching the runtime prototype defaults. The
    feature is vehicle-only and intentionally has no rotation parameters: the
    game keeps the controlled vehicle's real orientation.
    """

    if not block.complete:
        raise CollisionScriptError(
            f"Parsing incompleto: manca end per {block.kind} "
            f"{block.object_id}.")
    offset = (0.0, 0.0, 0.0)
    enabled = False
    for _line, key, raw, _indent in _parameter_rows(
            text.splitlines(), block):
        if key != "cockpit_camera_offset_xyz":
            continue
        offset = _float_tuple(raw, 3, key)
        enabled = True
    return (enabled, *offset)


_GUN_POINT_KEYS = {
    "robo_num_guns", "robo_act_gun",
    "robo_gun_pos_x", "robo_gun_pos_y", "robo_gun_pos_z",
    "robo_gun_dir_x", "robo_gun_dir_y", "robo_gun_dir_z",
    "robo_gun_type", "robo_gun_name",
    "unit_num_guns", "unit_act_gun",
    "unit_gun_pos_xyz", "unit_gun_dir_xyz",
    "unit_gun_type", "unit_gun_name", "unit_gun_icon",
}


_SCRIPT_TAB_KEYS = {
    _SCRIPT_TAB_BRIEFING: {"briefing_size"},
    _SCRIPT_TAB_COLLISION: {
        "radius", "overeof", "coll_num", "coll_act", "collision_shape",
        "coll_xyz", "coll_radius",
    },
    _SCRIPT_TAB_FIRE: {"fire_x", "fire_y", "fire_z", "num_weapons"},
    _SCRIPT_TAB_GUN: set(_GUN_POINT_KEYS),
    _SCRIPT_TAB_COCKPIT: {"cockpit_camera_offset_xyz"},
}


_TURRET_LIMIT_KEY_SET = {
    "gun_side_angle", "gun_up_angle", "gun_down_angle",
}


@dataclass(frozen=True)
class _ManagedScriptFamily:
    """One logical group that must be replaced as a unit in a script block."""

    name: str
    keys: frozenset[str]
    old_disabled_labels: frozenset[str] = frozenset()


_MANAGED_SCRIPT_FAMILIES = (
    _ManagedScriptFamily(
        "legacy radius", frozenset({"radius"}),
        frozenset({"legacy radius"})),
    _ManagedScriptFamily(
        "ground alignment", frozenset({"overeof"}),
        frozenset({"overeof", "ground alignment"})),
    _ManagedScriptFamily(
        "OpenNeoUA collision",
        frozenset({
            "coll_num", "coll_act", "coll_xyz",
            "coll_radius", "collision_shape",
        }),
        frozenset({"compound collisions", "openneoua collision"})),
    _ManagedScriptFamily(
        "fire points",
        frozenset({"fire_x", "fire_y", "fire_z", "num_weapons"}),
        frozenset({"fire points"})),
    _ManagedScriptFamily(
        "gun points", frozenset(_GUN_POINT_KEYS),
        frozenset({"gun points"})),
    _ManagedScriptFamily(
        "cockpit view",
        frozenset({"cockpit_camera_offset_xyz"}),
        frozenset({"cockpit camera offset", "cockpit view"})),
    _ManagedScriptFamily("briefing size", frozenset({"briefing_size"})),
    _ManagedScriptFamily(
        "turret limits", frozenset(_TURRET_LIMIT_KEY_SET),
        frozenset({"turret limits"})),
)


_OLD_DISABLED_HEADER_RE = re.compile(
    r"^\s*;\s*(?:Entity Designer|Collision Editor) disabled:\s*(?P<label>.+?)\s*$",
    re.IGNORECASE,
)


_COMMENTED_ASSIGNMENT_RE = re.compile(
    r"^\s*;\s*(?P<key>[A-Za-z0-9_][A-Za-z0-9_]*)\s*=",
    re.IGNORECASE,
)


def _top_level_assignment_rows(text: str, block: ScriptBlock):
    """Yield active top-level assignments with their complete RHS text.

    ``_parameter_rows`` intentionally tokenizes numeric values for the
    collision parser. Gun names/icons are strings, so this companion retains
    the whole value while using the same nested-scope rules.
    """

    lines = text.splitlines()
    nested_depth = 0
    for index in range(block.start_line + 1, block.end_line):
        code = _active_code(lines[index])
        token = code.split(None, 1)[0].lower() if code else ""
        if _opens_nested_scope(token):
            nested_depth += 1
            continue
        if token == "end" and nested_depth:
            nested_depth -= 1
            continue
        if nested_depth or not code:
            continue
        match = _GENERIC_PARAM_RE.match(lines[index])
        if match is None:
            continue
        yield (
            index,
            match.group("key").lower(),
            match.group("value").strip(),
            match.group("indent"),
        )


def gun_point_families_in_block(
        text: str, block: ScriptBlock,
) -> set[str]:
    """Return the script gun families explicitly present in one block."""

    families: set[str] = set()
    if not block.complete:
        return families
    for _line, key, _raw, _indent in _top_level_assignment_rows(text, block):
        if key not in _GUN_POINT_KEYS:
            continue
        if key.startswith("robo_"):
            families.add("robo")
        elif key.startswith("unit_"):
            families.add("unit")
    return families


def import_gun_points_block(
        text: str, block: ScriptBlock,
) -> tuple[bool, list[GunPoint], str]:
    """Read Host Station and generic OpenNeoUA vehicle gun mounts.

    The engine uses the same ``TRoboGun`` semantic payload for both families.
    Keep the originating family on every point so editing an existing Host
    Station does not silently rewrite ``robo_*`` data as ``unit_*`` data.
    """

    if not block.complete:
        raise CollisionScriptError(
            f"Parsing incompleto: manca end per {block.kind} "
            f"{block.object_id}.")

    families: dict[str, list[GunPoint]] = {"robo": [], "unit": []}
    active = {"robo": -1, "unit": -1}
    enabled = False
    unit_default_icon = ""

    def resize(scheme: str, count: int) -> None:
        count = max(0, min(20, int(count)))
        points = families[scheme]
        if len(points) < count:
            points.extend(GunPoint(scheme=scheme)
                          for _ in range(count - len(points)))
        elif len(points) > count:
            del points[count:]
        if active[scheme] >= count:
            active[scheme] = count - 1

    def point_for(scheme: str) -> GunPoint | None:
        index = active[scheme]
        if not (0 <= index < 20):
            return None
        points = families[scheme]
        if index >= len(points):
            points.extend(GunPoint(scheme=scheme)
                          for _ in range(index + 1 - len(points)))
        return points[index]

    for _line, key, raw, _indent in _top_level_assignment_rows(text, block):
        if key not in _GUN_POINT_KEYS:
            continue
        enabled = True
        scheme = "robo" if key.startswith("robo_") else "unit"
        prefix = f"{scheme}_"
        leaf = key[len(prefix):]

        if leaf == "num_guns":
            try:
                resize(scheme, int(float(raw.split()[0])))
            except (TypeError, ValueError) as exc:
                raise CollisionScriptError(
                    f"Valore non numerico per {key}: {raw}") from exc
            continue
        if leaf == "act_gun":
            try:
                index = int(float(raw.split()[0]))
            except (TypeError, ValueError) as exc:
                raise CollisionScriptError(
                    f"Valore non numerico per {key}: {raw}") from exc
            active[scheme] = max(0, min(19, index))
            point_for(scheme)
            continue

        point = point_for(scheme)
        if leaf == "gun_icon" and point is None:
            unit_default_icon = raw.strip().strip('"')
            continue
        if point is None:
            # Ignore malformed per-slot properties before a valid act_gun.
            # Generic unit_* data is a runtime no-op in this state; avoiding
            # legacy robo_* index -1 also keeps the editor defensive.
            continue
        if leaf in {"gun_pos_xyz", "gun_dir_xyz"}:
            # OpenNeoUA unit_* mounts use the compact x_y_z form.
            values = _float_tuple(raw, 3, key)
            if leaf == "gun_pos_xyz":
                point.x, point.y, point.z = values
            else:
                point.dir_x, point.dir_y, point.dir_z = values
        elif leaf in {
                "gun_pos_x", "gun_pos_y", "gun_pos_z",
                "gun_dir_x", "gun_dir_y", "gun_dir_z"}:
            try:
                value = float(raw.split()[0])
            except (TypeError, ValueError) as exc:
                raise CollisionScriptError(
                    f"Valore non numerico per {key}: {raw}") from exc
            attr = leaf.removeprefix("gun_").replace("pos_", "")
            if leaf.startswith("gun_dir_"):
                attr = "dir_" + leaf[-1]
            setattr(point, attr, value)
        elif leaf == "gun_type":
            try:
                point.gun_type = max(0, min(255, int(float(raw.split()[0]))))
            except (TypeError, ValueError) as exc:
                raise CollisionScriptError(
                    f"Valore non numerico per {key}: {raw}") from exc
        elif leaf == "gun_name":
            point.name = raw.strip().strip('"')
        elif leaf == "gun_icon" and scheme == "unit":
            point.icon = raw.strip().strip('"')

    return enabled, families["robo"] + families["unit"], unit_default_icon


_TURRET_LIMIT_KEYS = {
    "gun_side_angle": "side",
    "gun_up_angle": "up",
    "gun_down_angle": "down",
}


def _script_integer(raw: str, key: str) -> int:
    token = raw.strip().split()[0]
    try:
        value = int(token, 0)
    except ValueError:
        try:
            value = int(token, 10)
        except ValueError as exc:
            raise CollisionScriptError(
                f"Valore intero non valido per {key}: {raw}") from exc
    if not -(2 ** 31) <= value <= 2 ** 31 - 1:
        raise CollisionScriptError(
            f"Valore fuori dall'intervallo int32 per {key}: {raw}")
    return value


def import_turret_limits(text: str) -> dict[int, TurretLimits]:
    """Resolve gun-angle values by vehicle ID using script override order.

    The last active assignment wins exactly as it does while OpenNeoUA parses
    ``new_vehicle``/``modify_vehicle`` blocks. The write target is the last
    block that actually authored an angle, or the vehicle's ``new_vehicle``
    block when the three parameters are currently absent.
    """

    lines_by_vehicle: dict[int, list[ScriptBlock]] = {}
    for block in find_script_blocks(text):
        if block.complete and block.kind in ("new_vehicle", "modify_vehicle"):
            lines_by_vehicle.setdefault(block.object_id, []).append(block)

    result: dict[int, TurretLimits] = {}
    for vehicle_id, blocks in lines_by_vehicle.items():
        limits = TurretLimits(vehicle_id=vehicle_id)
        target = next(
            (block for block in blocks if block.kind == "new_vehicle"),
            blocks[0],
        )
        for block in blocks:
            if block.name:
                limits.source_name = block.name
            block_has_limit = False
            for _line, key, raw, _indent in _top_level_assignment_rows(
                    text, block):
                field_name = _TURRET_LIMIT_KEYS.get(key)
                if field_name is None:
                    continue
                value = _script_integer(raw, key)
                if value < 0:
                    raise CollisionScriptError(
                        f"Valore negativo non valido per {key}: {raw}")
                # Keep the editor inside its authored limits: Side may use
                # the full 0..3200 range, while Up/Down are capped at 1600.
                setattr(limits, field_name, _clamp_gun_angle(field_name, value))
                limits.enabled = True
                block_has_limit = True
            if block_has_limit:
                target = block
        limits.source_kind = target.kind
        result[vehicle_id] = limits
    return result


def turret_limit_data_lines(limits: TurretLimits) -> list[str]:
    if not limits.enabled:
        return []
    return [
        f"gun_side_angle = {_clamp_gun_angle('side', limits.side)}",
        f"gun_up_angle = {_clamp_gun_angle('up', limits.up)}",
        f"gun_down_angle = {_clamp_gun_angle('down', limits.down)}",
    ]


def gun_angle_degrees(value: int) -> float:
    """Convert OpenNeoUA's radians-times-1000 script unit to degrees."""

    return math.degrees(float(value) / GUN_ANGLE_SCALE)


def gun_direction_index(dir_x: float, dir_z: float) -> int | None:
    """Return the nearest ordered 45-degree XZ direction."""

    x = float(dir_x)
    z = float(dir_z)
    if not (math.isfinite(x) and math.isfinite(z)) or math.hypot(x, z) <= 1e-9:
        return None
    angle = math.atan2(x, z) % (2.0 * math.pi)
    return int(math.floor(angle / (math.pi / 4.0) + 0.5)) % 8


def gun_point_data_lines(project: CollisionProject) -> list[str]:
    """Render gun mounts without changing their script family."""

    lines: list[str] = []
    for scheme in ("robo", "unit"):
        points = [point for point in project.gun_points
                  if point.scheme == scheme]
        default_icon = (
            project.unit_gun_default_icon if scheme == "unit" else "")
        if not points and not default_icon:
            continue
        if lines:
            lines.append("")
        lines.append(f"{scheme}_num_guns = {len(points)}")
        if scheme == "unit" and default_icon:
            lines.append(f"unit_gun_icon = {default_icon}")
        for index, point in enumerate(points):
            lines.extend(["", f"{scheme}_act_gun = {index}"])
            if scheme == "unit":
                lines.extend([
                    f"unit_gun_pos_xyz = "
                    f"{_tuple_text(point.x, point.y, point.z)}",
                    f"unit_gun_dir_xyz = "
                    f"{_tuple_text(point.dir_x, point.dir_y, point.dir_z)}",
                ])
            else:
                # Host Station mounts keep the original separate robo_* keys.
                lines.extend([
                    f"{scheme}_gun_pos_x = {_number(point.x)}",
                    f"{scheme}_gun_pos_y = {_number(point.y)}",
                    f"{scheme}_gun_pos_z = {_number(point.z)}",
                    f"{scheme}_gun_dir_x = {_number(point.dir_x)}",
                    f"{scheme}_gun_dir_y = {_number(point.dir_y)}",
                    f"{scheme}_gun_dir_z = {_number(point.dir_z)}",
                ])
            lines.append(
                f"{scheme}_gun_type = {max(0, min(255, int(point.gun_type)))}")
            if point.name:
                lines.append(f"{scheme}_gun_name = {point.name}")
            if scheme == "unit" and point.icon:
                lines.append(f"unit_gun_icon = {point.icon}")
    return lines


def cockpit_camera_data_lines(project: CollisionProject) -> list[str]:
    """Render only the OpenNeoUA modern cockpit position parameters."""

    if project.target_category != VEHICLE or not project.cockpit_camera_enabled:
        return []
    return [
        "cockpit_camera_offset_xyz = " + _tuple_text(
            project.cockpit_camera_offset_x,
            project.cockpit_camera_offset_y,
            project.cockpit_camera_offset_z),
    ]


def collision_data_lines(project: CollisionProject) -> list[str]:
    lines: list[str] = []
    if project.legacy is not None:
        lines.append(f"radius = {_radius_number(project.legacy.radius)}")
    if project.target_category == VEHICLE and project.overeof_enabled:
        lines.append(f"overeof = {_number(project.overeof)}")
    lines.extend(briefing_size_data_lines(project))
    cockpit_lines = cockpit_camera_data_lines(project)
    if cockpit_lines:
        if lines:
            lines.append("")
        lines.extend(cockpit_lines)
    if project.target_category == VEHICLE and project.fire_points_enabled:
        if lines:
            lines.append("")
        lines.extend([
            f"fire_x = {_number(project.fire_x)}",
            f"fire_y = {_number(project.fire_y)}",
            f"fire_z = {_number(project.fire_z)}",
            f"num_weapons = {_num_weapons_text(project)}",
        ])
    if project.target_category == VEHICLE and project.gun_points_enabled:
        gun_lines = gun_point_data_lines(project)
        if gun_lines:
            if lines:
                lines.append("")
            lines.extend(gun_lines)
    if project.compound:
        if lines:
            lines.append("")
        lines.append(f"coll_num = {len(project.compound)}")
        for index, sphere in enumerate(project.compound):
            lines.extend([
                "",
                f"coll_act = {index}",
                f"coll_xyz = {_tuple_text(sphere.x, sphere.y, sphere.z)}",
                f"coll_radius = {_number(sphere.radius)}",
            ])
    return lines


def export_collision_text(project: CollisionProject) -> str:
    header = [
        f"; Entity Designer project: {project.name}",
        f"; Source model: {project.source_model or '<none>'}",
        f"; Source BASE: {project.source_base or '<none>'}",
        f"; Target category: {project.target_category}",
        "",
    ]
    data = collision_data_lines(project)
    referenced: list[str] = []
    dirty_limits = sorted(
        (limits for limits in project.turret_limits.values() if limits.dirty),
        key=lambda limits: limits.vehicle_id,
    )
    if dirty_limits:
        referenced.extend(["", "; Referenced gun vehicle turret limits"])
        for limits in dirty_limits:
            name = f" - {limits.source_name}" if limits.source_name else ""
            referenced.append(
                f"; Vehicle {limits.vehicle_id}{name}")
            if limits.enabled:
                referenced.append(f"modify_vehicle {limits.vehicle_id}")
                referenced.extend(
                    "    " + line for line in turret_limit_data_lines(limits))
                referenced.append("end")
            else:
                referenced.append(
                    "; Disable/remove gun_side_angle, gun_up_angle and "
                    "gun_down_angle in this vehicle definition.")
    return "\n".join(header + data + referenced).rstrip() + "\n"


def _line_ending(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _apply_turret_limit_updates(
        text: str, project: CollisionProject, *, comment_missing: bool) -> str:
    """Patch only edited referenced-gun definitions in the selected script."""

    updated = text
    dirty_limits = sorted(
        (limits for limits in project.turret_limits.values() if limits.dirty),
        key=lambda limits: limits.vehicle_id,
    )
    for limits in dirty_limits:
        matches = [
            block for block in find_script_blocks(updated)
            if block.kind == limits.source_kind
            and block.object_id == limits.vehicle_id
        ]
        if not matches:
            raise CollisionScriptError(
                "Impossibile aggiornare i limiti torretta: "
                f"{limits.source_kind} {limits.vehicle_id} non trovato.")
        if len(matches) == 1:
            block = matches[0]
        elif limits.source_kind == "modify_vehicle":
            # Multiple modify_vehicle blocks for the same ID are legal in
            # override-style scripts. Patch the final matching block that
            # actually authors at least one gun angle, exactly mirroring the
            # import override order.
            authored = [
                candidate for candidate in matches
                if any(
                    key in _TURRET_LIMIT_KEYS
                    for _line, key, _raw, _indent
                    in _top_level_assignment_rows(updated, candidate)
                )
            ]
            block = authored[-1] if authored else matches[-1]
        else:
            raise CollisionScriptError(
                "Impossibile aggiornare i limiti torretta: definizione "
                f"ambigua per {limits.source_kind} {limits.vehicle_id} "
                f"({len(matches)} blocchi).")
        if not block.complete:
            raise CollisionScriptError(
                f"Parsing incompleto: manca end per {block.kind} "
                f"{block.object_id}.")

        updated = _patch_top_level_keys(
            updated, block, _TURRET_LIMIT_KEY_SET,
            turret_limit_data_lines(limits),
            comment_missing=comment_missing, turret=True)
    return updated


def _collision_tab_data_lines(project: CollisionProject) -> list[str]:
    """Render only parameters owned by the Collision tab."""

    lines: list[str] = []
    if len(project.compound) > UNIT_COLL_MAX_COUNT:
        raise CollisionScriptError(
            f"OpenNeoUA collision spheres exceed the runtime limit "
            f"of {UNIT_COLL_MAX_COUNT}; imported data was kept unchanged.")
    if project.legacy is not None:
        lines.append(f"radius = {_radius_number(project.legacy.radius)}")
    if project.target_category == VEHICLE and project.overeof_enabled:
        lines.append(f"overeof = {_number(project.overeof)}")
    if project.compound:
        if lines:
            lines.append("")
        lines.append(f"coll_num = {len(project.compound)}")
        for index, sphere in enumerate(project.compound):
            lines.extend([
                "",
                f"coll_act = {index}",
                f"coll_xyz = {_tuple_text(sphere.x, sphere.y, sphere.z)}",
                f"coll_radius = {_number(sphere.radius)}",
            ])
    if project.collision_shape_path:
        path = project.collision_shape_path.replace("\\", "/").strip()
        if (not path.casefold().startswith("data/")
                or not path.casefold().endswith(".collision")
                or ".." in path.split("/")
                or any(character.isspace() for character in path)):
            raise CollisionScriptError(
                "Collision Shape path must be a space-free Data/... .collision path.")
        if lines:
            lines.append("")
        lines.append(f"collision_shape = {path}")
    return lines


def _fire_tab_data_lines(project: CollisionProject) -> list[str]:
    """Render only parameters owned by the Fire Points tab."""

    if project.target_category != VEHICLE or not project.fire_points_enabled:
        return []
    return [
        f"fire_x = {_number(project.fire_x)}",
        f"fire_y = {_number(project.fire_y)}",
        f"fire_z = {_number(project.fire_z)}",
        f"num_weapons = {_num_weapons_text(project)}",
    ]


def _script_tab_data_lines(
        project: CollisionProject, group: str) -> list[str]:
    """Return the complete script payload for one Entity Designer tab."""

    if group == _SCRIPT_TAB_COLLISION:
        return _collision_tab_data_lines(project)
    if group == _SCRIPT_TAB_FIRE:
        return _fire_tab_data_lines(project)
    if group == _SCRIPT_TAB_GUN:
        if project.target_category != VEHICLE or not project.gun_points_enabled:
            return []
        return gun_point_data_lines(project)
    if group == _SCRIPT_TAB_COCKPIT:
        return cockpit_camera_data_lines(project)
    if group == _SCRIPT_TAB_BRIEFING:
        return briefing_size_data_lines(project)
    raise CollisionScriptError(f"Gruppo script sconosciuto: {group}")


def build_editable_overwrite_preview(
        kind: str, object_id: int, project: CollisionProject,
        changed_groups: set[str] | frozenset[str]) -> str:
    """Build the editable patch shown by Overwrite Loaded Definition.

    Only tabs that differ from the last loaded script state are included. Each
    tab marker owns the code until the next marker. Removing a whole marked
    section therefore means "do not apply this tab" without touching the
    source data already present in the script.
    """

    groups = [group for group in _SCRIPT_TAB_ORDER if group in changed_groups]
    if project.target_category != VEHICLE:
        groups = [group for group in groups if group == _SCRIPT_TAB_COLLISION]
    if not groups:
        return ""

    lines = [f"{kind} {int(object_id)}"]
    for group in groups:
        if len(lines) > 1 and lines[-1] != "":
            lines.append("")
        lines.append(f"    ; [Tab: {_SCRIPT_TAB_LABELS[group]}]")
        data = _script_tab_data_lines(project, group)
        lines.extend("    " + line if line else "" for line in data)
    lines.append("end")

    if _SCRIPT_TAB_GUN in groups:
        dirty_limits = sorted(
            (limits for limits in project.turret_limits.values()
             if limits.dirty),
            key=lambda limits: limits.vehicle_id,
        )
        for limits in dirty_limits:
            lines.extend(["", f"{limits.source_kind} {limits.vehicle_id}"])
            lines.extend(
                "    " + line for line in turret_limit_data_lines(limits))
            lines.append("end")
    return "\n".join(lines).rstrip() + "\n"


def _script_tab_from_marker(line: str) -> str | None:
    match = _SCRIPT_TAB_MARKER_RE.match(line)
    if match is None:
        return None
    label = match.group(1).casefold()
    for group, candidate in _SCRIPT_TAB_LABELS.items():
        if candidate.casefold() == label:
            return group
    return None


def _validate_editable_tab_lines(
        group: str, lines: list[str], kind: str, object_id: int) -> None:
    """Reject unsupported code before an editable overwrite is applied."""

    allowed = _SCRIPT_TAB_KEYS[group]
    for line in lines:
        code = _active_code(line)
        if not code:
            continue
        match = _GENERIC_PARAM_RE.match(line)
        if match is None:
            raise CollisionScriptError(
                f"La sezione {_SCRIPT_TAB_LABELS[group]} contiene codice "
                f"non supportato: {code}")
        key = match.group("key").lower()
        if key not in allowed:
            raise CollisionScriptError(
                f"{key} non appartiene alla tab "
                f"{_SCRIPT_TAB_LABELS[group]}.")

    synthetic = [f"{kind} {int(object_id)}"]
    synthetic.extend(
        "    " + line.lstrip() if line.strip() else "" for line in lines)
    synthetic.append("end")
    text = "\n".join(synthetic) + "\n"
    block = find_script_blocks(text)[0]
    if group == _SCRIPT_TAB_COLLISION:
        import_collision_block(text, block, OPENNEOUA)
        import_collision_shape_reference(text, block)
        if "vehicle" in kind:
            import_overeof_block(text, block)
    elif group == _SCRIPT_TAB_FIRE:
        import_fire_points_block(text, block)
    elif group == _SCRIPT_TAB_GUN:
        import_gun_points_block(text, block)
    elif group == _SCRIPT_TAB_COCKPIT:
        import_cockpit_camera_block(text, block)
    elif group == _SCRIPT_TAB_BRIEFING:
        import_briefing_size_block(text, block)


def _resolve_patch_target_block(
        text: str, kind: str, object_id: int, *, turret: bool = False,
) -> ScriptBlock:
    matches = [
        block for block in find_script_blocks(text)
        if block.kind == kind and block.object_id == object_id
    ]
    if not matches:
        raise CollisionScriptError(f"Target {kind} {object_id} non trovato.")
    if len(matches) == 1:
        block = matches[0]
    elif turret and kind == "modify_vehicle":
        authored = [
            candidate for candidate in matches
            if any(
                key in _TURRET_LIMIT_KEY_SET
                for _line, key, _raw, _indent
                in _top_level_assignment_rows(text, candidate)
            )
        ]
        block = authored[-1] if authored else matches[-1]
    else:
        raise CollisionScriptError(
            f"Target ambiguo: {len(matches)} blocchi {kind} {object_id}.")
    if not block.complete:
        raise CollisionScriptError(
            f"Parsing incompleto: manca end per {kind} {object_id}.")
    return block


def _managed_families_for_keys(keys: set[str]) -> list[_ManagedScriptFamily]:
    """Return the known logical families covered by one editor tab."""

    requested = set(keys)
    families = [
        family for family in _MANAGED_SCRIPT_FAMILIES
        if family.keys & requested
    ]
    covered = set().union(*(family.keys for family in families)) \
        if families else set()
    remaining = requested - covered
    if remaining:
        families.append(_ManagedScriptFamily(
            "managed data", frozenset(remaining)))
    return families


def _replacement_lines_for_family(
        replacement_lines: list[str], keys: frozenset[str]) -> list[str]:
    """Keep one family's assignments and its internal separator lines."""

    selected: list[int] = []
    for index, line in enumerate(replacement_lines):
        match = _GENERIC_PARAM_RE.match(line)
        if match is not None and match.group("key").lower() in keys:
            selected.append(index)
    if not selected:
        return []

    first, last = selected[0], selected[-1]
    result: list[str] = []
    for line in replacement_lines[first:last + 1]:
        if not line.strip():
            result.append("")
            continue
        match = _GENERIC_PARAM_RE.match(line)
        if match is not None and match.group("key").lower() in keys:
            result.append(line)
    return result


def _old_generated_family_lines(
        lines: list[str], block: ScriptBlock,
        family: _ManagedScriptFamily) -> set[int]:
    """Find only comments emitted by the old Entity Designer writer."""

    labels = {label.casefold() for label in family.old_disabled_labels}
    if not labels:
        return set()
    found: set[int] = set()
    index = block.start_line + 1
    while index < block.end_line:
        match = _OLD_DISABLED_HEADER_RE.match(lines[index])
        if match is None or match.group("label").strip().casefold() not in labels:
            index += 1
            continue
        found.add(index)
        cursor = index + 1
        while cursor < block.end_line:
            line = lines[cursor]
            if not line.strip():
                found.add(cursor)
                cursor += 1
                continue
            commented = _COMMENTED_ASSIGNMENT_RE.match(line)
            if (commented is None
                    or commented.group("key").lower() not in family.keys):
                break
            found.add(cursor)
            cursor += 1
        index = cursor
    return found


def _fill_internal_family_blanks(
        lines: list[str], indices: set[int]) -> set[int]:
    """Include blank separators lying only between managed family rows."""

    expanded = set(indices)
    ordered = sorted(indices)
    for left, right in zip(ordered, ordered[1:]):
        if right <= left + 1:
            continue
        between = range(left + 1, right)
        if all(not lines[index].strip() for index in between):
            expanded.update(between)
    return expanded


def _deletion_ranges(indices: set[int]) -> list[tuple[int, int]]:
    if not indices:
        return []
    ordered = sorted(indices)
    ranges: list[tuple[int, int]] = []
    start = end = ordered[0]
    for index in ordered[1:]:
        if index == end + 1:
            end = index
            continue
        ranges.append((start, end))
        start = end = index
    ranges.append((start, end))
    return ranges


def _replace_top_level_family(
        text: str, block: ScriptBlock, family: _ManagedScriptFamily,
        replacement_lines: list[str], *,
        comment_missing: bool = False) -> str:
    """Replace one family at its first anchor and remove all duplicates."""

    newline = _line_ending(text)
    had_final_newline = text.endswith(("\n", "\r"))
    source_lines = text.splitlines()
    rows = [
        row for row in _top_level_assignment_rows(text, block)
        if row[1] in family.keys
    ]
    generated = _old_generated_family_lines(source_lines, block, family)
    content_indices = {row[0] for row in rows} | generated
    delete = _fill_internal_family_blanks(source_lines, content_indices)
    anchor = min(content_indices) if content_indices else None
    block_indent = next((
        row[3] for row in _top_level_assignment_rows(text, block) if row[3]
    ), re.match(
        r"^(\s*)", source_lines[block.start_line]).group(1) + "    ")
    indent = next(
        (row[3] for row in rows if row[3]),
        next((
            re.match(r"^(\s*)", source_lines[index]).group(1)
            for index in sorted(generated)
            if re.match(r"^(\s*)", source_lines[index]).group(1)
        ), block_indent),
    )
    rendered = [
        indent + line.lstrip() if line.strip() else ""
        for line in replacement_lines
    ]
    if not rendered and comment_missing and rows:
        rendered = [indent + f"; Entity Designer disabled: {family.name}"]
        rendered.extend(
            indent + "; " + source_lines[row[0]].lstrip()
            for row in rows
        )

    # Removing a later duplicate can join two existing separator lines. Drop
    # only the right-hand blank created by that removal; unrelated spacing is
    # otherwise preserved byte-for-byte.
    for start, end in _deletion_ranges(delete):
        inserts_here = bool(rendered) and anchor is not None \
            and start <= anchor <= end
        left = start - 1
        right = end + 1
        if (not inserts_here and left > block.start_line
                and right < block.end_line
                and left not in delete and right not in delete
                and not source_lines[left].strip()
                and not source_lines[right].strip()):
            delete.add(right)

    output: list[str] = []
    inserted = False
    for index, line in enumerate(source_lines):
        if anchor is not None and index == anchor and rendered:
            output.extend(rendered)
            inserted = True
        if index in delete:
            continue
        if index == block.end_line and rendered and not inserted:
            if output and output[-1].strip():
                output.append("")
            output.extend(rendered)
            inserted = True
        output.append(line)
    return newline.join(output) + (newline if had_final_newline else "")


def _patch_top_level_keys(
        text: str, block: ScriptBlock, keys: set[str],
        replacement_lines: list[str], *,
        comment_missing: bool = False, turret: bool = False) -> str:
    """Replace each logical family without moving existing family anchors."""

    updated = text
    for family in _managed_families_for_keys(keys):
        target = _resolve_patch_target_block(
            updated, block.kind, block.object_id, turret=turret)
        replacement = _replacement_lines_for_family(
            replacement_lines, family.keys)
        updated = _replace_top_level_family(
            updated, target, family, replacement,
            comment_missing=comment_missing)
    return updated


def apply_editable_overwrite_preview(
        source_text: str, patch_text: str,
        expected_kind: str, expected_id: int,
) -> tuple[str, set[str]]:
    """Apply the user-edited overwrite patch to the original script text."""

    if not patch_text.strip():
        return source_text, set()

    patch_blocks = find_script_blocks(patch_text)
    if not patch_blocks:
        raise CollisionScriptError(
            "La preview modificabile non contiene alcun blocco script completo.")

    patch_lines = patch_text.splitlines()
    main_block: ScriptBlock | None = None
    main_sections: dict[str, list[str]] = {}
    for block in patch_blocks:
        if not block.complete:
            raise CollisionScriptError(
                "Parsing incompleto nella preview: manca end per "
                f"{block.kind} {block.object_id}.")
        is_expected_target = (
            block.kind == expected_kind
            and block.object_id == int(expected_id))
        has_marker = any(
            _script_tab_from_marker(patch_lines[index]) is not None
            for index in range(block.start_line + 1, block.end_line))
        if not is_expected_target and not has_marker:
            continue

        sections: dict[str, list[str]] = {}
        active_group: str | None = None
        for index in range(block.start_line + 1, block.end_line):
            line = patch_lines[index]
            marker_group = _script_tab_from_marker(line)
            if marker_group is not None:
                if marker_group in sections:
                    raise CollisionScriptError(
                        f"La tab {_SCRIPT_TAB_LABELS[marker_group]} compare più "
                        "volte nella preview.")
                sections[marker_group] = []
                active_group = marker_group
                continue
            if active_group is not None:
                sections[active_group].append(line)
            elif _active_code(line):
                # A normal assignment without a tab marker would be ambiguous:
                # preserving it is safer than guessing which workspace owns it.
                raise CollisionScriptError(
                    "Ogni parametro del blocco principale deve stare sotto "
                    "un marcatore '; [Tab: ...]'.")

        if sections or is_expected_target:
            if main_block is not None:
                raise CollisionScriptError(
                    "La preview contiene più di un blocco principale "
                    "compatibile con la definizione caricata.")
            main_block = block
            main_sections = sections

    selected_groups: set[str] = set()
    updated = source_text
    if main_block is not None:
        if (main_block.kind != expected_kind
                or main_block.object_id != int(expected_id)):
            raise CollisionScriptError(
                "Il blocco principale della preview non corrisponde alla "
                f"definizione caricata ({expected_kind} {expected_id}).")
        for group in _SCRIPT_TAB_ORDER:
            if group not in main_sections:
                continue
            if "weapon" in expected_kind and group != _SCRIPT_TAB_COLLISION:
                raise CollisionScriptError(
                    f"La tab {_SCRIPT_TAB_LABELS[group]} non è valida per "
                    "una definizione weapon.")
            replacement = main_sections[group]
            _validate_editable_tab_lines(
                group, replacement, expected_kind, expected_id)
            target = _resolve_patch_target_block(
                updated, expected_kind, expected_id)
            updated = _patch_top_level_keys(
                updated, target, _SCRIPT_TAB_KEYS[group], replacement)
            selected_groups.add(group)

    for block in patch_blocks:
        if main_block is not None and block == main_block:
            continue
        # Referenced turret limits belong to the Gun Points tab. Removing the
        # whole Gun Points section from the editable preview skips these blocks
        # as well, so the source definitions remain untouched.
        if _SCRIPT_TAB_GUN not in selected_groups:
            continue
        body_rows = list(_top_level_assignment_rows(patch_text, block))
        unsupported = [
            row[1] for row in body_rows
            if row[1] not in _TURRET_LIMIT_KEY_SET
        ]
        if unsupported:
            raise CollisionScriptError(
                "I blocchi aggiuntivi della preview possono modificare solo "
                "gun_side_angle, gun_up_angle e gun_down_angle.")
        if "vehicle" not in block.kind:
            raise CollisionScriptError(
                "I blocchi aggiuntivi della preview devono essere vehicle.")
        replacement = [
            patch_lines[index]
            for index in range(block.start_line + 1, block.end_line)
        ]
        for _line, key, raw, _indent in body_rows:
            value = _script_integer(raw, key)
            if value < 0:
                raise CollisionScriptError(
                    f"Valore negativo non valido per {key}: {raw}")
        target = _resolve_patch_target_block(
            updated, block.kind, block.object_id, turret=True)
        updated = _patch_top_level_keys(
            updated, target, _TURRET_LIMIT_KEY_SET, replacement)
        selected_groups.add(_SCRIPT_TAB_GUN)

    return updated, selected_groups


def plan_script_update(
    text: str,
    kind: str,
    object_id: int,
    project: CollisionProject,
    *,
    comment_missing: bool = True,
    replace_all_managed: bool = False,
) -> tuple[str, str, str]:
    """Patch only supported spatial/gameplay keys in one script block."""

    matches = [
        block for block in find_script_blocks(text)
        if block.kind == kind and block.object_id == object_id
    ]
    if not matches:
        raise CollisionScriptError(f"Target {kind} {object_id} non trovato.")
    if len(matches) > 1:
        raise CollisionScriptError(
            f"Target ambiguo: {len(matches)} blocchi {kind} {object_id}.")
    block = matches[0]
    if not block.complete:
        raise CollisionScriptError(
            f"Parsing incompleto: manca end per {kind} {object_id}.")

    newline = _line_ending(text)
    had_final_newline = text.endswith(("\n", "\r"))
    lines = text.splitlines()
    rows = _parameter_rows(lines, block)
    radius_rows = [row for row in rows if row[1] == "radius"]
    overeof_rows = [row for row in rows if row[1] == "overeof"]
    fire_rows = [
        row for row in rows
        if row[1] in ("fire_x", "fire_y", "fire_z", "num_weapons")
    ]
    cockpit_rows = [
        row for row in rows if row[1] == "cockpit_camera_offset_xyz"
    ]
    gun_rows = [row for row in rows if row[1] in _GUN_POINT_KEYS]
    coll_rows = [row for row in rows if row[1].startswith("coll_")]
    shape_rows = [row for row in rows if row[1] == "collision_shape"]
    indent = next(
        (row[3] for row in rows if row[3]),
        re.match(r"^(\s*)", lines[block.start_line]).group(1) + "    ",
    )
    replace: dict[int, list[str]] = {}
    delete: set[int] = set()
    insert_before_end: list[str] = []

    if replace_all_managed:
        # The loaded-script workflow treats each editor family as canonical,
        # but each family keeps its own original position in the target block.
        # num_weapons is not exclusively a Fire Point parameter. If the source
        # contains no fire offsets, preserve that standalone gameplay value
        # while the Fire Point workspace remains disabled.
        source_has_fire_offsets = any(
            row[1] in {"fire_x", "fire_y", "fire_z"} for row in fire_rows)
        preserve_standalone_num_weapons = (
            "vehicle" in kind and not project.fire_points_enabled
            and not source_has_fire_offsets)
        updated = text
        for group in _SCRIPT_TAB_ORDER:
            if "weapon" in kind and group != _SCRIPT_TAB_COLLISION:
                continue
            group_keys = set(_SCRIPT_TAB_KEYS[group])
            if group == _SCRIPT_TAB_FIRE and preserve_standalone_num_weapons:
                group_keys.discard("num_weapons")
            target = _resolve_patch_target_block(updated, kind, object_id)
            updated = _patch_top_level_keys(
                updated, target, group_keys,
                _script_tab_data_lines(project, group))
        updated = _apply_turret_limit_updates(
            updated, project, comment_missing=False)
        preview = "".join(difflib.unified_diff(
            text.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile="current", tofile="Entity Designer preview",
        ))
        return updated, preview, block.name

    def replace_group(rows_to_replace, new_lines, disabled_label):
        if new_lines:
            rendered = [indent + value if value else "" for value in new_lines]
            if rows_to_replace:
                replace[rows_to_replace[0][0]] = rendered
                delete.update(row[0] for row in rows_to_replace[1:])
            else:
                if insert_before_end and insert_before_end[-1] != "":
                    insert_before_end.append("")
                insert_before_end.extend(rendered)
        elif rows_to_replace and comment_missing:
            first = rows_to_replace[0][0]
            replace[first] = [
                indent + f"; Entity Designer disabled: {disabled_label}",
                indent + "; " + lines[first].lstrip(),
            ]
            for row in rows_to_replace[1:]:
                replace[row[0]] = [indent + "; " + lines[row[0]].lstrip()]

    legacy_lines = (
        [f"radius = {_radius_number(project.legacy.radius)}"]
        if project.legacy is not None else [])
    compound_lines = []
    if project.compound:
        if len(project.compound) > UNIT_COLL_MAX_COUNT:
            raise CollisionScriptError(
                f"OpenNeoUA collision spheres exceed the runtime limit "
                f"of {UNIT_COLL_MAX_COUNT}; imported data was kept unchanged.")
        compound_lines = [f"coll_num = {len(project.compound)}"]
        for index, sphere in enumerate(project.compound):
            compound_lines.extend([
                "",
                f"coll_act = {index}",
                f"coll_xyz = {_tuple_text(sphere.x, sphere.y, sphere.z)}",
                f"coll_radius = {_number(sphere.radius)}",
            ])
    replace_group(radius_rows, legacy_lines, "legacy radius")
    if "vehicle" in kind and project.briefing_size_enabled:
        briefing_rows = [row for row in rows if row[1] == "briefing_size"]
        replace_group(briefing_rows, briefing_size_data_lines(project), "briefing size")
    # Overeof is a separate vanilla ground-alignment parameter, not a
    # collision sphere.  Preserve existing script data unless the user has
    # explicitly enabled management of it in a vehicle project.
    if "vehicle" in kind and project.overeof_enabled:
        replace_group(
            overeof_rows,
            [f"overeof = {_number(project.overeof)}"],
            "overeof",
        )
    if "vehicle" in kind and project.cockpit_camera_enabled:
        replace_group(
            cockpit_rows, cockpit_camera_data_lines(project),
            "cockpit camera offset")
    if "vehicle" in kind and project.fire_points_enabled:
        replace_group(
            fire_rows,
            [
                f"fire_x = {_number(project.fire_x)}",
                f"fire_y = {_number(project.fire_y)}",
                f"fire_z = {_number(project.fire_z)}",
                f"num_weapons = {_num_weapons_text(project)}",
            ],
            "fire points",
        )
    if "vehicle" in kind and project.gun_points_enabled:
        replace_group(
            gun_rows, gun_point_data_lines(project), "gun points")
    replace_group(coll_rows, compound_lines, "compound collisions")
    shape_lines = _collision_tab_data_lines(project)
    shape_lines = [
        line for line in shape_lines
        if line.startswith("collision_shape = ")
    ]
    replace_group(shape_rows, shape_lines, "collision shape")

    output: list[str] = []
    for index, line in enumerate(lines):
        if index == block.end_line and insert_before_end:
            if output and output[-1].strip():
                output.append("")
            output.extend(insert_before_end)
        if index in delete:
            continue
        if index in replace:
            output.extend(replace[index])
        else:
            output.append(line)
    updated = newline.join(output) + (newline if had_final_newline else "")
    updated = _apply_turret_limit_updates(
        updated, project, comment_missing=comment_missing)
    preview = "".join(difflib.unified_diff(
        text.splitlines(keepends=True),
        updated.splitlines(keepends=True),
        fromfile="current", tofile="Entity Designer preview",
    ))
    return updated, preview, block.name


def read_script_file(path: str | Path) -> tuple[str, str, bool]:
    """Read common OpenNeoUA text encodings without silently changing them."""

    data = Path(path).read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8", True
    try:
        return data.decode("utf-8"), "utf-8", False
    except UnicodeDecodeError:
        return data.decode("latin-1"), "latin-1", False


def write_script_file(
    path: str | Path, text: str, encoding: str, bom: bool,
) -> None:
    data = text.encode(encoding)
    if bom and encoding == "utf-8":
        data = b"\xef\xbb\xbf" + data
    Path(path).write_bytes(data)


def validate_project(
    project: CollisionProject,
    model_bounds: tuple[float, float, float, float, float, float] | None,
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if not project.name.strip():
        errors.append("Il progetto non ha un Vehicle / Weapon Name.")
    if not project.source_model:
        errors.append("Nessun modello è caricato.")
    if (project.target_category == VEHICLE and project.compound
            and not project.overeof_enabled):
        warnings.append(
            "Overeof non incluso: le collisioni compound non sostituiscono "
            "l'allineamento verticale al terreno. Mantieni o ripristina "
            "l'overeof vanilla nello script del veicolo.")
    if (project.target_category == VEHICLE and project.overeof_enabled
            and not math.isfinite(project.overeof)):
        errors.append("Overeof deve essere un numero finito.")
    if project.target_category == VEHICLE and project.cockpit_camera_enabled:
        if not all(math.isfinite(value) for value in (
                project.cockpit_camera_offset_x,
                project.cockpit_camera_offset_y,
                project.cockpit_camera_offset_z)):
            errors.append("Cockpit Camera X/Y/Z devono essere numeri finiti.")
    if project.target_category == VEHICLE and project.briefing_size_enabled:
        if not math.isfinite(project.briefing_size) or not 1 <= project.briefing_size <= 10000:
            errors.append("Briefing Model Size deve essere tra 1 e 10000.")
    if project.target_category == VEHICLE and project.fire_points_enabled:
        if not all(math.isfinite(value) for value in (
                project.fire_x, project.fire_y, project.fire_z)):
            errors.append("Fire X/Y/Z devono essere numeri finiti.")
        minimum, maximum = _project_num_weapons_range(project)
        if not ((minimum == 0 and maximum == 0) or
                (1 <= minimum <= maximum <= 255)):
            errors.append(
                "Num Weapons deve essere 0, un valore 1-255 o un range "
                "min_max interamente compreso tra 1 e 255.")
    if project.target_category == VEHICLE and project.gun_points_enabled:
        if len(project.gun_points) > 40:
            errors.append(
                "Gun Points oltre il limite combinato di 20 robo + 20 unit.")
        family_counts = {"robo": 0, "unit": 0}
        for index, point in enumerate(project.gun_points):
            if point.scheme not in family_counts:
                errors.append(f"Gun Point {index}: famiglia script non valida.")
                continue
            family_counts[point.scheme] += 1
            if family_counts[point.scheme] > 20:
                errors.append(
                    f"Gun Point {index}: massimo 20 punti {point.scheme}.")
            if not all(math.isfinite(value) for value in (
                    point.x, point.y, point.z,
                    point.dir_x, point.dir_y, point.dir_z)):
                errors.append(
                    f"Gun Point {index}: posizione/direzione non numerica.")
            if not (0 <= int(point.gun_type) <= 255):
                errors.append(
                    f"Gun Point {index}: gun type deve essere 0..255.")
        for vehicle_id, limits in project.turret_limits.items():
            if not limits.enabled:
                continue
            for field, label, value in (
                    ("side", "Side", limits.side),
                    ("up", "Up", limits.up),
                    ("down", "Down", limits.down)):
                maximum = _gun_angle_max(field)
                if not 0 <= int(value) <= maximum:
                    errors.append(
                        f"Vehicle {vehicle_id}: Gun {label} Angle deve essere "
                        f"compreso tra 0 e {maximum}.")
    for index, sphere in enumerate(project.spheres()):
        if not all(math.isfinite(value) for value in (
                sphere.x, sphere.y, sphere.z, sphere.radius)):
            errors.append(f"Sfera {index}: coordinate o raggio non numerici.")
        if sphere.radius <= 0:
            errors.append(f"Sfera {index}: il raggio deve essere maggiore di 0.")
    if model_bounds is not None:
        x0, y0, z0, x1, y1, z1 = model_bounds
        center = ((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2)
        extent = max(x1 - x0, y1 - y0, z1 - z0, 1.0)
        for index, sphere in enumerate(project.compound):
            distance = math.dist(center, sphere.center)
            if distance > extent * 4.0 + sphere.radius:
                warnings.append(
                    f"Sfera compound {index} molto distante dai bounds del "
                    "modello.")
    return errors, warnings


def _normalized_vector3(vector) -> tuple[float, float, float] | None:
    values = tuple(float(value) for value in vector)
    length = math.sqrt(sum(value * value for value in values))
    if not math.isfinite(length) or length <= 1e-9:
        return None
    return tuple(value / length for value in values)


def _cross3(a, b) -> tuple[float, float, float]:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def turret_limit_grid(
        origin, direction, side: int, up: int, down: int, radius: float,
        segments: int = 32) -> dict[str, list[list[tuple[float, float, float]]]]:
    """Build the runtime aiming envelope around one Gun Point direction.

    UA model space uses +Y downward, so positive elevation moves toward -Y.
    The frame follows ``ypagun_func128``: the authored ``gun_dir`` is the
    neutral forward axis and side/up/down limits rotate around that axis.
    """

    forward = _normalized_vector3(direction)
    if forward is None or not math.isfinite(radius) or radius <= 0.0:
        return {}
    origin = tuple(float(value) for value in origin)
    side = _clamp_gun_angle("side", side)
    up = _clamp_gun_angle("up", up)
    down = _clamp_gun_angle("down", down)

    # Match the engine's preferred local Y axis: world +Y projected onto the
    # plane perpendicular to gun_dir, with +Z as the vertical-direction
    # fallback used by ypagun_func128.
    world_down = (0.0, 1.0, 0.0)
    projection = sum(a * b for a, b in zip(world_down, forward))
    down_axis = _normalized_vector3(tuple(
        world_down[index] - forward[index] * projection
        for index in range(3)))
    if down_axis is None:
        fallback = (0.0, 0.0, 1.0)
        projection = sum(a * b for a, b in zip(fallback, forward))
        down_axis = _normalized_vector3(tuple(
            fallback[index] - forward[index] * projection
            for index in range(3)))
    if down_axis is None:
        return {}
    right_axis = _normalized_vector3(_cross3(down_axis, forward))
    if right_axis is None:
        return {}

    full_side = int(side) > GUN_FREE_SIDE_THRESHOLD
    side_radians = math.pi if full_side else min(
        math.pi, max(0.0, float(side) / GUN_ANGLE_SCALE))
    # Target directions cannot exceed a hemisphere even when a larger script
    # value is authored. Clamp only the visualization, never the saved value.
    up_radians = min(math.pi / 2.0, max(0.0, float(up) / GUN_ANGLE_SCALE))
    down_radians = min(
        math.pi / 2.0, max(0.0, float(down) / GUN_ANGLE_SCALE))
    segments = max(4, int(segments))

    def sample(start: float, end: float) -> list[float]:
        return [
            start + (end - start) * index / segments
            for index in range(segments + 1)
        ]

    yaw_values = sample(-math.pi, math.pi) if full_side else sample(
        -side_radians, side_radians)

    def point(yaw: float, pitch: float):
        cy, sy = math.cos(yaw), math.sin(yaw)
        cp, sp = math.cos(pitch), math.sin(pitch)
        horizontal = tuple(
            forward[index] * cy + right_axis[index] * sy
            for index in range(3))
        vector = tuple(
            horizontal[index] * cp - down_axis[index] * sp
            for index in range(3))
        return tuple(
            origin[index] + vector[index] * radius for index in range(3))

    yaw_guides = (
        (-math.pi, -math.pi / 2.0, 0.0, math.pi / 2.0)
        if full_side else (-side_radians, 0.0, side_radians)
    )
    pitch_values = sample(-down_radians, up_radians)
    return {
        "side": [[point(yaw, 0.0) for yaw in yaw_values]],
        "up": [[point(yaw, up_radians) for yaw in yaw_values]],
        "down": [[point(yaw, -down_radians) for yaw in yaw_values]],
        "meridians": [
            [point(yaw, pitch) for pitch in pitch_values]
            for yaw in yaw_guides
        ],
    }
