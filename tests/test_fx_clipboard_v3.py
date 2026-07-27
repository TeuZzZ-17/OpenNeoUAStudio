import os
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from anm_parser import VanmData, VanmFrame
from assembly_window import AssemblyWindow
from asset_family import AssetFamily, FamilyObject, MaterialGroup
from base_mapping_editor import MappingIndex
from base_parser import (
    AmeshBlock,
    AttsEntry,
    BaseObject,
    TextureRef,
    parse_base_bytes,
)
from fx_element_editor import (
    FxElementClipboard,
    append_fx_element_clipboard,
    build_fx_element_clipboard,
    build_new_fx_clipboard,
    detect_fx_elements,
    validate_fx_element_clipboard,
)
from geometry_editor import (
    GeometryClipboardError,
    apply_delete_geometry,
    plan_delete_geometry,
)
from ilbm_parser import IlbmImage
from sklt_parser import (
    SkltModel,
    create_minimal_sklt_model,
    parse_sklt_file,
    save_sklt_with_poo2_pol2_structure,
)


UVS = [(0, 0), (255, 0), (255, 255), (0, 255)]


def _chunk(tag, payload):
    result = tag + struct.pack(">I", len(payload)) + payload
    return result + (b"\0" if len(payload) & 1 else b"")


def _form(form_type, children):
    return _chunk(b"FORM", form_type + children)


def _fx_base_bytes(fx_name="FX1", *, vanm=False, bilateral=False):
    entries = [
        struct.pack(">hBBBB", index, 12, 34, 56, 78)
        for index in range(2 if bilateral else 1)
    ]
    if vanm:
        texture = _form(
            b"OBJT",
            _chunk(b"CLID", b"bmpanim.class\0")
            + _form(
                b"BANI",
                _chunk(
                    b"STRC",
                    struct.pack(">hhh", 1, 6, 1) + b"GLOW.ANM\0")),
        )
        olpl = b""
    else:
        texture = _form(
            b"OBJT",
            _chunk(b"CLID", b"ilbm.class\0")
            + _form(
                b"CIBO",
                _chunk(b"NAM2", f"{fx_name}.ILBM\0".encode("ascii"))
                + _chunk(b"OTL2", bytes(value for uv in UVS for value in uv))),
        )
        groups = [UVS, list(reversed(UVS))] if bilateral else [UVS]
        olpl_payload = b"".join(
            struct.pack(">h", len(group))
            + bytes(value for uv in group for value in uv)
            for group in groups)
        olpl = _chunk(b"OLPL", olpl_payload)
    area = _form(
        b"AREA",
        _chunk(b"STRC", struct.pack(">hHHBBBB", 1, 0, 0x88, 0, 255, 0, 255))
        + texture,
    )
    amesh = _form(
        b"AMSH", area + _chunk(b"ATTS", b"".join(entries)) + olpl)
    material = _form(
        b"OBJT", _chunk(b"CLID", b"amesh.class\0") + amesh)
    skeleton = _form(
        b"OBJT",
        _chunk(b"CLID", b"sklt.class\0")
        + _form(b"SKLC", _chunk(b"NAME", b"FXTEST.SKLT\0")),
    )
    base = _form(
        b"BASE",
        _chunk(b"STRC", bytes(62)) + skeleton
        + _form(b"ADES", material),
    )
    return _form(
        b"MC2 ",
        _form(b"OBJT", _chunk(b"CLID", b"base.class\0") + base),
    )


def _animation(fx_name="FX1"):
    return VanmData(
        source_name="GLOW.ANM",
        bitmap_class="ilbm.class",
        bitmap_names=[f"{fx_name}.ILBM", f"{fx_name}.ILBM"],
        texcoord_groups=[list(UVS), list(reversed(UVS))],
        frames=[
            VanmFrame(128, 0, 0),
            VanmFrame(256, 1, 1),
        ],
    )


def _image(name):
    palette = [(255, 80, 30)] + [(0, 0, 0)] * 255
    return IlbmImage(
        source_name=name,
        kind="VBMP",
        width=2,
        height=2,
        palette=palette,
        pixels=bytes([0, 0, 0, 0]),
    )


