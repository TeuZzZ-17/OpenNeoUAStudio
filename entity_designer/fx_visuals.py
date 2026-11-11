"""Model states, ground decal and crater of a vehicle or weapon definition as script keys.

The Visuals & FX list shows and edits ``vp_<state>``, ``base_<state>`` and
``3ds_<state>`` (Model row), ``ground_decal_id`` / ``ground_decal_size``
(Ground decal row) and ``crater_radius`` / ``crater_depth`` /
``crater_rim`` (Crater row) of the previewed definition. Pure Python:
reading and patching text, and one verified write that keeps encoding and every
untouched byte.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from entity_designer.script_model import (
    _GENERIC_PARAM_RE, _top_level_assignment_rows, find_script_blocks,
    read_script_file, validate_definition_source, write_script_file,
)
from studio.exchange.portable import commit_verified_files
from entity_designer import crater_preview

# (state, label) in the order the list shows them.
STATES = (
    ("normal", "Normal"), ("fire", "Fire"), ("wait", "Wait"),
    ("dead", "Dead"), ("megadeth", "Megadeth"), ("genesis", "Genesis"),
)
# VP is a number, BASE and 3DS are Data/... paths.
KINDS = (("vp", "VP"), ("base", "BASE"), ("3ds", "3DS"))

_FORBIDDEN_IN_PATH = re.compile(r"[;#\r\n=]")

DECAL_ID_KEY = "ground_decal_id"
DECAL_KEY = "ground_decal_size"
DECAL_KEYS = (DECAL_ID_KEY, DECAL_KEY)
# Largest decal diameter the engine accepts.
DECAL_SIZE_MAX = 12000.0


def state_keys(state: str) -> tuple[str, str, str]:
    return tuple(f"{kind}_{state}" for kind, _label in KINDS)


def all_keys() -> set[str]:
    return {key for state, _label in STATES for key in state_keys(state)}


CRATER_KEYS = crater_preview.KEYS


def managed_keys() -> set[str]:
    """Every key the list rows read and write."""

    return all_keys() | set(DECAL_KEYS) | set(CRATER_KEYS)


def block_for_origin(text: str, origin):
    """Find the definition at the validated source header."""

    _path, first, _last = origin
    return next((block for block in find_script_blocks(text) if block.start_line == first), None)


def read_values(text: str, block) -> dict[str, str]:
    """Active top-level visual keys of one block; the last assignment wins."""

    keys = managed_keys()
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
    if key == DECAL_ID_KEY:
        try:
            number = int(value, 0)
        except ValueError as exc:
            raise ValueError(f"{key} must be a whole number (-1 = no decal).") from exc
        if number < -1:
            raise ValueError(f"{key} must be -1 (no decal) or a profile number.")
        return str(number)
    if key == DECAL_KEY:
        from entity_designer.destruction_fx import parse_float_range

        size = parse_float_range(value)
        if size is None or not all(0.0 <= number <= DECAL_SIZE_MAX for number in size):
            raise ValueError(
                f"{key} must be a number or min~max between 0 and {DECAL_SIZE_MAX:g}.")
        return value
    if key in CRATER_KEYS:
        crater_preview.parse_range(key, value)
        return value
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
    if key == DECAL_KEY:
        # The size goes right after the decal profile it belongs to.
        profile = [row for row in rows if row[1] == "ground_decal_id"]
        if profile:
            return profile[-1][0] + 1, profile[-1][3]
    if key in CRATER_KEYS:
        # The crater keys stay together, after the decal they go with.
        near = [row for row in rows if row[1] in CRATER_KEYS or row[1] in DECAL_KEYS]
        if near:
            return near[-1][0] + 1, near[-1][3]
    visual = [row for row in rows if row[1].split("_", 1)[0] in {"vp", "base", "3ds"}
              and row[1].split("_", 1)[-1] in {state for state, _l in STATES}]
    if visual:
        return visual[-1][0] + 1, visual[-1][3]
    if rows:
        return end_line, rows[-1][3]
    return end_line, "        "


def save_values(origins, updates: dict[str, str], snapshots=None, expected=None) -> Path:
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
    if expected is not None and (block.kind.split("_", 1)[-1], block.object_id) != (
            expected[0].split("_", 1)[-1], expected[1]):
        raise ValueError("The source points to a different definition. Reload before saving.")
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

# Virtual list indexes of the Model, Ground decal and Crater rows (-1 means "no effect").
MODEL_INDEX = -2
DECAL_INDEX = -3
CRATER_INDEX = -4
STATE_INDEXES = (MODEL_INDEX, DECAL_INDEX, CRATER_INDEX)
DELETABLE_KEYS = {DECAL_INDEX: DECAL_KEYS, CRATER_INDEX: CRATER_KEYS}


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


def decal_text(values: dict[str, str]) -> str:
    return "\n".join(f"{key} = {values[key]}" for key in DECAL_KEYS if values.get(key))


def decal_summary(values: dict[str, str]) -> str:
    parts = [f"{label} {values[key]}" for key, label in
             ((DECAL_ID_KEY, "ID"), (DECAL_KEY, "Size")) if values.get(key)]
    return " · ".join(parts) or "Not set"


def parse_decal_text(text: str) -> dict[str, str]:
    """Check an edited Ground decal text; returns its keys ("" = not set)."""

    updates = {key: "" for key in DECAL_KEYS}
    for number, line in enumerate(text.splitlines(), 1):
        code = re.split(r";|//|#", line, maxsplit=1)[0].strip()
        if not code:
            continue
        key, separator, value = code.partition("=")
        key = key.strip().lower()
        if not separator or key not in DECAL_KEYS:
            raise ValueError(
                f"Line {number}: only {DECAL_ID_KEY} and {DECAL_KEY} can be written here.")
        updates[key] = normalize_value(key, value)
    return updates


def crater_text(values: dict[str, str]) -> str:
    return "\n".join(f"{key} = {values[key]}" for key in CRATER_KEYS if values.get(key))


def crater_summary(values: dict[str, str]) -> str:
    if not values.get(crater_preview.RADIUS_KEY) and not values.get(crater_preview.DEPTH_KEY):
        return "Not set"
    parts = [f"{label} {values[key]}" for key, label in
             ((crater_preview.RADIUS_KEY, "Radius"), (crater_preview.DEPTH_KEY, "Depth"),
              (crater_preview.RIM_KEY, "Rim %")) if values.get(key)]
    if crater_preview.config_from_values(values) is None:
        parts.append("(no crater: needs radius and depth)")
    return " · ".join(parts)


def parse_crater_text(text: str) -> dict[str, str]:
    """Check an edited Crater text; returns its keys ("" = not set)."""

    updates = {key: "" for key in CRATER_KEYS}
    for number, line in enumerate(text.splitlines(), 1):
        code = re.split(r";|//|#", line, maxsplit=1)[0].strip()
        if not code:
            continue
        key, separator, value = code.partition("=")
        key = key.strip().lower()
        if not separator or key not in CRATER_KEYS:
            raise ValueError(
                f"Line {number}: only {', '.join(CRATER_KEYS)} can be written here.")
        updates[key] = normalize_value(key, value)
    return updates


# A state row registers its keys and parser together. Add uses this same
# registry to route drafts without hard-coding each type in the editor.
_ROW_HANDLERS = {
    MODEL_INDEX: (all_keys, parse_model_text),
    DECAL_INDEX: (lambda: DECAL_KEYS, parse_decal_text),
    CRATER_INDEX: (lambda: CRATER_KEYS, parse_crater_text),
}
_PARSERS = {index: handler[1] for index, handler in _ROW_HANDLERS.items()}


def state_index_for_key(key: str, available) -> int | None:
    return next((index for index in available
                 if index in _ROW_HANDLERS and key.casefold() in _ROW_HANDLERS[index][0]()), None)


def split_state_text(text: str, available) -> dict[int, str] | None:
    """Group the lines of a new draft by the state row that owns their keys.

    Returns ``{row index: its lines}`` in order of appearance, or None when the
    draft has no state key at all. A key that no row of ``available`` owns raises
    ValueError: state keys cannot share an effect with anything else. Comments
    and blank lines follow the key line before them.
    """
    from entity_designer.destruction_fx import engine_tokens

    groups: dict[int, list[str]] = {}
    current = None
    lead: list[str] = []
    foreign = None
    for line in text.splitlines():
        tokens = engine_tokens(line)
        if tokens is None:
            (groups[current] if current is not None else lead).append(line)
            continue
        owner = state_index_for_key(tokens[0], available)
        if owner is None:
            foreign = foreign or tokens[0]
            continue
        current = owner
        groups.setdefault(owner, lead[:] if not groups else []).append(line)
        lead = []
    if not groups:
        return None
    if foreign:
        raise ValueError(
            f"{foreign} cannot share an effect with Model, Ground decal or Crater keys. "
            "Write each kind in its own effect.")
    return {index: "\n".join(lines) for index, lines in groups.items()}


def merge_new_state_text(index: int, text: str, original: str) -> str:
    """Preserve existing fields that the new draft does not mention."""
    from entity_designer.destruction_fx import engine_tokens

    _PARSERS[index](text)
    current = _PARSERS[index](original)
    mentioned = {tokens[0].casefold() for line in text.splitlines()
                 if (tokens := engine_tokens(line))}
    kept = [f"{key} = {value}" for key, value in current.items()
            if value and key not in mentioned]
    separator = "\n" if kept and text and not text.endswith(("\n", "\r")) else ""
    return text + separator + "\n".join(kept)


class StateSession:
    """Draft and saving of the Model, Ground decal and Crater rows, like ``FxTextSession`` does for effects.

    """

    def __init__(self, origins, expected=None) -> None:
        self.origins = [(Path(path), first, last) for path, first, last in origins]
        self.source_error = ""
        self.snapshots: dict[Path, bytes] = {}
        for path, first, last in self.origins:
            try:
                self.snapshots[path] = path.read_bytes()
                text, _encoding, _bom = read_script_file(path)
                identity = validate_definition_source(text, first, last, expected)
                if expected is None:
                    expected = identity
            except (OSError, UnicodeError, ValueError) as exc:
                self.source_error = str(exc)
        self.identity = expected
        self.values = read_effective_values(self.origins) if self.origins and not self.source_error else {}
        self.originals = self._texts() if self.origins else {}
        self.drafts = dict(self.originals)

    def _texts(self) -> dict[int, str]:
        return {MODEL_INDEX: model_text(self.values), DECAL_INDEX: decal_text(self.values),
                CRATER_INDEX: crater_text(self.values)}

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
        _PARSERS[index](text)
        return None, []

    def revert(self, index: int) -> None:
        self.drafts[index] = self.originals[index]

    def save(self, indexes) -> set[Path]:
        indexes = [index for index in indexes if index in self.originals and self.is_dirty(index)]
        if not indexes:
            return set()
        if self.source_error or not self.origins:
            raise ValueError(self.source_error or "No script source is attached to this definition.")
        updates: dict[str, str] = {}
        for index in indexes:
            updates.update(_PARSERS[index](self.drafts[index]))
        path = save_values(self.origins, updates, self.snapshots, self.identity)
        self.snapshots[path] = path.read_bytes()
        self.values = read_effective_values(self.origins)
        texts = self._texts()
        for index in indexes:
            self.originals[index] = texts[index]
            self.drafts[index] = texts[index]
        return {path}

    def refresh_snapshots(self, paths, origins=None) -> None:
        """Another save (effect code) changed these files; their keys are still ours."""

        if origins is not None:
            self.origins = list(origins)
        for path in paths:
            path = Path(path)
            if path in self.snapshots:
                self.snapshots[path] = path.read_bytes()
