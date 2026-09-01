/* ===== DownloadTools 门户逻辑 ===== */
"use strict";

const $ = (id) => document.getElementById(id);

async function checkHealth() {
  try {
    const r = await fetch("api/tools");
    const ok = r.ok;
    $("srvDot").className = "dot " + (ok ? "ok" : "err");
    $("srvText").textContent = ok ? "服务运行中" : "服务异常";
  } catch (e) {
    $("srvDot").className = "dot err";
    $("srvText").textContent = "无法连接";
  }
}

async function loadTools() {
  try {
    const r = await fetch("api/tools");
    const data = await r.json();
    renderTools(data.tools || []);
  } catch (e) {
    $("toolGrid").innerHTML = '<p class="err-msg">加载工具列表失败: ' + e.message + "</p>";
  }
}

function renderTools(tools) {
  const grid = $("toolGrid");
  grid.innerHTML = "";
  tools.forEach((t, i) => {
    const card = document.createElement("a");
    card.className = "tool-card glass";
    card.href = t.path;
    card.style.animationDelay = (i * 0.08) + "s";

    const icon = document.createElement("div");
    icon.className = "tool-icon";
    icon.textContent = t.icon || "🔧";

    const name = document.createElement("div");
    name.className = "tool-name";
    name.textContent = t.name;

    const desc = document.createElement("div");
    desc.className = "tool-desc";
    desc.textContent = t.desc;

    const tags = document.createElement("div");
    tags.className = "tool-tags";
    (t.tags || []).forEach(tag => {
      const s = document.createElement("span");
      s.className = "tag";
      s.textContent = tag;
      tags.appendChild(s);
    });

    const foot = document.createElement("div");
    foot.className = "tool-foot";
    const status = document.createElement("span");
    status.className = "tool-status";
    status.innerHTML = '<span class="dot ok"></span> 可用';
    const enter = document.createElement("span");
    enter.className = "tool-enter";
    enter.textContent = "打开工具 →";
    foot.appendChild(status);
    foot.appendChild(enter);

    card.appendChild(icon);
    card.appendChild(name);
    card.appendChild(desc);
    card.appendChild(tags);
    card.appendChild(foot);
    grid.appendChild(card);
  });
}

document.addEventListener("DOMContentLoaded", () => {
  checkHealth();
  setInterval(checkHealth, 15000);
  loadTools();
});
