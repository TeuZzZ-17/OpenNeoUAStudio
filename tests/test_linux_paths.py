"""Folders spelled differently from the code ('sounds' vs 'Sounds') must still resolve: Linux is case-sensitive."""
import pytest


@pytest.fixture
def lower_data(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "models" / "base").mkdir(parents=True)
    (data / "sets").mkdir()
    monkeypatch.setenv("ONUA_DATA", str(data))
    return data


def test_mcp_paths_follow_existing_spelling(lower_data):
    from studio_mcp import core

    assert core.under_data("Models/Base/New.BASE") == (lower_data / "models" / "base" / "New.BASE").resolve() \
        or core.under_data("Models/Base/New.BASE") == lower_data.resolve() / "models" / "base" / "New.BASE"
    backslashed = chr(92).join(("Models", "Missing", "x.base"))
    assert core.under_data(backslashed).parts[-3:] == ("models", "Missing", "x.base")
    assert core.under_data("Sets") == lower_data.resolve() / "sets"
    target = core.ensure_writable("Models/Base/New.BASE")
    assert target.parent.name == "base"
    assert lower_data.resolve() / "models" in core.writable_roots()
    with pytest.raises(core.McpToolError):
        core.ensure_writable("scripts/x.cfg")


def test_location_sound_and_sky_resolve_without_case(tmp_path):
    from map_editor.core.game_installation import GameInstallation
    from map_editor.core.resource_catalog import ResourceCatalog

    data = tmp_path / "Data"
    (data / "sounds" / "locations").mkdir(parents=True)
    (data / "objects").mkdir()
    (data / "objects" / "custom.base").write_bytes(b"x")
    catalog = ResourceCatalog(GameInstallation(data), 1)
    assert catalog.include_sky("Data/Objects/custom.base") == "data/objects/custom.base"
    assert "data/objects/custom.base" in catalog.skies
