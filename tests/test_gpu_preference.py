"""The rule that asks Windows for the graphics card, without touching the registry."""

import sys

import pytest

from studio.rendering import gpu_preference as pref
from studio.rendering.gpu import integrated_graphics


@pytest.mark.parametrize("name, integrated", [
    ("NVIDIA GeForce RTX 5070 Ti Laptop GPU/PCIe/SSE2", False),
    ("AMD Radeon RX 7900 XTX", False),
    ("Intel(R) Arc(TM) A770 Graphics", False),
    ("Intel(R) Graphics", True),
    ("Intel(R) UHD Graphics 770", True),
    ("Intel(R) Iris(R) Xe Graphics", True),
    ("AMD Radeon(TM) Vega 8 Graphics", True),
    ("llvmpipe (LLVM 15.0.6, 256 bits)", True),
    ("Microsoft Basic Render Driver", True),
    ("", False),
])
def test_which_names_are_built_in_chips(name, integrated):
    assert integrated_graphics(name) is integrated


@pytest.fixture
def fake_windows(monkeypatch):
    """A Windows where Studio is the built program and the registry is a dict."""

    stored = {}
    monkeypatch.setattr(sys, "platform", "win32", raising=False)
    monkeypatch.setattr(pref, "running_as_program", lambda: True)
    monkeypatch.setattr(pref, "program_path", lambda: r"C:\Studio\OpenNeoUAStudio.exe")
    monkeypatch.setattr(pref, "read_preference", lambda path: stored.get(path))
    monkeypatch.setattr(pref, "write_preference",
                        lambda path, value=pref.HIGH_PERFORMANCE: stored.setdefault(path, value) and True)
    return stored


def test_asks_for_the_card_when_on_a_built_in_chip(fake_windows):
    assert pref.prefer_graphics_card(on_integrated=True) == pref.APPLIED
    assert fake_windows[r"C:\Studio\OpenNeoUAStudio.exe"] == pref.HIGH_PERFORMANCE


def test_writes_nothing_when_already_on_a_graphics_card(fake_windows):
    assert pref.prefer_graphics_card(on_integrated=False) == pref.ALREADY_SET
    assert fake_windows == {}


def test_never_overwrites_a_choice_the_user_made(fake_windows):
    fake_windows[r"C:\Studio\OpenNeoUAStudio.exe"] = "GpuPreference=1;"
    assert pref.prefer_graphics_card(on_integrated=True) == pref.ALREADY_SET
    assert fake_windows[r"C:\Studio\OpenNeoUAStudio.exe"] == "GpuPreference=1;"


def test_does_not_decide_for_python_when_run_from_sources(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32", raising=False)
    monkeypatch.setattr(pref, "running_as_program", lambda: False)
    monkeypatch.setattr(pref, "write_preference",
                        lambda path, value=pref.HIGH_PERFORMANCE: pytest.fail("must not write"))
    assert pref.prefer_graphics_card(on_integrated=True) == pref.NOT_FROZEN


def test_other_systems_are_left_alone(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux", raising=False)
    monkeypatch.setattr(pref, "write_preference",
                        lambda path, value=pref.HIGH_PERFORMANCE: pytest.fail("must not write"))
    assert pref.prefer_graphics_card(on_integrated=True) == pref.UNSUPPORTED


def test_every_outcome_explains_itself():
    for result in (pref.APPLIED, pref.NOT_FROZEN, pref.UNSUPPORTED):
        assert pref.message(result, "Intel(R) Graphics")
    assert pref.message(pref.ALREADY_SET) == ""
