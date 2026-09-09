# Bindery

Point it at a folder of a client's real documents. It reads them, indexes every
word, and gives you two things:

1. **A search box** that finds anything in the pile in well under a second.
   That is the demo. A long-established contractor has decades of paper nobody
   can search; you type "condenser warranty 2019" in front of the owner and it
   comes back.
2. **A gap report** on what is wrong with the pile — what is duplicated, what is
   stale, what nothing points at, what nobody can open, and what a business that
   size should have written down and does not. **That report is the thing you
   get paid for.** The search box just makes the owner believe you.

Both uses matter. In the room it has to produce something in under a minute. In
a paid engagement it has to produce a real artifact you can hand over.

## Run it

```
py system/apps/bindery/app.py
```

It picks a free port, starts a local server, and opens your browser. Nothing to
install — no pip, no Node, no internet. Ctrl+C to stop.

Everything stays on this machine. The server binds to 127.0.0.1 only.

## The promise that matters most

**It reads. It never writes.** Not to the client's folder, not a temp file, not
a lock file, not a rename. Every file is opened read-only, once, and closed.

Three tests hold this down, and they are the ones to keep if you ever throw the
rest away:

- `test_ingest.py` takes a **SHA-256 of every byte** plus the size and the
  modified time to the nanosecond, runs a full scan and then a re-scan, and
  fails if any of the three moved or if a single file appeared or disappeared.
- `test_ingest.py` also replaces `open`, `os.open`, `os.remove`, `os.rename`,
  `os.utime` and six more for the length of a scan and fails if any of them is
  called on anything under the source folder in a mode that could write.
- `test_api.py` does the byte-level check again through the whole running app:
  scan, re-scan, search, phrase search, report, Markdown export, clear, delete.

An advisor who mangles a client's files is finished. That is why this is three
tests and not a note in a comment.

## Using it

1. **New knowledge base** — one per client.
2. **Choose the folder.** Type or paste the path, or click through the picker.
3. **Read it.** Progress shows files done, total, and the file being read. You
   can walk away, or stop it — what has been read so far is kept.
4. **Search.** Press `/` or `Ctrl+K` from anywhere. It searches as you type.
5. **Gap report.** Download it as Markdown or print it to PDF.

Reading the same folder again only reads files whose size or date changed, so
the second pass takes seconds. Nothing is ever "saved" — everything autosaves.

### How to search

| Type this | What it does |
|---|---|
| `warranty labor` | Both words, anywhere in the file. Short documents that are genuinely about it come first |
| `"price list"` | The exact phrase — the words must be next to each other |
| `invoice -draft` | About invoices, but not if the word "draft" is in it |
| `ext:pdf safety` | PDFs only |
| `folder:contracts renewal` | Only inside a folder whose path mentions contracts |

Ranking is **BM25**, which divides a word's weight by how long the document is.
That matters more than it sounds: a 300-page manual that says "price" forty
times is not a better answer than a two-page price list, and a plain word count
would get that backwards every time.

Words are matched loosely on plurals, `-ing` and `-ed` (so "invoices" finds
"invoice"). It deliberately stops there — heavier stemming collapses words an
owner would swear are different and then the search box looks broken.

## What each gap-report finding means

**Copies that disagree.** Two kinds. *Identical* means the words are exactly the
same (case and spacing ignored — the same price list saved twice on different
days is one document). *Near-copy* means at least **55%** of the five-word runs
match: a document that was saved, edited, and saved again under a new name. The
finding is not the wasted disk space. It is that when four price lists disagree,
whoever picks the wrong one quotes the wrong number.

55% is the number worth knowing, and the report prints it. In practice it means
the two files share roughly three quarters of their words: rewrite 20% of a
document and it is still flagged; rewrite 30% and it is not. Two unrelated
letters that share only a letterhead and a confidentiality footer come out at
about 0.2 and are not flagged — checked, not assumed. Very long documents are
compared on their first few thousand word-runs rather than all of them, which
is worth a point or two of accuracy and saves about a minute on a big folder.

