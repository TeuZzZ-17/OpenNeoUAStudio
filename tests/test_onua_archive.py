"""The .ONUA archive: one model plus every resource it needs, packed as a plain ZIP."""

import os
from pathlib import Path
import tempfile
import zipfile
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from studio.assets.family import load_asset_family
from studio.exchange.onua_archive import import_onua
from studio.exchange.portable import AssetFamilyPackageError, MANIFEST_NAME
from model_editor.window import ModelEditorWindow

SET1 = Path(r"C:\Users\proto\Desktop\ProgettoUA\UA Rising\Data\Sets\Set1\Objects\SET.BAS")
MODEL = Path(r"C:\Users\proto\Desktop\ProgettoUA\UA Rising\Data\Models\Base\New"
             r"\Taer_Radar\Taer_Radar_Normal\Taer_Radar_Normal.BASE")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def no_modal_dialogs():
    """A message box would block the test forever: turn it into a failure."""
    def refuse(*args, **kwargs):
        raise AssertionError(f"unexpected dialog: {args[1:3]}")
    with patch("PySide6.QtWidgets.QMessageBox.critical", refuse),             patch("PySide6.QtWidgets.QMessageBox.warning", refuse),             patch("PySide6.QtWidgets.QMessageBox.question", refuse),             patch("PySide6.QtWidgets.QMessageBox.information", refuse):
        yield


def _window(app):
    window = ModelEditorWindow()
    family = load_asset_family(MODEL, setbas=SET1)
    window._set_family(family)
    window._select_owner(family.root_object.owner_path)
    return window


def test_menus_offer_archive_import_and_export_and_no_blender_format(app):
    window = ModelEditorWindow()
    try:
        assert window.import_onua_action.text() == "Import Whole Model from Archive (.ONUA)"
        assert window.export_onua_action.text() == "Export Whole Model as Archive (.ONUA)"
        assert not hasattr(window, "import_onua3d_action")
    finally:
        window.close()


def test_export_then_import_round_trip(app):
    if not (MODEL.exists() and SET1.exists()):
        pytest.skip("Local game reference is unavailable")
    window = _window(app)
    try:
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "radar.onua"
            with patch("studio.workbench.window_model_io.QFileDialog.getSaveFileName",
                       return_value=(str(archive), "")):
                window._export_onua_archive()
            assert archive.is_file()
            with zipfile.ZipFile(archive) as zipped:   # plain ZIP: any archiver can open it
                names = zipped.namelist()
            assert MANIFEST_NAME in names
            assert any(name.upper().endswith(".BASE") for name in names)
            assert any(name.upper().endswith(".SKLT") for name in names)
            destination = Path(temp) / "unpacked"
            entry = import_onua(archive, destination)
            assert entry.is_file()
            assert load_asset_family(entry, isolated_root=destination).root_object is not None
            with pytest.raises(AssetFamilyPackageError):   # destination must be empty
                import_onua(archive, destination)
    finally:
        window.close()


def test_import_refuses_unsafe_or_foreign_archives(tmp_path):
    foreign = tmp_path / "foreign.onua"
    with zipfile.ZipFile(foreign, "w") as zipped:
        zipped.writestr("readme.txt", "no manifest")
    with pytest.raises(AssetFamilyPackageError):
        import_onua(foreign, tmp_path / "a")
    escape = tmp_path / "escape.onua"
    with zipfile.ZipFile(escape, "w") as zipped:
        zipped.writestr("../evil.txt", "x")
        zipped.writestr(MANIFEST_NAME, "{}")
    with pytest.raises(AssetFamilyPackageError):
        import_onua(escape, tmp_path / "b")
    assert not (tmp_path / "b").exists() or not any((tmp_path / "b").iterdir())
    with pytest.raises(AssetFamilyPackageError):
        import_onua(tmp_path / "missing.onua", tmp_path / "c")
