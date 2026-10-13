"""Shared compatibility reference, displayed only in the checker dialog."""
from __future__ import annotations

from dataclasses import dataclass
from PySide6.QtGui import QColor
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFrame, QLabel, QScrollArea, QVBoxLayout, QWidget,
)

VANILLA = "vanilla"
MIXED = "mixed"
OPENNEOUA = "openneoua"
PREVIEW = "preview"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class CompatStyle:
    title: str
    color: str
    description: str


COMPAT_STYLES = {
    VANILLA: CompatStyle("Vanilla + OpenNeoUA", "#5cbf72", "Uses vanilla data formats."),
    MIXED: CompatStyle("Partly vanilla", "#e3c447", "Includes OpenNeoUA extensions."),
    OPENNEOUA: CompatStyle("OpenNeoUA only", "#e8873a", "Not supported by vanilla Urban Assault."),
    PREVIEW: CompatStyle("Preview / Studio only", "#5cbf72", "Does not change game data."),
    UNKNOWN: CompatStyle("Depends on the data", "#e3c447", "Check the target game's supported assets and parameters."),
}


def compat_color(level: str) -> QColor:
    return QColor(COMPAT_STYLES[level].color)


def compatibility_entries(window, key):
    """Describe each workspace using its existing pages and supported output formats."""
    if key == "entity_designer":
        tabs = window.properties_tabs
        return [(tabs.tabText(i), *window._tab_compatibility[tabs.widget(i)])
                for i in range(tabs.count())]
    if key == "map_editor":
        tabs = window.palette_tabs
        rows = []
        for i in range(tabs.count()):
            title = tabs.tabText(i)
            if title == "Script":
                rows.append((title, MIXED, "Vanilla level script sections remain compatible. Custom script text can contain OpenNeoUA-only parameters; this reference does not validate those lines."))
            else:
                rows.append((title, VANILLA, "Edits existing vanilla LDF level data. Referenced units, buildings, sectors and SET assets must also exist in the target game's installation."))
        return rows
    resources = [
        ("BAS Manager", PREVIEW, "Browsing and extracting SET.BAS resources does not change the archive. Extracted assets retain their source format and compatibility."),
        ("Entity List", PREVIEW, "Selects entities and visual states for the editor. Availability in vanilla depends on the source scripts and assets; listing an entity does not make it vanilla-compatible."),
    ]
    if key == "model_editor":
        return resources + [
            ("Model and Texture Editor", MIXED, "BASE/SKLT geometry, mapping and indexed ILBM/VBMP textures use vanilla formats. External models referenced through OpenNeoUA-only script parameters require OpenNeoUA; the .onua3d exchange format is for Studio import/export, not direct vanilla loading."),
            ("Asset Dependencies / Asset Textures", PREVIEW, "Inspects the model's resources and textures. Preview substitutions are session-only; exported assets must satisfy the target game's format and palette requirements."),
            ("Import / Export", MIXED, "Exporting supported native assets preserves their format. PNG, OBJ and .onua3d are presentation or exchange files and require conversion before use as vanilla assets."),
        ]
    if key == "snapshot_studio":
        return [("Snapshot", PREVIEW, "Camera, background, lights, guides and image export affect the presentation only. A screenshot does not alter the entity's runtime compatibility.")] + resources
    if key == "wireframe_editor":
        return [("Wireframe geometry / Save / Export", VANILLA, "Edits the existing 2D SKLT wireframe format. The game must still reference the exported resource correctly."),
                ("View and editing guides", PREVIEW, "Zoom, selection, grid and symmetry guides belong to Studio and add no game parameters.")]
    return [("Sector Editor", UNKNOWN, "This workspace is not yet available; there is no exported data to assess.")]


def install_compatibility_markers(window, key):
    """Use the checker's compatibility entries for every workspace tab marker."""
    from PySide6.QtWidgets import QTabWidget
    palettes = [tabs._palette_strip for tabs in window.findChildren(QTabWidget)
                if hasattr(tabs, "_palette_strip")]
    map_tabs = getattr(window, "palette_tabs", None)
    if not palettes and map_tabs is None:
        return
    entries = compatibility_entries(window, key)
    by_title = {title.casefold(): (level, note)
                for names, level, note in entries for title in names.split(" / ")}
    for name in ("Log", "Warnings", "Validation"):
        by_title[name.casefold()] = (PREVIEW, "Studio diagnostics do not change game data.")
    for strip in palettes:
        markers = {}
        for leaf in strip._collect():
            name = strip._leaf_text(leaf)
            level, note = by_title.get(name.casefold(), (UNKNOWN, COMPAT_STYLES[UNKNOWN].description))
            markers[strip._leaf_page(leaf)] = compat_color(level)
        strip.set_markers(markers)
        for button in strip.buttons():
            level, note = by_title.get(button.text().casefold(), (UNKNOWN, COMPAT_STYLES[UNKNOWN].description))
            button.setToolTip(f"{COMPAT_STYLES[level].title}. {note}")
    if map_tabs is not None and hasattr(map_tabs, "_buttons"):
        for button in map_tabs._buttons:
            level, note = by_title[button.text().casefold()]
            button.marker = compat_color(level)
            button.setToolTip(f"{COMPAT_STYLES[level].title}. {note}")
            button.update()


class CompatibilityDialog(QDialog):
    """Scrollable compatibility reference for the editor currently open."""

    def __init__(self, window, key):
        super().__init__(window)
        from startup_selector import TOOL_OPTIONS
        title = next(option.title for option in TOOL_OPTIONS if option.key == key)
        self.setWindowTitle(f"Vanilla Compatibility Checker — {title}")
        self.resize(740, 620)
        layout = QVBoxLayout(self)
        heading = QLabel(title)
        heading.setStyleSheet("font-size: 19px; font-weight: 600;")
        layout.addWidget(heading)
        intro = QLabel("Compatibility of the editor's features and output formats. This reference does not scan the current file or certify custom assets.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        legend = QLabel("Tab dots: green = vanilla formats or preview without data changes; yellow = mixed or dependent on the data; orange = OpenNeoUA only.")
        legend.setWordWrap(True)
        layout.addWidget(legend)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        cards = QVBoxLayout(content)
        cards.setContentsMargins(0, 0, 8, 0)
        self.entries = compatibility_entries(window, key)
        for name, level, note in self.entries:
            style = COMPAT_STYLES[level]
            card = QFrame()
            card.setStyleSheet("QFrame { background: #252930; border-radius: 6px; }")
            row = QVBoxLayout(card)
            label = QLabel(f"{name} · {style.title}")
            label.setWordWrap(True)
            label.setStyleSheet(f"color: {style.color}; font-size: 14px; font-weight: 600;")
            row.addWidget(label)
            detail = QLabel(note)
            detail.setWordWrap(True)
            detail.setTextFormat(Qt.TextFormat.PlainText)
            detail.setStyleSheet("color: #e4e8ef; padding: 2px;")
            row.addWidget(detail)
            cards.addWidget(card)
        cards.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
