"""Location fields, title preview and map perimeter tools."""
import copy
from pathlib import Path

from PySide6.QtCore import QObject, QEvent, QPointF, QRectF, QSize, Qt, QTimer, QElapsedTimer, QUrl
from PySide6.QtGui import QColor, QIcon, QPainter, QPalette, QPen, QPixmap, QPolygonF, QFont, QFontDatabase, QPainterPath
from PySide6.QtWidgets import (QApplication, QComboBox, QStyledItemDelegate, QStyle,
    QStyleOptionViewItem, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QListWidget,
    QListWidgetItem, QLineEdit, QSpinBox, QPushButton, QLabel, QFileDialog, QDialog)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput

from studio.exchange.portable import find_existing_path

from .. import bootstrap
from ..core.locations import (DEFAULT_LOCATION_COLOR, LOCATION_COLORS, new_location,
                               validate_location, contains_point, location_color, notice_opacity)
from .settings_lock import SettingsLockButton

LOCATION_COLOR_ROLE = int(Qt.ItemDataRole.UserRole)
LOCATION_DRAFT_ROLE = LOCATION_COLOR_ROLE + 1


def _color_icon(rgb):
    pixmap = QPixmap(14, 14)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(8, 12, 16), 1))
    painter.setBrush(QColor(*rgb))
    painter.drawRoundedRect(QRectF(.5, .5, 13, 13), 2, 2)
    painter.end()
    return QIcon(pixmap)


class LocationListDelegate(QStyledItemDelegate):
    """Keep the name readable and colour only the appended colour name."""

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        return QSize(size.width(), max(22, size.height()))

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ''
        style = opt.widget.style() if opt.widget is not None else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        name = str(index.data(Qt.ItemDataRole.DisplayRole) or '')
        color_name = str(index.data(LOCATION_COLOR_ROLE) or DEFAULT_LOCATION_COLOR).lower()
        color_label = color_name.capitalize()
        rgb = LOCATION_COLORS.get(color_name, LOCATION_COLORS[DEFAULT_LOCATION_COLOR])
        chosen = QColor(*rgb)
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        text_color = opt.palette.color(QPalette.ColorRole.HighlightedText if selected
                                       else QPalette.ColorRole.Text)
        rect = opt.rect.adjusted(7, 0, -7, 0)
        metrics = opt.fontMetrics
        gap = 9
        color_width = metrics.horizontalAdvance(color_label)
        name_text = metrics.elidedText(name, Qt.TextElideMode.ElideRight,
                                       max(0, rect.width() - color_width - gap))
        align = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        painter.save()
        painter.setClipRect(opt.rect)
        painter.setFont(opt.font)
        painter.setPen(text_color)
        painter.drawText(rect, align, name_text)
        x = rect.left() + metrics.horizontalAdvance(name_text) + gap
        painter.setPen(chosen)
        painter.drawText(QRectF(x, rect.top(), rect.right() - x, rect.height()), align, color_label)
        painter.restore()


