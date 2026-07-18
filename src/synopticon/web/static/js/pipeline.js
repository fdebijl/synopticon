/* Pipeline page: command cards -> whitelisted option forms -> POST /api/jobs
   via the shared JobPanel, plus a job-history table. All Run buttons disable
   while any job is queued/running (polled from /api/jobs and refreshed on the
   job-done event). Server is the single validator; the forms only collect the
   params each JOB_SPECS builder accepts. */
(function () {
  "use strict";

  var panel = null;
  var pollTimer = null;

  function collectParams(card) {
    var params = {};
    card.querySelectorAll("[data-param]").forEach(function (el) {
      var key = el.getAttribute("data-param");
      if (el.type === "checkbox") {
        params[key] = el.checked;
      } else {
        var v = (el.value || "").trim();
        if (v !== "") params[key] = v;
      }
    });
    if (card.getAttribute("data-cmd") === "recluster") {
      params.overrides = collectOverrides(card);
    }
    return params;
  }

  function collectOverrides(card) {
    var out = {};
    card.querySelectorAll("[data-kv] .kv-row").forEach(function (row) {
      var k = (row.querySelector("[data-kv-key]").value || "").trim();
      var raw = (row.querySelector("[data-kv-val]").value || "").trim();
      if (!k) return;
      var val;
      try { val = JSON.parse(raw); } catch (e) { val = raw; }
      out[k] = val;
    });
    return out;
  }

  function addKvRow(container) {
    var row = document.createElement("div");
    row.className = "kv-row";
    row.innerHTML =
      '<input class="input" data-kv-key placeholder="clustering.threshold">' +
      '<input class="input" data-kv-val placeholder="0.55">' +
      '<button type="button" class="btn btn-sm btn-ghost" data-kv-del aria-label="Remove">&times;</button>';
    row.querySelector("[data-kv-del]").addEventListener("click", function () { row.remove(); });
    container.appendChild(row);
  }

  function run(card) {
    var name = card.getAttribute("data-cmd");
    var params = collectParams(card);
    panel.start(name, params)
      .then(function () { setRunning(true); refreshHistory(); })
      .catch(function (err) { Syn.toast(err.message || "Failed to start job", "error"); });
  }

  function setRunning(running) {
    document.querySelectorAll("[data-run]").forEach(function (b) { b.disabled = running; });
    var note = document.getElementById("pipeline-run-note");
    if (note) note.hidden = !running;
  }

  function fmtDuration(m) {
    if (!m.started_at) return "—";
    var end = m.ended_at || (Date.now() / 1000);
    var s = Math.max(0, Math.round(end - m.started_at));
    if (s < 60) return s + "s";
    var mm = Math.floor(s / 60), ss = s % 60;
    return mm + "m " + ss + "s";
  }

  function refreshHistory() {
    Syn.fetchJSON("/api/jobs").then(function (res) {
      var items = res.items || [];
      var active = items.some(function (m) { return m.state === "queued" || m.state === "running"; });
      setRunning(active);
      var tbody = document.getElementById("job-history");
      if (!tbody) return;
      if (!items.length) {
        tbody.innerHTML = '<tr><td colspan="4" class="muted">No jobs yet.</td></tr>';
        return;
      }
      tbody.textContent = "";
      items.forEach(function (m) {
        var tr = document.createElement("tr");
        var name = document.createElement("td"); name.className = "hist-name"; name.textContent = m.name;
        var state = document.createElement("td");
        var badge = document.createElement("span"); badge.className = "badge state-" + m.state; badge.textContent = m.state;
        state.appendChild(badge);
        var dur = document.createElement("td"); dur.textContent = fmtDuration(m);
        var link = document.createElement("td");
        var a = document.createElement("a"); a.href = "/jobs/" + m.id; a.textContent = "view"; link.appendChild(a);
        tr.append(name, state, dur, link);
        tbody.appendChild(tr);
      });
    }).catch(function () { /* transient */ });
  }

  window.addEventListener("DOMContentLoaded", function () {
    var panelEl = document.getElementById("job-panel");
    panel = new JobPanel(panelEl);

    document.querySelectorAll(".cmd-card").forEach(function (card) {
      var toggle = card.querySelector("[data-toggle]");
      if (toggle) toggle.addEventListener("click", function () { card.classList.toggle("open"); });
      var runBtn = card.querySelector("[data-run]");
      if (runBtn) runBtn.addEventListener("click", function () { run(card); });
      var kv = card.querySelector("[data-kv]");
      var kvAdd = card.querySelector("[data-kv-add]");
      if (kv && kvAdd) {
        addKvRow(kv);
        kvAdd.addEventListener("click", function () { addKvRow(kv); });
      }
    });

    panelEl.addEventListener("synopticon:job-done", function () { setRunning(false); refreshHistory(); });

    refreshHistory();
    pollTimer = setInterval(refreshHistory, 4000);
    window.addEventListener("beforeunload", function () { if (pollTimer) clearInterval(pollTimer); });
  });
})();
