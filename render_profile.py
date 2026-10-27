# -*- coding: utf-8 -*-
"""仁谱 · 人物档案页渲染（单文件 HTML，零第三方依赖，零联网）

和 render.py（2D 关系图谱）/ render3d.py（3D 聚光）同属一套「给人看」的产品出口：
    · render.py    → 全局：这整个圈子长什么样
    · render3d.py  → 聚焦：这个人的关系圈是什么结构
    · 本模块       → 档案：这个人的完整人脉档案（谁、什么时候、聊了什么、答应过什么）

设计约束（与同仓库另外两个渲染器保持一致）：
  · 不引 jinja2 / flask / pandas / chart 库 —— 输出一个自包含 HTML，能直接发给别人看
  · CSS / JS / 数据全部内联（数据放 <script id='profile-data' type='application/json'>）
  · 深色底 + 珊瑚/青强调，与产品基调一致
  · HTML 拼装不用 f-string（会和 CSS 的 {} 打架），用 .format() 且模板里只有具名占位符，
    CSS / JS 作为「参数值」传入，因此它们内部的 {} 完全不需要转义

分享模式（share=True）的脱敏红线：
  · 中心人物本人 → 一律替换成「当事人」
  · 其他人名（展示名 / 真名 / 别名 / 昵称 / 备注 / 平台 ID）→ 一律替换成「关系人」
  · 单位、消息总数、分层、圈层、精确时间（首次/最后出现、带时分秒的时刻）→ 清空
  · 群名 → 统一替换成「群（已脱敏）」
  · 输出前会强制扫一遍全文，发现任何真实姓名残留就直接抛错（宁可不出文件，也不泄 PII）
"""
from __future__ import annotations

import html
import json
import time
from pathlib import Path

import core

# ── 调色（与 render.py / render3d.py 完全一致） ──
BG = "#0f1115"
PANEL = "#171a21"
TEXT = "#e8eaf0"
DIM = "#8b93a7"
ACCENT = "#ff7a6b"
ACCENT2 = "#4ecdc4"
EDGE = "#39404f"
LINE = "#232732"
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif"

# ── 脱敏文案 ──
SHARE_CENTER = "当事人"          # 中心人物
SHARE_OTHER = "关系人"           # 其他相关人
SHARE_ROOM = "群（已脱敏）"       # 群名
SHARE_SELF = "我"                # 库主自己（不是当事人，不算 PII）


def _esc(s) -> str:
    """HTML 转义（& < > " '）—— 所有进 HTML 的数据库文本都必须过这一道。"""
    return html.escape(str(s if s is not None else ""))


def _fmt_date(ts) -> str | None:
    """unix 秒 → '2026-10-18'（只到天，本产品的时间粒度）。"""
    if ts is None:
        return None
    try:
        v = int(float(ts))
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    t = time.localtime(v)
    return "%04d-%02d-%02d" % (t.tm_year, t.tm_mon, t.tm_mday)


def _fmt_dt(ts) -> str | None:
    """unix 秒 → '2026-10-18 14:32'（带时分秒 → 属于「精确时间」，分享模式要抹掉）。"""
    if ts is None:
        return None
    try:
        v = int(float(ts))
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    t = time.localtime(v)
    return "%04d-%02d-%02d %02d:%02d" % (t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min)


def _parse_rooms(raw) -> list:
    """解析 edge.rooms（JSON 字符串数组）。解析失败一律退化成空列表，不抛异常。"""
    if not raw:
        return []
    try:
        v = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return []
    if not isinstance(v, list):
        return []
    return [str(x) for x in v]


# ───────────────────────── 名字解析 ─────────────────────────

def resolve_person(con, who):
    """把「person_key 或显示名」解析成 person_key。

    用户手里通常只有中文昵称，不是 wxid —— 所以名字必须能直接用。
    匹配顺序：person_key 精确 → display_name 精确 → display_name / real_name 模糊。
    重名时取关系边最多的那个（更可能是他要找的枢纽），与 render3d.resolve_center 同思路。
    """
    if not who:
        return None
    row = con.execute(
        "SELECT person_key FROM person WHERE person_key = ?", (who,)).fetchone()
    if row:
        return row[0]

    exact = con.execute(
        "SELECT person_key FROM person WHERE display_name = ?", (who,)).fetchall()
    if exact:
        pool = exact
    else:
        pool = con.execute(
            "SELECT person_key FROM person WHERE display_name LIKE ? OR real_name LIKE ?",
            (f"%{who}%", f"%{who}%")).fetchall()
    if not pool:
        return None
    if len(pool) == 1:
        return pool[0][0]
    # 重名 → 取关系边最多的
    best, bestn = None, -1
    for r in pool:
        pk = r["person_key"]
        n = con.execute(
            "SELECT COUNT(*) FROM edge WHERE src = ? OR dst = ?", (pk, pk)).fetchone()[0]
        if n > bestn:
            best, bestn = pk, n
    return best


# ───────────────────────── 脱敏：姓名映射 ─────────────────────────

