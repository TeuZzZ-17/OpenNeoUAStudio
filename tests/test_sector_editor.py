"""Native set round trips, independent cells, complete packages and workspace wiring."""

import os
import subprocess
import tempfile
from copy import deepcopy
from dataclasses import replace
from zipfile import ZipFile
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("NME_RENDERER", "software")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from map_editor import bootstrap
from map_editor.core.set_sdf_parser import (
    SetSdf, SdfBuilding, SdfBuildingModel, SdfSector, serialize_set_sdf,
    parse_set_sdf_text,
)
from sector_editor.document import SetDocument, SET_FOLDERS, table_issues, MODEL_LIMIT, BUILDING_LIMIT


@pytest.fixture
def document(tmp_path):
    root = tmp_path / "Set8"
    for name in SET_FOLDERS:
        folder = root / name
        folder.mkdir(parents=True)
        (folder / "sample.bin").write_bytes(name.encode())
    (root / "Objects" / "SET.BAS").write_bytes(b"fixture archive")
    sdf = SetSdf(
        models=[SdfBuildingModel(i, name, f"skeleton/{name[:-5]}.sklt", (0, 1, 50, 0), (), "Modello è")
                for i, name in enumerate(("A.base", "B.base", "dummy.base"))],
        buildings=[SdfBuilding(0, 0, 0, 0, 0, 1), SdfBuilding(1, 1, 1, 1, 1, 1)],
        sectors={5: SdfSector(5, 0, 1, 73, (0,) * 9, "Originale"),
                 9: SdfSector(9, 1, 0, 1, (0,), "Single")})
    raw = ("; appunti originali\r\n" + serialize_set_sdf(sdf).replace("\n", "\r\n") +
           "; note finali da preservare\r\n").encode("cp1252")
    (root / "Scripts" / "SET.SDF").write_bytes(raw)
    doc = SetDocument.open(root)
    doc.embedded_names = ["A", "B", "dummy"]
    # These output tests use small placeholder assets. Real asset validation is
    # exercised by the workspace test against the installed set below.
    doc.validate_resources = lambda: None
    doc._generated_base_files = lambda overrides: {}
    return doc


def test_lossless_existing_rows_and_notes(document):
    assert document.encoded() == document.source
    original = document.source.splitlines()[1]
    document.assign_building(5, {4}, 1)
    data = document.encoded()
    assert original in data.splitlines()
    assert data.startswith(b"; appunti originali\r\n")
    assert data.endswith(b"; note finali da preservare\r\n")
    assert b"\r\r\n" not in data
    assert parse_set_sdf_text(data.decode("cp1252")).sectors[5].subsects[4] == 1


def test_partial_retail_effect_keeps_supplied_coordinates():
    text = "A.base skeleton/A.sklt 0 1 0 0 81 23\n>\n0 0 0 0 0\n>\n1 1 0 1 0\n>\n"
    sdf = parse_set_sdf_text(text)
    assert sdf.models[0].effects == ((81, 23, 0, 0),)
    assert serialize_set_sdf(sdf, text) == text


def test_independent_cell_states_do_not_modify_shared_building(document):
    original = deepcopy(document.sdf.buildings[0])
    document.assign_model(5, {0, 8}, 1, state=1)
    assert document.sdf.buildings[0] == original
    cells = document.sdf.sectors[5].subsects
    assert cells[1:8] == (0,) * 7 and cells[0] == cells[8]
    clone = document.sdf.buildings[cells[0]]
    assert (clone.intact, clone.damaged, clone.heavy, clone.flat) == (0, 1, 0, 0)
    assert document.sdf.sectors[9].subsects == (0,)
    document.undo()
    assert not document.dirty
    document.redo()
    assert document.sdf.sectors[5].subsects == cells


def test_free_slots_duplication_layout_and_undo(document):
    assert document.free_sector() == 1
    number = document.add_sector(5)
    assert number == 1 and document.sdf.sectors[1].subsects == (0,) * 9
    document.assign_building(number, {4}, 1)
    document.set_layout(number, True)
    assert document.sdf.sectors[number].subsects == (1,)
    document.undo()
    assert document.sdf.sectors[number].subsects == (0, 0, 0, 0, 1, 0, 0, 0, 0)
    with pytest.raises(ValueError):
        document.add_sector(number=5)
    with pytest.raises(ValueError):
        document.add_sector(number=256)


def test_dummy_model_slot_reused_and_new_model_gets_runtime_override(document):
    slot = document.add_model("B.base", "skeleton/B.sklt")
    assert slot == 2 and len(document.sdf.models) == 3
    assert document.override_bytes() == b"lego 2 B.base\n"
    document.save()
    assert document.override_path.read_bytes() == b"lego 2 B.base\n"
    reopened = SetDocument.open(document.folder)
    assert reopened.sdf.models[slot].base == "B.base"


