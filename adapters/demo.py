# -*- coding: utf-8 -*-
"""仁谱 · 演示数据生成器

为什么要重做（2026-10-28 蒋平反馈「产品不够硬、缺外部证据」）：
    老版 demo 库只有 **10 人 / 28 条边 / 2000 条消息**，而且
    **一条私聊边都没有**（只有 co_group）。

    这意味着上台演示时：
      · 圈层稀稀拉拉，看着像玩具
      · **「私聊也是关系」这个核心洞察在 demo 库里完全看不见**
        —— 现场点开一个人，只有群共现，没有青色的私聊边
      · config 里的 `demo_people=80` 是**死的**（PEOPLE 硬编码 10 人，
        EXTRA_DEMOS 是空列表）→ 调配置没用

    新的 demo 库按「真库的关系形态」造：
      · 规模可配（默认 80 人，可调到 200+）
      · **圈层有结构**（不是平均分布，是几个大小不一的真实圈子）
      · **私聊边按真实分布**：一部分人跟你私聊很多、一部分只共现、
        还有一部分**只在私聊里出现、任何群里都没有**（这才是洞察的载体）
      · 每个人的分层 / 圈层 / 角色 / 消息量都合理

    ⚠️ 全部虚构：人名、群名、对话内容均为编造，不含任何真实信息。
"""
from __future__ import annotations

import random
import time

import core
from config import DEMO_PEOPLE, DEMO_MESSAGES
from adapters.wechat import DIRECT_MIN_MESSAGES  # 私聊关系边阈值（与真实源同口径）


# ── 虚构人名素材（组合生成，保证不与真实人名重合）──
_SURNAMES = ["林", "苏", "陈", "周", "顾", "沈", "江", "许", "罗", "温",
             "柯", "邵", "裴", "宋", "梁", "薛", "孟", "卫", "沈", "尤"]
_GIVEN = ["夏", "见白", "屿", "斯年", "南舟", "迟", "白露", "聿白", "青禾", "屿声",
          "砚", "澈", "桐", "屿", "珩", "野", "桉", "之遥", "叙白", "临川"]
_ROLES = ["researcher", "engineer", "designer", "PM", "data eng", "student",
          "investor", "operator", "editor", "founder"]
_CIRCLES = ["学术线", "教育线", "产品线", "同届", "行业线", "黑客松线", "资源线",
            "开源线", "投资线", "媒体线"]
_RELATIONS = ["同行", "mentor", "协作伙伴", "同学", "学姐", "顾问", "网友",
              "队友", "资源方", "前同事", "客户", "供应商"]
_TIERS = ["AAA", "AAA", "Aa", "Aa", "a", "a", "a", "a", "b", "b"]

# 圈层：每个圈有名字、规模、以及「这个圈里谁跟你私聊多」
_CIRCLES_PLAN = [
    # (圈名, 人数占比, 平均群数, 私聊倾向 0-1)
    ("核心圈",     0.12, 3.2, 0.85),
    ("同行圈",     0.20, 2.4, 0.45),
    ("同学圈",     0.18, 2.0, 0.30),
    ("黑客松线",   0.15, 1.8, 0.35),
    ("资源线",     0.12, 1.5, 0.20),
    ("行业线",     0.13, 1.2, 0.25),
    ("长尾",       0.10, 0.6, 0.10),
]

_TAGS = ["创业", "学生", "项目", "黑客松组织", "资源", "同届", "海外", "mentor",
         "开发者社区", "开源项目", "GOSIM", "知乎官方技术", "软件开发", "教育",
         "投资", "媒体", "数据", "模型", "产品", "设计"]

_SNIPPETS = [
    "下周二有个 AI 治理的圆桌，想去吗？",
    "你看下这个方案，备份方案下周三前定",
    "名单我重新整理了一下，附件给你",
    "今晚一起吃饭？我那边新开了家小馆",
    "刚跑了下 27B 的本地模型，效果意外不错",
    "接口对接我周末拉通，完了给你结果",
    "收到，我下午反馈你",
    "你那个协议我读了，妙",
    "明天 10 点前给我初稿，谢谢",
    "刚看到一篇论文，跟你那个很像",
    "改一下目录，明天再聊",
    "今天 GOSIM 的日程帮忙看下",
    "我去看了你说的那个方案，问题在第三节",
    "别催，我自己的节奏",
    "你那边需要什么资源，告诉我",
    "明天能再约一次吗",
    "这件事我记着呢，这周办",
    "材料我发你了，有空看看",
    "这个价格能谈，但得签协议",
    "简历我看过了，基础不错",
    "先做一个小样，跑通了再谈",
    "我们那边缺一个懂工程的",
    "你这个思路可以，但要能落地",
    "晚安",
    "在忙吗？有个事想请教",
    # 这几条要含「黑客松」—— 演示时用中文全文检索搜的就是这个词，
    # 消息正文里没有它，搜索演示就是空的（2026-10-28 实测）。
    "黑客松我们组要不要报？我缺一个做后端的",
    "黑客松的赛题出了，今年 AI 软件创新方向",
    "下周黑客松见，材料你带了吗",
]


