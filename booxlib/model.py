"""The in-memory model of a book and the decisions made about it."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

from .classify import Classification
from .metadata import Meta
from .names import ParsedName, author_folder, build_filename, contains_publisher_word

#: embedded titles that mean "nobody filled this in"
_JUNK_TITLES = {
    "untitled", "unknown", "document", "document1", "doc1", "ebook", "book", "cover",
    "title", "no title", "microsoft word", "book1", "new document", "scan", "img",
    "fulltext", "untitled document",
}
_JUNK_TITLE_RE = re.compile(
    r"^(microsoft\s+word|pscript|document\s*\d|doc\d|scan|img|dsc|fulltext|"
    r"untitled|pdf\s*-\s*\d|\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)
_JUNK_AUTHOR_RE = re.compile(
    r"^(unknown|admin|administrator|user|owner|pc|vp|vpavan|test|none|n/?a|"
    r"microsoft|adobe|calibre|scan|ocr)\b",
    re.IGNORECASE,
)


def title_quality(title: str) -> float:
    """0..1 heuristic for 'is this a real title'."""
    if not title:
        return 0.0
    t = title.strip()
    if not t:
        return 0.0
    if t.lower() in _JUNK_TITLES:
        return 0.0
    if _JUNK_TITLE_RE.match(t):
        return 0.1
    if len(t) < 3:
        return 0.0
    letters = sum(ch.isalpha() for ch in t)
    if letters / max(1, len(t)) < 0.4:
        return 0.1
    # things like "2212.10077" or "1505.00989"
    if re.fullmatch(r"[\d.\-v]+", t):
        return 0.0
    if len(t.split()) == 1 and len(t) < 5:
        return 0.3
    return 0.9


def name_quality(name: str) -> float:
    """0..1 heuristic for 'is this actually a person's name'.

    Normalisation happens *first* so archive.org-style
    "Jenness, Diamond, 1886-1969" is judged on "Diamond Jenness" rather than
    being rejected for containing a year.
    """
    from .names import normalize_author

    name = normalize_author(name or "")
    if not name or len(name) < 3 or len(name) > 60:
        return 0.0
    if _JUNK_AUTHOR_RE.match(name):
        return 0.0
    if len(name.split()) > 7:
        return 0.0
    # a real name never still contains a bare year after normalisation
    if re.search(r"(?<!\d)(1[5-9]\d{2}|20[0-4]\d)(?!\d)", name):
        return 0.0
    if ":" in name or contains_publisher_word(name):
        return 0.0
    if name.split()[0].lower().strip(",.") in {"the", "a", "an", "of", "on", "in", "and"}:
        return 0.0
    if title_quality(name) > 0.8 and len(name.split()) >= 5:
        return 0.2  # a whole title, not a name
    if " " not in name and name.islower():
        return 0.1
    return 0.9


def author_quality(authors: list[str]) -> float:
    return name_quality(authors[0]) if authors else 0.0


@dataclass
class Book:
    """A candidate file in the source tree, plus everything we decided about it."""

    # raw
    path: str
    ext: str
    size: int
    source_dir: str = ""
    rel_path: str = ""

    # evidence
    embedded: Meta = field(default_factory=Meta)
    parsed: ParsedName = field(default_factory=ParsedName)

    # resolved
    title: str = ""
    authors: list[str] = field(default_factory=list)
    year: str | None = None
    publisher: str | None = None
    language: str | None = None
    series: str | None = None

    classification: Classification | None = None
    is_artifact: bool = False
    convert_to: str | None = None
    issues: list[str] = field(default_factory=list)
    evidence: dict[str, str] = field(default_factory=dict)

    # dedupe bookkeeping
    sha256: str | None = None
    duplicate_of: str | None = None
    duplicate_reason: str | None = None

    # placement
    target_dir: str = ""
    target_name: str = ""
    staged_path: str | None = None
    meta_written: bool = False
    uploaded: bool = False

    @property
    def category(self) -> str:
        return self.classification.category if self.classification else "Unsorted"

    @property
    def output_ext(self) -> str:
        """Extension the uploaded file will have (may differ after conversion)."""
        return self.convert_to or self.ext

    @property
    def target_relpath(self) -> str:
        return f"{self.target_dir}/{self.target_name}" if self.target_dir else self.target_name

    @property
    def group_key(self) -> str:
        """Identity used for 'same book, different file' grouping."""
        from .names import fold, primary_author_key

        author = primary_author_key(self.authors[0]) if self.authors else "|"
        title = re.sub(r"[^a-z0-9]+", "", fold(self.title or ""))
        return f"{author}::{title}"

    def describe(self) -> dict[str, Any]:
        return {
            "source": self.rel_path,
            "size": self.size,
            "ext": self.ext,
            "output_ext": self.output_ext,
            "title": self.title,
            "authors": self.authors,
            "year": self.year,
            "publisher": self.publisher,
            "category": self.category,
            "target": self.target_relpath,
            "issues": self.issues,
            "evidence": self.evidence,
            "duplicate_of": self.duplicate_of,
            "duplicate_reason": self.duplicate_reason,
            "artifact": self.is_artifact,
        }

    def apply_naming(self, *, author_style: str, folder_style: str, max_len: int) -> None:
        self.target_name = build_filename(
            self.authors, self.title, self.year, self.output_ext,
            style=author_style, max_len=max_len,
        )
        if self.authors:
            folder = author_folder(self.authors[0], folder_style)
        else:
            folder = "Unknown Author"
        self.target_dir = f"{self.category}/{folder}"

    def target_path(self, books_root: str) -> str:
        return f"{books_root.rstrip('/')}/{self.target_relpath}"
