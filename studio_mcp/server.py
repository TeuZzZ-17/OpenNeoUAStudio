"""OpenNeoUA Studio MCP server: model inspection, rendering and editing for AI assistants.

Start: python -m studio_mcp.server   (stdio transport)

Tools come in two groups: fixed tools written here (inspect, render, export, undo ...) and one tool per
modeling operation of ``edit.OPS``, generated below so the operation list has a single source of truth.
"""

from __future__ import annotations

import inspect
import io
import json
import shutil
import subprocess
import sys
import tempfile
import traceback
import uuid
from pathlib import Path
from typing import Annotated, Any

try:  # mcp 2.x
    from mcp.server.mcpserver import Image, MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server, Image

from PIL import Image as PilImage, ImageDraw
from pydantic import Field

from studio_mcp import analysis, core, edit, guide, history, wire
from studio_mcp.loose import resolve_base
from studio_mcp.select import select_points, select_polygons, view_of_object

INSTRUCTIONS = """OpenNeoUA Studio tools for Urban Assault models (inspect, render, EDIT, undo).
FIRST call studio_help() for the workflow, conventions, selectors, UV modes and recipes.
Sources: 'set1:77' (VP number from studio_list_vp), 'set1:VPwGLID2' (object name) or a path to a .BASE file
(absolute, or relative to the game Data folder). Editing needs a loose copy: studio_export_model first.
Coordinates: x right, y DOWN (negative = up), z forward. Writes are limited to Data/Models; every edit keeps an
undo snapshot. Several edits can be chained atomically with studio_edit_batch."""

mcp = _Server("openneoua-studio", instructions=INSTRUCTIONS)

MODEL_HELP = "Loose model: the .BASE path (or a folder with exactly one .BASE); relative paths start at the game Data folder"
SELECTOR_HELP = "Selector (see studio_help('selectors')): list of indices, or a dict of conditions; omit for everything"


def _json(value) -> str:
    return json.dumps(value, indent=1, ensure_ascii=False)


def _guard(function, *args, **kwargs) -> str:
    """Run a tool body and turn errors into a readable message (a failed edit never leaves a half-written model)."""
    try:
        return _json(function(*args, **kwargs))
    except core.McpToolError as exc:
        return "ERROR: " + str(exc)
    except Exception as exc:  # unexpected: say what happened instead of killing the server
        print(traceback.format_exc(), file=sys.stderr)
        return f"ERROR (unexpected, nothing was written): {type(exc).__name__}: {exc}"


# --- guide, discovery -------------------------------------------------------------------


@mcp.tool()
def studio_help(topic: str = "start") -> str:
    """Manual for modeling with this server. Topics: start, conventions, selectors, uv, recipes, ops, all."""
    return guide.help_text(topic)


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
def studio_list_models(text_filter: str = "", limit: int = 200) -> str:
    """List the loose .BASE models under Data/Models (paths as used in the game scripts), e.g. text_filter='DEAD'."""
    return _guard(lambda: core.list_models(text_filter, limit))


# --- reading ----------------------------------------------------------------------------


@mcp.tool()
def studio_describe_model(source: str, set_hint: str = "") -> str:
    """Summarize a model: objects, point/polygon counts, size, textures and material blocks."""
    return _guard(lambda: core.describe(core.resolve_source(source, set_hint)))


@mcp.tool()
def studio_materials(source: str, set_hint: str = "") -> str:
    """List the material blocks (texture, shading, polygon count, UV box, a sample polygon to copy) and which
    blocks accept new polygons."""
    return _guard(lambda: analysis.materials(core.resolve_source(source, set_hint)))


@mcp.tool()
def studio_get_geometry(source: str, owner_path: str = "", set_hint: str = "",
                        points: Annotated[Any, Field(description=SELECTOR_HELP)] = None,
                        polygons: Annotated[Any, Field(description=SELECTOR_HELP)] = None,
                        with_uvs: bool = False, precision: int = 3) -> str:
    """Points and polygons (with their material block, optionally UVs) of a model's skeleton. Pass selectors in
    `points` / `polygons` to get only part of a big model."""
    return _guard(lambda: core.geometry(core.resolve_source(source, set_hint), owner_path, points, polygons,
                                        with_uvs, precision))


