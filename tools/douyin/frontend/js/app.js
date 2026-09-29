/* ===== douyin 前端逻辑 ===== */
"use strict";

const $ = (id) => document.getElementById(id);

// 自动推断挂载前缀
const _seg = window.location.pathname.split("/").filter(Boolean);
const BASE = _seg.length ? "/" + _seg[0] : "";

const state = {
  mode: "video",
  video: null,          // 单作品解析结果
  userItems: [],        // 主页作品条目
  upName: "",
  selected: new Set(),  // 主页选中的索引
  taskId: null,
  pollTimer: null,
  parseTaskId: null,    // 主页抓取任务
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
function fmtDur(sec) {
  if (!sec && sec !== 0) return "";
  sec = Number(sec) || 0;
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = Math.floor(sec % 60);
  if (h) return h + ":" + String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
  if (m) return m + ":" + String(s).padStart(2, "0");
  return s + " 秒";
}
function fmtTime(s) {
  if (s < 60) return s.toFixed(0) + " 秒";
  return Math.floor(s / 60) + " 分 " + Math.floor(s % 60) + " 秒";
}
function fmtCount(n) {
  n = Number(n) || 0;
  if (n >= 10000) return (n / 10000).toFixed(1).replace(/\.0$/, "") + "万";
  return String(n);
}
function fmtDate(ts) {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleDateString("zh-CN");
}
function setStatus(el, msg, cls) {
  el.textContent = msg;
  el.className = "status-line" + (cls ? " " + cls : "");
}

/* ---------- 健康检查 ---------- */
async function checkHealth() {
  try {
    const r = await fetch(BASE + "/api/config");
    $("srvDot").className = "dot " + (r.ok ? "ok" : "err");
    $("srvText").textContent = r.ok ? "服务运行中" : "服务异常";
  } catch (e) {
    $("srvDot").className = "dot err";
    $("srvText").textContent = "无法连接";
  }
}

/* ---------- 单作品解析 ---------- */
async function parseVideo() {
  const text = $("vUrlInput").value.trim();
  if (!text) { setStatus($("vStatus"), "请粘贴分享文案或抖音链接", "err"); return; }
  const btn = $("vParseBtn");
  btn.disabled = true; btn.textContent = "⏳ 解析中…";
  setStatus($("vStatus"), "正在解析(短链自动跳转, 作者/清晰度探测中)…");
  try {
    const r = await fetch(BASE + "/api/parse_video", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: text }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "解析失败");
    state.video = d;
    $("vTitle").textContent = d.title || "抖音作品";
    $("vThumb").src = d.cover ? BASE + "/api/img?url=" + encodeURIComponent(d.cover) : "";
    $("vThumb").style.display = d.cover ? "" : "none";
    const meta = [];
    if (d.author && d.author.name) meta.push("👤 " + d.author.name);
    if (d.author && d.author.unique_id) meta.push("@" + d.author.unique_id);
    if (d.kind === "image") meta.push("🖼 图集 " + d.image_count + " 张");
    else if (d.duration) meta.push("⏱ " + fmtDur(d.duration));
    if (d.create_time) meta.push("📅 " + fmtDate(d.create_time));
    const st = d.statistics || {};
    if (st.digg) meta.push("❤ " + fmtCount(st.digg));
    if (st.collect) meta.push("⭐ " + fmtCount(st.collect));
    meta.push(d.has_cookie ? "🔓 已配置 Cookie" : "🌐 匿名");
    $("vMeta").textContent = meta.join(" · ");

    const isVideo = d.kind !== "image";
    $("vQualityBox").classList.toggle("hidden", !isVideo);
    const qsel = $("vQuality");
    qsel.innerHTML = "";
    if (isVideo) {
      (d.quality || []).forEach((o, i) => {
        const opt = document.createElement("option");
        opt.value = String(i);
        const sz = o.size ? " · " + fmtBytes(o.size) : "";
        const rate = o.bitrate ? " · " + (o.bitrate / 1000).toFixed(0) + "kbps" : "";
        opt.textContent = `${o.label}${rate}${sz}`;
        qsel.appendChild(opt);
      });
      $("vDlBtn").textContent = "⬇ 下载视频";
      $("vDlHint").textContent = "ℹ 无水印单文件直链，服务器流式代理推送到浏览器，不落服务器磁盘。";
    } else {
      $("vDlBtn").textContent = "⬇ 下载 " + d.image_count + " 张图片";
      $("vDlHint").textContent = "ℹ 图集作品将逐张推送下载(浏览器可能询问批量下载权限)。";
    }
    $("vResult").classList.remove("hidden");
    setStatus($("vStatus"), "✅ 解析成功", "ok");
  } catch (e) {
    setStatus($("vStatus"), "❌ " + e.message, "err");
  } finally {
    btn.disabled = false; btn.textContent = "🔍 解析作品";
  }
}

