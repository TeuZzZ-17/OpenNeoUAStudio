"""Icon Selector tab: pick a vehicle's type_icon from the H_FINDER font page.

The page is shown in place of the 3D viewport. Arrows, WASD or a click move the
selection; the Symbol field and Random choose the TYPE_NS.FON key written to
``type_icon``. A key bound to another cell is refused.
"""

from __future__ import annotations

import math
from pathlib import Path
import subprocess
import sys
import tempfile

from PySide6.QtCore import QPoint, QRect, QRectF, Qt, QSignalBlocker, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QColor, QCursor, QDesktopServices, QFont, QImage, QPainter, QPalette, QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from map_editor import bootstrap
from map_editor.core.resource_catalog import preview_image
from entity_designer.script_model import VEHICLE, _data_root_for_path
from entity_designer.type_icons import (
    FREE, TAKEN, IconAtlas, load_atlas, new_key_allowed, parse_symbol,
    symbol_text, vanilla_readable,
)
from studio.exchange.portable import backup_enabled, backup_existing_file, find_existing_path
from studio.formats.font_def import glyph_token
from studio.formats.images import (
    TextureConvertError, convert_png_to_ilbm, convert_png_to_new_ilbm, convert_to_png,
)

TAB_TITLE = "Icon Selector"

# Pointing hand drawn on the font page: '#' outline, 'w' glove, 'c' cuff.
_HAND = (
    ".....##.........",
    "....#ww#........",
    "....#ww#........",
    "....#ww#........",
    "....#ww###......",
    "....#ww#ww##....",
    "....#ww#ww#w##..",
    ".##.#ww#ww#w#w#.",
    "#ww##wwwwwwwwww#",
    "#www#wwwwwwwwww#",
    ".#wwwwwwwwwwwww#",
    "..#wwwwwwwwwwww#",
    "..#wwwwwwwwwww#.",
    "...#wwwwwwwwww#.",
    "....#cccccccc#..",
    "....#cccccccc#..",
    "....##########..",
)
_HAND_SCALE = 2
_HAND_HOTSPOT = (5 * _HAND_SCALE + 1, 1)
_KEY_MOVES = {
    Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_A: (-1, 0),
    Qt.Key.Key_Right: (1, 0), Qt.Key.Key_D: (1, 0),
    Qt.Key.Key_Up: (0, -1), Qt.Key.Key_W: (0, -1),
    Qt.Key.Key_Down: (0, 1), Qt.Key.Key_S: (0, 1),
}
_YELLOW = (255, 255, 0)


def hand_cursor(cuff: QColor) -> QCursor:
    """Pixel-art pointing hand whose fingertip is the hot spot."""

    pixmap = QPixmap(len(_HAND[0]) * _HAND_SCALE, len(_HAND) * _HAND_SCALE)
    pixmap.fill(Qt.GlobalColor.transparent)
    colors = {"#": QColor(16, 16, 20), "w": QColor(250, 250, 245), "c": cuff}
    painter = QPainter(pixmap)
    for y, row in enumerate(_HAND):
        for x, mark in enumerate(row):
            if mark in colors:
                painter.fillRect(x * _HAND_SCALE, y * _HAND_SCALE,
                                 _HAND_SCALE, _HAND_SCALE, colors[mark])
    painter.end()
    return QCursor(pixmap, *_HAND_HOTSPOT)


def page_image(path) -> QImage:
    # preview_image keeps decoded pages until the file changes.
    return preview_image(Path(path)).convertToFormat(QImage.Format.Format_RGBA8888)


def _image_size(path):
    image = page_image(path)
    if image.isNull():
        raise ValueError(f"cannot read {path}")
    return image.width(), image.height()


def cell_icon(image: QImage, atlas: IconAtlas, cell) -> tuple[QImage, bool]:
    """The cell's icon with the page yellow made transparent, as in the game,
    and whether the cell holds nothing else."""

    x, y = atlas.origin(cell)
    icon = image.copy(x, y, atlas.cell_width, atlas.cell_height)
    empty = True
    for py in range(icon.height()):
        for px in range(icon.width()):
            color = icon.pixelColor(px, py)
            if (color.red(), color.green(), color.blue()) == _YELLOW:
                icon.setPixelColor(px, py, QColor(0, 0, 0, 0))
            else:
                empty = False
    return icon, empty


