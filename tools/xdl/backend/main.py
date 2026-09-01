#!/usr/bin/env python3
"""
xdl - X (Twitter) 视频/媒体下载工具
功能:
  1. 视频链接解析: 输入 x.com/.../status/xxx/video/1 或 status 链接, 匿名解析全部清晰度并下载
  2. 媒体时间线抓取: 输入 x.com/user/media 页面, 需要 cookie 认证,
     提取时间线中所有图片(高清化流程)和视频
"""
import asyncio
import json
import os
import re
import shutil
import sys
import time
import zipfile
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

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
GQL_CACHE_FILE = ROOT / "gql_cache.json"
HISTORY_FILE = ROOT / "history.json"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
UA_FX = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
CONCURRENCY = 4

# X 内部 GraphQL API 的公开 Bearer token (web 客户端固定值)
X_BEARER = ("AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
            "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA")

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

# ---------------- 全局状态 ----------------
tasks = {}          # 下载任务
parse_tasks = {}    # 媒体时间线抓取任务 (异步, 手机切后台不中断)
executor = ThreadPoolExecutor(max_workers=2)
history = []  # 已完成任务记录

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
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name).strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    if not name:
        name = "untitled"
    return name[:max_len]

def parse_status_id(url: str) -> str:
    """从任意 x.com 链接提取 status id"""
    m = re.search(r"/(?:status|statuses)/(\d+)", url)
    return m.group(1) if m else ""

def is_x_url(url: str) -> bool:
    return bool(re.match(r"https?://(www\.)?(x\.com|twitter\.com)/", url))

def normalize_x_url(url: str) -> str:
    """把 twitter.com 换成 x.com, 去掉 /video/1 尾巴"""
    url = re.sub(r"^https?://(www\.)?twitter\.com/", "https://x.com/", url)
    url = re.sub(r"/video/\d+/?$", "", url)  # /video/1 -> status 页
    url = url.rstrip("/")
    return url

def extract_video_variants(html: str) -> list:
    """从 status 页 HTML 提取所有视频清晰度变体"""
    variants = []
    # 方法1: bitrate:xxx,content_type:"video/mp4",url:"..."
    for m in re.finditer(r'bitrate:(\d+),content_type:"video/mp4",url:"([^"]+)"', html):
        br, url = int(m.group(1)), m.group(2)
        variants.append({"bitrate": br, "url": url})
    # 方法2: meta contentUrl (最高清晰度)
    m = re.search(r'itemProp="contentUrl"/><meta content="([^"]+\.mp4[^"]*)"', html)
    if m and m.group(1) not in [v["url"] for v in variants]:
        variants.append({"bitrate": 0, "url": m.group(1)})
    # 去重
    seen, out = set(), []
    for v in variants:
        if v["url"] not in seen:
            seen.add(v["url"])
            out.append(v)
    out.sort(key=lambda v: v["bitrate"], reverse=True)
    return out

def extract_thumb(html: str) -> str:
    patterns = [
        r'itemProp="thumbnailUrl"[^>]*content="([^"]+)"',
        r'itemProp="thumb[^"]*"[^>]*content="([^"]+)"',
        r'<meta content="([^"]+)"[^>]*itemProp="thumbnailUrl"',
        r'property="og:image" content="([^"]+)"',
    ]
    for pat in patterns:
        m = re.search(pat, html)
        if m and "pbs.twimg.com" in m.group(1):
            return m.group(1)
    return ""

def extract_page_title(html: str) -> str:
    m = re.search(r'<meta property="og:title" content="([^"]+)"', html) or \
        re.search(r'<title>([^<]+)</title>', html)
    if m:
        t = m.group(1).strip()
        t = re.sub(r"\s*on X\s*$", "", t)
        t = re.sub(r"\s*/\s*X\s*$", "", t)
        return t
    return ""

def check_login_required(html: str) -> bool:
    """判断页面是否需要登录 (空壳页特征)"""
    return ("aria-label=\"时间线" not in html and "INITIAL_STATE" in html
            and "<form" not in html and "js-login" in html)

def extract_cookie_header(raw_cookie: str) -> str:
    """从完整 cookie 字符串提取关键字段, 减小体积"""
    if not raw_cookie:
        return ""
    keep = ("auth_token", "ct0", "twid", "kdt", "guest_id", "lang",
            "personalization_id", "session")
    parts = []
    for seg in raw_cookie.split(";"):
        seg = seg.strip()
        if not seg:
            continue
        key = seg.split("=", 1)[0].strip()
        if key in keep:
            parts.append(seg)
    # 至少保留原始完整串 (若过滤后为空)
    return "; ".join(parts) if parts else raw_cookie

