// openSME portal — client enhancements (served at /app.js, CSP: script-src 'self').
//
// 1. Fast-switch: Ctrl/Cmd+K palette with fuzzy filtering over the service
//    cards, keyboard navigation (up/down/enter/esc).
// 2. AI assistant: minimal ask-box wired to POST /api/ai/chat (same-origin,
//    so CSP connect-src 'self' holds). The endpoint is only mounted when the
//    operator configured AI_API_URL; the box is hidden otherwise.
// 3. Tabs: Dienste / Umfragen / Notizen switch panels in-page (no reload).
// 4. Logout: POST /api/logout in a plain form (HttpOnly session cookie —
//    the button is always rendered; the handler is a no-op without a session).

(function () {
  "use strict";

  // ── Tabs ────────────────────────────────────────────────────────────
  var tabButtons = document.querySelectorAll(".tab-btn");
  var tabPanels = document.querySelectorAll(".tab-panel");
  tabButtons.forEach(function (btn) {
    btn.addEventListener("click", function () {
      var target = btn.getAttribute("data-tab");
      tabButtons.forEach(function (b) {
        var active = b === btn;
        b.classList.toggle("is-active", active);
        b.setAttribute("aria-selected", active ? "true" : "false");
      });
      tabPanels.forEach(function (p) {
        p.hidden = p.id !== "tab-" + target;
      });
    });
  });

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

// ── Polls (Umfragen) — optional card, present only when POLLS_ENABLED ──
(function () {
  "use strict";
  var plCard = document.querySelector("#polls-card");
  if (!plCard) return;

  var plList = plCard.querySelector("#pl-list");
  var plQuestion = plCard.querySelector(".pl-question");
  var plOptions = plCard.querySelector(".pl-options");
  var plUrl = plCard.querySelector(".pl-url");
  var plFile = plCard.querySelector("#pl-file");
  var plFileName = plCard.querySelector(".ic-file-name");
  var plCreate = plCard.querySelector(".pl-create");
  var plOut = plCard.querySelector(".pl-out");
  var VOTED_KEY = "opensme-poll-voted";

  function votedSet() {
    try {
      return JSON.parse(localStorage.getItem(VOTED_KEY) || "{}") || {};
    } catch (e) {
      return {};
    }
  }
  function markVoted(id) {
    var v = votedSet();
    v[id] = true;
    try {
      localStorage.setItem(VOTED_KEY, JSON.stringify(v));
    } catch (e) {}
  }
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function fail(msg) {
    plOut.textContent = msg;
    plOut.hidden = false;
  }
  function resetForm() {
    plQuestion.value = "";
    plOptions.value = "";
    plUrl.value = "";
    plFile.value = "";
    plFileName.textContent = "";
    plOut.hidden = true;
  }

  function renderPolls(polls) {
    plList.textContent = "";
    if (!polls || !polls.length) {
      plList.appendChild(el("li", "ic-empty", "Noch keine Umfragen — leg die erste an."));
      return;
    }
    var voted = votedSet();
    polls.forEach(function (p) {
      var li = el("li", "poll");
      li.appendChild(el("div", "poll-q", p.question));
      if (p.attachment && p.attachment.url) {
        var a = document.createElement("a");
        a.className = "poll-att";
        a.href = p.attachment.url;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        a.textContent = "\uD83D\uDCCE " + (p.attachment.name || "Anhang");
        li.appendChild(a);
      }
      var opts = el("div", "poll-opts");
      var total = (p.votes || []).reduce(function (acc, v) { return acc + v; }, 0);
      var isVoted = p.closed || voted[p.id];
      (p.options || []).forEach(function (opt, i) {
        var row = el("div", "poll-opt");
        var count = (p.votes && p.votes[i]) || 0;
        var pct = total ? Math.round((count / total) * 100) : 0;
        var fill = el("span", "poll-fill");
        fill.style.width = pct + "%";
        row.appendChild(fill);
        row.appendChild(el("span", "poll-label", opt));
        row.appendChild(el("span", "poll-count", total ? count + " · " + pct + "%" : String(count)));
        if (!isVoted) {
          var btn = el("button", "poll-vote", "Wählen");
          btn.type = "button";
          btn.setAttribute("aria-label", "Für „" + opt + "“ stimmen");
          btn.addEventListener("click", function () {
            fetch("/api/polls/" + p.id + "/vote", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ option: i })
            })
              .then(function (r) {
                if (!r.ok) throw new Error("HTTP " + r.status);
                markVoted(p.id);
                refresh();
              })
              .catch(function (err) { fail("Nicht gewählt: " + err.message); });
          });
          row.appendChild(btn);
        } else {
          row.classList.add("voted");
        }
        opts.appendChild(row);
      });
      li.appendChild(opts);
      plList.appendChild(li);
    });
  }

  function refresh() {
    fetch("/api/polls")
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function (d) { renderPolls(d.polls); })
      .catch(function () {
        plList.textContent = "";
        plList.appendChild(el("li", "ic-empty", "Umfragen nicht erreichbar."));
      });
  }

  plFile.addEventListener("change", function () {
    var f = plFile.files && plFile.files[0];
    if (!f) return;
    plFileName.textContent = f.name + " (" + Math.round(f.size / 1024) + " KB)";
  });

  plCreate.addEventListener("click", function () {
    var question = (plQuestion.value || "").trim();
    var options = (plOptions.value || "")
      .split("\n")
      .map(function (s) { return s.trim(); })
      .filter(Boolean);
    if (!question || options.length < 2) {
      fail("Bitte Frage und mindestens 2 Optionen angeben.");
      return;
    }
    plOut.textContent = "…";
    plOut.hidden = false;
    var file = plFile.files && plFile.files[0];
    var done = function () { resetForm(); refresh(); };
    var extraUrl = (plUrl.value || "").trim();
    if (extraUrl) {
      extraUrl = "&attachment_url=" + encodeURIComponent(extraUrl);
    } else {
      extraUrl = "";
    }
    if (file) {
      var url =
        "/api/polls/upload?question=" + encodeURIComponent(question) +
        "&options=" + encodeURIComponent(JSON.stringify(options)) +
        extraUrl +
        "&filename=" + encodeURIComponent(file.name);
      fetch(url, {
        method: "POST",
        headers: { "Content-Type": file.type || "application/octet-stream" },
        body: file
      })
        .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); })
        .then(done)
        .catch(function (err) { fail("Anlegen fehlgeschlagen: " + err.message); });
    } else {
      fetch("/api/polls", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          question: question,
          options: options,
          attachment_url: extraUrl ? (plUrl.value || "").trim() : ""
        })
      })
        .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); })
        .then(done)
        .catch(function (err) { fail("Anlegen fehlgeschlagen: " + err.message); });
    }
  });

  refresh();
})();
