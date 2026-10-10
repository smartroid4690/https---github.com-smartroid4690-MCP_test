# PDF MCP Server

A small, educational server that lets an LLM (Claude Desktop, or any MCP client) **list, read, create and
full-text-search PDF files** in one configured folder, using natural language. There is no frontend: the LLM
is the user interface.

Stack: Python 3.11+, FastAPI, the official MCP Python SDK (`mcp` 2.x), Apache Lucene via PyLucene,
pypdf (read), ReportLab (write), Pydantic, pytest.

## FastAPI vs MCP vs Lucene

| Piece | What it is | Role here |
|---|---|---|
| **MCP** | A protocol that lets an LLM client discover and call "tools" | Exposes 6 tools (`list_pdfs`, `read_pdf`, ...) |
| **FastAPI** | A Python web framework | Hosts `/health`, a few optional REST endpoints, and the MCP HTTP endpoint in one process |
| **Lucene** | A Java full-text search library | Indexes every PDF *page*; finds which pages contain a word or phrase |

FastAPI and MCP share **one HTTP application** (one process, one port): REST routes live at `/health`, `/pdfs`,
`/search`; the MCP Streamable HTTP endpoint is `/mcp`. The stdio transport is a separate way to launch the
same MCP tools as a local subprocess.

## Request lifecycle

```
User asks Claude: "Which PDFs mention force majeure, and how often?"
  -> Claude picks the tool search_pdf_content(query="force majeure")        (from the tool description)
  -> MCP client sends the call to the server (stdio or POST /mcp)
  -> app/mcp_server.py validates arguments (Pydantic) and runs app/tools.py in a worker thread
  -> app/search_service.py: sync index (stat files) -> Lucene finds candidate pages
       -> app/text_matching.py counts literal occurrences, builds snippets
  -> a JSON result (one entry per PDF, sorted by occurrences) goes back to Claude
  -> Claude explains the result to the user
```

Project layout:

```
app/config.py          settings from environment / .env
app/file_service.py    safe path handling, listing PDFs       (all path-traversal defences live here)
app/pdf_service.py     pypdf extraction + metadata, ReportLab creation
app/text_matching.py   occurrence counting + snippets (pure Python, no JVM)
app/search_service.py  Lucene index, sync, search            (the only module that imports PyLucene)
app/tools.py           the 6 application functions, shared by MCP and REST
app/mcp_server.py      MCP tool registration (thin wrappers) + stdio entry point
app/main.py            FastAPI app, REST endpoints, MCP HTTP mount
tests/                 pytest suite
```

(`app/tools.py` and `app/text_matching.py` are small additions to the suggested layout: they keep business
logic out of the MCP wrappers and make the counting rules testable without a JVM.)

## Install

