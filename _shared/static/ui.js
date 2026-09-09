/* ui.js — shared widgets and helpers for the practice apps.
   Plain script, no modules, no build step, no dependencies.
   Everything hangs off window.UI. */

(function () {
  "use strict";

  // ---------- tiny DOM helpers ----------

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      for (var k in attrs) {
        if (!Object.prototype.hasOwnProperty.call(attrs, k)) continue;
        var v = attrs[k];
        if (v === null || v === undefined || v === false) continue;
        if (k === "class") node.className = v;
        else if (k === "text") node.textContent = v;
        else if (k === "html") node.innerHTML = v;
        else if (k === "style" && typeof v === "object") Object.assign(node.style, v);
        else if (k.slice(0, 2) === "on" && typeof v === "function")
          node.addEventListener(k.slice(2).toLowerCase(), v);
        else if (k === "dataset") { for (var d in v) node.dataset[d] = v[d]; }
        else node.setAttribute(k, v === true ? "" : v);
      }
    }
    (children || []).forEach(function (c) {
      if (c === null || c === undefined || c === false) return;
      node.appendChild(typeof c === "string" || typeof c === "number"
        ? document.createTextNode(String(c)) : c);
    });
    return node;
  }

  var $  = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  };

  function clear(node) { while (node && node.firstChild) node.removeChild(node.firstChild); }

  /** Escape for safe interpolation into innerHTML. Use it. */
  function esc(s) {
    return String(s === null || s === undefined ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  // ---------- theme ----------

  var THEME_KEY = "appkit.theme";
  function theme(next) {
    if (next) {
      document.documentElement.setAttribute("data-theme", next);
      try { localStorage.setItem(THEME_KEY, next); } catch (e) {}
      return next;
    }
    return document.documentElement.getAttribute("data-theme") || "dark";
  }
  function toggleTheme() {
    var next = theme(theme() === "dark" ? "light" : "dark");
    // Let any theme buttons on the page repaint themselves.
    window.dispatchEvent(new CustomEvent("appkit:theme", { detail: next }));
    return next;
  }
  function initTheme() {
    var saved = "dark";
    try { saved = localStorage.getItem(THEME_KEY) || "dark"; } catch (e) {}
    theme(saved === "light" ? "light" : "dark");
  }
  initTheme();  // run immediately, before paint, so there is no flash

  function themeButton() {
    var b = el("button", { class: "btn icon ghost", title: "Light / dark  (Ctrl+J)",
                           "aria-label": "Toggle theme" }, []);
    function paint() { b.textContent = theme() === "dark" ? "◑" : "◐"; }
    b.addEventListener("click", toggleTheme);
    window.addEventListener("appkit:theme", paint);
    paint();
    return b;
  }

  // ---------- toasts ----------

  function toastHost() {
    var h = document.getElementById("toasts");
    if (!h) { h = el("div", { id: "toasts" }, []); document.body.appendChild(h); }
    return h;
  }
  /** toast(msg, kind, ms, action)
   *  action = {label, onClick} adds a button — this is how a destructive action
   *  offers Undo instead of asking "are you sure?" first. Reversible beats
   *  confirmable: a confirm dialog costs you a click every time, an undo costs
   *  a click only when you were wrong. */
  function toast(msg, kind, ms, action) {
    var t = el("div", { class: "toast " + (kind || ""), role: "status" },
                [el("span", { class: "grow", text: String(msg) }, [])]);
    var life = ms || (action ? 9000 : (kind === "bad" ? 5200 : 2600));
    var timer = null;

    function dismiss() {
      clearTimeout(timer);
      t.classList.add("out");
      setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, 200);
    }

    if (action && action.label) {
      t.appendChild(el("button", {
        class: "btn sm", onclick: function () {
          dismiss();
          try { action.onClick(); } catch (e) { toast.bad(e.message || String(e)); }
        }
      }, [action.label]));
    }

    toastHost().appendChild(t);
    timer = setTimeout(dismiss, life);
    t.dismiss = dismiss;
    return t;
  }
  toast.good = function (m, ms) { return toast(m, "good", ms); };
  toast.warn = function (m, ms) { return toast(m, "warn", ms); };
  toast.bad  = function (m, ms) { return toast(m, "bad", ms); };

  // ---------- modal ----------

  /** modal({title, body, footer, wide}) -> {close}. Esc and backdrop close it. */
  function modal(opts) {
    opts = opts || {};
    var box = el("div", { class: "modal" + (opts.wide ? " wide" : "") }, []);
    var back = el("div", { class: "modal-back" }, [box]);

    var head = el("header", {}, [el("h2", { text: opts.title || "" })]);
    var xbtn = el("button", { class: "btn icon ghost", title: "Close (Esc)" }, ["×"]);
    head.appendChild(xbtn);
    box.appendChild(head);

    var content = el("div", { class: "content" }, []);
    if (typeof opts.body === "string") content.innerHTML = opts.body;
    else if (opts.body) content.appendChild(opts.body);
    box.appendChild(content);

    if (opts.footer) {
      var f = el("footer", {}, []);
      (Array.isArray(opts.footer) ? opts.footer : [opts.footer])
        .forEach(function (n) { f.appendChild(n); });
      box.appendChild(f);
    }

    function close() {
      if (!back.parentNode) return;
      document.body.removeChild(back);
      document.removeEventListener("keydown", onKey, true);
      if (opts.onClose) opts.onClose();
    }
    function onKey(e) { if (e.key === "Escape") { e.stopPropagation(); close(); } }

    xbtn.addEventListener("click", close);
    back.addEventListener("mousedown", function (e) { if (e.target === back) close(); });
    document.addEventListener("keydown", onKey, true);
    document.body.appendChild(back);

    var first = box.querySelector("input,textarea,select,button.primary");
    if (first) setTimeout(function () { first.focus(); }, 30);

    return { close: close, el: box, content: content };
  }

  function confirmDanger(question, detail, onYes) {
    var yes = el("button", { class: "btn danger" }, ["Yes, do it"]);
    var no  = el("button", { class: "btn" }, ["Cancel"]);
    var m = modal({
      title: question,
      body: el("p", { class: "muted", text: detail || "" }),
      footer: [no, yes]
    });
    no.addEventListener("click", m.close);
    yes.addEventListener("click", function () { m.close(); onYes(); });
    return m;
  }

  function prompt(opts, onOk) {
    var input = el("input", { type: "text", value: opts.value || "",
                              placeholder: opts.placeholder || "" });
    var ok = el("button", { class: "btn primary" }, [opts.okText || "OK"]);
    var cancel = el("button", { class: "btn" }, ["Cancel"]);
    var wrap = el("div", { class: "field" }, [
      opts.label ? el("label", { text: opts.label }) : null, input,
      opts.hint ? el("div", { class: "hint", text: opts.hint }) : null
    ]);
    var m = modal({ title: opts.title || "", body: wrap, footer: [cancel, ok] });
    function submit() {
      var v = input.value.trim();
      if (!v) { input.focus(); return; }
      m.close(); onOk(v);
    }
    ok.addEventListener("click", submit);
    cancel.addEventListener("click", m.close);
    input.addEventListener("keydown", function (e) { if (e.key === "Enter") submit(); });
    return m;
  }

  // ---------- api ----------

  /** Where this app is mounted, or "" when it is serving itself.
   *
   *  Every app is written as though it owns the root, because on the desktop it
   *  does -- one app, one port. The phone shell serves all of them from a single
   *  origin under /a/<app>/, and sets window.__BASE__ before this file loads.
   *  Prefixing here means no app had to learn it might not be at the root.
   *
   *  Only absolute paths get the prefix. A relative URL already resolves against
   *  the document, so app.js and app.css need no help. */
  var BASE = String(window.__BASE__ || "").replace(/\/$/, "");
  function url(path) {
    return (BASE && String(path).charAt(0) === "/") ? BASE + path : path;
  }

  /** api('GET', '/api/x') -> Promise. Rejects with a readable Error on failure. */
  /** api(method, path, body, opts)
   *  opts.keepalive keeps the request alive past page teardown -- required for
   *  anything sent from beforeunload, which otherwise cancels with the page and
   *  loses the last save. */
  function api(method, path, body, extra) {
    var opts = { method: method, headers: {} };
    if (extra && extra.keepalive) opts.keepalive = true;
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    return fetch(url(path), opts).then(function (r) {
      var ct = r.headers.get("content-type") || "";
      if (ct.indexOf("application/json") === -1) {
        return r.text().then(function (t) {
          throw new Error(r.ok ? "Server sent something unexpected" :
                                 (r.status + ": " + t.slice(0, 160)));
        });
      }
      return r.json().then(function (data) {
        if (!r.ok) {
          // Carry the status on the Error. Callers need to tell a 409 conflict
          // from a 404 without regex-matching the message text.
          var err = new Error(data.error || ("Request failed (" + r.status + ")"));
          err.status = r.status;
          err.detail = data.detail;
          throw err;
        }
        return data;
      });
    });
  }
  api.get   = function (p, o)    { return api("GET", p, undefined, o); };
  api.post  = function (p, b, o) { return api("POST", p, b, o); };
  api.put   = function (p, b, o) { return api("PUT", p, b, o); };
  api.patch = function (p, b, o) { return api("PATCH", p, b, o); };
  api.del   = function (p, o)    { return api("DELETE", p, undefined, o); };

  /** Wrap a promise so failures always surface as a toast rather than silence. */
  function guard(p, what) {
    return p.catch(function (err) {
      toast.bad((what ? what + ": " : "") + (err && err.message ? err.message : err));
      throw err;
    });
  }

  // ---------- autosave ----------

  /** Debounced save with a visible status pill. No save button, ever.
   *  var save = UI.autosave(function(){ return API.put(...); }, statusEl);
   *  save();                       // schedules
   *  save.now();                   // flushes immediately (call before unload)
   */
  function autosave(doSave, statusEl, delay) {
    var timer = null, pending = false, inflight = false, inflightPromise = null;

    function paint(state, text) {
      if (!statusEl) return;
      statusEl.className = "saving" + (state === "busy" ? " busy" : "");
      clear(statusEl);
      statusEl.appendChild(el("span", { class: "dot" }, []));
      statusEl.appendChild(document.createTextNode(text));
    }

    function runSave() {
      inflight = true;
      pending = false;
      paint("busy", "Saving…");
      inflightPromise = Promise.resolve()
        .then(doSave)
        .then(function () { paint("ok", "Saved"); })
        .catch(function (e) {
          paint("busy", "Not saved");
          toast.bad("Could not save: " + (e.message || e));
        })
        .then(function () {
          inflight = false;
          inflightPromise = null;
          // Anything typed during the write gets its own save, and the promise
          // we already handed out only settles once that one is done too.
          if (pending) return runSave();
        });
      return inflightPromise;
    }

    function flush() {
      if (inflight) {
        // A save is already going. Mark the newer edits dirty and hand back the
        // promise that covers BOTH writes. Returning a resolved promise here
        // was a data-loss bug: beforeunload would let the page die believing
        // the work was safe when the second write had not started.
        pending = true;
        return inflightPromise || Promise.resolve();
      }
      return runSave();
    }

    function schedule() {
      paint("busy", "Saving…");
      clearTimeout(timer);
      timer = setTimeout(flush, delay || 550);
    }
    schedule.now = function () { clearTimeout(timer); return flush(); };
    schedule.idle = function () { paint("ok", "Saved"); };
    schedule.isPending = function () { return inflight || pending || timer !== null; };

    /* Flush when the page goes away, and do it here so no app has to remember.
       Every app already registered `beforeunload` by hand, and on a phone
       **that event does not fire.** Android kills a backgrounded WebView
       process without running a line of JavaScript in it, so the half-second
       of typing still sitting in `timer` dies with it -- silently, and most
       likely at the exact moment it matters, because the commonest reason this
       app goes to the background is that a call started.

       `visibilitychange` to hidden is the last moment mobile guarantees, and
       unlike `beforeunload` the page is still alive when it arrives, so an
       ordinary fetch completes normally. `pagehide` covers the desktop close
       and iOS's back-forward cache, which `visibilitychange` alone misses.

       Only when something is actually pending: an app can hold several of
       these, and flushing them all on every switch to the dialer would be a
       burst of writes that change nothing. */
    function flushIfDirty() {
      if (schedule.isPending()) { clearTimeout(timer); flush(); }
    }
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "hidden") flushIfDirty();
    });
    window.addEventListener("pagehide", flushIfDirty);

    return schedule;
  }

  /** Collapse a burst of calls into one. Returns the wrapped fn, with .cancel().
   *  Use for search-as-you-type, live recompute, list redraws. */
  function debounce(fn, ms) {
    var timer = null;
    function wrapped() {
      var args = arguments, self = this;
      clearTimeout(timer);
      timer = setTimeout(function () { fn.apply(self, args); }, ms || 200);
    }
    wrapped.cancel = function () { clearTimeout(timer); };
    wrapped.now = function () {
      clearTimeout(timer);
      return fn.apply(this, arguments);
    };
    return wrapped;
  }

  // ---------- keyboard ----------

  var _keys = {};
  /** hotkey('ctrl+k', fn). Ignored while typing unless force is true. */
  function hotkey(combo, fn, force) {
    _keys[combo.toLowerCase()] = { fn: fn, force: !!force };
  }
  document.addEventListener("keydown", function (e) {
    var parts = [];
    if (e.ctrlKey || e.metaKey) parts.push("ctrl");
    if (e.shiftKey) parts.push("shift");
    if (e.altKey) parts.push("alt");
    var k = (e.key || "").toLowerCase();
    if (["control", "shift", "alt", "meta"].indexOf(k) !== -1) return;
    parts.push(k);
    var hit = _keys[parts.join("+")];
    if (!hit) return;
    if (hit.force) { e.preventDefault(); hit.fn(e); return; }

    // A modal owns the keyboard while it is open. Otherwise a shortcut fires
    // "behind" the dialog and does something the user cannot see.
    if (document.querySelector(".modal-back")) return;

    var t = e.target;
    var typing = t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" ||
                       t.isContentEditable || t.tagName === "SELECT");
    if (typing) return;

    // Enter and Space activate a focused button or link by themselves. Firing a
    // global shortcut too means one press does the thing twice -- e.g. clicking
    // "add step" then pressing Enter added two steps.
    var activating = (k === "enter" || k === " " || k === "spacebar");
    var isControl = t && (t.tagName === "BUTTON" || t.tagName === "A" ||
                          t.getAttribute && t.getAttribute("role") === "button");
    if (activating && isControl && !e.ctrlKey && !e.metaKey && !e.altKey) return;

    e.preventDefault();
    hit.fn(e);
  });
  hotkey("ctrl+j", toggleTheme, true);

  // ---------- formatting ----------

  function fmtInt(n) {
    n = Number(n) || 0;
    return n.toLocaleString("en-US", { maximumFractionDigits: 0 });
  }
  function fmtMoney(n) {
    n = Number(n) || 0;
    return "$" + Math.round(n).toLocaleString("en-US");
  }
  /** Hours as the way a person says them: 0.5h -> "30m", 9.5 -> "9h 30m", 40 -> "1w" */
  function fmtHours(h) {
    h = Number(h) || 0;
    if (h === 0) return "0";
    if (h < 1) return Math.round(h * 60) + "m";
    if (h < 8) { var m = Math.round((h % 1) * 60);
                 return Math.floor(h) + "h" + (m ? " " + m + "m" : ""); }
    var d = h / 8;
    if (d < 5) return (Math.round(d * 10) / 10) + "d";
    return (Math.round((d / 5) * 10) / 10) + "w";
  }
  function fmtAgo(ms) {
    var s = Math.max(0, (Date.now() - Number(ms)) / 1000);
    if (s < 45) return "just now";
    if (s < 3600) return Math.round(s / 60) + "m ago";
    if (s < 86400) return Math.round(s / 3600) + "h ago";
    if (s < 86400 * 7) return Math.round(s / 86400) + "d ago";
    return new Date(Number(ms)).toLocaleDateString();
  }
  function pluralize(n, one, many) {
    return fmtInt(n) + " " + (Number(n) === 1 ? one : (many || one + "s"));
  }

  // ---------- phone numbers ----------

  /* The call list holds a number for ~250 companies, and these apps run on the
     device the call is made from. A number rendered as text means reading it
     off the screen and typing it into the dialer on the same phone, which is
     the one job the list exists to make easier.

     Nothing in Kotlin had to change for this. MainActivity already hands any
     URL that is not our own 127.0.0.1 origin to the system, and ACTION_VIEW on
     a `tel:` URI is the dialer. `tel:` is also not http(s), so smoke_test's
     OFFLINE check passes it untouched -- that check bans the CDN, not the
     protocol.

     On the laptop a tel: link is a dead end or a Phone Link prompt. Accepted
     deliberately rather than branching: one code path means the page the phone
     renders is the page the laptop renders, which is the whole premise of the
     Android build, and it keeps RENDER and MOUNTED looking at the same DOM. */

  /** A dialable `tel:` URI, or "" when there is nothing here to dial. */
  function telHref(raw) {
    var s = String(raw || "");

    // Pull an extension off the end first. "(229) 555-0100 x142" should dial
    // the main number, wait, then send 142 -- a comma is one pause in the tel:
    // scheme and every dialer honours it.
    var ext = "";
    var m = s.match(/(?:^|[\s,;(])(?:x|ext\.?|extension)\s*[:.]?\s*(\d{1,6})\s*\)?\s*$/i);
    if (m) { ext = m[1]; s = s.slice(0, m.index); }

    var international = s.replace(/[^\d+]/g, "").charAt(0) === "+";
    var digits = s.replace(/\D/g, "");

    // Seven digits is the shortest thing anyone can actually ring. Below that
    // it is a note someone typed in the wrong box, and a link to it would be a
    // tap that fails rather than a number that is missing.
    if (digits.length < 7) return "";

    var num;
    if (international) num = "+" + digits;
    else if (digits.length === 10) num = "+1" + digits;
    else if (digits.length === 11 && digits.charAt(0) === "1") num = "+" + digits;
    else num = digits;
    return "tel:" + num + (ext ? "," + ext : "");
  }

  /** The number as something you can tap -- or a plain span when you can't.
   *  Display text is always what was typed; only the href is normalised. */
  function telLink(raw, attrs) {
    var a = {};
    for (var k in (attrs || {})) {
      if (Object.prototype.hasOwnProperty.call(attrs, k)) a[k] = attrs[k];
    }
    a.text = String(raw || "");
    var href = telHref(raw);
    if (!href) return el("span", a, []);
    a.href = href;
    a.class = ((a.class ? a.class + " " : "") + "tel").trim();
    return el("a", a, []);
  }

  // ---------- download ----------

  function download(filename, text, mime) {
    var blob = new Blob([text], { type: mime || "text/plain;charset=utf-8" });
    var url = URL.createObjectURL(blob);
    var a = el("a", { href: url, download: filename }, []);
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(url); }, 1500);
  }

  function copy(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text)
        .then(function () { toast.good("Copied"); })
        .catch(function () { toast.warn("Could not copy"); });
    }
    var ta = el("textarea", { style: { position: "fixed", opacity: "0" } }, [text]);
    document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); toast.good("Copied"); }
    catch (e) { toast.warn("Could not copy"); }
    document.body.removeChild(ta);
    return Promise.resolve();
  }

  // ---------- boot signalling (used by the headless smoke test) ----------

  var errors = [];
  var didBoot = false;

  /** Tell the server how the page is doing. smoke_test.py polls /api/_boot for
   *  this, which is how "the GUI actually rendered" gets proved automatically. */
  var isSmokeRun = /[?&]smoke=1\b/.test(location.search);

  function beacon() {
    try {
      fetch(url("/api/_boot"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ok: didBoot && errors.length === 0,
          errors: errors.slice(0, 10),
          url: location.pathname
        })
      }).catch(function () {});
    } catch (e) { /* never let reporting break the app */ }

    // Under the smoke test the page shuts its own window. Managing browser
    // processes from the outside is unreliable when Edge is already running --
    // it hands the URL to the existing instance and the process we spawned
    // exits immediately. Self-closing sidesteps that entirely.
    if (isSmokeRun) setTimeout(function () { try { window.close(); } catch (e) {} }, 400);
  }

  function noteError(msg) {
    errors.push(String(msg).slice(0, 300));
    document.documentElement.setAttribute("data-js-error", errors.join(" | ").slice(0, 400));
    beacon();
  }

  window.addEventListener("error", function (e) { noteError(e.message); });
  window.addEventListener("unhandledrejection", function (e) {
    var r = e.reason;
    noteError(r && r.message ? r.message : r);
  });

  /** Call at the very end of your app's boot, once the UI is on screen.
   *  Sets data-boot="ok" and reports success to the server. */
  function booted() {
    didBoot = true;
    document.documentElement.setAttribute("data-boot", "ok");
    beacon();
  }

  // Safety net: if boot never completes within 8s, say so rather than hanging.
  setTimeout(function () {
    if (!didBoot) {
      noteError("boot never completed — UI.booted() was not reached within 8s");
    }
  }, 8000);

  window.UI = {
    el: el, $: $, $$: $$, clear: clear, esc: esc,
    theme: theme, toggleTheme: toggleTheme, themeButton: themeButton,
    toast: toast, modal: modal, confirmDanger: confirmDanger, prompt: prompt,
    api: api, url: url, base: function () { return BASE; },
    guard: guard, autosave: autosave, hotkey: hotkey, debounce: debounce,
    fmtInt: fmtInt, fmtMoney: fmtMoney, fmtHours: fmtHours, fmtAgo: fmtAgo,
    pluralize: pluralize, telHref: telHref, telLink: telLink,
    download: download, copy: copy,
    booted: booted, errors: errors
  };
})();