@mcp.tool()
def studio_select(source: str, points: Annotated[Any, Field(description=SELECTOR_HELP)] = None,
                  polygons: Annotated[Any, Field(description=SELECTOR_HELP)] = None, set_hint: str = "") -> str:
    """Test a selector: which point and/or polygon indices does it hit? Returns counts, indices, bounding box."""
    def run():
        obj = analysis._object(core.resolve_source(source, set_hint))
        view = view_of_object(obj)
        result = {}
        if points is not None:
            ids = select_points(points, view)
            result["points"] = {"count": len(ids), "indices": ids[:400],
                                "bbox": edit.meshmath.bbox([view.points[i] for i in ids])}
        if polygons is not None:
            ids = select_polygons(polygons, view)
            used = sorted({i for pid in ids for i in view.polygons[pid]})
            result["polygons"] = {"count": len(ids), "ids": ids[:400],
                                  "bbox": edit.meshmath.bbox([view.points[i] for i in used])}
        if not result:
            raise core.McpToolError("give `points` and/or `polygons`")
        return result
    return _guard(run)


@mcp.tool()
def studio_effects(source: str, set_hint: str = "") -> str:
    """List a model's FX elements (flames/smoke planes, with the index studio_add_fx/studio_delete_fx use),
    particle emitters (block + anchor point) and animated polygons."""
    return _guard(lambda: analysis.effects(core.resolve_source(source, set_hint)))


@mcp.tool()
def studio_list_textures(source: str, name_filter: str = "", set_hint: str = "") -> str:
    """Textures available to a model (loaded ones and every decodable bitmap of its set), for studio_set_texture."""
    return _guard(lambda: analysis.textures(core.resolve_source(source, set_hint), name_filter))


@mcp.tool()
def studio_analyze_model(source: str, set_hint: str = "") -> str:
    """Quality report: polygons without area, doubled points, inside-out solids, non-flat/non-convex polygons,
    missing materials, UV problems, symmetry. 'problems' must be fixed, 'warnings' are worth a look."""
    return _guard(lambda: analysis.analyze(core.resolve_source(source, set_hint)))


@mcp.tool()
def studio_compare_models(first: str, second: str, set_hint: str = "") -> str:
    """Compare two models (e.g. the original and your edit): size, counts, moved points or added/removed polygons."""
    return _guard(lambda: analysis.compare(core.resolve_source(first, set_hint), core.resolve_source(second, set_hint)))


@mcp.tool()
def studio_validate_model(source: str, set_hint: str = "") -> str:
    """Check a model: loads, skeleton found, every polygon has a material, no broken point references."""
    return _guard(lambda: core.validate(core.resolve_source(source, set_hint)))


