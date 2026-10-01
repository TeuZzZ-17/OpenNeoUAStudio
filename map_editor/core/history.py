from __future__ import annotations

from .ldf_model import MAX_HISTORY, LdfDocument


class History:
    """Undo/redo a snapshot, massimo 100 passi (regola Sektor2)."""

    def __init__(self, limit: int = MAX_HISTORY):
        self.limit = limit
        self._undo: list[dict] = []
        self._redo: list[dict] = []
        self._pending: dict | None = None

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()
        self._pending = None

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def begin(self, doc: LdfDocument) -> None:
        if self._pending is None:
            self._pending = doc.snapshot()

    def commit(self, doc: LdfDocument, changed: bool = True) -> bool:
        pending, self._pending = self._pending, None
        if pending is None or not changed:
            return False
        self._undo.append(pending)
        del self._undo[:-self.limit]
        self._redo.clear()
        return True

    def push(self, doc: LdfDocument) -> None:
        self._undo.append(doc.snapshot())
        del self._undo[:-self.limit]
        self._redo.clear()

    def undo(self, doc: LdfDocument) -> bool:
        if not self._undo:
            return False
        self._redo.append(doc.snapshot())
        doc.restore(self._undo.pop())
        return True

    def redo(self, doc: LdfDocument) -> bool:
        if not self._redo:
            return False
        self._undo.append(doc.snapshot())
        doc.restore(self._redo.pop())
        return True
