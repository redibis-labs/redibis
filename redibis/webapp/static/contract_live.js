// Live contract: every page that shows a contract calls redibisContractLive.watch(...)
// and is told at once when the contract changes — whoever changed it.
//  - server-sent events from /api/contracts/{table}/events (another user, the CLI, a
//    share link …), falling back to polling /head when the stream is unavailable;
//  - a BroadcastChannel, so other tabs of this browser update the moment you save.
(function () {
  "use strict";
  var CHANNEL = "redibis-contract";
  var chan = null;
  try { chan = ("BroadcastChannel" in window) ? new BroadcastChannel(CHANNEL) : null; } catch (_) { chan = null; }

  function withWs(path, ws) {
    return ws && ws !== "default" ? path + (path.indexOf("?") >= 0 ? "&" : "?") + "ws=" + encodeURIComponent(ws) : path;
  }

  // watch(table, onChange, {ws, eventsUrl, headUrl}) → stop()
  // onChange(head) runs once per new version (not for the version seen first).
  function watch(table, onChange, opts) {
    opts = opts || {};
    var base = "/api/contracts/" + encodeURIComponent(table);
    var eventsUrl = opts.eventsUrl || withWs(base + "/events", opts.ws);
    var headUrl = opts.headUrl || withWs(base + "/head", opts.ws);
    var known = null, stopped = false, es = null, poll = null;

    function seen(head) {
      if (stopped || !head) return;
      var ver = head.version != null ? head.version : (head.contract || {}).version;
      var v = ver == null ? "" : String(ver);
      if (known === null) { known = v; return; }
      if (v !== known) { known = v; try { onChange(head); } catch (e) { console.error(e); } }
    }
    function checkHead() {
      return fetch(headUrl, { credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(seen).catch(function () {});
    }
    function startPolling() {
      if (poll || stopped) return;
      poll = setInterval(function () { if (!document.hidden) checkHead(); }, 5000);
    }
    if ("EventSource" in window) {
      try {
        es = new EventSource(eventsUrl);
        es.addEventListener("contract", function (ev) {
          try { seen(JSON.parse(ev.data)); } catch (_) {}
        });
        es.onerror = function () { if (es && es.readyState === 2) startPolling(); };
      } catch (_) { startPolling(); }
    } else startPolling();

    function onMessage(ev) {
      var d = ev && ev.data;
      if (d && d.table === table) checkHead();
    }
    if (chan) chan.addEventListener("message", onMessage);

    return function stop() {
      stopped = true;
      if (es) es.close();
      if (poll) clearInterval(poll);
      if (chan) chan.removeEventListener("message", onMessage);
    };
  }

  // Tell the other tabs of this browser that `table` changed (after a save here).
  var localWrites = {};
  function announce(table) {
    if (!table) return;
    localWrites[table] = Date.now();
    if (chan) { try { chan.postMessage({ table: table, at: Date.now() }); } catch (_) {} }
  }
  // True right after this page saved `table` — the change it hears about is its own.
  function savedHereRecently(table, ms) {
    return Date.now() - (localWrites[table] || 0) < (ms || 4000);
  }

  // The table a contract API path writes to, e.g. /api/contracts/db.t/columns/x/pii → db.t.
  function tableOfPath(path) {
    var m = /\/api\/contracts\/([^/?#]+)/.exec(String(path || ""));
    return m ? decodeURIComponent(m[1]) : "";
  }

  function isTyping() {
    var el = document.activeElement;
    return !!(el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT" || el.isContentEditable));
  }

  window.redibisContractLive = { watch: watch, announce: announce, savedHereRecently: savedHereRecently,
                                 tableOfPath: tableOfPath, isTyping: isTyping };
})();
