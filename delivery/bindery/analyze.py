"""analyze — the gap report. This is the part that gets paid for.

The search box makes an owner's eyes widen. This file is the advisory finding.
It answers six questions about a business's own paperwork:

    What do you have more than one of, and do the copies agree?
    What has not been touched in years?
    What does nothing else point at?
    What can nobody open?
    What does a business your size normally have that you do not?
    How much of it sits in one place, one format, or one person's hands?

and then puts a number on it — with every step of the arithmetic shown, because
the rule in `05` §8 is that a figure the owner cannot check is a figure the
owner will not trust.
"""

from __future__ import annotations

import re
import time
import zlib
from collections import Counter, defaultdict

__all__ = [
    "analyze", "report_markdown", "DEFAULT_CHECKLIST", "SCORE_WEIGHTS",
    "shingles", "minhash", "jaccard",
]

YEAR = 365.25 * 24 * 3600

# ---------------------------------------------------------------- what a business this size should have

DEFAULT_CHECKLIST = [
    {"key": "safety", "label": "Safety rules or a safety manual",
     "why": "If someone gets hurt and there is nothing written down, the business owns that.",
     "phrases": ["safety", "job hazard", "toolbox talk", "ppe", "personal protective",
                 "osha", "incident report", "accident report"]},
    {"key": "pricing", "label": "A current price list or rate sheet",
     "why": "If the price lives in one person's head, you cannot hire, delegate, or check a quote.",
     "phrases": ["price list", "pricing", "rate sheet", "labor rate", "labour rate",
                 "price sheet", "flat rate", "markup"]},
    {"key": "onboarding", "label": "How a new hire gets started",
     "why": "Without it, every new person is trained differently and the first month is wasted.",
     "phrases": ["onboarding", "new hire", "orientation", "first day", "training plan",
                 "employee handbook"]},
    {"key": "vendors", "label": "Supplier and vendor agreements",
     "why": "You cannot hold a supplier to terms you cannot find.",
     "phrases": ["vendor agreement", "supplier agreement", "purchase agreement",
                 "terms and conditions", "master agreement", "vendor contract",
                 "supply agreement"]},
    {"key": "customer", "label": "How a customer job or order is handled",
     "why": "This is the document that makes the second employee as good as the first.",
     "phrases": ["work order", "job process", "customer service", "service procedure",
                 "scope of work", "standard operating", "sop", "checklist",
                 "installation procedure", "dispatch"]},
    {"key": "invoicing", "label": "How work turns into an invoice and then cash",
     "why": "Slow invoicing is the cheapest cash-flow fix there is, and it needs a written step order.",
     "phrases": ["invoice", "billing", "accounts receivable", "collections",
                 "payment terms", "net 30", "progress billing"]},
    {"key": "backup", "label": "What happens if the computers or the files are lost",
     "why": "Most small businesses find out they have no backup on the day they need one.",
     "phrases": ["backup", "back up", "disaster recovery", "business continuity",
                 "restore", "offsite copy", "continuity plan"]},
    {"key": "ai", "label": "A rule for staff using AI tools",
     "why": "Staff are already pasting customer and price information into chatbots. Nothing written means nothing to enforce.",
     "phrases": ["acceptable use", "artificial intelligence", "chatgpt", "ai policy",
                 "generative ai", "copilot", "technology use policy"]},
    {"key": "insurance", "label": "Insurance certificates and coverage",
     "why": "A customer asks for a certificate and it takes three days to find one.",
     "phrases": ["certificate of insurance", "general liability", "workers comp",
                 "workers' comp", "insurance policy", "coverage", "acord"]},
    {"key": "warranty", "label": "Warranty or service terms you give customers",
     "why": "Every argument about what was promised comes back to this page.",
     "phrases": ["warranty", "guarantee", "service agreement", "maintenance agreement",
                 "terms of service", "return policy"]},
]

# ---------------------------------------------------------------- the score

SCORE_WEIGHTS = [
    ("readable",  "Files you can actually open and search", 25),
    ("current",   "Files touched in the last three years", 20),
    ("unique",    "Files that are not a copy of another file", 15),
    ("covered",   "Things a business your size should have written down", 25),
    ("spread",    "Knowledge spread out rather than piled in one place", 15),
]

# Names Office writes when nobody set one. Telling an owner "most of your files
# were last saved by Administrator" is not a people risk, it is a default.
PLACEHOLDER_AUTHORS = {
    "un-named", "unnamed", "un named", "user", "users", "unknown", "owner",
    "administrator", "admin", "windows user", "microsoft office user",
    "author", "guest", "default", "office", "pc", "computer", "n/a", "none",
}


# ---------------------------------------------------------------- phrase matching

_PHRASE_RX: dict[str, "re.Pattern"] = {}


