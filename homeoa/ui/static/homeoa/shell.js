// Board shell behaviour: desktop More menu, phone More sheet, theme toggle.
(function () {
  "use strict";
  var moreBtn = document.getElementById("moreBtn");
  var menu = document.getElementById("moreMenu");
  if (moreBtn && menu) {
    var setMenu = function (open) {
      menu.classList.toggle("open", open);
      moreBtn.setAttribute("aria-expanded", open ? "true" : "false");
    };
    moreBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      setMenu(!menu.classList.contains("open"));
    });
    document.addEventListener("click", function () { setMenu(false); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape") setMenu(false); });
  }

  var wrap = document.getElementById("sheetWrap");
  var sheet = document.getElementById("sheet");
  var tabMore = document.getElementById("tabMore");
  if (wrap && sheet && tabMore) {
    var setSheet = function (open) {
      if (open) sheet.hidden = false;
      // Force layout so the slide-in transition runs after un-hiding.
      void sheet.offsetWidth;
      wrap.classList.toggle("sheet-open", open);
      if (open) {
        var first = sheet.querySelector("a");
        if (first) first.focus();
      } else {
        setTimeout(function () { if (!wrap.classList.contains("sheet-open")) sheet.hidden = true; }, 250);
      }
    };
    tabMore.addEventListener("click", function (e) { e.preventDefault(); setSheet(true); });
    document.getElementById("sheetBg").addEventListener("click", function () { setSheet(false); });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && wrap.classList.contains("sheet-open")) { setSheet(false); tabMore.focus(); }
    });
  }

  // htmx live-region announcements (WCAG 4.1.3). A swapped fragment can name what
  // changed with data-announce="..."; otherwise a generic "Updated" is read out when the
  // swap carried no messages of its own. Errors are always announced.
  var announcer = document.getElementById("sr-announce");
  var say = function (text) {
    if (!announcer || !text) return;
    announcer.textContent = "";
    setTimeout(function () { announcer.textContent = text; }, 50);
  };
  document.body.addEventListener("htmx:afterSettle", function (e) {
    var target = e.detail && e.detail.target;
    if (!target) return;
    var marked = target.matches("[data-announce]") ? target : target.querySelector("[data-announce]");
    if (marked) { say(marked.getAttribute("data-announce")); return; }
    var msgs = document.getElementById("messages");
    if (msgs && msgs.textContent.trim()) return;  // #messages is its own live region
    if (target.closest && target.closest("main")) say(document.body.dataset.announceUpdated);
  });
  ["htmx:responseError", "htmx:sendError"].forEach(function (name) {
    document.body.addEventListener(name, function () { say(document.body.dataset.announceError); });
  });

  // PWA (PLT-09): offline page only; see ui/sw.js.
  if ("serviceWorker" in navigator && document.body.dataset.sw) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register(document.body.dataset.sw, { scope: "/" }).catch(function () {});
    });
  }

  document.querySelectorAll("[data-theme-toggle]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var root = document.documentElement;
      var dark = root.dataset.theme
        ? root.dataset.theme === "dark"
        : window.matchMedia("(prefers-color-scheme: dark)").matches;
      root.dataset.theme = dark ? "light" : "dark";
      try { localStorage.setItem("homeoa-theme", root.dataset.theme); } catch (e) {}
    });
  });

  // A slider (data-mirror="<field id>") that moves with a number box. The number box is
  // the form field; the slider and its label stay hidden without JavaScript.
  document.querySelectorAll("input[type=range][data-mirror]").forEach(function (slider) {
    var box = document.getElementById(slider.dataset.mirror);
    if (!box) return;
    (slider.closest("[data-slider]") || slider).hidden = false;
    slider.addEventListener("input", function () { box.value = slider.value; });
    box.addEventListener("input", function () { if (box.value !== "") slider.value = box.value; });
  });
})();
