import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from PySide6.QtCore import Qt, QSignalBlocker

from map_editor import bootstrap
from map_editor.core.game_installation import GameInstallation
from game_object_catalog import load_game_objects
from game_browser import GameObjectBrowser, game_files
from collision_editor.editor import (
    CollisionProject, CollisionSphere, CollisionEditorWindow, VEHICLE, WEAPON, LEGACY,
    find_script_blocks, import_briefing_size_block, export_collision_text,
    plan_script_update, build_editable_overwrite_preview, apply_editable_overwrite_preview,
)
from collision_editor.briefing import find_background, briefing_layout


def test_catalog_obeys_manifest_order_and_nested_scopes(tmp_path):
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    (scripts / "Startup.cfg").write_text("include first.cfg\ninclude second.cfg\n")
    (scripts / "first.cfg").write_text("new_vehicle 1\nname = Old\nvp_normal = 2\nbegin_fx\nname = Wrong\nvp_normal = 90\nend\nend\nnew_weapon 1\nname = Weapon\nvp_normal = 4\nend\n")
    (scripts / "second.cfg").write_text("modify_vehicle 1\nname = Current\nvp_dead = 8\nend\nnew_building 3\nname = Power Plant\nsec_type = 201\nend\n")
    objects = load_game_objects(GameInstallation(tmp_path, {"scripts": scripts}))
    assert len(objects) == 3
    vehicle = next(obj for obj in objects if obj.kind == "vehicle")
    assert vehicle.name == "Current" and vehicle.values["vp_normal"] == "2"
    assert vehicle.values["vp_dead"] == "8"
    assert vehicle.effective_text.index("name = Current") > vehicle.effective_text.index("name = Old")


def test_briefing_size_roundtrips_every_script_writer():
    source = "new_vehicle 1\nname = Weasel\nradius = 40\nbriefing_size = 35\nbegin_fx\nradius = 9\nend\nend\n"
    block = find_script_blocks(source)[0]
    assert import_briefing_size_block(source, block) == (True, 35)
    project = CollisionProject(name="Weasel", target_category=VEHICLE,
        legacy=CollisionSphere(LEGACY, radius=40), briefing_size_enabled=True, briefing_size=70)
    assert "briefing_size = 70" in export_collision_text(project)
    for canonical in (False, True):
        result, diff, name = plan_script_update(source, "new_vehicle", 1, project, replace_all_managed=canonical)
        assert result.count("briefing_size = 70") == 1
        assert "radius = 40" in result and "radius = 9" in result
    preview = build_editable_overwrite_preview("new_vehicle", 1, project, {"briefing"})
    result, groups = apply_editable_overwrite_preview(source, preview, "new_vehicle", 1)
    assert groups == {"briefing"} and "briefing_size = 70" in result
    assert "radius = 40" in result and "radius = 9" in result
    state = project.snapshot()
    project.briefing_size = 90
    project.restore(state)
    assert project.briefing_size == 70
    project.target_category = WEAPON
    assert "briefing_size" not in export_collision_text(project)


def test_installed_backgrounds_and_native_rectangle_are_dynamic(tmp_path):
    bg = tmp_path / "Levels" / "Bg"
    bg.mkdir(parents=True)
    (bg / "Brief.png").touch()
    (bg / "Debrief.png").touch()
    source = tmp_path / "src"
    source.mkdir()
    (source / "yw_funcs.cpp").write_text("_briefScreen.ViewingObjectRect = Common::FRect(-.8, .2, -.1, .9);")
    install = GameInstallation.suggest(tmp_path)
    assert find_background(install, "brief") == bg / "Brief.png"
    assert find_background(install, "debrief") == bg / "Debrief.png"
    assert briefing_layout(install) == (-.8, .2, -.1, .9)
    manual = tmp_path / "custom.png"
    manual.touch()
    install.preview_backgrounds["brief"] = manual
    assert find_background(install, "brief") == manual


def test_wireframe_inventory_includes_overrides_and_excludes_builds(tmp_path):
    (tmp_path / "a.skl").touch()
    (tmp_path / "Build").mkdir()
    (tmp_path / "Build" / "generated.skl").touch()
    custom = tmp_path / "custom"
    custom.mkdir()
    (custom / "b.SKLT").touch()
    paths = game_files(GameInstallation(tmp_path, {"models": custom}), {".skl", ".sklt"})
    assert {path.name for path in paths} == {"a.skl", "b.SKLT"}


