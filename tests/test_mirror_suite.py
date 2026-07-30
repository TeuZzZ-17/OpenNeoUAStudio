import copy
import os
import unittest
from itertools import product
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PySide6.QtWidgets import QMessageBox

from anm_parser import VanmData, VanmFrame
from assembly_window import AssemblyWindow
from asset_family import AssetFamily, FamilyObject, MaterialGroup
from base_parser import AmeshBlock, AttsEntry, BaseObject, TextureRef
from geometry_editor import match_mirror_points, match_mirror_polygons
from fx_element_editor import (
    build_fx_elements_clipboard,
    detect_fx_elements,
    validate_fx_element_clipboard,
)
from sklt_parser import SkltModel


def _octants():
    return [
        (x, y, z)
        for x, y, z in product(
            (1.0, -1.0), (2.0, -2.0), (3.0, -3.0))
    ]


def _family(points, polygons):
    model = SkltModel(
        source_name="MIRROR.SKLT",
        points=list(points),
        polygons=[list(polygon) for polygon in polygons],
        parsed_polygon_count=len(polygons),
        poo2_payload_offset=12,
        poo2_payload_size=max(12, len(points) * 12),
        pol2_payload_offset=128,
        pol2_payload_size=max(8, len(polygons) * 8),
    )
    block = AmeshBlock(
        class_id="amesh.class",
        texture=TextureRef(
            class_id="ilbm.class", kind="ilbm", name="BODY.ILBM"),
        atts_chunk_offset=200,
        olpl_chunk_offset=240,
        atts=[
            AttsEntry(poly_id, 10 + poly_id, 20 + poly_id,
                      30 + poly_id, 40 + poly_id)
            for poly_id in range(len(polygons))
        ],
        olpl=[
            [(index * 30, poly_id * 20 + index * 10)
             for index in range(len(polygon))]
            for poly_id, polygon in enumerate(polygons)
        ],
    )
    base = BaseObject(
        name="MIRROR", skeleton_name="MIRROR.SKLT", ades=[block])
    obj = FamilyObject(
        base_object=base,
        skeleton_ref=SimpleNamespace(
            path=None, status="memory", source="memory"),
        skeleton=model,
        materials=[MaterialGroup(
            "BODY.ILBM", texture_name="BODY.ILBM", kind="ilbm",
            block=block,
            faces=[
                (poly_id, list(block.olpl[poly_id]),
                 block.atts[poly_id].shade_val)
                for poly_id in range(len(polygons))
            ])],
        owner_path="root",
    )
    return AssetFamily(root_object=obj), obj, model, block


def _symmetric_pair():
    return (
        [
            (1.0, 0.0, 0.0), (2.0, 0.0, 0.0),
            (1.0, 1.0, 0.0),
            (-1.0, 0.0, 0.0), (-2.0, 0.0, 0.0),
            (-1.0, 1.0, 0.0),
        ],
        [[0, 1, 2], [3, 5, 4]],
    )