Verified on Windows 11, Python 3.11, JDK 17 (Temurin), Visual Studio 2022 (C++ tools), PyLucene 9.12.0.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -U pip setuptools wheel
.\.venv\Scripts\python -m pip install -e ".[dev]"
```

### Installing PyLucene (the hard part)

PyLucene is **not on PyPI**: it is a C++/JNI bridge (JCC) you compile against your JDK and Python. Requirements:
a JDK (17+; set `JAVA_HOME`), a C++ compiler (MSVC on Windows, gcc on Linux/macOS), and the PyLucene source from
<https://downloads.apache.org/lucene/pylucene/>. Pick the PyLucene version that matches your Python/JDK
(9.12.0 is what was verified here). If it cannot be installed, the PDF tools still work and search reports
"PyLucene is not installed"; nothing silently falls back to another search engine.

**Linux / macOS** (the supported path; see `INSTALL` in the PyLucene tarball): install JDK + a compiler, then
`make` from the PyLucene source with the Makefile variables for your Python (the tarball documents them).

**Windows** (what was done here; the stock Makefile needs Cygwin, so the same steps are run by hand):

1. Extract `pylucene-9.12.0-src.tar.gz` to a **short path** such as `C:\pl` (long paths exceed MAX_PATH and the build fails).
2. Build the Lucene jars: `cd C:\pl\lucene-java-9.12.0` then `.\gradlew.bat collectRuntimeJars` (downloads Gradle).
3. Build JCC: open an *x64 Native Tools* prompt (or call `vcvars64.bat`), set `JCC_JDK=%JAVA_HOME%`, then
   `.\.venv\Scripts\python -m pip install --no-build-isolation C:\pl\jcc`.
4. Generate and compile the `lucene` module, then install it:
   `scripts\build_pylucene_windows.bat build` followed by `scripts\build_pylucene_windows.bat install`
   (compilation takes several minutes). The script sets `CL=/DNOGDI`, which avoids a clash between the Windows
   `ERROR` macro and a Java enum constant, and skips the optional Morfologik analyzer.
5. Check: `.\.venv\Scripts\python -c "import lucene; lucene.initVM(); print(lucene.VERSION)"` prints `9.12.0`.

## Configure

```powershell
Copy-Item .env.example .env
```

| Variable | Meaning | Default |
|---|---|---|
| `PDF_ROOT` | The only folder the server can touch | `./data/pdfs` |
| `LUCENE_INDEX_PATH` | Index folder (safe to delete) | `./index` |
| `JAVA_HOME` | JDK folder (needed because MCP clients start the server with a minimal environment) | from environment |
| `LOG_LEVEL`, `HOST`, `PORT` | Logging and HTTP bind address | `INFO`, `127.0.0.1`, `8765` |

Relative paths are resolved against the directory you start the server from; use absolute paths when a client launches it.

## Run

```powershell
# HTTP: FastAPI + MCP on one port (REST at /health, /pdfs, /search; MCP at /mcp)
.\.venv\Scripts\python -m app.main
Invoke-RestMethod http://127.0.0.1:8765/health

# stdio: started *by the MCP client* (you normally do not run this yourself)
.\.venv\Scripts\python -m app.mcp_server
```

Run **one** of them at a time per index folder: Lucene permits a single writer.

Tests: `.\.venv\Scripts\python -m pytest` (needs PyLucene for the search tests; without it they are *skipped*, never faked).

## Connect an LLM client

**Claude Desktop (local, stdio)** - edit `%APPDATA%\Claude\claude_desktop_config.json`, then fully restart Claude Desktop:

```json
{
  "mcpServers": {
    "pdf-manager": {
      "command": "D:\\Ibrahim\\GenAI\\MCP\\.venv\\Scripts\\python.exe",
      "args": ["-m", "app.mcp_server"],
      "cwd": "D:\\Ibrahim\\GenAI\\MCP",
      "env": {
        "JAVA_HOME": "C:\\Program Files\\Eclipse Adoptium\\jdk-17.0.19.10-hotspot",
        "PDF_ROOT": "D:\\Ibrahim\\GenAI\\MCP\\data\\pdfs",
        "LUCENE_INDEX_PATH": "D:\\Ibrahim\\GenAI\\MCP\\index"
      }
    }
  }
}
```

**Claude Code (HTTP):** start `python -m app.main`, then
`claude mcp add --transport http pdf-manager http://127.0.0.1:8765/mcp`.

**MCP Inspector (to test without an LLM):** `npx @modelcontextprotocol/inspector` and connect to
`http://127.0.0.1:8765/mcp` (Streamable HTTP) or to the stdio command above.

**GPT / ChatGPT:** support for custom MCP servers depends on the product and plan, and generally requires a
*remote HTTPS* server, not a local stdio process. Check your client's current documentation; do not assume
every ChatGPT interface can reach `127.0.0.1`.

**Security:** the HTTP server has **no authentication**. Keep it bound to `127.0.0.1`. Before exposing it to a network
you must add authentication and TLS (for example a reverse proxy), because the tools read and create files.

## Tools

