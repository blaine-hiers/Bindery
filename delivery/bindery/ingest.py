"""ingest — walk a folder of real client documents and pull the words out of them.

Standard library only. Two rules govern everything in this file:

  1. **Never write to the source folder.** Not a temp file, not a lock file,
     nothing. An advisor who mangles a client's files is finished. Every file
     is opened read-only, once, and closed.
  2. **Never crash, never silently drop.** A locked file, a corrupt PDF, a
     500 MB export, a filename full of emoji — each one comes back as a record
     with a status and a plain-English reason. "We could not read 340 of your
     files and here is why" is itself a finding worth being paid for.

Every file the walker sees becomes exactly one record:

    {"rel", "path", "name", "folder", "ext", "size", "mtime",
     "status": "ok" | "unreadable" | "skipped",
     "reason": None | "plain English why",
     "text", "nwords", "chash", "author"}
"""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import Path

__all__ = [
    "ingest_folder", "walk_files", "extract_file", "extract_bytes",
    "decode_bytes", "extract_docx", "extract_xlsx", "extract_pptx",
    "extract_pdf", "strip_markup", "MAX_FILE_BYTES", "IGNORE_DIRS",
    "TEXT_EXTS", "OFFICE_EXTS",
]

# ---------------------------------------------------------------- limits

MAX_FILE_BYTES = 40 * 1024 * 1024      # anything bigger is reported, not read
MAX_TEXT_CHARS = 1_500_000             # keep one monster file from eating memory
# One part inside an Office file cannot be bigger than this once unpacked. A
# .docx is a zip, and a zip can claim a 40 KB file unpacks to four gigabytes.
# Word would refuse it too; we refuse it on the declared size rather than
# finding out by running the machine out of memory.
MAX_ZIP_MEMBER_BYTES = 80 * 1024 * 1024

IGNORE_DIRS = {
    ".git", ".hg", ".svn", ".bzr",
    "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".venv", "venv", "env", ".tox", ".gradle", ".idea", ".vscode",
    "$recycle.bin", "system volume information", ".trash", ".trashes",
    ".obsidian", "dist", "build", ".next", ".cache",
    # Sync tools leave half-transferred files in these. Every one of them is a
    # meaningless extensionless blob, and on a Google Drive folder there can be
    # hundreds — enough to swamp the "not documents" count in the report.
    ".dropbox.cache", ".tmp.driveupload", ".tmp.drivedownload",
    ".onedrive.tmp", ".sync", ".synctemp", ".seafile-data", "$tf",
}

IGNORE_FILES = {".ds_store", "thumbs.db", "desktop.ini", "icon\r", ".gitignore"}

# Office lock files (~$Budget.xlsx), editor swap files, partial downloads.
IGNORE_PATTERNS = (
    re.compile(r"^~\$"),
    re.compile(r"^\..*\.sw[a-p]$"),
    re.compile(r"\.(tmp|temp|part|crdownload|partial|lock)$", re.I),
)

TEXT_EXTS = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".log",
             ".rtf", ".ini", ".cfg", ".yml", ".yaml", ".srt", ".vtt"}
MARKUP_EXTS = {".html", ".htm", ".xml", ".xhtml", ".svg"}
OFFICE_EXTS = {".docx", ".xlsx", ".pptx", ".docm", ".xlsm", ".pptm"}
PDF_EXTS = {".pdf"}

READABLE_EXTS = TEXT_EXTS | MARKUP_EXTS | OFFICE_EXTS | PDF_EXTS

# Old binary Office formats. We can say something useful about them, which is
# better than "skipped: .doc".
LEGACY_OFFICE = {
    ".doc": "old Word format (before 2007) — save it as .docx and it becomes searchable",
    ".xls": "old Excel format (before 2007) — save it as .xlsx and it becomes searchable",
    ".ppt": "old PowerPoint format (before 2007) — save it as .pptx and it becomes searchable",
    ".wpd": "WordPerfect format — nothing here can read it",
    ".pub": "Microsoft Publisher format — nothing here can read it",
    ".msg": "saved Outlook email — export the folder to .txt or .csv to make it searchable",
    ".one": "OneNote notebook — export the sections to Word or PDF to make them searchable",
}


# ---------------------------------------------------------------- text plumbing

def decode_bytes(raw: bytes) -> str:
    """Bytes to text, trying the encodings real business files actually use.

    latin-1 maps every byte to a character, so the chain can never throw. A file
    of raw binary comes back as nonsense, which the word-count check downstream
    then treats as 'no meaningful text' rather than a crash.
    """
    # utf-8-sig is first because it is utf-8 plus "throw away a leading byte-order
    # mark if there is one" — Notepad and Excel both write that mark.
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("latin-1", errors="replace")


