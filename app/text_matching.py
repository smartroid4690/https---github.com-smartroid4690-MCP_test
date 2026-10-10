"""Occurrence counting and snippet extraction on plain text.

Lucene decides *which pages* match; this module decides *how many times* the
term occurs on those pages. It is deliberately independent of Lucene (and of
the JVM) so the counting rules are easy to read and to test.

Counting rules
--------------
* Matching is on the literal text the user typed, not on Lucene tokens.
* Whitespace inside a phrase matches any run of whitespace (PDF line wraps).
* A match must not be glued to other word characters: searching "act" does not
  count "react" or "action".
* Matches do not overlap.
* Case-insensitive unless ``case_sensitive`` is true.
"""

import re

SNIPPET_CONTEXT_CHARS = 60
MAX_SNIPPETS_PER_PDF = 3


def build_pattern(query: str, case_sensitive: bool = False) -> re.Pattern[str]:
    """Compile the literal-match regex for a query."""
    words = query.split()
    if not words:
        raise ValueError("query must contain at least one non-space character")
    body = r"\s+".join(re.escape(word) for word in words)
    # Only demand a word boundary on a side where the query itself has a word character.
    prefix = r"(?<!\w)" if re.match(r"\w", words[0]) else ""
    suffix = r"(?!\w)" if re.search(r"\w$", words[-1]) else ""
    return re.compile(prefix + body + suffix, 0 if case_sensitive else re.IGNORECASE)


def count_occurrences(text: str, pattern: re.Pattern[str]) -> int:
    """Count non-overlapping matches of ``pattern`` in ``text``."""
    return sum(1 for _ in pattern.finditer(text))


def make_snippets(text: str, pattern: re.Pattern[str], limit: int) -> list[str]:
    """Return up to ``limit`` short, whitespace-normalised excerpts around matches."""
    snippets: list[str] = []
    for match in pattern.finditer(text):
        if len(snippets) >= limit:
            break
        start = max(0, match.start() - SNIPPET_CONTEXT_CHARS)
        end = min(len(text), match.end() + SNIPPET_CONTEXT_CHARS)
        excerpt = " ".join(text[start:end].split())
        snippet = ("..." if start > 0 else "") + excerpt + ("..." if end < len(text) else "")
        if snippet not in snippets:
            snippets.append(snippet)
    return snippets
