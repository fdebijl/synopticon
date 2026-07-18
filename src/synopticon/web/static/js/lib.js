/* Synopticon shared front-end helpers, exposed as window.Syn.
   fetch wrappers (401 -> /login), toasts, native-dialog confirms, menus, theme. */
(function () {
  "use strict";

  function redirectLogin() {
    var next = encodeURIComponent(location.pathname + location.search);
    location.href = "/login?next=" + next;
  }

  async function fetchJSON(url, opts) {
    var res = await fetch(url, opts || {});
    if (res.status === 401) { redirectLogin(); throw new Error("unauthorized"); }
    var data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (!res.ok) {
      var err = new Error((data && data.error) || ("HTTP " + res.status));
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  function postJSON(url, body) {
    return fetchJSON(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
  }

  function toast(message, kind) {
    var region = document.getElementById("toast-region");
    if (!region) { console.log(message); return; }
    var el = document.createElement("div");
    el.className = "toast" + (kind ? " " + kind : "");
    el.setAttribute("role", "status");
    el.textContent = message;
    region.appendChild(el);
    setTimeout(function () { el.remove(); }, 4000);
  }

  /* Theme: auto | light | dark. `auto` clears the override so the OS decides. */
  function setTheme(mode) {
    if (mode === "auto") {
      document.documentElement.removeAttribute("data-theme");
      localStorage.removeItem("syn-theme");
    } else {
      document.documentElement.setAttribute("data-theme", mode);
      localStorage.setItem("syn-theme", mode);
    }
    closeMenus();
  }

  function closeMenus() {
    document.querySelectorAll(".menu.open").forEach(function (m) {
      m.classList.remove("open");
      var btn = m.querySelector("[aria-expanded]");
      if (btn) btn.setAttribute("aria-expanded", "false");
    });
  }

  function toggleMenu(id) {
    var m = document.getElementById(id);
    if (!m) return;
    var wasOpen = m.classList.contains("open");
    closeMenus();
    if (!wasOpen) {
      m.classList.add("open");
      var btn = m.querySelector("[aria-expanded]");
      if (btn) btn.setAttribute("aria-expanded", "true");
    }
  }

  document.addEventListener("click", function (e) {
    if (!e.target.closest(".menu")) closeMenus();
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") closeMenus();
  });

  /* Native <dialog> confirm. Returns a Promise<boolean>. When phrase is given,
     the OK button stays disabled until the exact phrase is typed (2s minimum
     delay to defeat reflexive clicking); cancel is default focus. */
  function confirmDialog(opts) {
    opts = opts || {};
    var dlg = document.getElementById("confirm-modal");
    if (!dlg || !dlg.showModal) {
      return Promise.resolve(window.confirm(opts.message || "Are you sure?"));
    }
    var title = document.getElementById("confirm-title");
    var msg = document.getElementById("confirm-message");
    var okBtn = document.getElementById("confirm-ok");
    var cancelBtn = document.getElementById("confirm-cancel");
    var phraseWrap = document.getElementById("confirm-phrase-wrap");
    var phraseLabel = document.getElementById("confirm-phrase-label");
    var phraseInput = document.getElementById("confirm-phrase-input");

    title.textContent = opts.title || "Confirm";
    msg.textContent = opts.message || "";
    okBtn.textContent = opts.okLabel || "Confirm";
    okBtn.className = "btn " + (opts.danger === false ? "btn-action" : "btn-danger");

    var needPhrase = !!opts.phrase;
    phraseWrap.hidden = !needPhrase;
    phraseInput.value = "";
    if (needPhrase) {
      phraseLabel.textContent = 'Type "' + opts.phrase + '" to confirm';
      okBtn.disabled = true;
      var armed = false;
      setTimeout(function () { armed = true; recheck(); }, 2000);
      function recheck() {
        okBtn.disabled = !(armed && phraseInput.value === opts.phrase);
      }
      phraseInput.oninput = recheck;
    } else {
      okBtn.disabled = false;
    }

    return new Promise(function (resolve) {
      function done(ok) {
        dlg.removeEventListener("close", onClose);
        resolve(ok);
      }
      function onClose() { done(dlg.returnValue === "ok"); }
      dlg.addEventListener("close", onClose);
      dlg.showModal();
      (needPhrase ? cancelBtn : cancelBtn).focus();
    });
  }

  /* Sequential job ids render as "#42"; legacy timestamp-uuid ids as-is. */
  function fmtJobId(id) {
    return /^\d+$/.test(id) ? "#" + id : id;
  }

  window.Syn = {
    fetchJSON: fetchJSON,
    postJSON: postJSON,
    toast: toast,
    setTheme: setTheme,
    toggleMenu: toggleMenu,
    confirm: confirmDialog,
    fmtJobId: fmtJobId,
  };
})();
