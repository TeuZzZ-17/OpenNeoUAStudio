import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QDialog

from assembly_viewer import VIEW_PRESET_ANGLES
from model_editor import ModelEditorWindow
from model_editor.wireframe_dialog import WireframeGeneratorDialog


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


class _Viewport:
    def __init__(self, *, available=True, adapter=True):
        self._indexed_adapter = object() if adapter else None
        self.available = available

    def indexed_renderer_info(self):
        return {
            "available": self.available,
            "availability_reason": "test renderer unavailable"
            if not self.available else "",
        }


class WireframeGeneratorDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_dialog(self, generator, viewport=None):
        if viewport is None:
            viewport = _Viewport()
        loader = patch(
            "model_editor.wireframe_dialog._load_wireframe_generator",
            return_value=generator,
        )
        loader.start()
        self.addCleanup(loader.stop)
        dialog = WireframeGeneratorDialog(viewport)
        self.addCleanup(dialog.close)
        return dialog

    def test_defaults_to_current_view_and_transparency_when_available(self):
        calls = []

        def generate(viewport, **kwargs):
            calls.append(kwargs)
            return _Projection()

        dialog = self.make_dialog(generate)

        self.assertEqual(dialog.view_preset_combo.currentText(), "Current View")
        self.assertEqual(
            [dialog.view_preset_combo.itemText(i)
             for i in range(dialog.view_preset_combo.count())],
            ["Current View", *VIEW_PRESET_ANGLES],
        )
        self.assertTrue(dialog.respect_transparency_check.isChecked())
        self.assertTrue(dialog.save_button.isEnabled())
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["preset"], "Current View")
        self.assertTrue(calls[0]["respect_transparency"])
        self.assertEqual(calls[0]["simplification"], 3.0)

    def test_settings_regenerate_using_chosen_preset_and_geometry_mode(self):
        calls = []

        def generate(viewport, **kwargs):
            calls.append(kwargs)
            return _Projection()

        dialog = self.make_dialog(generate)
        preset = next(iter(VIEW_PRESET_ANGLES))
        dialog.view_preset_combo.setCurrentText(preset)
        dialog.respect_transparency_check.setChecked(False)
        dialog.simplification_spin.setValue(5.0)

        self.assertEqual(calls[-1]["preset"], preset)
        self.assertFalse(calls[-1]["respect_transparency"])
        self.assertEqual(calls[-1]["simplification"], 5.0)
        self.assertTrue(dialog.save_button.isEnabled())

    def test_transparency_error_disables_save_without_silent_fallback(self):
        calls = []

        def generate(viewport, **kwargs):
            calls.append(kwargs["respect_transparency"])
            if kwargs["respect_transparency"]:
                raise ValueError("texture mapping is unavailable")
            return _Projection()

        dialog = self.make_dialog(generate)

        self.assertEqual(calls, [True])
        self.assertFalse(dialog.save_button.isEnabled())
        self.assertIn("texture mapping is unavailable", dialog.error_label.text())
        self.assertTrue(dialog.preview._image.isNull())

        dialog.respect_transparency_check.setChecked(False)
        self.assertTrue(dialog.save_button.isEnabled())
        self.assertEqual(calls, [True, False])

        dialog.respect_transparency_check.setChecked(True)

        self.assertEqual(calls, [True, False, True])
        self.assertFalse(dialog.save_button.isEnabled())
        self.assertTrue(dialog.preview._image.isNull())

        dialog.respect_transparency_check.setChecked(False)
        self.assertEqual(calls, [True, False, True, False])
        self.assertTrue(dialog.save_button.isEnabled())

    def test_unavailable_renderer_defaults_to_disabled_geometry_option(self):
        calls = []

        def generate(viewport, **kwargs):
            calls.append(kwargs)
            return _Projection()

        dialog = self.make_dialog(
            generate, _Viewport(available=False, adapter=False))

        self.assertFalse(dialog.respect_transparency_check.isEnabled())
        self.assertFalse(dialog.respect_transparency_check.isChecked())
        self.assertFalse(calls[0]["respect_transparency"])
        self.assertTrue(dialog.save_button.isEnabled())

    def test_cancel_does_not_save_or_open_file_dialog(self):
        projection = _Projection()
        dialog = self.make_dialog(lambda *_args, **_kwargs: projection)
        chooser = patch(
            "model_editor.wireframe_dialog.QFileDialog.getSaveFileName")
        mocked_chooser = chooser.start()
        self.addCleanup(chooser.stop)

        dialog.cancel_button.click()

        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        self.assertIsNone(projection.saved_path)
        mocked_chooser.assert_not_called()

    def test_save_uses_selected_path_and_accepts_after_success(self):
        projection = _Projection()
        dialog = self.make_dialog(lambda *_args, **_kwargs: projection)
        chooser = patch(
            "model_editor.wireframe_dialog.QFileDialog.getSaveFileName",
            return_value=(r"C:\models\generated.skl", ""),
        )
        mocked_chooser = chooser.start()
        self.addCleanup(chooser.stop)

        dialog.save_button.click()

        self.assertEqual(str(projection.saved_path), r"C:\models\generated.skl")
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        mocked_chooser.assert_called_once()


class ModelEditorWireframeActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_file_action_is_before_exit_and_tracks_model_availability(self):
        window = ModelEditorWindow()
        self.addCleanup(window.close)

        actions = window.file_menu.actions()
        self.assertLess(actions.index(window.generate_wireframe_action),
                        actions.index(window.exit_action))
        self.assertFalse(window.generate_wireframe_action.isEnabled())

        window.viewport._faces.append(object())
        window.file_menu.aboutToShow.emit()

        self.assertTrue(window.generate_wireframe_action.isEnabled())

    def test_modal_dialog_pauses_and_restores_active_animation_timer(self):
        window = ModelEditorWindow()
        self.addCleanup(window.close)
        window.viewport._faces.append(object())
        window.viewport._anim_timer.start(1000)
        self.addCleanup(window.viewport._anim_timer.stop)
        observations = []

        class _Dialog:
            def __init__(self, viewport, parent):
                observations.append((viewport._anim_timer.isActive(), parent))

            def exec(self):
                observations.append(window.viewport._anim_timer.isActive())
                return 0

        with patch("model_editor.wireframe_dialog.WireframeGeneratorDialog",
                   _Dialog):
            window._open_wireframe_generator()

        self.assertEqual(observations[0], (False, window))
        self.assertFalse(observations[1])
        self.assertTrue(window.viewport._anim_timer.isActive())

    def test_modal_dialog_restores_animation_timer_when_open_raises(self):
        window = ModelEditorWindow()
        self.addCleanup(window.close)
        window.viewport._faces.append(object())
        window.viewport._anim_timer.start(1000)
        self.addCleanup(window.viewport._anim_timer.stop)

        class _Dialog:
            def __init__(self, viewport, parent):
                self.timer = viewport._anim_timer

            def exec(self):
                raise RuntimeError("dialog failed")

        with patch("model_editor.wireframe_dialog.WireframeGeneratorDialog",
                   _Dialog):
            with self.assertRaisesRegex(RuntimeError, "dialog failed"):
                window._open_wireframe_generator()

        self.assertTrue(window.viewport._anim_timer.isActive())


if __name__ == "__main__":
    unittest.main()
