# paper2audio

Turn scientific papers into audio files, so you can listen to them while cycling, walking or commuting instead of reading them on a screen.

paper2audio reads a paper (a PDF, an arXiv ID, a DOI, HTML, JATS XML, EPUB, LaTeX, text or Markdown) and turns it into speech with [ollama](https://ollama.com)'s **phonon** model. A local language model (Gemma 4 by default) speaks formulas in words, describes figures and tables, and leaves out headers, page numbers and citations, while the paper's own text is read as it is. You choose which parts of the paper you want to hear. Everything runs locally.

## Quick start

On macOS or Linux, with Python 3.11+:

```sh
git clone https://github.com/AeneasTews/paper2audio.git
cd paper2audio
./setup.sh      # installs ollama, the models and the commands; asks which language model to use

paper2audio convert --input=arxiv:1706.03762 --output=attention.mp3 --speed=1.5
paper2audio-gui # or use the desktop app
```

`--input` also takes a DOI, a URL or a local file. See [Usage](#usage) for more.

## Requirements

- macOS or Linux
- Python 3.11+
- [ollama](https://ollama.com) with the `phonon` model, a language model and `glm-ocr`, and [uv](https://docs.astral.sh/uv/). `setup.sh` installs these if they're missing.

## Installation

From the project folder:

```sh
./setup.sh            # asks which language model to download
./setup.sh gemma4:12b # or name one directly ("none" for no language model)
./setup.sh --no-gui   # only the command-line tool, without the desktop app
```

The script is safe to re-run; anything already set up is skipped. It:

1. installs ollama with the [official installer](https://ollama.com/download) if the `ollama` command isn't found
2. if no ollama server is running, starts a temporary one for the downloads and stops it again at the end (a server that's already running, like the Ollama app, is left alone)
3. installs uv with its official installer if it's missing
4. installs the `paper2audio` and `paper2audio-gui` commands (`uv tool install --editable '.[gui]'`), so changes to the code take effect immediately
5. downloads the `phonon` speech model, the language model and, with it, `glm-ocr` (2.7 GB), which transcribes PDF pages

If you already have everything, `uv tool install -e '.[gui]'` on its own is enough. Leave out `[gui]` if you only want the command line tool; the desktop app's extra packages ([NiceGUI](https://nicegui.io) and [pywebview](https://pywebview.flowrl.com)) are then not installed.

### Choosing a language model

The model that fits your machine's memory is recommended (on Macs that's RAM, on Linux the GPU's memory if there is an NVIDIA card). You can pick a smaller one to save download size. GLM-OCR (2.7 GB) is downloaded with any of them; it runs alongside the language model, so on a 16 GB machine the two may take turns in memory, which makes each page slower.

| Model | Memory | Download | Notes |
| --- | --- | --- | --- |
| `gemma4:26b` | 32 GB | 16 GB | Recommended where it fits: reliable and fast for its size |
| `gemma4:12b` | 16 GB | 7.7 GB | Good quality |
| `gemma4:e4b` | 8 GB | 6.6 GB (9.5 GB on Macs) | Smaller and faster; formulas and figure descriptions are less reliable |
| `gemma3:4b` | 8 GB | 3.4 GB | Smallest; in tests it often left formulas unconverted, so not recommended |
| `none` | – | – | Rule-based cleanup only: formulas and figures aren't handled |

Any other ollama model works too, with `--llm-model` or in the config file. Models that can't read images get only the extracted text, so they can't describe figures.

## Usage

```sh
# Papers by arXiv ID, DOI or URL: downloaded automatically
paper2audio convert --input=arxiv:1706.03762 --output=paper.mp3
paper2audio convert --input=10.1371/journal.pbio.1002533 --output=paper.mp3
paper2audio convert --input=https://example.org/paper.pdf --output=paper.mp3

# Any common audio format: .mp3, .m4a, .opus, .ogg, .flac, .wav, .aiff
paper2audio convert --input=paper.pdf --output=paper.mp3
paper2audio convert --input=paper.pdf --output=paper.opus

# Faster (or slower) playback, without the voice getting higher
paper2audio convert --input=paper.pdf --output=paper.mp3 --speed=1.5

# Local files: .pdf, .html, .xml (JATS), .epub, .tex, .txt, .md
paper2audio convert --input=notes.txt --output=notes.mp3
paper2audio convert --input=paper/main.tex --output=paper.mp3

# Choose what to hear
paper2audio convert --input=paper.pdf --output=paper.mp3 --include=summary,authors --exclude=figures

# Another language model, or none
paper2audio convert --input=paper.pdf --output=paper.mp3 --llm-model=gemma4:12b
paper2audio convert --input=paper.pdf --output=paper.mp3 --no-llm
```

| Option | Description |
| --- | --- |
| `--input` | The paper: a file, a URL, a DOI or an arXiv ID (required); see [Inputs](#inputs) |
| `--output` | Audio file to write: `.mp3`, `.m4a`, `.opus`, `.ogg`, `.flac`, `.wav` or `.aiff` (required) |
| `--speed` | Playback speed from 0.5 to 4, e.g. `1.5` (default: 1) |
| `--voice` | phonon voice (default: `Zoey`) |
| `--no-disclaimer` | Don't start the audio with the spoken note that it's machine-generated and can contain mistakes |
| `--llm-model` | ollama model that reads the pages (default: the one recommended for this machine, if installed) |
| `--no-llm` | Don't use a language model, only the rule-based cleanup |
| `--include` | Comma-separated sections to read in addition to the defaults |
| `--exclude` | Comma-separated sections to leave out |

### Inputs

| Input | How it's read |
| --- | --- |
| arXiv ID (`arxiv:1706.03762`, `1706.03762`, `hep-th/9711200`, an arxiv.org link, or an arXiv DOI) | The paper's HTML version from arxiv.org, or from [ar5iv](https://ar5iv.labs.arxiv.org) for older papers. It keeps the LaTeX of every formula and marks sections, captions, footnotes and references, so it's read more accurately than the PDF. Falls back to the PDF if there's no HTML version. |
| DOI (`10.1371/...`, `doi:10.1371/...` or a doi.org link) | Open-access papers in PubMed Central (most open-access biology, medicine and many chemistry journals) as JATS XML from [Europe PMC](https://europepmc.org). Otherwise the PDF linked from the publisher's page, if it's freely available; closed papers fail with a message. |
| URL | Downloaded and read according to what it is (PDF, HTML, JATS XML or EPUB). |
| `.pdf` | Each page is transcribed by [GLM-OCR](https://ollama.com/library/glm-ocr) and the text is copied as it is; the language model labels the parts (title, body, footnotes, references, ...) and describes figures. Without GLM-OCR the language model reads whole pages, and without a language model the rule-based cleanup is used. |
| `.html`, `.xml` (JATS), `.epub`, `.tex` | Structured: sections, captions, footnotes, references and formulas are taken from the markup, so section selection is exact. LaTeX files are followed through `\input`/`\include`, and simple macros are expanded. |
| `.txt`, `.md` | Read as text; Markdown syntax is stripped. |

Downloads are cached in `~/.cache/paper2audio`.

In structured formats, formulas are spoken in words by the language model, or shown as Unicode text without one (e.g. `√(d_k)`). Tables are described by the language model and skipped without one; captions are always read when `figures` is on.

### Changing the speed of an existing file

```sh
paper2audio speedup --input=paper.mp3 --speed=1.5                       # writes paper_1.5x.mp3
paper2audio speedup --input=lecture.m4a --speed=1.25 --output=lecture.opus
```

The input can be any audio file ffmpeg can read; the output can be any of the formats above. Each speed change is applied to already-encoded audio, so for the best quality set `--speed` when converting instead of speeding up a file several times.

### Sections

| Section | Default | What it is |
| --- | --- | --- |
| `title` | on | The paper's title |
| `authors` | off | Authors, affiliations, and notes like "corresponding author" |
| `keywords` | off | The keywords list |
| `metadata` | off | Article type, journal, dates, DOI, license |
| `abstract` | on | The abstract |
| `figures` | on | Spoken descriptions of figures and tables |
| `formulas` | on | Formulas, spoken in words |
| `footnotes` | on | Footnotes |
| `citations` | off | In-text citations like [12], [Shl00b] or (Smith et al., 2020). When left out, a citation that's part of the sentence is read as "the cited work": "as shown in [12]" → "as shown in the cited work" |
| `acknowledgements` | off | The acknowledgements |
| `declarations` | off | Funding, competing interests, author contributions, data availability |
| `references` | off | The reference list |
| `appendix` | off | Everything after the reference list |
| `summary` | off | A short generated summary, read before the paper |

`figures`, `formulas`, `footnotes`, `appendix` and `summary` need a language model. Without one, the rule-based cleanup handles the others with simple heuristics.

### Config file

Defaults for every run go in `~/.config/paper2audio/config.toml` (or `$XDG_CONFIG_HOME/paper2audio/config.toml`). Command-line options override it.

```toml
voice = "Zoey"
speed = 1.5
disclaimer = false         # leave out the spoken note at the start (default: true)
llm_model = "gemma4:12b"   # or "none"
zotero_dir = "~/Zotero"    # desktop app: the Zotero library to show (found automatically), or "none"

[sections]
authors = true
summary = true
citations = false
```

### Speed

With GLM-OCR and `gemma4:12b` or `gemma4:26b` on an M-series Mac, each PDF page takes about 50 seconds on average, from 25 seconds for plain text to 2 minutes for pages dense with formulas, so a 10-page paper takes around 10 minutes before speaking starts. arXiv HTML, JATS, EPUB and LaTeX are faster, since only formulas and tables go to the language model. Speaking is fast: about 30 minutes of audio takes around a minute. Without a language model, the whole conversion takes about a minute.

## Desktop app

```sh
paper2audio-gui              # or: paper2audio-gui paper.pdf
```

A PDF reader with a library, laid out like Zotero: the first tab is the **Library**, and every paper opens in its own tab with the reader (toolbar on top, a sidebar on the left, the paper in the middle and the conversion settings on the right).

- **Opening papers:** open a file (**⌘O**: PDF, HTML, JATS XML, EPUB, LaTeX, text or Markdown; several at once open several tabs), or open a paper by arXiv ID, DOI or URL with the link button (**⇧⌘O**); it's downloaded as described under [Inputs](#inputs). Audio for a downloaded paper is saved to your Downloads folder by default, named after the paper.
- **Tabs:** each paper keeps its own scroll position, zoom, annotations and conversion settings. Opening a paper that's already open switches to its tab. **⌘W** closes the tab (on the Library tab, the window), **⌃Tab** / **⌃⇧Tab** or **⇧⌘]** / **⇧⌘[** switch tabs, **⌘1** is the Library and **⌘2**…**⌘9** the papers. The open tabs come back the next time the app starts. One conversion runs at a time; you can keep reading other papers while it runs.
- **Quick search (⌘K):** find any paper by title, author, year or file name (recently opened ones first) and open it in a tab with **↩**. It searches everything the Library knows about, Zotero included.
- **Library:** every paper you open is listed under *Recently opened*. Add papers to *My Library* and sort them into collections, which can be nested: drag papers onto a collection, or right-click them. The toolbar adds files without opening them and makes new collections; right-click a collection to add a subcollection, rename it (or double-click it) or delete it, and drag collections onto each other to nest them. A paper can be in several collections, and removing it from a collection or the library never touches the file. The item pane on the right shows where the file is. The library is stored in `library.json` in your user data folder (`~/Library/Application Support/paper2audio/` on macOS); papers are remembered by their location, so a moved file shows as missing.
- **Zotero:** if you use the [Zotero](https://www.zotero.org) desktop app, its library is shown read-only under *Zotero*, with its collections and group libraries: papers with a PDF on this computer can be opened, searched with ⌘K and added to your own collections. It's read from Zotero's database (a copy of it, so this works while Zotero is open) and refreshed when it changes. The data directory is found from Zotero's settings; set `zotero_dir` in the [config file](#config-file) to use another one, or `"none"` to hide it. Zotero itself is never changed.
- **Reading:** PDF pages are rendered at exactly the size they're shown on screen, including on high-DPI (Retina) displays, so text stays sharp at any zoom. Zoom with the toolbar, **⌘ +** / **⌘ −** / **⌘ 0** (Ctrl on Windows and Linux), Ctrl/⌘ + scroll wheel, or by pinching on the trackpad. HTML, JATS XML, EPUB and LaTeX papers are shown as a formatted document (title, abstract, headings, captions, formulas, footnotes) built from the same blocks that are read aloud; downloaded pages are never run as web pages. Text and Markdown files are shown as text.
- **Selecting text:** text in PDFs can be selected and copied like in any PDF reader.
- **Annotations:** select text and pick a color in the popup to highlight it, or use the toolbar's highlight, underline and note tools (with eight colors). Every annotation gets a card in the sidebar, where you can add a comment, change its color or delete it (or select it and press Delete). Annotations are saved automatically in your user data folder (`~/Library/Application Support/paper2audio/annotations` on macOS), keyed by the PDF's contents. The PDF itself is never changed.
- **Sidebar:** annotations (searchable), page thumbnails, and the PDF's outline. Sections with subsections can be folded with their arrow (Alt-click folds everything under them), or all at once with the expand/collapse buttons.
- **Right pane:** the same settings as the command line: voice, speed, language model (*Automatic* picks one for your machine as described above, or choose an installed model, or *None* for the rule-based cleanup) and which [sections](#sections) to read. Defaults come from the [config file](#config-file). **Convert to audio** becomes **Stop** while it runs; it stops after the current page. Below it, a player plays the paper's audio once it's made, with play/pause, 10 seconds back and forward, a seek bar, and a folder button to find the file; it stays greyed out until then. Switching to another paper doesn't stop audio that's playing. The output defaults to the paper's name with `.mp3` next to the paper; any of the supported formats works.
- **Keyboard shortcuts:** ⌘O open, ⇧⌘O open from arXiv, DOI or URL, ⌘K find a paper, ⌘W close the tab, ⌃Tab next tab, ⌘1…9 go to a tab, ⌘↩ convert or stop, ⌘+ / ⌘− / ⌘0 zoom, ⌥⌘G go to page, ⌥⌘1 / 2 / 3 highlight, underline and note tools, arrow keys, Space, Page Up/Down and Home/End to move through the paper, Esc and Delete for annotations (Ctrl instead of ⌘ on Windows and Linux). ⌘/ or the keyboard button in the toolbar shows the full list.
- **Introduction:** the first time the app opens, a short introduction explains what it does, the models it uses and what your computer needs, including how much memory it has and which language model fits. The help button (?) in the toolbar shows it again.
- **Report an issue:** the bug button in the toolbar opens a form; **Open on GitHub** then opens a new issue on this repository in your browser, filled in with your description and (optionally) system info and the conversion log, with your home folder shown as `~`. You review and submit it on GitHub.
- **Setup:** when the window opens, it checks in the background for what `setup.sh` would install: ollama, the phonon model, and the language model recommended for this machine (or the one set in the config file) with GLM-OCR. If something is missing, a dialog lists it and **Install** runs `setup.sh --no-command` in the background, showing the download progress. The language model and GLM-OCR are optional and can be unticked. The wrench button in the toolbar opens this dialog at any time and reloads the voices and models. If the ollama installer needs a password it can't ask for here, run `./setup.sh` in a terminal instead.

It opens as a native window (no browser needed) and follows the system's light or dark mode. Under the hood it's a [NiceGUI](https://nicegui.io) app served only on `127.0.0.1` and shown with the system's web view. Closing the window during a conversion stops it after the current step, so a temporary ollama server is still shut down.

Icons are from [Lucide](https://lucide.dev) (ISC license, see `paper2audio/reader/icons/`).

## How it works

1. **Server** (`server.py`): if no ollama server is running, starts `ollama serve` for the duration of the run and shuts it down afterwards, also on Ctrl+C. An already-running server (e.g. the Ollama desktop app) is used and left alone.
2. **Model** (`hardware.py`, `llm.py`): picks the language model, from the command line, the config file, or memory size. If none is installed, the rule-based cleanup is used.
3. **Fetch and read** (`fetch.py`, `readers.py`, `document.py`): arXiv IDs, DOIs and URLs are downloaded. Structured formats are read into labeled blocks (title, authors, abstract, heading, body, equation, caption, table, footnote, references, acknowledgements, appendix); the chosen sections are kept, text is copied as it is, and only formulas are rewritten, into words.
4. **Read PDFs** (`ocr.py`, with a language model and GLM-OCR): renders each page as an image (`extract.py`, with [pypdfium2](https://github.com/pypdfium2-team/pypdfium2)) and has GLM-OCR transcribe it into Markdown, with formulas as LaTeX and tables as Markdown tables. The text is split into blocks, and the language model only labels each block (title, authors, abstract, heading, body, caption, table, equation, footnote, references, ...); the text itself is copied, never retyped. GLM-OCR leaves out figures, so they're found from their captions in the PDF's text layer ([pypdf](https://pypdf.readthedocs.io)) and described by the language model from the page image, as are tables GLM-OCR skipped. Sentences across page breaks are joined, and pages after the references are skipped unless the references or appendix are wanted. The blocks then go through the same steps as the structured formats.
   Without GLM-OCR, `transcribe.py` has the language model write out whole pages instead (less exact: it can drop or misread text). The optional summary is written from the full text.
5. **Clean** (`clean.py`): the rule-based cleanup for text, Markdown and PDFs without a language model:
   - removes repeated headers and footers, citation markers, URLs, emails and footnote symbols
   - drops the references, acknowledgements and front matter as configured
   - removes lines without any letters (page numbers, `_____` separators)
   - rejoins hyphenated words and glues lines back into paragraphs
   - strips Markdown syntax and skips code blocks; list items and table rows are read one at a time
6. **Speak** (`tts.py`): sends the text to `ollama run phonon` in chunks of about 4,000 characters and stitches the audio together. If phonon rejects a chunk, that chunk is skipped instead of failing the whole run.
7. **Disclaimer** (`disclaimer.py`): every audio file starts with a short spoken note that it was made by machine learning models and can contain mistakes, followed by a soft chime before the paper begins (`--no-disclaimer` or `disclaimer = false` in the config file leaves it out). The recording for the default voice ships with paper2audio (`paper2audio/assets/`); for other voices it's recorded once on first use and cached.
8. **Save** (`audio.py`): applies the speed with ffmpeg's `atempo` filter, which keeps the pitch of the voice, and encodes the chosen format (64 kbps for MP3/AAC, 32 kbps Opus, lossless FLAC/WAV/AIFF). This uses [PyAV](https://pyav.basswood-io.com), which bundles ffmpeg's libraries, so ffmpeg doesn't need to be installed. phonon has no speed setting of its own (ollama only accepts speed 1).

`paper2audio/pipeline.py` runs these steps; both `cli.py` and the desktop app (`gui.py`) call it.

## Known limitations

- Language models can make mistakes: figure descriptions may miss or misread details, and a formula can occasionally be read wrongly. Smaller models make more mistakes.
- Without a language model, math comes out of the PDF garbled, figures aren't described, and front matter is detected by simple heuristics.
- Scanned PDFs (no text layer) are untested. GLM-OCR reads page images, so they may work, but figures can't be found (that relies on the text layer); the rule-based cleanup can't read them at all.
- phonon fails a whole input with `Error: input has no text to speak` if any paragraph contains nothing pronounceable, such as a line of underscores or `* * *`. The cleanup removes these, but keep it in mind when piping text to phonon directly.
