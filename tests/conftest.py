import pytest

from app import tools
from app.config import get_settings


def point_app_at(monkeypatch, tmp_path):
    """Make the application use temporary PDF and index folders, never the user's real ones."""
    monkeypatch.setenv("PDF_ROOT", str(tmp_path / "pdfs"))
    monkeypatch.setenv("LUCENE_INDEX_PATH", str(tmp_path / "index"))
    get_settings.cache_clear()
    tools._search_index = None
    return get_settings()


def release_app() -> None:
    tools.close_search_index()
    get_settings.cache_clear()


@pytest.fixture
def app_settings(tmp_path, monkeypatch):
    settings = point_app_at(monkeypatch, tmp_path)
    yield settings
    release_app()
