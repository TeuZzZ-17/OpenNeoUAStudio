from __future__ import annotations

from pathlib import Path

from .. import bootstrap

bootstrap.ensure_studio_on_path()

from asset_family import AssetFamily, FamilyObject, load_asset_family  # noqa: E402
from setbas_reader import SetBasArchive, read_setbas  # noqa: E402
from vp_manager import normalize_base_name  # noqa: E402
from sklt_parser import parse_sklt_bytes  # noqa: E402,F401

from .set_sdf_parser import SetSdf, parse_set_sdf  # noqa: E402


class SetAssets:
    """Risorse di un Set nello stesso ordine del gioco: file sciolti, poi SET.BAS."""

    def __init__(self, set_number: int):
        self.set_number = set_number
        self.set_dir = bootstrap.set_dir(set_number)
        self.setbas_path = self._find_setbas()
        self.sdf_path = self._find_sdf()
        self.archive: SetBasArchive | None = None
        self.family: AssetFamily | None = None
        self.sdf: SetSdf | None = None
        self._by_name: dict[str, FamilyObject] = {}
        self._loose: dict[str, FamilyObject | None] = {}
        self._loose_index: dict[str, Path] | None = None
        self._loose_families = {}

    def _find_setbas(self) -> Path | None:
        objects = bootstrap.find_ci(self.set_dir, "objects")
        return bootstrap.find_ci(objects, "set.bas") if objects else None

    def _find_sdf(self) -> Path | None:
        scripts = bootstrap.find_ci(self.set_dir, "scripts")
        return bootstrap.find_ci(scripts, "set.sdf") if scripts else None

    def load(self) -> "SetAssets":
        if self.sdf_path is None:
            raise FileNotFoundError(f"set.sdf missing in {self.set_dir}")
        self.sdf = parse_set_sdf(self.sdf_path)
        if self.setbas_path is not None:
            self.archive = read_setbas(self.setbas_path)
            self.family = load_asset_family(
                self.setbas_path, [], {}, self.archive)
            for obj in self.family.all_objects():
                name = getattr(obj.base_object, "name", "") or ""
                if not name:
                    continue
                self._by_name.setdefault(self._key(name), obj)
        return self

    @staticmethod
    def _key(name: str) -> str:
        name = name.strip()
        if not name.lower().endswith(".base"):
            name += ".base"
        return normalize_base_name(name)

    def _index_loose(self) -> dict[str, Path]:
        if self._loose_index is None:
            index: dict[str, Path] = {}
            roots = [self.set_dir, bootstrap.game_data_dir() / "Models" / "Base"]
            for root in roots:
                if not root.is_dir():
                    continue
                for path in root.rglob("*"):
                    if path.suffix.lower() == ".base":
                        index.setdefault(self._key(path.name), path)
            self._loose_index = index
        return self._loose_index

    def base_object(self, base_name: str, *, set_only: bool = False) -> FamilyObject | None:
        key = self._key(base_name)
        if set_only:
            path = self._index_loose().get(key)
            # Positional VPs belong to the set. Unreferenced Models/Base files
            # can share a name with another visual state, such as Genesis.
            if path is None or not path.is_relative_to(self.set_dir):
                return self._by_name.get(key)
        if key not in self._loose:
            found = None
            path = self._index_loose().get(key)
            if path is not None:
                loose = load_asset_family(
                    path, [self.set_dir], {}, self.archive)
                found = loose.root_object
                if found is not None:
                    self._loose_families[key] = loose
            self._loose[key] = found
        return self._loose[key] or self._by_name.get(key)

    def has_base(self, base_name: str) -> bool:
        return self.base_object(base_name) is not None
