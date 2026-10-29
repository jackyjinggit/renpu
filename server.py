# -*- coding: utf-8 -*-
"""仁谱 Renpu 本地 API —— agent 接入点（仅绑 127.0.0.1，不对外暴露）

设计取舍（为什么不裸开 HTTP 给局域网）：
  人际数据是 PII 红线区。终端 = 本机用户本人；API = 本机 agent（WorkBuddy / 脚本 / 其他窗）。
  绑 127.0.0.1 + 无鉴权是刻意的：同机同用户，不构成新的暴露面；
  跨机访问 = 先过防火墙/SSH，是明确的动作而非默认状态。

用法：
  python server.py --port 8787
  curl http://127.0.0.1:8787/api/people?days=30
  curl http://127.0.0.1:8787/api/who?name=xxx
  curl http://127.0.0.1:8787/api/search?q=黑客松
  curl -X POST http://127.0.0.1:8787/api/note  -d '{"text":"...","name":"xxx","kind":"promise"}'
"""
import argparse
import json
import sqlite3
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, unquote

sys.path.insert(0, str(Path(__file__).resolve().parent))

import core
from config import DEFAULT_PORT, EXPORT_DIR


def _person_rows(con, limit):
    rows = [dict(r) for r in con.execute(
        "SELECT person_key, display_name, tier, circle, org, msg_total, msg_d30, last_active "
        "FROM person ORDER BY last_active DESC LIMIT ?", (limit,)).fetchall()]
    for r in rows:
        r["last_seen"] = time.strftime("%Y-%m-%d", time.localtime(r["last_active"])) if r["last_active"] else None
        r.pop("last_active")
    return rows


def api_stats(con):
    return core.stats(con)


def api_people(con, days=None, q=None, limit=30, share=True):
    where, vals = [], []
    if days:
        where.append("last_active > ?"); vals.append(time.time() - days * 86400)
    if q:
        where.append("(display_name LIKE ? OR real_name LIKE ?)"); vals += [f"%{q}%", f"%{q}%"]
    sql = ("SELECT person_key, display_name, tier, circle, org, msg_total, msg_d30, last_active "
           "FROM person" + (" WHERE " + " AND ".join(where) if where else "")
           + " ORDER BY last_active DESC LIMIT ?")
    rows = [dict(r) for r in con.execute(sql, vals + [limit]).fetchall()]
    for r in rows:
        r["last_seen"] = time.strftime("%Y-%m-%d", time.localtime(r["last_active"])) if r["last_active"] else None
        r["tags"] = core.person_tags(con, r["person_key"])
        r.pop("last_active")
    # 🔴 脱敏：person_key 在真实库里是 wxid_xxx（能加人的稳定标识）
    if share and not _is_demo_store(con):
        for i, r in enumerate(rows, 1):
            r["person_key"] = f"关系人{i}"
            if r.get("display_name"):
                r["display_name"] = f"关系人{i}"
    return {"count": len(rows), "rows": rows}


def api_who(con, name, limit=30, share=True):
    # 🔴 同 search/feed：who 返回**消息正文**（snippet）——
    #    2026-10-29 真库公网扫描实测命中 16 个 wxid + 6 个真名。
    #    真实库 + share 模式下禁用（公网 Agent 用演示库）。
    if share and not _is_demo_store(con):
        raise ApiError("who 端点在真实库上已禁用（消息正文含 PII）。"
                       "公网 Agent 请用演示库；本地调试加 share=0。")
    p = core.get_person(con, name)
    if not p:
        return {"found": False, "query": name,
                "candidates": [r[0] for r in con.execute(
                    "SELECT display_name FROM person WHERE display_name LIKE ? LIMIT 8", (f"%{name}%",))]}
    pk = p["person_key"]
    ident = con.execute("SELECT * FROM identity WHERE person_key=?", (pk,)).fetchone()
    msgs = [dict(r) for r in con.execute(
        "SELECT ts, dir, sender, snippet FROM msg_index WHERE conv_key=? ORDER BY ts DESC LIMIT ?", (pk, limit)).fetchall()]
    for m in msgs:
        m["when"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(m["ts"]))
        m["who"] = "我" if m["dir"] else (con.execute(
            "SELECT display_name FROM person WHERE person_key=?", (m["sender"],)).fetchone() or ["?"])[0]
        m.pop("ts", None); m.pop("dir", None); m.pop("sender", None)
    if share and not _is_demo_store(con):
        p = dict(p)
        p["person_key"] = "关系人"
        if p.get("display_name"):
            p["display_name"] = "关系人"
        if p.get("real_name"):
            p["real_name"] = "关系人"
    return {"found": True, "profile": dict(p), "tags": core.person_tags(con, pk),
            "identity": dict(ident) if ident else None, "recent": msgs,
            "rooms": [r[0].split(":", 1)[-1] for r in con.execute(
                "SELECT room_key FROM room_member WHERE person_key=?", (pk,)).fetchall()],
            "edges": [dict(r) for r in con.execute(
                "SELECT dst AS \"to\", kind, weight FROM edge WHERE src=? ORDER BY weight DESC LIMIT 15", (pk,)).fetchall()],
            "todos": [dict(r) for r in con.execute(
                "SELECT todo_id, kind, title, due FROM todo WHERE person_key=? AND status='open'", (pk,)).fetchall()],
            "events": [dict(r) for r in con.execute(
                "SELECT kind, ts, title, detail FROM event WHERE person_key=? ORDER BY ts DESC LIMIT 10", (pk,)).fetchall()]}