function selQualityOpt() {
  const idx = parseInt($("vQuality").value || "0", 10);
  return (state.video && state.video.quality) ? (state.video.quality[idx] || null) : null;
}

function streamDownload(url, name, delay) {
  setTimeout(() => {
    const a = document.createElement("a");
    a.href = BASE + "/api/stream?url=" + encodeURIComponent(url) + "&name=" + encodeURIComponent(name);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
  }, delay);
}

async function downloadVideo() {
  if (!state.video) return;
  const btn = $("vDlBtn");
  const old = btn.textContent;
  btn.disabled = true; btn.textContent = "⏳ 准备下载…";
  try {
    const opt = selQualityOpt();
    const qp = (state.video.kind === "image") ? "" :
      "&quality_type=" + (opt ? opt.quality_type : 0);
    const r = await fetch(BASE + "/api/direct?aweme_id=" +
      encodeURIComponent(state.video.aweme_id) + qp);
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "获取直链失败");
    if (!d.files || !d.files.length) throw new Error("没有可下载的内容");
    d.files.forEach((f, i) => streamDownload(f.url, f.name, i * 500));
    setStatus($("vStatus"), "✅ 已触发 " + d.files.length + " 个文件下载（浏览器可能询问保存位置）", "ok");
  } catch (e) {
    setStatus($("vStatus"), "❌ " + e.message, "err");
  } finally {
    btn.disabled = false; btn.textContent = old;
  }
}

/* ---------- 主页作品抓取 ---------- */
async function parseUser() {
  const text = $("sUrlInput").value.trim();
  if (!text) { setStatus($("sStatus"), "请粘贴主页分享文案或链接", "err"); return; }
  const btn = $("sParseBtn");
  btn.disabled = true; btn.textContent = "⏳ 创建任务…";
  setStatus($("sStatus"), "正在创建抓取任务…");
  try {
    const r = await fetch(BASE + "/api/parse_user", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: text }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "创建任务失败");
    state.parseTaskId = d.task_id;
    setStatus($("sStatus"), "任务已创建，正在抓取（可切走应用，任务会在服务器继续）…", "");
    startParsePoll();
  } catch (e) {
    setStatus($("sStatus"), "❌ " + e.message, "err");
  } finally {
    btn.disabled = false; btn.textContent = "🔍 抓取作品";
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
      setStatus($("sStatus"), "⏳ 抓取中… " + (t.count ? "已找到 " + t.count + " 个作品" : "正在翻页"), "");
      return;
    }
    stopParsePoll();
    if (t.status === "error") {
      setStatus($("sStatus"), "❌ " + (t.error || "抓取失败"), "err");
      return;
    }
    state.userItems = t.items || [];
    state.upName = t.up_name || "";
    const face = $("upFace");
    if (t.up_face) {
      face.src = BASE + "/api/img?url=" + encodeURIComponent(t.up_face);
      face.style.display = "";
    } else { face.style.display = "none"; }
    $("upName").textContent = state.upName || "抖音作者";
    const meta = [];
    if (t.unique_id) meta.push("@" + t.unique_id);
    meta.push("抓取到 " + state.userItems.length + " 个作品");
    if (t.aweme_total) meta.push("主页显示共 " + t.aweme_total + " 个");
    if (t.has_cookie) meta.push("🔓 已配置 Cookie"); else meta.push("🌐 匿名(分页受限)");
    $("upMeta").textContent = meta.join(" · ");
    const warn = $("sWarn");
    if (t.warn) { warn.textContent = "⚠ " + t.warn; warn.classList.remove("hidden"); }
    else warn.classList.add("hidden");
    $("sResult").classList.remove("hidden");
    state.selected = new Set(state.userItems.map((_, i) => i));
    renderUserGrid();
    setStatus($("sStatus"), "✅ 抓取完成: " + state.userItems.length + " 个作品", "ok");
    state.parseTaskId = null;
  } catch (e) {
    stopParsePoll();
    setStatus($("sStatus"), "❌ 轮询中断: " + e.message, "err");
  }
}

