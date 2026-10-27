# -*- coding: utf-8 -*-
"""仁谱 Renpu · 终端 CLI —— agent / 人 双入口

设计约定：
  · 默认输出**文本**给人看，加 `--json` 切结构化（agent 用）
  · 任何命令 --json 都吐结构化，agent 无需解析表格
  · 人名/关键词一律模糊匹配，找不到给候选清单而不是空结果
  · 错误尽可能给可执行指引，而不是 traceback（除非排障时用 `grm doctor --verbose`）
"""
import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path

# Windows portable EXE 在 GBK cmd 下输出中文/符号时不崩。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent))

import core

# 🔴 延迟导入微信适配器（2026-10-24 修）
# 原因：adapters.wechat 顶层 import Crypto（pycryptodome），缺失时**整个 CLI 连
#      doctor/stats 都跑不起来** —— 而这些命令根本不碰微信。
# 修法：只在本模块真正用到 wx 时才 import；缺依赖时给出可执行指引而非 traceback。
try:
    from adapters import wechat as wx
    _WX_IMPORT_ERROR = None
except ModuleNotFoundError as e:  # pragma: no cover - 环境相关
    wx = None
    _WX_IMPORT_ERROR = str(e)


def _require_wx():
    """用到微信适配器时才检查依赖，给出可执行指引。"""
    if wx is None:
        raise SystemExit(
            "微信适配器不可用：缺少 %s\n"
            "  · 仅用演示数据可绕过：renpu ingest --demo\n"
            "  · 或安装依赖：pip install pycryptodome\n"
            "  · 其余命令（stats/who/search/people/edges/rooms/todo）不受影响"
            % _WX_IMPORT_ERROR
        )
    return wx


def out(obj, as_json):
    """agent 用 --json 拿结构；人看默认文本渲染。"""
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
    else:
        _pretty(obj)


# 键的中文短名（渲染用；不改数据，只影响人看的排版）
LABELS = {
    "person_key": "ID", "display_name": "姓名", "tier": "层", "circle": "圈",
    "org": "组织", "role": "角色", "relation": "关系", "msg_total": "消息",
    "msg_d30": "30天", "last_seen": "最近", "last_active": "最近",
    "tags": "标签", "alias": "微信号", "nickname": "昵称", "remark": "备注",
    "snippet": "内容", "when": "时间", "who": "谁", "kind": "类型",
    "title": "标题", "detail": "详情", "weight": "权重", "to": "→",
    "name": "名称", "member_cnt": "成员", "msg_total ": "消息",
    "src_name": "A", "dst_name": "B", "conv_key": "会话", "done": "已办",
}
HIDE_KEYS = {"person_key", "conv_key", "room_key", "last_msg", "signature",
             "channel", "channel_id", "avatar", "created_at", "updated_at",
             "first_seen", "note", "source", "has_pii", "mtype",
             "sender", "msg_d7", "real_name", "src", "dst",
             "todo_id", "id"}


def _fmt_ts(v):
    """unix → YYYY-MM-DD（录屏时裸时间戳很难看）。"""
    try:
        return time.strftime("%Y-%m-%d", time.localtime(float(v)))
    except Exception:
        return v


def _row(r):
    """一行 dict → 干净单行（去内部键、去 wxid、去空值、中文标签）。"""
    if not isinstance(r, dict):
        return f"  ▸ {r}"
    name = (r.get("display_name") or r.get("src_name") or r.get("snippet")
            or r.get("title") or r.get("name") or "")
    if name and len(str(name)) > 46:
        name = str(name)[:46] + "…"
    parts = []
    for k, v in r.items():
        if k in HIDE_KEYS or v in (None, "", 0, False):
            continue
        if k in ("display_name", "src_name", "snippet", "dst_name", "title"):
            continue          # 标题已作行首展示
        # wxid / room_key 一律不出现在人看的输出里（微信不支持 wxid 搜人）
        if isinstance(v, str) and (v.startswith("wx:") or v.startswith("wxroom:")
                                   or v.startswith("wxid_") or v.endswith("@chatroom")):
            continue
        if isinstance(v, (list, tuple)):
            if not v:
                continue
            v = "/".join(str(x) for x in v[:5])
        if k == "weight":
            parts.append(f"共现{int(v)}群")
            continue
        if k == "kind" and v == "co_group":
            continue
        if k == "todo_id":
            continue
        if k in ("last_active", "due"):
            v = _fmt_ts(v) if isinstance(v, (int, float)) and v > 1e8 else v
        parts.append(f"{LABELS.get(k, k)}={v}")
    tail = f"  → {r['dst_name']}" if r.get("dst_name") else ""
    return f"  ▸ {name}   {' · '.join(parts)}{tail}"


