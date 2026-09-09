"""index — tokenising, the inverted index, BM25 ranking, and snippets.

Standard library only. The index lives in its own sqlite file so that search is
instant on the second run without re-reading a single client document.

Why BM25 and not a word count: an owner types "price list". A 300-page manual
that says "price" 40 times is not a better answer than a two-page price list.
BM25 divides a term's weight by how long the document is, so short documents
that are genuinely about the thing win. That is the whole trick, and it is the
difference between a demo that lands and one that does not.
"""

from __future__ import annotations

import math
import re
import sqlite3
import threading
from pathlib import Path

__all__ = [
    "SearchIndex", "tokenize", "stem", "parse_query", "Query",
    "bm25_scores", "make_snippet", "normalise_for_phrase", "STOPWORDS",
    "K1", "B",
]

K1 = 1.5
B = 0.75

# How many of the best-scoring documents a quoted phrase is verified against.
# Nothing below this rank would ever be shown on a page of 40 results.
PHRASE_CANDIDATE_CAP = 1200

# Deliberately small. A big stopword list starts eating real query words
# ("no", "not", "over") and the owner never finds out why nothing matched.
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from",
    "had", "has", "have", "he", "her", "his", "i", "if", "in", "into", "is",
    "it", "its", "of", "on", "or", "our", "she", "so", "than", "that", "the",
    "their", "them", "then", "there", "these", "they", "this", "to", "was",
    "we", "were", "what", "when", "which", "will", "with", "would", "you",
    "your",
}

_WORD = re.compile(r"[^\W_]+(?:[.'][^\W_]+)*", re.UNICODE)
# "running" -> "runn" -> "run". Note 'l' is deliberately absent: "billing" -> "bill"
# is a real word and "bil" is not.
_DOUBLE_END = "bdgmnprt"


def stem(word: str) -> str:
    """Plurals, -ing and -ed. Nothing more.

    A full Porter stemmer collapses words an owner would swear are different
    ("operate"/"operations"/"operator" all become "oper") and then the search
    box looks broken. Three rules is the honest amount of stemming for this.
    """
    w = word
    if len(w) <= 3:
        return w
    if w.endswith("sses"):
        return w[:-2]
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith("s") and not w.endswith(("ss", "us", "is", "as")):
        w = w[:-1]
        if len(w) <= 3:
            return w
    if w.endswith("ing") and len(w) > 5:
        w = w[:-3]
    elif w.endswith("ed") and len(w) > 4:
        w = w[:-2]
    else:
        return w
    if len(w) > 2 and w[-1] == w[-2] and w[-1] in _DOUBLE_END:
        w = w[:-1]
    return w


def tokenize(text: str, keep_stopwords: bool = False) -> list[str]:
    """Text to searchable terms: casefolded, punctuation gone, lightly stemmed."""
    if not text:
        return []
    out = []
    for raw in _WORD.findall(text.casefold()):
        raw = raw.strip(".'")
        if not raw:
            continue
        if not keep_stopwords and raw in STOPWORDS:
            continue
        out.append(stem(raw))
    return out


def normalise_for_phrase(text: str) -> str:
    """Flatten to ' word word word '. Kept for readability in tests and debugging."""
    return " " + re.sub(r"[^\w]+", " ", (text or "").casefold()).strip() + " "


_PHRASE_CACHE: dict[str, "re.Pattern | None"] = {}


def phrase_pattern(phrase: str):
    """A compiled regex that finds `phrase` in raw document text.

    Checking a phrase this way scans the text once in C, rather than rewriting
    every document into a normalised copy first. On a folder where thousands of
    documents contain the individual words, that is the difference between a
    search that feels instant and one that does not.
    """
    if phrase in _PHRASE_CACHE:
        return _PHRASE_CACHE[phrase]
    words = _WORD.findall(phrase or "")
    rx = None
    if words:
        body = r"[\W_]+".join(re.escape(w) for w in words)
        rx = re.compile(r"(?<![^\W_])" + body + r"(?![^\W_])", re.I | re.UNICODE)
    if len(_PHRASE_CACHE) > 500:
        _PHRASE_CACHE.clear()
    _PHRASE_CACHE[phrase] = rx
    return rx


