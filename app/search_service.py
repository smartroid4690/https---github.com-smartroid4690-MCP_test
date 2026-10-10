"""Apache Lucene (via PyLucene) indexing and search for PDF pages.

Design
------
* One Lucene document per PDF *page*, so a hit already knows its page number.
* A page document has these fields:

  ``path``       relative PDF path          StringField, stored  (exact match / delete key)
  ``page``       one-based page number      stored
  ``content``    page text                  TextField, stored    (analysed; stored so we can
                                                                  count and build snippets
                                                                  without re-reading the PDF)
  ``size``       file size in bytes         stored  \\ used by sync() to detect changes
  ``mtime_ns``   modification time (ns)     stored  /
  ``first_page`` "1" on page 1 only         StringField          (lets sync() list indexed PDFs)

* ``StandardAnalyzer`` is used for both indexing and querying: Unicode-aware word
  splitting and lower-casing. In Lucene 9 it has **no stop words and no
  stemming**, so "the" is searchable and "contracts" does not match "contract".
* Searches are *literal*: the query is analysed into tokens and turned into a
  TermQuery (one token) or PhraseQuery (several tokens). Lucene query syntax
  such as ``AND``, ``*`` or ``field:`` is NOT interpreted, so no user input can
  produce a malformed query.
* Lucene only picks candidate pages. Occurrence counts and snippets come from
  ``text_matching`` (see its docstring for the counting rules).

Threading
---------
The JVM is started once per process. Every thread that touches Lucene must be
attached to it first (``_attach_thread``), because the FastAPI layer runs index
work in worker threads. A single re-entrant lock serialises sync and search, so
a reader is never closed while another thread is using it.
"""

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

import lucene  # PyLucene: Python wrapper around the Java Lucene jars

from app import file_service, pdf_service, text_matching

# The JVM must be started exactly once, before any org.apache.lucene import.
_VM = lucene.initVM(vmargs=["-Djava.awt.headless=true"])

from java.nio.file import Paths  # noqa: E402
from org.apache.lucene.analysis.standard import StandardAnalyzer  # noqa: E402
from org.apache.lucene.analysis.tokenattributes import CharTermAttribute  # noqa: E402
from org.apache.lucene.document import Document, Field, StoredField, StringField, TextField  # noqa: E402
from org.apache.lucene.index import DirectoryReader, IndexWriter, IndexWriterConfig, Term  # noqa: E402
from org.apache.lucene.search import IndexSearcher, PhraseQuery, TermQuery  # noqa: E402
from org.apache.lucene.store import FSDirectory  # noqa: E402

logger = logging.getLogger(__name__)

CONTENT_FIELD = "content"
MAX_QUERY_CHARS = 500


class SearchError(file_service.InvalidInputError):
    """The search query is unusable (empty, too long, or contains no searchable words)."""


def _attach_thread() -> None:
    """Attach the calling thread to the JVM (a no-op if it already is)."""
    _VM.attachCurrentThread()


