"""Manual collision-sphere editor integrated with OpenUAStudio.

The tool deliberately stores only script data.  BASE/SKLT assets stay
read-only and all model loading, texture resolution, camera and rendering are
delegated to the existing OpenUAStudio asset-family and viewport code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import difflib
import math
from pathlib import Path
import re
import shutil
import sys

from PySide6.QtCore import (
    QEvent, QPoint, QPointF, QRectF, QSignalBlocker, QSize, Qt, Signal,
)
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QCloseEvent,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPen,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QSplitter,
    QToolBar,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
    QPlainTextEdit,
    QScrollArea,
    QSizePolicy,
    QStyledItemDelegate,
)

from assembly_viewer import AssetViewport, VIEW_PRESETS
from asset_family import AssetFamily, load_asset_family, load_manual_family
from asset_tree_filter import filter_tree as filter_asset_tree
from model_space_gizmo import ModelSpaceGizmo
from vp_manager import (
    EmbeddedVPSet,
    VPEmbeddedError,
    VPParseError,
    VPTable,
    load_visproto,
    normalize_base_name,
    normalize_skeleton_name,
    reconstruct_embedded_vps,
)


_PUBLIC_DEPENDENCY_DEFAULTS = {
    "load_asset_family": load_asset_family,
    "load_manual_family": load_manual_family,
}


LEGACY = "legacy"
VEHICLE = "vehicle"
WEAPON = "weapon"
COMPOUND_TYPES = (VEHICLE, WEAPON)
TYPE_LABELS = {
    LEGACY: "Legacy Radius",
    VEHICLE: "Vehicle Collision",
    WEAPON: "Weapon Collision",
}
# Exact F10 debug colors from OpenUA src/yw_game.cpp.
TYPE_COLORS = {
    LEGACY: QColor(220, 60, 60),
    VEHICLE: QColor(60, 220, 60),
    WEAPON: QColor(60, 130, 235),
}
FIRE_POINT_COLOR = QColor(235, 60, 60)
TRACER_POINT_COLOR = QColor(255, 215, 60)
SCRIPT_TYPES = (
    "new_vehicle", "modify_vehicle", "new_weapon", "modify_weapon",
)
_MODEL_NAME_ROLE = int(Qt.ItemDataRole.UserRole) + 1
_SPHERE_INDEX_ROLE = int(Qt.ItemDataRole.UserRole) + 2
_MODEL_VP_ROLE = int(Qt.ItemDataRole.UserRole) + 3
_FIRE_POINT_INDEX_ROLE = int(Qt.ItemDataRole.UserRole) + 4
_TRACER_POINT_INDEX_ROLE = int(Qt.ItemDataRole.UserRole) + 5
_RADIUS_SLIDER_STEPS = 10000
_RADIUS_LOG_MIN = -3.0
_RADIUS_LOG_MAX = 6.0
_HEADER_RE = re.compile(
    r"^\s*(new_vehicle|modify_vehicle|new_weapon|modify_weapon)"
    r"\s+(-?\d+)\b", re.IGNORECASE,
)
_PARAM_RE = re.compile(
    r"^(?P<indent>\s*)(?P<key>radius|overeof|fire_x|fire_y|fire_z|"
    r"num_weapons|coll_num|coll_act|"
    r"coll_x|coll_y|coll_z|coll_radius)\s*=\s*(?P<value>[^;#\r\n]+)",
    re.IGNORECASE,
)
_GENERIC_PARAM_RE = re.compile(
    r"^(?P<indent>\s*)(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
    r"(?P<value>[^;#\r\n]+)",
    re.IGNORECASE,
)


def _number(value: float) -> str:
    if math.isfinite(value) and abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _radius_number(value: float) -> str:
    """Engine-safe whole-number radius used by lists and text output."""

    return str(int(round(value)))


def fire_point_positions(project: "CollisionProject") -> list[tuple[float, float, float]]:
    """Return the exact vanilla projectile spawn offsets for a vehicle.

    Vanilla Urban Assault treats ``num_weapons`` values of zero and one as a
    single muzzle at the authored ``fire_x/y/z`` position.  With two or more
    weapons it distributes the muzzles evenly on local X from
    ``-abs(fire_x)`` to ``+abs(fire_x)`` while preserving fire Y and Z.
    """

    count = max(1, int(project.num_weapons))
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


def tracer_point_positions(
        project: "CollisionProject",
) -> list[tuple[float, float, float]]:
    """Return the exact OpenUA MGUN tracer origins in model space.

    One MGUN uses the authored X/Y/Z offset directly.  Two or more MGUNs use
    the absolute X value as a symmetric half-spacing and preserve the common
    Y/Z offsets, matching ``ypabact_GetMinigunTracerOrigin``.
    """

    count = max(1, int(project.num_mguns))
    if count == 1:
        xs = [float(project.mgun_tracer_offset_x)]
    else:
        extent = abs(float(project.mgun_tracer_offset_x))
        xs = [
            (index * 2.0 * extent / (count - 1)) - extent
            for index in range(count)
        ]
    return [
        (x, float(project.mgun_tracer_offset_y),
         float(project.mgun_tracer_offset_z))
        for x in xs
    ]


def effective_runtime_radius(project: "CollisionProject") -> float:
    """Return the collision broad-phase extent used internally by OpenUA.

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


def _point_segment_distance(
        point: QPointF, start: QPointF, end: QPointF) -> float:
    """Return the shortest 2D distance from *point* to a line segment."""

    dx = end.x() - start.x()
    dy = end.y() - start.y()
    length_sq = dx * dx + dy * dy
    if length_sq <= 1e-12:
        return math.hypot(point.x() - start.x(), point.y() - start.y())
    ratio = (
        (point.x() - start.x()) * dx
        + (point.y() - start.y()) * dy
    ) / length_sq
    ratio = max(0.0, min(1.0, ratio))
    closest_x = start.x() + ratio * dx
    closest_y = start.y() + ratio * dy
    return math.hypot(point.x() - closest_x, point.y() - closest_y)


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
class CollisionProject:
    name: str = ""
    source_model: str = ""
    source_base: str = ""
    target_category: str = VEHICLE
    # Visual-only preview of OpenUA's vp_scale_x/y/z.  These values never
    # modify the source model and are never emitted as collision parameters.
    model_scale_x: float = 1.0
    model_scale_y: float = 1.0
    model_scale_z: float = 1.0
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
    # OpenUA MGUN tracer origins. These are visual spawn offsets and never
    # participate in coll_num or alter the actual hitscan calculation.
    tracer_points_enabled: bool = False
    mgun_tracer_offset_x: float = 0.0
    mgun_tracer_offset_y: float = 0.0
    mgun_tracer_offset_z: float = 0.0
    num_mguns: int = 1
    legacy: CollisionSphere | None = None
    compound: list[CollisionSphere] = field(default_factory=list)

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
            self.overeof_enabled, self.overeof,
            self.fire_points_enabled,
            self.fire_x, self.fire_y, self.fire_z, self.num_weapons,
            self.tracer_points_enabled,
            self.mgun_tracer_offset_x, self.mgun_tracer_offset_y,
            self.mgun_tracer_offset_z, self.num_mguns,
            one(self.legacy),
            tuple(one(sphere) for sphere in self.compound),
        )

    def restore(self, state: tuple) -> None:
        def one(values):
            return None if values is None else CollisionSphere(*values)
        (self.name, self.source_model, self.source_base,
         self.target_category,
         self.model_scale_x, self.model_scale_y, self.model_scale_z,
         self.overeof_enabled, self.overeof,
         self.fire_points_enabled,
         self.fire_x, self.fire_y, self.fire_z, self.num_weapons,
         self.tracer_points_enabled,
         self.mgun_tracer_offset_x, self.mgun_tracer_offset_y,
         self.mgun_tracer_offset_z, self.num_mguns,
         legacy, compound) = state
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
    vp_normal: int
    scale_x: float = 1.0
    scale_y: float = 1.0
    scale_z: float = 1.0

    @property
    def label(self) -> str:
        suffix = f" — {self.block.name}" if self.block.name else ""
        return (
            f"{self.block.kind} {self.block.object_id}{suffix}  "
            f"[vp_normal {self.vp_normal}]"
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
            match.group("value").strip().split()[0],
            match.group("indent"),
        ))
    return rows


