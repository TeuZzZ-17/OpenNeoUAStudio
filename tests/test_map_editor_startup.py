from __future__ import annotations

import importlib
import os
import sys
from types import ModuleType, SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog

import main as studio_entry
from map_editor import editor as map_editor_entry
from map_editor.core.ldf_model import LdfDocument


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _fake_editor_ui(monkeypatch, window_type, new_map_type, install_type=None):
    ui_package = ModuleType("map_editor.ui")
    ui_package.__path__ = []
    dialogs = ModuleType("map_editor.ui.dialogs")
    dialogs.NewMapDialog = new_map_type
    dialogs.GameInstallationDialog = install_type
    main_window = ModuleType("map_editor.ui.main_window")
    main_window.MainWindow = window_type
    ui_package.dialogs = dialogs
    ui_package.main_window = main_window
    monkeypatch.setitem(sys.modules, "map_editor.ui", ui_package)
    monkeypatch.setitem(sys.modules, "map_editor.ui.dialogs", dialogs)
    monkeypatch.setitem(sys.modules, "map_editor.ui.main_window", main_window)


def test_map_editor_loads_optional_ldf_and_reuses_existing_application(
        app, monkeypatch, tmp_path):
    events = []
    install = object()
    doc = object()
    level_path = str(tmp_path / "L0101.LDF")

    class Application:
        @staticmethod
        def instance():
            return app

        def __init__(self, _args):
            pytest.fail("The existing Studio QApplication must be reused")

    class UnexpectedDialog:
        def __init__(self, *_args):
            pytest.fail("An existing LDF must bypass the new-map dialogs")

    class Window:
        def __init__(self, loaded_doc, path):
            events.append("window")
            self.doc, self.path = loaded_doc, path

        def setWindowTitle(self, title):
            self.title = title

        def setAttribute(self, attribute, enabled):
            self.attribute = (attribute, enabled)

        def show(self):
            events.append("show")

    monkeypatch.setattr(map_editor_entry, "QApplication", Application)
    monkeypatch.setattr(
        map_editor_entry, "load_remembered",
        lambda: events.append("remembered") or install)
    monkeypatch.setattr(
        map_editor_entry.bootstrap, "set_installation",
        lambda value: events.append(("installation", value)))
    monkeypatch.setattr(
        map_editor_entry.bootstrap, "ensure_studio_on_path",
        lambda: events.append("studio-path"))
    ldf_model = importlib.import_module("map_editor.core.ldf_model")
    monkeypatch.setattr(ldf_model, "load_ldf", lambda path: doc)
    _fake_editor_ui(monkeypatch, Window, UnexpectedDialog, UnexpectedDialog)

    window = map_editor_entry.create_window([level_path])

    assert window.doc is doc
    assert window.path == level_path
    assert window.title == "OpenNeoUA Studio - Map Editor"
    assert window.attribute == (Qt.WidgetAttribute.WA_DeleteOnClose, True)
    assert map_editor_entry._active_window is window
    assert events == [
        "remembered", ("installation", install), "studio-path", "window", "show"]


def test_map_editor_opens_default_set1_15x15_map_without_new_map_prompt(
        app, monkeypatch):
    events = []
    suggested, install = object(), object()

    class Application:
        @staticmethod
        def instance():
            return app

        def __init__(self, _args):
            pytest.fail("The existing Studio QApplication must be reused")

    class InstallDialog:
        def __init__(self, current, parent):
            events.append(("installation-dialog", current, parent))

        def exec(self):
            events.append("installation-dialog-exec")
            return QDialog.DialogCode.Accepted

        def installation(self):
            return install

    class NewMapDialog:
        def __init__(self, parent):
            pytest.fail("Startup must not show the new-map dialog")

        def exec(self):
            pytest.fail("Startup must not show the new-map dialog")

        def values(self):
            pytest.fail("Startup must not request custom map values")

    class Window:
        def __init__(self, doc, path):
            events.append("window")
            self.doc, self.path = doc, path

        def setWindowTitle(self, title):
            self.title = title

        def setAttribute(self, _attribute, _enabled):
            pass

        def show(self):
            events.append("show")

    monkeypatch.setattr(map_editor_entry, "QApplication", Application)
    monkeypatch.setattr(map_editor_entry, "load_remembered", lambda: None)
    monkeypatch.setattr(
        map_editor_entry.GameInstallation, "suggest",
        lambda path: events.append(("suggest", path)) or suggested)
    monkeypatch.setenv("NME_GAME_DATA", "C:/UA/Data")
    monkeypatch.setattr(
        map_editor_entry.bootstrap, "set_installation",
        lambda value: events.append(("installation", value)))
    monkeypatch.setattr(
        map_editor_entry.bootstrap, "ensure_studio_on_path",
        lambda: events.append("studio-path"))
    _fake_editor_ui(monkeypatch, Window, NewMapDialog, InstallDialog)

    window = map_editor_entry.create_window([])

    assert isinstance(window.doc, LdfDocument)
    assert (window.doc.mw, window.doc.mh, window.doc.set_number) == (15, 15, 1)
    assert window.path is None
    assert events == [
        ("suggest", "C:/UA/Data"),
        ("installation-dialog", suggested, None),
        "installation-dialog-exec",
        ("installation", install),
        "studio-path",
        "window",
        "show",
    ]


