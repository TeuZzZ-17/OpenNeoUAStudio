"""Coordinated filesystem commits for verified Studio outputs.

Callers must fully build and validate every source file before invoking
``commit_verified_files``.  The helper then stages all destinations, swaps
them as one coordinated operation, and restores the previous files if a
filesystem error interrupts the commit.
"""

from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
import shutil
import tempfile


class VerifiedCommitError(OSError):
    def __init__(self, message: str, *, rollback_complete: bool) -> None:
        super().__init__(message)
        self.rollback_complete = rollback_complete


def commit_verified_files(
        files: list[tuple[Path, Path]], *,
        verify: Callable[[], None] | None = None) -> list[str]:
    """Commit pre-validated files with coordinated rollback.

    Sources are copied to staging files beside every destination.  Existing
    destinations are moved aside only after all staging copies succeed.
    Duplicate destination paths are rejected before any write.  An optional
    final verifier runs while rollback copies are still available.
    """

    destination_keys: set[str] = set()
    normalized: list[tuple[Path, Path]] = []
    for source, target in files:
        source = Path(source)
        target = Path(target)
        key = str(target.resolve(strict=False)).casefold()
        if key in destination_keys:
            raise VerifiedCommitError(
                f"duplicate verified destination: {target}",
                rollback_complete=True)
        destination_keys.add(key)
        if not source.is_file():
            raise VerifiedCommitError(
                f"verified staging source is missing: {source}",
                rollback_complete=True)
        if target.exists() and not target.is_file() \
                and not target.is_symlink():
            raise VerifiedCommitError(
                f"verified destination exists but is not a file: {target}",
                rollback_complete=True)
        normalized.append((source, target))

    records: list[dict] = []
    created_directories: set[Path] = set()
    try:
        for source, target in normalized:
            missing_parents = []
            parent = target.parent
            while not parent.exists():
                missing_parents.append(parent)
                if parent == parent.parent:
                    break
                parent = parent.parent
            target.parent.mkdir(parents=True, exist_ok=True)
            created_directories.update(missing_parents)
            fd, stage_name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".stage",
                dir=target.parent)
            os.close(fd)
            record = {
                "target": target,
                "stage": Path(stage_name),
                "backup": None,
                "committed": False,
            }
            records.append(record)
            shutil.copy2(source, record["stage"])

        for record in records:
            target = record["target"]
            # ``Path.exists`` is false for a broken symlink.  It still needs a
            # rollback record before the staged file replaces the link.
            if target.exists() or target.is_symlink():
                fd, backup_name = tempfile.mkstemp(
                    prefix=f".{target.name}.", suffix=".rollback",
                    dir=target.parent)
                os.close(fd)
                backup = Path(backup_name)
                backup.unlink()
                os.replace(target, backup)
                record["backup"] = backup
            os.replace(record["stage"], target)
            record["committed"] = True
        if verify is not None:
            verify()
    except Exception as exc:
        rollback_errors = []
        for record in reversed(records):
            target = record["target"]
            backup = record["backup"]
            try:
                if backup is not None and backup.exists():
                    if target.exists():
                        target.unlink()
                    os.replace(backup, target)
                    record["backup"] = None
                elif record["committed"] and target.exists():
                    target.unlink()
            except OSError as rollback_exc:
                rollback_errors.append(f"{target}: {rollback_exc}")
        for record in records:
            stage = record["stage"]
            try:
                if stage.exists():
                    stage.unlink()
            except OSError as cleanup_exc:
                rollback_errors.append(f"{stage}: {cleanup_exc}")
        for directory in sorted(
                created_directories,
                key=lambda value: len(value.parts), reverse=True):
            try:
                directory.rmdir()
            except FileNotFoundError:
                pass
            except OSError as cleanup_exc:
                rollback_errors.append(f"{directory}: {cleanup_exc}")
        message = str(exc)
        if rollback_errors:
            message += ("\n\nRollback was incomplete; inspect these paths:\n"
                        + "\n".join(rollback_errors))
        raise VerifiedCommitError(
            message, rollback_complete=not rollback_errors) from exc

    warnings = []
    for record in records:
        backup = record["backup"]
        if backup is not None and backup.exists():
            try:
                backup.unlink()
            except OSError as exc:
                warnings.append(
                    "saved, but temporary backup cleanup failed: "
                    f"{backup} ({exc})")
        stage = record["stage"]
        if stage.exists():
            try:
                stage.unlink()
            except OSError as exc:
                warnings.append(
                    f"saved, but staging cleanup failed: {stage} ({exc})")
    return warnings


__all__ = ["VerifiedCommitError", "commit_verified_files"]