def test_save_detects_external_edits_without_overwriting(document):
    document.assign_building(5, {1}, 1)
    document.sdf.path.write_bytes(b"external change")
    with pytest.raises(ValueError, match="outside this editor"):
        document.save()
    assert document.sdf.path.read_bytes() == b"external change"


def test_existing_native_visual_overrides_remain_effective(document):
    document.override_path.write_bytes(b"; keep note\nvhcl 2 Other.base\nlego 0 B.base\n")
    reopened = SetDocument.open(document.folder)
    reopened.embedded_names = ["A", "B", "dummy"]
    assert not reopened.dirty and reopened.sdf.models[0].base == "B.base"
    output = reopened.override_bytes()
    assert b"; keep note\nvhcl 2 Other.base\n" in output
    assert b"lego 0 B.base\n" in output
    reopened.sdf.models[0] = replace(reopened.sdf.models[0], base="A.base")
    assert b"lego 0 " not in reopened.override_bytes()


def test_complete_zip_contains_unsaved_tables_and_every_resource(document, tmp_path):
    document.assign_model(5, {4}, 1, all_states=True)
    original = document.sdf.path.read_bytes()
    target = tmp_path / "Set8.zip"
    document.export_zip(target)
    with ZipFile(target) as archive:
        for name in SET_FOLDERS:
            assert archive.read(f"Data/Sets/Set8/{name}/sample.bin") == name.encode()
        sdf = parse_set_sdf_text(archive.read("Data/Sets/Set8/Scripts/SET.SDF").decode("cp1252"))
        assert sdf.sectors[5].subsects[4] == 1
        assert archive.testzip() is None
    assert document.sdf.path.read_bytes() == original and document.dirty
    with pytest.raises(ValueError, match="outside"):
        document.export_zip(document.folder / "bad.zip")


def test_copy_set_is_independent_and_never_overwrites(document, tmp_path):
    document.assign_building(5, {4}, 1)
    copied = document.copy_to(tmp_path / "Set9")
    assert copied.number == 9 and copied.sdf.sectors[5].subsects[4] == 1
    assert SetDocument.open(document.folder).sdf.sectors[5].subsects[4] == 0
    with pytest.raises(FileExistsError):
        document.copy_to(tmp_path / "Set9")


def test_new_set_cannot_recursively_copy_a_source_folder_into_itself(document):
    with pytest.raises(ValueError, match="outside all selected source folders"):
        SetDocument.create(document.folder / "HI/Set9",
                           {name: document.folder / name for name in SET_FOLDERS})


def test_extended_tables_retain_large_model_ids(document):
    for index in range(len(document.sdf.models), MODEL_LIMIT):
        document.sdf.models.append(replace(document.sdf.models[0], id=index))
    for index in range(len(document.sdf.buildings), BUILDING_LIMIT):
        document.sdf.buildings.append(SdfBuilding(index, 4095, 4094, 300, 256, 1))
    document.sdf.sectors[9] = replace(document.sdf.sectors[9], subsects=(4095,))
    assert not table_issues(document.sdf)
    encoded = document.encoded()
    sdf = parse_set_sdf_text(encoded.decode("cp1252"))
    assert sdf.buildings[4095].intact == 4095 and sdf.sectors[9].subsects == (4095,)
    document.sdf.models.append(replace(document.sdf.models[0], id=4096))
    assert any("Too many models" in issue for issue in table_issues(document.sdf))


def test_failed_multi_cell_edit_rolls_back_all_tables(document):
    before = deepcopy(document.sdf)
    with patch.object(document, "_building_slot", side_effect=[2, ValueError("full")]):
        with pytest.raises(ValueError):
            document.assign_model(5, {0, 1}, 1)
    assert document.sdf == before and not document._undo


def test_native_definition_exchange_remaps_all_four_states_and_undo(document, tmp_path):
    document.assign_model(5, {4}, 1, state=1)
    original = deepcopy(document.sdf)
    target = tmp_path / "selected.sdf"
    document.export_definitions([5, 9], target)
    subset = parse_set_sdf_text(target.read_text(encoding="utf-8"))
    assert set(subset.sectors) == {5, 9}
    assert len(subset.models) == 2 and not table_issues(subset)
    imported = document.import_definitions(target)
    assert imported == [1, 2]
    row = document.sdf.buildings[document.sdf.sectors[1].subsects[4]]
    assert (row.intact, row.damaged, row.heavy, row.flat) == (0, 1, 0, 0)
    assert len(document.sdf.models) == len(original.models)
    assert document.sdf.sectors[2].single
    document.undo()
    assert document.sdf == original
    document.redo()
    assert document.sdf.sectors[1].subsects == document.sdf.sectors[5].subsects
    with pytest.raises(ValueError, match="separate file"):
        document.export_definitions([5], document.sdf.path)