# ---------------------------------------------------------------- query language

class Query:
    """What the user typed, pulled apart.

      price list            -> two terms
      "price list"          -> a phrase; the words must be next to each other
      -draft                -> exclude anything containing 'draft'
      ext:pdf               -> only PDFs
      folder:invoices       -> only inside a folder whose path says 'invoices'
    """

    def __init__(self):
        self.raw = ""
        self.words: list[str] = []          # raw words, for highlighting
        self.terms: list[str] = []          # stemmed, for scoring
        self.phrases: list[str] = []
        self.exclude_terms: list[str] = []
        self.exclude_phrases: list[str] = []
        self.exts: list[str] = []
        self.folders: list[str] = []

    @property
    def is_empty(self) -> bool:
        return not (self.terms or self.phrases or self.exts or self.folders)

    @property
    def has_text(self) -> bool:
        return bool(self.terms or self.phrases)

    def __repr__(self):                                          # pragma: no cover
        return (f"Query(terms={self.terms}, phrases={self.phrases}, "
                f"exclude={self.exclude_terms}, ext={self.exts}, folder={self.folders})")


_TOKEN_RX = re.compile(r'(-?)(?:"([^"]*)"|(\S+))')


def parse_query(text: str) -> Query:
    q = Query()
    q.raw = text or ""
    for neg, quoted, bare in _TOKEN_RX.findall(q.raw):
        negated = neg == "-"
        if quoted is not None and quoted != "":
            phrase = quoted.strip()
            if not phrase:
                continue
            (q.exclude_phrases if negated else q.phrases).append(phrase)
            if not negated:
                q.words.extend(_WORD.findall(phrase))
                q.terms.extend(tokenize(phrase, keep_stopwords=True))
            else:
                q.exclude_terms.extend(tokenize(phrase, keep_stopwords=True))
            continue
        word = (bare or "").strip()
        if not word:
            continue
        low = word.casefold()
        if low.startswith("ext:"):
            value = low[4:].lstrip(".")
            if value:
                q.exts.append("." + value)
            continue
        if low.startswith(("folder:", "path:", "dir:")):
            value = word.split(":", 1)[1].strip().strip('"')
            if value:
                q.folders.append(value.casefold())
            continue
        toks = tokenize(word, keep_stopwords=negated)
        if negated:
            q.exclude_terms.extend(toks)
        else:
            q.terms.extend(toks)
            q.words.append(word)
    # de-dupe, keep order
    q.terms = list(dict.fromkeys(q.terms))
    q.exclude_terms = list(dict.fromkeys(q.exclude_terms))
    return q


# ---------------------------------------------------------------- ranking

def bm25_scores(postings: dict[str, dict[str, int]], doc_lengths: dict[str, int],
                terms, k1: float = K1, b: float = B) -> dict[str, float]:
    """Textbook Okapi BM25. Pure function so it can be checked by hand.

        idf(t)   = ln(1 + (N - df + 0.5) / (df + 0.5))
        score   += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl / avgdl))

    `postings` is {term: {doc_id: term_frequency}}, `doc_lengths` is
    {doc_id: how many words that document has}.
    """
    n_docs = len(doc_lengths)
    if not n_docs:
        return {}
    avgdl = sum(doc_lengths.values()) / n_docs or 1.0
    scores: dict[str, float] = {}
    for term in terms:
        docs = postings.get(term)
        if not docs:
            continue
        df = len(docs)
        idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
        for doc_id, tf in docs.items():
            dl = doc_lengths.get(doc_id, 0) or 1
            denom = tf + k1 * (1 - b + b * dl / avgdl)
            scores[doc_id] = scores.get(doc_id, 0.0) + idf * tf * (k1 + 1) / denom
    return scores


# ---------------------------------------------------------------- snippets

# The most query words we will try to light up in a snippet. Somebody pasting a
# whole paragraph into the box used to build a 20 KB regex and then recompile it
# once per result — forty times a page, before anything appeared on screen.
MAX_HIGHLIGHT_TERMS = 40

_PATTERN_CACHE: dict[tuple, "re.Pattern | None"] = {}


