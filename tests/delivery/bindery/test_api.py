"""API tests — every endpoint over real HTTP, including the ways it can go wrong.

    py tests/test_api.py
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

# Tests live in `system/apps/tests/`, mirroring the app tree. Walk up to the
# apps root, then reflect the path back down to the app this file tests.
_TESTS = Path(__file__).resolve().parent
_ROOT = next(p for p in _TESTS.parents if (p / "_shared" / "appkit.py").is_file())
_APP = _ROOT / _TESTS.relative_to(_ROOT / "tests")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_APP))
sys.path.insert(0, str(HERE))

_DATA = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
os.environ["BINDERY_DATA"] = _DATA.name

import fixtures                                                  # noqa: E402
import app as kbapp                                              # noqa: E402

BASE = ""


def call(method, path, body=None, raw=None):
    """(status, parsed json). Errors come back as data, not exceptions."""
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(payload)
        except ValueError:
            return exc.code, {"raw": payload}


def wait_for_scan(kb_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, data = call("GET", f"/api/kbs/{kb_id}/scan")
        p = data["progress"]
        if not p["running"]:
            return p
        time.sleep(0.05)
    raise AssertionError("the scan never finished")


class ApiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global BASE
        cls.server, BASE = kbapp.app.serve_background()
        cls.folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(cls.folder.name)
        (root / "SOPs").mkdir()
        (root / "SOPs" / "safety-manual.md").write_text(
            "# Safety manual\nWear PPE on every job site. Report injuries to the foreman.\n"
            "This manual is reviewed each January by the owner.", encoding="utf-8")
        (root / "SOPs" / "price list.txt").write_text(
            "Price list 2026\nCondenser fan motor 285.00\nCapacitor 42.50\n"
            "Labor rate ninety five dollars per hour", encoding="utf-8")
        (root / "copy of price list.txt").write_text(
            "Price list 2026\nCondenser fan motor 285.00\nCapacitor 42.50\n"
            "Labor rate ninety five dollars per hour", encoding="utf-8")
        (root / "warranty.docx").write_bytes(fixtures.make_docx(
            ["Warranty terms", "Parts are covered for twelve months from installation.",
             "Labor is covered for ninety days."]))
        (root / "quote.pdf").write_bytes(fixtures.make_pdf(
            ["Quotation for the Johnson project", "Total installed price is nine thousand",
             "Valid for thirty days from the date above"]))
        (root / "scanned.pdf").write_bytes(fixtures.make_scanned_pdf())
        (root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n not really a png")
        cls.root = str(root)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.folder.cleanup()

    def make_kb(self, name="Test client"):
        status, data = call("POST", "/api/kbs", {"name": name})
        self.assertEqual(status, 201)
        return data["kb"]


class TestBasics(ApiTestCase):
    def test_health(self):
        status, data = call("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

    def test_the_gui_is_served(self):
        with urllib.request.urlopen(BASE + "/") as r:
            html = r.read().decode("utf-8")
        self.assertIn("Knowledge Base Builder", html)
        self.assertIn("app.js", html)
        self.assertIn("/_shared/base.css", html)

    def test_shared_assets_are_served(self):
        for path in ("/_shared/base.css", "/_shared/ui.js", "/app.css", "/app.js"):
            with urllib.request.urlopen(BASE + path) as r:
                self.assertEqual(r.status, 200, path)
                self.assertTrue(len(r.read()) > 100, path)

    def test_unknown_endpoint_is_a_clean_404(self):
        status, data = call("GET", "/api/nope")
        self.assertEqual(status, 404)
        self.assertIn("error", data)

    def test_wrong_method_is_405_not_500(self):
        status, data = call("PUT", "/api/health")
        self.assertEqual(status, 405)
        self.assertIn("error", data)


class TestKbCrud(ApiTestCase):
    def test_create_list_get_update_delete(self):
        kb = self.make_kb("Northgate Mechanical")
        self.assertEqual(kb["name"], "Northgate Mechanical")
        self.assertTrue(kb["checklist"])

        status, data = call("GET", "/api/kbs")
        self.assertEqual(status, 200)
        self.assertIn(kb["id"], [k["id"] for k in data["kbs"]])

        status, data = call("GET", f"/api/kbs/{kb['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(data["kb"]["name"], "Northgate Mechanical")

        status, data = call("PUT", f"/api/kbs/{kb['id']}", {"name": "Northgate Mechanical",
                                                            "notes": "Owner is Dana"})
        self.assertEqual(status, 200)
        self.assertEqual(data["kb"]["name"], "Northgate Mechanical")

        status, _ = call("DELETE", f"/api/kbs/{kb['id']}")
        self.assertEqual(status, 200)
        status, _ = call("GET", f"/api/kbs/{kb['id']}")
        self.assertEqual(status, 404)

    def test_creating_without_a_name_still_works(self):
        status, data = call("POST", "/api/kbs", {})
        self.assertEqual(status, 201)
        self.assertTrue(data["kb"]["name"])

    def test_renaming_to_nothing_is_refused_politely(self):
        kb = self.make_kb()
        status, data = call("PUT", f"/api/kbs/{kb['id']}", {"name": "   "})
        self.assertEqual(status, 400)
        self.assertIn("name", data["error"].lower())

    def test_unknown_kb_is_404_everywhere(self):
        for method, path in (("GET", "/api/kbs/nope"),
                             ("PUT", "/api/kbs/nope"),
                             ("DELETE", "/api/kbs/nope"),
                             ("GET", "/api/kbs/nope/search?q=x"),
                             ("GET", "/api/kbs/nope/report"),
                             ("GET", "/api/kbs/nope/docs"),
                             ("POST", "/api/kbs/nope/scan")):
            body = {"name": "x"} if method in ("PUT", "POST") else None
            status, data = call(method, path, body)
            self.assertEqual(status, 404, f"{method} {path} -> {status}")
            self.assertIn("error", data)

    def test_malformed_json_body_is_400_not_500(self):
        kb = self.make_kb()
        status, data = call("PUT", f"/api/kbs/{kb['id']}", raw=b"{not json at all")
        self.assertEqual(status, 400)
        self.assertIn("JSON", data["error"])

    def test_a_body_that_is_not_an_object_is_400(self):
        kb = self.make_kb()
        status, data = call("PUT", f"/api/kbs/{kb['id']}", raw=b'["a","list"]')
        self.assertEqual(status, 400)

    def test_a_bad_checklist_shape_is_400(self):
        kb = self.make_kb()
        status, data = call("PUT", f"/api/kbs/{kb['id']}", {"checklist": "not a list"})
        self.assertEqual(status, 400)

    def test_a_checklist_can_be_replaced(self):
        kb = self.make_kb()
        status, data = call("PUT", f"/api/kbs/{kb['id']}", {"checklist": [
            {"key": "tickets", "label": "Service tickets", "phrases": "ticket, work order"}]})
        self.assertEqual(status, 200)
        self.assertEqual(len(data["kb"]["checklist"]), 1)
        self.assertEqual(data["kb"]["checklist"][0]["phrases"], ["ticket", "work order"])


class TestScanning(ApiTestCase):
    def test_a_folder_that_does_not_exist_is_a_clean_400(self):
        kb = self.make_kb()
        status, data = call("POST", f"/api/kbs/{kb['id']}/scan",
                            {"folder": r"Z:\nowhere\at\all"})
        self.assertEqual(status, 400)
        self.assertIn("no folder", data["error"].lower())

    def test_a_file_instead_of_a_folder_is_a_clean_400(self):
        kb = self.make_kb()
        status, data = call("POST", f"/api/kbs/{kb['id']}/scan",
                            {"folder": str(Path(self.root) / "logo.png")})
        self.assertEqual(status, 400)
        self.assertIn("not a folder", data["error"])

    def test_an_empty_folder_field_is_a_clean_400(self):
        kb = self.make_kb()
        status, data = call("POST", f"/api/kbs/{kb['id']}/scan", {"folder": "   "})
        self.assertEqual(status, 400)

    def test_progress_before_any_scan(self):
        kb = self.make_kb()
        status, data = call("GET", f"/api/kbs/{kb['id']}/scan")
        self.assertEqual(status, 200)
        self.assertFalse(data["progress"]["running"])

    def test_stopping_when_nothing_runs_is_not_an_error(self):
        kb = self.make_kb()
        status, data = call("DELETE", f"/api/kbs/{kb['id']}/scan")
        self.assertEqual(status, 200)
        self.assertFalse(data["stopped"])

    def test_a_real_scan_counts_everything(self):
        kb = self.make_kb("Scan me")
        status, _ = call("POST", f"/api/kbs/{kb['id']}/scan", {"folder": self.root})
        self.assertEqual(status, 202)
        p = wait_for_scan(kb["id"])
        self.assertIsNone(p["error"])
        self.assertFalse(p["cancelled"])
        summary = p["summary"]
        self.assertEqual(summary["total"], 7)
        self.assertEqual(summary["counts"]["ok"], 5)         # md, txt, txt, docx, pdf
        self.assertEqual(summary["counts"]["unreadable"], 1)  # the scan
        self.assertEqual(summary["counts"]["skipped"], 1)     # the png

        status, data = call("GET", f"/api/kbs/{kb['id']}")
        self.assertEqual(data["kb"]["stats"]["total"], 7)
        self.assertEqual(data["kb"]["folder"], self.root)
        self.assertTrue(data["kb"]["last_scan"])

    def test_a_second_scan_skips_unchanged_files(self):
        kb = self.make_kb("Twice")
        call("POST", f"/api/kbs/{kb['id']}/scan", {"folder": self.root})
        wait_for_scan(kb["id"])
        call("POST", f"/api/kbs/{kb['id']}/scan", {"folder": self.root})
        p = wait_for_scan(kb["id"])
        self.assertEqual(p["summary"]["counts"]["unchanged"], 7)
        self.assertEqual(p["summary"]["counts"]["ok"], 0)

    def test_reading_a_folder_changes_nothing_in_it(self):
        """The trust property: an advisor who mangles a client's files is finished."""
        def snapshot():
            out = {}
            for p in sorted(Path(self.root).rglob("*")):
                st = p.stat()
                out[str(p.relative_to(self.root))] = (p.is_dir(), st.st_size, int(st.st_mtime))
            return out

        before = snapshot()
        kb = self.make_kb("Read only")
        call("POST", f"/api/kbs/{kb['id']}/scan", {"folder": self.root})
        wait_for_scan(kb["id"])
        call("GET", f"/api/kbs/{kb['id']}/report")
        call("GET", f"/api/kbs/{kb['id']}/search?q=price")
        after = snapshot()
        self.assertEqual(before, after, "the source folder must come back byte-identical")

    def test_clearing_empties_the_index_without_touching_the_folder(self):
        kb = self.make_kb("Clear me")
        call("POST", f"/api/kbs/{kb['id']}/scan", {"folder": self.root})
        wait_for_scan(kb["id"])
        status, _ = call("POST", f"/api/kbs/{kb['id']}/clear", {})
        self.assertEqual(status, 200)
        status, data = call("GET", f"/api/kbs/{kb['id']}/docs")
        self.assertEqual(data["total"], 0)
        self.assertTrue(Path(self.root, "SOPs", "price list.txt").exists())