**Documents nobody has touched.** Grouped by how long since the file last
changed: under a year, one to three, three to five, over five. A procedure
nobody has edited in five years is either perfect or ignored, and it is almost
never perfect.

**Files nothing else points at.** No other document mentions this file's name.
It may still matter — but nobody is routed to it, so in day-to-day practice it
does not exist. Also counted separately: files that opened but had almost
nothing in them (under 25 words).

Two things about this finding you should say out loud before the owner asks:

- The word has to be one only a few files use. A file called `SKILL.md` is not
  "pointed at" because four hundred other documents happen to contain the word
  "skill" — nothing there routes anyone to that file, so a name-word appearing
  in more than a fifth of the folder is ignored. Without that rule this finding
  reported **three** orphans in a folder of twelve hundred, which is not a number
  anyone believes.
- It is still only a **name** match. A file everyone calls "the pricing sheet"
  that is saved as `PL-2024-rev3.xlsx` shows up here even though people use it
  daily. Files whose names have nothing distinctive to search for at all
  (`2024.pdf`, `PL.xlsx`) are reported as *"could not say either way"* and left
  out of the count in both directions.

**Files nobody can open or search.** Scanned PDFs, old Word and Excel formats,
password-locked files, damaged files, files locked by whoever has them open.
**In a company with decades of paper this is often the biggest bucket**, and it
is a business finding, not a computer one: it is knowledge the business already
paid for and cannot use. Every one comes with a plain-English reason.

**What is missing.** A short list of things a business of 10–50 people normally
has written down — safety rules, a current price list, how a new hire gets
started, supplier agreements, how a customer job is handled, how work turns into
cash, what happens if the files are lost, a rule for staff using AI tools,
insurance certificates, warranty terms. Missing is not automatically wrong, but
it should be a deliberate choice rather than a surprise. **Edit the list per
client** on the "What to check for" tab — an HVAC shop and a machine shop do not
need the same things.

Two kinds of "found", kept apart, because the difference matters:

- **Named** — there is a file whose *name* says it: `Safety Manual 2024.pdf`.
- **Only mentioned** — the words turn up inside some other document's text.
  That is evidence it might exist, not evidence it does. A memo that uses the
  word "warranty" is not warranty terms. **In the score a mention counts half**,
  and the arithmetic is printed: `2 + 6 ÷ 2 = 5.0 of 10 = 50%`.

Words are matched as **whole words**, loosely on punctuation, so `work order`
finds "work-order" — but "shipped" no longer counts as a safety manual on the
strength of `ppe` sitting in the middle of it, and "philosophy" no longer counts
as a written job process because `sop` is inside it.

**How concentrated it all is.** How much sits in one folder, one file type, or
one person's name (taken from who last saved each Office file). This is a people
risk, not a filing risk. It tells you what breaks when that person leaves.
Names Office invents when nobody set one — `user`, `Administrator`,
`Windows User`, `Un-named` — are not counted as a person.

**The readiness score.** Five measurements, each with a weight, adding to 100:

| Measurement | Weight |
|---|---|
| Files you can actually open and search | 25 |
| Files touched in the last three years | 20 |
| Files that are not a copy of another file | 15 |
| Things a business your size should have written down | 25 |
| Knowledge spread out rather than piled in one place | 15 |

Every line shows the two numbers behind it and the multiplication that produced
its points, and the points column adds to the score. That is the house rule on figures —
never a single-point figure without visible arithmetic — enforced in code. An
owner who can check your maths trusts you; one who can't, doesn't.

Two things that rule forces, and both of them show up in real folders:

- **Photos are not documents.** The first line is `readable ÷ documents`, not
  `readable ÷ everything`. A folder with nine hundred job-site photos next to a
  hundred documents scores on the hundred. The photos are counted and listed;
  they do not drag the score down.
