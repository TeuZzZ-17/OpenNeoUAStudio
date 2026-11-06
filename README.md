# OpenNeoUA Studio

Urban Assault Metropolisdawn thread: https://metropolisdawn.de/forum/thread/870-release-openuastudio-all-in-one-urban-assault-asset-editor-and-extraction-tool/

OpenNeoUA Studio (technical project name: `OpenNeoUAStudio`) is an independent,
community-developed editing workbench for OpenNeoUA and Microsoft Urban Assault
(1998). It is derived from the `UA_source`/OpenUA tool lineage without hiding
that provenance.

The project brings together tools and workflows for inspecting, editing, converting, and creating compatible game data.

Its structure, interface, supported formats, features, and integrated editors may change as development continues.

## Companion project: OpenNeoUA

[OpenNeoUA](https://github.com/TeuZzZ-17/OpenNeoUA) is a closely related, open-source engine for Urban Assault. OpenNeoUA Studio is designed not only for editing the original game, but also for working with the extended OpenNeoUA engine and its game data.

We recommend visiting the companion repository and reporting bugs or other problems in the relevant project's issue tracker. Reports are appreciated and will be addressed as promptly as possible.

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

On Linux and macOS, `run_OpenNeoUAStudio.sh` does steps 2-4 for you: it creates `.venv`, installs
`requirements.txt` (again only when that file changes) and starts the Studio. Any arguments are passed to `main.py`:

```bash
chmod +x run_OpenNeoUAStudio.sh      # only needed once
./run_OpenNeoUAStudio.sh
./run_OpenNeoUAStudio.sh --map-editor path/to/level.ldf
```

On Linux, PySide6 (Qt) may need a few system libraries that are not installed by `pip`.
If the application does not start and Qt reports that the `xcb` platform plugin could not be loaded,
install the XCB cursor library from your distribution (for example `xcb-util-cursor` on Fedora,
`libxcb-cursor0` on Debian/Ubuntu).
Music and sounds use Qt Multimedia, which needs GStreamer or FFmpeg on the system
(for example `gstreamer1.0-plugins-good` on Debian/Ubuntu).

### Choosing the game installation

On first start Studio only asks where the game is installed. Any folder works: the game
folder, its `Data` folder, or the folder around it. Studio then finds the data folder by itself,
recognises the game and catalogues every resource, whatever the letter case and the old or new
file formats. Each resource shows what was really found. A folder that is missing can be chosen
by hand; a resource the game does not have at all is locked.

- **Urban Assault (original)**: `Levels` (with `Mbpix`) and `Music` sit next to the `Data` folder,
  and the sets are loose `SetN` folders inside `Data`. `Models`, `Graphics` and `Collisions` exist
  only in OpenNeoUA, so they are locked, not reported as missing.
- **OpenNeoUA**: everything sits inside `Data`, as the engine reads `Data` first and the game folder second.
- **Metropolis Dawn**: the game sits in `dataxp` and `levelsxp`, also when you select the folder
  around `Game`. Installed beside the base game it borrows skies and music from `Data`. If both
  `Data` and `dataxp` are there, selecting the game folder defaults to `Data`; select `dataxp` for
  the expansion.

## Graphics card

On a computer with two graphics chips, a program is given the slow built-in one unless it asks for the other. The engine asks through its own executable; Studio runs under Python and cannot, so on Windows it asks the system instead, with the same "High performance" choice the Settings page writes.

- It is written in the current user's own settings, needs no administrator, and can be changed back any time in Settings, System, Display, Graphics.
- On Linux Studio asks the NVIDIA library for the card by itself, before Qt starts, in the same way the `prime-run` command does, and drops the request if no drawing context can be made with it. With an AMD or Intel card, start Studio with `DRI_PRIME=1`.
- The chip in use is written on the console at startup and shown in the renderer badge in the status bar.

Every way of starting Studio gets the card by itself: the Windows preference
belongs to one exact program path, so the built program, a copy in `dist` and
`python.exe` from the sources each get their own. When Studio finds itself on the
built-in chip of a computer with a card, it asks Windows for the card and restarts
once. A "Power saving" choice in Windows is respected.

Click the renderer badge to choose GPU or software rendering without closing the
current document. The badge shows the name reported by the active OpenGL
context; a software OpenGL implementation is identified as such. When multiple
devices are detected, the menu also offers Linux PCI devices, or the Windows
power-saving/high-performance preference for the packaged application. A device
preference takes effect after restarting Studio; availability and the resulting
device depend on the OS and driver. Check the badge after restarting.

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
