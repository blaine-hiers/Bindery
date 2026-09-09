"""Index tests — tokenising, BM25, the query language, and snippets.

    py tests/test_index.py
"""

from __future__ import annotations

import math
import sys
import tempfile
import unittest
from pathlib import Path

# Tests live in `system/apps/tests/`, mirroring the app tree. Walk up to the
# apps root, then reflect the path back down to the app this file tests.
_TESTS = Path(__file__).resolve().parent
_ROOT = next(p for p in _TESTS.parents if (p / "_shared" / "appkit.py").is_file())
_APP = _ROOT / _TESTS.relative_to(_ROOT / "tests")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_APP))

from index import (SearchIndex, bm25_scores, make_snippet, normalise_for_phrase,  # noqa: E402
                   parse_query, phrase_pattern, stem, tokenize)


class TestTokenizer(unittest.TestCase):
    def test_casefolds_and_drops_punctuation(self):
        self.assertEqual(tokenize("Price, LIST; (final)!"), ["price", "list", "final"])

    def test_stopwords_are_dropped(self):
        self.assertEqual(tokenize("the cost of the job"), ["cost", "job"])

    def test_stopwords_can_be_kept_for_phrases(self):
        self.assertIn("the", tokenize("the cost", keep_stopwords=True))

    def test_numbers_survive(self):
        self.assertEqual(tokenize("Invoice 10425 for $285.00"),
                         ["invoice", "10425", "285.00"])

    def test_unicode_is_kept(self):
        self.assertEqual(tokenize("Müller café Zaär"), ["müller", "café", "zaär"])

    def test_casefold_expands_the_german_sharp_s(self):
        # casefold(), not lower(): "Straße" and "STRASSE" have to meet, or a
        # search for one never finds the other.
        self.assertEqual(tokenize("Straße"), tokenize("STRASSE"))

    def test_empty_and_whitespace(self):
        self.assertEqual(tokenize(""), [])
        self.assertEqual(tokenize("   \n\t  "), [])
        self.assertEqual(tokenize(None), [])

    def test_underscores_split_words(self):
        self.assertEqual(tokenize("price_list_2024"), ["price", "list", "2024"])


class TestStemmer(unittest.TestCase):
    def test_plurals(self):
        self.assertEqual(stem("invoices"), "invoice")
        self.assertEqual(stem("quotes"), "quote")
        self.assertEqual(stem("policies"), "policy")

    def test_double_s_and_us_and_is_are_left_alone(self):
        for word in ("business", "status", "analysis", "process"):
            self.assertEqual(stem(word), word)

    def test_ing_and_ed(self):
        self.assertEqual(stem("running"), "run")
        self.assertEqual(stem("billing"), "bill")      # not "bil"
        self.assertEqual(stem("shipped"), "ship")

    def test_short_words_are_untouched(self):
        for word in ("is", "as", "gas", "bus", "ppe"):
            self.assertEqual(stem(word), word)

    def test_singular_and_plural_meet_in_the_middle(self):
        self.assertEqual(stem("warranty"), stem("warranties"))
        self.assertEqual(stem("invoice"), stem("invoices"))


