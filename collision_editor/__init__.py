"""Public package for OpenNeoUAStudio's Collision Editor.

The implementation lives in :mod:`collision_editor.editor`; public names are
re-exported here so existing imports such as ``from collision_editor import
CollisionEditorWindow`` keep working.
"""

def __getattr__(name):
    """Load the Qt editor only when a public editor name is requested."""

    from importlib import import_module

    editor = import_module(".editor", __name__)
    try:
        value = getattr(editor, name)
    except AttributeError:
        raise AttributeError(name) from None
    globals()[name] = value
    return value
