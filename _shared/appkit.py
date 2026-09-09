"""appkit — the tiny local-app framework the practice apps are built on.

Standard library only. No pip, no internet, no build step.

An app is: a folder with `static/` in it, plus routes registered on an App.
Run it and it picks a free port on localhost, serves the GUI, and opens a browser.

    from appkit import App
    app = App("Workflow Mapper", Path(__file__).parent)

    @app.api("GET", "/api/things")
    def list_things(req):
        return {"things": [...]}          # dict/list -> JSON

    app.run()                             # blocking; Ctrl+C to stop

Design rules this file exists to enforce:
  * Bind to loopback by default. These apps hold client data; a network address
    is never the default and never implicit. An app that wants to be reachable
    from a phone must pass `host=` and say out loud that it did -- see
    `phone/app.py`, which requires a flag and a PIN before it will do it.
  * Every API error returns JSON, never an HTML traceback page. The GUI can then
    show a real message instead of silently doing nothing.
  * No caching of app files, so an edit + refresh is the whole dev loop.
"""

from __future__ import annotations

import json
import mimetypes
import os
import posixpath
import re
import socket
import sys
import threading
import traceback
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse, parse_qs

__all__ = ["App", "Request", "HttpError", "SHARED_STATIC", "function_of",
           "vault_docs", "vault_docs_packed", "VAULT_DOCS_ENV"]

SHARED_STATIC = Path(__file__).resolve().parent / "static"


# ------------------------------------------------------- where the vault is

# Set by `_android/app/src/main/python/android_start.py` to the read-only copy
# of the vault's markdown that ships inside `payload.zip` under `_vault/`.
#
# **Deliberately not `PLANB_VAULT`**, which already exists and means something
# else: `hub/paths.py` reads it as *the writable workspace root* and hangs
# `ai_context_dir()` off it, so pointing that name at the packed copy would
# have the exporter writing a knowledge dump into a folder the next payload
# update deletes -- and which `_carry_device_folders()` does not carry across.
# A read-only answer needs a name that cannot be mistaken for a writable one.
VAULT_DOCS_ENV = "PLANB_VAULT_DOCS"


def _is_vault(folder: Path) -> bool:
    """The documented marker: **both** `Home.md` and `CLAUDE.md`.

    Both, not either. `system/apps/CLAUDE.md` is a real file, so a test for
    CLAUDE.md alone stops one level short at the apps folder and every vault
    path below it resolves against the wrong root -- silently, because that
    folder exists and can be listed.
    """
    try:
        return (folder / "Home.md").is_file() and (folder / "CLAUDE.md").is_file()
    except OSError:
        return False


def _packed_docs() -> Path | None:
    raw = os.environ.get(VAULT_DOCS_ENV, "").strip()
    if not raw:
        return None
    folder = Path(raw).expanduser()
    # Checked, not trusted. A stale or half-unpacked path falls through to the
    # walk rather than making every app report an empty vault.
    return folder.resolve() if _is_vault(folder) else None


def vault_docs(start: Path | None = None) -> Path | None:
    """Where the vault's documents are on **this** device. Read them, never write.

    One question, one answer, two platforms:

      * **On the laptop** there is a real vault above the apps folder, so this
        walks up to it -- never counts parent folders, because `parents[1]` was
        right until an app moved into a function folder and then silently wrong.
      * **On the phone** there is no vault above anything: the apps live in
        `<app files>/payload/` and the documents ride inside that payload at
        `payload/_vault/`. `android_start.py` sets `PLANB_VAULT_DOCS` to it
        before any app starts, and that answer wins.

    Returns None when there is neither -- a portable build on a USB stick, or a
    frozen exe unpacked into a temp folder. None means "say so on screen",
    never "scan nothing and report a clean bill of health".

    **The copy under `_vault/` is read-only and one-way.** Editing markdown on
    two devices is the Drive conflict problem the sync design exists to avoid,
    so nothing here writes and no caller may either.
    """
    packed = _packed_docs()
    if packed is not None:
        return packed
    here = Path(start).resolve() if start is not None else Path(__file__).resolve()
    for folder in [here] + list(here.parents):
        if folder.is_dir() and _is_vault(folder):
            return folder
    return None


def vault_docs_packed() -> bool:
    """True when `vault_docs()` is answering with the copy inside the payload.

    Worth saying out loud on screen. A packed copy is a snapshot taken when the
    payload was built, and a plan document that has moved on since is exactly
    the kind of quiet staleness Plan Clock was written to refuse.
    """
    return _packed_docs() is not None


