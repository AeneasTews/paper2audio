"""A paper as labeled blocks, and turning those blocks into text to be read aloud.

Structured sources (arXiv HTML, JATS XML, EPUB, LaTeX) are converted into blocks by
readers.py. The text of a block is copied as-is; only math is rewritten, into words.

Inside block text, inline math is written as $...$ (LaTeX), and citations and footnote
markers are wrapped in private-use characters so the listener's choices can be applied
after reading: CITE_START [12] CITE_END, NOTE_MARK.
"""

import re
from collections.abc import Collection
from dataclasses import dataclass

from pylatexenc.latex2text import LatexNodes2Text

from . import clean, llm
from .progress import Progress, print_progress

KINDS = ("title", "authors", "keywords", "metadata", "abstract", "heading", "body", "caption", "table",
         "figure", "equation", "footnote", "references", "acknowledgements", "declarations")
# "figure" is a finished spoken description of a figure (from ocr.py), read as it is.
CITE_START, CITE_END = "", ""
NOTE_MARK = ""  # a footnote marker in the text, always dropped
CITATION = re.compile(f"{CITE_START}(.*?){CITE_END}", re.DOTALL)
MATH = re.compile(r"\$(.+?)\$", re.DOTALL)
# Math simple enough to say without a model: "x", "42", "h_{t}", "d_k"
SIMPLE_MATH = re.compile(r"\s*([A-Za-z]|\d+(\.\d+)?)\s*(_\{?\s*([A-Za-z0-9]+)\s*\}?)?\s*")
MATH_BATCH = 40
CONTEXT_CHARS = 80  # of the sentence on either side of a formula, for the model
# HTML and XML text often has spaces around markup: "Figure 1 ," -> "Figure 1,"
SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([,.;:!?)\]])")
# LaTeX that draws something rather than writing math (TikZ pictures, boxes), or is too long
# to be worth reading out: it's left out.
DRAWING = re.compile(r"\\(pgf\w*|tikz\w*|hbox|vbox|mathchoice|includegraphics|rule)\b")
MAX_MATH_LENGTH = 600

MATH_PROMPT = """\
Convert each LaTeX snippet from a scientific paper into how it would be read aloud in \
plain spoken English, e.g. "\\frac{QK^T}{\\sqrt{d_k}}" -> "Q K transpose divided by the \
square root of d k", "x_i^2" -> "x i squared". Use the usual spoken conventions of \
mathematics: font commands just give the letter ("\\mathcal{O}_K" -> "O K", "\\mathbb{R}" -> "R"), \
"[K : \\mathbf{Q}]" -> "the degree of K over Q", "\\ldots" -> "and so on". Say subscripts right after their base \
without "sub" ("d_{model}" -> "d model"), unless that would be ambiguous.

Each snippet is shown in its sentence, marked with ⟦ ⟧. Answer with the spoken form of \
the marked part only, worded so the sentence still reads naturally: don't add a verb \
when the sentence has its own, e.g. "Let ⟦F \\subseteq K⟧ be number fields" -> "F \
subset of K", not "F is a subset of K". Keep numbers as digits ("10000" stays \
"10000"); they are read aloud correctly later. Answer with the spoken text only."""
TABLE_PROMPT = """\
Describe this table from a scientific paper for a listener who can't see it, in 2 to 5 \
spoken sentences: what it compares, its rows and columns, and notable values. Only state \
a conclusion if the caption or the paper's text below states it; don't draw your own. \
Say math in words. Plain text only, no Markdown.

Caption: {caption}

What the paper says about the table: {context}

{table}"""
# How much of the paragraphs mentioning a table is given along with it
TABLE_CONTEXT_CHARS = 1500


@dataclass
class Block:
    kind: str
    text: str
    appendix: bool = False  # after the references: appendices, supplementary material


