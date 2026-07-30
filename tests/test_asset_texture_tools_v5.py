import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QTreeWidget, QTreeWidgetItem

from asset_resolver import DirectoryIndex
from asset_tree_filter import filter_tree
from texture_assignment import classify_texture_assignment
from texture_catalog import build_texture_catalog


class AssetTreeFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_metadata_match_keeps_parent_and_restores_expansion_without_signal(self):
        tree = QTreeWidget()
        tree.setColumnCount(1)
        root = QTreeWidgetItem(["Root model"])
        child = QTreeWidgetItem(["Hull"])
        root.addChild(child)
        other = QTreeWidgetItem(["Unrelated"])
        tree.addTopLevelItems([root, other])
        root.setExpanded(False)
        tree.setCurrentItem(child)
        changes = []
        tree.currentItemChanged.connect(lambda *_args: changes.append(True))

        expansion = filter_tree(
            tree, "ilbm.class",
            lambda item: "ILBM.CLASS owner/root"
            if item is child else "",
        )

        self.assertFalse(root.isHidden())
        self.assertFalse(child.isHidden())
        self.assertTrue(other.isHidden())
        self.assertTrue(root.isExpanded())
        self.assertIs(tree.currentItem(), child)
        self.assertEqual(changes, [])

        expansion = filter_tree(tree, "", lambda _item: "", expansion)
        self.assertIsNone(expansion)
        self.assertFalse(root.isExpanded())
        self.assertFalse(other.isHidden())
        self.assertIs(tree.currentItem(), child)
        self.assertEqual(changes, [])

    def test_sklt_ilbm_child_base_and_missing_queries_are_recursive(self):
        tree = QTreeWidget()
        tree.setColumnCount(1)
        root = QTreeWidgetItem(["Root BASE"])
        child_base = QTreeWidgetItem(["Child 0: Turret"])
        skeleton = QTreeWidgetItem(["Skeleton: VP_TURRET.SKLT"])
        texture = QTreeWidgetItem(["TURRET.ILB"])
        child_base.addChildren([skeleton, texture])
        root.addChild(child_base)
        tree.addTopLevelItem(root)
        metadata = {
            id(child_base): "child base.class root/kid[0]",
            id(skeleton): "skeleton sklt.class root/kid[0]",
            id(texture): "texture ilbm.class root/kid[0]",
        }
        extra = lambda item: metadata.get(id(item), "")

        expansion = filter_tree(tree, "SKLT.CLASS", extra)
        self.assertFalse(root.isHidden())
        self.assertFalse(child_base.isHidden())
        self.assertFalse(skeleton.isHidden())
        self.assertTrue(texture.isHidden())

        expansion = filter_tree(tree, "ilbm.class", extra, expansion)
        self.assertTrue(skeleton.isHidden())
        self.assertFalse(texture.isHidden())

        expansion = filter_tree(tree, "child base.class", extra, expansion)
        self.assertFalse(child_base.isHidden())
        self.assertTrue(skeleton.isHidden())
        self.assertTrue(texture.isHidden())

        expansion = filter_tree(tree, "does-not-exist", extra, expansion)
        self.assertTrue(root.isHidden())
        self.assertTrue(child_base.isHidden())
        self.assertTrue(skeleton.isHidden())
        self.assertTrue(texture.isHidden())

        self.assertIsNone(filter_tree(tree, "", extra, expansion))
        self.assertFalse(any(
            item.isHidden()
            for item in (root, child_base, skeleton, texture)))


