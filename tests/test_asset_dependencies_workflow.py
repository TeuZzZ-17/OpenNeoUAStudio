import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QLineEdit

from assembly_window import AssemblyWindow
from asset_family import AssetFamily, FamilyObject
from asset_resolver import ResolvedFile
from base_dependency_resolver import AssetDependency
from base_mapping_editor import (
    MappingEditError,
    TextureNameEdit,
    apply_texture_name_edits_to_bytes,
)
from base_parser import AmeshBlock, BaseObject, TextureRef, parse_base_bytes
from tests.test_fx_clipboard_v3 import _fx_base_bytes, _memory_family, _writable_family


def _find_kind(item, kind):
    data = item.data(0, Qt.ItemDataRole.UserRole)
    if data and data[0] == kind:
        return item
    for index in range(item.childCount()):
        found = _find_kind(item.child(index), kind)
        if found is not None:
            return found
    return None


class AssetDependenciesWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_unified_tree_carries_status_provenance_and_candidates(self):
        family, obj = _memory_family("FX1")
        family.base_path = Path("C:/UA/Loose/FXTEST.BASE")
        loose_a = Path("C:/UA/Loose/A/FX1.ILBM")
        loose_b = Path("C:/UA/Loose/B/FX1.ILBM")
        family.texture_refs["FX1.ILBM"] = ResolvedFile(
            "FX1.ILBM", status="ambiguous", candidates=[loose_a, loose_b],
            source="loose", resolution_rule="two exact loose candidates",
            embedded_available=True,
            embedded_candidates=["SET.BAS:FX1.ILBM"])
        dep = AssetDependency(
            "texture", "FX1.ILBM", source="ADES block #0 CIBO NAM2",
            owner_node="root", status="ambiguous",
            candidates=[loose_a, loose_b], resolution_source="loose",
            resolution_rule="two exact loose candidates")
        family.dependencies = [dep]

        window = AssemblyWindow()
        try:
            window._family = family
            window._owner_to_obj = {"root": obj}
            window._selected_owner = "root"
            window._fill_asset_tree(family)
            texture = _find_kind(window.asset_tree.topLevelItem(0), "texture")
            self.assertIsNotNone(texture)
            self.assertEqual(texture.text(1), "ambiguous")
            self.assertIn("loose:", texture.text(2))
            candidate_rows = [
                texture.child(index)
                for index in range(texture.childCount())
                if (texture.child(index).data(0, Qt.ItemDataRole.UserRole)
                    or (None,))[0] == "dependency_candidate"]
            self.assertEqual(len(candidate_rows), 3)
            self.assertTrue(any(
                row.text(0) == "SET.BAS fallback" for row in candidate_rows))

            colors = {}
            for status in ("missing", "ambiguous", "failed_load"):
                dep.status = status
                dep.error = "decode failed" if status == "failed_load" else None
                window._fill_asset_tree(family)
                item = _find_kind(
                    window.asset_tree.topLevelItem(0), "texture")
                colors[status] = item.foreground(1).color().name()
            self.assertEqual(len(set(colors.values())), 3)
        finally:
            window.close()

    def test_missing_asset_can_select_candidate_or_browse_for_a_file(self):
        family, obj = _memory_family("FX1")
        family.base_path = Path("C:/UA/Loose/FXTEST.BASE")
        candidate = Path("C:/UA/Loose/Textures/FX1.ILBM")
        family.texture_refs["FX1.ILBM"] = ResolvedFile(
            "FX1.ILBM", status="missing", candidates=[candidate])
        family.dependencies = [AssetDependency(
            "texture", "FX1.ILBM", source="ADES block #0 CIBO NAM2",
            owner_node="root", status="missing", candidates=[candidate])]

        window = AssemblyWindow()
        try:
            window._family = family
            window._owner_to_obj = {"root": obj}
            window._selected_owner = "root"
            window._fill_asset_tree(family)
            texture = _find_kind(window.asset_tree.topLevelItem(0), "texture")
            window.asset_tree.setCurrentItem(texture)
            with patch(
                    "assembly_window.QInputDialog.getItem",
                    return_value=(f"Loose: {candidate}", True)), patch.object(
                        window, "_apply_dependency_candidate") as apply_choice:
                window._select_dependency_resource()
            apply_choice.assert_called_once_with("FX1.ILBM", str(candidate))

            family.texture_refs["FX1.ILBM"].candidates = []
            family.dependencies[0].candidates = []
            with patch(
                    "assembly_window.QInputDialog.getItem",
                    return_value=("Browse for a compatible file...", True)), \
                    patch.object(window, "_assign_manual_file") as browse:
                window._select_dependency_resource()
            browse.assert_called_once_with()
        finally:
            window.close()

    def test_exact_vp_base_entry_is_current_and_focused(self):
        family, obj = _memory_family("FX1")
        family.base_path = Path("C:/UA/Set1/SET.BAS")
        window = AssemblyWindow()
        try:
            window._family = family
            window._owner_to_obj = {"root": obj}
            window._selected_owner = "root"
            window._base_entry_names["root"] = "VP_HUBI2.BASE"
            window._fill_asset_tree(family)
            window._focus_assets_for_owner("root", switch_tabs=False)
            current = window.asset_tree.currentItem()
            self.assertIn("VP_HUBI2.BASE", current.text(0))
            self.assertEqual(
                current.data(0, Qt.ItemDataRole.UserRole)[0], "base")
        finally:
            window.close()

    def test_switching_archive_base_discards_the_previous_root_context(self):
        family, previous = _memory_family("FX1")
        family.base_path = Path("C:/UA/Set1/SET.BAS")
        current = FamilyObject(
            base_object=BaseObject(name="VP_NEW.BASE"),
            owner_path="root/kid[0]")
        previous.kids = [current]
        window = AssemblyWindow()
        try:
            window._setbas = SimpleNamespace(path=family.base_path)
            window._family = family
            window._owner_to_obj = {
                "root": previous, "root/kid[0]": current}
            window._selected_owner = "root"
            window._base_entry_names["root"] = "VP_OLD.BASE"
            with patch.object(
                    window, "_resolve_setbas_base",
                    return_value=(family, current, 0x200)), patch.object(
                    window, "_apply_selected_children_scope"):
                window._activate_setbas_base("VP_NEW.BASE")
            self.assertEqual(
                window._base_entry_names, {"root/kid[0]": "VP_NEW.BASE"})
            self.assertEqual(
                window.asset_tree.topLevelItem(0).text(0),
                "Root BASE: VP_NEW.BASE")
        finally:
            window.close()

    def test_base_dependency_specs_save_revert_and_writer_limits(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            family, obj = _writable_family(root, "FX1")
            window = AssemblyWindow()
            try:
                window._family = family
                window._owner_to_obj = {"root": obj}
                window._selected_owner = "root"
                specs = window._base_dependency_edit_specs(obj)
                texture = next(spec for spec in specs
                               if spec["kind"] == "Texture")
                skeleton = next(spec for spec in specs
                                if spec["kind"] == "Skeleton")
                self.assertTrue(texture["editable"])
                self.assertFalse(skeleton["editable"])

                with patch.object(window, "open_base"), patch(
                        "assembly_window.QMessageBox.critical"):
                    self.assertTrue(window._save_base_dependency_edits(
                        "root", [TextureNameEdit(
                            "root", 0, "A.ILBM", "texture")]))
                parsed = parse_base_bytes(family.base_path.read_bytes()).root
                self.assertEqual(parsed.ades[0].texture.name, "A.ILBM")
                self.assertTrue(any(root.glob("FXTEST.BASE.bak*")))

                window._open_base_dependency_dialog("root")
                dialog = window._base_dependency_dialog
                editor = dialog.findChildren(QLineEdit)[0]
                original = editor.text()
                editor.setText("B.ILBM")
                revert = next(
                    button for button in dialog.findChildren(
                        QDialogButtonBox)[0].buttons()
                    if button.text() == "Revert")
                revert.click()
                self.assertEqual(editor.text(), original)
                dialog.close()

                original_bytes = _fx_base_bytes("FX1")
                with self.assertRaises(MappingEditError):
                    apply_texture_name_edits_to_bytes(
                        original_bytes,
                        [TextureNameEdit(
                            "root", 0, "TOO_LONG.ILBM", "texture")])
            finally:
                window.close()

    def test_archive_editable_copy_never_modifies_setbas(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive = root / "SET.BAS"
            archive.write_bytes(_fx_base_bytes("FX1"))
            base_asset = parse_base_bytes(archive.read_bytes(), str(archive))
            obj = FamilyObject(base_object=base_asset.root, owner_path="root")
            family = AssetFamily(
                base_path=archive, base_asset=base_asset, root_object=obj)
            target = root / "Loose" / "VP_TEST.BASE"
            before = hashlib.sha256(archive.read_bytes()).digest()

            window = AssemblyWindow()
            try:
                window._setbas = SimpleNamespace(path=archive)
                window._family = family
                window._owner_to_obj = {"root": obj}
                result = window._create_editable_base_copy("root", target)
                self.assertEqual(result, target)
                self.assertIsNotNone(parse_base_bytes(target.read_bytes()).root)
                self.assertEqual(
                    hashlib.sha256(archive.read_bytes()).digest(), before)
            finally:
                window.close()

    def test_archive_entry_is_editable_in_memory_but_stays_read_only(self):
        window = AssemblyWindow()
        try:
            archive = SimpleNamespace(path=Path("C:/UA/Set1/SET.BAS"))
            obj = SimpleNamespace(
                skeleton=object(),
                skeleton_ref=SimpleNamespace(
                    path=None, status="setbas", source="SET.BAS"))
            window._setbas = archive
            window._selected_owner = "root"
            window._owner_to_obj = {"root": obj}

            window._family = SimpleNamespace(base_path=archive.path)
            self.assertTrue(window._selected_model_is_archive_read_only())
            self.assertTrue(window._has_editable_model())
            window._snapshot_mode_active = False
            window._sync_edit_action_states()
            editor_index = window._right_tabs.indexOf(window._editor_tabs)
            self.assertTrue(window._right_tabs.isTabEnabled(editor_index))
            self.assertTrue(window.edit_toggle_action.isEnabled())
            self.assertTrue(window.edit_menu.isEnabled())
            self.assertTrue(window.mapping_repair_action.isEnabled())
            self.assertIn(
                "never overwritten",
                window._right_tabs.tabToolTip(editor_index))

            window._family = SimpleNamespace(
                base_path=Path("C:/UA/Set1/Loose/VP_TEST.BASE"))
            self.assertFalse(window._selected_model_is_archive_read_only())
            self.assertTrue(window._has_editable_model())
        finally:
            window.close()

    def test_active_model_texture_names_include_all_subtree_textures(self):
        root_obj = FamilyObject(
            base_object=BaseObject(
                name="ROOT.BASE",
                ades=[AmeshBlock(texture=TextureRef(
                    class_id="ilbm.class", kind="ilbm",
                    name="ROOT.ILBM"))]),
            owner_path="root")
        child_obj = FamilyObject(
            base_object=BaseObject(
                name="CHILD.BASE",
                ades=[AmeshBlock(texture=TextureRef(
                    class_id="ilbm.class", kind="ilbm",
                    name="CHILD.ILBM"))]),
            owner_path="root/kid[0]")
        root_obj.kids = [child_obj]
        family = AssetFamily(root_object=root_obj)

        window = AssemblyWindow()
        try:
            window._family = family
            window._owner_to_obj = {
                "root": root_obj, "root/kid[0]": child_obj}
            window._selected_owner = "root"
            self.assertEqual(
                window._active_model_texture_names(),
                {"root.ilbm", "child.ilbm"})

            window._selected_owner = "root/kid[0]"
            self.assertEqual(
                window._active_model_texture_names(), {"child.ilbm"})
        finally:
            window.close()


    def test_asset_dependencies_are_always_scoped_to_selected_model(self):
        root_obj = FamilyObject(
            base_object=BaseObject(name="ROOT.BASE"),
            owner_path="root")
        child_obj = FamilyObject(
            base_object=BaseObject(name="CHILD.BASE"),
            owner_path="root/kid[0]")
        root_obj.kids = [child_obj]
        family = AssetFamily(
            base_path=Path("C:/UA/Set1/SET.BAS"),
            root_object=root_obj)

        window = AssemblyWindow()
        try:
            window._family = family
            window._owner_to_obj = {
                "root": root_obj, "root/kid[0]": child_obj}
            window._selected_owner = "root/kid[0]"
            window._fill_asset_tree(family)
            root = window.asset_tree.topLevelItem(0)
            self.assertIn("CHILD.BASE", root.text(0))
            self.assertNotIn("ROOT.BASE", root.text(0))
        finally:
            window.close()

    def test_dependency_focus_includes_kids_and_nested_components(self):
        root_obj = FamilyObject(
            base_object=BaseObject(
                name="ROOT.BASE",
                ades=[AmeshBlock(texture=TextureRef(
                    class_id="ilbm.class", kind="ilbm",
                    name="ROOT.ILBM"))]),
            owner_path="root")
        child_obj = FamilyObject(
            base_object=BaseObject(
                name="CHILD.BASE",
                ades=[AmeshBlock(texture=TextureRef(
                    class_id="ilbm.class", kind="ilbm",
                    name="CHILD.ILBM"))]),
            owner_path="root/kid[0]")
        root_obj.kids = [child_obj]
        family = AssetFamily(root_object=root_obj)

        window = AssemblyWindow()
        try:
            window._family = family
            window._owner_to_obj = {
                "root": root_obj, "root/kid[0]": child_obj}
            window._selected_owner = "root"
            window._fill_asset_tree(family)
            focused = window._asset_dependency_focus_items("root")
            labels = {item.text(0) for item in focused}
            self.assertTrue(any("CHILD.BASE" in label for label in labels))
            self.assertIn("ROOT.ILBM", labels)
            self.assertIn("CHILD.ILBM", labels)
        finally:
            window.close()

    def test_used_asset_textures_are_stably_prioritized_and_green(self):
        entries = [
            SimpleNamespace(
                name="A.ILBM", reference=None, archive_resource=None,
                status="found"),
            SimpleNamespace(
                name="USED.ILBM", reference=None, archive_resource=None,
                status="found"),
            SimpleNamespace(
                name="B.ILBM", reference=None, archive_resource=None,
                status="found"),
        ]
        family = SimpleNamespace(
            setbas_archive=None, textures={}, texture_tracy_usage={},
            external_palette=None)
        window = AssemblyWindow()
        try:
            window._family = family
            with patch(
                    "assembly_window.build_texture_catalog",
                    return_value=entries), patch.object(
                        window, "_active_model_texture_names",
                        return_value={"used.ilbm"}):
                window._fill_textures(family)
            names = [
                window.texture_list.item(index).data(
                    Qt.ItemDataRole.UserRole)
                for index in range(window.texture_list.count())]
            self.assertEqual(names, ["USED.ILBM", "A.ILBM", "B.ILBM"])
            highlighted = window.texture_list.item(0).background().color()
            self.assertGreater(highlighted.green(), highlighted.red())
            self.assertEqual(
                window.texture_list.item(0).foreground().color().name(),
                "#eeeeee")
            self.assertEqual(
                window.texture_list.horizontalScrollBarPolicy(),
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

            # A model switch must reprioritize the existing list immediately;
            # the catalog is not rebuilt just to update usage focus.
            with patch.object(
                    window, "_active_model_texture_names",
                    return_value={"b.ilbm"}):
                window._refresh_texture_usage_highlight()
            names = [
                window.texture_list.item(index).data(
                    Qt.ItemDataRole.UserRole)
                for index in range(window.texture_list.count())]
            self.assertEqual(names, ["B.ILBM", "USED.ILBM", "A.ILBM"])
            self.assertGreater(
                window.texture_list.item(0).background().color().green(),
                window.texture_list.item(0).background().color().red())
            self.assertEqual(
                window.texture_list.item(1).background().style(),
                Qt.BrushStyle.NoBrush)
            self.assertIn("rgba(74, 112, 84, 180)",
                          window.asset_tree.styleSheet())
        finally:
            window.close()

    def test_asset_dependencies_context_menu_has_no_obsolete_actions(self):
        source = (Path(__file__).resolve().parents[1] /
                  "assembly_window.py").read_text(encoding="utf-8")
        start = source.index("    def _show_asset_context_menu")
        end = source.index("    def _selected_texture_names", start)
        context_source = source[start:end]
        self.assertNotIn('"Select model"', context_source)
        self.assertNotIn('"Assign File..."', context_source)

    def test_asset_textures_suppresses_expected_missing_cmap_noise(self):
        source = (Path(__file__).resolve().parents[1] /
                  "assembly_window.py").read_text(encoding="utf-8")
        self.assertIn('"no cmap palette in file"', source.casefold())
        self.assertIn("ScrollBarAlwaysOff", source)

    def test_shared_component_requires_explicit_base_owner_choice(self):
        texture = TextureRef(
            class_id="ilbm.class", kind="ilbm", name="SHARED.ILBM")
        root_obj = FamilyObject(
            base_object=BaseObject(
                name="ROOT.BASE", ades=[AmeshBlock(texture=texture)]),
            owner_path="root")
        kid_obj = FamilyObject(
            base_object=BaseObject(
                name="KID.BASE", ades=[AmeshBlock(texture=texture)]),
            owner_path="root/kid[0]")
        root_obj.kids = [kid_obj]
        family = AssetFamily(
            base_path=Path("C:/UA/Loose/ROOT.BASE"), root_object=root_obj)
        window = AssemblyWindow()
        try:
            window._family = family
            window._owner_to_obj = {
                "root": root_obj, "root/kid[0]": kid_obj}
            window._selected_owner = "root"
            window._last_component_selection = ("texture", "SHARED.ILBM")
            expected_label = "KID.BASE [root/kid[0]]"
            with patch(
                    "assembly_window.QInputDialog.getItem",
                    return_value=(expected_label, True)) as chooser:
                owner = window._export_owner_for_selection()
            self.assertEqual(owner, "root/kid[0]")
            chooser.assert_called_once()
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
