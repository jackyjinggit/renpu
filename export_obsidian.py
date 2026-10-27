# -*- coding: utf-8 -*-
"""仁谱 · Obsidian vault 导出器

为什么做这个（2026-10-26 作者定方向）：
    「以 Obsidian 为基底，加插件」——不是给仁谱再写一套界面，
    而是让**仁谱成为数据生产者**，Obsidian 生态成为消费端。

    作者本机已装的社区插件：
        3d-graph / 3d-graph-new  → 3D 知识图谱
        breadcrumbs               → 双链关系导航
        smart-connections         → 语义检索
        juggl / templater-obsidian
    这些插件吃的是 **vault 里的 markdown 笔记 + 双链**，
    仁谱的真库（21823 人 / 35611 关系边）恰好能生成这种形态。

    所以分工是：
        仁谱  →  解密微信 → 建模关系 → 导出成 vault 笔记
        Obsidian → 渲染、图谱、检索、导航（现成的、成熟的、社区认可的）

    仁谱自己的 GUI/HTML 视图降级为「可选的独立视图」，
    不再是唯一入口 —— 这样才拿得到 Obsidian 的分发与生态。

导出形态：
    人/<名字>.md          每个联系人一篇
        frontmatter: 层级 / 圈层 / 消息数 / 首末出现 / 标签
        正文：与谁在哪些群共现、聊了多少、承诺、近期对话
    群/<群名>.md          每个群一篇（人 → 群 → 人，两跳可导航）
        frontmatter: 群名 / 人数 / 消息量
        正文：成员列表
    索引.md               总入口

原则：
    · 只读真库，不改真库
    · 默认脱敏（share=True）：名字用关系人编号、群名用代号
    · 可重入：重复导出覆盖，不产生重复文件（按稳定文件名）
    · 零第三方依赖
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path


# ───────────────────────── 工具 ─────────────────────────

def _safe_filename(name: str) -> str:
    """把名字变成安全的文件名（Obsidian 里链接靠文件名，所以要稳定可复现）。"""
    s = str(name or "").strip()
    # Windows 非法字符
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s)
    # 去掉首尾空白与点（Windows 不允许结尾是点）
    s = s.strip().rstrip(".").strip()
    if not s:
        s = "未命名"
    # 长度截断（Windows 路径限制）
    if len(s) > 80:
        s = s[:80]
    return s


def _yaml_str(v) -> str:
    """把值写成 YAML 字符串（带引号防特殊字符）。"""
    if v is None:
        return '""'
    s = str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    return f'"{s}"'


def _yaml_list(items) -> str:
    items = [str(x) for x in (items or []) if str(x).strip()]
    if not items:
        return "[]"
    return "[" + ", ".join(_yaml_str(x) for x in items) + "]"


def _ts(v) -> str:
    if not v:
        return ""
    try:
        return time.strftime("%Y-%m-%d", time.localtime(int(v)))
    except (TypeError, ValueError):
        return str(v)


# ───────────────────────── 数据采集 ─────────────────────────

def collect_for_export(con, limit=120, min_messages=20, share=False):
    """采集要导出的人、群、关系。

    limit 控制人数量（Obsidian 一次渲染几千个节点会卡；
    先导核心 N 个，其余留在仁谱自己的库里）。

    返回 {"people": [...], "rooms": [...], "links": [...], "todos": [...], "events": [...]}
    """
    # 人：按**关系度数**取核心（不是消息数！）
    # 为什么（2026-10-26 实测）：按消息数取会选出「聊得多但不在一个群」的人 ——
    #   实测导 40 人只有 26 条关系边 → 笔记里全是孤立节点 → Obsidian 图谱一片空白。
    #   Obsidian 图谱吃的是双链，所以必须选**互相有连接**的人。
    deg = {}
    for r in con.execute("SELECT src, dst FROM edge").fetchall():
        deg[r[0]] = deg.get(r[0], 0) + 1
        deg[r[1]] = deg.get(r[1], 0) + 1
    people = [dict(r) for r in con.execute(
        "SELECT person_key, display_name, real_name, tier, circle, org, role, "
        "       msg_total, msg_d30, first_seen, last_active, note "
        "FROM person WHERE msg_total >= ? ORDER BY msg_total DESC LIMIT ?",
        (min_messages, limit * 4)).fetchall()]
    for pp in people:
        pp["degree"] = deg.get(pp["person_key"], 0)
    people.sort(key=lambda x: (-x["degree"], -(x.get("msg_total") or 0)))
    people = people[:limit]
    # 「我」一定要在（仁谱是「我」的人脉）
    me = [dict(r) for r in con.execute(
        "SELECT person_key, display_name, real_name, tier, circle, org, role, "
        "       msg_total, msg_d30, first_seen, last_active, note "
        "FROM person WHERE person_key='me'").fetchall()]
    if me and not any(p["person_key"] == "me" for p in people):
        me[0]["degree"] = deg.get("me", 0)
        people = [me[0]] + people
    keys = [p["person_key"] for p in people]
    if not keys:
        return {"people": [], "rooms": [], "links": [], "todos": [], "events": []}

    # 名字映射（脱敏时用「关系人 N」）
    label = {}
    for i, p in enumerate(sorted(people, key=lambda x: x["person_key"]), 1):
        pk = p["person_key"]
        if pk == "me":
            label[pk] = "我"
        elif share:
            label[pk] = f"关系人 {i}"
        else:
            label[pk] = p["display_name"] or p["person_key"]

    ph = ",".join("?" * len(keys))
    kset = set(keys)

    # 关系边（两端都在导出集合里）
    links = []
    for r in con.execute(
        f"SELECT src, dst, kind, weight, rooms FROM edge "
        f"WHERE src IN ({ph}) AND dst IN ({ph})", keys + keys).fetchall():
        if r["src"] == r["dst"]:
            continue
        links.append({
            "src": r["src"], "dst": r["dst"],
            "kind": r["kind"] or "co_group",
            "weight": r["weight"],
            "rooms": json.loads(r["rooms"] or "[]"),
        })

    # 群：这些人都在的群
    rooms = []
    seen_room = set()
    for r in con.execute(
        f"SELECT rm.room_key, r.name, COUNT(*) mine FROM room_member rm "
        f"JOIN room r ON r.room_key = rm.room_key "
        f"WHERE rm.person_key IN ({ph}) GROUP BY rm.room_key, r.name "
        f"ORDER BY mine DESC", keys).fetchall():
        if r["room_key"] in seen_room:
            continue
        seen_room.add(r["room_key"])
        rooms.append({"room_key": r["room_key"], "name": r["name"] or r["room_key"],
                      "mine": r["mine"]})

    # 待办与事件
    todos = [dict(r) for r in con.execute(
        f"SELECT title, kind, due, person_key FROM todo "
        f"WHERE person_key IN ({ph}) AND status='open' ORDER BY due LIMIT 60",
        keys).fetchall()]
    events = [dict(r) for r in con.execute(
        f"SELECT kind, ts, title, detail, person_key FROM event "
        f"WHERE person_key IN ({ph}) ORDER BY ts DESC LIMIT 60",
        keys).fetchall()]

    return {"people": people, "rooms": rooms, "links": links,
            "todos": todos, "events": events, "label": label, "keys": kset}


# ───────────────────────── 渲染 ─────────────────────────

def _my_rooms(pk, links):
    """这个人参与的所有群（room_key 列表）。"""
    out = []
    for l in links:
        if l["src"] == pk:
            out.extend(l["rooms"])
        elif l["dst"] == pk:
            out.extend(l["rooms"])
    seen = set()
    uniq = []
    for rk in out:
        if rk not in seen:
            seen.add(rk)
            uniq.append(rk)
    return uniq


def _person_note(p, label, links, rooms, todos, events, share):
    """渲染一个人的笔记。"""
    pk = p["person_key"]
    name = label.get(pk, p["display_name"] or pk)

    # 与这个人的关系
    rels = []
    for l in links:
        if l["src"] == pk:
            other = l["dst"]
        elif l["dst"] == pk:
            other = l["src"]
        else:
            continue
        kind = "私聊" if l["kind"] == "direct" else "同群"
        detail = f"{int(l['weight'])} 条私聊" if l["kind"] == "direct" \
            else f"{len(l['rooms'])} 个共同群"
        rels.append((label.get(other, other), kind, detail, l["weight"], l["rooms"]))
    rels.sort(key=lambda x: -x[3])

    # 这个人所在的群
    my_rooms = [rm for rm in rooms if rm["mine"] >= 1]

    fm = [
        "---",
        f"name: {_yaml_str(name)}",
        f"kind: person",
    ]
    if not share:
        if p.get("tier"):
            fm.append(f"tier: {_yaml_str(p['tier'])}")
        if p.get("circle"):
            fm.append(f"circle: {_yaml_str(p['circle'])}")
        if p.get("org"):
            fm.append(f"org: {_yaml_str(p['org'])}")
        if p.get("role"):
            fm.append(f"role: {_yaml_str(p['role'])}")
    fm.append(f"messages: {int(p.get('msg_total') or 0)}")
    fm.append(f"messages_30d: {int(p.get('msg_d30') or 0)}")
    if p.get("first_seen"):
        fm.append(f"first_seen: {_yaml_str(_ts(p['first_seen']))}")
    if p.get("last_active"):
        fm.append(f"last_seen: {_yaml_str(_ts(p['last_active']))}")
    fm.append(f"relations: {len(rels)}")
    fm.append("---")

    body = [f"# {name}", ""]

    # 标签：Obsidian 的图谱插件用 getAllTags 读正文里的 #tag，
    # 命中即入节点标签 → 图谱能按标签分色。
    # 所以把「层级 / 圈层 / 关系类型」都写成 #tag。
    tags = []
    if p.get("tier"):
        tags.append(f"#层级/{p['tier']}")
    if p.get("circle"):
        tags.append(f"#圈层/{p['circle']}")
    if rels:
        if any(r[1] == "私聊" for r in rels):
            tags.append("#关系/私聊")
        if any(r[1] == "同群" for r in rels):
            tags.append("#关系/群共现")
    # 群标签 = 这个人所在的圈（仁谱的核心维度）
    # Obsidian 图谱按标签分色 → 同一个群的人自动同色，一眼看出「谁跟谁一伙的」。
    # 标签里不能用空格（Obsidian 的 tag 规则），所以群名里的空格要换成连字符。
    my_room_tags = []
    for rk in _my_rooms(pk, links):
        for rm in rooms:
            if rm["room_key"] == rk:
                tg = str(rm["name"]).replace(" ", "-").replace("#", "")
                if tg:
                    my_room_tags.append(f"#群/{tg}")
                break
    for tg in my_room_tags[:12]:
        if tg not in tags:
            tags.append(tg)
    if tags:
        body.append(" ".join(tags))
        body.append("")

    if rels:
        body.append("## 关系")
        body.append("")
        for other, kind, detail, _w, rk in rels[:40]:
            if kind == "私聊":
                body.append(f"- [[{other}]] · {detail}")
            else:
                rm_names = [rm["name"] for rm in rooms
                            if rm["room_key"] in rk][:4]
                tail = ("（" + "、".join(rm_names) + "）") if rm_names else ""
                body.append(f"- [[{other}]] · {detail} {tail}".rstrip())
        body.append("")

    my_todos = [t for t in todos if t["person_key"] == pk]
    if my_todos:
        body.append("## 承诺")
        body.append("")
        for t in my_todos:
            due = f"（{t['due']}）" if t.get("due") else ""
            body.append(f"- [ ] {t['title']} {due}".rstrip())
        body.append("")

    my_events = [e for e in events if e["person_key"] == pk]
    if my_events:
        body.append("## 事件")
        body.append("")
        for e in my_events[:20]:
            d = _ts(e["ts"])
            body.append(f"- **{d}** · {e['kind']} · {e.get('title') or ''}")
        body.append("")

    if p.get("note") and not share:
        body.append("> " + str(p["note"]).replace("\n", " "))
        body.append("")

    return "\n".join(fm + body)


def _room_note(rm, members, share):
    """渲染一个群的笔记（人 → 群 → 人 的中转）。"""
    name = rm["name"]
    fm = [
        "---",
        f"name: {_yaml_str(name)}",
        "kind: room",
        f"members: {len(members)}",
        "---",
        "",
        f"# {name}",
        "",
        "## 成员",
        "",
    ]
    for m in members:
        fm.append(f"- [[{m}]]")
    body = fm
    body.append("")
    body.append(f"共 {len(members)} 人。")
    body.append("")
    return "\n".join(body)


# ───────────────────────── 主函数 ─────────────────────────

def export_vault(con, out_dir, limit=120, min_messages=20, share=True, subdir="人脉"):
    """把人脉导出成一个 Obsidian vault 目录。

    返回 {"out_dir": str, "people": n, "rooms": n, "links": n, "files": n}
    """
    out = Path(out_dir)
    people_dir = out / subdir / "人"
    rooms_dir = out / subdir / "群"
    people_dir.mkdir(parents=True, exist_ok=True)
    rooms_dir.mkdir(parents=True, exist_ok=True)

    data = collect_for_export(con, limit=limit, min_messages=min_messages, share=share)
    label = data["label"]
    people = data["people"]
    links = data["links"]
    rooms = data["rooms"]
    todos = data["todos"]
    events = data["events"]

    # 群名脱敏：share 模式下群名也是身份信息
    # （实测：真实库里的群名会明文出现在笔记的关系段落里）
    if share:
        for i, rm in enumerate(rooms, 1):
            rm["name"] = f"群 {i}"

    # 群 → 成员
    room_members = {}
    for l in links:
        for rk in l["rooms"]:
            room_members.setdefault(rk, set()).add(l["src"])
            room_members.setdefault(rk, set()).add(l["dst"])

    written = 0
    used_names = {}

    def _unique(fname):
        """同名处理：加序号，保证链接对得上。"""
        if fname not in used_names:
            used_names[fname] = 0
            return fname
        used_names[fname] += 1
        return f"{fname} ({used_names[fname]})"

    # 人
    for p in people:
        pk = p["person_key"]
        nm = label.get(pk, p["display_name"] or pk)
        fname = _unique(_safe_filename(nm))
        label[pk] = fname          # 链接用文件名
        note = _person_note(p, label, links, rooms, todos, events, share)
        (people_dir / f"{fname}.md").write_text(note, encoding="utf-8")
        written += 1

    # 群
    room_files = []
    for rm in rooms:
        rk = rm["room_key"]
        members = sorted({label.get(m, m) for m in room_members.get(rk, set())
                          if m in label})
        if not members:
            continue
        base = _safe_filename(rm["name"])
        fname = _unique(base)
        note = _room_note(rm, members, share)
        (rooms_dir / f"{fname}.md").write_text(note, encoding="utf-8")
        written += 1
        # 记下**实际文件名**（去重后）—— 索引必须用它，否则同名群会产生断链
        room_files.append((fname, rm["mine"]))

    # 索引
    idx = ["---", "kind: index", f"people: {len(people)}",
           f"rooms: {len(rooms)}", f"relations: {len(links)}", "---", "",
           "# 人脉总览", "",
           f"导出 {len(people)} 人 · {len(rooms)} 群 · {len(links)} 条关系边。",
           "数据来自仁谱（本地微信关系谱系），生成于 "
           + time.strftime("%Y-%m-%d %H:%M") + "。",
           "", "## 人", ""]
    for p in people:
        pk = p["person_key"]
        idx.append(f"- [[{label.get(pk, pk)}]] · {int(p.get('msg_total') or 0)} 条消息")
    idx += ["", "## 群", ""]
    for fname, mine in room_files:
        idx.append(f"- [[{fname}]] · {mine} 人在内")
    idx.append("")
    (out / subdir / "索引.md").write_text("\n".join(idx), encoding="utf-8")
    written += 1

    # ── 使用说明：告诉用户装什么插件、怎么开图谱 ──
    # 为什么单独写一个 README（2026-10-26 实测）：
    #   仁谱**不应该**往用户的 vault 里塞第三方插件 ——
    #   Obsidian 见到带插件的仓库会弹「你能否信任这个仓库」的警告，
    #   用户得自己点「信任并启用插件」。所以这里只给**建议**，
    #   装不装、信任不信任，由用户决定。
    _write_readme(out, vr_summary={
        "people": len(people), "rooms": len(rooms), "links": len(links),
        "files": written})

    # ── Obsidian 颜色分组：让图谱打开就按圈分色 ──
    # 为什么要写这个（2026-10-26 实测）：
    #   Obsidian 的 graph.json 里 colorGroups 默认是空的 → 全部节点同色 →
    #   486 篇旧笔记 + 137 篇人脉笔记混成一坨，看不出圈子。
    #   颜色分组的 query 走的是 Obsidian 的 search 语法（"tag:#群/xxx"），
    #   所以只要把群里最多的几个圈写成分组，图谱立刻按圈着色。
    _write_color_groups(out, rooms, share)

    return {"out_dir": str(out), "people": len(people), "rooms": len(rooms),
            "links": len(links), "files": written}


def _write_readme(out: Path, vr_summary):
    """写一份「怎么用这个 vault」的说明（放在 vault 根）。"""
    txt = f"""# 人脉谱系 · Obsidian vault

