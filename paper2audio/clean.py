"""Turn raw PDF page text into prose that sounds reasonable when read aloud."""

import re
import statistics
import unicodedata
from collections import Counter
from collections.abc import Collection

from .config import Settings

# A line that starts the reference list, e.g. "References" or "7 Bibliography".
REFERENCES_HEADING = re.compile(r"^\s*(\d+\.?\s*)?(references|bibliography)\s*$", re.IGNORECASE)
# Numeric citations: [13], [ 35, 2, 5], [1–5]
CITATION = re.compile(r"\s*\[\s*\d+(\s*[,–-]\s*\d+)*\s*\]")
URL = re.compile(r"(https?://|www\.)\S+?(?=[.,;:!?)]*(\s|$))")  # keep trailing punctuation
EMAIL = re.compile(r"\S+@\S+\.\S+")
SENTENCE_END = (".", "!", "?", ":")
# "Abstract" as a heading of its own, or at the start of the abstract paragraph.
ABSTRACT_HEADING = re.compile(r"^\s*abstract\b", re.IGNORECASE)
ACKNOWLEDGEMENTS_HEADING = re.compile(r"^\s*(\d+\.?\s*)?acknowledge?ments?\b", re.IGNORECASE)
# A paragraph this short that matches a heading pattern is the heading alone.
MAX_HEADING_LENGTH = 40

# Markdown syntax
MD_FRONT_MATTER = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
MD_CODE_BLOCK = re.compile(r"^(```|~~~).*?^\1[^\n]*$", re.DOTALL | re.MULTILINE)
MD_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
MD_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
MD_LINK_DEFINITION = re.compile(r"^\s*\[[^\]]+\]:\s*\S+")
MD_BLOCK_START = re.compile(r"^\s*(#{1,6}\s+|>\s?|[-*+]\s+|\d+[.)]\s+)")
MD_INLINE = [
    (re.compile(r"!\[[^\]]*\]\([^)]*\)"), ""),  # images
    (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),  # [text](url)
    (re.compile(r"\[([^\]]+)\]\[[^\]]*\]"), r"\1"),  # [text][ref]
    (re.compile(r"<[^>\n]+>"), ""),  # HTML tags
    (re.compile(r"`([^`]*)`"), r"\1"),  # inline code
    (re.compile(r"(\*\*|__)(.+?)\1"), r"\2"),  # bold
    (re.compile(r"(?<!\w)([*_])(\S(?:.*?\S)?)\1(?!\w)"), r"\2"),  # italic, not snake_case
    (re.compile(r"~~(.+?)~~"), r"\1"),  # strikethrough
]


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text)  # also expands ligatures like "ﬁ"


def clean_pages(pages: list[str], sections: Collection[str] | None = None) -> str:
    """Clean text extracted from PDF pages. Returns one paragraph per line."""
    sections = _default(sections)
    lines = _drop_repeated_lines([normalize(p) for p in pages])
    if "references" not in sections:
        lines = _cut_references(lines)
    paragraphs = _join_paragraphs(_clean_lines(lines, sections))
    return "\n".join(_select_sections(paragraphs, sections))


def clean_text(text: str, sections: Collection[str] | None = None) -> str:
    """Clean a plain text file, where blank lines separate paragraphs. Returns one paragraph per line."""
    sections = _default(sections)
    # Unwrap hard-wrapped paragraphs: join the lines between blank lines.
    paragraphs = [" ".join(p.split()) for p in re.split(r"\n\s*\n", normalize(text))]
    if "references" not in sections:
        paragraphs = _cut_references(paragraphs)
    return "\n".join(_select_sections(_clean_lines(paragraphs, sections), sections))


def clean_markdown(text: str, sections: Collection[str] | None = None) -> str:
    """Strip Markdown syntax, keeping the readable text. Returns one paragraph per line."""
    text = MD_FRONT_MATTER.sub("", text)
    text = MD_CODE_BLOCK.sub("\n", text)  # code doesn't make sense read aloud
    text = MD_HTML_COMMENT.sub("", text)
    lines = []
    for line in text.splitlines():
        if MD_TABLE_SEPARATOR.match(line) or MD_LINK_DEFINITION.match(line):
            continue
        # Headings, list items, quotes and table rows each become a paragraph of their own,
        # so list items aren't read as one run-on sentence.
        block_start = MD_BLOCK_START.match(line)
        if block_start or line.lstrip().startswith("|"):
            lines.append("")
            line = line[block_start.end():] if block_start else re.sub(r"\s*\|\s*", ", ", line.strip().strip("|"))
        lines.append(_strip_inline_markdown(line))
    return clean_text("\n".join(lines), sections)


def _select_sections(paragraphs: list[str], sections: Collection[str]) -> list[str]:
    """Drop the front matter parts and acknowledgements the listener doesn't want.

    Without layout information this is a heuristic: everything before the abstract is front
    matter, its first paragraph the title and the rest authors and affiliations.
    """
    start = next((i for i, p in enumerate(paragraphs) if ABSTRACT_HEADING.match(p)), None)
    if start is not None:
        front, rest = paragraphs[:start], paragraphs[start:]
        abstract_length = 2 if len(rest[0]) <= MAX_HEADING_LENGTH else 1  # heading + paragraph
        paragraphs = (
            (front[:1] if "title" in sections else [])
            + (front[1:] if "authors" in sections else [])
            + (rest[:abstract_length] if "abstract" in sections else [])
            + rest[abstract_length:]
        )
    if "acknowledgements" not in sections:
        paragraphs = _drop_acknowledgements(paragraphs)
    return paragraphs


def _drop_acknowledgements(paragraphs: list[str]) -> list[str]:
    result = []
    skip_next = False
    for paragraph in paragraphs:
        if skip_next:
            skip_next = False
        elif ACKNOWLEDGEMENTS_HEADING.match(paragraph):
            skip_next = len(paragraph) <= MAX_HEADING_LENGTH  # heading alone: the text follows
        else:
            result.append(paragraph)
    return result


def _default(sections: Collection[str] | None) -> Collection[str]:
    return Settings().sections if sections is None else sections


def _strip_inline_markdown(line: str) -> str:
    for pattern, replacement in MD_INLINE:
        line = pattern.sub(replacement, line)
    return line


def _clean_lines(lines: list[str], sections: Collection[str]) -> list[str]:
    lines = [_clean_line(line, keep_citations="citations" in sections) for line in lines]
    # Lines without any letters (page numbers, "_____" separators, table digits) are
    # unspeakable, and phonon fails the whole input if a paragraph has nothing to say.
    return [line for line in lines if re.search(r"[^\W\d_]", line)]


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


def _clean_line(line: str, keep_citations: bool) -> str:
    if not keep_citations:
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