def api_search(con, q, limit=30, share=True):
    # 🔴 搜索结果 = 消息正文（2026-10-29 真库公网扫描实测）：
    #    snippet 里含真名、群名、wxid、原始群 key —— 全都漏出去。
    #    搜索的价值就是**内容本身**，脱敏后等于无效 →
    #    所以真实库 + share 模式下**明确禁用**，而不是给一份没意义的脱敏结果。
    if share and not _is_demo_store(con):
        raise ApiError("search 端点在真实库上已禁用（消息正文含 PII）。"
                       "公网 Agent 请用演示库；本地调试加 share=0。")
    rows = core.search_messages(con, q, limit=limit)
    for r in rows:
        r["when"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["ts"]))
        nm = con.execute("SELECT display_name FROM person WHERE person_key=?", (r["person_key"],)).fetchone()
        r["who"] = nm[0] if nm else r["person_key"]
        r.pop("ts"); r.pop("person_key")
    return {"query": q, "count": len(rows), "rows": rows}


def api_feed(con, days=7, limit=50, share=True):
    # 🔴 同 search：feed 返回消息正文，真实库 + share 模式下禁用。
    if share and not _is_demo_store(con):
        raise ApiError("feed 端点在真实库上已禁用（消息正文含 PII）。"
                       "公网 Agent 请用演示库；本地调试加 share=0。")
    rows = [dict(r) for r in con.execute(
        "SELECT m.ts, m.dir, m.snippet, m.sender FROM msg_index m WHERE m.ts>? ORDER BY m.ts DESC LIMIT ?",
        (time.time() - days * 86400, limit)).fetchall()]
    for r in rows:
        r["when"] = time.strftime("%m-%d %H:%M", time.localtime(r["ts"]))
        nm = con.execute("SELECT display_name FROM person WHERE person_key=?", (r["sender"],)).fetchone()
        r["who"] = "我" if r["dir"] else (nm[0] if nm else r["sender"])
        r.pop("ts"); r.pop("dir"); r.pop("sender")
    return {"days": days, "count": len(rows), "rows": rows}


def api_rooms(con, limit=30, share=True):
    rows = [dict(r) for r in con.execute(
        "SELECT room_key, name, member_cnt, msg_total, last_active FROM room ORDER BY msg_total DESC LIMIT ?",
        (limit,)).fetchall()]
    for r in rows:
        r["last_seen"] = time.strftime("%Y-%m-%d", time.localtime(r["last_active"])) if r["last_active"] else None
        r.pop("last_active")
        # 🔴 脱敏：room_key 是**能加群的稳定标识**（跟 wxid 同性质），
        #    群名也可能是纯数字（'8'/'5'）→ 群名那层脱敏挡不住它。
        #    这个端点现在暴露在公网（ClawHunt webhook），必须默认脱敏。
        if share:
            r["room_key"] = "群"
            src = (r.get("name") or "")
            if (con.execute("SELECT source FROM room WHERE name = ? LIMIT 1", (src,)).fetchone()
                    or ["wechat"])[0] != "demo":
                r["name"] = "群（已脱敏）"
    return {"count": len(rows), "rows": rows}


