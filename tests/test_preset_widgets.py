"""Editable preset fields and the animated collapsible rows of the export panel."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QWidget, QVBoxLayout

from studio.ui.widgets import CollapsibleSection, PresetNumberBox


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_template_pick_typed_value_and_limits(app):
    box = PresetNumberBox((60, 30, 25), minimum=1, maximum=60, value=25)
    seen = []
    box.valueChanged.connect(seen.append)
    assert box.value() == 25
    box.setCurrentIndex(0)
    box.activated.emit(0)
    assert box.value() == 60 and seen == [60]
    box.setEditText("37")
    box.lineEdit().editingFinished.emit()
    assert box.value() == 37
    box.setEditText("500")                       # above the range: clamped
    box.lineEdit().editingFinished.emit()
    assert box.value() == 60
    box.setEditText("abc")                       # not a number: last value kept
    box.lineEdit().editingFinished.emit()
    assert box.value() == 60 and box.currentText() == "60"


def test_decimal_suffix_and_wheel_guard(app):
    box = PresetNumberBox((1, 5), minimum=0.1, maximum=3600, decimals=1, suffix=" s", value=5)
    box.show()
    assert box.value() == 5.0 and box.currentText().endswith(" s")
    box.setEditText("7,5 s")
    box.lineEdit().editingFinished.emit()
    assert box.value() == pytest.approx(7.5)
    box.clearFocus()
    wheel = QWheelEvent(QPointF(5, 5), QPointF(5, 5), box.pos(), box.pos(), Qt.NoButton,
                        Qt.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    before = box.currentIndex()
    box.wheelEvent(wheel)
    assert box.currentIndex() == before and not wheel.isAccepted()
    box.close()


def test_section_animates_to_a_single_final_state(app):
    host = QWidget()
    layout = QVBoxLayout(host)
    section = CollapsibleSection(host)
    section.grid.addWidget(QLabel("row"), 0, 0)
    layout.addWidget(section)
    host.show()
    QTest.qWait(30)
    section.set_expanded(False, animate=False)
    assert not section.isVisible()
    section.set_expanded(True)
    heights = []
    for _ in range(14):
        QTest.qWait(15)
        heights.append(section.height())
    assert section.isVisible() and heights[-1] == section.sizeHint().height()
    assert heights == sorted(heights)            # grows steadily, never jumps back
    section.set_expanded(False)
    QTest.qWait(400)
    assert not section.isVisible()
    host.close()