def test_import_reserves_existing_dummy_models_needed_later(document, tmp_path):
    dummy = document.sdf.models[2]
    source = SetSdf(models=[replace(document.sdf.models[1], id=0, numbers=(0, 99, 20, 0)),
                           replace(dummy, id=1)],
                    buildings=[SdfBuilding(0, 0, 0, 0, 0, 1), SdfBuilding(1, 1, 1, 1, 1, 1)],
                    sectors={5: SdfSector(5, 0, 0, 1, (0, 1, 0, 1, 0, 1, 0, 1, 0))})
    target = tmp_path / "import.sdf"
    target.write_text(serialize_set_sdf(source), encoding="utf-8")
    number = document.import_definitions(target)[0]
    assert document.sdf.models[2] == dummy
    assert len(document.sdf.models) == 4
    cells = document.sdf.sectors[number].subsects
    assert document.sdf.buildings[cells[0]].intact == 3
    assert document.sdf.buildings[cells[1]].intact == 2


def test_import_resource_failure_rolls_back_the_whole_merge(document, tmp_path):
    target = tmp_path / "import.sdf"
    document.export_definitions([5], target)
    before = deepcopy(document.sdf)
    with patch.object(document, "validate_resources", side_effect=ValueError("Missing BASE")):
        with pytest.raises(ValueError, match="Missing BASE"):
            document.import_definitions(target)
    assert document.sdf == before and not document._undo


def test_pending_dummy_building_rows_are_not_overwritten(document):
    document.sdf.models = [replace(m, base="dummy.base") for m in document.sdf.models]
    document.sdf.sectors[5] = replace(document.sdf.sectors[5], subsects=(0, 1) + (0,) * 7)
    document.assign_model(5, [0, 1], 2)
    cells = document.sdf.sectors[5].subsects
    assert cells[0] != cells[1]
    assert document.sdf.buildings[cells[0]].damaged == 0
    assert document.sdf.buildings[cells[1]].damaged == 1


def test_restore_saved_layout_can_be_undone(document):
    original = document.sdf.sectors[5]
    document.assign_building(5, {0}, 1)
    edited = document.sdf.sectors[5]
    document.restore_sector(5)
    assert document.sdf.sectors[5] == original
    document.undo()
    assert document.sdf.sectors[5] == edited
    number = document.add_sector(5)
    document.restore_sector(number)
    assert number not in document.sdf.sectors


def test_independent_state_assignment_validates_before_changing_tables(document):
    before = deepcopy(document.sdf)
    document.assign_states(5, {0, 8}, (0, 1, 2, 0), 0)
    cells = document.sdf.sectors[5].subsects
    row = document.sdf.buildings[cells[0]]
    assert cells[0] == cells[8] and cells[4] == 0
    assert (row.intact, row.damaged, row.heavy, row.flat, row.destructible) == (0, 1, 2, 0, 0)
    assert document.sdf.buildings[0] == before.buildings[0]
    assert document.sectors_using_model(2) == {5}
    document.undo()
    assert document.sdf == before
    for states in ((0, 1), (0, 1, None, 0), (0, 1, 3, 0)):
        with pytest.raises(ValueError):
            document.assign_states(5, {4}, states, 1)
    assert document.sdf == before


def test_clipboard_keeps_model_definitions_after_model_slot_is_reused(document):
    slot = document.add_model("B.base", "skeleton/B.sklt")
    document.assign_model(5, {0}, slot, all_states=True)
    clipboard = document.copy_cells(5, {0})
    document.undo()
    document.undo()
    assert document.add_model("A.base", "skeleton/A.sklt") == slot
    document.paste_cells(5, {8}, clipboard)
    row = document.sdf.buildings[document.sdf.sectors[5].subsects[8]]
    for state in range(4):
        pasted = document.sdf.models[row.state(state)]
        assert pasted.base == "B.base" and pasted.numbers == (0, 1, 0, 0)
    assert document.sdf.models[slot].base == "A.base"
    assert not table_issues(document.sdf)


def test_clipboard_keeps_original_shared_model_and_pastes_patterns(document):
    document.assign_building(5, {4}, 1)
    clipboard = document.copy_cells(5, {0, 4, 8})
    document.change("Modify shared model", lambda: document.sdf.models.__setitem__(
        0, replace(document.sdf.models[0], base="B.base")))
    document.paste_cells(5, {1, 2, 3}, clipboard)
    cells = document.sdf.sectors[5].subsects
    names = [document.sdf.models[document.sdf.buildings[cells[i]].intact].base for i in (1, 2, 3)]
    assert names == ["A.base", "B.base", "A.base"]
    assert document.sdf.models[document.sdf.buildings[cells[0]].intact].base == "B.base"
    after = deepcopy(document.sdf)
    document.undo()
    document.redo()
    assert document.sdf == after
    with pytest.raises(ValueError):
        document.paste_cells(9, {4}, clipboard)
    assert document.sdf == after


