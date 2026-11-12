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



def _offset(panel):
    """Model-state rows lead the effects list; effect N is the row after them."""
    return len(panel.visible_state_indexes())


def _item(panel, number):
    return panel.effects_list.item(number + _offset(panel))


def _set_row(panel, number):
    # Qt adds the row to a multi-selection: clear it first so exactly this row is selected.
    panel.effects_list.clearSelection()
    panel.effects_list.setCurrentRow(number + _offset(panel))


def _effect_rows(panel):
    return panel.effects_list.count() - _offset(panel)

def _document(text):
    block = find_script_blocks(text)[0]
    return fx.extract_fx_document(text, block.start_line, block.end_line), block


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    yield instance
    # QTest shortcuts must not leave Ctrl pressed for the next test module.
    receiver = QWidget()
    QTest.keyRelease(receiver, Qt.Key.Key_Control)


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
               return_value=[(first, "first.cfg", 0), (second, "second.cfg", 0)]):
        obj = load_game_objects(SimpleNamespace(folder=lambda _name: tmp_path))[0]
    assert len(obj.body_sources) == 2
    document, _ = _document(obj.effective_text)
    session = FxTextSession(document, fx.CONTEXT_VEHICLE, obj.body_sources)
    assert not session.source_error
    session.set_draft(1, session.drafts[1].replace("400", "750"))
    session.save([1])
    assert (tmp_path / "first.cfg").read_text() == first
    assert (tmp_path / "second.cfg").read_text() == second.replace("400", "750")


def test_catalog_source_offsets_remain_correct_after_an_include(tmp_path):
    from studio.assets.catalog import load_game_objects

    (tmp_path / "Startup.cfg").write_text("include Weapons.cfg\n", encoding="utf-8")
    (tmp_path / "Other.cfg").write_text("new_weapon 5\nname = Other\nend\n", encoding="utf-8")
    source = ("new_weapon 1\nname = First\nend\ninclude Other.cfg\n"
              "new_weapon 37\nname = Target\n" + EFFECT + "\nend\n")
    path = tmp_path / "Weapons.cfg"
    path.write_text(source, encoding="utf-8")
    objects = load_game_objects(SimpleNamespace(folder=lambda _name: tmp_path))
    target = next(item for item in objects if item.kind == "weapon" and item.object_id == 37)
    document, _ = _document(target.effective_text)
    session = FxTextSession(document, fx.CONTEXT_WEAPON, target.body_sources,
                            expected=("new_weapon", 37))
    assert session.can_add(), session.source_error
    assert target.body_sources[0][1] == 4
    assert path.read_text(encoding="utf-8") == source


def test_stale_source_span_cannot_insert_into_another_definition(tmp_path):
    text = ("new_weapon 37\n    name = Target\n" + EFFECT + "\nend\n"
            "\nnew_weapon 38\n    name = Other\nend\n")
    path = tmp_path / "Weapons.cfg"
    path.write_text(text, encoding="utf-8")
    document, block = _document(text)
    session = FxTextSession(document, fx.CONTEXT_WEAPON,
                            [(path, block.start_line + 1, block.end_line + 1)])
    assert not session.can_add()
    with pytest.raises(ValueError):
        session.add_blank()
    assert path.read_text(encoding="utf-8") == text


def test_exact_span_of_another_weapon_is_rejected(tmp_path):
    from entity_designer import fx_visuals

    text = ("new_weapon 37\n" + EFFECT + "\nend\n"
            "new_weapon 38\n" + EFFECT + "\nvp_normal = 999\nend\n")
    path = tmp_path / "Weapons.cfg"
    path.write_text(text, encoding="utf-8")
    document, _ = _document(text)
    other = find_script_blocks(text)[1]
    origin = [(path, other.start_line, other.end_line)]
    session = FxTextSession(document, fx.CONTEXT_WEAPON, origin, expected=("new_weapon", 37))
    states = fx_visuals.StateSession(origin, expected=("new_weapon", 37))
    assert not session.can_add() and "different definition" in session.source_error
    assert "different definition" in states.source_error and not states.values
    states.set_draft(fx_visuals.MODEL_INDEX, "vp_normal = 111")
    with pytest.raises(ValueError, match="different definition"):
        states.save([fx_visuals.MODEL_INDEX])
    assert path.read_text(encoding="utf-8") == text


