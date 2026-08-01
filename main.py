"""OpenUAStudio entry point.

The normal startup path shows a lightweight tool selector first. The chosen
editor is imported only after the user selects it, while the explicit
``--map-editor`` command remains available for the separate Tk process.

Usage:
    python main.py [path/to/asset.base | path/to/SET.BAS]
    python main.py --map-editor [path/to/level.ldf]
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

APP_TITLE = "OpenUAStudio"
MAP_EDITOR_FLAG = "--map-editor"
MAIN_SUITE_TOOL = "main_suite"
MAP_EDITOR_TOOL = "map_editor"
COLLISION_EDITOR_TOOL = "collision_editor"
WIREFRAME_EDITOR_TOOL = "wireframe_editor"

_active_window = None


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


def launch_map_editor_process(startup_file: str | Path | None = None):
    """Launch the Tk-based Map Editor in its own process."""

    if getattr(sys, "frozen", False):
        command = [sys.executable, MAP_EDITOR_FLAG]
        working_directory = Path(sys.executable).resolve().parent
    else:
        main_path = Path(__file__).resolve()
        command = [sys.executable, str(main_path), MAP_EDITOR_FLAG]
        working_directory = main_path.parent

    if startup_file is not None:
        command.append(str(startup_file))
    return subprocess.Popen(command, cwd=str(working_directory))


def _startup_path(args: list[str]) -> str | None:
    """Return the optional document argument for a selected tool."""

    return args[0] if args else None


def _show_qt_tool(app, tool: str, startup_path: str | None) -> int:
    """Create and show the selected Qt editor, then run its event loop."""

    global _active_window

    if tool == MAIN_SUITE_TOOL:
        from assembly_window import AssemblyWindow

        window = AssemblyWindow()
        window.setWindowTitle(APP_TITLE)
        window.show()
        if startup_path:
            _open_startup_path(window, startup_path)
    elif tool == COLLISION_EDITOR_TOOL:
        from collision_editor import CollisionEditorWindow

        window = CollisionEditorWindow()
        window.show()
        if startup_path:
            if Path(startup_path).suffix.casefold() in {".skl", ".sklt"}:
                window.open_sklt(startup_path)
            else:
                window.open_base(startup_path)
    elif tool == WIREFRAME_EDITOR_TOOL:
        from wireframe_editor.window import WireframeEditorWindow

        window = WireframeEditorWindow()
        window.show()
        if startup_path:
            window.open_file(startup_path)
    else:
        return 1

    _active_window = window
    return app.exec()


def _launch_selected_map_editor(startup_path: str | None) -> int:
    """Start Map Editor after the selector has closed."""

    try:
        launch_map_editor_process(startup_path)
    except OSError as exc:
        from PySide6.QtWidgets import QMessageBox

        QMessageBox.critical(
            None,
            "Map Editor unavailable",
            "The Map Editor could not be launched.\n\n"
            f"{exc}",
        )
        return 1
    return 0


def main() -> int:
    args = sys.argv[1:]
    if MAP_EDITOR_FLAG in args:
        return _run_map_editor(args)

    from PySide6.QtWidgets import QApplication
    from startup_selector import StartupToolSelector

    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    # Each window composes its own full title. Keep the platform from
    # appending a second "OpenUAStudio" label to document titles.
    app.setApplicationDisplayName("")

    selector = StartupToolSelector()
    if selector.exec() != selector.DialogCode.Accepted:
        return 0

    tool = selector.selected_tool()
    if tool is None:
        return 0
    startup_path = _startup_path(args)
    if tool == MAP_EDITOR_TOOL:
        return _launch_selected_map_editor(startup_path)

    return _show_qt_tool(app, tool, startup_path)


if __name__ == "__main__":
    raise SystemExit(main())
