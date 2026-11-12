# boox-library

Turns a messy folder of downloaded ebooks into a clean, consistently named,
metadata-correct library on a **BOOX** e-reader — folders by category and
author, `Author - Title (Year).ext` filenames, and rewritten embedded
metadata — with no clicking on the device.

```
Books/
├── History/
│   └── Scott, James C/
│       └── Scott, James C - Against the Grain (2017).epub
├── Technology & Computing/
│   └── Grove, Andrew S/
│       └── Grove, Andrew S - High Output Management (2015).epub
└── Unsorted/
    └── Unknown Author/
        └── mcphee-draft-no-4.pdf
```

## Why this exists

The BOOX Drop web app can only do this by hand, one file at a time. Its
undocumented HTTP API can do the whole thing. This repository wraps that API,
plus Calibre's `ebook-meta`, into a repeatable command you can re-run whenever
the download folder grows.

## Safety

Every command is **read-only unless you pass `--apply`**, and this is enforced
in three independent layers:

1. `Device` is constructed read-only; mutating methods raise `WriteBlocked`.
2. The CLI never passes `allow_write=True` without `--apply`.
3. `wipe` additionally requires `--confirm-wipe`, and **refuses to delete
   anything if the device has notes/annotations** unless you also pass
   `--force`.

`tests/test_safety.py` runs the real code paths against a fake BOOX server and
asserts on the HTTP request journal, so an accidental write fails the build
even if the request would have "succeeded".

Nothing on the device is destructive until you run `wipe --apply --confirm-wipe`.

## Requirements

* Calibre (`brew install --cask calibre`) — provides `ebook-meta`. The macOS
  `.app` bundle path is detected automatically.
* Python 3.10+ with `requests` and `pyyaml`.
* The BOOX must be on the same network with **BOOX Drop** enabled
  (Settings → Transfer → BOOX Drop), by default on port 8085.

## Quick start

```bash
# 0. Configure: where your books are, and where the device is
cp config/library.example.yml config/library.yml
$EDITOR config/library.yml

# 1. What does the source tree contain?
bin/booxlib scan

# 2. What would the library look like?  (writes reports/plan-*.json + .csv)
bin/booxlib plan --json --unsorted 40

# 3. Inspect a specific decision
bin/booxlib explain "Primeval Kinship"

# 4. Confirm the device answers
bin/booxlib probe

# 5. Upload (dry run first, then for real)
bin/booxlib sync
bin/booxlib sync --apply

# 6. Check the device matches the plan
bin/booxlib verify
```

Starting over from scratch:

```bash
bin/booxlib wipe                      # show what would be deleted
bin/booxlib wipe --apply --confirm-wipe
bin/booxlib sync --apply
```

## Configuration

`config/library.yml` is yours and is **gitignored** — copy it from
`config/library.example.yml`. Only two values must be set:

```yaml
sources:
  root: ~/Downloads/books        # your messy folder
device:
  host: http://192.168.1.50:8085 # Settings -> Transfer -> BOOX Drop
```

The rest has working defaults: filename style, format preference (which edition
wins when you own several), the category rules, and the `.djvu -> .pdf`
conversion. Add `overrides:` entries to correct any individual book, using
`bin/booxlib explain "<title>"` to see why it was classified the way it was.

## Commands

| Command | Mutates device | Purpose |
|---|---|---|
| `scan` | no | list candidate files and their formats |
| `plan` | no | build the transfer plan; writes JSON/CSV reports |
| `explain <pattern>` | no | show how a file's metadata/title/category were decided |
| `probe` | only with `--apply` | round-trip test of every API call used |
| `sync` | only with `--apply` | upload the library |
| `wipe` | only with `--apply --confirm-wipe` | delete books from the device |
| `verify` | no | compare the device against the plan |

Useful flags: `--limit N` (first N books), `--replace` (overwrite existing),
`--include-artifacts`, `--host`, `--config`, `-v`.

## How metadata is decided

1. **Embedded metadata** via `ebook-meta`, which is authoritative for
   EPUB/MOBI/AZW3 because Calibre wrote it at conversion time.
2. **The filename**, parsed with ordered heuristics covering the conventions
   actually present in a real download folder: libgen
   (`Author - Title-Publisher (Year)`), Anna's Archive
   (`Title -- Author -- Year -- Publisher -- ISBN`), calibre
   (`Title - Author`), `[Series]` prefixes, `Title by Author`, and
   `{Author}(Year, Publisher)`.
