"""Optional persistence of dependency choices (tool-side, never asset-side).

Stores the user's picks for ambiguous references in
``~/.openneouastudio/dependency_choices.json`` so the same theme/texture
does not have to be chosen every session.  The profile NEVER lives next to
game assets and nothing in the original asset tree is ever modified.

A choice identity is deliberately narrow to avoid wrong reuse:
(root base filename, owner node, kind, raw reference) -> chosen path.

Conservative by default: a saved choice is only *offered* (highlighted /
preselected).  It is auto-applied only when the profile's ``auto_apply``
setting is enabled by the user.  Stale choices (file gone) are flagged and
skipped, never fatal.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path

PROFILE_DIR = Path.home() / ".openneouastudio"
PROFILE_PATH = PROFILE_DIR / "dependency_choices.json"
LEGACY_OPENUA_PROFILE_PATH = (Path.home() / ".openuastudio"
                              / "dependency_choices.json")
LEGACY_SKLTRON_PROFILE_PATH = (Path.home() / ".skltron"
                                / "skltron_dependency_choices.json")
PROFILE_VERSION = 1


@dataclass
class SavedChoice:
    root_base: str
    owner_node: str
    kind: str
    raw_ref: str
    chosen_path: str
    source: str = ""
    choice_mode: str = "manual"      # manual | trial | setbas
    created_at: str = ""
    last_used_at: str = ""

    def key(self) -> tuple[str, str, str, str]:
        return (self.root_base.lower(), self.owner_node or "root",
                self.kind, self.raw_ref.lower())

    @property
    def stale(self) -> bool:
        if self.chosen_path.lower().startswith("setbas:"):
            return False
        return not Path(self.chosen_path).is_file()


class DependencyProfile:
    """Load/save wrapper around the JSON profile.  All writes go to the
    tool's own config directory; failures degrade to an in-memory profile."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else PROFILE_PATH
        self.auto_apply = False
        self._choices: dict[tuple, SavedChoice] = {}
        self.load_error: str | None = None
        # One-way compatibility bridge for existing users.  Read the new
        # profile first, then the former OpenUAStudio location, then the
        # older SKLtron profile.  The next successful save always writes only
        # the new path; legacy files are never moved, deleted, or overwritten.
        self._load_path = self.path
        if path is None and not self.path.is_file():
            for legacy_path in (LEGACY_OPENUA_PROFILE_PATH,
                                LEGACY_SKLTRON_PROFILE_PATH):
                if legacy_path.is_file():
                    self._load_path = legacy_path
                    break
        self._load()

    # -- persistence -------------------------------------------------------------

    def _load(self) -> None:
        if not self._load_path.is_file():
            return
        try:
            data = json.loads(self._load_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.load_error = f"profile unreadable ({exc}); starting empty"
            return
        self.auto_apply = bool(data.get("auto_apply", False))
        for raw in data.get("choices", []):
            try:
                choice = SavedChoice(
                    root_base=raw["root_base"],
                    owner_node=raw.get("owner_node", "root"),
                    kind=raw.get("kind", "texture"),
                    raw_ref=raw["raw_ref"],
                    chosen_path=raw["chosen_path"],
                    source=raw.get("source", ""),
                    choice_mode=raw.get("choice_mode", "manual"),
                    created_at=raw.get("created_at", ""),
                    last_used_at=raw.get("last_used_at", ""),
                )
            except KeyError:
                continue
            self._choices[choice.key()] = choice

    def save(self) -> str | None:
        """Write the profile; returns an error string instead of raising."""

        payload = {
            "version": PROFILE_VERSION,
            "auto_apply": self.auto_apply,
            "choices": [
                {
                    "root_base": c.root_base,
                    "owner_node": c.owner_node,
                    "kind": c.kind,
                    "raw_ref": c.raw_ref,
                    "chosen_path": c.chosen_path,
                    "source": c.source,
                    "choice_mode": c.choice_mode,
                    "created_at": c.created_at,
                    "last_used_at": c.last_used_at,
                }
                for c in self._choices.values()
            ],
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(payload, indent=2),
                                 encoding="utf-8")
        except OSError as exc:
            return f"could not write profile: {exc}"
        return None

    # -- API ---------------------------------------------------------------------

    def _key(self, root_base: str, owner_node: str | None, kind: str,
             raw_ref: str) -> tuple:
        return (Path(root_base).name.lower(), owner_node or "root",
                kind, raw_ref.lower())

    def lookup(self, root_base: str, owner_node: str | None, kind: str,
               raw_ref: str) -> SavedChoice | None:
        return self._choices.get(self._key(root_base, owner_node, kind,
                                           raw_ref))

    def remember(self, root_base: str, owner_node: str | None, kind: str,
                 raw_ref: str, chosen_path: str, source: str = "",
                 choice_mode: str = "manual") -> SavedChoice:
        now = datetime.now().isoformat(timespec="seconds")
        choice = SavedChoice(
            root_base=Path(root_base).name, owner_node=owner_node or "root",
            kind=kind, raw_ref=raw_ref, chosen_path=chosen_path,
            source=source, choice_mode=choice_mode,
            created_at=now, last_used_at=now,
        )
        self._choices[choice.key()] = choice
        return choice

    def forget(self, root_base: str, owner_node: str | None, kind: str,
               raw_ref: str) -> bool:
        return self._choices.pop(
            self._key(root_base, owner_node, kind, raw_ref), None
        ) is not None

    def touch(self, choice: SavedChoice) -> None:
        choice.last_used_at = datetime.now().isoformat(timespec="seconds")

    def choices_for(self, root_base: str) -> list[SavedChoice]:
        base = Path(root_base).name.lower()
        return [c for c in self._choices.values()
                if c.root_base.lower() == base]

    def __len__(self) -> int:
        return len(self._choices)


if __name__ == "__main__":
    profile = DependencyProfile()
    print(f"profile: {profile.path}")
    print(f"auto_apply: {profile.auto_apply}")
    print(f"choices: {len(profile)}")
    for choice in profile._choices.values():
        marker = " [STALE]" if choice.stale else ""
        print(f"  {choice.root_base} / {choice.owner_node} / {choice.kind} "
              f"/ {choice.raw_ref} -> {choice.chosen_path}{marker}")
