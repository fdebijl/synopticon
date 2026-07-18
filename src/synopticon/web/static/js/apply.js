/* Apply page: pick kinds + scope, run a free dry-run preview, then a gated
   apply-to-NAS. Consent (plan §6) is mirrored into the job params: reassign and
   merge need explicit "I understand" acknowledgements; merge_named needs the
   typed-phrase dialog. Apply-to-NAS stays disabled until a preview for the exact
   current kind-set has succeeded this session. The phrase text lives only in the
   client for the user's own typing; the server re-validates every request. */
(function () {
  "use strict";

  var MERGE_NAMED_PHRASE = "merge named people";
  var panel = null;
  var previewedKey = null;   // kind-set key of the last successful preview
  var pendingPreviewKey = null;
  var mode = null;           // 'preview' | 'apply'

  function selectedKinds() {
    var out = [];
    document.querySelectorAll("[data-kind]").forEach(function (cb) {
      if (cb.checked) out.push(cb.getAttribute("data-kind"));
    });
    return out;
  }

  function kindsKey(kinds) { return kinds.slice().sort().join(","); }

  function baseParams(kinds) {
    var params = { kinds: kinds };
    var space = document.getElementById("apply-space").value.trim();
    if (space) params.space = space;
    var person = document.getElementById("apply-person").value.trim();
    if (person) params.person_id = person;
    return params;
  }

  function refreshConsentVisibility() {
    var kinds = selectedKinds();
    document.getElementById("consent-reassign").classList.toggle("hidden", kinds.indexOf("reassign") < 0);
    document.getElementById("consent-merge").classList.toggle("hidden", kinds.indexOf("merge") < 0);
    // Any kind change invalidates a prior preview.
    if (previewedKey !== kindsKey(kinds)) updateApplyEnabled();
  }

  function updateApplyEnabled() {
    var kinds = selectedKinds();
    var ok = previewedKey !== null && previewedKey === kindsKey(kinds);
    var btn = document.getElementById("btn-apply");
    btn.disabled = !ok;
    var hint = document.getElementById("apply-hint");
    hint.textContent = ok
      ? "Preview succeeded for this kind-set — Apply is enabled."
      : "Run a preview for the selected kinds to enable Apply.";
  }

  function loadCounts() {
    Syn.fetchJSON("/api/review/counts").then(function (res) {
      var approved = (res.counts && res.counts.approved) || {};
      document.querySelectorAll("[data-kind-count]").forEach(function (el) {
        var k = el.getAttribute("data-kind-count");
        el.textContent = (approved[k] || 0) + " approved";
      });
    }).catch(function () { /* transient */ });
  }

  function preview() {
    var kinds = selectedKinds();
    var params = baseParams(kinds);
    params.dry_run = true;
    mode = "preview";
    pendingPreviewKey = kindsKey(kinds);
    hide("audit-card");
    panel.start("apply", params).catch(function (e) { Syn.toast(e.message || "Preview failed", "error"); });
  }

  function submitApply(extraConsent) {
    var kinds = selectedKinds();
    var params = baseParams(kinds);
    params.dry_run = false;
    if (kinds.indexOf("reassign") >= 0) params.apply_reassigns = true;
    if (kinds.indexOf("merge") >= 0) params.apply_merges = true;
    var body = { name: "apply", params: params, confirm: true };
    if (extraConsent && extraConsent.confirm_phrase) body.confirm_phrase = extraConsent.confirm_phrase;
    mode = "apply";
    panel.start("apply", params, { confirm: true, confirm_phrase: body.confirm_phrase })
      .catch(function (e) {
        if (e.status === 428) Syn.toast("Consent required: " + (e.data && e.data.requirement || ""), "error");
        else Syn.toast(e.message || "Apply failed", "error");
      });
  }

  function apply() {
    var kinds = selectedKinds();
    if (kinds.indexOf("reassign") >= 0 && !document.getElementById("ack-reassign").checked) {
      Syn.toast("Acknowledge the reassign warning first.", "error"); return;
    }
    if (kinds.indexOf("merge") >= 0 && !document.getElementById("ack-merge").checked) {
      Syn.toast("Acknowledge the merge warning first.", "error"); return;
    }
    if (kinds.indexOf("merge_named") >= 0) {
      openMergeNamedDialog(function (phrase) { submitApply({ confirm_phrase: phrase }); });
      return;
    }
    submitApply(null);
  }

  function openMergeNamedDialog(onConfirm) {
    var dlg = document.getElementById("merge-named-dialog");
    var list = document.getElementById("mnd-list");
    var input = document.getElementById("mnd-input");
    var ok = document.getElementById("mnd-ok");
    var cancel = document.getElementById("mnd-cancel");
    var label = document.getElementById("mnd-label");
    list.textContent = "";
    input.value = "";
    ok.disabled = true;
    label.textContent = 'Type "' + MERGE_NAMED_PHRASE + '" to confirm';

    Syn.fetchJSON("/api/review/named-merge-pairs").then(function (res) {
      var pairs = res.pairs || [];
      if (!pairs.length) {
        var li = document.createElement("li"); li.className = "muted";
        li.textContent = "No approved named↔named merges are queued.";
        list.appendChild(li);
      }
      pairs.forEach(function (p) {
        var li = document.createElement("li");
        var a = document.createElement("strong"); a.textContent = p.label_a;
        var arrow = document.createElement("span"); arrow.className = "arrow"; arrow.textContent = "↔";
        var b = document.createElement("strong"); b.textContent = p.label_b;
        li.append(a, arrow, b);
        list.appendChild(li);
      });
    }).catch(function () { /* still allow typed confirm */ });

    var armed = false;
    ok.disabled = true;
    setTimeout(function () { armed = true; recheck(); }, 2000);
    function recheck() { ok.disabled = !(armed && input.value === MERGE_NAMED_PHRASE); }
    input.oninput = recheck;

    function onClose() {
      dlg.removeEventListener("close", onClose);
      if (dlg.returnValue === "ok" && input.value === MERGE_NAMED_PHRASE) onConfirm(input.value);
    }
    dlg.addEventListener("close", onClose);
    if (dlg.showModal) dlg.showModal();
    cancel.focus();
  }

  function hide(id) { document.getElementById(id).classList.add("hidden"); }
  function show(id) { document.getElementById(id).classList.remove("hidden"); }

  function renderStats(stats) {
    var dl = document.getElementById("result-stats");
    dl.textContent = "";
    Object.keys(stats || {}).forEach(function (k) {
      var dt = document.createElement("dt"); dt.textContent = k;
      var dd = document.createElement("dd"); dd.textContent = String(stats[k]);
      dl.append(dt, dd);
    });
    show("result-card");
  }

  function renderAudit() {
    Syn.fetchJSON("/api/audit?limit=50").then(function (res) {
      var tbody = document.getElementById("audit-tail");
      tbody.textContent = "";
      (res.items || []).forEach(function (r) {
        var tr = document.createElement("tr");
        var mark = document.createElement("td");
        mark.className = r.success ? "audit-ok" : "audit-fail";
        mark.textContent = r.success ? "✓" : "✗";
        var action = document.createElement("td"); action.textContent = r.action || "";
        var detail = document.createElement("td");
        detail.className = "mono"; detail.textContent = r.api || "";
        tr.append(mark, action, detail);
        tbody.appendChild(tr);
      });
      show("audit-card");
    }).catch(function () { /* audit optional */ });
  }

  window.addEventListener("DOMContentLoaded", function () {
    var panelEl = document.getElementById("job-panel");
    panel = new JobPanel(panelEl);

    document.querySelectorAll("[data-kind]").forEach(function (cb) {
      cb.addEventListener("change", refreshConsentVisibility);
    });
    document.getElementById("btn-preview").addEventListener("click", preview);
    document.getElementById("btn-apply").addEventListener("click", apply);

    panelEl.addEventListener("synopticon:job-done", function (e) {
      var d = e.detail || {};
      if (d.event === "result") { renderStats(d.stats); return; }
      // terminal (final) event carries .state
      if (d.state === "succeeded") {
        if (mode === "preview") {
          previewedKey = pendingPreviewKey;
          updateApplyEnabled();
        } else if (mode === "apply") {
          renderAudit();
          loadCounts();
        }
      }
    });

    loadCounts();
    refreshConsentVisibility();
  });
})();
