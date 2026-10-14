"""Live FX code editing, source provenance and non-destructive script saving."""

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QHBoxLayout, QMessageBox, QWidget

from entity_designer import destruction_fx as fx
from entity_designer.fx_authoring import FxTextSession, effect_text, parse_effect_text
from entity_designer.fx_panel import DestructionFxPanel
from entity_designer.script_model import find_script_blocks


EFFECT = "begin_fx\n    trigger = destroyed\n    mode = visual\n    vp_model = 129\n    duration = 400\nend"


def _document(text):
    block = find_script_blocks(text)[0]
    return fx.extract_fx_document(text, block.start_line, block.end_line), block


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _in_wide_window(panel, app):
    """Put the panel at the right of a wide window, like the Entity Designer does.

    The editor slides out to the left of the panel, over the free area.
    """
    window = QWidget()
    row = QHBoxLayout(window)
    row.setContentsMargins(0, 0, 0, 0)
    row.addStretch(1)
    panel.setFixedWidth(420)
    row.addWidget(panel)
    window.resize(1300, 760)
    window.show()
    app.processEvents()
    return window


def _open_drawer(panel, app):
    """Wait for the slide animation to end."""
    QTest.qWait(260)
    app.processEvents()
    return panel.code_drawer


def test_invalid_or_extra_code_does_not_become_an_effect():
    for text in (EFFECT.removesuffix("end"), EFFECT + "\nenergy = 500", "end\n" + EFFECT):
        with pytest.raises(ValueError):
            parse_effect_text(text, fx.CONTEXT_VEHICLE)
    item, _ = parse_effect_text(EFFECT.replace("400", "800"), fx.CONTEXT_VEHICLE)
    assert item.value("duration") == "800"


def test_save_preserves_crlf_latin1_and_other_definitions(tmp_path):
    text = "; città\r\nnew_vehicle 1\r\nname = Unit\r\n" + EFFECT.replace("\n", "\r\n")
    text += "\r\n" + EFFECT.replace("129", "130").replace("\n", "\r\n")
    text += "\r\nend\r\n\r\nnew_vehicle 2\r\nenergy = 777\r\nend\r\n"
    path = tmp_path / "vehicles.cfg"
    path.write_bytes(text.encode("latin-1"))
    document, block = _document(text)
    session = FxTextSession(document, fx.CONTEXT_VEHICLE, [(path, block.start_line, block.end_line)])
    assert not session.source_error
    draft = session.drafts[0].replace("duration = 400", "; preserved comment\n    duration = 900")
    session.set_draft(0, draft)
    session.save([0])
    expected = text.replace(EFFECT.replace("\n", "\r\n"), draft.replace("\n", "\r\n"), 1)
    assert path.read_bytes() == expected.encode("latin-1")
    session.set_draft(1, session.drafts[1].replace("400", "600"))
    session.save([1])
    assert path.read_bytes() == expected.replace("duration = 400", "duration = 600").encode("latin-1")
    assert not session.dirty
    assert sorted(p.name for p in tmp_path.iterdir()) == ["vehicles.cfg"]


def test_save_refuses_external_changes_and_keeps_draft(tmp_path):
    path = tmp_path / "effects.cfg"
    text = "new_vehicle 1\n" + EFFECT + "\nend\n"
    path.write_text(text, encoding="utf-8")
    document, block = _document(text)
    session = FxTextSession(document, fx.CONTEXT_VEHICLE, [(path, 0, block.end_line)])
    session.set_draft(0, session.drafts[0].replace("400", "800"))
    changed = text + "; external author\n"
    path.write_text(changed, encoding="utf-8")
    with pytest.raises(ValueError, match="changed outside"):
        session.save([0])
    assert path.read_text(encoding="utf-8") == changed
    assert session.dirty