def analyze_cookie(raw_cookie: str) -> dict:
    """分析 cookie 完整性, 返回字段清单和缺失的关键字段"""
    if not raw_cookie:
        return {"present": [], "missing": ["auth_token", "ct0", "twid", "kdt"],
                "count": 0, "looks_complete": False, "has_auth": False}
    fields = {}
    for seg in raw_cookie.split(";"):
        seg = seg.strip()
        if not seg or "=" not in seg:
            continue
        k, v = seg.split("=", 1)
        fields[k.strip()] = v.strip()
    required = ["auth_token", "ct0"]
    important = ["twid", "kdt"]
    missing = [f for f in required + important if f not in fields]
    return {
        "present": sorted(fields.keys()),
        "missing": missing,
        "count": len(fields),
        "has_auth": "auth_token" in fields,
        "has_ct0": "ct0" in fields,
        "looks_complete": bool(missing) is False,
        "looks_logged_in": "auth_token" in fields and "ct0" in fields and "twid" in fields,
    }

# ---------------- X GraphQL API ----------------
def _gql_headers(cookie: str, ct0: str, referer: str) -> dict:
    return {
        "User-Agent": UA_FX,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.8",
        "Cookie": cookie,
        "X-CSRF-Token": ct0,
        "Authorization": f"Bearer {X_BEARER}",
        "Content-Type": "application/json",
        "x-twitter-active-user": "yes",
        "x-twitter-client-language": "zh-cn",
        "Origin": "https://x.com",
        "Referer": referer,
    }

def _load_gql_cache() -> dict:
    if GQL_CACHE_FILE.exists():
        try:
            return json.loads(GQL_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def _save_gql_cache(cache: dict):
    try:
        GQL_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

async def fetch_main_bundle(session: aiohttp.ClientSession) -> str:
    """下载 X 主 JS bundle, 提取 queryId 与 featureSwitches"""
    # 尝试多个页面源找 bundle URL (首页可能被风控)
    headers = {"User-Agent": UA_FX, "Accept": "text/html,application/xhtml+xml"}
    bundle_url = ""
    for page in ("https://x.com/", "https://x.com/tuzisxbb21"):
        try:
            async with session.get(page, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=30)) as resp:
                html = await resp.text()
            m = re.search(r'https://abs\.twimg\.com/responsive-web/client-web/main\.[a-f0-9]+\.js', html)
            if m:
                bundle_url = m.group(0)
                break
        except Exception:
            continue
    if not bundle_url:
        return ""
    async with session.get(bundle_url, headers={"User-Agent": UA_FX},
                           timeout=aiohttp.ClientTimeout(total=60)) as resp:
        return await resp.text()

def parse_bundle_operations(js: str) -> dict:
    """从 bundle 提取所有 queryId -> {operationName, featureSwitches}"""
    ops = {}
    for m in re.finditer(
        r'queryId:"([^"]+)",operationName:"([^"]+)",operationType:"[^"]+",metadata:\{'
        r'featureSwitches:\[([^\]]*)\]', js):
        qid, name, fs_raw = m.group(1), m.group(2), m.group(3)
        switches = re.findall(r'"([^"]+)"', fs_raw)
        ops[name] = {"queryId": qid, "featureSwitches": switches}
    return ops

async def ensure_gql_schema(session: aiohttp.ClientSession) -> dict:
    """确保 queryId/features 可用, 从缓存或 bundle 提取"""
    cache = _load_gql_cache()
    now = time.time()
    if cache.get("updated_at") and (now - cache["updated_at"] < 86400) and cache.get("ops"):
        return cache
    js = await fetch_main_bundle(session)
    ops = parse_bundle_operations(js) if js else {}
    if not ops:
        return cache  # 提取失败, 用旧缓存
    # 校验: 必须包含关键操作才算有效 bundle
    if not all(k in ops for k in ("TweetDetail", "UserMedia", "UserByScreenName")):
        return cache
    # 每个 operation 的 features 默认值: 从 bundle 中查 "switch":true/false
    feats_defaults = {}
    for name, op in ops.items():
        for sw in op["featureSwitches"]:
            if sw in feats_defaults:
                continue
            m = re.search(r'"' + re.escape(sw) + r'":(true|false)', js)
            feats_defaults[sw] = (m.group(1) == "true") if m else True
    cache = {"updated_at": now, "ops": ops, "features": feats_defaults}
    _save_gql_cache(cache)
    return cache

async def gql_request(session: aiohttp.ClientSession, op_name: str,
                      variables: dict, referer: str = "https://x.com/") -> dict:
    """调用 X 内部 GraphQL API"""
    cfg = load_config()
    cookie = cfg.get("cookie", "")
    if not cookie:
        raise HTTPException(401, "需要登录认证: 请配置完整 Cookie (含 auth_token/ct0/twid)")
    ct0_m = re.search(r"(?:^|;\s*)ct0=([^;]+)", cookie)
    if not ct0_m:
        raise HTTPException(401, "Cookie 缺少 ct0 字段")
    schema = await ensure_gql_schema(session)
    ops = schema.get("ops", {})
    op = ops.get(op_name)
    if not op:
        raise HTTPException(502, f"GraphQL 操作 {op_name} 的 queryId 未知, 请刷新")
    feats = schema.get("features", {})
    url = (f"https://x.com/i/api/graphql/{op['queryId']}/{op_name}?variables=" +
           quote(json.dumps(variables, separators=(",", ":"))) +
           "&features=" + quote(json.dumps(feats, separators=(",", ":"))))
    headers = _gql_headers(cookie, ct0_m.group(1), referer)
    async with session.get(url, headers=headers,
                           timeout=aiohttp.ClientTimeout(total=30)) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise HTTPException(502, f"GraphQL 请求失败 HTTP {resp.status}: {body[:200]}")
        return await resp.json()

