# DownloadTools 🧰

下载工具集合门户 — 一站式下载中心，挂载多个下载工具，统一入口。

## 结构

```
DownloadTools/
├── portal/
│   ├── main.py           # 门户后端：工具注册表 + 子应用挂载
│   └── frontend/         # 门户前端（工具卡片集合页）
├── tools/
│   └── cosplayd/         # 工具1: cosplayD (Cosplay 图片下载)
│       ├── backend/main.py
│       ├── frontend/
│       ├── downloads/    # 下载保存目录
│       └── history.json
└── README.md
```

## 启动

```bash
cd DownloadTools
python3 portal/main.py        # 默认端口 8800
# 或指定端口
python3 portal/main.py 9000
```

访问：
- 门户首页: http://localhost:8800/
- cosplayD:   http://localhost:8800/cosplayd/

## 扩展新工具

1. 在 `tools/` 下新建目录（如 `tools/videodl/`），按 cosplayd 的模式提供：
   - `backend/main.py`：FastAPI 应用（导出 `app`），可选导出 `startup_background()`（后台任务，门户启动时会调用）
   - `frontend/`：工具页面（注意：静态资源与 API 请求用**相对路径**，以适应挂载前缀）
2. 在 `portal/main.py` 的 `TOOLS` 注册表中添加一项：

```python
{
    "id": "videodl",            # 目录名
    "name": "videoDL",          # 显示名
    "icon": "🎬",                # 卡片图标
    "desc": "视频下载工具 - ...",
    "tags": ["视频", "抓取"],
    "path": "/videodl/",        # 挂载路径
},
```

3. 重启门户即可。

## 工具开发约定

- 工具前端引用静态资源与 API 用相对路径（`static/...`、`api/...`），
  前端 JS 通过 `BASE` 变量自动推断挂载前缀（页面首段路径）。
- 工具后端无需关心挂载前缀，FastAPI 子应用挂载会自动处理。
- 独立调试工具：`cd tools/cosplayd && python3 backend/main.py 8810`

## 依赖

- Python 3.10+
- fastapi, uvicorn, aiohttp, beautifulsoup4, pydantic
