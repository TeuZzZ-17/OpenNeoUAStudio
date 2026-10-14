import json
import os
import tempfile
import time
import unittest
import zipfile
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QGroupBox

from studio.exchange.sevenzip import SIGNATURE as SEVENZIP_SIGNATURE
from snapshot_studio.batch_export import (
    BatchManifestRow,
    VPSnapshotBatchPanel,
    assign_source_folder_names,
    batch_renderable_sources,
    drawable_polygon_count,
)
from studio.rendering.snapshot_export import (
    AnimationExportCancelled,
    export_animation_atomic,
    pillow_supports_animated_webp,
    pillow_supports_static_webp,
    write_static_image_atomic,
)
from snapshot_studio import SnapshotStudioWindow


class _AnimatedViewport:
    has_animation = True

    def __init__(self, duration_ms=250):
        self.animation_duration_ms = duration_ms
        self.times = []
        self.time_ms = 0.0

    def seek_animation(self, time_ms):
        self.time_ms = time_ms
        self.times.append(time_ms)

    def render_snapshot(self, size, background, include_guides=False):
        image = QImage(size, QImage.Format.Format_ARGB32)
        image.fill(Qt.transparent)
        colors = (
            QColor(220, 20, 30), QColor(20, 40, 220),
            QColor(20, 180, 60), QColor(230, 180, 20),
            QColor(150, 30, 180),
        )
        color = colors[int(self.time_ms // 50) % len(colors)]
        image.setPixelColor(3, 3, color)
        if background is not None:
            image.fill(background)
            image.setPixelColor(3, 3, color)
        return image


class SnapshotExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_gif_frames_keep_transparency_and_timing(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unit.gif"
            viewport = _AnimatedViewport(250)
            count = export_animation_atomic(
                viewport, path, "gif", QSize(8, 8), None, False,
                fps=20, speed=1.0)
            self.assertEqual(count, 5)
            self.assertEqual(viewport.times, [0, 50, 100, 150, 200])
            with Image.open(path) as animation:
                self.assertEqual(animation.n_frames, 5)
                for index in range(animation.n_frames):
                    animation.seek(index)
                    self.assertEqual(animation.info["duration"], 50)
                    rgba = animation.convert("RGBA")
                    self.assertEqual(rgba.getpixel((0, 0))[3], 0)
                    self.assertEqual(rgba.getpixel((3, 3))[3], 255)

    def test_fixed_gif_duration_clips_the_last_frame(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixed.gif"
            count = export_animation_atomic(
                _AnimatedViewport(1000), path, "gif", QSize(8, 8),
                None, False, fps=20, speed=1.0,
                duration_seconds=0.16)
            self.assertEqual(count, 4)
            with Image.open(path) as animation:
                self.assertEqual(animation.n_frames, 4)
                durations = []
                for index in range(animation.n_frames):
                    animation.seek(index)
                    durations.append(animation.info["duration"])
                self.assertEqual(durations, [50, 50, 50, 10])

    @unittest.skipUnless(
        pillow_supports_animated_webp(), "Pillow WebP animation is unavailable")
    def test_webp_animation_decodes_with_expected_frames_and_timing(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unit.webp"
            viewport = _AnimatedViewport(250)
            count = export_animation_atomic(
                viewport, path, "webp_anim", QSize(8, 8), None, False,
                fps=10, speed=2.0, duration_seconds=0.3, quality=100)
            self.assertEqual(count, 3)
            self.assertEqual(viewport.times, [0, 200, 400])
            with Image.open(path) as animation:
                self.assertEqual(animation.n_frames, 3)
                for index in range(animation.n_frames):
                    animation.seek(index)
                    self.assertEqual(
                        animation.convert("RGBA").getpixel((0, 0))[3], 0)

    def test_cancelled_animation_does_not_leave_or_replace_a_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cancel.gif"
            path.write_bytes(b"previous file")
            viewport = _AnimatedViewport(250)
            calls = 0

            def cancelled():
                nonlocal calls
                calls += 1
                return calls == 2

            with self.assertRaises(AnimationExportCancelled):
                export_animation_atomic(
                    viewport, path, "gif", QSize(8, 8), None, False,
                    fps=20, speed=1.0, cancelled=cancelled)
            self.assertEqual(path.read_bytes(), b"previous file")
            self.assertEqual(list(Path(directory).glob("*.part")), [])

    def test_animation_memory_budget_fails_before_rendering(self):
        with tempfile.TemporaryDirectory() as directory:
            viewport = _AnimatedViewport(1000)
            with self.assertRaisesRegex(MemoryError, "Reduce the image size"):
                export_animation_atomic(
                    viewport, Path(directory) / "large.gif", "gif",
                    QSize(100, 100), None, False, fps=60, speed=1.0,
                    memory_budget_bytes=1)
            self.assertEqual(viewport.times, [])

    @unittest.skipUnless(
        pillow_supports_static_webp(), "Pillow static WebP is unavailable")
    def test_static_webp_remains_decodable(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "still.webp"
            image = QImage(8, 8, QImage.Format.Format_ARGB32)
            image.fill(QColor(40, 80, 120, 255))
            write_static_image_atomic(image, path, "webp", quality=100)
            with Image.open(path) as decoded:
                self.assertEqual(decoded.format, "WEBP")
                self.assertEqual(decoded.size, (8, 8))

    def test_batch_resource_names_add_ids_only_for_collisions(self):
        sources = [
            SimpleNamespace(
                source_kind="VP", source_id=3, vp_id=3,
                resource_name="Models/Shared.SKLT", base_name=""),
            SimpleNamespace(
                source_kind="VP", source_id=8, vp_id=8,
                resource_name="Other/Shared.SKLT", base_name=""),
            SimpleNamespace(
                source_kind="SKLT", source_id=11, vp_id=-1,
                resource_name="Models/Unique.SKLT", base_name=""),
        ]
        assign_source_folder_names(sources, "resource")
        self.assertEqual(
            [source.folder_name for source in sources],
            ["Shared_VP_0003", "Shared_VP_0008", "Unique"],
        )
        assign_source_folder_names(sources, "vp_id")
        self.assertEqual(
            [source.folder_name for source in sources],
            ["VP_0003", "VP_0008", "SKLT_0011"],
        )
        sources[0].base_name = "Objects/Complete_Model.base"
        assign_source_folder_names(sources, "resource")
        self.assertEqual(sources[0].folder_name, "Complete_Model")

    def test_long_batch_resource_name_collisions_reserve_suffix_and_counter(self):
        sources = [
            SimpleNamespace(
                source_kind="VP", source_id=3, vp_id=3,
                resource_name=f"Models/{'x' * 80}.SKLT", base_name="")
            for _ in range(3)
        ]
        assign_source_folder_names(sources, "resource")
        names = [source.folder_name for source in sources]
        self.assertEqual(len(set(name.casefold() for name in names)), 3)
        self.assertTrue(all(len(name) <= 80 for name in names))
        self.assertTrue(names[0].endswith("_VP_0003"))
        self.assertTrue(names[1].endswith("_VP_0003_2"))
        self.assertTrue(names[2].endswith("_VP_0003_3"))

    def test_batch_includes_vp129_particle_source_without_static_polygons(self):
        skeleton = SimpleNamespace(points=[(0, 0, 0)], polygons=[])
        self.assertEqual(drawable_polygon_count(skeleton), 0)
        particle_vp = SimpleNamespace(
            vp_id=129, decoded_skeleton=skeleton,
            skip_status="PARTICLE_ONLY", mapped_object=object())
        unmapped_particle = SimpleNamespace(
            skip_status="PARTICLE_ONLY", mapped_object=None)
        polygon_vp = SimpleNamespace(skip_status="", mapped_object=object())
        self.assertEqual(
            batch_renderable_sources(
                [particle_vp, unmapped_particle, polygon_vp]),
            [particle_vp, polygon_vp])

    def test_export_controls_share_one_group_and_keep_distinct_actions(self):
        window = SnapshotStudioWindow()
        try:
            export_groups = [
                box for box in window._snapshot_panel.findChildren(QGroupBox)
                if box.title() == "Export"
            ]
            self.assertEqual(export_groups, [window._snapshot_export_box])
            self.assertIs(window.vp_batch_panel.parentWidget(),
                          window._snapshot_export_box)
            self.assertEqual(window.snapshot_export_button.text(),
                             "Export Image")
            self.assertEqual(window.snapshot_figurine_button.text(),
                             "Export Image Set")
            self.assertEqual(window.vp_batch_panel.export_button.text(),
                             "Export All VP")
            self.assertTrue(window.vp_batch_panel.zip_check.isChecked())
            self.assertEqual(window.snapshot_fps_box.value(), 25)
            self.assertEqual(window.snapshot_export_speed_box.value(), 1.0)
        finally:
            window.close()

    def test_batch_running_keeps_cancel_enabled_and_restores_export_controls(self):
        window = SnapshotStudioWindow()
        panel = window.vp_batch_panel
        quality_was_enabled = window.snapshot_quality_box.isEnabled()
        try:
            panel._set_running(True)
            self.assertTrue(panel.cancel_button.isEnabled())
            self.assertTrue(window._snapshot_studio_box.isEnabled())
            self.assertTrue(window._snapshot_export_box.isEnabled())
            self.assertFalse(window.snapshot_export_button.isEnabled())
            self.assertFalse(window.snapshot_format_combo.isEnabled())

            panel._set_running(False)
            self.assertFalse(panel.cancel_button.isEnabled())
            self.assertTrue(window.snapshot_export_button.isEnabled())
            self.assertTrue(window.snapshot_format_combo.isEnabled())
            self.assertEqual(
                window.snapshot_quality_box.isEnabled(), quality_was_enabled)
        finally:
            if panel._running:
                panel._set_running(False)
            window.close()

    def test_batch_uses_shared_format_size_zoom_and_background_in_zip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Corpus"
            image = QImage(8, 9, QImage.Format.Format_ARGB32)
            image.fill(QColor(20, 30, 40))
            source = SimpleNamespace(
                folder_name="Shared_VP_0003", source_kind="VP",
                source_id=3, geometry_only=False)
            viewport = Mock()
            viewport.render_snapshot.return_value = image
            panel = SimpleNamespace(
                _running=True, _cancel_requested=False,
                _queue=[(source, 1, "Front")], _queue_index=0,
                progress=Mock(), status_label=Mock(), _root=root,
                _load_source=Mock(), skip_existing_check=Mock(),
                _batch_viewport=viewport, _target_size=QSize(8, 9),
                _zoom=145, _format_id="jpg", _quality=81,
                _fps=25, _speed=1.0, _duration_seconds=None,
                _background=QColor(50, 60, 70),
                _record=Mock(), _warnings=[], _failed=0, _written=0,
                _step=Mock(),
            )
            panel.skip_existing_check.isChecked.return_value = False
            written = []

            def write_image(rendered, path, format_id, quality):
                written.append((path, format_id, quality))
                path.write_bytes(b"image")

            with patch("snapshot_studio.batch_export.QTimer.singleShot"), \
                    patch(
                        "snapshot_studio.batch_export.write_static_image_atomic",
                        side_effect=write_image):
                VPSnapshotBatchPanel._step(panel)

            output_path, format_id, quality = written[0]
            self.assertEqual(output_path, root / "Shared_VP_0003" / "01_front.jpg")
            self.assertEqual((format_id, quality), ("jpg", 81))
            viewport.apply_snapshot_preset.assert_called_once_with(
                "Front", QSize(8, 9), 145)
            viewport.render_snapshot.assert_called_once_with(
                QSize(8, 9), QColor(50, 60, 70), include_guides=False)

            output_path.parent.mkdir(parents=True, exist_ok=True)
            (root / "unrelated.tmp").write_bytes(b"outside this run")
            (root / "manifest-image.jpg").write_bytes(b"image")
            panel._root = root
            panel._rows = [BatchManifestRow(
                "VP", 3, 3, "", 4, "Models/Shared.SKLT", "root",
                "Models/Shared.SKLT", "Front", "Shared_VP_0003/01_front.jpg",
                "WRITTEN", "800x900 JPEG")]
            panel._warnings = []
            panel._sources = [source]
            panel._target_size = QSize(8, 9)
            panel._zoom = 145
            panel._format_id = "webp_anim"
            panel._quality = 81
            panel._name_mode = "resource"
            panel._background = QColor(50, 60, 70)
            panel._fps = 30
            panel._speed = 2.0
            panel._duration_seconds = 3.5
            panel._renderer_mode = "textured"
            panel._written = 1
            panel._existing = 0
            panel._skipped_models = 0
            panel._skipped_animations = 2
            panel._failed = 0
            panel._started_at = time.time()
            panel._cancel_requested = False
            panel.window = SimpleNamespace(
                _setbas=None, _vp_source="embedded", _vp_source_path=None)
            VPSnapshotBatchPanel._write_manifests(panel, cancelled=False)
            # The archive helpers are methods of the real panel.
            panel.level_slider = SimpleNamespace(value=lambda: 6)
            panel.method_combo = SimpleNamespace(currentData=lambda: "lzma2")
            for name in ("_archive_members", "_tick", "_write_archive"):
                setattr(panel, name, partial(getattr(VPSnapshotBatchPanel, name), panel))
            zip_path = VPSnapshotBatchPanel._create_zip(panel)
            self.assertEqual(zip_path.suffix, ".zip")
            run_info = json.loads((root / "run_info.json").read_text(
                encoding="utf-8"))
            self.assertEqual(run_info["format_id"], "webp_anim")
            self.assertEqual(run_info["width"], 8)
            self.assertEqual(run_info["height"], 9)
            self.assertEqual(run_info["zoom_percent"], 145)
            self.assertEqual(run_info["animation_fps"], 30)
            self.assertEqual(run_info["animation_speed"], 2.0)
            self.assertEqual(run_info["animation_duration_seconds"], 3.5)
            with zipfile.ZipFile(zip_path) as archive:
                names = set(archive.namelist())
                self.assertIn("Corpus/manifest.json", names)
                self.assertIn(
                    "Corpus/Shared_VP_0003/01_front.jpg", names)
                self.assertNotIn("Corpus/unrelated.tmp", names)
            sevenzip_path = VPSnapshotBatchPanel._create_7z(panel)
            self.assertEqual(sevenzip_path.suffix, ".7z")
            self.assertFalse(sevenzip_path.with_name(sevenzip_path.name + ".part").exists())
            self.assertEqual(sevenzip_path.read_bytes()[:6], SEVENZIP_SIGNATURE)
            panel._cancel_requested = True
            self.assertIsNone(VPSnapshotBatchPanel._create_7z(panel))
            self.assertFalse(sevenzip_path.with_name(sevenzip_path.name + ".part").exists())


if __name__ == "__main__":
    unittest.main()
