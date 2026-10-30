"""Small Entity Designer widgets: geometry part tree, sphere tree and compact spin box.
"""

from __future__ import annotations

import math

from PySide6.QtCore import (
    QItemSelectionModel,
    QPointF,
    QSignalBlocker,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QMouseEvent,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDoubleSpinBox,
    QHeaderView,
    QLabel,
    QMenu,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from entity_designer.script_model import LEGACY, OPENNEOUA

# Exact Legacy red plus the existing OpenNeoUA compound green.
TYPE_COLORS = {
    LEGACY: QColor(220, 60, 60),
    OPENNEOUA: QColor(60, 220, 60),
}


FIRE_POINT_COLOR = QColor(235, 60, 60)


GUN_POINT_COLOR = QColor(178, 78, 238)


TURRET_SIDE_COLOR = QColor(65, 205, 235)


TURRET_UP_COLOR = QColor(90, 225, 120)


TURRET_DOWN_COLOR = QColor(255, 175, 70)


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



def _type_button_stylesheet(category: str) -> str:
    """Return the solid add-button color used by the matching sphere type."""

    color = TYPE_COLORS[category]
    return (
        "QPushButton {"
        f" background-color: rgb({color.red()}, {color.green()}, {color.blue()});"
        " color: rgb(18, 20, 24);"
        " font-weight: 600;"
        "}"
    )


_GEOMETRY_COMPONENT_ROLE = int(Qt.ItemDataRole.UserRole) + 1
# Index of one convex part in the collision shape.
_SHAPE_HULL_ROLE = int(Qt.ItemDataRole.UserRole) + 2
# Topology text of a component row, shown again when no shape is loaded.
_DIAGNOSTIC_ROLE = int(Qt.ItemDataRole.UserRole) + 3


class GeometryPartSelectionWidget(QWidget):
    """Keep a model's collision geometry selection visible in its tab."""

    selectionChanged = Signal()
    shapeHighlightChanged = Signal(object)

    def __init__(self, parent=None, on_component_highlight=None) -> None:
        super().__init__(parent)
        self._components = ()
        self._components_by_key = {}
        self._triangles_by_owner = {}
        self._source_key = None
        self._shape = None
        self._shape_root = None
        self._objects_count = 0
        self._on_component_highlight = on_component_highlight
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        self.summary_label = QLabel("Load a model to select collision geometry.")
        layout.addWidget(self.summary_label)
        self.tree = QTreeWidget()
        from studio.ui.widgets import use_standard_list_style
        use_standard_list_style(self.tree)
        self.tree.setHeaderLabels(["Geometry / component", "Triangles", "Topology"])
        self.tree.header().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.tree, 1)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.currentItemChanged.connect(
            self._update_component_highlight)
        self.tree.itemSelectionChanged.connect(
            lambda: self._update_component_highlight(self.tree.currentItem(), None))
        self.tree.itemChanged.connect(self._selection_changed)
        self._selection_timer = QTimer(self)
        self._selection_timer.setSingleShot(True)
        self._selection_timer.timeout.connect(self._flush_selection_changed)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_component_menu)

    def _show_component_menu(self, position) -> None:
        item = self.tree.itemAt(position)
        menu = QMenu(self)
        if item is not None:
            self.tree.setCurrentItem(item, 0,
                QItemSelectionModel.SelectionFlag.NoUpdate if item.isSelected()
                else QItemSelectionModel.SelectionFlag.ClearAndSelect)
            if item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                self._add_component_toggle_action(menu, item)
                menu.addSeparator()
        self._add_component_deselect_action(menu)
        menu.exec(self.tree.viewport().mapToGlobal(position))

    def deselect_component(self) -> None:
        self.tree.setCurrentItem(None)
        self.tree.clearSelection()
        self._update_component_highlight(None, None)

    def _add_component_deselect_action(self, menu) -> None:
        action = menu.addAction("Deselect")
        action.setEnabled(self.isEnabled() and bool(self.tree.selectedItems()))
        action.triggered.connect(self.deselect_component)

    def _add_component_toggle_action(self, menu, item=None) -> None:
        selected = list(self.tree.selectedItems())
        if item is not None and not item.isSelected():
            selected = [item]
        selected = [row for row in selected
                    if row.flags() & Qt.ItemFlag.ItemIsUserCheckable]
        if not selected:
            return
        enable = (item.checkState(0) != Qt.CheckState.Checked
                  if item is not None else
                  any(row.checkState(0) != Qt.CheckState.Checked
                      for row in selected))
        action = menu.addAction("Enable" if enable else "Disable")
        action.setEnabled(self.isEnabled())

        def set_selected_state():
            state = (Qt.CheckState.Checked if enable
                     else Qt.CheckState.Unchecked)
            for row in selected:
                row.setCheckState(0, state)

        action.triggered.connect(set_selected_state)

    def _items(self):
        stack = [self.tree.topLevelItem(i)
                 for i in range(self.tree.topLevelItemCount())]
        while stack:
            item = stack.pop()
            yield item
            stack.extend(item.child(i) for i in range(item.childCount()))

    @staticmethod
    def _subtree(item):
        stack = [item]
        while stack:
            row = stack.pop()
            yield row
            stack.extend(row.child(i) for i in range(row.childCount()))

    def _hull_rows(self):
        return [item for item in self._items()
                if item.data(0, _SHAPE_HULL_ROLE) is not None]

    def row_for_pick(self, pick):
        """Return the row of a viewport pick: a hull index or a component key."""

        if isinstance(pick, int):
            for item in self._items():
                if item.data(0, _SHAPE_HULL_ROLE) == pick:
                    return item
            if self._shape is None or not 0 <= pick < len(self._shape.hulls):
                return None
            pick = self._shape.hulls[pick].source_component or f"@hull:{pick}"
        for item in self._items():
            if item.data(0, _GEOMETRY_COMPONENT_ROLE) == pick:
                return item
        return None

    def select_shape_component(self, pick, additive=False) -> None:
        item = self.row_for_pick(pick)
        if item is None:
            return
        self.tree.setCurrentItem(item, 0,
            QItemSelectionModel.SelectionFlag.Toggle if additive
            else QItemSelectionModel.SelectionFlag.ClearAndSelect)
        self.tree.scrollToItem(item)

    def enabled_shape_keys(self):
        return {item.data(0, _GEOMETRY_COMPONENT_ROLE)
                for item in self._items()
                if item.checkState(0) == Qt.CheckState.Checked}

    def hull_row_states(self) -> dict[int, bool]:
        """Enabled state of every convex part that has its own row."""

        return {item.data(0, _SHAPE_HULL_ROLE):
                item.checkState(0) == Qt.CheckState.Checked
                for item in self._hull_rows()}

    @staticmethod
    def _row_identity(row):
        return (row.data(0, _GEOMETRY_COMPONENT_ROLE),
                row.data(0, Qt.ItemDataRole.UserRole),
                row.data(0, _SHAPE_HULL_ROLE))

    def set_shape(self, shape) -> None:
        if shape is self._shape:
            return
        self._selection_timer.stop()
        had_shape = self._shape is not None
        same_geometry = (shape is not None and self._shape is not None
                         and tuple((h.vertices, h.faces, h.source_component)
                                   for h in shape.hulls)
                         == tuple((h.vertices, h.faces, h.source_component)
                                  for h in self._shape.hulls))
        self._shape = shape
        current = self.tree.currentItem()
        current_key = (self._row_identity(current)
                       if current is not None else None)
        selected_keys = {self._row_identity(row)
                         for row in self.tree.selectedItems()}
        blocker = QSignalBlocker(self.tree)
        for row in self._hull_rows():
            parent = row.parent()
            # Keep the state the convex parts gave their component row: it
            # is the generation selection once the parts are gone.
            parent.setData(0, Qt.ItemDataRole.CheckStateRole,
                           parent.checkState(0))
            parent.removeChild(row)
        if self._shape_root is not None and not same_geometry:
            index = self.tree.indexOfTopLevelItem(self._shape_root)
            self.tree.takeTopLevelItem(index)
            self._shape_root = None
        for index in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(index).setHidden(False)
        for row in self._items():
            if row.data(0, _GEOMETRY_COMPONENT_ROLE) in self._components_by_key:
                row.setText(2, row.data(0, _DIAGNOSTIC_ROLE) or "")
                row.setToolTip(2, row.data(0, _DIAGNOSTIC_ROLE) or "")
        if shape is not None:
            by_key = {item.data(0, _GEOMETRY_COMPONENT_ROLE): item
                      for item in self._items()}
            grouped = {}
            for index, hull in enumerate(shape.hulls):
                key = hull.source_component or f"@hull:{index}"
                grouped.setdefault(key, []).append((index, hull))
            for row in self._items():
                key = row.data(0, _GEOMETRY_COMPONENT_ROLE)
                if key in self._components_by_key and key not in grouped:
                    row.setText(2, "no collision part")
                    row.setToolTip(
                        2, "The current shape has no convex part for this "
                        "geometry: it was not selected, or a neighbouring "
                        "part already covers it.")
            if not any(key in self._components_by_key for key in grouped):
                # Old files have no source mapping: expose their actual hulls.
                for index in range(self.tree.topLevelItemCount()):
                    item = self.tree.topLevelItem(index)
                    item.setHidden(item is not self._shape_root)
            for key, entries in grouped.items():
                item = by_key.get(key)
                if item is None:
                    if self._shape_root is None:
                        self._shape_root = QTreeWidgetItem(self.tree)
                        self._shape_root.setText(0, "Collision shape parts")
                        self._shape_root.setFlags(
                            self._shape_root.flags() | Qt.ItemFlag.ItemIsUserCheckable
                            | Qt.ItemFlag.ItemIsAutoTristate)
                    item = QTreeWidgetItem(self._shape_root)
                    group_count = key.partition("#planar-group:")[2].partition(":")[0]
                    if group_count.isdigit():
                        item.setText(
                            0, f"Combined planar components ({group_count})")
                        item.setToolTip(
                            0, "This collision part represents the selected "
                            "planar geometry components together.")
                    else:
                        item.setText(
                            0, f"Component {self._shape_root.childCount()}")
                    item.setData(0, _GEOMETRY_COMPONENT_ROLE, key)
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable
                                  | Qt.ItemFlag.ItemIsAutoTristate)
                    item.setText(1, str(sum(len(h.faces) for _, h in entries)))
                item.setText(2, f"{len(entries)} convex part"
                             + ("s" if len(entries) != 1 else ""))
                item.setToolTip(2, item.text(2))
                if len(entries) == 1:
                    item.setCheckState(0, Qt.CheckState.Checked
                                       if entries[0][1].enabled
                                       else Qt.CheckState.Unchecked)
                    continue
                # Several convex parts: each one can be switched on its own.
                for number, (index, hull) in enumerate(entries, 1):
                    child = QTreeWidgetItem(item)
                    child.setText(0, f"Convex part {number}")
                    child.setText(1, str(len(hull.faces)))
                    child.setData(0, _SHAPE_HULL_ROLE, index)
                    child.setFlags(
                        child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    child.setCheckState(0, Qt.CheckState.Checked
                                        if hull.enabled
                                        else Qt.CheckState.Unchecked)
            self.tree.expandAll()
            for row in self._items():
                key = self._row_identity(row)
                row.setSelected(key in selected_keys)
                if key == current_key:
                    self.tree.setCurrentItem(row, 0, QItemSelectionModel.SelectionFlag.NoUpdate)
            if not had_shape and self._shape_root is not None and self.tree.currentItem() is None:
                self.tree.setCurrentItem(
                    self._shape_root.child(0), 0,
                    QItemSelectionModel.SelectionFlag.NoUpdate)
            self.summary_label.setText(
                f"{len(grouped)} shape components; "
                f"{sum(h.enabled for h in shape.hulls)} of "
                f"{len(shape.hulls)} convex parts active.")
        else:
            self._update_summary()
        del blocker
        self._update_component_highlight(self.tree.currentItem(), None)

    def set_geometry(self, family, root_owner, components,
                     owner_triangles, source_key) -> None:
        self._selection_timer.stop()
        preserve = source_key == self._source_key and source_key is not None
        selected_keys = set(self.selected_component_keys()) if preserve else set()
        self._source_key = source_key
        self._components = tuple(components)
        self._components_by_key = {
            _geometry_component_key(component): component
            for component in self._components
        }
        self._triangles_by_owner = dict(owner_triangles)

        blocker = QSignalBlocker(self.tree)
        self.tree.clear()
        self._shape = None
        self._shape_root = None
        by_path = {}
        objects = []
        if family is not None:
            for obj in family.all_objects():
                if obj.skeleton is None:
                    continue
                owner = obj.owner_path
                if root_owner and not (
                        owner == root_owner
                        or owner.startswith(root_owner.rstrip("/") + "/")):
                    continue
                objects.append(obj)
                parent_item = by_path.get(owner.rpartition("/")[0])
                item = QTreeWidgetItem(parent_item or self.tree)
                item.setText(0, obj.display_name or owner)
                item.setText(
                    1, str(len(self._triangles_by_owner.get(owner, ()))))
                item.setData(0, Qt.ItemDataRole.UserRole, owner)
                item.setFlags(
                    item.flags() | Qt.ItemFlag.ItemIsUserCheckable
                    | Qt.ItemFlag.ItemIsAutoTristate)
                by_path[owner] = item

        pieces_by_owner = {}
        for component in self._components:
            pieces_by_owner.setdefault(component.owner, {}).setdefault(
                component.group_id or component.component_id, []).append(
                component)
        for owner, pieces in pieces_by_owner.items():
            owner_item = by_path.get(owner)
            if owner_item is None:
                continue
            owner_item.setText(
                1, str(sum(len(component.triangles)
                           for parts in pieces.values()
                           for component in parts)))
            for index, (_piece, parts) in enumerate(sorted(pieces.items()), 1):
                parent = owner_item
                if len(parts) > 1:
                    # One connected piece split where it folds inwards.
                    parent = QTreeWidgetItem(owner_item)
                    parent.setText(0, f"Component {index}")
                    parent.setText(
                        1, str(sum(len(part.triangles) for part in parts)))
                    parent.setText(2, f"{len(parts)} parts")
                    parent.setToolTip(
                        2, "One connected piece, split where its surface "
                        "folds inwards. Parts of the same piece are baked "
                        "together.")
                    parent.setFlags(
                        parent.flags() | Qt.ItemFlag.ItemIsUserCheckable
                        | Qt.ItemFlag.ItemIsAutoTristate)
                    parts = sorted(parts, key=lambda part: (
                        -len(part.triangles), part.component_id))
                for number, component in enumerate(parts, 1):
                    key = _geometry_component_key(component)
                    child = QTreeWidgetItem(parent)
                    child.setText(0, f"Component {index}" if parent is owner_item
                                  else f"Part {number}")
                    child.setText(1, str(len(component.triangles)))
                    diagnostic = _geometry_component_diagnostic(component)
                    child.setText(2, diagnostic)
                    child.setData(0, _DIAGNOSTIC_ROLE, diagnostic)
                    child.setData(0, _GEOMETRY_COMPONENT_ROLE, key)
                    points = [
                        point for triangle in component.triangles
                        for point in triangle
                    ]
                    if points:
                        axes = ("X", "Y", "Z")
                        bounds = "\n".join(
                            f"{axes[axis]}: "
                            f"{min(point[axis] for point in points):.3f} … "
                            f"{max(point[axis] for point in points):.3f}"
                            for axis in range(3))
                        child.setToolTip(
                            0, "Model-local bounds before visual scale and "
                            f"rotation:\n{bounds}")
                    child.setToolTip(2, diagnostic)
                    # Tristate only matters once a shape adds convex parts below.
                    child.setFlags(
                        child.flags() | Qt.ItemFlag.ItemIsUserCheckable
                        | Qt.ItemFlag.ItemIsAutoTristate)
                    child.setCheckState(
                        0, Qt.CheckState.Checked if (
                            not preserve or key in selected_keys)
                        else Qt.CheckState.Unchecked)

        if not self._components:
            for owner, item in by_path.items():
                item.setCheckState(
                    0, Qt.CheckState.Checked if not preserve
                    else item.data(0, Qt.ItemDataRole.UserRole)
                    in selected_keys)
        self.tree.expandAll()
        first = self.tree.topLevelItem(0)
        if first is not None:
            # Current for the keyboard, but not selected: a newly loaded unit
            # starts with nothing highlighted until the user picks a row.
            self.tree.setCurrentItem(
                first, 0, QItemSelectionModel.SelectionFlag.NoUpdate)
        del blocker
        self._objects_count = len(objects)
        self._update_summary()
        self._update_component_highlight(self.tree.currentItem(), None)
        self.selectionChanged.emit()

    def _update_summary(self) -> None:
        self.summary_label.setText(
            "No collision triangles in the selected model."
            if not self._objects_count
            else f"{self._objects_count} geometry parts; "
            f"{len(self.selected_components())} of {len(self._components)} "
            "components selected.")

    def selected_components(self):
        # A component whose convex parts are only partly enabled still
        # belongs to the generation selection.
        selected = [
            self._components_by_key[key] for item in self._items()
            if (key := item.data(0, _GEOMETRY_COMPONENT_ROLE))
            in self._components_by_key
            and item.checkState(0) != Qt.CheckState.Unchecked]
        return tuple(sorted(
            selected, key=lambda value: _geometry_component_key(value)))

    def selected_component_keys(self) -> tuple[str, ...]:
        return tuple(
            _geometry_component_key(component)
            for component in self.selected_components())

    def selected_owners(self) -> tuple[str, ...]:
        components = self.selected_components()
        if components:
            return tuple(sorted({component.owner for component in components}))
        selected = []
        for item in self._items():
            owner = item.data(0, Qt.ItemDataRole.UserRole)
            if owner and item.checkState(0) == Qt.CheckState.Checked:
                selected.append(str(owner))
        return tuple(sorted(selected))

    def _selection_changed(self, _item, _column) -> None:
        # Qt finishes propagating a parent checkbox before the shape is updated.
        # Rebuilding rows inside itemChanged can invalidate Qt's current children.
        self._selection_timer.start(0)

    def _flush_selection_changed(self) -> None:
        self._selection_timer.stop()
        if self._shape is None:
            self._update_summary()
        self._update_component_highlight(self.tree.currentItem(), None)
        self.selectionChanged.emit()

    def _update_component_highlight(self, _item, _previous) -> None:
        rows = [row for item in self.tree.selectedItems()
                for row in self._subtree(item)]
        if self._shape is not None:
            indices = set()
            keys = set()
            owners = set()
            for row in rows:
                hull_index = row.data(0, _SHAPE_HULL_ROLE)
                if hull_index is not None:
                    indices.add(hull_index)
                    continue
                key = row.data(0, _GEOMETRY_COMPONENT_ROLE)
                owner = row.data(0, Qt.ItemDataRole.UserRole)
                if key:
                    keys.add(key)
                if owner:
                    owners.add(str(owner).rstrip("/"))
            indices.update(
                index for index, hull in enumerate(self._shape.hulls)
                if (hull.source_component or f"@hull:{index}") in keys
                or any(hull.source_component.startswith(owner + "#")
                       or hull.source_component.startswith(owner + "/")
                       for owner in owners))
            self.shapeHighlightChanged.emit(indices)
            if self._on_component_highlight is not None:
                self._on_component_highlight(None)
            return
        self.shapeHighlightChanged.emit(set())
        if self._on_component_highlight is None:
            return
        # Without a shape the viewport shows which geometry the next
        # generation uses: ticked parts thin, rows picked in the list bold.
        if self._components:
            included = [triangle for component in self.selected_components()
                        for triangle in component.triangles]
            picked = {}
            for row in rows:
                key = row.data(0, _GEOMETRY_COMPONENT_ROLE)
                if key in self._components_by_key:
                    picked[key] = row.checkState(0) != Qt.CheckState.Unchecked
            highlighted, excluded = [], []
            for component in self._components:
                ticked = picked.get(_geometry_component_key(component))
                if ticked is not None:
                    (highlighted if ticked else excluded).extend(
                        component.triangles)
        else:
            ticked = set(self.selected_owners())
            included = [triangle for owner in ticked
                        for triangle in self._triangles_by_owner.get(owner, ())]
            highlighted, excluded = [], []
            for row in rows:
                owner = row.data(0, Qt.ItemDataRole.UserRole)
                target = highlighted if owner in ticked else excluded
                target.extend(self._triangles_by_owner.get(owner, ()))
        self._on_component_highlight(
            tuple(included) or None, tuple(highlighted) or None,
            tuple(excluded) or None)


def _geometry_component_key(component) -> str:
    return f"{component.owner}#component:{component.component_id}"


def _geometry_component_diagnostic(component) -> str:
    details = []
    if component.boundary_edges:
        details.append(f"open {component.boundary_edges}")
    if component.non_manifold_edges:
        details.append(f"non-manifold {component.non_manifold_edges}")
    if component.planar:
        details.append("planar, no volume")
    elif component.boundary_edges or component.non_manifold_edges:
        details.append("3D surface")
    elif component.has_volume:
        details.append("closed volume")
    else:
        details.append("no volume")
    return ", ".join(details) if details else "closed volume"


class SphereTreeWidget(QTreeWidget):
    """Sphere list with context-click behavior that respects multiselect."""

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.RightButton:
            pos = event.position().toPoint()
            item = self.itemAt(pos)
            if item is not None and item.isSelected():
                # Native QAbstractItemView right-click handling can collapse an
                # ExtendedSelection before the custom context menu opens. Keep
                # the group intact when the clicked row already belongs to it;
                # only make that row current without changing selection.
                model_index = self.indexAt(pos)
                if model_index.isValid():
                    self.selectionModel().setCurrentIndex(
                        model_index,
                        QItemSelectionModel.SelectionFlag.NoUpdate)
                self.setFocus(Qt.FocusReason.MouseFocusReason)
                event.accept()
                return
        super().mousePressEvent(event)


class CompactScaleSpinBox(QDoubleSpinBox):
    """Keep scale precision while hiding meaningless trailing zeroes."""

    def textFromValue(self, value: float) -> str:  # noqa: N802
        text = self.locale().toString(value, "f", self.decimals())
        decimal_point = self.locale().decimalPoint()
        if decimal_point in text:
            text = text.rstrip("0").rstrip(decimal_point)
        return text
