# -*- coding: utf-8 -*-
"""仁谱 Renpu · 核心库 · 渠道无关的存储层

对外只暴露：people / conversations / messages / events / edges / todos 六个域。
任何上游渠道（微信→飞书→邮件）只写这些域，上层 agent 无需感知来源。
"""
import json
import sqlite3
import time
from pathlib import Path

from config import DB_PATH, SCHEMA, SNIPPET_MAX


def now() -> int:
    return int(time.time())


def connect(db: Path = None) -> sqlite3.Connection:
    db = Path(db) if db else DB_PATH
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_db(db: Path = None) -> None:
    con = connect(db)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.commit()
    con.close()


# ───────────────────────── 人 ─────────────────────────

def upsert_person(con, person_key, display_name, **kw):
    cur = con.execute("SELECT person_key FROM person WHERE person_key=?", (person_key,))
    exists = cur.fetchone() is not None
    fields = dict(kw)
    if exists:
        sets, vals = [], []
        for k, v in fields.items():
            if v is not None:
                sets.append(f"{k}=?"); vals.append(v)
        if sets:
            vals.append(person_key)
            con.execute(f"UPDATE person SET {','.join(sets)}, updated_at=? WHERE person_key=?", vals + [now()])
    else:
        cols = ["person_key", "display_name"] + list(fields.keys()) + ["created_at", "updated_at"]
        vals = [person_key, display_name] + list(fields.values()) + [now(), now()]
        ph = ",".join("?" * len(cols))
        con.execute(f"INSERT OR REPLACE INTO person({','.join(cols)}) VALUES({ph})", vals)
    return person_key


def get_person(con, name_or_key):
    """按 person_key 精确查，否则按 display_name / real_name 模糊查。"""
    r = con.execute("SELECT * FROM person WHERE person_key=?", (name_or_key,)).fetchone()
    if r:
        return r
    return con.execute(
        "SELECT * FROM person WHERE display_name LIKE ? OR real_name LIKE ? OR display_name=? LIMIT 1",
        (f"%{name_or_key}%", f"%{name_or_key}%", name_or_key),
    ).fetchone()


# ───────────────────────── 身份 / 标签 ─────────────────────────

def upsert_identity(con, person_key, channel, channel_id, **kw):
    cols = ["person_key", "channel", "channel_id"] + list(kw.keys())
    vals = [person_key, channel, channel_id] + list(kw.values())
    ph = ",".join("?" * len(cols))
    con.execute(f"INSERT OR REPLACE INTO identity({','.join(cols)}) VALUES({ph})", vals)


def upsert_tag(con, name, source="ai", parent=None):
    tag_id = f"{source}:{name}"
    con.execute(
        "INSERT INTO tag(tag_id,name,source,parent,created_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(tag_id) DO UPDATE SET name=excluded.name",
        (tag_id, name, source, parent, now()),
    )
    return tag_id


def tag_person(con, person_key, name, source="ai"):
    con.execute(
        "INSERT OR IGNORE INTO person_tag(person_key,tag_id,source) VALUES(?,?,?)",
        (person_key, upsert_tag(con, name, source), source),
    )


def person_tags(con, person_key):
    rows = con.execute(
        "SELECT t.name, pt.source FROM person_tag pt JOIN tag t ON t.tag_id=pt.tag_id WHERE pt.person_key=?",
        (person_key,),
    ).fetchall()
    return [r["name"] for r in rows]


# ───────────────────────── 群 / 会话 ─────────────────────────

def upsert_room(con, room_key, **kw):
    con.execute("INSERT OR IGNORE INTO room(room_key, source) VALUES(?,?)",
                (room_key, kw.get("source", "wechat")))
    sets, vals = [], []
    for k in ("name", "channel_id", "owner", "member_cnt", "msg_total", "msg_d30", "last_active", "announcement", "source"):
        if kw.get(k) is not None:
            sets.append(f"{k}=?"); vals.append(kw[k])
    if sets:
        vals.append(room_key)
        con.execute(f"UPDATE room SET {','.join(sets)} WHERE room_key=?", vals)
    else:
        con.execute("INSERT INTO room(room_key,source) VALUES(?,?)", (room_key, kw.get("source", "wechat")))