def tidy(text: str) -> str:
    """Normalise whitespace without destroying paragraph structure."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", " ")
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r"[ \t   ]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:MAX_TEXT_CHARS]


_TAGS_WITH_BODY = re.compile(
    r"<(script|style|head)\b.*?</\1>", re.I | re.S)
_TAG = re.compile(r"<[^>]{0,4000}>", re.S)
_BLOCK_END = re.compile(r"</(p|div|br|li|tr|h[1-6]|section|article)\s*>", re.I)


def strip_markup(text: str) -> str:
    """HTML/XML to readable text. Deliberately simple — no parser to trip over."""
    import html as _html
    text = _TAGS_WITH_BODY.sub(" ", text)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = _BLOCK_END.sub("\n", text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = _TAG.sub(" ", text)
    return tidy(_html.unescape(text))


def content_hash(text: str) -> str:
    """Hash of the *meaning*, not the bytes: case and spacing folded away.

    Two price lists saved on different days with a different header wrap are the
    same document as far as an owner is concerned.
    """
    norm = re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


def _has_real_words(text: str, minimum: int = 12) -> bool:
    stripped = "".join(ch for ch in text if not ch.isspace())
    if len(stripped) < minimum:
        return False
    letters = sum(1 for ch in stripped if ch.isalnum())
    return letters >= 0.45 * len(stripped)


# ---------------------------------------------------------------- Office (zip) formats

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_DC = "{http://purl.org/dc/elements/1.1/}"
_CP = "{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}"


class _MemberTooBig(Exception):
    """One part inside an Office file claims to unpack to more than we will read."""


def _zread(zf: zipfile.ZipFile, name: str) -> bytes:
    """Read one part out of an Office file, refusing a decompression bomb.

    `file_size` is what the zip itself says the part unpacks to. Checking it
    first means a 200 KB file that claims to hold four gigabytes is refused in
    microseconds instead of being decompressed into memory to find out.
    """
    info = zf.getinfo(name)
    if info.file_size > MAX_ZIP_MEMBER_BYTES:
        raise _MemberTooBig(f"{name} ({info.file_size // (1024 * 1024)} MB)")
    return zf.read(name)


def _zip_author(zf: zipfile.ZipFile) -> str | None:
    """Whose name is on the file. The closest thing to an author signal we get."""
    try:
        root = ET.fromstring(_zread(zf, "docProps/core.xml"))
    except Exception:
        return None
    for tag in (_DC + "creator", _CP + "lastModifiedBy"):
        node = root.find(tag)
        if node is not None and (node.text or "").strip():
            return node.text.strip()[:120]
    return None


def _xml_text(node, tag: str, joiner: str = "") -> str:
    return joiner.join(t.text or "" for t in node.iter(tag))


def extract_docx(data: bytes) -> tuple[str, str | None, str | None]:
    """(text, reason_if_unreadable, author). One line per paragraph."""
    try:
        with zipfile.ZipFile(_bio(data)) as zf:
            author = _zip_author(zf)
            names = [n for n in ("word/document.xml",) if n in zf.namelist()]
            if not names:
                return "", "the file is a zip but has no Word document inside it", author
            root = ET.fromstring(_zread(zf, "word/document.xml"))
            lines = []
            for para in root.iter(_W + "p"):
                bits = []
                for node in para.iter():
                    tag = node.tag
                    if tag == _W + "t":
                        bits.append(node.text or "")
                    elif tag == _W + "tab":
                        bits.append("\t")
                    elif tag in (_W + "br", _W + "cr"):
                        bits.append("\n")
                lines.append("".join(bits))
            # Headers, footers and footnotes hold real policy text often enough.
            for extra in zf.namelist():
                if re.match(r"word/(header|footer|footnotes|endnotes)\d*\.xml$", extra):
                    try:
                        sub = ET.fromstring(_zread(zf, extra))
                        lines.append(_xml_text(sub, _W + "t", " "))
                    except Exception:
                        pass
            return tidy("\n".join(lines)), None, author
    except _MemberTooBig as big:
        return "", f"too big to read here — one part inside it is {big}", None
    except zipfile.BadZipFile:
        return "", "the file is damaged — Word could not have opened it either", None
    except ET.ParseError:
        return "", "the text inside the file is damaged", None
    except Exception as exc:
        return "", f"could not be read ({type(exc).__name__})", None


def extract_xlsx(data: bytes) -> tuple[str, str | None, str | None]:
    """(text, reason, author). Cell values, one row per line."""
    try:
        with zipfile.ZipFile(_bio(data)) as zf:
            author = _zip_author(zf)
            shared: list[str] = []
            if "xl/sharedStrings.xml" in zf.namelist():
                try:
                    root = ET.fromstring(_zread(zf, "xl/sharedStrings.xml"))
                    for si in root.iter(_S + "si"):
                        shared.append(_xml_text(si, _S + "t"))
                except ET.ParseError:
                    pass
            sheets = sorted(n for n in zf.namelist()
                            if re.match(r"xl/worksheets/sheet\d*\.xml$", n))
            if not sheets:
                return "", "the file is a zip but has no spreadsheet inside it", author
            out = []
            for name in sheets:
                try:
                    root = ET.fromstring(_zread(zf, name))
                except (ET.ParseError, _MemberTooBig):
                    continue
                for row in root.iter(_S + "row"):
                    cells = []
                    for cell in row.iter(_S + "c"):
                        kind = cell.get("t")
                        if kind == "s":
                            v = cell.find(_S + "v")
                            try:
                                cells.append(shared[int(v.text)])
                            except (TypeError, ValueError, IndexError, AttributeError):
                                pass
                        elif kind == "inlineStr":
                            cells.append(_xml_text(cell, _S + "t"))
                        else:
                            v = cell.find(_S + "v")
                            if v is not None and v.text:
                                cells.append(v.text)
                    line = "\t".join(c for c in cells if c)
                    if line.strip():
                        out.append(line)
            return tidy("\n".join(out)), None, author
    except _MemberTooBig as big:
        return "", f"too big to read here — one part inside it is {big}", None
    except zipfile.BadZipFile:
        return "", "the file is damaged — Excel could not have opened it either", None
    except Exception as exc:
        return "", f"could not be read ({type(exc).__name__})", None


def extract_pptx(data: bytes) -> tuple[str, str | None, str | None]:
    """(text, reason, author). One slide per block."""
    try:
        with zipfile.ZipFile(_bio(data)) as zf:
            author = _zip_author(zf)
            slides = sorted(
                (n for n in zf.namelist()
                 if re.match(r"ppt/(slides|notesSlides)/\w+\d*\.xml$", n)),
                key=lambda n: (0 if "/slides/" in n else 1, n))
            if not slides:
                return "", "the file is a zip but has no slides inside it", author
            out = []
            for name in slides:
                try:
                    root = ET.fromstring(_zread(zf, name))
                except (ET.ParseError, _MemberTooBig):
                    continue
                bits = [t.text or "" for t in root.iter(_A + "t")]
                block = "\n".join(b for b in bits if b.strip())
                if block:
                    out.append(block)
            return tidy("\n\n".join(out)), None, author
    except _MemberTooBig as big:
        return "", f"too big to read here — one part inside it is {big}", None
    except zipfile.BadZipFile:
        return "", "the file is damaged — PowerPoint could not have opened it either", None
    except Exception as exc:
        return "", f"could not be read ({type(exc).__name__})", None


def _bio(data: bytes):
    import io
    return io.BytesIO(data)


# ---------------------------------------------------------------- PDF

# There is no PDF library in the standard library, so this is a small one.
#
# Honest limits, stated once here and repeated in the README:
#   * Works on PDFs with a real text layer: Word, Excel, Google Docs, most
#     accounting packages, most quote and invoice generators. That includes the
#     common awkward case where the exporter embeds a cut-down copy of the font
#     and numbers the letters 1, 2, 3 — those files carry a /ToUnicode table
#     that says what each number means, and this reads it.
#   * Does NOT work on scans (there are no letters in the file at all, only a
#     picture), on encrypted files, or on the newer PDFs that pack their page
#     structure into compressed object streams and use a font with no
#     /ToUnicode table. Those come back as `unreadable` with the reason said out
#     loud — which is itself the finding.

_PDF_DELIM = b" \t\r\n\x00\f()<>[]{}/%"
_OBJ_HEAD = re.compile(rb"(?<![0-9])(\d{1,8})\s+(\d{1,5})\s+obj\b")
_REF = rb"(\d{1,8})\s+\d{1,5}\s+R"


def _pdf_string(raw: bytes) -> str:
    """Bytes from a PDF string when we have no font map to go on."""
    if raw[:2] == b"\xfe\xff":
        return raw[2:].decode("utf-16-be", errors="replace")
    return raw.decode("latin-1", errors="replace")


def _read_literal(buf: bytes, i: int) -> tuple[bytes, int]:
    """Read a (parenthesised) PDF string starting at buf[i] == '('. Returns raw bytes."""
    i += 1
    depth = 1
    out = bytearray()
    n = len(buf)
    escapes = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12}
    while i < n:
        c = buf[i]
        if c == 0x5C:                                   # backslash
            i += 1
            if i >= n:
                break
            nxt = buf[i]
            if nxt in escapes:
                out.append(escapes[nxt]); i += 1
            elif 0x30 <= nxt <= 0x37:                   # octal
                digits = ""
                while i < n and len(digits) < 3 and 0x30 <= buf[i] <= 0x37:
                    digits += chr(buf[i]); i += 1
                out.append(int(digits, 8) & 0xFF)
            elif nxt == 0x0A:
                i += 1
            elif nxt == 0x0D:
                i += 1
                if i < n and buf[i] == 0x0A:
                    i += 1
            else:
                out.append(nxt); i += 1
            continue
        if c == 0x28:
            depth += 1; out.append(c); i += 1; continue
        if c == 0x29:
            depth -= 1; i += 1
            if depth == 0:
                break
            out.append(c); continue
        out.append(c); i += 1
    return bytes(out), i


def _decode_string(raw: bytes, font) -> str:
    """One PDF string to text, using the current font's /ToUnicode table if it has one."""
    if not font:
        return _pdf_string(raw)
    cmap, width = font
    if not cmap:
        return _pdf_string(raw)
    out = []
    if width == 2:
        for i in range(0, len(raw) - 1, 2):
            code = (raw[i] << 8) | raw[i + 1]
            out.append(cmap.get(code, ""))
    else:
        for b in raw:
            out.append(cmap.get(b, ""))
    return "".join(out)


