"""The safety contract: nothing is written unless explicitly asked for.

These are the most important tests in the project.  They run the real code
paths against a fake device and assert on the *HTTP journal*, so a regression
that introduces an unexpected write is caught even if the call "succeeds".
"""

from __future__ import annotations

import argparse

import pytest

from booxlib.cli import main
from booxlib.device import Device, WriteBlocked
from booxlib.plan import build_plan
from booxlib.scan import scan


def _device(server, **kwargs):
    return Device(host=server.host, **kwargs)


# --------------------------------------------------------------------- device


def test_read_only_client_blocks_every_mutation(fake_boox):
    device = _device(fake_boox)
    assert device.allow_write is False

    calls = [
        lambda: device.create_dir("/storage/emulated/0/Books", "x"),
        lambda: device.ensure_dir("/storage/emulated/0/Books/x/y"),
        lambda: device.upload(__file__, "/storage/emulated/0/Books"),
        lambda: device.rename("/a/b", "c"),
        lambda: device.move(["/a/b"], "/c"),
        lambda: device.delete_files(["/a/b"]),
        lambda: device.delete_dir("/a/b"),
    ]
    for call in calls:
        with pytest.raises(WriteBlocked):
            call()

    assert fake_boox.state.mutations == [], "a read-only client must not send any write"


def test_reads_still_work_when_read_only(fake_boox):
    device = _device(fake_boox)
    assert device.ping() is True
    assert device.info().model == "FakeBoox"
    assert device.notes_count() == 0
    assert isinstance(device.list_dir("/storage/emulated/0/Books"), list)
    assert fake_boox.state.mutations == []


def test_writer_allows_mutations(fake_boox):
    device = _device(fake_boox, allow_write=True)
    device.create_dir("/storage/emulated/0/Books", "History")
    assert "/storage/emulated/0/Books/History" in fake_boox.state.dirs
    assert fake_boox.state.mutations


def test_writer_context_restores_read_only(fake_boox):
    device = _device(fake_boox)
    with device.writer():
        device.create_dir("/storage/emulated/0/Books", "tmp")
    assert device.allow_write is False
    with pytest.raises(WriteBlocked):
        device.create_dir("/storage/emulated/0/Books", "tmp2")


# ------------------------------------------------------------------ CLI paths


def _make_book(root, name: str, size: int = 5000) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def test_plan_command_never_mutates(tmp_path, config, fake_boox):
    import yaml

    _make_book(tmp_path, "Jane Doe - A History of Empires (1999).epub")
    _make_book(tmp_path, "John Roe - Collected Stories (2001).pdf")
    cfg = tmp_path / "library.yml"
    cfg.write_text(yaml.safe_dump(config.raw))

    code = main(["--config", str(cfg), "--report-dir", str(tmp_path / "reports"), "--host", fake_boox.host, "plan", "--json"])
    assert code == 0
    assert fake_boox.state.mutations == []


def test_sync_without_apply_uploads_nothing(tmp_path, config, fake_boox):
    import yaml

    _make_book(tmp_path, "Jane Doe - A History of Empires (1999).epub")
    cfg = tmp_path / "library.yml"
    cfg.write_text(yaml.safe_dump(config.raw))

    code = main(["--config", str(cfg), "--report-dir", str(tmp_path / "reports"), "--host", fake_boox.host, "sync"])
    assert code == 0
    assert fake_boox.state.mutations == []
    assert fake_boox.state.files == {}


def test_sync_with_apply_uploades(tmp_path, config, fake_boox):
    import yaml

    _make_book(tmp_path, "Jane Doe - A History of Empires (1999).epub")
    cfg = tmp_path / "library.yml"
    cfg.write_text(yaml.safe_dump(config.raw))

    code = main(["--config", str(cfg), "--report-dir", str(tmp_path / "reports"), "--host", fake_boox.host, "sync", "--apply"])
    assert code == 0
    assert fake_boox.state.files, "expected at least one uploaded file"
    path = next(iter(fake_boox.state.files))
    assert path.startswith("/storage/emulated/0/Books/History/")
    assert "Doe, Jane" in path and "(1999)" in path


def test_sync_is_idempotent(tmp_path, config, fake_boox):
    import yaml

    _make_book(tmp_path, "Jane Doe - A History of Empires (1999).epub")
    cfg = tmp_path / "library.yml"
    cfg.write_text(yaml.safe_dump(config.raw))

    main(["--config", str(cfg), "--report-dir", str(tmp_path / "reports"), "--host", fake_boox.host, "sync", "--apply"])
    before = dict(fake_boox.state.files)

    uploads_before = [p for m, p in fake_boox.state.mutations if p == "/api/storage/upload"]
    main(["--config", str(cfg), "--report-dir", str(tmp_path / "reports"), "--host", fake_boox.host, "sync", "--apply"])
    uploads_after = [p for m, p in fake_boox.state.mutations if p == "/api/storage/upload"]
    assert fake_boox.state.files == before
    # a second run must not re-upload anything
    assert len(uploads_after) == len(uploads_before) == 1


# ----------------------------------------------------------------------- wipe


def _seed_device(server: FakeBoox) -> None:
    server.state.files["/storage/emulated/0/Books/old.epub"] = 10
    server.state.files["/storage/emulated/0/Books/sub/other.pdf"] = 20
    server.state.dirs.add("/storage/emulated/0/Books/sub")


def test_wipe_requires_both_flags(tmp_path, config, fake_boox):
    import yaml

    _seed_device(fake_boox)
    cfg = tmp_path / "library.yml"
    cfg.write_text(yaml.safe_dump(config.raw))

    assert main(["--config", str(cfg), "--report-dir", str(tmp_path / "reports"), "--host", fake_boox.host, "wipe"]) == 0
    assert fake_boox.state.mutations == []

    # --apply alone is not enough
    assert main(["--config", str(cfg), "--report-dir", str(tmp_path / "reports"), "--host", fake_boox.host, "wipe", "--apply"]) == 3
    assert fake_boox.state.mutations == []
    assert fake_boox.state.files


def test_wipe_refuses_when_notes_exist(tmp_path, config, fake_boox):
    import yaml

    _seed_device(fake_boox)
    fake_boox.state.notes = 3
    cfg = tmp_path / "library.yml"
    cfg.write_text(yaml.safe_dump(config.raw))

    code = main(
        ["--config", str(cfg), "--report-dir", str(tmp_path / "reports"), "--host", fake_boox.host, "wipe", "--apply", "--confirm-wipe"]
    )
    assert code == 3
    assert fake_boox.state.mutations == []
    assert fake_boox.state.files, "annotations must protect the library"


def test_wipe_deletes_when_confirmed(tmp_path, config, fake_boox):
    import yaml

    _seed_device(fake_boox)
    cfg = tmp_path / "library.yml"
    cfg.write_text(yaml.safe_dump(config.raw))

    code = main(
        ["--config", str(cfg), "--report-dir", str(tmp_path / "reports"), "--host", fake_boox.host, "wipe", "--apply", "--confirm-wipe"]
    )
    assert code == 0
    assert fake_boox.state.files == {}
