"""Read a PDF with GLM-OCR: the text is transcribed from each page image and copied as it is.

A language model only decides what each block of text is (title, body, footnote,
references, ...) and describes the figures; it never retypes the paper. That avoids the
problems of having a model rewrite whole pages: dropped paragraphs and misread words.

Per page:
1. GLM-OCR turns the page image into Markdown, with formulas as LaTeX and tables as
   Markdown tables. It leaves out figures and their captions.
2. Code splits that into blocks; the language model labels each block.
3. Figures are found from their captions in the PDF's own text layer and described by the
   language model from the page image, as are tables GLM-OCR skipped (e.g. ones made of
   chemical structures).
The blocks then go through document.to_speech like any structured format.
"""

import re
from collections.abc import Collection
from pathlib import Path

from . import clean, extract, llm
from .document import Block, _no_notation
from .progress import Progress, print_progress
from .readers import _Sections

MODEL = "glm-ocr"
LABELS = ["title", "authors", "keywords", "metadata", "abstract", "heading", "body", "caption", "table",
          "equation", "footnote", "references", "acknowledgements", "declarations", "noise"]
LABEL_PROMPT = """\
Below are the numbered text blocks of one page of a scientific paper, in reading order \
(long blocks are shortened). Label each block with exactly one of: {labels}.

- title: the paper's title. authors: author names (also a lone name under the title), \
affiliations, emails, and notes about authors such as "* Corresponding author" or "Equal \
contribution".
- keywords: a keywords list. metadata: article type (e.g. "Full Research Paper", \
"Essay"), journal name, volume and pages, dates received, accepted or published, DOI, \
citation info ("Citation: ..."), license and copyright notices, editor names.
- declarations: funding, competing interests, author contributions, data or code availability.
- abstract: the abstract, including its heading.
- heading: a section heading. body: normal running text of the paper.
- caption: a figure or table caption. table: a table. equation: a displayed formula.
- footnote: a footnote. references: entries of the reference list or its heading.
- acknowledgements: the acknowledgements. noise: page numbers, running headers or \
footers, arXiv identifiers, stray fragments.

Answer with the label of each block."""
FIGURE_PROMPT = """\
Below are figures (or tables) on this page of a scientific paper. Describe each for a \
listener who can't see it, in 2 to 5 spoken sentences: what kind of figure it is, what \
it shows (components and how they connect, axes, groups) and what stands out. State a \
conclusion only as far as the figure and its caption show it; don't draw your own. Be \
exact, e.g. follow arrows for the order of steps; leave out details you're unsure about. \
For a table, say what it compares and its main takeaway rather than reading every cell. \
For a chemistry scheme, name the reaction or transformation rather than reading out \
atoms. Plain spoken English: no Markdown, no LaTeX, say math in words.

A listed figure or table may not be on this page after all (its caption can be on a \
different page than the figure). If you can't see it on this page, answer exactly \
"NOT ON PAGE" for it."""
# Answers saying the figure isn't there, also when worded freely ("Table 3 is not present on this page")
NOT_ON_PAGE = re.compile(r"not on page|\bnot (actually )?(present|visible|shown|included|found)\b.{0,40}\b(page|image)"
                         r"|does not appear on this page", re.IGNORECASE)

# Captions of things GLM-OCR doesn't transcribe, as they appear in the PDF's text layer.
FIGURE_CAPTION = re.compile(r"^\s*(Figure|Fig\.|Scheme|Chart|Box)\s*(\d+)\s*[:.|]", re.MULTILINE)
TABLE_CAPTION = re.compile(r"^\s*Table\s*(\d+)\s*[:.|]", re.MULTILINE)
# A footnote marker, which GLM-OCR writes as math: $^4$ or $^{4}$. Kept while labeling (it
# tells the model the block is a footnote), removed afterwards.
FOOTNOTE_MARKER = re.compile(r"\$\^\{?[\d*†‡§¶]+\}?\$")
# Noise (headers, page numbers, notices) is short; a long block is never dropped as noise.
MAX_NOISE_LENGTH = 100
LABEL_PREVIEW = 300  # characters of each block shown to the labeling model
# LaTeX commands GLM-OCR sometimes writes outside of $...$, e.g. "1, 2, \ldots"
LOOSE_LATEX = re.compile(r"\\[A-Za-z]+(?:\{[^{}$]*\})?")
# Notes about the authors, which the model sometimes labels as footnotes
AUTHOR_NOTE = re.compile(r"equal(ly)? contribut|corresponding author|work (was )?(performed|done) while|"
                         r"present address|current address|these authors contributed", re.IGNORECASE)
