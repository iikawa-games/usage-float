#!/usr/bin/env python3
"""
usage-float — light translucent usage HUD for Claude / Codex / Grok / LLM Proxy.

  • One row per signed-in provider; a provider without credentials is hidden
  • Multiple Codex accounts, one per CODEX_HOME (~/.codex, ~/.codex-2, …)
  • Optional LiteLLM / OpenAI-compatible proxy spend (key headers)
  • Compact floating rows or a full-screen auxiliary-display instrument grid
  • Bottom bar: one clickable relative-time + refresh control
  • Auto refresh every 5 minutes; click the whole control to refresh now
  • On failure, that model line becomes「更新失败」
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------

HOME = Path.home()
APP_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
VERSION = "1.3.1"
CLAUDE_HOME = Path(os.environ.get("CLAUDE_HOME", HOME / ".claude"))
CODEX_HOME = Path(os.environ.get("CODEX_HOME", HOME / ".codex"))
CODEX_HOME_2 = Path(os.environ.get("CODEX_HOME_2", HOME / ".codex-2"))
GROK_HOME = Path(os.environ.get("GROK_HOME", HOME / ".grok"))
LIWORK_ORCA_PATH = (
    Path(os.environ.get("APPDATA", str(HOME / "AppData" / "Roaming")))
    / "liwork"
    / "profiles"
    / "local-default"
    / "orca-data.json"
)
CONFIG_PATH = HOME / ".usage-float" / "config.json"
CACHE_PATH = HOME / ".usage-float" / "usage-cache.json"
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", str(APP_DIR)))
WALLPAPER_START_SCRIPT_PATH = RESOURCE_DIR / "wallpaper-start.lua"
if not WALLPAPER_START_SCRIPT_PATH.exists():
    WALLPAPER_START_SCRIPT_PATH = APP_DIR / "wallpaper-start.lua"
_INSTANCE_MUTEX_HANDLE: Any = None

CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_OAUTH_BETA = "oauth-2025-04-20"
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
# "cedar-ember" is Claude Code's internal name for the weekly /limit-reset
# entitlement. Its status rides along on the usage endpoint as a query flag,
# so reading it costs no extra call against an API that answers 429 easily.
CLAUDE_USAGE_RESET_URL = f"{CLAUDE_USAGE_URL}?cedar_ember=1&skip_spend=1"
# The 5h session reset (juniper_tide) is only filled in on the read the CLI
# makes at a limit; the regular read above returns it as null.
CLAUDE_USAGE_AT_WALL_URL = f"{CLAUDE_USAGE_URL}?at_wall=1&skip_spend=1"
CLAUDE_PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
# Redeem call used by the CLI's /limit-reset, keyed by the OAuth organization.
CLAUDE_RESET_CLAIM_URL = "https://api.anthropic.com/api/organizations/{org}/reset_rate_limits"
CLAUDE_RESET_PROGRAM = "cedar_ember"
# The CLI refuses to send ids outside these shapes; so does the HUD.
CLAUDE_RESET_GRANT_ID_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
CLAUDE_RESET_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# The server only hands that block to the CLI surface: a plain request comes
# back with ineligible_reason="surface". Report the version actually installed
# so the gate reflects this machine instead of a number frozen into the file.
CLAUDE_CLI_VERSION_FALLBACK = "2.1.278"
CLAUDE_CLI_PACKAGE_PATHS: tuple[Path, ...] = (
    Path(os.environ.get("APPDATA", str(HOME)))
    / "npm"
    / "node_modules"
    / "@anthropic-ai"
    / "claude-code"
    / "package.json",
    CLAUDE_HOME / "local" / "node_modules" / "@anthropic-ai" / "claude-code" / "package.json",
    HOME / ".npm-global" / "lib" / "node_modules" / "@anthropic-ai" / "claude-code" / "package.json",
)

CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
# Banked rate-limit resets ("重置卡"): OpenAI grants them to eligible plans and
# they stay redeemable for 30 days instead of firing the moment they arrive.
CODEX_RESET_CARDS_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"
CODEX_RESET_CONSUME_URL = (
    "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume"
)
CODEX_RESET_CARD_STATUS_AVAILABLE = "available"
CODEX_RESET_CARD_STATUS_TEXT = {
    "available": "可用",
    "redeeming": "正在使用",
    "redeemed": "已使用",
}



@dataclass(frozen=True)
class CodexAccount:
    """One signed-in Codex account, identified by its own CODEX_HOME.

    Codex stores exactly one credential per home directory, so a second account
    is simply a second home. Every Codex call takes the account it belongs to
    rather than reading the process-wide default.
    """

    provider_id: str
    display_name: str
    home: Path

    @property
    def auth_path(self) -> Path:
        return self.home / "auth.json"


CODEX_ACCOUNTS: tuple[CodexAccount, ...] = (
    CodexAccount("codex", "codex", CODEX_HOME),
    CodexAccount("codex-2", "codex-2", CODEX_HOME_2),
)
CODEX_ACCOUNT_BY_ID: dict[str, CodexAccount] = {
    account.provider_id: account for account in CODEX_ACCOUNTS
}

# Claude is one login but several rows, and each row is ordered and toggled
# on its own in Settings. Every other provider is a single row under its id.
CLAUDE_DISPLAY_ITEMS: tuple[str, ...] = ("claude:fable", "claude:7d", "claude:5h")
CLAUDE_WINDOW_ITEMS: dict[str, str] = {
    "seven_day": "claude:7d",
    "five_hour": "claude:5h",
}

# Display order. A provider only appears when it is actually signed in, so a
# machine with one Codex account shows one Codex row.
DEFAULT_PROVIDERS: tuple[str, ...] = (
    "codex",
    "codex-2",
    "grok",
    *CLAUDE_DISPLAY_ITEMS,
    "llmproxy",
)
PROVIDER_LABELS: dict[str, str] = {
    "codex": "Codex",
    "codex-2": "Codex 2",
    "grok": "Grok",
    "claude": "Claude",
    "claude:fable": "fable 7d",
    "claude:7d": "claude 7d",
    "claude:5h": "claude 5h",
    "llmproxy": "LLM Proxy",
}


def provider_fetch_id(item: str) -> str:
    """The provider a display item is fetched from ("claude:5h" -> "claude")."""
    return "claude" if item.startswith("claude:") else item

# SuperGrok / Grok Build credit window (same endpoint CC Switch / CodexBar use).
# NOT cli-chat-proxy.grok.com/v1/billing — that returns 0/0 for subscription accounts.
GROK_BILLING_URL = "https://grok.com/grok_api_v2.GrokBuildBilling/GetGrokCreditsConfig"

# Many LiteLLM virtual keys cannot call /key/info. Spend and budget come back
# on actual LLM responses as x-litellm-key-spend / x-litellm-key-max-budget.
# Probe models are overridable; leave empty unless env or config supplies an endpoint.
LLM_PROXY_DEFAULT_ENDPOINT = ""
LLM_PROXY_EMBED_MODEL = "gemini-embedding-2"
LLM_PROXY_CHAT_FALLBACK_MODEL = "glm-5.3-flash"
# Virtual keys often cannot call /key/info. Weekly (7d) budgets reset Monday
# midnight in the proxy timezone (UTC+8 unless configured).
LLM_PROXY_BUDGET_DURATION_DEFAULT = "7d"
LLM_PROXY_TIMEZONE_DEFAULT = "UTC+8"
LLM_PROXY_RESET_HEADERS = (
    "x-litellm-key-budget-reset-at",
    "x-litellm-budget-reset-at",
    "x-litellm-key-reset-at",
)
LLM_PROXY_DURATION_HEADERS = (
    "x-litellm-key-budget-duration",
    "x-litellm-budget-duration",
)

REFRESH_SECONDS = 300
CLAUDE_429_BACKOFF_SECONDS = 900
WINDOW_ALPHA = 0.92  # higher alpha = sharper GDI text on Windows layered windows
WARN_PCT = 70
DANGER_PCT = 90
# Defaults (overridable in ~/.usage-float/config.json via 设置)
CARD_WIDTH = 248
CARD_PADX = 10
ROW_HEIGHT = 24  # derived from font_size at runtime
FONT_SIZE_DEFAULT = 12
FONT_SIZE_MIN = 9
FONT_SIZE_MAX = 22
CARD_WIDTH_MIN = 180
CARD_WIDTH_MAX = 480
CARD_HEIGHT_MIN = 56
CARD_HEIGHT_MAX = 720
DISPLAY_MODE_FLOAT = "floating"
DISPLAY_MODE_PANEL = "panel"
MONITOR_POLL_MS = 3000
DOUBLE_CTRL_INTERVAL_SECONDS = 0.45
DOUBLE_CTRL_POLL_MS = 20
SHORTCUT_MODE_REPEAT = "repeat"
SHORTCUT_MODE_COMBO = "combo"
SHORTCUT_KEY_DEFAULT = "ctrl"
SHORTCUT_REPEAT_COUNT_DEFAULT = 2
SHORTCUT_REPEAT_COUNT_MIN = 2
SHORTCUT_REPEAT_COUNT_MAX = 8
SHORTCUT_COMBO_DEFAULT: tuple[str, ...] = ("ctrl", "alt")
CTRL_VIRTUAL_KEYS = frozenset((0x11, 0xA2, 0xA3))
_MODIFIER_VIRTUAL_KEYS: dict[str, tuple[int, ...]] = {
    "ctrl": (0x11, 0xA2, 0xA3),
    "shift": (0x10, 0xA0, 0xA1),
    "alt": (0x12, 0xA4, 0xA5),
    "win": (0x5B, 0x5C),
}
_NAMED_VIRTUAL_KEYS: dict[str, tuple[int, ...]] = {
    **_MODIFIER_VIRTUAL_KEYS,
    "esc": (0x1B,),
    "escape": (0x1B,),
    "tab": (0x09,),
    "space": (0x20,),
    "enter": (0x0D,),
}
# IME state/mode virtual keys can remain reported as physically down by
# GetAsyncKeyState (notably VK_KANJI=0x19 with Sogou). They are not real
# Ctrl combinations and must not poison double-Ctrl detection.
IME_STATE_VIRTUAL_KEYS = frozenset(
    (0x15, 0x17, 0x18, 0x19, 0x1C, 0x1D, 0x1E, 0x1F, 0xE5, 0xE7)
)
WALLPAPER_IMAGE_SECONDS = 10
WALLPAPER_IMAGE_EXTENSIONS = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".bmp",
        ".gif",
        ".avif",
    }
)
WALLPAPER_VIDEO_EXTENSIONS = frozenset(
    {
        ".mp4",
        ".mkv",
        ".webm",
        ".mov",
        ".avi",
        ".m4v",
        ".wmv",
        ".flv",
        ".ts",
        ".mpeg",
        ".mpg",
    }
)
WALLPAPER_MEDIA_EXTENSIONS = WALLPAPER_IMAGE_EXTENSIONS | WALLPAPER_VIDEO_EXTENSIONS
WALLPAPER_READY_POLL_MS = 25
WALLPAPER_FIRST_FRAME_SETTLE_SECONDS = 0.18
WALLPAPER_VIDEO_PREROLL_MS = 120
WALLPAPER_DIR_WEIGHT_DEFAULT = 10
WALLPAPER_DIR_WEIGHT_MIN = 0
WALLPAPER_DIR_WEIGHT_MAX = 100
WALLPAPER_RANDOM_MODE_DIRECTORY = "directory"
WALLPAPER_RANDOM_MODE_GLOBAL = "global"

# Light card colors
BG = "#ffffff"  # plate color (bg layer)
FG = "#2c2c2c"
FG_MUTED = "#9a9a9a"
FG_LABEL = "#555555"
GREEN = "#22a06b"
YELLOW = "#c98900"
RED = "#d14343"
TRACK = "#e1e5e9"
DIVIDER = "#d5dade"
PANEL_BG = "#000000"
PANEL_FG = "#f5f7fa"
PANEL_LABEL = "#e6edf3"
PANEL_MUTED = "#c7d0d9"
PANEL_TRACK = "#252b32"
PANEL_GREEN = "#35d07f"
PANEL_YELLOW = "#f5bd35"
PANEL_RED = "#ff6b6b"
# Match the original plate colour so ClearType anti-aliasing keeps the same
# neutral edge colours.  A saturated chroma key bleeds into every glyph.
TRANSPARENT_KEY = BG
BG_OPACITY_DEFAULT = 88  # 0–100 plate
TEXT_OPACITY_DEFAULT = 100  # 0–100 text layer
OPACITY_MIN = 5
OPACITY_MAX = 100


def _canonical_key_name(raw: str | None) -> str:
    text = (raw or "").strip().lower()
    aliases = {
        "control": "ctrl",
        "ctl": "ctrl",
        "vk_control": "ctrl",
        "option": "alt",
        "meta": "win",
        "super": "win",
        "windows": "win",
        "return": "enter",
        "escape": "esc",
    }
    return aliases.get(text, text)


def _vk_codes_for_key(name: str) -> tuple[int, ...]:
    key = _canonical_key_name(name)
    if not key:
        return ()
    if key in _NAMED_VIRTUAL_KEYS:
        return _NAMED_VIRTUAL_KEYS[key]
    if len(key) == 1 and "a" <= key <= "z":
        return (ord(key.upper()),)
    if len(key) == 1 and "0" <= key <= "9":
        return (ord(key),)
    if key.startswith("f") and key[1:].isdigit():
        index = int(key[1:])
        if 1 <= index <= 24:
            return (0x70 + index - 1,)
    return ()


def _named_key_down(get_key_state: Callable[[int], int], name: str) -> bool:
    return any(bool(get_key_state(vk) & 0x8000) for vk in _vk_codes_for_key(name))


def _shortcut_keys_currently_down(get_key_state: Callable[[int], int]) -> list[str]:
    found: list[str] = []
    for name in ("ctrl", "shift", "alt", "win"):
        if _named_key_down(get_key_state, name):
            found.append(name)
    for code in range(ord("A"), ord("Z") + 1):
        name = chr(code).lower()
        if _named_key_down(get_key_state, name):
            found.append(name)
    for code in range(ord("0"), ord("9") + 1):
        name = chr(code)
        if _named_key_down(get_key_state, name):
            found.append(name)
    for index in range(1, 13):
        name = f"f{index}"
        if _named_key_down(get_key_state, name):
            found.append(name)
    for name in ("esc", "tab", "space", "enter"):
        if _named_key_down(get_key_state, name) and name not in found:
            found.append(name)
    return found


def _normalize_shortcut_mode(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {SHORTCUT_MODE_COMBO, "chord", "hotkey", "组合", "组合键"}:
        return SHORTCUT_MODE_COMBO
    return SHORTCUT_MODE_REPEAT


def _normalize_shortcut_key(value: Any) -> str:
    key = _canonical_key_name(str(value or ""))
    if key in _MODIFIER_VIRTUAL_KEYS:
        return key
    return SHORTCUT_KEY_DEFAULT


def _normalize_shortcut_repeat_count(value: Any) -> int:
    try:
        count = int(value)
    except (TypeError, ValueError):
        count = SHORTCUT_REPEAT_COUNT_DEFAULT
    return max(SHORTCUT_REPEAT_COUNT_MIN, min(SHORTCUT_REPEAT_COUNT_MAX, count))


def _normalize_shortcut_combo(value: Any) -> list[str]:
    if isinstance(value, str):
        parts = [
            _canonical_key_name(part)
            for part in value.replace("+", " ").replace("-", " ").split()
        ]
    elif isinstance(value, (list, tuple)):
        parts = [_canonical_key_name(str(part)) for part in value]
    else:
        parts = []
    seen: list[str] = []
    for part in parts:
        if not part or part in seen or not _vk_codes_for_key(part):
            continue
        seen.append(part)
    order = list(_MODIFIER_VIRTUAL_KEYS)
    mods = [name for name in order if name in seen]
    rest = [name for name in seen if name not in order]
    normalized = mods + rest
    return normalized if len(normalized) >= 2 else list(SHORTCUT_COMBO_DEFAULT)


def _format_shortcut_combo(keys: list[str] | tuple[str, ...]) -> str:
    labels = {
        "ctrl": "Ctrl",
        "shift": "Shift",
        "alt": "Alt",
        "win": "Win",
        "esc": "Esc",
        "tab": "Tab",
        "space": "Space",
        "enter": "Enter",
    }
    return "+".join(labels.get(name, name.upper()) for name in keys)


def _normalize_shortcut_config(data: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = data if isinstance(data, dict) else {}
    enabled = raw.get("shortcut_enabled")
    if enabled is None:
        enabled = raw.get("double_ctrl_toggle", True)
    return {
        "shortcut_enabled": bool(enabled),
        "shortcut_mode": _normalize_shortcut_mode(raw.get("shortcut_mode")),
        "shortcut_key": _normalize_shortcut_key(raw.get("shortcut_key")),
        "shortcut_repeat_count": _normalize_shortcut_repeat_count(
            raw.get("shortcut_repeat_count")
        ),
        "shortcut_combo": _normalize_shortcut_combo(raw.get("shortcut_combo")),
        "double_ctrl_toggle": bool(enabled),
    }


@dataclass
class _RepeatKeyDetector:
    """N clean taps of one key, ignoring combos like Ctrl+C."""

    interval_seconds: float = DOUBLE_CTRL_INTERVAL_SECONDS
    tap_count: int = SHORTCUT_REPEAT_COUNT_DEFAULT
    key_down: bool = False
    press_used: bool = False
    taps: list[float] = field(default_factory=list)

    def reset(self) -> None:
        self.key_down = False
        self.press_used = False
        self.taps.clear()

    def update(self, key_down: bool, other_key_down: bool, now: float) -> bool:
        needed = max(1, int(self.tap_count))
        if key_down:
            if not self.key_down:
                self.press_used = bool(other_key_down)
            elif other_key_down:
                self.press_used = True
            self.key_down = True
            return False

        if not self.key_down:
            if self.taps and now - self.taps[-1] > self.interval_seconds:
                self.taps.clear()
            return False

        self.key_down = False
        if self.press_used:
            self.press_used = False
            self.taps.clear()
            return False

        if self.taps and now - self.taps[-1] > self.interval_seconds:
            self.taps.clear()
        self.taps.append(now)
        if len(self.taps) < needed:
            return False
        recent = self.taps[-needed:]
        if all(
            0.0 <= recent[index + 1] - recent[index] <= self.interval_seconds
            for index in range(needed - 1)
        ):
            self.taps.clear()
            return True
        return False


class _DoubleCtrlDetector(_RepeatKeyDetector):
    """Back-compat name for the default two-tap Ctrl detector."""

    @property
    def ctrl_down(self) -> bool:
        return self.key_down

    @property
    def last_tap_at(self) -> float | None:
        return self.taps[-1] if self.taps else None


@dataclass
class _ComboKeyDetector:
    held: bool = False

    def reset(self) -> None:
        self.held = False

    def update(self, active: bool) -> bool:
        if active:
            if self.held:
                return False
            self.held = True
            return True
        self.held = False
        return False


def _other_shortcut_key_down(
    get_key_state: Callable[[int], int],
    exclude_keys: list[str] | None = None,
) -> bool:
    excluded = set(IME_STATE_VIRTUAL_KEYS)
    for name in exclude_keys if exclude_keys is not None else ["ctrl"]:
        excluded.update(_vk_codes_for_key(name))
    return any(
        bool(get_key_state(vk) & 0x8000)
        for vk in range(1, 256)
        if vk not in excluded
    )


def _collect_wallpaper_media(folder: str | Path | None) -> list[Path]:
    """Return a stable recursive image/video playlist from a user folder."""
    if not folder:
        return []
    root = Path(folder).expanduser()
    if not root.is_dir():
        return []
    try:
        files = [
            path.resolve()
            for path in root.rglob("*")
            if path.is_file() and path.suffix.casefold() in WALLPAPER_MEDIA_EXTENSIONS
        ]
    except OSError:
        return []
    return sorted(files, key=lambda path: str(path).casefold())


def _wallpaper_path_key(path: str | Path) -> str:
    """Stable case-insensitive key for persisted Windows media directories."""
    expanded = os.path.abspath(os.path.normpath(str(Path(path).expanduser())))
    return os.path.normcase(expanded)


def _normalize_wallpaper_folders(
    value: Any,
    legacy_folder: str | Path | None = None,
) -> list[str]:
    raw: list[Any]
    if isinstance(value, (str, Path)):
        raw = [value]
    elif isinstance(value, (list, tuple)):
        raw = list(value)
    else:
        raw = []
    if not raw and legacy_folder:
        raw = [legacy_folder]
    folders: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not item:
            continue
        try:
            path = os.path.abspath(os.path.normpath(str(Path(item).expanduser())))
            key = os.path.normcase(path)
        except (OSError, TypeError, ValueError):
            continue
        if key in seen:
            continue
        seen.add(key)
        folders.append(path)
    return folders


def _normalize_wallpaper_disabled_folders(
    value: Any,
    folders: list[str] | tuple[str, ...],
) -> list[str]:
    """Keep disabled entries canonical, ordered, and limited to saved roots."""
    normalized_folders = _normalize_wallpaper_folders(folders)
    try:
        disabled_keys = {
            _wallpaper_path_key(folder)
            for folder in _normalize_wallpaper_folders(value)
        }
    except (OSError, TypeError, ValueError):
        disabled_keys = set()
    return [
        folder
        for folder in normalized_folders
        if _wallpaper_path_key(folder) in disabled_keys
    ]


def _enabled_wallpaper_folders(
    folders: list[str] | tuple[str, ...],
    disabled_folders: Any,
) -> list[str]:
    normalized_folders = _normalize_wallpaper_folders(folders)
    disabled = _normalize_wallpaper_disabled_folders(
        disabled_folders,
        normalized_folders,
    )
    disabled_keys = {_wallpaper_path_key(folder) for folder in disabled}
    return [
        folder
        for folder in normalized_folders
        if _wallpaper_path_key(folder) not in disabled_keys
    ]


def _normalize_wallpaper_weights(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, int] = {}
    for folder, raw_weight in value.items():
        if not folder:
            continue
        try:
            weight = int(raw_weight)
            key = _wallpaper_path_key(str(folder))
        except (OSError, TypeError, ValueError):
            continue
        normalized[key] = max(
            WALLPAPER_DIR_WEIGHT_MIN,
            min(WALLPAPER_DIR_WEIGHT_MAX, weight),
        )
    return normalized


def _merge_wallpaper_weight_values(
    current: Any,
    pending: Any,
) -> dict[str, int]:
    """Merge raw settings-entry values without relying on Tk focus changes."""
    merged = _normalize_wallpaper_weights(current)
    if not isinstance(pending, dict):
        return merged
    for folder, raw_weight in pending.items():
        if not folder:
            continue
        try:
            key = _wallpaper_path_key(folder)
        except (OSError, TypeError, ValueError):
            continue
        fallback = merged.get(key, WALLPAPER_DIR_WEIGHT_DEFAULT)
        try:
            weight = int(str(raw_weight).strip())
        except (TypeError, ValueError):
            weight = fallback
        merged[key] = max(
            WALLPAPER_DIR_WEIGHT_MIN,
            min(WALLPAPER_DIR_WEIGHT_MAX, weight),
        )
    return merged


def _commit_wallpaper_settings_before_close(
    commit_weights: Callable[[], None],
    apply_other_settings: Callable[[], None],
) -> None:
    """Preserve pending entry text before another handler can rebuild its rows."""
    commit_weights()
    apply_other_settings()


def _collect_wallpaper_media_folders(folders: list[str] | tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    seen: set[str] = set()
    for folder in folders:
        for path in _collect_wallpaper_media(folder):
            key = os.path.normcase(str(path))
            if key in seen:
                continue
            seen.add(key)
            files.append(path)
    return sorted(files, key=lambda path: str(path).casefold())


def _wallpaper_media_directories(files: list[Path]) -> list[tuple[Path, int]]:
    counts: dict[str, tuple[Path, int]] = {}
    for path in files:
        parent = path.parent
        key = _wallpaper_path_key(parent)
        existing = counts.get(key)
        counts[key] = (parent, (existing[1] if existing else 0) + 1)
    return sorted(counts.values(), key=lambda item: str(item[0]).casefold())


def _normalize_wallpaper_random_mode(value: Any) -> str:
    mode = str(value or WALLPAPER_RANDOM_MODE_GLOBAL).strip().casefold()
    if mode == WALLPAPER_RANDOM_MODE_DIRECTORY:
        return WALLPAPER_RANDOM_MODE_DIRECTORY
    return WALLPAPER_RANDOM_MODE_GLOBAL


def _randomize_wallpaper_session(
    files: list[Path],
    rng: random.Random | random.SystemRandom | None = None,
    *,
    folder_weights: dict[str, int] | None = None,
    random_mode: str = WALLPAPER_RANDOM_MODE_GLOBAL,
) -> tuple[list[Path], float | None]:
    """Shuffle globally or lock one session to a weighted media directory."""
    ordered = list(files)
    chooser = rng or random.SystemRandom()
    chooser.shuffle(ordered)
    random_mode = _normalize_wallpaper_random_mode(random_mode)
    if ordered and (
        folder_weights is not None
        or random_mode == WALLPAPER_RANDOM_MODE_DIRECTORY
    ):
        normalized_weights = _normalize_wallpaper_weights(folder_weights)
        directories: dict[str, list[Path]] = {}
        for path in ordered:
            directories.setdefault(_wallpaper_path_key(path.parent), []).append(path)
        weighted = [
            (
                key,
                normalized_weights.get(key, WALLPAPER_DIR_WEIGHT_DEFAULT),
            )
            for key in directories
        ]
        weighted = [(key, weight) for key, weight in weighted if weight > 0]
        # An all-zero setup cannot select a directory. Fall back to equal
        # probability so wallpaper mode still remains usable.
        if not weighted:
            weighted = [(key, 1) for key in directories]
        total = sum(weight for _, weight in weighted)
        if total > 0:
            target = chooser.uniform(0.0, float(total))
            running = 0.0
            chosen_key = weighted[-1][0]
            for key, weight in weighted:
                running += weight
                if target <= running:
                    chosen_key = key
                    break
            if random_mode == WALLPAPER_RANDOM_MODE_DIRECTORY:
                ordered = directories[chosen_key]
            else:
                chosen = directories[chosen_key][0]
                ordered.remove(chosen)
                ordered.insert(0, chosen)
    # Video sessions intentionally start at 0:00. Randomness applies to media
    # and directory selection only, never to playback progress.
    return ordered, None


def _with_preferred_wallpaper_first(
    files: list[Path],
    preferred: Path | None,
) -> list[Path]:
    """Put a dropped/requested file first without otherwise changing order."""
    if preferred is None:
        return list(files)
    preferred_key = os.path.normcase(str(preferred))
    return [
        preferred,
        *(
            path
            for path in files
            if os.path.normcase(str(path)) != preferred_key
        ),
    ]


def _build_wallpaper_playback_order(
    files: list[Path],
    *,
    preferred_first: Path | None = None,
    keep_order: bool = False,
    folder_weights: dict[str, int] | None = None,
    random_mode: str = WALLPAPER_RANDOM_MODE_GLOBAL,
    rng: random.Random | random.SystemRandom | None = None,
) -> tuple[list[Path], float | None]:
    if keep_order:
        return _with_preferred_wallpaper_first(files, preferred_first), None
    ordered, start_percent = _randomize_wallpaper_session(
        files,
        rng,
        folder_weights=folder_weights,
        random_mode=random_mode,
    )
    if preferred_first is None:
        return ordered, start_percent
    return _with_preferred_wallpaper_first(ordered, preferred_first), None


def _find_mpv_executable(configured: str | Path | None = None) -> Path | None:
    """Find bundled, configured, PATH, or Lively's existing mpv executable."""
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend(
        [
            APP_DIR / "vendor" / "mpv" / "mpv.exe",
            HOME / ".usage-float" / "runtime" / "mpv" / "mpv.exe",
        ]
    )
    local_app_data = Path(os.environ.get("LOCALAPPDATA", HOME / "AppData" / "Local"))
    candidates.extend(
        [
            local_app_data / "Programs" / "Lively Wallpaper" / "plugins" / "mpv" / "mpv.exe",
            local_app_data / "Lively Wallpaper" / "Mpv" / "mpv.exe",
        ]
    )
    from_path = shutil.which("mpv.exe") or shutil.which("mpv")
    if from_path:
        candidates.append(Path(from_path))
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            if resolved.is_file():
                return resolved
        except OSError:
            continue
    return None


def _write_wallpaper_playlist(files: list[Path], destination: Path) -> None:
    """Write our trusted local paths as file URIs for mpv's M3U parser."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    lines = ["#EXTM3U", *(path.resolve().as_uri() for path in files)]
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _build_mpv_wallpaper_command(
    executable: Path,
    host_hwnd: int,
    playlist_path: Path | None,
    *,
    image_seconds: int = WALLPAPER_IMAGE_SECONDS,
    first_file: Path | None = None,
    start_percent: float | None = None,
    ipc_pipe: str | None = None,
    paused: bool = False,
    audio_enabled: bool = True,
) -> list[str]:
    """Low-overhead mpv command adapted from Lively's documented backend."""
    hwnd_u32 = int(host_hwnd) & 0xFFFFFFFF
    command = [
        str(executable),
        "--no-config",
        "--profile=fast",
        "--hwdec=auto-safe",
        "--sub=no",
        "--cache=yes",
        "--cache-secs=2",
        "--demuxer-readahead-secs=2",
        "--demuxer-max-bytes=16MiB",
        "--demuxer-max-back-bytes=0",
        "--demuxer-hysteresis-secs=1",
        "--prefetch-playlist=yes",
        "--autoload-files=no",
        "--osc=no",
        "--osd-level=0",
        "--input-default-bindings=no",
        "--input-vo-keyboard=yes",
        "--input-cursor=yes",
        "--input-builtin-dragging=no",
        "--drag-and-drop=insert-next",
        "--cursor-autohide=always",
        "--no-border",
        "--no-terminal",
        "--really-quiet",
        "--keep-open=no",
        "--loop-playlist=inf",
        f"--image-display-duration={max(1, int(image_seconds))}",
        "--panscan=1.0",
        f"--wid={hwnd_u32}",
    ]
    command.insert(4, "--mute=yes" if audio_enabled else "--no-audio")
    if WALLPAPER_START_SCRIPT_PATH.is_file():
        command.append(f"--script={WALLPAPER_START_SCRIPT_PATH}")
    if ipc_pipe:
        command.append(f"--input-ipc-server={ipc_pipe}")
    if paused:
        command.append("--pause=yes")
    if first_file is not None:
        command.append("--{")
        if start_percent is not None:
            command.append(f"--start={max(0.0, min(90.0, start_percent)):.2f}%")
        command.extend([str(first_file), "--}"])
    if playlist_path is not None:
        command.append(f"--playlist={playlist_path}")
    return command


def _find_embedded_mpv_window(host_hwnd: int) -> int:
    """Return mpv's native child HWND once its render window exists."""
    if sys.platform != "win32" or not host_hwnd:
        return 0
    try:
        import ctypes
        from ctypes import wintypes

        find_window_ex = ctypes.windll.user32.FindWindowExW
        find_window_ex.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
        ]
        find_window_ex.restype = wintypes.HWND
        return int(find_window_ex(int(host_hwnd), 0, "mpv", None) or 0)
    except Exception:
        return 0


def _move_native_child_window(hwnd: int, x: int, y: int, width: int, height: int) -> bool:
    """Apply an immediate physical move to a Tk child HWND on Windows."""
    if sys.platform != "win32" or not hwnd:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        move_window = ctypes.windll.user32.MoveWindow
        move_window.argtypes = [
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.BOOL,
        ]
        move_window.restype = wintypes.BOOL
        return bool(
            move_window(
                int(hwnd),
                int(x),
                int(y),
                max(1, int(width)),
                max(1, int(height)),
                True,
            )
        )
    except Exception:
        return False


def _set_native_window_enabled(hwnd: int, enabled: bool) -> bool:
    if sys.platform != "win32" or not hwnd:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        enable_window = ctypes.windll.user32.EnableWindow
        enable_window.argtypes = [wintypes.HWND, wintypes.BOOL]
        enable_window.restype = wintypes.BOOL
        enable_window(wintypes.HWND(hwnd), wintypes.BOOL(bool(enabled)))
        return True
    except Exception:
        return False


def _dropped_wallpaper_videos(paths: Any) -> list[Path]:
    if not isinstance(paths, (list, tuple)):
        return []
    videos: list[Path] = []
    seen: set[str] = set()
    for raw_path in paths:
        try:
            path = Path(raw_path).expanduser().resolve()
        except (OSError, TypeError, ValueError):
            continue
        if not path.is_file() or path.suffix.casefold() not in WALLPAPER_VIDEO_EXTENSIONS:
            continue
        key = os.path.normcase(str(path))
        if key in seen:
            continue
        seen.add(key)
        videos.append(path)
    return videos


class _PendingFileDrops:
    """Hand Explorer paths from a native drop callback to the Tk thread.

    Native drop callbacks must not call into Tcl/Tk. Queue the paths here and
    let a Tk ``after`` poller drain them on the regular event loop.
    """

    def __init__(self, deliver: Callable[[list[str]], None]) -> None:
        self._deliver = deliver
        self._pending: list[list[str]] = []
        self._lock = threading.Lock()

    def push(self, files: list[str]) -> None:
        if not files:
            return
        with self._lock:
            self._pending.append(list(files))

    def drain(self) -> None:
        with self._lock:
            batches = self._pending
            self._pending = []
        for files in batches:
            try:
                self._deliver(files)
            except Exception:
                _log_exception("file drop")