def api_edges(con, limit=40, share=True):
    rows = [dict(r) for r in con.execute(
        "SELECT src, dst, kind, weight, src_name, dst_name FROM ("
        "SELECT e.src, e.dst, e.kind, e.weight, s.display_name AS src_name, d.display_name AS dst_name "
        "FROM edge e LEFT JOIN person s ON s.person_key=e.src LEFT JOIN person d ON d.person_key=e.dst) "
        "ORDER BY weight DESC LIMIT ?", (limit,)).fetchall()]
    # 🔴 脱敏：src/dst 是 person_key —— 真实库里是 wxid_xxx，
    #    那是**能加人的稳定标识**。这个端点暴露在公网，必须换掉。
    #
    #    按数据源分：demo 的人物名本来就是虚构的 → 保留（保留图谱可读性）；
    #    真实库的名字是真名 → 连 key 带名字一起换。
    if share:
        is_demo = _is_demo_store(con)
        for r in rows:
            if is_demo:
                r["src"] = _demo_key(r["src"])
                r["dst"] = _demo_key(r["dst"])
            else:
                r["src"] = "关系人"
                r["dst"] = "关系人"
                r["src_name"] = "关系人"
                r["dst_name"] = "关系人"
    return {"count": len(rows), "rows": rows}


def _is_demo_store(con):
    """这个库里的人是不是 demo 数据（虚构人物）。"""
    try:
        r = con.execute("SELECT source FROM person WHERE source='demo' LIMIT 1").fetchone()
        return bool(r)
    except Exception:
        return False


def _demo_key(key):
    """demo 库里的 person_key 形如 wx:demo_003 → wx:demo_003（本身不敏感，
    但保留 'me' 这种语义）。真实库里是 wxid_xxx，必须换掉。"""
    k = str(key or "")
    if k.startswith("wxid_") or (k.startswith("wx:") and "demo" not in k):
        return "关系人"
    return k


def api_todos(con):
    return {"rows": [dict(r) for r in con.execute(
        "SELECT t.todo_id, t.title, t.kind, t.due, p.display_name FROM todo t "
        "LEFT JOIN person p ON p.person_key=t.person_key WHERE t.status='open' ORDER BY t.due").fetchall()]}


# ───────────────────────── 新增：档案 / 图谱 / 时间线 ─────────────────────────
#
# 这四个端点解决一件事：Agent 不用自己去翻 HTML 文件就能拿到可视化产物的底层数据。
# share=1（默认）是「给 Agent 看的安全模式」—— 姓名全部换成「当事人 / 关系人 N」，
# 群名、角色、消息量、单位、精确时间一并抹掉。share=0 才是完整明细。

SHARE_CENTER = "当事人"        # 中心人物
SHARE_OTHER = "关系人"         # 其他相关人
SHARE_ROOM = "群（已脱敏）"     # 群名 / 群 key
SHARE_SELF = "我"              # 库主自己（不是当事人，不算 PII）


class ApiError(Exception):
    """调用方式错误 → 直接转成 {"error": ...} 响应，不 500。"""

    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


def _flag(q, key, default=False):
    """解析 0/1 开关参数（也接受 true/false/yes/no）。"""
    if key not in q or q.get(key) is None:
        return default
    v = str(q.get(key)).strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off", ""):
        return False
    raise ApiError(f"参数 {key} 只能是 0 或 1，收到 {q.get(key)!r}")


def _int_param(q, key, default, lo=None, hi=None):
    if key not in q or q.get(key) is None or q.get(key) == "":
        return default
    try:
        v = int(q[key])
    except (TypeError, ValueError):
        raise ApiError(f"参数 {key} 必须是整数，收到 {q.get(key)!r}")
    if lo is not None and v < lo:
        raise ApiError(f"参数 {key} 不能小于 {lo}，收到 {v}")
    if hi is not None and v > hi:
        raise ApiError(f"参数 {key} 不能大于 {hi}，收到 {v}")
    return v


def _share_labels(con, center_key=None):
    """构造 share 模式下的身份遮蔽表。

    返回 (key_label, name_label, scrub)：
      key_label  person_key → 标签（当事人 / 关系人 N / 我）
      name_label 显示名/别名 → 同一套标签
      scrub      把任意文本里的真实姓名替换掉的函数
    """
    people = con.execute(
        "SELECT person_key, display_name, real_name FROM person ORDER BY person_key").fetchall()
    key_label, name_label = {}, {}
    n = 0
    for p in people:
        pk = p["person_key"]
        if pk == "me":
            key_label[pk] = SHARE_SELF
            continue
        if center_key and pk == center_key:
            lb = SHARE_CENTER
        else:
            n += 1
            lb = f"{SHARE_OTHER} {n}"
        key_label[pk] = lb
        for nm in (p["display_name"], p["real_name"]):
            if nm and len(str(nm).strip()) >= 2:
                name_label.setdefault(str(nm), lb)
    # identity 表里还藏着别名 / 昵称 / 备注 / 平台 ID —— 一样是身份信息
    for i in con.execute(
            "SELECT person_key, alias, nickname, remark, channel_id FROM identity").fetchall():
        lb = key_label.get(i["person_key"], SHARE_OTHER)
        for nm in (i["alias"], i["nickname"], i["remark"], i["channel_id"]):
            if nm and len(str(nm).strip()) >= 2:
                name_label.setdefault(str(nm), lb)
    # 中心人物强制覆盖成「当事人」
    if center_key:
        for p in people:
            if p["person_key"] == center_key:
                for nm in (p["display_name"], p["real_name"]):
                    if nm and len(str(nm).strip()) >= 2:
                        name_label[str(nm)] = SHARE_CENTER
    # 长名先替换，避免短名把长名切碎
    items = sorted(((k, v) for k, v in name_label.items() if len(k) >= 2),
                   key=lambda kv: -len(kv[0]))

    def scrub(s):
        if not s:
            return s
        s = str(s)
        for nm, lb in items:
            if nm in s:
                s = s.replace(nm, lb)
        return s

    return key_label, name_label, scrub


