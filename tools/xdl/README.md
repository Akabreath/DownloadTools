# xdl 🐦

X (Twitter) 视频与媒体下载工具 — 解析视频全部清晰度、抓取用户媒体时间线的高清图片和视频，
支持复制直链、浏览器/服务器双通道下载、历史记录。

## 一、这是什么

xdl 是 DownloadTools 门户中的第二个下载工具，挂载在 `/xdl/` 路径下。
它解决两类需求：

1. **视频下载**：粘贴任意 X 视频帖子链接（含 `/video/1` 或纯 status 链接），
   解析出所有清晰度（最高可达 1080p/4K），显示大小和时长，选择后下载。
2. **媒体时间线抓取**：粘贴用户媒体页链接（如 `https://x.com/misao_28/media`），
   抓取该用户发布过的所有图片和视频（自动翻页，覆盖多年历史），
   高清原图批量下载。

## 二、功能特性

- 🎬 视频解析：多清晰度选择、显示分辨率/码率/大小/时长、复制直链
- 🖼 媒体时间线：图片+视频自动分页抓全、缩略图预览、一键全选、仅图片筛选
- 🌐 默认浏览器下载：完成后自动触发 ZIP 或逐文件下载
- 🖥 可选服务器保存：文件存到 downloads/ 目录
- 🔒 敏感内容支持：对标记为敏感/受限的帖子，自动走 GraphQL API 获取
- 🕘 历史记录 + 一键清除
- ⚙️ Cookie 管理：从浏览器复制完整 Cookie 保存，带字段完整性检查
- 🏠 返回主页按钮

## 三、工作原理（详细）

### 核心难点：X 平台的数据获取机制

X 的网页是 React SPA，且对数据获取有严格的反爬策略。经过反复实测，
总结出三条关键规律（这是整个工具能工作的基石）：

#### 规律 1：匿名请求反而返回完整数据

X 对**未登录**的页面请求做 SSR（服务端渲染，为了 SEO），HTML 里直接内嵌
推文数据和视频 URL；而对**已登录**的请求返回 SPA 空壳（数据靠浏览器 JS
再调接口加载）。

```
匿名请求 → HTML 含 video_info 变体（可直接正则提取）✅
带 cookie → HTML 是空壳（video 数据 0 条）❌
```

所以视频解析的策略是：**先匿名抓 HTML**，拿不到再考虑其他路径。

#### 规律 2：敏感/受限内容只在 GraphQL API 里

misao_28 这类账户被 X 标记为 `possibly_sensitive`（可能包含敏感内容）。
对这类账户，匿名请求和 syndication API 一律返回空壳或 `TweetTombstone`
（墓碑），HTML 里永远没有数据。必须走 X 内部的 GraphQL API。

#### 规律 3：GraphQL API 需要动态 queryId + featureSwitches

X 的内部 GraphQL 端点形如：
`https://x.com/i/api/graphql/<queryId>/<operationName>?variables=...&features=...`

其中 queryId 和 featureSwitches 是**动态变化**的（X 每次发版可能更换），
需要从主 JS bundle 中提取：

```
1. 抓任意页面 HTML → 找到 main.<hash>.js 的 bundle URL
2. 下载 bundle（约 1MB JS）
3. 正则提取 queryId:"xxx",operationName:"TweetDetail",metadata:{featureSwitches:[...]}
4. 把 queryId + 默认值（true/false）缓存到 gql_cache.json（24 小时有效）
```

### 视频解析流程（parse_video）

```
输入链接 → 提取 status_id → 规范化 URL（去掉 /video/1 尾巴）
    │
    ├─ 1. 匿名抓 HTML（SSR 含视频变体）→ 成功则用正则提取全部清晰度
    │
    └─ 2. HTML 无变体 → GraphQL TweetDetail（需 cookie）
           variables 传 focalTweetId=status_id
           → 从返回 JSON 提取 bitrate/content_type/url 变体
           → 提取 full_text 作标题（json.loads 解码转义字符）
           → 提取 media_url_https 作封面、duration_millis 作时长
    │
    └─ 3. 并发 HEAD 请求每个视频 URL → Content-Length 得到文件大小
```

- **变体排序**：按 bitrate 降序，最高清晰度排第一（默认选中）
- **分辨率标签**：从 URL 路径 `/vid/avc1/<W>x<H>/` 提取
- **标题解码**：GraphQL 返回的 JSON 里日文/emoji 是 `\uXXXX` 转义，
  用 `json.loads('"'+raw+'"')` 正确解码（避免显示乱码）

### 媒体时间线抓取流程（parse_media）