class TestSearchAndReport(ApiTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        status, data = call("POST", "/api/kbs", {"name": "Search fixture"})
        cls.kb = data["kb"]
        call("POST", f"/api/kbs/{cls.kb['id']}/scan", {"folder": cls.root})
        wait_for_scan(cls.kb["id"])

    def search(self, q):
        status, data = call("GET", f"/api/kbs/{self.kb['id']}/search?q="
                            + urllib.request.quote(q))
        self.assertEqual(status, 200)
        return data

    def test_search_finds_words_inside_a_docx(self):
        r = self.search("twelve months")
        self.assertTrue(r["results"])
        self.assertEqual(r["results"][0]["name"], "warranty.docx")

    def test_search_finds_words_inside_a_pdf(self):
        r = self.search("Johnson")
        self.assertTrue(r["results"])
        self.assertEqual(r["results"][0]["ext"], ".pdf")

    def test_results_carry_a_highlighted_snippet(self):
        r = self.search("capacitor")
        top = r["results"][0]
        self.assertIn("Capacitor", top["snippet"])
        self.assertTrue(any(p["hit"] for p in top["parts"]))

    def test_phrase_and_exclusion_reach_the_api(self):
        with_all = self.search("price list")
        excluded = self.search("price list -capacitor")
        self.assertGreater(with_all["total"], excluded["total"])

    def test_ext_filter_reaches_the_api(self):
        r = self.search("ext:pdf")
        self.assertTrue(all(x["ext"] == ".pdf" for x in r["results"]))

    def test_search_reports_how_long_it_took(self):
        r = self.search("safety")
        self.assertIn("ms", r)
        self.assertLess(r["ms"], 500)

    def test_an_empty_query_is_not_an_error(self):
        r = self.search("")
        self.assertEqual(r["total"], 0)
        self.assertEqual(r["results"], [])

    def test_a_bad_limit_is_a_clean_400(self):
        status, data = call("GET", f"/api/kbs/{self.kb['id']}/search?q=x&limit=lots")
        self.assertEqual(status, 400)
        self.assertIn("whole number", data["error"])

    def test_opening_a_document_returns_its_text(self):
        r = self.search("capacitor")
        doc_id = r["results"][0]["doc_id"]
        status, data = call("GET", f"/api/kbs/{self.kb['id']}/docs/{doc_id}")
        self.assertEqual(status, 200)
        self.assertIn("Capacitor", data["doc"]["text"])
        self.assertTrue(data["doc"]["exists"])

    def test_opening_a_document_that_is_gone_is_404(self):
        status, data = call("GET", f"/api/kbs/{self.kb['id']}/docs/deadbeefdeadbeef")
        self.assertEqual(status, 404)
        self.assertIn("error", data)

    def test_revealing_a_document_that_is_gone_is_404(self):
        status, _ = call("POST", f"/api/kbs/{self.kb['id']}/docs/deadbeef/reveal", {})
        self.assertEqual(status, 404)

    def test_listing_documents_and_filtering_by_state(self):
        status, data = call("GET", f"/api/kbs/{self.kb['id']}/docs")
        self.assertEqual(data["total"], 7)
        status, data = call("GET", f"/api/kbs/{self.kb['id']}/docs?status=unreadable")
        self.assertEqual(data["total"], 1)
        self.assertIn("scan", data["docs"][0]["reason"].lower())

    def test_the_report_finds_the_duplicate_price_list(self):
        status, data = call("GET", f"/api/kbs/{self.kb['id']}/report")
        self.assertEqual(status, 200)
        r = data["report"]
        self.assertEqual(r["duplicates"]["exact_group_count"], 1)
        self.assertEqual(r["counts"]["total"], 7)
        self.assertEqual(r["unreadable"]["count"], 1)

    def test_the_report_score_adds_up(self):
        status, data = call("GET", f"/api/kbs/{self.kb['id']}/report")
        s = data["report"]["score"]
        self.assertAlmostEqual(sum(c["points"] for c in s["components"]), s["total"], delta=0.3)
        self.assertEqual(sum(c["weight"] for c in s["components"]), 100)

    def test_markdown_export(self):
        status, data = call("GET", f"/api/kbs/{self.kb['id']}/report-markdown")
        self.assertEqual(status, 200)
        self.assertIn("Readiness score", data["markdown"])
        self.assertTrue(data["filename"].endswith(".md"))

    def test_a_report_on_an_unscanned_kb_is_a_clean_400(self):
        status, data = call("POST", "/api/kbs", {"name": "Nothing read"})
        empty_id = data["kb"]["id"]
        for path in (f"/api/kbs/{empty_id}/report", f"/api/kbs/{empty_id}/report-markdown"):
            status, data = call("GET", path)
            self.assertEqual(status, 400)
            self.assertIn("Read a folder first", data["error"])


class TestBrowse(ApiTestCase):
    def test_roots_come_back_without_a_path(self):
        status, data = call("GET", "/api/browse")
        self.assertEqual(status, 200)
        self.assertTrue(data["roots"])
        self.assertTrue(data["folders"])

    def test_listing_a_real_folder(self):
        status, data = call("GET", "/api/browse?path=" + urllib.request.quote(self.root))
        self.assertEqual(status, 200)
        self.assertIn("SOPs", [f["name"] for f in data["folders"]])
        self.assertIsNotNone(data["parent"])

    def test_a_folder_that_does_not_exist_is_a_clean_400(self):
        status, data = call("GET", "/api/browse?path=" + urllib.request.quote(r"Z:\nope"))
        self.assertEqual(status, 400)
        self.assertIn("error", data)

    def test_paths_the_operating_system_hates_are_400_not_500(self):
        for bad in ("a\x00b", "*", r"C:\<>|", r"\\nonexistent-host\share",
                    "?" * 300, "con", "."):
            status, data = call("GET", "/api/browse?path="
                                + urllib.request.quote(bad, safe=""))
            self.assertLess(status, 500, f"{bad!r} produced a {status}")


# ---------------------------------------------------------------- adversarial

class TestReadOnlyOverHttp(ApiTestCase):
    """Same guarantee as the unit test, but through the whole app: scan, re-scan,
    search, report, markdown, list. Every byte must come back identical."""

    def fingerprint(self, folder):
        out = {}
        for p in sorted(Path(folder).rglob("*")):
            st = p.stat()
            if p.is_dir():
                out[str(p)] = ("dir",)
            else:
                out[str(p)] = (st.st_size, st.st_mtime_ns,
                               hashlib.sha256(p.read_bytes()).hexdigest())
        return out

    def test_the_whole_app_never_moves_a_byte_in_the_source_folder(self):
        before = self.fingerprint(self.root)
        kb = self.make_kb("Read only, twice")
        call("POST", f"/api/kbs/{kb['id']}/scan", {"folder": self.root})
        wait_for_scan(kb["id"])
        after_first = self.fingerprint(self.root)
        self.assertEqual(before, after_first, "the first scan changed the folder")

        call("POST", f"/api/kbs/{kb['id']}/scan", {"folder": self.root})
        wait_for_scan(kb["id"])
        call("GET", f"/api/kbs/{kb['id']}/search?q=price")
        call("GET", f"/api/kbs/{kb['id']}/search?q=" + urllib.request.quote('"price list"'))
        call("GET", f"/api/kbs/{kb['id']}/report")
        call("GET", f"/api/kbs/{kb['id']}/report-markdown")
        call("GET", f"/api/kbs/{kb['id']}/docs?limit=2000")
        call("POST", f"/api/kbs/{kb['id']}/clear", {})
        call("DELETE", f"/api/kbs/{kb['id']}")
        self.assertEqual(before, self.fingerprint(self.root),
                         "something in the client's folder moved")


class TestScanLifecycle(ApiTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.bulk = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        for i in range(500):
            (Path(cls.bulk.name) / f"doc{i:04d}.txt").write_text(
                f"document {i} about price lists condensers warranty labour " * 30,
                encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.bulk.cleanup()
        super().tearDownClass()

    def test_stopping_and_starting_again_is_not_refused(self):
        kb = self.make_kb("Stop then go")
        call("POST", f"/api/kbs/{kb['id']}/scan", {"folder": self.bulk.name})
        time.sleep(0.1)
        call("DELETE", f"/api/kbs/{kb['id']}/scan")
        status, data = call("POST", f"/api/kbs/{kb['id']}/scan", {"folder": self.bulk.name})
        self.assertEqual(status, 202,
                         "you pressed Stop and then Read again; being told it is "
                         "'already being read' is wrong")
        wait_for_scan(kb["id"], timeout=120)

    def test_a_stopped_scan_is_reported_as_a_part_read_not_a_whole_one(self):
        kb = self.make_kb("Half read")
        call("POST", f"/api/kbs/{kb['id']}/scan", {"folder": self.bulk.name})
        time.sleep(0.12)
        call("DELETE", f"/api/kbs/{kb['id']}/scan")
        p = wait_for_scan(kb["id"], timeout=120)
        self.assertTrue(p["cancelled"])
        status, data = call("GET", f"/api/kbs/{kb['id']}")
        self.assertTrue(data["kb"]["partial"],
                        "a knowledge base built from a stopped scan must say so")
        status, data = call("GET", f"/api/kbs/{kb['id']}/report")
        self.assertEqual(status, 200)
        self.assertTrue(data["report"]["partial"])
        self.assertTrue(any("stopped" in h["text"].lower()
                            for h in data["report"]["headlines"]),
                        "the report must lead with the fact that it is incomplete")
        status, data = call("GET", f"/api/kbs/{kb['id']}/report-markdown")
        self.assertIn("stopped", data["markdown"].lower())

    def test_finishing_a_scan_clears_the_part_read_flag(self):
        kb = self.make_kb("Finished")
        call("POST", f"/api/kbs/{kb['id']}/scan", {"folder": self.root})
        wait_for_scan(kb["id"])
        status, data = call("GET", f"/api/kbs/{kb['id']}")
        self.assertFalse(data["kb"]["partial"])

    def test_deleting_a_knowledge_base_mid_scan_leaves_nothing_behind(self):
        kb = self.make_kb("Doomed")
        call("POST", f"/api/kbs/{kb['id']}/scan", {"folder": self.bulk.name})
        time.sleep(0.15)
        status, _ = call("DELETE", f"/api/kbs/{kb['id']}")
        self.assertEqual(status, 200)
        deadline = time.time() + 30
        while time.time() < deadline and kbapp.JOBS.get(kb["id"]):
            time.sleep(0.05)
        time.sleep(1.5)
        docs = kbapp.index._db.execute(
            "SELECT COUNT(*) n FROM docs WHERE kb_id=?", (kb["id"],)).fetchone()[0]
        posts = kbapp.index._db.execute(
            "SELECT COUNT(*) n FROM postings WHERE kb_id=?", (kb["id"],)).fetchone()[0]
        self.assertEqual((docs, posts), (0, 0),
                         "the scan thread kept writing into a knowledge base that "
                         "no longer exists")

    def test_two_scans_of_the_same_folder_at_once_are_refused_cleanly(self):
        kb = self.make_kb("Twice at once")
        first = call("POST", f"/api/kbs/{kb['id']}/scan", {"folder": self.bulk.name})
        second = call("POST", f"/api/kbs/{kb['id']}/scan", {"folder": self.bulk.name})
        self.assertEqual(first[0], 202)
        self.assertEqual(second[0], 409)
        call("DELETE", f"/api/kbs/{kb['id']}/scan")
        wait_for_scan(kb["id"], timeout=120)

    def test_a_scan_that_fails_halfway_keeps_what_it_read(self):
        kb = self.make_kb("Blows up")
        real = kbapp.ingest.extract_file
        seen = [0]

        def explode(path, root):
            seen[0] += 1
            if seen[0] == 25:
                raise RuntimeError("simulated disk failure")
            return real(path, root)
        kbapp.ingest.extract_file = explode
        try:
            call("POST", f"/api/kbs/{kb['id']}/scan", {"folder": self.bulk.name})
            p = wait_for_scan(kb["id"], timeout=120)
        finally:
            kbapp.ingest.extract_file = real
        self.assertIsNone(p["error"],
                          "one file nobody could read must not end the scan")
        status, data = call("GET", f"/api/kbs/{kb['id']}/docs?status=unreadable")
        self.assertGreaterEqual(data["total"], 1)


class TestSearchThroughTheApi(ApiTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        status, data = call("POST", "/api/kbs", {"name": "Torture"})
        cls.kb = data["kb"]
        call("POST", f"/api/kbs/{cls.kb['id']}/scan", {"folder": cls.root})
        wait_for_scan(cls.kb["id"])

    def test_no_query_can_produce_a_500(self):
        for q in ['"foo', '""', '-foo', 'ext:', 'folder:', 'the and of', 'C++', 'a(b',
                  '.*', '[', '\\', '(((', '\x00', '?' * 500, 'a' * 9000,
                  'price' + ' word' * 2000]:
            status, data = call("GET", f"/api/kbs/{self.kb['id']}/search?q="
                                + urllib.request.quote(q, safe=""))
            self.assertEqual(status, 200, f"{q[:20]!r} produced {status}")
            self.assertIn("results", data)

    def test_a_query_of_only_common_words_explains_itself(self):
        status, data = call("GET", f"/api/kbs/{self.kb['id']}/search?q="
                            + urllib.request.quote("the and of it"))
        self.assertEqual(data["total"], 0)
        self.assertTrue(data.get("empty_reason"))

    def test_the_report_carries_every_field_the_page_draws(self):
        """The GUI builds the report from these keys. A rename here is a blank
        section or a 'null%' in front of a client, and no Python test would
        otherwise notice."""
        status, data = call("GET", f"/api/kbs/{self.kb['id']}/report")
        r = data["report"]
        for key in ("partial", "counts", "duplicates", "stale", "orphans", "unreadable",
                    "skipped", "coverage", "concentration", "score", "headlines"):
            self.assertIn(key, r, key)
        for key in ("total", "out_of", "components", "band", "note"):
            self.assertIn(key, r["score"], key)
        for c in r["score"]["components"]:
            self.assertEqual(set(c), {"key", "label", "weight", "raw_pct", "points",
                                      "measured", "how"})
        for key in ("unreferenced", "unreferenced_count", "unreferenced_share",
                    "judged_count", "unjudged_count", "thin_count"):
            self.assertIn(key, r["orphans"], key)
        for key in ("found", "missing", "checked", "found_count", "named_count",
                    "mention_count", "mentioned_only", "credit", "credit_pct", "pct"):
            self.assertIn(key, r["coverage"], key)
        for f in r["coverage"]["found"]:
            self.assertIn("named", f)
            for hit in f["hits"]:
                self.assertEqual(set(hit), {"rel", "matched", "where"})
        for key in ("exact_groups", "near_groups", "exact_group_count",
                    "near_group_count", "files_involved", "share", "wasted_copies"):
            self.assertIn(key, r["duplicates"], key)
        self.assertIn("partial", data["kb"])

    def test_a_negative_offset_or_silly_limit_is_handled(self):
        for qs in ("limit=0", "limit=99999", "offset=-5", "limit=1&offset=999999"):
            status, data = call("GET", f"/api/kbs/{self.kb['id']}/search?q=price&{qs}")
            self.assertEqual(status, 200, qs)
            self.assertIn("results", data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
