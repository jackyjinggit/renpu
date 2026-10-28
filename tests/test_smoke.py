# -*- coding: utf-8 -*-
"""仁谱 Renpu · 端到端冒烟测试

跑法：
  $PY tests/test_smoke.py

验证：
  1. import 成功
  2. config 对外 describe() 工作
  3. demo ingest 可跑
  4. 七大命令各跑一条不出错
  5. 数据库写入幂等（再 ingest 一次，总数不变）

不依赖任何微信库。
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PARENT = HERE.parent
# 测试 DB 放 tempdir
TEST_HOME = Path(os.environ.get("TMPDIR", "/tmp")) / "renpu_test"
TEST_HOME.mkdir(parents=True, exist_ok=True)
os.environ["RENPU_DATA_HOME"] = str(TEST_HOME / "data")
os.environ["RENPU_DB_PATH"] = str(TEST_HOME / "renpu_test.db")
# 不让 demo write 到 demo 中央目录
os.environ["RENPU_LOG_DIR"] = str(TEST_HOME / "logs")
os.environ["RENPU_EXPORT_DIR"] = str(TEST_HOME / "exports")

sys.path.insert(0, str(PARENT))

import core  # noqa: E402  （需在 sys.path 设置之后导入）


def cli(args, expect_ok=True):
    """调 cli.py 当子进程（不然 import side effect 干扰）。"""
    py = os.environ.get("RENPU_PYTHON") or sys.executable
    r = subprocess.run([py, str(PARENT / "cli.py")] + list(args),
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if expect_ok and r.returncode != 0:
        raise AssertionError(
            f"命令 {' '.join(args)} 失败（exit {r.returncode}）\n"
            f"  STDOUT: {r.stdout[-500:]}\n  STDERR: {r.stderr[-500:]}"
        )
    return r


def reset_db():
    p = Path(os.environ["RENPU_DB_PATH"])
    for ext in ("", "-wal", "-shm"):
        q = Path(str(p) + ext)
        if q.exists():
            q.unlink()


def main():
    print("── 仁谱 Renpu smoke test")

    # 1. import
    import config  # noqa
    print("· import OK:", config.__file__)

    # 2. config describe
    desc = config.describe()
    for k in ("platform", "app_home", "db_path"):
        assert k in desc, f"describe 缺 {k}"
    print("· describe OK:", desc["db_path"])

    # 3. 建库 + demo
    reset_db()
    cli(["init"])
    cli(["ingest", "--demo"])

    # 取库里真实存在的两个人名 —— demo 库的人名是**随机生成**的，
    # 写死人名（林夏/顾南舟…）会在重做 demo 后集体失效（2026-10-28 实测）。
    # 用独立连接取（此时主 con 还没建）。
    import core as _c0
    _c0con = _c0.connect()
    _n1 = _c0con.execute("SELECT display_name FROM person WHERE display_name IS NOT NULL "
                          "AND person_key != 'me' ORDER BY person_key LIMIT 1").fetchone()
    _n2 = _c0con.execute("SELECT display_name FROM person WHERE display_name IS NOT NULL "
                          "AND person_key != 'me' AND display_name != ? "
                          "ORDER BY person_key LIMIT 1", (_n1[0],)).fetchone()
    P1, P2 = _n1[0], _n2[0]
    _c0con.close()

    # 4. 七大命令
    for cmd in [
        ["stats"],
        ["people", "--days", "7", "--limit", "5"],
        ["who", P1, "--limit", "3"],
        ["search", "demo"],
        ["feed", "--days", "3"],
        ["edges", "--limit", "5"],
        ["rooms", "--limit", "3"],
        ["todo"],
    ]:
        r = cli(cmd)
        # 文本输出不能为空
        assert r.stdout.strip(), f"{cmd} 输出空"
        print(f"  ✅ {cmd[0]}")

    # 5. 写回
    cli(["note", "smoke 测试", "--name", P1, "--kind", "ai_note"])
    cli(["note", "smoke 待办", "--name", P2, "--todo"])
    rj = subprocess.run([os.environ.get("RENPU_PYTHON") or sys.executable, str(PARENT / "cli.py"),
                        "--json", "todo"], capture_output=True, text=True)
    parsed = json.loads(rj.stdout)
    has_smoke = any("smoke" in (r.get("title") or "") for r in parsed["rows"])
    assert has_smoke, f"note 没成功（todo 中看不到 smoke）"
    print(f"  ✅ note 写回 + todo 读到")

    # 6. 幂等：再 ingest --demo 一次，所有数应不变
    r1 = subprocess.run([os.environ.get("RENPU_PYTHON") or sys.executable, str(PARENT / "cli.py"),
                        "--json", "stats"], capture_output=True, text=True)
    s1 = json.loads(r1.stdout)
    cli(["ingest", "--demo"])
    r2 = subprocess.run([os.environ.get("RENPU_PYTHON") or sys.executable, str(PARENT / "cli.py"),
                        "--json", "stats"], capture_output=True, text=True)
    s2 = json.loads(r2.stdout)
    assert s1["people"] == s2["people"], f"幂等失败：people {s1['people']} → {s2['people']}"
    assert s1["messages_indexed"] == s2["messages_indexed"], "幂等失败：messages"
    print(f"  ✅ 幂等：{s2['people']} 人 / {s2['messages_indexed']} 消息 重跑无变化")

    # 7. 图谱：生成 + 自包含 + 脱敏
    gpath = TEST_HOME / "graph-test.html"
    cli(["graph", "--share", "--out", str(gpath)])
    gtxt = gpath.read_text(encoding="utf-8")
    assert gpath.exists() and gpath.stat().st_size > 4000, "图谱文件太小，可能没生成"
    # 零外链（不能依赖 CDN / 第三方 JS）
    for bad in ("<script src", "cdn.", "unpkg", "jsdelivr"):
        assert bad not in gtxt, f"图谱含外链：{bad}"
    assert "graph-data" in gtxt, "图谱缺内嵌数据"
    # 分享模式：群名必须脱敏
    import re as _re
    m = _re.search(r"<script id='graph-data' type='application/json'>(.*?)</script>", gtxt, _re.S)
    assert m, "图谱数据块解析失败"
    import json as _json
    gd = _json.loads(m.group(1))
    assert gd["nodes"], "图谱没有节点"
    assert gd["links"], "图谱没有边"
    room_names = {r["name"] for r in gd["rooms"]}
    # demo 群名本来就是虚构的（source='demo'）→ 不该脱敏；
    # 真实群名必须脱敏。测试库是 demo 库，所以这里验「demo 群名没被误脱敏」。
    assert all("已脱敏" not in n for n in room_names), f"demo 群名被脱敏了：{room_names}"
    assert all(n.get("msg_total") is None for n in gd["nodes"]), "分享模式仍带消息数"
    print(f"  ✅ 图谱：{len(gd['nodes'])} 节点 / {len(gd['links'])} 边 / 零外链 / 分享模式已脱敏")

    # 7b. 社区检测 + 边标签
    comms = {n.get("community") for n in gd["nodes"]}
    comms.discard(None)
    assert len(comms) >= 2, f"社区划分没生效（只分出 {len(comms)} 个圈）"
    assert all("community" in n for n in gd["nodes"]), "节点缺 community 字段"
    assert all("shared_rooms" in l for l in gd["links"]), "边缺 shared_rooms 字段"
    assert any(l.get("shared_rooms", 0) > 0 for l in gd["links"]), "没有边带共同群记录"
    print(f"  ✅ 社区：{len(comms)} 个圈子 / 边带共同群数")

    # 7c. 边标签与共享群数必须一致（防止边标签说谎）
    for l in gd["links"]:
        assert l["shared_rooms"] <= l["weight"] + 0.001, (
            f"边 {l['src'][:12]}→{l['dst'][:12]} 的共同群数({l['shared_rooms']}) 超过权重({l['weight']})")
    print("  ✅ 边标签与权重一致（shared_rooms ≤ weight）")

    # 8. 一键体验（tour）：独立目录跑，不碰测试库
    tour_out = TEST_HOME / "tour"
    cli(["tour", "--out", str(tour_out)])
    assert (tour_out / "renpu-graph.html").exists(), "tour 没产出图谱"
    assert (tour_out / "renpu-report.md").exists(), "tour 没产出报告"
    rep = (tour_out / "renpu-report.md").read_text(encoding="utf-8")
    assert "仁谱" in rep and "关系边" in rep, "tour 报告内容不对"
    print("  ✅ tour：图谱 + 报告 都产出")

    # 9. edge.rooms 一致性：rooms 长度不能超过 weight（旧版覆盖 bug 的回归防护）
    con = core.connect()

    bad = 0
    for r in con.execute("SELECT weight, rooms FROM edge WHERE weight > 1").fetchall():
        try:
            rooms = json.loads(r["rooms"] or "[]")
        except (ValueError, TypeError):
            rooms = []
        if len(rooms) > r["weight"] + 0.001:
            bad += 1
    # 8b. 中文全文检索：FTS5 默认分词对中文按单字切分，整词 MATCH 会命中 0
    #     必须走 core.search_messages（加 * 前缀 + LIKE 兜底）
    hit = core.search_messages(con, "黑客松", limit=5)
    assert hit, "中文搜索命中 0 条 —— FTS 查询口径坏了"
    assert any("黑客松" in (h["snippet"] or "") for h in hit), "搜索结果与关键词无关"
    print(f"  ✅ 中文搜索：黑客松 → {len(hit)} 条")

    # 8b2. demo 库隔离：默认灌进 <data_home>/demo/，不占真实库位置
    #      （曾经 `renpu ingest --demo` 会把合成数据写进真实库位置）
    import config
    demo_home = Path(config.DATA_HOME) / "demo" / "renpu.db"
    real_slot = Path(config.DATA_HOME) / "renpu.db"
    assert demo_home != real_slot, "demo 库与真实库位置相同 —— 会互相污染"
    assert "demo" in demo_home.parts, f"demo 库不在独立子目录：{demo_home}"
    print(f"  ✅ demo 库隔离：{demo_home.parent.name}/（真实库位 {real_slot.name} 独立）")

    # 8b3. PII 遮蔽：wxid / 手机 / 邮箱 / QQ / 微信号 都必须遮，
    #      同时不能误伤普通英文（误伤会让正常消息看起来被审查过）
    import core as core_mod
    must_hide = [
        ("wxid_aaaa1111bbbb2222: 内容", "微信id"),
        ("手机 13812345678", "手机"),
        ("邮箱 zhangsan@example.com", "邮箱"),
        ("QQ 12345678", "QQ号"),
        ("微信号 zhangsan_2024", "微信号"),
        ("公众号 gh_abcdef123456", "公众号id"),
    ]
    for txt, tag in must_hide:
        out, hit = core_mod.mask_pii(txt)
        assert hit, f"PII 没遮住（{tag}）：{txt} → {out}"
        assert tag in out, f"遮蔽标记不对（{tag}）：{out}"
    # 关键回归点：Python 的  只认 ASCII 词字符，
    # 「wxid_xxx退群了」里 wxid 后直接跟中文 → 无边界 → 旧正则漏遮。
    # 必须用负向前瞻而不是 。
    for txt in ("wxid_cccc3333dddd4444退群了，祝他早日财富自由",
                "[微信id]: wxid_eeee5555ffff6666退群了",
                "邮箱 zhangsan@example.com 收到"):
        out, hit = core_mod.mask_pii(txt)
        assert hit, f"中文紧跟时没遮住：{txt} → {out}"
        assert "wxid_" not in out and "@" not in out.replace("[邮箱]", ""),             f"中文紧跟时漏遮：{txt} → {out}"
    must_keep = ["正常一句话没有敏感信息", "2026年10月26日", "版本号 v1.2.3",
                 "会议室 3021", "see you at the office", "hello_world",
                 "@全体 记得看", "请直接@我"]
    for txt in must_keep:
        out, hit = core_mod.mask_pii(txt)
        assert not hit, f"误伤正常文本：{txt} → {out}"
    print(f"  ✅ PII 遮蔽：{len(must_hide)} 类遮住 / {len(must_keep)} 条不误伤")

    # 8c. demo 库 room_key 口径一致：edge.rooms 的群 key 必须能在 room 表里找到
    room_keys = set(r[0] for r in con.execute("SELECT room_key FROM room"))
    edge_rooms = set()
    for (rs,) in con.execute("SELECT rooms FROM edge WHERE rooms IS NOT NULL AND rooms != ''"):
        edge_rooms.update(json.loads(rs))
    assert edge_rooms, "edge.rooms 全空"
    orphan = edge_rooms - room_keys
    assert not orphan, f"edge.rooms 里有对不上 room 表的群 key：{list(orphan)[:3]}"
    print(f"  ✅ room_key 口径一致：{len(edge_rooms)} 个群全部对得上")

    # 8c2. 分享模式必须脱敏**人名**（2026-10-26 真实库实测发现这是漏洞：
    #      原实现只清了群名/统计数字，人名与 person_key 原样保留）
    import json as _json
    import re as _re

    def _data_values(html, sid):
        m = _re.search(rf"""<script[^>]*id=['\"]{sid}['\"][^>]*>(.*?)</script>""",
                       html, _re.S)
        if not m:
            return None
        try:
            d = _json.loads(m.group(1).replace("<\/", "</"))
        except Exception:
            return None
        out = set()

        def _walk(o):
            if isinstance(o, dict):
                for v in o.values():
                    _walk(v)
            elif isinstance(o, list):
                for v in o:
                    _walk(v)
            elif isinstance(o, str):
                out.add(o.strip())
        _walk(d)
        return out

    # 收集库里所有「像人名」的字符串
    cand = set()
    for (nm,) in con.execute("SELECT display_name FROM person").fetchall():
        nm = (nm or "").strip()
        if len(nm) >= 2 and sum(1 for c in nm if ord(c) > 0x2E80) >= 2:
            cand.add(nm)
    for r in con.execute("SELECT alias, nickname, remark FROM identity").fetchall():
        for v in r:
            v = (v or "").strip()
            if len(v) >= 2 and sum(1 for c in v if ord(c) > 0x2E80) >= 2:
                cand.add(v)

    # 2D 图：分享模式
    import render
    import render3d
    gpath, gg = render.write_graph_html(con, TEST_HOME / "g_share.html",
                                        limit=20, share=True)
    gvals = _data_values(Path(gpath).read_text(encoding="utf-8"), "graph-data")
    assert gvals is not None, "2D 图找不到数据区"
    leak = sorted({n for n in cand if n in gvals})
    assert not leak, f"2D 分享模式泄漏人名：{leak[:5]}"
    ghtml = Path(gpath).read_text(encoding="utf-8")
    assert "wxid_" not in ghtml, "2D 分享模式泄漏 wxid"
    for n in gg["nodes"]:
        assert n["display_name"].startswith(("关系人", "我")),             f"2D 分享模式节点名没脱敏：{n['display_name']}"
        assert not str(n["person_key"]).startswith("wx:"),             f"2D 分享模式 person_key 没脱敏：{n['person_key']}"
    print(f"  ✅ 2D 分享脱敏：{len(cand)} 个候选名命中 0 / 节点名与 key 全脱敏")

    # 3D 圈：分享模式
    tpath, tg = render3d.write_graph_3d(con, TEST_HOME / "g3d_share.html",
                                         limit=12, share=True)
    tvals = _data_values(Path(tpath).read_text(encoding="utf-8"), "g3d")
    assert tvals is not None, "3D 圈找不到数据区"
    leak3 = sorted({n for n in cand if n in tvals})
    assert not leak3, f"3D 分享模式泄漏人名：{leak3[:5]}"
    thtml = Path(tpath).read_text(encoding="utf-8")
    assert "wxid_" not in thtml, "3D 分享模式泄漏 wxid"
    for n in tg["nodes"]:
        assert n["display_name"].startswith(("当事人", "关系人", "我")),             f"3D 分享模式节点名没脱敏：{n['display_name']}"
    print(f"  ✅ 3D 分享脱敏：命中 0 / 节点名与 key 全脱敏")

    # 8b4. demo 库必须含「我」+ 私聊关系边
    #      （实测：demo 之前没有 me 这个 person 记录，
    #        图上找不到自己、搜索结果显示「谁=me」、没有以我为中心的私聊边）
    _me = con.execute("SELECT person_key, display_name FROM person "
                      "WHERE person_key='me'").fetchone()
    assert _me is not None, "demo 库缺少「我」（me）节点"
    assert _me[1], "「我」的显示名为空"
    _ndirect = con.execute("SELECT COUNT(*) FROM edge WHERE kind='direct'").fetchone()[0]
    assert _ndirect > 0, "demo 库没有私聊关系边（direct）"
    _nme = con.execute("SELECT COUNT(*) FROM edge WHERE src='me' OR dst='me'").fetchone()[0]
    assert _nme > 0, "demo 库里「我」没有任何关系边"
    print(f"  ✅ demo 库含「我」+ {_ndirect} 条私聊边（我的 {_nme} 条）")

    # 8c3. API 端点：脱敏响应 + key 唯一 + 错误路径
    import server as _server
    for fn, args in ((_server.api_graph, (con, 8, True, "json")),
                     (_server.api_graph3d, (con, P1, 12, True, "json")),
                     (_server.api_profile, (con, P1, True)),
                     (_server.api_timeline, (con, "", 30))):
        out = fn(*args)
        _vals = _server._string_values(out)
        _leak = sorted({n for n in cand if n in _vals})
        assert not _leak, f"API 响应泄漏人名（{fn.__name__}）：{_leak[:5]}"
    _g = _server.api_graph(con, 8, True, "json")
    _ks = [n.get("person_key") for n in _g.get("nodes") or []]
    assert len(_ks) == len(set(_ks)),         f"API 脱敏后 person_key 不唯一：{len(_ks)} 个节点只有 {len(set(_ks))} 个 key"
    _g3 = _server.api_graph3d(con, P1, 12, True, "json")
    assert _g3.get("nodes"), "API graph3d 返回空节点"
    _pr = _server.api_profile(con, "不存在的人xyz", True)
    assert _pr.get("found") is False, "API 对不存在的人应返回 found=false"
    # 'me' 是系统标识（本人），不是姓名 —— 不能进脱敏校验表。
    # 实测：demo 的 identity 表里 me 的 channel_id='me'，
    #      被当成真实姓名 → API 直接 500（且只在 EXE 的库路径下暴露）。
    assert not _server._is_name_like("me"), "'me' 被当成姓名了"
    assert not _server._is_name_like("wx:demo_xxx"), "内部 key 被当成姓名了"
    assert not _server._is_name_like("7"), "纯数字被当成姓名了"
    assert _server._is_name_like(P1), "正常中文名没被识别"
    print("  ✅ API：脱敏命中 0 / key 唯一 / 3D 非空 / 错误路径正确 / 系统标识不误判")

    # 8c4. Obsidian vault 导出：节点/边/双链/标签 + 断链 0 + 脱敏
    import export_obsidian
    import re as _re2
    vault_dir = TEST_HOME / "vault"
    vr = export_obsidian.export_vault(con, vault_dir, limit=30,
                                      min_messages=1, share=True)
    assert vr["people"] > 0, "vault 导出没有人"
    assert vr["links"] > 0, "vault 导出没有关系边"
    vfiles = list(vault_dir.rglob("*.md"))
    assert len(vfiles) >= vr["people"], "vault 笔记数少于人数"
    vnames = {f.stem for f in vfiles}
    broken = []
    for f in vfiles:
        for m in _re2.finditer(r"\[\[([^\]|#]+)", f.read_text(encoding="utf-8")):
            if m.group(1).strip() not in vnames:
                broken.append((f.stem, m.group(1).strip()))
    assert not broken, f"vault 有断链：{broken[:4]}"
    # Obsidian 的 tag 规则：#tag 不能含空格
    tagre = _re2.compile(r"(?:^|\s)#([^\s#\[\]]+)")
    tag_count = 0
    for f in vfiles:
        tag_count += len(list(tagre.finditer(f.read_text(encoding="utf-8"))))
    assert tag_count > 0, "vault 笔记里没有 #tag（图谱插件靠 tag 分色）"
    # 脱敏：vault 里不能出现真名或群名
    vtext = "\n".join(f.read_text(encoding="utf-8") for f in vfiles)
    vleak = sorted({n for n in cand if n in vtext})
    assert not vleak, f"vault 泄漏真名：{vleak[:4]}"
    assert "wxid_" not in vtext, "vault 泄漏 wxid"
    print(f"  ✅ Obsidian vault：{vr['people']} 人 / {vr['links']} 边 / "
          f"断链 0 / {tag_count} 个 tag / 无真名")

    # 8d. 人物档案页：单文件、零外链、分享模式脱敏
    import render_profile
    prof_path, prof = render_profile.render_profile(con, P1,
                                                    TEST_HOME / "profile.html", share=False)
    prof_html = Path(prof_path).read_text(encoding="utf-8")
    assert "http://" not in prof_html.replace("http://www.w3.org", "")         and "https://" not in prof_html, "档案页有外链"
    for key in ("name", "edges", "todos", "events", "recent", "timeline", "rooms", "tags"):
        assert key in prof, f"档案字典缺字段：{key}"
    share_path, sprof = render_profile.render_profile(con, P1,
                                                      TEST_HOME / "profile_share.html", share=True)
    share_html = Path(share_path).read_text(encoding="utf-8")
    assert P1 not in share_html, "分享版档案页泄漏了真实姓名"
    print(f"  ✅ 档案页：{len(prof['edges'])} 关系人 / 零外链 / 分享版无真名")

    # 3D 关系圈：单文件、零外链、分享模式脱敏
    import render3d
    g3d_path, g3d = render3d.write_graph_3d(con, TEST_HOME / "g3d.html",
                                             limit=12, share=True)
    g3d_html = Path(g3d_path).read_text(encoding="utf-8")
    assert "http://" not in g3d_html.replace("http://www.w3.org", "")         and "https://" not in g3d_html, "3D 视图有外链"
    assert len(g3d["nodes"]) > 0, "3D 视图没有节点"
    assert g3d["nodes"][0]["is_center"], "3D 视图第一个节点不是中心人物"
    for n in g3d["nodes"]:
        assert n.get("msg_total") is None, f"3D 分享模式仍带消息数：{n['display_name']}"
    for r in g3d.get("rooms", []):
        # demo 群名本来��虚构的（source='demo'）→ 不该脱敏；
        # 真实群名必须脱敏。测试库是 demo，所以这里只验「不出现已脱敏」。
        assert "已脱敏" not in r["name"], f"demo 群名被脱敏了：{r['name']}"

    # 8c5. 分享模式不得泄漏**原始群标识** wxroom:xxx（2026-10-28 实测漏洞）
    #      edge.rooms 里存的是原始 room_key —— 跟 wxid 同性质，能加群的稳定标识。
    #      而且真库有一批群名是纯数字（'8'/'5'），群名那层脱敏挡不住它。
    import core as _core_mod
    _probe = {"links": [{"rooms": json.dumps(["wxroom:123@chatroom"])}],
              "rooms": [{"room_key": "wxroom:789@chatroom"}]}
    _core_mod.scrub_room_keys(_probe)
    assert "wxroom:" not in json.dumps(_probe, ensure_ascii=False),         "scrub_room_keys 没清掉原始群标识"
    for _label, _txt in (("2D", ghtml), ("3D", g3d_html), ("档案", share_html)):
        assert "wxroom:" not in _txt, f"{_label} 分享模式泄漏原始群标识"
    print("  ✅ 分享脱敏：原始群标识 wxroom:xxx 全清（三件套实测）")
    print(f"  ✅ 3D 圈：{len(g3d['nodes'])} 节点 / {len(g3d['links'])} 边 / 零外链 / 已脱敏")
    con.close()
    assert bad == 0, f"edge.rooms 又出现覆盖不一致（{bad} 条）"
    print("  ✅ edge.rooms 一致性（rooms 长度 ≤ weight）")

    print("\n✅ ALL GREEN")


if __name__ == "__main__":
    main()