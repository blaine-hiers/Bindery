"""Ingest tests — extraction per format, the encoding chain, and never crashing.

    py tests/test_ingest.py
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

# Tests live in `system/apps/tests/`, mirroring the app tree. Walk up to the
# apps root, then reflect the path back down to the app this file tests.
_TESTS = Path(__file__).resolve().parent
_ROOT = next(p for p in _TESTS.parents if (p / "_shared" / "appkit.py").is_file())
_APP = _ROOT / _TESTS.relative_to(_ROOT / "tests")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_APP))
sys.path.insert(0, str(HERE))

import ingest                                                    # noqa: E402
import fixtures                                                  # noqa: E402


class TestDecoding(unittest.TestCase):
    def test_utf8_wins(self):
        self.assertEqual(ingest.decode_bytes("Café Reyes".encode("utf-8")), "Café Reyes")

    def test_bom_is_stripped(self):
        self.assertEqual(ingest.decode_bytes("Price".encode("utf-8-sig")), "Price")

    def test_cp1252_smart_quotes_survive(self):
        # Bytes 0x92/0x93/0x94 are Word's curly quotes. They are not valid utf-8,
        # so the chain has to fall through to cp1252 to get them right.
        raw = b"The owner\x92s copy \x93final\x94"
        out = ingest.decode_bytes(raw)
        self.assertIn("owner’s", out)
        self.assertIn("“final”", out)

    def test_raw_bytes_never_throw(self):
        blob = bytes(range(256)) * 8
        out = ingest.decode_bytes(blob)
        self.assertIsInstance(out, str)
        self.assertTrue(len(out) > 0)

    def test_latin1_fallback_is_last(self):
        # 0x81 is undefined in cp1252, so this must fall through to latin-1.
        self.assertIsInstance(ingest.decode_bytes(b"\x81\x8d\x8f\x90\x9d"), str)


class TestMarkup(unittest.TestCase):
    def test_tags_are_stripped_and_entities_decoded(self):
        html = ("<html><head><style>p{color:red}</style></head><body>"
                "<h1>Safety&nbsp;Manual</h1><p>Wear <b>PPE</b> at all times.</p>"
                "<script>alert(1)</script></body></html>")
        out = ingest.strip_markup(html)
        self.assertIn("Safety", out)
        self.assertIn("Wear PPE at all times", out.replace("  ", " "))
        self.assertNotIn("alert", out)
        self.assertNotIn("color:red", out)

    def test_block_ends_become_line_breaks(self):
        out = ingest.strip_markup("<p>one</p><p>two</p>")
        self.assertEqual([l.strip() for l in out.splitlines() if l.strip()], ["one", "two"])


class TestOfficeFormats(unittest.TestCase):
    def test_docx_paragraphs_and_author(self):
        data = fixtures.make_docx(
            ["Warranty terms", "Parts are covered for twelve months.", "Labor is ninety days."],
            author="Dana Reyes")
        status, text, reason, author = ingest.extract_bytes(data, ".docx")
        self.assertEqual(status, "ok")
        self.assertIsNone(reason)
        self.assertIn("Parts are covered for twelve months.", text)
        self.assertIn("Labor is ninety days.", text)
        self.assertEqual(author, "Dana Reyes")
        self.assertEqual(len(text.splitlines()), 3)

    def test_xlsx_cells_come_out(self):
        data = fixtures.make_xlsx([["Item", "Price"],
                                   ["Condenser fan motor", "285.00"],
                                   ["Capacitor", "42.50"]])
        status, text, reason, author = ingest.extract_bytes(data, ".xlsx")
        self.assertEqual(status, "ok")
        self.assertIn("Condenser fan motor", text)
        self.assertIn("42.50", text)
        self.assertEqual(author, "Bookkeeper")

    def test_pptx_slides_come_out(self):
        data = fixtures.make_pptx([["Quarterly review", "Revenue up"],
                                   ["Next steps", "Hire a dispatcher"]])
        status, text, reason, _ = ingest.extract_bytes(data, ".pptx")
        self.assertEqual(status, "ok")
        self.assertIn("Quarterly review", text)
        self.assertIn("Hire a dispatcher", text)

    def test_damaged_office_file_is_reported_not_raised(self):
        status, text, reason, _ = ingest.extract_bytes(b"PK\x03\x04 this is not a real zip", ".docx")
        self.assertEqual(status, "unreadable")
        self.assertEqual(text, "")
        self.assertIn("damaged", reason)

    def test_zip_without_a_document_inside(self):
        import io
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("something/else.txt", "hello")
        status, _, reason, _ = ingest.extract_bytes(buf.getvalue(), ".docx")
        self.assertEqual(status, "unreadable")
        self.assertIn("no Word document", reason)

    def test_legacy_doc_gets_a_useful_reason(self):
        status, _, reason, _ = ingest.extract_bytes(b"\xd0\xcf\x11\xe0anything", ".doc")
        self.assertEqual(status, "unreadable")
        self.assertIn(".docx", reason)


class TestPdf(unittest.TestCase):
    LINES = ["Northgate Mechanical Services Incorporated",
             "Standard labor rate is ninety five dollars per hour.",
             "Emergency after hours calls are billed at time and a half."]

    def test_plain_pdf_extracts(self):
        text, reason = ingest.extract_pdf(fixtures.make_pdf(self.LINES))
        self.assertIsNone(reason)
        self.assertIn("Standard labor rate", text)
        self.assertIn("Emergency after hours", text)

    def test_flate_compressed_pdf_extracts(self):
        text, reason = ingest.extract_pdf(fixtures.make_pdf(self.LINES, compress=True))
        self.assertIsNone(reason)
        self.assertIn("ninety five dollars", text)

    def test_scanned_pdf_is_unreadable_with_a_reason(self):
        text, reason = ingest.extract_pdf(fixtures.make_scanned_pdf())
        self.assertEqual(text, "")
        self.assertIsNotNone(reason)
        self.assertIn("scan", reason.lower())

    def test_encrypted_pdf_says_so(self):
        text, reason = ingest.extract_pdf(fixtures.make_encrypted_pdf())
        self.assertEqual(text, "")
        self.assertIn("password", reason)

    def test_garbage_never_raises(self):
        for blob in (b"", b"not a pdf at all", os.urandom(4096),
                     b"%PDF-1.4\n" + os.urandom(9000)):
            text, reason = ingest.extract_pdf(blob)
            self.assertIsInstance(text, str)
            if not text:
                self.assertIsInstance(reason, str)
                self.assertTrue(reason)

    def test_a_cut_down_font_is_decoded_through_its_tounicode_table(self):
        words = ["Condenser", "fan", "motor", "replacement", "quoted", "at",
                 "two", "hundred", "eighty", "five", "dollars"]
        text, reason = ingest.extract_pdf(fixtures.make_subset_font_pdf(words))
        self.assertIsNone(reason)
        for word in words:
            self.assertIn(word, text)

    def test_letter_spacing_does_not_split_words_into_letters(self):
        text, _ = ingest.extract_pdf(fixtures.make_subset_font_pdf(["PREPARED", "FOR", "NORTHGATE"]))
        self.assertIn("PREPARED FOR NORTHGATE", text)
        self.assertNotIn("P R E P A R E D", text)

    def test_one_move_per_word_gives_one_word_per_line_not_one_letter(self):
        text, reason = ingest.extract_pdf(
            fixtures.make_subset_font_pdf(["Warranty", "terms", "attached"], per_line=True))
        self.assertIsNone(reason)
        self.assertEqual([l for l in text.splitlines() if l.strip()],
                         ["Warranty", "terms", "attached"])

    def test_tounicode_ranges_and_char_lists_are_both_understood(self):
        cmap = (b"1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
                b"1 beginbfchar\n<0001> <0041>\nendbfchar\n"
                b"2 beginbfrange\n<0002> <0004> <0042>\n<0010> <0011> [<0058> <0059>]\n"
                b"endbfrange\n")
        mapping, width = ingest._parse_tounicode(cmap)
        self.assertEqual(width, 2)
        self.assertEqual(mapping[0x01], "A")
        self.assertEqual([mapping[c] for c in (2, 3, 4)], ["B", "C", "D"])
        self.assertEqual(mapping[0x10], "X")
        self.assertEqual(mapping[0x11], "Y")

    def test_a_broken_tounicode_table_does_not_raise(self):
        mapping, width = ingest._parse_tounicode(b"beginbfchar <ZZ> <QQ> endbfchar")
        self.assertIsInstance(mapping, dict)
        self.assertIn(width, (1, 2))

    def test_escaped_parentheses_in_a_pdf_string(self):
        text, reason = ingest.extract_pdf(
            fixtures.make_pdf(["Change order (approved) for the Johnson job site work"]))
        self.assertIsNone(reason)
        self.assertIn("(approved)", text)


class TestWalking(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "SOPs").mkdir()
        (self.root / "node_modules" / "junk").mkdir(parents=True)
        (self.root / ".git").mkdir()
        (self.root / "SOPs" / "safety.md").write_text("# Safety\nWear PPE.", encoding="utf-8")
        (self.root / "price list.txt").write_text("Condenser 285.00", encoding="utf-8")
        (self.root / "~$price list.xlsx").write_bytes(b"lock file")
        (self.root / "Thumbs.db").write_bytes(b"junk")
        (self.root / "node_modules" / "junk" / "a.js").write_text("x", encoding="utf-8")
        (self.root / ".git" / "config").write_text("x", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_ignore_list_is_honoured(self):
        files, problems = ingest.walk_files(self.root)
        names = sorted(f.name for f in files)
        self.assertEqual(names, ["price list.txt", "safety.md"])
        self.assertEqual(problems, [])

    def test_emoji_and_spaces_in_filenames(self):
        (self.root / "quote 🚚 final.txt").write_text("Delivery quote for the crane",
                                                      encoding="utf-8")
        files, _ = ingest.walk_files(self.root)
        self.assertTrue(any("🚚" in f.name for f in files))
        rec = ingest.extract_file(self.root / "quote 🚚 final.txt", self.root)
        self.assertEqual(rec["status"], "ok")
        self.assertIn("crane", rec["text"])

    def test_unknown_extension_is_skipped_but_counted(self):
        (self.root / "photo.jpg").write_bytes(b"\xff\xd8\xff\xe0 not really a jpeg")
        rec = ingest.extract_file(self.root / "photo.jpg", self.root)
        self.assertEqual(rec["status"], "skipped")
        self.assertIn(".jpg", rec["reason"])
        self.assertGreater(rec["size"], 0)

    def test_empty_file_is_reported(self):
        (self.root / "blank.txt").write_bytes(b"")
        rec = ingest.extract_file(self.root / "blank.txt", self.root)
        self.assertEqual(rec["status"], "unreadable")
        self.assertIn("empty", rec["reason"])

    def test_oversize_file_is_reported_not_read(self):
        big = self.root / "huge.txt"
        big.write_text("x" * 5000, encoding="utf-8")
        old = ingest.MAX_FILE_BYTES
        ingest.MAX_FILE_BYTES = 1000
        try:
            rec = ingest.extract_file(big, self.root)
        finally:
            ingest.MAX_FILE_BYTES = old
        self.assertEqual(rec["status"], "unreadable")
        self.assertIn("too big", rec["reason"])

    def test_missing_file_does_not_raise(self):
        rec = ingest.extract_file(self.root / "nope.txt", self.root)
        self.assertEqual(rec["status"], "unreadable")
        self.assertTrue(rec["reason"])

    def test_relative_paths_use_forward_slashes(self):
        rec = ingest.extract_file(self.root / "SOPs" / "safety.md", self.root)
        self.assertEqual(rec["rel"], "SOPs/safety.md")
        self.assertEqual(rec["folder"], "SOPs")
        self.assertEqual(rec["ext"], ".md")


class TestIngestFolder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "a.txt").write_text("Alpha document about pricing", encoding="utf-8")
        (self.root / "b.md").write_text("Beta document about safety", encoding="utf-8")
        (self.root / "c.docx").write_bytes(fixtures.make_docx(["Gamma warranty terms"]))

    def tearDown(self):
        self.tmp.cleanup()

    def test_all_files_are_read(self):
        summary = ingest.ingest_folder(self.root)
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["counts"]["ok"], 3)
        self.assertEqual(len(summary["records"]), 3)
        self.assertFalse(summary["cancelled"])

    def test_re_ingest_skips_unchanged_files(self):
        first = ingest.ingest_folder(self.root)
        known = {r["rel"]: (r["size"], r["mtime"]) for r in first["records"]}
        second = ingest.ingest_folder(self.root, known=known)
        self.assertEqual(second["counts"]["unchanged"], 3)
        self.assertEqual(second["counts"]["ok"], 0)
        self.assertEqual(len(second["records"]), 0)
        self.assertEqual(len(second["seen"]), 3)

    def test_a_changed_file_is_re_read(self):
        first = ingest.ingest_folder(self.root)
        known = {r["rel"]: (r["size"], r["mtime"]) for r in first["records"]}
        time.sleep(1.05)                       # mtime on Windows has 1s granularity
        (self.root / "a.txt").write_text("Alpha document about pricing and rates",
                                         encoding="utf-8")
        second = ingest.ingest_folder(self.root, known=known)
        self.assertEqual(second["counts"]["unchanged"], 2)
        self.assertEqual(second["counts"]["ok"], 1)
        self.assertEqual(second["records"][0]["rel"], "a.txt")

    def test_progress_is_reported(self):
        seen = []
        ingest.ingest_folder(self.root, on_progress=lambda p: seen.append(p))
        self.assertTrue(seen)
        self.assertEqual(seen[-1]["done"], 3)
        self.assertEqual(seen[-1]["total"], 3)

    def test_cancel_stops_early(self):
        summary = ingest.ingest_folder(self.root, cancel=lambda: True)
        self.assertTrue(summary["cancelled"])
        self.assertEqual(summary["counts"]["ok"], 0)

    def test_batches_are_handed_over(self):
        got = []
        summary = ingest.ingest_folder(self.root, on_batch=got.extend, batch_size=1)
        self.assertEqual(len(got), 3)
        self.assertEqual(summary["records"], [])

    def test_missing_folder_raises_a_clear_error(self):
        with self.assertRaises(FileNotFoundError):
            ingest.ingest_folder(self.root / "not-here")

    def test_a_file_instead_of_a_folder_raises(self):
        with self.assertRaises(NotADirectoryError):
            ingest.ingest_folder(self.root / "a.txt")

    def test_content_hash_ignores_case_and_spacing(self):
        a = ingest.content_hash("Price List\n\nCondenser   285.00")
        b = ingest.content_hash("price list condenser 285.00")
        self.assertEqual(a, b)
        self.assertNotEqual(a, ingest.content_hash("price list condenser 385.00"))


# ---------------------------------------------------------------- adversarial

class TestRealWorldGarbage(unittest.TestCase):
    """The brief: every one of these must produce a reported record, not an
    exception and not a silent drop."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)

    def tearDown(self):
        try:
            self.tmp.cleanup()
        except OSError:
            pass

    def rec(self, name, data):
        p = self.root / name
        p.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        return ingest.extract_file(p, self.root)

    def test_a_docx_that_is_not_a_zip(self):
        r = self.rec("notazip.docx", b"this is plainly not a zip file")
        self.assertEqual(r["status"], "unreadable")
        self.assertIn("damaged", r["reason"])

    def test_a_zip_bomb_is_refused_before_it_is_decompressed(self):
        import io
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("word/document.xml", b"\0" * (120 * 1024 * 1024))
        r = self.rec("bomb.docx", buf.getvalue())
        self.assertEqual(r["status"], "unreadable")
        self.assertIn("too big", r["reason"],
                      "a 120 MB member must be refused on its declared size, "
                      "not decompressed into memory first")

    def test_an_xlsx_with_no_shared_strings(self):
        import io
        S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("xl/worksheets/sheet1.xml",
                       f'<worksheet xmlns="{S}"><sheetData><row r="1">'
                       f'<c r="A1" t="s"><v>7</v></c><c r="B1"><v>42</v></c>'
                       f'</row></sheetData></worksheet>')
        r = self.rec("nostrings.xlsx", buf.getvalue())
        self.assertIn(r["status"], ("ok", "unreadable"))
        self.assertNotIn("Traceback", str(r["reason"]))

    def test_a_pdf_truncated_mid_stream(self):
        pdf = fixtures.make_pdf(["Quotation for the Johnson project", "nine thousand dollars"])
        for frac in (0.2, 0.4, 0.6, 0.8):
            text, reason = ingest.extract_pdf(pdf[: int(len(pdf) * frac)])
            self.assertTrue(text or reason, f"cut at {frac} gave neither text nor a reason")

    def test_a_damaged_pdf_is_not_called_a_scan(self):
        """Telling an owner a truncated file 'is a scan' sends them looking for
        a piece of paper that does not exist."""
        pdf = fixtures.make_pdf(["Quotation for the Johnson project"])
        text, reason = ingest.extract_pdf(pdf[: int(len(pdf) * 0.25)])
        self.assertFalse(text)
        self.assertNotIn("scan", (reason or "").lower())
        self.assertIn("cut short", (reason or "").lower())

    def test_a_scan_with_a_whole_file_present_still_reads_as_a_scan(self):
        text, reason = ingest.extract_pdf(fixtures.make_scanned_pdf())
        self.assertFalse(text)
        self.assertIn("scan", (reason or "").lower())

    def test_awkward_filenames(self):
        names = ["100% margin.txt", "quote #4471.txt", "‮reversed.txt",
                 "emoji \U0001F600 file.txt", "sp  ace.txt", "dot.in.name.txt"]
        made = []
        for n in names:
            try:
                (self.root / n).write_text("condenser warranty labour", encoding="utf-8")
                made.append(n)
            except OSError:
                continue
        files, problems = ingest.walk_files(self.root)
        self.assertEqual(len(files), len(made))
        for p in files:
            r = ingest.extract_file(p, self.root)
            self.assertEqual(r["status"], "ok", p.name)

    def test_a_file_that_vanishes_between_the_walk_and_the_read(self):
        p = self.root / "vanishing.txt"
        p.write_text("here for now", encoding="utf-8")
        files, _ = ingest.walk_files(self.root)
        p.unlink()
        r = ingest.extract_file(files[0], self.root)
        self.assertEqual(r["status"], "unreadable")
        self.assertTrue(r["reason"])

    @unittest.skipUnless(os.name == "nt", "Windows file locking")
    def test_a_file_another_process_is_holding(self):
        import msvcrt
        p = self.root / "held.docx"
        p.write_bytes(fixtures.make_docx(["locked content"]))
        fh = open(p, "rb")
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            r = ingest.extract_file(p, self.root)
            self.assertEqual(r["status"], "unreadable")
            self.assertIn("locked", r["reason"])
        finally:
            fh.close()

    def test_a_folder_that_cannot_be_entered_is_reported_not_dropped(self):
        class Boom:
            def __call__(self, *a, **k):
                raise OSError(13, "Permission denied")
        real = os.scandir

        def fake(path=".", *a, **k):
            if str(path).endswith("locked-dir"):
                raise PermissionError(13, "Permission denied", str(path))
            return real(path, *a, **k)
        (self.root / "locked-dir").mkdir()
        (self.root / "ok.txt").write_text("fine", encoding="utf-8")
        os.scandir = fake
        try:
            files, problems = ingest.walk_files(self.root)
        finally:
            os.scandir = real
        self.assertTrue(problems, "a folder we could not enter must appear in the summary")
        self.assertIn("could not be opened", problems[0]["reason"])

    def test_one_impossible_file_does_not_kill_the_whole_scan(self):
        """The rule is 'never crash, never silently drop'. That has to hold even
        when extraction fails in a way nobody predicted."""
        for i in range(6):
            (self.root / f"doc{i}.txt").write_text(f"document {i} about pricing",
                                                   encoding="utf-8")
        real = ingest.extract_file

        def explode(path, root):
            if Path(path).name == "doc3.txt":
                raise RuntimeError("something nobody thought of")
            return real(path, root)
        ingest.extract_file = explode
        try:
            summary = ingest.ingest_folder(self.root)
        finally:
            ingest.extract_file = real
        self.assertEqual(summary["total"], 6)
        self.assertEqual(len(summary["records"]), 6, "every file must come back as a record")
        bad = [r for r in summary["records"] if r["status"] == "unreadable"]
        self.assertEqual(len(bad), 1)
        self.assertTrue(bad[0]["reason"])

    def test_sync_tool_scratch_folders_are_skipped(self):
        for junk in (".tmp.driveupload", ".tmp.drivedownload"):
            (self.root / junk).mkdir()
            (self.root / junk / "7301").write_bytes(b"half an uploaded file")
        (self.root / "real.txt").write_text("a real document", encoding="utf-8")
        files, _ = ingest.walk_files(self.root)
        self.assertEqual([f.name for f in files], ["real.txt"])