class LocationNotice(QWidget):
    _loaded_family = None
    def __init__(self, value):
        super().__init__()
        self.value = value
        self.setMinimumSize(500, 300)
        self.clock = QElapsedTimer()
        self.timer = QTimer(self, interval=16)
        self.timer.timeout.connect(self._tick)
        self.family = self._loaded_family or 'Xolonium'
        font_path = bootstrap.game_data_dir() / 'Fonts' / 'Xolonium-Bold.otf'
        if self._loaded_family is None and font_path.is_file():
            font_id = QFontDatabase.addApplicationFont(str(font_path))
            families = QFontDatabase.applicationFontFamilies(font_id)
            if families:
                self.family = families[0]
                type(self)._loaded_family = self.family

    def play(self):
        self.clock.start()
        self.timer.start()
        self.update()

    def _tick(self):
        if self.clock.elapsed() >= sum(self.value[k] for k in ('fade_in_ms', 'hold_ms', 'fade_out_ms')):
            self.timer.stop()
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor('#152630'))
        alpha = notice_opacity(self.value, self.clock.elapsed()) if self.clock.isValid() else 1
        painter.setOpacity(alpha)
        width, height = self.width(), self.height()
        top = height * .19
        small = QFont(self.family)
        small.setPixelSize(max(10, round(height * .023)))
        small.setBold(True)
        painter.setFont(small)
        painter.setPen(QColor('#90b9c2'))
        painter.drawText(QRectF(18, top, width - 36, 22), Qt.AlignmentFlag.AlignCenter, 'LOCATION')
        title = QFont(self.family)
        title.setPixelSize(max(14, round(height * .076)))
        title.setBold(True)
        painter.setFont(title)
        while painter.fontMetrics().horizontalAdvance(self.value['name']) > width - 48 and title.pixelSize() > 6:
            title.setPixelSize(title.pixelSize() - 1)
            painter.setFont(title)
        measured = painter.fontMetrics().horizontalAdvance(self.value['name'])
        scale = min(1, (width - 48) / max(1, measured))
        painter.save()
        painter.translate(width / 2, top + 28)
        painter.scale(scale, scale)
        rect = QRectF(-width / (2 * scale), 0, width / scale, 60 / scale)
        painter.setPen(QColor(0, 0, 0, 160))
        painter.drawText(rect.translated(1, 2), Qt.AlignmentFlag.AlignCenter, self.value['name'])
        color = LOCATION_COLORS.get(location_color(self.value), LOCATION_COLORS[DEFAULT_LOCATION_COLOR])
        painter.setPen(QColor(*color))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self.value['name'])
        painter.restore()
        painter.setPen(QPen(QColor('#80afb7'), 1))
        y = top + 94
        painter.drawLine(QPointF(width * .3, y), QPointF(width * .48, y))
        painter.drawLine(QPointF(width * .52, y), QPointF(width * .7, y))
        painter.end()


