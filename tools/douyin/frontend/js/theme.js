/* ===== 亮/暗/跟随系统 主题切换 ===== */
(function () {
  "use strict";

  var LS_KEY = "dt-theme";
  var mq = window.matchMedia("(prefers-color-scheme: dark)");

  function getPref() {
    try { return localStorage.getItem(LS_KEY) || "auto"; }
    catch (e) { return "auto"; }
  }

  function setPref(p) {
    try { localStorage.setItem(LS_KEY, p); } catch (e) {}
  }

  function isDark(p) {
    if (p === "light") return false;
    if (p === "dark") return true;
    return !!mq.matches; // auto
  }

  function applyTheme(p) {
    var dark = isDark(p);
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    var btn = document.getElementById("themeBtn");
    if (btn) {
      btn.textContent = p === "auto" ? "💻" : (dark ? "🌙" : "☀️");
      btn.title = p === "auto" ? "主题：跟随系统" : (dark ? "主题：暗色" : "主题：亮色");
    }
    var menu = document.getElementById("themeMenu");
    if (menu) {
      var opts = menu.querySelectorAll("button[data-theme]");
      for (var i = 0; i < opts.length; i++) {
        opts[i].classList.toggle("active", opts[i].getAttribute("data-theme") === p);
      }
    }
  }

  function switchTheme(p) {
    setPref(p);
    applyTheme(p);
    closeMenu();
  }

  function openMenu() {
    var menu = document.getElementById("themeMenu");
    if (menu) { menu.hidden = false; }
  }

  function closeMenu() {
    var menu = document.getElementById("themeMenu");
    if (menu) { menu.hidden = true; }
  }

  document.addEventListener("click", function (e) {
    var wrap = document.querySelector(".theme-wrap");
    if (!wrap || !wrap.contains(e.target)) { closeMenu(); }
  });

  var btn = document.getElementById("themeBtn");
  if (btn) {
    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      var menu = document.getElementById("themeMenu");
      if (menu && menu.hidden) { openMenu(); } else { closeMenu(); }
    });
  }

  var menu = document.getElementById("themeMenu");
  if (menu) {
    menu.addEventListener("click", function (e) {
      var t = e.target && e.target.closest ? e.target.closest("button[data-theme]") : null;
      if (t) { switchTheme(t.getAttribute("data-theme")); }
    });
  }

  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") { closeMenu(); }
  });

  // 跟随系统: 系统主题变化时自动切换
  if (mq.addEventListener) {
    mq.addEventListener("change", function () {
      if (getPref() === "auto") { applyTheme("auto"); }
    });
  } else if (mq.addListener) {
    mq.addListener(function () {
      if (getPref() === "auto") { applyTheme("auto"); }
    });
  }

  applyTheme(getPref());
})();
