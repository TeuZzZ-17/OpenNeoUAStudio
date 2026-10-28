"""Same-named files in other models' folders must not hide or replace the SET.BAS copy."""

from pathlib import Path
import shutil
import tempfile
import unittest

from studio.assets.family import load_asset_family

DATA = Path(__file__).resolve().parents[2].parent / "UA Rising" / "Data"
BASE = DATA / "Models" / "Base" / "Alpha_Models" / "Leopard_Fire" / "ALPHA_LEOPARD_FIRE.BASE"
SETBAS = DATA / "Sets" / "Set1" / "Objects" / "Set.BAS"


@unittest.skipUnless(BASE.is_file() and SETBAS.is_file(), "game data not available")
class ForeignModelFolderAssetsTest(unittest.TestCase):
    def test_fx2_and_hotshot_come_from_set_bas(self):
        family = load_asset_family(BASE, setbas=SETBAS)
        self.assertEqual(family.texture_refs["FX2.ILBM"].status, "setbas")
        self.assertEqual(family.animation_refs["HOTSHOT1.ANM"].status, "setbas")
        bad = [d for d in family.dependencies
               if d.status in ("missing", "ambiguous", "failed_load")]
        self.assertEqual(bad, [])

    def test_own_folder_files_still_win(self):
        own = DATA / "Models" / "Base" / "Alpha_Models" / "Weasel_Alpha_Fire" / "ALPHA_VP_SIMP2_FIRE.BASE"
        if not own.is_file():
            self.skipTest("ALPHA_VP_SIMP2_FIRE not available")
        family = load_asset_family(own, setbas=SETBAS)
        self.assertEqual(family.texture_refs["BAGGER.ILBM"].status, "found")
        self.assertEqual(family.animation_refs["SIMPEL.ANM"].status, "found")


SKY = DATA / "Objects" / "Loose" / "h2"


@unittest.skipUnless((SKY / "h2.BASE").is_file() and (SKY / "H.PNG").is_file(), "game data not available")
class ExternalBasePngFallbackTest(unittest.TestCase):
    """An external BASE uses the ILBM beside it first, then a PNG with the same name."""

    def _family(self, keep_ilbm):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        for item in SKY.iterdir():
            if keep_ilbm or item.suffix.lower() != ".ilbm":
                shutil.copyfile(item, tmp / item.name)
        return load_asset_family(tmp / "h2.BASE", isolated_root=tmp)

    def test_png_used_when_ilbm_is_missing(self):
        family = self._family(keep_ilbm=False)
        ref = family.texture_refs["H.ILBM"]
        self.assertEqual(ref.status, "found")
        self.assertEqual(ref.path.suffix.lower(), ".png")
        self.assertIn("H.ILBM", family.textures)

    def test_ilbm_wins_over_png(self):
        if not (SKY / "H.ILBM").is_file():
            self.skipTest("H.ILBM not present")
        family = self._family(keep_ilbm=True)
        self.assertEqual(family.texture_refs["H.ILBM"].path.suffix.lower(), ".ilbm")


if __name__ == "__main__":
    unittest.main()