def phrase_rx(phrase: str):
    """A regex that finds `phrase` as whole words.

    Written as a real word match rather than `phrase in text`, because plain
    substring matching told an owner they had a safety manual on the strength of
    'ppe' inside 'shipped', and that they had a written job process because a
    document contained the word 'philosophy' ('sop' is in the middle of it).
    Separators are loose, so 'work order' finds 'work-order' and 'Work  Order'.
    """
    rx = _PHRASE_RX.get(phrase)
    if rx is not None:
        return rx
    words = re.findall(r"[^\W_]+", (phrase or "").casefold())
    if not words:
        return None
    body = r"[\W_]+".join(re.escape(w) for w in words)
    rx = re.compile(r"(?<![^\W_])" + body + r"(?![^\W_])", re.I | re.UNICODE)
    if len(_PHRASE_RX) > 400:
        _PHRASE_RX.clear()
    _PHRASE_RX[phrase] = rx
    return rx


# ---------------------------------------------------------------- near-duplicate maths

_SHINGLE = 5
_HASHES = 64
_BANDS = 16
_MASK = 0xFFFFFFFF
# The most five-word runs we keep from any one document. A 300-page manual has
# a hundred thousand of them and comparing all of them costs half a second per
# file, which turns the report into a two-minute wait. Keeping the 2,000
# lowest-numbered runs is the standard sample: it is the same 2,000 for the same
# words every time, so two copies of a document still line up. When either side
# of a comparison was cut this way, `jaccard` trims both to the range they both
# cover in full, so a long document is not made to look unlike a short one just
# because its list stops earlier.
_MAX_SHINGLES = 2000
# Fixed multipliers so two runs of the report never disagree.
_A = [((i * 2654435761 + 40503) | 1) & _MASK for i in range(1, _HASHES + 1)]
_BB = [(i * 97531 + 1234567) & _MASK for i in range(1, _HASHES + 1)]


def _stable_hash(text: str) -> int:
    """A 64-bit hash that is the same in every process.

    Python's built-in hash() is salted per run, which would make the same folder
    produce a different report on Tuesday than it did on Monday.
    """
    raw = text.encode("utf-8", "replace")
    return (zlib.crc32(raw) << 32) | (zlib.adler32(raw) & 0xFFFFFFFF)


def shingles(text: str, size: int = _SHINGLE, cap: int = _MAX_SHINGLES) -> set:
    """Overlapping runs of `size` words, hashed. Two documents that share most of
    their five-word runs are the same document with edits.

    Long documents are cut down to the `cap` lowest-numbered runs — see the note
    on `_MAX_SHINGLES`.
    """
    words = re.findall(r"[^\W_]+", (text or "").casefold())
    if len(words) < size:
        return set()
    out = {_stable_hash(" ".join(words[i:i + size]))
           for i in range(len(words) - size + 1)}
    if cap and len(out) > cap:
        out = set(sorted(out)[:cap])
    return out


def minhash(sh: set) -> tuple:
    if not sh:
        return ()
    small = [s & _MASK for s in sh]
    return tuple(min((a * s + b) & _MASK for s in small) for a, b in zip(_A, _BB))


def jaccard(a: set, b: set, cap: int | None = None) -> float:
    """How much two documents overlap, 0 to 1.

    `cap` matters when one or both sets were cut down by `shingles`. A short
    document keeps every run it has; a long one keeps only its lowest-numbered
    ones. Comparing those two directly makes the pair look less alike than it is,
    purely because the long document's list stops earlier. So when either side
    was cut, both are trimmed to the range they both cover in full, and the
    answer is worked out there. Same arithmetic, no length bias.
    """
    if not a or not b:
        return 0.0
    if cap and (len(a) >= cap or len(b) >= cap):
        limit = min(max(a), max(b))
        a = {x for x in a if x <= limit}
        b = {x for x in b if x <= limit}
        if not a or not b:
            return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    return inter / len(a | b)


def _near_duplicate_groups(docs, threshold: float):
    """Candidate pairs by banded MinHash, then a real Jaccard on the pairs.

    Banding keeps this from being every-document-against-every-document, which
    on a real client folder would take minutes.
    """
    sigs, sets = {}, {}
    for d in docs:
        sh = shingles(d["text"])
        if len(sh) < 8:
            continue
        sets[d["doc_id"]] = sh
        sigs[d["doc_id"]] = minhash(sh)
    if len(sigs) < 2:
        return []

    rows = _HASHES // _BANDS
    buckets = defaultdict(list)
    for doc_id, sig in sigs.items():
        for band in range(_BANDS):
            key = (band, sig[band * rows:(band + 1) * rows])
            buckets[key].append(doc_id)

    pairs = set()
    for members in buckets.values():
        if len(members) < 2 or len(members) > 60:
            continue
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                pairs.add(tuple(sorted((members[i], members[j]))))

    # Union-find over pairs that clear the threshold.
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    scored = {}
    for a, b in pairs:
        sim = jaccard(sets[a], sets[b], cap=_MAX_SHINGLES)
        if sim >= threshold:
            scored[(a, b)] = sim
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

    groups = defaultdict(list)
    for doc_id in parent:
        groups[find(doc_id)].append(doc_id)

    out = []
    for members in groups.values():
        if len(members) < 2:
            continue
        sims = [s for (a, b), s in scored.items() if a in members and b in members]
        out.append({"members": sorted(members),
                    "similarity": round(max(sims) if sims else 0.0, 3)})
    out.sort(key=lambda g: (-len(g["members"]), -g["similarity"]))
    return out


