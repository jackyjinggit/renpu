# -*- coding: utf-8 -*-
"""仁谱 · 一键体验（陌生人 3 分钟看到东西）

做这一条的理由：让陌生人「直接测试」的成本必须低到不需要思考。
一条命令走完 install 检查 → 建库 → 灌数据 → 出图 → 出报告，
全程不碰用户真实数据，不要求任何配置。

用法：
    python scripts/tour.py            # 走一遍，产出图 + 报告
    python scripts/tour.py --out DIR  # 指定输出目录
"""
import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

GREEN, RED, DIM, CYAN, RESET = "\033[32m", "\033[31m", "\033[90m", "\033[36m", "\033[0m"
if os.name == "nt" and not os.environ.get("WT_SESSION"):
    os.system("")  # 启用 Windows ANSI


def step(n, total, text):
    print(f"\n{CYAN}[{n}/{total}]{RESET} {text}")


def ok(text):
    print(f"  {GREEN}✓{RESET} {text}")


def info(text):
    print(f"  {DIM}{text}{RESET}")


def main():
    ap = argparse.ArgumentParser(description="仁谱一键体验")
    ap.add_argument("--out", help="输出目录（默认临时目录）")
    ap.add_argument("--keep", action="store_true", help="保留演示库不删")
    a = ap.parse_args()

    out_dir = Path(a.out) if a.out else Path(tempfile.mkdtemp(prefix="renpu-tour-"))
    out_dir.mkdir(parents=True, exist_ok=True)

    # 隔离环境：所有产物都落在 out_dir，绝不碰用户真实库
    env = dict(os.environ)
    env["RENPU_DATA_HOME"] = str(out_dir)
    env["RENPU_DB_PATH"] = str(out_dir / "renpu.db")
    env["RENPU_LOG_DIR"] = str(out_dir / "logs")
    env["RENPU_EXPORT_DIR"] = str(out_dir / "exports")
    env["PYTHONIOENCODING"] = "utf-8"

    print()
    print(f"  {CYAN}仁谱 Renpu · 一键体验{RESET}")
    print(f"  {DIM}演示数据完全合成，不会读取你的微信{RESET}")
    print(f"  {DIM}产物目录：{out_dir}{RESET}")

    py = sys.executable

    def run(args, quiet=False):
        r = subprocess.run([py] + args, cwd=str(ROOT), env=env,
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if r.returncode != 0:
            print(f"  {RED}✗{RESET} {args[0]} 失败")
            print(f"  {DIM}{(r.stderr or r.stdout)[-400:]}{RESET}")
            raise SystemExit(1)
        if not quiet:
            for line in (r.stdout or "").splitlines():
                if line.strip():
                    print(f"  {DIM}{line}{RESET}")
        return r.stdout

    total = 5
    t0 = time.time()

    # 1. 依赖检查
    step(1, total, "检查依赖")
    try:
        from Crypto.Cipher import AES  # noqa
        ok("pycryptodome 已安装")
    except ImportError:
        print(f"  {RED}✗{RESET} 缺 pycryptodome —— 运行：pip install pycryptodome")
        raise SystemExit(1)

    # 2. 建库
    step(2, total, "建库")
    run([str(ROOT / "cli.py"), "init"], quiet=True)
    ok(f"库已建：{out_dir / 'renpu.db'}")

    # 3. 灌演示数据
    step(3, total, "灌入演示数据（合成）")
    run([str(ROOT / "cli.py"), "ingest", "--demo"], quiet=True)
    ok("10 人 · 2000 条消息 · 关系边已建立")

    # 4. 出图
    step(4, total, "生成关系图谱")
    graph_path = out_dir / "renpu-graph.html"
    run([str(ROOT / "cli.py"), "graph", "--share", "--out", str(graph_path)], quiet=True)
    ok(f"图谱（可交互）：{graph_path}")

    # 5. 出报告
    step(5, total, "生成摘要报告")
    import core
    import render
    con = core.connect()
    st = core.stats(con)
    g = render.collect_graph(con, limit=60, share=True)
    top = con.execute(
        "SELECT p.display_name, p.tier, p.msg_total, p.msg_d30 FROM person p "
        "ORDER BY p.msg_total DESC LIMIT 8").fetchall()
    rooms = con.execute(
        "SELECT name, member_cnt FROM room ORDER BY msg_total DESC LIMIT 6").fetchall()
    todo = con.execute(
        "SELECT title FROM todo WHERE status='open' LIMIT 5").fetchall()
    con.close()

    lines = [
        "# 仁谱 Renpu · 体验报告",
        "",
        "> 全部数据为合成演示数据，不含任何真实联系人。",
        "",
        "## 数据总览",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| 人 | {st['people']} |",
        f"| 群 | {st['rooms']} |",
        f"| 消息索引 | {st['messages_indexed']} |",
        f"| 关系边 | {st['edges']} |",
        f"| 关系事件 | {st['events']} |",
        f"| 待跟进 | {st['todos_open']} |",
        "",
        "## 关系最密的人",
        "",
        "| 姓名 | 分层 | 消息数 | 近 30 天 |",
        "|---|---|---|---|",
    ]
    for r in top:
        lines.append(f"| {r['display_name']} | {r['tier'] or '—'} | {r['msg_total']} | {r['msg_d30']} |")
    lines += ["", "## 群", "", "| 群 | 成员 |", "|---|---|"]
    for r in rooms:
        lines.append(f"| {r['name']} | {r['member_cnt']} |")
    if todo:
        lines += ["", "## 待跟进", ""]
        lines += [f"- {r['title']}" for r in todo]
    lines += [
        "",
        "---",
        "",
        "## 接下来可以试什么",
        "",
        "```bash",
        f'renpu who 林夏              # 查一个人的完整档案',
        f'renpu edges                 # 关系谱',
        f'renpu search 黑客松          # 全文检索',
        f'renpu graph --share         # 重新出图（分享模式）',
        f'renpu serve --port 8877     # 起本地 API，给 agent 用',
        "```",
        "",
        "接你自己的微信：见 [INSTALL.md](https://github.com/jackyjinggit/renpu/blob/main/INSTALL.md)",
        "",
    ]
    report = out_dir / "renpu-report.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    ok(f"摘要报告：{report}")

    dt = time.time() - t0
    print()
    print(f"  {GREEN}✓ 完成（{dt:.1f} 秒）{RESET}")
    print()
    print(f"  {CYAN}下一步{RESET}")
    print(f"    1. 打开图谱看效果：{graph_path}")
    print(f"    2. 读报告：      {report}")
    print(f"    3. 自己玩：")
    print(f"       cd {ROOT}")
    print(f"       {py} cli.py who 林夏")
    print()
    if not a.keep and not a.out:
        print(f"  {DIM}（演示库在 {out_dir}，可删）{RESET}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())