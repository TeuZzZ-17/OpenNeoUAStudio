import os
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QMessageBox

from studio.exchange.portable import set_backup_enabled
from studio.rendering.asset_viewport import VIEW_PRESET_ANGLES
from entity_designer.wireframe_detail import DEFAULT_DETAIL, DETAIL_LEVELS
from entity_designer.wireframe_panel import DEFAULT_SIMPLIFICATION, SLIDER_STEPS, WireframePanel


class _Projection:
    def __init__(self):
        self.points = [(0.0, 0.0, 0.0), (1.0, 0.0, 1.0)]
        self.segments = [(0, 1)]
        self.saved_path = None

    def preview_image(self, size):
        image = QImage(size, QImage.Format.Format_ARGB32)
        image.fill(QColor("white"))
        return image

    def save(self, path):
        self.saved_path = path


class _NewProjection(_Projection):
    """A new wireframe that can really be written (one line)."""

    def __init__(self):
        super().__init__()
        from entity_designer.wireframe_generator import WireframeProjection
        self._real = WireframeProjection([(0.0, 0.0, 0.0), (800.0, 0.0, 800.0)], [[0, 1]])
        self.points, self.segments = self._real.points, self._real.segments

    def save(self, path):
        self._real.save(path)


class _Timer:
    def __init__(self, active):
        self.active = active
        self.events = []

    def isActive(self):
        return self.active

    def stop(self):
        self.active = False
        self.events.append("stop")

    def start(self, *_args):
        self.active = True
        self.events.append("start")


class _Viewport:
    def __init__(self, *, available=True, adapter=True, animating=False):
        self._indexed_adapter = object() if adapter else None
        self.available = available
        self._anim_timer = _Timer(animating)
        self._faces = [object()]
        self.camera = {"yaw": 0.0, "pitch": 0.0}

    def _camera_state(self):
        return dict(self.camera)

    def indexed_renderer_info(self):
        return {
            "available": self.available,
            "availability_reason": "test renderer unavailable"
            if not self.available else "",
        }


class WireframePanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_panel(self, generator, viewport=None, *, refresh=True):
        if viewport is None:
            viewport = _Viewport()
        loader = patch(
            "entity_designer.wireframe_panel._load_wireframe_generator",
            return_value=generator,
        )
        loader.start()
        self.addCleanup(loader.stop)
        panel = WireframePanel(viewport)
        self.addCleanup(self._dispose, panel)
        if refresh:
            panel.refresh()   # what opening the tab does
        return panel

    def _dispose(self, panel):
        # Destroy the panel now: leftover widgets break the later restyling of a whole window.
        panel.close()
        panel.deleteLater()
        self.app.sendPostedEvents(None, 0)

    def test_nothing_is_built_until_the_tab_is_opened(self):
        calls = []
        panel = self.make_panel(lambda viewport, **kw: calls.append(kw) or _Projection(), refresh=False)
        self.assertEqual(calls, [])
        self.assertFalse(panel.save_button.isEnabled())

    def test_showing_the_tab_builds_the_preview(self):
        calls = []
        panel = self.make_panel(lambda viewport, **kw: calls.append(kw) or _Projection(), refresh=False)
        panel.show()
        self.app.processEvents()
        self.assertEqual(len(calls), 1)
        self.assertTrue(panel.save_button.isEnabled())

    def test_defaults_to_current_view_and_transparency_when_available(self):
        calls = []

        def generate(viewport, **kwargs):
            calls.append(kwargs)
            return _Projection()

        panel = self.make_panel(generate)

        self.assertEqual(panel.view_preset_combo.currentText(), "Current View")
        self.assertEqual(
            [panel.view_preset_combo.itemText(i)
             for i in range(panel.view_preset_combo.count())],
            ["Current View", *VIEW_PRESET_ANGLES],
        )
        self.assertFalse(hasattr(panel, "respect_transparency_check"))
        self.assertEqual(panel.save_button.text(), "Export")
        self.assertTrue(panel.save_button.isEnabled())
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["preset"], "Current View")
        self.assertTrue(calls[0]["respect_transparency"])
        self.assertEqual(calls[0]["simplification"], DEFAULT_SIMPLIFICATION)

    def test_settings_regenerate_using_chosen_preset(self):
        calls = []

        def generate(viewport, **kwargs):
            calls.append(kwargs)
            return _Projection()

        panel = self.make_panel(generate)
        preset = next(iter(VIEW_PRESET_ANGLES))
        panel.view_preset_combo.setCurrentText(preset)

        self.assertEqual(calls[-1]["preset"], preset)
        self.assertTrue(calls[-1]["respect_transparency"])
        self.assertEqual(calls[-1]["simplification"], DEFAULT_SIMPLIFICATION)
        self.assertFalse(hasattr(panel, "simplification_spin"))
        self.assertTrue(panel.save_button.isEnabled())

    def test_the_current_wireframe_button_replaces_refresh_in_the_middle_of_the_row(self):
        panel = self.make_panel(lambda viewport, **kw: _Projection())
        self.assertFalse(hasattr(panel, "refresh_button"))
        self.assertTrue(panel.current_button.isCheckable())
        self.assertEqual(panel.current_button.text(), "Show current wireframe")
        row = panel.layout().itemAt(panel.layout().count()-1).layout()
        self.assertEqual(
            [row.itemAt(index).widget().text() for index in range(row.count())],
            ["Overwrite", "Show current wireframe", "Export"])

    def test_detail_slider_starts_at_the_default_level_and_regenerates_on_change(self):
        calls = []

        def generate(viewport, **kwargs):
            calls.append(kwargs)
            return _Projection()

        panel = self.make_panel(generate)
        slider = panel.detail_slider
        self.assertEqual((slider.minimum(), slider.maximum()), (0, SLIDER_STEPS))
        self.assertAlmostEqual(panel.detail_position(), DEFAULT_DETAIL)
        self.assertEqual(panel.detail_label.text(), DETAIL_LEVELS[DEFAULT_DETAIL].label)
        self.assertAlmostEqual(calls[0]["detail"], DEFAULT_DETAIL)
        slider.setValue(0)
        self.assertEqual(calls[-1]["detail"], 0)
        self.assertEqual(panel.detail_label.text(), DETAIL_LEVELS[0].label)
        slider.setValue(SLIDER_STEPS)
        self.assertAlmostEqual(calls[-1]["detail"], len(DETAIL_LEVELS)-1)
        self.assertEqual(panel.detail_label.text(), DETAIL_LEVELS[-1].label)
        self.assertTrue(panel.save_button.isEnabled())

    def test_detail_slider_has_fine_steps_between_the_levels(self):
        calls = []
        panel = self.make_panel(lambda viewport, **kw: calls.append(kw["detail"]) or _Projection())
        panel.detail_slider.setValue(62)
        self.assertAlmostEqual(calls[-1], 62/SLIDER_STEPS*(len(DETAIL_LEVELS)-1))
        self.assertNotIn(calls[-1], range(len(DETAIL_LEVELS)))

    def test_dragging_follows_after_a_pause_and_a_release_builds_the_preview_once(self):
        calls = []
        panel = self.make_panel(lambda viewport, **kw: calls.append(kw["detail"]) or _Projection())
        count = len(calls)
        panel.detail_slider.setSliderDown(True)
        for value in (70, 60, 50, 40):
            panel.detail_slider.setValue(value)
        self.assertEqual(len(calls), count)                     # nothing is built while the handle moves
        self.assertTrue(panel._preview_timer.isActive())
        self.assertEqual(panel.detail_label.text(), panel._level_label(40))
        panel._preview_timer.timeout.emit()                     # a pause: the preview follows
        self.assertEqual(len(calls), count+1)
        self.assertAlmostEqual(calls[-1], 40/SLIDER_STEPS*(len(DETAIL_LEVELS)-1))
        panel.detail_slider.setSliderDown(False)                # release: same value, no second build
        self.assertEqual(len(calls), count+1)
        self.assertFalse(panel._preview_timer.isActive())

    def test_transparency_error_disables_save_without_silent_fallback(self):
        calls = []

        def generate(viewport, **kwargs):
            calls.append(kwargs["respect_transparency"])
            if kwargs["respect_transparency"]:
                raise ValueError("texture mapping is unavailable")
            return _Projection()

        panel = self.make_panel(generate)

        self.assertEqual(calls, [True])
        self.assertFalse(panel.save_button.isEnabled())
        self.assertIn("texture mapping is unavailable", panel.error_label.text())
        self.assertTrue(panel.preview._image.isNull())

    def test_unavailable_renderer_defaults_to_disabled_geometry_option(self):
        calls = []

        def generate(viewport, **kwargs):
            calls.append(kwargs)
            return _Projection()

        panel = self.make_panel(generate, _Viewport(available=False, adapter=False))

        self.assertFalse(calls[0]["respect_transparency"])
        self.assertTrue(panel.save_button.isEnabled())

    def test_the_animation_is_paused_while_the_preview_is_built_and_resumes_after(self):
        viewport = _Viewport(animating=True)
        seen = []

        def generate(vp, **kwargs):
            seen.append(vp._anim_timer.isActive())
            return _Projection()

        self.make_panel(generate, viewport)
        self.assertEqual(seen, [False])
        self.assertTrue(viewport._anim_timer.isActive())

    def test_the_animation_resumes_even_if_the_preview_fails(self):
        viewport = _Viewport(animating=True)

        def generate(vp, **kwargs):
            raise ValueError("boom")

        panel = self.make_panel(generate, viewport)
        self.assertTrue(viewport._anim_timer.isActive())
        self.assertIn("boom", panel.error_label.text())

    def test_a_still_animation_stays_still(self):
        viewport = _Viewport(animating=False)
        self.make_panel(lambda vp, **kw: _Projection(), viewport)
        self.assertEqual(viewport._anim_timer.events, [])

    def test_the_preview_follows_the_camera_only_after_it_stops_moving(self):
        viewport = _Viewport()
        calls = []
        panel = self.make_panel(lambda vp, **kw: calls.append(kw["preset"]) or _Projection(), viewport)
        now = [100.0]
        panel._clock = lambda: now[0]
        panel.show()
        self.app.processEvents()
        self.assertTrue(panel._live_timer.isActive())
        count = len(calls)
        panel._live_tick()                                    # nothing moved: nothing is rebuilt
        self.assertEqual(len(calls), count)
        viewport.camera["yaw"] = 30.0                         # the camera starts moving
        panel._live_tick()
        viewport.camera["yaw"] = 60.0                         # ...and keeps moving
        now[0] += 0.1
        panel._live_tick()
        viewport.camera["yaw"] = 90.0
        now[0] += 0.1
        panel._live_tick()
        self.assertEqual(len(calls), count)                   # no wireframe is built while it moves
        now[0] += 0.1
        panel._live_tick()                                    # still not settled
        self.assertEqual(len(calls), count)
        now[0] += 0.3
        panel._live_tick()                                    # settled: built once
        self.assertEqual(len(calls), count+1)
        now[0] += 1.0
        panel._live_tick()
        self.assertEqual(len(calls), count+1)
        viewport._faces = [object(), object()]                # another model, then it rests
        panel._live_tick()
        now[0] += 0.3
        panel._live_tick()
        self.assertEqual(len(calls), count+2)

    def test_a_fixed_view_does_not_follow_the_camera_and_a_hidden_tab_does_not_work(self):
        viewport = _Viewport()
        calls = []
        panel = self.make_panel(lambda vp, **kw: calls.append(kw["preset"]) or _Projection(), viewport)
        panel.show()
        self.app.processEvents()
        panel.view_preset_combo.setCurrentText(next(iter(VIEW_PRESET_ANGLES)))
        count = len(calls)
        viewport.camera["yaw"] = 90.0
        panel._live_tick()
        self.assertEqual(len(calls), count)                   # a fixed view ignores the camera
        panel.view_preset_combo.setCurrentText("Current View")
        count = len(calls)
        panel.hide()
        self.assertFalse(panel._live_timer.isActive())
        viewport.camera["yaw"] = 120.0
        panel._live_tick()
        self.assertEqual(len(calls), count)

    def test_dragging_the_detail_slider_pauses_the_camera_following(self):
        viewport = _Viewport()
        calls = []
        panel = self.make_panel(lambda vp, **kw: calls.append(1) or _Projection(), viewport)
        panel.show()
        self.app.processEvents()
        count = len(calls)
        panel.detail_slider.setSliderDown(True)
        viewport.camera["yaw"] = 10.0
        panel._live_tick()
        self.assertEqual(len(calls), count)

    def test_the_last_picture_stays_while_the_next_one_is_built(self):
        seen = []
        holder = []
        panel = self.make_panel(
            lambda vp, **kw: seen.append(holder[0].preview._image.isNull()) or _Projection(),
            _Viewport(), refresh=False)
        holder.append(panel)
        panel.refresh()
        panel.refresh()
        self.assertEqual(seen, [True, False])                 # the second build starts with a picture on screen

    def _skl(self, folder, name="current.skl"):
        from entity_designer.wireframe_generator import WireframeProjection
        path = Path(folder) / name
        WireframeProjection([(0.0, 0.0, 0.0), (1000.0, 0.0, 0.0), (1000.0, 0.0, 1000.0)],
                            [[0, 1], [1, 2], [2, 0]]).save(path)
        return path

    def test_the_shared_resolver_accepts_skl_and_sklt_spellings(self):
        import tempfile
        from entity_designer.wireframe_panel import resolve_wireframe_path
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / "Data"
            (data / "Scripts").mkdir(parents=True)
            (data / "Wireless").mkdir()
            skl = self._skl(data / "Wireless", "Tank.SKL")
            script = data / "Scripts" / "Vehicles.cfg"
            self.assertEqual(
                resolve_wireframe_path("wireless/tank.sklt", script),
                skl.resolve())
            self.assertIsNone(resolve_wireframe_path("0", script))
            self.assertIsNone(resolve_wireframe_path(None, script))

    def test_the_current_wireframe_is_shown_first_and_nothing_is_generated(self):
        import tempfile
        calls = []
        panel = self.make_panel(lambda vp, **kw: calls.append(kw) or _Projection(), refresh=False)
        with tempfile.TemporaryDirectory() as folder:
            path = self._skl(folder)
            panel.set_current_wireframe(path)
            panel.show()
            self.app.processEvents()
        self.assertTrue(panel.current_button.isEnabled() and panel.current_button.isChecked())
        self.assertEqual(calls, [])
        self.assertIn("3 points", panel.stats_label.text())
        self.assertIn("current.skl", panel.stats_label.text())
        self.assertTrue(panel.path_label.isVisible())
        self.assertEqual(panel.path_label.text(), str(path))
        self.assertEqual(
            panel.path_label.toolTip(), f"Current wireframe file:\n{path}")
        self.assertFalse(panel.save_button.isEnabled())       # only the new wireframe is exported
        for widget in (panel.view_preset_combo, panel.detail_slider):
            self.assertFalse(widget.isEnabled())
        self.assertGreater(panel.current_button.graphicsEffect().blurRadius(), 0)

    def test_turning_the_button_off_shows_the_new_wireframe_that_can_be_adjusted(self):
        import tempfile
        calls = []
        panel = self.make_panel(lambda vp, **kw: calls.append(kw) or _Projection(), refresh=False)
        with tempfile.TemporaryDirectory() as folder:
            panel.set_current_wireframe(self._skl(folder))
        panel.show()
        self.app.processEvents()
        panel.current_button.setChecked(False)
        self.assertEqual(len(calls), 1)
        self.assertTrue(panel.save_button.isEnabled())
        for widget in (panel.view_preset_combo, panel.detail_slider):
            self.assertTrue(widget.isEnabled())
        self.assertEqual(panel.current_button.graphicsEffect().blurRadius(), 0)
        panel.detail_slider.setValue(10)                      # the new one can be edited
        self.assertEqual(len(calls), 2)
        panel.current_button.setChecked(True)                 # and the current one comes back without building
        self.assertEqual(len(calls), 2)
        self.assertFalse(panel.save_button.isEnabled())

    def test_a_definition_without_a_wireframe_goes_straight_to_the_new_one(self):
        calls = []
        panel = self.make_panel(lambda vp, **kw: calls.append(kw) or _Projection(), refresh=False)
        panel.set_current_wireframe(None)
        self.assertFalse(panel.current_button.isEnabled())
        self.assertFalse(panel.current_button.isChecked())
        self.assertTrue(panel.path_label.isHidden())
        self.assertEqual(panel.path_label.text(), "")
        panel.show()
        self.app.processEvents()
        self.assertEqual(len(calls), 1)
        self.assertTrue(panel.save_button.isEnabled())

    def test_an_unreadable_current_wireframe_is_reported_and_the_new_one_is_used(self):
        import tempfile
        panel = self.make_panel(lambda vp, **kw: _Projection(), refresh=False)
        with tempfile.TemporaryDirectory() as folder:
            broken = Path(folder) / "broken.skl"
            broken.write_bytes(b"not a skeleton")
            panel.set_current_wireframe(broken)
        self.assertFalse(panel.current_button.isEnabled())
        self.assertFalse(panel.current_button.isChecked())
        self.assertIn("current wireframe", panel.error_label.text())
        self.assertEqual(panel.path_label.text(), str(broken))
        self.assertFalse(panel.path_label.isHidden())

    def _panel_with_current(self, folder, generator=None):
        panel = self.make_panel(generator or (lambda vp, **kw: _NewProjection()), refresh=False)
        path = self._skl(folder)
        panel.set_current_wireframe(path)
        panel.show()
        self.app.processEvents()
        panel.current_button.setChecked(False)        # the new wireframe is shown
        return panel, path

    def test_overwrite_replaces_the_current_file_keeps_a_backup_and_reads_back(self):
        import tempfile
        set_backup_enabled(True)
        self.addCleanup(set_backup_enabled, False)
        with tempfile.TemporaryDirectory() as folder:
            panel, path = self._panel_with_current(folder)
            original = path.read_bytes()
            self.assertEqual(panel.path_label.text(), str(path))
            self.assertFalse(panel.path_label.isHidden())
            self.assertTrue(panel.overwrite_button.isEnabled())
            with patch("entity_designer.wireframe_panel.QMessageBox.question",
                       return_value=QMessageBox.StandardButton.Yes):
                panel.overwrite_button.click()
            self.assertNotEqual(path.read_bytes(), original)
            self.assertEqual((Path(folder) / "current.skl.bak").read_bytes(), original)
            self.assertEqual(len(panel.current_projection.segments), 1)   # the file now holds the new wireframe
            self.assertEqual(sorted(p.name for p in Path(folder).iterdir()), ["current.skl", "current.skl.bak"])

    def test_declining_the_confirmation_leaves_the_file_alone(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            panel, path = self._panel_with_current(folder)
            original = path.read_bytes()
            with patch("entity_designer.wireframe_panel.QMessageBox.question",
                       return_value=QMessageBox.StandardButton.No):
                panel.overwrite_button.click()
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual([p.name for p in Path(folder).iterdir()], ["current.skl"])

    def test_overwrite_is_only_possible_for_the_new_wireframe_of_a_definition_that_has_one(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            panel, _path = self._panel_with_current(folder)
            self.assertTrue(panel.overwrite_button.isEnabled())
            panel.current_button.setChecked(True)         # looking at the current one: nothing to replace it with
            self.assertFalse(panel.overwrite_button.isEnabled())
            self.assertFalse(panel.save_button.isEnabled())
        lone = self.make_panel(lambda vp, **kw: _Projection(), refresh=False)
        lone.set_current_wireframe(None)
        lone.show()
        self.app.processEvents()
        self.assertTrue(lone.save_button.isEnabled())     # it can still be exported
        self.assertFalse(lone.overwrite_button.isEnabled())

    def test_a_failed_overwrite_keeps_the_old_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            panel, path = self._panel_with_current(folder)
            original = path.read_bytes()
            panel.projection.save = lambda target: (_ for _ in ()).throw(OSError("disk full"))
            with patch("entity_designer.wireframe_panel.QMessageBox.question",
                       return_value=QMessageBox.StandardButton.Yes):
                panel.overwrite_button.click()
            self.assertEqual(path.read_bytes(), original)
            self.assertIn("disk full", panel.error_label.text())
            self.assertIn("unchanged", panel.error_label.text())

    def test_save_uses_selected_path_and_stays_in_the_tab(self):
        projection = _Projection()
        panel = self.make_panel(lambda *_args, **_kwargs: projection)
        chooser = patch(
            "entity_designer.wireframe_panel.QFileDialog.getSaveFileName",
            return_value=(r"C:\models\generated.skl", ""),
        )
        mocked_chooser = chooser.start()
        self.addCleanup(chooser.stop)

        panel.save_button.click()

        self.assertEqual(str(projection.saved_path), r"C:\models\generated.skl")
        mocked_chooser.assert_called_once()
        self.assertTrue(panel.save_button.isEnabled())

    def test_cancelling_the_file_chooser_saves_nothing(self):
        projection = _Projection()
        panel = self.make_panel(lambda *_args, **_kwargs: projection)
        chooser = patch(
            "entity_designer.wireframe_panel.QFileDialog.getSaveFileName",
            return_value=("", ""),
        )
        chooser.start()
        self.addCleanup(chooser.stop)
        panel.save_button.click()
        self.assertIsNone(projection.saved_path)


class EntityDesignerWireframeTabTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _window(self):
        from entity_designer.editor import CollisionEditorWindow
        window = CollisionEditorWindow()
        self.addCleanup(window.close)
        return window

    def test_the_wireframe_tab_follows_the_visuals_and_fx_tab(self):
        window = self._window()
        tabs = window.properties_tabs
        self.assertEqual(tabs.tabText(window.wireframe_tab_index), "Wireframe")
        self.assertEqual(window.wireframe_tab_index, window.fx_tab_index+1)
        self.assertIs(tabs.widget(window.wireframe_tab_index), window.wireframe_panel)

    def test_the_tab_reads_the_wireframe_key_of_the_loaded_definition(self):
        import tempfile
        from entity_designer.script_model import find_script_blocks
        from entity_designer.wireframe_generator import WireframeProjection
        window = self._window()
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / "Data"
            (data / "Scripts").mkdir(parents=True)
            (data / "Wireless").mkdir()
            skl = data / "Wireless" / "Tank.SKL"
            WireframeProjection([(0.0, 0.0, 0.0), (500.0, 0.0, 500.0)], [[0, 1]]).save(skl)
            script = data / "Scripts" / "Vehicles.cfg"
            text = "new_vehicle 5\n    name = Tank\n    wireframe = wireless/tank.sklt ; HUD\nend\n"
            script.write_text(text, encoding="utf-8")
            window._active_script_path = script
            block = find_script_blocks(text)[0]
            window._sync_wireframe_current(text, block)
            self.assertEqual(window.wireframe_panel.current_path.name, "Tank.SKL")
            self.assertTrue(window.wireframe_panel.current_button.isChecked())
            self.assertEqual(window.wireframe_panel.path_label.text(), str(skl.resolve()))
            self.assertFalse(window.wireframe_panel.path_label.isHidden())
            # A definition without the key leaves the tab on the new wireframe.
            plain = "new_vehicle 6\n    name = Plain\nend\n"
            window._sync_wireframe_current(plain, find_script_blocks(plain)[0])
            self.assertIsNone(window.wireframe_panel.current_path)
            self.assertFalse(window.wireframe_panel.current_button.isEnabled())
            self.assertTrue(window.wireframe_panel.path_label.isHidden())

    def test_the_move_gizmo_box_is_hidden_on_the_wireframe_tab(self):
        window = self._window()
        window.properties_tabs.setCurrentIndex(window.wireframe_tab_index)
        self.assertFalse(window.transform_box.isVisibleTo(window))


if __name__ == "__main__":
    unittest.main()
