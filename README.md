# paper2audio

Turn scientific papers into audio files, so you can listen to them while cycling, walking or commuting instead of reading them on a screen.

paper2audio reads a paper (a PDF, an arXiv ID, a DOI, HTML, JATS XML, EPUB, LaTeX, text or Markdown) and turns it into speech with [ollama](https://ollama.com)'s **phonon** model. For PDFs, a local language model (Gemma 4 by default) looks at each page and writes out what should be spoken: formulas in words, figures and tables described, headers, page numbers and citations left out. You choose which parts of the paper you want to hear. Everything runs locally.

```sh
paper2audio convert --input=arxiv:1706.03762 --output=attention.mp3 --speed=1.5
```

## Requirements

- macOS or Linux
- Python 3.11+
- [ollama](https://ollama.com) with the `phonon` model and a language model, and [uv](https://docs.astral.sh/uv/). `setup.sh` installs these if they're missing.

## Installation

From the project folder:

```sh
./setup.sh            # asks which language model to download
./setup.sh gemma4:12b # or name one directly ("none" for no language model)
```

The script is safe to re-run; anything already set up is skipped. It:

1. installs ollama with the [official installer](https://ollama.com/download) if the `ollama` command isn't found
2. starts the ollama server if it isn't running
3. installs uv with its official installer if it's missing
4. installs the `paper2audio` command (`uv tool install --editable .`), so changes to the code take effect immediately
5. downloads the `phonon` speech model and the language model

### Choosing a language model

The model that fits your machine's memory is recommended (on Macs that's RAM, on Linux the GPU's memory if there is an NVIDIA card). You can pick a smaller one to save download size:

| Model | Memory | Download | Notes |
| --- | --- | --- | --- |
| `gemma4:26b` | 32 GB | 16 GB | Recommended where it fits: reliable and fast for its size |
| `gemma4:12b` | 16 GB | 7.7 GB | Good quality; occasionally stops early on a page and is retried automatically |
| `gemma4:e4b` | 8 GB | 6.6 GB (9.5 GB on Macs) | Smaller download |
| `gemma3:4b` | 8 GB | 3.4 GB | Smallest model that can read images; older |
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
| `.pdf` | Read page by page by the language model, or with the rule-based cleanup without one. |
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
| `authors` | off | Authors and affiliations |
| `abstract` | on | The abstract |
| `figures` | on | Spoken descriptions of figures and tables |
| `formulas` | on | Formulas, spoken in words |
| `footnotes` | on | Footnotes |
| `citations` | off | In-text citations like [12] or (Smith et al., 2020) |
| `acknowledgements` | off | The acknowledgements |
| `references` | off | The reference list |
| `appendix` | off | Everything after the reference list |
| `summary` | off | A short generated summary, read before the paper |

`figures`, `formulas`, `footnotes`, `appendix` and `summary` need a language model. Without one, the rule-based cleanup handles the others with simple heuristics.

### Config file

Defaults for every run go in `~/.config/paper2audio/config.toml` (or `$XDG_CONFIG_HOME/paper2audio/config.toml`). Command-line options override it.

```toml
voice = "Zoey"
speed = 1.5
llm_model = "gemma4:12b"   # or "none"

[sections]
authors = true
summary = true
citations = false
```

### Speed

With `gemma4:26b` or `gemma4:12b` on an M-series Mac, each page takes roughly 10 to 30 seconds, so a 10-page paper takes a few minutes before speaking starts. Speaking is fast: about 30 minutes of audio takes around a minute. Without a language model, the whole conversion takes about a minute.

## How it works

1. **Server** (`server.py`): if no ollama server is running, starts `ollama serve` for the duration of the run and shuts it down afterwards, also on Ctrl+C. An already-running server (e.g. the Ollama desktop app) is used and left alone.
2. **Model** (`hardware.py`, `llm.py`): picks the language model, from the command line, the config file, or memory size. If none is installed, the rule-based cleanup is used.
3. **Fetch and read** (`fetch.py`, `readers.py`, `document.py`): arXiv IDs, DOIs and URLs are downloaded. Structured formats are read into labeled blocks (title, authors, abstract, heading, body, equation, caption, table, footnote, references, acknowledgements, appendix); the chosen sections are kept, text is copied as it is, and only formulas are rewritten, into words.
4. **Transcribe** (`transcribe.py`, PDFs with a language model): renders each page as an image (`extract.py`, with [pypdfium2](https://github.com/pypdfium2-team/pypdfium2)) and sends it to the model with the page's text extracted by [pypdf](https://pypdf.readthedocs.io). The prompt is built from the chosen sections. The end of the previous page is passed along, so sentences across page breaks stay whole. If a page's output looks incomplete, it's retried once. Pages after the references are skipped unless the references or appendix are wanted. The optional summary is written from the full transcript.
5. **Clean** (`clean.py`): the rule-based cleanup for text, Markdown and PDFs without a language model:
   - removes repeated headers and footers, citation markers, URLs, emails and footnote symbols
   - drops the references, acknowledgements and front matter as configured
   - removes lines without any letters (page numbers, `_____` separators)
   - rejoins hyphenated words and glues lines back into paragraphs
   - strips Markdown syntax and skips code blocks; list items and table rows are read one at a time
6. **Speak** (`tts.py`): sends the text to `ollama run phonon` in chunks of about 4,000 characters and stitches the audio together. If phonon rejects a chunk, that chunk is skipped instead of failing the whole run.
7. **Save** (`audio.py`): applies the speed with ffmpeg's `atempo` filter, which keeps the pitch of the voice, and encodes the chosen format (64 kbps for MP3/AAC, 32 kbps Opus, lossless FLAC/WAV/AIFF). This uses [PyAV](https://pyav.basswood-io.com), which bundles ffmpeg's libraries, so ffmpeg doesn't need to be installed. phonon has no speed setting of its own (ollama only accepts speed 1).

## Known limitations

- Language models can make mistakes: figure descriptions may miss or misread details, and a formula can occasionally be read wrongly. Smaller models make more mistakes.
- Without a language model, math comes out of the PDF garbled, figures aren't described, and front matter is detected by simple heuristics.
- Scanned PDFs (no text layer) are untested. A language model that can read images may handle them; the rule-based cleanup can't.
- phonon fails a whole input with `Error: input has no text to speak` if any paragraph contains nothing pronounceable, such as a line of underscores or `* * *`. The cleanup removes these, but keep it in mind when piping text to phonon directly.
