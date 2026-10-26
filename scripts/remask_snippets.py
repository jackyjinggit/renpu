# -*- coding: utf-8 -*-
"""仁谱 · snippet 重新遮蔽（修历史遗留的 PII 残留）

为什么需要：
    仁谱早期版本的 PII 遮蔽只覆盖手机号 / 身份证 / 卡号，
    没覆盖 wxid、公众号 id、邮箱、QQ、微信号。
    微信消息正文里常带 `wxid_xxx:` 前缀（发送者标识），
    所以老库里这些标识是明文存的 —— 这是既存数据的 PII 泄露。

这个脚本做什么：
    对 msg_index.snippet 逐行重新跑一遍 core.mask_pii，
    变了就更新；同时把 msg_fts 里的 snippet 一起更新（否则搜索还会搜到旧的）。

安全设计：
    · 默认**只体检不写**（--apply 才写）
    · 写前必须先备份（脚本自己备份，除非 --no-backup）
    · 幂等：已经遮过的再跑不会变
    · 不动 person / edge / room 等任何其他表

用法：
    python scripts/remask_snippets.py --db <库路径>            # 只体检
    python scripts/remask_snippets.py --db <库路径> --apply    # 备份后重写
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core  # noqa: E402


def scan(con, limit=None):
    """找出 snippet 里还有可遮蔽标识的行。"""
    sql = ("SELECT rowid, snippet FROM msg_index "
           "WHERE snippet LIKE '%wxid[_]%' OR snippet LIKE '%gh[_]%' "
           "OR snippet LIKE '%@%' OR snippet LIKE '%[微信号]%'")
    if limit:
        sql += f" LIMIT {int(limit)}"
    hits = []
    for rowid, snip in con.execute(sql).fetchall():
        masked, pii = core.mask_pii(snip or "")
        if pii and masked != snip:
            hits.append((rowid, snip, masked))
    return hits


def apply_mask(con, hits, batch=2000):
    """重写 msg_index 与 msg_fts 里的 snippet。"""
    changed = 0
    for i in range(0, len(hits), batch):
        chunk = hits[i:i + batch]
        con.executemany(
            "UPDATE msg_index SET snippet=?, has_pii=1 WHERE rowid=?",
            [(m, rid) for rid, _s, m in chunk])
        con.executemany(
            "UPDATE msg_fts SET snippet=? WHERE rowid=?",
            [(m, rid) for rid, _s, m in chunk])
        con.commit()
        changed += len(chunk)
    return changed


def backup(db: Path) -> Path:
    """备份到 <db 同级>.bak-mask-<时间戳>"""
    bak = db.with_name(db.name + ".bak-mask-" + time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(db, bak)
    # WAL 也要一起备，否则备份不完整
    for suf in ("-wal", "-shm"):
        w = db.with_name(db.name + suf)
        if w.exists():
            shutil.copy2(w, bak.with_name(bak.name + suf))
    return bak


def main():
    ap = argparse.ArgumentParser(description="仁谱 · snippet 重新遮蔽")
    ap.add_argument("--db", help="数据库路径（默认用配置里的）")
    ap.add_argument("--apply", action="store_true", help="真的写入（默认只体检）")
    ap.add_argument("--no-backup", action="store_true", help="跳过自动备份（不建议）")
    ap.add_argument("--limit", type=int, help="只处理前 N 行（体检用）")
    a = ap.parse_args()

    db = Path(a.db) if a.db else Path(core.DB_PATH)
    if not db.exists():
        raise SystemExit(f"库不存在：{db}")

    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    try:
        total = con.execute("SELECT COUNT(*) FROM msg_index").fetchone()[0]
        print(f"── 体检 {db}")
        print(f"   消息总数：{total}")
        hits = scan(con, a.limit)
        print(f"   需要重遮：{len(hits)} 条")
        if hits:
            print("   样例（前 3 条，只显示遮蔽效果）：")
            for rid, old, new in hits[:3]:
                print(f"     前: {old[:64]}")
                print(f"     后: {new[:64]}")
                print()
        if not a.apply:
            print("   （体检模式，未写入。加 --apply 执行重写）")
            return
        if not hits:
            print("   无需修改。")
            return

        if not a.no_backup:
            bak = backup(db)
            print(f"   已备份 → {bak}")

        n = apply_mask(con, hits)
        print(f"   ✅ 已重写 {n} 条（msg_index + msg_fts 同步）")

        # 复检
        left = scan(con)
        print(f"   复检残留：{len(left)} 条")
        if left:
            print("   ⚠️ 仍有残留，样例：")
            for rid, old, _new in left[:3]:
                print(f"     {old[:70]}")
    finally:
        con.close()


if __name__ == "__main__":
    main()