def test_resource_path_typing_does_not_trigger_tool_shortcuts():
    app = QApplication.instance() or QApplication([])
    from startup_selector import StartupToolSelector
    window = StartupToolSelector()
    window.show()
    window.resources_panel.root.setFocus()
    window.resources_panel.root.clear()
    QTest.keyClicks(window.resources_panel.root, "Windows/Scripts")
    assert window.resources_panel.root.text() == "Windows/Scripts"
    assert window.selected_tool() == "model_editor"
    window.close()


def test_cancelled_entity_switch_restores_model_browser_and_state(tmp_path):
    app = QApplication.instance() or QApplication([])
    from model_editor import ModelEditorWindow
    from game_object_catalog import GameObject
    from game_visuals import VisualChoice
    from game_browser import GAME_OBJECT_ROLE
    window = ModelEditorWindow()
    old = GameObject("vehicle", 1, tmp_path / "old.cfg", {"name": "Old"})
    new = GameObject("vehicle", 2, tmp_path / "new.cfg", {"name": "New"})
    window.entity_browser.objects = [old, new]
    window.entity_browser.fill(old)
    window._current_entity = old
    window._loaded_entity_state = 1
    with QSignalBlocker(window.entity_state):
        window.entity_state.addItem("Normal", VisualChoice("Normal", tmp_path / "old.base"))
        window.entity_state.addItem("Dead", VisualChoice("Dead", tmp_path / "dead.base"))
        window.entity_state.setCurrentIndex(1)
    with patch("game_visuals.visual_choices", return_value=[VisualChoice("Normal", tmp_path / "new.base")]), \
            patch.object(window, "_confirm_discard_geometry", return_value=False):
        window._entity_selected(new)
    assert window._current_entity is old
    assert window.entity_state.currentText() == "Dead"
    assert window.entity_browser.tree.currentItem().data(0, GAME_OBJECT_ROLE) is old
    window.close()


def test_game_file_browser_folder_mode_and_header(tmp_path):
    app = QApplication.instance() or QApplication([])
    from game_browser import GameFileBrowser
    folder = tmp_path / "pick"
    (folder / "sub").mkdir(parents=True)
    (folder / "a.skl").touch()
    (folder / "sub" / "b.SKLT").touch()
    (folder / "notes.txt").touch()
    plain = GameFileBrowser({".skl", ".sklt"})
    assert plain.tree.headerItem().text(0) == "Browse"
    assert plain.folder_button is None
    browser = GameFileBrowser({".skl", ".sklt"}, allow_folder=True)
    assert not browser.game_button.isEnabled()
    with patch("game_browser.QFileDialog.getExistingDirectory", return_value=str(folder)):
        browser.folder_button.click()
    labels = {browser.tree.topLevelItem(i).text(0) for i in range(browser.tree.topLevelItemCount())}
    assert labels == {"a.skl", str(Path("sub") / "b.SKLT")}
    assert browser.game_button.isEnabled()
    browser.reload()  # a reload keeps the chosen folder
    assert browser.tree.topLevelItemCount() == 2
    browser.game_button.click()
    assert browser.folder is None and not browser.game_button.isEnabled()
    assert not any(label == "a.skl" for label in
                   (browser.tree.topLevelItem(i).text(0) for i in range(browser.tree.topLevelItemCount())))


def test_object_browser_set_selector_can_live_outside():
    app = QApplication.instance() or QApplication([])
    from PySide6.QtWidgets import QLayout
    inline = GameObjectBrowser()
    outside = GameObjectBrowser(inline_set_selector=False)

    def in_layout(browser):
        def walk(layout):
            for i in range(layout.count()):
                item = layout.itemAt(i)
                if item.widget() is browser.set_combo or (item.layout() and walk(item.layout())):
                    return True
            return False
        return walk(browser.layout())
    assert in_layout(inline) and not in_layout(outside)
    assert hasattr(outside, "setChanged") and hasattr(outside, "set_number")


