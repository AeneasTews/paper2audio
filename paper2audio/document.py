"""A paper as labeled blocks, and turning those blocks into text to be read aloud.

Structured sources (arXiv HTML, JATS XML, EPUB, LaTeX) are converted into blocks by
readers.py. The text of a block is copied as-is; only math is rewritten, into words.

Inside block text, inline math is written as $...$ (LaTeX), and citations and footnote
markers are wrapped in private-use characters so the listener's choices can be applied
after reading: CITE_START [12] CITE_END, NOTE_MARK.
"""

import re
import sys
from collections.abc import Collection
from dataclasses import dataclass

from pylatexenc.latex2text import LatexNodes2Text

from . import clean, llm

KINDS = ("title", "authors", "abstract", "heading", "body", "caption", "table", "equation",
         "footnote", "references", "acknowledgements")
CITE_START, CITE_END = "", ""
NOTE_MARK = ""  # a footnote marker in the text, always dropped
CITATION = re.compile(f"{CITE_START}(.*?){CITE_END}", re.DOTALL)
MATH = re.compile(r"\$(.+?)\$", re.DOTALL)
# Math simple enough to say without a model: "x", "42", "h_{t}", "d_k"
SIMPLE_MATH = re.compile(r"\s*([A-Za-z]|\d+(\.\d+)?)\s*(_\{?\s*([A-Za-z0-9]+)\s*\}?)?\s*")
MATH_BATCH = 40
# HTML and XML text often has spaces around markup: "Figure 1 ," -> "Figure 1,"
SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([,.;:!?)\]])")
# LaTeX that draws something rather than writing math (TikZ pictures, boxes), or is too long
# to be worth reading out: it's left out.
DRAWING = re.compile(r"\\(pgf\w*|tikz\w*|hbox|vbox|mathchoice|includegraphics|rule)\b")
MAX_MATH_LENGTH = 600

MATH_PROMPT = """\
Convert each LaTeX snippet from a scientific paper into how it would be read aloud in \
plain spoken English, e.g. "\\frac{QK^T}{\\sqrt{d_k}}" -> "Q K transpose divided by the \
square root of d k", "x_i^2" -> "x i squared". Say subscripts right after their base \
without "sub" ("d_{model}" -> "d model"), unless that would be ambiguous. Keep numbers as digits ("10000" stays \
"10000"); they are read aloud correctly later. Answer with the spoken text only."""
TABLE_PROMPT = """\
Describe this table from a scientific paper for a listener who can't see it, in 2 to 5 \
spoken sentences: what it compares, its rows and columns, and the main takeaway. Say \
math in words. Plain text only, no Markdown.

Caption: {caption}

{table}"""


@dataclass
class Block:
    kind: str
    text: str
    appendix: bool = False  # after the references: appendices, supplementary material


def to_speech(blocks: list[Block], sections: Collection[str], model: str | None) -> str:
    """The text to be read aloud, one paragraph per line."""
    blocks = [b for b in blocks if _wanted(b, sections)]
    paragraphs = []
    for i, block in enumerate(blocks):
        text = block.text.replace(NOTE_MARK, "")
        text = CITATION.sub(r"\1" if "citations" in sections else "", text)
        text = clean.EMAIL.sub("", clean.URL.sub("", text))
        if not text.strip():
            continue  # e.g. a footnote that only held a marker or a link
        if block.kind == "table":
            if not model:
                continue  # rows of numbers don't make sense read out; the caption is read instead
            # Readers put a table's caption right next to it.
            caption = next((blocks[j].text for j in (i + 1, i - 1)
                            if 0 <= j < len(blocks) and blocks[j].kind == "caption"), "")
            text = "Table description: " + " ".join(llm.generate(model, TABLE_PROMPT.format(caption=caption, table=text)).split())
        elif block.kind == "equation":
            text = f"${text}$"
        elif block.kind == "footnote":
            text = f"Footnote: {text}"
        paragraphs.append(text)

    spoken = _speak_math([m.group(1) for p in paragraphs for m in MATH.finditer(p)], model)
    paragraphs = [MATH.sub(lambda m: spoken[m.group(1)], p) for p in paragraphs]
    # Blocks are already selected; only tidy up: spaces, URLs, and no letterless lines (phonon fails on those).
    lines = [clean.normalize(SPACE_BEFORE_PUNCTUATION.sub(r"\1", " ".join(p.split()))) for p in paragraphs]
    return "\n".join(clean._clean_lines(lines, sections={"citations"}))


def _wanted(block: Block, sections: Collection[str]) -> bool:
    if block.appendix and "appendix" not in sections:
        return False
    if block.kind in ("heading", "body"):
        return True
    if block.kind in ("caption", "table"):
        return "figures" in sections
    if block.kind == "equation":
        return "formulas" in sections
    if block.kind == "footnote":
        return "footnotes" in sections
    return block.kind in sections  # title, authors, abstract, references, acknowledgements


def _speak_math(snippets: list[str], model: str | None) -> dict[str, str]:
    """Spoken form of each LaTeX snippet."""
    spoken = {}
    hard = []
    for snippet in dict.fromkeys(snippets):
        if DRAWING.search(snippet) or len(snippet) > MAX_MATH_LENGTH:
            spoken[snippet] = ""
        elif match := SIMPLE_MATH.fullmatch(snippet):
            spoken[snippet] = " ".join(filter(None, (match.group(1), match.group(4))))
        elif model:
            hard.append(snippet)
        else:
            spoken[snippet] = _latex_to_text(snippet)
    for start in range(0, len(hard), MATH_BATCH):
        batch = hard[start:start + MATH_BATCH]
        print(f"Converting formulas {start + 1}-{start + len(batch)} of {len(hard)}...", file=sys.stderr)
        answers = llm.generate_keyed(model, MATH_PROMPT, batch, field="spoken")
        for snippet, answer in zip(batch, answers):
            spoken[snippet] = answer.strip() if answer else _latex_to_text(snippet)
    return spoken


def _latex_to_text(latex: str) -> str:
    """Without a language model: LaTeX as Unicode text, e.g. "\\sqrt{d_k}" -> "√(d_k)"."""
    try:
        return LatexNodes2Text(math_mode="text").latex_to_text(latex).strip()
    except Exception:  # pylatexenc is lenient, but malformed input can still raise
        return latex