def test_clipboard_failure_rolls_back_all_allocated_dependencies(document):
    slot = document.add_model("B.base", "skeleton/B.sklt")
    document.assign_model(5, {4}, slot, all_states=True)
    clipboard = document.copy_cells(5, {4})
    document.undo()
    document.undo()
    before = deepcopy(document.sdf)
    history = len(document._undo)
    with patch.object(document, "validate_resources", side_effect=ValueError("Missing collision")):
        with pytest.raises(ValueError, match="Missing collision"):
            document.paste_cells(5, {8}, clipboard)
    assert document.sdf == before and len(document._undo) == history


def test_existing_six_game_sets_are_byte_identical():
    try:
        root = bootstrap.game_data_dir() / "Sets"
    except RuntimeError:
        pytest.skip("Game assets not configured")
    for number in range(1, 7):
        doc = SetDocument.open(root / f"Set{number}")
        assert doc.encoded() == doc.source


def test_generated_set_is_read_by_native_engine():
    try:
        data = bootstrap.game_data_dir()
    except RuntimeError:
        pytest.skip("Game assets not configured")
    executable = data / "Build/probes/sector_tables_test.exe"
    if not executable.is_file():
        pytest.skip("Build the native sector_tables_test probe first")
    from pathlib import Path
    with tempfile.TemporaryDirectory(prefix="sector-real-", dir=data / "Build/probes") as scratch:
        document = SetDocument.create(Path(scratch) / "Set12",
                                     {name: data / "Sets/Set1" / name for name in SET_FOLDERS})
        document.add_sector(number=1)
        while len(document.sdf.models) < 257:
            document.add_model("ST_CITY1.base", "skeleton/STcCITY1.sklt", source=3)
        document.assign_model(1, {0, 4, 8}, 256, all_states=True)
        clipboard = document.copy_cells(1, {4})
        document.undo()
        document.paste_cells(1, {0, 4, 8}, clipboard)
        zipped = Path(scratch) / "complete.zip"
        document.export_zip(zipped)
        with ZipFile(zipped) as archive:
            payload = archive.read("Data/Sets/Set12/Objects/ST_CITY1.bas")
            archive.extractall(Path(scratch) / "unpacked")
        document.save()
        assert (document.folder / "Objects/ST_CITY1.bas").read_bytes() == payload
        from map_editor.core.asset_bridge import SetAssets
        assets = SetAssets(12, set_dir=document.folder).load()
        assert assets._key("ST_CITY1.bas") == assets._key("st_city1.base")
        assert assets.base_object("ST_CITY1.base", set_only=True)
        assert assets._key("ST_CITY1.base") in assets._loose_families
        env = dict(os.environ)
        env["PATH"] = "C:/msys64/mingw64/bin;" + env.get("PATH", "")
        unpacked = Path(scratch) / "unpacked/Data/Sets/Set12"
        result = subprocess.run([str(executable), "--real-set", unpacked.relative_to(data).as_posix()],
                                cwd=data.parent, env=env, text=True, capture_output=True, timeout=45)
        (data / "Build/sector-realset-test.log").write_text(result.stdout + result.stderr, encoding="utf-8")
        assert result.returncode == 0, result.stdout + result.stderr


def test_sector_workspace_is_available_in_launcher():
    from studio.ui.shell import StartupToolSelector
    app = QApplication.instance() or QApplication([])
    dialog = StartupToolSelector()
    dialog.tool_list.set_current_key("sector_editor") if hasattr(dialog.tool_list, "set_current_key") else dialog.tool_list.set_current_row(5)
    with patch.object(dialog.resources_panel, "apply", return_value=object()):
        dialog.accept()
    assert dialog.result() == QDialogAccepted
    dialog.close()


QDialogAccepted = 1


