"""Live FX code editing, source provenance and non-destructive script saving."""

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

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


def test_click_opens_large_editor_and_live_drafts_keep_last_valid_preview(app, tmp_path):
    text = "new_vehicle 1\n" + EFFECT + "\nend\n"
    path = tmp_path / "vehicle.cfg"
    path.write_text(text, encoding="utf-8")
    document, block = _document(text)
    panel = DestructionFxPanel()
    try:
        panel.set_context(fx.CONTEXT_VEHICLE, document)
        panel.set_script_sources([(path, 0, block.end_line)])
        panel.resize(600, 650)
        panel.show()
        app.processEvents()
        row = panel.effects_list.item(0)
        checkbox = panel.effects_list.checkbox_rect(row).center()
        QTest.mouseClick(panel.effects_list.viewport(), Qt.MouseButton.LeftButton, pos=checkbox)
        assert panel.code_dialog is None
        assert panel.hidden_items == {0}
        point = panel.effects_list.visualItemRect(row).center()
        QTest.mouseClick(panel.effects_list.viewport(), Qt.MouseButton.LeftButton, pos=point)
        dialog = panel.code_dialog
        assert dialog is not None and dialog.isVisible()
        assert dialog.width() >= 900
        dialog.code.setPlainText(EFFECT.replace("400", "850"))
        panel.play_button.setChecked(True)
        panel.play_button.setChecked(False)
        assert panel.document.items[0].value("duration") == "850"
        assert panel.hidden_items == {0}
        assert panel.has_unsaved_text
        assert path.read_text() == text
        dialog.code.setPlainText(EFFECT.removesuffix("end"))
        QTest.qWait(300)
        assert panel.document.items[0].value("duration") == "850"
        assert not dialog.save_button.isEnabled()
        dialog.code.setPlainText(EFFECT.replace("400", "650"))
        QTest.qWait(300)
        dialog.activateWindow()
        dialog.code.editor.setFocus()
        app.processEvents()
        QTest.keyClick(dialog.code.editor, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
        assert "duration = 650" in path.read_text()
        assert not panel.has_unsaved_text
    finally:
        if panel.code_dialog is not None:
            panel.code_dialog.close()
        panel.close()


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


def test_dialog_list_text_opens_the_effect_and_checkbox_toggles_it(app):
    from entity_designer.fx_panel import FxEffectsView
    other = EFFECT.replace("400", "777")
    document, block = _document("new_vehicle 1" + chr(10) + EFFECT + chr(10) + other + chr(10) + "end" + chr(10))
    panel = DestructionFxPanel()
    try:
        panel.set_context(fx.CONTEXT_VEHICLE, document)
        panel.resize(600, 650)
        panel.show()
        panel.edit_effect_code(0)
        dialog = panel.code_dialog
        view = dialog.effects_view
        app.processEvents()
        assert isinstance(view, FxEffectsView)
        assert view.list.count() == panel.effects_list.count() == 2
        assert dialog.width() >= 900 and view.width() >= 300
        # Text click: the effect opens in the dialog's own editor, nothing is hidden.
        text_point = view.list.visualItemRect(view.list.item(1)).center()
        QTest.mouseClick(view.list.viewport(), Qt.MouseButton.LeftButton, pos=text_point)
        assert dialog.index == 1 and "777" in dialog.code.toPlainText()
        assert panel.hidden_items == set()
        # Checkbox click: only the visibility changes, the editor keeps its effect.
        box = view.list.checkbox_rect(view.list.item(0)).center()
        QTest.mouseClick(view.list.viewport(), Qt.MouseButton.LeftButton, pos=box)
        assert panel.hidden_items == {0} and dialog.index == 1
        assert panel.effects_list.item(0).checkState() == Qt.CheckState.Unchecked
        view.hide_all_button.click()
        assert panel.hidden_items == {0, 1}
        panel.show_all_button.click()
        assert view.list.item(0).checkState() == Qt.CheckState.Checked
        dialog.code.setPlainText(other.replace("777", "850"))
        QTest.qWait(300)
        assert view.list.item(1).text().split(chr(10))[0].endswith(" *")
    finally:
        if panel.code_dialog is not None:
            panel.code_dialog.close()
        panel.close()
