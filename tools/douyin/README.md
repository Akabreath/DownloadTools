# douyin · 抖音视频 / 图集 / 主页作品下载

DownloadTools 门户子工具，挂载于 `/douyin/`。

## 功能
1. **单作品解析下载**：粘贴整段分享文案或链接均可（自动抽取 `v.douyin.com` 短链、
   `douyin.com/video/<id>`、`…?modal_id=<id>`）。显示封面/作者/时长/点赞收藏，
   视频按 `video.bit_rate` 列出全部清晰度档（含码率与文件大小，无水印单文件直链）；
   图文（图集）作品逐张推送图片。下载走**服务器流式代理**，不落服务器磁盘、不进历史。
2. **主页作品下载**：分享的「查看TA的更多作品」短链或 `douyin.com/user/<sec_uid>`，
   分页抓取全部作品（视频 + 图文），可勾选、可设清晰度上限、默认打包 ZIP，
   文件夹与 ZIP 以**作者昵称**命名。服务器仅临时中转，任务完成 1 小时后自动清理。

## 设计约定（沿用 bili/xdl 工具）
- 下载全部推送浏览器/手机，无「保存到服务器」选项
- 单作品 = 流式代理（不落盘）；主页批量 = 临时落盘 + ZIP + history 记录 + 1h 清理
- 命名：单作品 = 作品标题（`desc`，截断 90 字符）；图集 = `标题_01.jpg…`；ZIP = 作者昵称

## 关键实现（排障必读）
- **抖音 web 接口必须带 a_bogus 签名 + ttwid + msToken cookie**：缺任意一项都返回
  HTTP 200 但 body 为空（静默拒绝，不是报错）。签名见 `backend/abogus.py`
  （纯 Python 实现：SM3 + RC4 + 自定义 base64；SM3 为内置实现，与 gmssl 输出逐字节一致，
  因为项目的 venv 里没有 pip、不能装 gmssl）
- **签名与发送必须用同一个 query 字符串**：`dy_get()` 里先 `urlencode(params)`，
  再用同一个字符串算 a_bogus，最后 `?{query}&a_bogus={...}`，顺序/编码不一致即失效
- **ttwid 自动获取**：`POST https://ttwid.bytedance.com/ttwid/union/register/`
  （service=www.ixigua.com，union=true），结果缓存进 `config.json`，12 小时过期；
  请求失败会自动强制刷新重试
- **主页翻页需要登录态**：匿名请求 `aweme/v1/web/aweme/post/` 只能拿到抖音返回的
  前若干条（实测某账号 anonymous 上限 41 条，且第 2 页恒返回空 body，换 cursor
  参数、伪造匿名 cookie、真浏览器匿名访问都不行）→ 配置网页版登录 Cookie
  （须含 `sessionid`）后可完整翻页（实测 158 → 抓 159 条）。未配置 Cookie 时界面会
  明确提示并按匿名结果返回，不会假装抓全
- **直链有防盗链**：不带 `Referer: https://www.douyin.com/` 直接 403；因此下载统一走
  `/api/stream`（流式转发 + 白名单域名校验防 SSRF）
- 清晰度用 `video.bit_rate[]`（`quality_type` 为档位标识、`play_addr` 为**无水印**地址、
  `data_size` 直接给出大小，无需 HEAD 探测）；`download_addr` 是带水印版本，未使用
- 清晰度命名按**短边**：竖屏视频 `height` 是长边（1080x1920 的 height=1920），
  按长边会显示成 2K/4K，故取 `min(width, height)`
- 短链跳转落点是 `www.iesdouyin.com/share/video|user/…`，`parse_target()` 兼容
  iesdouyin / www / m 三种域名形态与 `sec_uid=` query
- 参考 URL 里带 `aweme_id` 的页面地址可能触发页面跳转，统一用 `https://www.douyin.com/`
- 中文文件名用 `filename*=UTF-8''`（RFC 5987）；大字节省流式 `iter_chunked(1<<16)`
- 后端改代码后：`sudo systemctl restart downloadtools` 生效；独立运行端口 8812

## 验证（实测记录）
- 分享文案 `… https://v.douyin.com/httvHr1UzhU/` → aweme 7688166223825299904，
  作者「一只豆豆」，25s，14 档清晰度；4K 档 8.24MB 实际下载成功
- 分享文案 `… https://v.douyin.com/1aC_JrWrBt0/` → sec_uid（user 主页）
- 主页抓取（带登录 Cookie）：抓 159 条 / 主页显示 158，无截断；下载 2 条视频
  19.6MB + ZIP 19.6MB 正常
- 未配置 Cookie 时：单作品解析下载正常；主页只返回匿名可见部分并提示配置 Cookie
