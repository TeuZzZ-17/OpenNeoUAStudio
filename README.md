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

The **Sector Editor** is available from the startup selector and **Change Editor** menu.
Choose an installed set through **Change Set** or use **Open Set** to select a `SetN` folder. The left list
shows all 256 sector slots, including free IDs. Compose nine cells or select one
large cell, preview all four damage states, and assign existing buildings or models.
Assigning a model creates an independent building state row, so other sectors keep
their existing states. The Definitions tab edits shared model, collision, shield,
symbol and destruction-effect data. Rotate and mirror rearrange cell positions.

Sector and model cards show cached 3D thumbnails, generated only for visible rows.
Click the actual geometry to select a cell, including tall buildings; **Ctrl+click**
extends the selection; **Shift+click** selects a rectangle. The **Cells** panel shows
an interactive top view of the actual nine cell models, with small position indices.
Browsing the **Models** list previews the highlighted BASE as a grey ghost on the
selected cells. The ghost temporarily replaces the destination geometry in the shared
3D scene, preserving depth and staying visible while orbiting the camera. It never
changes the native tables. **W/A** browse backwards and **S/D** browse forwards in
resource lists; context menus accept WASD as directional keys. **Enter** applies the
model or confirms the active placement. Text fields keep normal text editing.

Drag a model onto either view to apply it; dropping inside the current selection
applies it to that selection. Drag an existing building or already selected cells to
move their pattern, keeping the grabbed cell beneath the pointer. Drag from background
or an empty cell to draw a selection rectangle; **Ctrl** adds to the selection.
**Shift+drag** also starts a selection rectangle over existing cells. Double-click
outside the sector to deselect all cells, including in single-cell layouts.

Right-click cells for **Move**, **Copy**, **Cut**, **Paste**, **Delete**, layout
rotation/mirroring and selection tools. **Copy** immediately starts a clipboard ghost,
including the relative positions and all four damage states. Point at a destination
and click, press **Enter**, or use **Ctrl+V** to place it. **Paste** restarts the visual
placement after cancellation; **Esc** cancels the preview without losing the clipboard.
Cut clears the source through Undo/Redo and starts the same placement preview. A copied
single cell can fill a destination selection; larger patterns must fit inside the grid.
Every edit uses the existing Undo/Redo history. Rotation rearranges the cell layout;
it does not rotate individual BASE geometry.

Use **Ctrl/Shift** in the sector list to select several definitions. **Ground / filler**
context tools fill, replace matching families, or sample the native connection family
(0-5), while preserving buildings. The inline preview shows its actual native connection
to family 0 at a sample height difference. A standalone sector has no neighboring
connections to display. Terrain inside a BASE remains part of that model. **Minimap
symbol** shows the real 4px and 8px game glyphs enlarged beside its numeric value.

**F** frames selected cells, the toolbar provides the shared Studio view presets, and
**Reset View** restores the sector's initial framing. **F5** reloads set assets while
preserving current edits. **Export Complete Set as ZIP** packages all set resources.
Table/resource checks remain active during save and export. **View > Validation Report**
opens their details; a status-bar link appears when the set has issues. There is no
persistent Validation tab.

**File > Export selected sector definition** and **Export all sector definitions**
write native SDF tables with the models and four damage states they reference.
**Import sector definitions** merges these into free ordinary slots and reuses
matching model/building rows. This exchanges definitions using the open set's assets;
use the complete ZIP to distribute the resource files as well. Missing assets block
the merge. **Sector > Restore saved sector layout** restores the saved cell arrangement
and properties and can be undone. **Export current view as PNG** saves the rendered
view on a transparent background without editor guides.

Model definition fields retain unapplied edits while working on cells. Switching
models offers Apply/Discard/Cancel, and Save/Save As/ZIP include pending model edits.
Malformed fields block the operation and remain available for correction.
Independent damage-state fields also retain pending edits across panel refreshes,
model-definition updates and asset reloads. Switching cells or sectors offers
Apply/Discard/Cancel; saving and exporting includes these pending states and the
current sector description even when its text field still has focus.

Right-click a model card and choose **Find sectors using this model**, or select
**Using selected model** in the sector filter, to find references across all four
damage states. Cell tooltips list both model IDs and their BASE names. The model
usage summary shows the number of affected sectors before a shared definition is
changed. Editing or refreshing a model preserves the current camera position.

The right panel separates **Cells**, **Models** and **Definitions**.
Cells contains the layout, nine-cell selection, existing buildings and independent
damage states. Models provides the larger thumbnail browser and clearly identifies
the destination sector/cells. Definitions contains the shared model properties.
**Choose model** opens the browser; **Inspect model** opens the assigned model for
the currently previewed damage state.