class LocationPanel(QWidget):
    def __init__(self):
        super().__init__()
        self.controller = None
        self.loading = False
        self.value = new_location(1)
        layout = QVBoxLayout(self)
        self.list = QListWidget()
        self.list.setItemDelegate(LocationListDelegate(self.list))
        layout.addWidget(self.list, 1)
        self.lock = SettingsLockButton()
        row = QHBoxLayout()
        for text, action in (('New', 'new'), ('Delete', 'delete'), ('Find', 'find')):
            button = QPushButton(text)
            button.clicked.connect(lambda checked=False, name=action: getattr(self.controller, name)())
            row.addWidget(button)
        row.addWidget(self.lock)
        layout.addLayout(row)
        fields = QWidget()
        form = QFormLayout(fields)
        self.name = QLineEdit()
        self.name.setMaxLength(96)
        self.color = QComboBox()
        for name, rgb in LOCATION_COLORS.items():
            self.color.addItem(_color_icon(rgb), name.capitalize(), name)
        self.color.currentTextChanged.connect(self.color_changed)
        self.sound = QLineEdit(placeholderText='Data/Sounds/…')
        form.addRow('Name', self.name)
        form.addRow('Text color', self.color)
        sound_row = QHBoxLayout()
        sound_row.addWidget(self.sound, 1)
        browse = QPushButton('Choose')
        browse.clicked.connect(self.choose_sound)
        sound_row.addWidget(browse)
        play = QPushButton('Listen')
        play.clicked.connect(self.play_sound)
        sound_row.addWidget(play)
        form.addRow('Entering sound', sound_row)
        self.times = {}
        for key, text, maximum in (('fade_in_ms', 'Fade in (ms)', 10000),
                                    ('hold_ms', 'Hold (ms)', 60000), ('fade_out_ms', 'Fade out (ms)', 10000)):
            spin = QSpinBox(minimum=0, maximum=maximum)
            self.times[key] = spin
            spin.valueChanged.connect(self.change)
            form.addRow(text, spin)
        self.name.editingFinished.connect(self.change)
        self.sound.editingFinished.connect(self.change)
        layout.addWidget(self.lock.wrap(fields))
        row = QHBoxLayout()
        cancel = QPushButton('Cancel')
        cancel.clicked.connect(lambda checked=False: self.controller.cancel())
        row.addWidget(cancel)
        layout.addLayout(row)
        preview = QPushButton('Preview title')
        preview.clicked.connect(self.preview)
        layout.addWidget(preview)
        self.status = QLabel('OpenNeoUA only · Left click: vertices · Close: first vertex or right-click · Backspace: undo · Esc: cancel')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(.35)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio)
        self.list.currentRowChanged.connect(self.select)
        self.set_value(self.value)

    def values(self):
        color = self.color.currentData() or DEFAULT_LOCATION_COLOR
        return self.value | dict(name=self.name.text().strip(), color=color,
                                sound=self.sound.text().strip(),
                                **{key: spin.value() for key, spin in self.times.items()})

    def set_value(self, value):
        self.loading = True
        self.value = copy.deepcopy(value)
        self.name.setText(value['name'])
        color = self.color.findData(location_color(value))
        self.color.setCurrentIndex(color if color >= 0 else 0)
        self.update_name_color()
        self.sound.setText(value['sound'])
        for key, spin in self.times.items():
            spin.setValue(value[key])
        self.loading = False

    def update_name_color(self):
        color = self.color.currentData() or DEFAULT_LOCATION_COLOR
        rgb = LOCATION_COLORS.get(color, LOCATION_COLORS[DEFAULT_LOCATION_COLOR])
        self.name.setStyleSheet(f'QLineEdit {{ color: rgb({rgb[0]}, {rgb[1]}, {rgb[2]}); }}')

    def color_changed(self, _name=''):
        self.update_name_color()
        self.change()

    def _add_location_item(self, value, draft=False):
        item = QListWidgetItem(value['name'] or 'New location')
        color = location_color(value)
        item.setData(LOCATION_COLOR_ROLE, color)
        item.setData(LOCATION_DRAFT_ROLE, draft)
        suffix = ' · draft, not saved yet' if draft else ''
        item.setToolTip(f'{item.text()} · {color.capitalize()}{suffix}')
        self.list.addItem(item)
        return item

    def refresh(self):
        if self.controller is None:
            return
        self.loading = True
        self.list.clear()
        for value in self.controller.window.doc.locations:
            self._add_location_item(value)
        if self.controller.draft is not None:
            self._add_location_item(self.controller.draft, draft=True)
            index = self.list.count() - 1
        else:
            index = self.controller.selected
            if not 0 <= index < self.list.count():
                index = -1
        self.list.setCurrentRow(index)
        self.loading = False
        if self.controller.draft is not None:
            self.set_value(self.controller.draft)
        elif 0 <= index < self.list.count():
            self.set_value(self.controller.window.doc.locations[index])
        else:
            self.set_value(new_location(1))

    def update_draft_row(self):
        if self.controller is None or self.controller.draft is None:
            return
        draft = self.controller.draft
        color = location_color(draft)
        for index in range(self.list.count()):
            item = self.list.item(index)
            if item.data(LOCATION_DRAFT_ROLE):
                item.setText(draft['name'] or 'New location')
                item.setData(LOCATION_COLOR_ROLE, color)
                item.setToolTip(f'{item.text()} · {color.capitalize()} · draft, not saved yet')
                return

    def select(self, index):
        if not self.loading and self.controller is not None:
            item = self.list.item(index)
            if item is not None and item.data(LOCATION_DRAFT_ROLE):
                self.controller.selected = -1
                if self.controller.draft is not None:
                    self.set_value(self.controller.draft)
                    self.controller.window.view.update()
                return
            self.controller.selected = index
            self.controller.cancel()
            if 0 <= index < len(self.controller.window.doc.locations):
                self.set_value(self.controller.window.doc.locations[index])
            self.controller.window.view.update()

    def change(self):
        if not self.loading and self.controller is not None:
            self.controller.change(self.values())
            self.update_draft_row()

    def choose_sound(self):
        data = bootstrap.game_data_dir().resolve()
        start = find_existing_path(data, 'Sounds') or data
        path, _ = QFileDialog.getOpenFileName(self, 'Choose location sound', str(start),
                                             'Wave sound (*.wav *.WAV)')
        if path:
            try:
                relative = Path(path).resolve().relative_to(data)
            except ValueError:
                self.status.setText('Choose a sound inside the game Data folder.')
                return
            self.sound.setText('Data/' + relative.as_posix())
            self.change()

    def play_sound(self):
        value = self.values()['sound']
        data = bootstrap.game_data_dir().resolve()
        path = ((find_existing_path(data, value[5:]) or data / value[5:]).resolve()
                if value.startswith('Data/') else data)
        if not path.is_relative_to(data) or not path.is_file():
            self.status.setText('Choose an existing sound inside Data.')
            return
        self.player.stop()
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.player.play()

    def preview(self):
        if hasattr(self, "_preview_dialog"):
            self._preview_dialog.close()
            self._preview_dialog.deleteLater()
        dialog = QDialog(self)
        dialog.setWindowTitle('Location title · OpenNeoUA')
        layout = QVBoxLayout(dialog)
        notice = LocationNotice(self.values())
        layout.addWidget(notice)
        play = QPushButton('Simulate entry')
        def start():
            notice.play()
            if self.values()['sound']:
                self.play_sound()
        play.clicked.connect(start)
        layout.addWidget(play)
        dialog.finished.connect(lambda _: (notice.timer.stop(), self.player.stop()))
        dialog.resize(720, 450)
        dialog.show()
        self._preview_dialog = dialog
        start()


