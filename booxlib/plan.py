"""Turn resolved books into a placement plan: dedupe, classify, name, collide."""

from __future__ import annotations

import hashlib
import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from .classify import Classifier
from .config import Config
from .model import Book, title_quality

log = logging.getLogger(__name__)


def sha256_of(path: str, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _format_score(book: Book, config: Config) -> float:
    """Higher is better.  Format preference dominates, then completeness."""
    score = float(config.preference.get(book.ext, 1))
    category = book.category
    if category in config.prefer_pdf_for:
        if book.ext == "pdf":
            score += 60
        elif book.ext == "epub":
            score -= 20
    if book.ext == "epub" and config.tiny_epub_bytes and book.size < config.tiny_epub_bytes:
        score -= 55  # broken link-dump epubs are worse than a real PDF
    score += title_quality(book.title) * 2
    if not book.authors:
        score -= 3
    if not book.year:
        score -= 0.5
    # log-ish size nudge so a fuller copy wins among equals
    score += min(book.size / 50_000_000.0, 2.0)
    return score


@dataclass
class Plan:
    books: list[Book] = field(default_factory=list)
    duplicates: list[Book] = field(default_factory=list)
    excluded: list[Book] = field(default_factory=list)
    collisions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(b.size for b in self.books)

    def by_category(self) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for b in self.books:
            out[b.category] += 1
        return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))

    def by_author(self) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for b in self.books:
            out[b.authors[0] if b.authors else "Unknown Author"] += 1
        return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))

    def summary(self) -> dict[str, Any]:
        return {
            "books": len(self.books),
            "duplicates_dropped": len(self.duplicates),
            "excluded_artifacts": len(self.excluded),
            "collisions": len(self.collisions),
            "bytes": self.total_bytes,
            "categories": self.by_category(),
            "authors": len({b.authors[0] for b in self.books if b.authors}),
            "with_issues": sum(1 for b in self.books if b.issues),
        }


def _mark_exact_duplicates(books: list[Book]) -> None:
    """Hash only files whose size collides — avoids reading the whole library."""
    by_size: dict[int, list[Book]] = defaultdict(list)
    for book in books:
        by_size[book.size].append(book)

    for size, group in by_size.items():
        if len(group) < 2:
            continue
        by_hash: dict[str, list[Book]] = defaultdict(list)
        for book in group:
            try:
                book.sha256 = sha256_of(book.path)
            except OSError as exc:
                log.warning("cannot hash %s: %s", book.path, exc)
                continue
            by_hash[book.sha256].append(book)
        for digest, same in by_hash.items():
            if len(same) < 2:
                continue
            log.debug("exact duplicate (%d copies): %s", len(same), same[0].title)


def _drop_exact_duplicates(books: list[Book], config: Config) -> tuple[list[Book], list[Book]]:
    """Collapse byte-identical files to the best-scoring representative."""
    by_hash: dict[str, list[Book]] = defaultdict(list)
    for book in books:
        key = book.sha256 or f"path:{book.path}"
        by_hash[key].append(book)

    kept: list[Book] = []
    dropped: list[Book] = []
    for group in by_hash.values():
        if len(group) == 1:
            kept.append(group[0])
            continue
        winner = max(group, key=lambda b: _format_score(b, config))
        kept.append(winner)
        for book in group:
            if book is winner:
                continue
            book.duplicate_of = winner.path
            book.duplicate_reason = "identical file content"
            dropped.append(book)
    return kept, dropped


def _drop_same_work(books: list[Book], config: Config) -> tuple[list[Book], list[Book]]:
    """Collapse 'same author + same title, different file' to the best format."""
    groups: dict[str, list[Book]] = defaultdict(list)
    loose: list[Book] = []
    for book in books:
        # only group when we actually know the author; otherwise we risk
        # merging unrelated books that share a generic title
        if book.authors and book.title:
            groups[book.group_key].append(book)
        else:
            loose.append(book)

    kept: list[Book] = list(loose)
    dropped: list[Book] = []
    for key, group in groups.items():
        if len(group) == 1:
            kept.append(group[0])
            continue
        winner = max(group, key=lambda b: _format_score(b, config))
        kept.append(winner)
        for book in group:
            if book is winner:
                continue
            book.duplicate_of = winner.path
            other = f"{book.ext} {book.size // 1024}K"
            book.duplicate_reason = f"same work, kept better {winner.ext} copy (this: {other})"
            dropped.append(book)
    return kept, dropped


def _resolve_collisions(books: list[Book]) -> list[dict[str, Any]]:
    """Two different books must not land on the same path."""
    buckets: dict[str, list[Book]] = defaultdict(list)
    for book in books:
        buckets[book.target_relpath.lower()].append(book)

    collisions: list[dict[str, Any]] = []
    for rel, group in buckets.items():
        if len(group) == 1:
            continue
        # deterministic order: keep the largest, disambiguate the rest
        group.sort(key=lambda b: (-b.size, b.rel_path))
        collisions.append(
            {
                "target": rel,
                "kept": group[0].rel_path,
                "others": [b.rel_path for b in group[1:]],
            }
        )
        for index, book in enumerate(group[1:], start=2):
            base, ext = os.path.splitext(book.target_name)
            book.target_name = f"{base} [{index}]{ext}"
            book.issues.append("renamed: path collision")
    return collisions


def build_plan(
    books: list[Book], config: Config, *, include_artifacts: bool = False
) -> Plan:
    classifier = Classifier(config)

    excluded = [] if include_artifacts else [b for b in books if b.is_artifact]
    if excluded:
        log.info("excluding %d non-book artifacts", len(excluded))
        books = [b for b in books if not b.is_artifact]

    for book in books:
        book.classification = classifier.classify(
            title=book.title,
            authors=book.authors,
            tags=book.embedded.tags,
            source_dir=book.source_dir,
            series=book.series,
        )

    _mark_exact_duplicates(books)
    kept, dropped_exact = _drop_exact_duplicates(books, config)
    kept, dropped_work = _drop_same_work(kept, config)
    dropped = dropped_exact + dropped_work

    conversions = config.convert
    for book in kept:
        book.convert_to = conversions.get(book.ext)
        book.apply_naming(
            author_style=config.author_style,
            folder_style=config.author_folder_style,
            max_len=config.max_filename_len,
        )

    collisions = _resolve_collisions(kept)
    kept.sort(key=lambda b: b.target_relpath.lower())
    return Plan(books=kept, duplicates=dropped, excluded=excluded, collisions=collisions)
