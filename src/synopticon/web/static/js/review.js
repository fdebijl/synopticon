/* Review queue: keyboard flow (y/n/s/j/k), session-local undo (u), bulk approve,
   unnamed filters, and infinite scroll via IntersectionObserver fetching
   /api/review/items. Card markup mirrors partials/review_card.html.j2 — keep the
   class names / data-* attrs in sync (they are the contract the flow relies on).

   Two layouts share this one file and one #grid DOM: the default Grid, and Focus
   (?view=focus) — a big card projected from the current grid .card plus a
   horizontal carousel of thumbs projected from every grid card. The grid DOM is
   the single source of truth for items, selection (.sel) and decided state
   (.decided); Focus is a pure projection re-rendered by onStateChange(). Focus
   adds ←/→ navigation and page prefetch (the grid's IntersectionObserver sentinel
   is display:none in focus mode and never fires).

   Input-focus guard: keyboard shortcuts are ignored while a text input/textarea
   has focus, so typing "y" in a new_person name field no longer approves the
   card (a real bug in the legacy UI). */
(function () {
  "use strict";

  var cfg = window.SYN_REVIEW || { kind: "", status: "pending", total: 0, loaded: 0, pageSize: 100, view: "grid" };
  var grid = document.getElementById("grid");
  var loadedCountEl = document.getElementById("loaded-count");
  var endNote = document.getElementById("end-note");
  var sentinel = document.getElementById("scroll-sentinel");
  var undoBtn = document.getElementById("undo-btn");
  var page = document.getElementById("review-page");
  var focusCurrentEl = document.getElementById("focus-current");
  var focusEmptyEl = document.getElementById("focus-empty");
  var carouselEl = document.getElementById("carousel");
  var viewInput = document.getElementById("f-view");

  var reducedMotion = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);

  var loaded = cfg.loaded;
  var total = cfg.total;
  var loading = false;
  var exhausted = loaded >= total;
  var undoStack = []; // {id, decision, kind} — session-local only
  var idx = 0;
  var view = "grid"; // resolved and applied by setView() during init

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

  /* A card is "visible" for navigation when it is not decided and not hidden by
     either unnamed filter. This is deliberately NOT `offsetParent !== null`:
     `offsetParent` is null for *every* grid card once the grid is display:none in
     focus mode, so the geometric check can't drive selection there. */
  function cardVisible(c) {
    if (c.classList.contains("decided")) return false;
    var b = document.body.classList;
    if (b.contains("hide-unnamed") && c.hasAttribute("data-unnamed-target")) return false;
    if (b.contains("hide-unnamed-merges") && c.hasAttribute("data-unnamed-merge")) return false;
    return true;
  }
  function visibleCards() {
    return Array.from(grid.querySelectorAll(".card")).filter(cardVisible);
  }
  function select(i) {
    var cs = visibleCards();
    grid.querySelectorAll(".card.sel").forEach(function (c) { c.classList.remove("sel"); });
    if (!cs.length) { idx = 0; onStateChange("select"); return; }
    idx = Math.max(0, Math.min(i, cs.length - 1));
    cs[idx].classList.add("sel");
    if (view === "grid") cs[idx].scrollIntoView({ block: "nearest" });
    onStateChange("select");
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
  function cardStatus(el) {
    var k = el.querySelector(".card-kind");
    if (!k) return "";
    var parts = k.textContent.split("·");
    return parts.length > 1 ? parts[1].trim() : "";
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
     scroll position stay put. Shared by both input paths. In focus mode the big
     card IS the selection, so the focus decide handler always advances. */
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
        onStateChange("decide");
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
        onStateChange("undo");
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
      var appended = [];
      (res.items || []).forEach(function (it) {
        var el = renderCard(it);
        grid.appendChild(el);
        appended.push(el);
      });
      loaded += (res.items || []).length;
      if (!res.items || !res.items.length || loaded >= total) {
        exhausted = true;
        if (endNote) endNote.hidden = false;
      }
      updateCount();
      loading = false;
      onStateChange("loadMore", appended);
    }).catch(function (e) { loading = false; Syn.toast(e.message, "error"); });
  }

  /* -- focus projection ------------------------------------------------------ */
  var KIND_ABBREV = {
    assign: "asn", low_confidence: "low", reassign: "rea",
    merge: "mrg", merge_named: "m·n", new_person: "new",
  };
  function kindAbbrev(kind) { return KIND_ABBREV[kind] || (kind || "?").slice(0, 3); }

  function thumbAria(card) {
    var kind = cardKind(card);
    var status = cardStatus(card) || "pending";
    var names = Array.from(card.querySelectorAll(".merge-name strong"))
      .map(function (s) { return s.textContent.trim(); })
      .filter(Boolean);
    var label = kind + ", " + status;
    if (names.length) label += " — " + names.join(" ↔ ");
    return label;
  }

  /* Reflect a grid card's decided/decision state onto its carousel thumb. */
  function syncThumb(btn, card) {
    var decided = card.classList.contains("decided");
    btn.classList.toggle("decided", decided);
    var status = decided ? cardStatus(card) : "";
    if (status === "approved" || status === "rejected") btn.setAttribute("data-decision", status);
    else btn.removeAttribute("data-decision");
    btn.setAttribute("aria-label", thumbAria(card));
  }

  /* One <button> thumb per grid card, projecting its first img (else a kind
     abbreviation) and carrying the filter data-* attrs so CSS hides the same
     thumbs the grid hides. Keeps a reference to the grid card for cheap syncs. */
  function makeThumb(card) {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "carousel-thumb";
    btn.dataset.id = card.dataset.id;
    if (card.hasAttribute("data-unnamed-target")) btn.setAttribute("data-unnamed-target", "1");
    if (card.hasAttribute("data-unnamed-merge")) btn.setAttribute("data-unnamed-merge", "1");
    if (card.hasAttribute("data-named-merge")) btn.setAttribute("data-named-merge", "1");
    var img = card.querySelector("img");
    var src = img && img.getAttribute("src");
    if (src) {
      var im = document.createElement("img");
      im.src = src;
      im.alt = "";
      btn.appendChild(im);
    } else {
      btn.classList.add("no-img");
      btn.textContent = kindAbbrev(cardKind(card));
    }
    btn._card = card;
    syncThumb(btn, card);
    return btn;
  }
  function appendThumbs(newCards) {
    if (!carouselEl || !newCards || !newCards.length) return;
    var frag = document.createDocumentFragment();
    newCards.forEach(function (c) { frag.appendChild(makeThumb(c)); });
    carouselEl.appendChild(frag);
  }
  function buildCarousel() {
    if (!carouselEl) return;
    carouselEl.innerHTML = "";
    appendThumbs(Array.from(grid.querySelectorAll(".card")));
  }

  function emptyMessage() {
    if (!exhausted) return "Loading…";
    var cards = Array.from(grid.querySelectorAll(".card"));
    if (!cards.length) return "Nothing to review.";
    var anyPending = cards.some(function (c) { return !c.classList.contains("decided"); });
    // Pending items exist but none are visible -> all hidden by the filters.
    if (anyPending) return "All remaining items are hidden by the current filters.";
    return "End of queue — every loaded item is decided.";
  }

  /* Render the big card by deep-cloning the current grid .card. The clone strips
     .sel (the grid card keeps it) and reflects the live name-input value (a DOM
     property cloneNode does not copy). */
  function renderFocus() {
    if (!focusCurrentEl) return;
    var cur = currentCard(visibleCards());
    if (!cur) {
      focusCurrentEl.innerHTML = "";
      if (focusEmptyEl) { focusEmptyEl.hidden = false; focusEmptyEl.textContent = emptyMessage(); }
      return;
    }
    if (focusEmptyEl) focusEmptyEl.hidden = true;
    // Keep the shared selection pinned to what focus shows.
    if (!cur.classList.contains("sel")) {
      grid.querySelectorAll(".card.sel").forEach(function (c) { c.classList.remove("sel"); });
      cur.classList.add("sel");
    }
    var clone = cur.cloneNode(true);
    clone.classList.remove("sel");
    var cloneInput = clone.querySelector("[data-name-input]");
    var origInput = cur.querySelector("[data-name-input]");
    if (cloneInput && origInput) cloneInput.value = origInput.value;
    focusCurrentEl.innerHTML = "";
    focusCurrentEl.appendChild(clone);
  }

  function syncCarousel() {
    if (!carouselEl) return;
    var cur = currentCard(visibleCards());
    var curId = cur ? cur.dataset.id : null;
    var currentBtn = null;
    Array.prototype.forEach.call(carouselEl.children, function (btn) {
      if (btn._card) syncThumb(btn, btn._card);
      if (curId != null && btn.dataset.id === curId) {
        btn.setAttribute("aria-current", "true");
        currentBtn = btn;
      } else {
        btn.removeAttribute("aria-current");
      }
    });
    if (currentBtn) {
      currentBtn.scrollIntoView({ inline: "center", block: "nearest", behavior: reducedMotion ? "auto" : "smooth" });
    }
  }

  /* Prefetch the next page in focus mode: the grid sentinel is display:none and
     its IntersectionObserver never fires, so drive loadMore() from the current
     position instead — within 20 of the end of the loaded cards. */
  function maybePrefetch() {
    if (view !== "focus" || exhausted || loading) return;
    var all = grid.querySelectorAll(".card");
    var sel = grid.querySelector(".card.sel");
    var pos = sel ? Array.prototype.indexOf.call(all, sel) : all.length - 1;
    if (pos >= 0 && all.length - pos <= 20) loadMore();
  }

  /* Single re-projection hook. No-op in grid mode; in focus mode it re-renders
     the big card, syncs the carousel, and prefetches when near the tail. */
  function onStateChange(reason, newCards) {
    if (view !== "focus") return;
    if (newCards && newCards.length) appendThumbs(newCards);
    renderFocus();
    syncCarousel();
    maybePrefetch();
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

  /* Focus big card is a clone that duplicates data-id, so resolve mutations
     against the grid card (never document.querySelector). */
  if (focusCurrentEl) {
    focusCurrentEl.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-decide]");
      if (!btn) return;
      var gridCard = grid.querySelector('.card[data-id="' + btn.dataset.id + '"]');
      if (gridCard) decide(gridCard, btn.dataset.decide, { advance: true });
    });
    focusCurrentEl.addEventListener("change", function (e) {
      if (!e.target.matches("[data-name-input]")) return;
      var id = e.target.dataset.nameInput;
      var val = e.target.value;
      setName(id, val);
      // Mirror into the grid original (property + attribute) so re-clones keep it.
      var orig = grid.querySelector('.card[data-id="' + id + '"] [data-name-input]');
      if (orig) { orig.value = val; orig.setAttribute("value", val); }
    });
  }

  if (carouselEl) {
    carouselEl.addEventListener("click", function (e) {
      var btn = e.target.closest(".carousel-thumb");
      if (!btn) return;
      var gridCard = grid.querySelector('.card[data-id="' + btn.dataset.id + '"]');
      if (!gridCard || !cardVisible(gridCard)) return; // decided/hidden -> no-op
      selectCard(gridCard);
    });
  }

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
    // Focus view only: ←/→ navigate without scrolling the page.
    else if (view === "focus" && e.key === "ArrowLeft") { e.preventDefault(); select(idx - 1); }
    else if (view === "focus" && e.key === "ArrowRight") { e.preventDefault(); select(idx + 1); }
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
      onStateChange("filter");
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

  // infinite scroll (grid mode; the sentinel is display:none in focus mode)
  if (sentinel && "IntersectionObserver" in window) {
    var io = new IntersectionObserver(function (entries) {
      if (entries.some(function (en) { return en.isIntersecting; })) loadMore();
    }, { rootMargin: "400px" });
    io.observe(sentinel);
  }

  /* -- view switching -------------------------------------------------------- */
  function resolveInitialView() {
    try {
      var p = new URLSearchParams(location.search).get("view");
      if (p === "focus" || p === "grid") return p;
      var ls = localStorage.getItem("reviewView");
      if (ls === "focus" || ls === "grid") return ls;
    } catch (e) { /* URL/localStorage unavailable — fall through */ }
    if (cfg.view === "focus" || cfg.view === "grid") return cfg.view;
    return "grid";
  }

  /* Switch layout: root class + toggle aria-pressed + hidden filter input +
     localStorage + URL (?view=focus, dropped for grid). Selection is shared, so
     the current item survives the switch both ways. */
  function setView(v) {
    view = v === "focus" ? "focus" : "grid";
    if (page) page.classList.toggle("view-focus", view === "focus");
    Array.prototype.forEach.call(document.querySelectorAll(".seg-btn"), function (b) {
      b.setAttribute("aria-pressed", b.dataset.view === view ? "true" : "false");
    });
    if (viewInput) viewInput.value = view;
    try { localStorage.setItem("reviewView", view); } catch (e) { /* ignore */ }
    try {
      var url = new URL(location.href);
      if (view === "focus") url.searchParams.set("view", "focus");
      else url.searchParams.delete("view");
      history.replaceState(null, "", url.pathname + url.search + url.hash);
    } catch (e) { /* history unavailable — ignore */ }
    if (view === "focus") {
      buildCarousel();
      renderFocus();
      syncCarousel();
    } else {
      // Back to grid: bring the shared selection into view so the user
      // resumes where focus mode left off.
      var sel = grid.querySelector(".card.sel");
      if (sel) sel.scrollIntoView({ block: "nearest" });
    }
  }
  Array.prototype.forEach.call(document.querySelectorAll(".seg-btn"), function (b) {
    b.addEventListener("click", function () { setView(b.dataset.view); });
  });

  updateCount();
  updateUndoButton();
  if (exhausted && endNote) endNote.hidden = false;
  setView(resolveInitialView());
  select(0);
})();