Right-click a cell for replacement, inspection, **Select matching buildings**, copy,
paste and clear. Copy/paste retains all four state and model definitions, including
after undo or model-slot reuse; pasting reuses matching definitions and restores
missing dependencies within the current set. A copied cell fills any selection;
a copied group retains its relative cell positions during visual placement. **Delete** clears
selected cells when working in the viewport; text fields keep normal text editing.

**New Set** assembles independently selected `HI`, `Objects`, `Palette`, `Remap` and
`Scripts` folders in a new `Set<number>` directory. It can start with no sectors;
without an existing `SET.SDF`, it starts with a dummy model and building row.
**Save** preserves unchanged SDF rows and research notes, and uses the game's native
`Scripts/override.lst` LEGO entries when visual BASE replacements are required.
Required inline BASE objects are extracted with the existing Studio writer to
native `Objects/*.bas` files, so the game can load each replacement directly.
**Save Set As** creates a complete independent set. **Export complete ZIP** includes
all set files and current edits under `Data/Sets/SetN/` without saving over the source.

Sector IDs remain **0–255** for map/save compatibility. Slots **248–255** are reserved
for native map borders. The updated OpenNeoUA engine supports **4096 model rows and
4096 building state rows**; sets exceeding either original 256-row limit require that
engine. The compatibility checker identifies these extended tables. Map and save
formats are unchanged. Missing BASE/collision resources block saving and packaging.

A precompiled Windows executable may be included in the repository for convenience.
On every other system (and on Windows, if you prefer) run the Studio from source.

### Running from source

Requirements: Python 3.10 or newer and `pip`.

1. Open a terminal in the repository folder.
2. (Recommended) create and activate a virtual environment, so the dependencies stay separate from the system Python:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate        # Windows: .venv\Scripts\activate
   ```

3. Install the dependencies:

   ```bash
   python -m pip install -r requirements.txt
   ```

4. Start the Studio:

   ```bash
   python main.py
   ```

On Linux, PySide6 (Qt) may need a few system libraries that are not installed by `pip`.
If the application does not start and Qt reports that the `xcb` platform plugin could not be loaded,
install the XCB cursor library from your distribution (for example `xcb-util-cursor` on Fedora,
`libxcb-cursor0` on Debian/Ubuntu).

### Choosing the game installation

On first start, select either the game folder or its `Data` folder. The Studio then fills in every
resource folder (sets, scripts, levels, briefing maps, music, ...) and shows how many items it found.
Every folder can still be changed by hand.

The following layouts are recognized:

- **Vanilla Urban Assault**: `Levels` (with `Mbpix`) and `Music` sit next to the `Data` folder,
  and the sets are loose `SetN` folders inside `Data`.
- **OpenNeoUA**: everything sits inside `Data`, as the engine reads `Data` first and the game folder second.
- **Metropolis Dawn**: select `dataxp` to use the expansion's scripts and sets together with the
  sibling `levelsxp` folder. If both `Data` and `dataxp` are installed, selecting the game folder
  defaults to `Data`; select `dataxp` explicitly for the expansion.

The Map Editor preserves every `target_level` of a beamgate and reads original level scripts,
including `script:` includes. Vehicle and Host Station IDs come from the selected installation's
scripts, including expansion-specific IDs.

The collapsible **Map issues** indicator in the left panel lists missing sector/building/model
references, inactive buildings owned by faction `0`, and flak without a matching Host Station.
Click an entry to find its sector. A power station owned by a faction without a Host is listed as
information: neutral stations, usually owner `7`, can be intentional. Checks do not change the map.

Stoudson Bomb countdowns use **1024 game ticks per second** and display `MM:SS`, rounding a remaining
fractional second up as the game does. Unedited imported tick values are preserved exactly.
Host AI delays remain editable as raw ticks, with the same conversion shown in their tooltips.

Folder and file names are matched without regard to case, so `DATA`, `Data` and `data`
(or `SET.BAS` and `set.bas`) all work on case-sensitive file systems such as Linux.
The `Models` and `Interface` folders exist only in OpenNeoUA, so they stay empty for a vanilla installation.

The chosen folders are remembered in `%APPDATA%\Sektor3\installation.json` on Windows and in
`~/.config/Sektor3/installation.json` (or `$XDG_CONFIG_HOME`) elsewhere. A remembered folder that no
longer exists is detected again automatically; *Reset Configuration* forgets all of them.

For a map of the source code (folders, shared layers, where each editor lives) see [ARCHITECTURE.md](ARCHITECTURE.md).

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
