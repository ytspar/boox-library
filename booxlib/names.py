"""Parsing messy ebook filenames and building clean ones.

The source library is a decade of accumulated downloads, so filenames follow a
dozen different conventions (libgen, Anna's Archive, calibre, hand-named, and
occasionally random strings).  Embedded metadata is always preferred; this
module exists to (a) fill gaps, (b) recover the *year* and *publisher* that
metadata often omits, and (c) produce names that sort sensibly and are legal
on Android's exFAT/F2FS storage.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# ---------------------------------------------------------------- sanitising

#: characters that are illegal or troublesome on Android / exFAT / Boox
_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
#: punctuation we keep verbatim; other non-alphanumerics become spaces
_SAFE_PUNCT = set(" ,'&()+._#-")
_WS = re.compile(r"\s+")
#: control characters and NULs leak out of broken PDF/EPUB metadata
_CONTROL = re.compile(r"[\x00-\x08\x09\x0a-\x1f\x7f\u200b-\u200f\ufeff]")


#: how mojibake looks: a UTF-8 byte or two read through a single-byte codec
_MOJIBAKE = re.compile(r"[\u00c2\u00c3\u00c4\u00c5\u0102\u0139\u00e2\u20ac\u00d0\u00d1]")


def repair_mojibake(text: str) -> str:
    """Undo the classic é -> 'Ã©' -> 'Ă©' damage found in shipped metadata.

    A string is only rewritten when encoding it back through a single-byte
    codec yields valid UTF-8, so genuine text that happens to contain these
    characters ('SÃO PAULO') is left alone.
    """
    if not text or not _MOJIBAKE.search(text):
        return text
    for codec in ("cp1252", "latin-1", "cp1250", "cp1251", "mac_roman"):
        try:
            fixed = text.encode(codec).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if fixed != text and "\ufffd" not in fixed:
            return fixed
    return text


def clean_text(text: str) -> str:
    """Repair mojibake, then remove control characters and zero-width marks."""
    return _CONTROL.sub(" ", repair_mojibake(text or ""))

#: fold plain-ASCII lookalikes that NFKD does not decompose
_FOLD_EXTRA = str.maketrans(
    {
        "\u00df": "ss",  # ß
        "\u00e6": "ae",  # æ
        "\u0153": "oe",  # œ
        "\u00f8": "o",   # ø
        "\u0111": "d",   # đ
        "\u0127": "h",   # ħ
        "\u0142": "l",   # ł
        "\u00fe": "th",  # þ
        "\u00f0": "d",   # ð
        "\u0131": "i",   # ı
    }
)


def fold(text: str) -> str:
    """Accent-insensitive, case-insensitive form used for *matching* only.

    Filenames keep their real characters (Thai, Chinese and accented Latin all
    survive on Android); this is only for comparing things like override keys.
    """
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.translate(_FOLD_EXTRA).casefold()


def sanitize(text: str, *, max_len: int = 120) -> str:
    """Make *text* safe for a filename component on Android.

    Non-ASCII letters are preserved - the device handles UTF-8 fine, and
    destroying Thai or Chinese titles would make the library unusable.
    """
    text = unicodedata.normalize("NFC", clean_text(text))
    text = (
        text.replace("\u2019", "'")
        .replace("\u2018", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2013", "-")
        .replace("\u2014", "-")
    )
    text = _ILLEGAL.sub(" ", text)
    kept = []
    for char in text:
        if char.isalnum() or char in _SAFE_PUNCT:
            kept.append(char)
        elif unicodedata.category(char).startswith("M"):
            kept.append(char)  # leave combining diacritics attached
        else:
            kept.append(" ")
    text = _WS.sub(" ", "".join(kept)).strip()
    text = re.sub(r"\.{2,}$", "", text)  # collapse trailing ellipses, keep initials
    if len(text) > max_len:
        text = text[:max_len].rsplit(" ", 1)[0].rstrip(" .,")
    return text or "Untitled"


#: suffix markers appended by warez/scene sites
_SITE_SUFFIX = re.compile(
    r"\s*[-\u2013\u2014]\s*(libgen\.(?:li|lc|is|rs|com)|z-lib\.org|z-lib|libgen|"
    r"anna'?s\s+archive|epublib\.com|freepaidbooks\.online|royallib\.com|"
    r"freepaidbooks|kat\.ph)\s*$",
    re.IGNORECASE,
)
_DUP_SUFFIX = re.compile(r"\s*\((\d{1,2})\)\s*$")
_COPY_SUFFIX = re.compile(r"\s+copy\s*$", re.IGNORECASE)


def clean_stem(stem: str) -> str:
    """Strip site suffixes, duplicate markers and stray whitespace."""
    stem = stem.strip()
    for _ in range(4):  # a name may carry several stacked suffixes
        before = stem
        stem = _SITE_SUFFIX.sub("", stem)
        stem = _DUP_SUFFIX.sub("", stem)
        stem = _COPY_SUFFIX.sub("", stem)
        stem = stem.strip()
        if stem == before:
            break
    stem = re.sub(r"\s*[-\u2013\u2014]\s*$", "", stem)
    return _WS.sub(" ", stem).strip(" ._-")


# ------------------------------------------------------- publisher detection

#: words that betray a publisher in a bare dash-suffix.  libgen's dominant
#: convention is "Author - Title-Publisher (Year)", and the dash is usually
#: glued to the publisher, so a lexicon is the only safe way to split it
#: without destroying titles like "Super-Cannes" or "Pair-Bonding".
_PUB_WORDS = re.compile(
    # strong generic markers: essentially never part of a book title
    r"\b(press|publishing|publishers?|university|univ|publications?|verlag|"
    r"classics|paperback|hardcover|inc|ltd|llc)\b"
    r"|"
    # distinctive imprints.  Deliberately excludes common English words that
    # also appear in titles (Little, Brown, Basic, Free, Crown, Times, ...)
    r"\b(routledge|penguin|harmondsworth|harvard|cambridge|oxford|yale|princeton|"
    r"vintage|knopf|norton|schuster|bloomsbury|macmillan|mcgraw|pearson|wiley|"
    r"springer|elsevier|hachette|harpercollins|faber|verso|scribner|doubleday|"
    r"cengage|canongate|silkworm|iseas|brill|reaktion|kogan|manning|o'reilly|"
    r"apress|packt|addison|prentice|houghton|mifflin|farrar|straus|giroux|"
    r"rinehart|winston|publicaffairs|algonquin|createspace|benbella|greenleaf|"
    r"patagonia|aasgaard|north river|black irish|tordotcom|tor books|del rey|"
    r"gollancz|picador|granta|daedalus|thames|hudson|phaidon|taschen)\b",
    re.IGNORECASE,
)

#: "3rd ed.", "2nd edition", ", 6th ed" noise inside titles
_EDITION_NOISE = re.compile(
    r"[,;]?\s*\b\d+(?:st|nd|rd|th)\s+(?:ed\.?|edition)\b\.?", re.IGNORECASE
)


def strip_edition_noise(title: str) -> str:
    cleaned = _EDITION_NOISE.sub("", title or "")
    return _WS.sub(" ", cleaned).strip(" ,;-\u2013")


def contains_publisher_word(text: str) -> bool:
    """True when *text* looks like a publishing house rather than a person."""
    return bool(_PUB_WORDS.search(text or ""))


def strip_publisher_suffix(text: str) -> tuple[str, str | None]:
    """Split a *glued* ``Title-Publisher`` suffix using a publisher lexicon.

    The separator must have no space after it: libgen writes
    ``Title-Publisher (Year)``, whereas a spaced `` - `` is the author/title
    separator.  Without that rule, ``John - Call for the Dead (Penguin, 2012)``
    would lose its title to "Penguin".
    """
    match = re.match(
        r"^(?P<head>.+?)[-\u2013\u2014](?P<tail>\S[^-\u2013\u2014]{2,59})$", text or ""
    )
    if not match:
        return text, None
    tail = match.group("tail").strip(" ,;")
    words = tail.split()
    if not 1 <= len(words) <= 5 or not _PUB_WORDS.search(tail):
        return text, None
    # a real imprint never contains a year or title prose
    if re.search(r"(?<!\d)(1[5-9]\d{2}|20[0-4]\d)(?!\d)", tail):
        return text, None
    if looks_like_title(tail):
        return text, None
    head = match.group("head").strip(" ,;-\u2013_")
    if len(head) < 3:
        return text, None
    return head, tail


# ---------------------------------------------------------------- parsing

YEAR = re.compile(r"(?<!\d)(1[5-9]\d{2}|20[0-4]\d)(?!\d)")
_ANNA = re.compile(r"\s+--\s+Anna['\u2019]s\s+Archive", re.IGNORECASE)

#: "...{Author}(Year, Publisher){id}"  (newer Anna's Archive / libgen style)
_BRACED = re.compile(
    r"^(?P<left>.+?)\{(?P<author>[^{}]+)\}\((?P<year>\d{4})(?:,\s*(?P<pub>[^)]*))?\)"
)
#: "Title_ Subtitle-Publisher (Year)"
_TRAILING_YEAR = re.compile(
    r"^(?P<body>.*?)[\s]*\((?P<year>\d{4})(?:,\s*(?P<pub>[^)]*))?\)\s*$"
)
#: the reverse: "Title-Publisher (Penguin, 2012)"
_TRAILING_PUB_YEAR = re.compile(
    r"^(?P<body>.*?)[\s]*\((?P<pub>[^(),]{2,60}),\s*(?P<year>\d{4})\)\s*$"
)
#: leading "[Series 1]" / "(Penguin Classics)" annotations
_SERIES_PREFIX = re.compile(r"^[\[(](?P<series>[^\])]+)[\])]\s*")

_HONORIFICS = {"sir", "dame", "dr", "prof", "professor", "mr", "mrs", "ms", "rev", "hon"}

#: words that appear in titles but essentially never in personal names.  Used
#: to break ties between "Andrew S. Grove" and "High Output Management".
_TITLE_WORDS = {
    "the", "a", "an", "of", "and", "or", "in", "on", "for", "to", "with", "at",
    "by", "from", "how", "why", "what", "when", "where", "who", "is", "are",
    "was", "were", "be", "been", "it", "its", "this", "that", "these", "those",
    "my", "your", "his", "her", "their", "our", "new", "old", "great",
    "little", "big", "last", "first", "best", "worst", "more", "most", "less",
    "world", "life", "time", "story", "stories", "history", "war", "book",
    "guide", "manual", "art", "science", "theory", "practice", "introduction",
    "essentials", "complete", "collected", "selected", "novel", "tales",
    "notes", "letters", "essays", "people", "city", "mind", "power", "state",
    "against", "beyond", "after", "before", "among", "between", "not", "no",
}


def looks_like_title(text: str) -> bool:
    """True when *text* contains a word that marks it as a title."""
    words = {w.lower().strip(".,;:!?'\"()[]") for w in (text or "").split()}
    return bool(words & _TITLE_WORDS)


def is_middle_initial(text: str) -> bool:
    """'Andrew S. Grove' has one; 'High Output Management' does not."""
    return bool(re.search(r"\b[A-Z]\.", text or ""))
_ROLE_PAREN = re.compile(
    r"\(\s*(?:auth|author|editor|edt|eds|ed|trans|translator|comp|illustrator)\.?\s*\)",
    re.IGNORECASE,
)


@dataclass
class ParsedName:
    """Best-effort decomposition of a filename."""

    title: str = ""
    authors: list[str] = field(default_factory=list)
    year: str | None = None
    publisher: str | None = None
    series: str | None = None
    confidence: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.title) and self.confidence >= 0.5


def normalize_author(name: str) -> str:
    """Turn "Chagnon, Napoleon A., 1938-" into "Napoleon A. Chagnon"."""
    name = _WS.sub(" ", clean_text(name)).strip(" ,;")
    if not name:
        return ""
    name = _ROLE_PAREN.sub("", name)
    # trailing life dates:  ", 1854-1941"  /  ", 1938-"
    name = re.sub(r",?\s*\d{4}\s*[-\u2013]\s*\d{0,4}\s*$", "", name)
    name = name.strip(" ,;")
    if "," in name:
        parts = [p.strip() for p in name.split(",") if p.strip()]
        parts = [p for p in parts if p.lower().strip(".") not in _HONORIFICS]
        if len(parts) >= 2:
            name = f"{' '.join(parts[1:])} {parts[0]}"
        elif parts:
            name = parts[0]
    words = [w for w in name.split() if w.lower().strip(".") not in _HONORIFICS]
    return " ".join(words).strip(" ,;.")


def _split_authors(text: str) -> list[str]:
    """Split an author blob on the separators libgen/AA actually use."""
    text = (text or "").strip()
    if not text:
        return []
    lowered = text.lower()
    if "_" in text:
        parts = [p for p in text.split("_") if p.strip()]
    elif " & " in text:
        parts = [p.strip() for p in text.split(" & ")]
    elif " and " in lowered and lowered.count(" and ") == 1:
        parts = re.split(r"\s+and\s+", text, flags=re.IGNORECASE)
    elif "," in text and _looks_like_author_list(text):
        # "Lee Cronk, Napoleon Chagnon, William Irons" is three people;
        # "Chagnon, Napoleon A." is one person in bibliographic order.
        parts = [p.strip() for p in text.split(",") if p.strip()]
    else:
        parts = [text]

    out: list[str] = []
    for part in parts:
        part = _ROLE_PAREN.sub("", part.replace("_", " "))
        part = _WS.sub(" ", part).strip(" ,;.")
        if part and part.lower() not in {"unknown", "n/a", "na", "none"}:
            out.append(part)
    return out


def _looks_like_author_list(text: str) -> bool:
    """True when a comma-separated blob lists several people, not Last, First."""
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if len(parts) < 2:
        return False
    # bibliographic "Last, First" has a single-word surname first
    if len(parts) == 2 and len(parts[0].split()) == 1:
        return False
    return all(len(p.split()) >= 2 for p in parts)


def _looks_like_person(text: str) -> bool:
    """Crude author detector used only to disambiguate 'A - B' stems."""
    # Strip role annotations but do NOT split: splitting on '_' or ' & ' would
    # silently truncate a title into something that looks like a name.
    text = _ROLE_PAREN.sub("", text or "").strip(" ,;")
    if not text or YEAR.search(text):
        return False
    if any(ch in text for ch in ":;?!"):
        return False
    if _PUB_WORDS.search(text):
        return False
    words = text.split()
    if not 1 < len(words) <= 6 or len(text) > 60:
        return False
    # "The Yanomamo" is a title, not a person
    if words[0].lower().strip(",.") in {"the", "a", "an", "of", "on", "in", "and", "for", "to"}:
        return False
    if looks_like_title(text) and not is_middle_initial(text):
        return False
    if re.fullmatch(r"[A-Z][\w'\u2019.\-]+(?:\s+[A-Z][\w'\u2019.\-]+){0,3},\s*\S.*", text):
        return True  # "Last, First"
    return all(
        w[0].isupper()
        or w[0] in ".-_"
        or w.lower() in {"de", "van", "von", "del", "la", "le", "al", "da", "di", "bin", "ibn"}
        for w in words
        if w
    )


def parse_filename(filename: str) -> ParsedName:
    """Decompose a filename into title / authors / year / publisher.

    Ordered heuristics, most-specific first.  Anything ambiguous is returned
    with a low ``confidence`` so the caller can route it to manual review
    rather than guessing.
    """
    stem, _, _ext = filename.rpartition(".") if "." in filename else (filename, "", "")
    result = ParsedName()
    notes = result.notes

    # Anna's Archive:  Title -- Author -- Ed,Year -- Pub -- ISBN -- hash -- AA
    if _ANNA.search(stem):
        body = _ANNA.split(stem)[0]
        parts = [p.strip() for p in re.split(r"\s+--\s+", body)]
        if len(parts) >= 3:
            result.title = parts[0]
            result.authors = _split_authors(parts[1])
            for chunk in parts[2:]:
                hit = YEAR.search(chunk)
                if hit and not result.year:
                    result.year = hit.group(1)
                if not result.publisher and not YEAR.search(chunk) and len(chunk) > 2:
                    result.publisher = chunk
            result.confidence = 0.9
            notes.append("annas-archive")
            return _finalise(result)

    stem = clean_stem(stem)

    # leading "[Series] Title ..."
    match = _SERIES_PREFIX.match(stem)
    if match:
        result.series = match.group("series").strip()
        stem = stem[match.end():].strip()

    # "...{Author}(Year, Publisher){id}"
    match = _BRACED.match(stem)
    if match:
        result.title = match.group("left").strip(" -_")
        result.authors = _split_authors(match.group("author"))
        result.year = match.group("year")
        result.publisher = (match.group("pub") or "").strip() or None
        result.confidence = 0.85
        notes.append("braced")
        return _finalise(result)

    # strip a trailing "(Year[, Publisher])"
    body = stem
    match = _TRAILING_YEAR.match(stem)
    if match:
        body = match.group("body").strip(" -_")
        result.year = match.group("year")
        publisher = (match.group("pub") or "").strip(" ,")
        if publisher and len(publisher) > 2:
            result.publisher = publisher
    else:
        match = _TRAILING_PUB_YEAR.match(stem)
        if match:
            body = match.group("body").strip(" -_")
            result.year = match.group("year")
            result.publisher = match.group("pub").strip(" ,")

    # strip a glued "-Publisher" suffix *before* deciding which half is which
    body, glued_publisher = strip_publisher_suffix(body)
    if glued_publisher and not result.publisher:
        result.publisher = glued_publisher

    # split "Author - Title" into halves
    halves = re.split(r"\s+[-\u2013\u2014]\s+", body, maxsplit=1)
    if len(halves) == 2:
        left, right = halves[0].strip(), halves[1].strip()
        if _looks_like_person(left) and not _looks_like_person(right):
            result.authors, title_part = _split_authors(left), right
            result.confidence = 0.8
        elif _looks_like_person(right) and not _looks_like_person(left):
            result.authors, title_part = _split_authors(right), left
            result.confidence = 0.7
            notes.append("title-first")
        elif _looks_like_person(left):
            # both halves look like names; "Author - Title" is by far the
            # dominant convention in this library, so keep the left as author
            result.authors, title_part = _split_authors(left), right
            result.confidence = 0.55
            notes.append("assumed-author-first")
        else:
            title_part = body
            result.confidence = 0.4
            notes.append("ambiguous-split")
    else:
        title_part = body
        result.confidence = 0.4

    # leftovers from the underscore-as-colon convention
    title_part = re.sub(r"_+", " ", title_part).strip(" -_:")

    # "Title by Author"
    if not result.authors:
        match = re.search(r"\s+by\s+(?P<auth>[A-Z][\w.'\u2019\- ]{2,60})$", title_part)
        if match:
            result.authors = _split_authors(match.group("auth"))
            title_part = title_part[: match.start()].strip()
            result.confidence = max(result.confidence, 0.65)

    result.title = title_part.strip(" -_:")

    # year may still be hiding inside the title
    if not result.year:
        match = YEAR.search(result.title)
        if match:
            result.year = match.group(1)
            result.title = (
                result.title[: match.start()] + result.title[match.end():]
            ).strip(" ,-()")

    return _finalise(result)


def strip_author_prefix(title: str, authors: list[str]) -> str:
    """Remove an author name that leaked into the title."""
    if not title or not authors:
        return title
    folded_title = fold(title)
    for author in authors:
        candidates = {
            fold(author),
            fold(author_folder(author, "sort")),
            fold(author_display(author, "sort")),
            fold(author_display(author, "natural")),
        }
        for form in candidates:
            if not form:
                continue
            for sep in (" - ", " _ ", ": ", ", "):
                if folded_title.startswith(form + fold(sep)):
                    return title[len(form) + len(sep):].lstrip(" -_:,")
    return title


def _finalise(result: ParsedName) -> ParsedName:
    result.title = _WS.sub(" ", result.title or "").strip(" -_:,")
    result.title = strip_edition_noise(result.title)
    result.authors = [normalize_author(a) for a in result.authors if a.strip(" ,;")]
    result.authors = [a for a in result.authors if a]
    if result.publisher:
        result.publisher = _WS.sub(" ", result.publisher).strip(" ,;.") or None
    if result.year and not re.fullmatch(r"\d{4}", result.year):
        result.year = None
    result.title = strip_author_prefix(result.title, result.authors)
    result.title = strip_edition_noise(result.title)
    if not result.title:
        result.confidence = 0.0
    return result


# ------------------------------------------------------------- display names


def split_person(name: str) -> tuple[str, str]:
    """Return (forenames, surname) for a person string."""
    name = (name or "").strip()
    if "," in name:
        last, _, first = name.partition(",")
        return first.strip(), last.strip()
    parts = name.split()
    if len(parts) == 1:
        return "", parts[0]
    return " ".join(parts[:-1]), parts[-1]


def author_display(name: str, style: str = "natural") -> str:
    """Render an author name for a filename."""
    first, last = split_person(name)
    if not first:
        return last
    if style == "sort":
        return f"{last}, {first}"
    return f"{first} {last}"


def author_folder(name: str, style: str = "sort") -> str:
    """Directory name for an author.  Default groups by surname."""
    first, last = split_person(name)
    if not first:
        return last
    return f"{last}, {first}" if style == "sort" else f"{first} {last}"


def primary_author_key(name: str) -> str:
    """Order-insensitive author identity used for grouping/dedupe.

    "John Le Carre" and "Le Carre, John" are the same person, but splitting on
    surname alone makes them "carre" and "lecarre" because the particle moves.
    Sorting the name tokens sidesteps surname-particle rules entirely.
    """
    return " ".join(sorted(re.findall(r"[a-z]+", fold(name))))


def build_filename(
    authors: list[str],
    title: str,
    year: str | None,
    ext: str,
    *,
    style: str = "natural",
    max_len: int = 140,
) -> str:
    """``Author - Title (Year).ext`` - the canonical shape for this library."""
    rendered = [author_display(a, style) for a in authors if a]
    if len(rendered) > 2:
        author_part = sanitize(f"{rendered[0]} et al.", max_len=80)
    elif rendered:
        author_part = sanitize(" & ".join(rendered), max_len=80)
    else:
        author_part = ""

    title_part = sanitize(title, max_len=max_len)
    year_part = f" ({year})" if year else ""

    stem = f"{author_part} - {title_part}{year_part}" if author_part else f"{title_part}{year_part}"
    if len(stem) > max_len:
        stem = stem[:max_len].rsplit(" ", 1)[0].rstrip(" .,-\u2013")
    return f"{stem}.{ext.lstrip('.')}"
