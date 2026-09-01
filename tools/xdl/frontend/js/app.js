/* ===== xdl 前端逻辑 ===== */
"use strict";

const $ = (id) => document.getElementById(id);

// 自动推断挂载前缀
const _seg = window.location.pathname.split("/").filter(Boolean);
const BASE = _seg.length ? "/" + _seg[0] : "";

const state = {
  mode: "video",
  video: null,          // 视频解析结果
  mediaItems: [],       // 媒体条目
  screenName: "",       // 媒体页用户名
  pageTitle: "",        // 媒体页标题(显示名, 用于文件夹命名)
  selected: new Set(),  // 选中的媒体索引
  visibleIdx: [],       // 当前可见(筛选后)的索引
  dest2: "browser",     // 媒体下载目标: server | browser
  taskId: null,
  pollTimer: null,
  parseTaskId: null,    // 媒体抓取任务
  parseTimer: null,
};

/* ---------- 工具 ---------- */
function fmtBytes(n) {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(i === 0 ? 0 : 1) + " " + u[i];
}
function fmtTime(s) {
  if (s < 60) return s.toFixed(0) + " 秒";
  return Math.floor(s / 60) + " 分 " + Math.floor(s % 60) + " 秒";
}
function setStatus(el, msg, cls) {
  el.textContent = msg;
  el.className = "status-line" + (cls ? " " + cls : "");
}

/* ---------- 健康检查 ---------- */
async function checkHealth() {
  try {
    const r = await fetch(BASE + "/api/config");
    const ok = r.ok;
    $("srvDot").className = "dot " + (ok ? "ok" : "err");
    $("srvText").textContent = ok ? "服务运行中" : "服务异常";
  } catch (e) {
    $("srvDot").className = "dot err";
    $("srvText").textContent = "无法连接";
  }
}

