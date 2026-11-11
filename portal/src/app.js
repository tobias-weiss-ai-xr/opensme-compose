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
            // Session-gated (metered endpoint): anonymous callers get 401 —
            // tell them why instead of a generic "unavailable".
            if (r.status === 401) {
              throw new Error("bitte zuerst anmelden — die KI-Antwort ist nur für Mitglieder");
            }
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

    function fmtTime(unixSecs) {
      if (!unixSecs) return "";
      var d = new Date(Number(unixSecs) * 1000);
      if (isNaN(d.getTime())) return "";
      var hh = String(d.getHours()).padStart(2, "0");
      var mm = String(d.getMinutes()).padStart(2, "0");
      return hh + ":" + mm;
    }

    function renderMessages(messages) {
      icList.textContent = "";
      (messages || []).forEach(function (m) {
        var li = document.createElement("li");
        // Kind badge: "note" stays badge-free (the default), info/warn get
        // a tiny pill. Class is built from a checked whitelist, label from
        // textContent — never innerHTML (XSS).
        if (m.kind === "info" || m.kind === "warn") {
          var kind = document.createElement("span");
          kind.className = "ic-kind ic-kind-" + m.kind;
          kind.textContent = m.kind === "warn" ? "WARN" : "INFO";
          li.appendChild(kind);
        }
        // meta line: author · time — textContent only, never innerHTML
        var meta = [];
        if (m.author) meta.push(m.author);
        var t = fmtTime(m.created_at);
        if (t) meta.push(t);
        if (meta.length) {
          var span = document.createElement("span");
          span.className = "ic-meta";
          span.textContent = meta.join(" · ");
          li.appendChild(span);
        }
        li.appendChild(document.createTextNode(m.text));
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
        // Dismiss control (moderation): a plain button carrying the id it
        // deletes; the single delegated listener below does the rest.
        var del = document.createElement("button");
        del.type = "button";
        del.className = "ic-del";
        del.setAttribute("data-id", String(m.id));
        del.setAttribute("aria-label", "Löschen");
        del.textContent = "\u2715";
        li.appendChild(del);
        icList.appendChild(li);
      });
    }

    // Delta polling: keep a client-side mirror of the newest-first list,
    // fetch only messages newer than our cursor (?since=<id>), merge and
    // re-render. First load (cursor 0) pulls everything in one call.
    var icAll = [];
    var icLastId = 0;

    function mergeMessages(incoming) {
      if (!(incoming && incoming.length)) return;
      var byId = {};
      icAll.forEach(function (m) { byId[m.id] = m; });
      incoming.forEach(function (m) { byId[m.id] = m; });
      icAll = Object.keys(byId)
        .map(function (k) { return byId[k]; })
        .sort(function (a, b) { return a.id - b.id; })
        .slice(-MAX_MESSAGES_CLIENT);
      if (icAll.length) icLastId = icAll[icAll.length - 1].id;
      renderMessages(icAll.slice().reverse());
      updateUnread();
    }
    var MAX_MESSAGES_CLIENT = 50;

    // ── Unread "N neu" (pure client state, persisted in localStorage) ──
    // icLastRead is the newest id the user has effectively seen; anything
    // above it counts as new. Survives reloads (and the store-reset path,
    // which already rewinds icLastId/icAll to 0).
    var IC_READ_KEY = "intercomLastRead";
    var icLastRead = 0;
    try {
      icLastRead = parseInt(localStorage.getItem(IC_READ_KEY), 10) || 0;
    } catch (e) {
      icLastRead = 0; // storage blocked (private mode): session-only badge
    }

    // The Notizen panel starts hidden, so a badge inside the card is
    // invisible until that tab is opened — mirror the count on the
    // always-visible tab button too, so new notes are discoverable from
    // every tab.
    var icBadgeHosts = [];
    var icCardTitle = icCard.querySelector("h3");
    if (icCardTitle) icBadgeHosts.push(icCardTitle);
    var icTabBtn = document.querySelector('.tab-btn[data-tab="notizen"]');
    if (icTabBtn) icBadgeHosts.push(icTabBtn);
    var icUnreadEls = [];

    function unreadBadges() {
      if (!icUnreadEls.length) {
        icBadgeHosts.forEach(function (host) {
          var b = document.createElement("span");
          b.className = "ic-unread";
          b.setAttribute("role", "status");
          b.hidden = true;
          host.appendChild(b); // textContent only — never innerHTML
          icUnreadEls.push(b);
        });
      }
      return icUnreadEls;
    }

    function updateUnread() {
      var n = 0;
      for (var i = 0; i < icAll.length; i++) {
        if (icAll[i].id > icLastRead) n++;
      }
      unreadBadges().forEach(function (b) {
        if (n > 0) {
          b.textContent = n + " neu";
          b.hidden = false;
        } else {
          b.hidden = true;
        }
      });
    }

    function markRead() {
      // Never while the tab is hidden — coming back must still show how
      // much arrived in the meantime.
      if (document.hidden) return;
      var latest = icAll.length ? icAll[icAll.length - 1].id : 0;
      if (!latest || icLastRead >= latest) {
        updateUnread();
        return;
      }
      icLastRead = latest;
      try {
        localStorage.setItem(IC_READ_KEY, String(latest));
      } catch (e) {
        /* storage blocked — the badge just resets on next load */
      }
      updateUnread();
    }

    function cardOnScreen() {
      if (document.hidden) return false;
      // Notizen tab not open (the panel is display:none) → not seen.
      if (icCard.closest && icCard.closest("[hidden]")) return false;
      var r = icCard.getBoundingClientRect();
      return r.height > 0 && r.bottom > 0 && r.top < (window.innerHeight || 0);
    }

    // Auto mark-read once the card is actually looked at: first paint in
    // view (IntersectionObserver fires the moment the Notizen tab opens),
    // plus interactive clicks/focus inside the card.
    if ("IntersectionObserver" in window) {
      new IntersectionObserver(
        function (entries) {
          for (var i = 0; i < entries.length; i++) {
            if (entries[i].isIntersecting) {
              markRead();
              break;
            }
          }
        },
        { threshold: 0.1 }
      ).observe(icCard);
    } else {
      var seenCheck = function () {
        if (cardOnScreen()) markRead();
      };
      window.addEventListener("scroll", seenCheck, { passive: true });
      document.addEventListener("visibilitychange", seenCheck);
    }
    ["click", "focusin"].forEach(function (evt) {
      icCard.addEventListener(evt, markRead, true);
    });

    // Delete (moderation): one delegated listener for every .ic-del in
    // the list — the buttons themselves carry no handlers.
    icList.addEventListener("click", function (ev) {
      var btn = ev.target && ev.target.closest ? ev.target.closest(".ic-del") : null;
      if (!btn || !icList.contains(btn)) return;
      ev.preventDefault();
      if (!window.confirm("Nachricht löschen?")) return;
      var id = btn.getAttribute("data-id") || "";
      fetch("/api/intercom/" + encodeURIComponent(id), { method: "DELETE" })
        .then(function (r) {
          if (r.status === 401) {
            icStopPolling(); // session gone — same rule as refresh()
            throw new Error("session expired");
          }
          if (r.status === 404) {
            refresh(); // someone else deleted it first — just resync
            return;
          }
          if (r.status !== 204) throw new Error("HTTP " + r.status);
          // Drop the mirror and the cursor, then pull the whole list
          // again so the remaining notes re-render and icLastId stays
          // truthful (the deleted id must not linger in either).
          icAll = [];
          icLastId = 0;
          return fetch("/api/intercom?since=0")
            .then(function (r2) {
              if (r2.status === 401) {
                icStopPolling();
                throw new Error("session expired");
              }
              return r2.json();
            })
            .then(function (full) {
              mergeMessages(full.messages);
              markRead(); // the user is looking at the list right now
            });
        })
        .catch(function (err) {
          icOut.textContent = "Not deleted: " + ((err && err.message) || "unknown error");
          icOut.hidden = false;
        });
    });

    var icPollTimer = null;
    function icStopPolling() {
      if (icPollTimer) {
        clearInterval(icPollTimer);
        icPollTimer = null;
      }
    }

    function refresh() {
      // Hidden tab: skip this tick (the visibilitychange listener below
      // catches up the moment the tab is shown again). Returns the
      // in-flight promise so callers (send/delete) can chain markRead().
      if (document.hidden) return;
      return fetch("/api/intercom?since=" + icLastId)
        .then(function (r) {
          // Session gone (logged out / expired): stop polling for good —
          // each further tick would just 401 against the gate.
          if (r.status === 401) {
            icStopPolling();
            throw new Error("session expired");
          }
          return r.json();
        })
        .then(function (d) {
          var latest = d.latest || 0;
          if (latest < icLastId) {
            // Store reset behind our cursor (portal restart, fresh store):
            // ids restart at 1 — the stale cursor would hide every new
            // note. Full reload and merge from scratch.
            icAll = [];
            icLastId = 0;
            return fetch("/api/intercom?since=0")
              .then(function (r2) { return r2.json(); })
              .then(function (full) { mergeMessages(full.messages); })
              .catch(function () {});
          }
          if (latest) icLastId = latest;
          mergeMessages(d.messages);
        })
        .catch(function () {});
    }

    // Teammates' notes appear without a reload (30 s is plenty for a
    // short-message panel; the delta keeps each poll tiny). Paused while
    // the tab is hidden — refresh() early-returns and the visibility
    // listener below fires an immediate catch-up on show.
    icPollTimer = setInterval(refresh, 30 * 1000);
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden && icPollTimer) refresh();
    });

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
          var p = refresh();
          // The author has obviously seen their own note.
          if (p) p.then(markRead);
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

    // ☁️ aus der Cloud: open the cloud picker (/files?picker=1) in a new
    // tab and consume its cross-tab handback. Same-origin, so the picker
    // tab's localStorage write reaches this page via the storage event;
    // the focus listener covers a missed event (picker tab closed very
    // fast). A closed-without-pick simply leaves the key absent — ignored.
    var icCloud = icCard.querySelector(".ic-cloud");
    if (icCloud) {
      var CLOUD_PICK_KEY = "cloudPickResult";
      icCloud.addEventListener("click", function () {
        // Stale result from an earlier session must not leak into this pick.
        try { localStorage.removeItem(CLOUD_PICK_KEY); } catch (e) {}
        window.open("/files?picker=1", "_blank");
        var consumePick = function () {
          var picked = null;
          try { picked = localStorage.getItem(CLOUD_PICK_KEY); } catch (e) { return; }
          if (!picked) return; // picker closed without a pick — ignore
          try { localStorage.removeItem(CLOUD_PICK_KEY); } catch (e) {}
          if (icUrl) icUrl.value = picked;
          if (icFile) icFile.value = ""; // either a cloud link or a picked file
          if (icFileName) icFileName.textContent = "";
          window.focus();
        };
        window.addEventListener("storage", consumePick);
        window.addEventListener("focus", consumePick);
      });
    }

    refresh();
  }
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

