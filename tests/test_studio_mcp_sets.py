"""Studio MCP set tools: sector edits, installing a new model, textures, new sets and renders.

Runs on a temporary copy of Set1, Set3 and Scripts, so the real game data is never written.
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

STUDIO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STUDIO))

from PIL import Image  # noqa: E402

from studio_mcp import core, edit, guide, history, sets, textures  # noqa: E402

try:
    REAL_DATA = core.data_root()
    HAVE_DATA = all((REAL_DATA / part).is_dir() for part in ("Sets/Set1", "Sets/Set3", "Scripts"))
except core.McpToolError:
    HAVE_DATA = False


class PureTests(unittest.TestCase):
    def test_palette_mapping_never_uses_the_see_through_index(self):
        palette = [(0, 0, 0)] + [(i, i, i) for i in range(1, 256)]
        pixels = textures.to_indices(Image.new("RGB", (4, 4), (0, 0, 0)), palette)
        self.assertNotIn(0, pixels)
        self.assertIn(0, textures.to_indices(Image.new("RGB", (4, 4), (0, 0, 0)), palette, allow_index0=True))

    def test_layers_build_a_picture_and_reject_unknown_ones(self):
        picture = textures.build_picture([{"op": "fill", "color": "#406080"},
                                          {"op": "windows", "cols": 2, "rows": 2},
                                          {"op": "noise", "amount": 5}], size=[32, 32])
        self.assertEqual(picture.size, (32, 32))
        with self.assertRaises(core.McpToolError):
            textures.build_picture([{"op": "bogus"}], size=[32, 32])

    def test_history_removes_files_that_did_not_exist(self):
        folder = Path(tempfile.mkdtemp(dir=core.work_dir()))
        try:
            old, new = folder / "old.txt", folder / "new.txt"
            old.write_text("before")
            history.snapshot(folder, {old: b"before", new: None}, "test")
            old.write_text("after")
            new.write_text("created")
            history.undo(folder)
            self.assertEqual(old.read_text(), "before")
            self.assertFalse(new.exists())
            history.redo(folder)
            self.assertEqual(new.read_text(), "created")
        finally:
            history.clear(folder)
            shutil.rmtree(folder, ignore_errors=True)

    def test_help_topics(self):
        self.assertIn("install_model", guide.help_text("set_ops"))
        self.assertIn("windows", guide.help_text("textures"))
        self.assertIn("SECTORS", guide.help_text("sets"))


@unittest.skipUnless(HAVE_DATA, "game data with Set1, Set3 and Scripts not found")
class SetToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.saved_env = {key: os.environ.get(key) for key in ("ONUA_DATA", "NME_GAME_DATA")}
        cls.root = Path(tempfile.mkdtemp(prefix="onua-sets-test-"))
        cls.data = cls.root / "Data"
        for part in ("Sets/Set1", "Sets/Set3", "Scripts"):
            shutil.copytree(REAL_DATA / part, cls.data / part)
        (cls.data / "Models").mkdir()
        os.environ["ONUA_DATA"] = os.environ["NME_GAME_DATA"] = str(cls.data)

    @classmethod
    def tearDownClass(cls):
        for key, value in cls.saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for folder in (cls.data / "Sets").iterdir():
            history.clear(folder)
        shutil.rmtree(cls.root, ignore_errors=True)

    def sdf(self, name="Set3"):
        return sets.open_document(name).sdf.path

    def test_variant_dry_run_failure_and_undo(self):
        path = self.sdf()
        before = path.read_bytes()
        steps = [{"op": "new_sector", "copy_of": 5, "comment": "variant", "save_as": "v"},
                 {"op": "place_model", "sector": {"named": "v"}, "cells": [0], "model": "ST_CITY1"}]
        result = sets.run("set3", steps, dry_run=True)
        self.assertFalse(result["written"])
        self.assertEqual(path.read_bytes(), before)
        with self.assertRaises(core.McpToolError):
            sets.run("set3", steps + [{"op": "place_model", "sector": {"named": "v"}, "model": "NO_SUCH"}])
        self.assertEqual(path.read_bytes(), before)
        result = sets.run("set3", steps)
        number = result["results"][0]["sector"]
        row = sets.sectors("set3", [number])[0]
        self.assertEqual(row["comment"], "variant")
        self.assertTrue(row["cells"][0].endswith("ST_CITY1"))
        sets.undo("set3")
        self.assertEqual(path.read_bytes(), before)

    def test_new_building_install_place_and_undo(self):
        made = core.new_model("Models/Base/New/Test_Block_Intact", "NX_TBLK1", "NX_TBLK1", donor="set3:ST_CITY1")
        textures.make("Models/Base/New/Test_Block_Intact/NXTBK.ILBM", [{"op": "fill", "color": "#605850"},
                      {"op": "windows", "cols": 4, "rows": 6}], set_name="set3")
        edit.run(made["base_file"], [{"op": "import_geometry", "source": "set3:ST_EMPTY"},
                                     {"op": "create_primitive", "shape": "box", "center": [0, -100, 0],
                                      "size": [200, 200, 200], "material": {"block": 1}, "save_as": "box"},
                                     {"op": "set_texture", "select": {"named": "box"}, "texture": "NXTBK.ILBM"},
                                     {"op": "auto_shade"}], set_hint="set3")
        folder = sets.set_folder("set3")
        before = {p for p in folder.rglob("*") if p.is_file()}
        result = sets.run("set3", [
            {"op": "install_model", "model": "Models/Base/New/Test_Block_Intact/NX_TBLK1.BASE", "like": "ST_CITY1",
             "save_as": "m"},
            {"op": "new_sector", "copy_of": 5, "save_as": "s"},
            {"op": "place_model", "sector": {"named": "s"}, "cells": [4], "model": {"named": "m"}}])
        install = result["results"][0]
        self.assertEqual(install["copied_resources"], ["NXTBK.ILBM"])
        self.assertEqual(sets.info("set3")["problems"], [])
        added = {p.name.casefold() for p in folder.rglob("*") if p.is_file()} - {p.name.casefold() for p in before}
        self.assertTrue({"nx_tblk1.bas", "nx_tblk1.sklt", "nxtbk.ilbm"} <= added)
        override = sets.open_document("set3").override_path.read_text()
        self.assertIn(f"lego {install['model']} NX_TBLK1.base", override)
        with self.assertRaises(core.McpToolError):   # the same name twice needs replace=
            sets.run("set3", [{"op": "install_model", "model": "Models/Base/New/Test_Block_Intact/NX_TBLK1.BASE"}])
        sets.undo("set3")
        self.assertEqual({p for p in folder.rglob("*") if p.is_file()}, before)

    def test_install_refuses_set_bas_names(self):
        core.export_model(core.resolve_source("set3:ST_CITY1"), "Models/Base/Original_Refined/Test_City1",
                          "ST_CITY1", "SX_CITY1")
        with self.assertRaises(core.McpToolError):
            sets.run("set3", [{"op": "install_model",
                               "model": "Models/Base/Original_Refined/Test_City1/ST_CITY1.BASE"}])
        result = sets.run("set3", [{"op": "install_model", "name": "SX_CITY1", "replace": "ST_CITY1",
                                    "collision": "keep",
                                    "model": "Models/Base/Original_Refined/Test_City1/ST_CITY1.BASE"}],
                          dry_run=True)
        self.assertTrue(result["results"][0]["replaced"])

    def test_create_set_keeps_border_sectors_and_writes_textures_at_the_root(self):
        result = sets.create(9, "set3")
        self.assertEqual(result["sectors"], 8)
        self.assertEqual(sorted(sets.open_document("set9").sdf.sectors), list(range(248, 256)))
        out = sets.run("set9", [{"op": "add_texture", "name": "NXGRD.ILBM", "source": "set3:BODEN2.ILBM"},
                                {"op": "new_sector", "models": "ST_CITY1", "ground": 1, "symbol": 7}])
        self.assertEqual(Path(out["results"][0]["file"]).parent, sets.set_folder("set9"))
        with self.assertRaises(core.McpToolError):   # repainting needs a Loose folder: Set1-Set7 only
            sets.run("set9", [{"op": "add_texture", "name": "BODEN2.ILBM", "replace": True}])
        with self.assertRaises(core.McpToolError):
            sets.create(9, "set3")

    def test_cells_buildings_and_import_in_one_batch(self):
        result = sets.run("set3", [
            {"op": "import_sectors", "from_set": "set1", "sectors": [5]},
            {"op": "new_sector", "models": ["ST_HAUS1", None, "ST_CITY1", None, None, None, None, None, None],
             "comment": "test", "save_as": "s"},
            {"op": "rotate_cells", "sector": {"named": "s"}, "cells": [0, 1, 3, 4], "quarters": 1},
            {"op": "copy_cells", "from_sector": 5, "cells": [0], "to_sector": {"named": "s"}, "to_cells": [8]},
            {"op": "add_building", "states": ["ST_HO", "STxHO", "STyHO", "STzHO"], "save_as": "b"},
            {"op": "place_building", "sector": {"named": "s"}, "cells": [4], "building": {"named": "b"}},
            {"op": "set_sector", "sectors": [{"named": "s"}], "ground": 4, "symbol": "auto",
             "comment": "test block"},
            {"op": "shift_state", "sector": {"named": "s"}, "cells": [4], "state": 2}])
        number = result["results"][1]["sector"]
        row = sets.sectors("set3", [number], detail=True)[0]
        self.assertEqual((row["ground"], row["comment"]), (4, "test block"))
        self.assertEqual(row["cells"][4]["base"].casefold(), "styho.base")
        self.assertEqual(sets.info("set3")["problems"], [])
        sets.undo("set3")

    def test_render_sectors_grid_and_models(self):
        from studio_mcp import server
        out = server.studio_set_render("set1", sectors=[5], models=["ST_CITY1"], grid=[[5, 6]],
                                       views=["iso", "top"], label_cells=True, width=160, height=120)
        self.assertTrue(out[0].startswith("Rendered 6 picture(s)"), out[0])
