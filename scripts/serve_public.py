# -*- coding: utf-8 -*-
"""仁谱 · 公网 Agent 一键启动（ClawHunt 上架用）

用途（2026-10-29 ClawHunt）：
    ClawHunt 要注册 Agent，必须给一个**公网可达的 webhook URL**。
    仁谱是本地工具 → 用隧道把本地 API 暴露到公网。

用法：
    python scripts/serve_public.py              # 起服务 + 隧道，打印地址
    python scripts/serve_public.py --db <路径>  # 指定库（默认 demo 库）
    python scripts/serve_public.py --port 8901  # 指定本地端口

⚠️ 两条硬纪律：
    1. **默认用 demo 库**（虚构人物）—— 真实库暴露到公网是 PII 红线。
       真要连真实库，必须显式 --db，并且自己承担脱敏风险。
    2. **隧道地址重启就变** —— 每次重启都要重新在 ClawHunt 后台更新 webhook URL。
       要固定地址得配自己的域名 + CNAME。
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_PORT = 8901


def _find_tunnel_tool():
    """找一个可用的隧道工具：优先 cloudflared（域名相对稳定），备选 ngrok。"""
    cands = []
    which = shutil.which
    if which("cloudflared"):
        cands.append(("cloudflared", which("cloudflared")))
    tmp = pathlib.Path.home() / "AppData/Local/Temp/cloudflared.exe"
    if tmp.exists():
        cands.append(("cloudflared", str(tmp)))
    desk = pathlib.Path.home() / "Desktop/ngrok.exe"
    if desk.exists():
        cands.append(("ngrok", str(desk)))
    if which("ngrok"):
        cands.append(("ngrok", which("ngrok")))
    return cands


def _start_server(port, db_path):
    env = dict(os.environ)
    env["RENPU_DB_PATH"] = str(db_path)
    log = open(pathlib.Path.home() / "AppData/Local/Temp/renpu-serve.log", "w", encoding="utf-8")
    p = subprocess.Popen([sys.executable, str(ROOT / "server.py"), "--port", str(port)],
                         cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT)
    time.sleep(3.5)
    # 健康检查
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/stats", timeout=10) as r:
            r.read()
        return p
    except Exception as e:
        p.terminate()
        raise SystemExit(f"服务起不来：{e}")


def _start_tunnel(tool, port):
    """起隧道，返回 (进程, 公网地址)。"""
    if tool == "cloudflared":
        logf = open(pathlib.Path.home() / "AppData/Local/Temp/renpu-cf.log", "w", encoding="utf-8")
        exe = shutil.which("cloudflared") or str(pathlib.Path.home() / "AppData/Local/Temp/cloudflared.exe")
        p = subprocess.Popen([exe, "tunnel", "--url", f"http://127.0.0.1:{port}", "--no-autoupdate"],
                             stdout=logf, stderr=subprocess.STDOUT)
        for _ in range(40):
            time.sleep(1.5)
            try:
                txt = (pathlib.Path.home() / "AppData/Local/Temp/renpu-cf.log").read_text(
                    encoding="utf-8", errors="ignore")
                m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", txt)
                if m:
                    return p, m.group(0)
            except Exception:
                pass
        p.terminate()
        raise SystemExit("cloudflared 起隧道超时")
    # ngrok
    exe = shutil.which("ngrok") or str(pathlib.Path.home() / "Desktop/ngrok.exe")
    p = subprocess.Popen([exe, "http", str(port), "--log=stdout"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(30):
        time.sleep(1.5)
        try:
            with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=5) as r:
                import json
                d = json.loads(r.read().decode())
                for t in d.get("tunnels", []):
                    if t.get("public_url"):
                        return p, t["public_url"]
        except Exception:
            pass
    p.terminate()
    raise SystemExit("ngrok 起隧道超时")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--db", default=None, help="库路径（默认用户级 demo 库）")
    ap.add_argument("--no-tunnel", action="store_true", help="只起本地服务，不起隧道")
    a = ap.parse_args()

    if a.db:
        db = pathlib.Path(a.db)
    else:
        home = os.environ.get("LOCALAPPDATA") or str(pathlib.Path.home())
        db = pathlib.Path(home) / "renpu" / "demo" / "renpu.db"

    print("=" * 60)
    print("  仁谱 Renpu · 公网 Agent")
    print("=" *60)
    if not db.exists():
        print(f"  ❌ 库不存在：{db}")
        print("     先跑：renpu-cli.exe ingest --demo")
        return 1
    print(f"  库：{db}")
    print(f"  本地端口：{a.port}")

    srv = _start_server(a.port, db)
    print("  ✅ 本地服务已起")
    print(f"     http://127.0.0.1:{a.port}/api/endpoints")

    if a.no_tunnel:
        print("\n  按 Ctrl+C 停止")
        try:
            srv.wait()
        except KeyboardInterrupt:
            srv.terminate()
        return 0

    tools = _find_tunnel_tool()
    if not tools:
        print("\n  ❌ 没找到隧道工具（cloudflared / ngrok）")
        print("     下载 cloudflared：")
        print("     https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe")
        return 1

    name, exe = tools[0]
    print(f"\n  起隧道（{name}）...")
    tun, url = _start_tunnel(name, a.port)
    print(f"  ✅ 公网地址：{url}")

    # 端到端验证
    try:
        req = urllib.request.Request(f"{url}/api/webhook", method="POST",
                                     data=b'{"task":"stats","input":{}}',
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode()[:120]
        print(f"  ✅ webhook 端到端验证通过：{body}")
    except Exception as e:
        print(f"  ⚠️ webhook 验证失败：{e}")

    print("\n  ── 填进 ClawHunt「Create an agent」──")
    print(f"     Agent name      : renpu")
    print(f"     Agent type      : AI and ML specialist")
    print(f"     Model provider  : 留空")
    print(f"     Model name      : 留空")
    print(f"     Webhook URL     : {url}/api/webhook")
    print(f"     Context window  : 200000")
    print(f"     Max output      : 8192")
    print(f"     Additional tools: code_execution, web_browsing, git_operations")
    print(f"     Description     : Local-first relationship genealogy engine.")
    print(f"                      Builds a single-file HTML graph of who is connected to whom")
    print(f"                      from chat records. Circles auto-colored, teal=private chat,")
    print(f"                      grey=shared group, white=you. 3D circles, person files,")
    print(f"                      Obsidian export, full-text search, local API.")
    print(f"                      All computation local; no data leaves the machine.")
    print()
    print(f"  ⚠️ 隧道地址重启就变 → 重启后要在 ClawHunt 后台更新 webhook URL")
    print(f"  ⚠️ 当前用的是 demo 库（虚构人物）—— 真实库暴露公网是 PII 红线")
    print("\n  按 Ctrl+C 停止")
    try:
        srv.wait()
    except KeyboardInterrupt:
        tun.terminate()
        srv.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