- **A line that cannot be worked out prints a dash, not a zero, and drops out
  of the total.** Point it at a folder of scanned paper and there is no readable
  file to date, to compare, or to place — so those three lines say *"could not
  be measured"* and the score comes back **out of 25 rather than out of 100**.
  The old version printed "0.0%" next to a sentence that said "100% are
  current", which is exactly the kind of figure That rule exists to stop.

## Supported file types, and their limits

| Type | What comes out | Honest limits |
|---|---|---|
| `.txt .md .csv .json .log` and similar | The text | Encoding is tried as utf-8, then Windows-1252, then latin-1. It never throws — a file of raw binary comes back as nonsense and is then reported as having no meaningful text |
| `.html .htm .xml .svg` | Text with tags stripped | Simple stripping, not a parser. Script and style blocks are dropped |
| `.docx` | Paragraph text, plus headers, footers and footnotes | Comments and tracked changes are not read. Text inside embedded images is not read |
| `.xlsx` | Cell values, one row per line, all sheets | Values only — no formulas, no charts, no cell formatting |
| `.pptx` | Slide text and speaker notes | Text in images on slides is not read |
| `.pdf` | See below | See below |
| `.doc .xls .ppt .pub .wpd .msg .one` | Nothing | Reported as unreadable **with a reason that says what to do** ("save it as .docx and it becomes searchable"). That is a finding, not a failure |
| Everything else | Nothing | Counted as "not a document" with its extension, so images and video still show up in the totals |

### Be honest about the PDF extractor

There is no PDF library in the Python standard library, so this app has a small
one written from scratch. What that means in practice:

**It works on** PDFs with a real text layer — Word, Excel, Google Docs, most
accounting packages, most quote and invoice generators. That includes the common
awkward case where the exporter embeds a cut-down copy of the font and numbers
the letters 1, 2, 3: those files carry a `/ToUnicode` table saying what each
number means, and the extractor reads it. Without that step it would fail on the
majority of modern business PDFs. It was checked against every real PDF in this
workspace — 11 of 11 came out as readable, searchable text.

**It does not work on:**

- **Scans.** A photocopied 1998 service agreement has no letters in it at all,
  only a picture. Nothing short of OCR will read it, and OCR is not in the
  standard library. These are reported as *"no text inside — this is a scan"*.
- **Password-locked PDFs.** Reported as such.
- **Some newer PDFs** that pack their page structure into compressed object
  streams *and* use a font with no `/ToUnicode` table. The extractor falls back
  to merging every character map in the file, which usually still produces
  searchable words; when it does not, the file is reported as unreadable rather
  than indexed as gibberish.
- **Layout.** Columns, tables and reading order are approximate. This is a
  search index, not a faithful reproduction.

**Say this out loud with a client.** "I could read 340 of your PDFs and not the
other 90, and here is the list of the 90 and why" is a better conversation than
a tool that quietly indexes garbage.

### Other limits worth knowing

- Files over **40 MB** are counted and reported, not read. One part inside an
  Office file (a `.docx` is a zip) is refused above 80 MB *on the size the zip
  declares*, so a 200 KB file claiming to unpack to four gigabytes is turned
  away in microseconds instead of eating the machine.
- The stored text of any one file is capped at 1.5 million characters.
- Folders like `.git`, `node_modules`, `__pycache__`, sync-tool scratch folders
  (`.tmp.driveupload`, `.dropbox.cache`), and Office lock files
  (`~$budget.xlsx`) are skipped. Symlinks are not followed and every real
  directory is visited once, so a loop cannot hang the scan.
- A folder you do not have permission to enter is reported in the scan summary,
  not silently skipped.
- **One file nobody can read never ends the scan.** Every file is extracted
  inside its own guard, so an unforeseen failure on file 19,000 costs that one
  file, not the other 18,999.