def gql_user_id_from_response(data: dict) -> str:
    """从 UserByScreenName 响应解码 rest_id"""
    try:
        uid_enc = data["data"]["user"]["result"]["id"]
        import base64
        dec = base64.b64decode(uid_enc).decode()
        if ":" in dec:
            return dec.split(":")[-1]
        return uid_enc
    except Exception:
        return ""

def _extract_tweet_items(tweet_containers: list) -> list:
    """从 tweet 容器列表 (item 或 moduleItems) 提取媒体条目"""
    items = []
    seen = set()
    for it in tweet_containers:
        ic = it.get("item", {}).get("itemContent", {})
        tr = ic.get("tweet_results", {}).get("result", {})
        legacy = tr.get("legacy", {})
        media = legacy.get("extended_entities", {}).get("media", [])
        if not media:
            continue
        tweet_id = tr.get("rest_id", "")
        if tweet_id in seen:
            continue
        seen.add(tweet_id)
        photos, videos = [], []
        for mm in media:
            mtype = mm.get("type", "")
            if mtype == "photo":
                url = mm.get("media_url_https", "")
                if url:
                    url = url.replace("_normal", "")
                    # 原图: 加 name=large (pbs 图床支持)
                    photos.append({"url": url, "w": mm.get("original_info", {}).get("width"),
                                   "h": mm.get("original_info", {}).get("height")})
            elif mtype in ("video", "animated_gif"):
                variants = []
                for v in mm.get("video_info", {}).get("variants", []):
                    if v.get("content_type") == "video/mp4":
                        variants.append({"bitrate": v.get("bitrate", 0),
                                         "url": v.get("url", "")})
                variants.sort(key=lambda x: x["bitrate"], reverse=True)
                if variants:
                    videos.append({
                        "url": variants[0]["url"],
                        "thumb": mm.get("media_url_https", ""),  # 视频封面图
                    })
        if photos or videos:
            items.append({
                "tweet_id": tweet_id,
                "photos": photos,
                "videos": videos,
                "thumb": photos[0]["url"] if photos else "",
            })
    return items

def parse_user_media_items(data: dict) -> list:
    """从 UserMedia GraphQL 响应提取媒体条目 (兼容分页两种指令)"""
    items = []
    seen = set()
    try:
        tl = data["data"]["user"]["result"]["timeline"]["timeline"]
        for inst in tl.get("instructions", []):
            itype = inst.get("type")
            containers = []
            if itype == "TimelineAddEntries":
                for entry in inst.get("entries", []):
                    if "profile-grid" not in entry.get("entryId", ""):
                        continue
                    containers.extend(entry.get("content", {}).get("items", []))
            elif itype == "TimelineAddToModule":
                containers.extend(inst.get("moduleItems", []))
            for it in _extract_tweet_items(containers):
                if it["tweet_id"] not in seen:
                    seen.add(it["tweet_id"])
                    items.append(it)
    except Exception:
        pass
    return items

async def gql_user_media_all(session: aiohttp.ClientSession, screen_name: str,
                             max_pages: int = 50) -> tuple:
    """抓取用户媒体时间线全部条目 (自动翻页直到游标耗尽)
    返回 (items, display_name)"""
    # 1. UserByScreenName -> rest_id + 显示名
    user_data = await gql_request(session, "UserByScreenName",
        {"screen_name": screen_name, "withSafetyModeUserFields": True},
        referer=f"https://x.com/{screen_name}/media")
    uid = gql_user_id_from_response(user_data)
    if not uid:
        raise HTTPException(404, "未找到该用户 (可能不存在或被限制)")
    # 显示名 (页面标题中的名字部分)
    display_name = screen_name
    m = re.search(r'"name"\s*:\s*"((?:[^"\\]|\\.)*)"', json.dumps(user_data, ensure_ascii=False))
    if m:
        raw = m.group(1)
        try:
            display_name = json.loads('"' + raw + '"')
        except Exception:
            display_name = raw

    all_items = []
    seen_tweets = set()
    cursor = ""
    for page in range(max_pages):
        variables = {
            "userId": uid, "count": 100, "includePromotedContent": True,
            "withClientEventToken": False, "withBirdwatchNotes": True,
            "withVoice": True, "withV2Timeline": True,
        }
        if cursor:
            variables["cursor"] = cursor
        try:
            media_data = await gql_request(session, "UserMedia", variables,
                                           referer=f"https://x.com/{screen_name}/media")
        except Exception:
            # 单页失败: 等待后重试一次, 仍失败则跳过该页
            await asyncio.sleep(1)
            try:
                media_data = await gql_request(session, "UserMedia", variables,
                                               referer=f"https://x.com/{screen_name}/media")
            except Exception:
                break
        page_items = parse_user_media_items(media_data)
        for it in page_items:
            if it["tweet_id"] not in seen_tweets:
                seen_tweets.add(it["tweet_id"])
                all_items.append(it)

        # 停止条件 (X 时间线末尾的 Bottom cursor 值每次都在变, 不能只靠 cursor 相同判断):
        # 本页无媒体条目 = 已到底 (X 每页返回数量不固定 99/100/82, 不满页不代表到底)
        if not page_items:
            break

        # 提取下一页 cursor (兼容字段顺序)
        s = json.dumps(media_data)
        m = re.search(r'"cursorType"\s*:\s*"Bottom"[^}]*?"value"\s*:\s*"([^"]+)"', s)
        if not m:
            m = re.search(r'"value"\s*:\s*"([^"]+)"[^}]*?"cursorType"\s*:\s*"Bottom"', s)
        new_cursor = m.group(1) if m else ""
        if not new_cursor or new_cursor == cursor:
            break  # 没有更多了
        cursor = new_cursor
        await asyncio.sleep(0.1)  # 礼貌限速
    return all_items, display_name

