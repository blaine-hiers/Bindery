"""Knowledge Base Builder — point it at a folder of a client's documents.

    py app.py

It reads the folder, indexes every word, and gives you two things:

  * a search box that finds anything in the pile in under a second, and
  * a report on what is wrong with the pile — what is duplicated, what is
    stale, what nothing points at, what nobody can open, and what a business
    that size should have written down and does not.

The search box is the demo. The report is the finding you get paid for.

Nothing leaves this machine. Nothing in the client's folder is written to,
moved, or renamed — every file is opened read-only, once.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(next(p / "_shared" for p in Path(__file__).resolve().parents
                            if (p / "_shared" / "appkit.py").is_file())))

from appkit import App, HttpError, Request          # noqa: E402
from store import Store, new_id, now_ms             # noqa: E402

import analyze as gap                               # noqa: E402
import ingest                                       # noqa: E402
from index import SearchIndex                       # noqa: E402

HERE = Path(__file__).resolve().parent
# Tests point this somewhere temporary; normal runs keep it beside the app.
DATA = Path(os.environ.get("BINDERY_DATA") or (HERE / "data"))

app = App("Knowledge Base Builder", HERE)
store = Store(DATA / "kb.db")
index = SearchIndex(DATA / "search.db")

KBS = "kbs"


# ---------------------------------------------------------------- ingest jobs

class Job:
    """One folder scan, running on its own thread so the GUI stays alive."""

    def __init__(self, kb_id: str, folder: str):
        self.kb_id = kb_id
        self.folder = folder
        self.lock = threading.Lock()
        self._cancel = False
        # Set when the knowledge base itself is deleted underneath a running
        # scan. Without it the thread keeps writing rows into an index for a
        # knowledge base that no longer exists, and nothing ever cleans them up.
        self.abandoned = False
        self.thread: threading.Thread | None = None
        self.state = {
            "running": True, "phase": "Starting", "done": 0, "total": 0,
            "current": "", "started": now_ms(), "finished": None,
            "error": None, "cancelled": False, "summary": None, "folder": folder,
        }

    def cancel(self):
        self._cancel = True

    def abandon(self):
        self.abandoned = True
        self._cancel = True

    def cancelled(self) -> bool:
        return self._cancel

    def set(self, **changes):
        with self.lock:
            self.state.update(changes)

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.state)


JOBS: dict[str, Job] = {}
REPORT_CACHE: dict[str, tuple[int, dict]] = {}


def _run_ingest(job: Job):
    kb_id, folder = job.kb_id, job.folder
    written = [0]
    try:
        known = index.fingerprints(kb_id)

        def on_progress(p):
            job.set(phase=p["phase"], done=p["done"], total=p["total"],
                    current=p.get("current", ""))

        def on_batch(records):
            if job.abandoned:            # the knowledge base was deleted under us
                return
            written[0] += index.add(kb_id, records)

        summary = ingest.ingest_folder(folder, known=known, on_progress=on_progress,
                                       cancel=job.cancelled, on_batch=on_batch)
        if job.abandoned:
            # Whatever landed before the delete took effect goes with it.
            index.drop_kb(kb_id)
            REPORT_CACHE.pop(kb_id, None)
            job.set(running=False, finished=now_ms(), cancelled=True, phase="Deleted")
            return
        if not summary["cancelled"]:
            job.set(phase="Tidying up")
            summary["removed"] = index.keep_only(kb_id, summary["seen"])
        else:
            summary["removed"] = 0
        summary["written"] = written[0]
        summary.pop("seen", None)
        summary.pop("records", None)

        stats = index.stats(kb_id)
        kb = store.get(KBS, kb_id)
        if kb:
            kb["folder"] = folder
            kb["last_scan"] = now_ms()
            kb["stats"] = stats
            kb["last_summary"] = summary
            # A stopped scan leaves a knowledge base holding part of a folder.
            # Everything downstream — the report, the score, the markdown — has
            # to say so, or an advisor hands a client a finding drawn from a
            # tenth of their paperwork.
            kb["partial"] = bool(summary["cancelled"])
            store.save(KBS, kb)
        REPORT_CACHE.pop(kb_id, None)
        job.set(running=False, finished=now_ms(), summary=summary,
                cancelled=summary["cancelled"],
                phase="Stopped" if summary["cancelled"] else "Done")
    except FileNotFoundError:
        job.set(running=False, finished=now_ms(), error=f"That folder does not exist: {folder}",
                phase="Failed")
    except NotADirectoryError:
        job.set(running=False, finished=now_ms(), error=f"That is a file, not a folder: {folder}",
                phase="Failed")
    except Exception as exc:                                     # never lose the thread silently
        job.set(running=False, finished=now_ms(),
                error=f"{type(exc).__name__}: {exc}", phase="Failed")


# ---------------------------------------------------------------- helpers

def _kb_or_404(kb_id: str) -> dict:
    kb = store.get(KBS, kb_id)
    if not kb:
        raise HttpError(404, "That knowledge base is gone. Pick another one on the left.")
    return kb


def _clean_folder(raw) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise HttpError(400, "Type the folder you want read, for example C:\\Clients\\Northgate-Mechanical")
    folder = os.path.expandvars(os.path.expanduser(raw.strip().strip('"')))
    p = Path(folder)
    try:
        exists, is_dir = p.exists(), p.is_dir()
    except OSError as exc:
        raise HttpError(400, f"That path could not be checked: {exc.strerror or exc}")
    if not exists:
        raise HttpError(400, f"There is no folder at {folder}")
    if not is_dir:
        raise HttpError(400, f"That is a file, not a folder: {folder}")
    return str(p)


def _kb_view(kb: dict) -> dict:
    job = JOBS.get(kb["id"])
    return {
        "id": kb["id"], "name": kb.get("name", "Untitled"),
        "folder": kb.get("folder", ""), "notes": kb.get("notes", ""),
        "last_scan": kb.get("last_scan"), "stats": kb.get("stats") or {},
        "created": kb.get("created"), "updated": kb.get("updated"),
        "checklist": kb.get("checklist") or _default_checklist(),
        "scanning": bool(job and job.snapshot()["running"]),
        "partial": bool(kb.get("partial")),
    }


def _default_checklist() -> list[dict]:
    return [{"key": c["key"], "label": c["label"], "why": c["why"],
             "phrases": list(c["phrases"]), "on": True} for c in gap.DEFAULT_CHECKLIST]


def _report_for(kb: dict) -> dict:
    kb_id = kb["id"]
    stamp = int(kb.get("last_scan") or 0)
    cached = REPORT_CACHE.get(kb_id)
    if cached and cached[0] == stamp:
        return cached[1]
    docs = index.docs_with_text(kb_id)
    # `checklist` may legitimately be an empty list — the owner turned every
    # item off — and `analyze` distinguishes that from "use the built-in list".
    checklist = kb.get("checklist")
    if checklist is None:
        checklist = _default_checklist()
    result = gap.analyze(docs, checklist=checklist, partial=bool(kb.get("partial")))
    REPORT_CACHE[kb_id] = (stamp, result)
    return result


# ---------------------------------------------------------------- knowledge bases

@app.api("GET", "/api/kbs")
def list_kbs(req: Request):
    rows = [_kb_view(k) for k in store.all(KBS)]
    rows.sort(key=lambda k: -(k.get("updated") or 0))
    return {"kbs": rows}


@app.api("POST", "/api/kbs")
def create_kb(req: Request):
    data = req.json()
    name = (data.get("name") or "").strip() or "New knowledge base"
    kb = store.save(KBS, {
        "id": new_id(), "name": name[:120], "folder": (data.get("folder") or "").strip(),
        "notes": "", "stats": {}, "last_scan": None, "checklist": _default_checklist(),
    })
    return 201, {"kb": _kb_view(kb)}


@app.api("GET", "/api/kbs/<kb_id>")
def get_kb(req: Request):
    return {"kb": _kb_view(_kb_or_404(req.params["kb_id"]))}


@app.api("PUT", "/api/kbs/<kb_id>")
def update_kb(req: Request):
    kb = _kb_or_404(req.params["kb_id"])
    data = req.json()
    if "name" in data:
        name = (data.get("name") or "").strip()
        if not name:
            raise HttpError(400, "A knowledge base needs a name.")
        kb["name"] = name[:120]
    if "notes" in data:
        kb["notes"] = str(data.get("notes") or "")[:20000]
    if "folder" in data:
        kb["folder"] = str(data.get("folder") or "").strip()
    if "checklist" in data:
        items = data.get("checklist")
        if not isinstance(items, list):
            raise HttpError(400, "The checklist has to be a list of items.")
        cleaned = []
        for item in items:
            if not isinstance(item, dict) or not (item.get("label") or "").strip():
                continue
            phrases = item.get("phrases")
            if isinstance(phrases, str):
                phrases = [p.strip() for p in phrases.split(",")]
            if not isinstance(phrases, list):
                phrases = []
            phrases = [str(p).strip() for p in phrases if str(p).strip()]
            cleaned.append({
                "key": str(item.get("key") or new_id())[:40],
                "label": str(item["label"]).strip()[:120],
                "why": str(item.get("why") or "")[:400],
                "phrases": phrases[:40] or [str(item["label"]).strip().casefold()],
                "on": bool(item.get("on", True)),
            })
        kb["checklist"] = cleaned
        REPORT_CACHE.pop(kb["id"], None)
    store.save(KBS, kb)
    return {"kb": _kb_view(kb)}


@app.api("DELETE", "/api/kbs/<kb_id>")
def delete_kb(req: Request):
    kb_id = req.params["kb_id"]
    _kb_or_404(kb_id)
    job = JOBS.get(kb_id)
    if job:
        # `abandon` rather than `cancel`: the running thread must also stop
        # writing, and clear up after itself, instead of quietly refilling the
        # index we are about to empty.
        job.abandon()
        if job.thread is not None:
            job.thread.join(timeout=8.0)
    index.drop_kb(kb_id)
    store.delete(KBS, kb_id)
    REPORT_CACHE.pop(kb_id, None)
    JOBS.pop(kb_id, None)
    return {"deleted": kb_id}


# ---------------------------------------------------------------- scanning

@app.api("POST", "/api/kbs/<kb_id>/scan")
def start_scan(req: Request):
    kb = _kb_or_404(req.params["kb_id"])
    data = req.body if isinstance(req.body, dict) else {}
    folder = _clean_folder(data.get("folder") or kb.get("folder"))

    running = JOBS.get(kb["id"])
    if running and running.snapshot()["running"]:
        if running.cancelled():
            # You pressed Stop and then Read again. The old thread is finishing
            # the file it was on; give it a moment rather than telling you the
            # folder is "already being read", which is the opposite of true.
            if running.thread is not None:
                running.thread.join(timeout=8.0)
            if running.snapshot()["running"]:
                raise HttpError(409, "It is still stopping. Give it a second and "
                                     "press Read again.")
        else:
            raise HttpError(409, "That knowledge base is already being read. "
                                 "Let it finish or stop it.")

    kb["folder"] = folder
    store.save(KBS, kb)

    job = Job(kb["id"], folder)
    JOBS[kb["id"]] = job
    job.thread = threading.Thread(target=_run_ingest, args=(job,), daemon=True)
    job.thread.start()
    return 202, {"started": True, "folder": folder}


@app.api("GET", "/api/kbs/<kb_id>/scan")
def scan_progress(req: Request):
    kb = _kb_or_404(req.params["kb_id"])
    job = JOBS.get(kb["id"])
    if not job:
        return {"progress": {"running": False, "phase": "Not started", "done": 0,
                             "total": 0, "current": "", "summary": kb.get("last_summary"),
                             "error": None, "cancelled": False}}
    return {"progress": job.snapshot()}


@app.api("DELETE", "/api/kbs/<kb_id>/scan")
def stop_scan(req: Request):
    _kb_or_404(req.params["kb_id"])
    job = JOBS.get(req.params["kb_id"])
    if not job or not job.snapshot()["running"]:
        return {"stopped": False, "why": "Nothing was running."}
    job.cancel()
    return {"stopped": True}


@app.api("POST", "/api/kbs/<kb_id>/clear")
def clear_kb(req: Request):
    kb = _kb_or_404(req.params["kb_id"])
    index.drop_kb(kb["id"])
    kb["stats"] = {}
    kb["last_scan"] = None
    kb["last_summary"] = None
    kb["partial"] = False
    store.save(KBS, kb)
    REPORT_CACHE.pop(kb["id"], None)
    return {"cleared": True}


# ---------------------------------------------------------------- search

@app.api("GET", "/api/kbs/<kb_id>/search")
def search(req: Request):
    kb = _kb_or_404(req.params["kb_id"])
    q = req.q("q", "") or ""
    limit = max(1, min(200, req.qint("limit", 40)))
    offset = max(0, req.qint("offset", 0))
    started = time.perf_counter()
    result = index.search(kb["id"], q, limit=limit, offset=offset)
    result["ms"] = round((time.perf_counter() - started) * 1000, 1)
    result["indexed"] = index.doc_count(kb["id"])
    return result


@app.api("GET", "/api/kbs/<kb_id>/docs")
def list_docs(req: Request):
    kb = _kb_or_404(req.params["kb_id"])
    status = req.q("status", "") or ""
    limit = max(1, min(2000, req.qint("limit", 300)))
    docs = index.all_docs(kb["id"])
    if status:
        docs = [d for d in docs if d["status"] == status]
    docs.sort(key=lambda d: -int(d["mtime"] or 0))
    return {"total": len(docs), "docs": docs[:limit]}


@app.api("GET", "/api/kbs/<kb_id>/docs/<doc_id>")
def get_doc(req: Request):
    kb = _kb_or_404(req.params["kb_id"])
    doc = index.get_doc(kb["id"], req.params["doc_id"])
    if not doc:
        raise HttpError(404, "That file is not in this knowledge base any more.")
    text = doc.pop("text", "") or ""
    doc["text"] = text[:400_000]
    doc["truncated"] = len(text) > 400_000
    doc["exists"] = Path(doc["path"]).exists()
    return {"doc": doc}


@app.api("POST", "/api/kbs/<kb_id>/docs/<doc_id>/reveal")
def reveal(req: Request):
    """Open the containing folder with the file selected. Read-only — this looks
    at the client's folder, it never touches it."""
    kb = _kb_or_404(req.params["kb_id"])
    doc = index.get_doc(kb["id"], req.params["doc_id"])
    if not doc:
        raise HttpError(404, "That file is not in this knowledge base any more.")
    target = Path(doc["path"])
    if not target.exists():
        raise HttpError(404, f"The file is no longer at {target}")
    try:
        if sys.platform.startswith("win"):
            subprocess.Popen(["explorer", "/select,", str(target)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target.parent)])
    except Exception as exc:
        raise HttpError(500, f"Could not open the folder: {type(exc).__name__}")
    return {"revealed": str(target)}