def _make_people(n):
    """按圈层计划生成 n 个虚构的人。"""
    people = []
    used = set()
    idx = 0
    for circle, share, rooms, dm_tendency in _CIRCLES_PLAN:
        count = max(2, int(n * share))
        for _ in range(count):
            if len(people) >= n:
                break
            # 造不重复的名字
            while True:
                nm = random.choice(_SURNAMES) + random.choice(_GIVEN)
                if nm not in used:
                    used.add(nm)
                    break
            idx += 1
            tier = random.choice(_TIERS)
            role = random.choice(_ROLES)
            relation = random.choice(_RELATIONS)
            # 私聊倾向：越高越可能只私聊、不同群
            dm = random.random() < dm_tendency
            people.append({
                "key": f"wx:demo_{idx:03d}",
                "name": nm,
                "tier": tier,
                "circle": circle,
                "role": role,
                "relation": relation,
                "rooms": max(1, int(random.gauss(rooms, 0.8))),
                "dm_tendency": dm,
            })
    # 补齐到 n
    while len(people) < n:
        idx += 1
        nm = random.choice(_SURNAMES) + random.choice(_GIVEN) + str(len(people))
        people.append({
            "key": f"wx:demo_{idx:03d}", "name": nm, "tier": "a",
            "circle": "长尾", "role": random.choice(_ROLES),
            "relation": random.choice(_RELATIONS),
            "rooms": 1, "dm_tendency": random.random() < 0.15,
        })
    return people