# ---------------------------------------------------------------- the report

def analyze(docs, *, checklist=None, now=None, near_threshold: float = 0.55,
            top_n: int = 12, partial: bool = False) -> dict:
    """Take every ingest record for one knowledge base and produce the report.

    `docs` want the keys `doc_id, rel, name, folder, ext, size, mtime, status,
    reason, author, chash, nwords, text`.

    `partial` — the scan that produced these records was stopped early, so this
    is a report on part of the folder. It gets said at the top, in the headlines
    and in the exported Markdown, because a half-read folder that reads as a
    finished report is the worst thing this file could produce.
    """
    now = now if now is not None else time.time()
    # `None` means "use the built-in list". An empty list means the owner turned
    # everything off, and that must not be quietly overruled by the default.
    source = DEFAULT_CHECKLIST if checklist is None else checklist
    checklist = [c for c in source if c.get("on", True)]
    docs = [dict(d) for d in docs]
    for d in docs:
        d.setdefault("doc_id", d.get("rel", ""))
        d.setdefault("text", "")
        d.setdefault("nwords", len((d.get("text") or "").split()))

    readable = [d for d in docs if d["status"] == "ok"]
    unreadable = [d for d in docs if d["status"] == "unreadable"]
    skipped = [d for d in docs if d["status"] == "skipped"]
    total = len(docs)

    counts = {
        "total": total,
        "readable": len(readable),
        "unreadable": len(unreadable),
        "skipped": len(skipped),
        "words": sum(d["nwords"] for d in readable),
        "bytes": sum(int(d.get("size") or 0) for d in docs),
    }

    # ---- duplicates --------------------------------------------------
    by_hash = defaultdict(list)
    for d in readable:
        if d.get("chash") and d["nwords"] >= 5:
            by_hash[d["chash"]].append(d)
    exact = []
    for members in by_hash.values():
        if len(members) < 2:
            continue
        members = sorted(members, key=lambda d: -int(d.get("mtime") or 0))
        exact.append({
            "kind": "exact",
            "similarity": 1.0,
            "words": members[0]["nwords"],
            "files": [_slim(d) for d in members],
            "newest": members[0]["rel"],
            "spread_years": round((max(int(d.get("mtime") or 0) for d in members)
                                   - min(int(d.get("mtime") or 0) for d in members)) / YEAR, 1),
        })
    exact.sort(key=lambda g: (-len(g["files"]), -g["words"]))

    in_exact = {f["rel"] for g in exact for f in g["files"]}
    near_pool = [d for d in readable if d["rel"] not in in_exact and d["nwords"] >= 30]
    by_id = {d["doc_id"]: d for d in readable}
    near = []
    for group in _near_duplicate_groups(near_pool, near_threshold):
        members = [by_id[m] for m in group["members"] if m in by_id]
        if len(members) < 2:
            continue
        members.sort(key=lambda d: -int(d.get("mtime") or 0))
        near.append({
            "kind": "near",
            "similarity": group["similarity"],
            "words": members[0]["nwords"],
            "files": [_slim(d) for d in members],
            "newest": members[0]["rel"],
            "spread_years": round((max(int(d.get("mtime") or 0) for d in members)
                                   - min(int(d.get("mtime") or 0) for d in members)) / YEAR, 1),
        })

    dup_files = {f["rel"] for g in exact + near for f in g["files"]}
    duplicates = {
        "exact_groups": exact[:top_n * 2],
        "near_groups": near[:top_n * 2],
        "exact_group_count": len(exact),
        "near_group_count": len(near),
        "files_involved": len(dup_files),
        "share": _pct(len(dup_files), len(readable)),
        "wasted_copies": sum(len(g["files"]) - 1 for g in exact + near),
    }

    # ---- stale -------------------------------------------------------
    buckets = {"under1": 0, "1to3": 0, "3to5": 0, "over5": 0, "unknown": 0}
    aged = []
    for d in readable:
        mt = int(d.get("mtime") or 0)
        if mt <= 0:
            buckets["unknown"] += 1
            continue
        years = (now - mt) / YEAR
        aged.append((years, d))
        if years < 1:
            buckets["under1"] += 1
        elif years < 3:
            buckets["1to3"] += 1
        elif years < 5:
            buckets["3to5"] += 1
        else:
            buckets["over5"] += 1
    aged.sort(key=lambda t: -t[0])
    over3 = buckets["3to5"] + buckets["over5"]
    stale = {
        "buckets": buckets,
        "over_3y": over3,
        "over_5y": buckets["over5"],
        "share_over_3y": _pct(over3, len(readable)),
        "oldest": [dict(_slim(d), years=round(y, 1)) for y, d in aged[:top_n]],
    }

    # ---- orphans -----------------------------------------------------
    # A file is "pointed at" if another file's text mentions its name.
    #
    # The word has to be a word only this handful of files use. A file called
    # SKILL.md is not "pointed at" because four hundred other documents happen
    # to contain the word "skill" — nothing there routes anyone to that file.
    # Counting it as referenced was making this finding report three orphans in
    # a folder of twelve hundred, which no owner would have believed.
    name_terms: dict[str, set] = {}
    for d in readable:
        stem = re.sub(r"\.[A-Za-z0-9]{1,6}$", "", d["name"])
        toks = {t for t in re.findall(r"[^\W_]+", stem.casefold()) if len(t) >= 4}
        toks = {t for t in toks if not t.isdigit()}
        if toks:
            name_terms[d["doc_id"]] = toks
    interesting = set().union(*name_terms.values()) if name_terms else set()
    mentions: dict[str, set] = defaultdict(set)          # token -> doc_ids containing it
    if interesting:
        for d in readable:
            for tok in set(re.findall(r"[^\W_]+", (d["text"] or "").casefold())) & interesting:
                mentions[tok].add(d["doc_id"])

    # A word in more than a fifth of the folder routes nobody anywhere.
    common_cutoff = max(2, 0.2 * len(readable))
    unreferenced, unjudged = [], []
    for d in readable:
        toks = name_terms.get(d["doc_id"])
        if not toks:
            # Nothing in the name to go on — "2024.pdf", "PL.xlsx". We cannot
            # say either way, so it is reported apart rather than silently left
            # out of both counts.
            unjudged.append(d)
            continue
        useful = {t for t in toks if len(mentions.get(t, ())) <= common_cutoff}
        if not useful:
            unreferenced.append(d)
            continue
        pointing = set.intersection(*[mentions.get(t, set()) for t in useful])
        pointing = set(pointing)
        pointing.discard(d["doc_id"])
        if not pointing:
            unreferenced.append(d)
    judged = len(readable) - len(unjudged)
    no_text = [d for d in readable if d["nwords"] < 25]
    orphans = {
        "unreferenced": [_slim(d) for d in sorted(unreferenced,
                                                  key=lambda d: -int(d.get("size") or 0))[:top_n * 3]],
        "unreferenced_count": len(unreferenced),
        "unreferenced_share": _pct(len(unreferenced), judged),
        "judged_count": judged,
        "unjudged_count": len(unjudged),
        "unjudged": [_slim(d) for d in unjudged[:top_n]],
        "thin": [_slim(d) for d in sorted(no_text, key=lambda d: d["nwords"])[:top_n * 2]],
        "thin_count": len(no_text),
    }

    # ---- unreadable --------------------------------------------------
    by_reason = Counter((d.get("reason") or "no reason recorded") for d in unreadable)
    by_ext = Counter(d["ext"] or "(no extension)" for d in unreadable)
    unreadable_report = {
        "count": len(unreadable),
        "share": _pct(len(unreadable), total),
        "by_reason": [
            {"reason": reason, "count": n,
             "examples": [_slim(d) for d in unreadable if (d.get("reason") or
                          "no reason recorded") == reason][:5]}
            for reason, n in by_reason.most_common()],
        "by_ext": [{"ext": e, "count": n} for e, n in by_ext.most_common(10)],
        "bytes": sum(int(d.get("size") or 0) for d in unreadable),
    }
    skipped_report = {
        "count": len(skipped),
        "by_ext": [{"ext": e, "count": n} for e, n in
                   Counter(d["ext"] or "(no extension)" for d in skipped).most_common(12)],
    }

    # ---- coverage ----------------------------------------------------
    lowered = [(d, (d["text"] or "").casefold(), d["rel"].casefold()) for d in readable]
    found, missing = [], []
    for item in checklist:
        patterns = [(p, phrase_rx(p)) for p in item["phrases"]]
        patterns = [(p, rx) for p, rx in patterns if rx is not None]
        hits = []
        for d, text, rel in lowered:
            for phrase, rx in patterns:
                where = "name" if rx.search(rel) else ("text" if rx.search(text) else None)
                if where:
                    hits.append({"rel": d["rel"], "matched": phrase, "where": where})
                    break
            if len(hits) >= 5:
                break
        # A match in a file's name is a document about the thing. A match buried
        # in someone else's prose is a mention, and the report says which.
        hits.sort(key=lambda h: 0 if h["where"] == "name" else 1)
        entry = {"key": item["key"], "label": item["label"], "why": item.get("why", ""),
                 "hits": hits, "count": len(hits),
                 "named": any(h["where"] == "name" for h in hits)}
        (found if hits else missing).append(entry)
    # A document called "Safety Manual.pdf" is the thing. The word "safety"
    # turning up in the middle of somebody else's memo is evidence that it might
    # exist, and it is worth half. Without this a folder of glossaries scored
    # ten out of ten for paperwork it did not have.
    named_count = sum(1 for f in found if f["named"])
    mention_count = len(found) - named_count
    credit = named_count + 0.5 * mention_count
    coverage = {
        "found": found, "missing": missing,
        # With nothing readable we did not look, so nothing is "missing" — the
        # safety manual may be sitting inside one of the scans. Saying "10 things
        # are missing" next to a score line that says coverage could not be
        # measured is the same contradiction, moved into the headline.
        "measured": len(readable) > 0 and len(checklist) > 0,
        "checked": len(checklist),
        "found_count": len(found),
        "named_count": named_count,
        "mention_count": mention_count,
        "mentioned_only": [f["label"] for f in found if not f["named"]],
        "credit": round(credit, 1),
        "credit_pct": _pct(credit, len(checklist)),
        "pct": _pct(len(found), len(checklist)),
    }

    # ---- concentration ------------------------------------------------
    def _share_table(keyfn, source=readable, limit=8):
        c = Counter(keyfn(d) for d in source)
        n = sum(c.values()) or 1
        return [{"key": k or "(none)", "count": v, "share": _pct(v, n)}
                for k, v in c.most_common(limit)]

    top_folder = lambda d: (d["folder"].split("/", 1)[0] if d["folder"] else "(top level)")
    folders = _share_table(top_folder)
    types = _share_table(lambda d: d["ext"] or "(no extension)")
    named = [d for d in readable if _real_author(d.get("author"))]
    authors = _share_table(lambda d: (d.get("author") or "").strip(), named)
    top_folder_share = folders[0]["share"] if folders else 0.0
    concentration = {
        "folders": folders, "types": types, "authors": authors,
        "top_folder": folders[0]["key"] if folders else "",
        "top_folder_share": top_folder_share,
        "top_type": types[0]["key"] if types else "",
        "top_type_share": types[0]["share"] if types else 0.0,
        "top_author": authors[0]["key"] if authors else "",
        "top_author_share": authors[0]["share"] if authors else 0.0,
        "authors_known": sum(a["count"] for a in authors),
    }

    # ---- the score ----------------------------------------------------
    #
    # Two rules here, both from `05` §8. First, the points column has to add to
    # the score. Second — and this is the one that used to be broken — a line
    # whose words say one number and whose points column says another is worse
    # than no line at all. In a folder of scanned paper there is no readable
    # file to measure staleness or copies against, and the honest answer is
    # "we could not measure this", not a silent zero. Those lines drop out of
    # the score and out of what the score is out of, and say so.
    documents = len(readable) + len(unreadable)      # things we tried to read
    can = {
        "readable": documents > 0,
        "current": len(readable) > 0,
        "unique": len(readable) > 0,
        # If nothing opened, we cannot say a business is missing its safety
        # manual — it may be sitting in one of the scans we could not read.
        "covered": len(checklist) > 0 and len(readable) > 0,
        "spread": len(readable) > 0,
    }
    raw = {
        "readable": _pct(len(readable), documents),
        "current": 100.0 - stale["share_over_3y"],
        "unique": 100.0 - duplicates["share"],
        "covered": coverage["credit_pct"],
        "spread": _spread_pct(top_folder_share),
    }
    how = {
        "readable": (f"{len(readable)} of {documents} documents opened and gave us words "
                     f"= {raw['readable']}%"
                     + (f" (the other {len(skipped)} files are photos, video and the like "
                        f"— not documents, so they are counted but not scored)"
                        if skipped else "")),
        "current": (f"{over3} of {len(readable)} readable files have not been touched in "
                    f"3 years, so {round(100 - stale['share_over_3y'], 1)}% are current"),
        "unique": (f"{len(dup_files)} of {len(readable)} readable files are a copy of "
                   f"something else, so {round(100 - duplicates['share'], 1)}% are unique"),
        "covered": (f"of {len(checklist)} things a business your size should have written "
                    f"down, {coverage['named_count']} are a document named for it and "
                    f"{coverage['mention_count']} are only a mention inside another file, "
                    f"which counts half: {coverage['named_count']} + "
                    f"{coverage['mention_count']} ÷ 2 = {coverage['credit']} of "
                    f"{len(checklist)} = {coverage['credit_pct']}%"),
        "spread": (f"the biggest folder holds {top_folder_share}% of the files; anything "
                   f"over 35% starts to count against you, giving {raw['spread']}%"),
    }
    cannot = {
        "readable": "could not be measured — no file here was a document we try to read",
        "current": "could not be measured — nothing here opened, so there is nothing to date",
        "unique": "could not be measured — nothing here opened, so nothing can be compared",
        "covered": ("could not be measured — every item on the checklist is switched off"
                    if len(readable) else
                    "could not be measured — nothing here opened, so anything on the "
                    "checklist could still be inside a file we could not read"),
        "spread": "could not be measured — nothing here opened, so there is nothing to place",
    }
    components, total_points, out_of = [], 0.0, 0
    for key, label, weight in SCORE_WEIGHTS:
        if not can[key]:
            components.append({"key": key, "label": label, "weight": weight,
                               "raw_pct": None, "points": None, "measured": False,
                               "how": cannot[key]})
            continue
        pct = round(max(0.0, min(100.0, raw[key])), 1)
        points = round(pct / 100.0 * weight, 1)
        total_points += points
        out_of += weight
        components.append({"key": key, "label": label, "weight": weight,
                           "raw_pct": pct, "points": points, "measured": True,
                           "how": how[key]})
    unmeasured = [c["label"] for c in components if not c["measured"]]
    note = ("Add the points column and you get the score. Nothing is hidden and "
            "nothing is weighted behind your back.")
    if unmeasured:
        note += (" " + str(len(unmeasured)) + " of the five measurements could not be "
                 "worked out on this folder, so the score is out of "
                 + str(out_of) + " rather than 100.")
    score = {
        "total": round(total_points, 1),
        "out_of": out_of,
        "components": components,
        "band": _band(100.0 * total_points / out_of) if out_of else
                "There was nothing here to score",
        "unmeasured": unmeasured,
        "note": note,
    }

    return {
        "generated": int(time.time() * 1000),
        "partial": bool(partial),
        "near_threshold": near_threshold,
        "counts": counts,
        "duplicates": duplicates,
        "stale": stale,
        "orphans": orphans,
        "unreadable": unreadable_report,
        "skipped": skipped_report,
        "coverage": coverage,
        "concentration": concentration,
        "score": score,
        "headlines": _headlines(counts, duplicates, stale, orphans, unreadable_report,
                                coverage, concentration, bool(partial)),
    }


