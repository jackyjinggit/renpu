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

    # 4. 七大命令
    for cmd in [
        ["stats"],
        ["people", "--days", "7", "--limit", "5"],
        ["who", "林夏", "--limit", "3"],
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
    cli(["note", "smoke 测试", "--name", "林夏", "--kind", "ai_note"])
    cli(["note", "smoke 待办", "--name", "苏见白", "--todo"])
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
    assert all("已脱敏" in n or n == "(群)" for n in room_names), f"分享模式群名没脱敏：{room_names}"
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
    con.close()
    assert bad == 0, f"edge.rooms 又出现覆盖不一致（{bad} 条）"
    print("  ✅ edge.rooms 一致性（rooms 长度 ≤ weight）")

    print("\n✅ ALL GREEN")


if __name__ == "__main__":
    main()