def _make_rooms(people):
    """按圈层生成群 —— 口径照抄真库（2026-10-28 逐项对照真库后重写）。

    真库的实测口径（用来对照，不是拍脑袋）：
        265 群 · 群均 70 人 · co_group 边权重 89% 是 1.0
        → 绝大多数人**只在一个群里**，所以「共同群」= 1，边自然稀疏
        → 同口径下图上是 4.1 条/人

    前两版都栽在「边太密」上（15 条/人 vs 真库 4.1）→
    全网互连成一团，图上根本看不出圈层。根因有二：
        ① 让人进多个群
        ② 把「圈里的核心人物」塞进该圈**每一个**群
           → 有几个人进了 10~19 个群 → 他们跟所有人连 → 边爆炸
    这版的做法：
        · 每人**最多进 2 个群**（第二个是跨界群）
        · 每个群 12~28 人
        · 群内没有"必须带上的核心人物"
    """
    rooms = []
    by_circle = {}
    for p in people:
        by_circle.setdefault(p["circle"], []).append(p["key"])

    for circle, members in by_circle.items():
        # 每圈 1~2 个群；圈内人 82% 进群，18% 留作长尾（只在私聊里出现）
        n_rooms = 1 if len(members) < 18 else 2
        shuffled = list(members)
        random.shuffle(shuffled)
        in_rooms = shuffled[:max(6, int(len(shuffled) * 0.82))]

        seg = max(6, len(in_rooms) // n_rooms)
        for i in range(n_rooms):
            chunk = in_rooms[i * seg:(i + 1) * seg]
            if len(chunk) < 3:
                if rooms and rooms[-1][2] is not None and rooms[-1][0].startswith(f"wxroom:{circle}"):
                    rooms[-1] = (rooms[-1][0], rooms[-1][1], rooms[-1][2] + chunk)
                continue
            rname = f"{circle}·{i+1}群" if n_rooms > 1 else f"{circle}群"
            rooms.append((f"wxroom:{rname}", rname, chunk))

    # 跨界者：约 6% 的人额外进**另一个圈**的一个群（真实世界也有这种人）
    if len(rooms) >= 2:
        for _ in range(max(1, len(people) // 16)):
            src = random.choice(rooms)
            dst = random.choice(rooms)
            if src[0] == dst[0]:
                continue
            who = random.choice(src[2])
            if who not in dst[2] and len(dst[2]) < 30:
                dst[2].append(who)
    return rooms


def ingest(progress=print):
    started = core.now()
    progress("🧪 Demo 模式启动 · 不会触碰真微信库")
    # ⚠️ 固定随机种子 —— demo 必须**幂等**（2026-10-28 实测回归）：
    #    重跑 `ingest --demo` 两次，消息数会变（随机生成的人数/分布不同）
    #    → 幂等断言挂、且用户会以为自己的库被改坏了。
    #    同一个种子 → 同一份数据。
    random.seed(20261028)
    # ⚠️ 必须先建表（2026-10-28 实测崩在这里）：
    #    全新安装的用户第一个命令就是 `ingest --demo`，
    #    而 purge_window 会 DELETE FROM msg_fts —— 表不存在直接 OperationalError。
    #    demo 不该要求用户先跑一遍 init。
    #    传当前库的路径（CLI 已把 config.DB_PATH 设成 demo 库），
    #    避免无参 init_db 建到别的位置去。
    core.init_db(core.DB_PATH)
    grm = core.connect()
    core.purge_window(grm, 0)
    # 🔴 必须清掉上一轮 demo 留下的**群成员关系**（2026-10-28 实测踩坑）：
    #    purge_window 只清消息，不清 room_member →
    #    重跑一次成员的群归属就叠加（实测有人进了 19 个群）→
    #    边密度 15.8 条/人（真库同口径 4.1）→ 图上全是一团，看不出圈层。
    grm.execute("DELETE FROM room_member")
    grm.execute("DELETE FROM room WHERE source='demo'")
    grm.execute("DELETE FROM person WHERE source='demo'")
    grm.execute("DELETE FROM identity WHERE person_key LIKE 'wx:demo_%'")
    grm.execute("DELETE FROM person_tag WHERE person_key LIKE 'wx:demo_%'")
    grm.execute("DELETE FROM tag WHERE source='demo'")
    grm.execute("DELETE FROM edge WHERE src LIKE 'wx:demo_%' OR dst LIKE 'wx:demo_%'")
    grm.commit()

    n_people = max(10, DEMO_PEOPLE)
    n_messages = max(500, DEMO_MESSAGES)
    people = _make_people(n_people)
    rooms = _make_rooms(people)
    by_key = {p["key"]: p for p in people}

    # ── 人 ──
    progress(f"… 注入 {len(people)} 个 demo 人（全虚构，含分层 / 圈层 / 角色）")
    for p in people:
        core.upsert_person(grm, p["key"], p["name"], tier=p["tier"],
                           circle=p["circle"], org=None, role=p["role"],
                           relation=p["relation"], source="demo",
                           note="演示数据 · 人名与关系均虚构")
        for tag in random.sample(_TAGS, random.randint(1, 4)):
            core.tag_person(grm, p["key"], tag, source="demo")
        core.upsert_identity(grm, p["key"], "demo", p["key"].split(":")[1])

    # ── 「我」──
    core.upsert_person(grm, "me", "我", source="demo", note="演示数据 · 这是你")
    core.upsert_identity(grm, "me", "demo", "me")

    # ── 群 ──
    progress(f"… 注入 {len(rooms)} 个 demo 群")
    for rk, name, member_ids in rooms:
        core.upsert_room(grm, rk, name=name, channel_id=name,
                         member_cnt=len(member_ids), source="demo")
        for mid in member_ids:
            core.add_room_member(grm, rk, mid)

    # ── 群共现边 ──
    progress("… 推导群共现关系边")
    edges = 0
    for rk, _name, member_ids in rooms:
        if not (2 <= len(member_ids) <= 60):
            continue
        for i in range(len(member_ids)):
            for j in range(i + 1, len(member_ids)):
                core.add_edge(grm, member_ids[i], member_ids[j], "co_group", 1.0, [rk])
                edges += 1
                if edges > 200000:
                    break
    grm.commit()

    # ── 消息（群里的）──
    progress(f"… 注入近 {n_messages} 条 demo 消息（覆盖近 60 天）")
    msgs = 0
    d30 = time.time() - 30 * 86400
    now = time.time()
    while msgs < n_messages:
        pk = random.choice([p["key"] for p in people])
        ts = now - random.uniform(0, 60 * 86400)
        sender = "me" if random.random() < 0.4 else pk
        snippet = random.choice(_SNIPPETS)
        core.add_message(grm, pk, ts, sender, 1 if sender == "me" else 0, 1, snippet)
        if ts > d30:
            grm.execute("UPDATE person SET msg_d30=COALESCE(msg_d30,0)+1, last_active=? "
                        "WHERE person_key=?", (ts, pk))
        grm.execute("UPDATE person SET msg_total=COALESCE(msg_total,0)+1 "
                    "WHERE person_key=?", (pk,))
        msgs += 1

    # ── 私聊消息 + 私聊关系边 ──
    # 关键：私聊边是仁谱的核心洞察载体。
    # demo 必须有 —— 且要有「只在私聊里、任何群里都不出现」的人，
    # 否则上台演示点开一个人只有群共现，看不出私聊边。
    progress("… 注入私聊对话与私聊关系边")
    dm_people = [p for p in people if p["dm_tendency"]]
    # 保证有一批「纯私聊型」的人：只跟「我」有私聊，不进任何群
    pure_dm = random.sample(dm_people, min(max(6, len(dm_people) // 3), len(dm_people)))
    pure_set = {p["key"] for p in pure_dm}
    # 纯私聊型的人从所有群里移除
    for rk, _name, member_ids in rooms:
        keep = [m for m in member_ids if m not in pure_set]
        if len(keep) < 2:
            keep = member_ids[:2]
        for mid in member_ids:
            grm.execute("DELETE FROM room_member WHERE room_key=? AND person_key=?",
                        (rk, mid))
        for mid in keep:
            core.add_room_member(grm, rk, mid)

    dm_counts = {}
    for p in people:
        if p["key"] in pure_set:
            # 纯私聊型：与我的私聊量很大，别的都是 0
            n = random.randint(30, 400)
        elif p["dm_tendency"] and random.random() < 0.5:
            n = random.randint(12, 120)
        else:
            n = 0
        if n:
            dm_counts[p["key"]] = n

    for pk, cnt in dm_counts.items():
        for _ in range(cnt):
            ts = now - random.uniform(0, 90 * 86400)
            mine = random.random() < 0.45
            core.add_message(grm, pk, ts, "me" if mine else pk,
                             1 if mine else 0, 1, random.choice(_SNIPPETS))
            grm.execute("UPDATE person SET msg_total=COALESCE(msg_total,0)+1 "
                        "WHERE person_key=?", (pk,))
            if ts > d30:
                grm.execute("UPDATE person SET msg_d30=COALESCE(msg_d30,0)+1 "
                            "WHERE person_key=?", (pk,))
    grm.commit()

    # 私聊关系边（覆盖模式：权重就是消息条数）
    direct = 0
    for pk, cnt in dm_counts.items():
        if cnt < DIRECT_MIN_MESSAGES:
            continue
        a, b = ("me", pk) if "me" < pk else (pk, "me")
        core.add_edge(grm, a, b, "direct", float(cnt), None,
                      note=f"私聊 {cnt} 条", accumulate=False)
        direct += 1
    grm.commit()
    edges += direct

    # ── 关系事件 / 待办（让档案页有内容）──
    # ⚠️ 先清掉上一轮 demo 留下的事件与待办（2026-10-28 实测）：
    #    不清的话重跑一次 events 3→6、todos 12→24 翻倍 → 破坏幂等。
    progress("… 注入 demo 关系事件 / 待办")
    grm.execute("DELETE FROM event WHERE source='demo' OR person_key LIKE 'wx:demo_%'")
    grm.execute("DELETE FROM todo WHERE person_key LIKE 'wx:demo_%'")
    grm.commit()
    now_i = int(time.time())
    SCRIPTED_EVENTS = [
        ("wx:demo_001", "first_talk", 42, "首次深聊", "从陌生到能约饭的关系节点"),
        ("wx:demo_001", "promise", 12, "答应引荐一位同行", "她说材料整理好就转发给我"),
        ("wx:demo_002", "intro", 26, "把我拉进一个组队群", "他组里缺一个懂工程的人"),
    ]
    for pk, kind, days_ago, title, detail in SCRIPTED_EVENTS:
        if by_key.get(pk):
            core.add_event(grm, pk, kind, now_i - days_ago * 86400,
                           title=title, detail=detail, source="demo")
    TAGGED = list(by_key)[:12]
    for i, pk in enumerate(TAGGED):
        if not by_key.get(pk):
            continue
        core.add_todo(grm, "下周一问那份材料到哪了", person_key=pk,
                      kind="followup", due=None, evidence="demo")

    grm.commit()

    # ── 审计 ──
    grm.execute(
        "INSERT INTO sync_log(source,src_path,started_at,ended_at,rows_people,rows_msgs,note) "
        "VALUES(?,?,?,?,?,?,?)",
        ("demo", "(演示数据 · 全虚构)", started, core.now(), len(people),
         msgs, f"rooms={len(rooms)} edges={edges} direct={direct} "
               f"pure_dm={len(pure_set)}"))
    grm.commit()
    st = core.stats(grm)
    grm.close()
    return {"people": len(people), "rooms": len(rooms), "messages": msgs,
            "edges": edges, "direct": direct, "pure_dm": len(pure_set),
            "stats": st}
