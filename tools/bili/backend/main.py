#!/usr/bin/env python3
"""
bili - bilibili 视频/UP主投稿下载工具
功能:
  1. 视频链接解析: 支持桌面链接(www.bilibili.com/video/BVxx, 可混在分享文案中)与
     b23.tv 短链, 显示分P与清晰度(720P及以下单文件直连; 1080P+ DASH 音视频分离,
     服务器临时下载 + ffmpeg 合并, 完成后清理)
  2. UP主主页下载: space.bilibili.com/<mid>, 抓取全部投稿视频, 按 UP主名命名,
     默认打包 ZIP 下载到浏览器
设计约定(用户要求):
  - 下载全部直接推送到浏览器/手机, 取消"保存到服务器"选项;
    服务器只做临时中转(高清合并/ZIP聚合), 任务完成后 1 小时自动清理
  - 文件夹/文件以视频标题或 UP主名命名
"""
import asyncio
import hashlib
import json
import os
import re
import shutil
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import aiohttp
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ---------------- 路径配置 ----------------
ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = ROOT / "frontend"
DOWNLOAD_DIR = ROOT / "downloads"
CONFIG_FILE = ROOT / "config.json"
HISTORY_FILE = ROOT / "history.json"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
API = "https://api.bilibili.com"
CONCURRENCY = 3          # 下载并发
KEEP_SECONDS = 3600      # 任务完成后临时文件保留时长(浏览器取完即自动清理)

# WBI 签名混淆表
WBI_MIXIN = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
             27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
             37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
             22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52]

QN_LABEL = {6: "240P", 16: "360P", 32: "480P", 64: "720P", 74: "720P60",
            80: "1080P", 112: "1080P+", 116: "1080P60", 120: "4K",
            125: "HDR", 126: "杜比", 127: "8K"}
CDN_SUFFIX = ("bilivideo.com", "bilivideo.cn", "hdslb.com", "bilibili.com",
              "akamaized.net", "mcdn.bilivideo.cn")

# ---------------- 配置 (cookie) ----------------
def load_config() -> dict:
    cfg = {"cookie": ""}
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    return cfg

def save_config(updates: dict) -> dict:
    cfg = load_config()
    cfg.update(updates)
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg

def cookie_header() -> str:
    return load_config().get("cookie", "").strip()

def analyze_cookie(raw: str) -> dict:
    """静态分析 cookie 关键字段(不联网)"""
    if not raw:
        return {"count": 0, "has_sessdata": False, "has_jct": False,
                "has_dedeid": False, "looks_complete": False}
    fields = {}
    for seg in raw.split(";"):
        seg = seg.strip()
        if not seg or "=" not in seg:
            continue
        k, v = seg.split("=", 1)
        fields[k.strip()] = v.strip()
    return {
        "count": len(fields),
        "has_sessdata": "SESSDATA" in fields,
        "has_jct": "bili_jct" in fields,
        "has_dedeid": "DedeUserID" in fields,
        "looks_complete": all(k in fields for k in ("SESSDATA", "bili_jct", "DedeUserID")),
    }

# ---------------- 全局状态 ----------------
tasks = {}          # 下载任务
parse_tasks = {}    # UP主投稿抓取任务
history = []
executor = ThreadPoolExecutor(max_workers=2)
_wbi_cache = {"ts": 0, "mixin": "", "keys": None}

def load_history():
    global history
    if HISTORY_FILE.exists():
        try:
            history = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            history = []

def save_history():
    try:
        HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

load_history()

# ---------------- 工具函数 ----------------
def sanitize_filename(name: str, max_len: int = 90) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", str(name)).strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    return name[:max_len] if name else "untitled"

def headers(referer: str = "https://www.bilibili.com/", use_cookie: bool = True) -> dict:
    h = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
         "Accept-Language": "zh-CN,zh;q=0.9", "Referer": referer}
    if use_cookie:
        ck = cookie_header()
        if ck:
            h["Cookie"] = ck
    return h

