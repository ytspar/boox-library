"""BooxDrop HTTP client.

The Boox "BOOX Drop" webserver (default port 8085) exposes an undocumented
REST API.  This module wraps the handful of endpoints we need so the rest of
the toolchain never touches HTTP directly.

Safety model
------------
A :class:`Device` is constructed *read-only* by default.  Every mutating
method calls :meth:`Device._require_write`, which raises
:class:`WriteBlocked` unless the instance was created with ``allow_write=True``.
That makes "dry run" the default at the lowest level, so a bug in a higher
layer cannot accidentally delete a user's library.
"""

from __future__ import annotations

import contextlib
import json
import logging
import mimetypes
import os
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Sequence

import re
import requests

log = logging.getLogger(__name__)

DEFAULT_HOST = "http://192.168.1.50:8085"
BOOKS_ROOT = "/storage/emulated/0/Books"

#: filenames the device respawns as system folders; never treat as books
IGNORED_DIRS = {"Android"}


class BooxError(RuntimeError):
    """Any non-success response from the device."""


class WriteBlocked(BooxError):
    """Raised when a mutating call is attempted on a read-only client."""


_SHELF_SUFFIX = re.compile(r"(?:-\d+)+$")


def clean_shelf_name(name: str) -> str:
    """Strip the "-1-1" suffixes the device appends to duplicate shelf names."""
    cleaned = _SHELF_SUFFIX.sub("", name).strip()
    return cleaned or name


class AlreadyExists(BooxError):
    """The device reports 417 / "There is file with the same name" for dupes."""


@dataclass
class Entry:
    """A file or directory as reported by the device."""

    name: str
    path: str
    is_dir: bool
    size: int = 0
    updated_at: int = 0


@dataclass
class DeviceInfo:
    model: str
    nickname: str
    host: str
    mac: str
    storage_total: str
    storage_used: str


@dataclass
class ActionLog:
    """Append-only record of everything this process did to the device.

    Written to disk so a run can be inspected or partially replayed.
    """

    entries: list[dict[str, Any]] = field(default_factory=list)

    def record(self, action: str, **fields: Any) -> None:
        self.entries.append({"action": action, "ts": time.time(), **fields})
        log.info("%s %s", action, fields)

    def dump(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.entries, fh, indent=2, ensure_ascii=False)

    @property
    def mutations(self) -> list[dict[str, Any]]:
        return [e for e in self.entries if e["action"] != "read"]


