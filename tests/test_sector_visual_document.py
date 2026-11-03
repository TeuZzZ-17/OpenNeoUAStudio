from copy import deepcopy
from dataclasses import replace
from unittest.mock import patch

import pytest

from map_editor.core.set_sdf_parser import (
    SetSdf, SdfBuilding, SdfBuildingModel, SdfSector,
)
from sector_editor.document import AUTO, SetDocument


def make_document(sectors):
    models = [
        SdfBuildingModel(i, f"Model{i}.BASE", f"skeleton/Model{i}.sklt",
                         (0, 1, 50, 0), (), f"Model {i}")
        for i in range(4)
    ]
    buildings = [
        SdfBuilding(i, i % 4, (i + 1) % 4, (i + 2) % 4, (i + 3) % 4,
                    i % 2, f"Building {i}")
        for i in range(10)
    ]
    with patch("sector_editor.document.bootstrap.find_ci", return_value=None):
        return SetDocument(".", 1, SetSdf(models=models, buildings=buildings,
                                           sectors=sectors), b"")


def sector(number=5, *, ground=1, cells=None, single=False, comment="Sector"):
    return SdfSector(number, int(single), ground, 73,
                     tuple(cells if cells is not None else range(1, 10)), comment)


def test_relocate_cells_duplicates_shape_and_preserves_building_rows():
    document = make_document({5: sector()})
    original = deepcopy(document.sdf)

    destinations = document.relocate_cells(5, {0, 1, 3}, 3)

    expected = list(original.sectors[5].subsects)
    for source, target in ((0, 3), (1, 4), (3, 6)):
        expected[target] = original.sectors[5].subsects[source]
    assert destinations == {3, 4, 6}
    assert document.sdf.sectors[5].subsects == tuple(expected)
    assert document.sdf.buildings == original.buildings
    assert document.sdf.models == original.models


def test_relocate_cells_move_uses_snapshot_for_overlap_and_is_one_undo():
    document = make_document({5: sector()})
    before = deepcopy(document.sdf)
    source = before.sectors[5].subsects

    destinations = document.relocate_cells(5, {0, 1, 3}, 1, move=True)

    expected = list(source)
    for cell in {0, 1, 3} - destinations:
        expected[cell] = 0
    for old, new in ((0, 1), (1, 2), (3, 4)):
        expected[new] = source[old]
    after = deepcopy(document.sdf)
    assert destinations == {1, 2, 4}
    assert after.sectors[5].subsects == tuple(expected)
    assert len(document._undo) == 1

    document.undo()
    assert document.sdf == before
    document.redo()
    assert document.sdf == after


def test_sector_fields_change_selected_sectors_and_undo_atomically():
    document = make_document({
        5: sector(5, ground=1, comment="Family A"),
        6: sector(6, ground=1, comment="Family A sibling"),
        7: sector(7, ground=2, comment="Family B"),
    })
    before = deepcopy(document.sdf)

    document.set_sector_fields({5, 7}, ground=4)

    after = deepcopy(document.sdf)
    assert {n: s.ground for n, s in after.sectors.items()} == {5: 4, 6: 1, 7: 4}
    assert {n: s.symbol for n, s in after.sectors.items()} == {n: s.symbol for n, s in before.sectors.items()}
    assert after.buildings == before.buildings
    assert len(document._undo) == 1
    document.undo()
    assert document.sdf == before
    document.redo()
    assert document.sdf == after


def test_automatic_fields_copy_the_most_similar_sector():
    document = make_document({
        5: sector(5, ground=3, comment="Same cells"),
        7: sector(7, ground=2, cells=(1,) * 9),
        9: sector(9, ground=0),
    })
    document.sdf.sectors[5] = replace(document.sdf.sectors[5], symbol=44)
    document.set_sector_fields([9], ground=AUTO, symbol=AUTO)
    assert (document.sdf.sectors[9].ground, document.sdf.sectors[9].symbol) == (3, 44)


def test_sector_fields_noop_does_not_add_undo():
    document = make_document({5: sector(ground=1)})
    before = deepcopy(document.sdf)

    document.set_sector_fields({5}, ground=1)

    assert document.sdf == before
    assert not document._undo




def test_textures_with_vanilla_names_become_loose_ilbm_in_the_set_palette(tmp_path):
    from PySide6.QtGui import QColor, QImage
    from studio.formats.images import parse_ilbm_file
    from sector_editor import textures

    class Resource:
        class_id, resource_name = "ilbm.class", "BODEN1.ILBM"

    palette = [(0, 0, 0)] * 256
    palette[7] = (250, 0, 0)
    original = textures.IlbmImage(source_name="BODEN1.ILBM", kind="ILBM", width=4, height=4,
                                  palette=palette, pixels=bytes(16))
    textures.write_image_as_ilbm(original, tmp_path / "Palette" / "STANDARD.PAL")
    image = QImage(8, 8, QImage.Format.Format_RGB32)
    image.fill(QColor(255, 0, 0))
    image.save(str(tmp_path / "boden1.png"))
    image.save(str(tmp_path / "unknown.png"))
    with patch.object(textures, "decode_texture", return_value=original):
        written, skipped = textures.import_textures(tmp_path, SimpleArchive([Resource]), [tmp_path])
    assert [p.relative_to(tmp_path).as_posix() for p in written] == ["Loose/ILBM/BODEN1.ILBM"]
    assert len(skipped) == 1 and "unknown.png" in skipped[0]
    result = parse_ilbm_file(written[0])
    assert (result.width, result.height) == (4, 4) and set(result.pixels) == {7}


class SimpleArchive:
    def __init__(self, resources):
        self.resources = [resource() for resource in resources]
