"""Same-named files in other models' folders must not hide or replace the SET.BAS copy."""

from pathlib import Path
import unittest

from studio.assets.family import load_asset_family

DATA = Path(__file__).resolve().parents[2].parent / "UA Rising" / "Data"
BASE = DATA / "Models" / "Base" / "ALPHA_LEOPARD_FIRE" / "LEOPARD_FIRE.BASE"
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
        family = load_asset_family(BASE, setbas=SETBAS)
        self.assertEqual(family.texture_refs["BAGGER.ILBM"].status, "found")
        self.assertEqual(family.animation_refs["LEOSIDE1.ANM"].status, "found")


if __name__ == "__main__":
    unittest.main()