@dataclass
class Device:
    host: str = DEFAULT_HOST
    allow_write: bool = False
    timeout: float = 30.0
    retries: int = 3
    sender: str = "booxlib"
    log: ActionLog = field(default_factory=ActionLog)
    session: requests.Session = field(default_factory=requests.Session)
    _ensured: set[str] = field(default_factory=set, repr=False)

    # ---------------------------------------------------------------- plumbing

    def _url(self, path: str) -> str:
        return self.host.rstrip("/") + path

    def _require_write(self, what: str) -> None:
        if not self.allow_write:
            raise WriteBlocked(
                f"refusing to {what}: device client is read-only "
                f"(construct Device(allow_write=True) to permit mutations)"
            )

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = self._url(path)
        kwargs.setdefault("timeout", self.timeout)
        last: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                resp = self.session.request(method, url, **kwargs)
                if resp.status_code in {409, 417}:
                    # the device signals "already exists" with 417
                    raise AlreadyExists(f"{resp.status_code} from {path}: {resp.text[:200]}")
                resp.raise_for_status()
                return resp
            except requests.RequestException as exc:  # pragma: no cover - network
                last = exc
                if attempt == self.retries:
                    break
                log.warning("%s %s failed (%s), retry %d/%d", method, path, exc, attempt, self.retries)
                time.sleep(0.5 * attempt)
        raise BooxError(f"{method} {url} failed after {self.retries} attempts: {last}")

    def _json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        resp = self._request(method, path, **kwargs)
        try:
            payload = resp.json()
        except ValueError:
            raise BooxError(f"{method} {path} returned non-JSON: {resp.text[:300]}")
        if isinstance(payload, dict) and payload.get("successful") is False:
            raise BooxError(f"{method} {path} reported failure: {payload.get('message')!r}")
        return payload

    # ------------------------------------------------------------------- reads

    def ping(self) -> bool:
        resp = self._request("GET", "/api/ping")
        return resp.text.strip() == "ok"

    def info(self) -> DeviceInfo:
        d = self._json("GET", "/api/device")
        return DeviceInfo(
            model=d.get("model", "?"),
            nickname=d.get("nickname", "?"),
            host=d.get("host", self.host),
            mac=d.get("mac", "?"),
            storage_total=d.get("storageTotal", "?"),
            storage_used=d.get("storageUsed", "?"),
        )

    def list_dir(self, path: str, *, recursive: bool = True, page: int = 500) -> list[Entry]:
        """List a directory.

        The device's ``dir`` filter is *recursive*: it returns everything below
        the given path.  ``page`` only bounds a single HTTP request.
        """
        out: list[Entry] = []
        offset = 0
        while True:
            args = json.dumps(
                {"limit": page, "offset": offset, "dir": path, "refresh": False},
                separators=(",", ":"),
            )
            payload = self._json(
                "GET", "/api/media/list?" + urllib.parse.urlencode({"args": args})
            )
            data = payload.get("data") or {}
            batch = data.get("list") or []
            for item in batch:
                out.append(
                    Entry(
                        name=item.get("name", ""),
                        path=item.get("path", ""),
                        is_dir=bool(item.get("dir")),
                        size=int(item.get("size") or 0),
                        updated_at=int(item.get("updatedAt") or 0),
                    )
                )
            total = int(data.get("count") or 0)
            offset += len(batch)
            if not batch or offset >= total:
                break
        if not recursive:
            prefix = path.rstrip("/") + "/"
            out = [e for e in out if "/" not in e.path[len(prefix):]]
        return out

    def list_dirs(self, path: str) -> list[Entry]:
        """List immediate subdirectories.

        ``/api/media/list`` returns *books* only and never reports folders, so
        directory existence has to go through ``/api/storage/directory/tree``.
        """
        args = json.dumps({"dir": path}, separators=(",", ":"))
        payload = self._json(
            "GET", "/api/storage/directory/tree?" + urllib.parse.urlencode({"args": args})
        )
        out = []
        for item in ((payload.get("data") or {}).get("list") or []):
            if item.get("dir"):
                out.append(
                    Entry(
                        name=item.get("name", ""),
                        path=item.get("path", ""),
                        is_dir=True,
                        size=int(item.get("size") or 0),
                    )
                )
        return out

    def walk_dirs(self, path: str, *, max_depth: int = 6) -> list[Entry]:
        """All descendant directories, deepest last."""
        found: list[Entry] = []
        frontier = [(path, 0)]
        while frontier:
            current, depth = frontier.pop()
            if depth >= max_depth:
                continue
            for entry in self.list_dirs(current):
                found.append(entry)
                frontier.append((entry.path, depth + 1))
        return found

    def list_files(self, path: str) -> list[Entry]:
        return [e for e in self.list_dir(path) if not e.is_dir]

    def notes(self) -> list[dict[str, Any]]:
        """Handwritten notes / annotations.  Non-empty means do not wipe."""
        payload = self._json("GET", "/api/note")
        return ((payload.get("data") or {}).get("list")) or []

    def notes_count(self) -> int:
        return len(self.notes())

    def library_books(self) -> list[dict[str, Any]]:
        payload = self._json("GET", "/api/library")
        return payload.get("visibleBookList") or []

    def top_level(self) -> list[Entry]:
        payload = self._json("GET", "/api/storage")
        items = (payload.get("data") or {}).get("list") or []
        return [
            Entry(
                name=i.get("name", ""),
                path=i.get("path", ""),
                is_dir=bool(i.get("dir")),
                size=int(i.get("size") or 0),
            )
            for i in items
        ]

    def free_bytes(self) -> int:
        """Approximate free space, parsed from the human-readable byte counts."""
        d = self._json("GET", "/api/device")

        def parse(text: str) -> int:
            text = (text or "0").strip().upper()
            units = {"KB": 10**3, "MB": 10**6, "GB": 10**9, "TB": 10**12, "B": 1}
            for suffix, mult in sorted(units.items(), key=lambda kv: -len(kv[0])):
                if text.endswith(suffix):
                    try:
                        return int(float(text[: -len(suffix)]) * mult)
                    except ValueError:
                        return 0
            return 0

        return max(0, parse(d.get("storageTotal", "")) - parse(d.get("storageUsed", "")))

    def exists(self, path: str) -> Entry | None:
        parent, _, name = path.rpartition("/")
        for entry in self.list_dir(parent, recursive=False):
            if entry.path == path or entry.name.casefold() == name.casefold():
                return entry
        return None

    # ------------------------------------------------------------------ writes

    def _ensure_component(self, parent: str, name: str) -> str:
        """Create ``parent/name`` unless it already exists.

        The device answers 417 for a directory request with an empty ``parent``,
        so that is rejected here rather than on the wire.
        """
        if not parent or not name:
            raise BooxError(f"refusing to create {name!r} under {parent!r}")
        expected = f"{parent.rstrip('/')}/{name}"
        for entry in self.list_dirs(parent):
            # Android storage is case-insensitive: "Carré, John Le" and
            # "Carré, John le" are the same directory
            if entry.name.casefold() == name.casefold():
                return entry.path
        try:
            payload = self._json(
                "POST", "/api/storage/directory", json={"parent": parent, "name": name}
            )
        except AlreadyExists:
            # the device is authoritative: it says the name is taken, so treat
            # creation as a no-op rather than failing the book
            log.info("directory already present (device is case-insensitive): %s", expected)
            return expected
        path = payload.get("message") or expected
        self.log.record("mkdir", parent=parent, name=name, path=path)
        return path

    # ----------------------------------------------------------------- shelves
    #
    # A "bookshelf" (library) is a virtual, single-parent grouping stored on the
    # device.  Moving a book into one does NOT move the file: ``location`` keeps
    # pointing at the same path on disk.  A book lives in exactly one shelf.

    def shelf_tree(self) -> list[dict[str, Any]]:
        payload = self._json("GET", "/api/library/tree")
        return payload.get("children") or []

    def shelves(self) -> dict[tuple[str, ...], str]:
        """Flatten the shelf tree to ``{path_tuple: idString}``.

        Keys are casefolded because the device compares names that way, and
        use the ``clean_shelf_name`` form so a shelf the device renamed to
        "Unsorted-1-1" still answers to "Unsorted".
        """
        out: dict[tuple[str, ...], str] = {}

        def walk(nodes: Sequence[dict[str, Any]], prefix: tuple[str, ...]) -> None:
            for node in nodes:
                lib = node.get("library") or {}
                name = lib.get("name")
                if not name:
                    continue
                path = prefix + (clean_shelf_name(name),)
                out[tuple(p.casefold() for p in path)] = lib.get("idString", "")
                walk(node.get("children") or [], path)

        walk(self.shelf_tree(), ())
        return out

    def shelf_renames(self) -> list[tuple[str, str, str]]:
        """``(idString, current name, clean name)`` for shelves that need fixing."""
        out: list[tuple[str, str, str]] = []
        for node in self.shelf_tree():
            lib = node.get("library") or {}
            name = lib.get("name") or ""
            clean = clean_shelf_name(name)
            if name and clean != name and lib.get("idString"):
                out.append((lib["idString"], name, clean))
        return out

    def rename_shelf(self, shelf_id: str, name: str) -> None:
        """Rename a shelf in place (books keep their placement)."""
        self._require_write("rename-shelf")
        self._json(
            "POST",
            "/api/library/rename",
            json={"idString": shelf_id, "file": shelf_id, "name": name},
        )
        self.log.record("rename-shelf", id=shelf_id, name=name)

    def shelf_books(self, shelf_id: str | None, *, page: int = 500) -> list[dict[str, Any]]:
        """Books directly in a shelf (``None`` = the unshelved root)."""
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            args = {
                "limit": page,
                "offset": offset,
                "sortBy": "CreationTime",
                "order": "Desc",
                "libraryUniqueId": shelf_id,
            }
            payload = self._json(
                "GET", "/api/library", params={"args": json.dumps(args)}
            )
            batch = payload.get("visibleBookList") or []
            out.extend(batch)
            offset += len(batch)
            if len(batch) < page or offset >= int(payload.get("bookCount") or 0):
                return out

    def create_shelf(self, parent_id: str | None, name: str) -> str:
        """Create a shelf under *parent_id* (``None`` = top level)."""
        self._require_write("create a bookshelf")
        try:
            payload = self._json(
                "POST", "/api/library", json={"parent": parent_id, "name": name}
            )
        except AlreadyExists:
            # the device reports a duplicate name with 417
            self.log.record("shelf-exists", parent=parent_id, name=name)
            return self.ensure_shelf((name,))
        shelf_id = (payload.get("data") or {}).get("idString", "")
        self.log.record("create-shelf", parent=parent_id, name=name, id=shelf_id)
        return shelf_id

    def ensure_shelf(self, path: Sequence[str]) -> str:
        """Create ``path`` (a tuple of shelf names) if absent; return its id."""
        existing = self.shelves()
        parent_id: str | None = None
        for depth in range(len(path)):
            key = tuple(p.casefold() for p in path[: depth + 1])
            if key in existing:
                parent_id = existing[key]
                continue
            payload = self._json(
                "POST", "/api/library", json={"parent": parent_id, "name": path[depth]}
            )
            parent_id = (payload.get("data") or {}).get("idString", "")
            self.log.record(
                "create-shelf", parent=path[:depth], name=path[depth], id=parent_id
            )
            # the device answers "success" but appends "-1" when the name is
            # already taken, so trust the tree, not the reply
            for sid, current, clean in self.shelf_renames():
                if sid == parent_id and clean == path[depth]:
                    try:
                        self.rename_shelf(sid, clean)
                    except BooxError as exc:  # cosmetic; keep going
                        self.log.record("rename-shelf-failed", id=sid, error=str(exc))
            existing = self.shelves()
        if parent_id is None:
            raise BooxError(f"could not resolve shelf {path!r}")
        return parent_id

    def move_to_shelf(
        self,
        paths: Sequence[str],
        shelf_id: str | None,
        shelf_name: str,
        *,
        current_parent: str | None = None,
        force: bool = False,
    ) -> None:
        """Group books into a shelf, without moving any file on disk."""
        self._require_write("move books to a bookshelf")
        payload = {
            "library": {"name": shelf_name, "idString": shelf_id},
            "force": force,
            "map": {
                "selectedMap": {
                    current_parent: {
                        "count": 0,
                        "selectedAllMode": False,
                        "selectedList": list(paths),
                    }
                }
            },
        }
        self._json("POST", "/api/library/move", json=payload)
        self.log.record(
            "move-to-shelf",
            count=len(paths),
            shelf=shelf_name,
            from_shelf=current_parent,
        )

    def delete_shelf(self, shelf_id: str) -> None:
        """Delete an empty shelf.  Never called implicitly - see docs."""
        self._require_write("delete a bookshelf")
        self._json("POST", "/api/library/delete", json={"args": shelf_id})
        self.log.record("delete-shelf", id=shelf_id)

    def create_dir(self, parent: str, name: str) -> str:
        """Create ``parent/name``.  Idempotent: returns existing path if present."""
        self._require_write("create directory")
        return self._ensure_component(parent, name)

    def ensure_dir(self, path: str, *, base: str | None = None) -> str:
        """Create ``path``, creating any missing directories beneath *base*.

        ``base`` is an ancestor known to exist (normally the Books root).  The
        device only enumerates from ``Books`` downward, so ancestors above it
        cannot be probed - without an anchor we only ever create the leaf.
        """
        self._require_write("create directory tree")
        path = path.rstrip("/")
        if path in self._ensured:
            return path

        if base:
            base = base.rstrip("/")
            if path == base:
                self._ensured.add(path)
                return path
            if not path.startswith(base + "/"):
                raise BooxError(f"{path!r} is not inside base {base!r}")
            current = base
            for part in path[len(base) + 1:].split("/"):
                current = f"{current}/{part}"
                self._ensure_component(current.rsplit("/", 1)[0], part)
        else:
            parent, _, name = path.rpartition("/")
            self._ensure_component(parent, name)

        self._ensured.add(path)
        return path

    def upload(self, local_path: str, dest_dir: str, *, name: str | None = None) -> str:
        """Upload one file into ``dest_dir``.  Returns the resulting device path."""
        self._require_write("upload file")
        basename = name or os.path.basename(local_path)
        ctype = mimetypes.guess_type(basename)[0] or "application/octet-stream"
        self.log.record("upload", local=local_path, dest_dir=dest_dir, name=basename)
        with open(local_path, "rb") as fh:
            payload = self._json(
                "POST",
                "/api/storage/upload",
                data={"dir": dest_dir, "sender": self.sender},
                files={"file": (basename, fh, ctype)},
                timeout=None,  # large files: let the socket decide
            )
        return payload.get("message") or f"{dest_dir.rstrip('/')}/{basename}"

    def rename(self, path: str, new_name: str) -> None:
        self._require_write("rename file")
        self._json("POST", "/api/storage/rename", json={"file": path, "name": new_name})
        self.log.record("rename", path=path, name=new_name)

    def move(self, paths: Sequence[str], parent: str, *, force: bool = False) -> None:
        self._require_write("move files")
        body = {
            "force": force,
            "parent": parent,
            "map": {
                "selectedMap": {
                    "null": {
                        "count": 0,
                        "selectedAllMode": False,
                        "selectedList": list(paths),
                    }
                }
            },
        }
        self._json("POST", "/api/storage/file/move", json=body)
        self.log.record("move", paths=list(paths), parent=parent)

    def delete_files(self, paths: Sequence[str]) -> None:
        self._require_write("delete files")
        if not paths:
            return
        body = {
            "selectedMap": {
                "null": {
                    "count": 0,
                    "selectedAllMode": False,
                    "selectedList": list(paths),
                }
            }
        }
        self._json("POST", "/api/storage/batDelete", json=body)
        self.log.record("delete_files", paths=list(paths))

    def delete_dir(self, path: str) -> None:
        self._require_write("delete directory")
        parent = path.rsplit("/", 1)[0]
        body = {
            "selectedMap": {
                parent: {"count": 0, "selectedAllMode": False, "selectedList": [path]}
            }
        }
        self._json("POST", "/api/storage/directory/delete", json=body)
        self.log.record("delete_dir", path=path)

    # --------------------------------------------------------------- utilities

    @contextlib.contextmanager
    def writer(self) -> Iterator["Device"]:
        """Temporarily enable writes, then restore the previous mode."""
        previous = self.allow_write
        self.allow_write = True
        try:
            yield self
        finally:
            self.allow_write = previous


def connect(host: str = DEFAULT_HOST, *, allow_write: bool = False) -> Device:
    dev = Device(host=host, allow_write=allow_write)
    info = dev.info()
    log.info("connected to %s (%s) at %s", info.model, info.nickname, info.host)
    return dev
