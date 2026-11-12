"""Embedded-metadata access via Calibre's ``ebook-meta``.

Calibre is the only tool that reliably reads *and writes* metadata across
EPUB/MOBI/AZW3/PDF, so we shell out to it rather than reimplementing PalmDB
record surgery.  Results are cached on disk because a full scan touches ~1300
files and ``ebook-meta`` is not cheap to start.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field, asdict
from typing import Any

log = logging.getLogger(__name__)

CALIBRE_APP_BIN = "/Applications/calibre.app/Contents/MacOS"

#: formats calibre 6.x will write metadata into (verified empirically)
WRITABLE_META = {
    "azw", "azw1", "azw3", "azw4", "docx", "epub", "fb2", "fbz",
    "htmlz", "lrf", "mobi", "odt", "pdb", "pdf", "prc", "rtf", "tpz", "txtz",
}
#: formats with no writable metadata block: filename is the only lever
READ_ONLY_META = {"djvu", "cbz", "cbr", "chm", "lit", "txt", "doc", "rtf"}


def can_write_meta(ext: str) -> bool:
    return ext.lower().lstrip(".") in WRITABLE_META


def calibre_bin(name: str) -> str:
    """Locate a calibre CLI tool, tolerating the macOS .app bundle layout."""
    found = shutil.which(name)
    if found:
        return found
    candidate = os.path.join(CALIBRE_APP_BIN, name)
    if os.path.exists(candidate):
        return candidate
    raise FileNotFoundError(
        f"{name} not found. Install Calibre (brew install --cask calibre) "
        f"or put its CLI on PATH."
    )


@dataclass
class Meta:
    """Normalised embedded metadata."""

    title: str = ""
    authors: list[str] = field(default_factory=list)
    year: str | None = None
    publisher: str | None = None
    language: str | None = None
    isbn: str | None = None
    series: str | None = None
    tags: list[str] = field(default_factory=list)
    read_error: str | None = None

    @property
    def empty(self) -> bool:
        return not self.title and not self.authors

    def to_row(self) -> dict[str, Any]:
        return asdict(self)


def _parse_ebook_meta(text: str) -> Meta:
    """Parse the ``Key : value`` block printed by ``ebook-meta``."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip().lower()] = value.strip()

    meta = Meta()
    meta.title = fields.get("title", "")

    authors_raw = fields.get("author(s)", "")
    if authors_raw:
        # "Andrew S. Grove [Grove, Andrew S.]" -> keep the display form
        display = authors_raw.split("[")[0].strip()
        parts = [p.strip() for p in re.split(r"\s+&\s+", display) if p.strip()]
        meta.authors = parts

    meta.publisher = fields.get("publisher") or None
    meta.language = (fields.get("languages") or "").split(",")[0].strip() or None

    published = fields.get("published", "")
    m = re.search(r"(\d{4})", published)
    if m:
        meta.year = m.group(1)

    ident = fields.get("identifiers", "")
    m = re.search(r"isbn:(\d[\dXx-]{9,16})", ident)
    if m:
        meta.isbn = m.group(1).replace("-", "")

    series = fields.get("series", "")
    if series:
        meta.series = series.strip()

    tags = fields.get("tags", "")
    if tags:
        meta.tags = [t.strip() for t in tags.split(",") if t.strip()]

    return meta