def _memory_family(
        fx_name="FX1", *, vanm=False, bilateral=False,
        shared=False, invalid=False):
    points = [
        (-1.0, -1.0, 0.0),
        (1.0, -1.0, 0.0),
        (1.0, 1.0, 0.0),
        (-1.0, 1.0, 0.0),
    ]
    polygons = [[0, 1, 2, 3]]
    if bilateral:
        polygons.append([3, 2, 1, 0])
    if shared:
        points.extend([(2.0, 0.0, 0.0), (2.0, 1.0, 0.0)])
        polygons.append([0, 4, 5])
    model = SkltModel(
        source_name="FXTEST.SKLT",
        points=points,
        polygons=polygons,
        parsed_polygon_count=len(polygons),
        poo2_payload_offset=1,
        poo2_payload_size=len(points) * 12,
        pol2_payload_offset=2,
        pol2_payload_size=16,
    )
    texture = TextureRef(
        class_id="bmpanim.class" if vanm else "ilbm.class",
        kind="bmpanim" if vanm else "ilbm",
        name="GLOW.ANM" if vanm else f"{fx_name}.ILBM",
        anim_type=1 if vanm else None,
        outline_uvs=[] if vanm else list(UVS),
    )
    count = 2 if bilateral else 1
    fx_block = AmeshBlock(
        class_id="amesh.class",
        polflags=0x88,
        texture=texture,
        atts=[
            AttsEntry(99 if invalid else index, 12, 34, 56, 78)
            for index in range(count)
        ],
        olpl=[] if vanm else [
            list(UVS),
            *([list(reversed(UVS))] if bilateral else []),
        ],
        atts_chunk_offset=1,
        olpl_chunk_offset=-1 if vanm else 2,
    )
    blocks = [fx_block]
    if shared:
        blocks.append(AmeshBlock(
            class_id="amesh.class",
            texture=TextureRef(
                class_id="ilbm.class", kind="ilbm", name="STONE.ILBM"),
            atts=[AttsEntry(len(polygons) - 1, 1, 2, 3, 4)],
            olpl=[[(0, 0), (255, 0), (0, 255)]],
            atts_chunk_offset=3,
            olpl_chunk_offset=4,
        ))
    base_object = BaseObject(
        name="FXTEST", skeleton_name="FXTEST.SKLT", ades=blocks)
    animations = {"GLOW.ANM": _animation(fx_name)} if vanm else {}
    face_uvs = animations["GLOW.ANM"].texcoord_groups[0] if vanm else UVS
    materials = [MaterialGroup(
        texture.name,
        texture_name=texture.name,
        kind=texture.kind,
        block=fx_block,
        faces=[
            (entry.poly_id, list(face_uvs), entry.shade_val)
            for entry in fx_block.atts
            if 0 <= entry.poly_id < len(polygons)
        ],
    )]
    if shared:
        static = blocks[1]
        materials.append(MaterialGroup(
            "STONE.ILBM", texture_name="STONE.ILBM", kind="ilbm",
            block=static,
            faces=[(len(polygons) - 1, list(static.olpl[0]), 2)],
        ))
    obj = FamilyObject(
        base_object=base_object,
        skeleton_ref=SimpleNamespace(path=None, status="memory",
                                     source="memory"),
        skeleton=model,
        materials=materials,
        owner_path="root",
    )
    textures = {
        f"{fx_name}.ILBM": _image(f"{fx_name}.ILBM"),
        "STONE.ILBM": _image("STONE.ILBM"),
    }
    return AssetFamily(
        root_object=obj, animations=animations, textures=textures), obj


def _writable_family(root: Path, fx_name="FX1", *, vanm=False,
                     bilateral=False):
    polygons = [[0, 1, 2, 3]]
    if bilateral:
        polygons.append([3, 2, 1, 0])
    points = [
        (-1.0, -1.0, 0.0),
        (1.0, -1.0, 0.0),
        (1.0, 1.0, 0.0),
        (-1.0, 1.0, 0.0),
    ]
    sklt_path = root / "FXTEST.SKLT"
    base_path = root / "FXTEST.BASE"
    save_sklt_with_poo2_pol2_structure(
        create_minimal_sklt_model(), points, polygons, sklt_path)
    base_path.write_bytes(_fx_base_bytes(
        fx_name, vanm=vanm, bilateral=bilateral))
    model = parse_sklt_file(sklt_path)
    base_asset = parse_base_bytes(base_path.read_bytes(), str(base_path))
    block = base_asset.root.ades[0]
    animations = {"GLOW.ANM": _animation(fx_name)} if vanm else {}
    face_uvs = animations["GLOW.ANM"].texcoord_groups[0] if vanm else UVS
    obj = FamilyObject(
        base_object=base_asset.root,
        skeleton_ref=SimpleNamespace(
            path=sklt_path, status="found", source="loose"),
        skeleton=model,
        materials=[MaterialGroup(
            block.texture.name,
            texture_name=block.texture.name,
            kind=block.texture.kind,
            block=block,
            faces=[
                (entry.poly_id, list(face_uvs), entry.shade_val)
                for entry in block.atts
            ],
        )],
        owner_path="root",
    )
    family = AssetFamily(
        base_path=base_path,
        base_asset=base_asset,
        root_object=obj,
        animations=animations,
        textures={f"{fx_name}.ILBM": _image(f"{fx_name}.ILBM")},
    )
    return family, obj


