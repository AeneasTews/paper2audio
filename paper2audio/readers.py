"""Read structured documents into labeled blocks: HTML (including arXiv's), JATS XML, EPUB, LaTeX."""

import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path, PurePosixPath

from bs4 import BeautifulSoup, NavigableString, Tag
from pylatexenc.latex2text import LatexNodes2Text

from .clean import ACKNOWLEDGEMENTS_HEADING, DECLARATIONS_HEADING, REFERENCES_HEADING
from .document import CITE_END, CITE_START, NOTE_MARK, Block

APPENDIX_HEADING = re.compile(r"^\s*(appendix|appendices|supplementary|supporting information)\b", re.IGNORECASE)
HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
SKIP_TAGS = {"script", "style", "nav", "header", "footer", "aside", "noscript", "button", "form"}


def _squash(text: str) -> str:
    return " ".join(text.split())


class _Sections:
    """Tracks which part of the paper we're in from the headings: references, acknowledgements, appendix."""

    def __init__(self):
        self.kind = None
        self.appendix = False

    def heading(self, text: str) -> None:
        if REFERENCES_HEADING.match(text):
            self.kind = "references"
        elif ACKNOWLEDGEMENTS_HEADING.match(text):
            self.kind = "acknowledgements"
        elif DECLARATIONS_HEADING.match(text):
            self.kind = "declarations"
        else:
            self.kind = None
            self.appendix = self.appendix or bool(APPENDIX_HEADING.match(text))

    def block(self, kind: str, text: str) -> Block | None:
        text = _squash(text)
        if not text:
            return None
        if self.kind and kind in ("body", "footnote"):
            kind = self.kind
        # These are their own sections, wherever they appear.
        appendix = self.appendix and kind not in ("references", "acknowledgements", "declarations")
        return Block(kind, text, appendix=appendix)


# --- HTML ---------------------------------------------------------------------------

def read_html(html: str) -> list[Block]:
    """Read an HTML paper. Papers converted from LaTeX by LaTeXML (arXiv, ar5iv) are understood
    in detail; other HTML is read by its headings, paragraphs, figures and tables."""
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one("article.ltx_document") or soup.body or soup
    for tag in root.find_all(SKIP_TAGS):
        tag.decompose()
    for tag in root.select(".ltx_tag_equation, .ltx_page_header, .ltx_page_footer, .ltx_TOC"):
        tag.decompose()
    _replace_math(root)
    for cite in root.select("cite.ltx_cite"):
        cite.replace_with(f"{CITE_START}{_squash(cite.get_text(' '))}{CITE_END}")

    reader = _HtmlReader()
    reader.walk(root)
    if not any(b.kind == "title" for b in reader.blocks) and soup.title and soup.title.string:
        reader.blocks.insert(0, Block("title", _squash(soup.title.string)))
    return reader.blocks


def _replace_math(root: Tag) -> None:
    """Replace MathML with its LaTeX, as $...$ (or as a block for displayed equations)."""
    for math in root.find_all("math"):
        tex = math.get("alttext") or (math.find("annotation", encoding="application/x-tex") or Tag(name="x")).get_text()
        tex = tex.strip() or _squash(math.get_text(" "))
        math.replace_with(f"${tex}$" if tex else "")