def _fx_symmetric_pair(fx_name="FX1", *, vanm=False, shared=False):
    points, polygons = _symmetric_pair()
    if shared:
        points.extend([
            (2.0, -1.0, 0.0), (2.0, -2.0, 0.0),
            (-2.0, -1.0, 0.0), (-2.0, -2.0, 0.0),
        ])
        polygons.extend([[0, 6, 7], [3, 8, 9]])
    model = SkltModel(
        source_name="MIRROR_FX.SKLT",
        points=points,
        polygons=polygons,
        parsed_polygon_count=len(polygons),
        poo2_payload_offset=12,
        poo2_payload_size=len(points) * 12,
        pol2_payload_offset=128,
        pol2_payload_size=max(8, len(polygons) * 8),
    )
    texture = TextureRef(
        class_id="bmpanim.class" if vanm else "ilbm.class",
        kind="bmpanim" if vanm else "ilbm",
        name="MIRROR.ANM" if vanm else f"{fx_name}.ILBM",
    )
    uvs = [(0, 0), (255, 0), (0, 255)]
    fx_block = AmeshBlock(
        class_id="amesh.class",
        texture=texture,
        atts_chunk_offset=200,
        olpl_chunk_offset=-1 if vanm else 240,
        atts=[
            AttsEntry(poly_id, 10 + poly_id, 20 + poly_id,
                      30 + poly_id, 40 + poly_id)
            for poly_id in (0, 1)
        ],
        olpl=[] if vanm else [list(uvs), list(reversed(uvs))],
    )
    blocks = [fx_block]
    materials = [MaterialGroup(
        texture.name, texture_name=texture.name, kind=texture.kind,
        block=fx_block,
        faces=[
            (0, list(uvs), 20),
            (1, list(reversed(uvs)), 21),
        ])]
    if shared:
        static = AmeshBlock(
            class_id="amesh.class",
            texture=TextureRef(
                class_id="ilbm.class", kind="ilbm", name="BODY.ILBM"),
            atts_chunk_offset=300,
            olpl_chunk_offset=340,
            atts=[
                AttsEntry(2, 1, 2, 3, 4),
                AttsEntry(3, 1, 2, 3, 4),
            ],
            olpl=[list(uvs), list(reversed(uvs))],
        )
        blocks.append(static)
        materials.append(MaterialGroup(
            "BODY.ILBM", texture_name="BODY.ILBM", kind="ilbm",
            block=static,
            faces=[
                (2, list(uvs), 2),
                (3, list(reversed(uvs)), 2),
            ]))
    base = BaseObject(
        name="MIRROR_FX", skeleton_name="MIRROR_FX.SKLT", ades=blocks)
    obj = FamilyObject(
        base_object=base,
        skeleton_ref=SimpleNamespace(
            path=None, status="memory", source="memory"),
        skeleton=model,
        materials=materials,
        owner_path="root",
    )
    animations = {}
    if vanm:
        animations["MIRROR.ANM"] = VanmData(
            source_name="MIRROR.ANM",
            bitmap_class="ilbm.class",
            bitmap_names=[f"{fx_name}.ILBM"],
            texcoord_groups=[list(uvs)],
            frames=[VanmFrame(128, 0, 0)],
        )
    return AssetFamily(
        root_object=obj, animations=animations), obj, model


def _fx_bilateral_symmetric_pair(fx_name="FX1", *, vanm=False):
    points = [
        (1.0, 0.0, 0.0), (2.0, 0.0, 0.0),
        (2.0, 1.0, 0.0), (1.0, 1.0, 0.0),
        (-1.0, 0.0, 0.0), (-2.0, 0.0, 0.0),
        (-2.0, 1.0, 0.0), (-1.0, 1.0, 0.0),
    ]
    polygons = [
        [0, 1, 2, 3], [3, 2, 1, 0],
        [4, 7, 6, 5], [5, 6, 7, 4],
    ]
    model = SkltModel(
        source_name="MIRROR_BILATERAL_FX.SKLT",
        points=points,
        polygons=polygons,
        parsed_polygon_count=len(polygons),
        poo2_payload_offset=12,
        poo2_payload_size=len(points) * 12,
        pol2_payload_offset=128,
        pol2_payload_size=max(8, len(polygons) * 10),
    )
    texture = TextureRef(
        class_id="bmpanim.class" if vanm else "ilbm.class",
        kind="bmpanim" if vanm else "ilbm",
        name="MIRROR_BILATERAL.ANM" if vanm else f"{fx_name}.ILBM",
    )
    uvs = [(0, 0), (255, 0), (255, 255), (0, 255)]
    block = AmeshBlock(
        class_id="amesh.class",
        texture=texture,
        atts_chunk_offset=200,
        olpl_chunk_offset=-1 if vanm else 240,
        atts=[
            AttsEntry(poly_id, 10 + poly_id, 20 + poly_id,
                      30 + poly_id, 40 + poly_id)
            for poly_id in range(4)
        ],
        olpl=[] if vanm else [
            list(uvs), list(reversed(uvs)),
            list(uvs), list(reversed(uvs)),
        ],
    )
    base = BaseObject(
        name="MIRROR_BILATERAL_FX",
        skeleton_name="MIRROR_BILATERAL_FX.SKLT",
        ades=[block],
    )
    obj = FamilyObject(
        base_object=base,
        skeleton_ref=SimpleNamespace(
            path=None, status="memory", source="memory"),
        skeleton=model,
        materials=[MaterialGroup(
            texture.name,
            texture_name=texture.name,
            kind=texture.kind,
            block=block,
            faces=[
                (poly_id,
                 list(reversed(uvs)) if poly_id % 2 else list(uvs),
                 20 + poly_id)
                for poly_id in range(4)
            ],
        )],
        owner_path="root",
    )
    animations = {}
    if vanm:
        animations["MIRROR_BILATERAL.ANM"] = VanmData(
            source_name="MIRROR_BILATERAL.ANM",
            bitmap_class="ilbm.class",
            bitmap_names=[f"{fx_name}.ILBM"],
            texcoord_groups=[list(uvs)],
            frames=[VanmFrame(128, 0, 0)],
        )
    return AssetFamily(
        root_object=obj, animations=animations), obj, model


