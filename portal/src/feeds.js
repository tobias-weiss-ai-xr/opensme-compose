// openSME portal — public news columns (served at /feeds.js, CSP: script-src 'self').
//
// Server-side RSS/Atom aggregation happens in the portal (/api/feeds), so the
// browser stays same-origin (CSP connect-src 'self'). This script fills every
// `#feed-<id>` list on the landing page — INCLUDING the anonymous public
// landing, where the full app.js (signed-in chrome: tabs, intercom, polls,
// AI, cloud) is deliberately NOT loaded (contracts/tiles.yml: anonymous
// landing = login CTA + public news). Feed titles are untrusted input and
// are written with textContent only.
(function () {
  "use strict";

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