class _HtmlReader:
    def __init__(self):
        self.blocks: list[Block] = []
        self.sections = _Sections()
        self.forced_kind: str | None = None  # inside the abstract, authors, bibliography...

    def add(self, kind: str, text: str) -> None:
        if self.forced_kind and kind in ("body", "heading", "keywords"):
            kind = self.forced_kind
        if block := self.sections.block(kind, text):
            self.blocks.append(block)

    def walk(self, node: Tag) -> None:
        for child in node.children:
            if isinstance(child, Tag):
                self.element(child)

    def element(self, el: Tag) -> None:
        classes = set(el.get("class") or [])
        name = el.name
        if name in HEADINGS:
            text = _squash(el.get_text(" "))
            if "ltx_title_document" in classes or (name == "h1" and not any(b.kind == "title" for b in self.blocks)):
                self.add("title", text)
            elif self.forced_kind == "abstract" and text.lower().rstrip(".:") == "abstract":
                pass  # the abstract's own heading
            else:
                self.sections.heading(text)
                if self.forced_kind is None:
                    self.add("heading", text)
        elif classes & {"ltx_keywords", "keywords", "kwd-group"}:
            self.add("keywords", el.get_text(" "))
        elif "ltx_authors" in classes:
            self.add("authors", el.get_text(" "))
        elif "ltx_abstract" in classes or el.get("id") == "abstract" or "abstract" in classes:
            self.inside("abstract", el)
        elif "ltx_bibliography" in classes:
            self.sections.kind = None
            for item in el.select("li") or [el]:
                self.add("references", item.get_text(" "))
        elif "ltx_acknowledgement" in classes or "ltx_acknowledgements" in classes:
            self.inside("acknowledgements", el)
        elif "ltx_appendix" in classes:
            self.sections.appendix = True
            self.walk(el)
        elif name == "figure" or classes & {"ltx_figure", "ltx_table", "ltx_float"}:
            self.figure(el)
        elif name == "table" and classes & {"ltx_equation", "ltx_equationgroup", "ltx_eqn_table"}:
            for tex in re.findall(r"\$(.+?)\$", el.get_text(" "), re.DOTALL):
                self.add("equation", tex)
        elif name == "table":
            self.add("table", _table_text(el))
        elif name in ("p", "li", "dd") or "ltx_p" in classes:
            self.paragraph(el)
        else:
            self.walk(el)

    def inside(self, kind: str, el: Tag) -> None:
        previous, self.forced_kind = self.forced_kind, kind
        self.walk(el)
        self.forced_kind = previous

    def figure(self, el: Tag) -> None:
        caption = el.find("figcaption") or el.select_one(".ltx_caption")
        for table in el.find_all("table"):
            if not table.find_parent("table"):
                self.add("table", _table_text(table))
        if caption:
            self.add("caption", caption.get_text(" "))

    def paragraph(self, el: Tag) -> None:
        if el.find(["p", "ul", "ol", "table", "figure"]):  # a container, not a paragraph
            self.walk(el)
            return
        notes = []
        for note in el.select(".ltx_note.ltx_role_footnote, .ltx_note"):
            content = note.select_one(".ltx_note_content") or note
            for mark in content.select(".ltx_note_mark, .ltx_tag_note"):
                mark.decompose()
            notes.append(content.get_text(" "))
            note.replace_with(NOTE_MARK)
        self.add("body", el.get_text(" "))
        for note in notes:
            self.add("footnote", note)


def _table_text(table: Tag) -> str:
    rows = [" | ".join(_squash(cell.get_text(" ")) for cell in row.find_all(["td", "th"]))
            for row in table.find_all("tr")]
    return "\n".join(row for row in rows if row.strip(" |"))


# --- JATS XML (PubMed Central, Europe PMC, PLOS, eLife, ...) -------------------------

MML = "{http://www.w3.org/1998/Math/MathML}"
# JATS footnote and section types for declarations (funding, competing interests, ...)
DECLARATION_FOOTNOTES = {"conflict", "coi-statement", "financial-disclosure", "con", "supported-by",
                         "data-availability", "supplementary-material"}
DECLARATION_SECTIONS = {"data-availability", "funding-information", "COI-statement", "author-contributions",
                        "ethics-statement"}


