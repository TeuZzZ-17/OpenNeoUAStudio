"""Applying the changes of one model state to the others, on the real Flak states."""

from __future__ import annotations

import os
from pathlib import Path
import shutil

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from model_editor.state_discovery import (
    family_key,
    merge_states,
    nearby_models,
    StateCandidate,
    states_by_name,
    state_word,
)
from studio.assets.family import load_asset_family
from studio.exchange.portable import DirectoryIndex
from studio_mcp.loose import LooseModel
from studio_mcp.state_sync import (
    apply_plan,
    build_plan,
    static_faces,
    SyncOptions,
    texture_files,
)


@pytest.fixture
def flak(tmp_path):
    data = Path(os.environ.get("NME_GAME_DATA", ""))
    original = data / "Models/Base/New/Kyt_Flak_01"
    if not original.is_dir():
        pytest.skip("Flak reference assets are not installed")
    shutil.copytree(original, tmp_path / "Kyt_Flak_01")
    return tmp_path / "Kyt_Flak_01"


def load(folder, state):
    DirectoryIndex.clear_cache()
    family = load_asset_family(folder / f"Kyt_Flak_{state}" / f"Kyt_Flak_{state}.BASE")
    return family, family.root_object


def uv_at_positions(obj, poly_id):
    block = next(b for b in obj.base_object.ades for e in b.atts if e.poly_id == poly_id)
    index = [e.poly_id for e in block.atts].index(poly_id)
    corners = obj.skeleton.polygons[poly_id]
    return {tuple(round(v, 2) for v in obj.skeleton.points[c]): tuple(uv)
            for c, uv in zip(corners, block.olpl[index])}


def test_state_names_are_recognised():
    assert state_word("Kyt_Flak_Fire") == "Fire"
    assert state_word("ALPHA_VP_NOCTU_GENESIS") == "Genesis"
    assert state_word("Firewall_Normal") == "Normal"
    assert state_word("Kyt_Flak") == ""
    assert family_key("Kyt_Flak_Fire") == family_key("kyt_flak_NORMAL")
    assert family_key("Kyt_Bunker_Fire") != family_key("Kyt_Flak_Fire")


def test_the_folder_finds_the_other_state_without_any_script(flak):
    found = states_by_name(flak / "Kyt_Flak_Fire" / "Kyt_Flak_Fire.BASE")
    assert [candidate.label for candidate in found] == ["Normal"]
    assert found[0].writable


def test_a_state_found_twice_is_one_row_with_both_sources(tmp_path):
    path = tmp_path / "Model_Fire.BASE"
    merged = merge_states([StateCandidate("Fire", path, found_by=["Folder"])],
                          [StateCandidate("Fire — VP", path, found_by=["Entity List"])])
    assert len(merged) == 1
    assert merged[0].found_by == ["Folder", "Entity List"]


def test_a_model_without_state_names_still_finds_its_neighbours(flak):
    found = nearby_models(flak / "Kyt_Flak_Fire" / "Kyt_Flak_Fire.BASE", set())
    assert [path.name for path in found] == ["Kyt_Flak_Normal.BASE"]


def test_most_of_the_real_states_pair_up_and_nothing_changes_on_identical_files(flak):
    _, fire = load(flak, "Fire")
    _, normal = load(flak, "Normal")
    plan = build_plan(fire, normal)
    assert plan.match_share > 0.5
    assert not plan.moved_points
    _, again = load(flak, "Fire")
    same = build_plan(fire, again, again)
    assert same.match_share == 1.0
    assert not same.has_work(SyncOptions(state_faces=True))


def test_texture_and_uv_follow_the_corner_even_with_reordered_faces(flak):
    fire_family, fire = load(flak, "Fire")
    normal_family, normal = load(flak, "Normal")
    plan = build_plan(fire, normal, load(flak, "Fire")[1])
    link = plan.links[0]
    source_face = plan.source_faces[link.source]
    block = fire.base_object.ades[source_face.block_index]
    block.olpl[source_face.atts_index] = [
        (10 + 7 * k, 20 + 5 * k) for k in range(len(source_face.corners))]
    expected = uv_at_positions(fire, link.source)
    untouched = plan.links[1].target
    before = uv_at_positions(normal, untouched)

    plan = build_plan(fire, normal, load(flak, "Fire")[1])
    assert [change.source for change in plan.material_changes] == [link.source]
    lm = LooseModel.from_family(normal_family, normal)
    apply_plan(lm, plan, fire_family, fire)
    assert lm.commit("test")["written"]

    _, saved = load(flak, "Normal")
    assert uv_at_positions(saved, link.target) == expected
    assert uv_at_positions(saved, untouched) == before


def test_a_moved_vertex_is_followed_and_keeps_faces_welded(flak):
    fire_family, fire = load(flak, "Fire")
    normal_family, normal = load(flak, "Normal")
    saved_fire = load(flak, "Fire")[1]
    link = build_plan(fire, normal).links[0]
    corner = fire.skeleton.polygons[link.source][0]
    x, y, z = fire.skeleton.points[corner]
    fire.skeleton.points[corner] = (x + 13.0, y - 7.0, z)

    plan = build_plan(fire, normal, saved_fire)
    assert plan.moved_points
    assert not plan.added_faces, "a moved vertex must not look like new faces"
    lm = LooseModel.from_family(normal_family, normal)
    apply_plan(lm, plan, fire_family, fire)
    assert lm.commit("test")["written"]
    _, saved = load(flak, "Normal")
    positions = {tuple(round(v, 2) for v in point) for point in saved.skeleton.points}
    assert (round(x + 13.0, 2), round(y - 7.0, 2), round(z, 2)) in positions
    assert (round(x, 2), round(y, 2), round(z, 2)) not in positions