class IconAtlasView(QWidget):
    """The font page enlarged, with the hovered and the selected cell."""

    cellPicked = Signal(int, int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.atlas: IconAtlas | None = None
        self.image = QImage()
        self.selected = None
        self.hovered = None
        self.keys: dict[tuple[int, int], str] = {}
        self.message = ""
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setCursor(hand_cursor(self.palette().color(QPalette.ColorRole.Highlight)))
        # The selection frame breathes gently while the page is shown.
        self._phase = 0.0
        self._pulse = QTimer(self)
        self._pulse.setInterval(50)
        self._pulse.timeout.connect(self._tick)

    def set_atlas(self, atlas: IconAtlas | None, image: QImage, message: str = "") -> None:
        self.atlas, self.image, self.message = atlas, image, message
        self.keys = {}
        if atlas is not None:
            for glyph in atlas.font.glyphs:
                cell = atlas.cell_of_key(glyph.code)
                if cell is not None and cell not in self.keys:
                    self.keys[cell] = symbol_text(glyph.code)
        self.update()

    def set_selected(self, cell) -> None:
        if cell != self.selected:
            self.selected = cell
            self.update()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._pulse.start()

    def hideEvent(self, event) -> None:  # noqa: N802
        self._pulse.stop()
        super().hideEvent(event)

    def _tick(self) -> None:
        self._phase = (self._phase + 0.12) % (2 * math.pi)
        if self.selected is not None:
            self.update()

    def _layout(self) -> tuple[int, QPoint]:
        """Whole-pixel zoom that fits the page, and the page's top-left corner."""

        if self.image.isNull():
            return 1, QPoint()
        margin = 24
        scale = max(1, min((self.width() - margin) // self.image.width(),
                           (self.height() - margin) // self.image.height()))
        left = (self.width() - self.image.width() * scale) // 2
        top = (self.height() - self.image.height() * scale) // 2
        return scale, QPoint(left, top)

    def cell_rect(self, cell) -> QRect:
        scale, corner = self._layout()
        x, y = self.atlas.origin(cell)
        return QRect(corner.x() + x * scale, corner.y() + y * scale,
                     self.atlas.cell_width * scale, self.atlas.cell_height * scale)

    def cell_at(self, point) -> tuple[int, int] | None:
        if self.atlas is None or self.image.isNull():
            return None
        scale, corner = self._layout()
        x = (point.x() - corner.x()) // scale - 1
        y = (point.y() - corner.y()) // scale - 1
        if x < 0 or y < 0:
            return None
        column = x // (self.atlas.cell_width + 1)
        row = y // (self.atlas.cell_height + 1)
        if column < self.atlas.columns and row < self.atlas.rows:
            return column, row
        return None

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.palette().color(QPalette.ColorRole.Base))
        if self.atlas is None or self.image.isNull():
            painter.setPen(self.palette().color(QPalette.ColorRole.PlaceholderText))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             self.message or "TYPE_NS.FON / H_FINDER not found.")
            return
        scale, corner = self._layout()
        target = QRect(corner, self.image.size() * scale)
        painter.drawImage(target, self.image)
        if scale >= 3:
            # The first symbol of each used cell, small in its corner.
            font = QFont(self.font())
            font.setPixelSize(max(9, scale * 3))
            font.setBold(True)
            painter.setFont(font)
            for cell, text in self.keys.items():
                rect = self.cell_rect(cell).adjusted(0, 0, -2, 0)
                painter.setPen(QColor(0, 0, 0, 150))
                painter.drawText(rect.translated(1, 1), Qt.AlignmentFlag.AlignRight
                                 | Qt.AlignmentFlag.AlignBottom, text)
                painter.setPen(QColor(255, 255, 255, 190))
                painter.drawText(rect, Qt.AlignmentFlag.AlignRight
                                 | Qt.AlignmentFlag.AlignBottom, text)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Dark and light rings stay visible on the yellow page and on the icons.
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if self.hovered is not None and self.hovered != self.selected:
            rect = QRectF(self.cell_rect(self.hovered)).adjusted(-1, -1, 1, 1)
            painter.setPen(QPen(QColor(0, 0, 0, 170), 3))
            painter.drawRoundedRect(rect, 3, 3)
            painter.setPen(QPen(QColor(255, 255, 255, 230), 1.5))
            painter.drawRoundedRect(rect, 3, 3)
        if self.selected is not None:
            accent = QColor(self.palette().color(QPalette.ColorRole.Highlight))
            rect = QRectF(self.cell_rect(self.selected)).adjusted(-3, -3, 3, 3)
            glow = 0.5 + 0.5 * math.sin(self._phase)
            halo = QColor(accent)
            halo.setAlpha(int(90 + 110 * glow))
            painter.setPen(QPen(halo, 9))
            painter.drawRoundedRect(rect, 5, 5)
            painter.setPen(QPen(QColor(0, 0, 0), 5))
            painter.drawRoundedRect(rect, 5, 5)
            painter.setPen(QPen(accent.lighter(130), 3))
            painter.drawRoundedRect(rect, 5, 5)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        cell = self.cell_at(event.position().toPoint())
        if cell != self.hovered:
            self.hovered = cell
            self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self.hovered = None
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        cell = self.cell_at(event.position().toPoint())
        if event.button() == Qt.MouseButton.LeftButton and cell is not None:
            self.cellPicked.emit(*cell)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        move = _KEY_MOVES.get(Qt.Key(event.key()))
        if move is None or self.atlas is None or event.modifiers() & (
                Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier):
            super().keyPressEvent(event)
            return
        column, row = self.selected or (0, 0)
        if self.selected is not None:
            column = min(max(column + move[0], 0), self.atlas.columns - 1)
            row = min(max(row + move[1], 0), self.atlas.rows - 1)
        self.cellPicked.emit(column, row)


class IconSelectorControls(QWidget):
    """Controls of the Icon Selector tab; ``editor`` is the Entity Designer window."""

    def __init__(self, editor) -> None:
        super().__init__(editor)
        self.editor = editor
        self.view = IconAtlasView()
        self.view.cellPicked.connect(self._cell_picked)
        self.atlas: IconAtlas | None = None
        # A cell picked while no valid symbol goes with it yet (or while no
        # vehicle is open): the project keeps its icon until the pair is complete.
        self._pending_cell = None
        self._seen_state = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 3, 2, 3)
        layout.setSpacing(3)
        group = QGroupBox(TAB_TITLE)
        group_layout = QGridLayout(group)
        group_layout.setContentsMargins(6, 4, 6, 6)
        group_layout.setHorizontalSpacing(8)
        group_layout.setVerticalSpacing(5)
        layout.addWidget(group)

        self.preview = QLabel()
        self.preview.setFixedSize(84, 68)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setAutoFillBackground(True)
        self.preview.setBackgroundRole(QPalette.ColorRole.Base)
        self.preview.setToolTip("Icon shown in the game for this unit.")
        group_layout.addWidget(self.preview, 0, 0, 3, 1)
        self.cell_label = QLabel()
        self.cell_label.setToolTip("Cell of the font page (column, row) and its pixel position.")
        group_layout.addWidget(self.cell_label, 0, 1, 1, 2)
        self.keys_label = QLabel()
        self.keys_label.setWordWrap(True)
        self.keys_label.setToolTip("TYPE_NS.FON symbols that already draw this cell.")
        group_layout.addWidget(self.keys_label, 1, 1, 2, 2)

        group_layout.addWidget(QLabel("Symbol"), 3, 0)
        symbol_row = QHBoxLayout()
        self.symbol_edit = QLineEdit()
        self.symbol_edit.setPlaceholderText("A or U+00E9")
        self.symbol_edit.setMaxLength(8)
        self.symbol_edit.setToolTip(
            "Key written as type_icon: one character or U+XXXX. A symbol that "
            "TYPE_NS.FON gives to another icon is refused; a new symbol must be "
            "one byte (up to U+00FF). Plain ASCII symbols also work in vanilla.")
        self.symbol_edit.textEdited.connect(self._symbol_edited)
        symbol_row.addWidget(self.symbol_edit, 1)
        self.random_button = QPushButton("Random")
        self.random_button.setToolTip(
            "Pick a symbol that TYPE_NS.FON does not use yet: a vanilla-readable "
            "ASCII symbol while any is left.")
        self.random_button.clicked.connect(self._random_symbol)
        symbol_row.addWidget(self.random_button)
        group_layout.addLayout(symbol_row, 3, 1, 1, 2)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        group_layout.addWidget(self.status_label, 4, 0, 1, 3)
        self.output = QLabel()
        self.output.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        group_layout.addWidget(self.output, 5, 0, 1, 3)
        group_layout.setColumnStretch(2, 1)

        # The font page file itself: open it, find it, convert PNG <-> ILBM.
        file_group = QGroupBox("Font Page")
        file_layout = QVBoxLayout(file_group)
        file_layout.setContentsMargins(6, 4, 6, 6)
        file_layout.setSpacing(5)
        self.page_label = QLabel()
        self.page_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        file_layout.addWidget(self.page_label)
        buttons = QGridLayout()
        buttons.setHorizontalSpacing(5)
        buttons.setVerticalSpacing(5)
        self.open_button = QPushButton("Open")
        self.open_button.setToolTip("Open the font page with its default program.")
        self.open_button.clicked.connect(self._open_page)
        self.open_with_button = QPushButton("Open With...")
        self.open_with_button.setToolTip("Choose the program that opens the font page.")
        self.open_with_button.clicked.connect(self._open_page_with)
        self.folder_button = QPushButton("Show in Folder")
        self.folder_button.setToolTip("Show the font page in its folder.")
        self.folder_button.clicked.connect(self._show_page)
        self.convert_button = QPushButton("Convert")
        self.convert_button.clicked.connect(self._convert_page)
        buttons.addWidget(self.open_button, 0, 0)
        buttons.addWidget(self.open_with_button, 0, 1)
        buttons.addWidget(self.folder_button, 1, 0)
        buttons.addWidget(self.convert_button, 1, 1)
        file_layout.addLayout(buttons)
        layout.addWidget(file_group)
        layout.addStretch(1)

    # -- data -------------------------------------------------------------------------

    def _data_root(self):
        root = _data_root_for_path(getattr(self.editor, "_active_script_path", None))
        if root is not None:
            return root
        try:
            return bootstrap.game_data_dir()
        except RuntimeError:
            return None

    def _load(self) -> None:
        self.atlas = load_atlas(self._data_root(), _image_size)
        image = page_image(self.atlas.image_path) if self.atlas else QImage()
        self.view.set_atlas(self.atlas, image)

    def _vehicle(self) -> bool:
        return self.editor.project.target_category == VEHICLE

    def _project_cell(self):
        project = self.editor.project
        if self.atlas is None or not project.type_icon or not self._vehicle():
            return None
        return project.type_icon_cell or self.atlas.cell_of_key(project.type_icon)

    def _selected_cell(self):
        return self._pending_cell or self._project_cell()

    # -- sync -------------------------------------------------------------------------

    def sync(self) -> None:
        project = self.editor.project
        state = (project.type_icon, project.type_icon_cell, project.target_category)
        if state != self._seen_state:
            # Undo, reset or a new definition replace any half-made choice.
            self._seen_state = state
            self._pending_cell = None
        if not self.isVisible():
            # The page is read again when the tab is shown (activate).
            return
        self._load()
        vehicle = self._vehicle()
        # The page can always be browsed; only a vehicle receives an icon.
        self.symbol_edit.setEnabled(vehicle)
        self.random_button.setEnabled(vehicle)
        cell = self._selected_cell()
        self.view.set_selected(cell)
        if self._pending_cell is None and not self.symbol_edit.hasFocus():
            with QSignalBlocker(self.symbol_edit):
                self.symbol_edit.setText(
                    symbol_text(project.type_icon)
                    if vehicle and project.type_icon else "")
        self._show_cell(cell)
        if self._pending_cell is None:
            self._show_status(*self._project_status())
        self.output.setText(
            f"type_icon = {glyph_token(project.type_icon)}"
            if vehicle and project.type_icon else "type_icon not written.")
        self._sync_page_buttons()

    @staticmethod
    def _compatibility(code: int) -> tuple[str, str]:
        if vanilla_readable(code):
            return "ok", "Vanilla compatible."
        return "warn", ("OpenNeoUA only: vanilla reads only plain ASCII "
                        "symbols from the script.")

    def _project_status(self) -> tuple[str, str]:
        project = self.editor.project
        if self.atlas is None:
            return "error", "TYPE_NS.FON or its font page was not found in Data/Fonts."
        if not self._vehicle():
            return "info", "Open a vehicle to give it an icon."
        if not project.type_icon:
            return "", ""
        if self.atlas.owner(project.type_icon) is None:
            if project.type_icon_cell is None:
                return "error", (f"{glyph_token(project.type_icon)} is not an icon of "
                                 "TYPE_NS.FON: the game shows a wrong icon.")
            level, text = self._compatibility(project.type_icon)
            return level, "New symbol: TYPE_NS.FON gets it when the script is written. " + text
        return self._compatibility(project.type_icon)

    def _show_cell(self, cell) -> None:
        if self.atlas is None or cell is None:
            self.preview.clear()
            self.cell_label.setText("No cell selected.")
            self.keys_label.setText("")
            return
        x, y = self.atlas.origin(cell)
        image, empty = cell_icon(self.view.image, self.atlas, cell)
        self.preview.setPixmap(QPixmap.fromImage(image).scaled(
            self.atlas.cell_width * 4, self.atlas.cell_height * 4,
            Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation))
        self.cell_label.setText(
            f"Cell {cell[0]}, {cell[1]}  ·  x {x}  y {y}" + ("  ·  empty" if empty else ""))
        keys = self.atlas.keys_on(cell)
        self.keys_label.setText("\n".join(
            f"{symbol_text(glyph.code)}  {glyph.note}".rstrip() for glyph in keys)
            or "No symbol yet.")

    def _show_status(self, level: str, text: str) -> None:
        colors = {"ok": "#7fd18b", "warn": "#f0b25a", "error": "#ff8a7a"}
        color = colors.get(level, "")
        self.status_label.setStyleSheet(f"color: {color};" if color else "")
        self.status_label.setText(text)
        self.status_label.setVisible(bool(text))

    # -- editing ----------------------------------------------------------------------

    def _commit(self, code: int, cell) -> None:
        project = self.editor.project
        new_cell = None if self.atlas.owner(code) is not None else cell
        self._pending_cell = None
        if (project.type_icon, project.type_icon_cell) == (code, new_cell):
            self.sync()
            return
        self.editor._push_undo()
        project.type_icon = code
        project.type_icon_cell = new_cell
        self._seen_state = (code, new_cell, project.target_category)
        self.editor._set_modified()
        self.editor._sync_all()

    def _hold(self, cell, level: str, text: str) -> None:
        """Show ``cell`` without giving it to the project."""

        self._pending_cell = cell
        self.view.set_selected(cell)
        self._show_cell(cell)
        self._show_status(level, text)

    def _try(self, code: int | None, cell) -> None:
        """Commit ``code`` on ``cell`` when TYPE_NS.FON allows it, else explain why not."""

        if self.atlas is None or cell is None:
            return
        if code is None:
            self._hold(cell, "info", "Type a symbol or press Random.")
            return
        status, glyph = self.atlas.check(code, cell)
        if status == TAKEN:
            self._hold(cell, "error", f"{symbol_text(code)} is already used by "
                       f"{glyph.note or 'another icon'}.")
            return
        if status == FREE and not new_key_allowed(code):
            self._hold(cell, "error", (
                f"{symbol_text(code)} is not a one-byte symbol: vanilla could not "
                "read it from TYPE_NS.FON. Use a symbol up to U+00FF."))
            return
        self._commit(code, cell)

    def _cell_picked(self, column: int, row: int) -> None:
        if self.atlas is None:
            return
        cell = (column, row)
        if not self._vehicle():
            self._hold(cell, *self._project_status())
            return
        project = self.editor.project
        keys = [glyph.code for glyph in self.atlas.keys_on(cell)]
        if project.type_icon in keys:
            code = project.type_icon
        elif keys:
            # 'I' is the debug unit: the Database hides it.
            code = next((key for key in keys if key != ord("I")), keys[0])
        else:
            typed = parse_symbol(self.symbol_edit.text())
            code = typed if typed is not None and self.atlas.owner(typed) is None else None
        self._try(code, cell)
        with QSignalBlocker(self.symbol_edit):
            self.symbol_edit.setText(symbol_text(code) if code else "")

    def _symbol_edited(self, text: str) -> None:
        cell = self._selected_cell()
        code = parse_symbol(text)
        if cell is None:
            self._show_status("info", "Select a cell first.")
            return
        if code is None:
            self._hold(cell, "error" if text.strip() else "info",
                       "Use one character or U+XXXX." if text.strip()
                       else "Type a symbol or press Random.")
            return
        self._try(code, cell)

    def _random_symbol(self) -> None:
        cell = self._selected_cell()
        if self.atlas is None or cell is None:
            self._show_status("info", "Select a cell first.")
            return
        code = self.atlas.random_free_key()
        if code is None:
            self._show_status("error", "No free one-byte symbol is left.")
            return
        with QSignalBlocker(self.symbol_edit):
            self.symbol_edit.setText(symbol_text(code))
        self._try(code, cell)

    def activate(self) -> None:
        """Called when the tab is shown: the page takes the keyboard."""

        self.sync()
        self.view.setFocus(Qt.FocusReason.OtherFocusReason)

    # -- font page file ---------------------------------------------------------------

    def _page_path(self) -> Path | None:
        return self.atlas.image_path if self.atlas is not None else None

    def _convert_target(self) -> Path | None:
        """The other format of the page: the ILBM named by TYPE_NS.FON, or its PNG."""

        source = self._page_path()
        if source is None:
            return None
        if source.suffix.casefold() == ".png":
            name = Path(self.atlas.font.page).name
            if Path(name).suffix.casefold() not in (".ilbm", ".ilb"):
                name = source.stem + ".ILBM"
            return find_existing_path(source.parent, name) or source.parent / name
        name = source.stem + ".PNG"
        return find_existing_path(source.parent, name) or source.parent / name

    def _sync_page_buttons(self) -> None:
        source = self._page_path()
        for button in (self.open_button, self.open_with_button,
                       self.folder_button, self.convert_button):
            button.setEnabled(source is not None)
        self.page_label.setText(source.name if source else "No font page.")
        self.page_label.setToolTip(str(source) if source else "")
        target = self._convert_target()
        if target is None:
            return
        to_png = target.suffix.casefold() == ".png"
        self.convert_button.setText("Convert to PNG" if to_png else "Convert to ILBM")
        self.convert_button.setToolTip(
            f"Write {target.name} from {source.name}. "
            + ("OpenNeoUA uses the PNG instead of the ILBM when both exist."
               if to_png else
               "Vanilla reads only the ILBM; OpenNeoUA prefers the PNG when both exist."))

    def _open_page(self) -> None:
        source = self._page_path()
        if source is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(source)))

    def _open_page_with(self) -> None:
        source = self._page_path()
        if source is None:
            return
        if sys.platform == "win32":
            # Windows' own "Open with" chooser.
            subprocess.Popen(["rundll32.exe", "shell32.dll,OpenAs_RunDLL", str(source)])
        else:
            self._open_page()

    def _show_page(self) -> None:
        source = self._page_path()
        if source is not None:
            from studio.workbench.common import _reveal_in_file_manager
            _reveal_in_file_manager(source)

    def _convert_page(self) -> None:
        source, target = self._page_path(), self._convert_target()
        if source is None or target is None:
            return
        if target.exists() and QMessageBox.question(
                self, "Convert font page",
                f"Replace {target.name} with a conversion of {source.name}?"
                + (" A .bak copy is kept." if backup_enabled() else ""),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        try:
            convert_font_page(source, target)
        except (OSError, TextureConvertError) as exc:
            QMessageBox.warning(self, "Convert font page", str(exc))
            return
        self.sync()
        self.editor.statusBar().showMessage(f"Font page written: {target}", 8000)


def convert_font_page(source: Path, target: Path) -> None:
    """Write ``target`` (PNG or ILBM) from ``source`` (the other format).

    An existing ILBM lends its palette and header to the new one, so the
    vanilla colours stay exactly as they were.
    """

    with tempfile.TemporaryDirectory(dir=target.parent, prefix=".font_page_") as folder:
        staged = Path(folder) / target.name
        if target.suffix.casefold() == ".png":
            convert_to_png(source, staged)
        elif target.exists():
            convert_png_to_ilbm(source, target, staged)
        else:
            convert_png_to_new_ilbm(source, staged)
        backup_existing_file(target)
        staged.replace(target)
