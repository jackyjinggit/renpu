# -*- coding: utf-8 -*-
"""仁谱 Renpu · 统一入口（单文件 EXE 用）

一个 EXE，两种模式：
  Renpu.exe                → 独立桌面界面（双击即用，无黑框）
  Renpu.exe stats          → 命令行模式（带控制台输出）
  Renpu.exe tour           → 一键体验

为什么需要这个文件：
  PyInstaller 的 --windowed 模式下 sys.stdout 是 None，命令行模式必须自己
  申请控制台；而 GUI 模式又不能每次都弹黑框。按有没有参数分流最干净。

单文件模式还有一个坑：每次启动都要把 15MB 运行时解压到临时目录，
所以 tour 走「进程内调用」，不再 fork 子进程重复解压。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _ensure_console():
    """冻结 + windowed 模式下 stdout/stderr 为 None，这里重新挂一个控制台。"""
    if sys.stdout is not None and not getattr(sys, "frozen", False):
        return
    if sys.stdout is not None:
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        # ATTACH_PARENT_PROCESS
        if not kernel32.AttachConsole(-1):
            kernel32.AllocConsole()
        # 重新绑定 stdout / stderr 到新控制台
        for name, fileno in (("stdout", 1), ("stderr", 2)):
            handle = kernel32.GetStdHandle(fileno)
            stream = open(f"CONOUT$", "w", encoding="utf-8", errors="replace",
                          buffering=1, closefd=False)
            setattr(sys, name, stream)
            _ = handle
    except Exception:
        pass


def _preparse_tour_env(argv):
    """tour 要在 import config 之前定好数据目录，否则 env 改了也不生效。"""
    if not argv or argv[0] != "tour":
        return
    out = None
    for i, a in enumerate(argv):
        if a == "--out" and i + 1 < len(argv):
            out = argv[i + 1]
        elif a.startswith("--out="):
            out = a.split("=", 1)[1]
    if not out:
        return
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    os.environ["RENPU_DATA_HOME"] = str(out_dir)
    os.environ["RENPU_DB_PATH"] = str(out_dir / "renpu.db")
    os.environ["RENPU_LOG_DIR"] = str(out_dir / "logs")
    os.environ["RENPU_EXPORT_DIR"] = str(out_dir / "exports")


def main():
    argv = sys.argv[1:]

    # 单文件 EXE：命令行模式必须先拿到控制台，否则中文/符号输出直接崩
    if argv:
        _ensure_console()
        if hasattr(sys.stdout, "reconfigure"):
            try:
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
                sys.stderr.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    _preparse_tour_env(argv)

    if not argv:
        from desktop import RenpuDesktop
        app = RenpuDesktop()
        app.mainloop()
        return 0

    # 命令行模式：交给 cli.py 的 argparse
    import cli
    return cli.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
