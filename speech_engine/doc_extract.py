"""Plain-text extraction from documents for the "Import" button and drag & drop: PDF, DOCX, EPUB (and plain text).

Only the text is read. Nothing is executed, nothing is fetched, nothing is sent anywhere, and the file is never modified.
  * DOCX / EPUB are ZIP + XML/HTML; they are read with the standard library only (size limits guard against zip bombs).
  * PDF needs the optional ``pypdf`` package (pure Python). A PDF without a text layer (a scan) has nothing to read; OCR is
    not part of this app and the error says so.
"""

from __future__ import annotations

import posixpath
import re
import threading
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from xml.etree import ElementTree as ET

from .exceptions import InvalidInputError, SynthesisCancelled

MAX_FILE_BYTES = 200 * 1024 * 1024        # refuse absurd inputs before reading them
MAX_PART_BYTES = 100 * 1024 * 1024        # largest single uncompressed ZIP member we will read
MAX_TEXT_CHARS = 2_000_000                # same ceiling as synthesis; longer text is cut and the caller is told
SUPPORTED = {"txt", "text", "md", "markdown", "log", "pdf", "docx", "epub"}
ProgressFn = Callable[[int, str], None]

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "section", "article",
               "pre", "table", "ul", "ol", "dt", "dd", "hr", "figcaption", "header", "footer"}
_SKIP_TAGS = {"script", "style", "head", "title", "svg", "math", "nav"}


