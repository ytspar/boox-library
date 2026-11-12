"""Health checks: what is on the device vs what the reader can actually see.

A BOOX keeps two independent views of the same library:

* the filesystem (``/api/storage``) - what was uploaded
* the library index (``/api/library``) - what the Library tab shows

They disagree more often than one would like.  A format the index refuses to
ingest (``.djvu`` and ``.lit`` on a Go 7) uploads "successfully" and then never
appears; a file deleted from disk can linger as an entry that cannot be opened
and cannot be deleted through the API.  ``verify`` only looks at the
filesystem, so both classes get their own check here.
"""

from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass, field

from .config import Config
from .names import fold
from .device import Device
from .plan import Plan
from .shelves import _fold, current_placement, desired

# Formats known to be unreadable/unindexable on BOOX: the conversion that
# makes them visible.
SUGGESTED_CONVERSIONS = {"djvu": "pdf", "lit": "epub"}


@dataclass
class Finding:
    """One problem found on the device."""

    kind: str  # invisible | ghost | shelf-names | unshelved
    severity: str  # error | warning
    message: str
    paths: list[str] = field(default_factory=list)

    def as_dict(self, limit: int = 20) -> dict:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "message": self.message,
            "count": len(self.paths),
            "paths": self.paths[:limit],
        }


@dataclass
class Report:
    disk_files: int = 0
    indexed: int = 0
    shelves: int = 0
    findings: list[Finding] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    def of_kind(self, kind: str) -> list[Finding]:
        return [f for f in self.findings if f.kind == kind]

    def as_dict(self, limit: int = 20) -> dict:
        return {
            "disk_files": self.disk_files,
            "indexed": self.indexed,
            "shelves": self.shelves,
            "findings": [f.as_dict(limit) for f in self.findings],
        }


def clear_ghosts(device: Device, config: Config, report: Report) -> int:
    """Drop index entries whose file is gone; returns how many were removed.

    ``DELETE /api/storage/delete`` deletes *both* the file and its record when
    a file is found at the path, so this re-checks the disk listing first and
    compares case-insensitively: an old record can differ from the real file
    only by case (`Carre, John Le` vs `Carré, John le`), and deleting such a
    record destroys the book. Records are only removed when no file matches.
    """
    disk_cf = {
        fold(e.path)
        for e in device.list_dir(config.books_root, recursive=True)
        if not e.is_dir
    }
    removed = 0
    for finding in report.of_kind("ghost"):
        for path in finding.paths:
            if fold(path) in disk_cf:
                continue  # a real book hides behind this record: leave it alone
            device.delete_index_record(path)
            removed += 1
    return removed


def _suggest(ext: str) -> str:
    target = SUGGESTED_CONVERSIONS.get(ext)
    if target:
        return f" - add `{ext}: {target}` under formats.convert and re-sync"
    return " - the index does not ingest this format; convert it under formats.convert"


def diagnose(device: Device, config: Config, plan: Plan | None = None) -> Report:
    """Compare the device filesystem against its library index (read-only)."""
    disk = [e for e in device.list_dir(config.books_root, recursive=True) if not e.is_dir]
    # Compare folded, not raw: the device resolves paths case- and
    # normalization-insensitively ('Carré' in NFC matches 'Carré' in NFD and
    # 'CARRE'), so a raw comparison reports files that are right there as
    # missing - and acting on that deletes real books.
    disk_cf = {fold(e.path) for e in disk}
    placement = current_placement(device)
    index_cf = {fold(p) for p in placement}

    report = Report(
        disk_files=len(disk),
        indexed=len(placement),
        shelves=len(device.shelves()),
    )

    # on disk but the reader cannot see it: the .lit/.djvu class of bug
    invisible = [e.path for e in disk if fold(e.path) not in index_cf]
    if invisible:
        by_ext: dict[str, list[str]] = defaultdict(list)
        for path in invisible:
            by_ext[os.path.splitext(path)[1].lower().lstrip(".") or "(none)"].append(path)
        for ext, paths in sorted(by_ext.items(), key=lambda kv: -len(kv[1])):
            report.findings.append(
                Finding(
                    "invisible",
                    "error",
                    f"{len(paths)} .{ext} file(s) are on the device but not in the "
                    f"library index{_suggest(ext)}",
                    paths,
                )
            )

    # in the index but the file is gone: unopenable ghost cards
    ghosts = [p for p in placement if fold(p) not in disk_cf]
    if ghosts:
        report.findings.append(
            Finding(
                "ghost",
                "warning",
                f"{len(ghosts)} library entr(y/ies) point at files that no longer "
                "exist - the reader lists them as cards that cannot be opened; run "
                "`booxlib doctor --fix` to drop them",
                ghosts,
            )
        )

    renames = device.shelf_renames()
    if renames:
        report.findings.append(
            Finding(
                "shelf-names",
                "warning",
                f"{len(renames)} shelf(s) carry a suffix like '-1-1' because the "
                "device disambiguates duplicate names instead of refusing them - "
                "run `booxlib shelves --apply` (or `doctor --fix`) to rename them "
                "back; books stay put",
                [f"{raw}  ->  {clean}" for _sid, raw, clean in renames],
            )
        )

    if plan is not None and config.shelves_layout != "none":
        misplaced = _misplaced(device, config, plan, placement)
        if misplaced:
            report.findings.append(
                Finding(
                    "unshelved",
                    "warning",
                    f"{len(misplaced):,} book(s) are not in their "
                    f"'{config.shelves_layout}' shelf - run `booxlib shelves --apply` "
                    "or `booxlib doctor --fix`",
                    misplaced,
                )
            )

    return report


def _misplaced(device: Device, config: Config, plan: Plan, placement: dict) -> list[str]:
    ids = device.shelves()
    wrong: list[str] = []
    for path, books in desired(plan, config.shelves_layout, config.books_root).items():
        shelf_id = ids.get(_fold(path))
        for book_path in books:
            # a shelf that does not exist yet cannot be the right place
            if shelf_id is None or placement.get(book_path) != shelf_id:
                wrong.append(book_path)
    return wrong