def _assert_unique_keys(graph):
    """脱敏后每个节点的 person_key 必须唯一。

    为什么要这道闸（2026-10-26 实证）：
        二次脱敏会把所有 person_key 打成同一个「关系人」→
        多个节点 key 相同 → 边的 src/dst 全指向同一个标签 →
        图的连接关系彻底失真，而且**看不出来**（节点数、边数都正常）。
    """
    keys = [p.get("person_key") for p in (graph.get("nodes") or [])]
    if len(keys) != len(set(keys)):
        seen, dup = set(), set()
        for k in keys:
            if k in seen:
                dup.add(k)
            seen.add(k)
        raise ApiError(f"脱敏后 person_key 不唯一：{sorted(dup)[:3]}")


def _mask_graph(con, graph, center_key=None):
    """share 模式下再擦一层身份：person_key / 显示名 / 群 key。

    render 的 share 只抹群名和角色，节点上的姓名与 person_key 还在；
    这一层是 API 侧的安全闸 —— share=1 的响应里不该出现任何真实姓名。
    """
    key_label, name_label, scrub = _share_labels(con, center_key)
    for p in graph.get("nodes") or []:
        pk = p.get("person_key")
        if pk is not None:
            p["person_key"] = key_label.get(pk, SHARE_OTHER)
        dn = p.get("display_name")
        if dn:
            p["display_name"] = name_label.get(str(dn), scrub(dn))
        # 消息量也是画像的一部分 —— 分享模式一并抹掉
        for k in ("msg_total", "msg_d30"):
            if k in p:
                p[k] = None
        # tier 留着（只有分层等级，不含身份）；circle / role 含身份，抹掉
        for k in ("circle", "role"):
            if k in p:
                p[k] = None
    for l in graph.get("links") or []:
        if l.get("src") is not None:
            l["src"] = key_label.get(l["src"], SHARE_OTHER)
        if l.get("dst") is not None:
            l["dst"] = key_label.get(l["dst"], SHARE_OTHER)
        if l.get("rooms"):
            l["rooms"] = SHARE_ROOM
    for r in graph.get("rooms") or []:
        if r.get("room_key"):
            r["room_key"] = SHARE_ROOM
        if r.get("name"):
            r["name"] = SHARE_ROOM
    if graph.get("center"):
        graph["center"] = key_label.get(graph["center"], SHARE_CENTER)
    return graph


def _is_name_like(nm) -> bool:
    """这个字符串像「名字」吗？—— 决定它该不该进脱敏校验表。

    为什么需要（2026-10-26 真实库实测）：
        库里有一批 display_name 是**纯数字**（'7'、'11'、'123'…）——
        那是微信群里没设昵称的成员占位。如果当成人名去校验，
        页面里任何数字（日期、计数）都会被判成泄漏，接口直接 500。
    """
    t = str(nm).strip()
    if len(t) < 2:
        return False
    # 系统标识不是姓名：'me'（本人）、'wx:demo_*' 这类内部 key
    if t.lower() in ("me", "self"):
        return False
    if t.startswith("wx:"):
        return False
    if not any(ch.isalpha() or ord(ch) > 0x2E80 for ch in t):
        return False
    return True


