"""Find the other visual states of the model open in the Model Editor.

A state can be known in different ways, so all of them are asked and merged:

  - Entity List: the game scripts name the states of every vehicle, weapon and
    building. The open model is looked up there, so it does not need to be selected.
  - Folder: models imported from outside are not in any script, but states are saved
    side by side with the state in the name (Kyt_Flak_Fire, Kyt_Flak_Normal).
  - Bas Manager: the same naming rule, on the objects inside the open SET.BAS.
  - Similar shape: other BASE files nearby that are clearly the same model, for
    names that follow no rule at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re

from studio.assets.visproto import normalize_base_name

STATE_WORDS = ("normal", "fire", "wait", "dead", "megadeth", "genesis", "create")
_STATE_TOKEN = re.compile(r"(?<![a-z0-9])(" + "|".join(STATE_WORDS) + r")(?![a-z0-9])", re.I)
# A folder search must stay near the model: one game holds thousands of BASE files.
MAX_NEARBY_MODELS = 24

ENTITY_LIST, FOLDER, BAS_MANAGER, SIMILAR = "Entity List", "Folder", "Bas Manager", "Similar shape"


@dataclass
class StateCandidate:
    """One other state of the open model."""

    label: str
    path: Path
    base_name: str = ""                # object inside SET.BAS; empty for a loose BASE
    found_by: list[str] = field(default_factory=list)

    @property
    def state(self) -> str:
        return state_word(self.label) or state_word(self.path.stem)

    @property
    def writable(self) -> bool:
        return not self.base_name and self.path.suffix.casefold() in (".base", ".bas")

    @property
    def key(self) -> str:
        # Scripts write 'VP_X.base', the archive objects are called 'VP_X': one key for both.
        return (normalize_base_name(self.base_name) if self.base_name
                else str(self.path.resolve()).casefold())


def state_word(name: str) -> str:
    """The state named in a file or object name, or an empty string."""
    match = _STATE_TOKEN.search(name.replace("_", " "))
    return match[1].title() if match else ""


def family_key(name: str) -> str | None:
    """The name without its state word: every state of one model has the same key."""
    words = name.replace("_", " ")
    if _STATE_TOKEN.search(words) is None:
        return None
    return _STATE_TOKEN.sub("*", words).casefold()


def _model_files(root: Path, depth: int) -> list[Path]:
    found, level = [], [root]
    for _ in range(depth + 1):
        below = []
        for folder in level:
            try:
                children = sorted(folder.iterdir())
            except OSError:
                continue
            found += [path for path in children if path.is_file()
                      and path.suffix.casefold() == ".base"]
            below += [path for path in children if path.is_dir()]
        level = below
    return found


def _search_root(base_path: Path) -> Path:
    """Where the states of a model live.

    The usual layout gives every state its own folder inside the model's folder
    (Kyt_Flak_01/Kyt_Flak_Fire/Kyt_Flak_Fire.BASE), so that one is searched.
    """
    folder = base_path.parent
    return folder.parent if folder.name.casefold() == base_path.stem.casefold() else folder


def states_by_name(base_path: Path) -> list[StateCandidate]:
    key = family_key(base_path.stem)
    if key is None:
        return []
    return [StateCandidate(state_word(path.stem), path, found_by=[FOLDER])
            for path in _model_files(_search_root(base_path), 2)
            if path.resolve() != base_path.resolve() and family_key(path.stem) == key]


def nearby_models(base_path: Path, known: set[str]) -> list[Path]:
    """BASE files around the model that no rule explained, to compare by shape."""
    return [path for path in _model_files(_search_root(base_path), 2)
            if path.resolve() != base_path.resolve()
            and str(path.resolve()).casefold() not in known][:MAX_NEARBY_MODELS]


def states_in_archive(family, current_name: str) -> list[StateCandidate]:
    key = family_key(current_name)
    if key is None or family.setbas_archive is None:
        return []
    found = []
    for obj in family.all_objects():
        name = getattr(obj.base_object, "name", "") or ""
        if name and name.casefold() != current_name.casefold() and family_key(name) == key:
            found.append(StateCandidate(state_word(name), Path(family.base_path), name, [BAS_MANAGER]))
    return found


def states_in_entities(install, set_number, base_path, base_name) -> list[StateCandidate]:
    """Every state of the game objects that use the open model."""
    from studio.assets import catalog
    from studio.exchange.portable import same_path

    def is_current(choice):
        if base_name and choice.base_name:
            return normalize_base_name(choice.base_name) == normalize_base_name(base_name)
        return (not choice.base_name and base_path is not None
                and same_path(choice.path, base_path))

    found = []
    for game_object in catalog.load_game_objects(install):
        try:
            choices = catalog.visual_choices(game_object, set_number)
        except (OSError, ValueError, KeyError):
            continue
        if any(is_current(choice) for choice in choices):
            found += [StateCandidate(choice.label, Path(choice.path), choice.base_name,
                                     [ENTITY_LIST])
                      for choice in choices if not is_current(choice)]
    return found


def merge_states(*groups) -> list[StateCandidate]:
    """One row per real file or object, remembering every way it was found."""
    merged: dict[str, StateCandidate] = {}
    for group in groups:
        for candidate in group:
            present = merged.get(candidate.key)
            if present is None:
                merged[candidate.key] = candidate
            else:
                present.found_by += [way for way in candidate.found_by
                                     if way not in present.found_by]
    return list(merged.values())


def discover_states(family, fam_obj, install, set_number) -> list[StateCandidate]:
    base_path = Path(family.base_path) if family.base_path is not None else None
    in_archive = base_path is not None and base_path.name.casefold() == "set.bas"
    base_name = (getattr(fam_obj.base_object, "name", "") or "") if in_archive else ""
    groups = []
    if install is not None and set_number in install.available_sets():
        groups.append(states_in_entities(install, set_number, None if in_archive else base_path,
                                         base_name))
    if in_archive:
        groups.append(states_in_archive(family, base_name))
    elif base_path is not None:
        groups.append(states_by_name(base_path))
    return merge_states(*groups)