def _install_windows_file_drop(
    widget: Any,
    on_files: Callable[[list[str]], None],
) -> Callable[[], None]:
    """Accept Explorer file drops on a Tk top-level without extra packages.

    OLE ``IDropTarget`` is used instead of subclassing the Tk WNDPROC.
    Replacing Tk's window procedure with a ctypes callback re-enters Tcl from
    inside DispatchMessage; with IME/antivirus hooks that abort()s pythonw
    (BEX64 / FAST_FAIL_FATAL_APP_EXIT) as soon as a file is dropped.
    """
    if sys.platform != "win32":
        return lambda: None
    try:
        import ctypes
        from ctypes import wintypes

        if ctypes.sizeof(ctypes.c_void_p) != 8:
            return lambda: None

        widget.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(widget.winfo_id()) or widget.winfo_id()
        if not hwnd:
            return lambda: None

        HRESULT = ctypes.c_long
        S_OK = 0
        E_NOINTERFACE = HRESULT(0x80004002).value
        E_POINTER = HRESULT(0x80004003).value
        E_UNEXPECTED = HRESULT(0x8000FFFF).value
        DROPEFFECT_NONE = 0
        DROPEFFECT_COPY = 1
        CF_HDROP = 15
        DVASPECT_CONTENT = 1
        TYMED_HGLOBAL = 1

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", ctypes.c_uint32),
                ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        class FORMATETC(ctypes.Structure):
            _fields_ = [
                ("cfFormat", wintypes.WORD),
                ("ptd", ctypes.c_void_p),
                ("dwAspect", wintypes.DWORD),
                ("lindex", wintypes.LONG),
                ("tymed", wintypes.DWORD),
            ]

        class STGMEDIUM(ctypes.Structure):
            _fields_ = [
                ("tymed", wintypes.DWORD),
                ("hGlobal", wintypes.HGLOBAL),
                ("pUnkForRelease", ctypes.c_void_p),
            ]

        class IDropTargetVtbl(ctypes.Structure):
            pass

        class IDropTarget(ctypes.Structure):
            _fields_ = [("lpVtbl", ctypes.POINTER(IDropTargetVtbl))]

        QueryInterfaceFn = ctypes.WINFUNCTYPE(
            HRESULT,
            ctypes.c_void_p,
            ctypes.POINTER(GUID),
            ctypes.POINTER(ctypes.c_void_p),
        )
        AddRefFn = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)
        ReleaseFn = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)
        # POINTL is two LONGs. On x64 that 8-byte struct is passed in one
        # register; declaring it as Structure would make ctypes pass a pointer.
        DragEnterFn = ctypes.WINFUNCTYPE(
            HRESULT,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_uint64,
            ctypes.POINTER(wintypes.DWORD),
        )
        DragOverFn = ctypes.WINFUNCTYPE(
            HRESULT,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_uint64,
            ctypes.POINTER(wintypes.DWORD),
        )
        DragLeaveFn = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p)
        DropFn = ctypes.WINFUNCTYPE(
            HRESULT,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_uint64,
            ctypes.POINTER(wintypes.DWORD),
        )
        GetDataFn = ctypes.WINFUNCTYPE(
            HRESULT,
            ctypes.c_void_p,
            ctypes.POINTER(FORMATETC),
            ctypes.POINTER(STGMEDIUM),
        )
        QueryGetDataFn = ctypes.WINFUNCTYPE(
            HRESULT,
            ctypes.c_void_p,
            ctypes.POINTER(FORMATETC),
        )

        IDropTargetVtbl._fields_ = [
            ("QueryInterface", QueryInterfaceFn),
            ("AddRef", AddRefFn),
            ("Release", ReleaseFn),
            ("DragEnter", DragEnterFn),
            ("DragOver", DragOverFn),
            ("DragLeave", DragLeaveFn),
            ("Drop", DropFn),
        ]

        iid_iunknown = GUID(
            0x00000000,
            0x0000,
            0x0000,
            (0xC0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46),
        )
        iid_idroptarget = GUID(
            0x00000122,
            0x0000,
            0x0000,
            (0xC0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46),
        )

        ole32 = ctypes.windll.ole32
        ole32.OleInitialize.argtypes = [ctypes.c_void_p]
        ole32.OleInitialize.restype = HRESULT
        ole32.OleUninitialize.argtypes = []
        ole32.OleUninitialize.restype = None
        ole32.RegisterDragDrop.argtypes = [wintypes.HWND, ctypes.c_void_p]
        ole32.RegisterDragDrop.restype = HRESULT
        ole32.RevokeDragDrop.argtypes = [wintypes.HWND]
        ole32.RevokeDragDrop.restype = HRESULT
        ole32.ReleaseStgMedium.argtypes = [ctypes.POINTER(STGMEDIUM)]
        ole32.ReleaseStgMedium.restype = None
        drag_query_file = ctypes.windll.shell32.DragQueryFileW
        drag_query_file.argtypes = [
            wintypes.HANDLE,
            wintypes.UINT,
            wintypes.LPWSTR,
            wintypes.UINT,
        ]
        drag_query_file.restype = wintypes.UINT

        def _guid_bytes(guid: GUID) -> bytes:
            return bytes(guid)

        def _vtbl_slot(this: int, index: int) -> int:
            vtbl = ctypes.c_void_p.from_address(int(this)).value
            if not vtbl:
                return 0
            slot = ctypes.c_void_p.from_address(
                int(vtbl) + index * ctypes.sizeof(ctypes.c_void_p)
            ).value
            return int(slot or 0)

        def _hdrop_cf() -> FORMATETC:
            fmt = FORMATETC()
            fmt.cfFormat = CF_HDROP
            fmt.ptd = None
            fmt.dwAspect = DVASPECT_CONTENT
            fmt.lindex = -1
            fmt.tymed = TYMED_HGLOBAL
            return fmt

        def _hdrop_paths(handle: int) -> list[str]:
            if not handle:
                return []
            drop_handle = wintypes.HANDLE(handle)
            count = int(drag_query_file(drop_handle, 0xFFFFFFFF, None, 0))
            paths: list[str] = []
            for index in range(count):
                length = int(drag_query_file(drop_handle, index, None, 0))
                if length <= 0:
                    continue
                buffer = ctypes.create_unicode_buffer(length + 1)
                if drag_query_file(drop_handle, index, buffer, length + 1):
                    paths.append(buffer.value)
            return paths

        def _dataobject_has_hdrop(pdata: int) -> bool:
            slot = _vtbl_slot(pdata, 5)
            if not slot:
                return False
            query = QueryGetDataFn(slot)
            return int(query(pdata, ctypes.byref(_hdrop_cf()))) >= 0

        def _dataobject_paths(pdata: int) -> list[str]:
            slot = _vtbl_slot(pdata, 3)
            if not slot:
                return []
            get_data = GetDataFn(slot)
            medium = STGMEDIUM()
            hr = int(get_data(pdata, ctypes.byref(_hdrop_cf()), ctypes.byref(medium)))
            if hr < 0:
                return []
            try:
                return _hdrop_paths(int(medium.hGlobal or 0))
            finally:
                ole32.ReleaseStgMedium(ctypes.byref(medium))

        def _set_effect(pdw_effect: Any, effect: int) -> None:
            if pdw_effect:
                pdw_effect[0] = effect

        ole_hr = int(ole32.OleInitialize(None))
        if ole_hr < 0:
            return lambda: None
        ole_owned = ole_hr == S_OK
        pending = _PendingFileDrops(on_files)
        active = True
        accepting = False
        refcount = 1

        @QueryInterfaceFn
        def query_interface(this: int, riid: Any, ppv: Any) -> int:
            try:
                if not ppv:
                    return E_POINTER
                guid = riid if isinstance(riid, GUID) else riid.contents
                requested = _guid_bytes(guid)
                if requested in (
                    _guid_bytes(iid_iunknown),
                    _guid_bytes(iid_idroptarget),
                ):
                    ppv[0] = this
                    add_ref(this)
                    return S_OK
                ppv[0] = None
                return E_NOINTERFACE
            except Exception:
                return E_UNEXPECTED

        @AddRefFn
        def add_ref(_this: int) -> int:
            nonlocal refcount
            refcount += 1
            return refcount

        @ReleaseFn
        def release(_this: int) -> int:
            nonlocal refcount
            refcount = max(0, refcount - 1)
            return refcount

        @DragEnterFn
        def drag_enter(
            _this: int,
            pdata: int,
            _key_state: int,
            _point: int,
            pdw_effect: Any,
        ) -> int:
            nonlocal accepting
            try:
                accepting = bool(active and pdata and _dataobject_has_hdrop(pdata))
                _set_effect(
                    pdw_effect,
                    DROPEFFECT_COPY if accepting else DROPEFFECT_NONE,
                )
                return S_OK
            except Exception:
                accepting = False
                _set_effect(pdw_effect, DROPEFFECT_NONE)
                return E_UNEXPECTED

        @DragOverFn
        def drag_over(
            _this: int,
            _key_state: int,
            _point: int,
            pdw_effect: Any,
        ) -> int:
            try:
                _set_effect(
                    pdw_effect,
                    DROPEFFECT_COPY if (active and accepting) else DROPEFFECT_NONE,
                )
                return S_OK
            except Exception:
                _set_effect(pdw_effect, DROPEFFECT_NONE)
                return E_UNEXPECTED

        @DragLeaveFn
        def drag_leave(_this: int) -> int:
            nonlocal accepting
            accepting = False
            return S_OK

        @DropFn
        def drop(
            _this: int,
            pdata: int,
            _key_state: int,
            _point: int,
            pdw_effect: Any,
        ) -> int:
            try:
                paths = _dataobject_paths(pdata) if active and pdata else []
                if paths:
                    pending.push(paths)
                _set_effect(
                    pdw_effect,
                    DROPEFFECT_COPY if paths else DROPEFFECT_NONE,
                )
                return S_OK
            except Exception:
                _set_effect(pdw_effect, DROPEFFECT_NONE)
                return E_UNEXPECTED

        vtbl = IDropTargetVtbl(
            query_interface,
            add_ref,
            release,
            drag_enter,
            drag_over,
            drag_leave,
            drop,
        )
        target = IDropTarget(ctypes.pointer(vtbl))
        target_ptr = ctypes.cast(ctypes.pointer(target), ctypes.c_void_p)
        register_hr = int(ole32.RegisterDragDrop(wintypes.HWND(hwnd), target_ptr))
        if register_hr < 0:
            if ole_owned:
                ole32.OleUninitialize()
            return lambda: None

        poll_job: Any = None

        def poll_pending() -> None:
            nonlocal poll_job
            poll_job = None
            if not active:
                return
            pending.drain()
            try:
                poll_job = widget.after(40, poll_pending)
            except Exception:
                poll_job = None

        try:
            poll_job = widget.after(40, poll_pending)
        except Exception:
            ole32.RevokeDragDrop(wintypes.HWND(hwnd))
            if ole_owned:
                ole32.OleUninitialize()
            return lambda: None

        def uninstall() -> None:
            nonlocal active, poll_job
            if not active:
                return
            active = False
            job = poll_job
            poll_job = None
            if job is not None:
                try:
                    widget.after_cancel(job)
                except Exception:
                    pass
            try:
                ole32.RevokeDragDrop(wintypes.HWND(hwnd))
            except Exception:
                pass
            if ole_owned:
                try:
                    ole32.OleUninitialize()
                except Exception:
                    pass

        setattr(uninstall, "_registered", True)
        # ctypes function-pointer fields do not keep the Python callables alive.
        setattr(
            uninstall,
            "_drop_state",
            (
                vtbl,
                target,
                target_ptr,
                pending,
                query_interface,
                add_ref,
                release,
                drag_enter,
                drag_over,
                drag_leave,
                drop,
            ),
        )
        return uninstall
    except Exception:
        _log_exception("file drop install")
        return lambda: None


def _send_mpv_ipc_command(pipe_name: str, command: list[Any]) -> bool:
    """Send one JSON IPC command to a Windows mpv named pipe."""
    if sys.platform != "win32" or not pipe_name:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        create_file = ctypes.windll.kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        write_file = ctypes.windll.kernel32.WriteFile
        write_file.argtypes = [
            wintypes.HANDLE,
            wintypes.LPCVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        write_file.restype = wintypes.BOOL
        close_handle = ctypes.windll.kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        generic_write = 0x40000000
        open_existing = 3
        handle = create_file(pipe_name, generic_write, 0, None, open_existing, 0, None)
        if int(handle) == ctypes.c_void_p(-1).value:
            return False
        try:
            payload = (json.dumps({"command": command}) + "\n").encode("utf-8")
            written = wintypes.DWORD(0)
            return bool(
                write_file(handle, payload, len(payload), ctypes.byref(written), None)
                and written.value == len(payload)
            )
        finally:
            close_handle(handle)
    except Exception:
        return False


class _MpvWallpaperPlayer:
    """Thin lifecycle wrapper; decoding/rendering remains entirely in mpv."""

    def __init__(self) -> None:
        self.process: subprocess.Popen[bytes] | None = None
        self.playlist_path = CONFIG_PATH.parent / "wallpaper-playlist.m3u8"
        self.last_error = ""
        self.last_order: list[Path] = []
        self.last_start_percent: float | None = None
        self.ipc_pipe = ""
        self._session_number = 0

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(
        self,
        executable: Path,
        host_hwnd: int,
        files: list[Path],
        *,
        image_seconds: int = WALLPAPER_IMAGE_SECONDS,
        audio_enabled: bool = True,
        folder_weights: dict[str, int] | None = None,
        random_mode: str = WALLPAPER_RANDOM_MODE_GLOBAL,
        preferred_first: Path | None = None,
        keep_order: bool = False,
    ) -> tuple[bool, str]:
        self.stop()
        if not files:
            self.last_error = "文件夹中没有支持的图片或视频"
            return False, self.last_error
        try:
            ordered, start_percent = _build_wallpaper_playback_order(
                files,
                preferred_first=preferred_first,
                keep_order=keep_order,
                folder_weights=folder_weights,
                random_mode=random_mode,
            )
            first_file = ordered[0]
            remaining = ordered[1:]
            if remaining:
                _write_wallpaper_playlist(remaining, self.playlist_path)
            self._session_number += 1
            self.ipc_pipe = (
                rf"\\.\pipe\usage-float-{os.getpid()}-{self._session_number}"
            )
            command = _build_mpv_wallpaper_command(
                executable,
                host_hwnd,
                self.playlist_path if remaining else None,
                image_seconds=image_seconds,
                first_file=first_file,
                start_percent=start_percent,
                ipc_pipe=self.ipc_pipe,
                paused=True,
                audio_enabled=audio_enabled,
            )
            creationflags = 0
            if sys.platform == "win32":
                creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
                creationflags |= getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            self.last_order = ordered
            self.last_start_percent = start_percent
            self.last_error = ""
            return True, ""
        except Exception as exc:
            self.process = None
            self.last_error = f"动态壁纸启动失败：{exc}"
            return False, self.last_error

    def set_paused(self, paused: bool) -> bool:
        if not self.running:
            return False
        return _send_mpv_ipc_command(
            self.ipc_pipe,
            ["set_property", "pause", bool(paused)],
        )

    def set_muted(self, muted: bool) -> bool:
        if not self.running:
            return False
        return _send_mpv_ipc_command(
            self.ipc_pipe,
            ["set_property", "mute", bool(muted)],
        )

    def stop(self) -> None:
        process = self.process
        self.process = None
        self.ipc_pipe = ""
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=0.8)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except Exception:
                pass
        except Exception:
            pass


def _clamp_opacity(value: Any, default: int) -> int:
    """Normalize a user-facing opacity percentage."""
    try:
        return max(OPACITY_MIN, min(OPACITY_MAX, int(value)))
    except (TypeError, ValueError):
        return default


def _apply_layer_opacities(
    text_window: Any,
    plate_window: Any,
    plate_opacity: Any,
    text_opacity: Any,
    *,
    restack: Callable[[], None] | None = None,
) -> None:
    """Apply plate and text opacity without coupling the two layers.

    The text layer deliberately uses ``text_opacity`` even when a separate
    plate is unavailable.  Falling back to the plate value would make the
    plate control fade the glyphs as well, which is worse than leaving the
    unsupported plate preview unchanged.
    """
    plate_alpha = _clamp_opacity(plate_opacity, BG_OPACITY_DEFAULT) / 100.0
    text_alpha = _clamp_opacity(text_opacity, TEXT_OPACITY_DEFAULT) / 100.0

    if plate_window is not None:
        try:
            plate_window.attributes("-alpha", plate_alpha)
        except Exception:
            pass

    # Keep this in a separate try block: a plate failure must never prevent a
    # live text-opacity update.
    try:
        text_window.attributes("-alpha", text_alpha)
    except Exception:
        pass

    if plate_window is not None and restack is not None:
        try:
            restack()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Config / cache
# ---------------------------------------------------------------------------


def _acquire_single_instance() -> bool:
    """Hold a per-session mutex so Windows startup restoration cannot duplicate the HUD."""
    global _INSTANCE_MUTEX_HANDLE
    if _INSTANCE_MUTEX_HANDLE is not None or sys.platform != "win32":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        create_mutex.restype = wintypes.HANDLE
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        handle = create_mutex(None, False, "Local\\UsageFloat.SingleInstance")
        if not handle:
            return True
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            close_handle(handle)
            return False
        _INSTANCE_MUTEX_HANDLE = handle
        return True
    except Exception:
        # A mutex failure must not make the HUD disappear completely. Startup
        # remains available, while process diagnostics can still reveal a fault.
        return True


def provider_label(provider_id: str) -> str:
    return PROVIDER_LABELS.get(provider_id, provider_id)


def _unique_provider_ids(values: Any) -> list[str]:
    out: list[str] = []
    for item in values or []:
        if isinstance(item, str):
            pid = item.strip()
            # Configs from before the per-row split name Claude once; it
            # stands for all of its rows, in their default order.
            for expanded in CLAUDE_DISPLAY_ITEMS if pid == "claude" else (pid,):
                if expanded and expanded not in out:
                    out.append(expanded)
    return out


def resolve_provider_config(
    providers: Any,
    disabled: Any = None,
    order: Any = None,
) -> tuple[list[str], list[str], list[str]]:
    """Return (order, enabled, disabled) for HUD + settings.

    Newly added DEFAULT_PROVIDERS are inserted into order and stay enabled
    unless they already appear in ``disabled``.
    """
    enabled_in = _unique_provider_ids(providers)
    disabled_list = _unique_provider_ids(disabled)
    disabled_in = set(disabled_list)
    known = _unique_provider_ids(list(order or []) + enabled_in + disabled_list)
    if not known:
        known = list(DEFAULT_PROVIDERS)
    for index, must in enumerate(DEFAULT_PROVIDERS):
        if must not in known:
            previous = DEFAULT_PROVIDERS[index - 1] if index else None
            if previous and must.startswith(previous + "-") and previous in known:
                known.insert(known.index(previous) + 1, must)
            else:
                known.append(must)
        if must not in disabled_in and must not in enabled_in:
            enabled_in.append(must)
    enabled_set = set(enabled_in) - disabled_in
    enabled = [pid for pid in known if pid in enabled_set]
    disabled_out = [pid for pid in known if pid not in enabled_set]
    return known, enabled, disabled_out


def move_provider(order: list[str], source: str, target: str) -> list[str]:
    """Place ``source`` at the current index of ``target``."""
    items = _unique_provider_ids(order)
    if source not in items or target not in items or source == target:
        return items
    destination = items.index(target)
    items.pop(items.index(source))
    items.insert(destination, source)
    return items


