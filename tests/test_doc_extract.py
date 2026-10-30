"""Document import: PDF / DOCX / EPUB / text -> plain text (speech_engine/doc_extract.py) and the worker action."""

import json
import sys
import threading
import zipfile
from pathlib import Path

import pytest

from speech_engine import doc_extract
from speech_engine.exceptions import InvalidInputError, SynthesisCancelled

sys.path.insert(0, str(Path(__file__).parent))
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def make_docx(path, paragraphs):
    body = "".join(f'<w:p><w:r><w:t xml:space="preserve">{p}</w:t></w:r></w:p>' for p in paragraphs)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", xml)
    return path


def make_epub(path, chapters):
    items = "".join(f'<item id="c{i}" href="text/c{i}.xhtml" media-type="application/xhtml+xml"/>' for i in range(len(chapters)))
    spine = "".join(f'<itemref idref="c{i}"/>' for i in reversed(range(len(chapters))))   # spine order != file order
    opf = (f'<package xmlns="http://www.idpf.org/2007/opf"><manifest>{items}</manifest><spine>{spine}</spine></package>')
    container = ('<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                 '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/content.opf", opf)
        for i, html in enumerate(chapters):
            z.writestr(f"OEBPS/text/c{i}.xhtml", html)
    return path


def make_pdf(path, lines):
    """A minimal one-page PDF whose content stream really draws text (so pypdf has a text layer to find)."""
    stream = "BT /F1 12 Tf 72 720 Td 14 TL " + " ".join(f"({l}) Tj T*" for l in lines) + " ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream", "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offs).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    Path(path).write_bytes(out)
    return path


def test_docx_paragraphs_in_order(tmp_path):
    p = make_docx(tmp_path / "a.docx", ["First paragraph.", "", "Second one &amp; more."])
    r = doc_extract.extract_text(str(p))
    assert r["text"] == "First paragraph.\n\nSecond one & more."
    assert r["format"] == "docx" and not r["truncated"]


def test_docx_old_doc_or_garbage_gives_a_clear_error(tmp_path):
    p = tmp_path / "old.docx"
    p.write_bytes(b"\xd0\xcf\x11\xe0 not a zip")
    with pytest.raises(InvalidInputError, match="not a valid .docx"):
        doc_extract.extract_text(str(p))


def test_epub_follows_spine_and_drops_markup(tmp_path):
    p = make_epub(tmp_path / "b.epub", ["<html><head><title>x</title><style>p{}</style></head><body><h1>One</h1><p>Alpha &amp; beta.</p></body></html>",
                                        "<html><body><p>Second<br/>chapter</p><script>evil()</script></body></html>"])
    r = doc_extract.extract_text(str(p))
    assert r["text"].index("Second") < r["text"].index("Alpha")          # spine lists chapter 2 first
    assert "Alpha & beta." in r["text"] and "evil" not in r["text"] and "p{}" not in r["text"]
    assert r["chapters"] == 2


def test_pdf_text_layer(tmp_path):
    pytest.importorskip("pypdf")
    p = make_pdf(tmp_path / "c.pdf", ["Hello from a PDF page.", "Second line continues the sentence"])
    r = doc_extract.extract_text(str(p))
    assert "Hello from a PDF page." in r["text"] and r["pages"] == 1
    assert "Second line continues the sentence" in r["text"]


def test_pdf_without_text_says_it_is_a_scan(tmp_path):
    pypdf = pytest.importorskip("pypdf")
    w = pypdf.PdfWriter()
    w.add_blank_page(200, 200)
    with open(tmp_path / "scan.pdf", "wb") as fh:
        w.write(fh)
    with pytest.raises(InvalidInputError, match="no text layer"):
        doc_extract.extract_text(str(tmp_path / "scan.pdf"))


def test_plain_text_and_bad_inputs(tmp_path):
    (tmp_path / "t.txt").write_bytes(b"\xef\xbb\xbfHello\nworld")
    assert doc_extract.extract_text(str(tmp_path / "t.txt"))["text"] == "Hello\nworld"
    (tmp_path / "bad.txt").write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(InvalidInputError, match="UTF-8"):
        doc_extract.extract_text(str(tmp_path / "bad.txt"))
    (tmp_path / "x.exe").write_bytes(b"MZ")
    with pytest.raises(InvalidInputError, match="not supported"):
        doc_extract.extract_text(str(tmp_path / "x.exe"))
    with pytest.raises(InvalidInputError, match="not found"):
        doc_extract.extract_text(str(tmp_path / "missing.pdf"))


def test_empty_document_and_cancel(tmp_path):
    p = make_docx(tmp_path / "e.docx", [""])
    with pytest.raises(InvalidInputError, match="no readable text"):
        doc_extract.extract_text(str(p))
    ev = threading.Event()
    ev.set()
    with pytest.raises(SynthesisCancelled):
        doc_extract.extract_text(str(make_docx(tmp_path / "f.docx", ["x"])), ev)


def test_very_long_text_is_cut_and_flagged(tmp_path, monkeypatch):
    monkeypatch.setattr(doc_extract, "MAX_TEXT_CHARS", 50)
    p = make_docx(tmp_path / "long.docx", ["word " * 40])
    r = doc_extract.extract_text(str(p))
    assert r["truncated"] and len(r["text"]) == 50


def test_worker_extract_text_action(tmp_path):
    from test_worker import W                      # the shared worker harness (protocol 1 helper; we speak 2 here)
    p = make_docx(tmp_path / "w.docx", ["Spoken through the worker."])
    w = W()
    try:
        w.send_raw(json.dumps({"protocol_version": 2, "request_id": "x1", "action": "extract_text", "path": str(p)}).encode())
        m, events = w.until_final("x1")
        assert m["status"] == "success" and m["text"] == "Spoken through the worker." and m["format"] == "docx"
        assert any(e.get("event") == "progress" for e in events)
        w.send_raw(json.dumps({"protocol_version": 2, "request_id": "x2", "action": "extract_text", "path": str(tmp_path / "no.pdf")}).encode())
        m, _ = w.until_final("x2")
        assert m["status"] == "error" and m["code"] == "INVALID_INPUT"
    finally:
        w.p.kill()