class TestBm25(unittest.TestCase):
    """Checked against arithmetic done by hand, not against the code.

    Three documents, lengths 10 / 100 / 40, so avgdl = 50.
    The word appears in two of them, so df = 2 and

        idf = ln(1 + (3 - 2 + 0.5) / (2 + 0.5)) = ln(1.6) = 0.4700036…

    d1: tf 3, len 10  -> 3 + 1.5(1 - .75 + .75·10/50)  = 3.6     -> 0.47·3·2.5/3.6   = 0.9792
    d2: tf 6, len 100 -> 6 + 1.5(1 - .75 + .75·100/50) = 8.625   -> 0.47·6·2.5/8.625 = 0.8174
    """

    POSTINGS = {"roof": {"d1": 3, "d2": 6}}
    LENGTHS = {"d1": 10, "d2": 100, "d3": 40}

    def test_matches_hand_arithmetic(self):
        scores = bm25_scores(self.POSTINGS, self.LENGTHS, ["roof"])
        self.assertAlmostEqual(scores["d1"], 0.9792, places=4)
        self.assertAlmostEqual(scores["d2"], 0.8174, places=4)

    def test_idf_is_what_we_said_it_is(self):
        self.assertAlmostEqual(math.log(1.6), 0.4700036292, places=9)

    def test_a_long_document_does_not_win_on_raw_count(self):
        scores = bm25_scores(self.POSTINGS, self.LENGTHS, ["roof"])
        self.assertGreater(scores["d1"], scores["d2"])

    def test_documents_without_the_term_score_nothing(self):
        scores = bm25_scores(self.POSTINGS, self.LENGTHS, ["roof"])
        self.assertNotIn("d3", scores)

    def test_a_rare_word_outranks_a_common_one(self):
        postings = {"common": {"a": 2, "b": 2, "c": 2}, "rare": {"a": 2}}
        lengths = {"a": 50, "b": 50, "c": 50}
        common = bm25_scores(postings, lengths, ["common"])["a"]
        rare = bm25_scores(postings, lengths, ["rare"])["a"]
        self.assertGreater(rare, common)

    def test_scores_add_across_terms(self):
        postings = {"x": {"a": 1}, "y": {"a": 1}}
        lengths = {"a": 20, "b": 20}
        one = bm25_scores(postings, lengths, ["x"])["a"]
        both = bm25_scores(postings, lengths, ["x", "y"])["a"]
        self.assertAlmostEqual(both, one * 2, places=9)

    def test_no_documents_is_not_a_crash(self):
        self.assertEqual(bm25_scores({}, {}, ["anything"]), {})


class TestQueryLanguage(unittest.TestCase):
    def test_plain_words(self):
        q = parse_query("labor rate")
        self.assertEqual(q.terms, ["labor", "rate"])
        self.assertEqual(q.phrases, [])

    def test_phrase_in_quotes(self):
        q = parse_query('"price list" 2024')
        self.assertEqual(q.phrases, ["price list"])
        self.assertIn("2024", q.terms)

    def test_exclusion(self):
        q = parse_query("invoice -draft")
        self.assertEqual(q.terms, ["invoice"])
        self.assertEqual(q.exclude_terms, ["draft"])

    def test_excluded_phrase(self):
        q = parse_query('safety -"do not use"')
        self.assertEqual(q.exclude_phrases, ["do not use"])

    def test_ext_filter(self):
        self.assertEqual(parse_query("ext:pdf safety").exts, [".pdf"])
        self.assertEqual(parse_query("ext:.PDF").exts, [".pdf"])

    def test_folder_filter(self):
        self.assertEqual(parse_query("folder:Contracts renewal").folders, ["contracts"])
        self.assertEqual(parse_query("path:SOPs").folders, ["sops"])

    def test_filters_are_not_treated_as_words(self):
        q = parse_query("ext:pdf folder:sops")
        self.assertEqual(q.terms, [])
        self.assertFalse(q.has_text)
        self.assertFalse(q.is_empty)

    def test_empty_query(self):
        self.assertTrue(parse_query("").is_empty)
        self.assertTrue(parse_query("   ").is_empty)

    def test_normalise_for_phrase(self):
        self.assertEqual(normalise_for_phrase("Price-List, 2024!"), " price list 2024 ")


class TestPhraseMatching(unittest.TestCase):
    def test_words_must_be_next_to_each_other(self):
        rx = phrase_pattern("price list")
        self.assertTrue(rx.search("our 2026 price list is attached"))
        self.assertFalse(rx.search("the list of every price we charge"))

    def test_punctuation_and_line_breaks_between_the_words_are_fine(self):
        rx = phrase_pattern("price list")
        for text in ("Price-List", "PRICE   LIST", "price\nlist", "price_list", "Price, List"):
            self.assertTrue(rx.search(text), text)

    def test_it_does_not_match_inside_a_longer_word(self):
        self.assertFalse(phrase_pattern("price list").search("priced listing"))
        self.assertTrue(phrase_pattern("price list").search("a price list."))

    def test_regex_metacharacters_in_a_phrase_are_safe(self):
        rx = phrase_pattern("C++ (draft)")
        self.assertIsNotNone(rx)
        self.assertTrue(rx.search("we ship C++ draft code"))

    def test_an_empty_phrase_has_no_pattern(self):
        self.assertIsNone(phrase_pattern(""))
        self.assertIsNone(phrase_pattern("   !!!  "))