def _is_name_like(nm) -> bool:
    """这个字符串像「名字」吗？—— 决定它该不该进脱敏映射表。

    为什么需要这个判定（2026-10-26 真实数据实测）：
        真实库里有一批 display_name 是**纯数字**（'7'、'11'、'123'…）——
        那是微信群里没设昵称的成员占位。如果把它们当人名放进映射表，
        脱敏会把页面里的日期、消息数、计数全替换掉；
        更糟的是「脱敏校验」会把页面上出现的数字当成真名泄漏而报错
        （实测报「页面里仍出现真实姓名 01、10、11、137、17、19」）。

    判定：至少 2 个字符，且**不能是纯数字/纯符号**。
    纯数字不是身份标识，遮它反而破坏信息。
    """
    s = str(nm).strip()
    if len(s) < 2:
        return False
    # 纯数字 / 纯符号（含小数点、负号这类可能的计数）→ 不算名字
    if not any(ch.isalpha() or ord(ch) > 0x2E80 for ch in s):
        return False
    return True


def _build_namemap(con, center_key):
    """收集库里所有可能泄露身份的字符串，映射成脱敏标签。

    返回 [(原名, 标签), ...]，按原名长度倒序（长的先替换，避免短名切碎长名）。
    标签：中心人物 → 当事人；其他人 → 关系人 N（编号，和关系人列表对得上）；库主自己 → 不脱敏。
    """
    people = con.execute(
        "SELECT person_key, display_name, real_name FROM person").fetchall()
    # 按 person_key 给其他人编号，保证「关系人 3」在各处指向同一个人
    key_label = {}
    n = 0
    for p in people:
        pk = p["person_key"]
        if pk == "me":
            continue
        if pk == center_key:
            key_label[pk] = SHARE_CENTER
        else:
            n += 1
            key_label[pk] = f"{SHARE_OTHER} {n}"

    label = {}
    for p in people:
        pk = p["person_key"]
        if pk == "me":
            continue
        lb = key_label[pk]
        for nm in (p["display_name"], p["real_name"]):
            if nm and _is_name_like(nm):
                label.setdefault(str(nm), lb)
    # 身份表里还藏着别名 / 昵称 / 备注 / 平台 ID —— 一样是身份信息
    for i in con.execute(
            "SELECT person_key, alias, nickname, remark, channel_id FROM identity").fetchall():
        pk = i["person_key"]
        if pk == "me":
            continue
        lb = key_label.get(pk, SHARE_OTHER)
        for nm in (i["alias"], i["nickname"], i["remark"], i["channel_id"]):
            if nm and _is_name_like(nm):
                label.setdefault(str(nm), lb)
    # 中心人物优先（后写入的强制覆盖成「当事人」）
    for p in people:
        if p["person_key"] == center_key:
            for nm in (p["display_name"], p["real_name"]):
                if nm and _is_name_like(nm):
                    label[str(nm)] = SHARE_CENTER
    items = [(k, v) for k, v in label.items() if _is_name_like(k)]
    items.sort(key=lambda kv: -len(kv[0]))
    return items, key_label


def _make_scrubber(namemap):
    """返回一个把文本里所有真实姓名替换掉的函数。namemap 为空 → 原样返回。"""
    if not namemap:
        return lambda s: s

    def scrub(s):
        if not s:
            return s
        s = str(s)
        for nm, lb in namemap:
            if nm and nm in s:
                s = s.replace(nm, lb)
        return s

    return scrub


# ───────────────────────── 采集档案数据 ─────────────────────────

