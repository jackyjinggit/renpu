# -*- coding: utf-8 -*-
"""仁谱 Renpu · 桌面工作台（第二版）

从「欢迎页 + 5 个按钮」升级成真正的工作台：

    左栏导航          │  中间内容区              │  右栏详情
    ─────────────    │  ──────────────────      │  ──────────
    总览              │  按导航切换的视图          │  选中的人 / 当前的图
    联系人            │  搜索框 + 结果列表        │  关系人 / 标签 / 群
    关系图谱          │  时间线 / 待办 / 事件      │  快捷动作
    3D 关系圈         │
    待办与承诺         │
    事件              │
    全文搜索          │
    Agent 接入         │
    数据与设置         │

设计原则：
  · 数据主权：默认只连**演示库**；接真实微信必须用户显式配置路径，程序不自动读。
  · 零第三方依赖：只用 tkinter + 标准库。
  · 长任务（摄取 / 出图）放后台线程，界面不卡死；进度只更新状态栏，不弹窗打断。
  · 所有生成物落在用户数据目录，不污染代码区。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_NAME = "仁谱 Renpu"
APP_VERSION = "1.0.0"

# ── 配色（与 render.py / render3d.py 一致） ──
BG = "#0f1115"
PANEL = "#171a21"
PANEL2 = "#12151b"
TEXT = "#e9ebf2"
DIM = "#8b93a7"
LINE = "#232732"
ACCENT = "#ff7a6b"
ACCENT2 = "#4ecdc4"
WARN = "#ffd166"


def app_root() -> Path:
    """应用代码根目录。

    单文件 EXE：运行时被解压到临时目录，资源（schema / adapters / scripts）在那儿；
    数据目录另算（用户级 %LOCALAPPDATA%\\renpu）。
    """
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent


def user_data_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    path = base / "renpu"
    path.mkdir(parents=True, exist_ok=True)
    return path


def user_config_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    return base / "renpu" / "config.json"


def load_user_config() -> dict:
    p = user_config_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[renpu] 配置读取失败（按空配置处理）：{exc}")
        return {}


def retarget_paths(env: dict) -> dict:
    """把 config / core 模块里**已绑定**的路径常量改到目标目录。

    为什么必须做：config.py 在 import 时就把 DB_PATH / SCHEMA / LOG_DIR /
    EXPORT_DIR 求值了（`from config import DB_PATH` 之后 core.DB_PATH 就是
    一个死值）。单文件 EXE 的 GUI 里，所有 CLI 命令都是**进程内调用**，
    没有新进程 → 不会重新 import config → 环境变量改了也没用。

    结果就是：GUI 里点「生成演示数据」，演示数据会写进用户的真实库。
    这个函数把这件事堵死。

    必须在**任何** core.connect() 之前调用。
    """
    import config
    import core

    data_home = Path(env.get("RENPU_DATA_HOME") or config.DATA_HOME)
    db_path = Path(env.get("RENPU_DB_PATH") or (data_home / "renpu.db"))
    log_dir = Path(env.get("RENPU_LOG_DIR") or (data_home / "logs"))
    export_dir = Path(env.get("RENPU_EXPORT_DIR") or (data_home / "exports"))

    for p in (data_home, log_dir, export_dir):
        p.mkdir(parents=True, exist_ok=True)

    config.DATA_HOME = data_home
    config.DB_PATH = db_path
    config.LOG_DIR = log_dir
    config.EXPORT_DIR = export_dir
    # core 用的是 `from config import DB_PATH` —— 导入时拷的副本，必须单独改
    core.DB_PATH = db_path
    core.SCHEMA = config.SCHEMA
    return {"data_home": data_home, "db_path": db_path,
            "log_dir": log_dir, "export_dir": export_dir}


def save_user_config(cfg: dict) -> None:
    p = user_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


class RenpuDesktop(tk.Tk):
    """仁谱桌面工作台。"""

    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} · 人脉的谱系")
        self.geometry("1280x800")
        self.minsize(1024, 640)
        self.configure(bg=BG)

        # 运行状态
        self.cfg = load_user_config()
        self.demo_dir = user_data_dir() / "demo"
        self.real_dir = user_data_dir()
        self.active_db = self.demo_dir / "renpu.db"     # 当前生效的库
        self.mode = "demo"                              # demo | real
        self.api_proc = None
        self.busy = False

        self.status_var = tk.StringVar(value="就绪。先在左栏选一个视图。")
        self.search_var = tk.StringVar()
        self.header_var = tk.StringVar(value="总览")
        self.subheader_var = tk.StringVar(value="")

        self._build_style()
        self._build_ui()
        self.after(120, self._refresh_overview)

    # ───────────────────────── 样式 ─────────────────────────
    def _build_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Root.TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("Panel2.TFrame", background=PANEL2)
        style.configure("Side.TFrame", background=PANEL2)
        style.configure("H1.TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 20, "bold"))
        style.configure("H2.TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 14, "bold"))
        style.configure("Sub.TLabel", background=BG, foreground=DIM, font=("Segoe UI", 10))
        # clam 主题默认给按钮画白边框，贴到深色底上很廉价 —— 全部去掉
        style.configure("Nav.TButton", background=PANEL2, foreground=DIM,
                        font=("Segoe UI", 11), padding=(14, 10), anchor="w",
                        borderwidth=0, relief="flat", focuscolor=PANEL2)
        style.map("Nav.TButton",
                  background=[("active", PANEL), ("hover", PANEL)],
                  foreground=[("active", ACCENT), ("hover", TEXT)],
                  bordercolor=PANEL2, lightcolor=PANEL2, darkcolor=PANEL2,
                  relief=[("active", "flat"), ("hover", "flat")])
        style.configure("Card.TLabel", background=PANEL, foreground=TEXT,
                        font=("Segoe UI", 11, "bold"))
        style.configure("CardV.TLabel", background=PANEL, foreground=ACCENT,
                        font=("Segoe UI", 22, "bold"))
        style.configure("CardS.TLabel", background=PANEL, foreground=DIM, font=("Segoe UI", 9))
        style.configure("Row.TLabel", background=PANEL, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("RowDim.TLabel", background=PANEL, foreground=DIM, font=("Segoe UI", 9))
        style.configure("Primary.TButton", background=ACCENT, foreground="#180b09",
                        font=("Segoe UI", 10, "bold"), padding=(13, 8),
                        borderwidth=0, relief="flat", focuscolor=ACCENT)
        style.map("Primary.TButton", background=[("active", "#ff9589")],
                  bordercolor=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT,
                  relief=[("active", "flat")])
        style.configure("Ghost.TButton", background="#20242d", foreground=TEXT,
                        font=("Segoe UI", 10), padding=(11, 7),
                        borderwidth=0, relief="flat", focuscolor="#20242d")
        style.map("Ghost.TButton", background=[("active", "#2a303c")],
                  bordercolor="#20242d", lightcolor="#20242d", darkcolor="#20242d",
                  relief=[("active", "flat")])
        style.configure("Status.TLabel", background=PANEL, foreground=DIM,
                        font=("Consolas", 9))
        style.configure("TSearch.TEntry", fieldbackground=PANEL, foreground=TEXT,
                        bordercolor=LINE, lightcolor=LINE, darkcolor=LINE,
                        insertcolor=TEXT, font=("Segoe UI", 11), padding=8)
        style.configure("TNotebook", background=BG, bordercolor=LINE)
        style.configure("TNotebook.Tab", background=PANEL2, foreground=DIM, padding=(14, 8))
        style.map("TNotebook.Tab",
                  background=[("selected", PANEL)], foreground=[("selected", TEXT)])
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                        foreground=TEXT, rowheight=26, borderwidth=0, font=("Segoe UI", 10))
        style.configure("Treeview.Heading", background=PANEL2, foreground=DIM,
                        font=("Segoe UI", 9, "bold"), relief="flat")
        style.map("Treeview", background=[("selected", "#2a303c")],
                  foreground=[("selected", TEXT)])

    # ───────────────────────── 布局 ─────────────────────────
    def _build_ui(self):
        # 顶栏
        top = ttk.Frame(self, style="Root.TFrame", padding=(18, 14))
        top.pack(fill="x")
        ttk.Label(top, textvariable=self.header_var, style="H1.TLabel").pack(side="left")
        ttk.Label(top, textvariable=self.subheader_var, style="Sub.TLabel").pack(side="left", padx=(14, 0))
        self.mode_label = ttk.Label(top, text="演示库", style="Sub.TLabel")
        self.mode_label.pack(side="right")

        body = ttk.Frame(self, style="Root.TFrame")
        body.pack(fill="both", expand=True)

        # 左栏导航
        side = ttk.Frame(body, style="Side.TFrame", width=188, padding=(0, 12, 0, 12))
        side.pack(side="left", fill="y")
        side.pack_propagate(False)

        self.nav_buttons = {}
        navs = [
            ("overview", "总览"),
            ("people", "联系人"),
            ("graph", "关系图谱"),
            ("graph3d", "3D 关系圈"),
            ("todos", "待办与承诺"),
            ("events", "事件"),
            ("search", "全文搜索"),
            ("api", "Agent 接入"),
            ("settings", "数据与设置"),
        ]
        for key, label in navs:
            b = ttk.Button(side, text=label, style="Nav.TButton", command=lambda k=key: self.navigate(k))
            b.pack(fill="x", pady=1)
            self.nav_buttons[key] = b
        ttk.Separator(side).pack(fill="x", pady=10)
        ttk.Button(side, text="重新载入数据", style="Ghost.TButton",
                   command=self.reload).pack(fill="x")

        # 中间内容
        mid = ttk.Frame(body, style="Root.TFrame", padding=(18, 0, 10, 10))
        mid.pack(side="left", fill="both", expand=True)

        # 搜索框
        # 搜索条只在「全文搜索」视图里出现 —— 常驻一整行空输入框很丑
        self.sbar = ttk.Frame(mid, style="Root.TFrame")
        self.search_entry = ttk.Entry(self.sbar, textvariable=self.search_var, style="TSearch.TEntry")
        self.search_entry.pack(side="left", fill="x", expand=True)
        self.search_entry.bind("<Return>", lambda e: self._do_search())
        ttk.Button(self.sbar, text="搜索", style="Ghost.TButton",
                   command=self._do_search).pack(side="left", padx=(8, 0))

        # 内容容器（每个视图一个 frame，切换时只显示一个）
        self.views = {}
        for key, _ in navs:
            f = ttk.Frame(mid, style="Root.TFrame")
            self.views[key] = f

        # 右栏详情
        self.right = ttk.Frame(body, style="Side.TFrame", width=310, padding=(14, 12, 14, 12))
        self.right.pack(side="right", fill="y")
        self.right.pack_propagate(False)
        self.right_title = ttk.Label(self.right, text="详情", style="H2.TLabel")
        self.right_title.pack(anchor="w")
        self.right_body = tk.Text(self.right, bg=PANEL, fg=TEXT, relief="flat",
                                  font=("Segoe UI", 10), wrap="word", padx=12, pady=12,
                                  insertbackground=TEXT, selectbackground="#2a303c")
        self.right_body.pack(fill="both", expand=True, pady=(10, 0))
        self.right_body.tag_configure("h", foreground=ACCENT, font=("Segoe UI", 10, "bold"))
        self.right_body.tag_configure("d", foreground=DIM, font=("Segoe UI", 9))
        self.right_body.tag_configure("n", foreground=TEXT, font=("Segoe UI", 10))
        self.right_body.tag_configure("ok", foreground=ACCENT2, font=("Segoe UI", 9, "bold"))
        self.right_body.configure(state="disabled")

        # 状态栏
        status = ttk.Frame(self, style="Panel.TFrame", padding=(14, 7))
        status.pack(fill="x")
        ttk.Label(status, textvariable=self.status_var, style="Status.TLabel").pack(side="left")
        ttk.Label(status, text=f"{APP_NAME} v{APP_VERSION}", style="Status.TLabel").pack(side="right")

        self.navigate("overview")

    # ───────────────────────── 导航 ─────────────────────────
    def navigate(self, key):
        for k, b in self.nav_buttons.items():
            try:
                b.state(["!active"])
            except tk.TclError:
                pass
        try:
            self.nav_buttons[key].state(["active"])
        except (KeyError, tk.TclError):
            pass
        for k, f in self.views.items():
            if k == key:
                f.pack(fill="both", expand=True)
            else:
                f.pack_forget()
        # 搜索条只在全文搜索视图出现
        if key == "search":
            self.sbar.pack(fill="x", pady=(0, 12))
        else:
            self.sbar.pack_forget()
        titles = {
            "overview": ("总览", "你这个人的关系盘子有多大"),
            "people": ("联系人", "按最近活跃排序"),
            "graph": ("关系图谱", "谁跟谁是一伙的 · 可拖时间轴"),
            "graph3d": ("3D 关系圈", "聚焦某个人，看他的关系结构"),
            "todos": ("待办与承诺", "答应过的事"),
            "events": ("事件", "发生过什么"),
            "search": ("全文搜索", "翻你自己聊过的话"),
            "api": ("Agent 接入", "让 AI 通过本机接口调用"),
            "settings": ("数据与设置", "数据在哪、接什么源",
                         ),
        }
        t = titles.get(key, ("", ""))
        self.header_var.set(t[0])
        self.subheader_var.set(t[1] if len(t) > 1 else "")
        fn = {
            "overview": self._refresh_overview,
            "people": self._refresh_people,
            "graph": self._refresh_graph,
            "graph3d": self._refresh_graph3d,
            "todos": self._refresh_todos,
            "events": self._refresh_events,
            "search": self._refresh_search,
            "api": self._refresh_api,
            "settings": self._refresh_settings,
        }.get(key)
        if fn:
            fn()

    # ───────────────────────── 数据访问 ─────────────────────────
    def _con(self):
        """打开当前生效的库连接。"""
        import core
        db = self.active_db
        if not db.exists():
            return None
        os.environ["RENPU_DB_PATH"] = str(db)
        return core.connect(db)

    def _set_right(self, title, blocks):
        """往右栏写内容。blocks = [(kind, text), ...]，kind ∈ h/d/n/ok"""
        self.right_title.configure(text=title)
        self.right_body.configure(state="normal")
        self.right_body.delete("1.0", "end")
        for kind, text in blocks:
            self.right_body.insert("end", text + "\n", kind)
            if kind == "h":
                self.right_body.insert("end", "\n")
        self.right_body.configure(state="disabled")

    # ───────────────────────── 视图：总览 ─────────────────────────
    def _refresh_overview(self):
        v = self.views["overview"]
        for c in v.winfo_children():
            c.destroy()
        con = self._con()
        if not con:
            box = ttk.Frame(v, style="Panel.TFrame", padding=18)
            box.pack(fill="x", pady=(0, 14))
            ttk.Label(box, text="还没有数据", style="Card.TLabel").pack(anchor="w")
            ttk.Label(box, text="演示库是合成数据，不碰你的微信，随时可以生成。",
                      style="CardS.TLabel").pack(anchor="w", pady=(6, 12))
            ttk.Button(box, text="生成演示数据", style="Primary.TButton",
                       command=self.make_demo).pack(anchor="w")
            ttk.Button(box, text="去接入我的微信", style="Ghost.TButton",
                       command=lambda: self.navigate("settings")).pack(anchor="w", pady=(10, 0))
            self._set_right("下一步", [
                ("h", "两条路"),
                ("d", "① 生成演示数据：合成关系，立刻看到产品长什么样"),
                ("d", "② 接入我的微信：填 db_storage 目录，只读不写"),
            ])
            return
        import core
        st = core.stats(con)
        con.close()

        # 注意：core.stats 的键是 messages_indexed / todos_open / edges …
        #      取错键名会静默显示 0（看着像没数据，其实是接口对不上）
        cards = [("人", st.get("people", 0)), ("关系边", st.get("edges", 0)),
                 ("群", st.get("rooms", 0)), ("消息", st.get("messages_indexed", 0)),
                 ("待办", st.get("todos_open", 0))]
        row = ttk.Frame(v, style="Root.TFrame")
        row.pack(fill="x", pady=(0, 16))
        for i, (label, val) in enumerate(cards):
            card = ttk.Frame(row, style="Panel.TFrame", padding=16)
            card.pack(side="left", fill="x", expand=True, padx=(0 if i == len(cards) - 1 else 10, 0))
            ttk.Label(card, text=str(val), style="CardV.TLabel").pack(anchor="w")
            ttk.Label(card, text=label, style="CardS.TLabel").pack(anchor="w")

        # 总览不该是空白页 —— 加两块实际内容
        con2 = self._con()
        if con2:
            recent = [dict(r) for r in con2.execute(
                "SELECT display_name, msg_total, last_active FROM person "
                "WHERE msg_total > 0 ORDER BY msg_total DESC LIMIT 8").fetchall()]
            todos = [dict(r) for r in con2.execute(
                "SELECT t.title, p.display_name FROM todo t "
                "LEFT JOIN person p ON p.person_key=t.person_key "
                "WHERE t.status='open' ORDER BY t.due LIMIT 6").fetchall()]
            con2.close()
            if recent:
                box = ttk.Frame(v, style="Panel.TFrame", padding=16)
                box.pack(fill="x", pady=(0, 14))
                ttk.Label(box, text="聊得最多的人", style="Card.TLabel").pack(anchor="w")
                for r in recent:
                    line = ttk.Frame(box, style="Panel.TFrame")
                    line.pack(fill="x", pady=(6, 0))
                    ttk.Label(line, text=r["display_name"] or "(无名)",
                              style="Row.TLabel").pack(side="left")
                    ttk.Label(line, text=f"{r['msg_total']} 条",
                              style="RowDim.TLabel").pack(side="right")
            if todos:
                box2 = ttk.Frame(v, style="Panel.TFrame", padding=16)
                box2.pack(fill="x", pady=(0, 14))
                ttk.Label(box2, text="待跟进", style="Card.TLabel").pack(anchor="w")
                for t in todos:
                    line = ttk.Frame(box2, style="Panel.TFrame")
                    line.pack(fill="x", pady=(6, 0))
                    ttk.Label(line, text=t["title"] or "", style="Row.TLabel").pack(side="left")
                    if t["display_name"]:
                        ttk.Label(line, text=t["display_name"],
                                  style="RowDim.TLabel").pack(side="right")

        self._set_right("这个盘子有多大", [
            ("h", "一句话"),
            ("n", "你有 %d 个人、%d 条关系边、%d 个群、%d 条消息。" % (
                st.get("people", 0), st.get("edges", 0), st.get("rooms", 0),
                st.get("messages_indexed", 0))),
            ("", ""),
            ("h", "关系边怎么来的"),
            ("d", "两个人同时出现在 N 个群里 → 一条权重 N 的边。不需要手动录入。"),
            ("", ""),
            ("h", "下一步"),
            ("d", "· 「关系图谱」看全景，拖时间轴看圈子怎么长\n"
                  "· 「3D 关系圈」聚焦一个人\n"
                  "· 「联系人」里点一个人看完整档案"),
        ])

    # ───────────────────────── 视图：联系人 ─────────────────────────
    def _refresh_people(self):
        v = self.views["people"]
        for c in v.winfo_children():
            c.destroy()
        con = self._con()
        if not con:
            ttk.Label(v, text="还没有数据。先生成演示数据，或在「数据与设置」接入微信源。",
                      style="Sub.TLabel").pack(anchor="w", pady=20)
            ttk.Button(v, text="生成演示数据", style="Primary.TButton",
                       command=self.make_demo).pack(anchor="w")
            return
        rows = [dict(r) for r in con.execute(
            "SELECT person_key, display_name, tier, circle, org, msg_total, last_active "
            "FROM person ORDER BY msg_total DESC LIMIT 300").fetchall()]
        con.close()

        cols = ("name", "tier", "circle", "org", "msgs")
        tree = ttk.Treeview(v, columns=cols, show="headings")
        for c, t, w in (("name", "名字", 220), ("tier", "层", 60),
                       ("circle", "圈层", 90), ("org", "单位", 140), ("msgs", "消息", 70)):
            tree.heading(c, text=t)
            tree.column(c, width=w, anchor="w")
        tree.pack(fill="both", expand=True)
        for r in rows:
            tree.insert("", "end", iid=r["person_key"], values=(
                r["display_name"] or "(无名)", r["tier"] or "", r["circle"] or "",
                r["org"] or "", r["msg_total"] or 0))
        tree.bind("<Double-1>", lambda e: self._open_profile(tree, e))
        tree.bind("<<TreeviewSelect>>", lambda e: self._show_person_detail(tree))
        self.people_tree = tree
        self.status_var.set(f"{len(rows)} 人（按消息数排序）· 双击看完整档案")

    def _show_person_detail(self, tree):
        sel = tree.selection()
        if not sel:
            return
        pk = sel[0]
        con = self._con()
        if not con:
            return
        p = con.execute(
            "SELECT display_name, tier, circle, org, msg_total, first_seen, last_active "
            "FROM person WHERE person_key=?", (pk,)).fetchone()
        rooms = [r[0] for r in con.execute(
            "SELECT room.name FROM room_member rm JOIN room ON room.room_key=rm.room_key "
            "WHERE rm.person_key=? LIMIT 12", (pk,)).fetchall()]
        edges = [dict(r) for r in con.execute(
            "SELECT d.display_name AS name, e.weight AS weight FROM edge e "
            "JOIN person d ON d.person_key=e.dst WHERE e.src=? ORDER BY e.weight DESC LIMIT 12",
            (pk,)).fetchall()]
        tags = con.execute(
            "SELECT t.name FROM person_tag pt JOIN tag t ON t.tag_id=pt.tag_id WHERE pt.person_key=?",
            (pk,)).fetchall()
        con.close()
        if not p:
            return
        name = p["display_name"] or "(无名)"
        blocks = [("h", "基本信息")]
        if p["tier"]:
            blocks.append(("n", "层：" + p["tier"]))
        if p["circle"]:
            blocks.append(("n", "圈层：" + p["circle"]))
        if p["org"]:
            blocks.append(("n", "单位：" + p["org"]))
        blocks.append(("n", f"消息：{p['msg_total'] or 0} 条"))
        if p["first_seen"]:
            blocks.append(("d", "首次出现：" + time.strftime("%Y-%m-%d", time.localtime(p["first_seen"]))))
        if p["last_active"]:
            blocks.append(("d", "最后活跃：" + time.strftime("%Y-%m-%d", time.localtime(p["last_active"]))))
        if tags:
            blocks.append(("h", "标签"))
            blocks.append(("n", "、".join(t[0] for t in tags)))
        if edges:
            blocks.append(("h", f"关系人（{len(edges)}+）"))
            for e in edges:
                blocks.append(("n", f"· {e['name']}   权重 {e['weight']:.0f}"))
        if rooms:
            blocks.append(("h", "所在群"))
            for r in rooms[:8]:
                blocks.append(("d", "· " + (r or "(群)")))
        blocks.append(("h", "操作"))
        blocks.append(("ok", "双击这一行 → 打开完整档案页"))
        self._set_right(name, blocks)

    def _open_profile(self, tree, event=None):
        sel = tree.selection()
        if not sel:
            return
        pk = sel[0]
        con = self._con()
        if not con:
            return
        nm = con.execute("SELECT display_name FROM person WHERE person_key=?", (pk,)).fetchone()
        con.close()
        who = nm[0] if nm else pk

        def work():
            try:
                import render_profile
                out = user_data_dir() / "exports" / f"profile-{pk.replace(':', '_').replace('/', '_')}.html"
                out.parent.mkdir(parents=True, exist_ok=True)
                path, _ = render_profile.render_profile(con, who, out, share=False)
                con.close()
                self.after(0, lambda: webbrowser.open(Path(path).resolve().as_uri()))
                self.after(0, lambda: self.status_var.set(f"已生成人物档案页：{path}"))
            except Exception as exc:
                try:
                    con.close()
                except Exception:
                    pass
                self.after(0, lambda: messagebox.showerror("生成档案页失败", str(exc)))
        threading.Thread(target=work, daemon=True).start()

    # ───────────────────────── 视图：关系图谱 ─────────────────────────
    def _refresh_graph(self):
        v = self.views["graph"]
        for c in v.winfo_children():
            c.destroy()
        ttk.Label(v, text="生成一个单文件 HTML 关系图谱：同色=同一圈子，"
                          "边上标共同群数，底部可拖时间轴看圈子随时间扩张。",
                  style="Sub.TLabel").pack(anchor="w", pady=(0, 14))
        row = ttk.Frame(v, style="Root.TFrame")
        row.pack(fill="x")
        self.graph_limit = tk.IntVar(value=60)
        ttk.Label(row, text="人数", style="Sub.TLabel").pack(side="left")
        tk.Spinbox(row, from_=20, to=300, increment=10, textvariable=self.graph_limit,
                   width=6, bg=PANEL, fg=TEXT, buttonbackground=PANEL2,
                   insertbackground=TEXT, relief="flat", highlightthickness=0
                   ).pack(side="left", padx=(6, 16))
        self.graph_share = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="分享模式（群名脱敏）", variable=self.graph_share).pack(side="left")
        ttk.Button(row, text="生成并打开", style="Primary.TButton",
                   command=self._make_graph).pack(side="left", padx=(18, 0))

        ex = ttk.Frame(v, style="Panel.TFrame", padding=16)
        ex.pack(fill="x", pady=(16, 0))
        ttk.Label(ex, text="导出目录", style="Card.TLabel").pack(anchor="w")
        ttk.Label(ex, text=str(user_data_dir() / "exports"), style="CardS.TLabel").pack(anchor="w")
        ttk.Button(ex, text="打开导出目录", style="Ghost.TButton",
                   command=lambda: os.startfile(str(user_data_dir() / "exports"))).pack(anchor="w", pady=(10, 0))

    def _make_graph(self):
        out = user_data_dir() / "exports" / "renpu-graph.html"
        args = ["graph", "--limit", str(self.graph_limit.get()), "--out", str(out)]
        if self.graph_share.get():
            args.append("--share")
        self._run_bg(args, f"已生成关系图谱：{out}", open_after=True)

    # ───────────────────────── 视图：3D 关系圈 ─────────────────────────
    def _refresh_graph3d(self):
        v = self.views["graph3d"]
        for c in v.winfo_children():
            c.destroy()
        ttk.Label(v, text="聚焦一个人：把他的关系圈摆成球面（越靠中心=关系越强），"
                          "用 3D 看这个人的结构。",
                  style="Sub.TLabel").pack(anchor="w", pady=(0, 14))
        row = ttk.Frame(v, style="Root.TFrame")
        row.pack(fill="x")
        self.g3d_person = tk.StringVar(value="")
        ttk.Label(row, text="中心人物", style="Sub.TLabel").pack(side="left")
        ent = ttk.Entry(row, textvariable=self.g3d_person, style="TSearch.TEntry", width=26)
        ent.pack(side="left", padx=(6, 12))
        self.g3d_share = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="分享模式", variable=self.g3d_share).pack(side="left")
        ttk.Button(row, text="生成并打开", style="Primary.TButton",
                   command=self._make_graph3d).pack(side="left", padx=(18, 0))

        ttk.Label(v, text="留空 = 自动选关系最多的那个人（通常是你的核心枢纽）。",
                  style="Sub.TLabel").pack(anchor="w", pady=(10, 0))

        self._set_right("3D 关系圈", [
            ("h", "什么时候值得用 3D"),
            ("d", "2D 回答「谁跟谁是一伙的」。\n"
                  "3D 回答「这个人的关系结构长什么样」。"),
            ("", ""),
            ("h", "约束"),
            ("d", "只做聚焦，不做全景 —— 几十个节点才看得清。\n"
                  "底层是 SVG 手写透视投影，不引 3D 库，离线可用。"),
        ])

    def _make_graph3d(self):
        out = user_data_dir() / "exports" / "renpu-graph3d.html"
        args = ["graph3d", "--out", str(out)]
        who = self.g3d_person.get().strip()
        if who:
            args += ["--person", who]
        if self.g3d_share.get():
            args.append("--share")
        self._run_bg(args, f"已生成 3D 关系圈：{out}", open_after=True)

    # ───────────────────────── 视图：待办 ─────────────────────────
    def _refresh_todos(self):
        v = self.views["todos"]
        for c in v.winfo_children():
            c.destroy()
        con = self._con()
        if not con:
            ttk.Label(v, text="还没有数据。", style="Sub.TLabel").pack(anchor="w", pady=20)
            return
        rows = [dict(r) for r in con.execute(
            "SELECT t.todo_id, t.title, t.kind, t.due, p.display_name "
            "FROM todo t LEFT JOIN person p ON p.person_key=t.person_key "
            "WHERE t.status='open' ORDER BY t.due").fetchall()]
        con.close()
        if not rows:
            ttk.Label(v, text="没有待办。", style="Sub.TLabel").pack(anchor="w", pady=20)
            return
        tree = ttk.Treeview(v, columns=("who", "kind", "due", "title"), show="headings")
        for c, t, w in (("who", "人", 130), ("kind", "类型", 80),
                       ("due", "截止", 110), ("title", "内容", 420)):
            tree.heading(c, text=t)
            tree.column(c, width=w, anchor="w")
        tree.pack(fill="both", expand=True)
        for r in rows:
            tree.insert("", "end", iid=str(r["todo_id"]), values=(
                r["display_name"] or "", r["kind"] or "", r["due"] or "", r["title"] or ""))
        tree.bind("<Double-1>", lambda e: self._close_todo(tree))
        self.todo_tree = tree
        self.status_var.set(f"{len(rows)} 条待办 · 双击标记完成")

    def _close_todo(self, tree):
        sel = tree.selection()
        if not sel:
            return
        tid = sel[0]
        con = self._con()
        if not con:
            return
        con.execute("UPDATE todo SET status='done', closed_at=? WHERE todo_id=?",
                    (time.time(), tid))
        con.commit()
        con.close()
        self._refresh_todos()
        self.status_var.set("已标记完成")

    # ───────────────────────── 视图：事件 ─────────────────────────
    def _refresh_events(self):
        v = self.views["events"]
        for c in v.winfo_children():
            c.destroy()
        con = self._con()
        if not con:
            ttk.Label(v, text="还没有数据。", style="Sub.TLabel").pack(anchor="w", pady=20)
            return
        rows = [dict(r) for r in con.execute(
            "SELECT e.event_id, e.kind, e.ts, e.title, e.detail, p.display_name "
            "FROM event e LEFT JOIN person p ON p.person_key=e.person_key "
            "ORDER BY e.ts DESC LIMIT 200").fetchall()]
        con.close()
        if not rows:
            ttk.Label(v, text="还没有记录事件。", style="Sub.TLabel").pack(anchor="w", pady=20)
            return
        tree = ttk.Treeview(v, columns=("when", "who", "kind", "title"), show="headings")
        for c, t, w in (("when", "时间", 130), ("who", "人", 130),
                       ("kind", "类型", 90), ("title", "内容", 440)):
            tree.heading(c, text=t)
            tree.column(c, width=w, anchor="w")
        tree.pack(fill="both", expand=True)
        for r in rows:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["ts"])) if r["ts"] else ""
            tree.insert("", "end", iid=str(r["event_id"]), values=(
                when, r["display_name"] or "", r["kind"] or "", r["title"] or ""))
        self.status_var.set(f"{len(rows)} 条事件")

    # ───────────────────────── 视图：搜索 ─────────────────────────
    def _refresh_search(self):
        v = self.views["search"]
        for c in v.winfo_children():
            c.destroy()
        self.search_result = ttk.Treeview(v, columns=("when", "who", "snippet"), show="headings")
        for c, t, w in (("when", "时间", 130), ("who", "人", 130), ("snippet", "内容", 560)):
            self.search_result.heading(c, text=t)
            self.search_result.column(c, width=w, anchor="w")
        self.search_result.pack(fill="both", expand=True)
        ttk.Label(v, text="在上方搜索框输入关键词后回车。", style="Sub.TLabel").pack(anchor="w", pady=(8, 0))

    def _do_search(self):
        q = self.search_var.get().strip()
        if not q:
            return
        con = self._con()
        if not con:
            self.status_var.set("没有数据可搜")
            return
        try:
            import core
            rows = core.search_messages(con, q, limit=80)
        except Exception as exc:
            # 不能静默吞：把真因显示出来，否则「搜不到」和「搜坏了」分不清
            rows = []
            self.status_var.set(f"检索失败：{exc}")
            print(f"[renpu] 检索失败（{q!r}）：{exc}")
        names = {}
        for r in rows:
            pk = r["person_key"]
            if pk not in names:
                nm = con.execute("SELECT display_name FROM person WHERE person_key=?", (pk,)).fetchone()
                names[pk] = nm[0] if nm else pk
        con.close()

        if not hasattr(self, "search_result"):
            return
        self.search_result.delete(*self.search_result.get_children())
        for i, r in enumerate(rows):
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["ts"])) if r["ts"] else ""
            snip = (r["snippet"] or "").replace("\n", " ")[:200]
            self.search_result.insert("", "end", iid=str(i), values=(when, names.get(r["person_key"], ""), snip))
        self.status_var.set(f"「{q}」命中 {len(rows)} 条")

    # ───────────────────────── 视图：Agent 接入 ─────────────────────────
    def _refresh_api(self):
        v = self.views["api"]
        for c in v.winfo_children():
            c.destroy()
        ttk.Label(v, text="启动一个只绑 127.0.0.1 的本地 HTTP 接口，"
                          "让你的 AI 助手直接调用仁谱。数据不出本机。",
                  style="Sub.TLabel").pack(anchor="w", pady=(0, 14))
        self.api_state = ttk.Label(v, text=self._api_status_text(), style="Card.TLabel")
        self.api_state.pack(anchor="w")
        row = ttk.Frame(v, style="Root.TFrame")
        row.pack(fill="x", pady=(14, 0))
        ttk.Button(row, text="启动 API", style="Primary.TButton",
                   command=self.start_api).pack(side="left")
        ttk.Button(row, text="停止", style="Ghost.TButton",
                   command=self.stop_api).pack(side="left", padx=(10, 0))
        ttk.Button(row, text="打开接口目录", style="Ghost.TButton",
                   command=lambda: webbrowser.open(f"http://127.0.0.1:{self._api_port()}/api")).pack(
                       side="left", padx=(10, 0))
        ex = ttk.Frame(v, style="Panel.TFrame", padding=16)
        ex.pack(fill="x", pady=(18, 0))
        ttk.Label(ex, text="可用端点", style="Card.TLabel").pack(anchor="w")
        for line in ("GET  /api/stats        总览统计",
                     "GET  /api/people?q=    联系人（可搜）",
                     "GET  /api/who?name=    单人档案",
                     "GET  /api/search?q=    全文检索",
                     "GET  /api/feed?days=   近期消息流",
                     "GET  /api/edges        关系谱",
                     "GET  /api/todos        待办",
                     "POST /api/note         写回事件/待办"):
            ttk.Label(ex, text=line, style="CardS.TLabel").pack(anchor="w")

    def _api_port(self):
        try:
            return int(self.cfg.get("port") or 8877)
        except (TypeError, ValueError):
            return 8877

    def _api_status_text(self):
        if self.api_proc and self.api_proc.poll() is None:
            return f"● 运行中 → http://127.0.0.1:{self._api_port()}/api"
        return "○ 未启动"

    def start_api(self):
        if self.api_proc and self.api_proc.poll() is None:
            self.status_var.set("API 已经在跑了")
            return
        db = self.active_db
        if not db.exists():
            messagebox.showinfo("还没有数据", "先生成演示数据或接入真实数据。")
            return
        env = os.environ.copy()
        env["RENPU_DB_PATH"] = str(db)
        env["RENPU_DATA_HOME"] = str(db.parent)
        env["PYTHONIOENCODING"] = "utf-8"
        port = self._api_port()
        try:
            cmd = [sys.executable, str(app_root() / "server.py"), "--port", str(port)] \
                if not getattr(sys, "frozen", False) else [sys.executable, "--api", str(port)]
            self.api_proc = subprocess.Popen(cmd, cwd=str(app_root()), env=env,
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.status_var.set(f"Agent API 已启动：http://127.0.0.1:{port}/api")
            if hasattr(self, "api_state"):
                self.api_state.configure(text=self._api_status_text())
        except Exception as exc:
            messagebox.showerror("API 启动失败", str(exc))

    def stop_api(self):
        if self.api_proc and self.api_proc.poll() is None:
            self.api_proc.terminate()
            try:
                self.api_proc.wait(timeout=5)
            except Exception:
                pass
        self.api_proc = None
        self.status_var.set("API 已停止")
        if hasattr(self, "api_state"):
            self.api_state.configure(text=self._api_status_text())

    # ───────────────────────── 视图：设置 ─────────────────────────
    def _refresh_settings(self):
        v = self.views["settings"]
        for c in v.winfo_children():
            c.destroy()
        self.mode_label.configure(text="演示库" if self.mode == "demo" else "真实库")

        ttk.Label(v, text="当前生效的库", style="Card.TLabel").pack(anchor="w")
        ttk.Label(v, text=str(self.active_db), style="Sub.TLabel").pack(anchor="w", pady=(4, 16))

        row = ttk.Frame(v, style="Root.TFrame")
        row.pack(fill="x", pady=(0, 18))
        ttk.Button(row, text="使用演示库", style="Ghost.TButton",
                   command=self.use_demo).pack(side="left")
        ttk.Button(row, text="切换到真实库", style="Ghost.TButton",
                   command=self.use_real).pack(side="left", padx=(10, 0))

        # 微信源配置
        card = ttk.Frame(v, style="Panel.TFrame", padding=18)
        card.pack(fill="x", pady=(0, 18))
        ttk.Label(card, text="接入微信源（可选）", style="Card.TLabel").pack(anchor="w")
        ttk.Label(card, text="填你的微信数据目录，仁谱只读不写。留空则只用演示库。",
                  style="CardS.TLabel").pack(anchor="w", pady=(4, 12))

        self.wc_live_var = tk.StringVar(value=self.cfg.get("wc_live") or "")
        line = ttk.Frame(card, style="Panel.TFrame")
        line.pack(fill="x", pady=(0, 8))
        ttk.Label(line, text="db_storage 目录", style="CardS.TLabel").pack(side="left")
        ttk.Entry(line, textvariable=self.wc_live_var, style="TSearch.TEntry").pack(
            side="left", fill="x", expand=True, padx=(10, 8))
        ttk.Button(line, text="浏览", style="Ghost.TButton", command=self._pick_wc).pack(side="left")

        self.self_wxid_var = tk.StringVar(value=self.cfg.get("self_wxid") or "")
        line2 = ttk.Frame(card, style="Panel.TFrame")
        line2.pack(fill="x", pady=(0, 12))
        ttk.Label(line2, text="我的 wxid（可选）", style="CardS.TLabel").pack(side="left")
        ttk.Entry(line2, textvariable=self.self_wxid_var, style="TSearch.TEntry").pack(
            side="left", fill="x", expand=True, padx=(10, 8))

        ttk.Button(card, text="保存配置", style="Primary.TButton", command=self._save_cfg).pack(anchor="w")
        ttk.Button(card, text="拉取并摄取微信数据", style="Ghost.TButton",
                   command=self._ingest_real).pack(anchor="w", pady=(10, 0))

        info = ttk.Frame(v, style="Panel.TFrame", padding=16)
        info.pack(fill="x")
        ttk.Label(info, text="目录", style="Card.TLabel").pack(anchor="w")
        ttk.Label(info, text=f"数据：{user_data_dir()}\n配置：{user_config_path()}\n"
                              f"导出：{user_data_dir() / 'exports'}",
                  style="CardS.TLabel").pack(anchor="w", pady=(6, 10))
        ttk.Button(info, text="打开数据目录", style="Ghost.TButton",
                   command=lambda: os.startfile(str(user_data_dir()))).pack(anchor="w")

    def _pick_wc(self):
        d = filedialog.askdirectory(title="选择微信 db_storage 目录")
        if d:
            self.wc_live_var.set(d)

    def _save_cfg(self):
        cfg = dict(self.cfg)
        if self.wc_live_var.get().strip():
            cfg["wc_live"] = self.wc_live_var.get().strip()
        if self.self_wxid_var.get().strip():
            cfg["self_wxid"] = self.self_wxid_var.get().strip()
        self.cfg = cfg
        save_user_config(cfg)
        self.status_var.set(f"配置已保存 → {user_config_path()}")

    def _ingest_real(self):
        self._save_cfg()
        if not self.cfg.get("wc_live"):
            messagebox.showinfo("还没填路径", "先填微信 db_storage 目录再摄取。")
            return
        db = self.real_dir / "renpu.db"
        env = {"RENPU_DB_PATH": str(db), "RENPU_DATA_HOME": str(self.real_dir),
               "RENPU_LOG_DIR": str(self.real_dir / "logs"),
               "RENPU_EXPORT_DIR": str(self.real_dir / "exports"),
               "RENPU_WC_LIVE": self.cfg.get("wc_live", "")}
        if self.cfg.get("self_wxid"):
            env["RENPU_SELF_WXID"] = self.cfg["self_wxid"]
        self._run_bg(["init"], "库已建", env=env, after_ok=self._ingest_real2)
        self._ingest_env = env

    def _ingest_real2(self):
        env = getattr(self, "_ingest_env", None)
        if not env:
            return
        self._run_bg(["ingest"], "微信数据摄取完成", env=env, after_ok=self._switch_real)

    def _switch_real(self):
        self.mode = "real"
        self.active_db = self.real_dir / "renpu.db"
        self.mode_label.configure(text="真实库")
        self.status_var.set(f"已切换到真实库：{self.active_db}")

    def use_demo(self):
        self.mode = "demo"
        self.active_db = self.demo_dir / "renpu.db"
        self.mode_label.configure(text="演示库")
        self.status_var.set("已切换到演示库")

    def use_real(self):
        self.mode = "real"
        self.active_db = self.real_dir / "renpu.db"
        self.mode_label.configure(text="真实库")
        self.status_var.set(f"已切换到真实库：{self.active_db}")

    def make_demo(self):
        """生成演示数据（隔离目录，不碰真实库）。"""
        def work():
            self.demo_dir.mkdir(parents=True, exist_ok=True)
            env = {
                "RENPU_DATA_HOME": str(self.demo_dir),
                "RENPU_DB_PATH": str(self.demo_dir / "renpu.db"),
                "RENPU_LOG_DIR": str(self.demo_dir / "logs"),
                "RENPU_EXPORT_DIR": str(self.demo_dir / "exports"),
                "PYTHONIOENCODING": "utf-8",
            }
            ok, detail = self._run_cli(["tour", "--out", str(self.demo_dir)], env=env)
            self.after(0, lambda: self._after_demo(ok, detail))

        threading.Thread(target=work, daemon=True).start()

    def _after_demo(self, ok, detail):
        if not ok:
            self.status_var.set("演示数据生成失败。")
            messagebox.showerror("生成失败", (detail or "")[:1500])
            return
        self.mode = "demo"
        self.active_db = self.demo_dir / "renpu.db"
        self.mode_label.configure(text="演示库")
        self.status_var.set(f"演示数据已就绪：{self.active_db}")
        self.navigate("overview")

    # ───────────────────────── 后台任务 ─────────────────────────
    def _run_bg(self, args, ok_msg, env=None, open_after=False, after_ok=None):
        """后台跑一条 CLI 命令，不卡界面。"""
        if self.busy:
            self.status_var.set("上一个任务还在跑，稍等。")
            return
        self.busy = True
        self.status_var.set(f"正在执行：renpu {' '.join(args)} …")

        def work():
            ok, detail = self._run_cli(args, env=env)
            self.after(0, lambda: self._finish_bg(ok, detail, ok_msg, open_after, after_ok))

        threading.Thread(target=work, daemon=True).start()

    def _finish_bg(self, ok, detail, ok_msg, open_after, after_ok):
        self.busy = False
        if not ok:
            self.status_var.set("执行失败。")
            messagebox.showerror("执行失败", (detail or "")[:1500])
            return
        self.status_var.set(ok_msg)
        if after_ok:
            after_ok()
            return
        if open_after:
            for cand in (user_data_dir() / "exports" / "renpu-graph3d.html",
                         user_data_dir() / "exports" / "renpu-graph.html"):
                if cand.exists() and str(cand) in (ok_msg or ""):
                    webbrowser.open(cand.resolve().as_uri())
                    break

    def _run_cli(self, args, env=None):
        """执行一条 CLI 命令，返回 (ok, 输出)。"""
        import contextlib
        import io
        buf = io.StringIO()
        saved = {}
        if env:
            for k in ("RENPU_DATA_HOME", "RENPU_DB_PATH", "RENPU_LOG_DIR",
                      "RENPU_EXPORT_DIR", "RENPU_WC_LIVE", "RENPU_SELF_WXID"):
                saved[k] = os.environ.get(k)
                if k in env:
                    os.environ[k] = env[k]
        try:
            if getattr(sys, "frozen", False):
                # 单文件 EXE：进程内调用（每次启动都要解压运行时，fork 子进程太慢）
                #
                # 关键：进程内调用不会重新 import config，所以光改环境变量没用。
                # 必须把 config/core 里已绑定的路径常量也一起改掉，
                # 否则演示数据会写进用户的真实库。
                #
                # 注意 env 可能是 None（比如「生成关系图谱」不带 env）——
                # 那时也必须重定向到当前生效的库，否则 GUI 画的是演示数据，
                # CLI 读的却是用户真实库（两套数据对不上）。
                target = env or {
                    "RENPU_DATA_HOME": str(self.active_db.parent),
                    "RENPU_DB_PATH": str(self.active_db),
                    "RENPU_LOG_DIR": str(self.active_db.parent / "logs"),
                    "RENPU_EXPORT_DIR": str(self.active_db.parent / "exports"),
                }
                retarget_paths(target)
                import cli as cli_mod
                ns = cli_mod.build_parser().parse_args(list(args))
                with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                    cli_mod.dispatch(ns)
                return True, buf.getvalue()
            cmd = [sys.executable, str(app_root() / "cli.py"), *args]
            r = subprocess.run(cmd, cwd=str(app_root()),
                               env={**os.environ, **(env or {})},
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            if r.returncode != 0:
                return False, (r.stderr or r.stdout)
            return True, r.stdout
        except SystemExit as e:
            # SystemExit(0) 是「正常跑完」，不是失败。
            # tour 走完就是 raise SystemExit(0) —— 以前这里一律当失败，
            # 结果 GUI 弹「生成失败」错误框，演示数据其实早就建好了。
            code = e.code
            if code in (0, None):
                return True, buf.getvalue()
            return False, buf.getvalue() or f"退出码 {code}"
        except Exception as exc:
            return False, str(exc)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def reload(self):
        """重新载入当前视图的数据。"""
        self.navigate(self._current_view())

    def _current_view(self):
        for k, b in self.nav_buttons.items():
            try:
                if "active" in b.state():
                    return k
            except tk.TclError:
                continue
        return "overview"

    def destroy(self):
        self.stop_api()
        super().destroy()


if __name__ == "__main__":
    app = RenpuDesktop()
    app.mainloop()
