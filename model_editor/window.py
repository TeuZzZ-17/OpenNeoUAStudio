"""Model editing and textured model-viewing workspace."""

from __future__ import annotations

from pathlib import Path

from assembly_window import AssemblyWindow


WINDOW_TITLE = "OpenUAStudio - Model Editor"


def _tab_index(tabs, title: str) -> int:
    """Return the first tab whose visible title matches ``title``."""

    for index in range(tabs.count()):
        if tabs.tabText(index) == title:
            return index
    return -1


class ModelEditorWindow(AssemblyWindow):
    """Main asset workbench without the dedicated Snapshot workspace."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._configure_model_editor_workspace()
        self._set_document_title(None)
        self.statusBar().showMessage(
            "Model Editor: view and edit textured models. Snapshot export "
            "is available from the separate Snapshot Studio workspace."
        )

    def _configure_model_editor_workspace(self) -> None:
        resources_tabs = self._resources_tabs
        editor_tabs = self._editor_tabs
        visuals_tabs = self._visuals_tabs

        texture_index = _tab_index(visuals_tabs, "Textures")
        if texture_index >= 0:
            textures_panel = visuals_tabs.widget(texture_index)
            visuals_tabs.removeTab(texture_index)
            resources_tabs.addTab(textures_panel, "Asset Textures")
            self._asset_textures_panel = textures_panel

        visuals_index = self._right_tabs.indexOf(visuals_tabs)
        if visuals_index >= 0:
            self._right_tabs.removeTab(visuals_index)

        # Put the working editor first, followed by resource browsing.
        # Remove/reinsert the existing widgets; no panel is duplicated.
        for panel in (resources_tabs, editor_tabs):
            index = self._right_tabs.indexOf(panel)
            if index >= 0:
                self._right_tabs.removeTab(index)
        self._right_tabs.addTab(editor_tabs, "Editor")
        self._right_tabs.addTab(resources_tabs, "Resources")

        # Other editors are launched only from the startup workspace selector.
        for action in (
                self.wireframe_editor_action,
                self.collision_editor_action,
                self.map_editor_action):
            action.setVisible(False)

        # Keep the detached container alive because the shared AssemblyWindow
        # still owns the Snapshot controls and their signal connections.
        self._detached_visuals_tabs = visuals_tabs
        self._right_tabs.setCurrentWidget(editor_tabs)

    def _open_visual_texture(
            self, name: str, *, show_preview: bool = False,
            switch_tabs: bool = True) -> None:
        """Open textures from the new Resources > Asset Textures tab."""

        if switch_tabs and hasattr(self, "_asset_textures_panel"):
            self._right_tabs.setCurrentWidget(self._resources_tabs)
            self._resources_tabs.setCurrentWidget(
                self._asset_textures_panel)
        super()._open_visual_texture(
            name, show_preview=show_preview, switch_tabs=False)

    def _set_document_title(self, path: str | Path | None) -> None:
        if path is None:
            self.setWindowTitle(WINDOW_TITLE)
            return
        full_path = Path(path).expanduser().resolve(strict=False)
        self.setWindowTitle(
            f"{WINDOW_TITLE} - {full_path.name} - {full_path}"
        )
