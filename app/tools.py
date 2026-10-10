"""Application functions: the single implementation behind both MCP tools and REST endpoints.

Each function validates its input, calls the PDF/file/search services and
returns a JSON-compatible dict. Expected problems raise ``ToolError`` with a
short, safe message (no stack traces, no absolute paths).
"""

import functools
import logging
import threading
from collections.abc import Callable
from typing import Any

from app import file_service, pdf_service
from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


class ToolError(Exception):
    """A user-facing error: the message is safe to show to an LLM or an API client."""

    def __init__(self, message: str, kind: str = "invalid_request") -> None:
        super().__init__(message)
        self.kind = kind  # "invalid_request" | "not_found" | "conflict" | "internal"


# Expected exception types and the kind of ToolError they become.
_ERROR_KINDS: list[tuple[type[Exception], str]] = [
    (file_service.PdfNotFoundError, "not_found"),
    (pdf_service.PdfExistsError, "conflict"),
    (file_service.InvalidInputError, "invalid_request"),  # also covers InvalidPathError
    (pdf_service.PdfReadError, "invalid_request"),
]


def _translate_errors(function: Callable[..., dict]) -> Callable[..., dict]:
    """Convert expected exceptions into ToolError; hide everything else behind a generic message."""

    @functools.wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> dict:
        try:
            return function(*args, **kwargs)
        except ToolError:
            raise
        except Exception as error:  # noqa: BLE001 - last line of defence, logged below
            for error_type, kind in _ERROR_KINDS:
                if isinstance(error, error_type):
                    raise ToolError(str(error), kind) from error
            if isinstance(error, PermissionError):
                raise ToolError("Permission denied while accessing the file.", "internal") from error
            logger.exception("Unexpected error in %s", function.__name__)
            raise ToolError("Internal error; see the server log for details.", "internal") from error

    return wrapper


# ---------------------------------------------------------------- managed Lucene index

_search_index = None
_search_index_lock = threading.Lock()


def get_search_index():
    """Return the process-wide Lucene index, creating it on first use.

    Imported lazily so that the PDF tools keep working (and give a clear error)
    when PyLucene is not installed, instead of the whole app failing at import.
    """
    global _search_index
    with _search_index_lock:
        if _search_index is None:
            settings = get_settings()  # also exports JAVA_HOME from .env before Lucene loads
            try:
                from app.search_service import PdfSearchIndex
            except ImportError as error:
                raise ToolError(
                    "Search is unavailable: PyLucene is not installed (see README, 'Installing PyLucene').",
                    "internal",
                ) from error
            except ValueError as error:  # PyLucene raises ValueError when it cannot find jvm.dll
                logger.error("JVM not found: %s", error)
                raise ToolError("Search is unavailable: Java was not found. Set JAVA_HOME (see README).", "internal") from error
            _search_index = PdfSearchIndex(settings.pdf_root, settings.lucene_index_path, settings.max_pdf_bytes)
        return _search_index


def close_search_index() -> None:
    """Release the Lucene index (called at shutdown)."""
    global _search_index
    with _search_index_lock:
        if _search_index is not None:
            _search_index.close()
            _search_index = None


# ---------------------------------------------------------------- the tools


@_translate_errors
def list_pdfs(subdirectory: str | None = None, recursive: bool = True) -> dict:
    """List PDFs under the PDF root (optionally one subdirectory), without their contents."""
    settings = get_settings()
    files = file_service.list_pdf_files(settings.pdf_root, subdirectory, recursive)
    return {"success": True, "files": files, "count": len(files)}


@_translate_errors
def get_pdf_info(filename: str) -> dict:
    """Return page count, size, modification time and metadata of one PDF."""
    settings = get_settings()
    info = pdf_service.get_pdf_info(settings.pdf_root, filename, settings.max_pdf_bytes)
    return {"success": True, **info}


@_translate_errors
def read_pdf(filename: str, page_start: int | None = None, page_end: int | None = None) -> dict:
    """Extract the text of a PDF, page by page, for an optional one-based page range."""
    settings = get_settings()
    result = pdf_service.read_pdf(
        settings.pdf_root, filename, settings.max_pdf_bytes, settings.max_pages_per_read,
        settings.max_content_chars, page_start, page_end,
    )
    return {"success": True, **result}


@_translate_errors
def create_pdf(filename: str, content: str, title: str | None = None, overwrite: bool = False) -> dict:
    """Create a new PDF from text inside the PDF root (refuses to overwrite by default)."""
    settings = get_settings()
    info = pdf_service.create_pdf(settings.pdf_root, filename, content, settings.max_content_chars, title, overwrite)
    return {"success": True, **info}


@_translate_errors
def search_pdf_content(query: str, limit: int = 10, case_sensitive: bool = False) -> dict:
    """Search all PDFs for a word or phrase; one result per PDF, sorted by occurrence count."""
    settings = get_settings()
    limit = min(limit, settings.max_search_results)
    result = get_search_index().search(query, limit, case_sensitive)
    return {"success": True, **result}


@_translate_errors
def reindex_pdfs(full_rebuild: bool = False) -> dict:
    """Synchronise the Lucene index with the PDF directory (or rebuild it completely)."""
    report = get_search_index().sync(full_rebuild)
    return {"success": True, "full_rebuild": full_rebuild, **report}


def health_status(settings: Settings | None = None) -> dict:
    """Small status object for /health: no paths, no secrets."""
    settings = settings or get_settings()
    root_ok = settings.pdf_root.is_dir()
    index_ready = _search_index is not None
    return {"status": "ok" if root_ok else "degraded", "pdf_root_accessible": root_ok, "search_index_ready": index_ready}