def read_meta(path: str, *, timeout: float = 60.0) -> Meta:
    """Read embedded metadata.  Never raises: failures come back as ``read_error``."""
    try:
        exe = calibre_bin("ebook-meta")
    except FileNotFoundError as exc:
        return Meta(read_error=str(exc))
    try:
        proc = subprocess.run(
            [exe, path], capture_output=True, text=True, timeout=timeout,
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return Meta(read_error="timeout")
    except OSError as exc:
        return Meta(read_error=str(exc))
    if proc.returncode != 0:
        return Meta(read_error=(proc.stderr or proc.stdout or "failed").strip()[:300])
    meta = _parse_ebook_meta(proc.stdout)
    if meta.empty:
        meta.read_error = "no embedded metadata"
    return meta


def apply_meta(
    path: str,
    *,
    title: str,
    authors: list[str],
    year: str | None = None,
    publisher: str | None = None,
    language: str | None = None,
    timeout: float = 300.0,
) -> tuple[bool, str]:
    """Rewrite metadata on an existing file in place."""
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    if not os.path.exists(path):
        return False, "file does not exist"
    if not can_write_meta(ext):
        return False, f"format .{ext} has no writable metadata (filename only)"

    try:
        exe = calibre_bin("ebook-meta")
    except FileNotFoundError as exc:
        return False, str(exc)

    cmd = [exe, path, "-t", title]
    if authors:
        cmd += ["-a", " & ".join(authors)]
    if publisher:
        cmd += ["-p", publisher]
    if language:
        cmd += ["-l", language]
    if year:
        cmd += ["-d", f"{year}-01-01"]

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, errors="replace"
        )
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except OSError as exc:
        return False, str(exc)

    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "failed").strip().splitlines()
        return False, (msg[-1] if msg else "failed")[:300]

    # calibre exits 0 even when a format silently refuses the write, so verify
    if not _verify(path, title, authors):
        return False, "write silently ignored"
    return True, "ok"


def stage_with_metadata(
    src: str,
    dst: str,
    *,
    title: str,
    authors: list[str],
    year: str | None = None,
    publisher: str | None = None,
    language: str | None = None,
    timeout: float = 300.0,
) -> tuple[bool, str]:
    """Copy *src* to *dst* and rewrite the copy's embedded metadata.

    ``ebook-meta`` edits in place, so we always work on a copy: the user's
    original file is never touched.  Returns ``(ok, message)``; for formats
    with no writable metadata block the copy is still produced and ``ok`` is
    ``False`` with a ``read-only`` message.
    """
    import shutil as _shutil

    ext = os.path.splitext(dst)[1].lstrip(".").lower()
    os.makedirs(os.path.dirname(os.path.abspath(dst)) or ".", exist_ok=True)
    _shutil.copy2(src, dst)

    if not can_write_meta(ext):
        return False, f"format .{ext} has no writable metadata (filename only)"

    try:
        exe = calibre_bin("ebook-meta")
    except FileNotFoundError as exc:
        return False, str(exc)

    cmd = [exe, dst, "-t", title]
    if authors:
        cmd += ["-a", " & ".join(authors)]
    if publisher:
        cmd += ["-p", publisher]
    if language:
        cmd += ["-l", language]
    if year:
        cmd += ["-d", f"{year}-01-01"]

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, errors="replace"
        )
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except OSError as exc:
        return False, str(exc)

    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "failed").strip().splitlines()
        return False, (msg[-1] if msg else "failed")[:300]

    # calibre exits 0 even when a format silently refuses the write, so verify
    if not _verify(dst, title, authors):
        return False, "write silently ignored"
    return True, "ok"


def _verify(path: str, title: str, authors: list[str]) -> bool:
    """Confirm the title actually landed in the file."""
    reread = read_meta(path)
    if reread.read_error:
        return False
    if title.strip().lower() not in (reread.title or "").strip().lower():
        return False
    if authors and reread.authors:
        return authors[0].split()[-1].lower() in reread.authors[0].lower()
    return True


# ------------------------------------------------------------------- caching


class MetaCache:
    """Disk cache keyed by (path, mtime, size)."""

    def __init__(self, path: str):
        self.path = path
        self._data: dict[str, Any] = {}
        self._dirty = False
        self.hits = 0
        self.misses = 0
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    self._data = json.load(fh)
            except (OSError, ValueError):
                self._data = {}

    @staticmethod
    def _key(path: str) -> str | None:
        try:
            st = os.stat(path)
        except OSError:
            return None
        return f"{path}|{int(st.st_mtime)}|{st.st_size}"

    def get(self, path: str) -> Meta | None:
        key = self._key(path)
        if not key or key not in self._data:
            self.misses += 1
            return None
        self.hits += 1
        return Meta(**self._data[key])

    def put(self, path: str, meta: Meta) -> None:
        key = self._key(path)
        if key:
            self._data[key] = meta.to_row()
            self._dirty = True

    def save(self) -> None:
        if not self._dirty:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, ensure_ascii=False)
        os.replace(tmp, self.path)
        self._dirty = False

    def read(self, path: str) -> Meta:
        cached = self.get(path)
        if cached is not None:
            return cached
        meta = read_meta(path)
        self.put(path, meta)
        return meta