async def probe_video_sizes(session: aiohttp.ClientSession, variants: list) -> list:
    """并发 HEAD 请求获取各变体大小 (字节)"""
    async def probe(v):
        try:
            async with session.head(v["url"], headers={"User-Agent": UA},
                                     timeout=aiohttp.ClientTimeout(total=15),
                                     allow_redirects=True) as resp:
                cl = resp.headers.get("Content-Length")
                v["size"] = int(cl) if cl and cl.isdigit() else 0
        except Exception:
            v["size"] = 0
        return v
    return await asyncio.gather(*[probe(v) for v in variants])

def extract_duration(gql_json: str) -> int:
    """从 GraphQL 响应提取视频时长 (秒)"""
    m = re.search(r'"duration_millis"\s*:\s*(\d+)', gql_json)
    if m:
        return int(m.group(1)) // 1000
    return 0

async def fetch_html(session: aiohttp.ClientSession, url: str, use_cookie: bool = True) -> str:
    headers = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml"}
    if use_cookie:
        cfg = load_config()
        if cfg.get("cookie"):
            headers["Cookie"] = extract_cookie_header(cfg["cookie"])
            ct0 = re.search(r"(?:^|;\s*)ct0=([^;]+)", cfg["cookie"])
            if ct0:
                headers["X-CSRF-Token"] = ct0.group(1)
    async with session.get(url, headers=headers, allow_redirects=True,
                           timeout=aiohttp.ClientTimeout(total=30)) as resp:
        return await resp.text()

# ---------------- 解析视频 ----------------
def parse_video_page(html: str, url: str) -> dict:
    variants = extract_video_variants(html)
    return {
        "title": extract_page_title(html),
        "thumb": extract_thumb(html),
        "variants": variants,
        "best": variants[0]["url"] if variants else "",
    }

# ---------------- 解析媒体时间线 ----------------
def parse_media_timeline(html: str, base_url: str) -> dict:
    """从 /media 页面 HTML 提取时间线条目 (需要登录态 SSR)"""
    # 找到 aria-label 含 "时间线" 且含 "媒体" 的 div (aria-label 值会变)
    items = []
    seen = set()

    # 提取所有 photo 链接 a[href="/user/status/xxx/photo/N"]
    photo_links = re.findall(r'<a[^>]*href="(/[^"]*/status/\d+/photo/\d+)"[^>]*>(.*?)</a>', html, re.S)
    # 提取所有 video 链接
    video_links = re.findall(r'<a[^>]*href="(/[^"]*/status/\d+/video/\d+)"[^>]*>', html)

    for href, inner in photo_links:
        base_photo = re.sub(r"/photo/\d+$", "/photo", href)
        if base_photo in seen:
            continue
        seen.add(base_photo)
        # 提取缩略图 (a 内部 img)
        thumb = ""
        im = re.search(r'<img[^>]*src="([^"]+)"', inner)
        if im:
            thumb = im.group(1)
            if thumb.startswith("//"):
                thumb = "https:" + thumb
        label = re.sub(r"<[^>]+>", " ", inner).strip()
        items.append({
            "type": "photo",
            "photo_page": "https://x.com" + base_photo,
            "photo_num": href.split("/photo/")[-1],
            "thumb": thumb,
            "label": label[:200],
        })

    for href in video_links:
        base = re.sub(r"/video/\d+$", "", href)
        if base in seen:
            continue
        seen.add(base)
        items.append({
            "type": "video",
            "status_url": "https://x.com" + base,
            "label": "",
        })

    return {"items": items}