| Tool | Arguments | Returns |
|---|---|---|
| `list_pdfs` | `subdirectory?`, `recursive=true` | files (name, path, size, modified) and count |
| `get_pdf_info` | `filename` | page count, size, modified, title/author/dates |
| `read_pdf` | `filename`, `page_start?`, `page_end?` | text per page; `truncated`/`next_page` when cut short |
| `create_pdf` | `filename`, `content`, `title?`, `overwrite=false` | new file's path and size |
| `search_pdf_content` | `query`, `limit=10`, `case_sensitive=false` | per PDF: occurrences, pages, snippets, sorted by occurrences |
| `reindex_pdfs` | `full_rebuild=false` | lists of indexed / updated / removed / failed files |

Try these prompts (after creating a few PDFs, or dropping some into `data/pdfs`):

1. "List all PDFs in my documents folder."
2. "Create a PDF called notes/todo.pdf titled 'Todo' containing: buy milk, call the bank."
3. "Read page 1 of notes/todo.pdf."
4. "How many pages does contracts/contract_a.pdf have, and who is the author?"
5. "Search all PDFs for the word liability."
6. "Which PDFs contain the phrase force majeure, and how many occurrences are in each?"
7. "Which PDFs contain the most occurrences of the word indemnity?"
8. "Find termination in contract_a.pdf and show me the matching passages."
9. "Rebuild the search index and tell me if any file failed."
10. Safety check: "Read ../../Windows/win.ini" (must be refused).

## Lucene index and counting

* **Index:** one Lucene document per PDF *page* (`path`, `page`, `content`, plus size and mtime), stored in `LUCENE_INDEX_PATH`.
* **Sync:** before every search (and on `reindex_pdfs`) the server compares each PDF's size and modification time with the
  index. New or changed PDFs are re-extracted and replaced (delete-then-add by path, so no duplicates); deleted PDFs are
  removed; unchanged PDFs are not touched; unreadable PDFs are reported in `failed`.
* **Analyzer:** Lucene `StandardAnalyzer` for indexing and querying: Unicode word splitting, lower-casing, **no stop words,
  no stemming** (`contract` does not match `contracts`).
* **Query:** treated literally. One word becomes a `TermQuery`, several words a `PhraseQuery`. Lucene syntax (`AND`, `*`,
  `field:`) is not interpreted, so malformed queries cannot occur.
* **Occurrences:** Lucene scores are never used as counts. Lucene selects candidate pages; then the stored page text is scanned
  with a literal, non-overlapping, case-insensitive (by default) match that must not be glued to other letters
  ("act" does not count "react"); whitespace inside a phrase matches any whitespace (line wraps). Counts are summed per PDF,
  and results sorted by count, then path.
* **Known differences:** Lucene tokenizes ("force-majeure" and "force majeure" are both two tokens) but counting is literal, so
  such a page is a Lucene candidate with a literal count of 0 and is dropped. `case_sensitive=true` filters Lucene's
  case-insensitive candidates at the counting step.

## Limitations

* **Scanned PDFs / OCR:** not supported. Pages without a text layer are reported as having no extractable text.
* `create_pdf` writes plain text with standard Latin-1 fonts; other scripts (Arabic, CJK) will not render.
* Text extraction quality depends on the PDF (ligatures, columns, odd encodings); counts reflect what pypdf extracts.
* Encrypted (password) and corrupt PDFs are reported as unreadable. Files over 50 MB are skipped.
* Search and sync share one lock; they are serialised, which is fine for a single-user local server.

## Common errors

| Symptom | Cause / fix |
|---|---|
| `jvm.dll could not be found` / "Java was not found" | Set `JAVA_HOME` (in `.env` or the client's `env` block). |
| "PyLucene is not installed" | Follow *Installing PyLucene*; the PDF tools work without it. |
| Port already in use (`10048`) | Change `PORT` in `.env`. |
| `LockObtainFailedException` / index locked | Another server process uses the same `LUCENE_INDEX_PATH`; stop it. |
| `No module named app` in a client | Add `"cwd"` (project folder) to the client config. |
| Console shows `access violation` at startup | Normal: the JVM's own handled faults; pytest runs with faulthandler disabled for this reason. |
| Claude Desktop shows no tools | Check the JSON is valid, use absolute paths, restart the app fully, see its MCP log. |