def _uncap(label: str) -> str:
    """Drop the capital off the front of a label so it reads inside a sentence.

    Only the first letter — `.lower()` on the whole thing turned "a rule for
    staff using AI tools" into "ai tools" in a document a client reads.
    """
    label = label or ""
    return (label[:1].lower() + label[1:]) if label else ""


def _real_author(name) -> bool:
    """Is this an actual person's name, or the placeholder Office wrote?"""
    clean = (name or "").strip()
    return bool(clean) and clean.casefold() not in PLACEHOLDER_AUTHORS


def _slim(d: dict) -> dict:
    return {"doc_id": d.get("doc_id", ""), "rel": d.get("rel", ""), "name": d.get("name", ""),
            "folder": d.get("folder", ""), "ext": d.get("ext", ""),
            "size": int(d.get("size") or 0), "mtime": int(d.get("mtime") or 0),
            "nwords": int(d.get("nwords") or 0), "reason": d.get("reason")}


def _pct(n, d) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


def _spread_pct(top_share: float) -> float:
    """35% of everything in one folder is normal. 100% in one folder is a risk.

    Below 35 it scores full marks; from 35 to 100 it falls straight to zero.
    """
    if top_share <= 35:
        return 100.0
    return round(max(0.0, 100.0 * (100.0 - top_share) / 65.0), 1)


