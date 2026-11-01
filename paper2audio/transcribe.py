"""Turn a PDF into speakable text with a local vision model, one page at a time.

The model sees each page as an image, so it reads formulas, figures and tables the way a
person would, and gets the text extracted by pypdf as a reference for the exact wording.
"""

import math
import sys
from collections.abc import Collection
from pathlib import Path

from . import clean, extract, llm

PROMPT = """\
You are preparing a page of a scientific paper to be read aloud by a text-to-speech \
engine, for a listener who cannot see the page.

{intro}

Write out what should be spoken for this page:
- Transcribe the whole page, every paragraph from top to bottom. Figure and table \
descriptions are additions, they never replace the page's text.
- Keep the paper's own wording for normal prose. Do not summarize or shorten it.
- If the page starts in the middle of a sentence, start your output with exactly that \
continuation, before anything else.
- Leave out page numbers, running headers and footers, and footnote markers.
- Keep section headings as their own line.
- Plain text only: no Markdown, no LaTeX, no bullet points.
{rules}
If nothing on this page should be read, output nothing.
{context}
Extracted text:
{text}"""

# Prompt rules for each section, depending on whether the listener wants it.
SECTION_RULES = {
    "title": (None, "Leave out the paper's title."),
    "authors": (None, "Leave out author names, affiliations, email addresses and notes about the authors."),
    "abstract": (None, "Leave out the abstract."),
    "formulas": (
        'Write every formula and math symbol as natural spoken English, e.g. "Q K transpose '
        'divided by the square root of d k".',
        "Leave out displayed equations entirely, and say inline math as briefly as possible in words.",
    ),
    "figures": (
        'Describe each figure or table exactly once, in 2 to 5 spoken sentences starting with '
        '"Figure description:" or "Table description:": what it shows (components and how they '
        "connect, axes, rows and columns) and its main takeaway, using what the caption says. "
        "Be exact about what the figure shows, e.g. follow its arrows for the order of steps; "
        "when unsure about a detail, leave it out rather than guess. "
        "Never output the caption, the labels inside a figure, or the table cells as separate "
        "text; everything about the figure or table goes into that one description. Place it "
        "after the paragraph that first mentions the figure or table, or at the end of the page.",
        "Leave out figures and tables entirely, including their captions and any text inside them.",
    ),
    "footnotes": ('Read footnotes as "Footnote: ..." at the end of the page.', "Leave out footnotes."),
    "citations": (
        'Keep in-text citations and read them naturally, e.g. "reference 12" or "Smith and '
        'colleagues, 2020".',
        "Leave out in-text citation markers like [12] or (Smith et al., 2020); keep author names "
        "that are part of the sentence.",
    ),
    "acknowledgements": (None, "Leave out the acknowledgements."),
    "references": (
        "Read the reference list as plain text, one entry per line.",
        "Leave out the reference list or bibliography entirely.",
    ),
    "appendix": (None, "Leave out anything that comes after the reference list, such as appendices."),
}
INTRO_VISION = """\
You get an image of the page and the raw text extracted from it. The extracted text has \
the right words but garbles math and layout; use the image as the source of truth."""
# For models that can't read images: they only get the extracted text.
INTRO_TEXT = """\
You get the raw text extracted from the page. It has the right words but garbles math \
and layout: reconstruct formulas from the context as well as you can, and leave out \
fragments you can't make sense of, such as text from inside figures."""
FIGURES_RULE_TEXT = 'Read figure and table captions as "Figure caption: ..." or "Table caption: ...".'
CONTEXT = """
The previous page ended with: "{tail}"
If this page continues that sentence or paragraph, continue it without repeating it.
"""
# How much of the previous page's output to pass on as context.
CONTEXT_CHARS = 300
# Smaller models sometimes stop after a figure description and drop the rest of the page.
# Output shorter than this fraction of the extracted text is retried once with a reminder.
# (Pages can legitimately shrink, e.g. when the authors or the references are left out.)
MIN_OUTPUT_RATIO = 0.4
RETRY_REMINDER = """
Important: a previous attempt stopped too early. Transcribe the entire page, every \
paragraph from top to bottom, not just the first part."""

