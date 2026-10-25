# -*- coding: utf-8 -*-
"""仁谱 · 回填 person.first_seen（时间维度地基）

为什么需要：
  图谱要表达「谁先进入你的圈子、圈子怎么扩进来的」，必须有进入时间。
  但真库 `person.first_seen` 覆盖率是 **0/21823**——字段存在，从没填过。

怎么算（两个来源，取更早的那个）：
  ① 私聊：msg_index 里该 sender 的最早消息时间（最准）
  ② 群：  该人所在群的最早有消息时间
     —— 注意这是「群有记录的最早时间」，不等于「此人进群时间」，
        但它是一个**不会更晚**的近似（人进群必不晚于群开始有记录）。

用法：
  python scripts/backfill_first_seen.py --db <路径>            # 只报告
  python scripts/backfill_first_seen.py --db <路径> --apply    # 执行
"""
from __future__ import annotations

import argparse
import datetime
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def compute(con):
    """算出每人可得的 first_seen（dict: person_key -> ts）"""
    # ① 私聊最早
    priv = {}
    for r in con.execute(
            "SELECT m.sender, MIN(m.ts) FROM msg_index m "
            "JOIN person p ON p.person_key = m.sender "
            "WHERE m.ts > 0 GROUP BY m.sender").fetchall():
        priv[r[0]] = r[1]

    # ② 群：群会话（conv_key 以 wxroom: 开头）的最早消息时间
    room_first = {}
    for r in con.execute(
            "SELECT conv_key, MIN(ts) FROM msg_index "
            "WHERE ts > 0 AND conv_key LIKE 'wxroom:%' GROUP BY conv_key").fetchall():
        room_first[r[0]] = r[1]

    # 群 → 成员，取该成员所有群里最早的那个
    via_room = {}
    for r in con.execute(
            "SELECT rm.person_key, rm.room_key FROM room_member rm "
            "JOIN person p ON p.person_key = rm.person_key").fetchall():
        t = room_first.get(r[1])
        if t is None:
            continue
        cur = via_room.get(r[0])
        if cur is None or t < cur:
            via_room[r[0]] = t

    # 合并：取更早
    merged = dict(via_room)
    for k, v in priv.items():
        cur = merged.get(k)
        if cur is None or v < cur:
            merged[k] = v
    return merged, priv, via_room


def main():
    ap = argparse.ArgumentParser(description="回填 person.first_seen")
    ap.add_argument("--db", required=True)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    db = Path(a.db)
    if not db.exists():
        print(f"[X] 数据库不存在：{db}")
        return 1

    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    try:
        total = con.execute("SELECT COUNT(*) FROM person").fetchone()[0]
        already = con.execute(
            "SELECT COUNT(*) FROM person WHERE first_seen IS NOT NULL AND first_seen>0").fetchone()[0]
        merged, priv, via_room = compute(con)
        only_priv = len(set(priv) - set(via_room))
        only_room = len(set(via_room) - set(priv))
        both = len(set(priv) & set(via_room))

        print("═══ first_seen 可回填体检 ═══")
        print(f"  person 总数          {total}")
        print(f"  已有 first_seen      {already}")
        print(f"  可回填（并集）        {len(merged)}  ({len(merged)*100//max(total,1)}%)")
        print(f"    ├ 仅私聊可推        {only_priv}")
        print(f"    ├ 仅群可推          {only_room}")
        print(f"    └ 两边都有          {both}")
        if merged:
            lo, hi = min(merged.values()), max(merged.values())
            print(f"  时间跨度              "
                  f"{datetime.datetime.fromtimestamp(lo).date()} → "
                  f"{datetime.datetime.fromtimestamp(hi).date()}")

        if not a.apply:
            print("\n（只读报告，未写入。加 --apply 执行回填）")
            return 0

        # 只回填「当前为空」的，不覆盖已有值
        cur = con.cursor()
        changed = 0
        for pk, ts in merged.items():
            row = cur.execute(
                "SELECT first_seen FROM person WHERE person_key=?", (pk,)).fetchone()
            if row is None:
                continue
            if row[0] and row[0] > 0:
                continue
            cur.execute("UPDATE person SET first_seen=? WHERE person_key=?", (ts, pk))
            changed += 1
        con.commit()
        print(f"\n✅ 已回填 {changed} 人的 first_seen")
        after = con.execute(
            "SELECT COUNT(*) FROM person WHERE first_seen IS NOT NULL AND first_seen>0").fetchone()[0]
        print(f"   现在覆盖率: {after}/{total} = {after*100//max(total,1)}%")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())