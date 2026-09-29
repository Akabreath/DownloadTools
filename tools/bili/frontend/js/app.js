/* ===== bili 前端逻辑 ===== */
"use strict";

const $ = (id) => document.getElementById(id);

// 自动推断挂载前缀
const _seg = window.location.pathname.split("/").filter(Boolean);
const BASE = _seg.length ? "/" + _seg[0] : "";

const state = {
  mode: "video",
  video: null,          // 视频解析结果
  pagesSel: new Set(),  // 选中的分P(page 号)
  selPages: [],         // 解析后可用分P [{page,part,duration,cid}]
  spaceItems: [],       // UP主投稿条目
  upName: "",
  selected: new Set(),  // 选中的投稿索引
  visibleIdx: [],
  taskId: null,
  pollTimer: null,
  parseTaskId: null,    // 空间抓取任务
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
  if (n >= 10000) return (n / 10000).toFixed(1).replace(/\.0$/, "") + "万";
  return String(n);
}
function setStatus(el, msg, cls) {
  el.textContent = msg;
  el.className = "status-line" + (cls ? " " + cls : "");
}
function sanitizeName(s, maxLen) {
  return String(s || "").replace(/[\\/:*?"<>|\r\n\t]/g, "_").replace(/\s+/g, " ").trim().slice(0, maxLen || 90) || "untitled";
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

/* ---------- 视频解析 ---------- */
async function parseVideo() {
  const text = $("vUrlInput").value.trim();
  if (!text) { setStatus($("vStatus"), "请粘贴视频链接或分享文字", "err"); return; }
  const btn = $("vParseBtn");
  btn.disabled = true; btn.textContent = "⏳ 解析中…";
  setStatus($("vStatus"), "正在解析(短链自动跳转, 分P/清晰度探测中)…");
  try {
    const r = await fetch(BASE + "/api/parse_video", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: text }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "解析失败");
    state.video = d;
    state.selPages = d.pages || [];
    $("vTitle").textContent = d.title || "视频";
    $("vThumb").src = d.pic ? BASE + "/api/img?url=" + encodeURIComponent(d.pic) : "";
    $("vThumb").style.display = d.pic ? "" : "none";
    const meta = [];
    if (d.owner && d.owner.name) meta.push("👤 " + d.owner.name);
    if (d.duration) meta.push("⏱ " + fmtDur(d.duration));
    meta.push("📑 " + state.selPages.length + "P");
    if (d.is_login) meta.push("🔓 已登录");
    else if (d.has_cookie) meta.push("🔒 Cookie 未登录(仅 720P)");
    else meta.push("🌐 匿名(仅 720P)");
    $("vMeta").textContent = meta.join(" · ");

    // 清晰度
    const qsel = $("vQuality");
    qsel.innerHTML = "";
    (d.quality || []).forEach((o, i) => {
      const opt = document.createElement("option");
      opt.value = String(i);
      const tag = o.kind === "dash" ? "· 需合并" : "· 直连";
      const sz = o.size ? " · " + fmtBytes(o.size) : "";
      opt.textContent = o.label + " " + tag + sz;
      qsel.appendChild(opt);
    });
    // 分P选择
    renderPagesBox();
    $("vResult").classList.remove("hidden");
    updateVDlHint();
    setStatus($("vStatus"), "✅ 解析成功", "ok");
  } catch (e) {
    setStatus($("vStatus"), "❌ " + e.message, "err");
  } finally {
    btn.disabled = false; btn.textContent = "🔍 解析视频";
  }
}

function renderPagesBox() {
  const box = $("vPagesBox");
  box.innerHTML = "";
  if (state.selPages.length <= 1) {
    box.classList.add("hidden");
    state.pagesSel = new Set([1]);
    return;
  }
  box.classList.remove("hidden");
  const lbl = document.createElement("div");
  lbl.className = "pages-title";
  lbl.textContent = "分P(共 " + state.selPages.length + " 个):";
  box.appendChild(lbl);
  const defAll = !(state.video && state.video.sel_p > 1);
  state.pagesSel = new Set();
  state.selPages.forEach(pg => {
    const lab = document.createElement("label");
    lab.className = "chk page-chk";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = defAll || pg.page === state.video.sel_p;
    if (cb.checked) state.pagesSel.add(pg.page);
    cb.onchange = () => {
      if (cb.checked) state.pagesSel.add(pg.page);
      else state.pagesSel.delete(pg.page);
      updateVDlHint();
    };
    lab.appendChild(cb);
    const span = document.createElement("span");
    span.textContent = "P" + pg.page + " " + (pg.part || "") + (pg.duration ? " · " + fmtDur(pg.duration) : "");
    lab.appendChild(span);
    box.appendChild(lab);
  });
}

function selQualityOpt() {
  const qsel = $("vQuality");
  const idx = parseInt(qsel.value || "0", 10);
  return (state.video && state.video.quality) ? (state.video.quality[idx] || null) : null;
}

function updateVDlHint() {
  const opt = selQualityOpt();
  const hint = $("vDlHint");
  if (!opt) { hint.textContent = ""; return; }
  const n = state.pagesSel.size;
  if (opt.kind === "dash") {
    hint.textContent = "⚠ " + opt.label + " 为音视频分离流，需服务器临时合并（几十秒~几分钟），完成后自动推送下载，不长期保存";
  } else {
    hint.textContent = "ℹ " + opt.label + " 单文件直连，不占服务器存储" + (n > 1 ? "，将依次下载 " + n + " 个分P" : "");
  }
}

function fileNameFor(page) {
  const title = sanitizeName(state.video.title);
  const multi = state.pagesSel.size > 1;
  return multi ? title + " P" + page + ".mp4" : title + ".mp4";
}

async function downloadVideo() {
  if (!state.video || !state.pagesSel.size) return;
  const opt = selQualityOpt();
  if (!opt) { setStatus($("vStatus"), "没有可用清晰度", "err"); return; }
  const btn = $("vDlBtn");
  btn.disabled = true; btn.textContent = "⏳ 准备下载…";
  try {
    if (opt.kind === "direct") {
      // 单文件直连: 逐P触发浏览器下载 (流式代理, 不落服务器磁盘)
      const pages = state.selPages.filter(pg => state.pagesSel.has(pg.page));
      pages.forEach((pg, k) => {
        setTimeout(() => {
          // durl 直链是解析时的实时地址, 需按 bvid+cid+qn 向后端要流
          fetch(BASE + "/api/durl?bvid=" + encodeURIComponent(state.video.bvid) +
                "&cid=" + pg.cid + "&qn=" + opt.qn)
            .then(r => r.json())
            .then(dd => {
              if (!dd.url) throw new Error(dd.error || "获取直链失败");
              const fname = fileNameFor(pg.page);
              const a = document.createElement("a");
              a.href = BASE + "/api/stream?url=" + encodeURIComponent(dd.url) + "&name=" + encodeURIComponent(fname);
              a.download = fname;
              document.body.appendChild(a);
              a.click();
              a.remove();
            })
            .catch(err => setStatus($("vStatus"), "❌ " + err.message, "err"));
        }, k * 500);
      });
      setStatus($("vStatus"), "✅ 已触发 " + pages.length + " 个文件下载（浏览器可能询问保存位置）", "ok");
      return;
    }
    // DASH 高清: 服务器临时合并任务
    const pages = state.selPages.filter(pg => state.pagesSel.has(pg.page));
    const items = pages.map(pg => ({ bvid: state.video.bvid, p: pg.page }));
    await createTask(state.video.title, items, opt.qn, false);
  } catch (e) {
    setStatus($("vStatus"), "❌ " + e.message, "err");
  } finally {
    btn.disabled = false; btn.textContent = "⬇ 下载";
  }
}

/* ---------- UP主空间抓取 ---------- */
async function parseSpace() {
  const text = $("sUrlInput").value.trim();
  if (!text) { setStatus($("sStatus"), "请粘贴 UP主空间链接", "err"); return; }
  const btn = $("sParseBtn");
  btn.disabled = true; btn.textContent = "⏳ 创建任务…";
  setStatus($("sStatus"), "正在创建抓取任务…");
  try {
    const r = await fetch(BASE + "/api/parse_space", {
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
    btn.disabled = false; btn.textContent = "🔍 抓取投稿";
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
      setStatus($("sStatus"), "⏳ 抓取中… " + (t.count ? "已找到 " + t.count + " 个视频" : "正在翻页"), "");
      return;
    }
    stopParsePoll();
    if (t.status === "error") {
      setStatus($("sStatus"), "❌ " + (t.error || "抓取失败"), "err");
      return;
    }
    state.spaceItems = t.items || [];
    state.upName = t.up_name || "";
    const face = $("upFace");
    if (t.up_face) { face.src = BASE + "/api/img?url=" + encodeURIComponent(t.up_face); face.style.display = ""; }
    else { face.style.display = "none"; }
    $("upName").textContent = state.upName || "UP主";
    $("sResult").classList.remove("hidden");
    state.selected = new Set(state.spaceItems.map((_, i) => i));
    renderSpaceGrid();
    setStatus($("sStatus"), "✅ 抓取完成: " + state.spaceItems.length + " 个视频", "ok");
    state.parseTaskId = null;
  } catch (e) {
    stopParsePoll();
    setStatus($("sStatus"), "❌ 轮询中断: " + e.message, "err");
  }
}

function renderSpaceGrid() {
  const grid = $("sGrid");
  grid.innerHTML = "";
  const items = state.spaceItems;
  items.forEach((it, i) => {
    const div = document.createElement("div");
    div.className = "m-item";
    div.dataset.idx = i;
    const img = document.createElement("img");
    img.loading = "lazy";
    img.alt = it.title;
    if (it.pic) {
      img.src = BASE + "/api/img?url=" + encodeURIComponent(it.pic);
    } else {
      img.style.opacity = "0.15";
    }
    img.onerror = () => { img.style.opacity = "0.15"; };
    div.appendChild(img);
    const lbl = document.createElement("div");
    lbl.className = "s-item-meta";
    const t1 = document.createElement("div");
    t1.className = "s-item-title";
    t1.textContent = it.title;
    t1.title = it.title;
    const t2 = document.createElement("div");
    t2.className = "s-item-sub";
    t2.textContent = "▶ " + fmtCount(it.play || 0) + " · " + (it.length || "?") +
      (it.created ? " · " + new Date(it.created * 1000).toLocaleDateString("zh-CN") : "");
    lbl.appendChild(t1);
    lbl.appendChild(t2);
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
      updateSpaceCount();
    };
    grid.appendChild(div);
  });
  $("sDlBtn").disabled = state.selected.size === 0;
  updateSpaceCount();
  updateSelectAllBtn();
}

function updateSpaceCount() {
  $("sCount").textContent = state.spaceItems.length + " 个视频 · 已选 " + state.selected.size;
}

function selectAllSpace() {
  if (state.selected.size > 0 && state.selected.size >= state.spaceItems.length) {
    state.selected.clear();
  } else {
    state.selected = new Set(state.spaceItems.map((_, i) => i));
  }
  document.querySelectorAll("#sGrid .m-item").forEach(div => {
    const i = parseInt(div.dataset.idx, 10);
    div.classList.toggle("selected", state.selected.has(i));
  });
  $("sDlBtn").disabled = state.selected.size === 0;
  updateSpaceCount();
  updateSelectAllBtn();
}

function updateSelectAllBtn() {
  const btn = $("sSelectAll");
  btn.textContent = (state.selected.size > 0 && state.spaceItems.length > 0 &&
                     state.selected.size >= state.spaceItems.length)
    ? "☑ 取消全选" : "☑ 全选";
}

async function downloadSpace() {
  if (!state.selected.size) return;
  const btn = $("sDlBtn");
  btn.disabled = true; btn.textContent = "⏳ 创建任务…";
  setStatus($("sStatus"), "正在创建下载任务…");
  try {
    const idxs = [...state.selected].sort((a, b) => a - b);
    const items = idxs.map(i => {
      const it = state.spaceItems[i];
      return { bvid: it.bvid, aid: it.aid || "", title: it.title };
    });
    const qn = parseInt($("sQuality").value || "80", 10);
    const needZip = $("sZip").checked;
    const title = state.upName || "UP主投稿";
    const r = await fetch(BASE + "/api/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title, items, quality: qn, need_zip: needZip }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "创建任务失败");
    state.taskId = d.task_id;
    $("progressPanel").classList.remove("hidden");
    $("progActions").classList.add("hidden");
    $("progErrors").classList.add("hidden");
    setStatus($("sStatus"), "任务已创建(" + items.length + " 个视频, " + (needZip ? "将打包 ZIP" : "逐个文件") + ")，开始下载…", "");
    startPoll();
  } catch (e) {
    setStatus($("sStatus"), "❌ " + e.message, "err");
  } finally {
    btn.disabled = false; btn.textContent = "⬇ 下载选中";
  }
}

/* ---------- 下载任务 ---------- */
async function createTask(title, items, quality, needZip) {
  const r = await fetch(BASE + "/api/download", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title, items, quality, need_zip: needZip }),
  });
  const d = await r.json();
  if (!r.ok) throw new Error(d.detail || "创建任务失败");
  state.taskId = d.task_id;
  $("progressPanel").classList.remove("hidden");
  $("progActions").classList.add("hidden");
  $("progErrors").classList.add("hidden");
  setStatus($("vStatus"), "高清合并任务已创建，请稍候…", "");
  startPoll();
}

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
      queued: "⏳ 排队中…", resolving: "🔍 解析分P…", downloading: "⬇ 下载/合并中…",
      packing: "📦 打包中…", done: "✅ 完成", error: "❌ 失败",
    };
    $("progPhase").textContent = phaseMap[t.phase] || t.phase;
    $("progPct").textContent = (t.percent || 0).toFixed(1) + "%";
    $("progFill").style.width = (t.percent || 0) + "%";
    $("progDetail").textContent = (t.phase === "resolving")
      ? (t.status_text || "解析中…")
      : (t.done + " / " + (t.total || 0) + (t.status_text ? " · " + t.status_text : ""));
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
      const msg = "✅ 完成: " + t.ok_count + " 个文件" + (t.fail_count ? "，" + t.fail_count + " 失败" : "");
      if (t.zip_path) {
        setStatus($("sStatus"), msg + "，ZIP 开始下载…", t.fail_count ? "err" : "ok");
      } else {
        setStatus($("sStatus"), msg + "，文件开始下载…", t.fail_count ? "err" : "ok");
      }
      // 完成后自动推送下载: zip 直接下; 否则逐文件
      if (t.zip_path) {
        const a = document.createElement("a");
        a.href = BASE + "/api/tasks/" + t.id + "/zip";
        a.download = "";
        document.body.appendChild(a);
        a.click();
        a.remove();
      } else {
        fetch(BASE + "/api/tasks/" + t.id + "/browse").then(r => r.json()).then(dd => {
          if (!dd.files || !dd.files.length) return;
          dd.files.forEach((f, i) => {
            setTimeout(() => {
              const a = document.createElement("a");
              a.href = BASE + "/api/tasks/" + t.id + "/files/" + encodeURIComponent(f.name);
              a.download = f.name;
              document.body.appendChild(a);
              a.click();
              a.remove();
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
      setStatus($("vStatus"), "❌ 任务失败: " + (t.error || "未知错误"), "err");
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
    if (a.has_sessdata) {
      msg += " ✅ 字段完整，可解锁 1080P+ 与空间抓取";
      $("cfgStatus").className = "status-line ok";
    } else {
      msg += " ⚠ 缺少 SESSDATA/bili_jct/DedeUserID 关键字段，仅可用 720P 及匿名解析";
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
  $("cfgStatus").textContent = "正在校验登录态…";
  const r = await fetch(BASE + "/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cookie }),
  });
  const d = await r.json();
  if (!r.ok) {
    setStatus($("cfgStatus"), "❌ 保存失败", "err");
    return;
  }
  if (d.is_login) {
    const vip = d.vip ? "大会员" : "普通会员";
    setStatus($("cfgStatus"), "✅ 已保存并通过登录校验：@" + d.uname + "（" + vip + "，最高可下 1080P，大会员内容另计）", "ok");
  } else if (d.warn) {
    setStatus($("cfgStatus"), "⚠ 已保存，但" + d.warn, "err");
  } else {
    setStatus($("cfgStatus"), "已保存", "ok");
  }
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
      main.appendChild(title);
      main.appendChild(meta);
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
                  document.body.appendChild(a);
                  a.click();
                  a.remove();
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
      $("spacePanel").classList.toggle("hidden", state.mode !== "space");
    };
  });

  $("vParseBtn").onclick = parseVideo;
  $("vUrlInput").addEventListener("focus", function () { this.select(); });  // 点击自动全选
  $("vUrlInput").addEventListener("keydown", e => { if (e.key === "Enter") parseVideo(); });
  $("vDlBtn").onclick = downloadVideo;
  $("vQuality").onchange = updateVDlHint;

  $("sParseBtn").onclick = parseSpace;
  $("sUrlInput").addEventListener("focus", function () { this.select(); });
  $("sUrlInput").addEventListener("keydown", e => { if (e.key === "Enter") parseSpace(); });
  $("sDlBtn").onclick = downloadSpace;
  $("sSelectAll").onclick = selectAllSpace;
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