这是**仁谱**从你本地微信数据生成的人脉库。{vr_summary['people']} 人 · {vr_summary['rooms']} 群 · {vr_summary['links']} 条关系边。

## 怎么看

**方式一 · Obsidian 自带图谱**（开箱即用，无需插件）
左侧栏点那个像结绳的图标（关系图谱），或命令面板搜「关系图谱」。
节点已经按圈分好色了 —— 同一个群的人同色。

**方式二 · 社区插件（更强）**
在「设置 → 第三方插件」里装这几个（社区插件市场搜）：

- **3D Graph** —— 三维力导向图谱，滚轮缩放，能看深度
- **Breadcrumbs** —— 侧栏面包屑，看「这个人属于哪些圈」
- **Smart Connections** —— 语义检索，直接问「谁跟我聊过类似的事」

> 仁谱**没有**替你装这些插件 —— 装不装、信任不信任，由你决定。

## 数据说明

- 每个人一篇笔记，正文里写着「跟谁在哪些群里共现」「聊了多少条」「有哪些承诺」
- 群单独一篇，成员列表即入口（人 → 群 → 人，两跳可达）
- frontmatter 存了层级、圈层、消息数、首次与最后出现时间
- 正文里的 `#群/xxx`、`#关系/私聊` 是标签，图谱按它分色

