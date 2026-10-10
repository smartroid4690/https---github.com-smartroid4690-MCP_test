"""PDF reading (pypdf) and creation (ReportLab)."""

import logging
from pathlib import Path
from xml.sax.saxutils import escape

from pypdf import PdfReader
from pypdf.errors import PyPdfError
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

from app.file_service import InvalidInputError, file_summary, resolve_pdf_path, to_relative

logger = logging.getLogger(__name__)


class PdfReadError(Exception):
    """The PDF is malformed, encrypted, too large, or otherwise unreadable."""


class PdfExistsError(FileExistsError):
    """A PDF with the requested name already exists."""


def _open_reader(path: Path, max_bytes: int) -> PdfReader:
    """Open a PDF, turning every expected pypdf failure into PdfReadError."""
    if path.stat().st_size > max_bytes:
        raise PdfReadError(f"{path.name} is larger than the {max_bytes} byte limit.")
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted and not reader.decrypt(""):
            raise PdfReadError(f"{path.name} is password-protected and cannot be read.")
        len(reader.pages)  # forces the page tree to load, so a broken file fails here
    except PdfReadError:
        raise
    except (PyPdfError, ValueError, OSError, KeyError, AttributeError, TypeError, RecursionError) as error:
        # pypdf raises a wide range of exceptions on corrupt input; none are useful to the caller.
        logger.warning("Could not open %s: %s", path.name, error)
        raise PdfReadError(f"{path.name} is not a valid or readable PDF.") from error
    return reader


def _extract_page_text(reader: PdfReader, index: int, name: str) -> str:
    """Extract one page's text; an unreadable page counts as empty rather than failing the file."""
    try:
        return (reader.pages[index].extract_text() or "").strip()
    except (PyPdfError, ValueError, KeyError, TypeError, AttributeError, RecursionError) as error:
        logger.warning("Could not extract page %d of %s: %s", index + 1, name, error)
        return ""


def extract_pages(path: Path, max_bytes: int) -> list[str]:
    """Return the text of every page (0-based list). Used by the search indexer."""
    reader = _open_reader(path, max_bytes)
    return [_extract_page_text(reader, i, path.name) for i in range(len(reader.pages))]


def _clean_metadata(reader: PdfReader) -> dict:
    """Pick the common document-information fields, skipping absent ones."""
    info = reader.metadata
    if not info:
        return {}
    fields = {
        "title": info.title,
        "author": info.author,
        "subject": info.subject,
        "creator": info.creator,
        "producer": info.producer,
        "created_at": info.creation_date.isoformat() if _safe_date(info, "creation_date") else None,
    }
    return {key: str(value) for key, value in fields.items() if value}


def _safe_date(info, attribute: str):
    """pypdf raises on malformed PDF dates; treat those as absent."""
    try:
        return getattr(info, attribute)
    except (ValueError, TypeError, PyPdfError):
        return None


def get_pdf_info(root: Path, filename: str, max_bytes: int) -> dict:
    """Metadata for a single PDF, without its text."""
    path = resolve_pdf_path(root, filename)
    reader = _open_reader(path, max_bytes)
    return {
        **file_summary(root, path),
        "page_count": len(reader.pages),
        "metadata": _clean_metadata(reader),
    }


def read_pdf(
    root: Path,
    filename: str,
    max_bytes: int,
    max_pages: int,
    max_chars: int,
    page_start: int | None = None,
    page_end: int | None = None,
) -> dict:
    """Extract text page by page, limited to a page range and a size budget.

    If the range is larger than ``max_pages`` or the text exceeds ``max_chars``,
    the result is cut short and ``next_page`` tells the caller where to resume.
    """
    path = resolve_pdf_path(root, filename)
    reader = _open_reader(path, max_bytes)
    total_pages = len(reader.pages)

    first = 1 if page_start is None else page_start
    last = total_pages if page_end is None else page_end
    requested_last = last
    if first < 1 or last < 1:
        raise InvalidInputError("Page numbers start at 1.")
    if first > last:
        raise InvalidInputError("page_start must not be greater than page_end.")
    if first > total_pages:
        raise InvalidInputError(f"page_start {first} is beyond the last page ({total_pages}).")
    last = min(last, total_pages, first + max_pages - 1)

    pages = []
    chars_used = 0
    next_page = None
    for number in range(first, last + 1):
        text = _extract_page_text(reader, number - 1, path.name)
        if pages and chars_used + len(text) > max_chars:
            next_page = number
            break
        chars_used += len(text)
        entry = {"page": number, "text": text}
        if not text:
            entry["note"] = "No extractable text (blank page, or a scanned image - OCR is not supported)."
        pages.append(entry)

    if next_page is None and last < min(requested_last, total_pages):
        next_page = last + 1  # stopped at max_pages

    return {
        "path": to_relative(root, path),
        "total_pages": total_pages,
        "metadata": _clean_metadata(reader),
        "pages": pages,
        "truncated": next_page is not None,
        "next_page": next_page,
    }


def _paragraphs_from_text(content: str) -> list[str]:
    """Split text into paragraphs on blank lines; single newlines become line breaks."""
    blocks = [block.strip() for block in content.replace("\r\n", "\n").split("\n\n")]
    return [escape(block).replace("\n", "<br/>") for block in blocks if block]


def create_pdf(
    root: Path,
    filename: str,
    content: str,
    max_content_chars: int,
    title: str | None = None,
    overwrite: bool = False,
) -> dict:
    """Write ``content`` to a new PDF inside ``root`` using ReportLab."""
    if not content or not content.strip():
        raise InvalidInputError("content must not be empty.")
    if len(content) > max_content_chars:
        raise InvalidInputError(f"content is longer than the {max_content_chars} character limit.")

    path = resolve_pdf_path(root, filename, must_exist=False)
    if path.exists() and not overwrite:
        raise PdfExistsError(f"{filename} already exists; pass overwrite=true to replace it.")
    path.parent.mkdir(parents=True, exist_ok=True)

    styles = getSampleStyleSheet()
    story = []
    if title:
        story += [Paragraph(escape(title), styles["Title"]), Spacer(1, 12)]
    for paragraph in _paragraphs_from_text(content):
        story += [Paragraph(paragraph, styles["BodyText"]), Spacer(1, 8)]

    document = SimpleDocTemplate(str(path), pagesize=A4, title=title or path.stem)
    document.build(story)  # flowables wrap lines and break pages automatically
    logger.info("Created %s", to_relative(root, path))
    return file_summary(root, path)