# ---------------- 高清图流程 ----------------
async def fetch_photo_large(session: aiohttp.ClientSession, photo_page_url: str) -> list:
    """
    用户指定的高清图流程:
    1. 获取 photo 页内容
    2. 找所有 a[href="/user/status/xxx/photo/N"], 判断去掉photo后数字 == 当前页链接(去掉https://x.com)
    3. 匹配则取 a 内 img src, name=small -> large
    """
    html = await fetch_html(session, photo_page_url)
    # 当前页面路径: /user/status/xxx/photo
    path = urlparse(photo_page_url).path  # 形如 /misao_28/status/xxx/photo
    # 正则匹配 path 中的用户+status: ^/([^/]+)/status/(\d+)/photo
    m = re.match(r"^/([^/]+)/status/(\d+)/photo$", path)
    if not m:
        return []
    user, sid = m.group(1), m.group(2)
    target = f"/{user}/status/{sid}/photo"  # 去掉数字后的形态

    results = []
    # 找所有 a 标签
    for am in re.finditer(r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', html, re.S):
        href = am.group(1)
        inner = am.group(2)
        # 去掉 photo 后的数字
        base = re.sub(r"/photo/\d+$", "/photo", href)
        if base != target:
            continue
        # 提取 img src
        im = re.search(r'<img[^>]*src="([^"]+)"', inner)
        if not im:
            continue
        src = im.group(1)
        # name=small -> name=large
        if "name=small" in src:
            src = src.replace("name=small", "name=large")
        elif "name=medium" in src:
            src = src.replace("name=medium", "name=large")
        if src.startswith("//"):
            src = "https:" + src
        results.append({"url": src, "num": re.sub(r".*/photo/(\d+).*", r"\1", href, flags=re.S)})
    return results

# ---------------- 下载任务 ----------------
def gen_task_id():
    return f"{int(time.time()*1000)}"

async def download_one(session, url: str, dest: Path, results: dict, i: int, total: int, task: dict, is_status: bool = False):
    headers = {"User-Agent": UA, "Referer": "https://x.com/"}
    try:
        # status 链接: 先解析视频真实地址 (匿名优先, X 对匿名做 SSR)
        if is_status:
            html = await fetch_html(session, normalize_x_url(url), use_cookie=False)
            variants = extract_video_variants(html)
            if not variants:
                cfg = load_config()
                if cfg.get("cookie"):
                    html = await fetch_html(session, normalize_x_url(url), use_cookie=True)
                    variants = extract_video_variants(html)
            if not variants:
                raise Exception("无法解析该帖子的视频")
            url = variants[0]["url"]
            headers = {"User-Agent": UA, "Referer": "https://x.com/"}
        async with session.get(url, headers=headers,
                               timeout=aiohttp.ClientTimeout(
                                   total=None,  # 大视频不设总超时, 靠 read 超时兜底
                                   sock_read=180)) as resp:
            if resp.status != 200:
                raise Exception(f"HTTP {resp.status}")
            ctype = resp.headers.get("Content-Type", "")
            # 文件名
            name = os.path.basename(urlparse(url).path)
            name = unquote(name)
            if not name or "." not in name:
                ext = "jpg"
                if "mp4" in ctype or ".mp4" in url:
                    ext = "mp4"
                name = f"media_{i}.{ext}"
            name = sanitize_filename(name)
            final = dest / name
            if final.exists():
                stem, ext = os.path.splitext(name)
                final = dest / f"{stem}_{i}{ext}"
            # 流式写入 (大视频不再整块读内存)
            tmp = final.with_suffix(final.suffix + ".part")
            size = 0
            with open(tmp, "wb") as f:
                async for chunk in resp.content.iter_chunked(1 << 16):
                    f.write(chunk)
                    size += len(chunk)
        if size < 100:
            tmp.unlink(missing_ok=True)
            raise Exception("内容过小")
        tmp.rename(final)
        results["ok"].append(str(final.name))
        results["bytes"] += size
    except Exception as e:
        results["fail"].append({"url": url, "error": str(e)[:200]})
    finally:
        task["done"] += 1
        task["percent"] = round(task["done"] / total * 100, 1) if total else 100

async def run_download(task: dict, items: list, folder: Path, need_zip: bool):
    t0 = time.time()
    folder.mkdir(parents=True, exist_ok=True)
    results = {"ok": [], "fail": [], "bytes": 0}
    total = len(items)
    task["phase"] = "downloading"
    async with aiohttp.ClientSession(max_line_size=65536, max_field_size=65536) as session:
        sem = asyncio.Semaphore(CONCURRENCY)
        async def guarded(it, i):
            async with sem:
                await download_one(session, it["url"], folder, results, i, total, task,
                                   is_status=bool(it.get("is_status")))
        await asyncio.gather(*[guarded(it, i) for i, it in enumerate(items, 1)])

    task["phase"] = "packing"
    zip_path = None
    if need_zip and results["ok"]:
        zip_path = folder.parent / (sanitize_filename(task["title"]) + ".zip")
        loop = asyncio.get_event_loop()
        def _zip():
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
                for f in sorted(folder.iterdir()):
                    if f.is_file():
                        zf.write(f, arcname=f.name)
        await loop.run_in_executor(executor, _zip)

    task.update({
        "phase": "done", "status": "done",
        "ok_count": len(results["ok"]), "fail_count": len(results["fail"]),
        "total_bytes": results["bytes"],
        "elapsed": round(time.time() - t0, 1),
        "folder": str(folder),
        "zip_path": str(zip_path) if zip_path else None,
        "files": results["ok"], "errors": results["fail"],
        "finished_at": time.time(),
    })
    # 记录历史
    if results["ok"]:
        history.insert(0, {
            "id": task["id"], "title": task["title"],
            "ok_count": len(results["ok"]), "fail_count": len(results["fail"]),
            "total_bytes": results["bytes"], "elapsed": round(time.time() - t0, 1),
            "folder": str(folder),
            "zip_path": str(zip_path) if zip_path else None,
            "finished_at": task["finished_at"],
        })
        history[:] = history[:50]
        save_history()

# ---------------- 应用 ----------------
_bg_started = False

async def startup_background():
    global _bg_started
    if _bg_started:
        return
    _bg_started = True
    # xdl 无独立后台任务

@asynccontextmanager
async def lifespan(app: FastAPI):
    await startup_background()
    yield

app = FastAPI(title="xdl", version="1.0.0", lifespan=lifespan)
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
        masked = cookie[:20] + "..." + str(len(cookie)) + "字符"
    return {"has_cookie": bool(cookie), "masked": masked, "analysis": analysis}

@app.post("/api/config")
async def api_set_config(body: ConfigBody):
    save_config({"cookie": body.cookie.strip()})
    return {"ok": True}

# ---------- 视频解析 ----------
class ParseVideoBody(BaseModel):
    url: str

@app.post("/api/parse_video")
async def api_parse_video(body: ParseVideoBody):
    url = body.url.strip()
    if not is_x_url(url):
        raise HTTPException(400, "请输入 x.com / twitter.com 链接")
    status_id = parse_status_id(url)
    if not status_id:
        raise HTTPException(400, "无法识别 status 链接")
    norm = normalize_x_url(url)
    cfg = load_config()
    has_cookie = bool(cfg.get("cookie"))
    used = "anonymous"
    data = None
    async with aiohttp.ClientSession(max_line_size=65536, max_field_size=65536) as session:
        # 1. 先匿名抓 HTML (X 对匿名做 SSR)
        try:
            html = await fetch_html(session, norm, use_cookie=False)
            data = parse_video_page(html, norm)
            if data["variants"]:
                used = "anonymous"
        except Exception:
            pass
        # 2. HTML 无变体 -> GraphQL TweetDetail (敏感/受限内容必需)
        if (not data or not data["variants"]) and has_cookie:
            try:
                gql = await gql_request(session, "TweetDetail", {
                    "focalTweetId": status_id,
                    "with_rux_injections": False,
                    "includePromotedContent": True,
                    "withCommunity": True,
                    "withQuickPromoteEligibilityTweetFields": True,
                    "withBirdwatchNotes": True,
                    "withVoice": True,
                    "withV2Timeline": True,
                    "withConversationQueryFields": True,
                    "withDownvotePerspective": False,
                    "withReactionsMetadata": False,
                    "withReactionsPerspective": False,
                    "withSuperFollowsTweetFields": True,
                    "withSuperFollowsUserFields": True,
                    "withNftAvatar": True,
                    "withSubscriptions": True,
                    "withGrokAnalyze": False,
                    "withArticleRichContentState": True,
                    "withComposeState": True,
                    "withReplyQueryResults": True,
                    "withUserResults": True,
                    "withFollowingFollowerFields": True,
                    "withProfileCardFields": False,
                    "withProfileBusinessFields": False,
                }, referer=norm)
                # 从响应中提取视频变体 (兼容有无空格)
                s = json.dumps(gql, ensure_ascii=False)
                variants = []
                for m in re.finditer(r'"bitrate"\s*:\s*(\d+)\s*,\s*"content_type"\s*:\s*"video/mp4"\s*,\s*"url"\s*:\s*"([^"]+)"', s):
                    br, u = int(m.group(1)), m.group(2).replace("\\u0026", "&")
                    variants.append({"bitrate": br, "url": u})
                # 去重保序
                seen, uniq = set(), []
                for v in variants:
                    if v["url"] not in seen:
                        seen.add(v["url"])
                        uniq.append(v)
                if uniq:
                    uniq.sort(key=lambda v: v["bitrate"], reverse=True)
                    # 标题: 直接从解析后的 dict 提取 full_text (避免 JSON 转义问题)
                    title = "X 视频"
                    for m in re.finditer(r'"full_text"\s*:\s*"((?:[^"\\]|\\.)*)"', s):
                        raw = m.group(1)
                        try:
                            title = json.loads('"' + raw + '"')
                            break
                        except Exception:
                            title = raw
                            break
                    # 缩略图 (兼容有无空格)
                    th = re.search(r'"media_url_https"\s*:\s*"([^"]+)"', s)
                    thumb = th.group(1) if th else ""
                    if thumb:
                        thumb = thumb.replace("\\u0026", "&")
                    data = {
                        "title": title, "thumb": thumb,
                        "variants": uniq, "best": uniq[0]["url"],
                        "duration": extract_duration(s),
                    }
                    used = "graphql"
            except HTTPException:
                raise
            except Exception as e:
                data = None
        # 3. 全部失败
        if not data or not data.get("variants"):
            raise HTTPException(404,
                "该页面未找到视频。可能原因: ① 帖子被删除/设为私密; "
                "② 账号或内容被标记为敏感 (需要完整登录 cookie 且通过 GraphQL API); "
                "③ 这不是视频帖子。最后尝试模式: " + used +
                (" (已配置 cookie)" if has_cookie else " (未配置 cookie)"))
    # 标签: 从 variants 提取分辨率 + 并发探测大小
    for v in data["variants"]:
        m = re.search(r"/vid/avc1/(\d+)x(\d+)/", v["url"])
        v["label"] = f"{m.group(1)}x{m.group(2)}" if m else "unknown"
        v["size"] = 0
    async with aiohttp.ClientSession(max_line_size=65536, max_field_size=65536) as session:
        try:
            data["variants"] = await probe_video_sizes(session, data["variants"])
        except Exception:
            pass
    return {
        "status_id": status_id,
        "title": data["title"],
        "thumb": data["thumb"],
        "variants": data["variants"],
        "best": data["best"],
        "auth_mode": used,
        "duration": data.get("duration", 0),
    }

# ---------- 媒体时间线 ----------
class ParseMediaBody(BaseModel):
    url: str

def normalize_media_url(url: str) -> str:
    """把媒体时间线链接归一化为 https://x.com/<user>/media
    兼容 x.com/user/media 与 x.com/user 两种输入, 自动补全 /media"""
    url = url.strip()
    m = re.match(r"https?://(?:www\.)?(?:x\.com|twitter\.com)/([A-Za-z0-9_]+)"
                 r"(?:/media)?/?$", url)
    if not m:
        return ""
    return f"https://x.com/{m.group(1)}/media"

async def run_parse_media(task: dict, url: str):
    """后台执行媒体时间线抓取, 更新 task 状态供前端轮询
    (任务制: 手机切后台/锁屏不中断后端抓取)"""
    try:
        cfg = load_config()
        if not cfg.get("cookie"):
            task["status"] = "error"
            task["error"] = ("需要登录认证: 请在浏览器登录 X 后复制完整 Cookie "
                             "(F12 -> Network -> 任意 x.com 请求 -> Request Headers), "
                             "填入「Cookie 设置」面板")
            return
        m = re.search(r"x\.com/([^/]+)/media", url)
        screen_name = m.group(1) if m else ""
        if not screen_name:
            task["status"] = "error"
            task["error"] = "无法识别用户名"
            return
        task["phase"] = "fetching"
        async with aiohttp.ClientSession(max_line_size=65536, max_field_size=65536) as session:
            try:
                items, display_name = await gql_user_media_all(session, screen_name)
            except HTTPException as e:
                task["status"] = "error"
                task["error"] = e.detail
                return
            except Exception as e:
                task["status"] = "error"
                task["error"] = f"GraphQL 抓取失败: {e}"
                return

        if not items:
            task["status"] = "error"
            task["error"] = ("未找到媒体时间线条目。可能原因: ① Cookie 无效或已过期; "
                             "② 账户为敏感内容类型且未确认; ③ 用户没有媒体内容")
            return
        # 扁平化为前端可用的条目 (图片带 large 原图, 视频带封面)
        flat = []
        for it in items:
            for p in it["photos"]:
                flat.append({
                    "type": "photo",
                    "url": p["url"] + "?name=large",
                    "thumb": p["url"] + "?name=small",
                    "tweet_id": it["tweet_id"],
                })
            for v in it["videos"]:
                flat.append({
                    "type": "video",
                    "url": v["url"],
                    "thumb": v.get("thumb", ""),
                    "tweet_id": it["tweet_id"],
                })
        task.update({
            "phase": "done", "status": "done",
            "screen_name": screen_name, "page_title": display_name,
            "count": len(flat), "items": flat,
            "finished_at": time.time(),
        })
    except HTTPException as e:
        task["status"] = "error"
        task["error"] = e.detail
    except Exception as e:
        task["status"] = "error"
        task["error"] = f"抓取失败: {e}"

@app.post("/api/parse_media")
async def api_parse_media(body: ParseMediaBody):
    norm = normalize_media_url(body.url)
    if not norm:
        raise HTTPException(400,
            "无法识别的媒体页链接。请粘贴 x.com/用户名 或 x.com/用户名/media 形式")
    # 清理超过 10 分钟的已完成任务, 防止 parse_tasks 无限膨胀
    now = time.time()
    for k in [k for k, t in parse_tasks.items()
              if t.get("finished_at") and now - t["finished_at"] > 600]:
        del parse_tasks[k]
    task_id = gen_task_id()
    task = {
        "id": task_id, "url": norm, "phase": "queued", "status": "running",
        "screen_name": "", "page_title": "", "count": 0, "items": [],
        "error": "", "created_at": now, "finished_at": None,
    }
    parse_tasks[task_id] = task
    asyncio.create_task(run_parse_media(task, norm))
    return {"task_id": task_id}

@app.get("/api/parse_tasks/{task_id}")
async def api_parse_task(task_id: str):
    t = parse_tasks.get(task_id)
    if not t:
        raise HTTPException(404, "抓取任务不存在")
    return t

# ---------- 高清图批量获取 ----------
class PhotoResolveBody(BaseModel):
    photo_pages: list

@app.post("/api/resolve_photos")
async def api_resolve_photos(body: PhotoResolveBody):
    """对 photo 页面批量执行高清图流程, 返回最终 large 图 URL 列表"""
    if not body.photo_pages:
        raise HTTPException(400, "列表为空")
    cfg = load_config()
    if not cfg.get("cookie"):
        raise HTTPException(401, "需要登录认证, 请先配置 cookie")
    results = []
    async with aiohttp.ClientSession(max_line_size=65536, max_field_size=65536) as session:
        for pp in body.photo_pages[:50]:
            try:
                imgs = await fetch_photo_large(session, pp)
                results.extend(imgs)
            except Exception as e:
                results.append({"url": "", "error": str(e)[:100]})
    ok = [r for r in results if r.get("url")]
    return {"count": len(ok), "images": ok}

# ---------- 图片代理 (预览) ----------
@app.get("/api/img")
async def api_img(url: str):
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "bad url")
    headers = {"User-Agent": UA, "Referer": "https://x.com/"}
    try:
        async with aiohttp.ClientSession(max_line_size=65536, max_field_size=65536) as session:
            async with session.get(url, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    raise HTTPException(502, f"图片获取失败 HTTP {resp.status}")
                ctype = resp.headers.get("Content-Type", "image/jpeg")
                data = await resp.read()
        return Response(data, media_type=ctype, headers={
            "Cache-Control": "public, max-age=3600",
        })
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"图片获取失败: {e}")

