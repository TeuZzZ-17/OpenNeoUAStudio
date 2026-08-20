"""Manual collision-sphere editor integrated with OpenNeoUAStudio.

The tool deliberately stores only script data.  BASE/SKLT assets stay
read-only and all model loading, texture resolution, camera and rendering are
delegated to the existing OpenNeoUAStudio asset-family and viewport code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import difflib
import math
from pathlib import Path
import re
import shutil

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
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
    QPlainTextEdit,
    QScrollArea,
    QSizePolicy,
)

from assembly_viewer import AssetViewport, VIEW_PRESETS
from asset_family import AssetFamily, load_asset_family, load_manual_family
from model_space_gizmo import ModelSpaceGizmo


LEGACY = "legacy"
VEHICLE = "vehicle"
WEAPON = "weapon"
COMPOUND_TYPES = (VEHICLE, WEAPON)
TYPE_LABELS = {
    LEGACY: "Legacy Radius",
    VEHICLE: "Vehicle Collision",
    WEAPON: "Weapon Collision",
}
# Exact F10 debug colors from OpenNeoUA src/yw_game.cpp.
TYPE_COLORS = {
    LEGACY: QColor(220, 60, 60),
    VEHICLE: QColor(60, 220, 60),
    WEAPON: QColor(60, 130, 235),
}
SCRIPT_TYPES = (
    "new_vehicle", "modify_vehicle", "new_weapon", "modify_weapon",
)
_MODEL_NAME_ROLE = int(Qt.ItemDataRole.UserRole) + 1
_SPHERE_INDEX_ROLE = int(Qt.ItemDataRole.UserRole) + 2
_RADIUS_SLIDER_STEPS = 10000
_RADIUS_LOG_MIN = -3.0
_RADIUS_LOG_MAX = 6.0
_HEADER_RE = re.compile(
    r"^\s*(new_vehicle|modify_vehicle|new_weapon|modify_weapon)"
    r"\s+(-?\d+)\b", re.IGNORECASE,
)
_PARAM_RE = re.compile(
    r"^(?P<indent>\s*)(?P<key>radius|coll_num|coll_act|coll_x|coll_y|"
    r"coll_z|coll_radius)\s*=\s*(?P<value>[^;#\r\n]+)",
    re.IGNORECASE,
)


def _number(value: float) -> str:
    if math.isfinite(value) and abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _radius_number(value: float) -> str:
    """Engine-safe whole-number radius used by lists and text output."""

    return str(int(round(value)))


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
            self.target_category, one(self.legacy),
            tuple(one(sphere) for sphere in self.compound),
        )

    def restore(self, state: tuple) -> None:
        def one(values):
            return None if values is None else CollisionSphere(*values)
        (self.name, self.source_model, self.source_base,
         self.target_category, legacy, compound) = state
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


def collision_data_lines(project: CollisionProject) -> list[str]:
    lines: list[str] = []
    if project.legacy is not None:
        lines.append(f"radius = {_radius_number(project.legacy.radius)}")
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
) -> tuple[str, str, str]:
    """Patch only collision keys in one unambiguous script definition."""

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
    coll_rows = [row for row in rows if row[1].startswith("coll_")]
    indent = next(
        (row[3] for row in rows if row[3]),
        re.match(r"^(\s*)", lines[block.start_line]).group(1) + "    ",
    )
    replace: dict[int, list[str]] = {}
    delete: set[int] = set()
    insert_before_end: list[str] = []

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
    if not project.compound:
        warnings.append("Il progetto non contiene sfere compound.")
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

    @property
    def directions(self) -> tuple[tuple[int, int, int], ...]:
        return (
            (0, 0, 1), (0, 1, 0), (1, 0, 0),
            (0, 0, -1), (0, -1, 0), (-1, 0, 0),
        )

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(220, 160)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(190, 145)


class CollisionViewport(AssetViewport):
    """Existing AssetViewport plus the exact F10 three-ring overlay."""

    spherePicked = Signal(int)
    sphereDragStarted = Signal(int)
    sphereDragged = Signal(int)
    sphereContextMenuRequested = Signal(int, QPoint)
    RING_SEGMENTS = 12

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._collision_spheres: list[CollisionSphere] = []
        self._collision_selected = -1
        self._collision_show = {
            LEGACY: True, VEHICLE: True, WEAPON: True,
        }
        self._collision_show_model = True
        self._sphere_drag_index = -1
        self._sphere_drag_last = QPointF()
        self._sphere_drag_started = False

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

    def _is_sphere_drawn(self, sphere: CollisionSphere) -> bool:
        return (
            sphere.visible and self._collision_show.get(sphere.category, True)
            and sphere.radius > 0 and math.isfinite(sphere.radius)
        )

    def _ring_points(self, sphere: CollisionSphere, axis: int):
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
            camera = self._camera_vertex(world)
            # AssetViewport clamps near projection instead of exposing a
            # clip result.  Its normal editing camera keeps model geometry in
            # front, so use the same projection for exact coordinate parity.
            points.append(self._project(camera))
        return points

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

    def _hit_sphere(self, point: QPointF) -> int:
        candidates = []
        for index, sphere in enumerate(self._collision_spheres):
            if not self._is_sphere_drawn(sphere):
                continue
            center = self._project(self._camera_vertex(sphere.center))
            radius = self._screen_radius(sphere)
            distance = math.hypot(
                point.x() - center.x(), point.y() - center.y())
            edge_distance = abs(distance - radius)
            tolerance = max(8.0, min(18.0, radius * 0.08))
            if edge_distance <= tolerance:
                candidates.append((edge_distance, -index, index))
        return min(candidates)[2] if candidates else -1

    def _draw_mode_label(self, *_args) -> None:
        """Collision assets stay in View Mode without a mode overlay."""

        self._mode_label_rect = None

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Tab:
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
            points = self._ring_points(sphere, axis)
            for start, end in zip(points, points[1:]):
                painter.drawLine(start, end)

    def paintEvent(self, event) -> None:  # noqa: N802
        if self._collision_show_model:
            super().paintEvent(event)
        else:
            painter = QPainter(self)
            painter.fillRect(self.rect(), QColor(24, 26, 32))
            painter.setPen(QColor(130, 135, 145))
            painter.drawText(
                QRectF(self.rect()), Qt.AlignmentFlag.AlignCenter,
                "Model hidden",
            )
            painter.end()

        painter = QPainter(self)
        # OpenNeoUA F10 uses unfilled, aliased one-pixel lines and 12 segments.
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        for index, sphere in enumerate(self._collision_spheres):
            if not self._is_sphere_drawn(sphere):
                continue
            color = TYPE_COLORS[sphere.category]
            if index == self._collision_selected:
                self._draw_collision_rings(
                    painter, sphere, QPen(QColor(255, 255, 255), 4.0))
            self._draw_collision_rings(
                painter, sphere, QPen(color, 1.0))
        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            index = self._hit_sphere(event.position())
            if index >= 0:
                self._collision_selected = index
                self._sphere_drag_index = index
                self._sphere_drag_last = event.position()
                self._sphere_drag_started = False
                self._camera_interacting = False
                self._press_pos = None
                self._last_mouse = event.position().toPoint()
                self.spherePicked.emit(index)
                self.update()
                event.accept()
                return
        if event.button() == Qt.MouseButton.RightButton:
            index = self._hit_sphere(event.position())
            if index >= 0:
                self._collision_selected = index
                self.spherePicked.emit(index)
                self.update()
            self.sphereContextMenuRequested.emit(
                index, event.globalPosition().toPoint())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._sphere_drag_index >= 0 \
                and event.buttons() & Qt.MouseButton.LeftButton:
            current = event.position()
            delta = current - self._sphere_drag_last
            self._sphere_drag_last = current
            sphere = self._collision_spheres[self._sphere_drag_index]
            if sphere.category != LEGACY \
                    and (delta.x() or delta.y()):
                if not self._sphere_drag_started:
                    self.sphereDragStarted.emit(self._sphere_drag_index)
                    self._sphere_drag_started = True
                denominator = max(
                    0.2, 4.0 - self._camera_vertex(sphere.center)[2])
                dx, dy, dz = self._screen_delta_to_world_at_depth(
                    delta.x(), delta.y(), denominator)
                sphere.x += dx
                sphere.y += dy
                sphere.z += dz
                self.sphereDragged.emit(self._sphere_drag_index)
                self.update()
            self._last_mouse = current.toPoint()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._sphere_drag_index >= 0 \
                and event.button() == Qt.MouseButton.LeftButton:
            self._sphere_drag_index = -1
            self._sphere_drag_started = False
            self._camera_interacting = False
            self._press_pos = None
            self._last_mouse = event.position().toPoint()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class ImportCollisionDialog(QDialog):
    def __init__(self, parent, path: Path, text: str) -> None:
        super().__init__(parent)
        self.setWindowTitle("Import Existing Collisions")
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
        note = QLabel(
            "Green/blue is editor-only. Imported coll_* data is unchanged.")
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Open
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected(self):
        return (
            self.block_combo.currentData(),
            self.category_combo.currentData(),
        )


class ApplyScriptDialog(QDialog):
    def __init__(self, parent, project: CollisionProject) -> None:
        super().__init__(parent)
        self.setWindowTitle("Apply Collisions to Script")
        self.resize(820, 620)
        self.project = project
        self.updated_text = ""
        self.backup_path: Path | None = None
        self._source_text = ""
        self._source_encoding = "utf-8"
        self._source_bom = False
        layout = QVBoxLayout(self)
        form = QGridLayout()
        self.path_edit = QLineEdit()
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse)
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
        form.addWidget(QLabel("Script"), 0, 0)
        form.addWidget(self.path_edit, 0, 1)
        form.addWidget(browse, 0, 2)
        form.addWidget(QLabel("Block type"), 1, 0)
        form.addWidget(self.kind_combo, 1, 1, 1, 2)
        form.addWidget(QLabel("Numeric ID"), 2, 0)
        form.addWidget(self.id_spin, 2, 1, 1, 2)
        form.addWidget(QLabel("Detected name"), 3, 0)
        form.addWidget(self.detected_name, 3, 1, 1, 2)
        form.addWidget(self.comment_missing, 4, 0, 1, 3)
        layout.addLayout(form)
        layout.addWidget(QLabel("Preview (only radius/coll_* may change)"))
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
        self.buttons.accepted.connect(self._apply)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        for widget_signal in (
            self.path_edit.textChanged,
            self.kind_combo.currentTextChanged,
            self.id_spin.valueChanged,
            self.comment_missing.toggled,
        ):
            widget_signal.connect(self.refresh_preview)

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select OpenNeoUA script", "",
            "Text scripts (*.txt *.ini);;All files (*)")
        if path:
            self.path_edit.setText(path)

    def refresh_preview(self, *_args):
        self.error_label.clear()
        self.preview.clear()
        self.apply_button.setEnabled(False)
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
    """Manual collision editor built on OpenNeoUAStudio's existing systems."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Collision Editor — OpenNeoUA Studio")
        self.resize(1100, 720)
        self.project = CollisionProject()
        self.family: AssetFamily | None = None
        self._current_owner: str | None = None
        self._selected = -1
        self._modified = False
        self._syncing = False
        self._last_directory = Path.home()
        self._undo: list[tuple] = []
        self._redo: list[tuple] = []
        self._radius_slider_active = False

        self.viewport = CollisionViewport()
        self.viewport.spherePicked.connect(self._select_sphere)
        self.viewport.sphereDragStarted.connect(
            self._begin_mouse_sphere_drag)
        self.viewport.sphereDragged.connect(self._mouse_sphere_dragged)
        self.viewport.sphereContextMenuRequested.connect(
            self._show_sphere_context_menu)
        self.viewport.statusMessage.connect(
            lambda text: self.statusBar().showMessage(text, 4500))
        self.model_tree = QTreeWidget()
        self.model_tree.setHeaderLabels(["Internal path", "VP"])
        self.model_tree.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.model_tree.currentItemChanged.connect(self._model_changed)
        self.sphere_tree = QTreeWidget()
        self.sphere_tree.setHeaderLabels(["Sphere", "Radius"])
        self.sphere_tree.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.sphere_tree.currentItemChanged.connect(
            self._sphere_tree_selection_changed)

        self._build_actions()
        self._build_ui()
        self._sync_all()
        self.statusBar().showMessage(
            "BASE/SKLT assets are read-only; only explicit text exports or "
            "confirmed script application write files.")

    def _build_actions(self):
        file_menu = self.menuBar().addMenu("&File")
        self.file_menu = file_menu
        self.open_base_action = QAction("Open BAS Archive...", self)
        self.open_base_action.setShortcut(QKeySequence.StandardKey.Open)
        self.open_base_action.triggered.connect(self.open_base_dialog)
        file_menu.addAction(self.open_base_action)
        self.open_sklt_action = QAction("Open SKLT...", self)
        self.open_sklt_action.triggered.connect(self.open_sklt_dialog)
        file_menu.addAction(self.open_sklt_action)
        file_menu.addSeparator()
        import_action = QAction("Import Existing Collisions...", self)
        import_action.triggered.connect(self.import_collisions)
        file_menu.addAction(import_action)
        export_action = QAction("Export Collision Text...", self)
        export_action.triggered.connect(self.export_text)
        file_menu.addAction(export_action)
        copy_action = QAction("Copy Output to Clipboard", self)
        copy_action.triggered.connect(self.copy_output)
        file_menu.addAction(copy_action)
        apply_action = QAction("Apply to Existing Script...", self)
        apply_action.triggered.connect(self.apply_to_script)
        file_menu.addAction(apply_action)

        edit_menu = self.menuBar().addMenu("&Edit")
        self.undo_action = QAction("Undo", self)
        self.undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        self.undo_action.triggered.connect(self.undo)
        edit_menu.addAction(self.undo_action)
        self.redo_action = QAction("Redo", self)
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
        self.reset_collisions_action = QAction("Reset Collisions", self)
        self.reset_collisions_action.triggered.connect(self.reset_collisions)
        for action in (
                self.add_legacy_action, self.add_vehicle_action,
                self.add_weapon_action, self.duplicate_action,
                self.delete_action, self.reset_collisions_action):
            edit_menu.addAction(action)

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
        toolbar.addWidget(QLabel(" View preset: "))
        self.toolbar_view_preset_combo = QComboBox()
        self.toolbar_view_preset_combo.addItems(VIEW_PRESETS)
        self.toolbar_view_preset_combo.currentTextChanged.connect(
            self._on_view_preset_changed)
        toolbar.addWidget(self.toolbar_view_preset_combo)
        toolbar.addSeparator()
        for action in (
                self.open_base_action, self.open_sklt_action,
                self.undo_action, self.redo_action):
            toolbar.addAction(action)
        toolbar.addSeparator()
        for action in (
                self.add_legacy_action, self.add_vehicle_action,
                self.add_weapon_action, self.duplicate_action,
                self.delete_action, self.reset_collisions_action):
            toolbar.addAction(action)
        self.reset_view_action = QAction("Reset View", self)
        self.reset_view_action.triggered.connect(self._reset_view)
        toolbar.addAction(self.reset_view_action)
        self.viewport.manualCameraChanged.connect(
            self._on_manual_camera_changed)

    def _build_ui(self):
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.setCentralWidget(splitter)

        source_panel = QWidget()
        source_panel.setMaximumWidth(195)
        source_layout = QVBoxLayout(source_panel)
        source_layout.addWidget(QLabel("Resources in archive"))
        source_layout.addWidget(self.model_tree, 1)
        self.source_label = QLabel("No source loaded.")
        self.source_label.setWordWrap(True)
        source_layout.addWidget(self.source_label)
        self.model_tree.header().setMinimumSectionSize(28)
        self.model_tree.setColumnWidth(0, 140)
        self.model_tree.setColumnWidth(1, 35)
        splitter.addWidget(source_panel)
        splitter.addWidget(self.viewport)

        properties = QWidget()
        right = QVBoxLayout(properties)
        right.setContentsMargins(5, 5, 5, 5)
        right.setSpacing(4)
        project_box = QGroupBox("Project")
        project_form = QFormLayout(project_box)
        project_form.setRowWrapPolicy(
            QFormLayout.RowWrapPolicy.WrapLongRows)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Wasp")
        self.name_edit.editingFinished.connect(self._project_fields_changed)
        self.target_combo = QComboBox()
        self.target_combo.addItem("vehicle", VEHICLE)
        self.target_combo.addItem("weapon", WEAPON)
        self.target_combo.currentIndexChanged.connect(
            self._project_fields_changed)
        project_form.addRow("Vehicle / Weapon Name", self.name_edit)
        project_form.addRow("Target category", self.target_combo)
        right.addWidget(project_box)

        selected_box = QGroupBox("Selected Sphere")
        selected_form = QFormLayout(selected_box)
        self.type_value = QLabel("None")
        self.index_value = QLabel("None")
        self.visible_check = QCheckBox("Active / visible")
        self.visible_check.toggled.connect(self._visibility_changed)
        selected_form.addRow("Type", self.type_value)
        selected_form.addRow("Index", self.index_value)
        selected_form.addRow(self.visible_check)

        spheres_box = QGroupBox("Spheres")
        spheres_layout = QVBoxLayout(spheres_box)
        self.sphere_tree.setMinimumHeight(90)
        self.sphere_tree.setMaximumHeight(125)
        spheres_layout.addWidget(self.sphere_tree)

        radius_box = QGroupBox("Sphere Radius")
        radius_layout = QVBoxLayout(radius_box)
        self.radius_slider = QSlider(Qt.Orientation.Horizontal)
        self.radius_slider.setRange(0, _RADIUS_SLIDER_STEPS)
        self.radius_slider.sliderPressed.connect(
            self._begin_radius_slider)
        self.radius_slider.valueChanged.connect(
            self._radius_slider_changed)
        self.radius_slider.sliderReleased.connect(
            self._finish_radius_slider)
        radius_layout.addWidget(self.radius_slider)
        radius_row = QHBoxLayout()
        radius_row.addWidget(QLabel("Radius"))
        self.radius_spin = self._coordinate_spin()
        self.radius_spin.setMinimum(1.0)
        self.radius_spin.setDecimals(0)
        self.radius_spin.setSingleStep(1.0)
        self.radius_spin.editingFinished.connect(
            self._radius_spin_changed)
        radius_row.addWidget(self.radius_spin)
        radius_layout.addLayout(radius_row)
        right.addWidget(radius_box)

        transform_box = QGroupBox("Move Sphere")
        transform_layout = QVBoxLayout(transform_box)
        transform_layout.setContentsMargins(6, 6, 6, 6)
        transform_layout.setSpacing(3)
        self.gizmo = CollisionMoveGizmo()
        self.gizmo.setMinimumSize(190, 145)
        self.gizmo.setMaximumHeight(165)
        self.gizmo.directionTriggered.connect(self._gizmo_nudge)
        transform_layout.addWidget(self.gizmo)
        strength_row = QHBoxLayout()
        strength_row.addWidget(QLabel("Move strength"))
        self.move_strength_slider = QSlider(Qt.Orientation.Horizontal)
        self.move_strength_slider.setRange(1, 500)
        self.move_strength_slider.setValue(1)
        self.move_strength_value = QLabel("1")
        self.move_strength_value.setMinimumWidth(28)
        self.move_strength_slider.valueChanged.connect(
            lambda value: self.move_strength_value.setText(str(value)))
        strength_row.addWidget(self.move_strength_slider, 1)
        strength_row.addWidget(self.move_strength_value)
        transform_layout.addLayout(strength_row)
        transform_box.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        right.addWidget(transform_box)
        right.addWidget(spheres_box)
        right.addWidget(selected_box)

        suggested = QPushButton("Create Suggested Sphere")
        suggested.clicked.connect(self.create_suggested)
        right.addWidget(suggested)
        output_row = QGridLayout()
        for position, (text, slot) in enumerate((
            ("Export Collision Text", self.export_text),
            ("Copy Output to Clipboard", self.copy_output),
            ("Import Existing", self.import_collisions),
            ("Apply to Script", self.apply_to_script),
        )):
            button = QPushButton(text)
            button.clicked.connect(slot)
            button.setSizePolicy(
                QSizePolicy.Policy.Ignored,
                QSizePolicy.Policy.Fixed)
            output_row.addWidget(button, position // 2, position % 2)
        right.addLayout(output_row)
        properties_scroll = QScrollArea()
        properties_scroll.setWidgetResizable(True)
        properties_scroll.setFrameShape(QFrame.Shape.NoFrame)
        properties_scroll.setMinimumWidth(275)
        properties_scroll.setWidget(properties)
        splitter.addWidget(properties_scroll)
        splitter.setSizes([180, 640, 280])

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
        title = "Collision Editor — OpenNeoUA Studio"
        self.setWindowTitle(("* " if self._modified else "") + title)

    def undo(self):
        if not self._undo:
            return
        self._redo.append(self.project.snapshot())
        self.project.restore(self._undo.pop())
        self._selected = min(
            self._selected, len(self.project.spheres()) - 1)
        self._set_modified()
        self._sync_all()

    def redo(self):
        if not self._redo:
            return
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
            self, "Open BAS Archive", str(self._last_directory),
            "BAS archives (SET.BAS *.bas *.BAS);;All files (*)")
        if path:
            self.open_base(path)

    def open_base(self, path: str | Path):
        try:
            family = load_asset_family(path)
        except Exception as exc:
            QMessageBox.critical(
                self, "Load failed", f"No file was modified.\n\n{exc}")
            return
        if not any(obj.skeleton is not None for obj in family.all_objects()):
            QMessageBox.warning(
                self, "No models",
                "The BASE was parsed, but no SKLT model could be resolved.")
        self.family = family
        self._last_directory = Path(path).parent
        self.project.source_base = Path(path).name
        self._fill_models(family)
        self.source_label.setText(
            f"{Path(path).name}\n"
            f"{len([o for o in family.all_objects() if o.skeleton])} models; "
            f"{len(family.textures)} textures loaded")
        self._set_modified()

    def open_sklt_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open external SKLT", str(self._last_directory),
            "Skeletons (*.sklt *.skl *.SKLT *.SKL);;All files (*)")
        if path:
            self.open_sklt(path)

    def open_sklt(self, path: str | Path):
        try:
            family = load_manual_family(path, [], [])
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
        first = None
        for obj in family.all_objects():
            if obj.skeleton is None:
                continue
            item = QTreeWidgetItem([
                obj.base_object.skeleton_name or obj.owner_path,
                str(model_index),
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, obj.owner_path)
            item.setData(0, _MODEL_NAME_ROLE, obj.display_name)
            self.model_tree.addTopLevelItem(item)
            first = first or item
            model_index += 1
        self.model_tree.resizeColumnToContents(0)
        if first is not None:
            self.model_tree.setCurrentItem(first)

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
        return self.viewport._owner_bounds.get(self._current_owner)

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
        self._set_modified()
        self._sync_all()

    def add_compound(self, category: str):
        self._push_undo()
        self.project.compound.append(self._default_sphere(category))
        self._selected = len(self.project.spheres()) - 1
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
        compound_index = self.project.compound.index(sphere)
        duplicate = sphere.clone()
        self.project.compound.insert(compound_index + 1, duplicate)
        self._selected = (
            (1 if self.project.legacy is not None else 0)
            + compound_index + 1)
        self._set_modified()
        self._sync_all()

    def delete_sphere(self):
        sphere = self._selected_sphere()
        if sphere is None:
            return
        self._push_undo()
        if sphere.category == LEGACY:
            self.project.legacy = None
        else:
            self.project.compound.remove(sphere)
        self._selected = min(
            self._selected, len(self.project.spheres()) - 1)
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
        self._set_modified()
        self._sync_all()

    def _create_sphere_context_menu(self, index: int) -> QMenu:
        menu = QMenu(self)
        if index >= 0:
            menu.addAction(self.duplicate_action)
            menu.addAction(self.delete_action)
            menu.addSeparator()
        menu.addAction(self.reset_collisions_action)
        return menu

    def _show_sphere_context_menu(self, index: int, global_pos: QPoint):
        self._create_sphere_context_menu(index).exec(global_pos)

    def _begin_mouse_sphere_drag(self, index: int):
        self._selected = index
        self._push_undo()

    def _mouse_sphere_dragged(self, index: int):
        self._selected = index
        self._set_modified()
        self._sync_all()

    def _selected_sphere(self):
        spheres = self.project.spheres()
        return spheres[self._selected] if 0 <= self._selected < len(
            spheres) else None

    def _select_sphere(self, index: int):
        self._selected = index
        self._sync_all()

    def _sphere_tree_selection_changed(self, current, _previous):
        if self._syncing or current is None:
            return
        index = current.data(0, _SPHERE_INDEX_ROLE)
        if isinstance(index, int):
            self._select_sphere(index)

    def _gizmo_nudge(self, direction):
        sphere = self._selected_sphere()
        if sphere is None:
            return
        step = float(self.move_strength_slider.value())
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

    def _project_fields_changed(self, *_args):
        if self._syncing:
            return
        self._push_undo()
        self.project.name = self.name_edit.text()
        self.project.target_category = self.target_combo.currentData()
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

    def _radius_spin_changed(self):
        if self._syncing:
            return
        sphere = self._selected_sphere()
        if sphere is None:
            return
        value = self.radius_spin.value()
        if abs(value - sphere.radius) < 1e-9:
            return
        self._push_undo()
        sphere.radius = round(value)
        self._set_modified()
        self._sync_all()

    def _sphere_display_name(self, sphere: CollisionSphere) -> str:
        if sphere.category == LEGACY:
            return "Legacy Radius"
        index = self.project.compound.index(sphere)
        return f"{TYPE_LABELS[sphere.category]} {index}"

    def _refresh_sphere_tree(self):
        with QSignalBlocker(self.sphere_tree):
            self.sphere_tree.clear()
            selected_item = None
            for flat_index, sphere in enumerate(self.project.spheres()):
                item = QTreeWidgetItem([
                    self._sphere_display_name(sphere),
                    _radius_number(sphere.radius),
                ])
                item.setData(0, _SPHERE_INDEX_ROLE, flat_index)
                item.setForeground(0, QBrush(TYPE_COLORS[sphere.category]))
                self.sphere_tree.addTopLevelItem(item)
                if flat_index == self._selected:
                    selected_item = item
            if selected_item is not None:
                self.sphere_tree.setCurrentItem(selected_item)
        self.sphere_tree.resizeColumnToContents(0)

    def _refresh_project_summary_menu(self):
        menu = self.project_summary_menu
        menu.clear()
        vehicle_count = sum(
            sphere.category == VEHICLE for sphere in self.project.compound)
        weapon_count = sum(
            sphere.category == WEAPON for sphere in self.project.compound)
        sphere = self._selected_sphere()
        selected = (
            self._sphere_display_name(sphere) if sphere is not None else "None")
        lines = [
            f"Project: {self.project.name or '<unnamed>'}",
            f"Source model: {self.project.source_model or '<none>'}",
            f"Legacy Radius: "
            f"{'Present' if self.project.legacy is not None else 'Missing'}",
            f"Vehicle Collision Spheres: {vehicle_count}",
            f"Weapon Collision Spheres: {weapon_count}",
            f"Total Compound Spheres: {len(self.project.compound)}",
            f"Selected Sphere: {selected}",
        ]
        for line in lines:
            action = menu.addAction(line)
            action.setEnabled(False)

    def _sync_all(self):
        self._syncing = True
        blockers = [
            QSignalBlocker(widget) for widget in (
                self.name_edit, self.target_combo, self.radius_slider,
                self.radius_spin, self.visible_check)
        ]
        self.name_edit.setText(self.project.name)
        self.target_combo.setCurrentIndex(
            0 if self.project.target_category == VEHICLE else 1)
        sphere = self._selected_sphere()
        enabled = sphere is not None
        for widget in (
                self.radius_slider, self.radius_spin, self.visible_check):
            widget.setEnabled(enabled)
        self.gizmo.setEnabled(
            sphere is not None and sphere.category != LEGACY)
        if sphere is None:
            self.type_value.setText("None")
            self.index_value.setText("None")
            self.radius_spin.setValue(1.0)
            self.radius_slider.setValue(0)
            self.visible_check.setChecked(False)
        else:
            self.type_value.setText(TYPE_LABELS[sphere.category])
            if sphere.category == LEGACY:
                index_text = "Legacy"
            else:
                index_text = str(self.project.compound.index(sphere))
            self.index_value.setText(index_text)
            self.radius_spin.setValue(sphere.radius)
            self.radius_slider.setValue(
                self._radius_to_slider(sphere.radius))
            self.visible_check.setChecked(sphere.visible)
        self._refresh_sphere_tree()
        self._refresh_project_summary_menu()
        self.viewport.set_collision_spheres(
            self.project.spheres(), self._selected)
        self._sync_gizmo_camera()
        self.undo_action.setEnabled(bool(self._undo))
        self.redo_action.setEnabled(bool(self._redo))
        self.duplicate_action.setEnabled(
            sphere is not None and sphere.category != LEGACY)
        self.delete_action.setEnabled(sphere is not None)
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
        except CollisionScriptError as exc:
            QMessageBox.warning(self, "Import failed", str(exc))
            return
        self._push_undo()
        self.project.legacy = legacy
        self.project.compound = compound
        self.project.target_category = (
            WEAPON if "weapon" in block.kind else VEHICLE)
        if block.name:
            self.project.name = block.name
        self._selected = 0 if self.project.spheres() else -1
        self._last_directory = Path(path).parent
        self._set_modified()
        self._sync_all()
        message = (
            f"Imported {len(compound)} compound sphere(s)"
            + (" and Legacy Radius." if legacy else "."))
        if warnings:
            message += "\n\n" + "\n".join(warnings)
            QMessageBox.warning(self, "Imported with warnings", message)
        else:
            self.statusBar().showMessage(message, 7000)

    def apply_to_script(self):
        if not self._validate_for_output():
            return
        dialog = ApplyScriptDialog(self, self.project)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.statusBar().showMessage(
                f"Script updated. Backup: {dialog.backup_path}", 10000)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self._modified:
            answer = QMessageBox.question(
                self, "Unsaved Collision Editor project",
                "The collision project has unexported changes. Close anyway?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        event.accept()