def test_added_faces_are_copied_but_state_only_faces_stay_home(flak):
    fire_family, fire = load(flak, "Fire")
    normal_family, normal = load(flak, "Normal")
    saved_fire = load(flak, "Fire")[1]
    lm_source = LooseModel.from_family(fire_family, fire)
    block, _ = lm_source.polygon_slot(next(iter(static_faces(fire))))
    a = lm_source.append_point((500.0, 0.0, 0.0))
    b = lm_source.append_point((510.0, 0.0, 0.0))
    c = lm_source.append_point((500.0, 0.0, 10.0))
    new_face = lm_source.append_polygon([a, b, c], block, (0, 0, 0, 0), [(1, 1), (9, 1), (1, 9)])

    plan = build_plan(fire, normal, saved_fire)
    assert plan.added_faces == [new_face]
    faces_before = len(normal.skeleton.polygons)
    lm = LooseModel.from_family(normal_family, normal)
    apply_plan(lm, plan, fire_family, fire)
    assert lm.commit("test")["written"]
    _, saved = load(flak, "Normal")
    assert len(saved.skeleton.polygons) == faces_before + 1


def test_deleted_faces_leave_the_other_state_too(flak):
    fire_family, fire = load(flak, "Fire")
    normal_family, normal = load(flak, "Normal")
    saved_fire = load(flak, "Fire")[1]
    victim = build_plan(fire, normal).links[0]
    lm_source = LooseModel.from_family(fire_family, fire)
    lm_source.delete_polygons([victim.source])

    plan = build_plan(fire, normal, saved_fire)
    assert plan.deleted_faces == [victim.target]
    faces_before = len(normal.skeleton.polygons)
    lm = LooseModel.from_family(normal_family, normal)
    apply_plan(lm, plan, fire_family, fire)
    assert lm.commit("test")["written"]
    _, saved = load(flak, "Normal")
    assert len(saved.skeleton.polygons) == faces_before - 1


def test_options_decide_what_travels(flak):
    fire_family, fire = load(flak, "Fire")
    normal_family, normal = load(flak, "Normal")
    saved_fire = load(flak, "Fire")[1]
    link = build_plan(fire, normal).links[0]
    corner = fire.skeleton.polygons[link.source][0]
    x, y, z = fire.skeleton.points[corner]
    fire.skeleton.points[corner] = (x + 3.0, y, z)
    plan = build_plan(fire, normal, saved_fire)
    options = SyncOptions(shape=False)
    assert plan.counts(options)["points"] == 0
    before = list(normal.skeleton.points)
    apply_plan(LooseModel.from_family(normal_family, normal), plan, fire_family, fire, options)
    assert normal.skeleton.points == before


def test_a_texture_the_destination_cannot_find_is_copied_beside_it(flak):
    fire_family, fire = load(flak, "Fire")
    normal_family, normal = load(flak, "Normal")
    plan = build_plan(fire, normal)
    if not plan.state_faces:
        pytest.skip("this Fire state has no extra static faces")
    lm = LooseModel.from_family(normal_family, normal)
    lm.family.search_roots = []
    lm.family.setbas_archive = None
    files = texture_files(lm, plan, fire_family, fire, SyncOptions(state_faces=True))
    assert all(data for data in files.values())


def test_the_dialog_previews_in_memory_and_writes_only_ticked_states(flak):
    from unittest.mock import patch
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QMainWindow
    from model_editor.state_sync_dialog import StateSyncDialog

    app = QApplication.instance() or QApplication([])
    family, fire = load(flak, "Fire")
    link = build_plan(fire, load(flak, "Normal")[1]).links[0]
    face = static_faces(fire)[link.source]
    block = fire.base_object.ades[face.block_index]
    block.olpl[face.atts_index] = [(9 + k, 9 + 2 * k) for k in range(len(face.corners))]
    normal_path = flak / "Kyt_Flak_Normal" / "Kyt_Flak_Normal.BASE"
    disk_before = normal_path.read_bytes()

    window = QMainWindow()
    window._family = family
    window._owner_to_obj = {fire.owner_path: fire}
    window._selected_owner = fire.owner_path
    window._workbench_obj = fire
    window._extra_roots = []
    window._setbas = None
    with patch("model_editor.state_sync_dialog.discover_states",
               lambda *a: states_by_name(flak / "Kyt_Flak_Fire" / "Kyt_Flak_Fire.BASE")):
        dialog = StateSyncDialog(window)
    assert dialog.states.topLevelItemCount() == 1
    item = dialog.states.topLevelItem(0)
    assert item.text(2) == "1 textures"
    assert not dialog.apply_button.isEnabled()
    assert normal_path.read_bytes() == disk_before, "a preview must not write"

    item.setCheckState(0, Qt.CheckState.Checked)
    assert dialog.apply_button.isEnabled()
    dialog._apply()
    assert normal_path.read_bytes() != disk_before
    dialog.preview.clear()
    dialog.close()


def test_the_mcp_copies_from_another_state(flak, monkeypatch):
    monkeypatch.setenv("ONUA_MCP_WRITE_ROOTS", str(flak))
    from studio_mcp import edit

    fire = flak / "Kyt_Flak_Fire" / "Kyt_Flak_Fire.BASE"
    normal = flak / "Kyt_Flak_Normal" / "Kyt_Flak_Normal.BASE"
    before = normal.read_bytes()
    result = edit.run(str(normal), [{"op": "copy_from_state", "source": str(fire)}], "test")
    assert result["results"][0]["match"] > 0.5
    assert result["results"][0]["textures"]
    assert normal.read_bytes() != before
