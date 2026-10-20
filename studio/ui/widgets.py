"""Reusable Qt widgets, list styling, busy feedback and small transform dialogs.
"""

from __future__ import annotations

from contextlib import contextmanager
from functools import wraps

from PySide6.QtCore import (
    QEasingCurve, QEventLoop, QLocale, QPointF, QPropertyAnimation, QSize, Qt, QTimer, Signal,
)
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QIcon,
    QImage,
    QPainter,
    QPainterPath,
    QPalette,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

# ----------------------------------------------------------------------------
# Dirty-resource text shared by the entity catalog and archive browser.
# (formerly resource_status.py)
# ----------------------------------------------------------------------------

MODIFIED_ROLE = int(Qt.ItemDataRole.UserRole) + 90


ORIGINAL_COLORS_ROLE = MODIFIED_ROLE + 1


MODIFIED_COLOR = QColor(255, 80, 80)


LIST_BASE = "#2d2d2d"


LIST_SELECTED = "#006d96"


LIST_HOVER = "#234e64"


LIST_DEPENDENCY_SELECTED = "#31483a"


# One selection look for every resource list (game catalog, BAS Manager,
# entity lists). Without it some lists took the Windows accent color and
# others the native grey.
SELECTION_STYLE = (
    f"QTreeView, QListView {{ background-color: {LIST_BASE}; alternate-background-color: {LIST_BASE}; }}"
    f"QTreeView::item:selected, QListView::item:selected, QTreeView::item:selected:!active, QListView::item:selected:!active {{ background-color: {LIST_SELECTED}; color: #ffffff; }}"
    f"QTreeView::item:hover:!selected, QListView::item:hover:!selected {{ background-color: {LIST_HOVER}; color: #ffffff; }}"
)


def use_standard_list_style(view, *, selected_color: str | None = None) -> None:
    """Apply the shared list colors, with an optional selection accent."""

    view.setAlternatingRowColors(False)
    view.setMouseTracking(True)
    if not QApplication.instance().property("studioStyleLoaded") and SELECTION_STYLE not in view.styleSheet():
        view.setStyleSheet(view.styleSheet() + SELECTION_STYLE)
    if selected_color is not None:
        view.setStyleSheet(
            view.styleSheet()
            + f"QTreeWidget::item:selected, QTreeWidget::item:selected:!active {{ background-color: {selected_color}; color: #ffffff; }}"
            + f"QTreeWidget::item:selected:hover {{ background-color: {LIST_HOVER}; color: #ffffff; }}"
        )


def use_resource_delegate(tree) -> None:
    """Shared dirty-row delegate and selection colors of a resource list."""

    use_standard_list_style(tree)
    tree.setItemDelegate(ResourceStatusDelegate(tree))


# Rows of assets that could not be resolved (missing, ambiguous, failed to
# load) are painted red, over the green "used by the model" tint.
PROBLEM_ROLE = Qt.ItemDataRole.UserRole + 41
PROBLEM_BACKGROUND = QColor("#6e2b2b")
PROBLEM_STATUSES = frozenset(
    {"missing", "ambiguous", "failed_load", "decode failed", "skipped",
     "unsupported_loader"})


def mark_problem_item(item, problem: bool, columns: int = 1) -> None:
    """Flag a tree/list item as an unresolved asset and tint its row red."""

    item.setData(0, PROBLEM_ROLE, bool(problem)) if columns > 1 else item.setData(PROBLEM_ROLE, bool(problem))
    brush = QBrush(PROBLEM_BACKGROUND) if problem else QBrush()
    if columns > 1:
        for column in range(columns):
            item.setBackground(column, brush)
    else:
        item.setBackground(brush)


class ProblemRowDelegate(QStyledItemDelegate):
    """Draw flagged rows red even when they are selected or hovered."""

    def paint(self, painter, option, index):
        if index.siblingAtColumn(0).data(PROBLEM_ROLE):
            option = QStyleOptionViewItem(option)
            option.state &= ~(QStyle.StateFlag.State_Selected
                              | QStyle.StateFlag.State_MouseOver
                              | QStyle.StateFlag.State_HasFocus)
        super().paint(painter, option, index)


class ResourceStatusDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        # Keep modified text red while preserving distinct selection and hover bands.
        highlighted = QStyle.StateFlag.State_Selected | QStyle.StateFlag.State_MouseOver
        if index.siblingAtColumn(0).data(MODIFIED_ROLE) and option.state & highlighted:
            visible = QStyleOptionViewItem(option)
            visible.state &= ~(QStyle.StateFlag.State_Selected | QStyle.StateFlag.State_MouseOver)
            self.initStyleOption(visible, index)
            visible.backgroundBrush = QBrush(QColor(
                "#15435b" if option.state & QStyle.StateFlag.State_Selected
                else "#213e50"))
            style = visible.widget.style()
            style.drawControl(QStyle.ControlElement.CE_ItemViewItem, visible, painter, visible.widget)
            return
        super().paint(painter, option, index)

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        if index.siblingAtColumn(0).data(MODIFIED_ROLE):
            option.palette.setColor(QPalette.ColorRole.HighlightedText, MODIFIED_COLOR)


def mark_resource_modified(item, modified):
    previous = bool(item.data(0, MODIFIED_ROLE))
    if previous == modified:
        return
    if modified:
        item.setData(0, ORIGINAL_COLORS_ROLE, [item.foreground(i) for i in range(item.columnCount())])
    colors = item.data(0, ORIGINAL_COLORS_ROLE) or []
    for column in range(item.columnCount()):
        item.setForeground(column, QBrush(MODIFIED_COLOR) if modified else colors[column])
    item.setData(0, MODIFIED_ROLE, modified)


# ----------------------------------------------------------------------------
# Shared busy feedback for synchronous resource loads on the GUI thread.
# (formerly loading_feedback.py)
# ----------------------------------------------------------------------------

DIALOG_STYLE = (
    "QDialog { background: #15141b; }"
    "QLabel { color: #ece9f7; background: transparent; font-size: 14px; }"
)


@contextmanager
def loading_resources(parent, label):
    if getattr(parent, '_resource_load_active', False):
        yield
        return
    parent._resource_load_active = True
    dialog = QDialog(parent)
    dialog.setWindowTitle('Loading Resources')
    # Explicit colours: without them the dialog can paint as a blank white box.
    dialog.setStyleSheet(DIALOG_STYLE)
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(24, 22, 24, 22)
    message = QLabel(label)
    message.setWordWrap(True)
    message.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(message)
    dialog.setMinimumWidth(320)
    dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
    dialog.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
    dialog.show()
    # Paint the feedback without allowing another edit or set change mid-load.
    QApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
    dialog.repaint()
    QApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
    try:
        yield
    finally:
        dialog.close()
        dialog.deleteLater()
        parent._resource_load_active = False


def loading_set(method):
    @wraps(method)
    def wrapped(self, number, *args, **kwargs):
        with loading_resources(self, f'Loading Set {number}. Please wait…'):
            return method(self, number, *args, **kwargs)
    return wrapped


BAS_ARCHIVE_FILTER = (
    "SET.BAS archives (SET.BAS *.bas *.BAS);;All files (*)"
)


def create_import_bas_archive_action(parent, callback, *, shortcut=False) -> QAction:
    """Create the canonical BAS-archive import action used by Studio tools."""

    action = QAction("Import BAS Archive", parent)
    if shortcut:
        from PySide6.QtGui import QKeySequence
        action.setShortcut(QKeySequence.StandardKey.Open)
    action.triggered.connect(callback)
    return action


def choose_bas_archive(parent, directory) -> str:
    """Open the shared SET.BAS chooser and return the selected path or ''."""

    path, _ = QFileDialog.getOpenFileName(
        parent, "Import BAS Archive", str(directory), BAS_ARCHIVE_FILTER)
    return path


def configure_operation_status_bar(window):
    """Style the shared bottom-left non-blocking operation channel."""

    bar = window.statusBar()
    bar.setObjectName("operationStatusBar")
    bar.setMinimumHeight(30)
    bar.setStyleSheet(
        "QStatusBar#operationStatusBar {"
        " color: #ff7474; font-size: 15px; font-weight: 600;"
        " padding-left: 4px; }"
        "QStatusBar#operationStatusBar::item { border: none; }")
    return bar


def install_standard_file_menu_tail(
        file_menu, parent, *, close_archive_callback=None, exit_text="Exit"):
    """Install the canonical Close Current Resource / Exit tail.

    Editors that do not own a closeable Studio resource omit ``close_archive_callback`` and
    receive only the shared separator + Exit action.  The actual close
    lifecycle remains editor-owned; only the action semantics and placement
    are centralized here.
    """

    close_action = None
    if close_archive_callback is not None:
        close_action = QAction("Close Current Resource", parent)
        close_action.setEnabled(False)
        close_action.triggered.connect(close_archive_callback)
        file_menu.addAction(close_action)
    file_menu.addSeparator()
    exit_action = QAction(exit_text, parent)
    exit_action.triggered.connect(parent.close)
    file_menu.addAction(exit_action)
    return close_action, exit_action


