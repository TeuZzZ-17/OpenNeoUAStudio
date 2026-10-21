"""Export and Overwrite of a loose BASE opened from the game folder."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QMessageBox

from studio.exchange.portable import AssetResolver, _alias_names, set_backup_enabled
from studio.formats.sklt import parse_sklt_file

DATA = Path(__file__).resolve().parents[2].parent / "UA Rising" / "Data"
SETBAS = DATA / "Sets" / "Set1" / "Objects" / "Set.BAS"
MODEL = DATA / "Models" / "Base" / "ALPHA_LEOPARD_FIRE"


class ExtensionlessResourceTest(unittest.TestCase):
    def test_animation_without_extension_finds_exported_file(self):
        # SET.BAS animations such as PANZERKETTEN have no extension; the package stores them as .ANM.
        self.assertIn("PANZERKETTEN.ANM", _alias_names("PANZERKETTEN", "animation"))
        self.assertEqual(_alias_names("PANZERKETTEN", "any"), ["PANZERKETTEN"])
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "PANZERKETTEN.ANM").write_bytes(b"x")
            ref = AssetResolver([tmp]).resolve("PANZERKETTEN", "animation")
            self.assertEqual(ref.status, "found")


@unittest.skipUnless((MODEL / "ALPHA_LEOPARD_FIRE.BASE").is_file() and SETBAS.is_file(), "game data not available")
class LooseBaseSaveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _window(self, folder):
        from model_editor import ModelEditorWindow
        window = ModelEditorWindow()
        window.open_setbas(SETBAS)
        window.open_base(folder / "ALPHA_LEOPARD_FIRE.BASE", confirm_discard=False)
        self.app.processEvents()
        return window

    def test_export_with_set_bas_animation_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "model"
            shutil.copytree(MODEL, work)
            window = self._window(work)
            out = Path(tmp) / "out"
            owner, family, obj, _model, _tree = window._model_save_context("root")
            ok = window._write_model_files(
                owner, family, obj, out / "VPLEOPRF.sklt", out / "T.BASE", ask_replace=False)
            self.assertTrue(ok)
            self.assertTrue((out / "T.BASE").is_file())

    def test_overwrite_keeps_backup_and_writes_only_base_and_skeleton(self):
        set_backup_enabled(True)
        self.addCleanup(set_backup_enabled, False)
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "model"
            shutil.copytree(MODEL, work)
            names_before = sorted(p.name for p in work.iterdir())
            window = self._window(work)
            self.assertTrue(window.can_overwrite_geometry())
            obj = window._owner_to_obj["root"]
            moved = tuple(obj.skeleton.points[12])
            obj.skeleton.points[12] = (moved[0] + 3.0, moved[1], moved[2])
            window._geom_dirty["root"] = obj
            yes = QMessageBox.StandardButton.Yes
            with patch.object(QMessageBox, "question", staticmethod(lambda *a, **k: yes)), \
                    patch.object(QMessageBox, "critical", staticmethod(lambda *a, **k: self.fail(a[2]))):
                window._overwrite_model()
            saved = parse_sklt_file(work / "VPLEOPRF.sklt").points[12]
            self.assertEqual(tuple(saved), (moved[0] + 3.0, moved[1], moved[2]))
            added = sorted(set(p.name for p in work.iterdir()) - set(names_before))
            self.assertEqual(added, ["ALPHA_LEOPARD_FIRE.BASE.bak", "VPLEOPRF.sklt.bak"])


if __name__ == "__main__":
    unittest.main()
