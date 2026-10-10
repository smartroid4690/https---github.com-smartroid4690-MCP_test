"""MCP server: registers the PDF tools and delegates to ``app.tools``.

Each tool is a thin wrapper. All validation of paths, ranges and sizes, and all
PDF/search logic, lives in the service modules so REST and MCP behave the same.

The application functions are synchronous (pypdf, ReportLab and PyLucene all
block), so every wrapper runs them in a worker thread to keep the event loop free.

Run as a local stdio server:   python -m app.mcp_server
(The HTTP transport is mounted into FastAPI by ``app.main``.)
"""

import asyncio
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError as McpToolError
from pydantic import Field

from app import tools
from app.config import configure_logging, get_settings

INSTRUCTIONS = (
    "Manage PDF files in one configured folder. All paths are relative to that folder "
    "(for example 'contracts/contract.pdf'). Use list_pdfs to discover files, "
    "search_pdf_content to find which PDFs contain a word or phrase, and read_pdf with "
    "a page range to read text. Do not read whole large PDFs unless the user asks."
)

mcp = MCPServer("pdf-mcp-server", instructions=INSTRUCTIONS)

# Declared as dict[str, Any] (not bare dict) so the SDK publishes an output schema and
# returns structured content alongside the text.
ToolResult = dict[str, Any]

RelativePdfPath = Annotated[str, Field(description="PDF path relative to the PDF folder, e.g. 'contracts/a.pdf'.", max_length=300)]


async def _run(function, *args: Any) -> ToolResult:
    """Run a blocking application function without blocking the event loop."""
    try:
        return await asyncio.to_thread(function, *args)
    except tools.ToolError as error:
        # Only the SDK's own ToolError message is shown to the model; any other exception
        # becomes a generic "Error executing tool". Ours are already safe, short messages.
        raise McpToolError(str(error)) from None


@mcp.tool()
async def list_pdfs(
    subdirectory: Annotated[str | None, Field(description="Optional folder, relative to the PDF folder.", max_length=300)] = None,
    recursive: Annotated[bool, Field(description="Include PDFs in nested folders.")] = True,
) -> ToolResult:
    """List PDF files (name, relative path, size, modified time). Does not return contents."""
    return await _run(tools.list_pdfs, subdirectory, recursive)


@mcp.tool()
async def get_pdf_info(filename: RelativePdfPath) -> ToolResult:
    """Get one PDF's page count, size, modification time and metadata (title, author, ...) without its text."""
    return await _run(tools.get_pdf_info, filename)


@mcp.tool()
async def read_pdf(
    filename: RelativePdfPath,
    page_start: Annotated[int | None, Field(description="First page to read (1-based). Default: 1.", ge=1)] = None,
    page_end: Annotated[int | None, Field(description="Last page to read (1-based). Default: last page.", ge=1)] = None,
) -> ToolResult:
    """Extract a PDF's text page by page. Large ranges are cut short; 'next_page' says where to continue.
    Scanned PDFs have no extractable text (OCR is not supported)."""
    return await _run(tools.read_pdf, filename, page_start, page_end)


@mcp.tool()
async def create_pdf(
    filename: Annotated[str, Field(description="New PDF path relative to the PDF folder; must end in .pdf.", max_length=300)],
    content: Annotated[str, Field(description="Plain text. Blank lines separate paragraphs.")],
    title: Annotated[str | None, Field(description="Optional title printed at the top.", max_length=200)] = None,
    overwrite: Annotated[bool, Field(description="Replace the file if it exists. Default false.")] = False,
) -> ToolResult:
    """Create a new PDF from plain text inside the PDF folder. Refuses to overwrite unless overwrite=true."""
    return await _run(tools.create_pdf, filename, content, title, overwrite)


@mcp.tool()
async def search_pdf_content(
    query: Annotated[str, Field(description="A word or phrase to find. Treated literally; Lucene operators (AND, *, field:) are not supported.", min_length=1, max_length=500)],
    limit: Annotated[int, Field(description="Maximum number of PDFs to return.", ge=1, le=50)] = 10,
    case_sensitive: Annotated[bool, Field(description="Match upper/lower case exactly. Default false.")] = False,
) -> ToolResult:
    """Search all PDFs (Apache Lucene full-text index) for a word or phrase.
    Returns one result per matching PDF with its total occurrence count, matching page numbers and
    short snippets, sorted by occurrence count (highest first). No stemming: 'contract' does not match 'contracts'.
    The index is synchronised automatically before each search."""
    return await _run(tools.search_pdf_content, query, limit, case_sensitive)


@mcp.tool()
async def reindex_pdfs(
    full_rebuild: Annotated[bool, Field(description="Discard the index and re-extract every PDF. Default false.")] = False,
) -> ToolResult:
    """Synchronise the Lucene index with the PDF folder: index new/changed PDFs, drop deleted ones.
    Searching already does this automatically; use this to troubleshoot or force a full rebuild."""
    return await _run(tools.reindex_pdfs, full_rebuild)


if __name__ == "__main__":
    # Local stdio transport: the MCP client starts this process and talks over stdin/stdout.
    configure_logging(get_settings().log_level)  # logs go to stderr, never stdout
    try:
        mcp.run("stdio")
    finally:
        tools.close_search_index()
