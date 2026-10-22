# -*- coding: utf-8 -*-
"""微信数据源适配器 · 仁谱 第一个 channel adapter

复用本地已验证的解密流水线（wc_export/fresh_pull.py），不重造轮子、不写死路径。
只读：live 库 → 解密快照 fresh_<date>/ → 仁谱。微信源库本身零写入。
"""
import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core
from config import SELF_WXID, WC_EXPORT, WC_LIVE
from adapters._wechat_decrypt import pull_builtin, find_db_key

EXCLUDE_PREFIX = ("gh_", "@openim", "fake_", "qqmail", "filehelper", "weixin", "tmessage")
EXCLUDE_EXACT = {"qmessage", "notifymessage", "floatbottle", "medianote", "qmsg"}
SERVICE_WORDS = ("服务号", "团队", "助手", "客服", "通知", "提醒", "公众号", "小程序")


def latest_fresh() -> Path:
    """取最新解密快照。

    坑（2026-10-22 实测）：`fresh_20260906` 是 09-06 的无连字符旧命名，
    按名字 sort 会把它排到所有 `fresh_2026-10-xx` 之后 → 选到 3 个月前的库。
    故：① 只认 `fresh_YYYY-MM-DD` 连字符格式 ② 再按目录 mtime 取最新（防手工改名目录）。
    """
    # 优先级：wc_export/<fresh_*> → 仁谱自带 fresh_<date> 在 wc_live 的兄弟目录
    if WC_EXPORT and WC_EXPORT.exists():
        cands = [p for p in WC_EXPORT.glob("fresh_*")
                 if p.is_dir() and re.fullmatch(r"fresh_\d{4}-\d{2}-\d{2}", p.name)]
        if cands:
            return max(cands, key=lambda p: p.stat().st_mtime)
    # 仁谱自带的内联路径：把 fresh_<date>/ 建在 app_home/logs 下（不进 D 盘）
    local = Path(__file__).resolve().parent.parent / "logs" / "fresh"
    if local.exists():
        cands = [p for p in local.glob("fresh_*")
                 if p.is_dir() and re.fullmatch(r"fresh_\d{4}-\d{2}-\d{2}", p.name)]
        if cands:
            return max(cands, key=lambda p: p.stat().st_mtime)
    raise SystemExit(
        "找不到任何 fresh_<YYYY-MM-DD>/ 快照。\n"
        "  解法：先跑 `grm sync --pull`（自动解密最近的微信库）"
    )


def is_excluded(username, nickname="", remark=""):
    u = (username or "").lower()
    if any(u.startswith(p) for p in EXCLUDE_PREFIX) or u in EXCLUDE_EXACT:
        return True
    label = (remark or nickname or "")
    return any(w in label for w in SERVICE_WORDS)


def table_for(username: str) -> str:
    return "Msg_" + hashlib.md5(username.encode()).hexdigest()


def _user_cfg_name():
    """「我」在 person 表里的显示名。config.json 里可写 "self_name" 覆盖；默认「我」。"""
    import config
    return config._user_cfg("self_name") or "我"