function renderUserGrid() {
  const grid = $("sGrid");
  grid.innerHTML = "";
  state.userItems.forEach((it, i) => {
    const div = document.createElement("div");
    div.className = "m-item" + (state.selected.has(i) ? " selected" : "");
    div.dataset.idx = i;
    const img = document.createElement("img");
    img.loading = "lazy";
    img.alt = it.title;
    if (it.cover) img.src = BASE + "/api/img?url=" + encodeURIComponent(it.cover);
    else img.style.opacity = "0.15";
    img.onerror = () => { img.style.opacity = "0.15"; };
    div.appendChild(img);
    if (it.kind === "image") {
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = "图集 " + (it.image_count || 0);
      div.appendChild(tag);
    } else if (it.duration) {
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = fmtDur(it.duration);
      div.appendChild(tag);
    }
    const lbl = document.createElement("div");
    lbl.className = "s-item-meta";
    const t1 = document.createElement("div");
    t1.className = "s-item-title";
    t1.textContent = it.title || "(无标题)";
    t1.title = it.title || "";
    const t2 = document.createElement("div");
    t2.className = "s-item-sub";
    t2.textContent = "❤ " + fmtCount(it.digg) + " · 💬 " + fmtCount(it.comment) +
      (it.create_time ? " · " + fmtDate(it.create_time) : "");
    lbl.appendChild(t1); lbl.appendChild(t2);
    div.appendChild(lbl);
    const mark = document.createElement("span");
    mark.className = "sel-mark";
    mark.textContent = "✓";
    div.appendChild(mark);
    div.onclick = () => {
      if (state.selected.has(i)) state.selected.delete(i);
      else state.selected.add(i);
      div.classList.toggle("selected", state.selected.has(i));
      $("sDlBtn").disabled = state.selected.size === 0;
      updateUserCount();
    };
    grid.appendChild(div);
  });
  $("sDlBtn").disabled = state.selected.size === 0;
  updateUserCount();
  updateSelectAllBtn();
}

function updateUserCount() {
  $("sCount").textContent = state.userItems.length + " 个作品 · 已选 " + state.selected.size;
  const imgs = state.userItems.filter((x, i) => state.selected.has(i) && x.kind === "image").length;
  $("sHint").textContent = imgs ? ("其中 " + imgs + " 个图集") : "";
}

function selectAllUser() {
  if (state.selected.size >= state.userItems.length) state.selected.clear();
  else state.selected = new Set(state.userItems.map((_, i) => i));
  document.querySelectorAll("#sGrid .m-item").forEach(div => {
    const i = parseInt(div.dataset.idx, 10);
    div.classList.toggle("selected", state.selected.has(i));
  });
  $("sDlBtn").disabled = state.selected.size === 0;
  updateUserCount();
  updateSelectAllBtn();
}