def script_model_references(text: str) -> list[VehicleModelReference]:
    """Read ``vp_normal`` references from new vehicles and weapons.

    The final active assignment wins, matching Urban Assault script override
    behavior. ``modify_vehicle`` and ``modify_weapon`` are deliberately
    excluded because resolving inherited ``vp_normal`` would require the full
    include chain rather than one source file. ``vp_scale_x/y/z`` are read as
    visual-only preview values for both supported definition types.
    """

    lines = text.splitlines()
    references: list[VehicleModelReference] = []
    for block in find_script_blocks(text):
        if block.kind not in ("new_vehicle", "new_weapon") \
                or not block.complete:
            continue
        values = {
            "vp_normal": None,
            "vp_scale_x": 1.0,
            "vp_scale_y": 1.0,
            "vp_scale_z": 1.0,
        }
        for _line, key, raw, _indent in _generic_parameter_rows(lines, block):
            if key not in values:
                continue
            try:
                values[key] = float(raw)
            except ValueError as exc:
                raise CollisionScriptError(
                    f"Non-numeric {key} in {block.kind} "
                    f"{block.object_id}: {raw}") from exc
        raw_vp = values["vp_normal"]
        if raw_vp is None:
            continue
        if not math.isfinite(raw_vp):
            raise CollisionScriptError(
                f"Non-finite vp_normal in {block.kind} "
                f"{block.object_id}: {raw_vp}")
        vp_id = int(raw_vp)
        if abs(raw_vp - vp_id) > 1e-9 or vp_id < 0:
            raise CollisionScriptError(
                f"Invalid vp_normal in {block.kind} "
                f"{block.object_id}: {raw_vp}")
        scales = (
            float(values["vp_scale_x"]),
            float(values["vp_scale_y"]),
            float(values["vp_scale_z"]),
        )
        if not all(math.isfinite(value) and value >= 0.0 for value in scales):
            raise CollisionScriptError(
                f"Invalid vp_scale_x/y/z in {block.kind} "
                f"{block.object_id}: {scales}")
        references.append(VehicleModelReference(
            block=block,
            vp_normal=vp_id,
            scale_x=scales[0],
            scale_y=scales[1],
            scale_z=scales[2],
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
    """Resolve the same VP source precedence used by OpenUAStudio.

    Loose ``VISPROTO.LST`` wins over the embedded table; the legacy Scripts
    copy is only a fallback when neither of the stronger sources is usable.
    Invalid optional files never replace a valid embedded database.
    """

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
    if embedded.entries:
        return embedded.as_table(), "embedded visproto.base", warnings
    if scripts.is_file():
        try:
            return (
                load_visproto(scripts, require_terminator=False),
                "fallback Scripts/VISPROTO.LST",
                warnings,
            )
        except (OSError, VPParseError) as exc:
            warnings.append(
                f"Invalid Scripts/VISPROTO.LST ignored: {exc}")
    raise CollisionScriptError(
        "No usable VP database was found beside the selected SET.BAS.")


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
    generic_rows = _generic_parameter_rows(lines, block)
    legacy = None
    expected = None
    current = None
    compounds: list[CollisionSphere] = []
    warnings: list[str] = []

    for _line, key, raw, _indent in rows:
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
            attr = {
                "coll_x": "x", "coll_y": "y", "coll_z": "z",
                "coll_radius": "radius",
            }.get(key)
            if attr is not None:
                setattr(current, attr, value)

    if expected is not None and expected != len(compounds):
        warnings.append(
            f"coll_num={expected}, ma sono stati letti "
            f"{len(compounds)} blocchi coll_act.")
    return legacy, compounds, warnings


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
) -> tuple[bool, float, float, float, int]:
    """Read active top-level vanilla vehicle fire-point parameters."""

    if not block.complete:
        raise CollisionScriptError(
            f"Parsing incompleto: manca end per {block.kind} "
            f"{block.object_id}.")
    values = {
        "fire_x": 0.0,
        "fire_y": 0.0,
        "fire_z": 0.0,
        "num_weapons": 1.0,
    }
    enabled = False
    for _line, key, raw, _indent in _parameter_rows(
            text.splitlines(), block):
        if key not in values:
            continue
        try:
            values[key] = float(raw)
        except ValueError as exc:
            raise CollisionScriptError(
                f"Valore non numerico per {key}: {raw}") from exc
        enabled = True
    return (
        enabled,
        values["fire_x"], values["fire_y"], values["fire_z"],
        max(0, min(255, int(values["num_weapons"]))),
    )


def import_tracer_points_block(
        text: str, block: ScriptBlock, runtime_radius: float = 20.0,
) -> tuple[bool, float, float, float, int]:
    """Read active OpenUA MGUN tracer-origin parameters."""

    if not block.complete:
        raise CollisionScriptError(
            f"Parsing incompleto: manca end per {block.kind} "
            f"{block.object_id}.")
    values = {
        "mgun_tracer_offset_x": None,
        "mgun_tracer_offset_y": None,
        "mgun_tracer_offset_z": None,
        "num_mguns": 1.0,
    }
    offset_seen = False
    for _line, key, raw, _indent in _generic_parameter_rows(
            text.splitlines(), block):
        if key not in values:
            continue
        try:
            values[key] = float(raw)
        except ValueError as exc:
            raise CollisionScriptError(
                f"Valore non numerico per {key}: {raw}") from exc
        if key.startswith("mgun_tracer_offset_"):
            offset_seen = True
    radius = (
        float(runtime_radius)
        if math.isfinite(runtime_radius) and runtime_radius > 0.0 else 20.0)
    count = max(1, min(255, int(values["num_mguns"])))
    return (
        offset_seen,
        (values["mgun_tracer_offset_x"]
         if values["mgun_tracer_offset_x"] is not None
         else radius * 0.25 if count > 1 else 0.0),
        (values["mgun_tracer_offset_y"]
         if values["mgun_tracer_offset_y"] is not None
         else -radius * 0.25),
        (values["mgun_tracer_offset_z"]
         if values["mgun_tracer_offset_z"] is not None else radius),
        count,
    )


def collision_data_lines(project: CollisionProject) -> list[str]:
    lines: list[str] = []
    if project.legacy is not None:
        lines.append(f"radius = {_radius_number(project.legacy.radius)}")
    if project.target_category == VEHICLE and project.overeof_enabled:
        lines.append(f"overeof = {_number(project.overeof)}")
    if project.target_category == VEHICLE and project.fire_points_enabled:
        if lines:
            lines.append("")
        lines.extend([
            f"fire_x = {_number(project.fire_x)}",
            f"fire_y = {_number(project.fire_y)}",
            f"fire_z = {_number(project.fire_z)}",
            f"num_weapons = {max(0, min(255, int(project.num_weapons)))}",
        ])
    if project.target_category == VEHICLE and project.tracer_points_enabled:
        if lines:
            lines.append("")
        lines.extend([
            "; OpenUA-only MGUN tracer points",
            "mgun_tracer_offset_x = "
            f"{_number(project.mgun_tracer_offset_x)}",
            "mgun_tracer_offset_y = "
            f"{_number(project.mgun_tracer_offset_y)}",
            "mgun_tracer_offset_z = "
            f"{_number(project.mgun_tracer_offset_z)}",
            f"num_mguns = {max(1, min(255, int(project.num_mguns)))}",
        ])
    if project.compound:
        if lines:
            lines.append("")
        lines.append(f"coll_num = {len(project.compound)}")
        for index, sphere in enumerate(project.compound):
            lines.extend([
                "",
                f"coll_act = {index}",
                f"coll_x = {_number(sphere.x)}",
                f"coll_y = {_number(sphere.y)}",
                f"coll_z = {_number(sphere.z)}",
                f"coll_radius = {_radius_number(sphere.radius)}",
            ])
    return lines


def export_collision_text(project: CollisionProject) -> str:
    header = [
        f"; Collision Editor project: {project.name}",
        f"; Source model: {project.source_model or '<none>'}",
        f"; Source BASE: {project.source_base or '<none>'}",
        f"; Target category: {project.target_category}",
        "",
    ]
    data = collision_data_lines(project)
    return "\n".join(header + data).rstrip() + "\n"


def _line_ending(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


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
    generic_rows = _generic_parameter_rows(lines, block)
    radius_rows = [row for row in rows if row[1] == "radius"]
    overeof_rows = [row for row in rows if row[1] == "overeof"]
    fire_rows = [
        row for row in rows
        if row[1] in ("fire_x", "fire_y", "fire_z", "num_weapons")
    ]
    tracer_rows = [
        row for row in generic_rows
        if row[1] in (
            "mgun_tracer_offset_x", "mgun_tracer_offset_y",
            "mgun_tracer_offset_z", "num_mguns")
    ]
    coll_rows = [row for row in rows if row[1].startswith("coll_")]
    indent = next(
        (row[3] for row in rows if row[3]),
        re.match(r"^(\s*)", lines[block.start_line]).group(1) + "    ",
    )
    replace: dict[int, list[str]] = {}
    delete: set[int] = set()
    insert_before_end: list[str] = []

    if replace_all_managed:
        # The loaded-script workflow treats the editor state as the canonical
        # source for every parameter it manages.  Remove all active managed
        # assignments from the selected block, then insert one clean block.
        # Commented historical lines stay untouched because the game ignores
        # them and deleting user notes silently would be destructive.
        delete.update(row[0] for row in rows)
        if project.tracer_points_enabled:
            delete.update(row[0] for row in tracer_rows)
        canonical = collision_data_lines(project)
        insert_before_end.extend(
            indent + line if line else "" for line in canonical)

        output: list[str] = []
        for index, line in enumerate(lines):
            if index == block.end_line and insert_before_end:
                while output and not output[-1].strip():
                    output.pop()
                if output and output[-1].strip():
                    output.append("")
                output.extend(insert_before_end)
                if output and output[-1].strip():
                    output.append("")
            if index in delete:
                continue
            output.append(line)
        updated = newline.join(output) + (newline if had_final_newline else "")
        preview = "".join(difflib.unified_diff(
            text.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile="current", tofile="Collision Editor preview",
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
                indent + f"; Collision Editor disabled: {disabled_label}",
                indent + "; " + lines[first].lstrip(),
            ]
            for row in rows_to_replace[1:]:
                replace[row[0]] = [indent + "; " + lines[row[0]].lstrip()]

    legacy_lines = (
        [f"radius = {_radius_number(project.legacy.radius)}"]
        if project.legacy is not None else [])
    compound_lines = []
    if project.compound:
        compound_lines = [f"coll_num = {len(project.compound)}"]
        for index, sphere in enumerate(project.compound):
            compound_lines.extend([
                "",
                f"coll_act = {index}",
                f"coll_x = {_number(sphere.x)}",
                f"coll_y = {_number(sphere.y)}",
                f"coll_z = {_number(sphere.z)}",
                f"coll_radius = {_radius_number(sphere.radius)}",
            ])
    replace_group(radius_rows, legacy_lines, "legacy radius")
    # Overeof is a separate vanilla ground-alignment parameter, not a
    # collision sphere.  Preserve existing script data unless the user has
    # explicitly enabled management of it in a vehicle project.
    if "vehicle" in kind and project.overeof_enabled:
        replace_group(
            overeof_rows,
            [f"overeof = {_number(project.overeof)}"],
            "overeof",
        )
    if "vehicle" in kind and project.fire_points_enabled:
        replace_group(
            fire_rows,
            [
                f"fire_x = {_number(project.fire_x)}",
                f"fire_y = {_number(project.fire_y)}",
                f"fire_z = {_number(project.fire_z)}",
                "num_weapons = "
                f"{max(0, min(255, int(project.num_weapons)))}",
            ],
            "fire points",
        )
    if "vehicle" in kind and project.tracer_points_enabled:
        replace_group(
            tracer_rows,
            [
                "mgun_tracer_offset_x = "
                f"{_number(project.mgun_tracer_offset_x)}",
                "mgun_tracer_offset_y = "
                f"{_number(project.mgun_tracer_offset_y)}",
                "mgun_tracer_offset_z = "
                f"{_number(project.mgun_tracer_offset_z)}",
                f"num_mguns = {max(1, min(255, int(project.num_mguns)))}",
            ],
            "OpenUA MGUN tracer points",
        )
    replace_group(coll_rows, compound_lines, "compound collisions")

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
    preview = "".join(difflib.unified_diff(
        text.splitlines(keepends=True),
        updated.splitlines(keepends=True),
        fromfile="current", tofile="Collision Editor preview",
    ))
    return updated, preview, block.name


def create_backup(path: str | Path) -> Path:
    source = Path(path)
    candidate = source.with_name(source.name + ".bak")
    if candidate.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        candidate = source.with_name(source.name + f".{stamp}.bak")
        suffix = 2
        while candidate.exists():
            candidate = source.with_name(
                source.name + f".{stamp}.{suffix}.bak")
            suffix += 1
    shutil.copy2(source, candidate)
    return candidate


def read_script_file(path: str | Path) -> tuple[str, str, bool]:
    """Read common OpenUA text encodings without silently changing them."""

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
    if not project.compound:
        warnings.append("Il progetto non contiene sfere compound.")
    if (project.target_category == VEHICLE and project.compound
            and not project.overeof_enabled):
        warnings.append(
            "Overeof non incluso: le collisioni compound non sostituiscono "
            "l'allineamento verticale al terreno. Mantieni o ripristina "
            "l'overeof vanilla nello script del veicolo.")
    if (project.target_category == VEHICLE and project.overeof_enabled
            and not math.isfinite(project.overeof)):
        errors.append("Overeof deve essere un numero finito.")
    if project.target_category == VEHICLE and project.fire_points_enabled:
        if not all(math.isfinite(value) for value in (
                project.fire_x, project.fire_y, project.fire_z)):
            errors.append("Fire X/Y/Z devono essere numeri finiti.")
        if not (0 <= int(project.num_weapons) <= 255):
            errors.append("Num Weapons deve essere compreso tra 0 e 255.")
    if project.target_category == VEHICLE and project.tracer_points_enabled:
        offsets = (
            project.mgun_tracer_offset_x,
            project.mgun_tracer_offset_y,
            project.mgun_tracer_offset_z,
        )
        if not all(math.isfinite(value) for value in offsets):
            errors.append("MGUN Tracer Offset X/Y/Z devono essere numeri finiti.")
        elif any(abs(value) > 6000.0 for value in offsets):
            errors.append(
                "MGUN Tracer Offset X/Y/Z devono essere compresi tra "
                "-6000 e 6000.")
        if not (1 <= int(project.num_mguns) <= 255):
            errors.append("Num MGUNs deve essere compreso tra 1 e 255.")
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


class CollisionMoveGizmo(ModelSpaceGizmo):
    """Camera-oriented move gizmo limited to the six signed model axes."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # Keep the controls visually bold while allowing the properties
        # column to fit on a 1080p desktop without vertical scrolling.
        self.set_visual_scale(
            extent_ratio=0.98, margin=8.0,
            handle_scale=1.50, line_scale=1.35)

    @property
    def directions(self) -> tuple[tuple[int, int, int], ...]:
        return (
            (0, 0, 1), (0, 1, 0), (1, 0, 0),
            (0, 0, -1), (0, -1, 0), (-1, 0, 0),
        )

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(320, 175)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(260, 165)


class RadiusItemDelegate(QStyledItemDelegate):
    """Compact whole-number editor for the Radius column only."""

    def createEditor(self, parent, option, index):  # noqa: N802
        if index.column() != 1:
            return None
        editor = QSpinBox(parent)
        editor.setRange(1, 1_000_000)
        editor.setSingleStep(1)
        editor.setFrame(False)
        return editor

    def setEditorData(self, editor, index):  # noqa: N802
        if isinstance(editor, QSpinBox):
            try:
                value = int(round(float(index.data())))
            except (TypeError, ValueError):
                value = 1
            editor.setValue(max(1, value))
            return
        super().setEditorData(editor, index)

    def setModelData(self, editor, model, index):  # noqa: N802
        if isinstance(editor, QSpinBox):
            model.setData(index, str(editor.value()), Qt.ItemDataRole.EditRole)
            return
        super().setModelData(editor, model, index)


class CompactScaleSpinBox(QDoubleSpinBox):
    """Keep scale precision while hiding meaningless trailing zeroes."""

    def textFromValue(self, value: float) -> str:  # noqa: N802
        text = self.locale().toString(value, "f", self.decimals())
        decimal_point = self.locale().decimalPoint()
        if decimal_point in text:
            text = text.rstrip("0").rstrip(decimal_point)
        return text


class CollisionViewport(AssetViewport):
    """Existing AssetViewport plus the exact F10 three-ring overlay."""

    spherePicked = Signal(int)
    firePointPicked = Signal(int)
    tracerPointPicked = Signal(int)
    sphereContextMenuRequested = Signal(int, QPoint)
    sphereNudgeRequested = Signal(object)
    RING_SEGMENTS = 12
    SELECTED_HALO_WIDTH = 6.0
    SELECTED_COLOR_WIDTH = 3.4

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._collision_spheres: list[CollisionSphere] = []
        self._collision_selected = -1
        self._collision_show = {
            LEGACY: True, VEHICLE: True, WEAPON: True,
        }
        self._collision_show_model = True
        self._pick_cycle_point: QPointF | None = None
        self._pick_cycle_candidates: tuple[int, ...] = ()
        self._pick_cycle_offset = -1
        self._model_preview_scale = (1.0, 1.0, 1.0)
        self._ground_alignment_available = True
        self._ground_alignment_authored = False
        self._ground_alignment_source_loaded = False
        self._overeof = 0.0
        self._ground_simulation_visible = True
        self._overeof_visible = True
        self._fire_points: list[tuple[float, float, float]] = []
        self._fire_point_selected = -1
        self._fire_points_visible = True
        self._tracer_points: list[tuple[float, float, float]] = []
        self._tracer_point_selected = -1
        self._tracer_points_visible = True
        self._model_preview_base_faces: list[
            tuple[tuple[float, float, float], ...]] = []
        self._model_preview_base_sen_boxes: list[
            tuple[tuple[float, float, float], ...]] = []
        self._model_preview_base_owner_bounds: dict[str, tuple] = {}

    @property
    def model_preview_scale(self) -> tuple[float, float, float]:
        return self._model_preview_scale

    @property
    def overeof_preview_offset(self) -> float:
        """World-Y translation matching ``ground_y - overeof``."""

        if not (self._ground_alignment_available
                and self._ground_alignment_authored):
            return 0.0
        return -self._overeof

    @property
    def overeof_value(self) -> float:
        return self._overeof

    def clear(self) -> None:
        super().clear()
        self._ground_alignment_source_loaded = False
        self._fire_points = []
        self._fire_point_selected = -1
        self._collision_selected = -1
        self._tracer_points = []
        self._tracer_point_selected = -1
        self._model_preview_base_faces = []
        self._model_preview_base_sen_boxes = []
        self._model_preview_base_owner_bounds = {}

    def load_family(self, family, visible_owners=None, keep_camera=False,
                    primary_owner=None) -> None:
        """Load through AssetViewport, then apply a non-destructive scale.

        The base viewport already resolves BASE/SKLT transforms, children and
        textures.  Collision Editor only adds one final global axis scale,
        matching the visual effect of vp_scale_x/y/z while leaving collision
        sphere coordinates untouched.
        """

        super().load_family(
            family, visible_owners, keep_camera=keep_camera,
            primary_owner=primary_owner)
        self._ground_alignment_source_loaded = True
        self._capture_unscaled_model_geometry()
        self._apply_model_preview_scale()

    def _capture_unscaled_model_geometry(self) -> None:
        self._model_preview_base_faces = [
            tuple(tuple(vertex) for vertex in face.vertices)
            for face in self._faces
        ]
        self._model_preview_base_sen_boxes = [
            tuple(tuple(point) for point in box)
            for box in self._sen_boxes
        ]
        self._model_preview_base_owner_bounds = dict(self._owner_bounds)

    @staticmethod
    def _scaled_point(point, scale):
        return (
            point[0] * scale[0],
            point[1] * scale[1],
            point[2] * scale[2],
        )

    def _preview_point(self, point):
        scaled = self._scaled_point(point, self._model_preview_scale)
        return (
            scaled[0],
            scaled[1] + self.overeof_preview_offset,
            scaled[2],
        )

    def local_scaled_owner_bounds(self, owner: str | None):
        """Return scaled model-space bounds without the visual ground offset.

        Collision coordinates are authored relative to the actor origin.
        Suggested spheres and validation must therefore ignore the temporary
        world placement produced by ``overeof``.
        """

        bounds = self._model_preview_base_owner_bounds.get(owner)
        if bounds is None:
            return None
        x0, y0, z0, x1, y1, z1 = bounds
        scale = self._model_preview_scale
        a = self._scaled_point((x0, y0, z0), scale)
        b = self._scaled_point((x1, y1, z1), scale)
        return (
            min(a[0], b[0]), min(a[1], b[1]), min(a[2], b[2]),
            max(a[0], b[0]), max(a[1], b[1]), max(a[2], b[2]),
        )

    def _apply_model_preview_scale(self) -> None:
        if len(self._model_preview_base_faces) == len(self._faces):
            for face, vertices in zip(
                    self._faces, self._model_preview_base_faces):
                face.vertices = [
                    self._preview_point(vertex) for vertex in vertices
                ]
        if len(self._model_preview_base_sen_boxes) == len(self._sen_boxes):
            self._sen_boxes = [
                [self._preview_point(point) for point in box]
                for box in self._model_preview_base_sen_boxes
            ]
        preview_bounds = {}
        offset_y = self.overeof_preview_offset
        for owner in self._model_preview_base_owner_bounds:
            bounds = self.local_scaled_owner_bounds(owner)
            if bounds is None:
                continue
            x0, y0, z0, x1, y1, z1 = bounds
            preview_bounds[owner] = (
                x0, y0 + offset_y, z0,
                x1, y1 + offset_y, z1,
            )
        self._owner_bounds = preview_bounds
        self.update()

    def set_model_preview_scale(
            self, x: float, y: float, z: float) -> None:
        values = tuple(float(value) for value in (x, y, z))
        if not all(math.isfinite(value) and value >= 0.0 for value in values):
            return
        self._model_preview_scale = values
        self._apply_model_preview_scale()

    def set_ground_alignment(
            self, available: bool, authored: bool, overeof: float) -> None:
        value = float(overeof)
        if not math.isfinite(value):
            return
        self._ground_alignment_available = bool(available)
        self._ground_alignment_authored = bool(authored)
        self._overeof = value
        self._apply_model_preview_scale()

    def set_ground_simulation_visible(self, visible: bool) -> None:
        self._ground_simulation_visible = bool(visible)
        self.update()

    def set_overeof_visible(self, visible: bool) -> None:
        self._overeof_visible = bool(visible)
        self.update()

    def set_fire_points(
            self, points: list[tuple[float, float, float]],
            selected: int = -1) -> None:
        self._fire_points = [
            tuple(float(value) for value in point) for point in points
        ]
        self._fire_point_selected = (
            selected if 0 <= selected < len(self._fire_points) else -1)
        self.update()

    def set_fire_points_visible(self, visible: bool) -> None:
        self._fire_points_visible = bool(visible)
        self.update()

    def set_tracer_points(
            self, points: list[tuple[float, float, float]],
            selected: int = -1) -> None:
        self._tracer_points = [
            tuple(float(value) for value in point) for point in points
        ]
        self._tracer_point_selected = (
            selected if 0 <= selected < len(self._tracer_points) else -1)
        self.update()

    def set_tracer_points_visible(self, visible: bool) -> None:
        self._tracer_points_visible = bool(visible)
        self.update()

    def set_collision_spheres(
        self, spheres: list[CollisionSphere], selected: int = -1,
    ) -> None:
        self._collision_spheres = list(spheres)
        self._collision_selected = selected
        self.update()

    def set_collision_category_visible(self, category: str, visible: bool):
        self._collision_show[category] = bool(visible)
        self.update()

    def set_model_visible(self, visible: bool):
        self._collision_show_model = bool(visible)
        self.update()

    def set_textures_visible(self, visible: bool):
        self.set_mode("textured" if visible else "solid")

    def _ground_grid_spec(self) -> tuple[float, float, float]:
        size = 1.4 / max(abs(self._scale), 1e-9)
        return self._center[0], self._center[2], size

    def _draw_grid(self, painter: QPainter, target: QRectF, camera: dict) -> None:
        """Draw a fixed Y=0 ground plane instead of a model-centered grid."""

        if not (self._ground_alignment_available
                and self._ground_alignment_source_loaded
                and self._ground_simulation_visible):
            return
        center_x, center_z, size = self._ground_grid_spec()
        corners_world = (
            (center_x - size, 0.0, center_z - size),
            (center_x + size, 0.0, center_z - size),
            (center_x + size, 0.0, center_z + size),
            (center_x - size, 0.0, center_z + size),
        )
        corners = QPolygonF([
            self._project(self._camera_vertex(point), target, camera)
            for point in corners_world
        ])
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(34, 118, 132, 42))
        painter.drawPolygon(corners)

        steps = 8
        for i in range(-steps, steps + 1):
            offset = i * size / steps
            major = i == 0
            painter.setPen(QPen(
                QColor(70, 170, 180, 150 if major else 86),
                1.25 if major else 0.8,
            ))
            for start, end in (
                ((center_x + offset, 0.0, center_z - size),
                 (center_x + offset, 0.0, center_z + size)),
                ((center_x - size, 0.0, center_z + offset),
                 (center_x + size, 0.0, center_z + offset)),
            ):
                a = self._project(
                    self._camera_vertex(start), target, camera)
                b = self._project(
                    self._camera_vertex(end), target, camera)
                painter.drawLine(a, b)

    def _is_sphere_drawn(self, sphere: CollisionSphere) -> bool:
        return (
            sphere.visible and self._collision_show.get(sphere.category, True)
            and sphere.radius > 0 and math.isfinite(sphere.radius)
        )

    def _ring_world_points(
            self, sphere: CollisionSphere, axis: int
    ) -> list[tuple[float, float, float]]:
        points = []
        for step in range(self.RING_SEGMENTS + 1):
            angle = 2.0 * math.pi * step / self.RING_SEGMENTS
            ca = math.cos(angle) * sphere.radius
            sa = math.sin(angle) * sphere.radius
            if axis == 0:
                world = (sphere.x + ca, sphere.y + sa, sphere.z)
            elif axis == 1:
                world = (sphere.x + ca, sphere.y, sphere.z + sa)
            else:
                world = (sphere.x, sphere.y + ca, sphere.z + sa)
            points.append(world)
        return points

    def _ring_points(self, sphere: CollisionSphere, axis: int):
        # Public/test helper retains the historical projected-point contract.
        return [
            self._project(self._camera_vertex(world))
            for world in self._ring_world_points(sphere, axis)
        ]

    @staticmethod
    def _camera_point_is_projectable(camera_point) -> bool:
        # AssetViewport uses a camera distance of 4 and clamps its denominator
        # at 0.2. OpenUA F10 instead drops points behind the near plane.
        # Applying the equivalent rejection here prevents large AoE spheres
        # from producing diagonal lines across the entire viewport.
        return 4.0 - float(camera_point[2]) > 0.2

    def _project_visible_world(self, world):
        camera_point = self._camera_vertex(world)
        if not self._camera_point_is_projectable(camera_point):
            return None
        screen = self._project(camera_point)
        # OpenUA F10 also rejects projected points beyond a small screen
        # margin, then restarts the ring at the next valid point. Without
        # this guard, very large AoE radii can still draw long cross-screen
        # chords even though the world-space radius itself is correct.
        margin = 64.0
        if not (-margin <= screen.x() <= self.width() + margin
                and -margin <= screen.y() <= self.height() + margin):
            return None
        return screen

    def _screen_radius(self, sphere: CollisionSphere) -> float:
        center = self._project(self._camera_vertex(sphere.center))
        samples = [
            self._project(self._camera_vertex((
                sphere.x + sphere.radius, sphere.y, sphere.z))),
            self._project(self._camera_vertex((
                sphere.x, sphere.y + sphere.radius, sphere.z))),
            self._project(self._camera_vertex((
                sphere.x, sphere.y, sphere.z + sphere.radius))),
        ]
        return max(
            math.hypot(point.x() - center.x(), point.y() - center.y())
            for point in samples)

    def _sphere_hit_candidates(self, point: QPointF) -> list[int]:
        candidates: list[tuple[float, float, int]] = []
        for index, sphere in enumerate(self._collision_spheres):
            if not self._is_sphere_drawn(sphere):
                continue
            center = self._project(self._camera_vertex(sphere.center))
            center_distance = math.hypot(
                point.x() - center.x(), point.y() - center.y())
            screen_radius = self._screen_radius(sphere)
            ring_distance = math.inf
            for axis in range(3):
                points = self._ring_points(sphere, axis)
                for start, end in zip(points, points[1:]):
                    ring_distance = min(
                        ring_distance,
                        _point_segment_distance(point, start, end),
                    )
            tolerance = max(12.0, min(24.0, 10.0 + screen_radius * 0.08))
            center_tolerance = max(
                14.0, min(24.0, 11.0 + screen_radius * 0.10))
            center_hit = center_distance <= center_tolerance
            if ring_distance <= tolerance or center_hit:
                score = min(
                    ring_distance,
                    center_distance if center_hit else math.inf,
                )
                depth = self._camera_vertex(sphere.center)[2]
                candidates.append((score, -depth, index))
        candidates.sort()
        return [item[2] for item in candidates]

    def _hit_sphere(self, point: QPointF, *, cycle: bool = True) -> int:
        candidates = tuple(self._sphere_hit_candidates(point))
        if not candidates:
            self._pick_cycle_point = None
            self._pick_cycle_candidates = ()
            self._pick_cycle_offset = -1
            return -1
        if not cycle:
            return candidates[0]
        same_point = self._pick_cycle_point is not None and math.hypot(
            point.x() - self._pick_cycle_point.x(),
            point.y() - self._pick_cycle_point.y(),
        ) <= 6.0
        if same_point and candidates == self._pick_cycle_candidates:
            self._pick_cycle_offset = (
                self._pick_cycle_offset + 1) % len(candidates)
        else:
            self._pick_cycle_point = QPointF(point)
            self._pick_cycle_candidates = candidates
            self._pick_cycle_offset = 0
        return candidates[self._pick_cycle_offset]

    def _draw_mode_label(self, *_args) -> None:
        """Collision assets stay in View Mode without a mode overlay."""

        self._mode_label_rect = None

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Tab:
            event.accept()
            return
        if (self._collision_selected >= 0
                or self._fire_point_selected >= 0
                or self._tracer_point_selected >= 0) and not (
                event.modifiers() & (
                    Qt.KeyboardModifier.ControlModifier
                    | Qt.KeyboardModifier.AltModifier
                    | Qt.KeyboardModifier.MetaModifier)):
            directions = {
                Qt.Key.Key_Left: (-1, 0, 0),
                Qt.Key.Key_Right: (1, 0, 0),
                Qt.Key.Key_Up: (0, 0, -1),
                Qt.Key.Key_Down: (0, 0, 1),
                Qt.Key.Key_PageUp: (0, -1, 0),
                Qt.Key.Key_PageDown: (0, 1, 0),
            }
            direction = directions.get(event.key())
            if direction is not None:
                self.sphereNudgeRequested.emit(direction)
                event.accept()
                return
        super().keyPressEvent(event)

    def event(self, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.KeyPress \
                and event.key() == Qt.Key.Key_Tab:
            event.accept()
            return True
        return super().event(event)

    def _draw_collision_rings(
        self, painter: QPainter, sphere: CollisionSphere, pen: QPen,
    ) -> None:
        painter.setPen(pen)
        for axis in range(3):
            projected = [
                self._project_visible_world(world)
                for world in self._ring_world_points(sphere, axis)
            ]
            for start, end in zip(projected, projected[1:]):
                # Match OpenUA's F10 conversion: a segment is emitted only
                # when both endpoints survive near-plane projection.
                if start is not None and end is not None:
                    painter.drawLine(start, end)

    def _draw_ground_alignment_overlay(self, painter: QPainter) -> None:
        if not (self._ground_alignment_available
                and self._ground_alignment_source_loaded):
            return
        camera = self._camera_state()
        target = QRectF(self.rect())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self._ground_simulation_visible:
            _center_x, _center_z, size = self._ground_grid_spec()
            label_world = (
                self._center[0] - size, 0.0, self._center[2] - size)
            label_point = self._project(
                self._camera_vertex(label_world), target, camera)
            painter.setPen(QPen(QColor(125, 225, 230, 230), 1.2))
            painter.drawText(
                label_point + QPointF(8.0, -8.0),
                "Ground Simulation / Y = 0",
            )

        if not (self._ground_alignment_authored and self._overeof_visible):
            return
        origin_world = (0.0, self.overeof_preview_offset, 0.0)
        ground_world = (0.0, 0.0, 0.0)
        origin = self._project(
            self._camera_vertex(origin_world), target, camera)
        ground = self._project(
            self._camera_vertex(ground_world), target, camera)
        pen = QPen(QColor(255, 190, 80, 235), 2.2)
        pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.drawLine(origin, ground)
        painter.setBrush(QColor(255, 190, 80, 235))
        painter.drawEllipse(origin, 4.0, 4.0)
        painter.drawEllipse(ground, 4.0, 4.0)
        midpoint = QPointF(
            (origin.x() + ground.x()) * 0.5,
            (origin.y() + ground.y()) * 0.5,
        )
        painter.drawText(
            midpoint + QPointF(7.0, -6.0),
            f"overeof = {_number(self._overeof)}",
        )

    def _fire_point_screen_positions(self):
        return [self._project_visible_world(point) for point in self._fire_points]

    def _tracer_point_screen_positions(self):
        return [
            self._project_visible_world(point) for point in self._tracer_points
        ]

    def _hit_fire_point(self, point: QPointF) -> int:
        if not (self._ground_alignment_source_loaded
                and self._fire_points_visible):
            return -1
        candidates = []
        for index, screen in enumerate(self._fire_point_screen_positions()):
            if screen is None:
                continue
            distance = math.hypot(point.x() - screen.x(), point.y() - screen.y())
            if distance <= 16.0:
                candidates.append((distance, index))
        return min(candidates)[1] if candidates else -1

    def _hit_tracer_point(self, point: QPointF) -> int:
        if not (self._ground_alignment_source_loaded
                and self._tracer_points_visible):
            return -1
        candidates = []
        for index, screen in enumerate(self._tracer_point_screen_positions()):
            if screen is None:
                continue
            distance = math.hypot(point.x() - screen.x(), point.y() - screen.y())
            if distance <= 13.0:
                candidates.append((distance, index))
        return min(candidates)[1] if candidates else -1

    @staticmethod
    def _draw_fire_marker(
            painter: QPainter, screen: QPointF, pen: QPen,
            brush: QColor, extent: float) -> None:
        painter.setPen(pen)
        painter.setBrush(brush)
        painter.drawEllipse(screen, extent * 0.6, extent * 0.6)
        painter.drawLine(
            QPointF(screen.x() - extent, screen.y()),
            QPointF(screen.x() + extent, screen.y()))
        painter.drawLine(
            QPointF(screen.x(), screen.y() - extent),
            QPointF(screen.x(), screen.y() + extent))

    def _draw_fire_points_overlay(self, painter: QPainter) -> None:
        if not (self._ground_alignment_source_loaded
                and self._fire_points_visible and self._fire_points):
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        fill = QColor(
            FIRE_POINT_COLOR.red(), FIRE_POINT_COLOR.green(),
            FIRE_POINT_COLOR.blue(), 225)
        rack_selected = (
            self._fire_point_selected >= 0 and len(self._fire_points) > 1)
        for index, screen in enumerate(self._fire_point_screen_positions()):
            if screen is None:
                continue
            is_primary = index == self._fire_point_selected
            if is_primary or rack_selected:
                self._draw_fire_marker(
                    painter, screen,
                    QPen(QColor(255, 255, 255),
                         6.0 if is_primary else 4.5),
                    QColor(255, 255, 255, 235),
                    11.0 if is_primary else 10.5)
            self._draw_fire_marker(
                painter, screen, QPen(FIRE_POINT_COLOR, 2.4), fill, 10.0)
            painter.setPen(QPen(
                QColor(255, 255, 255)
                if (is_primary or rack_selected) else FIRE_POINT_COLOR,
                1.5))
            painter.drawText(
                screen + QPointF(9.0, -9.0), f"F{index + 1}")

    def _draw_tracer_points_overlay(self, painter: QPainter) -> None:
        if not (self._ground_alignment_source_loaded
                and self._tracer_points_visible and self._tracer_points):
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for index, screen in enumerate(self._tracer_point_screen_positions()):
            if screen is None:
                continue
            selected = index == self._tracer_point_selected
            if selected:
                painter.setPen(QPen(QColor(255, 255, 255), 2.5))
                painter.setBrush(QColor(255, 255, 255, 235))
                painter.drawEllipse(screen, 8.0, 8.0)
            painter.setPen(QPen(TRACER_POINT_COLOR, 1.8))
            painter.setBrush(TRACER_POINT_COLOR)
            painter.drawEllipse(screen, 5.0, 5.0)
            painter.setPen(QPen(
                QColor(255, 255, 255) if selected else TRACER_POINT_COLOR,
                1.3))
            painter.drawText(screen + QPointF(8.0, -7.0), f"T{index + 1}")

    def paintEvent(self, event) -> None:  # noqa: N802
        if self._collision_show_model:
            super().paintEvent(event)
        else:
            painter = QPainter(self)
            painter.fillRect(self.rect(), QColor(24, 26, 32))
            if self._show_grid:
                self._draw_grid(
                    painter, QRectF(self.rect()), self._camera_state())
            painter.setPen(QColor(130, 135, 145))
            painter.drawText(
                QRectF(self.rect()), Qt.AlignmentFlag.AlignCenter,
                "Model hidden",
            )
            painter.end()

        painter = QPainter(self)
        self._draw_ground_alignment_overlay(painter)
        # OpenUA F10 uses unfilled, aliased one-pixel lines and 12 segments.
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        selected_pair = None
        for index, sphere in enumerate(self._collision_spheres):
            if not self._is_sphere_drawn(sphere):
                continue
            if index == self._collision_selected:
                selected_pair = (index, sphere)
                continue
            color = TYPE_COLORS[sphere.category]
            self._draw_collision_rings(
                painter, sphere, QPen(color, 1.0))
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        if selected_pair is not None:
            _index, sphere = selected_pair
            color = TYPE_COLORS[sphere.category]
            self._draw_collision_rings(
                painter, sphere, QPen(
                    QColor(255, 255, 255), self.SELECTED_HALO_WIDTH))
            self._draw_collision_rings(
                painter, sphere, QPen(
                    color, self.SELECTED_COLOR_WIDTH))
            center = self._project(self._camera_vertex(sphere.center))
            painter.setPen(QPen(QColor(255, 255, 255), 2.0))
            painter.setBrush(QColor(
                color.red(), color.green(), color.blue(), 235))
            painter.drawEllipse(center, 6.0, 6.0)
        self._draw_tracer_points_overlay(painter)
        self._draw_fire_points_overlay(painter)
        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            tracer_index = self._hit_tracer_point(event.position())
            if tracer_index >= 0:
                self.setFocus(Qt.FocusReason.MouseFocusReason)
                self._tracer_point_selected = tracer_index
                self._fire_point_selected = -1
                self._collision_selected = -1
                self._camera_interacting = False
                self._press_pos = None
                self._last_mouse = event.position().toPoint()
                self.tracerPointPicked.emit(tracer_index)
                self.update()
                event.accept()
                return
            fire_index = self._hit_fire_point(event.position())
            if fire_index >= 0:
                self.setFocus(Qt.FocusReason.MouseFocusReason)
                self._fire_point_selected = fire_index
                self._tracer_point_selected = -1
                self._collision_selected = -1
                self._camera_interacting = False
                self._press_pos = None
                self._last_mouse = event.position().toPoint()
                self.firePointPicked.emit(fire_index)
                self.update()
                event.accept()
                return
            index = self._hit_sphere(event.position())
            if index >= 0:
                self.setFocus(Qt.FocusReason.MouseFocusReason)
                self._collision_selected = index
                self._fire_point_selected = -1
                self._tracer_point_selected = -1
                self._camera_interacting = False
                self._press_pos = None
                self._last_mouse = event.position().toPoint()
                self.spherePicked.emit(index)
                self.update()
                event.accept()
                return
        if event.button() == Qt.MouseButton.RightButton:
            tracer_index = self._hit_tracer_point(event.position())
            if tracer_index >= 0:
                self._tracer_point_selected = tracer_index
                self._fire_point_selected = -1
                self._collision_selected = -1
                self.tracerPointPicked.emit(tracer_index)
                self.update()
                self.sphereContextMenuRequested.emit(
                    -1, event.globalPosition().toPoint())
                event.accept()
                return
            fire_index = self._hit_fire_point(event.position())
            if fire_index >= 0:
                self._fire_point_selected = fire_index
                self._tracer_point_selected = -1
                self._collision_selected = -1
                self.firePointPicked.emit(fire_index)
                self.update()
                self.sphereContextMenuRequested.emit(
                    -1, event.globalPosition().toPoint())
                event.accept()
                return
            index = self._hit_sphere(event.position(), cycle=False)
            if index >= 0:
                self._collision_selected = index
                self._fire_point_selected = -1
                self._tracer_point_selected = -1
                self.spherePicked.emit(index)
                self.update()
            self.sphereContextMenuRequested.emit(
                index, event.globalPosition().toPoint())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            tracer_index = self._hit_tracer_point(event.position())
            if tracer_index >= 0:
                self._tracer_point_selected = tracer_index
                self._fire_point_selected = -1
                self._collision_selected = -1
                self.tracerPointPicked.emit(tracer_index)
                self.update()
                event.accept()
                return
            fire_index = self._hit_fire_point(event.position())
            if fire_index >= 0:
                self._fire_point_selected = fire_index
                self._tracer_point_selected = -1
                self._collision_selected = -1
                self.firePointPicked.emit(fire_index)
            else:
                index = self._hit_sphere(event.position(), cycle=False)
                self._collision_selected = index
                self._fire_point_selected = -1
                self._tracer_point_selected = -1
                self.spherePicked.emit(index)
                if index < 0:
                    self.firePointPicked.emit(-1)
                    self.tracerPointPicked.emit(-1)
            self.update()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class ImportCollisionDialog(QDialog):
    def __init__(self, parent, path: Path, text: str) -> None:
        super().__init__(parent)
        self.setWindowTitle("Import Collision Text")
        self.resize(620, 390)
        self.path = path
        self.text = text
        self.blocks = find_script_blocks(text)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(str(path)))
        form = QFormLayout()
        self.block_combo = QComboBox()
        for block in self.blocks:
            self.block_combo.addItem(block.label, block)
        self.category_combo = QComboBox()
        self.category_combo.addItem("Vehicle Collision (green)", VEHICLE)
        self.category_combo.addItem("Weapon Collision (blue)", WEAPON)
        form.addRow("Definition", self.block_combo)
        form.addRow("Interpret coll_* as", self.category_combo)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Open
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(
            QDialogButtonBox.StandardButton.Open).setText("Import")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected(self):
        return (
            self.block_combo.currentData(),
            self.category_combo.currentData(),
        )


class OpenScriptObjectDialog(QDialog):
    """Choose one vehicle/weapon definition and the SET.BAS VP database."""

    def __init__(
            self, parent, script_path: Path,
            references: list[VehicleModelReference],
            current_set_bas: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Import Vehicle / Weapon from Script")
        self.resize(720, 300)
        self._all_references = list(references)
        layout = QVBoxLayout(self)
        path_label = QLabel(str(script_path))
        path_label.setWordWrap(True)
        layout.addWidget(path_label)
        form = QGridLayout()
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(6)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search by name or numeric ID")
        self.block_combo = QComboBox()
        self.vp_label = QLabel("—")
        self.set_path_edit = QLineEdit()
        self.set_path_edit.setPlaceholderText("Select the SET.BAS VP database")
        if current_set_bas is not None:
            self.set_path_edit.setText(str(current_set_bas))
        browse = QPushButton("Browse")
        browse.clicked.connect(self._browse_set)
        form.addWidget(QLabel("Search"), 0, 0)
        form.addWidget(self.search_edit, 0, 1, 1, 2)
        form.addWidget(QLabel("Definition"), 1, 0)
        form.addWidget(self.block_combo, 1, 1, 1, 2)
        form.addWidget(QLabel("Resolved VP"), 2, 0)
        form.addWidget(self.vp_label, 2, 1, 1, 2)
        form.addWidget(QLabel("SET.BAS database"), 3, 0)
        form.addWidget(self.set_path_edit, 3, 1)
        form.addWidget(browse, 3, 2)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Open
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(
            QDialogButtonBox.StandardButton.Open).setText("Import")
        self.open_button = buttons.button(
            QDialogButtonBox.StandardButton.Open)
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.search_edit.textChanged.connect(self._filter_references)
        self.block_combo.currentIndexChanged.connect(self._sync_reference)
        self._filter_references()

    def _browse_set(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select SET.BAS VP database",
            self.set_path_edit.text(),
            "SET.BAS archive (SET.BAS *.bas *.BAS);;All files (*)")
        if path:
            self.set_path_edit.setText(path)

    def _filter_references(self, *_args):
        query = self.search_edit.text().strip().casefold()
        terms = [part for part in query.split() if part]
        current = self.block_combo.currentData()
        with QSignalBlocker(self.block_combo):
            self.block_combo.clear()
            for reference in self._all_references:
                block = reference.block
                haystack = (
                    f"{block.kind} {block.object_id} {block.name} "
                    f"vp_normal {reference.vp_normal}"
                ).casefold()
                if query.isdigit():
                    if block.object_id != int(query):
                        continue
                elif terms and not all(term in haystack for term in terms):
                    continue
                self.block_combo.addItem(reference.label, reference)
            if current is not None:
                for index in range(self.block_combo.count()):
                    candidate = self.block_combo.itemData(index)
                    if candidate == current:
                        self.block_combo.setCurrentIndex(index)
                        break
        self._sync_reference()

    def _sync_reference(self, *_args):
        reference = self.block_combo.currentData()
        self.vp_label.setText(
            f"vp_normal = {reference.vp_normal}"
            if reference is not None else "No matching definition")
        self.open_button.setEnabled(reference is not None)

    def _accept_if_valid(self):
        if self.block_combo.currentData() is None:
            QMessageBox.warning(
                self, "Definition required",
                "Choose a vehicle or weapon definition first.")
            return
        path = Path(self.set_path_edit.text())
        if not path.is_file():
            QMessageBox.warning(
                self, "SET.BAS required",
                "Select the SET.BAS archive that defines the selected VP.")
            return
        self.accept()

    def selected(self) -> tuple[VehicleModelReference, Path]:
        return self.block_combo.currentData(), Path(self.set_path_edit.text())


# Compatibility alias for integrations importing the previous class name.
OpenVehicleScriptDialog = OpenScriptObjectDialog


class ApplyScriptDialog(QDialog):
    def __init__(
            self, parent, project: CollisionProject, *,
            replace_all_managed: bool = False,
            initial_path: str | Path | None = None,
            initial_kind: str | None = None,
            initial_id: int | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Apply to Existing Script")
        self.resize(820, 620)
        self.project = project
        self.updated_text = ""
        self.backup_path: Path | None = None
        self._source_text = ""
        self._source_encoding = "utf-8"
        self._source_bom = False
        self.replace_all_managed = bool(replace_all_managed)
        self._script_blocks: list[ScriptBlock] = []
        layout = QVBoxLayout(self)
        form = QGridLayout()
        self.path_edit = QLineEdit()
        browse = QPushButton("Browse")
        browse.clicked.connect(self._browse)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search by name or numeric ID")
        self.block_combo = QComboBox()
        self.kind_combo = QComboBox()
        self.kind_combo.addItems(SCRIPT_TYPES)
        default_kind = (
            "modify_weapon" if project.target_category == WEAPON
            else "modify_vehicle")
        self.kind_combo.setCurrentText(default_kind)
        self.id_spin = QSpinBox()
        self.id_spin.setRange(0, 65535)
        self.detected_name = QLabel("—")
        self.comment_missing = QCheckBox(
            "Comment existing collision data when absent in the project")
        self.comment_missing.setChecked(True)
        if self.replace_all_managed:
            self.setWindowTitle("Overwrite Loaded Definition")
            self.comment_missing.setText(
                "Replace all active Collision Editor parameters with the "
                "current values")
            self.comment_missing.setChecked(True)
            self.comment_missing.setEnabled(False)
            self.comment_missing.setToolTip(
                "The loaded vehicle block is rewritten canonically only for "
                "parameters managed by Collision Editor. Unrelated script "
                "data and comments are preserved.")
        form.addWidget(QLabel("Script"), 0, 0)
        form.addWidget(self.path_edit, 0, 1)
        form.addWidget(browse, 0, 2)
        form.addWidget(QLabel("Search"), 1, 0)
        form.addWidget(self.search_edit, 1, 1, 1, 2)
        form.addWidget(QLabel("Definition"), 2, 0)
        form.addWidget(self.block_combo, 2, 1, 1, 2)
        form.addWidget(QLabel("Block type"), 3, 0)
        form.addWidget(self.kind_combo, 3, 1, 1, 2)
        form.addWidget(QLabel("Numeric ID"), 4, 0)
        form.addWidget(self.id_spin, 4, 1, 1, 2)
        form.addWidget(QLabel("Detected name"), 5, 0)
        form.addWidget(self.detected_name, 5, 1, 1, 2)
        form.addWidget(self.comment_missing, 6, 0, 1, 3)
        layout.addLayout(form)
        layout.addWidget(QLabel(
            "Preview (only supported spatial/collision parameters may "
            "change)"))
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        layout.addWidget(self.preview, 1)
        self.error_label = QLabel()
        self.error_label.setStyleSheet("color: #e06060")
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply
            | QDialogButtonBox.StandardButton.Cancel)
        self.apply_button = self.buttons.button(
            QDialogButtonBox.StandardButton.Apply)
        self.apply_button.setEnabled(False)
        # Apply has ApplyRole, so QDialogButtonBox.accepted is not emitted
        # for it.  Connect the actual button or the dialog only previews and
        # never writes the selected script.
        self.apply_button.clicked.connect(self._apply)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.search_edit.textChanged.connect(self._filter_script_blocks)
        self.block_combo.currentIndexChanged.connect(
            self._sync_selected_script_block)
        for widget_signal in (
            self.path_edit.textChanged,
            self.kind_combo.currentTextChanged,
            self.id_spin.valueChanged,
            self.comment_missing.toggled,
        ):
            widget_signal.connect(self.refresh_preview)
        if initial_path is not None:
            self.path_edit.setText(str(initial_path))
        if initial_kind is not None:
            self.kind_combo.setCurrentText(initial_kind)
        if initial_id is not None:
            self.id_spin.setValue(int(initial_id))
        self._load_script_blocks()
        self.refresh_preview()

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select OpenUA script", "",
            "Text scripts (*.txt *.ini);;All files (*)")
        if path:
            self.path_edit.setText(path)

    def _load_script_blocks(self, *_args) -> None:
        path = Path(self.path_edit.text())
        self._script_blocks = []
        if path.is_file():
            try:
                source_text, _encoding, _bom = read_script_file(path)
                self._script_blocks = find_script_blocks(source_text)
            except (OSError, UnicodeError):
                pass
        self._filter_script_blocks(refresh=False)

    def _filter_script_blocks(self, *_args, refresh: bool = True) -> None:
        query = self.search_edit.text().strip().casefold()
        terms = [part for part in query.split() if part]
        current = self.block_combo.currentData()
        preferred = (
            self.kind_combo.currentText().casefold(), self.id_spin.value())
        matches = []
        for block in self._script_blocks:
            haystack = (
                f"{block.kind} {block.object_id} {block.name}"
            ).casefold()
            if query.isdigit():
                if block.object_id != int(query):
                    continue
            elif terms and not all(term in haystack for term in terms):
                continue
            matches.append(block)
        with QSignalBlocker(self.block_combo):
            self.block_combo.clear()
            for block in matches:
                self.block_combo.addItem(block.label, block)
            selected = None
            for index, block in enumerate(matches):
                if (block.kind.casefold(), block.object_id) == preferred:
                    selected = index
                    break
            if selected is None and current in matches:
                selected = matches.index(current)
            if selected is None and matches:
                selected = 0
            if selected is not None:
                self.block_combo.setCurrentIndex(selected)
        block = self.block_combo.currentData()
        if block is not None and (
                self.kind_combo.currentText().casefold() != block.kind.casefold()
                or self.id_spin.value() != block.object_id):
            with QSignalBlocker(self.kind_combo), QSignalBlocker(self.id_spin):
                self.kind_combo.setCurrentText(block.kind)
                self.id_spin.setValue(block.object_id)
        if refresh:
            self.refresh_preview()

    def _sync_selected_script_block(self, *_args) -> None:
        block = self.block_combo.currentData()
        if block is None:
            self.refresh_preview()
            return
        with QSignalBlocker(self.kind_combo), QSignalBlocker(self.id_spin):
            self.kind_combo.setCurrentText(block.kind)
            self.id_spin.setValue(block.object_id)
        self.refresh_preview()

    def refresh_preview(self, *_args):
        self.error_label.clear()
        self.preview.clear()
        self.apply_button.setEnabled(False)
        self._load_script_blocks()
        path = Path(self.path_edit.text())
        if not path.is_file():
            self.detected_name.setText("—")
            return
        try:
            (self._source_text, self._source_encoding,
             self._source_bom) = read_script_file(path)
            updated, preview, name = plan_script_update(
                self._source_text,
                self.kind_combo.currentText(),
                self.id_spin.value(),
                self.project,
                comment_missing=self.comment_missing.isChecked(),
                replace_all_managed=self.replace_all_managed,
            )
        except (OSError, UnicodeError, CollisionScriptError) as exc:
            self.detected_name.setText("—")
            self.error_label.setText(str(exc))
            return
        self.updated_text = updated
        self.detected_name.setText(name or "(name not present)")
        self.preview.setPlainText(preview or "(No changes)")
        self.apply_button.setEnabled(bool(preview))

    def _apply(self):
        path = Path(self.path_edit.text())
        try:
            self.backup_path = create_backup(path)
            write_script_file(
                path, self.updated_text,
                self._source_encoding, self._source_bom)
        except OSError as exc:
            QMessageBox.critical(
                self, "Write failed",
                f"No further file was modified.\n\n{exc}")
            return
        self.accept()


class CollisionEditorWindow(QMainWindow):
    """Manual collision editor built on OpenUAStudio's existing systems."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Collision Editor — OpenUAStudio")
        self.resize(1100, 720)
        self.project = CollisionProject()
        self.family: AssetFamily | None = None
        self._vp_embedded: EmbeddedVPSet | None = None
        self._vp_table: VPTable | None = None
        self._vp_table_source: str = ""
        self._active_script_path: Path | None = None
        self._active_script_kind: str = ""
        self._active_script_id: int | None = None
        self._current_owner: str | None = None
        self._selected = -1
        self._selected_fire_point = -1
        self._selected_tracer_point = -1
        self._modified = False
        self._syncing = False
        self._last_directory = Path.home()
        self._undo: list[tuple] = []
        self._redo: list[tuple] = []
        self._radius_slider_active = False
        self._radius_spin_active = False
        self._overeof_spin_active = False
        self._vehicle_preview_active_edits: set[str] = set()
        self._model_filter_expansion: dict[int, bool] | None = None

        self.viewport = CollisionViewport()
        self.viewport.spherePicked.connect(self._select_sphere)
        self.viewport.firePointPicked.connect(self._select_fire_point)
        self.viewport.tracerPointPicked.connect(self._select_tracer_point)
        self.viewport.sphereContextMenuRequested.connect(
            self._show_sphere_context_menu)
        self.viewport.sphereNudgeRequested.connect(self._gizmo_nudge)
        self.viewport.statusMessage.connect(
            lambda text: self.statusBar().showMessage(text, 4500))
        self.model_tree = QTreeWidget()
        self.model_tree.setHeaderLabels(["Internal path", "VP"])
        self.model_tree.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.model_tree.currentItemChanged.connect(self._model_changed)
        self.sphere_tree = QTreeWidget()
        self.sphere_tree.setHeaderLabels(
            ["Sphere", "Radius", "Visible", "Index"])
        self.sphere_tree.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.sphere_tree.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.sphere_tree.setItemDelegate(
            RadiusItemDelegate(self.sphere_tree))
        self.sphere_tree.currentItemChanged.connect(
            self._sphere_tree_selection_changed)
        self.sphere_tree.itemDoubleClicked.connect(
            self._sphere_tree_double_clicked)
        self.sphere_tree.itemChanged.connect(
            self._sphere_tree_item_changed)
        self.fire_point_tree = QTreeWidget()
        self.fire_point_tree.setHeaderLabels(["Point", "X", "Y", "Z"])
        self.fire_point_tree.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.fire_point_tree.currentItemChanged.connect(
            self._fire_point_tree_selection_changed)
        self.tracer_point_tree = QTreeWidget()
        self.tracer_point_tree.setHeaderLabels(["Point", "X", "Y", "Z"])
        self.tracer_point_tree.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.tracer_point_tree.currentItemChanged.connect(
            self._tracer_point_tree_selection_changed)

        self.sphere_tree.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.sphere_tree.customContextMenuRequested.connect(
            self._show_sphere_tree_context_menu)

        self._build_actions()
        self._build_ui()
        self._sync_all()
        self.statusBar().showMessage(
            "BASE/SKLT assets are read-only; only explicit text exports or "
            "confirmed script application write files.")

    def _build_actions(self):
        file_menu = self.menuBar().addMenu("&File")
        self.file_menu = file_menu
        self.open_base_action = QAction("Import BAS Archive", self)
        self.open_base_action.setShortcut(QKeySequence.StandardKey.Open)
        self.open_base_action.triggered.connect(self.open_base_dialog)
        self.open_sklt_action = QAction("Import SKLT", self)
        self.open_sklt_action.triggered.connect(self.open_sklt_dialog)
        self.open_vehicle_script_action = QAction(
            "Import Vehicle / Weapon from Script", self)
        self.open_vehicle_script_action.setIconText("Import Script Object")
        self.open_vehicle_script_action.triggered.connect(
            self.open_vehicle_script_dialog)
        self.import_action = QAction("Import Collision Text", self)
        self.import_action.setIconText("Import Collision Text")
        self.import_action.triggered.connect(self.import_collisions)

        self.file_import_menu = file_menu.addMenu("Import")
        for action in (
                self.open_base_action, self.open_sklt_action,
                self.open_vehicle_script_action, self.import_action):
            self.file_import_menu.addAction(action)

        self.export_action = QAction("Export Collision Text", self)
        self.export_action.setIconText("Export Collision Text")
        self.export_action.triggered.connect(self.export_text)
        self.copy_output_action = QAction(
            "Copy Output to Clipboard", self)
        self.copy_output_action.triggered.connect(self.copy_output)

        self.file_export_menu = file_menu.addMenu("Export")
        for action in (self.export_action, self.copy_output_action):
            self.file_export_menu.addAction(action)

        self.apply_script_action = QAction(
            "Apply to Existing Script", self)
        self.apply_script_action.setIconText("Apply to Existing Script")
        self.apply_script_action.triggered.connect(self.apply_to_script)
        self.save_loaded_script_action = QAction("Overwrite", self)
        self.save_loaded_script_action.setIconText("Overwrite")
        self.save_loaded_script_action.setToolTip(
            "Overwrite the vehicle or weapon definition previously imported "
            "from a script. A backup and preview are still provided.")
        self.save_loaded_script_action.triggered.connect(
            self.overwrite_loaded_script)
        self.overwrite_loaded_action = self.save_loaded_script_action

        self.file_script_menu = file_menu.addMenu("Script")
        self.file_script_menu.addAction(self.apply_script_action)
        self.file_script_menu.addAction(self.save_loaded_script_action)

        file_menu.addSeparator()
        self.exit_action = QAction("Exit", self)
        self.exit_action.triggered.connect(self.close)
        file_menu.addAction(self.exit_action)
        self.create_suggested_action = QAction(
            "Create Suggested Sphere", self)
        self.create_suggested_action.triggered.connect(
            self.create_suggested)

        edit_menu = self.menuBar().addMenu("&Edit")
        self.undo_action = QAction("Undo", self)
        self.undo_action.setIconText("< Undo")
        self.undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        self.undo_action.triggered.connect(self.undo)
        edit_menu.addAction(self.undo_action)
        self.redo_action = QAction("Redo", self)
        self.redo_action.setIconText("Redo >")
        self.redo_action.setShortcuts([
            QKeySequence.StandardKey.Redo, QKeySequence("Ctrl+Shift+Z")])
        self.redo_action.triggered.connect(self.redo)
        edit_menu.addAction(self.redo_action)
        edit_menu.addSeparator()

        self.add_legacy_action = QAction("Add Legacy Radius", self)
        self.add_legacy_action.triggered.connect(self.add_legacy)
        self.add_vehicle_action = QAction("Add Vehicle Collision", self)
        self.add_vehicle_action.triggered.connect(
            lambda: self.add_compound(VEHICLE))
        self.add_weapon_action = QAction("Add Weapon Collision", self)
        self.add_weapon_action.triggered.connect(
            lambda: self.add_compound(WEAPON))
        self.duplicate_action = QAction("Duplicate Sphere", self)
        self.duplicate_action.setShortcut(QKeySequence("Ctrl+D"))
        self.duplicate_action.triggered.connect(self.duplicate_sphere)
        self.delete_action = QAction("Delete Sphere", self)
        self.delete_action.setShortcut(QKeySequence.StandardKey.Delete)
        self.delete_action.triggered.connect(self.delete_sphere)
        self.change_to_legacy_action = QAction(
            "Change to Legacy Radius", self)
        self.change_to_vehicle_action = QAction(
            "Change to Vehicle Collision", self)
        self.change_to_weapon_action = QAction(
            "Change to Weapon Collision", self)
        self.change_to_legacy_action.triggered.connect(
            lambda: self.change_sphere_type(LEGACY))
        self.change_to_vehicle_action.triggered.connect(
            lambda: self.change_sphere_type(VEHICLE))
        self.change_to_weapon_action.triggered.connect(
            lambda: self.change_sphere_type(WEAPON))
        self.mirror_x_action = QAction("Mirror on X Axis", self)
        self.mirror_y_action = QAction("Mirror on Y Axis", self)
        self.mirror_z_action = QAction("Mirror on Z Axis", self)
        self.mirror_x_action.triggered.connect(
            lambda: self.mirror_selected_sphere("x"))
        self.mirror_y_action.triggered.connect(
            lambda: self.mirror_selected_sphere("y"))
        self.mirror_z_action.triggered.connect(
            lambda: self.mirror_selected_sphere("z"))
        self.reset_collisions_action = QAction("Reset Collisions", self)
        self.reset_collisions_action.triggered.connect(self.reset_collisions)
        for action in (
                self.add_legacy_action, self.add_vehicle_action,
                self.add_weapon_action, self.duplicate_action,
                self.delete_action):
            edit_menu.addAction(action)
        change_type_menu = edit_menu.addMenu("Change Sphere Type")
        self._populate_change_type_menu(change_type_menu)
        self.change_type_action = change_type_menu.menuAction()
        mirror_menu = edit_menu.addMenu("Mirror Selected Sphere")
        mirror_menu.addAction(self.mirror_x_action)
        mirror_menu.addAction(self.mirror_y_action)
        mirror_menu.addAction(self.mirror_z_action)
        edit_menu.addAction(self.reset_collisions_action)

        self.viewpoint_menu = self.menuBar().addMenu("Viewpoint")
        self.viewpoint_actions = {}
        viewpoint_options = (
            ("Show Model", True, self.viewport.set_model_visible),
            ("Show Textures", True, self.viewport.set_textures_visible),
            ("Show Legacy Radius", True, lambda value:
             self.viewport.set_collision_category_visible(LEGACY, value)),
            ("Show Vehicle Collisions", True, lambda value:
             self.viewport.set_collision_category_visible(VEHICLE, value)),
            ("Show Weapon Collisions", True, lambda value:
             self.viewport.set_collision_category_visible(WEAPON, value)),
            ("Show Ground Simulation", True,
             self.viewport.set_ground_simulation_visible),
            ("Show Overeof", True,
             self.viewport.set_overeof_visible),
            ("Show Fire Points", True,
             self.viewport.set_fire_points_visible),
            ("Show Tracer Points", True,
             self.viewport.set_tracer_points_visible),
        )
        for text, checked, slot in viewpoint_options:
            action = QAction(text, self)
            action.setCheckable(True)
            action.setChecked(checked)
            action.toggled.connect(slot)
            self.viewpoint_menu.addAction(action)
            self.viewpoint_actions[text] = action

        self.project_summary_menu = self.menuBar().addMenu("Project Summary")
        self.project_summary_menu.setStyleSheet("""
            QMenu::item { color: #69c9e8; padding: 5px 12px; }
            QMenu::item:disabled { color: #69c9e8; }
            QMenu::separator { background: #397f96; height: 1px;
                               margin: 4px 8px; }
        """)

        toolbar = QToolBar("Collision tools", self)
        toolbar.setObjectName("collisionTools")
        toolbar.setMovable(False)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, toolbar)
        for action in (
                self.add_legacy_action, self.add_vehicle_action,
                self.add_weapon_action, self.duplicate_action,
                self.delete_action):
            toolbar.addAction(action)
        self.change_type_button = QToolButton()
        self.change_type_button.setText("Change Sphere Type")
        self.change_type_button.setToolTip(
            "Choose exactly which collision category the selected sphere "
            "should become.")
        self.change_type_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup)
        change_type_button_menu = QMenu(self.change_type_button)
        self._populate_change_type_menu(change_type_button_menu)
        self.change_type_button.setMenu(change_type_button_menu)
        toolbar.addWidget(self.change_type_button)
        self.mirror_sphere_button = QToolButton()
        self.mirror_sphere_button.setText("Mirror Selected Sphere")
        self.mirror_sphere_button.setToolTip(
            "Duplicate the selected compound sphere on the opposite side "
            "of the chosen model axis.")
        self.mirror_sphere_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup)
        mirror_button_menu = QMenu(self.mirror_sphere_button)
        mirror_button_menu.addAction(self.mirror_x_action)
        mirror_button_menu.addAction(self.mirror_y_action)
        mirror_button_menu.addAction(self.mirror_z_action)
        self.mirror_sphere_button.setMenu(mirror_button_menu)
        toolbar.addWidget(self.mirror_sphere_button)
        toolbar.addAction(self.reset_collisions_action)
        self.reset_view_action = QAction("Reset View", self)
        self.reset_view_action.triggered.connect(self._reset_view)
        toolbar.addAction(self.reset_view_action)
        toolbar.addSeparator()
        toolbar.addAction(self.undo_action)
        toolbar.addAction(self.redo_action)
        self._build_model_preview_toolbar()
        self.viewport.manualCameraChanged.connect(
            self._on_manual_camera_changed)

    def _build_model_preview_toolbar(self):
        """Create a full-width second row for visual-only VP scaling."""

        self.addToolBarBreak(Qt.ToolBarArea.TopToolBarArea)
        toolbar = QToolBar("Model Preview Scale", self)
        toolbar.setObjectName("modelPreviewScaleTools")
        toolbar.setMovable(False)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, toolbar)
        toolbar.addWidget(QLabel(" View preset: "))
        self.toolbar_view_preset_combo = QComboBox()
        self.toolbar_view_preset_combo.addItems(VIEW_PRESETS)
        self.toolbar_view_preset_combo.currentTextChanged.connect(
            self._on_view_preset_changed)
        toolbar.addWidget(self.toolbar_view_preset_combo)
        toolbar.addSeparator()
        toolbar.addWidget(QLabel(" Model Preview Scale: "))
        self.model_scale_spins = {}
        for axis in ("X", "Y", "Z"):
            toolbar.addWidget(QLabel(f" {axis} "))
            spin = CompactScaleSpinBox()
            spin.setRange(0.0, 1000.0)
            spin.setDecimals(3)
            spin.setSingleStep(0.1)
            spin.setValue(1.0)
            spin.setKeyboardTracking(False)
            spin.setMinimumWidth(92)
            spin.setMaximumWidth(118)
            spin.setToolTip(
                f"Visual-only vp_scale_{axis.lower()} preview. "
                "The model changes size; collision spheres and exported "
                "coll_* values are not transformed automatically.")
            spin.valueChanged.connect(self._model_preview_scale_changed)
            self.model_scale_spins[axis.lower()] = spin
            toolbar.addWidget(spin)
        self.model_scale_x_spin = self.model_scale_spins["x"]
        self.model_scale_y_spin = self.model_scale_spins["y"]
        self.model_scale_z_spin = self.model_scale_spins["z"]
        toolbar.addSeparator()
        self.reset_model_scale_button = QPushButton("Reset")
        self.reset_model_scale_button.setToolTip(
            "Reset the model preview to X 1.0, Y 1.0, Z 1.0.")
        self.reset_model_scale_button.clicked.connect(
            self._reset_model_preview_scale)
        toolbar.addWidget(self.reset_model_scale_button)
        toolbar.addSeparator()
        # Keep the output/apply workflow on the full-width toolbar. These
        # actions are shared with the File menu, so there is one execution
        # path and no duplicate button logic hiding at the bottom of the
        # properties panel.
        for action in (
                self.create_suggested_action,
                self.export_action,
                self.copy_output_action,
                self.import_action,
                self.apply_script_action):
            toolbar.addAction(action)
        self.model_preview_scale_toolbar = toolbar
        # Compatibility alias retained for integrations that only used this
        # attribute to locate the preview-scale controls.
        self.model_preview_scale_box = toolbar

    def _build_ui(self):
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter = splitter
        self.setCentralWidget(splitter)

        source_panel = QWidget()
        source_panel.setMinimumWidth(260)
        source_panel.setMaximumWidth(440)
        source_panel.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        source_layout = QVBoxLayout(source_panel)
        source_layout.addWidget(QLabel("Resources in archive"))
        self.model_search = QLineEdit()
        self.model_search.setPlaceholderText("Search model names...")
        self.model_search.setClearButtonEnabled(True)
        self.model_search.setToolTip(
            "Filter models by name, internal path or owner path.")
        self.model_search.textChanged.connect(self._filter_models)
        source_layout.addWidget(self.model_search)
        source_layout.addWidget(self.model_tree, 1)
        self.source_label = QLabel("No source loaded.")
        self.source_label.setWordWrap(True)
        source_layout.addWidget(self.source_label)
        model_header = self.model_tree.header()
        model_header.setMinimumSectionSize(36)
        # QHeaderView stretches the last section by default.  With VP as the
        # last column that silently consumed half the list, leaving a large
        # empty gap and truncating Internal path.  Keep VP compact so the
        # existing panel width is spent on the model path instead.
        model_header.setStretchLastSection(False)
        model_header.setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        model_header.setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents)
        self.model_tree.headerItem().setTextAlignment(
            1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.model_tree.setColumnWidth(1, 48)
        self.model_tree.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.model_tree.setTextElideMode(Qt.TextElideMode.ElideRight)
        splitter.addWidget(source_panel)
        splitter.addWidget(self.viewport)

        properties = QWidget()
        right = QVBoxLayout(properties)
        right.setContentsMargins(4, 3, 4, 3)
        right.setSpacing(1)
        project_box = QGroupBox("Project")
        project_form = QFormLayout(project_box)
        project_form.setContentsMargins(6, 5, 6, 5)
        project_form.setHorizontalSpacing(6)
        project_form.setVerticalSpacing(3)
        project_form.setRowWrapPolicy(
            QFormLayout.RowWrapPolicy.WrapLongRows)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Example")
        self.name_edit.editingFinished.connect(self._project_fields_changed)
        self.target_combo = QComboBox()
        self.target_combo.addItem("vehicle", VEHICLE)
        self.target_combo.addItem("weapon", WEAPON)
        self.target_combo.currentIndexChanged.connect(
            self._project_fields_changed)
        project_form.addRow("Vehicle / Weapon Name", self.name_edit)
        project_form.addRow("Target category", self.target_combo)
        self.vanilla_collision_notice = QLabel(
            "Vanilla Urban Assault supports Legacy Radius only. "
            "Compound collision spheres require OpenUA.")
        self.vanilla_collision_notice.setWordWrap(True)
        self.vanilla_collision_notice.setStyleSheet(
            "color: #e1aa62; font-size: 10px;")
        self.vanilla_collision_notice.setToolTip(
            "Red Legacy Radius is vanilla-compatible. Green and blue "
            "compound spheres are OpenUA-only script data.")
        project_form.addRow(self.vanilla_collision_notice)
        right.addWidget(project_box)

        self.ground_alignment_box = QGroupBox("Ground Alignment")
        ground_layout = QVBoxLayout(self.ground_alignment_box)
        ground_layout.setContentsMargins(6, 4, 6, 4)
        ground_layout.setSpacing(3)
        ground_controls = QGridLayout()
        ground_controls.setHorizontalSpacing(5)
        ground_controls.setVerticalSpacing(3)
        self.overeof_check = QCheckBox("Include Overeof")
        self.overeof_check.setToolTip(
            "When enabled, the model and collision preview follow the "
            "OpenUA placement rule actor_y = ground_y - overeof, and the "
            "value is included in vehicle text output.")
        self.overeof_check.toggled.connect(
            self._overeof_enabled_changed)
        ground_controls.addWidget(self.overeof_check, 0, 0)
        self.overeof_spin = self._coordinate_spin()
        # Overeof is authored as a practical placement value. One visible
        # decimal keeps useful half/tenth-unit precision without presenting
        # noisy values such as 20,0000 in the compact properties panel.
        self.overeof_spin.setDecimals(1)
        self.overeof_spin.setSingleStep(1.0)
        self.overeof_spin.setKeyboardTracking(True)
        self.overeof_spin.setMinimumWidth(105)
        self.overeof_spin.valueChanged.connect(
            self._overeof_value_changed)
        self.overeof_spin.editingFinished.connect(
            self._finish_overeof_edit)
        ground_controls.addWidget(self.overeof_spin, 0, 1)
        self.suggest_overeof_button = QPushButton("Suggest from Model")
        self.suggest_overeof_button.setToolTip(
            "Use the lowest scaled model point as a starting Overeof "
            "estimate. The result remains manually editable.")
        self.suggest_overeof_button.clicked.connect(
            self._suggest_overeof_from_bounds)
        ground_controls.addWidget(self.suggest_overeof_button, 1, 0)
        self.reset_overeof_button = QPushButton("Reset")
        self.reset_overeof_button.setToolTip(
            "Reset Overeof preview to 0 and omit it from output.")
        self.reset_overeof_button.clicked.connect(self._reset_overeof)
        ground_controls.addWidget(self.reset_overeof_button, 1, 1)
        ground_controls.setColumnStretch(1, 1)
        ground_layout.addLayout(ground_controls)
        self.ground_alignment_notice = QLabel("")
        self.ground_alignment_notice.hide()
        right.addWidget(self.ground_alignment_box)

        self.fire_points_box = QGroupBox("Fire Points (Vanilla)")
        fire_layout = QVBoxLayout(self.fire_points_box)
        fire_layout.setContentsMargins(6, 4, 6, 4)
        fire_layout.setSpacing(3)
        self.fire_points_check = QCheckBox("Include Fire Points")
        self.fire_points_check.setToolTip(
            "Include vanilla fire_x, fire_y, fire_z and num_weapons in "
            "output and show their exact projectile spawn positions.")
        self.fire_points_check.toggled.connect(
            self._fire_points_enabled_changed)
        fire_layout.addWidget(self.fire_points_check)
        fire_grid = QGridLayout()
        fire_grid.setHorizontalSpacing(5)
        fire_grid.setVerticalSpacing(3)
        self.fire_point_spins = {}
        for index, axis in enumerate(("X", "Y", "Z")):
            row, pair = divmod(index, 2)
            fire_grid.addWidget(QLabel(axis), row, pair * 2)
            spin = CompactScaleSpinBox()
            spin.setRange(-1_000_000.0, 1_000_000.0)
            spin.setDecimals(3)
            spin.setSingleStep(1.0)
            spin.setKeyboardTracking(True)
            spin.setMinimumWidth(72)
            spin.setToolTip(
                f"Vanilla fire_{axis.lower()} projectile spawn offset.")
            key = f"fire_{axis.lower()}"
            spin.valueChanged.connect(
                lambda value, field=key:
                self._fire_point_value_changed(field, value))
            spin.editingFinished.connect(
                lambda field=key: self._finish_vehicle_preview_edit(field))
            self.fire_point_spins[axis.lower()] = spin
            fire_grid.addWidget(spin, row, pair * 2 + 1)
        self.fire_x_spin = self.fire_point_spins["x"]
        self.fire_y_spin = self.fire_point_spins["y"]
        self.fire_z_spin = self.fire_point_spins["z"]
        fire_grid.addWidget(QLabel("Weapons"), 1, 2)
        self.num_weapons_spin = QSpinBox()
        self.num_weapons_spin.setRange(0, 255)
        self.num_weapons_spin.setValue(1)
        self.num_weapons_spin.setMinimumWidth(72)
        self.num_weapons_spin.setToolTip(
            "Vanilla num_weapons. Values 0 and 1 produce one point; two "
            "or more points stay evenly distributed and symmetric on X.")
        self.num_weapons_spin.valueChanged.connect(
            self._num_weapons_changed)
        self.num_weapons_spin.editingFinished.connect(
            lambda: self._finish_vehicle_preview_edit("num_weapons"))
        fire_grid.addWidget(self.num_weapons_spin, 1, 3)
        fire_grid.setColumnStretch(1, 1)
        fire_grid.setColumnStretch(3, 1)
        fire_layout.addLayout(fire_grid)
        self.fire_point_notice = QLabel("")
        self.fire_point_notice.hide()
        self.fire_point_tree.setMinimumHeight(90)
        self.fire_point_tree.setMaximumHeight(135)
        fire_header = self.fire_point_tree.header()
        fire_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            fire_header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents)
            self.fire_point_tree.headerItem().setTextAlignment(
                column, Qt.AlignmentFlag.AlignRight
                | Qt.AlignmentFlag.AlignVCenter)
        fire_layout.addWidget(self.fire_point_tree)
        right.addWidget(self.fire_points_box)

        self.tracer_points_box = QGroupBox("Tracer Points (OpenUA)")
        tracer_layout = QVBoxLayout(self.tracer_points_box)
        tracer_layout.setContentsMargins(6, 4, 6, 4)
        tracer_layout.setSpacing(3)
        self.tracer_points_check = QCheckBox("Include Tracer Points")
        self.tracer_points_check.setToolTip(
            "Include OpenUA mgun_tracer_offset_x/y/z and num_mguns in "
            "output and show the exact visual tracer origins.")
        self.tracer_points_check.toggled.connect(
            self._tracer_points_enabled_changed)
        tracer_layout.addWidget(self.tracer_points_check)
        tracer_grid = QGridLayout()
        tracer_grid.setHorizontalSpacing(5)
        tracer_grid.setVerticalSpacing(3)
        self.tracer_point_spins = {}
        for index, axis in enumerate(("X", "Y", "Z")):
            row, pair = divmod(index, 2)
            tracer_grid.addWidget(QLabel(axis), row, pair * 2)
            spin = CompactScaleSpinBox()
            spin.setRange(-6000.0, 6000.0)
            spin.setDecimals(3)
            spin.setSingleStep(1.0)
            spin.setKeyboardTracking(True)
            spin.setMinimumWidth(72)
            key = f"mgun_tracer_offset_{axis.lower()}"
            spin.setToolTip(f"OpenUA {key} visual tracer-origin offset.")
            spin.valueChanged.connect(
                lambda value, field=key:
                self._tracer_point_value_changed(field, value))
            spin.editingFinished.connect(
                lambda field=key: self._finish_vehicle_preview_edit(field))
            self.tracer_point_spins[axis.lower()] = spin
            tracer_grid.addWidget(spin, row, pair * 2 + 1)
        self.tracer_x_spin = self.tracer_point_spins["x"]
        self.tracer_y_spin = self.tracer_point_spins["y"]
        self.tracer_z_spin = self.tracer_point_spins["z"]
        tracer_grid.addWidget(QLabel("MGUNs"), 1, 2)
        self.num_mguns_spin = QSpinBox()
        self.num_mguns_spin.setRange(1, 255)
        self.num_mguns_spin.setValue(1)
        self.num_mguns_spin.setMinimumWidth(72)
        self.num_mguns_spin.setToolTip(
            "OpenUA num_mguns. Multiple Tracer Points are distributed "
            "evenly and symmetrically from -X to +X.")
        self.num_mguns_spin.valueChanged.connect(self._num_mguns_changed)
        self.num_mguns_spin.editingFinished.connect(
            lambda: self._finish_vehicle_preview_edit("num_mguns"))
        tracer_grid.addWidget(self.num_mguns_spin, 1, 3)
        tracer_grid.setColumnStretch(1, 1)
        tracer_grid.setColumnStretch(3, 1)
        tracer_layout.addLayout(tracer_grid)
        self.tracer_points_notice = QLabel("OpenUA only")
        self.tracer_points_notice.setStyleSheet(
            "color: #e1aa62; font-size: 10px;")
        tracer_layout.addWidget(self.tracer_points_notice)
        self.tracer_point_tree.setMinimumHeight(72)
        self.tracer_point_tree.setMaximumHeight(120)
        tracer_header = self.tracer_point_tree.header()
        tracer_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            tracer_header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents)
            self.tracer_point_tree.headerItem().setTextAlignment(
                column, Qt.AlignmentFlag.AlignRight
                | Qt.AlignmentFlag.AlignVCenter)
        tracer_layout.addWidget(self.tracer_point_tree)
        right.addWidget(self.tracer_points_box)

        selected_box = QGroupBox("Selected Element")
        self.selected_box = selected_box
        selected_layout = QHBoxLayout(selected_box)
        selected_layout.setContentsMargins(6, 4, 6, 4)
        selected_layout.setSpacing(5)
        # Kept as non-visible compatibility attributes for integrations that
        # queried them before Index moved into the Spheres table.
        self.type_value = QLabel("None")
        self.index_value = QLabel("None")
        self.type_value.hide()
        self.index_value.hide()
        self.selected_element_label = QLabel("None")
        self.visible_check = QCheckBox("Active / visible")
        self.visible_check.toggled.connect(self._visibility_changed)
        self.runtime_radius_value = QLabel("")
        self.runtime_radius_value.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.runtime_radius_value.setStyleSheet("color: #d9a35f;")
        self.runtime_radius_value.hide()
        selected_layout.addWidget(self.selected_element_label)
        selected_layout.addStretch(1)
        selected_layout.addWidget(self.visible_check)
        selected_layout.addWidget(self.runtime_radius_value)

        spheres_box = QGroupBox("Spheres")
        spheres_layout = QVBoxLayout(spheres_box)
        self.spheres_box = spheres_box
        self.sphere_tree.setMinimumHeight(150)
        self.sphere_tree.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        spheres_box.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        sphere_header = self.sphere_tree.header()
        sphere_header.setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        sphere_header.setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents)
        sphere_header.setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents)
        sphere_header.setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents)
        self.sphere_tree.headerItem().setTextAlignment(
            1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.sphere_tree.headerItem().setTextAlignment(
            2, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.sphere_tree.headerItem().setTextAlignment(
            3, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.sphere_tree.headerItem().setToolTip(
            1, "Double-click a Radius value to edit the sphere size.")
        spheres_layout.addWidget(self.sphere_tree, 1)

        radius_box = QGroupBox("Sphere Radius")
        radius_layout = QHBoxLayout(radius_box)
        radius_layout.setContentsMargins(6, 4, 6, 4)
        radius_layout.setSpacing(5)
        radius_layout.addWidget(QLabel("Radius"))
        self.radius_slider = QSlider(Qt.Orientation.Horizontal)
        self.radius_slider.setRange(0, _RADIUS_SLIDER_STEPS)
        self.radius_slider.sliderPressed.connect(
            self._begin_radius_slider)
        self.radius_slider.valueChanged.connect(
            self._radius_slider_changed)
        self.radius_slider.sliderReleased.connect(
            self._finish_radius_slider)
        radius_layout.addWidget(self.radius_slider, 1)
        self.radius_spin = self._coordinate_spin()
        self.radius_spin.setMinimum(1.0)
        self.radius_spin.setDecimals(0)
        self.radius_spin.setSingleStep(1.0)
        self.radius_spin.setKeyboardTracking(True)
        self.radius_spin.valueChanged.connect(
            self._radius_spin_changed)
        self.radius_spin.editingFinished.connect(
            self._finish_radius_spin_edit)
        self.radius_spin.setMinimumWidth(92)
        radius_layout.addWidget(self.radius_spin)
        right.addWidget(radius_box)

        transform_box = QGroupBox("Move Element")
        self.transform_box = transform_box
        transform_box.setToolTip(
            "Move the selected compound sphere or Fire Point. "
            "With the viewport focused: Left/Right move on X, Up/Down move "
            "on Z, and Page Up/Page Down move on Y. Every key press uses "
            "the current Move strength value.")
        transform_layout = QVBoxLayout(transform_box)
        transform_layout.setContentsMargins(5, 3, 5, 3)
        transform_layout.setSpacing(2)
        self.gizmo = CollisionMoveGizmo()
        self.gizmo.setMinimumSize(260, 165)
        self.gizmo.setMaximumHeight(175)
        self.gizmo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.gizmo.directionTriggered.connect(self._gizmo_nudge)
        transform_layout.addWidget(self.gizmo, 1)
        strength_row = QHBoxLayout()
        strength_row.addWidget(QLabel("Move strength"))
        self.move_strength_slider = QSlider(Qt.Orientation.Horizontal)
        self.move_strength_slider.setRange(1, 500)
        self.move_strength_slider.setValue(1)
        self.move_strength_spin = QSpinBox()
        self.move_strength_spin.setRange(1, 1_000_000)
        self.move_strength_spin.setValue(1)
        self.move_strength_spin.setMinimumWidth(72)
        self.move_strength_value = self.move_strength_spin
        self.move_strength_slider.valueChanged.connect(
            self._move_strength_slider_changed)
        self.move_strength_spin.valueChanged.connect(
            self._move_strength_spin_changed)
        strength_row.addWidget(self.move_strength_slider, 1)
        strength_row.addWidget(self.move_strength_spin)
        transform_layout.addLayout(strength_row)
        transform_box.setMaximumHeight(235)
        transform_box.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        right.addWidget(transform_box)
        right.insertWidget(0, selected_box)
        right.addWidget(spheres_box, 1)

        properties_scroll = QScrollArea()
        properties_scroll.setWidgetResizable(True)
        properties_scroll.setFrameShape(QFrame.Shape.NoFrame)
        properties_scroll.setMinimumWidth(275)
        properties_scroll.setWidget(properties)
        splitter.addWidget(properties_scroll)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([300, 830, 360])

    @staticmethod
    def _coordinate_spin() -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(-1000000.0, 1000000.0)
        spin.setDecimals(4)
        spin.setSingleStep(1.0)
        return spin

    @staticmethod
    def _radius_to_slider(radius: float) -> int:
        radius = max(10 ** _RADIUS_LOG_MIN, min(
            10 ** _RADIUS_LOG_MAX, float(radius)))
        ratio = (
            (math.log10(radius) - _RADIUS_LOG_MIN)
            / (_RADIUS_LOG_MAX - _RADIUS_LOG_MIN)
        )
        return round(ratio * _RADIUS_SLIDER_STEPS)

    @staticmethod
    def _slider_to_radius(value: int) -> float:
        ratio = max(0.0, min(1.0, value / _RADIUS_SLIDER_STEPS))
        exponent = (
            _RADIUS_LOG_MIN
            + ratio * (_RADIUS_LOG_MAX - _RADIUS_LOG_MIN)
        )
        return 10 ** exponent

    def _push_undo(self):
        state = self.project.snapshot()
        if not self._undo or self._undo[-1] != state:
            self._undo.append(state)
            self._undo = self._undo[-100:]
        self._redo.clear()

    def _set_modified(self, modified=True):
        self._modified = bool(modified)
        title = "Collision Editor — OpenUAStudio"
        self.setWindowTitle(("* " if self._modified else "") + title)

    def undo(self):
        if not self._undo:
            return
        self._radius_spin_active = False
        self._vehicle_preview_active_edits.clear()
        self._redo.append(self.project.snapshot())
        self.project.restore(self._undo.pop())
        self._selected = min(
            self._selected, len(self.project.spheres()) - 1)
        self._set_modified()
        self._sync_all()

    def redo(self):
        if not self._redo:
            return
        self._radius_spin_active = False
        self._vehicle_preview_active_edits.clear()
        self._undo.append(self.project.snapshot())
        self.project.restore(self._redo.pop())
        self._selected = min(
            self._selected, len(self.project.spheres()) - 1)
        self._set_modified()
        self._sync_all()

    def _sync_gizmo_camera(self):
        self.gizmo.set_camera_orientation(
            self.viewport._yaw, self.viewport._pitch)

    def _on_manual_camera_changed(self):
        with QSignalBlocker(self.toolbar_view_preset_combo):
            self.toolbar_view_preset_combo.setCurrentText("Current View")
        self._sync_gizmo_camera()

    def _on_view_preset_changed(self, preset: str):
        if preset != "Current View":
            self.viewport.apply_view_preset(
                preset, self.viewport.size(), 100)
        self._sync_gizmo_camera()

    def _reset_view(self):
        self.viewport.reset_view()
        with QSignalBlocker(self.toolbar_view_preset_combo):
            self.toolbar_view_preset_combo.setCurrentText("Current View")
        self._sync_gizmo_camera()

    def open_base_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import BAS Archive", str(self._last_directory),
            "BAS archives (SET.BAS *.bas *.BAS);;All files (*)")
        if path:
            self.open_base(path)

    def _current_set_bas_path(self) -> Path | None:
        if self._vp_embedded is None:
            return None
        return Path(self._vp_embedded.source_path)

    def _clear_script_link(self) -> None:
        self._active_script_path = None
        self._active_script_kind = ""
        self._active_script_id = None

    def open_vehicle_script_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import vehicle / weapon definition",
            str(self._last_directory),
            "OpenUA scripts (*.txt *.scr *.ini *.ldf);;All files (*)")
        if path:
            self.open_vehicle_script(path)

    def open_vehicle_script(
            self, path: str | Path, *, vehicle_id: int | None = None,
            object_id: int | None = None, object_kind: str | None = None,
            set_bas_path: str | Path | None = None) -> bool:
        """Load one ``new_vehicle`` or ``new_weapon`` through ``vp_normal``.

        ``vehicle_id`` remains as a compatibility argument and implies
        ``new_vehicle``. New callers may use ``object_id`` plus ``object_kind``.
        The linked asset and script are read-only until an explicit Apply or
        Overwrite action is confirmed.
        """

        script_path = Path(path)
        try:
            text, _encoding, _bom = read_script_file(script_path)
            references = script_model_references(text)
        except (OSError, UnicodeError, CollisionScriptError) as exc:
            QMessageBox.critical(
                self, "Script object load failed", str(exc))
            return False
        if not references:
            QMessageBox.warning(
                self, "No resolvable definition",
                "No complete new_vehicle or new_weapon block with an active "
                "vp_normal was found. modify_* inheritance is not guessed.")
            return False

        reference = None
        requested_id = object_id
        requested_kind = object_kind.lower() if object_kind else None
        if vehicle_id is not None:
            requested_id = int(vehicle_id)
            requested_kind = "new_vehicle"
        if requested_id is not None:
            matches = [
                candidate for candidate in references
                if candidate.block.object_id == int(requested_id)
                and (requested_kind is None
                     or candidate.block.kind == requested_kind)
            ]
            if len(matches) != 1:
                kind_text = requested_kind or "new_vehicle/new_weapon"
                QMessageBox.warning(
                    self, "Definition not found",
                    f"Expected exactly one {kind_text} {requested_id} with "
                    "vp_normal.")
                return False
            reference = matches[0]

        current_set = self._current_set_bas_path()
        chosen_set = Path(set_bas_path) if set_bas_path is not None else current_set
        if reference is None or chosen_set is None:
            dialog = OpenScriptObjectDialog(
                self, script_path, references, current_set)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return False
            reference, chosen_set = dialog.selected()

        chosen_set = Path(chosen_set)
        if current_set is None or current_set.resolve() != chosen_set.resolve():
            if not self.open_base(chosen_set):
                return False
        if self._vp_embedded is None or self._vp_table is None:
            QMessageBox.critical(
                self, "VP database unavailable",
                "The selected archive did not expose a SET.BAS VP table.")
            return False
        if not 0 <= reference.vp_normal < len(self._vp_table.entries):
            QMessageBox.critical(
                self, "VP out of range",
                f"vp_normal {reference.vp_normal} is outside the SET.BAS "
                f"database (0..{len(self._vp_table.entries) - 1}).")
            return False
        if not self._select_model_by_vp(reference.vp_normal):
            entry = self._vp_table.entry(reference.vp_normal)
            QMessageBox.critical(
                self, "VP model unavailable",
                f"VP {reference.vp_normal} resolves to "
                f"{entry.base_name}, but its SKLT could not be selected "
                "from the loaded SET.BAS family. The active VP table may "
                "reference a loose BASE that is not embedded in this SET.")
            return False

        block = reference.block
        target_category = (
            WEAPON if block.kind == "new_weapon" else VEHICLE)
        try:
            legacy, compounds, warnings = import_collision_block(
                text, block, target_category)
            if target_category == VEHICLE:
                overeof_enabled, overeof = import_overeof_block(text, block)
                (fire_enabled, fire_x, fire_y, fire_z,
                 num_weapons) = import_fire_points_block(text, block)
                tracer_probe = CollisionProject(
                    legacy=legacy, compound=compounds)
                (tracer_enabled, tracer_x, tracer_y, tracer_z,
                 num_mguns) = import_tracer_points_block(
                    text, block, effective_runtime_radius(tracer_probe))
            else:
                overeof_enabled, overeof = False, 0.0
                fire_enabled, fire_x, fire_y, fire_z = False, 0.0, 0.0, 0.0
                num_weapons = 1
                tracer_enabled = False
                tracer_x, tracer_y, tracer_z, num_mguns = 0.0, 0.0, 0.0, 1
        except CollisionScriptError as exc:
            QMessageBox.critical(
                self, "Script parameter import failed", str(exc))
            return False

        current_item = self.model_tree.currentItem()
        source_model = (
            current_item.data(0, _MODEL_NAME_ROLE)
            if current_item is not None else "")
        fallback_name = (
            "Weapon" if target_category == WEAPON else "Vehicle")
        self.project = CollisionProject(
            name=block.name or f"{fallback_name}_{block.object_id}",
            source_model=source_model,
            source_base=chosen_set.name,
            target_category=target_category,
            model_scale_x=reference.scale_x,
            model_scale_y=reference.scale_y,
            model_scale_z=reference.scale_z,
            overeof_enabled=overeof_enabled,
            overeof=overeof,
            fire_points_enabled=fire_enabled,
            fire_x=fire_x,
            fire_y=fire_y,
            fire_z=fire_z,
            num_weapons=num_weapons,
            tracer_points_enabled=tracer_enabled,
            mgun_tracer_offset_x=tracer_x,
            mgun_tracer_offset_y=tracer_y,
            mgun_tracer_offset_z=tracer_z,
            num_mguns=num_mguns,
            legacy=legacy,
            compound=compounds,
        )
        self._active_script_path = script_path
        self._active_script_kind = block.kind
        self._active_script_id = block.object_id
        self._selected = 0 if self.project.spheres() else -1
        self._selected_fire_point = -1
        self._selected_tracer_point = -1
        self._undo.clear()
        self._redo.clear()
        self._last_directory = script_path.parent
        self._set_modified(False)
        self.source_label.setText(
            f"{chosen_set.name}\nVP {reference.vp_normal}: "
            f"{source_model or '<model>'}\n"
            f"VP source: {self._vp_table_source}\n"
            f"Script: {script_path.name} — {block.kind} {block.object_id}")
        self._sync_all()
        message = (
            f"Loaded {block.kind} {block.object_id} through VP "
            f"{reference.vp_normal} with preview scale "
            f"{_number(reference.scale_x)}/"
            f"{_number(reference.scale_y)}/"
            f"{_number(reference.scale_z)}.")
        if warnings:
            message += " " + " ".join(warnings)
        self.statusBar().showMessage(message, 7000)
        return True

    def _select_model_by_vp(self, vp_id: int) -> bool:
        matches = []
        for index in range(self.model_tree.topLevelItemCount()):
            item = self.model_tree.topLevelItem(index)
            vp_ids = item.data(0, _MODEL_VP_ROLE) or ()
            if int(vp_id) in vp_ids:
                matches.append(item)
        if not matches:
            return False
        # VP slots are unique.  If a malformed archive exposes more than one
        # candidate, prefer the root object whose source offset matched the
        # embedded VP entry and keep the operation deterministic.
        self.model_tree.setCurrentItem(matches[0])
        return True

    def overwrite_loaded_script(self):
        if (self._active_script_path is None
                or self._active_script_id is None
                or not self._active_script_kind):
            QMessageBox.information(
                self, "No linked script definition",
                "Import a vehicle or weapon from a script first. For an "
                "arbitrary target, use Apply to Existing Script.")
            return
        expected_category = (
            WEAPON if "weapon" in self._active_script_kind else VEHICLE)
        if self.project.target_category != expected_category:
            QMessageBox.warning(
                self, "Target category mismatch",
                "The linked script definition and current project category "
                "do not match.")
            return
        if not self._validate_for_output():
            return
        dialog = ApplyScriptDialog(
            self, self.project,
            replace_all_managed=True,
            initial_path=self._active_script_path,
            initial_kind=self._active_script_kind,
            initial_id=self._active_script_id,
        )
        dialog.path_edit.setReadOnly(True)
        dialog.search_edit.setEnabled(False)
        dialog.block_combo.setEnabled(False)
        dialog.kind_combo.setEnabled(False)
        dialog.id_spin.setEnabled(False)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._set_modified(False)
            self.statusBar().showMessage(
                f"Loaded definition overwritten. Backup: "
                f"{dialog.backup_path}", 7000)

    def save_loaded_vehicle_script(self):
        """Compatibility alias for the previous vehicle-only action."""

        self.overwrite_loaded_script()

    def open_base(self, path: str | Path):
        self._clear_script_link()
        path = Path(path)
        embedded = None
        vp_table = None
        vp_source = ""
        vp_warnings: list[str] = []
        if path.name.casefold() == "set.bas":
            try:
                embedded = reconstruct_embedded_vps(path)
                vp_table, vp_source, vp_warnings = runtime_vp_table(
                    path, embedded)
            except (VPEmbeddedError, OSError, ValueError,
                    CollisionScriptError) as exc:
                QMessageBox.critical(
                    self, "VP database unavailable",
                    "SET.BAS was found, but its visual-prototype table could "
                    f"not be reconstructed.\n\n{exc}")
                return False
        try:
            family = _public_dependency(
                "load_asset_family", load_asset_family)(path)
        except Exception as exc:
            QMessageBox.critical(
                self, "Load failed", f"No file was modified.\n\n{exc}")
            return False
        if not any(obj.skeleton is not None for obj in family.all_objects()):
            QMessageBox.warning(
                self, "No models",
                "The BASE was parsed, but no SKLT model could be resolved.")
        self.family = family
        self._vp_embedded = embedded
        self._vp_table = vp_table
        self._vp_table_source = vp_source
        self._last_directory = path.parent
        self.project.source_base = path.name
        self._fill_models(family)
        self.source_label.setText(
            f"{path.name}\n"
            f"{len([o for o in family.all_objects() if o.skeleton])} models; "
            f"{len(family.textures)} textures loaded"
            + (f"\nVP source: {vp_source}" if vp_source else ""))
        if vp_warnings:
            self.statusBar().showMessage(" ".join(vp_warnings), 9000)
        self._set_modified()
        return True

    def open_sklt_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import external SKLT", str(self._last_directory),
            "Skeletons (*.sklt *.skl *.SKLT *.SKL);;All files (*)")
        if path:
            self.open_sklt(path)

    def open_sklt(self, path: str | Path):
        self._clear_script_link()
        try:
            family = _public_dependency(
                "load_manual_family", load_manual_family)(path, [], [])
        except Exception as exc:
            QMessageBox.critical(
                self, "Load failed", f"No file was modified.\n\n{exc}")
            return
        if not any(obj.skeleton is not None for obj in family.all_objects()):
            QMessageBox.warning(
                self, "SKLT parse failed",
                "\n".join(family.warnings) or "The model could not be loaded.")
            return
        self.family = family
        self._vp_embedded = None
        self._vp_table = None
        self._vp_table_source = ""
        self._last_directory = Path(path).parent
        self.project.source_base = ""
        self._fill_models(family)
        self.source_label.setText(
            f"{Path(path).name}\nExternal SKLT; geometry-only unless its "
            "material mapping is supplied by a BASE.")
        self._set_modified()

    def _fill_models(self, family: AssetFamily):
        self.model_tree.clear()
        model_index = 0
        vp_ids_by_offset: dict[int, list[int]] = {}
        vp_ids_by_skeleton: dict[str, list[int]] = {}
        if self._vp_embedded is not None and self._vp_table is not None:
            embedded_by_base: dict[str, list] = {}
            for entry in self._vp_embedded.entries:
                embedded_by_base.setdefault(
                    normalize_base_name(entry.base_name), []).append(entry)
            for active_entry in self._vp_table.entries:
                candidates = embedded_by_base.get(
                    normalize_base_name(active_entry.base_name), [])
                selected_entry = None
                # Repeated dummy/BASE assignments are common.  Preserve the
                # positional match when the active table and embedded table
                # agree at this slot; otherwise accept only a unique match.
                if active_entry.index < len(self._vp_embedded.entries):
                    positional = self._vp_embedded.entries[active_entry.index]
                    if (normalize_base_name(positional.base_name)
                            == normalize_base_name(active_entry.base_name)):
                        selected_entry = positional
                if selected_entry is None and len(candidates) == 1:
                    selected_entry = candidates[0]
                if selected_entry is None:
                    continue
                if selected_entry.source_objt_offset >= 0:
                    vp_ids_by_offset.setdefault(
                        selected_entry.source_objt_offset, []).append(
                            active_entry.index)
                key = normalize_skeleton_name(selected_entry.skeleton_name)
                if key:
                    vp_ids_by_skeleton.setdefault(key, []).append(
                        active_entry.index)
        for obj in family.all_objects():
            if obj.skeleton is None:
                continue
            source_offset = int(getattr(
                obj.base_object, "source_objt_offset", -1))
            vp_ids = list(vp_ids_by_offset.get(source_offset, []))
            if not vp_ids and not vp_ids_by_offset:
                skeleton_key = normalize_skeleton_name(
                    obj.base_object.skeleton_name or "")
                candidates = vp_ids_by_skeleton.get(skeleton_key, [])
                if candidates:
                    vp_ids = list(dict.fromkeys(candidates))
            if not vp_ids and self._vp_embedded is None:
                vp_ids = [model_index]
            vp_ids = sorted(set(vp_ids))
            exact_vp = bool(vp_ids)
            vp_text = ", ".join(map(str, vp_ids)) if vp_ids else "—"
            item = QTreeWidgetItem([
                obj.base_object.skeleton_name or obj.owner_path,
                vp_text,
            ])
            full_path = obj.base_object.skeleton_name or obj.owner_path
            item.setData(0, Qt.ItemDataRole.UserRole, obj.owner_path)
            item.setData(0, _MODEL_NAME_ROLE, obj.display_name)
            item.setData(
                0, _MODEL_VP_ROLE, tuple(vp_ids) if exact_vp else ())
            item.setToolTip(0, full_path)
            item.setToolTip(
                1, f"VP {vp_text}" if exact_vp
                else "Child model without its own VP slot")
            item.setTextAlignment(
                1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.model_tree.addTopLevelItem(item)
            model_index += 1
        self.model_tree.resizeColumnToContents(1)
        self._filter_models(self.model_search.text())
        for index in range(self.model_tree.topLevelItemCount()):
            item = self.model_tree.topLevelItem(index)
            if not item.isHidden():
                self.model_tree.setCurrentItem(item)
                break

    def _filter_models(self, text: str) -> None:
        self._model_filter_expansion = filter_asset_tree(
            self.model_tree, text, self._model_item_search_metadata,
            self._model_filter_expansion)

    @staticmethod
    def _model_item_search_metadata(item) -> str:
        display_name = item.data(0, _MODEL_NAME_ROLE) or ""
        owner_path = item.data(0, Qt.ItemDataRole.UserRole) or ""
        return f"{display_name} {owner_path}"

    def _model_changed(self, current, _previous):
        if current is None or self.family is None:
            return
        owner = current.data(0, Qt.ItemDataRole.UserRole)
        self._current_owner = owner
        self.viewport.load_family(
            self.family, {owner}, primary_owner=owner)
        self.viewport.frame_owner(owner)
        self.project.source_model = current.data(0, _MODEL_NAME_ROLE)
        with QSignalBlocker(self.toolbar_view_preset_combo):
            self.toolbar_view_preset_combo.setCurrentText("Current View")
        self._sync_gizmo_camera()
        self._set_modified()
        self._sync_all()

    def _model_bounds(self):
        return self.viewport.local_scaled_owner_bounds(self._current_owner)

    def _default_sphere(self, category: str) -> CollisionSphere:
        bounds = self._model_bounds()
        if bounds is None:
            return CollisionSphere(category, radius=1.0)
        x0, y0, z0, x1, y1, z1 = bounds
        radius = max(x1 - x0, y1 - y0, z1 - z0, 1.0) * 0.5
        return CollisionSphere(
            category,
            (x0 + x1) * 0.5,
            (y0 + y1) * 0.5,
            (z0 + z1) * 0.5,
            radius,
        )

    def add_legacy(self):
        self._push_undo()
        if self.project.legacy is None:
            sphere = self._default_sphere(LEGACY)
            sphere.x = sphere.y = sphere.z = 0.0
            self.project.legacy = sphere
        self._selected = 0
        self._selected_fire_point = -1
        self._selected_tracer_point = -1
        self._set_modified()
        self._sync_all()

    def add_compound(self, category: str):
        self._push_undo()
        self.project.compound.append(self._default_sphere(category))
        self._selected = len(self.project.spheres()) - 1
        self._selected_fire_point = -1
        self._selected_tracer_point = -1
        self._set_modified()
        self._sync_all()

    def create_suggested(self):
        if self._model_bounds() is None:
            QMessageBox.warning(
                self, "No model", "Load and select a model first.")
            return
        self.add_compound(self.target_combo.currentData())

    def duplicate_sphere(self):
        sphere = self._selected_sphere()
        if sphere is None:
            return
        if sphere.category == LEGACY:
            QMessageBox.information(
                self, "Single legacy radius",
                "A project can contain only one Legacy Radius.")
            return
        self._push_undo()
        compound_index = self._selected_compound_index()
        if compound_index is None:
            return
        duplicate = sphere.clone()
        self.project.compound.insert(compound_index + 1, duplicate)
        self._selected = (
            (1 if self.project.legacy is not None else 0)
            + compound_index + 1)
        self._selected_fire_point = -1
        self._selected_tracer_point = -1
        self._set_modified()
        self._sync_all()

    def _populate_change_type_menu(self, menu: QMenu) -> None:
        menu.addAction(self.change_to_legacy_action)
        menu.addAction(self.change_to_vehicle_action)
        menu.addAction(self.change_to_weapon_action)

    def change_sphere_type(self, target_category: str):
        """Convert the selected sphere to an explicitly chosen category."""

        if target_category not in (LEGACY, VEHICLE, WEAPON):
            raise ValueError(
                f"Unsupported collision category: {target_category}")
        sphere = self._selected_sphere()
        if sphere is None or sphere.category == target_category:
            return
        if target_category == LEGACY and self.project.legacy is not None:
            self.statusBar().showMessage(
                "A project can contain only one Legacy Radius.", 5000)
            return

        previous_category = sphere.category
        self._push_undo()
        if target_category == LEGACY:
            compound_index = self._selected_compound_index()
            if compound_index is None:
                return
            del self.project.compound[compound_index]
            sphere.category = LEGACY
            sphere.x = sphere.y = sphere.z = 0.0
            self.project.legacy = sphere
            self._selected = 0
            detail = (
                " Positional offsets were reset because vanilla radius "
                "has no offset.")
        elif previous_category == LEGACY:
            self.project.legacy = None
            sphere.category = target_category
            self.project.compound.insert(0, sphere)
            self._selected = 0
            detail = ""
        else:
            sphere.category = target_category
            detail = ""

        self._selected_fire_point = -1
        self._selected_tracer_point = -1
        self._set_modified()
        self._sync_all()
        self.statusBar().showMessage(
            f"{TYPE_LABELS[previous_category]} changed to "
            f"{TYPE_LABELS[target_category]}.{detail}", 5000)

    def mirror_selected_sphere(self, axis: str):
        """Duplicate a compound sphere across the selected model axis."""

        sphere = self._selected_sphere()
        if sphere is None:
            return
        if sphere.category == LEGACY:
            self.statusBar().showMessage(
                "Legacy Radius is fixed at the origin and cannot be mirrored.",
                5000)
            return
        if axis not in ("x", "y", "z"):
            raise ValueError(f"Unsupported mirror axis: {axis}")
        compound_index = self._selected_compound_index()
        if compound_index is None:
            return
        self._push_undo()
        mirrored = sphere.clone()
        setattr(mirrored, axis, -getattr(mirrored, axis))
        self.project.compound.insert(compound_index + 1, mirrored)
        self._selected = (
            (1 if self.project.legacy is not None else 0)
            + compound_index + 1)
        self._selected_fire_point = -1
        self._selected_tracer_point = -1
        self._set_modified()
        self._sync_all()
        self.statusBar().showMessage(
            f"Mirrored selected sphere across the {axis.upper()} axis.", 4000)

    def delete_sphere(self):
        sphere = self._selected_sphere()
        if sphere is None:
            return
        self._push_undo()
        if sphere.category == LEGACY:
            self.project.legacy = None
        else:
            compound_index = self._selected_compound_index()
            if compound_index is None:
                return
            del self.project.compound[compound_index]
        self._selected = min(
            self._selected, len(self.project.spheres()) - 1)
        self._selected_fire_point = -1
        self._selected_tracer_point = -1
        self._set_modified()
        self._sync_all()

    def reset_collisions(self):
        if not self.project.spheres():
            return
        result = QMessageBox.question(
            self, "Reset Collisions",
            "Delete every Legacy, Vehicle and Weapon collision sphere?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if result != QMessageBox.StandardButton.Yes:
            return
        self._push_undo()
        self.project.legacy = None
        self.project.compound.clear()
        self._selected = -1
        self._selected_fire_point = -1
        self._selected_tracer_point = -1
        self._set_modified()
        self._sync_all()

    def _create_sphere_context_menu(self, index: int) -> QMenu:
        menu = QMenu(self)
        menu.addAction(self.undo_action)
        menu.addAction(self.redo_action)
        menu.addSeparator()
        menu.addAction(self.add_legacy_action)
        menu.addAction(self.add_vehicle_action)
        menu.addAction(self.add_weapon_action)
        menu.addSeparator()
        menu.addAction(self.duplicate_action)
        menu.addAction(self.delete_action)
        change_type_menu = menu.addMenu("Change Sphere Type")
        self._populate_change_type_menu(change_type_menu)
        mirror_menu = menu.addMenu("Mirror Selected Sphere")
        mirror_menu.addAction(self.mirror_x_action)
        mirror_menu.addAction(self.mirror_y_action)
        mirror_menu.addAction(self.mirror_z_action)
        menu.addSeparator()
        menu.addAction(self.reset_collisions_action)
        menu.addAction(self.reset_view_action)
        menu.addSeparator()
        menu.addAction(self.open_base_action)
        menu.addAction(self.open_sklt_action)
        preset_menu = menu.addMenu("View Preset")
        for preset in VIEW_PRESETS:
            action = preset_menu.addAction(preset)
            action.setCheckable(True)
            action.setChecked(
                preset == self.toolbar_view_preset_combo.currentText())
            action.triggered.connect(
                lambda _checked=False, value=preset:
                self.toolbar_view_preset_combo.setCurrentText(value))
        return menu

    def _show_sphere_context_menu(self, index: int, global_pos: QPoint):
        self._create_sphere_context_menu(index).exec(global_pos)

    def _show_sphere_tree_context_menu(self, local_pos: QPoint):
        item = self.sphere_tree.itemAt(local_pos)
        index = -1
        if item is not None:
            self.sphere_tree.setCurrentItem(item)
            candidate = item.data(0, _SPHERE_INDEX_ROLE)
            if isinstance(candidate, int):
                index = candidate
        self._show_sphere_context_menu(
            index, self.sphere_tree.viewport().mapToGlobal(local_pos))

    def _selected_sphere(self):
        spheres = self.project.spheres()
        return spheres[self._selected] if 0 <= self._selected < len(
            spheres) else None

    def _selected_compound_index(self) -> int | None:
        if self._selected < 0:
            return None
        offset = 1 if self.project.legacy is not None else 0
        compound_index = self._selected - offset
        if 0 <= compound_index < len(self.project.compound):
            return compound_index
        return None

    def _select_sphere(self, index: int):
        self._radius_spin_active = False
        self._selected = index
        self._selected_fire_point = -1
        self._selected_tracer_point = -1
        self._sync_all()

    def _select_fire_point(self, index: int):
        self._radius_spin_active = False
        point_count = len(fire_point_positions(self.project))
        self._selected_fire_point = (
            index if (self.project.fire_points_enabled
                      and 0 <= index < point_count) else -1)
        self._selected_tracer_point = -1
        self._selected = -1
        self._sync_all()

    def _select_tracer_point(self, index: int):
        self._radius_spin_active = False
        point_count = len(tracer_point_positions(self.project))
        self._selected_tracer_point = (
            index if (self.project.tracer_points_enabled
                      and 0 <= index < point_count) else -1)
        self._selected_fire_point = -1
        self._selected = -1
        self._sync_all()

    def _fire_point_tree_selection_changed(self, current, _previous):
        if self._syncing or current is None:
            return
        index = current.data(0, _FIRE_POINT_INDEX_ROLE)
        if isinstance(index, int):
            self._select_fire_point(index)

    def _tracer_point_tree_selection_changed(self, current, _previous):
        if self._syncing or current is None:
            return
        index = current.data(0, _TRACER_POINT_INDEX_ROLE)
        if isinstance(index, int):
            self._select_tracer_point(index)

    def _sphere_tree_selection_changed(self, current, _previous):
        if self._syncing or current is None:
            return
        index = current.data(0, _SPHERE_INDEX_ROLE)
        if isinstance(index, int):
            self._select_sphere(index)

    def _sphere_tree_double_clicked(self, item, column: int):
        """Edit Radius in place; the name and runtime index stay read-only."""

        if item is None or column != 1:
            return
        index = item.data(0, _SPHERE_INDEX_ROLE)
        if not isinstance(index, int):
            return
        self.sphere_tree.setCurrentItem(item)
        self.sphere_tree.editItem(item, 1)

    def _sphere_tree_item_changed(self, item, column: int):
        if self._syncing or item is None or column != 1:
            return
        index = item.data(0, _SPHERE_INDEX_ROLE)
        spheres = self.project.spheres()
        if not isinstance(index, int) or not (0 <= index < len(spheres)):
            return
        try:
            value = max(1, int(round(float(item.text(1)))))
        except (TypeError, ValueError):
            with QSignalBlocker(self.sphere_tree):
                item.setText(1, _radius_number(spheres[index].radius))
            self.statusBar().showMessage(
                "Radius must be a positive whole number.", 4000)
            return
        sphere = spheres[index]
        if abs(sphere.radius - value) < 1e-9:
            with QSignalBlocker(self.sphere_tree):
                item.setText(1, str(value))
            return
        self._push_undo()
        self._selected = index
        sphere.radius = float(value)
        self._set_modified()
        self._sync_all()

    def _move_strength_slider_changed(self, value: int):
        with QSignalBlocker(self.move_strength_spin):
            self.move_strength_spin.setValue(value)

    def _move_strength_spin_changed(self, value: int):
        # A typed value may exceed the normal quick-slider range. Expand only
        # while necessary, then restore the useful fine-control range.
        with QSignalBlocker(self.move_strength_slider):
            self.move_strength_slider.setMaximum(max(500, value))
            self.move_strength_slider.setValue(value)

    def _model_preview_scale_changed(self, *_args):
        if self._syncing:
            return
        values = (
            self.model_scale_x_spin.value(),
            self.model_scale_y_spin.value(),
            self.model_scale_z_spin.value(),
        )
        current = (
            self.project.model_scale_x,
            self.project.model_scale_y,
            self.project.model_scale_z,
        )
        if all(abs(a - b) < 1e-9 for a, b in zip(values, current)):
            return
        self._push_undo()
        (self.project.model_scale_x,
         self.project.model_scale_y,
         self.project.model_scale_z) = values
        self.viewport.set_model_preview_scale(*values)
        self._set_modified()
        self._sync_all()

    def _reset_model_preview_scale(self):
        current = (
            self.project.model_scale_x,
            self.project.model_scale_y,
            self.project.model_scale_z,
        )
        if all(abs(value - 1.0) < 1e-9 for value in current):
            return
        self._push_undo()
        self.project.model_scale_x = 1.0
        self.project.model_scale_y = 1.0
        self.project.model_scale_z = 1.0
        self.viewport.set_model_preview_scale(1.0, 1.0, 1.0)
        self._set_modified()
        self._sync_all()

    def _overeof_enabled_changed(self, enabled: bool):
        if self._syncing or self.project.target_category != VEHICLE:
            return
        enabled = bool(enabled)
        if enabled == self.project.overeof_enabled:
            return
        self._overeof_spin_active = False
        self._push_undo()
        self.project.overeof_enabled = enabled
        self._set_modified()
        self._sync_all()

    def _overeof_value_changed(self, value: float):
        if self._syncing or self.project.target_category != VEHICLE:
            return
        value = float(value)
        if not math.isfinite(value) or abs(value - self.project.overeof) < 1e-9:
            return
        if not self._overeof_spin_active:
            self._push_undo()
            self._overeof_spin_active = True
        self.project.overeof = value
        self.project.overeof_enabled = True
        self._set_modified()
        self._sync_all()

    def _finish_overeof_edit(self):
        self._overeof_spin_active = False

    def _suggest_overeof_from_bounds(self):
        bounds = self._model_bounds()
        if bounds is None:
            QMessageBox.warning(
                self, "No model", "Load and select a model first.")
            return
        # OpenUA places the actor origin at ground_y - overeof.  In the
        # model convention used by UA the lowest point is the maximum local Y.
        suggested = float(bounds[4])
        if (self.project.overeof_enabled
                and abs(self.project.overeof - suggested) < 1e-9):
            return
        self._overeof_spin_active = False
        self._push_undo()
        self.project.overeof = suggested
        self.project.overeof_enabled = True
        self._set_modified()
        self._sync_all()
        self.statusBar().showMessage(
            "Suggested Overeof uses the lowest scaled model point; verify "
            "the final terrain contact in-game.", 7000)

    def _reset_overeof(self):
        if (not self.project.overeof_enabled
                and abs(self.project.overeof) < 1e-9):
            return
        self._overeof_spin_active = False
        self._push_undo()
        self.project.overeof_enabled = False
        self.project.overeof = 0.0
        self._set_modified()
        self._sync_all()

    def _begin_vehicle_preview_edit(self, field: str) -> None:
        if field not in self._vehicle_preview_active_edits:
            self._push_undo()
            self._vehicle_preview_active_edits.add(field)

    def _finish_vehicle_preview_edit(self, field: str) -> None:
        self._vehicle_preview_active_edits.discard(field)

    def _fire_points_enabled_changed(self, enabled: bool) -> None:
        if self._syncing or self.project.target_category != VEHICLE:
            return
        enabled = bool(enabled)
        if enabled == self.project.fire_points_enabled:
            return
        self._vehicle_preview_active_edits.clear()
        self._push_undo()
        self.project.fire_points_enabled = enabled
        if not enabled:
            self._selected_fire_point = -1
        self._set_modified()
        self._sync_all()

    def _fire_point_value_changed(self, field: str, value: float) -> None:
        if self._syncing or self.project.target_category != VEHICLE:
            return
        value = float(value)
        if not math.isfinite(value):
            return
        current = float(getattr(self.project, field))
        if abs(current - value) < 1e-9:
            return
        self._begin_vehicle_preview_edit(field)
        setattr(self.project, field, value)
        self.project.fire_points_enabled = True
        self._set_modified()
        self._sync_all()

    def _num_weapons_changed(self, value: int) -> None:
        if self._syncing or self.project.target_category != VEHICLE:
            return
        value = max(0, min(255, int(value)))
        if value == self.project.num_weapons:
            return
        self._begin_vehicle_preview_edit("num_weapons")
        self.project.num_weapons = value
        self.project.fire_points_enabled = True
        point_count = max(1, value)
        if self._selected_fire_point >= point_count:
            self._selected_fire_point = point_count - 1
        self._set_modified()
        self._sync_all()

    def _tracer_points_enabled_changed(self, enabled: bool) -> None:
        if self._syncing or self.project.target_category != VEHICLE:
            return
        enabled = bool(enabled)
        if enabled == self.project.tracer_points_enabled:
            return
        self._vehicle_preview_active_edits.clear()
        self._push_undo()
        self.project.tracer_points_enabled = enabled
        if not enabled:
            self._selected_tracer_point = -1
        self._set_modified()
        self._sync_all()

    def _tracer_point_value_changed(self, field: str, value: float) -> None:
        if self._syncing or self.project.target_category != VEHICLE:
            return
        value = float(value)
        if not math.isfinite(value):
            return
        current = float(getattr(self.project, field))
        if abs(current - value) < 1e-9:
            return
        self._begin_vehicle_preview_edit(field)
        setattr(self.project, field, value)
        self.project.tracer_points_enabled = True
        self._set_modified()
        self._sync_all()

    def _num_mguns_changed(self, value: int) -> None:
        if self._syncing or self.project.target_category != VEHICLE:
            return
        value = max(1, min(255, int(value)))
        if value == self.project.num_mguns:
            return
        self._begin_vehicle_preview_edit("num_mguns")
        self.project.num_mguns = value
        self.project.tracer_points_enabled = True
        if self._selected_tracer_point >= value:
            self._selected_tracer_point = value - 1
        self._set_modified()
        self._sync_all()

    def _gizmo_nudge(self, direction):
        sphere = self._selected_sphere()
        step = float(self.move_strength_spin.value())
        if sphere is not None:
            if sphere.category == LEGACY:
                self.statusBar().showMessage(
                    "Legacy Radius has no script offset and remains at origin.",
                    5000)
                return
            self._push_undo()
            sphere.x += direction[0] * step
            sphere.y += direction[1] * step
            sphere.z += direction[2] * step
            self._set_modified()
            self._sync_all()
            return

        index = self._selected_fire_point
        points = fire_point_positions(self.project)
        tracer_selected = (
            self.project.tracer_points_enabled
            and 0 <= self._selected_tracer_point
            < len(tracer_point_positions(self.project)))
        if not (self.project.fire_points_enabled
                and 0 <= index < len(points)):
            if tracer_selected:
                self._nudge_tracer_points(direction, step)
            return

        dx = float(direction[0]) * step
        dy = float(direction[1]) * step
        dz = float(direction[2]) * step
        count = max(1, int(self.project.num_weapons))
        factor = None
        if dx and count > 1:
            factor = -1.0 + (2.0 * index / (count - 1))
            if abs(factor) <= 1e-9 and not (dy or dz):
                self.statusBar().showMessage(
                    "The center Fire Point of an odd vanilla rack is "
                    "locked to X = 0. Select another point to change "
                    "the symmetric spacing.", 6500)
                return

        self._push_undo()
        changed = False
        if dy:
            self.project.fire_y += dy
            changed = True
        if dz:
            self.project.fire_z += dz
            changed = True
        if dx:
            if count == 1:
                self.project.fire_x += dx
                changed = True
            elif factor is not None and abs(factor) <= 1e-9:
                self.statusBar().showMessage(
                    "The center Fire Point of an odd vanilla rack is "
                    "locked to X = 0. Y/Z movement still moved the full "
                    "rack.", 6500)
            else:
                current_x = factor * abs(float(self.project.fire_x))
                new_extent = max(0.0, (current_x + dx) / factor)
                sign = -1.0 if self.project.fire_x < 0.0 else 1.0
                self.project.fire_x = sign * new_extent
                changed = True
                self.statusBar().showMessage(
                    "Vanilla multi-weapon Fire Points stay symmetric; "
                    "X movement changed the spacing of the full rack.",
                    5000)
        if not changed:
            return
        self.project.fire_points_enabled = True
        self._set_modified()
        self._sync_all()

    def _nudge_tracer_points(self, direction, step: float) -> None:
        index = self._selected_tracer_point
        count = max(1, int(self.project.num_mguns))
        dx = float(direction[0]) * step
        dy = float(direction[1]) * step
        dz = float(direction[2]) * step
        factor = None
        if dx and count > 1:
            factor = -1.0 + (2.0 * index / (count - 1))
            if abs(factor) <= 1e-9 and not (dy or dz):
                self.statusBar().showMessage(
                    "The center Tracer Point is locked to X = 0. Select "
                    "another point to change the symmetric spacing.", 6500)
                return
        old_values = (
            self.project.mgun_tracer_offset_x,
            self.project.mgun_tracer_offset_y,
            self.project.mgun_tracer_offset_z,
        )
        self._push_undo()
        changed = False
        if dy:
            self.project.mgun_tracer_offset_y += dy
            changed = True
        if dz:
            self.project.mgun_tracer_offset_z += dz
            changed = True
        if dx:
            if count == 1:
                self.project.mgun_tracer_offset_x += dx
                changed = True
            elif factor is not None and abs(factor) > 1e-9:
                current_x = factor * abs(
                    float(self.project.mgun_tracer_offset_x))
                new_extent = max(0.0, (current_x + dx) / factor)
                self.project.mgun_tracer_offset_x = new_extent
                changed = True
        new_values = (
            self.project.mgun_tracer_offset_x,
            self.project.mgun_tracer_offset_y,
            self.project.mgun_tracer_offset_z,
        )
        if not changed or new_values == old_values:
            if self._undo:
                self._undo.pop()
            return
        self.project.tracer_points_enabled = True
        self._set_modified()
        self._sync_all()

    def _project_fields_changed(self, *_args):
        if self._syncing:
            return
        self._push_undo()
        self.project.name = self.name_edit.text()
        self.project.target_category = self.target_combo.currentData()
        if self.project.target_category != VEHICLE:
            self._selected_fire_point = -1
            self._selected_tracer_point = -1
        self._set_modified()
        self._sync_all()

    def _visibility_changed(self, visible: bool):
        if self._syncing:
            return
        sphere = self._selected_sphere()
        if sphere is None:
            return
        self._push_undo()
        sphere.visible = bool(visible)
        self._set_modified()
        self._sync_all()

    def _begin_radius_slider(self):
        sphere = self._selected_sphere()
        if sphere is None:
            return
        self._radius_spin_active = False
        self._push_undo()
        self._radius_slider_active = True

    def _radius_slider_changed(self, value: int):
        if self._syncing:
            return
        sphere = self._selected_sphere()
        if sphere is None:
            return
        if not self._radius_slider_active:
            self._push_undo()
        sphere.radius = max(1.0, round(self._slider_to_radius(value)))
        self._set_modified()
        self._sync_all()

    def _finish_radius_slider(self):
        self._radius_slider_active = False

    def _radius_spin_changed(self, value: float):
        if self._syncing:
            return
        sphere = self._selected_sphere()
        if sphere is None:
            return
        if abs(value - sphere.radius) < 1e-9:
            return
        if not self._radius_spin_active:
            self._push_undo()
            self._radius_spin_active = True
        sphere.radius = round(value)
        self._set_modified()
        self._sync_all()

    def _finish_radius_spin_edit(self):
        self._radius_spin_active = False

    def _sphere_display_name(
            self, sphere: CollisionSphere,
            compound_index: int | None = None) -> str:
        if sphere.category == LEGACY:
            return "Legacy Radius"
        if compound_index is None:
            for index, candidate in enumerate(self.project.compound):
                if candidate is sphere:
                    compound_index = index
                    break
        if compound_index is None:
            compound_index = -1
        return f"{TYPE_LABELS[sphere.category]} {compound_index}"

    def _refresh_sphere_tree(self):
        with QSignalBlocker(self.sphere_tree):
            self.sphere_tree.clear()
            selected_item = None
            compound_index = 0
            compound_mode = bool(self.project.compound)
            for flat_index, sphere in enumerate(self.project.spheres()):
                current_compound = None
                if sphere.category != LEGACY:
                    current_compound = compound_index
                    compound_index += 1
                index_text = (
                    "Legacy" if sphere.category == LEGACY
                    else str(current_compound)
                )
                item = QTreeWidgetItem([
                    TYPE_LABELS[sphere.category],
                    _radius_number(sphere.radius),
                    "Visible" if sphere.visible else "",
                    index_text,
                ])
                item.setFlags(
                    item.flags() | Qt.ItemFlag.ItemIsEditable)
                item.setData(0, _SPHERE_INDEX_ROLE, flat_index)
                item.setForeground(0, QBrush(TYPE_COLORS[sphere.category]))
                item.setToolTip(
                    1, "Double-click this Radius value to edit the sphere size.")
                item.setTextAlignment(
                    1, Qt.AlignmentFlag.AlignRight
                    | Qt.AlignmentFlag.AlignVCenter)
                item.setTextAlignment(
                    2, Qt.AlignmentFlag.AlignRight
                    | Qt.AlignmentFlag.AlignVCenter)
                item.setTextAlignment(
                    3, Qt.AlignmentFlag.AlignRight
                    | Qt.AlignmentFlag.AlignVCenter)
                if sphere.category == LEGACY and compound_mode:
                    message = (
                        "Legacy Radius is disabled at runtime because manual "
                        "compound coll_* spheres are present. OpenUA F10 shows "
                        "only the compound collision spheres for this object."
                    )
                    for column in range(4):
                        item.setToolTip(column, message)
                self.sphere_tree.addTopLevelItem(item)
                if flat_index == self._selected:
                    selected_item = item
            if selected_item is not None:
                self.sphere_tree.setCurrentItem(selected_item)
            else:
                self.sphere_tree.setCurrentItem(None)
                self.sphere_tree.clearSelection()

    def _refresh_fire_point_tree(self, points):
        with QSignalBlocker(self.fire_point_tree):
            self.fire_point_tree.clear()
            selected_item = None
            for index, point in enumerate(points):
                item = QTreeWidgetItem([
                    f"F{index + 1}",
                    _number(point[0]), _number(point[1]), _number(point[2]),
                ])
                item.setData(0, _FIRE_POINT_INDEX_ROLE, index)
                item.setForeground(0, QBrush(FIRE_POINT_COLOR))
                for column in (1, 2, 3):
                    item.setTextAlignment(
                        column, Qt.AlignmentFlag.AlignRight
                        | Qt.AlignmentFlag.AlignVCenter)
                self.fire_point_tree.addTopLevelItem(item)
                if index == self._selected_fire_point:
                    selected_item = item
            if selected_item is not None:
                self.fire_point_tree.setCurrentItem(selected_item)
            else:
                self.fire_point_tree.setCurrentItem(None)
                self.fire_point_tree.clearSelection()

    def _refresh_tracer_point_tree(self, points):
        with QSignalBlocker(self.tracer_point_tree):
            self.tracer_point_tree.clear()
            selected_item = None
            for index, point in enumerate(points):
                item = QTreeWidgetItem([
                    f"T{index + 1}",
                    _number(point[0]), _number(point[1]), _number(point[2]),
                ])
                item.setData(0, _TRACER_POINT_INDEX_ROLE, index)
                item.setForeground(0, QBrush(TRACER_POINT_COLOR))
                for column in (1, 2, 3):
                    item.setTextAlignment(
                        column, Qt.AlignmentFlag.AlignRight
                        | Qt.AlignmentFlag.AlignVCenter)
                self.tracer_point_tree.addTopLevelItem(item)
                if index == self._selected_tracer_point:
                    selected_item = item
            if selected_item is not None:
                self.tracer_point_tree.setCurrentItem(selected_item)
            else:
                self.tracer_point_tree.setCurrentItem(None)
                self.tracer_point_tree.clearSelection()

    def _refresh_project_summary_menu(self):
        menu = self.project_summary_menu
        menu.clear()
        vehicle_count = sum(
            sphere.category == VEHICLE for sphere in self.project.compound)
        weapon_count = sum(
            sphere.category == WEAPON for sphere in self.project.compound)
        sphere = self._selected_sphere()
        compound_index = self._selected_compound_index()
        if sphere is not None:
            selected = self._sphere_display_name(sphere, compound_index)
        elif (self.project.target_category == VEHICLE
              and self.project.fire_points_enabled
              and 0 <= self._selected_fire_point
              < len(fire_point_positions(self.project))):
            selected = f"Fire Point F{self._selected_fire_point + 1}"
        elif (self.project.target_category == VEHICLE
              and self.project.tracer_points_enabled
              and 0 <= self._selected_tracer_point
              < len(tracer_point_positions(self.project))):
            selected = f"Tracer Point T{self._selected_tracer_point + 1}"
        else:
            selected = "None"
        compound_mode = bool(self.project.compound)
        if self.project.legacy is None:
            legacy_status = "Missing"
        elif compound_mode:
            legacy_status = "Disabled by compound collisions"
        else:
            legacy_status = "Active"
        collision_mode = (
            "OpenUA compound (Legacy Radius disabled)"
            if compound_mode else "Vanilla Legacy Radius"
        )
        lines = [
            f"Project: {self.project.name or '<unnamed>'}",
            f"Source model: {self.project.source_model or '<none>'}",
            "Linked script object: "
            + (f"{self._active_script_path.name} — "
               f"{self._active_script_kind} {self._active_script_id}"
               if (self._active_script_path is not None
                   and self._active_script_id is not None)
               else "Not linked"),
            "Model preview scale: "
            f"X {_number(self.project.model_scale_x)}  "
            f"Y {_number(self.project.model_scale_y)}  "
            f"Z {_number(self.project.model_scale_z)}",
            "Ground alignment: "
            + ("Not shown for weapon target"
               if self.project.target_category == WEAPON
               else (f"Overeof {_number(self.project.overeof)}"
                     if self.project.overeof_enabled
                     else "Overeof not included")),
            "Fire points: "
            + (f"{max(1, int(self.project.num_weapons))} at "
               f"({_number(self.project.fire_x)}, "
               f"{_number(self.project.fire_y)}, "
               f"{_number(self.project.fire_z)})"
               if (self.project.target_category == VEHICLE
                   and self.project.fire_points_enabled)
               else "Not included"),
            "Tracer points: "
            + (f"{max(1, int(self.project.num_mguns))} at "
               f"({_number(self.project.mgun_tracer_offset_x)}, "
               f"{_number(self.project.mgun_tracer_offset_y)}, "
               f"{_number(self.project.mgun_tracer_offset_z)})"
               if (self.project.target_category == VEHICLE
                   and self.project.tracer_points_enabled)
               else "Not included"),
            f"Collision mode: {collision_mode}",
            f"Legacy Radius: {legacy_status}",
            f"Internal broad-phase extent: "
            f"{_radius_number(effective_runtime_radius(self.project))}",
            f"Vehicle Collision Spheres: {vehicle_count}",
            f"Weapon Collision Spheres: {weapon_count}",
            f"Total Compound Spheres: {len(self.project.compound)}",
            f"Selected Element: {selected}",
        ]
        for line in lines:
            action = menu.addAction(line)
            action.setEnabled(False)

    def _preview_spheres(self) -> list[CollisionSphere]:
        """Return collision preview matching the current OpenUA engine rule.

        Any authored compound ``coll_*`` sphere disables the visible/physical
        Legacy Radius.  Keep an invisible clone in the list so editor selection
        indexes remain stable while the red sphere disappears from viewport
        drawing and picking.
        """

        offset_y = (
            -self.project.overeof
            if (self.project.target_category == VEHICLE
                and self.project.overeof_enabled)
            else 0.0
        )
        previews = []
        for sphere in self.project.spheres():
            preview = sphere.clone()
            preview.y += offset_y
            previews.append(preview)
        if self.project.legacy is not None and self.project.compound:
            previews[0].visible = False
        return previews

    def _sync_all(self):
        self._syncing = True
        blockers = [
            QSignalBlocker(widget) for widget in (
                self.name_edit, self.target_combo, self.radius_slider,
                self.radius_spin, self.visible_check,
                self.model_scale_x_spin, self.model_scale_y_spin,
                self.model_scale_z_spin, self.overeof_check,
                self.overeof_spin, self.fire_points_check,
                self.fire_x_spin, self.fire_y_spin, self.fire_z_spin,
                self.num_weapons_spin, self.tracer_points_check,
                self.tracer_x_spin, self.tracer_y_spin, self.tracer_z_spin,
                self.num_mguns_spin)
        ]
        self.name_edit.setText(self.project.name)
        self.model_scale_x_spin.setValue(self.project.model_scale_x)
        self.model_scale_y_spin.setValue(self.project.model_scale_y)
        self.model_scale_z_spin.setValue(self.project.model_scale_z)
        self.viewport.set_model_preview_scale(
            self.project.model_scale_x, self.project.model_scale_y,
            self.project.model_scale_z)
        vehicle_mode = self.project.target_category == VEHICLE
        fire_count = (len(fire_point_positions(self.project))
                      if (vehicle_mode and self.project.fire_points_enabled)
                      else 0)
        if not (0 <= self._selected_fire_point < fire_count):
            self._selected_fire_point = -1
        tracer_count = (len(tracer_point_positions(self.project))
                        if (vehicle_mode and self.project.tracer_points_enabled)
                        else 0)
        if not (0 <= self._selected_tracer_point < tracer_count):
            self._selected_tracer_point = -1
        self.viewport.set_ground_alignment(
            vehicle_mode, self.project.overeof_enabled,
            self.project.overeof)
        self.target_combo.setCurrentIndex(0 if vehicle_mode else 1)
        self.ground_alignment_box.setVisible(vehicle_mode)
        self.overeof_check.setChecked(
            vehicle_mode and self.project.overeof_enabled)
        self.overeof_spin.setValue(self.project.overeof)
        self.overeof_spin.setEnabled(
            vehicle_mode and self.project.overeof_enabled)
        self.suggest_overeof_button.setEnabled(
            vehicle_mode and self._model_bounds() is not None)
        self.reset_overeof_button.setEnabled(
            vehicle_mode and (self.project.overeof_enabled
                              or abs(self.project.overeof) > 1e-9))
        self.fire_points_box.setVisible(vehicle_mode)
        self.tracer_points_box.setVisible(vehicle_mode)
        self.fire_points_check.setChecked(
            vehicle_mode and self.project.fire_points_enabled)
        self.fire_x_spin.setValue(self.project.fire_x)
        self.fire_y_spin.setValue(self.project.fire_y)
        self.fire_z_spin.setValue(self.project.fire_z)
        self.num_weapons_spin.setValue(
            max(0, min(255, int(self.project.num_weapons))))
        fire_controls_enabled = (
            vehicle_mode and self.project.fire_points_enabled)
        for widget in (
                self.fire_x_spin, self.fire_y_spin, self.fire_z_spin,
                self.num_weapons_spin):
            widget.setEnabled(fire_controls_enabled)
        self.tracer_points_check.setChecked(
            vehicle_mode and self.project.tracer_points_enabled)
        self.tracer_x_spin.setValue(self.project.mgun_tracer_offset_x)
        self.tracer_y_spin.setValue(self.project.mgun_tracer_offset_y)
        self.tracer_z_spin.setValue(self.project.mgun_tracer_offset_z)
        self.num_mguns_spin.setValue(
            max(1, min(255, int(self.project.num_mguns))))
        tracer_controls_enabled = (
            vehicle_mode and self.project.tracer_points_enabled)
        for widget in (
                self.tracer_x_spin, self.tracer_y_spin, self.tracer_z_spin,
                self.num_mguns_spin):
            widget.setEnabled(tracer_controls_enabled)
        sphere = self._selected_sphere()
        fire_selected = (
            vehicle_mode and self.project.fire_points_enabled
            and 0 <= self._selected_fire_point
            < len(fire_point_positions(self.project)))
        tracer_selected = (
            vehicle_mode and self.project.tracer_points_enabled
            and 0 <= self._selected_tracer_point
            < len(tracer_point_positions(self.project)))
        enabled = sphere is not None
        for widget in (
                self.radius_slider, self.radius_spin, self.visible_check):
            widget.setEnabled(enabled)
        self.visible_check.setVisible(enabled)
        self.gizmo.setEnabled(
            (sphere is not None and sphere.category != LEGACY)
            or fire_selected or tracer_selected)
        self.transform_box.setTitle(
            "Move Fire Point" if fire_selected
            else "Move Tracer Point" if tracer_selected
            else "Move Sphere")
        if sphere is None:
            if fire_selected:
                self.type_value.setText("Fire Point")
                self.index_value.setText(str(self._selected_fire_point))
                self.selected_element_label.setText(
                    f"Fire Point F{self._selected_fire_point + 1}")
            elif tracer_selected:
                self.type_value.setText("Tracer Point")
                self.index_value.setText(str(self._selected_tracer_point))
                self.selected_element_label.setText(
                    f"Tracer Point T{self._selected_tracer_point + 1}")
            else:
                self.type_value.setText("None")
                self.index_value.setText("None")
                self.selected_element_label.setText("None")
            self.radius_spin.setValue(1.0)
            self.radius_slider.setValue(0)
            self.visible_check.setChecked(False)
            self.runtime_radius_value.clear()
            self.runtime_radius_value.hide()
            self.radius_spin.setToolTip("")
        else:
            self.selected_element_label.setText(
                self._sphere_display_name(
                    sphere, self._selected_compound_index()))
            self.type_value.setText(TYPE_LABELS[sphere.category])
            if sphere.category == LEGACY:
                index_text = "Legacy"
            else:
                compound_index = self._selected_compound_index()
                index_text = str(compound_index) if compound_index is not None \
                    else "None"
            self.index_value.setText(index_text)
            self.radius_spin.setValue(sphere.radius)
            self.radius_slider.setValue(
                self._radius_to_slider(sphere.radius))
            self.visible_check.setChecked(sphere.visible)
            if sphere.category == LEGACY and self.project.compound:
                self.runtime_radius_value.setText(
                    "Legacy disabled by compound coll_*")
                self.runtime_radius_value.setToolTip(
                    "Manual compound collision spheres replace Legacy Radius. "
                    "OpenUA F10 shows only the compound spheres.")
                self.runtime_radius_value.show()
                self.radius_spin.setToolTip(
                    "Stored authored radius. It is inactive while compound "
                    "coll_* spheres are present.")
            else:
                self.runtime_radius_value.clear()
                self.runtime_radius_value.hide()
                self.radius_spin.setToolTip("")
        self._refresh_sphere_tree()
        self._refresh_project_summary_menu()
        self.viewport.set_collision_spheres(
            self._preview_spheres(), self._selected)
        preview_offset_y = (
            -self.project.overeof
            if (vehicle_mode and self.project.overeof_enabled)
            else 0.0)
        authored_points = []
        points = []
        if vehicle_mode and self.project.fire_points_enabled:
            authored_points = fire_point_positions(self.project)
            points = [
                (x, y + preview_offset_y, z)
                for x, y, z in authored_points
            ]
        self._refresh_fire_point_tree(authored_points)
        self.viewport.set_fire_points(points, self._selected_fire_point)
        authored_tracer_points = []
        preview_tracer_points = []
        if vehicle_mode and self.project.tracer_points_enabled:
            authored_tracer_points = tracer_point_positions(self.project)
            preview_tracer_points = [
                (x, y + preview_offset_y, z)
                for x, y, z in authored_tracer_points
            ]
        self._refresh_tracer_point_tree(authored_tracer_points)
        self.viewport.set_tracer_points(
            preview_tracer_points, self._selected_tracer_point)
        self._sync_gizmo_camera()
        self.undo_action.setEnabled(bool(self._undo))
        self.redo_action.setEnabled(bool(self._redo))
        self.save_loaded_script_action.setEnabled(
            self._active_script_path is not None
            and self._active_script_id is not None)
        self.duplicate_action.setEnabled(
            sphere is not None and sphere.category != LEGACY)
        self.delete_action.setEnabled(sphere is not None)
        self.change_type_action.setEnabled(sphere is not None)
        self.change_type_button.setEnabled(sphere is not None)
        self.change_to_legacy_action.setEnabled(
            sphere is not None and sphere.category != LEGACY
            and self.project.legacy is None)
        self.change_to_vehicle_action.setEnabled(
            sphere is not None and sphere.category != VEHICLE)
        self.change_to_weapon_action.setEnabled(
            sphere is not None and sphere.category != WEAPON)
        mirror_enabled = sphere is not None and sphere.category != LEGACY
        for action in (
                self.mirror_x_action, self.mirror_y_action,
                self.mirror_z_action):
            action.setEnabled(mirror_enabled)
        self.mirror_sphere_button.setEnabled(mirror_enabled)
        self.reset_collisions_action.setEnabled(
            bool(self.project.spheres()))
        del blockers
        self._syncing = False

    def _validate_for_output(self) -> bool:
        errors, warnings = validate_project(
            self.project, self._model_bounds())
        if errors:
            QMessageBox.warning(
                self, "Collision validation failed", "\n".join(errors))
            return False
        if warnings:
            result = QMessageBox.warning(
                self, "Collision validation warnings",
                "\n".join(warnings) + "\n\nContinue?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes)
            return result == QMessageBox.StandardButton.Yes
        return True

    def export_text(self):
        if not self._validate_for_output():
            return
        suggested = (
            f"{self.project.name}_collisions.txt"
            if self.project.name else "collisions.txt")
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Collision Text",
            str(self._last_directory / suggested),
            "Text files (*.txt);;All files (*)")
        if not path:
            return
        try:
            Path(path).write_text(
                export_collision_text(self.project), encoding="utf-8",
                newline="\n")
        except OSError as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        self._last_directory = Path(path).parent
        self._set_modified(False)
        self.statusBar().showMessage(f"Exported {path}", 7000)

    def copy_output(self):
        if not self._validate_for_output():
            return
        QApplication.clipboard().setText(export_collision_text(self.project))
        self.statusBar().showMessage(
            "Collision output copied to clipboard.", 5000)

    def import_collisions(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import collisions from script",
            str(self._last_directory),
            "Text scripts (*.txt *.ini);;All files (*)")
        if not path:
            return
        try:
            text, _encoding, _bom = read_script_file(path)
        except (OSError, UnicodeError) as exc:
            QMessageBox.critical(self, "Import failed", str(exc))
            return
        dialog = ImportCollisionDialog(self, Path(path), text)
        if not dialog.blocks:
            QMessageBox.warning(
                self, "No definitions",
                "No supported new/modify vehicle/weapon block was found.")
            return
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        block, category = dialog.selected()
        try:
            legacy, compound, warnings = import_collision_block(
                text, block, category)
            overeof_enabled, overeof = import_overeof_block(text, block)
            fire_enabled, fire_x, fire_y, fire_z, num_weapons = (
                import_fire_points_block(text, block))
            tracer_probe = CollisionProject(legacy=legacy, compound=compound)
            tracer_enabled, tracer_x, tracer_y, tracer_z, num_mguns = (
                import_tracer_points_block(
                    text, block, effective_runtime_radius(tracer_probe)))
        except CollisionScriptError as exc:
            QMessageBox.warning(self, "Import failed", str(exc))
            return
        self._clear_script_link()
        self._push_undo()
        self.project.legacy = legacy
        self.project.compound = compound
        self.project.target_category = (
            WEAPON if "weapon" in block.kind else VEHICLE)
        self.project.overeof_enabled = (
            self.project.target_category == VEHICLE and overeof_enabled)
        self.project.overeof = (
            overeof if self.project.overeof_enabled else 0.0)
        vehicle_block = self.project.target_category == VEHICLE
        self.project.fire_points_enabled = vehicle_block and fire_enabled
        self.project.fire_x = fire_x if vehicle_block else 0.0
        self.project.fire_y = fire_y if vehicle_block else 0.0
        self.project.fire_z = fire_z if vehicle_block else 0.0
        self.project.num_weapons = num_weapons if vehicle_block else 1
        self.project.tracer_points_enabled = vehicle_block and tracer_enabled
        self.project.mgun_tracer_offset_x = tracer_x if vehicle_block else 0.0
        self.project.mgun_tracer_offset_y = tracer_y if vehicle_block else 0.0
        self.project.mgun_tracer_offset_z = tracer_z if vehicle_block else 0.0
        self.project.num_mguns = num_mguns if vehicle_block else 1
        if block.name:
            self.project.name = block.name
        self._selected = 0 if self.project.spheres() else -1
        self._selected_fire_point = -1
        self._selected_tracer_point = -1
        self._last_directory = Path(path).parent
        self._set_modified()
        self._sync_all()
        message = (
            f"Imported {len(compound)} compound sphere(s)"
            + (" and Legacy Radius" if legacy else "")
            + (" and Overeof" if self.project.overeof_enabled else "")
            + (" and Fire Points" if self.project.fire_points_enabled else "")
            + (" and Tracer Points"
               if self.project.tracer_points_enabled else "")
            + ".")
        if warnings:
            message += "\n\n" + "\n".join(warnings)
            QMessageBox.warning(self, "Imported with warnings", message)
        else:
            self.statusBar().showMessage(message, 7000)

    def apply_to_script(self):
        if not self._validate_for_output():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Apply to Existing Script", str(self._last_directory),
            "OpenUA scripts (*.txt *.scr *.ini *.ldf);;All files (*)")
        if not path:
            return
        self._last_directory = Path(path).parent
        dialog = ApplyScriptDialog(
            self, self.project, initial_path=path)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.statusBar().showMessage(
                f"Script updated. Backup: {dialog.backup_path}", 10000)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self._modified:
            answer = QMessageBox.question(
                self, "Unexported Collision Editor project",
                "The collision project has unexported changes. Close anyway?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        event.accept()