def test_catalog_tracks_effects_from_new_and_modify_files(tmp_path):
    from studio.assets.catalog import load_game_objects
    first = "new_vehicle 1\nname = Test\n" + EFFECT + "\nend\n"
    second = "modify_vehicle 1\n" + EFFECT.replace("129", "130") + "\nend\n"
    (tmp_path / "first.cfg").write_text(first, encoding="utf-8")
    (tmp_path / "second.cfg").write_text(second, encoding="utf-8")
    with patch("studio.assets.catalog.script_texts",
               return_value=[(first, "first.cfg"), (second, "second.cfg")]):
        obj = load_game_objects(SimpleNamespace(folder=lambda _name: tmp_path))[0]
    assert len(obj.body_sources) == 2
    document, _ = _document(obj.effective_text)
    session = FxTextSession(document, fx.CONTEXT_VEHICLE, obj.body_sources)
    assert not session.source_error
    session.set_draft(1, session.drafts[1].replace("400", "750"))
    session.save([1])
    assert (tmp_path / "first.cfg").read_text() == first
    assert (tmp_path / "second.cfg").read_text() == second.replace("400", "750")


def test_click_opens_attached_editor_and_live_drafts_keep_last_valid_preview(app, tmp_path):
    text = "new_vehicle 1\n" + EFFECT + "\nend\n"
    path = tmp_path / "vehicle.cfg"
    path.write_text(text, encoding="utf-8")
    document, block = _document(text)
    panel = DestructionFxPanel()
    window = _in_wide_window(panel, app)
    try:
        panel.set_context(fx.CONTEXT_VEHICLE, document)
        panel.set_script_sources([(path, 0, block.end_line)])
        app.processEvents()
        row = panel.effects_list.item(0)
        checkbox = panel.effects_list.checkbox_rect(row).center()
        QTest.mouseClick(panel.effects_list.viewport(), Qt.MouseButton.LeftButton, pos=checkbox)
        assert not panel.code_drawer.expanded
        assert panel.hidden_items == {0}
        point = panel.effects_list.visualItemRect(row).center()
        QTest.mouseClick(panel.effects_list.viewport(), Qt.MouseButton.LeftButton, pos=point)
        drawer = _open_drawer(panel, app)
        assert drawer.expanded and drawer.isVisible()
        # Attached: same height as the Effects box, ending where the box begins.
        box = panel.effects_box
        corner = box.mapTo(window, box.rect().topLeft())
        assert drawer.height() == box.height() and drawer.y() == corner.y()
        assert drawer.x() + drawer.width() == corner.x()
        assert drawer.width() >= 600
        drawer.code.setPlainText(EFFECT.replace("400", "850"))
        panel.play_button.setChecked(True)
        panel.play_button.setChecked(False)
        assert panel.document.items[0].value("duration") == "850"
        assert panel.hidden_items == {0}
        assert panel.has_unsaved_text
        assert path.read_text() == text
        drawer.code.setPlainText(EFFECT.removesuffix("end"))
        QTest.qWait(300)
        assert panel.document.items[0].value("duration") == "850"
        assert not drawer.save_button.isEnabled()
        drawer.code.setPlainText(EFFECT.replace("400", "650"))
        QTest.qWait(300)
        window.activateWindow()
        drawer.code.editor.setFocus()
        app.processEvents()
        QTest.keyClick(drawer.code.editor, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
        assert "duration = 650" in path.read_text()
        assert not panel.has_unsaved_text
    finally:
        window.close()


def test_discard_confirmation_preserves_cancelled_draft(app):
    text = "new_vehicle 1\n" + EFFECT + "\nend\n"
    document, _ = _document(text)
    panel = DestructionFxPanel()
    try:
        panel.set_context(fx.CONTEXT_VEHICLE, document)
        item, _ = panel.text_session.set_draft(0, EFFECT.replace("400", "900"))
        panel.apply_text_item(0, item)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Cancel):
            assert not panel.confirm_text_changes()
        assert panel.has_unsaved_text
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Discard):
            assert panel.confirm_text_changes()
        assert panel.document.items[0].value("duration") == "400"
        assert not panel.has_unsaved_text
    finally:
        panel.close()


