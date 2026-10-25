# -*- coding: utf-8 -*-
"""仁谱 Demo Adapter · 没有微信库也能体验功能

让任何人在没有微信 live 库、没有 fresh_ 快照的情况下，
通过 `renpu ingest --demo` 拿到一份看起来真实的演示库。

仅用于产品演示 / 本地体验 / 单元测试。

【脱敏纪律 · 公开仓必读】
数据 100% 合成，**人名一律虚构**（林夏 / 苏见白 / 陈屿 …），不带任何真实联系人。
wxid 全部是 `wx:demo_*` 前缀，一眼可辨是 demo 而非用户真实库。
曾经这里掺过真实人名——公开仓会把私域关系信息写进 git 永久历史，撤不干净，
故改为全虚构。示例人名本身也刻意选得不像真人，减少误认。
"""
import random
import time

import core
from config import DEMO_PEOPLE, DEMO_MESSAGES


# (person_key, 姓名, 分层, 关系描述, 角色, 圈层)  —— 全虚构
PEOPLE = [
    ("wx:demo_lin",  "林夏",   "AAA", "同行",       "researcher",   "学术线"),
    ("wx:demo_su",   "苏见白", "AAA", "mentor",     "AI 教育",      "教育线"),
    ("wx:demo_chen", "陈屿",   "AAA", "协作伙伴",   "工程师",       "产品线"),
    ("wx:demo_zhou", "周斯年", "AAA", "同学",       "data eng",    "同届"),
    ("wx:demo_gu",   "顾南舟", "AAA", "同学",       "AI 工程",      "同届"),
    ("wx:demo_shen", "沈迟",   "Aa",  "学姐",       "NLP",          "学术线"),
    ("wx:demo_jiang", "江白露", "Aa",  "顾问",       "AI 公司",      "行业线"),
    ("wx:demo_xu",   "许聿白", "a",   "网友",       "developer",    "线上"),
    ("wx:demo_luo",  "罗青禾", "a",   "队友",       "AI/RL",        "黑客松线"),
    ("wx:demo_wen",  "温屿声", "a",   "资源方",     "数据销售",     "资源线"),
]

EXTRA_DEMOS = []  # 自动按 PEOPLE 模板生成

TAGS = ["AAA", "AAAA", "Aa", "a", "创业", "学生", "项目", "黑客松组织",
        "资源", "同届", "海外", "mentor", "开发者社区", "开源项目",
        "GOSIM", "知乎官方技术", "众达", "软件开发"]


SNIPPETS = [
    "下周二有个 AI 治理的圆桌，想去吗？",
    "你看下这个方案，备份方案下周三前定",
    "AAA 名单我重新整理了一下，附件给你",
    "今晚一起吃饭？我那边新开了家小馆",
    "刚跑了下 27B 的本地模型，效果意外不错",
    "TrueFellow 那边的接口对接我周末拉通",
    "有个朋友想认识你，叫 TA 直接加你微信还是先约我这里？",
    "黑客松的赛题出了，今年 AI 软件创新方向",
    "黑客松我们组要不要报？我缺一个做后端的",
    "GOSIM 那个黑客松我看到你了，你直接报名",
    "收到，我下午反馈你",
    "你那个 PDSS 协议我读了，妙",
    "明天 10 点前给我初稿，谢谢",
    "刚看到一篇论文，跟你那个很像",
    "改一下目录，明天再聊",
    "今天 GOSIM 的日程帮忙看下",
    "我去看了你说的那个方案，问题在第三节",
    "别催，我自己的节奏",
    "你那边需要什么资源，告诉我",
    "明天能再约一次吗",
    "这件事我记着呢，这周办",
    "好，等你",
    "晚安",
]


# 群模板：(room_key, 群名, 成员)
# room_key 必须与下面 core.upsert_room(f"wxroom:{name}") 的口径完全一致，
# 否则 edge.rooms 里的群 key 对不上 room 表 → 「共同群数」和「所在群」全是错的。
ROOM_TEMPLATES = [
    ("wxroom:开发者社区 线上",  "开发者社区 线上",
     ["wx:demo_xu", "wx:demo_jiang", "wx:demo_su", "wx:demo_chen"]),
    ("wxroom:核心圈内部群",  "核心圈内部群",
     [p[0] for p in PEOPLE if p[2] == "AAA"][:5]),
    ("wxroom:同届群", "同届群",
     [p[0] for p in PEOPLE if "同届" in (p[5] or "")][:6]),
    ("wxroom:黑客松组队群", "黑客松组队群",
     ["wx:demo_luo", "wx:demo_gu", "wx:demo_xu", "wx:demo_lin", "wx:demo_wen"]),
    ("wxroom:开源项目组", "开源项目组",
     ["wx:demo_lin", "wx:demo_chen", "wx:demo_xu", "wx:demo_su"]),
]