class MirrorMatcherTests(unittest.TestCase):
    def test_every_axis_combination_uses_one_deduplicated_orbit(self):
        points = _octants()
        cases = {
            ("X",): 1,
            ("Y",): 1,
            ("Z",): 1,
            ("X", "Y"): 3,
            ("X", "Z"): 3,
            ("Y", "Z"): 3,
            ("X", "Y", "Z"): 7,
        }
        for axes, expected in cases.items():
            with self.subTest(axes=axes):
                result = match_mirror_points(points, {0}, axes)
                self.assertEqual(len(result.recipient_indices), expected)
                self.assertEqual(
                    len(result.recipient_indices),
                    len(set(result.recipient_indices)))

    def test_tolerance_partial_missing_and_ambiguous_are_explicit(self):
        tolerant = match_mirror_points(
            [(1.0, 2.0, 3.0), (-1.000001, 2.0, 3.0)],
            {0}, ("X",))
        self.assertEqual(tolerant.recipient_indices, {1})

        partial = match_mirror_points(
            [(1.0, 1.0, 0.0), (-1.0, 1.0, 0.0)],
            {0}, ("X", "Y", "Z"))
        self.assertEqual(partial.recipient_indices, {1})

        missing = match_mirror_points(
            [(1.0, 0.0, 0.0)], {0}, ("X",))
        self.assertEqual(missing.unmatched, {0})

        ambiguous = match_mirror_points(
            [(1.0, 0.0, 0.0),
             (-1.0, 0.0, 0.0),
             (-1.0, 0.0, 0.0)],
            {0}, ("X",))
        self.assertEqual(ambiguous.recipient_indices, set())
        self.assertEqual(ambiguous.ambiguous[0][0], 0)

    def test_central_points_are_deterministic_without_modal_transform(self):
        points = [(0.0, 2.0, 3.0), (0.0, -2.0, 3.0)]
        first = match_mirror_points(points, {0}, ("X", "Y", "Z"))
        second = match_mirror_points(points, {0}, ("X", "Y", "Z"))
        self.assertEqual(first, second)
        self.assertIn(0, first.self_mirrored)
        self.assertEqual(first.recipient_indices, {1})

    def test_polygon_matching_accepts_cyclic_and_reverse_winding(self):
        points, _polygons = _symmetric_pair()
        for target in ([3, 5, 4], [5, 4, 3]):
            with self.subTest(target=target):
                result = match_mirror_polygons(
                    points, [[0, 1, 2], target],
                    {0}, ("X", "Y", "Z"))
                self.assertEqual(result.recipient_indices, {1})

    def test_overlapping_polygon_candidates_are_ambiguous(self):
        points, _polygons = _symmetric_pair()
        result = match_mirror_polygons(
            points,
            [[0, 1, 2], [3, 4, 5], [5, 3, 4]],
            {0}, ("X", "Y", "Z"))
        self.assertEqual(result.recipient_indices, set())
        self.assertEqual(result.ambiguous[0][0], 0)


class MirrorSelectModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _window(self, points, polygons):
        family, obj, model, block = _family(points, polygons)
        window = AssemblyWindow()
        window._set_family(family)
        window._show_model_editor()
        return window, obj, model, block

    def test_enabling_mode_expands_existing_selection_and_keeps_source(self):
        points, polygons = _symmetric_pair()
        window, _obj, model, block = self._window(points, polygons)
        try:
            window._on_polygon_picked(0)
            before = (
                copy.deepcopy(model.points),
                copy.deepcopy(model.polygons),
                copy.deepcopy(block.atts),
                copy.deepcopy(block.olpl),
            )
            window.mirror_select_check.setChecked(True)
            self.assertTrue(window.mirror_select_check.isChecked())
            self.assertEqual(window._selected_polys, {0, 1})
            self.assertEqual(
                window.viewport.edit_session.selection, set(range(6)))
            self.assertEqual(model.points, before[0])
            self.assertEqual(model.polygons, before[1])
            self.assertEqual(block.atts, before[2])
            self.assertEqual(block.olpl, before[3])
            self.assertEqual(window._edit_undo_stack, [])
            self.assertNotIn("root", window._geom_dirty)
        finally:
            window.close()

    def test_active_mode_expands_every_new_polygon_pick(self):
        points, polygons = _symmetric_pair()
        window, _obj, _model, _block = self._window(points, polygons)
        try:
            window.mirror_select_check.setChecked(True)
            window._on_polygon_picked(0)
            self.assertEqual(window._selected_polys, {0, 1})
            self.assertEqual(
                window.viewport.edit_session.selection, set(range(6)))
        finally:
            window.close()

    def test_disabling_mode_keeps_only_the_original_source_selected(self):
        points, polygons = _symmetric_pair()
        window, _obj, _model, _block = self._window(points, polygons)
        try:
            window.mirror_select_check.setChecked(True)
            window._on_polygon_picked(0)
            self.assertEqual(window._selected_polys, {0, 1})
            window.mirror_select_check.setChecked(False)
            self.assertEqual(window._selected_polys, {0})
            self.assertEqual(
                window.viewport.edit_session.selection, {0, 1, 2})
            self.assertFalse(
                window.viewport.edit_session.mirror_enabled)
            self.assertEqual(
                window.viewport.edit_session.mirror_source_selection,
                set())
        finally:
            window.close()

    def test_checkbox_moves_left_for_rotate_and_scale(self):
        points, polygons = _symmetric_pair()
        window, _obj, _model, _block = self._window(points, polygons)
        try:
            layout = window._transform_options_layout

            def position(widget):
                return layout.getItemPosition(layout.indexOf(widget))[:2]

            window._set_transform_mode("move")
            self.assertEqual(position(window.mirror_select_check), (0, 0))
            self.assertEqual(position(window.auto_align_check), (0, 1))
            for mode in ("rotate", "scale"):
                window._set_transform_mode(mode)
                self.assertEqual(
                    position(window.mirror_select_check), (0, 0))
                self.assertFalse(window.auto_align_check.isVisible())
        finally:
            window.close()

    def test_missing_or_ambiguous_counterpart_preserves_source(self):
        window, _obj, _model, _block = self._window(
            [(1.0, 0.0, 0.0),
             (-1.0, 0.0, 0.0),
             (-1.0, 0.0, 0.0)],
            [[0], [1], [2]])
        try:
            window.mirror_select_check.setChecked(True)
            window._on_polygon_picked(0)
            self.assertEqual(window._selected_polys, {0})
            self.assertEqual(window.viewport.edit_session.selection, {0})
        finally:
            window.close()

    def test_vertex_gesture_keeps_source_and_adds_counterpart(self):
        points, polygons = _symmetric_pair()
        window, _obj, model, _block = self._window(points, polygons)
        try:
            session = window.viewport.edit_session
            window.mirror_select_check.setChecked(True)
            session.set_selection({0})
            before = copy.deepcopy(model.points)
            window._on_edit_selection_details_changed({0}, "vertex")
            self.assertEqual(session.selection, {0, 3})
            self.assertEqual(window._selected_polys, set())
            self.assertEqual(model.points, before)
        finally:
            window.close()

    def test_normal_copy_receives_both_selected_halves(self):
        points, polygons = _symmetric_pair()
        window, _obj, _model, _block = self._window(points, polygons)
        try:
            window.mirror_select_check.setChecked(True)
            window._on_polygon_picked(0)
            window._copy_geometry()
            self.assertIsNotNone(window._geometry_clipboard)
            self.assertEqual(len(window._geometry_clipboard.polygons), 2)
            self.assertTrue(window.viewport.paste_preview_active)
        finally:
            window.close()

    def test_normal_delete_removes_both_selected_halves_and_is_undoable(self):
        points, polygons = _symmetric_pair()
        points += [
            (0.0, 2.0, 0.0),
            (0.0, 3.0, 0.0),
            (0.0, 2.0, 1.0),
        ]
        polygons += [[6, 7, 8]]
        window, _obj, model, _block = self._window(points, polygons)
        try:
            window.mirror_select_check.setChecked(True)
            window._on_polygon_picked(0)
            self.assertEqual(window._resolved_geometry_polys(), {0, 1})
            with patch.object(
                    QMessageBox, "question",
                    return_value=QMessageBox.StandardButton.Yes):
                window._delete_geometry()
            self.assertEqual(len(model.polygons), 1)
            self.assertEqual(len(window._edit_undo_stack), 1)
            window._undo_edit()
            self.assertEqual(len(model.polygons), 3)
        finally:
            window.close()

    def test_move_rotate_and_scale_use_true_mirrored_propagation(self):
        points, polygons = _symmetric_pair()
        for operation in ("move", "rotate", "scale"):
            with self.subTest(operation=operation):
                window, _obj, _model, _block = self._window(
                    points, polygons)
                try:
                    window.mirror_select_check.setChecked(True)
                    window._on_polygon_picked(0)
                    session = window.viewport.edit_session
                    self.assertEqual(
                        session.mirror_source_selection, {0, 1, 2})
                    self.assertTrue(session.begin_modal())
                    if operation == "move":
                        session.preview_grab((0.5, 0.25, 0.0))
                    elif operation == "rotate":
                        session.preview_rotate(
                            (0.0, 0.0, 1.0), 0.4)
                    else:
                        session.preview_scale_axes((1.4, 0.8, 1.0))
                    pending = session.points()
                    self.assertEqual(
                        pending[3],
                        (-pending[0][0], pending[0][1], pending[0][2]))
                    self.assertEqual(
                        pending[4],
                        (-pending[1][0], pending[1][1], pending[1][2]))
                    self.assertEqual(
                        pending[5],
                        (-pending[2][0], pending[2][1], pending[2][2]))
                finally:
                    window.close()

    def test_fx1_fx2_direct_and_vanm_use_the_same_mirror_mechanics(self):
        for fx_name in ("FX1", "FX2"):
            for vanm in (False, True):
                with self.subTest(fx_name=fx_name, vanm=vanm):
                    family, obj, model = _fx_symmetric_pair(
                        fx_name, vanm=vanm)
                    window = AssemblyWindow()
                    try:
                        window._set_family(family)
                        window._show_model_editor()
                        self.assertEqual(len(window._fx_elements), 2)
                        window.mirror_select_check.setChecked(True)
                        window._on_polygon_picked(0)
                        self.assertEqual(window._selected_polys, {0, 1})
                        self.assertEqual(
                            window.viewport.edit_session.selection,
                            set(range(6)))
                        self.assertEqual(
                            window.viewport.edit_session
                            .mirror_source_selection,
                            {0, 1, 2})

                        session = window.viewport.edit_session
                        for operation in ("move", "rotate", "scale"):
                            self.assertTrue(session.begin_modal())
                            if operation == "move":
                                session.preview_grab((0.5, 0.25, 0.0))
                            elif operation == "rotate":
                                session.preview_rotate(
                                    (0.0, 0.0, 1.0), 0.4)
                            else:
                                session.preview_scale_axes(
                                    (1.4, 0.8, 1.0))
                            pending = session.points()
                            self.assertEqual(
                                pending[3],
                                (-pending[0][0], pending[0][1],
                                 pending[0][2]))
                            session.cancel_modal()

                        elements = window._active_fx_elements()
                        self.assertEqual(len(elements), 2)
                        clipboard = build_fx_elements_clipboard(
                            obj, elements, family.animations)
                        self.assertEqual(
                            clipboard.shared_state, "selection")
                        self.assertEqual(len(clipboard.polygons), 2)
                        validate_fx_element_clipboard(
                            obj, clipboard, family.animations)
                        window._copy_geometry()
                        self.assertEqual(
                            window._geometry_clipboard.shared_state,
                            "selection")
                        self.assertTrue(
                            window.viewport.paste_preview_active)
                        window.viewport.cancel_paste_preview()
                    finally:
                        window.close()

    def test_mirrored_bilateral_fx_pair_is_visually_selected_as_two_elements(self):
        for fx_name in ("FX1", "FX2"):
            for vanm in (False, True):
                with self.subTest(fx_name=fx_name, vanm=vanm):
                    family, _obj, model = _fx_bilateral_symmetric_pair(
                        fx_name, vanm=vanm)
                    window = AssemblyWindow()
                    try:
                        window._set_family(family)
                        window._show_model_editor()
                        self.assertEqual(len(window._fx_elements), 2)
                        self.assertTrue(all(
                            element.bilateral
                            for element in window._fx_elements))
                        window.mirror_select_check.setChecked(True)
                        window._on_polygon_picked(0)

                        self.assertEqual(
                            window._mirror_select_source_polys, {0, 1})
                        self.assertEqual(
                            window._selected_polys, {0, 1, 2, 3})
                        self.assertEqual(
                            window.viewport._highlight_polys, {0, 1, 2, 3})
                        self.assertEqual(
                            window.viewport.edit_session.selection,
                            set(range(8)))
                        self.assertEqual(
                            window.viewport.edit_session
                            .mirror_source_selection,
                            {0, 1, 2, 3})

                        session = window.viewport.edit_session
                        self.assertTrue(session.begin_modal())
                        session.preview_grab((0.5, 0.25, 0.0))
                        pending = session.points()
                        for source, recipient in (
                                (0, 4), (1, 5), (2, 6), (3, 7)):
                            self.assertEqual(
                                pending[recipient],
                                (-pending[source][0],
                                 pending[source][1],
                                 pending[source][2]))
                        session.cancel_modal()
                    finally:
                        window.close()

    def test_bilateral_fx_is_completed_when_one_face_is_picked(self):
        family, _obj, model = _fx_symmetric_pair("FX1")
        model.polygons[1] = list(reversed(model.polygons[0]))
        window = AssemblyWindow()
        try:
            window._set_family(family)
            window._show_model_editor()
            self.assertEqual(len(window._fx_elements), 1)
            self.assertTrue(window._fx_elements[0].bilateral)
            window.mirror_select_check.setChecked(True)
            window._on_polygon_picked(0)
            self.assertEqual(window._selected_polys, {0, 1})
            self.assertEqual(
                window.viewport.edit_session.selection, {0, 1, 2})
        finally:
            window.close()

    def test_shared_mirrored_fx_auto_detach_and_still_transform_together(self):
        family, _obj, model = _fx_symmetric_pair("FX2", shared=True)
        window = AssemblyWindow()
        try:
            window._set_family(family)
            window._show_model_editor()
            self.assertEqual(
                [element.shared_state for element in window._fx_elements],
                ["shared", "shared"])
            window.mirror_select_check.setChecked(True)
            with patch.object(
                    window, "_geometry_writer_reason", return_value=""):
                window._on_polygon_picked(0)
            self.assertEqual(window._selected_polys, {0, 1})
            self.assertTrue(all(
                element.editable for element in window._fx_elements))
            self.assertEqual(len(model.points), 12)
            session = window.viewport.edit_session
            self.assertTrue(session.begin_modal())
            session.preview_scale_axes((1.25, 0.75, 1.0))
            pending = session.points()
            source = model.polygons[0]
            recipient = model.polygons[1]
            for source_index, recipient_index in zip(
                    source, (recipient[0], recipient[2], recipient[1])):
                self.assertEqual(
                    pending[recipient_index],
                    (-pending[source_index][0],
                     pending[source_index][1],
                     pending[source_index][2]))
        finally:
            window.close()

    def test_mirrored_fx_delete_plan_contains_both_elements(self):
        family, _obj, _model = _fx_symmetric_pair("FX1")
        window = AssemblyWindow()
        try:
            window._set_family(family)
            window._show_model_editor()
            window.mirror_select_check.setChecked(True)
            window._on_polygon_picked(0)
            # Keep one non-FX polygon so structural deletion is legal.
            window.viewport.edit_session.model.points.extend([
                (0.0, 2.0, 0.0),
                (0.0, 3.0, 0.0),
                (0.0, 2.0, 1.0),
            ])
            window.viewport.edit_session.model.polygons.append([6, 7, 8])
            window.viewport.edit_session.model.parsed_polygon_count += 1
            block = AmeshBlock(
                class_id="amesh.class",
                texture=TextureRef(
                    class_id="ilbm.class", kind="ilbm",
                    name="BODY.ILBM"),
                atts_chunk_offset=400,
                olpl_chunk_offset=440,
                atts=[AttsEntry(2, 1, 2, 3, 4)],
                olpl=[[(0, 0), (255, 0), (0, 255)]],
            )
            window._workbench_obj.base_object.ades.append(block)
            plan = window._delete_geometry_plan()
            self.assertEqual(plan.deleted_polygon_indices, (0, 1))
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
