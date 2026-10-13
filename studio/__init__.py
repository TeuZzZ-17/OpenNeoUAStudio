"""Shared layers of OpenNeoUA Studio (everything the editors have in common).

Dependencies point downwards in this list - a layer does not import one above it:

1. ``studio.exchange.portable`` - Qt-free core (resolver, ONUA3D package); also bundled in Blender
2. ``studio.formats``   - binary file formats of Urban Assault (readers/writers, no UI)
3. ``studio.assets``    - asset families, dependencies, catalogs, reports, VISPROTO, mapping, SET.BAS export
4. ``studio.rendering`` - 3D viewport, indexed renderer, GPU surface, gizmo, geometry editing engine
5. ``studio.ui`` and ``studio.exchange.onua3d`` - shared widgets/tabs/browsers/shell; glTF export/import
6. ``studio.workbench`` - the asset workbench window and the outline editor

The one deliberate exception is a lazy import of the renderer's adapter inside
``studio.assets.family`` (package validation). The editors (``model_editor``, ``entity_designer``,
``snapshot_studio``, ``wireframe_editor``, ``map_editor``) sit on top; a few shared modules still reach into
the editors (listed in ARCHITECTURE.md, "Known couplings"). See ARCHITECTURE.md.
"""