def _assert_no_pii(con, payload):
    """share 模式的最后一道闸：整份响应扫一遍，出现真实姓名就直接拦下。

    宁可返回错误，也不让 PII 漏出去 —— 静默通过比报错危险得多。
    错误信息里不带姓名本身（否则报错就成了一次泄漏）。
    """
    names = []
    for r in con.execute("SELECT display_name, real_name FROM person").fetchall():
        for nm in (r["display_name"], r["real_name"]):
            if nm and _is_name_like(nm):
                names.append(str(nm))
    for i in con.execute("SELECT alias, nickname, remark, channel_id FROM identity").fetchall():
        for nm in (i["alias"], i["nickname"], i["remark"], i["channel_id"]):
            if nm and _is_name_like(nm):
                names.append(str(nm))
    if not names:
        return
    # ⚠️ 必须**精确比对每个字符串值**，不能用子串包含（2026-10-26 实证）：
    #     子串匹配会把 JSON 键名、英文单词、通用中文词全判成泄漏
    #     （实测 3D 圈接口直接 500：'circle' 既是字段名也是 JSON 里的值形态）。
    values = _string_values(payload)
    hits = {nm for nm in names if nm in values}
    if hits:
        raise ApiError(
            f"脱敏校验失败：响应中仍有 {len(hits)} 处真实姓名，已阻止返回", 500)


def _string_values(obj):
    """递归取出结构里所有字符串值（精确值集合）。"""
    out = set()

    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str):
            t = o.strip()
            if t:
                out.add(t)
                # 长文本里整段等于某个名字的情况
                for part in t.split():
                    if part:
                        out.add(part.strip(".,;:!?，。；：！？、()（）[]【】\"'"))
    walk(obj)
    return out


def _scrub_html_file(con, path, center_key=None):
    """把已生成的 HTML 里的真实姓名抹掉（share 模式专用）。

    render.write_graph_html / render3d.write_graph_3d 的 share 模式只抹群名和角色，
    节点上的显示名与 person_key 仍在 HTML 里 —— 文件落到磁盘就是一次 PII 泄漏。
    这里在 server 侧补一层文本替换，不动 render 模块。

    替换表把所有身份串（显示名 / 别名 / 昵称 / 备注 / 平台 ID / person_key）
    合成一张表统一按长度倒序替换 —— 否则 wx:demo_lin 会被先切成 wx:关系人 1。
    """
    p = Path(path)
    if not p.exists():
        return
    text = p.read_text(encoding="utf-8", errors="replace")
    key_label, name_label, _ = _share_labels(con, center_key)
    table = {}
    for nm, lb in name_label.items():
        table[nm] = lb
    for pk, lb in key_label.items():
        if pk and pk != "me":
            table[pk] = lb
    for nm, lb in sorted(table.items(), key=lambda kv: -len(kv[0])):
        if nm and nm in text:
            text = text.replace(nm, lb)
    p.write_text(text, encoding="utf-8")


def _mask_profile(con, profile, center_key):
    """把一份「未脱敏」的档案字典就地脱敏成 share 形态。

    为什么在 API 层做而不是用 render_profile 的 share=True：
      1. API 的脱敏契约要更严 —— 还要盖住 person_key、群 key、关系人编号；
      2. API 不该依赖 render 模块的 share 实现细节（那边仍在改）；
      3. 拿数（collect_profile）与脱敏分层，本机 / 分享两条路走同一套取数逻辑。
    """
    key_label, name_label, scrub = _share_labels(con, center_key)
    # 中心人物 → 当事人
    profile["name"] = SHARE_CENTER
    # 身份类字段 → 空
    for k in ("tier", "circle", "org", "msg_total"):
        if k in profile:
            profile[k] = None
    profile["tags"] = [scrub(t) for t in (profile.get("tags") or [])]
    # 群名全变同一个标签 → 去重，和 render_profile share 模式的契约一致
    profile["rooms"] = sorted({SHARE_ROOM for _ in (profile.get("rooms") or [])})
    for e in (profile.get("edges") or []):
        nm = e.get("name")
        e["name"] = name_label.get(str(nm), scrub(nm)) if nm else SHARE_OTHER
    for t in (profile.get("todos") or []):
        t["title"] = scrub(t.get("title") or "")
    for ev in (profile.get("events") or []):
        ev["title"] = scrub(ev.get("title") or "")
        ev["detail"] = scrub(ev.get("detail"))
    for m in (profile.get("recent") or []):
        # 精确时间（带时分秒）在分享模式下抹掉，只留天
        w = str(m.get("when") or "")
        m["when"] = w.split(" ", 1)[0]
        who = str(m.get("who") or "")
        m["who"] = name_label.get(who, scrub(who))
        m["snippet"] = scrub(m.get("snippet") or "")
    return profile


