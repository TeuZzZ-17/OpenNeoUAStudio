"""Model editing and textured model-viewing workspace."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QAction
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QMessageBox

from assembly_window import AssemblyWindow, _BAS_KIND_ROLE, _BAS_NAME_ROLE


WINDOW_TITLE = "OpenNeoUA Studio - Model Editor"


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

        self.generate_wireframe_action = QAction(
            "Generate Wireframe", self)
        self.generate_wireframe_action.setEnabled(False)
        self.generate_wireframe_action.triggered.connect(
            self._open_wireframe_generator)
        self.file_menu.insertAction(
            self.exit_action, self.generate_wireframe_action)

        self.generate_genesis_action = QAction("Generate Genesis", self)
        self.generate_genesis_action.triggered.connect(self._generate_genesis)
        self.file_menu.insertAction(self.exit_action, self.generate_genesis_action)
        self.file_menu.aboutToShow.connect(self._sync_generator_actions)
        row = QHBoxLayout()
        self.generate_wireframe_button = QPushButton("Generate Wireframe")
        self.generate_genesis_button = QPushButton("Generate Genesis")
        for button, action in (
                (self.generate_wireframe_button, self.generate_wireframe_action),
                (self.generate_genesis_button, self.generate_genesis_action)):
            button.clicked.connect(action.trigger)
            row.addWidget(button)
        layout = self.model_texture_label.parentWidget().layout()
        layout.insertLayout(layout.indexOf(self.model_texture_label), row)
        self._sync_generator_actions()

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

    def _sync_wireframe_generator_action(self) -> None:
        self.generate_wireframe_action.setEnabled(self.viewport.has_model)

    def _sync_edit_action_states(self) -> None:
        super()._sync_edit_action_states()
        if hasattr(self, "generate_genesis_action"):
            self._sync_generator_actions()

    def _sync_generator_actions(self) -> None:
        self._sync_wireframe_generator_action()
        self.generate_wireframe_button.setEnabled(self.viewport.has_model)
        reason = self._material_structure_reason()
        obj = self._workbench_obj
        if not reason and (self._family.base_asset is None
                           or self._family.base_asset.tree is None):
            reason = "load a complete BASE asset family first"
        enabled = not reason and obj is not None and bool(obj.skeleton.polygons)
        self.generate_genesis_action.setEnabled(enabled)
        self.generate_genesis_button.setEnabled(enabled)
        tip = ("Transform the current model into a Genesis. Undo restores it."
               if enabled else "Generate Genesis: " + (reason or "load a model first"))
        self.generate_genesis_action.setToolTip(tip)
        self.generate_genesis_button.setToolTip(tip)

    def _add_model_generator_context_actions(self, menu, item) -> None:
        kind = item.data(0, _BAS_KIND_ROLE) if item is not None else None
        menu.addSeparator()
        for text, genesis in (("Generate Wireframe", False),
                              ("Generate Genesis", True)):
            action = menu.addAction(text)
            action.setEnabled(kind in ("base", "sklt.class"))
            action.triggered.connect(
                lambda _checked=False, g=genesis:
                self._generate_from_setbas_item(item, genesis=g))

    def _create_viewport_context_menu(self, position=None):
        menu = super()._create_viewport_context_menu(position)
        self._sync_generator_actions()
        reset_camera = menu.actions()[-1]
        menu.insertAction(reset_camera, self.generate_wireframe_action)
        menu.insertAction(reset_camera, self.generate_genesis_action)
        menu.insertSeparator(reset_camera)
        return menu

    def _generate_from_setbas_item(self, item, *, genesis: bool) -> None:
        if self._setbas is None or item is None:
            return
        kind = item.data(0, _BAS_KIND_ROLE)
        if kind not in ("base", "sklt.class"):
            return
        if kind == "base":
            name = item.data(0, _BAS_NAME_ROLE) or item.text(0)
            _family, target, _offset = self._resolve_setbas_base(str(name))
            if target is None:
                return
            if target is not self._owner_to_obj.get(self._selected_owner) \
                    and not self._confirm_discard_geometry():
                return
            if self._activate_setbas_base(str(name)) is None:
                return
        else:
            index = item.data(0, Qt.ItemDataRole.UserRole)
            if index is None:
                return
            resource = self._setbas.resources[index]
            obj = self._owner_to_obj.get(self._selected_owner)
            same_model = (obj is not None and self._family is not None
                          and self._family.setbas_archive is self._setbas
                          and obj.base_object.skeleton_name.replace("\\", "/").casefold()
                          == resource.resource_name.replace("\\", "/").casefold())
            if not same_model:
                if not self._confirm_discard_geometry():
                    return
                if not self._preview_setbas_skeleton(
                        resource, confirm_discard=False):
                    return
        if genesis:
            self._right_tabs.setCurrentWidget(self._editor_tabs)
            self._editor_tabs.setCurrentWidget(self._model_editor_panel)
            self._sync_tab_edit_mode()
            self._generate_genesis()
        else:
            self._open_wireframe_generator()

    def _generate_genesis(self) -> None:
        if not self._require_editing("Generate Genesis"):
            return
        if self._family is None or self._family.base_asset is None:
            self._notify("Generate Genesis requires a complete BASE asset family.", 6000)
            return
        from .genesis_generator import generate_genesis
        try:
            self._apply_material_structure(
                "Generate Genesis",
                lambda: generate_genesis(self._family, self._workbench_obj))
        except Exception as exc:
            QMessageBox.warning(self, "Generate Genesis failed", str(exc))
            return
        self._notify("Genesis generated. Undo restores the original model; "
                     "Export Asset Family saves the result.", 7000)

    def _open_wireframe_generator(self) -> None:
        if not self.viewport.has_model:
            return

        from .wireframe_dialog import WireframeGeneratorDialog

        animation_was_active = self.viewport._anim_timer.isActive()
        if animation_was_active:
            self.viewport._anim_timer.stop()
        try:
            WireframeGeneratorDialog(self.viewport, self).exec()
        finally:
            if animation_was_active:
                self.viewport._anim_timer.start()

    def _set_document_title(self, path: str | Path | None) -> None:
        if path is None:
            self.setWindowTitle(WINDOW_TITLE)
            return
        full_path = Path(path).expanduser().resolve(strict=False)
        self.setWindowTitle(
            f"{WINDOW_TITLE} - {full_path.name} - {full_path}"
        )
