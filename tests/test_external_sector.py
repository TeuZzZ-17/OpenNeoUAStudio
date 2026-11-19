"""External sectors (.sector folders and sector .onua archives): format, Sector Editor import/export.

The data tests run on a temporary copy of Set3 and Scripts, so the real game data is never written.
"""

import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

STUDIO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STUDIO))

from studio_mcp import core  # noqa: E402
from studio.formats.sector_file import (  # noqa: E402
    SectorFileError, format_sector, parse_sec_type, parse_sector_text, read_sector,
)

try:
    REAL_DATA = core.data_root()
    HAVE_DATA = all((REAL_DATA / part).is_dir() for part in ("Sets/Set3", "Scripts"))
except core.McpToolError:
    HAVE_DATA = False

OWN_MODEL = "Models/Base/New/Ghorkov/Kyt_Flak/Kyt_Flak_Normal"

SAMPLE = """; sample
begin_sector
    cells = G|G|G|G|CORE|G|G|G|G
    ground = 0
    map_symbol = 3
end
begin_piece G
    states = ground
end
begin_piece CORE
    states = core|core|core|ground
    destructible = 1
end
begin_model ground
    visual = ST_EMPTY.base
end
begin_model core
    visual = Kyt_Flak_Normal.BASE
    collision = KYTFLKN1.sklt
    shield = 40
    explosion = 49|0|0|100
end
"""


class FormatTests(unittest.TestCase):
    def test_sec_type_values(self):
        self.assertEqual(parse_sec_type("50"), ("", 50))
        self.assertEqual(parse_sec_type("Data/Models/Sectors/A/A.sector|50"), ("Data/Models/Sectors/A/A.sector", 50))
        self.assertEqual(parse_sec_type("data/Models/Sectors/A/A.sector"), ("Data/Models/Sectors/A/A.sector", 0))
        for bad in ("Models/A.sector", "Data/../A.sector", "Data/A.txt", "300"):
            with self.assertRaises(SectorFileError):
                parse_sec_type(bad)

    def test_parse_and_round_trip(self):
        sector = parse_sector_text(SAMPLE)
        self.assertEqual(len(sector.cells), 9)
        self.assertEqual(sector.piece("core").state(3), "ground")
        self.assertEqual(sector.model("core").explosions, ((49, 0, 0, 100),))
        self.assertIsNone(sector.model("ground").shield)
        again = parse_sector_text(format_sector(sector))
        self.assertEqual((again.cells, again.pieces, again.models), (sector.cells, sector.pieces, sector.models))

    def test_engine_rejections(self):
        broken = {
            "unknown key": SAMPLE.replace("map_symbol = 3", "colour = 3"),
            "unknown piece": SAMPLE.replace("G|CORE|G", "G|NOPE|G"),
            "unknown model": SAMPLE.replace("core|core|core|ground", "core|core|nope|ground"),
            "two states": SAMPLE.replace("core|core|core|ground", "core|ground"),
            "duplicate": SAMPLE + "begin_piece G\n    states = ground\nend\n",
            "ground range": SAMPLE.replace("ground = 0", "ground = 9"),
            "unclosed": SAMPLE + "begin_model x\n    visual = y\n",
        }
        for label, text in broken.items():
            with self.subTest(label), self.assertRaises(SectorFileError):
                parse_sector_text(text)

    def test_transition_explosions_follow_the_engine(self):
        from entity_designer import destruction_fx as fx
        from sector_editor.transition_fx import effect_position, required_vps, transition_simulation
        catalog = {7: fx.legacy_vehicle_from_text(7, "new_vehicle 7\nvp_dead = 30\nvp_megadeth = 31\nmass = 50\nend")}
        self.assertEqual(effect_position((7, 10, 20, 250)), (10.0, -250.0, -20.0))
        self.assertIsNone(transition_simulation([(), ()], catalog))
        simulation = transition_simulation([((7, 0, 0, 200),)], catalog)
        self.assertGreater(simulation.duration, 0)
        self.assertEqual(required_vps(simulation), {30, 31})
        start = simulation.items_at(1.0)[0]
        self.assertEqual((start.vp, round(start.position[1])), (30, -200))
        missing = transition_simulation([((99, 0, 0, 0),)], catalog)
        self.assertTrue(any("99" in note for note in missing.notes))

    def test_names_may_repeat_between_models_and_pieces(self):
        text = SAMPLE.replace("begin_model core", "begin_model CORE")
        self.assertIsNotNone(parse_sector_text(text).model("core"))


