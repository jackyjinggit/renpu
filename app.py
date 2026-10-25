# -*- coding: utf-8 -*-
"""仁谱 Renpu · 统一入口（单文件 EXE）

一个 EXE，两种模式：
  Renpu.exe                 → 独立桌面界面（双击即用，无黑框）
  Renpu.exe stats           → 命令行模式（带控制台输出）
  Renpu.exe tour --out DIR  → 一键体验
  Renpu.exe --api 8877      → 只起本地 HTTP API（GUI 内部调用）
  Renpu.exe --selftest      → GUI 自检（渲染每个视图 + 截图 + 出报告）

单文件 EXE 的三个坑，本文件全部处理掉：

坑 1 · windowed 模式下 sys.stdout is None
    PyInstaller 的 --windowed 会把 stdout/stderr 置 None，直接 print 静默失败、
    直接 reconfigure 抛 AttributeError。这里自己挂一个控制台并重新绑定流。

坑 2 · 单文件每次启动都要把运行时解压到临时目录（15~25MB）
    所以「在 EXE 里再 fork 一个 python 跑 cli.py」这条路走不通 ——
    sys.executable 是 GUI 自己，不能当解释器用。所有子命令都走进程内调用。

坑 3 · Windows GBK 控制台输出 Unicode 符号会崩
    ✅ ❌ · ✓ 这些符号在 cp936 里没有，print 抛 UnicodeEncodeError。
    统一把输出流设成 errors="replace"，且控制台 CP 切到 UTF-8。
"""
from __future__ import annotations

import ctypes
import io
import json
import os
import sys
import time
import traceback
from pathlib import Path

APP_NAME = "仁谱 Renpu"


# ────────────────────────── 控制台 / 编码 ──────────────────────────

class _NullStream(io.TextIOBase):
    """没有控制台时的兜底流：吃掉输出，永不抛异常。"""

    def write(self, s):
        return len(s) if s else 0

    def flush(self):
        pass

    def isatty(self):
        return False


class _ConsolePassthrough:
    """包一层原始控制台流：**屏蔽 reconfigure** + **用 WriteConsoleW 直写 Unicode**。

    为什么需要这一层（这是本轮踩的最隐蔽的坑）：

    Python 在 Windows 控制台上默认走 `WriteConsoleW` —— 直接写 UTF-16，
    任何字符（包括 ✅ ❌ · 这些 cp936 里没有的符号）都能正确显示。

    但只要有人调了 `sys.stdout.reconfigure(encoding="utf-8")`，
    Python 就改走「写原始字节」的路子；控制台此时按**本地代码页（cp936/GBK）**
    去解释这些字节 → 中文全变乱码（“浠佽氨”这种）。

    cli.py / tour.py 在 import 时都会 reconfigure，所以在「控制台已连接」的场景下
    必须把这层 reconfigure 屏蔽掉。

    光屏蔽还不够 —— 保险起见，这里在写入前显式走一遍 WriteConsoleW：
    不依赖 Python 内部对「是不是控制台」的判断，任何代码路径下中文都不会乱码。

    只在 stdout 是**管道/文件**（非终端）时才真的切 UTF-8 —— 那时没有控制台代码页
    这回事，UTF-8 才是正确的线格式。
    """

    def __init__(self, raw):
        self._raw = raw
        self._handle = None
        self._checked = False

    def _console_handle(self):
        """拿到 stdout 的 Windows HANDLE（确认是控制台才返回）。"""
        if self._checked:
            return self._handle
        self._checked = True
        try:
            import msvcrt
            import ctypes
            fd = self._raw.fileno()
            h = msvcrt.get_osfhandle(fd)
            if h in (-1, 0):
                self._handle = None
            else:
                self._handle = h
        except Exception:
            self._handle = None
        return self._handle

    def _write_console(self, s):
        """用 WriteConsoleW 直接写 —— Unicode 通道，不受代码页影响。"""
        h = self._console_handle()
        if h is None:
            return False
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            # GetConsoleMode 失败说明它不是控制台
            mode = ctypes.c_ulong()
            if not kernel32.GetConsoleMode(h, ctypes.byref(mode)):
                return False
            data = s.encode("utf-16-le")
            written = ctypes.c_ulong()
            kernel32.WriteConsoleW(h, data, len(data) // 2, ctypes.byref(written), None)
            return True
        except Exception:
            return False

    def write(self, s):
        if not s:
            return 0
        if self._write_console(s):
            return len(s)
        return self._raw.write(s)
    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def flush(self):
        return self._raw.flush()

    def isatty(self):
        try:
            return self._raw.isatty()
        except Exception:
            return False

    def fileno(self):
        return self._raw.fileno()

    def readable(self):
        return False

    def writable(self):
        return True

    def seekable(self):
        return False

    @property
    def encoding(self):
        return getattr(self._raw, "encoding", None) or "utf-8"

    @property
    def errors(self):
        return getattr(self._raw, "errors", None) or "replace"

    def __getattr__(self, name):
        # 明确不转发 reconfigure —— 那正是要屏蔽的东西
        if name == "reconfigure":
            raise AttributeError(name)
        return getattr(self._raw, name)


def _is_tty(stream) -> bool:
    try:
        return bool(stream) and bool(stream.isatty())
    except Exception:
        return False


def _reconfigure(stream, encoding="utf-8"):
    """把输出流调成 UTF-8 + replace。

    errors="replace" 是关键：cp936 控制台遇到 ✅ 会抛 UnicodeEncodeError，
    replace 把它降级成 '?'，程序继续跑完 —— 用户看到的是一个方块，
    而不是整条命令崩掉。
    """
    try:
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding=encoding, errors="replace")
    except Exception:
        pass


