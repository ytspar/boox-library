"""Walk the source tree and build resolved :class:`Book` records."""

from __future__ import annotations

import logging
import os
import re
from typing import Iterator

from .config import Config
from .metadata import Meta, MetaCache
from .model import Book, author_quality, name_quality, title_quality
from .names import (
    clean_text,
    normalize_author,
    parse_filename,
    strip_author_prefix,
    strip_edition_noise,
)

log = logging.getLogger(__name__)

MIN_BYTES = 2048  # anything smaller is a stub/placeholder, not a book
_YEAR_OK = range(1400, 2100)
#: arXiv-style ids, DOIs and similar: academic PDFs, not books
_PAPER_ID = re.compile(r"^(?:\d{4}\.\d{4,5}(?:v\d+)?|[a-z\-]+/\d{7}|\d{7}\.\d{4,5})$", re.I)

#: typesetting / archive artifacts that are not readable books
_ARTIFACT_PATTERNS = [
    (re.compile(r"\.(indd|vp|dvi|fm|tex)$", re.I), "typesetting file"),
    (re.compile(r"^\d{3,}-[a-z]\d+-\d+-box-", re.I), "archive box scan"),
    (re.compile(r"^\d{4,}-\d+_[a-z0-9]+_", re.I), "print front-matter"),
    (re.compile(r"^(ba\d+pspb|jccp\d+)", re.I), "journal proof"),
]
#: a book split into per-chapter PDFs
_CHAPTER_SPLIT = re.compile(r"[-_ ](?:ch|chapter|part)[-_. ]?\d+(?:\.pdf)?$", re.I)

#: formats whose embedded metadata is trustworthy
_RELIABLE_EMBEDDED = {"epub", "azw3", "azw", "mobi", "fb2", "lit", "odt", "docx", "rtf"}


def _plausible_year(year: str | None) -> str | None:
    if year and year.isdigit() and int(year) in _YEAR_OK:
        return year
    return None


def discover_files(config: Config) -> Iterator[tuple[str, str, int]]:
    """Yield ``(abs_path, rel_path, size)`` for every ebook-looking file."""
    root = config.source_root
    exts = config.extensions
    exclude = config.exclude_paths
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        parts = [] if rel_dir == "." else rel_dir.split(os.sep)
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in exclude
            and not d.startswith(".")
            and not d.endswith(".app")
            and d != "__MACOSX"
        )
        for name in sorted(filenames):
            if name.startswith(".") or name.startswith("~$"):
                continue
            ext = os.path.splitext(name)[1].lstrip(".").lower()
            if ext not in exts:
                continue
            full = os.path.join(dirpath, name)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            if size < MIN_BYTES:
                continue
            rel = os.path.join(*parts, name) if parts else name
            yield full, rel, size


def _recover_swapped_metadata(book: Book) -> None:
    """Fix PDFs whose Title and Author fields are transposed.

    A large share of scanned/academic PDFs store the author in the title field
    and the title (with publisher and year glued on) in the author field.
    """
    title, authors = book.embedded.title, book.embedded.authors
    if not title or not authors:
        return
    title_is_person = name_quality(title) >= 0.5
    author_is_title = title_quality(authors[0]) >= 0.6 and name_quality(authors[0]) < 0.5
    if title_is_person and author_is_title:
        book.embedded.title, book.embedded.authors = authors[0], [title]
        book.issues.append("recovered swapped title/author metadata")


def _choose_title(book: Book, prefer_filename: bool) -> None:
    """Trust embedded metadata when it looks real; fall back to the filename."""
    emb, par = book.embedded.title, book.parsed.title
    tq_e, tq_p = title_quality(emb), title_quality(par)

    order: list[str] = []
    if prefer_filename:
        order = ["filename", "embedded"]
    else:
        order = ["embedded", "filename"]

    chosen = False
    for source in order:
        if source == "embedded" and tq_e >= 0.45:
            book.title, book.evidence["title"] = emb, "embedded"
            chosen = True
        elif source == "filename" and tq_p >= 0.35:
            book.title, book.evidence["title"] = par, "filename"
            chosen = True
        if chosen:
            break

    if not chosen:
        if emb:
            book.title, book.evidence["title"] = emb, "embedded"
        elif par:
            book.title, book.evidence["title"] = par, "filename"

    if tq_e < 0.35 and tq_p < 0.35:
        book.issues.append("no usable title")


