"""Produce the file that actually goes to the device.

Two transformations can happen here:

* **format conversion** - the BOOX file index ignores ``.djvu`` entirely, so a
  DjVu book uploaded as-is is invisible in the reader.  ``ddjvu`` converts it
  to a normal PDF.
* **metadata rewrite** - handled by :mod:`booxlib.metadata` via ``ebook-meta``.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess

from .metadata import apply_meta

log = logging.getLogger(__name__)


class ConversionError(RuntimeError):
    pass


def _run(cmd: list[str], timeout: float = 600.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, errors="replace"
        )
    except subprocess.TimeoutExpired:
        raise ConversionError("conversion timed out")
    except OSError as exc:
        raise ConversionError(str(exc))
    return proc.returncode, (proc.stderr or proc.stdout or "").strip()


def djvu_to_pdf(src: str, dst: str) -> None:
    exe = shutil.which("ddjvu")
    if not exe:
        raise ConversionError(
            "ddjvu not found - install DjVuLibre (brew install djvulibre) "
            "to convert .djvu books, or exclude them"
        )
    code, output = _run([exe, "-format=pdf", src, dst])
    if code != 0 or not os.path.exists(dst) or os.path.getsize(dst) == 0:
        raise ConversionError(f"ddjvu failed: {output[:300]}")


CONVERTERS = {("djvu", "pdf"): djvu_to_pdf}


def produce(
    book,
    dest: str,
    *,
    title: str,
    authors: list[str],
    year: str | None,
    publisher: str | None,
    language: str | None,
) -> tuple[bool, str]:
    """Create *dest* ready for upload.  Returns ``(metadata_written, note)``."""
    os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)

    if book.convert_to:
        convert = CONVERTERS.get((book.ext, book.convert_to))
        if not convert:
            raise ConversionError(f"no converter for .{book.ext} -> .{book.convert_to}")
        convert(book.path, dest)
        ok, message = apply_meta(
            dest, title=title, authors=authors, year=year,
            publisher=publisher, language=language,
        )
        return ok, f"converted from .{book.ext}: {message}"

    shutil.copy2(book.path, dest)
    return apply_meta(
        dest, title=title, authors=authors, year=year,
        publisher=publisher, language=language,
    )