SUMMARY_PROMPT = """\
Below is the full text of a scientific paper, prepared to be read aloud. Write a summary \
that is read to the listener before the paper itself: one paragraph of about 150 to 250 \
words in plain spoken English, with no Markdown, lists or formulas in symbols. Cover the \
question the paper addresses, its approach, and its main results.

Base the summary only on the text below, not on anything you may know about the paper or \
its later influence. Keep a neutral tone: report what the authors claim and found, without \
praise or hype such as "groundbreaking" or "revolutionary".

Paper:
{text}"""


def transcribe_pdf(pdf_path: Path, model: str, sections: Collection[str]) -> str:
    """Return the speakable text of the paper. Returns one paragraph per line."""
    pages = [clean.normalize(text) for text in extract.extract_pages(pdf_path)]
    page_count = _pages_to_read(pages, sections)
    vision = "vision" in llm.capabilities(model)
    if not vision:
        print(f"Note: {model} can't read images, so figures can't be described", file=sys.stderr)
    intro, prompt_rules = (INTRO_VISION if vision else INTRO_TEXT), _rules(sections, vision)
    paragraphs: list[str] = []
    for i in range(page_count):
        print(f"Transcribing page {i + 1}/{page_count}...", file=sys.stderr)
        context = CONTEXT.format(tail=paragraphs[-1][-CONTEXT_CHARS:]) if paragraphs else ""
        prompt = PROMPT.format(intro=intro, rules=prompt_rules, context=context, text=pages[i])
        images = [extract.render_page(pdf_path, i)] if vision else []
        output = llm.generate(model, prompt, images=images)
        if len(output) < MIN_OUTPUT_RATIO * len(pages[i]):
            print("  output looks incomplete, retrying", file=sys.stderr)
            retry = llm.generate(model, prompt + RETRY_REMINDER, images=images)
            output = max(output, retry, key=len)
        _append_page(paragraphs, clean.clean_markdown(output, sections).splitlines())
    return "\n".join(paragraphs)


def summarize(text: str, model: str) -> str:
    """A short spoken summary of the paper, as one paragraph."""
    print("Writing summary...", file=sys.stderr)
    # Room for the paper (roughly 3 characters per token) plus the answer, in steps of 8K.
    num_ctx = 8192 * math.ceil((len(text) / 3 + 2048) / 8192)
    summary = llm.generate(model, SUMMARY_PROMPT.format(text=text), num_ctx=num_ctx)
    return " ".join(clean.clean_markdown(summary).splitlines())


def _rules(sections: Collection[str], vision: bool) -> str:
    rules = [rule for name, (on, off) in SECTION_RULES.items() if (rule := on if name in sections else off)]
    if not vision and "figures" in sections:  # describing a figure needs to see it
        rules[rules.index(SECTION_RULES["figures"][0])] = FIGURES_RULE_TEXT
    return "\n".join(f"- {rule}" for rule in rules)


def _pages_to_read(pages: list[str], sections: Collection[str]) -> int:
    """Pages up to the one with the references heading, unless the references or appendix are wanted."""
    if "references" in sections or "appendix" in sections:
        return len(pages)
    for i, page in enumerate(pages):
        if any(clean.REFERENCES_HEADING.match(line) for line in page.splitlines()):
            return i + 1
    return len(pages)


def _append_page(paragraphs: list[str], page: list[str]) -> None:
    """Add a page's paragraphs, joining a sentence that runs across the page break."""
    if paragraphs and page and not paragraphs[-1].endswith(clean.SENTENCE_END) and page[0][:1].islower():
        paragraphs[-1] = f"{paragraphs[-1]} {page.pop(0)}"
    paragraphs.extend(page)