@mcp.tool()
def studio_render_model(source: str, views: list[str] | None = None, width: int = 480, height: int = 360,
                        zoom_percent: int = 100, background: str = "#1f2228", set_hint: str = "",
                        highlight_polygons: Annotated[Any, Field(description="Selector of polygons to paint magenta")] = None,
                        label_points: Annotated[Any, Field(description="Selector of points to mark with their index number")] = None,
                        label_polygons: Annotated[Any, Field(description="Selector of polygons to mark with P<id>")] = None,
                        show_wire: bool = False) -> list:
    """Render a model with Studio's own textured Retail renderer. Views: iso, iso_left, iso_back, iso_back_left,
    front, back, left, right, top, bottom. Several views come back as one labelled contact sheet.
    highlight_polygons / label_points / label_polygons / show_wire draw on top so you can read indices off the image."""
    views = views or ["iso"]
    if not 64 <= width <= 1600 or not 64 <= height <= 1600:
        return ["ERROR: width and height must be between 64 and 1600"]
    out_dir = Path(tempfile.gettempdir()) / "onua_studio_mcp" / f"render_{uuid.uuid4().hex[:8]}"
    request = {"spec": source, "views": views, "size": [width, height], "zoom": zoom_percent,
               "background": background, "out_dir": str(out_dir), "set_hint": set_hint,
               "highlight_polygons": highlight_polygons, "label_points": label_points,
               "label_polygons": label_polygons, "show_wire": show_wire}
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
    sheet_file = out_dir / "sheet.png"
    sheet_file.write_bytes(sheet)
    _prune_renders(out_dir.parent)
    note = (f"Rendered {source} ({result.get('renderer', '')}), views: {', '.join(i['view'] for i in result['images'])}\n"
            f"Image file (for clients that cannot show images): {sheet_file}")
    if result["warnings"]:
        note += "\nWarnings: " + "; ".join(result["warnings"][:4])
    return [note, Image(data=sheet, format="png")]


def _prune_renders(folder: Path, keep: int = 20) -> None:
    """Keep only the newest render folders/requests in the work folder."""
    runs = sorted((p for p in folder.glob("render_*") if p.is_dir()), key=lambda p: p.stat().st_mtime)
    for old in runs[:-keep]:
        shutil.rmtree(old, ignore_errors=True)
        old.with_suffix(".json").unlink(missing_ok=True)


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


# --- making an editable copy, history -------------------------------------------------------


@mcp.tool()
def studio_export_model(source: str, out_dir: str, base_name: str, skeleton_stem: str) -> str:
    """Copy a Set.BAS model (e.g. 'set1:VPwGLID2') into a loose BASE + skeleton under Data/Models, so it can be
    edited and used with base_dead/base_wait/... in Vehicles.cfg. skeleton_stem must have the same number of
    characters as the original skeleton name (VPWGLID2 -> VPKGLID2) and differ from it."""
    return _guard(lambda: core.export_model(core.resolve_source(source), out_dir, base_name, skeleton_stem))


def _history_key(model: str) -> Path:
    """Undo stacks belong to a .BASE model, or to a bare wireframe .SKL file."""
    path = core.resolve_existing(model)
    return path if path.is_file() and path.suffix.lower() == ".skl" else resolve_base(model)


@mcp.tool()
def studio_undo(model: Annotated[str, Field(description=MODEL_HELP + " (or a wireframe .SKL)")], steps: int = 1) -> str:
    """Undo the last edit(s) of this model (every write took a snapshot of the BASE and skeleton)."""
    return _guard(lambda: history.undo(_history_key(model), steps))


@mcp.tool()
def studio_redo(model: Annotated[str, Field(description=MODEL_HELP + " (or a wireframe .SKL)")], steps: int = 1) -> str:
    """Redo edits that were undone (until the next new edit)."""
    return _guard(lambda: history.redo(_history_key(model), steps))


@mcp.tool()
def studio_history(model: Annotated[str, Field(description=MODEL_HELP + " (or a wireframe .SKL)")]) -> str:
    """List the undo/redo entries kept for this model."""
    return _guard(lambda: history.listing(_history_key(model)))


# --- wireframes (bare .SKL skeletons) -----------------------------------------------------------


@mcp.tool()
def studio_wire_get(skeleton: str) -> str:
    """Read a wireframe skeleton (e.g. 'Wireless/ARROW.SKL'): points as [x, z], lines/outlines as point-index
    lists, OTL2 outline points. See studio_help('wireframe')."""
    return _guard(wire.get, skeleton)


@mcp.tool()
def studio_wire_render(skeleton: str, labels: bool = True,
                       highlight: Annotated[Any, Field(description=SELECTOR_HELP)] = None) -> list:
    """Picture of a wireframe with point numbers (x across, z down); highlight = point selector to colour."""
    try:
        return [Image(data=wire.render_png(skeleton, 520, labels, highlight), format="png")]
    except core.McpToolError as exc:
        return ["ERROR: " + str(exc)]


