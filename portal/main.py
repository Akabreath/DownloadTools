#!/usr/bin/env python3
"""
DownloadTools 门户 - 下载工具集合
挂载各下载工具为子应用, 统一入口
"""
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
PORTAL_DIR = Path(__file__).resolve().parent
TOOLS_DIR = ROOT / "tools"

# 让 tools/ 下的包可导入
sys.path.insert(0, str(TOOLS_DIR))

# ---------------- 工具注册表 ----------------
# 以后扩展新工具: 1) 在 tools/ 下建目录 2) 在此注册
TOOLS = [
    {
        "id": "cosplayd",
        "name": "cosplayD",
        "icon": "📸",
        "desc": "Cosplay 页面图片下载 - 输入链接自动提取正文 p 标签下图片, 支持预览/下载/ZIP",
        "tags": ["图片", "抓取"],
        "path": "/cosplayd/",
    },
    {
        "id": "xdl",
        "name": "xdl",
        "icon": "🐦",
        "desc": "X (Twitter) 视频与媒体下载 - 解析视频清晰度, 抓取用户媒体时间线图片/视频",
        "tags": ["视频", "图片", "X"],
        "path": "/xdl/",
    },
    {
        "id": "bili",
        "name": "bili",
        "icon": "📺",
        "desc": "bilibili 视频与 UP主投稿下载 - 单视频 720P 直连 / 1080P+ 合并, 空间主页全部投稿打包 ZIP",
        "tags": ["视频", "bilibili"],
        "path": "/bili/",
    },
    {
        "id": "douyin",
        "name": "douyin",
        "icon": "🎵",
        "desc": "抖音视频/图集/主页作品下载 - 单作品多档清晰度无水印直连, 主页作品分页抓取打包 ZIP",
        "tags": ["视频", "图片", "抖音"],
        "path": "/douyin/",
    },
]

# ---------------- 应用 ----------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动各工具的独立后台任务 (子应用 lifespan 不会被挂载时自动调用)
    for tool in TOOLS:
        try:
            mod = __import__(f"{tool['id']}.backend.main", fromlist=["startup_background"])
            await mod.startup_background()
        except Exception as e:
            print(f"[portal] 启动 {tool['id']} 后台任务失败: {e}")
    yield

app = FastAPI(title="DownloadTools", version="1.0.0", lifespan=lifespan)

# 挂载各工具子应用
for tool in TOOLS:
    mod = __import__(f"{tool['id']}.backend.main", fromlist=["app"])
    app.mount(tool["path"], mod.app, name=tool["id"])
    print(f"[portal] 已挂载 {tool['name']} -> {tool['path']}")

@app.get("/api/tools")
async def api_tools():
    return {"tools": TOOLS}

@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(str(PORTAL_DIR / "frontend" / "index.html"))

# 门户静态资源
app.mount("/static", StaticFiles(directory=str(PORTAL_DIR / "frontend")), name="portal_static")

if __name__ == "__main__":
    import uvicorn
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8800
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