def _band(score: float) -> str:
    if score >= 80:
        return "In good shape"
    if score >= 60:
        return "Workable, with real gaps"
    if score >= 40:
        return "Needs work before it can be relied on"
    return "Not something the business can lean on yet"


def _headlines(counts, duplicates, stale, orphans, unreadable, coverage, conc,
               partial: bool = False) -> list[dict]:
    """The three or four lines you would actually say out loud in the room."""
    out = []
    if partial:
        out.append({"kind": "bad", "text":
                    f"The read was stopped early, so this covers only the "
                    f"{counts['total']} files that had been opened by then — not the "
                    f"whole folder. Read it again to the end before showing this to "
                    f"anyone."})
    if unreadable["count"]:
        out.append({"kind": "bad", "text":
                    f"{unreadable['count']} files ({unreadable['share']}% of everything) "
                    f"cannot be opened or searched by anything — including your own staff."})
    if duplicates["wasted_copies"]:
        biggest = (duplicates["exact_groups"] or duplicates["near_groups"] or [None])[0]
        extra = ""
        if biggest:
            extra = f" The worst is {len(biggest['files'])} copies of {biggest['files'][0]['name']}."
        out.append({"kind": "warn", "text":
                    f"{duplicates['files_involved']} files are copies or near-copies of "
                    f"each other.{extra} When they disagree, nobody knows which one is right."})
    if stale["over_3y"]:
        out.append({"kind": "warn", "text":
                    f"{stale['over_3y']} files ({stale['share_over_3y']}% of what we could "
                    f"read) have not been touched in over three years."})
    if coverage["missing"] and coverage.get("measured", True):
        names = ", ".join(_uncap(m["label"]) for m in coverage["missing"][:3])
        out.append({"kind": "bad", "text":
                    f"{len(coverage['missing'])} things a business your size normally has "
                    f"written down are missing — starting with {names}."})
    if conc["top_folder_share"] >= 50:
        out.append({"kind": "warn", "text":
                    f"{conc['top_folder_share']}% of everything sits in one folder "
                    f"(\"{conc['top_folder']}\")."})
    if orphans["unreferenced_count"]:
        out.append({"kind": "info", "text":
                    f"{orphans['unreferenced_count']} of the {orphans['judged_count']} files "
                    f"we could check are not named in any other document. Nothing points at "
                    f"them, so nobody finds them."})
    return out[:5]


