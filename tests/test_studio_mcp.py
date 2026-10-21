"""Studio MCP server: pure helpers, modeling operations on a real exported model, and a stdio round trip."""

import asyncio
import json
import os
import shutil
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

STUDIO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STUDIO))

from studio_mcp import analysis, core, edit, guide, history, meshmath, shapes, uvs, wire  # noqa: E402
from studio_mcp.loose import LooseModel  # noqa: E402
from studio_mcp.select import MeshView, select_points, select_polygons  # noqa: E402

try:
    core.data_root()
    HAVE_DATA = bool(core.list_sets())
except core.McpToolError:
    HAVE_DATA = False


class PureHelperTests(unittest.TestCase):
    """No game data needed."""

    def setUp(self):
        # a flat square on the floor (y = 0) made of two triangles, plus one wall
        self.view = MeshView(
            points=[(-1, 0, -1), (1, 0, -1), (1, 0, 1), (-1, 0, 1), (0, -2, 0)],
            polygons=[[0, 1, 2], [0, 2, 3], [0, 1, 4]], poly_block={0: 1, 1: 1, 2: 2})

    def test_point_selectors(self):
        self.assertEqual(select_points({"x": [0, None]}, self.view), [1, 2, 4])
        self.assertEqual(select_points({"box": {"min": [None, -3, None], "max": [None, -1, None]}}, self.view), [4])
        self.assertEqual(select_points({"block": 2}, self.view), [0, 1, 4])
        self.assertEqual(select_points({"nearest": {"to": [1, 0, 1], "count": 1}}, self.view), [2])
        self.assertEqual(select_points({"not": {"x": [0, None]}, "invert": False}, self.view), [0, 3])
        self.assertEqual(select_points({"or": [[0], [3]]}, self.view), [0, 3])
        with self.assertRaises(core.McpToolError):
            select_points({"bogus": 1}, self.view)
        with self.assertRaises(core.McpToolError):
            select_points([99], self.view)

    def test_polygon_selectors(self):
        self.assertEqual(select_polygons({"block": 1}, self.view), [0, 1])
        self.assertEqual(select_polygons({"points_any": [4]}, self.view), [2])
        self.assertEqual(select_polygons({"connected_to": 0}, self.view), [0, 1, 2])
        self.assertEqual(select_polygons({"vertices": 3, "invert": True}, self.view), [])

    def test_facing_follows_ua_winding(self):
        # UA's visible side is the NEGATIVE Newell normal: the floor triangle [0,1,2] faces down (+y) or up (-y)
        normal = meshmath.outward_normal([self.view.points[i] for i in (0, 1, 2)])
        up = select_polygons({"facing": "up"}, self.view)
        down = select_polygons({"facing": "down"}, self.view)
        self.assertEqual(normal[1] < 0, 0 in up)
        self.assertEqual(normal[1] > 0, 0 in down)

    def test_shapes_face_outward_and_are_closed(self):
        for name, kwargs in (("box", {}), ("wedge", {}), ("prism", {"segments": 6}), ("sphere", {"segments": 8, "rings": 4}),
                             ("prism", {"segments": 8, "top_scale": 0.0})):
            points, polygons = shapes.make(name, size=(4, 6, 8), **kwargs)
            center = meshmath.centroid(points)
            for ring in polygons:
                pts = [points[i] for i in ring]
                normal = meshmath.outward_normal(pts)
                self.assertGreater(meshmath.dot(normal, meshmath.sub(meshmath.centroid(pts), center)), -1e-6, name)
        points, polygons = shapes.make("box", size=(2, 2, 2))
        self.assertEqual((len(points), len(polygons)), (8, 6))
        points, polygons = shapes.make("box", size=(2, 2, 2), taper=0.0)   # pyramid: apex merged
        self.assertEqual((len(points), len(polygons)), (5, 5))
        with self.assertRaises(core.McpToolError):
            shapes.make("teapot")

    def test_uv_modes(self):
        square = [(0, 0, 0), (10, 0, 0), (10, 0, 10), (0, 0, 10)]
        planar = uvs.compute({"mode": "planar"}, square)
        self.assertEqual((min(u for u, _ in planar), max(u for u, _ in planar)), (16, 240))
        rect = uvs.compute({"mode": "rect", "rect": [0, 0, 100, 50]}, square)
        self.assertEqual((max(u for u, _ in rect), max(v for _, v in rect)), (100, 50))
        copy = uvs.compute({"mode": "copy"}, square, reference=[(1, 2), (3, 4), (5, 6), (7, 8)])
        self.assertEqual(copy, [(1, 2), (3, 4), (5, 6), (7, 8)])
        moved = uvs.adjust([(10, 10), (20, 10), (20, 20)], offset=(5, 0), flip_u=True)
        self.assertEqual(sorted(u for u, _ in moved), [15, 15, 25])
        with self.assertRaises(core.McpToolError):
            uvs.compute({"mode": "explicit"}, square)

    def test_guide_lists_every_operation(self):
        text = guide.help_text("ops")
        for name in edit.OPS:
            self.assertIn(name, text)
        self.assertIn("Unknown topic", guide.help_text("nonsense"))


