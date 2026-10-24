# -*- coding: utf-8 -*-
"""仁谱 Renpu · 独立桌面启动器

第一版定位：便携 EXE，双击即用。
- 演示模式完全隔离，首次打开就能看到价值
- 关系图谱 / 报告 / 主页一键打开
- 保留 CLI 与本地 API 入口，Agent 可继续接入
- 不自动读取真实微信；接入真实数据仍由用户主动配置
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_NAME = "仁谱 Renpu"
APP_VERSION = "0.3.0"


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def user_data_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    path = base / "renpu"
    path.mkdir(parents=True, exist_ok=True)
    return path


class RenpuDesktop(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} · 人脉的谱系")
        self.geometry("920x620")
        self.minsize(780, 520)
        self.configure(bg="#0f1115")
        self.demo_dir = user_data_dir() / "demo"
        self.status_var = tk.StringVar(value="还没有打开数据。先点「体验演示」。")
        self.api_proc = None
        self._build_style()
        self._build_ui()

    def _build_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Root.TFrame", background="#0f1115")
        style.configure("Panel.TFrame", background="#171a21")
        style.configure("Title.TLabel", background="#0f1115", foreground="#e9ebf2",
                        font=("Segoe UI", 26, "bold"))
        style.configure("Subtitle.TLabel", background="#0f1115", foreground="#8b93a7",
                        font=("Segoe UI", 11))
        style.configure("CardTitle.TLabel", background="#171a21", foreground="#e9ebf2",
                        font=("Segoe UI", 13, "bold"))
        style.configure("CardText.TLabel", background="#171a21", foreground="#8b93a7",
                        font=("Segoe UI", 10), wraplength=245, justify="left")
        style.configure("Primary.TButton", background="#ff7a6b", foreground="#180b09",
                        font=("Segoe UI", 10, "bold"), padding=(13, 8))
        style.map("Primary.TButton", background=[("active", "#ff9589")])
        style.configure("Ghost.TButton", background="#20242d", foreground="#e9ebf2",
                        font=("Segoe UI", 10), padding=(12, 7))
        style.configure("Status.TLabel", background="#171a21", foreground="#8b93a7",
                        font=("Consolas", 9))

    def _build_ui(self):
        root = ttk.Frame(self, style="Root.TFrame", padding=32)
        root.pack(fill="both", expand=True)
        ttk.Label(root, text="人脉的谱系", style="Title.TLabel").pack(anchor="w")
        ttk.Label(root, text="仁谱 Renpu  ·  本地优先，关系数据不出本机",
                  style="Subtitle.TLabel").pack(anchor="w", pady=(5, 24))

        hero = ttk.Frame(root, style="Panel.TFrame", padding=22)
        hero.pack(fill="x", pady=(0, 18))
        ttk.Label(hero, text="先看到价值，再接你的数据", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(hero, text="演示数据完全合成，不会读取你的微信。体验完成后，再决定是否接入自己的本地库。",
                  style="CardText.TLabel", wraplength=760).pack(anchor="w", pady=(6, 14))
        ttk.Button(hero, text="体验演示", style="Primary.TButton",
                   command=self.run_demo).pack(anchor="w")

        grid = ttk.Frame(root, style="Root.TFrame")
        grid.pack(fill="both", expand=True)
        cards = [
            ("关系图谱", "查看谁和谁在共同群里出现过。分享模式会自动隐藏群名和消息数。", self.open_graph),
            ("体验报告", "打开一份结构化摘要：人数、关系边、群和待跟进事项。", self.open_report),
            ("Agent 接入", "启动本机 API，让你的 Agent 通过 127.0.0.1 调用仁谱。", self.start_api),
            ("数据与设置", "打开仁谱数据目录。真实微信接入仍需你主动配置，程序不会自动读取。", self.open_data),
        ]
        for i, (title, text, cmd) in enumerate(cards):
            card = ttk.Frame(grid, style="Panel.TFrame", padding=18)
            card.grid(row=i // 2, column=i % 2, sticky="nsew", padx=(0 if i % 2 == 0 else 9, 9 if i % 2 == 0 else 0), pady=(0, 18))
            grid.columnconfigure(i % 2, weight=1)
            grid.rowconfigure(i // 2, weight=1)
            ttk.Label(card, text=title, style="CardTitle.TLabel").pack(anchor="w")
            ttk.Label(card, text=text, style="CardText.TLabel").pack(anchor="w", pady=(8, 14), fill="x", expand=True)
            ttk.Button(card, text="打开", style="Ghost.TButton", command=cmd).pack(anchor="w")

        status = ttk.Frame(root, style="Panel.TFrame", padding=10)
        status.pack(fill="x")
        ttk.Label(status, textvariable=self.status_var, style="Status.TLabel").pack(anchor="w")

    def _python(self):
        return sys.executable

    def _cli(self, *args):
        if getattr(sys, "frozen", False):
            # portable 发布包同时携带 renpu-cli.exe；GUI 进程不冒充 Python 解释器
            cli_exe = app_root() / "renpu-cli.exe"
            return [str(cli_exe), *args]
        return [self._python(), str(app_root() / "cli.py"), *args]

    def run_demo(self):
        def work():
            try:
                self.demo_dir.mkdir(parents=True, exist_ok=True)
                env = os.environ.copy()
                env.update({
                    "RENPU_DATA_HOME": str(self.demo_dir),
                    "RENPU_DB_PATH": str(self.demo_dir / "renpu.db"),
                    "RENPU_LOG_DIR": str(self.demo_dir / "logs"),
                    "RENPU_EXPORT_DIR": str(self.demo_dir / "exports"),
                    "PYTHONIOENCODING": "utf-8",
                })
                self.status_var.set("正在创建隔离演示库……")
                subprocess.run(self._cli("tour", "--out", str(self.demo_dir)),
                               cwd=str(app_root()), env=env,
                               capture_output=True, check=True)
                self.after(0, lambda: self.status_var.set(
                    f"演示已就绪：{self.demo_dir} · 可打开关系图谱或体验报告"))
                self.after(0, self.open_graph)
            except Exception as exc:
                self.after(0, lambda: messagebox.showerror("体验失败", str(exc)))
                self.after(0, lambda: self.status_var.set("体验失败，请查看错误信息。"))
        threading.Thread(target=work, daemon=True).start()

    def open_graph(self):
        path = self.demo_dir / "renpu-graph.html"
        if not path.exists():
            messagebox.showinfo("还没有图谱", "先点「体验演示」，生成隔离演示数据。")
            return
        webbrowser.open(path.resolve().as_uri())
        self.status_var.set(f"已打开关系图谱：{path}")

    def open_report(self):
        path = self.demo_dir / "renpu-report.md"
        if not path.exists():
            # 先生成报告的最小提示
            messagebox.showinfo("还没有报告", "先点「体验演示」，生成隔离演示数据。")
            return
        os.startfile(str(path))
        self.status_var.set(f"已打开体验报告：{path}")

    def start_api(self):
        if self.api_proc and self.api_proc.poll() is None:
            webbrowser.open("http://127.0.0.1:8877/api/stats")
            return
        try:
            env = os.environ.copy()
            env["RENPU_DB_PATH"] = str(self.demo_dir / "renpu.db")
            env["RENPU_DATA_HOME"] = str(self.demo_dir)
            self.api_proc = subprocess.Popen(self._cli("serve", "--port", "8877"),
                                              cwd=str(app_root()), env=env,
                                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.status_var.set("Agent API 已启动：http://127.0.0.1:8877")
            webbrowser.open("http://127.0.0.1:8877/api/stats")
        except Exception as exc:
            messagebox.showerror("API 启动失败", str(exc))

    def open_data(self):
        user_data_dir().mkdir(parents=True, exist_ok=True)
        os.startfile(str(user_data_dir()))
        self.status_var.set(f"已打开数据目录：{user_data_dir()}")

    def destroy(self):
        if self.api_proc and self.api_proc.poll() is None:
            self.api_proc.terminate()
        super().destroy()


if __name__ == "__main__":
    app = RenpuDesktop()
    app.mainloop()