class TextureCatalogTests(unittest.TestCase):
    def test_union_is_lazy_case_insensitive_and_keeps_archive_only_ilbm(self):
        reference = SimpleNamespace(status="found")
        archive_texture = SimpleNamespace(
            class_id="ilbm.class", resource_name="HULL.ILB",
            decodable=True, error="")
        unsupported = SimpleNamespace(
            class_id="ilbm.class", resource_name="ArchiveOnly.ILB",
            decodable=False, error="")
        ignored = SimpleNamespace(
            class_id="sklt.class", resource_name="NotATexture",
            decodable=True, error="")
        family = SimpleNamespace(
            texture_refs={"hull.ilb": reference},
            textures={"LoadedOnly.ILB": object()},
        )
        archive = SimpleNamespace(
            resources=[archive_texture, unsupported, ignored])

        entries = build_texture_catalog(family, archive)

        self.assertEqual(
            [entry.name.casefold() for entry in entries],
            ["archiveonly.ilb", "hull.ilb", "loadedonly.ilb"])
        hull = next(
            entry for entry in entries
            if entry.name.casefold() == "hull.ilb")
        self.assertTrue(hull.referenced)
        self.assertIs(hull.archive_resource, archive_texture)
        archive_only = entries[0]
        self.assertFalse(archive_only.decodable)
        self.assertEqual(archive_only.status, "unsupported")