def test_real_workspace_edit_preview_and_history():
    try:
        bootstrap.game_data_dir()
    except RuntimeError:
        pytest.skip("Game assets not configured")
    from sector_editor import SectorEditorWindow
    app = QApplication.instance() or QApplication([])
    window = SectorEditorWindow()
    try:
        assert window.document and window.sectors.count() == 256
        window.show()
        window.tabs.setCurrentWidget(window.definitions_page)
        app.processEvents()
        scroll = window.definitions_page
        assert scroll.widget().width() <= scroll.viewport().width()
        assert scroll.horizontalScrollBar().maximum() == 0
        window.tabs.setCurrentWidget(window.cells_page)
        assert not window.document.resource_issues()
        assert window.view.terrain.cell_y(0, 0) == 0
        saved_model = window.document.sdf.models[3]
        window.document.sdf.models[3] = replace(saved_model, base="nonexistent-sector-model.base")
        assert any("missing from this set" in issue for issue in window.document.resource_issues())
        window.document.sdf.models[3] = saved_model
        window.sectors.setCurrentRow(142)
        window.select_cell(0)
        window.select_cell(8, True)
        assert window.selected_cells == {0, 8}
        model = window.document.sdf.models[3]
        window.model_combo.setCurrentIndex(window.model_combo.findData(model.id))
        original = deepcopy(window.document.sdf)
        window.apply_model()
        assert window.document.dirty
        assert window.view.lib.mesh(142).faces
        window.undo()
        assert window.document.sdf == original
        window.redo()
        window._layout_changed(1)
        assert window.document.sdf.sectors[142].single
        assert window.view.lib.mesh(142).resolved.subs[0].index == 4
        window.preview_mode.setCurrentIndex(1)
        assert window.view.lib.model == model.id
    finally:
        if window.document:
            window.document.saved = deepcopy(window.document.sdf)
        window.close()


@pytest.fixture
def real_window():
    try:
        bootstrap.game_data_dir()
    except RuntimeError:
        pytest.skip("Game assets not configured")
    from sector_editor import SectorEditorWindow
    app = QApplication.instance() or QApplication([])
    window = SectorEditorWindow()
    window.show()
    app.processEvents()
    yield window
    window._model_snapshot = None
    window._state_snapshot = None
    window._sector_fields_number = None
    window.document.saved = deepcopy(window.document.sdf)
    window.close()


def test_pending_states_survive_refresh_and_save_without_apply(real_window):
    window = real_window
    window.sectors.setCurrentRow(142)
    window.state_models[1].setCurrentIndex(window.state_models[1].findData(3))
    window._refresh()
    assert window.state_models[1].currentData() == 3
    with patch.object(window.document, "save"):
        assert window.save()
    sector = window.document.sdf.sectors[142]
    assert window.document.sdf.buildings[sector.subsects[4]].damaged == 3
    assert window.document.sdf.buildings[sector.subsects[0]].damaged != 3


def test_save_includes_description_without_focus_change(real_window):
    window = real_window
    window.sectors.setCurrentRow(142)
    window.description.setText("Typed immediately before saving")
    with patch.object(window.document, "save"):
        assert window.save()
    assert window.document.sdf.sectors[142].comment == "Typed immediately before saving"


def test_failed_model_edit_keeps_renderer_bound_to_restored_tables(real_window):
    from PySide6.QtWidgets import QMessageBox
    window = real_window
    window.base.setCurrentText("")
    with patch.object(QMessageBox, "warning"):
        assert not window._commit_model_form()
    assert window.assets.sdf is window.document.sdf
    assert window.library.resolver.sdf is window.document.sdf


def test_pending_states_cancel_discard_and_save_keep_original_target(real_window):
    from PySide6.QtWidgets import QMessageBox
    window = real_window
    window.sectors.setCurrentRow(142)
    original = deepcopy(window.document.sdf)
    window.state_models[2].setCurrentIndex(window.state_models[2].findData(3))
    with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Cancel):
        window.select_cell(0)
        window.sectors.setCurrentRow(141)
    assert window.current_sector == 142 and window.sectors.currentRow() == 142
    assert window.selected_cells == {4} and window._pending_states()
    assert window.document.sdf == original
    with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Save):
        window.select_cell(0)
    cells = window.document.sdf.sectors[142].subsects
    assert window.document.sdf.buildings[cells[4]].heavy == 3
    assert window.document.sdf.buildings[cells[0]].heavy != 3
    window.state_models[2].setCurrentIndex(window.state_models[2].findData(4))
    before = deepcopy(window.document.sdf)
    with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Discard):
        window.select_cell(8)
    assert window.selected_cells == {8} and not window._pending_states()
    assert window.document.sdf == before
    window.state_models[2].setCurrentIndex(window.state_models[2].findData(4))
    with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Save):
        window.sectors.setCurrentRow(141)
    assert window.current_sector == 141 and window.sectors.currentRow() == 141
    cells = window.document.sdf.sectors[142].subsects
    assert window.document.sdf.buildings[cells[8]].heavy == 4


def test_pending_state_survives_model_table_rebuild_and_rotation(real_window):
    from PySide6.QtWidgets import QMessageBox
    window = real_window
    window.sectors.setCurrentRow(142)
    window.select_cell(0)
    window.state_models[1].setCurrentIndex(window.state_models[1].findData(3))
    window.model_combo.setCurrentIndex(3)
    window.model_comment.setText("Updated card detail")
    window.update_model()
    assert window._pending_states() and window.state_models[1].currentData() == 3
    assert window.models.item(3).data(int(Qt.ItemDataRole.UserRole) + 1) == "Updated card detail"
    with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Save):
        window.transform_cells("rotate")
    cells = window.document.sdf.sectors[142].subsects
    assert window.document.sdf.buildings[cells[2]].damaged == 3
    assert window.document.sdf.buildings[cells[0]].damaged != 3
    window.undo()
    assert window.document.sdf.buildings[window.document.sdf.sectors[142].subsects[0]].damaged == 3


