import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PySide6.QtWidgets import QApplication, QTreeWidget, QVBoxLayout, QWidget

from studio.ui.sorting import (
    SortableTreeItem, SortableListWidget, add_list_sort, enable_tree_sort, sort_key,
)


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


def test_sort_key_orders_numbers_by_value_not_text():
    values = ["10", "2", "1"]
    assert sorted(values, key=sort_key) == ["1", "2", "10"]


def test_sort_key_orders_labels_case_insensitively():
    values = ["banana", "Apple", "cherry"]
    assert sorted(values, key=sort_key) == ["Apple", "banana", "cherry"]


def test_add_list_sort_toggles_ascending_then_descending(app):
    widget = QWidget()
    layout = QVBoxLayout(widget)
    lst = SortableListWidget()
    add_list_sort(layout, lst, label="Name")
    lst.addItems(["Charlie", "Alpha", "Bravo"])
    # The sort button is the first widget added to the layout.
    button = layout.itemAt(0).widget()
    button.click()
    assert [lst.item(i).text() for i in range(lst.count())] == ["Alpha", "Bravo", "Charlie"]
    button.click()
    assert [lst.item(i).text() for i in range(lst.count())] == ["Charlie", "Bravo", "Alpha"]


def test_enable_tree_sort_orders_numeric_column(app):
    tree = QTreeWidget()
    tree.setHeaderLabels(["Name", "ID"])
    enable_tree_sort(tree)
    for name, id_ in (("Bravo", "10"), ("Alpha", "2"), ("Charlie", "1")):
        tree.addTopLevelItem(SortableTreeItem([name, id_]))
    header = tree.header()
    header.sectionClicked.emit(1)
    ids = [tree.topLevelItem(i).text(1) for i in range(tree.topLevelItemCount())]
    assert ids == ["1", "2", "10"]
    header.sectionClicked.emit(1)
    ids = [tree.topLevelItem(i).text(1) for i in range(tree.topLevelItemCount())]
    assert ids == ["10", "2", "1"]


def test_destruction_effects_header_sort_is_visual_only(app):
    from sector_editor.effects_editor import DestructionEffectsEditor
    editor = DestructionEffectsEditor()
    editor._catalog = {1: "Spark", 2: "Smoke"}
    editor.add_effect((1, 0, 0, 300))
    editor.add_effect((2, 0, 0, 100))
    editor.add_effect((1, 0, 0, 200))
    before = editor.values()
    header = editor.table.horizontalHeader()
    header.sectionClicked.emit(3)
    # Visual order follows Height ascending, but the saved effect order is untouched.
    vheader = editor.table.verticalHeader()
    visual_heights = [editor.table.cellWidget(vheader.logicalIndex(pos), 3).value()
                      for pos in range(editor.table.rowCount())]
    assert visual_heights == [100, 200, 300]
    assert editor.values() == before