```
输入 x.com/<user>/media → 提取用户名
    │
    ├─ 1. GraphQL UserByScreenName → 拿用户 rest_id
    │        （screen_name=用户名, withSafetyModeUserFields=true）
    │        返回的 id 是 base64（如 VXNlcjoxOTMzMzQwNjIz）
    │        → base64 解码取冒号后的数字
    │
    └─ 2. GraphQL UserMedia → 媒体时间线（count=40/页）
             variables: userId=rest_id, cursor=翻页游标
             │
             ├─ 第 1 页：TimelineAddEntries 指令，profile-grid 条目
             ├─ 第 2+ 页：TimelineAddToModule 指令，moduleItems 追加
             │    （两种指令结构不同，解析函数都要兼容）
             └─ 每页尾部取 cursor-bottom 的 value 作为下一页游标
                 自动翻页直到游标耗尽或重复（上限 50 页 ≈ 2000 条推文，
                 大账户如 HongKong_Doll 可抓到 500+ 条媒体）
    │
    └─ 3. 解析每条推文的 legacy.extended_entities.media
             ├─ photo：media_url_https + "?name=large"（高清原图）
             │          缩略图用 "?name=small"
             └─ video/animated_gif：video_info.variants 里挑最高码率 mp4
```

**为什么图片加 ?name=large**：pbs.twimg.com 图床支持按参数裁剪，
`name=small` 是缩略图、`name=large` 是原图（实测可达 1464x2048）。
时间线 API 直接给出原图 URL，比"打开 photo 页再匹配"更高效可靠
（photo 页流程作为备用方案保留在后端）。

### 下载与输出

- 下载任务与 cosplayD 同构：aiohttp 并发（默认 4）、进度追踪、ZIP 打包
- 视频文件用 URL 末尾文件名，图片用 media ID 文件名
- 完成后记录历史到 history.json（最近 50 条）

## 四、使用步骤

### 视频下载

1. 打开工具 `http://localhost:8800/xdl/`（或从门户点 xdl 卡片）
2. 粘贴视频链接（如 `https://x.com/user/status/1234567890/video/1`
   或 `https://x.com/i/status/1234567890`），点「🔍 解析视频」
3. 查看标题、时长、封面；下拉选择清晰度（显示分辨率/码率/大小）
4. 可选：点「📋 复制链接」复制当前清晰度直链
5. 选择保存方式（默认浏览器）和是否打包 ZIP（默认不打包）
6. 点「⬇ 下载」→ 进度条 → 完成后浏览器自动触发下载

### 媒体时间线抓取

1. 点击「🖼 媒体时间线抓取」切换模式
2. **先配置 Cookie**（媒体抓取需要登录态）：
   - 浏览器登录 X → F12 → Network → 刷新页面
   - 点任意 x.com 请求 → Request Headers → 复制整个 Cookie 值
   - 粘贴到「⚙️ Cookie 设置」保存（需含 auth_token/ct0/twid 等完整字段）
3. 粘贴媒体页链接（如 `https://x.com/misao_28/media`），点「🔍 抓取媒体」
4. 预览缩略图网格，可「☑ 全选」、勾选「仅图片」过滤
5. 选保存方式（默认浏览器）和 ZIP 打包，点「⬇ 下载选中」
6. 完成后浏览器自动触发下载；历史区保留记录

## 五、Cookie 说明（重要）

媒体时间线抓取依赖登录态 Cookie。X 要求：
- **完整 Cookie**：至少包含 auth_token、ct0、twid、kdt 等关键字段
  （只有 auth_token+ct0 会被 X 当匿名处理，抓不到数据）
- **时效性**：Cookie 会过期，失效时重新从浏览器复制
- **安全**：Cookie 只保存在本机 `config.json`，不会外传

工具会在 Cookie 设置面板自动检查字段完整性（缺失哪些字段一目了然）。

## 六、目录结构

```
tools/xdl/
├── backend/
│   └── main.py          # FastAPI 后端（解析/GraphQL/下载/历史）
├── frontend/
│   ├── index.html       # 前端页面
│   ├── css/style.css    # 样式
│   └── js/app.js        # 前端逻辑
├── downloads/           # 下载保存目录
├── config.json          # Cookie 配置（自动生成）
├── gql_cache.json       # GraphQL queryId 缓存（自动生成）
└── history.json         # 历史记录（自动生成）
```

## 七、技术要点

- **GraphQL 客户端**：自定义实现，从 bundle 动态提取 queryId +
  featureSwitches（默认 true/false），带完整浏览器请求头
  （UA/Cookie/X-CSRF-Token/Bearer token/Origin/Referer）
- **公开 Bearer token**：X web 客户端的固定 token，内置于代码
- **兼容分页结构**：TimelineAddEntries（首页）与 TimelineAddToModule
  （追加页）两种指令都处理
- **容错**：单页抓取失败自动重试一次，仍失败跳过该页继续
- **BASE 前缀**：与 cosplayD 相同的前端挂载机制

## 八、常见问题

- **视频解析失败**：
  - 帖子被删除/设为私密 → 无解，X 已移除内容
  - 敏感内容 → 需要配置完整 Cookie（含 twid），自动走 GraphQL
  - 不是视频帖子 → 检查链接
- **媒体抓取提示"Cookie 无效"**：Cookie 过期或字段不全，
  按 Cookie 设置面板提示的缺失字段重新复制
- **图片是缩略图**：确认 URL 带 `?name=large`（工具已自动处理），
  若仍小可能原帖就是小图
- **抓取慢**：媒体时间线要翻多页（每页 40 条 + 限速），
  大账户（几百条）需要十几秒，属正常
