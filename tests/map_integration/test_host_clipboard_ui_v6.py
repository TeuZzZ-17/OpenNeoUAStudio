"""Run these Qt integration tests on Windows with PySide6 installed."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import copy
import pytest
pytest.importorskip('PySide6')
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from map_editor.core.ldf_model import LdfDocument, ensure_host_defaults
from map_editor.ui.main_window import MainWindow


@pytest.fixture
def editor():
    app = QApplication.instance() or QApplication([])
    win = MainWindow(LdfDocument(mw=9, mh=9))
    win._icons.stop()
    yield win
    win._cancel_operation(clear=False)
    win.dirty = False
    win.close()
    win._icon_pool.waitForDone(10000)
    win.view._pool.waitForDone(10000)


def test_copy_paste_host_preview_commit_collision_and_undo(editor):
    host = dict(owner=6, veh=56, energy=123456, pos_y=-900,
                x=2, y=2, hidden=True, ai={'preset': 'Balanced'})
    ensure_host_defaults(host)
    editor.doc.host_stations = [host]
    editor._refresh_squads()
    editor._refresh_hosts(0)
    editor.palette_tabs.setCurrentIndex(editor.host_tab_index)
    before = editor.doc.snapshot()

    editor._copy_elements()
    assert editor._clipboard[0] == 'host'
    assert editor._draft_host is not None
    assert editor._draft_host['_preview']
    assert editor._draft_host['energy'] == host['energy']
    assert editor.doc.snapshot() == before

    # The same-sector Host Station must be rejected.
    editor._confirm_placement((2, 2))
    assert len(editor.doc.host_stations) == 1
    editor._confirm_placement((5, 4))
    assert len(editor.doc.host_stations) == 2
    assert editor.doc.host_stations[1]['ai'] == host['ai']
    assert (editor.doc.host_stations[1]['x'], editor.doc.host_stations[1]['y']) == (5, 4)
    editor.undo()
    assert editor.doc.snapshot() == before


def test_cancel_host_paste_preserves_level(editor):
    host = dict(owner=1, veh=56, energy=500000, pos_y=-700, x=2, y=2)
    ensure_host_defaults(host)
    editor.doc.host_stations = [host]
    editor._refresh_squads()
    editor._refresh_hosts(0)
    editor.palette_tabs.setCurrentIndex(editor.host_tab_index)
    before = editor.doc.snapshot()
    editor._copy_elements()
    editor._cancel_operation(clear=False)
    assert editor._draft_host is None
    assert editor.doc.snapshot() == before