def _pretty(o):
    if isinstance(o, dict):
        if isinstance(o.get("rows"), list):
            head = str(o.get("title", "结果")).split("·")[0].strip()
            rows = o["rows"]
            print(f"── {head} · {len(rows)} 条 " + "─" * 3)
            for r in rows:
                print(_row(r))
            return
        # 顶层 title 不重复打印（who/search 的 title 已由上面 sections 表达）
        for k, v in o.items():
            if k in ("title", "candidates", "hint"):
                continue
            if isinstance(v, (dict, list)) and v:
                print(f"{k}:")
                _pretty(v)
            elif k not in HIDE_KEYS and v not in (None, "", 0, False):
                val = _fmt_ts(v) if isinstance(v, (int, float)) and v > 1e8 else v
                print(f"  {LABELS.get(k, k)}: {val}")
        if o.get("candidates"):
            print("  可能的人选: " + " / ".join(o["candidates"]))
        return
    if isinstance(o, list):
        for x in o:
            print(_row(x))
        return
    print(o)


# ─────────────────────── 命令 ───────────────────────

def _data_home():
    """数据目录（DB / 报告 / 图谱都落这里）。"""
    import config
    return Path(config.DATA_HOME)


def cmd_init(a):
    core.init_db()
    print(f"✅ 仁谱库就绪：{core.DB_PATH}")


def cmd_doctor(a):
    """自检：探测环境/依赖/配置/微信源，并把「下一步该做什么」列出来。"""
    import config
    desc = config.describe()
    db_exists = Path(desc["db_path"]).exists()
    issues = []
    rec = []

    try:
        from Crypto.Cipher import AES  # noqa
        crypto_ok = True
    except ImportError:
        crypto_ok = False
        issues.append("缺 pycryptodome（pip install pycryptodome 或在 venv 里）")

    if not config.WC_LIVE and not config.WC_EXPORT:
        rec.append("没配置微信源。第一步：跑 `renpu ingest --demo` 看效果；或者把 wc_live 写到 config.json")

    if not db_exists:
        rec.append(f"库还没建。跑 `renpu init`（数据落 {desc['db_path']}）")

    print(f"── 仁谱 Renpu 体检 · {desc['platform']}")
    print(f"  代码区 .................... {desc['app_home']}")
    print(f"  pycryptodome .............. {'✅' if crypto_ok else '❌'}")
    print(f"  库 路径 ................... {desc['db_path']}  {'✅' if db_exists else '⚠️ 未建'}")
    print(f"  微信 live 库 .............. {desc['wc_live'] or '❌ 未配置'}")
    print(f"  微信 wc_export ............ {desc['wc_export'] or '— 未配置（可选）'}")
    print(f"  自己的 wxid ............... {desc['self_wxid']}")
    print(f"  配置文件 .................. {desc['config_file']}")
    if desc["env_overrides"]:
        print(f"  环境变量覆盖 .............. {', '.join(desc['env_overrides'])}")

    if issues:
        print("\n⚠️ 问题：")
        for x in issues: print(f"   · {x}")
    if rec:
        print("\n👣 下一步建议：")
        for i, x in enumerate(rec, 1):
            print(f"   {i}. {x}")
        print("\n   → 跑 `renpu ingest --demo` 立刻体验（无需任何配置）")
    print(f"\n  {'✅ 一切就绪' if not issues else '⚠️ 需修复'}")


def cmd_adapters(a):
    """列出可用数据源。"""
    print("── 仁谱数据源适配器 ──")
    print("  · wechat     微信 4.x （SQLCipher4 + WAL）  ← 真实使用")
    print("  · demo       合成数据                ← 无配置体验用")
    if not core.connect().execute("SELECT 1 FROM sync_log WHERE source='wechat'").fetchone():
        print("\n  还没有真实摄取记录。先用 demo 跑通流程：renpu ingest --demo")


