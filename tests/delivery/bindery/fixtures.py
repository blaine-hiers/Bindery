"""Test fixtures built in code. No binary files are committed to this repo.

Every Office format here is a real zip with real Open XML inside it, so the
extractors are exercised against the same shape of file Word actually writes.
"""

from __future__ import annotations

import io
import zipfile
import zlib
from pathlib import Path

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"

_CORE = ('<?xml version="1.0"?>'
         '<cp:coreProperties '
         'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
         'xmlns:dc="http://purl.org/dc/elements/1.1/">'
         '<dc:creator>{author}</dc:creator></cp:coreProperties>')


def _zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, body in files.items():
            z.writestr(name, body)
    return buf.getvalue()


def make_docx(paragraphs, author="Dana Reyes") -> bytes:
    body = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{p}</w:t></w:r></w:p>' for p in paragraphs)
    doc = (f'<?xml version="1.0"?><w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>')
    return _zip({
        "[Content_Types].xml": '<?xml version="1.0"?><Types/>',
        "word/document.xml": doc,
        "docProps/core.xml": _CORE.format(author=author),
    })


def make_xlsx(rows, author="Bookkeeper") -> bytes:
    """rows: list of lists of strings. Everything goes through sharedStrings."""
    strings, order = {}, []
    for row in rows:
        for cell in row:
            if cell not in strings:
                strings[cell] = len(order)
                order.append(cell)
    shared = ('<?xml version="1.0"?><sst xmlns="%s" count="%d" uniqueCount="%d">%s</sst>'
              % (S, len(order), len(order),
                 "".join(f"<si><t>{v}</t></si>" for v in order)))
    body = []
    for r, row in enumerate(rows, 1):
        cells = "".join(
            f'<c r="A{r}" t="s"><v>{strings[c]}</v></c>' for c in row)
        body.append(f'<row r="{r}">{cells}</row>')
    sheet = ('<?xml version="1.0"?><worksheet xmlns="%s"><sheetData>%s</sheetData></worksheet>'
             % (S, "".join(body)))
    return _zip({
        "[Content_Types].xml": '<?xml version="1.0"?><Types/>',
        "xl/sharedStrings.xml": shared,
        "xl/worksheets/sheet1.xml": sheet,
        "docProps/core.xml": _CORE.format(author=author),
    })


def make_pptx(slides, author="Sales") -> bytes:
    """slides: list of lists of strings (one list of text runs per slide)."""
    files = {"[Content_Types].xml": '<?xml version="1.0"?><Types/>',
             "docProps/core.xml": _CORE.format(author=author)}
    for i, runs in enumerate(slides, 1):
        shapes = "".join(f"<a:p><a:r><a:t>{t}</a:t></a:r></a:p>" for t in runs)
        files[f"ppt/slides/slide{i}.xml"] = (
            f'<?xml version="1.0"?><p:sld xmlns:p="{P}" xmlns:a="{A}">'
            f"<p:cSld><p:spTree>{shapes}</p:spTree></p:cSld></p:sld>")
    return _zip(files)


def make_pdf(lines, compress: bool = False) -> bytes:
    """A small but genuinely valid PDF with a real text layer."""
    parts = ["BT /F1 12 Tf 14 TL 72 720 Td"]
    for line in lines:
        safe = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        parts.append(f"({safe}) Tj T*")
    parts.append("ET")
    content = "\n".join(parts).encode("latin-1")
    if compress:
        stream, extra = zlib.compress(content), b"/Filter /FlateDecode "
    else:
        stream, extra = content, b""

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< " + extra + b"/Length " + str(len(stream)).encode() + b" >>\nstream\n"
        + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += str(n).encode() + b" 0 obj\n" + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n0000000000 65535 f \n"
    for off in offsets:
        out += ("%010d 00000 n \n" % off).encode()
    out += (b"trailer\n<< /Size " + str(len(objects) + 1).encode()
            + b" /Root 1 0 R >>\nstartxref\n" + str(xref).encode() + b"\n%%EOF\n")
    return bytes(out)


def make_subset_font_pdf(words, per_line: bool = False) -> bytes:
    """The awkward-but-normal case: a cut-down font whose letters are numbered
    1, 2, 3 …, with a /ToUnicode table saying what each number means.

    This is what Word, Google Docs and most report generators actually produce,
    so the extractor has to handle it or it handles almost nothing real.
    """
    text = " ".join(words)
    alphabet = sorted(set(text))
    code = {ch: i + 1 for i, ch in enumerate(alphabet)}

    bfchars = "\n".join(f"<{code[ch]:04X}> <{ord(ch):04X}>" for ch in alphabet)
    cmap = (
        "/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
        "1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
        f"{len(alphabet)} beginbfchar\n{bfchars}\nendbfchar\n"
        "endcmap CMapName currentdict /CMap defineresource pop end end"
    ).encode("latin-1")

    lines = []
    if per_line:
        # one Td move per word, vertical — the layout that used to come out as
        # one letter per line
        for n, word in enumerate(words):
            hexs = "".join(f"{code[c]:04X}" for c in word)
            lines.append(f"BT /F1 12 Tf 72 {700 - n * 14} Td <{hexs}> Tj ET")
        content = "\n".join(lines).encode("latin-1")
    else:
        # one TJ array with letter-spacing kerns that must NOT become spaces
        parts = []
        for ch in text:
            parts.append(f"<{code[ch]:04X}>-176")
        content = ("BT /F1 12 Tf 72 700 Td [" + "".join(parts) + "] TJ ET").encode("latin-1")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type0 /BaseFont /AAAAAA+Calibri /Encoding /Identity-H "
        b"/ToUnicode 6 0 R >>",
        b"<< /Length " + str(len(cmap)).encode() + b" >>\nstream\n" + cmap + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.5\n")
    for n, obj in enumerate(objects, 1):
        out += str(n).encode() + b" 0 obj\n" + obj + b"\nendobj\n"
    out += b"trailer\n<< /Size 7 /Root 1 0 R >>\n%%EOF\n"
    return bytes(out)


def make_encrypted_pdf() -> bytes:
    body = make_pdf(["This should never be reached by the extractor at all."])
    return body.replace(b"trailer\n<< /Size", b"trailer\n<< /Encrypt 9 0 R /Size")


def make_scanned_pdf() -> bytes:
    """A PDF with a picture in it and no text layer — the classic old-company file."""
    image = zlib.compress(bytes(range(256)) * 40)
    return (b"%PDF-1.4\n1 0 obj\n<< /Type /XObject /Subtype /Image /Filter /FlateDecode "
            b"/Length " + str(len(image)).encode() + b" >>\nstream\n" + image
            + b"\nendstream\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n")
# Tests live in `system/apps/tests/`, mirroring the app tree. Walk up to the
# apps root, then reflect the path back down to the app this file tests.
_TESTS = Path(__file__).resolve().parent
_ROOT = next(p for p in _TESTS.parents if (p / "_shared" / "appkit.py").is_file())
_APP = _ROOT / _TESTS.relative_to(_ROOT / "tests")

