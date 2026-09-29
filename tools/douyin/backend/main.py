#!/usr/bin/env python3
"""
douyin - 抖音视频/图集/主页作品下载工具

功能:
  1. 单作品解析下载: 支持整段分享文案(含 v.douyin.com 短链)、www.douyin.com/video/<id>、
     modal_id 链接; 显示封面/时长/作者与多档清晰度(video.bit_rate);
     下载走服务器流式代理直推浏览器, 不落盘、不进历史
  2. 主页作品下载: 分享的「查看TA的更多作品」短链 / user/<sec_uid> 链接, 分页抓取全部作品
     (视频 + 图文), 文件夹与 ZIP 以作者昵称命名, 任务完成 1 小时后自动清理
设计约定(沿用 bili 工具):
  - 全部推送到浏览器/手机, 无「保存到服务器」选项; 服务器仅临时中转
  - 单作品 = 流式代理不落盘; 主页批量 = 服务器临时落盘 + ZIP + 1h 清理
关键实现:
  - 抖音 web 接口需要 a_bogus 签名(query 顺序必须与签名时一致) + ttwid/msToken cookie,
    见 backend/abogus.py (纯 Python, 无第三方依赖)
  - 匿名单作品/主页第一页可用; 主页分页(第 2 页起)需登录 Cookie(sessionid), 未配置时
    仅能拿到抖音匿名返回的部分作品并在界面提示
  - 视频直链带防盗链: 必须带 UA + Referer(https://www.douyin.com/), 因此下载统一走 /api/stream
"""
import asyncio
import json
import os
import random
import re
import shutil
import string
import sys
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, unquote, urlencode, urlparse

import aiohttp
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent))
from abogus import ABogus  # noqa: E402

# ---------------- 路径/常量 ----------------
ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = ROOT / "frontend"
DOWNLOAD_DIR = ROOT / "downloads"
CONFIG_FILE = ROOT / "config.json"
HISTORY_FILE = ROOT / "history.json"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
UA_MOBILE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1")
HOST = "https://www.douyin.com"
TTWID_API = "https://ttwid.bytedance.com/ttwid/union/register/"
REFERER = "https://www.douyin.com/"

CONCURRENCY = 3          # 下载并发
KEEP_SECONDS = 3600      # 任务完成后临时文件保留时长
PAGE_COUNT = 50          # 主页作品每页条数
MAX_PAGES = 60           # 主页作品最多翻页数
TTWID_TTL = 12 * 3600    # ttwid 缓存时长

# 允许代理/流式的域名后缀(防 SSRF)
CDN_SUFFIX = ("douyinvod.com", "douyinpic.com", "byteimg.com", "douyin.com",
              "bytedance.com", "ibytedtos.com", "byteintlapi.com", "pstatp.com",
              "snssdk.com", "ixigua.com", "byteicdn.com", "bytednsdoc.com",
              "douyinstatic.com", "volccdn.com", "amemv.com")

# 抖音 web 接口公共参数(顺序固定, 会进签名)
COMMON_PARAMS = {
    "device_platform": "webapp", "aid": "6383", "channel": "channel_pc_web",
    "pc_client_type": "1", "version_code": "190500", "version_name": "19.5.0",
    "cookie_enabled": "true", "screen_width": "1536", "screen_height": "864",
    "browser_language": "zh-CN", "browser_platform": "Win32",
    "browser_name": "Chrome", "browser_version": "125.0.0.0",
    "browser_online": "true", "engine_name": "Blink",
    "engine_version": "125.0.0.0", "os_name": "Windows", "os_version": "10",
    "cpu_core_num": "16", "device_memory": "8", "platform": "PC",
    "downlink": "10", "effective_type": "4g", "round_trip_time": "50",
}

# ---------------- 配置 ----------------
def load_config() -> dict:
    cfg = {"cookie": "", "ttwid": "", "ttwid_ts": 0}
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


def user_cookie() -> str:
    return (load_config().get("cookie") or "").strip()


def parse_cookie(raw: str) -> dict:
    fields = {}
    for seg in (raw or "").split(";"):
        seg = seg.strip()
        if not seg or "=" not in seg:
            continue
        k, v = seg.split("=", 1)
        fields[k.strip()] = v.strip()
    return fields


def analyze_cookie(raw: str) -> dict:
    """静态分析 cookie 关键字段(不联网)"""
    f = parse_cookie(raw)
    return {
        "count": len(f),
        "has_sessionid": "sessionid" in f,
        "has_ttwid": "ttwid" in f,
        "looks_complete": "sessionid" in f,
    }


