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

    def _add_component_toggle_action(self, menu, item) -> None:
        enable = item.checkState(0) == Qt.CheckState.Unchecked
        action = menu.addAction("Enable" if enable else "Disable")
        action.setEnabled(self.isEnabled())
        action.triggered.connect(
            lambda: item.setCheckState(
                0, Qt.CheckState.Checked if enable else Qt.CheckState.Unchecked))

    def _items(self):
        stack = [self.tree.topLevelItem(i)
                 for i in range(self.tree.topLevelItemCount())]
        while stack:
            item = stack.pop()
            yield item
            stack.extend(item.child(i) for i in range(item.childCount()))

    def select_shape_component(self, key, additive=False) -> None:
        for item in self._items():
            if item.data(0, _GEOMETRY_COMPONENT_ROLE) == key:
                self.tree.setCurrentItem(item, 0,
                    QItemSelectionModel.SelectionFlag.Toggle if additive
                    else QItemSelectionModel.SelectionFlag.ClearAndSelect)
                self.tree.scrollToItem(item)
                return

    def enabled_shape_keys(self):
        return {item.data(0, _GEOMETRY_COMPONENT_ROLE)
                for item in self._items()
                if item.checkState(0) == Qt.CheckState.Checked}

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
        current_key = ((current.data(0, _GEOMETRY_COMPONENT_ROLE),
                        current.data(0, Qt.ItemDataRole.UserRole))
                       if current is not None else None)
        selected_keys = {(row.data(0, _GEOMETRY_COMPONENT_ROLE),
                          row.data(0, Qt.ItemDataRole.UserRole))
                         for row in self.tree.selectedItems()}
        blocker = QSignalBlocker(self.tree)
        if self._shape_root is not None and not same_geometry:
            index = self.tree.indexOfTopLevelItem(self._shape_root)
            self.tree.takeTopLevelItem(index)
            self._shape_root = None
        for index in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(index).setHidden(False)
        for row in self._items():
            if row.data(0, _GEOMETRY_COMPONENT_ROLE) in self._components_by_key:
                row.setHidden(False)
        if shape is not None:
            by_key = {item.data(0, _GEOMETRY_COMPONENT_ROLE): item
                      for item in self._items()}
            grouped = {}
            for index, hull in enumerate(shape.hulls):
                key = hull.source_component or f"@hull:{index}"
                grouped.setdefault(key, []).append(hull)
            for row in self._items():
                key = row.data(0, _GEOMETRY_COMPONENT_ROLE)
                if key in self._components_by_key:
                    # An old export may have permanently omitted this component's hulls.
                    row.setHidden(key not in grouped)
            if not any(key in self._components_by_key for key in grouped):
                # Old files have no source mapping: expose their actual hulls.
                for index in range(self.tree.topLevelItemCount()):
                    item = self.tree.topLevelItem(index)
                    item.setHidden(item is not self._shape_root)
            for key, hulls in grouped.items():
                item = by_key.get(key)
                if item is None:
                    if self._shape_root is None:
                        self._shape_root = QTreeWidgetItem(self.tree)
                        self._shape_root.setText(0, "Collision shape parts")
                        self._shape_root.setFlags(
                            self._shape_root.flags() | Qt.ItemFlag.ItemIsUserCheckable
                            | Qt.ItemFlag.ItemIsAutoTristate)
                    item = QTreeWidgetItem(self._shape_root)
                    item.setText(0, f"Component {self._shape_root.childCount()}")
                    item.setData(0, _GEOMETRY_COMPONENT_ROLE, key)
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setText(1, str(sum(len(h.faces) for h in hulls)))
                    item.setText(2, f"{len(hulls)} convex parts")
                item.setCheckState(0, Qt.CheckState.Checked if any(
                    hull.enabled for hull in hulls) else Qt.CheckState.Unchecked)
            self.tree.expandAll()
            for row in self._items():
                key = (row.data(0, _GEOMETRY_COMPONENT_ROLE),
                       row.data(0, Qt.ItemDataRole.UserRole))
                row.setSelected(key in selected_keys)
                if key == current_key:
                    self.tree.setCurrentItem(row, 0, QItemSelectionModel.SelectionFlag.NoUpdate)
            if not had_shape and self._shape_root is not None and self.tree.currentItem() is None:
                self.tree.setCurrentItem(self._shape_root.child(0))
            self.summary_label.setText(
                f"{len(grouped)} shape components; "
                f"{sum(h.enabled for h in shape.hulls)} active convex parts.")
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

        components_by_owner = {}
        for component in self._components:
            components_by_owner.setdefault(component.owner, []).append(
                component)
        for owner, owner_components in components_by_owner.items():
            owner_item = by_path.get(owner)
            if owner_item is None:
                continue
            owner_item.setText(
                1, str(sum(len(component.triangles)
                           for component in owner_components)))
            for index, component in enumerate(owner_components, 1):
                key = _geometry_component_key(component)
                child = QTreeWidgetItem(owner_item)
                child.setText(0, f"Component {index}")
                child.setText(1, str(len(component.triangles)))
                diagnostic = _geometry_component_diagnostic(component)
                child.setText(2, diagnostic)
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
                child.setFlags(
                    child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
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
            self.tree.setCurrentItem(first)
        del blocker
        self.summary_label.setText(
            "No collision triangles in the selected model." if not objects
            else f"{len(objects)} geometry parts; "
            f"{len(self.selected_components())} components selected.")
        self._update_component_highlight(self.tree.currentItem(), None)
        self.selectionChanged.emit()

    def selected_components(self):
        selected = []
        stack = [self.tree.topLevelItem(index)
                 for index in range(self.tree.topLevelItemCount())]
        while stack:
            item = stack.pop()
            key = item.data(0, _GEOMETRY_COMPONENT_ROLE)
            component = self._components_by_key.get(key)
            if (component is not None
                    and item.checkState(0) == Qt.CheckState.Checked):
                selected.append(component)
            stack.extend(
                item.child(index) for index in range(item.childCount()))
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
        stack = [self.tree.topLevelItem(index)
                 for index in range(self.tree.topLevelItemCount())]
        while stack:
            item = stack.pop()
            owner = item.data(0, Qt.ItemDataRole.UserRole)
            if owner and item.checkState(0) == Qt.CheckState.Checked:
                selected.append(str(owner))
            stack.extend(
                item.child(index) for index in range(item.childCount()))
        return tuple(sorted(selected))

    def _selection_changed(self, _item, _column) -> None:
        # Qt finishes propagating a parent checkbox before the shape is updated.
        # Rebuilding rows inside itemChanged can invalidate Qt's current children.
        self._selection_timer.start(0)

    def _flush_selection_changed(self) -> None:
        self._selection_timer.stop()
        self.summary_label.setText(
            f"{len(self._triangles_by_owner)} geometry parts; "
            f"{len(self.selected_components())} components selected.")
        self._update_component_highlight(self.tree.currentItem(), None)
        self.selectionChanged.emit()

    def _update_component_highlight(self, item, _previous) -> None:
        indices = set()
        for item in self.tree.selectedItems() if self._shape is not None else ():
            key = item.data(0, _GEOMETRY_COMPONENT_ROLE)
            owner = item.data(0, Qt.ItemDataRole.UserRole)
            indices.update(index for index, hull in enumerate(self._shape.hulls)
                       if (hull.source_component or f"@hull:{index}") == key
                       or (owner and (hull.source_component.startswith(str(owner) + "#")
                           or hull.source_component.startswith(str(owner).rstrip("/") + "/")))
                       or item is self._shape_root)
        self.shapeHighlightChanged.emit(indices)
        if self._shape is not None:
            if self._on_component_highlight is not None:
                self._on_component_highlight(None)
            return
        if self._on_component_highlight is None:
            return
        triangles = []
        for item in self.tree.selectedItems():
            key = item.data(0, _GEOMETRY_COMPONENT_ROLE)
            component = self._components_by_key.get(key)
            if component is not None:
                if item.checkState(0) == Qt.CheckState.Checked:
                    triangles.extend(component.triangles)
                continue
            owner = item.data(0, Qt.ItemDataRole.UserRole)
            if owner:
                prefix = str(owner).rstrip("/")
                triangles.extend(triangle
                    for path, owner_triangles in self._triangles_by_owner.items()
                    if path == prefix or path.startswith(prefix + "/")
                    for triangle in owner_triangles)
        self._on_component_highlight(tuple(triangles) or None)


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
