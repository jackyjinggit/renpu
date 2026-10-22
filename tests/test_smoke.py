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

    print("\n✅ ALL GREEN")


if __name__ == "__main__":
    main()