# ---------- 流式代理下载 (浏览器模式, 不落服务器磁盘) ----------
@app.get("/api/stream")
async def api_stream(url: str, name: str = ""):
    """浏览器下载模式的直连通道: 流式转发 X 媒体, 带防盗链 Referer/Cookie,
    边收边发不写磁盘, 不占 downloads/ 目录"""
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "bad url")
    headers = {"User-Agent": UA, "Referer": "https://x.com/"}
    cfg = load_config()
    if cfg.get("cookie"):
        headers["Cookie"] = extract_cookie_header(cfg["cookie"])
        ct0 = re.search(r"(?:^|;\s*)ct0=([^;]+)", cfg["cookie"])
        if ct0:
            headers["X-CSRF-Token"] = ct0.group(1)
    if not name:
        # 从 URL 推断文件名
        p = urlparse(url)
        name = os.path.basename(p.path)
        name = unquote(name)
        if not name or "." not in name:
            name = "download"
    name = sanitize_filename(name)

    async def gen():
        try:
            async with aiohttp.ClientSession(max_line_size=65536, max_field_size=65536) as session:
                async with session.get(url, headers=headers,
                                       timeout=aiohttp.ClientTimeout(
                                           total=None, sock_read=180),
                                       allow_redirects=True) as resp:
                    if resp.status != 200:
                        yield f"下载失败: HTTP {resp.status}".encode()
                        return
                    async for chunk in resp.content.iter_chunked(1 << 16):
                        yield chunk
        except Exception as e:
            yield f"下载失败: {str(e)[:200]}".encode()

    return StreamingResponse(
        gen(),
        media_type="application/octet-stream",
        headers={"Content-Disposition":
                 f"attachment; filename*=UTF-8''{quote(name)}"},
    )