class DirectoryIndexTests(unittest.TestCase):
    def test_setbas_export_outputs_do_not_shadow_archive_resources(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            export = root / "set"
            export.mkdir()
            (export / "manifest.json").write_text("{}", encoding="utf-8")
            for folder in ("raw", "textures_ilbm", "textures_png"):
                target = export / folder
                target.mkdir()
                (target / "SHADOW.ILBM").write_bytes(b"preview")
            loose = root / "Loose"
            loose.mkdir()
            real = loose / "REAL.ILBM"
            real.write_bytes(b"asset")

            index = DirectoryIndex.get(root, refresh=True)
            self.assertEqual(index.find_name("SHADOW.ILBM"), [])
            self.assertEqual(index.find_name("REAL.ILBM"), [real])

            explicit = DirectoryIndex.get(
                export / "textures_ilbm", refresh=True)
            self.assertEqual(
                len(explicit.find_name("SHADOW.ILBM")), 1)


class TextureAssignmentTests(unittest.TestCase):
    @staticmethod
    def _ref(block, block_index):
        return SimpleNamespace(block=block, block_index=block_index)

    def test_large_selection_partitions_safe_static_groups_from_incompatible(self):
        ilbm = SimpleNamespace(kind="ilbm")
        vanm = SimpleNamespace(kind="bmpanim")
        safe_block = SimpleNamespace(
            texture=ilbm,
            atts=[
                SimpleNamespace(poly_id=0),
                SimpleNamespace(poly_id=1),
            ],
        )
        animated_block = SimpleNamespace(
            texture=vanm, atts=[SimpleNamespace(poly_id=3)])
        mapping = SimpleNamespace(
            poly_count=5,
            refs={
                0: [self._ref(safe_block, 2)],
                1: [self._ref(safe_block, 2)],
                2: [],
                3: [self._ref(animated_block, 4)],
            },
            status=lambda poly_id: {
                0: "mapped", 1: "mapped", 2: "unmapped",
                3: "mapped", 4: "invalid",
            }[poly_id],
        )

        result = classify_texture_assignment(mapping, range(5))

        self.assertEqual(len(result.groups), 2)
        self.assertEqual(result.groups[0].block_index, 2)
        self.assertEqual(result.groups[0].selected_polys, {0, 1})
        self.assertEqual(result.affected_polys, {0, 1, 3})
        self.assertEqual(
            dict(result.skipped),
            {2: "unmapped", 4: "invalid"})
        self.assertEqual(result.groups[1].binding_kind, "bmpanim")
        self.assertEqual(result.groups[1].affected_polys, {3})

    def test_duplicate_overlap_rejects_the_whole_material_group(self):
        block = SimpleNamespace(
            texture=SimpleNamespace(kind="ilbm"),
            atts=[
                SimpleNamespace(poly_id=0),
                SimpleNamespace(poly_id=1),
            ],
        )
        other = SimpleNamespace(texture=SimpleNamespace(kind="ilbm"), atts=[])
        mapping = SimpleNamespace(
            poly_count=2,
            refs={
                0: [self._ref(block, 0)],
                1: [self._ref(block, 0), self._ref(other, 1)],
            },
            status=lambda poly_id: "mapped" if poly_id == 0 else "duplicate",
        )

        result = classify_texture_assignment(mapping, {0})

        self.assertEqual(result.groups, ())
        self.assertEqual(
            dict(result.skipped),
            {0: "material overlaps duplicate mapping"})

    def test_primary_and_tracy_bindings_are_distinct_choices(self):
        block = SimpleNamespace(
            texture=SimpleNamespace(kind="ilbm", name="BODY.ILBM"),
            tracy_texture=SimpleNamespace(
                kind="ilbm", name="MASK.ILBM"),
            atts=[SimpleNamespace(poly_id=0)],
        )
        mapping = SimpleNamespace(
            poly_count=1,
            refs={0: [self._ref(block, 3)]},
            status=lambda _poly_id: "mapped",
        )
        result = classify_texture_assignment(mapping, {0})
        self.assertEqual(
            [(group.binding_slot, group.binding_name)
             for group in result.groups],
            [("texture", "BODY.ILBM"),
             ("tracy_texture", "MASK.ILBM")])
        self.assertEqual(result.affected_polys, {0})

    def test_same_logical_texture_does_not_expand_past_selected_blocks(self):
        first = SimpleNamespace(
            texture=SimpleNamespace(kind="ilbm", name="HUBI.ILBM"),
            tracy_texture=None,
            atts=[SimpleNamespace(poly_id=0)],
        )
        second = SimpleNamespace(
            texture=SimpleNamespace(kind="ilbm", name="hubi.ilbm"),
            tracy_texture=None,
            atts=[
                SimpleNamespace(poly_id=1),
                SimpleNamespace(poly_id=2),
            ],
        )
        different = SimpleNamespace(
            texture=SimpleNamespace(kind="ilbm", name="OTHER.ILBM"),
            tracy_texture=None,
            atts=[SimpleNamespace(poly_id=3)],
        )
        mapping = SimpleNamespace(
            poly_count=4,
            refs={
                0: [self._ref(first, 2)],
                1: [self._ref(second, 3)],
                2: [self._ref(second, 3)],
                3: [self._ref(different, 4)],
            },
            status=lambda _poly_id: "mapped",
        )

        result = classify_texture_assignment(mapping, {0})

        self.assertEqual(len(result.groups), 1)
        group = result.groups[0]
        self.assertEqual(
            [binding.block_index for binding in group.bindings], [2])
        self.assertEqual(group.selected_polys, {0})
        self.assertEqual(group.affected_polys, {0})
        self.assertNotIn(3, result.affected_polys)

    def test_partial_shared_material_block_tracks_only_selected_polygons(self):
        block = SimpleNamespace(
            texture=SimpleNamespace(kind="ilbm", name="BODY.ILBM"),
            tracy_texture=None,
            atts=[
                SimpleNamespace(poly_id=0),
                SimpleNamespace(poly_id=1),
            ],
        )
        mapping = SimpleNamespace(
            poly_count=2,
            refs={
                0: [self._ref(block, 0)],
                1: [self._ref(block, 0)],
            },
            status=lambda _poly_id: "mapped",
        )
        partial = classify_texture_assignment(mapping, {0})
        self.assertEqual(len(partial.groups), 1)
        self.assertEqual(partial.groups[0].selected_polys, {0})
        self.assertEqual(partial.groups[0].affected_polys, {0})
        self.assertEqual(
            partial.groups[0].bindings[0].selected_polys, {0})

        complete = classify_texture_assignment(mapping, {0, 1})
        self.assertEqual(len(complete.groups), 1)
        self.assertEqual(complete.groups[0].affected_polys, {0, 1})


if __name__ == "__main__":
    unittest.main()
