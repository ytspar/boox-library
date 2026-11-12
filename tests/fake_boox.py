"""A fake BOOX Drop server used by the test suite.

It implements just enough of the undocumented device API to exercise the whole
pipeline, and - crucially - records every request so tests can assert that a
dry run really does nothing.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass
class FakeState:
    """Mutable device-like state plus a request journal."""

    books_root: str = "/storage/emulated/0/Books"
    files: dict[str, int] = field(default_factory=dict)  # path -> size
    dirs: set[str] = field(default_factory=set)
    # virtual bookshelves: idString -> {"name": str, "parent": str | None}
    shelves: dict[str, dict] = field(default_factory=dict)
    # book path -> shelf idString (absent or None == unshelved at the root)
    placement: dict[str, str | None] = field(default_factory=dict)
    notes: int = 0
    requests: list[tuple[str, str]] = field(default_factory=list)

    def record(self, method: str, path: str) -> None:
        self.requests.append((method, urllib.parse.urlparse(path).path))

    @property
    def mutations(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p in self.requests if m in {"POST", "PUT", "DELETE"}]


def _entry(path: str, size: int, is_dir: bool = False) -> dict[str, Any]:
    return {
        "name": path.rstrip("/").rsplit("/", 1)[-1],
        "path": path,
        "dir": is_dir,
        "size": size,
        "updatedAt": 0,
    }


class _Handler(BaseHTTPRequestHandler):
    state: FakeState  # injected

    def log_message(self, *args: Any) -> None:  # silence the test output
        pass

    # ------------------------------------------------------------- utilities
    def _send(self, payload: Any, status: int = 200, ctype: str = "application/json") -> None:
        body = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _ok(self, **extra: Any) -> None:
        self._send({"code": 0, "successful": True, **extra})

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except ValueError:
            return {}

    def _multipart(self) -> dict[str, Any]:
        """Very small multipart parser: enough for ``dir``/``sender``/``file``."""
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        fields: dict[str, Any] = {}
        boundary = self.headers.get("Content-Type", "").split("boundary=")[-1].strip()
        if not boundary:
            return fields
        for part in raw.split(b"--" + boundary.encode()):
            if b"\r\n\r\n" not in part:
                continue
            head, _, body = part.partition(b"\r\n\r\n")
            # flatten headers onto one line so each attribute is its own token
            head_text = head.decode("utf-8", "replace").replace("\r\n", "; ").replace("\n", "; ")
            name = None
            filename = None
            for token in head_text.split(";"):
                token = token.strip()
                if token.startswith("name="):
                    name = token.split("=", 1)[1].strip('"')
                elif token.startswith("filename="):
                    filename = token.split("=", 1)[1].strip('"')
            if name is None:
                continue
            if body.endswith(b"\r\n"):
                body = body[:-2]
            if filename:
                fields[name] = (filename, len(body))
            else:
                fields[name] = body.decode("utf-8", "replace")
        return fields

    # ---------------------------------------------------------------- routes
    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        self.state.record("GET", self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/api/ping":
            self._send("ok", ctype="text/plain")
        elif path == "/api/device":
            self._send(
                {
                    "host": f"http://{self.headers.get('Host')}",
                    "id": "aa:bb", "mac": "aa:bb", "model": "FakeBoox",
                    "nickname": "FakeBoox", "storageTotal": "64GB",
                    "storageUsed": "1GB", "type": "server",
                }
            )
        elif path == "/api/storage":
            self._send(
                {
                    "code": 0, "successful": True,
                    "data": {
                        "count": 1, "fileCount": 0, "folderCount": 1,
                        "list": [_entry(self.state.books_root, 0, True)],
                    },
                }
            )
        elif path == "/api/media/list":
            args = json.loads(query.get("args", ["{}"])[0])
            root = args.get("dir") or self.state.books_root
            prefix = root.rstrip("/") + "/"
            items = [
                _entry(p, s) for p, s in self.state.files.items() if p.startswith(prefix)
            ]
            items += [
                _entry(d, 0, True) for d in self.state.dirs
                if d.startswith(prefix) or d == root
            ]
            self._send(
                {
                    "code": 0, "successful": True,
                    "data": {"count": len(items), "list": items,
                             "fileCount": len(items), "folderCount": 0},
                }
            )
        elif path == "/api/storage/directory/tree":
            args = json.loads(query.get("args", ["{}"])[0])
            root = (args.get("dir") or self.state.books_root).rstrip("/")
            prefix = root + "/"
            children = [
                _entry(d, 0, True) for d in self.state.dirs
                if d.startswith(prefix) and "/" not in d[len(prefix):]
            ]
            self._send(
                {"code": 0, "successful": True,
                 "data": {"count": len(children), "list": children,
                          "fileCount": 0, "folderCount": len(children)}}
            )
        elif path == "/api/note":
            self._send(
                {"code": 0, "successful": True,
                 "data": {"count": self.state.notes, "list": [{}] * self.state.notes}}
            )
        elif path == "/api/library/tree":
            def node(sid):
                return {
                    "bookCount": sum(1 for v in self.state.placement.values() if v == sid),
                    "library": {"_id": sid, "idString": sid,
                                "name": self.state.shelves[sid]["name"]},
                    "children": [
                        node(c) for c, s in self.state.shelves.items()
                        if s["parent"] == sid
                    ],
                }
            self._send({
                "bookCount": 0,
                "children": [node(sid) for sid, s in self.state.shelves.items()
                             if s["parent"] is None],
            })
        elif path == "/api/library":
            args = json.loads((query.get("args") or ["{}"])[0])
            uid = args.get("libraryUniqueId")
            # a book is in the library once it exists on disk; placement only
            # records which shelf it was grouped into
            known = set(self.state.files) | set(self.state.placement)
            books = sorted(p for p in known if self.state.placement.get(p) == uid)
            self._send({
                "bookCount": len(books), "libraryCount": len(self.state.shelves),
                "visibleBookList": [
                    {"metadata": {"location": p, "idString": p,
                                  "title": p.rsplit("/", 1)[-1], "size": self.state.files[p]}}
                    for p in books
                ],
            })
        elif path == "/api/storage/sdcard":
            self._send([])
        else:
            self._send({"code": 1, "successful": False, "message": "not found"}, 404)

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        self.state.record("POST", self.path)

        if path == "/api/storage/directory":
            body = self._body()
            parent = body.get("parent", "")
            name = body.get("name", "")
            new = f"{parent.rstrip('/')}/{name}"
            self.state.dirs.add(new)
            self._send({"code": 0, "successful": True, "message": new})
        elif path == "/api/storage/upload":
            fields = self._multipart()
            directory = fields.get("dir", "")
            filename, size = fields.get("file", ("unnamed", 0))
            self.state.files[f"{directory.rstrip('/')}/{filename}"] = size
            self._ok(message=f"{directory.rstrip('/')}/{filename}")
        elif path == "/api/storage/batDelete":
            body = self._body()
            selected = (
                body.get("selectedMap", {}).get("null", {}).get("selectedList", [])
            )
            for p in selected:
                self.state.files.pop(p, None)
            self._ok(message="success")
        elif path == "/api/library":
            body = self._body()
            parent = body.get("parent")
            name = body.get("name", "")
            for sid, s in self.state.shelves.items():
                if s["parent"] == parent and s["name"].casefold() == name.casefold():
                    self._send({"code": -1, "successful": False,
                                "message": "exist same name bookshelf"}, 417)
                    return
            sid = f"shelf-{len(self.state.shelves) + 1}"
            self.state.shelves[sid] = {"name": name, "parent": parent}
            self._send({"code": 0, "successful": True,
                        "data": {"idString": sid, "name": name}})
        elif path == "/api/library/move":
            body = self._body()
            dest = (body.get("library") or {}).get("idString")
            moved = 0
            for _parent, group in (body.get("map", {}).get("selectedMap") or {}).items():
                for book in group.get("selectedList", []):
                    self.state.placement[book] = dest
                    moved += 1
            self._send({"code": 0, "successful": True, "message": "success"})
        elif path == "/api/library/delete":
            body = self._body()
            sid = body.get("args")
            if isinstance(sid, dict):
                sid = sid.get("idString")
            self.state.shelves.pop(sid, None)
            for book, placed in list(self.state.placement.items()):
                if placed == sid:
                    self.state.placement[book] = None
            self._send({"code": 0, "successful": True, "message": "success"})
        elif path == "/api/storage/directory/delete":
            body = self._body()
            for group in body.get("selectedMap", {}).values():
                for p in group.get("selectedList", []):
                    self.state.dirs.discard(p)
                    for f in [f for f in self.state.files if f.startswith(p + "/")]:
                        self.state.files.pop(f, None)
            self._ok(message="success")
        elif path == "/api/storage/rename":
            body = self._body()
            old = body.get("file")
            new = body.get("name")
            if old in self.state.files:
                parent = old.rsplit("/", 1)[0]
                self.state.files[f"{parent}/{new}"] = self.state.files.pop(old)
            self._ok()
        elif path == "/api/storage/file/move":
            self._ok()
        else:
            self._send({"code": 1, "successful": False, "message": "not found"}, 404)

    def do_PUT(self) -> None:
        self.state.record("PUT", self.path)
        self._ok()

    def do_DELETE(self) -> None:
        self.state.record("DELETE", self.path)
        self._ok()


class FakeBoox:
    """Context manager that runs the fake device on a free port."""

    def __init__(self, state: FakeState | None = None):
        self.state = state or FakeState()
        handler = type("Handler", (_Handler,), {"state": self.state})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def host(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> "FakeBoox":
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
