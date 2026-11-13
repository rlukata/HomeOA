// Apply a saved light/dark preference before first paint. Default follows the OS.
try {
  var t = localStorage.getItem("homeoa-theme");
  if (t) document.documentElement.dataset.theme = t;
} catch (e) {}

// Select the whole value of read-only fields marked data-select-on-click (the CSP
// forbids inline on* handlers).
document.addEventListener("click", function (e) {
  var el = e.target.closest && e.target.closest("[data-select-on-click]");
  if (el && el.select) el.select();
});

// A table too wide for its wrapper scrolls sideways. Make the wrapper a tab stop while it
// scrolls, so the arrow keys can reach the hidden columns (WCAG 2.1.1).
(function () {
  var mark = function () {
    document.querySelectorAll(".table-wrap").forEach(function (wrap) {
      if (wrap.scrollWidth > wrap.clientWidth) wrap.setAttribute("tabindex", "0");
      else wrap.removeAttribute("tabindex");
    });
  };
  document.addEventListener("DOMContentLoaded", mark);
  document.addEventListener("htmx:afterSettle", mark);
  window.addEventListener("resize", mark);
})();