def read_jats(xml: str) -> list[Block]:
    root = ET.fromstring(xml)
    blocks: list[Block] = []
    sections = _Sections()

    def add(kind, text):
        if block := sections.block(kind, text):
            blocks.append(block)

    meta = root.find("front/article-meta")
    if meta is not None:
        if (title := meta.find("title-group/article-title")) is not None:
            add("title", _jats_text(title)[0])
        names = [" ".join(filter(None, [(c.findtext("name/given-names") or ""), (c.findtext("name/surname") or "")]))
                 for c in meta.iter("contrib") if c.get("contrib-type") == "author"]
        affiliations = [_jats_text(a)[0] for a in meta.iter("aff")]
        add("authors", ", ".join(n for n in names if n.strip()) + (". " + " ".join(affiliations) if affiliations else ""))
        if keywords := [_jats_text(k)[0] for group in meta.iter("kwd-group") for k in group.iter("kwd")]:
            add("keywords", "Keywords: " + ", ".join(keywords))
        for funding in meta.iter("funding-statement"):
            add("declarations", _jats_text(funding)[0])
        for abstract in meta.findall("abstract"):
            for el in abstract.iter():
                if el.tag == "p":
                    add("abstract", _jats_text(el)[0])

    def walk(el, kind="body"):
        for child in el:
            tag = child.tag
            if tag == "title":
                text = _jats_text(child)[0]
                sections.heading(text)
                if kind == "body":
                    add("heading", text)
            elif tag == "p":
                text, extra = _jats_text(child)
                add(kind, text)
                for extra_kind, extra_text in extra:
                    add(extra_kind, extra_text)
            elif tag == "disp-formula":
                add("equation", _formula(child))
            elif tag in ("fig", "table-wrap"):
                for extra_kind, extra_text in _float(child):
                    add(extra_kind, extra_text)
            elif tag == "list":
                for item in child.iter("list-item"):
                    for p in item.findall("p"):
                        add(kind, _jats_text(p)[0])
            elif tag == "fn":
                declaration = child.get("fn-type") in DECLARATION_FOOTNOTES
                add("declarations" if declaration else "footnote", " ".join(_jats_text(p)[0] for p in child.iter("p")))
            elif tag == "ref-list":
                sections.kind = None
                for ref in child.iter("ref"):
                    citation = next((c for c in ref if c.tag in ("mixed-citation", "element-citation", "citation")), ref)
                    add("references", " ".join(t.strip() for t in citation.itertext() if t.strip()))
            elif tag == "ack":
                walk(child, "acknowledgements")
            elif tag in ("app", "app-group"):
                sections.appendix = True
                walk(child, kind)
            elif tag == "sec" and child.get("sec-type") in DECLARATION_SECTIONS:
                walk(child, "declarations")
            elif tag in ("sec", "boxed-text", "fn-group", "notes", "back", "body", "def-list"):
                walk(child, kind)

    for part in ("body", "back"):
        if (el := root.find(part)) is not None:
            walk(el)
    return blocks


def _jats_text(el: ET.Element) -> tuple[str, list[tuple[str, str]]]:
    """Text of an element with inline math as $...$, citations and footnote marks marked.
    Also returns figures, tables and footnotes found inside it, as (kind, text)."""
    parts = [el.text or ""]
    extra: list[tuple[str, str]] = []
    for child in el:
        tag = child.tag
        if tag == "xref" and child.get("ref-type") == "bibr":
            parts.append(f"{CITE_START}{''.join(child.itertext())}{CITE_END}")
        elif tag == "xref" and child.get("ref-type") == "fn":
            parts.append(NOTE_MARK)
        elif tag in ("inline-formula", "disp-formula") or tag == f"{MML}math":
            parts.append(f"${_formula(child)}$")
        elif tag in ("fig", "table-wrap"):
            extra += _float(child)
        elif tag == "fn":
            extra.append(("footnote", " ".join(_jats_text(p)[0] for p in child.iter("p"))))
        else:
            text, more = _jats_text(child)
            parts.append(text)
            extra += more
        parts.append(child.tail or "")
    return _squash("".join(parts)), extra


def _float(el: ET.Element) -> list[tuple[str, str]]:
    label = "".join(el.findtext("label") or "")
    caption = el.find("caption")
    blocks = []
    if el.tag == "table-wrap" and (table := el.find(".//table")) is not None:
        rows = [" | ".join(_squash("".join(cell.itertext())) for cell in row) for row in table.iter("tr")]
        blocks.append(("table", "\n".join(rows)))
    if caption is not None:
        text = " ".join(_jats_text(p)[0] for p in caption if p.tag in ("title", "p"))
        blocks.append(("caption", f"{label}: {text}" if label else text))
    return blocks