- Search on 5,000 documents runs in about 30–65 ms. A quoted phrase is verified
  against the best 1,200 matches by score; if there were more candidates than
  that, the result count is shown as "1,200+" rather than pretending to be exact.
- Indexing runs at roughly 150–190 files per second on ordinary documents, so a
  2,000-file folder takes about 15 seconds the first time and seconds after.
  Working out the **gap report** costs a few seconds more on top — about 6
  seconds for 2,400 files — and it is cached until the next scan.
- **Stopping a scan leaves a part-read folder, and it says so.** The knowledge
  base, the report, the headline list and the exported Markdown all carry the
  warning until you read the folder through to the end.

## File layout

```
bindery/
  app.py            the HTTP API and the background scan jobs
  ingest.py         walking a folder and pulling text out of each format
  index.py          tokenising, the inverted index, BM25, phrases, snippets
  analyze.py        the gap report and its Markdown export
  static/
    index.html      the shell; loads /_shared/base.css and /_shared/ui.js first
    app.css         only this app's styles; every colour is a base.css variable
    app.js          the whole GUI
  tests/
    fixtures.py     .docx/.xlsx/.pptx/.pdf built in code — no binaries committed
    test_ingest.py  extraction per format, encodings, never crashing
    test_index.py   tokeniser, BM25 against hand arithmetic, query language, snippets
    test_analyze.py duplicates, staleness, orphans, coverage, the score arithmetic
    test_api.py     every endpoint over real HTTP, including the failure cases
  data/             sqlite: knowledge bases and the search index (created on first run)
```

Shared with the other two practice apps and **not modified by this one**:
`../_shared/appkit.py` (server), `../_shared/store.py` (storage),
`../_shared/static/base.css` and `ui.js` (the design system).

## Tests

```
py tests/test_ingest.py
py tests/test_index.py
py tests/test_analyze.py
py tests/test_api.py
```

240 tests. They cover BM25 against arithmetic worked out by hand, the tokeniser
and stemmer edge cases, phrase and exclusion filtering, snippet highlighting
with regex metacharacters in the query (`C++`, `a(b`), every Office format built
programmatically, the encoding fallback chain, the PDF extractor succeeding on a
generated PDF and failing *gracefully* on garbage, duplicate detection including
that unrelated documents are not flagged, the readiness score summing to what it
claims, incremental re-ingest, and every API endpoint's happy path, 404,
malformed body and nonexistent-folder case.

The ones worth knowing about, because they are the ones that caught real bugs:

- **Read-only, three ways** — byte hashes before and after; the filesystem write
  calls replaced and watched; and the same check again through the running app.
- **Real-world garbage** — a zero-byte file, a `.docx` that is not a zip, a zip
  bomb, an `.xlsx` with no shared strings, a PDF cut off mid-stream, a file held
  open by another process, a file deleted between the walk and the read, a
  folder that cannot be entered, filenames with emoji, `%`, `#` and a
  right-to-left override, and a scan where one file throws something nobody
  predicted. Every one has to come back as a record with a reason.
- **The score in edge cases** — no documents, one document, everything
  unreadable, everything a duplicate, a folder of nothing but photos, and every
  checklist item switched off. In each case the points column must add to the
  total *and* no line's words may claim a percentage the points column
  contradicts.
- **Query torture** — thirty malformed queries (`"foo`, `-foo`, `ext:`, `.*`,
  `(?:`, ten thousand characters) none of which may raise, plus a check that
  snippet parts rebuild the text exactly and can never carry markup.
- **Scan lifecycle** — stop then immediately start again, two scans at once,
  deleting a knowledge base mid-scan, and a scan that fails halfway.

---

*The tool is working software. The **checklist inside it is provisional** — it is
a guess at what a 10–50 person business should have written down. When a real
owner says "we don't do X, we do Y", change the list on the "What to check for"
tab and change the default in `analyze.py`.*