def _ensure_console():
    """保证有一个能用的输出通道。

    三种情况：
      1. stdout 是控制台 → 包一层屏蔽 reconfigure（保持 WriteConsoleW，符号不乱码）
      2. stdout 是管道/文件 → 直接 reconfigure 成 UTF-8 + replace（不崩）
      3. stdout 是 None（--windowed 构建）→ 挂一个控制台出来
    """
    if sys.stdout is not None:
        if _is_tty(sys.stdout):
            sys.stdout = _ConsolePassthrough(sys.stdout)
            if sys.stderr is not None:
                sys.stderr = _ConsolePassthrough(sys.stderr)
        else:
            _reconfigure(sys.stdout)
            _reconfigure(sys.stderr)
        return True

    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        # ATTACH_PARENT_PROCESS(-1)：从 cmd / PowerShell 启动时接管父控制台
        if not kernel32.AttachConsole(-1):
            kernel32.AllocConsole()
        # 控制台代码页切 UTF-8，否则 CONOUT$ 写的 utf-8 字节会被按 GBK 解释
        try:
            kernel32.SetConsoleOutputCP(65001)
            kernel32.SetConsoleCP(65001)
        except Exception:
            pass
        out = open("CONOUT$", "w", encoding="utf-8", errors="replace",
                   buffering=1, closefd=False)
        err = open("CONOUT$", "w", encoding="utf-8", errors="replace",
                   buffering=1, closefd=False)
        sys.stdout = out
        sys.stderr = err
        _reconfigure(out)
        _reconfigure(err)
        return True
    except Exception:
        sys.stdout = _NullStream()
        sys.stderr = _NullStream()
        return False


# ────────────────────────── 环境准备 ──────────────────────────

# 全局选项里会吃掉一个值的 flag —— 判断 "tour" 是不是子命令时要用
_VALUE_FLAGS = {"--out", "--days", "--limit", "--port", "--min-weight",
                "--tier", "--focus", "--title", "--name", "--kind"}