@unittest.skipUnless(HAVE_DATA, "game Data folder with sets not available")
class CoreTests(unittest.TestCase):
    def setUp(self):
        self.out = core.work_dir() / "test_core"
        shutil.rmtree(self.out, ignore_errors=True)

    def export(self, source="set1:VPwGLID2", stem="VPKGLID2"):
        shutil.rmtree(self.out, ignore_errors=True)
        return core.export_model(core.resolve_source(source), str(self.out), "Probe", stem)["base_file"]

    def run_ops(self, base, *operations, **kwargs):
        return edit.run(base, list(operations), **kwargs)

    def test_vp_listing_and_describe(self):
        rows = core.list_vp("set1", "GLID2")
        self.assertIn(77, [r["vp"] for r in rows])
        info = core.describe(core.resolve_source("set1:77"))
        self.assertEqual(info["objects"][0]["points"], 41)

    def test_export_edit_and_validate_roundtrip(self):
        base = self.export()
        before = core.geometry(core.resolve_source(base))
        self.run_ops(base, {"op": "transform", "select": [11], "translate": [0, 10, 0]})
        after = core.geometry(core.resolve_source(base))
        self.assertAlmostEqual(after["points"][11][1], before["points"][11][1] + 10, places=2)
        self.assertEqual(before["points"][0], after["points"][0])
        self.assertTrue(core.validate(core.resolve_source(base))["ok"])

    def test_geometry_filters_and_uvs(self):
        base = self.export()
        part = core.geometry(core.resolve_source(base), points={"x": [0, None]}, polygons={"block": 1}, with_uvs=True)
        self.assertTrue(all("uv" in poly for poly in part["polygons"]))
        self.assertEqual(len(part["points"]), len(part["point_indices"]))

    def test_bad_requests_are_refused(self):
        with self.assertRaises(core.McpToolError):
            core.export_model(core.resolve_source("set1:VPwGLID2"), str(self.out), "Bad", "SHORT")
        with self.assertRaises(core.McpToolError):
            core.ensure_writable(core.data_root() / "Scripts" / "x.cfg")
        base = self.export()
        with self.assertRaises(core.McpToolError):
            self.run_ops(base, {"op": "set_points", "all_points": [[0, 0, 0]]})
        with self.assertRaises(core.McpToolError):
            self.run_ops(base, {"op": "transform", "select": [999]})
        with self.assertRaises(core.McpToolError):
            self.run_ops(base, {"op": "transform", "bogus": 1})

    def test_failed_batch_writes_nothing(self):
        base = self.export()
        sklt = next(Path(base).parent.glob("*.sklt"))
        digests = (core.file_digest(Path(base)), core.file_digest(sklt))
        with self.assertRaises(core.McpToolError):
            self.run_ops(base, {"op": "transform", "select": "all", "translate": [1, 1, 1]},
                         {"op": "delete_polygons", "select": [9999]})
        self.assertEqual(digests, (core.file_digest(Path(base)), core.file_digest(sklt)))
        dry = self.run_ops(base, {"op": "transform", "select": "all", "translate": [1, 1, 1]}, dry_run=True)
        self.assertFalse(dry["written"])
        self.assertEqual(digests, (core.file_digest(Path(base)), core.file_digest(sklt)))

    def test_primitive_extrude_mirror_flip_roundtrip(self):
        base = self.export()
        start = LooseModel.open(base, writable=False)
        points0, polys0 = len(start.points), len(start.polygons)
        result = self.run_ops(
            base,
            {"op": "create_primitive", "shape": "box", "center": [0, -12, 0], "size": [20, 6, 30],
             "material": {"like_polygon": 0}, "save_as": "box"},
            {"op": "extrude", "select": {"named": "box", "facing": "up"}, "distance": 4, "scale": 0.5, "save_as": "tower"},
            {"op": "mirror", "select": {"named": "tower"}, "axis": "x", "plane": 15},
        )
        self.assertTrue(result["written"])
        loaded = LooseModel.open(base, writable=False)
        self.assertGreater(len(loaded.polygons), polys0 + 6)
        self.assertGreater(len(loaded.points), points0 + 8)
        report = analysis.analyze(core.resolve_source(base))
        self.assertEqual(report["problems"], [])
        self.assertTrue(core.validate(core.resolve_source(base))["ok"])
        # the box was built outward-facing: no inside-out closed solid reported
        flipped = self.run_ops(base, {"op": "flip", "select": {"facing": "up", "y": [None, -13]}})
        self.assertGreaterEqual(flipped["results"][0]["flipped"], 1)

    def test_weld_subdivide_delete_cleanup(self):
        base = self.export()
        self.run_ops(base, {"op": "create_primitive", "shape": "box", "size": [10, 10, 10], "center": [0, -20, 0],
                            "material": {"like_polygon": 0}},
                     {"op": "create_primitive", "shape": "box", "size": [10, 10, 10], "center": [10, -20, 0],
                      "material": {"like_polygon": 0}})
        welded = self.run_ops(base, {"op": "weld", "select": "all", "tolerance": 0.01})
        self.assertGreater(welded["results"][0]["merged"], 0)
        before = len(LooseModel.open(base, writable=False).polygons)
        sub = self.run_ops(base, {"op": "subdivide", "select": {"facing": "up", "y": [None, -24]}})
        self.assertGreater(len(sub["results"][0]["polygons"]), 1)
        self.assertGreater(len(LooseModel.open(base, writable=False).polygons), before)
        self.run_ops(base, {"op": "delete_polygons", "select": {"y": [None, -24], "facing": "up"}})
        self.run_ops(base, {"op": "cleanup"})
        self.assertTrue(core.validate(core.resolve_source(base))["ok"])

    def test_undo_redo_history(self):
        base = self.export()
        original = core.geometry(core.resolve_source(base))["points"]
        self.run_ops(base, {"op": "transform", "select": "all", "translate": [5, 0, 0]})
        self.run_ops(base, {"op": "create_primitive", "shape": "box", "material": {"like_polygon": 0}})
        self.assertEqual([h["label"] for h in history.listing(Path(base))["undo"]], ["transform", "create_primitive"])
        history.undo(Path(base), 2)
        self.assertEqual(core.geometry(core.resolve_source(base))["points"], original)
        history.redo(Path(base), 1)
        moved = core.geometry(core.resolve_source(base))["points"]
        self.assertAlmostEqual(moved[0][0], original[0][0] + 5, places=2)
        with self.assertRaises(core.McpToolError):
            history.undo(Path(self.export()), 1)

    def test_symmetric_move_and_selectors(self):
        base = self.export()
        view = core.geometry(core.resolve_source(base))
        points = view["points"]
        right = [i for i, p in enumerate(points) if p[0] > 5]
        result = self.run_ops(base, {"op": "transform", "select": right[:1], "translate": [0, 7, 0], "mirror": "x"})
        self.assertTrue(result["results"][0]["followed"], "the mirror counterpart should follow")
        mirrored = core.geometry(core.resolve_source(base), points={"mirror_of": {"points": right[:1], "axes": "x"}})
        self.assertTrue(mirrored["point_indices"])

    def test_materials_textures_and_blocks(self):
        base = self.export()
        listing = analysis.materials(core.resolve_source(base))
        self.assertTrue(any(row["takes_new_polygons"] for row in listing["blocks"]))
        textures = analysis.textures(core.resolve_source(base), "BODEN")
        self.assertTrue(any(t["name"].upper().startswith("BODEN") for t in textures["textures"]))
        added = self.run_ops(base, {"op": "add_material_block", "from_block": 1})
        block = added["results"][0]["block"]
        self.run_ops(base, {"op": "set_texture", "select": [0, 1], "texture": "BODEN2.ILBM"})
        names = {row["texture"] for row in analysis.materials(core.resolve_source(base))["blocks"]}
        self.assertIn("BODEN2.ILBM", names)
        self.run_ops(base, {"op": "delete_material_block", "block": block})
        self.assertTrue(core.validate(core.resolve_source(base))["ok"])
        with self.assertRaises(core.McpToolError):
            self.run_ops(base, {"op": "delete_material_block", "block": 1})   # still has polygons

    def test_import_geometry_effects_and_emitters(self):
        base = self.export()
        imported = self.run_ops(base, {"op": "import_geometry", "source": "set1:30", "exclude_fx": True,
                                       "translate": [0, -25, 0], "scale": 0.5})
        self.assertGreater(len(imported["results"][0]["polygons"]), 3)
        fx = self.run_ops(base, {"op": "add_fx", "source": "set1:12", "fx": 0, "translate": [0, 5, 0]})
        self.assertTrue(fx["results"][0]["polygons"])
        emitter = self.run_ops(base, {"op": "add_emitter", "source": "set1:14", "source_block": 0, "point": 2})
        block = emitter["results"][0]["block"]
        report = analysis.effects(core.resolve_source(base))
        self.assertIn(block, [e["block"] for e in report["emitters"]])
        self.assertTrue(report["fx_elements"])
        # emitters follow their point when an edit renumbers points
        anchor = core.geometry(core.resolve_source(base))["points"][2]
        self.run_ops(base, {"op": "delete_polygons", "select": {"points_any": [0]}})
        moved = [e for e in analysis.effects(core.resolve_source(base))["emitters"] if e["block"] >= 0][-1]
        self.assertEqual(core.geometry(core.resolve_source(base))["points"][moved["point"]], anchor)
        block = moved["block"]
        self.run_ops(base, {"op": "move_emitter", "block": block, "point": 3})
        self.run_ops(base, {"op": "delete_emitter", "block": block})
        self.run_ops(base, {"op": "delete_fx", "fx": 0})
        self.assertTrue(core.validate(core.resolve_source(base))["ok"])

    def test_compare_models(self):
        base = self.export()
        self.run_ops(base, {"op": "transform", "select": [3], "translate": [0, 4, 0]})
        diff = analysis.compare(core.resolve_source("set1:VPwGLID2"), core.resolve_source(base))
        self.assertTrue(diff["same_topology"])
        self.assertEqual(diff["moved_points"], 1)
        base = self.export()
        self.run_ops(base, {"op": "create_primitive", "shape": "box", "material": {"like_polygon": 0}})
        diff = analysis.compare(core.resolve_source("set1:VPwGLID2"), core.resolve_source(base))
        self.assertFalse(diff["same_topology"])
        self.assertEqual(diff["polygons_only_in_b"]["count"], 6)

    def test_fit_center_symmetrize(self):
        base = self.export()
        self.run_ops(base, {"op": "fit", "size": [None, None, 100], "uniform": True, "recenter": "xz"})
        box = core.geometry(core.resolve_source(base))["bbox"]
        self.assertAlmostEqual(box["size"][2], 100, delta=0.5)
        self.run_ops(base, {"op": "center_at_origin"})
        box = core.geometry(core.resolve_source(base))["bbox"]
        self.assertAlmostEqual(box["min"][0] + box["max"][0], 0, delta=0.5)
        self.run_ops(base, {"op": "symmetrize", "axis": "x", "source": "positive", "tolerance": 2})

    def test_add_polygons_uv_snap_smooth_and_material_moves(self):
        base = self.export()
        added = self.run_ops(base, {
            "op": "add_polygons", "polygons": [[[0, -30, 0], [10, -30, 0], [10, -30, 10], [0, -30, 10]]],
            "material": {"like_polygon": 0}, "uv": {"mode": "world", "texels_per_unit": 4}})
        pid = added["results"][0]["polygons"][0]
        uv = core.geometry(core.resolve_source(base), polygons=[pid], with_uvs=True)["polygons"][0]["uv"]
        self.assertEqual(len(uv), 4)
        self.run_ops(base, {"op": "set_uvs", "select": [pid], "mode": "adjust", "offset": [3, 0], "flip_u": True},
                     {"op": "set_uvs", "select": [pid], "mode": "rect", "rect": [20, 20, 60, 60]})
        uv = core.geometry(core.resolve_source(base), polygons=[pid], with_uvs=True)["polygons"][0]["uv"]
        self.assertEqual((min(u for u, _ in uv), max(u for u, _ in uv)), (20, 60))
        self.run_ops(base, {"op": "set_material", "select": [pid], "block": 2, "atts": {"shade": 40}})
        self.assertEqual(core.geometry(core.resolve_source(base), polygons=[pid])["polygons"][0]["block"], 2)
        self.run_ops(base, {"op": "snap", "select": "all", "mode": "grid", "step": 5},
                     {"op": "smooth", "select": {"y": [None, -25]}, "iterations": 2})
        self.assertTrue(core.validate(core.resolve_source(base))["ok"])
        with self.assertRaises(core.McpToolError):
            self.run_ops(base, {"op": "map_polygons"})   # nothing is unmapped
        with self.assertRaises(core.McpToolError):
            self.run_ops(base, {"op": "add_polygons", "polygons": [[0, 1, 2]]})   # which material?

    def test_clear_then_build_new_model_and_genesis(self):
        base = self.export()
        self.run_ops(base, {"op": "clear"},
                     {"op": "create_primitive", "shape": "prism", "segments": 6, "size": [30, 10, 30],
                      "material": {"block": 1}, "uv": {"mode": "planar"}},
                     {"op": "create_primitive", "shape": "box", "center": [0, -8, 0], "size": [10, 6, 10],
                      "taper": 0.5, "material": {"block": 1}})
        loaded = LooseModel.open(base, writable=False)
        self.assertGreater(len(loaded.polygons), 10)
        self.assertEqual(analysis.analyze(core.resolve_source(base))["problems"], [])
        self.run_ops(base, {"op": "generate_genesis"})
        self.assertTrue(core.validate(core.resolve_source(base))["ok"])
        self.assertTrue(all(row["class"] != "amesh.class" or row["polygons"] == 0
                            for row in analysis.materials(core.resolve_source(base))["blocks"]))

    def test_emitters_survive_clear_and_renumbering(self):
        base = self.export("set1:2", "VPKBRGR1")       # a dead model: polygons plus three emitters on points 3, 7, 15
        source = core.resolve_source(base)
        places = [core.geometry(source)["points"][e["point"]] for e in analysis.effects(source)["emitters"]]
        self.assertEqual(len(places), 3)
        self.run_ops(base, {"op": "clear"})
        after = analysis.effects(core.resolve_source(base))["emitters"]
        self.assertEqual([core.geometry(core.resolve_source(base))["points"][e["point"]] for e in after], places)
        self.assertEqual(len(core.geometry(core.resolve_source(base))["polygons"]), 0)

    def test_selectors_on_real_model(self):
        base = self.export()
        source = core.resolve_source(base)
        up = core.geometry(source, polygons={"facing": "up"})["polygons"]
        down = core.geometry(source, polygons={"facing": "down"})["polygons"]
        self.assertTrue({p["id"] for p in up}.isdisjoint({p["id"] for p in down}))
        left = core.geometry(source, polygons={"mirror_of": {"polygons": {"x": [10, None]}, "axes": "x"}})
        self.assertIsInstance(left["polygons"], list)
        self.assertEqual(core.geometry(source, polygons={"unmapped": True})["polygons"], [])