def test_export_zip_flushes_all_pending_fields_and_tracks_further_edits(real_window, tmp_path):
    from PySide6.QtWidgets import QFileDialog
    window = real_window
    window.sectors.setCurrentRow(142)
    window.description.setText("Pending ZIP description")
    window.state_models[1].setCurrentIndex(window.state_models[1].findData(3))
    window.model_combo.setCurrentIndex(3)
    window.model_comment.setText("First pending model edit")
    target = tmp_path / "states.zip"
    with patch.object(QFileDialog, "getSaveFileName", return_value=(str(target), "")):
        window.export_zip()
    with ZipFile(target) as archive:
        sdf = parse_set_sdf_text(archive.read("Data/Sets/Set1/Scripts/SET.SDF").decode("cp1252"))
    assert sdf.sectors[142].comment == "Pending ZIP description"
    assert sdf.models[3].comment == "First pending model edit"
    assert sdf.buildings[sdf.sectors[142].subsects[4]].damaged == 3
    assert window.cell_grid.lib.assets.sdf.sectors[142].subsects[4] == sdf.sectors[142].subsects[4]
    assert window.models.item(3).data(int(Qt.ItemDataRole.UserRole) + 1) == "First pending model edit"
    window.model_comment.setText("Second pending model edit")
    assert window._pending_model()
    with patch.object(window.document, "save"):
        assert window.save()
    assert window.document.sdf.models[3].comment == "Second pending model edit"


def test_description_switch_and_undo_do_not_reintroduce_old_text(real_window):
    window = real_window
    window.sectors.setCurrentRow(142)
    original = window.document.sdf.sectors[142].comment
    window.description.setText("Description before changing cell")
    window.select_cell(0)
    assert window.document.sdf.sectors[142].comment == "Description before changing cell"
    window.undo()
    assert window.document.sdf.sectors[142].comment == original
    assert window.description.text() == original and not window._pending_sector_fields()
    window.description.setText("Description before changing sector")
    window.sectors.setCurrentRow(141)
    assert window.document.sdf.sectors[142].comment == "Description before changing sector"
    assert window.description.text() == window.document.sdf.sectors[141].comment


def test_usage_filter_includes_models_in_damage_states(real_window):
    window = real_window
    window.document.assign_states(142, {4}, (0, 3, 4, 5), 1)
    window._refresh()
    window.model_combo.setCurrentIndex(3)
    window.find_model_usage()
    expected = window.document.sectors_using_model(3)
    visible = {n for n in range(window.sectors.count()) if not window.sectors.item(n).isHidden()}
    assert 142 in visible and visible == expected
    window.model_combo.setCurrentIndex(4)
    visible = {n for n in range(window.sectors.count()) if not window.sectors.item(n).isHidden()}
    assert 142 in visible and visible == window.document.sectors_using_model(4)


def test_model_preview_refresh_preserves_camera(real_window):
    window = real_window
    window.model_combo.setCurrentIndex(3)
    window.preview_mode.setCurrentIndex(1)
    window.view.camera.yaw = 22
    window.view.camera.zoom *= 1.5
    camera = window.view.camera.copy()
    window._refresh()
    assert window.view.camera.yaw == camera.yaw and window.view.camera.zoom == camera.zoom


def test_copied_states_remain_valid_after_the_source_edit_is_undone(real_window):
    window = real_window
    window.sectors.setCurrentRow(142)
    window.document.assign_states(142, {4}, (0, 3, 4, 5), 1)
    window._refresh()
    window.copy_cells()
    window.undo()
    window.select_cell(8)
    window.paste_cells()
    window.confirm_placement()
    assert not table_issues(window.document.sdf)
    sector = window.document.sdf.sectors[142]
    row = window.document.sdf.buildings[sector.subsects[8]]
    assert (row.intact, row.damaged, row.heavy, row.flat) == (0, 3, 4, 5)


def test_cell_tools_inspect_damage_model_and_keep_pending_edits(real_window):
    from PySide6.QtWidgets import QMessageBox
    window = real_window
    window.sectors.setCurrentRow(142)
    window.document.assign_states(142, {4}, (0, 3, 4, 5), 1)
    window._refresh()
    window.state.setCurrentIndex(2)
    window.model_comment.setText("Pending shared definition")
    with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Cancel):
        window.inspect_cell_model()
    assert window.model_combo.currentData() == 0 and window._pending_model()
    with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Discard):
        window.inspect_cell_model()
    assert window.model_combo.currentData() == 4
    assert window.tabs.currentWidget() is window.definitions_page and window.view.lib.model == 4
    window.choose_cell_model()
    assert window.tabs.currentWidget() is window.models_page
    assert window.preview_mode.currentIndex() == 0
    assert window.selected_cells == {4}