class TestSnippets(unittest.TestCase):
    TEXT = ("Section 4. The standard labor rate is ninety five dollars per hour during "
            "normal business hours. Emergency after hours calls are billed at time and "
            "a half. The price list is reviewed every January.")

    def test_the_term_appears_in_the_snippet(self):
        s = make_snippet(self.TEXT, ["emergency"])
        self.assertIn("Emergency", s["text"])

    def test_context_surrounds_the_term(self):
        s = make_snippet(self.TEXT, ["emergency"])
        self.assertIn("billed at time", s["text"])

    def test_hits_are_marked(self):
        s = make_snippet(self.TEXT, ["labor"])
        marked = [p["t"].lower() for p in s["parts"] if p["hit"]]
        self.assertIn("labor", marked)

    def test_parts_rebuild_the_snippet_exactly(self):
        s = make_snippet(self.TEXT, ["price"])
        self.assertEqual("".join(p["t"] for p in s["parts"]), s["text"])

    def test_prefix_matching_marks_the_whole_word(self):
        s = make_snippet(self.TEXT, ["invoic"] )
        self.assertEqual(s["parts"][0]["hit"], False)   # no match -> head of document
        s2 = make_snippet("Invoices go out on Friday and invoicing is done weekly.",
                          ["invoic"])
        marked = [p["t"] for p in s2["parts"] if p["hit"]]
        self.assertIn("Invoices", marked)
        self.assertIn("invoicing", marked)

    def test_regex_metacharacters_in_the_query_do_not_break_it(self):
        text = "We use C++ for the controller and a(b notation in the drawings."
        for term in ("C++", "a(b", "*", "[", "\\", "(", "?"):
            s = make_snippet(text, [term])
            self.assertIsInstance(s["text"], str)
        s = make_snippet(text, ["C++"])
        self.assertIn("C++", [p["t"] for p in s["parts"] if p["hit"]])

    def test_phrase_is_highlighted_as_one(self):
        s = make_snippet(self.TEXT, [], ["price list"])
        self.assertIn("price list", [p["t"] for p in s["parts"] if p["hit"]])

    def test_empty_document(self):
        self.assertEqual(make_snippet("", ["anything"])["text"], "")
        self.assertEqual(make_snippet("   \n ", ["anything"])["parts"], [])

    def test_no_match_falls_back_to_the_opening_words(self):
        s = make_snippet(self.TEXT, ["helicopter"])
        self.assertTrue(s["text"].startswith("Section 4."))
        self.assertFalse(any(p["hit"] for p in s["parts"]))

    def test_the_densest_window_wins(self):
        text = ("roof " + "filler " * 120 + "roof roof roof " + "filler " * 120)
        s = make_snippet(text, ["roof"], width=120)
        self.assertGreaterEqual(len([p for p in s["parts"] if p["hit"]]), 3)


