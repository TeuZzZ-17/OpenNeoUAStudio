from __future__ import annotations

import re

from .. import bootstrap

# Valori vanilla di World.ini; le personalizzazioni del gioco hanno precedenza.
DEFAULT_OWNER_RGB = {
    0: (0, 0, 0), 1: (0, 110, 194), 2: (0, 179, 66),
    3: (232, 232, 232), 4: (255, 171, 28), 5: (73, 73, 73),
    6: (255, 0, 0), 7: (0, 0, 0),
}


def load_owner_colors():
    colors = dict(DEFAULT_OWNER_RGB)
    path = bootstrap.find_ci(bootstrap.game_data_dir(), "World.ini")
    if path is None:
        return colors
    in_colors = False
    for line in path.read_text(encoding="cp1252").splitlines():
        body = line.split(";", 1)[0].strip().lower()
        if body == "begin_colors":
            in_colors = True
        elif body == "end":
            in_colors = False
        elif in_colors:
            match = re.fullmatch(r"owner_([0-7])\s*=\s*(\d+)_(\d+)_(\d+)(?:_\d+)?", body)
            if match:
                owner, red, green, blue = map(int, match.groups())
                if all(0 <= channel <= 255 for channel in (red, green, blue)):
                    colors[owner] = (red, green, blue)
    return colors
