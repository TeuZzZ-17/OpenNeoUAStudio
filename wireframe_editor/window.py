"""OpenNeoUA Studio Wireframe Editor window.

This is the complete former 2D editor, integrated into the same QApplication
as the 3D/SET.BAS workbench.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QAction, QColor, QKeySequence, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QAbstractSpinBox,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QTextEdit,
    QToolButton,
    QAbstractItemView,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QLabel,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QSizePolicy,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from studio.ui.widgets import configure_operation_status_bar, install_standard_file_menu_tail
from studio.workbench.outline import (
    HUD_PROJECTION_AXES,
    EditorState,
    OutlineEditor,
    OutlineSession,
    ReferenceLayer,
)
from studio.formats.sklt import (
    SkltModel,
    SkltParseError,
    create_minimal_sklt_model,
    parse_sklt_file,
    save_sklt_with_otl2_points,
    save_sklt_with_poo2_pol2_structure,
    save_sklt_with_poo2_points,
)
APP_TITLE = "OpenNeoUA Studio - Wireframe Editor"

# Wireframe keys of a definition and their short names, in display order.
# mgun_wireframe is the OpenNeoUA MGUN info wireframe, separate from mg_wireframe.
VEHICLE_WIREFRAME_KEYS = (
    ("hud_wireframe", "HUD"),
    ("wpn_wireframe_1", "Weapon 1"),
    ("wpn_wireframe_2", "Weapon 2"),
    ("mg_wireframe", "MG"),
    ("mgun_wireframe", "MGUN"),
    ("wireframe", "Unit"),
)
WEAPON_WIREFRAME_KEYS = (("wireframe", "Weapon"),)

# The visor is drawn as on a 4:3 screen (GFXEngine::ApplyResolution keeps that
# ratio on wide screens): one wireframe unit is 4/3 wider than it is tall.
VISOR_ASPECT = 4.0 / 3.0
# Half the screen in wireframe units: yw_RenderVector2D maps 1000 units to 1.0.
VISOR_HALF_HEIGHT = 1000.0
# Scale of each visor part as yw_RenderHUDTarget draws it, centred, with no target
# locked: the HUD fills the screen, the weapon and MG sights are small.
VISOR_SCALES = {
    "hud_wireframe": (0.99, 0.99),
    "wpn_wireframe_1": (0.3, 0.4),
    "wpn_wireframe_2": (0.3, 0.4),
    "mg_wireframe": (0.3, 0.4),
}

Placement = tuple[float, float, float, float]


@dataclass
class WireComponent:
    """One wireframe file of the open vehicle or weapon.

    ``placement`` (scale x, scale y, offset x, offset y) puts it into the shared
    preview: the visor parts as the game assembles them, the others below.
    ``session`` is out of date while the part is the one being edited.
    """

    label: str
    path: Path
    session: OutlineSession
    key: str = ""
    placement: Placement = (1.0, 1.0, 0.0, 0.0)


def wireframe_parts(obj) -> tuple[list[tuple[str, Path, str]], list[str]]:
    """Wireframe files of a game object (label, path, first key) and the keys that point to no file.

    One entry per file: keys that share a file (often both weapon slots)
    are edited once, with both names in the label.
    """
    from entity_designer.wireframe_panel import resolve_wireframe_path

    keys = VEHICLE_WIREFRAME_KEYS if obj.kind == "vehicle" else WEAPON_WIREFRAME_KEYS
    found: dict[Path, tuple[list[str], str]] = {}
    missing: list[str] = []
    for key, label in keys:
        value = obj.values.get(key)
        if not value:
            continue
        path = resolve_wireframe_path(value, obj.source)
        if path is None:
            missing.append(f"{key} = {value}")
            continue
        found.setdefault(path.resolve(), ([], key))[0].append(label)
    return [(" + ".join(names), path, key) for path, (names, key) in found.items()], missing


def _state_points(state: EditorState) -> list[tuple[float, float]]:
    """Points of a document as the canvas shows them."""
    if state.mode == "otl2":
        return [(float(x), float(y)) for x, y in state.points_2d]
    if state.mode == "poo2":
        first, second = HUD_PROJECTION_AXES
        return [(point[first], point[second]) for point in state.points_3d]
    return []


def _placed(points, placement: Placement) -> tuple[tuple[float, float], ...]:
    scale_x, scale_y, offset_x, offset_y = placement
    return tuple((x * scale_x + offset_x, y * scale_y + offset_y) for x, y in points)


def visor_frame() -> ReferenceLayer:
    """The 4:3 screen the visor parts are drawn on."""
    half_width = VISOR_HALF_HEIGHT * VISOR_ASPECT
    corners = (
        (-half_width, VISOR_HALF_HEIGHT), (half_width, VISOR_HALF_HEIGHT),
        (half_width, -VISOR_HALF_HEIGHT), (-half_width, -VISOR_HALF_HEIGHT),
    )
    return ReferenceLayer("Screen", corners, ((0, 1, 2, 3),), selectable=False)


def assemble_parts(keys: list[str], point_sets: list[list[tuple[float, float]]]) -> list[Placement]:
    """Place the wireframes of one definition like the game shows them.

    Visor parts (HUD, weapon and MG sights) are drawn over each other in the
    4:3 screen, with the engine's scales. The other wireframes (unit outline,
    MGUN, a weapon's own wireframe) keep their size and sit in a row below the
    screen, or centred when there is no visor.
    """
    placements: list[Placement] = [(1.0, 1.0, 0.0, 0.0)] * len(keys)
    has_visor = any(key in VISOR_SCALES for key in keys)
    for index, key in enumerate(keys):
        if key in VISOR_SCALES:
            scale_x, scale_y = VISOR_SCALES[key]
            placements[index] = (scale_x * VISOR_ASPECT, scale_y, 0.0, 0.0)

    others = [index for index, key in enumerate(keys) if key not in VISOR_SCALES]
    if not has_visor and len(others) <= 1:
        return placements
    boxes = {}
    for index in others:
        points = point_sets[index] or [(0.0, 0.0)]
        boxes[index] = (min(x for x, _ in points), max(x for x, _ in points),
                        min(y for _, y in points), max(y for _, y in points))
    gap = VISOR_HALF_HEIGHT * 0.25
    total_width = sum(box[1] - box[0] for box in boxes.values()) + gap * (len(boxes) - 1)
    top = -VISOR_HALF_HEIGHT - gap if has_visor else 0.0
    cursor = -total_width * 0.5
    for index in others:
        min_x, max_x, min_y, max_y = boxes[index]
        # Canvas Y grows upwards: the row hangs below the screen.
        placements[index] = (1.0, 1.0, cursor - min_x, top - max_y)
        cursor += max_x - min_x + gap
    return placements


def _write_wireframe(model: SkltModel, state: EditorState, output_path: Path) -> Path | None:
    """Write one edited document; the single writer of the Overwrite and Export paths."""
    if state.mode == "otl2":
        return save_sklt_with_otl2_points(model, list(state.points_2d), output_path)
    if state.mode == "poo2":
        points = list(state.points_3d)
        polygons = [list(group) for group in state.groups]
        if len(points) != len(model.points) or polygons != model.polygons:
            return save_sklt_with_poo2_pol2_structure(model, points, polygons, output_path)
        return save_sklt_with_poo2_points(model, points, output_path)
    raise SkltParseError("No editable data is available to export.")


MirrorGuide = tuple[tuple[float, float], tuple[float, float], bool]


def _match_mirror_vertices(
    points: list[tuple[float, float]], scale: float
) -> tuple[dict[int, int], set[int]]:
    """Match existing vertices around the X=0 mirror axis (left <-> right).

    Points are (x, z). A vehicle is symmetric between its left and right side,
    so the mirror of (x, z) is (-x, z).

    Pairing is intentionally conservative: only vertices already reasonably
    close to a mirrored counterpart are linked. This prevents Mirror Symmetry
    from grabbing unrelated geometry in an asymmetric model.
    """
    safe_scale = max(abs(float(scale)), 1.0e-9)
    axis_tolerance = 6.0 / safe_scale
    pair_tolerance = 50.0 / safe_scale

    center_indices = {
        index for index, (x_value, _z_value) in enumerate(points)
        if abs(x_value) <= axis_tolerance
    }
    left_indices = [
        index for index, (x_value, _z_value) in enumerate(points)
        if x_value < -axis_tolerance
    ]
    right_indices = [
        index for index, (x_value, _z_value) in enumerate(points)
        if x_value > axis_tolerance
    ]

    candidates: list[tuple[float, int, int]] = []
    for left in left_indices:
        left_x, left_z = points[left]
        target_x = -left_x
        for right in right_indices:
            right_x, right_z = points[right]
            distance = ((right_x - target_x) ** 2 + (right_z - left_z) ** 2) ** 0.5
            if distance <= pair_tolerance:
                candidates.append((distance, left, right))

    pairs: dict[int, int] = {}
    used_left: set[int] = set()
    used_right: set[int] = set()
    for _distance, left, right in sorted(candidates):
        if left in used_left or right in used_right:
            continue
        used_left.add(left)
        used_right.add(right)
        pairs[left] = right
        pairs[right] = left

    return pairs, center_indices


def _swap_axes(point: tuple[float, float]) -> tuple[float, float]:
    """Swap (x, z) <-> (z, x): lets the X=0 mirror code also work on the Z=0 axis."""
    return (point[1], point[0])


def _choose_mirror_axis(
    points: list[tuple[float, float]], scale: float
) -> tuple[int, dict[int, int], set[int]]:
    """Pick the mirror axis that matches the shape.

    Axis 0 mirrors left <-> right (X=0, vehicles drawn nose up). Axis 1 mirrors
    top <-> bottom (Z=0, shapes drawn lying sideways, e.g. missiles). The axis
    with more mirrored vertex pairs wins; on a tie axis 0 is kept.
    """
    pairs_x, center_x = _match_mirror_vertices(points, scale)
    swapped = [_swap_axes(point) for point in points]
    pairs_z, center_z = _match_mirror_vertices(swapped, scale)
    if len(pairs_z) > len(pairs_x):
        return 1, pairs_z, center_z
    return 0, pairs_x, center_x


def _apply_mirror_positions(
    source_positions: dict[int, tuple[float, float]],
    pair_map: dict[int, int],
    center_indices: set[int],
    drag_start_points: dict[int, tuple[float, float]],
) -> tuple[dict[int, tuple[float, float]], list[MirrorGuide]]:
    """Return one symmetric move, including counterpart vertices and guides."""
    positions = dict(source_positions)
    guides: list[MirrorGuide] = []
    source_indices = set(source_positions)
    processed_pairs: set[tuple[int, int]] = set()

    for index in sorted(source_indices):
        point = source_positions[index]
        # A vertex on the axis stays on it: it can only slide along Z.
        if index in center_indices:
            positions[index] = (0.0, point[1])
            continue

        partner = pair_map.get(index)
        if partner is None:
            mirror_target = (-point[0], point[1])
            guides.append((positions[index], mirror_target, False))
            continue

        pair_key = (min(index, partner), max(index, partner))
        if pair_key in processed_pairs:
            continue
        processed_pairs.add(pair_key)

        if partner in source_indices:
            # Both sides are being dragged: average them so the pair stays
            # exactly mirrored. The side each vertex started on is preserved.
            partner_point = source_positions[partner]
            shared_z = (point[1] + partner_point[1]) * 0.5
            half_width = (abs(point[0]) + abs(partner_point[0])) * 0.5
            index_started_left = (
                drag_start_points.get(index, point)[0]
                <= drag_start_points.get(partner, partner_point)[0]
            )
            if index_started_left:
                positions[index] = (-half_width, shared_z)
                positions[partner] = (half_width, shared_z)
            else:
                positions[index] = (half_width, shared_z)
                positions[partner] = (-half_width, shared_z)
            guides.append((positions[index], positions[partner], True))
            continue

        mirror_target = (-positions[index][0], positions[index][1])
        positions[partner] = mirror_target
        guides.append((positions[index], mirror_target, True))

    return positions, guides


class MirrorGuideOverlay(QWidget):
    """Lightweight visual helper drawn above the existing wireframe canvas."""

    def __init__(self, canvas: QWidget) -> None:
        super().__init__(canvas)
        self._canvas = canvas
        self._guides: list[MirrorGuide] = []
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setGeometry(canvas.rect())
        canvas.installEventFilter(self)
        self.hide()

    def eventFilter(self, watched, event) -> bool:
        if watched is self._canvas and event.type() in (
            QEvent.Type.Resize,
            QEvent.Type.Show,
        ):
            self.setGeometry(self._canvas.rect())
        return False

    def set_guides(self, guides: list[MirrorGuide]) -> None:
        self._guides = list(guides)
        self.setVisible(bool(self._guides))
        if self._guides:
            self.raise_()
        self.update()

    def clear_guides(self) -> None:
        self.set_guides([])

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        if not self._guides:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        transform = self._canvas._make_transform()

        axis = self._canvas._to_screen((0.0, 0.0), transform)
        painter.setPen(QPen(QColor(218, 160, 255, 52), 1.0, Qt.PenStyle.DashLine))
        painter.drawLine(
            QPointF(axis.x(), 0.0),
            QPointF(axis.x(), float(self.height())),
        )

        for source, target, matched in self._guides:
            source_screen = self._canvas._to_screen(source, transform)
            target_screen = self._canvas._to_screen(target, transform)
            alpha = 105 if matched else 70
            style = Qt.PenStyle.DashLine if matched else Qt.PenStyle.DotLine
            painter.setPen(QPen(QColor(218, 160, 255, alpha), 1.0, style))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawLine(source_screen, target_screen)
            painter.drawEllipse(target_screen, 4.0, 4.0)

        painter.end()


class WireframeEditorWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        from studio.ui.style import apply_studio_style
        apply_studio_style()
        self.setWindowTitle(APP_TITLE)
        self.resize(1400, 760)
        self._last_directory = Path.cwd()
        self._current_model: SkltModel | None = None
        self._current_file_path: Path | None = None
        # Vehicle/weapon mode: every wireframe of ``_game_object`` is open at once.
        # Empty when a single file is edited.
        self._game_object = None
        self._components: list[WireComponent] = []
        self._active_component = -1
        self._layer_components: list[int] = []

        self.outline_editor = OutlineEditor()
        self.outline_editor.canvas.referenceLayerActivated.connect(self._activate_layer)
        self._mirror_pair_map: dict[int, int] = {}
        self._mirror_center_indices: set[int] = set()
        self._mirror_axis = 0
        self._mirror_drag_start_points: dict[int, tuple[float, float]] = {}
        self._mirror_guide_overlay = MirrorGuideOverlay(self.outline_editor.canvas)
        self.mirror_symmetry_check = QCheckBox("Mirror Symmetry")
        self.mirror_symmetry_check.setChecked(True)
        self.mirror_symmetry_check.setToolTip(
            "Move mode: keep existing mirrored vertices symmetric. The axis is detected "
            "automatically: left/right (X=0) or top/bottom (Z=0), whichever matches the shape. "
            "Thin guide lines show the mirrored target while dragging."
        )
        self.mirror_symmetry_check.toggled.connect(self._mirror_symmetry_toggled)
        self.outline_editor.canvas.pointsMoved.disconnect(
            self.outline_editor.move_projected_points
        )
        self.outline_editor.canvas.pointsMoved.connect(self._move_points_with_mirror)
        self.outline_editor.canvas.selectionDragStarted.connect(self._prepare_mirror_drag)
        self.outline_editor.canvas.selectionDragFinished.connect(self._finish_mirror_drag)
        self.outline_editor.show_indices_check.setChecked(False)
        self.outline_editor.dirtyChanged.connect(self._outline_dirty_changed)
        self.outline_editor.geometryChanged.connect(self._outline_geometry_changed)
        self.outline_editor.undoRedoChanged.connect(self._update_edit_controls)
        self.outline_editor.resetApplied.connect(self._outline_reset_applied)
        self.outline_editor.selectionChanged.connect(self._outline_selection_changed)
        self.file_value = QLabel("No file imported")
        self.file_value.setWordWrap(True)
        self.edit_mode_value = QLabel("No editable geometry")
        self.points_value = QLabel("0")
        self.polygons_value = QLabel("0")
        self.sensors_value = QLabel("0")
        self.outline_lines_value = QLabel("0")
        self.selection_value = QLabel("No selection")
        self.selected_elements_list = QListWidget()
        self.status_value = QLabel("Ready to import a file")

        self.new_action: QAction | None = None
        self.save_action: QAction | None = None
        self.save_as_action: QAction | None = None
        self.undo_action: QAction | None = None
        self.redo_action: QAction | None = None
        self.copy_action: QAction | None = None
        self.cut_action: QAction | None = None
        self.paste_action: QAction | None = None
        self.link_action: QAction | None = None
        self.delete_link_action: QAction | None = None
        self.add_point_action: QAction | None = None
        self.delete_point_action: QAction | None = None
        self.align_horizontal_action: QAction | None = None
        self.align_vertical_action: QAction | None = None
        self.reset_action: QAction | None = None
        self.add_vertex_action: QAction | None = None
        self.add_triangle_action: QAction | None = None
        self.add_square_action: QAction | None = None
        self.add_pentagon_action: QAction | None = None
        self.add_circle_action: QAction | None = None
        self.edit_toolbar: QToolBar | None = None

        configure_operation_status_bar(self)
        self._build_ui()
        self._build_menu()
        QApplication.instance().installEventFilter(self)
        self._build_edit_toolbar()
        # Start in the same editable empty-wireframe state as File -> New,
        # but keep the untouched startup document clean so opening an existing
        # file does not prompt to overwrite an empty session.
        self._start_new_session(mark_dirty=False)

    def eventFilter(self, watched, event) -> bool:
        if event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
            self._finish_mirror_drag()
        if event.type() == QEvent.Type.ShortcutOverride:
            widget = QApplication.focusWidget()
            editable = False
            while widget is not None and widget is not self:
                if isinstance(widget, (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox)) or (
                    isinstance(widget, QComboBox) and widget.isEditable()
                ):
                    editable = True
                widget = widget.parentWidget()
            if widget is self and editable:
                event.accept()
                return True
        return super().eventFilter(watched, event)

    def _build_ui(self) -> None:
        side_panel = QWidget()
        side_panel.setObjectName("wireframeInfoPanel")
        side_panel.setMinimumWidth(250)
        side_layout = QVBoxLayout(side_panel)
        side_layout.setContentsMargins(10, 8, 8, 8)
        side_layout.setSpacing(6)

        from studio.ui.browsers import GameFileBrowser
        self.game_file_browser = GameFileBrowser({".skl", ".sklt"}, self, allow_folder=True)
        self.game_file_browser.selected.connect(self._open_game_file)
        side_layout.addWidget(self.game_file_browser, 8)

        title = QLabel("Wireframe Overview")
        title.setObjectName("panelTitle")
        subtitle = QLabel("Edit the geometry and export a clean SKLT/SKL file.")
        subtitle.setObjectName("panelSubtitle")
        subtitle.setWordWrap(True)
        side_layout.addWidget(title)
        side_layout.addWidget(subtitle)

        asset_box = QGroupBox("Asset")
        asset_layout = QFormLayout(asset_box)
        asset_layout.setContentsMargins(8, 6, 8, 8)
        asset_layout.setVerticalSpacing(5)
        self.file_value.setObjectName("mutedValue")
        asset_layout.addRow("File", self.file_value)
        asset_layout.addRow("Type", self.edit_mode_value)
        asset_layout.addRow("Status", self.status_value)
        side_layout.addWidget(asset_box)

        geometry_box = QGroupBox("Geometry")
        geometry_layout = QGridLayout(geometry_box)
        geometry_layout.setContentsMargins(8, 7, 8, 8)
        geometry_layout.setHorizontalSpacing(8)
        geometry_layout.setVerticalSpacing(7)
        metrics = (
            ("Vertices", self.points_value),
            ("Shapes", self.polygons_value),
            ("Connections", self.outline_lines_value),
            ("Sensors", self.sensors_value),
        )
        for index, (label, value) in enumerate(metrics):
            row, column = divmod(index, 2)
            caption = QLabel(label)
            value.setObjectName("metricValue")
            value.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            geometry_layout.addWidget(caption, row, column * 2)
            geometry_layout.addWidget(value, row, column * 2 + 1)
        geometry_layout.setColumnStretch(1, 1)
        geometry_layout.setColumnStretch(3, 1)
        side_layout.addWidget(geometry_box)

        selection_box = QGroupBox("Selection")
        selection_layout = QVBoxLayout(selection_box)
        selection_layout.setContentsMargins(8, 6, 8, 8)
        self.selection_value.setObjectName("metricValue")
        selection_layout.addWidget(self.selection_value)
        selection_hint = QLabel("Click a vertex or connection to edit it.")
        selection_hint.setObjectName("mutedValue")
        selection_hint.setWordWrap(True)
        selection_layout.addWidget(selection_hint)
        side_layout.addWidget(selection_box)

        selected_elements_box = QGroupBox("Selected Elements")
        selected_elements_layout = QVBoxLayout(selected_elements_box)
        selected_elements_layout.setContentsMargins(8, 6, 8, 8)
        selected_elements_layout.setSpacing(4)
        self.selected_elements_list.setAlternatingRowColors(False)
        from studio.ui.widgets import use_standard_list_style
        use_standard_list_style(self.selected_elements_list)
        self.selected_elements_list.setMinimumHeight(60)
        self.selected_elements_list.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.selected_elements_list.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection
        )
        selected_elements_layout.addWidget(self.selected_elements_list)
        selected_elements_box.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        side_layout.addWidget(selected_elements_box, 1)

        transform_box = QGroupBox("Transform")
        transform_layout = QHBoxLayout(transform_box)
        self.transform_buttons = {}
        for mode, action in self.outline_editor.transform_actions.items():
            button = QToolButton()
            button.setDefaultAction(action)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            transform_layout.addWidget(button)
            self.transform_buttons[mode] = button
        side_layout.addWidget(transform_box)
        self.warning_status_label = QLabel()
        self.warning_status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.warning_status_label.setWordWrap(True)
        self.warning_status_label.setMaximumHeight(54)
        self.warning_status_label.setProperty("studioRole", "warning")
        self.statusBar().addPermanentWidget(self.warning_status_label, 1)
        self.warning_status_label.hide()
        from studio.rendering.gpu import add_renderer_badge
        self.renderer_badge = add_renderer_badge(self, self.outline_editor.canvas)

        # Same vehicle/weapon list as the Entity Designer.
        from studio.ui.browsers import GameObjectBrowser
        self.game_object_browser = GameObjectBrowser(self, kinds=("vehicle", "weapon"))
        self.game_object_browser.setMinimumWidth(200)
        self.game_object_browser.selected.connect(self._open_game_object)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.game_object_browser)
        splitter.addWidget(self.outline_editor)
        splitter.addWidget(side_panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 4)
        splitter.setStretchFactor(2, 1)
        splitter.setSizes([260, 820, 280])
        self.setCentralWidget(splitter)

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        self.file_menu = file_menu

        self.new_action = QAction("&New", self)
        self.new_action.setShortcut(QKeySequence.StandardKey.New)
        self.new_action.triggered.connect(self.new_file)
        file_menu.addAction(self.new_action)

        self.import_action = QAction("&Import", self)
        self.import_action.setShortcut(QKeySequence.StandardKey.Open)
        self.import_action.triggered.connect(self.open_dialog)

        self.save_action = QAction("&Overwrite", self)
        self.save_action.setShortcut(QKeySequence.StandardKey.Save)
        self.save_action.setToolTip("Write the changes into the loaded file.")
        self.save_action.triggered.connect(self.save_current_file)
        self.overwrite_action = self.save_action

        self.save_as_action = QAction("&Export", self)
        self.save_as_action.setShortcut(QKeySequence.StandardKey.SaveAs)
        self.save_as_action.setToolTip(
            "Save a copy to a location you choose; the loaded file is left untouched.")
        self.save_as_action.triggered.connect(self.save_outline_as)
        self.export_action = self.save_as_action

        file_menu.addAction(self.import_action)
        file_menu.addAction(self.save_action)
        file_menu.addAction(self.save_as_action)

        _close_action, self.exit_action = install_standard_file_menu_tail(
            file_menu, self, exit_text="E&xit")

        edit_menu = self.menuBar().addMenu("&Edit")
        self.edit_menu = edit_menu
        edit_menu.addActions(list(self.outline_editor.transform_actions.values()))
        edit_menu.addSeparator()
        self.select_all_action = QAction("Select All", self)
        self.select_all_action.setShortcut(QKeySequence.StandardKey.SelectAll)
        self.select_all_action.triggered.connect(self.outline_editor.select_all)
        edit_menu.addAction(self.select_all_action)
        self.cancel_action = QAction("Cancel Operation", self)
        self.cancel_action.setShortcut("Esc")
        self.cancel_action.triggered.connect(self.outline_editor.cancel_operation)
        edit_menu.addAction(self.cancel_action)

        self.undo_action = QAction("&Undo", self)
        self.undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        self.undo_action.triggered.connect(self.outline_editor.undo)
        edit_menu.addAction(self.undo_action)

        self.redo_action = QAction("&Redo", self)
        self.redo_action.setShortcuts(
            [QKeySequence.StandardKey.Redo, QKeySequence("Ctrl+Shift+Z")]
        )
        self.redo_action.triggered.connect(self.outline_editor.redo)
        edit_menu.addAction(self.redo_action)

        edit_menu.addSeparator()

        self.copy_action = QAction("&Copy", self)
        self.copy_action.setShortcut(QKeySequence.StandardKey.Copy)
        self.copy_action.triggered.connect(self.outline_editor.copy_selection)
        edit_menu.addAction(self.copy_action)

        self.cut_action = QAction("Cu&t", self)
        self.cut_action.setShortcut(QKeySequence.StandardKey.Cut)
        self.cut_action.triggered.connect(self.outline_editor.cut_selection)
        edit_menu.addAction(self.cut_action)

        self.paste_action = QAction("&Paste", self)
        self.paste_action.setShortcut(QKeySequence.StandardKey.Paste)
        self.paste_action.triggered.connect(self.outline_editor.paste_clipboard)
        edit_menu.addAction(self.paste_action)

        edit_menu.addSeparator()

        self.link_action = QAction("&Link", self)
        self.link_action.triggered.connect(self.outline_editor.start_link)
        edit_menu.addAction(self.link_action)

        self.add_point_action = None

        self.delete_point_action = QAction("&Delete", self)
        self.delete_point_action.setShortcut(QKeySequence(Qt.Key.Key_Delete))
        self.delete_point_action.triggered.connect(self.outline_editor.delete_selected_point)
        edit_menu.addAction(self.delete_point_action)

        edit_menu.addSeparator()

        self.align_horizontal_action = QAction("Align Horizontally", self)
        self.align_horizontal_action.triggered.connect(self.outline_editor.align_selected_horizontal)
        edit_menu.addAction(self.align_horizontal_action)

        self.align_vertical_action = QAction("Align Vertically", self)
        self.align_vertical_action.triggered.connect(self.outline_editor.align_selected_vertical)
        edit_menu.addAction(self.align_vertical_action)

        edit_menu.addSeparator()

        self.reset_action = QAction("&Reset", self)
        self.reset_action.triggered.connect(self.reset_active_view)
        edit_menu.addAction(self.reset_action)

        add_menu = self.menuBar().addMenu("&Add")
        self.add_vertex_action = QAction("&Vertex", self)
        self.add_triangle_action = QAction("&Triangle", self)
        self.add_square_action = QAction("&Square", self)
        self.add_pentagon_action = QAction("&Pentagon", self)
        self.add_circle_action = QAction("&Circle", self)
        self.add_vertex_action.triggered.connect(self.outline_editor.add_point)
        self.add_triangle_action.triggered.connect(lambda: self.outline_editor.add_shape("triangle"))
        self.add_square_action.triggered.connect(lambda: self.outline_editor.add_shape("square"))
        self.add_pentagon_action.triggered.connect(lambda: self.outline_editor.add_shape("pentagon"))
        self.add_circle_action.triggered.connect(lambda: self.outline_editor.add_shape("circle"))
        add_menu.addAction(self.add_vertex_action)
        add_menu.addSeparator()
        add_menu.addAction(self.add_triangle_action)
        add_menu.addAction(self.add_square_action)
        add_menu.addAction(self.add_pentagon_action)
        add_menu.addAction(self.add_circle_action)

    def _build_edit_toolbar(self) -> None:
        self.edit_toolbar = QToolBar("2D Edit Controls", self)
        self.edit_toolbar.setMovable(False)
        self.edit_toolbar.setFloatable(False)

        # Reuse the editor's existing history/reset controls so menu, context
        # menu and toolbar all share the same Undo/Redo/Reset implementation.
        self.edit_toolbar.addWidget(self.outline_editor.undo_button)
        self.edit_toolbar.addWidget(self.outline_editor.redo_button)
        self.edit_toolbar.addWidget(self.outline_editor.reset_button)
        self.edit_toolbar.addSeparator()
        self.outline_editor.show_indices_check.setText("Vertex IDs")
        self.edit_toolbar.addWidget(self.outline_editor.show_indices_check)
        self.edit_toolbar.addSeparator()
        self.edit_toolbar.addWidget(self.outline_editor.auto_align_check)
        self.edit_toolbar.addWidget(self.mirror_symmetry_check)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self.edit_toolbar)

    def _mirror_symmetry_toggled(self, enabled: bool) -> None:
        if not enabled:
            self._finish_mirror_drag()

    def _prepare_mirror_drag(self) -> None:
        self._mirror_pair_map.clear()
        self._mirror_center_indices.clear()
        self._mirror_drag_start_points.clear()
        self._mirror_guide_overlay.clear_guides()
        if (
            not self.mirror_symmetry_check.isChecked()
            or self.outline_editor.save_mode != "poo2"
            or self.outline_editor.canvas.transform_mode != "move"
        ):
            return

        points = self.outline_editor._projected_points()
        scale = self.outline_editor.canvas.pixels_per_unit()
        self._mirror_axis, self._mirror_pair_map, self._mirror_center_indices = (
            _choose_mirror_axis(points, scale)
        )
        # Points are stored already swapped when mirroring around Z=0.
        self._mirror_drag_start_points = {
            index: self._to_mirror_space(point) for index, point in enumerate(points)
        }

    def _to_mirror_space(self, point: tuple[float, float]) -> tuple[float, float]:
        """Convert (x, z) to the space where the mirror axis is always X=0 (and back)."""
        return _swap_axes(point) if self._mirror_axis == 1 else point

    def _finish_mirror_drag(self) -> None:
        self._mirror_pair_map.clear()
        self._mirror_center_indices.clear()
        self._mirror_drag_start_points.clear()
        self._mirror_axis = 0
        self._mirror_guide_overlay.clear_guides()

    def _move_points_with_mirror(self, point_positions: object, commit_undo: bool) -> None:
        editor = self.outline_editor
        if (
            not self.mirror_symmetry_check.isChecked()
            or editor.save_mode != "poo2"
            or editor.canvas.transform_mode != "move"
            or not isinstance(point_positions, dict)
        ):
            self._mirror_guide_overlay.clear_guides()
            editor.move_projected_points(point_positions, commit_undo)
            return

        source_positions: dict[int, tuple[float, float]] = {}
        for index, value in point_positions.items():
            if (
                isinstance(index, int)
                and isinstance(value, (tuple, list))
                and len(value) == 2
            ):
                source_positions[index] = (float(value[0]), float(value[1]))
        if not source_positions:
            return

        if not self._mirror_drag_start_points:
            self._prepare_mirror_drag()

        # Preserve the editor's existing Auto Align behavior before mirror
        # counterparts are added. Otherwise the extra mirrored vertices would
        # make a single-vertex drag look like a multi-selection to Auto Align.
        auto_align_message = ""
        if editor.auto_align_check.isChecked():
            if len(source_positions) == 1:
                index, point = next(iter(source_positions.items()))
                source_positions[index] = editor._auto_align_projected_vertex(
                    index, point[0], point[1]
                )
            else:
                source_positions = editor._snap_moved_group_center_to_origin(
                    source_positions
                )
            auto_align_message = editor._last_auto_align_message

        mirrored_positions, guides = _apply_mirror_positions(
            {i: self._to_mirror_space(p) for i, p in source_positions.items()},
            self._mirror_pair_map,
            self._mirror_center_indices,
            self._mirror_drag_start_points,
        )
        mirrored_positions = {
            i: self._to_mirror_space(p) for i, p in mirrored_positions.items()
        }
        guides = [
            (self._to_mirror_space(a), self._to_mirror_space(b), linked)
            for a, b, linked in guides
        ]

        # Auto Align was already applied to the user's side above. Temporarily
        # leave Move mode so the normal move handler does not snap a second
        # time after the mirrored counterparts have been appended.
        transform_mode = editor.canvas.transform_mode
        editor.canvas.transform_mode = "mirror_move"
        try:
            editor.move_projected_points(mirrored_positions, commit_undo)
        finally:
            editor.canvas.transform_mode = transform_mode

        if auto_align_message:
            editor._last_auto_align_message = auto_align_message
            editor._update_controls()

        if commit_undo:
            self._mirror_guide_overlay.clear_guides()
        else:
            # Keep the normal Auto Align preview alive while Mirror Symmetry
            # adds its own lighter overlay above the canvas.
            projected = editor._projected_points()
            editor.canvas._update_snap_preview({
                index: projected[index]
                for index in source_positions
                if 0 <= index < len(projected)
            })
            self._mirror_guide_overlay.set_guides(guides)

    def _component_dirty(self, index: int) -> bool:
        if index == self._active_component:
            return self.outline_editor.is_dirty
        return self._components[index].session.is_dirty

    def _dirty_names(self) -> list[str]:
        """Names of the open wireframes with unsaved changes."""
        if self._components:
            return [
                component.path.name for index, component in enumerate(self._components)
                if self._component_dirty(index)
            ]
        if not self.outline_editor.is_dirty:
            return []
        return [self._current_file_path.name if self._current_file_path else "current file"]

    def _maybe_save_dirty(self) -> bool:
        names = self._dirty_names()
        if not names:
            return True
        title = ", ".join(names)
        message_box = QMessageBox(self)
        message_box.setIcon(QMessageBox.Icon.Question)
        message_box.setWindowTitle("Overwrite changes?")
        message_box.setText(
            f"Overwrite changes to {title} before continuing?")
        message_box.setStandardButtons(
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel)
        message_box.setDefaultButton(QMessageBox.StandardButton.Save)
        overwrite_button = message_box.button(QMessageBox.StandardButton.Save)
        if overwrite_button:
            overwrite_button.setText("Overwrite")
        reply = message_box.exec()
        if reply == QMessageBox.StandardButton.Cancel:
            return False
        if reply == QMessageBox.StandardButton.Discard:
            return True
        self.save_current_file()
        return not self._dirty_names()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._maybe_save_dirty():
            event.accept()
        else:
            event.ignore()

    def _start_new_session(self, *, mark_dirty: bool) -> None:
        """Initialize the shared empty editable wireframe session.

        Startup and File -> New deliberately use the same model/setup path.
        The only difference is dirtiness: an untouched startup session is clean
        so importing a real file does not trigger an unnecessary save prompt.
        """
        model = create_minimal_sklt_model("Untitled.SKL")
        self._leave_game_object()
        self._current_file_path = None
        self._current_model = model
        self.game_file_browser.select_path(None)
        self._show_model(model, Path("Untitled.SKL"))
        if mark_dirty:
            self.outline_editor.mark_dirty()
        else:
            self.outline_editor.mark_clean()
        self._update_metadata_from_editor()
        self._update_window_title()
        self._update_save_controls()
        self._update_edit_controls()
        self.statusBar().showMessage("New SKLT session: empty wireframe")

    def new_file(self) -> None:
        if not self._maybe_save_dirty():
            return
        self._start_new_session(mark_dirty=True)

    def open_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import file",
            str(self._last_directory),
            "Urban Assault skeletons (*.sklt *.SKLT *.skl *.SKL);;All files (*)",
        )
        if path:
            self.open_file(path)

    def save_current_file(self) -> None:
        if self.outline_editor.canvas._dragging_point:
            self.outline_editor.cancel_operation()
        if not self._current_model:
            return
        if self._components:
            self._overwrite_components()
            return
        if not self.outline_editor.can_save or not self.outline_editor.is_dirty:
            return
        if not self._current_file_path:
            self.save_outline_as()
            return
        save_mode = self.outline_editor.save_mode
        try:
            self._write_edited_file(self._current_file_path)
            saved_model = parse_sklt_file(self._current_file_path)
        except SkltParseError as exc:
            QMessageBox.warning(self, "Could not overwrite file", str(exc))
            self.statusBar().showMessage("Overwrite failed")
            return

        save_label = "OTL2 outline" if save_mode == "otl2" else "wireframe"
        message = f"Overwrote {self._current_file_path.name} with the edited {save_label}."
        QMessageBox.information(self, "File overwritten", message)
        self._show_model(saved_model, self._current_file_path)

    def save_outline_as(self) -> None:
        if self.outline_editor.canvas._dragging_point:
            self.outline_editor.cancel_operation()
        if not self._current_model or not self.outline_editor.can_save:
            QMessageBox.information(
                self,
                "No editable data",
                "This file has no editable OTL2/OLPL outline or projected POO2/POL2 vertex data to export.",
            )
            return

        suggested = self._current_file_path or self._last_directory / "outline.SKL"
        save_mode = self.outline_editor.save_mode
        save_label = "OTL2 outline" if save_mode == "otl2" else "wireframe"
        path, _ = QFileDialog.getSaveFileName(
            self,
            f"Export edited {save_label} as",
            str(suggested),
            "Urban Assault skeletons (*.sklt *.SKLT *.skl *.SKL);;All files (*)",
        )
        if not path:
            return

        output_path = Path(path)
        if self._current_file_path and output_path.resolve() == self._current_file_path.resolve():
            QMessageBox.warning(
                self,
                "Choose a different file",
                "Export saves a copy, so it cannot target the loaded file. "
                "Use Overwrite to replace it, or choose a different destination.",
            )
            return

        try:
            self._write_edited_file(output_path)
            saved_model = parse_sklt_file(output_path)
        except SkltParseError as exc:
            QMessageBox.warning(self, "Could not export file", str(exc))
            self.statusBar().showMessage("Export failed")
            return

        message = f"Exported edited {save_label} to {output_path.name}."
        QMessageBox.information(self, "Outline exported", message)

        self._last_directory = output_path.parent
        if self._components:
            # The vehicle keeps editing the files its script names; the copy is only listed.
            self.game_file_browser.reload()
            self.game_file_browser.select_path(self._current_file_path)
            return
        # The editor continues on the exported file (new or overwritten). The
        # previous file is left untouched on disk; the list is reloaded so a
        # brand-new file shows up and gets selected.
        self._current_file_path = output_path
        self.game_file_browser.reload()
        self._show_model(saved_model, output_path)

    def _write_edited_file(self, output_path: Path) -> Path | None:
        if not self._current_model:
            raise SkltParseError("No SKLT file is imported.")
        return _write_wireframe(
            self._current_model, self.outline_editor.capture_session().state, output_path)

    def open_file(self, path: str | Path) -> None:
        if not self._maybe_save_dirty():
            return
        file_path = Path(path)
        try:
            model = parse_sklt_file(file_path)
        except SkltParseError as exc:
            QMessageBox.warning(self, "Could not import file", str(exc))
            self.statusBar().showMessage("Import failed")
            return
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Unexpected parsing error",
                f"The file was not modified.\n\n{exc}",
            )
            self.statusBar().showMessage("Import failed safely")
            return

        self._leave_game_object()
        self._last_directory = file_path.parent
        self._current_file_path = file_path
        self._show_model(model, file_path)

    def reload_game_resources(self):
        self.game_file_browser.reload()
        self.game_file_browser.select_path(self._current_file_path)
        self.game_object_browser.reload()
        self.game_object_browser.select_object(self._game_object)
        self._refresh_component_rows()

    def _open_game_file(self, path):
        index = self._component_index(path)
        if index >= 0:
            self._activate_component(index)
            return
        previous = self._current_file_path
        self.open_file(path)
        if self._current_file_path != Path(path):
            self.game_file_browser.select_path(previous)

    # -- Vehicle / weapon mode -------------------------------------------------

    def _open_game_object(self, obj) -> None:
        """Open every wireframe of a vehicle or weapon together."""
        current = self._game_object
        if current is not None and (current.kind, current.object_id) == (obj.kind, obj.object_id):
            return
        if not self._maybe_save_dirty():
            self.game_object_browser.select_object(current)
            return
        parts, problems = wireframe_parts(obj)
        components = []
        for label, path, key in parts:
            try:
                model = parse_sklt_file(path)
            except (SkltParseError, OSError) as exc:
                problems.append(f"{path.name}: {exc}")
                continue
            components.append(WireComponent(label, path, OutlineEditor.new_session(model), key))
        if not components:
            details = "\n".join(problems) or "The definition names no wireframe."
            QMessageBox.information(
                self, "No wireframe", f"{obj.name} has no wireframe that can be opened.\n\n{details}")
            self.game_object_browser.select_object(current)
            return

        placements = assemble_parts(
            [part.key for part in components],
            [_state_points(part.session.state) for part in components])
        for component, placement in zip(components, placements):
            component.placement = placement
        self._game_object = obj
        self._components = components
        self._active_component = 0
        first = components[0]
        self._current_file_path = first.path
        self._current_model = first.session.model
        self._finish_mirror_drag()
        self.outline_editor.restore_session(first.session)
        self._update_reference_layers()
        self.outline_editor.canvas.reset_view(fit_content=True)
        self.game_file_browser.set_pinned([(part.path, part.label) for part in components])
        self._refresh_document_panels(first.session.model, first.path)
        message = f"{obj.name}: {len(components)} wireframe(s) open"
        if problems:
            message += " · not found: " + "; ".join(problems)
        self.statusBar().showMessage(message)

    def _leave_game_object(self) -> None:
        """Back to editing a single file: forget the vehicle/weapon parts."""
        if self._game_object is None and not self._components:
            return
        self._game_object = None
        self._components = []
        self._active_component = -1
        self._layer_components = []
        self.outline_editor.canvas.set_reference_layers([])
        self.outline_editor.canvas.set_placement(1.0, 1.0, 0.0, 0.0)
        self.game_file_browser.set_pinned([])
        self.game_file_browser.set_modified([])
        self.game_object_browser.select_object(None)

    def _component_index(self, path) -> int:
        if path is None:
            return -1
        target = Path(path).resolve()
        return next(
            (index for index, part in enumerate(self._components) if part.path == target), -1)

    def _activate_layer(self, layer: int) -> None:
        index = self._layer_components[layer]
        if index >= 0:
            self._activate_component(index)

    def _activate_component(self, index: int) -> None:
        """Edit another part of the open vehicle; the view stays where it is."""
        if not (0 <= index < len(self._components)) or index == self._active_component:
            return
        self.outline_editor.cancel_operation()
        previous = self._components[self._active_component]
        previous.session = self.outline_editor.capture_session()
        component = self._components[index]
        self._active_component = index
        self._current_file_path = component.path
        self._current_model = component.session.model
        self._finish_mirror_drag()
        self.outline_editor.restore_session(component.session)
        self._update_reference_layers()
        self._refresh_document_panels(component.session.model, component.path)
        self.statusBar().showMessage(f"Editing {component.label}: {component.path.name}")

    def _update_reference_layers(self) -> None:
        """Place the edited part and draw the other parts of the definition around it."""
        active = self._components[self._active_component]
        layers = []
        self._layer_components = []
        if any(part.key in VISOR_SCALES for part in self._components):
            layers.append(visor_frame())
            self._layer_components.append(-1)
        for index, component in enumerate(self._components):
            if index == self._active_component:
                continue
            layers.append(ReferenceLayer(
                component.label,
                _placed(_state_points(component.session.state), component.placement),
                component.session.state.groups,
            ))
            self._layer_components.append(index)
        canvas = self.outline_editor.canvas
        canvas.set_placement(*active.placement)
        canvas.set_reference_layers(layers, active.label if len(self._components) > 1 else "")

    def _overwrite_components(self) -> None:
        """Overwrite every part of the vehicle that has unsaved changes."""
        active = self._components[self._active_component]
        active.session = self.outline_editor.capture_session()
        written = []
        for component in self._components:
            if not component.session.is_dirty:
                continue
            try:
                _write_wireframe(component.session.model, component.session.state, component.path)
                saved_model = parse_sklt_file(component.path)
            except (SkltParseError, OSError) as exc:
                QMessageBox.warning(
                    self, "Could not overwrite file", f"{component.path.name}: {exc}")
                self.statusBar().showMessage("Overwrite failed")
                break
            component.session = OutlineEditor.new_session(saved_model)
            written.append(component.path.name)
        self.outline_editor.restore_session(active.session)
        self._current_model = active.session.model
        self._update_reference_layers()
        self._refresh_document_panels(active.session.model, active.path)
        if written:
            QMessageBox.information(
                self, "File overwritten", "Overwrote " + ", ".join(written) + ".")

    def _refresh_component_rows(self) -> None:
        self.game_file_browser.set_modified([
            component.path for index, component in enumerate(self._components)
            if self._component_dirty(index)
        ])

    def _show_model(self, model: SkltModel, file_path: Path) -> None:
        self.outline_editor.set_model(model)
        self._refresh_document_panels(model, file_path)

    def _refresh_document_panels(self, model: SkltModel, file_path: Path) -> None:
        self.game_file_browser.select_path(self._current_file_path)
        self._current_model = model
        if self._components:
            label = self._components[self._active_component].label
            self.file_value.setText(f"{label}  ·  {file_path.name}")
        else:
            self.file_value.setText(file_path.name)
        self.file_value.setToolTip(str(file_path))
        self._refresh_component_rows()
        self.sensors_value.setText(str(len(model.sensors)))
        self._update_metadata_from_editor()

        warning_text = "\n".join(model.warnings)
        self.warning_status_label.setText(
            f"{len(model.warnings)} warning(s): {model.warnings[0]}" if model.warnings else ""
        )
        self.warning_status_label.setToolTip(warning_text)
        self.warning_status_label.setVisible(bool(model.warnings))

        self._update_window_title()
        self._update_save_controls()
        self._update_edit_controls()
        self.statusBar().showMessage(
            f"Imported {file_path.name}: {len(model.points)} vertices, "
            f"{model.rendered_polygon_count} rendered polygons, "
            f"{len(model.warnings)} warning(s)"
        )

    def _outline_dirty_changed(self, dirty: bool) -> None:
        self._update_window_title()
        self._update_save_controls()
        self._refresh_component_rows()

    def _outline_selection_changed(self, index: int) -> None:
        self._update_edit_controls()

    def _outline_geometry_changed(self) -> None:
        self._update_metadata_from_editor()
        self._update_selected_elements()

    def _outline_reset_applied(self) -> None:
        if self.outline_editor.save_mode == "poo2":
            self._outline_geometry_changed()

    def _update_metadata_from_editor(self) -> None:
        mode_labels = {
            "otl2": "2D Outline",
            "poo2": "HUD Wireframe",
            "none": "No editable geometry",
        }
        self.edit_mode_value.setText(
            mode_labels.get(self.outline_editor.save_mode, "No editable geometry"))
        if self.outline_editor.save_mode == "poo2":
            self.points_value.setText(str(self.outline_editor.editable_point_count))
            self.polygons_value.setText(str(self.outline_editor.editable_polygon_count))
            self.outline_lines_value.setText(str(self.outline_editor.editable_line_count))
        elif self._current_model:
            self.points_value.setText(str(self.outline_editor.editable_point_count))
            self.polygons_value.setText(str(self.outline_editor.editable_polygon_count))
            self.outline_lines_value.setText(str(self.outline_editor.editable_line_count))
        else:
            self.points_value.setText("0")
            self.polygons_value.setText("0")
            self.outline_lines_value.setText("0")

    def _update_selected_elements(self) -> None:
        """Show every explicitly selected vertex/connection in the side panel."""
        self.selected_elements_list.clear()

        vertex_ids = self.outline_editor.selected_vertex_indices
        edge_ids = self.outline_editor.selected_edges
        if not vertex_ids and not edge_ids:
            self.selected_elements_list.addItem("No selected elements")
            return

        if self.outline_editor.save_mode == "poo2":
            points = self.outline_editor.projected_points
            for index in vertex_ids:
                if 0 <= index < len(points):
                    x, y, z = points[index]
                    self.selected_elements_list.addItem(
                        f"Vertex {index}   X {x:.3f}   Y {y:.3f}   Z {z:.3f}"
                    )
        elif self.outline_editor.save_mode == "otl2":
            points = self.outline_editor.outline_points
            for index in vertex_ids:
                if 0 <= index < len(points):
                    x, y = points[index]
                    self.selected_elements_list.addItem(
                        f"Vertex {index}   X {x}   Y {y}"
                    )
        else:
            for index in vertex_ids:
                self.selected_elements_list.addItem(f"Vertex {index}")

        for first, second in edge_ids:
            self.selected_elements_list.addItem(
                f"Connection {first} ↔ {second}"
            )

    def _update_save_controls(self) -> None:
        # Overwrite only makes sense for pending edits, while Export remains
        # available for any editable wireframe so an imported file can always
        # be copied to a different destination.
        can_export = bool(self._current_model and self.outline_editor.can_save)
        can_overwrite = bool(self._dirty_names()) if self._components else (
            can_export and self.outline_editor.is_dirty)
        if self.save_action:
            self.save_action.setEnabled(can_overwrite)
        if self.save_as_action:
            self.save_as_action.setEnabled(can_export)
        self._update_status_value()

    def _update_status_value(self) -> None:
        if self._current_model is None:
            self.status_value.setText("Ready to import a file")
        elif self._dirty_names():
            self.status_value.setText("Unsaved changes")
        else:
            self.status_value.setText("Ready")

    def _update_edit_controls(self) -> None:
        if self.undo_action:
            self.undo_action.setEnabled(self.outline_editor.can_undo)
        if self.redo_action:
            self.redo_action.setEnabled(self.outline_editor.can_redo)
        can_edit_structure = self.outline_editor.save_mode == "poo2"
        self.mirror_symmetry_check.setEnabled(can_edit_structure)
        if not can_edit_structure:
            self._finish_mirror_drag()
        has_single_selection = self.outline_editor.selected_index >= 0 and self.outline_editor.selected_vertex_count == 1
        has_any_selection = self.outline_editor.has_vertex_selection or self.outline_editor.has_selected_link
        if self.copy_action:
            self.copy_action.setEnabled(can_edit_structure and has_any_selection)
        if self.cut_action:
            self.cut_action.setEnabled(can_edit_structure and has_any_selection)
        if self.paste_action:
            self.paste_action.setEnabled(can_edit_structure and self.outline_editor.has_clipboard)
        if self.link_action:
            self.link_action.setEnabled(can_edit_structure and has_single_selection)
        if self.add_point_action:
            self.add_point_action.setEnabled(can_edit_structure)
        if self.delete_point_action:
            self.delete_point_action.setEnabled(can_edit_structure and has_any_selection)
        if self.align_horizontal_action:
            self.align_horizontal_action.setEnabled(can_edit_structure and has_single_selection)
        if self.align_vertical_action:
            self.align_vertical_action.setEnabled(can_edit_structure and has_single_selection)
        if self.reset_action:
            self.reset_action.setEnabled(self.outline_editor.can_reset)
        for action in (
            self.add_vertex_action,
            self.add_triangle_action,
            self.add_square_action,
            self.add_pentagon_action,
            self.add_circle_action,
        ):
            if action:
                action.setEnabled(can_edit_structure)

        selected_vertices = self.outline_editor.selected_vertex_count
        selected_links = self.outline_editor.selected_edge_count
        selection_parts = []
        if selected_vertices:
            selection_parts.append(
                f"{selected_vertices} vertex"
                + ("" if selected_vertices == 1 else "es"))
        if selected_links:
            selection_parts.append(
                f"{selected_links} connection"
                + ("" if selected_links == 1 else "s"))
        self.selection_value.setText(
            " + ".join(selection_parts) if selection_parts else "No selection")
        self._update_selected_elements()

    def _update_window_title(self) -> None:
        dirty_marker = " *" if self._dirty_names() else ""
        if self._game_object is not None and self._components:
            label = self._components[self._active_component].label
            self.setWindowTitle(
                f"{APP_TITLE} - {self._game_object.name} - {label}: "
                f"{self._current_file_path.name}{dirty_marker}")
            return
        if self._current_file_path:
            full_path = self._current_file_path.expanduser().resolve(
                strict=False
            )
            self.setWindowTitle(
                f"{APP_TITLE} - {full_path.name} - "
                f"{full_path}{dirty_marker}"
            )
            return
        if self._current_model:
            self.setWindowTitle(f"{APP_TITLE} - Untitled.SKL{dirty_marker}")
            return
        self.setWindowTitle(APP_TITLE)

    def reset_active_view(self) -> None:
        self.outline_editor.reset_to_loaded()


def main() -> int:
    from studio.rendering.gpu import request_desktop_opengl

    request_desktop_opengl()
    app = QApplication(sys.argv)
    app.setApplicationName("OpenNeoUAStudio")
    app.setApplicationDisplayName("")
    window = WireframeEditorWindow()
    window.show()

    if len(sys.argv) > 1:
        window.open_file(sys.argv[1])

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