// ── Cloud file picker (/files) — SSR page, JS enhancement ─────────────
// The server renders the current directory's entries (no-JS browsing +
// download work). This module re-pulls the list via the /api/cloud
// bridge and adds upload (PUT), mkdir (POST), delete (DELETE) and the
// SELECT handback: in ?picker=1 mode selecting a file writes its
// canonical URL into localStorage 'cloudPickResult' (same origin) and
// closes the tab; the host page (intercom card) consumes it.
(function () {
  "use strict";
  var page = document.getElementById("cloud-page");
  if (!page) return;

  var PICK_KEY = "cloudPickResult";
  var pickerMode = page.getAttribute("data-picker") === "1";

  var grid = document.getElementById("cf-grid");
  var trail = document.getElementById("cf-trail");
  var msg = document.getElementById("cf-msg");
  var fileInput = document.getElementById("cf-file");
  var fileName = document.getElementById("cf-file-name");
  var uploadBtn = document.getElementById("cf-upload");
  var mkdirInput = document.getElementById("cf-mkdir-name");
  var mkdirBtn = document.getElementById("cf-mkdir");
  var doneBtn = document.getElementById("cf-done");

  // ── path state (mirrors cloud::validate_path on the server) ──────────
  function cleanPath(raw) {
    var segs = String(raw || "").split("/").filter(function (s) { return s.length; });
    for (var i = 0; i < segs.length; i++) {
      if (segs[i] === "." || segs[i] === "..") return "";
    }
    return segs.join("/");
  }
  function pathFromSearch(search) {
    var m = /[?&]path=([^&]*)/.exec(search || "");
    if (!m) return "";
    try {
      // "+" is a space in query strings (the SSR links are form-encoded)
      return cleanPath(decodeURIComponent(m[1].replace(/\+/g, " ")));
    } catch (e) { return ""; }
  }
  var current = pathFromSearch(window.location.search);

  function joinPath(dir, name) {
    return dir ? dir + "/" + name : name;
  }
  function pageUrl(dir, picker) {
    var q = [];
    if (dir) q.push("path=" + encodeURIComponent(dir));
    if (picker) q.push("picker=1");
    return "/files" + (q.length ? "?" + q.join("&") : "");
  }
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function humanSize(bytes) {
    if (bytes == null) return "—";
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1048576) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / 1048576).toFixed(1) + " MB";
  }
  function say(text, ok) {
    if (!msg) return;
    msg.textContent = text;
    msg.classList.toggle("is-ok", !!ok);
    msg.hidden = false;
  }
  function failText(r) {
    return r.json().then(function (e) {
      throw new Error(e.error || ("HTTP " + r.status));
    }, function () {
      throw new Error("HTTP " + r.status);
    });
  }

  // ── rendering (createElement/textContent only — file names are
  //    user data from the cloud, never innerHTML) ─────────────────────
  function row(entry) {
    var rel = joinPath(current, entry.name);
    var li = el("li", "cf-row " + (entry.is_dir ? "cf-dir" : "cf-file"));
    var main;
    if (entry.is_dir) {
      // Dir click navigates in every mode — picking happens per FILE.
      main = document.createElement("a");
      main.href = pageUrl(rel, pickerMode);
    } else {
      main = el("span", "cf-main");
    }
    main.className = "cf-main";
    main.appendChild(el("span", "cf-icon", entry.is_dir ? "📁" : "📄"));
    main.appendChild(el("span", "cf-name", entry.name));
    main.appendChild(el("span", "cf-size", entry.is_dir ? "Ordner" : humanSize(entry.size)));
    li.appendChild(main);

    var actions = el("span", "cf-actions");
    if (!entry.is_dir) {
      var dl = document.createElement("a");
      dl.className = "cf-dl";
      dl.textContent = "Download";
      dl.href = "/api/cloud/file?path=" + encodeURIComponent(rel);
      actions.appendChild(dl);
      var sel = el("button", "cf-select", "Auswählen");
      sel.type = "button";
      sel.setAttribute("data-path", rel);
      sel.setAttribute("data-name", entry.name);
      actions.appendChild(sel);
    }
    var del = el("button", "cf-del", "🗑");
    del.type = "button";
    del.setAttribute("aria-label", "Löschen");
    del.setAttribute("data-path", rel);
    del.setAttribute("data-name", entry.name);
    del.setAttribute("data-is-dir", entry.is_dir ? "1" : "0");
    actions.appendChild(del);
    li.appendChild(actions);
    return li;
  }

  function renderTrail() {
    if (!trail) return;
    trail.textContent = "";
    var root = document.createElement("a");
    root.href = pageUrl("", pickerMode);
    root.textContent = "☁ Cloud-Dateien";
    trail.appendChild(root);
    var acc = "";
    (current ? current.split("/") : []).forEach(function (seg) {
      trail.appendChild(el("span", "cf-sep", "›"));
      acc = acc ? acc + "/" + seg : seg;
      if (acc === current) {
        trail.appendChild(el("span", "cf-here", seg));
      } else {
        var a = document.createElement("a");
        a.href = pageUrl(acc, pickerMode);
        a.textContent = seg;
        trail.appendChild(a);
      }
    });
    // keep the URL deep-linkable while browsing without a reload
    try { window.history.replaceState(null, "", pageUrl(current, pickerMode)); } catch (e) {}
  }

  function render(entries) {
    grid.textContent = "";
    if (!entries.length) {
      grid.appendChild(el("li", "cf-empty", "Dieser Ordner ist leer."));
    } else {
      entries.forEach(function (e) { grid.appendChild(row(e)); });
    }
    renderTrail();
  }

  function load() {
    return fetch("/api/cloud/files?path=" + encodeURIComponent(current))
      .then(function (r) {
        if (r.status === 503) throw new Error("Cloud-Speicher ist nicht konfiguriert.");
        if (!r.ok) return failText(r);
        return r.json();
      })
      .then(function (d) { render(d.entries || []); })
      .catch(function (err) { say("Ordner nicht ladbar: " + err.message); });
  }

  // ── select: hand the canonical URL back ─────────────────────────────
  function handback(url, name) {
    try { localStorage.setItem(PICK_KEY, url); } catch (e) {}
    say("✔ Ausgewählt: " + name, true);
    if (msg) msg.classList.add("is-ok");
    setTimeout(function () { window.close(); }, 500);
  }

  function select(btn) {
    var rel = btn.getAttribute("data-path") || "";
    var name = btn.getAttribute("data-name") || rel;
    if (!pickerMode) {
      // Plain browse mode: selecting opens the file (download bridge).
      window.location.href = "/api/cloud/file?path=" + encodeURIComponent(rel);
      return;
    }
    say("…");
    fetch("/api/cloud/share?path=" + encodeURIComponent(rel), { method: "POST" })
      .then(function (r) {
        if (!r.ok) return failText(r);
        return r.json();
      })
      .then(function (d) {
        handback(d.url || "/api/cloud/file?path=" + encodeURIComponent(rel), name);
      })
      .catch(function () {
        // Share minting failed (OCS unavailable): still hand something
        // same-origin back so the host can proceed.
        handback("/api/cloud/file?path=" + encodeURIComponent(rel), name);
      });
  }

  // ── grid events (delegated — covers SSR rows too, before re-render) ──
  grid.addEventListener("click", function (ev) {
    var t = ev.target;
    if (!t || !t.closest) return;
    var dirLink = t.closest("a.cf-main");
    if (dirLink && grid.contains(dirLink)) {
      ev.preventDefault(); // client-side navigation: fetch + re-render
      current = pathFromSearch("?" + (dirLink.search || ""));
      load();
      return;
    }
    var del = t.closest(".cf-del");
    if (del && grid.contains(del)) {
      ev.preventDefault();
      var rel = del.getAttribute("data-path") || "";
      if (!rel) return;
      if (!window.confirm("„" + (del.getAttribute("data-name") || rel) + "“ löschen?")) return;
      fetch("/api/cloud/file?path=" + encodeURIComponent(rel), { method: "DELETE" })
        .then(function (r) {
          if (r.status === 401) throw new Error("Sitzung abgelaufen");
          if (r.status !== 204) return failText(r);
          load();
        })
        .catch(function (err) { say("Nicht gelöscht: " + err.message); });
      return;
    }
    var sel = t.closest(".cf-select");
    if (sel && grid.contains(sel)) {
      ev.preventDefault();
      select(sel);
    }
  });

  // Browser back/forward between deep-linked directories.
  window.addEventListener("popstate", function () {
    current = pathFromSearch(window.location.search);
    load();
  });

  // ── upload (raw-body PUT, same pattern as intercom/polls) ────────────
  fileInput.addEventListener("change", function () {
    var f = fileInput.files && fileInput.files[0];
    fileName.textContent = f ? f.name + " (" + humanSize(f.size) + ")" : "";
  });

  uploadBtn.addEventListener("click", function () {
    var f = fileInput.files && fileInput.files[0];
    if (!f) {
      say("Bitte zuerst eine Datei auswählen.");
      return;
    }
    say("Lade hoch…");
    fetch(
      "/api/cloud/files?path=" + encodeURIComponent(current) +
      "&name=" + encodeURIComponent(f.name),
      {
        method: "PUT",
        headers: { "Content-Type": f.type || "application/octet-stream" },
        body: f,
      }
    )
      .then(function (r) {
        if (r.status === 401) throw new Error("Sitzung abgelaufen");
        if (!r.ok) return failText(r);
        fileInput.value = "";
        fileName.textContent = "";
        say("Hochgeladen: " + f.name, true);
        if (msg) msg.classList.add("is-ok");
        load();
      })
      .catch(function (err) { say("Upload fehlgeschlagen: " + err.message); });
  });

  // ── mkdir ────────────────────────────────────────────────────────────
  mkdirBtn.addEventListener("click", function () {
    var name = (mkdirInput.value || "").trim();
    if (!name || name.indexOf("/") !== -1 || name === "." || name === "..") {
      say("Bitte einen gültigen Ordnernamen angeben.");
      return;
    }
    say("Lege Ordner an…");
    fetch(
      "/api/cloud/files?path=" + encodeURIComponent(current) +
      "&name=" + encodeURIComponent(name),
      { method: "POST" }
    )
      .then(function (r) {
        if (r.status === 401) throw new Error("Sitzung abgelaufen");
        if (r.status === 409) throw new Error("Ordner existiert bereits");
        if (!r.ok) return failText(r);
        mkdirInput.value = "";
        say("Ordner angelegt: " + name, true);
        load();
      })
      .catch(function (err) { say("Ordner nicht angelegt: " + err.message); });
  });
  mkdirInput.addEventListener("keydown", function (ev) {
    if (ev.key === "Enter") {
      ev.preventDefault();
      mkdirBtn.click();
    }
  });

  // ── footer ───────────────────────────────────────────────────────────
  if (doneBtn) {
    doneBtn.addEventListener("click", function () {
      // window.close() works for script-opened tabs (the picker is
      // opened via window.open); otherwise back to the portal.
      if (!window.close()) window.location.href = "/";
    });
  }

  // First paint is SSR; this refreshes the list through the bridge and
  // binds the JS-only affordances for the current directory.
  load();
})();
