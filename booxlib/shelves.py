"""Virtual bookshelves layered on top of the on-disk folder tree.

A shelf is a *single-parent* grouping stored on the device.  Moving a book into
one does not move the file — ``location`` still points at the same path under
``Books/`` — so shelves are a browsing dimension in the BOOX Library tab, not a
second copy of the folder structure.

The value over plain folders: the Library tab lists shelves directly, so the
whole collection is navigable by category (or category then author) without
walking the filesystem tree.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

LAYOUTS = ("none", "category", "category-author", "author")


def shelf_path(book, layout: str) -> tuple[str, ...]:
    """Shelf path a book belongs to, derived from its target folder."""
    if layout in ("none", ""):
        return ()
    parts = tuple(p for p in (book.target_dir or "").split("/") if p)
    if layout == "category":
        return parts[:1]
    if layout == "author":
        return parts[1:2]
    if layout == "category-author":
        return parts[:2]
    raise ValueError(f"unknown shelf layout {layout!r} (choose from {', '.join(LAYOUTS)})")


def desired(plan, layout: str, books_root: str) -> dict[tuple[str, ...], list[str]]:
    """``{shelf_path: [device file paths]}`` for every book in the plan."""
    out: dict[tuple[str, ...], list[str]] = {}
    if layout in ("none", ""):
        return out
    root = books_root.rstrip("/")
    for book in plan.books:
        path = shelf_path(book, layout)
        if not path:
            continue
        out.setdefault(path, []).append(f"{root}/{book.target_relpath}")
    return out


def current_placement(device) -> dict[str, str | None]:
    """``{device file path: shelf id}``, ``None`` meaning unshelved at the root."""
    place: dict[str, str | None] = {}

    def id_of(entry) -> str | None:
        return (entry.get("metadata") or {}).get("location")

    for book in device.shelf_books(None):
        if id_of(book):
            place[id_of(book)] = None

    tree = device.shelf_tree()

    def walk(nodes, inherited: str | None = None) -> None:
        for node in nodes:
            lib = node.get("library") or {}
            sid = lib.get("idString")
            for book in device.shelf_books(sid):
                if id_of(book):
                    place[id_of(book)] = sid
            walk(node.get("children") or [], sid)

    walk(tree)
    return place


@dataclass
class ShelfSummary:
    layout: str = "none"
    shelves_needed: int = 0
    shelves_missing: int = 0
    shelves_created: int = 0
    books: int = 0
    already_right: int = 0
    to_move: int = 0
    moved: int = 0
    failed: int = 0
    missing_shelves: list[tuple[str, ...]] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "layout": self.layout,
            "shelves_needed": self.shelves_needed,
            "shelves_missing": self.shelves_missing,
            "shelves_created": self.shelves_created,
            "books": self.books,
            "already_right": self.already_right,
            "to_move": self.to_move,
            "moved": self.moved,
            "failed": self.failed,
        }


def _fold(path) -> tuple[str, ...]:
    return tuple(p.casefold() for p in path)


def sync_shelves(device, plan, config, *, apply: bool = False, layout: str | None = None) -> ShelfSummary:
    """Create any missing shelves and group each book under the right one.

    Idempotent: only missing shelves are created and only misplaced books are
    moved.  Shelves are never deleted - the device's delete semantics are not
    fully understood, and a stray empty shelf is harmless.
    """
    layout = (layout or config.shelves_layout or "none").lower()
    summary = ShelfSummary(layout=layout)
    book_map = desired(plan, layout, config.books_root)
    if not book_map:
        return summary

    # every prefix is a shelf: category-author needs the category created too
    needed: set[tuple[str, ...]] = set()
    for path in book_map:
        for depth in range(1, len(path) + 1):
            needed.add(path[:depth])
    summary.shelves_needed = len(needed)
    summary.books = sum(len(v) for v in book_map.values())

    ids = dict(device.shelves())
    missing = sorted((p for p in needed if _fold(p) not in ids), key=len)
    summary.shelves_missing = len(missing)
    summary.missing_shelves = sorted(missing)[:20]

    if not apply:
        place = current_placement(device)
        for path, books in book_map.items():
            sid = ids.get(_fold(path))
            for book_path in books:
                if sid and place.get(book_path) == sid:
                    summary.already_right += 1
                else:
                    summary.to_move += 1
        return summary

    for path in missing:
        try:
            ids[_fold(path)] = device.ensure_shelf(path)
        except Exception as exc:  # noqa: BLE001 - one bad shelf must not abort
            log.warning("could not create shelf %s: %s", "/".join(path), exc)
            summary.failed += 1
    # count what the device really holds, not what we asked for
    summary.shelves_created = max(0, len(device.shelves()) - len(ids) + len(missing)
                                  - summary.failed)

    place = current_placement(device)
    names: dict[str, str] = {}
    groups: dict[tuple[str | None, str], list[str]] = {}
    for path, books in book_map.items():
        sid = ids.get(_fold(path))
        if not sid:
            summary.failed += len(books)
            continue
        names[sid] = "/".join(path)
        for book_path in books:
            current = place.get(book_path)
            if current == sid:
                summary.already_right += 1
            else:
                groups.setdefault((current, sid), []).append(book_path)

    for (current, sid), paths in groups.items():
        try:
            device.move_to_shelf(
                paths, sid, names[sid], current_parent=current, force=False
            )
            summary.to_move += len(paths)
            summary.moved += len(paths)
        except Exception as exc:  # noqa: BLE001
            log.warning("move of %d book(s) into %s failed: %s", len(paths), names[sid], exc)
            summary.failed += len(paths)
    return summary
