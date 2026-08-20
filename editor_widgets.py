"""Reusable Qt widgets and image helpers for the OpenNeoUAStudio workbench."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QSize, QTimer, Qt
from PySide6.QtGui import (
    QColor,
    QIcon,
    QImage,
    QPainter,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


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


class TexturePickerDialog(QDialog):
    """Searchable texture chooser with incremental thumbnails."""

    def __init__(self, names: list[str], current: str, thumbnail_loader,
                 parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Choose Texture")
        self.resize(720, 520)
        self._thumbnail_loader = thumbnail_loader
        self._pending_items: list[QListWidgetItem] = []

        layout = QVBoxLayout(self)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter textures...")
        self.search.textChanged.connect(self._filter_items)
        layout.addWidget(self.search)

        self.list = QListWidget()
        self.list.setViewMode(QListView.ViewMode.IconMode)
        self.list.setResizeMode(QListView.ResizeMode.Adjust)
        self.list.setMovement(QListView.Movement.Static)
        self.list.setIconSize(QSize(96, 96))
        self.list.setGridSize(QSize(132, 128))
        self.list.setWordWrap(True)
        self.list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        layout.addWidget(self.list, 1)

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
        QTimer.singleShot(0, self._load_thumbnail_batch)

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
