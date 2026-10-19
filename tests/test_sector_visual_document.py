from copy import deepcopy
from unittest.mock import patch

import pytest

from map_editor.core.set_sdf_parser import (
    SetSdf, SdfBuilding, SdfBuildingModel, SdfSector,
)
from sector_editor.document import SetDocument


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


@pytest.mark.parametrize(
    ("sources", "target"),
    [([], 0), ([9], 0), ([-1], 0), ([1.5], 0), ([0, 2], 2), ([0], 9)],
)
def test_relocate_cells_rejects_invalid_or_off_grid_footprints_without_changes(
        sources, target):
    document = make_document({5: sector()})
    before = deepcopy(document.sdf)

    with pytest.raises(ValueError):
        document.relocate_cells(5, sources, target, move=True)

    assert document.sdf == before
    assert not document._undo


def test_relocate_single_sector_only_accepts_center_cell_noop():
    document = make_document({
        9: sector(9, ground=2, cells=(5,), single=True, comment="Single")
    })
    before = deepcopy(document.sdf)

    with pytest.raises(ValueError):
        document.relocate_cells(9, {4}, 3)
    with pytest.raises(ValueError):
        document.relocate_cells(9, {0}, 4)
    assert document.relocate_cells(9, {4}, 4) == {4}
    assert document.sdf == before
    assert not document._undo


def test_relocate_cells_same_position_does_not_add_undo():
    document = make_document({5: sector()})
    before = deepcopy(document.sdf)

    assert document.relocate_cells(5, {0, 1}, 0, move=True) == {0, 1}

    assert document.sdf == before
    assert not document._undo


def test_set_ground_changes_only_matching_sectors_and_undoes_atomically():
    document = make_document({
        5: sector(5, ground=1, comment="Family A"),
        6: sector(6, ground=1, comment="Family A sibling"),
        7: sector(7, ground=2, comment="Family B"),
        9: sector(9, ground=1, cells=(4,), single=True, comment="Single A"),
    })
    before = deepcopy(document.sdf)

    document.set_ground({5, 6, 7, 9}, 4, replace_ground=1)

    after = deepcopy(document.sdf)
    assert {n: s.ground for n, s in after.sectors.items()} == {
        5: 4, 6: 4, 7: 2, 9: 4,
    }
    for number, original in before.sectors.items():
        expected = SdfSector(original.number, original.kind,
                             4 if original.ground == 1 else original.ground,
                             original.symbol, original.subsects, original.comment)
        assert after.sectors[number] == expected
    assert after.buildings == before.buildings
    assert after.models == before.models
    assert len(document._undo) == 1
    document.undo()
    assert document.sdf == before
    document.redo()
    assert document.sdf == after


def test_set_ground_without_filter_changes_all_selected_families():
    document = make_document({
        5: sector(5, ground=1), 7: sector(7, ground=2),
    })

    document.set_ground((5, 7), 3)

    assert {n: s.ground for n, s in document.sdf.sectors.items()} == {5: 3, 7: 3}


@pytest.mark.parametrize(
    ("numbers", "ground", "replace_ground"),
    [([5, 99], 3, None), ([5, True], 3, None), ([5], 6, None),
     ([5], 3, -1)],
)
def test_set_ground_validates_every_input_before_changing(numbers, ground,
                                                          replace_ground):
    document = make_document({5: sector()})
    before = deepcopy(document.sdf)

    with pytest.raises(ValueError):
        document.set_ground(numbers, ground, replace_ground=replace_ground)

    assert document.sdf == before
    assert not document._undo


def test_set_ground_filtered_noop_does_not_add_undo():
    document = make_document({5: sector(ground=1)})
    before = deepcopy(document.sdf)

    document.set_ground({5}, 1, replace_ground=2)

    assert document.sdf == before
    assert not document._undo
