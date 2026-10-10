"""Counting and snippet rules. These need no JVM, so they run even without PyLucene."""

from app.text_matching import build_pattern, count_occurrences, make_snippets


def count(text, query, case_sensitive=False):
    return count_occurrences(text, build_pattern(query, case_sensitive))


def test_counts_repeated_occurrences():
    assert count("Fee, fee, FEE and fees.", "fee") == 3  # "fees" is a different word


def test_word_boundaries():
    assert count("react action act. Act!", "act") == 2


def test_phrase_spans_line_breaks():
    assert count("the force\n  majeure clause; force majeure again", "force majeure") == 2


def test_case_sensitive_flag():
    assert count("Fee fee FEE", "fee", case_sensitive=True) == 1


def test_matches_do_not_overlap():
    assert count("aaaa", "aa") == 0  # word boundaries forbid partial-word hits
    assert count("a a a", "a a") == 1  # non-overlapping: "a a" then a leftover "a"


def test_punctuation_and_unicode():
    assert count("e-mail me. E-MAIL!", "e-mail") == 2
    assert count("Größe und GRÖSSE; größe", "größe") == 2


def test_snippets_are_short_deduplicated_and_limited():
    text = ("padding " * 40) + "indemnity clause " + ("padding " * 40) + "indemnity clause " + ("x " * 100)
    snippets = make_snippets(text, build_pattern("indemnity"), limit=1)
    assert len(snippets) == 1 and "indemnity" in snippets[0] and len(snippets[0]) < 200
    assert snippets[0].startswith("...") and snippets[0].endswith("...")

    repeated = "see indemnity. see indemnity. see indemnity."
    assert len(make_snippets(repeated, build_pattern("indemnity"), limit=5)) == 1
