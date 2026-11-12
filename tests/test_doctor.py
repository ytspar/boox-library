"""The device holds two views of the library; these checks keep them honest."""

from dataclasses import replace

from booxlib.doctor import clear_ghosts, diagnose
from booxlib.device import Device
from booxlib.plan import build_plan
from booxlib.scan import scan
from booxlib.shelves import sync_shelves
from tests.fake_boox import FakeBoox


def _device(fake_boox, config, write=False):
    return Device(host=fake_boox.host, allow_write=write)


def _plan(config):
    return build_plan(scan(config), config)


def test_clean_device_has_no_findings(fake_boox, config):
    state = fake_boox.state
    path = "/storage/emulated/0/Books/History/Doe, Jane/Doe, Jane - Empires (1999).epub"
    state.files[path] = 1000

    report = diagnose(_device(fake_boox, config), config)

    assert report.disk_files == 1
    assert report.indexed == 1
    assert report.findings == []


def test_file_the_index_ignores_is_an_error(fake_boox, config):
    state = fake_boox.state
    good = "/storage/emulated/0/Books/History/a.epub"
    invisible = "/storage/emulated/0/Books/History/b.lit"
    state.files.update({good: 1000, invisible: 2000})
    state.unindexed.add(invisible)

    report = diagnose(_device(fake_boox, config), config)

    assert [f.kind for f in report.findings] == ["invisible"]
    finding = report.findings[0]
    assert finding.severity == "error"
    assert finding.paths == [invisible]
    assert ".lit" in finding.message and "lit: epub" in finding.message


def test_entry_without_a_file_is_a_warning(fake_boox, config):
    state = fake_boox.state
    ghost = "/storage/emulated/0/Books/History/gone.djvu"
    state.placement[ghost] = None

    report = diagnose(_device(fake_boox, config), config)

    assert [f.kind for f in report.findings] == ["ghost"]
    assert report.findings[0].severity == "warning"
    assert report.errors == []


def test_books_outside_their_shelf_are_reported(fake_boox, config, tmp_path):
    (tmp_path / "A History of Empires.epub").write_bytes(b"x" * 3000)
    cfg = replace(config, raw={**config.raw, "shelves": {"layout": "category"}})
    plan = _plan(cfg)
    target = plan.books[0].target_path(cfg.books_root)
    fake_boox.state.files[target] = 3000  # on the device, but unshelved

    report = diagnose(_device(fake_boox, cfg), cfg, plan)

    unshelved = report.of_kind("unshelved")
    assert len(unshelved) == 1 and unshelved[0].paths == [target]
    assert unshelved[0].severity == "warning"


def test_diagnose_is_read_only(fake_boox, config, tmp_path):
    (tmp_path / "A History of Empires.epub").write_bytes(b"x" * 3000)
    cfg = replace(config, raw={**config.raw, "shelves": {"layout": "category"}})
    plan = _plan(cfg)
    target = plan.books[0].target_path(cfg.books_root)
    fake_boox.state.files[target] = 3000

    diagnose(_device(fake_boox, cfg), cfg, plan)

    assert fake_boox.state.mutations == []


def test_fixing_shelves_clears_the_finding(fake_boox, config, tmp_path):
    (tmp_path / "A History of Empires.epub").write_bytes(b"x" * 3000)
    cfg = replace(config, raw={**config.raw, "shelves": {"layout": "category"}})
    plan = _plan(cfg)
    target = plan.books[0].target_path(cfg.books_root)
    fake_boox.state.files[target] = 3000

    sync_shelves(_device(fake_boox, cfg, write=True), plan, cfg, apply=True)
    report = diagnose(_device(fake_boox, cfg), cfg, plan)

    assert report.of_kind("unshelved") == []


def test_suffixed_shelf_names_are_reported_and_fixed(fake_boox, config, tmp_path):
    (tmp_path / "A History of Empires.epub").write_bytes(b"x" * 3000)
    cfg = replace(config, raw={**config.raw, "shelves": {"layout": "category"}})
    plan = _plan(cfg)
    target = plan.books[0].target_path(cfg.books_root)
    fake_boox.state.files[target] = 3000
    fake_boox.state.shelves["shelf-1"] = {"name": "History-1-1", "parent": None}
    fake_boox.state.placement[target] = "shelf-1"

    report = diagnose(_device(fake_boox, cfg), cfg, plan)
    finding = report.of_kind("shelf-names")[0]
    assert finding.severity == "warning"
    assert "History-1-1  ->  History" in finding.paths
    assert report.of_kind("unshelved") == []  # suffix is not a misfiling

    sync_shelves(_device(fake_boox, cfg, write=True), plan, cfg, apply=True)

    assert fake_boox.state.shelves["shelf-1"]["name"] == "History"
    assert diagnose(_device(fake_boox, cfg), cfg, plan).findings == []


def test_ghost_can_be_dropped_when_no_file_exists(fake_boox, config):
    state = fake_boox.state
    ghost = "/storage/emulated/0/Books/History/gone.djvu"
    state.placement[ghost] = None
    report = diagnose(_device(fake_boox, config), config)

    cleared = clear_ghosts(_device(fake_boox, config, write=True), config, report)

    assert cleared == 1
    assert ghost not in fake_boox.state.placement


def test_a_record_covering_a_real_file_is_never_deleted(fake_boox, config):
    """The device filesystem is case-insensitive: an old record whose path
    differs from the real file only by case must not be treated as a ghost."""
    state = fake_boox.state
    real = "/storage/emulated/0/Books/Fiction/Carré, John le/book.epub"
    stale = "/storage/emulated/0/Books/Fiction/Carre, John Le/book.epub"
    state.files[real] = 3000
    state.placement[stale] = None  # record kept by the index, file renamed

    report = diagnose(_device(fake_boox, config), config)
    cleared = clear_ghosts(_device(fake_boox, config, write=True), config, report)

    assert cleared == 0
    assert real in fake_boox.state.files
    assert state.mutations == []
