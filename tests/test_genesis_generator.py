"""Genesis conversion, native serialization, and editor history contracts."""

import copy
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMenu, QTreeWidgetItem
from PySide6.QtCore import Qt
import pytest

from anm_parser import export_anm_bytes, parse_anm_bytes
from asset_family import AssetFamily, FamilyObject
from base_mapping_editor import MappingEditError
from base_parser import BaseObject
from assembly_window import _BAS_KIND_ROLE, AssemblyWindow
from model_editor.genesis_generator import generate_genesis
from model_editor import genesis_reference
from model_editor.window import ModelEditorWindow
from sklt_parser import create_minimal_sklt_model


def model():
    skeleton = create_minimal_sklt_model()
    skeleton.points = [(0., 0., 0.), (2., 0., 0.), (2., 2., 0.),
                       (1., 3., 0.), (0., 2., 0.)]
    skeleton.polygons = [[0, 1, 4], [0, 1, 2, 4], [0, 1, 2, 3, 4]]
    skeleton.parsed_polygon_count = 3
    obj = FamilyObject(BaseObject(), skeleton=skeleton)
    return AssetFamily(root_object=obj), obj


def test_conversion_keeps_geometry_and_uses_reference_timeline():
    family, obj = model()
    geometry = copy.deepcopy((obj.skeleton.points, obj.skeleton.polygons))
    generate_genesis(family, obj)
    assert (obj.skeleton.points, obj.skeleton.polygons) == geometry
    assert [b.ade_poly_id for b in obj.base_object.ades] == [0, 1, 2]
    assert all(b.polflags == 0x8a and b.texture.anim_type == 0
               for b in obj.base_object.ades)
    for block in obj.base_object.ades:
        animation = family.animations[block.texture.name]
        parsed = parse_anm_bytes(export_anm_bytes(animation))
        assert [f.frame_time for f in parsed.frames] == [80] * 5
        assert all(len(uvs) == len(obj.skeleton.polygons[block.ade_poly_id])
                   for uvs in parsed.texcoord_groups)
        assert parsed.bitmap_names == ["FX2.ILBM"]
    assert family.textures["FX2.ILBM"].has_body


def test_shared_animation_collision_gets_new_name_without_overwrite():
    family, obj = model()
    original = parse_anm_bytes(genesis_reference.TRIANGLE_ANIMATION, "GENES3-1.ANM")
    original.texcoord_groups[0][0] = (0, 0)
    family.animations["GENES3-1.ANM"] = original
    generate_genesis(family, obj)
    assert family.animations["GENES3-1.ANM"] is original
    assert original.texcoord_groups[0][0] == (0, 0)
    assert obj.base_object.ades[0].texture.name != "GENES3-1.ANM"


def test_invalid_polygon_refuses_before_resources_or_materials_change():
    family, obj = model()
    obj.skeleton.polygons[1][0] = 900
    with pytest.raises(MappingEditError, match="invalid vertex"):
        generate_genesis(family, obj)
    assert not family.animations and not family.textures
    assert not obj.base_object.ades


def test_existing_fx_atlas_case_is_kept_and_animation_exports_that_name():
    from ilbm_parser import parse_ilbm_bytes
    family, obj = model()
    atlas = parse_ilbm_bytes(genesis_reference.FX_TEXTURE)
    family.textures["fx2.ilbm"] = atlas
    generate_genesis(family, obj)
    assert list(family.textures) == ["fx2.ilbm"]
    assert family.textures["fx2.ilbm"] is atlas
    for animation in family.animations.values():
        assert parse_anm_bytes(export_anm_bytes(animation)).bitmap_names == ["fx2.ilbm"]


def test_loose_fx_extension_alias_is_resolved_through_family_loader(tmp_path):
    family, obj = model()
    atlas = tmp_path / "FX2.ILB"
    atlas.write_bytes(genesis_reference.FX_TEXTURE)
    family.search_roots = [str(tmp_path)]
    generate_genesis(family, obj)
    assert family.texture_refs["FX2.ILBM"].path == atlas


def test_ngon_animation_collision_stays_within_native_name_capacity():
    family, obj = model()
    original = parse_anm_bytes(genesis_reference.TRIANGLE_ANIMATION)
    family.animations["GNS00002.ANM"] = original
    generate_genesis(family, obj)
    block = obj.base_object.ades[2]
    assert block.texture.name == "G0002001.ANM"
    assert family.animations["GNS00002.ANM"] is original
    assert len(block.texture.name.encode("latin-1")) + 1 <= block.texture.name_capacity


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_button_tracks_model_and_opens_same_wireframe_command(app):
    window = ModelEditorWindow()
    try:
        assert not window.generate_wireframe_button.isEnabled()
        window.viewport._faces.append(object())
        window._sync_generator_actions()
        assert window.generate_wireframe_button.isEnabled()
        with patch("model_editor.wireframe_dialog.WireframeGeneratorDialog") as dialog:
            window.generate_wireframe_button.click()
            dialog.assert_called_once_with(window.viewport, window)
    finally:
        window.close()


def test_context_actions_only_allow_model_resources_and_share_current_commands(app):
    window = ModelEditorWindow()
    try:
        for kind, enabled in (("base", True), ("sklt.class", True),
                              ("ilbm.class", False), ("bmpanim.class", False)):
            item = QTreeWidgetItem(["test"])
            item.setData(0, _BAS_KIND_ROLE, kind)
            menu = QMenu()
            window._add_model_generator_context_actions(menu, item)
            actions = [a for a in menu.actions() if not a.isSeparator()]
            assert [a.text() for a in actions] == ["Generate Wireframe", "Generate Genesis"]
            assert all(a.isEnabled() == enabled for a in actions)
            if enabled:
                with patch.object(window, "_generate_from_setbas_item") as handler:
                    actions[0].trigger()
                    handler.assert_called_once_with(item, genesis=False)
        snapshot_menu = QMenu()
        AssemblyWindow._add_model_generator_context_actions(window, snapshot_menu, item)
        assert not snapshot_menu.actions()
    finally:
        window.close()


