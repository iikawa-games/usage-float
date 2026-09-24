# usage-float

Windows 用量 HUD：把 Claude / Codex / Grok 订阅用量（以及可选的 LiteLLM 代理额度）铺在专用副屏上，双击 Ctrl 切到文件夹动态壁纸。无标题栏、不占任务栏。

## 截图

专用副屏用量面板：

![专用副屏用量面板](docs/screenshots/panel.png)

用量设置（扫描、勾选、拖动排序）：

![用量设置](docs/screenshots/settings-usage.png)

副屏与快捷键设置：

![副屏与快捷键设置](docs/screenshots/settings-display.png)

动态壁纸目录权重（PlayStation △□○✕ 为示例目录名）：

![动态壁纸随机权重](docs/screenshots/settings-wallpaper.png)

## 安装与启动

**发行包（推荐）**：从 [Releases](https://github.com/iikawa-games/usage-float/releases) 下载 `usage-float-*-windows-x64.zip`，解压后双击 `UsageFloat.exe`。不需要 Python，已内置 mpv。未签名程序可能触发 SmartScreen，选「仍要运行」即可。开机自启在右键菜单里开关。

**源码运行**：Windows + Python 3.10+（带 tkinter）。动态壁纸需先执行 `.\install-mpv.ps1`，或把 `mpv.exe` 放到 `vendor/mpv/`。

```powershell
python usage_float.py once          # 命令行看一眼（发行包：UsageFloat.exe once）
pythonw usage_float.py              # 浮窗 / 副屏
.\start.vbs                         # 脱离当前会话启动
python usage_float.py autostart on  # 开机自启
```

## 凭证

凭证都在本机，不经过第三方服务器：

| Provider | 默认凭证 |
|---|---|
| Claude | `~/.claude/.credentials.json` |
| Codex | `~/.codex/auth.json` |
| Codex 第二账号 | `~/.codex-2/auth.json`（可用 `CODEX_HOME_2`） |
| Grok | `~/.grok` 登录态 |
| LiteLLM 代理 | 环境变量 `LLM_PROXY_API_KEY` + `LLM_PROXY_ENDPOINT`，或配置里的 `llm_proxy_api_key` / `llm_proxy_endpoint` |

登录第二个 Codex 账号：

```powershell
$env:CODEX_HOME="$env:USERPROFILE\.codex-2"; codex login
```

## 交互

| 操作 | 效果 |
|------|------|
| 点击左下角刷新区 / `F5` | 刷新 |
| 点击 Codex 用量数字 | 该账号的重置卡列表 |
| 点击 `claude 7d` / `claude 5h` 数字 | Claude 用量重置 / 5 小时重置 |
| 拖动 | 移动悬浮窗 |
| 右键 | 刷新 / 设置 / 切换专用副屏 / 置顶 / 开机自启 / 退出 |
| 快捷键（默认连按两次 Ctrl） | 用量仪表盘 ↔ 动态壁纸 |
| `Esc` | 退出 |
| `Home` | 复位到主屏角落 |

## 设置

右键 → **设置…**。

**用量**

- **扫描 provider**：读取本机登录态，列出已知 provider 以及是否已登录
- **勾选「显示」**：只画勾上的行；空格键切换当前行
- **拖动行**：按住名称或状态列上下拖，调整面板顺序（点第一列是勾选，不是拖动）
- Claude 拆成 `fable 7d`、`claude 7d`、`claude 5h` 三行，各自勾选、排序，可与其他 provider 穿插
- 未登录的 provider 即使勾选也不占位，登录后才出现
- LiteLLM 代理的重置时区，默认 UTC+8

**副屏**：选中目标显示器 → 打开「专用副屏模式」。程序按显示器硬件标识锁定副屏，重新插拔后再铺满；显示器被拔掉时临时退回悬浮窗。快捷键也在这一页，可改成连按 N 次或组合键。

**动态壁纸**：添加媒体目录（含子目录），可给子目录加权。

- 支持常见 JPG / PNG / WebP / GIF / MP4 / WebM / MKV / MOV
- 每次进入随机首项；30 分钟以内的视频从头播，更长的优先随机章节，没有章节则随机时间
- 图片默认停留 10 秒（2–300 秒可调）；竖图居中裁成正方形，横图和视频铺满副屏
- 点击副屏后：视频左右键 ±5 秒，图片左右键上一张/下一张，滚轮调音量
- 把视频拖到副屏立即播放，结束后回到轮播
- 切回用量时 mpv 进程退出，不留后台解码

## Codex 重置卡

符合条件的 Plus / Pro 账号会发「用量重置」机会，30 天内自选时间使用。点 Codex 那一行的用量数字打开列表，**每行只动对应账号**。

- 列表 `GET https://chatgpt.com/backend-api/wham/rate-limit-reset-credits`
- 核销 `POST …/consume`，每次带随机 `redeem_request_id`，接口按它去重

## Claude 用量重置

Anthropic 会给符合条件的账号发「用量重置」（CLI 的 `/limit-reset`，内部代号 cedar-ember）。点 `claude 7d` 的数字，弹窗列出每份额度的有效期和能重置的窗口，选中后「使用」并确认。

状态随常规用量请求一起读，**不增加额外调用**：

```
GET https://api.anthropic.com/api/oauth/usage?cedar_ember=1&skip_spend=1
```

这个字段只发给 CLI（普通请求返回 `ineligible_reason: "surface"`），所以请求带上本机已装 Claude Code 的版本号（`User-Agent: claude-cli/<版本>`、`anthropic-client-platform: cli`）。版本号实时读取 `@anthropic-ai/claude-code/package.json`，不写死。

使用时发送与 `/limit-reset` 相同的请求：

```
POST https://api.anthropic.com/api/organizations/<organizationUuid>/reset_rate_limits
{"program": "cedar_ember", "grant_id": "<next_grant_id>", "request_id": "<uuid>"}
```

- 服务端只核销 `next_grant_id` 那一份，其余额度显示为「排队中」、不可选
- 组织 ID 取自 `~/.claude.json` 的 `oauthAccount.organizationUuid`，取不到再查 `/api/oauth/profile`
- 没收到结果（断网、5xx、429）时，重试沿用同一个 `request_id`，不会多扣

### 5 小时重置

点 `claude 5h` 的数字：每周一次、只重置 5 小时会话（内部代号 juniper-tide）。这是实验功能，只有分到 `reset` 组的账号才有，且要真正撞到 5 小时上限才开放。常规用量请求里该字段为 `null`，只有 `?at_wall=1&skip_spend=1` 才会填，所以打开弹窗时单独读一次（只读）。使用时发送：

```
POST https://api.anthropic.com/api/organizations/<organizationUuid>/reset_rate_limits
{"program": "juniper_tide"}
```

没有 `request_id`；每周一次的额度本身防止重复核销（第二次返回 `already_used`）。

## LiteLLM 代理用量

LiteLLM 虚拟 key 通常读不到 `/key/info`。刷新时打一次很便宜的 embeddings（失败再试短 chat），从响应头 `x-litellm-key-spend`、`x-litellm-key-max-budget` 读出 `$已用/$额度`。重置倒计时按 LiteLLM 的周规则（每周一 0:00）计算，时区可在设置里改，或用 `LLM_PROXY_TIMEZONE` / `llm_proxy_timezone`。没配 key 就不显示这一行。

## 配置

写在 `~/.usage-float/config.json`，一般用设置页改即可。常用字段：

```json
{
  "always_on_top": true,
  "providers": ["codex", "codex-2", "grok", "claude", "llmproxy"],
  "provider_order": ["codex", "codex-2", "grok", "claude", "llmproxy"],
  "disabled_providers": [],
  "refresh_seconds": 300,
  "display_mode": "panel",
  "shortcut_enabled": true,
  "shortcut_mode": "repeat",
  "shortcut_key": "ctrl",
  "shortcut_repeat_count": 2,
  "shortcut_combo": ["ctrl", "alt"],
  "wallpaper_folders": ["D:\\Wallpapers"],
  "wallpaper_image_seconds": 10,
  "wallpaper_audio": true,
  "llm_proxy_api_key": "",
  "llm_proxy_endpoint": "",
  "llm_proxy_budget_duration": "7d",
  "llm_proxy_timezone": "UTC+8"
}
```

`refresh_seconds` 下限 300 秒。Claude 被限流（429）时继续显示一天内最近一次成功读到的数据。这个文件可能含代理 key，不要提交。

## 开发

```powershell
python -m unittest test_usage_float.py
.\build-release.ps1    # 生成 dist\usage-float-<version>-windows-x64.zip
```

## 许可

MIT。mpv 为 GPL：源码树不含二进制，由 `install-mpv.ps1` 下载；Release 的 zip 附带 mpv，见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
