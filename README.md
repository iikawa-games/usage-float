# usage-float

Windows 用量 HUD：把 Claude / Codex / Grok 订阅用量（以及可选的 LiteLLM 代理额度）铺在专用副屏上，双击 Ctrl 切到文件夹动态壁纸。

无标题栏、不占任务栏。没有登录凭证的 provider 不会出现占位行。

## 截图

专用副屏用量面板：

![专用副屏用量面板](docs/screenshots/panel.png)

副屏设置：

![副屏设置](docs/screenshots/settings-display.png)

动态壁纸目录权重（PlayStation △□○✕ 为示例目录名）：

![动态壁纸随机权重](docs/screenshots/settings-wallpaper.png)

## 需要什么

- Windows
- Python 3.10+（带 tkinter）
- 可选：`.\install-mpv.ps1` 下载副屏动态壁纸用的 mpv

凭证都在本机，不经过第三方服务器：

| Provider | 默认凭证 |
|---|---|
| Claude | `~/.claude/.credentials.json` |
| Codex | `~/.codex/auth.json` |
| Codex 第二账号 | `~/.codex-2/auth.json`（可用 `CODEX_HOME_2`） |
| Grok | `~/.grok` 登录态 |
| LiteLLM 代理 | 环境变量 `LLM_PROXY_API_KEY` + `LLM_PROXY_ENDPOINT`，或 `~/.usage-float/config.json` 里的同名字段。若本机装了 LiWork，也会尝试读取其 `llmProxyApiKey` / `llmProxyEndpoint` |

第二 Codex 账号：

```powershell
$env:CODEX_HOME="$env:USERPROFILE\.codex-2"; codex login
```

## 启动

```powershell
python usage_float.py once          # 命令行看一眼
pythonw usage_float.py              # 浮窗 / 副屏
.\start.vbs                         # 脱离当前会话启动
python usage_float.py autostart on  # 开机自启
```

## 交互

| 操作 | 效果 |
|------|------|
| 点击左下角刷新区 | 手动刷新 |
| 点击 Codex 用量数字 | 弹出该账号的重置卡列表 |
| 拖动 | 移动悬浮窗 |
| 右键 | 刷新 / 设置 / 切换专用副屏 / 置顶 / 开机自启 / 退出 |
| 连续按两次 Ctrl | 用量仪表盘 ↔ 动态壁纸；Ctrl+C / Ctrl+V 不会触发 |
| `F5` | 刷新 |
| `Esc` | 退出 |
| `Home` | 复位到主屏角落 |

## 专用副屏

右键 → **设置…** → 选中目标显示器 → 打开「专用副屏模式」。程序用显示器硬件标识锁定副屏，重新插拔后会再铺满该屏；显示器被拔掉时临时退回悬浮窗。

## 动态壁纸

先运行 `.\install-mpv.ps1`（或把 `mpv.exe` 放到 `vendor/mpv/`）。然后设置里添加媒体目录。

- 支持常见 JPG / PNG / WebP / GIF / MP4 / WebM / MKV / MOV，含子目录
- 每次进入随机首项；30 分钟以内的视频从 0:00 播，更长的优先随机章节、没有章节则随机时间
- 图片默认停留 10 秒（2–300 秒可调）
- 双击 Ctrl 切回用量时 mpv 进程退出，不留后台解码
- 可给子目录加权；竖图居中裁成正方形，横图和视频铺满副屏
- 点击副屏后：视频左右键 ±5 秒，图片左右键上一张/下一张，滚轮调音量
- 可把视频拖到副屏立即播放，结束后回到轮播

第三方解码器许可证见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## Codex 重置卡

符合条件的 Plus / Pro 账号会发「用量重置」机会，30 天内自己选时间用。点 Codex 那一行的用量数字打开列表，**每行只动对应账号**。

- 列表 `GET https://chatgpt.com/backend-api/wham/rate-limit-reset-credits`
- 核销 `POST …/consume`

每次点击带随机 `redeem_request_id`，接口按它去重。

## LiteLLM 代理用量

部分 LiteLLM 虚拟 key 不能读 `/key/info`。刷新时会打一次很便宜的 embeddings（失败则再试短 chat），从响应头读：

- `x-litellm-key-spend`
- `x-litellm-key-max-budget`

面板显示 `$已用/$额度`。没配 key 就不显示这一行。探测模型可用环境或代码里的默认值覆盖。

## 配置

运行后写到 `~/.usage-float/config.json`（不要提交这个文件）。常用字段：

```json
{
  "always_on_top": true,
  "providers": ["codex", "codex-2", "grok", "claude", "llmproxy"],
  "refresh_seconds": 300,
  "display_mode": "panel",
  "double_ctrl_toggle": true,
  "wallpaper_folders": ["D:\\Wallpapers"],
  "wallpaper_image_seconds": 10,
  "wallpaper_audio": true
}
```

`refresh_seconds` 下限 300 秒。Claude 遇到 429 会保留上次成功数据并标 stale。

## 开发

```powershell
python -m unittest test_usage_float.py
```

## 许可

MIT。mpv 为 GPL，由 `install-mpv.ps1` 另行下载，不包含在本仓库源码树里。