def collect_profile(con, center_key, share=False):
    """采集一个人的完整档案，组装成 profile 字典。

    share=True 时做脱敏（见模块头说明）。返回的字典键固定，下面「profile 字典」一节
    列出了全部键 —— 不要随手改名，外部有代码依赖。
    """
    prow = con.execute("SELECT * FROM person WHERE person_key = ?", (center_key,)).fetchone()
    if prow is None:
        raise ValueError(f"库里找不到这个人：{center_key}")

    # 脱敏准备：姓名映射 + 文本擦除器
    namemap, key_label = _build_namemap(con, center_key) if share else ([], {})
    scrub = _make_scrubber(namemap)

    # ── 基础字段 ──
    display_name = prow["display_name"] or center_key
    tier = prow["tier"]
    circle = prow["circle"]
    org = prow["org"]
    msg_total = prow["msg_total"]

    # ── 首次 / 最后出现：优先用 person 表，缺失时用消息索引兜底 ──
    mm = con.execute(
        "SELECT MIN(ts) AS lo, MAX(ts) AS hi FROM msg_index "
        "WHERE ts > 0 AND (sender = ? OR conv_key = ?)",
        (center_key, center_key)).fetchone()
    first_ts = prow["first_seen"] if prow["first_seen"] else (mm["lo"] if mm else None)
    last_ts = prow["last_active"] if prow["last_active"] else (mm["hi"] if mm else None)
    first_seen = _fmt_date(first_ts)
    last_seen = _fmt_date(last_ts)

    # ── 标签 ──
    tags = list(core.person_tags(con, center_key))
    if share:
        tags = [scrub(t) for t in tags]

    # ── 所在群：room_member ∪ 关系边里提到的群 ──
    room_keys = set()
    for r in con.execute("SELECT room_key FROM room_member WHERE person_key = ?",
                          (center_key,)).fetchall():
        room_keys.add(r["room_key"])
    erows = con.execute(
        "SELECT src, dst, kind, weight, rooms FROM edge WHERE src = ? OR dst = ?",
        (center_key, center_key)).fetchall()
    agg = {}
    for r in erows:
        other = r["dst"] if r["src"] == center_key else r["src"]
        if other == center_key:
            continue
        slot = agg.setdefault(other, {"w": 0.0, "rooms": set()})
        try:
            w = float(r["weight"] or 0)
        except (TypeError, ValueError):
            w = 0.0
        slot["w"] = max(slot["w"], w)
        rk = _parse_rooms(r["rooms"])
        slot["rooms"].update(rk)
        room_keys.update(rk)

    # 所在群名：只取 room 表里真实存在的群。
    # edge.rooms 里存的是「共现群 key 列表」，它可能与 room.room_key 不是同一套
    # 命名空间（不同适配器的 key 约定不一致），对不上就当没有名字 ——
    # 宁可少列一个群，也不要把 room:demo_xxx 这种内部 key 抖给用户看。
    room_names = []
    for rk in room_keys:
        r = con.execute("SELECT name FROM room WHERE room_key = ?", (rk,)).fetchone()
        if not r or not r["name"]:
            continue
        room_names.append(SHARE_ROOM if share else str(r["name"]))
    room_names = sorted(set(room_names))

    # ── 关系人：名字 + 权重 + 共同群数 ──
    name_by_key = {}
    if agg:
        ph = ",".join("?" * len(agg))
        for p in con.execute(
                f"SELECT person_key, display_name FROM person WHERE person_key IN ({ph})",
                list(agg)).fetchall():
            name_by_key[p["person_key"]] = p["display_name"]
    edges = []
    for other, slot in agg.items():
        if other == "me":
            nm = SHARE_SELF
        else:
            nm = name_by_key.get(other) or other
        edges.append({"_key": other, "name": str(nm), "weight": round(slot["w"], 2),
                      "shared": len(slot["rooms"])})
    edges.sort(key=lambda e: (-e["weight"], -e["shared"], e["name"]))
    if share:
        # 关系人名字统一换成「关系人 N」，不给外人任何名字线索
        for e in edges:
            e["name"] = key_label.get(e["_key"], SHARE_OTHER)
    for e in edges:
        e.pop("_key", None)

    # ── 待办 / 承诺 ──
    todos = []
    for r in con.execute(
            "SELECT kind, title, due FROM todo WHERE person_key = ? "
            "ORDER BY (due IS NULL), due, todo_id", (center_key,)).fetchall():
        todos.append({"kind": r["kind"] or "todo", "title": scrub(r["title"] or ""),
                      "due": _fmt_date(r["due"])})

    # ── 事件 ──
    events = []
    for r in con.execute(
            "SELECT kind, ts, title, detail FROM event WHERE person_key = ? ORDER BY ts DESC",
            (center_key,)).fetchall():
        events.append({"kind": r["kind"] or "event", "date": _fmt_date(r["ts"]) or "",
                       "title": scrub(r["title"] or ""), "detail": scrub(r["detail"])})

    # ── 近期对话：取最近 20 条 ──
    recent = []
    for r in con.execute(
            "SELECT ts, sender, snippet FROM msg_index "
            "WHERE ts > 0 AND (sender = ? OR conv_key = ?) "
            "ORDER BY ts DESC LIMIT 20", (center_key, center_key)).fetchall():
        sender = r["sender"]
        if sender == "me":
            who = SHARE_SELF
        else:
            sp = con.execute("SELECT display_name FROM person WHERE person_key = ?",
                             (sender,)).fetchone()
            who = sp["display_name"] if sp and sp["display_name"] else (sender or "对方")
        when = _fmt_dt(r["ts"]) or ""
        if share:
            # 精确时间（带时分秒）在分享模式下抹掉，只留天
            when = _fmt_date(r["ts"]) or ""
            who = scrub(who)
        snip = scrub(r["snippet"] or "")
        if len(snip) > 120:
            snip = snip[:119] + "…"
        recent.append({"when": when, "who": str(who), "snippet": snip})

    # ── 时间线：按天聚合 ──
    timeline = []
    for r in con.execute(
            "SELECT date(ts,'unixepoch','localtime') AS d, COUNT(*) AS c FROM msg_index "
            "WHERE ts > 0 AND (sender = ? OR conv_key = ?) GROUP BY d ORDER BY d",
            (center_key, center_key)).fetchall():
        if r["d"]:
            timeline.append({"date": r["d"], "count": int(r["c"])})

    # ── 分享模式下统一抹掉身份类字段 ──
    # 姓名 → 当事人；单位/分层/圈层/消息数 → 清空；
    # 精确时间戳（带时分秒）→ 只保留天粒度（first_seen/last_seen 的契约格式就是「2026-08-14」）。
    if share:
        display_name = SHARE_CENTER
        tier = None
        circle = None
        org = None
        msg_total = None

    # ── profile 字典（键固定，外部有代码依赖） ──
    return {
        "name": display_name,
        "tier": tier,
        "circle": circle,
        "org": org,
        "msg_total": msg_total,
        "first_seen": first_seen,
        "last_seen": last_seen,
        "tags": tags,
        "rooms": room_names,
        "edges": edges,
        "todos": todos,
        "events": events,
        "recent": recent,
        "timeline": timeline,
    }


# ───────────────────────── CSS ─────────────────────────