def _choose_authors(book: Book, prefer_filename: bool) -> None:
    emb, par = book.embedded.authors, book.parsed.authors
    aq_e, aq_p = author_quality(emb), author_quality(par)

    order = ["filename", "embedded"] if prefer_filename else ["embedded", "filename"]
    book.authors = []
    for source in order:
        if source == "embedded" and aq_e > 0:
            book.authors, book.evidence["authors"] = list(emb), "embedded"
            break
        if source == "filename" and aq_p > 0:
            book.authors, book.evidence["authors"] = list(par), "filename"
            break
    else:
        book.authors, book.evidence["authors"] = [], "none"

    if not book.authors:
        book.issues.append("no author")


def resolve(book: Book) -> Book:
    """Merge embedded metadata with filename evidence into final fields."""
    _recover_swapped_metadata(book)

    # PDF metadata is unreliable in both directions, so when the filename
    # parses confidently it is the better witness.  EPUB/MOBI/AZW3 metadata
    # is authoritative because calibre wrote it at conversion time.
    prefer_filename = (
        book.ext == "pdf"
        and book.parsed.confidence >= 0.7
        and bool(book.parsed.title)
    ) or book.ext in {"djvu", "cbz", "cbr", "chm"}

    _choose_title(book, prefer_filename)
    _choose_authors(book, prefer_filename)

    year = _plausible_year(book.embedded.year) or _plausible_year(book.parsed.year)
    book.year = year
    book.evidence["year"] = (
        "embedded"
        if _plausible_year(book.embedded.year)
        else ("filename" if year else "none")
    )
    if not year:
        book.issues.append("no year")

    book.publisher = book.embedded.publisher or book.parsed.publisher
    book.language = book.embedded.language
    book.series = book.embedded.series or book.parsed.series

    if book.title:
        book.title = strip_edition_noise(
            re.sub(r"\s+", " ", clean_text(book.title)).strip(" -\u2013_.,")
        )

    if book.authors:
        cleaned = [normalize_author(a) for a in book.authors if a.strip()]
        # drop corporate/secondary authors that do not look like people
        book.authors = [a for a in cleaned if a and name_quality(a) > 0]

    # an author name sometimes leaks into the embedded title
    book.title = strip_author_prefix(book.title, book.authors)

    # the year is appended by build_filename, so strip a copy already in the title
    if book.title:
        book.title = re.sub(r"[\s,;:-]*\(\s*\d{4}\s*\)\s*$", "", book.title).strip(" ,-")
        if book.year:
            book.title = re.sub(
                r"[\s,;:-]+" + re.escape(book.year) + r"\s*$", "", book.title
            ).strip(" ,-")

    _flag_artifacts(book)
    return book


def _flag_artifacts(book: Book) -> None:
    """Mark files that are not really readable books."""
    stem = os.path.splitext(os.path.basename(book.path))[0]
    for pattern, label in _ARTIFACT_PATTERNS:
        if pattern.search(stem) or pattern.search(book.title or ""):
            book.issues.append(f"artifact: {label}")
            book.is_artifact = True
            return
    if _CHAPTER_SPLIT.search(stem):
        book.issues.append("artifact: chapter fragment")
        book.is_artifact = True
        return
    if title_quality(book.title) < 0.3 and not book.authors:
        book.issues.append("artifact: unusable title and no author")
        book.is_artifact = True


def scan(config: Config, *, cache_path: str | None = None) -> list[Book]:
    """Scan the source tree and return resolved books (no dedupe yet)."""
    cache = MetaCache(cache_path) if cache_path else None
    books: list[Book] = []
    count = 0
    for full, rel, size in discover_files(config):
        count += 1
        ext = os.path.splitext(full)[1].lstrip(".").lower()
        embedded = cache.read(full) if cache else Meta()
        parsed = parse_filename(os.path.basename(full))
        book = Book(
            path=full,
            ext=ext,
            size=size,
            source_dir=os.path.dirname(rel),
            rel_path=rel,
            embedded=embedded,
            parsed=parsed,
        )
        if embedded.read_error and not embedded.empty:
            book.issues.append(f"metadata: {embedded.read_error}")

        stem = os.path.splitext(os.path.basename(full))[0]
        if _PAPER_ID.match(stem.strip()):
            book.issues.append("likely-academic-paper")

        resolve(book)
        books.append(book)
        if count % 200 == 0:
            log.info("scanned %d files...", count)

    if cache:
        cache.save()
        log.info("metadata cache: %d hits, %d misses", cache.hits, cache.misses)
    log.info("scanned %d candidate files", count)
    return books
