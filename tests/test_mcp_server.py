"""Protocol-level tests: an MCP client talks to the server in-process, as Claude would."""

import asyncio

import pytest
from mcp import Client

from app.mcp_server import mcp

EXPECTED_TOOLS = {"list_pdfs", "get_pdf_info", "read_pdf", "create_pdf", "search_pdf_content", "reindex_pdfs"}


def call(tool_name, arguments=None):
    """Call one tool through a real MCP client session and return (is_error, structured, text)."""

    async def run():
        async with Client(mcp) as client:
            result = await client.call_tool(tool_name, arguments or {})
            text = " ".join(getattr(block, "text", "") for block in result.content)
            return result.is_error, result.structured_content, text

    return asyncio.run(run())


def test_tools_are_discoverable_with_descriptions_and_schemas():
    async def run():
        async with Client(mcp) as client:
            return (await client.list_tools()).tools

    tools = {tool.name: tool for tool in asyncio.run(run())}
    assert set(tools) == EXPECTED_TOOLS
    assert all(tool.description for tool in tools.values())
    assert "query" in tools["search_pdf_content"].input_schema["properties"]
    assert tools["read_pdf"].input_schema["required"] == ["filename"]


def test_create_list_info_read_roundtrip(app_settings):
    is_error, created, _ = call("create_pdf", {"filename": "notes/a.pdf", "content": "Hello MCP", "title": "T"})
    assert not is_error and created["path"] == "notes/a.pdf"

    _, listing, _ = call("list_pdfs")
    assert listing["count"] == 1 and listing["files"][0]["path"] == "notes/a.pdf"

    _, info, _ = call("get_pdf_info", {"filename": "notes/a.pdf"})
    assert info["page_count"] == 1

    _, read, _ = call("read_pdf", {"filename": "notes/a.pdf", "page_start": 1, "page_end": 1})
    assert "Hello MCP" in read["pages"][0]["text"]


@pytest.mark.parametrize(
    ("tool_name", "arguments", "expected"),
    [
        ("read_pdf", {"filename": "../../secret.pdf"}, "outside|not allowed"),
        ("read_pdf", {"filename": "missing.pdf"}, "not found"),
        ("read_pdf", {"filename": "C:/Windows/x.pdf"}, "Absolute"),
        ("create_pdf", {"filename": "x.txt", "content": "hi"}, "Only .pdf"),
        ("search_pdf_content", {"query": "x", "limit": 0}, "limit|greater"),
    ],
)
def test_errors_are_clean_tool_errors(app_settings, tool_name, arguments, expected):
    import re

    is_error, _, text = call(tool_name, arguments)
    assert is_error
    assert re.search(expected, text, re.IGNORECASE)
    assert "Traceback" not in text and str(app_settings.pdf_root) not in text


def test_create_refuses_overwrite(app_settings):
    call("create_pdf", {"filename": "a.pdf", "content": "one"})
    is_error, _, text = call("create_pdf", {"filename": "a.pdf", "content": "two"})
    assert is_error and "already exists" in text


def test_search_and_reindex_through_mcp(app_settings):
    pytest.importorskip("lucene", reason="PyLucene is not installed")
    call("create_pdf", {"filename": "a.pdf", "content": "force majeure and force majeure"})
    call("create_pdf", {"filename": "b.pdf", "content": "one force majeure only"})

    is_error, found, _ = call("search_pdf_content", {"query": "force majeure"})
    assert not is_error and found["total_matching_pdfs"] == 2
    assert [r["path"] for r in found["results"]] == ["a.pdf", "b.pdf"]
    assert [r["occurrences"] for r in found["results"]] == [2, 1]

    _, report, _ = call("reindex_pdfs", {"full_rebuild": True})
    assert sorted(report["indexed"]) == ["a.pdf", "b.pdf"] and report["failed"] == []
