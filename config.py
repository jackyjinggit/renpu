# -*- coding: utf-8 -*-
"""仁谱 Renpu · 配置 · 单一真源，所有路径在此声明

三层回退（次序）：
  1. 环境变量 RENPU_<KEY>（旧的 GRM_<KEY> 仍兼容，v0.1→v0.2 过渡期）
  2. 用户配置  ~/.config/renpu/config.json   （Windows: %LOCALAPPDATA%\\renpu\\config.json）
  3. builtin 默认

这样：① 出厂不配置也能跑 demo；② 用户传他电脑的微信库路径，免改源码；③ CI/自动化能跑。

仁谱 = 人脉的谱系。做的是「他是谁的人」，不是「有什么该做」。
数据主权（默认）：数据区 = 用户级 vault 路径，不在代码仓内，不入 git。
用户的存储位置由用户自己决定，CLI 不替用户拍板。
"""
import json
import os
import sys
from pathlib import Path

APP = "renpu"          # 数据/配置目录名
APP_LEGACY = "grm"     # v0.1 及更早的目录名（读兼容）


def _user_config_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return base / APP


def _user_data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    return base / APP


def _load_user_config() -> dict:
    """新目录优先；找不到就读旧的 grm/ 目录（平滑迁移）。"""
    for name in (APP, APP_LEGACY):
        p = _user_config_dir().parent / name / "config.json"
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
    return {}


# ── builtin 默认（仅在用户配置与环境变量都不覆盖时生效） ──
_DEFAULTS = {
    # 仁谱自己代码区（自动定位）
    "app_home": None,
    # 数据库与导出
    "data_home": None,
    "db_path": None,
    "export_dir": None,
    # 微信源
    "wc_live": None,
    "wc_export": None,
    "self_wxid": None,
    # 运行参数
    "default_days": 90,
    "port": 8877,          # 8877 = 人脉（避 8787 的撞车）
    "snippet_max": 200,
    # demo adapter 注入量
    # demo 规模（2026-10-28 按真库口径重做后调大）：
    # 老值 80/2000 实际只生成 10 人 28 边（PEOPLE 硬编码，配置是死的），
    # 上台演示看着像玩具。现在按真库的关系形态造：200 人 / 14 群 / 约 440 边。
    "demo_people": 200,
    "demo_messages": 30000,
}


def _resolve(path: str | Path | None) -> Path | None:
    if path is None:
        return None
    p = Path(os.path.expandvars(os.path.expanduser(str(path))))
    if not p.exists():
        return None
    return p


def _env(key: str):
    """读 RENPU_<KEY>，旧名 GRM_<KEY> 兜底（v0.1 兼容）。"""
    v = os.environ.get("RENPU_" + key.upper())
    if v is not None:
        return v
    return os.environ.get("GRM_" + key.upper())


def _user_cfg(k: str):
    cfg = _load_user_config()
    if k in cfg:
        return cfg[k]
    # 旧 config.json 的 "grm_home" → 新 "app_home"
    legacy_alias = {"app_home": "grm_home"}
    lk = legacy_alias.get(k)
    if lk and lk in cfg:
        return cfg[lk]
    return _env(k)


# ── 自动定位 APP_HOME（自身代码区） ──
_THIS_FILE = Path(__file__).resolve()
APP_HOME = Path(_env("HOME") or _user_cfg("app_home") or _THIS_FILE.parent)
SCHEMA = APP_HOME / "schema.sql"
LOG_DIR = APP_HOME / "logs"
ADAPTERS_DIR = APP_HOME / "adapters"

# ── 数据区 ──
DATA_HOME = Path(_env("DATA_HOME") or _user_cfg("data_home") or _user_data_dir())
DB_PATH = Path(_env("DB_PATH") or _user_cfg("db_path") or (DATA_HOME / "renpu.db"))
EXPORT_DIR = Path(_env("EXPORT_DIR") or _user_cfg("export_dir") or (DATA_HOME / "exports"))
LOG_DIR = Path(_env("LOG_DIR") or _user_cfg("log_dir") or (APP_HOME / "logs"))


def _autodetect_wc_live() -> Path | None:
    """用户没配 wc_live 时，尝试自动探测常见微信数据目录。"""
    found = _resolve(_user_cfg("wc_live"))
    if found:
        return found
    candidates = []
    if sys.platform == "win32":
        roots = []
        for letter in ("C", "D", "E"):
            d = Path(f"{letter}:/Users")
            if d.exists():
                roots.append(d)
        for d in roots:
            for u in d.iterdir():
                for rel in ("Documents/Tencent Files/xwechat_files",
                            "Documents/Tencent Files",
                            "AppData/Local/Tencent Files/xwechat_files"):
                    folder = u / rel
                    if folder.exists():
                        for sub in folder.iterdir():
                            if sub.is_dir() and (sub / "db_storage").exists():
                                candidates.append(sub / "db_storage")
    elif sys.platform == "darwin":
        folder = Path.home() / "Documents" / "Tencent Files"
        if folder.exists():
            for sub in folder.iterdir():
                if (sub / "db_storage").exists():
                    candidates.append(sub / "db_storage")
    return candidates[0] if candidates else None


WC_LIVE = _autodetect_wc_live()
WC_EXPORT = _resolve(_user_cfg("wc_export"))


def _autodetect_self_wxid() -> str | None:
    """从 contact 表猜自己的 wxid（读 live 的 contact.db，不改它）。"""
    if not WC_LIVE:
        return None
    live = WC_LIVE / "contact" / "contact.db"
    if not live.exists():
        return None
    try:
        import sqlite3
        c = sqlite3.connect(str(live))
        r = c.execute(
            "SELECT username FROM contact WHERE username LIKE 'wxid_%' "
            "AND length(username)>10 LIMIT 1"
        ).fetchone()
        c.close()
        return r[0] if r else None
    except Exception:
        return None


SELF_WXID = _env("SELF_WXID") or _user_cfg("self_wxid") or _autodetect_self_wxid() or "wxid_me"

# ── 运行参数 ──
DEFAULT_SYNC_DAYS = int(_user_cfg("default_days") or _DEFAULTS["default_days"])
SNIPPET_MAX = int(_user_cfg("snippet_max") or _DEFAULTS["snippet_max"])
DEFAULT_PORT = int(_user_cfg("port") or _env("PORT") or _DEFAULTS["port"])
DEMO_PEOPLE = int(_user_cfg("demo_people") or _DEFAULTS["demo_people"])
DEMO_MESSAGES = int(_user_cfg("demo_messages") or _DEFAULTS["demo_messages"])

# ── 一律创建 ──
for p in (DATA_HOME, LOG_DIR, EXPORT_DIR):
    p.mkdir(parents=True, exist_ok=True)


def describe() -> dict:
    """给 `renpu doctor` / 安装诊断用。"""
    return {
        "platform": sys.platform,
        "app_home": str(APP_HOME),
        "data_home": str(DATA_HOME),
        "db_path": str(DB_PATH),
        "wc_live": str(WC_LIVE) if WC_LIVE else None,
        "wc_export": str(WC_EXPORT) if WC_EXPORT else None,
        "self_wxid": SELF_WXID,
        "config_file": str(_user_config_dir() / "config.json"),
        "env_overrides": sorted(k for k in os.environ if k.startswith("RENPU_")),
    }