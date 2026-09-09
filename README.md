# Bindery

Point it at a folder of documents. It reads them, indexes every word, and gives
you two things: a search box that finds anything in the pile in well under a
second, and a report on what is wrong with the pile — what is duplicated, what
contradicts something else, what nobody has touched in four years, and what is
sitting there with no way in.

No dependencies. No installer. No network. `py delivery/bindery/app.py` opens a
browser and it runs on the standard library, on a folder on your own disk.

```
py delivery/bindery/app.py        # opens in your browser
py run_all_tests.py               # 247 tests
```

Windows: double-click `run.cmd`.

## What it actually does

**Ingest.** Walks a folder and pulls text out of `.docx`, `.pdf`, `.xlsx`,
`.md`, `.txt`, `.csv`, `.html` and a few others, including the awkward ones —
PDFs with subset fonts, files that lie about their encoding, documents whose
declared type and actual bytes disagree. Anything it could not read is
**listed**, not skipped. A file that quietly vanishes from an index is worse
than one that is missing loudly.

**Index.** A full-text index built by hand on top of sqlite: tokenising,
stemming, ranking. Search returns matches with the surrounding sentence, so you
can see why something matched before you open it.

**The gap report.** This is the part that is worth something. Given a pile of
documents it will tell you:

- **Duplicates and near-duplicates** — the same procedure saved four times with
  three different dates on it.
- **Contradictions** — two documents that state a different value for the same
  thing.
- **Stale** — last touched years ago, still being handed to new staff.
- **Orphans** — nothing references it and it references nothing.
- **What is missing** — measured against a checklist you can edit, because what
  a business ought to have written down depends on the business.

Every finding names the files it came from. There is no score, no percentage,
and no maturity rating, because a number invites an argument about the number
instead of a look at the documents.

## Design notes

- **Read-only on your documents.** The scan opens files in `r` mode and writes
  nothing back to the folder it is pointed at. The index lives in `data/`
  beside the app.
- **`data/` never leaves the machine and is not in this repo.** It is a sqlite
  database of whatever you scanned.
- **Findings show their arithmetic.** Where the report gives a range it shows
  how it got there. It will not state a single-point figure for anything.
- **Nothing is filtered away.** Everything found is kept and ordered. A tool
  that quietly drops what it judged uninteresting narrows what you know without
  telling you.

Longer notes on the ingest edge cases and the scoring live in
[`delivery/bindery/README.md`](delivery/bindery/README.md).

## Layout

```
_shared/            server, storage and design system (vendored — see below)
delivery/bindery/   the app
tests/              247 tests, mirroring the app tree
```

`_shared/` is vendored from a larger private workspace of about twenty of these
apps that share one server base, one storage layer and one visual language. The
two-level `delivery/…` path is not decoration: the folder an app sits in decides
its colour, and the test tree mirrors the app tree exactly. Its comments
occasionally mention sibling apps that are not in this repo.

`tests/test_publishable.py` guards the seam between that private workspace and
this one — it fails if a copied file brings private fixture data back with it.

## Licence

MIT. See [`LICENSE`](LICENSE).