class TestNeverWritesToTheSource(unittest.TestCase):
    """The single most important property in this app. An advisor whose tool
    mangles a client's documents is finished in a small town."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        (self.root / "SOPs").mkdir()
        (self.root / "SOPs" / "safety.md").write_text("Wear PPE on every job.",
                                                      encoding="utf-8")
        (self.root / "price list.txt").write_text("Condenser 285.00\nCapacitor 42.50",
                                                  encoding="utf-8")
        (self.root / "warranty.docx").write_bytes(fixtures.make_docx(["Twelve months"]))
        (self.root / "quote.pdf").write_bytes(fixtures.make_pdf(["Johnson project"]))
        (self.root / "scan.pdf").write_bytes(fixtures.make_scanned_pdf())
        (self.root / "broken.docx").write_bytes(b"not a zip at all")
        (self.root / "blank.txt").write_bytes(b"")
        (self.root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    def tearDown(self):
        try:
            self.tmp.cleanup()
        except OSError:
            pass

    def fingerprint(self):
        """Size, modified time to the nanosecond, and a hash of every byte."""
        out = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            for n in sorted(dirnames):
                p = Path(dirpath) / n
                out[str(p.relative_to(self.root))] = ("dir", None, None)
            for n in sorted(filenames):
                p = Path(dirpath) / n
                st = p.stat()
                out[str(p.relative_to(self.root))] = (
                    st.st_size, st.st_mtime_ns,
                    hashlib.sha256(p.read_bytes()).hexdigest())
        return out

    def test_a_full_scan_and_a_re_scan_change_nothing(self):
        before = self.fingerprint()
        first = ingest.ingest_folder(self.root)
        mid = self.fingerprint()
        self.assertEqual(before, mid, "the first read moved something")

        known = {r["rel"]: (r["size"], r["mtime"]) for r in first["records"]}
        ingest.ingest_folder(self.root, known=known)
        after = self.fingerprint()
        self.assertEqual(before, after, "the second read moved something")
        self.assertEqual(sorted(before), sorted(after), "a file appeared or disappeared")

    def test_nothing_new_appears_in_the_source_folder(self):
        before = set(self.fingerprint())
        ingest.ingest_folder(self.root)
        self.assertEqual(before, set(self.fingerprint()),
                         "no temp file, no lock file, no index file, ever")

    def test_no_filesystem_write_call_is_made_while_reading(self):
        """Belt and braces: fail the test if anything opens a source file for
        writing, or renames/removes/creates anything under the folder."""
        import builtins
        offences = []
        real_open, real_os_open = builtins.open, os.open
        watched = {"remove": os.remove, "unlink": os.unlink, "rename": os.rename,
                   "replace": os.replace, "mkdir": os.mkdir, "makedirs": os.makedirs,
                   "rmdir": os.rmdir, "utime": os.utime, "chmod": os.chmod,
                   "truncate": os.truncate}
        root = str(self.root).lower()

        def guard_open(file, mode="r", *a, **k):
            if str(file).lower().startswith(root) and any(c in mode for c in "wax+"):
                offences.append(("open", str(file), mode))
            return real_open(file, mode, *a, **k)

        def guard_os_open(path, flags, *a, **k):
            if str(path).lower().startswith(root) and flags & (os.O_WRONLY | os.O_RDWR |
                                                               os.O_CREAT | os.O_TRUNC):
                offences.append(("os.open", str(path), flags))
            return real_os_open(path, flags, *a, **k)

        def make_guard(name, fn):
            def guarded(*a, **k):
                if a and str(a[0]).lower().startswith(root):
                    offences.append((name, str(a[0])))
                return fn(*a, **k)
            return guarded

        builtins.open, os.open = guard_open, guard_os_open
        for name, fn in watched.items():
            setattr(os, name, make_guard(name, fn))
        try:
            ingest.ingest_folder(self.root)
        finally:
            builtins.open, os.open = real_open, real_os_open
            for name, fn in watched.items():
                setattr(os, name, fn)
        self.assertEqual(offences, [], f"the scan tried to write to the source: {offences}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
