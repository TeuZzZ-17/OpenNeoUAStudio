"""Entity Designer: edit vehicle and weapon prototypes (collision, fire/gun points, cockpit, FX).

Modules of this package:

* ``editor``          - the main window (``CollisionEditorWindow``) that ties the tabs together
* ``script_model``    - script blocks, collision project data classes, overwrite previews (pure Python)
* ``viewport``        - the 3D viewport with the move gizmo
* ``widgets``         - small widgets (geometry part tree, sphere tree, compact spin box)
* ``dialogs``         - import collision, open script object and apply script dialogs
* ``shape``           - model-derived collision shapes (bake worker)
* ``sphere_generator``, ``mesh_parts`` - automatic collision spheres and 3D mesh splitting
* ``briefing``        - briefing size preview
* ``destruction_fx``, ``fx_panel``, ``fx_viewport``, ``fx_audio`` - the Destruction FX preview tab

Public names are re-exported lazily so ``from entity_designer import CollisionEditorWindow`` keeps
working without importing Qt until the name is really used.
"""

# Searched in this order; ``editor`` first because it imports the others in a known-good order.
_SEARCHED_MODULES = ("editor", "script_model", "viewport", "widgets", "dialogs")


def __getattr__(name):
    """Load the Qt editor only when a public editor name is requested."""

    from importlib import import_module
    from importlib.util import find_spec

    # Python and PySide probe packages for special attributes (``__wrapped__``...), and
    # ``from entity_designer import fx_panel`` asks for a submodule: in both cases let Python
    # answer itself (loading the whole editor here would start a circular import).
    if name.startswith("__") or find_spec(f"{__name__}.{name}") is not None:
        raise AttributeError(name)
    for module_name in _SEARCHED_MODULES:
        module = import_module(f".{module_name}", __name__)
        if hasattr(module, name):
            value = getattr(module, name)
            globals()[name] = value
            return value
    raise AttributeError(name)