def test_select_matching_buildings_and_free_slot_controls(real_window):
    window = real_window
    window.sectors.setCurrentRow(142)
    window.select_cell(0)
    window.select_matching_cells()
    assert window.selected_cells == {0, 8}
    assert "2 cells" in window.model_target.text()
    window.copy_cells()
    assert window.actions["paste"].isEnabled()
    window.select_cell(4)
    assert window.actions["paste"].isEnabled()
    window._layout_changed(1)
    window.select_matching_cells()
    assert window.selected_cells == {4}
    free = window.document.free_sector()
    window.sectors.setCurrentRow(free)
    for key in ("clear", "copy", "paste", "inspect", "choose_model", "select_matching"):
        assert not window.actions[key].isEnabled()
    assert not window.apply_model_button.isEnabled() and not window.apply_states_button.isEnabled()
    assert not window.select_all_button.isEnabled() and not window.clear_cells_button.isEnabled()


def test_panels_separate_cell_states_from_model_browser(real_window):
    window = real_window
    assert [window.tabs.tabText(i) for i in range(window.tabs.count())] == ["Cells", "Models", "Definitions"]
    assert window.cells_page.isAncestorOf(window.state_models[0])
    assert window.models_page.isAncestorOf(window.models)
    assert window.definitions_page.isAncestorOf(window.model_combo)
    assert not window.definitions_page.isAncestorOf(window.state_models[0])
    for page in (window.cells_page, window.definitions_page):
        window.tabs.setCurrentWidget(page)
        QApplication.processEvents()
        assert page.horizontalScrollBar().maximum() == 0


def test_delete_shortcut_keeps_text_editing_and_cell_editing_separate(real_window):
    from PySide6.QtTest import QTest
    window = real_window
    window.sectors.setCurrentRow(142)
    window.activateWindow()
    window.description.setFocus()
    window.description.setText("Text to delete")
    window.description.selectAll()
    before = window.document.sdf.sectors[142].subsects
    QTest.keyClick(window.description, Qt.Key.Key_Delete)
    assert window.description.text() == "" and window.document.sdf.sectors[142].subsects == before
    window.select_cell(0)
    window.view.setFocus()
    QTest.keyClick(window.view, Qt.Key.Key_Delete)
    assert window.document.sdf.sectors[142].subsects[0] == 0
    assert window.document.sdf.sectors[142].subsects[8] == before[8]
    window.undo()
    assert window.document.sdf.sectors[142].subsects == before


def test_close_guard_and_invalid_state_preserve_unapplied_fields(real_window):
    from PySide6.QtWidgets import QMessageBox
    window = real_window
    window.sectors.setCurrentRow(142)
    before = deepcopy(window.document.sdf)
    window.state_models[1].setCurrentIndex(3)
    with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Cancel):
        assert not window._guard_unsaved()
    assert window.document.sdf == before and window._pending_states()
    window.state_models[1].setCurrentIndex(-1)
    with patch.object(QMessageBox, "warning"), patch.object(window.document, "save") as save:
        assert not window.save()
        save.assert_not_called()
    assert window.document.sdf == before and window._pending_states()
    assert window.state_models[1].currentIndex() == -1
    window.state_models[1].setCurrentIndex(3)
    with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Save), patch.object(window.document, "save"):
        assert window._guard_unsaved()
    assert not window._pending_states()


def test_model_form_cancel_validation_and_save_keep_the_right_model(real_window):
    from PySide6.QtWidgets import QMessageBox
    window = real_window
    window.model_combo.setCurrentIndex(3)
    original = deepcopy(window.document.sdf.models[3])
    window.model_comment.setText("Pending model description")
    window._refresh()
    assert window._pending_model() and window.model_comment.text() == "Pending model description"
    with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Cancel):
        window.model_combo.setCurrentIndex(4)
    assert window.model_combo.currentData() == 3 and window.models.currentRow() == 3
    window.effects.setPlainText("81 23")
    with patch.object(QMessageBox, "warning"), patch.object(window.document, "save") as save:
        assert not window.save()
        save.assert_not_called()
    assert window.document.sdf.models[3] == original and window._pending_model()
    window.effects.setPlainText("81 23 0 0")
    window.base.setCurrentText("")
    with patch.object(QMessageBox, "warning"):
        assert not window._commit_model_form()
    assert window.document.sdf.models[3] == original
    assert window.assets.sdf is window.document.sdf
    window.base.setCurrentText(original.base)
    with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Save):
        window.model_combo.setCurrentIndex(4)
    assert window.document.sdf.models[3].comment == "Pending model description"
    assert window.document.sdf.models[3].effects == ((81, 23, 0, 0),)
    assert window._editing_model_id == 4 and not window._pending_model()
    window.model_comment.setText("Saved without clicking Apply")
    with patch.object(window.document, "save") as save:
        assert window.save()
        save.assert_called_once()
    assert window.document.sdf.models[4].comment == "Saved without clicking Apply"