def _preparse_tour_env(argv):
    """tour 要在 import config 之前定好数据目录，否则 env 改了也不生效。

    关键细节：tour 不带 --out 时，scripts/tour.py 会自己造一个临时目录，
    但那时 config 早就 import 完了 → 演示数据会污染用户真实库。
    所以这里**先替 tour 造好临时目录**，把 env 钉死，再把 --out 塞回 argv。
    """
    # 找 "tour" 子命令在哪（前面可能有全局 flag，如 --json tour）
    idx = -1
    for i, a in enumerate(argv):
        if a == "tour":
            prev = argv[i - 1] if i > 0 else ""
            if prev in _VALUE_FLAGS:
                continue          # 这是某个选项的值，不是子命令
            idx = i
            break
    if idx < 0:
        return argv

    out = None
    for i, a in enumerate(argv):
        if a == "--out" and i + 1 < len(argv):
            out = argv[i + 1]
        elif a.startswith("--out="):
            out = a.split("=", 1)[1]
    if not out:
        import tempfile
        out = tempfile.mkdtemp(prefix="renpu-tour-")
        argv = list(argv) + ["--out", out]
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    os.environ["RENPU_DATA_HOME"] = str(out_dir)
    os.environ["RENPU_DB_PATH"] = str(out_dir / "renpu.db")
    os.environ["RENPU_LOG_DIR"] = str(out_dir / "logs")
    os.environ["RENPU_EXPORT_DIR"] = str(out_dir / "exports")
    return argv


def _is_cli_build():
    """当前是不是 renpu-cli.exe（控制台版）构建。"""
    try:
        stem = Path(sys.argv[0]).stem.lower()
    except Exception:
        stem = ""
    return "cli" in stem


# ────────────────────────── GUI ──────────────────────────

def _run_gui():
    """启动桌面工作台（优先 desktop2，失败则退回旧版 desktop）。"""
    try:
        from desktop2 import RenpuDesktop
    except Exception:
        traceback.print_exc()
        from desktop import RenpuDesktop
    app = RenpuDesktop()
    app.mainloop()
    return 0


def _run_api(argv):
    """只起本地 HTTP API（GUI 的「Agent 接入」用它拉子进程）。

    注意：DB 路径靠继承的环境变量 RENPU_DB_PATH 传进来 ——
    子进程是全新解释器，config 在 import 时读 env，所以是对的。
    """
    port = None
    for a in argv:
        if a.startswith("--port="):
            port = int(a.split("=", 1)[1])
        elif a.isdigit():
            port = int(a)
    if port is None:
        import config
        port = int(config.DEFAULT_PORT)
    import server
    server.serve(port)
    return 0


# ────────────────────────── 自检（GUI 真能干活？） ──────────────────────────

def _png_write(path, w, h, rows):
    """把 RGB 行数据写成 PNG（纯标准库 zlib + struct，不引 PIL）。"""
    import struct
    import zlib

    raw = bytearray()
    for r in rows:
        raw.append(0)          # filter type 0
        raw += r

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 6))
    png += chunk(b"IEND", b"")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(png)


