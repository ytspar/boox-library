"""Record of what was uploaded, so re-runs are cheap and correct.

An upload is not a byte-for-byte copy of the source: ``ebook-meta`` rewrites
the embedded metadata first, which changes the file size.  Comparing the device
file against the *source* size would therefore report every book as missing and
re-upload ~8 GB on every run.

The manifest records, per device path, the source fingerprint at upload time and
the size that actually landed on the device.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass


@dataclass
class Record:
    source: str
    fingerprint: str
    device_size: int
    uploaded_at: float


def fingerprint(book) -> str:
    """Identity of the *input* file: path + size + mtime."""
    try:
        mtime = int(os.path.getmtime(book.path))
    except OSError:
        mtime = 0
    return f"{book.size}:{mtime}"


class Manifest:
    def __init__(self, path: str):
        self.path = path
        self._data: dict[str, dict] = {}
        self._dirty = False
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    self._data = json.load(fh)
            except (OSError, ValueError):
                self._data = {}

    @staticmethod
    def key(target_path: str) -> str:
        return target_path.casefold()

    def get(self, target_path: str) -> Record | None:
        raw = self._data.get(self.key(target_path))
        return Record(**raw) if raw else None

    def record(self, target_path: str, book, device_size: int) -> None:
        self._data[self.key(target_path)] = asdict(
            Record(
                source=book.path,
                fingerprint=fingerprint(book),
                device_size=device_size,
                uploaded_at=time.time(),
            )
        )
        self._dirty = True

    def forget(self, target_path: str) -> None:
        if self._data.pop(self.key(target_path), None) is not None:
            self._dirty = True

    def needs_upload(self, target_path: str, book, device_entry) -> bool:
        """True when this book must be (re)sent."""
        if device_entry is None:
            return True
        rec = self.get(target_path)
        if rec is None:
            # present but unknown: adopt it rather than re-upload 8 GB
            self.record(target_path, book, device_entry.size)
            return False
        if rec.fingerprint != fingerprint(book):
            return True  # source file changed since the upload
        return rec.device_size != device_entry.size

    def save(self) -> None:
        if not self._dirty:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, indent=2)
        os.replace(tmp, self.path)
        self._dirty = False
