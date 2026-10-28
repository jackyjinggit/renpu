# -*- coding: utf-8 -*-
"""仁谱 · 上台演示包 · 一键出图

用途（2026-10-28 ClawHunt 黑客松）：
    现场只有 3 分钟，不能让人等命令跑。点一下，三张图 + 一个档案页全出。

设计：
    · **断网可跑** —— 全部本地算，不请求任何网络
    · **零参数** —— 双击就跑，不需要懂命令行
    · **失败也能演** —— 出图失败时用预生成的兜底文件

用法：
    python demo_pack.py            # 出全套
    python demo_pack.py --out 目录 # 指定输出
"""
from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

# 演示库里挑的人（名字是随机生成的，这里按关系特征找，不写死）
PICK_BY = {
    "center": "群共现最多的人",
    "private": "只在私聊里出现、任何群里都没有的人",
    "hub": "消息最多的人",
}


def _db_path() -> pathlib.Path:
    """演示库位置：优先环境变量，其次用户级 demo 库。"""
    env = os.environ.get("RENPU_DB_PATH")
    if env:
        return pathlib.Path(env)
    home = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    cand = pathlib.Path(home) / "renpu" / "demo" / "renpu.db"
    return cand


def _run(con, args, out):
    """跑一个 CLI 命令，返回是否成功。"""
    env = dict(os.environ)
    env["RENPU_DB_PATH"] = str(con)
    env["RENPU_PYTHON"] = sys.executable
    cmd = [sys.executable, str(ROOT / "cli.py")] + args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=env)
        ok = r.returncode == 0 and out.exists()
        if not ok:
            tail = (r.stdout or r.stderr or "").strip().splitlines()
            print(f"   ⚠️ {' '.join(args[:2])} 失败：{tail[-1] if tail else '?'}")
        return ok
    except Exception as exc:
        print(f"   ⚠️ {' '.join(args[:2])} 异常：{exc}")
        return False


def _pick_people(con):
    """按关系特征挑三个人（不写死名字 —— demo 人名是随机生成的）。"""
    import core
    db = core.connect(con)
    out = {}
    try:
        r = db.execute(
            "SELECT p.display_name FROM person p JOIN edge e "
            "ON (e.src=p.person_key OR e.dst=p.person_key) AND e.kind='co_group' "
            "WHERE p.person_key != 'me' GROUP BY p.person_key "
            "ORDER BY COUNT(*) DESC LIMIT 1").fetchone()
        if r:
            out["center"] = r[0]
        r = db.execute(
            "SELECT p.display_name FROM person p JOIN edge e "
            "ON (e.src=p.person_key OR e.dst=p.person_key) AND e.kind='direct' "
            "WHERE p.person_key != 'me' AND NOT EXISTS ("
            "  SELECT 1 FROM edge e2 WHERE (e2.src=p.person_key OR e2.dst=p.person_key) "
            "  AND e2.kind='co_group') ORDER BY e.weight DESC LIMIT 1").fetchone()
        if r:
            out["private"] = r[0]
        r = db.execute(
            "SELECT display_name FROM person WHERE person_key != 'me' "
            "ORDER BY msg_total DESC LIMIT 1").fetchone()
        if r:
            out["hub"] = r[0]
    finally:
        db.close()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None, help="输出目录")
    ap.add_argument("--db", default=None, help="指定库路径")
    a = ap.parse_args()

    out = pathlib.Path(a.out) if a.out else (HERE / "演示产物")
    out.mkdir(parents=True, exist_ok=True)
    db = pathlib.Path(a.db) if a.db else _db_path()

    print("=" * 56)
    print("  仁谱 Renpu · 演示包")
    print("=" * 56)

    if not db.exists():
        print(f"  ❌ 找不到演示库：{db}")
        print("     先跑一次：renpu-cli.exe ingest --demo")
        return 1
    print(f"  库：{db}")
    print(f"  出图：{out}")
    print()

    people = _pick_people(db)
    if people:
        print("  演示用到的人：")
        for k, label in PICK_BY.items():
            if k in people:
                print(f"    · {label}：{people[k]}")
        print()

    t0 = time.time()
    steps = []

    def step(label, args, fname):
        ok = _run(db, args, out / fname)
        steps.append((label, ok))
        print(f"  {'✅' if ok else '❌'} {label} → {fname}")
        return ok

    step("关系图谱", ["graph", "--limit", "60", "--focus", "connected",
                    "--share", "--out", str(out / "1-关系图谱.html")],
         "1-关系图谱.html")
    step("3D 关系圈", ["graph3d", "--person", people.get("center", ""),
                     "--share", "--out", str(out / "2-3D关系圈.html")],
         "2-3D关系圈.html")
    step("一人一档", ["profile", people.get("hub", ""),
                    "--share", "--out", str(out / "3-人物档案.html")],
         "3-人物档案.html")
    step("Obsidian 导出", ["export-vault", "--limit", "80", "--min-messages", "5",
                       "--out", str(out / "4-Obsidian导出")],
         "4-Obsidian导出/")

    print()
    ok_n = sum(1 for _, ok in steps if ok)
    print(f"  {ok_n}/{len(steps)} 完成，用时 {time.time()-t0:.1f}s")
    if ok_n:
        print()
        print("  演示顺序建议：")
        print("    1) 关系图谱 —— 先说「这是关系网，不是消息流」")
        print("       → 点「我」（白色节点），看青色私聊边有多少条")
        print("    2) 3D 关系圈 —— 聚焦一个人，看他的圈层结构")
        print("    3) 一人一档 —— 一个人的完整档案")
        if "private" in people:
            print(f"    ★ 加分项：打开「{people['private']}」的档案 ——")
            print("      他只跟你私聊、任何群里都没有，")
            print("      旧算法里这类人是「零关系」（看不见）。")
        print("    4) Obsidian 导出 —— 关系图可以直接进你现有的笔记系统")
    print()
    print(f"  产物目录：{out}")
    print("  全部在本地生成，不联网。")
    return 0 if ok_n == len(steps) else 2


if __name__ == "__main__":
    raise SystemExit(main())
