"""Optional original-data regressions, configured without copying game data into Git.

ONUA3D_HUBI2_PACKAGE: original VP_HUBI2.onua3d regression input.
ONUA3D_RETAIL_ARCHIVE: original SET.BAS containing the retail VP entries.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from assembly_window import AssemblyWindow
from asset_family_package import validate_family_package
from onua3d_export import write_onua3d
from tests.test_onua3d_export import decode_glb, values


class Onua3dRetailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_original_hubi2_retains_all_prop1_frames_timing_and_ua_bytes(self):
        source = Path(os.environ.get("ONUA3D_HUBI2_PACKAGE", ""))
        if not source.is_file():
            self.skipTest("set ONUA3D_HUBI2_PACKAGE to the original regression package")
        before = source.read_bytes()
        with tempfile.TemporaryDirectory() as temp, zipfile.ZipFile(source) as original:
            root = Path(temp)
            original.extractall(root / "source")
            target = root / "result.onua3d"
            write_onua3d(root / "source" / "ua_family", target)
            first = target.read_bytes()
            write_onua3d(root / "source" / "ua_family", target)
            self.assertEqual(first, target.read_bytes())
            with zipfile.ZipFile(target) as result:
                for name in original.namelist():
                    if name.startswith("ua_family/"):
                        self.assertEqual(original.read(name), result.read(name), name)
                manifest = json.loads(result.read("onua3d_manifest.json"))
                blocks = manifest["nodes"][0]["vanm"]
                self.assertEqual(len(blocks), 2)
                for block in blocks:
                    self.assertEqual(block["logical_name"], "PROP1.ANM")
                    self.assertEqual(block["period_ticks"], 320)
                    self.assertEqual([f["frame_time"] for f in block["frames"]], [40] * 8)
                    self.assertEqual([f["uv_group_id"] for f in block["frames"]], list(range(8)))
                doc, binary = decode_glb(result.read("scene.glb"))
                self.assertNotIn("skins", doc)
                channels = doc["animations"][0]["channels"]
                samplers = doc["animations"][0]["samplers"]
                for tick in [0, 39, 40, 79, 80, 280, 319, 320, 360]:
                    active = []
                    for channel in channels:
                        sampler = samplers[channel["sampler"]]
                        keys = values(doc, binary, sampler["input"])
                        scales = values(doc, binary, sampler["output"])
                        selected = max(i for i, key in enumerate(keys) if key <= tick / 1024)
                        if scales[selected] == (1, 1, 1):
                            active.append(doc["nodes"][channel["target"]["node"]]["extras"]["onua3d"])
                    self.assertEqual(sorted((a["block_index"], a["frame_index"]) for a in active),
                                     [(0, (tick // 40) % 8), (1, (tick // 40) % 8)])
        self.assertEqual(source.read_bytes(), before)

    def test_retail_family_exports_use_the_actual_ui_business_path(self):
        source = Path(os.environ.get("ONUA3D_RETAIL_ARCHIVE", ""))
        if not source.is_file():
            self.skipTest("set ONUA3D_RETAIL_ARCHIVE to an original SET.BAS")
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        window = AssemblyWindow()
        try:
            window.open_setbas(str(source))
            with tempfile.TemporaryDirectory() as temp:
                for name in ("VP_FLAK1", "VPfFLAK1", "VP_HUBI4", "VP_MIG"):
                    with self.subTest(asset=name):
                        obj = window._activate_setbas_base(name + ".base")
                        self.assertIsNotNone(obj)
                        target = Path(temp) / (name + ".onua3d")
                        with patch("assembly_window.QFileDialog.getSaveFileName", return_value=(str(target), "")), \
                                patch("assembly_window.QMessageBox.critical") as errors, \
                                patch.object(window, "_notify"):
                            window._export_to_blender()
                            errors.assert_not_called()
                            first = target.read_bytes()
                            window._export_to_blender()
                            errors.assert_not_called()
                        self.assertEqual(first, target.read_bytes())
                        with zipfile.ZipFile(target) as archive:
                            archive.extractall(Path(temp) / name)
                        validation = validate_family_package(Path(temp) / name / "ua_family")
                        self.assertTrue(validation.valid, validation.errors)
                        self.assertEqual(len(validation.family.root_object.skeleton.polygons), len(obj.skeleton.polygons))
        finally:
            window.close()
            window.deleteLater()
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