class PdfSearchIndex:
    """An on-disk Lucene index of the PDFs under ``pdf_root``."""

    def __init__(self, pdf_root: Path, index_path: Path, max_pdf_bytes: int) -> None:
        _attach_thread()
        self._pdf_root = pdf_root
        self._max_pdf_bytes = max_pdf_bytes
        self._lock = threading.RLock()
        self._analyzer = StandardAnalyzer()
        self._directory = FSDirectory.open(Paths.get(str(index_path)))
        # One writer for the life of the process; Lucene itself refuses a second writer
        # on the same directory (write.lock), which protects the index from corruption.
        config = IndexWriterConfig(self._analyzer)
        config.setOpenMode(IndexWriterConfig.OpenMode.CREATE_OR_APPEND)
        self._writer = IndexWriter(self._directory, config)
        self._writer.commit()  # makes sure a brand-new index has a first commit
        self._reader = DirectoryReader.open(self._writer)
        logger.info("Lucene index opened at %s (%d pages)", index_path, self._reader.numDocs())

    # ------------------------------------------------------------------ lifecycle

    def close(self) -> None:
        """Release the reader, writer and directory. Safe to call once at shutdown."""
        _attach_thread()
        with self._lock:
            self._reader.close()
            self._writer.close()
            self._directory.close()
        logger.info("Lucene index closed")

    def page_count(self) -> int:
        """Number of indexed pages (used by the health check)."""
        _attach_thread()
        with self._lock:
            return int(self._reader.numDocs())

    def _refresh_reader(self) -> None:
        """Make the latest committed changes visible to searches."""
        new_reader = DirectoryReader.openIfChanged(self._reader)
        if new_reader is not None:
            self._reader.close()
            self._reader = new_reader

    # ------------------------------------------------------------------ indexing

    def _indexed_files(self) -> dict[str, tuple[int, int]]:
        """Map relative path -> (size, mtime_ns) for every PDF currently in the index."""
        searcher = IndexSearcher(self._reader)
        query = TermQuery(Term("first_page", "1"))
        total = searcher.count(query)
        indexed = {}
        if total:
            stored = searcher.storedFields()
            for hit in searcher.search(query, total).scoreDocs:
                doc = stored.document(hit.doc)
                indexed[doc.get("path")] = (int(doc.get("size")), int(doc.get("mtime_ns")))
        return indexed

    def _page_document(self, relative_path: str, page_number: int, text: str, size: int, mtime_ns: int) -> Document:
        doc = Document()
        doc.add(StringField("path", relative_path, Field.Store.YES))
        doc.add(StoredField("page", str(page_number)))
        doc.add(TextField(CONTENT_FIELD, text, Field.Store.YES))
        doc.add(StoredField("size", str(size)))
        doc.add(StoredField("mtime_ns", str(mtime_ns)))
        if page_number == 1:
            doc.add(StringField("first_page", "1", Field.Store.NO))
        return doc

    def _replace_pdf(self, path: Path) -> None:
        """Replace every indexed page of one PDF with freshly extracted pages."""
        relative_path = file_service.to_relative(self._pdf_root, path)
        stat = path.stat()
        pages = pdf_service.extract_pages(path, self._max_pdf_bytes)  # may raise PdfReadError
        # Delete-then-add under the writer lock: repeated syncs never leave duplicates.
        self._writer.deleteDocuments(Term("path", relative_path))
        for number, text in enumerate(pages, start=1):
            self._writer.addDocument(self._page_document(relative_path, number, text, stat.st_size, stat.st_mtime_ns))

    def sync(self, full_rebuild: bool = False) -> dict:
        """Bring the index in line with the PDF directory.

        New and modified PDFs (different size or modification time) are
        (re)indexed, deleted PDFs are removed, unchanged PDFs are not touched.
        ``full_rebuild`` wipes the index first. A PDF that cannot be read is
        reported in ``failed`` and does not stop the others.
        """
        _attach_thread()
        with self._lock:
            if full_rebuild:
                self._writer.deleteAll()
            indexed = {} if full_rebuild else self._indexed_files()
            on_disk = {}
            for path in file_service.iter_pdf_paths(self._pdf_root):
                stat = path.stat()
                on_disk[file_service.to_relative(self._pdf_root, path)] = (path, (stat.st_size, stat.st_mtime_ns))

            report = {"indexed": [], "updated": [], "removed": [], "failed": [], "unchanged": 0}

            for relative_path in sorted(set(indexed) - set(on_disk)):
                self._writer.deleteDocuments(Term("path", relative_path))
                report["removed"].append(relative_path)

            for relative_path, (path, signature) in sorted(on_disk.items()):
                if indexed.get(relative_path) == signature:
                    report["unchanged"] += 1
                    continue
                try:
                    self._replace_pdf(path)
                except pdf_service.PdfReadError as error:
                    # Drop any stale pages of a PDF that has become unreadable.
                    self._writer.deleteDocuments(Term("path", relative_path))
                    report["failed"].append({"path": relative_path, "error": str(error)})
                    continue
                report["updated" if relative_path in indexed else "indexed"].append(relative_path)

            self._writer.commit()
            self._refresh_reader()
            logger.info(
                "Index sync: %d new, %d updated, %d removed, %d failed, %d unchanged",
                len(report["indexed"]), len(report["updated"]), len(report["removed"]),
                len(report["failed"]), report["unchanged"],
            )
            return report

    # ------------------------------------------------------------------ searching

    def _query_tokens(self, text: str) -> list[str]:
        """Run the query through the same analyzer used at index time."""
        tokens = []
        stream = self._analyzer.tokenStream(CONTENT_FIELD, text)
        term_attribute = stream.addAttribute(CharTermAttribute.class_)
        try:
            stream.reset()
            while stream.incrementToken():
                tokens.append(term_attribute.toString())
            stream.end()
        finally:
            stream.close()
        return tokens

    def _build_query(self, text: str):
        """Literal query: one token -> TermQuery, several tokens -> PhraseQuery."""
        tokens = self._query_tokens(text)
        if not tokens:
            raise SearchError("The query contains no searchable words.")
        if len(tokens) == 1:
            return TermQuery(Term(CONTENT_FIELD, tokens[0]))
        builder = PhraseQuery.Builder()
        for token in tokens:
            builder.add(Term(CONTENT_FIELD, token))
        return builder.build()

    def search(self, query_text: str, limit: int, case_sensitive: bool = False) -> dict:
        """Find PDFs containing a word or phrase, ranked by occurrence count.

        Steps: (1) sync the index, (2) Lucene returns every candidate page,
        (3) occurrences are counted in the page text, (4) results are grouped
        per PDF and sorted by count (ties broken by path).
        """
        query_text = query_text.strip()
        if not query_text:
            raise SearchError("The query must not be empty.")
        if len(query_text) > MAX_QUERY_CHARS:
            raise SearchError(f"The query is longer than {MAX_QUERY_CHARS} characters.")
        if limit < 1:
            raise SearchError("limit must be at least 1.")

        self.sync()  # cheap when nothing changed: it only compares size and mtime
        _attach_thread()
        pattern = text_matching.build_pattern(query_text, case_sensitive)

        with self._lock:
            lucene_query = self._build_query(query_text)
            searcher = IndexSearcher(self._reader)
            candidate_count = searcher.count(lucene_query)
            per_pdf: dict[str, dict] = {}
            if candidate_count:
                stored = searcher.storedFields()
                # Fetch ALL matching pages: ranking by score would lose occurrences.
                for hit in searcher.search(lucene_query, candidate_count).scoreDocs:
                    doc = stored.document(hit.doc)
                    page_text = doc.get(CONTENT_FIELD)
                    occurrences = text_matching.count_occurrences(page_text, pattern)
                    if occurrences == 0:
                        continue  # Lucene matched on tokens, but the literal text differs (or case mismatch)
                    entry = per_pdf.setdefault(doc.get("path"), {"occurrences": 0, "pages": {}, "mtime_ns": int(doc.get("mtime_ns"))})
                    entry["occurrences"] += occurrences
                    entry["pages"][int(doc.get("page"))] = text_matching.make_snippets(page_text, pattern, 1)

        results = [self._format_result(path, entry) for path, entry in per_pdf.items()]
        results.sort(key=lambda item: (-item["occurrences"], item["path"].lower()))
        return {
            "query": query_text,
            "case_sensitive": case_sensitive,
            "total_matching_pdfs": len(results),
            "results": results[:limit],
        }

    @staticmethod
    def _format_result(path: str, entry: dict) -> dict:
        pages = sorted(entry["pages"])
        snippets = []
        for page in pages:  # first snippet of each matching page, deduplicated
            for snippet in entry["pages"][page]:
                if snippet not in snippets and len(snippets) < text_matching.MAX_SNIPPETS_PER_PDF:
                    snippets.append(f"[p.{page}] {snippet}")
        return {
            "name": path.rsplit("/", 1)[-1],
            "path": path,
            "occurrences": entry["occurrences"],
            "pages": pages,
            "snippets": snippets,
            "modified_at": datetime.fromtimestamp(entry["mtime_ns"] / 1e9, tz=timezone.utc).isoformat(),
        }
