"""Safe access to PDF files inside one configured root directory.

Every public tool takes a path *relative to the root*. This module is the only
place that turns such a string into a real filesystem path, so all the
path-traversal defences live here.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

logger = logging.getLogger(__name__)

PDF_SUFFIX = ".pdf"


class InvalidInputError(ValueError):
    """A tool argument is invalid (bad page range, empty content, ...)."""


class InvalidPathError(InvalidInputError):
    """The requested path is malformed, unsupported, or outside the PDF root."""


class PdfNotFoundError(FileNotFoundError):
    """The requested PDF does not exist inside the PDF root."""


def _check_relative_text(relative_path: str) -> None:
    """Reject path strings that are absolute, empty, or contain suspicious parts."""
    if not relative_path or not relative_path.strip():
        raise InvalidPathError("Path must not be empty.")
    if "\x00" in relative_path:
        raise InvalidPathError("Path contains a null byte.")
    # Check both flavours so behaviour is identical on Windows and Linux.
    for flavour in (PurePosixPath(relative_path), PureWindowsPath(relative_path)):
        if flavour.is_absolute() or flavour.anchor or getattr(flavour, "drive", ""):
            raise InvalidPathError("Absolute paths are not allowed; use a path relative to the PDF root.")
        if ".." in flavour.parts:
            raise InvalidPathError("Paths containing '..' are not allowed.")


def resolve_inside_root(root: Path, relative_path: str) -> Path:
    """Resolve ``relative_path`` under ``root`` and guarantee it stays inside.

    ``Path.resolve()`` follows symlinks, so a symlink that points outside the
    root is caught by the final ``is_relative_to`` check.
    """
    _check_relative_text(relative_path)
    candidate = (root / relative_path).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise InvalidPathError("Path resolves outside the configured PDF root.")
    return candidate


def resolve_pdf_path(root: Path, relative_path: str, must_exist: bool = True) -> Path:
    """Return the absolute path of a PDF inside ``root``, validating it."""
    path = resolve_inside_root(root, relative_path)
    if path.suffix.lower() != PDF_SUFFIX:
        raise InvalidPathError(f"Only {PDF_SUFFIX} files are supported.")
    if must_exist and not path.is_file():
        raise PdfNotFoundError(f"PDF not found: {relative_path}")
    return path


def to_relative(root: Path, path: Path) -> str:
    """Return the root-relative path with forward slashes (the public form)."""
    return path.resolve().relative_to(root.resolve()).as_posix()


def file_summary(root: Path, path: Path) -> dict:
    """Describe a PDF file without reading its contents."""
    stat = path.stat()
    return {
        "name": path.name,
        "path": to_relative(root, path),
        "size_bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    }


def iter_pdf_paths(root: Path, subdirectory: str | None = None, recursive: bool = True) -> list[Path]:
    """List PDF files under ``root`` (or one subdirectory), sorted by relative path.

    Symlinks that lead outside the root are skipped rather than followed.
    """
    base = root.resolve()
    if subdirectory:
        base = resolve_inside_root(root, subdirectory)
        if not base.is_dir():
            raise PdfNotFoundError(f"Directory not found: {subdirectory}")

    pattern = "**/*" if recursive else "*"
    found = []
    for path in base.glob(pattern):
        if path.suffix.lower() != PDF_SUFFIX or not path.is_file():
            continue
        if not path.resolve().is_relative_to(root.resolve()):
            logger.warning("Skipping %s: resolves outside the PDF root", path.name)
            continue
        found.append(path)
    return sorted(found, key=lambda p: to_relative(root, p).lower())


def list_pdf_files(root: Path, subdirectory: str | None = None, recursive: bool = True) -> list[dict]:
    """Return a summary dict for each PDF found."""
    return [file_summary(root, path) for path in iter_pdf_paths(root, subdirectory, recursive)]