def function_of(root: Path) -> str:
    """Which part of the business an app belongs to. The folder decides.

    `<apps>/growth/pipeline` is growth. Where an app sits is a fact; what a
    config file claims about it is a claim, so the colour comes from the
    layout and never from apps.json -- a file that is allowed to be missing is
    a colour source that silently falls back to the house green.

    Two answers are deliberately empty:

      * **the hub**, which shows all six functions at once and so owns the
        house style rather than any one function's;
      * **a frozen build**, where this file is unpacked to a temp folder and
        the walk finds no apps root. Colour is decoration; an exe that cannot
        work out its own layout should still start.

    Anything else sitting loose at the apps root comes back "unfiled", which
    has a deliberately drab entry in base.css. An app nobody filed should look
    unfinished, because it is.
    """
    root = Path(root).resolve()
    apps_root = next((p for p in root.parents
                      if (p / "_shared" / "appkit.py").is_file()), None)
    if apps_root is None:
        return ""
    parts = root.relative_to(apps_root).parts
    if not parts:
        return ""
    if len(parts) == 1:
        return "" if parts[0] == "hub" else "unfiled"
    return parts[0]


_HEAD_RX = re.compile(rb"<head[^>]*>", re.IGNORECASE)
_DOCTYPE_RX = re.compile(rb"<!doctype[^>]*>", re.IGNORECASE)

# Windows registry occasionally maps .js to a bogus type; pin the ones we serve.
_MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".map": "application/json; charset=utf-8",
}


class HttpError(Exception):
    """Raise inside a handler to return a clean JSON error to the GUI."""

    def __init__(self, status: int, message: str, detail: Any = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.detail = detail


@dataclass
class Request:
    method: str
    path: str
    params: dict[str, str] = field(default_factory=dict)
    query: dict[str, list[str]] = field(default_factory=dict)
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)

    def q(self, name: str, default: str | None = None) -> str | None:
        """First value of a query-string parameter."""
        vals = self.query.get(name)
        return vals[0] if vals else default

    def qint(self, name: str, default: int) -> int:
        raw = self.q(name)
        if raw is None or raw == "":
            return default
        try:
            return int(raw)
        except ValueError:
            raise HttpError(400, f"'{name}' must be a whole number, got {raw!r}")

    def json(self) -> dict:
        """Body as a dict, or a clean 400 if it is not one."""
        if not isinstance(self.body, dict):
            raise HttpError(400, "Expected a JSON object in the request body")
        return self.body

    def require(self, *names: str) -> tuple:
        """Pull required fields out of a JSON body, erroring by name."""
        data = self.json()
        missing = [n for n in names if data.get(n) in (None, "")]
        if missing:
            raise HttpError(400, "Missing required field(s): " + ", ".join(missing))
        return tuple(data[n] for n in names)


_PARAM = re.compile(r"<([a-zA-Z_][a-zA-Z0-9_]*)>")


def _compile(pattern: str) -> re.Pattern:
    """'/api/docs/<id>' -> regex with a named group."""
    escaped = re.escape(pattern)
    for name in _PARAM.findall(pattern):
        escaped = escaped.replace(re.escape(f"<{name}>"), f"(?P<{name}>[^/]+)")
    return re.compile("^" + escaped + "$")


