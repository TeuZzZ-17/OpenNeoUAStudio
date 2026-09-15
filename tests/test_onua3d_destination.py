from __future__ import annotations

import os
from pathlib import Path
import unittest
from unittest.mock import patch

from onua3d_import import Onua3dImportError, import_onua3d, prepare_onua3d
from tests import test_onua3d_export as fixture
from onua3d_export import write_onua3d


class Onua3dDestinationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.Onua3dTests.setUpClass()

    def setUp(self):
        fixture.Onua3dTests.setUp(self)
        self.package = self.root / "baseline.onua3d"
        write_onua3d(self.source, self.package)

    def test_existing_empty_directory_is_materialized_without_suffix(self):
        destination = self.root / "empty"
        destination.mkdir()
        with prepare_onua3d(self.package) as prepared:
            staging = prepared.staging_root
            result = prepared.materialize(destination)
            self.assertTrue(staging.exists())
        self.assertEqual(result.output_root, destination.resolve())
        self.assertTrue((destination / "TEST.BASE").is_file())
        self.assertFalse((destination / "baseline_imported").exists())

    def test_missing_directory_is_created_at_exact_destination(self):
        destination = self.root / "new" / "family"
        result = import_onua3d(self.package, destination)
        self.assertEqual(result.output_root, destination.resolve())
        self.assertTrue((destination / "TEST.BASE").is_file())

    def test_nonempty_directory_is_rejected_and_sentinel_is_preserved(self):
        destination = self.root / "occupied"
        destination.mkdir()
        sentinel = destination / "sentinel.txt"
        sentinel.write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(Onua3dImportError, "must be empty"):
            import_onua3d(self.package, destination)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
        self.assertEqual(list(destination.iterdir()), [sentinel])

    def test_direct_symlink_destination_is_rejected(self):
        real = self.root / "real"
        real.mkdir()
        destination = self.root / "link"
        try:
            destination.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"directory symlink unavailable: {exc}")
        with self.assertRaisesRegex(Onua3dImportError, "symlink/junction/reparse"):
            import_onua3d(self.package, destination)
        self.assertEqual(list(real.iterdir()), [])

    def test_symlink_ancestor_is_rejected_before_resolve(self):
        real = self.root / "real-parent"
        real.mkdir()
        link = self.root / "link-parent"
        try:
            link.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"directory symlink unavailable: {exc}")
        destination = link / "family"
        with self.assertRaisesRegex(Onua3dImportError, "symlink/junction/reparse"):
            import_onua3d(self.package, destination)
        self.assertFalse((real / "family").exists())

    def test_reparse_point_destination_and_ancestor_are_rejected(self):
        direct = self.root / "direct-reparse"
        direct.mkdir()
        ancestor = self.root / "ancestor-reparse"
        ancestor.mkdir()

        def mocked_reparse(path, _info):
            return path.name in {"direct-reparse", "ancestor-reparse"}

        with patch("onua3d_import._is_reparse_or_link", side_effect=mocked_reparse):
            with self.assertRaisesRegex(Onua3dImportError, "reparse"):
                import_onua3d(self.package, direct)
            with self.assertRaisesRegex(Onua3dImportError, "reparse"):
                import_onua3d(self.package, ancestor / "family")
        self.assertEqual(list(direct.iterdir()), [])
        self.assertEqual(list(ancestor.iterdir()), [])

    def test_final_validation_failure_rolls_back_to_existing_empty_directory(self):
        destination = self.root / "final-failure"
        destination.mkdir()
        with self.assertRaisesRegex(Onua3dImportError, "forced final validation"):
            with prepare_onua3d(self.package) as prepared:
                with patch(
                        "onua3d_import._verify_materialized",
                        side_effect=Onua3dImportError("forced final validation")):
                    prepared.materialize(destination)
        self.assertTrue(destination.is_dir())
        self.assertEqual(list(destination.iterdir()), [])

    def test_mid_publish_failure_removes_new_directory_and_partial_files(self):
        destination = self.root / "mid-failure"
        real_link = os.link
        calls = 0

        def fail_second_link(source, target, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("forced mid-publish failure")
            return real_link(source, target, *args, **kwargs)

        with self.assertRaisesRegex(Onua3dImportError, "mid-publish"):
            with prepare_onua3d(self.package) as prepared:
                with patch("verified_io.os.link", side_effect=fail_second_link):
                    prepared.materialize(destination)
        self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