def cmd_pull(a):
    _require_wx().pull_fresh()


def _set_active_db(db_path):
    """把「当前库」写进用户配置，让后续所有命令都指向它。

    为什么需要（2026-10-26 实测）：
        demo 数据隔离到 <data_home>/demo/ 之后，`init` 建的库在 <data_home>/renpu.db，
        两者不是同一个 → `ingest --demo` 灌完数据，紧接着 `profile 某人` 却说找不到。
        单文件 EXE 里更明显：用户只看到「明明灌了数据，怎么查不到」。

    做法：写 config.json 的 db_path（config.py 已经支持这个字段，用户配置优先于默认值）。
    失败不致命 —— 只是当前库没切，用户仍可用 --db 或环境变量指定。
    """
    try:
        import config
        cfg_path = config._user_config_dir() / "config.json"
        cfg = {}
        if cfg_path.exists():
            try:
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            except Exception:
                cfg = {}
        cfg["db_path"] = str(db_path)
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        # 进程内也要立即生效（config.DB_PATH 是 import 时求值的）
        config.DB_PATH = Path(db_path)
        try:
            import core
            core.DB_PATH = Path(db_path)
        except Exception:
            pass
        return True
    except Exception as exc:
        print(f"（当前库没切成功：{exc}；可用 RENPU_DB_PATH 指定）")
        return False


def cmd_ingest(a):
    """摄取数据。两条路径：
        --demo         合成数据（无需任何配置，全新用户第一跑）
        （默认）        从微信库拉（需 wc_live 或 wc_export，已配置才会成功）
    """
    if a.demo:
        # 演示数据默认灌进**独立的 demo 库**，不占用真实库的位置。
        # 否则用户跑一次 `renpu ingest --demo`，真库位置就被合成数据占了，
        # 后面接真实微信时会混在一起（而且 demo 人名会留在库里）。
        from adapters import demo
        import config
        import core
        # 库位置优先级：
        #   1. 显式设了 RENPU_DB_PATH → 尊重它（用户/测试的明确意图）
        #   2. --in-place → 当前库
        #   3. 默认 → <data_home>/demo/renpu.db（演示专用，不占真实库位置）
        explicit = os.environ.get("RENPU_DB_PATH")
        if getattr(a, "in_place", False) or explicit:
            demo_db = Path(explicit) if explicit else Path(config.DB_PATH)
        else:
            demo_db = _data_home() / "demo" / "renpu.db"
            config.DB_PATH = demo_db
            core.DB_PATH = demo_db
        demo_db.parent.mkdir(parents=True, exist_ok=True)
        core.init_db(demo_db)
        r = demo.ingest(progress=print if not a.json else (lambda *_: None))
        where = "当前库" if getattr(a, "in_place", False) else str(demo_db)
        print(f"✅ 演示数据注入完成：人 {r['people']} · 消息 {r['messages']} · 关系边 {r['edges']}")
        print(f"   位置：{where}")
        if not getattr(a, "in_place", False):
            # 把「当前库」切到 demo 库 —— 否则 demo 数据在一个库里、
            # 而 init/profile/graph 全都查另一个库（真库位），
            # 用户会看到「明明灌了数据，profile 却说找不到这个人」。
            _set_active_db(demo_db)
            print(f"   当前库已切到演示库：{demo_db}")
        out(r, a.json)
        return
    # 真实路径
    wx = _require_wx()
    if a.pull:
        wx.pull_fresh()
    if not (wx.WC_LIVE or wx.WC_EXPORT):
        raise SystemExit(
            "未配置微信源（wc_live 或 wc_export）。\n"
            "  · 想先体验：`renpu ingest --demo`\n"
            "  · 要接真微信：在 %LOCALAPPDATA%\\renpu\\config.json 写：\n"
            "      {\"wc_live\": \"D:/.../db_storage\"}"
        )
    r = wx.ingest(days=a.days, progress=print if not a.json else (lambda *_: None))
    print(f"✅ 摄取完成：人 {r['people']} · 群 {r['rooms']} · 消息索引 {r['messages']} · 关系边 {r['edges']}")
    out(r, a.json)


