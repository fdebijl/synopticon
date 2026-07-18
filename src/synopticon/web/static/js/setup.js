/* Setup wizard controller. Drives the multi-step card in setup.html.j2:
   0 account · 1 NAS + test-connection · 2 storage · 3 models · 4 sync · 5 done.
   Resumable from /api/setup/status; each step advances only once its own gate
   is satisfied. Uses window.Syn (fetch helpers) and window.JobPanel. */
(function () {
  "use strict";

  var LAST_STEP = 5;
  var current = 0;
  var status = null;
  var modelsPanel = null;
  var syncPanel = null;

  function $(id) { return document.getElementById(id); }
  function steps() { return Array.prototype.slice.call(document.querySelectorAll(".wizard-step")); }

  function renderDots() {
    document.querySelectorAll("#wizard-steps li").forEach(function (li) {
      var n = parseInt(li.getAttribute("data-step"), 10);
      var state = n < current ? "done" : (n === current ? "current" : "");
      if (state) li.setAttribute("data-state", state);
      else li.removeAttribute("data-state");
    });
  }

  function goto(step) {
    current = Math.max(0, Math.min(LAST_STEP, step));
    steps().forEach(function (el) {
      el.classList.toggle("active", parseInt(el.getAttribute("data-step"), 10) === current);
    });
    renderDots();
    // Focus the first control of the newly shown step for keyboard users.
    var active = document.querySelector(".wizard-step.active");
    var focusable = active && active.querySelector("input, button, a");
    if (focusable) focusable.focus();
  }

  /* Earliest step not yet satisfied, given /api/setup/status. */
  function resumeStep(s) {
    if (!s.account_created) return 0;
    if (!s.nas_configured) return 1;
    if (!s.models_ready) return 3;
    if (!s.photos_synced) return 4;
    return LAST_STEP;
  }

  function prefill(s) {
    var nas = s.nas || {};
    if (nas.url) $("nas-url").value = nas.url;
    if (nas.account) $("nas-account").value = nas.account;
    $("nas-verify-tls").checked = nas.verify_tls !== false;
    var spaces = nas.spaces || ["personal"];
    $("nas-space-personal").checked = spaces.indexOf("personal") !== -1;
    $("nas-space-shared").checked = spaces.indexOf("shared") !== -1;
    var st = s.storage || {};
    if (st.data_dir) $("st-data-dir").value = st.data_dir;
    if (st.models_dir) $("st-models-dir").value = st.models_dir;
    $("st-keep-originals").checked = !!st.keep_originals;
    if (st.originals_cache_gb) $("st-cache-gb").value = st.originals_cache_gb;
  }

  // -- Step 0: create account ------------------------------------------------
  function wireAccount() {
    $("create-account-form").addEventListener("submit", function (e) {
      e.preventDefault();
      var err = $("account-error");
      err.hidden = true;
      var body = {
        username: $("su-username").value.trim(),
        password: $("su-password").value,
      };
      Syn.postJSON("/api/auth/create-account", body).then(function () {
        goto(1);
      }).catch(function (ex) {
        err.textContent = ex.message || "Could not create account.";
        err.hidden = false;
      });
    });
  }

  // -- Step 1: NAS + test connection ----------------------------------------
  function nasBody() {
    var spaces = [];
    if ($("nas-space-personal").checked) spaces.push("personal");
    if ($("nas-space-shared").checked) spaces.push("shared");
    return {
      url: $("nas-url").value.trim(),
      account: $("nas-account").value.trim(),
      password: $("nas-password").value,
      otp_code: $("nas-otp").value.trim(),
      verify_tls: $("nas-verify-tls").checked,
      spaces: spaces,
    };
  }

  function renderProbe(result) {
    var ul = $("probe-steps");
    ul.hidden = false;
    ul.textContent = "";
    (result.steps || []).forEach(function (step) {
      var li = document.createElement("li");
      li.className = step.ok ? "probe-ok" : "probe-fail";
      var mark = document.createElement("span");
      mark.className = "probe-mark";
      mark.textContent = step.ok ? "✓" : "✗";
      var name = document.createElement("span");
      name.className = "probe-name";
      name.textContent = step.name;
      var detail = document.createElement("span");
      detail.className = "probe-detail";
      detail.textContent = step.detail;
      li.appendChild(mark); li.appendChild(name); li.appendChild(detail);
      ul.appendChild(li);
    });
  }

  function wireNas() {
    $("btn-test-connection").addEventListener("click", function () {
      var btn = this;
      btn.disabled = true;
      var prev = btn.textContent;
      btn.textContent = "Testing…";
      Syn.postJSON("/api/setup/test-connection", nasBody()).then(function (result) {
        renderProbe(result);
        $("btn-save-nas").disabled = !result.ok;
        if (result.ok) Syn.toast("Connection OK", "ok");
        else Syn.toast(result.error || "Connection failed", "error");
      }).catch(function (ex) {
        Syn.toast(ex.message || "Test failed", "error");
      }).finally(function () {
        btn.disabled = false;
        btn.textContent = prev;
      });
    });

    $("btn-save-nas").addEventListener("click", function () {
      var body = nasBody();
      // Drop empty otp so it is not persisted; keep password only if entered.
      var nas = {
        url: body.url, account: body.account,
        verify_tls: body.verify_tls, spaces: body.spaces,
      };
      if (body.password) nas.password = body.password;
      if (body.otp_code) nas.otp_code = body.otp_code;
      Syn.fetchJSON("/api/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ nas: nas }),
      }).then(function () {
        goto(2);
      }).catch(function (ex) {
        // PUT /api/config is provided by a separate module; if it is missing,
        // don't trap the user — advance and let them configure via Settings.
        if (ex.status === 404 || ex.status === 405) { goto(2); return; }
        Syn.toast(ex.message || "Could not save NAS config", "error");
      });
    });
  }

  // -- Step 2: storage -------------------------------------------------------
  function storageBody() {
    return { data_dir: $("st-data-dir").value.trim(), models_dir: $("st-models-dir").value.trim() };
  }

  function wireStorage() {
    $("btn-check-storage").addEventListener("click", function () {
      Syn.postJSON("/api/setup/check-storage", storageBody()).then(function (res) {
        var box = $("storage-result");
        box.hidden = false;
        box.textContent = "";
        Object.keys(res.dirs || {}).forEach(function (key) {
          var d = res.dirs[key];
          var line = document.createElement("div");
          line.className = d.ok ? "ok" : "bad";
          var free = d.free_gb != null ? " (" + d.free_gb + " GB free)" : "";
          line.textContent = (d.ok ? "✓ " : "✗ ") + key + ": " + d.detail + free;
          box.appendChild(line);
        });
      }).catch(function (ex) { Syn.toast(ex.message || "Check failed", "error"); });
    });

    $("btn-save-storage").addEventListener("click", function () {
      var storage = {
        data_dir: $("st-data-dir").value.trim(),
        models_dir: $("st-models-dir").value.trim(),
        keep_originals: $("st-keep-originals").checked,
        originals_cache_gb: parseFloat($("st-cache-gb").value) || 50,
      };
      Syn.fetchJSON("/api/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ storage: storage }),
      }).then(function () {
        goto(3);
      }).catch(function (ex) {
        if (ex.status === 404 || ex.status === 405) { goto(3); return; }
        Syn.toast(ex.message || "Could not save storage config", "error");
      });
    });
  }

  // -- Step 3: models --------------------------------------------------------
  function showMissingModels(missing) {
    var box = $("models-missing");
    var list = $("models-missing-list");
    list.textContent = "";
    (missing || []).forEach(function (key) {
      var li = document.createElement("li");
      var code = document.createElement("code");
      code.textContent = key;
      li.appendChild(code);
      list.appendChild(li);
    });
    box.hidden = false;
    // A partial/failed download must never dead-end: offer a way forward.
    $("btn-models-continue-anyway").hidden = false;
  }

  // Reflect the authoritative on-disk status into the models step's controls.
  function applyModelsStatus(s) {
    if (s && s.models_ready) {
      $("btn-models-next").disabled = false;
      $("models-missing").hidden = true;
      $("btn-models-continue-anyway").hidden = true;
    } else if (s && s.models_missing && s.models_missing.length) {
      showMissingModels(s.models_missing);
    }
  }

  function wireModels() {
    var el = $("models-job-panel");
    modelsPanel = new JobPanel(el);
    el.addEventListener("synopticon:job-done", function (e) {
      // Any terminal state: re-fetch the real on-disk status (the download job
      // exits non-zero when AdaFace/MagFace aren't exportable, but the other
      // models may still be present). Disk presence, not job exit code, decides.
      Syn.fetchJSON("/api/setup/status").then(function (s) {
        status = s;
        if (s.models_ready) {
          $("btn-models-next").disabled = false;
          $("models-missing").hidden = true;
          $("btn-models-continue-anyway").hidden = true;
          Syn.toast("Models ready", "ok");
        } else {
          showMissingModels(s.models_missing);
        }
      }).catch(function () {
        // Status unreachable — fall back to the job state so we never trap.
        if (e.detail && e.detail.state === "succeeded") {
          $("btn-models-next").disabled = false;
        } else {
          $("btn-models-continue-anyway").hidden = false;
        }
      });
      // Re-enable the Download button so the user can retry after a failed run.
      $("btn-download-models").disabled = false;
    });
    $("btn-download-models").addEventListener("click", function () {
      this.disabled = true;
      modelsPanel.start("models-download", {}).catch(function (ex) {
        Syn.toast(ex.message || "Could not start download", "error");
        $("btn-download-models").disabled = false;
      });
    });
    $("btn-models-next").addEventListener("click", function () { goto(4); });
    $("btn-models-continue-anyway").addEventListener("click", function () { goto(4); });
  }

  // -- Step 4: first sync (skippable) ---------------------------------------
  function wireSync() {
    var el = $("sync-job-panel");
    syncPanel = new JobPanel(el);
    el.addEventListener("synopticon:job-done", function (e) {
      if (e.detail && e.detail.state === "succeeded") {
        $("btn-sync-next").disabled = false;
        Syn.toast("Sync complete", "ok");
      }
    });
    $("btn-run-sync").addEventListener("click", function () {
      this.disabled = true;
      syncPanel.start("sync", {}).catch(function (ex) {
        Syn.toast(ex.message || "Could not start sync", "error");
        $("btn-run-sync").disabled = false;
      });
    });
    $("btn-skip-sync").addEventListener("click", function () { goto(LAST_STEP); });
    $("btn-sync-next").addEventListener("click", function () { goto(LAST_STEP); });
  }

  function wireBack() {
    document.querySelectorAll("[data-back]").forEach(function (btn) {
      btn.addEventListener("click", function () { goto(current - 1); });
    });
  }

  function init() {
    wireAccount();
    wireNas();
    wireStorage();
    wireModels();
    wireSync();
    wireBack();
    Syn.fetchJSON("/api/setup/status").then(function (s) {
      status = s;
      prefill(s);
      applyModelsStatus(s);
      goto(resumeStep(s));
    }).catch(function () {
      // No status (e.g. not yet reachable) — start at the top.
      goto(0);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
