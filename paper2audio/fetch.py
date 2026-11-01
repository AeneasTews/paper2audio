"""Turn an arXiv ID, DOI or URL into a local file, downloading the best available version.

- arXiv: the HTML version (arxiv.org, then ar5iv for older papers), which keeps every
  formula's LaTeX and marks up sections, captions and footnotes; the PDF if there is none.
- DOI: arXiv DOIs as above; open-access papers in PubMed Central as JATS XML from
  Europe PMC; otherwise the PDF linked from the publisher's page, if it's freely available.
- URL: downloaded as it is.

Downloads are cached in ~/.cache/paper2audio.
"""

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .progress import Progress, print_progress

USER_AGENT = "paper2audio/0.1 (+https://github.com/AeneasTews/paper2audio)"
TIMEOUT = 60  # seconds

ARXIV_ID = r"(\d{4}\.\d{4,5}(?:v\d+)?|[a-z][a-z\-]*(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?)"
ARXIV = re.compile(
    rf"^(?:arxiv:\s*|https?://(?:www\.|export\.)?(?:arxiv\.org|ar5iv\.(?:labs\.)?arxiv\.org)/(?:abs|pdf|html)/)?{ARXIV_ID}(?:\.pdf)?/?$",
    re.IGNORECASE,
)
DOI = re.compile(r"^(?:doi:\s*|https?://(?:dx\.)?doi\.org/)?(10\.\d{4,9}/\S+)$", re.IGNORECASE)
ARXIV_DOI = re.compile(rf"^10\.48550/arxiv\.{ARXIV_ID}$", re.IGNORECASE)
PDF_META = re.compile(r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)', re.IGNORECASE)
PDF_META_REVERSED = re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']citation_pdf_url', re.IGNORECASE)


class FetchError(Exception):
    pass


def resolve(source: str, progress: Progress = print_progress) -> Path:
    """A local file for the input: the file itself, or a download of the paper."""
    path = Path(source).expanduser()
    if path.is_file():
        return path
    if match := ARXIV.match(source.strip()):
        return _arxiv(match.group(1), progress)
    if match := DOI.match(source.strip()):
        return _doi(match.group(1), progress)
    if source.startswith(("http://", "https://")):
        return _url(source, progress)
    raise FetchError(f"{source!r} is not a file, URL, DOI or arXiv ID")


def _arxiv(arxiv_id: str, progress: Progress) -> Path:
    for url in (f"https://arxiv.org/html/{arxiv_id}", f"https://ar5iv.labs.arxiv.org/html/{arxiv_id}"):
        cached = _cache_path(f"arxiv-{arxiv_id}", ".html")
        if cached.exists():
            return cached
        progress(f"Downloading {url}...", None)
        try:
            body, _, final_url = _get(url)
        except FetchError:
            continue
        # Both sites answer with a page even when there's no HTML version of the paper
        # (arxiv.org redirects to the abstract, ar5iv shows "No content available").
        if b"ltx_para" in body and b"No content available" not in body and "/abs/" not in final_url:
            cached.write_bytes(body)
            return cached
    if (cached := _cache_path(f"arxiv-{arxiv_id}", ".pdf")).exists():
        return cached
    return _download(f"https://arxiv.org/pdf/{arxiv_id}", f"arxiv-{arxiv_id}", progress)


def _doi(doi: str, progress: Progress) -> Path:
    if match := ARXIV_DOI.match(doi):
        return _arxiv(match.group(1), progress)
    name = "doi-" + re.sub(r"[^\w.-]", "_", doi)
    for suffix in (".xml", ".pdf"):
        if (cached := _cache_path(name, suffix)).exists():
            return cached

    # Open-access papers in PubMed Central: the full text as JATS XML.
    query = urllib.parse.quote(f'DOI:"{doi}"')
    try:
        body, _, _ = _get(f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?query={query}&format=json&resultType=lite")
        results = json.loads(body).get("resultList", {}).get("result", [])
    except (FetchError, ValueError):
        results = []
    for result in results:
        if result.get("pmcid") and result.get("isOpenAccess") == "Y":
            progress(f"Downloading {result['pmcid']} from Europe PMC...", None)
            body, _, _ = _get(f"https://www.ebi.ac.uk/europepmc/webservices/rest/{result['pmcid']}/fullTextXML")
            cached = _cache_path(name, ".xml")
            cached.write_bytes(body)
            return cached

    # Otherwise the publisher's page, which usually links its PDF in a citation_pdf_url tag.
    progress(f"Looking up https://doi.org/{doi}...", None)
    body, content_type, final_url = _get(f"https://doi.org/{doi}")
    if body.startswith(b"%PDF"):
        return _save(body, name, ".pdf")
    html = body.decode("utf-8", errors="replace")
    if match := PDF_META.search(html) or PDF_META_REVERSED.search(html):
        pdf_url = urllib.parse.urljoin(final_url, match.group(1))
        progress(f"Downloading {pdf_url}...", None)
        try:
            body, _, _ = _get(pdf_url)
        except FetchError:
            body = b""
        if body.startswith(b"%PDF"):  # closed papers answer with a paywall page instead
            return _save(body, name, ".pdf")
    raise FetchError(
        f"couldn't download the full text for DOI {doi}: it may not be open access, or the publisher "
        "blocks downloads. Download the PDF yourself and pass the file instead."
    )


def _url(url: str, progress: Progress) -> Path:
    name = "url-" + re.sub(r"[^\w.-]", "_", url.split("://", 1)[1])[:120]
    return _download(url, name, progress)


def _download(url: str, name: str, progress: Progress) -> Path:
    progress(f"Downloading {url}...", None)
    body, content_type, _ = _get(url)
    return _save(body, name, _suffix(body, content_type))


def _suffix(body: bytes, content_type: str) -> str:
    if body.startswith(b"%PDF"):
        return ".pdf"
    if body.startswith(b"PK") and b"application/epub+zip" in body[:100]:
        return ".epub"
    head = body[:2000].lower()
    if b"<article" in head and (b"jats" in head or b"<front>" in body[:20000].lower()):
        return ".xml"
    if "html" in content_type or b"<html" in head or b"<!doctype html" in head:
        return ".html"
    if "pdf" in content_type:
        raise FetchError("the server said PDF but sent something else; the paper may not be freely available")
    raise FetchError(f"don't know how to read the downloaded file ({content_type or 'unknown type'})")


def _get(url: str) -> tuple[bytes, str, str]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.read(), response.headers.get("Content-Type", ""), response.geturl()
    except urllib.error.HTTPError as e:
        raise FetchError(f"{url}: HTTP {e.code} {e.reason}") from None
    except OSError as e:
        raise FetchError(f"{url}: {e}") from None


def _save(body: bytes, name: str, suffix: str) -> Path:
    path = _cache_path(name, suffix)
    path.write_bytes(body)
    return path


def _cache_path(name: str, suffix: str) -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "paper2audio"
    base.mkdir(parents=True, exist_ok=True)
    return base / (re.sub(r"[^\w.-]", "_", name) + suffix)