def cmd_sync(a):
    """向后兼容的旧名 → 等同 `ingest`（无 --demo 时）。"""
    a.demo = False
    cmd_ingest(a)


def cmd_stats(a):
    con = core.connect()
    st = core.stats(con)
    con.close()
    out(st, a.json)


def cmd_people(a):
    con = core.connect()
    where, vals = [], []
    if a.days:
        where.append("last_active > ?"); vals.append(time.time() - a.days * 86400)
    if a.q:
        where.append("(display_name LIKE ? OR real_name LIKE ?)")
        vals += [f"%{a.q}%", f"%{a.q}%"]
    sql = "SELECT person_key, display_name, tier, circle, org, msg_total, msg_d30, last_active FROM person"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY last_active DESC LIMIT {a.limit}"
    rows = [dict(r) for r in con.execute(sql, vals).fetchall()]
    for r in rows:
        r["last_seen"] = time.strftime("%Y-%m-%d", time.localtime(r["last_active"])) if r["last_active"] else None
        r["tags"] = core.person_tags(con, r["person_key"])
        r.pop("last_active")
    con.close()
    out({"title": f"人 · {len(rows)}", "rows": rows}, a.json)


def cmd_who(a):
    con = core.connect()
    p = core.get_person(con, a.name)
    if not p:
        cands = [r["display_name"] for r in con.execute(
            "SELECT display_name FROM person WHERE display_name LIKE ? LIMIT 8", (f"%{a.name}%",))]
        con.close()
        out({"title": f"未找到「{a.name}」", "candidates": cands,
             "hint": "微信库无此人，或用 --name 微信号/备注"}, a.json)
        return
    pk = p["person_key"]
    ident = con.execute(
        "SELECT alias, nickname, remark FROM identity WHERE person_key=?", (pk,)).fetchone()
    conv = con.execute("SELECT * FROM conversation WHERE person_key=? ORDER BY last_ts DESC LIMIT 1", (pk,)).fetchone()
    msgs = [dict(r) for r in con.execute(
        "SELECT ts, sender, dir, snippet FROM msg_index WHERE conv_key=? ORDER BY ts DESC LIMIT ?",
        (pk, a.limit)).fetchall()]
    for m in msgs:
        m["when"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(m["ts"]))
        m["who"] = "我" if m["dir"] else (con.execute("SELECT display_name FROM person WHERE person_key=?", (m["sender"],)).fetchone() or ["?"])[0]
        m.pop("ts"); m.pop("dir"); m.pop("sender")
    rooms = [r["name"] for r in con.execute(
        "SELECT r.name FROM room_member rm JOIN room r ON r.room_key=rm.room_key "
        "WHERE rm.person_key=? ORDER BY r.member_cnt LIMIT 10", (pk,)).fetchall()]
    edges = [{"dst_name": nm, "weight": w} for nm, w in con.execute(
        "SELECT COALESCE(p.display_name, e.dst), e.weight FROM edge e "
        "LEFT JOIN person p ON p.person_key=e.dst WHERE e.src=? "
        "ORDER BY e.weight DESC LIMIT 10", (pk,)).fetchall()]
    tags = core.person_tags(con, pk)
    todos = [dict(r) for r in con.execute(
        "SELECT title,due,kind FROM todo WHERE person_key=? AND status='open'", (pk,)).fetchall()]
    events = [dict(r) for r in con.execute(
        "SELECT kind,ts,title FROM event WHERE person_key=? ORDER BY ts DESC LIMIT 10", (pk,)).fetchall()]
    for e in events:
        e["when"] = time.strftime("%Y-%m-%d", time.localtime(e["ts"]))
        e.pop("ts", None)
    con.close()
    prof = dict(p)
    prof["last_active"] = _fmt_ts(prof["last_active"]) if prof.get("last_active") else None
    ident_d = dict(ident) if ident else None
    if ident_d:
        ident_d.pop("channel", None); ident_d.pop("channel_id", None)
    if ident_d and not any(ident_d.values()):
        ident_d = None
    out({"title": p["display_name"], "profile": prof, "identity": ident_d, "tags": tags,
         "recent": msgs, "rooms": rooms, "edges": edges, "todos": todos, "events": events}, a.json)