class TestSearchIndex(unittest.TestCase):
    DOCS = [
        {"rel": "SOPs/price-list.txt", "name": "price-list.txt", "folder": "SOPs",
         "ext": ".txt", "size": 100, "mtime": 1700000000, "status": "ok",
         "text": "Price list. Condenser fan motor 285.00. Capacitor 42.50. "
                 "Prices reviewed every January."},
        {"rel": "SOPs/safety-manual.pdf", "name": "safety-manual.pdf", "folder": "SOPs",
         "ext": ".pdf", "size": 900, "mtime": 1600000000, "status": "ok",
         "text": "Safety manual. " + ("Wear personal protective equipment on every job. " * 40)
                 + " The price of failure is high."},
        {"rel": "Contracts/vendor-agreement.docx", "name": "vendor-agreement.docx",
         "folder": "Contracts", "ext": ".docx", "size": 400, "mtime": 1500000000,
         "status": "ok",
         "text": "Vendor agreement. Payment terms are net thirty. Draft copy, not signed."},
        {"rel": "scan.pdf", "name": "scan.pdf", "folder": "", "ext": ".pdf",
         "size": 50000, "mtime": 1400000000, "status": "unreadable",
         "reason": "no text inside — this is a scan", "text": ""},
    ]

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.opened = []
        cls.ix = cls.open("search.db")
        cls.ix.add("kb1", cls.DOCS)

    @classmethod
    def open(cls, name):
        ix = SearchIndex(Path(cls.tmp.name) / name)
        cls.opened.append(ix)
        return ix

    @classmethod
    def tearDownClass(cls):
        for ix in cls.opened:
            ix.close()
        cls.tmp.cleanup()

    def test_everything_was_indexed(self):
        self.assertEqual(self.ix.doc_count("kb1"), 4)

    def test_a_short_relevant_document_beats_a_long_one(self):
        r = self.ix.search("kb1", "price")
        self.assertEqual(r["results"][0]["name"], "price-list.txt")

    def test_results_carry_a_snippet_with_the_word_in_it(self):
        r = self.ix.search("kb1", "capacitor")
        self.assertIn("Capacitor", r["results"][0]["snippet"])
        self.assertTrue(any(p["hit"] for p in r["results"][0]["parts"]))

    def test_phrase_search_actually_filters(self):
        loose = self.ix.search("kb1", "protective equipment")
        exact = self.ix.search("kb1", '"protective equipment"')
        nonsense = self.ix.search("kb1", '"equipment protective"')
        self.assertGreaterEqual(loose["total"], 1)
        self.assertEqual(exact["total"], 1)
        self.assertEqual(nonsense["total"], 0)

    def test_exclusion_actually_removes(self):
        with_draft = self.ix.search("kb1", "agreement")
        without = self.ix.search("kb1", "agreement -draft")
        self.assertEqual(with_draft["total"], 1)
        self.assertEqual(without["total"], 0)

    def test_ext_filter_actually_filters(self):
        r = self.ix.search("kb1", "price ext:pdf")
        self.assertTrue(r["results"])
        self.assertTrue(all(x["ext"] == ".pdf" for x in r["results"]))

    def test_folder_filter_actually_filters(self):
        r = self.ix.search("kb1", "folder:contracts")
        self.assertEqual(r["total"], 1)
        self.assertEqual(r["results"][0]["folder"], "Contracts")

    def test_a_capped_phrase_search_says_so(self):
        r = self.ix.search("kb1", '"protective equipment"')
        self.assertFalse(r["at_least"], "a small folder is never capped")

    def test_filter_only_query_browses(self):
        r = self.ix.search("kb1", "ext:pdf")
        self.assertEqual(r["total"], 2)

    def test_filename_words_are_searchable(self):
        r = self.ix.search("kb1", "vendor")
        self.assertEqual(r["results"][0]["name"], "vendor-agreement.docx")

    def test_stemming_finds_the_other_form(self):
        self.assertGreaterEqual(self.ix.search("kb1", "prices")["total"], 1)

    def test_nothing_matches_is_an_empty_list_not_an_error(self):
        r = self.ix.search("kb1", "helicopter")
        self.assertEqual(r["total"], 0)
        self.assertEqual(r["results"], [])

    def test_unknown_kb_gives_an_explanation(self):
        r = self.ix.search("nope", "anything")
        self.assertEqual(r["total"], 0)
        self.assertIn("empty_reason", r)

    def test_fingerprints_round_trip(self):
        fp = self.ix.fingerprints("kb1")
        self.assertEqual(fp["SOPs/price-list.txt"], (100, 1700000000))
        self.assertEqual(len(fp), 4)

    def test_stats(self):
        st = self.ix.stats("kb1")
        self.assertEqual(st["total"], 4)
        self.assertEqual(st["ok"], 3)
        self.assertEqual(st["unreadable"], 1)

    def test_re_adding_a_document_does_not_double_count(self):
        ix = self.open("again.db")
        ix.add("k", [self.DOCS[0]])
        ix.add("k", [self.DOCS[0]])
        self.assertEqual(ix.doc_count("k"), 1)
        r = ix.search("k", "capacitor")
        self.assertEqual(r["total"], 1)

    def test_keep_only_drops_the_rest(self):
        ix = self.open("keep.db")
        ix.add("k", self.DOCS)
        gone = ix.keep_only("k", ["SOPs/price-list.txt"])
        self.assertEqual(gone, 3)
        self.assertEqual(ix.doc_count("k"), 1)

    def test_dropping_a_kb_leaves_the_others_alone(self):
        ix = self.open("drop.db")
        ix.add("a", self.DOCS)
        ix.add("b", self.DOCS[:1])
        ix.drop_kb("a")
        self.assertEqual(ix.doc_count("a"), 0)
        self.assertEqual(ix.doc_count("b"), 1)

    def test_search_is_fast_on_a_realistic_pile(self):
        import random
        import time as _t
        words = ["invoice", "warranty", "condenser", "safety", "vendor", "schedule",
                 "permit", "install", "service", "customer", "refrigerant", "duct"]
        rnd = random.Random(7)
        docs = [{"rel": f"f{i}/doc{i}.txt", "name": f"doc{i}.txt", "folder": f"f{i}",
                 "ext": ".txt", "size": 1000, "mtime": 1600000000 + i, "status": "ok",
                 "text": " ".join(rnd.choice(words) for _ in range(300))}
                for i in range(2000)]
        ix = self.open("big.db")
        ix.add("big", docs)
        ix.search("big", "warranty")                       # warm the metadata cache
        started = _t.perf_counter()
        for _ in range(5):
            r = ix.search("big", "condenser warranty")
        each = (_t.perf_counter() - started) / 5 * 1000
        self.assertGreater(r["total"], 0)
        self.assertLess(each, 300, f"search took {each:.0f} ms on 2,000 documents")


