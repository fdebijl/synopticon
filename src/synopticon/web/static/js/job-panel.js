/* JobPanel(el): live job progress via SSE, falling back to polling.
   .start(name, params, extra) submits a job then attaches; .attach(jobId)
   follows an existing one. Renders phase chips, a progress bar (indeterminate
   when total is null), a 500-line log ring with pause-on-scroll-up, and a
   cancel button. Fires a `synopticon:job-done` CustomEvent on the panel element
   when the job reaches a terminal state. Single render path for SSE + polling. */
(function () {
  "use strict";

  var LOG_MAX = 500;
  var TERMINAL = { succeeded: 1, failed: 1, cancelled: 1, interrupted: 1 };

  function JobPanel(el) {
    this.el = el;
    this.jobId = null;
    this.seq = 0;
    this.sseErrors = 0;
    this.es = null;
    this.pollTimer = null;
    this.done = false;
    this.phases = {};
    this.logPaused = false;

    this.titleEl = el.querySelector("[data-job-title]");
    this.stateEl = el.querySelector("[data-job-state]");
    this.phasesEl = el.querySelector("[data-job-phases]");
    this.progressEl = el.querySelector("[data-job-progress]");
    this.barEl = el.querySelector("[data-job-progress-bar]");
    this.logEl = el.querySelector("[data-job-log]");
    this.cancelBtn = el.querySelector("[data-job-cancel]");

    var self = this;
    if (this.cancelBtn) {
      this.cancelBtn.addEventListener("click", function () { self.cancel(); });
    }
    if (this.logEl) {
      this.logEl.addEventListener("scroll", function () {
        var atBottom = self.logEl.scrollHeight - self.logEl.scrollTop - self.logEl.clientHeight < 24;
        self.logPaused = !atBottom;
      });
    }
  }

  JobPanel.prototype.start = function (name, params, extra) {
    var body = Object.assign({ name: name, params: params || {} }, extra || {});
    var self = this;
    return Syn.postJSON("/api/jobs", body).then(function (res) {
      self.attach(res.job_id);
      return res.job_id;
    });
  };

  JobPanel.prototype.attach = function (jobId) {
    this.reset();
    this.jobId = jobId;
    if (this.titleEl) this.titleEl.textContent = Syn.fmtJobId(jobId);
    if (this.cancelBtn) this.cancelBtn.hidden = false;
    this.connectSSE();
  };

  JobPanel.prototype.reset = function () {
    this.seq = 0; this.sseErrors = 0; this.done = false; this.phases = {};
    if (this.es) { this.es.close(); this.es = null; }
    if (this.pollTimer) { clearTimeout(this.pollTimer); this.pollTimer = null; }
    if (this.logEl) this.logEl.textContent = "";
    if (this.phasesEl) this.phasesEl.textContent = "";
    if (this.progressEl) this.progressEl.hidden = true;
  };

  JobPanel.prototype.connectSSE = function () {
    if (this.done) return;
    var self = this;
    try {
      this.es = new EventSource("/api/jobs/" + this.jobId + "/stream?after=" + this.seq);
    } catch (e) { this.startPolling(); return; }
    this.es.onmessage = function (ev) {
      try { self.ingest(JSON.parse(ev.data)); } catch (e) { /* ping */ }
    };
    this.es.onerror = function () {
      if (self.es) { self.es.close(); self.es = null; }
      if (self.done) return;
      self.sseErrors += 1;
      if (self.sseErrors >= 2) self.startPolling();
      else setTimeout(function () { self.connectSSE(); }, 500);
    };
  };

  JobPanel.prototype.startPolling = function () {
    if (this.done) return;
    var self = this;
    function tick() {
      if (self.done) return;
      Syn.fetchJSON("/api/jobs/" + self.jobId + "/events?after=" + self.seq)
        .then(function (res) {
          (res.events || []).forEach(function (e) { self.ingest(e); });
          if (!self.done && TERMINAL[res.state]) self.finish(res.state);
          if (!self.done) self.pollTimer = setTimeout(tick, 1500);
        })
        .catch(function () { if (!self.done) self.pollTimer = setTimeout(tick, 1500); });
    }
    tick();
  };

  JobPanel.prototype.ingest = function (evt) {
    if (typeof evt.seq === "number" && evt.seq > this.seq) this.seq = evt.seq;
    switch (evt.event) {
      case "phase": this.setPhase(evt.phase, "active"); break;
      case "progress": this.setPhase(evt.phase, "active"); this.setProgress(evt.done, evt.total); break;
      case "log": this.appendLog(evt.level, evt.message); break;
      case "result":
        this.appendLog("info", "result: " + JSON.stringify(evt.stats || {}));
        this.el.dispatchEvent(new CustomEvent("synopticon:job-done", { detail: evt, bubbles: true }));
        break;
      case "error": this.appendLog("error", evt.message || "error"); break;
      case "final": this.finish(evt.state); break;
    }
  };

  JobPanel.prototype.setPhase = function (phase, cls) {
    if (!phase || !this.phasesEl) return;
    if (!this.phases[phase]) {
      var chip = document.createElement("span");
      chip.className = "phase-chip";
      chip.textContent = phase;
      this.phasesEl.appendChild(chip);
      this.phases[phase] = chip;
    }
    // mark previously active phases done
    Object.keys(this.phases).forEach(function (p) {
      var c = this.phases[p];
      if (p !== phase && c.classList.contains("active")) { c.classList.remove("active"); c.classList.add("done"); }
    }, this);
    this.phases[phase].classList.add(cls);
  };

  JobPanel.prototype.setProgress = function (done, total) {
    if (!this.progressEl) return;
    this.progressEl.hidden = false;
    if (total == null) {
      this.progressEl.classList.add("indeterminate");
      this.progressEl.removeAttribute("aria-valuenow");
    } else {
      this.progressEl.classList.remove("indeterminate");
      var pct = total ? Math.round((done / total) * 100) : 0;
      this.barEl.style.width = pct + "%";
      this.progressEl.setAttribute("aria-valuenow", String(pct));
    }
  };

  JobPanel.prototype.appendLog = function (level, message) {
    if (!this.logEl) return;
    var line = document.createElement("div");
    line.className = "log-" + (level || "info");
    line.textContent = message;
    this.logEl.appendChild(line);
    while (this.logEl.childNodes.length > LOG_MAX) this.logEl.removeChild(this.logEl.firstChild);
    if (!this.logPaused) this.logEl.scrollTop = this.logEl.scrollHeight;
  };

  JobPanel.prototype.finish = function (state) {
    if (this.done) return;
    this.done = true;
    if (this.es) { this.es.close(); this.es = null; }
    if (this.pollTimer) { clearTimeout(this.pollTimer); this.pollTimer = null; }
    if (this.cancelBtn) this.cancelBtn.hidden = true;
    if (this.progressEl) this.progressEl.classList.remove("indeterminate");
    Object.keys(this.phases).forEach(function (p) {
      this.phases[p].classList.remove("active");
      if (state === "succeeded") this.phases[p].classList.add("done");
    }, this);
    if (this.stateEl) {
      this.stateEl.hidden = false;
      this.stateEl.textContent = state;
      this.stateEl.className = "job-state badge state-" + state;
    }
    this.el.dispatchEvent(new CustomEvent("synopticon:job-done", { detail: { state: state }, bubbles: true }));
  };

  JobPanel.prototype.cancel = function () {
    if (!this.jobId || this.done) return;
    Syn.postJSON("/api/jobs/" + this.jobId + "/cancel", {})
      .then(function () { Syn.toast("Cancelling…"); })
      .catch(function (e) { Syn.toast(e.message, "error"); });
  };

  window.JobPanel = JobPanel;
})();