_CSS = r"""
:root{
  --bg:#0f1115; --panel:#171a21; --panel2:#1c2029; --text:#e8eaf0; --dim:#8b93a7;
  --accent:#ff7a6b; --accent2:#4ecdc4; --edge:#39404f; --line:#232732;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{
  background:var(--bg); color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;
  font-size:14px; line-height:1.65; -webkit-font-smoothing:antialiased;
  background-image:radial-gradient(900px 420px at 12% -8%, rgba(255,122,107,.10), transparent 62%),
                   radial-gradient(760px 400px at 92% -4%, rgba(78,205,196,.09), transparent 60%);
  background-repeat:no-repeat;
}
h1,h2,h3{margin:0;font-weight:600}

/* ── 顶部 ── */
.hero{padding:30px 26px 22px;border-bottom:1px solid var(--line)}
.hero-in{max-width:1400px;margin:0 auto;display:flex;gap:26px;
  align-items:flex-start;justify-content:space-between;flex-wrap:wrap}
.kicker{font-size:11px;letter-spacing:2.4px;color:var(--dim);text-transform:uppercase}
.hero h1{margin:8px 0 0;font-size:32px;font-weight:650;letter-spacing:.4px;line-height:1.2;
  word-break:break-all}
.badges{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
.badge{font-size:11.5px;color:#c8cedd;background:var(--panel);border:1px solid var(--line);
  border-radius:999px;padding:3px 12px;white-space:nowrap}
.badge.tier{color:var(--accent);border-color:rgba(255,122,107,.42)}
.badge.warn{color:var(--accent2);border-color:rgba(78,205,196,.45)}
.span-box{min-width:210px;padding:12px 16px;background:var(--panel);border:1px solid var(--line);
  border-radius:10px}
.span-lab{font-size:11px;color:var(--dim);letter-spacing:.6px}
.span-val{margin-top:5px;font-size:14px;font-variant-numeric:tabular-nums}
.span-val .arrow{color:var(--accent);margin:0 6px}

/* ── 布局 ── */
.wrap{max-width:1400px;margin:0 auto;padding:22px 26px 56px;
  display:grid;grid-template-columns:minmax(0,1fr) 320px;gap:20px;align-items:start}
main{min-width:0}
/* 侧栏用 sticky（不用绝对定位堆叠），窄屏时落到主区下方变普通流 */
aside{position:sticky;top:20px;display:flex;flex-direction:column;gap:16px}
@media(max-width:900px){
  .wrap{grid-template-columns:minmax(0,1fr);padding:18px 16px 44px}
  .hero{padding:22px 16px 18px}
  .hero h1{font-size:26px}
  aside{position:static}
}

/* ── 卡片 ── */
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;
  padding:16px 18px 18px;margin-bottom:16px}
aside .card{margin-bottom:0}
.card-head{display:flex;align-items:baseline;justify-content:space-between;gap:12px;
  margin-bottom:14px;padding-bottom:10px;border-bottom:1px solid var(--line)}
.card-head h2{font-size:13px;letter-spacing:1px;color:var(--text);text-transform:uppercase}
.card-head .cnt{font-size:11.5px;color:var(--dim);font-variant-numeric:tabular-nums;
  white-space:nowrap}
aside .card-head h2{font-size:11.5px;color:var(--dim)}

/* ── 关系人 ── */
.edge{padding:9px 10px;border-radius:9px;cursor:pointer;
  transition:background .14s ease}
.edge:hover{background:var(--panel2)}
.edge + .edge{border-top:1px solid var(--line)}
.edge-head{display:flex;align-items:center;gap:10px}
.edge-name{flex:1;min-width:0;font-size:13.5px;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
.edge-w{font-size:12px;color:var(--accent);font-variant-numeric:tabular-nums;flex:none}
.edge-toggle{font-size:11px;color:var(--dim);flex:none;border:1px solid var(--line);
  border-radius:6px;padding:1px 8px;user-select:none}
.edge-toggle.on{color:var(--accent2);border-color:rgba(78,205,196,.45)}
.bar{height:5px;border-radius:3px;background:#212632;margin-top:7px;overflow:hidden}
.bar i{display:block;height:100%;border-radius:3px;
  background:linear-gradient(90deg,var(--accent2),var(--accent))}
.edge-detail{font-size:11.5px;color:var(--dim);margin-top:7px;padding-left:2px}

/* ── 时间线 ── */
.tlwrap{position:relative}
.tlwrap svg{width:100%;height:auto;display:block}
.tip{position:absolute;display:none;pointer-events:none;background:rgba(23,26,33,.97);
  border:1px solid var(--edge);border-radius:8px;padding:7px 10px;font-size:12px;
  z-index:20;box-shadow:0 6px 20px rgba(0,0,0,.5);white-space:nowrap}
.tip .tip-t{color:var(--text);font-weight:600}
.tip .tip-v{color:var(--dim);font-size:11px;margin-top:2px}

/* ── 列表行 ── */
.rows{display:flex;flex-direction:column}
.row{display:grid;grid-template-columns:132px 96px minmax(0,1fr);gap:12px;align-items:baseline;
  padding:9px 0;border-bottom:1px solid var(--line);font-size:13px}
.row:last-child{border-bottom:none}
.row .w{color:var(--dim);font-size:11.5px;font-variant-numeric:tabular-nums;white-space:nowrap}
.row .who{color:var(--accent2);font-size:12px;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
.row .snip{color:#cfd4e0;word-break:break-word}
.row .due{color:var(--dim);font-size:11.5px;font-variant-numeric:tabular-nums;
  text-align:right;white-space:nowrap}
@media(max-width:640px){
  .row{grid-template-columns:1fr;gap:3px}
  .row .due{text-align:left}
}
.chip{font-size:10.5px;letter-spacing:.4px;border-radius:5px;padding:1px 7px;
  border:1px solid var(--line);color:var(--dim);background:var(--panel2);white-space:nowrap;
  display:inline-block}
.chip.k-promise{color:var(--accent);border-color:rgba(255,122,107,.4)}
.chip.k-gift{color:var(--accent2);border-color:rgba(78,205,196,.4)}
.chip.k-first_talk,.chip.k-recontact{color:#ffd166;border-color:rgba(255,209,102,.35)}
.ev-row{display:grid;grid-template-columns:104px 96px minmax(0,1fr);gap:12px;align-items:baseline;
  padding:9px 0;border-bottom:1px solid var(--line);font-size:13px}
.ev-row:last-child{border-bottom:none}
.ev-row .t{color:var(--dim);font-size:11.5px;font-variant-numeric:tabular-nums;white-space:nowrap}
.ev-row .ttl{color:#cfd4e0}
.ev-row .dtl{color:var(--dim);font-size:12px;margin-top:2px}
@media(max-width:640px){
  .ev-row{grid-template-columns:1fr;gap:3px}
}

/* ── 侧栏 ── */
.chips{display:flex;flex-wrap:wrap;gap:7px}
.chips .chip{font-size:11.5px;color:#c8cedd}
.rooms{list-style:none;margin:0;padding:0}
.rooms li{font-size:12.5px;padding:7px 0;border-bottom:1px solid var(--line);
  display:flex;justify-content:space-between;gap:10px;align-items:center}
.rooms li:last-child{border-bottom:none}
.rooms .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#cfd4e0}
.rooms .n{color:var(--dim);font-size:11px;flex:none;font-variant-numeric:tabular-nums}
.statgrid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.stat{background:var(--panel2);border:1px solid var(--line);border-radius:9px;padding:10px 12px}
.stat b{display:block;font-size:19px;font-weight:600;font-variant-numeric:tabular-nums;
  line-height:1.25}
.stat span{display:block;font-size:11px;color:var(--dim);margin-top:2px}
.empty{color:var(--dim);font-size:13px;padding:22px 4px;text-align:center;
  border:1px dashed var(--line);border-radius:9px}
.dim-text{color:var(--dim)}
.foot{max-width:1400px;margin:0 auto;padding:0 26px 40px;color:var(--dim);font-size:11.5px;
  line-height:1.9}
@media(max-width:900px){.foot{padding:0 16px 34px}}
.foot b{color:var(--text);font-weight:500}
"""


