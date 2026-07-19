/* Settings page: section tabs over per-section config forms generated from
   /api/config (values + JSON schema), plus the Access tab (change password,
   API keys). Field-level 422 errors map back by dotted `loc`; keys shadowed by
   a SYNOPTICON_* env/.env var get a warn chip; the password field is write-only
   ("set — leave blank to keep") and sent only when non-empty. A dirty flag drives
   the save bar and a beforeunload guard. Server is the single validator. */
(function () {
  "use strict";

  var SECTIONS = [
    "nas", "storage", "inference", "detection",
    "restoration", "clustering", "crossref",
  ];
  var LABELS = {
    nas: "NAS", storage: "Storage", inference: "Inference", detection: "Detection",
    restoration: "Restoration", clustering: "Clustering", crossref: "Crossref",
    access: "Access",
  };

  var tabsEl = document.getElementById("settings-tabs");
  var bodyEl = document.getElementById("settings-body");
  var loadingEl = document.getElementById("settings-loading");
  var saveBar = document.getElementById("save-bar");
  var saveBtn = document.getElementById("save-btn");
  var resetBtn = document.getElementById("reset-btn");
  var saveStatus = document.getElementById("save-status");

  var fields = [];      // {section, key, el, kind, itemType, nullable, initial, isSecret, rowEl}
  var envOverrides = {}; // "section.key" -> true
  var dirty = false;

  function esc(s) {
    var d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }

  /* Dereference a schema node ($ref / single-item allOf) against $defs. */
  function deref(node, root) {
    if (!node) return {};
    if (node.$ref) {
      var name = node.$ref.split("/").pop();
      return deref((root.$defs || {})[name] || {}, root);
    }
    if (node.allOf && node.allOf.length === 1) return deref(node.allOf[0], root);
    return node;
  }

  /* Pick a control kind from a resolved property schema. */
  function classify(p) {
    if (p.format === "password") return { kind: "password" };
    if (Array.isArray(p.enum)) return { kind: "enum", options: p.enum };
    if (p.anyOf) {
      var real = p.anyOf.filter(function (a) { return a.type !== "null"; });
      var nullable = p.anyOf.some(function (a) { return a.type === "null"; });
      var inner = real[0] || {};
      var c = classify(inner);
      c.nullable = nullable;
      return c;
    }
    switch (p.type) {
      case "boolean": return { kind: "boolean" };
      case "integer": return { kind: "integer" };
      case "number": return { kind: "number" };
      case "array": return { kind: "array", itemType: (p.items && p.items.type) || "string",
                             itemEnum: p.items && p.items.enum };
      case "object": return { kind: "object" };
      default: return { kind: "string" };
    }
  }

  function humanize(key) {
    return key.replace(/_/g, " ").replace(/\b\w/g, function (m) { return m.toUpperCase(); });
  }

  /* --- rendering config sections ---------------------------------------- */
  function renderField(section, key, propSchema, value) {
    var info = classify(propSchema);
    var row = document.createElement("div");
    row.className = "field-row";
    var dotted = section + "." + key;

    var label = document.createElement("div");
    label.className = "field-label";
    label.innerHTML =
      '<span class="field-name">' + esc(humanize(key)) + "</span>" +
      '<span class="field-key">' + esc(dotted) + "</span>";

    var control = document.createElement("div");
    control.className = "field-control";

    var el, isSecret = false, initial;
    if (info.kind === "password") {
      el = document.createElement("input");
      el.type = "password";
      el.className = "input";
      el.placeholder = value && value.set ? "set — leave blank to keep" : "not set";
      el.autocomplete = "new-password";
      isSecret = true;
      initial = "";
    } else if (info.kind === "enum") {
      el = document.createElement("select");
      el.className = "select";
      info.options.forEach(function (opt) {
        var o = document.createElement("option");
        o.value = opt; o.textContent = opt;
        if (opt === value) o.selected = true;
        el.appendChild(o);
      });
      initial = value;
    } else if (info.kind === "boolean") {
      el = document.createElement("input");
      el.type = "checkbox";
      el.className = "check";
      el.checked = !!value;
      initial = !!value;
    } else if (info.kind === "array") {
      el = document.createElement("input");
      el.type = "text";
      el.className = "input";
      el.value = (value || []).join(", ");
      el.placeholder = "comma-separated";
      initial = (value || []).join(", ");
    } else if (info.kind === "object") {
      el = document.createElement("textarea");
      el.value = JSON.stringify(value == null ? {} : value, null, 2);
      initial = el.value;
    } else if (info.kind === "integer" || info.kind === "number") {
      el = document.createElement("input");
      el.type = "number";
      el.className = "input";
      if (info.kind === "number") el.step = "any";
      el.value = value == null ? "" : value;
      initial = el.value;
    } else {
      el = document.createElement("input");
      el.type = "text";
      el.className = "input";
      el.value = value == null ? "" : value;
      initial = el.value;
    }
    el.id = "f-" + dotted.replace(/\./g, "-");
    el.addEventListener("input", markDirty);
    el.addEventListener("change", markDirty);

    control.appendChild(el);

    if (propSchema.description) {
      var desc = document.createElement("div");
      desc.className = "field-desc";
      desc.textContent = propSchema.description;
      control.appendChild(desc);
    }

    if (envOverrides[dotted]) {
      var chip = document.createElement("span");
      chip.className = "env-chip";
      var varName = "SYNOPTICON_" + section.toUpperCase() + "__" + key.toUpperCase();
      chip.textContent = "overridden by " + varName + " — saved value has no effect";
      control.appendChild(chip);
    }
    
    var errSlot = document.createElement("div");
    errSlot.className = "field-error";
    control.appendChild(errSlot);

    row.appendChild(label);
    row.appendChild(control);

    fields.push({
      section: section, key: key, dotted: dotted, el: el, info: info,
      isSecret: isSecret, initial: initial, rowEl: row, errSlot: errSlot,
    });
    return row;
  }

  function renderConfigPanel(section, defs, values, root) {
    var panel = document.createElement("div");
    panel.className = "settings-panel";
    panel.dataset.tab = section;
    var card = document.createElement("div");
    card.className = "card";
    var sectionSchema = deref(root.properties[section], root);
    var props = sectionSchema.properties || {};
    var vals = values[section] || {};
    Object.keys(props).forEach(function (key) {
      card.appendChild(renderField(section, key, deref(props[key], root), vals[key]));
    });
    panel.appendChild(card);
    return panel;
  }

  /* --- reading control values back -------------------------------------- */
  function currentValue(f) {
    var el = f.el, info = f.info;
    if (info.kind === "password") return el.value;
    if (info.kind === "boolean") return el.checked;
    if (info.kind === "enum" || info.kind === "string" || info.kind === "password") return el.value;
    if (info.kind === "array") {
      var parts = el.value.split(",").map(function (s) { return s.trim(); }).filter(Boolean);
      if (info.itemType === "number" || info.itemType === "integer") return parts.map(Number);
      return parts;
    }
    if (info.kind === "object") return JSON.parse(el.value || "{}");
    if (info.kind === "integer" || info.kind === "number") {
      if (el.value.trim() === "") return info.nullable ? null : "";
      return info.kind === "integer" ? parseInt(el.value, 10) : Number(el.value);
    }
    return el.value;
  }

  function isChanged(f) {
    if (f.isSecret) return f.el.value.length > 0; // only send when a new secret is typed
    if (f.info.kind === "boolean") return f.el.checked !== f.initial;
    if (f.info.kind === "array" || f.info.kind === "object") return f.el.value !== f.initial;
    return String(f.el.value) !== String(f.initial);
  }

  /* --- dirty state ------------------------------------------------------- */
  function markDirty() {
    dirty = true;
    saveStatus.textContent = "Unsaved changes";
    saveStatus.className = "save-status dirty";
  }
  function clearDirty() { dirty = false; }

  window.addEventListener("beforeunload", function (e) {
    if (dirty) { e.preventDefault(); e.returnValue = ""; return ""; }
  });

  /* --- tabs -------------------------------------------------------------- */
  function activate(tab) {
    document.querySelectorAll(".settings-tab").forEach(function (b) {
      b.classList.toggle("active", b.dataset.tab === tab);
      b.setAttribute("aria-selected", b.dataset.tab === tab ? "true" : "false");
    });
    document.querySelectorAll(".settings-panel").forEach(function (p) {
      p.classList.toggle("active", p.dataset.tab === tab);
    });
  }

  function buildTabs(order) {
    order.forEach(function (tab, i) {
      var b = document.createElement("button");
      b.className = "settings-tab" + (i === 0 ? " active" : "");
      b.dataset.tab = tab;
      b.type = "button";
      b.setAttribute("role", "tab");
      b.setAttribute("aria-selected", i === 0 ? "true" : "false");
      b.innerHTML = esc(LABELS[tab] || tab) + '<span class="err-dot" aria-hidden="true"></span>';
      b.addEventListener("click", function () { activate(tab); });
      tabsEl.appendChild(b);
    });
  }

  /* --- error mapping ----------------------------------------------------- */
  function clearErrors() {
    fields.forEach(function (f) {
      f.rowEl.classList.remove("has-error");
      f.errSlot.textContent = "";
    });
    document.querySelectorAll(".settings-tab").forEach(function (b) {
      b.classList.remove("has-error");
    });
  }

  function showErrors(errors) {
    var badSections = {};
    errors.forEach(function (err) {
      var loc = err.loc || "";
      var f = fields.find(function (x) {
        return x.dotted === loc || loc.indexOf(x.dotted + ".") === 0;
      });
      if (f) {
        f.rowEl.classList.add("has-error");
        f.errSlot.textContent = err.msg;
        badSections[f.section] = true;
      } else {
        Syn.toast(loc + ": " + err.msg, "error");
      }
    });
    document.querySelectorAll(".settings-tab").forEach(function (b) {
      if (badSections[b.dataset.tab]) b.classList.add("has-error");
    });
    var first = Object.keys(badSections)[0];
    if (first) activate(first);
  }

  /* --- save -------------------------------------------------------------- */
  function buildPartial() {
    var partial = {};
    fields.forEach(function (f) {
      if (!isChanged(f)) return;
      (partial[f.section] = partial[f.section] || {})[f.key] = currentValue(f);
    });
    return partial;
  }

  async function save() {
    clearErrors();
    var partial;
    try {
      partial = buildPartial();
    } catch (e) {
      saveStatus.textContent = "Invalid JSON in one of the fields";
      saveStatus.className = "save-status err";
      return;
    }
    if (Object.keys(partial).length === 0) {
      saveStatus.textContent = "Nothing to save";
      saveStatus.className = "save-status";
      return;
    }
    saveBtn.disabled = true;
    try {
      await Syn.fetchJSON("/api/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(partial),
      });
      clearDirty();
      saveStatus.textContent = "Saved. Some changes take effect on restart.";
      saveStatus.className = "save-status ok";
      Syn.toast("Configuration saved", "ok");
      reload();
    } catch (err) {
      if (err.status === 422 && err.data && err.data.errors) {
        showErrors(err.data.errors);
        saveStatus.textContent = "Please fix the highlighted fields";
        saveStatus.className = "save-status err";
      } else if (err.status === 409) {
        saveStatus.textContent = "A job is running — try again once it finishes";
        saveStatus.className = "save-status err";
      } else {
        saveStatus.textContent = err.message || "Save failed";
        saveStatus.className = "save-status err";
      }
    } finally {
      saveBtn.disabled = false;
    }
  }

  /* --- access tab: password + API keys ---------------------------------- */
  function renderAccessPanel() {
    var panel = document.createElement("div");
    panel.className = "settings-panel";
    panel.dataset.tab = "access";
    panel.innerHTML =
      '<div class="card access-section">' +
      '  <h3>Change password</h3>' +
      '  <form class="access-form" id="pw-form">' +
      '    <label>Current password<input class="input" type="password" id="pw-current" autocomplete="current-password" required></label>' +
      '    <label>New password<input class="input" type="password" id="pw-new" autocomplete="new-password" required></label>' +
      '    <label>Confirm new password<input class="input" type="password" id="pw-confirm" autocomplete="new-password" required></label>' +
      '    <button class="btn btn-primary" type="submit">Update password</button>' +
      '  </form>' +
      '</div>' +
      '<div class="card access-section">' +
      '  <h3>API keys</h3>' +
      '  <p class="muted">Full-access keys for automation (e.g. a sidecar extension). The secret is shown once.</p>' +
      '  <form class="access-form" id="key-form" style="flex-direction:row;align-items:flex-end;gap:var(--sp-2)">' +
      '    <label style="flex:1">Key name<input class="input" type="text" id="key-name" placeholder="my-laptop" required></label>' +
      '    <button class="btn btn-action" type="submit">Create key</button>' +
      '  </form>' +
      '  <div id="key-reveal-wrap"></div>' +
      '  <table class="data key-table" id="key-table" style="margin-top:var(--sp-3)"><thead>' +
      '    <tr><th>Name</th><th>Prefix</th><th>Created</th><th>Last used</th><th></th></tr>' +
      '  </thead><tbody id="key-rows"></tbody></table>' +
      '</div>';
    return panel;
  }

  function fmtTime(t) { return t ? new Date(t * 1000).toLocaleString() : "—"; }

  async function loadKeys() {
    var rows = document.getElementById("key-rows");
    if (!rows) return;
    var data;
    try { data = await Syn.fetchJSON("/api/auth/keys"); } catch (e) { return; }
    rows.innerHTML = "";
    (data.keys || []).forEach(function (k) {
      var tr = document.createElement("tr");
      if (k.revoked) tr.className = "revoked";
      tr.innerHTML =
        "<td>" + esc(k.name) + "</td>" +
        "<td class=mono>" + esc(k.key_prefix) + "…</td>" +
        "<td>" + esc(fmtTime(k.created_at)) + "</td>" +
        "<td>" + esc(fmtTime(k.last_used_at)) + "</td>" +
        "<td></td>";
      if (!k.revoked) {
        var btn = document.createElement("button");
        btn.className = "btn btn-sm btn-danger";
        btn.textContent = "Revoke";
        btn.addEventListener("click", function () { revokeKey(k.id, k.name); });
        tr.lastChild.appendChild(btn);
      }
      rows.appendChild(tr);
    });
  }

  async function revokeKey(id, name) {
    var ok = await Syn.confirm({
      title: "Revoke API key",
      message: 'Revoke "' + name + '"? Any client using it will stop working.',
      okLabel: "Revoke",
    });
    if (!ok) return;
    try {
      await Syn.postJSON("/api/auth/keys/" + id + "/revoke", {});
      Syn.toast("Key revoked", "ok");
      loadKeys();
    } catch (e) { Syn.toast(e.message || "Revoke failed", "error"); }
  }

  function wireAccess() {
    var pwForm = document.getElementById("pw-form");
    if (pwForm) {
      pwForm.addEventListener("submit", async function (e) {
        e.preventDefault();
        var cur = document.getElementById("pw-current").value;
        var nw = document.getElementById("pw-new").value;
        var cf = document.getElementById("pw-confirm").value;
        if (nw !== cf) { Syn.toast("New passwords do not match", "error"); return; }
        try {
          await Syn.postJSON("/api/auth/change-password", {
            current_password: cur, new_password: nw,
          });
          pwForm.reset();
          Syn.toast("Password updated", "ok");
        } catch (err) { Syn.toast(err.message || "Could not change password", "error"); }
      });
    }
    var keyForm = document.getElementById("key-form");
    if (keyForm) {
      keyForm.addEventListener("submit", async function (e) {
        e.preventDefault();
        var name = document.getElementById("key-name").value.trim();
        if (!name) return;
        try {
          var res = await Syn.postJSON("/api/auth/keys", { name: name });
          keyForm.reset();
          var wrap = document.getElementById("key-reveal-wrap");
          wrap.innerHTML =
            '<div class="key-reveal">New key (copy it now — it will not be shown again):<br>' +
            esc(res.key) + "</div>";
          loadKeys();
        } catch (err) { Syn.toast(err.message || "Could not create key", "error"); }
      });
    }
    loadKeys();
  }

  /* --- boot -------------------------------------------------------------- */
  function render(cfg) {
    fields = [];
    envOverrides = {};
    (cfg.env_overrides || []).forEach(function (k) { envOverrides[k] = true; });

    bodyEl.innerHTML = "";
    var root = cfg.schema || {};
    var values = cfg.values || {};
    SECTIONS.forEach(function (section) {
      bodyEl.appendChild(renderConfigPanel(section, root.$defs || {}, values, root));
    });
    bodyEl.appendChild(renderAccessPanel());
    activate(SECTIONS[0]);
    saveBar.classList.remove("hidden");
    saveStatus.textContent = "";
    saveStatus.className = "save-status";
    wireAccess();
  }

  var _built = false;
  async function reload() {
    var cfg;
    try {
      cfg = await Syn.fetchJSON("/api/config");
    } catch (e) {
      if (loadingEl) loadingEl.textContent = "Could not load configuration: " + (e.message || "error");
      return;
    }
    if (loadingEl) loadingEl.remove();
    if (!_built) { buildTabs(SECTIONS.concat(["access"])); _built = true; }
    render(cfg);
  }

  saveBtn.addEventListener("click", save);
  resetBtn.addEventListener("click", function () { clearDirty(); reload(); });

  window.addEventListener("DOMContentLoaded", reload);
})();
