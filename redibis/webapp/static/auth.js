/* Attach CSRF header on mutating fetch() so existing dashboard JS keeps working. */
(function () {
  function csrfToken() {
    var match = document.cookie.split("; ").find(function (c) {
      return c.indexOf("redibis_csrf=") === 0;
    });
    if (!match) return String(window.REDIBIS_CSRF || "");
    return decodeURIComponent(match.split("=").slice(1).join("="));
  }
  var orig = window.fetch;
  window.fetch = function (input, init) {
    init = init || {};
    var method = String(init.method || "GET").toUpperCase();
    if (["POST", "PUT", "PATCH", "DELETE"].indexOf(method) !== -1) {
      var headers = new Headers(init.headers || {});
      if (!headers.has("X-CSRF-Token")) {
        headers.set("X-CSRF-Token", csrfToken());
      }
      init.headers = headers;
    }
    if (!init.credentials) init.credentials = "same-origin";
    return orig.call(this, input, init);
  };
})();
