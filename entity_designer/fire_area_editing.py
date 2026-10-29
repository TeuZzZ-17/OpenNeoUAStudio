"""Fire Area tab logic of the Entity Designer window (kept out of editor.py).

The mixin owns what is specific to this tab: the preview-only state, the panel
and viewport wiring, grip dragging and nudging. Data, undo, the script writers
and the shared move gizmo stay in ``CollisionEditorWindow``.
"""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtWidgets import QSizePolicy, QVBoxLayout, QWidget

from entity_designer import destruction_fx as fx
from entity_designer import fire_area as fa
from entity_designer.fire_area_panel import FireAreaPanel
from entity_designer.fire_area_units import find_firing_units
from entity_designer.script_model import (
    WEAPON,
    fire_area_data_lines,
    fire_area_problems,
)
from entity_designer.shape import visual_transform_point


class FireAreaEditingMixin:
    def _init_fire_area(self) -> None:
        self._fire_area_handle = ""
        self._fire_area_height = 1000.0
        # The camera is framed on the volume only when a weapon or vehicle is opened
        # or the tab is entered, never after an edit. The request waits for the next
        # sync because the muzzle height must be applied first.
        self._fire_area_frame_pending = False
        # The pending frame closes in on the unit, so that is the first state.
        self._fire_area_focus_on_unit = True
        # (volume, size, dead_zone) when a grip drag starts: the drag is always
        # computed from it, so rounding never accumulates.
        self._fire_area_drag_start = None
        self._fire_area_weapon_family: str | None = None
        self._fire_area_framed_definition = None
        # Vehicles that fire the loaded weapon and the one shown (None = only the
        # weapon at its muzzle).
        self._fire_area_units: list = []
        self._fire_area_unit_id: int | None = None
        # Preview-only memory: the size last edited for each volume, so going back
        # to a volume brings its edit back. ``loaded`` is the area read from the
        # script, the target of the Reset button.
        self._fire_area_drafts: dict[str, tuple] = {}
        self._fire_area_loaded = fa.FireArea()
        # Preview-only: where a forward shot flies (fa.AIM_LOW / fa.AIM_HIGH).
        self._fire_area_aim = fa.AIM_LOW

    def _build_fire_area_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(2, 3, 2, 3)
        layout.setSpacing(3)
        page.setMinimumSize(0, 0)
        page.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.fire_area_panel = FireAreaPanel(page)
        panel = self.fire_area_panel
        panel.shapeChosen.connect(self._fire_area_shape_chosen)
        panel.unitChosen.connect(self._fire_area_unit_chosen)
        panel.valueEdited.connect(self._fire_area_value_edited)
        panel.editFinished.connect(
            lambda field: self._finish_vehicle_preview_edit(f"fire_area_{field}"))
        panel.frameRequested.connect(lambda: self._fire_area_frame(unit_only=False))
        panel.frameUnitRequested.connect(lambda: self._fire_area_frame(unit_only=True))
        panel.resetRequested.connect(self._fire_area_reset_all)
        panel.aimChosen.connect(self._fire_area_aim_chosen)
        # The unit's own animation runs and pauses with the shot.
        self.fx_panel.play_button.toggled.connect(self._fire_area_transport_toggled)
        layout.addWidget(panel, 1)
        viewport = self.viewport
        viewport.fireAreaHandlePicked.connect(self._fire_area_handle_picked)
        viewport.fireAreaDragStarted.connect(self._fire_area_drag_started)
        viewport.fireAreaDragged.connect(self._fire_area_dragged)
        viewport.fireAreaFocusToggled.connect(self._fire_area_toggle_focus)
        return page

    # -- state ----------------------------------------------------------------------

    def _is_fire_area_tab_active(self) -> bool:
        return (hasattr(self, "fire_area_tab_index")
                and self.properties_tabs.currentIndex() == self.fire_area_tab_index
                and self.project.target_category == WEAPON)

    def _fire_area_edit_shape(self) -> str | None:
        """Volume the panel and the grips edit: the active one, else the first set."""

        area = self.project.fire_area
        shapes = area.authored_shapes()
        return area.active_shape() or (shapes[0] if shapes else None)

    def _fire_area_load_defaults(self, text: str, block) -> None:
        """Remember what the weapon class accepts, who fires it and a preview height."""

        def value(key: str) -> str:
            return fx.top_level_value(text, block.start_line, block.end_line, key) or ""

        self._fire_area_weapon_family = fa.usable_family(
            value("model"), value("vertical_laser_enable").strip() == "1")
        self._fire_area_handle = ""
        self._fire_area_drafts = {}
        self._fire_area_loaded = self.project.fire_area
        objects = getattr(self.game_object_browser, "objects", None) or []
        self._fire_area_units = (
            find_firing_units(block.object_id, objects, text)
            if block.kind == "new_weapon" else [])
        self._fire_area_unit_id = (
            self._fire_area_units[0].vehicle_id if self._fire_area_units else None)
        self._fire_area_reset_height()
        self._fire_area_frame_pending = True

    def _fire_area_unit(self):
        """The firing unit shown in the preview, or None."""

        return next((unit for unit in self._fire_area_units
                     if unit.vehicle_id == self._fire_area_unit_id), None)

    def _fire_area_reset_height(self) -> None:
        """Preview height of the muzzle: a ground unit stands on the floor, a flying
        unit or a bare weapon starts at the default (editable) height."""

        unit = self._fire_area_unit()
        self._fire_area_height = 0.0 if unit is not None and not unit.flies else 1000.0

    def _fire_area_unit_position(self) -> tuple[float, float, float]:
        """Where the firing unit (or the bare muzzle) is: y is down, ground at 0."""

        unit = self._fire_area_unit()
        rest = unit.overeof if unit is not None else 0.0
        return (0.0, -(self._fire_area_height + rest), 0.0)

    def _fire_area_muzzle(self) -> tuple[float, float, float]:
        """The fire point: the unit's fire_y/fire_z ahead of its centre. The sideways
        range (fire_x) is not a point, it widens the volume (see x span)."""

        x, y, z = self._fire_area_unit_position()
        unit = self._fire_area_unit()
        if unit is not None:
            y, z = y + unit.fire[1], z + unit.fire[2]
        return (x, y, z)

    def _fire_area_x_span(self) -> tuple[float, float]:
        unit = self._fire_area_unit()
        return unit.x_span if unit is not None else (0.0, 0.0)

    def _fire_area_shots(self) -> list:
        """(launch, target) of every projectile of the first firing event, one per
        sideways muzzle position of the unit (fire_x and its modes); empty without
        a valid volume. A bare weapon fires one projectile from the centre."""

        shape = self._fire_area_edit_shape()
        size = self.project.fire_area.size(shape) if shape else None
        if size is None or not fa.valid_size(shape, size):
            return []
        unit = self._fire_area_unit()
        muzzle = self._fire_area_muzzle()
        shots = []
        for x in (unit.muzzle_xs() if unit is not None else [0.0]):
            shots.append(fa.shot_path(
                shape, size, (muzzle[0] + x, muzzle[1], muzzle[2]), 0.0,
                self.project.fire_area.max_inclination, self._fire_area_aim))
        return shots

    def _fire_area_extra_vps(self) -> set[int]:
        """VP of the firing unit, so its model is loaded with the effects."""

        unit = self._fire_area_unit()
        if unit is None or not self._is_fire_area_tab_active():
            return set()
        return {unit.vp}

    def _fire_area_unit_items(self) -> list:
        """The firing unit as a preview item, at rest on the ground or at height."""

        unit = self._fire_area_unit()
        if unit is None:
            return []
        # Resolved by the scene preview: from the unit's family, or from the set
        # when the weapon is a loose BASE (same path the effects' VPs take).
        columns = [visual_transform_point(axis, unit.scale, unit.rotation)
                   for axis in ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))]
        rotation = tuple(tuple(columns[column][row] for column in range(3))
                         for row in range(3))
        return [fx.PreviewItem(
            -1, "unit", unit.vp, self._fire_area_unit_position(), rotation,
            (self.project.model_scale_x, self.project.model_scale_y,
             self.project.model_scale_z), 1.0, None,
            f"firing unit · {unit.label}")]

    def _fire_area_note(self) -> str:
        family = self._fire_area_weapon_family
        if family == "below":
            return "This weapon is dropped: the engine accepts only the Below volumes."
        if family == "forward":
            return "This weapon is fired ahead: the engine accepts only the Forward volumes."
        return ("The AI opens fire when a target is inside the volume. Drag the "
                "squares in the 3D view to resize it.")

    def _sync_fire_area(self) -> None:
        """Push the project's area to the panel and the viewport."""

        if not hasattr(self, "fire_area_panel"):
            return
        project = self.project
        weapon = project.target_category == WEAPON
        area = project.fire_area
        shape = self._fire_area_edit_shape()
        if not weapon or shape is None:
            self._fire_area_handle = ""
        _errors, warnings = fire_area_problems(area)
        family = self._fire_area_weapon_family
        if shape is not None and family and fa.is_below(shape) != (family == "below"):
            warnings.append(
                f"{fa.SHAPE_LABELS[shape]} is not used by this weapon class: "
                "the engine ignores it.")
        lines = fire_area_data_lines(project) if weapon else []
        preview_lines = []
        for line in lines:
            key, separator, value = line.partition(" = ")
            if key == "ai_fire_trigger_dead_zone" and separator:
                line = f"{key} = {round(float(value))}"
            preview_lines.append(line)
        self.fire_area_panel.set_state(
            area, shape, available=weapon, height=self._fire_area_height,
            lines=preview_lines or (
                [] if weapon else ["; Fire Area is available only for weapons"]),
            note=self._fire_area_note() if weapon else
            "Select a weapon: ai_fire_trigger_* belong to weapon definitions.",
            warning=" ".join(warnings),
            units=[(unit.vehicle_id, unit.label) for unit in self._fire_area_units],
            unit_id=self._fire_area_unit_id, aim=self._fire_area_aim)
        active = self._is_fire_area_tab_active()
        shots = self._fire_area_shots() if active else []
        shot = shots[0] if shots else None
        self.viewport.set_fire_area(
            area if weapon else fa.FireArea(), shape if weapon else None,
            self._fire_area_handle, origin=self._fire_area_muzzle(),
            x_span=self._fire_area_x_span(),
            focus=[self._fire_area_unit_position(), *( [shot[1]] if shot else [])])
        # The weapon flies the shot only while this tab is open.
        self.fx_panel.set_shot(shot, shots[1:])
        if active:
            self._fire_area_transport_toggled(self.fx_panel.is_playing())

    def _fire_area_transport_toggled(self, playing: bool) -> None:
        """The firing unit's VANM animation follows Play / Pause of the shot."""

        if self._is_fire_area_tab_active() and self.play_button.isChecked() != playing:
            self.play_button.setChecked(playing)

    # -- edits ----------------------------------------------------------------------

    def _set_fire_area(self, area: fa.FireArea) -> None:
        self.project.fire_area = area
        if not self._modified:
            self._set_modified()
        self._sync_all()

    def _fire_area_shape_chosen(self, shape) -> None:
        if self._syncing or self.project.target_category != WEAPON:
            return
        area = self._fire_area_volume_for(shape)
        if area == self.project.fire_area:
            return
        self._vehicle_preview_active_edits.clear()
        self._push_undo()
        self._fire_area_handle = ""
        self.project.fire_area = area
        self._fire_area_reset_height()
        self._set_fire_area(area)

    def _fire_area_volume_for(self, shape: str | None) -> fa.FireArea:
        """The area showing only ``shape``: its size as edited here, else as written
        in the script, else the guide default. The volume left is remembered."""

        area = self.project.fire_area
        current = self._fire_area_edit_shape()
        if current is not None and area.size(current) is not None:
            self._fire_area_drafts[current] = area.size(current)
        if shape is None:
            return area.with_only(None)
        size = (area.size(shape) or self._fire_area_drafts.get(shape)
                or self._fire_area_loaded.size(shape) or fa.DEFAULT_SIZES[shape])
        return area.with_only(None).with_size(shape, size)

    def _fire_area_reset_all(self) -> None:
        """Everything the tab shows goes back exactly as when the weapon was opened:
        the area as the script has it (also none at all), the firing unit, height,
        shot zone and the camera on the unit."""

        if self._syncing or self.project.target_category != WEAPON:
            return
        area, current = self._fire_area_loaded, self.project.fire_area
        self._fire_area_drafts.clear()
        self._fire_area_unit_id = (
            self._fire_area_units[0].vehicle_id if self._fire_area_units else None)
        self._fire_area_aim = fa.AIM_LOW
        self._fire_area_reset_height()
        self._fire_area_handle = ""
        self._fire_area_frame_pending = True
        if area != current:
            self._vehicle_preview_active_edits.clear()
            self._push_undo()
            self._set_fire_area(area)
        else:
            self._sync_all()

    def _fire_area_value_edited(self, field: str, value: float) -> None:
        if self._syncing or self.project.target_category != WEAPON:
            return
        if field == "height":
            self._fire_area_height = float(value)
            self._sync_all()
            return
        area = self.project.fire_area
        shape = self._fire_area_edit_shape()
        if field == "dead_zone":
            new = replace(area, dead_zone=float(value))
        elif field == "max_inclination":
            new = replace(area, max_inclination=float(value))
        else:
            size = area.size(shape) if shape else None
            if size is None:
                return
            index = fa.SIZE_FIELDS[shape][int(field[4:])][1]
            values = list(size)
            values[index] = float(value)
            new = area.with_size(shape, values)
        if new == area:
            return
        self._begin_vehicle_preview_edit(f"fire_area_{field}")
        self._set_fire_area(new)

    def _fire_area_handle_picked(self, key: str) -> None:
        self._fire_area_handle = key
        self._sync_fire_area()

    def _fire_area_drag_started(self, _key: str) -> None:
        shape = self._fire_area_edit_shape()
        area = self.project.fire_area
        size = area.size(shape) if shape else None
        self._fire_area_drag_start = (
            None if size is None else (shape, size, area.dead_zone))
        self._vehicle_preview_active_edits.clear()
        self._push_undo()

    def _fire_area_dragged(self, key: str, delta: float) -> None:
        start = self._fire_area_drag_start
        if start is None or self.project.target_category != WEAPON:
            return
        shape, size, dead_zone = start
        new_size, new_min = fa.dragged(shape, size, dead_zone, key, delta)
        area = self.project.fire_area.with_size(shape, new_size)
        area = replace(area, dead_zone=new_min)
        if area != self.project.fire_area:
            self._set_fire_area(area)

    def _fire_area_nudge(self, direction, step: float) -> None:
        """Move the selected grip with the shared move gizmo (model-space axes)."""

        shape = self._fire_area_edit_shape()
        area = self.project.fire_area
        size = area.size(shape) if shape else None
        handle = next((h for h in fa.volume_handles(
            shape, size, area.dead_zone) if h.key == self._fire_area_handle), None) \
            if size is not None else None
        if handle is None:
            self.statusBar().showMessage(
                "Click a square on the fire volume first, then move it.", 4000)
            return
        delta = step * sum(float(direction[i]) * handle.axis[i] for i in range(3))
        if not delta:
            return
        new_size, new_min = fa.dragged(shape, size, area.dead_zone, handle.key, delta)
        new = replace(area.with_size(shape, new_size), dead_zone=new_min)
        if new == area:
            return
        self._vehicle_preview_active_edits.clear()
        self._push_undo()
        self._set_fire_area(new)

    # -- camera -----------------------------------------------------------------------

    def _fire_area_frame(self, unit_only: bool) -> None:
        """Frame the unit or the whole volume and remember which one is shown."""

        self._fire_area_focus_on_unit = unit_only
        self.viewport.frame_fire_area(unit_only=unit_only)

    def _fire_area_toggle_focus(self) -> None:
        """Double middle click: Focus Unit and Focus Volume, one after the other."""

        if self._is_fire_area_tab_active():
            self._fire_area_frame(unit_only=not self._fire_area_focus_on_unit)

    def _fire_area_enter_leave(self) -> None:
        """Focus the unit the first time the tab opens for a definition.

        The camera of the other tabs is saved and given back by the scene
        preview (``_sync_fx_preview``), which this tab shares with Visuals & FX.
        """

        if self._is_fire_area_tab_active():
            definition = (self._active_script_path, self._active_script_kind,
                          self._active_script_id)
            if definition != self._fire_area_framed_definition:
                self._fire_area_framed_definition = definition
                self._fire_area_frame_pending = True
        else:
            self._fire_area_framed_definition = None

    def _fire_area_aim_chosen(self, aim: str) -> None:
        """Preview-only choice: nothing is written to the script."""

        if self._syncing or aim == self._fire_area_aim:
            return
        self._fire_area_aim = aim
        self._sync_all()

    def _fire_area_unit_chosen(self, vehicle_id) -> None:
        """Preview-only choice: nothing is written to the script."""

        if self._syncing:
            return
        self._fire_area_unit_id = None if vehicle_id is None else int(vehicle_id)
        self._fire_area_reset_height()
        self._sync_all()
