import os

import pytest

from app import file_service
from app.file_service import InvalidPathError, PdfNotFoundError
from app.pdf_service import create_pdf

LIMIT = 10_000


def make_pdf(root, name, text="hello"):
    return create_pdf(root, name, text, max_content_chars=LIMIT)


def test_list_empty_directory(tmp_path):
    assert file_service.list_pdf_files(tmp_path) == []


def test_list_valid_pdfs_with_subdirectories(tmp_path):
    make_pdf(tmp_path, "a.pdf")
    make_pdf(tmp_path, "contracts/b.pdf")
    (tmp_path / "notes.txt").write_text("not a pdf")

    files = file_service.list_pdf_files(tmp_path)
    assert [f["path"] for f in files] == ["a.pdf", "contracts/b.pdf"]
    assert files[0]["size_bytes"] > 0 and files[0]["modified_at"]

    only_sub = file_service.list_pdf_files(tmp_path, subdirectory="contracts")
    assert [f["path"] for f in only_sub] == ["contracts/b.pdf"]
    non_recursive = file_service.list_pdf_files(tmp_path, recursive=False)
    assert [f["path"] for f in non_recursive] == ["a.pdf"]


@pytest.mark.parametrize(
    "bad_path",
    ["../outside.pdf", "a/../../outside.pdf", "/etc/passwd.pdf", "C:\\Windows\\x.pdf", "..\\x.pdf", "", "a\x00.pdf"],
)
def test_rejects_paths_outside_root(tmp_path, bad_path):
    with pytest.raises(InvalidPathError):
        file_service.resolve_pdf_path(tmp_path, bad_path, must_exist=False)


def test_rejects_non_pdf_extension_and_missing_file(tmp_path):
    (tmp_path / "notes.txt").write_text("x")
    with pytest.raises(InvalidPathError):
        file_service.resolve_pdf_path(tmp_path, "notes.txt")
    with pytest.raises(PdfNotFoundError):
        file_service.resolve_pdf_path(tmp_path, "missing.pdf")


def test_rejects_symlink_escaping_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "secret.pdf"
    make_pdf(tmp_path, "secret.pdf")
    link = root / "link.pdf"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this system")

    with pytest.raises(InvalidPathError):
        file_service.resolve_pdf_path(root, "link.pdf")
    assert file_service.list_pdf_files(root) == []