function updateSelectAllBtn() {
  $("sSelectAll").textContent =
    (state.selected.size > 0 && state.selected.size >= state.userItems.length)
      ? "☑ 取消全选" : "☑ 全选";
}

async function downloadUser() {
  if (!state.selected.size) return;
  const btn = $("sDlBtn");
  btn.disabled = true; btn.textContent = "⏳ 创建任务…";
  setStatus($("sStatus"), "正在创建下载任务…");
  try {
    const idxs = [...state.selected].sort((a, b) => a - b);
    const items = idxs.map(i => {
      const it = state.userItems[i];
      return { aweme_id: it.aweme_id, title: it.title, kind: it.kind };
    });
    const maxHeight = parseInt($("sQuality").value || "0", 10);
    const needZip = $("sZip").checked;
    const r = await fetch(BASE + "/api/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: state.upName || "抖音作品", items,
                             quality: 0, max_height: maxHeight, need_zip: needZip }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "创建任务失败");
    state.taskId = d.task_id;
    $("progressPanel").classList.remove("hidden");
    $("progActions").classList.add("hidden");
    $("progErrors").classList.add("hidden");
    setStatus($("sStatus"), "任务已创建(" + items.length + " 个作品, " +
      (needZip ? "将打包 ZIP" : "逐个文件") + ")，开始下载…", "");
    startPoll();
  } catch (e) {
    setStatus($("sStatus"), "❌ " + e.message, "err");
  } finally {
    btn.disabled = false; btn.textContent = "⬇ 下载选中";
  }
}