class FxClipboardV3Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_direct_fx1_fx2_copy_and_append_remain_typed(self):
        for fx_name in ("FX1", "FX2"):
            family, obj = _memory_family(fx_name)
            element = detect_fx_elements(obj, family.animations)[0]
            self.assertEqual(
                (element.fx_name, element.source_kind,
                 element.shared_state),
                (fx_name, "direct", "exclusive"))
            clipboard = build_fx_element_clipboard(
                obj, element, family.animations)
            self.assertIsInstance(clipboard, FxElementClipboard)
            self.assertEqual(clipboard.fx_name, fx_name)
            self.assertEqual(clipboard.source_kind, "direct")
            self.assertEqual(clipboard.polygons[0].olpl, tuple(UVS))
            source_points = clipboard.points
            obj.skeleton.points[0] = (99.0, 99.0, 99.0)
            self.assertEqual(clipboard.points, source_points)
            obj.skeleton.points[0] = source_points[0]

            result = append_fx_element_clipboard(
                obj, clipboard, (3.0, 0.0, 0.0), family.animations)
            self.assertEqual(result.polygon_indices, (1,))
            self.assertEqual(obj.base_object.ades[0].texture.name,
                             f"{fx_name}.ILBM")
            self.assertEqual(obj.base_object.ades[0].olpl[-1], UVS)
            detected = detect_fx_elements(obj, family.animations)
            self.assertEqual(
                [(item.fx_name, item.source_kind) for item in detected],
                [(fx_name, "direct"), (fx_name, "direct")])

    def test_vanm_snapshot_append_and_changed_timeline_refusal_are_atomic(self):
        family, obj = _memory_family("FX2", vanm=True)
        element = detect_fx_elements(obj, family.animations)[0]
        self.assertEqual(
            (element.fx_name, element.source_kind, element.uv_source),
            ("FX2", "VANM", "VANM"))
        clipboard = build_fx_element_clipboard(
            obj, element, family.animations)
        self.assertEqual(clipboard.polygons[0].olpl, None)
        self.assertEqual(clipboard.animation.bitmap_names,
                         ("FX2.ILBM", "FX2.ILBM"))
        self.assertEqual(
            clipboard.animation.texcoord_groups[0], tuple(UVS))
        self.assertEqual(
            clipboard.animation.frames,
            ((128, 0, 0), (256, 1, 1)))

        result = append_fx_element_clipboard(
            obj, clipboard, (4.0, 0.0, 0.0), family.animations)
        self.assertEqual(result.polygon_indices, (1,))
        block = obj.base_object.ades[0]
        self.assertEqual((len(block.atts), block.olpl), (2, []))
        self.assertEqual(
            (block.texture.kind, block.texture.name, block.texture.anim_type),
            ("bmpanim", "GLOW.ANM", 1))
        self.assertTrue(all(
            item.source_kind == "VANM"
            for item in detect_fx_elements(obj, family.animations)))

        before = (
            list(obj.skeleton.points),
            [list(poly) for poly in obj.skeleton.polygons],
            list(block.atts),
        )
        family.animations["GLOW.ANM"].frames[0].frame_time = 999
        with self.assertRaises(GeometryClipboardError):
            append_fx_element_clipboard(
                obj, clipboard, (8.0, 0.0, 0.0), family.animations)
        self.assertEqual(obj.skeleton.points, before[0])
        self.assertEqual(obj.skeleton.polygons, before[1])
        self.assertEqual(block.atts, before[2])

    def test_bmpanim_named_fx_is_still_classified_as_vanm(self):
        family, obj = _memory_family("FX2", vanm=True)
        block = obj.base_object.ades[0]
        animation = family.animations.pop("GLOW.ANM")
        block.texture.name = "FX2.ANM"
        family.animations["FX2.ANM"] = animation
        element = detect_fx_elements(obj, family.animations)[0]
        self.assertEqual(
            (element.fx_name, element.source_kind, element.uv_source),
            ("FX2", "VANM", "VANM"))
        clipboard = build_fx_element_clipboard(
            obj, element, family.animations)
        self.assertEqual(clipboard.source_kind, "VANM")
        self.assertIsNotNone(clipboard.animation)

    def test_vanm_with_paired_olpl_uses_vanm_uvs_but_preserves_olpl(self):
        family, obj = _memory_family("FX1", vanm=True)
        block = obj.base_object.ades[0]
        paired_olpl = [(7, 9), (17, 19), (27, 29), (37, 39)]
        block.olpl[:] = [list(paired_olpl)]
        block.olpl_chunk_offset = 2
        element = detect_fx_elements(obj, family.animations)[0]
        self.assertEqual(element.uv_source, "VANM")
        clipboard = build_fx_element_clipboard(
            obj, element, family.animations)
        self.assertEqual(clipboard.uv_source, "VANM")
        self.assertEqual(
            clipboard.polygons[0].olpl, tuple(paired_olpl))
        append_fx_element_clipboard(
            obj, clipboard, (3.0, 0.0, 0.0), family.animations)
        self.assertEqual(
            block.olpl, [paired_olpl, paired_olpl])
        self.assertEqual(
            [item.uv_source
             for item in detect_fx_elements(obj, family.animations)],
            ["VANM", "VANM"])

        window = AssemblyWindow()
        try:
            window._family = family
            window._owner_to_obj = {"root": obj}
            window._selected_owner = "root"
            window._workbench_obj = obj
            window.viewport.load_family(family, primary_owner="root")
            window.viewport.set_selected_owner("root")
            window._rebuild_workbench(family, "root")
            window._update_uv_editor(0)
            self.assertFalse(window.uv_editor._editable)
            self.assertEqual(window.uv_editor.uvs(), UVS)
            self.assertNotEqual(
                window.uv_editor.uvs(), paired_olpl)
        finally:
            window.close()

    def test_bilateral_is_copied_added_and_deleted_atomically(self):
        family, obj = _memory_family("FX1", bilateral=True)
        obj.skeleton.points.extend([
            (3.0, 0.0, 0.0),
            (4.0, 0.0, 0.0),
            (3.0, 1.0, 0.0),
        ])
        obj.skeleton.polygons.append([4, 5, 6])
        obj.skeleton.parsed_polygon_count = 3
        static = AmeshBlock(
            class_id="amesh.class",
            texture=TextureRef(
                class_id="ilbm.class", kind="ilbm", name="STONE.ILBM"),
            atts=[AttsEntry(2, 1, 2, 3, 4)],
            olpl=[[(0, 0), (255, 0), (0, 255)]],
            atts_chunk_offset=3,
            olpl_chunk_offset=4,
        )
        obj.base_object.ades.append(static)
        elements = detect_fx_elements(obj, family.animations)
        bilateral = next(item for item in elements if item.bilateral)
        self.assertEqual(bilateral.poly_ids, (0, 1))
        clipboard = build_fx_element_clipboard(
            obj, bilateral, family.animations)
        self.assertTrue(clipboard.bilateral)
        self.assertEqual(len(clipboard.polygons), 2)
        self.assertEqual(
            clipboard.polygons[1].local_indices,
            tuple(reversed(clipboard.polygons[0].local_indices)))

        new_clipboard = build_new_fx_clipboard(
            obj, bilateral, family.animations, 2.0, True)
        self.assertEqual(len(new_clipboard.points), 4)
        self.assertEqual(len(new_clipboard.polygons), 2)
        self.assertEqual(
            new_clipboard.polygons[1].local_indices, (3, 2, 1, 0))

        mapping = MappingIndex(obj)
        plan = plan_delete_geometry(
            obj, set(bilateral.poly_ids), mapping)
        apply_delete_geometry(
            obj.skeleton, obj.base_object.ades, plan)
        self.assertEqual(len(obj.skeleton.polygons), 1)
        self.assertEqual(obj.base_object.ades[0].atts, [])
        self.assertEqual(obj.base_object.ades[0].olpl, [])
        self.assertEqual(
            obj.base_object.ades[0].texture.name, "FX1.ILBM")
        self.assertEqual(
            static.texture.name, "STONE.ILBM")

    def test_shared_and_invalid_fx_are_read_only(self):
        family, obj = _memory_family("FX1", shared=True)
        element = detect_fx_elements(obj, family.animations)[0]
        self.assertEqual(element.shared_state, "shared")
        with self.assertRaises(GeometryClipboardError):
            build_fx_element_clipboard(obj, element, family.animations)

    def test_mixed_static_and_fx_ui_selection_is_refused(self):
        family, obj = _memory_family("FX1")
        obj.skeleton.points.extend([
            (3.0, 0.0, 0.0),
            (4.0, 0.0, 0.0),
            (3.0, 1.0, 0.0),
        ])
        obj.skeleton.polygons.append([4, 5, 6])
        obj.skeleton.parsed_polygon_count = 2
        static = AmeshBlock(
            class_id="amesh.class",
            texture=TextureRef(
                class_id="ilbm.class", kind="ilbm", name="STONE.ILBM"),
            atts=[AttsEntry(1, 1, 2, 3, 4)],
            olpl=[[(0, 0), (255, 0), (0, 255)]],
            atts_chunk_offset=3,
            olpl_chunk_offset=4,
        )
        obj.base_object.ades.append(static)
        window = AssemblyWindow()
        try:
            window._family = family
            window._owner_to_obj = {"root": obj}
            window._selected_owner = "root"
            window._workbench_obj = obj
            window._mapping_index = MappingIndex(obj)
            window.viewport.load_family(family, primary_owner="root")
            window.viewport.set_selected_owner("root")
            window._refresh_fx_elements()
            window.viewport.enter_edit_mode("root")
            window.viewport.edit_session.selection = set(range(7))
            window._on_edit_selection_details_changed(
                window.viewport.edit_session.selection, "vertex")
            self.assertIn(
                "cannot be combined", window._copy_geometry_reason())
            self.assertIn(
                "cannot be combined", window._delete_geometry_reason())
            self.assertFalse(window.copy_geometry_action.isEnabled())
            self.assertFalse(window.delete_geometry_action.isEnabled())
        finally:
            window.close()

        family, obj = _memory_family("FX2", invalid=True)
        element = detect_fx_elements(obj, family.animations)[0]
        self.assertEqual(element.shared_state, "invalid")
        with self.assertRaises(GeometryClipboardError):
            build_fx_element_clipboard(obj, element, family.animations)

    def test_shared_fx_vertices_are_protected_from_normal_edit_mode(self):
        family, obj = _memory_family("FX1", shared=True)
        window = AssemblyWindow()
        try:
            window._family = family
            window._owner_to_obj = {"root": obj}
            window._selected_owner = "root"
            window._workbench_obj = obj
            window._mapping_index = MappingIndex(obj)
            window.viewport.load_family(family, primary_owner="root")
            window.viewport.set_selected_owner("root")
            window.viewport.enter_edit_mode("root")
            window._refresh_fx_elements()
            window._sync_edit_action_states()
            self.assertFalse(window.add_fx_action.isEnabled())
            window.viewport.select_all_edit_vertices()
            self.assertEqual(
                window.viewport.edit_session.selection, {4, 5})
            self.assertTrue(
                set(range(4)).issubset(
                    window.viewport._edit_read_only_vertices))
            window.viewport.edit_session.selection = {0, 4}
            window.viewport._begin_modal("grab")
            self.assertEqual(
                window.viewport.edit_session.selection, {4})
            window.viewport._cancel_modal()
            window._on_polygon_picked(0)
            self.assertIsNone(window._selected_polygon_vertices())
        finally:
            window.close()

    def test_new_fx_supports_direct_vanm_single_and_bilateral(self):
        for vanm in (False, True):
            family, obj = _memory_family("FX1", vanm=vanm)
            template = detect_fx_elements(obj, family.animations)[0]
            for bilateral in (False, True):
                clipboard = build_new_fx_clipboard(
                    obj, template, family.animations, 3.5, bilateral)
                validate_fx_element_clipboard(
                    obj, clipboard, family.animations)
                self.assertEqual(len(clipboard.points), 4)
                self.assertEqual(
                    len(clipboard.polygons), 2 if bilateral else 1)
                self.assertEqual(
                    clipboard.source_kind, "VANM" if vanm else "direct")

    def test_atts_only_vanm_delete_preserves_material_and_no_olpl_is_invented(self):
        family, obj = _memory_family("FX1", vanm=True, shared=True)
        fx = detect_fx_elements(obj, family.animations)[0]
        self.assertEqual(fx.shared_state, "shared")
        # The shared-state policy rejects UI deletion.  The structural planner
        # itself must nevertheless preserve demonstrated ATTS-only semantics.
        plan = plan_delete_geometry(
            obj, {0}, MappingIndex(obj))
        apply_delete_geometry(
            obj.skeleton, obj.base_object.ades, plan)
        block = obj.base_object.ades[0]
        self.assertEqual(block.atts, [])
        self.assertEqual(block.olpl, [])
        self.assertEqual(block.texture.kind, "bmpanim")
        self.assertEqual(block.texture.name, "GLOW.ANM")

    def test_ui_dispatch_copy_paste_undo_redo_and_vanm_uv_read_only(self):
        with tempfile.TemporaryDirectory() as temp:
            family, obj = _writable_family(
                Path(temp), "FX2", vanm=True)
            window = AssemblyWindow()
            try:
                window._family = family
                window._owner_to_obj = {"root": obj}
                window._selected_owner = "root"
                window._workbench_obj = obj
                window.viewport.load_family(
                    family, primary_owner="root")
                window.viewport.set_selected_owner("root")
                window._rebuild_workbench(family, "root")
                window._refresh_fx_elements()
                window.viewport.enter_edit_mode("root")
                window.viewport.configure_edit_interaction(
                    selected_only=False, pick_polygons=True)
                window._on_polygon_picked(0)
                self.assertTrue(window.copy_geometry_action.isEnabled())
                self.assertTrue(window.add_fx_action.isEnabled())
                self.assertIn(
                    "FX2 VANM Element",
                    window.copy_geometry_action.toolTip())
                self.assertIn(
                    "Delete the complete FX2 VANM Element",
                    window.delete_geometry_action.toolTip())

                window._update_uv_editor(0)
                self.assertFalse(window.uv_editor._editable)
                self.assertIn(
                    "UV source: VANM", window.uv_editor.toolTip())
                self.assertIn("frame 1/2", window.uv_editor.toolTip())
                self.assertIn("FX2.ILBM", window.uv_editor.toolTip())
                self.assertEqual(window.uv_editor.uvs(), UVS)
                self.assertFalse(window.uv_editor._image.isNull())
                window.viewport.step_animation()
                self.assertEqual(
                    window.uv_editor.uvs(), list(reversed(UVS)))
                self.assertIn("frame 2/2", window.uv_editor.toolTip())
                animation_uvs = [
                    list(group)
                    for group in family.animations[
                        "GLOW.ANM"].texcoord_groups]
                window.uv_editor.select_all()
                window.uv_editor.nudge_selected(5, 5)
                self.assertEqual(
                    family.animations["GLOW.ANM"].texcoord_groups,
                    animation_uvs)

                window._copy_geometry()
                self.assertIsInstance(
                    window._geometry_clipboard, FxElementClipboard)
                self.assertTrue(window.viewport.paste_preview_active)
                self.assertIn(
                    "Paste FX Preview",
                    window.paste_geometry_action.toolTip())
                self.assertIn(
                    "FX", window.viewport._paste_preview.clipboard.fx_name)
                window._confirm_paste_geometry()
                self.assertFalse(window.viewport.paste_preview_active)
                self.assertEqual(len(obj.skeleton.polygons), 2)
                self.assertEqual(len(obj.base_object.ades[0].atts), 2)
                self.assertEqual(obj.base_object.ades[0].olpl, [])
                self.assertEqual(
                    len(window._selected_fx_element().poly_ids), 1)
                window._undo_edit()
                self.assertEqual(len(obj.skeleton.polygons), 1)
                window._redo_edit()
                self.assertEqual(len(obj.skeleton.polygons), 2)

                template = next(
                    element for element in window._fx_elements
                    if element.editable)
                expected_clone = build_fx_element_clipboard(
                    obj, template, family.animations)
                with patch(
                        "assembly_window.AddFxElementDialog.exec",
                        return_value=QDialog.DialogCode.Accepted), patch(
                        "assembly_window.AddFxElementDialog.selected_template",
                        return_value=template):
                    window._add_fx_element()
                self.assertTrue(window.viewport.paste_preview_active)
                self.assertEqual(window._geometry_clipboard, expected_clone)
                self.assertEqual(
                    window._geometry_clipboard.source_poly_ids,
                    template.poly_ids)
                window._confirm_paste_geometry()
                self.assertEqual(len(obj.skeleton.polygons), 3)
                self.assertEqual(len(obj.base_object.ades[0].atts), 3)
                self.assertEqual(obj.base_object.ades[0].olpl, [])
                window._undo_edit()
                self.assertEqual(len(obj.skeleton.polygons), 2)
            finally:
                window.close()

    def test_direct_fx_olpl_uv_nudge_undo_and_redo(self):
        with tempfile.TemporaryDirectory() as temp:
            family, obj = _writable_family(Path(temp), "FX1")
            window = AssemblyWindow()
            try:
                window._family = family
                window._owner_to_obj = {"root": obj}
                window._selected_owner = "root"
                window._workbench_obj = obj
                window.viewport.load_family(
                    family, primary_owner="root")
                window.viewport.set_selected_owner("root")
                window._rebuild_workbench(family, "root")
                window._refresh_fx_elements()
                window.viewport.enter_edit_mode("root")
                window._on_polygon_picked(0)
                window._update_uv_editor(0)
                self.assertTrue(window.uv_editor._editable)
                window.uv_editor.select_point(1)
                window.uv_editor.nudge_selected(-5, 0)
                edited = list(obj.base_object.ades[0].olpl[0])
                self.assertNotEqual(edited, UVS)
                self.assertEqual(len(window._edit_undo_stack), 1)
                window._undo_edit()
                self.assertEqual(
                    obj.base_object.ades[0].olpl[0], UVS)
                window._redo_edit()
                self.assertEqual(
                    obj.base_object.ades[0].olpl[0], edited)
            finally:
                window.close()

    def test_add_uv_vertex_updates_poo2_pol2_olpl_save_and_history(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            family, obj = _writable_family(root, "BODY")
            original_base = family.base_path.read_bytes()
            window = AssemblyWindow()
            try:
                window._family = family
                window._owner_to_obj = {"root": obj}
                window._selected_owner = "root"
                window._workbench_obj = obj
                window.viewport.load_family(
                    family, primary_owner="root")
                window.viewport.set_selected_owner("root")
                window._rebuild_workbench(family, "root")
                window._refresh_fx_elements()
                window._on_polygon_picked(0)
                window.uv_editor.select_point(0)

                self.assertEqual(window.uv_editor.toolTip(), "")
                self.assertFalse(hasattr(
                    window.uv_editor, "align_selected_horizontal"))
                self.assertFalse(hasattr(
                    window, "uv_align_horizontal_button"))
                self.assertTrue(window.uv_add_vertex_button.isEnabled())

                window._add_uv_vertex()
                self.assertEqual(len(obj.skeleton.points), 5)
                self.assertEqual(
                    obj.skeleton.points[4], (0.0, -1.0, 0.0))
                self.assertEqual(
                    obj.skeleton.polygons[0], [0, 4, 1, 2, 3])
                self.assertEqual(
                    obj.base_object.ades[0].olpl[0],
                    [(0, 0), (128, 0), (255, 0),
                     (255, 255), (0, 255)])
                self.assertEqual(
                    window.uv_editor.selected_points(), {1})
                self.assertEqual(len(window._edit_undo_stack), 1)

                window._undo_edit()
                self.assertEqual(len(obj.skeleton.points), 4)
                self.assertEqual(obj.skeleton.polygons[0], [0, 1, 2, 3])
                self.assertEqual(obj.base_object.ades[0].olpl[0], UVS)
                window._redo_edit()
                self.assertEqual(len(obj.skeleton.points), 5)
                self.assertEqual(
                    obj.skeleton.polygons[0], [0, 4, 1, 2, 3])

                target_sklt = root / "export" / "BODY.SKLT"
                target_base = root / "export" / "BODY.BASE"
                self.assertTrue(window._write_model_files(
                    "root", family, obj, target_sklt, target_base,
                    ask_replace=False))
                saved_model = parse_sklt_file(target_sklt)
                saved_base = parse_base_bytes(
                    target_base.read_bytes()).root
                self.assertEqual(
                    saved_model.polygons[0], [0, 4, 1, 2, 3])
                self.assertEqual(
                    saved_base.ades[0].olpl[0],
                    [(0, 0), (128, 0), (255, 0),
                     (255, 255), (0, 255)])
                self.assertEqual(family.base_path.read_bytes(), original_base)
            finally:
                window.close()

    def test_rotate_menu_live_preview_and_frame_command_removal(self):
        family, obj = _memory_family("BODY")
        window = AssemblyWindow()
        try:
            window._family = family
            window._owner_to_obj = {"root": obj}
            window._selected_owner = "root"
            window._workbench_obj = obj
            window.viewport.load_family(family, primary_owner="root")
            window.viewport.set_selected_owner("root")
            window._rebuild_workbench(family, "root")
            window.viewport.enter_edit_mode_with_vertices(
                "root", range(4), pick_polygons=True)

            menu = window._create_viewport_context_menu()
            labels = [action.text() for action in menu.actions()]
            self.assertIn("Scale...", labels)
            self.assertIn("Rotate...", labels)
            self.assertNotIn("Frame Selected Model", labels)
            self.assertEqual(window.edit_scale_action.text(), "Scale...")
            self.assertEqual(window.edit_rotate_action.text(), "Rotate...")
            self.assertFalse(hasattr(window, "edit_frame_action"))

            before = list(obj.skeleton.points)
            window._rotate_selected_geometry()
            dialog = window._live_rotate_dialog
            self.assertIsNotNone(dialog)
            dialog.axis_combo.setCurrentText("Z")
            dialog.angle_spin.setValue(90.0)
            QApplication.processEvents()
            dialog.accept()
            QApplication.processEvents()
            self.assertIsNone(window._live_rotate_dialog)
            self.assertAlmostEqual(obj.skeleton.points[0][0], 1.0)
            self.assertAlmostEqual(obj.skeleton.points[0][1], -1.0)
            self.assertEqual(len(window._edit_undo_stack), 1)
            window._undo_edit()
            self.assertEqual(obj.skeleton.points, before)
        finally:
            window.close()

    def test_ui_cut_delete_and_add_dispatch_complete_fx_elements(self):
        family, obj = _memory_family("FX1")
        obj.skeleton.points.extend([
            (3.0, 0.0, 0.0),
            (4.0, 0.0, 0.0),
            (3.0, 1.0, 0.0),
        ])
        obj.skeleton.polygons.append([4, 5, 6])
        obj.skeleton.parsed_polygon_count = 2
        static = AmeshBlock(
            class_id="amesh.class",
            texture=TextureRef(
                class_id="ilbm.class", kind="ilbm", name="STONE.ILBM"),
            atts=[AttsEntry(1, 1, 2, 3, 4)],
            olpl=[[(0, 0), (255, 0), (0, 255)]],
            atts_chunk_offset=3,
            olpl_chunk_offset=4,
        )
        obj.base_object.ades.append(static)
        obj.materials.append(MaterialGroup(
            "STONE.ILBM", texture_name="STONE.ILBM", kind="ilbm",
            block=static, faces=[(1, list(static.olpl[0]), 2)]))

        def prepare():
            window = AssemblyWindow()
            window._family = family
            window._owner_to_obj = {"root": obj}
            window._selected_owner = "root"
            window._workbench_obj = obj
            window._mapping_index = MappingIndex(obj)
            window.viewport.load_family(family, primary_owner="root")
            window.viewport.set_selected_owner("root")
            window._refresh_fx_elements()
            window.viewport.enter_edit_mode("root")
            window.viewport.configure_edit_interaction(
                selected_only=False, pick_polygons=True)
            window._on_polygon_picked(0)
            return window

        window = prepare()
        try:
            selected = window._selected_fx_element()
            expected_clone = build_fx_element_clipboard(
                obj, selected, family.animations)
            with patch(
                    "assembly_window.AddFxElementDialog.exec",
                    return_value=QDialog.DialogCode.Accepted), patch(
                    "assembly_window.AddFxElementDialog.selected_template",
                    return_value=selected):
                window._add_fx_element()
            self.assertTrue(window.viewport.paste_preview_active)
            self.assertIsInstance(
                window._geometry_clipboard, FxElementClipboard)
            self.assertEqual(window._geometry_clipboard, expected_clone)
            self.assertEqual(
                window._geometry_clipboard.source_poly_ids,
                selected.poly_ids)
            window.viewport.cancel_paste_preview()

            window._on_polygon_picked(0)
            window._cut_geometry()
            self.assertIsInstance(
                window._geometry_clipboard, FxElementClipboard)
            self.assertEqual(len(obj.skeleton.polygons), 1)
            self.assertEqual(static.texture.name, "STONE.ILBM")
            self.assertEqual(
                obj.base_object.ades[0].texture.name, "FX1.ILBM")
            self.assertEqual(len(window._edit_undo_stack), 1)
            window._undo_edit()
            self.assertEqual(len(obj.skeleton.polygons), 2)
        finally:
            window.close()

        window = prepare()
        try:
            with patch.object(
                    QMessageBox, "question",
                    return_value=QMessageBox.StandardButton.Yes):
                window._delete_geometry()
            self.assertEqual(len(obj.skeleton.polygons), 1)
            self.assertEqual(static.texture.name, "STONE.ILBM")
            self.assertEqual(
                obj.base_object.ades[0].texture.name, "FX1.ILBM")
            window._undo_edit()
            self.assertEqual(len(obj.skeleton.polygons), 2)
        finally:
            window.close()

    def test_direct_and_vanm_structural_save_reload_preserve_fx_type(self):
        for vanm in (False, True):
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                family, obj = _writable_family(
                    root, "FX1", vanm=vanm)
                element = detect_fx_elements(
                    obj, family.animations)[0]
                clipboard = build_fx_element_clipboard(
                    obj, element, family.animations)
                append_fx_element_clipboard(
                    obj, clipboard, (3.0, 0.0, 0.0),
                    family.animations)
                window = AssemblyWindow()
                try:
                    window._family = family
                    window._owner_to_obj = {"root": obj}
                    window._selected_owner = "root"
                    window._workbench_obj = obj
                    target_sklt = root / "out" / "FXTEST.SKLT"
                    target_base = root / "out" / "FXTEST.BASE"
                    self.assertTrue(window._write_model_files(
                        "root", family, obj, target_sklt, target_base,
                        ask_replace=False))
                    saved_model = parse_sklt_file(target_sklt)
                    saved_base = parse_base_bytes(
                        target_base.read_bytes()).root
                    saved_block = saved_base.ades[0]
                    self.assertEqual(len(saved_model.polygons), 2)
                    self.assertEqual(len(saved_block.atts), 2)
                    self.assertEqual(
                        saved_block.texture.kind,
                        "bmpanim" if vanm else "ilbm")
                    self.assertEqual(
                        saved_block.texture.name,
                        "GLOW.ANM" if vanm else "FX1.ILBM")
                    self.assertEqual(saved_block.texture.anim_type,
                                     1 if vanm else None)
                    self.assertEqual(
                        len(saved_block.olpl), 0 if vanm else 2)
                    reloaded_obj = FamilyObject(
                        base_object=saved_base,
                        skeleton=saved_model,
                        owner_path="root",
                    )
                    detected = detect_fx_elements(
                        reloaded_obj, family.animations)
                    self.assertEqual(len(detected), 2)
                    self.assertTrue(all(
                        item.source_kind
                        == ("VANM" if vanm else "direct")
                        for item in detected))

                    append_fx_element_clipboard(
                        obj, clipboard, (6.0, 0.0, 0.0),
                        family.animations)
                    self.assertTrue(window._write_model_files(
                        "root", family, obj, target_sklt, target_base,
                        ask_replace=False))
                    self.assertEqual(
                        len(parse_sklt_file(target_sklt).polygons), 3)
                    second_base = parse_base_bytes(
                        target_base.read_bytes()).root.ades[0]
                    self.assertEqual(len(second_base.atts), 3)
                    self.assertEqual(
                        len(second_base.olpl), 0 if vanm else 3)

                    shrink = plan_delete_geometry(
                        obj, {2}, MappingIndex(obj))
                    apply_delete_geometry(
                        obj.skeleton, obj.base_object.ades, shrink)
                    self.assertTrue(window._write_model_files(
                        "root", family, obj, target_sklt, target_base,
                        ask_replace=False))
                    self.assertEqual(
                        len(parse_sklt_file(target_sklt).polygons), 2)
                    shrunk_base = parse_base_bytes(
                        target_base.read_bytes()).root.ades[0]
                    self.assertEqual(len(shrunk_base.atts), 2)
                    self.assertEqual(
                        len(shrunk_base.olpl), 0 if vanm else 2)
                    self.assertEqual(
                        shrunk_base.texture.kind,
                        "bmpanim" if vanm else "ilbm")
                finally:
                    window.close()

    def test_added_bilateral_fx_survives_save_and_reload(self):
        for vanm in (False, True):
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                family, obj = _writable_family(
                    root, "FX2", vanm=vanm)
                template = detect_fx_elements(
                    obj, family.animations)[0]
                added = build_new_fx_clipboard(
                    obj, template, family.animations, 2.5, True)
                append_fx_element_clipboard(
                    obj, added, (4.0, 0.0, 0.0),
                    family.animations)
                window = AssemblyWindow()
                try:
                    window._family = family
                    window._owner_to_obj = {"root": obj}
                    window._selected_owner = "root"
                    window._workbench_obj = obj
                    target_sklt = root / "added" / "FXTEST.SKLT"
                    target_base = root / "added" / "FXTEST.BASE"
                    self.assertTrue(window._write_model_files(
                        "root", family, obj, target_sklt, target_base,
                        ask_replace=False))
                    saved_model = parse_sklt_file(target_sklt)
                    saved_base = parse_base_bytes(
                        target_base.read_bytes()).root
                    reloaded_obj = FamilyObject(
                        base_object=saved_base,
                        skeleton=saved_model,
                        owner_path="root",
                    )
                    elements = detect_fx_elements(
                        reloaded_obj, family.animations)
                    bilateral = next(
                        element for element in elements
                        if element.bilateral)
                    self.assertEqual(len(bilateral.poly_ids), 2)
                    self.assertEqual(
                        bilateral.source_kind,
                        "VANM" if vanm else "direct")
                    self.assertEqual(
                        saved_base.ades[0].texture.name,
                        "GLOW.ANM" if vanm else "FX2.ILBM")
                finally:
                    window.close()


if __name__ == "__main__":
    unittest.main()