def _pdf_content_text(buf: bytes, fonts: dict | None = None) -> str:
    """Pull the show-text operators out of one decoded PDF content stream.

    `fonts` maps a resource name ("F1") to (code -> character, bytes per code).
    The scanner follows the `Tf` operator so each string is decoded with the
    font that was actually selected for it.
    """
    fonts = fonts or {}
    out: list[str] = []
    pending: list[bytes] = []
    current = None
    last_name = None
    array_depth = 0
    nums: list[float] = []
    last_y: float | None = None
    i, n = 0, len(buf)

    def flush():
        if pending:
            out.append("".join(_decode_string(p, current) if isinstance(p, bytes) else p
                               for p in pending))
            pending.clear()

    while i < n:
        c = buf[i:i + 1]
        if c == b"%":
            end = buf.find(b"\n", i)
            i = n if end < 0 else end + 1
            continue
        if c == b"(":
            s, i = _read_literal(buf, i)
            pending.append(s)
            continue
        if c == b"/":
            j = i + 1
            while j < n and buf[j:j + 1] not in _PDF_DELIM:
                j += 1
            last_name = buf[i + 1:j].decode("latin-1", "replace")
            i = j
            continue
        if c == b"<":
            if buf[i + 1:i + 2] == b"<":
                i += 2
                continue
            end = buf.find(b">", i)
            if end < 0:
                break
            hexs = re.sub(rb"[^0-9A-Fa-f]", b"", buf[i + 1:end])
            if len(hexs) % 2:
                hexs += b"0"
            try:
                pending.append(bytes.fromhex(hexs.decode("ascii")))
            except ValueError:
                pass
            i = end + 1
            continue
        if c == b"[":
            array_depth += 1; i += 1; continue
        if c == b"]":
            array_depth = max(0, array_depth - 1); i += 1; continue
        if c in b" \t\r\n\x00\f":
            i += 1; continue

        j = i
        while j < n and buf[j:j + 1] not in _PDF_DELIM:
            j += 1
        token = buf[i:j]
        i = j if j > i else i + 1
        if not token:
            continue
        if token == b"Tf":
            current = fonts.get(last_name)
            nums.clear()
        elif token in (b"Tj", b"TJ", b"'", b'"'):
            flush()
            if token in (b"'", b'"'):
                out.append("\n")
            nums.clear()
        elif token in (b"Td", b"TD"):
            # "tx ty Td" moves the cursor. Only a vertical move is a new line —
            # treating every move as one turns a word set letter by letter into
            # one letter per line, which is unsearchable.
            flush()
            if len(nums) >= 2 and abs(nums[-1]) > 0.1:
                out.append("\n")
            nums.clear()
        elif token == b"Tm":
            flush()
            if len(nums) >= 6:
                y = nums[-1]
                if last_y is not None and abs(y - last_y) > 0.1:
                    out.append("\n")
                last_y = y
            nums.clear()
        elif token in (b"T*", b"ET", b"BT"):
            flush()
            out.append("\n")
            nums.clear()
        else:
            try:
                value = float(token)
            except ValueError:
                nums.clear()
                continue
            nums.append(value)
            # Inside a TJ array a big negative kern is how a PDF writes a space.
            # 200 thousandths of an em is the usual cut-off; smaller gaps are
            # letter-spacing in a heading, not word breaks.
            if array_depth and pending and value <= -200:
                pending.append(" ")
    flush()
    return "".join(out)


