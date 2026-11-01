"""Turn raw PDF page text into prose that sounds reasonable when read aloud."""

import re
import statistics
import unicodedata
from collections import Counter

# A line that starts the reference list, e.g. "References" or "7 Bibliography".
REFERENCES_HEADING = re.compile(r"^\s*(\d+\.?\s*)?(references|bibliography)\s*$", re.IGNORECASE)
# Numeric citations: [13], [ 35, 2, 5], [1–5]
CITATION = re.compile(r"\s*\[\s*\d+(\s*[,–-]\s*\d+)*\s*\]")
URL = re.compile(r"(https?://|www\.)\S+")
EMAIL = re.compile(r"\S+@\S+\.\S+")
SENTENCE_END = (".", "!", "?", ":")


def clean_pages(pages: list[str]) -> str:
    """Return cleaned text with one paragraph per line."""
    pages = [unicodedata.normalize("NFKC", p) for p in pages]  # also expands ligatures like "ﬁ"
    lines = _drop_repeated_lines(pages)
    lines = _cut_references(lines)
    lines = [_clean_line(line) for line in lines]
    # Lines without any letters (page numbers, "_____" separators, table digits) are
    # unspeakable, and phonon fails the whole input if a paragraph has nothing to say.
    lines = [line for line in lines if re.search(r"[^\W\d_]", line)]
    return "\n".join(_join_paragraphs(lines))


def _drop_repeated_lines(pages: list[str]) -> list[str]:
    """Flatten pages into lines, dropping headers/footers that repeat on most pages."""
    page_lines = [[l.strip() for l in p.splitlines() if l.strip()] for p in pages]
    repeated: set[str] = set()
    if len(pages) >= 3:
        counts = Counter(line for lines in page_lines for line in set(lines))
        repeated = {line for line, n in counts.items() if n >= len(pages) / 2}
    return [line for lines in page_lines for line in lines if line not in repeated]


def _cut_references(lines: list[str]) -> list[str]:
    for i in range(len(lines) - 1, -1, -1):
        if REFERENCES_HEADING.match(lines[i]):
            return lines[:i]
    return lines


def _clean_line(line: str) -> str:
    line = CITATION.sub("", line)
    line = URL.sub("", line)
    line = EMAIL.sub("", line)
    line = re.sub(r"[∗†‡§¶]", "", line)  # footnote / affiliation markers
    return re.sub(r"\s+", " ", line).strip()


def _join_paragraphs(lines: list[str]) -> list[str]:
    """PDF text is broken at the end of every printed line; glue lines back into paragraphs.

    A line ends a paragraph if it ends a sentence or is noticeably shorter than a full
    line (headings, last line of a paragraph).
    """
    if not lines:
        return []
    full_width = statistics.median(len(line) for line in lines)
    paragraphs: list[str] = []
    current = ""
    for line in lines:
        if current.endswith("-") and line[:1].islower():
            last_word = current.rsplit(" ", 1)[-1]
            if last_word[:1].isupper():
                current += line  # real compound: "English-" + "to-German"
            else:
                current = current[:-1] + line  # re-join hyphenated word: "transduc-" + "tion"
        else:
            current = f"{current} {line}".strip()
        if line.endswith(SENTENCE_END) or len(line) < 0.6 * full_width:
            paragraphs.append(current)
            current = ""
    if current:
        paragraphs.append(current)
    return paragraphs