STATUS_COLORS = {
    "found": QColor(90, 200, 110),
    "manual": QColor(110, 170, 255),
    "manual (SET.BAS)": QColor(110, 170, 255),
    "setbas": QColor(120, 210, 210),
    "ambiguous": QColor(255, 190, 70),
    "missing": QColor(240, 90, 90),
    "decode failed": QColor(200, 90, 200),
}


class ResponsiveButtonGrid(QWidget):
    """Keep a short tool row readable by wrapping it at narrow widths."""

    def __init__(self, buttons: list[QPushButton], parent=None) -> None:
        super().__init__(parent)
        self._buttons = buttons
        self._columns = 0
        self._layout = QGridLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setHorizontalSpacing(5)
        self._layout.setVerticalSpacing(5)
        self.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Minimum,
        )
        for button in self._buttons:
            button.setMinimumWidth(0)
            button.setSizePolicy(
                QSizePolicy.Policy.Ignored,
                QSizePolicy.Policy.Fixed,
            )
        self._relayout(2)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(0, self._layout.minimumSize().height())

    def _required_width(self, columns: int) -> int:
        spacing = self._layout.horizontalSpacing()
        widest_row = 0
        for start in range(0, len(self._buttons), columns):
            row = self._buttons[start:start + columns]
            row_width = sum(button.sizeHint().width() for button in row)
            row_width += spacing * max(0, len(row) - 1)
            widest_row = max(widest_row, row_width)
        return widest_row

    def _best_columns(self, available_width: int) -> int:
        candidates = [len(self._buttons)]
        if len(self._buttons) > 2:
            candidates.append(2)
        candidates.append(1)
        for columns in candidates:
            if available_width >= self._required_width(columns):
                return columns
        return 1

    def _relayout(self, columns: int) -> None:
        if columns == self._columns:
            return
        while self._layout.count():
            self._layout.takeAt(0)
        for index, button in enumerate(self._buttons):
            row, column = divmod(index, columns)
            self._layout.addWidget(button, row, column)
        for column in range(len(self._buttons)):
            self._layout.setColumnStretch(column, 0)
        for column in range(columns):
            self._layout.setColumnStretch(column, 1)
        self._columns = columns
        self._layout.activate()
        height = self._layout.sizeHint().height()
        self.setMinimumHeight(height)
        self.setMaximumHeight(height)
        self.updateGeometry()

    def resizeEvent(self, event) -> None:  # noqa: N802
        self._relayout(self._best_columns(event.size().width()))
        super().resizeEvent(event)


class ViewportWidthScrollArea(QScrollArea):
    """Keep the scroll page exactly as wide as its visible viewport."""

    def setWidget(self, widget: QWidget) -> None:  # noqa: N802
        widget.setMinimumWidth(0)
        widget.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        super().setWidget(widget)
        QTimer.singleShot(0, self._sync_page_width)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._sync_page_width()
        QTimer.singleShot(0, self._sync_page_width)

    def _sync_page_width(self) -> None:
        page = self.widget()
        if page is None:
            return
        width = self.viewport().width()
        if width <= 0:
            return
        if page.maximumWidth() != width:
            page.setMaximumWidth(width)
        if page.width() != width:
            page.resize(width, page.height())
        page.updateGeometry()


