"""Turn raw PDF page text into prose that sounds reasonable when read aloud."""

import re
import statistics
import unicodedata
from collections import Counter
from collections.abc import Callable

# A line that starts the reference list, e.g. "References" or "7 Bibliography".
REFERENCES_HEADING = re.compile(r"^\s*(\d+\.?\s*)?(references|bibliography)\s*$", re.IGNORECASE)
# Numeric citations: [13], [ 35, 2, 5], [1–5]
CITATION = re.compile(r"\s*\[\s*\d+(\s*[,–-]\s*\d+)*\s*\]")
URL = re.compile(r"(https?://|www\.)\S+?(?=[.,;:!?)]*(\s|$))")  # keep trailing punctuation
EMAIL = re.compile(r"\S+@\S+\.\S+")
SENTENCE_END = (".", "!", "?", ":")
# Stands in for an image while the text is cleaned. Private-use characters can't occur in
# the extracted text, and the word gives the line letters so it isn't dropped as unspeakable.
FIGURE_MARKER = re.compile(r"^\uE000figure (\d+)\uE000$")
FIGURE_INTRO = "Figure description:"

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


def clean_pages(
    pages: list[str],
    images: list[list[bytes]] | None = None,
    describe: Callable[[list[bytes]], list[str | None]] | None = None,
) -> str:
    """Clean text extracted from PDF pages. Returns one paragraph per line.

    If `images` (per page) and `describe` are given, each image is replaced by a spoken
    description, placed at the first paragraph break after the page it appears on.
    Only images that survive the cleanup (e.g. not in the appendix after the
    references) are passed to `describe`, which returns one description per image.
    """
    pages = [unicodedata.normalize("NFKC", p) for p in pages]  # also expands ligatures like "ﬁ"
    figures = [image for page in images or [] for image in page] if describe else []
    if figures:
        markers = iter(range(len(figures)))
        pages = [
            "\n".join([page] + [f"\uE000figure {next(markers)}\uE000" for _ in page_images])
            for page, page_images in zip(pages, images, strict=True)
        ]
    lines = _drop_repeated_lines(pages)
    lines = _cut_references(lines)
    paragraphs = _join_paragraphs(_clean_lines(lines))
    if figures:
        paragraphs = _insert_figures(paragraphs, figures, describe)
    return "\n".join(paragraphs)


def clean_text(text: str) -> str:
    """Clean a plain text file, where blank lines separate paragraphs. Returns one paragraph per line."""
    text = unicodedata.normalize("NFKC", text)
    # Unwrap hard-wrapped paragraphs: join the lines between blank lines.
    paragraphs = [" ".join(p.split()) for p in re.split(r"\n\s*\n", text)]
    return "\n".join(_clean_lines(_cut_references(paragraphs)))


def clean_markdown(text: str) -> str:
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
    return clean_text("\n".join(lines))


def _insert_figures(
    paragraphs: list[str],
    figures: list[bytes],
    describe: Callable[[list[bytes]], list[str | None]],
) -> list[str]:
    """Replace figure markers with spoken descriptions, dropping those that have none."""
    kept = [int(m.group(1)) for p in paragraphs if (m := FIGURE_MARKER.match(p))]
    descriptions = dict(zip(kept, describe([figures[i] for i in kept])))
    result = []
    for paragraph in paragraphs:
        if not (m := FIGURE_MARKER.match(paragraph)):
            result.append(paragraph)
            continue
        # Models sometimes answer in Markdown or several paragraphs; make it one spoken paragraph.
        description = " ".join(clean_markdown(descriptions.get(int(m.group(1))) or "").splitlines())
        if description:
            result.append(f"{FIGURE_INTRO} {description}")
    return result


def _strip_inline_markdown(line: str) -> str:
    for pattern, replacement in MD_INLINE:
        line = pattern.sub(replacement, line)
    return line


def _clean_lines(lines: list[str]) -> list[str]:
    lines = [_clean_line(line) for line in lines]
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
    text_lines = [line for line in lines if not FIGURE_MARKER.match(line)]
    if not text_lines:
        return lines
    full_width = statistics.median(len(line) for line in text_lines)
    paragraphs: list[str] = []
    pending_figures: list[str] = []  # held back until the current paragraph ends
    current = ""
    for line in lines:
        if FIGURE_MARKER.match(line):
            if current:
                pending_figures.append(line)
            else:
                paragraphs.append(line)
            continue
        if current.endswith("-") and line[:1].islower():
            last_word = current.rsplit(" ", 1)[-1]
            if last_word[:1].isupper():
                current += line  # real compound: "English-" + "to-German"
            else:
                current = current[:-1] + line  # re-join hyphenated word: "transduc-" + "tion"
        else:
            current = f"{current} {line}".strip()
        if line.endswith(SENTENCE_END) or len(line) < 0.6 * full_width:
            paragraphs += [current, *pending_figures]
            current = ""
            pending_figures = []
    if current:
        paragraphs.append(current)
    return paragraphs + pending_figures
