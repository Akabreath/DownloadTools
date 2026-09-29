# bili · bilibili 视频与 UP主投稿下载

DownloadTools 门户子工具，挂载于 `/bili/`。

## 功能
1. **视频解析下载**：粘贴视频链接或整段分享文字均可（支持桌面链接
   `www.bilibili.com/video/BVxx`、`b23.tv` 短链），自动识别分P（可勾选下载哪些P），
   显示各清晰度大小：
   - 720P 及以下：单文件流，浏览器直连下载（服务器纯流式代理，不落盘）
   - 1080P/1080P60/4K：B站音视频分离 DASH 流，服务器临时下载 + ffmpeg 合并后推送
2. **UP主投稿下载**：`space.bilibili.com/<mid>`，WBI 签名接口分页抓取全部投稿
   （带登录 Cookie 才能稳定过风控），文件夹与 ZIP 以 **UP主名** 命名，默认打包 ZIP

## 设计约定（用户要求）
- 下载全部直接推送浏览器/手机，**无「保存到服务器」选项**；服务器仅临时中转，
  任务完成后 **1 小时自动清理** downloads 目录与 ZIP（history 仅留记录）
- 文件命名：单视频 = 视频标题；多P = `标题 P{n}.mp4`；合集文件夹/ZIP = UP主名
- 1080P+ 需登录 Cookie（SESSDATA/bili_jct/DedeUserID），Cookie 存 `config.json`

## 关键实现
- `x/web-interface/view` 拿标题/UP主/分P；`x/player/playurl`(fnval=0/16) 拿直链；
  高清档逐个请求验证真实可用（非会员 accept 虚列 1080P60 会被过滤）
- 空间列表 `x/space/wbi/arc/search`：WBI 签名（nav 拿 img/sub key + mixin 混淆表，
  内存缓存 6h），`data.page.count` 分页（ps=30），页间 sleep 0.4s
- DASH 下载：`baseUrl`(或 backup_url) 直拉 `.m4s`（实为 mp4 分段，H.264/AAC
  `-c copy` 流拷贝合并），`Range: bytes=0-0` 探测大小
- b23 短链手动跟随 302，兼容停在 `b23.tv/video/BVxxx` 中间态（再请求返回 200）
  → `to_www_url()` 归一化
- 风控码 -412/-799/-352/-509 指数退避重试；CDN 直链域名白名单防 SSRF
- 中文文件名用 `filename*=UTF-8''` (RFC 5987)
- 后端改代码后：`sudo systemctl restart downloadtools` 生效
- 清理逻辑兼容进程重启：history 条目同样到期删除文件（sweep 每 5 分钟 + 请求时惰性）

## 验证
- 实测样本：`BV1DJbu6HExJ`（1P 16s，720P 2.89MB / 1080P 6.5MB 合并 OK）；
  饼干Saku (mid 3546915322988637) 136 个视频全量抓取；合集 2 视频 ZIP 5.46MB
