import os
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from studio.workbench.effect_catalog import FrameSequence
from studio.workbench.window_emitters import _EmitterAnchorPick, _EmittersMixin
from studio.rendering.asset_viewport_paint import _PaintingMixin


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("cut", [False, True])
def test_copy_and_cut_immediately_start_animated_placement(monkeypatch, cut):
    block = SimpleNamespace(ade_point_id=0)
    owner = SimpleNamespace(base_object=SimpleNamespace(ades=[block]))
    family = object()
    image = QImage(2, 2, QImage.Format.Format_ARGB32)
    image.fill(QColor("red"))
    preview = FrameSequence([image, image], (50, 100))
    make_preview = Mock(return_value=preview)
    monkeypatch.setattr("studio.workbench.window_emitters.emitter_preview", make_preview)

    class Window(_EmittersMixin):
        _family = family
        _workbench_obj = owner
        _selected_emitter = lambda self: (0, block)
        _material_structure_reason = lambda self: None
        _sync_emitter_actions = lambda self: None
        _notify = lambda *args: None
        _pick_emitter_anchor = Mock()

    window = Window()
    window._copy_emitter(cut=cut)
    preview_block = make_preview.call_args.args[1]
    assert preview_block is block if cut else preview_block is not block
    assert owner.base_object.ades == [block]
    assert window._pick_emitter_anchor.call_count == 1
    assert window._pick_emitter_anchor.call_args.kwargs["preview"] is preview
    assert window._pick_emitter_anchor.call_args.kwargs["cut_block"] is (block if cut else None)


def test_cut_preview_clears_its_marker_on_cancel(app):
    class Viewport(QWidget):
        edit_owner = "root"
        animation_speed = 1.0

        def _nearest_edit_vertex(self, position, radius):
            return None

    viewport = Viewport()
    viewport.resize(200, 200)
    image = QImage(2, 2, QImage.Format.Format_ARGB32)
    image.fill(QColor("red"))
    block = object()
    callback = Mock()
    picker = _EmitterAnchorPick(SimpleNamespace(viewport=viewport), callback,
                                FrameSequence([image, image], (50, 100)),
                                cut_block=block)
    assert viewport._emitter_cut_block is block
    assert picker.ghost_timer.isActive()
    color = picker.ghost_frames.images[0].pixelColor(0, 0)
    assert color.red() == color.green() == color.blue()
    assert 0 < color.alpha() < 255
    QTest.keyClick(viewport, Qt.Key.Key_Escape)
    assert viewport._emitter_cut_block is None
    assert not picker.ghost_timer.isActive()
    callback.assert_not_called()
    viewport.close()


def test_preview_confirms_clicked_anchor(app):
    class Viewport(QWidget):
        edit_owner = "root"
        animation_speed = 1.0

        def _nearest_edit_vertex(self, position, radius):
            return 1

        def _edit_screen_points(self):
            return [QPointF(10, 10), QPointF(20, 20)]

    viewport = Viewport()
    viewport.resize(200, 200)
    callback = Mock()
    block = SimpleNamespace(ade_point_id=0)
    picker = _EmitterAnchorPick(SimpleNamespace(viewport=viewport), callback,
                                cut_block=block)
    QTest.mouseClick(viewport, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
    callback.assert_called_once_with(1)
    assert viewport._emitter_cut_block is None
    viewport.close()


@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("hover", [None, 0])
def test_cut_hides_only_marker_without_another_emitter(shared, hover):
    block = SimpleNamespace(class_id="particle.class", ade_point_id=0)
    blocks = [block]
    if shared:
        blocks.append(SimpleNamespace(class_id="particle.class", ade_point_id=0))
    session = SimpleNamespace(
        fam_obj=SimpleNamespace(base_object=SimpleNamespace(ades=blocks)),
        selection=set(), selection_pivot=lambda: None)
    viewport = SimpleNamespace(
        _edit_session=session, _edit_screen_points=lambda *args: [QPointF(20, 20)],
        _available_edit_vertices=lambda screen: {0}, _modal_op=None,
        _edit_read_only_vertices=set(), _edit_visible_vertices={0},
        _direct_transform_mode="move", _active_edit_vertex=None,
        _draw_gizmo_handles=Mock(), _emitter_anchor_hover=hover,
        _emitter_cut_block=block, _box_rect=None, _draw_mode_label=Mock())
    painter = Mock()
    _PaintingMixin._draw_edit_overlay(viewport, painter, QRectF(0, 0, 50, 50), {})
    assert painter.drawEllipse.call_count == int(shared)


@pytest.mark.parametrize("cut", [False, True])
def test_placement_commits_only_after_anchor_is_chosen(monkeypatch, cut):
    block = SimpleNamespace(ade_point_id=0)
    owner = SimpleNamespace(base_object=SimpleNamespace(ades=[block]))
    family = object()
    moved = SimpleNamespace(ade_point_id=1)
    monkeypatch.setattr("studio.workbench.window_emitters.emitter_preview",
                        lambda *args, **kwargs: FrameSequence([], ()))
    monkeypatch.setattr("studio.workbench.window_emitters.copy_particle_emitter",
                        lambda *args: moved)

    class Window(_EmittersMixin):
        _family = family
        _workbench_obj = owner
        _selected_emitter = lambda self: (0, block)
        _material_structure_reason = lambda self: None
        _sync_emitter_actions = lambda self: None
        _notify = lambda *args: None
        _pick_emitter_anchor = Mock()
        _insert_emitter = Mock()

        def _edit_emitter_blocks(self, label, operation):
            operation()
            return True

    window = Window()
    window._copy_emitter(cut=cut)
    callback = window._pick_emitter_anchor.call_args.args[0]
    assert owner.base_object.ades == [block]
    callback(1)
    if cut:
        assert owner.base_object.ades == [moved]
    else:
        window._insert_emitter.assert_called_once()
        assert owner.base_object.ades == [block]