3. **Scoring** decides between them. PDFs frequently carry junk
   (`Title: document1`) or *swapped* fields (`Title: Bruce G. Trigger`,
   `Author: Understanding Early Civilizations...`); both are detected and the
   filename wins.

Characters are preserved verbatim — Thai, Chinese and accented Latin all
survive, because destroying them would make those books unidentifiable.
Control characters and filesystem-illegal characters are removed.

## Categories

Categories are rules in `config/library.yml` (keyword lists, author/title
overrides, directory hints). First match wins; anything unmatched lands in
`Unsorted`. When a book lands in the wrong place, add an entry to `overrides`
and re-run — the pipeline is idempotent.

## Duplicates

Files with **identical bytes** collapse to one copy. Files that are the **same
work in different formats** (same author + title) collapse to the best format
per `formats.preference` (EPUB > AZW3 > MOBI > PDF > DJVU by default), with
exceptions for image-heavy categories. Broken EPUBs under
`formats.tiny_epub_bytes` lose to a real PDF.

## Format conversion

The BOOX file index **ignores `.djvu` entirely** — those files upload
"successfully" and then never appear in the reader. `formats.convert` in the
config maps source formats to what actually gets uploaded:

```yaml
  convert:
    djvu: pdf
```

This needs DjVuLibre (`brew install djvulibre`, provides `ddjvu`). Everything
else converts through Calibre's `ebook-convert`:

```yaml
  convert:
    djvu: pdf      # the reader cannot open DjVu at all
    lit: epub      # .lit is never indexed, so it is invisible in the Library
```

The failure mode is quiet and easy to miss: a format the index skips uploads
"successfully" and then simply never appears in the reader. `.djvu` and `.lit`
both behave this way on a Go 7. Converted books are named and indexed like any
other, so the usual `sync --apply --prune` flow replaces the originals.

## Bookshelves

The BOOX Library tab has its own grouping — *bookshelves* — which is separate
from the folders under `Books/`. Both are useful, and neither replaces the
other:

* **Folders** are where the files live. They survive a rescan, are visible to
  any app, and are what `sync` writes.
* **Shelves** are virtual. Grouping a book into a shelf does **not** move or
  copy the file — its `location` is unchanged. A book belongs to exactly one
  shelf, so shelves are a single-parent tree.

```bash
bin/booxlib shelves                        # dry run
bin/booxlib shelves --apply                # create shelves, group books
bin/booxlib shelves --layout category-author --apply   # nested: category/author
```

Layouts: `category` (20 shelves), `category-author` (category, then author
shelves — one level deeper, ~740 shelves), or `author`. Re-running is
idempotent: only missing shelves are created and only misplaced books move.

Newly uploaded books land unshelved at the library root, so the last step of a
sync is:

```bash
bin/booxlib sync --apply --prune && bin/booxlib shelves --apply
```

Shelves are **never deleted automatically**. The device's delete semantics are
not fully understood (a delete can remove an empty sibling shelf), and a stray
empty shelf is harmless, so removal stays a manual, opt-in action.

## Re-running

`sync` is incremental. It keeps a manifest (`reports/.cache/manifest.json`)
recording, per device path, the source fingerprint at upload time and the size
actually written. That matters because the uploaded file is *not* byte-identical
to the source — rewriting metadata changes its size — so comparing against the
source size would re-upload everything.

After changing a category or the naming rules, use `--prune` to delete the
copies the new plan no longer produces:

```bash
bin/booxlib sync --apply --prune
```

## Non-book artifacts

Typesetting files (`*.indd`, `*.vp`), print front-matter, archive box scans and
per-chapter PDF fragments are excluded by default and listed in the plan report
under `excluded`. Pass `--include-artifacts` to keep them.

## Tests

```bash
python3 -m pytest tests/ -q
```

## Notes on the BOOX API

Discovered by reading the BOOX Drop JavaScript bundle. Endpoints used:

```
GET  /api/device        GET  /api/storage        GET  /api/media/list?args={...}
GET  /api/note          GET  /api/library        GET  /api/ping
POST /api/storage/directory     {parent, name}
POST /api/storage/upload        multipart: dir, sender, file
POST /api/storage/rename        {file, name}
POST /api/storage/file/move     {parent, map:{selectedMap:{...}}}
POST /api/storage/batDelete     {selectedMap:{...}}
POST /api/storage/directory/delete
```

The device indexes **nested filesystem folders under `/storage/emulated/0/Books`
automatically**, so real directories are all that is needed — the separate
"library shelf" API is not required.
