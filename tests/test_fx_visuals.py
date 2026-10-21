"""Base data list of the Visuals & FX tab: reading and writing model-state keys."""

from pathlib import Path

import pytest

from entity_designer import fx_visuals as visuals
from entity_designer.script_model import find_script_blocks

SCRIPT = (
    "new_vehicle 5\n"
    "    name = Test\n"
    "    vp_normal = 10    ; main model\n"
    "    base_fire = Data/Models/Base/A.BASE\n"
    "    energy = 100\n"
    "begin_fx\n"
    "    trigger = death\n"
    "    vp_normal = 999\n"
    "end\n"
    "end\n"
    "\n"
    "new_weapon 6\n"
    "    vp_normal = 20\n"
    "end\n")


def _block(text, kind="new_vehicle"):
    return next(b for b in find_script_blocks(text) if b.kind == kind)


def test_reads_top_level_keys_only_and_the_last_assignment_wins():
    text = SCRIPT.replace("energy = 100", "vp_normal = 11\n    energy = 100")
    values = visuals.read_values(text, _block(text))
    assert values == {"vp_normal": "11", "base_fire": "Data/Models/Base/A.BASE"}


def test_replaces_a_value_and_keeps_its_comment():
    text = visuals.patch_values(SCRIPT, _block(SCRIPT), {"vp_normal": "12"})
    assert "    vp_normal = 12    ; main model" in text
    assert "    vp_normal = 999" in text  # inside begin_fx: untouched


def test_new_key_goes_after_the_last_visual_key_and_empty_removes():
    text = visuals.patch_values(SCRIPT, _block(SCRIPT), {"3ds_dead": "Data/Models/3ds/d.3ds"})
    lines = text.splitlines()
    assert lines[lines.index("    base_fire = Data/Models/Base/A.BASE") + 1] == \
        "    3ds_dead = Data/Models/3ds/d.3ds"
    removed = visuals.patch_values(text, _block(text), {"base_fire": ""})
    assert "base_fire" not in removed and "3ds_dead" in removed


def test_key_without_any_visual_row_goes_before_the_block_end():
    text = visuals.patch_values(SCRIPT, _block(SCRIPT, "new_weapon"), {"vp_wait": "7"})
    assert text.splitlines()[-3:] == ["    vp_normal = 20", "    vp_wait = 7", "end"] or \
        "    vp_wait = 7\nend" in text.replace("\r\n", "\n")


def test_crlf_and_final_newline_are_kept():
    crlf = SCRIPT.replace("\n", "\r\n")
    text = visuals.patch_values(crlf, _block(crlf), {"vp_fire": "3"})
    assert text.count("\r\n") == text.count("\n") and text.endswith("\r\n")


def test_values_are_checked():
    assert visuals.normalize_value("vp_normal", " 12 ") == "12"
    assert visuals.normalize_value("base_dead", "Data\\Models\\A.BASE") == "Data/Models/A.BASE"
    assert visuals.normalize_value("3ds_dead", "  ") == ""
    for key, bad in (("vp_normal", "abc"), ("vp_normal", "-1"), ("base_dead", "a;b")):
        with pytest.raises(ValueError):
            visuals.normalize_value(key, bad)


def test_save_writes_a_verified_file_and_refuses_a_changed_one(tmp_path: Path):
    path = tmp_path / "Vehicles.cfg"
    path.write_bytes(SCRIPT.encode("utf-8"))
    block = _block(SCRIPT)
    origins = [(path, block.start_line, block.end_line)]
    snapshot = {path: path.read_bytes()}
    visuals.save_values(origins, {"vp_normal": "42"}, snapshot)
    assert "vp_normal = 42" in path.read_text(encoding="utf-8")
    assert visuals.read_effective_values(origins)["vp_normal"] == "42"
    with pytest.raises(ValueError, match="changed outside"):
        visuals.save_values(origins, {"vp_normal": "43"}, snapshot)
    assert "vp_normal = 42" in path.read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        visuals.save_values(origins, {"vp_normal": "x"}, {path: path.read_bytes()})
