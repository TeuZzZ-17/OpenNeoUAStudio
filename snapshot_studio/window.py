"""Read-only model presentation and snapshot-export workspace."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QToolBar,
)

from assembly_window import AssemblyWindow
from editor_widgets import (
    ViewportWidthScrollArea as _ViewportWidthScrollArea,
)
from snapshot_studio.batch_export import VPSnapshotBatchPanel


WINDOW_TITLE = "OpenNeoUA Studio - Snapshot Studio"


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
        self._snapshot_workspace_configured = False
        super().__init__(parent)
        self._configure_snapshot_workspace()
        self._set_document_title(None)
        self.statusBar().showMessage(
            "Snapshot Studio: compose one image or export every renderable "
            "VP and embedded SKLT model. Editing is disabled."
        )

    def _configure_snapshot_workspace(self) -> None:
        """Flatten the shared workbench into exactly two primary tabs."""

        right_tabs = self._right_tabs
        resources_tabs = self._resources_tabs
        visuals_tabs = self._visuals_tabs
        snapshot_panel = self._snapshot_panel
        bas_panel = self._bas_panel

        for tabs in (right_tabs, resources_tabs, visuals_tabs):
            tabs.blockSignals(True)
        try:
            snapshot_index = visuals_tabs.indexOf(snapshot_panel)
            if snapshot_index >= 0:
                visuals_tabs.removeTab(snapshot_index)

            bas_index = resources_tabs.indexOf(bas_panel)
            if bas_index >= 0:
                resources_tabs.removeTab(bas_index)

            self._detached_resources_tabs = resources_tabs
            self._detached_editor_tabs = self._editor_tabs
            self._detached_visuals_tabs = visuals_tabs

            # Reuse the same width-synchronised scroll page as Model Editor.
            # It keeps the page exactly as wide as the visible viewport, so
            # controls reflow instead of being clipped horizontally. Vertical
            # scrolling appears only when the available height is insufficient.
            snapshot_scroll = _ViewportWidthScrollArea()
            snapshot_scroll.setObjectName("snapshotStudioScroll")
            snapshot_scroll.setWidgetResizable(True)
            snapshot_scroll.setFrameShape(QFrame.Shape.NoFrame)
            snapshot_scroll.setHorizontalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            snapshot_scroll.setVerticalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            snapshot_scroll.setWidget(snapshot_panel)
            self._snapshot_scroll = snapshot_scroll

            right_tabs.clear()
            right_tabs.addTab(snapshot_scroll, "Snapshot")
            right_tabs.addTab(bas_panel, "BAS Manager")
            right_tabs.setCurrentWidget(snapshot_scroll)
        finally:
            for tabs in (right_tabs, resources_tabs, visuals_tabs):
                tabs.blockSignals(False)

        self.edit_menu.menuAction().setVisible(False)
        self.file_export_menu.menuAction().setVisible(False)
        self.mapping_repair_action.setVisible(False)
        self.integrated_editors_separator.setVisible(False)
        for action in (
                self.wireframe_editor_action,
                self.collision_editor_action,
                self.map_editor_action):
            action.setVisible(False)

        # View options stay on the shared AssemblyWindow QAction path.
        # Snapshot Studio changes only their default checked state; it does
        # not own a second menu or a second set of overlay controls.

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

        self._configure_snapshot_controls()
        self._install_batch_panel()

        self._snapshot_workspace_configured = True
        self._force_view_only()
        self._on_right_tab_changed(right_tabs.currentIndex())

    def _configure_snapshot_controls(self) -> None:
        """Create focused VANM controls and hide, but never destroy, toolbars.

        AssemblyWindow still uses hidden toolbar widgets such as mode_combo
        internally while entering Snapshot mode.  Removing the QToolBars can
        destroy their QWidgetAction-owned controls in PySide6, leaving the
        shared lifecycle with invalid C++ objects.  Snapshot Studio therefore
        keeps the inherited toolbars alive but invisible and uses dedicated
        controls in the right panel.
        """

        animation_box = next(
            (
                box for box in self._snapshot_panel.findChildren(QGroupBox)
                if box.title() == "Animation frame"
            ),
            None,
        )
        self._snapshot_studio_box = next(
            (
                box for box in self._snapshot_panel.findChildren(QGroupBox)
                if box.title() == "Photo Studio"
            ),
            None,
        )

        # Keep the original toolbar-owned controls alive inside hidden
        # toolbars.  The visible Snapshot controls below are independent
        # widgets wired to the same existing viewport methods.
        self._toolbar_play_button = self.play_button
        self._toolbar_step_button = self.step_button
        self._toolbar_speed_spin = self.speed_spin

        self.play_button = QPushButton("Play")
        self.play_button.setCheckable(True)
        self.play_button.setEnabled(False)
        self.play_button.toggled.connect(self._toggle_play)

        self.step_button = QPushButton(">>")
        self.step_button.setEnabled(False)
        self.step_button.clicked.connect(self.viewport.step_animation)

        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setRange(0.05, 8.0)
        self.speed_spin.setSingleStep(0.25)
        self.speed_spin.setValue(
            self._toolbar_speed_spin.value())
        self.speed_spin.setMaximumWidth(96)
        self.speed_spin.valueChanged.connect(
            self._on_animation_speed_changed)

        if animation_box is not None:
            vanm_row = QHBoxLayout()
            vanm_row.setSpacing(4)
            vanm_row.addWidget(QLabel("VANM preview:"))
            vanm_row.addWidget(self.play_button)
            vanm_row.addWidget(self.step_button)
            vanm_row.addWidget(QLabel("Speed:"))
            vanm_row.addWidget(self.speed_spin)
            vanm_row.addStretch(1)
            animation_box.layout().insertLayout(0, vanm_row)

        self.global_undo_button.setVisible(False)
        self.global_redo_button.setVisible(False)
        self.global_undo_button.setEnabled(False)
        self.global_redo_button.setEnabled(False)

        # Hiding removes the bars visually while preserving every inherited
        # control required by AssemblyWindow's internal Snapshot lifecycle.
        for toolbar in self.findChildren(QToolBar):
            toolbar.hide()

        self._sync_animation_controls()

    def _install_batch_panel(self) -> None:
        """Add the compact complete-model batch controller."""

        self.vp_batch_panel = VPSnapshotBatchPanel(self)
        snapshot_layout = self._snapshot_panel.layout()
        snapshot_layout.insertWidget(1, self.vp_batch_panel)
        # Recompute the page minimum after the batch controls are inserted.
        # The surrounding QScrollArea will then provide vertical scrolling
        # whenever the window is not tall enough.
        self._snapshot_panel.adjustSize()

    def open_setbas(self, path: str | Path) -> None:
        super().open_setbas(path)
        if hasattr(self, "vp_batch_panel"):
            self.vp_batch_panel.refresh()

    def _sync_vp_controls(self) -> None:
        super()._sync_vp_controls()
        panel = getattr(self, "vp_batch_panel", None)
        if panel is not None:
            panel.refresh()

    def _close_archive_block_reason(self) -> str:
        panel = getattr(self, "vp_batch_panel", None)
        if panel is not None and panel.is_running:
            return (
                "A Snapshot batch is still running. Cancel it or wait for the "
                "current image to finish before closing the current resource.")
        return super()._close_archive_block_reason()

    def closeEvent(self, event) -> None:  # noqa: N802
        panel = getattr(self, "vp_batch_panel", None)
        if panel is not None and panel.is_running:
            panel.request_cancel()
            QMessageBox.information(
                self,
                "Snapshot batch in progress",
                "Cancellation was requested. Wait for the current image "
                "to finish before closing Snapshot Studio.",
            )
            event.ignore()
            return
        super().closeEvent(event)

    def _snapshot_panel_is_active(self) -> bool:
        if not self._snapshot_workspace_configured:
            return super()._snapshot_panel_is_active()
        return True

    def _raise_setbas_tab(self) -> None:
        if not self._snapshot_workspace_configured:
            return super()._raise_setbas_tab()
        self._right_tabs.setCurrentWidget(self._bas_panel)

    def _focus_assets_for_owner(
            self, owner: str | None, switch_tabs: bool = True) -> None:
        super()._focus_assets_for_owner(owner, switch_tabs=False)

    def _open_visual_texture(
            self, name: str, *, show_preview: bool = False,
            switch_tabs: bool = True) -> None:
        super()._open_visual_texture(
            name, show_preview=show_preview, switch_tabs=False)

    def _force_view_only(self) -> None:
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

        for button_name in (
                "global_undo_button", "global_redo_button"):
            button = getattr(self, button_name, None)
            if button is not None:
                button.setVisible(False)
                button.setEnabled(False)

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
