// openSME portal — client enhancements (served at /app.js, CSP: script-src 'self').
//
// 1. Fast-switch: Ctrl/Cmd+K palette with fuzzy filtering over the service
//    cards, keyboard navigation (up/down/enter/esc).
// 2. AI assistant: minimal ask-box wired to POST /api/ai/chat (same-origin,
//    so CSP connect-src 'self' holds). The endpoint is only mounted when the
//    operator configured AI_API_URL; the box is hidden otherwise.

(function () {
  "use strict";

  // ── Fast-switch palette ────────────────────────────────────────────
  var palette = document.createElement("div");
  palette.id = "palette";
  palette.hidden = true;
  palette.innerHTML =
    '<div class="pal-box" role="dialog" aria-label="Quick switch">' +
    '<input id="pal-input" type="text" placeholder="Jump to…" autocomplete="off" spellcheck="false">' +
    '<ul id="pal-list"></ul>' +
    '<p class="pal-hint">↑↓ navigate · Enter open · Esc close</p>' +
    "</div>";
  document.body.appendChild(palette);

  var input = palette.querySelector("#pal-input");
  var list = palette.querySelector("#pal-list");
  var entries = []; // {name, url, node}
  var selected = 0;

  function collectEntries() {
    entries = Array.prototype.map.call(
      document.querySelectorAll("a.card[data-pal]"),
      function (a) {
        return {
          name: (a.getAttribute("data-pal") || "").toLowerCase(),
          url: a.href,
          node: a,
        };
      }
    );
  }

  function render(query) {
    var q = (query || "").toLowerCase().trim();
    var hits = q
      ? entries.filter(function (e) {
          return e.name.indexOf(q) !== -1;
        })
      : entries;
    selected = Math.min(selected, Math.max(hits.length - 1, 0));
    list.innerHTML = "";
    hits.forEach(function (e, i) {
      var li = document.createElement("li");
      li.textContent = e.name;
      if (i === selected) li.classList.add("sel");
      li.addEventListener("mousedown", function (ev) {
        ev.preventDefault();
        window.open(e.url, "_blank", "noopener");
        close();
      });
      list.appendChild(li);
    });
    if (!hits.length) {
      var none = document.createElement("li");
      none.className = "none";
      none.textContent = "no match";
      list.appendChild(none);
    }
  }

  function open() {
    collectEntries();
    selected = 0;
    render("");
    palette.hidden = false;
    input.value = "";
    input.focus();
  }

  function close() {
    palette.hidden = true;
  }

  document.addEventListener("keydown", function (ev) {
    if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "k") {
      ev.preventDefault();
      palette.hidden ? open() : close();
      return;
    }
    if (palette.hidden) return;
    if (ev.key === "Escape") {
      ev.preventDefault();
      close();
    } else if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
      ev.preventDefault();
      var n = list.querySelectorAll("li:not(.none)").length;
      if (!n) return;
      selected = ev.key === "ArrowDown" ? (selected + 1) % n : (selected + n - 1) % n;
      render(input.value);
    } else if (ev.key === "Enter") {
      ev.preventDefault();
      var hit = list.querySelector("li.sel");
      if (hit) {
        var name = hit.textContent.toLowerCase();
        var e = entries.filter(function (x) { return x.name === name; })[0];
        if (e) {
          window.open(e.url, "_blank", "noopener");
          close();
        }
      }
    }
  });

  input.addEventListener("input", function () {
    selected = 0;
    render(input.value);
  });

  palette.addEventListener("mousedown", function (ev) {
    if (ev.target === palette) close();
  });

  var hint = document.createElement("button");
  hint.id = "pal-hint";
  hint.type = "button";
  hint.title = "Quick switch (Ctrl+K)";
  hint.textContent = "⌘K";
  hint.addEventListener("click", open);
  document.body.appendChild(hint);

  // ── AI assistant box ──────────────────────────────────────────────
  var aiCard = document.querySelector("#ai-card");
  if (aiCard) {
    var box = aiCard.querySelector(".ai-box");
    var q = aiCard.querySelector(".ai-q");
    var out = aiCard.querySelector(".ai-out");
    var ask = aiCard.querySelector(".ai-ask");
    if (box && q && out && ask) {
      ask.addEventListener("click", function () {
        var question = (q.value || "").trim();
        if (!question) return;
        out.textContent = "…";
        out.hidden = false;
        fetch("/api/ai/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ question: question }),
        })
          .then(function (r) {
            if (!r.ok) throw new Error("HTTP " + r.status);
            return r.json();
          })
          .then(function (d) {
            out.textContent = d.answer || "(empty answer)";
          })
          .catch(function (err) {
            out.textContent = "AI unavailable: " + err.message;
          });
      });
      q.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter") {
          ev.preventDefault();
          ask.click();
        }
      });
    }
  }
  // ── Intercom: short notes with cloud attachments ────────────────────
  var icCard = document.querySelector("#intercom-card");
  if (icCard) {
    var icList = icCard.querySelector("#ic-list");
    var icText = icCard.querySelector(".ic-text");
    var icUrl = icCard.querySelector(".ic-url");
    var icFile = icCard.querySelector(".ic-file");
    var icFileLabel = icCard.querySelector(".ic-file-label");
    var icFileName = icCard.querySelector(".ic-file-name");
    var icSend = icCard.querySelector(".ic-send");
    var icOut = icCard.querySelector(".ic-out");

    icFile.addEventListener("change", function () {
      var f = icFile.files && icFile.files[0];
      if (f) {
        icFileName.textContent = f.name + " (" + humanSize(f.size) + ")";
        icUrl.value = ""; // either a cloud link or a picked file
      } else {
        icFileName.textContent = "";
      }
    });

    function humanSize(bytes) {
      if (bytes == null) return "";
      if (bytes < 1024) return bytes + " B";
      if (bytes < 1048576) return (bytes / 1024).toFixed(1) + " KB";
      return (bytes / 1048576).toFixed(1) + " MB";
    }

    function renderMessages(messages) {
      icList.textContent = "";
      (messages || []).forEach(function (m) {
        var li = document.createElement("li");
        li.textContent = m.text;
        if (m.attachment) {
          var a = document.createElement("a");
          a.className = "ic-att";
          a.href = m.attachment.url;
          a.target = "_blank";
          a.rel = "noopener noreferrer";
          var label = "\uD83D\uDCCE " + (m.attachment.name || "attachment");
          var sz = humanSize(m.attachment.size);
          if (sz) label += " (" + sz + ")";
          if (m.attachment.content_type) label += " \u00B7 " + m.attachment.content_type;
          a.textContent = label; // textContent — never innerHTML (XSS)
          li.appendChild(a);
        }
        icList.appendChild(li);
      });
    }

    function refresh() {
      fetch("/api/intercom")
        .then(function (r) { return r.json(); })
        .then(function (d) { renderMessages(d.messages); })
        .catch(function () {});
    }

    icSend.addEventListener("click", function () {
      var text = (icText.value || "").trim();
      if (!text) {
        icOut.textContent = "Write a short note first.";
        icOut.hidden = false;
        return;
      }
      var file = icFile.files && icFile.files[0];
      var url;
      var opts;
      if (file) {
        // Picked file: raw-body upload posts the note directly.
        url = "/api/intercom/upload?text=" + encodeURIComponent(text) +
              "&filename=" + encodeURIComponent(file.name);
        opts = {
          method: "POST",
          headers: { "Content-Type": file.type || "application/octet-stream" },
          body: file,
        };
      } else {
        var payload = { text: text };
        var att = (icUrl.value || "").trim();
        if (att) payload.attachment_url = att;
        url = "/api/intercom";
        opts = {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        };
      }
      icOut.textContent = "…";
      icOut.hidden = false;
      fetch(url, opts)
        .then(function (r) {
          if (!r.ok) return r.json().then(function (e) { throw new Error(e.error || ("HTTP " + r.status)); });
          return r.json();
        })
        .then(function (d) {
          icText.value = "";
          icUrl.value = "";
          icFile.value = "";
          icFileName.textContent = "";
          icOut.hidden = true;
          refresh();
        })
        .catch(function (err) {
          icOut.textContent = "Not sent: " + err.message;
        });
    });

    icText.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" && !ev.shiftKey) {
        ev.preventDefault();
        icSend.click();
      }
    });

    refresh();
  }

  // ── News columns: Startup / Markt / Legal (server-aggregated) ──────
  // The portal fetches external RSS/Atom server-side (/api/feeds) so the
  // browser stays same-origin (CSP connect-src 'self'). Lists are filled
  // with textContent only — feed titles are untrusted input.
  function markFeedUnavailable(list) {
    list.textContent = "";
    var li = document.createElement("li");
    li.className = "error";
    li.textContent = "Feed derzeit nicht verfügbar";
    list.appendChild(li);
  }

  function renderFeedColumn(col) {
    var list = document.getElementById("feed-" + col.id);
    if (!list) return;
    var entries = col.entries || [];
    if (!entries.length) {
      markFeedUnavailable(list);
      return;
    }
    list.textContent = "";
    entries.forEach(function (e) {
      var li = document.createElement("li");
      var a = document.createElement("a");
      a.href = e.url;
      a.target = "_blank";
      a.rel = "noopener";
      a.textContent = e.title;
      li.appendChild(a);
      var date = document.createElement("span");
      date.className = "date";
      var label = "";
      if (e.date) {
        var d = new Date(e.date);
        if (!isNaN(d.getTime())) {
          label = d.toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit", year: "numeric" });
        }
      }
      if (e.source) label = (label ? label + " · " : "") + e.source;
      date.textContent = label;
      li.appendChild(date);
      list.appendChild(li);
    });
  }

  function loadFeeds() {
    var lists = document.querySelectorAll("[id^='feed-']");
    if (!lists.length) return;
    fetch("/api/feeds")
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (d) {
        (d.columns || []).forEach(renderFeedColumn);
        // Any configured column the server didn't answer gets a fallback.
        Array.prototype.forEach.call(lists, function (l) {
          if (/^feed-[a-z0-9-]+$/.test(l.id) && !l.childNodes.length) {
            markFeedUnavailable(l);
          }
        });
      })
      .catch(function () {
        Array.prototype.forEach.call(lists, markFeedUnavailable);
      });
  }

  loadFeeds();
  setInterval(loadFeeds, 10 * 60 * 1000);
})();