def api_profile(con, name, share=True):
    """人物档案的结构化数据（不是 HTML）。share=True → 全脱敏。"""
    import render_profile
    if not name:
        raise ApiError("缺少参数 name（person_key 或显示名）")
    center_key = render_profile.resolve_person(con, name)
    if not center_key:
        out = {"found": False, "query": name}
        if not share:
            # 脱敏模式下连候选名单都不能给 —— 候选名也是身份
            out["candidates"] = [r[0] for r in con.execute(
                "SELECT display_name FROM person WHERE display_name LIKE ? LIMIT 8",
                (f"%{name}%",))]
        return out
    try:
        # 取数一律用未脱敏模式，脱敏由 _mask_profile 在 API 层做（见其 docstring）
        profile = render_profile.collect_profile(con, center_key, share=False)
    except sqlite3.OperationalError as e:
        raise ApiError(f"数据库不可用：{e}", 500)
    except ValueError as e:
        raise ApiError(str(e), 404)
    if share:
        profile = _mask_profile(con, profile, center_key)
    out = {"found": True, "profile": profile}
    if share:
        _assert_no_pii(con, out)
    return out


def api_graph(con, limit=60, share=True, fmt="json"):
    """关系图谱。fmt=json → 图数据；fmt=path → 生成 HTML 并返回绝对路径。"""
    import render
    fmt = str(fmt or "json").lower()
    if fmt not in ("json", "path"):
        raise ApiError("参数 format 只能是 json 或 path")
    if fmt == "path":
        outp = Path(EXPORT_DIR) / "renpu-api-graph.html"
        path, graph = render.write_graph_html(
            con, outp, limit=limit, focus="connected", share=share)
        if share:
            _scrub_html_file(con, path)
        return {"file": str(Path(path).resolve()), "share": bool(share),
                "nodes": len(graph["nodes"]), "links": len(graph["links"]),
                "rooms": len(graph["rooms"])}
    graph = render.collect_graph(con, limit=limit, focus="connected", share=share)
    # ⚠️ 不要在这里再调 _mask_graph：render.collect_graph 的 share 模式
    #    **自己已经脱敏**（2026-10-26 修的漏洞：人名与 person_key 都已替换）。
    #    二次脱敏会把 person_key 全打成同一个「关系人」→
    #    多个节点 key 相同 → 边的连接关系全乱。
    _assert_unique_keys(graph)
    graph["count"] = len(graph.get("nodes") or [])
    graph["share"] = bool(share)
    return graph


def api_graph3d(con, person="", limit=36, share=True, fmt="json"):
    """3D 关系圈。person 为空 → 自动选关系边最多的人当中心。"""
    import render3d
    fmt = str(fmt or "json").lower()
    if fmt not in ("json", "path"):
        raise ApiError("参数 format 只能是 json 或 path")
    center_key = None
    if person:
        center_key = render3d.resolve_center(con, person)
        if not center_key:
            raise ApiError(f"库里找不到这个人：{person}", 404)
    if not center_key:
        # 默认选群共现边最多的人（3D 圈的价值是看圈子结构；
        # direct 边权重是消息条数，按总边数选会永远选到私聊最多的人）
        row = con.execute(
            "SELECT src FROM edge WHERE kind='co_group' "
            "GROUP BY src ORDER BY COUNT(*) DESC LIMIT 1").fetchone()
        if not row:
            row = con.execute(
                "SELECT src FROM edge GROUP BY src ORDER BY COUNT(*) DESC LIMIT 1").fetchone()
        if not row:
            raise ApiError("库里没有关系边，无法生成 3D 视图")
        center_key = row[0]
    if fmt == "path":
        outp = Path(EXPORT_DIR) / "renpu-api-graph3d.html"
        path, g = render3d.write_graph_3d(
            con, outp, center_key=center_key, limit=limit, share=share)
        if share:
            _scrub_html_file(con, path, center_key=center_key)
        return {"file": str(Path(path).resolve()), "share": bool(share),
                "nodes": len(g["nodes"]), "links": len(g["links"])}
    g = render3d.collect_neighbourhood(con, center_key, max_people=limit, share=share)
    if share:
        _assert_unique_keys(g)
        # 🔴 center 字段是**原始 person_key**（真实库里是 wxid_xxx）——
        #    2026-10-29 真库公网扫描实测命中。
        #    节点和边都脱敏了，中心人物却漏了 —— 单独处理。
        _c = g.get("center")
        if _c and _c != "me" and "wxid_" in str(_c):
            g["center"] = "关系人"
    g["count"] = len(g.get("nodes") or [])
    g["share"] = bool(share)
    return g


