import hashlib
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

from vp_manager import (
    AMBIGUOUS_VP,
    EmbeddedVPEntry,
    VPConflictError,
    VPEmbeddedError,
    VPManager,
    VPParseError,
    build_vp_relations,
    export_visproto,
    export_visproto_bytes,
    load_visproto,
    normalize_base_name,
    normalize_skeleton_name,
    parse_visproto_bytes,
    parse_visproto_text,
    reconstruct_embedded_vps,
)


class VisprotoParsingTests(unittest.TestCase):
    def test_comments_blanks_dummy_and_optional_terminator_keep_real_slots(self):
        table = parse_visproto_text(
            "# full comment\n"
            "\n"
            "VP_ONE.base ; first model\n"
            "; another full comment\n"
            "dummy.base # real placeholder\n"
            "VP_TWO.base trailing note\n"
        )

        self.assertFalse(table.had_terminator)
        self.assertEqual(
            [(entry.index, entry.base_name, entry.comment)
             for entry in table.entries],
            [
                (0, "VP_ONE.base", "first model"),
                (1, "dummy.base", "real placeholder"),
                (2, "VP_TWO.base", "trailing note"),
            ],
        )
        self.assertTrue(table.entries[1].is_dummy)
        self.assertEqual(table.entries[1].vp_id, 1)
        with self.assertRaises(VPParseError):
            parse_visproto_text(
                "VP_ONE.base\n", require_terminator=True)

    def test_terminator_is_retained_and_rejects_later_content(self):
        table = parse_visproto_text(
            "VP_ONE.base\n>\n# trailing comment\n>\n",
            require_terminator=True,
        )
        self.assertTrue(table.had_terminator)
        self.assertEqual(len(table), 1)

        with self.assertRaisesRegex(VPParseError, "after VISPROTO"):
            parse_visproto_text("VP_ONE.base\n>\nVP_TWO.base\n")

    def test_legacy_cp1252_and_unicode_boms_decode_strictly(self):
        cp1252 = "VP_ONE.base ; WÜRFEL\n>\n".encode("cp1252")
        table = parse_visproto_bytes(cp1252, require_terminator=True)
        self.assertEqual(table.source_encoding, "cp1252")
        self.assertEqual(table.entries[0].comment, "WÜRFEL")

        utf16 = "VP_TWO.base\n>\n".encode("utf-16")
        decoded = parse_visproto_bytes(utf16, require_terminator=True)
        self.assertEqual(decoded.source_encoding, "utf-16")
        self.assertEqual(decoded.entries[0].base_name, "VP_TWO.base")

    def test_canonical_export_always_terminates_and_comments_are_optional(self):
        table = parse_visproto_text(
            "VP_ONE.base ; first\n"
            "dummy.base # real\n"
        )
        with_comments = export_visproto(table)
        self.assertEqual(
            with_comments,
            "VP_ONE.base ; first\n"
            "dummy.base ; real\n"
            ">\n",
        )
        self.assertEqual(
            export_visproto(table, include_comments=False),
            "VP_ONE.base\n"
            "dummy.base\n"
            ">\n",
        )
        self.assertEqual(
            export_visproto_bytes(table, encoding="cp1252"),
            with_comments.encode("cp1252"),
        )
        with self.assertRaises(ValueError):
            export_visproto(table, newline="not-a-newline")
        reparsed = parse_visproto_text(
            with_comments, require_terminator=True)
        self.assertEqual(
            [entry.base_name for entry in reparsed],
            [entry.base_name for entry in table],
        )


class VPEditingTests(unittest.TestCase):
    def setUp(self):
        self.table = parse_visproto_text(
            "VP_ONE.base ; slot zero\n"
            "dummy.base ; slot one\n"
            "VP_TWO.base ; slot two\n"
            ">\n",
        )

    def test_assign_requires_explicit_replacement_and_undo_is_in_memory(self):
        manager = VPManager(self.table)
        with self.assertRaises(VPConflictError):
            manager.assign(1, "VP_NEW.base")
        self.assertFalse(manager.can_undo)
        self.assertEqual(manager.table.entries[1].base_name, "dummy.base")

        self.assertTrue(manager.assign(
            1, "VP_NEW.base", expected_current="DUMMY.BASE"))
        self.assertEqual(manager.table.entries[1].base_name, "VP_NEW.base")
        self.assertTrue(manager.can_undo)
        self.assertTrue(manager.undo())
        self.assertEqual(manager.table, self.table)
        self.assertTrue(manager.redo())
        self.assertEqual(manager.table.entries[1].base_name, "VP_NEW.base")

    def test_explicit_allow_replace_and_swap_are_undoable(self):
        manager = VPManager(self.table)
        self.assertTrue(manager.assign(
            0, "VP_REPLACED.base", allow_replace=True))
        replaced = manager.table
        self.assertEqual(replaced.entries[0].comment, "slot zero")

        self.assertTrue(manager.swap(0, 2))
        self.assertEqual(
            [entry.base_name for entry in manager.table],
            ["VP_TWO.base", "dummy.base", "VP_REPLACED.base"],
        )
        # Comments describe slots and do not silently move with assignments.
        self.assertEqual(manager.table.entries[0].comment, "slot zero")
        self.assertTrue(manager.undo())
        self.assertEqual(manager.table, replaced)

    def test_base_relation_reports_every_slot_and_marks_ambiguity(self):
        table = parse_visproto_text(
            "VP_DUP.base\n"
            "VP_OTHER.base\n"
            "objects\\vp_dup.BASE\n"
            ">\n",
        )
        self.assertEqual(table.vps_for_base("vp_dup"), (0, 2))
        self.assertEqual(table.vp_for_base("VP_DUP.base"), AMBIGUOUS_VP)
        self.assertEqual(table.vp_for_base("missing.base"), None)