def real_message_tables(cur):
    tables = [r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'").fetchall()]
    real = []
    for t in tables:
        cols = [c[1] for c in cur.execute(f"PRAGMA table_info('{t}')").fetchall()]
        if "message_content" in cols and "create_time" in cols:
            real.append(t)
    return real


def pull_fresh():
    """拉最新微信快照。优先级：

    A. 用户配了 wc_export（含 fresh_pull.py）→ subprocess 跑它（用户自带的工具链）
    B. 用户配了 wc_live → 用 仁谱自带 `_wechat_decrypt.py`（任何人能跑，不依赖 external repo）
    C. 都没有 → SystemExit 给出修复指引
    """
    if WC_EXPORT and (WC_EXPORT / "fresh_pull.py").exists():
        py = _find_python_with_pycryptodome()
        r = subprocess.run([py, str(WC_EXPORT / "fresh_pull.py"), "pull"],
                           cwd=str(WC_EXPORT),
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            raise SystemExit(f"解密失败：{r.stdout[-500:]}\n{r.stderr[-500:]}")
        return r.stdout
    if WC_LIVE and WC_LIVE.exists():
        # 仁谱自带路径 —— fresh 落在 app_home/logs/fresh/
        import datetime
        fresh_dir = (Path(__file__).resolve().parent.parent / "logs"
                     / f"fresh_{datetime.date.today().isoformat()}")
        key = find_db_key(WC_LIVE)
        pull_builtin(WC_LIVE, fresh_dir, key)
        return f"仁谱 内置解密完成：{fresh_dir}\n"
    raise SystemExit(
        "未配置微信源。两种处理：\n"
        "  · 设 wc_live（让 仁谱自带解密器工作）：\n"
        "      在 %LOCALAPPDATA%\\grm\\config.json 写 {\"wc_live\": \"<你的微信 db_storage 路径>\"}\n"
        "  · 设 wc_export（含 fresh_pull.py 的目录）：\n"
        "      {\"wc_export\": \"<路径>\"}  +  把 db_key.txt 放进 wc_export 根目录"
    )


def _find_python_with_pycryptodome() -> str:
    """找有 pycryptodome 的 python（依次尝试：用户配置 / env / 系统 PATH 上的 python）。"""
    import shutil, subprocess
    candidates = [
        os.environ.get("RENPU_PYTHON"),
        shutil.which("python3"), shutil.which("python"),
        str(Path(sys.executable)),
    ]
    for c in candidates:
        if not c:
            continue
        try:
            r = subprocess.run([c, "-c", "from Crypto.Cipher import AES; print('ok')"],
                               capture_output=True, text=True, timeout=5)
            if r.stdout.strip() == "ok":
                return c
        except Exception:
            continue
    raise SystemExit(
        "找不到带 pycryptodome 的 Python。请：\n"
        "  pip install pycryptodome  （或在 venv 里装）\n"
        "  把 RENPU_PYTHON=<带 pycryptodome 的 python 路径> 写进环境变量"
    )


# ───────────────────────────── 摄取主流程 ─────────────────────────────

def ingest(days: int = 90, fresh: Path = None, progress=print):
    fresh = Path(fresh) if fresh else latest_fresh()
    started = core.now()
    progress(f"[1/4] 源快照：{fresh}")

    con_cc = sqlite3.connect(str(fresh / "contact.db")); con_cc.text_factory = str
    con_msg = sqlite3.connect(str(fresh / "message_0.db")); con_msg.text_factory = str
    grm = core.connect()
    cur_m, cur_c = con_msg.cursor(), con_cc.cursor()

    # ── 人 ──
    progress("[2/4] 摄取联系人…")
    people, ids, room_names = 0, {}, {}
    # 「我」在 person 表里的显示名：优先用配置 self_name，否则用自己微信昵称，再兜底「我」
    self_name = _user_cfg_name()
    contacts = cur_c.execute(
        "SELECT username, local_type, alias, remark, nick_name, description, is_in_chat_room "
        "FROM contact WHERE delete_flag=0").fetchall()
    for u, lt, alias, remark, nick, desc, in_room in contacts:
        name = (remark or nick or alias or u)
        if is_excluded(u, nick or "", remark or ""):
            continue
        is_room = 1 if (u or "").endswith("@chatroom") else 0
        if is_room:
            room_names[u] = nick or u          # 群真名在 nick_name（remark 多为空）
            continue                            # 群不是「人」，走 room 表
        if u == SELF_WXID:
            self_name = nick or remark or self_name
        pk = "me" if u == SELF_WXID else f"wx:{u}"   # 自己独立成 me，不与他人同类
        ids[u] = pk
        core.upsert_person(grm, pk, self_name if u == SELF_WXID else name,
                           real_name=None if u == SELF_WXID else (nick if (remark and nick and remark != nick) else None),
                           wechat_type=lt, is_room=0, source="wechat",
                           note="本人" if u == SELF_WXID else None)
        core.upsert_identity(grm, pk, "wechat", u,
                             alias=alias, nickname=nick, remark=remark,
                             signature=None if u == SELF_WXID else desc)
        people += 1
    grm.commit()

    # ── 群 + 成员 ──
    progress("[3/4] 摄取群与群成员…")
    rooms = 0
    # 权威成员源 = chatroom_member（room_id=群 username，member_id=成员 wxid）；
    # ext_buffer 正则只作补漏（protobuf，且返回 bytes，须先 decode）。
    room_members = {}
    for rid, mid in cur_c.execute("SELECT room_id, member_id FROM chatroom_member").fetchall():
        if isinstance(rid, bytes): rid = rid.decode("utf-8", "ignore")
        if isinstance(mid, bytes): mid = mid.decode("utf-8", "ignore")
        room_members.setdefault(rid, set()).add(mid)

    for u, owner, ext in cur_c.execute("SELECT username, owner, ext_buffer FROM chat_room").fetchall():
        if isinstance(u, bytes): u = u.decode("utf-8", "ignore")
        if isinstance(owner, bytes): owner = owner.decode("utf-8", "ignore")
        if isinstance(ext, bytes): ext = ext.decode("utf-8", "ignore")
        raw_members = set(room_members.get(u, set()))
        raw_members |= set(re.findall(r"(wxid_[A-Za-z0-9_\-]+)", ext or ""))
        raw_members.discard(SELF_WXID)
        # 补建仅在群成员名单里出现、contact 表没有的人（如已被删好友的成员）
        for m in raw_members:
            if m not in ids:
                core.upsert_person(grm, f"wx:{m}", m, source="wechat", is_room=0)
                core.upsert_identity(grm, f"wx:{m}", "wechat", m)
                ids[m] = f"wx:{m}"
                people += 1
        rk = f"wxroom:{u}"
        room_name = room_names.get(u) or u
        member_keys = sorted({ids[m] for m in raw_members if m in ids})
        core.upsert_room(grm, rk, name=room_name, channel_id=u,
                         owner=ids.get(owner) if owner else None,
                         member_cnt=len(member_keys), source="wechat")
        for pk in member_keys:
            core.add_room_member(grm, rk, pk)
        ids["room:" + u] = rk        # 群会话走 room_key，不占 person 命名空间
        rooms += 1
    grm.commit()

    # ── 微信标签（contact_fts.search_key 第 3 段 = 逗号分隔标签表） ──
    # 真源优先级：D 盘 live 的 contact_fts.db > 本次快照（旧快照标签表会滞后）
    tags_loaded = 0
    fts = fresh / "contact_fts.db"
    if not fts.exists():
        fts = WC_LIVE / "contact" / "contact_fts.db"
    if fts.exists():
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("fp", WC_EXPORT / "fresh_pull.py")
            fp = importlib.util.module_from_spec(spec); spec.loader.exec_module(fp)
            key = bytes.fromhex((WC_EXPORT / "db_key.txt").read_text().strip())
            tmp = fresh / "contact_fts.db"
            if not tmp.exists():
                if not fp.decrypt_db(fts, tmp, key):
                    tmp = None
            if tmp and tmp.exists():
                fcon = sqlite3.connect(str(tmp)); fcon.text_factory = str
                # 标签名定义表在 contact.db（不在 fts 库）
                label_defs = {r[0] for r in cur_c.execute("SELECT label_name_ FROM contact_label").fetchall()}
                # search_key 按 \x08 分段（2026-10-22 实测 7 段）：
                #   [0]=姓名 [1]=拼音 [2]=备注 [3]=标签(逗号分隔) [4]=微信号alias [5]=地区
                for (key_str,) in fcon.execute("SELECT search_key FROM contact_fts_v5").fetchall():
                    parts = (key_str or "").split("\x08")
                    if len(parts) < 5:
                        continue
                    tagpart = parts[3].strip()
                    alias = parts[4].strip()
                    if not tagpart or not alias:
                        continue
                    r = grm.execute(
                        "SELECT person_key FROM identity WHERE channel='wechat' AND alias=?",
                        (alias,)).fetchone()
                    if not r:
                        continue
                    pid = r[0]
                    for tag_name in [t.strip() for t in tagpart.split(",") if t.strip()]:
                        if label_defs and tag_name not in label_defs:
                            continue    # fts 里可能是旧标签名，不在定义表内则跳过
                        core.tag_person(grm, pid, tag_name, source="wechat_label")
                        tags_loaded += 1
                fcon.close()
                grm.commit()
        except Exception as e:      # 标签是增强项，失败不阻断主流程
            progress(f"  （标签解析跳过：{e}）")
    progress(f"  标签写入 {tags_loaded} 条")

    # ── 消息索引（近 N 天） ──
    since = time.time() - days * 86400
    progress(f"[4/4] 摄取消息索引（近 {days} 天）…")
    rid2u = {r[0]: r[1] for r in cur_m.execute("SELECT rowid, user_name FROM Name2Id").fetchall()}
    real = real_message_tables(cur_m)
    # 表名 → (kind, conv_key)：一次建 O(1) 查；逐个 md5 比对是 O(表数×人数) = 上千万次，慢到不可用
    t2u = {}
    for u in ids:
        if u.startswith("room:"):
            t2u.setdefault(table_for(u[5:]), ("room", ids[u]))
        else:
            t2u.setdefault(table_for(u), ("person", ids[u]))
    total_t = "SELECT COUNT(*), MAX(create_time) FROM {t} WHERE create_time>?"
    total_r = "SELECT create_time, real_sender_id, local_type, message_content FROM {t} WHERE create_time>? ORDER BY create_time"
    # local_type=1 文本才有可读内容；其余是图片/表情/语音/压缩包（message_content 里是二进制）
    # → 只索引文本，避免把 zstd/加密块当文本存进库里污染 FTS（10-22 实测踩到）
    TEXT_TYPES = (1, 3, 47, 42, 10000)

    msgs, done = 0, 0
    d30 = time.time() - 30 * 86400
    core.purge_window(grm, 0)          # 幂等：先清空索引窗口，重跑不膨胀
    grm.commit()
    for t in real:
        done += 1
        if done % 200 == 0:
            progress(f"      {done}/{len(real)} 表… 已索引 {msgs} 条")
        target = t2u.get(t)          # (kind, conv_key)
        if not target:
            continue
        try:
            n, last = cur_m.execute(total_t.format(t=t), (since,)).fetchone()
        except Exception:
            continue
        if not n:
            continue
        kind, conv_key = target
        if kind == "room":
            room_key, person_key = conv_key, None
            title = grm.execute("SELECT name FROM room WHERE room_key=?", (conv_key,)).fetchone()
        else:
            person_key, room_key = conv_key, None
            title = grm.execute("SELECT display_name FROM person WHERE person_key=?", (conv_key,)).fetchone()
        core.upsert_conversation(grm, conv_key, person_key=person_key, room_key=room_key,
                                 title=(title[0] if title else None), msg_total=n, last_ts=last)
        last_snip, last_from = None, 0
        for ts, sid, lt, content in cur_m.execute(total_r.format(t=t), (since,)).fetchall():
            if lt not in TEXT_TYPES:
                continue
            sender_u = rid2u.get(sid, "")
            me = (sender_u == SELF_WXID)
            raw = core.decode_text(content)
            if not raw:
                continue
            if core.add_message(grm, conv_key, ts, "me" if me else ids.get(sender_u, f"wx:{sender_u}"),
                                1 if me else 0, lt, raw):
                msgs += 1
            if ts == last:
                last_snip, last_from = core.clean_text(raw)[:60], (1 if me else 0)
        if last_snip:
            core.upsert_conversation(grm, conv_key, last_msg=last_snip, last_from_me=last_from)
        if last and last > d30:
            if room_key:
                grm.execute("UPDATE room SET msg_d30=COALESCE(msg_d30,0)+?, last_active=? WHERE room_key=?",
                            (n, last, room_key))
            elif person_key and person_key != "me":
                grm.execute("UPDATE person SET msg_d30=COALESCE(msg_d30,0)+?, last_active=?, "
                            "msg_total=COALESCE(msg_total,0)+? WHERE person_key=?", (n, last, n, person_key))
        grm.commit()

    # ── 群共现 → 人际边 ──
    progress("… 推导群共现关系边")
    edges = 0
    for rk, n_members in grm.execute(
            "SELECT room_key, COUNT(*) c FROM room_member GROUP BY room_key HAVING c BETWEEN 2 AND 60").fetchall():
        members = [r[0] for r in grm.execute("SELECT person_key FROM room_member WHERE room_key=?", (rk,)).fetchall()]
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                core.add_edge(grm, members[i], members[j], "co_group", 1.0, [rk])
                edges += 1
                if edges > 200000:
                    break
    grm.commit()

    # ── 审计 ──
    grm.execute(
        "INSERT INTO sync_log(source,src_path,started_at,ended_at,rows_people,rows_msgs,note) "
        "VALUES(?,?,?,?,?,?,?)",
        ("wechat", str(fresh), started, core.now(), people, msgs,
         f"rooms={rooms} edges={edges} days={days}"))
    grm.commit()
    st = core.stats(grm)
    con_cc.close(); con_msg.close(); grm.close()
    return {"fresh": str(fresh), "people": people, "rooms": rooms,
            "messages": msgs, "edges": edges, "stats": st}