def test_save_effect_and_model_targets_the_correct_modify_block(app, tmp_path):
    from entity_designer import fx_visuals

    text = ("new_vehicle 1\nname = Target\n" + EFFECT + "\nend\n"
            "modify_vehicle 1\nenergy = 20\nend\n"
            "new_vehicle 2\nname = Other\nend\n")
    path = tmp_path / "Vehicles.cfg"
    path.write_text(text, encoding="utf-8")
    blocks = find_script_blocks(text)
    effective = "new_vehicle 1\nname = Target\n" + EFFECT + "\nenergy = 20\nend\n"
    document, _ = _document(effective)
    panel = DestructionFxPanel()
    try:
        panel.set_context(fx.CONTEXT_VEHICLE, document)
        panel.set_script_sources([(path, b.start_line, b.end_line) for b in blocks[:2]],
                                 expected=("new_vehicle", 1))
        panel.text_session.set_draft(0, EFFECT.replace("    duration", "; one\n; two\n    duration"))
        panel.state_session.set_draft(fx_visuals.MODEL_INDEX, "vp_normal = 211")
        panel.save_rows(None)
        saved = path.read_text(encoding="utf-8")
        updated = find_script_blocks(saved)
        lines = saved.splitlines()
        first = "\n".join(lines[updated[0].start_line:updated[0].end_line + 1])
        second = "\n".join(lines[updated[1].start_line:updated[1].end_line + 1])
        assert "vp_normal" not in first
        assert "vp_normal = 211" in second
        assert saved.endswith("new_vehicle 2\nname = Other\nend\n")
    finally:
        panel.close()


def test_unavailable_source_explains_why_add_is_disabled(app, tmp_path):
    text = "new_vehicle 1\n" + EFFECT + "\nend\n"
    path = tmp_path / "Vehicles.cfg"
    path.write_text(text, encoding="utf-8")
    document, block = _document(text)
    panel = DestructionFxPanel()
    try:
        panel.set_context(fx.CONTEXT_VEHICLE, document)
        panel.set_script_sources([(path, block.start_line + 1, block.end_line + 1)])
        assert not panel.effects_view.add_button.isEnabled()
        assert "stale" in panel.effects_view.add_button.toolTip()
        assert "Editing unavailable" in panel.reload_note.text()
        assert path.read_text(encoding="utf-8") == text
    finally:
        panel.close()