def _formula(el: ET.Element) -> str:
    """LaTeX for a formula: its tex-math if given, else converted from its MathML."""
    if (tex := el.find(".//tex-math")) is not None and tex.text:
        latex = tex.text
        if "\\begin{document}" in latex:
            latex = latex.split("\\begin{document}", 1)[1].split("\\end{document}", 1)[0]
        return latex.strip().strip("$").strip()
    math = el if el.tag == f"{MML}math" else el.find(f".//{MML}math")
    if math is None:
        return _squash("".join(el.itertext()))
    return math.get("alttext") or _mathml_to_latex(math)


def _mathml_to_latex(el: ET.Element) -> str:
    tag = el.tag.removeprefix(MML)
    kids = [_mathml_to_latex(c) for c in el]
    if tag in ("mi", "mn", "mo", "mtext", "ms"):
        return (el.text or "").strip()
    if tag == "mfrac" and len(kids) == 2:
        return f"\\frac{{{kids[0]}}}{{{kids[1]}}}"
    if tag == "msqrt":
        return f"\\sqrt{{{' '.join(kids)}}}"
    if tag == "mroot" and len(kids) == 2:
        return f"\\sqrt[{kids[1]}]{{{kids[0]}}}"
    if tag in ("msup", "mover") and len(kids) == 2:
        return f"{{{kids[0]}}}^{{{kids[1]}}}"
    if tag in ("msub", "munder") and len(kids) == 2:
        return f"{{{kids[0]}}}_{{{kids[1]}}}"
    if tag in ("msubsup", "munderover") and len(kids) == 3:
        return f"{{{kids[0]}}}_{{{kids[1]}}}^{{{kids[2]}}}"
    if tag == "semantics":
        return kids[0] if kids else ""
    if tag in ("annotation", "annotation-xml"):
        return ""
    return " ".join(k for k in kids if k)


# --- EPUB ---------------------------------------------------------------------------

def read_epub(path: Path) -> list[Block]:
    """An EPUB is zipped XHTML: read the chapters in reading order."""
    with zipfile.ZipFile(path) as epub:
        container = ET.fromstring(epub.read("META-INF/container.xml"))
        opf_path = next(e.get("full-path") for e in container.iter() if e.tag.endswith("rootfile"))
        opf = ET.fromstring(epub.read(opf_path))
        base = PurePosixPath(opf_path).parent
        manifest = {item.get("id"): item for item in opf.iter() if item.tag.endswith("}item")}
        title = next((e.text for e in opf.iter() if e.tag.endswith("}title") and e.text), None)
        blocks = [Block("title", title)] if title else []
        for ref in (e for e in opf.iter() if e.tag.endswith("}itemref")):
            item = manifest.get(ref.get("idref"))
            if item is None or "nav" in (item.get("properties") or "") or "html" not in (item.get("media-type") or ""):
                continue
            chapter = read_html(epub.read(str(base / item.get("href"))).decode("utf-8", errors="replace"))
            blocks += [b if b.kind != "title" else Block("heading", b.text) for b in chapter]
    return blocks


# --- LaTeX --------------------------------------------------------------------------

