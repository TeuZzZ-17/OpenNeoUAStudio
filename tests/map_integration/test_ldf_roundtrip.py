import glob
import os
import sys
import tempfile

import pytest

from map_editor import bootstrap
from map_editor.core import ldf_model as lm

LEVELS = sorted(glob.glob(str(bootstrap.game_data_dir() / "Levels" / "**" / "*.LDF"),
                          recursive=True))


def test_constants_and_rules():
    assert (lm.HGT_MIN, lm.HGT_MAX, lm.DEFAULT_HGT) == (0x61, 0x9D, 0x7F)
    assert lm.grid_to_world(2, 3) == (3100, -4300)
    assert lm.world_to_grid(3100, -4300) == (2, 3)
    doc = lm.LdfDocument(mw=5, mh=5)
    doc.grids['hgt'][1][1] = 0x90
    doc.normalize_border_heights()
    assert doc.grids['hgt'][0][0] == 0x90 and doc.grids['hgt'][0][2] == 0x7F
    assert doc.grids['type'][0][0] == 'f8' and doc.grids['type'][4][4] == 'fa'


def test_resize_validation():
    doc = lm.LdfDocument(mw=10, mh=10)
    doc.squads.append({'owner': 1, 'veh': 1, 'num': 1, 'hidden': False,
                       'useable': False, 'custom_name': None, 'x': 8, 'y': 8})
    with pytest.raises(ValueError):
        doc.resize(5, 5)
    counts = doc.resize(5, 5, remove_out_of_bounds=True)
    assert counts['squads'] == 1 and not doc.squads and doc.mw == 5


@pytest.mark.skipif(not LEVELS, reason="nessun LDF")
def test_idempotent_all_levels():
    for path in LEVELS:
        doc = lm.load_ldf(path)
        first = lm.dumps_ldf(doc)
        second = lm.dumps_ldf(lm.loads_ldf(first, doc.encoding))
        assert first == second, path


@pytest.mark.skipif(not LEVELS, reason="nessun LDF")
def test_byte_identical_with_sektor2():
    sys.path.insert(0, os.path.dirname(__file__))
    try:
        import sektor2_oracle
        sektor2_oracle.load_oracle()
    except Exception as exc:
        pytest.skip(f"oracolo Sektor2 non disponibile: {exc}")
    with tempfile.TemporaryDirectory() as tmp:
        for path in LEVELS:
            dst = os.path.join(tmp, "oracle.LDF")
            sektor2_oracle.sektor2_roundtrip(path, dst)
            doc = lm.load_ldf(path)
            if any(os.path.splitext(str(doc.lvl_info.get(key, "")))[1].casefold()
                   not in ("", ".iff") for key in ("mbmap", "dbmap")):
                # Sektor2 forces IFF even when the game level names a modern image.
                continue
            # The old editor snaps squads to cell centres. Compare its remaining
            # output contract separately from our exact-coordinate round trip.
            for squad in doc.squads:
                squad.pop('pos_x', None)
                squad.pop('pos_z', None)
            for host in doc.host_stations:
                host.pop('pos_x', None)
                host.pop('pos_z', None)
            # Sektor2 drops empty allowlists. Compare its other formatting here;
            # the new explicit-empty contract is covered by dedicated tests.
            doc.tech_explicit.clear()
            mine = lm.dumps_ldf(doc).encode(doc.encoding)
            with open(dst, "rb") as fh:
                assert mine == fh.read(), path


def test_modern_briefing_extension_is_preserved():
    assert lm.briefing_for_export("MB_02.PNG") == "MB_02.PNG"
    assert lm.briefing_for_export("DB_02.IFF") == "DB_02.IFF"
    assert lm.briefing_for_export("MB_02") == "MB_02.IFF"
    modern = bootstrap.game_data_dir() / "Levels" / "Single" / "L0101.LDF"
    if modern.is_file():
        output = lm.dumps_ldf(lm.load_ldf(modern))
        assert "MB_02.PNG" in output and "DB_02.PNG" in output


def test_atomic_save(tmp_path):
    doc = lm.LdfDocument(mw=6, mh=6)
    target = tmp_path / "T.LDF"
    lm.save_ldf(doc, target)
    again = lm.load_ldf(target)
    assert (again.mw, again.mh) == (6, 6)
    assert not [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