ONLY_MATH = re.compile(r"\$\$[^$]+\$\$|\$[^$]+\$")
MARKDOWN_HEADING = re.compile(r"^#{1,6}\s+")
# A line of names, like "John P.A. Ioannidis" or "A. Smith, B. Jones and C. Lee"
_NAME = r"(?:[A-Z][\w'’\-]*\.?|[A-Z]\.)[*†‡¤§\d,]*\s*"  # a name or initial, maybe with footnote marks
NAMES = re.compile(rf"^(?:{_NAME}){{2,5}}(?:(?:,|\band\b|&)\s*(?:{_NAME}){{2,5}})*$")


def is_available() -> bool:
    try:
        return MODEL in llm.installed_models()
    except RuntimeError:
        return False


def read_pdf(pdf_path: Path, model: str, sections: Collection[str], progress: Progress = print_progress) -> list[Block]:
    """The paper as labeled blocks. `model` labels blocks and describes figures."""
    texts = [clean.normalize(t) for t in extract.extract_pages(pdf_path)]
    page_count = _pages_to_read(texts, sections)
    vision = "vision" in llm.capabilities(model)
    blocks: list[Block] = []
    state = _Sections()
    state.after_references = False  # a heading after the reference list starts the appendix
    for i in range(page_count):
        progress(f"Reading page {i + 1}/{page_count}...", i / page_count)
        image = extract.render_page(pdf_path, i)
        markdown = llm.generate(MODEL, "Text Recognition:", images=[image], temperature=0)
        page = _page_blocks(markdown, texts[i], image, model, vision, sections, state, first_page=i == 0)
        _append_page(blocks, page)
    llm.unload(MODEL)
    return blocks


def _page_blocks(markdown: str, text_layer: str, image: bytes, model: str, vision: bool,
                 sections: Collection[str], state: _Sections, first_page: bool) -> list[Block]:
    chunks = _split(markdown)
    labels = _labels(chunks, model, first_page)
    page: list[Block] = []
    for chunk, label in zip(chunks, labels):
        if label == "noise":
            continue
        if label == "heading":
            state.appendix = state.appendix or state.after_references
            state.heading(chunk)
        elif label == "references":
            state.kind = None
            state.after_references = True
        kind = label if label != "body" or state.kind is None else state.kind
        appendix = state.appendix and kind not in ("references", "acknowledgements", "declarations")
        text = FOOTNOTE_MARKER.sub("", chunk).strip()
        if kind == "equation":
            text = text.strip("$").strip()
        elif kind != "table":
            text = _tidy(text)
        if not text:
            continue
        if kind == "table" and page and page[-1].kind == "table":
            # GLM-OCR sometimes splits a table, e.g. its last rows; described apart, the
            # pieces lose their context, so they're put back together.
            page[-1].text += "\n" + text
        else:
            page.append(Block(kind, text, appendix=appendix))
    if "figures" in sections and vision:
        _add_figures(page, markdown, text_layer, image, model, state.appendix)
    return page


def _split(markdown: str) -> list[str]:
    """Paragraphs separated by blank lines; a displayed formula is a block of its own."""
    chunks = []
    for paragraph in re.split(r"\n\s*\n", markdown.strip()):
        lines = [line.strip() for line in paragraph.splitlines() if line.strip()]
        if lines and all(line.startswith("$$") for line in lines):
            chunks += lines
        elif lines:
            chunks.append("\n".join(lines) if lines[0].startswith("|") else " ".join(lines))
    return chunks