def test_selector_routes_optional_ldf_to_integrated_map_editor(
        monkeypatch, tmp_path):
    calls = []
    level_path = str(tmp_path / "L0101.LDF")

    class Application:
        def __init__(self, _args):
            pass

        def setApplicationName(self, _name):
            pass

        def setApplicationDisplayName(self, _name):
            pass

    class Selector:
        DialogCode = SimpleNamespace(Accepted=1)

        def exec(self):
            return 1

        def selected_tool(self):
            return "map_editor"

    monkeypatch.setattr("PySide6.QtWidgets.QApplication", Application)
    monkeypatch.setattr("startup_selector.StartupToolSelector", Selector)
    monkeypatch.setattr(sys, "argv", ["main.py", level_path])
    monkeypatch.setattr(
        map_editor_entry, "main",
        lambda argv: calls.append(argv) or 37)

    assert studio_entry.main() == 37
    assert calls == [[level_path]]


def test_map_editor_flag_routes_optional_ldf_without_selector(
        monkeypatch, tmp_path):
    calls = []
    level_path = str(tmp_path / "L0101.LDF")
    monkeypatch.setattr(
        map_editor_entry, "main",
        lambda argv: calls.append(argv) or 19)

    assert studio_entry._run_map_editor(["--map-editor", level_path]) == 19
    assert calls == [[level_path]]


def test_editor_main_reuses_application_and_runs_its_event_loop(
        monkeypatch, tmp_path):
    calls = []
    level_path = str(tmp_path / "L0101.LDF")

    class Application:
        def exec(self):
            calls.append("event-loop")
            return 23

    app_instance = Application()

    class ApplicationFactory:
        @staticmethod
        def instance():
            return app_instance

        def __init__(self, _args):
            pytest.fail("Map Editor startup must reuse the Studio QApplication")

    monkeypatch.setattr(map_editor_entry, "QApplication", ApplicationFactory)
    monkeypatch.setattr(
        map_editor_entry, "create_window",
        lambda argv: calls.append(("window", argv)) or object())

    assert map_editor_entry.main([level_path]) == 23
    assert calls == [("window", [level_path]), "event-loop"]


def test_assembly_menu_opens_and_retains_map_editor_window(monkeypatch):
    from assembly_window import AssemblyWindow

    calls = []
    owner = SimpleNamespace(_map_editor_windows=[])

    class Signal:
        def connect(self, slot):
            self.slot = slot

    class Window:
        def __init__(self):
            self.destroyed = Signal()

        def setAttribute(self, _attribute, _enabled):
            calls.append("delete-on-close")

        def show(self):
            calls.append("show")

        def raise_(self):
            calls.append("raise")

        def activateWindow(self):
            calls.append("activate")

    map_window = Window()
    monkeypatch.setattr(
        map_editor_entry, "create_window",
        lambda parent=None: calls.append(("create", parent)) or map_window)

    AssemblyWindow._open_map_editor(owner)

    assert owner._map_editor_windows == [map_window]
    assert calls == [
        ("create", owner), "delete-on-close", "show", "raise", "activate"]
    map_window.destroyed.slot()
    assert owner._map_editor_windows == []
