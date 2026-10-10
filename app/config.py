"""Central configuration, loaded from environment variables (and an optional .env file)."""

import logging
import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_DIR = Path(__file__).resolve().parent.parent

# Hard limits that protect the server (and the LLM's context window).
# They are settings so tests can lower them, but the defaults are sensible.
DEFAULT_MAX_PDF_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_CONTENT_CHARS = 200_000
DEFAULT_MAX_PAGES_PER_READ = 20
DEFAULT_MAX_SEARCH_RESULTS = 50


class Settings(BaseSettings):
    """Application settings. Environment variable names match field names (PDF_ROOT, ...)."""

    # Anchored to the project folder, not the current directory: MCP clients start us from anywhere.
    model_config = SettingsConfigDict(env_file=PROJECT_DIR / ".env", extra="ignore")

    # JDK location. PyLucene needs it to find jvm.dll; MCP clients start us with a minimal
    # environment that usually lacks JAVA_HOME, so it can also be set in .env.
    java_home: Path | None = None
    pdf_root: Path = Path("./data/pdfs")
    lucene_index_path: Path = Path("./index")
    log_level: str = "INFO"
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)

    max_pdf_bytes: int = DEFAULT_MAX_PDF_BYTES
    max_content_chars: int = DEFAULT_MAX_CONTENT_CHARS
    max_pages_per_read: int = DEFAULT_MAX_PAGES_PER_READ
    max_search_results: int = DEFAULT_MAX_SEARCH_RESULTS


@lru_cache
def get_settings() -> Settings:
    """Load settings once, resolve paths to absolute form and create the directories."""
    settings = Settings()
    # Absolute paths make the "is this file inside the root?" check unambiguous.
    settings.pdf_root = settings.pdf_root.expanduser().resolve()
    settings.lucene_index_path = settings.lucene_index_path.expanduser().resolve()
    if settings.java_home:
        os.environ.setdefault("JAVA_HOME", str(settings.java_home))  # must happen before `import lucene`
    settings.pdf_root.mkdir(parents=True, exist_ok=True)
    settings.lucene_index_path.mkdir(parents=True, exist_ok=True)
    return settings


def configure_logging(level_name: str) -> None:
    """Configure the standard logging module.

    Logs go to stderr: with the stdio MCP transport, stdout carries protocol
    messages and must never receive log output.
    """
    logging.basicConfig(
        level=getattr(logging, level_name.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