def test_delete_effect_keeps_the_modify_block_editable(app, tmp_path):
    from entity_designer import fx_visuals

    text = ("new_vehicle 1\nname = Target\n" + EFFECT + "\nend\n"
            "modify_vehicle 1\nenergy = 20\nend\n"
            "new_vehicle 2\nname = Other\nend\n")
    path = tmp_path / "Vehicles.cfg"
    path.write_text(text, encoding="utf-8")
    blocks = find_script_blocks(text)
    document, _ = _document(text)
    panel = DestructionFxPanel()
    try:
        panel.set_context(fx.CONTEXT_VEHICLE, document)
        panel.set_script_sources([(path, b.start_line, b.end_line) for b in blocks[:2]],
                                 expected=("new_vehicle", 1))
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            assert panel.delete_effects([0])
        panel.state_session.set_draft(fx_visuals.MODEL_INDEX, "vp_normal = 211")
        panel.save_rows([fx_visuals.MODEL_INDEX])
        saved = path.read_text(encoding="utf-8")
        updated = find_script_blocks(saved)
        lines = saved.splitlines()
        second = "\n".join(lines[updated[1].start_line:updated[1].end_line + 1])
        assert "vp_normal = 211" in second
        assert saved.endswith("new_vehicle 2\nname = Other\nend\n")
    finally:
        panel.close()


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
        row = _item(panel, 0)
        checkbox = panel.effects_list.checkbox_rect(row).center()
        QTest.mouseClick(panel.effects_list.viewport(), Qt.MouseButton.LeftButton, pos=checkbox)
        assert not panel.code_drawer.expanded
        assert panel.hidden_items == {0}
        point = panel.effects_list.visualItemRect(row).center()
        QTest.mouseClick(panel.effects_list.viewport(), Qt.MouseButton.LeftButton, pos=point)
        drawer = _open_drawer(panel, app)
        assert drawer.expanded and drawer.isVisible()
        # Nothing edited yet: the Save button is grayed out.
        assert not drawer.code.save_button.isEnabled()
        assert not drawer.code.save_button.isEnabled()
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
        assert not drawer.code.save_button.isEnabled()
        drawer.code.setPlainText(EFFECT.replace("400", "650"))
        QTest.qWait(300)
        assert drawer.code.save_button.isEnabled()
        window.activateWindow()
        drawer.code.editor.setFocus()
        app.processEvents()
        QTest.keyClick(drawer.code.editor, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
        assert "duration = 650" in path.read_text()
        assert not panel.has_unsaved_text
        QTest.qWait(200)
        assert not drawer.expanded and not drawer.isVisible()
    finally:
        window.close()


def test_save_and_reload_effect_closes_editor_and_refreshes_preview(app, tmp_path):
    text = "new_vehicle 1\n" + EFFECT + "\nend\n"
    path = tmp_path / "vehicle.cfg"
    path.write_text(text, encoding="utf-8")
    document, block = _document(text)
    panel = DestructionFxPanel()
    window = _in_wide_window(panel, app)
    try:
        panel.set_context(fx.CONTEXT_VEHICLE, document)
        panel.set_script_sources([(path, 0, block.end_line)])
        reloaded = []

        def reload_from_disk():
            saved = path.read_text(encoding="utf-8")
            refreshed, source = _document(saved)
            panel.set_context(fx.CONTEXT_VEHICLE, refreshed, keep_view=True)
            panel.set_script_sources([(path, 0, source.end_line)])
            reloaded.append(saved)

        panel.visualsSaved.connect(reload_from_disk)
        panel.edit_effect_code(0)
        drawer = _open_drawer(panel, app)
        assert drawer.code.save_button.text() == "Save and Reload"
        drawer.code.setPlainText(EFFECT.replace("400", "650"))
        assert drawer._save()
        assert reloaded and "duration = 650" in reloaded[0]
        assert panel.document.items[0].value("duration") == "650"
        assert not drawer.expanded
    finally:
        window.close()


def test_save_keeps_other_effect_drafts_when_reload_is_deferred(app, tmp_path):
    from entity_designer.editor import CollisionEditorWindow
    from entity_designer.script_model import VehicleModelReference

    text = "new_vehicle 1\n" + EFFECT + "\n" + EFFECT.replace("129", "130") + "\nend\n"
    path = tmp_path / "vehicle.cfg"
    path.write_text(text, encoding="utf-8")
    document, block = _document(text)
    window = CollisionEditorWindow()
    try:
        reference = VehicleModelReference(block=block, vp_normal=10)
        window._active_script_path, window._active_script_id = path, 1
        window._active_script_kind, window._active_model_reference = "new_vehicle", reference
        window._load_fx_context(text, block, reference)
        panel = window.fx_panel
        panel.edit_effect_code(1)
        drawer = panel.code_drawer
        second_draft = drawer.code.toPlainText().replace("400", "900")
        drawer.code.setPlainText(second_draft)
        panel.edit_effect_code(0)
        drawer.code.setPlainText(drawer.code.toPlainText().replace("400", "650"))
        assert drawer._save()
        assert "duration = 650" in path.read_text(encoding="utf-8")
        assert "duration = 900" not in path.read_text(encoding="utf-8")
        assert panel.text_session.drafts[1] == second_draft
        assert panel.has_unsaved_text and not drawer.expanded
    finally:
        if window.fx_panel.text_session.dirty:
            window.fx_panel.text_session.revert(1)
        window._set_modified(False)
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
        assert _effect_rows(panel) == 2
        box = panel.effects_box
        corner = box.mapTo(window, box.rect().topLeft())
        assert tab.isVisible() and tab.x() < corner.x() < tab.x() + tab.width()
        assert abs(tab.geometry().center().y() - (corner.y() + box.height() // 2)) <= 1
        assert not drawer.isVisible()
        # The tab opens the editor on the selected effect ...
        _set_row(panel, 1)
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


# -- deleting effects and the cross-check that every change reaches the script ----------------

THREE = "\n".join(
    "begin_fx\n    trigger = destroyed\n    mode = visual\n    vp_model = %d\n    duration = %d\nend" % (vp, ms)
    for vp, ms in ((129, 400), (130, 500), (131, 600)))


def _three_effects(tmp_path, newline="\n"):
    text = ("; keep me\nnew_vehicle 1\nname = A\n" + THREE + "\nend\n\nnew_vehicle 2\nenergy = 9\nend\n")
    path = tmp_path / "vehicles.cfg"
    path.write_bytes(text.replace("\n", newline).encode("latin-1"))
    document, block = _document(text)
    return path, text, document, block


def test_session_delete_removes_only_the_chosen_effects_and_keeps_drafts(tmp_path):
    for newline in ("\n", "\r\n"):
        path, text, document, block = _three_effects(tmp_path, newline)
        session = FxTextSession(document, fx.CONTEXT_VEHICLE, [(path, block.start_line, block.end_line)])
        assert not session.source_error
        # An unsaved edit of the last effect must survive the deletion of the first.
        session.set_draft(2, session.drafts[2].replace("600", "650"))
        assert session.delete([0]) == {path}
        written = path.read_bytes().decode("latin-1").replace("\r\n", "\n")
        assert written == text.replace(effect_text(document.items[0]) + "\n", "", 1)
        assert "vp_model = 129" not in written and "energy = 9" in written and "; keep me" in written
        assert len(session.originals) == 2 and session.drafts[1].count("650") == 1
        assert session.is_dirty(1) and not session.is_dirty(0)
        # Later saves and deletes still land on the right lines.
        session.save([1])
        assert "duration = 650" in path.read_text(encoding="latin-1")
        session.delete([0])
        final = path.read_bytes().decode("latin-1").replace("\r\n", "\n")
        assert "vp_model = 130" not in final and "vp_model = 131" in final
        session.delete([0])
        assert "begin_fx" not in path.read_text(encoding="latin-1")
        assert "name = A" in path.read_text(encoding="latin-1") and not session.originals
        assert path.read_bytes().count(b"\r\n") == (path.read_bytes().count(b"\n") if newline == "\r\n" else 0)


def test_session_delete_refuses_files_changed_outside_and_detects_wrong_spans(tmp_path):
    path, text, document, block = _three_effects(tmp_path)
    session = FxTextSession(document, fx.CONTEXT_VEHICLE, [(path, block.start_line, block.end_line)])
    path.write_text(text + "; external\n", encoding="latin-1")
    with pytest.raises(ValueError, match="changed outside"):
        session.delete([1])
    assert path.read_text(encoding="latin-1") == text + "; external\n"
    # The read-back check notices an effect that is not where the session believes.
    path.write_text(text, encoding="latin-1")
    fresh = FxTextSession(document, fx.CONTEXT_VEHICLE, [(path, block.start_line, block.end_line)])
    fresh.sources[1].first += 1
    fresh.sources[1].last += 1
    with pytest.raises(RuntimeError, match="does not contain the effects"):
        fresh._check_spans(fresh.sources, fresh.originals)


def test_delete_button_and_menu_remove_effects_from_the_script_and_the_preview(app, tmp_path):
    path, text, document, block = _three_effects(tmp_path)
    panel = DestructionFxPanel()
    window = _in_wide_window(panel, app)
    try:
        panel.set_context(fx.CONTEXT_VEHICLE, document)
        panel.set_script_sources([(path, block.start_line, block.end_line)])
        view = panel.effects_view
        assert not view.delete_button.isEnabled()
        # The list keeps its room: the five buttons are small.
        assert view.delete_button.height() >= view.show_all_button.height() - 4
        panel.hide_all_button.click()
        _set_row(panel, 1)
        panel.edit_effect_code(2)
        _open_drawer(panel, app)
        _set_row(panel, 1)
        assert view.delete_button.isEnabled()
        # Cancel: nothing changes.
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Cancel):
            view.delete_button.click()
        assert path.read_text(encoding="latin-1") == text and len(panel.document.items) == 3
        # Yes: effect 1 leaves the file and the list; the others keep their marks and editor.
        panel.show_all_button.click()
        panel.set_hidden_items({2})
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            view.delete_button.click()
        written = path.read_text(encoding="latin-1")
        assert "vp_model = 130" not in written and written.count("begin_fx") == 2
        assert _effect_rows(panel) == 2 and len(panel.document.items) == 2
        assert panel.hidden_items == {1}
        assert panel.code_drawer.index == 1 and "vp_model = 131" in panel.code_drawer.code.toPlainText()
        assert not panel.has_unsaved_text
        # The editor and the effect list stay in step with the file afterwards.
        panel.code_drawer.code.setPlainText(panel.code_drawer.code.toPlainText().replace("600", "620"))
        QTest.qWait(300)
        assert QTest.keyClick(panel.code_drawer.code.editor, Qt.Key.Key_S,
                              Qt.KeyboardModifier.ControlModifier) is None
        assert "duration = 620" in path.read_text(encoding="latin-1")
        # Menu: the deleted effect that is open in the editor folds the editor away.
        _set_row(panel, 1)
        menu = panel._create_effects_context_menu()
        delete = next(action for action in menu.actions() if action.text() == "Delete Effect")
        assert delete.isEnabled()
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            delete.trigger()
        assert path.read_text(encoding="latin-1").count("begin_fx") == 1
        assert panel.code_drawer.index == -1 and not panel.code_drawer.expanded
        assert "energy = 9" in path.read_text(encoding="latin-1")
    finally:
        window.close()


def test_delete_without_a_script_source_changes_nothing(app):
    document, _ = _document("new_vehicle 1\n" + THREE + "\nend\n")
    panel = DestructionFxPanel()
    try:
        panel.set_context(fx.CONTEXT_VEHICLE, document)
        with patch.object(QMessageBox, "warning") as warned, \
                patch.object(QMessageBox, "question") as asked:
            assert not panel.delete_effects([0])
        assert warned.called and not asked.called and len(panel.document.items) == 3
    finally:
        panel.close()


def test_effect_code_editor_has_no_transport_or_save_rows(app):
    document, _ = _document("new_vehicle 1\n" + THREE + "\nend\n")
    panel = DestructionFxPanel()
    window = _in_wide_window(panel, app)
    try:
        panel.set_context(fx.CONTEXT_VEHICLE, document)
        panel.edit_effect_code(0)
        drawer = _open_drawer(panel, app)
        # Under the code only the line / column info is left; saving uses the toolbar.
        for name in ("play_button", "speed_combo", "revert_button", "save_button"):
            assert not hasattr(drawer, name)
        assert drawer.code.status_label.text().startswith("Line ")
        # Reset lives in the toolbar with Undo, Redo, Find, Replace and Save.
        assert drawer.reset_button.text() == "Reset" and not hasattr(drawer, "save_all_button")
        assert drawer.code.undo_button.text() == "< Undo" and drawer.code.redo_button.text() == "Redo >"
        assert not drawer.reset_button.isEnabled()
    finally:
        panel.close()


def test_add_button_writes_a_new_effect_whose_type_comes_from_its_text(app, tmp_path):
    text = "new_vehicle 1\n    name = Test\n" + EFFECT + "\nend\nnew_vehicle 2\nend\n"
    path = tmp_path / "vehicle.cfg"
    path.write_text(text, encoding="utf-8")
    document, block = _document(text)
    panel = DestructionFxPanel()
    window = _in_wide_window(panel, app)
    try:
        view = panel.effects_view
        # Without a script file there is nowhere to write a new effect.
        assert not view.add_button.isEnabled()
        panel.set_context(fx.CONTEXT_VEHICLE, document)
        panel.set_script_sources([(path, 0, block.end_line)])
        app.processEvents()
        assert view.add_button.isEnabled()

        view.add_button.click()
        drawer = _open_drawer(panel, app)
        assert len(panel.document.items) == 2 and drawer.expanded
        row = _item(panel, 1)
        assert row.text().startswith("New effect") and "Empty" in row.text()
        assert not drawer.code.save_button.isEnabled()             # nothing written yet
        assert path.read_text(encoding="utf-8") == text

        # A sound-event shake: the row becomes a Shake row.
        drawer.code.setPlainText("    shk_explode_slot = 1\n    shk_explode_time = 300")
        QTest.qWait(300)
        assert _item(panel, 1).text().startswith("Shake · explode")
        assert drawer.code.save_button.isEnabled()
        # A begin_fx block: the same row becomes a Visual row.
        drawer.code.setPlainText(EFFECT.replace("destroyed", "air_destroyed"))
        QTest.qWait(300)
        assert _item(panel, 1).text().startswith("Visual · air_destroyed")
        drawer.code.save_button.click()
        saved = path.read_text(encoding="utf-8")
        # Written inside the first definition, right before its own end.
        assert saved == text.replace(
            "    duration = 400\nend\nend\n",
            "    duration = 400\nend\n" + EFFECT.replace("destroyed", "air_destroyed")
            + "\nend\n", 1)
        assert not drawer.code.save_button.isEnabled() and not panel.has_unsaved_text

        # A second new effect goes after the first one; an unsaved one is deleted silently.
        view.add_button.click()
        assert len(panel.document.items) == 3
        _set_row(panel, 2)
        with patch.object(QMessageBox, "question") as ask:
            view.delete_button.click()
            assert not ask.called
        assert len(panel.document.items) == 2
        assert path.read_text(encoding="utf-8") == saved
    finally:
        panel.close()
        window.close()


def test_new_effect_crater_text_can_be_saved_and_reloaded(app, tmp_path):
    from entity_designer.fx_visuals import CRATER_INDEX, StateSession

    text = "new_weapon 1\n    name = Test\n    energy = 500\nend\n"
    path = tmp_path / "Weapons.cfg"
    path.write_text(text, encoding="utf-8")
    document, block = _document(text)
    panel = DestructionFxPanel()
    window = _in_wide_window(panel, app)
    try:
        panel.set_context(fx.CONTEXT_WEAPON, document)
        panel.set_script_sources([(path, 0, block.end_line)])
        panel.effects_view.add_button.click()
        drawer = _open_drawer(panel, app)
        draft = "crater_radius = 300~400\ncrater_depth = 150~200\ncrater_rim = 15~20"
        drawer.code.setPlainText(draft)
        QTest.qWait(300)
        assert drawer.index == CRATER_INDEX
        assert drawer.code.toPlainText() == draft
        assert drawer.code.save_button.isEnabled()
        drawer.code.save_button.click()
        saved = path.read_text(encoding="utf-8")
        assert "name = Test" in saved and "energy = 500" in saved
        assert all(line in saved for line in draft.splitlines())
        assert saved.count("crater_radius") == 1
        end = find_script_blocks(saved)[0].end_line
        reopened = StateSession([(path, 0, end)])
        assert reopened.originals[CRATER_INDEX] == draft
        assert not panel.has_unsaved_text
    finally:
        panel.close()
        window.close()


@pytest.mark.parametrize("original,draft,expected", [
    ("    crater_radius = 100\n    crater_depth = 40\n", "crater_rim = 15~20", "crater_radius = 100\ncrater_depth = 40\ncrater_rim = 15~20"),
    ("    crater_radius = 100\n    crater_depth = 40\n", "crater_radius = 300", "crater_radius = 300\ncrater_depth = 40"),
    ("    ground_decal_id = 2\n", "ground_decal_size = 120~150", "ground_decal_id = 2\nground_decal_size = 120~150"),
    ("    vp_normal = 20\n", "vp_genesis = 30", "vp_normal = 20\n\nvp_genesis = 30"),
])
def test_new_effect_routes_state_keys_and_preserves_existing_values(app, tmp_path, original, draft, expected):
    from entity_designer import fx_visuals

    text = "new_weapon 1\n" + original + "end\n"
    path = tmp_path / "Weapons.cfg"
    path.write_text(text, encoding="utf-8")
    document, block = _document(text)
    panel = DestructionFxPanel()
    window = _in_wide_window(panel, app)
    try:
        panel.set_context(fx.CONTEXT_WEAPON, document)
        panel.set_script_sources([(path, 0, block.end_line)])
        panel.add_effect()
        drawer = _open_drawer(panel, app)
        drawer.code.setPlainText(draft)
        QTest.qWait(300)
        assert drawer.index in fx_visuals.STATE_INDEXES
        assert drawer.code.save_button.isEnabled()
        drawer.code.save_button.click()
        saved = path.read_text(encoding="utf-8")
        end = find_script_blocks(saved)[0].end_line
        reopened = fx_visuals.StateSession([(path, 0, end)])
        assert reopened.originals[drawer.index] == expected
    finally:
        panel.close()
        window.close()


def test_one_new_effect_can_fill_several_state_rows(app, tmp_path):
    from entity_designer import fx_visuals

    text = "new_weapon 1\n    name = Test\nend\n"
    path = tmp_path / "Weapons.cfg"
    path.write_text(text, encoding="utf-8")
    document, block = _document(text)
    panel = DestructionFxPanel()
    window = _in_wide_window(panel, app)
    try:
        panel.set_context(fx.CONTEXT_WEAPON, document)
        panel.set_script_sources([(path, 0, block.end_line)])
        panel.add_effect()
        drawer = _open_drawer(panel, app)
        drawer.code.setPlainText("crater_radius = 300\ncrater_depth = 100\n"
                                 "ground_decal_id = 2\nvp_normal = 20")
        QTest.qWait(300)
        # The editor keeps the first row, the other rows hold their own lines.
        assert drawer.index == fx_visuals.CRATER_INDEX
        assert drawer.code.toPlainText() == "crater_radius = 300\ncrater_depth = 100"
        assert panel.state_session.drafts[fx_visuals.DECAL_INDEX] == "ground_decal_id = 2"
        assert panel.state_session.drafts[fx_visuals.MODEL_INDEX] == "vp_normal = 20"
        assert not panel.document.items
        drawer.code.save_button.click()
        saved = path.read_text(encoding="utf-8")
        assert all(line in saved for line in
                   ("crater_radius = 300", "ground_decal_id = 2", "vp_normal = 20"))
        assert not panel.has_unsaved_text
    finally:
        panel.close()
        window.close()


@pytest.mark.parametrize("context,draft,message", [
    (fx.CONTEXT_WEAPON, "future_param = 5", "future_param is not an effect"),
    (fx.CONTEXT_VEHICLE, "snd_future_sample = a.wav", "unknown sound event"),
    (fx.CONTEXT_VEHICLE, "vp_normal = 5\nsnd_explode_sample = a.wav", "own effect"),
])
def test_new_effect_explains_what_it_cannot_take(app, tmp_path, context, draft, message):
    kind = "weapon" if context == fx.CONTEXT_WEAPON else "vehicle"
    text = f"new_{kind} 1\n    name = Test\nend\n"
    path = tmp_path / "x.cfg"
    path.write_text(text, encoding="utf-8")
    document, block = _document(text)
    panel = DestructionFxPanel()
    window = _in_wide_window(panel, app)
    try:
        panel.set_context(context, document)
        panel.set_script_sources([(path, 0, block.end_line)])
        panel.add_effect()
        drawer = _open_drawer(panel, app)
        drawer.code.setPlainText(draft)
        QTest.qWait(300)
        assert message in drawer.code._error_message
        assert not drawer.code.save_button.isEnabled()
        assert path.read_text(encoding="utf-8") == text
    finally:
        panel.close()
        window.close()


def test_new_state_key_does_not_replace_an_existing_unsaved_draft(app, tmp_path):
    from entity_designer.fx_visuals import CRATER_INDEX

    path = tmp_path / "Weapons.cfg"
    text = "new_weapon 1\n    name = Test\nend\n"
    path.write_text(text, encoding="utf-8")
    document, block = _document(text)
    panel = DestructionFxPanel()
    window = _in_wide_window(panel, app)
    try:
        panel.set_context(fx.CONTEXT_WEAPON, document)
        panel.set_script_sources([(path, 0, block.end_line)])
        previous = "crater_radius = 100\ncrater_depth = 40"
        panel.state_session.set_draft(CRATER_INDEX, previous)
        panel.add_effect()
        drawer = _open_drawer(panel, app)
        drawer.code.setPlainText("crater_radius = 300")
        QTest.qWait(300)
        assert drawer.index == 0 and not drawer.code.save_button.isEnabled()
        assert drawer.code.toPlainText() == "crater_radius = 300"
        assert panel.state_session.drafts[CRATER_INDEX] == previous
        assert path.read_text(encoding="utf-8") == text
    finally:
        panel.close()
        window.close()


@pytest.mark.parametrize("draft", [
    EFFECT,
    EFFECT.replace("    duration = 400", "    duration = 400\n    future_parameter = 123"),
    "    dest_fx = death_46_0_0_0",
    "    snd_explode_sample = sounds/engine.wav\n    snd_explode_volume = 100",
    "    pal_explode_slot = 1\n    pal_explode_time = 300",
    "    shk_explode_slot = 1\n    shk_explode_time = 300",
])
def test_add_effect_saves_and_reloads_supported_or_unknown_block_keys(app, tmp_path, draft):
    path = tmp_path / "Vehicles.cfg"
    text = "new_vehicle 1\n    name = Test\nend\n"
    path.write_text(text, encoding="utf-8")
    document, block = _document(text)
    panel = DestructionFxPanel()
    window = _in_wide_window(panel, app)
    try:
        panel.set_context(fx.CONTEXT_VEHICLE, document)
        panel.set_script_sources([(path, 0, block.end_line)])
        panel.add_effect()
        drawer = _open_drawer(panel, app)
        drawer.code.setPlainText(draft)
        QTest.qWait(300)
        assert drawer.code.save_button.isEnabled()
        drawer.code.save_button.click()
        saved = path.read_text(encoding="utf-8")
        reopened, _ = _document(saved)
        assert len(reopened.items) == 1
        assert effect_text(reopened.items[0]) == draft
        assert "name = Test" in saved
    finally:
        panel.close()
        window.close()


def test_two_new_effects_are_saved_together_in_list_order(tmp_path):
    text = "new_vehicle 1\n" + EFFECT + "\nend\n"
    path = tmp_path / "vehicle.cfg"
    path.write_text(text, encoding="utf-8")
    document, block = _document(text)
    session = FxTextSession(document, fx.CONTEXT_VEHICLE, [(path, 0, block.end_line)])
    first, second = session.add_blank(), session.add_blank()
    assert (first, second) == (1, 2) and session.is_pending(first)
    assert not session.dirty                      # blank effects have nothing to save
    session.set_draft(first, "    shk_explode_slot = 1")
    session.set_draft(second, "    pal_explode_slot = 2")
    session.save([first, second])
    assert path.read_text(encoding="utf-8") == (
        "new_vehicle 1\n" + EFFECT + "\n    shk_explode_slot = 1\n    pal_explode_slot = 2\nend\n")
    assert not session.is_pending(first) and session.end_line[1] == 9
    # Later saves keep working on the moved lines.
    session.set_draft(first, "    shk_explode_slot = 1\n    shk_explode_time = 99")
    session.save([first])
    assert "shk_explode_time = 99\n    pal_explode_slot = 2\nend" in path.read_text(encoding="utf-8")

@pytest.mark.parametrize("context,kind,trigger", [
    (fx.CONTEXT_VEHICLE, "vehicle", "destroyed"),
    (fx.CONTEXT_WEAPON, "weapon", "detonate"),
])
@pytest.mark.parametrize("position", [0, 2, 4, 6, 7])
@pytest.mark.parametrize("values,expected", [
    ([], None),
    (["255|231|8|255"], (255, 231, 8, 255)),
    (["255|231|8|255", "8|231|255|128"], (8, 231, 255, 128)),
    (["invalid"], (255, 255, 255, 255)),
    (["999|-3|8"], (255, 0, 8, 255)),
])
def test_visual_block_tint_is_shared_in_preview_and_saved_text(
        tmp_path, context, kind, trigger, position, values, expected):
    rows = ["mode = visual", "duration = 900", "vp_model = 1",
            "vp_model = 2", "base_model = Data/Models/Base/FX.BASE",
            "3ds_model = Data/Models/3ds/FX.3ds", "vp_model = 3"]
    rows[position:position] = ["tint = " + value for value in values]
    effect = "begin_fx\ntrigger = " + trigger + "\n" + "\n".join(rows) + "\nend"
    item, issues = parse_effect_text(effect, context)
    assert not issues
    spec = fx.interpret_block(item, context)
    assert spec.active and len(spec.visuals) == 3
    assert all(visual.tint == expected for visual in spec.visuals)
    # The editor keeps the user's code, including tint placement and comments.
    text = f"new_{kind} 1\n{EFFECT.replace('destroyed', trigger)}\nend\n"
    path = tmp_path / "effects.cfg"
    path.write_text(text, encoding="utf-8")
    document, block = _document(text)
    session = FxTextSession(document, context, [(path, 0, block.end_line)])
    session.set_draft(0, effect)
    session.save([0])
    saved, _ = _document(path.read_text(encoding="utf-8"))
    assert effect_text(saved.items[0]) == effect
    simulation = fx.PreviewSimulation(saved, context, trigger)
    for time in (10, 350, 700):
        rendered = [item for item in simulation.items_at(time) if item.kind == "visual"]
        assert len(rendered) == 1
        assert rendered[0].tint == expected
