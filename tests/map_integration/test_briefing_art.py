import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
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


def test_briefing_style_preserves_source_hues_and_ignores_level_info_art_names():
    doc = _document()
    doc.lvl_info.update(mbmap="MB_01", dbmap="DB30")
    camera = briefing_camera(doc)
    source_pixels = np.empty((camera.height, camera.width, 4), dtype=np.uint8)
    source_pixels[:, :, :3] = (40, 80, 140)
    source_pixels[:, camera.width // 2:, :3] = (140, 90, 40)
    source_pixels[:, :, 3] = 255
    source = QImage(source_pixels.data, camera.width, camera.height,
                     camera.width * 4, QImage.Format.Format_RGBA8888).copy()
    source_bytes = _image_bytes(source)
    before = doc.snapshot()

    styled = style_briefing(source, doc, camera)
    output = np.frombuffer(_image_bytes(styled), np.uint8).reshape(
        styled.height(), styled.width(), 4)
    yy, xx = np.indices((camera.height, camera.width))
    wx = -(xx - camera.width / 2) / camera.zoom + camera.center[0]
    wz = (yy - camera.height / 2) / camera.zoom + camera.center[2]
    cols, rows = np.floor(wx / 4000).astype(int), np.floor(-wz / 4000).astype(int)
    inside = ((cols >= 1) & (cols < doc.mw - 1) & (rows >= 1)
              & (rows < doc.mh - 1) & (output[:, :, 3] > 0))
    blue_area = inside & (xx > camera.width * .28) & (xx < camera.width * .43)
    brown_area = inside & (xx > camera.width * .57) & (xx < camera.width * .72)
    blue = np.median(output[blue_area, :3], axis=0)
    brown = np.median(output[brown_area, :3], axis=0)

    assert blue[2] > blue[1] > blue[0]
    assert brown[0] > brown[1] > brown[2]
    assert np.allclose(blue / blue.sum(), np.array((40, 80, 140)) / 260, atol=.025)
    assert np.allclose(brown / brown.sum(), np.array((140, 90, 40)) / 270, atol=.025)
    assert _image_bytes(source) == source_bytes
    assert doc.snapshot() == before


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


def test_write_briefing_pair_keeps_geometry_and_darkens_debriefing(library, tmp_path):
    _, lib = library
    image = render_briefing(_document(), lib)

    source = _image_bytes(image)
    mb_path, db_path = write_briefing_pair(image, tmp_path, "Field_01")
    mb, db = QImage(str(mb_path)), QImage(str(db_path))

    assert mb_path.name == "Mb_Field_01.png"
    assert db_path.name == "Db_Field_01.png"
    assert mb_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert (mb.width(), mb.height()) == (db.width(), db.height()) == (image.width(), image.height())
    assert _image_bytes(mb) == source
    assert _image_bytes(image) == source
    mb_pixels = np.frombuffer(_image_bytes(mb), np.uint8).reshape(mb.height(), mb.width(), 4)
    db_pixels = np.frombuffer(_image_bytes(db), np.uint8).reshape(db.height(), db.width(), 4)
    expected_db = np.rint(mb_pixels[:, :, :3].astype(float) * .72).astype(np.uint8)
    assert np.array_equal(db_pixels[:, :, :3], expected_db)
    assert np.array_equal(db_pixels[:, :, 3], mb_pixels[:, :, 3])
    assert db_pixels[:, :, :3].mean() < mb_pixels[:, :, :3].mean()


def test_briefing_render_excludes_hosts_without_mutating_source(library):
    _, lib = library
    doc = _document()
    with_hosts = _document()
    with_hosts.host_stations = [dict(owner=1, veh=56, x=4, y=4, pos_y=-900,
                                     viewangle=90, energy=500000)]
    before_doc, before_hosts = doc.snapshot(), with_hosts.snapshot()

    without_host_image = render_briefing(doc, lib)
    with_host_image = render_briefing(with_hosts, lib)

    assert _image_bytes(with_host_image) == _image_bytes(without_host_image)
    assert doc.snapshot() == before_doc
    assert with_hosts.snapshot() == before_hosts


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