class PresetNumberBox(QComboBox):
    """Number field with a list of ready-made values that can still be typed over.

    Pick a template from the drop-down, or type any value inside the range.
    It offers the small spin-box API (``value``, ``setValue``, ``valueChanged``)
    so it replaces a spin box without changes in the code that reads it.
    """

    valueChanged = Signal(float)

    def __init__(self, presets, *, minimum: float, maximum: float, decimals: int = 0,
                 suffix: str = "", value: float | None = None, parent=None) -> None:
        super().__init__(parent)
        self._minimum, self._maximum = float(minimum), float(maximum)
        self._decimals = decimals
        self._suffix = suffix
        self._value = self._clamp(presets[0] if value is None else value)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        for preset in presets:
            self.addItem(self._format(preset), float(preset))
        self.lineEdit().setPlaceholderText(self._format(self._value))
        self.activated.connect(self._preset_chosen)
        self.lineEdit().editingFinished.connect(self._commit_text)
        self._show(self._value)

    def _clamp(self, value: float) -> float:
        value = max(self._minimum, min(self._maximum, float(value)))
        return round(value, self._decimals)

    def _format(self, value: float) -> str:
        return QLocale().toString(float(value), "f", self._decimals) + self._suffix

    def _show(self, value: float) -> None:
        self.setEditText(self._format(value))

    def value(self):
        return int(self._value) if self._decimals == 0 else self._value

    def setValue(self, value: float) -> None:  # noqa: N802
        value = self._clamp(value)
        self._show(value)
        if value != self._value:
            self._value = value
            self.valueChanged.emit(value)

    def setSuffix(self, suffix: str) -> None:  # noqa: N802
        self._suffix = suffix
        self._show(self._value)

    def _preset_chosen(self, index: int) -> None:
        self.setValue(self.itemData(index))

    def _commit_text(self) -> None:
        """Accept typed text; anything that is not a number restores the last value."""

        text = self.currentText().replace(self._suffix.strip(), "").strip()
        number, ok = QLocale().toDouble(text)
        if not ok:
            number, ok = QLocale.c().toDouble(text)
        self.setValue(number if ok else self._value)
        self._show(self._value)

    def wheelEvent(self, event) -> None:  # noqa: N802
        # Scrolling a panel over the field must not change the value.
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class CollapsibleSection(QWidget):
    """Group of rows that open and close with a short animation.

    Showing or hiding many widgets one by one makes everything below jump.
    The rows live in one container whose height is animated instead, so the
    layout moves smoothly in a single motion.
    """

    DURATION_MS = 140

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self._expanded = True
        self._animation = QPropertyAnimation(self, b"maximumHeight", self)
        self._animation.setDuration(self.DURATION_MS)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.finished.connect(self._animation_finished)

    @property
    def expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, expanded: bool, *, animate: bool = True) -> None:
        if expanded == self._expanded and not self._animation.state():
            return
        self._expanded = expanded
        self._animation.stop()
        full = self.sizeHint().height()
        parent = self.parentWidget()
        if not animate or parent is None or not parent.isVisible():
            self.setMaximumHeight(16777215)
            self.setVisible(expanded)
            return
        start = self.height() if self.isVisible() else 0
        self.setVisible(True)
        self._animation.setStartValue(start)
        self._animation.setEndValue(full if expanded else 0)
        self._animation.start()

    def _animation_finished(self) -> None:
        if self._expanded:
            self.setMaximumHeight(16777215)
        else:
            self.setVisible(False)


def status_icon(status: str) -> QPixmap:
    pix = QPixmap(12, 12)
    pix.fill(STATUS_COLORS.get(status, QColor(150, 150, 150)))
    return pix