def scan_providers(
    configured: list[str] | None = None,
    order: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Scan known provider slots and report which ones have credentials."""
    known, _enabled, _disabled = resolve_provider_config(
        configured if configured is not None else list(DEFAULT_PROVIDERS),
        [],
        order,
    )
    return [
        {
            "id": pid,
            "label": provider_label(pid),
            "available": provider_available(pid),
        }
        for pid in known
    ]


def load_config() -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "always_on_top": True,
        "x": None,
        "y": None,
        "providers": list(DEFAULT_PROVIDERS),
        "provider_order": list(DEFAULT_PROVIDERS),
        "disabled_providers": [],
        "refresh_seconds": REFRESH_SECONDS,
        "alpha": WINDOW_ALPHA,  # legacy; migrated to bg_opacity
        "bg_opacity": BG_OPACITY_DEFAULT,
        "text_opacity": TEXT_OPACITY_DEFAULT,
        "font_size": FONT_SIZE_DEFAULT,
        "card_width": CARD_WIDTH,
        "card_height": None,  # None = auto height from content
        "allow_resize": False,
        "display_mode": DISPLAY_MODE_FLOAT,
        "monitor_device": None,
        "monitor_id": None,
        "double_ctrl_toggle": True,
        "shortcut_enabled": True,
        "shortcut_mode": SHORTCUT_MODE_REPEAT,
        "shortcut_key": SHORTCUT_KEY_DEFAULT,
        "shortcut_repeat_count": SHORTCUT_REPEAT_COUNT_DEFAULT,
        "shortcut_combo": list(SHORTCUT_COMBO_DEFAULT),
        "llm_proxy_budget_duration": LLM_PROXY_BUDGET_DURATION_DEFAULT,
        "llm_proxy_timezone": LLM_PROXY_TIMEZONE_DEFAULT,
        "wallpaper_folder": None,
        "wallpaper_folders": [],
        "wallpaper_disabled_folders": [],
        "wallpaper_subdir_weights": {},
        "wallpaper_random_mode": WALLPAPER_RANDOM_MODE_GLOBAL,
        "wallpaper_image_seconds": WALLPAPER_IMAGE_SECONDS,
        "wallpaper_audio": True,
        "mpv_path": None,
    }
    file_data: dict[str, Any] = {}
    try:
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                file_data = data
                defaults.update(data)
    except Exception:
        pass
    try:
        rs = float(defaults.get("refresh_seconds") or REFRESH_SECONDS)
    except Exception:
        rs = REFRESH_SECONDS
    defaults["refresh_seconds"] = max(REFRESH_SECONDS, rs)
    try:
        fs = int(defaults.get("font_size") or FONT_SIZE_DEFAULT)
    except Exception:
        fs = FONT_SIZE_DEFAULT
    defaults["font_size"] = max(FONT_SIZE_MIN, min(FONT_SIZE_MAX, fs))
    try:
        cw = int(defaults.get("card_width") or CARD_WIDTH)
    except Exception:
        cw = CARD_WIDTH
    defaults["card_width"] = max(CARD_WIDTH_MIN, min(CARD_WIDTH_MAX, cw))
    ch = defaults.get("card_height")
    if ch is not None:
        try:
            ch_i = int(ch)
            defaults["card_height"] = max(CARD_HEIGHT_MIN, min(CARD_HEIGHT_MAX, ch_i))
        except Exception:
            defaults["card_height"] = None
    defaults["allow_resize"] = bool(defaults.get("allow_resize", False))
    mode = str(defaults.get("display_mode") or DISPLAY_MODE_FLOAT).lower()
    defaults["display_mode"] = (
        mode if mode in (DISPLAY_MODE_FLOAT, DISPLAY_MODE_PANEL) else DISPLAY_MODE_FLOAT
    )
    monitor_device = defaults.get("monitor_device")
    defaults["monitor_device"] = (
        str(monitor_device).strip() if monitor_device else None
    )
    monitor_id = defaults.get("monitor_id")
    defaults["monitor_id"] = str(monitor_id).strip() if monitor_id else None
    shortcut = _normalize_shortcut_config(defaults)
    defaults.update(shortcut)
    defaults["llm_proxy_budget_duration"] = (
        str(defaults.get("llm_proxy_budget_duration") or "").strip()
        or LLM_PROXY_BUDGET_DURATION_DEFAULT
    )
    defaults["llm_proxy_timezone"] = _normalize_llm_proxy_timezone(
        defaults.get("llm_proxy_timezone")
    )
    wallpaper_folders = _normalize_wallpaper_folders(
        defaults.get("wallpaper_folders"),
        defaults.get("wallpaper_folder"),
    )
    defaults["wallpaper_folders"] = wallpaper_folders
    # Keep the old field as a compatibility mirror for older releases.
    defaults["wallpaper_folder"] = wallpaper_folders[0] if wallpaper_folders else None
    defaults["wallpaper_disabled_folders"] = _normalize_wallpaper_disabled_folders(
        defaults.get("wallpaper_disabled_folders"),
        wallpaper_folders,
    )
    defaults["wallpaper_subdir_weights"] = _normalize_wallpaper_weights(
        defaults.get("wallpaper_subdir_weights")
    )
    defaults["wallpaper_random_mode"] = _normalize_wallpaper_random_mode(
        defaults.get("wallpaper_random_mode")
    )
    try:
        image_seconds = int(defaults.get("wallpaper_image_seconds") or WALLPAPER_IMAGE_SECONDS)
    except (TypeError, ValueError):
        image_seconds = WALLPAPER_IMAGE_SECONDS
    defaults["wallpaper_image_seconds"] = max(2, min(300, image_seconds))
    defaults["wallpaper_audio"] = bool(defaults.get("wallpaper_audio", True))
    mpv_path = defaults.get("mpv_path")
    defaults["mpv_path"] = str(mpv_path).strip() if mpv_path else None
    # Opacity 0–100; migrate legacy alpha (0–1 float) → bg_opacity once
    if "bg_opacity" not in defaults or defaults.get("bg_opacity") is None:
        try:
            legacy = float(defaults.get("alpha") or WINDOW_ALPHA)
            defaults["bg_opacity"] = int(round(legacy * 100)) if legacy <= 1.0 else int(legacy)
        except Exception:
            defaults["bg_opacity"] = BG_OPACITY_DEFAULT
    defaults["bg_opacity"] = _clamp_opacity(
        defaults.get("bg_opacity"), BG_OPACITY_DEFAULT
    )
    defaults["text_opacity"] = _clamp_opacity(
        defaults.get("text_opacity"), TEXT_OPACITY_DEFAULT
    )
    if "provider_order" in file_data:
        order_arg: Any = file_data.get("provider_order")
    elif "providers" in file_data or "disabled_providers" in file_data:
        order_arg = _unique_provider_ids(
            list(file_data.get("providers") or [])
            + list(file_data.get("disabled_providers") or [])
        )
    else:
        order_arg = defaults.get("provider_order")
    order, enabled, disabled = resolve_provider_config(
        defaults.get("providers"),
        defaults.get("disabled_providers"),
        order_arg,
    )
    defaults["provider_order"] = order
    defaults["providers"] = enabled
    defaults["disabled_providers"] = disabled
    shot_root = os.environ.get("USAGE_FLOAT_DOCS_SHOT_ROOT")
    if shot_root:
        root = Path(shot_root)
        defaults["wallpaper_folders"] = [str(root)]
        defaults["wallpaper_folder"] = str(root)
        defaults["wallpaper_disabled_folders"] = []
        defaults["display_mode"] = DISPLAY_MODE_FLOAT
        defaults["always_on_top"] = False
        defaults["double_ctrl_toggle"] = True
        defaults["shortcut_enabled"] = True
        defaults["shortcut_mode"] = SHORTCUT_MODE_REPEAT
        defaults["shortcut_key"] = SHORTCUT_KEY_DEFAULT
        defaults["shortcut_repeat_count"] = SHORTCUT_REPEAT_COUNT_DEFAULT
        defaults["shortcut_combo"] = list(SHORTCUT_COMBO_DEFAULT)
        weights: dict[str, int] = {}
        ordered = [root, *(sorted(root.iterdir()) if root.exists() else [])]
        values = (100, 100, 100, 10)
        index = 0
        for path in ordered:
            if not path.is_dir():
                continue
            weights[_wallpaper_path_key(path)] = values[min(index, len(values) - 1)]
            index += 1
        defaults["wallpaper_subdir_weights"] = weights
    return defaults


def _docs_shot_active() -> bool:
    return bool(os.environ.get("USAGE_FLOAT_DOCS_SHOT"))


def _capture_widget_png(widget: Any, path: Path) -> None:
    """PrintWindow a Tk toplevel to PNG. Used only for docs screenshots."""
    import ctypes
    from ctypes import wintypes

    widget.update_idletasks()
    widget.deiconify()
    widget.lift()
    widget.update()
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    hwnd = user32.FindWindowW(None, str(widget.title()))
    if not hwnd:
        hwnd = int(widget.winfo_id())
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    width = max(1, int(rect.right - rect.left))
    height = max(1, int(rect.bottom - rect.top))
    hwnd_dc = user32.GetWindowDC(hwnd)
    mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
    bmp = gdi32.CreateCompatibleBitmap(hwnd_dc, width, height)
    gdi32.SelectObject(mem_dc, bmp)
    user32.PrintWindow(hwnd, mem_dc, 2)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", ctypes.c_long),
            ("biHeight", ctypes.c_long),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", ctypes.c_long),
            ("biYPelsPerMeter", ctypes.c_long),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER)]

    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = width
    bmi.bmiHeader.biHeight = -height
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0
    buf = ctypes.create_string_buffer(width * height * 4)
    gdi32.GetDIBits(mem_dc, bmp, 0, height, buf, ctypes.byref(bmi), 0)
    from PIL import Image

    Image.frombuffer("RGBA", (width, height), buf, "raw", "BGRA", 0, 1).save(path)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem_dc)
    user32.ReleaseDC(hwnd, hwnd_dc)


def save_config(cfg: dict[str, Any]) -> None:
    if _docs_shot_active():
        return
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass


def load_usage_cache() -> dict[str, Any]:
    try:
        if CACHE_PATH.exists():
            data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {"providers": {}, "claude_backoff_until": None}


def save_usage_cache(cache: dict[str, Any]) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        t = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(t)
    except Exception:
        return None


def format_remaining(resets_at: str | None, now: datetime | None = None) -> str:
    dt = parse_iso(resets_at)
    if not dt:
        return ""
    now = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    seconds = int((dt - now).total_seconds())
    if seconds <= 0:
        return "soon"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours > 0:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m"


def format_relative(ts: float | None, now: float | None = None) -> str:
    if not ts:
        return "尚未更新"
    now = now or time.time()
    sec = int(max(0, now - ts))
    if sec < 15:
        return "刚刚"
    if sec < 60:
        return f"{sec} 秒前"
    if sec < 3600:
        return f"{sec // 60} 分钟前"
    if sec < 86400:
        return f"{sec // 3600} 小时前"
    return f"{sec // 86400} 天前"


def _format_refresh_control(
    last_ok_at: float | None,
    refreshing: bool,
    now: float | None = None,
) -> str:
    relative = "更新中…" if refreshing else format_relative(last_ok_at, now)
    return f"{relative}  ↻"


def _point_in_box(
    x: int,
    y: int,
    box: tuple[int, int, int, int] | None,
) -> bool:
    if box is None:
        return False
    left, top, right, bottom = box
    return left <= x < right and top <= y < bottom


def _compact_row_height(linespace: int, font_size: int) -> int:
    """Keep a small safety margin without turning each line into a loose row."""
    return max(linespace + 2, font_size + 9)


def _effective_card_height(
    configured_height: int | None,
    allow_resize: bool,
) -> int | None:
    # A fixed height only has meaning while manual resizing is enabled.
    return configured_height if allow_resize else None


def tone_color(pct: float) -> str:
    if pct >= DANGER_PCT:
        return RED
    if pct >= WARN_PCT:
        return YELLOW
    return GREEN


def panel_tone_color(pct: float) -> str:
    if pct >= DANGER_PCT:
        return PANEL_RED
    if pct >= WARN_PCT:
        return PANEL_YELLOW
    return PANEL_GREEN


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class UsageWindow:
    id: str
    label: str
    used_pct: float
    resets_at: str | None = None


@dataclass
class ProviderUsage:
    provider_id: str
    display_name: str
    plan_label: str | None = None
    windows: list[UsageWindow] = field(default_factory=list)
    error: str | None = None
    available: bool = True
    stale: bool = False
    fetched_at: str | None = None
    # Free-form one-line summary override (e.g. grok credits without %)
    summary: str | None = None
    # Raw provider-specific reset entitlement block (Claude's cedar_ember).
    # Kept as the untouched payload so a field added upstream survives the
    # cache round-trip without a schema change here.
    reset_block: dict[str, Any] | None = None


def _provider_to_dict(p: ProviderUsage) -> dict[str, Any]:
    return {
        "provider_id": p.provider_id,
        "display_name": p.display_name,
        "plan_label": p.plan_label,
        "windows": [
            {
                "id": w.id,
                "label": w.label,
                "used_pct": w.used_pct,
                "resets_at": w.resets_at,
            }
            for w in p.windows
        ],
        "error": p.error,
        "available": p.available,
        "stale": p.stale,
        "fetched_at": p.fetched_at,
        "summary": p.summary,
        "reset_block": p.reset_block,
    }


def _provider_from_dict(d: dict[str, Any]) -> ProviderUsage:
    windows = [
        UsageWindow(
            id=str(w.get("id") or ""),
            label=str(w.get("label") or ""),
            used_pct=float(w.get("used_pct") or 0),
            resets_at=w.get("resets_at"),
        )
        for w in (d.get("windows") or [])
        if isinstance(w, dict)
    ]
    return ProviderUsage(
        provider_id=str(d.get("provider_id") or ""),
        display_name=str(d.get("display_name") or "?"),
        plan_label=d.get("plan_label"),
        windows=windows,
        error=d.get("error"),
        available=bool(d.get("available", True)),
        stale=bool(d.get("stale", False)),
        fetched_at=d.get("fetched_at"),
        summary=d.get("summary"),
        reset_block=d.get("reset_block") if isinstance(d.get("reset_block"), dict) else None,
    )


def cache_get_provider(provider_id: str) -> ProviderUsage | None:
    raw = (load_usage_cache().get("providers") or {}).get(provider_id)
    if not isinstance(raw, dict):
        return None
    p = _provider_from_dict(raw)
    if not p.windows and not p.summary:
        return None
    p.stale = True
    p.available = True
    return p


def cache_put_provider(p: ProviderUsage) -> None:
    if not p.available:
        return
    if not p.windows and not p.summary:
        return
    cache = load_usage_cache()
    providers = cache.setdefault("providers", {})
    snap = _provider_to_dict(p)
    snap["stale"] = False
    snap["error"] = None
    snap["fetched_at"] = p.fetched_at or datetime.now(timezone.utc).isoformat()
    providers[p.provider_id] = snap
    cache["providers"] = providers
    save_usage_cache(cache)


def claude_in_backoff() -> bool:
    until = load_usage_cache().get("claude_backoff_until")
    dt = parse_iso(str(until) if until else None)
    if not dt:
        return False
    return datetime.now(timezone.utc) < dt


def set_claude_backoff(seconds: int = CLAUDE_429_BACKOFF_SECONDS) -> None:
    cache = load_usage_cache()
    until = datetime.now(timezone.utc).timestamp() + seconds
    cache["claude_backoff_until"] = datetime.fromtimestamp(until, tz=timezone.utc).isoformat()
    save_usage_cache(cache)


def clear_claude_backoff() -> None:
    cache = load_usage_cache()
    cache["claude_backoff_until"] = None
    save_usage_cache(cache)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _parse_json_body(raw: str) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return raw


def http_json_response(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = 20.0,
) -> tuple[int, Any, dict[str, str]]:
    data = None
    req_headers = {"Accept": "application/json", "User-Agent": "usage-float/1.2"}
    if headers:
        req_headers.update(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return (
                resp.status,
                _parse_json_body(raw),
                {str(k).lower(): str(v) for k, v in resp.headers.items()},
            )
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        return (
            e.code,
            _parse_json_body(raw),
            {str(k).lower(): str(v) for k, v in e.headers.items()},
        )
    except Exception as e:
        return 0, str(e), {}


def http_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = 20.0,
) -> tuple[int, Any]:
    status, payload, _headers = http_json_response(
        url, method=method, headers=headers, body=body, timeout=timeout
    )
    return status, payload


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------


def read_claude_oauth() -> tuple[dict[str, Any] | None, Path | None]:
    cred_path = CLAUDE_HOME / ".credentials.json"
    if not cred_path.exists():
        return None, None
    try:
        data = json.loads(cred_path.read_text(encoding="utf-8"))
        oauth = data.get("claudeAiOauth") or {}
        if oauth.get("accessToken"):
            return oauth, cred_path
    except Exception:
        return None, None
    return None, None


def save_claude_oauth(cred_path: Path, oauth: dict[str, Any]) -> None:
    try:
        data = json.loads(cred_path.read_text(encoding="utf-8"))
        data["claudeAiOauth"] = oauth
        cred_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def refresh_claude_token(refresh_token: str) -> dict[str, Any] | None:
    status, payload = http_json(
        CLAUDE_TOKEN_URL,
        method="POST",
        body={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLAUDE_CLIENT_ID,
            "scope": "user:profile user:inference user:sessions:claude_code user:mcp_servers",
        },
    )
    if status != 200 or not isinstance(payload, dict):
        return None
    return payload


def claude_cli_version() -> str:
    """Version of the Claude Code CLI installed on this machine.

    The weekly-reset block is gated on the caller looking like a recent CLI, so
    a stale number would silently turn the field off. Read it fresh on every
    call — the CLI self-updates under a long-running HUD, and re-reading a 1 KB
    manifest once per refresh is cheaper than serving a version that has since
    dropped below the gate.
    """
    for path in CLAUDE_CLI_PACKAGE_PATHS:
        try:
            if not path.exists():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            found = str((data or {}).get("version") or "").strip()
            if found:
                return found
        except Exception:
            continue
    return CLAUDE_CLI_VERSION_FALLBACK


def claude_request_headers(token: str) -> dict[str, str]:
    version = claude_cli_version()
    return {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": CLAUDE_OAUTH_BETA,
        # Identify as the CLI surface; without this the response reports
        # ineligible_reason="surface" and the reset block is always empty.
        "User-Agent": f"claude-cli/{version} (external, cli)",
        "anthropic-client-platform": "cli",
        "anthropic-client-version": version,
    }


@dataclass
class ClaudeResetGrant:
    """One /limit-reset grant; the claim call names it by id."""

    grant_id: str
    resets_left: int = 0
    expires_at: str | None = None
    clears: tuple[str, ...] = ()
    usable_now: bool = False
    use_requires_limit: bool = False
    paused: bool = False


@dataclass
class ClaudeResetStatus:
    """Claude's weekly /limit-reset entitlement, as shown by the HUD."""

    eligible: bool = False
    ineligible_reason: str | None = None
    at_limit: bool = False
    resets_left: int = 0
    usable_now: bool = False
    use_requires_limit: bool = False
    expires_at: str | None = None
    weekly_resets_at: str | None = None
    cooldown_until: str | None = None
    # Describes the grant that lapses first: Anthropic ships a human label
    # ("Claude Opus 5.5 launch: one usage-limit reset…") and the list of
    # windows it refills.
    label: str | None = None
    clears: tuple[str, ...] = ()
    # Unspent grants, soonest expiry first. The server only redeems the one
    # named by next_grant_id and answers "not_next_grant" for any other.
    grants: tuple[ClaudeResetGrant, ...] = ()
    next_grant_id: str | None = None

    @property
    def available(self) -> bool:
        return self.resets_left > 0

    def claimable(self, grant: ClaudeResetGrant) -> bool:
        return (
            grant.grant_id == self.next_grant_id
            and grant.usable_now
            and not grant.paused
            and grant.resets_left > 0
        )


CLAUDE_RESET_WINDOW_TEXT = {
    "five_hour": "5 小时会话",
    "seven_day": "7 天周额度",
    "seven_day_overage_included": "7 天周额度（含超额）",
    "seven_day_opus": "Opus 周额度",
    "seven_day_sonnet": "Sonnet 周额度",
}


CLAUDE_RESET_REASON_TEXT = {
    "surface": "当前客户端不被允许读取重置额度",
    "cli_version": "Claude Code 版本过低，升级后才会发放",
    "plan": "当前订阅方案不包含用量重置",
    "no_profile_scope": "登录凭证缺少所需权限",
    "not_authorized": "账号未获授权",
}


def _claude_grant_clears(grant: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(window)
        for window in (_first_present(grant, "clears", "limit_types", "limitTypes") or [])
        if window
    )


def _claude_grant_int(grant: dict[str, Any], *names: str) -> int:
    value = _first_present(grant, *names)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def parse_claude_reset_status(block: Any) -> ClaudeResetStatus:
    """Read the cedar_ember block into the few fields the HUD actually shows."""
    if not isinstance(block, dict):
        return ClaudeResetStatus()
    reason = _first_present(block, "ineligible_reason", "ineligibleReason")
    status = ClaudeResetStatus(
        eligible=bool(_first_present(block, "eligible") or False),
        ineligible_reason=str(reason) if reason else None,
        at_limit=bool(_first_present(block, "at_limit", "atLimit") or False),
        weekly_resets_at=_iso_timestamp(
            _first_present(block, "weekly_resets_at", "weeklyResetsAt")
        ),
        cooldown_until=_iso_timestamp(
            _first_present(block, "cooldown_until", "cooldownUntil")
        ),
    )

    next_id = _first_present(block, "next_grant_id", "nextGrantId")
    status.next_grant_id = str(next_id) if next_id else None

    total = 0
    soonest_expiry: str | None = None
    grants: list[ClaudeResetGrant] = []
    for grant in _first_present(block, "grants") or []:
        if not isinstance(grant, dict):
            continue
        left = _claude_grant_int(grant, "resets_left", "resetsLeft")
        if left <= 0 or _first_present(grant, "paused"):
            continue
        total += left
        grant_id = _first_present(grant, "id", "grant_id", "grantId")
        if grant_id:
            grants.append(
                ClaudeResetGrant(
                    grant_id=str(grant_id),
                    resets_left=left,
                    expires_at=_iso_timestamp(_first_present(grant, "ends_at", "endsAt")),
                    clears=_claude_grant_clears(grant),
                    usable_now=bool(_first_present(grant, "usable_now", "usableNow")),
                    use_requires_limit=bool(
                        _first_present(grant, "use_requires_limit", "useRequiresLimit")
                    ),
                )
            )
        if _first_present(grant, "usable_now", "usableNow"):
            status.usable_now = True
        if _first_present(grant, "use_requires_limit", "useRequiresLimit"):
            status.use_requires_limit = True
        ends_at = _iso_timestamp(_first_present(grant, "ends_at", "endsAt"))
        # Describe the deadline that lapses first — that is the one worth acting on.
        if ends_at is None and soonest_expiry is not None:
            continue
        if ends_at is not None and soonest_expiry is not None and _iso_epoch(
            ends_at, float("inf")
        ) >= _iso_epoch(soonest_expiry, float("inf")):
            continue
        soonest_expiry = ends_at or soonest_expiry
        label = _first_present(grant, "label", "title", "name")
        status.label = str(label).strip() if label else None
        status.clears = _claude_grant_clears(grant)

    # Some responses only carry the rolled-up count, with no per-grant rows.
    status.resets_left = total or _claude_grant_int(
        block, "resets_left_total", "resetsLeftTotal", "resets_left"
    )
    status.expires_at = soonest_expiry
    status.grants = tuple(
        sorted(
            grants,
            key=lambda g: (_iso_epoch(g.expires_at, float("inf")), g.grant_id),
        )
    )
    return status


def claude_reset_status(provider: ProviderUsage | None) -> ClaudeResetStatus:
    if provider is None:
        return ClaudeResetStatus()
    return parse_claude_reset_status(provider.reset_block)


def claude_authorized_json(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    timeout: float = 20.0,
) -> tuple[int, Any]:
    """Call an Anthropic OAuth endpoint, refreshing the token once on 401/403.

    Returns status 0 when there are no local credentials at all.
    """
    oauth, cred_path = read_claude_oauth()
    if not oauth:
        return 0, None

    def call(token: str) -> tuple[int, Any]:
        return http_json(
            url,
            method=method,
            headers=claude_request_headers(token),
            body=body,
            timeout=timeout,
        )

    status, payload = call(oauth["accessToken"])
    if status not in (401, 403):
        return status, payload
    rt = oauth.get("refreshToken")
    if not rt or not cred_path:
        return status, payload
    refreshed = refresh_claude_token(rt)
    if not refreshed or not refreshed.get("access_token"):
        return status, payload
    oauth["accessToken"] = refreshed["access_token"]
    if refreshed.get("refresh_token"):
        oauth["refreshToken"] = refreshed["refresh_token"]
    save_claude_oauth(cred_path, oauth)
    return call(oauth["accessToken"])


def claude_organization_uuid() -> str | None:
    """The OAuth organization the claim is filed under.

    Claude Code records it in .claude.json at login; the profile endpoint is
    the fallback when that file is somewhere unexpected.
    """
    for path in (CLAUDE_HOME / ".claude.json", HOME / ".claude.json"):
        try:
            if not path.exists():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            found = str(((data or {}).get("oauthAccount") or {}).get("organizationUuid") or "")
            if found.strip():
                return found.strip()
        except Exception:
            continue
    status, payload = claude_authorized_json(CLAUDE_PROFILE_URL)
    if status == 200 and isinstance(payload, dict):
        found = str((payload.get("organization") or {}).get("uuid") or "").strip()
        if found:
            return found
    return None


@dataclass
class ClaudeResetClaim:
    ok: bool
    message: str
    # False when the outcome is unknown (network drop, 5xx): the next attempt
    # must resend the same request_id so the server can dedupe it instead of
    # spending a second reset.
    settled: bool = True


CLAUDE_RESET_RESULT_TEXT = {
    "already_used": "这次重置已经用过了。",
    "not_limited": "还没到用量上限，暂时不能使用，额度未消耗。",
    "cooldown": "刚用过一次重置，冷却中，稍后再试。",
    "ineligible": "当前账号不符合使用条件。",
    "unavailable": "重置暂不可用，稍后再试。",
}


def claim_claude_reset(grant_id: str, *, request_id: str) -> ClaudeResetClaim:
    """Spend one /limit-reset grant. Only call this after the user confirms."""
    if not CLAUDE_RESET_GRANT_ID_RE.match(grant_id or "") or not (
        CLAUDE_RESET_REQUEST_ID_RE.match(request_id or "")
    ):
        return ClaudeResetClaim(False, "重置数据格式异常，未发送请求。")
    org = claude_organization_uuid()
    if not org:
        return ClaudeResetClaim(False, "找不到 Claude 组织信息，请在 Claude Code 里重新登录。")
    status, payload = claude_authorized_json(
        CLAUDE_RESET_CLAIM_URL.format(org=urllib.parse.quote(org, safe="")),
        method="POST",
        body={
            "program": CLAUDE_RESET_PROGRAM,
            "grant_id": grant_id,
            "request_id": request_id,
        },
        timeout=25.0,
    )
    if status == 0 and payload is None:
        return ClaudeResetClaim(False, "未找到可用的 Claude 登录凭证")
    if status == 200 and isinstance(payload, dict):
        result = str(payload.get("result") or "unavailable")
        if result == "reset":
            cleared = "、".join(
                CLAUDE_RESET_WINDOW_TEXT.get(str(w), str(w))
                for w in payload.get("cleared") or []
            )
            message = f"已重置{cleared}。" if cleared else "用量已重置。"
            left = payload.get("resets_left")
            if isinstance(left, int):
                message += f" 剩余 {left} 次。"
            return ClaudeResetClaim(True, message)
        return ClaudeResetClaim(
            False, CLAUDE_RESET_RESULT_TEXT.get(result, CLAUDE_RESET_RESULT_TEXT["unavailable"])
        )
    if status == 429:
        return ClaudeResetClaim(False, "请求太频繁，稍后再试。", settled=False)
    if status in (401, 403):
        return ClaudeResetClaim(False, "Claude 登录已失效，请在 Claude Code 里重新登录。")
    return ClaudeResetClaim(
        False, f"使用失败：{codex_error_text(status, payload)}", settled=False
    )


CLAUDE_SESSION_RESET_PROGRAM = "juniper_tide"


@dataclass
class ClaudeSessionReset:
    """Claude's weekly 5h session reset (juniper_tide).

    It is an experiment: only accounts in its "reset" arm get one, and the
    server only opens it once the 5h window is actually at its limit.
    """

    eligible: bool = False
    ineligible_reason: str | None = None
    in_experiment: bool = False
    arm: str | None = None
    available: bool = False
    next_available_at: str | None = None
    resets_per_week: int = 1

    @property
    def offered(self) -> bool:
        return self.arm == "reset"

    @property
    def claimable(self) -> bool:
        return self.eligible and self.offered and self.available


CLAUDE_SESSION_REASON_TEXT = {
    "tier": "当前订阅方案不包含 5 小时重置",
    "tenure": "账号注册时间还不够",
    "surface": "当前客户端不被允许使用",
    "mobile": "移动端不可用",
    "cli_version": "Claude Code 版本过低",
    "not_at_wall": "到达 5 小时上限后才会开放",
    "weekly_limit": "周额度已用完，5 小时重置无法使用",
    "no_weekly_limit": "账号没有周额度限制，不需要重置",
    "other_experiment": "账号在其他实验中",
    "extra_usage": "已开启超额用量，不需要重置",
}


def parse_claude_session_reset(block: Any) -> ClaudeSessionReset:
    if not isinstance(block, dict):
        return ClaudeSessionReset()
    reason = _first_present(block, "ineligible_reason", "ineligibleReason")
    arm = _first_present(block, "arm")
    return ClaudeSessionReset(
        eligible=bool(_first_present(block, "eligible") or False),
        ineligible_reason=str(reason) if reason else None,
        in_experiment=bool(_first_present(block, "in_experiment", "inExperiment") or False),
        arm=str(arm) if arm else None,
        available=bool(_first_present(block, "available") or False),
        next_available_at=_iso_timestamp(
            _first_present(block, "next_available_at", "nextAvailableAt")
        ),
        resets_per_week=_claude_grant_int(block, "resets_per_week", "resetsPerWeek") or 1,
    )


def fetch_claude_session_reset() -> tuple[ClaudeSessionReset | None, str | None]:
    """Read the 5h session reset. Only the 5h dialog calls this, on demand."""
    status, payload = claude_authorized_json(CLAUDE_USAGE_AT_WALL_URL)
    if status == 0 and payload is None:
        return None, "未找到可用的 Claude 登录凭证"
    if status == 429:
        return None, "请求太频繁，稍后再试。"
    if status != 200 or not isinstance(payload, dict):
        return None, f"读取失败：{codex_error_text(status, payload)}"
    return parse_claude_session_reset(payload.get("juniper_tide")), None


CLAUDE_SESSION_RESULT_TEXT = {
    "already_used": "本周的 5 小时重置已经用过了。",
    "not_limited": "还没到 5 小时上限，暂时不能使用，额度未消耗。",
    "ineligible": "当前账号不符合使用条件。",
    "unavailable": "重置暂不可用，稍后再试。",
}


def claim_claude_session_reset() -> ClaudeResetClaim:
    """Spend this week's 5h session reset. Only call after the user confirms.

    The CLI sends no request id for this program; the weekly quota itself is
    what stops a retried click from spending twice ("already_used").
    """
    org = claude_organization_uuid()
    if not org:
        return ClaudeResetClaim(False, "找不到 Claude 组织信息，请在 Claude Code 里重新登录。")
    status, payload = claude_authorized_json(
        CLAUDE_RESET_CLAIM_URL.format(org=urllib.parse.quote(org, safe="")),
        method="POST",
        body={"program": CLAUDE_SESSION_RESET_PROGRAM},
        timeout=25.0,
    )
    if status == 0 and payload is None:
        return ClaudeResetClaim(False, "未找到可用的 Claude 登录凭证")
    if status == 200 and isinstance(payload, dict):
        result = str(payload.get("result") or "unavailable")
        if result == "reset":
            return ClaudeResetClaim(True, "5 小时会话已重置。")
        return ClaudeResetClaim(
            False,
            CLAUDE_SESSION_RESULT_TEXT.get(result, CLAUDE_SESSION_RESULT_TEXT["unavailable"]),
        )
    if status == 429:
        return ClaudeResetClaim(False, "请求太频繁，稍后再试。", settled=False)
    if status in (401, 403):
        return ClaudeResetClaim(False, "Claude 登录已失效，请在 Claude Code 里重新登录。")
    return ClaudeResetClaim(
        False, f"使用失败：{codex_error_text(status, payload)}", settled=False
    )


def fetch_claude_usage(*, force: bool = False) -> ProviderUsage:
    if claude_in_backoff() and not force:
        return ProviderUsage(
            provider_id="claude",
            display_name="claude",
            available=False,
            error="更新失败",
            stale=True,
        )

    oauth, _cred_path = read_claude_oauth()
    if not oauth:
        return ProviderUsage(
            provider_id="claude",
            display_name="claude",
            available=False,
            error="更新失败",
        )

    plan = oauth.get("subscriptionType")
    status, payload = claude_authorized_json(CLAUDE_USAGE_RESET_URL)

    if status == 429:
        set_claude_backoff()
        return ProviderUsage(
            provider_id="claude",
            display_name="claude",
            available=False,
            error="更新失败",
            stale=True,
        )

    if status != 200 or not isinstance(payload, dict):
        return ProviderUsage(
            provider_id="claude",
            display_name="claude",
            available=False,
            error="更新失败",
        )

    windows: list[UsageWindow] = []
    five = payload.get("five_hour") or {}
    # Rows read "fable 7d → claude 7d → claude 5h"; Settings decides which of
    # them show and in what order.
    #
    # Model-scoped weekly limits (e.g. Fable)
    for entry in payload.get("limits") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("kind") != "weekly_scoped":
            continue
        scope = entry.get("scope") or {}
        model = ((scope.get("model") or {}).get("display_name") or "").strip()
        if not model:
            continue
        # Prefer Fable; still show other scoped models if present
        pct = entry.get("percent")
        if pct is None:
            continue
        try:
            pct_f = float(pct)
        except Exception:
            continue
        model_key = model.lower().replace(" ", "")
        windows.append(
            UsageWindow(
                id=f"weekly_{model_key}",
                label=f"{model_key} 7d",
                used_pct=pct_f,
                resets_at=entry.get("resets_at"),
            )
        )

    week = payload.get("seven_day") or {}
    if isinstance(week, dict) and week.get("utilization") is not None:
        windows.append(
            UsageWindow(
                id="seven_day",
                label="7d",
                used_pct=float(week["utilization"]),
                resets_at=week.get("resets_at"),
            )
        )

    if isinstance(five, dict) and five.get("utilization") is not None:
        windows.append(
            UsageWindow(
                id="five_hour",
                label="5h",
                used_pct=float(five["utilization"]),
                resets_at=five.get("resets_at"),
            )
        )

    reset_block = payload.get("cedar_ember")
    result = ProviderUsage(
        provider_id="claude",
        display_name="claude",
        plan_label=str(plan) if plan else None,
        windows=windows,
        available=True,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        reset_block=reset_block if isinstance(reset_block, dict) else None,
    )
    clear_claude_backoff()
    cache_put_provider(result)
    return result


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------


def read_codex_auth(home: Path = CODEX_HOME) -> tuple[dict[str, Any] | None, Path | None]:
    auth_path = home / "auth.json"
    if not auth_path.exists():
        return None, None
    try:
        data = json.loads(auth_path.read_text(encoding="utf-8"))
        tokens = (data.get("tokens") or {}) if isinstance(data, dict) else {}
        if tokens.get("access_token"):
            return data, auth_path
    except Exception:
        return None, None
    return None, None


def save_codex_auth(auth_path: Path, auth: dict[str, Any], refreshed: dict[str, Any]) -> None:
    try:
        tokens = auth.setdefault("tokens", {})
        tokens["access_token"] = refreshed.get("access_token") or tokens.get("access_token")
        if refreshed.get("refresh_token"):
            tokens["refresh_token"] = refreshed["refresh_token"]
        auth_path.write_text(json.dumps(auth, indent=2), encoding="utf-8")
    except Exception:
        pass


def refresh_codex_token(refresh_token: str) -> dict[str, Any] | None:
    body = (
        f"grant_type=refresh_token"
        f"&refresh_token={urllib.parse.quote(refresh_token)}"
        f"&client_id={urllib.parse.quote(CODEX_CLIENT_ID)}"
    ).encode("utf-8")
    req = urllib.request.Request(
        CODEX_TOKEN_URL,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "usage-float/1.1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def codex_authorized_json(
    url: str,
    *,
    home: Path = CODEX_HOME,
    method: str = "GET",
    body: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    """Call a ChatGPT backend endpoint with the local Codex OAuth token.

    Returns the same (status, payload) pair as http_json(); status 0 means the
    local credential is missing, unusable, or could not be refreshed.
    """
    auth, auth_path = read_codex_auth(home)
    if not auth:
        return 0, None

    tokens = auth.get("tokens") or {}
    access = tokens.get("access_token")
    account_id = tokens.get("account_id")
    if not access:
        return 0, None

    def call(token: str) -> tuple[int, Any]:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if account_id:
            headers["ChatGPT-Account-ID"] = str(account_id)
        return http_json(url, method=method, headers=headers, body=body)

    status, payload = call(access)
    if status in (401, 403):
        rt = tokens.get("refresh_token")
        if not rt or not auth_path:
            return status, payload
        refreshed = refresh_codex_token(rt)
        if not refreshed or not refreshed.get("access_token"):
            return status, payload
        save_codex_auth(auth_path, auth, refreshed)
        status, payload = call(refreshed["access_token"])
    return status, payload


def fetch_codex_usage(account: CodexAccount) -> ProviderUsage:
    status, payload = codex_authorized_json(CODEX_USAGE_URL, home=account.home)
    if status != 200 or not isinstance(payload, dict):
        return ProviderUsage(
            provider_id=account.provider_id,
            display_name=account.display_name,
            available=False,
            error="更新失败",
        )

    rate = payload.get("rate_limit") or {}
    # Prefer the more limiting / primary window for the single-line display.
    # Use primary if present, else secondary; expose remaining reset of that window.
    primary = rate.get("primary_window") or {}
    secondary = rate.get("secondary_window") or {}
    chosen = primary if primary.get("used_percent") is not None else secondary
    if chosen.get("used_percent") is None:
        return ProviderUsage(
            provider_id=account.provider_id,
            display_name=account.display_name,
            available=False,
            error="更新失败",
        )

    resets_at = None
    if chosen.get("reset_at") is not None:
        try:
            resets_at = datetime.fromtimestamp(float(chosen["reset_at"]), tz=timezone.utc).isoformat()
        except Exception:
            resets_at = None

    windows = [
        UsageWindow(
            id="usage",
            label=account.display_name,
            used_pct=float(chosen["used_percent"]),
            resets_at=resets_at,
        )
    ]
    result = ProviderUsage(
        provider_id=account.provider_id,
        display_name=account.display_name,
        plan_label=str(payload.get("plan_type") or "") or None,
        windows=windows,
        available=True,
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )
    cache_put_provider(result)
    return result


@dataclass
class CodexResetCard:
    """One banked rate-limit reset ("重置卡") on the signed-in Codex account."""

    card_id: str
    status: str = CODEX_RESET_CARD_STATUS_AVAILABLE
    reset_type: str | None = None
    granted_at: str | None = None
    expires_at: str | None = None

    @property
    def usable(self) -> bool:
        return self.status == CODEX_RESET_CARD_STATUS_AVAILABLE

    @property
    def status_text(self) -> str:
        return CODEX_RESET_CARD_STATUS_TEXT.get(self.status, self.status)


def _first_present(data: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = data.get(name)
        if value is not None:
            return value
    return None


def _iso_timestamp(value: Any) -> str | None:
    """Normalise a card timestamp to ISO-8601 so parse_iso() can read it.

    The endpoint has shipped both epoch seconds and RFC 3339 strings, so accept
    either rather than dropping a card because of its timestamp encoding.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
        except (OSError, OverflowError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if parse_iso(text) is not None:
        return text
    try:
        return datetime.fromtimestamp(float(text), tz=timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def _codex_reset_card_from_dict(data: dict[str, Any]) -> CodexResetCard | None:
    raw_id = _first_present(data, "id", "credit_id", "creditId")
    card_id = str(raw_id).strip() if raw_id is not None else ""
    if not card_id:
        return None
    raw_status = _first_present(data, "status", "state")
    reset_type = _first_present(data, "reset_type", "resetType")
    return CodexResetCard(
        card_id=card_id,
        status=(
            str(raw_status).strip().lower()
            if raw_status is not None
            else CODEX_RESET_CARD_STATUS_AVAILABLE
        ),
        reset_type=str(reset_type) if reset_type else None,
        granted_at=_iso_timestamp(
            _first_present(data, "granted_at", "grantedAt")
        ),
        expires_at=_iso_timestamp(
            _first_present(data, "expires_at", "expiresAt")
        ),
    )


def _iso_epoch(ts: str | None, default: float) -> float:
    dt = parse_iso(ts)
    if dt is None:
        return default
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def sort_codex_reset_cards(cards: list[CodexResetCard]) -> list[CodexResetCard]:
    """Usable cards first, soonest expiry first — spend what would lapse next."""
    return sorted(
        cards,
        key=lambda card: (
            0 if card.usable else 1,
            _iso_epoch(card.expires_at, float("inf")),
            card.card_id,
        ),
    )


def codex_error_text(status: int, payload: Any) -> str:
    detail: str | None = None
    if isinstance(payload, dict):
        for key in ("detail", "message", "error_description"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                detail = value.strip()
                break
        if detail is None:
            error = payload.get("error")
            if isinstance(error, str) and error.strip():
                detail = error.strip()
            elif isinstance(error, dict):
                value = error.get("message") or error.get("detail")
                if isinstance(value, str) and value.strip():
                    detail = value.strip()
    elif isinstance(payload, str) and payload.strip():
        detail = payload.strip()
    if detail:
        detail = detail[:160]
        return f"HTTP {status} · {detail}" if status else detail
    return f"HTTP {status}" if status else "网络不可用"


def fetch_codex_reset_cards(
    home: Path = CODEX_HOME,
) -> tuple[list[CodexResetCard], str | None]:
    """Return (cards, error). Cards include spent ones so counts stay honest."""
    status, payload = codex_authorized_json(CODEX_RESET_CARDS_URL, home=home)
    if status == 0:
        return [], "未找到可用的 Codex 登录凭证"
    if status != 200 or not isinstance(payload, dict):
        return [], f"读取重置卡失败：{codex_error_text(status, payload)}"
    cards: list[CodexResetCard] = []
    for item in payload.get("credits") or []:
        if not isinstance(item, dict):
            continue
        card = _codex_reset_card_from_dict(item)
        if card is not None:
            cards.append(card)
    return sort_codex_reset_cards(cards), None


def consume_codex_reset_card(
    card_id: str,
    *,
    home: Path = CODEX_HOME,
    request_id: str | None = None,
) -> tuple[bool, str]:
    """Redeem one reset card. Returns (ok, message) for direct UI display."""
    card_id = str(card_id).strip()
    if not card_id:
        return False, "没有选中任何重置卡"
    body = {
        "credit_id": card_id,
        # The backend dedupes on this id, so a lost response cannot burn a
        # second card when the click is retried.
        "redeem_request_id": request_id or uuid.uuid4().hex,
    }
    status, payload = codex_authorized_json(
        CODEX_RESET_CONSUME_URL,
        home=home,
        method="POST",
        body=body,
    )
    if status == 0:
        return False, "未找到可用的 Codex 登录凭证"
    if status in (200, 201, 202, 204):
        return True, "重置卡已使用，用量窗口已重置。"
    return False, f"使用失败：{codex_error_text(status, payload)}"


# ---------------------------------------------------------------------------
# Grok
# ---------------------------------------------------------------------------


def extract_grok_token(auth: Any) -> str | None:
    """Prefer SuperGrok OIDC entry (https://auth.x.ai::…), then legacy session."""
    if auth is None or not isinstance(auth, dict):
        return None
    top = auth.get("access_token")
    if isinstance(top, str) and top:
        return top

    oidc = None
    legacy = None
    for scope, value in auth.items():
        if not isinstance(value, dict):
            continue
        key = value.get("key")
        if not isinstance(key, str) or not key:
            continue
        s = str(scope)
        if s.startswith("https://auth.x.ai::"):
            oidc = key
        elif s == "https://accounts.x.ai/sign-in" or "/sign-in" in s:
            legacy = key
    return oidc or legacy


def read_grok_token() -> str | None:
    env = os.environ.get("GROK_API_KEY") or os.environ.get("GROK_TOKEN")
    if env:
        return env
    path = GROK_HOME / "auth.json"
    if not path.exists():
        return None
    try:
        return extract_grok_token(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return None


def _read_varint(buf: bytes, index: int) -> tuple[int | None, int]:
    value = 0
    shift = 0
    i = index
    while i < len(buf) and shift < 64:
        byte = buf[i]
        i += 1
        value |= (byte & 0x7F) << shift
        if byte & 0x80 == 0:
            return value, i
        shift += 7
    return None, i


def _scan_protobuf(
    buf: bytes,
    depth: int,
    path: list[int],
    order: int,
    fixed32: list[tuple[list[int], float, int]],
    varints: list[tuple[list[int], int]],
) -> int:
    """Heuristic protobuf walker (ported from CC Switch / CodexBar)."""
    import struct as _struct

    i = 0
    next_order = order
    n = len(buf)
    while i < n:
        start = i
        key, i = _read_varint(buf, i)
        if key is None or key == 0:
            i = start + 1
            continue
        field_number = key >> 3
        wire_type = key & 0x07
        field_path = path + [field_number]

        if wire_type == 0:
            val, i = _read_varint(buf, i)
            if val is None:
                i = start + 1
                continue
            varints.append((field_path, val))
        elif wire_type == 1:
            if i + 8 > n:
                return next_order
            i += 8
        elif wire_type == 2:
            length, i = _read_varint(buf, i)
            if length is None or i + length > n:
                i = start + 1
                continue
            end = i + length
            if depth < 4:
                next_order = _scan_protobuf(
                    buf[i:end], depth + 1, field_path, next_order, fixed32, varints
                )
            i = end
        elif wire_type == 5:
            if i + 4 > n:
                return next_order
            bits = _struct.unpack_from("<I", buf, i)[0]
            value = _struct.unpack("<f", _struct.pack("<I", bits))[0]
            fixed32.append((field_path, value, next_order))
            next_order += 1
            i += 4
        else:
            i = start + 1
    return next_order


def _grpc_web_data_frames(data: bytes) -> list[bytes]:
    frames: list[bytes] = []
    i = 0
    while i < len(data):
        if i + 5 > len(data):
            return []
        flags = data[i]
        length = int.from_bytes(data[i + 1 : i + 5], "big")
        start = i + 5
        end = start + length
        if end > len(data):
            return []
        if flags & 0x80 == 0:
            frames.append(data[start:end])
        i = end
    return frames


def _parse_grok_billing_payload(data: bytes, now_secs: int) -> tuple[float, str | None]:
    """Return (used_percent, resets_at_iso). Raises ValueError on failure."""
    payloads = _grpc_web_data_frames(data)
    if not payloads and data:
        # bare protobuf fallback
        first = data[0]
        if (first >> 3) > 0 and (first & 0x07) in (0, 1, 2, 5):
            payloads = [data]
    if not payloads:
        raise ValueError("empty billing payload")

    fixed32: list[tuple[list[int], float, int]] = []
    varints: list[tuple[list[int], int]] = []
    for payload in payloads:
        _scan_protobuf(payload, 0, [], 0, fixed32, varints)

    percent_cands = [
        (path, value, order)
        for path, value, order in fixed32
        if path
        and path[-1] == 1
        and value == value  # not NaN
        and 0.0 <= value <= 100.0
    ]
    percent_cands.sort(key=lambda x: (len(x[0]), x[2]))
    parsed_percent = percent_cands[0][1] if percent_cands else None

    reset_cands = [
        (path, value)
        for path, value in varints
        if 1_700_000_000 <= value <= 2_100_000_000 and value > now_secs
    ]
    reset_ts = None
    preferred = [v for path, v in reset_cands if path == [1, 5, 1]]
    if preferred:
        reset_ts = min(preferred)
    elif reset_cands:
        reset_ts = min(v for _, v in reset_cands)

    has_usage_period = any(
        path[:2] == [1, 6] or (path == [1, 8, 1] and value in (1, 2))
        for path, value in varints
    )
    no_usage_yet = (
        parsed_percent is None
        and not fixed32
        and reset_ts is not None
        and has_usage_period
    )
    if parsed_percent is None and no_usage_yet:
        parsed_percent = 0.0
    if parsed_percent is None:
        raise ValueError("could not locate usage percent")

    resets_at = None
    if reset_ts is not None:
        resets_at = datetime.fromtimestamp(reset_ts, tz=timezone.utc).isoformat()
    return float(parsed_percent), resets_at


def fetch_grok_usage() -> ProviderUsage:
    """SuperGrok credit window via grok.com gRPC-web (same as CC Switch)."""
    token = read_grok_token()
    if not token:
        return ProviderUsage(
            provider_id="grok",
            display_name="grok",
            available=False,
            error="更新失败",
        )

    # Empty gRPC-web frame: flags(1) + big-endian length(4) = 5 zero bytes
    body = bytes(5)
    req = urllib.request.Request(
        GROK_BILLING_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Origin": "https://grok.com",
            "Referer": "https://grok.com/?_s=usage",
            "Accept": "*/*",
            "Content-Type": "application/grpc-web+proto",
            "x-grpc-web": "1",
            "x-user-agent": "connect-es/2.1.1",
            "User-Agent": "usage-float",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            status = resp.status
            raw = resp.read()
            grpc_status = resp.headers.get("grpc-status")
            if grpc_status and grpc_status not in ("0", "00"):
                return ProviderUsage(
                    provider_id="grok",
                    display_name="grok",
                    available=False,
                    error="更新失败",
                )
            if status != 200:
                return ProviderUsage(
                    provider_id="grok",
                    display_name="grok",
                    available=False,
                    error="更新失败",
                )
            pct, resets_at = _parse_grok_billing_payload(raw, int(time.time()))
    except Exception:
        return ProviderUsage(
            provider_id="grok",
            display_name="grok",
            available=False,
            error="更新失败",
        )

    result = ProviderUsage(
        provider_id="grok",
        display_name="grok",
        windows=[
            UsageWindow(
                id="credits",
                label="额度",
                used_pct=max(0.0, min(100.0, pct)),
                resets_at=resets_at,
            )
        ],
        available=True,
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )
    cache_put_provider(result)
    return result


# ---------------------------------------------------------------------------
# LiteLLM / OpenAI-compatible proxy
# ---------------------------------------------------------------------------


def _llm_proxy_openai_base(endpoint: str) -> str:
    base = (endpoint or LLM_PROXY_DEFAULT_ENDPOINT).strip().rstrip("/")
    if not base:
        return ""
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    return base


def _read_liwork_llm_proxy() -> tuple[str, str]:
    try:
        if not LIWORK_ORCA_PATH.exists():
            return "", ""
        data = json.loads(LIWORK_ORCA_PATH.read_text(encoding="utf-8"))
        settings = data.get("settings") if isinstance(data, dict) else None
        if not isinstance(settings, dict):
            return "", ""
        key = str(settings.get("llmProxyApiKey") or "").strip()
        endpoint = str(settings.get("llmProxyEndpoint") or "").strip()
        return key, endpoint
    except Exception:
        return "", ""


def read_llm_proxy_credentials() -> tuple[str, str] | tuple[None, None]:
    """Return (api_key, openai_base) from env or usage-float config."""
    key = str(os.environ.get("LLM_PROXY_API_KEY") or "").strip()
    endpoint = str(os.environ.get("LLM_PROXY_ENDPOINT") or "").strip()
    if not key or not endpoint:
        try:
            if CONFIG_PATH.exists():
                cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                if isinstance(cfg, dict):
                    key = key or str(cfg.get("llm_proxy_api_key") or "").strip()
                    endpoint = endpoint or str(cfg.get("llm_proxy_endpoint") or "").strip()
        except Exception:
            pass
    if not key:
        liwork_key, liwork_endpoint = _read_liwork_llm_proxy()
        key = key or liwork_key
        endpoint = endpoint or liwork_endpoint
    if not key:
        return None, None
    base = _llm_proxy_openai_base(endpoint)
    if not base:
        return None, None
    return key, base


def _header_float(headers: dict[str, str], name: str) -> float | None:
    raw = headers.get(name)
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _spend_looks_like_this_request(spend: float | None, cost: float | None) -> bool:
    """True when LiteLLM has not flushed cumulative key spend yet."""
    if spend is None:
        return False
    if cost is None:
        return spend <= 1e-5
    return spend <= max(cost * 2.0, 1e-5)


def parse_llm_proxy_spend_headers(
    headers: dict[str, str],
) -> tuple[float | None, float | None, float | None]:
    lowered = {str(k).lower(): str(v) for k, v in headers.items()}
    spend = _header_float(lowered, "x-litellm-key-spend")
    budget = _header_float(lowered, "x-litellm-key-max-budget")
    cost = _header_float(lowered, "x-litellm-response-cost")
    return spend, budget, cost


def _parse_utc_offset(text: str) -> timedelta | None:
    raw = text.strip().upper().replace(" ", "")
    if raw in {"UTC", "GMT", "Z"}:
        return timedelta(0)
    if raw.startswith("UTC"):
        raw = raw[3:]
    if not raw or raw[0] not in "+-":
        return None
    sign = 1 if raw[0] == "+" else -1
    rest = raw[1:]
    hours = 0
    minutes = 0
    if ":" in rest:
        hour_text, minute_text = rest.split(":", 1)
        if not hour_text.isdigit() or not minute_text.isdigit():
            return None
        hours, minutes = int(hour_text), int(minute_text)
    elif rest.isdigit():
        if len(rest) <= 2:
            hours = int(rest)
        elif len(rest) == 4:
            hours, minutes = int(rest[:2]), int(rest[2:])
        else:
            return None
    else:
        return None
    if hours > 14 or minutes > 59:
        return None
    return timedelta(hours=sign * hours, minutes=sign * minutes)


def _normalize_llm_proxy_timezone(value: Any) -> str:
    text = str(value or "").strip() or LLM_PROXY_TIMEZONE_DEFAULT
    aliases = {
        "cst": "UTC+8",
        "prc": "UTC+8",
        "asia/shanghai": "UTC+8",
        "asia/hong_kong": "UTC+8",
        "asia/taipei": "UTC+8",
        "asia/singapore": "UTC+8",
        "asia/tokyo": "UTC+9",
        "asia/seoul": "UTC+9",
        "us/eastern": "UTC-5",
        "us/pacific": "UTC-8",
        "europe/london": "UTC",
    }
    mapped = aliases.get(text.lower())
    if mapped:
        return mapped
    offset = _parse_utc_offset(text)
    if offset is not None:
        total_minutes = int(offset.total_seconds() // 60)
        if total_minutes == 0:
            return "UTC"
        sign = "+" if total_minutes > 0 else "-"
        hours, minutes = divmod(abs(total_minutes), 60)
        if minutes:
            return f"UTC{sign}{hours:02d}:{minutes:02d}"
        return f"UTC{sign}{hours}"
    return text


def _llm_proxy_zoneinfo(name: str | None) -> timezone | ZoneInfo:
    text = _normalize_llm_proxy_timezone(name)
    offset = _parse_utc_offset(text)
    if offset is not None:
        return timezone(offset)
    try:
        return ZoneInfo(text)
    except Exception:
        return timezone(timedelta(hours=8))


def _normalize_llm_proxy_budget_duration(raw: str | None) -> str:
    text = (raw or "").strip().lower()
    aliases = {
        "hourly": "1h",
        "daily": "1d",
        "weekly": "7d",
        "monthly": "30d",
        "24h": "1d",
        "1w": "7d",
        "1mo": "30d",
    }
    return aliases.get(text, text)


def _parse_llm_proxy_duration(raw: str | None) -> tuple[int, str] | None:
    text = _normalize_llm_proxy_budget_duration(raw)
    if not text:
        return None
    index = 0
    while index < len(text) and text[index].isdigit():
        index += 1
    if index == 0 or index == len(text):
        return None
    try:
        value = int(text[:index])
    except ValueError:
        return None
    unit = text[index:]
    if unit not in {"s", "m", "h", "d", "w", "mo"}:
        return None
    return value, unit


def next_llm_proxy_budget_reset(
    duration: str | None,
    *,
    now: datetime | None = None,
    timezone_name: str | None = None,
) -> datetime | None:
    """Next LiteLLM-style budget reset (1d midnight, 7d Monday, 30d 1st of month)."""
    parsed = _parse_llm_proxy_duration(duration)
    if parsed is None:
        return None
    value, unit = parsed
    tzinfo = _llm_proxy_zoneinfo(timezone_name)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(tzinfo)
    midnight = current.replace(hour=0, minute=0, second=0, microsecond=0)

    def at_midnight(moment: datetime) -> datetime:
        return moment.replace(hour=0, minute=0, second=0, microsecond=0)

    if unit == "d" and value == 1:
        candidate = midnight
        return candidate + timedelta(days=1) if candidate <= current else candidate
    if (unit == "d" and value == 7) or (unit == "w" and value == 1):
        days_until_monday = (7 - current.weekday()) % 7
        candidate = midnight + timedelta(days=days_until_monday)
        return candidate + timedelta(days=7) if candidate <= current else candidate
    if (unit == "d" and value == 30) or (unit == "mo" and value == 1):
        candidate = midnight.replace(day=1)
        if candidate <= current:
            if candidate.month == 12:
                candidate = candidate.replace(year=candidate.year + 1, month=1)
            else:
                candidate = candidate.replace(month=candidate.month + 1)
        return candidate
    if unit == "h":
        return current.replace(minute=0, second=0, microsecond=0) + timedelta(hours=value)
    if unit == "m":
        return current.replace(second=0, microsecond=0) + timedelta(minutes=value)
    if unit == "s":
        return current.replace(microsecond=0) + timedelta(seconds=value)
    if unit == "d":
        return at_midnight(midnight + timedelta(days=value))
    if unit == "w":
        return at_midnight(midnight + timedelta(weeks=value))
    return None


def _parse_llm_proxy_reset_timestamp(raw: str | None) -> str | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        stamp = float(text)
        if stamp > 1e12:
            stamp /= 1000.0
        if stamp > 1e9:
            return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat()
    except ValueError:
        pass
    parsed = parse_iso(text)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def _llm_proxy_budget_settings() -> tuple[str, str]:
    duration = str(os.environ.get("LLM_PROXY_BUDGET_DURATION") or "").strip()
    timezone_name = str(os.environ.get("LLM_PROXY_TIMEZONE") or "").strip()
    try:
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                duration = duration or str(data.get("llm_proxy_budget_duration") or "").strip()
                timezone_name = timezone_name or str(
                    data.get("llm_proxy_timezone") or ""
                ).strip()
    except Exception:
        pass
    return (
        duration or LLM_PROXY_BUDGET_DURATION_DEFAULT,
        _normalize_llm_proxy_timezone(timezone_name or LLM_PROXY_TIMEZONE_DEFAULT),
    )


def resolve_llm_proxy_resets_at(
    headers: dict[str, str] | None = None,
    *,
    now: datetime | None = None,
    duration: str | None = None,
    timezone_name: str | None = None,
) -> str | None:
    lowered = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    for key in LLM_PROXY_RESET_HEADERS:
        parsed = _parse_llm_proxy_reset_timestamp(lowered.get(key))
        if parsed:
            return parsed
    header_duration = None
    for key in LLM_PROXY_DURATION_HEADERS:
        raw = lowered.get(key)
        if raw:
            header_duration = raw
            break
    reset = next_llm_proxy_budget_reset(
        header_duration or duration or LLM_PROXY_BUDGET_DURATION_DEFAULT,
        now=now,
        timezone_name=timezone_name,
    )
    return reset.isoformat() if reset is not None else None


def _round_dollars(value: float) -> int:
    """Half-up dollar rounding (2.5 → 3), not banker's rounding or truncation."""
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def format_llm_proxy_spend(spend: float, budget: float | None = None) -> str:
    """Compact dollar readout, e.g. $2/200."""
    used = _round_dollars(spend)
    if budget is None or budget <= 0:
        return f"${used}"
    return f"${used}/{_round_dollars(budget)}"


def _llm_proxy_probe(
    base: str, key: str, *, model: str, kind: str
) -> tuple[int, dict[str, str]]:
    headers = {"Authorization": f"Bearer {key}"}
    if kind == "embeddings":
        status, _payload, resp_headers = http_json_response(
            f"{base}/embeddings",
            method="POST",
            headers=headers,
            body={"model": model, "input": "usage"},
        )
        return status, resp_headers
    status, _payload, resp_headers = http_json_response(
        f"{base}/chat/completions",
        method="POST",
        headers=headers,
        body={
            "model": model,
            "messages": [{"role": "user", "content": "."}],
            "max_tokens": 1,
        },
    )
    return status, resp_headers


def fetch_llmproxy_usage() -> ProviderUsage:
    creds = read_llm_proxy_credentials()
    if creds[0] is None:
        return ProviderUsage(
            provider_id="llmproxy",
            display_name="llmproxy",
            available=False,
            error="更新失败",
        )
    key, base = creds
    probes = (
        ("embeddings", LLM_PROXY_EMBED_MODEL),
        ("chat", LLM_PROXY_CHAT_FALLBACK_MODEL),
    )
    spend: float | None = None
    budget: float | None = None
    headers: dict[str, str] = {}
    for kind, model in probes:
        status, headers = _llm_proxy_probe(base, key, model=model, kind=kind)
        if status != 200:
            continue
        spend, budget, cost = parse_llm_proxy_spend_headers(headers)
        if _spend_looks_like_this_request(spend, cost):
            status, headers = _llm_proxy_probe(base, key, model=model, kind=kind)
            if status == 200:
                spend, budget, cost = parse_llm_proxy_spend_headers(headers)
        if spend is not None:
            break
    if spend is None:
        return ProviderUsage(
            provider_id="llmproxy",
            display_name="llmproxy",
            available=False,
            error="更新失败",
        )

    duration, timezone_name = _llm_proxy_budget_settings()
    resets_at = resolve_llm_proxy_resets_at(
        headers,
        duration=duration,
        timezone_name=timezone_name,
    )

    if budget is None or budget <= 0:
        result = ProviderUsage(
            provider_id="llmproxy",
            display_name="llmproxy",
            summary=format_llm_proxy_spend(spend),
            windows=[
                UsageWindow(
                    id="spend",
                    label="额度",
                    used_pct=0.0,
                    resets_at=resets_at,
                )
            ]
            if resets_at
            else [],
            available=True,
            fetched_at=datetime.now(timezone.utc).isoformat(),
        )
        cache_put_provider(result)
        return result

    used_pct = max(0.0, min(100.0, (spend / budget) * 100.0))
    result = ProviderUsage(
        provider_id="llmproxy",
        display_name="llmproxy",
        plan_label=f"${budget:.0f}",
        summary=format_llm_proxy_spend(spend, budget),
        windows=[
            UsageWindow(
                id="spend",
                label="额度",
                used_pct=used_pct,
                resets_at=resets_at,
            )
        ],
        available=True,
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )
    cache_put_provider(result)
    return result


# ---------------------------------------------------------------------------
# Fetch all
# ---------------------------------------------------------------------------


def provider_available(provider_id: str) -> bool:
    """True when this machine actually holds a credential for the provider.

    A provider that is not signed in is hidden outright rather than drawn as a
    failed row, so an unused slot never occupies space in the strip.
    """
    provider_id = provider_fetch_id(provider_id)
    account = CODEX_ACCOUNT_BY_ID.get(provider_id)
    if account is not None:
        return read_codex_auth(account.home)[0] is not None
    if provider_id == "claude":
        return read_claude_oauth()[0] is not None
    if provider_id == "grok":
        return read_grok_token() is not None
    if provider_id == "llmproxy":
        return read_llm_proxy_credentials()[0] is not None
    return False


def active_providers(configured: list[str] | None = None) -> list[str]:
    """Configured providers narrowed to the ones that are actually signed in."""
    selected = configured if configured is not None else list(DEFAULT_PROVIDERS)
    return [pid for pid in selected if provider_available(pid)]


def fetch_all(providers: list[str], *, force: bool = False) -> list[ProviderUsage]:
    """Fetch each provider once, however many display items it backs."""
    providers = list(dict.fromkeys(provider_fetch_id(item) for item in providers))
    out: list[ProviderUsage] = []
    for p in providers:
        try:
            if p == "claude":
                out.append(fetch_claude_usage(force=force))
            elif p in CODEX_ACCOUNT_BY_ID:
                out.append(fetch_codex_usage(CODEX_ACCOUNT_BY_ID[p]))
            elif p == "grok":
                out.append(fetch_grok_usage())
            elif p == "llmproxy":
                out.append(fetch_llmproxy_usage())
        except Exception:
            out.append(
                ProviderUsage(
                    provider_id=p,
                    display_name=p,
                    available=False,
                    error="更新失败",
                )
            )
    order = {pid: i for i, pid in enumerate(providers)}
    out.sort(key=lambda x: order.get(x.provider_id, 99))
    return out


# ---------------------------------------------------------------------------
# Display rows (fixed-width strip: multi-window models expand to N lines)
# ---------------------------------------------------------------------------


@dataclass
class DisplayRow:
    """One visual row: left title, right usage."""

    title: str
    used_pct: float | None = None
    resets_at: str | None = None
    summary: str | None = None  # free text on the right (e.g. failure)
    failed: bool = False
    provider_id: str = ""
    window_id: str = ""


def row_opens_reset_ui(row: DisplayRow) -> bool:
    """Whether clicking this row's number should open a reset dialog.

    Every Codex account has cards to list. Claude's 7d row opens the
    usage-limit grants (they refill the weekly window) and its 5h row opens
    the weekly 5h session reset.
    """
    if row.provider_id in CODEX_ACCOUNT_BY_ID:
        return True
    return row.provider_id == "claude" and row.window_id in ("five_hour", "seven_day")


def reset_target_id(row: DisplayRow) -> str:
    """What a click on this row's number opens: a Codex account or a Claude row."""
    if row.provider_id == "claude":
        return display_item(row) or row.provider_id
    return row.provider_id


def display_item(row: DisplayRow) -> str | None:
    """The Settings entry that shows or hides this row.

    None for a Claude row that is not tied to one window (a failure line).
    """
    if row.provider_id != "claude":
        return row.provider_id
    if row.window_id.startswith("weekly_"):
        return "claude:fable"
    return CLAUDE_WINDOW_ITEMS.get(row.window_id)


def iter_display_rows(
    providers: list[ProviderUsage],
    items: list[str] | None = None,
) -> list[DisplayRow]:
    """Expand providers into fixed rows.

    Claude (and any multi-window model) becomes one row per time window so the
    strip width stays stable instead of growing when 5h+7d are concatenated.
    With ``items`` (the enabled Settings entries, in order) rows are filtered
    and sorted by them; a Claude failure line sits where its first row would.
    """
    rows = _expand_display_rows(providers)
    if items is None:
        return rows
    rank = {item: index for index, item in enumerate(_unique_provider_ids(items))}
    claude_ranks = [rank[item] for item in CLAUDE_DISPLAY_ITEMS if item in rank]
    ranked: list[tuple[int, DisplayRow]] = []
    for row in rows:
        item = display_item(row)
        if item is None:
            if not claude_ranks:
                continue
            position = min(claude_ranks)
        elif item in rank:
            position = rank[item]
        else:
            continue
        ranked.append((position, row))
    ranked.sort(key=lambda pair: pair[0])
    return [row for _position, row in ranked]


def _expand_display_rows(providers: list[ProviderUsage]) -> list[DisplayRow]:
    rows: list[DisplayRow] = []
    for p in providers:
        name = (p.display_name or p.provider_id).lower()

        if not p.available or (p.error == "更新失败" and not p.windows and not p.summary):
            rows.append(
                DisplayRow(
                    title=name,
                    failed=True,
                    summary="更新失败",
                    provider_id=p.provider_id,
                )
            )
            continue

        if p.summary and not p.windows:
            rows.append(
                DisplayRow(
                    title=name,
                    summary=p.summary.replace(name, "").strip() or p.summary,
                    provider_id=p.provider_id,
                )
            )
            continue

        if p.windows:
            multi = len(p.windows) > 1
            # Allow 5h + 7d + scoped model weeks (e.g. fable 7d)
            for w in p.windows[:5]:
                # "claude 5h" / "claude 7d" / "fable 7d" (scoped rows omit provider prefix)
                if w.id.startswith("weekly_"):
                    title = w.label  # e.g. "fable 7d" — no "claude"
                elif multi:
                    title = f"{name} {w.label}"
                else:
                    title = name
                rows.append(
                    DisplayRow(
                        title=title,
                        used_pct=w.used_pct,
                        resets_at=w.resets_at,
                        summary=p.summary if not multi else None,
                        provider_id=p.provider_id,
                        window_id=w.id,
                    )
                )
            continue

        rows.append(
            DisplayRow(
                title=name,
                failed=True,
                summary="更新失败",
                provider_id=p.provider_id,
            )
        )
    return rows


def _enable_dpi_awareness() -> None:
    """Mark process DPI-aware so Windows doesn't bitmap-stretch a tiny window.

    Do NOT force Tk's ``tk scaling`` to 1.0 afterwards — at 96 DPI Tk's natural
    scale is ~1.33 (96/72). Forcing 1.0 makes point-sized fonts look ~25% too small.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        # Per-monitor v2 handles mixed-scale displays and WM_DPICHANGED best.
        # The negative pseudo-handle is DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2.
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        import ctypes

        # 2 = PROCESS_PER_MONITOR_DPI_AWARE (Win 8.1+)
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return
    except Exception:
        pass
    try:
        import ctypes

        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Autostart
# ---------------------------------------------------------------------------

AUTOSTART_VALUE = "UsageFloat"


def _script_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}"'
    script = Path(__file__).resolve()
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = str(pyw if pyw.exists() else Path(sys.executable))
    return f'"{exe}" "{script}"'


def is_autostart_enabled() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_READ,
        )
        try:
            winreg.QueryValueEx(key, AUTOSTART_VALUE)
            return True
        except FileNotFoundError:
            return False
        finally:
            winreg.CloseKey(key)
    except Exception:
        return False


def set_autostart(enabled: bool) -> tuple[bool, str]:
    if sys.platform != "win32":
        return False, "仅支持 Windows 自启动"
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE,
        )
        try:
            if enabled:
                winreg.SetValueEx(key, AUTOSTART_VALUE, 0, winreg.REG_SZ, _script_command())
                return True, "已开启开机自启"
            try:
                winreg.DeleteValue(key, AUTOSTART_VALUE)
            except FileNotFoundError:
                pass
            return True, "已关闭开机自启"
        finally:
            winreg.CloseKey(key)
    except Exception as e:
        return False, f"自启动设置失败: {e}"


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------


def _log_exception(prefix: str = "error") -> None:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with (CONFIG_PATH.parent / "error.log").open("a", encoding="utf-8") as f:
            f.write(f"\n---- {datetime.now().isoformat()} {prefix} ----\n")
            traceback.print_exc(file=f)
    except Exception:
        pass


def _report_tk_callback_exception(exc: Any, val: Any, tb: Any) -> None:
    """Log Tk callback failures without aborting on a recursive stderr printer."""
    try:
        sys.last_type = exc
        sys.last_value = val
        sys.last_traceback = tb
    except Exception:
        pass
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with (CONFIG_PATH.parent / "error.log").open("a", encoding="utf-8") as f:
            f.write(f"\n---- {datetime.now().isoformat()} tk callback ----\n")
            traceback.print_exception(exc, val, tb, limit=50, file=f)
    except Exception:
        pass


@dataclass(frozen=True)
class MonitorInfo:
    """Physical-pixel monitor geometry for a per-monitor-DPI-aware process."""

    device: str
    name: str
    x: int
    y: int
    width: int
    height: int
    work_x: int
    work_y: int
    work_width: int
    work_height: int
    monitor_id: str = ""
    dpi_x: int = 96
    dpi_y: int = 96
    primary: bool = False

    @property
    def bounds(self) -> tuple[int, int, int, int]:
        return self.x, self.y, self.width, self.height

    @property
    def work_bounds(self) -> tuple[int, int, int, int]:
        return self.work_x, self.work_y, self.work_width, self.work_height

    @property
    def scale_percent(self) -> int:
        return max(100, int(round(self.dpi_y / 96 * 100)))


@dataclass(frozen=True)
class PanelMetrics:
    columns: int
    rows: int
    outer_padding: int
    cell_gap: int
    title_points: int
    value_points: int
    meta_points: int
    footer_points: int
    bar_height: int


def _display_number(device: str) -> int:
    suffix = str(device).upper().rsplit("DISPLAY", 1)[-1]
    try:
        return int(suffix)
    except (TypeError, ValueError):
        return 9999


def _monitor_label(monitor: MonitorInfo) -> str:
    number = _display_number(monitor.device)
    display = f"显示器 {number}" if number != 9999 else monitor.device
    primary = "（主屏）" if monitor.primary else ""
    return (
        f"{display} · {monitor.width}×{monitor.height} · "
        f"{monitor.scale_percent}%{primary}"
    )


def _monitor_hardware_key(monitor_id: str | None) -> str:
    """Stable model key; ignores the Windows instance suffix after rearranging."""
    parts = str(monitor_id or "").casefold().split("\\")
    return "\\".join(parts[:2]) if len(parts) >= 2 else str(monitor_id or "").casefold()


def _choose_panel_monitor(
    monitors: list[MonitorInfo],
    preferred_device: str | None,
    preferred_id: str | None = None,
) -> MonitorInfo | None:
    """Resolve the persisted panel, or pick the smallest secondary on first use.

    A missing persisted device deliberately returns ``None``. That makes a
    disconnected sensor panel fall back to the floating HUD instead of covering
    the main desktop full-screen. With no persisted choice, the smallest
    secondary is the safest AIDA64-style panel candidate.
    """
    if not monitors:
        return None
    if preferred_id:
        wanted_id = preferred_id.casefold()
        exact = next(
            (m for m in monitors if m.monitor_id and m.monitor_id.casefold() == wanted_id),
            None,
        )
        if exact is not None:
            return exact
        wanted_hardware = _monitor_hardware_key(preferred_id)
        hardware_match = next(
            (
                m
                for m in monitors
                if m.monitor_id and _monitor_hardware_key(m.monitor_id) == wanted_hardware
            ),
            None,
        )
        if hardware_match is not None:
            return hardware_match
    if preferred_device:
        wanted = preferred_device.casefold()
        return next((m for m in monitors if m.device.casefold() == wanted), None)
    if preferred_id:
        return None
    secondary = [m for m in monitors if not m.primary]
    return min(secondary or monitors, key=lambda m: (m.width * m.height, _display_number(m.device)))


def _monitor_at_point(
    monitors: list[MonitorInfo],
    x: int,
    y: int,
) -> MonitorInfo | None:
    return next(
        (
            m
            for m in monitors
            if m.x <= x < m.x + m.width and m.y <= y < m.y + m.height
        ),
        None,
    )


def _tk_position(x: int, y: int) -> str:
    x_part = f"+{x}" if x >= 0 else str(x)
    y_part = f"+{y}" if y >= 0 else str(y)
    return f"{x_part}{y_part}"


def _tk_geometry(width: int, height: int, x: int, y: int) -> str:
    """Build valid Tk geometry even when a display sits left/above the primary."""
    return f"{width}x{height}{_tk_position(x, y)}"


# Height, at 96 DPI, that one row of the tuned panel type needs incl. gaps.
PANEL_TUNED_ROW_PX = 200


def _panel_grid_shape(row_count: int, width: int, height: int) -> tuple[int, int]:
    count = max(1, int(row_count))
    columns = 2 if count >= 4 and width >= int(height * 1.15) else 1
    rows = (count + columns - 1) // columns
    return columns, rows


def _panel_metrics(
    width: int,
    height: int,
    row_count: int,
    dpi: int = 96,
) -> PanelMetrics:
    columns, rows = _panel_grid_shape(row_count, width, height)
    dpi = max(72, int(dpi or 96))
    cell_height = max(72, int((height - max(48, height * 0.10)) / rows))
    # The type sizes below were tuned for three rows on a 640 px-tall panel.
    # Beyond the rows that height holds, shrink them together so extra rows
    # (e.g. Claude split into three) stay on screen instead of being clipped.
    fit_rows = max(1, int(height / (PANEL_TUNED_ROW_PX * dpi / 96)))
    scale = min(1.0, fit_rows / rows)

    def points(px: float, low: int, high: int) -> int:
        return max(low, min(high, int(round(px * 72 / dpi))))

    return PanelMetrics(
        columns=columns,
        rows=rows,
        outer_padding=max(16, min(36, int(round(min(width, height) * 0.035)))),
        cell_gap=max(10, min(24, int(round(min(width, height) * 0.022)))),
        # Fixed, user-tuned hierarchy for the dedicated low-resolution panel.
        title_points=max(12, int(round(28 * scale))),
        value_points=max(18, int(round(48 * scale))),
        meta_points=max(10, int(round(24 * scale))),
        footer_points=20,
        bar_height=max(6, min(10, int(round(cell_height * 0.045)))),
    )


def _panel_value_slot_width(font: Any, value_text: str) -> int:
    """Keep 100% stable, but don't clip dollar readouts like $2/200."""
    sample = value_text or "100%"
    return max(int(font.measure("100%")), int(font.measure(sample)))


def _enumerate_monitors() -> list[MonitorInfo]:
    if sys.platform != "win32":
        return [
            MonitorInfo(
                device="display-1",
                name="Display",
                x=0,
                y=0,
                width=1280,
                height=720,
                work_x=0,
                work_y=0,
                work_width=1280,
                work_height=720,
                primary=True,
            )
        ]
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        shcore = ctypes.windll.shcore
        monitors: list[MonitorInfo] = []

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", wintypes.LONG),
                ("top", wintypes.LONG),
                ("right", wintypes.LONG),
                ("bottom", wintypes.LONG),
            ]

        class MONITORINFOEXW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", RECT),
                ("rcWork", RECT),
                ("dwFlags", wintypes.DWORD),
                ("szDevice", wintypes.WCHAR * 32),
            ]

        class DISPLAY_DEVICEW(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("DeviceName", wintypes.WCHAR * 32),
                ("DeviceString", wintypes.WCHAR * 128),
                ("StateFlags", wintypes.DWORD),
                ("DeviceID", wintypes.WCHAR * 128),
                ("DeviceKey", wintypes.WCHAR * 128),
            ]

        monitor_proc = ctypes.WINFUNCTYPE(
            wintypes.BOOL,
            wintypes.HMONITOR,
            wintypes.HDC,
            ctypes.POINTER(RECT),
            wintypes.LPARAM,
        )

        def callback(hmon, _hdc, _rect, _lparam):
            info = MONITORINFOEXW()
            info.cbSize = ctypes.sizeof(info)
            if not user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
                return True

            display = DISPLAY_DEVICEW()
            display.cb = ctypes.sizeof(display)
            friendly_name = ""
            try:
                if user32.EnumDisplayDevicesW(info.szDevice, 0, ctypes.byref(display), 0):
                    friendly_name = str(display.DeviceString or "")
            except Exception:
                pass

            dpi_x = ctypes.c_uint(96)
            dpi_y = ctypes.c_uint(96)
            try:
                shcore.GetDpiForMonitor(
                    hmon,
                    0,
                    ctypes.byref(dpi_x),
                    ctypes.byref(dpi_y),
                )
            except Exception:
                pass

            bounds = info.rcMonitor
            work = info.rcWork
            monitors.append(
                MonitorInfo(
                    device=str(info.szDevice),
                    name=friendly_name or str(info.szDevice),
                    x=int(bounds.left),
                    y=int(bounds.top),
                    width=int(bounds.right - bounds.left),
                    height=int(bounds.bottom - bounds.top),
                    work_x=int(work.left),
                    work_y=int(work.top),
                    work_width=int(work.right - work.left),
                    work_height=int(work.bottom - work.top),
                    monitor_id=str(display.DeviceID or ""),
                    dpi_x=int(dpi_x.value or 96),
                    dpi_y=int(dpi_y.value or 96),
                    primary=bool(info.dwFlags & 1),
                )
            )
            return True

        callback_ref = monitor_proc(callback)
        user32.EnumDisplayMonitors(None, None, callback_ref, 0)
        if monitors:
            return sorted(monitors, key=lambda m: (_display_number(m.device), m.x, m.y))
    except Exception:
        pass
    return [
        MonitorInfo(
            device="display-1",
            name="Display",
            x=0,
            y=0,
            width=1280,
            height=720,
            work_x=0,
            work_y=0,
            work_width=1280,
            work_height=720,
            primary=True,
        )
    ]


def _screen_bounds() -> list[tuple[int, int, int, int]]:
    return [monitor.work_bounds for monitor in _enumerate_monitors()]


def _clamp_position(x: int, y: int, w: int = 220, h: int = 100) -> tuple[int, int]:
    for mx, my, mw, mh in _screen_bounds():
        if mx <= x < mx + mw and my <= y < my + mh:
            return min(max(x, mx), mx + mw - w), min(max(y, my), my + mh - h)
    mx, my, mw, mh = _screen_bounds()[0]
    return mx + max(20, mw - w - 40), my + 48


def _hide_from_taskbar(root: Any) -> None:
    try:
        import ctypes

        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
        GWL_EXSTYLE = -20
        WS_EX_TOOLWINDOW = 0x00000080
        WS_EX_APPWINDOW = 0x00040000
        style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        style = (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0027)
    except Exception:
        pass


def run_ui() -> None:
    import tkinter as tk
    from tkinter import filedialog
    from tkinter import font as tkfont
    from tkinter import ttk

    _enable_dpi_awareness()

    cfg = load_config()
    font_size = int(cfg.get("font_size") or FONT_SIZE_DEFAULT)
    card_width = int(cfg.get("card_width") or CARD_WIDTH)
    card_height = cfg.get("card_height")  # None or int
    if card_height is not None:
        try:
            card_height = max(CARD_HEIGHT_MIN, min(CARD_HEIGHT_MAX, int(card_height)))
        except Exception:
            card_height = None
    allow_resize = bool(cfg.get("allow_resize", False))
    card_height = _effective_card_height(card_height, allow_resize)
    bg_opacity = _clamp_opacity(cfg.get("bg_opacity"), BG_OPACITY_DEFAULT)
    text_opacity = _clamp_opacity(cfg.get("text_opacity"), TEXT_OPACITY_DEFAULT)
    display_mode = str(cfg.get("display_mode") or DISPLAY_MODE_FLOAT)
    monitor_device = cfg.get("monitor_device")
    monitor_id = cfg.get("monitor_id")
    available_monitors = _enumerate_monitors()
    initial_panel_monitor = (
        _choose_panel_monitor(available_monitors, monitor_device, monitor_id)
        if display_mode == DISPLAY_MODE_PANEL
        else None
    )
    if display_mode == DISPLAY_MODE_PANEL and initial_panel_monitor:
        monitor_device = initial_panel_monitor.device
        monitor_id = initial_panel_monitor.monitor_id or monitor_id
        cfg["monitor_device"] = monitor_device
        cfg["monitor_id"] = monitor_id

    # Split plate vs text: bg_layer is translucent white; text layer uses
    # transparentcolor so glyphs stay solid (Windows). Fallback: single alpha.
    use_split_alpha = sys.platform == "win32"
    floating_ui_bg = TRANSPARENT_KEY if use_split_alpha else BG
    ui_bg = PANEL_BG if initial_panel_monitor is not None else floating_ui_bg

    root = tk.Tk()
    root.report_callback_exception = _report_tk_callback_exception
    root.title("UsageFloat")
    if _docs_shot_active():
        root.withdraw()
    if initial_panel_monitor is not None:
        try:
            root.tk.call("tk", "scaling", initial_panel_monitor.dpi_y / 72.0)
        except Exception:
            pass
    root.configure(bg=ui_bg)
    root.overrideredirect(True)  # no title bar / no system chrome
    root.attributes("-topmost", bool(cfg.get("always_on_top", True)))
    root.resizable(False, False)
    try:
        root.configure(highlightthickness=0, bd=0)
    except Exception:
        pass
    root.minsize(CARD_WIDTH_MIN, 40)
    try:
        # Floating mode is clamped in apply_geometry; panel mode may occupy a
        # complete 960×640 (or larger) auxiliary display.
        root.maxsize(10000, 10000)
    except Exception:
        pass

    bg_layer: Any = None
    if use_split_alpha:
        bg_layer = tk.Toplevel(root)
        bg_layer.overrideredirect(True)
        # The plate keeps the original background colour.  Only the content
        # window uses the chroma key, so text colours remain unchanged.
        bg_layer.configure(bg=BG, highlightthickness=0, bd=0)
        bg_layer.attributes("-topmost", bool(cfg.get("always_on_top", True)))
        try:
            # Text layer: only non-key pixels (glyphs) are drawn — fully opaque
            root.wm_attributes("-transparentcolor", TRANSPARENT_KEY)
        except Exception:
            use_split_alpha = False
            ui_bg = BG
            root.configure(bg=ui_bg)
            try:
                bg_layer.destroy()
            except Exception:
                pass
            bg_layer = None

    layer_hwnds: dict[str, int] = {}

    def stack_text_above_plate() -> None:
        """Keep the text window immediately above the plate on Windows."""
        if bg_layer is None or state.get("panel_active"):
            return
        try:
            import ctypes
            from ctypes import wintypes

            text_hwnd = layer_hwnds.get("text")
            plate_hwnd = layer_hwnds.get("plate")
            if not text_hwnd or not plate_hwnd:
                root.update_idletasks()
                text_hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
                plate_hwnd = ctypes.windll.user32.GetParent(bg_layer.winfo_id()) or bg_layer.winfo_id()
                layer_hwnds["text"] = int(text_hwnd)
                layer_hwnds["plate"] = int(plate_hwnd)
                # The plate is visual only.  Never let a click activate it and
                # raise its translucent white surface over the text window.
                GWL_EXSTYLE = -20
                WS_EX_TOOLWINDOW = 0x00000080
                WS_EX_NOACTIVATE = 0x08000000
                plate_style = ctypes.windll.user32.GetWindowLongW(plate_hwnd, GWL_EXSTYLE)
                ctypes.windll.user32.SetWindowLongW(
                    plate_hwnd,
                    GWL_EXSTYLE,
                    plate_style | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
                )
            SWP_NOSIZE = 0x0001
            SWP_NOMOVE = 0x0002
            SWP_NOACTIVATE = 0x0010
            HWND_TOPMOST = -1
            HWND_NOTOPMOST = -2
            insert_after = HWND_TOPMOST if bool(state.get("always_on_top", True)) else HWND_NOTOPMOST
            flags = SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
            set_window_pos = ctypes.windll.user32.SetWindowPos
            set_window_pos.argtypes = [
                wintypes.HWND,
                wintypes.HWND,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.UINT,
            ]
            set_window_pos.restype = wintypes.BOOL
            # Put the plate in the requested band first, then the text. This
            # keeps both windows topmost while preserving their internal order.
            set_window_pos(
                wintypes.HWND(plate_hwnd), wintypes.HWND(insert_after), 0, 0, 0, 0, flags
            )
            set_window_pos(
                wintypes.HWND(text_hwnd), wintypes.HWND(insert_after), 0, 0, 0, 0, flags
            )
            # Alpha changes can remap a layered window and disturb the order.
            # Position the plate immediately behind the text layer explicitly,
            # without raising it above the settings window.
            set_window_pos(
                wintypes.HWND(plate_hwnd), wintypes.HWND(text_hwnd), 0, 0, 0, 0, flags
            )
        except Exception:
            try:
                root.lift(bg_layer)
            except Exception:
                pass

    def apply_opacity() -> None:
        if state.get("panel_active"):
            # Dedicated mode is a single opaque surface. Besides maximizing
            # contrast, removing the translucent plate eliminates cross-window
            # hover churn on transparent pixels.
            try:
                root.attributes("-alpha", 1.0)
            except Exception:
                pass
            if bg_layer is not None:
                try:
                    bg_layer.withdraw()
                except Exception:
                    pass
            return
        if bg_layer is not None:
            try:
                bg_layer.deiconify()
            except Exception:
                pass
        _apply_layer_opacities(
            root,
            bg_layer,
            state.get("bg_opacity", bg_opacity),
            state.get("text_opacity", text_opacity),
            restack=stack_text_above_plate,
        )

    # Content root — width via geometry(); footer pinned to bottom so it
    # never covers the last provider row (e.g. grok) when height is tight.
    body = tk.Frame(root, bg=ui_bg, bd=0, padx=CARD_PADX, pady=3)
    body.pack(fill="both", expand=True)

    footer = tk.Frame(body, bg=ui_bg)
    # Pack footer FIRST with side=BOTTOM so it stays at window bottom
    footer.pack(side="bottom", fill="x", pady=0)

    lines_frame = tk.Frame(body, bg=ui_bg)
    lines_frame.pack(side="top", fill="both", expand=True, anchor="n")

    # mpv creates its own hardware-accelerated child inside this native HWND.
    # The host remains unpacked while the usage dashboard is active.
    wallpaper_host = tk.Frame(root, bg=PANEL_BG, bd=0, highlightthickness=0)
    wallpaper_status = tk.Label(
        wallpaper_host,
        text="",
        fg=PANEL_MUTED,
        bg=PANEL_BG,
        font=("Microsoft YaHei UI", 18, "bold"),
        justify="center",
    )
    wallpaper_status.place(relx=0.5, rely=0.5, anchor="center")

    # Resize grip (shown when 允许调整大小)
    grip = tk.Label(
        root,
        text="◢",
        fg=FG_MUTED,
        bg=ui_bg,
        cursor="size_nw_se",
        font=("Segoe UI", 8),
    )

    # YaHei UI renders Chinese + Latin crisply on Windows ClearType
    font_family = "Microsoft YaHei UI"
    try:
        if font_family.lower() not in {f.lower() for f in tkfont.families()}:
            font_family = "Segoe UI"
    except Exception:
        font_family = "Segoe UI"

    font_main = tkfont.Font(family=font_family, size=font_size)
    font_footer = tkfont.Font(family=font_family, size=max(FONT_SIZE_MIN, font_size - 1))
    font_grip = tkfont.Font(family="Segoe UI", size=max(8, font_size - 2))
    number_family = "Bahnschrift SemiBold"
    try:
        if number_family.lower() not in {f.lower() for f in tkfont.families()}:
            number_family = "Segoe UI Semibold"
    except Exception:
        number_family = "Segoe UI Semibold"
    initial_panel_metrics = _panel_metrics(
        initial_panel_monitor.width if initial_panel_monitor else 960,
        initial_panel_monitor.height if initial_panel_monitor else 640,
        5,
        initial_panel_monitor.dpi_y if initial_panel_monitor else 96,
    )
    font_panel_title = tkfont.Font(
        family=font_family,
        size=initial_panel_metrics.title_points,
        weight="bold",
    )
    font_panel_value = tkfont.Font(
        family=number_family,
        size=initial_panel_metrics.value_points,
        weight="bold",
    )
    font_panel_meta = tkfont.Font(
        family=font_family,
        size=initial_panel_metrics.meta_points,
        weight="bold",
    )
    font_panel_footer = tkfont.Font(
        family=font_family,
        size=initial_panel_metrics.footer_points,
        weight="bold",
    )
    grip.configure(font=font_grip)

    def _initial_row_metrics() -> tuple[int, int]:
        try:
            ls = int(font_main.metrics("linespace"))
        except Exception:
            ls = font_size + 8
        return _compact_row_height(ls, font_size), max(72, int(font_size * 6))

    _rh0, _rc0 = _initial_row_metrics()

    state: dict[str, Any] = {
        "providers": [],
        "refreshing": False,
        "last_ok_at": None,
        "drag_ox": 0,
        "drag_oy": 0,
        "press_x": 0,
        "press_y": 0,
        "dragging": False,
        "always_on_top": bool(cfg.get("always_on_top", True)),
        "font_size": font_size,
        "card_width": card_width,
        "card_height": card_height,  # None = auto; int = user override (min content)
        "allow_resize": allow_resize,
        "display_mode": display_mode,
        "monitor_device": monitor_device,
        "monitor_id": monitor_id,
        "panel_monitor": initial_panel_monitor,
        "panel_active": display_mode == DISPLAY_MODE_PANEL and initial_panel_monitor is not None,
        "monitor_signature": None,
        "tk_dpi": initial_panel_monitor.dpi_y if initial_panel_monitor else None,
        "panel_rect_job": None,
        "floating_x": cfg.get("x"),
        "floating_y": cfg.get("y"),
        "bg_opacity": bg_opacity,
        "text_opacity": text_opacity,
        "row_height": _rh0,
        "right_col_w": _rc0,
        "settings_win": None,
        "settings_open_job": None,
        "resize_active": False,
        "resize_start_w": 0,
        "resize_start_h": 0,
        "resize_start_x": 0,
        "resize_start_y": 0,
        "rendering": False,
        "render_job": None,
        "refresh_control": None,
        "refresh_hitbox": None,
        "reset_click_targets": [],
        "reset_click_widgets": [],
        "reset_hitboxes": [],
        "reset_hover": False,
        "refresh_hover": False,
        "reset_cards_win": None,
        "claude_reset_win": None,
        "claude_session_win": None,
        "claude_session_busy": False,
        "claude_reset_busy": False,
        # (grant_id, request_id) of a claim whose outcome never arrived.
        "claude_reset_unsettled": None,
        "reset_cards_account": None,
        "reset_cards_busy": False,
        "panel_structure": None,
        "panel_items": [],
        "panel_empty_label": None,
        "double_ctrl_toggle": bool(cfg.get("shortcut_enabled", cfg.get("double_ctrl_toggle", True))),
        "shortcut_enabled": bool(cfg.get("shortcut_enabled", cfg.get("double_ctrl_toggle", True))),
        "shortcut_mode": _normalize_shortcut_mode(cfg.get("shortcut_mode")),
        "shortcut_key": _normalize_shortcut_key(cfg.get("shortcut_key")),
        "shortcut_repeat_count": _normalize_shortcut_repeat_count(
            cfg.get("shortcut_repeat_count")
        ),
        "shortcut_combo": _normalize_shortcut_combo(cfg.get("shortcut_combo")),
        "llm_proxy_timezone": _normalize_llm_proxy_timezone(
            cfg.get("llm_proxy_timezone")
        ),
        "double_ctrl_job": None,
        "wallpaper_active": False,
        "wallpaper_folder": cfg.get("wallpaper_folder"),
        "wallpaper_folders": list(cfg.get("wallpaper_folders") or []),
        "wallpaper_disabled_folders": list(
            cfg.get("wallpaper_disabled_folders") or []
        ),
        "wallpaper_subdir_weights": dict(cfg.get("wallpaper_subdir_weights") or {}),
        "wallpaper_random_mode": _normalize_wallpaper_random_mode(
            cfg.get("wallpaper_random_mode")
        ),
        "wallpaper_image_seconds": int(
            cfg.get("wallpaper_image_seconds") or WALLPAPER_IMAGE_SECONDS
        ),
        "wallpaper_audio": bool(cfg.get("wallpaper_audio", True)),
        "wallpaper_check_job": None,
        "wallpaper_ready_job": None,
        "wallpaper_background_job": None,
        "wallpaper_unpause_job": None,
        "wallpaper_preparing": False,
        "wallpaper_ready": False,
        "wallpaper_child_seen_at": None,
        "wallpaper_child_hwnd": 0,
        "wallpaper_prepare_started": None,
        "wallpaper_prepare_error": "",
        "wallpaper_dropped_video": None,
        "wallpaper_drop_retry_job": None,
        "wallpaper_drop_cleanup": None,
    }

    repeat_detector = _RepeatKeyDetector(
        tap_count=_normalize_shortcut_repeat_count(cfg.get("shortcut_repeat_count"))
    )
    combo_detector = _ComboKeyDetector()
    wallpaper_player = _MpvWallpaperPlayer()
    get_async_key_state: Any = None
    if sys.platform == "win32":
        try:
            import ctypes

            get_async_key_state = ctypes.windll.user32.GetAsyncKeyState
            get_async_key_state.argtypes = [ctypes.c_int]
            get_async_key_state.restype = ctypes.c_short
        except Exception:
            get_async_key_state = None

    def apply_mode_surface() -> None:
        nonlocal ui_bg
        panel_mode_active = bool(state.get("panel_active"))
        ui_bg = PANEL_BG if panel_mode_active else floating_ui_bg
        try:
            root.configure(bg=ui_bg)
            body.configure(bg=ui_bg)
            footer.configure(bg=ui_bg)
            lines_frame.configure(bg=ui_bg)
            grip.configure(bg=ui_bg)
            control = state.get("refresh_control")
            if control is not None and control.winfo_exists():
                control.configure(
                    bg=ui_bg,
                    activebackground=ui_bg,
                    fg=PANEL_MUTED if panel_mode_active else FG_MUTED,
                    activeforeground=PANEL_FG if panel_mode_active else FG_LABEL,
                )
        except Exception:
            pass
        apply_opacity()

    def enforce_native_panel_rect() -> None:
        """Pin the opaque panel in physical pixels across mixed-DPI layouts.

        Tk geometry coordinates can be virtualized while Windows is applying a
        new display arrangement. SetWindowPos on the native wrapper keeps the
        selected physical monitor authoritative.
        """
        state["panel_rect_job"] = None
        panel: MonitorInfo | None = state.get("panel_monitor")
        if not state.get("panel_active") or panel is None or sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes

            root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
            insert_after = -1 if bool(state.get("always_on_top", True)) else -2
            SWP_NOACTIVATE = 0x0010
            SWP_SHOWWINDOW = 0x0040
            set_window_pos = ctypes.windll.user32.SetWindowPos
            set_window_pos.argtypes = [
                wintypes.HWND,
                wintypes.HWND,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.UINT,
            ]
            set_window_pos.restype = wintypes.BOOL
            set_window_pos(
                wintypes.HWND(hwnd),
                wintypes.HWND(insert_after),
                panel.x,
                panel.y,
                panel.width,
                panel.height,
                SWP_NOACTIVATE | SWP_SHOWWINDOW,
            )
        except Exception:
            pass

    def schedule_native_panel_rect(delay_ms: int = 80) -> None:
        previous = state.get("panel_rect_job")
        if previous is not None:
            try:
                root.after_cancel(previous)
            except Exception:
                pass
        state["panel_rect_job"] = root.after(delay_ms, enforce_native_panel_rect)

    def apply_shortcut_config(
        *,
        enabled: bool | None = None,
        mode: str | None = None,
        key: str | None = None,
        repeat_count: int | None = None,
        combo: list[str] | None = None,
        persist: bool = True,
    ) -> None:
        if enabled is not None:
            state["shortcut_enabled"] = bool(enabled)
            state["double_ctrl_toggle"] = bool(enabled)
        if mode is not None:
            state["shortcut_mode"] = _normalize_shortcut_mode(mode)
        if key is not None:
            state["shortcut_key"] = _normalize_shortcut_key(key)
        if repeat_count is not None:
            state["shortcut_repeat_count"] = _normalize_shortcut_repeat_count(repeat_count)
        if combo is not None:
            state["shortcut_combo"] = _normalize_shortcut_combo(combo)
        repeat_detector.tap_count = int(state["shortcut_repeat_count"])
        repeat_detector.reset()
        combo_detector.reset()
        enabled_now = bool(state.get("shortcut_enabled"))
        if not enabled_now and state.get("wallpaper_preparing") and not state.get("wallpaper_active"):
            _clear_wallpaper_state()
        elif enabled_now:
            _schedule_background_wallpaper_prepare()
        cfg["shortcut_enabled"] = enabled_now
        cfg["double_ctrl_toggle"] = enabled_now
        cfg["shortcut_mode"] = str(state["shortcut_mode"])
        cfg["shortcut_key"] = str(state["shortcut_key"])
        cfg["shortcut_repeat_count"] = int(state["shortcut_repeat_count"])
        cfg["shortcut_combo"] = list(state["shortcut_combo"])
        if persist:
            save_config(cfg)

    def set_double_ctrl_toggle(enabled: bool, *, persist: bool = True) -> None:
        apply_shortcut_config(enabled=enabled, persist=persist)

    def _cancel_wallpaper_check() -> None:
        job = state.get("wallpaper_check_job")
        state["wallpaper_check_job"] = None
        if job is not None:
            try:
                root.after_cancel(job)
            except Exception:
                pass

    def _cancel_wallpaper_prepare_jobs() -> None:
        for key in (
            "wallpaper_ready_job",
            "wallpaper_background_job",
            "wallpaper_unpause_job",
            "wallpaper_drop_retry_job",
        ):
            job = state.get(key)
            state[key] = None
            if job is not None:
                try:
                    root.after_cancel(job)
                except Exception:
                    pass

    def _clear_wallpaper_state(*, stop_player: bool = True) -> None:
        _cancel_wallpaper_prepare_jobs()
        _cancel_wallpaper_check()
        state["wallpaper_preparing"] = False
        state["wallpaper_ready"] = False
        state["wallpaper_child_seen_at"] = None
        state["wallpaper_child_hwnd"] = 0
        state["wallpaper_prepare_started"] = None
        state["wallpaper_prepare_error"] = ""
        if stop_player:
            wallpaper_player.stop()
        wallpaper_host.place_forget()

    def _show_wallpaper_error(message: str) -> None:
        state["wallpaper_active"] = True
        state["wallpaper_preparing"] = False
        state["wallpaper_ready"] = False
        state["wallpaper_prepare_error"] = message
        body.pack_forget()
        wallpaper_host.place(x=0, y=0, relwidth=1, relheight=1)
        root.update_idletasks()
        _move_native_child_window(
            wallpaper_host.winfo_id(),
            0,
            0,
            root.winfo_width(),
            root.winfo_height(),
        )
        wallpaper_host.lift()
        wallpaper_status.configure(text=message)
        wallpaper_status.place(relx=0.5, rely=0.5, anchor="center")
        wallpaper_status.lift()

    def _check_wallpaper_player() -> None:
        state["wallpaper_check_job"] = None
        if not state.get("wallpaper_active"):
            return
        process = wallpaper_player.process
        if process is not None and process.poll() is not None:
            wallpaper_status.configure(
                text="动态壁纸播放器已退出\n请检查媒体格式或 mpv 安装"
            )
            wallpaper_status.place(relx=0.5, rely=0.5, anchor="center")
            wallpaper_status.lift()
            return
        state["wallpaper_check_job"] = root.after(1000, _check_wallpaper_player)

    def _finish_wallpaper_reveal() -> None:
        state["wallpaper_unpause_job"] = None
        if not state.get("wallpaper_active") or not wallpaper_player.running:
            return
        _cancel_wallpaper_prepare_jobs()
        state["wallpaper_preparing"] = False
        wallpaper_status.place_forget()
        wallpaper_host.place(x=0, y=0, width=0, height=0, relwidth=1, relheight=1)
        root.update_idletasks()
        _move_native_child_window(
            wallpaper_host.winfo_id(),
            0,
            0,
            root.winfo_width(),
            root.winfo_height(),
        )
        body.pack_forget()
        wallpaper_host.lift()
        if state.get("wallpaper_audio"):
            wallpaper_player.set_muted(False)
        state["wallpaper_check_job"] = root.after(1000, _check_wallpaper_player)

    def _reveal_ready_wallpaper() -> None:
        if not state.get("wallpaper_active") or not state.get("wallpaper_ready"):
            return
        if state.get("wallpaper_unpause_job") is not None:
            return
        if not wallpaper_player.set_paused(False):
            def retry_reveal() -> None:
                state["wallpaper_unpause_job"] = None
                _reveal_ready_wallpaper()

            state["wallpaper_unpause_job"] = root.after(
                40,
                retry_reveal,
            )
            return
        first_is_video = bool(
            wallpaper_player.last_order
            and wallpaper_player.last_order[0].suffix.casefold()
            in WALLPAPER_VIDEO_EXTENSIONS
        )
        if first_is_video:
            # Start decoding/motion while the prepared HWND is still clipped.
            # The usage panel remains visible until the short preroll completes.
            state["wallpaper_unpause_job"] = root.after(
                WALLPAPER_VIDEO_PREROLL_MS,
                _finish_wallpaper_reveal,
            )
        else:
            _finish_wallpaper_reveal()

    def _poll_wallpaper_ready() -> None:
        state["wallpaper_ready_job"] = None
        if not state.get("wallpaper_preparing"):
            return
        process = wallpaper_player.process
        if process is None or process.poll() is not None:
            message = "动态壁纸播放器启动失败\n请检查媒体格式或 mpv 安装"
            if state.get("wallpaper_active"):
                _show_wallpaper_error(message)
            else:
                _clear_wallpaper_state()
            return

        now = time.monotonic()
        child = _find_embedded_mpv_window(wallpaper_host.winfo_id())
        child_seen_at = state.get("wallpaper_child_seen_at")
        if child:
            state["wallpaper_child_hwnd"] = child
            # mpv is hosted by a Tk HWND owned by another process. Let it render,
            # but route pointer/keyboard/drop input to the Tk parent so focus and
            # Explorer drops work reliably across the process boundary.
            _set_native_window_enabled(child, False)
            if child_seen_at is None:
                state["wallpaper_child_seen_at"] = now
            elif now - float(child_seen_at) >= WALLPAPER_FIRST_FRAME_SETTLE_SECONDS:
                state["wallpaper_ready"] = True
                if state.get("wallpaper_active"):
                    _reveal_ready_wallpaper()
                return

        started = state.get("wallpaper_prepare_started")
        if state.get("wallpaper_active") and started is not None and now - float(started) > 4.0:
            wallpaper_player.stop()
            _show_wallpaper_error("动态壁纸首帧加载超时\n请检查媒体文件")
            return
        state["wallpaper_ready_job"] = root.after(
            WALLPAPER_READY_POLL_MS,
            _poll_wallpaper_ready,
        )

    def _prepare_wallpaper(*, commit: bool) -> bool:
        if not state.get("panel_active"):
            return False
        dropped_video = state.get("wallpaper_dropped_video")
        if not isinstance(dropped_video, Path) or not dropped_video.is_file():
            dropped_video = None
        if state.get("wallpaper_preparing"):
            # A drop must not reveal the already-preloaded random item.
            if dropped_video is not None or not wallpaper_player.running:
                _clear_wallpaper_state()
                return _prepare_wallpaper(commit=commit)
            if commit:
                state["wallpaper_active"] = True
                if state.get("wallpaper_ready"):
                    _reveal_ready_wallpaper()
            return True

        folders = _enabled_wallpaper_folders(
            list(state.get("wallpaper_folders") or []),
            state.get("wallpaper_disabled_folders"),
        )
        existing_order = list(wallpaper_player.last_order or [])
        keep_order = bool(dropped_video is not None and existing_order)
        if keep_order:
            files = _with_preferred_wallpaper_first(existing_order, dropped_video)
        else:
            files = _collect_wallpaper_media_folders(folders)
            if dropped_video is not None:
                files = _with_preferred_wallpaper_first(files, dropped_video)
        executable = _find_mpv_executable(cfg.get("mpv_path"))
        error = ""
        if not folders and dropped_video is None:
            error = "请至少启用一个动态壁纸目录"
        elif not files:
            error = "所选文件夹中没有支持的图片或视频"
        elif executable is None:
            error = "未找到 mpv 播放后端\n请运行 install-mpv.ps1"
        if error:
            state["wallpaper_prepare_error"] = error
            if commit:
                _show_wallpaper_error(error)
            return True

        state["wallpaper_active"] = bool(commit)
        state["wallpaper_preparing"] = True
        state["wallpaper_ready"] = False
        state["wallpaper_child_seen_at"] = None
        state["wallpaper_prepare_started"] = time.monotonic()
        state["wallpaper_prepare_error"] = ""
        wallpaper_status.place_forget()
        # A native mpv child does not obey Tk's sibling stacking reliably.
        # Keep its full-size host beyond the root's clipped area while it
        # decodes the first frame, then move the host on-screen atomically.
        host_width = max(1, root.winfo_width())
        host_height = max(1, root.winfo_height())
        wallpaper_host.place(
            x=host_width + 64,
            y=host_height + 64,
            width=host_width,
            height=host_height,
        )
        root.update_idletasks()
        _move_native_child_window(
            wallpaper_host.winfo_id(),
            host_width + 64,
            host_height + 64,
            host_width,
            host_height,
        )

        ok, error = wallpaper_player.start(
            executable,
            wallpaper_host.winfo_id(),
            files,
            image_seconds=int(state["wallpaper_image_seconds"]),
            audio_enabled=bool(state.get("wallpaper_audio", True)),
            folder_weights=dict(state.get("wallpaper_subdir_weights") or {}),
            random_mode=str(state.get("wallpaper_random_mode")),
            preferred_first=dropped_video,
            keep_order=keep_order,
        )
        if not ok:
            state["wallpaper_preparing"] = False
            if commit:
                _show_wallpaper_error(error)
            else:
                _clear_wallpaper_state()
            return True

        if dropped_video is not None:
            state["wallpaper_dropped_video"] = None

        state["wallpaper_ready_job"] = root.after(
            WALLPAPER_READY_POLL_MS,
            _poll_wallpaper_ready,
        )
        return True

    def _schedule_background_wallpaper_prepare(delay_ms: int = 80) -> None:
        job = state.get("wallpaper_background_job")
        state["wallpaper_background_job"] = None
        if job is not None:
            try:
                root.after_cancel(job)
            except Exception:
                pass
        if (
            not state.get("double_ctrl_toggle")
            or not state.get("panel_active")
            or state.get("wallpaper_active")
            or state.get("wallpaper_preparing")
            or not _enabled_wallpaper_folders(
                list(state.get("wallpaper_folders") or []),
                state.get("wallpaper_disabled_folders"),
            )
        ):
            return

        def prepare() -> None:
            state["wallpaper_background_job"] = None
            if not state.get("wallpaper_active"):
                _prepare_wallpaper(commit=False)

        state["wallpaper_background_job"] = root.after(delay_ms, prepare)

    def set_wallpaper_active(active: bool) -> bool:
        active = bool(active)
        if active and state.get("wallpaper_active"):
            return True

        if not active:
            state["wallpaper_active"] = False
            if not body.winfo_manager():
                body.pack(fill="both", expand=True)
            body.lift()
            render(relayout=False)
            root.update_idletasks()
            _clear_wallpaper_state()
            _schedule_background_wallpaper_prepare()
            return True
        return _prepare_wallpaper(commit=True)

    def play_dropped_video(path: Path) -> None:
        state["wallpaper_drop_retry_job"] = None
        try:
            if not state.get("panel_active"):
                return
            state["wallpaper_dropped_video"] = path
            # The usage panel keeps a random session preloaded off-screen.
            # Revealing that session, or insert-next-play on top of it, plays
            # the preloaded file instead of the one that was just dropped.
            if (
                state.get("wallpaper_active")
                or state.get("wallpaper_preparing")
                or wallpaper_player.running
            ):
                _clear_wallpaper_state()
                state["wallpaper_active"] = False
            set_wallpaper_active(True)
        except Exception:
            _log_exception("dropped video")
            try:
                _show_wallpaper_error("拖入视频播放失败\n请检查视频格式")
            except Exception:
                pass

    def handle_dropped_files(paths: list[str]) -> None:
        try:
            videos = _dropped_wallpaper_videos(paths)
            if not videos:
                return
            retry_job = state.get("wallpaper_drop_retry_job")
            if retry_job is not None:
                try:
                    root.after_cancel(retry_job)
                except Exception:
                    pass
                state["wallpaper_drop_retry_job"] = None
            play_dropped_video(videos[0])
        except Exception:
            _log_exception("dropped video")

    state["wallpaper_drop_cleanup"] = _install_windows_file_drop(
        root,
        handle_dropped_files,
    )

    def focus_wallpaper_controls(_event: Any = None) -> None:
        if not state.get("wallpaper_active"):
            return
        try:
            root.focus_force()
        except Exception:
            pass

    def on_wallpaper_arrow(key: str) -> str | None:
        if not state.get("wallpaper_active") or not wallpaper_player.running:
            return None
        _send_mpv_ipc_command(wallpaper_player.ipc_pipe, ["keypress", key])
        return "break"

    def on_wallpaper_wheel(event: Any) -> str | None:
        if not state.get("wallpaper_active") or not wallpaper_player.running:
            return None
        delta = int(getattr(event, "delta", 0) or 0)
        if not delta:
            return None
        volume_step = 5 if delta > 0 else -5
        _send_mpv_ipc_command(
            wallpaper_player.ipc_pipe,
            ["add", "volume", volume_step],
        )
        return "break"

    root.bind("<Button-1>", focus_wallpaper_controls, add="+")
    root.bind("<Left>", lambda _event: on_wallpaper_arrow("LEFT"), add="+")
    root.bind("<Right>", lambda _event: on_wallpaper_arrow("RIGHT"), add="+")
    root.bind("<MouseWheel>", on_wallpaper_wheel, add="+")

    def toggle_wallpaper_mode() -> None:
        set_wallpaper_active(not bool(state.get("wallpaper_active")))

    def poll_double_ctrl_toggle() -> None:
        state["double_ctrl_job"] = None
        try:
            if (
                not state.get("shortcut_enabled")
                or get_async_key_state is None
                or state.get("shortcut_capture")
            ):
                repeat_detector.reset()
                combo_detector.reset()
                return
            toggled = False
            mode = _normalize_shortcut_mode(state.get("shortcut_mode"))
            if mode == SHORTCUT_MODE_COMBO:
                combo_keys = list(state.get("shortcut_combo") or SHORTCUT_COMBO_DEFAULT)
                if len(combo_keys) >= 2:
                    all_down = all(
                        _named_key_down(get_async_key_state, name) for name in combo_keys
                    )
                    extra = _other_shortcut_key_down(
                        get_async_key_state, exclude_keys=combo_keys
                    )
                    toggled = combo_detector.update(all_down and not extra)
            else:
                combo_detector.reset()
                key_name = _normalize_shortcut_key(state.get("shortcut_key"))
                key_down = _named_key_down(get_async_key_state, key_name)
                other_key_down = False
                if key_down:
                    other_key_down = _other_shortcut_key_down(
                        get_async_key_state, exclude_keys=[key_name]
                    )
                repeat_detector.tap_count = _normalize_shortcut_repeat_count(
                    state.get("shortcut_repeat_count")
                )
                toggled = repeat_detector.update(
                    key_down,
                    other_key_down,
                    time.monotonic(),
                )
            if toggled:
                toggle_wallpaper_mode()
        finally:
            try:
                state["double_ctrl_job"] = root.after(
                    DOUBLE_CTRL_POLL_MS,
                    poll_double_ctrl_toggle,
                )
            except Exception:
                state["double_ctrl_job"] = None

    apply_mode_surface()

    def metrics_for_font(size: int) -> tuple[int, int]:
        """(row_height, right_col_min) from real font metrics — avoids under-height clip."""
        try:
            # linespace is the true pixel line box for the active font
            ls = int(font_main.metrics("linespace"))
        except Exception:
            ls = size + 8
        return _compact_row_height(ls, size), max(72, int(size * 6))

    def apply_font_size(size: int, *, persist: bool = True, relayout: bool = True) -> None:
        size = max(FONT_SIZE_MIN, min(FONT_SIZE_MAX, int(size)))
        state["font_size"] = size
        try:
            font_main.configure(size=size)
            font_footer.configure(size=max(FONT_SIZE_MIN, size - 1))
            font_grip.configure(size=max(8, size - 2))
        except Exception:
            pass
        # Measure after updating the named font. Measuring first made the live
        # preview use the previous slider value until the next event/close.
        rh, rcw = metrics_for_font(size)
        state["row_height"] = rh
        state["right_col_w"] = rcw
        if persist:
            cfg["font_size"] = size
            save_config(cfg)
        if relayout:
            # Defer one frame so Scale drag never re-enters destroy mid-callback.
            schedule_render(16)

    def set_allow_resize(enabled: bool, *, persist: bool = True) -> None:
        state["allow_resize"] = bool(enabled)
        if state["allow_resize"] and not state.get("panel_active"):
            grip.place(relx=1.0, rely=1.0, anchor="se", x=-2, y=-1)
            grip.lift()
        else:
            grip.place_forget()
            if not state["allow_resize"] and state.get("card_height") is not None:
                state["card_height"] = None
                cfg["card_height"] = None
                schedule_render(0)
        if persist:
            cfg["allow_resize"] = state["allow_resize"]
            save_config(cfg)

    def clear_lines() -> None:
        # Snapshot first — never iterate children while destroying during re-entry
        kids = list(lines_frame.winfo_children())
        for w in kids:
            try:
                w.destroy()
            except Exception:
                pass
        state["panel_structure"] = None
        state["panel_items"] = []
        state["panel_empty_label"] = None
        state["reset_click_targets"] = []
        state["reset_click_widgets"] = []
        state["reset_hitboxes"] = []
        # Grid weights survive child destruction. Reset them before switching
        # between the compact strip and the auxiliary-panel instrument grid.
        try:
            for index in range(12):
                # uniform too: an emptied row left in the uniform group still
                # takes an equal share, squeezing the grid when rows drop.
                lines_frame.grid_rowconfigure(index, weight=0, minsize=0, uniform="")
                lines_frame.grid_columnconfigure(index, weight=0, minsize=0, uniform="")
        except Exception:
            pass

    def bind_hover_tracking(widget: tk.Misc) -> None:
        widget.bind("<Motion>", track_reset_hover, add="+")
        widget.bind("<Leave>", lambda _event: set_reset_hover(False), add="+")

    def bind_drag(widget: tk.Misc) -> None:
        widget.bind("<ButtonPress-1>", start_move)
        widget.bind("<B1-Motion>", do_move)
        widget.bind("<ButtonRelease-1>", stop_move)
        widget.bind("<Button-3>", show_menu)

    def start_move(event: tk.Event) -> None:
        stack_text_above_plate()
        if state.get("panel_active"):
            state["dragging"] = False
            return
        state["press_x"] = event.x_root
        state["press_y"] = event.y_root
        state["drag_ox"] = event.x_root - root.winfo_x()
        state["drag_oy"] = event.y_root - root.winfo_y()
        state["dragging"] = False

    def do_move(event: tk.Event) -> None:
        if state.get("panel_active"):
            return
        if abs(event.x_root - state["press_x"]) > 4 or abs(event.y_root - state["press_y"]) > 4:
            state["dragging"] = True
            x = event.x_root - state["drag_ox"]
            y = event.y_root - state["drag_oy"]
            root.geometry(_tk_position(x, y))
            if bg_layer is not None:
                try:
                    bg_layer.geometry(_tk_position(x, y))
                except Exception:
                    pass

    def _event_local_point(event: tk.Event) -> tuple[int, int] | None:
        try:
            return (
                int(event.x_root) - int(root.winfo_rootx()),
                int(event.y_root) - int(root.winfo_rooty()),
            )
        except Exception:
            return None

    def event_hits_refresh(event: tk.Event) -> bool:
        point = _event_local_point(event)
        if point is None:
            return False
        return _point_in_box(point[0], point[1], state.get("refresh_hitbox"))

    def reset_target_at_event(event: tk.Event) -> str | None:
        """Which provider's usage number sits under the pointer, if any.

        Returns a Codex account id or a Claude row ("claude:7d" / "claude:5h");
        the caller opens whichever reset UI that target has.
        """
        point = _event_local_point(event)
        if point is None:
            return None
        for box, provider_id in state.get("reset_hitboxes") or []:
            if _point_in_box(point[0], point[1], box):
                return provider_id
        return None

    def register_reset_click_targets(targets: list[tuple[tk.Misc, str]]) -> None:
        """Track each clickable percentage label with the provider it belongs to.

        The rows are rendered exactly as before; this only remembers which
        labels are live. Transparent padding and the plate layer swallow direct
        widget events, so hover and click are resolved from window-relative
        boxes the same way the refresh control is. Carrying the provider id
        next to each box is what stops a click on one account's number from
        redeeming the other account's card, and keeps Claude's row pointed at
        its own dialog.
        """
        state["reset_click_targets"] = targets
        state["reset_click_widgets"] = [widget for widget, _ in targets]
        state["reset_hitboxes"] = []
        state["reset_hover"] = False
        for widget, _provider_id in targets:
            try:
                # Remember the rendered colour so hover can hand it back
                # untouched instead of recomputing the tone.
                setattr(widget, "_usage_rest_fg", widget.cget("fg"))
                widget.configure(cursor="hand2")
                widget.bind("<Enter>", lambda _event: set_reset_hover(True), add="+")
                widget.bind("<Leave>", lambda _event: set_reset_hover(False), add="+")
            except Exception:
                pass
        root.after_idle(update_reset_hitboxes)

    def set_reset_hover(hovering: bool) -> None:
        hovering = bool(hovering)
        if bool(state.get("reset_hover")) == hovering:
            return
        state["reset_hover"] = hovering
        if state.get("panel_active"):
            # Same trade-off as the refresh control: repainting a full-screen
            # layered surface for a hover tint is not worth it, and the hand
            # cursor already communicates the click.
            return
        for widget in state.get("reset_click_widgets") or []:
            try:
                if not widget.winfo_exists():
                    continue
                rest = getattr(widget, "_usage_rest_fg", None)
                widget.configure(fg=FG if hovering else (rest or FG))
            except Exception:
                pass
        update_plate_cursor()

    def update_reset_hitboxes(attempt: int = 0) -> None:
        boxes: list[tuple[tuple[int, int, int, int], str]] = []
        unmapped = False
        for widget, provider_id in state.get("reset_click_targets") or []:
            try:
                if not widget.winfo_exists():
                    continue
                width = widget.winfo_width()
                height = widget.winfo_height()
                if width <= 1 or height <= 1 or not widget.winfo_ismapped():
                    # Tk reports 1x1 at 0,0 until the row is actually mapped,
                    # and rows are rebuilt on every render, so measure again
                    # instead of freezing a degenerate box until the next
                    # refresh five minutes later.
                    unmapped = True
                    continue
                left = widget.winfo_rootx() - root.winfo_rootx()
                top = widget.winfo_rooty() - root.winfo_rooty()
                boxes.append(((left, top, left + width, top + height), provider_id))
            except Exception:
                continue
        if unmapped and attempt < 8:
            root.after(60, lambda: update_reset_hitboxes(attempt + 1))
            if not boxes:
                return
        state["reset_hitboxes"] = boxes

    def update_plate_cursor() -> None:
        if bg_layer is None or state.get("panel_active"):
            return
        hot = bool(state.get("refresh_hover") or state.get("reset_hover"))
        try:
            bg_layer.configure(cursor="hand2" if hot else "")
        except Exception:
            pass

    def set_refresh_hover(hovering: bool) -> None:
        control = state.get("refresh_control")
        try:
            if control is not None and control.winfo_exists():
                if state.get("panel_active"):
                    # Avoid repainting a full-screen layered surface for a tiny
                    # hover effect. The hand cursor already communicates click.
                    control.configure(fg=PANEL_MUTED)
                else:
                    control.configure(fg=FG_LABEL if hovering else FG_MUTED)
        except Exception:
            pass
        state["refresh_hover"] = bool(hovering)
        update_plate_cursor()

    def track_reset_hover(event: tk.Event) -> None:
        set_reset_hover(reset_target_at_event(event) is not None)

    def open_reset_ui(provider_id: str) -> None:
        """Send the click to whichever reset UI this provider has."""
        if provider_id == "claude:7d":
            open_claude_reset_info()
        elif provider_id == "claude:5h":
            open_claude_session_reset()
        elif provider_id in CODEX_ACCOUNT_BY_ID:
            open_codex_reset_cards(provider_id)

    def stop_move(event: tk.Event) -> None:
        if state.get("panel_active"):
            if event_hits_refresh(event) and not state["refreshing"]:
                refresh_async(force=True)
            else:
                provider_id = reset_target_at_event(event)
                if provider_id:
                    open_reset_ui(provider_id)
        elif state["dragging"]:
            cfg["x"] = root.winfo_x()
            cfg["y"] = root.winfo_y()
            state["floating_x"] = cfg["x"]
            state["floating_y"] = cfg["y"]
            save_config(cfg)
        elif event_hits_refresh(event) and not state["refreshing"]:
            refresh_async(force=True)
        else:
            provider_id = reset_target_at_event(event)
            if provider_id:
                open_reset_ui(provider_id)

    def shown_items() -> list[str]:
        return list(cfg.get("providers") or DEFAULT_PROVIDERS)

    def _right_text(dr: DisplayRow) -> tuple[str, str, str | None]:
        """Return (pct_text, time_text, fail_or_summary) for right side."""
        if dr.failed:
            return "", "", dr.summary or "更新失败"
        if dr.used_pct is not None:
            rem = format_remaining(dr.resets_at)
            pct_text = dr.summary or f"{dr.used_pct:.0f}%"
            return pct_text, rem, None
        if dr.summary:
            return "", "", dr.summary
        return "", "", ""

    def _panel_row_presentation(
        dr: DisplayRow,
    ) -> tuple[str, str, str, tkfont.Font, str]:
        pct_text, reset_text, extra = _right_text(dr)
        if pct_text:
            value_text = pct_text
            value_color = panel_tone_color(dr.used_pct or 0)
            value_font = font_panel_value
        else:
            value_text = extra or "—"
            value_color = PANEL_RED if dr.failed else PANEL_FG
            value_font = font_panel_title
        meta_text = reset_text if reset_text else ("数据不可用" if dr.failed else "")
        return pct_text, value_text, value_color, value_font, meta_text

    def _paint_panel_bar(
        canvas: tk.Canvas,
        *,
        width: int | None = None,
        height: int | None = None,
    ) -> None:
        bar_width = max(1, int(width or canvas.winfo_width()))
        bar_height = max(1, int(height or canvas.winfo_height()))
        pct = float(getattr(canvas, "_usage_pct", 0.0))
        color = str(getattr(canvas, "_usage_color", PANEL_FG))
        fill_width = int(round(bar_width * max(0.0, min(100.0, pct)) / 100.0))
        canvas.delete("usage-bar")
        canvas.create_rectangle(
            0,
            0,
            bar_width,
            bar_height,
            fill=PANEL_TRACK,
            outline="",
            tags="usage-bar",
        )
        if fill_width:
            canvas.create_rectangle(
                0,
                0,
                fill_width,
                bar_height,
                fill=color,
                outline="",
                tags="usage-bar",
            )

    def _render_panel_lines(providers: list[ProviderUsage]) -> None:
        panel: MonitorInfo | None = state.get("panel_monitor")
        width = panel.width if panel is not None else max(640, root.winfo_width())
        height = panel.height if panel is not None else max(480, root.winfo_height())
        rows = iter_display_rows(providers, shown_items()) if providers else []
        metrics = _panel_metrics(
            width,
            height,
            max(5, len(rows)),
            panel.dpi_y if panel is not None else 96,
        )
        body.configure(
            padx=metrics.outer_padding,
            pady=max(12, metrics.outer_padding - 4),
        )
        font_panel_title.configure(size=metrics.title_points)
        font_panel_value.configure(size=metrics.value_points)
        font_panel_meta.configure(size=metrics.meta_points)
        font_panel_footer.configure(size=metrics.footer_points)

        if not rows:
            message = "正在读取用量…" if state["refreshing"] else "暂无用量数据"
            empty = state.get("panel_empty_label")
            try:
                if empty is not None and empty.winfo_exists():
                    empty.configure(text=message)
                    return
            except Exception:
                pass
            clear_lines()
            empty = tk.Label(
                lines_frame,
                text=message,
                fg=PANEL_MUTED,
                bg=ui_bg,
                font=font_panel_title,
                anchor="center",
            )
            empty.pack(fill="both", expand=True)
            state["panel_structure"] = ("empty",)
            state["panel_empty_label"] = empty
            bind_drag(empty)
            bind_drag(lines_frame)
            return

        columns, grid_rows = _panel_grid_shape(max(4, len(rows)), width, height)
        presentations = [_panel_row_presentation(dr) for dr in rows]
        structure = (
            columns,
            grid_rows,
            metrics.cell_gap,
            metrics.bar_height,
            tuple(
                (dr.title, bool(presentation[0]), presentation[1])
                for dr, presentation in zip(rows, presentations)
            ),
        )
        items = state.get("panel_items") or []
        if state.get("panel_structure") == structure and len(items) == len(rows):
            try:
                for dr, presentation, item in zip(rows, presentations, items):
                    pct_text, value_text, value_color, value_font, meta_text = presentation
                    title_label = item["title"]
                    value_label = item["value"]
                    meta_label = item["meta"]
                    if not all(
                        widget.winfo_exists()
                        for widget in (title_label, value_label, meta_label)
                    ):
                        raise tk.TclError("stale panel widget cache")
                    title_label.configure(text=dr.title)
                    value_width = _panel_value_slot_width(font_panel_value, value_text)
                    value_label.configure(
                        text=value_text,
                        fg=value_color,
                        font=value_font,
                    )
                    value_label.place(
                        relx=1.0,
                        rely=1.0,
                        anchor="se",
                        y=-7,
                        width=value_width,
                    )
                    headline = item.get("headline")
                    if headline is not None and headline.winfo_exists():
                        headline.grid_columnconfigure(1, minsize=value_width)
                    meta_label.configure(text=meta_text)
                    bar = item.get("bar")
                    if pct_text and bar is not None and bar.winfo_exists():
                        bar._usage_pct = float(dr.used_pct or 0)  # type: ignore[attr-defined]
                        bar._usage_color = value_color  # type: ignore[attr-defined]
                        _paint_panel_bar(bar)
                # Nothing was removed or remapped: Tk paints the changed values
                # together after this callback, so the panel never exposes an
                # intermediate all-black frame during automatic refresh.
                return
            except (KeyError, tk.TclError):
                pass

        clear_lines()
        for column in range(columns):
            lines_frame.grid_columnconfigure(column, weight=1, uniform="panel-column")
        for row_index in range(grid_rows):
            lines_frame.grid_rowconfigure(row_index, weight=1, uniform="panel-row")

        half_gap = max(4, metrics.cell_gap // 2)
        panel_items: list[dict[str, Any]] = []
        reset_targets: list[tuple[tk.Misc, str]] = []
        for index, (dr, presentation) in enumerate(zip(rows, presentations)):
            # Column-major placement keeps Claude's related windows together.
            column = min(columns - 1, index // grid_rows)
            row_index = index % grid_rows
            tile = tk.Frame(lines_frame, bg=ui_bg, bd=0)
            tile.grid(
                row=row_index,
                column=column,
                sticky="nsew",
                padx=(half_gap if column else 0, half_gap if column < columns - 1 else 0),
                pady=(half_gap if row_index else 0, half_gap if row_index < grid_rows - 1 else 0),
            )

            instrument = tk.Frame(tile, bg=ui_bg, bd=0)
            instrument.pack(fill="x", expand=True, anchor="center")
            headline = tk.Frame(instrument, bg=ui_bg, bd=0)
            headline.pack(fill="x")
            pct_text, value_text, value_color, value_font, meta_text = presentation
            value_width = _panel_value_slot_width(font_panel_value, value_text)
            headline.grid_columnconfigure(0, weight=1)
            # Reserve enough room for 100% so percentage changes do not shift
            # the title. Dollar readouts like $2/200 are wider than 100%.
            headline.grid_columnconfigure(
                1,
                weight=0,
                minsize=value_width,
            )

            title_label = tk.Label(
                headline,
                text=dr.title,
                fg=PANEL_LABEL,
                bg=ui_bg,
                font=font_panel_title,
                anchor="w",
                justify="left",
            )
            title_label.grid(row=0, column=0, sticky="sw")

            value_label = tk.Label(
                headline,
                text=value_text,
                fg=value_color,
                bg=ui_bg,
                font=value_font,
                anchor="e",
                justify="right",
            )
            # Overlay the large value at bottom-right instead of letting its
            # 48pt line box stretch the title/reset rows vertically.
            value_label.place(
                relx=1.0,
                rely=1.0,
                anchor="se",
                y=-7,
                width=value_width,
            )

            meta_label = tk.Label(
                headline,
                text=meta_text,
                fg=PANEL_MUTED,
                bg=ui_bg,
                font=font_panel_meta,
                anchor="w",
                justify="left",
            )
            meta_label.grid(
                row=1,
                column=0,
                sticky="sw",
                pady=(2, 7),
            )

            if pct_text and row_opens_reset_ui(dr):
                reset_targets.append((value_label, reset_target_id(dr)))

            if pct_text:
                bar = tk.Canvas(
                    instrument,
                    height=metrics.bar_height,
                    bg=ui_bg,
                    bd=0,
                    highlightthickness=0,
                )
                bar.pack(fill="x")
                bar._usage_pct = float(dr.used_pct or 0)  # type: ignore[attr-defined]
                bar._usage_color = value_color  # type: ignore[attr-defined]
                bar.bind(
                    "<Configure>",
                    lambda event, canvas=bar: _paint_panel_bar(
                        canvas,
                        width=int(event.width),
                        height=int(event.height),
                    ),
                )
                bind_drag(bar)
            else:
                bar = None
                divider = tk.Frame(instrument, bg=PANEL_TRACK, height=1, bd=0)
                divider.pack(fill="x")
                bind_drag(divider)

            panel_items.append(
                {
                    "title": title_label,
                    "value": value_label,
                    "meta": meta_label,
                    "bar": bar,
                    "headline": headline,
                }
            )

            for widget in (tile, instrument, headline, title_label, value_label, meta_label):
                bind_drag(widget)

        bind_drag(lines_frame)
        bind_drag(body)
        state["panel_structure"] = structure
        state["panel_items"] = panel_items
        register_reset_click_targets(reset_targets)

    def render_lines() -> None:
        providers: list[ProviderUsage] = state["providers"]
        if state.get("panel_active"):
            _render_panel_lines(providers)
            return

        clear_lines()
        body.configure(padx=CARD_PADX, pady=3)
        cw = int(state["card_width"])
        content_w = max(120, cw - CARD_PADX * 2)
        row_h = int(state["row_height"])

        if not providers and state["refreshing"]:
            row = tk.Frame(lines_frame, bg=ui_bg)
            row.pack(fill="x", anchor="w")
            tk.Label(row, text="加载中…", fg=FG_MUTED, bg=ui_bg, font=font_main).pack(side="left")
            bind_drag(row)
            return
        if not providers:
            row = tk.Frame(lines_frame, bg=ui_bg)
            row.pack(fill="x", anchor="w")
            tk.Label(row, text="暂无数据", fg=FG_MUTED, bg=ui_bg, font=font_main).pack(side="left")
            bind_drag(row)
            return

        reset_targets: list[tuple[tk.Misc, str]] = []
        for dr in iter_display_rows(providers, shown_items()):
            # pack order: right first (natural width, never clipped), then left expands
            row = tk.Frame(lines_frame, bg=ui_bg, height=row_h)
            row.pack(fill="x", anchor="w", pady=0)
            row.pack_propagate(False)
            # force row to card content width
            row.configure(width=content_w)

            pct_t, time_t, extra = _right_text(dr)

            right_box = tk.Frame(row, bg=ui_bg)
            # Pack RIGHT first so usage is always fully visible
            right_box.pack(side="right", padx=(8, 0))

            if extra is not None and not pct_t:
                color = RED if dr.failed else FG
                s = tk.Label(
                    right_box,
                    text=extra,
                    fg=color,
                    bg=ui_bg,
                    font=font_main,
                    anchor="e",
                    width=10,
                )
                s.pack(side="right")
                bind_drag(s)
            else:
                # Right block sticks to window edge when resizing.
                # Columns L→R inside block: [ 27% right-aligned] [4h33m left-aligned]
                # Pack order is right-first: time (outer), then percent.
                t = tk.Label(
                    right_box,
                    text=time_t or "",
                    fg=FG_LABEL,
                    bg=ui_bg,
                    font=font_main,
                    anchor="w",
                    justify="left",
                    width=6,  # fits "5d16h"; left-aligned within column
                )
                t.pack(side="right")
                bind_drag(t)
                # percent column — fixed width, digits right-aligned as a column
                pct_lbl = tk.Label(
                    right_box,
                    text=pct_t or "",
                    fg=tone_color(dr.used_pct or 0) if pct_t else FG_MUTED,
                    bg=ui_bg,
                    font=font_main,
                    anchor="e",
                    justify="right",
                    width=max(4, len(pct_t or "")),
                )
                pct_lbl.pack(side="right", padx=(0, 4))
                bind_drag(pct_lbl)
                if pct_t and row_opens_reset_ui(dr):
                    reset_targets.append((pct_lbl, reset_target_id(dr)))

            left = tk.Label(
                row,
                text=dr.title,
                fg=FG_LABEL,
                bg=ui_bg,
                font=font_main,
                anchor="w",
                justify="left",
            )
            # Left fills remainder; long titles clip on the left side only
            left.pack(side="left", fill="x", expand=True)
            # Soft clip: limit left label pixel width so it never overlaps right
            try:
                right_box.update_idletasks()
                rw = max(right_box.winfo_reqwidth(), 1)
                left.configure(wraplength=0)
                # Use a clip via max width on the label using place... pack is enough if right is packed first
                max_left = max(40, content_w - rw - 4)
                # Tk Label doesn't clip by width easily; use a fixed width in characters approx
                # Measure and truncate with ellipsis if needed
                title = dr.title
                while title and font_main.measure(title) > max_left:
                    title = title[:-1]
                if title != dr.title and len(title) > 1:
                    title = title[:-1] + "…"
                left.configure(text=title)
            except Exception:
                pass

            bind_drag(left)
            bind_drag(right_box)
            bind_drag(row)

        bind_drag(lines_frame)
        bind_drag(body)
        register_reset_click_targets(reset_targets)

    def render_footer() -> None:
        def on_refresh() -> None:
            if state["refreshing"]:
                return
            refresh_async(force=True)

        # Timestamp and icon are one control with no visual plate of their own.
        # Transparent padding clicks land on bg_layer and are forwarded via the
        # matching hitbox, so the entire block remains interactive.
        refresh_control = state.get("refresh_control")
        control_exists = False
        try:
            control_exists = bool(
                refresh_control is not None and refresh_control.winfo_exists()
            )
        except Exception:
            control_exists = False

        control_options = {
            "text": _format_refresh_control(
                state["last_ok_at"], bool(state["refreshing"])
            ),
            "command": on_refresh,
            "fg": PANEL_MUTED if state.get("panel_active") else FG_MUTED,
            "bg": ui_bg,
            "activeforeground": PANEL_FG if state.get("panel_active") else FG_LABEL,
            "activebackground": ui_bg,
            "font": font_panel_footer if state.get("panel_active") else font_footer,
        }
        if control_exists:
            refresh_control.configure(**control_options)
        else:
            for widget in list(footer.winfo_children()):
                try:
                    widget.destroy()
                except Exception:
                    pass
            state["refresh_hitbox"] = None
            refresh_control = tk.Button(
                footer,
                **control_options,
                cursor="hand2",
                padx=0,
                pady=0,
                bd=0,
                highlightthickness=0,
                relief="flat",
                overrelief="flat",
                takefocus=False,
                anchor="w",
            )
            refresh_control.pack(side="left")
            refresh_control.bind("<Enter>", lambda _e: set_refresh_hover(True))
            refresh_control.bind("<Leave>", lambda _e: set_refresh_hover(False))
        state["refresh_control"] = refresh_control

        def update_refresh_hitbox() -> None:
            if state.get("refresh_control") is not refresh_control:
                return
            try:
                left = refresh_control.winfo_rootx() - root.winfo_rootx()
                top = refresh_control.winfo_rooty() - root.winfo_rooty()
                state["refresh_hitbox"] = (
                    left,
                    top,
                    left + refresh_control.winfo_width(),
                    top + refresh_control.winfo_height(),
                )
            except Exception:
                state["refresh_hitbox"] = None

        root.after_idle(update_refresh_hitbox)
        bind_drag(footer)
        if state["allow_resize"]:
            grip.lift()

    def content_min_height() -> int:
        """Minimum height so all rows + bottom footer fit without clipping.

        Prefer measured widget sizes after layout; fall back to row math.
        """
        n = max(
            1,
            len(iter_display_rows(state["providers"], shown_items()))
            if state["providers"]
            else 1,
        )
        rh = int(state["row_height"])
        try:
            footer_linespace = int(font_footer.metrics("linespace"))
        except Exception:
            footer_linespace = int(state["font_size"]) + 7
        footer_h = max(16, footer_linespace + 2)
        # body pady top+bottom (3*2) + compact grip slack
        estimate = 3 + 3 + n * rh + footer_h + 2

        measured = 0
        try:
            root.update_idletasks()
            lines_h = 0
            for child in lines_frame.winfo_children():
                lines_h += max(int(child.winfo_reqheight()), rh)
            foot_h = max(int(footer.winfo_reqheight()), footer_h)
            # padx doesn't affect height; pady=3 top+bottom on body
            measured = lines_h + foot_h + 6 + 2
        except Exception:
            measured = 0

        return max(CARD_HEIGHT_MIN, estimate, measured)

    def apply_geometry() -> None:
        try:
            root.update_idletasks()
            panel: MonitorInfo | None = state.get("panel_monitor")
            if state.get("panel_active") and panel is not None:
                geo = _tk_geometry(panel.width, panel.height, panel.x, panel.y)
                root.geometry(geo)
                enforce_native_panel_rect()
                # WM_DPICHANGED can arrive just after the first move; assert the
                # physical rectangle once more after that transition settles.
                schedule_native_panel_rect(120)
                grip.place_forget()
                return

            auto_h = content_min_height()

            w = max(CARD_WIDTH_MIN, min(CARD_WIDTH_MAX, int(state["card_width"])))
            state["card_width"] = w

            user_h = state.get("card_height")
            if user_h is not None:
                try:
                    user_h = int(user_h)
                except Exception:
                    user_h = None
            # Never allow a saved height shorter than content (fixes grok clip)
            if user_h is not None:
                h = max(auto_h, min(CARD_HEIGHT_MAX, user_h))
                if user_h < auto_h:
                    state["card_height"] = auto_h
                    cfg["card_height"] = auto_h
                    try:
                        save_config(cfg)
                    except Exception:
                        pass
                else:
                    state["card_height"] = h
            else:
                h = auto_h

            x, y = root.winfo_x(), root.winfo_y()
            geo = _tk_geometry(w, h, x, y)
            root.geometry(geo)
            if bg_layer is not None:
                try:
                    bg_layer.geometry(geo)
                    bg_layer.attributes("-topmost", bool(state["always_on_top"]))
                    # Keep both windows in the same topmost band and place the
                    # content immediately above its plate.  lower() could put
                    # the plate behind unrelated applications.
                    stack_text_above_plate()
                except Exception:
                    pass
            if state["allow_resize"]:
                grip.lift()
        except Exception:
            pass

    def render(*, relayout: bool = True) -> None:
        if state.get("rendering"):
            return
        state["rendering"] = True
        try:
            render_lines()
            render_footer()
            if relayout:
                apply_geometry()
            # Second measure after fonts/layout settle (fixes last-row clip)
            root.after_idle(_post_layout_fix)
        finally:
            state["rendering"] = False

    def _post_layout_fix() -> None:
        if state.get("rendering"):
            return
        if state.get("panel_active"):
            return
        try:
            auto_h = content_min_height()
            cur_h = int(root.winfo_height() or 0)
            if cur_h and cur_h < auto_h:
                state["card_height"] = max(int(state.get("card_height") or 0), auto_h)
                apply_geometry()
        except Exception:
            pass

    def schedule_render(delay_ms: int = 40) -> None:
        """Debounced full relayout — safe to call from Scale drag."""
        job = state.get("render_job")
        if job is not None:
            try:
                root.after_cancel(job)
            except Exception:
                pass
        state["render_job"] = root.after(delay_ms, _run_scheduled_render)

    def _run_scheduled_render() -> None:
        state["render_job"] = None
        render()

    def _runtime_monitor_signature(monitors: list[MonitorInfo]) -> tuple[Any, ...]:
        return tuple(
            (
                monitor.device,
                monitor.bounds,
                monitor.work_bounds,
                monitor.dpi_x,
                monitor.dpi_y,
                monitor.primary,
            )
            for monitor in monitors
        )

    def _set_runtime_dpi(dpi: int) -> None:
        dpi = max(72, int(dpi or 96))
        if state.get("tk_dpi") == dpi:
            return
        try:
            root.tk.call("tk", "scaling", dpi / 72.0)
            state["tk_dpi"] = dpi
            # Named fonts keep point sizes, but row metrics must be re-read at
            # the target monitor's pixel density.
            apply_font_size(int(state["font_size"]), persist=False, relayout=False)
        except Exception:
            pass

    def _restore_floating_geometry(monitors: list[MonitorInfo]) -> None:
        x = state.get("floating_x")
        y = state.get("floating_y")
        if x is None or y is None:
            primary = next((m for m in monitors if m.primary), monitors[0])
            x = primary.work_x + max(12, primary.work_width - int(state["card_width"]) - 24)
            y = primary.work_y + 36
        x, y = _clamp_position(
            int(x),
            int(y),
            int(state["card_width"]),
            120,
        )
        state["floating_x"], state["floating_y"] = x, y
        compact_geo = _tk_geometry(int(state["card_width"]), 120, x, y)
        root.geometry(compact_geo)
        if bg_layer is not None:
            try:
                bg_layer.geometry(compact_geo)
            except Exception:
                pass

    def _sync_display_runtime(
        monitors: list[MonitorInfo] | None = None,
        *,
        persist: bool = False,
    ) -> None:
        monitors = monitors or _enumerate_monitors()
        if not monitors:
            return
        state["monitor_signature"] = _runtime_monitor_signature(monitors)

        panel = None
        if state.get("display_mode") == DISPLAY_MODE_PANEL:
            panel = _choose_panel_monitor(
                monitors,
                state.get("monitor_device"),
                state.get("monitor_id"),
            )
            if panel is not None:
                state["monitor_device"] = panel.device
                state["monitor_id"] = panel.monitor_id or state.get("monitor_id")

        was_active = bool(state.get("panel_active"))
        state["panel_monitor"] = panel
        state["panel_active"] = panel is not None
        if panel is None and (
            state.get("wallpaper_active") or state.get("wallpaper_preparing")
        ):
            set_wallpaper_active(False)
        apply_mode_surface()

        if panel is not None:
            _set_runtime_dpi(panel.dpi_y)
            apply_geometry()
        else:
            floating_monitor = _monitor_at_point(
                monitors,
                int(state.get("floating_x") or 0),
                int(state.get("floating_y") or 0),
            ) or next((m for m in monitors if m.primary), monitors[0])
            _set_runtime_dpi(floating_monitor.dpi_y)
            if was_active or state.get("display_mode") == DISPLAY_MODE_PANEL:
                _restore_floating_geometry(monitors)

        set_allow_resize(bool(state["allow_resize"]), persist=False)
        cfg["display_mode"] = str(state["display_mode"])
        cfg["monitor_device"] = state.get("monitor_device")
        cfg["monitor_id"] = state.get("monitor_id")
        if persist:
            save_config(cfg)
        schedule_render(0)
        if panel is not None and not state.get("wallpaper_active"):
            _schedule_background_wallpaper_prepare()

    def set_display_mode(
        mode: str,
        monitor_device_value: str | None = None,
        *,
        persist: bool = True,
    ) -> None:
        mode = mode if mode in (DISPLAY_MODE_FLOAT, DISPLAY_MODE_PANEL) else DISPLAY_MODE_FLOAT
        if not state.get("panel_active"):
            state["floating_x"] = root.winfo_x()
            state["floating_y"] = root.winfo_y()
            cfg["x"] = state["floating_x"]
            cfg["y"] = state["floating_y"]
        state["display_mode"] = mode
        if monitor_device_value:
            state["monitor_device"] = monitor_device_value
            selected = next(
                (
                    monitor
                    for monitor in _enumerate_monitors()
                    if monitor.device.casefold() == monitor_device_value.casefold()
                ),
                None,
            )
            if selected is not None:
                state["monitor_id"] = selected.monitor_id or state.get("monitor_id")
        _sync_display_runtime(persist=persist)

    def poll_monitors() -> None:
        try:
            monitors = _enumerate_monitors()
            signature = _runtime_monitor_signature(monitors)
            if signature != state.get("monitor_signature"):
                _sync_display_runtime(monitors, persist=False)
        finally:
            root.after(MONITOR_POLL_MS, poll_monitors)

    def refresh_async(force: bool = False) -> None:
        if state["refreshing"]:
            return
        state["refreshing"] = True
        render_footer()

        def work() -> None:
            selected = active_providers(list(cfg.get("providers") or DEFAULT_PROVIDERS))
            try:
                providers = fetch_all(selected, force=force)
                root.after(0, lambda p=providers: apply_data(p))
            except Exception:
                root.after(
                    0,
                    lambda: apply_data(
                        [
                            ProviderUsage(
                                provider_id=pid,
                                display_name=pid,
                                available=False,
                                error="更新失败",
                            )
                            for pid in selected
                        ]
                    ),
                )

        threading.Thread(target=work, daemon=True).start()

    def apply_data(providers: list[ProviderUsage]) -> None:
        state["refreshing"] = False
        state["providers"] = providers
        # Mark successful overall refresh time if at least one provider is OK
        if any(p.available for p in providers):
            state["last_ok_at"] = time.time()
        elif state["last_ok_at"] is None:
            # still stamp attempt time so footer is not empty forever
            state["last_ok_at"] = time.time()
        # A data refresh does not change the dedicated panel's native bounds.
        # Avoid SetWindowPos(...SWP_SHOWWINDOW) on every refresh; it needlessly
        # invalidates the full-screen surface on mixed-DPI monitor layouts.
        render(relayout=not state.get("panel_active"))

    # Relative time ticker
    def tick_footer() -> None:
        if not state["refreshing"]:
            render_footer()
        root.after(15_000, tick_footer)

    # Match Windows shell context menu: Segoe UI ~9pt (system menu font)
    menu_font = tkfont.Font(family="Segoe UI", size=9)
    try:
        # Prefer real system menu font when available (Windows NONCLIENTMETRICS)
        import ctypes
        from ctypes import wintypes

        class LOGFONTW(ctypes.Structure):
            _fields_ = [
                ("lfHeight", wintypes.LONG),
                ("lfWidth", wintypes.LONG),
                ("lfEscapement", wintypes.LONG),
                ("lfOrientation", wintypes.LONG),
                ("lfWeight", wintypes.LONG),
                ("lfItalic", wintypes.BYTE),
                ("lfUnderline", wintypes.BYTE),
                ("lfStrikeOut", wintypes.BYTE),
                ("lfCharSet", wintypes.BYTE),
                ("lfOutPrecision", wintypes.BYTE),
                ("lfClipPrecision", wintypes.BYTE),
                ("lfQuality", wintypes.BYTE),
                ("lfPitchAndFamily", wintypes.BYTE),
                ("lfFaceName", wintypes.WCHAR * 32),
            ]

        class NONCLIENTMETRICSW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.UINT),
                ("iBorderWidth", ctypes.c_int),
                ("iScrollWidth", ctypes.c_int),
                ("iScrollHeight", ctypes.c_int),
                ("iCaptionWidth", ctypes.c_int),
                ("iCaptionHeight", ctypes.c_int),
                ("lfCaptionFont", LOGFONTW),
                ("iSmCaptionWidth", ctypes.c_int),
                ("iSmCaptionHeight", ctypes.c_int),
                ("lfSmCaptionFont", LOGFONTW),
                ("iMenuWidth", ctypes.c_int),
                ("iMenuHeight", ctypes.c_int),
                ("lfMenuFont", LOGFONTW),
                ("lfStatusFont", LOGFONTW),
                ("lfMessageFont", LOGFONTW),
                ("iPaddedBorderWidth", ctypes.c_int),
            ]

        SPI_GETNONCLIENTMETRICS = 0x0029
        ncm = NONCLIENTMETRICSW()
        ncm.cbSize = ctypes.sizeof(NONCLIENTMETRICSW)
        if ctypes.windll.user32.SystemParametersInfoW(
            SPI_GETNONCLIENTMETRICS, ncm.cbSize, ctypes.byref(ncm), 0
        ):
            face = ncm.lfMenuFont.lfFaceName or "Segoe UI"
            # lfHeight is often negative pixel height; convert approx to points
            h = abs(int(ncm.lfMenuFont.lfHeight)) or 12
            # points ≈ pixels * 72 / dpi; assume 96 dpi baseline if dpi unknown
            dpi = 96
            try:
                dpi = int(ctypes.windll.user32.GetDpiForSystem())
            except Exception:
                pass
            pts = max(8, min(12, round(h * 72 / dpi)))
            menu_font = tkfont.Font(family=face, size=pts)
    except Exception:
        pass

    menu = tk.Menu(root, tearoff=0, font=menu_font)

    def place_settings_on_primary(window: Any) -> None:
        """Map settings once at its final primary-monitor position."""
        try:
            window.update_idletasks()
            monitors = _enumerate_monitors()
            host = next((monitor for monitor in monitors if monitor.primary), monitors[0])
            width = window.winfo_reqwidth()
            height = window.winfo_reqheight()
            wx = host.work_x + max(12, (host.work_width - width) // 2)
            wy = host.work_y + max(12, (host.work_height - height) // 2)
            window.geometry(_tk_position(wx, wy))
        except Exception:
            pass

    def open_settings() -> None:
        existing = state.get("settings_win")
        if existing is not None:
            try:
                if existing.winfo_exists():
                    place_settings_on_primary(existing)
                    existing.deiconify()
                    existing.lift()
                    existing.focus_set()
                    return
            except Exception:
                pass

        win = tk.Toplevel(root)
        # Prevent the native wrapper from mapping for one frame at Windows'
        # default screen-edge position while the notebook calculates its size.
        win.withdraw()
        state["settings_win"] = win
        win.title("UsageFloat 设置")
        win.configure(bg=BG)
        win.resizable(True, True)
        win.minsize(460, 620)
        # This stays a normal independent window on the primary display. Setting
        # topmost/transient before its first map makes Tk recreate the native
        # wrapper across two DPI contexts; that loses the first menu invocation
        # and forces a full DWM recomposition when the dialog closes.

        shell = tk.Frame(win, bg=BG, padx=12, pady=10)
        shell.pack(fill="both", expand=True)

        style = ttk.Style(win)
        try:
            style.configure("UsageFloat.TNotebook", background=BG, borderwidth=0)
            style.configure(
                "UsageFloat.TNotebook.Tab",
                font=(font_family, 10),
                padding=(14, 6),
            )
        except Exception:
            pass
        notebook = ttk.Notebook(shell, style="UsageFloat.TNotebook", width=450, height=580)
        notebook.pack(fill="both", expand=True)
        providers_tab = tk.Frame(notebook, bg=BG)
        display_tab = tk.Frame(notebook, bg=BG)
        wallpaper_tab = tk.Frame(notebook, bg=BG)
        floating_tab = tk.Frame(notebook, bg=BG)
        notebook.add(providers_tab, text="用量")
        notebook.add(display_tab, text="副屏")
        notebook.add(wallpaper_tab, text="动态壁纸")
        notebook.add(floating_tab, text="悬浮窗")
        providers_pad = tk.Frame(providers_tab, bg=BG, padx=14, pady=12)
        providers_pad.pack(fill="both", expand=True)
        display_pad = tk.Frame(display_tab, bg=BG, padx=14, pady=12)
        display_pad.pack(fill="both", expand=True)
        floating_pad = tk.Frame(floating_tab, bg=BG, padx=14, pady=12)
        floating_pad.pack(fill="both", expand=True)

        wallpaper_notebook = ttk.Notebook(
            wallpaper_tab,
            style="UsageFloat.TNotebook",
        )
        wallpaper_notebook.pack(fill="both", expand=True, padx=10, pady=8)
        directories_tab = tk.Frame(wallpaper_notebook, bg=BG)
        weights_tab = tk.Frame(wallpaper_notebook, bg=BG)
        wallpaper_notebook.add(directories_tab, text="目录")
        wallpaper_notebook.add(weights_tab, text="随机权重")
        directories_pad = tk.Frame(directories_tab, bg=BG, padx=10, pady=10)
        directories_pad.pack(fill="both", expand=True)
        weights_pad = tk.Frame(weights_tab, bg=BG, padx=10, pady=10)
        weights_pad.pack(fill="both", expand=True)

        # Settings chrome stays at a stable size while the HUD font is previewed.
        settings_font = tkfont.Font(family=font_family, size=10)
        settings_font_small = tkfont.Font(family=font_family, size=9)
        # Tk widgets only keep the Tcl font name. Retain the Python objects so
        # their destructors do not delete those named fonts while the window is open.
        setattr(win, "_usage_float_fonts", (settings_font, settings_font_small))

        provider_order = list(cfg.get("provider_order") or DEFAULT_PROVIDERS)
        enabled_providers = list(cfg.get("providers") or DEFAULT_PROVIDERS)
        provider_drag = {"item": None, "moved": False}

        tk.Label(
            providers_pad,
            text="显示的用量",
            fg=FG,
            bg=BG,
            font=settings_font,
            anchor="w",
        ).pack(fill="x", pady=(0, 2))
        tk.Label(
            providers_pad,
            text="扫描本机登录态。勾选要显示的用量，按住行拖动可调整顺序。未登录的不会出现在面板上。",
            fg=FG_MUTED,
            bg=BG,
            font=settings_font_small,
            anchor="w",
            justify="left",
            wraplength=380,
        ).pack(fill="x", pady=(0, 6))

        provider_list_frame = tk.Frame(providers_pad, bg=BG)
        provider_list_frame.pack(fill="both", expand=True)
        provider_tree = ttk.Treeview(
            provider_list_frame,
            columns=("enabled", "name", "status"),
            show="headings",
            height=7,
            selectmode="browse",
        )
        provider_tree.heading("enabled", text="显示")
        provider_tree.heading("name", text="Provider", anchor="w")
        provider_tree.heading("status", text="状态")
        provider_tree.column("enabled", width=48, minwidth=48, stretch=False, anchor="center")
        provider_tree.column("name", width=210, minwidth=120, stretch=True, anchor="w")
        provider_tree.column("status", width=90, minwidth=72, stretch=False, anchor="center")
        provider_tree.tag_configure("disabled", foreground=FG_MUTED)
        provider_tree.pack(side="left", fill="both", expand=True)
        provider_scrollbar = tk.Scrollbar(provider_list_frame, command=provider_tree.yview)
        provider_scrollbar.pack(side="right", fill="y")
        provider_tree.configure(yscrollcommand=provider_scrollbar.set)

        provider_button_row = tk.Frame(providers_pad, bg=BG)
        provider_button_row.pack(fill="x", pady=(6, 4))
        provider_status = tk.Label(
            providers_pad,
            text="",
            fg=FG_MUTED,
            bg=BG,
            font=settings_font_small,
            anchor="w",
            justify="left",
            wraplength=380,
        )
        provider_status.pack(fill="x")

        def persist_providers(*, refresh: bool = True) -> None:
            order, enabled, disabled = resolve_provider_config(
                enabled_providers,
                [pid for pid in provider_order if pid not in set(enabled_providers)],
                provider_order,
            )
            provider_order[:] = order
            enabled_providers[:] = enabled
            cfg["provider_order"] = list(order)
            cfg["providers"] = list(enabled)
            cfg["disabled_providers"] = list(disabled)
            save_config(cfg)
            if refresh:
                refresh_async(force=True)

        def render_provider_tree(
            scanned: list[dict[str, Any]] | None = None,
            *,
            note: str = "",
        ) -> None:
            if scanned is None:
                scanned = scan_providers(enabled_providers, provider_order)
            by_id = {row["id"]: row for row in scanned}
            provider_tree.delete(*provider_tree.get_children())
            enabled_set = set(enabled_providers)
            for pid in provider_order:
                row = by_id.get(pid) or {
                    "id": pid,
                    "label": provider_label(pid),
                    "available": provider_available(pid),
                }
                checked = pid in enabled_set
                provider_tree.insert(
                    "",
                    "end",
                    iid=pid,
                    values=(
                        "☑" if checked else "☐",
                        row.get("label") or provider_label(pid),
                        "已登录" if row.get("available") else "未检测到",
                    ),
                    tags=(() if checked else ("disabled",)),
                )
            available_count = sum(1 for row in scanned if row.get("available"))
            text = (
                f"已登录 {available_count} / 共 {len(provider_order)}"
                f" · 显示 {len(enabled_providers)}"
            )
            if note:
                text = f"{text} · {note}"
            provider_status.configure(text=text)

        def toggle_provider(pid: str) -> None:
            if not pid:
                return
            enabled_set = set(enabled_providers)
            if pid in enabled_set:
                enabled_set.remove(pid)
            else:
                enabled_set.add(pid)
            enabled_providers[:] = [item for item in provider_order if item in enabled_set]
            persist_providers()
            render_provider_tree()

        def on_provider_press(event: Any) -> str | None:
            if provider_tree.identify_region(event.x, event.y) != "cell":
                provider_drag["item"] = None
                return None
            item = provider_tree.identify_row(event.y)
            if not item:
                provider_drag["item"] = None
                return None
            if provider_tree.identify_column(event.x) == "#1":
                provider_drag["item"] = None
                toggle_provider(item)
                return "break"
            provider_drag["item"] = item
            provider_drag["moved"] = False
            provider_tree.selection_set(item)
            return None

        def on_provider_motion(event: Any) -> str | None:
            item = provider_drag["item"]
            if not item:
                return None
            target = provider_tree.identify_row(event.y)
            if target and target != item:
                provider_tree.move(item, "", provider_tree.index(target))
                provider_drag["moved"] = True
            return "break"

        def on_provider_release(_event: Any) -> None:
            if provider_drag["moved"]:
                provider_order[:] = list(provider_tree.get_children())
                enabled_set = set(enabled_providers)
                enabled_providers[:] = [
                    pid for pid in provider_order if pid in enabled_set
                ]
                persist_providers()
                render_provider_tree()
            provider_drag["item"] = None
            provider_drag["moved"] = False

        def on_provider_space(_event: Any) -> str:
            selection = provider_tree.selection()
            if selection:
                toggle_provider(selection[0])
            return "break"

        def on_scan_providers() -> None:
            scanned = scan_providers(enabled_providers, provider_order)
            changed = False
            for row in scanned:
                pid = str(row.get("id") or "")
                if not pid or pid in provider_order:
                    continue
                provider_order.append(pid)
                if pid not in enabled_providers:
                    enabled_providers.append(pid)
                changed = True
            if changed:
                persist_providers(refresh=True)
            else:
                refresh_async(force=True)
            render_provider_tree(scanned, note="已扫描")

        provider_tree.bind("<ButtonPress-1>", on_provider_press)
        provider_tree.bind("<B1-Motion>", on_provider_motion)
        provider_tree.bind("<ButtonRelease-1>", on_provider_release)
        provider_tree.bind("<space>", on_provider_space)
        tk.Button(
            provider_button_row,
            text="扫描 provider",
            command=on_scan_providers,
            bg="#f0f0f0",
            fg=FG,
            relief="flat",
            padx=10,
            pady=3,
            font=settings_font,
        ).pack(side="left")
        render_provider_tree()

        tk.Label(
            providers_pad,
            text="额度重置时区",
            fg=FG,
            bg=BG,
            font=settings_font,
            anchor="w",
        ).pack(fill="x", pady=(10, 2))
        tk.Label(
            providers_pad,
            text="用于 LLM Proxy 的重置倒计时。默认 UTC+8，也可填写 UTC、UTC+9 或 IANA 名称。",
            fg=FG_MUTED,
            bg=BG,
            font=settings_font_small,
            anchor="w",
            justify="left",
            wraplength=380,
        ).pack(fill="x", pady=(0, 4))
        timezone_var = tk.StringVar(
            value=_normalize_llm_proxy_timezone(
                state.get("llm_proxy_timezone") or cfg.get("llm_proxy_timezone")
            )
        )

        def persist_timezone(_event: Any = None) -> None:
            normalized = _normalize_llm_proxy_timezone(timezone_var.get())
            timezone_var.set(normalized)
            if normalized == state.get("llm_proxy_timezone"):
                return
            state["llm_proxy_timezone"] = normalized
            cfg["llm_proxy_timezone"] = normalized
            save_config(cfg)
            refresh_async(force=True)

        timezone_combo = ttk.Combobox(
            providers_pad,
            textvariable=timezone_var,
            values=("UTC+8", "UTC", "UTC+9", "UTC-5", "UTC-8"),
            font=settings_font,
        )
        timezone_combo.pack(fill="x")
        timezone_combo.bind("<<ComboboxSelected>>", persist_timezone)
        timezone_combo.bind("<Return>", persist_timezone)
        timezone_combo.bind("<FocusOut>", persist_timezone)

        settings_monitors = _enumerate_monitors()
        monitor_by_label = {_monitor_label(monitor): monitor for monitor in settings_monitors}
        selected_monitor = _choose_panel_monitor(
            settings_monitors,
            state.get("monitor_device"),
            state.get("monitor_id"),
        )
        if selected_monitor is None:
            selected_monitor = _choose_panel_monitor(settings_monitors, None)
        selected_monitor_label = (
            _monitor_label(selected_monitor) if selected_monitor is not None else "未检测到显示器"
        )

        tk.Label(
            display_pad,
            text="显示位置",
            fg=FG,
            bg=BG,
            font=settings_font,
            anchor="w",
        ).pack(fill="x", pady=(0, 4))

        monitor_var = tk.StringVar(value=selected_monitor_label)

        def selected_device() -> str | None:
            selected = monitor_by_label.get(monitor_var.get())
            return selected.device if selected is not None else None

        def place_settings_window() -> None:
            place_settings_on_primary(win)

        panel_mode_var = tk.BooleanVar(
            value=state.get("display_mode") == DISPLAY_MODE_PANEL
        )

        def update_panel_status() -> None:
            active = bool(state.get("panel_active"))
            requested = bool(panel_mode_var.get())
            if requested and active:
                panel_status.configure(text="已铺满所选副屏；右键可随时打开菜单。")
            elif requested:
                panel_status.configure(text="所选副屏未连接，当前临时显示为悬浮窗。")
            else:
                panel_status.configure(text="开启后铺满所选屏幕；拔下时自动回到悬浮窗。")
            try:
                scale.configure(state="disabled" if requested else "normal")
                size_label.configure(text="自动" if requested else str(size_var.get()))
                chk.configure(state="disabled" if requested else "normal")
                opacity_state = "disabled" if requested else "normal"
                bg_op_scale.configure(state=opacity_state)
                tx_op_scale.configure(state=opacity_state)
                bg_op_lbl.configure(text="固定黑底" if requested else f"{bg_op_var.get()}%")
                tx_op_lbl.configure(text="固定清晰" if requested else f"{tx_op_var.get()}%")
                opacity_hint.configure(
                    text=(
                        "专用副屏使用不透明黑底和清晰文字；透明度设置仅用于悬浮窗。"
                        if requested
                        else "拖动即实时预览。数值越高越不透明，底板与文字互不影响。"
                    )
                )
            except Exception:
                pass

        def on_panel_mode_toggle() -> None:
            requested = bool(panel_mode_var.get())
            device = selected_device()
            if requested and device is None:
                fallback = _choose_panel_monitor(_enumerate_monitors(), None)
                device = fallback.device if fallback is not None else None
            set_display_mode(
                DISPLAY_MODE_PANEL if requested else DISPLAY_MODE_FLOAT,
                device,
                persist=True,
            )
            update_panel_status()
            win.after_idle(place_settings_window)

        panel_checkbox = tk.Checkbutton(
            display_pad,
            text="专用副屏模式（整屏仪表盘）",
            variable=panel_mode_var,
            command=on_panel_mode_toggle,
            bg=BG,
            fg=FG,
            activebackground=BG,
            activeforeground=FG,
            selectcolor=BG,
            font=settings_font,
            anchor="w",
        )
        panel_checkbox.pack(fill="x", pady=(0, 4))

        def on_monitor_selected(label: str) -> None:
            monitor = monitor_by_label.get(label)
            if monitor is None:
                return
            state["monitor_device"] = monitor.device
            state["monitor_id"] = monitor.monitor_id or state.get("monitor_id")
            cfg["monitor_device"] = monitor.device
            cfg["monitor_id"] = state.get("monitor_id")
            if panel_mode_var.get():
                set_display_mode(DISPLAY_MODE_PANEL, monitor.device, persist=True)
            else:
                save_config(cfg)
            update_panel_status()
            win.after_idle(place_settings_window)

        monitor_menu = tk.OptionMenu(
            display_pad,
            monitor_var,
            *monitor_by_label.keys(),
            command=on_monitor_selected,
        )
        monitor_menu.configure(
            bg="#f3f4f5",
            fg=FG,
            activebackground="#e8eaec",
            activeforeground=FG,
            font=settings_font_small,
            relief="flat",
            bd=0,
            highlightthickness=0,
            anchor="w",
        )
        monitor_menu["menu"].configure(font=settings_font_small)
        monitor_menu.pack(fill="x", pady=(0, 4))

        panel_status = tk.Label(
            display_pad,
            text="",
            fg=FG_MUTED,
            bg=BG,
            font=settings_font_small,
            anchor="w",
            justify="left",
            wraplength=300,
        )
        panel_status.pack(fill="x", pady=(0, 10))

        key_labels = (("Ctrl", "ctrl"), ("Shift", "shift"), ("Alt", "alt"), ("Win", "win"))
        key_label_to_id = {label: key for label, key in key_labels}
        key_id_to_label = {key: label for label, key in key_labels}
        shortcut_enabled_var = tk.BooleanVar(value=bool(state.get("shortcut_enabled", True)))
        shortcut_mode_var = tk.StringVar(
            value=_normalize_shortcut_mode(state.get("shortcut_mode"))
        )
        shortcut_key_var = tk.StringVar(
            value=key_id_to_label.get(
                _normalize_shortcut_key(state.get("shortcut_key")), "Ctrl"
            )
        )
        shortcut_count_var = tk.StringVar(
            value=str(_normalize_shortcut_repeat_count(state.get("shortcut_repeat_count")))
        )
        shortcut_combo_keys = list(
            state.get("shortcut_combo") or SHORTCUT_COMBO_DEFAULT
        )
        shortcut_combo_var = tk.StringVar(
            value=_format_shortcut_combo(shortcut_combo_keys)
        )

        def persist_shortcut_from_ui() -> None:
            apply_shortcut_config(
                enabled=bool(shortcut_enabled_var.get()),
                mode=shortcut_mode_var.get(),
                key=key_label_to_id.get(shortcut_key_var.get(), "ctrl"),
                repeat_count=_normalize_shortcut_repeat_count(shortcut_count_var.get()),
                combo=shortcut_combo_keys,
                persist=True,
            )
            shortcut_count_var.set(str(state["shortcut_repeat_count"]))
            shortcut_combo_var.set(_format_shortcut_combo(state["shortcut_combo"]))
            update_shortcut_controls()

        def update_shortcut_controls() -> None:
            enabled = bool(shortcut_enabled_var.get())
            mode = _normalize_shortcut_mode(shortcut_mode_var.get())
            repeat_state = "normal" if enabled and mode == SHORTCUT_MODE_REPEAT else "disabled"
            combo_state = "normal" if enabled and mode == SHORTCUT_MODE_COMBO else "disabled"
            try:
                for widget in shortcut_repeat_widgets:
                    widget.configure(state=repeat_state)
                for widget in shortcut_combo_widgets:
                    widget.configure(state=combo_state)
                for widget in shortcut_mode_widgets:
                    widget.configure(state="normal" if enabled else "disabled")
            except Exception:
                pass

        tk.Label(
            display_pad,
            text="快捷键",
            fg=FG,
            bg=BG,
            font=settings_font,
            anchor="w",
        ).pack(fill="x", pady=(0, 2))
        tk.Checkbutton(
            display_pad,
            text="启用快捷键，切换用量 / 动态壁纸",
            variable=shortcut_enabled_var,
            command=persist_shortcut_from_ui,
            bg=BG,
            fg=FG,
            activebackground=BG,
            activeforeground=FG,
            selectcolor=BG,
            font=settings_font,
            anchor="w",
        ).pack(fill="x")

        shortcut_mode_widgets: list[Any] = []
        shortcut_repeat_widgets: list[Any] = []
        shortcut_combo_widgets: list[Any] = []

        mode_row = tk.Frame(display_pad, bg=BG)
        mode_row.pack(fill="x", pady=(4, 2))
        repeat_radio = tk.Radiobutton(
            mode_row,
            text="连按",
            value=SHORTCUT_MODE_REPEAT,
            variable=shortcut_mode_var,
            command=persist_shortcut_from_ui,
            bg=BG,
            fg=FG,
            activebackground=BG,
            activeforeground=FG,
            selectcolor=BG,
            font=settings_font_small,
            anchor="w",
        )
        repeat_radio.pack(side="left")
        combo_radio = tk.Radiobutton(
            mode_row,
            text="组合键",
            value=SHORTCUT_MODE_COMBO,
            variable=shortcut_mode_var,
            command=persist_shortcut_from_ui,
            bg=BG,
            fg=FG,
            activebackground=BG,
            activeforeground=FG,
            selectcolor=BG,
            font=settings_font_small,
            anchor="w",
        )
        combo_radio.pack(side="left", padx=(12, 0))
        shortcut_mode_widgets.extend((repeat_radio, combo_radio))

        repeat_row = tk.Frame(display_pad, bg=BG)
        repeat_row.pack(fill="x", pady=(0, 4))
        tk.Label(
            repeat_row,
            text="按键",
            fg=FG,
            bg=BG,
            font=settings_font_small,
        ).pack(side="left")
        key_menu = tk.OptionMenu(
            repeat_row,
            shortcut_key_var,
            *[label for label, _key in key_labels],
            command=lambda _value: persist_shortcut_from_ui(),
        )
        key_menu.configure(
            bg="#f3f4f5",
            fg=FG,
            activebackground="#e8eaec",
            font=settings_font_small,
            relief="flat",
            bd=0,
            highlightthickness=0,
        )
        key_menu.pack(side="left", padx=(6, 12))
        tk.Label(
            repeat_row,
            text="次数",
            fg=FG,
            bg=BG,
            font=settings_font_small,
        ).pack(side="left")
        count_spin = tk.Spinbox(
            repeat_row,
            from_=SHORTCUT_REPEAT_COUNT_MIN,
            to=SHORTCUT_REPEAT_COUNT_MAX,
            width=3,
            textvariable=shortcut_count_var,
            command=persist_shortcut_from_ui,
            font=settings_font_small,
            justify="right",
        )
        count_spin.pack(side="left", padx=(6, 0))
        count_spin.bind("<FocusOut>", lambda _event: persist_shortcut_from_ui())
        count_spin.bind("<Return>", lambda _event: persist_shortcut_from_ui())
        shortcut_repeat_widgets.extend((key_menu, count_spin))

        combo_row = tk.Frame(display_pad, bg=BG)
        combo_row.pack(fill="x", pady=(0, 2))
        combo_label = tk.Label(
            combo_row,
            textvariable=shortcut_combo_var,
            fg=FG,
            bg="#f3f4f5",
            font=settings_font_small,
            anchor="w",
            padx=8,
            pady=3,
        )
        combo_label.pack(side="left", fill="x", expand=True)

        def start_combo_capture() -> None:
            if get_async_key_state is None:
                shortcut_combo_var.set("当前系统无法录制快捷键")
                return
            state["shortcut_capture"] = True
            shortcut_combo_var.set("请按下组合键…")
            seen: list[str] = []

            def finish(keys: list[str]) -> None:
                state["shortcut_capture"] = False
                if len(keys) >= 2:
                    shortcut_combo_keys[:] = _normalize_shortcut_combo(keys)
                    persist_shortcut_from_ui()
                else:
                    shortcut_combo_var.set(_format_shortcut_combo(shortcut_combo_keys))

            def poll_capture(remaining: int) -> None:
                if not state.get("shortcut_capture"):
                    return
                down = _shortcut_keys_currently_down(get_async_key_state)
                if down:
                    seen[:] = down
                elif seen:
                    finish(list(seen))
                    return
                if remaining <= 0:
                    finish(list(seen))
                    return
                win.after(40, lambda: poll_capture(remaining - 1))

            win.after(200, lambda: poll_capture(150))

        capture_btn = tk.Button(
            combo_row,
            text="设置",
            command=start_combo_capture,
            bg="#f0f0f0",
            fg=FG,
            relief="flat",
            padx=8,
            pady=2,
            font=settings_font_small,
        )
        capture_btn.pack(side="left", padx=(6, 0))
        shortcut_combo_widgets.extend((combo_label, capture_btn))

        tk.Label(
            display_pad,
            text="连按默认两次 Ctrl；组合键按下后立即切换。仅专用副屏模式生效。Ctrl+C 等不会触发连按。",
            fg=FG_MUTED,
            bg=BG,
            font=settings_font_small,
            anchor="w",
            wraplength=380,
            justify="left",
        ).pack(fill="x", pady=(0, 8))
        update_shortcut_controls()

        wallpaper_folders = list(state.get("wallpaper_folders") or [])
        wallpaper_disabled_folders = list(
            state.get("wallpaper_disabled_folders") or []
        )
        wallpaper_image_seconds_var = tk.StringVar(
            value=str(int(state["wallpaper_image_seconds"]))
        )
        wallpaper_audio_var = tk.BooleanVar(
            value=bool(state.get("wallpaper_audio", True))
        )
        wallpaper_random_mode_var = tk.StringVar(
            value=_normalize_wallpaper_random_mode(
                state.get("wallpaper_random_mode")
            )
        )
        settings_media_files: list[Path] = []
        weight_vars: dict[str, tk.StringVar] = {}

        def apply_wallpaper_random_mode() -> None:
            mode = _normalize_wallpaper_random_mode(wallpaper_random_mode_var.get())
            wallpaper_random_mode_var.set(mode)
            changed = mode != state.get("wallpaper_random_mode")
            state["wallpaper_random_mode"] = mode
            cfg["wallpaper_random_mode"] = mode
            save_config(cfg)
            if not changed:
                return
            if state.get("wallpaper_active"):
                set_wallpaper_active(False)
                set_wallpaper_active(True)
            else:
                _clear_wallpaper_state()
                _schedule_background_wallpaper_prepare()

        tk.Label(
            directories_pad,
            text="媒体目录",
            fg=FG,
            bg=BG,
            font=settings_font,
            anchor="w",
        ).pack(fill="x", pady=(0, 2))
        tk.Label(
            directories_pad,
            text="可添加多个目录；取消勾选会暂停使用该目录，但不会删除目录或权重。",
            fg=FG_MUTED,
            bg=BG,
            font=settings_font_small,
            anchor="w",
            justify="left",
            wraplength=360,
        ).pack(fill="x", pady=(0, 6))

        folder_list_frame = tk.Frame(directories_pad, bg=BG)
        folder_list_frame.pack(fill="x")
        folder_tree = ttk.Treeview(
            folder_list_frame,
            columns=("enabled", "path", "media"),
            show="headings",
            height=7,
            selectmode="extended",
        )
        folder_tree.heading("enabled", text="启用")
        folder_tree.heading("path", text="目录", anchor="w")
        folder_tree.heading("media", text="媒体")
        folder_tree.column("enabled", width=48, minwidth=48, stretch=False, anchor="center")
        folder_tree.column("path", width=284, minwidth=150, stretch=True, anchor="w")
        folder_tree.column("media", width=52, minwidth=52, stretch=False, anchor="center")
        folder_tree.tag_configure("disabled", foreground=FG_MUTED)
        folder_tree.pack(side="left", fill="both", expand=True)
        folder_scrollbar = tk.Scrollbar(folder_list_frame, command=folder_tree.yview)
        folder_scrollbar.pack(side="right", fill="y")
        folder_tree.configure(yscrollcommand=folder_scrollbar.set)

        folder_button_row = tk.Frame(directories_pad, bg=BG)
        folder_button_row.pack(fill="x", pady=(6, 4))
        folder_status = tk.Label(
            directories_pad,
            text="",
            fg=FG_MUTED,
            bg=BG,
            font=settings_font_small,
            anchor="w",
            justify="left",
            wraplength=360,
        )
        folder_status.pack(fill="x", pady=(0, 8))

        tk.Label(
            weights_pad,
            text="随机范围",
            fg=FG,
            bg=BG,
            font=settings_font,
            anchor="w",
        ).pack(fill="x", pady=(0, 2))
        for mode, label in (
            (
                WALLPAPER_RANDOM_MODE_DIRECTORY,
                "单目录随机：选中目录后，本轮只在其中播放",
            ),
            (
                WALLPAPER_RANDOM_MODE_GLOBAL,
                "每次都在全目录中随机",
            ),
        ):
            tk.Radiobutton(
                weights_pad,
                text=label,
                value=mode,
                variable=wallpaper_random_mode_var,
                command=apply_wallpaper_random_mode,
                fg=FG,
                bg=BG,
                activebackground=BG,
                activeforeground=FG,
                selectcolor=BG,
                font=settings_font_small,
                anchor="w",
            ).pack(fill="x", pady=1)

        tk.Label(
            weights_pad,
            text="目录权重",
            fg=FG,
            bg=BG,
            font=settings_font,
            anchor="w",
        ).pack(fill="x", pady=(7, 2))
        tk.Label(
            weights_pad,
            text="用于选择本轮的首个目录。10 为默认，20 约为 10 的两倍，0 表示禁用首选。",
            fg=FG_MUTED,
            bg=BG,
            font=settings_font_small,
            anchor="w",
            justify="left",
            wraplength=360,
        ).pack(fill="x", pady=(0, 6))

        weight_list_frame = tk.Frame(weights_pad, bg=BG)
        weight_list_frame.pack(fill="both", expand=True)
        weight_canvas = tk.Canvas(
            weight_list_frame,
            bg=BG,
            highlightthickness=0,
            bd=0,
            height=270,
        )
        weight_scrollbar = tk.Scrollbar(weight_list_frame, command=weight_canvas.yview)
        weight_scrollbar.pack(side="right", fill="y")
        weight_canvas.pack(side="left", fill="both", expand=True)
        weight_canvas.configure(yscrollcommand=weight_scrollbar.set)
        weight_rows = tk.Frame(weight_canvas, bg=BG)
        weight_canvas_window = weight_canvas.create_window((0, 0), window=weight_rows, anchor="nw")
        weight_rows.bind(
            "<Configure>",
            lambda _event: weight_canvas.configure(scrollregion=weight_canvas.bbox("all")),
        )
        weight_canvas.bind(
            "<Configure>",
            lambda event: weight_canvas.itemconfigure(weight_canvas_window, width=event.width),
        )

        def scroll_weight_list(event: Any) -> str:
            delta = int(getattr(event, "delta", 0) or 0)
            if delta:
                steps = -int(delta / 120)
                if steps == 0:
                    steps = -1 if delta > 0 else 1
                weight_canvas.yview_scroll(steps, "units")
            return "break"

        def bind_weight_list_wheel(widget: Any) -> None:
            widget.bind("<MouseWheel>", scroll_weight_list)
            for child in widget.winfo_children():
                bind_weight_list_wheel(child)

        bind_weight_list_wheel(weight_list_frame)

        def restart_wallpaper_preparation() -> None:
            if state.get("wallpaper_active"):
                set_wallpaper_active(False)
                set_wallpaper_active(True)
            else:
                _clear_wallpaper_state()
                _schedule_background_wallpaper_prepare()

        def persist_wallpaper_folders() -> None:
            normalized = _normalize_wallpaper_folders(wallpaper_folders)
            wallpaper_folders[:] = normalized
            disabled = _normalize_wallpaper_disabled_folders(
                wallpaper_disabled_folders,
                normalized,
            )
            wallpaper_disabled_folders[:] = disabled
            active_directory_keys = {
                _wallpaper_path_key(directory)
                for directory, _count in _wallpaper_media_directories(
                    _collect_wallpaper_media_folders(normalized)
                )
            }
            weights = {
                key: weight
                for key, weight in _normalize_wallpaper_weights(
                    state.get("wallpaper_subdir_weights")
                ).items()
                if key in active_directory_keys
            }
            state["wallpaper_folders"] = list(normalized)
            state["wallpaper_folder"] = normalized[0] if normalized else None
            state["wallpaper_disabled_folders"] = list(disabled)
            state["wallpaper_subdir_weights"] = weights
            cfg["wallpaper_folders"] = list(normalized)
            cfg["wallpaper_folder"] = normalized[0] if normalized else None
            cfg["wallpaper_disabled_folders"] = list(disabled)
            cfg["wallpaper_subdir_weights"] = weights
            save_config(cfg)

        def weight_directory_label(directory: Path) -> str:
            candidates: list[tuple[int, Path, Path]] = []
            for root_text in wallpaper_folders:
                root_path = Path(root_text)
                try:
                    relative = directory.relative_to(root_path)
                except ValueError:
                    continue
                candidates.append((len(root_path.parts), root_path, relative))
            if not candidates:
                return str(directory)
            _, root_path, relative = max(candidates, key=lambda item: item[0])
            root_name = root_path.name or str(root_path)
            return f"{root_name} /（根目录）" if not relative.parts else f"{root_name} / {relative}"

        def apply_directory_weight(key: str, variable: tk.StringVar) -> None:
            weights = _merge_wallpaper_weight_values(
                state.get("wallpaper_subdir_weights"),
                {key: variable.get()},
            )
            weight = weights.get(key, WALLPAPER_DIR_WEIGHT_DEFAULT)
            variable.set(str(weight))
            state["wallpaper_subdir_weights"] = weights
            cfg["wallpaper_subdir_weights"] = weights
            save_config(cfg)
            if not state.get("wallpaper_active"):
                _clear_wallpaper_state()
                _schedule_background_wallpaper_prepare()

        def commit_all_directory_weights() -> None:
            previous = _normalize_wallpaper_weights(
                state.get("wallpaper_subdir_weights")
            )
            weights = _merge_wallpaper_weight_values(
                previous,
                {key: variable.get() for key, variable in weight_vars.items()},
            )
            for key, variable in weight_vars.items():
                variable.set(str(weights.get(key, WALLPAPER_DIR_WEIGHT_DEFAULT)))
            state["wallpaper_subdir_weights"] = weights
            cfg["wallpaper_subdir_weights"] = weights
            if weights != previous and not state.get("wallpaper_active"):
                _clear_wallpaper_state()
                _schedule_background_wallpaper_prepare()

        def refresh_weight_rows() -> None:
            for child in weight_rows.winfo_children():
                child.destroy()
            weight_vars.clear()
            directories = _wallpaper_media_directories(settings_media_files)
            if not directories:
                tk.Label(
                    weight_rows,
                    text="添加含图片或视频的目录后，这里会显示可配置的子目录。",
                    fg=FG_MUTED,
                    bg=BG,
                    font=settings_font_small,
                    anchor="w",
                    justify="left",
                    wraplength=330,
                ).pack(fill="x", pady=8)
                return
            weights = dict(state.get("wallpaper_subdir_weights") or {})
            for directory, media_count in directories:
                key = _wallpaper_path_key(directory)
                row = tk.Frame(weight_rows, bg=BG)
                row.pack(fill="x", pady=2)
                row.grid_columnconfigure(0, weight=1)
                label_frame = tk.Frame(row, bg=BG)
                label_frame.grid(row=0, column=0, sticky="ew")
                tk.Label(
                    label_frame,
                    text=weight_directory_label(directory),
                    fg=FG,
                    bg=BG,
                    font=settings_font_small,
                    anchor="w",
                    justify="left",
                    wraplength=270,
                ).pack(fill="x")
                tk.Label(
                    label_frame,
                    text=f"{media_count} 个媒体",
                    fg=FG_MUTED,
                    bg=BG,
                    font=settings_font_small,
                    anchor="w",
                ).pack(fill="x")
                variable = tk.StringVar(
                    value=str(weights.get(key, WALLPAPER_DIR_WEIGHT_DEFAULT))
                )
                weight_vars[key] = variable
                spinbox = tk.Spinbox(
                    row,
                    from_=WALLPAPER_DIR_WEIGHT_MIN,
                    to=WALLPAPER_DIR_WEIGHT_MAX,
                    width=4,
                    textvariable=variable,
                    command=lambda k=key, v=variable: apply_directory_weight(k, v),
                    font=settings_font,
                    justify="right",
                )
                spinbox.grid(row=0, column=1, sticky="ne", padx=(8, 2), pady=2)
                spinbox.bind(
                    "<FocusOut>",
                    lambda _event, k=key, v=variable: apply_directory_weight(k, v),
                )
                spinbox.bind(
                    "<Return>",
                    lambda _event, k=key, v=variable: apply_directory_weight(k, v),
                )
            bind_weight_list_wheel(weight_rows)

        def refresh_wallpaper_directory_ui() -> None:
            nonlocal settings_media_files
            folder_tree.delete(*folder_tree.get_children())
            disabled_keys = {
                _wallpaper_path_key(folder)
                for folder in wallpaper_disabled_folders
            }
            for index, folder in enumerate(wallpaper_folders):
                files = _collect_wallpaper_media(folder)
                enabled = _wallpaper_path_key(folder) not in disabled_keys
                folder_tree.insert(
                    "",
                    "end",
                    iid=f"folder-{index}",
                    values=("☑" if enabled else "☐", folder, len(files)),
                    tags=(() if enabled else ("disabled",)),
                )
            enabled_folders = _enabled_wallpaper_folders(
                wallpaper_folders,
                wallpaper_disabled_folders,
            )
            settings_media_files = _collect_wallpaper_media_folders(enabled_folders)
            executable = _find_mpv_executable(cfg.get("mpv_path"))
            engine_text = "mpv 已就绪" if executable is not None else "mpv 未安装"
            folder_status.configure(
                text=(
                    f"启用 {len(enabled_folders)} / 共 {len(wallpaper_folders)} 个目录"
                    f" · {len(settings_media_files)} 个媒体 · {engine_text}"
                )
            )
            refresh_weight_rows()

        def toggle_wallpaper_folder_indices(indices: list[int]) -> None:
            if not indices:
                return
            disabled_keys = {
                _wallpaper_path_key(folder)
                for folder in wallpaper_disabled_folders
            }
            for index in indices:
                if not 0 <= index < len(wallpaper_folders):
                    continue
                folder = wallpaper_folders[index]
                key = _wallpaper_path_key(folder)
                if key in disabled_keys:
                    disabled_keys.remove(key)
                else:
                    disabled_keys.add(key)
            wallpaper_disabled_folders[:] = [
                folder
                for folder in wallpaper_folders
                if _wallpaper_path_key(folder) in disabled_keys
            ]
            state["wallpaper_disabled_folders"] = list(wallpaper_disabled_folders)
            cfg["wallpaper_disabled_folders"] = list(wallpaper_disabled_folders)
            save_config(cfg)
            refresh_wallpaper_directory_ui()
            restart_wallpaper_preparation()

        def folder_tree_index(item: str) -> int | None:
            try:
                prefix, raw_index = item.rsplit("-", 1)
                if prefix != "folder":
                    return None
                return int(raw_index)
            except (TypeError, ValueError):
                return None

        def on_folder_tree_click(event: Any) -> str | None:
            if folder_tree.identify_region(event.x, event.y) != "cell":
                return None
            if folder_tree.identify_column(event.x) != "#1":
                return None
            index = folder_tree_index(folder_tree.identify_row(event.y))
            if index is None:
                return None
            toggle_wallpaper_folder_indices([index])
            return "break"

        def on_folder_tree_space(_event: Any) -> str:
            indices = [
                index
                for item in folder_tree.selection()
                if (index := folder_tree_index(item)) is not None
            ]
            toggle_wallpaper_folder_indices(indices)
            return "break"

        folder_tree.bind("<Button-1>", on_folder_tree_click)
        folder_tree.bind("<space>", on_folder_tree_space)

        def add_wallpaper_folder() -> None:
            current = wallpaper_folders[-1] if wallpaper_folders else ""
            initial = current if current and Path(current).is_dir() else str(HOME / "Pictures")
            selected = filedialog.askdirectory(
                parent=win,
                title="添加动态壁纸目录",
                initialdir=initial,
                mustexist=True,
            )
            if not selected:
                return
            wallpaper_folders.append(selected)
            persist_wallpaper_folders()
            refresh_wallpaper_directory_ui()
            restart_wallpaper_preparation()

        def remove_wallpaper_folders() -> None:
            selected = {
                index
                for item in folder_tree.selection()
                if (index := folder_tree_index(item)) is not None
            }
            if not selected:
                return
            wallpaper_folders[:] = [
                folder for index, folder in enumerate(wallpaper_folders) if index not in selected
            ]
            persist_wallpaper_folders()
            refresh_wallpaper_directory_ui()
            restart_wallpaper_preparation()

        tk.Button(
            folder_button_row,
            text="＋ 添加目录",
            command=add_wallpaper_folder,
            bg="#f0f0f0",
            fg=FG,
            relief="flat",
            padx=10,
            pady=3,
            font=settings_font,
        ).pack(side="left")
        tk.Button(
            folder_button_row,
            text="移除所选",
            command=remove_wallpaper_folders,
            bg="#f0f0f0",
            fg=FG,
            relief="flat",
            padx=10,
            pady=3,
            font=settings_font,
        ).pack(side="left", padx=(6, 0))

        image_interval_row = tk.Frame(directories_pad, bg=BG)
        image_interval_row.pack(fill="x", pady=(0, 10))
        tk.Label(
            image_interval_row,
            text="图片切换间隔",
            fg=FG,
            bg=BG,
            font=settings_font,
        ).pack(side="left")

        def apply_wallpaper_image_seconds(
            _event: Any = None,
            *,
            persist: bool = True,
            reprepare: bool = True,
        ) -> None:
            try:
                seconds = int(wallpaper_image_seconds_var.get().strip())
            except (TypeError, ValueError):
                seconds = int(state["wallpaper_image_seconds"])
            seconds = max(2, min(300, seconds))
            wallpaper_image_seconds_var.set(str(seconds))
            changed = seconds != int(state["wallpaper_image_seconds"])
            state["wallpaper_image_seconds"] = seconds
            cfg["wallpaper_image_seconds"] = seconds
            if persist:
                save_config(cfg)
            if changed and reprepare and not state.get("wallpaper_active"):
                _clear_wallpaper_state()
                _schedule_background_wallpaper_prepare()

        image_interval_spinbox = tk.Spinbox(
            image_interval_row,
            from_=2,
            to=300,
            increment=1,
            width=5,
            textvariable=wallpaper_image_seconds_var,
            command=apply_wallpaper_image_seconds,
            font=settings_font,
            justify="right",
        )
        image_interval_spinbox.pack(side="left", padx=(10, 4))
        image_interval_spinbox.bind("<FocusOut>", apply_wallpaper_image_seconds)
        image_interval_spinbox.bind("<Return>", apply_wallpaper_image_seconds)
        tk.Label(
            image_interval_row,
            text="秒（2–300）",
            fg=FG_MUTED,
            bg=BG,
            font=settings_font_small,
        ).pack(side="left")

        def apply_wallpaper_audio() -> None:
            enabled = bool(wallpaper_audio_var.get())
            changed = enabled != bool(state.get("wallpaper_audio", True))
            state["wallpaper_audio"] = enabled
            cfg["wallpaper_audio"] = enabled
            save_config(cfg)
            if not changed:
                return
            was_active = bool(state.get("wallpaper_active"))
            if was_active:
                set_wallpaper_active(False)
                set_wallpaper_active(True)
            else:
                _clear_wallpaper_state()
                _schedule_background_wallpaper_prepare()

        tk.Checkbutton(
            directories_pad,
            text="播放视频声音",
            variable=wallpaper_audio_var,
            command=apply_wallpaper_audio,
            fg=FG,
            bg=BG,
            activebackground=BG,
            selectcolor=BG,
            font=settings_font,
            anchor="w",
        ).pack(fill="x", pady=(0, 10))
        def reset_all_directory_weights() -> None:
            keys = list(weight_vars)
            weights = dict(state.get("wallpaper_subdir_weights") or {})
            for key in keys:
                weights[key] = WALLPAPER_DIR_WEIGHT_DEFAULT
                weight_vars[key].set(str(WALLPAPER_DIR_WEIGHT_DEFAULT))
            state["wallpaper_subdir_weights"] = weights
            cfg["wallpaper_subdir_weights"] = weights
            save_config(cfg)
            if not state.get("wallpaper_active"):
                _clear_wallpaper_state()
                _schedule_background_wallpaper_prepare()

        tk.Button(
            weights_pad,
            text="全部恢复为 10",
            command=reset_all_directory_weights,
            bg="#f0f0f0",
            fg=FG,
            relief="flat",
            padx=10,
            pady=3,
            font=settings_font_small,
        ).pack(anchor="e", pady=(6, 0))
        refresh_wallpaper_directory_ui()

        tk.Label(floating_pad, text="悬浮窗字体大小", fg=FG, bg=BG, font=settings_font, anchor="w").pack(
            fill="x", pady=(0, 4)
        )

        size_var = tk.IntVar(value=int(state["font_size"]))
        size_label = tk.Label(
            floating_pad,
            text=str(size_var.get()),
            fg=FG_MUTED,
            bg=BG,
            font=settings_font_small,
            width=4,
        )

        def on_font_slide(val: str) -> None:
            try:
                v = int(float(val))
            except Exception:
                return
            try:
                size_label.configure(text=str(v))
            except Exception:
                pass
            # Only tweak font metrics immediately; full rebuild is debounced
            apply_font_size(v, persist=False, relayout=True)

        scale = tk.Scale(
            floating_pad,
            from_=FONT_SIZE_MIN,
            to=FONT_SIZE_MAX,
            orient="horizontal",
            variable=size_var,
            command=on_font_slide,
            showvalue=False,
            length=220,
            bg=BG,
            fg=FG,
            highlightthickness=0,
            troughcolor="#e8e8e8",
            activebackground=GREEN,
            sliderrelief="flat",
            bd=0,
        )
        scale.pack(fill="x")
        size_label.pack(anchor="e", pady=(2, 8))

        # ── Separate plate / text opacity ──
        tk.Label(
            floating_pad,
            text="底板不透明度",
            fg=FG,
            bg=BG,
            font=settings_font,
            anchor="w",
        ).pack(
            fill="x", pady=(6, 2)
        )
        bg_op_var = tk.IntVar(value=int(state["bg_opacity"]))
        bg_op_lbl = tk.Label(
            floating_pad,
            text=f"{bg_op_var.get()}%",
            fg=FG_MUTED,
            bg=BG,
            font=settings_font_small,
        )

        def on_bg_opacity(val: str) -> None:
            try:
                v = int(float(val))
            except Exception:
                return
            v = _clamp_opacity(v, BG_OPACITY_DEFAULT)
            state["bg_opacity"] = v
            bg_op_lbl.configure(text=f"{v}%")
            apply_opacity()

        bg_op_scale = tk.Scale(
            floating_pad,
            from_=OPACITY_MIN,
            to=OPACITY_MAX,
            orient="horizontal",
            variable=bg_op_var,
            command=on_bg_opacity,
            showvalue=False,
            length=220,
            bg=BG,
            fg=FG,
            highlightthickness=0,
            troughcolor="#e8e8e8",
            activebackground=GREEN,
            sliderrelief="flat",
            bd=0,
        )
        bg_op_scale.pack(fill="x")
        bg_op_lbl.pack(anchor="e", pady=(0, 4))

        tk.Label(
            floating_pad,
            text="文字不透明度",
            fg=FG,
            bg=BG,
            font=settings_font,
            anchor="w",
        ).pack(
            fill="x", pady=(4, 2)
        )
        tx_op_var = tk.IntVar(value=int(state["text_opacity"]))
        tx_op_lbl = tk.Label(
            floating_pad,
            text=f"{tx_op_var.get()}%",
            fg=FG_MUTED,
            bg=BG,
            font=settings_font_small,
        )

        def on_text_opacity(val: str) -> None:
            try:
                v = int(float(val))
            except Exception:
                return
            v = _clamp_opacity(v, TEXT_OPACITY_DEFAULT)
            state["text_opacity"] = v
            tx_op_lbl.configure(text=f"{v}%")
            apply_opacity()

        tx_op_scale = tk.Scale(
            floating_pad,
            from_=OPACITY_MIN,
            to=OPACITY_MAX,
            orient="horizontal",
            variable=tx_op_var,
            command=on_text_opacity,
            showvalue=False,
            length=220,
            bg=BG,
            fg=FG,
            highlightthickness=0,
            troughcolor="#e8e8e8",
            activebackground=GREEN,
            sliderrelief="flat",
            bd=0,
        )
        tx_op_scale.pack(fill="x")
        tx_op_lbl.pack(anchor="e", pady=(0, 6))
        opacity_hint = tk.Label(
            floating_pad,
            text="拖动即实时预览。数值越高越不透明，底板与文字互不影响。",
            fg=FG_MUTED,
            bg=BG,
            font=settings_font_small,
            anchor="w",
            wraplength=400,
            justify="left",
        )
        opacity_hint.pack(fill="x", pady=(0, 8))

        resize_var = tk.BooleanVar(value=bool(state["allow_resize"]))

        def on_resize_toggle() -> None:
            set_allow_resize(bool(resize_var.get()), persist=True)

        chk = tk.Checkbutton(
            floating_pad,
            text="悬浮窗允许拖动调整大小",
            variable=resize_var,
            command=on_resize_toggle,
            bg=BG,
            fg=FG,
            activebackground=BG,
            activeforeground=FG,
            selectcolor=BG,
            font=settings_font,
            anchor="w",
        )
        chk.pack(fill="x", pady=(4, 2))
        tk.Label(
            floating_pad,
            text="开启后，拖主窗口右下角 ◢ 可同时改宽度和高度。\n高度不会小于内容所需，避免上下裁切。",
            fg=FG_MUTED,
            bg=BG,
            font=settings_font_small,
            wraplength=400,
            justify="left",
            anchor="w",
        ).pack(fill="x", pady=(0, 10))
        update_panel_status()

        def on_close() -> None:
            # Slider callbacks already previewed font changes. Persist the
            # final value without scheduling another full HUD rebuild while
            # this dialog is being destroyed.
            # Commit weights before any other setting handler can rebuild the
            # weight rows and replace their pending StringVars.
            _commit_wallpaper_settings_before_close(
                commit_all_directory_weights,
                lambda: apply_wallpaper_image_seconds(
                    persist=False,
                    reprepare=True,
                ),
            )
            final_font_size = int(size_var.get())
            apply_font_size(final_font_size, persist=False, relayout=False)
            cfg["font_size"] = final_font_size
            cfg["card_width"] = int(state["card_width"])
            cfg["card_height"] = state.get("card_height")
            cfg["allow_resize"] = bool(state["allow_resize"])
            cfg["bg_opacity"] = int(state["bg_opacity"])
            cfg["text_opacity"] = int(state["text_opacity"])
            cfg["display_mode"] = str(state["display_mode"])
            cfg["monitor_device"] = state.get("monitor_device")
            cfg["monitor_id"] = state.get("monitor_id")
            cfg["double_ctrl_toggle"] = bool(state.get("shortcut_enabled", True))
            cfg["shortcut_enabled"] = bool(state.get("shortcut_enabled", True))
            cfg["shortcut_mode"] = _normalize_shortcut_mode(state.get("shortcut_mode"))
            cfg["shortcut_key"] = _normalize_shortcut_key(state.get("shortcut_key"))
            cfg["shortcut_repeat_count"] = _normalize_shortcut_repeat_count(
                state.get("shortcut_repeat_count")
            )
            cfg["shortcut_combo"] = _normalize_shortcut_combo(state.get("shortcut_combo"))
            cfg["llm_proxy_timezone"] = _normalize_llm_proxy_timezone(
                state.get("llm_proxy_timezone")
            )
            cfg["wallpaper_folder"] = state.get("wallpaper_folder")
            cfg["wallpaper_folders"] = list(state.get("wallpaper_folders") or [])
            cfg["wallpaper_disabled_folders"] = list(
                state.get("wallpaper_disabled_folders") or []
            )
            cfg["wallpaper_subdir_weights"] = dict(
                state.get("wallpaper_subdir_weights") or {}
            )
            cfg["wallpaper_random_mode"] = _normalize_wallpaper_random_mode(
                state.get("wallpaper_random_mode")
            )
            cfg["wallpaper_image_seconds"] = int(state["wallpaper_image_seconds"])
            cfg["wallpaper_audio"] = bool(state.get("wallpaper_audio", True))
            persist_timezone()
            persist_shortcut_from_ui()
            persist_providers(refresh=False)
            if not state.get("panel_active"):
                cfg["x"] = root.winfo_x()
                cfg["y"] = root.winfo_y()
            save_config(cfg)
            state["settings_win"] = None
            win.withdraw()
            win.after_idle(win.destroy)

        btn_row = tk.Frame(shell, bg=BG)
        btn_row.pack(fill="x", pady=(4, 0))
        close_btn = tk.Button(
            btn_row,
            text="完成",
            command=on_close,
            bg="#f0f0f0",
            fg=FG,
            relief="flat",
            padx=12,
            pady=4,
            font=settings_font,
        )
        close_btn.pack(side="right")

        win.protocol("WM_DELETE_WINDOW", on_close)
        # Keep settings on the primary work area even while the HUD occupies a
        # dedicated display, so changing/closing the mode is always reachable.
        place_settings_window()
        win.deiconify()
        win.lift()
        win.focus_set()

    def open_claude_reset_info() -> None:
        """Claude's /limit-reset grants, laid out like the Codex card dialog."""
        existing = state.get("claude_reset_win")
        if existing is not None:
            try:
                if existing.winfo_exists():
                    place_settings_on_primary(existing)
                    existing.deiconify()
                    existing.lift()
                    existing.focus_set()
                    return
            except Exception:
                pass

        win = tk.Toplevel(root)
        # Same reason as the settings dialog: map once, at the final position.
        win.withdraw()
        state["claude_reset_win"] = win
        state["claude_reset_busy"] = False
        win.title("claude 用量重置")
        win.configure(bg=BG)
        win.resizable(False, False)

        dialog_font = tkfont.Font(family=font_family, size=10)
        dialog_font_small = tkfont.Font(family=font_family, size=9)
        setattr(win, "_usage_float_fonts", (dialog_font, dialog_font_small))

        shell = tk.Frame(win, bg=BG, padx=14, pady=12)
        shell.pack(fill="both", expand=True)

        tk.Label(
            shell,
            text="选择一次重置，立即重置 claude 的用量窗口。",
            fg=FG,
            bg=BG,
            font=dialog_font,
            anchor="w",
            justify="left",
        ).pack(fill="x")

        list_frame = tk.Frame(shell, bg=BG, width=360)
        list_frame.pack(fill="both", expand=True, pady=(10, 6))

        status_label = tk.Label(
            shell,
            text="",
            fg=FG_MUTED,
            bg=BG,
            font=dialog_font_small,
            anchor="w",
            justify="left",
            wraplength=360,
        )
        status_label.pack(fill="x")

        btn_row = tk.Frame(shell, bg=BG)
        btn_row.pack(fill="x", pady=(10, 0))

        selected_grant = tk.StringVar(value="")
        grants_by_id: dict[str, ClaudeResetGrant] = {}
        pending_note = {"text": ""}

        def alive() -> bool:
            try:
                return bool(win.winfo_exists())
            except Exception:
                return False

        def close() -> None:
            state["claude_reset_win"] = None
            state["claude_reset_busy"] = False
            try:
                win.destroy()
            except Exception:
                pass

        def sync_buttons() -> None:
            if not alive():
                return
            busy = bool(state.get("claude_reset_busy"))
            try:
                use_btn.configure(
                    state="disabled" if busy or not selected_grant.get() else "normal"
                )
                reload_btn.configure(state="disabled" if busy else "normal")
            except Exception:
                pass

        def window_names(windows: tuple[str, ...]) -> str:
            return "、".join(CLAUDE_RESET_WINDOW_TEXT.get(w, w) for w in windows)

        def describe(grant: ClaudeResetGrant, reset: ClaudeResetStatus) -> str:
            parts: list[str] = []
            remaining = format_remaining(grant.expires_at)
            if remaining == "soon":
                parts.append("即将过期")
            elif remaining:
                parts.append(f"{remaining} 后过期")
            elif grant.expires_at is None:
                parts.append("长期有效")
            if grant.resets_left > 1:
                parts.append(f"{grant.resets_left} 次")
            if grant.clears:
                parts.append(f"重置{window_names(grant.clears)}")
            if not reset.claimable(grant):
                if grant.grant_id != reset.next_grant_id:
                    parts.append("排队中")
                elif grant.use_requires_limit:
                    parts.append("到达上限后可用")
                else:
                    parts.append("暂不可用")
            return " · ".join(parts) or grant.grant_id

        def claude_provider() -> ProviderUsage | None:
            for provider in state.get("providers") or []:
                if provider.provider_id == "claude":
                    return provider
            return None

        def render_grants(provider: ProviderUsage | None, error: str | None) -> None:
            if not alive():
                return
            for widget in list(list_frame.winfo_children()):
                try:
                    widget.destroy()
                except Exception:
                    pass
            grants_by_id.clear()
            selected_grant.set("")
            note = pending_note["text"]
            pending_note["text"] = ""

            if error:
                status_label.configure(text=error, fg=RED)
                sync_buttons()
                place_settings_on_primary(win)
                return

            reset = claude_reset_status(provider)
            for index, grant in enumerate(reset.grants, start=1):
                claimable = reset.claimable(grant)
                if claimable:
                    grants_by_id[grant.grant_id] = grant
                tk.Radiobutton(
                    list_frame,
                    text=f"{index}. {describe(grant, reset)}",
                    value=grant.grant_id,
                    variable=selected_grant,
                    command=sync_buttons,
                    state="normal" if claimable else "disabled",
                    fg=FG,
                    bg=BG,
                    activebackground=BG,
                    activeforeground=FG,
                    disabledforeground=FG_MUTED,
                    selectcolor=BG,
                    font=dialog_font,
                    anchor="w",
                    justify="left",
                    bd=0,
                    highlightthickness=0,
                ).pack(fill="x", pady=1)

            if grants_by_id:
                selected_grant.set(next(iter(grants_by_id)))
            if reset.grants:
                text = f"共 {reset.resets_left} 次可用重置。"
            else:
                tk.Label(
                    list_frame,
                    text="账号里没有可用的用量重置。",
                    fg=FG_MUTED,
                    bg=BG,
                    font=dialog_font,
                    anchor="w",
                    justify="left",
                ).pack(fill="x")
                reason = reset.ineligible_reason
                text = CLAUDE_RESET_REASON_TEXT.get(reason, reason) if reason else ""
            status_label.configure(
                text=f"{note} {text}".strip() if note else text,
                fg=FG_MUTED,
            )
            sync_buttons()
            place_settings_on_primary(win)

        def load_grants() -> None:
            if not alive() or state.get("claude_reset_busy"):
                return
            state["claude_reset_busy"] = True
            sync_buttons()
            status_label.configure(text="正在读取重置额度…", fg=FG_MUTED)

            def work() -> None:
                try:
                    fresh: ProviderUsage | None = fetch_claude_usage(force=True)
                except Exception:
                    _log_exception("claude reset status")
                    fresh = None

                def deliver() -> None:
                    state["claude_reset_busy"] = False
                    if fresh is None or not fresh.available:
                        render_grants(None, "读取重置额度失败")
                        return
                    # Share the fresh read with the HUD so its 5h number and
                    # reset mark agree with what the dialog lists.
                    state["providers"] = [
                        fresh if p.provider_id == "claude" else p
                        for p in state.get("providers") or []
                    ]
                    render(relayout=not state.get("panel_active"))
                    render_grants(fresh, None)

                root.after(0, deliver)

            threading.Thread(target=work, daemon=True).start()

        def finish(claim: ClaudeResetClaim) -> None:
            state["claude_reset_busy"] = False
            if claim.settled:
                state["claude_reset_unsettled"] = None
            if not alive():
                if claim.ok:
                    refresh_async(force=True)
                return
            if claim.ok:
                # Reloading the grants also refreshes the HUD's claude rows.
                pending_note["text"] = claim.message
                load_grants()
            else:
                status_label.configure(text=claim.message, fg=RED)
                sync_buttons()

        def use_selected() -> None:
            if state.get("claude_reset_busy"):
                return
            grant_id = selected_grant.get()
            grant = grants_by_id.get(grant_id)
            if grant is None:
                return
            from tkinter import messagebox

            windows = window_names(grant.clears) or "用量窗口"
            if not messagebox.askokcancel(
                "使用用量重置",
                f"使用后这次重置立即作废，claude 的{windows}会重新开始计算。\n"
                "确定要使用吗？",
                parent=win,
            ):
                return
            # Resend the id of an attempt whose outcome never arrived, so the
            # server dedupes it rather than spending a second reset.
            unsettled = state.get("claude_reset_unsettled")
            if unsettled and unsettled[0] == grant_id:
                request_id = unsettled[1]
            else:
                request_id = str(uuid.uuid4())
                state["claude_reset_unsettled"] = (grant_id, request_id)
            state["claude_reset_busy"] = True
            sync_buttons()
            status_label.configure(text="正在使用重置…", fg=FG_MUTED)

            def work() -> None:
                try:
                    claim = claim_claude_reset(grant_id, request_id=request_id)
                except Exception:
                    _log_exception("claude reset claim")
                    claim = ClaudeResetClaim(False, "使用失败", settled=False)
                root.after(0, lambda: finish(claim))

            threading.Thread(target=work, daemon=True).start()

        use_btn = tk.Button(
            btn_row,
            text="使用",
            command=use_selected,
            bg="#f0f0f0",
            fg=FG,
            relief="flat",
            padx=12,
            pady=4,
            font=dialog_font,
            state="disabled",
        )
        use_btn.pack(side="right")
        reload_btn = tk.Button(
            btn_row,
            text="刷新列表",
            command=load_grants,
            bg="#f0f0f0",
            fg=FG,
            relief="flat",
            padx=12,
            pady=4,
            font=dialog_font,
        )
        reload_btn.pack(side="right", padx=(0, 8))
        tk.Button(
            btn_row,
            text="关闭",
            command=close,
            bg="#f0f0f0",
            fg=FG,
            relief="flat",
            padx=12,
            pady=4,
            font=dialog_font,
        ).pack(side="left")

        win.protocol("WM_DELETE_WINDOW", close)
        win.bind("<Escape>", lambda _event: close())
        # The usage read already carries the grants; reuse it instead of
        # another call to an endpoint that answers 429 readily.
        provider = claude_provider()
        cached = provider is not None and provider.reset_block is not None
        if cached:
            render_grants(provider, None)
        place_settings_on_primary(win)
        win.deiconify()
        win.lift()
        win.focus_set()
        if not cached:
            load_grants()

    def open_claude_session_reset() -> None:
        """Claude's weekly 5h session reset, laid out like the Codex card dialog."""
        existing = state.get("claude_session_win")
        if existing is not None:
            try:
                if existing.winfo_exists():
                    place_settings_on_primary(existing)
                    existing.deiconify()
                    existing.lift()
                    existing.focus_set()
                    return
            except Exception:
                pass

        win = tk.Toplevel(root)
        # Same reason as the settings dialog: map once, at the final position.
        win.withdraw()
        state["claude_session_win"] = win
        state["claude_session_busy"] = False
        win.title("claude 5 小时重置")
        win.configure(bg=BG)
        win.resizable(False, False)

        dialog_font = tkfont.Font(family=font_family, size=10)
        dialog_font_small = tkfont.Font(family=font_family, size=9)
        setattr(win, "_usage_float_fonts", (dialog_font, dialog_font_small))

        shell = tk.Frame(win, bg=BG, padx=14, pady=12)
        shell.pack(fill="both", expand=True)

        tk.Label(
            shell,
            text="选择一次重置，立即重置 claude 的 5 小时会话。",
            fg=FG,
            bg=BG,
            font=dialog_font,
            anchor="w",
            justify="left",
        ).pack(fill="x")

        list_frame = tk.Frame(shell, bg=BG, width=360)
        list_frame.pack(fill="both", expand=True, pady=(10, 6))

        status_label = tk.Label(
            shell,
            text="",
            fg=FG_MUTED,
            bg=BG,
            font=dialog_font_small,
            anchor="w",
            justify="left",
            wraplength=360,
        )
        status_label.pack(fill="x")

        btn_row = tk.Frame(shell, bg=BG)
        btn_row.pack(fill="x", pady=(10, 0))

        selected = tk.StringVar(value="")
        pending_note = {"text": ""}

        def alive() -> bool:
            try:
                return bool(win.winfo_exists())
            except Exception:
                return False

        def close() -> None:
            state["claude_session_win"] = None
            state["claude_session_busy"] = False
            try:
                win.destroy()
            except Exception:
                pass

        def sync_buttons() -> None:
            if not alive():
                return
            busy = bool(state.get("claude_session_busy"))
            try:
                use_btn.configure(state="disabled" if busy or not selected.get() else "normal")
                reload_btn.configure(state="disabled" if busy else "normal")
            except Exception:
                pass

        def render_reset(reset: ClaudeSessionReset | None, error: str | None) -> None:
            if not alive():
                return
            for widget in list(list_frame.winfo_children()):
                try:
                    widget.destroy()
                except Exception:
                    pass
            selected.set("")
            note = pending_note["text"]
            pending_note["text"] = ""

            if error or reset is None:
                status_label.configure(text=error or "读取重置额度失败", fg=RED)
                sync_buttons()
                place_settings_on_primary(win)
                return

            if reset.offered:
                parts = [f"每周 {reset.resets_per_week} 次"]
                remaining = format_remaining(reset.next_available_at)
                if reset.available:
                    parts.append("本周可用")
                elif remaining:
                    parts.append(f"{remaining} 后可用")
                else:
                    parts.append("本周已用")
                tk.Radiobutton(
                    list_frame,
                    text=f"1. {' · '.join(parts)}",
                    value="session",
                    variable=selected,
                    command=sync_buttons,
                    state="normal" if reset.claimable else "disabled",
                    fg=FG,
                    bg=BG,
                    activebackground=BG,
                    activeforeground=FG,
                    disabledforeground=FG_MUTED,
                    selectcolor=BG,
                    font=dialog_font,
                    anchor="w",
                    justify="left",
                    bd=0,
                    highlightthickness=0,
                ).pack(fill="x", pady=1)
                if reset.claimable:
                    selected.set("session")
            else:
                tk.Label(
                    list_frame,
                    text="账号里没有可用的 5 小时重置。",
                    fg=FG_MUTED,
                    bg=BG,
                    font=dialog_font,
                    anchor="w",
                    justify="left",
                ).pack(fill="x")

            if reset.claimable:
                text = "共 1 次可用重置。"
            else:
                reason = reset.ineligible_reason
                text = CLAUDE_SESSION_REASON_TEXT.get(reason, reason) if reason else ""
            status_label.configure(
                text=f"{note} {text}".strip() if note else text,
                fg=FG_MUTED,
            )
            sync_buttons()
            place_settings_on_primary(win)

        def load_reset() -> None:
            if not alive() or state.get("claude_session_busy"):
                return
            state["claude_session_busy"] = True
            sync_buttons()
            status_label.configure(text="正在读取重置额度…", fg=FG_MUTED)

            def work() -> None:
                try:
                    reset, error = fetch_claude_session_reset()
                except Exception:
                    _log_exception("claude session reset status")
                    reset, error = None, "读取重置额度失败"

                def deliver() -> None:
                    state["claude_session_busy"] = False
                    render_reset(reset, error)

                root.after(0, deliver)

            threading.Thread(target=work, daemon=True).start()

        def finish(claim: ClaudeResetClaim) -> None:
            state["claude_session_busy"] = False
            if not alive():
                if claim.ok:
                    refresh_async(force=True)
                return
            if claim.ok:
                pending_note["text"] = claim.message
                refresh_async(force=True)
                load_reset()
            else:
                status_label.configure(text=claim.message, fg=RED)
                sync_buttons()

        def use_selected() -> None:
            if state.get("claude_session_busy") or not selected.get():
                return
            from tkinter import messagebox

            if not messagebox.askokcancel(
                "使用 5 小时重置",
                "使用后本周的 5 小时重置立即作废，claude 的 5 小时会话会重新开始计算。\n"
                "确定要使用吗？",
                parent=win,
            ):
                return
            state["claude_session_busy"] = True
            sync_buttons()
            status_label.configure(text="正在使用重置…", fg=FG_MUTED)

            def work() -> None:
                try:
                    claim = claim_claude_session_reset()
                except Exception:
                    _log_exception("claude session reset claim")
                    claim = ClaudeResetClaim(False, "使用失败", settled=False)
                root.after(0, lambda: finish(claim))

            threading.Thread(target=work, daemon=True).start()

        use_btn = tk.Button(
            btn_row,
            text="使用",
            command=use_selected,
            bg="#f0f0f0",
            fg=FG,
            relief="flat",
            padx=12,
            pady=4,
            font=dialog_font,
            state="disabled",
        )
        use_btn.pack(side="right")
        reload_btn = tk.Button(
            btn_row,
            text="刷新列表",
            command=load_reset,
            bg="#f0f0f0",
            fg=FG,
            relief="flat",
            padx=12,
            pady=4,
            font=dialog_font,
        )
        reload_btn.pack(side="right", padx=(0, 8))
        tk.Button(
            btn_row,
            text="关闭",
            command=close,
            bg="#f0f0f0",
            fg=FG,
            relief="flat",
            padx=12,
            pady=4,
            font=dialog_font,
        ).pack(side="left")

        win.protocol("WM_DELETE_WINDOW", close)
        win.bind("<Escape>", lambda _event: close())
        place_settings_on_primary(win)
        win.deiconify()
        win.lift()
        win.focus_set()
        # The regular usage read doesn't carry this block, so read it now.
        load_reset()

    def open_codex_reset_cards(provider_id: str = "codex") -> None:
        account = CODEX_ACCOUNT_BY_ID.get(provider_id) or CODEX_ACCOUNTS[0]
        existing = state.get("reset_cards_win")
        if existing is not None:
            try:
                if existing.winfo_exists():
                    if state.get("reset_cards_account") == account.provider_id:
                        place_settings_on_primary(existing)
                        existing.deiconify()
                        existing.lift()
                        existing.focus_set()
                        return
                    # Clicking the other account's number must not hand back a
                    # window still listing the first account's cards.
                    existing.destroy()
            except Exception:
                pass

        win = tk.Toplevel(root)
        # Same reason as the settings dialog: map once, at the final position.
        win.withdraw()
        state["reset_cards_win"] = win
        state["reset_cards_account"] = account.provider_id
        state["reset_cards_busy"] = False
        win.title(f"{account.display_name} 重置卡")
        win.configure(bg=BG)
        win.resizable(False, False)

        dialog_font = tkfont.Font(family=font_family, size=10)
        dialog_font_small = tkfont.Font(family=font_family, size=9)
        setattr(win, "_usage_float_fonts", (dialog_font, dialog_font_small))

        shell = tk.Frame(win, bg=BG, padx=14, pady=12)
        shell.pack(fill="both", expand=True)

        tk.Label(
            shell,
            text=f"选择一张重置卡，立即重置 {account.display_name} 的用量窗口。",
            fg=FG,
            bg=BG,
            font=dialog_font,
            anchor="w",
            justify="left",
        ).pack(fill="x")

        list_frame = tk.Frame(shell, bg=BG, width=360)
        list_frame.pack(fill="both", expand=True, pady=(10, 6))

        status_label = tk.Label(
            shell,
            text="正在读取重置卡…",
            fg=FG_MUTED,
            bg=BG,
            font=dialog_font_small,
            anchor="w",
            justify="left",
            wraplength=360,
        )
        status_label.pack(fill="x")

        btn_row = tk.Frame(shell, bg=BG)
        btn_row.pack(fill="x", pady=(10, 0))

        selected_card = tk.StringVar(value="")
        cards_by_id: dict[str, CodexResetCard] = {}
        pending_note = {"text": ""}

        def alive() -> bool:
            try:
                return bool(win.winfo_exists())
            except Exception:
                return False

        def close() -> None:
            state["reset_cards_win"] = None
            state["reset_cards_account"] = None
            state["reset_cards_busy"] = False
            try:
                win.destroy()
            except Exception:
                pass

        def sync_buttons() -> None:
            if not alive():
                return
            busy = bool(state.get("reset_cards_busy"))
            try:
                use_btn.configure(
                    state="disabled" if busy or not selected_card.get() else "normal"
                )
                reload_btn.configure(state="disabled" if busy else "normal")
            except Exception:
                pass

        def describe(card: CodexResetCard) -> str:
            parts: list[str] = []
            remaining = format_remaining(card.expires_at)
            if remaining == "soon":
                parts.append("即将过期")
            elif remaining:
                parts.append(f"{remaining} 后过期")
            elif card.expires_at is None:
                parts.append("长期有效")
            granted = parse_iso(card.granted_at)
            if granted is not None:
                if granted.tzinfo is None:
                    granted = granted.replace(tzinfo=timezone.utc)
                parts.append(f"获得于 {granted.astimezone():%Y-%m-%d %H:%M}")
            if not card.usable:
                parts.append(card.status_text)
            return " · ".join(parts) or card.card_id

        def render_cards(cards: list[CodexResetCard], error: str | None) -> None:
            if not alive():
                return
            for widget in list(list_frame.winfo_children()):
                try:
                    widget.destroy()
                except Exception:
                    pass
            cards_by_id.clear()
            selected_card.set("")
            note = pending_note["text"]
            pending_note["text"] = ""

            if error:
                status_label.configure(text=error, fg=RED)
                sync_buttons()
                place_settings_on_primary(win)
                return

            usable = [card for card in cards if card.usable]
            for index, card in enumerate(usable, start=1):
                cards_by_id[card.card_id] = card
                tk.Radiobutton(
                    list_frame,
                    text=f"{index}. {describe(card)}",
                    value=card.card_id,
                    variable=selected_card,
                    command=sync_buttons,
                    fg=FG,
                    bg=BG,
                    activebackground=BG,
                    activeforeground=FG,
                    selectcolor=BG,
                    font=dialog_font,
                    anchor="w",
                    justify="left",
                    bd=0,
                    highlightthickness=0,
                ).pack(fill="x", pady=1)

            if usable:
                selected_card.set(usable[0].card_id)
                text = f"共 {len(usable)} 张可用重置卡。"
                spent = len(cards) - len(usable)
                if spent:
                    text += f" 另有 {spent} 张已使用或正在使用。"
            else:
                tk.Label(
                    list_frame,
                    text="账号里没有可用的重置卡。",
                    fg=FG_MUTED,
                    bg=BG,
                    font=dialog_font,
                    anchor="w",
                    justify="left",
                ).pack(fill="x")
                text = "OpenAI 发放重置卡后会出现在这里，每张自发放起 30 天内有效。"
            status_label.configure(
                text=f"{note} {text}".strip() if note else text,
                fg=FG_MUTED,
            )
            sync_buttons()
            place_settings_on_primary(win)

        def load_cards() -> None:
            if not alive() or state.get("reset_cards_busy"):
                return
            state["reset_cards_busy"] = True
            sync_buttons()
            status_label.configure(text="正在读取重置卡…", fg=FG_MUTED)

            def work() -> None:
                try:
                    cards, error = fetch_codex_reset_cards(account.home)
                except Exception:
                    _log_exception("codex reset cards")
                    cards, error = [], "读取重置卡失败"

                def deliver() -> None:
                    state["reset_cards_busy"] = False
                    render_cards(cards, error)

                root.after(0, deliver)

            threading.Thread(target=work, daemon=True).start()

        def finish(ok: bool, message: str) -> None:
            state["reset_cards_busy"] = False
            if ok:
                # The window only moves once the backend acknowledges the
                # redeem, so pull fresh numbers instead of guessing locally.
                refresh_async(force=True)
            if not alive():
                return
            if ok:
                pending_note["text"] = message
                load_cards()
            else:
                status_label.configure(text=message, fg=RED)
                sync_buttons()

        def use_selected() -> None:
            if state.get("reset_cards_busy"):
                return
            card_id = selected_card.get()
            if card_id not in cards_by_id:
                return
            from tkinter import messagebox

            if not messagebox.askokcancel(
                "使用重置卡",
                f"使用后这张重置卡立即作废，{account.display_name} 的用量窗口会重新开始计算。\n"
                "确定要使用吗？",
                parent=win,
            ):
                return
            state["reset_cards_busy"] = True
            sync_buttons()
            status_label.configure(text="正在使用重置卡…", fg=FG_MUTED)

            def work() -> None:
                try:
                    ok, message = consume_codex_reset_card(card_id, home=account.home)
                except Exception:
                    _log_exception("codex reset consume")
                    ok, message = False, "使用失败"
                root.after(0, lambda: finish(ok, message))

            threading.Thread(target=work, daemon=True).start()

        use_btn = tk.Button(
            btn_row,
            text="使用",
            command=use_selected,
            bg="#f0f0f0",
            fg=FG,
            relief="flat",
            padx=12,
            pady=4,
            font=dialog_font,
            state="disabled",
        )
        use_btn.pack(side="right")
        reload_btn = tk.Button(
            btn_row,
            text="刷新列表",
            command=load_cards,
            bg="#f0f0f0",
            fg=FG,
            relief="flat",
            padx=12,
            pady=4,
            font=dialog_font,
        )
        reload_btn.pack(side="right", padx=(0, 8))
        tk.Button(
            btn_row,
            text="关闭",
            command=close,
            bg="#f0f0f0",
            fg=FG,
            relief="flat",
            padx=12,
            pady=4,
            font=dialog_font,
        ).pack(side="left")

        win.protocol("WM_DELETE_WINDOW", close)
        win.bind("<Escape>", lambda _event: close())
        place_settings_on_primary(win)
        win.deiconify()
        win.lift()
        win.focus_set()
        load_cards()

    def request_open_settings() -> None:
        if state.get("settings_open_job") is not None:
            return

        def open_after_menu_closes() -> None:
            state["settings_open_job"] = None
            open_settings()

        # Let the native popup finish its selection/unpost cycle first. Opening
        # a Toplevel directly from the menu command made the first dialog lose
        # its mapping; the second click merely lifted that hidden instance.
        state["settings_open_job"] = root.after_idle(open_after_menu_closes)

    def toggle_panel_mode() -> None:
        if state.get("display_mode") == DISPLAY_MODE_PANEL:
            set_display_mode(DISPLAY_MODE_FLOAT, persist=True)
            return
        monitor = _choose_panel_monitor(
            _enumerate_monitors(),
            state.get("monitor_device"),
            state.get("monitor_id"),
        )
        if monitor is None:
            monitor = _choose_panel_monitor(_enumerate_monitors(), None)
        set_display_mode(
            DISPLAY_MODE_PANEL,
            monitor.device if monitor is not None else None,
            persist=True,
        )

    def rebuild_menu() -> None:
        menu.delete(0, "end")
        menu.add_command(label="刷新", command=lambda: refresh_async(force=True))
        menu.add_command(label="设置…", command=request_open_settings)
        menu.add_command(
            label=(
                "退出专用副屏模式"
                if state.get("display_mode") == DISPLAY_MODE_PANEL
                else "进入专用副屏模式"
            ),
            command=toggle_panel_mode,
        )
        menu.add_command(
            label=("✓ 置顶" if state["always_on_top"] else "置顶"),
            command=toggle_topmost,
        )
        menu.add_separator()
        menu.add_command(
            label=("✓ 开机自启" if is_autostart_enabled() else "开机自启"),
            command=toggle_autostart,
        )
        menu.add_separator()
        menu.add_command(label="退出", command=do_quit)

    def toggle_topmost() -> None:
        state["always_on_top"] = not state["always_on_top"]
        root.attributes("-topmost", state["always_on_top"])
        if bg_layer is not None and not state.get("panel_active"):
            try:
                bg_layer.attributes("-topmost", state["always_on_top"])
                stack_text_above_plate()
            except Exception:
                pass
        cfg["always_on_top"] = state["always_on_top"]
        save_config(cfg)

    def toggle_autostart() -> None:
        set_autostart(not is_autostart_enabled())

    def do_quit() -> None:
        if not state.get("panel_active"):
            cfg["x"] = root.winfo_x()
            cfg["y"] = root.winfo_y()
        cfg["font_size"] = int(state["font_size"])
        cfg["card_width"] = int(state["card_width"])
        cfg["card_height"] = state.get("card_height")
        cfg["allow_resize"] = bool(state["allow_resize"])
        cfg["bg_opacity"] = int(state["bg_opacity"])
        cfg["text_opacity"] = int(state["text_opacity"])
        cfg["display_mode"] = str(state["display_mode"])
        cfg["monitor_device"] = state.get("monitor_device")
        cfg["monitor_id"] = state.get("monitor_id")
        cfg["double_ctrl_toggle"] = bool(state.get("shortcut_enabled", True))
        cfg["shortcut_enabled"] = bool(state.get("shortcut_enabled", True))
        cfg["shortcut_mode"] = _normalize_shortcut_mode(state.get("shortcut_mode"))
        cfg["shortcut_key"] = _normalize_shortcut_key(state.get("shortcut_key"))
        cfg["shortcut_repeat_count"] = _normalize_shortcut_repeat_count(
            state.get("shortcut_repeat_count")
        )
        cfg["shortcut_combo"] = _normalize_shortcut_combo(state.get("shortcut_combo"))
        cfg["llm_proxy_timezone"] = _normalize_llm_proxy_timezone(
            state.get("llm_proxy_timezone")
        )
        cfg["wallpaper_folder"] = state.get("wallpaper_folder")
        cfg["wallpaper_folders"] = list(state.get("wallpaper_folders") or [])
        cfg["wallpaper_disabled_folders"] = list(
            state.get("wallpaper_disabled_folders") or []
        )
        cfg["wallpaper_subdir_weights"] = dict(
            state.get("wallpaper_subdir_weights") or {}
        )
        cfg["wallpaper_random_mode"] = _normalize_wallpaper_random_mode(
            state.get("wallpaper_random_mode")
        )
        cfg["wallpaper_image_seconds"] = int(state["wallpaper_image_seconds"])
        cfg["wallpaper_audio"] = bool(state.get("wallpaper_audio", True))
        save_config(cfg)
        drop_cleanup = state.get("wallpaper_drop_cleanup")
        if callable(drop_cleanup):
            drop_cleanup()
            state["wallpaper_drop_cleanup"] = None
        _cancel_wallpaper_prepare_jobs()
        _cancel_wallpaper_check()
        wallpaper_player.stop()
        double_ctrl_job = state.get("double_ctrl_job")
        if double_ctrl_job is not None:
            try:
                root.after_cancel(double_ctrl_job)
            except Exception:
                pass
        try:
            if bg_layer is not None:
                bg_layer.destroy()
        except Exception:
            pass
        root.destroy()

    def show_menu(event: tk.Event) -> str:
        try:
            stack_text_above_plate()
            rebuild_menu()
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        # Most HUD children have their own interaction binding and Tk then
        # continues through the Toplevel bindtag. Without "break", one physical
        # right-click posts this same menu twice: the first Settings selection
        # merely reveals the second popup, and its lingering grab flashes the
        # panel again when the dialog closes.
        return "break"

    # ── Resize grip: width + height (overrideredirect has no native border) ──
    def start_resize(event: tk.Event) -> None:
        if not state["allow_resize"] or state.get("panel_active"):
            return
        state["resize_active"] = True
        state["resize_start_w"] = int(state["card_width"])
        state["resize_start_h"] = max(
            content_min_height(),
            int(state["card_height"] or root.winfo_height() or content_min_height()),
        )
        state["resize_start_x"] = event.x_root
        state["resize_start_y"] = event.y_root

    def do_resize(event: tk.Event) -> None:
        if not state["resize_active"]:
            return
        dw = event.x_root - int(state["resize_start_x"])
        dh = event.y_root - int(state["resize_start_y"])
        new_w = max(
            CARD_WIDTH_MIN,
            min(CARD_WIDTH_MAX, int(state["resize_start_w"]) + dw),
        )
        auto_h = content_min_height()
        new_h = max(
            auto_h,
            min(CARD_HEIGHT_MAX, int(state["resize_start_h"]) + dh),
        )
        changed = False
        if new_w != state["card_width"]:
            state["card_width"] = new_w
            changed = True
        if state.get("card_height") != new_h:
            state["card_height"] = new_h
            changed = True
        if changed:
            # Width change needs column recompute; height-only can just set geometry
            if new_w != int(state["resize_start_w"]) + 0 and abs(dw) > abs(dh):
                schedule_render(16)
            else:
                apply_geometry()

    def stop_resize(_event: tk.Event) -> None:
        if not state["resize_active"]:
            return
        state["resize_active"] = False
        cfg["card_width"] = int(state["card_width"])
        cfg["card_height"] = state.get("card_height")
        cfg["x"] = root.winfo_x()
        cfg["y"] = root.winfo_y()
        save_config(cfg)
        schedule_render(0)

    grip.bind("<ButtonPress-1>", start_resize)
    grip.bind("<B1-Motion>", do_resize)
    grip.bind("<ButtonRelease-1>", stop_resize)
    grip.bind("<Button-3>", show_menu)

    bind_drag(body)
    bind_drag(root)
    if bg_layer is not None:
        # Transparent pixels in the text window hit the plate underneath.
        # Mirror the normal window interactions there so the HUD never feels
        # frozen when the user clicks or drags an empty area.
        bind_drag(bg_layer)
        bg_layer.bind("<Motion>", lambda event: set_refresh_hover(event_hits_refresh(event)))
        bg_layer.bind("<Leave>", lambda _e: set_refresh_hover(False))
        bind_hover_tracking(bg_layer)
        bg_layer.bind("<Escape>", lambda _e: do_quit())
        bg_layer.bind("<F5>", lambda _e: refresh_async(force=True))
    root.bind("<Button-3>", show_menu)
    root.bind("<FocusIn>", lambda _e: root.after_idle(stack_text_above_plate))
    root.bind("<Map>", lambda _e: root.after_idle(stack_text_above_plate))
    if bg_layer is not None:
        bg_layer.bind("<FocusIn>", lambda _e: root.after_idle(stack_text_above_plate))
    root.bind("<Escape>", lambda _e: do_quit())
    root.bind("<F5>", lambda _e: refresh_async(force=True))

    def reset_position() -> None:
        if state.get("panel_active"):
            apply_geometry()
            return
        mx, my, mw, mh = _screen_bounds()[0]
        w = int(state["card_width"])
        x, y = mx + max(12, mw - w - 24), my + 36
        root.geometry(_tk_position(x, y))
        state["floating_x"], state["floating_y"] = x, y
        cfg["x"], cfg["y"] = x, y
        save_config(cfg)

    root.bind("<Home>", lambda _e: reset_position())

    # Position — width from config
    root.update_idletasks()
    panel = state.get("panel_monitor") if state.get("panel_active") else None
    if panel is not None:
        ww, wh = panel.width, panel.height
        x, y = panel.x, panel.y
    else:
        ww, wh = int(state["card_width"]), 120
        if cfg.get("x") is not None and cfg.get("y") is not None:
            x, y = _clamp_position(int(cfg["x"]), int(cfg["y"]), ww, wh)
        else:
            mx, my, mw, mh = _screen_bounds()[0]
            x, y = mx + max(12, mw - ww - 24), my + 36
        state["floating_x"], state["floating_y"] = x, y
    initial_geo = _tk_geometry(ww, wh, x, y)
    root.geometry(initial_geo)
    if bg_layer is not None and panel is None:
        try:
            bg_layer.geometry(initial_geo)
            stack_text_above_plate()
        except Exception:
            pass
    if panel is None:
        cfg["x"], cfg["y"] = x, y
    cfg["bg_opacity"] = int(state["bg_opacity"])
    cfg["text_opacity"] = int(state["text_opacity"])
    cfg["font_size"] = int(state["font_size"])
    cfg["card_width"] = int(state["card_width"])
    cfg["card_height"] = state.get("card_height")
    cfg["allow_resize"] = bool(state["allow_resize"])
    cfg["display_mode"] = str(state["display_mode"])
    cfg["monitor_device"] = state.get("monitor_device")
    cfg["monitor_id"] = state.get("monitor_id")
    cfg["double_ctrl_toggle"] = bool(state.get("shortcut_enabled", True))
    cfg["shortcut_enabled"] = bool(state.get("shortcut_enabled", True))
    cfg["shortcut_mode"] = _normalize_shortcut_mode(state.get("shortcut_mode"))
    cfg["shortcut_key"] = _normalize_shortcut_key(state.get("shortcut_key"))
    cfg["shortcut_repeat_count"] = _normalize_shortcut_repeat_count(
        state.get("shortcut_repeat_count")
    )
    cfg["shortcut_combo"] = _normalize_shortcut_combo(state.get("shortcut_combo"))
    cfg["llm_proxy_timezone"] = _normalize_llm_proxy_timezone(
        state.get("llm_proxy_timezone")
    )
    cfg["wallpaper_folder"] = state.get("wallpaper_folder")
    cfg["wallpaper_folders"] = list(state.get("wallpaper_folders") or [])
    cfg["wallpaper_disabled_folders"] = list(
        state.get("wallpaper_disabled_folders") or []
    )
    cfg["wallpaper_subdir_weights"] = dict(
        state.get("wallpaper_subdir_weights") or {}
    )
    cfg["wallpaper_random_mode"] = _normalize_wallpaper_random_mode(
        state.get("wallpaper_random_mode")
    )
    cfg["wallpaper_image_seconds"] = int(state["wallpaper_image_seconds"])
    cfg["wallpaper_audio"] = bool(state.get("wallpaper_audio", True))
    cfg["refresh_seconds"] = max(REFRESH_SECONDS, int(cfg.get("refresh_seconds") or REFRESH_SECONDS))
    save_config(cfg)

    # Apply initial resize grip visibility + opacity
    set_allow_resize(bool(state["allow_resize"]), persist=False)
    apply_opacity()
    _sync_display_runtime(available_monitors, persist=False)

    def enforce_window_band() -> None:
        try:
            root.attributes("-topmost", bool(state["always_on_top"]))
            if bg_layer is not None and not state.get("panel_active"):
                bg_layer.attributes("-topmost", bool(state["always_on_top"]))
            stack_text_above_plate()
        except Exception:
            pass

    # Tk may map/recreate the native wrapper after its first topmost call.
    root.after_idle(enforce_window_band)
    root.after(250, enforce_window_band)
    root.after(1200, enforce_window_band)
    root.after(MONITOR_POLL_MS, poll_monitors)
    state["double_ctrl_job"] = root.after(
        DOUBLE_CTRL_POLL_MS,
        poll_double_ctrl_toggle,
    )
    if not _docs_shot_active():
        root.after(350, _schedule_background_wallpaper_prepare)

    root.after(10, lambda: _hide_from_taskbar(root))
    root.after(100, lambda: _hide_from_taskbar(root))

    interval_ms = int(float(cfg.get("refresh_seconds") or REFRESH_SECONDS) * 1000)

    def tick_refresh() -> None:
        refresh_async(force=False)
        root.after(interval_ms, tick_refresh)

    if _docs_shot_active():
        def _run_docs_shot() -> None:
            log_path = Path(__file__).resolve().parent / "docs" / "screenshots" / "_shot_log.txt"
            try:
                from tkinter import ttk

                state["display_mode"] = DISPLAY_MODE_PANEL
                state["double_ctrl_toggle"] = True
                state["shortcut_enabled"] = True
                state["panel_active"] = True
                open_settings()
                win = state.get("settings_win")
                if win is None:
                    log_path.write_text("no settings win\n", encoding="utf-8")
                    root.destroy()
                    return
                notebooks: list[Any] = []

                def walk(widget: Any) -> None:
                    if isinstance(widget, ttk.Notebook):
                        notebooks.append(widget)
                    for child in widget.winfo_children():
                        walk(child)

                walk(win)
                for notebook in notebooks:
                    notebook.configure(height=520)
                win.geometry("480x740")
                win.update()
                out_dir = Path(__file__).resolve().parent / "docs" / "screenshots"
                out_dir.mkdir(parents=True, exist_ok=True)
                log_path.write_text(
                    f"notebooks={len(notebooks)} title={win.title()!r}\n",
                    encoding="utf-8",
                )
                def select_notebook_tab(widget: Any, title: str) -> None:
                    for index, tab_id in enumerate(widget.tabs()):
                        if widget.tab(tab_id, "text") == title:
                            widget.select(index)
                            return

                def capture_main_tab(title: str, path: Path, inner_title: str | None = None) -> None:
                    select_notebook_tab(notebooks[0], title)
                    win.update_idletasks()
                    win.update()
                    if inner_title and len(notebooks) >= 2:
                        select_notebook_tab(notebooks[1], inner_title)
                        win.update_idletasks()
                        win.update()
                    time.sleep(0.35)
                    win.update()
                    _capture_widget_png(win, path)

                if notebooks:
                    capture_main_tab("用量", out_dir / "settings-usage.png")
                    capture_main_tab("副屏", out_dir / "settings-display.png")
                    capture_main_tab(
                        "动态壁纸",
                        out_dir / "settings-wallpaper.png",
                        "随机权重",
                    )
            except Exception:
                log_path.write_text(traceback.format_exc(), encoding="utf-8")
            finally:
                root.destroy()

        root.after(800, _run_docs_shot)
    else:
        refresh_async(force=True)
        root.after(interval_ms, tick_refresh)
        root.after(15_000, tick_footer)
    root.mainloop()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        if not _docs_shot_active() and not _acquire_single_instance():
            return 0
        run_ui()
        return 0

    cmd = argv[0]
    if cmd in ("-h", "--help", "help"):
        print(
            """usage-float

  python usage_float.py              Start HUD
  python usage_float.py once         Print usage once
  python usage_float.py autostart on|off|status
"""
        )
        return 0

    if cmd == "once":
        once_cfg = load_config()
        providers = fetch_all(
            active_providers(list(once_cfg.get("providers") or DEFAULT_PROVIDERS)),
            force=True,
        )
        for row in iter_display_rows(
            providers, list(once_cfg.get("providers") or DEFAULT_PROVIDERS)
        ):
            if row.failed:
                print(f"{row.title}  更新失败")
                continue
            right = row.summary or ""
            if row.used_pct is not None:
                rem = format_remaining(row.resets_at)
                right = (row.summary or f"{row.used_pct:.0f}%") + (
                    f"  {rem}" if rem else ""
                )
            print(f"{row.title}  {right}".rstrip())
        return 0

    if cmd in ("autostart", "install", "uninstall"):
        if cmd == "install":
            ok, msg = set_autostart(True)
        elif cmd == "uninstall":
            ok, msg = set_autostart(False)
        else:
            sub = argv[1] if len(argv) > 1 else "status"
            if sub == "on":
                ok, msg = set_autostart(True)
            elif sub == "off":
                ok, msg = set_autostart(False)
            else:
                print("enabled" if is_autostart_enabled() else "disabled")
                return 0
        print(msg)
        return 0 if ok else 1

    print(f"Unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        _log_exception("fatal")
        traceback.print_exc()
        try:
            import tkinter as tk
            from tkinter import messagebox

            r = tk.Tk()
            r.withdraw()
            messagebox.showerror("UsageFloat", traceback.format_exc()[-800:])
            r.destroy()
        except Exception:
            pass
        raise