class WireframeTests(unittest.TestCase):
    def setUp(self):
        self.dir = core.work_dir() / "test_wire"
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir(parents=True)

    def test_new_edit_undo_render(self):
        path = str(self.dir / "NEW.SKL")
        wire.new(path)
        result = wire.run(path, [
            {"op": "add_shape", "shape": "triangle", "center": [0, 0], "radius": 100, "save_as": "hull"},
            {"op": "add_points", "points": [[0, -150]]},
            {"op": "add_polygons", "polygons": [[0, 3]]},
            {"op": "transform", "select": {"named": "hull"}, "translate": [10, 5], "mirror": False},
            {"op": "mirror_copy", "select": [0, 1, 2], "axis": "z"},
            {"op": "align", "select": [0, 1], "axis": "z"},
        ])
        self.assertTrue(result["written"])
        data = wire.get(path)
        self.assertEqual(data["mode"], "poo2")
        self.assertGreaterEqual(data["polygons"], 3)
        self.assertEqual(wire.render_png(path)[1:4], b"PNG")
        history.undo(Path(path), 1)
        self.assertLess(wire.get(path)["points"], 7)
        with self.assertRaises(core.McpToolError):
            wire.run(path, [{"op": "add_polygons", "polygons": [[0, 99]]}])
        with self.assertRaises(core.McpToolError):
            wire.run(path, [{"op": "nonsense"}])

    @unittest.skipUnless(HAVE_DATA, "game Data folder not available")
    def test_edit_existing_wireless_file(self):
        copy = self.dir / "ARROW.SKL"
        shutil.copy(core.data_root() / "Wireless" / "ARROW.SKL", copy)
        before = wire.get(str(copy))
        wire.run(str(copy), [{"op": "add_shape", "shape": "square", "center": [0, 0], "radius": 30}])
        after = wire.get(str(copy))
        self.assertEqual(after["points"], before["points"] + 4)
        history.undo(copy, 1)
        self.assertEqual(wire.get(str(copy))["points"], before["points"])