class App:
    def __init__(self, name: str, root: Path, *, index: str = "index.html",
                 host: str = "127.0.0.1"):
        self.name = name
        self.root = Path(root).resolve()
        self.static_dir = self.root / "static"
        self.index = index
        self.host = host
        self.function = function_of(self.root)
        self._routes: list[tuple[str, re.Pattern, Callable]] = []
        self._server: ThreadingHTTPServer | None = None
        self.on_start: list[Callable[[], None]] = []

        # Escape hatch for a request this framework's two categories -- JSON API
        # and static file -- cannot express. It runs before both and returns
        # either None ("not mine, carry on") or a full (status, headers, body).
        #
        # It exists for one real case: the phone shell proxies whole responses
        # from the other apps, headers and content type included, and those are
        # neither its own JSON nor its own files. Kept as one hook rather than a
        # plugin system, because there is exactly one caller.
        self.raw_handler: Callable[..., tuple[int, dict, bytes] | None] | None = None

        # Boot beacon. UI.booted() posts here when the page finishes starting,
        # and ui.js posts here again if anything throws afterwards. This is how
        # smoke_test.py proves the GUI actually rendered in a real browser --
        # far better evidence than scraping a DOM dump, because it reports the
        # page's own runtime errors rather than guessing from markup.
        self.boot_report: dict | None = None

        @self.api("POST", "/api/_boot")
        def _boot_post(req: Request):
            data = req.body if isinstance(req.body, dict) else {}
            self.boot_report = {
                "ok": bool(data.get("ok")),
                "errors": data.get("errors") or [],
                "url": data.get("url", ""),
                "at": __import__("time").time(),
            }
            return {"received": True}

        @self.api("GET", "/api/_boot")
        def _boot_get(req: Request):
            return self.boot_report or {"ok": False, "pending": True}

    # ---- routing -------------------------------------------------------

    def api(self, method: str, pattern: str):
        """Register a JSON handler. Return a dict/list; it gets serialised."""

        def deco(fn: Callable):
            self._routes.append((method.upper(), _compile(pattern), fn))
            return fn

        return deco

    def dispatch(self, req: Request):
        """Match a request to a handler. Exposed so tests can bypass HTTP."""
        matched_path = False
        for method, rx, fn in self._routes:
            m = rx.match(req.path)
            if not m:
                continue
            matched_path = True
            if method != req.method:
                continue
            req.params = {k: unquote(v) for k, v in m.groupdict().items()}
            return fn(req)
        if matched_path:
            raise HttpError(405, f"{req.method} not allowed on {req.path}")
        raise HttpError(404, f"No such endpoint: {req.path}")

    # ---- static --------------------------------------------------------

    def _resolve_static(self, url_path: str) -> Path | None:
        """Map a URL to a file on disk, refusing anything outside the roots."""
        clean = posixpath.normpath(unquote(url_path))
        if clean.startswith("/_shared/"):
            base, rel = SHARED_STATIC, clean[len("/_shared/") :]
        else:
            base, rel = self.static_dir, clean.lstrip("/")
        if not rel or rel.endswith("/"):
            rel = (rel or "") + self.index
        target = (base / rel).resolve()
        try:
            target.relative_to(base.resolve())
        except ValueError:
            return None  # traversal attempt
        return target if target.is_file() else None

    # ---- html decoration ------------------------------------------------

    def decorate_html(self, body: bytes) -> bytes:
        """Stamp the app's function onto <html>, so base.css can colour it.

        A one-line script rather than writing `data-function` into each page's
        `<html>` tag, because not every page here has one -- bindery serves
        a bare fragment and leans on the implicit head every browser builds,
        which is legal HTML and works fine.

        Not a fetch from ui.js either, for two reasons: under the phone shell
        `/_shared/` is served by the *shell*, so a shared script would report
        the shell's function for every app it mounts; and a fetch paints blue
        first and repaints a moment later, which reads as a bug.

        It goes after any doctype -- a doctype that is no longer the first
        thing in the document throws the page into quirks mode -- and inside
        <head> when there is one, ahead of the stylesheet, so the colour is
        already set the first time anything is painted.
        """
        if not self.function:
            return body
        tag = ('<script>(function(d){d.setAttribute("data-function","%s")})'
               '(document.documentElement)</script>' % self.function).encode("ascii")
        m = _HEAD_RX.search(body)
        if m:
            return body[:m.end()] + tag + body[m.end():]
        d = _DOCTYPE_RX.match(body.lstrip()[:120])
        if d:
            at = body.index(b">", body.lower().index(b"<!doctype")) + 1
            return body[:at] + tag + body[at:]
        return tag + body

    # ---- lifecycle -----------------------------------------------------

    def build_server(self, port: int = 0, host: str | None = None,
                     track: bool = True) -> ThreadingHTTPServer:
        """`track=False` builds a listener without claiming `self._server`.

        An app normally has one. The phone shell can have two at once -- the
        loopback one it started with, plus a second on the network address when
        wifi access is switched on -- and the first must stay the one that
        `run()` is blocking on.
        """
        app = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "appkit"
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):  # quiet by default
                if app_verbose[0]:
                    sys.stderr.write("  %s\n" % (fmt % args))

            # -- helpers
            def _send(self, status: int, body: bytes, ctype: str,
                      extra: dict | None = None):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                sent = {"content-type", "content-length"}
                for k, v in (extra or {}).items():
                    if k.lower() in sent:
                        continue
                    sent.add(k.lower())
                    self.send_header(k, v)
                if "cache-control" not in sent:
                    self.send_header("Cache-Control", "no-store")
                if "x-content-type-options" not in sent:
                    self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _json(self, status: int, payload: Any):
                body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
                self._send(status, body, "application/json; charset=utf-8")

            def _handle(self, method: str):
                parsed = urlparse(self.path)
                path = parsed.path

                # Read the body before anything dispatches, so the raw handler
                # and the JSON path see the same bytes and neither can leave an
                # unread body sitting in a keep-alive socket.
                raw_body = b""
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    raw_body = self.rfile.read(length)

                if app.raw_handler is not None:
                    hdrs = {k.lower(): v for k, v in self.headers.items()}
                    try:
                        res = app.raw_handler(method, path, parsed.query, hdrs,
                                              raw_body, self.client_address[0])
                    except HttpError as e:
                        self._json(e.status, {"error": e.message, "detail": e.detail})
                        return
                    except Exception as e:
                        traceback.print_exc()
                        self._json(500, {"error": f"{type(e).__name__}: {e}"})
                        return
                    if res is not None:
                        status, headers, body = res
                        ctype = headers.get("Content-Type") or \
                            headers.get("content-type") or "application/octet-stream"
                        self._send(status, body, ctype, extra=headers)
                        return

                # static first — it is the common case and cannot throw
                if not path.startswith("/api/"):
                    f = app._resolve_static(path)
                    if f is not None:
                        ctype = _MIME.get(f.suffix.lower()) or (
                            mimetypes.guess_type(f.name)[0] or "application/octet-stream"
                        )
                        try:
                            payload = f.read_bytes()
                            if ctype.startswith("text/html"):
                                payload = app.decorate_html(payload)
                            self._send(200, payload, ctype)
                        except OSError as e:
                            self._send(500, str(e).encode(), "text/plain; charset=utf-8")
                        return
                    self._send(404, b"Not found", "text/plain; charset=utf-8")
                    return

                body = None
                if raw_body:
                    try:
                        body = json.loads(raw_body.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        self._json(400, {"error": "Request body was not valid JSON"})
                        return

                req = Request(
                    method=method,
                    path=path,
                    query=parse_qs(parsed.query),
                    body=body,
                    headers={k.lower(): v for k, v in self.headers.items()},
                )
                try:
                    result = app.dispatch(req)
                except HttpError as e:
                    self._json(e.status, {"error": e.message, "detail": e.detail})
                except Exception as e:  # never leak an HTML traceback to the GUI
                    traceback.print_exc()
                    self._json(500, {"error": f"{type(e).__name__}: {e}"})
                else:
                    if isinstance(result, tuple) and len(result) == 2:
                        status, payload = result
                    else:
                        status, payload = 200, result
                    self._json(status, payload)

            def do_GET(self):
                self._handle("GET")

            def do_HEAD(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_PUT(self):
                self._handle("PUT")

            def do_PATCH(self):
                self._handle("PATCH")

            def do_DELETE(self):
                self._handle("DELETE")

        app_verbose = [False]
        self._verbose_flag = app_verbose
        srv = ThreadingHTTPServer((host or self.host, port), Handler)
        srv.daemon_threads = True
        if track:
            self._server = srv
        return srv

    def serve_background(self, port: int = 0, host: str | None = None
                         ) -> tuple[ThreadingHTTPServer, str]:
        """Start on a background thread. Returns (server, base_url). For tests."""
        srv = self.build_server(port, host)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv, f"http://127.0.0.1:{srv.server_address[1]}"

    def run(self, port: int = 0, open_browser: bool = True, verbose: bool = False,
            host: str | None = None, banner: Callable[[str], None] | None = None):
        """Start and block. Env overrides exist so the smoke test can drive any app:
        APPKIT_PORT=8123   APPKIT_NO_BROWSER=1   APPKIT_VERBOSE=1

        `host` overrides the bind address for this run; `banner` replaces the
        "Local only" block, because an app reachable from a phone must not print
        a reassurance that has stopped being true.
        """
        env_port = os.environ.get("APPKIT_PORT")
        if env_port:
            try:
                port = int(env_port)
            except ValueError:
                pass
        if os.environ.get("APPKIT_NO_BROWSER"):
            open_browser = False
        if os.environ.get("APPKIT_VERBOSE"):
            verbose = True

        for hook in self.on_start:
            hook()
        srv = self.build_server(port, host)
        self._verbose_flag[0] = verbose
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        if banner is not None:
            banner(url)
        else:
            line = f"  {self.name}  ->  {url}  "
            print("\n" + "=" * len(line))
            print(line)
            print("=" * len(line))
            print("  Local only. Nothing leaves this machine.")
            print("  Ctrl+C to stop.\n")
        if open_browser:
            threading.Timer(0.4, lambda: webbrowser.open(url)).start()
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\n  Stopped. Your work was saved as you went.\n")
        finally:
            srv.shutdown()
            srv.server_close()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
