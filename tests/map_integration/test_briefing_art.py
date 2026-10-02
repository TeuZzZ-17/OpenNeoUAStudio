import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from map_editor.core.asset_bridge import SetAssets
from map_editor.core.ldf_model import LdfDocument
from map_editor.render.briefing_art import (
    briefing_camera,
    render_briefing,
    style_briefing,
    write_briefing_pair,
)
from map_editor.render.sector_mesh import SectorMeshLibrary


@pytest.fixture(scope="module")
def library():
    app = QApplication.instance() or QApplication([])
    return app, SectorMeshLibrary(SetAssets(1).load())


def _document():
    doc = LdfDocument(mw=11, mh=7)
    doc.grids["type"][2][3] = "05"
    doc.grids["type"][3][6] = "05"
    doc.grids["hgt"][3][4] += 3
    doc.squads = [dict(owner=1, veh=1, num=2, x=3, y=3,
                       pos_x=3100.25, pos_z=-3100.5)]
    return doc


def _image_bytes(image: QImage) -> bytes:
    rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
    return bytes(rgba.bits())


def test_briefing_camera_matches_retail_full_map_aspect():
    doc = _document()
    camera = briefing_camera(doc)

    assert camera.yaw == 0 and camera.pitch == 90
    assert (camera.width, camera.height) == (302, 192)
    assert camera.width == pytest.approx(camera.height * doc.mw / doc.mh, abs=1)

    # DB_100.IFF in the retail data is 302x179 for a 54x32 level.
    retail_wide = briefing_camera(LdfDocument(mw=54, mh=32))
    assert (retail_wide.width, retail_wide.height) == (302, 179)


def test_briefing_style_is_deterministic():
    doc = _document()
    camera = briefing_camera(doc)
    source = QImage(camera.width, camera.height, QImage.Format.Format_RGBA8888)
    source.fill(0xff806040)

    first = style_briefing(source.copy(), doc, camera)
    second = style_briefing(source.copy(), doc, camera)

    assert first.format() == QImage.Format.Format_RGBA8888
    assert (first.width(), first.height()) == (camera.width, camera.height)
    assert _image_bytes(first) == _image_bytes(second)


def test_render_is_deterministic_proportional_and_does_not_mutate_document(library):
    _, lib = library
    doc = _document()
    before = doc.snapshot()

    first = render_briefing(doc, lib)
    second = render_briefing(doc, lib)

    assert not first.isNull()
    assert first.format() == QImage.Format.Format_RGBA8888
    assert (first.width(), first.height()) == (
        briefing_camera(doc).width, briefing_camera(doc).height)
    assert first.width() == pytest.approx(
        first.height() * doc.mw / doc.mh, abs=1)
    assert _image_bytes(first) == _image_bytes(second)
    assert doc.snapshot() == before


def test_write_briefing_pair_uses_identical_png_bytes(library, tmp_path):
    _, lib = library
    image = render_briefing(_document(), lib)

    mb_path, db_path = write_briefing_pair(image, tmp_path, "Field_01")

    assert mb_path.name == "Mb_Field_01.png"
    assert db_path.name == "Db_Field_01.png"
    assert mb_path.read_bytes() == db_path.read_bytes()
    assert mb_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_write_briefing_pair_never_overwrites_existing_art(library, tmp_path):
    _, lib = library
    image = render_briefing(_document(), lib)
    mb_path = tmp_path / "Mb_Existing.png"
    original = b"original artwork"
    mb_path.write_bytes(original)

    with pytest.raises(FileExistsError):
        write_briefing_pair(image, tmp_path, "Existing")

    assert mb_path.read_bytes() == original
    assert not (tmp_path / "Db_Existing.png").exists()


@pytest.mark.parametrize("stem", ["", "../outside", "folder/name", "bad:name", "bad."])
def test_write_briefing_pair_rejects_invalid_names(stem, tmp_path):
    image = QImage(8, 8, QImage.Format.Format_RGBA8888)
    image.fill(0xff112233)

    with pytest.raises(ValueError):
        write_briefing_pair(image, tmp_path, stem)

    assert list(tmp_path.iterdir()) == []
