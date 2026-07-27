"""Signal-safe filtering helpers for the hierarchical Assets tree."""

from __future__ import annotations

from collections.abc import Callable


def iter_tree_items(tree):
    """Yield every item depth-first without changing selection/current item."""

    def visit(item):
        yield item
        for index in range(item.childCount()):
            yield from visit(item.child(index))

    for index in range(tree.topLevelItemCount()):
        yield from visit(tree.topLevelItem(index))


def snapshot_expansion(tree) -> dict[int, bool]:
    return {id(item): item.isExpanded() for item in iter_tree_items(tree)}


def filter_tree(
        tree, query: str, extra_text: Callable[[object], str],
        previous_expansion: dict[int, bool] | None = None,
) -> dict[int, bool] | None:
    """Filter recursively and expand only branches containing matches.

    The caller owns ``previous_expansion`` between keystrokes.  Clearing the
    query restores the expansion state captured when filtering began.
    """

    needle = query.strip().casefold()
    if needle and previous_expansion is None:
        previous_expansion = snapshot_expansion(tree)

    signals_were_blocked = tree.blockSignals(True)
    try:
        if not needle:
            for item in iter_tree_items(tree):
                item.setHidden(False)
                if previous_expansion is not None:
                    item.setExpanded(
                        previous_expansion.get(id(item), item.isExpanded()))
            return None

        def visit(item) -> bool:
            child_hit = False
            for index in range(item.childCount()):
                child_hit = visit(item.child(index)) or child_hit
            own_text = " ".join(
                item.text(column) for column in range(tree.columnCount()))
            metadata = extra_text(item)
            own_hit = needle in f"{own_text} {metadata}".casefold()
            visible = own_hit or child_hit
            item.setHidden(not visible)
            if item.childCount():
                item.setExpanded(child_hit)
            return visible

        for index in range(tree.topLevelItemCount()):
            visit(tree.topLevelItem(index))
        return previous_expansion
    finally:
        tree.blockSignals(signals_were_blocked)
