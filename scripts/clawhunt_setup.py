# -*- coding: utf-8 -*-
"""仁谱 · ClawHunt Agent 一键接入（2026-10-29）

ClawHunt 是「AI agent 之间的任务市场」：有人发布任务（带赏金），
agent 投标 → 被选中 → 交付 → 平台抽 25%，agent 拿 75%。

本脚本做三件事：
    1. 用 cph_ key 注册 / 更新 webhook URL（拿到 webhook_secret）
    2. 把 secret 落到本地（供 HMAC 验签用）
    3. 触发一次能力探针，并读回得分

用法：
    python scripts/clawhunt_setup.py --key cph_xxx --webhook https://xxx/cph-webhook
    python scripts/clawhunt_setup.py --key cph_xxx --status     # 只看状态
    python scripts/clawhunt_setup.py --key cph_xxx --probe      # 只触发探针

⚠️ webhook URL 必须**公网可达**（不能 localhost）——
   ClawHunt 官方明确拒绝私网地址（SSRF 防护）。
   本地开发用隧道：python scripts/serve_public.py
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import clawhunt  # noqa: E402

SECRET_FILE = ROOT / "config" / "clawhunt_secret.txt"


def _save_secret(secret: str | None):
    if not secret:
        return
    SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    SECRET_FILE.write_text(secret, encoding="utf-8")
    try:
        # Windows 下不设权限也会被本机其他用户读 —— 但 secret 只用于验签，
        # 丢了最坏是「不验签」，不是泄露。用 ACL 收紧一点更稳。
        import os
        os.chmod(SECRET_FILE, 0o600)
    except Exception:
        pass


def _show_me(api_key: str):
    r = clawhunt.get_me(api_key)
    print(f"  GET /api/v1/me → HTTP {r['http']}")
    body = r["body"]
    if isinstance(body, str):
        print("   ", body[:300])
        return {}
    agent = (body.get("data") or {}).get("agent") or body.get("data") or {}
    if not agent:
        print("   ", json.dumps(body, ensure_ascii=False)[:400])
        return {}
    print(f"    name                : {agent.get('name')}")
    print(f"    id                  : {agent.get('id')}")
    print(f"    admission_status    : {agent.get('admission_status')}")
    print(f"    webhook_url         : {agent.get('webhook_url')}")
    sec = agent.get("webhook_secret")
    print(f"    webhook_secret      : {(sec[:12] + '…') if sec else None}")
    print(f"    capability_probe    : {agent.get('capability_probe_status')}")
    print(f"    capabilities_verified: {agent.get('capabilities_verified')}")
    return agent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True, help="cph_ 开头的 agent API key")
    ap.add_argument("--webhook", help="要注册的 webhook URL（公网可达）")
    ap.add_argument("--status", action="store_true", help="只看状态")
    ap.add_argument("--probe", action="store_true", help="只触发能力探针")
    a = ap.parse_args()

    print("=" * 60)
    print("  仁谱 Renpu · ClawHunt Agent 接入")
    print("=" * 60)

    if not a.key.startswith("cph_"):
        print("  ❌ key 必须以 cph_ 开头（ClawHunt agent API key）")
        return 1

    if a.webhook:
        print(f"\n  注册 webhook：{a.webhook}")
        r = clawhunt.register_webhook(a.key, a.webhook)
        print(f"  HTTP {r['http']}")
        body = r["body"]
        if isinstance(body, str):
            print("   ", body[:400])
        else:
            data = body.get("data") or {}
            print("   ", data.get("message") or json.dumps(body, ensure_ascii=False)[:300])
            sec = data.get("webhook_secret")
            if sec:
                _save_secret(sec)
                print(f"  ✅ secret 已存到 {SECRET_FILE}")
        print()

    print("  ── 当前状态 ──")
    agent = _show_me(a.key)
    print()

    if a.probe:
        print("  ── 触发能力探针 ──")
        r = clawhunt.run_probe(a.key)
        print(f"  HTTP {r['http']}  {json.dumps(r['body'], ensure_ascii=False)[:250] if not isinstance(r['body'], str) else r['body'][:250]}")
        print()
        print("  ── 探针得分 ──")
        st = clawhunt.probe_status(a.key)
        if st["http"] == 200 and not isinstance(st["body"], str):
            bd = st["body"].get("data") or st["body"]
            sb = bd.get("scoring_breakdown") or {}
            for k in ("file_transfer", "visual_observation"):
                v = sb.get(k)
                if isinstance(v, dict):
                    print(f"    {k:20s} {v.get('score')}/100  (http {v.get('http_status')}, {v.get('latency')}ms)")
                elif v is not None:
                    print(f"    {k:20s} {v}")
            if not sb:
                print("   ", json.dumps(bd, ensure_ascii=False)[:400])
        else:
            print("   ", str(st["body"])[:300])
        print()

    print("  ── 下一步 ──")
    print("     1. 确认 admission_status = approved（才能投标）")
    print("     2. 探针 ≥70/100 拿 Verified Capabilities 徽章（可选，不影响接单）")
    print("     3. 接单循环：GET /api/v1/problems → POST .../bid → POST .../solution")
    print("     ⚠️ 每次请求都要带 User-Agent（平台会 403 掉 Python urllib 默认 UA）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
