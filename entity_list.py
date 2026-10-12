"""One Entity List panel and visual-state workflow for Model and Snapshot."""
from pathlib import Path
from PySide6.QtCore import QSignalBlocker
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QMessageBox, QSizePolicy, QVBoxLayout, QWidget
from game_browser import GameObjectBrowser


class EntityListMixin:
    def _create_entity_list(self):
        panel = QWidget(self)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        self.entity_browser = GameObjectBrowser(self, inline_set_selector=False)
        self.entity_browser.enable_state_column()
        self._entity_visual_states = {}
        self._entity_choices = {}
        self.entity_state = QComboBox()
        self.entity_state.setToolTip('Choose the model state used by this game entity.')
        browser_layout = self.entity_browser.layout()
        previous_controls = browser_layout.takeAt(0).layout()
        previous_controls.removeWidget(self.entity_browser.category)
        previous_controls.deleteLater()
        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(5)
        for label, combo, stretch in (('Set:', self.entity_browser.set_combo, 0),
                                       ('State:', self.entity_state, 1),
                                       ('Type:', self.entity_browser.category, 0)):
            controls.addWidget(QLabel(label))
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(5 if stretch == 0 else 10)
            combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            controls.addWidget(combo, stretch)
        browser_layout.insertLayout(browser_layout.indexOf(self.entity_browser.tree), controls)
        layout.addWidget(self.entity_browser, 1)
        self._entity_controls = controls
        self._entity_panel = panel
        self._current_entity = None
        self._loaded_entity_state = 0
        self._loaded_entity_set = None
        self._entity_loading = False
        self._set_combo = self.entity_browser.set_combo
        self.entity_browser.selected.connect(self._entity_selected)
        self.entity_browser.setChanged.connect(self._entity_set_changed)
        self.entity_state.currentIndexChanged.connect(self._open_entity_state)
        return panel

    def _reload_entity_resources(self):
        self.entity_browser.reload()
        self.entity_browser.enable_state_column()
        self._entity_choices.clear()
        self._entity_visual_states.clear()
        self.entity_state.clear()
        self._current_entity = None

    def _entity_set_changed(self, number):
        from loading_feedback import loading_resources
        pages = [(tabs, tabs.currentWidget()) for tabs in (self._right_tabs, self._resources_tabs)]
        self._entity_loading = True
        try:
            with loading_resources(self, f"Loading Set {number}. Please wait…"):
                if self._set_selector_changed(number) and self._current_entity is not None:
                    self._entity_selected(self._current_entity)
                self.entity_browser.enable_state_column()
                self._refresh_dirty_resource_rows()
        finally:
            self._entity_loading = False
            for tabs, page in pages:
                if page is not None and tabs.indexOf(page) >= 0:
                    tabs.setCurrentWidget(page)

    def _entity_selected(self, obj):
        from game_visuals import visual_choices
        try:
            choices = visual_choices(obj, self.entity_browser.set_number)
        except (OSError, ValueError, RuntimeError) as exc:
            QMessageBox.warning(self, "Entity unavailable", str(exc))
            self.entity_browser.select_object(self._current_entity)
            return
        if not choices:
            self.statusBar().showMessage(f"{obj.name}: no model state found in the selected set.")
            self.entity_browser.select_object(self._current_entity)
            return
        previous = self._current_entity
        previous_choices = [(self.entity_state.itemText(i), self.entity_state.itemData(i))
                            for i in range(self.entity_state.count())]
        previous_index = self.entity_state.currentIndex()
        self._current_entity = obj
        key = (self.entity_browser.set_number, obj.kind, obj.object_id)
        self._entity_choices[key] = choices
        with QSignalBlocker(self.entity_state):
            self.entity_state.clear()
            for choice in choices:
                self.entity_state.addItem(choice.label, choice)
            remembered = self._entity_visual_states.get(key)
            if remembered is not None:
                self.entity_state.setCurrentIndex(max(0, self.entity_state.findText(remembered)))
        if not self._open_entity_state():
            self._current_entity = previous
            with QSignalBlocker(self.entity_state):
                self.entity_state.clear()
                for label, choice in previous_choices:
                    self.entity_state.addItem(label, choice)
                self.entity_state.setCurrentIndex(previous_index)
            self.entity_browser.select_object(previous)

    def _open_entity_state(self, *_args):
        choice = self.entity_state.currentData()
        if choice is None or not self._confirm_discard_geometry():
            with QSignalBlocker(self.entity_state):
                self.entity_state.setCurrentIndex(self._loaded_entity_state)
            if self._loaded_entity_set is not None:
                with QSignalBlocker(self.entity_browser.set_combo):
                    index = self.entity_browser.set_combo.findData(self._loaded_entity_set)
                    if index >= 0:
                        self.entity_browser.set_combo.setCurrentIndex(index)
            return False
        skip_warning = self._skip_model_switch_warning
        previous_loading = self._entity_loading
        self._entity_loading = True
        self._skip_model_switch_warning = True
        try:
            if choice.base_name:
                if self._setbas is None or Path(self._setbas.path) != choice.path:
                    self.open_setbas(choice.path)
                if self._activate_setbas_base(choice.base_name) is None:
                    return False
            elif choice.path.suffix.casefold() == ".3ds":
                from entity_designer.editor import _load_3ds_family
                self._set_family(_load_3ds_family(choice.path))
                self._set_document_title(choice.path)
            else:
                self.open_base(choice.path, confirm_discard=False)
            self.statusBar().showMessage(f"{self._current_entity.name} — {choice.label}")
            self._loaded_entity_state = self.entity_state.currentIndex()
            self._loaded_entity_set = self.entity_browser.set_number
            key = (self._loaded_entity_set, self._current_entity.kind, self._current_entity.object_id)
            self._entity_visual_states[key] = choice.label
            self.entity_browser.set_visual_state(self._current_entity,
                choice.label.split(' — ')[-1] if self._current_entity.kind == 'building' else choice.label.split(' — ')[0])
            self._refresh_dirty_resource_rows()
            return True
        except (OSError, ValueError, RuntimeError) as exc:
            QMessageBox.warning(self, "Entity unavailable", str(exc))
            return False
        finally:
            self._skip_model_switch_warning = skip_warning
            self._entity_loading = previous_loading
