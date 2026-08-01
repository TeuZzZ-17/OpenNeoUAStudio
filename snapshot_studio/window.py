"""Read-only model presentation and snapshot-export workspace."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QMenu

from assembly_window import AssemblyWindow


WINDOW_TITLE = "OpenUAStudio - Snapshot Studio"


class SnapshotStudioWindow(AssemblyWindow):
    """Focused view-only workspace using the existing Snapshot renderer."""

    _EDIT_ACTION_NAMES = (
        "edit_toggle_action",
        "edit_undo_action",
        "edit_redo_action",
        "edit_reset_action",
        "edit_select_all_action",
        "edit_select_none_action",
        "copy_geometry_action",
        "paste_geometry_action",
        "cut_geometry_action",
        "delete_geometry_action",
        "add_fx_action",
        "edit_scale_action",
        "edit_rotate_action",
    )

    def __init__(self, parent=None) -> None:
        # Base initialization still uses the normal nested workbench contract.
        # The flag prevents subclass hooks from changing that construction.
        self._snapshot_workspace_configured = False
        super().__init__(parent)
        self._configure_snapshot_workspace()
        self._set_document_title(None)
        self.statusBar().showMessage(
            "Snapshot Studio: choose a model in BAS Manager, compose the "
            "view in Snapshot, then export the image. Editing is disabled."
        )

    def _configure_snapshot_workspace(self) -> None:
        """Flatten the shared workbench into exactly two primary tabs."""

        right_tabs = self._right_tabs
        resources_tabs = self._resources_tabs
        visuals_tabs = self._visuals_tabs
        snapshot_panel = self._snapshot_panel
        bas_panel = self._bas_panel

        # Avoid transient tab-change callbacks while shared pages are moved.
        for tabs in (right_tabs, resources_tabs, visuals_tabs):
            tabs.blockSignals(True)
        try:
            snapshot_index = visuals_tabs.indexOf(snapshot_panel)
            if snapshot_index >= 0:
                visuals_tabs.removeTab(snapshot_index)

            bas_index = resources_tabs.indexOf(bas_panel)
            if bas_index >= 0:
                resources_tabs.removeTab(bas_index)

            # Preserve ownership references to the detached shared containers.
            # No parser, loader, viewport or renderer is copied.
            self._detached_resources_tabs = resources_tabs
            self._detached_editor_tabs = self._editor_tabs
            self._detached_visuals_tabs = visuals_tabs

            right_tabs.clear()
            right_tabs.addTab(snapshot_panel, "Snapshot")
            right_tabs.addTab(bas_panel, "BAS Manager")
            right_tabs.setCurrentWidget(snapshot_panel)
        finally:
            for tabs in (right_tabs, resources_tabs, visuals_tabs):
                tabs.blockSignals(False)

        # Snapshot Studio exports images only, never model structures.
        self.edit_menu.menuAction().setVisible(False)
        self.file_export_menu.menuAction().setVisible(False)
        self.mapping_repair_action.setVisible(False)
        self.integrated_editors_separator.setVisible(False)
        for action in (
                self.wireframe_editor_action,
                self.collision_editor_action,
                self.map_editor_action):
            action.setVisible(False)

        # Snapshot mode deliberately disables editor-only viewport filters.
        # Hide those unavailable entries instead of leaving grey commands in
        # the View menu. Backface Cull and Reset Camera remain available.
        for action in (
                self.sen_check,
                self.owner_bounds_check,
                self.wire_check,
                self.axes_check,
                self.grid_check,
                self.overlay_check,
                self.mapping_diag_check):
            action.setVisible(False)

        self.global_undo_button.setVisible(False)
        self.global_redo_button.setVisible(False)
        for name in self._EDIT_ACTION_NAMES:
            action = getattr(self, name, None)
            if action is None:
                continue
            action.blockSignals(True)
            if action.isCheckable():
                action.setChecked(False)
            action.blockSignals(False)
            action.setShortcuts([])
            action.setEnabled(False)
            action.setVisible(False)

        self._snapshot_workspace_configured = True
        self._force_view_only()
        self._on_right_tab_changed(right_tabs.currentIndex())

    def _snapshot_panel_is_active(self) -> bool:
        """Keep the whole focused workspace in safe Snapshot/View mode."""

        if not self._snapshot_workspace_configured:
            return super()._snapshot_panel_is_active()
        return True

    def _raise_setbas_tab(self) -> None:
        """Bring the direct BAS Manager tab to the front."""

        if not self._snapshot_workspace_configured:
            return super()._raise_setbas_tab()
        self._right_tabs.setCurrentWidget(self._bas_panel)

    def _focus_assets_for_owner(
            self, owner: str | None, switch_tabs: bool = True) -> None:
        """Keep selection state without opening detached resource tabs."""

        super()._focus_assets_for_owner(owner, switch_tabs=False)

    def _open_visual_texture(
            self, name: str, *, show_preview: bool = False,
            switch_tabs: bool = True) -> None:
        """Resolve or preview textures without exposing hidden tabs."""

        super()._open_visual_texture(
            name, show_preview=show_preview, switch_tabs=False)

    def _force_view_only(self) -> None:
        """Exit and disable every inherited model-edit entry path."""

        viewport = getattr(self, "viewport", None)
        if viewport is not None:
            if viewport.paste_preview_active:
                viewport.cancel_paste_preview()
            if viewport.is_edit_mode:
                viewport.exit_edit_mode()

        action = getattr(self, "edit_toggle_action", None)
        if action is not None:
            action.blockSignals(True)
            action.setChecked(False)
            action.blockSignals(False)
            action.setShortcuts([])
            action.setEnabled(False)
            action.setVisible(False)

    def _editing_allowed(self) -> bool:
        return False

    def _sync_tab_edit_mode(self) -> None:
        if not self._snapshot_workspace_configured:
            return super()._sync_tab_edit_mode()
        self._force_view_only()

    def _set_global_edit_mode(self, enabled: bool) -> None:
        if not self._snapshot_workspace_configured:
            return super()._set_global_edit_mode(enabled)
        self._force_view_only()

    def _on_edit_mode_toggled(self, active: bool) -> None:
        if not self._snapshot_workspace_configured:
            return super()._on_edit_mode_toggled(active)
        self._force_view_only()

    def _on_right_tab_changed(self, index: int) -> None:
        super()._on_right_tab_changed(index)
        if self._snapshot_workspace_configured:
            self._force_view_only()

    def _create_viewport_context_menu(self, _position=None) -> QMenu:
        """Expose only a camera-safe action in the Snapshot viewport."""

        menu = QMenu(self.viewport)
        reset_camera = menu.addAction(
            "Reset camera", self._reset_view_and_gizmo)
        reset_camera.setEnabled(self.viewport.can_reset_camera)
        return menu

    def _set_document_title(self, path: str | Path | None) -> None:
        if path is None:
            self.setWindowTitle(WINDOW_TITLE)
            return
        full_path = Path(path).expanduser().resolve(strict=False)
        self.setWindowTitle(
            f"{WINDOW_TITLE} - {full_path.name} - {full_path}"
        )
