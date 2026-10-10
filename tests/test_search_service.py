"""Tests of the real Lucene index. Skipped (not faked) when PyLucene is not installed."""

import os

import pytest

pytest.importorskip("lucene", reason="PyLucene is not installed; Lucene tests cannot run (see README)")

from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

from app.search_service import PdfSearchIndex, SearchError  # noqa: E402

LIMIT_BYTES = 10_000_000


def write_pdf(root, name, pages):
    """Write a PDF with one page per string in ``pages`` (each string is drawn line by line)."""
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(path), pagesize=A4)
    for text in pages:
        y = 780
        for line in text.split("\n"):
            pdf.drawString(50, y, line)
            y -= 14
        pdf.showPage()
    pdf.save()
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))  # guarantee a visible mtime change


@pytest.fixture
def index(tmp_path):
    root = tmp_path / "pdfs"
    root.mkdir()
    search_index = PdfSearchIndex(root, tmp_path / "index", LIMIT_BYTES)
    search_index.root = root  # convenience for the tests
    yield search_index
    search_index.close()


def search(index, query, **kwargs):
    return index.search(query, kwargs.pop("limit", 10), **kwargs)


def test_word_present_in_one_pdf(index):
    write_pdf(index.root, "a.pdf", ["The indemnity clause applies."])
    write_pdf(index.root, "b.pdf", ["Nothing relevant here."])
    result = search(index, "indemnity")
    assert [r["path"] for r in result["results"]] == ["a.pdf"]
    assert result["total_matching_pdfs"] == 1
    assert "indemnity" in result["results"][0]["snippets"][0]


def test_phrase_across_multiple_pdfs_and_only_exact_phrase(index):
    write_pdf(index.root, "a.pdf", ["The force majeure clause."])
    write_pdf(index.root, "sub/b.pdf", ["Subject to force majeure conditions."])
    write_pdf(index.root, "c.pdf", ["majeure force is not the phrase; force alone; majeure alone"])
    result = search(index, "force majeure")
    assert sorted(r["path"] for r in result["results"]) == ["a.pdf", "sub/b.pdf"]


def test_counts_repeated_occurrences_and_pages(index):
    write_pdf(index.root, "a.pdf", ["liability and Liability\nLIABILITY", "no match here", "one more liability"])
    result = search(index, "liability")["results"][0]
    assert result["occurrences"] == 4
    assert result["pages"] == [1, 3]
    assert all("[p." in s for s in result["snippets"])


def test_results_sorted_by_occurrences_with_path_tiebreak(index):
    write_pdf(index.root, "few.pdf", ["term"])
    write_pdf(index.root, "many.pdf", ["term term term term"])
    write_pdf(index.root, "b_tie.pdf", ["term term"])
    write_pdf(index.root, "a_tie.pdf", ["term term"])
    paths = [r["path"] for r in search(index, "term")["results"]]
    assert paths == ["many.pdf", "a_tie.pdf", "b_tie.pdf", "few.pdf"]
    assert [r["path"] for r in search(index, "term", limit=2)["results"]] == ["many.pdf", "a_tie.pdf"]
    assert search(index, "term", limit=2)["total_matching_pdfs"] == 4


def test_absent_word_returns_empty_result(index):
    write_pdf(index.root, "a.pdf", ["hello world"])
    result = search(index, "zebra")
    assert result["results"] == [] and result["total_matching_pdfs"] == 0


def test_case_sensitive_search(index):
    write_pdf(index.root, "a.pdf", ["Apple apple APPLE"])
    assert search(index, "apple")["results"][0]["occurrences"] == 3
    assert search(index, "apple", case_sensitive=True)["results"][0]["occurrences"] == 1
    assert search(index, "pineapple", case_sensitive=True)["results"] == []


def test_no_stemming_and_no_partial_words(index):
    write_pdf(index.root, "a.pdf", ["contracts and reacted"])
    assert search(index, "contract")["results"] == []
    assert search(index, "act")["results"] == []


def test_query_syntax_is_treated_literally(index):
    write_pdf(index.root, "a.pdf", ["alpha beta"])
    for query in ['alpha AND (beta', 'alpha*', 'content:alpha', '"unbalanced', 'alpha OR zzz', 'alpha \\ beta']:
        search(index, query)  # must not raise a parser error
    # '*' is literal punctuation, not a wildcard: the text has no "alpha*", so nothing is counted.
    assert search(index, "alpha*")["results"] == []
    assert search(index, "alpha")["results"][0]["path"] == "a.pdf"


@pytest.mark.parametrize("bad_query", ["", "   ", "!!! ???", "\\", "x" * 501])
def test_unusable_queries_raise_search_error(index, bad_query):
    with pytest.raises(SearchError):
        search(index, bad_query)


def test_new_modified_and_deleted_pdfs_are_reflected(index):
    write_pdf(index.root, "a.pdf", ["old content"])
    assert search(index, "old")["total_matching_pdfs"] == 1

    write_pdf(index.root, "a.pdf", ["brand new text"])  # modified
    assert search(index, "old")["results"] == []
    assert search(index, "brand")["total_matching_pdfs"] == 1

    write_pdf(index.root, "b.pdf", ["brand again"])  # created
    assert search(index, "brand")["total_matching_pdfs"] == 2

    (index.root / "a.pdf").unlink()  # deleted
    result = search(index, "brand")
    assert [r["path"] for r in result["results"]] == ["b.pdf"]


def test_repeated_sync_does_not_duplicate_pages(index):
    write_pdf(index.root, "a.pdf", ["one", "two", "three"])
    first = index.sync()
    assert first["indexed"] == ["a.pdf"]
    pages_after_first = index.page_count()
    for _ in range(3):
        report = index.sync()
        assert report["unchanged"] == 1 and not report["indexed"] and not report["updated"]
    assert index.page_count() == pages_after_first == 3
    assert index.sync(full_rebuild=True)["indexed"] == ["a.pdf"]
    assert index.page_count() == 3
    assert search(index, "one")["results"][0]["occurrences"] == 1


def test_malformed_pdf_is_reported_without_stopping_indexing(index):
    (index.root / "broken.pdf").write_bytes(b"%PDF-1.4 definitely not a pdf")
    write_pdf(index.root, "good.pdf", ["valid searchable text"])
    report = index.sync()
    assert [f["path"] for f in report["failed"]] == ["broken.pdf"]
    assert "good.pdf" in report["indexed"]
    assert search(index, "searchable")["total_matching_pdfs"] == 1


def test_index_survives_restart(tmp_path):
    root = tmp_path / "pdfs"
    root.mkdir()
    write_pdf(root, "a.pdf", ["persisted words"])
    first = PdfSearchIndex(root, tmp_path / "index", LIMIT_BYTES)
    first.sync()
    first.close()
    second = PdfSearchIndex(root, tmp_path / "index", LIMIT_BYTES)
    try:
        assert second.sync()["unchanged"] == 1  # nothing re-extracted
        assert second.search("persisted", 5)["total_matching_pdfs"] == 1
    finally:
        second.close()


def test_unicode_text_is_searchable(index):
    write_pdf(index.root, "a.pdf", ["Café société und Größe"])
    assert search(index, "CAFÉ")["results"][0]["occurrences"] == 1