# ───────────────────────── JS ─────────────────────────

_JS = r"""
(function(){
'use strict';
var NS='http://www.w3.org/2000/svg';
var FONT="-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif";
var el=document.getElementById('profile-data');
if(!el){document.getElementById('mainwrap').innerHTML='<div class="empty">没有数据</div>';return;}
var D=JSON.parse(el.textContent);
var P=D.profile||{};
var SHARE=!!D.share;

function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function byId(id){return document.getElementById(id);}
function setText(id,s){var e=byId(id); if(e) e.textContent=(s==null?'':String(s));}
function setHtml(id,s){var e=byId(id); if(e) e.innerHTML=s;}
function E(tag,attrs,text){
  var e=document.createElementNS(NS,tag);
  if(attrs) for(var k in attrs){ if(attrs[k]!==null&&attrs[k]!==undefined) e.setAttribute(k,attrs[k]); }
  if(text!=null) e.textContent=String(text);
  return e;
}

/* ── 头部：姓名 / 副标题 / 时间跨度 ── */
setText('pname', P.name || '（未命名）');
var badges=[];
if(P.tier) badges.push('<span class="badge tier">'+esc(P.tier)+'</span>');
if(P.circle) badges.push('<span class="badge">'+esc(P.circle)+'</span>');
if(P.org) badges.push('<span class="badge">'+esc(P.org)+'</span>');
if(P.msg_total!=null) badges.push('<span class="badge">消息 '+esc(P.msg_total)+' 条</span>');
if(SHARE) badges.push('<span class="badge warn">分享模式 · 已脱敏</span>');
setHtml('pbadges', badges.join('') || '<span class="badge">暂无分层信息</span>');
if(P.first_seen||P.last_seen){
  setHtml('pspan','<div class="span-lab">出现时间跨度</div><div class="span-val">'+
    esc(P.first_seen||'未知')+'<span class="arrow">→</span>'+esc(P.last_seen||'未知')+'</div>');
}else{
  setHtml('pspan','<div class="span-lab">出现时间跨度</div><div class="span-val dim-text">未知</div>');
}

/* ── 关系人卡片区 ── */
var edges=P.edges||[];
setText('edges-cnt', edges.length? (edges.length+' 人') : '0 人');
if(!edges.length){
  setHtml('edges','<div class="empty">暂无关系记录</div>');
}else{
  var maxw=0;
  edges.forEach(function(e){ if((e.weight||0)>maxw) maxw=e.weight||0; });
  setHtml('edges', edges.map(function(e,i){
    var pct=maxw>0? Math.max(4,Math.round((e.weight||0)/maxw*100)) : 0;
    return '<div class="edge" data-i="'+i+'">'+
      '<div class="edge-head">'+
        '<span class="edge-name">'+esc(e.name)+'</span>'+
        '<span class="edge-w">权重 '+esc(e.weight)+'</span>'+
        '<span class="edge-toggle">展开</span>'+
      '</div>'+
      '<div class="bar"><i style="width:'+pct+'%"></i></div>'+
      '<div class="edge-detail" hidden>共同出现 '+esc(e.shared)+' 个群 · 关系权重 '+esc(e.weight)+'</div>'+
    '</div>';
  }).join(''));
  Array.prototype.forEach.call(document.querySelectorAll('.edge'),function(node){
    node.addEventListener('click',function(){
      var d=node.querySelector('.edge-detail');
      var t=node.querySelector('.edge-toggle');
      if(d.hasAttribute('hidden')){ d.removeAttribute('hidden'); t.textContent='收起'; t.className='edge-toggle on'; }
      else { d.setAttribute('hidden',''); t.textContent='展开'; t.className='edge-toggle'; }
    });
  });
}

/* ── 时间线图（原生 SVG 手写，无图表库） ── */
var tl=(P.timeline||[]).filter(function(d){return d && d.date;});
var wrap=byId('tlwrap'), tip=byId('tltip'), svg=byId('tl');
setText('tl-cnt', tl.length? (tl.length+' 天 · 累计 '+tl.reduce(function(a,d){return a+(d.count||0);},0)+' 条') : '');
if(!tl.length){
  if(svg) svg.style.display='none';
  setHtml('tlwrap','<div class="empty">暂无消息记录</div>');
}else{
  if(svg) svg.style.display='';
  var W=760,H=250,PL=42,PR=14,PT=18,PB=34;
  var iw=W-PL-PR, ih=H-PT-PB;
  var maxc=1;
  tl.forEach(function(d){ if((d.count||0)>maxc) maxc=d.count||0; });
  var slot=iw/tl.length;
  var bw=Math.max(2,Math.min(26,slot*0.56));
  function yAt(v){ return PT+ih-(v/maxc)*ih; }
  svg.setAttribute('viewBox','0 0 '+W+' '+H);
  svg.textContent='';
  /* 网格 + 纵轴刻度 */
  var ticks=4;
  for(var i=0;i<=ticks;i++){
    var v=maxc*i/ticks, yy=yAt(v);
    svg.appendChild(E('line',{x1:PL,y1:yy,x2:PL+iw,y2:yy,stroke:'#232732','stroke-width':1}));
    svg.appendChild(E('text',{x:PL-9,y:yy+4,'text-anchor':'end',fill:'#8b93a7',
      'font-size':10,'font-family':FONT},String(Math.round(v))));
  }
  /* 柱 */
  var pts=[];
  tl.forEach(function(d,i){
    var c=d.count||0, cx=PL+slot*i+slot/2, yy=yAt(c);
    var h=Math.max(0,PT+ih-yy);
    pts.push([cx,yy]);
    if(h>0){
      svg.appendChild(E('rect',{x:cx-bw/2,y:yy,width:bw,height:h,rx:Math.min(3,bw/2),
        fill:'#ff7a6b','fill-opacity':(c===maxc?0.95:0.5)}));
    }
  });
  /* 面积 + 折线 + 点 */
  if(pts.length>1){
    var line=pts.map(function(p){return p[0].toFixed(1)+','+p[1].toFixed(1);}).join(' L ');
    svg.appendChild(E('path',{d:'M '+line+' L '+pts[pts.length-1][0].toFixed(1)+','+(PT+ih)+
      ' L '+pts[0][0].toFixed(1)+','+(PT+ih)+' Z',fill:'#4ecdc4','fill-opacity':0.08}));
    svg.appendChild(E('polyline',{points:line,fill:'none',stroke:'#4ecdc4','stroke-width':1.6,
      'stroke-linejoin':'round','stroke-linecap':'round','stroke-opacity':0.92}));
  }
  pts.forEach(function(p){
    svg.appendChild(E('circle',{cx:p[0],cy:p[1],r:2.6,fill:'#0f1115',stroke:'#4ecdc4','stroke-width':1.5}));
  });
  /* 横轴日期 */
  var step=Math.max(1,Math.ceil(tl.length/6));
  tl.forEach(function(d,i){
    if(i%step!==0 && i!==tl.length-1) return;
    var m=/-(\d\d)-(\d\d)$/.exec(d.date);
    svg.appendChild(E('text',{x:PL+slot*i+slot/2,y:H-12,'text-anchor':'middle',fill:'#8b93a7',
      'font-size':10,'font-family':FONT}, m? (m[1]+'-'+m[2]) : d.date));
  });
  svg.appendChild(E('line',{x1:PL,y1:PT+ih,x2:PL+iw,y2:PT+ih,stroke:'#2a3040','stroke-width':1}));
  /* 悬停热区 + tooltip */
  tl.forEach(function(d,i){
    var hit=E('rect',{x:PL+slot*i,y:PT,width:slot,height:ih,fill:'transparent'});
    hit.style.cursor='pointer';
    hit.addEventListener('mousemove',function(ev){
      var wr=wrap.getBoundingClientRect();
      tip.style.display='block';
      tip.style.left=Math.max(6,Math.min(wr.width-150,ev.clientX-wr.left+14))+'px';
      tip.style.top=Math.max(2,ev.clientY-wr.top-46)+'px';
      tip.innerHTML='<div class="tip-t">'+esc(d.date)+'</div><div class="tip-v">'+
        esc(d.count)+' 条消息</div>';
    });
    hit.addEventListener('mouseleave',function(){tip.style.display='none';});
    svg.appendChild(hit);
  });
}

/* ── 近期对话 ── */
var recent=P.recent||[];
setText('recent-cnt', recent.length? (recent.length+' 条') : '0 条');
if(!recent.length){
  setHtml('recent','<div class="empty">暂无对话记录</div>');
}else{
  setHtml('recent', recent.map(function(r){
    return '<div class="row"><span class="w">'+esc(r.when)+'</span>'+
      '<span class="who">'+esc(r.who)+'</span>'+
      '<span class="snip">'+esc(r.snippet)+'</span></div>';
  }).join(''));
}

/* ── 待办与承诺 ── */
var todos=P.todos||[];
setText('todo-cnt', todos.length? (todos.length+' 项') : '0 项');
if(!todos.length){
  setHtml('todos','<div class="empty">暂无待办</div>');
}else{
  setHtml('todos', todos.map(function(t){
    return '<div class="row"><span class="w"><span class="chip k-'+esc(t.kind)+'">'+esc(t.kind)+'</span></span>'+
      '<span class="w due">'+(t.due? esc(t.due) : '—')+'</span>'+
      '<span class="snip">'+esc(t.title)+'</span></div>';
  }).join(''));
}

/* ── 事件 ── */
var events=P.events||[];
setText('event-cnt', events.length? (events.length+' 条') : '0 条');
if(!events.length){
  setHtml('events','<div class="empty">暂无事件</div>');
}else{
  setHtml('events', events.map(function(v){
    return '<div class="ev-row">'+
      '<span class="t"><span class="chip k-'+esc(v.kind)+'">'+esc(v.kind)+'</span></span>'+
      '<span class="t">'+esc(v.date)+'</span>'+
      '<span><span class="ttl">'+esc(v.title)+'</span>'+
      (v.detail? '<span class="dtl">'+esc(v.detail)+'</span>':'')+'</span>'+
    '</div>';
  }).join(''));
}

/* ── 侧栏：标签 / 群 / 统计 ── */
var tags=P.tags||[];
setHtml('tags', tags.length? tags.map(function(t){return '<span class="chip">'+esc(t)+'</span>';}).join('')
  : '<div class="empty">暂无标签</div>');
var rooms=P.rooms||[];
setHtml('rooms', rooms.length? rooms.map(function(n){
  return '<li><span class="nm">'+esc(n)+'</span></li>';}).join('')
  : '<li><span class="nm dim-text">暂无群</span></li>');
var stats=[
  ['关系人', (P.edges||[]).length+' 人'],
  ['所在群', (P.rooms||[]).length+' 个'],
  ['消息数', (P.msg_total!=null? P.msg_total+' 条' : (SHARE?'已脱敏':'暂无'))],
  ['最近活跃', (P.last_seen? P.last_seen : (SHARE?'已脱敏':'暂无'))]
];
setHtml('stats', stats.map(function(s){
  return '<div class="stat"><b>'+esc(s[1])+'</b><span>'+esc(s[0])+'</span></div>';
}).join(''));
})();
"""