def api_timeline(con, person="", days=90):
    """消息量按天聚合。person 为空 → 全局。"""
    if days <= 0:
        raise ApiError(f"参数 days 必须大于 0，收到 {days}")
    since = time.time() - days * 86400
    if person:
        import render_profile
        pk = render_profile.resolve_person(con, person)
        if not pk:
            raise ApiError(f"库里找不到这个人：{person}", 404)
        sql = ("SELECT date(ts,'unixepoch','localtime') AS d, COUNT(*) AS c "
               "FROM msg_index WHERE ts > 0 AND ts > ? AND (sender = ? OR conv_key = ?) "
               "GROUP BY d ORDER BY d")
        vals = [since, pk, pk]
    else:
        sql = ("SELECT date(ts,'unixepoch','localtime') AS d, COUNT(*) AS c "
               "FROM msg_index WHERE ts > 0 AND ts > ? GROUP BY d ORDER BY d")
        vals = [since]
    rows = [{"date": r["d"], "count": int(r["c"])}
            for r in con.execute(sql, vals).fetchall() if r["d"]]
    return {"days": days, "count": len(rows), "person": person or None, "rows": rows}


# 端点自发现清单（Agent 不用猜有哪些接口）
ENDPOINTS = [
    {"path": "/api/stats", "method": "GET", "desc": "全局统计：人数、关系边、群数、消息量"},
    {"path": "/api/people", "method": "GET", "desc": "人物列表；参数 days / q / limit"},
    {"path": "/api/who", "method": "GET", "desc": "单人摘要：身份、标签、最近消息、关系、待办、事件；参数 name / limit"},
    {"path": "/api/search", "method": "GET", "desc": "全文搜消息；参数 q / limit"},
    {"path": "/api/feed", "method": "GET", "desc": "最近消息流；参数 days / limit"},
    {"path": "/api/rooms", "method": "GET", "desc": "群列表；参数 limit"},
    {"path": "/api/edges", "method": "GET", "desc": "关系边列表；参数 limit"},
    {"path": "/api/todos", "method": "GET", "desc": "未完成待办清单"},
    {"path": "/api/profile", "method": "GET",
     "desc": "人物档案结构化数据（非 HTML）；参数 name 必填，share=1 默认全脱敏，share=0 明细"},
    {"path": "/api/graph", "method": "GET",
     "desc": "关系图谱；参数 limit / share，format=json（默认）返回图数据，format=path 生成 HTML 并返回绝对路径"},
    {"path": "/api/graph3d", "method": "GET",
     "desc": "3D 关系圈；参数 person（空则自动取关系边最多的人）/ limit / share，format=json|path"},
    {"path": "/api/timeline", "method": "GET",
     "desc": "消息量按天聚合；参数 person（空则全局）/ days"},
    {"path": "/api/endpoints", "method": "GET", "desc": "本清单：所有可用端点及参数说明"},
    {"path": "/api/note", "method": "POST",
     "desc": "记一条事件/待办；body {text, name?, kind?, todo?}"},
    {"path": "/api/todo/close", "method": "POST", "desc": "关掉一条待办；body {todo_id}"},
    {"path": "/api/webhook", "method": "POST",
     "desc": "ClawHunt 回调端点：接收任务，返回结果（Agent 接单）"},
]


