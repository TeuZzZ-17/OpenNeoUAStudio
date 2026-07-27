"""Lazy, case-insensitive texture catalogue for the Visuals panel."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TextureCatalogEntry:
    name: str
    referenced: bool = False
    reference: object | None = None
    archive_resource: object | None = None

    @property
    def decodable(self) -> bool:
        resource = self.archive_resource
        if resource is None:
            return True
        return bool(getattr(resource, "decodable", False))

    @property
    def status(self) -> str:
        if self.reference is not None:
            return str(getattr(self.reference, "status", "found"))
        resource = self.archive_resource
        if resource is None:
            return "loaded"
        if getattr(resource, "error", ""):
            return "decode failed"
        return "setbas" if self.decodable else "unsupported"


def build_texture_catalog(family, archive=None) -> list[TextureCatalogEntry]:
    """Return family + archive ILBMs without case-only duplicates.

    This function only indexes metadata; it deliberately never decodes an
    archive payload.
    """

    entries: dict[str, TextureCatalogEntry] = {}
    order: list[str] = []

    def merge(name: str, *, referenced=False, reference=None,
              archive_resource=None) -> None:
        if not name:
            return
        key = name.casefold()
        current = entries.get(key)
        if current is None:
            entries[key] = TextureCatalogEntry(
                name=name, referenced=referenced, reference=reference,
                archive_resource=archive_resource)
            order.append(key)
            return
        entries[key] = TextureCatalogEntry(
            name=current.name if current.referenced else name,
            referenced=current.referenced or referenced,
            reference=current.reference or reference,
            archive_resource=current.archive_resource or archive_resource,
        )

    if family is not None:
        for name, reference in getattr(family, "texture_refs", {}).items():
            merge(name, referenced=True, reference=reference)
        for name in getattr(family, "textures", {}):
            merge(name)

    if archive is not None:
        for resource in getattr(archive, "resources", ()):
            if str(getattr(resource, "class_id", "")).casefold() \
                    != "ilbm.class":
                continue
            merge(str(getattr(resource, "resource_name", "")),
                  archive_resource=resource)

    return sorted((entries[key] for key in order),
                  key=lambda entry: entry.name.casefold())