class EmbeddedVPTests(unittest.TestCase):
    @staticmethod
    def _object(name, skeleton, offset):
        return SimpleNamespace(
            name=name,
            skeleton_name=skeleton,
            source_objt_offset=offset,
            kids=[],
        )

    def test_reconstruction_uses_first_root_child_positionally(self):
        visproto = SimpleNamespace(
            source_objt_offset=100,
            kids=[
                self._object("VP_ONE", "Skeleton/ONE.sklt", 110),
                self._object("dummy", "Skeleton/DUMMY.sklt", 120),
            ],
        )
        unrelated = SimpleNamespace(
            source_objt_offset=200,
            kids=[self._object("NOT_A_VP", "OTHER.sklt", 210)],
        )
        parsed = SimpleNamespace(
            root=SimpleNamespace(kids=[visproto, unrelated]))

        with patch("vp_manager.parse_base_file", return_value=parsed):
            embedded = reconstruct_embedded_vps("SET.BAS")

        self.assertEqual(embedded.container_source_offset, 100)
        self.assertEqual(
            [entry.base_name for entry in embedded.entries],
            ["VP_ONE.base", "dummy.base"],
        )
        self.assertEqual(
            [entry.index for entry in embedded.entries], [0, 1])
        self.assertEqual(
            [entry.base_name for entry in embedded.as_table()],
            ["VP_ONE.base", "dummy.base"],
        )

    def test_missing_first_child_is_rejected_without_guessing(self):
        parsed = SimpleNamespace(root=SimpleNamespace(kids=[]))
        with patch("vp_manager.parse_base_file", return_value=parsed):
            with self.assertRaises(VPEmbeddedError):
                reconstruct_embedded_vps("SET.BAS")

    def test_relations_normalize_names_and_retain_all_ambiguous_vps(self):
        relations = build_vp_relations([
            EmbeddedVPEntry(
                0, "VP_ONE.base", "Skeleton\\Shared.SKLT"),
            EmbeddedVPEntry(
                1, "objects/VP_TWO.BASE", "shared.skl"),
            EmbeddedVPEntry(
                2, "vp_one.BASE", "Skeleton/Unique"),
        ])

        self.assertEqual(
            normalize_base_name("Objects\\VP_ONE"), "vp_one.base")
        self.assertEqual(
            normalize_skeleton_name("Skeleton\\SHARED.skl"),
            "shared.sklt")
        self.assertEqual(relations.vps_for_base("vp_one"), (0, 2))
        self.assertEqual(relations.vp_for_base("vp_one"), AMBIGUOUS_VP)
        self.assertEqual(
            relations.vps_for_skeleton("Skeleton/SHARED.SKLT"), (0, 1))
        self.assertEqual(
            relations.vp_for_skeleton("shared.skl"), AMBIGUOUS_VP)
        self.assertEqual(relations.vp_for_skeleton("unique"), 2)


class RealSet1ReadOnlyTests(unittest.TestCase):
    ROOT = Path.home() / "Documents" / "UA_RC1" / "DATA" / "SET1"
    SET_BAS = ROOT / "OBJECTS" / "SET.BAS"
    VISPROTO = ROOT / "SCRIPTS" / "VISPROTO.LST"

    @unittest.skipUnless(
        SET_BAS.is_file() and VISPROTO.is_file(),
        "UA_RC1 Set1 corpus is not available on this machine")
    def test_real_list_matches_first_embedded_child_without_asset_writes(self):
        before = {
            path: hashlib.sha256(path.read_bytes()).digest()
            for path in (self.SET_BAS, self.VISPROTO)
        }

        table = load_visproto(
            self.VISPROTO, require_terminator=True)
        embedded = reconstruct_embedded_vps(self.SET_BAS)

        self.assertEqual(table.source_encoding, "cp1252")
        self.assertTrue(table.had_terminator)
        self.assertEqual(len(table), 362)
        self.assertEqual(len(embedded.entries), 362)
        self.assertEqual(table.entries[22].base_name.lower(), "dummy.base")
        self.assertTrue(table.entries[22].is_dummy)
        # The shipped line comment says "90", but the real positional VP is
        # zero-based slot 20; comments never define indices.
        self.assertEqual(table.entries[20].index, 20)
        self.assertIn("90", table.entries[20].comment)

        listed_names = [
            normalize_base_name(entry.base_name) for entry in table]
        embedded_names = [
            entry.normalized_base for entry in embedded.entries]
        self.assertEqual(listed_names, embedded_names)

        relations = embedded.relations()
        self.assertEqual(
            relations.vps_for_skeleton(
                "Skeleton\\VP_BRGR1.SKLT"), (0,))
        self.assertEqual(relations.vp_for_base("VP_BRGR1"), 0)
        self.assertEqual(
            relations.vp_for_base("dummy.base"), AMBIGUOUS_VP)
        self.assertGreater(
            len(relations.vps_for_skeleton("DUMMY.skl")), 1)

        canonical = export_visproto(table)
        canonical_table = parse_visproto_text(
            canonical, require_terminator=True)
        self.assertEqual(
            [entry.base_name for entry in canonical_table],
            [entry.base_name for entry in table],
        )

        after = {
            path: hashlib.sha256(path.read_bytes()).digest()
            for path in (self.SET_BAS, self.VISPROTO)
        }
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