def _labels(chunks: list[str], model: str, first_page: bool) -> list[str]:
    shown = [chunk[:LABEL_PREVIEW] for chunk in chunks]
    answers = llm.generate_keyed(model, LABEL_PROMPT.format(labels=", ".join(LABELS)), shown, field="label")
    labels = [a if a in LABELS else "body" for a in (str(a or "").strip().lower() for a in answers)]
    for i, chunk in enumerate(chunks):
        if labels[i] == "noise" and len(chunk) > MAX_NOISE_LENGTH:
            labels[i] = "body"  # the model was wrong: real text is never short noise
        if chunk.startswith("|") and labels[i] in ("body", "noise"):
            labels[i] = "table"
        if labels[i] == "footnote" and AUTHOR_NOTE.search(chunk):
            labels[i] = "authors"
        # Only math alone is an equation; prose with inline math is body text, whatever the model said.
        only_math = bool(ONLY_MATH.fullmatch(chunk.strip()))
        if labels[i] == "equation" and not only_math:
            labels[i] = "body"
        elif only_math and labels[i] in ("body", "noise"):
            labels[i] = "equation"
    if first_page:
        # A line of names between the title and the abstract is the author list, even
        # when the model took it for a heading.
        end = next((i for i, l in enumerate(labels) if l in ("abstract", "body")), len(labels))
        for i in range(end):
            if labels[i] in ("heading", "body") and NAMES.match(_tidy(chunks[i])):
                labels[i] = "authors"
    return labels


def _tidy(text: str) -> str:
    """Markdown and stray LaTeX commands out of text; math stays as $...$."""
    pieces = re.split(r"(\$[^$]+\$)", MARKDOWN_HEADING.sub("", text))
    for i, piece in enumerate(pieces):
        if not piece.startswith("$"):
            piece = clean._strip_inline_markdown(piece)
            pieces[i] = LOOSE_LATEX.sub(lambda m: f"${m.group()}$", piece)  # spoken by the formula step
    return "".join(pieces)


def _add_figures(page: list[Block], markdown: str, text_layer: str, image: bytes, model: str, appendix: bool) -> None:
    """Describe the figures on the page, and tables GLM-OCR skipped, after their first mention."""
    names = [f"{m.group(1)} {m.group(2)}" for m in FIGURE_CAPTION.finditer(text_layer)]
    names += [f"Table {n}" for n in TABLE_CAPTION.findall(text_layer)]
    # Tables and text boxes GLM-OCR transcribed itself don't need describing from the image.
    names = [n for n in names if not (n.startswith(("Table", "Box"))
                                      and re.search(rf"(?m)^\W*{n.replace(' ', r'\s*')}\b", markdown))]
    names = list(dict.fromkeys(names))
    if not names:
        return
    descriptions = llm.generate_keyed(model, FIGURE_PROMPT, names, field="description", images=[image])
    for name, description in zip(names, descriptions):
        if not description or NOT_ON_PAGE.search(description):
            continue
        kind, number = name.rsplit(" ", 1)
        label = "Table" if kind == "Table" else "Figure"
        words = "Table" if kind == "Table" else r"Figure|Fig\.|Scheme|Chart|Box"
        mention = re.compile(rf"\b(?:{words})\s*{number}\b")
        at = next((i + 1 for i, b in enumerate(page) if b.kind in ("body", "caption") and mention.search(b.text)),
                  len(page))
        page.insert(at, Block("figure", f"{label} description: {_no_notation(description)}", appendix=appendix))


def _append_page(blocks: list[Block], page: list[Block]) -> None:
    """Add a page's blocks, joining a sentence that runs across the page break."""
    first = next((b for b in page if b.kind == "body"), None)
    last = next((b for b in reversed(blocks) if b.kind == "body"), None)
    if first and last and first.text[:1].islower() and not last.text.rstrip().endswith(clean.SENTENCE_END):
        last.text = f"{last.text} {first.text}"
        page = [b for b in page if b is not first]
    blocks.extend(page)


def _pages_to_read(texts: list[str], sections: Collection[str]) -> int:
    """Up to the page with the references heading, unless the references or appendix are wanted."""
    if "references" in sections or "appendix" in sections:
        return len(texts)
    for i, text in enumerate(texts):
        if any(clean.REFERENCES_HEADING.match(line) for line in text.splitlines()):
            return i + 1
    return len(texts)
