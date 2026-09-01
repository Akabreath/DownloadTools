/* ===== cosplayD 前端逻辑 ===== */
"use strict";

const $ = (id) => document.getElementById(id);

// 自动推断挂载前缀: 页面在 /cosplayd/ 下时 BASE=/cosplayd, 独立运行时为空
const _seg = window.location.pathname.split("/").filter(Boolean);
const BASE = _seg.length ? "/" + _seg[0] : "";

const state = {
  images: [],
  taskId: null,
  pollTimer: null,
  lbIndex: 0,
  fmt: "zip",      // zip | files
  dest: "server",  // server | browser
};

/* ---------- 工具 ---------- */
function fmtBytes(n) {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return n.toFixed(i === 0 ? 0 : 1) + " " + units[i];
}
function fmtTime(sec) {
  if (sec < 60) return sec.toFixed(0) + " 秒";
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return m + " 分 " + s + " 秒";
}
function setStatus(el, msg, cls) {
  el.textContent = msg;
  el.className = "status-line" + (cls ? " " + cls : "");
}
function proxyImg(url) {
  return BASE + "/api/img?url=" + encodeURIComponent(url);
}

/* ---------- 服务器状态 ---------- */
async function checkHealth() {
  try {
    const r = await fetch(BASE + "/api/health");
    const ok = r.ok && (await r.json()).status === "ok";
    $("srvDot").className = "dot " + (ok ? "ok" : "err");
    $("srvText").textContent = ok ? "服务运行中" : "服务异常";
  } catch (e) {
    $("srvDot").className = "dot err";
    $("srvText").textContent = "无法连接";
  }
}

