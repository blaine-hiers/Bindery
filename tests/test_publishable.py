"""The guard on the seam between the private workspace and this public repo.

This app was carved out of a larger private workspace that runs a real
consulting practice. Over there the fixtures are real: a live prospect's
company name, an owner's name, real domains, real street names, the employer's
domain. None of that belongs in a public repository, and all of it was
replaced on the way out.

The problem is that the seam is a file copy. The natural way to pull a fix
across is to copy the file, and a copied file brings the private names back
with it. That is not a thing to remember; it is a thing to test.

So: this walks every text file in the repo and fails if a denied term
reappears anywhere --- source, test, fixture, README, JSON starter. It is the
same shape as the rest of the suite. A rule nobody checks is a rule that has
already been broken once without anyone noticing.

**On the encoding.** The denied terms are base64 rather than plain text, for
the obvious reason that a plain-text list would make this file the exact
searchable index of private names it exists to prevent. It is obfuscation and
not secrecy --- decoding it is one line, and the docstring tells you which one.
Anyone who wants the names can have them; a search engine should not get them
for free.

    py tests/test_publishable.py
"""

from __future__ import annotations

import base64
import re
import unittest
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / "_shared" / "appkit.py").is_file())

# base64.b64decode(_DENIED).decode().split("|")
_DENIED = (
    "cmF5ICYgc29ufHJheSBhbmQgc29ufHJheS1zb258Z3JlZ2d8MTkxNXNvdXRofDE5MTUgc291"
    "dGh8dmFsZG9zdGF8YmF5dHJlZXxhc2hsZXkgc3R8bXJ0Y3V0dGluZ3xtZXRhbCByZW1vdmFs"
    "IHRlY2hub2xvZ3xodXJzdCBib2lsZXJ8YXJjaGJvbGR8dW5zdG9wcGFibGVwaWV8aGFycmVs"
    "bG1hY2hpbmUuY29tfG5vcnRoc2lkZWRlbnRhbC5jb218a2ItYnVpbGRlcnxhaS1yYWRhcnxk"
    "ZW1vLWdlbmVyYXRvcg=="
)
DENIED = base64.b64decode(_DENIED).decode("utf-8").split("|")

TEXT_SUFFIXES = {".py", ".js", ".css", ".html", ".json", ".md", ".txt", ".cmd",
                 ".yml", ".yaml", ".toml", ".cfg", ".ini", ""}
SKIP_DIRS = {".git", "__pycache__", "node_modules", "data", "dist", "reports"}

# RFC 2606 reserves these for documentation. A sample address on any other
# domain is either somebody's real address or a domain somebody really owns.
SAFE_EMAIL_DOMAINS = (".example", "example.com", "example.org", "example.net",
                      "localhost")

EMAIL_RX = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
WINDOWS_HOME_RX = re.compile(r"[Cc]:[\\/]+[Uu]sers[\\/]+(?!<)[A-Za-z0-9._-]+")


def text_files() -> list[Path]:
    out = []
    for f in ROOT.rglob("*"):
        if not f.is_file():
            continue
        if any(part in SKIP_DIRS for part in f.parts):
            continue
        if f.suffix.lower() not in TEXT_SUFFIXES:
            continue
        out.append(f)
    return out


def read(f: Path) -> str:
    try:
        return f.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return ""


class ThereIsSomethingToCheck(unittest.TestCase):
    """If the walk finds nothing, every test below passes for the wrong
    reason. That is the failure mode of every scanner ever written."""

    def test_the_walk_actually_finds_files(self):
        self.assertGreater(len(text_files()), 10)

    def test_it_can_see_this_file(self):
        self.assertIn(Path(__file__).resolve(), text_files())


class NoPrivateNames(unittest.TestCase):
    def test_no_denied_term_appears_anywhere(self):
        me = Path(__file__).resolve()
        hits = []
        for f in text_files():
            if f == me:            # the encoded list is not a hit
                continue
            body = read(f).lower()
            for term in DENIED:
                if term in body:
                    hits.append(f"{f.relative_to(ROOT)}: {term!r}")
        self.assertEqual(hits, [], "private names came back:\n  " + "\n  ".join(hits))


class NoRealAddresses(unittest.TestCase):
    def test_every_sample_email_is_on_a_reserved_domain(self):
        bad = []
        for f in text_files():
            for addr in EMAIL_RX.findall(read(f)):
                low = addr.lower()
                if not any(low.endswith(d) or d in low for d in SAFE_EMAIL_DOMAINS):
                    bad.append(f"{f.relative_to(ROOT)}: {addr}")
        self.assertEqual(bad, [], "sample data uses a real domain:\n  " + "\n  ".join(bad))

    def test_no_absolute_path_names_a_real_user(self):
        """A hardcoded C:\\Users\\<name> leaks whoever built this, and it is the
        single easiest thing to paste in from a working machine."""
        me = Path(__file__).resolve()
        bad = []
        for f in text_files():
            if f == me:
                continue
            for hit in WINDOWS_HOME_RX.findall(read(f)):
                if hit.lower() not in ("user", "username", "you", "youruser"):
                    bad.append(f"{f.relative_to(ROOT)}: C:\\Users\\{hit}")
        self.assertEqual(bad, [], "an absolute home path leaked:\n  " + "\n  ".join(bad))


if __name__ == "__main__":
    unittest.main()