def add_room_member(con, room_key, person_key):
    con.execute("INSERT OR IGNORE INTO room_member(room_key,person_key) VALUES(?,?)", (room_key, person_key))


def upsert_conversation(con, conv_key, **kw):
    con.execute("INSERT OR IGNORE INTO conversation(conv_key) VALUES(?)", (conv_key,))
    sets, vals = [], []
    for k in ("person_key", "room_key", "title", "msg_total", "last_ts", "last_msg", "last_from_me"):
        if kw.get(k) is not None:
            sets.append(f"{k}=?"); vals.append(kw[k])
    if sets:
        vals.append(conv_key)
        con.execute(f"UPDATE conversation SET {','.join(sets)}, updated_at=? WHERE conv_key=?", vals + [now()])


# ───────────────────────── 消息 ─────────────────────────

def decode_text(raw) -> str:
    """从 message_content 字节里抽出真文本，认不出就返回空串（不硬塞乱码）。

    微信 4.x 的压缩包/加密语音：message_content 里是 zstd/protobuf 二进制，
    utf-8 忽略解码会得到一堆不可见控制字符——这类必须判废，否则污染 FTS 索引。
    """
    import re
    if isinstance(raw, bytes):
        if b"\x00" in raw[:64]:
            return ""                    # 明显二进制（protobuf 头几乎必带 0x00）
        raw = raw.decode("utf-8", "ignore")
    txt = str(raw or "")
    # 控制字符占比过高 = 二进制残留
    ctrl = sum(1 for ch in txt[:200] if ord(ch) < 32 and ch not in "\t\n\r")
    if ctrl > 6:
        return ""
    return txt


def clean_text(txt) -> str:
    """去 XML 标签、压空白、去 emoji 噪声。"""
    import re
    if isinstance(txt, bytes):
        txt = txt.decode("utf-8", "ignore")
    txt = str(txt or "")
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = re.sub(r"&nbsp;", " ", txt)
    return re.sub(r"\s+", " ", txt).strip()


PII_PATTERNS = [
    (r"\b1[3-9]\d{9}\b", "[手机号]"),
    (r"\b\d{17}[\dXx]\b", "[身份证]"),
    (r"\b\d{16,19}\b", "[卡号]"),
]


def mask_pii(txt: str) -> tuple:
    import re
    hit = False
    for pat, rep in PII_PATTERNS:
        if re.search(pat, txt):
            hit = True
            txt = re.sub(pat, rep, txt)
    return txt, hit


def add_message(con, conv_key, ts, sender, direction, mtype, snippet):
    sn, pii = mask_pii(clean_text(snippet)[:SNIPPET_MAX])
    cur = con.execute(
        "INSERT OR IGNORE INTO msg_index(conv_key,ts,sender,dir,mtype,snippet,has_pii) VALUES(?,?,?,?,?,?,?)",
        (conv_key, ts, sender, direction, mtype, sn, 1 if pii else 0),
    )
    if cur.rowcount:   # 仅新增行才写 fts，避免重跑 sync 索引翻倍
        con.execute(
            "INSERT INTO msg_fts(snippet,person_key,conv_key,ts) VALUES(?,?,?,?)",
            (sn, sender, conv_key, ts),
        )
    return cur.rowcount


def purge_window(con, since):
    """清掉旧窗口（幂等重跑）：先删 fts 再删索引，否则 fts 会留孤儿行。"""
    con.execute("DELETE FROM msg_fts WHERE ts >= ?", (since,))
    con.execute("DELETE FROM msg_index WHERE ts >= ?", (since,))
    con.execute("DELETE FROM msg_index WHERE ts < ?", (since,))   # 全量刷新语义
    con.execute("DELETE FROM msg_fts WHERE ts < ?", (since,))
    con.execute("UPDATE person SET msg_d30=0, msg_total=0, last_active=NULL WHERE person_key<>'me'")
    con.execute("UPDATE room SET msg_d30=0, msg_total=0, last_active=NULL")
    con.execute("UPDATE conversation SET msg_total=0, last_ts=NULL, last_msg=NULL")