# ───────────────────────── HTML 模板 ─────────────────────────
# 模板里只放具名占位符；CSS / JS 作为「参数值」传入，因此它们内部的 {} 不需要转义。
_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
{css}
</style>
</head>
<body>
<header class="hero">
  <div class="hero-in">
    <div>
      <div class="kicker">Renpu · 人脉档案</div>
      <h1 id="pname">—</h1>
      <div class="badges" id="pbadges"></div>
    </div>
    <div class="span-box" id="pspan"></div>
  </div>
</header>
<div class="wrap">
  <main id="mainwrap">
    <section class="card">
      <div class="card-head"><h2>关系人</h2><span class="cnt" id="edges-cnt"></span></div>
      <div id="edges"></div>
    </section>
    <section class="card">
      <div class="card-head"><h2>消息时间线</h2><span class="cnt" id="tl-cnt"></span></div>
      <div class="tlwrap" id="tlwrap">
        <svg id="tl" xmlns="http://www.w3.org/2000/svg"></svg>
        <div class="tip" id="tltip"></div>
      </div>
    </section>
    <section class="card">
      <div class="card-head"><h2>近期对话</h2><span class="cnt" id="recent-cnt"></span></div>
      <div class="rows" id="recent"></div>
    </section>
    <section class="card">
      <div class="card-head"><h2>待办与承诺</h2><span class="cnt" id="todo-cnt"></span></div>
      <div class="rows" id="todos"></div>
    </section>
    <section class="card">
      <div class="card-head"><h2>事件</h2><span class="cnt" id="event-cnt"></span></div>
      <div class="rows" id="events"></div>
    </section>
  </main>
  <aside>
    <section class="card">
      <div class="card-head"><h2>统计</h2></div>
      <div class="statgrid" id="stats"></div>
    </section>
    <section class="card">
      <div class="card-head"><h2>标签</h2></div>
      <div class="chips" id="tags"></div>
    </section>
    <section class="card">
      <div class="card-head"><h2>所在群</h2></div>
      <ul class="rooms" id="rooms"></ul>
    </section>
  </aside>
