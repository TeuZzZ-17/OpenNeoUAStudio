# Studio MCP server

Lets an AI assistant (Claude Code, Codex and any other MCP client) inspect, render and **edit** OpenNeoUA models
through Studio's own parsers, writers and Retail indexed renderer. Headless: no Studio window is needed.

An assistant should start with `studio_help()`: it explains the workflow, conventions, selectors, UV modes and recipes.

## Tools

**Read and look**

| Tool | What it does |
|---|---|
| `studio_help` | Built-in manual: start, design, conventions, selectors, uv, recipes, wireframe, ops |
| `studio_swatches` | Named faction texture patches (`ghorkov.hull`, `taerkasten.plate`, ...) with a picture of each (`swatches.json`, `swatches.png`) |
| `studio_status` / `studio_list_vp` / `studio_list_models` | Data folder and sets, VP numbers of a set, loose models under `Data/Models` |
| `studio_describe_model` / `studio_materials` | Objects, counts, size; material blocks with texture, shading, UV box and a sample polygon to copy |
| `studio_get_geometry` | Points and polygons, optionally filtered by selectors and with UVs |
| `studio_select` | Test a selector: which point / polygon indices does it hit? |
| `studio_effects` / `studio_list_textures` | FX elements, particle emitters, animated polygons; textures a model can use |
| `studio_analyze_model` | Quality report: no-area polygons, doubled points, inside-out solids, non-flat/non-convex polygons, UV and symmetry |
| `studio_compare_models` / `studio_validate_model` | Diff of two models; load + mapping checks |
| `studio_render_model` | Textured render, several views; `label_points`, `label_polygons`, `highlight_polygons`, `show_wire` draw indices on the image; `compare_with` shows a reference model beside it at the same scale. Polygons without material are outlined in magenta instead of blocking the render |

**Make an editable copy, history**

| Tool | What it does |
|---|---|
| `studio_new_model` | Start a new design: an empty loose model (no polygons, plain material blocks kept) |
| `studio_export_model` | Copy a Set.BAS model into a loose BASE + skeleton under `Data/Models` |
| `studio_undo` / `studio_redo` / `studio_history` | Every write keeps a snapshot of the BASE and skeleton |

**Edit** (one tool per operation, plus `studio_edit_batch` to chain several steps atomically)

| Group | Operations |
|---|---|
| Move points | `transform_points` (move/rotate/scale, `mirror` = the editor's Mirror Select), `set_points`, `snap_points`, `smooth_points`, `symmetrize_model`, `fit_model`, `center_at_origin` |
| Build new designs | `loft` (closed hull from cross-sections), `paint` (faction swatch onto polygons), `auto_shade` (baked light like the originals); `swatch=` also works on `add_polygons` and `create_primitive` |
| Add / remove geometry | `add_points`, `add_polygons`, `create_primitive` (box, wedge, prism/cylinder/cone, sphere, plane), `extrude_polygons`, `subdivide_polygons`, `duplicate_polygons`, `cut_paste`, `mirror_polygons`, `flip_polygons`, `weld_points`, `delete_polygons`, `cleanup_model`, `clear_model` |
| Other models | `import_geometry` (copy/paste polygons from another model, materials are matched or added) |
| Materials | `set_material`, `set_texture`, `set_uvs` (planar / rect / world / copy / explicit / adjust), `map_polygons`, `add_material_block`, `delete_material_block` |
| Effects | `add_fx`, `delete_fx`, `add_emitter`, `move_emitter`, `delete_emitter`, `generate_genesis` |
| Wireframes (`.SKL`) | `studio_wire_get`, `studio_wire_render` (drawn like the game: +z up, first segment of each entry), `studio_wire_new`, `studio_wire_edit` (`add_lines`, `add_shape` circle/arc/rect/cross/brackets/dashes, `trace_image`, `normalize`, `split_outlines`, ...), `studio_wire_from_model` (Studio's wireframe generator, effects left out by default) |

Selectors describe *which* points or polygons to edit (`{"x":[0,null]}`, `{"facing":"up"}`, `{"connected_to":12}`,
`{"mirror_of":{...}}` ...) so assistants do not have to list hundreds of numbers. See `studio_help('selectors')`.

## How edits are made safe

* Edits run on an in-memory copy of Studio's own data structures; the files are written **once**, after the
  staged result was re-read and checked. If any step of a batch fails, nothing is written.
* The commit reuses the Studio window's writers (`build_topology_states`, `save_model_base_copy`,
  `save_sklt_with_poo2_pol2_structure`) and its clipboard / material / FX routines: no format is re-implemented.
* Writes are limited to `Data/Models`, `Data/Wireless` and a temp work folder (more roots via
  `ONUA_MCP_WRITE_ROOTS`). Set.BAS files are only read.
* Particle emitters are tied to point numbers; when an edit renumbers points the emitters follow their point.
* New polygons copy the shading and texel density of the model's original polygons of that material.

## Connect a client

Claude Code reads `.mcp.json` in the project folder. Codex reads `~/.codex/config.toml`:

```toml
[mcp_servers.openneoua-studio]
command = 'C:\Users\<you>\AppData\Local\Python\pythoncore-3.14-64\python.exe'
args = ["-m", "studio_mcp.server"]
startup_timeout_sec = 60
tool_timeout_sec = 180

[mcp_servers.openneoua-studio.env]
ONUA_DATA = 'C:\...\UA Rising\Data'
PYTHONPATH = 'C:\...\Guide-Backup-Materiali\OpenNeoUAStudio'
```

The server reads `ONUA_DATA` (game Data folder); by default it uses `<project>/UA Rising/Data`. After changing the
server code, restart the client session so it launches the new version.

## Run / test

```bash
python -m studio_mcp.server                       # stdio server (set PYTHONPATH to the Studio folder)
python -m pytest tests/test_studio_mcp.py         # helpers, editing on real models, wireframes, a real MCP round trip
```

Renders run in a separate process (`render_worker.py`) so a Qt problem never stops the server.
Requires `mcp` (2.x or 1.x), `PySide6`, `numpy`, `Pillow`, `PyOpenGL`.

## Files

| File | Role |
|---|---|
| `server.py` | Tool registration; one tool per entry of `edit.OPS`, generated from the operation's signature and docstring |
| `edit.py`, `edit_materials.py` | The modeling operations and the batch runner |
| `edit_build.py`, `swatches.json`, `swatches.png` | New-design operations (loft, paint, auto_shade) and the faction swatch catalog |
| `loose.py` | A loose model opened for editing and its verified atomic commit |
| `select.py`, `uvs.py`, `shapes.py`, `meshmath.py` | Selectors, UV generation, primitives, vector helpers |
| `analysis.py` | Materials, effects, textures, quality report, comparison |
| `wire.py` | Wireframe (`.SKL`) editing |
| `history.py` | Undo / redo snapshots |
| `guide.py` | Text of `studio_help` |
| `core.py`, `render_worker.py` | Locations, sources, export; headless render with overlays |
