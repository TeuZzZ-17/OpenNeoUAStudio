"""Asking the NVIDIA library for the graphics card on Linux.

Nothing here touches the real environment of the test run: every case puts its
own copy in place first. That copy is not empty, because pytest keeps its own
entry in it, so the checks name the two variables instead.
"""

import os
import sys

import pytest

from studio.rendering import gpu_preference as pref


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """A Linux with an empty environment and no request made yet."""

    monkeypatch.setattr(sys, "platform", "linux", raising=False)
    monkeypatch.setattr(os, "environ", {}, raising=False)
    monkeypatch.setattr(pref, "_asked_nvidia", False, raising=False)
    yield


def test_asks_when_the_driver_is_there(monkeypatch):
    monkeypatch.setattr(pref, "nvidia_driver_loaded", lambda: True)
    assert pref.ask_nvidia_offload() is True
    assert os.environ["__NV_PRIME_RENDER_OFFLOAD"] == "1"
    assert os.environ["__GLX_VENDOR_LIBRARY_NAME"] == "nvidia"
    assert pref.asked_nvidia_offload() is True


def test_asks_nothing_without_the_driver(monkeypatch):
    monkeypatch.setattr(pref, "nvidia_driver_loaded", lambda: False)
    assert pref.ask_nvidia_offload() is False
    assert not any(name in os.environ for name in pref.NVIDIA_OFFLOAD)


def test_keeps_what_the_person_set(monkeypatch):
    monkeypatch.setattr(pref, "nvidia_driver_loaded", lambda: True)
    os.environ["__NV_PRIME_RENDER_OFFLOAD"] = "0"
    os.environ["__GLX_VENDOR_LIBRARY_NAME"] = "mesa"
    assert pref.ask_nvidia_offload() is False
    assert os.environ["__NV_PRIME_RENDER_OFFLOAD"] == "0"
    assert os.environ["__GLX_VENDOR_LIBRARY_NAME"] == "mesa"


def test_the_request_can_be_taken_back(monkeypatch):
    monkeypatch.setattr(pref, "nvidia_driver_loaded", lambda: True)
    pref.ask_nvidia_offload()
    assert pref.stop_asking_nvidia_offload() is True
    assert not any(name in os.environ for name in pref.NVIDIA_OFFLOAD)
    assert pref.asked_nvidia_offload() is False
    # Taking back a request that was never made changes nothing.
    assert pref.stop_asking_nvidia_offload() is False


def test_nothing_is_asked_on_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32", raising=False)
    monkeypatch.setattr(pref, "nvidia_driver_loaded", lambda: True)
    assert pref.ask_nvidia_offload() is False
    assert not any(name in os.environ for name in pref.NVIDIA_OFFLOAD)


def test_windows_only_path_is_not_used_on_linux(monkeypatch):
    monkeypatch.setattr(pref, "write_preference",
                        lambda path, value=pref.HIGH_PERFORMANCE: pytest.fail("must not write"))
    assert pref.prefer_graphics_card(on_integrated=True) == pref.UNSUPPORTED
