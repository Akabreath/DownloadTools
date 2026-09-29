/* ===== 悬浮「一键滑动到结尾」按钮 =====
   向下滚动 (>200px 且未到底) 显示; 向上滚动立即隐藏; 已在页面底部时自动隐藏(不遮挡内容) */
(function () {
  "use strict";
  var BTN_ID = "scrollBottomBtn";
  var btn = null;
  var lastY = window.pageYOffset || 0;
  var ticking = false;

  function nearBottom() {
    var doc = document.documentElement;
    return (window.innerHeight + window.pageYOffset) >= (doc.scrollHeight - 80);
  }

  function update() {
    ticking = false;
    if (!btn) return;
    var y = window.pageYOffset;
    var diff = y - lastY;
    if (Math.abs(diff) < 6) return;
    if (diff > 0 && y > 200 && !nearBottom()) btn.classList.add("show");
    else btn.classList.remove("show");
    lastY = y;
  }

  function onScroll() {
    if (!ticking) {
      ticking = true;
      window.requestAnimationFrame(update);
    }
  }

  function build() {
    var b = document.createElement("button");
    b.id = BTN_ID;
    b.type = "button";
    b.className = "scroll-bottom-btn";
    b.title = "一键滑动到结尾";
    b.setAttribute("aria-label", "一键滑动到结尾");
    b.textContent = "↓";
    b.addEventListener("click", function () {
      window.scrollTo({ top: document.documentElement.scrollHeight, behavior: "smooth" });
    });
    document.body.appendChild(b);
    return b;
  }

  function init() {
    if (document.getElementById(BTN_ID)) return;
    btn = build();
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", onScroll, { passive: true });
    lastY = window.pageYOffset || 0;
    update();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
