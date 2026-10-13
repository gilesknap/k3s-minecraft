// Small progressive enhancements: copy buttons, live log tail (SSE), filter.
(function () {
  "use strict";

  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest("[data-copy]");
    if (!btn) return;
    var text = btn.getAttribute("data-copy");
    var ok = function () {
      btn.textContent = "✓";
      setTimeout(function () { btn.textContent = "⧉"; }, 1200);
    };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(ok);
    } else {
      var ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); ok(); } finally { ta.remove(); }
    }
  });

  // Close other open <details class="pop"> when one opens.
  document.addEventListener("toggle", function (ev) {
    var d = ev.target;
    if (!(d instanceof HTMLDetailsElement) || !d.open || !d.classList.contains("pop")) return;
    document.querySelectorAll("details.pop[open]").forEach(function (o) {
      if (o !== d) o.open = false;
    });
  }, true);

  function startLog() {
    var pre = document.getElementById("log");
    if (!pre || !window.EventSource) return;
    var state = document.getElementById("log-state");
    var MAX = 500;
    var es = new EventSource(pre.getAttribute("data-src"));
    var setState = function (t) { if (state) state.textContent = t; };
    es.onopen = function () { setState("live"); };
    es.onmessage = function (ev) {
      var stick = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
      pre.appendChild(document.createTextNode(ev.data + "\n"));
      while (pre.childNodes.length > MAX) pre.removeChild(pre.firstChild);
      if (stick) pre.scrollTop = pre.scrollHeight;
    };
    es.addEventListener("end", function () { setState("ended"); es.close(); });
    es.addEventListener("error", function (ev) {
      if (ev.data) {
        setState("error: " + ev.data);
        es.close();
      } else if (es.readyState === EventSource.CLOSED) {
        setState("disconnected");
      } else {
        setState("reconnecting…");
      }
    });
    window.addEventListener("beforeunload", function () { es.close(); });
  }

  function startFilter() {
    var input = document.querySelector("input[data-filter]");
    if (!input) return;
    var sel = input.getAttribute("data-filter");
    input.addEventListener("input", function () {
      var q = input.value.trim().toLowerCase();
      document.querySelectorAll(sel).forEach(function (el) {
        el.hidden = q !== "" && el.getAttribute("data-name").indexOf(q) === -1;
      });
    });
  }

  // RCON console: Up/Down history (per world, in localStorage) and a
  // suggestion dropdown fed by GET <data-complete>?line=... .
  function startConsole() {
    var input = document.getElementById("rcon-input");
    var list = document.getElementById("rcon-suggest");
    if (!input || !list) return;
    var form = input.form;
    var help = document.getElementById("rcon-help");
    var helpText = help ? help.textContent : "";
    var key = input.getAttribute("data-history-key");
    var url = input.getAttribute("data-complete");
    var MAX = 100;

    var load = function () {
      try {
        var h = JSON.parse(localStorage.getItem(key) || "[]");
        return Array.isArray(h) ? h.filter(function (s) { return typeof s === "string"; }) : [];
      } catch (e) { return []; }
    };
    var history = load();
    var pos = history.length;  // == length: editing the draft, not a recalled entry
    var draft = "";
    var sent = null;

    var setValue = function (v) {
      input.value = v;
      input.setSelectionRange(v.length, v.length);
    };

    // Record a command only once the request succeeded.
    form.addEventListener("htmx:beforeRequest", function () { sent = input.value.trim(); });
    form.addEventListener("htmx:afterRequest", function (ev) {
      if (ev.detail.successful && sent) {
        history = load();  // another tab may have added some
        if (history[history.length - 1] !== sent) history.push(sent);
        if (history.length > MAX) history = history.slice(history.length - MAX);
        try { localStorage.setItem(key, JSON.stringify(history)); } catch (e) { /* full or blocked */ }
      }
      sent = null;
      pos = history.length;
      draft = "";
      close();
    });

    // ---- suggestions
    var items = [];   // [{text, kind, detail}] as returned by the server
    var start = 0;    // where the token being completed starts in the input
    var active = -1;  // index into items, -1 = none highlighted
    var seq = 0;
    var timer = null;

    var isOpen = function () { return !list.hidden; };
    function close() {
      list.hidden = true;
      list.textContent = "";
      items = [];
      active = -1;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
    }
    var pickable = function (i) { return items[i] && items[i].kind !== "hint"; };

    var highlight = function (i) {
      active = i;
      Array.prototype.forEach.call(list.children, function (li, n) {
        li.classList.toggle("active", n === i);
        li.setAttribute("aria-selected", n === i ? "true" : "false");
        if (n === i) {
          input.setAttribute("aria-activedescendant", li.id);
          li.scrollIntoView({ block: "nearest" });
        }
      });
    };

    var render = function (data) {
      list.textContent = "";
      items = data.items || [];
      start = data.start || 0;
      active = -1;
      items.forEach(function (it, n) {
        var li = document.createElement("li");
        li.id = "rcon-sug-" + n;
        li.className = it.kind;
        li.setAttribute("role", "option");
        li.appendChild(document.createTextNode(it.text));
        if (it.detail) {
          var d = document.createElement("span");
          d.className = "detail";
          d.textContent = it.detail;
          li.appendChild(d);
        }
        list.appendChild(li);
      });
      if (data.status === "pending") {
        var note = document.createElement("li");
        note.className = "note";
        note.textContent = "Loading the command list…";
        list.appendChild(note);
      }
      var show = list.children.length > 0 && input.value.trim() !== "";
      list.hidden = !show;
      input.setAttribute("aria-expanded", show ? "true" : "false");
      if (help) {
        help.textContent = data.status === "none" && data.message
          ? helpText + " · no completion: " + data.message
          : helpText;
      }
    };

    var fetchSuggestions = function () {
      clearTimeout(timer);
      var line = input.value;
      if (input.selectionStart !== line.length) { close(); return; }  // caret mid-line
      var mine = ++seq;
      fetch(url + "?line=" + encodeURIComponent(line), { headers: { Accept: "application/json" } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (data) {
          if (mine !== seq || document.activeElement !== input) return;  // stale
          if (!data) { close(); return; }
          render(data);
          if (data.status === "pending") timer = setTimeout(fetchSuggestions, 4000);
        })
        .catch(function () { if (mine === seq) close(); });
    };
    var schedule = function () {
      clearTimeout(timer);
      timer = setTimeout(fetchSuggestions, 120);
    };

    var accept = function (i) {
      var it = items[i];
      if (!it || it.kind === "hint") return false;
      setValue(input.value.slice(0, start) + it.text + " ");
      pos = history.length;
      fetchSuggestions();
      return true;
    };

    var move = function (step) {
      var n = items.length;
      for (var k = 1; k <= n; k++) {
        var i = ((active < 0 ? (step > 0 ? -1 : n) : active) + step * k + n * 2) % n;
        if (pickable(i)) { highlight(i); return; }
      }
    };

    input.addEventListener("input", function () {
      pos = history.length;
      schedule();
    });
    input.addEventListener("focus", function () {
      // Warms the server's command list for this world; opens only if typed.
      fetchSuggestions();
    });
    input.addEventListener("blur", function () {
      clearTimeout(timer);
      seq++;
      setTimeout(close, 150);
    });
    list.addEventListener("mousedown", function (ev) {
      var li = ev.target.closest("li");
      ev.preventDefault();  // keep focus in the input
      if (li) accept(Array.prototype.indexOf.call(list.children, li));
    });

    input.addEventListener("keydown", function (ev) {
      if (ev.altKey || ev.ctrlKey || ev.metaKey || ev.isComposing) return;
      var open = isOpen() && items.some(function (it, i) { return pickable(i); });
      switch (ev.key) {
        case "ArrowUp":
        case "ArrowDown":
          ev.preventDefault();
          if (open) { move(ev.key === "ArrowDown" ? 1 : -1); return; }
          if (ev.key === "ArrowUp") {
            if (pos === 0 || !history.length) return;
            if (pos === history.length) draft = input.value;
            pos--;
            setValue(history[pos]);
          } else {
            if (pos >= history.length) return;
            pos++;
            setValue(pos === history.length ? draft : history[pos]);
          }
          close();
          return;
        case "Tab":
          if (!open || ev.shiftKey) return;
          ev.preventDefault();
          accept(active >= 0 ? active : items.findIndex(function (it, i) { return pickable(i); }));
          return;
        case "Enter":
          if (open && active >= 0) { ev.preventDefault(); accept(active); }
          return;  // otherwise submit as usual
        case "Escape":
          ev.preventDefault();
          if (isOpen()) { close(); return; }
          setValue("");
          pos = history.length;
          draft = "";
          return;
      }
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    startLog();
    startFilter();
    startConsole();
  });
})();
