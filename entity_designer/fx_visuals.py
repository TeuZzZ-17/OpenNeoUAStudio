"""Model states (VP / BASE / 3DS) of a vehicle or weapon definition as script keys.

The "Base data" list of the Visuals & FX tab shows and edits ``vp_<state>``,
``base_<state>`` and ``3ds_<state>`` of the previewed definition. Pure Python:
reading and patching text, and one verified write that keeps encoding and every
untouched byte.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from entity_designer.script_model import (
    _GENERIC_PARAM_RE, _top_level_assignment_rows, find_script_blocks,
    read_script_file, write_script_file,
)
from studio.exchange.portable import commit_verified_files

# (state, label) in the order the list shows them.
STATES = (
    ("normal", "Normal"), ("fire", "Fire"), ("wait", "Wait"),
    ("dead", "Dead"), ("megadeth", "Megadeth"), ("genesis", "Genesis"),
)
# VP is a number, BASE and 3DS are Data/... paths.
KINDS = (("vp", "VP"), ("base", "BASE"), ("3ds", "3DS"))

_FORBIDDEN_IN_PATH = re.compile(r"[;#\r\n=]")


def state_keys(state: str) -> tuple[str, str, str]:
    return tuple(f"{kind}_{state}" for kind, _label in KINDS)


def all_keys() -> set[str]:
    return {key for state, _label in STATES for key in state_keys(state)}


def block_for_origin(text: str, origin):
    """The definition of ``text`` that ends where the origin's span ends."""

    _path, first, last = origin
    blocks = find_script_blocks(text)
    # The span start is stable when effects inside the block are saved; its end is not.
    for block in blocks:
        if block.start_line <= first <= block.end_line:
            return block
    for block in blocks:
        if block.start_line <= last <= block.end_line:
            return block
    return None


def read_values(text: str, block) -> dict[str, str]:
    """Active top-level visual keys of one block; the last assignment wins."""

    keys = all_keys()
    values: dict[str, str] = {}
    for _line, key, value, _indent in _top_level_assignment_rows(text, block):
        if key in keys:
            values[key] = value.strip().strip('"')
    return values


def read_effective_values(origins) -> dict[str, str]:
    """Visual keys over every script part of the definition (``modify_*`` wins)."""

    values: dict[str, str] = {}
    for origin in origins:
        try:
            text, _encoding, _bom = read_script_file(origin[0])
        except (OSError, UnicodeError):
            continue
        block = block_for_origin(text, origin)
        if block is not None:
            values.update(read_values(text, block))
    return values


def normalize_value(key: str, raw: str) -> str:
    """The text written to the script for one field; "" removes the key."""

    value = raw.strip().strip('"')
    if not value:
        return ""
    if key.startswith("vp_"):
        try:
            number = int(value, 0)
        except ValueError as exc:
            raise ValueError(f"{key} must be a whole number.") from exc
        if number < 0:
            raise ValueError(f"{key} cannot be negative.")
        return str(number)
    value = value.replace("\\", "/")
    if _FORBIDDEN_IN_PATH.search(value):
        raise ValueError(f"{key}: the path cannot contain ; # = or line breaks.")
    return value


def patch_values(text: str, block, updates: dict[str, str]) -> str:
    """Set (or, for "", remove) top-level keys of ``block`` without touching the rest."""

    newline = "\r\n" if "\r\n" in text else "\n"
    final_newline = text.endswith(("\n", "\r"))
    lines = text.splitlines()
    end_line = block.end_line
    for key, value in updates.items():
        rows = [(index, indent) for index, row_key, _value, indent
                in _top_level_assignment_rows(
                    newline.join(lines) + newline, _Span(block.start_line, end_line))
                if row_key == key]
        if rows and value:
            # Only the effective (last) row changes; its comment stays.
            index = rows[-1][0]
            match = _GENERIC_PARAM_RE.match(lines[index])
            raw = match.group("value")
            padding = raw[len(raw.rstrip()):]
            lines[index] = (lines[index][:match.start("value")] + value + padding
                            + lines[index][match.end("value"):])
        elif rows:
            for index, _indent in sorted(rows, reverse=True):
                del lines[index]
                end_line -= 1
        elif value:
            at, indent = _insert_position(lines, block, end_line, key)
            lines.insert(at, f"{indent}{key} = {value}")
            end_line += 1
    return newline.join(lines) + (newline if final_newline else "")


class _Span:
    """The part of ``find_script_blocks`` that ``_top_level_assignment_rows`` reads."""

    def __init__(self, start_line: int, end_line: int) -> None:
        self.start_line = start_line
        self.end_line = end_line


def _insert_position(lines, block, end_line: int, key: str):
    """New keys go after the last visual key of the block, else before its ``end``."""

    text = "\n".join(lines) + "\n"
    rows = list(_top_level_assignment_rows(text, _Span(block.start_line, end_line)))
    visual = [row for row in rows if row[1].split("_", 1)[0] in {"vp", "base", "3ds"}
              and row[1].split("_", 1)[-1] in {state for state, _l in STATES}]
    if visual:
        return visual[-1][0] + 1, visual[-1][3]
    if rows:
        return end_line, rows[-1][3]
    return end_line, "        "


