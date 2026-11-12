"""Assign a category (which becomes a device folder) to each book.

Priority order, highest first:

1. explicit title/author overrides from the config
2. the source sub-directory the file came from (e.g. a reading-list folder)
3. category keyword rules
4. ``fallback_category``

Rules are deliberately simple and inspectable, and the assignment is recorded
with the matching keyword so a bad rule is easy to find and fix.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import Config
from .names import fold


@dataclass
class Classification:
    category: str
    reason: str
    confidence: float


class Classifier:
    def __init__(self, config: Config):
        self.config = config
        self._rules: list[tuple[str, list[re.Pattern[str]]]] = []
        for entry in config.categories:
            name = entry.get("name")
            if not name:
                continue
            patterns = [
                re.compile(re.escape(str(kw)).replace(r"\ ", r"[\s\-_]+"), re.IGNORECASE)
                for kw in (entry.get("keywords") or [])
            ]
            self._rules.append((name, patterns))

        overrides = config.overrides
        self._title_overrides = [
            (fold(str(k)), str(v)) for k, v in (overrides.get("titles") or {}).items()
        ]
        self._author_overrides = [
            (fold(str(k)), str(v)) for k, v in (overrides.get("authors") or {}).items()
        ]
        self._directory_category = {
            fold(k): v for k, v in config.directory_category.items()
        }

    def classify(
        self,
        *,
        title: str,
        authors: list[str],
        tags: list[str] | None = None,
        source_dir: str = "",
        series: str | None = None,
    ) -> Classification:
        title_l = fold(title or "")
        author_blob = fold(" ".join(authors or []))
        haystack = fold(
            " ".join([title or "", " ".join(authors or []), " ".join(tags or []), series or ""])
        )

        for key, category in self._title_overrides:
            if key and key in title_l:
                return Classification(category, f"title override {key!r}", 1.0)

        for key, category in self._author_overrides:
            if key and key in author_blob:
                return Classification(category, f"author override {key!r}", 1.0)

        # keyword rules outrank the source directory: a book filed under
        # that is plainly about Napoleon is History,
        # not Anthropology.
        for name, patterns in self._rules:
            for pattern in patterns:
                match = pattern.search(haystack)
                if match:
                    return Classification(name, f"keyword {match.group(0)!r}", 0.7)

        top_dir = fold(source_dir.split("/")[0]) if source_dir else ""
        if top_dir in self._directory_category:
            category = self._directory_category[top_dir]
            return Classification(category, f"source dir {top_dir!r}", 0.5)

        return Classification(self.config.fallback_category, "no rule matched", 0.0)
