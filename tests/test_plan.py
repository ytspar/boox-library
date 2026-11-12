"""Dedupe, artifact exclusion and placement rules."""

from __future__ import annotations

import os

from booxlib.model import Book
from booxlib.plan import build_plan


def make_book(tmp_path, name, *, ext=None, size=5000, emb_title="", emb_authors=None, year=None,
              tags=None):
    from booxlib.metadata import Meta
    from booxlib.names import parse_filename

    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    # unique content per filename so only genuinely identical files collide
    path.write_bytes((name.encode() * (size // max(1, len(name)) + 1))[:size])
    ext = ext or path.suffix.lstrip(".")
    book = Book(
        path=str(path), ext=ext, size=size, rel_path=name,
        embedded=Meta(title=emb_title, authors=emb_authors or [], year=year, tags=tags or []),
        parsed=parse_filename(name),
    )
    from booxlib.scan import resolve

    return resolve(book)


def test_epub_beats_pdf_for_same_work(tmp_path, config):
    a = make_book(tmp_path, "Jane Doe - The Same Book (1999).pdf")
    b = make_book(tmp_path, "Jane Doe - The Same Book (1999).epub")
    plan = build_plan([a, b], config)
    assert len(plan.books) == 1
    assert plan.books[0].ext == "epub"
    assert len(plan.duplicates) == 1


def test_identical_files_collapse(tmp_path, config):
    a = make_book(tmp_path, "Jane Doe - Alpha (1999).epub")
    b = make_book(tmp_path, "Jane Doe - Alpha (1999) - libgen.li.epub")
    b.path = a.path  # same bytes on disk => identical hash
    plan = build_plan([a, b], config)
    assert len(plan.books) == 1
    assert plan.duplicates[0].duplicate_reason == "identical file content"


def test_tiny_epub_loses_to_a_real_pdf(tmp_path, config):
    """A broken 10 KB 'epub' link-dump should not beat a genuine PDF."""
    from dataclasses import replace

    strict = replace(config, raw={**config.raw, "formats": {
        **config.raw["formats"], "tiny_epub_bytes": 80000}})
    a = make_book(tmp_path, "Jane Doe - Same Book (1999).pdf", size=90000)
    b = make_book(tmp_path, "Jane Doe - Same Book (1999).epub", size=5000)
    plan = build_plan([a, b], strict)
    assert plan.books[0].ext == "pdf"


def test_different_books_are_not_merged(tmp_path, config):
    a = make_book(tmp_path, "Jane Doe - Alpha (1999).epub")
    b = make_book(tmp_path, "Jane Doe - Beta (1999).epub")
    plan = build_plan([a, b], config)
    assert len(plan.books) == 2


def test_unknown_author_books_are_not_merged_by_title(tmp_path, config):
    a = make_book(tmp_path, "Liberty.pdf", size=3000)
    b = make_book(tmp_path, "Liberty 2.pdf", size=4001)
    plan = build_plan([a, b], config)
    assert len(plan.books) == 2


def test_collisions_are_disambiguated(tmp_path, config):
    """Two different books must never land on the same device path."""
    from booxlib.plan import _resolve_collisions

    a = make_book(tmp_path, "Jane Doe - Alpha (1999).epub", size=4000)
    b = make_book(tmp_path, "Jane Doe - Beta (1999).epub", size=9000)
    # force the pathological case: both resolve to an identical target
    for book in (a, b):
        book.target_dir, book.target_name = "History", "Same Name.epub"

    collisions = _resolve_collisions([a, b])
    assert collisions
    assert a.target_relpath != b.target_relpath
    # the larger file keeps the clean name, the other is disambiguated
    assert any("[2]" in book.target_name for book in (a, b))


def test_artifacts_excluded_by_default(tmp_path, config):
    good = make_book(tmp_path, "Jane Doe - A Real Book (1999).epub")
    junk = make_book(tmp_path, "13521-00_FM_4thPgs.indd.pdf")
    plan = build_plan([good, junk], config)
    assert len(plan.books) == 1
    assert len(plan.excluded) == 1
    assert plan.excluded[0].is_artifact


def test_artifacts_can_be_included(tmp_path, config):
    good = make_book(tmp_path, "Jane Doe - A Real Book (1999).epub")
    junk = make_book(tmp_path, "13521-00_FM_4thPgs.indd.pdf")
    plan = build_plan([good, junk], config, include_artifacts=True)
    assert len(plan.books) == 2


def test_chapter_fragments_are_artifacts(tmp_path, config):
    book = make_book(tmp_path, "The Boron Letters - Chapter 3.pdf")
    assert book.is_artifact


def test_category_from_keyword(tmp_path, config):
    book = make_book(tmp_path, "Jane Doe - A History of the Empire (1999).epub")
    plan = build_plan([book], config)
    assert plan.books[0].category == "History"


def test_fallback_category(tmp_path, config):
    book = make_book(tmp_path, "Jane Doe - Zzzz (1999).epub")
    plan = build_plan([book], config)
    assert plan.books[0].category == "Unsorted"


def test_target_uses_category_and_author_folder(tmp_path, config):
    book = make_book(tmp_path, "Jane Doe - A History of Empires (1999).epub")
    plan = build_plan([book], config)
    assert plan.books[0].target_relpath == "History/Doe, Jane/Doe, Jane - A History of Empires (1999).epub"


def test_swapped_pdf_metadata_is_recovered(tmp_path, config):
    from booxlib.metadata import Meta

    path = tmp_path / "Bruce G. Trigger - Understanding Early Civilizations (2007).pdf"
    path.write_bytes(b"x" * 5000)
    book = Book(
        path=str(path), ext="pdf", size=5000,
        rel_path="Bruce G. Trigger - Understanding Early Civilizations (2007).pdf",
        embedded=Meta(
            title="Bruce G. Trigger",
            authors=["Understanding Early Civilizations A Comparative Study-Cambridge University Press (2007)"],
        ),
    )
    from booxlib.scan import resolve

    resolve(book)
    assert book.authors == ["Bruce G. Trigger"]
    assert "Understanding Early Civilizations" in book.title


def test_djvu_is_planned_as_pdf(tmp_path, config):
    """The BOOX index ignores .djvu, so those books are converted on the way out."""
    from dataclasses import replace

    converting = replace(config, raw={**config.raw, "formats": {
        **config.raw["formats"], "convert": {"djvu": "pdf"}}})
    book = make_book(tmp_path, "Jane Doe - A History of Empires (1999).djvu")
    plan = build_plan([book], converting)
    assert plan.books[0].output_ext == "pdf"
    assert plan.books[0].target_name.endswith(".pdf")
    assert plan.books[0].ext == "djvu"


def test_no_conversion_configured_keeps_original_ext(tmp_path, config):
    book = make_book(tmp_path, "Jane Doe - A History of Empires (1999).djvu")
    plan = build_plan([book], config)
    assert plan.books[0].output_ext == "djvu"
