# OpenCD Free RSS

一个轻量 Python 脚本，用来轮询 OpenCD RSS，打开条目详情页识别促销信息，并把 `free` / `2xfree` 种子添加到 Transmission 或 qBittorrent。

## 功能

- 每 10 分钟读取一次 RSS，默认读取 RSS 返回的最多 50 条。
- 逐个打开详情页，默认每个详情请求间隔 10 秒，避免瞬间打满站点。
- 识别 `free` 和 `2xfree` 促销。
- 已添加的种子不重复添加；非促销详情最多检查 3 次后跳过。
- 支持 Transmission 和 qBittorrent WebUI。
- 支持 Transmission 按 tracker 匹配 OpenCD 种子，让其不受全局上传限速影响。
- 支持 CookieCloud 获取站点 cookie，也支持静态 `SITE_COOKIE`。
- 支持 Telegram 通知 CookieCloud 失败、cookie 失效、Cloudflare 挑战、下载器添加失败。
- 自动裁剪日志，避免日志无限增长。

## 准备

需要 Python 3。使用 CookieCloud 加密同步时，系统还需要有 `openssl` 命令。

```bash
cp .env.example test.env
# 编辑 test.env，填入 RSS、下载器、CookieCloud、Telegram 等配置
```

## 下载器

默认使用 Transmission：

```env
DOWNLOAD_CLIENT=transmission
TRANSMISSION_URL=http://127.0.0.1:9091/transmission/rpc
TRANSMISSION_USERNAME=
TRANSMISSION_PASSWORD=
```

切到 qBittorrent：

```env
DOWNLOAD_CLIENT=qbit
QBITTORRENT_URL=http://127.0.0.1:8080
QBITTORRENT_USERNAME=admin
QBITTORRENT_PASSWORD=你的密码
```

`DOWNLOAD_DIR` 和 `ADD_PAUSED` 对两个下载器都生效。

## 运行

任务调度模式（推荐）：

```bash
python3 opencd_free_rss.py --once
```

用户 crontab 每 10 分钟执行一轮：

```cron
# opencd-free-rss
*/10 * * * * cd /home/liouyuze123/opencd-free-rss && flock -n opencd_free_rss.once.lock python3 opencd_free_rss.py --once >> opencd_free_rss.log 2>&1 # opencd-free-rss
```

`flock` 用来防止上一轮还没跑完时重叠执行。

常驻模式（可选）：

```bash
python3 opencd_free_rss.py
./start.sh
```

`start.sh` 会避免重复启动已有的 `opencd_free_rss.py` 进程。

## 运维检查

```bash
python3 opencd_free_rss.py --status
python3 opencd_free_rss.py --cookie-test
python3 opencd_free_rss.py --notify-test
```

- `--status`：查看 `seen.json` 中已添加、待检查、已耗尽检查次数的统计。
- `--cookie-test`：检查当前能否拿到 cookie，只输出数量，不输出 cookie 值。
- `--notify-test`：发送一条 Telegram 测试通知。

## 主要配置

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `RSS_URL` | 必填 | OpenCD RSS 地址 |
| `SITE_COOKIE` | 空 | 静态站点 cookie，CookieCloud 不可用时兜底 |
| `DOWNLOAD_CLIENT` | `transmission` | 下载器，支持 `transmission` / `tr` / `qbittorrent` / `qbit` / `qb` |
| `TRANSMISSION_URL` | `http://127.0.0.1:9091/transmission/rpc` | Transmission RPC 地址 |
| `TRANSMISSION_USERNAME` / `TRANSMISSION_PASSWORD` | 空 | Transmission 登录信息 |
| `TRANSMISSION_UNLIMIT_UPLOAD_TRACKER` | `open.cd` | Transmission 按 tracker 匹配种子，取消 per-torrent 上传限速并让其不受全局上传限速影响；留空可关闭 |
| `QBITTORRENT_URL` | `http://127.0.0.1:8080` | qBittorrent WebUI 地址 |
| `QBITTORRENT_USERNAME` / `QBITTORRENT_PASSWORD` | 空 | qBittorrent WebUI 登录信息 |
| `DOWNLOAD_DIR` | 空 | 下载目录，空则使用下载器默认目录 |
| `ADD_PAUSED` | `false` | 是否以暂停状态添加 |
| `POLL_SECONDS` | `600` | RSS 轮询间隔 |
| `REQUEST_DELAY_SECONDS` | `10` | 详情页请求间隔 |
| `MAX_DETAIL_CHECKS` | `3` | 非促销详情最多检查次数 |
| `STATE_FILE` | `seen.json` | 状态文件 |
| `COOKIECLOUD_URL` / `COOKIECLOUD_UUID` / `COOKIECLOUD_PASSWORD` | 空 | CookieCloud 配置 |
| `COOKIECLOUD_HOST` | `open.cd` | 从 CookieCloud 中筛选的域名 |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | 空 | Telegram 通知配置 |
| `LOG_FILE` | `opencd_free_rss.log` | 日志文件 |
| `LOG_MAX_BYTES` | `2097152` | 日志超过该大小后保留尾部 |
| `USER_AGENT` | 浏览器 UA | 请求使用的 User-Agent，默认伪装成常见浏览器 |