def build_cookie_header(ms_token: str, ttwid: str) -> str:
    """用户 cookie + 本次请求的 msToken/ttwid(同名以本次请求为准)"""
    ck = parse_cookie(user_cookie())
    ck["msToken"] = ms_token
    ck["ttwid"] = ttwid
    return "; ".join(f"{k}={v}" for k, v in ck.items())


def gen_ms_token(n: int = 120) -> str:
    base = string.ascii_letters + string.digits + "="
    return "".join(random.choice(base) for _ in range(n))


def sanitize_filename(name: str, max_len: int = 90) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", str(name)).strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    return name[:max_len] if name else "untitled"


# ---------------- 全局状态 ----------------
tasks = {}          # 下载任务
parse_tasks = {}    # 主页抓取任务
history = []
_ttwid_lock = asyncio.Lock()


def load_history():
    global history
    if HISTORY_FILE.exists():
        try:
            history = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            history = []


def save_history():
    try:
        HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2),
                                encoding="utf-8")
    except Exception:
        pass


load_history()


# ---------------- ttwid ----------------
async def register_ttwid(session: aiohttp.ClientSession) -> str:
    body = {
        "region": "cn", "aid": 1768, "needFid": False,
        "service": "www.ixigua.com",
        "migrate_info": {"ticket": "", "source": "node"},
        "cbUrlProtocol": "https", "union": True,
    }
    async with session.post(TTWID_API, json=body,
                            headers={"User-Agent": UA,
                                     "Content-Type": "application/json"},
                            timeout=aiohttp.ClientTimeout(total=20)) as resp:
        sc = resp.headers.get("Set-Cookie", "") or ""
    m = re.search(r"ttwid=([^;]+)", sc)
    if not m:
        raise HTTPException(502, "获取 ttwid 失败(抖音接口防爬校验 token)")
    return m.group(1)


async def ensure_ttwid(session: aiohttp.ClientSession, force: bool = False) -> str:
    cfg = load_config()
    t, ts = cfg.get("ttwid", ""), cfg.get("ttwid_ts", 0)
    if not force and t and time.time() - ts < TTWID_TTL:
        return t
    async with _ttwid_lock:
        cfg = load_config()
        t, ts = cfg.get("ttwid", ""), cfg.get("ttwid_ts", 0)
        if not force and t and time.time() - ts < TTWID_TTL:
            return t
        t = await register_ttwid(session)
        save_config({"ttwid": t, "ttwid_ts": int(time.time())})
        return t


