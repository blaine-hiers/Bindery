"""Gap report tests — duplicates, staleness, orphans, coverage, and the score.

    py tests/test_analyze.py
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

# Tests live in `system/apps/tests/`, mirroring the app tree. Walk up to the
# apps root, then reflect the path back down to the app this file tests.
_TESTS = Path(__file__).resolve().parent
_ROOT = next(p for p in _TESTS.parents if (p / "_shared" / "appkit.py").is_file())
_APP = _ROOT / _TESTS.relative_to(_ROOT / "tests")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_APP))

import analyze as gap                                            # noqa: E402

NOW = 1_760_000_000          # a fixed "today" so the tests never drift
YEAR = int(365.25 * 24 * 3600)

LOREM = ("The technician shall inspect the condenser coil and record the readings on the "
         "service ticket before leaving the customer site every single visit without fail")


def doc(rel, text="", *, mtime=NOW, status="ok", ext=None, size=1000,
        reason=None, author=None):
    name = rel.rsplit("/", 1)[-1]
    return {
        "doc_id": rel, "rel": rel, "name": name,
        "folder": rel.rsplit("/", 1)[0] if "/" in rel else "",
        "ext": ext if ext is not None else ("." + name.rsplit(".", 1)[-1] if "." in name else ""),
        "size": size, "mtime": mtime, "status": status, "reason": reason,
        "author": author, "chash": gap_hash(text) if status == "ok" else None,
        "nwords": len(text.split()), "text": text,
    }


def gap_hash(text):
    import hashlib
    import re
    return hashlib.sha1(re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()
                        .encode("utf-8")).hexdigest()


class TestDuplicates(unittest.TestCase):
    def test_exact_copies_are_grouped(self):
        docs = [doc("a/price.txt", LOREM), doc("b/price.txt", LOREM),
                doc("c/price copy.txt", LOREM), doc("other.txt", "something else entirely")]
        r = gap.analyze(docs, now=NOW)
        self.assertEqual(r["duplicates"]["exact_group_count"], 1)
        self.assertEqual(len(r["duplicates"]["exact_groups"][0]["files"]), 3)
        self.assertEqual(r["duplicates"]["wasted_copies"], 2)

    def test_case_and_spacing_do_not_stop_an_exact_match(self):
        docs = [doc("a.txt", LOREM), doc("b.txt", LOREM.upper() + "   \n\n")]
        r = gap.analyze(docs, now=NOW)
        self.assertEqual(r["duplicates"]["exact_group_count"], 1)

    def test_near_copies_are_found(self):
        base = (LOREM + " ") * 6
        edited = base.replace("condenser coil", "evaporator coil") + " Revised April."
        r = gap.analyze([doc("v1.txt", base), doc("v2.txt", edited)], now=NOW)
        self.assertEqual(r["duplicates"]["near_group_count"], 1)
        group = r["duplicates"]["near_groups"][0]
        self.assertEqual(len(group["files"]), 2)
        self.assertGreater(group["similarity"], 0.55)

    def test_unrelated_documents_are_not_flagged(self):
        a = ("Safety policy. All staff must wear eye protection in the shop area at all "
             "times and report any injury to the foreman before the end of the shift. ") * 4
        b = ("Vendor agreement. Payment is net thirty days from the date of invoice and "
             "late payments accrue interest at one and a half percent per month. ") * 4
        r = gap.analyze([doc("safety.txt", a), doc("vendor.txt", b)], now=NOW)
        self.assertEqual(r["duplicates"]["near_group_count"], 0)
        self.assertEqual(r["duplicates"]["exact_group_count"], 0)
        self.assertEqual(r["duplicates"]["files_involved"], 0)

    def test_a_high_threshold_stops_flagging_a_loose_pair(self):
        base = (LOREM + " ") * 6
        edited = base.replace("condenser", "evaporator").replace("technician", "installer")
        loose = gap.analyze([doc("a.txt", base), doc("b.txt", edited)],
                            now=NOW, near_threshold=0.4)
        strict = gap.analyze([doc("a.txt", base), doc("b.txt", edited)],
                             now=NOW, near_threshold=0.999)
        self.assertEqual(loose["duplicates"]["near_group_count"], 1)
        self.assertEqual(strict["duplicates"]["near_group_count"], 0)

    def test_jaccard_is_what_it_says(self):
        self.assertEqual(gap.jaccard({1, 2, 3}, {1, 2, 3}), 1.0)
        self.assertEqual(gap.jaccard({1, 2}, {3, 4}), 0.0)
        self.assertAlmostEqual(gap.jaccard({1, 2, 3, 4}, {3, 4, 5, 6}), 2 / 6)
        self.assertEqual(gap.jaccard(set(), {1}), 0.0)

    def test_cutting_long_documents_down_does_not_bias_the_similarity(self):
        """A long document keeps only its lowest-numbered word runs. Comparing
        that against a short document's complete list must not make a real pair
        look unrelated."""
        import random
        rnd = random.Random(11)
        vocab = [f"w{i}" for i in range(600)]
        shared = " ".join(rnd.choice(vocab) for _ in range(3000))
        long_doc = shared + " " + " ".join(rnd.choice(vocab) for _ in range(300))
        short_doc = shared
        exact = gap.jaccard(gap.shingles(long_doc, cap=0), gap.shingles(short_doc, cap=0))
        capped = gap.jaccard(gap.shingles(long_doc), gap.shingles(short_doc),
                             cap=gap._MAX_SHINGLES)
        self.assertGreater(exact, 0.8)
        self.assertAlmostEqual(capped, exact, delta=0.06,
                               msg=f"cutting the long document moved the answer from "
                                   f"{exact:.3f} to {capped:.3f}")

    def test_a_very_long_document_does_not_stall_the_report(self):
        import time as _t
        text = " ".join(f"word{i % 900}" for i in range(120000))
        started = _t.perf_counter()
        gap.minhash(gap.shingles(text))
        self.assertLess(_t.perf_counter() - started, 0.6,
                        "one long manual must not cost half a second of report time")

    def test_shingles_are_stable_between_runs(self):
        self.assertEqual(gap.shingles(LOREM), gap.shingles(LOREM))
        self.assertEqual(gap.shingles("only four words here"), set())

    def test_tiny_documents_are_not_called_duplicates(self):
        r = gap.analyze([doc("a.txt", "ok"), doc("b.txt", "ok")], now=NOW)
        self.assertEqual(r["duplicates"]["exact_group_count"], 0)


class TestStale(unittest.TestCase):
    DOCS = [
        doc("new.txt", LOREM, mtime=NOW - 30 * 86400),
        doc("twoyear.txt", LOREM + " a", mtime=NOW - 2 * YEAR),
        doc("fouryear.txt", LOREM + " b", mtime=NOW - 4 * YEAR),
        doc("sevenyear.txt", LOREM + " c", mtime=NOW - 7 * YEAR),
    ]

    def test_buckets(self):
        b = gap.analyze(self.DOCS, now=NOW)["stale"]["buckets"]
        self.assertEqual(b["under1"], 1)
        self.assertEqual(b["1to3"], 1)
        self.assertEqual(b["3to5"], 1)
        self.assertEqual(b["over5"], 1)

    def test_share_over_three_years(self):
        st = gap.analyze(self.DOCS, now=NOW)["stale"]
        self.assertEqual(st["over_3y"], 2)
        self.assertEqual(st["share_over_3y"], 50.0)

    def test_the_oldest_are_named_oldest_first(self):
        oldest = gap.analyze(self.DOCS, now=NOW)["stale"]["oldest"]
        self.assertEqual(oldest[0]["rel"], "sevenyear.txt")
        self.assertAlmostEqual(oldest[0]["years"], 7.0, places=1)


class TestOrphans(unittest.TestCase):
    def test_a_file_nothing_mentions_is_unreferenced(self):
        docs = [
            doc("handbook.txt", "See the warranty-terms document for details. " + LOREM),
            doc("warranty-terms.txt", "Parts are covered for twelve months. " + LOREM),
            doc("forgotten-pricing-sheet.txt", "Nobody links here. " + LOREM),
        ]
        o = gap.analyze(docs, now=NOW)["orphans"]
        rels = [f["rel"] for f in o["unreferenced"]]
        self.assertIn("forgotten-pricing-sheet.txt", rels)
        self.assertNotIn("warranty-terms.txt", rels)

    def test_thin_files_are_counted_separately(self):
        docs = [doc("empty-ish.txt", "just three words"), doc("full.txt", LOREM)]
        o = gap.analyze(docs, now=NOW)["orphans"]
        self.assertEqual(o["thin_count"], 1)
        self.assertEqual(o["thin"][0]["rel"], "empty-ish.txt")


class TestUnreadable(unittest.TestCase):
    DOCS = [
        doc("scan1.pdf", status="unreadable", reason="no text inside — this is a scan"),
        doc("scan2.pdf", status="unreadable", reason="no text inside — this is a scan"),
        doc("locked.pdf", status="unreadable", reason="the PDF is locked with a password"),
        doc("photo.jpg", status="skipped", reason=".jpg files are not read"),
        doc("good.txt", LOREM),
    ]

    def test_grouped_by_reason_with_the_reason_kept(self):
        u = gap.analyze(self.DOCS, now=NOW)["unreadable"]
        self.assertEqual(u["count"], 3)
        self.assertEqual(u["by_reason"][0]["count"], 2)
        self.assertIn("scan", u["by_reason"][0]["reason"])
        self.assertEqual(len(u["by_reason"]), 2)

    def test_skipped_files_are_counted_but_kept_apart(self):
        r = gap.analyze(self.DOCS, now=NOW)
        self.assertEqual(r["skipped"]["count"], 1)
        self.assertEqual(r["counts"]["total"], 5)
        self.assertEqual(r["counts"]["readable"], 1)

    def test_share_is_of_everything_not_just_readable(self):
        u = gap.analyze(self.DOCS, now=NOW)["unreadable"]
        self.assertEqual(u["share"], 60.0)


class TestCoverage(unittest.TestCase):
    CHECK = [
        {"key": "safety", "label": "Safety rules", "why": "someone gets hurt",
         "phrases": ["safety", "ppe"]},
        {"key": "pricing", "label": "A price list", "why": "quotes go wrong",
         "phrases": ["price list", "rate sheet"]},
        {"key": "backup", "label": "Backups", "why": "the day you need one",
         "phrases": ["backup", "disaster recovery"]},
    ]

    def test_found_and_missing_are_split(self):
        docs = [doc("safety-manual.txt", "Wear PPE. " + LOREM),
                doc("prices.txt", "Our price list for the year. " + LOREM)]
        cov = gap.analyze(docs, checklist=self.CHECK, now=NOW)["coverage"]
        self.assertEqual(cov["found_count"], 2)
        self.assertEqual([m["key"] for m in cov["missing"]], ["backup"])
        self.assertAlmostEqual(cov["pct"], 66.7, places=1)

    def test_a_match_in_the_filename_counts(self):
        docs = [doc("disaster recovery plan.txt", LOREM)]
        cov = gap.analyze(docs, checklist=self.CHECK, now=NOW)["coverage"]
        self.assertIn("backup", [f["key"] for f in cov["found"]])

    def test_which_words_matched_is_recorded(self):
        docs = [doc("x.txt", "Our rate sheet is attached. " + LOREM)]
        cov = gap.analyze(docs, checklist=self.CHECK, now=NOW)["coverage"]
        hit = [f for f in cov["found"] if f["key"] == "pricing"][0]
        self.assertEqual(hit["hits"][0]["matched"], "rate sheet")

    def test_switched_off_items_are_not_checked(self):
        check = [dict(self.CHECK[0]), dict(self.CHECK[1], on=False)]
        cov = gap.analyze([doc("a.txt", LOREM)], checklist=check, now=NOW)["coverage"]
        self.assertEqual(cov["checked"], 1)

    def test_the_shipped_checklist_is_the_right_shape(self):
        for item in gap.DEFAULT_CHECKLIST:
            self.assertTrue(item["key"] and item["label"] and item["why"])
            self.assertTrue(item["phrases"])


class TestConcentration(unittest.TestCase):
    def test_folder_share(self):
        docs = ([doc(f"Invoices/i{i}.txt", LOREM + str(i)) for i in range(8)]
                + [doc("SOPs/s1.txt", LOREM + "x"), doc("SOPs/s2.txt", LOREM + "y")])
        k = gap.analyze(docs, now=NOW)["concentration"]
        self.assertEqual(k["top_folder"], "Invoices")
        self.assertEqual(k["top_folder_share"], 80.0)

    def test_author_share_only_counts_files_that_record_one(self):
        docs = [doc("a.txt", LOREM + "a", author="Dana"),
                doc("b.txt", LOREM + "b", author="Dana"),
                doc("c.txt", LOREM + "c", author=None)]
        k = gap.analyze(docs, now=NOW)["concentration"]
        self.assertEqual(k["top_author"], "Dana")
        self.assertEqual(k["authors_known"], 2)
        self.assertEqual(k["top_author_share"], 100.0)


class TestScore(unittest.TestCase):
    DOCS = [doc("SOPs/safety.txt", "Wear PPE at all times. " + LOREM),
            doc("SOPs/price list.txt", "Our price list. " + LOREM + " x"),
            doc("SOPs/old.txt", LOREM + " y", mtime=NOW - 6 * YEAR),
            doc("scan.pdf", status="unreadable", reason="this is a scan")]

    def test_weights_add_to_one_hundred(self):
        self.assertEqual(sum(w for _, _, w in gap.SCORE_WEIGHTS), 100)

    def test_the_points_column_adds_up_to_the_score(self):
        s = gap.analyze(self.DOCS, now=NOW)["score"]
        self.assertAlmostEqual(sum(c["points"] for c in s["components"]), s["total"], places=1)
        self.assertEqual(s["out_of"], 100)

    def test_every_component_shows_its_arithmetic(self):
        s = gap.analyze(self.DOCS, now=NOW)["score"]
        self.assertEqual(len(s["components"]), 5)
        for c in s["components"]:
            self.assertTrue(c["how"], "every component must say how it was worked out")
            self.assertAlmostEqual(c["points"], c["raw_pct"] / 100 * c["weight"], delta=0.06)
            self.assertTrue(0 <= c["raw_pct"] <= 100)

    def test_readable_component_is_the_readable_share(self):
        s = gap.analyze(self.DOCS, now=NOW)["score"]
        readable = [c for c in s["components"] if c["key"] == "readable"][0]
        self.assertEqual(readable["raw_pct"], 75.0)      # 3 of 4 files opened
        self.assertEqual(readable["points"], 18.8)       # 75 % of 25

    def test_a_perfect_folder_scores_high_and_a_bad_one_low(self):
        good = [doc(f"f{i}/doc{i}.txt", LOREM + " " + phrase, mtime=NOW - 86400)
                for i, phrase in enumerate(
                    ["safety ppe", "price list", "onboarding new hire", "vendor agreement",
                     "work order standard operating", "invoice billing", "backup restore",
                     "acceptable use chatgpt", "certificate of insurance", "warranty"])]
        bad = [doc(f"Pile/x{i}.pdf", status="unreadable", reason="this is a scan")
               for i in range(10)]
        self.assertGreater(gap.analyze(good, now=NOW)["score"]["total"], 80)
        self.assertLess(gap.analyze(bad, now=NOW)["score"]["total"], 25)

    def test_spread_penalty_only_bites_over_thirty_five_percent(self):
        self.assertEqual(gap._spread_pct(20.0), 100.0)
        self.assertEqual(gap._spread_pct(35.0), 100.0)
        self.assertEqual(gap._spread_pct(100.0), 0.0)
        self.assertAlmostEqual(gap._spread_pct(67.5), 50.0, places=1)

    def test_band_words_change_with_the_score(self):
        self.assertEqual(gap._band(90), "In good shape")
        self.assertNotEqual(gap._band(90), gap._band(30))


class TestWholeReport(unittest.TestCase):
    DOCS = [doc("SOPs/safety.txt", "Wear PPE. " + LOREM),
            doc("SOPs/copy of safety.txt", "Wear PPE. " + LOREM),
            doc("old/manual.txt", LOREM + " z", mtime=NOW - 8 * YEAR),
            doc("scan.pdf", status="unreadable", reason="this is a scan"),
            doc("logo.png", status="skipped", reason=".png files are not read")]

    def test_an_empty_knowledge_base_does_not_explode(self):
        r = gap.analyze([], now=NOW)
        self.assertEqual(r["counts"]["total"], 0)
        self.assertEqual(r["score"]["total"], 0.0)
        self.assertEqual(r["duplicates"]["files_involved"], 0)

    def test_headlines_are_plain_sentences(self):
        r = gap.analyze(self.DOCS, now=NOW)
        self.assertTrue(r["headlines"])
        for h in r["headlines"]:
            self.assertIn(h["kind"], ("bad", "warn", "info"))
            self.assertTrue(h["text"].endswith("."))
            for jargon in ("orphan", "entropy", "corpus", "taxonomy", "heuristic"):
                self.assertNotIn(jargon, h["text"].lower())

    def test_markdown_export_contains_the_arithmetic(self):
        r = gap.analyze(self.DOCS, now=NOW)
        md = gap.report_markdown("Northgate Mechanical", r"C:\Clients\Northgate", r)
        self.assertIn("# What your documents look like — Northgate Mechanical", md)
        self.assertIn("Readiness score", md)
        self.assertIn("Copies that disagree", md)
        self.assertIn("Files nobody can open or search", md)
        self.assertIn("What is missing", md)
        self.assertIn("× 25 ÷ 100", md.replace("×  ", "× "))
        self.assertIn(str(r["score"]["total"]), md)

    def test_markdown_never_states_a_bare_number_without_its_two_parts(self):
        r = gap.analyze(self.DOCS, now=NOW)
        md = gap.report_markdown("Client", "/tmp", r)
        for comp in r["score"]["components"]:
            self.assertIn(comp["how"], md)


# ---------------------------------------------------------------- adversarial

class TestScoreEdgeCases(unittest.TestCase):
    """`05` §8: never a figure without visible arithmetic. A line whose words say
    one number and whose points column says another is worse than no number."""

    def _check_no_contradiction(self, r, label):
        s = r["score"]
        self.assertAlmostEqual(
            sum(c["points"] for c in s["components"] if c.get("measured", True)),
            s["total"], places=1, msg=f"{label}: the points column must add to the score")
        self.assertEqual(
            sum(c["weight"] for c in s["components"] if c.get("measured", True)),
            s["out_of"], f"{label}: the score must be out of the weights it actually used")
        for c in s["components"]:
            if not c.get("measured", True):
                self.assertIsNone(c["points"], f"{label}/{c['key']}: unmeasured means no points")
                self.assertIn("could not", c["how"].lower())
                continue
            # The sentence must not claim a percentage the points column disagrees with.
            import re as _re
            claimed = [float(x) for x in _re.findall(r"(\d+(?:\.\d+)?)\s*%", c["how"])]
            self.assertIn(c["raw_pct"], claimed,
                          f"{label}/{c['key']}: the arithmetic says {claimed}, "
                          f"the column says {c['raw_pct']}% — one of them is lying")

    def test_every_document_unreadable(self):
        docs = [doc(f"scan{i}.pdf", status="unreadable", reason="this is a scan")
                for i in range(8)]
        r = gap.analyze(docs, now=NOW)
        self._check_no_contradiction(r, "all unreadable")
        keys = {c["key"] for c in r["score"]["components"] if not c.get("measured", True)}
        self.assertEqual(keys, {"current", "unique", "spread", "covered"},
                         "with no readable file there is nothing to measure staleness, "
                         "copies, spread or coverage against")
        self.assertEqual(r["score"]["out_of"], 25,
                         "the only thing measurable here is that nothing opened")

    def test_nothing_is_missing_if_we_could_not_look(self):
        """With no readable file the safety manual may be sitting inside one of
        the scans. A headline saying '10 things are missing' next to a score line
        saying coverage could not be measured is the same lie, moved."""
        docs = [doc(f"scan{i}.pdf", status="unreadable", reason="this is a scan")
                for i in range(20)]
        r = gap.analyze(docs, now=NOW)
        self.assertFalse(r["coverage"]["measured"])
        for h in r["headlines"]:
            self.assertNotIn("missing", h["text"].lower())
        md = gap.report_markdown("Client", "/tmp", r)
        self.assertIn("could not look", md)
        self.assertNotIn("**Not found anywhere:**", md)
        self.assertIn("How concentrated it all is", md, "the report must still finish")

    def test_zero_documents(self):
        r = gap.analyze([], now=NOW)
        self._check_no_contradiction(r, "empty")
        self.assertEqual(r["score"]["out_of"], 0)
        self.assertIn("nothing", r["score"]["band"].lower())

    def test_one_document(self):
        r = gap.analyze([doc("a.txt", LOREM)], now=NOW)
        self._check_no_contradiction(r, "one document")

    def test_every_document_a_duplicate(self):
        docs = [doc(f"folder{i}/copy.txt", LOREM) for i in range(6)]
        r = gap.analyze(docs, now=NOW)
        self._check_no_contradiction(r, "all duplicates")
        unique = [c for c in r["score"]["components"] if c["key"] == "unique"][0]
        self.assertEqual(unique["raw_pct"], 0.0)

    def test_a_folder_of_photos_is_not_scored_as_unreadable_documents(self):
        """Skipped files are not documents. Punishing an owner 25 points because
        they keep job photos next to their paperwork is not a finding."""
        docs = ([doc(f"photo{i}.jpg", status="skipped", reason=".jpg files are not read")
                 for i in range(90)]
                + [doc(f"SOP/doc{i}.txt", LOREM + str(i)) for i in range(10)])
        r = gap.analyze(docs, now=NOW)
        readable = [c for c in r["score"]["components"] if c["key"] == "readable"][0]
        self.assertEqual(readable["raw_pct"], 100.0,
                         "all 10 documents opened; the 90 photos are not documents")
        self.assertIn("10 of 10", readable["how"])

    def test_an_all_photo_folder_cannot_be_scored_on_readability(self):
        docs = [doc(f"photo{i}.jpg", status="skipped", reason=".jpg files are not read")
                for i in range(20)]
        r = gap.analyze(docs, now=NOW)
        self._check_no_contradiction(r, "all photos")
        readable = [c for c in r["score"]["components"] if c["key"] == "readable"][0]
        self.assertFalse(readable["measured"])

    def test_turning_the_whole_checklist_off_is_honoured_not_ignored(self):
        docs = [doc("a.txt", LOREM)]
        r = gap.analyze(docs, checklist=[], now=NOW)
        self.assertEqual(r["coverage"]["checked"], 0)
        covered = [c for c in r["score"]["components"] if c["key"] == "covered"][0]
        self.assertFalse(covered["measured"],
                         "an empty checklist must not silently fall back to the built-in one")

    def test_the_markdown_shows_unmeasured_lines_honestly(self):
        docs = [doc(f"scan{i}.pdf", status="unreadable", reason="this is a scan")
                for i in range(4)]
        r = gap.analyze(docs, now=NOW)
        md = gap.report_markdown("Client", "/tmp", r)
        self.assertIn("out of " + str(r["score"]["out_of"]), md)
        self.assertNotIn("| 0.0% | 20 | 0.0 |", md,
                         "a line that could not be measured must not print as a zero score")


class TestCoverageFalsePositives(unittest.TestCase):
    """'ppe' inside 'shipped' told an owner they had a safety manual."""

    CHECK = [{"key": "safety", "label": "Safety rules", "why": "someone gets hurt",
              "phrases": ["ppe", "safety"]},
             {"key": "customer", "label": "How a job is handled", "why": "second employee",
              "phrases": ["sop", "work order"]}]

    def test_a_word_inside_another_word_is_not_a_match(self):
        docs = [doc("note.txt", "The order shipped on Tuesday and upper management agreed. "
                                "Our philosophy is a sophisticated one. " + LOREM)]
        cov = gap.analyze(docs, checklist=self.CHECK, now=NOW)["coverage"]
        self.assertEqual(cov["found_count"], 0,
                         "'shipped' is not PPE and 'philosophy' is not an SOP")

    def test_a_real_word_still_matches(self):
        docs = [doc("a.txt", "Wear PPE on site. " + LOREM),
                doc("b.txt", "Follow the SOP for every job. " + LOREM)]
        cov = gap.analyze(docs, checklist=self.CHECK, now=NOW)["coverage"]
        self.assertEqual(cov["found_count"], 2)

    def test_a_hyphenated_or_punctuated_form_still_matches(self):
        docs = [doc("a.txt", "See the work-order process. " + LOREM)]
        cov = gap.analyze(docs, checklist=self.CHECK, now=NOW)["coverage"]
        self.assertIn("customer", [f["key"] for f in cov["found"]])

    def test_the_report_says_where_it_matched(self):
        docs = [doc("safety-ppe-manual.txt", LOREM),
                doc("random.txt", "we follow the SOP here. " + LOREM)]
        cov = gap.analyze(docs, checklist=self.CHECK, now=NOW)["coverage"]
        by_key = {f["key"]: f for f in cov["found"]}
        self.assertEqual(by_key["safety"]["hits"][0]["where"], "name")
        self.assertEqual(by_key["customer"]["hits"][0]["where"], "text")

    def test_a_mention_is_worth_half_a_document_and_says_so(self):
        """A glossary that uses the word 'warranty' is not warranty terms. The
        old score gave 25 out of 25 for ten passing mentions."""
        named = [doc("safety-ppe-manual.txt", LOREM)]
        mention = [doc("memo.txt", "Follow the SOP for every job. " + LOREM)]
        r = gap.analyze(named + mention, checklist=self.CHECK, now=NOW)
        cov = r["coverage"]
        self.assertEqual((cov["named_count"], cov["mention_count"]), (1, 1))
        self.assertEqual(cov["credit"], 1.5)
        self.assertEqual(cov["credit_pct"], 75.0)
        comp = [c for c in r["score"]["components"] if c["key"] == "covered"][0]
        self.assertEqual(comp["raw_pct"], 75.0)
        self.assertIn("÷ 2", comp["how"])
        self.assertIn("1.5 of 2", comp["how"])

    def test_a_folder_of_mentions_alone_cannot_score_full_marks(self):
        docs = [doc("memo.txt", "we talk about ppe and the sop here. " + LOREM)]
        r = gap.analyze(docs, checklist=self.CHECK, now=NOW)
        comp = [c for c in r["score"]["components"] if c["key"] == "covered"][0]
        self.assertEqual(r["coverage"]["found_count"], 2)
        self.assertEqual(comp["raw_pct"], 50.0)

    def test_the_shipped_checklist_survives_word_boundaries(self):
        """Every default phrase must still be findable in ordinary business prose."""
        for item in gap.DEFAULT_CHECKLIST:
            docs = [doc("x.txt", f"This document is about {item['phrases'][0]} and nothing else. "
                                 + LOREM)]
            cov = gap.analyze(docs, checklist=[item], now=NOW)["coverage"]
            self.assertEqual(cov["found_count"], 1, item["key"])


class TestOrphansAreMeaningful(unittest.TestCase):
    """A filename word that half the folder contains does not 'point at' anything."""

    def test_a_common_word_in_the_filename_is_not_a_reference(self):
        docs = [doc(f"note{i}.txt", "We follow the standard procedure here. " + LOREM)
                for i in range(30)]
        docs.append(doc("procedure.txt", "Nobody links to this by name. " + LOREM))
        o = gap.analyze(docs, now=NOW)["orphans"]
        self.assertIn("procedure.txt", [f["rel"] for f in o["unreferenced"]],
                      "'procedure' appears in every file, so it routes nobody to this one")

    def test_a_genuine_mention_by_name_still_counts(self):
        docs = [doc("handbook.txt", "See warranty-terms for the details. " + LOREM),
                doc("warranty-terms.txt", "Parts covered twelve months. " + LOREM)]
        o = gap.analyze(docs, now=NOW)["orphans"]
        self.assertNotIn("warranty-terms.txt", [f["rel"] for f in o["unreferenced"]])

    def test_files_we_cannot_judge_are_reported_apart_not_hidden(self):
        docs = [doc("2024.txt", LOREM), doc("a.txt", LOREM + " b")]
        o = gap.analyze(docs, now=NOW)
        self.assertIn("unjudged_count", o["orphans"])
        self.assertEqual(o["orphans"]["unjudged_count"], 2,
                         "a name with no distinctive word in it cannot be judged either way")

    def test_the_share_is_out_of_what_was_actually_judged(self):
        docs = [doc("2024.txt", LOREM), doc("distinctive-warranty-doc.txt", LOREM + " x")]
        o = gap.analyze(docs, now=NOW)["orphans"]
        self.assertEqual(o["judged_count"] + o["unjudged_count"], 2)
        if o["judged_count"]:
            self.assertEqual(o["unreferenced_share"],
                             round(100.0 * o["unreferenced_count"] / o["judged_count"], 1))


class TestPartialScans(unittest.TestCase):
    def test_a_stopped_scan_is_flagged_in_the_report(self):
        docs = [doc(f"a{i}.txt", LOREM + str(i)) for i in range(5)]
        r = gap.analyze(docs, now=NOW, partial=True)
        self.assertTrue(r["partial"])
        self.assertTrue(any("stopped" in h["text"].lower() or "part" in h["text"].lower()
                            for h in r["headlines"]),
                        "a half-read folder must say so before anything else")
        md = gap.report_markdown("Client", "/tmp", r)
        self.assertIn("stopped", md.lower())

    def test_a_finished_scan_is_not_flagged(self):
        r = gap.analyze([doc("a.txt", LOREM)], now=NOW)
        self.assertFalse(r["partial"])


class TestAuthorNames(unittest.TestCase):
    def test_placeholder_names_are_not_reported_as_a_person(self):
        docs = [doc(f"a{i}.docx", LOREM + str(i), author=name) for i, name in
                enumerate(["Un-named", "user", "Administrator", "Windows User", "unknown"])]
        docs.append(doc("real.docx", LOREM + " z", author="Dana Reyes"))
        k = gap.analyze(docs, now=NOW)["concentration"]
        self.assertEqual(k["top_author"], "Dana Reyes")
        self.assertEqual(k["authors_known"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
