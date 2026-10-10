import pytest

from app import pdf_service
from app.file_service import InvalidInputError, InvalidPathError
from app.pdf_service import PdfExistsError, PdfReadError

BYTES, PAGES, CHARS = 10_000_000, 20, 100_000


def read(root, name, **kwargs):
    return pdf_service.read_pdf(root, name, BYTES, PAGES, CHARS, **kwargs)


def make_multipage(root, name, pages=5):
    """Create a PDF with one distinctive line per page (forced onto separate pages)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    pdf = canvas.Canvas(str(root / name), pagesize=A4)
    for number in range(1, pages + 1):
        pdf.drawString(72, 700, f"Content of page {number}")
        pdf.showPage()
    pdf.save()


def test_create_then_read_back(tmp_path):
    info = pdf_service.create_pdf(
        tmp_path, "notes/hello.pdf", "First paragraph.\n\nSecond <b>paragraph</b> & more.", 10_000, title="My Notes"
    )
    assert info["path"] == "notes/hello.pdf" and info["size_bytes"] > 0

    result = read(tmp_path, "notes/hello.pdf")
    text = result["pages"][0]["text"]
    assert "My Notes" in text and "First paragraph." in text
    assert "<b>paragraph</b> & more" in text  # markup is escaped, not interpreted
    assert result["total_pages"] == 1 and result["truncated"] is False


def test_long_text_flows_onto_several_pages(tmp_path):
    pdf_service.create_pdf(tmp_path, "long.pdf", "\n\n".join(["word " * 200] * 40), 1_000_000)
    assert read(tmp_path, "long.pdf")["total_pages"] > 1


def test_create_refuses_overwrite_unless_asked(tmp_path):
    pdf_service.create_pdf(tmp_path, "a.pdf", "one", 1000)
    with pytest.raises(PdfExistsError):
        pdf_service.create_pdf(tmp_path, "a.pdf", "two", 1000)
    pdf_service.create_pdf(tmp_path, "a.pdf", "two", 1000, overwrite=True)
    assert "two" in read(tmp_path, "a.pdf")["pages"][0]["text"]


def test_create_rejects_traversal_and_bad_input(tmp_path):
    with pytest.raises(InvalidPathError):
        pdf_service.create_pdf(tmp_path, "../evil.pdf", "x", 1000)
    with pytest.raises(InvalidPathError):
        pdf_service.create_pdf(tmp_path, "evil.txt", "x", 1000)
    with pytest.raises(InvalidInputError):
        pdf_service.create_pdf(tmp_path, "empty.pdf", "   ", 1000)
    with pytest.raises(InvalidInputError):
        pdf_service.create_pdf(tmp_path, "big.pdf", "x" * 11, 10)


def test_read_page_range(tmp_path):
    make_multipage(tmp_path, "five.pdf", pages=5)
    result = read(tmp_path, "five.pdf", page_start=2, page_end=3)
    assert [p["page"] for p in result["pages"]] == [2, 3]
    assert "page 2" in result["pages"][0]["text"]
    assert result["total_pages"] == 5 and result["truncated"] is False


def test_read_truncates_at_page_limit_and_reports_next_page(tmp_path):
    make_multipage(tmp_path, "five.pdf", pages=5)
    result = pdf_service.read_pdf(tmp_path, "five.pdf", BYTES, 2, CHARS)
    assert [p["page"] for p in result["pages"]] == [1, 2]
    assert result["truncated"] is True and result["next_page"] == 3


def test_read_rejects_bad_ranges(tmp_path):
    make_multipage(tmp_path, "five.pdf", pages=5)
    for kwargs in ({"page_start": 0}, {"page_start": 4, "page_end": 2}, {"page_start": 9}):
        with pytest.raises(InvalidInputError):
            read(tmp_path, "five.pdf", **kwargs)


def test_blank_page_is_reported(tmp_path):
    from reportlab.pdfgen import canvas

    pdf = canvas.Canvas(str(tmp_path / "blank.pdf"))
    pdf.showPage()
    pdf.save()
    page = read(tmp_path, "blank.pdf")["pages"][0]
    assert page["text"] == "" and "No extractable text" in page["note"]


def test_get_pdf_info(tmp_path):
    pdf_service.create_pdf(tmp_path, "info.pdf", "body", 1000, title="Report")
    info = pdf_service.get_pdf_info(tmp_path, "info.pdf", BYTES)
    assert info["name"] == "info.pdf" and info["page_count"] == 1
    assert info["metadata"]["title"] == "Report"
    assert info["size_bytes"] > 0 and info["modified_at"]


def test_malformed_pdf_raises_clean_error(tmp_path):
    (tmp_path / "broken.pdf").write_bytes(b"%PDF-1.4 this is not really a pdf")
    with pytest.raises(PdfReadError):
        read(tmp_path, "broken.pdf")
    with pytest.raises(PdfReadError):
        pdf_service.extract_pages(tmp_path / "broken.pdf", BYTES)


def test_size_limit_enforced(tmp_path):
    pdf_service.create_pdf(tmp_path, "a.pdf", "hello", 1000)
    with pytest.raises(PdfReadError):
        pdf_service.extract_pages(tmp_path / "a.pdf", 10)
