// Runs in <head> before the page paints, so a saved dark theme never flashes white first.
(function () {
  var t = "light";
  try { t = localStorage.getItem("theme") || "light"; } catch (e) { /* private mode: stay light */ }
  document.documentElement.setAttribute("data-theme", t === "dark" ? "dark" : "light");
})();