def _cdn_check(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return any(host.endswith(s) for s in CDN_SUFFIX)

# ---------------- 链接识别 ----------------
def extract_bili_url(text: str) -> str:
    """从整段分享文案中抽取 bilibili / b23.tv 链接"""
    text = text.strip()
    urls = re.findall(r"https?://[^\s\u4e00-\u9fff\"'<>，。；、）】]+", text)
    for u in urls:
        u = u.rstrip(".,;:!?)]}")
        if "bilibili.com" in u or "b23.tv" in u:
            return u
    # 裸 b23.tv
    m = re.search(r"(b23\.tv/[A-Za-z0-9]+)", text)
    return "https://" + m.group(1) if m else ""

async def resolve_short_url(session: aiohttp.ClientSession, url: str) -> str:
    """b23.tv 短链 -> 真实地址 (手动跟随 302, 最多 6 跳;
    兼容中间态 b23.tv/video/BVxxx: 该地址再请求可能返回 200 而非跳转)"""
    cur = url
    for _ in range(6):
        async with session.get(cur, headers={"User-Agent": UA},
                               allow_redirects=False,
                               timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status in (301, 302, 303, 307, 308):
                cur = resp.headers.get("Location") or cur
                if cur.startswith("/"):
                    cur = "https://b23.tv" + cur
                continue
            break
    return to_www_url(cur)

def to_www_url(url: str) -> str:
    """b23.tv 域上的 video/space 中间态 -> www.bilibili.com 等价地址"""
    m = re.match(r"https?://b23\.tv/(video/BV[0-9A-Za-z]{10})", url)
    if m:
        return "https://www.bilibili.com/" + m.group(1)
    m = re.match(r"https?://b23\.tv/(space/\d+)", url)
    if m:
        return "https://www.bilibili.com/" + m.group(1)
    return url

def classify_url(url: str) -> dict:
    """识别链接类型, 返回 {kind: video|space|unknown, ...}"""
    url = re.sub(r"^https?://", "", url).split("?")[0].split("#")[0]
    m = re.match(r"(?:www\.|m\.)?bilibili\.com/video/(BV[0-9A-Za-z]{10}|av\d+)(?:/.*)?$", url)
    if m:
        bv = m.group(1)
        p = re.search(r"/video/[^/]+/\?*p=(\d+)", url)
        return {"kind": "video", "id": bv if bv.startswith("BV") else bv,
                "p": int(re.search(r"p=(\d+)", url).group(1)) if re.search(r"p=(\d+)", url) else 0}
    m = re.match(r"(?:www\.|m\.|space\.)?bilibili\.com/space/(\d+)", url)
    if m:
        return {"kind": "space", "mid": m.group(1)}
    return {"kind": "unknown"}

# ---------------- WBI 签名 ----------------
async def _fetch_wbi_keys(session: aiohttp.ClientSession) -> dict:
    now = time.time()
    if _wbi_cache["ts"] and now - _wbi_cache["ts"] < 21600:
        return _wbi_cache
    async with session.get(f"{API}/x/web-interface/nav", headers=headers(),
                           timeout=aiohttp.ClientTimeout(total=15)) as resp:
        d = await resp.json()
    img = (d.get("data") or {}).get("wbi_img") or {}
    img_key = img.get("img_url", "").rsplit("/", 1)[-1].split(".")[0]
    sub_key = img.get("sub_url", "").rsplit("/", 1)[-1].split(".")[0]
    if not img_key or not sub_key:
        return _wbi_cache
    mixin = "".join((img_key + sub_key)[i] for i in WBI_MIXIN)[:32]
    _wbi_cache.update(ts=now, mixin=mixin,
                      keys={"img_key": img_key, "sub_key": sub_key})
    return _wbi_cache

def _wbi_sign(params: dict, mixin: str) -> dict:
    p = dict(params)
    p["wts"] = int(time.time())
    q = "&".join(f"{k}={v}" for k, v in sorted(p.items()))
    p["w_rid"] = hashlib.md5((q + mixin).encode()).hexdigest()
    return p

async def api_json(session: aiohttp.ClientSession, url: str, referer: str,
                   retries: int = 3) -> dict:
    """GET bilibili API 并校验 code, 风控码自动退避重试"""
    last = None
    for i in range(retries):
        try:
            async with session.get(url, headers=headers(referer),
                                   timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    last = f"HTTP {resp.status}"
                else:
                    d = await resp.json(content_type=None)
                    code = d.get("code")
                    if code == 0:
                        return d
                    last = f"code={code} {d.get('message', '')}"
                    if code in (-412, -799, -352, -509):
                        # 风控/限速: 退避重试
                        await asyncio.sleep(2 * (i + 1))
                        continue
                    # 其它业务错误直接返回, 由调用方处理
                    return d
        except asyncio.CancelledError:
            raise
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        await asyncio.sleep(1.5 * (i + 1))
    raise HTTPException(502, f"bilibili API 请求失败: {url.split('?')[0]} -> {last}")

# ---------------- 视频信息 ----------------
async def fetch_view(session: aiohttp.ClientSession, bvid: str, aid: str = "") -> dict:
    """x/web-interface/view: 标题/UP主/分P"""
    url = f"{API}/x/web-interface/view?bvid={bvid}" if not aid else f"{API}/x/web-interface/view?aid={aid}"
    d = await api_json(session, url, "https://www.bilibili.com/video/")
    data = d.get("data") or {}
    if not data:
        raise HTTPException(404, "视频不存在或已失效")
    pages = []
    for pg in data.get("pages", []):
        pages.append({
            "cid": pg.get("cid", 0), "page": pg.get("page", 1),
            "part": pg.get("part", ""), "duration": pg.get("duration", 0),
        })
    pic = data.get("pic", "")
    if pic.startswith("http://"):
        pic = "https://" + pic[7:]
    return {
        "bvid": data.get("bvid", bvid), "aid": str(data.get("aid", "")),
        "title": data.get("title", ""), "pic": pic,
        "duration": data.get("duration", 0),
        "owner": {"mid": str((data.get("owner") or {}).get("mid", "")),
                  "name": (data.get("owner") or {}).get("name", "")},
        "pages": pages,
        "stat": data.get("stat", {}),
    }

def _qn_desc(durl_accept: list, dash_accept: list) -> list:
    """把接口返回的可用清晰度整理成选项列表(高清 dash 在前, 单文件 direct 在后)
    durl(单文件, <=720P 直连) / dash(>=1080P 需合并)"""
    opts = []
    for qn in sorted(set(dash_accept), reverse=True):
        if qn > 64:
            opts.append({"qn": qn, "label": QN_LABEL.get(qn, f"{qn}P"),
                         "kind": "dash"})
    for qn in sorted(set(durl_accept), reverse=True):
        if qn <= 64 and qn in (16, 32, 64):
            opts.append({"qn": qn, "label": QN_LABEL.get(qn, f"{qn}P"),
                         "kind": "direct"})
    return opts

async def fetch_playurl(session: aiohttp.ClientSession, bvid: str, cid: int,
                        qn: int, dash: bool) -> dict:
    fnval = 16 if dash else 0
    url = (f"{API}/x/player/playurl?bvid={bvid}&cid={cid}&qn={qn}"
           f"&fnval={fnval}&fnver=0&fourk=1")
    d = await api_json(session, url, f"https://www.bilibili.com/video/{bvid}")
    return d.get("data") or {}

async def probe_video(session: aiohttp.ClientSession, bvid: str, cid: int,
                      use_cookie: bool) -> dict:
    """探测可用清晰度/大小, 返回 options 列表"""
    # 单文件档: fnval=0 (有 cookie 也只是到 720P)
    d1 = await fetch_playurl(session, bvid, cid, 64, dash=False)
    durl = d1.get("durl") or []
    durl_accept = [int(x) for x in (d1.get("accept_quality") or [])]
    durl_accept = [q for q in durl_accept if q <= 64]
    # 高清档: fnval=16, 逐个候选档验证真实可用(非会员 accept 里 116 之类实际拿不到)
    d2 = await fetch_playurl(session, bvid, cid, 120, dash=True)
    dash_candidates = sorted({int(x) for x in (d2.get("accept_quality") or [])
                              if int(x) > 64}, reverse=True)
    dash_opts = []
    for qn in dash_candidates:
        try:
            dq = await fetch_playurl(session, bvid, cid, qn, dash=True)
            if dq.get("quality", 0) < qn:
                continue  # 被降级, 该档不可用
            size = 0
            videos = ((dq.get("dash") or {}).get("video")) or []
            target = next((v for v in videos if v.get("id") == qn), None) or \
                (max(videos, key=lambda v: v.get("id", 0)) if videos else None)
            u = stream_url(target) if target else ""
            if u:
                async with session.get(u, headers={"User-Agent": UA,
                                                   "Referer": f"https://www.bilibili.com/video/{bvid}",
                                                   "Range": "bytes=0-0"},
                                       timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status in (200, 206):
                        m = re.search(r"/(\d+)$", resp.headers.get("Content-Range", ""))
                        if m:
                            size = int(m.group(1))
            dash_opts.append({"qn": qn, "size": size})
        except Exception:
            continue
    opts = []
    for o in dash_opts:
        opts.append({"qn": o["qn"], "label": QN_LABEL.get(o["qn"], f"{o['qn']}P"),
                     "kind": "dash", "size": o["size"]})
    for qn in sorted(set(durl_accept), reverse=True):
        if qn in (16, 32, 64):
            size = (durl[0].get("size") or 0) if qn == d1.get("quality") and durl else 0
            opts.append({"qn": qn, "label": QN_LABEL.get(qn, f"{qn}P"),
                         "kind": "direct", "size": size})
    return opts

# ---------------- UP主空间 ----------------
def space_mid(url: str) -> str:
    m = re.search(r"space\.bilibili\.com/(\d+)", url)
    return m.group(1) if m else ""

async def _space_page(session: aiohttp.ClientSession, mid: str, pn: int,
                      ps: int = 30) -> dict:
    wb = await _fetch_wbi_keys(session)
    if not wb.get("mixin"):
        raise HTTPException(502, "无法获取 WBI 密钥")
    p = _wbi_sign({"mid": mid, "ps": str(ps), "pn": str(pn),
                   "order": "pubdate", "platform": "web",
                   "web_location": "1550101"}, wb["mixin"])
    qs = "&".join(f"{k}={v}" for k, v in p.items())
    d = await api_json(session, f"{API}/x/space/wbi/arc/search?{qs}",
                       f"https://space.bilibili.com/{mid}/video")
    return d.get("data") or {}

async def run_parse_space(task: dict, url: str):
    """后台抓取 UP主全部投稿(任务制, 手机切后台不中断)"""
    try:
        mid = space_mid(url)
        if not mid:
            task.update(status="error", error="无法识别空间链接中的用户 ID")
            return
        task.update(phase="fetching", mid=mid)
        async with aiohttp.ClientSession() as session:
            first = await _space_page(session, mid, 1)
            vlist = ((first.get("list") or {}).get("vlist")) or []
            page = first.get("page") or {}
            total = page.get("count") or 0
            if not vlist and total == 0:
                task.update(status="error", error="该空间没有公开投稿视频, 或 Cookie 无效/已被风控")
                return
            up_name = vlist[0].get("author") or ""
            # 头像一个: 列表无 face 字段, 从 acc/info 补(失败不阻塞)
            face = ""
            try:
                wb = await _fetch_wbi_keys(session)
                p = _wbi_sign({"mid": mid}, wb["mixin"])
                qs = "&".join(f"{k}={v}" for k, v in p.items())
                acc = await api_json(session, f"{API}/x/space/wbi/acc/info?{qs}",
                                     f"https://space.bilibili.com/{mid}")
                ad = acc.get("data") or {}
                if ad.get("name"):
                    up_name = ad["name"]
                face = ad.get("face") or ""
            except Exception:
                pass
            all_videos = []
            seen = set()
            def flatten(page_vlist):
                for v in page_vlist:
                    bv = v.get("bvid")
                    if not bv or bv in seen:
                        continue
                    seen.add(bv)
                    pic = v.get("pic", "")
                    if pic.startswith("http://"):
                        pic = "https://" + pic[7:]
                    all_videos.append({
                        "bvid": bv, "aid": str(v.get("aid", "")),
                        "title": v.get("title", ""), "pic": pic,
                        "length": v.get("length", ""),   # mm:ss
                        "play": v.get("play", 0),
                        "created": v.get("created", 0),
                    })
            flatten(vlist)
            total_pages = max(1, -(-total // 30))
            for pn in range(2, total_pages + 1):
                await asyncio.sleep(0.4)
                try:
                    d = await _space_page(session, mid, pn)
                    flatten(((d.get("list") or {}).get("vlist")) or [])
                except Exception:
                    continue
                task.update(count=len(all_videos))  # 中途进度
        if not all_videos:
            task.update(status="error", error="未抓取到任何视频")
            return
        task.update({
            "phase": "done", "status": "done",
            "up_name": up_name, "up_face": face, "mid": mid,
            "count": len(all_videos), "items": all_videos,
            "finished_at": time.time(),
        })
    except HTTPException as e:
        task.update(status="error", error=str(e.detail))
    except Exception as e:
        task.update(status="error", error=f"抓取失败: {e}")

# ---------------- 下载任务 ----------------
def gen_task_id():
    return f"{int(time.time()*1000)}"

def pick_stream(dash: dict, qn: int) -> dict:
    """从 DASH 响应里选目标清晰度视频流(缺失则降到最高可用)"""
    videos = dash.get("video") or []
    if not videos:
        return {}
    actual = min(qn, max(v.get("id", 0) for v in videos))
    target = next((v for v in videos if v.get("id") == actual), None)
    if not target:
        target = max(videos, key=lambda v: v.get("id", 0))
    target = dict(target)
    target["actual_qn"] = target.get("id", 0)
    return target

def stream_url(v: dict) -> str:
    u = v.get("baseUrl") or ""
    if not u and v.get("backup_url"):
        u = v["backup_url"][0]
    return u

async def _download_to(session: aiohttp.ClientSession, url: str, dest: Path,
                       referer: str, use_cookie: bool = False) -> int:
    """流式下载到 dest.part 再改名, 返回字节数; 防盗链带 UA+Referer"""
    h = {"User-Agent": UA, "Referer": referer}
    if use_cookie:
        ck = cookie_header()
        if ck:
            h["Cookie"] = ck
    tmp = dest.with_suffix(dest.suffix + ".part")
    size = 0
    async with session.get(url, headers=h, allow_redirects=True,
                           timeout=aiohttp.ClientTimeout(total=None,
                                                         sock_read=180)) as resp:
        if resp.status != 200:
            raise Exception(f"HTTP {resp.status}")
        with open(tmp, "wb") as f:
            async for chunk in resp.content.iter_chunked(1 << 16):
                f.write(chunk)
                size += len(chunk)
    if size < 100:
        tmp.unlink(missing_ok=True)
        raise Exception("内容过小")
    tmp.rename(dest)
    return size

async def _ffmpeg_merge(video: Path, audio: Path, out: Path):
    """DASH 音视频流合并(m4s 即 mp4 分段, 流拷贝不转码)"""
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-i", str(audio), "-i", str(video),
           "-c", "copy", "-movflags", "+faststart", str(out)]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise Exception(f"ffmpeg 合并失败: {err.decode(errors='replace')[:200]}")

async def download_unit(session: aiohttp.ClientSession, unit: dict, folder: Path,
                        results: dict, i: int, total: int, task: dict):
    """下载一个视频单元(单P):
    unit = {bvid, cid, page, fname}  quality 从 task['quality'] 取"""
    qn = task["quality"]
    fname = unit["fname"]
    referer = f"https://www.bilibili.com/video/{unit['bvid']}"
    dest = folder / fname
    try:
        if dest.exists():
            raise Exception("文件已存在")
        # 请求播放地址
        want_dash = qn > 64
        d = await fetch_playurl(session, unit["bvid"], unit["cid"], qn,
                                dash=want_dash)
        actual = d.get("quality", 0)
        if want_dash:
            # 若实际只到 <=64 且有 durl, 转单文件直连省合并
            if actual <= 64:
                durl = d.get("durl") or []
                if durl:
                    await _download_to(session, durl[0]["url"], dest, referer)
                    results["ok"].append(fname)
                    results["bytes"] += dest.stat().st_size
                    return
            dash = d.get("dash") or {}
            v = pick_stream(dash, qn)
            if not stream_url(v):
                raise Exception(f"该视频无 {QN_LABEL.get(qn, qn)} 及以上流(降级失败)")
            audio = max(dash.get("audio") or [], key=lambda a: a.get("id", 0))
            if not stream_url(audio):
                raise Exception("无音频流")
            tv = folder / f".{fname}.v.m4s"
            ta = folder / f".{fname}.a.m4s"
            try:
                await _download_to(session, stream_url(v), tv, referer)
                await _download_to(session, stream_url(audio), ta, referer)
                await _ffmpeg_merge(tv, ta, dest)
                results["bytes"] += dest.stat().st_size
            finally:
                tv.unlink(missing_ok=True)
                ta.unlink(missing_ok=True)
        else:
            durl = d.get("durl") or []
            if not durl:
                # 降级: 重试 fnval=0 低档
                raise Exception("该视频无单文件流")
            await _download_to(session, durl[0]["url"], dest, referer)
            results["bytes"] += dest.stat().st_size
        results["ok"].append(fname)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        results["fail"].append({"name": fname, "error": str(e)[:200]})
    finally:
        task["done"] += 1
        task["percent"] = round(task["done"] / total * 100, 1) if total else 100

async def run_download(task: dict, items: list, folder: Path, need_zip: bool):
    """下载任务主流程: 每视频先 view 展开分P -> 逐P下载 -> ZIP -> 定清理时间"""
    t0 = time.time()
    folder.mkdir(parents=True, exist_ok=True)
    results = {"ok": [], "fail": [], "bytes": 0}
    try:
        # 阶段1: 展开分P (视频 -> 单P单元)
        task["phase"] = "resolving"
        task["status_text"] = "正在解析分P信息…"
        units = []
        used_names = set()
        async with aiohttp.ClientSession() as session:
            for it in items:
                try:
                    v = await fetch_view(session, it.get("bvid", ""),
                                         it.get("aid", ""))
                except Exception as e:
                    results["fail"].append({"name": it.get("title", "?")[:40],
                                            "error": str(e)[:150]})
                    continue
                pages = v["pages"]
                if it.get("p"):
                    pages = [pg for pg in pages if pg["page"] == it["p"]]
                multi = len(pages) > 1
                title = sanitize_filename(v["title"])
                for pg in pages:
                    base = title if not multi else f"{title} P{pg['page']}"
                    fname = f"{base}.mp4"
                    # 不同视频同名标题防冲突
                    k = 2
                    while fname in used_names:
                        fname = f"{base}_{k}.mp4"
                        k += 1
                    used_names.add(fname)
                    units.append({"bvid": v["bvid"], "cid": pg["cid"],
                                  "page": pg["page"], "fname": fname})
                await asyncio.sleep(0.15)  # 礼貌限速
        total = len(units)
        task["total"] = total
        task["done"] = len(results["fail"]) if not total else 0
        if total == 0 and results["fail"]:
            raise Exception("所有视频均无法解析")
        # 阶段2: 下载
        task["phase"] = "downloading"
        task["status_text"] = ""
        if units:
            sem = asyncio.Semaphore(CONCURRENCY)
            async def guarded(u, i):
                async with sem:
                    await download_unit(session, u, folder, results, i, total, task)
            async with aiohttp.ClientSession() as session:
                await asyncio.gather(*[guarded(u, i) for i, u in enumerate(units, 1)])
        # 阶段3: ZIP
        zip_path = None
        if need_zip and results["ok"]:
            task["phase"] = "packing"
            task["status_text"] = "正在打包 ZIP…"
            zip_path = folder.parent / (folder.name + ".zip")
            loop = asyncio.get_event_loop()
            def _zip():
                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
                    for f in sorted(folder.iterdir()):
                        if f.is_file():
                            zf.write(f, arcname=f.name)
            await loop.run_in_executor(executor, _zip)
    except Exception as e:
        task.update(phase="error", status="error", error=str(e)[:300],
                    finished_at=time.time())
        return
    task.update({
        "phase": "done", "status": "done",
        "ok_count": len(results["ok"]), "fail_count": len(results["fail"]),
        "total_bytes": results["bytes"],
        "elapsed": round(time.time() - t0, 1),
        "folder": str(folder),
        "zip_path": str(zip_path) if zip_path else None,
        "files": results["ok"], "errors": results["fail"],
        "clean_at": time.time() + KEEP_SECONDS,
        "cleaned": False,
        "finished_at": time.time(),
    })
    # 历史(仅临时中转, 文件到期自动清)
    if results["ok"]:
        history.insert(0, {
            "id": task["id"], "title": task["title"],
            "ok_count": len(results["ok"]), "fail_count": len(results["fail"]),
            "total_bytes": results["bytes"], "elapsed": task["elapsed"],
            "folder": str(folder),
            "zip_path": str(zip_path) if zip_path else None,
            "finished_at": task["finished_at"], "cleaned": False,
        })
        history[:] = history[:50]
        save_history()

# ---------------- 临时文件清扫 ----------------
def _rm_task_paths(entry: dict):
    """删除条目指向的文件夹与 ZIP"""
    for p in (entry.get("zip_path"), entry.get("folder")):
        try:
            if p:
                pp = Path(p)
                if pp.is_dir():
                    shutil.rmtree(pp, ignore_errors=True)
                elif pp.exists():
                    pp.unlink(missing_ok=True)
        except Exception:
            pass

def sweep_temp_files():
    """清理超过保留期的临时文件/文件夹/ZIP
    (内存 tasks 与持久化 history 都清, 进程重启后历史残留同样过期删除)"""
    now = time.time()
    for t in list(tasks.values()):
        if t.get("cleaned") or not t.get("finished_at"):
            continue
        if now - t["finished_at"] < KEEP_SECONDS:
            continue
        _rm_task_paths(t)
        t["cleaned"] = True
    changed = False
    for h in history:
        if h.get("cleaned") or not h.get("finished_at"):
            continue
        if now - h["finished_at"] < KEEP_SECONDS:
            continue
        _rm_task_paths(h)
        h["cleaned"] = True
        changed = True
    if changed:
        save_history()

def _task_or_history(task_id: str) -> dict:
    """任务查询: 优先内存 tasks, 回退持久化 history(进程重启后 zip/文件仍可取)"""
    t = tasks.get(task_id)
    if t:
        return t
    for h in history:
        if h.get("id") == task_id:
            return h
    return {}

async def _sweep_loop():
    while True:
        await asyncio.sleep(300)
        try:
            sweep_temp_files()
        except Exception:
            pass

# ---------------- 应用 ----------------
_bg_started = False

async def startup_background():
    global _bg_started
    if _bg_started:
        return
    _bg_started = True
    asyncio.create_task(_sweep_loop())

@asynccontextmanager
async def lifespan(app: FastAPI):
    await startup_background()
    yield

app = FastAPI(title="bili", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

# ---------- 配置 ----------
class ConfigBody(BaseModel):
    cookie: str = ""

@app.get("/api/config")
async def api_get_config():
    cfg = load_config()
    cookie = cfg.get("cookie", "")
    analysis = analyze_cookie(cookie)
    masked = ""
    if cookie:
        masked = cookie[:24] + "..." + str(len(cookie)) + "字符"
    return {"has_cookie": bool(cookie), "masked": masked, "analysis": analysis}

@app.post("/api/config")
async def api_set_config(body: ConfigBody):
    cookie = body.cookie.strip()
    save_config({"cookie": cookie})
    # 顺带联网验证登录态(失败也保存, 匿名 720P 仍可用)
    result = {"ok": True, "saved": bool(cookie), "is_login": False,
              "uname": "", "vip": 0, "warn": ""}
    if cookie:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{API}/x/web-interface/nav",
                                       headers=headers(),
                                       timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    d = await resp.json(content_type=None)
            nd = d.get("data") or {}
            result["is_login"] = bool(nd.get("isLogin"))
            result["uname"] = nd.get("uname", "")
            result["vip"] = nd.get("vipStatus", 0)
            if not nd.get("isLogin"):
                result["warn"] = "Cookie 未通过登录校验(SESSDATA 可能已过期), 仅可用 720P 及以下清晰度"
        except Exception as e:
            result["warn"] = f"无法联网校验: {e}"
    return result

# ---------- 视频解析 ----------
class ParseVideoBody(BaseModel):
    url: str

def _extract_video_id(raw: str) -> tuple:
    """返回 (bvid, aid, p); 支持 BV/av 形态"""
    raw = raw.strip()
    m = re.search(r"(BV[0-9A-Za-z]{10})", raw)
    if m:
        p = re.search(r"[?&]p=(\d+)", raw)
        return m.group(1), "", int(p.group(1)) if p else 0
    m = re.search(r"(?:/video/|av)(\d+)", raw)
    if m:
        p = re.search(r"[?&]p=(\d+)", raw)
        return "", m.group(1), int(p.group(1)) if p else 0
    return "", "", 0

@app.post("/api/parse_video")
async def api_parse_video(body: ParseVideoBody):
    raw = body.url.strip()
    url = extract_bili_url(raw)
    if not url:
        raise HTTPException(400, "未找到 bilibili 链接, 请粘贴视频链接或整段分享文字")
    async with aiohttp.ClientSession() as session:
        if "b23.tv" in url:
            url = await resolve_short_url(session, url)
        cl = classify_url(url)
        if cl["kind"] != "video":
            raise HTTPException(400, "该链接不是视频链接(bilibili.com/video/BV…), 请检查")
        bvid, aid, p = _extract_video_id(url)
        if not bvid and not aid:
            raise HTTPException(400, "无法识别视频 ID")
        info = await fetch_view(session, bvid, aid)
        cid = info["pages"][0]["cid"] if info["pages"] else 0
        use_cookie = bool(cookie_header())
        try:
            opts = await probe_video(session, info["bvid"], cid, use_cookie)
        except HTTPException as e:
            opts = []
            if e.status_code != 502:
                raise
        if not opts:
            raise HTTPException(404, "未获取到可用清晰度(可能视频仅限大会员/已失效)")
    sel_p = p or 1
    pages = [{"page": pg["page"], "part": pg["part"], "duration": pg["duration"],
              "cid": pg["cid"]} for pg in info["pages"]]
    return {
        "bvid": info["bvid"], "aid": info["aid"],
        "title": info["title"], "pic": info["pic"],
        "duration": info["duration"],
        "owner": info["owner"],
        "pages": pages, "sel_p": sel_p,
        "quality": opts,
        "has_cookie": bool(cookie_header()),
        "is_login": analyze_cookie(cookie_header()).get("has_sessdata", False),
    }

# ---------- UP主空间 ----------
class ParseSpaceBody(BaseModel):
    url: str

@app.post("/api/parse_space")
async def api_parse_space(body: ParseSpaceBody):
    raw = body.url.strip()
    url = extract_bili_url(raw) or raw
    mid = space_mid(url)
    if not mid:
        raise HTTPException(400, "无法识别的空间链接, 请输入 space.bilibili.com/<数字ID>")
    # 清理超过 10 分钟的已完成抓取任务
    now = time.time()
    for k in [k for k, t in parse_tasks.items()
              if t.get("finished_at") and now - t["finished_at"] > 600]:
        del parse_tasks[k]
    task_id = gen_task_id()
    task = {
        "id": task_id, "url": url, "phase": "queued", "status": "running",
        "mid": "", "up_name": "", "up_face": "", "count": 0, "items": [],
        "error": "", "created_at": now, "finished_at": None,
    }
    parse_tasks[task_id] = task
    asyncio.create_task(run_parse_space(task, url))
    return {"task_id": task_id}

@app.get("/api/parse_tasks/{task_id}")
async def api_parse_task(task_id: str):
    t = parse_tasks.get(task_id)
    if not t:
        raise HTTPException(404, "抓取任务不存在")
    return t

# ---------- 下载 ----------
class DownloadBody(BaseModel):
    title: str = ""
    items: list = []       # [{bvid|aid, title, p?}]
    quality: int = 80
    need_zip: bool = True

@app.post("/api/download")
async def api_download(body: DownloadBody):
    if not body.items:
        raise HTTPException(400, "下载列表为空")
    if body.quality not in (16, 32, 64, 80, 112, 116, 120):
        body.quality = 80
    title = sanitize_filename(body.title) if body.title else f"bili_{int(time.time())}"
    task_id = gen_task_id()
    task = {
        "id": task_id, "title": body.title or title, "quality": body.quality,
        "total": len(body.items), "done": 0, "percent": 0,
        "phase": "queued", "status": "running", "status_text": "排队中…",
        "created_at": time.time(), "error": "",
        "ok_count": 0, "fail_count": 0, "total_bytes": 0,
        "elapsed": 0, "folder": "", "zip_path": None,
        "files": [], "errors": [], "clean_at": None, "cleaned": False,
    }
    folder = DOWNLOAD_DIR / title
    n = 1
    while folder.exists():
        folder = DOWNLOAD_DIR / f"{title}_{n}"
        n += 1
    task["folder"] = str(folder)
    tasks[task_id] = task
    asyncio.create_task(run_download(task, body.items, folder, body.need_zip))
    return {"task_id": task_id, "folder": str(folder)}

@app.get("/api/tasks/{task_id}")
async def api_task(task_id: str):
    sweep_temp_files()
    t = tasks.get(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    # 供前端判断文件是否还能取
    t["cleaned"] = bool(t.get("cleaned"))
    return t

@app.get("/api/history")
async def api_history():
    sweep_temp_files()
    return {"history": history}

@app.post("/api/history/clear")
async def api_history_clear():
    history.clear()
    save_history()
    return {"ok": True}

@app.get("/api/tasks/{task_id}/zip")
async def api_task_zip(task_id: str):
    t = _task_or_history(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    if t.get("cleaned") or not t.get("zip_path") or not os.path.isfile(t["zip_path"]):
        raise HTTPException(404, "ZIP 已清理(服务器仅临时中转, 任务完成后保留 1 小时)")
    return FileResponse(t["zip_path"], media_type="application/zip",
                        filename=os.path.basename(t["zip_path"]))

@app.get("/api/tasks/{task_id}/browse")
async def api_task_browse(task_id: str):
    t = _task_or_history(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    folder = Path(t["folder"])
    if not folder.is_dir():
        return {"files": [], "cleaned": True}
    files = [{"name": f.name, "size": f.stat().st_size}
             for f in sorted(folder.iterdir()) if f.is_file()]
    return {"folder": str(folder), "files": files, "title": t["title"],
            "cleaned": False}

@app.get("/api/tasks/{task_id}/files/{fname:path}")
async def api_task_file(task_id: str, fname: str):
    t = _task_or_history(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    fp = Path(t["folder"]) / unquote(fname)
    if t.get("cleaned") or not fp.is_file():
        raise HTTPException(404, "文件已清理(服务器仅临时中转, 任务完成后保留 1 小时)")
    return FileResponse(fp, filename=fp.name)

# ---------- 图片代理 (预览) ----------
@app.get("/api/img")
async def api_img(url: str):
    if not (url.startswith(("http://", "https://")) and _cdn_check(url)):
        raise HTTPException(400, "bad url")
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers(referer="https://www.bilibili.com/"),
                                   timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    raise HTTPException(502, f"图片获取失败 HTTP {resp.status}")
                ctype = resp.headers.get("Content-Type", "image/jpeg")
                data = await resp.read()
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"图片获取失败: {e}")
    return Response(data, media_type=ctype,
                    headers={"Cache-Control": "public, max-age=3600"})

# ---------- 实时单文件直链 (720P及以下, 前端直连下载用) ----------
@app.get("/api/durl")
async def api_durl(bvid: str = "", cid: int = 0, qn: int = 64):
    if not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid) or cid <= 0:
        raise HTTPException(400, "参数错误")
    if qn not in (16, 32, 64):
        qn = 64
    try:
        d = await _durl_direct(bvid, cid, qn)
        durl = d.get("durl") or []
        if not durl:
            raise HTTPException(404, "该视频无单文件流(可能仅限登录/会员)")
        return {"url": durl[0]["url"], "size": durl[0].get("size", 0),
                "quality": d.get("quality", 0)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"获取直链失败: {e}")

async def _durl_direct(bvid: str, cid: int, qn: int) -> dict:
    async with aiohttp.ClientSession() as session:
        return await fetch_playurl(session, bvid, cid, qn, dash=False)

# ---------- 流式代理 (单文件直连下载, 不落服务器磁盘) ----------
@app.get("/api/stream")
async def api_stream(url: str, name: str = ""):
    if not url.startswith(("http://", "https://")) or not _cdn_check(url):
        raise HTTPException(400, "仅允许 bilibili CDN 直链")
    if not name:
        name = os.path.basename(urlparse(url).path) or "download"
        name = unquote(name)
        if not name.endswith((".mp4", ".m4s", ".flv")):
            name += ".mp4"
    name = sanitize_filename(name, 120)

    async def gen():
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers={"User-Agent": UA,
                                                     "Referer": "https://www.bilibili.com/"},
                                       timeout=aiohttp.ClientTimeout(total=None,
                                                                     sock_read=180),
                                       allow_redirects=True) as resp:
                    if resp.status != 200:
                        yield f"下载失败: HTTP {resp.status}".encode()
                        return
                    async for chunk in resp.content.iter_chunked(1 << 16):
                        yield chunk
        except Exception as e:
            yield f"下载失败: {str(e)[:200]}".encode()

    return StreamingResponse(
        gen(), media_type="application/octet-stream",
        headers={"Content-Disposition":
                 f"attachment; filename*=UTF-8''{quote(name)}"},
    )

# ---------- 静态 ----------
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(str(FRONTEND_DIR / "index.html"))

if __name__ == "__main__":
    import uvicorn
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8811
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
