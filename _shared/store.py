"""store — a tiny thread-safe JSON document store on sqlite.

Standard library only.

Why sqlite and not a JSON file: these apps autosave on every keystroke-ish
event, and a JSON file rewritten that often will eventually be truncated by a
crash or a Ctrl+C at the wrong moment. Losing an owner's workflow map in front
of the owner is not a recoverable situation. sqlite gives durability for free.

    st = Store(Path("data.db"))
    doc = st.save("maps", {"name": "Northgate Mechanical"})     # gets an id + timestamps
    st.get("maps", doc["id"])
    st.all("maps")                                    # newest first
    st.delete("maps", doc["id"])
"""

from __future__ import annotations

import gc
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

__all__ = ["Store", "Conflict", "new_id", "now_ms"]


class Conflict(Exception):
    """Someone else changed this document since you loaded it.

    Raised by save(..., expect_updated=N). The caller must decide which copy
    wins -- this layer will not silently discard a person's work.
    """

    def __init__(self, message: str, current: dict | None = None):
        super().__init__(message)
        self.current = current


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def now_ms() -> int:
    return int(time.time() * 1000)


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.recovered_from: Path | None = None
        try:
            self._open()
        except sqlite3.DatabaseError:
            # A truncated or corrupt file must not stop the app from starting.
            # Move it aside -- never delete it, it may still be recoverable by
            # hand -- and come up clean so the user can work and be told where
            # the old one went.
            #
            # The dead connection has to be closed explicitly first: sqlite3
            # opens the file before it discovers the contents are not a
            # database, and Windows will not rename a file that is still open.
            try:
                self._db.close()
            except Exception:
                pass
            self._db = None
            gc.collect()

            stamp = time.strftime("%Y%m%d-%H%M%S")
            spoiled = self.path.with_name(self.path.name + f".unreadable-{stamp}")
            try:
                self.path.replace(spoiled)
            except OSError as e:
                # Don't retry into the same failure and raise something
                # confusing -- say plainly what is wrong and what to do.
                raise sqlite3.DatabaseError(
                    f"{self.path} is not a readable database and it could not be "
                    f"moved aside ({e}). Close anything using it, or rename it "
                    f"by hand, then start the app again."
                ) from e
            self.recovered_from = spoiled
            self._open()

    def _open(self) -> None:
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS docs (
                       collection TEXT NOT NULL,
                       id         TEXT NOT NULL,
                       data       TEXT NOT NULL,
                       created    INTEGER NOT NULL,
                       updated    INTEGER NOT NULL,
                       PRIMARY KEY (collection, id)
                   )"""
            )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS docs_updated ON docs(collection, updated DESC)"
            )
            self._db.commit()

    # ---- writes --------------------------------------------------------

    def save(self, collection: str, doc: dict, expect_version: int | None = None) -> dict:
        """Insert or update. Returns the stored document, id/stamps/version filled.

        Every save bumps doc["version"]. Pass expect_version with the version you
        last read to get compare-and-swap: if someone else wrote in between, this
        raises Conflict instead of quietly destroying their edits. Two browser
        windows on one document is the ordinary way that happens.

        Version is a counter, deliberately, not the `updated` timestamp --
        now_ms() only has millisecond resolution, so two saves inside the same
        millisecond are indistinguishable and a timestamp check silently passes
        exactly when it matters most. (Found by a test that should have failed
        and didn't.)
        """
        if not isinstance(doc, dict):
            raise TypeError("documents must be dicts")
        doc = dict(doc)
        doc.setdefault("id", new_id())
        ts = now_ms()
        with self._lock:
            row = self._db.execute(
                "SELECT created, updated, data FROM docs WHERE collection=? AND id=?",
                (collection, doc["id"]),
            ).fetchone()
            stored = json.loads(row["data"]) if row else None
            current_version = int(stored.get("version", 0)) if stored else 0
            if expect_version is not None and stored is not None:
                if current_version != int(expect_version):
                    raise Conflict(
                        "This was changed somewhere else since you opened it.",
                        stored,
                    )
            doc["version"] = current_version + 1
            created = row["created"] if row else ts
            doc["created"], doc["updated"] = created, ts
            self._db.execute(
                "INSERT INTO docs(collection,id,data,created,updated) VALUES(?,?,?,?,?) "
                "ON CONFLICT(collection,id) DO UPDATE SET data=excluded.data, updated=excluded.updated",
                (collection, doc["id"], json.dumps(doc, ensure_ascii=False), created, ts),
            )
            self._db.commit()
        return doc

    def save_many(self, collection: str, docs: Iterable[dict]) -> list[dict]:
        return [self.save(collection, d) for d in docs]

    def patch(self, collection: str, doc_id: str, changes: dict) -> dict | None:
        """Merge changes into an existing doc. None if it does not exist."""
        with self._lock:
            current = self.get(collection, doc_id)
            if current is None:
                return None
            current.update(changes)
            current["id"] = doc_id
            return self.save(collection, current)

    def delete(self, collection: str, doc_id: str) -> bool:
        with self._lock:
            cur = self._db.execute(
                "DELETE FROM docs WHERE collection=? AND id=?", (collection, doc_id)
            )
            self._db.commit()
            return cur.rowcount > 0

    def clear(self, collection: str) -> int:
        with self._lock:
            cur = self._db.execute("DELETE FROM docs WHERE collection=?", (collection,))
            self._db.commit()
            return cur.rowcount

    # ---- reads ---------------------------------------------------------

    def get(self, collection: str, doc_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT data FROM docs WHERE collection=? AND id=?", (collection, doc_id)
            ).fetchone()
        return json.loads(row["data"]) if row else None

    def all(self, collection: str, limit: int | None = None) -> list[dict]:
        sql = "SELECT data FROM docs WHERE collection=? ORDER BY updated DESC"
        args: tuple[Any, ...] = (collection,)
        if limit is not None:
            sql += " LIMIT ?"
            args += (limit,)
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [json.loads(r["data"]) for r in rows]

    def count(self, collection: str) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) AS n FROM docs WHERE collection=?", (collection,)
            ).fetchone()
        return int(row["n"])

    # ---- housekeeping --------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