def test_asset_reload_preserves_composition_selection_and_history(real_window):
    window = real_window
    window.sectors.setCurrentRow(142)
    window.document.assign_model(142, {0, 8}, 3)
    window._refresh()
    before = deepcopy(window.document.sdf)
    camera = window.view.camera.copy()
    assets, library = window.assets, window.library
    history = len(window.document._undo)
    window.reload_assets()
    assert window.assets is not assets and window.library is not library
    assert window.document.assets is window.assets
    assert window.document.sdf == before and window.document.dirty
    assert len(window.document._undo) == history
    assert window.current_sector == 142
    assert window.view.camera.center == camera.center and window.view.camera.zoom == camera.zoom
    assert window.view.lib.mesh(142).faces


def test_change_set_menu_restores_selection_when_open_is_cancelled(real_window):
    from map_editor.core.game_installation import GameInstallation
    from studio.ui.shell import install_workspace_shell, change_game_set
    window = real_window
    install = GameInstallation.suggest(bootstrap.game_data_dir())
    with patch("map_editor.bootstrap.installation", return_value=install):
        install_workspace_shell(window, "sector_editor")
        assert window.studio_set_menu.title() == "Change Set"
        with patch.object(window, "open_set", return_value=False) as opened:
            change_game_set(window, 2)
        opened.assert_called_once_with(bootstrap.set_dir(2))
    assert window.sets.currentData() == window.document.number


def test_visible_previews_are_lazy_cached_and_refresh_after_edits(real_window):
    from PySide6.QtTest import QTest
    from sector_editor.window import PREVIEW_ROLE
    from sector_editor.previews import visible_items
    window = real_window
    window._request_previews()
    visible = visible_items(window.sectors) + visible_items(window.models)
    assert 0 < len(window.previews.pending) <= len(visible) < 20
    for _ in range(40):
        QTest.qWait(25)
        if window.previews.cache and not window.previews.pending and not window.previews.busy:
            break
    assert window.previews.cache
    assert any(not item.icon().isNull() for item in visible)
    count = window.previews.rendered
    window._request_previews()
    assert not set(window.previews.pending).intersection(window.previews.cache)
    assert window.previews.rendered == count
    item = window.sectors.item(1)
    window.sectors.setCurrentRow(1)
    window._request_previews()
    before = item.data(PREVIEW_ROLE)
    window.document.assign_model(1, {4}, 3)
    window._refresh()
    window._request_previews()
    assert window.sectors.item(1).data(PREVIEW_ROLE) != before


def test_current_view_export_and_reset_use_real_sector_geometry(real_window):
    window = real_window
    window.sectors.setCurrentRow(142)
    home = window.view.camera.copy()
    window.view.set_camera_angles(0, 90)
    window.view.frame_selection()
    window.view.reset_camera()
    assert window.view.camera.center == home.center
    assert window.view.camera.yaw == home.yaw and window.view.camera.pitch == home.pitch
    image = window.view.capture_preview(160)
    assert not image.isNull() and image.width() == 160
    assert image.hasAlphaChannel()
    assert any(image.pixelColor(x, y).alpha() for y in range(image.height()) for x in range(image.width()))


def test_subcell_picker_uses_nearest_visible_geometry():
    from types import SimpleNamespace
    from PySide6.QtCore import QPointF
    from studio.rendering.asset_viewport import ViewFace
    from sector_editor.viewport import SectorViewport
    from map_editor.core.ldf_model import LdfDocument
    from map_editor.render.sector_mesh import SectorMesh
    app = QApplication.instance() or QApplication([])
    view = SectorViewport()
    try:
        face = ViewFace(((-100, 100, 0), (100, 100, 0), (100, -100, 0), (-100, -100, 0)), (), None)
        mesh = SectorMesh(1, [(face, 0, 300), (face, 0, -300)])
        view.lib = SimpleNamespace(mesh=lambda number: mesh)
        view.doc = LdfDocument(mw=1, mh=1)
        view.camera.center = (600, 0, -600)
        view.camera.yaw = view.camera.pitch = 0
        assert view.pick_subcell(QPointF(view.camera.width / 2, view.camera.height / 2)) == 1
        view.model_preview = True
        assert view.pick_subcell(QPointF(400, 300)) is None
    finally:
        view.stop_rendering()
        view.close()
