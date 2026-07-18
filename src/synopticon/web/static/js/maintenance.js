/* Maintenance page: one card per destructive command with live "what will be
   removed" counts. Consent (plan §6) is enforced by the shared confirm modal:
   clear-queue / delete-crops / reset take a plain confirm; dedupe --apply and
   reset --all take typed phrases ("delete duplicates" / "reset all"). Each
   action submits a job so the shared JobPanel streams its progress. */
(function () {
  "use strict";

  var PHRASE_DEDUPE = "delete duplicates";
  var PHRASE_RESET_ALL = "reset all";
  var panel = null;

  function fmtBytes(n) {
    if (n == null) return "n/a";
    var u = ["B", "KB", "MB", "GB", "TB"], i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n : n.toFixed(1)) + " " + u[i];
  }

  function loadCounts() {
    Syn.fetchJSON("/api/maintenance/counts").then(function (d) {
      setText("mc-pending", d.pending_queue);
      setText("mc-faces", d.faces);
      setText("mc-embeddings", d.embeddings);
      setText("mc-runs", d.cluster_runs);
      setText("mc-photos", d.photos);
      var crops = d.crops || {};
      var files = crops.files;
      setTextRaw("mc-crops", files == null ? "n/a" : files + " files · " + fmtBytes(crops.bytes));
    }).catch(function () { /* transient */ });
  }

  function setText(id, v) { var el = document.getElementById(id); if (el) el.textContent = (v == null ? "—" : v); }
  function setTextRaw(id, s) { var el = document.getElementById(id); if (el) el.textContent = s; }

  function startJob(name, params, extra) {
    panel.start(name, params || {}, extra || {})
      .then(function () { setTimeout(loadCounts, 500); })
      .catch(function (e) {
        if (e.status === 428) Syn.toast("Consent required.", "error");
        else Syn.toast(e.message || "Failed to start job", "error");
      });
  }

  function dedupeParams() {
    var params = {
      exact: document.getElementById("dd-exact").checked,
      visual: document.getElementById("dd-visual").checked,
    };
    var t = document.getElementById("dd-threshold").value.trim();
    if (t !== "") params.threshold = t;
    return params;
  }

  function dedupePreview() {
    var params = dedupeParams();
    if (!params.exact && !params.visual) { Syn.toast("Pick exact and/or visual.", "error"); return; }
    startJob("dedupe", params);
  }

  function dedupeApply() {
    var params = dedupeParams();
    if (!params.exact && !params.visual) { Syn.toast("Pick exact and/or visual.", "error"); return; }
    Syn.confirm({
      title: "Delete duplicate photos",
      message: "This permanently deletes duplicate photos from the NAS.",
      phrase: PHRASE_DEDUPE,
      okLabel: "Delete duplicates",
    }).then(function (ok) {
      if (!ok) return;
      params.apply = true;
      startJob("dedupe", params, { confirm: true, confirm_phrase: PHRASE_DEDUPE });
    });
  }

  function clearQueue() {
    Syn.confirm({
      title: "Clear review queue",
      message: "Remove all pending review items? Approved and applied decisions are kept.",
      okLabel: "Clear queue",
    }).then(function (ok) {
      if (ok) startJob("clear-queue", {}, { confirm: true });
    });
  }

  function deleteCrops() {
    Syn.confirm({
      title: "Delete crop images",
      message: "Wipe all cached face crops? They can be rebuilt with regen-crops.",
      okLabel: "Delete crops",
    }).then(function (ok) {
      if (ok) startJob("delete-crops", {}, { confirm: true });
    });
  }

  function reset() {
    var all = document.getElementById("rs-all").checked;
    var keepCrops = document.getElementById("rs-keep-crops").checked;
    var params = { keep_crops: keepCrops };
    if (all) {
      params.all = true;
      Syn.confirm({
        title: "Reset EVERYTHING",
        message: "This drops all local pipeline data including synced photos. The NAS is untouched, but you will need to re-sync.",
        phrase: PHRASE_RESET_ALL,
        okLabel: "Reset all",
      }).then(function (ok) {
        if (ok) startJob("reset", params, { confirm_phrase: PHRASE_RESET_ALL });
      });
    } else {
      Syn.confirm({
        title: "Reset local database",
        message: "Drop faces, embeddings, clusters and the review queue from the local DB?",
        okLabel: "Reset",
      }).then(function (ok) {
        if (ok) startJob("reset", params, { confirm: true });
      });
    }
  }

  window.addEventListener("DOMContentLoaded", function () {
    panel = new JobPanel(document.getElementById("job-panel"));
    document.getElementById("dd-preview").addEventListener("click", dedupePreview);
    document.getElementById("dd-apply").addEventListener("click", dedupeApply);
    document.getElementById("cq-run").addEventListener("click", clearQueue);
    document.getElementById("crops-run").addEventListener("click", deleteCrops);
    document.getElementById("rs-run").addEventListener("click", reset);
    document.getElementById("job-panel").addEventListener("synopticon:job-done", loadCounts);
    loadCounts();
  });
})();
