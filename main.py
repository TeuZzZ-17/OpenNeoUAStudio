"""OpenUAStudio entry point.

Launches the integrated Urban Assault asset workbench. The main window
contains the 3D/BASE/SET.BAS tools and opens the complete Wireframe Editor
or Map Editor from the Tools menu.

Usage:
    python main.py [path/to/asset.base | path/to/SET.BAS]
    python main.py --map-editor [path/to/level.ldf]
"""

from __future__ import annotations

import sys
from pathlib import Path

APP_TITLE = "OpenUAStudio"
MAP_EDITOR_FLAG = "--map-editor"


def _open_startup_path(window, value: str) -> None:
    """Route the documented command-line file types to the matching UI.

    ``SET.BAS`` is both a ``.BAS`` filename and a resource archive.  Sending
    it through ``open_base`` happens to expose some geometry, but skips the
    archive browser/provider state that the File menu initializes.
    """

    path = Path(value)
    if path.name.casefold() == "set.bas":
        window.open_setbas(path)
    else:
        window.open_base(path)


def _run_map_editor(args: list[str]) -> int:
    from map_editor.editor import main as map_editor_main

    try:
        flag_index = args.index(MAP_EDITOR_FLAG)
    except ValueError:
        return 1
    return map_editor_main(args[flag_index + 1:flag_index + 2])


def main() -> int:
    args = sys.argv[1:]
    if MAP_EDITOR_FLAG in args:
        return _run_map_editor(args)

    from PySide6.QtWidgets import QApplication
    from assembly_window import AssemblyWindow

    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    # Each window composes its own full title. Keep the platform from
    # appending a second "OpenUAStudio" label to document titles.
    app.setApplicationDisplayName("")

    window = AssemblyWindow()
    window.setWindowTitle(APP_TITLE)
    window.show()

    if args:
        _open_startup_path(window, args[0])

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