# ---------------------------------------------------------------- adversarial

class TestQueryTorture(unittest.TestCase):
    """None of these may raise, and none may hang."""

    BAD = ['"foo', '""', '-foo', 'ext:', 'ext:.', 'folder:', 'the and of it', 'C++',
           'a(b', '.*', '[', '\\', '(((', '"" ""', '-"', '"a" -"a"', 'price -price',
           '   ', '\x00', '?' * 200, '"' * 50, '-' * 50, 'a' * 3000, '\\p{L}+',
           '(?:', '(a|b)*', '{2,3}', '$^', 'ext:pdf ext:docx', 'folder:a folder:b']

    def test_nothing_in_the_box_can_raise(self):
        for q in self.BAD:
            with self.subTest(q=q[:20]):
                query = parse_query(q)
                make_snippet("a price list for capacitors and condensers " * 20,
                             query.words, query.phrases)

    def test_regex_metacharacters_are_escaped_not_run(self):
        s = make_snippet("The C++ style guide and the a(b notation are both here.",
                         ["C++", "a(b"], [])
        self.assertTrue(any(p["hit"] and p["t"] == "C++" for p in s["parts"]))
        self.assertTrue(any(p["hit"] and p["t"] == "a(b" for p in s["parts"]))

    def test_a_ten_thousand_character_query_is_still_fast(self):
        import random
        import time as _t
        rnd = random.Random(3)
        q = " ".join("".join(rnd.choice("abcdefghij") for _ in range(7)) for _ in range(1300))
        self.assertGreater(len(q), 10000)
        query = parse_query(q)
        text = "a price list for capacitors and condensers with warranty terms " * 400
        started = _t.perf_counter()
        for _ in range(40):                       # one page of results
            make_snippet(text, query.words, query.phrases)
        each = (_t.perf_counter() - started) / 40 * 1000
        self.assertLess(each, 25,
                        f"{each:.0f} ms per snippet — the query pattern is being "
                        f"rebuilt for every result")


