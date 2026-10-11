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

  document.addEventListener("DOMContentLoaded", function () {
    startLog();
    startFilter();
  });
})();