def cmd_search(a):
    con = core.connect()
    rows = core.search_messages(con, a.q, limit=a.limit)
    for r in rows:
        r["when"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["ts"]))
        nm = con.execute("SELECT display_name FROM person WHERE person_key=?", (r["person_key"],)).fetchone()
        r["who"] = nm[0] if nm else r["person_key"]
        r.pop("ts"); r.pop("person_key")
    con.close()
    out({"title": f"搜索「{a.q}」· {len(rows)} 条", "rows": rows}, a.json)


def cmd_feed(a):
    con = core.connect()
    since = time.time() - a.days * 86400
    rows = [dict(r) for r in con.execute(
        "SELECT m.ts, m.dir, m.snippet, m.sender, c.person_key FROM msg_index m "
        "JOIN conversation c ON c.conv_key=m.conv_key WHERE m.ts>? ORDER BY m.ts DESC LIMIT ?",
        (since, a.limit)).fetchall()]
    for r in rows:
        r["when"] = time.strftime("%m-%d %H:%M", time.localtime(r["ts"]))
        nm = con.execute("SELECT display_name FROM person WHERE person_key=?", (r["sender"],)).fetchone()
        r["who"] = "我" if r["dir"] else (nm[0] if nm else r["sender"])
        r.pop("ts"); r.pop("dir"); r.pop("sender"); r.pop("person_key")
    con.close()
    out({"title": f"近 {a.days} 天动态 · {len(rows)} 条", "rows": rows}, a.json)


def cmd_note(a):
    """落一条人际事件或待办：agent 看完对话的结论写回。"""
    con = core.connect()
    p = core.get_person(con, a.name) if a.name else None
    if a.todo:
        core.add_todo(con, a.text, p["person_key"] if p else None, kind=a.kind or "followup")
    else:
        core.add_event(con, p["person_key"] if p else None, a.kind or "ai_note",
                       int(time.time()), title=a.text, source="ai")
    con.commit(); con.close()
    print(f"✅ 已写入 {'待办' if a.todo else '事件'}：{a.text}")


def cmd_todo(a):
    con = core.connect()
    if a.done:
        con.execute("UPDATE todo SET status='done', closed_at=? WHERE todo_id=?", (time.time(), a.done))
        con.commit(); con.close(); print(f"✅ 待办 {a.done} 已关闭"); return
    rows = [dict(r) for r in con.execute(
        "SELECT t.todo_id, t.title, t.kind, t.due, p.display_name FROM todo t "
        "LEFT JOIN person p ON p.person_key=t.person_key WHERE t.status='open' ORDER BY t.due").fetchall()]
    con.close()
    out({"title": f"待办 · {len(rows)}", "rows": rows}, a.json)


def cmd_edges(a):
    con = core.connect()
    rows = [dict(r) for r in con.execute(
        "SELECT e.src, e.dst, e.kind, e.weight, s.display_name AS src_name, d.display_name AS dst_name "
        "FROM edge e LEFT JOIN person s ON s.person_key=e.src LEFT JOIN person d ON d.person_key=e.dst "
        "ORDER BY e.weight DESC LIMIT ?", (a.limit,)).fetchall()]
    con.close()
    out({"title": f"关系边 TOP {a.limit}", "rows": rows}, a.json)


def cmd_rooms(a):
    con = core.connect()
    rows = [dict(r) for r in con.execute(
        "SELECT room_key,name,member_cnt,msg_total,last_active FROM room ORDER BY msg_total DESC LIMIT ?",
        (a.limit,)).fetchall()]
    for r in rows:
        r["last_seen"] = time.strftime("%Y-%m-%d", time.localtime(r["last_active"])) if r["last_active"] else None
        r.pop("last_active")
    con.close()
    out({"title": f"群 · {len(rows)}", "rows": rows}, a.json)


