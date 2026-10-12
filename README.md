# OpenNeoUA Studio

Urban Assault Metropolisdawn thread: https://metropolisdawn.de/forum/thread/870-release-openuastudio-all-in-one-urban-assault-asset-editor-and-extraction-tool/

OpenNeoUA Studio (technical project name: `OpenNeoUAStudio`) is an independent,
community-developed editing workbench for OpenNeoUA and Microsoft Urban Assault
(1998). It is derived from the `UA_source`/OpenUA tool lineage without hiding
that provenance.

The project brings together tools and workflows for inspecting, editing, converting, and creating compatible game data.

Its structure, interface, supported formats, features, and integrated editors may change as development continues.

## Project status

OpenNeoUA Studio is under active development.

Features, file layouts, commands, dependencies, workflows, and user-interface elements may be added, removed, renamed, or reorganized without notice.

The current repository should be treated as a development version rather than a final product specification.

## Basic use

Run from source:

```bash
python main.py
```

On normal startup, OpenNeoUA Studio first shows a tool selector for the Model
Editor, Snapshot Studio, Map Editor, Entity Designer, or Wireframe Editor.

Resources are global to the application and must be configured at startup.
Selections persist between launches. On startup, Studio automatically discovers
and suggests existing standard resource folders; you can override those
suggestions by choosing folders manually. Studio does not create folders or
copy data. The shared Tools menu opens another editor while keeping current
editor sessions open.

The Map Editor is integrated in the same application. Choose Map Editor at
startup, or run `python main.py --map-editor path/to/level.LDF`. Choose the local
game installation when prompted; game resources remain external and are loaded
from the selected original Urban Assault or OpenNeoUA data folders.

The map palette includes Beamgates, Super Items and Gems alongside the terrain,
actor, technology, script and level pages. Configure the next object or select
an existing one, then use Add for a movable grey preview. Click to confirm;
Esc or Deselect cancels without changing the map. Place / Move previews a move
of the selected object. Dragging temporarily greys the selected geometry. Gates and super items have editable
key sectors; gems have effects for vehicles, buildings and weapons, with
searchable targets and custom IDs. Copy places an independent duplicate with
relative key positions. Esc cancels placement; Undo/Redo includes data and
sector visuals. Place or delete pending objects before saving. Each special
category retains the LDF limit of ten objects.
Special sectors share actor picking and colored silhouettes, with a white
selection contour. Host viewangle displays the actual viewing direction and
updates an active POV; the host body follows the game orientation. Countdown
controls display MM:SS while LDF values retain millisecond precision. Generated
MB/DB use terrain relief, grain and palette statistics from local retail
artwork; MB is brighter than DB.

## Shared resources and editor workflows

Entity Designer only previews building visual states and sector slots. Building
models are edited in Model Editor through its shared Entity List, with All,
Vehicles, Weapons, and Buildings filters showing the sets currently available in
Resources. The Model Editor also lets you choose a VP state for the loaded
model. Wireframe Editor searches configured resources for SKL and SKLT files;
Vertex IDs are hidden by default.

Model Editor and Snapshot Studio share the same Entity List. Its compact Set,
State and Type selectors sit below search, and its State column remembers the
chosen vehicle, weapon or building state. Importing an installed SET.BAS updates
the shared set selector. Wireframe selection follows the actual loaded file
and clears for New or files outside the browser. Unsaved resource
edits appear in red in Entity List and Bas Manager; Undo and Reset clear the red
when they restore the original data. Unselected Wireframe vertices are yellow.
Set changes and BAS imports show a shared waiting message. The selected set is
shared by tools for the current game installation during the Studio session,
and a set change preserves the open tab. Modified selected rows use a dark
background with red text and a light selection border. Entity List offers an
essential context menu for opening, copying identifiers and filtering.
Generate Wireframe uses automatic transparency selection and a fixed 3-pixel
contour simplification; its export button is labelled Export.
Editor windows open maximized.
Entity Designer shows Game resources directly, with read-only entity identity,
and supports Collision Shape for vehicles and weapons without a missing-spheres
warning.

Map Change Set choices are built from complete sets already present in the
configured resources. Sector Editor is coming soon. The Collision Editor's
Briefing Model Size tab reads the installed Brief and Debrief files and previews
the vehicle's `briefing_size` through Studio's renderer. The preview can differ
visually from the retail game.

Install source dependencies with `python -m pip install -r requirements.txt`.
The map uses persistent OpenGL 3.3 geometry. The Model, Snapshot, Collision and
Wireframe and UV viewports use GPU painting; textured 3D previews share the map's
indexed texture, SHADERMP and TRACYRMP framebuffer. Retail source-face culling,
clipping, BSP ordering and editing tools remain shared. Exact diagnostic and
snapshot exports retain the software reference rasterizer. Systems without a
usable OpenGL context automatically use software; `NME_RENDERER=software`
forces that backend for troubleshooting. Click the GPU/Software status badge to
switch renderers; an unavailable GPU keeps Software active and reports the
reason.

A precompiled Windows executable may also be included in the repository for convenience.

## License

Copyright (C) 2025-2026 TeuZzZ-17

The original OpenNeoUA Studio source code and original project components are licensed under the GNU General Public License version 3 only (`GPL-3.0-only`).

See the `LICENSE` file for the complete license terms.

The GNU GPL applies only to material for which the OpenNeoUA Studio copyright holders have the legal authority to grant that license.

It does not relicense third-party software, game data, trademarks, artwork, textures, models, sounds, documentation, or other materials owned by their respective rights holders.

## Third-party game data and asset notice

OpenNeoUA Studio is an unofficial, fan-made project.

It is not affiliated with, endorsed by, sponsored by, or approved by Microsoft, Xbox Game Studios, TerraTools, or any other original publisher, developer, or rights holder connected with Urban Assault.

Microsoft Urban Assault, its name, trademarks, logos, artwork, game data, audiovisual material, and other proprietary content remain the property of their respective owners.

## Safety and data handling

Treat original game files as read-only whenever possible.

Save edited assets and levels to explicit output paths and keep backups of source data.

OpenNeoUA Studio is intended to support safe editing workflows, but users remain responsible for protecting their own files and installations.

## Warranty

OpenNeoUA Studio is provided without warranty.

Use it at your own risk.

The full warranty disclaimer and limitation of liability are contained in the GNU GPL v3 license text.
