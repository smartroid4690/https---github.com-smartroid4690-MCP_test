"""FastAPI application: /health, a few REST endpoints, and the MCP HTTP transport.

One process, one HTTP application:
    GET  /health            status
    GET  /pdfs              list PDFs
    GET  /pdfs/{path}       PDF info
    POST /pdfs              create a PDF
    GET  /search?q=...      search
    POST /mcp               MCP Streamable HTTP endpoint (mounted from the MCP SDK)

Run:  python -m app.main
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Annotated

import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import tools
from app.config import configure_logging, get_settings
from app.mcp_server import mcp

logger = logging.getLogger(__name__)

HTTP_STATUS_BY_ERROR_KIND = {"invalid_request": 400, "not_found": 404, "conflict": 409, "internal": 500}

# Builds the MCP ASGI app. Must be called before mcp.session_manager is used.
mcp_http_app = mcp.streamable_http_app()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start and stop everything that has a lifecycle: logging, Lucene index, MCP sessions."""
    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        # Opening Lucene (and the JVM) is slow and blocking, so do it off the event loop.
        await asyncio.to_thread(tools.get_search_index)
    except tools.ToolError as error:
        logger.error("Search index not available: %s", error)  # PDF tools still work
    # A mounted sub-app's own lifespan is not run by FastAPI, so run the MCP session manager here.
    async with mcp.session_manager.run():
        yield
    await asyncio.to_thread(tools.close_search_index)


app = FastAPI(title="PDF MCP Server", lifespan=lifespan)


@app.exception_handler(tools.ToolError)
async def tool_error_handler(request: Request, error: tools.ToolError) -> JSONResponse:
    status = HTTP_STATUS_BY_ERROR_KIND.get(error.kind, 500)
    return JSONResponse(status_code=status, content={"success": False, "error": str(error)})


class CreatePdfRequest(BaseModel):
    filename: str = Field(max_length=300)
    content: str = Field(min_length=1)
    title: str | None = Field(default=None, max_length=200)
    overwrite: bool = False


@app.get("/health")
def health() -> dict:
    """Report that the app is running, whether the PDF folder is reachable and the index is open."""
    return tools.health_status()


# These are plain `def` endpoints: FastAPI runs them in a thread pool, which is
# what we want for blocking PDF/Lucene work.


@app.get("/pdfs")
def rest_list_pdfs(subdirectory: str | None = None, recursive: bool = True) -> dict:
    return tools.list_pdfs(subdirectory, recursive)


@app.get("/pdfs/{filename:path}")
def rest_pdf_info(filename: str) -> dict:
    return tools.get_pdf_info(filename)


@app.post("/pdfs", status_code=201)
def rest_create_pdf(body: CreatePdfRequest) -> dict:
    return tools.create_pdf(body.filename, body.content, body.title, body.overwrite)


@app.get("/search")
def rest_search(
    q: Annotated[str, Query(min_length=1, max_length=500)],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    case_sensitive: bool = False,
) -> dict:
    return tools.search_pdf_content(q, limit, case_sensitive)


# Mounted last so the REST routes above take precedence; the MCP app serves /mcp.
app.mount("/", mcp_http_app)


if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run(app, host=settings.host, port=settings.port, log_level=settings.log_level.lower())