TEX_STRUCTURE = re.compile(
    r"\\(?P<section>part|chapter|section|subsection|subsubsection|paragraph)\*?\s*(?:\[[^\]]*\])?\s*\{"
    r"|\\begin\{(?P<env>abstract|figure\*?|table\*?|equation\*?|align\*?|gather\*?|multline\*?|eqnarray\*?|displaymath"
    r"|thebibliography|acknowledgments?|acknowledgements?)\}"
    r"|\\(?P<display>\[)|(?P<dollars>\$\$)"
    r"|\\(?P<appendix>appendix)\b"
    r"|\\bibliography\{(?P<bib>[^}]*)\}"
)
DISPLAY_ENVS = ("equation", "align", "gather", "multline", "eqnarray", "displaymath")
TEX_INLINE_MATH = re.compile(r"(?<!\\)\$(.+?)(?<!\\)\$|\\\((.+?)\\\)", re.DOTALL)
TEX_CITE = re.compile(r"\\(?:cite[tp]?|citeauthor|citeyear)\*?(?:\[[^\]]*\])*\{[^}]*\}")
TEX_REF = re.compile(r"~?\\(?:eq|auto|c|C)?ref\{[^}]*\}")
TEX_LABEL = re.compile(r"\\label\{[^}]*\}")
# Layout commands whose arguments are lengths or files, not text.
TEX_LAYOUT = re.compile(
    r"\\(?:vspace|hspace|vskip|hskip|setlength|addtolength|includegraphics|resizebox|scalebox|rule|raisebox)\*?"
    r"(?:\s*\[[^\]]*\])*(?:\s*\{[^{}]*\})*"
)
TEX_FOOTNOTE = re.compile(r"\\(?:footnote|thanks)(?![A-Za-z])\s*(?:\[[^\]]*\])?\s*\{")
TEX_FOOTNOTE_MARK = re.compile(r"\\footnotemark(?:\[[^\]]*\])?")
# Argument-less macro definitions: \newcommand{\dmodel}{d_{\text{model}}} or \def\dmodel{...}
TEX_MACRO_DEFINITION = re.compile(r"\\(?:re)?newcommand\*?\s*\{?\\(\w+)\}?\s*(?=\{)|\\def\s*\\(\w+)\s*(?=\{)")


def read_tex(path: Path) -> list[Block]:
    source = _tex_source(path)
    source = _expand_macros(source, TEX_LAYOUT.sub("", TEX_LABEL.sub("", source)))
    blocks: list[Block] = []
    sections = _Sections()

    def add(kind, text):
        text = text.strip()
        if text and (block := sections.block(kind, text)):
            blocks.append(block)

    def emit(kind, latex):
        """Add LaTeX as a block, followed by its footnotes."""
        text, notes = _tex_text(latex)
        add(kind, text)
        for note in notes:
            add("footnote", note)

    for name in ("title", "author"):
        if (start := source.find(f"\\{name}")) >= 0 and (group := _braced(source, source.find("{", start))):
            emit("title" if name == "title" else "authors", group[0])
    body = source.split("\\begin{document}", 1)[-1].split("\\end{document}", 1)[0]
    body = body.replace("\\maketitle", "")

    position = 0
    while match := TEX_STRUCTURE.search(body, position):
        _tex_paragraphs(body[position:match.start()], emit)
        position = match.end()
        if match["section"]:
            heading, position = _braced(body, match.end() - 1)
            text = _tex_text(heading)[0]
            sections.heading(text)
            add("heading", text)
        elif match["appendix"]:
            sections.appendix = True
        elif match["bib"] is not None:
            bbl = path.with_suffix(".bbl")
            if bbl.exists():
                _tex_bibliography(bbl.read_text(errors="replace"), emit, sections)
        elif match["display"] or match["dollars"]:
            end = body.find("\\]" if match["display"] else "$$", position)
            end = len(body) if end < 0 else end
            add("equation", body[position:end])
            position = end + 2
        else:
            env = match["env"]
            end_tag = f"\\end{{{env}}}"
            end = body.find(end_tag, position)
            end = len(body) if end < 0 else end
            content = body[position:end]
            position = end + len(end_tag)
            base = env.rstrip("*")
            if base == "abstract":
                emit("abstract", content)
            elif base in DISPLAY_ENVS:
                add("equation", content.replace("\\nonumber", "").replace("&", " "))
            elif base in ("figure", "table"):
                if base == "table" and (tabular := re.search(r"\\begin\{tabular\}.*?\\end\{tabular\}", content, re.DOTALL)):
                    rows = [" | ".join(_tex_text(c)[0] for c in row.split("&"))
                            for row in re.sub(r"\\begin\{tabular\}\{[^}]*\}|\\end\{tabular\}|\\hline|\\[a-z]*rule", "",
                                              tabular.group()).split("\\\\")]
                    add("table", "\n".join(r for r in rows if r.strip(" |")))
                if (start := content.find("\\caption")) >= 0 and (group := _braced(content, content.find("{", start))):
                    emit("caption", f"{base.capitalize()}: {group[0]}")
            elif base == "thebibliography":
                _tex_bibliography(content, emit, sections)
            elif base.startswith("acknowledg"):
                previous, sections.kind = sections.kind, "acknowledgements"
                _tex_paragraphs(content, emit)
                sections.kind = previous
    _tex_paragraphs(body[position:], emit)
    return blocks


