"""Filename parsing and sanitisation."""

from __future__ import annotations

from booxlib.names import (
    author_folder,
    build_filename,
    fold,
    normalize_author,
    parse_filename,
    sanitize,
    strip_publisher_suffix,
)


def test_author_title_publisher_year():
    p = parse_filename("Andrew S. Grove - High Output Management-Vintage (2015).epub")
    assert p.title == "High Output Management"
    assert p.authors == ["Andrew S. Grove"]
    assert p.year == "2015"
    assert p.publisher == "Vintage"


def test_series_prefix_is_removed():
    p = parse_filename(
        "(Routledge Classics) Pierre Bourdieu - Distinction_ A Social Critique of the "
        "Judgement of Taste-Routledge (1986).epub"
    )
    assert p.series == "Routledge Classics"
    assert p.title.startswith("Distinction")
    assert p.authors == ["Pierre Bourdieu"]
    assert p.year == "1986"


def test_libgen_source_suffixes_are_stripped():
    p = parse_filename(
        "Dara Horn - People Love Dead Jews_ Reports from a Haunted Present "
        "(2021, W. W. Norton & Company) - libgen.li.epub"
    )
    assert "libgen" not in (p.title or "").lower()
    assert p.authors == ["Dara Horn"]
    assert p.year == "2021"
    assert p.publisher == "W. W. Norton & Company"


def test_annas_archive_format():
    p = parse_filename(
        "Anti-Judaism_ The Western Tradition -- Nirenberg, David -- 1, 2013 -- "
        "W_ W_ Norton & Company -- 9780393058246 -- abc123 -- Anna's Archive.epub"
    )
    assert p.title.startswith("Anti-Judaism")
    assert p.authors == ["David Nirenberg"]
    assert p.year == "2013"


def test_title_first_calibre_style():
    p = parse_filename("Flatland - Edwin A. Abbott.mobi")
    assert p.title == "Flatland"
    assert p.authors == ["Edwin A. Abbott"]


def test_title_by_author():
    p = parse_filename("A Gentleman in Moscow by Amor Towles.epub")
    assert p.title == "A Gentleman in Moscow"
    assert p.authors == ["Amor Towles"]


def test_hyphenated_titles_survive():
    p = parse_filename("Bernard Chapais - Primeval Kinship_ How Pair-Bonding Gave Birth.pdf")
    assert "Pair-Bonding" in p.title


def test_author_life_dates_removed():
    assert normalize_author("Chagnon, Napoleon A., 1938-") == "Napoleon A. Chagnon"
    assert normalize_author("Frazer, James George, Sir, 1854-1941") == "James George Frazer"
    assert normalize_author("Jenness, Diamond, 1886-1969") == "Diamond Jenness"


def test_comma_separated_author_list():
    from booxlib.names import _split_authors

    assert _split_authors("Lee Cronk, Napoleon Chagnon, William Irons") == [
        "Lee Cronk",
        "Napoleon Chagnon",
        "William Irons",
    ]
    # bibliographic form stays one person
    assert _split_authors("Chagnon, Napoleon A") == ["Chagnon, Napoleon A"]


def test_role_annotations_removed():
    from booxlib.names import _split_authors

    assert _split_authors("Gisli Palsson (edt)") == ["Gisli Palsson"]


def test_publisher_suffix_only_when_lexicon_matches():
    assert strip_publisher_suffix("Title-Harvard University Press") == (
        "Title",
        "Harvard University Press",
    )
    # a hyphenated title must not be mangled
    assert strip_publisher_suffix("Super-Cannes") == ("Super-Cannes", None)


# --------------------------------------------------------------- sanitising


def test_unicode_is_preserved():
    assert sanitize("The Yąnomamö") == "The Yąnomamö"
    assert sanitize("Carré") == "Carré"
    # Thai must survive: dropping it would make the book unidentifiable
    assert "กำเนิดสยาม" in sanitize("Siam Mapped กำเนิดสยามจากแผนที่")


def test_illegal_path_characters_removed():
    cleaned = sanitize('a/b\\c:d*e?f"g<h>i|j')
    for bad in '/\\:*?"<>|':
        assert bad not in cleaned


def test_control_characters_removed():
    assert "\x00" not in sanitize("Lotte\x00 (Qat)")
    assert "\x07" not in sanitize("bell\x07")


def test_fold_is_accent_insensitive():
    assert fold("Carré") == fold("Carre")
    assert fold("Yąnomamö") == fold("Yanomamo")


# ------------------------------------------------------------------ building


def test_build_filename_shape():
    assert build_filename(["Andrew S. Grove"], "High Output Management", "2015", "epub") == (
        "Andrew S. Grove - High Output Management (2015).epub"
    )


def test_build_filename_sort_style():
    assert (
        build_filename(["Andrew S. Grove"], "High Output", "2015", "epub", style="sort")
        == "Grove, Andrew S. - High Output (2015).epub"
    )


def test_build_filename_without_author_omits_prefix():
    assert build_filename([], "Flatland", "1884", "pdf") == "Flatland (1884).pdf"


def test_build_filename_three_authors_uses_et_al():
    name = build_filename(["A One", "B Two", "C Three"], "Book", "2000", "epub")
    assert "et al" in name


def test_author_folder_groups_by_surname():
    assert author_folder("Andrew S. Grove") == "Grove, Andrew S."


def test_long_titles_cut_on_word_boundary():
    name = build_filename(["A B"], "word " * 40, "2000", "epub", max_len=60)
    assert len(name) <= 61
    assert not name[:-5].endswith("wor")
