"""Modeless effect-code workspace with live preview and explicit saving."""

from PySide6.QtCore import QSignalBlocker, Qt, QTimer
from PySide6.QtWidgets import (
    QDialog, QGroupBox, QHBoxLayout, QLabel, QMessageBox, QPushButton, QSplitter, QVBoxLayout, QWidget,
)

from entity_designer.fx_panel import FxEffectsView
from studio.ui.code_editor import ScriptCodeEditor

# The script lines are short: the free right part of the editor hosts the effects list.
DIALOG_SIZE = (1000, 830)


class FxCodeDialog(QDialog):
    def __init__(self, panel):
        super().__init__(panel.window())
        self.panel = panel
        self.index = -1
        self.resize(*DIALOG_SIZE)
        self.setMinimumSize(720, 480)
        self.setModal(False)
        outer = QHBoxLayout(self)
        split = QSplitter(Qt.Orientation.Horizontal, self)
        split.setChildrenCollapsible(False)
        outer.addWidget(split)
        left = QWidget(split)
        layout = QVBoxLayout(left)
        layout.setContentsMargins(0, 0, 0, 0)
        self.source_label = QLabel()
        self.source_label.setWordWrap(True)
        layout.addWidget(self.source_label)
        self.code = ScriptCodeEditor(self)
        layout.addWidget(self.code, 1)
        buttons = QHBoxLayout()
        self.play_button = QPushButton("Play Preview")
        self.play_button.clicked.connect(panel.play_button.click)
        panel.play_button.toggled.connect(
            lambda playing: self.play_button.setText("Pause Preview" if playing else "Play Preview"))
        buttons.addWidget(self.play_button)
        buttons.addStretch(1)
        self.revert_button = QPushButton("Revert Effect")
        self.revert_button.clicked.connect(self._revert)
        buttons.addWidget(self.revert_button)
        self.save_button = QPushButton("Save Effect")
        self.save_button.clicked.connect(self._save)
        buttons.addWidget(self.save_button)
        self.save_all_button = QPushButton("Save All Effects")
        self.save_all_button.clicked.connect(lambda: self._save(all_effects=True))
        buttons.addWidget(self.save_all_button)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        # Same list as the tab's Effects box: text opens the effect here, checkboxes show/hide.
        effects = QGroupBox("Effects", split)
        effects_layout = QVBoxLayout(effects)
        effects_layout.setContentsMargins(6, 4, 6, 4)
        self.effects_view = FxEffectsView(panel, parent=effects)
        panel.add_effects_view(self.effects_view)
        effects_layout.addWidget(self.effects_view)
        split.addWidget(left)
        split.addWidget(effects)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([int(DIALOG_SIZE[0] * 0.6), int(DIALOG_SIZE[0] * 0.4)])
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self._apply)
        self.code.textChanged.connect(self._edited)
        self.code.saveRequested.connect(self._save)

    def load_effect(self, index):
        self.timer.stop()
        if self.index >= 0 and self.index != index:
            self._apply()
        if self.index == index and self.isVisible():
            self.raise_()
            self.code.editor.setFocus()
            return
        self.index = index
        self.effects_view.select_effect(index)
        with QSignalBlocker(self.code):
            self.code.setPlainText(self.panel.text_session.drafts[index])
        self._apply()
        self.show()
        self.raise_()
        self.code.editor.setFocus()

    def _edited(self, text):
        if self.index < 0:
            return
        self.panel.text_session.drafts[self.index] = text
        self.panel.update_text_status()
        self.timer.start()

    def _apply(self):
        if self.index < 0:
            return False
        session = self.panel.text_session
        try:
            item, issues = session.set_draft(self.index, self.code.toPlainText())
        except ValueError as exc:
            self.code.set_error(f"Preview keeps the last valid version. {exc}")
            self.save_button.setEnabled(False)
            self._sync_title()
            return False
        self.panel.apply_text_item(self.index, item)
        message = "; ".join(issue.message for issue in issues)
        self.code.set_error(message)
        self.save_button.setEnabled(not session.source_error and session.is_dirty(self.index))
        self.save_all_button.setEnabled(not session.source_error and session.dirty)
        self._sync_title()
        return True

    def _sync_title(self):
        session = self.panel.text_session
        dirty = " *" if session.is_dirty(self.index) else ""
        self.setWindowTitle(f"Effect {self.index + 1}{dirty} — OpenNeoUA Studio")
        source = (str(session.sources[self.index].path)
                  if len(session.sources) > self.index else "Preview draft")
        self.source_label.setText(source + ("\n" + session.source_error if session.source_error else ""))
        self.revert_button.setEnabled(session.is_dirty(self.index))
        self.play_button.setText("Pause Preview" if self.panel.is_playing() else "Play Preview")

    def _save(self, _checked=False, *, all_effects=False):
        self.timer.stop()
        if not self._apply():
            return False
        try:
            paths = self.panel.text_session.save(
                range(len(self.panel.document.items)) if all_effects else [self.index])
        except (OSError, ValueError, RuntimeError, UnicodeError) as exc:
            QMessageBox.warning(self, "Effect was not saved", str(exc))
            return False
        self.panel.sourceSaved.emit(paths)
        self.panel.update_text_status()
        self._apply()
        return True

    def _revert(self):
        self.timer.stop()
        session = self.panel.text_session
        item = session.revert(self.index)
        with QSignalBlocker(self.code):
            self.code.setPlainText(session.drafts[self.index])
        self.panel.apply_text_item(self.index, item)
        self._sync_title()
        self.code.set_error("")
        self.save_button.setEnabled(False)

    def closeEvent(self, event):  # noqa: N802
        self.timer.stop()
        if self.index >= 0:
            self._apply()
        super().closeEvent(event)
