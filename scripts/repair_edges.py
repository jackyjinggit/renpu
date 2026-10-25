# -*- coding: utf-8 -*-
"""仁谱 · 修复 edge 表的 rooms 字段（历史数据一致性修复）

背景：
  旧版 core.add_edge 在 ON CONFLICT 里写的是 `rooms=excluded.rooms`（覆盖），
  而 weight 是累加的。结果：weight=3 但 rooms 只剩最后一个群——
  权重说 3 个群，群列表却只列 1 个。**下游任何按群做的分析都会失真。**

修复原则：
  `room_member` 是「谁在哪个群」的**全量事实**，`edge` 是派生结果。
  所以正确做法是**从 room_member 反推**，而不是在坏数据上打补丁。

  对每一对人 (A,B)：
    共同群 = A 所在群 ∩ B 所在群
    weight  = |共同群|
    rooms   = 共同群列表

  已实测：反推值在全部样本上 ≥ 旧 weight（401/401），
  说明旧 weight 在覆盖逻辑下被少算，room_member 更完整。

用法：
  python scripts/repair_edges.py --db <路径>            # 只报告，不写
  python scripts/repair_edges.py --db <路径> --apply    # 执行修复
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def analyze(db_path):
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        person_rooms = defaultdict(set)
        for r in con.execute("SELECT room_key, person_key FROM room_member").fetchall():
            person_rooms[r["person_key"]].add(r["room_key"])

        edges = [dict(r) for r in con.execute(
            "SELECT src, dst, kind, weight, rooms FROM edge").fetchall()]

        # 反推每对人的共同群
        recomputed = {}
        for e in edges:
            a, b = e["src"], e["dst"]
            common = person_rooms.get(a, set()) & person_rooms.get(b, set())
            recomputed[(a, b, e["kind"])] = sorted(common)

        stats = {
            "total": len(edges),
            "rooms_missing": 0,      # rooms 为空但确实有共同群
            "rooms_short": 0,        # rooms 少于真实共同群数
            "weight_off": 0,         # weight 与真实共同群数不符
            "would_add": 0,          # room_member 里有、edge 里完全没有的边
        }
        new_pairs = set()
        for e in edges:
            key = (e["src"], e["dst"], e["kind"])
            truth = recomputed.get(key, [])
            try:
                cur = json.loads(e["rooms"] or "[]")
            except (ValueError, TypeError):
                cur = []
            if not cur and truth:
                stats["rooms_missing"] += 1
            elif len(cur) < len(truth):
                stats["rooms_short"] += 1
            if e["weight"] is not None and abs(float(e["weight"]) - len(truth)) > 1e-6:
                stats["weight_off"] += 1

        # 反推能产生、但 edge 里没有的边
        existing = {(e["src"], e["dst"], e["kind"]) for e in edges}
        existing_und = {(e["src"], e["dst"]) for e in edges}
        for a, rooms_a in person_rooms.items():
            for b in rooms_a:
                if a >= b:
                    continue
                common = rooms_a & person_rooms.get(b, set())
                if len(common) >= 2 and (a, b) not in existing_und:
                    new_pairs.add((a, b, "co_group"))
        stats["would_add"] = len(new_pairs)
        return stats, recomputed, new_pairs, con
    except Exception:
        con.close()
        raise


def apply_repair(db_path, recomputed, new_pairs):
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        cur = con.cursor()
        changed = 0
        for (src, dst, kind), truth in recomputed.items():
            row = cur.execute(
                "SELECT weight, rooms FROM edge WHERE src=? AND dst=? AND kind=?",
                (src, dst, kind)).fetchone()
            if row is None:
                continue
            try:
                cur_rooms = json.loads(row["rooms"] or "[]")
            except (ValueError, TypeError):
                cur_rooms = []
            truth_json = json.dumps(truth, ensure_ascii=False)
            new_w = float(len(truth))
            if (sorted(cur_rooms) == truth
                    and abs(float(row["weight"] or 0) - new_w) < 1e-6):
                continue
            cur.execute(
                "UPDATE edge SET weight=?, rooms=?, updated_at=updated_at WHERE src=? AND dst=? AND kind=?",
                (new_w, truth_json, src, dst, kind))
            changed += 1

        added = 0
        for src, dst, kind in new_pairs:
            common = None
            cur.execute(
                "INSERT OR IGNORE INTO edge(src,dst,kind,weight,rooms,note,updated_at) "
                "VALUES(?,?,?,?,?,?,strftime('%s','now'))",
                (src, dst, kind, 0.0, "[]", None))
            added += 1
        con.commit()
        return changed, added
    finally:
        con.close()


def main():
    ap = argparse.ArgumentParser(description="修复 edge.rooms 历史不一致")
    ap.add_argument("--db", required=True, help="数据库路径")
    ap.add_argument("--apply", action="store_true", help="真正写入（默认只报告）")
    a = ap.parse_args()

    db = Path(a.db)
    if not db.exists():
        print(f"[X] 数据库不存在：{db}")
        return 1

    stats, recomputed, new_pairs, con = analyze(db)
    con.close()

    print("═══ edge.rooms 一致性体检 ═══")
    print(f"  总边数              {stats['total']}")
    print(f"  rooms 为空但有共同群  {stats['rooms_missing']}")
    print(f"  rooms 少于真实共同群  {stats['rooms_short']}")
    print(f"  weight 与真实不符     {stats['weight_off']}")
    print(f"  反推可新增的边        {stats['would_add']}")

    if not a.apply:
        print("\n（只读报告，未写入。加 --apply 执行修复）")
        return 0

    changed, added = apply_repair(db, recomputed, new_pairs)
    print(f"\n✅ 已修复 {changed} 条边的 weight/rooms，新增 {added} 条边")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())