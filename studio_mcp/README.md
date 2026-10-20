# Studio MCP server

Lets an AI assistant (Claude Code and other MCP clients) work with OpenNeoUA models through Studio's own
parsers and its Retail indexed renderer. Headless: no Studio window is needed.

## Tools

| Tool | What it does |
|---|---|
| `studio_status` | Game Data folder, sets found, folders where writing is allowed |
| `studio_list_vp` | VP numbers of a set from `VISPROTO.LST` (optional text filter) |
| `studio_describe_model` | Objects, point/polygon counts, size, textures, material blocks |
| `studio_get_geometry` | All points and polygons (with their material block) |
| `studio_render_model` | Textured render: views `iso`, `front`, `top`, ... as one labelled sheet |
| `studio_export_model` | Copy a Set.BAS model into a loose BASE + skeleton under `Data/Models` |
| `studio_set_points` / `studio_transform_points` | Edit the points of a loose skeleton (topology unchanged) |
| `studio_validate_model` | Loads, skeleton found, every polygon mapped, no broken point references |

Sources are `set1:77` (VP number), `set1:VPwGLID2` (object name) or a path to a `.BASE` file.
Axes: x right, y down (negative = up), z forward.

## Safety

Writes are limited to `Data/Models` and a temp work folder (more roots via `ONUA_MCP_WRITE_ROOTS`).
Set.BAS files are only read. Topology changes (adding/removing polygons) are not supported yet.

## Run / test

```bash
python -m studio_mcp.server                       # stdio server (set PYTHONPATH to the Studio folder)
python -m pytest tests/test_studio_mcp.py         # core operations + a real MCP round trip
```

The server reads `ONUA_DATA` (game Data folder); by default it uses `<project>/UA Rising/Data`.
Renders run in a separate process (`render_worker.py`) so a Qt problem never stops the server.
Requires `mcp` (2.x or 1.x), `PySide6`, `numpy`, `Pillow`, `PyOpenGL`.