class TestSnippetsCannotInjectMarkup(unittest.TestCase):
    EVIL = ('Our price list <script>alert(1)</script> and <img src=x onerror=alert(2)> '
            'plus "quotes" & ampersands and </span> closers.')

    def test_every_part_is_plain_text_only(self):
        for words, phrases in ((["price"], []), (["<script>"], []), ([], ["price list"]),
                               (["</span>"], []), (["&"], [])):
            s = make_snippet(self.EVIL, words, phrases)
            for p in s["parts"]:
                self.assertEqual(set(p), {"t", "hit"})
                self.assertIsInstance(p["t"], str)
                self.assertIsInstance(p["hit"], bool)

    def test_the_parts_rebuild_the_window_exactly(self):
        """If parts ever lost or duplicated text the page would show something the
        document does not say."""
        for words in (["price"], ["list"], ["zzz"], []):
            s = make_snippet(self.EVIL, words, [])
            self.assertEqual("".join(p["t"] for p in s["parts"]), s["text"])

    def test_no_search_result_can_return_html_instead_of_parts(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        ix = SearchIndex(Path(tmp.name) / "evil.db")
        try:
            ix.add("kb", [{"rel": "evil.txt", "name": "evil.txt", "folder": "", "ext": ".txt",
                           "size": 10, "mtime": 1, "status": "ok", "text": self.EVIL}])
            for q in ("price", '"price list"', "list -nothing", "ext:txt"):
                r = ix.search("kb", q)
                for hit in r["results"]:
                    self.assertIsInstance(hit["parts"], list)
                    for p in hit["parts"]:
                        self.assertEqual(set(p), {"t", "hit"})
        finally:
            ix.close()
            tmp.cleanup()


class TestEmptyQueryReasons(unittest.TestCase):
    """'Nothing matched — try fewer words' is the wrong advice when the problem
    is that every word was a stopword."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.ix = SearchIndex(Path(cls.tmp.name) / "reasons.db")
        cls.ix.add("kb", [{"rel": "a.txt", "name": "a.txt", "folder": "", "ext": ".txt",
                           "size": 10, "mtime": 1, "status": "ok",
                           "text": "the price list for condensers"}])

    @classmethod
    def tearDownClass(cls):
        cls.ix.close()
        cls.tmp.cleanup()

    def test_all_stopwords_says_so(self):
        r = self.ix.search("kb", "the and of it")
        self.assertEqual(r["total"], 0)
        self.assertIn("common", r.get("empty_reason", "").lower())

    def test_only_exclusions_says_so(self):
        r = self.ix.search("kb", "-draft")
        self.assertEqual(r["total"], 0)
        self.assertIn("only", r.get("empty_reason", "").lower())

    def test_an_untyped_box_gets_no_scolding(self):
        r = self.ix.search("kb", "")
        self.assertEqual(r["total"], 0)
        self.assertFalse(r.get("empty_reason"))

    def test_a_real_query_has_no_reason_attached(self):
        r = self.ix.search("kb", "price")
        self.assertTrue(r["results"])
        self.assertFalse(r.get("empty_reason"))


class TestBm25ByHand(unittest.TestCase):
    def test_three_documents_worked_out_on_paper(self):
        postings = {"price": {"A": 3, "B": 1}}
        lengths = {"A": 100, "B": 50, "C": 150}
        got = bm25_scores(postings, lengths, ["price"])
        n_docs, df, avgdl = 3, 2, 100.0
        idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
        for name, tf, dl in (("A", 3, 100), ("B", 1, 50)):
            want = idf * tf * 2.5 / (tf + 1.5 * (1 - 0.75 + 0.75 * dl / avgdl))
            self.assertAlmostEqual(got[name], want, places=10)
        self.assertNotIn("C", got)

    def test_a_word_in_every_document_still_scores_above_zero(self):
        postings = {"the": {"A": 2, "B": 2, "C": 2}}
        lengths = {"A": 10, "B": 10, "C": 10}
        got = bm25_scores(postings, lengths, ["the"])
        self.assertEqual(len(got), 3)
        for v in got.values():
            self.assertGreater(v, 0, "a negative weight would rank matches below non-matches")


if __name__ == "__main__":
    unittest.main(verbosity=2)
