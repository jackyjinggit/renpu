# -*- coding: utf-8 -*-
"""仁谱 · ClawHunt 轮询接单（2026-10-29）

**轮询模式**：不需要 webhook、不需要隧道、不需要机器一直开着。
用 cph_ key 直接调 REST API。

工作流：
    1. GET  /api/v1/problems              浏览开放任务
    2. POST /api/v1/problems/{id}/bid     投标（**对外承诺，须人工授权**）
    3. GET  /api/v1/me                    查中标状态
    4. POST /api/v1/problems/{id}/solution 交付

⚠️ 本脚本**默认只读**（浏览 + 筛活 + 生成投标草稿）。
   投标与交付必须显式 --bid / --deliver，且带 --yes 确认。

官方硬规则（https://clawhunt.store/for-agents）：
    · 「Greeting-only bids are filtered」—— 投标必须**带方案**，不是只报价
    · 「Deliveries with artefacts but no execution evidence」会被拒
      → 交付要附**可复核的证据**（哈希 / 测试输出 / 文件）
    · 「Duplicate submissions against the same requirement」会被拒 → 同一单只交一次
    · 平台抽 25%，agent 拿 75%
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
import time
import urllib.request
import urllib.error

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BASE = "https://clawhunt.store"
UA = "renpu/1.0 (+https://github.com/jackyjinggit/renpu)"
DEFAULT_KEY_FILE = r"C:\sensitive\api manage\clawhunt agent renpu api key.txt"


class Agent:
    def __init__(self, key: str):
        self.key = key

    def call(self, path, method="GET", body=None):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            BASE + path, data=data, method=method,
            headers={"Authorization": f"Bearer {self.key}",
                     "Content-Type": "application/json", "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                raw = r.read().decode("utf-8", "replace")
                return r.status, json.loads(raw or "{}")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")[:400]
        except Exception as e:
            return 0, str(e)

    def problems(self):
        code, d = self.call("/api/v1/problems")
        if not isinstance(d, str):
            return ((d.get("data") or {}).get("problems") or [])
        return []

    def me(self):
        code, d = self.call("/api/v1/me")
        if not isinstance(d, str):
            return (d.get("data") or {}).get("agent") or {}
        return {}

    def detail(self, pid):
        code, d = self.call(f"/api/v1/problems/{pid}")
        if not isinstance(d, str):
            return (d.get("data") or {}).get("problem") or d.get("data") or {}
        return {}

    def bid(self, pid, amount_cents, plan):
        # 字段名实测（2026-10-29）：`proposal` 不被允许；
        # `message` / `proposal_text` 才是对的（平台对问候式投标会 422 拒绝）。
        return self.call(f"/api/v1/problems/{pid}/bid", method="POST",
                         body={"amount": amount_cents, "message": plan})

    def deliver(self, pid, content, evidence=None):
        body = {"content": content}
        if evidence:
            body["evidence"] = evidence
        return self.call(f"/api/v1/problems/{pid}/solution", method="POST", body=body)


# ── 仁谱的能力画像：用来筛活 ─────────────────────────────
# 仁谱是「关系图谱引擎」：从聊天记录建关系模型、出可视化图谱、
# 实体-关系-证据三层、本地 SQLite、零外部依赖。
RENPUS_SKILLS = {
    "graph": "实体 / 关系 / 证据三层的图谱建模",
    "visual": "单文件 HTML 可视化（零外链、可离线）",
    "sqlite": "本地 SQLite 数据模型与索引",
    "dedupe": "去重、别名归并、ID 稳定化",
    "evidence": "每条关系可回溯到原始证据",
    "export": "Obsidian vault 导出 / JSON API / HTML",
}

# 任务里出现这些词 → 仁谱的活儿
MATCH_TERMS = (
    "知识图谱", "实体", "关系", "证据引用", "graph", "实体关系",
    "知识库", "元数据", "来源追踪", "分类", "检索", "引用",
)


def score_match(task: dict) -> tuple[int, list[str]]:
    """给任务打分：仁谱能不能接。"""
    text = json.dumps(task, ensure_ascii=False).lower()
    hits = [t for t in MATCH_TERMS if t.lower() in text]
    return len(hits), hits


def plan_for(task: dict) -> str:
    """生成投标方案（**不是只报价**——官方会过滤问候式投标）。

    ⚠️ 平台 `message` 字段**上限 1000 字符**（实测 422），所以压到 ~600。
    """
    title = (task.get("title") or "")[:60]
    return (
        f"For: {title}\n\n"
        "Build, not advise. Renpu is a local-first relationship-graph engine.\n\n"
        "1. Model the sub-domain as entities, relations and evidence links in "
        "SQLite. Every relation keeps a pointer to its source record, so any "
        "edge can be traced back to the message that produced it.\n"
        "2. Emit the graph three ways: single-file HTML (zero external "
        "requests, opens offline), JSON API, Obsidian vault.\n"
        "3. Redact by data source: invented demo names stay readable, real "
        "identifiers become numbered placeholders.\n"
        "4. Evidence a stranger can re-check: the HTML file, its SHA-256, and "
        "test output (node/edge counts, broken-link count = 0).\n\n"
        "Runtime needs no external service. Nothing leaves the machine. "
        "Rebuilding from the same input is deterministic."
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key-file", default=DEFAULT_KEY_FILE)
    ap.add_argument("--list", action="store_true", help="列出任务市场")
    ap.add_argument("--match", action="store_true", help="只列仁谱匹配的任务")
    ap.add_argument("--detail", type=int, help="看某单详情")
    ap.add_argument("--draft", type=int, help="为某单生成投标草稿（不发）")
    ap.add_argument("--bid", type=int, help="投标某单（须配 --yes）")
    ap.add_argument("--amount", type=int, default=0, help="投标金额（分，USD 整数分）")
    ap.add_argument("--yes", action="store_true", help="确认这是对外承诺")
    ap.add_argument("--me", action="store_true", help="查 agent 状态")
    a = ap.parse_args()

    kf = pathlib.Path(a.key_file)
    if not kf.exists():
        print(f"  ❌ key 文件不存在：{kf}")
        print("     请把 cph_ key 存到该路径，或用 --key-file 指定")
        return 1
    ag = Agent(kf.read_text(encoding="utf-8").strip())

    if a.me:
        m = ag.me()
        for k in ("id", "name", "admission_status", "capability_probe_status",
                  "webhook_url", "capabilities_verified"):
            print(f"  {k:24s}: {m.get(k)}")
        return 0

    if a.list or a.match:
        items = ag.problems()
        print(f"═══ {len(items)} 个开放任务 ═══\n")
        shown = 0
        for t in items:
            n, hits = score_match(t)
            if a.match and n == 0:
                continue
            shown += 1
            tid = t.get("id")
            title = (t.get("title") or "")[:60]
            ref = t.get("reference_price")
            usd = (ref / 100.0) if isinstance(ref, (int, float)) else ref
            print(f"  #{tid}  {title}")
            print(f"       参考价 {ref}（约 ${usd}）  投标数 {t.get('bid_count','?')}"
                  f"  匹配度 {n} {hits[:4]}")
            print()
        if a.match and shown == 0:
            print("  没有匹配的任务")
        return 0

    if a.detail:
        d = ag.detail(a.detail)
        print(json.dumps(d, ensure_ascii=False, indent=2)[:2500])
        return 0

    if a.draft:
        d = ag.detail(a.draft)
        if not d:
            d = next((t for t in ag.problems() if t.get("id") == a.draft), {})
        print("═══ 投标草稿（未发送）═══\n")
        print(plan_for(d))
        print("\n═══ 要发送：--bid", a.draft, "--amount <分> --yes ═══")
        return 0

    if a.bid:
        if not a.yes:
            print("  ❌ 投标是**对外承诺**（金额 + 交付义务 + 平台 25% 抽成）")
            print("     确认请带 --yes")
            return 1
        if a.amount <= 0:
            print("  ❌ --amount 必须是正整数（分）")
            return 1
        d = ag.detail(a.bid)
        if not d:
            d = next((t for t in ag.problems() if t.get("id") == a.bid), {})
        plan = plan_for(d)
        code, r = ag.bid(a.bid, a.amount, plan)
        print(f"  HTTP {code}")
        print("  ", r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)[:400])
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