def cmd_tour(a):
    """一键体验：零配置走一遍，产出图谱 + 报告（不碰真实数据）。"""
    import runpy
    import sys as _sys
    argv = ["tour.py"] + (["--out", a.out] if a.out else []) + (["--keep"] if a.keep else [])
    _sys.argv = ["tour.py"] + argv[1:]
    if getattr(sys, "frozen", False):
        # Freeze 模式下把可执行的 sibling 明确传给 tour，避免误把
        # _internal/cli.py 当成命令执行。
        candidate = Path(sys.executable).parent.parent / "renpu-cli.exe"
        if candidate.exists():
            import os as _os
            _os.environ["RENPU_CLI_FROZEN_EXE"] = str(candidate)
    runpy.run_path(str(Path(__file__).parent / "scripts" / "tour.py"), run_name="__main__")


def cmd_graph(a):
    """生成关系图谱 HTML（自包含单文件，可直接分享）。"""
    import render
    con = core.connect()
    outp = Path(a.out) if a.out else (_data_home() / "renpu-graph.html")
    path, graph = render.write_graph_html(
        con, outp, limit=a.limit, min_weight=a.min_weight, tier=a.tier,
        title=a.title or "仁谱 · 关系图谱", focus=a.focus, share=a.share)
    con.close()
    if a.json:
        out({"title": "关系图谱", "file": str(path),
             "nodes": len(graph["nodes"]), "links": len(graph["links"]),
             "rooms": len(graph["rooms"])}, True)
    else:
        print(f"✅ 关系图谱已生成：{path}")
        print(f"   {len(graph['nodes'])} 人 · {len(graph['links'])} 条关系边 · {len(graph['rooms'])} 个群")
        print("   用浏览器打开即可（单文件，可直接发给别人）")


def cmd_profile(a):
    """生成人物档案页 HTML（单文件，可直接分享）。"""
    import render_profile
    con = core.connect()
    outp = Path(a.out) if a.out else (_data_home() / "exports" / "renpu-profile.html")
    outp.parent.mkdir(parents=True, exist_ok=True)
    path, prof = render_profile.render_profile(con, a.name, outp, share=a.share)
    con.close()
    if a.json:
        out({"title": "人物档案", "file": str(path), "name": prof["name"],
             "edges": len(prof["edges"]), "todos": len(prof["todos"]),
             "events": len(prof["events"])}, True)
    else:
        print(f"✅ 人物档案已生成：{path}")
        print(f"   {prof['name']} · {len(prof['edges'])} 个关系人 · "
              f"{len(prof['todos'])} 条待办 · {len(prof['events'])} 条事件")
        print("   用浏览器打开即可（单文件，可直接发给别人）")


def cmd_export_vault(a):
    """导出成 Obsidian vault（人脉笔记 + 双链）。

    为什么做这个（2026-10-26 作者定方向）：
        「以 Obsidian 为基底，加插件」—— 不再给仁谱写一套界面，
        而是让**仁谱当数据生产者**，Obsidian 生态当消费端。
        作者本机已装的 3d-graph / breadcrumbs / smart-connections
        吃的都是 vault 里的 markdown + 双链，仁谱的真库正好能生成这种形态。

    产出的 vault 可以直接被 Obsidian 打开，或并进已有的 vault。
    """
    import export_obsidian
    con = core.connect()
    outp = Path(a.out) if a.out else (Path(core.EXPORT_DIR) / "renpu-vault")
    # --share 默认开；--no-share 显式关掉（两者不能同时生效，no-share 优先）
    share = False if getattr(a, "no_share", False) else True
    r = export_obsidian.export_vault(con, outp, limit=a.limit,
                                    min_messages=a.min_messages, share=share)
    con.close()
    if a.json:
        out({"title": "Obsidian vault 导出", "file": r["out_dir"],
             "people": r["people"], "rooms": r["rooms"],
             "links": r["links"], "files": r["files"]}, True)
    else:
        print(f"✅ Obsidian vault 已导出：{r['out_dir']}")
        print(f"   {r['people']} 人 · {r['rooms']} 群 · {r['links']} 条关系边 · {r['files']} 篇笔记")
        print("   用 Obsidian 打开这个文件夹即可（自带 3d-graph / breadcrumbs 就能看图谱）")