def test_model_editor_set_selector_is_shared_and_bas_follows_set(tmp_path):
    app = QApplication.instance() or QApplication([])
    from types import SimpleNamespace
    from model_editor import ModelEditorWindow
    sets = tmp_path / "Sets"
    for number in (1, 2):
        (sets / f"Set{number}" / "Objects").mkdir(parents=True)
        (sets / f"Set{number}" / "Objects" / "SET.BAS").touch()
        (sets / f"Set{number}" / "Scripts").mkdir()
        (sets / f"Set{number}" / "Scripts" / "set.sdf").touch()
    install = GameInstallation(tmp_path, {"sets": sets})
    loaded = []

    def fake_open(self, path):
        loaded.append(Path(path))
        self._setbas = SimpleNamespace(path=Path(path))

    old = bootstrap.installation()
    bootstrap.set_installation(install)
    try:
        with patch("model_editor.window.ModelEditorWindow.open_setbas", fake_open), \
                patch("game_object_catalog.load_game_objects", return_value=[]), \
                patch("game_browser.load_game_objects", return_value=[]):
            window = ModelEditorWindow()
            tabs = window._resources_tabs
            assert [tabs.tabText(i) for i in range(tabs.count())][:3] == [
                "Bas Manager", "Entity List", "Asset Dependencies"]
            combo = window.entity_browser.set_combo
            assert window._resources_tabs.isAncestorOf(combo)
            assert not window.entity_browser.isAncestorOf(combo)
            assert loaded == [sets / "Set1" / "Objects" / "SET.BAS"]
            combo.setCurrentIndex(combo.findData(2))
            assert loaded[-1] == sets / "Set2" / "Objects" / "SET.BAS"
            assert window._bas_set == 2
            window.close()
    finally:
        bootstrap.set_installation(old)


def test_snapshot_studio_loads_set_1_and_follows_the_set_selector(tmp_path):
    app = QApplication.instance() or QApplication([])
    from types import SimpleNamespace
    from snapshot_studio.window import SnapshotStudioWindow
    sets = tmp_path / "Sets"
    for number in (1, 2):
        (sets / f"Set{number}" / "Objects").mkdir(parents=True)
        (sets / f"Set{number}" / "Objects" / "SET.BAS").touch()
        (sets / f"Set{number}" / "Scripts").mkdir()
        (sets / f"Set{number}" / "Scripts" / "set.sdf").touch()
    install = GameInstallation(tmp_path, {"sets": sets})
    loaded = []

    def fake_open(self, path):
        loaded.append(Path(path))
        self._setbas = SimpleNamespace(path=Path(path))

    old = bootstrap.installation()
    bootstrap.set_installation(install)
    try:
        with patch("snapshot_studio.window.SnapshotStudioWindow.open_setbas", fake_open):
            window = SnapshotStudioWindow()
            assert loaded == [sets / "Set1" / "Objects" / "SET.BAS"]
            assert window._right_tabs.currentWidget() is window._snapshot_scroll
            assert window._bas_panel.isAncestorOf(window._set_combo)
            window._set_combo.setCurrentIndex(window._set_combo.findData(2))
            assert loaded[-1] == sets / "Set2" / "Objects" / "SET.BAS"
            window.close()
    finally:
        bootstrap.set_installation(old)


def test_briefing_size_is_an_integer_and_larger_means_larger():
    from collision_editor.briefing import (
        radius_to_briefing_size, briefing_size_to_slider, slider_to_briefing_size)
    assert radius_to_briefing_size(80) == 50 and radius_to_briefing_size(35) == 114
    assert radius_to_briefing_size(0.0001) == 10000 and radius_to_briefing_size(1e9) == 1
    for size in (1, 8, 39, 120, 10000):
        assert slider_to_briefing_size(briefing_size_to_slider(size)) == size


def test_entity_designer_list_has_no_buildings(tmp_path):
    from game_object_catalog import GameObject
    app = QApplication.instance() or QApplication([])
    objects = [GameObject(kind, number, tmp_path / "s.cfg", {"name": kind})
               for number, kind in enumerate(("vehicle", "weapon", "building"), 1)]
    with patch("game_browser.load_game_objects", return_value=objects), \
            patch.object(bootstrap, "installation",
                         return_value=SimpleNamespace(available_sets=lambda: [1])):
        browser = GameObjectBrowser(kinds=("vehicle", "weapon"))
    assert [browser.category.itemText(i) for i in range(browser.category.count())] == ["All", "Vehicles", "Weapons"]
    assert [o.kind for o in browser.objects] == ["vehicle", "weapon"]