def save_values(origins, updates: dict[str, str], snapshots=None) -> Path:
    """Write ``updates`` into the last script part of the definition and verify it.

    ``snapshots`` maps a path to the bytes the editor loaded: a file changed
    meanwhile is never overwritten.
    """

    if not origins:
        raise ValueError("No script source is attached to this definition.")
    origin = origins[-1]
    path = Path(origin[0])
    if snapshots is not None and path in snapshots and path.read_bytes() != snapshots[path]:
        raise ValueError(
            f"{path.name} changed outside the editor. No file was overwritten. "
            "Reload the definition and try again.")
    text, encoding, bom = read_script_file(path)
    block = block_for_origin(text, origin)
    if block is None or not block.complete:
        raise ValueError("The definition was not found in its script file.")
    clean = {key: normalize_value(key, value) for key, value in updates.items()}
    patched = patch_values(text, block, clean)
    if patched == text:
        return path
    with tempfile.TemporaryDirectory(prefix="onua_visuals_") as directory:
        stage = Path(directory) / "visuals.cfg"
        write_script_file(stage, patched, encoding, bom)
        expected = stage.read_bytes()

        def verify():
            if path.read_bytes() != expected:
                raise ValueError(f"Saved script verification failed: {path.name}")

        commit_verified_files([(stage, path)], verify=verify)
    return path


# -- the "Model" row of the effects list ------------------------------------------------------

MODEL_COLOR = "#a58cf0"
# Virtual list index of the single Model row (-1 means "no effect").
MODEL_INDEX = -2


def model_text(values: dict[str, str]) -> str:
    """The editable lines of every state, one blank line between states."""

    groups = []
    for state, _label in STATES:
        lines = [f"{key} = {values[key]}" for key in state_keys(state) if values.get(key)]
        if lines:
            groups.append("\n".join(lines))
    return "\n\n".join(groups)


def model_summary(values: dict[str, str]) -> str:
    """One line per state that has a model, in the order of the text."""

    parts = []
    for state, label in STATES:
        vp_key, base_key, mesh_key = state_keys(state)
        shown = (f"3DS {values[mesh_key]}" if values.get(mesh_key)
                 else f"BASE {values[base_key]}" if values.get(base_key)
                 else f"VP {values[vp_key]}" if values.get(vp_key) else "")
        if shown:
            parts.append(f"{label}: {shown}")
    return "\n".join(parts) or "No model"


def parse_model_text(text: str) -> dict[str, str]:
    """Check an edited Model text; returns every state key ("" = not set)."""

    keys = all_keys()
    updates = {key: "" for key in keys}
    for number, line in enumerate(text.splitlines(), 1):
        code = re.split(r";|//|#", line, maxsplit=1)[0].strip()
        if not code:
            continue
        key, separator, value = code.partition("=")
        key = key.strip().lower()
        if not separator or key not in keys:
            raise ValueError(
                f"Line {number}: only vp_, base_ and 3ds_ keys of the model states "
                f"(normal, fire, wait, dead, megadeth, genesis) can be written here.")
        updates[key] = normalize_value(key, value)
    return updates


class StateSession:
    """Draft and saving of the Model row, like ``FxTextSession`` does for effects."""

    def __init__(self, origins) -> None:
        self.origins = [(Path(path), first, last) for path, first, last in origins]
        self.source_error = ""
        self.snapshots: dict[Path, bytes] = {}
        for path, _first, _last in self.origins:
            try:
                self.snapshots[path] = path.read_bytes()
            except OSError as exc:
                self.source_error = str(exc)
        self.values = read_effective_values(self.origins) if self.origins else {}
        self.originals = ({MODEL_INDEX: model_text(self.values)}
                          if self.origins else {})
        self.drafts = dict(self.originals)

    def indexes(self) -> list[int]:
        return list(self.originals)

    def is_dirty(self, index: int) -> bool:
        return self.drafts.get(index, "") != self.originals.get(index, "")

    @property
    def dirty(self) -> bool:
        return any(self.is_dirty(index) for index in self.originals)

    def set_draft(self, index: int, text: str):
        """Validate a draft; the preview of the models changes when it is saved."""

        self.drafts[index] = text
        parse_model_text(text)
        return None, []

    def revert(self, index: int) -> None:
        self.drafts[index] = self.originals[index]

    def save(self, indexes) -> set[Path]:
        indexes = [index for index in indexes if index in self.originals and self.is_dirty(index)]
        if not indexes:
            return set()
        if self.source_error or not self.origins:
            raise ValueError(self.source_error or "No script source is attached to this definition.")
        updates = parse_model_text(self.drafts[MODEL_INDEX])
        path = save_values(self.origins, updates, self.snapshots)
        self.snapshots[path] = path.read_bytes()
        self.values = read_effective_values(self.origins)
        self.originals[MODEL_INDEX] = model_text(self.values)
        self.drafts[MODEL_INDEX] = self.originals[MODEL_INDEX]
        return {path}

    def refresh_snapshots(self, paths) -> None:
        """Another save (effect code) changed these files; their keys are still ours."""

        for path in paths:
            path = Path(path)
            if path in self.snapshots:
                self.snapshots[path] = path.read_bytes()
