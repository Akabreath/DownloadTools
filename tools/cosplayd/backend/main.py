#!/usr/bin/env python3
"""
cosplayD - Cosplay 图片下载工具
后端: FastAPI + aiohttp + BeautifulSoup
功能: 输入 cosblay 页面链接 -> 提取 p 标签下所有图片 -> 预览 -> 下载(直接/ZIP)
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
from urllib.parse import quote, unquote, urlparse

import aiohttp
import uvicorn
from bs4 import BeautifulSoup
from bs4 import Tag
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ---------------- 路径配置 ----------------
ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = ROOT / "frontend"
DOWNLOAD_DIR = ROOT / "downloads"
HISTORY_FILE = ROOT / "history.json"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
REFERER = "https://cosblay.com/"
CONCURRENCY = 8  # 并发下载数

# ---------------- 全局状态 ----------------
tasks = {}          # task_id -> task dict
tasks_lock = asyncio.Lock()
history = []        # 已完成任务记录

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
def sanitize_filename(name: str, max_len: int = 80) -> str:
    """清洗文件名/文件夹名, 去除非法字符"""
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name).strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    if not name:
        name = "untitled"
    return name[:max_len]

def safe_name_for_url(url: str) -> str:
    """从 URL 提取安全的文件名"""
    name = os.path.basename(urlparse(url).path)
    name = unquote(name)
    name = sanitize_filename(name)
    if not name or "." not in name:
        name = f"img_{abs(hash(url)) % 100000}.jpg"
    return name

def guess_ext(url: str, content_type: str = "") -> str:
    """根据 URL 和 Content-Type 推断扩展名"""
    path = urlparse(url).path.lower()
    m = re.search(r"\.(jpe?g|png|gif|webp|avif|bmp|svg)$", path)
    if m:
        return m.group(1).replace("jpeg", "jpg")
    ct = content_type.lower()
    mapping = {
        "image/jpeg": "jpg", "image/png": "png", "image/gif": "gif",
        "image/webp": "webp", "image/avif": "avif", "image/bmp": "bmp",
        "image/svg+xml": "svg",
    }
    for k, v in mapping.items():
        if k in ct:
            return v
    return "jpg"

async def fetch_page(url: str) -> str:
    """抓取页面 HTML (带重试, 抗偶发 5xx/断连)"""
    timeout = aiohttp.ClientTimeout(total=30)
    headers = {"User-Agent": UA, "Referer": REFERER}
    last_err = None
    for attempt in range(3):
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        return await resp.text()
                    if resp.status in (500, 502, 503, 429):
                        last_err = aiohttp.ClientResponseError(
                            resp.request_info, resp.history, status=resp.status,
                            message=f"HTTP {resp.status}")
                    else:
                        resp.raise_for_status()
        except aiohttp.ClientResponseError as e:
            last_err = e
        except (aiohttp.ServerDisconnectedError, aiohttp.ClientConnectorError,
                asyncio.TimeoutError) as e:
            last_err = e
        await asyncio.sleep(1.5 * (attempt + 1))
    raise last_err if last_err else Exception("抓取失败")

def parse_page(html: str, page_url: str) -> dict:
    """解析页面: 标题 + p 标签下所有图片 URL"""
    soup = BeautifulSoup(html, "html.parser")

    # 标题: og:title > <title> > h1
    title = ""
    og = soup.find("meta", property="og:title")
    if isinstance(og, Tag) and og.get("content"):
        title = og["content"].strip()
    if not title and isinstance(soup.title, Tag) and soup.title:
        title = soup.title.get_text(strip=True)
    if not title:
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(strip=True)
    # 去掉站点名尾巴 (e.g. "xxx - CosBlay") 和分页尾巴 (e.g. "xxx – Page 2")
    title = re.sub(r"\s*[-–—|]\s*(CosBlay|cosblay)\s*$", "", title).strip()
    title = re.sub(r"\s*[-–—|]\s*Page\s*\d+\s*$", "", title).strip()

    # 正文容器
    entry = soup.find("div", class_="entry-content") or \
            soup.find("article") or \
            soup.find("div", id="content") or soup

    urls = []
    for p in entry.find_all("p"):
        for img in p.find_all("img"):
            if not isinstance(img, Tag):
                continue
            src = img.get("src") or img.get("data-src") or img.get("data-original")
            if not src or not isinstance(src, str):
                continue
            # 跳过 base64 和 1px 占位图
            if src.startswith("data:") or "1x1" in src or "blank" in src.lower():
                continue
            # srcset: 仅当 src 缺失/无效时, 取最大宽度候选
            srcset = img.get("srcset")
            if srcset and (not src or "1x1" in src or "blank" in src.lower()):
                candidates = []  # (width, url)
                for part in srcset.split(","):
                    part = part.strip()
                    if not part:
                        continue
                    tok = part.split()
                    if len(tok) >= 2 and tok[1].endswith("w"):
                        try:
                            candidates.append((int(tok[1][:-1]), tok[0]))
                        except ValueError:
                            pass
                if candidates:
                    candidates.sort(reverse=True)
                    src = candidates[0][1]
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = urljoin_base(page_url, src)
            urls.append(src)

    # 去重保序
    seen, uniq = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return {"title": title, "urls": uniq}

def urljoin_base(base: str, path: str) -> str:
    p = urlparse(base)
    return f"{p.scheme}://{p.netloc}{path}"

# ---------------- 分页支持 ----------------
def clean_page_url(url: str) -> str:
    """去掉 URL 的 fragment/query, 便于比较与排序"""
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}{p.path}"

def page_no_of(url: str) -> int:
    """从分页 URL 提取页码, 无数字后缀视为第 1 页"""
    m = re.search(r"/(\d+)/?$", url)
    return int(m.group(1)) if m else 1

def collect_page_urls(soup, base_url: str) -> list:
    """从页面中提取文章分页链接 (数字页码), 返回按页码升序的 URL 列表 (含首页)

    兼容 WP 文章内分页的几种标记: post-page-numbers / page-numbers / .page-links a
    """
    base_url = clean_page_url(base_url)
    urls = {base_url}
    for a in soup.select("a.post-page-numbers, a.page-numbers, .page-links a"):
        href = a.get("href")
        text = a.get_text(strip=True)
        if not isinstance(href, str) or not href:
            continue
        # 只认纯数字页码文本, 跳过 "Next »" / "« Previous" 等导航
        if not re.fullmatch(r"\d+", text):
            continue
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = urljoin_base(base_url, href)
        href = clean_page_url(href)
        if href.startswith("http"):
            urls.add(href)
    return sorted(urls, key=page_no_of)

async def parse_all_pages(url: str) -> dict:
    """抓取文章全部页并合并图片, 返回 {title, pages, urls}"""
    html = await fetch_page(url)
    soup = BeautifulSoup(html, "html.parser")
    page_urls = collect_page_urls(soup, url)
    images, seen = [], set()
    title = ""
    for pu in page_urls:
        h = await fetch_page(pu) if clean_page_url(pu) != clean_page_url(url) else html
        data = parse_page(h, pu)
        if not title and data["title"]:
            title = data["title"]
        for u in data["urls"]:
            if u not in seen:
                seen.add(u)
                images.append(u)
    return {"title": title, "pages": len(page_urls), "urls": images}

# ---------------- 应用 ----------------
_bg_started = False

async def startup_background():
    """启动后台任务 (幂等, 独立运行与门户挂载共用)"""
    global _bg_started
    if _bg_started:
        return
    _bg_started = True
    asyncio.create_task(_finalize_history())

@asynccontextmanager
async def lifespan(app: FastAPI):
    await startup_background()
    yield

app = FastAPI(title="cosplayD", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)
executor = ThreadPoolExecutor(max_workers=2)

@app.get("/api/health")
async def health():
    return {"status": "ok", "time": time.time()}

class ParseBody(BaseModel):
    url: str

@app.post("/api/parse")
async def api_parse(body: ParseBody):
    """解析页面(含全部分页), 返回标题 + 图片列表"""
    url = body.url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "请输入合法的 http(s) 链接")
    try:
        data = await parse_all_pages(url)
    except aiohttp.ClientResponseError as e:
        raise HTTPException(502, f"抓取页面失败: HTTP {e.status}")
    except Exception as e:
        raise HTTPException(502, f"抓取页面失败: {e}")
    if not data["urls"]:
        raise HTTPException(404, "未在 p 标签下找到任何图片")
    return {
        "url": url,
        "title": data["title"],
        "count": len(data["urls"]),
        "pages": data["pages"],
        "images": data["urls"],
    }

# ---------- 图片代理 (预览用, 防防盗链) ----------
@app.get("/api/img")
async def api_img(url: str = Query(...)):
    """代理图片, 加 UA/Referer 头, 解决防盗链与跨域"""
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "bad url")
    timeout = aiohttp.ClientTimeout(total=30)
    headers = {"User-Agent": UA, "Referer": REFERER}
    try:
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        raise HTTPException(502, f"图片获取失败 HTTP {resp.status}")
                    ctype = resp.headers.get("Content-Type", "image/jpeg")
                    data = await resp.read()
        except aiohttp.ClientSSLError:
            # 证书异常(时间漂移/新证书未生效)时降级: 跳过 SSL 验证重试一次
            conn = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(timeout=timeout, headers=headers,
                                             connector=conn) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        raise HTTPException(502, f"图片获取失败 HTTP {resp.status}")
                    ctype = resp.headers.get("Content-Type", "image/jpeg")
                    data = await resp.read()
        return Response(data, media_type=ctype, headers={
            "Cache-Control": "public, max-age=3600",
            "Content-Length": str(len(data)),
        })
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"图片获取失败: {e}")

# ---------- 下载任务 ----------
def gen_task_id():
    return f"{int(time.time()*1000)}"

async def download_one(session, url: str, dest: Path, results: dict, i: int, total: int, task: dict):
    """下载单张图片, 更新任务进度"""
    headers = {"User-Agent": UA, "Referer": REFERER}
    try:
        try:
            resp = await session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=90))
        except aiohttp.ClientSSLError:
            # 站点证书异常(时间漂移/新证书未生效)时降级: 跳过 SSL 验证重试一次
            conn = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90),
                                             headers=headers, connector=conn) as s2:
                resp = await s2.get(url)
        async with resp:
            if resp.status != 200:
                raise Exception(f"HTTP {resp.status}")
            ctype = resp.headers.get("Content-Type", "")
            data = await resp.read()
        if len(data) < 100:  # 过滤错误页/空图
            raise Exception("内容过小, 疑似错误页")
        name = safe_name_for_url(url)
        if not name.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".svg")):
            name = f"{os.path.splitext(name)[0]}.{guess_ext(url, ctype)}"
        # 防重名
        final = dest / name
        if final.exists():
            stem, ext = os.path.splitext(name)
            final = dest / f"{stem}_{i}{ext}"
        final.write_bytes(data)
        results["ok"].append(str(final.name))
        results["bytes"] += len(data)
    except Exception as e:
        results["fail"].append({"url": url, "error": str(e)[:200]})
    finally:
        task["done"] += 1
        task["percent"] = round(task["done"] / total * 100, 1) if total else 100

async def run_download(task: dict, urls: list, folder: Path, need_zip: bool):
    """执行下载任务主流程"""
    t0 = time.time()
    folder.mkdir(parents=True, exist_ok=True)
    results = {"ok": [], "fail": [], "bytes": 0, "files": []}
    total = len(urls)
    task["phase"] = "downloading"
    async with aiohttp.ClientSession() as session:
        sem = asyncio.Semaphore(CONCURRENCY)

        async def guarded(url, i):
            async with sem:
                await download_one(session, url, folder, results, i, total, task)

        await asyncio.gather(*[guarded(u, i) for i, u in enumerate(urls, 1)])

    task["phase"] = "packing"
    zip_path = None
    if need_zip and results["ok"]:
        zip_name = sanitize_filename(task["title"]) + ".zip"
        zip_path = folder.parent / zip_name
        loop = asyncio.get_event_loop()
        def _zip():
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
                for f in sorted(folder.iterdir()):
                    if f.is_file():
                        zf.write(f, arcname=f.name)
            return zip_path
        await loop.run_in_executor(executor, _zip)

    elapsed = round(time.time() - t0, 1)
    task.update({
        "phase": "done" if results["fail"] or results["ok"] else "error",
        "status": "done",
        "ok_count": len(results["ok"]),
        "fail_count": len(results["fail"]),
        "total_bytes": results["bytes"],
        "elapsed": elapsed,
        "folder": str(folder),
        "zip_path": str(zip_path) if zip_path else None,
        "files": results["ok"],
        "errors": results["fail"],
        "finished_at": time.time(),
    })

class DownloadBody(BaseModel):
    url: str
    title: str = ""
    images: list = []
    need_zip: bool = True

@app.post("/api/download")
async def api_download(body: DownloadBody):
    """创建下载任务, 立即返回 task_id; 后台下载到 downloads/<标题>/"""
    if not body.images:
        raise HTTPException(400, "图片列表为空")
    title = sanitize_filename(body.title) if body.title else f"page_{int(time.time())}"
    task_id = gen_task_id()
    task = {
        "id": task_id,
        "title": body.title or title,
        "url": body.url,
        "total": len(body.images),
        "done": 0,
        "percent": 0,
        "phase": "queued",
        "status": "running",
        "created_at": time.time(),
        "ok_count": 0, "fail_count": 0, "total_bytes": 0,
        "elapsed": 0, "folder": "", "zip_path": None,
        "files": [], "errors": [],
    }
    # 防重复文件夹: 若已存在同名, 加后缀
    folder = DOWNLOAD_DIR / title
    n = 1
    while folder.exists():
        folder = DOWNLOAD_DIR / f"{title}_{n}"
        n += 1
    task["folder"] = str(folder)
    tasks[task_id] = task
    asyncio.create_task(run_download(task, body.images, folder, body.need_zip))
    return {"task_id": task_id, "folder": str(folder)}

@app.get("/api/tasks")
async def api_tasks():
    """任务列表(含历史)"""
    live = [t for t in tasks.values()]
    return {"tasks": live, "history": history}

@app.post("/api/history/clear")
async def api_history_clear():
    """清除历史记录"""
    history.clear()
    save_history()
    return {"ok": True}

@app.get("/api/tasks/{task_id}")
async def api_task(task_id: str):
    t = tasks.get(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    return t

@app.get("/api/tasks/{task_id}/zip")
async def api_task_zip(task_id: str):
    """下载打包好的 zip"""
    t = tasks.get(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    if not t.get("zip_path") or not os.path.isfile(t["zip_path"]):
        raise HTTPException(404, "ZIP 尚未生成")
    return FileResponse(
        t["zip_path"],
        media_type="application/zip",
        filename=os.path.basename(t["zip_path"]),
    )

@app.get("/api/tasks/{task_id}/files/{fname:path}")
async def api_task_file(task_id: str, fname: str):
    """下载任务文件夹中的单个文件"""
    t = tasks.get(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    fname = unquote(fname)
    fp = Path(t["folder"]) / fname
    if not fp.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(fp, filename=fname)

@app.get("/api/tasks/{task_id}/browse")
async def api_task_browse(task_id: str):
    """浏览任务下载的文件列表"""
    t = tasks.get(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    folder = Path(t["folder"])
    if not folder.is_dir():
        return {"files": []}
    files = [{"name": f.name, "size": f.stat().st_size}
             for f in sorted(folder.iterdir()) if f.is_file()]
    return {"folder": str(folder), "files": files, "title": t["title"]}

# 任务完成时写历史
async def _finalize_history():
    while True:
        await asyncio.sleep(2)
        changed = False
        for t in list(tasks.values()):
            if t["status"] == "done" and not t.get("_history_written"):
                t["_history_written"] = True
                rec = {k: t[k] for k in
                       ("id", "title", "url", "ok_count", "fail_count",
                        "total_bytes", "elapsed", "folder", "zip_path",
                        "finished_at") if k in t}
                history.insert(0, rec)
                history[:] = history[:50]
                changed = True
        if changed:
            save_history()

# ---------- 静态前端 ----------
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(str(FRONTEND_DIR / "index.html"))

if __name__ == "__main__":
    port = 8800
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
