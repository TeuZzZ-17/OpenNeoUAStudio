"""Whole-sector playback and readable effect editing, without changing file coordinates."""
from copy import deepcopy
from types import SimpleNamespace

from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QSignalSpy

from test_sector_editor import real_window  # noqa: F401
from sector_editor.effects_editor import DestructionEffectsEditor
from sector_editor.playback_scene import sector_transition_steps


def test_effect_rows_preserve_file_axes_and_unknown_ids():
    app = QApplication.instance() or QApplication([])
    editor = DestructionEffectsEditor()
    editor._catalog = {51: "Large explosion"}
    editor.setPlainText("51 35 -87 900\n87 30 30 100")
    assert editor.table.cellWidget(0, 0).currentText() == "Large explosion - ID 51"
    assert editor.table.cellWidget(0, 2).value() == 87
    assert editor.values() == [(51, 35, -87, 900), (87, 30, 30, 100)]
    editor.table.cellWidget(0, 3).setValue(700)
    assert editor.toPlainText().splitlines()[0] == "51 35 -87 700"
    editor.deleteLater()


def test_effect_selection_previews_and_removes_exact_row():
    app = QApplication.instance() or QApplication([])
    editor = DestructionEffectsEditor()
    editor.setPlainText("51 0 0 100\n87 20 -30 200")
    spy = QSignalSpy(editor.previewRequested)
    editor.table.selectRow(1)
    editor.preview_selected()
    assert spy.count() == 1 and tuple(spy.at(0)[0]) == (87, 20, -30, 200)
    editor.remove_selected()
    assert editor.values() == [(51, 0, 0, 100)]
    editor.deleteLater()


def test_whole_sector_transition_offsets_all_cells():
    building = SimpleNamespace(state=lambda state: state)
    models = [SimpleNamespace(effects=((51, 10, 20, 100),)), SimpleNamespace(effects=())]
    sector = SimpleNamespace(single=False, subsects=(0,) * 9)
    document = SimpleNamespace(sdf=SimpleNamespace(sectors={1: sector}, buildings=[building], models=models))
    effects = sector_transition_steps(document, 1, 0, 1)[0]
    assert len(effects) == 9
    assert effects[0] == (51, -290, -280, 100)
    assert effects[4] == (51, 10, 20, 100)
    assert effects[8] == (51, 310, 320, 100)


def test_play_keeps_same_view_camera_and_starts_automatically(real_window):
    window = real_window
    assert window.play_button.isChecked()
    window.sectors.setCurrentRow(38)
    window.select_cell(0)
    view = window.view
    camera = deepcopy(vars(view.camera))
    before = deepcopy(window.document.sdf)
    window.play_button.setChecked(False)
    assert window.view is view and view.isVisible()
    assert vars(view.camera) == camera
    clock = view.animation_time_ms
    window.play_button.setChecked(True)
    assert window.view is view and view.isVisible()
    assert vars(view.camera) == camera
    window._advance_animation(200)
    assert view.animation_time_ms == clock + 200
    assert vars(view.camera) == camera
    window.state.setCurrentIndex(1)
    assert view.isVisible() and window._transition is not None
    assert window.play_view is not None and not window.play_view.isVisible()
    window._advance_animation(200)
    assert window.document.sdf == before


def test_playback_does_not_mutate_cached_source_geometry(real_window):
    window = real_window
    window.sectors.setCurrentRow(38)
    original = window.view.lib.library.mesh(window.current_sector)
    points = deepcopy([face.vertices for face, _, _ in original.faces])
    window._advance_animation(500)
    window.view.lib.mesh(window.current_sector)
    assert [face.vertices for face, _, _ in original.faces] == points


def test_shared_transport_style_and_labels():
    from studio.ui.playback_controls import PlaybackButton
    app = QApplication.instance() or QApplication([])
    first, second = PlaybackButton(), PlaybackButton()
    assert first.property("studioRole") == second.property("studioRole") == "play"
    assert first.minimumSize() == second.minimumSize()
    assert first.maximumWidth() == second.maximumWidth()
    first.setChecked(True)
    assert first.text() == "Pause" and second.text() == "Play"
    assert first.icon().isNull()
    first.deleteLater()
    second.deleteLater()


def test_transition_geometry_uses_main_sector_renderer(real_window):
    from sector_editor.transition_fx import transition_simulation
    from entity_designer.destruction_fx import legacy_vehicle_from_text
    window = real_window
    vehicle = legacy_vehicle_from_text(999, "new_vehicle 999\nmodel = plane\nvp_dead = 2\nvp_wait = 2\nvp_megadeth = 2\n", "test")
    simulation = transition_simulation([[(999, 0, 0, 100)]], {999: vehicle})
    window._begin_effect_simulation(simulation, None, None)
    window._advance_animation(100)
    assert window.view.lib.effects
    for face in window.view.lib.effects:
        assert window.view.lib.surface_for(face) is not None
    assert window.view.isVisible() and not window.play_view.isVisible()


def test_sector_texture_timeline_changes_real_frames(real_window):
    window = real_window
    slot = next(slot for slot in window.document.sdf.sectors
                if any(face.animated for face, _, _ in window.library.mesh(slot).faces))
    window.current_sector = slot
    window._preview()
    face = next(face for face, _, _ in window.view.lib.mesh(slot).faces if face.animated)
    states = []
    for time_ms in (0, 333, 666, 1000, 1500):
        window.view.sample_animation(time_ms)
        window.view.lib.mesh(slot)
        states.append((window.library._loader._anim_states.get(face.material),
                       tuple(window.view.lib.face_uvs(face))))
    assert len(set(states)) > 1