def test_sector_visual_choices_follow_cell_states_and_exact_paths(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from map_editor import bootstrap
    from studio.assets import catalog

    folder = tmp_path / "Models" / "Sectors" / "Post"
    folder.mkdir(parents=True)
    ground = folder / "ground" / "post.BASE"
    intact = tmp_path / "Models" / "Shared" / "post.BASE"
    for path in (ground, intact):
        path.parent.mkdir()
        path.touch()
    sector_file = folder / "post.sector"
    sector_file.write_text(SAMPLE.replace("ST_EMPTY.base", "GROUND/post.base")
                           .replace("Kyt_Flak_Normal.BASE", "data/models/shared/POST.base")
                           .replace("core|core|core|ground", "core|vp|named|ground")
                           + "begin_model vp\nvisual = 0\nend\n"
                             "begin_model named\nvisual = elsewhere/ARCHIVE.BASE\nend\n",
                           encoding="utf-8")
    archive = tmp_path / "Set.BAS"
    set_model = SimpleNamespace(base="Archive.BASE")
    sdf = SimpleNamespace(models=[set_model], sectors={}, buildings=[])
    assets = SimpleNamespace(
        sdf=sdf, setbas_path=archive, visual_proto_names=("VP.BASE",),
        _key=str.casefold, _loose_families={},
        base_object=lambda name, *, set_only: object() if set_only else None,
    )
    monkeypatch.setenv("NME_GAME_DATA", str(tmp_path))
    monkeypatch.setattr(bootstrap, "_installation", None)
    monkeypatch.setattr(catalog, "set_assets", lambda number: assets)
    building = catalog.GameObject("building", 1, tmp_path / "Buildings.cfg",
                                  {"sec_type": "Data/Models/Sectors/Post/post.sector|52"})

    choices = {choice.label: choice for choice in catalog.visual_choices(building, 1)}
    assert choices["Sector slot 1 — Intact"].path == ground
    assert choices["Sector slot 5 — Intact"].path == intact
    assert choices["Sector slot 5 — Damaged"].base_name == "VP.BASE"
    assert choices["Sector slot 5 — Heavy"].base_name == "Archive.BASE"
    assert choices["Sector slot 5 — Flat"].path == ground
    intact.unlink()
    assert catalog.visual_choices(building, 1) == []


@unittest.skipUnless(HAVE_DATA, "game data with Set3 and Scripts not found")
class SectorEditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.saved_env = {key: os.environ.get(key) for key in ("ONUA_DATA", "NME_GAME_DATA")}
        cls.root = Path(tempfile.mkdtemp(prefix="onua-sector-test-"))
        cls.data = cls.root / "Data"
        for part in ("Sets/Set3", "Scripts"):
            shutil.copytree(REAL_DATA / part, cls.data / part)
        (cls.data / "Models" / "Sectors").mkdir(parents=True)
        os.environ["ONUA_DATA"] = os.environ["NME_GAME_DATA"] = str(cls.data)
        from studio_mcp import sets
        sets._prepare()

    @classmethod
    def tearDownClass(cls):
        for key, value in cls.saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(cls.root, ignore_errors=True)

    def doc(self):
        from sector_editor.document import SetDocument
        return SetDocument.open(self.data / "Sets" / "Set3")

    def own_sector(self, name):
        folder = self.data / "Models" / "Sectors" / name
        folder.mkdir()
        for file in ("Kyt_Flak_Normal.BASE", "KYTFLKN1.sklt"):
            shutil.copy2(REAL_DATA / OWN_MODEL / file, folder / file)
        (folder / f"{name}.sector").write_text(SAMPLE, encoding="utf-8")
        return folder

    def test_vanilla_sector_exports_names_only(self):
        doc = self.doc()
        target = self.data / "Models" / "Sectors" / "City5"
        path = doc.export_external_sector(5, target)
        self.assertEqual([p.name for p in target.iterdir()], ["City5.sector"])
        sector = read_sector(path)
        original = doc.sdf.sectors[5]
        self.assertEqual(len(sector.cells), len(original.subsects))
        self.assertTrue(all(not m.visual.casefold().endswith(".base") or
                            doc.assets._by_name.get(doc.assets._key(m.visual)) for m in sector.models))
        # Imported back, it rebuilds the same cells from the set's own rows.
        slot = doc.import_external_sector(target)
        self.assertNotEqual(slot, 5)
        self.assertEqual(doc.sdf.sectors[slot].subsects, original.subsects)
        self.assertEqual(doc.external_own, set())
        # Edited with a changed shield, then wiped: the helper rows never reach SET.SDF.
        before = doc.sdf.path.read_bytes()
        doc.wipe_sectors([slot])          # the first copy uses only set rows: a normal sector
        changed = re.sub(r"    shield = \d+", "    shield = 99", path.read_text(encoding="utf-8"), count=1)
        (target / "City5.sector").write_text(changed, encoding="utf-8")
        slot = doc.import_external_sector(target)
        self.assertTrue(doc.external_models)
        doc.wipe_sectors([slot])
        doc.save()
        self.assertEqual(doc.sdf.path.read_bytes(), before)

    def test_own_models_round_trip_and_save_guard(self):
        from studio.exchange.onua_archive import import_sector_onua, is_sector_archive
        doc = self.doc()
        sdf_before = doc.sdf.path.read_bytes()
        slot = doc.import_external_sector(self.own_sector("Flak_Post"))
        self.assertTrue(doc.external_own)
        self.assertEqual(doc.resource_issues(), [])
        with self.assertRaises(ValueError):
            doc.save()

        copy = self.data / "Models" / "Sectors" / "Flak_Post_Copy"
        doc.export_external_sector(slot, copy)
        names = {p.name.casefold() for p in copy.iterdir()}
        self.assertTrue({"flak_post_copy.sector", "kyt_flak_normal.base", "kytflkn1.sklt"} <= names)
        exported = read_sector(copy / "Flak_Post_Copy.sector")
        core_model = next(m for m in exported.models if m.visual.casefold() == "kyt_flak_normal.base")
        self.assertEqual((core_model.shield, core_model.explosions), (40, ((49, 0, 0, 100),)))
        ground = next(m for m in exported.models if m is not core_model)
        self.assertEqual(ground.visual.casefold(), "st_empty.base")   # SET model: name only

        archive = self.root / "Flak_Post.onua"
        doc.export_external_sector(slot, archive)
        self.assertTrue(is_sector_archive(archive))
        unpacked = import_sector_onua(archive, self.root / "unpacked")
        self.assertEqual(read_sector(unpacked).cells, exported.cells)

        doc.wipe_sectors([slot])
        doc.save()
        self.assertEqual(doc.sdf.path.read_bytes(), sdf_before)

    def test_map_icons_generate_export_and_reimport(self):
        from PySide6.QtWidgets import QApplication
        from sector_editor.visual_aids import map_zoom_image
        QApplication.instance() or QApplication([])
        doc = self.doc()
        slot = doc.import_external_sector(self.own_sector("Icon_Post"))
        self.assertGreater(doc.generate_map_icons(slot), 0)
        own = next(iter(doc.external_own))
        self.assertEqual(len(doc.map_icons[("model", own)]), 3)
        # The small test turret vanishes at 4/8 px per sector: load the sector icon by hand.
        doc.set_map_icon("sector", slot, doc.map_icons[("model", own)][:2])
        self.assertEqual(len(doc.map_icons[("sector", slot)]), 2)
        for zoom, size in enumerate((4, 8, 12, 24, 48)):
            image = map_zoom_image(doc, slot, 0, zoom)
            self.assertEqual((image.width(), image.height()), (size, size))
        target = self.data / "Models" / "Sectors" / "Icon_Post_Out"
        doc.export_external_sector(slot, target)
        exported = read_sector(target / "Icon_Post_Out.sector")
        self.assertEqual(len(exported.map_icon), 2)
        icons = [m.map_icon for m in exported.models if m.map_icon]
        self.assertEqual([len(i) for i in icons], [3])
        self.assertTrue(all((target / f).is_file() for f in (*exported.map_icon, *icons[0])))
        again = self.doc()
        slot = again.import_external_sector(target)
        self.assertIn(("sector", slot), again.map_icons)
        self.assertTrue(any(key[0] == "model" for key in again.map_icons))

    def test_mcp_edit_flow_leaves_the_set_unchanged(self):
        from studio_mcp import sets
        self.own_sector("Mcp_Post")
        sdf = sets.open_document("set3").sdf.path
        before = sdf.read_bytes()
        steps = [{"op": "import_external_sector", "source": "Data/Models/Sectors/Mcp_Post", "save_as": "s"},
                 {"op": "shift_state", "sector": {"named": "s"}, "cells": [0], "state": 3}]
        with self.assertRaises(core.McpToolError):        # own models still used: nothing is written
            sets.run("set3", steps)
        self.assertEqual(sdf.read_bytes(), before)
        result = sets.run("set3", steps + [
            {"op": "export_external_sector", "sector": {"named": "s"}, "name": "Mcp_Post_V2", "archive": True},
            {"op": "wipe_sectors", "sectors": [{"named": "s"}]}])
        self.assertEqual(result["results"][2]["sector_file"], "Data/Models/Sectors/Mcp_Post_V2/Mcp_Post_V2.sector")
        self.assertTrue((self.data / "Models" / "Sectors" / "Mcp_Post_V2.onua").is_file())
        self.assertEqual(sdf.read_bytes(), before)
        sets.undo("set3")
        self.assertEqual(sdf.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
