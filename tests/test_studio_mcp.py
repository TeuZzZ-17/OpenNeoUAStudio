"""Studio MCP server: core operations plus a real stdio round trip through the MCP client."""

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

from studio_mcp import core  # noqa: E402

try:
    core.data_root()
    HAVE_DATA = bool(core.list_sets())
except core.McpToolError:
    HAVE_DATA = False


@unittest.skipUnless(HAVE_DATA, "game Data folder with sets not available")
class CoreTests(unittest.TestCase):
    def setUp(self):
        self.out = core.work_dir() / "test_core"
        shutil.rmtree(self.out, ignore_errors=True)

    def test_vp_listing_and_describe(self):
        rows = core.list_vp("set1", "GLID2")
        self.assertIn(77, [r["vp"] for r in rows])
        info = core.describe(core.resolve_source("set1:77"))
        self.assertEqual(info["objects"][0]["points"], 41)

    def test_export_edit_and_validate_roundtrip(self):
        exported = core.export_model(core.resolve_source("set1:VPwGLID2"), str(self.out), "Probe", "VPKGLID2")
        self.assertTrue(Path(exported["skeleton_file"]).exists())
        before = core.geometry(core.resolve_source(exported["base_file"]))
        core.transform_points(str(self.out), [11], translate=(0, 10, 0))
        after = core.geometry(core.resolve_source(exported["base_file"]))
        self.assertAlmostEqual(after["points"][11][1], before["points"][11][1] + 10, places=2)
        self.assertEqual(before["points"][0], after["points"][0])
        self.assertTrue(core.validate(core.resolve_source(exported["base_file"]))["ok"])

    def test_bad_requests_are_refused(self):
        with self.assertRaises(core.McpToolError):
            core.export_model(core.resolve_source("set1:VPwGLID2"), str(self.out), "Bad", "SHORT")
        with self.assertRaises(core.McpToolError):
            core.ensure_writable(core.data_root() / "Scripts" / "x.cfg")
        exported = core.export_model(core.resolve_source("set1:VPwGLID2"), str(self.out), "Probe", "VPKGLID2")
        with self.assertRaises(core.McpToolError):
            core.set_points(exported["skeleton_file"], [[0, 0, 0]])
        with self.assertRaises(core.McpToolError):
            core.transform_points(exported["skeleton_file"], [999])


@unittest.skipUnless(HAVE_DATA, "game Data folder with sets not available")
class ServerRoundTripTests(unittest.TestCase):
    def test_stdio_tools(self):
        from mcp.client.stdio import StdioServerParameters, stdio_client
        from mcp.client import ClientSession

        async def run():
            params = StdioServerParameters(command=sys.executable, args=["-m", "studio_mcp.server"], cwd=str(STUDIO))
            async with stdio_client(params) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    tools = {t.name for t in (await session.list_tools()).tools}
                    listing = await session.call_tool("studio_list_vp", {"set_name": "set1", "text_filter": "GLID2"})
                    render = await session.call_tool("studio_render_model", {"source": "set1:77", "views": ["iso", "top"]})
                    return tools, listing, render

        tools, listing, render = asyncio.run(run())
        self.assertIn("studio_render_model", tools)
        self.assertIn("studio_export_model", tools)
        self.assertIn("VP_GLID2", json.dumps([c.model_dump() for c in listing.content]))
        kinds = [c.type for c in render.content]
        self.assertIn("image", kinds)


if __name__ == "__main__":
    unittest.main()
