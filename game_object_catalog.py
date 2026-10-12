"""Game prototypes in the same manifest and override order as the engine."""

from dataclasses import dataclass, field
from pathlib import Path
import re

from map_editor.core.script_catalog import script_texts

_HEADER = re.compile(r"(new|modify)_(vehicle|weapon|building)\s+(\d+)\b", re.I)


@dataclass
class GameObject:
    kind: str
    object_id: int
    source: Path
    values: dict[str, str] = field(default_factory=dict)
    bodies: list[str] = field(default_factory=list)

    @property
    def name(self):
        return self.values.get("name", f"{self.kind.title()} {self.object_id}")

    @property
    def effective_text(self):
        return f"new_{self.kind} {self.object_id}\n" + "\n".join(self.bodies) + "\nend\n"

    def number(self, key, default=0):
        try:
            return float(self.values.get(key, str(default)))
        except (ValueError, OverflowError):
            return default


def load_game_objects(installation):
    scripts = installation.folder("scripts")
    objects = {}
    for text, source in script_texts(scripts, with_source=True):
        active = None
        depth = 0
        body = []
        for raw in text.splitlines():
            code = re.split(r";|//|#", raw, maxsplit=1)[0].strip()
            header = _HEADER.fullmatch(code)
            if header and active is None:
                mode, kind, number = header.groups()
                kind = kind.casefold()
                key = kind, int(number)
                if mode.casefold() == "new" or key not in objects:
                    objects[key] = GameObject(kind, key[1], scripts / source)
                active = objects[key]
                depth, body = 0, []
                continue
            if active is None:
                continue
            token = code.split(None, 1)[0].casefold() if code else ""
            if token == "end" and depth == 0:
                active.bodies.append("\n".join(body))
                active = None
                continue
            body.append(raw)
            if token == "begin" or token.startswith("begin_"):
                depth += 1
            elif token == "end":
                depth = max(0, depth - 1)
            elif depth == 0 and "=" in code:
                name, value = code.split("=", 1)
                active.values[name.strip().casefold()] = value.strip().strip('"')
    return sorted(objects.values(), key=lambda obj: (obj.name.casefold(), obj.kind, obj.object_id))