/* ---------- 视频解析 ---------- */
async function parseVideo() {
  const url = $("vUrlInput").value.trim();
  if (!/^https?:\/\//i.test(url)) { setStatus($("vStatus"), "请输入合法链接", "err"); return; }
  const btn = $("vParseBtn");
  btn.disabled = true; btn.textContent = "⏳ 解析中…";
  setStatus($("vStatus"), "正在抓取页面…");
  try {
    const r = await fetch(BASE + "/api/parse_video", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "解析失败");
    state.video = d;
    $("vTitle").textContent = d.title || "视频";
    $("vThumb").src = d.thumb ? BASE + "/api/img?url=" + encodeURIComponent(d.thumb) : "";
    $("vThumb").style.display = d.thumb ? "" : "none";
    // 时长信息
    const meta = [];
    if (d.duration) meta.push("⏱ " + fmtDur(d.duration));
    if (d.auth_mode === "graphql") meta.push("🔒 GraphQL");
    else if (d.auth_mode === "anonymous") meta.push("🌐 匿名解析");
    $("vMeta").textContent = meta.join(" · ");
    const sel = $("vQuality");
    sel.innerHTML = "";
    d.variants.forEach((v, i) => {
      const opt = document.createElement("option");
      opt.value = i;
      const br = v.bitrate ? (v.bitrate / 1000).toFixed(0) + " kbps" : "best";
      const sz = v.size ? " · " + fmtBytes(v.size) : "";
      opt.textContent = `${v.label || "unknown"} · ${br}${sz}`;
      sel.appendChild(opt);
    });
    $("vResult").classList.remove("hidden");
    setStatus($("vStatus"), "✅ 找到 " + d.variants.length + " 个清晰度", "ok");
  } catch (e) {
    setStatus($("vStatus"), "❌ " + e.message, "err");
  } finally {
    btn.disabled = false; btn.textContent = "🔍 解析视频";
  }
}

function fmtDur(sec) {
  if (!sec) return "";
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  if (h) return h + ":" + String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
  return m + ":" + String(s).padStart(2, "0");
}

async function copyVideoLink() {
  if (!state.video) return;
  const idx = parseInt($("vQuality").value || "0", 10);
  const v = state.video.variants[idx];
  if (!v) return;
  try {
    await navigator.clipboard.writeText(v.url);
    const btn = $("vCopyBtn");
    btn.textContent = "✅ 已复制";
    setTimeout(() => { btn.textContent = "📋 复制链接"; }, 1500);
  } catch (e) {
    // 剪贴板不可用时降级
    const ta = document.createElement("textarea");
    ta.value = v.url;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
    const btn = $("vCopyBtn");
    btn.textContent = "✅ 已复制";
    setTimeout(() => { btn.textContent = "📋 复制链接"; }, 1500);
  }
}

async function downloadVideo() {
  if (!state.video) return;
  const idx = parseInt($("vQuality").value || "0", 10);
  const v = state.video.variants[idx];
  const title = state.video.title || "x_video_" + state.video.status_id;
  state.vdest = document.querySelector(".v-actions .seg-inline .seg-btn.active")?.dataset.vdest || "browser";
  if (state.vdest === "browser") {
    // 浏览器模式: 流式直连下载, 不落服务器磁盘
    const ext = ".mp4";
    const fname = sanitizeName(title) + ext;
    const url = BASE + "/api/stream?url=" + encodeURIComponent(v.url) + "&name=" + encodeURIComponent(fname);
    const a = document.createElement("a");
    a.href = url;
    a.download = fname;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setStatus($("vStatus"), "✅ 已触发下载（浏览器直连，不占服务器存储）", "ok");
    return;
  }
  const items = [{ url: v.url, kind: "video" }];
  await createTask(title, items, $("vZip").checked);
}

function sanitizeName(s) {
  return String(s || "").replace(/[\\/:*?"<>|\r\n\t]/g, "_").trim().slice(0, 60) || "x_video";
}

/* ---------- 媒体抓取 ---------- */
function normalizeMediaUrl(url) {
  // 兼容 x.com/user/media 与 x.com/user, 自动补全为 /media
  url = url.trim();
  let m = url.match(/^https?:\/\/(?:www\.)?(?:x\.com|twitter\.com)\/([A-Za-z0-9_]+)(?:\/media)?\/?$/);
  if (!m) return "";
  return "https://x.com/" + m[1] + "/media";
}

async function parseMedia() {
  let url = $("mUrlInput").value.trim();
  const norm = normalizeMediaUrl(url);
  if (!norm) { setStatus($("mStatus"), "请输入 x.com/用户名 或 x.com/用户名/media 链接", "err"); return; }
  $("mUrlInput").value = norm;  // 输入框回显补全后的链接
  const btn = $("mParseBtn");
  btn.disabled = true; btn.textContent = "⏳ 创建任务…";
  setStatus($("mStatus"), "正在创建抓取任务…");
  try {
    const r = await fetch(BASE + "/api/parse_media", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: norm }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "创建任务失败");
    state.parseTaskId = d.task_id;
    setStatus($("mStatus"), "任务已创建，正在抓取（可切走应用，任务会在服务器继续）…", "ok");
    startParsePoll();
  } catch (e) {
    setStatus($("mStatus"), "❌ " + e.message, "err");
  } finally {
    btn.disabled = false; btn.textContent = "🔍 抓取媒体";
  }
}

function startParsePoll() {
  stopParsePoll();
  state.parseTimer = setInterval(pollParseTask, 1500);
}
function stopParsePoll() {
  if (state.parseTimer) { clearInterval(state.parseTimer); state.parseTimer = null; }
}

async function pollParseTask() {
  if (!state.parseTaskId) return;
  try {
    const r = await fetch(BASE + "/api/parse_tasks/" + state.parseTaskId);
    if (!r.ok) throw new Error("任务不存在");
    const t = await r.json();
    if (t.status === "running") {
      setStatus($("mStatus"), "⏳ 抓取中… " + (t.phase === "fetching" ? "正在拉取媒体时间线" : "排队中"), "");
      return;
    }
    stopParsePoll();
    if (t.status === "error") {
      setStatus($("mStatus"), "❌ " + (t.error || "抓取失败"), "err");
      return;
    }
    // 完成
    state.mediaItems = t.items || [];
    state.screenName = t.screen_name || "";
    state.pageTitle = t.page_title || t.screen_name || "";
    state.selected = new Set();
    renderMediaGrid();
    $("mResult").classList.remove("hidden");
    setStatus($("mStatus"), "✅ 找到 " + t.count + " 条媒体", "ok");
    state.parseTaskId = null;
  } catch (e) {
    stopParsePoll();
    setStatus($("mStatus"), "❌ 轮询中断: " + e.message, "err");
  }
}

function renderMediaGrid() {
  const grid = $("mGrid");
  grid.innerHTML = "";
  // 筛选: 仅图片
  const onlyImg = $("mOnlyImg").checked;
  const visible = state.mediaItems
    .map((it, i) => ({ it, i }))
    .filter(({ it }) => !(onlyImg && it.type === "video"));
  state.visibleIdx = visible.map(v => v.i);
  $("mCount").textContent = state.mediaItems.length + " 条" +
    (onlyImg ? "（显示图片 " + visible.length + "）" : "");
  visible.forEach(({ it: item, i }) => {
    const div = document.createElement("div");
    div.className = "m-item" + (item.type === "video" ? " video-item" : "");
    div.dataset.idx = i;

    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = item.type === "video" ? "🎬 视频" : "🖼 图片";
    div.appendChild(tag);

    if (item.type === "video") {
      // 视频: 封面图 (GraphQL media_url_https) + 播放角标
      const img = document.createElement("img");
      img.loading = "lazy";
      img.alt = "媒体 " + (i + 1);
      if (item.thumb) {
        img.src = BASE + "/api/img?url=" + encodeURIComponent(item.thumb);
      } else {
        img.style.opacity = "0.15";
        img.alt = "无封面";
      }
      img.onerror = () => { img.style.opacity = "0.15"; };
      div.appendChild(img);
      const lbl = document.createElement("div");
      lbl.style.cssText = "position:absolute;bottom:6px;left:6px;right:6px;font-size:10px;color:#fff;background:rgba(0,0,0,0.6);padding:4px 8px;border-radius:6px;word-break:break-all;";
      lbl.textContent = "tweet " + (item.tweet_id || "");
      div.appendChild(lbl);
    } else {
      const img = document.createElement("img");
      img.loading = "lazy";
      img.alt = "媒体 " + (i + 1);
      if (item.thumb) {
        img.src = BASE + "/api/img?url=" + encodeURIComponent(item.thumb);
      } else {
        img.style.opacity = "0.15";
        img.alt = "无缩略图";
      }
      img.onerror = () => { img.style.opacity = "0.15"; };
      div.appendChild(img);
    }

    const mark = document.createElement("span");
    mark.className = "sel-mark";
    mark.textContent = "✓";
    div.appendChild(mark);

    div.onclick = () => {
      if (state.selected.has(i)) state.selected.delete(i);
      else state.selected.add(i);
      div.classList.toggle("selected", state.selected.has(i));
      $("mDlBtn").disabled = state.selected.size === 0;
    };
    grid.appendChild(div);
  });
  $("mDlBtn").disabled = state.selected.size === 0;
  updateSelectAllBtn();
}

function selectAll() {
  const onlyImg = $("mOnlyImg").checked;
  if (state.selected.size > 0 && state.selected.size >= state.visibleIdx.length) {
    // 已全选 -> 取消
    state.selected.clear();
  } else {
    state.visibleIdx.forEach(i => {
      const item = state.mediaItems[i];
      if (onlyImg && item.type === "video") return;
      state.selected.add(i);
    });
  }
  // 更新 UI
  document.querySelectorAll(".m-item").forEach(div => {
    const i = parseInt(div.dataset.idx, 10);
    div.classList.toggle("selected", state.selected.has(i));
  });
  $("mDlBtn").disabled = state.selected.size === 0;
  updateSelectAllBtn();
}

function updateSelectAllBtn() {
  const btn = $("mSelectAll");
  if (state.selected.size > 0 && state.visibleIdx.length > 0 &&
      state.selected.size >= state.visibleIdx.length) {
    btn.textContent = "☑ 取消全选";
  } else {
    btn.textContent = "☑ 全选";
  }
}

async function downloadMedia() {
  if (!state.selected.size) return;
  const sel = [...state.selected].sort((a, b) => a - b);
  const items = [];
  const btn = $("mDlBtn");
  btn.disabled = true; btn.textContent = "⏳ 准备下载…";
  setStatus($("mStatus"), "正在准备下载列表…");

  try {
    // 图片直接带 large URL, 视频直接带 mp4 URL (GraphQL 已给出)
    sel.forEach(i => {
      const item = state.mediaItems[i];
      if (!item.url) return;
      items.push({ url: item.url, kind: item.type });
    });

    if (!items.length) { setStatus($("mStatus"), "❌ 没有可下载的内容", "err"); return; }
    state.dest2 = document.querySelector(".seg-inline .seg-btn.active")?.dataset.dest2 || "browser";
    if (state.dest2 === "browser") {
      // 浏览器模式: 逐个流式直连下载, 不落服务器磁盘
      items.forEach((it, k) => {
        setTimeout(() => {
          const ext = it.kind === "video" ? ".mp4" : ".jpg";
          const fname = (it.kind === "video" ? "video_" : "img_") + (k + 1) + ext;
          const url = BASE + "/api/stream?url=" + encodeURIComponent(it.url) + "&name=" + encodeURIComponent(fname);
          const a = document.createElement("a");
          a.href = url;
          a.download = fname;
          document.body.appendChild(a);
          a.click();
          a.remove();
        }, k * 300);
      });
      setStatus($("mStatus"), "✅ 已触发 " + items.length + " 个文件下载（浏览器可能要求允许批量下载）", "ok");
      return;
    }
    // 文件夹名用页面标题 (显示名), 如 柚子气泡水✨
    const title = state.pageTitle || state.screenName || "x_media_" + Math.floor(Date.now() / 1000);
    await createTask(title, items, $("mZip").checked);
  } catch (e) {
    setStatus($("mStatus"), "❌ " + e.message, "err");
  } finally {
    btn.disabled = false; btn.textContent = "⬇ 下载选中";
  }
}

/* ---------- 下载任务 ---------- */
async function createTask(title, items, needZip) {
  const r = await fetch(BASE + "/api/download", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title, items, need_zip: needZip }),
  });
  const d = await r.json();
  if (!r.ok) throw new Error(d.detail || "创建任务失败");
  state.taskId = d.task_id;
  $("progressPanel").classList.remove("hidden");
  $("progActions").classList.add("hidden");
  $("progErrors").classList.add("hidden");
  setStatus($("mStatus"), "任务已创建，开始下载…", "ok");
  setStatus($("vStatus"), "任务已创建，开始下载…", "ok");
  startPoll();
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
    const phaseMap = {
      queued: "排队中…", downloading: "⬇ 下载中…", packing: "📦 打包中…",
      done: "✅ 完成", error: "❌ 失败",
    };
    $("progPhase").textContent = phaseMap[t.phase] || t.phase;
    $("progPct").textContent = t.percent.toFixed(1) + "%";
    $("progFill").style.width = t.percent + "%";
    $("progDetail").textContent = t.done + " / " + t.total;
    $("progElapsed").textContent = "耗时 " + fmtTime(t.elapsed || 0);
    if (t.errors && t.errors.length) {
      $("progErrors").classList.remove("hidden");
      $("progErrors").textContent = "⚠ " + t.errors.length + " 项失败: " +
        t.errors.map(e => e.error).join("; ").slice(0, 300);
    }
    if (t.status === "done") {
      stopPoll();
      $("progActions").classList.remove("hidden");
      const zipLink = $("zipLink");
      if (t.zip_path) {
        zipLink.href = BASE + "/api/tasks/" + t.id + "/zip";
        zipLink.classList.remove("hidden");
      } else {
        zipLink.classList.add("hidden");
      }
      loadHistory();
      setStatus($("mStatus"), "✅ 完成: " + t.ok_count + " 成功" + (t.fail_count ? "，" + t.fail_count + " 失败" : ""), t.fail_count ? "err" : "ok");
      // 浏览器下载模式: 完成后自动触发下载
      const wantBrowser = state.vdest === "browser" || state.dest2 === "browser";
      if (wantBrowser) {
        if (t.zip_path) {
          const a = document.createElement("a");
          a.href = BASE + "/api/tasks/" + t.id + "/zip";
          a.download = "";
          document.body.appendChild(a);
          a.click();
          a.remove();
          setStatus($("mStatus"), "✅ 完成，ZIP 已开始下载（浏览器可能询问保存位置）", "ok");
        } else {
          fetch(BASE + "/api/tasks/" + t.id + "/browse").then(r => r.json()).then(d => {
            if (!d.files) return;
            d.files.forEach((f, i) => {
              setTimeout(() => {
                const a = document.createElement("a");
                a.href = BASE + "/api/tasks/" + t.id + "/files/" + encodeURIComponent(f.name);
                a.download = f.name;
                document.body.appendChild(a);
                a.click();
                a.remove();
              }, i * 400);
            });
            setStatus($("mStatus"), "✅ 已触发 " + d.files.length + " 个文件下载（浏览器可能要求允许批量下载）", "ok");
          }).catch(() => {});
        }
      }
    }
  } catch (e) {
    stopPoll();
  }
}