@unittest.skipUnless(HAVE_DATA, "game Data folder with sets not available")
class ServerRoundTripTests(unittest.TestCase):
    def test_stdio_tools(self):
        from mcp.client.stdio import StdioServerParameters, stdio_client
        from mcp.client import ClientSession

        out = core.work_dir() / "test_server"
        shutil.rmtree(out, ignore_errors=True)
        base = core.export_model(core.resolve_source("set1:VPwGLID2"), str(out), "Probe", "VPKGLID2")["base_file"]

        async def run():
            params = StdioServerParameters(command=sys.executable, args=["-m", "studio_mcp.server"], cwd=str(STUDIO))
            async with stdio_client(params) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    tools = {t.name for t in (await session.list_tools()).tools}
                    listing = await session.call_tool("studio_list_vp", {"set_name": "set1", "text_filter": "GLID2"})
                    render = await session.call_tool("studio_render_model", {
                        "source": "set1:77", "views": ["iso", "top"], "label_points": {"x": [0, 5]}, "show_wire": True})
                    helped = await session.call_tool("studio_help", {"topic": "selectors"})
                    edited = await session.call_tool("studio_extrude_polygons", {
                        "model": base, "select": {"facing": "up"}, "distance": 2.0, "dry_run": True})
                    batch = await session.call_tool("studio_edit_batch", {
                        "model": base, "operations": [{"op": "flip", "select": [0]}], "dry_run": True})
                    return tools, listing, render, helped, edited, batch

        tools, listing, render, helped, edited, batch = asyncio.run(run())
        for expected in ("studio_render_model", "studio_export_model", "studio_edit_batch", "studio_undo",
                         "studio_extrude_polygons", "studio_import_geometry", "studio_add_emitter", "studio_help"):
            self.assertIn(expected, tools)
        self.assertIn("VP_GLID2", json.dumps([c.model_dump() for c in listing.content]))
        self.assertIn("image", [c.type for c in render.content])
        self.assertIn("SELECTORS", helped.content[0].text)
        self.assertIn('"dry_run": true', edited.content[0].text)
        self.assertIn('"flip"', batch.content[0].text)


if __name__ == "__main__":
    unittest.main()
