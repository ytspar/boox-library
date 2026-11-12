"""Virtual bookshelves: the Library-tab grouping layer.

A shelf is virtual: grouping a book must never move the file on disk, and a
shelf must never be deleted implicitly.
"""

from __future__ import annotations

import pytest

from booxlib.device import Device, WriteBlocked
from booxlib.plan import build_plan
from booxlib.shelves import desired, shelf_path, sync_shelves
from tests.test_plan import make_book


class _B:
    target_dir = "Anthropology/Chagnon, Napoleon A"


def test_shelf_path_layouts():
    assert shelf_path(_B(), "category") == ("Anthropology",)
    assert shelf_path(_B(), "category-author") == ("Anthropology", "Chagnon, Napoleon A")
    assert shelf_path(_B(), "author") == ("Chagnon, Napoleon A",)
    assert shelf_path(_B(), "none") == ()


def test_shelf_path_rejects_unknown_layout():
    with pytest.raises(ValueError):
        shelf_path(_B(), "sideways")


def _plan(tmp_path, config):
    books = [
        make_book(tmp_path, "Jane Doe - A History of Empires (1999).epub"),
        make_book(tmp_path, "Jane Doe - A History of Rome (2001).epub"),
        make_book(tmp_path, "John Roe - Stories of the Sea (2010).epub"),
    ]
    return build_plan(books, config)


def _seed(state, plan, config):
    """Put the planned books on the (fake) device, unshelved."""
    for book in plan.books:
        state.files[f"{config.books_root}/{book.target_relpath}"] = book.size


def test_desired_groups_by_category(tmp_path, config):
    plan = _plan(tmp_path, config)
    book_map = desired(plan, "category", config.books_root)
    assert set(book_map) == {("History",), ("Fiction",)}
    assert len(book_map[("History",)]) == 2


def test_read_only_device_refuses_shelf_writes(fake_boox):
    device = Device(fake_boox.host, allow_write=False)
    with pytest.raises(WriteBlocked):
        device.create_shelf(None, "History")
    with pytest.raises(WriteBlocked):
        device.move_to_shelf(["/x.epub"], "id", "History")
    with pytest.raises(WriteBlocked):
        device.delete_shelf("id")


def test_dry_run_creates_nothing(tmp_path, config, fake_boox, empty_state):
    plan = _plan(tmp_path, config)
    device = Device(fake_boox.host, allow_write=False)
    summary = sync_shelves(device, plan, config, apply=False, layout="category")
    assert summary.shelves_needed == 2
    assert summary.shelves_created == 0
    assert summary.moved == 0
    assert empty_state.mutations == []
    assert empty_state.shelves == {}


def test_apply_creates_shelves_and_groups_without_moving_files(
    tmp_path, config, fake_boox, empty_state
):
    plan = _plan(tmp_path, config)
    _seed(empty_state, plan, config)
    device = Device(fake_boox.host, allow_write=True)
    summary = sync_shelves(device, plan, config, apply=True, layout="category")
    assert summary.shelves_created == 2
    assert summary.moved == 3
    assert summary.failed == 0

    names = {s["name"] for s in empty_state.shelves.values()}
    assert names == {"History", "Fiction"}
    # every book is grouped, and no file was touched
    assert len(empty_state.placement) == 3
    assert set(empty_state.files) == {
        f"{config.books_root}/{b.target_relpath}" for b in plan.books
    }


def test_apply_is_idempotent(tmp_path, config, fake_boox, empty_state):
    plan = _plan(tmp_path, config)
    _seed(empty_state, plan, config)
    device = Device(fake_boox.host, allow_write=True)
    sync_shelves(device, plan, config, apply=True, layout="category")
    before = len([m for m in empty_state.mutations if m[1].startswith("/api/library")])

    summary = sync_shelves(device, plan, config, apply=True, layout="category")
    after = len([m for m in empty_state.mutations if m[1].startswith("/api/library")])

    assert summary.shelves_created == 0
    assert summary.moved == 0
    assert summary.already_right == 3
    assert after == before


def test_shelves_are_never_deleted(tmp_path, config, fake_boox, empty_state):
    empty_state.shelves["keep-me"] = {"name": "Handmade", "parent": None}
    plan = _plan(tmp_path, config)
    _seed(empty_state, plan, config)
    device = Device(fake_boox.host, allow_write=True)
    sync_shelves(device, plan, config, apply=True, layout="category")
    assert "keep-me" in empty_state.shelves


def test_category_author_layout_nests_shelves(tmp_path, config, fake_boox, empty_state):
    plan = _plan(tmp_path, config)
    _seed(empty_state, plan, config)
    device = Device(fake_boox.host, allow_write=True)
    summary = sync_shelves(device, plan, config, apply=True, layout="category-author")
    assert summary.shelves_created == 4  # 2 categories + 2 authors
    parents = {s["parent"] for s in empty_state.shelves.values()}
    assert any(p is not None for p in parents), "author shelves should nest under categories"