# ---------- 下载 ----------
class DownloadBody(BaseModel):
    title: str = ""
    items: list = []
    need_zip: bool = True

@app.post("/api/download")
async def api_download(body: DownloadBody):
    if not body.items:
        raise HTTPException(400, "下载列表为空")
    title = sanitize_filename(body.title) if body.title else f"x_{int(time.time())}"
    task_id = gen_task_id()
    task = {
        "id": task_id, "title": body.title or title,
        "total": len(body.items), "done": 0, "percent": 0,
        "phase": "queued", "status": "running",
        "created_at": time.time(),
        "ok_count": 0, "fail_count": 0, "total_bytes": 0,
        "elapsed": 0, "folder": "", "zip_path": None,
        "files": [], "errors": [],
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
    t = tasks.get(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    return t

@app.get("/api/history")
async def api_history():
    return {"history": history}

@app.post("/api/history/clear")
async def api_history_clear():
    history.clear()
    save_history()
    return {"ok": True}

@app.get("/api/tasks/{task_id}/zip")
async def api_task_zip(task_id: str):
    t = tasks.get(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    if not t.get("zip_path") or not os.path.isfile(t["zip_path"]):
        raise HTTPException(404, "ZIP 尚未生成")
    return FileResponse(t["zip_path"], media_type="application/zip",
                        filename=os.path.basename(t["zip_path"]))

@app.get("/api/tasks/{task_id}/browse")
async def api_task_browse(task_id: str):
    t = tasks.get(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    folder = Path(t["folder"])
    if not folder.is_dir():
        return {"files": []}
    files = [{"name": f.name, "size": f.stat().st_size}
             for f in sorted(folder.iterdir()) if f.is_file()]
    return {"folder": str(folder), "files": files, "title": t["title"]}

@app.get("/api/tasks/{task_id}/files/{fname:path}")
async def api_task_file(task_id: str, fname: str):
    t = tasks.get(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    fp = Path(t["folder"]) / unquote(fname)
    if not fp.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(fp, filename=fp.name)

# ---------- 静态 ----------
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(str(FRONTEND_DIR / "index.html"))

if __name__ == "__main__":
    import uvicorn
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8810
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
