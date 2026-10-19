import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from entity_designer.editor import CollisionEditorWindow
from entity_designer.shape import CollisionHull, CollisionShape
from entity_designer.widgets import _GEOMETRY_COMPONENT_ROLE


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    yield instance


@pytest.fixture
def window(app):
    def hull(index, enabled=True):
        shift = float(index * 4)
        vertices = (
            (shift, 0.0, 0.0), (shift + 1.0, 0.0, 0.0),
            (shift, 1.0, 0.0), (shift, 0.0, 1.0),
        )
        faces = ((0, 2, 1), (0, 1, 3), (0, 3, 2), (1, 2, 3))
        return CollisionHull(vertices, faces, f"component-{index}", enabled)

    editor = CollisionEditorWindow()
    editor.project.collision_shape = CollisionShape(
        "Model", "a" * 64, (1.0, 1.0, 1.0), (0.0, 0.0, 0.0),
        tuple(hull(index) for index in range(3)))
    editor._sync_all()
    editor.properties_tabs.setCurrentIndex(editor.collision_shape_tab_index)
    editor.show()
    app.processEvents()
    editor._set_modified(False)
    yield editor
    editor._set_modified(False)
    editor.close()


def _select_rows_with_qtest(widget, rows):
    tree = widget.tree
    tree.expandAll()
    for offset, index in enumerate(rows):
        item = widget._shape_root.child(index)
        rect = tree.visualItemRect(item)
        assert rect.isValid()
        QTest.mouseClick(
            tree.viewport(), Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.ControlModifier if offset else
            Qt.KeyboardModifier.NoModifier,
            rect.center())
    QApplication.processEvents()


def _context_action_with_qtest(widget, key, action_text):
    tree = widget.tree
    item = next(row for row in widget._items()
                if row.data(0, _GEOMETRY_COMPONENT_ROLE) == key)
    rect = tree.visualItemRect(item)
    triggered = []

    def trigger_action():
        menu = QApplication.activePopupWidget()
        if menu is None:
            return
        action = next((candidate for candidate in menu.actions()
                       if candidate.text() == action_text), None)
        triggered.append(action.text() if action is not None else None)
        if action is not None:
            action.trigger()
        menu.close()

    QTimer.singleShot(0, trigger_action)
    widget._show_component_menu(rect.center())
    QApplication.processEvents()
    assert triggered == [action_text]


def _selected_keys(widget):
    return {item.data(0, _GEOMETRY_COMPONENT_ROLE)
            for item in widget.tree.selectedItems()}


def test_context_menu_disables_and_enables_selected_group_with_one_undo(window, app):
    widget = window.collision_geometry_widget
    _select_rows_with_qtest(widget, (0, 1))
    assert _selected_keys(widget) == {"component-0", "component-1"}
    original = window.project.collision_shape.hulls
    undo_count = len(window._undo)

    _context_action_with_qtest(widget, "component-0", "Disable")
    disabled = window.project.collision_shape.hulls
    assert [part.enabled for part in disabled] == [False, False, True]
    assert disabled[0] == type(original[0])(
        original[0].vertices, original[0].faces,
        original[0].source_component, False)
    assert disabled[1] == type(original[1])(
        original[1].vertices, original[1].faces,
        original[1].source_component, False)
    assert disabled[2] == original[2]
    assert _selected_keys(widget) == {"component-0", "component-1"}
    assert len(window._undo) == undo_count + 1

    window.undo()
    assert [part.enabled for part in window.project.collision_shape.hulls] == [
        True, True, True]
    window.redo()
    assert [part.enabled for part in window.project.collision_shape.hulls] == [
        False, False, True]

    undo_count = len(window._undo)
    _context_action_with_qtest(widget, "component-1", "Enable")
    assert [part.enabled for part in window.project.collision_shape.hulls] == [
        True, True, True]
    assert len(window._undo) == undo_count + 1


def test_mixed_group_action_follows_clicked_component_and_is_undoable(window, app):
    widget = window.collision_geometry_widget
    _select_rows_with_qtest(widget, (0, 1))
    widget._shape_root.child(1).setCheckState(0, Qt.CheckState.Unchecked)
    app.processEvents()
    mixed = window.project.collision_shape.hulls
    assert [part.enabled for part in mixed] == [True, False, True]
    window._undo.clear()
    window._redo.clear()

    _context_action_with_qtest(widget, "component-0", "Disable")
    disabled = window.project.collision_shape.hulls
    assert [part.enabled for part in disabled] == [False, False, True]
    assert disabled[2] == mixed[2]
    assert len(window._undo) == 1
    window.undo()
    assert window.project.collision_shape.hulls == mixed

    _context_action_with_qtest(widget, "component-1", "Enable")
    enabled = window.project.collision_shape.hulls
    assert [part.enabled for part in enabled] == [True, True, True]
    assert enabled[2] == mixed[2]
    assert len(window._undo) == 1
    window.undo()
    assert window.project.collision_shape.hulls == mixed


def test_context_clicking_unselected_component_changes_only_that_component(window, app):
    widget = window.collision_geometry_widget
    _select_rows_with_qtest(widget, (0, 1))
    _context_action_with_qtest(widget, "component-2", "Disable")
    assert [part.enabled for part in window.project.collision_shape.hulls] == [
        True, True, False]
    assert _selected_keys(widget) == {"component-2"}


def test_collision_shape_component_uses_open_and_closed_hand_cursors(window):
    viewport = window.viewport
    assert viewport._collision_shape_overlay_visible
    hull = window.project.collision_shape.hulls[0]
    triangle = [viewport._project_visible_world(hull.vertices[i])
                for i in hull.faces[0]]
    point = QPointF(sum(p.x() for p in triangle) / 3,
                    sum(p.y() for p in triangle) / 3).toPoint()
    assert viewport._hit_shape_component(QPointF(point)) == "component-0"

    QTest.mouseMove(viewport, point)
    assert viewport.cursor().shape() == Qt.CursorShape.OpenHandCursor

    blank = QPoint(2, 2)
    assert viewport._hit_shape_component(QPointF(blank)) is None
    QTest.mouseMove(viewport, blank)
    idle_cursor = viewport.cursor()
    assert idle_cursor.shape() not in (
        Qt.CursorShape.ArrowCursor,
        Qt.CursorShape.OpenHandCursor,
        Qt.CursorShape.ClosedHandCursor,
    )
    idle_pixmap = idle_cursor.pixmap()
    assert not idle_pixmap.isNull()
    image = idle_pixmap.toImage()
    assert any(image.pixelColor(x, y).alpha()
               for x in range(image.width())
               for y in range(image.height()))
    assert idle_cursor.hotSpot() == QPoint(11, 11)

    QTest.mouseMove(viewport, point)
    assert viewport.cursor().shape() == Qt.CursorShape.OpenHandCursor

    target = point + QPoint(12, 8)
    local = QPointF(target)
    global_pos = QPointF(viewport.mapToGlobal(target))
    press = QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(point),
        QPointF(viewport.mapToGlobal(point)),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(viewport, press)
    move = QMouseEvent(
        QEvent.Type.MouseMove, local, global_pos,
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(viewport, move)
    assert viewport.cursor().shape() == Qt.CursorShape.ClosedHandCursor
    release = QMouseEvent(
        QEvent.Type.MouseButtonRelease, local, global_pos,
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(viewport, release)
    assert viewport.cursor().shape() != Qt.CursorShape.ClosedHandCursor
    previous_tracking = viewport._shape_cursor_mouse_tracking_previous
    viewport.set_collision_shape_overlay_visible(False)
    assert viewport.hasMouseTracking() == previous_tracking