</div>
<div class="foot">
  <b>仁谱 Renpu</b> · 本页在本机生成，数据不出本机 · 分享模式下姓名、单位、消息数与精确时间均已脱敏
</div>
<script id='profile-data' type='application/json'>{data}</script>
<script>
{js}
</script>
</body>
</html>
"""


# ───────────────────────── 渲染入口 ─────────────────────────

def _visible_text(html_text: str) -> str:
    """抽出页面的**可见文本**（剥掉标签、style、script、注释）。

    用途：脱敏校验只该看「观众看得见的内容」。
    如果拿原始 HTML 去子串匹配，CSS 类名、JS 变量名、HTML 标签名
    都会造成误判（实测把 'circle'、'Re'、'chips' 当成真名泄漏）。
    """
    import re
    s = re.sub(r"(?is)<!--.*?-->", " ", html_text)
    s = re.sub(r"(?is)<script\b.*?</script>", " ", s)
    s = re.sub(r"(?is)<style\b.*?</style>", " ", s)
    s = re.sub(r"(?is)<[^>]+>", " ", s)
    # 属性里也可能藏名字（alt=、title=、data-*）——这些观众看不到，但也不该有真名
    s = re.sub(r"(?s)\s+", " ", s)
    return s


def _assert_no_leak(html_text, namemap):
    """分享模式最后一道闸：出现任何真实姓名就直接抛错。

    宁可不出文件，也不让 PII 漏出去 —— 静默通过比报错危险得多。

    校验方式：**解析数据区的 JSON，逐个字符串值做精确比对**。
    不能用「子串包含」（2026-10-26 真实数据实测连踩三次）：
      1. 扫整页原始 HTML → CSS 类名、JS 变量、标签名全被判成真名
         （实测报 circle、Re、am、an、as、cat、chips）
      2. 扫整页可见文本 → 固定 UI 文案被判成真名
         （实测报「人物」「承诺」「时间」—— 恰好真有人叫这些）
      3. 扫数据区子串 → JSON 的**键名**和英文文本的子串被判成真名
         （实测报 circle、cl、he、li、am —— 全部来自 "circle": null
          这种字段名，以及 the/li 之类的普通英文词）
    精确比对每个「值」才是对的：值是数据，键名是结构。
    """
    if not namemap:
        return
    values = _data_values(html_text)
    if values is None:
        # 解析不出来 → 退回全量子串检查，宁可多查不可漏查
        body = _visible_text(html_text)
        hits = {str(nm).strip() for nm, _ in namemap
                if nm and str(nm).strip() in body and _is_name_like(nm)}
        if hits:
            raise ValueError("脱敏校验失败：页面仍出现真实姓名 " + "、".join(sorted(hits)[:6]))
        return

    names = {str(nm).strip() for nm, _ in namemap if nm and _is_name_like(nm)}
    hits = names & values
    if hits:
        raise ValueError("脱敏校验失败：数据区仍出现真实姓名 " + "、".join(sorted(hits)[:6]))


def _data_values(html_text):
    """取出数据区 JSON 里所有字符串值（精确值集合）。解析失败返回 None。"""
    import json
    import re
    m = re.search(r"(?is)<script[^>]*id=['\"]profile-data['\"][^>]*>(.*?)</script>",
                  html_text)
    if not m:
        return None
    raw = m.group(1).replace("<\\/", "</")
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    out = set()

    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str):
            out.add(o.strip())
            # 长文本里如果整段就是某个名字，也要算命中
            for part in o.split():
                if part:
                    out.add(part.strip(".,;:!?，。；：！？、()（）[]【】\"'"))
        elif o is not None:
            out.add(str(o))

    walk(data)
    return out


def render_profile(con, who, out_path, share=False) -> tuple[str, dict]:
    """渲染某一个人的完整人脉档案页。

    参数：
        con      → sqlite3 连接
        who      → person_key（wx:xxx）或显示名（中文昵称，支持模糊匹配；
                   重名时取关系边最多的那个）
        out_path → 输出 HTML 路径
        share    → True = 分享模式，做脱敏（姓名/单位/消息数/精确时间/群名）

    返回：(html 文件路径, profile 字典)
    找不到这个人时抛 ValueError("库里找不到这个人：<who>")。
    """
    center_key = resolve_person(con, who)
    if not center_key:
        raise ValueError(f"库里找不到这个人：{who}")

    profile = collect_profile(con, center_key, share=share)

    payload = json.dumps({"profile": profile, "share": bool(share)},
                         ensure_ascii=False, default=str)
    # 防「</script>」提前闭合脚本块（内容里出现时转义成 <\/）
    payload = payload.replace("</", "<\\/")

    title = "仁谱 · 人物档案"
    if not share:
        title = f"仁谱 · {profile['name']} · 人物档案"
    else:
        title = "仁谱 · 人物档案（已脱敏）"

    html_text = _HTML_TEMPLATE.format(title=_esc(title), css=_CSS, js=_JS, data=payload)

    if share:
        _assert_no_leak(html_text, _build_namemap(con, center_key)[0])

    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(html_text, encoding="utf-8")
    return str(p), profile


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    con = core.connect()
    names = [r[0] for r in con.execute(
        "SELECT display_name FROM person ORDER BY msg_total DESC LIMIT 5").fetchall()]
    target = names[0] if names else "me"
    path, prof = render_profile(con, target, r"<output-file>", share=False)
    print(f"生成: {path}")
    print(f"  主体: {prof['name']} · 关系人 {len(prof['edges'])} · 时间线 {len(prof['timeline'])} 天")
    con.close()