def _term_pattern(words, phrases) -> re.Pattern | None:
    """One regex that finds every query word in the raw text.

    `re.escape` everywhere, so a query of `C++` or `a(b` highlights instead of
    raising. Word-prefix matching (`invoic` finds `invoices`) uses a lookbehind
    rather than \\b, because \\b does the wrong thing next to punctuation.

    Compiled once and cached: one search paints forty snippets from the same
    query, and building this each time is most of what a long query costs.
    """
    key = (tuple(words or ()), tuple(phrases or ()))
    if key in _PATTERN_CACHE:
        return _PATTERN_CACHE[key]
    parts = []
    for phrase in phrases:
        bits = _WORD.findall(phrase)
        if bits:
            parts.append(r"\s+".join(re.escape(b) for b in bits))
    for word in words:
        word = (word or "").strip()
        if not word:
            continue
        parts.append(re.escape(word) + (r"\w*" if word[-1:].isalnum() else ""))
    parts.sort(key=len, reverse=True)
    parts = parts[:MAX_HIGHLIGHT_TERMS]
    rx = None
    if parts:
        try:
            rx = re.compile(r"(?<!\w)(?:" + "|".join(parts) + ")", re.I)
        except re.error:                                         # pragma: no cover
            rx = None
    if len(_PATTERN_CACHE) > 200:
        _PATTERN_CACHE.clear()
    _PATTERN_CACHE[key] = rx
    return rx