/* ---------- 下载任务轮询 ---------- */
function startPoll() {
  stopPoll();
  state.pollTimer = setInterval(pollTask, 1200);
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
      queued: "⏳ 排队中…", downloading: "⬇ 下载中…", packing: "📦 打包中…",
      done: "✅ 完成", error: "❌ 失败",
    };
    $("progPhase").textContent = phaseMap[t.phase] || t.phase;
    $("progPct").textContent = (t.percent || 0).toFixed(1) + "%";
    $("progFill").style.width = (t.percent || 0) + "%";
    $("progDetail").textContent = (t.done || 0) + " / " + (t.total || 0) +
      (t.status_text ? " · " + t.status_text : "");
    $("progElapsed").textContent = "耗时 " + fmtTime(t.elapsed || 0);
    if (t.errors && t.errors.length) {
      $("progErrors").classList.remove("hidden");
      $("progErrors").textContent = "⚠ " + t.errors.length + " 项失败: " +
        t.errors.map(e => e.error).join("; ").slice(0, 400);
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
      const msg = "✅ 完成: " + t.ok_count + " 个文件" +
        (t.fail_count ? "，" + t.fail_count + " 失败" : "");
      setStatus($("sStatus"), msg + (t.zip_path ? "，ZIP 开始下载…" : "，文件开始下载…"),
        t.fail_count ? "err" : "ok");
      if (t.zip_path) {
        const a = document.createElement("a");
        a.href = BASE + "/api/tasks/" + t.id + "/zip";
        a.download = "";
        document.body.appendChild(a); a.click(); a.remove();
      } else {
        fetch(BASE + "/api/tasks/" + t.id + "/browse").then(rr => rr.json()).then(dd => {
          if (!dd.files || !dd.files.length) return;
          dd.files.forEach((f, i) => {
            setTimeout(() => {
              const a = document.createElement("a");
              a.href = BASE + "/api/tasks/" + t.id + "/files/" + encodeURIComponent(f.name);
              a.download = f.name;
              document.body.appendChild(a); a.click(); a.remove();
            }, i * 500);
          });
          setStatus($("sStatus"), "已触发 " + dd.files.length + " 个文件下载（浏览器可能要求允许批量下载）", "ok");
        }).catch(() => {});
      }
    } else if (t.status === "error") {
      stopPoll();
      $("progActions").classList.remove("hidden");
      $("zipLink").classList.add("hidden");
      setStatus($("sStatus"), "❌ 任务失败: " + (t.error || "未知错误"), "err");
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
    let msg = d.has_cookie ? "已配置 Cookie：" + d.masked : "尚未配置 Cookie（单作品仍可匿名解析下载）";
    if (a.has_sessionid) {
      msg += " ✅ 含 sessionid，主页可分页抓取全部作品";
      $("cfgStatus").className = "status-line ok";
    } else {
      msg += " ⚠ 缺 sessionid，主页翻页受限（只能拿到抖音匿名返回的前若干条）";
      $("cfgStatus").className = "status-line err";
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
  if (!cookie) { setStatus($("cfgStatus"), "Cookie 为空，未保存", "err"); return; }
  $("cfgStatus").textContent = "正在保存…";
  const r = await fetch(BASE + "/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cookie }),
  });
  const d = await r.json();
  if (!r.ok) { setStatus($("cfgStatus"), "❌ 保存失败", "err"); return; }
  if (d.warn) setStatus($("cfgStatus"), "⚠ 已保存，" + d.warn, "err");
  else setStatus($("cfgStatus"), "✅ 已保存", "ok");
  $("cookieInput").value = "";
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
      meta.textContent = `${when} · ${h.ok_count} 个文件 · ${fmtBytes(h.total_bytes || 0)}`;
      if (h.cleaned) meta.textContent += " · 🧹 服务器临时文件已清理";
      main.appendChild(title); main.appendChild(meta);
      div.appendChild(main);
      const acts = document.createElement("div");
      acts.className = "his-actions";
      if (!h.cleaned) {
        if (h.zip_path) {
          const z = document.createElement("a");
          z.className = "btn btn-success btn-sm";
          z.href = BASE + "/api/tasks/" + h.id + "/zip";
          z.download = "";
          z.textContent = "📦 ZIP";
          acts.appendChild(z);
        } else {
          const f = document.createElement("button");
          f.className = "btn btn-ghost btn-sm";
          f.textContent = "📁 重新下载文件";
          f.onclick = () => {
            fetch(BASE + "/api/tasks/" + h.id + "/browse").then(rr => rr.json()).then(dd => {
              if (!dd.files || !dd.files.length) { alert("文件已清理"); return; }
              dd.files.forEach((ff, i) => {
                setTimeout(() => {
                  const a = document.createElement("a");
                  a.href = BASE + "/api/tasks/" + h.id + "/files/" + encodeURIComponent(ff.name);
                  a.download = ff.name;
                  document.body.appendChild(a); a.click(); a.remove();
                }, i * 500);
              });
            }).catch(() => {});
          };
          acts.appendChild(f);
        }
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
      $("userPanel").classList.toggle("hidden", state.mode !== "user");
    };
  });

  $("vParseBtn").onclick = parseVideo;
  $("vUrlInput").addEventListener("focus", function () { this.select(); });
  $("vUrlInput").addEventListener("keydown", e => { if (e.key === "Enter") parseVideo(); });
  $("vDlBtn").onclick = downloadVideo;

  $("sParseBtn").onclick = parseUser;
  $("sUrlInput").addEventListener("focus", function () { this.select(); });
  $("sUrlInput").addEventListener("keydown", e => { if (e.key === "Enter") parseUser(); });
  $("sDlBtn").onclick = downloadUser;
  $("sSelectAll").onclick = selectAllUser;
  $("historyClear").onclick = clearHistory;

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
});

// 手机切回应用/解锁时立即恢复轮询 (后台标签页 setInterval 会被浏览器降频)
document.addEventListener("visibilitychange", () => {
  if (document.hidden) return;
  if (state.taskId && !state.pollTimer) startPoll();
  if (state.parseTaskId && !state.parseTimer) startParsePoll();
  checkHealth();
});
