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

## Level source preservation

Saving an opened level applies only editor changes to its original LDF source.
Unchanged blocks, unknown parameters, manual scripts, comments and formatting remain intact.
Squad and Host edits retain the original object's custom fields; terrain edits update only changed cells.
New maps are generated normally. Save and Save As both preserve an opened level's source.
If the file changes on disk after opening or saving, Studio refuses to overwrite it: reopen it or save a separate copy.
Locations remain visible across Map Editor tabs with a faint color tint and outlined names; editing controls stay in Locations.

## Safety and data handling

Treat original game files as read-only whenever possible.

Save edited assets and levels to explicit output paths and keep backups of source data.

OpenNeoUA Studio is intended to support safe editing workflows, but users remain responsible for protecting their own files and installations.

## Warranty

OpenNeoUA Studio is provided without warranty.

Use it at your own risk.

The full warranty disclaimer and limitation of liability are contained in the GNU GPL v3 license text.