def make_snippet(text: str, words, phrases=(), width: int = 260) -> dict:
    """A real snippet: the densest window of matches, with the hits marked.

    Returns {"text": plain string, "parts": [{"t":..., "hit": bool}, ...]}.
    The frontend builds DOM nodes from `parts`, so nothing in a client document
    can inject markup into the page.
    """
    text = text or ""
    rx = _term_pattern(words, phrases)
    if not text.strip():
        return {"text": "", "parts": []}
    hits = list(rx.finditer(text)) if rx else []
    if not hits:
        head = text[:width].strip()
        return {"text": head + ("…" if len(text) > width else ""),
                "parts": [{"t": head, "hit": False}] if head else []}

    # Slide a window over the hits and keep the one covering the most.
    best_i, best_n = 0, 0
    for i, h in enumerate(hits):
        n = 0
        for j in range(i, len(hits)):
            if hits[j].start() - h.start() > width:
                break
            n += 1
        if n > best_n:
            best_i, best_n = i, n

    centre = hits[best_i].start()
    start = max(0, centre - width // 4)
    end = min(len(text), start + width)
    if start > 0:
        space = text.find(" ", start, start + 30)
        if space != -1:
            start = space + 1
    if end < len(text):
        space = text.rfind(" ", end - 30, end)
        if space != -1:
            end = space

    window = text[start:end].replace("\n", " ").strip()
    parts = []
    cursor = 0
    for m in (rx.finditer(window) if rx else []):
        if m.start() > cursor:
            parts.append({"t": window[cursor:m.start()], "hit": False})
        parts.append({"t": m.group(0), "hit": True})
        cursor = m.end()
    if cursor < len(window):
        parts.append({"t": window[cursor:], "hit": False})

    plain = ("… " if start > 0 else "") + window + (" …" if end < len(text) else "")
    if start > 0:
        parts.insert(0, {"t": "… ", "hit": False})
    if end < len(text):
        parts.append({"t": " …", "hit": False})
    return {"text": plain, "parts": parts}


# ---------------------------------------------------------------- the store

_SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    kb_id   TEXT NOT NULL,
    doc_id  TEXT NOT NULL,
    rel     TEXT NOT NULL,
    path    TEXT NOT NULL,
    name    TEXT NOT NULL,
    folder  TEXT NOT NULL,
    ext     TEXT NOT NULL,
    size    INTEGER NOT NULL,
    mtime   INTEGER NOT NULL,
    status  TEXT NOT NULL,
    reason  TEXT,
    author  TEXT,
    chash   TEXT,
    nwords  INTEGER NOT NULL DEFAULT 0,
    ntok    INTEGER NOT NULL DEFAULT 0,
    text    TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (kb_id, doc_id)
);
CREATE INDEX IF NOT EXISTS docs_kb ON docs(kb_id);
CREATE TABLE IF NOT EXISTS postings (
    kb_id  TEXT NOT NULL,
    term   TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    tf     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS postings_lookup ON postings(kb_id, term);
CREATE INDEX IF NOT EXISTS postings_doc ON postings(kb_id, doc_id);
"""


def doc_id_for(rel: str) -> str:
    import hashlib
    return hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16]


class SearchIndex:
    """Durable inverted index. One sqlite file, many knowledge bases."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(_SCHEMA)
            self._db.commit()
        self._meta_cache: dict[str, dict] = {}

    # ---- writing -------------------------------------------------------

    def add(self, kb_id: str, records) -> int:
        """Insert or replace a batch of ingest records. Returns how many."""
        rows, posting_rows, doc_ids = [], [], []
        for rec in records:
            did = doc_id_for(rec["rel"])
            doc_ids.append(did)
            text = rec.get("text") or ""
            counts: dict[str, int] = {}
            for tok in tokenize(text):
                counts[tok] = counts.get(tok, 0) + 1
            # The filename is part of the document as far as search is concerned.
            for tok in tokenize(rec.get("rel", "")):
                counts[tok] = counts.get(tok, 0) + 2
            ntok = sum(counts.values())
            rows.append((kb_id, did, rec["rel"], rec.get("path", ""), rec.get("name", ""),
                         rec.get("folder", ""), rec.get("ext", ""), int(rec.get("size") or 0),
                         int(rec.get("mtime") or 0), rec.get("status", "ok"),
                         rec.get("reason"), rec.get("author"), rec.get("chash"),
                         int(rec.get("nwords") or 0), ntok, text))
            posting_rows.extend((kb_id, term, did, tf) for term, tf in counts.items())
        if not rows:
            return 0
        with self._lock:
            self._db.executemany(
                "DELETE FROM postings WHERE kb_id=? AND doc_id=?",
                [(kb_id, d) for d in doc_ids])
            self._db.executemany(
                "INSERT INTO docs(kb_id,doc_id,rel,path,name,folder,ext,size,mtime,"
                "status,reason,author,chash,nwords,ntok,text) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(kb_id,doc_id) DO UPDATE SET "
                "rel=excluded.rel, path=excluded.path, name=excluded.name, "
                "folder=excluded.folder, ext=excluded.ext, size=excluded.size, "
                "mtime=excluded.mtime, status=excluded.status, reason=excluded.reason, "
                "author=excluded.author, chash=excluded.chash, nwords=excluded.nwords, "
                "ntok=excluded.ntok, text=excluded.text", rows)
            self._db.executemany(
                "INSERT INTO postings(kb_id,term,doc_id,tf) VALUES(?,?,?,?)", posting_rows)
            self._db.commit()
        self._meta_cache.pop(kb_id, None)
        return len(rows)

    def keep_only(self, kb_id: str, rels) -> int:
        """Drop documents that are no longer in the folder. Returns how many went."""
        keep = {doc_id_for(r) for r in rels}
        with self._lock:
            have = [r["doc_id"] for r in self._db.execute(
                "SELECT doc_id FROM docs WHERE kb_id=?", (kb_id,))]
            gone = [d for d in have if d not in keep]
            if gone:
                self._db.executemany("DELETE FROM docs WHERE kb_id=? AND doc_id=?",
                                     [(kb_id, d) for d in gone])
                self._db.executemany("DELETE FROM postings WHERE kb_id=? AND doc_id=?",
                                     [(kb_id, d) for d in gone])
                self._db.commit()
        self._meta_cache.pop(kb_id, None)
        return len(gone)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def drop_kb(self, kb_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM docs WHERE kb_id=?", (kb_id,))
            self._db.execute("DELETE FROM postings WHERE kb_id=?", (kb_id,))
            self._db.commit()
        self._meta_cache.pop(kb_id, None)

    # ---- reading -------------------------------------------------------

    def fingerprints(self, kb_id: str) -> dict[str, tuple[int, int]]:
        """{relative path: (size, mtime)} — what makes re-scanning cheap."""
        with self._lock:
            rows = self._db.execute(
                "SELECT rel, size, mtime FROM docs WHERE kb_id=?", (kb_id,)).fetchall()
        return {r["rel"]: (r["size"], r["mtime"]) for r in rows}

    def _meta(self, kb_id: str) -> dict[str, dict]:
        cached = self._meta_cache.get(kb_id)
        if cached is not None:
            return cached
        with self._lock:
            rows = self._db.execute(
                "SELECT doc_id,rel,path,name,folder,ext,size,mtime,status,reason,"
                "author,chash,nwords,ntok FROM docs WHERE kb_id=?", (kb_id,)).fetchall()
        meta = {r["doc_id"]: dict(r) for r in rows}
        self._meta_cache[kb_id] = meta
        return meta

    def all_docs(self, kb_id: str) -> list[dict]:
        return list(self._meta(kb_id).values())

    def doc_count(self, kb_id: str) -> int:
        with self._lock:
            row = self._db.execute("SELECT COUNT(*) n FROM docs WHERE kb_id=?",
                                   (kb_id,)).fetchone()
        return int(row["n"])

    def get_doc(self, kb_id: str, doc_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM docs WHERE kb_id=? AND doc_id=?",
                                   (kb_id, doc_id)).fetchone()
        return dict(row) if row else None

    def docs_with_text(self, kb_id: str) -> list[dict]:
        """Everything, text included. Used by the gap report."""
        with self._lock:
            rows = self._db.execute("SELECT * FROM docs WHERE kb_id=?", (kb_id,)).fetchall()
        return [dict(r) for r in rows]

    def stats(self, kb_id: str) -> dict:
        with self._lock:
            rows = self._db.execute(
                "SELECT status, COUNT(*) n, SUM(size) bytes, SUM(nwords) words "
                "FROM docs WHERE kb_id=? GROUP BY status", (kb_id,)).fetchall()
        out = {"total": 0, "ok": 0, "unreadable": 0, "skipped": 0, "words": 0, "bytes": 0}
        for r in rows:
            out[r["status"]] = r["n"]
            out["total"] += r["n"]
            out["words"] += int(r["words"] or 0)
            out["bytes"] += int(r["bytes"] or 0)
        return out

    def _postings_for(self, kb_id: str, term: str) -> dict[str, int]:
        with self._lock:
            rows = self._db.execute(
                "SELECT doc_id, tf FROM postings WHERE kb_id=? AND term=?",
                (kb_id, term)).fetchall()
        return {r["doc_id"]: r["tf"] for r in rows}

    # ---- search --------------------------------------------------------

    def search(self, kb_id: str, text: str, limit: int = 40, offset: int = 0,
               snippets: bool = True) -> dict:
        q = parse_query(text)
        if q.is_empty:
            # An empty box is not "show me everything" — it is "I have not asked
            # yet". But a box with words in it that came to nothing needs to say
            # why, or the GUI tells the owner to "try fewer words" when the real
            # answer is that every word they typed is in every document.
            return {"query": text, "total": 0, "results": [], "parsed": _parsed(q),
                    "empty_reason": _why_nothing(q)}
        meta = self._meta(kb_id)
        if not meta:
            return {"query": text, "total": 0, "results": [], "parsed": _parsed(q),
                    "empty_reason": "Nothing has been read into this knowledge base yet."}

        doc_lengths = {d: m["ntok"] or 1 for d, m in meta.items()}

        if q.has_text:
            postings = {t: self._postings_for(kb_id, t) for t in q.terms}
            scores = bm25_scores(postings, doc_lengths, q.terms)
            for term in q.exclude_terms:
                for doc_id in self._postings_for(kb_id, term):
                    scores.pop(doc_id, None)
        else:
            # Filter-only query ("ext:pdf") — browse mode, newest first.
            scores = {d: 0.0 for d in meta}

        # Filters
        if q.exts:
            wanted = {e.lower() for e in q.exts}
            scores = {d: s for d, s in scores.items() if meta[d]["ext"].lower() in wanted}
        if q.folders:
            scores = {d: s for d, s in scores.items()
                      if any(f in meta[d]["rel"].casefold() for f in q.folders)}

        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], meta[kv[0]]["rel"]))
        if not q.has_text:
            ranked = sorted(scores.items(), key=lambda kv: -meta[kv[0]]["mtime"])

        # Phrases and exclusion-phrases need the real text, so they are checked
        # after ranking, on the best candidates only. On a folder where every
        # document contains the individual words, checking all of them would cost
        # a second; the cut-off keeps search instant and is reported honestly.
        capped = False
        if q.phrases or q.exclude_phrases:
            before = len(ranked)
            ranked = self._filter_phrases(kb_id, ranked, q)
            capped = before > PHRASE_CANDIDATE_CAP

        # A filename match is what a person means most of the time.
        if q.has_text and q.words:
            wanted = set(q.terms)
            boosted = []
            for doc_id, score in ranked:
                name_terms = set(tokenize(meta[doc_id]["name"], keep_stopwords=True))
                if wanted and wanted <= name_terms:
                    score *= 1.6
                elif wanted & name_terms:
                    score *= 1.15
                boosted.append((doc_id, score))
            ranked = sorted(boosted, key=lambda kv: (-kv[1], meta[kv[0]]["rel"]))

        total = len(ranked)
        page = ranked[offset:offset + limit]
        want_text = {d for d, _ in page}
        texts = self._texts(kb_id, want_text) if (snippets and want_text) else {}

        results = []
        for doc_id, score in page:
            m = meta[doc_id]
            snip = (make_snippet(texts.get(doc_id, ""), q.words, q.phrases)
                    if snippets else {"text": "", "parts": []})
            results.append({
                "doc_id": doc_id, "rel": m["rel"], "path": m["path"], "name": m["name"],
                "folder": m["folder"], "ext": m["ext"], "size": m["size"],
                "mtime": m["mtime"], "status": m["status"], "reason": m["reason"],
                "nwords": m["nwords"], "score": round(score, 4),
                "snippet": snip["text"], "parts": snip["parts"],
            })
        return {"query": text, "total": total, "at_least": capped,
                "results": results, "parsed": _parsed(q)}

    def _texts(self, kb_id: str, doc_ids) -> dict[str, str]:
        ids = list(doc_ids)
        out: dict[str, str] = {}
        with self._lock:
            for i in range(0, len(ids), 400):
                chunk = ids[i:i + 400]
                marks = ",".join("?" * len(chunk))
                rows = self._db.execute(
                    f"SELECT doc_id, text FROM docs WHERE kb_id=? AND doc_id IN ({marks})",
                    (kb_id, *chunk)).fetchall()
                out.update({r["doc_id"]: r["text"] for r in rows})
        return out

    def _filter_phrases(self, kb_id: str, ranked, q: Query):
        head = ranked[:PHRASE_CANDIDATE_CAP]
        texts = self._texts(kb_id, [d for d, _ in head])
        wanted = [rx for rx in (phrase_pattern(p) for p in q.phrases) if rx]
        banned = [rx for rx in (phrase_pattern(p) for p in q.exclude_phrases) if rx]
        keep = []
        for doc_id, score in head:
            text = texts.get(doc_id, "")
            if any(not rx.search(text) for rx in wanted):
                continue
            if any(rx.search(text) for rx in banned):
                continue
            keep.append((doc_id, score))
        return keep


def _why_nothing(q: Query) -> str:
    """Plain English for a query that parsed down to nothing searchable."""
    typed = (q.raw or "").strip()
    if not typed:
        return ""
    if q.exclude_terms or q.exclude_phrases:
        return ("You have only told it what to leave out. Add a word to look for "
                "as well — for example \"invoice -draft\".")
    words = _WORD.findall(typed)
    if words and all(w.casefold() in STOPWORDS for w in words):
        common = ", ".join(sorted({w.casefold() for w in words})[:6])
        return (f"Those are all very common words ({common}), so they are not "
                f"searched — nearly every document contains them. Try a word that "
                f"is particular to what you are looking for.")
    return ("There is nothing to search for in that. Try a word, a \"quoted "
            "phrase\", or ext:pdf to narrow it by file type.")


def _parsed(q: Query) -> dict:
    return {"terms": q.terms, "words": q.words, "phrases": q.phrases,
            "exclude": q.exclude_terms + q.exclude_phrases,
            "ext": q.exts, "folder": q.folders}
