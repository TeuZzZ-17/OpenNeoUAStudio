import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QKeyEvent, QPainter
from PySide6.QtWidgets import QApplication

from assembly_viewer import (
    PASTE_GHOST_OPACITY,
    AssetViewport,
    ViewFace,
    ViewMaterial,
)
from base_mapping_editor import MappingIndex
from base_parser import AmeshBlock, AttsEntry
from geometry_editor import GeometryEditSession, build_geometry_clipboard
from sklt_parser import SkltModel


def _fixture():
    model = SkltModel(
        source_name="TEST.SKLT",
        points=[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0),
                (0.0, 1.0, 0.0)],
        polygons=[[0, 1, 2]],
        parsed_polygon_count=1,
    )
    block = AmeshBlock(
        class_id="amesh.class",
        atts=[AttsEntry(0, 1, 2, 3, 0)],
        olpl=[[(0, 0), (255, 0), (0, 255)]],
    )
    obj = SimpleNamespace(
        skeleton=model,
        base_object=SimpleNamespace(ades=[block]),
    )
    clipboard = build_geometry_clipboard(
        obj, "root", {0}, MappingIndex(obj))
    viewport = AssetViewport()
    viewport.resize(320, 240)
    viewport._backface_cull = False
    viewport._edit_session = GeometryEditSession(
        obj,
        [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
        (0.0, 0.0, 0.0),
    )
    viewport._edit_owner = "root"
    viewport._materials = [ViewMaterial("test")]
    viewport._faces = [ViewFace(
        vertices=list(model.points),
        uvs=list(block.olpl[0]),
        material=0,
        poly_id=0,
        owner="root",
    )]
    viewport._selected_owner = "root"
    viewport._owner_bounds["root"] = (0.0, 0.0, 0.0, 1.0, 1.0, 0.0)
    return viewport, model, clipboard


class GeometryPastePreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_mouse_maps_to_source_pivot_view_plane_without_mutation(self):
        viewport, model, clipboard = _fixture()
        before_points = list(model.points)
        before_polygons = [list(poly) for poly in model.polygons]
        source_world = clipboard.pivot
        source_screen = viewport._project(
            viewport._camera_vertex(source_world)).toPoint()
        target = source_screen + QPoint(36, -24)
        self.assertTrue(viewport.begin_paste_preview(clipboard, target))
        delta = viewport.paste_preview_delta
        destination = tuple(
            clipboard.pivot[axis] + delta[axis] for axis in range(3))
        projected = viewport._project(viewport._camera_vertex(destination))
        self.assertAlmostEqual(projected.x(), target.x(), delta=1.1)
        self.assertAlmostEqual(projected.y(), target.y(), delta=1.1)
        self.assertEqual(model.points, before_points)
        self.assertEqual(model.polygons, before_polygons)
        self.assertFalse(viewport.edit_session.dirty)

    def test_axis_numeric_nudge_and_cancel_share_one_preview_state(self):
        viewport, model, clipboard = _fixture()
        start = viewport._project(
            viewport._camera_vertex(clipboard.pivot)).toPoint()
        viewport.begin_paste_preview(clipboard, start)
        viewport._paste_key_press(QKeyEvent(
            QEvent.Type.KeyPress, Qt.Key.Key_X,
            Qt.KeyboardModifier.NoModifier, "x"))
        for text, key in (("1", Qt.Key.Key_1), ("2", Qt.Key.Key_2)):
            viewport._paste_key_press(QKeyEvent(
                QEvent.Type.KeyPress, key,
                Qt.KeyboardModifier.NoModifier, text))
        self.assertEqual(viewport._paste_preview.axis, "X")
        self.assertAlmostEqual(viewport.paste_preview_delta[0], 12.0)
        before_nudge = viewport.paste_preview_delta
        self.assertTrue(viewport.nudge_paste_preview(8.0, 0.0))
        self.assertNotEqual(viewport.paste_preview_delta, before_nudge)
        self.assertTrue(viewport.cancel_paste_preview())
        self.assertFalse(viewport.paste_preview_active)
        self.assertEqual(len(model.points), 3)
        self.assertEqual(len(model.polygons), 1)
        self.assertFalse(viewport.edit_session.can_undo)

    def test_ghost_and_guides_do_not_enter_pick_shapes(self):
        viewport, _model, clipboard = _fixture()
        # This test covers overlay/picking isolation, not the fail-closed
        # Retail indexed backend (the synthetic fixture has no SET profile).
        viewport.set_mode("materials")
        start = viewport._project(
            viewport._camera_vertex(clipboard.pivot)).toPoint()
        viewport.set_show_owner_bbox(False)
        viewport.begin_paste_preview(clipboard, start)
        image = QImage(320, 240, QImage.Format.Format_ARGB32)
        image.fill(QColor(20, 20, 20))
        painter = QPainter(image)
        viewport._render_scene(
            painter, QRectF(0, 0, 320, 240), QColor(20, 20, 20), False,
            viewport._camera_state())
        painter.end()
        self.assertEqual(len(viewport._pick_shapes), 1)
        self.assertEqual(viewport._pick_shapes[0].face.poly_id, 0)

    def test_low_ghost_opacity_and_model_nudge_do_not_mutate_model(self):
        viewport, model, clipboard = _fixture()
        start = viewport._project(
            viewport._camera_vertex(clipboard.pivot)).toPoint()
        viewport.begin_paste_preview(clipboard, start)
        before = list(model.points)
        self.assertGreaterEqual(PASTE_GHOST_OPACITY, 0.16)
        self.assertLessEqual(PASTE_GHOST_OPACITY, 0.22)
        before_delta = viewport.paste_preview_delta
        self.assertTrue(
            viewport.nudge_paste_preview_model(0.0, 2.0, -2.0))
        delta = viewport.paste_preview_delta
        self.assertAlmostEqual(delta[0], before_delta[0])
        self.assertAlmostEqual(delta[1] - before_delta[1], 2.0)
        self.assertAlmostEqual(delta[2] - before_delta[2], -2.0)
        self.assertEqual(model.points, before)

    def test_lmb_enter_confirm_and_escape_rmb_cancel(self):
        from PySide6.QtTest import QTest

        viewport, _model, clipboard = _fixture()
        start = viewport._project(
            viewport._camera_vertex(clipboard.pivot)).toPoint()
        confirmations = []
        viewport.pastePreviewConfirmRequested.connect(
            lambda: confirmations.append(True))

        viewport.begin_paste_preview(clipboard, start)
        QTest.mouseClick(
            viewport, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier, start)
        self.assertEqual(len(confirmations), 1)
        viewport.cancel_paste_preview()

        viewport.begin_paste_preview(clipboard, start)
        viewport._paste_key_press(QKeyEvent(
            QEvent.Type.KeyPress, Qt.Key.Key_Return,
            Qt.KeyboardModifier.NoModifier))
        self.assertEqual(len(confirmations), 2)
        viewport.cancel_paste_preview()

        viewport.begin_paste_preview(clipboard, start)
        viewport._paste_key_press(QKeyEvent(
            QEvent.Type.KeyPress, Qt.Key.Key_Escape,
            Qt.KeyboardModifier.NoModifier))
        self.assertFalse(viewport.paste_preview_active)

        viewport.begin_paste_preview(clipboard, start)
        QTest.mouseClick(
            viewport, Qt.MouseButton.RightButton,
            Qt.KeyboardModifier.NoModifier, start)
        self.assertFalse(viewport.paste_preview_active)
        self.assertTrue(viewport.consume_context_menu_suppression())


if __name__ == "__main__":
    unittest.main()
