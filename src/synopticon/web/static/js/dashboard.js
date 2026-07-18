/* Dashboard: stat tiles + pipeline-state strip + audit tail, rendered from the
   /api/stats + /api/audit payloads. Initial data is embedded server-side
   (window.SYN_STATS / SYN_AUDIT) so the first paint needs no fetch; while a job
   is running the page re-fetches /api/stats every ~10s (otherwise it stays
   static — only a cheap /api/jobs heartbeat runs to catch a job starting
   elsewhere). The server decides the empty-DB state; when it renders the
   "run your first sync" CTA instead of the tiles there is nothing to hydrate. */
(function () {
  "use strict";

  var REFRESH_MS = 10000;

  function num(n) { return (Number(n) || 0).toLocaleString(); }

  function ago(ts) {
    if (!ts) return "—";
    var s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
    if (s < 60) return s + "s ago";
    var m = Math.floor(s / 60);
    if (m < 60) return m + "m ago";
    var h = Math.floor(m / 60);
    if (h < 24) return h + "h ago";
    return Math.floor(h / 24) + "d ago";
  }

  function sumKinds(byKind) {
    var t = 0;
    Object.keys(byKind || {}).forEach(function (k) { t += byKind[k] || 0; });
    return t;
  }

  function statusTotal(review, status) {
    return sumKinds((review || {})[status]);
  }

  /* ------------------------------- tiles -------------------------------- */
  function tileHTML(label, value, sub, href, extra) {
    var tag = href ? "a" : "div";
    var attrs = href ? ' class="tile" href="' + href + '"' : ' class="tile"';
    return "<" + tag + attrs + ">" +
      '<div class="tile-label">' + label + "</div>" +
      '<div class="tile-value">' + value + "</div>" +
      (sub ? '<div class="tile-sub">' + sub + "</div>" : "") +
      (extra || "") +
      "</" + tag + ">";
  }

  function renderTiles(stats) {
    var host = document.getElementById("stat-tiles");
    if (!host) return;
    var html = "";

    var photos = stats.photos || {};
    Object.keys(photos).forEach(function (space) {
      var p = photos[space];
      var sub = num(p.hashed) + " hashed" +
        (p.deleted ? " · " + num(p.deleted) + " deleted" : "");
      html += tileHTML("Photos · " + space, num(p.synced), sub, "/pipeline");
    });

    html += tileHTML("Faces", num(stats.faces),
      num(stats.embeddings) + " embeddings", "/pipeline");

    var ex = stats.extract || {};
    if (!ex.models_ready) {
      html += tileHTML("Extracted", "—",
        '<span class="tile-hint"><a href="/pipeline">Download models</a> to enable extraction</span>',
        null);
    } else {
      var pct = ex.coverage != null ? Math.round(ex.coverage * 100) + "% covered" : "no eligible photos";
      var val = num(ex.processed) + " / " + num(ex.eligible);
      html += tileHTML("Extracted", val, pct, "/pipeline");
    }

    var cl = stats.cluster;
    if (cl) {
      html += tileHTML("Clusters", num(cl.clusters),
        "run #" + cl.run_id + " · " + ago(cl.created_at), "/pipeline");
    } else {
      html += tileHTML("Clusters", "—", "not clustered yet", "/pipeline");
    }

    var review = stats.review || {};
    var pending = statusTotal(review, "pending");
    var breakdown = "";
    ["approved", "applied", "rejected", "failed"].forEach(function (st) {
      var n = statusTotal(review, st);
      if (n) breakdown += '<a href="/review?status=' + st + '">' + st + " " + num(n) + "</a>";
    });
    var extra = '<div class="review-breakdown">' +
      (breakdown || '<span class="muted">no decisions yet</span>') + "</div>";
    html += tileHTML("Review queue", num(pending), "pending",
      "/review?status=pending", extra);

    host.innerHTML = html;
  }

  /* -------------------------- pipeline strip ---------------------------- */
  function computeStages(stats) {
    var photos = stats.photos || {};
    var synced = 0;
    Object.keys(photos).forEach(function (k) { synced += photos[k].synced || 0; });

    var ex = stats.extract || {};
    var exState, exNote;
    if (!ex.models_ready) { exState = "pending"; exNote = "models needed"; }
    else if (ex.eligible > 0 && ex.processed != null && ex.processed >= ex.eligible) {
      exState = "done"; exNote = "complete";
    } else if (ex.processed > 0) {
      exState = "active"; exNote = num(ex.processed) + " / " + num(ex.eligible);
    } else { exState = "pending"; exNote = "not started"; }

    var cl = stats.cluster;
    var review = stats.review || {};
    var pending = statusTotal(review, "pending");
    var approved = statusTotal(review, "approved");
    var applied = statusTotal(review, "applied");
    var totalItems = pending + approved + applied +
      statusTotal(review, "rejected") + statusTotal(review, "failed");

    var revState, revNote;
    if (totalItems === 0) { revState = "pending"; revNote = "no items"; }
    else if (pending > 0) { revState = "active"; revNote = num(pending) + " pending"; }
    else { revState = "done"; revNote = "all reviewed"; }

    var apState, apNote;
    if (approved > 0) { apState = "active"; apNote = num(approved) + " approved"; }
    else if (applied > 0) { apState = "done"; apNote = num(applied) + " applied"; }
    else { apState = "pending"; apNote = "nothing to apply"; }

    return [
      { label: "Sync", href: "/pipeline", state: synced > 0 ? "done" : "pending",
        note: synced > 0 ? num(synced) + " synced" : "not started" },
      { label: "Extract", href: "/pipeline", state: exState, note: exNote },
      { label: "Cluster", href: "/pipeline", state: cl ? "done" : "pending",
        note: cl ? num(cl.clusters) + " clusters" : "not started" },
      { label: "Review", href: "/review", state: revState, note: revNote },
      { label: "Apply", href: "/apply", state: apState, note: apNote },
    ];
  }

  function renderStrip(stats) {
    var host = document.getElementById("pipeline-strip");
    if (!host) return;
    var stages = computeStages(stats);
    var parts = ['<h3 class="sr-only">Pipeline status</h3><div class="strip">'];
    stages.forEach(function (s, i) {
      if (i > 0) parts.push('<span class="stage-sep" aria-hidden="true">›</span>');
      parts.push(
        '<a class="stage ' + s.state + '" href="' + s.href + '">' +
        '<span class="stage-name"><span class="stage-dot" aria-hidden="true"></span>' +
        s.label + "</span>" +
        '<span class="stage-note">' + s.note + "</span></a>"
      );
    });
    parts.push("</div>");
    host.innerHTML = parts.join("");
  }

  /* ------------------------------ audit --------------------------------- */
  function renderAudit(items) {
    var host = document.getElementById("audit-tail");
    if (!host) return;
    if (!items || !items.length) {
      host.innerHTML = '<p class="muted">No writes recorded yet.</p>';
      return;
    }
    var table = document.createElement("table");
    table.className = "data";
    table.innerHTML =
      "<thead><tr><th>When</th><th>Action</th><th>Result</th></tr></thead>";
    var tbody = document.createElement("tbody");
    items.forEach(function (it) {
      var tr = document.createElement("tr");
      var when = document.createElement("td");
      when.textContent = it.ts ? new Date(it.ts * 1000).toLocaleString() : "—";
      var action = document.createElement("td");
      action.textContent = it.action || "";
      var result = document.createElement("td");
      if (it.success === 1 || it.success === true) {
        result.textContent = "✓"; result.className = "audit-ok";
      } else if (it.success === 0 || it.success === false) {
        result.textContent = "✗"; result.className = "audit-fail";
      } else {
        result.textContent = "—";
      }
      tr.append(when, action, result);
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    host.replaceChildren(table);
  }

  function render(stats, audit) {
    renderTiles(stats);
    renderStrip(stats);
    if (audit !== undefined) renderAudit(audit);
  }

  /* ---------------------------- live refresh ---------------------------- */
  function jobActive(stats) {
    return !!(stats.job && stats.job.current);
  }

  function start() {
    // Nothing to hydrate on the empty-DB CTA page.
    if (!document.getElementById("stat-tiles")) return;

    var initial = window.SYN_STATS || {};
    render(initial, window.SYN_AUDIT || []);

    var wasRunning = !!window.SYN_RUNNING;

    setInterval(function () {
      // Cheap heartbeat: only pull full stats while (or just after) a job runs.
      Syn.fetchJSON("/api/jobs").then(function (res) {
        var running = (res.items || []).some(function (m) {
          return m.state === "queued" || m.state === "running";
        });
        if (running || wasRunning) {
          Promise.all([
            Syn.fetchJSON("/api/stats"),
            Syn.fetchJSON("/api/audit?limit=20"),
          ]).then(function (r) {
            render(r[0], (r[1] && r[1].items) || []);
          }).catch(function () { /* transient */ });
        }
        wasRunning = running;
      }).catch(function () { /* transient */ });
    }, REFRESH_MS);
  }

  window.addEventListener("DOMContentLoaded", start);
})();