# ---------------------------------------------------------------- the gap report

@app.api("GET", "/api/kbs/<kb_id>/report")
def report(req: Request):
    kb = _kb_or_404(req.params["kb_id"])
    if index.doc_count(kb["id"]) == 0:
        raise HttpError(400, "Read a folder first — there is nothing to report on yet.")
    return {"report": _report_for(kb), "kb": _kb_view(kb)}


@app.api("GET", "/api/kbs/<kb_id>/report-markdown")
def report_md(req: Request):
    kb = _kb_or_404(req.params["kb_id"])
    if index.doc_count(kb["id"]) == 0:
        raise HttpError(400, "Read a folder first — there is nothing to report on yet.")
    result = _report_for(kb)
    return {"markdown": gap.report_markdown(kb.get("name", "Client"),
                                            kb.get("folder", ""), result),
            "filename": _slug(kb.get("name", "client")) + "-document-review.md"}


def _slug(text: str) -> str:
    import re as _re
    return _re.sub(r"[^a-z0-9]+", "-", (text or "").casefold()).strip("-") or "client"


@app.api("GET", "/api/browse")
def browse(req: Request):
    """List folders so the GUI can offer a picker. Reads names only, nothing else."""
    raw = (req.q("path", "") or "").strip()
    if not raw:
        places = []
        if sys.platform.startswith("win"):
            for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
                root = Path(f"{letter}:\\")
                try:
                    if root.exists():
                        places.append({"name": f"{letter}:\\", "path": str(root)})
                except OSError:
                    pass
        home = Path.home()
        for name in ("Documents", "Desktop", "Downloads", "OneDrive"):
            p = home / name
            try:
                if p.is_dir():
                    places.append({"name": name, "path": str(p)})
            except OSError:
                pass
        places.insert(0, {"name": str(home), "path": str(home)})
        return {"path": "", "parent": None, "folders": places, "roots": True}

    here = Path(os.path.expandvars(os.path.expanduser(raw.strip('"'))))
    try:
        if not here.is_dir():
            raise HttpError(400, f"There is no folder at {here}")
        folders = []
        for entry in sorted(os.scandir(here), key=lambda e: e.name.lower()):
            try:
                if entry.is_dir(follow_symlinks=False) and not entry.name.startswith("$"):
                    folders.append({"name": entry.name, "path": entry.path})
            except OSError:
                continue
    except PermissionError:
        raise HttpError(400, f"You do not have permission to look inside {here}")
    except OSError as exc:
        raise HttpError(400, f"That folder could not be listed: {exc.strerror or exc}")
    parent = str(here.parent) if here.parent != here else None
    return {"path": str(here), "parent": parent, "folders": folders[:500], "roots": False}


@app.api("GET", "/api/health")
def health(req: Request):
    return {"ok": True, "app": app.name, "kbs": store.count(KBS)}


def _seed():
    """First run only: one empty knowledge base so the app is never a blank wall."""
    if store.count(KBS) == 0:
        store.save(KBS, {"id": new_id(), "name": "First client", "folder": "",
                         "notes": "", "stats": {}, "last_scan": None,
                         "checklist": _default_checklist()})


app.on_start.append(_seed)


if __name__ == "__main__":
    app.run(verbose="-v" in sys.argv)