def _tidy(text: str) -> str:
    """Normalise whitespace but keep paragraph breaks (a blank line = a longer pause when read aloud)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ").replace("­", "").replace("\x00", "")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _read_member(z: zipfile.ZipFile, name: str) -> bytes:
    try:
        info = z.getinfo(name)
    except KeyError:
        raise InvalidInputError(f"the file is damaged: {name} is missing") from None
    if info.file_size > MAX_PART_BYTES:
        raise InvalidInputError(f"{name} is too large to read safely")
    with z.open(info) as fh:
        data = fh.read(MAX_PART_BYTES + 1)
    if len(data) > MAX_PART_BYTES:
        raise InvalidInputError(f"{name} is too large to read safely")
    return data


# ---------------------------------------------------------------------------------------------------------- DOCX
def _docx_text(path: Path, cancel: threading.Event, progress: ProgressFn) -> tuple[str, dict]:
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise InvalidInputError("this is not a valid .docx file (it may be an old .doc file: save it as .docx first)") from None
    with z:
        if "word/document.xml" not in z.namelist():
            raise InvalidInputError("this is not a Word (.docx) document")
        try:
            root = ET.fromstring(_read_member(z, "word/document.xml"))
        except ET.ParseError as exc:
            raise InvalidInputError(f"the document text could not be read: {exc}") from exc
    paras: list[str] = []
    body = root.find(f"{_W}body")
    paragraphs = list((body if body is not None else root).iter(f"{_W}p"))
    for i, p in enumerate(paragraphs):
        if cancel.is_set():
            raise SynthesisCancelled("cancelled")
        parts: list[str] = []
        for el in p.iter():
            if el.tag == f"{_W}t" and el.text:
                parts.append(el.text)
            elif el.tag == f"{_W}tab":
                parts.append(" ")
            elif el.tag in (f"{_W}br", f"{_W}cr"):
                parts.append("\n")
        paras.append("".join(parts))
        if i % 500 == 0:
            progress(min(95, 95 * i // max(len(paragraphs), 1)), "reading document")
    return "\n\n".join(t for t in paras if t.strip()), {"paragraphs": len(paras)}


# ---------------------------------------------------------------------------------------------------------- EPUB
class _HtmlText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.out.append("\n\n" if tag not in ("br", "li", "tr", "dt", "dd") else "\n")

    def handle_startendtag(self, tag, attrs):
        if tag in _BLOCK_TAGS:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.out.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.out.append(data)


def _html_to_text(raw: bytes) -> str:
    for enc in ("utf-8", "utf-16", "cp1252"):
        try:
            html = raw.decode(enc)
            break
        except UnicodeError:
            continue
    else:
        html = raw.decode("utf-8", "replace")
    p = _HtmlText()
    p.feed(html)
    p.close()
    return "".join(p.out)


def _epub_text(path: Path, cancel: threading.Event, progress: ProgressFn) -> tuple[str, dict]:
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise InvalidInputError("this is not a valid .epub file") from None
    with z:
        names = set(z.namelist())
        order: list[str] = []
        try:
            container = ET.fromstring(_read_member(z, "META-INF/container.xml"))
            rootfile = next(e for e in container.iter() if e.tag.endswith("rootfile")).get("full-path")
            opf_dir = posixpath.dirname(rootfile)
            opf = ET.fromstring(_read_member(z, rootfile))
            manifest = {e.get("id"): e.get("href") for e in opf.iter() if e.tag.endswith("}item") or e.tag == "item"}
            for ref in (e for e in opf.iter() if e.tag.endswith("itemref")):
                href = manifest.get(ref.get("idref"))
                if href:
                    order.append(posixpath.normpath(posixpath.join(opf_dir, href.split("#")[0].replace("%20", " "))))
        except (InvalidInputError, StopIteration, ET.ParseError, TypeError, AttributeError):
            order = []
        order = [n for n in order if n in names]
        if not order:                                                        # no usable spine: read pages in name order
            order = sorted(n for n in names if n.lower().endswith((".xhtml", ".html", ".htm")))
        if not order:
            raise InvalidInputError("no readable text was found in this EPUB")
        sections: list[str] = []
        for i, n in enumerate(order):
            if cancel.is_set():
                raise SynthesisCancelled("cancelled")
            sections.append(_tidy(_html_to_text(_read_member(z, n))))
            progress(min(95, 95 * (i + 1) // len(order)), f"reading chapter {i + 1}/{len(order)}")
    return "\n\n".join(s for s in sections if s), {"chapters": len(order)}


# ---------------------------------------------------------------------------------------------------------- PDF
def _pdf_text(path: Path, cancel: threading.Event, progress: ProgressFn) -> tuple[str, dict]:
    try:
        import pypdf
    except ImportError:
        raise InvalidInputError("PDF import needs the 'pypdf' package (pip install pypdf). Text and Word files work without it.") from None
    try:
        reader = pypdf.PdfReader(str(path))
        if reader.is_encrypted:
            try:
                ok = reader.decrypt("")                                       # many PDFs are 'encrypted' with an empty password
            except Exception:
                ok = 0
            if not ok:
                raise InvalidInputError("this PDF is password protected; remove the password and import it again")
        pages = reader.pages
        n = len(pages)
    except InvalidInputError:
        raise
    except Exception as exc:
        raise InvalidInputError(f"this PDF could not be opened: {type(exc).__name__}: {exc}") from exc
    out: list[str] = []
    for i in range(n):
        if cancel.is_set():
            raise SynthesisCancelled("cancelled")
        try:
            out.append(pages[i].extract_text() or "")
        except Exception:                                                     # one bad page must not lose the others
            out.append("")
        progress(min(95, 95 * (i + 1) // max(n, 1)), f"reading page {i + 1}/{n}")
    text = "\n\n".join(_unwrap(t) for t in out if t.strip())
    if not text.strip():
        raise InvalidInputError("this PDF has no text layer (it looks like a scan). Voicely cannot read images of text from a PDF; "
                                "run OCR on it first, then import the result.")
    return text, {"pages": n}


def _unwrap(page: str) -> str:
    """PDF text comes with a hard line break at the end of every printed line. Join those lines (and undo hyphen
    wraps) but keep blank lines as paragraph breaks."""
    page = page.replace("\r\n", "\n").replace("\r", "\n")
    page = re.sub(r"(\w)-\n(\w)", r"\1\2", page)                              # "inter-\nnational" -> "international"
    page = re.sub(r"(?<![.!?:;\n])\n(?!\n)", " ", page)                       # a line that does not end a sentence continues
    return page


# ---------------------------------------------------------------------------------------------------------- plain text
def _plain_text(path: Path) -> tuple[str, dict]:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        return raw.decode("utf-8"), {}
    except UnicodeDecodeError:
        raise InvalidInputError("this file is not valid UTF-8. Re-save it as UTF-8 text and import again.") from None


# ---------------------------------------------------------------------------------------------------------- entry point
def extract_text(path: str, cancel: threading.Event | None = None, progress: ProgressFn | None = None) -> dict:
    """Return ``{text, format, truncated, chars, ...}``. Raises InvalidInputError with a message fit to show the user."""
    cancel = cancel or threading.Event()
    progress = progress or (lambda pct, msg: None)
    p = Path(path)
    if not p.is_file():
        raise InvalidInputError(f"file not found: {p}")
    ext = p.suffix.lower().lstrip(".")
    if ext not in SUPPORTED:
        raise InvalidInputError(f"'.{ext}' files are not supported (use .txt, .md, .pdf, .docx or .epub)")
    if p.stat().st_size > MAX_FILE_BYTES:
        raise InvalidInputError("this file is too large to import (limit 200 MB)")
    progress(0, "opening")
    if ext == "pdf":
        text, info = _pdf_text(p, cancel, progress)
    elif ext == "docx":
        text, info = _docx_text(p, cancel, progress)
    elif ext == "epub":
        text, info = _epub_text(p, cancel, progress)
    else:
        text, info = _plain_text(p)
    text = text if ext in ("txt", "text", "md", "markdown", "log") else _tidy(text)
    if not text.strip():
        raise InvalidInputError("no readable text was found in this file")
    truncated = len(text) > MAX_TEXT_CHARS
    if truncated:
        text = text[:MAX_TEXT_CHARS]
    progress(100, "done")
    return {"text": text, "format": ext, "chars": len(text), "truncated": truncated, **info}