# ---------------------------------------------------------------- markdown export

def report_markdown(kb_name: str, folder: str, r: dict, when: str | None = None) -> str:
    """The whole report as Markdown — paste-ready into the assessment document."""
    when = when or time.strftime("%B %d, %Y", time.localtime(r.get("generated", 0) / 1000
                                                             or time.time()))
    c, s = r["counts"], r["score"]
    L = [f"# What your documents look like — {kb_name}", ""]
    L += [f"**Folder looked at:** `{folder}`  ", f"**Date:** {when}", ""]
    if r.get("partial"):
        L += ["> **This is a part read.** The scan was stopped before it finished, so every",
              "> number below covers only the files that had been opened by then. Read the",
              "> folder again to the end before this goes to anybody.", ""]
    L += ["This is a report on your own files: what is in them, what is missing, what is",
          "duplicated, and what nobody can open. Nothing here left your computer.", ""]

    L += ["## The short version", ""]
    for h in r["headlines"]:
        L.append(f"- {h['text']}")
    if not r["headlines"]:
        L.append("- Nothing stood out. That is unusual and worth a second look.")
    L += ["", "## Readiness score", ""]
    L += [f"**{s['total']} out of {s['out_of']} — {s['band']}**", "",
          "| What we measured | Where you are | Weight | Points |",
          "|---|---|---|---|"]
    for comp in s["components"]:
        if comp["measured"]:
            L.append(f"| {comp['label']} | {comp['raw_pct']}% | {comp['weight']} "
                     f"| {comp['points']} |")
        else:
            L.append(f"| {comp['label']} | not measured | — | — |")
    L.append(f"| **Total** | | **{s['out_of']}** | **{s['total']}** |")
    L += ["", "How each line was worked out:", ""]
    for comp in s["components"]:
        if comp["measured"]:
            L.append(f"- **{comp['label']}** — {comp['how']}. "
                     f"{comp['raw_pct']}% × {comp['weight']} ÷ 100 = {comp['points']} points.")
        else:
            L.append(f"- **{comp['label']}** — {comp['how']}. It is left out of the score "
                     f"rather than counted as a zero, so the score is out of "
                     f"{s['out_of']}.")
    L += ["", f"_{s['note']}_", ""]

    skipped_note = ""
    if r["skipped"]["by_ext"]:
        kinds = ", ".join(f"{x['ext']} ×{x['count']}" for x in r["skipped"]["by_ext"][:5])
        skipped_note = f" — mostly {kinds}"
    L += ["## What we looked at", "",
          f"- {c['total']} files in total",
          f"- {c['readable']} we could read, holding about {c['words']:,} words",
          f"- {c['unreadable']} we could not read",
          f"- {c['skipped']} counted but not read, because they are not documents"
          f"{skipped_note}",
          ""]

    u = r["unreadable"]
    L += ["## Files nobody can open or search", ""]
    if not u["count"]:
        L.append("Everything opened. That is rare — good.")
    else:
        L.append(f"{u['count']} files ({u['share']}% of everything) could not be read. "
                 f"This is usually the biggest single finding in an older business, and it is "
                 f"not a computer problem — it is knowledge the business already paid for and "
                 f"cannot use.")
        L += ["", "| Why | How many | For example |", "|---|---|---|"]
        for row in u["by_reason"]:
            eg = ", ".join(e["name"] for e in row["examples"][:2])
            L.append(f"| {row['reason']} | {row['count']} | {eg} |")
    L.append("")

    d = r["duplicates"]
    L += ["## Copies that disagree", ""]
    if not (d["exact_groups"] or d["near_groups"]):
        L.append("No duplicate documents found.")
    else:
        L.append(f"{d['files_involved']} files ({d['share']}% of what we could read) are "
                 f"copies or near-copies. That is {d['wasted_copies']} extra copies. "
                 f"The question is never how many copies — it is which one is right.")
        L.append("")
        L.append(f"\"Near-copy\" means at least {int(r.get('near_threshold', 0.55) * 100)}% "
                 f"of the five-word runs in the two files are the same — in practice a "
                 f"document that was saved, edited, and saved again under a new name.")
        L.append("")
        for g in (d["exact_groups"][:6] + d["near_groups"][:6]):
            kind = "identical" if g["kind"] == "exact" else f"{int(g['similarity'] * 100)}% the same"
            L.append(f"**{len(g['files'])} files, {kind}** "
                     f"(spread over {g['spread_years']} years):")
            for f in g["files"]:
                L.append(f"  - `{f['rel']}`")
            L.append("")

    st = r["stale"]
    L += ["## Documents nobody has touched", "",
          f"- Under a year old: {st['buckets']['under1']}",
          f"- One to three years: {st['buckets']['1to3']}",
          f"- Three to five years: {st['buckets']['3to5']}",
          f"- Over five years: {st['buckets']['over5']}", ""]
    if st["oldest"]:
        L += ["Oldest first:", ""]
        for f in st["oldest"][:10]:
            L.append(f"- `{f['rel']}` — {f['years']} years")
        L.append("")

    o = r["orphans"]
    L += ["## Files nothing else points at", "",
          f"{o['unreferenced_count']} of the {o['judged_count']} files we could judge "
          f"({o['unreferenced_share']}%) are never named in any other document. They are not "
          f"necessarily wrong — but nobody navigates to them, so in practice they do not "
          f"exist.", "",
          "This is a name match. A file everyone calls \"the pricing sheet\" but which is "
          "saved as `PL-2024-rev3.xlsx` will show up here even though people use it every "
          "day, so read the list before you read anything into it.", ""]
    for f in o["unreferenced"][:10]:
        L.append(f"- `{f['rel']}`")
    if o["unjudged_count"]:
        L += ["", f"A further {o['unjudged_count']} files have nothing distinctive in the "
                  f"name to search for (things like `2024.pdf`), so we could not say either "
                  f"way. They are not counted above."]
    if o["thin_count"]:
        L += ["", f"A further {o['thin_count']} files opened but had almost nothing in them "
                  f"(under 25 words)."]
    L.append("")

    cov = r["coverage"]
    L += ["## What is missing", ""]
    if not cov.get("measured", True):
        L += ["Nothing here opened, so we could not look. Anything on the checklist could "
              "still be inside one of the files nobody can read — that is the finding above, "
              "not this one.", ""]
        cov = {"missing": [], "found": [], "mentioned_only": []}
    else:
        L += [f"We checked for {cov['checked']} things a business your size normally has "
              f"written down. {cov['found_count']} were found — {cov['named_count']} of them "
              f"as a document with the words in its name, {cov['mention_count']} only as a "
              f"mention inside some other file. In the score a mention counts half, because a "
              f"memo that says the word \"warranty\" is not the same as having warranty terms "
              f"written down.", ""]
    if cov["mentioned_only"]:
        L += ["Worth checking by hand, because a mention is not the same as having the "
              "document: " + ", ".join(_uncap(m) for m in cov["mentioned_only"]) + ".", ""]
    if cov["missing"]:
        L += ["**Not found anywhere:**", ""]
        for m in cov["missing"]:
            L.append(f"- **{m['label']}** — {m['why']}")
        L.append("")
    if cov["found"]:
        L += ["**Found:**", ""]
        for f in cov["found"]:
            hit = f["hits"][0]
            how = ("in the file's name" if hit["where"] == "name"
                   else "only mentioned inside the text")
            L.append(f"- {f['label']} — for example `{hit['rel']}` "
                     f"(matched \"{hit['matched']}\", {how})")
        L.append("")

    k = r["concentration"]
    L += ["## How concentrated it all is", "",
          f"- Biggest folder: **{k['top_folder']}** holds {k['top_folder_share']}% of the files",
          f"- Most common file type: **{k['top_type']}** at {k['top_type_share']}%"]
    if k["authors"]:
        L.append(f"- Most files last saved by: **{k['top_author']}** "
                 f"({k['top_author_share']}% of the {k['authors_known']} files that record a name)")
    L += ["", "Concentration is a people risk, not a filing risk. If one folder, one format, or "
              "one person's name is on most of it, that is where the business breaks when they "
              "leave.", ""]

    L += ["---", "",
          "*Counts come from reading the folder as it stood on the date above. No file was "
          "changed, moved, or copied. Where a figure is a share, the two numbers behind it are "
          "shown so you can check the arithmetic.*"]
    return "\n".join(L)