def ingest(progress=print):
    started = core.now()
    progress("🧪 Demo 模式启动 · 不会触碰真微信库")
    grm = core.connect()
    core.purge_window(grm, 0); grm.commit()

    # ── 人 ──
    progress(f"… 注入 {len(PEOPLE)} 个 demo 人（全虚构，含分层标签 AAA/Aa/a + 圈层）")
    for pk, name, tier, relation, role, circle in PEOPLE:
        core.upsert_person(grm, pk, name, tier=tier, circle=circle, org=None,
                           role=role, relation=relation, source="demo",
                           note="演示数据 · 人名与关系均虚构")
        # 取随机 2-5 个标签
        for tag in random.sample(TAGS, random.randint(1, 4)):
            core.tag_person(grm, pk, tag, source="demo")
        core.upsert_identity(grm, pk, "demo", pk.split(":")[1])

    # ── 群 ──
    for rk, name, member_ids in ROOM_TEMPLATES:
        member_ids = [m for m in member_ids if m]
        if not member_ids:
            continue
        core.upsert_room(grm, f"wxroom:{name}", name=name, channel_id=name,
                         member_cnt=len(member_ids), source="demo")
        for mid in member_ids:
            core.add_room_member(grm, f"wxroom:{name}", mid)

    grm.commit()

    # ── 消息 ──
    progress(f"… 注入近 {DEMO_MESSAGES} 条 demo 消息（覆盖近 60 天）")
    msgs = 0
    d30 = time.time() - 30 * 86400
    now = time.time()
    while msgs < DEMO_MESSAGES:
        pk = random.choice([p[0] for p in PEOPLE])
        ts = now - random.uniform(0, 60 * 86400)
        sender = "me" if random.random() < 0.4 else pk
        snippet = random.choice(SNIPPETS)
        core.add_message(grm, pk, ts, sender, 1 if sender == "me" else 0, 1, snippet)
        if ts > d30:
            grm.execute("UPDATE person SET msg_d30=COALESCE(msg_d30,0)+1, last_active=? WHERE person_key=?",
                        (ts, pk))
            grm.execute("UPDATE person SET msg_total=COALESCE(msg_total,0)+1 WHERE person_key=?", (pk,))
        else:
            grm.execute("UPDATE person SET msg_total=COALESCE(msg_total,0)+1 WHERE person_key=?", (pk,))
        msgs += 1

    # 一次聚合
    for pk in [p[0] for p in PEOPLE]:
        row = grm.execute(
            "SELECT MAX(last_active), SUM(msg_total), SUM(msg_d30) FROM ("
            "  SELECT MAX(ts) last_active, 0 msg_total, 0 msg_d30 FROM msg_index WHERE conv_key=? "
            "  UNION ALL SELECT NULL, COUNT(*), 0 FROM msg_index WHERE conv_key=?"
            "  UNION ALL SELECT NULL, 0, COUNT(*) FROM msg_index WHERE conv_key=? AND ts > ?"
            ")", (pk, pk, pk, d30)).fetchone()
        if row and row[0]:
            grm.execute("UPDATE person SET last_active=? WHERE person_key=? AND (last_active IS NULL OR last_active<?)",
                        (row[0], pk, row[0]))

    # ── 关系边（群共现） ──
    edges = 0
    for rk, _, member_ids in ROOM_TEMPLATES:
        for i, a in enumerate(member_ids):
            for b in member_ids[i + 1:]:
                core.add_edge(grm, a, b, "co_group", 1.0, [rk])
                edges += 1
    grm.commit()

    # 写死几个关系事件/待办（视频脚本要演的固定人物必须命中，否则画面空）
    progress("… 注入 demo 关系事件/待办")
    now = int(time.time())
    SCRIPTED_EVENTS = [
        ("wx:demo_lin", "first_talk", 12, "首次深聊", "从陌生到能约饭的关系节点"),
        ("wx:demo_lin", "promise", 3, "答应引荐一位同行", "她说材料整理好就转发给我"),
        ("wx:demo_su",  "intro", 5, "把我拉进一个黑客松组队群", "他组里缺一个懂工程的人"),
    ]
    for pk, kind, days_ago, title, detail in SCRIPTED_EVENTS:
        core.add_event(grm, pk, kind, now - days_ago * 86400,
                       title=title, detail=detail,
                       evidence="（演示数据，非真实对话）", source="demo")
    others = [p[0] for p in PEOPLE if p[0] not in
              {e[0] for e in SCRIPTED_EVENTS}]
    for pk in random.sample(others, min(2, len(others))):
        core.add_event(grm, pk, "first_talk", now - random.randint(10, 40) * 86400,
                       title="首次深聊", detail="（演示）从陌生到能约饭",
                       evidence="（演示数据）", source="demo")
    SCRIPTED_TODOS = [("wx:demo_lin", "下周一问那份材料到哪了"),
                      ("wx:demo_su",  "黑客松组队：确认工程人选")]
    for pk, title in SCRIPTED_TODOS:
        core.add_todo(grm, title, person_key=pk, kind="followup",
                      evidence="（演示数据）")
    for pk in random.sample(others, min(1, len(others))):
        core.add_todo(grm, "（演示）回访跟进", person_key=pk, kind="followup")

    grm.execute(
        "INSERT INTO sync_log(source,src_path,started_at,ended_at,rows_people,rows_msgs,note) "
        "VALUES(?,?,?,?,?,?,?)",
        ("demo", "(synthetic)", started, core.now(), len(PEOPLE), msgs,
         f"edges={edges} DEMO 只为产品演示"))
    grm.commit()
    grm.close()
    return {"source": "demo", "people": len(PEOPLE), "messages": msgs,
            "edges": edges, "tags": len(TAGS),
            "stats": core.stats(core.connect())}