def _pdf_streams(data: bytes):
    pos = 0
    while True:
        start = data.find(b"stream", pos)
        if start < 0:
            return
        j = start + 6
        if data[j:j + 2] == b"\r\n":
            j += 2
        elif data[j:j + 1] in (b"\n", b"\r"):
            j += 1
        end = data.find(b"endstream", j)
        if end < 0:
            return
        yield data[j:end]
        pos = end + 9


def _inflate(raw: bytes) -> bytes | None:
    for wbits in (15, -15):
        try:
            return zlib.decompressobj(wbits).decompress(raw)
        except zlib.error:
            continue
    # Uncompressed content streams are legal and common in small PDFs.
    if b"Tj" in raw or b"TJ" in raw or b"BT" in raw:
        return raw
    return None


# ---- the little bit of PDF structure we need -------------------------------

def _pdf_objects(data: bytes) -> dict[int, bytes]:
    """{object number: the bytes between 'N 0 obj' and 'endobj'}."""
    objects: dict[int, bytes] = {}
    for m in _OBJ_HEAD.finditer(data):
        end = data.find(b"endobj", m.end())
        if end < 0:
            end = min(len(data), m.end() + 200_000)
        objects[int(m.group(1))] = data[m.end():end]
    return objects


def _object_stream(body: bytes) -> bytes | None:
    """The decoded stream inside one object body, if it has one."""
    start = body.find(b"stream")
    if start < 0:
        return None
    header = body[:start]
    j = start + 6
    if body[j:j + 2] == b"\r\n":
        j += 2
    elif body[j:j + 1] in (b"\n", b"\r"):
        j += 1
    end = body.find(b"endstream", j)
    raw = body[j:end if end > 0 else len(body)]
    if b"/FlateDecode" in header:
        return _inflate(raw)
    return raw