/* ---------- Cookie 设置 ---------- */
async function loadCfg() {
  try {
    const r = await fetch(BASE + "/api/config");
    const d = await r.json();
    const a = d.analysis || {};
    let msg = d.has_cookie ? "已配置 Cookie：" + d.masked : "尚未配置 Cookie";
    if (a && d.has_cookie) {
      if (a.looks_logged_in) {
        msg += " ✅ 字段完整，看起来是登录态";
        $("cfgStatus").className = "status-line ok";
      } else {
        msg += " ⚠ 缺少关键字段: " + (a.missing || []).join(", ") +
               "。请复制浏览器里完整的 Cookie 字符串（应含 auth_token/ct0/twid/kdt 等）";
        $("cfgStatus").className = "status-line err";
      }
    } else {
      $("cfgStatus").className = "status-line" + (d.has_cookie ? " ok" : "");
    }
    $("cfgStatus").textContent = msg;
  } catch (e) { /* ignore */ }
}

function openCfg() {
  $("cfgModal").classList.remove("hidden");
  loadCfg();
}
function closeCfg() {
  $("cfgModal").classList.add("hidden");
}

async function saveCfg() {
  const cookie = $("cookieInput").value.trim();
  const r = await fetch(BASE + "/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cookie }),
  });
  if (r.ok) {
    setStatus($("cfgStatus"), "✅ 已保存 (" + cookie.length + " 字符)", "ok");
    $("cookieInput").value = "";
  } else {
    setStatus($("cfgStatus"), "❌ 保存失败", "err");
  }
  loadCfg();
}

