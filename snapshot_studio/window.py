"""Read-only model presentation and snapshot-export workspace."""

from __future__ import annotations

from pathlib import Path

from assembly_window import AssemblyWindow


WINDOW_TITLE = "OpenUAStudio - Snapshot Studio"


def _tab_index(tabs, title: str) -> int:
    """Return the first tab whose visible title matches ``title``."""

    for index in range(tabs.count()):
        if tabs.tabText(index) == title:
            return index
    return -1


class SnapshotStudioWindow(AssemblyWindow):
    """Focused read-only workspace using the existing Snapshot renderer."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._configure_snapshot_workspace()
        self._set_document_title(None)
        self.statusBar().showMessage(
            "Snapshot Studio is read-only: load an asset, choose a view, "
            "adjust animation and framing, then export the image."
        )

    def _configure_snapshot_workspace(self) -> None:
        resources_tabs = self._resources_tabs
        editor_tabs = self._editor_tabs
        snapshot_tabs = self._visuals_tabs

        texture_index = _tab_index(snapshot_tabs, "Textures")
        if texture_index >= 0:
            textures_panel = snapshot_tabs.widget(texture_index)
            snapshot_tabs.removeTab(texture_index)
            resources_tabs.addTab(textures_panel, "Asset Textures")
            self._asset_textures_panel = textures_panel

        snapshot_index = snapshot_tabs.indexOf(self._snapshot_panel)
        if snapshot_index >= 0:
            snapshot_tabs.setTabText(snapshot_index, "Photo Studio")

        editor_index = self._right_tabs.indexOf(editor_tabs)
        if editor_index >= 0:
            self._right_tabs.removeTab(editor_index)

        snapshot_outer_index = self._right_tabs.indexOf(snapshot_tabs)
        if snapshot_outer_index >= 0:
            self._right_tabs.setTabText(snapshot_outer_index, "Snapshot")

        # Snapshot Studio must never expose model-writing commands. Loading,
        # resource browsing, texture inspection and image export remain active.
        self.edit_menu.menuAction().setVisible(False)
        self.file_export_menu.menuAction().setVisible(False)
        self.mapping_repair_action.setVisible(False)
        self.global_undo_button.setVisible(False)
        self.global_redo_button.setVisible(False)

        self._right_tabs.setCurrentWidget(snapshot_tabs)
        snapshot_tabs.setCurrentWidget(self._snapshot_panel)
        self._on_right_tab_changed(self._right_tabs.currentIndex())

    def _open_visual_texture(
            self, name: str, *, show_preview: bool = False,
            switch_tabs: bool = True) -> None:
        """Open textures from the shared Resources > Asset Textures tab."""

        if switch_tabs and hasattr(self, "_asset_textures_panel"):
            self._right_tabs.setCurrentWidget(self._resources_tabs)
            self._resources_tabs.setCurrentWidget(
                self._asset_textures_panel)
        super()._open_visual_texture(
            name, show_preview=show_preview, switch_tabs=False)

    def _editing_allowed(self) -> bool:
        """Keep Snapshot Studio read-only even if an edit shortcut fires."""

        return False

    def _on_right_tab_changed(self, index: int) -> None:
        super()._on_right_tab_changed(index)
        # The shared handler restores normal controls outside Photo Studio;
        # this workspace must remain read-only in every tab.
        self.edit_toggle_action.setEnabled(False)

    def _set_document_title(self, path: str | Path | None) -> None:
        if path is None:
            self.setWindowTitle(WINDOW_TITLE)
            return
        full_path = Path(path).expanduser().resolve(strict=False)
        self.setWindowTitle(
            f"{WINDOW_TITLE} - {full_path.name} - {full_path}"
        )
