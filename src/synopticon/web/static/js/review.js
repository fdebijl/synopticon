/* Review queue: keyboard flow (y/n/s/j/k), session-local undo (u), bulk approve,
   unnamed filters, and infinite scroll via IntersectionObserver fetching
   /api/review/items. Card markup mirrors partials/review_card.html.j2 — keep the
   class names / data-* attrs in sync (they are the contract the flow relies on).

   Input-focus guard: keyboard shortcuts are ignored while a text input/textarea
   has focus, so typing "y" in a new_person name field no longer approves the
   card (a real bug in the legacy UI). */
(function () {
  "use strict";

  var cfg = window.SYN_REVIEW || { kind: "", status: "pending", total: 0, loaded: 0, pageSize: 100 };
  var grid = document.getElementById("grid");
  var loadedCountEl = document.getElementById("loaded-count");
  var endNote = document.getElementById("end-note");
  var sentinel = document.getElementById("scroll-sentinel");
  var undoBtn = document.getElementById("undo-btn");

  var loaded = cfg.loaded;
  var total = cfg.total;
  var loading = false;
  var exhausted = loaded >= total;
  var undoStack = []; // {id, decision, kind} — session-local only
  var idx = 0;

  function esc(s) {
    var d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }

  var HIDDEN_BADGE =
    '<span class="hidden-badge" title="Hidden on Synology Photos" aria-label="Person hidden on Synology Photos">' +
    '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 ' +
    '9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/>' +
    '<line x1="1" y1="1" x2="23" y2="23"/></svg></span>';

  function link(url, label) {
    if (url) return '<a href="' + esc(url) + '" target="_blank" rel="noopener">' + esc(label) + "</a>";
    return esc(label);
  }
  function thumbs(list) {
    return '<div class="thumbs">' + (list || []).map(function (c) {
      return c ? '<img src="' + esc(c) + '" alt="">' : "";
    }).join("") + "</div>";
  }
  function personName(p) { return (p && (p.name || p.person_id)) || ""; }

  /* Build a card element from a shaped item dict (see review/queries.py). */
  function renderCard(it) {
    var wrap = document.createElement("div");
    var attrs = 'data-id="' + it.item_id + '"';
    if (it.unnamed_target) attrs += ' data-unnamed-target="1"';
    if (it.unnamed_merge) attrs += ' data-unnamed-merge="1"';
    if (it.named_merge) attrs += ' data-named-merge="1"';
    var body = "";

    if (it.crop && it.kind !== "reassign") {
      var img = '<img src="' + esc(it.crop) + '" alt="face crop">';
      body += it.item_url
        ? '<a href="' + esc(it.item_url) + '" target="_blank" rel="noopener">' + img + "</a>"
        : img;
    }

    var p = it.payload || {};
    if (it.kind === "new_person") {
      body += thumbs(it.new_person_crops);
      body += '<input class="name-input" placeholder="suggested name" value="' + esc(p.suggested_name || "") +
        '" data-name-input="' + it.item_id + '" aria-label="Suggested name">';
    } else if (it.kind === "merge" || it.kind === "merge_named") {
      if (it.named_merge) {
        body += '<div class="danger-banner">&#9888; named &harr; named — irreversible, destroys a human label</div>';
      }
      body += '<div class="merge"><div class="merge-side"><div class="merge-name"><strong>' +
        link(it.person_a_url, personName(p.person_a)) + "</strong></div>" +
        '<div class="thumb-group">' + (it.person_a_hidden ? HIDDEN_BADGE : "") + thumbs(it.merge_crops_a) + "</div></div>" +
        '<div class="merge-arrow" aria-hidden="true">&harr;</div>' +
        '<div class="merge-side"><div class="merge-name"><strong>' +
        link(it.person_b_url, personName(p.person_b)) + "</strong></div>" +
        '<div class="thumb-group">' + (it.person_b_hidden ? HIDDEN_BADGE : "") + thumbs(it.merge_crops_b) + "</div></div></div>";
    } else if (it.kind === "reassign") {
      var crop = it.crop
        ? (it.item_url
            ? '<a href="' + esc(it.item_url) + '" target="_blank" rel="noopener"><img src="' + esc(it.crop) + '" alt="face crop"></a>'
            : '<img src="' + esc(it.crop) + '" alt="face crop">')
        : "";
      body += '<div class="merge"><div class="merge-side"><div class="merge-name"><strong>' +
        link(it.from_person_url, p.from_person_name || p.from_person_id) + "</strong></div>" + crop + "</div>" +
        '<div class="merge-arrow" aria-hidden="true">&rarr;</div>' +
        '<div class="merge-side"><div class="merge-name"><strong>' +
        link(it.person_url, p.person_name || p.person_id) + "</strong></div>" +
        '<div class="thumb-group">' + (it.target_hidden ? HIDDEN_BADGE : "") + thumbs(it.target_crops) + "</div></div></div>";
      body += '<div class="muted">to-sim ' + (it.confidence || 0).toFixed(3) +
        (p.from_similarity != null ? " · from-sim " + p.from_similarity.toFixed(3) : "") + "</div>";
    } else {
      body += '<div class="merge-name"><strong>' + esc(p.person_name || p.person_id) + "</strong>" +
        (it.target_hidden ? HIDDEN_BADGE : "") + "</div>";
      body += '<div class="muted">conf ' + (it.confidence || 0).toFixed(3) + "</div>";
    }

    body += '<div class="muted card-kind">' + esc(it.kind) + " · " + esc(it.status) + "</div>";
    body += '<div class="card-actions">' +
      '<button class="btn btn-sm btn-primary" data-decide="approve" data-id="' + it.item_id + '">&check; <kbd>y</kbd></button>' +
      '<button class="btn btn-sm btn-ghost" data-decide="reject" data-id="' + it.item_id + '">&cross; <kbd>n</kbd></button></div>';

    wrap.innerHTML = '<div class="card" ' + attrs + ' tabindex="0">' + body + "</div>";
    return wrap.firstChild;
  }

  function visibleCards() {
    return Array.from(grid.querySelectorAll(".card")).filter(function (c) {
      return c.offsetParent !== null && !c.classList.contains("decided");
    });
  }
  function select(i) {
    var cs = visibleCards();
    grid.querySelectorAll(".card.sel").forEach(function (c) { c.classList.remove("sel"); });
    if (!cs.length) { idx = 0; return; }
    idx = Math.max(0, Math.min(i, cs.length - 1));
    cs[idx].classList.add("sel");
    cs[idx].scrollIntoView({ block: "nearest" });
  }
  /* Move selection onto a specific card element (undo target), if it is
     currently visible. Scrolls it into view like keyboard navigation. */
  function selectCard(el) {
    if (!el) return;
    var i = visibleCards().indexOf(el);
    if (i >= 0) select(i);
  }

  var STATUS_BY_DECISION = { approve: "approved", reject: "rejected", undo: "pending" };

  /* The card's "<kind> · <status>" footer is the client-side status display.
     Read the kind once (cached on data-kind) and rewrite the status half. */
  function cardKind(el) {
    var k = el.querySelector(".card-kind");
    if (!k) return "";
    return (k.dataset.kind || k.textContent.split("·")[0]).trim();
  }
  function setCardStatus(el, status) {
    var k = el.querySelector(".card-kind");
    if (!k) return;
    var kind = cardKind(el);
    k.dataset.kind = kind;
    k.textContent = kind + " · " + status;
  }

  function updateUndoButton() {
    if (!undoBtn) return;
    var last = undoStack[undoStack.length - 1];
    undoBtn.disabled = !last;
    undoBtn.title = last
      ? "Undo " + last.decision + " of " + last.kind + " #" + last.id
      : "Nothing to undo";
  }

  /* Decide one card. Keyboard (y/n) passes {advance:true} to move selection to
     the next card; mouse clicks pass {advance:false} so the selection and
     scroll position stay put. Shared by both input paths. */
  function decide(card, decision, opts) {
    if (!card) return Promise.resolve();
    var advance = !!(opts && opts.advance);
    var id = card.dataset.id;
    return Syn.postJSON("/api/review/" + id + "/decide", { decision: decision })
      .then(function () {
        card.classList.add("decided");
        setCardStatus(card, STATUS_BY_DECISION[decision] || "decided");
        undoStack.push({ id: id, decision: decision, kind: cardKind(card) });
        updateUndoButton();
        // The decided card drops out of visibleCards; re-selecting idx lands on
        // the next card (advance). On mouse clicks we leave selection alone.
        if (advance) select(idx);
      })
      .catch(function (e) { Syn.toast(e.message, "error"); });
  }

  /* Real undo: revert the last approve/reject server-side, restore the card,
     and move selection to it. Session-local stack, most-recent first. */
  function undo() {
    var last = undoStack.pop();
    updateUndoButton();
    if (!last) { Syn.toast("Nothing to undo"); return; }
    Syn.postJSON("/api/review/" + last.id + "/decide", { decision: "undo" })
      .then(function () {
        var el = grid.querySelector('.card[data-id="' + last.id + '"]');
        if (el) {
          el.classList.remove("decided");
          setCardStatus(el, "pending");
        }
        Syn.toast("Undid " + last.decision + " of " + last.kind + " #" + last.id);
        selectCard(el);
      })
      .catch(function (e) { Syn.toast(e.message, "error"); });
  }

  function setName(id, name) {
    Syn.postJSON("/api/review/" + id + "/name", { name: name })
      .catch(function (e) { Syn.toast(e.message, "error"); });
  }

  function updateCount() {
    if (loadedCountEl) loadedCountEl.textContent = loaded + " of " + total + " loaded";
  }

  function loadMore() {
    if (loading || exhausted) return;
    loading = true;
    var url = "/api/review/items?kind=" + encodeURIComponent(cfg.kind) +
      "&status=" + encodeURIComponent(cfg.status) +
      "&limit=" + cfg.pageSize + "&offset=" + loaded;
    Syn.fetchJSON(url).then(function (res) {
      total = res.total;
      (res.items || []).forEach(function (it) { grid.appendChild(renderCard(it)); });
      loaded += (res.items || []).length;
      if (!res.items || !res.items.length || loaded >= total) {
        exhausted = true;
        if (endNote) endNote.hidden = false;
      }
      updateCount();
      loading = false;
    }).catch(function (e) { loading = false; Syn.toast(e.message, "error"); });
  }

  /* -- events -- */
  grid.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-decide]");
    if (btn) {
      var card = btn.closest(".card");
      // Mouse decide: do not advance selection, do not scroll.
      decide(card, btn.dataset.decide, { advance: false });
      return;
    }
  });
  grid.addEventListener("change", function (e) {
    if (e.target.matches("[data-name-input]")) setName(e.target.dataset.nameInput, e.target.value);
  });

  function typingInField() {
    var a = document.activeElement;
    return a && (a.tagName === "INPUT" || a.tagName === "TEXTAREA" || a.isContentEditable);
  }
  /* Resolve the current card from the .sel class and resync `idx` to its
     position in `cs`. Mouse decides remove cards from visibleCards() without
     touching `idx`, so the stored index goes stale; the .sel card is the
     source of truth. If the .sel card itself is decided/filtered out, fall
     forward to the next visible card in DOM order. */
  function currentCard(cs) {
    var sel = grid.querySelector(".card.sel");
    if (sel) {
      var i = cs.indexOf(sel);
      if (i >= 0) { idx = i; return sel; }
      var all = Array.from(grid.querySelectorAll(".card"));
      for (var j = all.indexOf(sel) + 1; j < all.length; j++) {
        var k = cs.indexOf(all[j]);
        if (k >= 0) { idx = k; return all[j]; }
      }
    }
    idx = Math.min(idx, cs.length - 1);
    return cs[idx];
  }

  document.addEventListener("keydown", function (e) {
    if (typingInField()) return; // input-focus guard
    if (e.key === "u") { undo(); return; }
    var cs = visibleCards();
    if (!cs.length) return;
    var cur = currentCard(cs);
    // Keyboard decide: advance selection to the next card.
    if (e.key === "y" && cur) decide(cur, "approve", { advance: true });
    else if (e.key === "n" && cur) decide(cur, "reject", { advance: true });
    else if (e.key === "s") select(idx + 1);
    else if (e.key === "j") select(idx + 1);
    else if (e.key === "k") select(idx - 1);
  });

  if (undoBtn) undoBtn.addEventListener("click", undo);

  var bulkBtn = document.getElementById("bulk-approve");
  if (bulkBtn) bulkBtn.addEventListener("click", function () {
    var conf = parseFloat(document.getElementById("bulk-conf").value) || 0;
    Syn.postJSON("/api/review/bulk", { kind: cfg.kind || "assign", min_confidence: conf })
      .then(function (res) { Syn.toast("Approved " + res.approved); location.reload(); })
      .catch(function (e) { Syn.toast(e.message, "error"); });
  });

  function bindToggle(id, cls, key) {
    var cb = document.getElementById(id);
    if (!cb) return;
    if (localStorage.getItem(key)) { cb.checked = true; document.body.classList.add(cls); }
    cb.addEventListener("change", function () {
      document.body.classList.toggle(cls, cb.checked);
      localStorage.setItem(key, cb.checked ? "1" : "");
      select(0);
    });
  }
  bindToggle("hide-unnamed", "hide-unnamed", "hideUnnamed");
  bindToggle("hide-unnamed-merges", "hide-unnamed-merges", "hideUnnamedMerges");

  // shortcut legend popover
  var legend = document.getElementById("shortcut-legend");
  var legendBtn = document.getElementById("shortcut-legend-btn");
  var legendClose = document.getElementById("shortcut-legend-close");
  if (legendBtn) legendBtn.addEventListener("click", function () { legend.hidden = !legend.hidden; });
  if (legendClose) legendClose.addEventListener("click", function () { legend.hidden = true; });

  // infinite scroll
  if (sentinel && "IntersectionObserver" in window) {
    var io = new IntersectionObserver(function (entries) {
      if (entries.some(function (en) { return en.isIntersecting; })) loadMore();
    }, { rootMargin: "400px" });
    io.observe(sentinel);
  }

  updateCount();
  updateUndoButton();
  if (exhausted && endNote) endNote.hidden = false;
  select(0);
})();