def _tex_source(path: Path, depth: int = 0) -> str:
    """The file with comments removed and \\input / \\include files inlined."""
    text = re.sub(r"(?<!\\)%.*", "", path.read_text(errors="replace"))
    if depth > 10:
        return text

    def include(match):
        name = match.group(1).strip()
        target = path.parent / (name if name.endswith(".tex") else f"{name}.tex")
        return _tex_source(target, depth + 1) if target.exists() else ""

    return re.sub(r"\\(?:input|include)\{([^}]*)\}", include, text)


def _expand_macros(source: str, body: str) -> str:
    """Replace simple macros (no arguments) defined in the source with their definition."""
    macros = {}
    for match in TEX_MACRO_DEFINITION.finditer(source):
        if group := _braced(source, match.end()):
            macros[match.group(1) or match.group(2)] = group[0]
    for _ in range(3):  # macros can use other macros
        for name, definition in macros.items():
            body = re.sub(rf"\\{name}(?![A-Za-z])\s?", lambda _, d=definition: d + " ", body)
    return body


def _braced(text: str, start: int) -> tuple[str, int] | None:
    """The content of the {...} group starting at text[start], and the position after it."""
    if start < 0 or start >= len(text) or text[start] != "{":
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{" and text[i - 1] != "\\":
            depth += 1
        elif text[i] == "}" and text[i - 1] != "\\":
            depth -= 1
            if depth == 0:
                return text[start + 1:i], i + 1
    return text[start + 1:], len(text)


def _tex_paragraphs(text: str, emit) -> None:
    for paragraph in re.split(r"\n\s*\n|\\par\b", text):
        emit("body", paragraph)


def _tex_text(text: str) -> tuple[str, list[str]]:
    """LaTeX source to plain text, keeping inline math as $...$. Also returns the footnotes."""
    # Footnotes: take them out of the sentence and add them after it.
    notes = []
    text = TEX_FOOTNOTE_MARK.sub(NOTE_MARK, text)
    while match := TEX_FOOTNOTE.search(text):
        group = _braced(text, match.end() - 1)
        if not group:
            break
        notes.append(group[0])
        text = text[:match.start()] + NOTE_MARK + text[group[1]:]
    text = TEX_CITE.sub(f"{CITE_START}[citation]{CITE_END}", text)
    text = TEX_REF.sub("", text)
    # Convert the prose to text, but leave the math as LaTeX for the formula step.
    pieces, position = [], 0
    for match in TEX_INLINE_MATH.finditer(text):
        pieces.append(_latex_prose(text[position:match.start()]))
        pieces.append(f"${(match.group(1) or match.group(2)).strip()}$")
        position = match.end()
    pieces.append(_latex_prose(text[position:]))
    footnotes = []
    for note in notes:
        note_text, nested = _tex_text(note)
        footnotes += [note_text, *nested]
    return _squash("".join(pieces)), footnotes


def _latex_prose(text: str) -> str:
    # Keep the private-use markers: pylatexenc doesn't know them and would keep them anyway.
    try:
        text = LatexNodes2Text(keep_comments=False).latex_to_text(text)
    except Exception:
        pass
    return re.sub(r"(?m)^\s*\*\s+", "", text)  # list bullets


def _tex_bibliography(text: str, emit, sections: _Sections) -> None:
    sections.kind = None
    for item in re.split(r"\\bibitem(?:\[[^\]]*\])?\{[^}]*\}", text)[1:]:
        emit("references", re.sub(r"\\newblock", " ", item))
