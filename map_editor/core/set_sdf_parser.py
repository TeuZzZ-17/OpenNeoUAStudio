from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SdfBuildingModel:
    """Sezione 1: modello edificio. L'ID e' l'indice di riga (0-based)."""
    id: int
    base: str
    sklt: str
    numbers: tuple[int, ...]
    effects: tuple[tuple[int, int, int, int], ...]
    comment: str = ""

    @property
    def collision_sklt(self) -> str:
        return self.sklt

    @property
    def hp(self) -> int:
        return self.numbers[2] if len(self.numbers) > 2 else 0


@dataclass
class SdfBuilding:
    """Sezione 2: 4 stati dell'edificio + flag distruttibile."""
    id: int
    intact: int
    damaged: int
    heavy: int
    flat: int
    destructible: int
    comment: str = ""

    def state(self, index: int) -> int:
        return (self.intact, self.damaged, self.heavy, self.flat)[index]


@dataclass
class SdfSector:
    """Sezione 3: settore. `number` e' il primo numero della riga."""
    number: int
    kind: int
    ground: int
    symbol: int
    subsects: tuple[int, ...]
    comment: str = ""

    @property
    def single(self) -> bool:
        return self.kind == 1


@dataclass
class SetSdf:
    path: Path | None = None
    models: list[SdfBuildingModel] = field(default_factory=list)
    buildings: list[SdfBuilding] = field(default_factory=list)
    sectors: dict[int, SdfSector] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def decode_sdf(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _split_comment(line: str) -> tuple[str, str]:
    body, _, comment = line.partition(";")
    return body.strip(), comment.strip()


def _ints(tokens: list[str], where: str, warnings: list[str]) -> list[int]:
    out = []
    for tok in tokens:
        try:
            out.append(int(tok))
        except ValueError:
            warnings.append(f"{where}: non-numeric token {tok!r}")
            break
    return out


def parse_set_sdf_text(text: str, path: Path | None = None) -> SetSdf:
    sdf = SetSdf(path=path)
    section = 0
    for lineno, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if stripped.startswith(">"):
            section += 1
            continue
        body, comment = _split_comment(raw)
        if not body:
            continue
        tokens = body.split()
        where = f"line {lineno}"
        if section == 0:
            if len(tokens) < 6:
                sdf.warnings.append(f"{where}: section 1 is too short")
                continue
            nums = _ints(tokens[2:], where, sdf.warnings)
            head, tail = tuple(nums[:4]), nums[4:]
            if len(tail) % 4:
                sdf.warnings.append(f"{where}: effect values must be a multiple of 4")
                tail = tail[: len(tail) - len(tail) % 4]
            fx = tuple(tuple(tail[i:i + 4]) for i in range(0, len(tail), 4))
            sdf.models.append(SdfBuildingModel(
                len(sdf.models), tokens[0], tokens[1], head, fx, comment))
        elif section == 1:
            nums = _ints(tokens, where, sdf.warnings)
            if len(nums) < 5:
                sdf.warnings.append(f"{where}: section 2 requires 5 numbers")
                continue
            sdf.buildings.append(SdfBuilding(
                len(sdf.buildings), *nums[:5], comment=comment))
        elif section == 2:
            nums = _ints(tokens, where, sdf.warnings)
            if len(nums) < 5:
                sdf.warnings.append(f"{where}: section 3 is too short")
                continue
            number, kind, ground, symbol = nums[:4]
            count = 1 if kind == 1 else 9
            subs = tuple(nums[4:4 + count])
            if len(subs) < count:
                sdf.warnings.append(
                    f"{where}: sector {number} has {len(subs)}/{count} sub")
                subs = subs + (0,) * (count - len(subs))
            if number in sdf.sectors:
                sdf.warnings.append(f"{where}: sector {number} is duplicated")
            sdf.sectors[number] = SdfSector(
                number, kind, ground, symbol, subs, comment)
    if section < 3:
        sdf.warnings.append(f"expected 3 sections, found {section} separators")
    return sdf


def parse_set_sdf(path: str | Path) -> SetSdf:
    path = Path(path)
    return parse_set_sdf_text(decode_sdf(path.read_bytes()), path)
