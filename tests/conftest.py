import faulthandler
import gc
import os
import sys
import threading
from pathlib import Path

# Defaults so a bare `pytest tests/test_x.py` works: no visible windows, and the game data of this
# workspace (ProgettoUA/UA Rising/Data) when NME_GAME_DATA is not set. Set before Qt is imported.
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
_WORKSPACE_DATA = Path(__file__).resolve().parents[3] / 'UA Rising' / 'Data'
if 'NME_GAME_DATA' not in os.environ and _WORKSPACE_DATA.is_dir():
    os.environ['NME_GAME_DATA'] = str(_WORKSPACE_DATA)

import pytest  # noqa: E402
from PySide6.QtCore import QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from shiboken6 import isValid  # noqa: E402

# A test blocked by a native dialog, menu or clipboard never returns and would freeze the whole run.
# After this many seconds the run stops, naming the test (STUDIO_TEST_TIMEOUT=0 disables it).
TEST_TIMEOUT = float(os.environ.get('STUDIO_TEST_TIMEOUT', '120'))
KNOWN_FAILURES = Path(__file__).with_name('known_failures.txt')


@pytest.hookimpl(tryfirst=True)
def pytest_cmdline_main(config):
    # The whole suite run in one process takes ~40 minutes and slows down as windows pile up.
    # A run over a folder or many files is spread over worker processes, a whole file per worker
    # (module fixtures stay valid). Focused runs of 1-3 files stay in one process. `-n 0` forces serial.
    if os.environ.get('PYTEST_XDIST_WORKER') or not config.pluginmanager.hasplugin('xdist'):
        return
    if config.option.numprocesses is not None or config.option.collectonly:
        return
    targets = [Path(arg.split('::', 1)[0]) for arg in config.args] or [Path('tests')]
    if any(path.is_dir() for path in targets) or len(targets) > 3:
        config.option.numprocesses = min(12, max(2, (os.cpu_count() or 4) // 2))
        config.option.dist = 'loadfile'


def _known_failures():
    # One pytest node id (or a file / class prefix of it) per line; '#' starts a comment.
    if not KNOWN_FAILURES.is_file():
        return []
    entries = []
    for line in KNOWN_FAILURES.read_text(encoding='utf-8').splitlines():
        entry = line.split('#', 1)[0].strip().replace('\\', '/')
        if entry:
            entries.append(entry)
    return entries


def pytest_collection_modifyitems(config, items):
    # Known broken or hanging tests are skipped, so nobody spends time rediscovering them.
    entries = _known_failures()
    if not entries:
        return
    skip = pytest.mark.skip(reason='listed in tests/known_failures.txt')
    for item in items:
        node = item.nodeid.replace('\\', '/')
        if any(node == entry or node.startswith(entry + '::') or node.startswith(entry + '[')
               for entry in entries):
            item.add_marker(skip)


def _abort_hung_test(config, nodeid):
    # pytest captures stderr: release it first, or the message dies with the process.
    capture = config.pluginmanager.getplugin('capturemanager')
    if capture is not None:
        capture.suspend_global_capture(in_=True)
    sys.stderr.write(f'\n\nTIMEOUT: {nodeid} ran over {TEST_TIMEOUT:g} s; run stopped. '
                     'If it is not caused by your change, add it to tests/known_failures.txt.\n')
    faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
    sys.stderr.flush()
    os._exit(3)


@pytest.fixture(autouse=True)
def test_timeout(request):
    if TEST_TIMEOUT <= 0:
        yield
        return
    timer = threading.Timer(TEST_TIMEOUT, _abort_hung_test, args=(request.config, request.node.nodeid))
    timer.daemon = True
    timer.start()
    yield
    timer.cancel()


@pytest.fixture(autouse=True)
def isolated_studio_session():
    # Session persistence is exercised inside each test, not between tests.
    app = QApplication.instance()
    if app is not None:
        app.setProperty('studioSelectedSets', {})
    before = set(app.topLevelWidgets()) if app is not None else set()
    yield
    # Windows a test leaves behind pile up: every later processEvents() repaints the visible
    # ones and a theme switch restyles ALL of them (minutes and gigabytes after a few thousand
    # tests). Hide each one (close() could ask to save), then let Qt delete it. A workspace that
    # still has a background job (preview, loader) is deleted after the events of the test ran.
    app = QApplication.instance()
    if app is None:
        return
    leftovers = [widget for widget in app.topLevelWidgets()
                 if widget not in before and isValid(widget)]
    for widget in leftovers:
        widget.hide()
    app.processEvents()
    for widget in leftovers:
        if isValid(widget):
            widget.deleteLater()
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    # Finalize this test's Qt wrappers now. Left to the interpreter's exit, the collector destroys
    # half-deleted widgets late and in any order: a random heap-corruption crash after all tests passed.
    gc.collect()