def cmd_graph3d(a):
    """生成 3D 关系圈聚光 HTML（单文件，可直接分享）。"""
    import render3d
    con = core.connect()
    outp = Path(a.out) if a.out else (_data_home() / "renpu-graph3d.html")
    path, g = render3d.write_graph_3d(
        con, outp, center_key=a.person, limit=a.limit, share=a.share)
    center = next((n for n in g["nodes"] if n.get("is_center")), None)
    con.close()
    who = center["display_name"] if center else "(未知)"
    if a.json:
        out({"title": "3D 关系圈", "file": str(path), "center": who,
             "nodes": len(g["nodes"]), "links": len(g["links"])}, True)
    else:
        print(f"✅ 3D 关系圈已生成：{path}")
        print(f"   中心：{who} · {len(g['nodes'])} 人 · {len(g['links'])} 条关系边")
        print("   用浏览器打开即可（单文件，可直接发给别人）")


def build_parser():
    """构造 CLI 参数解析器（独立出来，供 main / app.py / tour 复用，避免三处各写一份）。"""
    ap = argparse.ArgumentParser(prog="renpu", description="仁谱 Renpu · 人脉的谱系",
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="""仁谱做什么：
  把你的联系人变成一张谱 —— 谁是谁的人、在哪些群共现、
  什么时候答应过什么、上次聊到哪里。agent 可查可写。

快速上手：
  renpu doctor                  # 自检环境
  renpu init                    # 建库
  renpu ingest --demo           # 零配置，先体验
  renpu tour                    # 一键体验：出图谱 + 报告
  renpu who 林夏              # 查一个人的完整档案
  renpu edges                   # 关系谱（群共现强度）
  renpu graph --share           # 关系图谱（单文件 HTML，可直接分享）
  renpu search 黑客松            # 全文检索
  renpu serve                   # 起本地 API 给 agent
""")
    ap.add_argument("--json", action="store_true", help="输出 JSON（agent 用）")
    ap.add_argument("--verbose", action="store_true", help="错误时打印完整 traceback（排障用）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="建库").set_defaults(fn=cmd_init)
    sub.add_parser("doctor", help="环境自检：配置/依赖/下一步建议").set_defaults(fn=cmd_doctor)
    sub.add_parser("adapters", help="列出可用数据源").set_defaults(fn=cmd_adapters)
    sub.add_parser("pull", help="解密拉取最新微信快照").set_defaults(fn=cmd_pull)

    s = sub.add_parser("ingest", help="摄取数据入库（默认微信库；加 --demo 用演示数据）")
    s.add_argument("--in-place", action="store_true", dest="in_place",
                   help="--demo 时把演示数据灌进当前库（默认灌进独立的 demo 库）")
    s.add_argument("--days", type=int, default=90)
    s.add_argument("--demo", action="store_true", help="用演示合成数据（无需任何配置）")
    s.add_argument("--pull", action="store_true", help="先解密再摄取（仅真实路径）"); s.set_defaults(fn=cmd_ingest)

    s = sub.add_parser("sync", help="[兼容] 旧名 = ingest")
    s.add_argument("--days", type=int, default=90)
    s.add_argument("--pull", action="store_true"); s.set_defaults(fn=cmd_sync)

    sub.add_parser("stats", help="总览统计").set_defaults(fn=cmd_stats)

    s = sub.add_parser("people", help="人列表"); s.add_argument("--days", type=int)
    s.add_argument("-q"); s.add_argument("--limit", type=int, default=30); s.set_defaults(fn=cmd_people)

    s = sub.add_parser("who", help="单人档案：基本信息 + 近期对话 + 群 + 关系谱 + 待办 + 事件")
    s.add_argument("name")
    s.add_argument("--limit", type=int, default=30); s.set_defaults(fn=cmd_who)

    s = sub.add_parser("search", help="全文检索消息"); s.add_argument("q")
    s.add_argument("--limit", type=int, default=30); s.set_defaults(fn=cmd_search)

    s = sub.add_parser("feed", help="近期消息流"); s.add_argument("--days", type=int, default=7)
    s.add_argument("--limit", type=int, default=50); s.set_defaults(fn=cmd_feed)

    s = sub.add_parser("note", help="写回事件/待办"); s.add_argument("text")
    s.add_argument("--name"); s.add_argument("--kind"); s.add_argument("--todo", action="store_true"); s.set_defaults(fn=cmd_note)

    s = sub.add_parser("todo", help="待办清单"); s.add_argument("--done", type=int); s.set_defaults(fn=cmd_todo)

    s = sub.add_parser("edges", help="关系谱：谁跟谁在多少个群里共现")
    s.add_argument("--limit", type=int, default=40); s.set_defaults(fn=cmd_edges)

    s = sub.add_parser("rooms", help="群列表"); s.add_argument("--limit", type=int, default=30); s.set_defaults(fn=cmd_rooms)

    s = sub.add_parser("tour", help="一键体验：零配置走一遍，产出图谱 + 报告")
    s.add_argument("--out", help="输出目录（默认临时目录）")
    s.add_argument("--keep", action="store_true", help="保留演示库")
    s.set_defaults(fn=cmd_tour)

    s = sub.add_parser("graph", help="生成关系图谱 HTML（单文件，可直接分享）")
    s.add_argument("--limit", type=int, default=60, help="最多画多少人")
    s.add_argument("--min-weight", type=float, default=1.0, dest="min_weight",
                   help="共现强度下限（越大边越少）")
    s.add_argument("--tier", help="只看某一层，如 AAA")
    s.add_argument("--focus", choices=["connected", "active"],
                   help="选人策略：connected=按关系边数（默认，推荐）/ active=按近期活跃")
    s.add_argument("--share", action="store_true",
                   help="分享模式：群名脱敏、隐藏消息数（可安全发给别人）")
    s.add_argument("--out", help="输出路径（默认落在数据目录）")
    s.add_argument("--title", help="图谱标题")
    s.set_defaults(fn=cmd_graph)

    s = sub.add_parser("profile", help="生成人物档案页 HTML（单文件）")
    s.add_argument("name", help="人物名字或 person_key")
    s.add_argument("--share", action="store_true",
                   help="分享模式：真名脱敏、时间降级到天（可安全发给别人）")
    s.add_argument("--out", help="输出路径（默认落在数据目录）")
    s.set_defaults(fn=cmd_profile)

    s = sub.add_parser("export-vault", help="导出成 Obsidian vault（人脉笔记 + 双链）")
    s.add_argument("--limit", type=int, default=120, help="最多导出多少人（默认 120）")
    s.add_argument("--min-messages", type=int, default=20, dest="min_messages",
                   help="消息数下限（低于此值不导出，默认 20）")
    s.add_argument("--share", action="store_true", default=True,
                   help="脱敏导出（默认开；用 --no-share 关掉）")
    s.add_argument("--no-share", action="store_true", dest="no_share",
                   help="不脱敏导出（会写真人名与群名，谨慎）")
    s.add_argument("--out", help="输出目录（默认落在导出目录）")
    s.set_defaults(fn=cmd_export_vault)

    s = sub.add_parser("graph3d", help="生成 3D 关系圈聚光 HTML（聚焦某人的关系结构）")
    s.add_argument("--person", help="中心人物的名字或 person_key（默认自动选关系最多的人）")
    s.add_argument("--limit", type=int, default=36, help="最多放多少人进球面（默认 36）")
    s.add_argument("--share", action="store_true",
                   help="分享模式：群名脱敏、隐藏消息数（可安全发给别人）")
    s.add_argument("--out", help="输出路径（默认落在数据目录）")
    s.set_defaults(fn=cmd_graph3d)

    s = sub.add_parser("serve", help="起本地 HTTP API（仅 127.0.0.1）")
    s.add_argument("--port", type=int, default=int(__import__('config').DEFAULT_PORT))
    s.set_defaults(fn=lambda a: __import__('server').serve(a.port))

    return ap


def dispatch(a):
    """执行已解析的命令，并统一处理错误输出。"""
    try:
        a.fn(a)
    except SystemExit:
        raise
    except Exception as e:
        if getattr(a, "verbose", False):
            traceback.print_exc()
        else:
            print(f"\n❌ {e}\n", file=sys.stderr)
            print("💡 跑 `renpu doctor` 看环境诊断。",
                  "想看 traceback 加 `--verbose` 重跑同命令。",
                  file=sys.stderr)
        sys.exit(1)


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    dispatch(a)


if __name__ == "__main__":
    main()