# ---------------- 签名请求 ----------------
async def dy_get(session: aiohttp.ClientSession, path: str, extra: dict,
                 referer: str = REFERER, retries: int = 3) -> dict:
    """带 a_bogus 签名的 GET; 空响应/风控码自动重试(必要时刷新 ttwid)"""
    params = dict(COMMON_PARAMS)
    params.update(extra)
    params["msToken"] = gen_ms_token()
    query = urlencode(params)
    last = ""
    for i in range(retries):
        ttwid = await ensure_ttwid(session, force=(i > 0))
        a_bogus = ABogus(UA).generate_a_bogus(query)   # 与上面的 urlencode 完全一致
        url = f"{HOST}{path}?{query}&a_bogus={quote(a_bogus, safe='')}"
        headers = {
            "User-Agent": UA, "Referer": referer,
            "Cookie": build_cookie_header(params["msToken"], ttwid),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9", "Sec-Fetch-Site": "same-origin",
        }
        try:
            async with session.get(url, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    last = f"HTTP {resp.status}"
                    await asyncio.sleep(1.5 * (i + 1))
                    continue
                body = await resp.text()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            await asyncio.sleep(1.5 * (i + 1))
            continue
        if not body.strip():
            last = "空响应(签名/风控校验未通过)"
            await asyncio.sleep(1.5 * (i + 1))
            continue
        try:
            d = json.loads(body)
        except Exception:
            last = f"响应非 JSON: {body[:120]}"
            await asyncio.sleep(1.5 * (i + 1))
            continue
        code = d.get("status_code", 0)
        if code in (0, None):
            return d
        last = f"status_code={code} {d.get('status_msg', '')}"
        if code in (-1, 8, 2154):
            await asyncio.sleep(1.5 * (i + 1))
            continue
        return d
    raise HTTPException(502, f"抖音接口请求失败: {path} -> {last}")


# ---------------- 链接识别 ----------------
def extract_douyin_url(text: str) -> str:
    """从整段分享文案中抽取抖音链接(短链/桌面链接)"""
    text = (text or "").strip()
    if re.fullmatch(r"https?://\S+", text):
        return text
    urls = re.findall(r"https?://[^\s\u4e00-\u9fff\"'<>，。；、）】]+", text)
    for u in urls:
        u = u.rstrip(".,;:!?)]}，。；、")
        if any(d in u for d in ("douyin.com", "iesdouyin.com", "douyin.com")):
            return u
    m = re.search(r"((?:v|m)\.douyin\.com/[A-Za-z0-9_\-]+)", text)
    return "https://" + m.group(1) if m else ""


def parse_target(url: str) -> dict:
    """识别链接类型: 作品(aweme_id) / 主页(sec_uid)"""
    u = url.strip()
    m = re.search(r"[?&]modal_id=(\d+)", u)
    if m:
        return {"kind": "video", "aweme_id": m.group(1)}
    m = re.search(r"/video/(\d+)", u)
    if m:
        return {"kind": "video", "aweme_id": m.group(1)}
    m = re.search(r"/share/video/(\d+)", u)
    if m:
        return {"kind": "video", "aweme_id": m.group(1)}
    m = re.search(r"/user/([A-Za-z0-9_\-]+)", u)
    if m:
        return {"kind": "user", "sec_uid": m.group(1)}
    m = re.search(r"/share/user/([A-Za-z0-9_\-]+)", u)
    if m:
        return {"kind": "user", "sec_uid": m.group(1)}
    m = re.search(r"[?&]sec_uid=([A-Za-z0-9_\-]+)", u)
    if m:
        return {"kind": "user", "sec_uid": m.group(1)}
    if re.fullmatch(r"MS4wLjABAAAA[A-Za-z0-9_\-]+", u):
        return {"kind": "user", "sec_uid": u}
    return {"kind": "unknown"}


async def resolve_share_url(session: aiohttp.ClientSession, url: str) -> str:
    """v.douyin.com 短链 -> 真实地址(手动跟随 302, 最多 6 跳)"""
    cur = url
    for _ in range(6):
        if parse_target(cur)["kind"] != "unknown":
            return cur
        try:
            async with session.get(cur, headers={"User-Agent": UA_MOBILE,
                                                 "Accept-Language": "zh-CN,zh;q=0.9"},
                                   allow_redirects=False,
                                   timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("Location") or ""
                    if not loc:
                        break
                    cur = "https://www.douyin.com" + loc if loc.startswith("/") else loc
                    continue
                break
        except Exception:
            break
    return cur


# ---------------- 数据抓取 ----------------
def _ms_to_sec(v) -> int:
    try:
        return int(round(int(v) / 1000))
    except Exception:
        return 0


def quality_options(detail: dict) -> list:
    """video.bit_rate -> 清晰度选项列表(按码率降序, 按 quality_type 去重)"""
    v = detail.get("video") or {}
    opts, seen = [], set()
    for br in v.get("bit_rate") or []:
        qt = br.get("quality_type")
        pa = br.get("play_addr") or {}
        if not pa.get("url_list") or qt is None or qt in seen:
            continue
        seen.add(qt)
        h = pa.get("height") or br.get("height") or 0
        w = pa.get("width") or br.get("width") or 0
        label = _quality_label(h, w)
        size = pa.get("data_size") or br.get("data_size") or 0
        rate = br.get("bit_rate") or 0
        opts.append({
            "quality_type": qt, "gear": br.get("gear_name", ""),
            "label": label, "width": w, "height": h,
            "bitrate": rate, "size": size,
        })
    opts.sort(key=lambda o: (o["bitrate"] or 0), reverse=True)
    # 默认档(play_addr) 作为兜底
    if not opts and (v.get("play_addr") or {}).get("url_list"):
        pa = v["play_addr"]
        opts.append({"quality_type": 0, "gear": "default",
                     "label": _quality_label(pa.get("height") or 0, pa.get("width") or 0),
                     "width": pa.get("width") or 0, "height": pa.get("height") or 0,
                     "bitrate": 0, "size": pa.get("data_size") or 0})
    return opts


def _quality_label(h: int, w: int = 0) -> str:
    """按短边命名(竖屏视频 height 是长边, 用短边才是用户认知的 1080P/720P)"""
    short = min([x for x in (h, w) if x]) if (h or w) else 0
    if not short:
        return "默认"
    if short >= 2160:
        return "4K"
    if short >= 1440:
        return "2K"
    return f"{short}P"


def pick_bit_rate(detail: dict, quality_type: int, max_height: int = 0) -> dict:
    """下载时按 quality_type 选流; quality_type=0 时按 max_height 上限取最高码率;
    max_height=0 表示不限(取最高); 都拿不到则退回默认 play_addr"""
    v = detail.get("video") or {}
    brs = [b for b in (v.get("bit_rate") or [])
           if (b.get("play_addr") or {}).get("url_list")]
    if quality_type:
        for br in brs:
            if br.get("quality_type") == quality_type:
                return _stream_of(br["play_addr"], quality_type)
        cand = sorted(brs, key=lambda b: b.get("bit_rate") or 0, reverse=True)
        if cand:
            return _stream_of(cand[0]["play_addr"], cand[0].get("quality_type"))
    elif brs:
        pool = [b for b in brs
                if not max_height or (b["play_addr"].get("height") or 0) <= max_height]
        pool = pool or brs
        best = max(pool, key=lambda b: b.get("bit_rate") or 0)
        return _stream_of(best["play_addr"], best.get("quality_type"))
    pa = v.get("play_addr") or {}
    urls = pa.get("url_list") or []
    return {"url": urls[0] if urls else "", "urls": urls,
            "size": pa.get("data_size") or 0, "quality_type": 0}


def _stream_of(pa: dict, quality_type) -> dict:
    urls = pa.get("url_list") or []
    return {"url": urls[0] if urls else "", "urls": urls,
            "size": pa.get("data_size") or 0, "quality_type": quality_type or 0}


def image_urls(detail: dict) -> list:
    """图文作品图片地址(优先无水印 url_list)"""
    urls = []
    for img in detail.get("images") or []:
        cand = img.get("url_list") or img.get("download_url_list") or []
        if cand:
            urls.append(cand[0])
    if not urls:
        for img in detail.get("image_infos") or []:
            cand = img.get("url_list") or []
            if cand:
                urls.append(cand[0])
    return urls


def normalize_item(aweme: dict) -> dict:
    """主页作品条目 -> 前端卡片数据"""
    v = aweme.get("video") or {}
    imgs = aweme.get("images") or aweme.get("image_infos") or []
    cover = ""
    for src in ((v.get("cover") or {}).get("url_list"),
                (v.get("origin_cover") or {}).get("url_list")):
        if src:
            cover = src[0]
            break
    if not cover and imgs:
        cover = (imgs[0].get("url_list") or [""])[0]
    return {
        "aweme_id": aweme.get("aweme_id", ""),
        "title": aweme.get("desc", "") or "",
        "cover": cover,
        "duration": _ms_to_sec(aweme.get("duration") or v.get("duration") or 0),
        "create_time": aweme.get("create_time", 0),
        "kind": "image" if (aweme.get("aweme_type") == 68 or imgs) else "video",
        "image_count": len(imgs),
        "digg": (aweme.get("statistics") or {}).get("digg_count", 0),
        "comment": (aweme.get("statistics") or {}).get("comment_count", 0),
        "collect": (aweme.get("statistics") or {}).get("collect_count", 0),
    }


async def fetch_detail(session: aiohttp.ClientSession, aweme_id: str) -> dict:
    d = await dy_get(session, "/aweme/v1/web/aweme/detail/", {"aweme_id": aweme_id},
                     referer=f"{HOST}/video/{aweme_id}")
    detail = d.get("aweme_detail")
    if not detail:
        fd = d.get("filter_detail") or {}
        reason = fd.get("filter_reason") or fd.get("detail_msg") or "作品不可用"
        raise HTTPException(404, f"作品数据不可用({reason})")
    return detail


async def fetch_profile(session: aiohttp.ClientSession, sec_uid: str) -> dict:
    """作者信息(匿名可用)"""
    try:
        d = await dy_get(session, "/aweme/v1/web/user/profile/other/",
                         {"sec_user_id": sec_uid, "publish_video_strategy_type": "2",
                          "personal_center_strategy": "1"},
                         referer=f"{HOST}/user/{sec_uid}")
    except HTTPException:
        return {}
    u = d.get("user") or {}
    avatar = ((u.get("avatar_thumb") or {}).get("url_list") or [""])[0]
    return {"nickname": u.get("nickname", ""), "unique_id": u.get("unique_id", ""),
            "avatar": avatar, "aweme_count": u.get("aweme_count", 0),
            "signature": u.get("signature", ""), "uid": u.get("uid", "")}


async def fetch_post_page(session: aiohttp.ClientSession, sec_uid: str, cursor,
                          count: int = PAGE_COUNT):
    """主页作品一页 -> (items, has_more, next_cursor)"""
    d = await dy_get(session, "/aweme/v1/web/aweme/post/",
                     {"sec_user_id": sec_uid, "max_cursor": str(cursor),
                      "count": str(count), "locate_query": "false",
                      "show_live_replay_strategy": "1", "need_time_list": "1",
                      "time_list_query": "0", "whale_cut_token": "",
                      "cut_version": "1", "publish_video_strategy_type": "2"},
                     referer=f"{HOST}/user/{sec_uid}")
    items = d.get("aweme_list") or []
    return items, bool(d.get("has_more")), d.get("max_cursor")


# ---------------- 主页抓取任务 ----------------
async def run_parse_user(task: dict, sec_uid: str):
    """后台分页抓取主页全部作品(任务制, 手机切后台不中断)"""
    try:
        task.update(phase="fetching", sec_uid=sec_uid)
        all_items, seen = [], set()
        has_more = False
        cursor = 0
        async with aiohttp.ClientSession() as session:
            profile = await fetch_profile(session, sec_uid)
            if profile.get("nickname"):
                task.update(up_name=profile["nickname"], up_face=profile.get("avatar", ""),
                            aweme_total=profile.get("aweme_count", 0),
                            unique_id=profile.get("unique_id", ""))
            for page in range(MAX_PAGES):
                items, has_more, cursor = await fetch_post_page(session, sec_uid, cursor)
                if not items:
                    break
                for aw in items:
                    aid = aw.get("aweme_id")
                    if not aid or aid in seen:
                        continue
                    seen.add(aid)
                    all_items.append(normalize_item(aw))
                task.update(count=len(all_items))
                if not has_more:
                    break
                await asyncio.sleep(0.5)
        if not all_items:
            who = task.get("up_name") or "该账号"
            extra = "" if user_cookie() else " (未配置 Cookie 时抖音通常只返回首页作品)"
            task.update(status="error",
                        error=f"未抓取到任何作品: {who} 可能无公开作品/受保护{extra}")
            return
        truncated = has_more
        warn = ""
        if truncated:
            if user_cookie():
                warn = f"翻页在第 {MAX_PAGES} 页上限停止或接口提前结束, 共抓取 {len(all_items)} 条"
            else:
                total = task.get("aweme_total") or 0
                warn = (f"未配置登录 Cookie: 抖音匿名只返回前 {len(all_items)} 条作品"
                        + (f"(该账号共 {total} 条)" if total else "")
                        + ", 到「Cookie 设置」粘贴网页版登录 Cookie 后可抓取全部")
        task.update({
            "phase": "done", "status": "done",
            "count": len(all_items), "items": all_items,
            "truncated": truncated, "warn": warn,
            "has_cookie": bool(user_cookie()),
            "finished_at": time.time(),
        })
    except HTTPException as e:
        task.update(status="error", error=str(e.detail))
    except Exception as e:
        task.update(status="error", error=f"抓取失败: {e}")


# ---------------- 单作品解析 ----------------
def video_payload(detail: dict) -> dict:
    v = detail.get("video") or {}
    author = detail.get("author") or {}
    cover = ((v.get("cover") or {}).get("url_list") or [""])[0]
    imgs = image_urls(detail)
    kind = "image" if (detail.get("aweme_type") == 68 or imgs) else "video"
    return {
        "aweme_id": detail.get("aweme_id", ""),
        "title": detail.get("desc", "") or "",
        "cover": cover,
        "duration": _ms_to_sec(detail.get("duration") or v.get("duration") or 0),
        "create_time": detail.get("create_time", 0),
        "kind": kind,
        "image_count": len(imgs),
        "images": imgs,
        "author": {"name": author.get("nickname", ""),
                   "unique_id": author.get("unique_id", ""),
                   "sec_uid": author.get("sec_uid", ""),
                   "avatar": ((author.get("avatar_thumb") or {}).get("url_list") or [""])[0]},
        "quality": quality_options(detail) if kind == "video" else [],
        "statistics": {"digg": (detail.get("statistics") or {}).get("digg_count", 0),
                       "comment": (detail.get("statistics") or {}).get("comment_count", 0),
                       "collect": (detail.get("statistics") or {}).get("collect_count", 0)},
    }


# ---------------- 下载 ----------------
def gen_task_id():
    return f"{int(time.time() * 1000)}"


async def _download_to(session: aiohttp.ClientSession, url: str, dest: Path,
                       referer: str = REFERER) -> int:
    """流式下载到 dest.part 再改名(防盗链带 UA + Referer + Cookie)"""
    headers = {"User-Agent": UA, "Referer": referer,
               "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9"}
    ck = user_cookie()
    if ck:
        headers["Cookie"] = ck
    tmp = dest.with_suffix(dest.suffix + ".part")
    size = 0
    async with session.get(url, headers=headers, allow_redirects=True,
                           timeout=aiohttp.ClientTimeout(total=None,
                                                         sock_read=180)) as resp:
        if resp.status not in (200, 206):
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


def _img_ext(url: str) -> str:
    path = urlparse(url).path.lower()
    for ext in (".jpeg", ".jpg", ".png", ".webp", ".heic"):
        if path.endswith(ext):
            return ".jpg" if ext in (".jpeg", ".jpg") else ext
    return ".jpg"


async def download_item(session: aiohttp.ClientSession, item: dict, folder: Path,
                        results: dict, used_names: set, task: dict, total: int):
    """下载单个作品(视频 或 图集)"""
    aweme_id = item.get("aweme_id", "")
    title = sanitize_filename(item.get("title") or f"douyin_{aweme_id}")
    referer = f"{HOST}/video/{aweme_id}"
    try:
        detail = await fetch_detail(session, aweme_id)
        if item.get("kind") == "image" or image_urls(detail):
            urls = image_urls(detail)
            if not urls:
                raise Exception("图文作品无图片地址")
            for idx, u in enumerate(urls, 1):
                fname = f"{title}_{idx:02d}{_img_ext(u)}"
                k = 2
                while fname in used_names:
                    fname = f"{title}_{idx:02d}_{k}{_img_ext(u)}"
                    k += 1
                used_names.add(fname)
                dest = folder / fname
                if dest.exists():
                    raise Exception("文件已存在")
                await _download_to(session, u, dest, referer)
                results["bytes"] += dest.stat().st_size
            results["ok"].append(f"{title}(图集 {len(urls)} 张)")
        else:
            qtype = int(task.get("quality") or 0)
            stream = pick_bit_rate(detail, qtype, int(task.get("max_height") or 0))
            if not stream.get("url"):
                raise Exception("未获取到播放地址")
            fname = f"{title}.mp4"
            k = 2
            while fname in used_names:
                fname = f"{title}_{k}.mp4"
                k += 1
            used_names.add(fname)
            dest = folder / fname
            if dest.exists():
                raise Exception("文件已存在")
            try:
                await _download_to(session, stream["url"], dest, referer)
            except Exception:
                # 备用地址重试一次
                for alt in (stream.get("urls") or [])[1:2]:
                    await _download_to(session, alt, dest, referer)
                    break
                else:
                    raise
            results["bytes"] += dest.stat().st_size
            results["ok"].append(fname)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        results["fail"].append({"name": (item.get("title") or aweme_id)[:40],
                                "error": str(e)[:200]})
    finally:
        task["done"] += 1
        task["percent"] = round(task["done"] / total * 100, 1) if total else 100


async def run_download(task: dict, items: list, folder: Path, need_zip: bool):
    """下载任务主流程: 逐个作品实时取直链下载 -> 可选 ZIP -> 定清理时间"""
    t0 = time.time()
    folder.mkdir(parents=True, exist_ok=True)
    results = {"ok": [], "fail": [], "bytes": 0}
    used_names = set()
    try:
        total = len(items)
        task.update(total=total, done=0, phase="downloading",
                    status_text="实时获取直链中…")
        sem = asyncio.Semaphore(CONCURRENCY)

        async with aiohttp.ClientSession() as session:
            async def guarded(it):
                async with sem:
                    await download_item(session, it, folder, results, used_names,
                                        task, total)

            await asyncio.gather(*[guarded(it) for it in items])
        if not results["ok"]:
            raise Exception("全部作品下载失败: " +
                            "; ".join(f"{f['name']}: {f['error']}" for f in results["fail"][:3]))
        zip_path = None
        if need_zip:
            task.update(phase="packing", status_text="正在打包 ZIP…")
            zip_path = folder.parent / (folder.name + ".zip")
            loop = asyncio.get_event_loop()

            def _zip():
                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
                    for f in sorted(folder.iterdir()):
                        if f.is_file():
                            zf.write(f, arcname=f.name)

            await loop.run_in_executor(None, _zip)
    except Exception as e:
        task.update(phase="error", status="error", error=str(e)[:300],
                    finished_at=time.time())
        return
    task.update({
        "phase": "done", "status": "done",
        "ok_count": len(results["ok"]), "fail_count": len(results["fail"]),
        "total_bytes": results["bytes"], "elapsed": round(time.time() - t0, 1),
        "folder": str(folder), "zip_path": str(zip_path) if zip_path else None,
        "files": results["ok"], "errors": results["fail"],
        "clean_at": time.time() + KEEP_SECONDS, "cleaned": False,
        "finished_at": time.time(),
    })
    if results["ok"]:
        history.insert(0, {
            "id": task["id"], "title": task["title"],
            "ok_count": len(results["ok"]), "fail_count": len(results["fail"]),
            "total_bytes": results["bytes"], "elapsed": task["elapsed"],
            "folder": str(folder), "zip_path": str(zip_path) if zip_path else None,
            "finished_at": task["finished_at"], "cleaned": False,
        })
        history[:] = history[:50]
        save_history()


# ---------------- 临时文件清扫 ----------------
def _rm_task_paths(entry: dict):
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


app = FastAPI(title="douyin", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])


# ---------- 配置 ----------
class ConfigBody(BaseModel):
    cookie: str = ""


@app.get("/api/config")
async def api_get_config():
    cfg = load_config()
    cookie = (cfg.get("cookie") or "").strip()
    a = analyze_cookie(cookie)
    masked = (cookie[:24] + "..." + str(len(cookie)) + "字符") if cookie else ""
    return {"has_cookie": bool(cookie), "masked": masked, "analysis": a,
            "has_ttwid": bool(cfg.get("ttwid")),
            "ttwid_ts": cfg.get("ttwid_ts", 0)}


@app.post("/api/config")
async def api_set_config(body: ConfigBody):
    cookie = (body.cookie or "").strip()
    save_config({"cookie": cookie})
    a = analyze_cookie(cookie)
    result = {"ok": True, "saved": bool(cookie), "is_login": a["has_sessionid"],
              "warn": ""}
    if cookie and not a["has_sessionid"]:
        result["warn"] = ("Cookie 缺少 sessionid 字段, 可能不是登录后的完整 Cookie, "
                          "主页分页仍会受限")
    # 用当前配置试抓一次当前账号主页第一页, 验证 cookie 是否有效
    if cookie:
        try:
            async with aiohttp.ClientSession() as session:
                ttwid = await register_ttwid(session)
                save_config({"ttwid": ttwid, "ttwid_ts": int(time.time())})
        except Exception as e:
            result["warn"] = (result["warn"] + " " if result["warn"] else "") + \
                f"ttwid 获取失败: {e}"
    return result


# ---------- 单作品解析 ----------
class ParseBody(BaseModel):
    url: str


@app.post("/api/parse_video")
async def api_parse_video(body: ParseBody):
    raw = (body.url or "").strip()
    url = extract_douyin_url(raw)
    if not url:
        raise HTTPException(400, "未找到抖音链接, 请粘贴分享文案或视频链接")
    async with aiohttp.ClientSession() as session:
        if "v.douyin.com" in url or "m.douyin.com/share" in url:
            url = await resolve_share_url(session, url)
        tgt = parse_target(url)
        if tgt["kind"] != "video":
            raise HTTPException(400, "该链接不是作品链接(可能是主页链接), "
                                     "请用「主页作品下载」模式; 或检查链接是否完整")
        detail = await fetch_detail(session, tgt["aweme_id"])
        payload = video_payload(detail)
    payload["has_cookie"] = bool(user_cookie())
    return payload


# ---------- 主页作品抓取 ----------
@app.post("/api/parse_user")
async def api_parse_user(body: ParseBody):
    raw = (body.url or "").strip()
    url = extract_douyin_url(raw) or raw
    async with aiohttp.ClientSession() as session:
        if "v.douyin.com" in url:
            url = await resolve_share_url(session, url)
    tgt = parse_target(url)
    if tgt["kind"] != "user":
        raise HTTPException(400, "无法识别主页链接, 请输入分享的「查看TA的更多作品」"
                                 "短链或 douyin.com/user/<sec_uid>")
    now = time.time()
    for k in [k for k, t in parse_tasks.items()
              if t.get("finished_at") and now - t["finished_at"] > 600]:
        del parse_tasks[k]
    task_id = gen_task_id()
    task = {
        "id": task_id, "url": url, "phase": "queued", "status": "running",
        "sec_uid": tgt["sec_uid"], "up_name": "", "up_face": "", "unique_id": "",
        "aweme_total": 0, "count": 0, "items": [], "truncated": False, "warn": "",
        "has_cookie": bool(user_cookie()), "error": "",
        "created_at": now, "finished_at": None,
    }
    parse_tasks[task_id] = task
    asyncio.create_task(run_parse_user(task, tgt["sec_uid"]))
    return {"task_id": task_id}


@app.get("/api/parse_tasks/{task_id}")
async def api_parse_task(task_id: str):
    t = parse_tasks.get(task_id)
    if not t:
        raise HTTPException(404, "抓取任务不存在")
    return t


# ---------- 下载任务 ----------
class DownloadBody(BaseModel):
    title: str = ""
    items: list = []       # [{aweme_id, title, kind}]
    quality: int = 0       # quality_type, 0 = 默认(按 max_height 取最高)
    max_height: int = 0    # quality=0 时的清晰度上限(0 = 不限)
    need_zip: bool = True


@app.post("/api/download")
async def api_download(body: DownloadBody):
    if not body.items:
        raise HTTPException(400, "下载列表为空")
    title = sanitize_filename(body.title) if body.title else f"douyin_{int(time.time())}"
    task_id = gen_task_id()
    task = {
        "id": task_id, "title": body.title or title,
        "quality": int(body.quality or 0), "max_height": int(body.max_height or 0),
        "total": len(body.items), "done": 0, "percent": 0,
        "phase": "queued", "status": "running", "status_text": "排队中…",
        "created_at": time.time(), "error": "",
        "ok_count": 0, "fail_count": 0, "total_bytes": 0, "elapsed": 0,
        "folder": "", "zip_path": None, "files": [], "errors": [],
        "clean_at": None, "cleaned": False,
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
    return {"folder": str(folder), "files": files, "title": t["title"], "cleaned": False}


@app.get("/api/tasks/{task_id}/files/{fname:path}")
async def api_task_file(task_id: str, fname: str):
    t = _task_or_history(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    fp = Path(t["folder"]) / unquote(fname)
    if t.get("cleaned") or not fp.is_file():
        raise HTTPException(404, "文件已清理(服务器仅临时中转, 任务完成后保留 1 小时)")
    return FileResponse(fp, filename=fp.name)


# ---------- 单作品直链(浏览器直下用) ----------
@app.get("/api/direct")
async def api_direct(aweme_id: str = "", quality_type: int = 0, max_height: int = 0):
    """返回可直连下载的地址列表(视频 1 条 / 图集多条)"""
    if not re.fullmatch(r"\d{6,25}", aweme_id or ""):
        raise HTTPException(400, "参数错误")
    async with aiohttp.ClientSession() as session:
        detail = await fetch_detail(session, aweme_id)
    v = detail.get("video") or {}
    title = sanitize_filename(detail.get("desc") or f"douyin_{aweme_id}")
    imgs = image_urls(detail)
    if detail.get("aweme_type") == 68 or imgs:
        if not imgs:
            raise HTTPException(404, "图文作品无图片地址")
        files = [{"url": u, "name": f"{title}_{i:02d}{_img_ext(u)}"}
                 for i, u in enumerate(imgs, 1)]
        return {"kind": "image", "files": files, "quality_type": 0}
    stream = pick_bit_rate(detail, int(quality_type or 0), int(max_height or 0))
    if not stream.get("url"):
        raise HTTPException(404, "未获取到播放地址")
    return {"kind": "video", "quality_type": stream["quality_type"],
            "size": stream.get("size", 0),
            "files": [{"url": stream["url"], "name": f"{title}.mp4"}]}


# ---------- 流式代理(不落盘) ----------
def _cdn_check(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == s or host.endswith("." + s) for s in CDN_SUFFIX)


@app.get("/api/stream")
async def api_stream(url: str, name: str = ""):
    if not url.startswith(("http://", "https://")) or not _cdn_check(url):
        raise HTTPException(400, "仅允许抖音 CDN 直链")
    if not name:
        name = unquote(os.path.basename(urlparse(url).path)) or "download"
    name = sanitize_filename(name, 120)

    async def gen():
        headers = {"User-Agent": UA, "Referer": REFERER, "Accept": "*/*"}
        ck = user_cookie()
        if ck:
            headers["Cookie"] = ck
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, allow_redirects=True,
                                       timeout=aiohttp.ClientTimeout(total=None,
                                                                     sock_read=180)) as resp:
                    if resp.status not in (200, 206):
                        yield f"下载失败: HTTP {resp.status}".encode()
                        return
                    async for chunk in resp.content.iter_chunked(1 << 16):
                        yield chunk
        except Exception as e:
            yield f"下载失败: {str(e)[:200]}".encode()

    return StreamingResponse(
        gen(), media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"},
    )


# ---------- 图片代理(封面等) ----------
@app.get("/api/img")
async def api_img(url: str):
    if not (url.startswith(("http://", "https://")) and _cdn_check(url)):
        raise HTTPException(400, "bad url")
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers={"User-Agent": UA, "Referer": REFERER},
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


# ---------- 静态 ----------
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(str(FRONTEND_DIR / "index.html"))


if __name__ == "__main__":
    import uvicorn
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8812
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