def _parse_tounicode(cmap: bytes) -> tuple[dict, int]:
    """A /ToUnicode CMap to {code: text}, plus how many bytes a code takes."""
    mapping: dict[int, str] = {}
    width = 1
    space = re.search(rb"begincodespacerange(.*?)endcodespacerange", cmap, re.S)
    if space:
        pair = re.findall(rb"<([0-9A-Fa-f]+)>", space.group(1))
        if pair:
            width = max(1, min(2, len(pair[0]) // 2))

    def text_of(hexs: bytes) -> str:
        try:
            raw = bytes.fromhex(hexs.decode("ascii"))
        except ValueError:
            return ""
        if len(raw) % 2:
            raw += b"\x00"
        return raw.decode("utf-16-be", errors="replace").replace("\x00", "")

    for block in re.findall(rb"beginbfchar(.*?)endbfchar", cmap, re.S):
        for src, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            mapping[int(src, 16)] = text_of(dst)
            width = max(width, min(2, len(src) // 2))

    for block in re.findall(rb"beginbfrange(.*?)endbfrange", cmap, re.S):
        # <lo> <hi> <dst>   — consecutive codes map to consecutive characters
        for lo, hi, dst in re.findall(
                rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            start, end = int(lo, 16), int(hi, 16)
            if end - start > 65535:
                continue
            base = text_of(dst)
            width = max(width, min(2, len(lo) // 2))
            if len(base) == 1:
                for k in range(end - start + 1):
                    mapping[start + k] = chr(ord(base) + k)
            else:
                mapping[start] = base
        # <lo> <hi> [<a> <b> <c>] — an explicit list
        for lo, hi, arr in re.findall(
                rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\[(.*?)\]", block, re.S):
            start = int(lo, 16)
            width = max(width, min(2, len(lo) // 2))
            for k, dst in enumerate(re.findall(rb"<([0-9A-Fa-f]*)>", arr)):
                mapping[start + k] = text_of(dst)
    return mapping, width


def _font_table(objects: dict[int, bytes], font_dict: bytes) -> dict:
    """A page's /Font resource dict to {resource name: (cmap, bytes per code)}."""
    table = {}
    for name, num in re.findall(rb"/([^\s/<>\[\]()]+)\s+" + _REF, font_dict):
        body = objects.get(int(num))
        if body is None:
            continue
        ref = re.search(rb"/ToUnicode\s+" + _REF, body)
        if not ref:
            table[name.decode("latin-1", "replace")] = None
            continue
        cmap_body = objects.get(int(ref.group(1)))
        stream = _object_stream(cmap_body) if cmap_body else None
        if not stream:
            table[name.decode("latin-1", "replace")] = None
            continue
        try:
            table[name.decode("latin-1", "replace")] = _parse_tounicode(stream)
        except Exception:
            table[name.decode("latin-1", "replace")] = None
    return table


def _resolve(objects: dict[int, bytes], body: bytes, key: bytes) -> bytes | None:
    """Get /key out of an object, following one indirect reference if needed."""
    ref = re.search(b"/" + key + rb"\s+" + _REF, body)
    if ref:
        return objects.get(int(ref.group(1)))
    m = re.search(b"/" + key + rb"\s*<<", body)
    if m:
        # crude but adequate brace matching for a nested dictionary
        depth, i = 1, m.end()
        while i < len(body) - 1 and depth:
            pair = body[i:i + 2]
            if pair == b"<<":
                depth += 1; i += 2; continue
            if pair == b">>":
                depth -= 1; i += 2; continue
            i += 1
        return body[m.end():i]
    return None


def _pages_text(data: bytes) -> str:
    """Walk the page objects so each string is decoded with its own font."""
    objects = _pdf_objects(data)
    if not objects:
        return ""
    pages = [(num, body) for num, body in sorted(objects.items())
             if re.search(rb"/Type\s*/Page\b", body)]
    out = []
    for num, body in pages:
        resources = _resolve(objects, body, b"Resources") or body
        font_dict = _resolve(objects, resources, b"Font") or b""
        fonts = _font_table(objects, font_dict) if font_dict else {}

        contents = []
        ref = re.search(rb"/Contents\s+" + _REF, body)
        if ref:
            contents = [int(ref.group(1))]
        else:
            arr = re.search(rb"/Contents\s*\[(.*?)\]", body, re.S)
            if arr:
                contents = [int(x) for x in re.findall(_REF, arr.group(1))]
        chunk = []
        for cnum in contents:
            cbody = objects.get(cnum)
            stream = _object_stream(cbody) if cbody else None
            if stream:
                chunk.append(stream)
        if not chunk:
            continue
        try:
            out.append(_pdf_content_text(b"\n".join(chunk), fonts))
        except Exception:
            continue
    return "\n".join(out)


def _all_streams_text(data: bytes) -> str:
    """Fallback for PDFs whose page structure we could not read.

    Every /ToUnicode table in the file is merged into one. That is not strictly
    correct when two cut-down fonts number their letters the same way, but it
    turns unreadable control characters into mostly-right words, and mostly-right
    words are searchable.
    """
    merged: dict[int, str] = {}
    width = 1
    for raw in _pdf_streams(data):
        buf = _inflate(raw)
        if not buf or b"beginbfchar" not in buf and b"beginbfrange" not in buf:
            continue
        try:
            table, w = _parse_tounicode(buf)
        except Exception:
            continue
        width = max(width, w)
        for code, ch in table.items():
            merged.setdefault(code, ch)
    fonts = {"*": (merged, width)} if merged else {}

    chunks = []
    for raw in _pdf_streams(data):
        buf = _inflate(raw)
        if not buf or (b"Tj" not in buf and b"TJ" not in buf):
            continue
        try:
            chunks.append(_pdf_content_text(buf, fonts))
        except Exception:
            continue
    if not chunks:
        return ""
    text = "\n".join(chunks)
    if merged and not _has_real_words(text):
        # The merged map made it worse; try again with no map at all.
        plain = []
        for raw in _pdf_streams(data):
            buf = _inflate(raw)
            if buf and (b"Tj" in buf or b"TJ" in buf):
                try:
                    plain.append(_pdf_content_text(buf, {}))
                except Exception:
                    pass
        if plain and _has_real_words("\n".join(plain)):
            return "\n".join(plain)
    return text


def _looks_complete(data: bytes) -> bool:
    """Does this PDF have an ending on it?

    Every PDF writer finishes the file with `%%EOF`, and most write a `trailer`
    or an `/XRef` stream before it. A file with neither near the end was cut
    short — a failed download, a full disk, a copy that was interrupted.
    """
    tail = data[-2048:]
    if b"%%EOF" in tail:
        return True
    return b"trailer" in data[-8192:] or b"startxref" in data[-8192:]


def extract_pdf(data: bytes) -> tuple[str, str | None]:
    """(text, reason_if_unreadable). Never raises."""
    if not data[:1024].lstrip().startswith(b"%PDF"):
        return "", "this does not look like a PDF inside (wrong file header)"
    if re.search(rb"/Encrypt[\s/<]", data):
        return "", "the PDF is locked with a password"

    text = ""
    try:
        text = tidy(_pages_text(data))
    except Exception:
        text = ""
    if not _has_real_words(text):
        try:
            fallback = tidy(_all_streams_text(data))
        except Exception as exc:
            return "", f"could not be read ({type(exc).__name__})"
        if len(fallback.split()) > len(text.split()):
            text = fallback

    if not text:
        # A PDF whose page objects are all there but hold no letters is a scan.
        # A PDF that stops halfway through is a damaged file, and telling an
        # owner it is "a scan" sends them hunting for a piece of paper that was
        # never involved.
        if not _looks_complete(data):
            return "", ("the file is cut short — it stops part-way through, so most "
                        "PDF readers will refuse it too")
        return "", "no text inside — this is a scan or a picture of a document"
    if not _has_real_words(text):
        return "", ("the text could not be decoded — the PDF uses a font with no "
                    "readable character map, or it is a scan")
    return text, None

# ---------------------------------------------------------------- one file

def extract_bytes(data: bytes, ext: str) -> tuple[str, str, str | None, str | None]:
    """(status, text, reason, author) for a blob of bytes with a known extension."""
    ext = ext.lower()
    if ext in TEXT_EXTS:
        return "ok", tidy(decode_bytes(data)), None, None
    if ext in MARKUP_EXTS:
        return "ok", strip_markup(decode_bytes(data)), None, None
    if ext in (".docx", ".docm"):
        text, reason, author = extract_docx(data)
    elif ext in (".xlsx", ".xlsm"):
        text, reason, author = extract_xlsx(data)
    elif ext in (".pptx", ".pptm"):
        text, reason, author = extract_pptx(data)
    elif ext in PDF_EXTS:
        text, reason = extract_pdf(data)
        author = None
    elif ext in LEGACY_OFFICE:
        return "unreadable", "", LEGACY_OFFICE[ext], None
    else:
        return "skipped", "", f"{ext or 'no extension'} files are not read", None
    if reason:
        return "unreadable", "", reason, author
    return "ok", text, None, author


def extract_file(path: Path, root: Path) -> dict:
    """Turn one file on disk into one record. Opens read-only. Never raises."""
    path = Path(path)
    try:
        rel = str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        rel = path.name
    folder = rel.rsplit("/", 1)[0] if "/" in rel else ""
    ext = path.suffix.lower()
    rec = {
        "rel": rel, "path": str(path), "name": path.name, "folder": folder,
        "ext": ext, "size": 0, "mtime": 0, "status": "skipped",
        "reason": None, "text": "", "nwords": 0, "chash": None, "author": None,
    }
    try:
        st = path.stat()
        rec["size"] = int(st.st_size)
        rec["mtime"] = int(st.st_mtime)
    except OSError as exc:
        rec["status"] = "unreadable"
        rec["reason"] = f"the file could not be opened ({exc.strerror or type(exc).__name__})"
        return rec

    if ext not in READABLE_EXTS and ext not in LEGACY_OFFICE:
        rec["reason"] = f"{ext or 'no extension'} files are not read"
        return rec
    if rec["size"] == 0:
        rec["status"] = "unreadable"
        rec["reason"] = "the file is empty (zero bytes)"
        return rec
    if rec["size"] > MAX_FILE_BYTES:
        rec["status"] = "unreadable"
        rec["reason"] = (f"too big to read here "
                         f"({rec['size'] // (1024 * 1024)} MB, the limit is "
                         f"{MAX_FILE_BYTES // (1024 * 1024)} MB)")
        return rec

    try:
        with open(path, "rb") as fh:
            data = fh.read(MAX_FILE_BYTES)
    except PermissionError:
        rec["status"] = "unreadable"
        rec["reason"] = "locked or you do not have permission to open it"
        return rec
    except OSError as exc:
        rec["status"] = "unreadable"
        rec["reason"] = f"the file could not be opened ({exc.strerror or type(exc).__name__})"
        return rec
    except Exception as exc:                                    # pragma: no cover
        rec["status"] = "unreadable"
        rec["reason"] = f"could not be read ({type(exc).__name__})"
        return rec

    try:
        status, text, reason, author = extract_bytes(data, ext)
    except Exception as exc:                                    # pragma: no cover
        status, text, reason, author = "unreadable", "", f"could not be read ({type(exc).__name__})", None

    rec["status"], rec["text"], rec["reason"], rec["author"] = status, text, reason, author
    if status == "ok":
        rec["nwords"] = len(text.split())
        rec["chash"] = content_hash(text)
        if rec["nwords"] == 0:
            rec["status"] = "unreadable"
            rec["reason"] = "the file opened but there were no words in it"
            rec["chash"] = None
    return rec


# ---------------------------------------------------------------- the walk

def _ignored_file(name: str) -> bool:
    low = name.lower()
    if low in IGNORE_FILES:
        return True
    return any(p.search(name) for p in IGNORE_PATTERNS)


def walk_files(root, ignore_dirs=None, cancel=None) -> tuple[list[Path], list[dict]]:
    """Every candidate file under root, plus a list of folders we could not enter.

    Symlinks are not followed, so a loop cannot happen; we also remember which
    real directories we have visited in case of Windows junctions.
    """
    root = Path(root)
    ignore = {d.lower() for d in (ignore_dirs if ignore_dirs is not None else IGNORE_DIRS)}
    files: list[Path] = []
    problems: list[dict] = []
    seen_dirs: set = set()

    def on_error(exc: OSError):
        problems.append({
            "path": getattr(exc, "filename", "") or str(root),
            "reason": f"folder could not be opened ({exc.strerror or type(exc).__name__})",
        })

    for dirpath, dirnames, filenames in os.walk(root, onerror=on_error, followlinks=False):
        if cancel is not None and cancel():
            break
        try:
            key = os.path.realpath(dirpath)
        except OSError:
            key = dirpath
        if key in seen_dirs:
            dirnames[:] = []
            continue
        seen_dirs.add(key)
        dirnames[:] = sorted(d for d in dirnames
                             if d.lower() not in ignore and not d.startswith("~$"))
        for name in sorted(filenames):
            if _ignored_file(name):
                continue
            files.append(Path(dirpath) / name)
    return files, problems


def ingest_folder(root, *, known=None, on_progress=None, cancel=None,
                  on_batch=None, batch_size=150) -> dict:
    """Walk `root` and extract every file. Returns a summary.

    `known`   — {rel: (size, mtime)} from the last run; matching files are
                reported as unchanged and never reopened. This is what makes a
                second scan of a big folder take seconds.
    `on_batch(list_of_records)` — called as records accumulate so the caller can
                write them to the index without holding a whole client's
                document set in memory.
    `cancel()` — return True to stop cleanly at the next file boundary.
    """
    root = Path(root)
    known = known or {}
    if not root.exists():
        raise FileNotFoundError(str(root))
    if not root.is_dir():
        raise NotADirectoryError(str(root))

    def note(phase, done, total, current=""):
        if on_progress:
            on_progress({"phase": phase, "done": done, "total": total, "current": current})

    note("Looking for files", 0, 0)
    files, problems = walk_files(root, cancel=cancel)
    total = len(files)
    note("Reading files", 0, total)

    seen_rels: list[str] = []
    batch: list[dict] = []
    counts = {"ok": 0, "unreadable": 0, "skipped": 0, "unchanged": 0}
    cancelled = False

    for n, path in enumerate(files, 1):
        if cancel is not None and cancel():
            cancelled = True
            break
        try:
            rel = str(path.relative_to(root)).replace("\\", "/")
        except ValueError:
            rel = path.name
        seen_rels.append(rel)

        prior = known.get(rel)
        if prior is not None:
            try:
                st = path.stat()
                if int(st.st_size) == int(prior[0]) and int(st.st_mtime) == int(prior[1]):
                    counts["unchanged"] += 1
                    if n % 25 == 0 or n == total:
                        note("Reading files", n, total, rel)
                    continue
            except OSError:
                pass

        try:
            rec = extract_file(path, root)
        except Exception as exc:
            # extract_file is written not to raise, but "never crash, never
            # silently drop" has to be structural rather than a promise. One
            # file nobody anticipated must not lose the other nineteen thousand.
            rec = {"rel": rel, "path": str(path), "name": path.name,
                   "folder": rel.rsplit("/", 1)[0] if "/" in rel else "",
                   "ext": path.suffix.lower(), "size": 0, "mtime": 0,
                   "status": "unreadable", "text": "", "nwords": 0,
                   "chash": None, "author": None,
                   "reason": f"could not be read ({type(exc).__name__})"}
        counts[rec["status"]] = counts.get(rec["status"], 0) + 1
        batch.append(rec)
        if on_batch and len(batch) >= batch_size:
            on_batch(batch); batch = []
        if n % 5 == 0 or n == total:
            note("Reading files", n, total, rel)

    if batch and on_batch:
        on_batch(batch)
    if cancel is not None and cancel():
        cancelled = True

    return {
        "root": str(root),
        "total": total,
        "counts": counts,
        "seen": seen_rels,
        "problems": problems,
        "cancelled": cancelled,
        "records": batch if not on_batch else [],
    }
