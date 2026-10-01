from pathlib import Path

import pytest

from map_editor import bootstrap
from map_editor.core.set_sdf_parser import parse_set_sdf
from map_editor.core.sector_resolver import SectorResolver

SET1 = bootstrap.set_dir(1)


@pytest.fixture(scope="module")
def assets():
    from map_editor.core.asset_bridge import SetAssets
    return SetAssets(1).load()


def test_setbas_resources(assets):
    assert len(assets.archive.resources) == 1119


def test_sdf_sections():
    sdf = parse_set_sdf(bootstrap.find_ci(SET1 / "Scripts", "set.sdf"))
    assert (len(sdf.models), len(sdf.buildings), len(sdf.sectors)) == (256, 172, 177)
    assert not sdf.warnings
    assert sdf.models[3].base == "ST_CITY1.base"
    assert sdf.sectors[5].subsects == (8, 29, 4, 0, 29, 0, 40, 29, 6)


def test_resolver_typ_and_bases(assets):
    resolver = SectorResolver(assets.sdf)
    res = resolver.resolve_typ(5)
    assert len(res.subs) == 9 and not res.warnings
    assert res.subs[2].base_name == "ST_CITY1.base"
    assert res.subs[3].building_id == 0
    gate = resolver.resolve_typ(4)
    assert len(gate.subs) == 1 and gate.subs[0].building_id == 15


def test_all_used_bases_have_objects(assets):
    resolver = SectorResolver(assets.sdf)
    missing = set()
    for typ in assets.sdf.sectors:
        for sub in resolver.resolve_typ(typ).subs:
            if sub.model and not sub.base_name.lower().startswith(
                    ("st_empty", "gr_e", "gs_e")) and not assets.has_base(sub.base_name):
                missing.add(sub.base_name)
    print(sorted(missing))
    assert len(missing) < 40
