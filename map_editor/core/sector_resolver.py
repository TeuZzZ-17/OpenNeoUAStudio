from __future__ import annotations

from dataclasses import dataclass, field

from .set_sdf_parser import SdfBuilding, SdfBuildingModel, SdfSector, SetSdf

SECTOR_SIZE = 1200
# Il quarto passo del settore ospita i raccordi tra terreni vicini.
SUB_SIZE = SECTOR_SIZE // 4

STATE_INTACT, STATE_DAMAGED, STATE_HEAVY, STATE_FLAT = range(4)


@dataclass
class ResolvedSub:
    index: int
    building_id: int
    model_id: int
    model: SdfBuildingModel | None
    building: SdfBuilding | None

    @property
    def col(self) -> int:
        return self.index % 3

    @property
    def row(self) -> int:
        return self.index // 3

    @property
    def offset(self) -> tuple[int, int]:
        return (self.col - 1) * SUB_SIZE, (1 - self.row) * SUB_SIZE

    @property
    def base_name(self) -> str:
        return self.model.base if self.model else ""

    @property
    def sklt_name(self) -> str:
        return self.model.sklt if self.model else ""


@dataclass
class ResolvedSector:
    typ: int
    sector: SdfSector | None
    subs: list[ResolvedSub] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def base_names(self) -> list[str]:
        return [s.base_name for s in self.subs if s.model]


class SectorResolver:
    def __init__(self, sdf: SetSdf):
        self.sdf = sdf

    def resolve_building(self, building_id: int, state: int = STATE_INTACT,
                         index: int = 4) -> ResolvedSub:
        building = (self.sdf.buildings[building_id]
                    if 0 <= building_id < len(self.sdf.buildings) else None)
        model_id = building.state(state) if building else -1
        model = (self.sdf.models[model_id]
                 if 0 <= model_id < len(self.sdf.models) else None)
        return ResolvedSub(index, building_id, model_id, model, building)

    def resolve_typ(self, typ: int, state: int = STATE_INTACT) -> ResolvedSector:
        sector = self.sdf.sectors.get(typ)
        result = ResolvedSector(typ, sector)
        if sector is None:
            result.warnings.append(f"typ {typ:#04x} missing from set.sdf")
            return result
        if sector.single:
            result.subs.append(self.resolve_building(sector.subsects[0], state, 4))
        else:
            for i, bid in enumerate(sector.subsects):
                result.subs.append(self.resolve_building(bid, state, i))
        for sub in result.subs:
            if sub.building is None:
                result.warnings.append(
                    f"sub {sub.index}: building {sub.building_id} does not exist")
            elif sub.model is None:
                result.warnings.append(
                    f"sub {sub.index}: model {sub.model_id} does not exist")
        return result