/* ---------- 解析 ---------- */
async function parsePage() {
  const url = $("urlInput").value.trim();
  if (!url) { setStatus($("parseStatus"), "请输入链接", "err"); return; }
  if (!/^https?:\/\//i.test(url)) { setStatus($("parseStatus"), "链接必须以 http(s):// 开头", "err"); return; }

  const btn = $("parseBtn");
  btn.disabled = true;
  btn.textContent = "⏳ 解析中…";
  setStatus($("parseStatus"), "正在抓取页面…");

  try {
    const r = await fetch(BASE + "/api/parse", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || "解析失败");

    state.images = data.images;
    $("pageTitle").textContent = data.title || "(无标题)";
    $("statCount").textContent = data.count;
    $("statSize").textContent = data.pages > 1 ? "共 " + data.pages + " 页 · 已自动合并" : "单页内容";
    setStatus($("parseStatus"), "✅ 找到 " + data.count + " 张图片" + (data.pages > 1 ? "（已合并 " + data.pages + " 个分页）" : ""), "ok");
    $("resultSection").classList.remove("hidden");
    renderGrid();
    resetProgress();
    $("resultSection").scrollIntoView({ behavior: "smooth" });
  } catch (e) {
    setStatus($("parseStatus"), "❌ " + e.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = "🔍 解析页面";
  }
}

/* ---------- 预览网格 ---------- */
function renderGrid() {
  const grid = $("grid");
  grid.innerHTML = "";
  state.images.forEach((u, i) => {
    const item = document.createElement("div");
    item.className = "grid-item loading";
    item.dataset.idx = i;

    const idx = document.createElement("span");
    idx.className = "idx";
    idx.textContent = i + 1;
    item.appendChild(idx);

    const img = document.createElement("img");
    img.loading = "lazy";
    img.alt = "预览 " + (i + 1);
    img.onload = () => item.classList.remove("loading");
    img.onerror = () => {
      item.classList.remove("loading");
      img.style.opacity = "0.25";
    };
    img.src = proxyImg(u);
    item.appendChild(img);

    item.onclick = () => openLightbox(i);
    grid.appendChild(item);
  });
  $("gridCount").textContent = "共 " + state.images.length + " 张 · 点击缩略图可放大预览";
  $("grid").style.display = $("showThumb").checked ? "" : "none";
  $("gridCount").style.display = $("showThumb").checked ? "" : "none";
}

/* ---------- 灯箱 ---------- */
function openLightbox(i) {
  state.lbIndex = i;
  updateLb();
  $("lightbox").classList.remove("hidden");
  document.body.style.overflow = "hidden";
}
function closeLb() {
  $("lightbox").classList.add("hidden");
  document.body.style.overflow = "";
}
function updateLb() {
  const img = $("lbImg");
  img.src = proxyImg(state.images[state.lbIndex]);
  $("lbIdx").textContent = (state.lbIndex + 1) + " / " + state.images.length;
}
function lbStep(d) {
  state.lbIndex = (state.lbIndex + d + state.images.length) % state.images.length;
  updateLb();
}

/* ---------- 下载 ---------- */
async function startDownload() {
  if (!state.images.length) return;
  const btn = $("dlBtn");
  btn.disabled = true;
  btn.textContent = "⏳ 创建任务…";
  setStatus($("dlStatus"), "");

  const body = {
    url: $("urlInput").value.trim(),
    title: $("pageTitle").textContent.replace(/^\(无标题\)$/, "") || "",
    images: state.images,
    need_zip: state.fmt === "zip",
  };

  try {
    const r = await fetch(BASE + "/api/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || "创建任务失败");
    state.taskId = data.task_id;
    $("progressPanel").classList.remove("hidden");
    $("progActions").classList.add("hidden");
    $("progErrors").classList.add("hidden");
    setStatus($("dlStatus"), "任务已创建，开始下载…", "ok");
    startPoll();
  } catch (e) {
    setStatus($("dlStatus"), "❌ " + e.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = "⬇ 开始下载";
  }
}

function startPoll() {
  stopPoll();
  state.pollTimer = setInterval(pollTask, 1000);
}
function stopPoll() {
  if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
}

async function pollTask() {
  if (!state.taskId) return;
  try {
    const r = await fetch(BASE + "/api/tasks/" + state.taskId);
    if (!r.ok) throw new Error("任务不存在");
    const t = await r.json();
    updateProgress(t);

    if (t.status === "done") {
      stopPoll();
      onTaskDone(t);
    }
  } catch (e) {
    stopPoll();
    setStatus($("dlStatus"), "❌ 轮询失败: " + e.message, "err");
  }
}

function updateProgress(t) {
  const phaseMap = {
    queued: "排队中…", downloading: "⬇ 下载图片中…", packing: "📦 打包 ZIP 中…",
    done: "✅ 完成", error: "❌ 失败",
  };
  $("progPhase").textContent = phaseMap[t.phase] || t.phase;
  $("progPct").textContent = t.percent.toFixed(1) + "%";
  $("progFill").style.width = t.percent + "%";
  $("progDetail").textContent = t.done + " / " + t.total;
  $("progElapsed").textContent = "耗时 " + fmtTime(t.elapsed || 0);
  if (t.total_bytes) $("progSpeed").textContent = "已下载 " + fmtBytes(t.total_bytes);

  if (t.errors && t.errors.length) {
    const box = $("progErrors");
    box.classList.remove("hidden");
    box.textContent = "⚠ " + t.errors.length + " 张失败: " + t.errors.map(e => e.error).join("; ").slice(0, 300);
  }
}

function onTaskDone(t) {
  setStatus($("dlStatus"),
    "✅ 完成: " + t.ok_count + " 张成功" + (t.fail_count ? "，" + t.fail_count + " 张失败" : ""),
    t.fail_count ? "err" : "ok");

  const actions = $("progActions");
  actions.classList.remove("hidden");

  const zipLink = $("zipLink");

  // ZIP 格式
  if (t.zip_path) {
    zipLink.href = BASE + "/api/tasks/" + t.id + "/zip";
    zipLink.classList.remove("hidden");
  } else {
    zipLink.classList.add("hidden");
  }

  // 浏览器直接下载图片文件 (逐张触发)
  if (state.fmt === "files" && state.dest === "browser") {
    triggerBatchDownload(t);
  }

  loadHistory();
}

function triggerBatchDownload(t) {
  // 逐个触发浏览器下载 (浏览器可能询问权限)
  const start = async () => {
    const r = await fetch(BASE + "/api/tasks/" + t.id + "/browse");
    const data = await r.json();
    if (!data.files) return;
    data.files.forEach((f, i) => {
      setTimeout(() => {
        const a = document.createElement("a");
        a.href = BASE + "/api/tasks/" + t.id + "/files/" + encodeURIComponent(f.name);
        a.download = f.name;
        document.body.appendChild(a);
        a.click();
        a.remove();
      }, i * 400);
    });
    setStatus($("dlStatus"), "✅ 已触发 " + data.files.length + " 个文件下载（浏览器可能要求允许批量下载）", "ok");
  };
  start();
}

function resetProgress() {
  $("progressPanel").classList.add("hidden");
  $("progFill").style.width = "0%";
  $("progPct").textContent = "0%";
  $("progPhase").textContent = "准备中…";
  $("progDetail").textContent = "0 / 0";
  $("progSpeed").textContent = "";
  $("progElapsed").textContent = "";
  $("progErrors").classList.add("hidden");
  stopPoll();
}

/* ---------- 历史 ---------- */
async function loadHistory() {
  try {
    const r = await fetch(BASE + "/api/tasks");
    const data = await r.json();
    const list = $("historyList");
    const items = data.history || [];
    if (!items.length) {
      list.innerHTML = '<p class="empty">暂无记录</p>';
      return;
    }
    list.innerHTML = "";
    items.forEach(h => {
      const div = document.createElement("div");
      div.className = "history-item";

      const main = document.createElement("div");
      main.className = "his-main";
      const title = document.createElement("div");
      title.className = "his-title";
      title.textContent = h.title || "(无标题)";
      const meta = document.createElement("div");
      meta.className = "his-meta";
      const when = h.finished_at ? new Date(h.finished_at * 1000).toLocaleString("zh-CN") : "";
      meta.textContent = `${when} · ${h.ok_count} 张 · ${fmtBytes(h.total_bytes || 0)} · ${fmtTime(h.elapsed || 0)}`;
      main.appendChild(title);
      main.appendChild(meta);
      div.appendChild(main);

      const acts = document.createElement("div");
      acts.className = "his-actions";
      if (h.zip_path) {
        const z = document.createElement("a");
        z.className = "btn btn-success";
        z.href = BASE + "/api/tasks/" + h.id + "/zip";
        z.download = "";
        z.textContent = "📦 ZIP";
        acts.appendChild(z);
      }
      div.appendChild(acts);
      list.appendChild(div);
    });
  } catch (e) { /* ignore */ }
}

async function clearHistory() {
  try {
    await fetch(BASE + "/api/history/clear", { method: "POST" });
    loadHistory();
  } catch (e) { /* ignore */ }
}

/* ---------- 事件绑定 ---------- */
function bindEvents() {
  $("parseBtn").onclick = parsePage;
  $("urlInput").addEventListener("keydown", (e) => { if (e.key === "Enter") parsePage(); });

  // 格式分段
  document.querySelectorAll("#fmtSeg .seg-btn").forEach(b => {
    b.onclick = () => {
      document.querySelectorAll("#fmtSeg .seg-btn").forEach(x => x.classList.remove("active"));
      b.classList.add("active");
      state.fmt = b.dataset.fmt;
    };
  });
  // 保存目标
  document.querySelectorAll("#destSeg .seg-btn").forEach(b => {
    b.onclick = () => {
      document.querySelectorAll("#destSeg .seg-btn").forEach(x => x.classList.remove("active"));
      b.classList.add("active");
      state.dest = b.dataset.dest;
    };
  });

  $("dlBtn").onclick = startDownload;
  $("historyClear").onclick = clearHistory;

  // 灯箱
  $("lbClose").onclick = closeLb;
  $("lbPrev").onclick = () => lbStep(-1);
  $("lbNext").onclick = () => lbStep(1);
  $("lightbox").addEventListener("click", (e) => {
    if (e.target === $("lightbox")) closeLb();
  });
  document.addEventListener("keydown", (e) => {
    if ($("lightbox").classList.contains("hidden")) return;
    if (e.key === "Escape") closeLb();
    if (e.key === "ArrowLeft") lbStep(-1);
    if (e.key === "ArrowRight") lbStep(1);
  });

  // 缩略图开关
  $("showThumb").onchange = (e) => {
    $("grid").style.display = e.target.checked ? "" : "none";
    $("gridCount").style.display = e.target.checked ? "" : "none";
  };
}

/* ---------- 启动 ---------- */
document.addEventListener("DOMContentLoaded", () => {
  bindEvents();
  checkHealth();
  setInterval(checkHealth, 15000);
  loadHistory();
});
