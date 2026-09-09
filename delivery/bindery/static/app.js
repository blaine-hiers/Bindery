/* app.js — Knowledge Base Builder.
   Plain script. Everything shared lives on window.UI (see /_shared/ui.js). */

(function () {
  "use strict";

  var el = UI.el, $ = UI.$, api = UI.api, toast = UI.toast;

  var S = {
    kbs: [],
    kb: null,           // the active knowledge base
    tab: "search",
    query: "",
    results: null,
    report: null,
    doc: null,          // open document, if any
    files: null,
    poll: null,
    lastWords: []
  };

  var saveState = $("#saveState");
  var save = UI.autosave(function () {
    if (!S.kb) return;
    return api.put("/api/kbs/" + S.kb.id, {
      name: S.kb.name, notes: S.kb.notes, folder: S.kb.folder, checklist: S.kb.checklist
    });
  }, saveState);

  // ------------------------------------------------------------ small helpers

  function bytes(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return Math.round(n / 1024) + " KB";
    if (n < 1024 * 1024 * 1024) return (n / 1048576).toFixed(1) + " MB";
    return (n / 1073741824).toFixed(1) + " GB";
  }
  function whenSec(sec) {
    if (!sec) return "unknown";
    return new Date(Number(sec) * 1000).toLocaleDateString();
  }
  function rxEscape(s) { return String(s).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }

  /** Build DOM from snippet parts. Never innerHTML — client documents are not trusted. */
  function partsToNodes(parts) {
    return (parts || []).map(function (p) {
      return p.hit ? el("span", { class: "hitmark", text: p.t }) : document.createTextNode(p.t);
    });
  }

  /** Highlight query words inside a whole document, safely. */
  function highlightInto(node, text, words) {
    UI.clear(node);
    var bits = (words || []).filter(Boolean).map(function (w) {
      var e = rxEscape(w);
      return /[a-z0-9]$/i.test(w) ? e + "\\w*" : e;
    });
    if (!bits.length) { node.appendChild(document.createTextNode(text)); return; }
    var rx;
    try { rx = new RegExp("(?:" + bits.join("|") + ")", "gi"); }
    catch (e) { node.appendChild(document.createTextNode(text)); return; }
    var cursor = 0, m, guard = 0;
    while ((m = rx.exec(text)) !== null && guard++ < 20000) {
      if (m.index > cursor) node.appendChild(document.createTextNode(text.slice(cursor, m.index)));
      node.appendChild(el("span", { class: "hitmark", text: m[0] }));
      cursor = m.index + m[0].length;
      if (m[0].length === 0) rx.lastIndex++;
    }
    if (cursor < text.length) node.appendChild(document.createTextNode(text.slice(cursor)));
  }

  function empty(icon, title, msg, action) {
    return el("div", { class: "empty" }, [
      el("div", { class: "big", text: icon }),
      el("h2", { text: title }),
      el("p", { text: msg }),
      action || null
    ]);
  }

  // ------------------------------------------------------------ knowledge bases

  function loadKbs(selectId) {
    return UI.guard(api.get("/api/kbs"), "Could not load").then(function (d) {
      S.kbs = d.kbs || [];
      var want = selectId || (S.kb && S.kb.id);
      var found = null;
      S.kbs.forEach(function (k) { if (k.id === want) found = k; });
      S.kb = found || S.kbs[0] || null;
      renderSidebar();
      renderTopbar();
      renderSource();
      renderInspector();
      startPolling();
      return S.kb;
    });
  }

  function selectKb(id) {
    if (S.kb && S.kb.id === id) return;
    var found = null;
    S.kbs.forEach(function (k) { if (k.id === id) found = k; });
    if (!found) return;
    S.kb = found;
    S.results = null; S.report = null; S.doc = null; S.files = null;
    renderSidebar(); renderTopbar(); renderSource(); renderInspector();
    startPolling();
    render();
    if (S.tab === "search" && S.query) runSearch();
  }

  function renderTopbar() {
    $("#kbName").textContent = S.kb ? S.kb.name : "no knowledge base yet";
  }

  function renderSidebar() {
    var host = $("#kbList");
    UI.clear(host);
    if (!S.kbs.length) {
      host.appendChild(el("div", { class: "pad muted small",
        text: "No knowledge bases yet. Make one per client." }));
      return;
    }
    S.kbs.forEach(function (k) {
      var st = k.stats || {};
      var sub = st.total ? (UI.fmtInt(st.total) + " files · " + UI.fmtInt(st.ok || 0) + " readable"
                           + (k.partial ? " · part read only" : ""))
                         : "nothing read yet";
      host.appendChild(el("div", {
        class: "list-item kbrow" + (S.kb && S.kb.id === k.id ? " active" : ""),
        onclick: function () { selectKb(k.id); }
      }, [
        el("div", { class: "t" }, [
          el("strong", { text: k.name }),
          el("small", {}, [
            k.scanning ? el("span", { class: "spin", text: "reading… " }) : null,
            document.createTextNode(sub)
          ])
        ])
      ]));
    });
  }

  function renderSource() {
    var host = $("#sourcePane");
    UI.clear(host);
    if (!S.kb) return;
    var kb = S.kb;

    host.appendChild(el("h3", { text: "Folder being read" }));
    host.appendChild(el("div", {
      class: "path" + (kb.folder ? "" : " none"),
      text: kb.folder || "Nothing chosen yet"
    }));

    var pick = el("button", { class: "btn sm", onclick: openPicker }, ["Choose…"]);
    var scan = el("button", { class: "btn sm primary", onclick: startScan },
                  [kb.last_scan ? "Read again" : "Read it"]);
    host.appendChild(el("div", { class: "btns" }, [pick, scan]));

    var prog = el("div", { class: "prog", id: "progBox" }, []);
    host.appendChild(prog);
    paintProgress(S.progress);

    var more = el("div", { class: "btns", style: { marginTop: "8px" } }, [
      el("button", { class: "btn sm ghost", onclick: renameKb }, ["Rename"]),
      el("button", { class: "btn sm ghost", onclick: deleteKb }, ["Delete"])
    ]);
    host.appendChild(more);
  }

  function paintProgress(p) {
    var box = $("#progBox");
    if (!box) return;
    UI.clear(box);
    if (!p) return;
    if (p.error) {
      box.appendChild(el("div", { class: "badge bad", text: "Could not read it" }));
      box.appendChild(el("div", { class: "cur", text: p.error }));
      return;
    }
    if (p.running) {
      var pct = p.total ? Math.round(100 * p.done / p.total) : 0;
      box.appendChild(el("div", {}, [p.phase + " — " + UI.fmtInt(p.done) + " of " +
                                     UI.fmtInt(p.total)]));
      box.appendChild(el("div", { class: "meter" }, [el("span", { style: { width: pct + "%" } }, [])]));
      box.appendChild(el("span", { class: "cur", text: p.current || "" }));
      box.appendChild(el("button", {
        class: "btn sm block", style: { marginTop: "6px" },
        onclick: function () { UI.guard(api.del("/api/kbs/" + S.kb.id + "/scan"), "Stop"); }
      }, ["Stop"]));
      return;
    }
    var sum = p.summary;
    if (!sum) return;
    var c = sum.counts || {};
    box.appendChild(el("div", { class: "small muted" }, [
      (p.cancelled ? "Stopped early. " : "") +
      UI.fmtInt(sum.total || 0) + " files seen · " +
      UI.fmtInt(c.ok || 0) + " read · " +
      UI.fmtInt(c.unreadable || 0) + " unreadable · " +
      UI.fmtInt(c.unchanged || 0) + " unchanged"
    ]));
    if ((sum.problems || []).length) {
      box.appendChild(el("div", { class: "cur",
        text: sum.problems.length + " folders could not be opened" }));
    }
  }

  function renameKb() {
    if (!S.kb) return;
    UI.prompt({ title: "Rename", label: "Name", value: S.kb.name }, function (v) {
      S.kb.name = v; renderSidebar(); renderTopbar(); save();
    });
  }

  function deleteKb() {
    if (!S.kb) return;
    var kb = S.kb;
    UI.confirmDanger("Delete “" + kb.name + "”?",
      "This removes the index and the report. It does not touch a single file in " +
      (kb.folder || "the client's folder") + ".",
      function () {
        UI.guard(api.del("/api/kbs/" + kb.id), "Delete").then(function () {
          S.kb = null; S.results = null; S.report = null; S.doc = null; S.files = null;
          loadKbs().then(render);
          toast.good("Deleted");
        });
      });
  }

  function newKb() {
    UI.prompt({ title: "New knowledge base", label: "Client or job name",
                placeholder: "Northgate Mechanical Services", okText: "Create" }, function (v) {
      UI.guard(api.post("/api/kbs", { name: v }), "Create").then(function (d) {
        loadKbs(d.kb.id).then(function () { render(); openPicker(); });
      });
    });
  }

  // ------------------------------------------------------------ folder picker

  function openPicker() {
    if (!S.kb) return;
    var listBox = el("div", { class: "picker" }, []);
    var pathInput = el("input", { type: "text", value: S.kb.folder || "",
                                  placeholder: "C:\\Clients\\Northgate-Mechanical" });
    var useBtn = el("button", { class: "btn primary" }, ["Use this folder"]);
    var cancel = el("button", { class: "btn" }, ["Cancel"]);

    var body = el("div", {}, [
      el("div", { class: "field" }, [
        el("label", { text: "Folder" }), pathInput,
        el("div", { class: "hint", text: "Type or paste a path, or click through below. " +
                                         "Nothing in it is ever changed." })
      ]),
      listBox
    ]);
    var m = UI.modal({ title: "Which folder holds the documents?", body: body,
                       footer: [cancel, useBtn], wide: true });

    function go(path) {
      UI.clear(listBox);
      listBox.appendChild(el("div", { class: "pad muted small", text: "Loading…" }));
      api.get("/api/browse?path=" + encodeURIComponent(path || "")).then(function (d) {
        UI.clear(listBox);
        if (d.parent !== null && d.parent !== undefined) {
          listBox.appendChild(el("div", {
            class: "list-item", onclick: function () { go(d.parent); }
          }, [el("div", { class: "t" }, [el("strong", { text: "↑ up one level" }),
                                        el("small", { text: d.parent })])]));
        } else if (!d.roots) {
          listBox.appendChild(el("div", {
            class: "list-item", onclick: function () { go(""); }
          }, [el("div", { class: "t" }, [el("strong", { text: "↑ drives and home" })])]));
        }
        if (!d.folders.length) {
          listBox.appendChild(el("div", { class: "pad muted small",
            text: "No folders inside this one." }));
        }
        d.folders.forEach(function (f) {
          listBox.appendChild(el("div", {
            class: "list-item",
            onclick: function () { pathInput.value = f.path; go(f.path); }
          }, [el("div", { class: "t" }, [el("strong", { text: f.name }),
                                        el("small", { text: f.path })])]));
        });
      }).catch(function (e) {
        UI.clear(listBox);
        listBox.appendChild(el("div", { class: "pad small",
          text: e.message || "Could not list that folder" }));
      });
    }
    go(S.kb.folder || "");

    cancel.addEventListener("click", m.close);
    useBtn.addEventListener("click", function () {
      var v = pathInput.value.trim();
      if (!v) { pathInput.focus(); return; }
      S.kb.folder = v;
      m.close();
      renderSource();
      save.now().then(startScan);
    });
    pathInput.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); useBtn.click(); }
    });
  }

  // ------------------------------------------------------------ scanning

  function startScan() {
    if (!S.kb) return;
    if (!S.kb.folder) { openPicker(); return; }
    UI.guard(api.post("/api/kbs/" + S.kb.id + "/scan", { folder: S.kb.folder }), "Read folder")
      .then(function () {
        S.report = null;
        toast("Reading " + S.kb.folder);
        startPolling(true);
      });
  }

  function startPolling(force) {
    if (S.poll) { clearInterval(S.poll); S.poll = null; }
    if (!S.kb) return;
    var tick = function () {
      if (!S.kb) return;
      var id = S.kb.id;
      api.get("/api/kbs/" + id + "/scan").then(function (d) {
        if (!S.kb || S.kb.id !== id) return;
        var p = d.progress;
        var was = S.progress;
        S.progress = p;
        paintProgress(p);
        var wasRunning = was && was.running;
        if (p.running) {
          if (!S.poll) S.poll = setInterval(tick, 600);
        } else {
          if (S.poll) { clearInterval(S.poll); S.poll = null; }
          if (wasRunning) {
            if (p.error) toast.bad(p.error);
            else if (p.cancelled) toast.warn("Stopped. What was read so far is kept.");
            else toast.good("Done reading " + S.kb.folder);
            S.report = null; S.files = null;
            loadKbs(id).then(function () { render(); if (S.query) runSearch(); });
          }
        }
      }).catch(function () { /* polling is best-effort; the GUI stays up */ });
    };
    tick();
    if (force) S.poll = setInterval(tick, 600);
  }

  // ------------------------------------------------------------ tabs

  function setTab(name) {
    S.tab = name;
    S.doc = null;
    UI.$$("#tabs .tab").forEach(function (b) {
      b.classList.toggle("active", b.dataset.tab === name);
    });
    $("#searchbar").classList.toggle("off", name !== "search");
    render();
  }

  function render() {
    var pane = $("#pane");
    UI.clear(pane);
    renderInspector();
    if (!S.kb) {
      pane.appendChild(empty("📁", "No knowledge base yet",
        "Make one per client. Point it at the folder where their documents live.",
        el("button", { class: "btn primary", onclick: newKb }, ["New knowledge base"])));
      return;
    }
    if (S.doc) { renderDoc(pane); return; }
    if (S.tab === "search") renderSearch(pane);
    else if (S.tab === "report") renderReport(pane);
    else if (S.tab === "files") renderFiles(pane);
    else if (S.tab === "checklist") renderChecklist(pane);
  }

  // ------------------------------------------------------------ search tab

  var searchTimer = null, searchSeq = 0;

  function runSearch() {
    if (!S.kb) return;
    var q = S.query;
    var seq = ++searchSeq;
    var id = S.kb.id;
    api.get("/api/kbs/" + id + "/search?q=" + encodeURIComponent(q) + "&limit=40")
      .then(function (d) {
        if (seq !== searchSeq || !S.kb || S.kb.id !== id) return;
        S.results = d;
        S.lastWords = (d.parsed && d.parsed.words) || [];
        if (S.tab === "search" && !S.doc) render();
        paintSearchMeta();
      })
      .catch(function (e) {
        if (seq !== searchSeq) return;
        S.results = { error: e.message, results: [], total: 0 };
        if (S.tab === "search" && !S.doc) render();
      });
  }

  function paintSearchMeta() {
    var box = $("#searchMeta");
    UI.clear(box);
    var r = S.results;
    if (!S.kb) return;
    var st = S.kb.stats || {};
    if (!S.query) {
      box.appendChild(el("span", { text: st.total
        ? UI.fmtInt(st.ok || 0) + " searchable files ready"
        : "Nothing read yet — choose a folder on the left" }));
      box.appendChild(el("span", { class: "muted",
        text: 'Try: "price list"   ·   -draft   ·   ext:pdf   ·   folder:contracts' }));
      return;
    }
    if (!r) { box.appendChild(el("span", { text: "Searching…" })); return; }
    box.appendChild(el("span", {}, [
      el("b", { text: UI.fmtInt(r.total || 0) + (r.at_least ? "+" : "") }),
      document.createTextNode(r.at_least
        ? " matches (only the best 1,200 were checked for the exact phrase)"
        : " matches")]));
    if (r.ms !== undefined) box.appendChild(el("span", { text: r.ms + " ms" }));
    var p = r.parsed || {};
    (p.phrases || []).forEach(function (x) {
      box.appendChild(el("span", { class: "badge accent", text: '"' + x + '"' }));
    });
    (p.ext || []).forEach(function (x) { box.appendChild(el("span", { class: "badge", text: x })); });
    (p.folder || []).forEach(function (x) {
      box.appendChild(el("span", { class: "badge", text: "in " + x }));
    });
    (p.exclude || []).forEach(function (x) {
      box.appendChild(el("span", { class: "badge warn", text: "not " + x }));
    });
  }

  function renderSearch(pane) {
    var st = S.kb.stats || {};
    if (!st.total) {
      pane.appendChild(empty("📂", "Nothing read yet",
        "Choose the folder that holds this client's documents on the left, then press " +
        "“Read it”. A few thousand files takes under a minute.",
        el("button", { class: "btn primary", onclick: openPicker }, ["Choose a folder"])));
      return;
    }
    if (!S.query) {
      pane.appendChild(renderAtAGlance());
      return;
    }
    var r = S.results;
    if (!r) { pane.appendChild(el("div", { class: "empty", text: "Searching…" })); return; }
    if (r.error) {
      pane.appendChild(empty("⚠", "That search did not work", r.error));
      return;
    }
    if (!r.results.length) {
      pane.appendChild(empty("🔍", "Nothing matched “" + S.query + "”",
        r.empty_reason ||
        "Try fewer words. Quotes force an exact phrase, a minus sign removes a word, " +
        "and ext:pdf narrows it to one file type."));
      return;
    }
    var host = el("div", { class: "hits" }, []);
    r.results.forEach(function (h) { host.appendChild(hitCard(h)); });
    pane.appendChild(host);
    if (r.total > r.results.length) {
      pane.appendChild(el("div", { class: "muted small center mt",
        text: "Showing the best " + r.results.length + " of " + UI.fmtInt(r.total) + "." }));
    }
  }

  function hitCard(h) {
    var snip = el("div", { class: "snip" }, partsToNodes(h.parts));
    return el("div", {
      class: "hit-card", onclick: function () { openDoc(h.doc_id); }
    }, [
      el("div", { class: "top" }, [
        el("span", { class: "nm", text: h.name }),
        el("span", { class: "fld", text: h.folder || "(top level)" }),
        el("span", { class: "sc", text: h.score ? h.score.toFixed(2) : "" })
      ]),
      h.snippet ? snip : el("div", { class: "snip muted", text: "(no text in this file)" }),
      el("div", { class: "facts" }, [
        el("span", { text: bytes(h.size) }),
        el("span", { text: "changed " + whenSec(h.mtime) }),
        el("span", { text: UI.fmtInt(h.nwords) + " words" }),
        h.status !== "ok" ? el("span", { class: "badge warn", text: h.status }) : null
      ])
    ]);
  }

  function renderAtAGlance() {
    var st = S.kb.stats || {};
    var card = el("div", { class: "card" }, [
      el("header", {}, [el("h2", { text: "What is in this folder" })]),
      el("div", { class: "stats" }, [
        stat("Files seen", UI.fmtInt(st.total || 0)),
        stat("Searchable", UI.fmtInt(st.ok || 0)),
        stat("Cannot be opened", UI.fmtInt(st.unreadable || 0)),
        stat("Not documents", UI.fmtInt(st.skipped || 0)),
        stat("Words indexed", UI.fmtInt(st.words || 0))
      ])
    ]);
    var tips = el("div", { class: "card" }, [
      el("header", {}, [el("h2", { text: "How to search it" })]),
      el("table", { class: "grid" }, [
        el("tbody", {}, [
          tipRow("warranty labor", "Both words, anywhere in the file. Short files that are really about it come first."),
          tipRow('"price list"', "The exact phrase, words next to each other."),
          tipRow("invoice -draft", "Anything about invoices, but not if the word draft is in it."),
          tipRow("ext:pdf safety", "Only PDFs."),
          tipRow("folder:contracts renewal", "Only inside a folder whose path mentions contracts.")
        ])
      ])
    ]);
    return el("div", {}, [card, tips]);
  }

  function tipRow(a, b) {
    return el("tr", {}, [el("td", {}, [el("code", { text: a })]), el("td", { text: b })]);
  }
  function stat(k, v, d) {
    return el("div", { class: "stat" }, [
      el("div", { class: "k", text: k }), el("div", { class: "v", text: v }),
      d ? el("div", { class: "d", text: d }) : null
    ]);
  }

  // ------------------------------------------------------------ document view

  function openDoc(docId) {
    if (!S.kb) return;
    UI.guard(api.get("/api/kbs/" + S.kb.id + "/docs/" + docId), "Open file")
      .then(function (d) { S.doc = d.doc; render(); $("#pane").scrollTop = 0; });
  }

  function renderDoc(pane) {
    var d = S.doc;
    var back = el("button", { class: "btn sm", onclick: function () { S.doc = null; render(); } },
                  ["← Back"]);
    var revealBtn = el("button", {
      class: "btn sm",
      onclick: function () {
        UI.guard(api.post("/api/kbs/" + S.kb.id + "/docs/" + d.doc_id + "/reveal", {}), "Show")
          .then(function () { toast.good("Opened the folder"); });
      }
    }, ["Show me the file"]);
    var copyBtn = el("button", {
      class: "btn sm ghost", onclick: function () { UI.copy(d.path); }
    }, ["Copy path"]);

    var body = el("div", { class: "docview" }, [
      el("div", { class: "head" }, [back, el("h1", { class: "truncate", text: d.name }),
                                    revealBtn, copyBtn]),
      el("div", { class: "meta", text: d.path }),
      el("div", { class: "flex mb" }, [
        el("span", { class: "badge" + (d.status === "ok" ? " good" : " bad"),
                     text: d.status === "ok" ? "readable" : d.status }),
        el("span", { class: "badge", text: d.ext || "no extension" }),
        el("span", { class: "badge", text: bytes(d.size) }),
        el("span", { class: "badge", text: "changed " + whenSec(d.mtime) }),
        el("span", { class: "badge", text: UI.fmtInt(d.nwords) + " words" }),
        d.author ? el("span", { class: "badge", text: "saved by " + d.author }) : null,
        !d.exists ? el("span", { class: "badge bad", text: "no longer on disk" }) : null
      ])
    ]);

    if (d.reason) {
      body.appendChild(el("div", { class: "headline bad" },
        ["Could not read this one: " + d.reason]));
    }
    if (d.text) {
      var box = el("pre", { class: "doctext" }, []);
      highlightInto(box, d.text, S.lastWords);
      body.appendChild(box);
      if (d.truncated) {
        body.appendChild(el("div", { class: "muted small mt",
          text: "Only the first 400,000 characters are shown." }));
      }
    } else if (!d.reason) {
      body.appendChild(el("div", { class: "empty", text: "There are no words in this file." }));
    }
    pane.appendChild(body);
  }

  // ------------------------------------------------------------ all files tab

  function renderFiles(pane) {
    if (!S.files) {
      pane.appendChild(el("div", { class: "empty", text: "Loading…" }));
      UI.guard(api.get("/api/kbs/" + S.kb.id + "/docs?limit=2000"), "Load files")
        .then(function (d) { S.files = d; if (S.tab === "files") render(); });
      return;
    }
    var d = S.files;
    if (!d.docs.length) {
      pane.appendChild(empty("📂", "Nothing read yet",
        "Choose a folder on the left and press “Read it”."));
      return;
    }
    var filterRow = el("div", { class: "flex mb" }, [
      el("span", { class: "muted small", text: UI.fmtInt(d.total) + " files" }),
      el("span", { class: "grow" }, []),
      chipBtn("Everything", ""), chipBtn("Readable", "ok"),
      chipBtn("Cannot be opened", "unreadable"), chipBtn("Not documents", "skipped")
    ]);
    pane.appendChild(filterRow);

    var rows = d.docs.filter(function (x) { return !S.fileFilter || x.status === S.fileFilter; });
    var table = el("table", { class: "grid" }, [
      el("thead", {}, [el("tr", {}, [
        el("th", { text: "Name" }), el("th", { text: "Folder" }),
        el("th", { text: "Type" }), el("th", { class: "num", text: "Size" }),
        el("th", { text: "Changed" }), el("th", { text: "State" })
      ])]),
      el("tbody", {}, rows.slice(0, 800).map(function (x) {
        return el("tr", { style: { cursor: "pointer" },
                          onclick: function () { openDoc(x.doc_id); } }, [
          el("td", { text: x.name }),
          el("td", { class: "muted small", text: x.folder || "(top level)" }),
          el("td", { class: "small", text: x.ext || "—" }),
          el("td", { class: "num", text: bytes(x.size) }),
          el("td", { class: "small", text: whenSec(x.mtime) }),
          el("td", {}, [x.status === "ok"
            ? el("span", { class: "badge good", text: "readable" })
            : el("span", { class: "badge " + (x.status === "unreadable" ? "bad" : ""),
                           title: x.reason || "", text: x.reason || x.status })])
        ]);
      }))
    ]);
    pane.appendChild(el("div", { class: "panel" }, [table]));
    if (rows.length > 800) {
      pane.appendChild(el("div", { class: "muted small center mt",
        text: "Showing the first 800 of " + UI.fmtInt(rows.length) + "." }));
    }
  }

  function chipBtn(label, value) {
    return el("button", {
      class: "btn sm" + ((S.fileFilter || "") === value ? " primary" : ""),
      onclick: function () { S.fileFilter = value; render(); }
    }, [label]);
  }

  // ------------------------------------------------------------ gap report tab

  function renderReport(pane) {
    if (!S.report) {
      pane.appendChild(el("div", { class: "empty", text: "Working out the report…" }));
      api.get("/api/kbs/" + S.kb.id + "/report").then(function (d) {
        S.report = d.report;
        if (S.tab === "report") render();
      }).catch(function (e) {
        if (S.tab !== "report") return;
        UI.clear(pane);
        pane.appendChild(empty("📊", "No report yet", e.message,
          el("button", { class: "btn primary", onclick: openPicker }, ["Choose a folder"])));
      });
      return;
    }
    var r = S.report, kb = S.kb;
    var root = el("div", { class: "report" }, []);

    root.appendChild(el("div", { class: "flex mb no-print" }, [
      el("span", { class: "grow" }, []),
      el("button", { class: "btn sm", onclick: downloadMarkdown }, ["Download as Markdown"]),
      el("button", { class: "btn sm", onclick: copyMarkdown }, ["Copy"]),
      el("button", { class: "btn sm primary", onclick: function () { window.print(); } },
        ["Print / save as PDF"])
    ]));

    root.appendChild(el("h1", { text: "What your documents look like — " + kb.name }));
    root.appendChild(el("div", { class: "lede" }, [
      "Folder read: ", el("code", { text: kb.folder || "—" }),
      ". Nothing was changed, moved or copied."
    ]));

    // --- headlines
    var sec = section("The short version",
      "The three or four things worth saying out loud.");
    (r.headlines || []).forEach(function (h) {
      sec.appendChild(el("div", { class: "headline " + h.kind, text: h.text }));
    });
    if (!(r.headlines || []).length) {
      sec.appendChild(el("div", { class: "headline info",
        text: "Nothing stood out. That is unusual and worth a second look." }));
    }
    root.appendChild(sec);

    if (r.partial) {
      root.appendChild(el("div", { class: "headline bad" }, [
        "This is a part read. The scan was stopped before it finished, so every number " +
        "below covers only the files that had been opened by then. Read the folder again " +
        "to the end before this goes to anybody."
      ]));
    }

    // --- score
    var s = r.score;
    var sc = section("Readiness score",
      "Every line is shown with the arithmetic. Add the points column and you get the score.");
    var pctOfMax = s.out_of ? (100 * s.total / s.out_of) : 0;
    var meterKind = pctOfMax >= 80 ? "good" : (pctOfMax >= 50 ? "warn" : "bad");
    sc.appendChild(el("div", { class: "card" }, [
      el("div", { class: "scorebox" }, [
        el("div", { class: "scorebig" }, [String(s.total),
          el("small", { text: " / " + s.out_of })]),
        el("div", { class: "grow" }, [
          el("div", { style: { fontWeight: "600", marginBottom: "6px" }, text: s.band }),
          el("div", { class: "meter " + meterKind },
            [el("span", { style: { width: Math.max(0, Math.min(100, pctOfMax)) + "%" } }, [])])
        ])
      ]),
      el("div", { class: "sep" }, []),
      el("table", { class: "grid" }, [
        el("thead", {}, [el("tr", {}, [
          el("th", { text: "What we measured" }), el("th", { class: "num", text: "Where you are" }),
          el("th", { class: "num", text: "Weight" }), el("th", { class: "num", text: "Points" })
        ])]),
        el("tbody", {}, s.components.map(function (c) {
          // A line we could not work out shows a dash, not a zero. A zero is a
          // finding; a dash is an honest "we could not tell".
          var measured = c.measured !== false;
          return el("tr", { class: measured ? "" : "unmeasured" }, [
            el("td", {}, [el("div", { text: c.label }),
                          el("div", { class: "muted small", text: c.how })]),
            el("td", { class: "num", text: measured ? c.raw_pct + "%" : "not measured" }),
            el("td", { class: "num", text: measured ? String(c.weight) : "—" }),
            el("td", { class: "num", text: measured ? String(c.points) : "—" })
          ]);
        }).concat([
          el("tr", {}, [el("td", {}, [el("strong", { text: "Total" })]), el("td", {}, []),
                        el("td", { class: "num" }, [el("strong", { text: String(s.out_of) })]),
                        el("td", { class: "num" }, [el("strong", { text: String(s.total) })])])
        ]))
      ]),
      el("div", { class: "muted small mt", text: s.note })
    ]));
    root.appendChild(sc);

    // --- what we looked at
    var c = r.counts;
    var look = section("What we looked at", "");
    look.appendChild(el("div", { class: "stats" }, [
      stat("Files seen", UI.fmtInt(c.total)),
      stat("Could read", UI.fmtInt(c.readable)),
      stat("Could not read", UI.fmtInt(c.unreadable)),
      stat("Not documents", UI.fmtInt(c.skipped)),
      stat("Words", UI.fmtInt(c.words)),
      stat("On disk", bytes(c.bytes))
    ]));
    root.appendChild(look);

    // --- unreadable
    var u = r.unreadable;
    var un = section("Files nobody can open or search",
      "In an older company this is usually the biggest single finding. It is knowledge the " +
      "business already paid for and cannot use.");
    if (!u.count) {
      un.appendChild(el("p", { class: "muted", text: "Everything opened. That is rare — good." }));
    } else {
      un.appendChild(el("div", { class: "headline bad",
        text: UI.fmtInt(u.count) + " files (" + u.share + "% of everything, " + bytes(u.bytes) +
              " on disk) could not be read." }));
      un.appendChild(el("div", { class: "panel" }, [
        el("table", { class: "grid" }, [
          el("thead", {}, [el("tr", {}, [el("th", { text: "Why" }),
                                         el("th", { class: "num", text: "How many" }),
                                         el("th", { text: "For example" })])]),
          el("tbody", {}, u.by_reason.map(function (row) {
            return el("tr", {}, [
              el("td", { text: row.reason }),
              el("td", { class: "num", text: UI.fmtInt(row.count) }),
              el("td", { class: "small muted" },
                 row.examples.slice(0, 3).map(function (e) {
                   return el("div", { style: { cursor: "pointer" }, text: e.rel,
                     onclick: function () { openDoc(e.doc_id); } });
                 }))
            ]);
          }))
        ])
      ]));
    }
    if (r.skipped.count) {
      un.appendChild(el("div", { class: "muted small mt" }, [
        UI.fmtInt(r.skipped.count) + " more files were not documents at all (" +
        r.skipped.by_ext.slice(0, 6).map(function (x) { return x.ext + " ×" + x.count; }).join(", ") +
        "). They are counted, not read, and they do not count against the score."
      ]));
    }
    root.appendChild(un);

    // --- duplicates
    var d = r.duplicates;
    var dup = section("Copies that disagree",
      "The question is never how many copies there are. It is which one is right.");
    if (!d.exact_groups.length && !d.near_groups.length) {
      dup.appendChild(el("p", { class: "muted", text: "No duplicate documents found." }));
    } else {
      dup.appendChild(el("div", { class: "headline warn",
        text: UI.fmtInt(d.files_involved) + " files (" + d.share + "% of what we could read) are " +
              "copies or near-copies of each other — " + UI.fmtInt(d.wasted_copies) +
              " extra copies across " + (d.exact_group_count + d.near_group_count) + " sets." }));
      d.exact_groups.slice(0, 8).forEach(function (g) { dup.appendChild(dupGroup(g)); });
      d.near_groups.slice(0, 8).forEach(function (g) { dup.appendChild(dupGroup(g)); });
    }
    root.appendChild(dup);

    // --- stale
    var st = r.stale;
    var stale = section("Documents nobody has touched",
      "A procedure nobody has edited in five years is either perfect or ignored. It is almost " +
      "never perfect.");
    stale.appendChild(el("div", { class: "stats mb" }, [
      stat("Under a year", UI.fmtInt(st.buckets.under1)),
      stat("1 to 3 years", UI.fmtInt(st.buckets["1to3"])),
      stat("3 to 5 years", UI.fmtInt(st.buckets["3to5"])),
      stat("Over 5 years", UI.fmtInt(st.buckets.over5))
    ]));
    if (st.oldest.length) {
      stale.appendChild(el("div", { class: "panel" }, [
        el("table", { class: "grid" }, [
          el("thead", {}, [el("tr", {}, [el("th", { text: "Oldest first" }),
                                         el("th", { class: "num", text: "Years" })])]),
          el("tbody", {}, st.oldest.map(function (f) {
            return el("tr", { style: { cursor: "pointer" },
                              onclick: function () { openDoc(f.doc_id); } }, [
              el("td", { class: "small", text: f.rel }),
              el("td", { class: "num", text: String(f.years) })
            ]);
          }))
        ])
      ]));
    }
    root.appendChild(stale);

    // --- orphans
    var o = r.orphans;
    var orp = section("Files nothing else points at",
      "This is a name match. A file everyone calls “the pricing sheet” but which is saved as " +
      "PL-2024-rev3.xlsx shows up here even though people use it daily — read the list before " +
      "you read anything into it.");
    orp.appendChild(el("div", { class: "headline info",
      text: UI.fmtInt(o.unreferenced_count) + " of the " + UI.fmtInt(o.judged_count) +
            " files we could check (" + o.unreferenced_share +
            "%) are never named in any other document." }));
    if (o.unreferenced.length) {
      orp.appendChild(el("ul", { class: "pathlist" }, o.unreferenced.slice(0, 20).map(function (f) {
        return el("li", { text: f.rel, onclick: function () { openDoc(f.doc_id); } });
      })));
    }
    if (o.unjudged_count) {
      orp.appendChild(el("p", { class: "muted small mt",
        text: "A further " + UI.fmtInt(o.unjudged_count) + " files have nothing distinctive in " +
              "the name to search for (things like 2024.pdf), so we could not say either way. " +
              "They are not in the count above." }));
    }
    if (o.thin_count) {
      orp.appendChild(el("p", { class: "muted small mt",
        text: "A further " + UI.fmtInt(o.thin_count) + " files opened but had almost nothing in " +
              "them (under 25 words)." }));
    }
    root.appendChild(orp);

    // --- coverage
    var cov = r.coverage;
    var cvg = section("What is missing",
      "Checked against what a business this size normally has written down.");
    if (cov.measured === false) {
      // Nothing opened, so nothing is "missing" — it could be inside one of the
      // files nobody can read. Saying otherwise contradicts the score.
      cvg.appendChild(el("div", { class: "headline info",
        text: "Nothing here opened, so we could not look. Anything on the checklist could " +
              "still be inside one of the files nobody can read — that is the finding above, " +
              "not this one." }));
      cov = { measured: false, missing: [], found: [], mentioned_only: [] };
    } else {
      cvg.appendChild(el("div", { class: "headline " + (cov.missing.length ? "bad" : "info"),
        text: cov.found_count + " of " + cov.checked + " found — " + cov.named_count +
              " as a document with the words in its name, " + cov.mention_count +
              " only as a mention inside some other file. In the score a mention counts " +
              "half, because a memo that says “warranty” is not warranty terms." }));
    }
    if ((cov.mentioned_only || []).length) {
      cvg.appendChild(el("p", { class: "muted small",
        text: "Worth checking by hand, because a mention is not the same as having the " +
              "document: " + cov.mentioned_only.map(function (m) { return m.charAt(0).toLowerCase() + m.slice(1); })
                .join(", ") + "." }));
    }
    if (cov.missing.length) {
      var box = el("div", { class: "card" }, [
        el("header", {}, [el("h2", { text: "Not found anywhere" })])
      ]);
      cov.missing.forEach(function (m) {
        box.appendChild(el("div", { class: "missing-item" }, [
          el("div", { class: "lbl", text: m.label }),
          el("div", { class: "why", text: m.why })
        ]));
      });
      cvg.appendChild(box);
    }
    if (cov.found.length) {
      var fbox = el("div", { class: "card" }, [
        el("header", {}, [el("h2", { text: "Found" })])
      ]);
      cov.found.forEach(function (f) {
        var hit = f.hits[0];
        fbox.appendChild(el("div", { class: "missing-item" }, [
          el("div", { class: "lbl" }, [
            document.createTextNode(f.label),
            f.named ? null : el("span", { class: "badge warn",
                                          style: { marginLeft: "8px" },
                                          text: "only a mention" })
          ]),
          el("div", { class: "why" }, [
            "matched “" + hit.matched + "” " +
            (hit.where === "name" ? "in the file's name — " : "inside the text of — "),
            el("span", { style: { cursor: "pointer", textDecoration: "underline" },
                         text: hit.rel,
                         onclick: (function (matched) {
                           return function () { S.query = '"' + matched + '"';
                                                $("#q").value = S.query; setTab("search");
                                                runSearch(); };
                         })(hit.matched) })
          ])
        ]));
      });
      cvg.appendChild(fbox);
    }
    root.appendChild(cvg);

    // --- concentration
    var k = r.concentration;
    var con = section("How concentrated it all is",
      "Concentration is a people risk, not a filing risk. If one folder, one format or one " +
      "person's name is on most of it, that is where the business breaks when they leave.");
    con.appendChild(barBlock("By folder", k.folders));
    con.appendChild(barBlock("By file type", k.types));
    if (k.authors.length) con.appendChild(barBlock("By who last saved it", k.authors));
    else con.appendChild(el("p", { class: "muted small",
      text: "No file recorded who last saved it, so we cannot say how concentrated it is by person." }));
    root.appendChild(con);

    pane.appendChild(root);
  }

  function section(title, sub) {
    var s = el("section", {}, [el("h2", { text: title })]);
    if (sub) s.appendChild(el("div", { class: "sub", text: sub }));
    return s;
  }

  function dupGroup(g) {
    var label = g.kind === "exact" ? "identical"
                                   : Math.round(g.similarity * 100) + "% the same";
    return el("div", { class: "dupgroup" }, [
      el("div", { class: "hdr" }, [
        el("span", { class: "badge " + (g.kind === "exact" ? "bad" : "warn"),
                     text: g.files.length + " copies, " + label }),
        el("span", { class: "muted small",
                     text: "spread over " + g.spread_years + " years · " +
                           UI.fmtInt(g.words) + " words each" })
      ]),
      el("ul", {}, g.files.map(function (f) {
        return el("li", { text: f.rel + "   (" + whenSec(f.mtime) + ")",
                          onclick: function () { openDoc(f.doc_id); } });
      }))
    ]);
  }

  function barBlock(title, rows) {
    var box = el("div", { class: "card tight" }, [el("h3", { text: title })]);
    rows.forEach(function (r) {
      box.appendChild(el("div", { class: "bar-row" }, [
        el("span", { class: "lbl", text: r.key }),
        el("div", { class: "meter" }, [el("span", { style: { width: r.share + "%" } }, [])]),
        el("span", { class: "num", text: r.share + "% · " + r.count })
      ]));
    });
    return box;
  }

  function withMarkdown(then) {
    return UI.guard(api.get("/api/kbs/" + S.kb.id + "/report-markdown"), "Export").then(then);
  }
  function downloadMarkdown() {
    withMarkdown(function (d) {
      UI.download(d.filename, d.markdown, "text/markdown;charset=utf-8");
      toast.good("Saved to your downloads");
    });
  }
  function copyMarkdown() { withMarkdown(function (d) { UI.copy(d.markdown); }); }

  // ------------------------------------------------------------ checklist tab

  function renderChecklist(pane) {
    var list = S.kb.checklist || [];
    pane.appendChild(el("div", { class: "card" }, [
      el("header", {}, [el("h2", { text: "What a business this size should have written down" })]),
      el("p", { class: "muted small",
        text: "The gap report checks for each of these. Turn off what does not apply to this " +
              "client and add anything their industry needs. Words are matched against file " +
              "names and file contents; separate them with commas." })
    ]));

    var host = el("div", {}, []);
    list.forEach(function (item, i) { host.appendChild(checklistRow(item, i)); });
    pane.appendChild(host);

    pane.appendChild(el("div", { class: "flex mt" }, [
      el("button", {
        class: "btn", onclick: function () {
          S.kb.checklist.push({ key: "custom" + Date.now(), label: "Something else",
                                why: "", phrases: [], on: true });
          S.report = null; save(); render();
        }
      }, ["Add one"]),
      el("button", {
        class: "btn ghost", onclick: function () {
          UI.guard(api.put("/api/kbs/" + S.kb.id, { checklist: null }), "Reset");
        }, disabled: true, style: { display: "none" }
      }, ["Reset"])
    ]));
  }

  function checklistRow(item, i) {
    var on = el("input", { type: "checkbox", checked: item.on !== false });
    on.addEventListener("change", function () {
      item.on = on.checked; S.report = null; save();
    });
    var label = el("input", { type: "text", value: item.label });
    label.addEventListener("input", function () {
      item.label = label.value; S.report = null; save();
    });
    var phrases = el("input", { type: "text", value: (item.phrases || []).join(", ") });
    phrases.addEventListener("input", function () {
      item.phrases = phrases.value.split(",").map(function (x) { return x.trim(); })
                            .filter(Boolean);
      S.report = null; save();
    });
    var del = el("button", {
      class: "btn sm ghost", title: "Remove",
      onclick: function () {
        S.kb.checklist.splice(i, 1); S.report = null; save(); render();
      }
    }, ["×"]);
    return el("div", { class: "chk-item" }, [
      el("div", { class: "row1" }, [on, label, del]),
      item.why ? el("div", { class: "muted small", style: { marginTop: "4px" }, text: item.why })
               : null,
      el("div", { class: "ph" }, [el("label", { text: "Words that count as finding it" }), phrases])
    ]);
  }

  // ------------------------------------------------------------ inspector

  function renderInspector() {
    var host = $("#inspector");
    UI.clear(host);
    if (!S.kb) return;
    var st = S.kb.stats || {};
    var pad = el("div", { class: "pad" }, [el("h3", { text: "At a glance" })]);
    pad.appendChild(kv("Files seen", UI.fmtInt(st.total || 0)));
    pad.appendChild(kv("Searchable", UI.fmtInt(st.ok || 0)));
    pad.appendChild(kv("Cannot open", UI.fmtInt(st.unreadable || 0)));
    pad.appendChild(kv("Not documents", UI.fmtInt(st.skipped || 0)));
    pad.appendChild(kv("Words", UI.fmtInt(st.words || 0)));
    pad.appendChild(kv("On disk", bytes(st.bytes || 0)));
    pad.appendChild(kv("Last read", S.kb.last_scan ? UI.fmtAgo(S.kb.last_scan) : "never"));
    if (S.kb.partial) {
      pad.appendChild(el("div", { class: "headline bad", style: { marginTop: "8px" },
        text: "The last read was stopped early, so this covers part of the folder. " +
              "Read it again before showing the report to anyone." }));
    }
    host.appendChild(pad);

    var notes = el("textarea", { placeholder: "Notes for this client — what to ask about, " +
                                              "what they said, what to chase.",
                                 style: { minHeight: "150px" } });
    notes.value = S.kb.notes || "";
    notes.addEventListener("input", function () { S.kb.notes = notes.value; save(); });
    host.appendChild(el("div", { class: "pad" }, [
      el("h3", { text: "Notes" }), notes,
      el("div", { class: "hint", text: "Saved as you type." })
    ]));

    host.appendChild(el("div", { class: "pad" }, [
      el("div", { class: "muted small",
        text: "This app only reads. It never writes to, moves, or renames anything in the " +
              "client's folder." })
    ]));
  }

  function kv(k, v) {
    return el("div", { class: "kv" }, [el("span", { text: k }), el("span", { text: v })]);
  }

  // ------------------------------------------------------------ help

  function showHelp() {
    var body = el("div", {}, [
      helpBit("Copies that disagree",
        "Two kinds. Identical means the words are exactly the same. Near-copy means most of " +
        "the sentences match — a document that was saved, edited, and saved again under a new " +
        "name. The finding is not the wasted disk space. It is that when four price lists " +
        "disagree, whoever picks the wrong one quotes the wrong number."),
      helpBit("Documents nobody has touched",
        "Grouped by how long since the file was last changed. A procedure that has not been " +
        "edited in five years is either perfect or ignored, and it is almost never perfect."),
      helpBit("Files nothing else points at",
        "No other document mentions this file's name. It may still be important — but nobody " +
        "is routed to it, so in day-to-day practice it does not exist."),
      helpBit("Files nobody can open or search",
        "Scanned PDFs, old Word and Excel formats, password-locked files, files that are " +
        "damaged. In a company with decades of paper this is often the biggest bucket, and " +
        "it is a business finding, not a computer one."),
      helpBit("What is missing",
        "A short list of things a business of 10 to 50 people normally has written down. " +
        "Missing does not always mean wrong — but it should be a deliberate choice, not a " +
        "surprise. Edit the list per client on the “What to check for” tab."),
      helpBit("How concentrated it all is",
        "How much sits in one folder, one file type, or one person's name. This is a people " +
        "risk. It tells you what breaks when that person leaves."),
      helpBit("The readiness score",
        "Five measurements, each with a weight, adding to 100. Every line shows the two " +
        "numbers behind it so the owner can check the arithmetic and argue with it. There is " +
        "no hidden model.")
    ]);
    UI.modal({ title: "What each finding means", body: body, wide: true });
  }

  function helpBit(title, text) {
    return el("div", { style: { marginBottom: "14px" } }, [
      el("div", { style: { fontWeight: "600", marginBottom: "3px" }, text: title }),
      el("div", { class: "muted", style: { fontSize: "13px" }, text: text })
    ]);
  }

  // ------------------------------------------------------------ boot

  function boot() {
    $("#themeSlot").appendChild(UI.themeButton());
    $("#btnNewKb").addEventListener("click", newKb);
    $("#btnHelp").addEventListener("click", showHelp);

    UI.$$("#tabs .tab").forEach(function (b) {
      b.addEventListener("click", function () { setTab(b.dataset.tab); });
    });

    var q = $("#q");
    q.addEventListener("input", function () {
      S.query = q.value.trim();
      clearTimeout(searchTimer);
      if (!S.query) { S.results = null; paintSearchMeta(); render(); return; }
      searchTimer = setTimeout(runSearch, 140);
    });
    q.addEventListener("keydown", function (e) {
      if (e.key === "Escape") { q.value = ""; S.query = ""; S.results = null;
                                paintSearchMeta(); render(); }
      if (e.key === "Enter") { clearTimeout(searchTimer); runSearch(); }
    });

    UI.hotkey("/", function () { setTab("search"); q.focus(); q.select(); });
    UI.hotkey("ctrl+k", function () { setTab("search"); q.focus(); q.select(); }, true);
    UI.hotkey("ctrl+r", function () { startScan(); }, true);
    window.addEventListener("beforeunload", function () { save.now(); });

    paintSearchMeta();
    return loadKbs().then(function () {
      render();
      paintSearchMeta();
    });
  }

  function done() { save.idle(); UI.booted(); }

  try {
    boot().then(done, function (e) {
      var pane = $("#pane");
      if (pane && !pane.firstChild) {
        pane.appendChild(empty("⚠", "Could not start",
          (e && e.message) || "The local server did not answer."));
      }
      done();
    });
  } catch (e) {
    done();
    throw e;
  }
})();