## 隐私

默认导出是**脱敏**的：真名换成「关系人 N」，群名换成「群 N」。
要写真名导出，加 `--no-share`（谨慎：那份文件不能随便发人）。
"""
    (out / "人脉谱系.md").write_text(txt, encoding="utf-8")
    return True


def _write_color_groups(out: Path, rooms, share, top_n=10):
    """写 .obsidian/graph.json 的颜色分组（按群分色）。

    注意：只在**独立 vault**（out 自己就是 vault 根）时写 ——
    往已有 vault 里塞 graph.json 会覆盖用户自己的分组设置。
    """
    obs = out / ".obsidian"
    if obs.exists() and any(obs.iterdir()):
        return False          # 已有 .obsidian 配置 → 不碰
    # 人脉笔记放在 subdir 下 → tag 查询要带路径前缀吗？
    # Obsidian 的 tag 查询是全局的（tag:#群/1），不需要路径。
    counts = {}
    for rm in rooms:
        counts[rm["name"]] = rm["mine"]
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:top_n]
    palette = ["#ff7a6b", "#4ecdc4", "#ffd166", "#a78bfa", "#7dd3fc",
               "#f472b6", "#86efac", "#fb923c", "#c4b5fd", "#67e8f9"]
    groups = []
    for i, (name, _n) in enumerate(top):
        tag = str(name).replace(" ", "-").replace("#", "")
        groups.append({
            "query": f"tag:#群/{tag}",
            "color": {"a": 1, "rgb": int(palette[i % len(palette)][1:], 16)},
        })
    # 「我」单独一组 —— 它是中心
    groups.insert(0, {"query": "path:我", "color": {"a": 1, "rgb": 0xFFFFFF}})
    obs.mkdir(parents=True, exist_ok=True)
    (obs / "graph.json").write_text(json.dumps({
        "collapse-filter": False, "search": "", "showTags": True,
        "showAttachments": True, "hideUnresolved": False, "showOrphans": True,
        "collapse-color-groups": True, "colorGroups": groups,
        "collapse-display": True, "showArrow": False,
        "textFadeMultiplier": 0, "nodeSizeMultiplier": 1, "lineSizeMultiplier": 1,
        "collapse-forces": True, "centerStrength": 0.5,
        "repelStrength": 10, "linkStrength": 1, "linkDistance": 250,
        "scale": 1, "close": False,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import core
    con = core.connect()
    r = export_vault(con, r"<output-dir>", limit=30, min_messages=5, share=False)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    con.close()