class LocationTools(QObject):
    def __init__(self, window, panel, tab):
        super().__init__(window)
        self.window, self.panel, self.tab = window, panel, tab
        self.selected = -1
        self.draft = None
        self.hover = None
        self.drag = None
        panel.controller = self
        window.view.location_tools = self
        window.view.installEventFilter(self)

    def active(self):
        return self.window.palette_tabs.currentIndex() == self.tab

    def cancel(self):
        self.draft = self.drag = self.hover = None
        self.panel.refresh()
        self.window.view.update()

    def new(self):
        self.window._cancel_operation(clear=False)
        if self.window.view.camera.perspective:
            self.window.view.reset_camera()
        if len(self.window.doc.locations) >= 256:
            self.panel.status.setText('Only 256 locations can be saved.')
            return
        self.selected = -1
        used = {v['id'] for v in self.window.doc.locations}
        self.draft = new_location(next(i for i in range(1, 258) if i not in used))
        self.panel.refresh()
        self.panel.status.setText('Draft · click vertices on the map · Close Area is in the right-click menu')
        self.window.view.setFocus()
        self.window.view.update()

    def _commit(self, value):
        try:
            validate_location(value)
        except ValueError as error:
            self.panel.status.setText(str(error))
            return False
        doc = self.window.doc
        values = copy.deepcopy(doc.locations)
        index = next((i for i, old in enumerate(values) if old['id'] == value['id']), len(values))
        if index == len(values):
            values.append(copy.deepcopy(value))
        else:
            values[index] = copy.deepcopy(value)
        if values == doc.locations:
            return True
        self.window.history.push(doc)
        doc.locations = values
        self.selected = index
        self.draft = self.drag = None
        self.window.dirty = True
        self.window._after_history()
        self.panel.status.setText('Location saved · OpenNeoUA only')
        return True

    def close(self):
        if self.draft is not None and not self.panel.lock.isChecked():
            self._commit(self.panel.values() | {'id': self.draft['id'], 'points': self.draft['points']})

    def change(self, value):
        if self.panel.lock.isChecked():
            return
        if self.draft is not None:
            self.draft.update({key: item for key, item in value.items() if key not in ('id', 'points')})
            self.window.view.update()
        elif 0 <= self.selected < len(self.window.doc.locations):
            self._commit(value)

    def delete(self):
        if self.draft is not None:
            self.cancel()
            self.panel.refresh()
        elif 0 <= self.selected < len(self.window.doc.locations):
            self.window.history.push(self.window.doc)
            del self.window.doc.locations[self.selected]
            self.selected = min(self.selected, len(self.window.doc.locations) - 1)
            self.window.dirty = True
            self.window._after_history()

    def find(self):
        if 0 <= self.selected < len(self.window.doc.locations):
            points = self.window.doc.locations[self.selected]['points']
            x = sum(p[0] for p in points) / len(points)
            z = sum(p[1] for p in points) / len(points)
            col, row = int(x // 1200), int(-z // 1200)
            y = self.window.view.terrain.cell_y(col, row)
            self.window.view.camera.center = (x, y, z)
            self.window.view.camera.pan = (0, 0)
            self.window.view.cameraChanged.emit()
            self.window.view.update()

    def _point(self, position):
        view = self.window.view
        cell = view.ground_cell(position.x(), position.y())
        if cell is None or not (0 <= cell[0] < self.window.doc.mw and 0 <= cell[1] < self.window.doc.mh):
            return None
        world = view.camera.screen_to_ground(position.x(), position.y(), view.terrain.cell_y(*cell))
        if world is None or not (0 <= world[0] <= self.window.doc.mw * 1200 and
                                  -self.window.doc.mh * 1200 <= world[2] <= 0):
            return None
        return round(world[0], 5), round(world[2], 5)

    def _screen(self, point):
        view = self.window.view
        x, z = point
        col, row = int(x // 1200), int(-z // 1200)
        y = view.terrain.cell_y(col, row) if 0 <= col < view.terrain.width and 0 <= row < view.terrain.height else 0
        return QPointF(*view.camera.world_to_screen((x, y - 4, z))[0])

    def eventFilter(self, obj, event):
        if not self.active():
            return False
        kind = event.type()
        if kind == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Escape and (self.draft is not None or self.drag is not None):
                self.cancel()
                self.panel.refresh()
                return True
            if event.key() == Qt.Key.Key_Backspace and self.draft is not None:
                if self.draft['points']:
                    self.draft['points'].pop()
                obj.update()
                return True
        if kind == QEvent.Type.MouseButtonDblClick and event.button() == Qt.MouseButton.LeftButton:
            return True
        if kind == QEvent.Type.MouseMove:
            point = self._point(event.position())
            self.hover = point
            if self.drag is not None and point is not None:
                self.drag[1]['points'][self.drag[0]] = point
                obj.update()
                return True
            if self.draft is not None:
                obj.update()
        if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            obj.setFocus()
            point = self._point(event.position())
            if point is None:
                return True
            if self.draft is not None and not self.panel.lock.isChecked():
                points = self.draft['points']
                if len(points) >= 3 and (self._screen(points[0]) - event.position()).manhattanLength() < 16:
                    self.close()
                elif len(points) < 256 and point not in points:
                    points.append(point)
                obj.update()
                return True
            values = self.window.doc.locations
            if 0 <= self.selected < len(values) and not self.panel.lock.isChecked():
                for index, vertex in enumerate(values[self.selected]['points']):
                    if (self._screen(vertex) - event.position()).manhattanLength() < 14:
                        self.drag = (index, copy.deepcopy(values[self.selected]))
                        return True
            for index in range(len(values) - 1, -1, -1):
                if contains_point(values[index]['points'], *point):
                    self.panel.list.setCurrentRow(index)
                    break
            return True
        if kind == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
            if self.drag is not None:
                value = self.drag[1]
                self.drag = None
                self._commit(value)
                obj.update()
            return True
        return False

    def _color(self, value):
        rgb = LOCATION_COLORS.get(location_color(value), LOCATION_COLORS[DEFAULT_LOCATION_COLOR])
        return QColor(*rgb)

    def _polygon(self, points):
        view = self.window.view
        if not view.camera.perspective:
            return QPolygonF([self._screen(p) for p in points])
        vertices = []
        for x, z in points:
            col, row = int(x // 1200), int(-z // 1200)
            y = view.terrain.cell_y(col, row) if 0 <= col < view.terrain.width and 0 <= row < view.terrain.height else 0
            vertices.append(view.camera.to_camera((x, y - 4, z)))
        # Clip against the near plane before projecting; never draw behind-camera polygons.
        clipped = []
        for a, b in zip(vertices, vertices[1:] + vertices[:1]):
            inside_a, inside_b = a[2] < 3.99, b[2] < 3.99
            if inside_a:
                clipped.append(a)
            if inside_a != inside_b:
                t = (3.99 - a[2]) / (b[2] - a[2])
                clipped.append(tuple(a[i] + t * (b[i] - a[i]) for i in range(3)))
        return QPolygonF([QPointF(*view.camera.to_screen(p)) for p in clipped])

    def draw(self, painter):
        editing = self.active() and not self.window.view.camera.perspective
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for index, value in enumerate(self.window.doc.locations):
            if self.draft is not None and value['id'] == self.draft['id']:
                continue
            if self.drag is not None and index == self.selected:
                value = self.drag[1]
            polygon = self._polygon(value['points'])
            base = self._color(value)
            selected = editing and index == self.selected
            line = QColor(base)
            line.setAlpha(230 if selected else 220)
            fill = QColor(base)
            fill.setAlpha(12 if selected else 8)
            outline = QColor(8, 12, 16, 130 if selected else 85)
            painter.setPen(QPen(outline, 2.8 if selected else 2.2))
            painter.setBrush(fill)
            painter.drawPolygon(polygon)
            painter.setPen(QPen(line, 1.5 if selected else 1.4))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPolygon(polygon)
            if polygon.count():
                position = polygon[0] + QPointF(8, -9)
                font = QFont(painter.font())
                font.setBold(True)
                label = QPainterPath()
                label.addText(position, font, value['name'])
                border = QPen(QColor(8, 12, 18, 235), 3, Qt.PenStyle.SolidLine,
                              Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
                painter.strokePath(label, border)
                painter.fillPath(label, base)
            if selected:
                painter.setPen(QPen(QColor(8, 12, 18, 220), 1.5))
                painter.setBrush(line)
                for point in polygon:
                    painter.drawEllipse(point, 4, 4)
        if editing and self.draft is not None and self.draft['points']:
            polygon = QPolygonF([self._screen(p) for p in self.draft['points']])
            base = self._color(self.draft)
            if len(polygon) >= 3:
                painter.setPen(QPen(QColor(8, 12, 16, 160), 2))
                painter.setBrush(QColor(base.red(), base.green(), base.blue(), 12))
                painter.drawPolygon(polygon)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(base, 2))
            painter.drawPolyline(polygon)
            painter.setPen(QPen(QColor(8, 12, 18, 210), 1))
            painter.setBrush(base)
            for point in polygon:
                painter.drawEllipse(point, 4, 4)
            if self.hover:
                ghost = QColor(base)
                ghost.setAlpha(170)
                painter.setPen(QPen(ghost, 1, Qt.PenStyle.DashLine))
                painter.drawLine(polygon[-1], self._screen(self.hover))
                painter.drawLine(self._screen(self.hover), polygon[0])
        painter.restore()