def test_viewport_menu_reuses_generator_actions_and_labels_without_ellipsis(app):
    window = ModelEditorWindow()
    try:
        menu = window._create_viewport_context_menu()
        actions = menu.actions()
        assert window.generate_wireframe_action in actions
        assert window.generate_genesis_action in actions
        assert window.generate_wireframe_action.text() == "Generate Wireframe"
        assert window.generate_wireframe_button.text() == "Generate Wireframe"
        assert not window.generate_wireframe_action.isEnabled()
        assert not window.generate_genesis_action.isEnabled()
        window.viewport._faces.append(object())
        menu = window._create_viewport_context_menu()
        assert window.generate_wireframe_action.isEnabled()
        with patch("model_editor.wireframe_dialog.WireframeGeneratorDialog") as dialog:
            window.generate_wireframe_action.trigger()
            dialog.assert_called_once_with(window.viewport, window)
        shared = AssemblyWindow._create_viewport_context_menu(window)
        assert window.generate_wireframe_action not in shared.actions()
        assert window.generate_genesis_action not in shared.actions()
    finally:
        window.close()


def test_failed_or_cancelled_resource_activation_does_not_generate_stale_model(app):
    window = ModelEditorWindow()
    try:
        from types import SimpleNamespace
        window._setbas = SimpleNamespace(resources=[SimpleNamespace(class_id="sklt.class")])
        item = QTreeWidgetItem(["test"])
        item.setData(0, _BAS_KIND_ROLE, "sklt.class")
        item.setData(0, Qt.ItemDataRole.UserRole, 0)
        with patch.object(window, "_confirm_discard_geometry", return_value=False), \
                patch.object(window, "_open_wireframe_generator") as generator:
            window._generate_from_setbas_item(item, genesis=False)
            generator.assert_not_called()
        with patch.object(window, "_confirm_discard_geometry", return_value=True), \
                patch.object(window, "_preview_setbas_skeleton", return_value=False), \
                patch.object(window, "_open_wireframe_generator") as generator:
            window._generate_from_setbas_item(item, genesis=False)
            generator.assert_not_called()
    finally:
        window._setbas = None
        window.close()


def test_real_family_genesis_undo_redo_and_export_roundtrip(app):
    archive = Path(r"C:\Users\proto\Desktop\ProgettoUA\UA Rising\Data\Sets\Set3\Objects\SET.BAS")
    if not archive.exists():
        pytest.skip("Local game reference is unavailable")
    from asset_family import load_asset_family
    family = load_asset_family(archive, setbas=archive)
    obj = next(o for o in family.all_objects()
               if o.base_object.skeleton_name.casefold().endswith("/vp_hubi5.sklt"))
    dead = next(o for o in family.all_objects() if o.owner_path == "root/kid[0]/kid[331]")
    emitter = copy.deepcopy(next(b for b in dead.base_object.ades
                                if b.class_id == "particle.class"))
    obj.base_object.ades.append(emitter)
    window = ModelEditorWindow()
    try:
        window._set_family(family)
        window._select_owner(obj.owner_path)
        window._right_tabs.setCurrentWidget(window._editor_tabs)
        window._editor_tabs.setCurrentWidget(window._model_editor_panel)
        window._sync_tab_edit_mode()
        menu = window._create_viewport_context_menu()
        assert window.generate_genesis_action in menu.actions()
        assert window.generate_genesis_action.isEnabled()
        uv_key = (obj.owner_path, 4, 0)
        original_uvs = copy.deepcopy(obj.base_object.ades[4].olpl[0])
        window._uv_original[uv_key] = original_uvs
        obj.base_object.ades[4].olpl[0][0] = (11, 17)
        before = copy.deepcopy(obj.base_object.ades)
        points = copy.deepcopy(obj.skeleton.points)
        window.generate_genesis_action.trigger()
        assert len(window._edit_undo_stack) == 1
        assert len(obj.base_object.ades) == len(obj.skeleton.polygons) + 1
        assert obj.base_object.ades[-1] == emitter
        assert uv_key not in window._uv_original
        after = copy.deepcopy(obj.base_object.ades)
        window._undo_edit()
        assert obj.base_object.ades == before
        assert window._uv_original[uv_key] == original_uvs
        window._redo_edit()
        assert obj.base_object.ades == after and obj.skeleton.points == points
        assert uv_key not in window._uv_original
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assert window._write_model_files(
                obj.owner_path, family, obj, root / "Skeleton/VP_HUBI5.sklt",
                root / "Genesis.BASE", ask_replace=False)
            reopened = load_asset_family(root / "Genesis.BASE")
            assert reopened.root_object.skeleton.points == points
            assert len(reopened.root_object.base_object.ades) == len(after)
            assert all(len(reopened.animations[block.texture.name].frames) == 5
                       for block in reopened.root_object.base_object.ades
                       if block.class_id == "area.class")
            assert reopened.root_object.base_object.ades[-1].class_id == "particle.class"
            assert reopened.textures["FX2.ILBM"].has_body
    finally:
        window._geom_dirty.clear()
        window.close()