def api_endpoints():
    return {"service": "renpu-仁谱", "version": "0.1",
            "count": len(ENDPOINTS), "endpoints": ENDPOINTS}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        # 🔴 查询串必须做 UTF-8 解码（2026-10-29 实测）：
        #    Agent 传中文人名（?name=<中文名>）时，不解码会抛
        #    'ascii' codec can't encode characters → HTTP 500。
        q = {k: unquote(v[0]) for k, v in parse_qs(u.query, encoding="utf-8").items()}
        con = None
        try:
            con = core.connect()
            if u.path in ("/", "/api"):
                self._send({"service": "renpu-仁谱", "version": "0.1",
                            "endpoints": ["stats", "people", "who", "search", "feed", "rooms", "edges", "todos",
                                          "profile", "graph", "graph3d", "timeline", "endpoints"]})
            elif u.path == "/api/stats":
                self._send(api_stats(con))
            elif u.path == "/api/people":
                self._send(api_people(con, days=int(q["days"]) if "days" in q else None,
                                      q=q.get("q"), limit=int(q.get("limit", 30)),
                                      share=_flag(q, "share", True)))
            elif u.path == "/api/who":
                self._send(api_who(con, q.get("name", ""), int(q.get("limit", 30)),
                                   share=_flag(q, "share", True)))
            elif u.path == "/api/search":
                self._send(api_search(con, q.get("q", ""), int(q.get("limit", 30)),
                                      share=_flag(q, "share", True)))
            elif u.path == "/api/feed":
                self._send(api_feed(con, int(q.get("days", 7)), int(q.get("limit", 50)),
                                    share=_flag(q, "share", True)))
            elif u.path == "/api/rooms":
                self._send(api_rooms(con, int(q.get("limit", 30)),
                                     share=_flag(q, "share", True)))
            elif u.path == "/api/edges":
                self._send(api_edges(con, int(q.get("limit", 40)),
                                     share=_flag(q, "share", True)))
            elif u.path == "/api/todos":
                self._send(api_todos(con))
            elif u.path == "/api/endpoints":
                self._send(api_endpoints())
            elif u.path == "/api/profile":
                share = _flag(q, "share", True)
                out = api_profile(con, q.get("name", ""), share=share)
                self._send(out)
            elif u.path == "/api/graph":
                share = _flag(q, "share", True)
                fmt = q.get("format", "json")
                out = api_graph(con, limit=_int_param(q, "limit", 60, lo=1, hi=2000),
                                share=share, fmt=fmt)
                if share and str(fmt).lower() == "json":
                    _assert_no_pii(con, out)
                self._send(out)
            elif u.path == "/api/graph3d":
                share = _flag(q, "share", True)
                fmt = q.get("format", "json")
                out = api_graph3d(con, person=q.get("person", ""),
                                  limit=_int_param(q, "limit", 36, lo=1, hi=500),
                                  share=share, fmt=fmt)
                if share and str(fmt).lower() == "json":
                    _assert_no_pii(con, out)
                self._send(out)
            elif u.path == "/api/timeline":
                self._send(api_timeline(con, person=q.get("person", ""),
                                        days=_int_param(q, "days", 90, lo=1, hi=3650)))
            else:
                self._send({"error": "no such endpoint", "path": u.path}, 404)
        except ApiError as e:
            self._send({"error": str(e)}, e.code)
        except sqlite3.Error as e:
            self._send({"error": f"数据库错误：{e}"}, 500)
        except Exception as e:
            self._send({"error": f"{type(e).__name__}: {e}"}, 500)
        finally:
            if con is not None:
                con.close()

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(n) or b"{}") if n else {}
        con = core.connect()
        try:
            if u.path == "/api/note":
                p = core.get_person(con, payload.get("name", "")) if payload.get("name") else None
                if payload.get("todo"):
                    core.add_todo(con, payload["text"], p["person_key"] if p else None,
                                  kind=payload.get("kind", "followup"))
                else:
                    core.add_event(con, p["person_key"] if p else None,
                                   payload.get("kind", "ai_note"), int(time.time()),
                                   title=payload.get("text"), source="agent")
                con.commit()
                self._send({"ok": True, "kind": "todo" if payload.get("todo") else "event"})
            elif u.path == "/api/todo/close":
                con.execute("UPDATE todo SET status='done', closed_at=? WHERE todo_id=?",
                            (time.time(), payload.get("todo_id")))
                con.commit()
                self._send({"ok": True})
            elif u.path == "/api/webhook":
                # ClawHunt 回调端点：接收任务，返回结果（Agent 接单）
                # 格式：{"task": "...", "input": {...}} → 返回 {"result": "...", "output": {...}}
                task = payload.get("task", "")
                inp = payload.get("input", {})
                if task == "graph":
                    # 生成关系图谱
                    limit = inp.get("limit", 60)
                    share = inp.get("share", True)
                    out = api_graph(con, limit=limit, share=share, fmt="json")
                    self._send({"result": "graph", "output": out})
                elif task == "profile":
                    # 生成人物档案
                    name = inp.get("name", "")
                    share = inp.get("share", True)
                    out = api_profile(con, name, share=share)
                    self._send({"result": "profile", "output": out})
                elif task == "graph3d":
                    # 生成 3D 关系圈
                    person = inp.get("person", "")
                    limit = inp.get("limit", 36)
                    share = inp.get("share", True)
                    out = api_graph3d(con, person=person, limit=limit, share=share, fmt="json")
                    self._send({"result": "graph3d", "output": out})
                elif task == "stats":
                    # 全局统计
                    out = api_stats(con)
                    self._send({"result": "stats", "output": out})
                else:
                    self._send({"error": f"unknown task: {task}"}, 400)
            else:
                self._send({"error": "no such endpoint"}, 404)
        except Exception as e:
            self._send({"error": str(e)}, 500)
        finally:
            con.close()


def serve(port=DEFAULT_PORT):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"仁谱 Renpu API 已启动 → http://127.0.0.1:{port}/api  （仅本机 · Ctrl+C 停）")
    srv.serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve(ap.parse_args().port)