async function clearCfg() {
  await fetch(BASE + "/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cookie: "" }),
  });
  $("cookieInput").value = "";
  setStatus($("cfgStatus"), "已清空", "");
  loadCfg();
}

/* ---------- 历史记录 ---------- */
async function loadHistory() {
  try {
    const r = await fetch(BASE + "/api/history");
    const d = await r.json();
    const list = $("historyList");
    const items = d.history || [];
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
      meta.textContent = `${when} · ${h.ok_count} 项 · ${fmtBytes(h.total_bytes || 0)}`;
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

/* ---------- 事件 ---------- */
function bindEvents() {
  document.querySelectorAll(".mode-btn").forEach(b => {
    b.onclick = () => {
      document.querySelectorAll(".mode-btn").forEach(x => x.classList.remove("active"));
      b.classList.add("active");
      state.mode = b.dataset.mode;
      $("videoPanel").classList.toggle("hidden", state.mode !== "video");
      $("mediaPanel").classList.toggle("hidden", state.mode !== "media");
    };
  });

  $("vParseBtn").onclick = parseVideo;
  $("vUrlInput").addEventListener("focus", function () { this.select(); });  // 点击自动全选
  $("vUrlInput").addEventListener("keydown", e => { if (e.key === "Enter") parseVideo(); });
  $("vDlBtn").onclick = downloadVideo;
  $("vCopyBtn").onclick = copyVideoLink;
  document.querySelectorAll(".v-actions .seg-inline .seg-btn").forEach(b => {
    b.onclick = () => {
      document.querySelectorAll(".v-actions .seg-inline .seg-btn").forEach(x => x.classList.remove("active"));
      b.classList.add("active");
      // 浏览器直连模式无 ZIP 打包, 禁用勾选
      $("vZip").disabled = b.dataset.vdest === "browser";
    };
  });

  $("mParseBtn").onclick = parseMedia;
  $("mUrlInput").addEventListener("focus", function () { this.select(); });  // 点击自动全选
  $("mUrlInput").addEventListener("keydown", e => { if (e.key === "Enter") parseMedia(); });
  $("mDlBtn").onclick = downloadMedia;
  $("mSelectAll").onclick = selectAll;
  $("historyClear").onclick = clearHistory;
  $("mOnlyImg").onchange = () => {
    state.selected.clear();
    renderMediaGrid();
  };
  document.querySelectorAll(".seg-inline .seg-btn").forEach(b => {
    b.onclick = () => {
      document.querySelectorAll(".seg-inline .seg-btn").forEach(x => x.classList.remove("active"));
      b.classList.add("active");
      // 浏览器直连模式无 ZIP 打包, 禁用勾选
      $("mZip").disabled = b.dataset.dest2 === "browser";
    };
  });

  $("cfgBtn").onclick = openCfg;
  $("cfgClose").onclick = closeCfg;
  $("cfgSave").onclick = saveCfg;
  $("cfgClear").onclick = clearCfg;
  $("cfgModal").addEventListener("click", e => { if (e.target === $("cfgModal")) closeCfg(); });
}

document.addEventListener("DOMContentLoaded", () => {
  bindEvents();
  checkHealth();
  setInterval(checkHealth, 15000);
  loadCfg();
  loadHistory();
  // 初始同步: 默认浏览器直连模式, ZIP 禁用
  $("vZip").disabled = document.querySelector(".v-actions .seg-inline .seg-btn.active")?.dataset.vdest === "browser";
  $("mZip").disabled = document.querySelector(".media-actions .seg-inline .seg-btn.active")?.dataset.dest2 === "browser";
});

// 手机切回应用/解锁时立即恢复轮询 (后台标签页 setInterval 会被浏览器降频, 切回后不等待)
document.addEventListener("visibilitychange", () => {
  if (document.hidden) return;
  if (state.taskId && !state.pollTimer) startPoll();
  if (state.parseTaskId && !state.parseTimer) startParsePoll();
  checkHealth();
});