def _grab_screen(x, y, w, h, out_path, hwnd=None):
    """抓一张 PNG（ctypes + GDI，纯标准库）。

    优先用 PrintWindow 直接抓**窗口自己的**内容 —— 窗口有一部分在屏幕外时
    （分辨率不够 / 多显示器），抓屏幕区域会截到别的程序或半张脸。
    拿不到窗口句柄才退回抓屏幕区域。
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD),
                    ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG),
                    ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]

    def _dump(mem, bmp, ww, hh):
        bi = BITMAPINFOHEADER()
        bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.biWidth = ww
        bi.biHeight = -hh
        bi.biPlanes = 1
        bi.biBitCount = 32
        bi.biCompression = 0
        buf = ctypes.create_string_buffer(ww * hh * 4)
        gdi32.GetDIBits(mem, bmp, 0, hh, buf, ctypes.byref(bi), 0)
        raw = buf.raw
        rows = []
        for yy in range(hh):
            base = yy * ww * 4
            row = bytearray(ww * 3)
            o = 0
            for xx in range(ww):
                i = base + xx * 4
                row[o] = raw[i + 2]
                row[o + 1] = raw[i + 1]
                row[o + 2] = raw[i]
                o += 3
            rows.append(bytes(row))
        return rows

    # ── 路径 1：PrintWindow 抓窗口本体 ──
    try:
        if not hwnd:
            hwnd = user32.GetForegroundWindow()
        if hwnd:
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            ww, hh = rect.right - rect.left, rect.bottom - rect.top
            if ww > 10 and hh > 10:
                src = user32.GetWindowDC(hwnd)
                mem = gdi32.CreateCompatibleDC(src)
                bmp = gdi32.CreateCompatibleBitmap(src, ww, hh)
                gdi32.SelectObject(mem, bmp)
                # 2 = PW_RENDERFULLCONTENT（能抓到 DWM 合成的窗口内容）
                ok = user32.PrintWindow(hwnd, mem, 2)
                if not ok:
                    ok = user32.PrintWindow(hwnd, mem, 0)
                rows = _dump(mem, bmp, ww, hh) if ok else None
                gdi32.DeleteObject(bmp)
                gdi32.DeleteDC(mem)
                user32.ReleaseDC(hwnd, src)
                if rows:
                    _png_write(out_path, ww, hh, rows)
                    return {"file": str(out_path), "w": ww, "h": hh,
                            "method": "PrintWindow"}
    except Exception:
        pass

    # ── 路径 2：抓屏幕区域 ──
    src = user32.GetDC(0)
    mem = gdi32.CreateCompatibleDC(src)
    bmp = gdi32.CreateCompatibleBitmap(src, w, h)
    gdi32.SelectObject(mem, bmp)
    gdi32.BitBlt(mem, 0, 0, w, h, src, x, y, 0x00CC0020)   # SRCCOPY
    rows = _dump(mem, bmp, w, h)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(0, src)
    _png_write(out_path, w, h, rows)
    return {"file": str(out_path), "w": w, "h": h, "method": "BitBlt"}


def _run_selftest(argv):
    """GUI 自检：真渲染每个视图 + 截图 + 出报告。

    为什么需要：进程还活着 ≠ 界面画出来了。必须真的把 9 个视图都渲染一遍，
    确认没有异常、内容真的填上了（控件数 / 右栏字数），并截一张图肉眼验收。
    """
    out_path = None
    shot_path = None
    make_demo = False
    for i, a in enumerate(argv):
        if a == "--out" and i + 1 < len(argv):
            out_path = argv[i + 1]
        elif a.startswith("--out="):
            out_path = a.split("=", 1)[1]
        elif a == "--shot" and i + 1 < len(argv):
            shot_path = argv[i + 1]
        elif a.startswith("--shot="):
            shot_path = a.split("=", 1)[1]
        elif a == "--demo":
            make_demo = True

    from desktop2 import RenpuDesktop
    app = RenpuDesktop()
    report = {"ok": False, "title": None, "size": None, "views": {},
              "errors": [], "frozen": bool(getattr(sys, "frozen", False)),
              "executable": sys.executable, "argv": list(sys.argv)}

    # 弹窗会把 Tk 主循环卡住 —— 自检必须无人值守，所以把 messagebox 换成记录。
    try:
        from tkinter import messagebox as _mb
        _mb.showerror = lambda *a, **k: report["errors"].append(
            "messagebox.showerror: " + str(a[:1]))
        _mb.showinfo = lambda *a, **k: None
        _mb.showwarning = lambda *a, **k: None
        _mb.askyesno = lambda *a, **k: False
    except Exception:
        pass

    def wait_for(pred, timeout=180):
        end = time.time() + timeout
        while time.time() < end:
            app.update()
            if pred():
                return True
            time.sleep(0.25)
        return False

    def watchdog():
        """兜底：超过 240 秒还没跑完就强制写报告并退出。

        自检必须有终点 —— 任何一步卡死（弹窗、死锁）都不能让进程永远挂着，
        否则自动化验收会被无限拖住。
        """
        if report.get("ok") is not None and report.get("done"):
            return
        if time.time() < t0 + 240:
            app.after(2000, watchdog)
            return
        report["errors"].append("watchdog: 自检超时（240 秒）")
        report["ok"] = False
        try:
            if out_path:
                Path(out_path).parent.mkdir(parents=True, exist_ok=True)
                Path(out_path).write_text(
                    json.dumps(report, ensure_ascii=False, indent=2),
                    encoding="utf-8")
        except Exception:
            pass
        try:
            app.destroy()
        except Exception:
            pass

    t0 = time.time()

    def step():
        try:
            app.deiconify()
            app.attributes("-topmost", True)
            app.lift()
            app.focus_force()
            app.update()
            report["title"] = app.title()
            report["size"] = [app.winfo_width(), app.winfo_height()]
            report["nav"] = list(app.nav_buttons.keys())
            time.sleep(0.8)
            app.update()

            # 演示数据：走真实路径（GUI 的「生成演示数据」按钮 → 进程内 tour）
            db = app.active_db
            report["db"] = str(db)
            report["db_exists_before"] = db.exists()
            if make_demo and not db.exists():
                app.make_demo()

                def db_ready():
                    """库真的建好了没？

                    不能只看 db.exists() —— sqlite3.connect 会**立刻**建出空文件，
                    那时候 schema 还没写进去，查 person/event 会报
                    "no such table: event"。必须等到表结构真的在。
                    """
                    if not db.exists():
                        return False
                    try:
                        import sqlite3
                        c = sqlite3.connect(str(db))
                        n = c.execute("SELECT COUNT(*) FROM person").fetchone()[0]
                        c.close()
                        return n > 0
                    except Exception:
                        return False

                ok = wait_for(db_ready, timeout=240)
                report["demo_generated"] = ok
                report["db_exists_after"] = db.exists()
                app.update()
            app.mode = "demo"
            app.active_db = db

            for v in ["overview", "people", "graph", "graph3d", "todos",
                      "events", "search", "api", "settings"]:
                try:
                    app.navigate(v)
                    app.update_idletasks()
                    app.update()
                    time.sleep(0.3)
                    app.update()
                    kids = len(app.views[v].winfo_children())
                    rlen = len(app.right_body.get("1.0", "end-1c").strip())
                    report["views"][v] = {"widgets": kids, "right_chars": rlen}
                except Exception as exc:
                    report["errors"].append(f"view {v}: {exc!r}")

            # 交互实测：搜索
            try:
                app.navigate("search")
                app.update()
                app.search_var.set("黑客松")
                app._do_search()
                app.update()
                n = len(app.search_result.get_children()) if hasattr(app, "search_result") else -1
                report["search_hits"] = n
            except Exception as exc:
                report["errors"].append("search: " + repr(exc))

            # 交互实测：联系人选中 → 右栏
            try:
                app.navigate("people")
                app.update()
                tree = app.people_tree
                items = tree.get_children()
                report["people_rows"] = len(items)
                if items:
                    tree.selection_set(items[0])
                    app._show_person_detail(tree)
                    app.update()
                    report["person_detail_chars"] = len(
                        app.right_body.get("1.0", "end-1c").strip())
            except Exception as exc:
                report["errors"].append("people: " + repr(exc))

            # 交互实测：GUI 里生成关系图谱（走进程内 CLI）。
            # 关键验证点：它读的是**当前生效的演示库**，不是用户真实库 ——
            # 如果路径重定向坏了，这里出的图会是空的或报错。
            try:
                app.navigate("graph")
                app.update()
                outp = db.parent / "exports" / "selftest-graph.html"
                ok_g, detail_g = app._run_cli(
                    ["graph", "--limit", "30", "--share", "--out", str(outp)])
                report["graph_cli_ok"] = ok_g
                report["graph_cli_file"] = str(outp)
                report["graph_cli_exists"] = outp.exists()
                report["graph_cli_size"] = outp.stat().st_size if outp.exists() else 0
                if not ok_g:
                    report["errors"].append("graph CLI 失败: " + str(detail_g)[:300])
            except Exception as exc:
                report["errors"].append("graph: " + repr(exc))

            # 截图：回总览（最能看出界面成不成立）
            try:
                app.navigate("overview")
                app.update()
                time.sleep(0.4)
                app.update()
                # 窗口可能有一部分在屏幕外（分辨率不够）——
                # 先挪到屏幕内，截图才完整。
                try:
                    sw = app.winfo_screenwidth()
                    sh = app.winfo_screenheight()
                    w0, h0 = app.winfo_width(), app.winfo_height()
                    if w0 > sw - 20 or h0 > sh - 80:
                        app.geometry(f"{min(w0, sw - 40)}x{min(h0, sh - 120)}+0+0")
                    else:
                        app.geometry(f"+0+0")
                    app.update()
                    time.sleep(0.4)
                    app.update()
                except Exception:
                    pass
                x = app.winfo_rootx()
                y = app.winfo_rooty()
                w = app.winfo_width()
                h = app.winfo_height()
                report["shot_rect"] = [x, y, w, h]
                report["screen"] = [app.winfo_screenwidth(), app.winfo_screenheight()]
                if shot_path and w > 10 and h > 10:
                    # 用窗口自己的 HWND 抓（PrintWindow），
                    # 这样窗口在屏幕外也抓得到完整画面。
                    hwnd = None
                    try:
                        hwnd = ctypes.windll.user32.GetParent(app.winfo_id()) or \
                            app.winfo_id()
                    except Exception:
                        pass
                    report["shot"] = _grab_screen(x, y, w, h, shot_path, hwnd=hwnd)
            except Exception as exc:
                report["errors"].append("shot: " + repr(exc))

            report["ok"] = not report["errors"]
        except Exception as exc:
            report["errors"].append("fatal: " + repr(exc))
            report["traceback"] = traceback.format_exc()
        finally:
            report["done"] = True
            report["elapsed"] = round(time.time() - t0, 1)
            try:
                if out_path:
                    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
                    Path(out_path).write_text(
                        json.dumps(report, ensure_ascii=False, indent=2),
                        encoding="utf-8")
            except Exception:
                pass
            try:
                app.destroy()
            except Exception:
                pass

    app.after(1000, step)
    app.after(1500, watchdog)
    app.mainloop()
    return 0 if report.get("ok") else 1


# ────────────────────────── 主入口 ──────────────────────────

def main():
    argv = list(sys.argv[1:])

    # ── 内部子命令：起 API 服务（GUI 的 Agent 接入用它拉子进程） ──
    if argv and argv[0] == "--api":
        return _run_api(argv[1:])

    # ── 内部子命令：GUI 自检 ──
    if argv and argv[0] == "--selftest":
        return _run_selftest(argv[1:])

    # ── 命令行模式：必须先拿到控制台，否则中文/符号输出直接崩 ──
    if argv:
        _ensure_console()
    elif _is_cli_build():
        # renpu-cli.exe 双击：没参数就打印帮助（而不是 argparse 报错）
        _ensure_console()
        import cli
        cli.build_parser().print_help()
        return 0

    # tour 必须在 import config 之前把数据目录钉死
    argv = _preparse_tour_env(argv)

    if not argv:
        return _run_gui()

    # 命令行模式：交给 cli.py 的 argparse
    import cli
    return cli.main(argv) or 0


if __name__ == "__main__":
    raise SystemExit(main())