def checker_thumbnail(image: QImage, size: int = 96) -> QPixmap:
    """Render an image over an alpha checkerboard."""

    scaled = image.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                          Qt.TransformationMode.FastTransformation)
    pix = QPixmap(scaled.size())
    painter = QPainter(pix)
    cell = 8
    for y in range(0, scaled.height(), cell):
        for x in range(0, scaled.width(), cell):
            light = ((x // cell) + (y // cell)) % 2 == 0
            painter.fillRect(
                x, y, cell, cell,
                QColor(200, 200, 200) if light
                else QColor(140, 140, 140))
    painter.drawImage(0, 0, scaled)
    painter.end()
    return pix


def qimage_from_ilbm(image, palette_override=None) -> QImage | None:
    """Convert a decoded ILBM/VBMP image to a Qt preview image."""

    if image is None:
        return None
    rgba = image.to_rgba_bytes(
        palette_override=palette_override,
        alpha_mode="chroma",
    )
    if rgba is None:
        return None
    qimage = QImage(
        rgba,
        image.width,
        image.height,
        image.width * 4,
        QImage.Format.Format_RGBA8888,
    )
    return qimage.convertToFormat(QImage.Format.Format_ARGB32)


def draw_uv_polygon(painter: QPainter, uvs, size: int) -> None:
    points = [QPointF(u / 256 * size, v / 256 * size) for u, v in uvs]
    if len(points) >= 2:
        painter.drawPolygon(QPolygonF(points))
    for point in points:
        painter.drawEllipse(point, 2.0, 2.0)


def crop_uv_region(image: QImage, groups) -> QImage:
    """Show only the selected UV footprint, with transparent pixels outside it."""
    if image is None or image.isNull():
        return QImage()
    path = QPainterPath()
    path.setFillRule(Qt.FillRule.WindingFill)
    for uvs in groups:
        if len(uvs) >= 3:
            path.addPolygon(QPolygonF([
                QPointF(u / 256.0 * image.width(), v / 256.0 * image.height())
                for u, v in uvs]))
            path.closeSubpath()
    if path.isEmpty():
        return QImage()
    masked = QImage(image.size(), QImage.Format.Format_ARGB32_Premultiplied)
    masked.fill(Qt.GlobalColor.transparent)
    painter = QPainter(masked)
    painter.setClipPath(path)
    painter.drawImage(0, 0, image)
    painter.end()
    bounds = path.boundingRect().toAlignedRect().intersected(image.rect())
    return masked.copy(bounds) if not bounds.isEmpty() else QImage()


def configure_stable_image_preview(label: QLabel, height: int) -> None:
    """Keep animated preview frames from changing dialog geometry.

    QLabel derives its size hint from the current pixmap.  Animated textures can
    use frames with different aspect ratios/sizes, so letting that hint feed
    back into the layout makes the dialog grow/shrink on every frame.  Preview
    widgets deliberately ignore pixmap width hints and keep one fixed viewport
    height; frames are then fitted inside that stable viewport.
    """

    label.setMinimumWidth(0)
    label.setFixedHeight(max(1, int(height)))
    label.setSizePolicy(
        QSizePolicy.Policy.Ignored,
        QSizePolicy.Policy.Fixed,
    )
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)


class TexturePickerDialog(QDialog):
    """Searchable texture chooser with incremental thumbnails."""

    def __init__(self, names: list[str], current: str, thumbnail_loader,
                 parent=None, *, frame_loader=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Choose Texture")
        self.resize(720, 520)
        self._thumbnail_loader = thumbnail_loader
        self._pending_items: list[QListWidgetItem] = []
        self._frame_loader = frame_loader
        self._frames = []
        self._frame_index = 0
        self._preview_timer = QTimer(self)
        self._preview_timer.timeout.connect(self._advance_frame)

        layout = QVBoxLayout(self)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter textures")
        self.search.textChanged.connect(self._filter_items)
        layout.addWidget(self.search)

        self.list = QListWidget()
        use_standard_list_style(self.list)
        self.list.setViewMode(QListView.ViewMode.IconMode)
        self.list.setResizeMode(QListView.ResizeMode.Adjust)
        self.list.setMovement(QListView.Movement.Static)
        self.list.setIconSize(QSize(96, 96))
        self.list.setGridSize(QSize(132, 128))
        self.list.setWordWrap(True)
        self.list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        layout.addWidget(self.list, 1)
        self.preview = QLabel()
        configure_stable_image_preview(self.preview, 180)
        self.preview.setVisible(frame_loader is not None)
        layout.addWidget(self.preview)

        placeholder = QPixmap(96, 96)
        placeholder.fill(QColor(42, 44, 50))
        current_item = None
        for name in names:
            item = QListWidgetItem(QIcon(placeholder), name)
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
            self.list.addItem(item)
            self._pending_items.append(item)
            if name.lower() == current.lower():
                current_item = item
        if current_item is not None:
            self.list.setCurrentItem(current_item)
            self.list.scrollToItem(current_item)
        elif self.list.count():
            self.list.setCurrentRow(0)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.list.itemDoubleClicked.connect(lambda _item: self.accept())
        self.list.currentItemChanged.connect(lambda *_: self._refresh_preview())
        self._refresh_preview()
        QTimer.singleShot(0, self._load_thumbnail_batch)

    def _refresh_preview(self):
        self._preview_timer.stop()
        self._frames = []
        self._frame_index = 0
        if self._frame_loader is not None and self.selected_name():
            self._frames = self._frame_loader(self.selected_name())
        self._show_frame()

    def _show_frame(self):
        if not self._frames:
            self.preview.setText("No compatible UV preview")
            return
        image, duration = self._frames[self._frame_index]
        self.preview.setPixmap(checker_thumbnail(image, 180))
        if len(self._frames) > 1:
            self._preview_timer.start(max(1, round(duration)))

    def _advance_frame(self):
        if self._frames:
            self._frame_index = (self._frame_index + 1) % len(self._frames)
            self._show_frame()

    def done(self, result):
        self._preview_timer.stop()
        super().done(result)

    def _filter_items(self, text: str) -> None:
        needle = text.strip().lower()
        first_visible = None
        for index in range(self.list.count()):
            item = self.list.item(index)
            visible = not needle or needle in item.text().lower()
            item.setHidden(not visible)
            if visible and first_visible is None:
                first_visible = item
        current = self.list.currentItem()
        if current is None or current.isHidden():
            self.list.setCurrentItem(first_visible)

    def _load_thumbnail_batch(self) -> None:
        for _ in range(min(6, len(self._pending_items))):
            item = self._pending_items.pop(0)
            try:
                image = self._thumbnail_loader(
                    item.data(Qt.ItemDataRole.UserRole))
            except Exception:
                image = None
            if image is not None and not image.isNull():
                item.setIcon(QIcon(checker_thumbnail(image, 96)))
        if self._pending_items and self.isVisible():
            QTimer.singleShot(0, self._load_thumbnail_batch)

    def selected_name(self) -> str | None:
        item = self.list.currentItem()
        return (item.data(Qt.ItemDataRole.UserRole)
                if item is not None else None)


# ----------------------------------------------------------------------------
# Small non-modal transform dialogs used by the model editor.
# (formerly transform_dialogs.py)
# ----------------------------------------------------------------------------

class LiveScaleDialog(QDialog):
    """Uniform scale slider whose changes are previewed live."""

    factorChanged = Signal(float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Scale")
        self.setWindowModality(Qt.WindowModality.NonModal)
        layout = QVBoxLayout(self)
        self.value_label = QLabel("Scale: 1.00x")
        self.value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.value_label)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(10, 400)
        self.slider.setValue(100)
        self.slider.setTickInterval(25)
        self.slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.slider.valueChanged.connect(self._value_changed)
        layout.addWidget(self.slider)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(360, self.sizeHint().height())

    def factor(self) -> float:
        return self.slider.value() / 100.0

    def _value_changed(self, value: int) -> None:
        factor = value / 100.0
        self.value_label.setText(f"Scale: {factor:.2f}x")
        self.factorChanged.emit(factor)


class LiveRotateDialog(QDialog):
    """Model-space axis/angle controls with a live rotation preview."""

    rotationChanged = Signal(str, float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Rotate")
        self.setWindowModality(Qt.WindowModality.NonModal)
        layout = QVBoxLayout(self)

        axis_row = QHBoxLayout()
        axis_row.addWidget(QLabel("Axis"))
        self.axis_combo = QComboBox()
        self.axis_combo.addItems(["X", "Y", "Z"])
        axis_row.addWidget(self.axis_combo, 1)
        layout.addLayout(axis_row)

        angle_row = QHBoxLayout()
        angle_row.addWidget(QLabel("Angle"))
        self.angle_spin = QDoubleSpinBox()
        self.angle_spin.setRange(-360.0, 360.0)
        self.angle_spin.setDecimals(2)
        self.angle_spin.setSingleStep(1.0)
        self.angle_spin.setSuffix("°")
        angle_row.addWidget(self.angle_spin, 1)
        layout.addLayout(angle_row)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(-3600, 3600)
        self.slider.setValue(0)
        self.slider.setTickInterval(450)
        self.slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        layout.addWidget(self.slider)

        self.axis_combo.currentTextChanged.connect(self._emit_rotation)
        self.angle_spin.valueChanged.connect(self._spin_changed)
        self.slider.valueChanged.connect(self._slider_changed)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(360, self.sizeHint().height())

    def axis(self) -> str:
        return self.axis_combo.currentText()

    def angle_degrees(self) -> float:
        return self.angle_spin.value()

    def _spin_changed(self, value: float) -> None:
        slider_value = round(value * 10.0)
        if self.slider.value() != slider_value:
            self.slider.blockSignals(True)
            self.slider.setValue(slider_value)
            self.slider.blockSignals(False)
        self._emit_rotation()

    def _slider_changed(self, value: int) -> None:
        angle = value / 10.0
        if self.angle_spin.value() != angle:
            self.angle_spin.blockSignals(True)
            self.angle_spin.setValue(angle)
            self.angle_spin.blockSignals(False)
        self._emit_rotation()

    def _emit_rotation(self, _value=None) -> None:
        self.rotationChanged.emit(self.axis(), self.angle_degrees())