@mcp.tool()
def studio_wire_new(skeleton: str) -> str:
    """Create an empty wireframe file (writable: Data/Models and Data/Wireless); fill it with studio_wire_edit."""
    return _guard(wire.new, skeleton)


@mcp.tool()
def studio_wire_edit(skeleton: str, operations: Annotated[list[dict], Field(description="Steps {'op': name, ...}; "
                     "see studio_help('wireframe'). Add 'save_as' to name new points/polygons.")],
                     label: str = "", dry_run: bool = False) -> str:
    """Edit a wireframe skeleton: add_points, add_shape, add_polygons (lines/outlines), delete_points,
    delete_polygons, transform (move/rotate/scale, mirror symmetry), set_points, align, mirror_copy, weld,
    outline_move. Steps run on one copy and are written once; undo with studio_undo."""
    return _guard(wire.run, skeleton, operations, label, dry_run)


# --- editing ----------------------------------------------------------------------------------


@mcp.tool()
def studio_edit_batch(model: Annotated[str, Field(description=MODEL_HELP)],
                      operations: Annotated[list[dict], Field(description="Steps, each {'op': name, ...arguments}; "
                                                              "see studio_help('ops'). Add 'save_as' to name a result.")],
                      label: str = "", dry_run: bool = False, owner_path: str = "", set_hint: str = "") -> str:
    """Run several modeling steps in order on one in-memory copy and write ONCE after verification. If a step
    fails nothing is written. dry_run=true checks the steps without saving. Steps can share results: give a step
    "save_as":"name" and use {"named":"name"} in later selectors."""
    return _guard(edit.run, model, operations, label, owner_path, set_hint, dry_run)


def _annotation(default):
    if isinstance(default, bool):
        return bool
    if isinstance(default, int):
        return int
    if isinstance(default, float):
        return float
    return Any


SELECTOR_ARGS = {"select": SELECTOR_HELP, "points": SELECTOR_HELP, "polygons": SELECTOR_HELP}


def _make_op_tool(op_name: str, function):
    """One MCP tool for one modeling operation, with its docstring and defaults taken from edit.OPS."""
    parameters = [inspect.Parameter("model", inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                    annotation=Annotated[str, Field(description=MODEL_HELP)])]
    defaults = {}
    for name, parameter in inspect.signature(function).parameters.items():
        if name in ("ctx", "save_as"):
            continue
        default = None if parameter.default is inspect.Parameter.empty else parameter.default
        defaults[name] = default
        annotation = _annotation(default)
        if name in SELECTOR_ARGS:
            annotation = Annotated[Any, Field(description=SELECTOR_ARGS[name])]
        parameters.append(inspect.Parameter(name, inspect.Parameter.POSITIONAL_OR_KEYWORD, default=default,
                                            annotation=annotation))
    common = {"label": "", "dry_run": False, "owner_path": "", "set_hint": ""}
    for name, default in common.items():
        parameters.append(inspect.Parameter(name, inspect.Parameter.POSITIONAL_OR_KEYWORD, default=default,
                                            annotation=_annotation(default)))

    def tool(**kwargs):
        target = kwargs.pop("model")
        extras = {name: kwargs.pop(name) for name in common}
        arguments = {name: value for name, value in kwargs.items() if value != defaults.get(name)}
        return _guard(edit.run, target, [{"op": op_name, **arguments}], extras["label"] or op_name,
                      extras["owner_path"], extras["set_hint"], extras["dry_run"])

    tool.__signature__ = inspect.Signature(parameters, return_annotation=str)
    tool.__name__ = edit.tool_name(op_name)
    description = " ".join((function.__doc__ or op_name).split())
    mcp.add_tool(tool, name=edit.tool_name(op_name), description=description)


for _name, _function in sorted(edit.OPS.items()):
    _make_op_tool(_name, _function)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
