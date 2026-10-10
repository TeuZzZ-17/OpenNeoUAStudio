import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

from map_editor.ui.colored_tabs import PaletteTabs


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


def test_nine_tabs_fit_two_equal_rows_and_emit_selection(app):
    tabs = PaletteTabs()
    labels = [
        'Sectors', 'Buildings', 'Factions', 'Terrain', 'Squad',
        'Script', 'Level Info', 'Hosts', 'Tech',
    ]
    pages = [QWidget() for _ in labels]
    for page, label in zip(pages, labels):
        tabs.addTab(page, label)

    tabs.resize(400, 240)
    tabs.show()
    app.processEvents()

    assert tabs.width() == 400
    assert tabs.count() == len(labels)
    assert [tabs.tabText(index) for index in range(tabs.count())] == labels
    assert [tabs.widget(index) for index in range(tabs.count())] == pages

    buttons = [tabs.findChild(QPushButton, f'palette_tab_{index}')
               for index in range(len(labels))]
    assert all(button is not None and button.isVisible() for button in buttons)
    assert len({button.geometry().top() for button in buttons[:5]}) == 1
    assert len({button.geometry().top() for button in buttons[5:]}) == 1
    assert buttons[5].geometry().top() > buttons[0].geometry().top()
    assert max(button.width() for button in buttons) - min(
        button.width() for button in buttons
    ) <= 1

    changes = []
    tabs.currentChanged.connect(changes.append)
    tabs.setCurrentIndex(8)
    for index, button in enumerate(buttons):
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        app.processEvents()
        assert tabs.currentIndex() == index
        assert tabs.currentWidget() is pages[index]
        assert button.isChecked()

    assert changes == [8, *range(len(labels))]


def test_all_twelve_palette_tabs_have_unique_colors():
    from map_editor.ui.colored_tabs import TAB_COLORS
    assert len(TAB_COLORS) == 12
    assert len(set(TAB_COLORS)) == 12
