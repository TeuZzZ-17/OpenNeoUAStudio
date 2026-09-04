import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSize
from PySide6.QtGui import QImage, QImageWriter
from snapshot_studio.batch_export import VPSnapshotBatchPanel


class BatchPngTests(unittest.TestCase):
    def run_step(self, root):
        image = QImage(8, 8, QImage.Format_ARGB32)
        image.fill(0xff123456)
        source = SimpleNamespace(folder_name="unit", geometry_only=False)
        panel = SimpleNamespace(
            _running=True, _cancel_requested=False,
            _queue=[(source, 1, "Front")], _queue_index=0,
            progress=Mock(), status_label=Mock(), _root=root,
            _load_source=Mock(), skip_existing_check=Mock(),
            _batch_viewport=Mock(), _target_size=QSize(8, 8), _zoom=1,
            _record=Mock(), _warnings=[], _failed=0, _written=0,
            _step=Mock(),
        )
        panel.skip_existing_check.isChecked.return_value = False
        panel._batch_viewport.render_snapshot.return_value = image
        with patch("snapshot_studio.batch_export.QTimer.singleShot"):
            VPSnapshotBatchPanel._step(panel)
        return panel

    def test_writes_readable_png_without_partial_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            panel = self.run_step(root)
            self.assertEqual(panel._failed, 0, panel._warnings)
            self.assertEqual(panel._written, 1)
            files = list(root.rglob("*.png"))
            self.assertEqual(len(files), 1)
            result = QImage(str(files[0]))
            self.assertEqual(result.size(), QSize(8, 8))
            self.assertEqual(result.pixel(0, 0), 0xff123456)
            self.assertEqual(list(root.rglob("*.part")), [])

    def test_failed_write_cleans_partial_file_and_reports_error(self):
        class FailedWriter(QImageWriter):
            def write(self, image):
                super().write(image)
                return False

            def errorString(self):
                return "Injected write failure"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("snapshot_studio.batch_export.QImageWriter", FailedWriter):
                panel = self.run_step(root)
            self.assertEqual(panel._failed, 1)
            self.assertEqual(panel._written, 0)
            self.assertIn("Injected write failure", panel._warnings[0])
            self.assertEqual(list(root.rglob("*.part")), [])
            self.assertEqual(list(root.rglob("*.png")), [])