# ───────────────────────── 事件 / 边 / 待办 ─────────────────────────

def add_event(con, person_key, kind, ts, title=None, detail=None, evidence=None, source="ai"):
    con.execute(
        "INSERT INTO event(person_key,kind,ts,title,detail,evidence,source,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (person_key, kind, ts, title, detail, evidence, source, now()),
    )


def add_edge(con, src, dst, kind, weight=1.0, rooms=None, note=None):
    """写入/累加一条关系边。

    rooms 是「共同出现的群」列表。累加时必须**合并去重**而不是覆盖——
    否则会出现 weight=3 但 rooms 只剩最后一个群的数据不一致
    （权重说 3 个群，群列表却只列 1 个，下游标签会误导人）。
    """
    incoming = json.dumps(rooms or [], ensure_ascii=False)
    con.execute(
        "INSERT INTO edge(src,dst,kind,weight,rooms,note,updated_at) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(src,dst,kind) DO UPDATE SET "
        "  weight=edge.weight+excluded.weight, "
        "  rooms=(SELECT json_group_array(value) FROM ("
        "          SELECT value FROM json_each(edge.rooms) "
        "          UNION "
        "          SELECT value FROM json_each(excluded.rooms)"
        "        )), "
        "  updated_at=excluded.updated_at",
        (src, dst, kind, weight, incoming, note, now()),
    )


def add_todo(con, title, person_key=None, kind="followup", due=None, evidence=None):
    con.execute(
        "INSERT INTO todo(person_key,kind,title,due,status,evidence,created_at) VALUES(?,?,?,?,?,?,?)",
        (person_key, kind, title, due, "open", evidence, now()),
    )


# ───────────────────────── 统计 ─────────────────────────

def search_messages(con, q, limit=30):
    """全文检索消息。

    FTS5 的默认分词器对中文按**单字**切分，整词 MATCH 会命中 0 条
    （实测 demo 库：MATCH '黑客松' → 0，MATCH '黑客松*' → 183）。
    所以：
      1. 关键词加 * 前缀做前缀匹配（中文场景的正确用法）
      2. FTS 仍为 0 时回退 LIKE —— 兜底，保证「搜得到」永远成立
    """
    q = (q or "").strip()
    if not q:
        return []
    # FTS 查询串：双引号包裹 + 前缀通配
    fts_q = '"' + q.replace('"', '""') + '"*'
    rows = []
    try:
        rows = con.execute(
            "SELECT snippet, person_key, conv_key, ts FROM msg_fts "
            "WHERE msg_fts MATCH ? ORDER BY ts DESC LIMIT ?", (fts_q, limit)).fetchall()
    except Exception:
        rows = []          # FTS 语法/索引问题 → 走 LIKE 兜底
    if rows:
        return [dict(r) for r in rows]
    # 兜底：LIKE（不支持中文分词，但保证不搜不到）
    like = f"%{q}%"
    rows = con.execute(
        "SELECT snippet, person_key, conv_key, ts FROM msg_fts "
        "WHERE snippet LIKE ? ORDER BY ts DESC LIMIT ?", (like, limit)).fetchall()
    return [dict(r) for r in rows]


def stats(con):
    def one(sql, *a):
        r = con.execute(sql, a).fetchone()
        return r[0] if r else 0

    return {
        "people": one("SELECT COUNT(*) FROM person"),
        "people_30d": one("SELECT COUNT(*) FROM person WHERE last_active > ?", now() - 30 * 86400),
        "people_7d": one("SELECT COUNT(*) FROM person WHERE last_active > ?", now() - 7 * 86400),
        "rooms": one("SELECT COUNT(*) FROM room"),
        "conversations": one("SELECT COUNT(*) FROM conversation"),
        "messages_indexed": one("SELECT COUNT(*) FROM msg_index"),
        "events": one("SELECT COUNT(*) FROM event"),
        "edges": one("SELECT COUNT(*) FROM edge"),
        "todos_open": one("SELECT COUNT(*) FROM todo WHERE status='open'"),
        "last_sync": (con.execute("SELECT MAX(ended_at) FROM sync_log").fetchone()[0]),
    }