def to_speech(blocks: list[Block], sections: Collection[str], model: str | None,
              progress: Progress = print_progress) -> str:
    """The text to be read aloud, one paragraph per line."""
    blocks = [b for b in blocks if _wanted(b, sections)]
    paragraphs = []
    for i, block in enumerate(blocks):
        text = block.text.replace(NOTE_MARK, "")
        text = CITATION.sub(r"\1" if "citations" in sections else "", text)
        if "citations" not in sections:
            text = clean.remove_citations(text)  # plain ones like [12], e.g. in OCR text
        text = clean.EMAIL.sub("", clean.URL.sub("", text))
        if not text.strip():
            continue  # e.g. a footnote that only held a marker or a link
        if block.kind == "table":
            if not model:
                continue  # rows of numbers don't make sense read out; the caption is read instead
            # Readers put a table's caption right next to it.
            caption = next((blocks[j].text for j in (i + 1, i - 1)
                            if 0 <= j < len(blocks) and blocks[j].kind == "caption"), "")
            context = _mentions(blocks, caption)
            progress("Describing a table...", None)
            prompt = TABLE_PROMPT.format(caption=caption, context=context or "(nothing found)", table=text)
            text = "Table description: " + _no_notation(llm.generate(model, prompt))
        elif block.kind == "equation":
            text = f"${text}$"
        elif block.kind == "footnote":
            text = f"Footnote: {text}"
        paragraphs.append(text)

    spoken = speak_math([(m.group(1), _context(p, m)) for p in paragraphs for m in MATH.finditer(p)], model, progress)
    paragraphs = [MATH.sub(lambda m: spoken[m.group(1)], p) for p in paragraphs]
    # Blocks are already selected; only tidy up: spaces, URLs, and no letterless lines (phonon fails on those).
    lines = [clean.normalize(SPACE_BEFORE_PUNCTUATION.sub(r"\1", " ".join(p.split()))) for p in paragraphs]
    return "\n".join(clean._clean_lines(lines, sections={"citations"}))


def _wanted(block: Block, sections: Collection[str]) -> bool:
    if block.appendix and "appendix" not in sections:
        return False
    if block.kind in ("heading", "body"):
        return True
    if block.kind in ("caption", "table", "figure"):
        return "figures" in sections
    if block.kind == "equation":
        return "formulas" in sections
    if block.kind == "footnote":
        return "footnotes" in sections
    return block.kind in sections  # title, authors, keywords, metadata, abstract, references, ...


def speak_math(snippets: list[tuple[str, str]], model: str | None,
               progress: Progress = print_progress) -> dict[str, str]:
    """Spoken form of each LaTeX snippet, given as (snippet, the sentence around it).

    A snippet that occurs several times is converted once, in its first context.
    """
    contexts = dict(reversed(snippets))  # first context wins
    spoken = {}
    hard = []
    for snippet in contexts:
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
        progress(f"Converting formulas {start + 1}-{start + len(batch)} of {len(hard)}...", start / len(hard))
        answers = llm.generate_keyed(model, MATH_PROMPT, [contexts[s] for s in batch], field="spoken")
        for snippet, answer in zip(batch, answers):
            spoken[snippet] = _no_notation(answer) if answer else _latex_to_text(snippet)
    return spoken


def _context(paragraph: str, match: re.Match, chars: int = CONTEXT_CHARS) -> str:
    """The snippet marked with ⟦ ⟧, with the words around it (other math left as LaTeX)."""
    before = paragraph[max(0, match.start() - chars):match.start()]
    after = paragraph[match.end():match.end() + chars]
    return f"…{before}⟦{match.group(1).strip()}⟧{after}…"


def _mentions(blocks: list[Block], caption: str) -> str:
    """The paragraphs that mention the table named in the caption ("Table 5: ..."), for context."""
    match = re.match(r"\W*Table\s*(\d+)", caption)
    if not match:
        return ""
    mention = re.compile(rf"\bTables?\s*{match.group(1)}\b")
    text = " ".join(b.text for b in blocks if b.kind == "body" and mention.search(b.text))
    return text[:TABLE_CONTEXT_CHARS]


def _no_notation(spoken: str) -> str:
    """A safety net for the model's answer: notation it left in ("Z^{n+m}") as words."""
    if not re.search(r"[\\^_{}]", spoken):
        return spoken.strip()
    text = _latex_to_text(spoken)
    text = re.sub(r"\^\s*\(([^()]*)\)", r" to the power of \1", text)  # x^(2 pi i)
    text = re.sub(r"\^\s*(\S+)", r" to the power of \1", text)  # x^2, Z^n+m
    return " ".join(re.sub(r"[_{}\\]", " ", text).split())


def _latex_to_text(latex: str) -> str:
    """Without a language model: LaTeX as Unicode text, e.g. "\\sqrt{d_k}" -> "√(d_k)"."""
    try:
        return LatexNodes2Text(math_mode="text").latex_to_text(latex).strip()
    except Exception:  # pylatexenc is lenient, but malformed input can still raise
        return latex
