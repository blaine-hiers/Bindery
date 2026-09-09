/* mobile.js — the small amount of behaviour mobile.css cannot express.
   Injected by the phone shell only. Nothing on the desktop loads it.

   Three jobs, and it refuses to do a fourth:
     1. Turn a fixed sidebar into a drawer, and give it a button.
     2. Wrap wide tables so they scroll sideways instead of the page doing it.
     3. Put a way back to the app list in the top bar.

   The apps build their DOM in JavaScript after this file runs, so everything
   here is either delegated or re-applied by an observer. Nothing assumes the
   page is finished. */

(function () {
  "use strict";

  var PHONE = 820;
  function isPhone() { return window.innerWidth <= PHONE; }

  function el(tag, attrs, kids) {
    var n = document.createElement(tag);
    for (var k in attrs || {}) {
      if (k === "class") n.className = attrs[k];
      else if (k === "text") n.textContent = attrs[k];
      else n.setAttribute(k, attrs[k]);
    }
    (kids || []).forEach(function (c) { n.appendChild(c); });
    return n;
  }

  // ---------- drawer ----------

  var backdrop = null;

  function sidebar() { return document.querySelector(".body > .sidebar"); }

  function closeDrawer() {
    var s = sidebar();
    if (s) s.classList.remove("drawer-open");
    if (backdrop && backdrop.parentNode) backdrop.parentNode.removeChild(backdrop);
    backdrop = null;
  }

  function openDrawer() {
    var s = sidebar();
    if (!s) return;
    s.classList.add("drawer-open");
    backdrop = el("button", { "class": "drawer-back", "aria-label": "Close the list" });
    backdrop.addEventListener("click", closeDrawer);
    document.body.appendChild(backdrop);
  }

  function toggleDrawer() {
    var s = sidebar();
    if (!s) return;
    if (s.classList.contains("drawer-open")) closeDrawer(); else openDrawer();
  }

  /* Tapping something in the drawer means "show me this" — the content is
     behind the drawer, so leaving it open hides the thing just chosen. */
  document.addEventListener("click", function (e) {
    var s = sidebar();
    if (!s || !s.classList.contains("drawer-open")) return;
    if (!s.contains(e.target)) return;
    if (e.target.closest(".list-item, .btn:not(.drawer-btn), a")) {
      setTimeout(closeDrawer, 90);
    }
  });

  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") closeDrawer();
  });

  // Rotating to a wide layout must not leave a translated drawer off-screen.
  window.addEventListener("resize", function () {
    if (!isPhone()) closeDrawer();
    syncChrome();
  });

  // ---------- top bar chrome ----------

  function syncChrome() {
    var bar = document.querySelector(".topbar");
    if (!bar) return;

    // Back to the app list. Present at every width — a tablet needs it too.
    if (!bar.querySelector(".shell-home")) {
      var home = el("a", {
        "class": "btn icon ghost shell-home",
        href: (window.__SHELL_HOME__ || "/"),
        title: "All apps",
        "aria-label": "All apps"
      });
      home.innerHTML =
        '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" ' +
        'stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
        'stroke-linejoin="round"><path d="M15 18l-6-6 6-6"/></svg>';
      bar.insertBefore(home, bar.firstChild);
    }

    // The drawer button only exists when there is a drawer to open.
    var btn = bar.querySelector(".drawer-btn");
    var needed = isPhone() && !!sidebar();
    if (needed && !btn) {
      btn = el("button", {
        "class": "btn icon ghost drawer-btn",
        title: "Show the list",
        "aria-label": "Show the list"
      });
      btn.innerHTML =
        '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" ' +
        'stroke="currentColor" stroke-width="2" stroke-linecap="round"><path ' +
        'd="M3 6h18M3 12h18M3 18h18"/></svg>';
      btn.addEventListener("click", toggleDrawer);
      var after = bar.querySelector(".shell-home");
      bar.insertBefore(btn, after ? after.nextSibling : bar.firstChild);
    } else if (!needed && btn) {
      btn.parentNode.removeChild(btn);
    }
  }

  // ---------- wide tables ----------

  function wrapTables() {
    var tables = document.querySelectorAll("table.grid");
    for (var i = 0; i < tables.length; i++) {
      var t = tables[i];
      if (t.parentNode && t.parentNode.classList.contains("tscroll")) continue;
      var box = el("div", { "class": "tscroll" });
      t.parentNode.insertBefore(box, t);
      box.appendChild(t);
    }
  }

  // ---------- keep up with a page that renders itself ----------

  var queued = false;
  function apply() {
    queued = false;
    syncChrome();
    wrapTables();
  }
  function schedule() {
    if (queued) return;
    queued = true;
    setTimeout(apply, 60);
  }

  function start() {
    apply();
    // The apps re-render whole panes on every keystroke in some screens, so this
    // watches for structure rather than trying to hook each app's render.
    try {
      new MutationObserver(schedule).observe(document.body,
        { childList: true, subtree: true });
    } catch (e) { /* an un-observed page is still a usable page */ }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