def test_tab_opens_and_collapses_the_editor_and_text_click_opens_the_effect(app):
    other = EFFECT.replace("400", "777")
    document, block = _document("new_vehicle 1" + chr(10) + EFFECT + chr(10) + other + chr(10) + "end" + chr(10))
    panel = DestructionFxPanel()
    window = _in_wide_window(panel, app)
    try:
        panel.set_context(fx.CONTEXT_VEHICLE, document)
        app.processEvents()
        drawer = panel.code_drawer
        tab = drawer.tab
        # One list only, with the small tab sticking out of the box on its left.
        assert panel.effects_list.count() == 2
        box = panel.effects_box
        corner = box.mapTo(window, box.rect().topLeft())
        assert tab.isVisible() and tab.x() < corner.x() < tab.x() + tab.width()
        assert abs(tab.geometry().center().y() - (corner.y() + box.height() // 2)) <= 1
        assert not drawer.isVisible()
        # The tab opens the editor on the selected effect ...
        panel.effects_list.setCurrentRow(1)
        QTest.mouseClick(tab, Qt.MouseButton.LeftButton)
        _open_drawer(panel, app)
        assert drawer.expanded and drawer.isVisible() and drawer.index == 1
        assert "777" in drawer.code.toPlainText()
        # ... a text click changes the effect, a checkbox only toggles visibility ...
        view = panel.effects_view
        QTest.mouseClick(view.list.viewport(), Qt.MouseButton.LeftButton,
                         pos=view.list.visualItemRect(view.list.item(0)).center())
        assert drawer.index == 0 and panel.hidden_items == set()
        box_point = view.list.checkbox_rect(view.list.item(1)).center()
        QTest.mouseClick(view.list.viewport(), Qt.MouseButton.LeftButton, pos=box_point)
        assert panel.hidden_items == {1} and drawer.index == 0
        view.hide_all_button.click()
        assert panel.hidden_items == {0, 1}
        panel.show_all_button.click()
        assert view.list.item(0).checkState() == Qt.CheckState.Checked
        drawer.code.setPlainText(EFFECT.replace("400", "850"))
        QTest.qWait(300)
        assert view.list.item(0).text().split(chr(10))[0].endswith(" *")
        # ... and the tab folds it away again, keeping the edit.
        QTest.mouseClick(tab, Qt.MouseButton.LeftButton)
        _open_drawer(panel, app)
        assert not drawer.expanded and not drawer.isVisible()
        assert panel.document.items[0].value("duration") == "850"
        QTest.mouseClick(tab, Qt.MouseButton.LeftButton)
        _open_drawer(panel, app)
        assert drawer.expanded and drawer.index == 0
    finally:
        window.close()


def test_editor_keys_do_not_trigger_window_shortcuts(app):
    from PySide6.QtGui import QAction, QKeySequence
    document, _ = _document("new_vehicle 1" + chr(10) + EFFECT + chr(10) + "end" + chr(10))
    panel = DestructionFxPanel()
    window = _in_wide_window(panel, app)
    try:
        panel.set_context(fx.CONTEXT_VEHICLE, document)
        triggered = []
        action = QAction("Duplicate", window)
        action.setShortcut(QKeySequence("Ctrl+D"))
        action.triggered.connect(lambda: triggered.append(True))
        window.addAction(action)
        panel.edit_effect_code(0)
        drawer = _open_drawer(panel, app)
        window.activateWindow()
        drawer.code.editor.setFocus()
        app.processEvents()
        lines = drawer.code.toPlainText().count(chr(10))
        QTest.keyClick(drawer.code.editor, Qt.Key.Key_D, Qt.KeyboardModifier.ControlModifier)
        assert not triggered
        assert drawer.code.toPlainText().count(chr(10)) == lines + 1
    finally:
        window.close()
