"""Command-line interface for boox-library.

Every command is read-only unless ``--apply`` is passed, and ``wipe`` needs a
second confirmation flag on top of that.  Running the tool with no flags can
never modify the device.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from dataclasses import asdict
from typing import Any

from .config import ConfigError, load_config
from .device import BooxError, Device, WriteBlocked
from .manifest import Manifest
from .metadata import READ_ONLY_META, can_write_meta
from .produce import ConversionError, produce
from .doctor import clear_ghosts, diagnose
from .shelves import LAYOUTS, current_placement, sync_shelves
from .names import fold
from .plan import Plan, build_plan
from .scan import scan

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CACHE = os.path.join(REPO_ROOT, "reports", ".cache", "metadata.json")
DEFAULT_REPORT_DIR = os.path.join(REPO_ROOT, "reports")

log = logging.getLogger("booxlib")


# --------------------------------------------------------------------- output


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:,.1f} {unit}"
        n /= 1000
    return f"{n:,.1f} TB"


def _print_summary(plan: Plan) -> None:
    s = plan.summary()
    print()
    print("=" * 68)
    print(f"  Books to transfer : {s['books']:,}")
    print(f"  Total size        : {_human(s['bytes'])}")
    print(f"  Distinct authors  : {s['authors']:,}")
    print(f"  Duplicates dropped: {s['duplicates_dropped']:,}")
    print(f"  Non-book artifacts : {s['excluded_artifacts']:,}  (excluded)")
    print(f"  Name collisions   : {s['collisions']:,}")
    print(f"  Books with issues : {s['with_issues']:,}")
    print("=" * 68)
    print("  By category:")
    for name, count in s["categories"].items():
        print(f"    {count:5,d}  {name}")
    print()


def _write_reports(plan: Plan, out_dir: str, tag: str) -> tuple[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    json_path = os.path.join(out_dir, f"{tag}-{stamp}.json")
    csv_path = os.path.join(out_dir, f"{tag}-{stamp}.csv")

    payload = {
        "generated": stamp,
        "summary": plan.summary(),
        "books": [b.describe() for b in plan.books],
        "duplicates": [b.describe() for b in plan.duplicates],
        "excluded": [b.describe() for b in plan.excluded],
        "collisions": plan.collisions,
    }
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)

    import csv

    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["category", "author", "title", "year", "ext", "size_mb", "target", "source", "issues"]
        )
        for book in plan.books:
            writer.writerow(
                [
                    book.category,
                    "; ".join(book.authors),
                    book.title,
                    book.year or "",
                    book.ext,
                    f"{book.size / 1e6:.1f}",
                    book.target_relpath,
                    book.rel_path,
                    "; ".join(book.issues),
                ]
            )
    return json_path, csv_path


# ------------------------------------------------------------------ commands


def cmd_scan(args: argparse.Namespace, config) -> int:
    books = scan(config, cache_path=args.cache)
    total = sum(b.size for b in books)
    print(f"{len(books):,} candidate files, {_human(total)}")
    by_ext: dict[str, int] = {}
    for book in books:
        by_ext[book.ext] = by_ext.get(book.ext, 0) + 1
    for ext, count in sorted(by_ext.items(), key=lambda kv: -kv[1]):
        print(f"  {count:5,d}  .{ext}")
    if args.show:
        for book in books[: args.show]:
            print(f"  - {book.rel_path}")
            print(f"      title={book.title!r} authors={book.authors} year={book.year}")
    return 0


def cmd_explain(args: argparse.Namespace, config) -> int:
    """Show exactly how one or more files are interpreted."""
    books = scan(config, cache_path=args.cache)
    plan = build_plan(books, config, include_artifacts=True)
    dropped = {b.rel_path for b in plan.duplicates}

    needle = fold(args.pattern)
    matches = [b for b in plan.books if needle in fold(b.rel_path) or needle in fold(b.title)]
    matches += [b for b in plan.duplicates if needle in fold(b.rel_path)]
    if not matches:
        print(f"no match for {args.pattern!r}")
        return 1

    for book in matches[: args.limit]:
        print("=" * 72)
        print(f"source      : {book.rel_path}")
        print(f"size/format : {_human(book.size)}  .{book.ext}")
        print(f"embedded    : title={book.embedded.title!r}")
        print(f"              authors={book.embedded.authors} year={book.embedded.year}")
        print(f"              publisher={book.embedded.publisher!r} err={book.embedded.read_error}")
        print(f"filename    : title={book.parsed.title!r}")
        print(f"              authors={book.parsed.authors} year={book.parsed.year}")
        print(f"              publisher={book.parsed.publisher!r} conf={book.parsed.confidence}")
        print(f"RESOLVED    : {book.title!r}")
        print(f"              authors={book.authors}")
        print(f"              year={book.year} publisher={book.publisher!r}")
        print(f"category    : {book.category}  ({book.classification.reason if book.classification else ''})")
        print(f"target      : {book.target_relpath}")
        if book.issues:
            print(f"issues      : {'; '.join(book.issues)}")
        if book.rel_path in dropped:
            print(f"DUPLICATE   : {book.duplicate_reason}  ->  {book.duplicate_of}")
    return 0


def cmd_plan(args: argparse.Namespace, config) -> int:
    books = scan(config, cache_path=args.cache)
    plan = build_plan(books, config, include_artifacts=args.include_artifacts)
    _print_summary(plan)

    if args.show:
        selected = plan.books
        if args.category:
            wanted = fold(args.category)
            selected = [b for b in selected if fold(b.category) == wanted]
        for book in selected[: args.show]:
            print(f"  {book.target_relpath}")

    if args.unsorted:
        print("  Unsorted titles:")
        for book in [b for b in plan.books if b.category == config.fallback_category][: args.unsorted]:
            print(f"    {book.title!r}  <- {book.rel_path}")

    if args.json:
        path, csv_path = _write_reports(plan, args.report_dir, "plan")
        print(f"  report: {path}")
        print(f"  csv   : {csv_path}")

    if plan.excluded and args.show_excluded:
        print("  Excluded artifacts:")
        for book in plan.excluded[: args.show_excluded]:
            print(f"    - {book.rel_path}   [{'; '.join(book.issues)}]")

    if plan.duplicates and args.show_duplicates:
        print("  Duplicates dropped:")
        for book in plan.duplicates[: args.show_duplicates]:
            print(f"    - {book.rel_path}")
            print(f"        because {book.duplicate_reason}")

    return 0


def cmd_probe(args: argparse.Namespace, config) -> int:
    """Harmless round-trip test of every API operation we rely on."""
    device = Device(host=args.host or config.device_host, allow_write=args.apply)
    info = device.info()
    print(f"device : {info.model} / {info.nickname}")
    print(f"host   : {info.host}")
    print(f"storage: {info.storage_used} used of {info.storage_total}")
    print(f"free   : ~{_human(device.free_bytes())}")
    print(f"notes  : {device.notes_count()}")
    print(f"library: {len(device.library_books())} books indexed")
    print(f"mode   : {'READ-WRITE' if device.allow_write else 'read-only (dry run)'}")

    if not args.apply:
        print("\n(dry run — pass --apply to exercise create/upload/rename/delete)")
        return 0

    probe_dir = f"{config.books_root}/_booxlib_probe"
    sample = args.sample
    if not sample or not os.path.exists(sample):
        print("no --sample file given; skipping write test")
        return 0

    try:
        device.create_dir(config.books_root, "_booxlib_probe")
        device.upload(sample, probe_dir)
        print("upload   : ok")
        device.rename(f"{probe_dir}/{os.path.basename(sample)}", "renamed.bin")
        print("rename   : ok")
        device.delete_files([f"{probe_dir}/renamed.bin"])
        print("delete   : ok")
    finally:
        try:
            device.delete_dir(probe_dir)
            print("cleanup  : ok")
        except BooxError as exc:
            print(f"cleanup failed (remove {probe_dir} manually): {exc}")
    return 0


def _device_summary(device: Device) -> dict[str, Any]:
    entries = device.list_dir(device_books_root(device), recursive=True)
    files = [e for e in entries if not e.is_dir]
    return {
        "files": len(files),
        "bytes": sum(e.size for e in files),
        "names": {e.name for e in files},
        "sizes": {e.path: e.size for e in files},
    }


def device_books_root(device: Device) -> str:
    return "/storage/emulated/0/Books"


def cmd_sync(args: argparse.Namespace, config) -> int:
    books = scan(config, cache_path=args.cache)
    plan = build_plan(books, config, include_artifacts=args.include_artifacts)
    _print_summary(plan)

    device = Device(host=args.host or config.device_host, allow_write=args.apply)
    info = device.info()

    required = plan.total_bytes
    free = device.free_bytes()
    if required > free - config.min_free_bytes:
        print(f"ERROR: need {_human(required)} but only {_human(free)} is free")
        return 2

    books_root = config.books_root
    manifest = Manifest(os.path.join(args.report_dir, ".cache", "manifest.json"))

    existing = device.list_dir(books_root, recursive=True)
    # the device filesystem is case-insensitive, so index case-folded
    existing_files = {e.path.casefold(): e for e in existing if not e.is_dir}

    to_upload = []
    for book in plan.books:
        target = book.target_path(books_root)
        if args.replace:
            to_upload.append(book)
            continue
        if not manifest.needs_upload(target, book, existing_files.get(target.casefold())):
            book.uploaded = True
            continue
        to_upload.append(book)

    print(f"  already present : {len(plan.books) - len(to_upload):,}")
    print(f"  to upload       : {len(to_upload):,}")
    print()

    if not args.apply:
        print("DRY RUN — nothing will be changed. Pass --apply to execute.")
        if args.prune:
            _prune(device, plan, books_root, apply=False)
        if args.limit:
            for book in to_upload[: args.limit]:
                print(f"  would upload: {book.target_relpath}  ({_human(book.size)})")
        return 0

    tempdir = tempfile.mkdtemp(prefix="booxlib-")
    uploaded = failed = 0
    failures: list[dict[str, str]] = []
    try:
        for index, book in enumerate(to_upload[: args.limit or None], start=1):
            dest_dir = f"{books_root}/{book.target_dir}"
            staged = os.path.join(tempdir, book.target_name)
            try:
                device.ensure_dir(dest_dir, base=books_root)
                ok, message = produce(
                    book, staged,
                    title=book.title,
                    authors=book.authors,
                    year=book.year,
                    publisher=book.publisher,
                    language=book.language,
                )
                book.meta_written = ok
                if not ok:
                    book.issues.append(f"metadata: {message}")

                if args.replace:
                    dupe = device.exists(f"{dest_dir}/{book.target_name}")
                    if dupe:
                        device.delete_files([dupe.path])

                device.upload(staged, dest_dir, name=book.target_name)
                book.uploaded = True
                uploaded += 1
                manifest.record(
                    book.target_path(books_root), book, os.path.getsize(staged)
                )
                print(
                    f"  [{index}/{len(to_upload)}] {_human(book.size):>10}  "
                    f"{book.target_relpath}"
                    + ("" if ok else "   [filename-only]")
                )
            except (BooxError, OSError, ConversionError) as exc:
                failed += 1
                failures.append({"book": book.rel_path, "error": str(exc)})
                print(f"  [{index}/{len(to_upload)}] FAILED {book.rel_path}: {exc}")
            finally:
                if os.path.exists(staged):
                    os.remove(staged)
    finally:
        shutil.rmtree(tempdir, ignore_errors=True)

    manifest.save()

    if args.prune:
        _prune(device, plan, books_root, apply=True)

    log_path = os.path.join(args.report_dir, f"sync-{time.strftime('%Y%m%d-%H%M%S')}.json")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "device": asdict(info),
                "uploaded": uploaded,
                "failed": failed,
                "failures": failures,
                "actions": device.log.entries,
                "books": [b.describe() for b in plan.books],
            },
            fh, indent=2, ensure_ascii=False,
        )

    print()
    print(f"  uploaded: {uploaded:,}   failed: {failed:,}")
    print(f"  log: {log_path}")
    return 0 if failed == 0 else 1


def _prune(device: Device, plan: Plan, books_root: str, *, apply: bool) -> int:
    """Delete device files that the current plan no longer produces.

    Needed because renaming a book or changing its category leaves the old
    copy behind, which would show up twice in the library.
    """
    entries = device.list_dir(books_root, recursive=True)
    on_device = {e.path: e for e in entries if not e.is_dir}
    expected = {b.target_path(books_root).casefold() for b in plan.books}

    orphans = [
        path
        for path in on_device
        if path.casefold() not in expected
        and not os.path.basename(path).startswith("_")
    ]

    if not orphans:
        print("  prune: nothing to remove")
        return 0

    # directories that will be left empty
    orphan_dirs = set()
    for path in orphans:
        parent = os.path.dirname(path)
        still_used = any(
            p.casefold() in expected
            for p in on_device
            if os.path.dirname(p) == parent and p not in orphans
        )
        if not still_used:
            orphan_dirs.add(parent)

    print(f"  prune: {len(orphans):,} orphaned file(s), {len(orphan_dirs):,} empty folder(s)")
    if not apply:
        for path in orphans[:20]:
            print(f"    would delete: {path}")
        return 0

    for start in range(0, len(orphans), 100):
        device.delete_files(orphans[start:start + 100])

    for directory in sorted(orphan_dirs, key=lambda d: -d.count("/")):
        try:
            device.delete_dir(directory)
        except BooxError as exc:
            log.debug("could not remove %s: %s", directory, exc)

    print(f"  prune: removed {len(orphans):,} file(s)")
    return len(orphans)


def cmd_wipe(args: argparse.Namespace, config) -> int:
    device = Device(host=args.host or config.device_host, allow_write=args.apply)
    books_root = config.books_root

    notes = device.notes_count()
    entries = device.list_dir(books_root, recursive=True)
    files = [e for e in entries if not e.is_dir]
    # media/list hides folders; directory/tree is the only way to see them
    dirs = device.walk_dirs(books_root)
    total = sum(e.size for e in files)

    print(f"  device      : {device.info().nickname}")
    print(f"  {books_root}: {len(files):,} files, {_human(total)}, {len(dirs):,} folders")
    print(f"  notes       : {notes}")
    print()

    if notes and not args.force:
        print("REFUSING: device has notes/annotations. Pass --force to wipe anyway.")
        return 3

    if not args.apply:
        print("DRY RUN — pass --apply --confirm-wipe to delete these files.")
        for entry in files[: args.show]:
            print(f"  would delete: {entry.path}")
        return 0

    if not args.confirm_wipe:
        print("REFUSING: deletion needs --confirm-wipe as well as --apply.")
        return 3

    # delete files first, then the now-empty directories deepest-first
    for start in range(0, len(files), 100):
        batch = [e.path for e in files[start : start + 100]]
        device.delete_files(batch)
        print(f"  deleted {min(start + 100, len(files)):,}/{len(files):,} files")

    for entry in sorted(dirs, key=lambda e: -e.path.count("/")):
        try:
            device.delete_dir(entry.path)
        except BooxError as exc:
            log.warning("could not remove dir %s: %s", entry.path, exc)

    print("  wipe complete")
    return 0


def cmd_shelves(args: argparse.Namespace, config) -> int:
    """Group books into virtual bookshelves (the Library tab's own layers)."""
    layout = args.layout or config.shelves_layout
    if layout == "none":
        print("  shelves are disabled (set shelves.layout in the config,")
        print("  or pass --layout category|category-author|author)")
        return 0

    books = scan(config, cache_path=args.cache)
    plan = build_plan(books, config, include_artifacts=args.include_artifacts)
    device = Device(host=args.host or config.device_host, allow_write=bool(args.apply))

    summary = sync_shelves(device, plan, config, apply=args.apply, layout=layout)

    print(f"  layout       : {summary.layout}")
    print(f"  shelves      : {summary.shelves_needed:,} ({summary.shelves_missing:,} missing)")
    print(f"  books        : {summary.books:,}")
    if summary.shelves_renamed:
        print(f"  names fixed  : {summary.shelves_renamed:,}")
    if args.apply:
        print(f"  shelves made : {summary.shelves_created:,}")
        print(f"  books moved  : {summary.moved:,}")
    else:
        print(f"  already right: {summary.already_right:,}")
        print(f"  to move      : {summary.to_move:,}")
    if summary.failed:
        print(f"  failed       : {summary.failed:,}")
    for path in summary.missing_shelves:
        print(f"    new shelf: {'/'.join(path)}")

    report = os.path.join(args.report_dir, f"shelves-{time.strftime('%Y%m%d-%H%M%S')}.json")
    os.makedirs(args.report_dir, exist_ok=True)
    with open(report, "w") as fh:
        json.dump(summary.as_dict(), fh, indent=2)
    device.log.dump(report.replace(".json", "-actions.json"))

    print()
    if not args.apply:
        print("DRY RUN - nothing will be changed. Pass --apply to execute.")
    else:
        print("  done")
    print(f"  log: {report}")
    return 1 if summary.failed else 0


def cmd_doctor(args: argparse.Namespace, config) -> int:
    books = scan(config, cache_path=args.cache)
    plan = build_plan(books, config, include_artifacts=args.include_artifacts)
    host = args.host or config.device_host
    device = Device(host=host, allow_write=bool(args.fix))

    report = diagnose(device, config, plan)
    print(f"  files on device : {report.disk_files:,}")
    print(f"  in the library  : {report.indexed:,}")
    print(f"  shelves         : {report.shelves:,}")

    def show(rep) -> None:
        if not rep.findings:
            print("\n  no problems found")
        for finding in rep.findings:
            mark = "!" if finding.severity == "error" else "-"
            print(f"\n  {mark} {finding.message}")
            for path in finding.paths[: args.show]:
                print(f"      {path}")
            if len(finding.paths) > args.show:
                print(f"      ... and {len(finding.paths) - args.show:,} more")

    show(report)

    if args.fix and report.of_kind("ghost"):
        cleared = clear_ghosts(device, config, report)
        print(f"\n  dropped {cleared:,} library entr(y/ies) with no file behind them")

    if args.fix and (report.of_kind("unshelved") or report.of_kind("shelf-names")):
        summary = sync_shelves(device, plan, config, apply=True)
        print(f"\n  moved {summary.moved:,} book(s) into shelves, "
              f"renamed {summary.shelves_renamed:,} shelf(s)")

    if args.fix:
        report = diagnose(Device(host=host, allow_write=False), config, plan)
        show(report)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(args.report_dir, f"doctor-{stamp}.json")
    os.makedirs(args.report_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report.as_dict(args.show), handle, indent=2, ensure_ascii=False)
    print(f"\n  log: {path}")
    return 1 if report.errors else 0


def cmd_verify(args: argparse.Namespace, config) -> int:
    books = scan(config, cache_path=args.cache)
    plan = build_plan(books, config, include_artifacts=args.include_artifacts)
    device = Device(host=args.host or config.device_host, allow_write=False)

    manifest = Manifest(os.path.join(args.report_dir, ".cache", "manifest.json"))
    entries = device.list_dir(config.books_root, recursive=True)
    on_device = {e.path.casefold(): e for e in entries if not e.is_dir}

    missing, wrong_size, present = [], [], 0
    for book in plan.books:
        target = book.target_path(config.books_root)
        entry = on_device.get(target.casefold())
        if not entry:
            missing.append(book)
            continue
        record = manifest.get(target)
        # the uploaded file has rewritten metadata, so its size differs from the
        # source; only compare when the manifest knows what we actually sent
        if record and record.device_size != entry.size:
            wrong_size.append((book, entry.size))
        else:
            present += 1

    expected = {b.target_path(config.books_root).casefold() for b in plan.books}
    extra = [
        entry.path
        for key, entry in on_device.items()
        if key not in expected and not os.path.basename(entry.path).startswith("_")
    ]

    print(f"  expected : {len(plan.books):,}")
    print(f"  present  : {present:,}")
    print(f"  missing  : {len(missing):,}")
    print(f"  size diff: {len(wrong_size):,}")
    print(f"  extra    : {len(extra):,}")
    for book in missing[: args.show]:
        print(f"    missing: {book.target_relpath}")
    for book, size in wrong_size[: args.show]:
        print(f"    size:    {book.target_relpath} (device {_human(size)} vs {_human(book.size)})")
    for path in extra[: args.show]:
        print(f"    extra:   {path}")
    return 0 if not missing and not wrong_size else 1


# --------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="booxlib",
        description="Build a clean, metadata-correct ebook library on a Boox reader.",
    )
    parser.add_argument("-c", "--config", help="config file (default: config/library.yml)")
    parser.add_argument("--host", help="BooxDrop host, e.g. http://192.168.1.50:8085")
    parser.add_argument("--cache", default=DEFAULT_CACHE, help="metadata cache path")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "--report-dir", default=DEFAULT_REPORT_DIR, help="where to write reports"
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="list candidate files (read-only)")
    p_scan.add_argument("--show", type=int, default=0, help="print N parsed records")
    p_scan.set_defaults(func=cmd_scan)

    p_explain = sub.add_parser("explain", help="show how specific files are interpreted")
    p_explain.add_argument("pattern", help="substring of a source path or title")
    p_explain.add_argument("--limit", type=int, default=5)
    p_explain.set_defaults(func=cmd_explain)

    p_plan = sub.add_parser("plan", help="build the transfer plan (read-only)")
    p_plan.add_argument("--show", type=int, default=0, help="print N target paths")
    p_plan.add_argument("--category", help="with --show, limit to one category")
    p_plan.add_argument("--unsorted", type=int, default=0, help="print N Unsorted titles")
    p_plan.add_argument("--show-duplicates", type=int, default=0)
    p_plan.add_argument("--show-excluded", type=int, default=0,
                        help="print N excluded artifact paths")
    p_plan.add_argument("--include-artifacts", action="store_true",
                        help="upload typesetting files / chapter fragments too")
    p_plan.add_argument("--json", action="store_true", help="write JSON/CSV reports")
    p_plan.set_defaults(func=cmd_plan)

    p_probe = sub.add_parser("probe", help="check the device API end to end")
    p_probe.add_argument("--apply", action="store_true", help="also test upload/rename/delete")
    p_probe.add_argument("--sample", help="file to use for the write test")
    p_probe.set_defaults(func=cmd_probe)

    p_sync = sub.add_parser("sync", help="upload the library to the device")
    p_sync.add_argument("--apply", action="store_true", help="actually write to the device")
    p_sync.add_argument("--limit", type=int, default=0, help="only process N books")
    p_sync.add_argument("--replace", action="store_true", help="overwrite existing files")
    p_sync.add_argument("--include-artifacts", action="store_true")
    p_sync.add_argument(
        "--prune", action="store_true",
        help="delete device files the plan no longer produces (after renames)",
    )
    p_sync.set_defaults(func=cmd_sync)

    p_wipe = sub.add_parser("wipe", help="delete books from the device")
    p_wipe.add_argument("--apply", action="store_true", help="actually delete")
    p_wipe.add_argument("--confirm-wipe", action="store_true", help="second confirmation")
    p_wipe.add_argument("--force", action="store_true", help="wipe even if notes exist")
    p_wipe.add_argument("--show", type=int, default=10)
    p_wipe.set_defaults(func=cmd_wipe)

    p_shelves = sub.add_parser(
        "shelves", help="group books into device bookshelves (virtual, file paths unchanged)"
    )
    p_shelves.add_argument(
        "--layout", choices=list(LAYOUTS),
        help="shelf shape: category, category-author, or author (default: config)",
    )
    p_shelves.add_argument("--apply", action="store_true", help="actually create/move")
    p_shelves.add_argument("--include-artifacts", action="store_true")
    p_shelves.set_defaults(func=cmd_shelves)

    p_doctor = sub.add_parser(
        "doctor", help="check that the reader can see every file on the device"
    )
    p_doctor.add_argument("--fix", action="store_true", help="group misplaced books into shelves")
    p_doctor.add_argument("--show", type=int, default=10, help="paths to print per finding")
    p_doctor.add_argument("--include-artifacts", action="store_true")
    p_doctor.add_argument("--cache", default=DEFAULT_CACHE, help="metadata cache path")
    p_doctor.set_defaults(func=cmd_doctor)

    p_verify = sub.add_parser("verify", help="compare the device against the plan")
    p_verify.add_argument("--show", type=int, default=20)
    p_verify.add_argument("--include-artifacts", action="store_true")
    p_verify.set_defaults(func=cmd_verify)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    try:
        return args.func(args, config)
    except WriteBlocked as exc:
        print(f"blocked: {exc}", file=sys.stderr)
        return 3
    except BooxError as exc:
        print(f"device error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
