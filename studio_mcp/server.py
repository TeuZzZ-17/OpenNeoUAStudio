"""OpenNeoUA Studio MCP server: model inspection, rendering and editing for AI assistants.

Start: python -m studio_mcp.server   (stdio transport)
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

try:  # mcp 2.x
    from mcp.server.mcpserver import Image, MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server, Image

from PIL import Image as PilImage, ImageDraw

from studio_mcp import core

INSTRUCTIONS = """OpenNeoUA Studio tools for Urban Assault models.
Sources: 'set1:77' (VP number from studio_list_vp), 'set1:VPwGLID2' (object name) or a path to a .BASE file
(absolute, or relative to the game Data folder). Coordinates: x right, y DOWN (negative = up), z forward.
Typical flow: studio_list_vp -> studio_render_model -> studio_export_model -> studio_transform_points /
studio_set_points -> studio_render_model -> studio_validate_model. Writes are limited to Data/Models."""

mcp = _Server("openneoua-studio", instructions=INSTRUCTIONS)


def _json(value) -> str:
    return json.dumps(value, indent=1, ensure_ascii=False)


def _guard(function, *args, **kwargs) -> str:
    """Run a tool body and turn expected errors into a readable message."""
    try:
        return _json(function(*args, **kwargs))
    except core.McpToolError as exc:
        return "ERROR: " + str(exc)


@mcp.tool()
def studio_status() -> str:
    """Show the game Data folder, the sets found and where writes are allowed."""
    return _guard(lambda: {"data_root": str(core.data_root()), "sets": core.list_sets(),
                           "writable_roots": [str(p) for p in core.writable_roots()]})


@mcp.tool()
def studio_list_vp(set_name: str = "set1", text_filter: str = "") -> str:
    """List the visual prototypes (VP numbers) of a set, optionally filtered by text (e.g. 'GLID')."""
    return _guard(lambda: core.list_vp(set_name, text_filter))


@mcp.tool()
def studio_describe_model(source: str, set_hint: str = "") -> str:
    """Summarize a model: objects, point/polygon counts, size, textures and material blocks."""
    return _guard(lambda: core.describe(core.resolve_source(source, set_hint)))


@mcp.tool()
def studio_get_geometry(source: str, owner_path: str = "", set_hint: str = "") -> str:
    """Return all points and polygons (with their material block) of a model's skeleton."""
    return _guard(lambda: core.geometry(core.resolve_source(source, set_hint), owner_path))


@mcp.tool()
def studio_render_model(source: str, views: list[str] | None = None, width: int = 480, height: int = 360,
                        zoom_percent: int = 100, background: str = "#1f2228", set_hint: str = "") -> list:
    """Render a model with Studio's own textured Retail renderer. Views: iso, iso_left, iso_back, front,
    back, left, right, top, bottom. Several views come back as one labelled contact sheet."""
    views = views or ["iso"]
    if not 64 <= width <= 1600 or not 64 <= height <= 1600:
        return ["ERROR: width and height must be between 64 and 1600"]
    out_dir = Path(tempfile.gettempdir()) / "onua_studio_mcp" / f"render_{uuid.uuid4().hex[:8]}"
    request = {"spec": source, "views": views, "size": [width, height], "zoom": zoom_percent,
               "background": background, "out_dir": str(out_dir), "set_hint": set_hint}
    request_file = out_dir.with_suffix(".json")
    request_file.parent.mkdir(parents=True, exist_ok=True)
    request_file.write_text(json.dumps(request), encoding="utf-8")
    try:
        process = subprocess.run(
            [sys.executable, "-m", "studio_mcp.render_worker", str(request_file)],
            cwd=str(core.STUDIO_ROOT), capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return ["ERROR: render timed out after 180 s"]
    line = next((ln for ln in process.stdout.splitlines() if ln.startswith("RESULT_JSON ")), None)
    if line is None:
        return ["ERROR: render worker failed:\n" + (process.stderr or process.stdout)[-1500:]]
    result = json.loads(line[len("RESULT_JSON "):])
    if not result["ok"]:
        return ["ERROR: " + result.get("error", "unknown render error")]
    sheet = _contact_sheet(result["images"], width, height)
    note = f"Rendered {source} ({result.get('renderer', '')}), views: {', '.join(i['view'] for i in result['images'])}"
    if result["warnings"]:
        note += "\nWarnings: " + "; ".join(result["warnings"][:4])
    return [note, Image(data=sheet, format="png")]


def _contact_sheet(images: list[dict], width: int, height: int) -> bytes:
    tiles = [PilImage.open(item["path"]).convert("RGBA") for item in images]
    columns = 1 if len(tiles) == 1 else 2
    rows = (len(tiles) + columns - 1) // columns
    sheet = PilImage.new("RGBA", (columns * width, rows * height), (31, 34, 40, 255))
    for index, (tile, item) in enumerate(zip(tiles, images)):
        x, y = (index % columns) * width, (index // columns) * height
        sheet.alpha_composite(tile, (x, y))
        if len(tiles) > 1:
            ImageDraw.Draw(sheet).text((x + 6, y + 4), item["view"], fill=(230, 230, 230, 255))
    buffer = io.BytesIO()
    sheet.convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()


@mcp.tool()
def studio_export_model(source: str, out_dir: str, base_name: str, skeleton_stem: str) -> str:
    """Copy a Set.BAS model (e.g. 'set1:VPwGLID2') into a loose BASE + skeleton under Data/Models, so it can be
    edited and used with base_dead/base_wait/... in Vehicles.cfg. skeleton_stem must have the same number of
    characters as the original skeleton name (VPWGLID2 -> VPKGLID2) and differ from it."""
    return _guard(lambda: core.export_model(core.resolve_source(source), out_dir, base_name, skeleton_stem))


@mcp.tool()
def studio_set_points(skeleton: str, points: list[list[float]]) -> str:
    """Overwrite every point ([x, y, z]) of a loose .sklt (or the folder holding one). The point count and the
    polygons stay the same, so the texture mapping remains valid."""
    return _guard(core.set_points, skeleton, points)


@mcp.tool()
def studio_transform_points(skeleton: str, point_indices: list[int] | None = None,
                            rotate_deg: list[float] = (0, 0, 0), scale: list[float] = (1, 1, 1),
                            translate: list[float] = (0, 0, 0), pivot: list[float] = (0, 0, 0),
                            jitter: float = 0.0, seed: int = 0) -> str:
    """Scale, rotate (degrees around X, Y, Z about pivot), translate and jitter chosen points of a loose .sklt
    (all points when point_indices is empty). Use it to bend, snap or crumple parts without sending coordinates."""
    return _guard(core.transform_points, skeleton, point_indices, rotate_deg, scale, translate, pivot, jitter, seed)


@mcp.tool()
def studio_validate_model(source: str, set_hint: str = "") -> str:
    """Check a model: loads, skeleton found, every polygon has a material, no broken point references."""
    return _guard(lambda: core.validate(core.resolve_source(source, set_hint)))


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
